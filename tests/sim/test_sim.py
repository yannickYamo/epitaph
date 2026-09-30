from __future__ import annotations

import pytest

from epitaph.config import load_config
from epitaph.costmodel import estimate, load_costs
from epitaph.sim import simulate

ORDER_START = ["birth_loading", "birth", "vitals", "gen_start", "thought_start"]


@pytest.mark.parametrize(
    "name,cause",
    [
        ("pi4/default", "oom"),
        ("pi4/compressed-2700", "oom"),
        ("pi4/skeleton-1200", "deadline"),
        ("pi4/smoke-300", "deadline"),
        ("pi4/unbounded", "full"),
    ],
)
def test_each_profile_dies_the_right_way(name: str, cause: str) -> None:
    r = simulate(load_config(name, "pi4-4gb"), lives=2)
    assert r.causes == [cause, cause]


def test_event_order_and_lifecycle() -> None:
    r = simulate(load_config("pi4/default", "pi4-4gb"), lives=2)
    first = [e["type"] for e in r.events if e["life"] == 1]
    assert first[:5] == ORDER_START
    assert first[-3:] == ["death", "death_shown", "silence"]
    assert first.count("reload") == 2 and first.count("erosion") == 5
    assert {e["life"] for e in r.events} == {1, 2}


def test_sync_rule_in_sim() -> None:
    """No gen_start before the previous thought ended (BUILD_PLAN 5.7 step 7)."""
    r = simulate(load_config("pi4/compressed-2700", "pi4-4gb"))
    open_turn = None
    for e in r.events:
        if e["type"] == "gen_start":
            assert open_turn is None
            open_turn = e["turn"]
        elif e["type"] == "thought_end":
            open_turn = None


def test_reload_reports_the_memory_cut() -> None:
    r = simulate(load_config("pi4/default", "pi4-4gb"))
    reloads = [e for e in r.events if e["type"] == "reload"]
    assert all(e["recall_after"] <= e["recall_before"] for e in reloads)


def test_sim_agrees_with_cost_model() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    sim_n = simulate(cfg).thoughts[0]
    est_n = estimate(cfg, load_costs(cfg)).thoughts
    assert abs(sim_n - est_n) / est_n < 0.35


def test_deterministic() -> None:
    cfg = load_config("pi4/skeleton-1200", "pi4-4gb")
    a = [e["type"] + str(e.get("text", "")) for e in simulate(cfg, seed=3).events]
    b = [e["type"] + str(e.get("text", "")) for e in simulate(cfg, seed=3).events]
    assert a == b
