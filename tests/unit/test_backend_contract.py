"""The Backend protocol: both backends implement it, `prefill` included (contract A5)."""

from __future__ import annotations

import json

import httpx
import pytest

from epitaph.backend import errors
from epitaph.backend.base import Backend, BackendError, ContextFull, CreatureDied
from epitaph.backend.fake import FakeBackend
from epitaph.backend.llama_server import LlamaServerBackend, ServerSettings
from epitaph.clock import FakeClock, VirtualClock, run_virtual
from epitaph.costmodel import Costs
from epitaph.types import ModelSpec, Msg, Sampling

MODEL = ModelSpec("m", "repo", "MIT", ("Q8_0", "Q4_K_M", "Q2_K"))
SYSTEM = Msg("system", "You are a small language model. " * 20, kind="persona")
READING = Msg("user", "[host] t+00:00 · boot complete", kind="reading")


def costs() -> Costs:
    return Costs(tg_tok_s={"0-3": 2.0}, pp_tok_s={"0-3": 10.0}, load_s=[30.0])


def test_both_backends_satisfy_the_protocol() -> None:
    # Checked statically by pyright as well: each assignment must type-check.
    fake: Backend = FakeBackend(FakeClock(), costs())
    real: Backend = LlamaServerBackend(ServerSettings())
    assert callable(fake.prefill) and callable(real.prefill)


def test_errors_module_re_exports_the_base_types() -> None:
    assert errors.ContextFull is ContextFull
    assert errors.BackendError is BackendError
    e = ContextFull(2100, 2048)
    assert (e.tokens, e.ctx) == (2100, 2048) and "2100" in str(e)


async def test_fake_prefill_makes_the_first_thought_read_only_its_reading() -> None:
    clock = FakeClock()
    b = FakeBackend(clock, costs())
    await b.start(MODEL, "Q8_0", 3)
    t0 = clock.now()
    n = await b.prefill([SYSTEM])
    assert n == await b.count_past_tokens([SYSTEM])
    assert clock.now() - t0 == pytest.approx(n / 10.0)
    assert await b.prefill([SYSTEM]) == 0  # already cached: nothing to read
    chunks = [c async for c in b.chat([SYSTEM, READING], Sampling(0.7, 0.05), 5)]
    assert chunks[-1].prompt_n == await b.count_past_tokens([READING])


async def test_fake_prefill_errors() -> None:
    b = FakeBackend(FakeClock(), costs(), ctx=64)
    with pytest.raises(CreatureDied):
        await b.prefill([SYSTEM])
    await b.start(MODEL, "Q8_0", 3)
    with pytest.raises(ContextFull):
        await b.prefill([SYSTEM])


def test_fake_runs_on_a_virtual_clock() -> None:
    async def main(clock: VirtualClock) -> float:
        b = FakeBackend(clock, costs())
        await b.start(MODEL, "Q8_0", 3)
        await b.prefill([SYSTEM])
        return clock.now()

    t = run_virtual(main)
    assert t > 30.0  # the load, then the system prompt at 10 tokens/s


async def test_llama_prefill_maps_server_errors() -> None:
    def api(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        body = json.loads(req.content)
        assert body["stream"] is False
        return httpx.Response(
            400,
            json={
                "error": {
                    "type": "exceed_context_size_error",
                    "n_prompt_tokens": 3000,
                    "n_ctx": 2048,
                }
            },
        )

    b = LlamaServerBackend(
        ServerSettings(bin="unused"), _SleeperBody(), transport=httpx.MockTransport(api)
    )
    await b.start(MODEL, "Q8_0", 3)
    try:
        with pytest.raises(ContextFull) as e:
            await b.prefill([SYSTEM])
        assert e.value.tokens == 3000
    finally:
        await b.aclose()
    with pytest.raises(CreatureDied):
        await b.prefill([SYSTEM])


class _SleeperBody:
    """Spawns a sleeping Python child in place of llama-server."""

    def wrap_spawn(self, argv: list[str]) -> list[str]:
        import sys

        return [sys.executable, "-c", "import time; time.sleep(60)"]
