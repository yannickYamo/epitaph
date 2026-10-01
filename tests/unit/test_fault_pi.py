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


def test_hang_waits_for_a_busy_creature_not_the_transcript() -> None:
    """Regression (phase 2, first Pi run): the hang row waited for a gen_start in events.jsonl,
    which the transcript flushes only at the end of each thought, so it never saw one in flight
    and gave up after 600 s. It now waits for the creature's CPU time to move (cpu.stat)."""
    text = FAULT.read_text()
    hang = text[text.index("row_hang() {") :]
    hang = hang[: hang.index("\n}\n")]
    assert "wait_for 600 busy" in hang and "events.jsonl" not in hang
    busy = text[text.index("busy() {") :]
    assert "usage_usec" in busy[: busy.index("\n}\n")]


def test_the_matrix_row_names_are_accepted() -> None:
    """tools/fault_matrix_pi.sh names rows as BUILD_PLAN 10.4 does; fault_pi.sh maps them."""
    for row, own in (("creature-network", "netblock"), ("controller-killed", "controller-kill")):
        out = subprocess.run(
            ["bash", str(FAULT), row, "--dry-run"], capture_output=True, text=True, check=True
        )
        assert f"== fault {own}" in out.stdout
