# pyright: strict
"""The real body: a delegated cgroup v2 subtree around the creature (BUILD_PLAN 5.5, 9 C3).

Layout under the controller's delegated cgroup (systemd `Delegate=yes`)::

    <service cgroup>/            subtree_control: +memory +cpu +io
        supervisor/              the controller itself (cgroup v2 forbids processes in
                                 an inner node, so it moves here first)
        creature/                llama-server: memory.swap.max=0, memory.oom.group=1,
                                 cpu.max (the CPU share), memory.max (death only)

Everything is plain file I/O on cgroupfs, so the unit tests run it against a temporary
directory laid out like cgroupfs. Nothing needs root once systemd has delegated the subtree.
What the Pi spikes proved (S3, S3b, S3c) is recorded in docs/SPIKE.md.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from epitaph.body.base import Body
from epitaph.body.cpuclock import MAX_MHZ, CpuClock
from epitaph.body.vitals import DEFAULT_PATHS, SysPaths, VitalsReader, machine_facts
from epitaph.types import Cause, CreatureStatus, Knobs, MachineFacts, ProgressCounters, Vitals

if TYPE_CHECKING:
    from epitaph.config import Config

log = logging.getLogger(__name__)

CGROUP_FS = Path("/sys/fs/cgroup")
CONTROLLERS = ("memory", "cpu", "io")
MIB = 1024 * 1024
UNIT_PREFIX = "epitaph"


class CgroupError(RuntimeError):
    """The delegated cgroup subtree is missing or unusable."""


@dataclass(frozen=True)
class CgroupSettings:
    """Machine settings for the body, from `[body]` and `[backend]` in the config."""

    creature_cpus: str = "1-3"
    death_mode: str = "oom"  # oom | deadline
    squeeze: str = "death_only"  # death_only | gradual | off
    cpu_share: bool = True
    cpu_period_us: int = 100_000
    # The death limit: a fixed size, or a fraction of the creature's memory at that moment.
    death_limit_mb: int | None = None
    death_fraction: float = 0.5
    kill_wait_s: float = 5.0
    # The clock helper (cpuclock.HELPER on the Pi); empty leaves the CPU clock alone.
    clock_helper: str = ""

    @classmethod
    def from_config(cls, cfg: Config) -> CgroupSettings:
        """Read `[body]` and `backend.creature_cpus` from the config."""
        body = cfg.section("body")
        limit = body.get("death_limit_mb")
        return cls(
            creature_cpus=str(cfg.get("backend.creature_cpus", "1-3") or ""),
            death_mode=str(body.get("death_mode", "oom")),
            squeeze=str(body.get("squeeze", "death_only")),
            cpu_share=bool(body.get("cpu_share", True)),
            cpu_period_us=int(body.get("cpu_period_us", 100_000)),
            death_limit_mb=int(limit) if limit is not None else None,
            death_fraction=float(body.get("death_fraction", 0.5)),
            clock_helper=str(body.get("clock_helper", "") or ""),
        )

    @property
    def cpu_count(self) -> int:
        """How many CPUs `creature_cpus` names ("1-3" is 3); 0 when unpinned."""
        n = 0
        for part in filter(None, self.creature_cpus.split(",")):
            lo, _, hi = part.partition("-")
            n += int(hi or lo) - int(lo) + 1
        return n


DEFAULT_SETTINGS = CgroupSettings()


def wrap_argv(argv: list[str], procs_file: Path | None, cpus: str) -> list[str]:
    """argv that joins `procs_file`'s cgroup, pins itself to `cpus`, then execs the command.

    The shell writes its own pid into cgroup.procs before `exec`, so the creature (and every
    thread it starts) is born inside the cgroup and keeps the pid the backend sees.
    """
    out: list[str] = []
    if procs_file is not None:
        out += ["/bin/sh", "-c", 'echo $$ > "$0" && exec "$@"', str(procs_file)]
    if cpus and shutil.which("taskset"):
        out += ["taskset", "-c", cpus]
    return out + list(argv)


def drop_page_cache(path: str | Path) -> None:
    """Evict a file's clean pages from the page cache (no root needed).

    Page-cache pages stay charged to the cgroup that first read them. A model file cached by
    an rsync, a checksum or a bench is therefore not in the creature's memory.current, and
    the death limit cannot touch it (spike S3). The backend calls this before spawning an
    mmap creature, so the creature reads, and is charged for, its own weights.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


def own_cgroup(proc_cgroup: Path = Path("/proc/self/cgroup")) -> str:
    """This process's cgroup v2 path, e.g. "/system.slice/epitaph-controller.service"."""
    for line in proc_cgroup.read_text().splitlines():
        if line.startswith("0::"):
            return line[3:].strip()
    raise CgroupError(f"no cgroup v2 entry in {proc_cgroup}")


def _read(path: Path) -> str:
    return path.read_text().strip()


def read_flat_keyed(path: Path) -> dict[str, int]:
    """Parse cpu.stat, memory.stat or memory.events ("key value" per line)."""
    out: dict[str, int] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if value.strip().lstrip("-").isdigit():
            out[key] = int(value)
    return out


def read_io_rbytes(path: Path) -> int:
    """Sum of rbytes over every device in io.stat."""
    total = 0
    try:
        text = path.read_text()
    except OSError:
        return 0
    for line in text.splitlines():
        for field in line.split()[1:]:
            key, _, value = field.partition("=")
            if key == "rbytes" and value.isdigit():
                total += int(value)
    return total


class CgroupBody:
    """The creature's cgroup: CPU share, the death limit, counters and the kill."""

    def __init__(
        self,
        root: Path,
        settings: CgroupSettings = DEFAULT_SETTINGS,
        vitals: VitalsReader | None = None,
        sys_paths: SysPaths = DEFAULT_PATHS,
        clock: CpuClock | None = None,
    ) -> None:
        """`root` is the delegated cgroup; nothing is touched until setup().

        `vitals` defaults to a VitalsReader on `sys_paths`. `clock` defaults to a CpuClock on
        `settings.clock_helper`, or none when that is empty.
        """
        self.root = root
        self.settings = settings
        self.supervisor = root / "supervisor"
        self.creature = root / "creature"
        self._vitals = vitals or VitalsReader(sys_paths)
        self._sys_paths = sys_paths
        self.controllers: set[str] = set()
        self._share: float | None = None
        self._squeezed_at: float | None = None
        self._kill_cause: Cause | None = None
        self._oom_base = 0
        if clock is None and settings.clock_helper:
            clock = CpuClock(settings.clock_helper)
        self.clock = clock

    # --- setup -------------------------------------------------------------------------

    @classmethod
    def delegated(
        cls,
        settings: CgroupSettings = DEFAULT_SETTINGS,
        fs: Path = CGROUP_FS,
        proc_cgroup: Path = Path("/proc/self/cgroup"),
    ) -> CgroupBody:
        """The body for the cgroup this process runs in (the controller's service cgroup).

        Only a unit whose name starts with `epitaph` is adopted: setup() moves every process
        of the cgroup into supervisor/, which must never happen to a terminal's or a desktop
        session's scope that merely happens to be delegated to the user.
        """
        rel = own_cgroup(proc_cgroup).lstrip("/")
        path = fs / rel if rel else fs
        if path.name == "supervisor":  # set up already, in an earlier call
            path = path.parent
        if not path.name.startswith(UNIT_PREFIX):
            raise CgroupError(f"{path.name} is not an {UNIT_PREFIX}* unit; not adopting it")
        if not (path / "cgroup.procs").exists():
            raise CgroupError(f"{path} is not a cgroup")
        if not os.access(path / "cgroup.subtree_control", os.W_OK):
            raise CgroupError(
                f"{path} is not delegated to this user (run under a Delegate=yes unit)"
            )
        body = cls(path, settings)
        body.setup()
        return body

    def setup(self) -> None:
        """Supervisor leaf, controllers on, creature leaf, clean limits. Idempotent."""
        self.supervisor.mkdir(exist_ok=True)
        # cgroup v2 "no internal processes": the inner node must be empty before it can
        # hand controllers to its children. Every process moves; threads follow.
        for pid in _read(self.root / "cgroup.procs").split():
            with contextlib.suppress(ProcessLookupError):  # exited meanwhile
                (self.supervisor / "cgroup.procs").write_text(pid)
        available = set(_read(self.root / "cgroup.controllers").split())
        enabled = set(_read(self.root / "cgroup.subtree_control").split())
        wanted = [c for c in CONTROLLERS if c in available and c not in enabled]
        if wanted:
            (self.root / "cgroup.subtree_control").write_text(" ".join(f"+{c}" for c in wanted))
        self.controllers = {
            c.lstrip("+") for c in _read(self.root / "cgroup.subtree_control").split()
        }
        if "memory" not in self.controllers and self.settings.death_mode == "oom":
            log.warning("memory controller unavailable: death falls back to the deadline")
        self.creature.mkdir(exist_ok=True)
        self.reset_creature_cgroup()

    @property
    def death_mode(self) -> str:
        """The death mode actually in force (oom needs the memory controller)."""
        if self.settings.death_mode == "oom" and "memory" in self.controllers:
            return "oom"
        return "deadline"

    # --- small helpers -----------------------------------------------------------------

    def _write(self, name: str, value: str, controller: str | None = None) -> None:
        if controller is not None and controller not in self.controllers:
            return
        (self.creature / name).write_text(value)

    def pids(self) -> list[int]:
        """Pids in the creature cgroup; empty when it cannot be read."""
        try:
            return [int(p) for p in _read(self.creature / "cgroup.procs").split()]
        except OSError:
            return []

    def populated(self) -> bool:
        """Whether a process is left in the creature cgroup (cgroup.events, else cgroup.procs)."""
        events = read_flat_keyed(self.creature / "cgroup.events")
        if "populated" in events:
            return events["populated"] == 1
        return bool(self.pids())

    def memory_events(self) -> dict[str, int]:
        """The creature's memory.events counters (oom, oom_kill, oom_group_kill, ...)."""
        return read_flat_keyed(self.creature / "memory.events")

    def _oom_kills(self) -> int:
        ev = self.memory_events()
        return max(ev.get("oom_kill", 0), ev.get("oom_group_kill", 0))

    def _kill_all(self) -> None:
        try:
            (self.creature / "cgroup.kill").write_text("1")
            return
        except OSError:
            pass
        for pid in self.pids():  # kernels before 5.14 have no cgroup.kill
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

    def wait_empty(self, timeout_s: float | None = None) -> bool:
        """Block until the creature cgroup is empty, polling every 50 ms; False on timeout.

        The timeout is in seconds and defaults to `settings.kill_wait_s`.
        """
        deadline = time.monotonic() + (
            self.settings.kill_wait_s if timeout_s is None else timeout_s
        )
        while self.populated():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    # --- the Body protocol -------------------------------------------------------------

    def restore_clock(self, force: bool = False) -> None:
        """Back to the full clock; without `force`, only if a lower cap is (or may be) set."""
        if self.clock is not None and (force or self.clock.mhz != MAX_MHZ):
            self.clock.reset()

    def reset_creature_cgroup(self) -> None:
        """Kill any leftover creature and restore the birth limits and the full clock.

        cpu.max unlimited, memory.high and memory.max off, swap 0, memory.oom.group on. The share,
        squeeze and kill cause are forgotten, and the OOM-kill count is taken as the new baseline
        so that an earlier life's kill is not read as this life's death. The clock helper is
        always called here: a controller that crashed may have left the clock capped.
        """
        if self.populated():
            self._kill_all()
            if not self.wait_empty():
                log.error("creature cgroup still populated after cgroup.kill")
        period = self.settings.cpu_period_us
        self._write("cpu.max", f"max {period}", "cpu")
        self._write("memory.high", "max", "memory")
        self._write("memory.max", "max", "memory")
        self._write("memory.swap.max", "0", "memory")
        self._write("memory.oom.group", "1", "memory")
        self._share = None
        self._squeezed_at = None
        self._kill_cause = None
        self._oom_base = self._oom_kills()
        self.restore_clock(force=True)

    def wrap_spawn(self, argv: list[str]) -> list[str]:
        """Argv that joins the creature cgroup and pins to `creature_cpus` (see wrap_argv)."""
        return wrap_argv(argv, self.creature / "cgroup.procs", self.settings.creature_cpus)

    def apply(self, knobs: Knobs) -> None:
        """Set cpu.max and the clock cap when they changed; squeeze to death once, when asked.

        The squeeze needs the oom death mode in force and `squeeze` not "off". The clock helper
        runs only when `cpu_mhz` changes (CpuClock remembers the last value).
        """
        if self.settings.cpu_share and knobs.cpu_share != self._share:
            self.set_cpu_share(knobs.cpu_share)
        if self.clock is not None:
            self.clock.set(knobs.cpu_mhz)
        if (
            knobs.death_squeeze
            and self._squeezed_at is None
            and self.death_mode == "oom"
            and self.settings.squeeze != "off"
        ):
            self.squeeze_to_death()

    def set_cpu_share(self, cores: float) -> None:
        """cpu.max for `cores` CPUs' worth of time; unlimited at or above the pinned CPUs."""
        period = self.settings.cpu_period_us
        n = self.settings.cpu_count
        if n and cores >= n:
            value = f"max {period}"
        else:
            value = f"{max(1000, round(cores * period))} {period}"
        self._write("cpu.max", value, "cpu")
        self._share = cores

    def death_limit_bytes(self) -> int:
        """The death level: a fraction of the creature's *anonymous* memory.

        S3: a limit below the anonymous memory kills in about 1 s in every load mode (swap
        is off, so anon cannot be reclaimed). A limit that only undercuts memory.current
        does not kill an mmap creature: the kernel evicts weight pages and it thrashes on
        the SD card (no kill in 30 s, 5 of 5). memory.current is the fallback when
        memory.stat has no anon line.
        """
        if self.settings.death_limit_mb is not None:
            return self.settings.death_limit_mb * MIB
        base = read_flat_keyed(self.creature / "memory.stat").get("anon", 0)
        if not base:
            try:
                base = int(_read(self.creature / "memory.current"))
            except (OSError, ValueError):
                base = 0
        return max(4 * MIB, int(base * self.settings.death_fraction))

    def squeeze_to_death(self) -> None:
        """Take the creature's RAM: memory.max below its working set (swap is off)."""
        limit = self.death_limit_bytes()
        log.info("death squeeze: memory.max = %d MiB", limit // MIB)
        self._write("memory.max", str(limit), "memory")
        self._squeezed_at = time.monotonic()

    def progress(self) -> ProgressCounters:
        """The creature cgroup's usage_usec, io.stat rbytes and pgmajfault."""
        return ProgressCounters(
            cpu_usec=read_flat_keyed(self.creature / "cpu.stat").get("usage_usec", 0),
            io_rbytes=read_io_rbytes(self.creature / "io.stat"),
            majfault=read_flat_keyed(self.creature / "memory.stat").get("pgmajfault", 0),
        )

    def kill_now(self, cause: Cause) -> None:
        """Record `cause`, kill every process in the creature cgroup, restore the full clock."""
        self._kill_cause = cause
        self._kill_all()
        self.restore_clock()

    def death_cause(self, status: CreatureStatus) -> Cause:
        """The recorded kill cause; else OOM if the kernel OOM-killed it or a SIGKILL followed
        the squeeze; else a crash.

        Every death passes through here, so the full clock is restored here too (an OOM or a
        crash never goes through kill_now).
        """
        self.restore_clock()
        if self._kill_cause is not None:
            return self._kill_cause
        if self._oom_kills() > self._oom_base:
            return Cause.OOM
        if self._squeezed_at is not None and status.signal == signal.SIGKILL:
            return Cause.OOM
        return Cause.CRASH

    def vitals(self) -> Vitals:
        """Vitals with the creature's memory.max and memory.current (MiB) and its CPU share."""
        limit: int | None = None
        used: int | None = None
        try:
            raw = _read(self.creature / "memory.max")
            limit = None if raw == "max" else int(raw) // MIB
            used = int(_read(self.creature / "memory.current")) // MIB
        except (OSError, ValueError):
            pass
        cores = self._share if self._share is not None else float(self.settings.cpu_count or 0)
        return self._vitals.read(ram_limit_mb=limit, mem_used_mb=used, cores_effective=cores)

    def facts(self) -> MachineFacts:
        """True machine facts: board, cores, nominal RAM."""
        return machine_facts(self._sys_paths)


class PlainBody:
    """A body without cgroups (the laptop): pinning only, real vitals, no limits.

    The kill itself is the backend's job here (it holds the pid); `kill_now` records the cause.
    """

    death_mode = "deadline"

    def __init__(
        self, settings: CgroupSettings = DEFAULT_SETTINGS, sys_paths: SysPaths = DEFAULT_PATHS
    ) -> None:
        """Pin to `settings.creature_cpus`; read vitals from `sys_paths`."""
        self.settings = settings
        self._vitals = VitalsReader(sys_paths)
        self._sys_paths = sys_paths
        self._share: float | None = None
        self._kill_cause: Cause | None = None

    def reset_creature_cgroup(self) -> None:
        """Forget the last kill cause (there is no cgroup to clean)."""
        self._kill_cause = None

    def wrap_spawn(self, argv: list[str]) -> list[str]:
        """Argv pinned to `creature_cpus`, with no cgroup to join."""
        return wrap_argv(argv, None, self.settings.creature_cpus)

    def apply(self, knobs: Knobs) -> None:
        """Remember the CPU share for the vitals; nothing enforces it."""
        self._share = knobs.cpu_share

    def progress(self) -> ProgressCounters:
        """Zero counters: without a cgroup there is nothing to read."""
        return ProgressCounters()

    def kill_now(self, cause: Cause) -> None:
        """Record `cause`; the backend, which holds the pid, does the kill."""
        self._kill_cause = cause

    def death_cause(self, status: CreatureStatus) -> Cause:
        """The recorded kill cause, else a crash."""
        return self._kill_cause or Cause.CRASH

    def vitals(self) -> Vitals:
        """Real temperature and throttling, with the last applied CPU share."""
        return self._vitals.read(cores_effective=self._share)

    def facts(self) -> MachineFacts:
        """True machine facts: board, cores, nominal RAM."""
        return machine_facts(self._sys_paths)


def make_body(
    cfg: Config, fs: Path = CGROUP_FS, proc_cgroup: Path = Path("/proc/self/cgroup")
) -> Body:
    """The body for this config: the delegated cgroup on a Pi, a plain body elsewhere.

    `body.cgroups = "off"` always gives the plain body. With "auto", a Pi without a usable
    delegated cgroup is an error (the decline would not be real); the laptop falls back.
    """
    settings = CgroupSettings.from_config(cfg)
    if str(cfg.get("body.cgroups", "auto")) == "off":
        return PlainBody(settings)
    try:
        return CgroupBody.delegated(settings, fs, proc_cgroup)
    except (CgroupError, OSError) as e:
        if cfg.hw_class in ("pi4", "pi5"):
            raise CgroupError(f"cannot use the creature cgroup: {e}") from e
        log.info("no delegated cgroup (%s): running without limits", e)
        return PlainBody(settings)
