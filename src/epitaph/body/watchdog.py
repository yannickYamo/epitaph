# pyright: strict
"""systemd notifications and the watchdogs (BUILD_PLAN 4, 9 C5).

The controller unit is `Type=notify` with `WatchdogSec=30`: the controller says READY=1 once it
is up, then WATCHDOG=1 at least every half of that interval, in every state (birth, life,
reload, silence, outside opening hours). If the pings stop, systemd kills and restarts it.

Below systemd, the hardware watchdog (bcm2835) reboots the Pi if systemd itself stops; Raspberry
Pi OS enables it (`RuntimeWatchdogSec=1m`), and `hardware_watchdog_usec` checks that it is on.

No dependency on python-systemd: the notify protocol is one datagram on $NOTIFY_SOCKET.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Callable, Mapping

# Sends one notify datagram to an address; injected in tests.
Sender = Callable[[str, bytes], None]


def _send_datagram(address: str, payload: bytes) -> None:
    if address.startswith("@"):  # abstract namespace socket
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as s:
        s.connect(address)
        s.sendall(payload)


class Notifier:
    """sd_notify for the controller: READY, STATUS, WATCHDOG and STOPPING messages.

    Outside systemd ($NOTIFY_SOCKET unset) every call is a no-op, so the same controller runs
    from a terminal on the laptop.
    """

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        send: Sender = _send_datagram,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        """Read $NOTIFY_SOCKET, $WATCHDOG_USEC and $WATCHDOG_PID from `env` (default os.environ)."""
        env = os.environ if env is None else env
        self.address = env.get("NOTIFY_SOCKET") or None
        self._send = send
        self._now = now
        usec = env.get("WATCHDOG_USEC", "")
        pid = env.get("WATCHDOG_PID", "")
        for_us = not pid or (pid.isdigit() and int(pid) == os.getpid())
        self.watchdog_s = int(usec) / 1e6 if usec.isdigit() and for_us else None
        self._last_ping = -float("inf")

    @property
    def enabled(self) -> bool:
        """Whether a notify socket is present (running under a Type=notify unit)."""
        return self.address is not None

    @property
    def ping_every_s(self) -> float | None:
        """How often to ping: half the watchdog interval; None without a watchdog."""
        return self.watchdog_s / 2 if self.watchdog_s else None

    def notify(self, **fields: str | int) -> bool:
        """Send `KEY=value` lines (READY=1, STATUS=...). False if nothing was sent."""
        if self.address is None or not fields:
            return False
        payload = "\n".join(f"{k.upper()}={v}" for k, v in fields.items()).encode()
        try:
            self._send(self.address, payload)
        except OSError:
            return False
        return True

    def ready(self, status: str = "") -> bool:
        """READY=1, with an optional STATUS line."""
        return self.notify(ready=1, status=status) if status else self.notify(ready=1)

    def status(self, text: str) -> bool:
        """STATUS=text, shown by `systemctl status`."""
        return self.notify(status=text)

    def stopping(self) -> bool:
        """STOPPING=1 before a clean exit."""
        return self.notify(stopping=1)

    def ping(self) -> bool:
        """WATCHDOG=1 now."""
        sent = self.notify(watchdog=1)
        if sent:
            self._last_ping = self._now()
        return sent

    def maybe_ping(self) -> bool:
        """WATCHDOG=1 if half the interval has passed since the last ping; cheap to call often."""
        every = self.ping_every_s
        if every is None or self._now() - self._last_ping < every:
            return False
        return self.ping()


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
