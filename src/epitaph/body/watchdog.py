# pyright: strict
"""The hardware watchdog check (BUILD_PLAN 4, 9 C5).

The controller unit is `Type=notify` with `WatchdogSec=30`; the controller itself sends
READY=1, WATCHDOG=1 and STOPPING=1 (`epitaph.controller.sd_notify`), and stops pinging when its
life loop stops moving, so that systemd restarts it.

Below systemd, the hardware watchdog (bcm2835) reboots the Pi if systemd itself stops; Raspberry
Pi OS enables it (`RuntimeWatchdogSec=1m`), and `hardware_watchdog_usec` checks that it is on.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable


def hardware_watchdog_usec(
    run: Callable[[list[str]], str] | None = None,
) -> int | None:
    """systemd's RuntimeWatchdogUSec (0 = hardware watchdog off); None if it cannot be read."""

    def _run(argv: list[str]) -> str:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False).stdout

    try:
        out = (run or _run)(["systemctl", "show", "-p", "RuntimeWatchdogUSec", "--value"])
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_usec(out.strip())


def parse_usec(text: str) -> int | None:
    """systemd's time spans as printed by `systemctl show` ("1min", "30s", "0", "infinity")."""
    if not text:
        return None
    if text == "infinity":
        return 2**63 - 1
    units = {"us": 1, "ms": 1000, "s": 1_000_000, "min": 60_000_000, "h": 3_600_000_000}
    total = 0
    for part in text.split():
        num = part.rstrip("abcdefghijklmnopqrstuvwxyz")
        unit = part[len(num) :] or "us"
        if not num.replace(".", "", 1).isdigit() or unit not in units:
            return None
        total += round(float(num) * units[unit])
    return total
