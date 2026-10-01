from __future__ import annotations

import pytest

from epitaph.clock import Schedule
from epitaph.config import Config, load_config
from epitaph.costmodel import estimate, load_costs
from epitaph.sim import simulate
from tests.conftest import v6_config

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
    cfg = load_config("pi4/default", "pi4-4gb")
    r = simulate(cfg, lives=2)
    first = [e["type"] for e in r.events if e["life"] == 1]
    assert first[:5] == ORDER_START
    assert first[-3:] == ["death", "death_shown", "silence"]
    assert first.count("reload") == 2 and first.count("erosion") == len(
        Schedule(cfg.profile).erosion_times()
    )
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
    # Pinned to the v6 reference (V6_REFERENCE), where the two agree within 10%. On the Qwen3 4B
    # installation the sim shows more thoughts than the estimate (33 against 23): the sim
    # ignores the late slowdown (78% for the 4B) and its thoughts use less than `fill` of
    # max_tokens, so the cost model stays the conservative judge.
    cfg = v6_config()
    sim_n = simulate(cfg).thoughts[0]
    est_n = estimate(cfg, load_costs(cfg)).thoughts
    assert abs(sim_n - est_n) / est_n < 0.20


def test_deterministic() -> None:
    cfg = load_config("pi4/skeleton-1200", "pi4-4gb")
    a = [e["type"] + str(e.get("text", "")) for e in simulate(cfg, seed=3).events]
    b = [e["type"] + str(e.get("text", "")) for e in simulate(cfg, seed=3).events]
    assert a == b


def test_event_conventions() -> None:
    """Contract decisions E2, E3, D2, D6 and E4: `t` on every event (0 before birth),
    `birth_loading` names what the life runs with, and `gen_end` carries its timings."""
    cfg = load_config("pi4/compressed-2700", "pi4-4gb")
    r = simulate(cfg, lives=2)
    assert all("t" in e for e in r.events)
    for life in (1, 2):
        loading = next(e for e in r.events if e["life"] == life and e["type"] == "birth_loading")
        assert loading["t"] == 0.0
        assert loading["profile"] == "pi4/compressed-2700"
        assert loading["hardware"] == "pi4-4gb"
        assert loading["lifespan_s"] == cfg.profile.lifespan_s
    ends = [e for e in r.events if e["type"] == "gen_end"]
    assert all(e["prompt_n"] > 0 and e["tok_s"] > 0 for e in ends)


def test_every_memory_cut_emits_forget_the_reload_included() -> None:
    """Decision D5: the display fades what the reload cut, like any other loss."""
    r = simulate(load_config("pi4/default", "pi4-4gb"))
    types = [e["type"] for e in r.events]
    for i, e in enumerate(r.events):
        if e["type"] == "reload":
            assert e["recall_after"] < e["recall_before"]
            assert types[i + 1] == "forget" and r.events[i + 1]["items"]


def test_readings_come_from_the_mind() -> None:
    """The simulator writes the real readings, marker included (mind.prompt.Reader)."""
    r = simulate(load_config("pi4/default", "pi4-4gb"))
    readings = [e["reading"] for e in r.events if e["type"] == "vitals"]
    assert readings[0].startswith("[host] t+00:00 · boot complete · health: nominal")
    assert any("forgotten:" in x for x in readings)
    assert readings[-1].startswith("[host] ") and " · terminal · " in readings[-1]
    vitals = [e for e in r.events if e["type"] == "vitals"]
    assert vitals[0]["marker"] is False and vitals[-1]["marker"] is True


def test_a_full_context_from_the_server_is_a_full_death(monkeypatch) -> None:
    """If the server's count says full before the mind's does, the life still ends `full`."""
    from epitaph.mind.memory import Memory

    monkeypatch.setattr(Memory, "fits", lambda *a, **k: True)
    cfg = load_config("pi4/unbounded", "pi4-4gb")
    cfg.profile.settings["ctx"] = 1200
    assert simulate(cfg).causes == ["full"]


def _reload_silences(cfg: Config) -> list[tuple[float, int]]:
    """(seconds from each reload to the first word after it, prompt tokens read for it)."""
    ev = simulate(cfg).events
    out: list[tuple[float, int]] = []
    for i, e in enumerate(ev):
        if e["type"] == "reload":
            word = next(x for x in ev[i:] if x["type"] == "word")
            gen = next(x for x in ev[i:] if x["type"] == "gen_end")
            out.append((word["t"] - e["t"], int(gen["prompt_n"])))
    return out


def test_sim_honours_the_slot_handover() -> None:
    """Regression (checkpoint A): `epitaph sim` built its fake backend without
    `backend.reload_handover`, so every simulated reload re-read the whole context while the
    cost model, the rehearsal and the real backend carried the cache across it."""
    reread = _reload_silences(v6_config(overrides={"backend": {"reload_handover": "reread"}}))
    slot = _reload_silences(v6_config(overrides={"backend": {"reload_handover": "slot"}}))
    assert len(reread) == len(slot) == 2
    for (t_re, n_re), (t_slot, n_slot) in zip(reread, slot, strict=True):
        assert n_slot < n_re / 4 and t_slot < t_re / 2
