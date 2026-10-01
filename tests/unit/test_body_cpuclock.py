"""The CPU clock cap: the body's calls to the helper, and the helper script itself (ADR-025)."""

from __future__ import annotations

import shutil
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from epitaph.body import cpuclock
from epitaph.body.cgroup import CgroupBody, CgroupSettings, make_body
from epitaph.body.cpuclock import CpuClock, clamp_mhz, read_max_mhz
from epitaph.body.vitals import SysPaths
from epitaph.config import load_config
from epitaph.types import Cause, CreatureStatus
from tests.unit.test_body_cgroup import REL, knobs

ROOT = Path(__file__).resolve().parents[2]
HELPER_SCRIPT = ROOT / "deploy" / "sbin" / "epitaph-clock"


class FakeRunner:
    """Records helper calls; fails while `rc` is non-zero."""

    def __init__(self, rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.rc = rc

    def __call__(self, argv: Sequence[str]) -> int:
        self.calls.append(list(argv))
        return self.rc

    @property
    def args(self) -> list[str]:
        return [c[1] for c in self.calls]


@pytest.fixture
def fs(tmp_path: Path) -> Path:
    fs = tmp_path / "cgroup"
    root = fs / REL
    root.mkdir(parents=True)
    (root / "cgroup.procs").write_text("4242\n")
    (root / "cgroup.controllers").write_text("cpuset cpu io memory pids\n")
    (root / "cgroup.subtree_control").write_text("\n")
    return fs


def body_with(fs: Path, runner: FakeRunner) -> CgroupBody:
    body = CgroupBody(fs / REL, CgroupSettings(), sys_paths=SysPaths(), clock=CpuClock("h", runner))
    body.setup()
    return body


def test_clamp() -> None:
    assert clamp_mhz(1199.6) == 1200
    assert clamp_mhz(100) == 600
    assert clamp_mhz(5000) == 1800


def test_set_only_on_change_and_reset_always() -> None:
    r = FakeRunner()
    c = CpuClock("/x/epitaph-clock", r)
    assert c.set(1800) and c.set(1800)
    assert c.set(1400) and c.set(1400.2)
    assert c.set(600)
    assert c.reset() and c.reset()
    assert r.calls[0] == ["/x/epitaph-clock", "reset"]
    assert r.args == ["reset", "1400", "600", "reset", "reset"]
    assert c.mhz == 1800


def test_failure_is_not_retried_every_thought() -> None:
    r = FakeRunner(rc=1)
    c = CpuClock("h", r)
    assert not c.set(1400)
    assert not c.set(1400)  # same value: no second call, no second error
    assert c.mhz is None and c.failures == 1
    r.rc = 0
    assert c.set(900)  # the next change tries again
    assert r.args == ["1400", "900"] and c.mhz == 900


def test_missing_helper_never_calls_sudo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    called: list[Sequence[str]] = []
    monkeypatch.setattr(cpuclock, "sudo_runner", lambda argv: called.append(argv) or 0)
    c = CpuClock(str(tmp_path / "absent"))
    assert not c.reset()
    assert called == [] and c.failures == 1
    helper = tmp_path / "present"
    helper.write_text("")
    assert CpuClock(str(helper)).set(700)
    assert called == [[str(helper), "700"]]


def test_background_clock_never_blocks_the_caller() -> None:
    """Regression: sudo (up to 10 s) ran on the controller's event loop and could stall the
    watchdog pings. In the background the call returns at once and runs in order."""
    gate = threading.Event()
    r = FakeRunner()

    def slow(argv: Sequence[str]) -> int:
        gate.wait(5)
        return r(argv)

    c = CpuClock("h", slow, background=True)
    assert c.set(900) and c.set(900) and c.reset()  # none of these waits for the helper
    assert r.calls == [] and c.mhz is None
    gate.set()
    c.wait_idle()
    assert r.args == ["900", "reset"] and c.mhz == 1800


def test_the_controller_body_runs_the_clock_in_the_background(fs: Path) -> None:
    body = CgroupBody(fs / REL, CgroupSettings(clock_helper="/absent/helper"), sys_paths=SysPaths())
    assert body.clock is not None
    assert body.clock._pool is not None  # pyright: ignore[reportPrivateUsage]


def test_body_sets_clock_only_when_it_changes(fs: Path) -> None:
    r = FakeRunner()
    body = body_with(fs, r)
    assert r.args == ["reset"]  # setup: a crashed controller may have left it capped
    for mhz in (1800, 1800, 1400, 1400, 900, 600, 600):
        body.apply(replace(knobs(), cpu_mhz=mhz))
    assert r.args == ["reset", "1400", "900", "600"]


def test_full_clock_restored_at_every_death(fs: Path) -> None:
    r = FakeRunner()
    body = body_with(fs, r)
    died = CreatureStatus(alive=False, pid=1, signal=9)
    body.apply(replace(knobs(), cpu_mhz=600))
    body.kill_now(Cause.DEADLINE)  # the deadline, a hang, a manual kill
    assert r.args[-1] == "reset"
    n = len(r.calls)
    body.death_cause(died)  # already at full clock: no extra call
    assert len(r.calls) == n
    body.apply(replace(knobs(), cpu_mhz=900))
    body.death_cause(died)  # an OOM or a crash never goes through kill_now
    assert r.args[-2:] == ["900", "reset"]
    body.reset_creature_cgroup()  # the next birth: always called
    assert r.args[-1] == "reset" and len(r.calls) == n + 3


def test_failed_reset_is_tried_again_at_the_next_death(fs: Path) -> None:
    r = FakeRunner()
    body = body_with(fs, r)
    body.apply(replace(knobs(), cpu_mhz=600))
    r.rc = 1
    body.kill_now(Cause.DEADLINE)
    r.rc = 0
    body.death_cause(CreatureStatus(alive=False))
    assert r.args[-2:] == ["reset", "reset"] and body.clock is not None
    assert body.clock.mhz == 1800


def test_no_helper_no_clock(fs: Path) -> None:
    body = CgroupBody(fs / REL, CgroupSettings(), sys_paths=SysPaths())
    assert body.clock is None
    body.setup()
    body.apply(replace(knobs(), cpu_mhz=600))  # nothing to call
    body.kill_now(Cause.DEADLINE)


def test_pi_overlay_wires_the_helper(fs: Path, tmp_path: Path) -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    assert CgroupSettings.from_config(cfg).clock_helper == cpuclock.HELPER
    assert CgroupSettings.from_config(load_config("pi4/default", "dev")).clock_helper == ""
    (tmp_path / "self_cgroup").write_text(f"0::/{REL}\n")
    cfg.data["body"]["netblock_helper"] = ""  # not installed on this machine
    body = make_body(cfg, fs, tmp_path / "self_cgroup")
    assert isinstance(body, CgroupBody) and body.clock is not None
    assert body.clock.helper == cpuclock.HELPER


def test_read_max_mhz(tmp_path: Path) -> None:
    for i, khz in enumerate((1200000, 1800000)):
        (tmp_path / f"policy{i}").mkdir()
        (tmp_path / f"policy{i}" / "scaling_max_freq").write_text(f"{khz}\n")
    (tmp_path / "policy9").mkdir()
    (tmp_path / "policy9" / "scaling_max_freq").write_text("junk")
    assert read_max_mhz(tmp_path) == [1200, 1800]
    assert read_max_mhz(tmp_path / "none") == []


# --- the helper script, run against a fake cpufreq tree (never through sudo) -----------------


@pytest.fixture
def helper(tmp_path: Path) -> tuple[Path, Path]:
    tree = tmp_path / "cpufreq"
    for i, hw in ((0, 1800000), (4, 1500000)):
        p = tree / f"policy{i}"
        p.mkdir(parents=True)
        (p / "scaling_max_freq").write_text(f"{hw}\n")
        (p / "cpuinfo_max_freq").write_text(f"{hw}\n")
    script = tmp_path / "epitaph-clock"
    text = HELPER_SCRIPT.read_text()
    assert "CPUFREQ=/sys/devices/system/cpu/cpufreq\n" in text
    script.write_text(text.replace("CPUFREQ=/sys/devices/system/cpu/cpufreq", f"CPUFREQ={tree}"))
    return script, tree


def run_helper(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(script), *args], capture_output=True, text=True, timeout=10, check=False
    )


def test_helper_sets_every_policy_and_resets(helper: tuple[Path, Path]) -> None:
    script, tree = helper
    out = run_helper(script, "1200")
    assert out.returncode == 0, out.stderr
    assert read_max_mhz(tree) == [1200, 1200]
    assert "2 policies" in out.stdout
    assert run_helper(script, "1700").returncode == 0
    assert read_max_mhz(tree) == [1700, 1500]  # capped at policy4's hardware maximum
    assert run_helper(script, "reset").returncode == 0
    assert read_max_mhz(tree) == [1800, 1500]


@pytest.mark.parametrize(
    "args",
    [(), ("599",), ("1801",), ("0600",), ("12e2",), ("-1",), ("1200", "x"), ("1200.5",),
     ("$(id)",), ("--help",), ("",), ("99999",)],
)  # fmt: skip
def test_helper_rejects_everything_else(helper: tuple[Path, Path], args: tuple[str, ...]) -> None:
    script, tree = helper
    out = run_helper(script, *args)
    assert out.returncode == 2
    assert read_max_mhz(tree) == [1800, 1500]  # untouched


def test_helper_without_cpufreq(tmp_path: Path) -> None:
    script = tmp_path / "epitaph-clock"
    text = HELPER_SCRIPT.read_text()
    script.write_text(text.replace("/sys/devices/system/cpu/cpufreq", str(tmp_path / "none")))
    assert run_helper(script, "1200").returncode == 1


def test_helper_is_executable_and_posix() -> None:
    assert HELPER_SCRIPT.stat().st_mode & 0o111
    if shutil.which("dash"):
        out = subprocess.run(
            ["dash", "-n", str(HELPER_SCRIPT)], capture_output=True, text=True, check=False
        )
        assert out.returncode == 0, out.stderr
