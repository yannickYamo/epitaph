"""The cost model's `speed_monotonic` rule (review 2, F2): no reload speeds generation up.

Crafted costs pin the rule (a rise, a slowdown, a rise that only the shorter context after
the reload causes, the warn mode) on the v6 reference schedule's reload times (tests/conftest.py
V6_REFERENCE, pi4/default before checkpoint A) with every CPU share set to the thread count,
so the rates alone decide; the Pi 4 profiles as tuned are checked on the costs in bench/.
"""

from __future__ import annotations

import json
import runpy
import shutil
from pathlib import Path
from typing import Any

import pytest

from epitaph.config import REPO_ROOT, Config, deep_merge, load_config
from epitaph.costmodel import Costs, estimate, format_report, load_costs
from tests.conftest import V6_REFERENCE_PROFILE, v6_config

# The v6 reference: step 0 (3 threads), reload 1 to step 1, reload 2 to step 2 (2 threads)
FAST_PP = {"0-3": 200.0, "1-3": 200.0, "2-2": 200.0}


def cfg(mode: str | None = "fail", profile: str | None = None) -> Config:
    """The profile as tuned (the v6 reference if None), with `estimate.speed_monotonic` set to
    `mode`."""
    overrides: dict[str, Any] | None = {"estimate": {"speed_monotonic": mode}} if mode else None
    if profile is None:
        return v6_config(overrides=overrides)
    return load_config(profile, "pi4-4gb", overrides=overrides)


def full_share(mode: str = "fail") -> Config:
    """The v6 reference with every keyframe's CPU share equal to its threads: rates alone decide."""
    config = cfg(mode)
    for kf in config.profile.keyframes:
        kf.values["cpu_share"] = float(kf.values["threads"])
    return config


def costs(tg: dict[str, float], **kw: Any) -> Costs:
    return Costs(tg, dict(FAST_PP), [20.0, 20.0, 20.0], estimated=False, **kw)


def speed_rules(config: Config, c: Costs) -> list[str]:
    report = estimate(config, c)
    return [v.detail for v in report.violations if v.rule == "speed_monotonic"]


def test_a_faster_quant_after_a_reload_is_a_violation() -> None:
    """The round-1 shape: Q8_0 at 3 threads, then Q4_K_M at the same share, is faster."""
    rules = speed_rules(full_share(), costs({"0-3": 1.65, "1-3": 2.41, "2-2": 1.0}))
    assert len(rules) == 1
    assert "from 1.65 to 2.41 tokens/s (+46%)" in rules[0]
    assert rules[0].startswith("reload at 28.")


def test_slower_after_every_reload_passes() -> None:
    report = estimate(full_share(), costs({"0-3": 2.0, "1-3": 1.5, "2-2": 1.2}))
    assert not [v for v in report.violations if v.rule == "speed_monotonic"], format_report(report)
    note = next(n for n in report.notes if n.startswith("speed across reloads"))
    assert "2.00 -> 1.50 at 28." in note and "1.50 -> 1.20 at" in note


def test_equal_speed_passes() -> None:
    assert not speed_rules(full_share(), costs({"0-3": 1.5, "1-3": 1.5, "2-2": 1.5}))


def test_a_shorter_context_after_the_reload_counts() -> None:
    """The reload cuts memory; a model that is faster at a short context speeds up although
    its deep rates are equal. Without the prompt sizes of the bench runs it cannot tell."""
    flat = {"0-3": 1.5, "1-3": 1.5, "2-2": 1.5}
    short = {"0-3": 2.0, "1-3": 2.0, "2-2": 2.0}
    aware = costs(flat, tg_short_tok_s=short, short_ctx=280.0, deep_ctx=830.0)
    rules = speed_rules(full_share(), aware)
    assert rules and "from 1.50 to" in rules[0]
    assert not speed_rules(full_share(), costs(flat, tg_short_tok_s=short))


def test_tg_at_interpolates_between_the_bench_contexts() -> None:
    c = costs({"1-3": 2.4}, tg_short_tok_s={"1-3": 2.8}, short_ctx=300.0, deep_ctx=800.0)
    assert c.tg_at(1, 3, 3.0, 300) == pytest.approx(2.8)
    assert c.tg_at(1, 3, 3.0, 550) == pytest.approx(2.6)
    assert c.tg_at(1, 3, 3.0, 800) == pytest.approx(2.4)
    assert c.tg_at(1, 3, 3.0, 50) == pytest.approx(2.8)  # held, not extrapolated
    assert c.tg_at(1, 3, 3.0, 2000) == pytest.approx(2.4)
    assert c.tg_at(1, 3, 1.5, 550) == pytest.approx(1.3)  # scales with the CPU share
    blind = costs({"1-3": 2.4}, tg_short_tok_s={"1-3": 2.8})
    assert blind.tg_at(1, 3, 3.0, 300) == pytest.approx(2.4)


def test_bench_files_give_the_context_sizes() -> None:
    """bench/ records the birth prompt and the deep prompt of every measured thought."""
    c = load_costs(cfg())
    assert c.short_ctx is not None and c.deep_ctx is not None
    assert 200 < c.short_ctx < c.deep_ctx < 1200


def test_warn_mode_reports_a_note() -> None:
    report = estimate(full_share("warn"), costs({"0-3": 1.65, "1-3": 2.41, "2-2": 1.0}))
    assert not [v for v in report.violations if v.rule == "speed_monotonic"]
    assert any(n.startswith("WARNING speed_monotonic") for n in report.notes)


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="speed_monotonic"):
        estimate(full_share("maybe"), costs({"0-3": 1.0, "1-3": 1.0, "2-2": 1.0}))


@pytest.mark.parametrize("profile", ["pi4/smoke-300", "pi4/skeleton-1200", "pi4/unbounded"])
def test_profiles_without_reloads_have_nothing_to_compare(profile: str) -> None:
    config = cfg(profile=profile)
    report = estimate(config, load_costs(config))
    assert not any(n.startswith("speed across reloads") for n in report.notes)
    assert not [v for v in report.violations if v.rule == "speed_monotonic"]


@pytest.mark.parametrize("profile", ["pi4/default", "pi4/compressed-2700"])
def test_pi4_profiles_never_speed_up_at_a_reload(profile: str) -> None:
    """The merge gate for F2 on the costs in bench/: every reload keeps or lowers the speed.
    (The kept Qwen3 1.7B schedule does not: it rises 7% at reload 1, see V6_REFERENCE.)"""
    config = cfg(profile=profile)
    report = estimate(config, load_costs(config))
    assert not [v for v in report.violations if v.rule == "speed_monotonic"], format_report(report)


def test_the_g0_script_judges_it_in_strict_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`estimate_measured.py --strict` (gate G0.4) fails a rise even while the config warns."""
    import epitaph.config

    def warning_config(*a: Any, overrides: dict[str, Any] | None = None, **kw: Any) -> Config:
        # The config as it was before checkpoint A, when the rule only warned.
        over = deep_merge({"estimate": {"speed_monotonic": "warn"}}, overrides or {})
        return load_config(*a, overrides=over, **kw)

    monkeypatch.setattr(epitaph.config, "load_config", warning_config)
    for f in (REPO_ROOT / "bench").glob("pi4-qwen3-1.7b-*.json"):
        shutil.copy(f, tmp_path)
    rec = json.loads((tmp_path / "pi4-qwen3-1.7b-1-3.json").read_text())
    rec.update(tg_tok_s=9.0, tg_tok_s_birth=9.0)  # step 1 far faster than step 0: a rise
    (tmp_path / "pi4-qwen3-1.7b-1-3.json").write_text(json.dumps(rec))
    script = runpy.run_path(str(REPO_ROOT / ".github" / "scripts" / "estimate_measured.py"))
    argv = ["--bench", str(tmp_path), "--profiles", V6_REFERENCE_PROFILE, "--models", "qwen3-1.7b"]
    script["main"](argv)
    assert "(speed_monotonic)" not in capsys.readouterr().out
    assert script["main"]([*argv, "--strict"]) == 1
    assert "(speed_monotonic)" in capsys.readouterr().out
