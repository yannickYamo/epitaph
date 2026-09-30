"""verify-life's `speed_monotonic` (review 2, F2): generation never speeds up across a reload.

Crafted lives pin the rule (tolerance, the averaging window, the vitals fallback and its
one-thought lag, the edge cases); simulated lives on the Pi 4 profiles check it against an
independent reading of their `gen_end` events.
"""

from __future__ import annotations

import statistics
from typing import Any

import pytest

from epitaph import verify as v
from epitaph.config import Config, load_config
from tests.helpers import LifeBuilder, read_events, slowing

PROFILE = "pi4/compressed-2700"
TEXT = "My memory holds 900 tokens now. I am slower than before, and something is gone."


def cfg(**verify: Any) -> Config:
    return load_config(PROFILE, "pi4-4gb", overrides={"verify": verify} if verify else None)


def life(
    before: list[float | None],
    after: list[float | None],
    *more: list[float | None],
    gen_end: bool = True,
) -> LifeBuilder:
    """Thoughts at the given speeds, a reload between each list; speeds go on `gen_end`
    (or, with gen_end=False, lagged on the next thought's vitals like a real controller)."""
    b = LifeBuilder().birth()
    last: float | None = None
    for i, group in enumerate([before, after, *more]):
        if i:
            b.reload(frm=f"q{i - 1}", to=f"q{i}")
        for speed in group:
            if gen_end:
                b.thought(TEXT, tok_s=speed)
            else:
                b.thought(TEXT, vitals={"tok_s": last})
                last = speed
    return b.death("oom")


def check(b: LifeBuilder, config: Config | None = None) -> v.Check:
    verifier = v.Verifier(v.parse_life(b.events), config or cfg())
    (result,) = verifier.check_speed_monotonic()
    return result


# -- the rule on crafted lives -------------------------------------------------------------


def test_a_rise_across_a_reload_fails() -> None:
    """The round-1 profile's reload 1: 1.65 -> 2.41 tokens/s (F2)."""
    c = check(life([1.65, 1.65, 1.65], [2.41, 2.41]))
    assert c.status == "fail"
    assert c.value == pytest.approx(2.41 / 1.65, abs=1e-3)
    assert c.limit == pytest.approx(1.05)
    assert "1.65 -> 2.41 tok/s (+46%)" in c.detail


def test_a_slowdown_passes() -> None:
    c = check(life([1.65, 1.65], [1.2, 1.1], [0.9, 0.8]))
    assert c.status == "pass"
    assert c.value == pytest.approx(0.85 / 1.15, abs=1e-3)  # the worse of the two reloads
    assert c.detail.count("reload at") == 2


@pytest.mark.parametrize(("after", "status"), [(1.04, "pass"), (1.05, "pass"), (1.06, "fail")])
def test_noise_within_the_tolerance_passes(after: float, status: str) -> None:
    assert check(life([1.0, 1.0], [after, after])).status == status


def test_the_tolerance_comes_from_config() -> None:
    b = life([1.65, 1.65], [2.41, 2.41])
    assert check(b, cfg(speed_monotonic_tolerance=0.5)).status == "pass"
    assert check(b, cfg(speed_monotonic_tolerance=0.0)).status == "fail"
    assert check(life([1.0], [1.01]), cfg(speed_monotonic_tolerance=0.0)).status == "fail"


def test_two_thoughts_are_averaged_on_each_side() -> None:
    """Before: the last two thoughts (2.0, 1.0 -> 1.5); after: the first two (1.1, 1.1)."""
    b = life([5.0, 2.0, 1.0], [1.1, 1.1, 9.0])
    assert check(b).status == "pass"
    assert check(b).value == pytest.approx(1.1 / 1.5, abs=1e-3)
    one = check(b, cfg(speed_monotonic_thoughts=1))  # 1.0 -> 1.1
    assert one.status == "fail" and one.value == pytest.approx(1.1, abs=1e-3)


def test_windows_stop_at_the_neighbouring_reloads() -> None:
    """Reload 2 compares the thoughts between the reloads only: 1.0 -> 0.9, not 3.0 -> 0.9."""
    c = check(life([3.0, 3.0], [1.0], [0.9, 0.9]), cfg(speed_monotonic_thoughts=5))
    assert c.status == "pass"
    assert "3.00 -> 1.00" in c.detail and "1.00 -> 0.90" in c.detail


def test_vitals_fallback_follows_the_one_thought_lag() -> None:
    """Without gen_end rates, a thought's speed is on the next vitals: the first vitals after
    the reload still reports the old speed, so the rise is attributed to the right thought."""
    b = life([1.65, 1.65], [2.41, 2.41], gen_end=False)
    speeds = v.Verifier(v.parse_life(b.events), cfg()).thought_speeds()
    assert speeds == [1.65, 1.65, 2.41, None]  # the last thought has no reading after it
    c = check(b)
    assert c.status == "fail" and c.value == pytest.approx(2.41 / 1.65, abs=1e-3)
    assert check(life([1.65, 1.65], [1.5, 1.4], gen_end=False)).status == "pass"


def test_gen_end_rates_win_over_vitals() -> None:
    b = LifeBuilder().birth()
    b.thought(TEXT, tok_s=1.0, vitals={"tok_s": 9.0})
    b.reload()
    b.thought(TEXT, tok_s=0.9, vitals={"tok_s": 9.0})
    b.death("oom")
    assert check(b).status == "pass"


def test_a_thought_without_a_rate_is_left_out() -> None:
    """A thought whose gen_end has no usable rate (none, zero) does not count as a sample."""
    b = life([1.0, None], [0.0, 0.9])  # gen_end tok_s absent before, 0.0 after
    c = check(b)
    assert c.status == "pass" and c.value == pytest.approx(0.9, abs=1e-3)


def test_no_reload_is_skipped() -> None:
    b = LifeBuilder().birth().thought(TEXT, tok_s=1.0).thought(TEXT, tok_s=2.0).death("oom")
    assert check(b).status == "skip"


def test_death_in_the_silence_is_not_judged() -> None:
    b = LifeBuilder().birth().thought(TEXT, tok_s=1.0).reload().death("oom")
    c = check(b)
    assert c.status == "skip" and "not compared" in c.detail


def test_reloads_without_any_rate_fail() -> None:
    b = life([None, None], [None, None])
    for e in b.events:  # no vitals rates either
        e.pop("tok_s", None)
    c = check(b)
    assert c.status == "fail" and c.detail == "no tokens/s samples"


# -- where it runs --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "runs"),
    [("smoke", False), ("skeleton", False), ("screen", False), ("full", True), ("rehearsal", True)],
)
def test_it_runs_at_the_levels_with_whole_lives(level: str, runs: bool) -> None:
    res = v.verify_life(v.parse_life(life([1.0], [2.0]).events), cfg(), level)
    names = {c.name for c in res.checks}
    assert ("speed_monotonic" in names) is runs
    if runs:
        assert "speed_monotonic" in res.to_json()["failed"]


def test_summary_and_compare_report_it() -> None:
    res = v.verify_life(v.parse_life(life([1.0], [2.0]).events), cfg(), "rehearsal")
    s = v.summarize(res)
    assert s["metrics"]["speed_monotonic"] == pytest.approx(2.0)
    assert s["status"]["speed_monotonic"] == "fail"
    table = v.format_compare([{**s, "label": "x"}])
    assert "Speed after/before reload" in table and "**2.00**" in table


def test_the_thresholds_are_in_the_default_config() -> None:
    """Both thresholds are set in config/default.toml, not only in verify's fallback."""
    section = load_config(PROFILE, "pi4-4gb").section("verify")
    assert section["speed_monotonic_tolerance"] == v.DEFAULT_THRESHOLDS["speed_monotonic_tolerance"]
    assert section["speed_monotonic_thoughts"] == v.DEFAULT_THRESHOLDS["speed_monotonic_thoughts"]


# -- simulated lives on the profiles -------------------------------------------------------


def rise_from_gen_end(events: list[dict[str, Any]], k: int) -> list[float]:
    """An independent reading: per reload, mean gen_end rate of k thoughts after / k before."""
    ratios: list[float] = []
    reloads = [i for i, e in enumerate(events) if e["type"] == "reload"]
    edges = [-1, *reloads, len(events)]
    rates = [(i, float(e["tok_s"])) for i, e in enumerate(events) if e["type"] == "gen_end"]
    for j, at in enumerate(reloads):
        before = [r for i, r in rates if edges[j] < i < at][-k:]
        after = [r for i, r in rates if at < i < edges[j + 2]][:k]
        if before and after:
            ratios.append(statistics.fmean(after) / statistics.fmean(before))
    return ratios


@pytest.mark.parametrize("profile", ["pi4/default", "pi4/compressed-2700"])
def test_simulated_lives_agree_with_their_own_rates(recorded_life, profile: str) -> None:
    """Whatever the profile's CPU share at each reload, the verdict matches the rates the
    simulator logged; the same life with its speeds held non-increasing passes."""
    events = read_events(recorded_life(profile) / "lives" / "000001" / "events.jsonl")
    config = load_config(profile, "pi4-4gb")
    k = int(config.get("verify.speed_monotonic_thoughts"))
    tol = float(config.get("verify.speed_monotonic_tolerance"))
    ratios = rise_from_gen_end(events, k)
    assert len(ratios) == 2  # both reloads compared
    res = v.verify_life(v.parse_life(events), config)
    c = res.by_name("speed_monotonic")
    assert c.value == pytest.approx(max(ratios), abs=1e-3)
    assert c.status == ("pass" if max(ratios) <= 1 + tol else "fail")
    held = v.verify_life(v.parse_life(slowing(events)), config)
    assert held.by_name("speed_monotonic").status == "pass"
