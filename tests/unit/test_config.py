from __future__ import annotations

import pytest

from epitaph.config import (
    ConfigError,
    TimeSpec,
    deep_merge,
    load_config,
    parse_duration,
    parse_time,
    validate_config,
)

PI4 = ["pi4/default", "pi4/smoke-300", "pi4/skeleton-1200", "pi4/unbounded"]
PI5 = ["pi5/default", "pi5/skeleton-600", "pi5/unbounded"]


def test_parse_times() -> None:
    assert parse_duration("60:00") == 3600
    assert parse_duration(90) == 90
    assert parse_time("28:00") == TimeSpec(1680, False)
    assert parse_time("end-3:30") == TimeSpec(210, True)
    for bad in ("28", "end-", "1:60", "x"):
        with pytest.raises(ConfigError):
            parse_time(bad)


def test_time_resolution_scales_or_anchors() -> None:
    assert TimeSpec(1800, False).resolve(3600, 1800) == 900
    assert TimeSpec(180, True).resolve(3600, 1800) == 1620


@pytest.mark.parametrize("name", PI4)
def test_pi4_profiles_load(name: str) -> None:
    cfg = load_config(name, "pi4-4gb")
    assert cfg.hw_class == "pi4"
    assert cfg.profile.keyframes[0].values["step"] in (0, 1)


@pytest.mark.parametrize("name", PI5)
def test_pi5_profiles_load(name: str) -> None:
    assert load_config(name, "pi5-8gb").hw_class == "pi5"


def test_extends_inherits_and_rescales() -> None:
    cfg = load_config("pi5/skeleton-600", "pi5-8gb")
    assert cfg.profile.lifespan_s == 600
    assert cfg.profile.nominal_s == 1200


def test_impossible_lifespan_is_rejected() -> None:
    with pytest.raises(ConfigError, match="not after the previous"):
        load_config("pi4/default", "pi4-4gb", lifespan_s=600)


def test_budget_feasibility_is_checked() -> None:
    with pytest.raises(ConfigError, match="exceeds ctx"):
        load_config("pi4/default", "pi4-4gb", overrides={"backend": {"ctx": 1024}})


def test_unknown_model_is_rejected() -> None:
    with pytest.raises(ConfigError, match=r"not in models\.toml"):
        load_config("pi4/default", "pi4-4gb", overrides={"life": {"models": ["nope"]}})


def test_overlay_overrides_base() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    assert cfg.get("verify.wpm_birth_range") == [15, 60]  # decision 30
    assert cfg.get("body.token_gap_timeout_s") == 120


def test_deep_merge() -> None:
    assert deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}}) == {"a": {"b": 1, "c": 3}}


def test_model_ladder_per_class() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    m = cfg.model("qwen3-4b-instruct-2507")
    assert m.ladder[0] == "Q4_K_M"  # a 4B Q6_K does not fit in 4 GB
    assert m.quant(5) == "Q2_K"


def test_material_readings_need_quiet_readings() -> None:
    with pytest.raises(ConfigError, match="readings_material needs"):
        load_config("pi4/default", "pi4-4gb", overrides={"prompt": {"readings_quiet": False}})


@pytest.mark.parametrize("mhz", [0, 500, 2000])
def test_cpu_clock_outside_the_pi_range_is_rejected(mhz: int) -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    cfg.profile.keyframes[-1].values["cpu_mhz"] = mhz
    with pytest.raises(ConfigError, match="cpu_mhz must be 600-1800"):
        validate_config(cfg)


def test_bad_rules_fail_at_load() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    cfg.profile.settings["rules"] = {"after_reload": 0}
    with pytest.raises(ConfigError, match="after_reload"):
        validate_config(cfg)


@pytest.mark.parametrize("value", ["Blocked", "off", "", "block", 0])
def test_creature_network_must_be_blocked_or_allowed(value: object) -> None:
    """Regression: anything but "blocked" used to leave the creature's network open."""
    with pytest.raises(ConfigError, match="creature_network"):
        load_config("pi4/default", "pi4-4gb", overrides={"body": {"creature_network": value}})
    for ok in ("blocked", "allowed"):
        load_config("pi4/default", "pi4-4gb", overrides={"body": {"creature_network": ok}})
