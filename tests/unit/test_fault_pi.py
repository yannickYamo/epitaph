"""tools/fault_pi.sh without a Pi: arguments, the lock, and the dry run (BUILD_PLAN 10.4)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FAULT = ROOT / "tools" / "fault_pi.sh"


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(FAULT), *args], capture_output=True, text=True, check=False, env=env
    )


def test_parses_and_is_executable() -> None:
    assert FAULT.stat().st_mode & 0o111
    assert subprocess.run(["bash", "-n", str(FAULT)], check=False).returncode == 0


@pytest.mark.parametrize("args", [[], ["power-cut"], ["crash", "--reboot"]])
def test_refuses_unknown_rows(args: list[str]) -> None:
    assert run(*args).returncode == 2


def test_refuses_without_the_lock(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "EPITAPH_LOCK_DIR": str(tmp_path)}
    out = run("crash", env=env)
    assert out.returncode == 2 and "under the Pi lock" in out.stderr


def test_dry_run_prints_every_row() -> None:
    out = run("all", "--dry-run", env={"PATH": "/usr/bin:/bin", "PI_HOST": "pi"})
    assert out.returncode == 0, out.stderr
    rows = [line.split()[2] for line in out.stdout.splitlines() if line.startswith("== fault ")]
    assert rows == ["netblock", "two-controllers", "crash", "controller-kill", "hang"]
    assert "PASS" not in out.stdout and "FAIL" not in out.stdout
    for cmd in ("kill -9 4242", "kill -STOP 4242", "systemctl kill -s KILL epitaph-controller"):
        assert cmd.replace(" ", "\\ ") in out.stderr
    assert out.stdout.rstrip().endswith("dry run: 5 row(s) printed, nothing run")
