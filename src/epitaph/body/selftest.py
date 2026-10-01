# pyright: strict
"""`epitaph selftest`: can this machine take the creature's resources away? (BUILD_PLAN 9 C6)

The checks are spike S3b's, made permanent:

- the delegated cgroup: `+memory +cpu +io` in the subtree, the supervisor and creature leaves
- limits set and cleared (cpu.max, memory.max, memory.swap.max, memory.oom.group)
- `cgroup.kill` empties the creature cgroup, and its progress counters rise
- the CPU clock helper round trip: 1200 MHz, then back to 1800 (ADR-025)
- the llama-server binary is present and executable

Delegation only exists inside a `Delegate=yes` unit. Run from a shell, selftest relaunches
itself as a transient system unit for the service user (`sudo systemd-run --uid=<user>
-p Delegate=yes`), the way the controller runs; `--inside` runs the checks in place (what the
unit executes). Exit status 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import pwd
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from epitaph.body.cgroup import CgroupBody, CgroupError, CgroupSettings
from epitaph.body.cpuclock import CPUFREQ, MAX_MHZ, CpuClock, read_max_mhz
from epitaph.types import Cause

if TYPE_CHECKING:
    from epitaph.config import Config

CLOCK_PROBE_MHZ = 1200
SETTLE_S = 0.6  # how long the busy child runs before the counters are read
JOIN_TIMEOUT_S = 5.0  # how long the child may take to appear in the creature cgroup
UNIT_PREFIX = "epitaph-selftest"
# Burns about 0.3 s of CPU, then waits to be killed: the counters must move, the kill must work.
BUSY_CHILD = "import time\nt = time.time()\nwhile time.time() - t < 0.3: pass\ntime.sleep(120)"


@dataclass(frozen=True)
class Check:
    """One check's outcome."""

    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        """`PASS name  detail` (or FAIL)."""
        return f"{'PASS' if self.ok else 'FAIL'} {self.name:<18} {self.detail}".rstrip()


class Spawn(Protocol):
    """Starts argv and returns a handle that can be polled and waited on."""

    def __call__(self, argv: list[str]) -> subprocess.Popen[bytes]:
        """Start `argv`."""
        ...


def _popen(argv: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)


# --- the checks --------------------------------------------------------------------------


def check_delegation(body: CgroupBody) -> list[Check]:
    """The controllers in the subtree and the two leaves (setup() has run)."""
    out = [
        Check(
            f"controller {c}",
            c in body.controllers,
            "enabled in subtree_control" if c in body.controllers else "not delegated",
        )
        for c in ("memory", "cpu", "io")
    ]
    leaves = [p.name for p in (body.supervisor, body.creature) if (p / "cgroup.procs").exists()]
    out.append(Check("leaves", len(leaves) == 2, " + ".join(leaves) or "none created"))
    return out


def check_limits(body: CgroupBody) -> Check:
    """cpu.max and memory.max set, read back, then cleared by reset_creature_cgroup()."""
    c = body.creature
    want_cpu, want_mem = "50000 100000", str(256 * 1024 * 1024)
    try:
        (c / "cpu.max").write_text(want_cpu)
        (c / "memory.max").write_text(want_mem)
        got = ((c / "cpu.max").read_text().strip(), (c / "memory.max").read_text().strip())
        body.reset_creature_cgroup()
        cleared = {
            name: (c / name).read_text().strip()
            for name in ("cpu.max", "memory.max", "memory.swap.max", "memory.oom.group")
        }
    except OSError as e:
        return Check("limits", False, str(e))
    want_cleared = {
        "cpu.max": f"max {body.settings.cpu_period_us}",
        "memory.max": "max",
        "memory.swap.max": "0",
        "memory.oom.group": "1",
    }
    if got != (want_cpu, want_mem):
        return Check("limits", False, f"set {want_cpu!r}/{want_mem!r}, read back {got}")
    if cleared != want_cleared:
        return Check("limits", False, f"after reset: {cleared}")
    return Check("limits", True, "cpu.max, memory.max set and cleared; swap 0, oom.group 1")


def check_kill(
    body: CgroupBody,
    spawn: Spawn = _popen,
    settle_s: float | None = None,
    join_timeout_s: float | None = None,
) -> list[Check]:
    """A busy child in the creature cgroup: counters rise, cgroup.kill empties the cgroup.

    `settle_s` and `join_timeout_s` default to SETTLE_S and JOIN_TIMEOUT_S.
    """
    settle_s = SETTLE_S if settle_s is None else settle_s
    join_timeout_s = JOIN_TIMEOUT_S if join_timeout_s is None else join_timeout_s
    argv = body.wrap_spawn([sys.executable, "-c", BUSY_CHILD])
    try:
        proc = spawn(argv)
    except OSError as e:
        return [Check("spawn", False, str(e))]
    try:
        deadline = time.monotonic() + join_timeout_s
        while not body.populated() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not body.populated():
            return [Check("spawn", False, "the child never appeared in the creature cgroup")]
        if settle_s > 0:
            time.sleep(settle_s)
        counters = body.progress()
        body.kill_now(Cause.MANUAL)
        empty = body.wait_empty()
    finally:
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
    events = body.memory_events()
    return [
        Check("spawn", True, f"pid {proc.pid} joined the creature cgroup"),
        Check(
            "progress counters",
            counters.cpu_usec > 0,
            f"usage_usec {counters.cpu_usec}, io rbytes {counters.io_rbytes}, "
            f"majfault {counters.majfault}",
        ),
        Check(
            "cgroup.kill",
            empty,
            f"cgroup empty, child exit {proc.returncode}, memory.events oom_kill "
            f"{events.get('oom_kill', 0)}"
            if empty
            else f"still populated {body.settings.kill_wait_s:g} s after cgroup.kill",
        ),
    ]


def check_clock(clock: CpuClock, cpufreq: Path = CPUFREQ) -> Check:
    """The helper caps every policy at 1200 MHz, then restores 1800 (always restored)."""
    if not read_max_mhz(cpufreq):
        return Check("clock helper", False, f"no cpufreq policy under {cpufreq}")
    try:
        if not clock.set(CLOCK_PROBE_MHZ):
            return Check("clock helper", False, f"{clock.helper} {CLOCK_PROBE_MHZ} failed")
        low = read_max_mhz(cpufreq)
    finally:
        restored = clock.reset()
    high = read_max_mhz(cpufreq)
    ok = restored and set(low) == {CLOCK_PROBE_MHZ} and set(high) == {MAX_MHZ}
    return Check("clock helper", ok, f"scaling_max_freq {low} MHz, then {high} MHz")


def check_binary(path: str) -> Check:
    """The llama-server binary exists and is executable."""
    p = Path(path).expanduser()
    ok = p.is_file() and os.access(p, os.X_OK)
    return Check("llama-server", ok, str(p) if ok else f"{p} missing or not executable")


def run_checks(
    cfg: Config,
    body: CgroupBody | None = None,
    clock: CpuClock | None = None,
    spawn: Spawn = _popen,
    cpufreq: Path = CPUFREQ,
) -> list[Check]:
    """Every check, in order; `body` defaults to the delegated cgroup this process runs in."""
    settings = CgroupSettings.from_config(cfg)
    checks: list[Check] = []
    if body is None:
        try:
            body = CgroupBody.delegated(settings)
        except (CgroupError, OSError) as e:
            checks.append(Check("delegation", False, str(e)))
    if body is not None:
        checks.append(Check("delegation", True, str(body.root)))
        checks += check_delegation(body)
        checks.append(check_limits(body))
        checks += check_kill(body, spawn)
    helper = settings.clock_helper
    if clock is None and helper:
        clock = CpuClock(helper)
    if clock is not None:
        checks.append(check_clock(clock, cpufreq))
    else:
        checks.append(Check("clock helper", True, "skipped: no body.clock_helper on this machine"))
    checks.append(check_binary(str(cfg.get("backend.bin", ""))))
    return checks


def report(checks: Sequence[Check], write: Callable[[str], object] = print) -> int:
    """Print one line per check and a summary; 0 if every check passed, else 1."""
    for c in checks:
        write(c.line())
    failed = [c.name for c in checks if not c.ok]
    write(
        f"selftest: {len(checks) - len(failed)}/{len(checks)} passed"
        + (f"; failed: {', '.join(failed)}" if failed else "")
    )
    return 1 if failed else 0


# --- relaunching inside a delegated unit ---------------------------------------------------


def relaunch_argv(
    inner: Sequence[str],
    user: str,
    unit: str,
    env: dict[str, str],
    systemd_run: str = "systemd-run",
) -> list[str]:
    """`sudo systemd-run` argv running `inner` as `user` in a Delegate=yes system unit.

    `--pipe --wait` streams its output and returns its exit status; `--collect` leaves
    nothing behind, even on failure.
    """
    argv = [
        "sudo", "-n", systemd_run,
        f"--unit={unit}", f"--uid={user}", f"--gid={user}",
        "-p", "Delegate=yes", "-p", "CPUAffinity=0",
        "--pipe", "--wait", "--collect", "--quiet",
    ]  # fmt: skip
    argv += [f"--setenv={k}={v}" for k, v in sorted(env.items())]
    return argv + list(inner)


def in_epitaph_unit(proc_cgroup: Path = Path("/proc/self/cgroup")) -> bool:
    """Whether this process already runs in an epitaph* unit's cgroup."""
    try:
        text = proc_cgroup.read_text()
    except OSError:
        return False
    return any(seg.startswith("epitaph") for seg in text.replace("/", " ").split())


def add_arguments(p: argparse.ArgumentParser) -> None:
    """The flags of `epitaph selftest` (besides the common --profile/--hardware)."""
    p.add_argument("--inside", action="store_true", help="run the checks in this process")
    p.add_argument("--user", help="service user for the relaunch (default: the current user)")


def relaunch(args: argparse.Namespace, run: Callable[[list[str]], int] | None = None) -> int:
    """Run `epitaph selftest --inside` in a transient Delegate=yes unit; its exit status."""
    if shutil.which("systemd-run") is None:
        print("selftest: systemd-run not found; run with --inside in a Delegate=yes unit")
        return 1
    inner = [sys.executable, "-m", "epitaph", "selftest", "--inside"]
    for flag in ("profile", "hardware"):
        value = getattr(args, flag, None)
        if value:
            inner += [f"--{flag}", str(value)]
    keep = ("EPITAPH_CONFIG_DIR", "PYTHONPATH")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    user = args.user or getpass.getuser()
    # The service user's home, not the caller's: backend.bin is "~/llama.cpp/...".
    with contextlib.suppress(KeyError):
        env["HOME"] = pwd.getpwnam(user).pw_dir
    argv = relaunch_argv(inner, user, f"{UNIT_PREFIX}-{os.getpid()}", env)
    return (run or _call)(argv)


def _call(argv: list[str]) -> int:
    return subprocess.run(argv, check=False).returncode
