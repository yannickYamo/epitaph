from __future__ import annotations

import json

import pytest

from epitaph.clock import Schedule
from epitaph.config import REPO_ROOT, RULE_DEFAULTS, ConfigError, load_config
from epitaph.costmodel import (
    Costs,
    estimate,
    format_report,
    load_costs,
    rule_minimums,
)
from tests.conftest import v6_config


@pytest.mark.parametrize(
    "name",
    ["pi4/default-reloads", "pi4/skeleton-1200", "pi4/smoke-300", "pi4/unbounded"],
)
def test_pi4_profiles_pass_with_bench_costs(name: str) -> None:
    """The merge gate: every Pi 4 profile passes on the costs in bench/ (measured, S1b-S4)."""
    cfg = load_config(name, "pi4-4gb")
    costs = load_costs(cfg)
    assert not costs.estimated, costs.source
    report = estimate(cfg, costs)
    assert report.ok, format_report(report)


@pytest.mark.parametrize("hardware", ["pi5-8gb", "pi5-16gb"])
@pytest.mark.parametrize("name", ["pi5/default", "pi5/skeleton-600", "pi5/unbounded"])
def test_pi5_profiles_pass_on_the_overlay_estimates(name: str, hardware: str) -> None:
    """The Pi 5 profiles pass the estimate on the Pi 5 overlays' estimated
    costs (no Pi 5 has been measured); speed never rises across a reload; unbounded fills."""
    cfg = load_config(name, hardware)
    costs = load_costs(cfg)
    assert costs.estimated
    report = estimate(cfg, costs)
    assert report.ok, format_report(report)
    if cfg.profile.unbounded:
        assert any("cause=full" in n for n in report.notes), report.notes


def test_the_kept_qwen3_1_7b_profile_passes_with_its_model() -> None:
    """pi4/default-qwen3-1.7b, the schedule before checkpoint A, still fits with its model."""
    cfg = v6_config()
    report = estimate(cfg, load_costs(cfg))
    assert report.ok, format_report(report)


def test_default_life_has_about_forty_thoughts() -> None:
    # 38 on measured Qwen3 1.7B costs since the reloads lower the CPU share.
    # Pinned to the v6 reference (V6_REFERENCE); the 4B installation since checkpoint A has
    # about 23.
    cfg = v6_config()
    assert 35 <= estimate(cfg, load_costs(cfg)).thoughts <= 75


def test_v5_schedule_would_fail_rule_b() -> None:
    """The v5 bug: reload 2 at 48:00 runs into erosion at 50:00 (Appendix C, V1)."""
    cfg = v6_config()  # the v6 schedule, whose reload 2 sits at end-17:30
    for kf in cfg.profile.keyframes:
        if kf.values["step"] == 2 and kf.at.from_end and kf.at.seconds == 17.5 * 60:
            object.__setattr__(kf.at, "seconds", 12 * 60)  # reload 2 at 48:00
    report = estimate(cfg, load_costs(cfg))
    assert any(v.rule == "b" for v in report.violations), format_report(report)


def test_unbounded_dies_full() -> None:
    cfg = load_config("pi4/unbounded", "pi4-4gb")
    assert any("cause=full" in n for n in estimate(cfg, load_costs(cfg)).notes)


def test_measured_costs_override_estimates(tmp_path) -> None:
    cfg = v6_config()
    (tmp_path / f"pi4-{cfg.model().name}-0-3.json").write_text(
        json.dumps({"step": 0, "threads": 3, "tg_tok_s": 0.5, "pp_tok_s": 4.0, "load_s": 90})
    )
    costs = load_costs(cfg, bench_dir=tmp_path)
    assert not costs.estimated and costs.tg(0, 3, 3.0) == 0.5 and costs.load(0) == 90
    slow = estimate(cfg, costs)
    fast = estimate(cfg, load_costs(cfg))
    assert slow.thoughts < fast.thoughts


def test_rates_scale_with_cpu_share() -> None:
    cfg = load_config("pi4/default-reloads", "pi4-4gb")
    c = load_costs(cfg)
    assert c.tg(2, 2, 1.0) == pytest.approx(c.tg(2, 2, 2.0) / 2)


def test_prompt_threads_follow_threads_batch() -> None:
    """S4: prompt processing keeps 3 threads when generation drops to 2, share permitting."""
    c = Costs({"1-2": 2.0}, {"1-2": 4.0, "1-3": 6.0}, [30.0])
    assert c.pp(1, 2, 3.0) == 4.0
    assert c.pp(1, 2, 3.0, threads_batch=3) == 6.0
    assert c.pp(1, 2, 2.0, threads_batch=3) == pytest.approx(4.0)  # 3 threads on 2 cores


def test_generation_drifts_down_through_a_life(tmp_path) -> None:
    """S1c: the bench file's late speed is reached after `late_after_s` and kept."""
    cfg = load_config("pi4/default-reloads", "pi4-4gb")
    rec = {"step": 0, "threads": 3, "tg_tok_s": 2.0, "tg_tok_s_birth": 2.2}
    (tmp_path / f"pi4-{cfg.model().name}-0-3.json").write_text(
        json.dumps({**rec, "tg_tok_s_late": 1.5, "late_after_s": 600})
    )
    c = load_costs(cfg, bench_dir=tmp_path)
    assert c.tg_late_factor == pytest.approx(0.75) and c.late_after_s == 600
    assert c.drift(0) == 1.0 and c.drift(300) == pytest.approx(0.875)
    assert c.drift(600) == c.drift(3000) == pytest.approx(0.75)
    assert c.tg_short(0, 3, 3.0) == 2.2 and c.tg_short(1, 2, 2.0) == c.tg(1, 2, 2.0)
    flat = Costs({"0-3": 1.0}, {"0-3": 5.0}, [50.0], late_after_s=0, tg_late_factor=0.9)
    assert flat.drift(0) == 0.9


def test_reload_silence_is_checked() -> None:
    """A reload that re-reads a large memory fails like verify-life would (S4: 180 s)."""
    cfg = load_config(
        "pi4/default-reloads", "pi4-4gb", overrides={"verify": {"max_reload_silence_s": 60}}
    )
    report = estimate(cfg, load_costs(cfg))
    assert [v.rule for v in report.violations].count("silence") == 2, format_report(report)


def test_speed_decline_is_checked() -> None:
    """The last 5 minutes must run under 40% of the first 5 (verify-life, full level)."""
    cfg = load_config("pi4/default-reloads", "pi4-4gb")
    for kf in cfg.profile.keyframes:
        if "cpu_share" in kf.values and kf.values["cpu_share"] < 2.0:
            kf.values["cpu_share"] = 2.0
        if "cpu_mhz" in kf.values:
            kf.values["cpu_mhz"] = 1800.0  # the clock lever slows it too (spike S7)
    report = estimate(cfg, load_costs(cfg))
    assert any(v.rule == "speed" for v in report.violations), format_report(report)
    ok = estimate(load_config("pi4/default-reloads", "pi4-4gb"), load_costs(cfg))
    assert any("speed last 5 min" in n for n in ok.notes)
    skeleton = load_config("pi4/skeleton-1200", "pi4-4gb")  # not a full-level profile
    assert not any("speed" in n for n in estimate(skeleton, load_costs(skeleton)).notes)


def test_first_marker_costs_only_its_tokens() -> None:
    """Decision A3: the marker rides on the reading, so it adds tokens but no re-read."""
    cfg = load_config("pi4/skeleton-1200", "pi4-4gb")
    with_marker = estimate(cfg, load_costs(cfg))
    silent = load_config(
        "pi4/skeleton-1200", "pi4-4gb", overrides={"prompt": {"memory_gap_marker": ""}}
    )
    without = estimate(silent, load_costs(silent))
    assert with_marker.thoughts == without.thoughts


def test_system_prompt_falls_back_to_the_estimate_without_persona_text() -> None:
    cfg = load_config(
        "pi4/default-reloads", "pi4-4gb", overrides={"prompt": {"persona_active": "?"}}
    )
    empty = {"prompt": {"persona_groups": []}}
    none = load_config("pi4/default-reloads", "pi4-4gb", overrides=empty, validate=False)
    for c in (cfg, none):
        assert estimate(c, load_costs(c)).thoughts > 0


def test_slot_handover_shortens_reloads_and_fits_the_4b() -> None:
    """ADR-014 (contract A16): a reload that carries the cache is silent for the load, not the
    re-read, and the Qwen3 4B profile only fits the hour with it."""

    m = "qwen3-4b-instruct-2507"
    reports = {}
    for handover in ("reread", "slot"):
        cfg = load_config(
            "pi4/default-reloads",  # the 4B schedule (pi4/default-qwen3-4b before checkpoint A)
            "pi4-4gb",
            overrides={"life": {"models": [m]}, "backend": {"reload_handover": handover}},
        )
        reports[handover] = estimate(cfg, load_costs(cfg, m, REPO_ROOT / "bench" / "measured"))
    assert reports["slot"].thoughts > reports["reread"].thoughts
    assert reports["slot"].ok, format_report(reports["slot"])
    assert not reports["reread"].ok


def test_rule_minimums_default_to_the_one_hour_values() -> None:
    assert rule_minimums(Schedule(v6_config().profile)) == RULE_DEFAULTS


def test_the_thirty_minute_life_sets_its_own_minimums() -> None:
    need = rule_minimums(Schedule(load_config("pi4/default-reloads", "pi4-4gb").profile))
    assert need == {
        "between_health": 2,
        "after_reload": 1,
        "per_erosion_step": 1,
        "after_erosion_start": 3,
    }


def test_a_raised_minimum_is_reported_with_its_value() -> None:
    cfg = load_config("pi4/default-reloads", "pi4-4gb")
    cfg.profile.settings["rules"] = {"after_erosion_start": 40}
    report = estimate(cfg, load_costs(cfg))
    assert any(v.rule == "d" and "need 40" in v.detail for v in report.violations)


@pytest.mark.parametrize("rules", [{"after_reload": 0}, {"bogus": 2}, {"after_reload": 1.5}, "x"])
def test_bad_minimums_are_refused(rules: object) -> None:
    cfg = load_config("pi4/default-reloads", "pi4-4gb")
    cfg.profile.settings["rules"] = rules
    with pytest.raises(ConfigError):
        rule_minimums(Schedule(cfg.profile))
