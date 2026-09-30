"""The fake creature: speeds, prompt cache, cache reuse and every fault hook (BUILD_PLAN 9 A4)."""

from __future__ import annotations

import asyncio

import pytest

from epitaph.backend.base import CreatureDied
from epitaph.backend.errors import ContextFull
from epitaph.backend.fake import SIGKILL, SIGSEGV, FakeBackend, FakeFaults
from epitaph.clock import FakeClock
from epitaph.costmodel import Costs
from epitaph.types import Chunk, CreatureStatus, ModelSpec, Msg, Sampling

MODEL = ModelSpec("m", "repo", "MIT", ("Q6_K", "Q4_K_M", "Q2_K"))
SAMPLING = Sampling(temperature=0.7, min_p=0.05)


def costs() -> Costs:
    return Costs(
        tg_tok_s={"0-3": 2.0, "1-2": 2.0, "2-2": 2.0},
        pp_tok_s={"0-3": 10.0, "1-2": 10.0, "2-2": 10.0},
        load_s=[60.0, 45.0, 30.0],
    )


def make(**kw: object) -> tuple[FakeBackend, FakeClock]:
    clock = FakeClock()
    return FakeBackend(clock, costs(), **kw), clock  # type: ignore[arg-type]


async def run(b: FakeBackend, msgs: list[Msg], max_tokens: int = 40) -> list[Chunk]:
    return [c async for c in b.chat(msgs, SAMPLING, max_tokens)]


def turn(i: int, n_letters: int = 400) -> list[Msg]:
    return [
        Msg("user", f"[host] reading {i} " + "r" * 40, turn=i, kind="reading"),
        Msg("assistant", f"thought {i} " + "x" * n_letters, turn=i),
    ]


SYSTEM = Msg("system", "You are a small language model. " * 20, kind="persona")


async def test_load_time_speed_and_timings() -> None:
    b, clock = make()
    await b.start(MODEL, "Q4_K_M", 2)
    assert clock.now() == pytest.approx(45.0)
    chunks = await run(b, [SYSTEM, Msg("user", "[host] t+00:00")], max_tokens=20)
    last = chunks[-1]
    assert last.done and last.predicted_n == 20
    assert last.prompt_n == await b.count_past_tokens([SYSTEM, Msg("user", "[host] t+00:00")])
    # 20 tokens at 2 tok/s after the prompt at 10 tok/s
    assert clock.now() == pytest.approx(45.0 + last.prompt_n / 10 + 10.0)
    assert b.status().alive and b.status().pid is not None


async def test_prefix_cache_reads_only_new_turns() -> None:
    b, _ = make(cache_reuse=False)
    await b.start(MODEL, "Q6_K", 3)
    msgs = [SYSTEM, *turn(1), *turn(2), Msg("user", "[host] now")]
    await run(b, msgs)
    first = b.requests[-1].prompt_n
    msgs2 = [*msgs[:-1], Msg("user", "[host] now"), Msg("assistant", "a reply"), Msg("user", "x")]
    await run(b, msgs2)
    assert b.requests[-1].prompt_n < first / 4


@pytest.mark.parametrize("reuse", [True, False])
async def test_front_trim_with_and_without_cache_reuse(reuse: bool) -> None:
    b, _ = make(cache_reuse=reuse, cache_reuse_min=64)
    await b.start(MODEL, "Q6_K", 3)
    history = [m for i in range(1, 6) for m in turn(i)]
    await run(b, [SYSTEM, *history, Msg("user", "[host] a")])
    # the oldest turn is forgotten: a front trim after the system prompt
    trimmed = [SYSTEM, *history[2:], Msg("user", "[host] a")]
    total = sum(len(m.content) // 4 + 4 for m in trimmed)
    await run(b, trimmed)
    processed = b.requests[-1].prompt_n
    if reuse:
        assert processed <= 0.25 * total  # the S2f go criterion
    else:
        assert processed >= 0.7 * total


async def test_reload_empties_the_cache() -> None:
    b, _ = make()
    await b.start(MODEL, "Q6_K", 3)
    msgs = [SYSTEM, Msg("user", "[host] a")]
    await run(b, msgs)
    await b.start(MODEL, "Q4_K_M", 2)
    await run(b, msgs)
    assert b.requests[-1].prompt_n == b.requests[0].prompt_n


async def test_oom_at_token_fires_on_death_and_breaks_the_stream() -> None:
    b, _ = make(faults=FakeFaults(oom_at_token=5))
    seen: list[CreatureStatus] = []
    b.on_death(seen.append)
    await b.start(MODEL, "Q6_K", 3)
    got: list[Chunk] = []
    with pytest.raises(CreatureDied) as e:
        async for c in b.chat([Msg("user", "hi")], SAMPLING, 40):
            got.append(c)
    assert len(got) == 5
    assert e.value.status.signal == SIGKILL
    assert seen and seen[0].signal == SIGKILL and not b.status().alive


async def test_oom_at_clock_time_during_prompt_processing() -> None:
    b, clock = make(faults=FakeFaults(oom_at_s=100.0))
    await b.start(MODEL, "Q6_K", 3)  # t = 60
    long = Msg("user", "y" * 4000)  # about 1000 tokens: 100 s of prompt processing
    with pytest.raises(CreatureDied):
        await run(b, [long])
    assert 100.0 <= clock.now() <= 106.0


async def test_crash_signal_and_exit_code() -> None:
    b, _ = make(faults=FakeFaults(crash_at_token=2))
    await b.start(MODEL, "Q6_K", 3)
    with pytest.raises(CreatureDied) as e:
        await run(b, [Msg("user", "hi")])
    assert e.value.status.signal == SIGSEGV
    b2, _ = make(faults=FakeFaults(crash_at_token=2, crash_exit_code=134, crash_signal=None))
    await b2.start(MODEL, "Q6_K", 3)
    with pytest.raises(CreatureDied) as e2:
        await run(b2, [Msg("user", "hi")])
    assert e2.value.status.exit_code == 134 and e2.value.status.signal is None


async def test_death_between_requests_fires_on_death() -> None:
    b, _ = make()
    seen: list[CreatureStatus] = []
    b.on_death(seen.append)
    await b.start(MODEL, "Q6_K", 3)
    b.oom()
    assert len(seen) == 1
    with pytest.raises(CreatureDied):
        await run(b, [Msg("user", "hi")])


async def test_stop_is_not_a_death() -> None:
    b, _ = make()
    seen: list[CreatureStatus] = []
    b.on_death(seen.append)
    await b.start(MODEL, "Q6_K", 3)
    await b.stop()
    assert not seen and not b.status().alive


async def test_hang_stays_alive_until_killed() -> None:
    b, clock = make(faults=FakeFaults(hang_at_token=3))
    await b.start(MODEL, "Q6_K", 3)
    got: list[Chunk] = []

    async def consume() -> None:
        async for c in b.chat([Msg("user", "hi")], SAMPLING, 40):
            got.append(c)

    task = asyncio.create_task(consume())
    for _ in range(20):
        await asyncio.sleep(0)
    assert b.hung and b.status().alive and len(got) == 3
    t_hung = clock.now()
    await clock.sleep(130)  # the controller's token-gap watchdog
    assert len(got) == 3  # no progress while hung
    b.kill(SIGKILL)
    with pytest.raises(CreatureDied):
        await task
    assert clock.now() >= t_hung + 130


async def test_hang_resumes() -> None:
    b, _ = make(faults=FakeFaults(hang_at_token=3))
    await b.start(MODEL, "Q6_K", 3)
    task = asyncio.create_task(run(b, [Msg("user", "hi")], 10))
    for _ in range(20):
        await asyncio.sleep(0)
    assert b.hung
    b.resume()
    chunks = await task
    assert chunks[-1].done and chunks[-1].predicted_n == 10


async def test_hang_during_load_then_killed() -> None:
    b, _ = make(faults=FakeFaults(hang_on_start=True))
    task = asyncio.create_task(b.start(MODEL, "Q6_K", 3))
    for _ in range(10):
        await asyncio.sleep(0)
    assert not task.done() and b.hung
    b.kill(SIGKILL)
    with pytest.raises(CreatureDied):
        await task


async def test_crash_on_start() -> None:
    b, _ = make(faults=FakeFaults(crash_on_start=True))
    with pytest.raises(CreatureDied):
        await b.start(MODEL, "Q6_K", 3)


async def test_slow_reload_and_slow_prompt() -> None:
    b, clock = make(faults=FakeFaults(load_extra_s=200.0, pp_factor=4.0))
    await b.start(MODEL, "Q6_K", 3)
    assert clock.now() == pytest.approx(260.0)
    msg = Msg("user", "z" * 396)  # 99 + 4 = 103 tokens
    chunks = await run(b, [msg], max_tokens=1)
    assert chunks[-1].prompt_per_s == pytest.approx(2.5)
    assert clock.now() == pytest.approx(260.0 + 103 / 2.5 + 0.5)


async def test_full_context() -> None:
    b, _ = make(ctx=200)
    await b.start(MODEL, "Q6_K", 3)
    with pytest.raises(ContextFull):
        await run(b, [Msg("user", "w" * 1000)])
    chunks = await run(b, [Msg("user", "w" * 700)], max_tokens=80)  # 179 tokens: 21 left
    assert chunks[-1].predicted_n == 21 and b.truncated


async def test_cpu_share_slows_generation() -> None:
    b, clock = make()
    await b.start(MODEL, "Q6_K", 3)
    b.set_cpu_share(1.5)
    t0 = clock.now()
    await run(b, [Msg("user", "")], max_tokens=10)
    # 10 tokens at 2 tok/s x (1.5 / 3), plus 4 prompt tokens at 5 tok/s
    assert clock.now() - t0 == pytest.approx(10 / 1.0 + 4 / 5.0)


async def test_legacy_fail_after_tokens() -> None:
    b, _ = make()
    await b.start(MODEL, "Q6_K", 3)
    b.fail_after_tokens = 4
    with pytest.raises(CreatureDied):
        await run(b, [Msg("user", "hi")])


async def test_complete_and_count() -> None:
    b, _ = make()
    await b.start(MODEL, "Q6_K", 3)
    chunks = [c async for c in b.complete("Dear diary", SAMPLING, 5)]
    assert chunks[-1].done
    assert await b.count_past_tokens([Msg("user", "abcd" * 10)]) == 14


async def test_default_reuse_follows_costs() -> None:
    c = costs()
    c.cache_reuse_works = False
    b = FakeBackend(FakeClock(), c)
    assert b.cache_reuse is False


MARKER = "[host] earlier memory lost"


async def _history(b: FakeBackend) -> list[Msg]:
    history = [m for i in range(1, 6) for m in turn(i)]
    await run(b, [SYSTEM, *history, Msg("user", "[host] a")])
    return history


@pytest.mark.parametrize("where", ["oldest", "reading"])
async def test_first_marker_placement(where: str) -> None:
    """S2f: a marker inserted before kept turns stops reuse; appended to the reading it does not."""
    b, _ = make(cache_reuse=True, cache_reuse_min=64)
    await b.start(MODEL, "Q6_K", 3)
    history = await _history(b)
    kept = history[2:]
    if where == "oldest":
        kept = [Msg("user", f"{MARKER}\n{kept[0].content}"), *kept[1:]]
        msgs = [SYSTEM, *kept, Msg("user", "[host] b")]
    else:
        msgs = [SYSTEM, *kept, Msg("user", f"{MARKER}\n[host] b")]
    total = await b.count_past_tokens(msgs)
    await run(b, msgs)
    share = b.requests[-1].prompt_n / total
    assert share > 0.6 if where == "oldest" else share < 0.25


async def test_marker_that_moves_keeps_reuse() -> None:
    b, _ = make(cache_reuse=True, cache_reuse_min=64)
    await b.start(MODEL, "Q6_K", 3)
    history = [m for i in range(1, 7) for m in turn(i)]
    with_marker = [Msg("user", f"{MARKER}\n{history[0].content}"), *history[1:]]
    await run(b, [SYSTEM, *with_marker, Msg("user", "[host] a")])
    kept = history[2:]
    moved = [Msg("user", f"{MARKER}\n{kept[0].content}"), *kept[1:], Msg("user", "[host] b")]
    total = await b.count_past_tokens([SYSTEM, *moved])
    await run(b, [SYSTEM, *moved])
    assert b.requests[-1].prompt_n < 0.25 * total


async def test_erosion_removal_keeps_reuse() -> None:
    b, _ = make(cache_reuse=True, cache_reuse_min=64)
    two = Msg("system", "Group one sentence here.\nGroup two sentence that goes away.")
    await b.start(MODEL, "Q6_K", 3)
    history = [m for i in range(1, 6) for m in turn(i)]
    await run(b, [two, *history, Msg("user", "[host] a")])
    one = Msg("system", "Group one sentence here.")
    msgs = [one, *history, Msg("user", "[host] a"), Msg("user", "[host] b")]
    await run(b, msgs)
    assert b.requests[-1].prompt_n < 0.25 * await b.count_past_tokens(msgs)


async def test_prefill_reads_the_system_prompt_ahead() -> None:
    b, _ = make()
    await b.start(MODEL, "Q6_K", 3)
    n = await b.prefill([SYSTEM])
    assert n == await b.count_past_tokens([SYSTEM])
    await run(b, [SYSTEM, Msg("user", "[host] t+00:00")], 5)
    assert b.requests[-1].prompt_n == await b.count_past_tokens([Msg("user", "[host] t+00:00")])
