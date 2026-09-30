from __future__ import annotations

import json

import pytest

from epitaph.config import load_config
from epitaph.costmodel import estimate, format_report, load_costs


@pytest.mark.parametrize(
    "name",
    ["pi4/default", "pi4/compressed-2700", "pi4/skeleton-1200", "pi4/smoke-300", "pi4/unbounded"],
)
def test_pi4_profiles_pass_with_estimated_costs(name: str) -> None:
    cfg = load_config(name, "pi4-4gb")
    report = estimate(cfg, load_costs(cfg))
    assert report.ok, format_report(report)


def test_default_life_has_about_fifty_thoughts() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    assert 40 <= estimate(cfg, load_costs(cfg)).thoughts <= 75


def test_v5_schedule_would_fail_rule_b() -> None:
    """The v5 bug: reload 2 at 48:00 runs into erosion at 50:00 (Appendix C, V1)."""
    cfg = load_config("pi4/default", "pi4-4gb")
    for kf in cfg.profile.keyframes:
        if kf.values["step"] == 2 and kf.at.from_end and kf.at.seconds == 17 * 60:
            object.__setattr__(kf.at, "seconds", 12 * 60)  # reload 2 at 48:00
    report = estimate(cfg, load_costs(cfg))
    assert any(v.rule == "b" for v in report.violations), format_report(report)


def test_unbounded_dies_full() -> None:
    cfg = load_config("pi4/unbounded", "pi4-4gb")
    assert any("cause=full" in n for n in estimate(cfg, load_costs(cfg)).notes)


def test_measured_costs_override_estimates(tmp_path) -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    (tmp_path / "pi4-llama-3.2-3b-instruct-0-3.json").write_text(
        json.dumps({"step": 0, "threads": 3, "tg_tok_s": 0.5, "pp_tok_s": 4.0, "load_s": 90})
    )
    costs = load_costs(cfg, bench_dir=tmp_path)
    assert not costs.estimated and costs.tg(0, 3, 3.0) == 0.5 and costs.load(0) == 90
    slow = estimate(cfg, costs)
    fast = estimate(cfg, load_costs(cfg))
    assert slow.thoughts < fast.thoughts


def test_rates_scale_with_cpu_share() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    c = load_costs(cfg)
    assert c.tg(2, 2, 1.0) == pytest.approx(c.tg(2, 2, 2.0) / 2)
