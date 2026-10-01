"""deploy/sbin/epitaph-display-hotplug against a fake sysfs, systemctl and epitaph.

The helper runs as root on the Pi, started by the udev rule and the timer; here `systemctl`
keeps unit state in files, `epitaph display --screen-present` answers from a file, and the DRM
connectors are a folder. No Pi, no screen, no real systemd.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOTPLUG = ROOT / "deploy" / "sbin" / "epitaph-display-hotplug"

SYSTEMCTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_DIR/systemctl.log"
cmd="$1"; shift
unit=""
for a in "$@"; do case "$a" in -*) ;; *) unit="$a" ;; esac; done
case "$cmd" in
  is-enabled) [ -f "$FAKE_DIR/$unit.enabled" ] ;;
  is-active)
    if [ -f "$FAKE_DIR/$unit.active" ]; then echo active; exit 0; fi
    echo inactive; exit 3 ;;
  start|restart) touch "$FAKE_DIR/$unit.active" ;;
  stop) rm -f "$FAKE_DIR/$unit.active" ;;
esac
"""

EPITAPH = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_DIR/epitaph.log"
if [ "$(cat "$FAKE_DIR/answer")" = yes ]; then echo "screen: yes (drm: card1-HDMI-A-1)"; exit 0; fi
echo "screen: no (drm: no connector connected)"; exit 1
"""

DISPLAY = "epitaph-display.service"
CONTROLLER = "epitaph-controller.service"


class Pi:
    """The fake machine: connectors, unit states and what `--screen-present` answers."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.drm = root / "drm"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, body in (("systemctl", SYSTEMCTL), ("epitaph", EPITAPH)):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_DIR": str(root),
            "EPITAPH_BIN": str(bin_dir / "epitaph"),
            "EPITAPH_DRM": str(self.drm),
            "EPITAPH_HOTPLUG_STATE": str(root / "state"),
            # the fakes first: never the laptop's own systemctl
            "EPITAPH_HOTPLUG_PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        }
        self.connector("card1-HDMI-A-1", "disconnected")
        self.connector("card1-HDMI-A-2", "disconnected")
        self.connector("card1-Writeback-1", "unknown")
        self.unit(DISPLAY, enabled=True)
        self.unit(CONTROLLER, enabled=True, active=True)

    def connector(self, name: str, status: str) -> None:
        d = self.drm / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "status").write_text(status + "\n")

    def plug(self, connected: bool) -> None:
        self.connector("card1-HDMI-A-1", "connected" if connected else "disconnected")
        (self.root / "answer").write_text("yes" if connected else "no")

    def unit(self, name: str, *, enabled: bool | None = None, active: bool | None = None) -> None:
        for flag, value in (("enabled", enabled), ("active", active)):
            if value is not None:
                f = self.root / f"{name}.{flag}"
                f.touch() if value else f.unlink(missing_ok=True)

    def active(self, name: str) -> bool:
        return (self.root / f"{name}.active").exists()

    def calls(self, log: str) -> list[str]:
        p = self.root / f"{log}.log"
        lines = p.read_text().splitlines() if p.exists() else []
        p.unlink(missing_ok=True)
        return lines

    def run(self) -> subprocess.CompletedProcess[str]:
        res = subprocess.run(
            ["sh", str(HOTPLUG)], env=self.env, capture_output=True, text=True, timeout=30
        )
        assert res.returncode == 0, res.stderr
        return res


@pytest.fixture
def pi(tmp_path: Path) -> Pi:
    p = Pi(tmp_path)
    p.plug(False)
    return p


def actions(p: Pi) -> list[str]:
    return [c for c in p.calls("systemctl") if c.split()[0] in ("start", "stop", "restart")]


def test_installed_root_executable() -> None:
    assert HOTPLUG.stat().st_mode & 0o111
    assert subprocess.run(["sh", "-n", str(HOTPLUG)], check=False).returncode == 0


def test_a_disabled_display_stays_off(pi: Pi) -> None:
    pi.unit(DISPLAY, enabled=False)
    pi.plug(True)
    assert pi.run().stdout == ""
    assert actions(pi) == [] and pi.calls("epitaph") == []


def test_a_screen_plugged_in_after_boot_starts_the_display(pi: Pi) -> None:
    pi.run()  # the first run after a headless boot: nothing to do
    assert actions(pi) == []
    pi.plug(True)
    res = pi.run()
    assert actions(pi) == [f"start --no-block {DISPLAY}"]
    assert "screen: yes" in res.stdout and "starting" in res.stdout
    assert pi.calls("epitaph") == ["display --screen-present", "display --screen-present"]


def test_no_change_costs_no_python_and_does_nothing(pi: Pi) -> None:
    pi.plug(True)
    pi.run()
    pi.calls("epitaph")
    pi.calls("systemctl")
    for _ in range(3):  # the timer, every minute
        assert pi.run().stdout == ""
    assert pi.calls("epitaph") == []
    assert actions(pi) == []


def test_the_display_started_at_boot_is_left_alone(pi: Pi) -> None:
    pi.plug(True)
    pi.unit(DISPLAY, active=True)
    assert pi.run().stdout == ""
    assert actions(pi) == []


def test_unplug_stops_the_display_and_a_replug_starts_it_fresh(pi: Pi) -> None:
    pi.plug(True)
    pi.unit(DISPLAY, active=True)
    pi.run()
    pi.plug(False)
    res = pi.run()
    assert actions(pi) == [f"stop --no-block {DISPLAY}"]
    assert "stopping" in res.stdout
    pi.plug(True)
    pi.run()
    assert actions(pi) == [f"start --no-block {DISPLAY}"]


def test_a_display_still_running_after_an_unplug_restarts_on_replug(pi: Pi) -> None:
    pi.run()  # remembered: no screen
    pi.unit(DISPLAY, active=True)  # started meanwhile (by hand, or a forced screen)
    pi.plug(True)
    res = pi.run()
    assert actions(pi) == [f"restart --no-block {DISPLAY}"]
    assert "fresh mode" in res.stdout


def test_no_screen_never_starts_the_display(pi: Pi) -> None:
    """No crash loop: an unplugged Pi only ever stops the display, it never starts it."""
    for status in ("disconnected", "unknown", "disconnected"):
        pi.connector("card1-HDMI-A-2", status)
        pi.run()
    assert actions(pi) == []
    assert not pi.active(DISPLAY)


def test_a_stopped_controller_is_not_started_through_the_display(pi: Pi) -> None:
    pi.unit(CONTROLLER, active=False)  # stopped on purpose (a smoke run, maintenance)
    pi.plug(True)
    res = pi.run()
    assert actions(pi) == []
    assert "is not running" in res.stdout
    # Not remembered: once the controller runs again, the next timer run starts the display.
    pi.unit(CONTROLLER, active=True)
    pi.run()
    assert actions(pi) == [f"start --no-block {DISPLAY}"]


def test_writeback_connectors_are_not_screens(pi: Pi) -> None:
    pi.run()
    pi.calls("epitaph")
    pi.connector("card1-Writeback-1", "connected")
    pi.run()
    assert pi.calls("epitaph") == []
    assert actions(pi) == []


def test_no_drm_at_all(pi: Pi, tmp_path: Path) -> None:
    pi.env["EPITAPH_DRM"] = str(tmp_path / "nowhere")
    pi.run()
    assert actions(pi) == []
