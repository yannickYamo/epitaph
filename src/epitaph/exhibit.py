"""Exhibition hours: when the piece is on show, from the wall clock.

`[exhibit] hours = "10:00-18:00"` names the opening hours in local time; an empty string means
always on, and a closing time before the opening time runs across midnight ("20:00-02:00").
Outside the hours, `outside` decides what the controller does:

- `unseen` (the default): lives go on and the screen goes dark (an `exhibit {open}` event that
  every display obeys);
- `pause`: the current life finishes, then the controller waits for the opening, with the
  screen dark. Watchdog pings continue while it waits.

The Pi has no real-time clock: until NTP has synced, the wall clock is wherever the last
shutdown left it. Hours are therefore applied only while the kernel reports synced time
(`adjtimex`, as `timedatectl` reads it); without it the piece stays on, with a warning.

Everything that reads the world is injectable (`wall`, `synced`, `mono`), so tests run on a
fake clock.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from epitaph.config import Config, ConfigError

__all__ = ["OUTSIDE", "Exhibit", "Hours", "adjtimex_status", "ntp_synced", "parse_hours"]

log = logging.getLogger(__name__)

OUTSIDE = ("unseen", "pause")
DAY_MIN = 24 * 60
SYNC_RECHECK_S = 60.0  # how often the time sync is read again (it can come after the boot)

# adjtimex(2): the clock state the kernel returns when time is not synchronized, the status bit
# NTP clears once it is, and the largest error systemd still calls synced (16 s, in µs).
TIME_ERROR = 5
STA_UNSYNC = 0x0040
MAX_ERROR_US = 16_000_000


@dataclass(frozen=True)
class Hours:
    """Opening hours as minutes after local midnight; `closes` < `opens` runs past midnight."""

    opens: int
    closes: int

    def is_open(self, now: datetime) -> bool:
        """Whether `now` (local wall time) falls inside the hours; opening inclusive."""
        m = now.hour * 60 + now.minute + now.second / 60 + now.microsecond / 60e6
        if self.opens < self.closes:
            return self.opens <= m < self.closes
        return m >= self.opens or m < self.closes

    def next_opening(self, now: datetime) -> datetime:
        """The next opening at or after `now` (local wall time)."""
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        at = midnight + timedelta(minutes=self.opens)
        return at if at >= now else at + timedelta(days=1)

    def __str__(self) -> str:
        """As written in the config: "HH:MM-HH:MM"."""
        return f"{_hhmm(self.opens)}-{_hhmm(self.closes)}"


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _minutes(text: str) -> int:
    hh, sep, mm = text.strip().partition(":")
    if not sep or not hh.isdigit() or not mm.isdigit() or len(mm) != 2:
        raise ValueError(text)
    h, m = int(hh), int(mm)
    if h == 24 and m == 0:
        return DAY_MIN
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(text)
    return h * 60 + m


def parse_hours(text: str) -> Hours | None:
    """`"HH:MM-HH:MM"` as Hours; an empty string (always on) as None. Raises ConfigError."""
    text = text.strip()
    if not text:
        return None
    start, sep, end = text.partition("-")
    try:
        if not sep:
            raise ValueError(text)
        opens, closes = _minutes(start), _minutes(end)
    except ValueError:
        raise ConfigError(
            f'exhibit.hours must be "HH:MM-HH:MM" (or "" for always on), not {text!r}'
        ) from None
    opens %= DAY_MIN
    closes %= DAY_MIN
    if opens == closes:
        raise ConfigError(f'exhibit.hours {text!r} opens and closes at once; "" means always on')
    return Hours(opens, closes)


# ---------------------------------------------------------------------------------------
# time sync


class _Timex(ctypes.Structure):
    # The head of struct timex (linux/timex.h); the rest is padding the kernel may write.
    _fields_ = [
        ("modes", ctypes.c_uint),
        ("offset", ctypes.c_long),
        ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long),
        ("esterror", ctypes.c_long),
        ("status", ctypes.c_int),
        ("_rest", ctypes.c_char * 512),
    ]


def adjtimex_status() -> tuple[int, int, int]:
    """(clock state, status bits, max error in µs) from a read-only adjtimex(2) call.

    Raises OSError where there is no adjtimex (not Linux) or the call fails.
    """
    name = ctypes.util.find_library("c")
    if name is None:
        raise OSError("no C library")
    libc = ctypes.CDLL(name, use_errno=True)
    fn = getattr(libc, "adjtimex", None)
    if fn is None:
        raise OSError("no adjtimex")
    tx = _Timex()  # modes = 0: read only, no privilege needed
    state = int(fn(ctypes.byref(tx)))
    if state < 0:
        raise OSError(ctypes.get_errno(), "adjtimex failed")
    return state, int(tx.status), int(tx.maxerror)


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def ntp_synced(
    adjtimex: Callable[[], tuple[int, int, int]] | None = adjtimex_status,
    run: Runner | None = subprocess.run,
) -> bool | None:
    """Whether the system clock is NTP-synchronized; None when it cannot be told.

    The kernel's own word first (adjtimex, the test systemd's `NTPSynchronized` makes), then
    `timedatectl show -p NTPSynchronized` where adjtimex is not available.
    """
    if adjtimex is not None:
        try:
            state, status, maxerror = adjtimex()
        except OSError as e:
            log.debug("adjtimex unavailable: %s", e)
        else:
            return state != TIME_ERROR and not status & STA_UNSYNC and maxerror < MAX_ERROR_US
    if run is not None:
        try:
            out = run(
                ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            log.debug("timedatectl unavailable: %s", e)
        else:
            value = out.stdout.strip().lower()
            if out.returncode == 0 and value in ("yes", "no"):
                return value == "yes"
    return None


# ---------------------------------------------------------------------------------------
# the hours, applied


class Exhibit:
    """Whether the piece is on show now, and how long until it opens."""

    def __init__(
        self,
        hours: Hours | None,
        outside: str = "unseen",
        *,
        wall: Callable[[], datetime] = datetime.now,
        synced: Callable[[], bool | None] = ntp_synced,
        mono: Callable[[], float] = time.monotonic,
        recheck_s: float = SYNC_RECHECK_S,
    ) -> None:
        """`wall()` is local wall time; `synced()` says whether it can be trusted; `mono()`
        paces the sync re-check (every `recheck_s`). Raises ConfigError for a bad `outside`."""
        if outside not in OUTSIDE:
            raise ConfigError(f"exhibit.outside must be one of {OUTSIDE}, not {outside!r}")
        self.hours = hours
        self.outside = outside
        self.wall = wall
        self.synced_fn = synced
        self.mono = mono
        self.recheck_s = recheck_s
        self._synced: bool | None = None
        self._checked_at: float | None = None
        self._warned = False

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        *,
        wall: Callable[[], datetime] = datetime.now,
        synced: Callable[[], bool | None] = ntp_synced,
        mono: Callable[[], float] = time.monotonic,
    ) -> Exhibit:
        """The `[exhibit]` section of `cfg`. Raises ConfigError for bad hours or mode."""
        hours = parse_hours(str(cfg.get("exhibit.hours", "") or ""))
        outside = str(cfg.get("exhibit.outside", "unseen"))
        return cls(hours, outside, wall=wall, synced=synced, mono=mono)

    @property
    def enabled(self) -> bool:
        """Whether hours are configured (they may still be off while time is unsynced)."""
        return self.hours is not None

    @property
    def pause(self) -> bool:
        """Whether lives stop outside the hours (else they go on unseen)."""
        return self.outside == "pause"

    def time_trusted(self) -> bool:
        """Whether the wall clock is synced; re-read every `recheck_s`. Warns on a change."""
        now = self.mono()
        if self._checked_at is None or now - self._checked_at >= self.recheck_s:
            self._checked_at = now
            try:
                synced = self.synced_fn()
            except Exception:
                log.exception("reading the time sync failed")
                synced = None
            if synced is not True and not self._warned:
                log.warning(
                    "exhibition hours %s are off: the system time is not NTP-synchronized "
                    "(%s); the piece stays on until it is",
                    self.hours,
                    "unknown" if synced is None else "not synced",
                )
                self._warned = True
            elif synced is True and self._warned:
                log.info("system time synchronized: exhibition hours %s apply", self.hours)
                self._warned = False
            self._synced = synced
        return self._synced is True

    def is_open(self) -> bool:
        """Whether the piece is on show now: always without hours or without synced time."""
        if self.hours is None or not self.time_trusted():
            return True
        return self.hours.is_open(self.wall())

    def seconds_to_open(self) -> float:
        """Seconds until the next opening (0 when open now)."""
        if self.is_open():
            return 0.0
        assert self.hours is not None
        now = self.wall()
        return max(0.0, (self.hours.next_opening(now) - now).total_seconds())

    def describe(self) -> dict[str, object]:
        """For the status file: the hours, the mode, whether open and whether time is trusted."""
        return {
            "hours": str(self.hours) if self.hours is not None else "",
            "outside": self.outside,
            "open": self.is_open(),
            "time_synced": self._synced,
        }
