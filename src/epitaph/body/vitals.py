"""Vitals and machine facts read from the running system (BUILD_PLAN 9 C4).

Every reader takes its paths from a `SysPaths`, so the unit tests point them at a
temporary directory. Nothing here needs root. A missing file gives `None`, never an error:
the laptop has no `vcgencmd` and may have no thermal zone.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from epitaph.types import MachineFacts, Vitals

# Bits of `vcgencmd get_throttled` (Raspberry Pi firmware documentation).
UNDERVOLT_NOW = 0x1
THROTTLED_NOW = 0x4
UNDERVOLT_OCCURRED = 0x10000
THROTTLED_OCCURRED = 0x40000


@dataclass(frozen=True)
class SysPaths:
    """Where the readers look. The defaults are the real system."""

    thermal: Path = Path("/sys/class/thermal/thermal_zone0/temp")
    meminfo: Path = Path("/proc/meminfo")
    model: Path = Path("/proc/device-tree/model")
    throttled_sysfs: Path = Path("/sys/devices/platform/soc/soc:firmware/get_throttled")
    vcgencmd: str = "vcgencmd"


DEFAULT_PATHS = SysPaths()


def read_cpu_temp(paths: SysPaths = DEFAULT_PATHS) -> float | None:
    """CPU temperature in °C, or None."""
    try:
        return round(int(paths.thermal.read_text().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def read_throttled(paths: SysPaths = DEFAULT_PATHS) -> int | None:
    """The firmware's throttling bits (`vcgencmd get_throttled`), or None off a Pi."""
    try:
        return int(paths.throttled_sysfs.read_text().strip(), 16)
    except (OSError, ValueError):
        pass
    exe = shutil.which(paths.vcgencmd)
    if exe is None:
        return None
    try:
        out = subprocess.run(
            [exe, "get_throttled"], capture_output=True, text=True, timeout=2, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_throttled(out)


def parse_throttled(text: str) -> int | None:
    """Parse "throttled=0x50000"."""
    _, _, value = text.strip().partition("=")
    try:
        return int(value, 16)
    except ValueError:
        return None


def describe_throttled(bits: int) -> list[str]:
    """Human-readable names for the bits that matter here."""
    names = {
        UNDERVOLT_NOW: "under-voltage now",
        THROTTLED_NOW: "throttled now",
        UNDERVOLT_OCCURRED: "under-voltage occurred",
        THROTTLED_OCCURRED: "throttling occurred",
    }
    return [name for bit, name in names.items() if bits & bit]


def read_meminfo(paths: SysPaths = DEFAULT_PATHS) -> dict[str, int]:
    """/proc/meminfo in kB."""
    out: dict[str, int] = {}
    try:
        for line in paths.meminfo.read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                out[key.strip()] = int(parts[0])
    except OSError:
        pass
    return out


def machine_facts(paths: SysPaths = DEFAULT_PATHS) -> MachineFacts:
    """True facts for the persona facts line: board, cores, nominal RAM.

    RAM is the board's nominal size (MemTotal rounded up to a whole GB), so a 4 GB Pi 4
    that reports 3.7 GiB is described as 4 GB, which is what is printed on the board.
    """
    try:
        model = paths.model.read_text(errors="ignore").rstrip("\x00\n ")
        model = model.split(" Rev ")[0]
    except OSError:
        model = os.uname().machine
    kib = read_meminfo(paths).get("MemTotal", 0)
    ram_gb = float(math.ceil(kib / 1024 / 1024)) if kib else 0.0
    return MachineFacts(model=model, cores=os.cpu_count() or 1, ram_gb=ram_gb)


class VitalsReader:
    """Reads the body's vitals; `vcgencmd` is called at most every `throttle_every_s`."""

    def __init__(self, paths: SysPaths = DEFAULT_PATHS, throttle_every_s: float = 10.0) -> None:
        self.paths = paths
        self.throttle_every_s = throttle_every_s
        self._throttled: int | None = None
        self._throttled_at = -math.inf

    def throttled(self) -> int | None:
        now = time.monotonic()
        if now - self._throttled_at >= self.throttle_every_s:
            self._throttled = read_throttled(self.paths)
            self._throttled_at = now
        return self._throttled

    def read(
        self,
        ram_limit_mb: int | None = None,
        mem_used_mb: int | None = None,
        cores_effective: float | None = None,
    ) -> Vitals:
        return Vitals(
            cpu_c=read_cpu_temp(self.paths),
            throttled=self.throttled(),
            ram_limit_mb=ram_limit_mb,
            mem_used_mb=mem_used_mb,
            cores_effective=cores_effective,
        )
