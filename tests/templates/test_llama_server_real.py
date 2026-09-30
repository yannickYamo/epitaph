"""The real backend against a real llama-server (BUILD_PLAN 10.1: template and cache-reuse rows).

Needs the laptop llama.cpp build and a small model; run under the laptop lock:
  tools/laptop_lock.sh run A 15 -- make test-model
or  PYTHONPATH=src .venv/bin/python -m pytest -m model tests/templates
Model: EPITAPH_TEST_MODEL (default llama-3.2-1b-instruct:Q4_K_M) in ~/epitaph-models.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import pytest

from epitaph.backend.base import CreatureDied
from epitaph.backend.llama_server import LlamaServerBackend, ServerSettings
from epitaph.config import load_config
from epitaph.types import CreatureStatus, Msg, Sampling

pytestmark = pytest.mark.model

NAME, QUANT = os.environ.get("EPITAPH_TEST_MODEL", "llama-3.2-1b-instruct:Q4_K_M").split(":")
MODELS_DIR = Path(os.environ.get("EPITAPH_MODELS_DIR", "~/epitaph-models")).expanduser()
BIN = Path("~/llama.cpp/build/bin/llama-server").expanduser()
SAMPLING = Sampling(temperature=0.7, min_p=0.05, seed=1)
MARKER = "[host] earlier memory lost"

if not (MODELS_DIR / NAME / f"{QUANT}.gguf").exists() or not BIN.exists():
    pytest.skip("model or llama-server missing", allow_module_level=True)


@pytest.fixture
async def backend():
    cfg = load_config("pi4/default", "dev", validate=False)
    s = ServerSettings(bin=str(BIN), models_dir=str(MODELS_DIR), port=8097, ctx=2048,
                       cache_reuse=256, log_path=str(MODELS_DIR / "test-server.log"))  # fmt: skip
    b = LlamaServerBackend(s)
    await b.start(cfg.models[NAME], QUANT, 4)
    yield b
    await b.aclose()


def system() -> str:
    cfg = load_config("pi4/default", "dev", validate=False)
    return "\n\n".join([*cfg.get("prompt.persona_groups"), cfg.get("prompt.mechanics")])


def reading(i: int) -> str:
    return f"[host] t+{i:02d}:00 · health: nominal · memory 1280 tokens · precision 6-bit"


async def thought(b: LlamaServerBackend, msgs: list[Msg], n: int = 40) -> tuple[str, int, int]:
    text, prompt_n, predicted = "", 0, 0
    async for c in b.chat(msgs, SAMPLING, n):
        text += c.text
        if c.done:
            prompt_n, predicted = c.prompt_n or 0, c.predicted_n or 0
    return text, prompt_n, predicted


async def test_stream_timings_and_count(backend: LlamaServerBackend) -> None:
    msgs = [Msg("system", system()), Msg("user", reading(0))]
    text, prompt_n, predicted = await thought(backend, msgs)
    assert text.strip() and 0 < predicted <= 40
    assert "<think>" not in text and "[host]" not in text
    past = [Msg("user", reading(0)), Msg("assistant", text)]
    n = await backend.count_past_tokens(past)
    content = len(text) // 6
    assert content < n < len(text) + 200
    _, prompt_n2, _ = await thought(backend, [msgs[0], *past, Msg("user", reading(1))], 5)
    assert prompt_n2 < 0.3 * (prompt_n + n)  # the prefix cache holds


async def test_cache_reuse_after_front_trim_and_erosion(backend: LlamaServerBackend) -> None:
    """S2f as a regression test: warm trims and erosion re-read at most 25% of the prompt."""
    sys_full = system()
    turns: list[tuple[str, str]] = []
    for i in range(10):
        msgs = [Msg("system", sys_full)]
        for r, t in turns:
            msgs += [Msg("user", r), Msg("assistant", t)]
        msgs.append(Msg("user", reading(i)))
        text, _, _ = await thought(backend, msgs, 60)
        turns.append((reading(i), text))
    total = await backend.count_past_tokens(
        [m for r, t in turns for m in (Msg("user", r), Msg("assistant", t))]
    )
    # a plain front trim (the marker is already there: it rides on the oldest reading)
    del turns[:2]

    def build(sys_text: str, nxt: str) -> list[Msg]:
        msgs = [Msg("system", sys_text)]
        for r, t in turns:
            msgs += [Msg("user", r), Msg("assistant", t)]
        return [*msgs, Msg("user", nxt)]

    _, trim_n, _ = await thought(backend, build(sys_full, reading(20)), 5)
    assert trim_n <= 0.25 * total
    eroded = sys_full.replace(
        load_config("pi4/default", "dev", validate=False).get("prompt.persona_groups")[4], ""
    )
    _, erosion_n, _ = await thought(backend, build(eroded, reading(21)), 5)
    assert erosion_n <= 0.25 * total


async def test_kill_fires_on_death_and_breaks_the_stream(backend: LlamaServerBackend) -> None:
    seen: list[CreatureStatus] = []
    backend.on_death(seen.append)
    pid = backend.status().pid
    assert pid is not None
    msgs = [Msg("system", system()), Msg("user", reading(0))]
    killed = False
    with pytest.raises(CreatureDied):
        async for _ in backend.chat(msgs, SAMPLING, 200):
            if not killed:
                os.kill(pid, signal.SIGKILL)
                killed = True
    for _ in range(50):
        if seen:
            break
        await asyncio.sleep(0.05)
    assert seen and seen[0].signal == signal.SIGKILL
