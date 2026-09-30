"""The llama-server backend without a model: argv, SSE parsing, errors, the process watcher.

HTTP goes through httpx.MockTransport; the "server" process is a sleeping Python child, so no
network and no model are needed. The real server is exercised in tests/templates (model mark).
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from typing import Any

import httpx
import pytest

from epitaph.backend.base import CreatureDied
from epitaph.backend.errors import BackendError, ContextFull
from epitaph.backend.llama_server import (
    LlamaServerBackend,
    ServerSettings,
    build_argv,
    chunk_from_event,
    parse_sse_line,
    request_body,
)
from epitaph.config import load_config
from epitaph.types import Chunk, CreatureStatus, ModelSpec, Msg, Sampling

MODEL = ModelSpec("llama-3.2-3b-instruct", "r", "l", ("Q6_K", "Q4_K_M", "Q2_K"))
GEMMA = ModelSpec("gemma-3-4b-it", "r", "l", ("Q4_K_M",), sliding_window=True)
SAMPLING = Sampling(temperature=0.7, min_p=0.05, seed=3)


def sse(events: list[dict[str, Any]]) -> bytes:
    return b"".join(f"data: {json.dumps(e)}\n\n".encode() for e in events) + b"data: [DONE]\n\n"


def chat_events(words: list[str]) -> list[dict[str, Any]]:
    evs: list[dict[str, Any]] = [
        {"choices": [{"delta": {"content": w}, "finish_reason": None}]} for w in words
    ]
    evs.append(
        {
            "choices": [{"delta": {}, "finish_reason": "length"}],
            "timings": {
                "prompt_n": 45,
                "predicted_n": len(words),
                "prompt_per_second": 9.5,
                "predicted_per_second": 1.4,
            },
        }
    )
    return evs


class SleeperBody:
    """Places the 'server' in a sleeping Python child instead of llama-server."""

    def __init__(self, code: str = "import time; time.sleep(60)") -> None:
        self.code = code
        self.seen: list[list[str]] = []

    def wrap_spawn(self, argv: list[str]) -> list[str]:
        self.seen.append(argv)
        return [sys.executable, "-c", self.code]


def make(handler: Any, body: Any = None, **kw: Any) -> LlamaServerBackend:
    s = ServerSettings(models_dir="/m", load_timeout_s=5, stop_timeout_s=2, **kw)
    return LlamaServerBackend(s, body or SleeperBody(), transport=httpx.MockTransport(handler))


def healthy(extra: Any = None) -> Any:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if extra is not None:
            return extra(req)
        return httpx.Response(404)

    return handler


def test_argv_defaults_and_options() -> None:
    s = ServerSettings(models_dir="/m", port=9000, ctx=2048, cache_reuse=256)
    argv = build_argv(s, MODEL, "Q6_K", 3)
    assert argv[argv.index("-m") + 1] == "/m/llama-3.2-3b-instruct/Q6_K.gguf"
    for flag, val in [("-c", "2048"), ("-t", "3"), ("-np", "1"), ("--cache-reuse", "256")]:
        assert argv[argv.index(flag) + 1] == val
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert "--jinja" in argv and "--swa-full" not in argv and "--load-mode" not in argv
    s2 = ServerSettings(models_dir="/m", cache_reuse=0, mmap=False, cache_type_k="q8_0")
    argv2 = build_argv(s2, GEMMA, "Q4_K_M", 2)
    assert "--swa-full" in argv2 and "--cache-reuse" not in argv2
    assert argv2[argv2.index("--load-mode") + 1] == "none"
    assert argv2[argv2.index("-ctk") + 1] == "q8_0"
    assert "--swa-full" not in build_argv(ServerSettings(swa_full=False), GEMMA, "Q4_K_M", 2)


def test_settings_from_config() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    s = ServerSettings.from_config(cfg)
    assert s.ctx == 2048 and s.port == 8081 and s.cache_reuse == 256
    assert s.load_timeout_s == 300


def test_request_body() -> None:
    b = request_body([Msg("system", "s"), Msg("user", "u")], SAMPLING, 70)
    assert b["stream"] and b["cache_prompt"] and b["max_tokens"] == 70 and b["seed"] == 3
    assert b["messages"] == [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert b["chat_template_kwargs"] == {"enable_thinking": False}
    assert b["dry_multiplier"] == 0.8 and "grammar" not in b
    raw = request_body(None, Sampling(0.7, 0.05, latin_only=True), 20, prompt="Dear")
    assert raw["prompt"] == "Dear" and raw["n_predict"] == 20 and "grammar" in raw


def test_sse_parsing() -> None:
    assert parse_sse_line(": keep-alive") is None
    assert parse_sse_line("data: [DONE]") is None
    assert parse_sse_line('data: {"a": 1}') == {"a": 1}
    text, t = chunk_from_event({"choices": [{"delta": {"content": "hi"}}]}, chat=True)
    assert (text, t) == ("hi", None)
    text, t = chunk_from_event({"content": "x", "stop": True, "timings": {"prompt_n": 1}}, False)
    assert text == "x" and t == {"prompt_n": 1}
    with pytest.raises(ContextFull):
        chunk_from_event(
            {"error": {"type": "exceed_context_size_error", "message": "exceeds context"}}, True
        )
    with pytest.raises(BackendError):
        chunk_from_event({"error": {"message": "boom"}}, True)


async def test_start_stream_and_timings() -> None:
    def api(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/chat/completions"
        assert json.loads(req.content)["messages"][0]["role"] == "system"
        return httpx.Response(200, content=sse(chat_events(["I ", "am ", "here."])))

    body = SleeperBody()
    b = make(healthy(api), body)
    await b.start(MODEL, "Q6_K", 3)
    try:
        assert body.seen and "-m" in body.seen[0]
        assert b.status().alive and b.load_s is not None
        chunks: list[Chunk] = [c async for c in b.chat([Msg("system", "s")], SAMPLING, 3)]
        assert "".join(c.text for c in chunks) == "I am here."
        last = chunks[-1]
        assert last.done and last.prompt_n == 45 and last.predicted_n == 3
        assert last.predicted_per_s == pytest.approx(1.4)
        assert b.status().tok_s == pytest.approx(1.4)
    finally:
        await b.aclose()
    assert not b.status().alive


async def test_complete_stream() -> None:
    def api(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/completion"
        evs = [{"content": "a"}, {"content": "b", "stop": True, "timings": {"predicted_n": 2}}]
        return httpx.Response(200, content=sse(evs))

    b = make(healthy(api))
    await b.start(MODEL, "Q6_K", 3)
    try:
        chunks = [c async for c in b.complete("Dear diary", SAMPLING, 2)]
        assert [c.text for c in chunks[:-1]] == ["a", "b"] and chunks[-1].predicted_n == 2
    finally:
        await b.aclose()


async def test_http_error_maps_to_context_full() -> None:
    def api(req: httpx.Request) -> httpx.Response:
        err = {"code": 400, "type": "exceed_context_size_error", "message": "exceeds the context",
               "n_prompt_tokens": 2100, "n_ctx": 2048}  # fmt: skip
        return httpx.Response(400, json={"error": err})

    b = make(healthy(api))
    await b.start(MODEL, "Q6_K", 3)
    try:
        with pytest.raises(ContextFull) as e:
            _ = [c async for c in b.chat([Msg("user", "u")], SAMPLING, 3)]
        assert e.value.ctx == 2048
    finally:
        await b.aclose()


async def test_death_between_requests_fires_on_death_and_breaks_next_call() -> None:
    b = make(healthy())
    seen: list[CreatureStatus] = []
    b.on_death(seen.append)
    await b.start(MODEL, "Q6_K", 3)
    pid = b.status().pid
    assert pid is not None
    import os

    os.kill(pid, signal.SIGKILL)  # the kernel's OOM kill
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.02)
    assert seen and seen[0].signal == signal.SIGKILL and not b.status().alive
    with pytest.raises(CreatureDied):
        _ = [c async for c in b.chat([Msg("user", "u")], SAMPLING, 3)]
    await b.aclose()


async def test_broken_stream_from_a_dead_process_raises_creature_died() -> None:
    holder: dict[str, LlamaServerBackend] = {}

    def api(req: httpx.Request) -> httpx.Response:
        import os

        pid = holder["b"].status().pid
        assert pid is not None
        os.kill(pid, signal.SIGKILL)
        raise httpx.RemoteProtocolError("peer closed connection")

    b = make(healthy(api))
    holder["b"] = b
    await b.start(MODEL, "Q6_K", 3)
    with pytest.raises(CreatureDied) as e:
        _ = [c async for c in b.chat([Msg("user", "u")], SAMPLING, 3)]
    assert e.value.status.signal == signal.SIGKILL
    await b.aclose()


async def test_stop_is_not_a_death() -> None:
    b = make(healthy())
    seen: list[CreatureStatus] = []
    b.on_death(seen.append)
    await b.start(MODEL, "Q6_K", 3)
    await b.stop()
    await asyncio.sleep(0.05)
    assert not seen and not b.status().alive
    await b.aclose()


async def test_exit_during_load_raises_creature_died() -> None:
    def never_ok(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "loading"})

    b = make(never_ok, SleeperBody("import sys; sys.exit(3)"))
    with pytest.raises(CreatureDied) as e:
        await b.start(MODEL, "Q6_K", 3)
    assert e.value.status.exit_code == 3
    await b.aclose()


async def test_load_timeout_kills_the_child() -> None:
    def never_ok(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "loading"})

    s = ServerSettings(models_dir="/m", load_timeout_s=0.5, stop_timeout_s=1)
    b = LlamaServerBackend(s, SleeperBody(), transport=httpx.MockTransport(never_ok))
    with pytest.raises(TimeoutError):
        await b.start(MODEL, "Q6_K", 3)
    assert not b.status().alive
    await b.aclose()


async def test_count_past_tokens_renders_and_tokenizes() -> None:
    def api(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        if req.url.path == "/apply-template":
            text = "".join(f"<{m['role']}>{m['content']}" for m in payload["messages"])
            return httpx.Response(200, json={"prompt": text})
        if req.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": list(range(len(payload["content"])))})
        return httpx.Response(404)

    b = make(healthy(api))
    await b.start(MODEL, "Q6_K", 3)
    try:
        n = await b.count_past_tokens([Msg("user", "abc"), Msg("assistant", "de")])
        assert n == len("<user>abc<assistant>de")
        assert await b.count_past_tokens([]) == 0
    finally:
        await b.aclose()


async def test_prefill_posts_a_one_token_request() -> None:
    def api(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["stream"] is False and body["max_tokens"] == 1
        assert body["messages"][0] == {"role": "system", "content": "s"}
        return httpx.Response(200, json={"timings": {"prompt_n": 262}})

    b = make(healthy(api))
    await b.start(MODEL, "Q6_K", 3)
    try:
        assert await b.prefill([Msg("system", "s")]) == 262
    finally:
        await b.aclose()
