"""B1: clocks, the virtual-time loop, and schedule boundaries at every keyframe."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from epitaph.clock import (
    HOLD_UNTIL_RELOAD,
    FakeClock,
    RealClock,
    RehearsalClock,
    Schedule,
    VirtualDeadlock,
    VirtualEventLoop,
    run_virtual,
)
from epitaph.config import INTERPOLATED, STEPPED, ConfigError, load_config
from tests.conftest import v6_config

EPS = 1e-6

PROFILES = [
    ("pi4/default", "pi4-4gb"),
    ("pi4/default-qwen3-1.7b", "pi4-4gb"),
    ("pi4/smoke-300", "pi4-4gb"),
    ("pi4/skeleton-1200", "pi4-4gb"),
    ("pi4/unbounded", "pi4-4gb"),
    ("pi5/default", "pi5-8gb"),
    ("pi5/skeleton-600", "pi5-8gb"),
    ("pi5/unbounded", "pi5-8gb"),
    ("sim", "pi4-4gb"),
]

# Lifespans for the rescale checks; ones a profile cannot hold are skipped (config rejects).
RESCALES = [None, 20 * 60, 30 * 60, 45 * 60, 75 * 60, 120 * 60]


def _schedules() -> list[tuple[str, float | None, Schedule]]:
    out: list[tuple[str, float | None, Schedule]] = []
    for name, hw in PROFILES:
        for life in RESCALES:
            try:
                cfg = load_config(name, hw, lifespan_s=life)
            except ConfigError:
                continue
            out.append((name, life, Schedule.from_profile(cfg)))
    return out


SCHEDULES = _schedules()


def test_every_profile_has_at_least_its_nominal_schedule() -> None:
    names = {n for n, life, _ in SCHEDULES if life is None}
    assert names == {n for n, _ in PROFILES}
    # and the Pi 4 default rescales to a 45-minute and a 75-minute life
    assert {life for n, life, _ in SCHEDULES if n == "pi4/default"} >= {45 * 60, 75 * 60}


@pytest.mark.parametrize(
    ("name", "life", "s"), SCHEDULES, ids=[f"{n}@{life}" for n, life, _ in SCHEDULES]
)
def test_boundaries_at_every_keyframe(name: str, life: float | None, s: Schedule) -> None:
    p = s.profile
    assert s.times[0] == 0
    assert s.times == sorted(s.times)
    for i, (kf, t) in enumerate(zip(p.keyframes, s.times, strict=True)):
        # keyframe times: fractions scale, end anchors keep their offset
        if kf.at.from_end:
            assert t == pytest.approx(s.lifespan_s - kf.at.seconds)
        else:
            assert t == pytest.approx(kf.at.seconds * s.lifespan_s / p.nominal_s)
        at = s.at(t)
        for f in STEPPED:
            assert getattr(at, f) == kf.values[f], (name, life, i, f)
        for f in INTERPOLATED:
            got = getattr(at, f)
            want = float(kf.values[f])
            assert got == pytest.approx(want, abs=0.51), (name, life, i, f)
        assert s.keyframe_index(t) == i
        if i == 0:
            continue
        prev = p.keyframes[i - 1].values
        before = s.at(t - EPS)
        for f in STEPPED:
            assert getattr(before, f) == prev[f], (name, life, i, f, "just before")
        reload = (kf.values["step"], kf.values["threads"]) != (prev["step"], prev["threads"])
        for f in INTERPOLATED:
            got = float(getattr(before, f))
            if reload and f in HOLD_UNTIL_RELOAD:
                # recall and CPU share are cut at a reload, never eased into it
                assert got == pytest.approx(float(prev[f]), abs=0.51), (name, life, i, f)
            else:
                assert got == pytest.approx(float(kf.values[f]), abs=0.51), (name, life, i, f)


@pytest.mark.parametrize(
    ("name", "life", "s"), SCHEDULES, ids=[f"{n}@{life}" for n, life, _ in SCHEDULES]
)
def test_death_squeeze_and_change_times(name: str, life: float | None, s: Schedule) -> None:
    if s.death_s is None:
        assert not s.at(s.lifespan_s).death_squeeze
    else:
        assert not s.at(s.death_s - EPS).death_squeeze
        assert s.at(s.death_s).death_squeeze
        assert s.death_s < s.lifespan_s
    for t in s.reload_times():
        a, b = s.at(t - EPS), s.at(t)
        assert (a.step, a.threads) != (b.step, b.threads)
    for t in s.erosion_times():
        assert s.at(t - EPS).persona_groups > s.at(t).persona_groups
    for t in s.health_times():
        assert s.at(t - EPS).health != s.at(t).health
    assert set(s.reload_times()) | set(s.erosion_times()) | set(s.health_times()) <= set(
        s.change_times()
    )
    changes = s.change_times()
    if changes:
        assert s.next_change(0) == changes[0]
        assert s.next_change(changes[-1]) is None


def test_rescale_moves_fractional_keyframes_only() -> None:
    # the v6 reference schedule (V6_REFERENCE), whose keyframes these numbers describe
    base = Schedule.from_profile(v6_config())
    cfg = v6_config()
    short = Schedule.from_profile(cfg, 45 * 60)
    assert short.lifespan_s == 2700
    # 12:00 of 60 becomes 9:00 of 45; end-17:00 stays 17 minutes before the end
    assert 9 * 60 in short.times
    assert short.reload_times() == [21 * 60, 2700 - 17.5 * 60]
    assert short.erosion_times() == [2700 - x * 60 for x in (11, 9, 7, 5, 3)]
    assert short.death_s == 2700 - 30
    assert base.erosion_times()[0] - base.reload_times()[1] == (
        short.erosion_times()[0] - short.reload_times()[1]
    )


def test_rescale_that_breaks_the_order_is_rejected() -> None:
    with pytest.raises(ConfigError, match="not after the previous"):
        v6_config(lifespan_s=15 * 60)


def test_values_past_the_end_hold_the_last_keyframe() -> None:
    s = Schedule.from_profile(v6_config())
    k = s.at(10_000)
    assert (k.persona_groups, k.mechanics, k.recall, k.readings) == (0, False, 48, "minimal")
    assert s.at(-5).recall == 1280


# ---------------------------------------------------------------------------------------
# clocks


def test_real_clock_is_monotonic_and_charge_is_a_noop() -> None:
    c = RealClock()
    c.start()
    a = c.elapsed()
    c.charge(1000)
    assert 0 <= a <= c.elapsed() < 5
    asyncio.run(c.sleep(0))
    asyncio.run(c.sleep(0.001))


def test_fake_and_rehearsal_clocks_charge() -> None:
    c = FakeClock(10)
    c.start()
    c.charge(3)
    c.charge(-1)
    assert c.elapsed() == 3 and c.now() == 13
    with pytest.raises(ValueError):
        c.advance(-1)
    r = RehearsalClock()
    r.charge(2.5)
    asyncio.run(r.sleep(1))
    assert r.elapsed() == 3.5


def test_virtual_loop_overlaps_concurrent_sleepers() -> None:
    async def main(clock):  # type: ignore[no-untyped-def]
        log: list[tuple[str, float]] = []

        async def a() -> None:
            for _ in range(3):
                await asyncio.sleep(1.5)
                log.append(("a", clock.elapsed()))

        async def b() -> None:
            for _ in range(3):
                await clock.sleep(1)
                log.append(("b", clock.elapsed()))

        await asyncio.gather(a(), b())
        return log

    t0 = time.monotonic()
    log = run_virtual(main)
    assert time.monotonic() - t0 < 1
    assert log == [("b", 1), ("a", 1.5), ("b", 2), ("a", 3), ("b", 3), ("a", 4.5)]


def test_virtual_loop_timeouts_and_charge() -> None:
    async def main(clock):  # type: ignore[no-untyped-def]
        clock.start()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.Event().wait(), 3600)
        at_timeout = clock.elapsed()
        clock.charge(10)
        clock.advance(5)
        await clock.sleep(-1)
        return at_timeout, clock.elapsed(), clock.now()

    at_timeout, elapsed, now = run_virtual(main, start=100)
    assert at_timeout == pytest.approx(3600)
    assert elapsed == pytest.approx(3615)
    assert now == pytest.approx(3715)


def test_virtual_loop_detects_deadlock() -> None:
    loop = VirtualEventLoop(real_wait_s=0.01)
    try:
        with pytest.raises(VirtualDeadlock):
            loop.run_until_complete(asyncio.Event().wait())
        with pytest.raises(ValueError):
            loop.jump(-1)
    finally:
        loop.close()


def test_virtual_loop_still_hears_other_threads() -> None:
    async def main(clock):  # type: ignore[no-untyped-def]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[int] = loop.create_future()
        threading.Thread(
            target=lambda: loop.call_soon_threadsafe(fut.set_result, 7), daemon=True
        ).start()
        return await fut

    assert run_virtual(main) == 7
