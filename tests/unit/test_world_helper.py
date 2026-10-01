"""deploy/sbin/epitaph-world, run unprivileged: its argument checks as installed, and its
behaviour in a sandbox copy whose paths and PATH point at fakes (systemctl, nmcli, the LEDs).

Nothing here runs as root or through sudo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "deploy" / "sbin" / "epitaph-world"

pytestmark = pytest.mark.skipif(shutil.which("flock") is None, reason="no flock")

FAKE_SYSTEMCTL = """#!/bin/sh
# fake systemctl: a unit is active while $STATE/active/<unit> exists
while [ "$1" = --no-block ] || [ "$1" = -q ]; do shift; done
cmd=$1; shift
case "$cmd" in
  is-active) [ "$1" = --quiet ] && shift; [ -e "$STATE/active/$1" ] ;;
  cat) [ -e "$STATE/units/$1" ] ;;
  stop) for u in "$@"; do rm -f "$STATE/active/$u"; done; echo "stop $*" >> "$STATE/log" ;;
  start)
    for u in "$@"; do [ -e "$STATE/masked/$u" ] && exit 1; touch "$STATE/active/$u"; done
    echo "start $*" >> "$STATE/log" ;;
  mask) [ "$1" = --runtime ] || exit 1; shift
    for u in "$@"; do touch "$STATE/masked/$u"; done; echo "mask $*" >> "$STATE/log" ;;
  unmask) [ "$1" = --runtime ] || exit 1; shift
    for u in "$@"; do rm -f "$STATE/masked/$u"; done; echo "unmask $*" >> "$STATE/log" ;;
  *) exit 1 ;;
esac
"""

FAKE_NMCLI = """#!/bin/sh
# fake nmcli radio wifi [on|off]
[ "$1 $2" = "radio wifi" ] || exit 2
case "${3:-}" in
  "") cat "$STATE/radio" ;;
  on) echo enabled > "$STATE/radio" ;;
  off) echo disabled > "$STATE/radio" ;;
  *) exit 2 ;;
esac
"""


def run(
    helper: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(helper), *args], capture_output=True, text=True, check=False, env=env
    )


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["stop"],
        ["stop", "cron", "now"],
        ["stop", "../etc/passwd"],
        ["stop", "-f"],
        ["stop", "cron;reboot"],
        ["kill", "cron"],
        ["radio"],
        ["radio", "maybe"],
        ["light", "on"],
        ["restore", "all"],
        ["--help"],
    ],
)
def test_installed_helper_refuses_bad_arguments(args: list[str]) -> None:
    out = run(HELPER, *args)
    assert out.returncode == 2, out.stdout + out.stderr


@pytest.mark.parametrize(
    "name", ["ssh", "systemd-journald", "NetworkManager", "epitaph-controller"]
)
def test_installed_helper_refuses_protected_services(name: str) -> None:
    out = run(HELPER, "stop", name)
    assert out.returncode == 2 and "protected" in out.stderr


class Sandbox:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.state = tmp / "state"
        for d in ("active", "units", "masked"):
            (self.state / d).mkdir(parents=True)
        for unit in ("cron.service", "bluetooth.service", "rpcbind.service", "rpcbind.socket"):
            (self.state / "active" / unit).touch()
            (self.state / "units" / unit).touch()
        (self.state / "radio").write_text("enabled\n")
        bin_ = tmp / "bin"
        bin_.mkdir()
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("nmcli", FAKE_NMCLI)):
            (bin_ / name).write_text(body)
            (bin_ / name).chmod(0o755)
        self.leds = tmp / "leds"
        for name, trig, bright in (("ACT", "mmc0", "0"), ("PWR", "default-on", "255")):
            self.set_led(name, trig, bright)
        self.allow = tmp / "etc" / "world-services"
        self.allow.parent.mkdir()
        self.allow.write_text("# header\ncron\nbluetooth  # comment\nrpcbind\nssh\n")
        self.allow.chmod(0o644)
        text = HELPER.read_text()
        for old, new in (
            ("PATH=/usr/sbin:/usr/bin:/sbin:/bin", f"PATH={bin_}:/usr/bin:/bin"),
            ("ALLOW=/etc/epitaph/world-services", f"ALLOW={self.allow}"),
            ("RUN=/run/epitaph-world", f"RUN={tmp / 'run'}"),
            ("KEEP=/var/lib/epitaph-world", f"KEEP={tmp / 'keep'}"),
            ("LEDS=/sys/class/leds", f"LEDS={self.leds}"),
            ("LOCK=/run/lock/epitaph-world.lock", f"LOCK={tmp / 'lock'}"),
            ('[ "$owner" != 0 ]', f'[ "$owner" != {os.getuid()} ]'),
        ):
            assert old in text, old
            text = text.replace(old, new)
        self.helper = tmp / "epitaph-world"
        self.helper.write_text(text)
        self.env = {"STATE": str(self.state), "PATH": "/usr/bin:/bin"}

    def set_led(self, name: str, trig: str, bright: str) -> None:
        d = self.leds / name
        d.mkdir(parents=True, exist_ok=True)
        opts = ["none", "mmc0", "default-on", "heartbeat"]
        (d / "trigger").write_text(" ".join(f"[{t}]" if t == trig else t for t in opts) + "\n")
        (d / "brightness").write_text(bright + "\n")

    def led(self, name: str) -> tuple[str, str]:
        trig = (self.leds / name / "trigger").read_text()
        trig = trig.split("[", 1)[1].split("]", 1)[0] if "[" in trig else trig.strip()
        return trig, (self.leds / name / "brightness").read_text().strip()

    def active(self) -> set[str]:
        return {p.name for p in (self.state / "active").iterdir()}

    def masked(self) -> set[str]:
        return {p.name for p in (self.state / "masked").iterdir()}

    def taken(self) -> list[str]:
        f = self.tmp / "run" / "taken"
        return f.read_text().splitlines() if f.exists() else []

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run(self.helper, *args, env=self.env)


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def test_stop_start_and_restore_services(box: Sandbox) -> None:
    assert box("stop", "cron").returncode == 0
    assert "cron.service" not in box.active()
    # masked for the runtime too: D-Bus or socket activation cannot bring it back
    assert box.masked() == {"cron.service"}
    # a socket-activated service loses its socket too, so nothing starts it again
    assert box("stop", "rpcbind").returncode == 0
    assert not {"rpcbind.service", "rpcbind.socket"} & box.active()
    assert box.taken() == ["cron 1 cron.service", "rpcbind 1 rpcbind.socket rpcbind.service"]
    assert box("stop", "cron").returncode == 0  # taken again: the first record stays
    assert box.taken()[0] == "cron 1 cron.service"
    assert box("start", "cron").returncode == 0
    assert "cron.service" in box.active() and "cron.service" not in box.masked()
    assert box.taken() == ["rpcbind 1 rpcbind.socket rpcbind.service"]
    out = box("restore")
    assert out.returncode == 0, out.stderr
    assert {"rpcbind.service", "rpcbind.socket", "cron.service"} <= box.active()
    assert box.masked() == set() and box.taken() == []


def test_a_service_stopped_before_is_not_started_by_restore(box: Sandbox) -> None:
    (box.state / "active" / "bluetooth.service").unlink()  # the owner's choice
    assert box("stop", "bluetooth").returncode == 0
    assert box.taken() == ["bluetooth 0 bluetooth.service"]
    assert box("restore").returncode == 0
    assert "bluetooth.service" not in box.active()
    assert box.masked() == set()  # unmasked all the same


def test_only_the_allowlist(box: Sandbox) -> None:
    out = box("stop", "avahi-daemon")
    assert out.returncode == 2 and "not in" in out.stderr
    # listed, but protected: refused anyway
    assert box("stop", "ssh").returncode == 2
    box.allow.chmod(0o666)
    out = box("stop", "cron")
    assert out.returncode == 2 and "root only" in out.stderr
    assert "cron.service" in box.active()


def test_radio(box: Sandbox) -> None:
    assert box("radio", "off").returncode == 0
    assert (box.state / "radio").read_text().strip() == "disabled"
    assert box("radio", "off").returncode == 0  # the saved state stays the first one
    assert (box.tmp / "keep" / "radio").read_text().strip() == "enabled"
    assert box("restore").returncode == 0
    assert (box.state / "radio").read_text().strip() == "enabled"
    assert not (box.tmp / "keep" / "radio").exists()
    assert box("radio", "off").returncode == 0
    assert box("radio", "on").returncode == 0
    assert not (box.tmp / "keep" / "radio").exists()


def test_radio_kept_off_by_the_owner_stays_off(box: Sandbox) -> None:
    (box.state / "radio").write_text("disabled\n")
    assert box("radio", "off").returncode == 0
    assert box("restore").returncode == 0
    assert (box.state / "radio").read_text().strip() == "disabled"


def test_light_off_and_back(box: Sandbox) -> None:
    assert box("light", "off").returncode == 0
    assert box.led("ACT") == ("none", "0") and box.led("PWR") == ("none", "0")
    assert box("light", "off").returncode == 0  # the saved state stays the first one
    assert box("light", "restore").returncode == 0
    # triggers back; a triggered LED's brightness is left to its trigger
    assert box.led("ACT") == ("mmc0", "0") and box.led("PWR")[0] == "default-on"
    box.set_led("PWR", "none", "1")
    assert box("light", "off").returncode == 0
    assert box("restore").returncode == 0
    assert box.led("PWR") == ("none", "1")


def test_light_without_leds(box: Sandbox) -> None:
    shutil.rmtree(box.leds)
    assert box("light", "off").returncode == 0
    assert box("restore").returncode == 0


def test_restore_with_nothing_taken(box: Sandbox) -> None:
    out = box("restore")
    assert out.returncode == 0 and "epitaph-world: restore" in out.stdout
    assert not (box.state / "log").exists()  # nothing started or stopped
