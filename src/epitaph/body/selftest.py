# pyright: strict
"""`epitaph selftest`: can this machine take the creature's resources away? (BUILD_PLAN 9 C6)

The checks are spike S3b's, made permanent:

- the delegated cgroup: `+memory +cpu +io` in the subtree, the supervisor and creature leaves
- limits set and cleared (cpu.max, memory.max, memory.swap.max, memory.oom.group)
- `cgroup.kill` empties the creature cgroup, and its progress counters rise
- the creature has no network: the nftables rule names its cgroup id, and from inside the
  creature cgroup an outbound TCP connect is refused while 127.0.0.1 answers (ADR-005)
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
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from epitaph.body.cgroup import CgroupBody, CgroupError, CgroupSettings
from epitaph.body.cpuclock import CPUFREQ, MAX_MHZ, CpuClock, read_max_mhz
from epitaph.body.netblock import BLOCKED
from epitaph.types import Cause

if TYPE_CHECKING:
    from epitaph.config import Config

CLOCK_PROBE_MHZ = 1200
SETTLE_S = 0.6  # how long the busy child runs before the counters are read
JOIN_TIMEOUT_S = 5.0  # how long the child may take to appear in the creature cgroup
UNIT_PREFIX = "epitaph-selftest"
# Burns about 0.3 s of CPU, then waits to be killed: the counters must move, the kill must work.
BUSY_CHILD = "import time\nt = time.time()\nwhile time.time() - t < 0.3: pass\ntime.sleep(120)"
# Run inside the creature cgroup: an outbound TCP connect, then one to a listener of its own
# on 127.0.0.1. Prints {"out": "connected" | the error, "lo": ...} as one JSON line.
NET_PROBE = """
import json, socket, sys
host, port = sys.argv[1].rsplit(":", 1)
def connect(addr):
    s = socket.socket(socket.AF_INET6 if ":" in addr[0] else socket.AF_INET)
    s.settimeout(4)
    try:
        s.connect(addr)
        return "connected"
    except OSError as e:
        return type(e).__name__ + ": " + (e.strerror or str(e))
    finally:
        s.close()
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(1)
print(json.dumps({"out": connect((host.strip("[]"), int(port))), "lo": connect(srv.getsockname())}))
"""
DEFAULT_PROBE = "1.1.1.1:443"


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


def probe_connect(target: str, timeout_s: float = 4.0) -> str:
    """ "connected", or the error, for a TCP connect to `target` ("host:port") from here."""
    host, _, port = target.rpartition(":")
    try:
        with socket.create_connection((host.strip("[]"), int(port)), timeout=timeout_s):
            return "connected"
    except OSError as e:
        return f"{type(e).__name__}: {e.strerror or e}"


def check_network(
    body: CgroupBody,
    target: str = DEFAULT_PROBE,
    run: Callable[[list[str]], str] | None = None,
    control: Callable[[str], str] = probe_connect,
) -> Check:
    """The creature's network is blocked: the rule, a refused connect, loopback still works.

    A child in the creature cgroup connects to `target` (it must fail; with the rule it is
    refused at once) and to a listener of its own on 127.0.0.1 (it must connect). The same
    connect from here, outside the creature cgroup, is reported for comparison: when it
    connects too, the refusal is the rule's doing and not an offline machine.
    """
    if body.settings.creature_network != BLOCKED:
        return Check("network", True, "skipped: body.creature_network allows it")
    if body.netblock is None:
        return Check("network", True, "skipped: no body.netblock_helper on this machine")
    if not body.netblock.verify(body.creature, body.fs):
        return Check("network", False, "no nftables rule names the creature cgroup's id")
    argv = body.wrap_spawn([sys.executable, "-c", NET_PROBE, target])
    try:
        out = (run or _probe_output)(argv)
        res = json.loads(out.strip().splitlines()[-1])
        inside, lo = str(res["out"]), str(res["lo"])
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as e:
        return Check("network", False, f"probe failed: {e}")
    outside = control(target)
    # With the rule the connect is refused at once; on a machine with no route out it fails
    # earlier (unreachable). Either way nothing leaves; the detail says which.
    ok = inside != "connected" and lo == "connected"
    return Check(
        "network",
        ok,
        f"creature -> {target}: {inside}; creature -> 127.0.0.1: {lo}; "
        f"outside the cgroup -> {target}: {outside}",
    )


def _probe_output(argv: list[str]) -> str:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=20, check=True, stdin=subprocess.DEVNULL
    ).stdout


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
    net_run: Callable[[list[str]], str] | None = None,
    net_control: Callable[[str], str] = probe_connect,
) -> list[Check]:
    """Every check, in order; `body` defaults to the delegated cgroup this process runs in
    (its network rule is removed again at the end)."""
    settings = CgroupSettings.from_config(cfg)
    checks: list[Check] = []
    own = body is None
    if body is None:
        try:
            body = CgroupBody.delegated(settings)
        except (CgroupError, OSError) as e:
            checks.append(Check("delegation", False, str(e)))
    if body is not None:
        try:
            checks.append(Check("delegation", True, str(body.root)))
            checks += check_delegation(body)
            checks.append(check_limits(body))
            checks += check_kill(body, spawn)
            target = str(cfg.get("body.netblock_probe", DEFAULT_PROBE))
            checks.append(check_network(body, target, net_run, net_control))
        finally:
            if own:
                body.reset_creature_cgroup()
                body.release_network()
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


def relaunch(
    args: argparse.Namespace,
    run: Callable[[list[str]], int] | None = None,
    command: str = "selftest",
    extra: Sequence[str] = (),
    unit_prefix: str = UNIT_PREFIX,
) -> int:
    """Run `epitaph <command> --inside [extra]` in a transient Delegate=yes unit; its exit
    status. `selftest` by default; `calibrate` uses it too."""
    if shutil.which("systemd-run") is None:
        print(f"{command}: systemd-run not found; run with --inside in a Delegate=yes unit")
        return 1
    inner = [sys.executable, "-m", "epitaph", command, "--inside", *extra]
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
    argv = relaunch_argv(inner, user, f"{unit_prefix}-{os.getpid()}", env)
    return (run or _call)(argv)


def _call(argv: list[str]) -> int:
    return subprocess.run(argv, check=False).returncode
