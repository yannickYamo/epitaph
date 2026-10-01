"""Fault matrix rows that the fakes can inject today (BUILD_PLAN 10.4), checked end to end:
simulate the fault, then let verify-life judge the recorded life.

Rows that need the controller (hang detection, full pacing queue at death, recovery) or the
Pi arrive with B's controller (P1-P2) and C's fault scripts; docs/GATES.md tracks them.
"""

from __future__ import annotations

from typing import Any

import pytest

from epitaph import sim as sim_mod
from epitaph import verify as v
from epitaph.backend.fake import FakeBackend
from epitaph.config import load_config
from epitaph.costmodel import Costs, load_costs
from epitaph.sim import simulate


class CrashingBackend(FakeBackend):
    """Dies (signal 9) after a fixed number of generated tokens, like `kill -9`."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.fail_after_tokens = 150


def test_crash_is_recorded_and_fails_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sim_mod, "FakeBackend", CrashingBackend)
    cfg = load_config("pi4/skeleton-1200", "pi4-4gb")
    r = simulate(cfg, lives=2)
    assert r.causes == ["crash", "crash"]  # and the next life is born after the silence
    types = [e["type"] for e in r.events if e["life"] == 1]
    # The death flush (BUILD_PLAN 5.8): words generated before the crash are shown after it.
    assert types.count("death") == 1 and types.index("death") < types.index("death_shown")
    assert types[-2:] == ["death_shown", "silence"]
    res = v.verify_life(v.parse_life(r.events, 1), cfg, next_life=v.parse_life(r.events, 2))
    assert res.by_name("cause").status == "fail"
    assert res.by_name("duration").status == "fail"
    assert res.by_name("next_birth").status == "pass"


def test_deadline_during_a_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reload longer than the time left: cause=deadline, nothing typed after it."""
    deadline = {"body": {"death_mode": "deadline"}}
    cfg = load_config("pi4/compressed-2700", "pi4-4gb", overrides=deadline)

    def slow_step_2(cfg: Any) -> Costs:  # measured bench costs override the overlay's
        costs = load_costs(cfg)
        costs.load_s[2] = 3000.0
        return costs

    monkeypatch.setattr(sim_mod, "load_costs", slow_step_2)
    r = simulate(cfg)
    assert r.causes == ["deadline"]
    reloads = [e for e in r.events if e["type"] == "reload"]
    death = next(e for e in r.events if e["type"] == "death")
    assert len(reloads) == 2
    assert not [e for e in r.events if e["type"] == "word" and e["t"] > reloads[-1]["t"]]
    assert death["lived_s"] >= cfg.profile.lifespan_s


def test_full_context_in_a_small_ctx() -> None:
    cfg = load_config("pi4/unbounded", "pi4-4gb")
    cfg.profile.settings["ctx"] = 1200
    r = simulate(cfg)
    assert r.causes == ["full"]
    res = v.verify_life(v.parse_life(r.events), cfg)
    assert res.by_name("cause").status == "pass"


def test_oom_death_at_the_squeeze() -> None:
    cfg = load_config("pi4/compressed-2700", "pi4-4gb")
    r = simulate(cfg)
    death = next(e for e in r.events if e["type"] == "death")
    assert death["cause"] == "oom"
    squeeze = cfg.profile.lifespan_s - 30
    assert squeeze <= death["lived_s"] <= squeeze + 60
