"""The piece runs with no network at all (the offline round; docs/INSTALLATION.md "Offline").

Static checks of what boots on the Pi (units, udev rule, install.sh) and of the parts that
must not depend on a correct wall clock: the Pi has no RTC, so offline its clock starts from
the last saved time and may jump when NTP returns. No network, no real time.
"""

from __future__ import annotations

import configparser
import re
import time
from pathlib import Path

import pytest

from epitaph.clock import RealClock
from epitaph.display.layout import life_times
from epitaph.exhibit import Exhibit, Hours

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
INSTALL = DEPLOY / "install.sh"
UNITS = sorted((DEPLOY / "systemd").iterdir())


def unit(path: Path) -> configparser.RawConfigParser:
    p = configparser.RawConfigParser(strict=False, interpolation=None)
    p.optionxform = str  # type: ignore[assignment,method-assign]
    p.read_string(path.read_text())
    return p


# -- boot without a network ------------------------------------------------------------------


@pytest.mark.parametrize("path", UNITS, ids=[p.name for p in UNITS])
def test_no_unit_waits_for_a_network(path: Path) -> None:
    section = unit(path)["Unit"]
    for key in ("After", "Wants", "Requires", "BindsTo", "Requisite"):
        assert "network" not in section.get(key, ""), f"{path.name}: {key}={section[key]}"


def test_controller_is_ordered_after_the_time_restore_but_never_waits_for_it() -> None:
    section = unit(DEPLOY / "systemd" / "epitaph-controller.service")["Unit"]
    assert section["After"].split() == ["time-sync.target"]
    for key in ("Wants", "Requires", "BindsTo", "Requisite"):
        assert key not in section


def test_install_fails_if_time_wait_sync_would_hold_the_controller() -> None:
    text = INSTALL.read_text()
    assert "systemctl is-enabled --quiet systemd-time-wait-sync.service" in text
    assert re.search(r'fail "systemd-time-wait-sync\.service is enabled', text)


def test_install_reports_how_the_clock_survives_a_reboot() -> None:
    text = INSTALL.read_text()
    assert "fake-hwclock" in text and "systemd-timesyncd" in text
    assert 'note "clock across offline reboots' in text


# -- the screen hot-plug ----------------------------------------------------------------------


def test_udev_rule_queues_the_hotplug_unit_on_drm_change() -> None:
    rules = [
        line
        for line in (DEPLOY / "udev" / "90-epitaph-display.rules").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert rules == [
        'SUBSYSTEM=="drm", ACTION=="change", KERNEL=="card[0-9]*", '
        'RUN+="/usr/bin/systemctl --no-block start epitaph-display-hotplug.service"'
    ]


def test_hotplug_units() -> None:
    svc = unit(DEPLOY / "systemd" / "epitaph-display-hotplug.service")
    s = svc["Service"]
    assert s["Type"] == "oneshot"
    assert s["ExecStart"] == "/usr/local/sbin/epitaph-display-hotplug"
    assert s["RuntimeDirectoryPreserve"] == "yes" and s["CPUAffinity"] == "0"
    assert "Install" not in svc  # started by the udev rule and the timer only
    timer = unit(DEPLOY / "systemd" / "epitaph-display-hotplug.timer")
    assert timer["Timer"]["OnUnitActiveSec"] == "1min"
    assert timer["Install"]["WantedBy"] == "timers.target"


def test_display_unit_runs_on_the_console_without_a_network() -> None:
    s = unit(DEPLOY / "systemd" / "epitaph-display.service")["Service"]
    env = s["Environment"]
    assert "SDL_VIDEODRIVER=kmsdrm" in env
    assert "DISPLAY=" not in env and "WAYLAND_DISPLAY" not in env
    assert {"video", "render"} <= set(s["SupplementaryGroups"].split())
    assert "--connect" not in s["ExecStart"]  # the local bus on 127.0.0.1


def test_install_puts_the_hotplug_pieces_in_place() -> None:
    text = INSTALL.read_text()
    assert "deploy/sbin/epitaph-display-hotplug" in text
    assert "deploy/udev/90-epitaph-display.rules" in text
    assert "udevadm control --reload" in text
    enabled = re.search(r"^ENABLE_UNITS=\((.*)\)$", text, flags=re.M)
    assert enabled is not None
    assert enabled.group(1).split() == [
        "epitaph-controller.service",
        "epitaph-display.service",
        "epitaph-display-hotplug.timer",
    ]
    units = re.search(r"^UNITS=\(([^)]*)\)", text, flags=re.M)
    assert units is not None
    assert sorted(units.group(1).split()) == sorted(p.name for p in UNITS)


# -- nothing depends on the wall clock ---------------------------------------------------------


def test_the_life_clock_ignores_wall_clock_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    mono = [1000.0]
    wall = [1.7e9]
    monkeypatch.setattr(time, "monotonic", lambda: mono[0])
    monkeypatch.setattr(time, "time", lambda: wall[0])
    c = RealClock()
    c.start()
    mono[0] += 60
    wall[0] -= 5400  # NTP comes back and steps the clock 90 minutes back
    assert c.elapsed() == 60
    mono[0] += 30
    wall[0] += 86400 * 365  # or a year forward
    assert c.elapsed() == 90


def test_the_screen_follows_the_life_clock_not_the_wall_clock() -> None:
    events = [
        {"type": "birth", "t": 0.0, "ts": 1_000_000.0},
        {"type": "thought", "t": 30.0, "ts": 1_000_030.0},
        {"type": "thought", "t": 60.0, "ts": 994_000.0},  # the wall clock stepped back
        {"type": "thought", "t": 90.0, "ts": 2_000_000.0},  # and forward
    ]
    assert life_times(events) == [0.0, 30.0, 60.0, 90.0]


def test_hours_stay_on_while_the_restored_clock_is_unsynced() -> None:
    # Offline the clock is the last saved time, possibly the middle of the night: still on.
    ex = Exhibit(Hours(600, 1080), "pause", synced=lambda: False, mono=lambda: 0.0)
    assert ex.is_open() and ex.seconds_to_open() == 0.0
    assert ex.describe()["time_synced"] is False
