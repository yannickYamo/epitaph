"""Schedule mechanics, pinned to the v6 reference schedule (tests/conftest.py V6_REFERENCE)."""

from __future__ import annotations

import pytest

from epitaph.clock import FakeClock, Schedule
from epitaph.config import Config, load_config
from epitaph.types import Health
from tests.conftest import v6_config


def test_birth_values(v6_default: Config) -> None:
    k = Schedule(v6_default.profile).at(0)
    assert (k.step, k.threads, k.recall, k.persona_groups, k.health) == (
        0,
        3,
        1280,
        5,
        Health.NOMINAL,
    )
    assert not k.death_squeeze


def test_recall_and_cpu_share_are_cut_at_the_reload(v6_default: Config) -> None:
    s = Schedule(v6_default.profile)
    assert s.at(27.9 * 60).recall == 1000
    assert s.at(27.9 * 60).cpu_share == 3.0
    k = s.at(28 * 60)
    # Rebased on measured Pi 4 costs (docs/PROFILES.md): the thread drop is at reload 2, and
    # each reload lowers the CPU share so generation never speeds up (review 2, F2).
    assert (k.recall, k.step, k.threads, k.cpu_share) == (220, 1, 3, 2.0)
    k2 = s.at(42.5 * 60)
    assert (k2.recall, k2.step, k2.threads, k2.cpu_share) == (100, 2, 2, 1.5)


def test_interpolation_between_keyframes(v6_default: Config) -> None:
    s = Schedule(v6_default.profile)
    mid = s.at(32 * 60)
    assert 190 < mid.recall < 220
    assert 0.85 < mid.temperature < 1.0


def test_stepped_fields_hold(v6_default: Config) -> None:
    s = Schedule(v6_default.profile)
    assert s.at(48.9 * 60).persona_groups == 5
    assert s.at(49 * 60).persona_groups == 4


def test_v6_timeline(v6_default: Config) -> None:
    s = Schedule(v6_default.profile)
    assert s.reload_times() == [1680, 2550]
    assert s.erosion_times() == [2940, 3060, 3180, 3300, 3420]
    assert s.death_s == 3570
    assert s.at(3571).death_squeeze


def test_end_anchors_survive_rescale() -> None:
    # The v6 schedule (end-3:00 last erosion, end-0:30 death) rescaled to 45 min; before
    # checkpoint A this used a 45-minute test profile, since retired.
    cfg = v6_config(lifespan_s=2700)
    s = Schedule(cfg.profile)
    assert s.erosion_times()[-1] == 2700 - 180
    assert s.death_s == 2670


def test_fake_clock() -> None:
    c = FakeClock()
    c.advance(5)
    c.start()
    c.advance(2)
    assert c.elapsed() == 2 and c.now() == 7


def test_cpu_clock_defaults_to_full_and_interpolates() -> None:
    """cpu_mhz is optional (1800 when unset) and scales the compute with the share (S7)."""
    sch = Schedule(v6_config().profile)
    assert {sch.at(t).cpu_mhz for t in (0.0, 1800.0, 3500.0)} == {1800.0}
    k = Schedule(load_config("pi4/default", "pi4-4gb").profile).at(1790.0)
    assert k.cpu_mhz < 1800.0
    assert k.compute == pytest.approx(k.cpu_share * k.cpu_mhz / 1800.0)
