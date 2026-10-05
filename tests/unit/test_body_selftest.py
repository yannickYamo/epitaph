"""`epitaph selftest` against a fake cgroupfs and a fake clock helper."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from epitaph.body import selftest
from epitaph.body.cgroup import CgroupBody, CgroupSettings
from epitaph.body.cpuclock import CpuClock, read_max_mhz
from epitaph.body.selftest import (
    Check,
    check_binary,
    check_clock,
    check_delegation,
    check_kill,
    check_limits,
    in_epitaph_unit,
    relaunch_argv,
    report,
    run_checks,
)
from epitaph.body.vitals import SysPaths
from epitaph.cli import main
from epitaph.config import load_config
from tests.unit.test_body_cgroup import REL


@pytest.fixture
def body(tmp_path: Path) -> CgroupBody:
    root = tmp_path / "cgroup" / REL
    root.mkdir(parents=True)
    (root / "cgroup.procs").write_text("4242\n")
    (root / "cgroup.controllers").write_text("cpuset cpu io memory pids\n")
    (root / "cgroup.subtree_control").write_text("\n")
    b = CgroupBody(root, CgroupSettings(kill_wait_s=0.2), sys_paths=SysPaths())
    b.setup()
    (b.supervisor / "cgroup.procs").write_text("4242\n")
    (b.creature / "cgroup.procs").write_text("")
    return b


class FakeProc:
    pid = 777
    returncode: int | None = None

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0


def fake_spawn(b: CgroupBody, joins: bool = True) -> Any:
    def spawn(argv: list[str]) -> FakeProc:
        assert argv[3] == str(b.creature / "cgroup.procs")
        if joins:
            (b.creature / "cgroup.events").write_text("populated 1\n")
            (b.creature / "cpu.stat").write_text("usage_usec 300000\n")
        return FakeProc()

    return spawn


def test_delegation_and_limits_pass(body: CgroupBody) -> None:
    checks = check_delegation(body)
    assert all(c.ok for c in checks), checks
    assert check_limits(body).ok


def test_delegation_reports_missing_controller(body: CgroupBody) -> None:
    body.controllers = {"cpu", "memory"}
    failed = [c.name for c in check_delegation(body) if not c.ok]
    assert failed == ["controller io"]


def test_limits_fail_when_reset_does_not_clear(
    body: CgroupBody, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(body, "reset_creature_cgroup", lambda: None)
    c = check_limits(body)
    assert not c.ok and "after reset" in c.detail


def test_kill_check(body: CgroupBody, monkeypatch: pytest.MonkeyPatch) -> None:
    def kill_all() -> None:
        (body.creature / "cgroup.events").write_text("populated 0\n")

    monkeypatch.setattr(body, "_kill_all", kill_all)
    checks = check_kill(body, fake_spawn(body), settle_s=0)
    assert [(c.name, c.ok) for c in checks] == [
        ("spawn", True),
        ("progress counters", True),
        ("cgroup.kill", True),
    ]
    assert "usage_usec 300000" in checks[1].detail


def test_kill_check_fails_when_cgroup_stays_populated(body: CgroupBody) -> None:
    checks = check_kill(body, fake_spawn(body), settle_s=0)  # cgroup.kill is a plain file here
    assert [c.name for c in checks if not c.ok] == ["cgroup.kill"]


def test_kill_check_spawn_errors(body: CgroupBody) -> None:
    def broken(argv: list[str]) -> Any:
        raise FileNotFoundError(argv[0])

    assert not check_kill(body, broken)[0].ok
    checks = check_kill(body, fake_spawn(body, joins=False), join_timeout_s=0)
    assert checks == [Check("spawn", False, "the child never appeared in the creature cgroup")]


def cpufreq(tmp_path: Path) -> Path:
    tree = tmp_path / "cpufreq" / "policy0"
    tree.mkdir(parents=True)
    (tree / "scaling_max_freq").write_text("1800000\n")
    return tree.parent


def clock_writing(tree: Path, fail_at: str = "") -> CpuClock:
    def runner(argv: Sequence[str]) -> int:
        arg = argv[1]
        if arg == fail_at:
            return 1
        mhz = 1800 if arg == "reset" else int(arg)
        (tree / "policy0" / "scaling_max_freq").write_text(f"{mhz * 1000}\n")
        return 0

    return CpuClock("helper", runner)


def test_clock_round_trip(tmp_path: Path) -> None:
    tree = cpufreq(tmp_path)
    c = check_clock(clock_writing(tree), tree)
    assert c.ok, c.detail
    assert c.detail == "scaling_max_freq [1200] MHz, then [1800] MHz"


def test_clock_failures(tmp_path: Path) -> None:
    tree = cpufreq(tmp_path)
    assert not check_clock(clock_writing(tree), tmp_path / "none").ok
    assert not check_clock(clock_writing(tree, fail_at="1200"), tree).ok
    assert not check_clock(clock_writing(tree, fail_at="reset"), tree).ok
    noop = CpuClock("helper", lambda argv: 0)  # says yes, changes nothing
    assert not check_clock(noop, tree).ok
    assert read_max_mhz(tree) == [1200]  # left by the reset that "failed" above


def test_binary(tmp_path: Path) -> None:
    exe = tmp_path / "llama-server"
    assert not check_binary(str(exe)).ok
    exe.write_text("#!/bin/sh\n")
    assert not check_binary(str(exe)).ok
    exe.chmod(0o755)
    assert check_binary(str(exe)).ok


def test_run_checks_and_report(body: CgroupBody, tmp_path: Path, monkeypatch) -> None:
    def kill_all() -> None:
        (body.creature / "cgroup.events").write_text("populated 0\n")

    monkeypatch.setattr(body, "_kill_all", kill_all)
    monkeypatch.setattr(selftest, "SETTLE_S", 0)
    tree = cpufreq(tmp_path)
    exe = tmp_path / "llama-server"
    exe.write_text("")
    exe.chmod(0o755)
    cfg = load_config("pi4/default", "pi4-4gb")
    cfg.data["backend"]["bin"] = str(exe)
    checks = run_checks(cfg, body, clock_writing(tree), fake_spawn(body), tree)
    lines: list[str] = []
    assert report(checks, lines.append) == 0, lines
    assert lines[-1] == f"selftest: {len(checks)}/{len(checks)} passed"
    assert lines[0].startswith("PASS delegation")


def test_run_checks_without_delegation(tmp_path: Path, monkeypatch) -> None:
    def refuse(*a: object, **k: object) -> CgroupBody:
        raise selftest.CgroupError("not an epitaph* unit")

    monkeypatch.setattr(CgroupBody, "delegated", refuse)
    cfg = load_config("pi4/default", "dev")  # no clock helper on the laptop
    cfg.data["backend"]["bin"] = str(tmp_path / "missing")
    checks = run_checks(cfg)
    lines: list[str] = []
    assert report(checks, lines.append) == 1
    assert [c.name for c in checks] == ["delegation", "clock helper", "llama-server"]
    assert "skipped" in checks[1].detail
    assert lines[-1].endswith("failed: delegation, llama-server")


def test_relaunch_argv() -> None:
    argv = relaunch_argv(["py", "-m", "epitaph"], "pi", "epitaph-selftest-1", {"HOME": "/h"})
    assert argv[:3] == ["sudo", "-n", "systemd-run"]
    assert "--uid=pi" in argv and "--gid=pi" in argv and "Delegate=yes" in argv
    assert {"--pipe", "--wait", "--collect"} <= set(argv)
    assert "--setenv=HOME=/h" in argv
    assert argv[-3:] == ["py", "-m", "epitaph"]


def test_relaunch(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(selftest.shutil, "which", lambda name: "/usr/bin/" + name)
    args = argparse.Namespace(profile="pi4/default", hardware=None, user="root")
    assert selftest.relaunch(args, lambda argv: ran.append(argv) or 0) == 0
    assert "--setenv=HOME=/root" in ran[0] and "--uid=root" in ran[0]
    inner = ran[0][ran[0].index("--quiet") + 1 :]
    inner = [a for a in inner if not a.startswith("--setenv")]
    assert inner[1:] == ["-m", "epitaph", "selftest", "--inside", "--profile", "pi4/default"]
    monkeypatch.setattr(selftest.shutil, "which", lambda name: None)
    assert selftest.relaunch(args, lambda argv: 0) == 1


def test_in_epitaph_unit(tmp_path: Path) -> None:
    f = tmp_path / "cgroup"
    f.write_text("0::/system.slice/epitaph-selftest-9.service\n")
    assert in_epitaph_unit(f)
    f.write_text("0::/user.slice/user-1000.slice/session-3.scope\n")
    assert not in_epitaph_unit(f)
    assert not in_epitaph_unit(tmp_path / "none")


def test_cli_selftest(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    calls: list[argparse.Namespace] = []
    monkeypatch.setattr(selftest, "relaunch", lambda args: calls.append(args) or 0)
    monkeypatch.setattr(selftest, "in_epitaph_unit", lambda: False)
    assert main(["selftest", "--hardware", "dev", "--user", "pi"]) == 0
    assert calls[0].user == "pi" and not calls[0].inside
    monkeypatch.setattr(selftest, "run_checks", lambda cfg: [Check("x", False, "why")])
    assert main(["selftest", "--inside", "--profile", "pi4/default", "--hardware", "dev"]) == 1
    assert "FAIL x" in capsys.readouterr().out
