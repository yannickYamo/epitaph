# pyright: strict
"""`epitaph probe`: what this machine has for a life to lose, read without changing anything.

A life takes its world from the outside in (ADR-031): services, the radio, the lights, the
screen, then the CPU share and clock, the memory and at last the RAM. Each needs something of
the machine: a cgroup controller, cpufreq, an LED, NetworkManager, a connected screen. The
probe reads which are there, so that someone bringing the piece to another Linux board knows
before installing what a life can lose on it and what goes in its hardware overlay. Whatever
is missing is never taken, and by the truth rule never reported: the piece still runs.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from epitaph.body.pi_world import (
    DRM,
    LED_NAMES,
    LEDS,
    PROC,
    Query,
    count_processes,
    plain_query,
    screen_connected,
    valid_service,
)

CGROUP = Path("/sys/fs/cgroup")
CPUFREQ = Path("/sys/devices/system/cpu/cpufreq")
MODEL = Path("/proc/device-tree/model")
# Services a life may lose without harm on a Debian-like board, outermost first: the
# reference Pi's (config/hardware/pi4-4gb.toml) and others often found running.
CANDIDATES = (
    "nfs-blkmap",
    "rpcbind",
    "cron",
    "bluetooth",
    "avahi-daemon",
    "ModemManager",
    "cups",
    "triggerhappy",
)


@dataclass(frozen=True)
class Found:
    """One thing a life can lose: whether this machine has it, and what the probe read."""

    what: str
    ok: bool
    detail: str

    def line(self) -> str:
        """One line of the report."""
        return f"{'yes' if self.ok else 'no ':3}  {self.what:22} {self.detail}"


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="ignore").strip().strip("\x00")
    except OSError:
        return ""


def board(model: Path = MODEL, proc: Path = PROC) -> str:
    """The board's name, its cores and its RAM."""
    name = _read(model) or "a machine without a device tree (a PC?)"
    ram_kb = 0
    for ln in _read(proc / "meminfo").splitlines():
        if ln.startswith("MemTotal:"):
            ram_kb = int(ln.split()[1])
    return f"{name}, {os.cpu_count() or 1} cores, {ram_kb / 1024 / 1024:.1f} GB"


def clock_range(cpufreq: Path = CPUFREQ) -> tuple[int, int] | None:
    """The lowest and highest clock of the first cpufreq policy, in MHz; None without one."""
    for policy in sorted(cpufreq.glob("policy*")):
        try:
            lo = int(_read(policy / "cpuinfo_min_freq")) // 1000
            hi = int(_read(policy / "cpuinfo_max_freq")) // 1000
        except ValueError:
            continue
        return lo, hi
    return None


def running(names: Sequence[str], query: Query) -> list[str]:
    """Those of `names` that are active services a life may lose."""
    allowed = [n for n in names if valid_service(n)]
    if not allowed:
        return []
    _, out = query(["systemctl", "is-active", *(f"{n}.service" for n in allowed)])
    return [n for n, st in zip(allowed, out.split(), strict=False) if st == "active"]


def probe(
    query: Query = plain_query,
    cgroup: Path = CGROUP,
    cpufreq: Path = CPUFREQ,
    leds: Path = LEDS,
    drm: Path = DRM,
    proc: Path = PROC,
    candidates: Sequence[str] = CANDIDATES,
) -> list[Found]:
    """What a life can lose here, outermost first."""
    out: list[Found] = []
    services = running(candidates, query)
    out.append(
        Found(
            "services",
            bool(services),
            ", ".join(services) if services else "none of the usual ones runs: name yours",
        )
    )
    out.append(Found("processes around it", True, f"{count_processes(proc)} run now"))
    rc, radio = query(["nmcli", "radio", "wifi"])
    out.append(
        Found(
            "radio",
            rc == 0 and radio.strip() == "enabled",
            f"Wi-Fi {radio.strip()}" if rc == 0 else "no NetworkManager (nmcli)",
        )
    )
    have = [n for n in LED_NAMES if (leds / n / "brightness").is_file()]
    try:
        others = sorted(d.name for d in leds.iterdir() if d.name not in LED_NAMES)
    except OSError:
        others = []
    detail = ", ".join(have) if have else f"no {' or '.join(LED_NAMES)} LED"
    if others and not have:
        detail += (
            f" (this board has {', '.join(others[:4])}: name them as LED_NAMES in "
            "deploy/sbin/epitaph-world and body/pi_world.py)"
        )
    out.append(Found("light", bool(have), detail))
    out.append(
        Found(
            "screen",
            screen_connected(drm),
            "connected" if screen_connected(drm) else "none connected: it is never dimmed",
        )
    )
    controllers = _read(cgroup / "cgroup.controllers").split()
    out.append(
        Found(
            "CPU share",
            "cpu" in controllers,
            "cgroup v2 cpu controller" if "cpu" in controllers else "no cgroup v2 cpu controller",
        )
    )
    mhz = clock_range(cpufreq)
    out.append(
        Found(
            "CPU clock",
            mhz is not None and mhz[0] < mhz[1],
            f"{mhz[0]} to {mhz[1]} MHz" if mhz else "no cpufreq: the clock is never lowered",
        )
    )
    out.append(
        Found(
            "RAM (the death)",
            "memory" in controllers,
            "cgroup v2 memory controller"
            if "memory" in controllers
            else "no cgroup v2 memory controller: on a Pi add cgroup_enable=memory "
            "cgroup_memory=1 to cmdline.txt (docs/INSTALLATION.md)",
        )
    )
    return out


def overlay(found: Sequence[Found], name: str) -> str:
    """The lines of a hardware overlay that depend on this machine."""
    by = {f.what: f for f in found}
    services = by["services"].detail.split(", ") if by["services"].ok else []
    quoted = ", ".join(f'"{s}"' for s in services)
    clock = "/usr/local/sbin/epitaph-clock" if by["CPU clock"].ok else ""
    return (
        f"# config/hardware/{name}.toml: start from pi4-4gb.toml and set these.\n"
        "[body]\n"
        f'clock_helper = "{clock}"\n'
        "\n"
        "[world]\n"
        'helper = "/usr/local/sbin/epitaph-world"\n'
        f"services = [{quoted}]\n"
    )


def report(found: Sequence[Found], name: str, write: Callable[[str], object] = print) -> int:
    """Print the report and the overlay lines; 0 when the death is possible, else 1."""
    write(f"{board()}\n")
    write("What a life can lose on this machine:")
    for f in found:
        write("  " + f.line())
    write("")
    write(overlay(found, name))
    death = next(f for f in found if f.what.startswith("RAM"))
    if not death.ok:
        write("Without the memory controller a life cannot die of RAM here; fix that first.")
    return 0 if death.ok else 1
