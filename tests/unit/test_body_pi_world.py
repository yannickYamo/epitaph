"""The real world on the Pi (body/pi_world.py), with a fake helper and fake machine files.

The helper is never called through sudo here: a fake runner plays it against a small model of
the machine (services, radio, LEDs) that the fake unprivileged queries read back.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from epitaph.body.cgroup import CgroupBody, CgroupSettings, make_pi_world
from epitaph.body.pi_world import (
    HELPER,
    PiWorld,
    allowed_services,
    allowlist_text,
    count_processes,
    led_state,
    main,
    names,
    screen_connected,
    valid_service,
)
from epitaph.body.world import TakeResult, WorldState
from epitaph.config import CONFIG_DIR, load_config

SERVICES = ["nfs-blkmap", "rpcbind", "cron", "bluetooth", "avahi-daemon"]


class Machine:
    """The machine the fake helper changes and the fake queries read."""

    def __init__(self, leds: Path, active: Sequence[str] = SERVICES) -> None:
        self.active = set(active)
        self.radio = "enabled"
        self.leds = leds
        self.calls: list[list[str]] = []
        self.rc = 0  # what the helper exits with
        self.stuck = False  # a stop that does not stop
        self.saved: dict[str, str] = {}
        for name, trig, bright in (("ACT", "mmc0", "0"), ("PWR", "default-on", "255")):
            d = leds / name
            d.mkdir(parents=True)
            self._set(d, trig, bright)

    @staticmethod
    def _set(d: Path, trig: str, bright: str) -> None:
        names_ = ["none", "mmc0", "default-on", "heartbeat"]
        (d / "trigger").write_text(" ".join(f"[{t}]" if t == trig else t for t in names_) + "\n")
        (d / "brightness").write_text(bright + "\n")

    def runner(self, argv: Sequence[str]) -> int:
        self.calls.append(list(argv))
        if self.rc:
            return self.rc
        args = list(argv[1:])
        if args[0] == "stop" and not self.stuck:
            self.active.discard(args[1])
        elif args[0] == "start":
            self.active.add(args[1])
        elif args == ["radio", "off"]:
            self.radio = "disabled"
        elif args == ["light", "off"]:
            for d in self.leds.iterdir():
                self._set(d, "none", "0")
        elif args == ["restore"]:
            self.active = set(SERVICES)
            self.radio = "enabled"
            self._set(self.leds / "ACT", "mmc0", "0")
            self._set(self.leds / "PWR", "default-on", "255")
        return 0

    def query(self, argv: Sequence[str]) -> tuple[int, str]:
        if list(argv[:2]) == ["systemctl", "is-active"]:
            states = [
                "active" if u.removesuffix(".service") in self.active else "inactive"
                for u in argv[2:]
            ]
            return (0 if all(s == "active" for s in states) else 3), "\n".join(states) + "\n"
        if list(argv) == ["nmcli", "radio", "wifi"]:
            return (0, self.radio + "\n") if self.radio else (8, "")
        return 127, ""


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    return Machine(tmp_path / "leds")


def fake_proc(root: Path, user: int = 24, kernel: int = 100) -> Path:
    for pid in range(1, user + kernel + 1):
        d = root / str(pid)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(b"/usr/sbin/thing\x00" if pid <= user else b"")
    (root / "self").mkdir()
    (root / "meminfo").write_text("x")
    return root


def drm(root: Path, connected: bool) -> Path:
    for name, st in (("card1-HDMI-A-1", connected), ("card1-HDMI-A-2", False)):
        (root / name).mkdir(parents=True)
        (root / name / "status").write_text("connected\n" if st else "disconnected\n")
    return root


def world(machine: Machine, tmp_path: Path, screen: bool = False) -> PiWorld:
    return PiWorld(
        SERVICES,
        helper="/nonexistent/epitaph-world",
        runner=machine.runner,
        query=machine.query,
        proc=fake_proc(tmp_path / "proc"),
        leds=machine.leds,
        drm=drm(tmp_path / "drm", screen),
    )


def test_inventory_reads_the_machine(machine: Machine, tmp_path: Path) -> None:
    w = world(machine, tmp_path)
    assert w.inventory() == WorldState(
        services=tuple(SERVICES), processes=24, radio="on", light="on", screen=None
    )
    machine.active.discard("cron")
    machine.radio = "disabled"
    inv = w.inventory()
    assert inv.services == ("nfs-blkmap", "rpcbind", "bluetooth", "avahi-daemon")
    assert inv.radio == "off"
    assert machine.calls == []  # reading needs no helper


def test_take_a_service(machine: Machine, tmp_path: Path) -> None:
    w = world(machine, tmp_path)
    r = w.take("service:bluetooth")
    assert r == TakeResult("service:bluetooth", True, "bluetooth stopped")
    assert machine.calls == [["/nonexistent/epitaph-world", "stop", "bluetooth"]]
    assert "bluetooth" not in w.inventory().services
    # a loss already suffered is not a loss again, and the helper is not called
    again = w.take("service:bluetooth")
    assert not again.performed and again.detail == "not running"
    assert len(machine.calls) == 1


def test_a_service_outside_the_list_is_never_touched(machine: Machine, tmp_path: Path) -> None:
    w = world(machine, tmp_path)
    for action in ("service:ssh", "service:systemd-journald", "service:epitaph-controller"):
        r = w.take(action)
        assert not r.performed and r.detail == "not in the allowed services"
    assert machine.calls == []


def test_a_failed_or_ineffective_stop_is_not_reported(machine: Machine, tmp_path: Path) -> None:
    w = world(machine, tmp_path)
    machine.rc = 1
    r = w.take("service:cron")
    assert not r.performed and r.detail == "helper exited 1"
    assert w.failures == 1
    machine.rc = 0
    machine.stuck = True
    r = w.take("service:cron")
    assert not r.performed and r.detail == "still running after stop"


def test_radio_and_light(machine: Machine, tmp_path: Path) -> None:
    w = world(machine, tmp_path)
    assert w.take("radio:off") == TakeResult("radio:off", True, "radio off")
    assert w.take("light:off") == TakeResult("light:off", True, "light off")
    inv = w.inventory()
    assert (inv.radio, inv.light) == ("off", "off")
    assert machine.calls[-2:] == [
        ["/nonexistent/epitaph-world", "radio", "off"],
        ["/nonexistent/epitaph-world", "light", "off"],
    ]
    # taken already: not again
    assert w.take("radio:off").detail == "already off"
    assert w.take("light:off").detail == "already off"
    assert len(machine.calls) == 2


def test_radio_unknown_or_unchanged(machine: Machine, tmp_path: Path) -> None:
    w = world(machine, tmp_path)
    machine.radio = ""  # nmcli cannot answer
    r = w.take("radio:off")
    assert not r.performed and r.detail == "already absent" and machine.calls == []
    machine.radio = "enabled"
    machine.rc = 0
    real = machine.runner

    def no_effect(argv: Sequence[str]) -> int:
        machine.calls.append(list(argv))
        return 0

    w.runner = no_effect
    r = w.take("radio:off")
    assert not r.performed and r.detail == "still on after the helper"
    w.runner = real


def test_screen_needs_a_screen(machine: Machine, tmp_path: Path) -> None:
    w = world(machine, tmp_path, screen=False)
    r = w.take("screen:40")
    assert not r.performed and r.detail == "no screen connected"
    w.drm = drm(tmp_path / "drm2", True)
    r = w.take("screen:40")
    assert r.performed and "40%" in r.detail
    for bad in ("screen:140", "screen:-1", "screen:half", "screen:"):
        assert not w.take(bad).performed
    assert machine.calls == []  # the display dims the screen, not the helper


def test_unknown_actions(machine: Machine, tmp_path: Path) -> None:
    w = world(machine, tmp_path)
    for action in ("radio:on", "light:on", "memory:off", "", "service"):
        r = w.take(action)
        assert not r.performed
    assert machine.calls == []


def test_restore(machine: Machine, tmp_path: Path) -> None:
    w = world(machine, tmp_path)
    w.take("service:cron")
    w.take("radio:off")
    w.take("light:off")
    w.restore()
    assert machine.calls[-1] == ["/nonexistent/epitaph-world", "restore"]
    assert w.inventory() == WorldState(tuple(SERVICES), 24, "on", "on", None)


def test_restore_never_raises(machine: Machine, tmp_path: Path) -> None:
    w = world(machine, tmp_path)
    machine.rc = 1
    w.restore()  # logged, not raised

    def boom(argv: Sequence[str]) -> int:
        raise RuntimeError("sudo vanished")

    w.runner = boom
    w.restore()


def test_helper_not_installed(machine: Machine, tmp_path: Path) -> None:
    w = PiWorld(SERVICES, helper=str(tmp_path / "missing"), query=machine.query)
    r = w.take("service:cron")
    assert not r.performed and r.detail == "helper exited 127"
    w.restore()  # no sudo: the helper is not there


def test_protected_and_malformed_names_are_dropped() -> None:
    w = PiWorld(
        ["cron", "ssh", "systemd-journald", "../x", "cron", "-f", "getty@tty1", "bluetooth"]
    )
    assert w.services == ("cron", "bluetooth")
    for name in ("NetworkManager", "dbus", "epitaph-display", "systemd-timesyncd", "udev"):
        assert not valid_service(name)
    assert valid_service("avahi-daemon")
    assert PiWorld([]).running_services() == ()


def test_names() -> None:
    assert names(["a", 1]) == ["a", "1"]
    assert names("cron") == [] and names(None) == []


def test_count_processes(tmp_path: Path) -> None:
    assert count_processes(fake_proc(tmp_path / "p", user=7, kernel=3)) == 7
    assert count_processes(tmp_path / "absent") == 0


def test_led_state(tmp_path: Path) -> None:
    assert led_state(tmp_path / "none") is None
    m = Machine(tmp_path / "leds")
    assert led_state(m.leds) == "on"
    Machine._set(m.leds / "PWR", "none", "0")
    assert led_state(m.leds) == "on"  # ACT still follows the card
    Machine._set(m.leds / "ACT", "none", "0")
    assert led_state(m.leds) == "off"
    Machine._set(m.leds / "ACT", "none", "1")
    assert led_state(m.leds) == "on"


def test_screen_connected(tmp_path: Path) -> None:
    assert not screen_connected(tmp_path / "nothing")
    assert screen_connected(drm(tmp_path / "a", True))
    assert not screen_connected(drm(tmp_path / "b", False))


def test_from_config_and_the_pi_overlay() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    w = PiWorld.from_config(cfg)
    assert w.helper == HELPER
    assert w.services == tuple(SERVICES)
    assert isinstance(make_pi_world(cfg), PiWorld)
    assert make_pi_world(load_config("pi4/default", "dev")) is None


def test_allowlist_from_the_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert allowed_services(CONFIG_DIR, "pi4-4gb") == SERVICES
    assert allowed_services(CONFIG_DIR, "dev") == []
    text = allowlist_text(SERVICES)
    lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
    assert lines == SERVICES
    assert main(["allowlist", "--hardware", "pi4-4gb"]) == 0
    assert capsys.readouterr().out == text


def test_allowlist_drops_protected_names(tmp_path: Path) -> None:
    (tmp_path / "hardware").mkdir()
    (tmp_path / "default.toml").write_text('[life]\nhardware = "x"\n[world]\nservices = []\n')
    (tmp_path / "hardware" / "x.toml").write_text(
        '[world]\nhelper = "/usr/local/sbin/epitaph-world"\n'
        'services = ["cron", "ssh", "NetworkManager", "cron", "bluetooth"]\n'
    )
    assert allowed_services(tmp_path) == ["cron", "bluetooth"]


def test_the_body_restores_the_world_at_every_reset(tmp_path: Path) -> None:
    rel = "system.slice/epitaph-controller.service"
    fs = tmp_path / "cgroup"
    root = fs / rel
    root.mkdir(parents=True)
    (root / "cgroup.procs").write_text("4242\n")
    (root / "cgroup.controllers").write_text("cpuset cpu io memory pids\n")
    (root / "cgroup.subtree_control").write_text("\n")
    restored: list[int] = []

    class Spy:
        def inventory(self) -> WorldState:
            return WorldState()

        def take(self, action: str) -> TakeResult:
            return TakeResult(action, False)

        def restore(self) -> None:
            restored.append(1)

    body = CgroupBody(root, replace(CgroupSettings(), creature_cpus="1-3"), world=Spy(), fs=fs)
    body.setup()  # the controller's start
    assert restored == [1]
    body.reset_creature_cgroup()  # a death
    assert restored == [1, 1]
