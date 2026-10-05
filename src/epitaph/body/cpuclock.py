# pyright: strict
"""The CPU clock cap, through a small privileged helper (ADR-025).

The controller runs unprivileged, but `scaling_max_freq` belongs to root. The helper
`/usr/local/sbin/epitaph-clock` (deploy/sbin/epitaph-clock) accepts only a whole number of MHz
in 600-1800, or `reset`, and writes the cap on every cpufreq policy. A sudoers drop-in lets the
service user run exactly that command without a password; deploy/install.sh installs both.

The clock is set only when the value changes, and restored to the full clock at every death
and at every start (the controller unit also resets it in ExecStartPre and ExecStopPost, so a
crashed controller cannot leave the machine slow).

`sudo` can take seconds (up to the 10 s limit): with `background=True` (the controller's body)
the helper runs on one worker thread, in order, so the event loop and its watchdog pings never
wait on it.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from epitaph.types import FULL_MHZ

log = logging.getLogger(__name__)

HELPER = "/usr/local/sbin/epitaph-clock"
MIN_MHZ = 600
MAX_MHZ = int(FULL_MHZ)
CPUFREQ = Path("/sys/devices/system/cpu/cpufreq")

# Runs argv and returns its exit status; injected so that tests never call sudo.
Runner = Callable[[Sequence[str]], int]


def sudo_runner(argv: Sequence[str]) -> int:
    """Run argv under `sudo -n` (never prompts), with a 10 s limit; 127 if it cannot start."""
    try:
        res = subprocess.run(
            ["sudo", "-n", *argv], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.error("clock helper did not run: %s", e)
        return 127
    if res.returncode != 0:
        log.error("clock helper %s exited %d: %s", argv, res.returncode, res.stderr.strip())
    return res.returncode


def clamp_mhz(mhz: float) -> int:
    """A knob value as the whole MHz the helper accepts, inside 600-1800."""
    return max(MIN_MHZ, min(MAX_MHZ, round(mhz)))


def read_max_mhz(cpufreq: Path = CPUFREQ) -> list[int]:
    """The current cap of every cpufreq policy, in MHz (empty without cpufreq)."""
    out: list[int] = []
    for f in sorted(cpufreq.glob("policy*/scaling_max_freq")):
        try:
            out.append(int(f.read_text().strip()) // 1000)
        except (OSError, ValueError):
            continue
    return out


class CpuClock:
    """Sets the clock cap through the helper, once per change; `reset` restores 1800 MHz."""

    def __init__(
        self, helper: str = HELPER, runner: Runner | None = None, background: bool = False
    ) -> None:
        """Call `helper` through `runner` (default: sudo_runner; a fake in tests).

        With `background`, `set` and `reset` hand the call to one worker thread and return
        True at once (asked, not yet done); `mhz` follows when the helper has run.
        """
        self.helper = helper
        self.runner = runner
        self.mhz: int | None = None  # what the helper last set; None = unknown
        self.failures = 0
        self._asked: int | None = None  # the last value asked for, set or not
        self._pool = (
            ThreadPoolExecutor(1, thread_name_prefix="epitaph-clock") if background else None
        )

    def set(self, mhz: float) -> bool:
        """Cap the clock at `mhz` (clamped to 600-1800) unless it is already there.

        Returns False if the helper failed; the life goes on at whatever clock it had, and
        only the next change tries again (no retry, and no error, on every thought).
        """
        value = clamp_mhz(mhz)
        if value == self._asked:
            return self._pool is not None or value == self.mhz
        arg = "reset" if value == MAX_MHZ else str(value)
        return self._call(arg, value)

    def reset(self) -> bool:
        """Restore the full clock, always calling the helper (the state may be stale)."""
        return self._call("reset", MAX_MHZ)

    def wait_idle(self) -> None:
        """Block until every call handed to the worker thread has run (tests, shutdown)."""
        if self._pool is not None:
            self._pool.submit(lambda: None).result()

    def _call(self, arg: str, value: int) -> bool:
        if self._pool is None:
            return self._run(arg, value)
        self._asked = value
        self._pool.submit(self._run, arg, value)
        return True

    def _run(self, arg: str, value: int) -> bool:
        self._asked = value
        if self.runner is None and not Path(self.helper).is_file():
            log.error("clock helper %s is not installed (deploy/install.sh)", self.helper)
            rc = 127
        else:
            rc = (self.runner or sudo_runner)([self.helper, arg])
        if rc != 0:
            self.failures += 1
            self.mhz = None  # unknown until a later call succeeds
            return False
        log.info("cpu clock cap %d MHz", value)
        self.mhz = value
        return True
