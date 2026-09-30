"""Life clocks and the schedule (BUILD_PLAN 5.2, 5.3).

Owned by part B after phase 0a. The life clock is monotonic time since the model finished
loading; it never pauses and never reads the wall clock.

Three kinds of clock:

- `RealClock`: monotonic wall time, for real lives.
- `FakeClock` / `RehearsalClock`: one-task virtual time; `sleep` jumps time forward at once.
  Right for a single sequential loop (the simulator, the cost model, the rehearsal).
- `VirtualClock` on a `VirtualEventLoop` (`run_virtual`): virtual time for several concurrent
  tasks. The loop's own timer queue is the scheduler, so `asyncio.sleep`, `wait_for` and
  timeouts all run in virtual time and overlap correctly (generation while words are typed).
"""

from __future__ import annotations

import asyncio
import selectors
import time
from bisect import bisect_right
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar

from epitaph.config import INTERPOLATED, Config, Profile
from epitaph.types import Health, Knobs

T = TypeVar("T")

# Budgets that change only at a reload: easing them into the reload would blur the loss.
HOLD_UNTIL_RELOAD = ("recall", "cpu_share")


class LifeClock(Protocol):
    """Time since birth, and a way to wait on it."""

    def elapsed(self) -> float: ...

    async def sleep(self, s: float) -> None: ...

    def start(self) -> None: ...

    def charge(self, cost_s: float) -> None:
        """Rehearsal: advance by a Pi cost. A no-op on real time."""
        ...


class RealClock:
    """Monotonic wall time for real lives."""

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def start(self) -> None:
        self._t0 = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    async def sleep(self, s: float) -> None:
        if s > 0:
            await asyncio.sleep(s)

    def charge(self, cost_s: float) -> None:
        """Real time cannot be charged; the cost is paid by actually waiting."""


class FakeClock:
    """Virtual time for tests and the simulator: sleeping advances time instantly."""

    def __init__(self, t: float = 0.0) -> None:
        self._t = t
        self._t0 = t

    def start(self) -> None:
        self._t0 = self._t

    def elapsed(self) -> float:
        return self._t - self._t0

    def now(self) -> float:
        """Absolute virtual time, across lives."""
        return self._t

    def advance(self, s: float) -> None:
        if s < 0:
            raise ValueError("time cannot go backwards")
        self._t += s

    async def sleep(self, s: float) -> None:
        self.advance(max(0.0, s))
        await asyncio.sleep(0)

    def charge(self, cost_s: float) -> None:
        self.advance(max(0.0, cost_s))


class RehearsalClock(FakeClock):
    """Virtual time charged at Pi costs while a real model runs on the laptop (BUILD_PLAN 5.11)."""


# ---------------------------------------------------------------------------------------
# virtual time for concurrent tasks


class VirtualDeadlock(RuntimeError):
    """Every task waits and no timer is pending: nothing can ever happen again."""


class _VirtualSelector(selectors.DefaultSelector):
    """Polls real I/O without blocking and turns the loop's timer wait into a time jump."""

    def __init__(self, loop: VirtualEventLoop, real_wait_s: float) -> None:
        super().__init__()
        self._loop = loop
        self._real_wait_s = real_wait_s

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        ready = super().select(0)
        if ready or timeout == 0:
            return ready
        if timeout is None:
            # No timer is pending. Only another thread (an executor) could still wake the loop.
            ready = super().select(self._real_wait_s)
            if not ready:
                raise VirtualDeadlock("every task is waiting and no timer is pending")
            return ready
        self._loop.jump(timeout)
        return ready


class VirtualEventLoop(asyncio.SelectorEventLoop):
    """An asyncio loop whose clock is virtual: when every task waits, time jumps to the next
    timer. Deterministic, and a 60-minute life runs in milliseconds."""

    def __init__(self, start: float = 0.0, real_wait_s: float = 2.0) -> None:
        self._vt = start
        super().__init__(_VirtualSelector(self, real_wait_s))

    def time(self) -> float:
        return self._vt

    def jump(self, s: float) -> None:
        if s < 0:
            raise ValueError("time cannot go backwards")
        self._vt += s


class VirtualClock:
    """A LifeClock that reads a VirtualEventLoop. Sleeping is `asyncio.sleep` in virtual time,
    so concurrent sleepers overlap as they would on a real clock."""

    def __init__(self, loop: VirtualEventLoop) -> None:
        self.loop = loop
        self._t0 = loop.time()

    def start(self) -> None:
        self._t0 = self.loop.time()

    def elapsed(self) -> float:
        return self.loop.time() - self._t0

    def now(self) -> float:
        """Absolute virtual time, across lives."""
        return self.loop.time()

    async def sleep(self, s: float) -> None:
        await asyncio.sleep(max(0.0, s))

    def charge(self, cost_s: float) -> None:
        """Jump forward by a cost. Timers that fall due in the jump fire late, as on a busy
        machine; prefer `sleep` when other tasks must see the time pass."""
        self.loop.jump(max(0.0, cost_s))

    def advance(self, s: float) -> None:
        self.loop.jump(s)


def run_virtual(main: Callable[[VirtualClock], Awaitable[T]], start: float = 0.0) -> T:
    """Run `main(clock)` to completion on a fresh virtual-time loop."""
    loop = VirtualEventLoop(start)
    clock = VirtualClock(loop)

    async def wrapper() -> T:
        return await main(clock)

    try:
        return loop.run_until_complete(wrapper())
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


class Schedule:
    """Knob values at any moment of a life, from a resolved profile."""

    def __init__(self, profile: Profile, lifespan_s: float | None = None) -> None:
        self.profile = profile if lifespan_s is None else profile.with_lifespan(lifespan_s)
        p = self.profile
        self.lifespan_s = p.lifespan_s
        self.times: list[float] = [kf.at.resolve(p.nominal_s, p.lifespan_s) for kf in p.keyframes]
        self.values: list[dict[str, Any]] = [kf.values for kf in p.keyframes]
        self.death_s: float | None = (
            p.death.resolve(p.nominal_s, p.lifespan_s) if p.death is not None else None
        )

    @classmethod
    def from_profile(cls, cfg: Config, lifespan_s: float | None = None) -> Schedule:
        """The schedule of the configured profile, optionally rescaled (BUILD_PLAN 6.4).
        Fractional keyframes scale with the lifespan; end-anchored ones keep their offset."""
        return cls(cfg.profile, lifespan_s)

    def keyframe_index(self, t_s: float) -> int:
        """Index of the keyframe in force at t (the last one at or before t)."""
        return max(0, bisect_right(self.times, t_s) - 1)

    def next_change(self, t_s: float) -> float | None:
        """The first keyframe time after t where a stepped field changes, if any."""
        return next((c for c in self.change_times() if c > t_s), None)

    def at(self, t_s: float) -> Knobs:
        """Stepped fields hold the last keyframe's value; interpolated fields move linearly."""
        i = max(0, bisect_right(self.times, t_s) - 1)
        cur = self.values[i]
        nxt = self.values[i + 1] if i + 1 < len(self.values) else None
        frac = 0.0
        if nxt is not None:
            span = self.times[i + 1] - self.times[i]
            frac = min(1.0, max(0.0, (t_s - self.times[i]) / span)) if span > 0 else 0.0

        # Recall is cut at a reload, not eased into it: the reload is the life's big loss
        # (BUILD_PLAN 5.4), so it holds until a keyframe that changes step or threads.
        reload_next = nxt is not None and (nxt["step"], nxt["threads"]) != (
            cur["step"],
            cur["threads"],
        )

        def lerp(name: str) -> float:
            a = float(cur[name])
            if nxt is None or (name in HOLD_UNTIL_RELOAD and reload_next):
                return a
            return a + (float(nxt[name]) - a) * frac

        interp = {name: lerp(name) for name in INTERPOLATED}
        return Knobs(
            t=t_s,
            phase=str(cur["phase"]),
            health=Health(str(cur["health"])),
            recall=round(interp["recall"]),
            step=int(cur["step"]),
            threads=int(cur["threads"]),
            cpu_share=round(interp["cpu_share"], 3),
            temperature=round(interp["temperature"], 3),
            min_p=round(interp["min_p"], 4),
            max_tokens=max(1, round(interp["max_tokens"])),
            pause_s=round(interp["pause_s"], 3),
            persona_groups=int(cur["persona_groups"]),
            mechanics=bool(cur["mechanics"]),
            readings=cur["readings"],
            letter_ms=round(interp["letter_ms"], 2),
            jitter=round(interp["jitter"], 4),
            hesitation=round(interp["hesitation"], 4),
            death_squeeze=self.death_s is not None and t_s >= self.death_s,
        )

    def change_times(self) -> list[float]:
        """Times where any stepped field changes (reloads, health, erosion, readings form)."""
        out: list[float] = []
        for i in range(1, len(self.values)):
            a, b = self.values[i - 1], self.values[i]
            if any(a[f] != b[f] for f in ("health", "step", "threads", "persona_groups")):
                out.append(self.times[i])
        return out

    def reload_times(self) -> list[float]:
        """Keyframe times where the ladder step or thread count changes."""
        return [
            self.times[i]
            for i in range(1, len(self.values))
            if (self.values[i]["step"], self.values[i]["threads"])
            != (self.values[i - 1]["step"], self.values[i - 1]["threads"])
        ]

    def erosion_times(self) -> list[float]:
        return [
            self.times[i]
            for i in range(1, len(self.values))
            if self.values[i]["persona_groups"] != self.values[i - 1]["persona_groups"]
        ]

    def health_times(self) -> list[float]:
        return [
            self.times[i]
            for i in range(1, len(self.values))
            if self.values[i]["health"] != self.values[i - 1]["health"]
        ]
