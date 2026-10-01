# pyright: strict
"""The real world on the Pi: services, the radio and the lights, taken for real (the dread plan).

The controller runs unprivileged. Stopping a service, switching the Wi-Fi radio and the board's
LEDs need root: the root-owned helper `/usr/local/sbin/epitaph-world`
(deploy/sbin/epitaph-world) does exactly these and nothing else, and a sudoers drop-in lets the
service user run it without a password; deploy/install.sh installs both, and the allowed
services in `/etc/epitaph/world-services` from `[world] services` (`python -m
epitaph.body.pi_world allowlist`). The helper keeps what it changed and `restore` puts it back:
the body calls it at every death and at every controller start, and the controller unit at
every start and stop (ExecStartPre, ExecStopPost).

Reading what is there needs no privilege: `systemctl is-active`, `nmcli radio wifi`, /proc and
/sys/class/leds. The screen's brightness belongs to the display (it dims on the `world` event);
the body cannot read it and reports None.

Truth rule: a loss is reported performed only when the machine shows it afterwards (the service
inactive, the radio off, the lights off) or, for the screen, when a screen is connected for the
display to dim. Anything else is performed=False and its reading is left out.

`take` and `restore` block on sudo: on the Pi 4 a service takes about 1 s (the runtime mask
reloads systemd), the radio 0.8 s, the lights 0.1 s, a full restore 1.2 s; the limit is 30 s.
The controller calls them off its event loop, and a reading follows the loss when the machine
shows it.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from epitaph.body.world import OnOff, TakeResult, WorldState

if TYPE_CHECKING:
    from epitaph.config import Config

log = logging.getLogger(__name__)

HELPER = "/usr/local/sbin/epitaph-world"
LED_NAMES = ("ACT", "PWR")
LEDS = Path("/sys/class/leds")
PROC = Path("/proc")
DRM = Path("/sys/class/drm")
# Never offered to a life, whatever the config says (the helper refuses them too): the machine
# must stay reachable, keep its time and its journal, and keep running the piece.
PROTECTED = re.compile(
    r"^(systemd.*|dbus.*|ssh|sshd|NetworkManager.*|wpa_supplicant|epitaph.*|.*getty.*|"
    r"user@.*|user-runtime-dir@.*|polkit|udev.*|init)$"
)
SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._-]*$")

# Runs argv under sudo and returns its exit status; injected so that tests never call sudo.
Runner = Callable[[Sequence[str]], int]
# Runs argv unprivileged: (exit status, stdout). Injected in tests.
Query = Callable[[Sequence[str]], tuple[int, str]]


def sudo_runner(argv: Sequence[str]) -> int:
    """Run argv under `sudo -n` (never prompts), with a 30 s limit; 127 if it cannot start."""
    try:
        res = subprocess.run(
            ["sudo", "-n", *argv], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.error("world helper did not run: %s", e)
        return 127
    if res.returncode != 0:
        log.error("world helper %s exited %d: %s", argv, res.returncode, res.stderr.strip())
    return res.returncode


def plain_query(argv: Sequence[str]) -> tuple[int, str]:
    """Run argv as this user with a 5 s limit: (exit status, stdout); 127 if it cannot run."""
    try:
        res = subprocess.run(list(argv), capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return res.returncode, res.stdout


def names(value: object) -> list[str]:
    """A config list of names as strings; anything that is not a list is no names."""
    if not isinstance(value, list):
        return []
    return [str(v) for v in cast("list[object]", value)]


def valid_service(name: str) -> bool:
    """A plain unit name (no suffix, no path, no option) that is not a protected one."""
    return bool(SERVICE_NAME.match(name)) and not PROTECTED.match(name)


def count_processes(proc: Path = PROC) -> int:
    """User-space processes: /proc entries with a command line (kernel threads have none)."""
    n = 0
    try:
        entries = list(proc.iterdir())
    except OSError:
        return 0
    for d in entries:
        if not d.name.isdigit():
            continue
        try:
            if (d / "cmdline").read_bytes():
                n += 1
        except OSError:
            continue  # exited meanwhile
    return n


def led_state(leds: Path = LEDS, names: Sequence[str] = LED_NAMES) -> OnOff | None:
    """ "off" when every board LED is dark with no trigger, "on" otherwise, None without LEDs."""
    found = False
    for name in names:
        d = leds / name
        try:
            trigger = (d / "trigger").read_text()
            brightness = int((d / "brightness").read_text().strip() or "0")
        except (OSError, ValueError):
            continue
        found = True
        m = re.search(r"\[([^\]]+)\]", trigger)
        active = m.group(1) if m else "none"
        if active != "none" or brightness > 0:
            return "on"
    return "off" if found else None


def screen_connected(drm: Path = DRM) -> bool:
    """Whether a DRM connector (HDMI, DSI, ...) reports a connected screen."""
    try:
        return any(
            (s.read_text().strip() == "connected") for s in sorted(drm.glob("card*-*/status"))
        )
    except OSError:
        return False


class PiWorld:
    """The World on the Pi, through the root helper (writes) and plain reads (inventory)."""

    def __init__(
        self,
        services: Sequence[str],
        helper: str = HELPER,
        runner: Runner | None = None,
        query: Query | None = None,
        proc: Path = PROC,
        leds: Path = LEDS,
        drm: Path = DRM,
    ) -> None:
        """`services` are the names a life may lose (protected and malformed ones are dropped).

        `runner` calls the helper (default: sudo_runner) and `query` runs the unprivileged
        reads (default: plain_query); tests pass fakes for both, and paths laid out like /proc,
        /sys/class/leds and /sys/class/drm.
        """
        kept: list[str] = []
        for s in services:
            if valid_service(s) and s not in kept:
                kept.append(s)
            elif not valid_service(s):
                log.warning("world: %r is not a service a life may lose; ignored", s)
        self.services = tuple(kept)
        self.helper = helper
        self.runner = runner
        self.query = query or plain_query
        self.proc = proc
        self.leds = leds
        self.drm = drm
        self.failures = 0

    @classmethod
    def from_config(cls, cfg: Config) -> PiWorld:
        """`[world] services` and `[world] helper` (the hardware overlay names the helper)."""
        world = cfg.section("world")
        return cls(
            names(world.get("services")),
            helper=str(world.get("helper", "") or HELPER),
        )

    # --- reading -----------------------------------------------------------------------

    def running_services(self) -> tuple[str, ...]:
        """The configured services that are active now, in the configured order."""
        if not self.services:
            return ()
        _, out = self.query(["systemctl", "is-active", *(f"{s}.service" for s in self.services)])
        states = out.split()
        return tuple(
            s for s, st in zip(self.services, states, strict=False) if st in ("active", "reloading")
        )

    def radio(self) -> OnOff | None:
        """The Wi-Fi radio: "on", "off", or None when NetworkManager cannot say."""
        rc, out = self.query(["nmcli", "radio", "wifi"])
        word = out.strip().lower()
        if rc != 0:
            return None
        states: dict[str, OnOff] = {"enabled": "on", "disabled": "off"}
        return states.get(word)

    def inventory(self) -> WorldState:
        """What is there now; the screen is the display's to know (None)."""
        return WorldState(
            services=self.running_services(),
            processes=count_processes(self.proc),
            radio=self.radio(),
            light=led_state(self.leds),
            screen=None,
        )

    # --- taking ----------------------------------------------------------------------------

    def _helper(self, *args: str) -> int:
        if self.runner is None and not Path(self.helper).is_file():
            log.error("world helper %s is not installed (deploy/install.sh)", self.helper)
            return 127
        rc = (self.runner or sudo_runner)([self.helper, *args])
        if rc != 0:
            self.failures += 1
        return rc

    def take(self, action: str) -> TakeResult:
        """Perform one action; performed only when the machine shows the loss afterwards."""
        kind, _, arg = action.partition(":")
        if kind == "service":
            return self._take_service(action, arg)
        if action == "radio:off":
            return self._take_toggle(action, ("radio", "off"), self.radio)
        if action == "light:off":
            return self._take_toggle(action, ("light", "off"), lambda: led_state(self.leds))
        if kind == "screen":
            return self._take_screen(action, arg)
        return TakeResult(action, False, "unknown action")

    def _take_service(self, action: str, name: str) -> TakeResult:
        if name not in self.services:
            return TakeResult(action, False, "not in the allowed services")
        if name not in self.running_services():
            return TakeResult(action, False, "not running")
        rc = self._helper("stop", name)
        if rc != 0:
            return TakeResult(action, False, f"helper exited {rc}")
        if name in self.running_services():
            return TakeResult(action, False, "still running after stop")
        return TakeResult(action, True, f"{name} stopped")

    def _take_toggle(
        self, action: str, args: tuple[str, str], read: Callable[[], str | None]
    ) -> TakeResult:
        before = read()
        if before != "on":
            return TakeResult(action, False, f"already {before or 'absent'}")
        rc = self._helper(*args)
        if rc != 0:
            return TakeResult(action, False, f"helper exited {rc}")
        after = read()
        if after != "off":
            return TakeResult(action, False, f"still {after or 'unknown'} after the helper")
        return TakeResult(action, True, f"{args[0]} off")

    def _take_screen(self, action: str, arg: str) -> TakeResult:
        if not arg.isdigit() or not 0 <= int(arg) <= 100:
            return TakeResult(action, False, "screen percent must be 0-100")
        if not screen_connected(self.drm):
            return TakeResult(action, False, "no screen connected")
        return TakeResult(action, True, f"screen to {int(arg)}% (dimmed by the display)")

    def restore(self) -> None:
        """Everything the helper took, back (services, radio, lights); never raises."""
        try:
            rc = self._helper("restore")
        except Exception:  # a restore must never stop a death or a start
            log.exception("world restore failed")
            return
        if rc != 0:
            log.error("world restore: helper exited %d", rc)


# --- the allowlist the installer writes ------------------------------------------------------


def allowed_services(config_dir: Path, hardware: str | None = None) -> list[str]:
    """`[world] services` of default.toml with the hardware overlay over it, protected ones out.

    Read without the profile and the models, so that the installer can call it before
    anything else is in place. `hardware` None means `life.hardware`, "auto" detects it.
    """
    from epitaph.config import deep_merge, detect_hardware

    with (config_dir / "default.toml").open("rb") as f:
        data: dict[str, Any] = tomllib.load(f)
    hw = hardware or str(data.get("life", {}).get("hardware", "auto"))
    if hw == "auto":
        hw = detect_hardware()
    with (config_dir / "hardware" / f"{hw}.toml").open("rb") as f:
        data = deep_merge(data, tomllib.load(f))
    world: dict[str, Any] = data.get("world", {})
    out: list[str] = []
    if not world.get("helper"):
        return out  # a simulated world (no helper): nothing the installer may let it stop
    for name in names(world.get("services")):
        if valid_service(name) and name not in out:
            out.append(name)
    return out


def allowlist_text(services: Sequence[str]) -> str:
    """The contents of /etc/epitaph/world-services: a header and one service per line."""
    head = (
        "# epitaph: the services a life may stop (deploy/sbin/epitaph-world). Written by\n"
        "# deploy/install.sh from [world] services; edit the config, not this file.\n"
    )
    return head + "".join(f"{s}\n" for s in services)


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m epitaph.body.pi_world allowlist [--hardware HW]`: print the allowlist file."""
    import argparse

    from epitaph.config import CONFIG_DIR

    p = argparse.ArgumentParser(prog="python -m epitaph.body.pi_world")
    p.add_argument("command", choices=["allowlist"])
    p.add_argument("--hardware", default=None)
    p.add_argument("--config-dir", type=Path, default=CONFIG_DIR)
    args = p.parse_args(argv)
    sys.stdout.write(allowlist_text(allowed_services(args.config_dir, args.hardware)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
