"""The controller (BUILD_PLAN 5.8, 5.9, 6.3) on the fakes, in virtual time.

Every death cause, a death during a reload, recovery of an interrupted life, the life
counter, the sync rule, the death flush, hang detection and watchdog pings in every state.
No network (a unix socket at most), no real time.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from epitaph.backend.base import CreatureDied
from epitaph.backend.fake import FakeBackend
from epitaph.body.fake import FakeBody
from epitaph.clock import Schedule, VirtualClock, run_virtual
from epitaph.config import Config, load_config
from epitaph.controller import (
    Controller,
    GuardedBackend,
    HangLimits,
    HangWatch,
    Life,
    pick_model,
    sd_notify,
    watchdog_interval,
)
from epitaph.costmodel import Costs, load_costs
from epitaph.events import Event
from epitaph.sim import FakeSlots, SimBody, make_controller
from epitaph.state import LifeCounter, life_dir
from epitaph.transcript import read_events
from epitaph.types import Cause, ModelSpec, Msg, Sampling

BACKGROUND: list[asyncio.Future[None]] = []  # tasks a test schedules beside the controller
SMOKE = "pi4/smoke-300"  # 5 minutes, deadline death, no reloads
DEFAULT = "pi4/default"  # 30 minutes, two reloads, OOM death at end-0:30


def cfg_of(profile: str = SMOKE, **over: Any) -> Config:
    return load_config(profile, "pi4-4gb", overrides=over or None)


Setup = Callable[[Controller, VirtualClock], Awaitable[None]]


def run(
    cfg: Config,
    *,
    lives: int = 1,
    backend_cls: type[FakeBackend] = FakeBackend,
    state_dir: Path | None = None,
    costs: Costs | None = None,
    setup: Setup | None = None,
    notify: Callable[[str], object] | None = None,
) -> tuple[Controller, list[Event]]:
    """Run `lives` lives of the real controller on the fakes; return it and its events."""
    events: list[Event] = []

    async def main(clock: VirtualClock) -> Controller:
        ctl = make_controller(
            cfg,
            clock,
            lives=lives,
            publish=events,
            state_dir=state_dir,
            costs=costs,
            backend_cls=backend_cls,
        )
        ctl.notify = notify
        if setup is not None:
            await setup(ctl, clock)
        await ctl.run()
        return ctl

    return run_virtual(main), events


def of(events: list[Event], etype: str, life: int | None = None) -> list[Event]:
    return [e for e in events if e["type"] == etype and (life is None or e["life"] == life)]


def death(events: list[Event], life: int = 1) -> Event:
    (d,) = of(events, "death", life)
    return d


def with_faults(**faults: Any) -> type[FakeBackend]:
    """A fake creature class whose faults are set on every instance."""

    class Faulty(FakeBackend):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            for k, v in faults.items():
                setattr(self.faults, k, v)

    return Faulty


# -- every death cause --------------------------------------------------------------------


def test_deadline_kills_at_the_lifespan_and_the_words_are_flushed() -> None:
    ctl, ev = run(cfg_of(SMOKE))
    d = death(ev)
    assert d["cause"] == "deadline" and d["lived_s"] == pytest.approx(300.0)
    types = [e["type"] for e in ev]
    shown = of(ev, "death_shown")[0]
    # the death flush: words generated before the deadline are typed after it, then the card
    late_words = [e for e in of(ev, "word") if e["t"] > d["t"]]
    assert late_words and types.index("death") < types.index("death_shown")
    assert shown["t"] >= late_words[-1]["t"]
    assert shown["t"] - d["t"] <= float(cfg_of().get("verify.max_death_display_delay_s"))
    assert shown["words_total"] == len(of(ev, "word"))
    assert ctl.records[0].cause == "deadline" and ctl.records[0].thoughts == 3


def test_oom_at_the_squeeze_on_time() -> None:
    cfg = cfg_of(DEFAULT)
    _, ev = run(cfg)
    d = death(ev)
    assert d["cause"] == "oom"
    assert d["lived_s"] == pytest.approx(Schedule(cfg.profile).death_s, abs=0.1)


def test_crash_mid_thought() -> None:
    _, ev = run(cfg_of(SMOKE), backend_cls=with_faults(crash_at_s=200.0))
    d = death(ev)
    born = of(ev, "birth")[0]
    assert d["cause"] == "crash"
    # crash_at_s is absolute clock time; the life clock started at birth, after the load
    assert d["ts"] - born["ts"] == pytest.approx(d["t"], abs=0.01)
    assert d["t"] < 200.0


def test_hang_is_detected_without_progress() -> None:
    """kill -STOP: no token and no counter moves; the gap limit (120 s on the Pi 4) kills."""
    cfg = cfg_of(SMOKE)
    _, ev = run(cfg, backend_cls=with_faults(hang_at_s=180.0))
    d = death(ev)
    assert d["cause"] == "hang"
    last_token_t = max(e["t"] for e in of(ev, "word"))
    born = of(ev, "birth")[0]
    hang_t = 180.0 - (born["ts"] - of(ev, "birth_loading")[0]["ts"])  # in life time
    gap = float(cfg.get("body.token_gap_timeout_s"))
    assert hang_t + 60 <= d["t"] <= hang_t + gap + 3  # first-token or gap limit, 1 s ticks
    assert d["t"] < 300.0
    assert last_token_t <= d["t"] + 120  # the flush only types what came before


def test_a_slow_creature_that_progresses_is_not_a_hang() -> None:
    """Prompt processing four times slower: the counters move, so no false hang (10.4)."""
    _, ev = run(cfg_of(SMOKE), backend_cls=with_faults(pp_factor=4.0, tg_factor=3.0))
    assert death(ev)["cause"] == "deadline"


def test_hang_while_loading_at_birth() -> None:
    cfg = cfg_of(SMOKE)
    _, ev = run(cfg, backend_cls=with_faults(hang_on_start=True))
    d = death(ev)
    assert d["cause"] == "hang" and d["t"] == 0.0 and d["lived_s"] == 0.0  # never born
    assert not of(ev, "birth")
    loading, shown = of(ev, "birth_loading")[0], of(ev, "death_shown")[0]
    assert shown["ts"] - loading["ts"] == pytest.approx(cfg.get("life.load_timeout_s"), abs=2)


def test_full_context_ends_the_unbounded_life() -> None:
    cfg = load_config("pi4/unbounded", "pi4-4gb")
    cfg.profile.settings["ctx"] = 1200
    _, ev = run(cfg)
    assert death(ev)["cause"] == "full"


def test_manual_new_life_ends_this_one_and_starts_the_next_now() -> None:
    cfg = cfg_of(SMOKE)
    asked: list[dict[str, Any]] = []

    async def setup(ctl: Controller, clock: VirtualClock) -> None:
        ctl.reconfigure = lambda profile, lifespan: load_config(
            profile or SMOKE, "pi4-4gb", lifespan_s=lifespan
        )

        async def later() -> None:
            await asyncio.sleep(200)
            asked.append(await ctl.ctl_new_life({"lifespan": 240}))

        BACKGROUND.append(asyncio.ensure_future(later()))

    ctl, ev = run(cfg, lives=2, setup=setup)
    assert asked == [{"ok": True, "ending": 1}]
    assert [r.cause for r in ctl.records] == ["manual", "deadline"]
    # the silence is cut short and the next life runs with the asked lifespan, once
    loading = of(ev, "birth_loading", 2)[0]
    assert loading["lifespan_s"] == 240.0
    assert loading["ts"] - of(ev, "death_shown", 1)[0]["ts"] < 1.0
    assert death(ev, 2)["lived_s"] == pytest.approx(240.0)


def test_new_life_rejects_a_bad_request_without_ending_the_life() -> None:
    """Regression: a bad profile was only read after the current life had been killed."""
    replies: list[dict[str, Any]] = []

    async def setup(ctl: Controller, clock: VirtualClock) -> None:
        from epitaph.config import ConfigError

        def reconfigure(profile: str | None, lifespan: float | None) -> Config:
            raise ConfigError(f"no profile {profile}")

        async def later() -> None:
            await asyncio.sleep(100)
            replies.append(await ctl.ctl_new_life({"model": "nope"}))
            ctl.reconfigure = reconfigure
            replies.append(await ctl.ctl_new_life({"profile": "pi4/nope"}))
            replies.append(await ctl.ctl_new_life({"lifespan": "soon"}))
            ctl.reconfigure = None
            replies.append(await ctl.ctl_new_life({"profile": "pi4/default"}))

        BACKGROUND.append(asyncio.ensure_future(later()))

    ctl, _ = run(cfg_of(SMOKE), setup=setup)
    assert [r["ok"] for r in replies] == [False] * 4
    assert "nope" in replies[0]["error"] and "pi4/nope" in replies[1]["error"]
    assert "cannot change" in replies[3]["error"]
    assert [r.cause for r in ctl.records] == ["deadline"]  # the life was never touched


# -- deaths during a reload -----------------------------------------------------------------


def test_crash_during_a_reload() -> None:
    class CrashOnReload(FakeBackend):
        async def start(self, model: ModelSpec, quant: str, threads: int) -> None:
            self.faults.crash_on_start = self.starts >= 1  # birth loads, the reload dies
            await super().start(model, quant, threads)

    _, ev = run(cfg_of(DEFAULT), backend_cls=CrashOnReload)
    reload = of(ev, "reload")[0]
    d = death(ev)
    assert d["cause"] == "crash" and d["t"] > reload["t"]
    assert not of(ev, "reload_done")
    assert not [e for e in of(ev, "word") if e["t"] > reload["t"]]
    assert of(ev, "death_shown") and of(ev, "silence")


def test_deadline_during_a_reload() -> None:
    cfg = cfg_of(DEFAULT, body={"death_mode": "deadline"})
    costs = load_costs(cfg)
    costs.load_s[2] = 5000.0  # reload 2 cannot finish before the end
    _ctl, ev = run(cfg, costs=costs)
    d = death(ev)
    assert d["cause"] == "deadline" and d["lived_s"] == pytest.approx(1800.0)
    assert len(of(ev, "reload")) == 2 and len(of(ev, "reload_done")) == 1
    assert not [e for e in of(ev, "word") if e["t"] > of(ev, "reload")[-1]["t"]]


def test_hang_during_a_reload() -> None:
    class HangOnReload(FakeBackend):
        async def start(self, model: ModelSpec, quant: str, threads: int) -> None:
            self.faults.hang_on_start = self.starts >= 1
            await super().start(model, quant, threads)

    cfg = cfg_of(DEFAULT)
    _, ev = run(cfg, backend_cls=HangOnReload)
    reload, d = of(ev, "reload")[0], death(ev)
    assert d["cause"] == "hang"
    # no progress for load_timeout_s, measured from the last counter change (1 s ticks)
    assert 0 <= d["t"] - reload["t"] - float(cfg.get("life.load_timeout_s")) <= 3


def test_watchdog_pings_through_a_long_reload() -> None:
    cfg = cfg_of(DEFAULT)
    costs = load_costs(cfg)
    costs.load_s[1] = 600.0  # ten minutes to load step 1
    notes: list[str] = []
    ctl, ev = run(cfg, costs=costs, notify=notes.append)
    reload, done = of(ev, "reload")[0], of(ev, "reload_done")[0]
    assert done["seconds"] >= 600
    # From the start to the end of the run (loading, the long reload, the silence), never
    # more than 5 s without a ping. The loop's time is 0 when the controller starts, which
    # is when the first life's birth_loading is stamped.
    times = ctl.ping_times
    assert times[0] == pytest.approx(0.0)
    assert max(b - a for a, b in itertools.pairwise(times)) <= 5.0 + 1e-6
    start = of(ev, "birth_loading")[0]["ts"]
    r0 = reload["ts"] - start
    assert [t for t in times if r0 < t < r0 + done["seconds"]]
    assert times[-1] >= of(ev, "silence")[0]["ts"] - start - 5.0
    assert notes[0] == "READY=1" and notes.count("WATCHDOG=1") == ctl.pings


# -- the sync rule ------------------------------------------------------------------------


def test_sync_rule_no_request_before_the_last_word_is_shown() -> None:
    cfg = cfg_of(DEFAULT)
    _, ev = run(cfg)
    shown_until: float | None = None
    for e in ev:
        if e["type"] == "word":
            end = e["t"] + (sum(e["char_ms"]) + e["pause_after_ms"]) / 1000
            shown_until = max(shown_until or 0.0, end)
        elif e["type"] == "gen_start":
            if shown_until is not None:
                assert e["t"] >= shown_until - 0.01, e
        elif e["type"] == "thought_end":
            assert shown_until is None or e["t"] >= shown_until - 0.01
    starts = [e["turn"] for e in of(ev, "gen_start")]
    assert starts == sorted(starts)


def test_pause_overlaps_the_next_request() -> None:
    """The pause is a minimum silence between thoughts, not added after it (5.7 step 7)."""
    cfg = cfg_of(SMOKE)
    _, ev = run(cfg)
    k = Schedule(cfg.profile).at(0)
    ends = of(ev, "thought_end")
    for prev_end in ends[:-1]:
        nxt_start = next(e for e in of(ev, "gen_start") if e["turn"] == prev_end["turn"] + 1)
        nxt_word = next(e for e in of(ev, "word") if e["turn"] == prev_end["turn"] + 1)
        assert nxt_start["t"] == pytest.approx(prev_end["t"], abs=0.01)
        assert nxt_word["t"] - prev_end["t"] >= k.pause_s - 0.01


# -- recovery, the counter, transcripts ----------------------------------------------------


def test_counter_is_written_before_anything_of_the_life(tmp_path: Path) -> None:
    seen: list[int] = []
    counter = LifeCounter(tmp_path)

    async def setup(ctl: Controller, clock: VirtualClock) -> None:
        inner = ctl.publish
        assert inner is not None

        def spy(e: Event) -> None:
            if e["type"] == "birth_loading":
                seen.append(counter.current())
            inner(e)

        ctl.publish = spy

    _ctl, _ = run(cfg_of(SMOKE), lives=2, state_dir=tmp_path, setup=setup)
    assert seen == [1, 2]
    _ctl2, ev2 = run(cfg_of(SMOKE), lives=1, state_dir=tmp_path)
    assert {e["life"] for e in ev2} == {3} and counter.current() == 3


def test_transcripts_are_written_per_life(tmp_path: Path) -> None:
    _, ev = run(cfg_of(SMOKE), lives=2, state_dir=tmp_path)
    for n in (1, 2):
        d = life_dir(tmp_path, n)
        events = read_events(d / "events.jsonl")
        assert events == [e for e in ev if e["life"] == n]
        record = json.loads((d / "death.json").read_text())
        assert record["cause"] == "deadline" and record["life"] == n
        assert record["words"] == len(of(ev, "word", n))
        text = (d / "thoughts.txt").read_text()
        assert text.count("[host]") == len(of(ev, "thought_end", n))
        assert "-- death" in text
        meta = json.loads((d / "meta.json").read_text())
        assert meta["profile"] == SMOKE and meta["life"] == n
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "stopped" and status["lives_run"] == 2


def test_recovery_closes_an_interrupted_life(tmp_path: Path) -> None:
    """The controller was killed mid-life: the next start closes it, kills any leftover
    creature, and the counter goes on from there."""
    run(cfg_of(SMOKE), state_dir=tmp_path)
    # life 2 was cut off mid-thought: events but no death record, the counter at 2
    LifeCounter(tmp_path).next()
    d2 = life_dir(tmp_path, 2)
    d2.mkdir(parents=True)
    lines = [
        {"v": 1, "ts": 1.0, "life": 2, "type": "birth_loading", "t": 0.0, "model": "m"},
        {"v": 1, "ts": 2.0, "life": 2, "type": "birth", "t": 0.0},
        {"v": 1, "ts": 50.0, "life": 2, "type": "word", "t": 48.5, "text": "I"},
    ]
    (d2 / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines) + '{"torn')
    body = SimBody()
    body.death_squeeze = True  # a squeeze left over from the killed controller

    async def setup(ctl: Controller, clock: VirtualClock) -> None:
        ctl.body = body

    _ctl, ev = run(cfg_of(SMOKE), state_dir=tmp_path, setup=setup)
    record = json.loads((d2 / "death.json").read_text())
    assert record["cause"] == "interrupted" and record["lived_s"] == 48.5
    assert record["words"] == 1 and record["recovered"] is True
    tail = read_events(d2 / "events.jsonl")[-1]
    assert tail["type"] == "death" and tail["cause"] == "interrupted"
    assert {e["life"] for e in ev} == {3}
    assert not body.death_squeeze  # the creature cgroup was reset before the birth


def test_recovery_keeps_a_cause_already_recorded(tmp_path: Path) -> None:
    d = life_dir(tmp_path, 1)
    d.mkdir(parents=True)
    lines = [
        {"v": 1, "ts": 1.0, "life": 1, "type": "birth_loading", "t": 0.0, "model": "m"},
        {"v": 1, "ts": 9.0, "life": 1, "type": "death", "t": 70.0, "cause": "oom", "lived_s": 70},
    ]
    (d / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    ctl = Controller(
        cfg_of(SMOKE),
        clock=VirtualClock(asyncio.new_event_loop()),  # type: ignore[arg-type]
        backend_for=lambda n, m: FakeBackend(None, load_costs(cfg_of())),  # type: ignore[arg-type]
        body=FakeBody(),
        costs_for=lambda m: load_costs(cfg_of()),
        state_dir=tmp_path,
    )
    assert ctl.recover() == [1]
    record = json.loads((d / "death.json").read_text())
    assert record["cause"] == "oom" and record["lived_s"] == 70
    assert len(read_events(d / "events.jsonl")) == 2  # nothing appended


# -- the control channel ------------------------------------------------------------------


def test_status_and_screenshot() -> None:
    replies: list[dict[str, Any]] = []

    async def setup(ctl: Controller, clock: VirtualClock) -> None:
        async def later() -> None:
            await asyncio.sleep(150)
            replies.append(await ctl.ctl_status({}))
            replies.append(await ctl.ctl_screenshot({"path": "/tmp/x.png"}))

        BACKGROUND.append(asyncio.ensure_future(later()))

    _ctl, ev = run(cfg_of(SMOKE), setup=setup)
    status, shot = replies
    assert status["state"] == "living" and status["life"] == 1 and status["model"]
    assert status["profile"] == SMOKE and 0 < status["t"] < 300 and status["turn"] >= 1
    assert shot == {"requested": "/tmp/x.png"}
    (e,) = of(ev, "screenshot")
    assert e["path"] == "/tmp/x.png"


def test_attach_registers_the_control_commands() -> None:
    class Bus:
        def __init__(self) -> None:
            self.handlers: dict[str, Any] = {}

        def on(self, cmd: str, fn: Any) -> None:
            self.handlers[cmd] = fn

    async def setup(ctl: Controller, clock: VirtualClock) -> None:
        bus = Bus()
        ctl.attach(bus)
        assert set(bus.handlers) == {"status", "new_life", "screenshot"}

    run(cfg_of(SMOKE), setup=setup)


# -- reloads -------------------------------------------------------------------------------


def test_reloads_carry_the_echo_and_the_slot() -> None:
    _, ev = run(cfg_of(DEFAULT))
    assert len(of(ev, "reload")) == len(of(ev, "reload_done")) == 2
    readings = [e["reading"] for e in of(ev, "vitals")]
    after = [r for r in readings if "-bit (was" in r]
    assert len(after) == 2 and all("your words now:" in r for r in after)
    # the echo did not cost the carried memory: the first request after the reload is small
    for r in of(ev, "reload"):
        gen = next(e for e in of(ev, "gen_end") if e["t"] > r["t"])
        assert gen["prompt_n"] < 100


def test_a_failed_slot_save_means_no_echo() -> None:
    async def setup(ctl: Controller, clock: VirtualClock) -> None:
        async def refuse(name: str) -> bool:
            return False

        def slots_for(b: Any) -> FakeSlots:
            s = FakeSlots(b)
            s.save = refuse  # type: ignore[method-assign]
            return s

        ctl.slots_for = slots_for

    _, ev = run(cfg_of(DEFAULT), setup=setup)
    readings = [e["reading"] for e in of(ev, "vitals")]
    assert not [r for r in readings if "your words now:" in r]


def test_a_reload_that_comes_late_jumps_to_the_current_target() -> None:
    """A thought so slow that both reload times pass: one reload, to the current target, and
    the reload it jumped over is reported as skipped (5.2)."""
    cfg = cfg_of(DEFAULT)
    sch = Schedule(cfg.profile)
    life = Life(cfg, VirtualClock(asyncio.new_event_loop()), None, FakeBody(), lambda e: None)  # type: ignore[arg-type]
    r1, r2 = sch.reload_times()
    assert life.skipped_reloads(r1 - 1) == []
    assert life.skipped_reloads(r2 + 5) == [r1] and life.reload_kf == r2
    assert life.skipped_reloads(r2 + 60) == []

    # generation twelve times slower: the second thought ends after both reload times
    _, ev = run(cfg, backend_cls=with_faults(tg_factor=12.0))
    (reload,), (skipped,) = of(ev, "reload"), of(ev, "reload_skipped")
    assert reload["t"] > r2 and skipped["t"] == reload["t"]
    assert reload["to"] == cfg.model().quant(sch.at(r2).step)
    assert skipped["to"] == cfg.model().quant(sch.at(r1).step) and skipped["skipped"] == 1


# -- pieces --------------------------------------------------------------------------------


def test_hang_watch() -> None:
    now = [0.0]
    w = HangWatch(lambda: now[0])
    assert not w.check(1)
    w.begin("load", 10.0)
    now[0] = 9.0
    assert not w.check(1)
    now[0] = 10.5
    assert w.check(1)
    assert not w.check(2)  # the counters moved: progress
    now[0] = 15.0
    w.token(3.0)
    now[0] = 18.5
    assert w.check(2)
    w.end()
    assert not w.check(2)
    assert HangLimits().first_token(300, 10.0) == pytest.approx(150.0)


def test_guarded_stream_dies_when_the_controller_declares_it() -> None:
    cfg = cfg_of(SMOKE)

    async def main(clock: VirtualClock) -> list[str]:
        fake = FakeBackend(clock, load_costs(cfg))
        died = asyncio.Event()
        g = GuardedBackend(fake, died, HangWatch(clock.now), HangLimits(), lambda: 10.0)
        model = cfg.model()
        await g.start(model, model.quant(0), 3)
        got: list[str] = []

        async def kill() -> None:
            await asyncio.sleep(60)
            died.set()

        BACKGROUND.append(asyncio.ensure_future(kill()))
        with pytest.raises(CreatureDied):
            async for c in g.chat([Msg("user", "[host] hello")], Sampling(0.7, 0.05), 80):
                got.append(c.text)
        with pytest.raises(CreatureDied):
            await g.prefill([Msg("system", "x")])
        g.close()
        return got

    got = run_virtual(main)
    assert got  # some tokens came before the death


def test_pick_model_rotation() -> None:
    names = ["qwen3-1.7b", "qwen3-4b-instruct-2507"]
    cfg = cfg_of(SMOKE, life={"models": names})
    assert [pick_model(cfg, i).name for i in range(3)] == [names[0], names[1], names[0]]
    fixed = cfg_of(SMOKE, life={"models": names, "rotation": "fixed"})
    assert {pick_model(fixed, i).name for i in range(4)} == {names[0]}
    rnd = cfg_of(SMOKE, life={"models": names, "rotation": "random"})
    picks = [pick_model(rnd, i, seed=1).name for i in range(20)]
    assert set(picks) == set(names) and picks == [
        pick_model(rnd, i, seed=1).name for i in range(20)
    ]


def test_rotation_across_lives() -> None:
    names = ["qwen3-1.7b", "qwen3-4b-instruct-2507"]
    _, ev = run(cfg_of(SMOKE, life={"models": names}), lives=2)
    assert [e["model"] for e in of(ev, "birth_loading")] == names


def test_sd_notify_sends_one_datagram(tmp_path: Path) -> None:
    path = tmp_path / "notify.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as srv:
        srv.bind(str(path))
        assert sd_notify("WATCHDOG=1", {"NOTIFY_SOCKET": str(path)})
        assert srv.recv(64) == b"WATCHDOG=1"
    assert not sd_notify("WATCHDOG=1", {})
    assert not sd_notify("READY=1", {"NOTIFY_SOCKET": str(tmp_path / "gone.sock")})


def test_sd_notify_abstract_socket() -> None:
    name = f"epitaph-test-{id(object())}"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as srv:
        srv.bind("\0" + name)
        assert sd_notify("READY=1", {"NOTIFY_SOCKET": "@" + name})
        assert srv.recv(64) == b"READY=1"


def test_watchdog_interval() -> None:
    assert watchdog_interval({}) == 5.0
    assert watchdog_interval({"WATCHDOG_USEC": "4000000"}) == 2.0
    assert watchdog_interval({"WATCHDOG_USEC": "60000000"}) == 5.0
    assert watchdog_interval({"WATCHDOG_USEC": "junk"}) == 5.0


def test_cause_by_clock_when_nobody_names_it() -> None:
    cfg = cfg_of(DEFAULT)
    clock = VirtualClock(asyncio.new_event_loop())  # type: ignore[arg-type]
    life = Life(cfg, clock, None, FakeBody(), lambda e: None)  # type: ignore[arg-type]
    assert life.time_cause() == Cause.CRASH.value  # not born: nothing on the clock
    assert life.lived() == 0.0
