from __future__ import annotations

from epitaph.clock import FakeClock, Schedule
from epitaph.config import Config, load_config
from epitaph.types import Health


def test_birth_values(pi4_default: Config) -> None:
    k = Schedule(pi4_default.profile).at(0)
    assert (k.step, k.threads, k.recall, k.persona_groups, k.health) == (
        0,
        3,
        1280,
        5,
        Health.NOMINAL,
    )
    assert not k.death_squeeze


def test_recall_and_cpu_share_are_cut_at_the_reload(pi4_default: Config) -> None:
    s = Schedule(pi4_default.profile)
    assert s.at(27.9 * 60).recall == 1000
    assert s.at(27.9 * 60).cpu_share == 3.0
    k = s.at(28 * 60)
    # Rebased on measured Pi 4 costs (docs/PROFILES.md): recall 300 after reload 1, and the
    # thread drop moved to reload 2.
    assert (k.recall, k.step, k.threads, k.cpu_share) == (300, 1, 3, 3.0)
    k2 = s.at(43 * 60)
    assert (k2.recall, k2.step, k2.threads, k2.cpu_share) == (200, 2, 2, 2.0)


def test_interpolation_between_keyframes(pi4_default: Config) -> None:
    s = Schedule(pi4_default.profile)
    mid = s.at(32 * 60)
    assert 260 < mid.recall < 300
    assert 0.85 < mid.temperature < 1.0


def test_stepped_fields_hold(pi4_default: Config) -> None:
    s = Schedule(pi4_default.profile)
    assert s.at(48.9 * 60).persona_groups == 5
    assert s.at(49 * 60).persona_groups == 4


def test_v6_timeline(pi4_default: Config) -> None:
    s = Schedule(pi4_default.profile)
    assert s.reload_times() == [1680, 2580]
    assert s.erosion_times() == [2940, 3060, 3180, 3300, 3420]
    assert s.death_s == 3570
    assert s.at(3571).death_squeeze


def test_end_anchors_survive_rescale() -> None:
    cfg = load_config("pi4/compressed-2700", "pi4-4gb")
    s = Schedule(cfg.profile)
    assert s.erosion_times()[-1] == 2700 - 180
    assert s.death_s == 2670


def test_fake_clock() -> None:
    c = FakeClock()
    c.advance(5)
    c.start()
    c.advance(2)
    assert c.elapsed() == 2 and c.now() == 7
