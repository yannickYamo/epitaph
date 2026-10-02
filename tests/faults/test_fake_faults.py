"""The fault matrix rows the fakes can inject (BUILD_PLAN 10.4; card E6), end to end.

Each test injects one fault into the real controller on the fakes (virtual time), on the
installation's own profile, `pi4/default` (ADR-024), then lets verify-life judge the
recorded life, as the Pi rows are judged. The controller-level unit tests
(tests/unit/test_controller*.py) prove the mechanisms; these prove what the evidence says.

docs/GATES.md names each test in the fault table's laptop column; `ROWS` below maps a 10.4 row
to its test, and tests/unit/test_fault_matrix_pi.py checks the two agree. Rows that need the
Pi run through tools/fault_matrix_pi.sh.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from epitaph import verify as v
from epitaph.backend.fake import FakeBackend
from epitaph.clock import Schedule, VirtualClock, run_virtual
from epitaph.config import Config, load_config
from epitaph.controller import HangWatch
from epitaph.costmodel import Costs, load_costs
from epitaph.events import Event
from epitaph.sim import SimBody, make_controller
from epitaph.types import Cause, ProgressCounters

DEFAULT = "pi4/default-reloads"

# 10.4 row -> the test in this file that injects it on the fakes.
ROWS: dict[str, str] = {
    "RAM death (`death_mode = oom`)": "test_oom_death_at_the_squeeze",
    "Crash": "test_crash_is_recorded_and_fails_verify",
    "Hang": "test_hang_kills_the_creature_and_the_next_life_follows",
    "Slow first token at low CPU share": "test_slow_first_token_at_low_cpu_share_is_not_a_hang",
    "Waiting on the SD card": "test_waiting_on_the_sd_card_is_not_a_hang",
    "Full context (`unbounded`)": "test_full_context_in_a_small_ctx",
    "Reload longer than a keyframe gap": "test_reload_longer_than_a_keyframe_gap",
    "Deadline during a reload": "test_deadline_during_a_reload",
    "Death with a full pacing queue": "test_death_with_a_full_pacing_queue",
    "Controller killed": "test_controller_killed_mid_life_is_recovered",
}


def cfg_of(profile: str = DEFAULT, **over: Any) -> Config:
    return load_config(profile, "pi4-4gb", overrides=over or None)


def run(
    cfg: Config,
    *,
    lives: int = 1,
    backend_cls: type[FakeBackend] = FakeBackend,
    costs: Costs | None = None,
    body: SimBody | None = None,
    state_dir: Path | None = None,
) -> list[Event]:
    """Run `lives` lives of the real controller on the fakes; return the events."""
    events: list[Event] = []

    async def main(clock: VirtualClock) -> None:
        ctl = make_controller(
            cfg,
            clock,
            lives=lives,
            publish=events,
            costs=costs,
            body=body,
            backend_cls=backend_cls,
            state_dir=state_dir,
        )
        await ctl.run()

    run_virtual(main)
    return events


def with_faults(**faults: Any) -> type[FakeBackend]:
    """A fake creature class whose faults are set on every instance (every life)."""

    class Faulty(FakeBackend):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            for k, val in faults.items():
                setattr(self.faults, k, val)

    return Faulty


def of(events: list[Event], etype: str, life: int = 1) -> list[Event]:
    return [e for e in events if e["type"] == etype and e["life"] == life]


def judge(events: list[Event], cfg: Config, n: int = 1) -> v.VerifyResult:
    """verify-life at the profile's level, with the next life (if recorded) for next_birth."""
    nxt = v.parse_life(events, n + 1) if any(e["life"] == n + 1 for e in events) else None
    return v.verify_life(v.parse_life(events, n), cfg, next_life=nxt)


def status(res: v.VerifyResult, name: str) -> str:
    return res.by_name(name).status


# -- deaths -------------------------------------------------------------------------------


def test_oom_death_at_the_squeeze() -> None:
    """The RAM death: cause=oom within 10 s of the squeeze; the next life after the silence."""
    cfg = cfg_of()
    ev = run(cfg, lives=2)
    death = of(ev, "death")[0]
    squeeze = Schedule(cfg.profile).death_s
    assert death["cause"] == "oom" and squeeze is not None
    assert squeeze <= death["lived_s"] <= squeeze + 10
    res = judge(ev, cfg)
    assert res.ok, v.format_result(res)
    for name in ("cause", "death_time", "next_birth", "reload_targets", "erosion_steps"):
        assert status(res, name) == "pass", name


def test_crash_is_recorded_and_fails_verify() -> None:
    """kill -9 on the creature: cause=crash, the death flush, then the next life."""

    class Crashing(FakeBackend):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            self.fail_after_tokens = 200  # the third thought, before the first reload

    cfg = cfg_of()
    ev = run(cfg, lives=2, backend_cls=Crashing)
    assert [e["cause"] for e in ev if e["type"] == "death"] == ["crash", "crash"]
    types = [e["type"] for e in ev if e["life"] == 1]
    assert types.index("death") < types.index("death_shown")
    assert types[-2:] == ["death_shown", "silence"]
    res = judge(ev, cfg)
    assert not res.ok
    assert status(res, "cause") == "fail" and status(res, "death_time") == "fail"
    assert status(res, "duration") == "fail"
    assert status(res, "next_birth") == "pass"


def test_hang_kills_the_creature_and_the_next_life_follows() -> None:
    """kill -STOP: cause=hang after the gap limit, the creature's cgroup killed, a new life."""
    kills: list[Cause] = []

    class Body(SimBody):
        def kill_now(self, cause: Cause) -> None:
            kills.append(cause)
            super().kill_now(cause)

    cfg = cfg_of()
    ev = run(cfg, lives=2, backend_cls=with_faults(hang_at_token=200), body=Body())
    death = of(ev, "death")[0]
    assert death["cause"] == "hang"
    assert Cause.HANG in kills  # cgroup.kill, not left stopped
    last_token = max(e["t"] for e in of(ev, "gen_start"))
    assert death["t"] - last_token >= 60  # at least the first-token floor of 5.9
    res = judge(ev, cfg)
    assert status(res, "cause") == "fail" and status(res, "next_birth") == "pass"
    assert of(ev, "birth", 2)  # the next life was born


def test_slow_first_token_at_low_cpu_share_is_not_a_hang() -> None:
    """Prompt processing six times slower, generation half as fast, through the late, low
    CPU share and clock: the counters move, so no false hang; the life dies as planned."""
    cfg = cfg_of()
    ev = run(cfg, backend_cls=with_faults(pp_factor=6.0, tg_factor=2.0))
    death = of(ev, "death")[0]
    assert death["cause"] == "oom"
    gaps = [g["t"] - s["t"] for s, g in zip(of(ev, "gen_start"), of(ev, "gen_end"), strict=False)]
    assert max(gaps) > float(cfg.get("body.token_gap_timeout_s"))  # longer than a token gap
    res = judge(ev, cfg)
    assert status(res, "cause") == "pass" and status(res, "death_time") == "pass"


def test_waiting_on_the_sd_card_is_not_a_hang() -> None:
    """The creature waits on the card for 200 s (longer than the 120 s token gap): no CPU time
    and no token, but its read I/O rises, so it is not a hang (5.9); with nothing moving at
    all, the same wait is one."""
    wait_s = 200.0

    class WaitsOnTheCard(FakeBackend):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            self.faults.hang_at_token = 150

        def hang(self) -> None:
            super().hang()

            async def later() -> None:
                await self.clock.sleep(wait_s)
                self.resume()

            asyncio.get_running_loop().create_task(later())

    class Body(SimBody):
        def __init__(self, io_moves: bool) -> None:
            super().__init__()
            self.io_moves = io_moves
            self.io = 0

        def progress(self) -> ProgressCounters:
            frozen = super().progress()  # frozen CPU time while the creature is stopped
            if self.creature is not None and self.creature.hung and self.io_moves:
                self.io += 4096
                return ProgressCounters(frozen.cpu_usec, frozen.io_rbytes + self.io)
            return frozen

    cfg = cfg_of()
    ev = run(cfg, backend_cls=WaitsOnTheCard, body=Body(io_moves=True))
    assert of(ev, "death")[0]["cause"] == "oom"
    gaps = [g["t"] - s["t"] for s, g in zip(of(ev, "gen_start"), of(ev, "gen_end"), strict=False)]
    assert max(gaps) >= wait_s  # the wait happened, inside one request
    assert status(judge(ev, cfg), "cause") == "pass"

    still = run(cfg, backend_cls=WaitsOnTheCard, body=Body(io_moves=False))
    assert of(still, "death")[0]["cause"] == "hang"

    # The rule itself: CPU flat, I/O rising, for longer than the limit.
    now = [0.0]
    watch = HangWatch(lambda: now[0])
    watch.begin("tokens", 120.0)
    for step in range(30):
        now[0] = step * 10.0
        assert not watch.check(ProgressCounters(cpu_usec=5, io_rbytes=step * 4096))
    now[0] += 121
    assert watch.check(ProgressCounters(cpu_usec=5, io_rbytes=29 * 4096))


def test_full_context_in_a_small_ctx() -> None:
    cfg = load_config("pi4/unbounded", "pi4-4gb")
    cfg.profile.settings["ctx"] = 1200
    ev = run(cfg)
    assert of(ev, "death")[0]["cause"] == "full"
    res = judge(ev, cfg)
    assert status(res, "cause") == "pass" and status(res, "death_time") == "skip"


# -- reloads ------------------------------------------------------------------------------


def test_reload_longer_than_a_keyframe_gap() -> None:
    """A thought so slow that both reload times pass before the loop can reload: one reload
    to the current target, the other reported as skipped; verify-life counts both."""
    cfg = cfg_of()
    ev = run(cfg, backend_cls=with_faults(tg_factor=12.0))
    (reload,), (skipped,) = of(ev, "reload"), of(ev, "reload_skipped")
    sch = Schedule(cfg.profile)
    assert reload["to"] == cfg.model().quant(sch.at(reload["t"]).step)
    assert skipped["t"] == reload["t"]
    res = judge(ev, cfg)
    assert status(res, "reload_count") == "pass"  # one done + one skipped = two planned
    assert status(res, "reload_targets") == "pass"


def test_deadline_during_a_reload() -> None:
    """A reload longer than the time left: cause=deadline, nothing typed after it."""
    cfg = cfg_of(body={"death_mode": "deadline"})
    costs = load_costs(cfg)
    costs.load_s[2] = 5000.0  # reload 2 cannot finish before the end
    ev = run(cfg, costs=costs)
    death = of(ev, "death")[0]
    assert death["cause"] == "deadline" and death["lived_s"] == pytest.approx(1800.0)
    reloads = of(ev, "reload")
    assert len(reloads) == 2 and len(of(ev, "reload_done")) == 1
    assert not [e for e in of(ev, "word") if e["t"] > reloads[-1]["t"]]
    res = judge(ev, cfg)
    assert status(res, "cause") == "pass" and status(res, "death_time") == "pass"
    assert status(res, "reload_targets") == "pass"  # the cut reload is not judged


# -- the death flush ----------------------------------------------------------------------


def test_death_with_a_full_pacing_queue() -> None:
    """The deadline falls while a whole thought waits to be typed (generation ten times faster
    than typing, a smoke life cut a second after its last thought was generated): the 20-odd
    words it really generated are typed at pace after the death, then death_shown (5.8). The
    installation's own late thoughts hold about eight words, so a short deadline life fills
    the queue instead. The deadline is placed from a life without it (nothing before the
    deadline depends on the lifespan), so a change of the readings does not move it off."""
    faults = with_faults(tg_factor=0.1)
    whole = run(load_config("pi4/smoke-300", "pi4-4gb"), backend_cls=faults)
    generated = [e["t"] for e in of(whole, "gen_end") if e["t"] < 298]
    cfg = load_config("pi4/smoke-300", "pi4-4gb", lifespan_s=int(generated[-1]) + 2)
    ev = run(cfg, backend_cls=faults)
    death, shown = of(ev, "death")[0], of(ev, "death_shown")[0]
    assert death["cause"] == "deadline"
    late = [e for e in of(ev, "word") if e["t"] > death["t"]]
    assert len(late) >= 20
    # the rest of the last thought, every word of it, in order
    last = of(ev, "thought_end")[-1]
    assert {e["turn"] for e in late} == {last["turn"]}
    assert last["text"].endswith(" ".join(e["text"] for e in late))
    for a, b in itertools.pairwise(late):  # at pace: never faster than typed
        assert b["t"] - a["t"] >= (sum(a["char_ms"]) + a["pause_after_ms"]) / 1000 - 0.01
    assert shown["t"] >= late[-1]["t"] and shown["words_total"] == len(of(ev, "word"))
    res = judge(ev, cfg)
    assert status(res, "death_shown_delay") == "pass" and status(res, "sync_rule") == "pass"


# -- the controller -----------------------------------------------------------------------


def test_controller_killed_mid_life_is_recovered(tmp_path: Path) -> None:
    """systemctl kill -s KILL: the life's files stop mid-life (no death record, a torn last
    line). The restarted controller closes it as `interrupted`, resets the creature's
    cgroup, and the counter goes on; verify-life does not pass the interrupted life."""
    cfg = cfg_of()
    state = tmp_path / "state"
    run(cfg, state_dir=state)
    life = state / "lives" / "000001"
    (life / "death.json").unlink()
    events = (life / "events.jsonl").read_text().splitlines()
    cut = next(i for i, line in enumerate(events) if json.loads(line)["t"] > 600)
    (life / "events.jsonl").write_text("\n".join(events[:cut]) + '\n{"torn')
    body = SimBody()
    body.death_squeeze = True  # left over from the killed controller

    ev = run(cfg, state_dir=state, body=body)
    record = json.loads((life / "death.json").read_text())
    assert record["cause"] == "interrupted" and record["recovered"] is True
    assert {e["life"] for e in ev} == {2}  # counter + 1
    assert not body.death_squeeze
    copy = tmp_path / "copy"
    shutil.copytree(state / "lives", copy / "lives")
    rec = v.load_events(copy / "lives" / "000001" / "events.jsonl")
    res = v.verify_life(v.parse_life(rec, 1), cfg, next_life=v.parse_life(ev, 2))
    assert status(res, "cause") == "fail" and res.by_name("cause").value == "interrupted"
    # no death screen was shown for a killed controller's life, so next_birth cannot be judged
    assert status(res, "next_birth") == "pending"
