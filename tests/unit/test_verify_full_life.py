"""verify-life at level full on the 30-minute installation life (ADR-024; card E6).

The checks phase 2 adds to the full level: the death comes when the plan kills it
(`death_time`), every reload loads its keyframe's rung and the decline reaches the last one
(`reload_targets`), every erosion step is taken as its own step (`erosion_steps`), and the
controller's non-fatal errors are counted (`error_events`, advisory). Each runs on a simulated
`pi4/default` life, then on the same life edited to break it.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from epitaph import verify as v
from epitaph.config import Config, load_config
from tests.helpers import read_events

Event = dict[str, Any]
PROFILE = "pi4/default"


@pytest.fixture(scope="module")
def default_life(tmp_path_factory: pytest.TempPathFactory) -> list[Event]:
    from epitaph.sim import simulate

    cfg = load_config(PROFILE, "pi4-4gb")
    events = simulate(cfg).events
    return [e for e in events if e["life"] == 1]


def judge(events: list[Event], cfg: Config | None = None) -> v.VerifyResult:
    cfg = cfg or load_config(PROFILE, "pi4-4gb")
    return v.verify_life(v.parse_life(copy.deepcopy(events), 1), cfg, "full")


def edit(events: list[Event], etype: str, nth: int = 0, **fields: Any) -> list[Event]:
    """A copy with fields of the nth event of a type changed."""
    out = copy.deepcopy(events)
    hits = [e for e in out if e["type"] == etype]
    hits[nth].update(fields)
    return out


def drop(events: list[Event], etype: str, nth: int = 0) -> list[Event]:
    hits = [i for i, e in enumerate(events) if e["type"] == etype]
    gone = hits[nth]
    return [copy.deepcopy(e) for i, e in enumerate(events) if i != gone]


def test_the_simulated_installation_life_passes_the_new_checks(default_life: list[Event]) -> None:
    res = judge(default_life)
    for name in ("death_time", "reload_targets", "erosion_steps", "error_events"):
        assert res.by_name(name).status == "pass", res.by_name(name)
    assert res.by_name("erosion_steps").value == 2  # ADR-024: two steps
    assert "groups at" in res.by_name("erosion_steps").detail  # the lag of each step


# -- death_time ---------------------------------------------------------------------------


def test_death_time_is_the_delay_after_the_squeeze(default_life: list[Event]) -> None:
    c = judge(default_life).by_name("death_time")
    assert c.limit == 10.0 and c.value == pytest.approx(0.0, abs=1.0)
    assert "death squeeze at 1770s" in c.detail


def test_a_slow_oom_kill_fails(default_life: list[Event]) -> None:
    """10.4: cause=oom within 10 s of the squeeze; a kernel that thrashes for 15 s fails."""
    late = edit(default_life, "death", lived_s=1785.0, t=1785.0)
    res = judge(late)
    assert res.by_name("death_time").status == "fail"
    assert res.by_name("duration").status == "pass"  # the old check alone let it through
    assert not res.ok


def test_a_death_before_the_squeeze_fails(default_life: list[Event]) -> None:
    early = edit(default_life, "death", lived_s=1700.0, t=1700.0)
    assert judge(early).by_name("death_time").status == "fail"


def test_without_a_death_event_death_time_fails(default_life: list[Event]) -> None:
    c = judge(drop(default_life, "death")).by_name("death_time")
    assert c.status == "fail" and c.detail == "no death event"


def test_death_mode_deadline_is_judged_at_the_lifespan(default_life: list[Event]) -> None:
    cfg = load_config(PROFILE, "pi4-4gb", overrides={"body": {"death_mode": "deadline"}})
    at_deadline = edit(default_life, "death", cause="deadline", lived_s=1800.0, t=1800.0)
    c = judge(at_deadline, cfg).by_name("death_time")
    assert c.status == "pass" and "deadline at 1800s" in c.detail
    assert judge(default_life, cfg).by_name("death_time").status == "fail"  # 30 s early


def test_death_time_skips_an_unbounded_life() -> None:
    cfg = load_config("pi4/unbounded", "pi4-4gb")
    from epitaph.sim import simulate

    cfg.profile.settings["ctx"] = 1200
    events = [e for e in simulate(cfg).events if e["life"] == 1]
    assert judge(events, cfg).by_name("death_time").status == "skip"


def test_death_time_runs_at_full_only(default_life: list[Event]) -> None:
    cfg = load_config(PROFILE, "pi4-4gb")
    res = v.verify_life(v.parse_life(copy.deepcopy(default_life), 1), cfg, "skeleton")
    names = {c.name for c in res.checks}
    assert not names & {"death_time", "reload_targets", "erosion_steps"}
    assert "error_events" in names  # every controller level counts them


# -- reload_targets -----------------------------------------------------------------------


def _vitals_after_reload(events: list[Event], k: int) -> int:
    """Index (among vitals) of the first vitals after the k-th reload."""
    reload_at = [i for i, e in enumerate(events) if e["type"] == "reload"][k]
    vitals = [i for i, e in enumerate(events) if e["type"] == "vitals"]
    return next(n for n, i in enumerate(vitals) if i > reload_at)


def test_a_reload_that_loads_the_wrong_rung_fails(default_life: list[Event]) -> None:
    n = _vitals_after_reload(default_life, 1)
    wrong = edit(default_life, "vitals", n, step=1)
    c = judge(wrong).by_name("reload_targets")
    assert c.status == "fail" and "reload at" in c.detail


def test_a_reload_with_the_wrong_threads_fails(default_life: list[Event]) -> None:
    wrong = edit(default_life, "reload", 1, threads=3)
    assert judge(wrong).by_name("reload_targets").status == "fail"


def test_a_reload_whose_quant_differs_from_the_loaded_one_fails(
    default_life: list[Event],
) -> None:
    wrong = edit(default_life, "reload", 0, to="Q8_0")
    assert judge(wrong).by_name("reload_targets").status == "fail"


def test_a_decline_that_stops_short_of_the_last_rung_fails(default_life: list[Event]) -> None:
    """The last reading must be at the step in force then (the reload count may still pass)."""
    out = copy.deepcopy(default_life)
    last = [e for e in out if e["type"] == "vitals"][-1]
    last["step"], last["threads"] = 1, 3
    c = judge(out).by_name("reload_targets")
    assert c.status == "fail" and "last reading" in c.detail


def test_a_reload_cut_by_the_death_is_not_judged(default_life: list[Event]) -> None:
    out = copy.deepcopy(default_life)
    reloads = [i for i, e in enumerate(out) if e["type"] == "reload"]
    cut = out[: reloads[1] + 1] + [e for e in out[reloads[1] + 1 :] if e["type"] != "vitals"]
    c = judge(cut).by_name("reload_targets")
    assert "no reading after it" in c.detail


def test_reload_targets_skip_a_profile_without_reloads(recorded_life: Any, tmp_path: Path) -> None:
    state = recorded_life("pi4/skeleton-1200")
    events = read_events(state / "lives" / "000001" / "events.jsonl")
    cfg = load_config("pi4/skeleton-1200", "pi4-4gb")
    assert judge(events, cfg).by_name("reload_targets").status == "skip"
    assert judge(events, cfg).by_name("erosion_steps").status == "skip"


def test_no_vitals_fails_reload_targets(default_life: list[Event]) -> None:
    out = [copy.deepcopy(e) for e in default_life if e["type"] != "vitals"]
    c = judge(out).by_name("reload_targets")
    assert c.status == "fail" and "no vitals" in c.detail


# -- erosion_steps ------------------------------------------------------------------------


def test_two_erosion_steps_merged_into_one_fail(default_life: list[Event]) -> None:
    """The first step never happened: the model went from five groups to none at once."""
    merged = drop(default_life, "erosion", 0)
    res = judge(merged)
    assert res.by_name("erosion_steps").status == "fail"
    assert res.by_name("persona_groups_at_death").status == "pass"  # the old check missed it


def test_an_erosion_step_with_the_wrong_groups_fails(default_life: list[Event]) -> None:
    wrong = edit(default_life, "erosion", 0, groups_left=3)
    c = judge(wrong).by_name("erosion_steps")
    assert c.status == "fail" and "planned [(2, True), (0, False)]" in c.detail


def test_an_erosion_step_that_keeps_the_mechanics_fails(default_life: list[Event]) -> None:
    wrong = edit(default_life, "erosion", 1, mechanics_present=True)
    assert judge(wrong).by_name("erosion_steps").status == "fail"


def test_a_step_planned_after_the_last_reading_is_not_expected(
    default_life: list[Event],
) -> None:
    """A life that dies before the second step's reading only owes the first one."""
    out = copy.deepcopy(default_life)
    second = [i for i, e in enumerate(out) if e["type"] == "erosion"][1]
    cut = [e for i, e in enumerate(out) if i < second or e["type"] in ("death", "death_shown")]
    c = judge(cut).by_name("erosion_steps")
    assert c.status == "pass" and c.limit == 1


# -- error_events -------------------------------------------------------------------------


def test_errors_are_advisory_and_never_fail_the_life(default_life: list[Event]) -> None:
    out = copy.deepcopy(default_life)
    at = next(i for i, e in enumerate(out) if e["type"] == "thought_end")
    err = {**out[at], "type": "error", "where": "transcript", "message": "disk full"}
    err.pop("turn", None)
    err.pop("text", None)
    out.insert(at + 1, err)
    res = judge(out)
    c = res.by_name("error_events")
    assert c.status == "advisory" and c.value == 1 and "transcript: disk full" in c.detail
    assert "error_events" in res.to_json()["advisory"]
    assert "error_events" not in res.to_json()["failed"]


# -- a life cut by a killed controller ----------------------------------------------------


def _lines(*items: object) -> str:
    return "".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in items)


BIRTH = {"v": 1, "ts": 1.0, "life": 1, "type": "birth", "t": 0.0}


def test_a_torn_line_before_the_recovery_record_is_ignored(tmp_path: Path) -> None:
    """Regression: recovery ends the torn line and appends a death record after it, which left
    the torn line in the middle of the file, and verify-life refused the life (exit 2)."""
    death = {"type": "death", "t": 8.0, "cause": "interrupted", "recovered": True}
    path = tmp_path / "events.jsonl"
    path.write_text(_lines(BIRTH, '{"v": 1, "ts": 2.0, "ty', death))
    assert [e["type"] for e in v.load_events(path)] == ["birth", "death"]


def test_a_bad_line_anywhere_else_is_an_error(tmp_path: Path) -> None:
    word = {"v": 1, "ts": 3.0, "life": 1, "type": "word", "t": 2.0, "text": "I"}
    path = tmp_path / "events.jsonl"
    path.write_text(_lines(BIRTH, '{"torn', word))
    with pytest.raises(json.JSONDecodeError):
        v.load_events(path)
    path.write_text(_lines(BIRTH, '{"torn', {"type": "death", "recovered": False}))
    with pytest.raises(json.JSONDecodeError):
        v.load_events(path)
    path.write_text(_lines(BIRTH, '{"torn'))  # last line torn: ignored, as before
    assert len(v.load_events(path)) == 1


# -- deferred reloads and merged erosion steps (gate review findings 6 and 7) ----------------


def _until_reload(events: list[Event], k: int) -> list[Event]:
    """The life up to (not including) its k-th reload."""
    at = [i for i, e in enumerate(events) if e["type"] == "reload"][k]
    return copy.deepcopy(events[:at])


def _end(events: list[Event], t: float, **vitals: Any) -> list[Event]:
    """Append a reading at t with these fields, then the death."""
    last = next(e for e in reversed(events) if e["type"] == "vitals")
    out = [*events, {**copy.deepcopy(last), "t": t, **vitals}]
    out.append({**copy.deepcopy(out[0]), "type": "death", "t": t + 5, "cause": "oom"})
    return out


def test_a_reload_deferred_by_the_gap_is_not_a_wrong_rung(default_life: list[Event]) -> None:
    """Regression: reload 1 at 489 s; the 780 s keyframe is due only at 489 + gap. A reading at
    805 s still at step 1 is then correct, not a decline that stopped short."""
    life = _end(_until_reload(default_life, 1), 805.206, step=1, threads=3)
    cfg = load_config(PROFILE, "pi4-4gb", overrides={"life": {"min_reload_gap_s": 400}})
    c = judge(life, cfg).by_name("reload_targets")
    assert c.status == "pass", c
    assert "reload deferred" in c.detail and "min_reload_gap_s" in c.detail
    # With the 120 s gap the reload was due at 780 s and the turn began at 805 s: a fault.
    c = judge(life).by_name("reload_targets")
    assert c.status == "fail" and "last reading" in c.detail


def test_a_reload_pending_when_the_reading_was_written(default_life: list[Event]) -> None:
    """The turn began before the keyframe; its reading came after it: the reload is next."""
    life = _until_reload(default_life, 1)
    next(e for e in reversed(life) if e["type"] == "thought_end")["t"] = 775.0
    c = judge(_end(life, 790.0, step=1, threads=3)).by_name("reload_targets")
    assert c.status == "pass" and "reload deferred" in c.detail
    # A rung that was never loaded is still wrong.
    c = judge(_end(life, 790.0, step=0, threads=3)).by_name("reload_targets")
    assert c.status == "fail"


def test_a_reload_skipped_after_the_reading_is_not_a_wrong_rung(
    default_life: list[Event],
) -> None:
    life = _end(_until_reload(default_life, 1), 805.206, step=1, threads=3)
    skipped = {**copy.deepcopy(life[0]), "type": "reload_skipped", "t": 900.0, "at": 780.0}
    life.insert(len(life) - 1, skipped)
    c = judge(life).by_name("reload_targets")
    assert c.status == "pass" and "skipped by the next reload" in c.detail


def test_two_erosion_keyframes_in_one_turn_merge_into_one_step(default_life: list[Event]) -> None:
    """Regression: a turn spanning both keyframes takes both at once; no reading saw the first."""
    out = drop(default_life, "erosion", 0)
    merged = [e for e in out if not (e["type"] == "vitals" and 1170 <= e["t"] < 1350)]
    c = judge(merged).by_name("erosion_steps")
    assert c.status == "pass", c
    assert "merged step" in c.detail and "19.5, 22.5" in c.detail


def test_the_mechanics_leave_with_the_last_group_whatever_the_keyframe_says(
    default_life: list[Event],
) -> None:
    """Persona.update forces the mechanics off at 0 groups: expect that, not the keyframe."""
    cfg = load_config(PROFILE, "pi4-4gb")
    for kf in cfg.profile.keyframes:
        if int(kf.values["persona_groups"]) == 0:
            kf.values["mechanics"] = True
    c = judge(default_life, cfg).by_name("erosion_steps")
    assert c.status == "pass", c
