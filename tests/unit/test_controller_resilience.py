"""The controller keeps going, unattended (gate review): a stuck loop loses its watchdog pings,
the supervisor survives its own errors, a broken sink or a full card never raises into the
loop, a life whose setup fails is closed as a crash, blocking body calls leave the event loop.

Fakes and virtual time only: no network, no real time.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from epitaph import controller as controller_mod
from epitaph.backend.base import CreatureDied
from epitaph.backend.fake import FakeBackend
from epitaph.body.fake import FakeBody
from epitaph.clock import Schedule, VirtualClock, run_virtual
from epitaph.config import Config
from epitaph.controller import (
    PING_HISTORY,
    STALL_GRACE_S,
    STALL_MARGIN_S,
    Controller,
    GuardedBackend,
    Life,
)
from epitaph.costmodel import load_costs
from epitaph.events import Event
from epitaph.pacing import Spoken
from epitaph.sim import FakeSlots, make_controller
from epitaph.state import life_dir
from epitaph.transcript import Transcript
from epitaph.types import ModelSpec
from tests.unit.test_controller import BACKGROUND, DEFAULT, SMOKE, Setup, cfg_of, death, of, run


def run_until(
    cfg: Config, until_s: float, *, lives: int = 1, setup: Setup | None = None
) -> tuple[Controller, list[Event], list[str]]:
    """Run the controller, then stop it after `until_s` virtual seconds (a wedged loop never
    ends on its own). Returns it, its events and what it told systemd."""
    events: list[Event] = []
    notes: list[str] = []

    async def main(clock: VirtualClock) -> Controller:
        ctl = make_controller(cfg, clock, lives=lives, publish=events)
        ctl.notify = notes.append
        if setup is not None:
            await setup(ctl, clock)
        task = asyncio.ensure_future(ctl.run())

        async def stop() -> None:
            await asyncio.sleep(until_s)
            task.cancel()

        BACKGROUND.append(asyncio.ensure_future(stop()))
        with pytest.raises(asyncio.CancelledError):
            await task
        return ctl

    return run_virtual(main), events, notes


def budget(cfg: Config) -> float:
    """The stall budget outside a request (a load's worth plus the margin)."""
    load = float(cfg.get("life.load_timeout_s"))
    gap = float(cfg.get("body.token_gap_timeout_s"))
    return max(load, gap) + STALL_MARGIN_S


# -- the watchdog follows the life loop ---------------------------------------------------


def test_a_stuck_loop_kills_its_creature_then_loses_its_pings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: pings came from a separate task, so a life loop stuck on an await that
    no guard covers went on pinging forever. Now the creature is killed (hang) once the
    state's budget is overrun, and if the loop still does not move the pings stop."""
    stuck_at: list[float] = []

    class Stuck(Life):
        async def thought(self, t: float) -> Spoken:
            if self.turn >= 1:
                stuck_at.append(asyncio.get_running_loop().time())
                await asyncio.Event().wait()  # never set, never guarded
            return await super().thought(t)

    monkeypatch.setattr(controller_mod, "Life", Stuck)
    cfg = cfg_of(DEFAULT)
    ctl, ev, notes = run_until(cfg, 1500.0)
    d = death(ev)
    assert d["cause"] == "hang"
    start = of(ev, "birth_loading")[0]["ts"]  # loop time 0
    killed = d["ts"] - start
    assert killed - stuck_at[0] == pytest.approx(budget(cfg), abs=ctl.watchdog_s + 0.1)
    assert ctl.wedged and not of(ev, "death_shown")  # the loop never got past the stall
    last = ctl.ping_times[-1]
    assert killed + STALL_GRACE_S - ctl.watchdog_s - 0.1 <= last <= killed + STALL_GRACE_S + 0.1
    gaps = [b - a for a, b in itertools.pairwise(ctl.ping_times)]
    assert max(gaps) <= ctl.watchdog_s + 1e-6  # pinged on time until then
    assert notes.count("WATCHDOG=1") == ctl.pings


def test_a_loop_freed_by_the_stall_kill_keeps_its_pings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kill comes first: a loop waiting on the creature moves on, dies of a hang, and
    the controller goes on pinging through the silence."""

    class WaitsOnTheCreature(Life):
        async def thought(self, t: float) -> Spoken:
            if self.turn >= 1:
                guard = self.backend
                assert isinstance(guard, GuardedBackend)
                await guard.died.wait()  # not raced: only the kill frees it
                raise CreatureDied(guard.status())
            return await super().thought(t)

    monkeypatch.setattr(controller_mod, "Life", WaitsOnTheCreature)
    ctl, ev = run(cfg_of(DEFAULT))
    assert death(ev)["cause"] == "hang" and of(ev, "death_shown") and of(ev, "silence")
    assert not ctl.wedged
    assert max(b - a for a, b in itertools.pairwise(ctl.ping_times)) <= ctl.watchdog_s + 1e-6


def test_long_but_moving_states_are_not_stalls() -> None:
    """A slow load that progresses (the hang watch's limit, from the last progress) and a
    long silence (its own length) keep their pings."""
    cfg = cfg_of(SMOKE, life={"silence_seconds": 900})
    costs = load_costs(cfg)
    costs.load_s[0] = 900.0  # three times load_timeout_s, but the counters move
    _ctl, ev = run(cfg, lives=2, costs=costs)
    assert [death(ev, n)["cause"] for n in (1, 2)] == ["deadline", "deadline"]
    assert not [e for e in ev if e["type"] == "death" and e["cause"] == "hang"]


def test_ping_history_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: every ping time was kept for ever (BUILD_PLAN 11: memory under 20 MB)."""
    monkeypatch.setattr(controller_mod, "PING_HISTORY", 20)
    ctl, _ = run(cfg_of(SMOKE))
    assert ctl.pings > 20 and len(ctl.ping_times) == 20
    assert PING_HISTORY >= 2 * 30 / 5  # still covers a few systemd intervals


# -- the supervisor survives -------------------------------------------------------------


def test_a_failing_supervisor_pass_does_not_stop_the_pings() -> None:
    calls = [0]

    async def setup(ctl: Controller, clock: VirtualClock) -> None:
        check = ctl._check  # pyright: ignore[reportPrivateUsage]

        def flaky(cur: Any) -> float:
            calls[0] += 1
            if 10 <= calls[0] < 40:
                raise RuntimeError("a bug in a pass")
            return check(cur)

        ctl._check = flaky  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]

    ctl, ev = run(cfg_of(SMOKE), setup=setup)
    assert calls[0] > 40 and death(ev)["cause"] == "deadline"
    assert max(b - a for a, b in itertools.pairwise(ctl.ping_times)) <= ctl.watchdog_s + 1e-6


def test_a_supervisor_that_exits_fails_the_run_loudly() -> None:
    """Regression: the supervisor could end (an error) and the run went on without pings."""

    async def setup(ctl: Controller, clock: VirtualClock) -> None:
        async def gone() -> None:
            await asyncio.sleep(30)
            raise RuntimeError("supervisor bug")

        ctl._supervise = gone  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(RuntimeError, match="supervisor exited") as info:
        run(cfg_of(SMOKE), setup=setup)
    assert "supervisor bug" in repr(info.value.__cause__)


# -- events never raise into the loop ------------------------------------------------------


def test_a_broken_subscriber_and_a_broken_transcript_do_not_end_the_life(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an exception in Transcript.write or in publish (LifeView.handle) went up
    into the life loop."""

    def broken_write(self: Transcript, e: dict[str, Any]) -> None:
        if e["type"] == "word":
            raise ValueError("not serialisable")

    monkeypatch.setattr(Transcript, "_write", broken_write)
    seen: list[str] = []

    async def setup(ctl: Controller, clock: VirtualClock) -> None:
        def publish(e: Event) -> None:
            seen.append(e["type"])
            if e["type"] == "vitals":
                raise RuntimeError("display mirror bug")

        ctl.publish = publish

    ctl, _ = run(cfg_of(SMOKE), lives=2, state_dir=tmp_path, setup=setup)
    assert [r.cause for r in ctl.records] == ["deadline", "deadline"]
    assert seen.count("death_shown") == 2
    assert (life_dir(tmp_path, 2) / "death.json").exists()


def test_a_full_card_does_not_end_the_life(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from epitaph import transcript

    def enospc(path: Path, text: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(transcript, "_append", enospc)
    ctl, ev = run(cfg_of(SMOKE), state_dir=tmp_path)
    assert ctl.records[0].cause == "deadline" and of(ev, "death_shown")


def test_the_death_comes_after_the_last_reading_in_thoughts_txt(tmp_path: Path) -> None:
    """Regression (first Pi life): the deadline cut a thought, and its reading was written
    after the death line."""
    _ctl, ev = run(cfg_of(SMOKE), state_dir=tmp_path)
    assert [e for e in of(ev, "vitals") if e["t"] < death(ev)["t"]]
    text = (life_dir(tmp_path, 1) / "thoughts.txt").read_text()
    assert text.rstrip().splitlines()[-1].startswith("t+05:00  -- death")
    assert text.rindex("[host]") < text.index("-- death")


# -- one life's setup failing --------------------------------------------------------------


def test_a_life_whose_setup_fails_is_closed_and_the_next_one_lives(tmp_path: Path) -> None:
    """Regression: an error in costs_for, the transcript or the body before the birth ended
    the controller. Now that life is closed as a crash and the next one comes after the
    silence."""

    async def setup(ctl: Controller, clock: VirtualClock) -> None:
        costs_for = ctl.costs_for

        def first_fails(model: ModelSpec) -> Any:
            if ctl.life_counter is not None and ctl.life_counter.current() == 1:
                raise FileNotFoundError("bench/missing.json")
            return costs_for(model)

        ctl.costs_for = first_fails

    ctl, ev = run(cfg_of(SMOKE), lives=2, state_dir=tmp_path, setup=setup)
    assert [r.cause for r in ctl.records] == ["crash", "deadline"]
    assert death(ev, 1)["cause"] == "crash" and of(ev, "silence", 1)
    record = json.loads((life_dir(tmp_path, 1) / "death.json").read_text())
    assert record["cause"] == "crash" and "bench/missing.json" in record["setup_error"]
    assert of(ev, "error", 1)[0]["where"] == "controller"
    shown, born = of(ev, "death_shown", 1)[0], of(ev, "birth_loading", 2)[0]
    assert born["ts"] - shown["ts"] >= float(cfg_of(SMOKE).get("life.silence_seconds")) - 1


def test_a_transcript_that_cannot_open_is_a_setup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = [0]

    def open_once(self: Transcript, meta: dict[str, Any]) -> None:
        opened[0] += 1
        if opened[0] == 1:
            raise OSError(5, "Input/output error")
        self.dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Transcript, "open", open_once)
    ctl, _ = run(cfg_of(SMOKE), lives=2, state_dir=tmp_path)
    assert [r.cause for r in ctl.records] == ["crash", "deadline"]


# -- the echo's slot calls are bounded -----------------------------------------------------


def test_a_slot_save_that_never_answers_costs_only_the_echo() -> None:
    """Regression: the echo's slot save and restore were awaited without a bound."""

    async def setup(ctl: Controller, clock: VirtualClock) -> None:
        async def never(name: str) -> bool:
            await asyncio.Event().wait()
            return True

        def slots_for(b: Any) -> FakeSlots:
            s = FakeSlots(b)
            s.save = never  # type: ignore[method-assign]
            return s

        ctl.slots_for = slots_for

    ctl, ev = run(cfg_of(DEFAULT), setup=setup)
    assert len(of(ev, "reload_done")) == 2 and death(ev)["cause"] == "oom"
    assert not [e for e in of(ev, "vitals") if "your words now:" in e["reading"]]
    for r, done in zip(of(ev, "reload"), of(ev, "reload_done"), strict=True):
        assert done["t"] - r["t"] >= ctl.slot_timeout_s  # given up, then the life went on


def test_a_slot_restore_that_never_answers_is_given_up() -> None:
    async def setup(ctl: Controller, clock: VirtualClock) -> None:
        async def never(name: str) -> None:
            await asyncio.Event().wait()

        def slots_for(b: Any) -> FakeSlots:
            s = FakeSlots(b)
            s.restore = never  # type: ignore[method-assign]
            return s

        ctl.slots_for = slots_for

    _ctl, ev = run(cfg_of(DEFAULT), setup=setup)
    assert len(of(ev, "reload_done")) == 2 and death(ev)["cause"] == "oom"


# -- a reload never revives a dead life ----------------------------------------------------


def test_reload_on_a_dead_life_stops_where_it_is() -> None:
    """Regression: a death declared during the load (no guard, as in the rehearsal) was
    followed by reload_done and state=living."""
    cfg = cfg_of(DEFAULT)
    r1 = Schedule(cfg.profile).reload_times()[0]

    async def main(clock: VirtualClock) -> tuple[list[Event], Life]:
        fake = FakeBackend(clock, load_costs(cfg))
        events: list[Event] = []
        life = Life(cfg, clock, fake, FakeBody(), events.append)
        await life.birth(life.sch.at(0))
        start = fake.start

        async def dying_start(model: ModelSpec, quant: str, threads: int) -> None:
            await start(model, quant, threads)
            life.declare_death("manual")

        fake.start = dying_start  # type: ignore[method-assign]
        await life.reload(life.sch.at(r1), r1)
        await life.reload(life.sch.at(r1), r1)  # dead: nothing at all
        return events, life

    events, life = run_virtual(main)
    assert life.state == "dead" and life.dead == "manual"
    assert len(of(events, "reload")) == 1 and not of(events, "reload_done")


# -- blocking body calls leave the event loop; systemd hears the stop -----------------------


def test_blocking_body_calls_go_through_offload() -> None:
    ran: list[str] = []

    async def setup(ctl: Controller, clock: VirtualClock) -> None:
        async def spy(fn: Any) -> Any:
            ran.append(getattr(fn, "__name__", repr(fn)))
            return fn()

        ctl.offload = spy

    run(cfg_of(SMOKE), lives=2, setup=setup)
    # the world is restored at the start and after every death (ADR-031)
    assert ran == [
        "recover",
        "restore",
        "reset_creature_cgroup",
        "restore",
        "reset_creature_cgroup",
        "restore",
    ]
    plain = Controller(
        cfg_of(SMOKE),
        clock=VirtualClock(asyncio.new_event_loop()),  # type: ignore[arg-type]
        backend_for=lambda n, m: FakeBackend(None, load_costs(cfg_of())),  # type: ignore[arg-type]
        body=FakeBody(),
        costs_for=lambda m: load_costs(cfg_of()),
    )
    assert plain.offload is asyncio.to_thread  # the Pi: a worker thread


def test_stopping_tells_systemd() -> None:
    from epitaph.cli import _on_stop_signal  # pyright: ignore[reportPrivateUsage]

    notes: list[str] = []

    class Main:
        cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    ctl = Controller(
        cfg_of(SMOKE),
        clock=VirtualClock(asyncio.new_event_loop()),  # type: ignore[arg-type]
        backend_for=lambda n, m: FakeBackend(None, load_costs(cfg_of())),  # type: ignore[arg-type]
        body=FakeBody(),
        costs_for=lambda m: load_costs(cfg_of()),
        notify=notes.append,
    )
    main = Main()
    _on_stop_signal(ctl, main)  # type: ignore[arg-type]
    assert notes == ["STOPPING=1"] and main.cancelled
