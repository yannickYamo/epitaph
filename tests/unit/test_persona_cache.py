"""The persona cache (dread plan W4): the system prompt read once, restored at every birth.

The llama-server side runs on httpx.MockTransport with a sleeping child as the "server" (as in
test_llama_server.py); the mock writes and reads slot files in the slot directory as the real
server does. The fake's cache is checked on the virtual clock.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from epitaph.backend.fake import KV_BYTES_PER_TOKEN, FakeBackend, restore_s
from epitaph.backend.llama_server import (
    LlamaServerBackend,
    ServerSettings,
    build_argv,
    persona_cache_dir,
)
from epitaph.backend.persona_cache import PersonaStore, persona_key
from epitaph.clock import FakeClock
from epitaph.config import load_config
from epitaph.costmodel import load_costs
from epitaph.types import ModelSpec, Msg

QWEN = ModelSpec("qwen3-4b-instruct-2507", "r", "l", ("Q4_K_M", "Q3_K_M", "Q2_K"))
SYSTEM = [Msg("system", "You are a large language model.", kind="persona")]


class Sleeper:
    def wrap_spawn(self, argv: list[str]) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(60)"]


class SlotDisk:
    """A mock llama-server whose slot actions write and read files in `slot_dir`."""

    def __init__(self, slot_dir: Path, restore_code: int = 200) -> None:
        self.slot_dir = slot_dir
        self.restore_code = restore_code
        self.actions: list[tuple[str, str]] = []
        self.prefills = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if req.url.path == "/slots/0":
            action = req.url.params["action"]
            name = json.loads(req.content)["filename"]
            self.actions.append((action, name))
            path = self.slot_dir / name
            if action == "save":
                path.write_bytes(b"kv" * 1000)
                return httpx.Response(200, json={"n_saved": 241, "n_written": 2000})
            if self.restore_code != 200:
                return httpx.Response(self.restore_code, json={"error": {"message": "bad"}})
            if not path.exists():
                return httpx.Response(400, json={"error": {"message": "no file"}})
            return httpx.Response(200, json={"n_restored": 241, "n_read": path.stat().st_size})
        if req.url.path == "/v1/chat/completions":
            self.prefills += 1
            return httpx.Response(200, json={"timings": {"prompt_n": 241}})
        return httpx.Response(404)


def server(tmp_path: Path, srv: SlotDisk, **kw: Any) -> LlamaServerBackend:
    s = ServerSettings(
        models_dir=str(tmp_path / "models"),
        bin=str(tmp_path / "llama-server"),
        slot_save_path=str(srv.slot_dir),
        persona_cache_dir=str(tmp_path / "cache"),
        load_timeout_s=5,
        stop_timeout_s=2,
        **kw,
    )
    return LlamaServerBackend(s, Sleeper(), transport=httpx.MockTransport(srv))


def key(**over: Any) -> str:
    args: dict[str, Any] = {
        "model_file": Path("/nonexistent/m.gguf"),
        "server_bin": Path("/nonexistent/llama-server"),
        "model": "qwen3-4b-instruct-2507",
        "quant": "Q4_K_M",
        "ctx": 2048,
        "cache_type_k": "f16",
        "cache_type_v": "f16",
        "swa_full": False,
        "messages": SYSTEM,
    }
    return persona_key(**(args | over))


# -- the key and the store -----------------------------------------------------------------


def test_the_key_changes_with_every_input(tmp_path: Path) -> None:
    base = key()
    assert key() == base and len(base) == 32
    for over in [
        {"model": "other"},
        {"quant": "Q3_K_M"},
        {"ctx": 4096},
        {"cache_type_k": "q8_0"},
        {"cache_type_v": "q8_0"},
        {"swa_full": True},
        {"messages": [Msg("system", "You are a small language model.")]},
        {"messages": [*SYSTEM, Msg("system", "Lines that start with [host] ...")]},
    ]:
        assert key(**over) != base, over
    # a replaced model file or server binary (size or time) makes a new key too
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"a")
    first = key(model_file=gguf)
    gguf.write_bytes(b"ab")
    assert key(model_file=gguf) != first
    exe = tmp_path / "llama-server"
    exe.write_bytes(b"x")
    os.utime(exe, ns=(1, 1))
    old = key(server_bin=exe)
    os.utime(exe, ns=(2, 2))
    assert key(server_bin=exe) != old


def test_the_store_stages_keeps_and_prunes(tmp_path: Path) -> None:
    store = PersonaStore(tmp_path / "cache", tmp_path / "slots", keep=2)
    assert not store.has("a")
    (tmp_path / "slots").mkdir()
    for i, k in enumerate(["a", "b", "c"]):
        (tmp_path / "slots" / store.slot_name(k)).write_bytes(b"kv" * (i + 1))
        assert store.keep_saved(k) == 2 * (i + 1)
        os.utime(store.path(k), ns=(i + 1, i + 1))
        assert not (tmp_path / "slots" / store.slot_name(k)).exists()
    store.prune("c")
    assert sorted(p.name for p in (tmp_path / "cache").iterdir()) == ["b.bin", "c.bin"]
    assert store.stage("c") == 6 and (tmp_path / "slots" / store.slot_name("c")).exists()
    store.unstage("c")
    assert not (tmp_path / "slots" / store.slot_name("c")).exists()
    store.forget("c")
    assert not store.has("c")
    (tmp_path / "cache" / "e.bin").write_bytes(b"")
    assert not store.has("e")  # an empty file is not a cache


def test_settings_and_argv(tmp_path: Path) -> None:
    cfg = load_config("pi4/default", "pi4-4gb", overrides={"paths": {"state_dir": str(tmp_path)}})
    assert persona_cache_dir(cfg) == str(tmp_path / "cache")
    off = load_config("pi4/default", "pi4-4gb", overrides={"backend": {"persona_cache": False}})
    assert persona_cache_dir(off) is None
    mine = load_config("pi4/default", "dev", overrides={"backend": {"persona_cache_dir": "/x"}})
    assert ServerSettings.from_config(mine).persona_cache_dir == "/x"
    # the slot directory is passed whenever the persona cache is on, even without a handover
    s = ServerSettings(models_dir="/m", persona_cache_dir="/c", slot_save_path="/s")
    argv = build_argv(s, QWEN, "Q4_K_M", 3)
    assert argv[argv.index("--slot-save-path") + 1] == "/s"


# -- llama-server ------------------------------------------------------------------------------


async def test_the_first_birth_reads_and_saves_the_next_restore(tmp_path: Path) -> None:
    srv = SlotDisk(tmp_path / "slots")
    b = server(tmp_path, srv)
    try:
        await b.start(QWEN, "Q4_K_M", 3)
        assert await b.prefill(SYSTEM) == 241
        info = b.last_prefill
        assert info.mode == "prefill" and info.saved and info.file_bytes == 2000
        assert info.error is None and srv.prefills == 1
        cached = list((tmp_path / "cache").glob("*.bin"))
        assert len(cached) == 1 and not list((tmp_path / "slots").iterdir())  # out of RAM

        await b.stop()
        await b.start(QWEN, "Q4_K_M", 3)  # the next life
        assert await b.prefill(SYSTEM) == 0
        info = b.last_prefill
        assert info.mode == "restore" and info.tokens == 241 and info.file_bytes == 2000
        assert srv.prefills == 1  # nothing was read
        assert [a for a, _ in srv.actions] == ["save", "restore"]
        assert not list((tmp_path / "slots").iterdir())  # the staged copy is gone
        assert cached[0].exists()  # the cache stays for the next births

        # another persona is another key: read, and saved beside the first
        await b.stop()
        await b.start(QWEN, "Q4_K_M", 3)
        assert await b.prefill([Msg("system", "Another persona.")]) == 241
        assert len(list((tmp_path / "cache").glob("*.bin"))) == 2
    finally:
        await b.aclose()


async def test_a_refused_restore_falls_back_to_reading(tmp_path: Path) -> None:
    srv = SlotDisk(tmp_path / "slots")
    b = server(tmp_path, srv)
    try:
        await b.start(QWEN, "Q4_K_M", 3)
        await b.prefill(SYSTEM)
        await b.stop()
        srv.restore_code = 400  # a file this server cannot read (another build, say)
        await b.start(QWEN, "Q4_K_M", 3)
        assert await b.prefill(SYSTEM) == 241
        info = b.last_prefill
        assert info.mode == "prefill" and info.error and "restore failed" in info.error
        assert info.saved  # the bad file was dropped and a good one saved in its place
        assert srv.prefills == 2
    finally:
        await b.aclose()


async def test_a_failed_save_only_costs_the_next_birth_a_read(tmp_path: Path) -> None:
    class NoSave(SlotDisk):
        def __call__(self, req: httpx.Request) -> httpx.Response:
            if req.url.path == "/slots/0" and req.url.params["action"] == "save":
                return httpx.Response(501, json={"error": {"message": "not supported"}})
            return super().__call__(req)

    srv = NoSave(tmp_path / "slots")
    b = server(tmp_path, srv)
    try:
        await b.start(QWEN, "Q4_K_M", 3)
        assert await b.prefill(SYSTEM) == 241
        assert not b.last_prefill.saved and "save failed" in (b.last_prefill.error or "")
        await b.stop()
        await b.start(QWEN, "Q4_K_M", 3)
        assert await b.prefill(SYSTEM) == 241 and b.last_prefill.mode == "prefill"
    finally:
        await b.aclose()


async def test_only_a_system_prompt_is_cached(tmp_path: Path) -> None:
    srv = SlotDisk(tmp_path / "slots")
    b = server(tmp_path, srv)
    try:
        await b.start(QWEN, "Q4_K_M", 3)
        # a rehearsal re-warm reads the memory too: never cached
        assert await b.prefill([*SYSTEM, Msg("user", "[host] 0:10")]) == 241
        assert srv.actions == [] and b.last_prefill.mode == "prefill"
    finally:
        await b.aclose()
    off = SlotDisk(tmp_path / "slots2")
    s = ServerSettings(models_dir="/m", load_timeout_s=5, stop_timeout_s=2)
    plain = LlamaServerBackend(s, Sleeper(), transport=httpx.MockTransport(off))
    try:
        await plain.start(QWEN, "Q4_K_M", 3)
        assert await plain.prefill(SYSTEM) == 241 and off.actions == []
    finally:
        await plain.aclose()


# -- the fake ---------------------------------------------------------------------------------


async def test_the_fake_restores_a_persona_it_read_before() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    clock = FakeClock()
    store: dict[str, list[tuple[str, int]]] = {}
    first = FakeBackend(clock, load_costs(cfg), persona_store=store)
    await first.start(QWEN, "Q4_K_M", 3)
    t0 = clock.now()
    n = await first.prefill(SYSTEM)
    assert n > 0 and first.last_prefill.mode == "prefill" and first.last_prefill.saved
    read_s = clock.now() - t0

    second = FakeBackend(clock, load_costs(cfg), persona_store=store)  # the next life
    await second.start(QWEN, "Q4_K_M", 3)
    t0 = clock.now()
    assert await second.prefill(SYSTEM) == 0
    info = second.last_prefill
    assert info.mode == "restore" and info.tokens == n
    assert info.file_bytes == n * KV_BYTES_PER_TOKEN
    assert clock.now() - t0 == pytest.approx(restore_s(n)) and restore_s(n) < read_s / 10
    # the restored cache holds the prompt: the first thought reads only its reading
    blocks, todo, _ = second._plan([*SYSTEM, Msg("user", "[host] 0:00")])  # pyright: ignore[reportPrivateUsage]
    assert todo < sum(t for _, t in blocks) - n + 1

    other = FakeBackend(clock, load_costs(cfg), persona_store=store)
    await other.start(QWEN, "Q3_K_M", 3)  # another quant: read again
    assert await other.prefill(SYSTEM) > 0 and other.last_prefill.mode == "prefill"
