"""The thermal pause hook and the heat readings."""

from __future__ import annotations

from pathlib import Path

from epitaph.body.cgroup import CgroupSettings, PlainBody
from epitaph.body.fake import FakeBody
from epitaph.body.thermal import ThermalGuard, ThermalSettings
from epitaph.body.vitals import SysPaths
from epitaph.config import load_config


def guard(temps: list[float | None], bits: int | None = 0) -> ThermalGuard:
    it = iter(temps)
    settings = ThermalSettings(80.0, 75.0, 15.0)
    return ThermalGuard(settings, temp=lambda: next(it), throttled=lambda: bits)


def test_no_pause_at_normal_temperatures() -> None:
    g = guard([40.0, 57.0, 79.9])  # the Pi 4's range under load (S1c, S7), and just below
    assert [g.pause_s() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert not g.paused and g.pauses == 0


def test_pause_with_hysteresis() -> None:
    g = guard([80.0, 78.0, 75.1, 75.0, 79.0, 81.5, 70.0])
    assert [g.pause_s() for _ in range(7)] == [15.0, 15.0, 15.0, 0.0, 0.0, 15.0, 0.0]
    assert g.pauses == 2 and not g.paused


def test_unreadable_temperature_never_pauses() -> None:
    g = guard([85.0, None, 85.0])
    assert g.pause_s() == 15.0
    assert g.pause_s() == 0.0 and not g.paused
    assert g.pause_s() == 15.0 and g.pauses == 2


def test_state_reports_the_firmware_bits() -> None:
    g = guard([82.0], bits=0x50005)
    g.pause_s()
    st = g.state()
    assert st["cpu_c"] == 82.0 and st["thermal_paused"] and st["thermal_pauses"] == 1
    assert st["throttling_now"] and st["undervolt_now"]
    assert "under-voltage occurred" in st["throttled_flags"]
    quiet = guard([50.0, 50.0], bits=None).state()
    assert quiet["throttled"] is None and quiet["throttled_flags"] == []
    assert not quiet["throttling_now"] and quiet["cpu_c"] == 50.0


def test_settings_from_config() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    assert ThermalSettings.from_config(cfg) == ThermalSettings(80.0, 75.0, 15.0)
    cfg.data["body"]["thermal_limit_c"] = 70
    del cfg.data["body"]["thermal_resume_c"]
    assert ThermalSettings.from_config(cfg).resume_c == 65.0
    cfg.data["body"]["thermal_resume_c"] = 90  # never above the limit
    assert ThermalSettings.from_config(cfg).resume_c == 70.0
    assert CgroupSettings.from_config(cfg).thermal.limit_c == 70.0


def test_bodies_expose_the_hook(tmp_path: Path) -> None:
    (tmp_path / "temp").write_text("52100\n")
    paths = SysPaths(thermal=tmp_path / "temp", throttled_sysfs=tmp_path / "thr")
    (tmp_path / "thr").write_text("0\n")
    plain = PlainBody(CgroupSettings(creature_cpus=""), paths)
    assert plain.thermal_pause_s() == 0.0
    (tmp_path / "temp").write_text("83000\n")
    assert plain.thermal_pause_s() == 15.0
    assert plain.thermal.state()["throttled"] == 0
    fake = FakeBody()
    assert fake.thermal_pause_s() == 0.0
    fake.thermal_pause = 15.0
    assert fake.thermal_pause_s() == 15.0
