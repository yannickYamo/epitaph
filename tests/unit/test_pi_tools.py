"""tools/smoke_pi.sh and tools/headless_boot_check.sh against a fake Pi (card E4).

The fake Pi is a folder of stand-in commands on PATH: `ssh` runs the remote command locally,
`sudo` drops itself, `systemd-run` runs the unit in the foreground, `systemctl` answers from
files, and `epitaph run` copies simulated lives into a fake state dir. Everything else in the
scripts is real: the Pi lock (in a temporary lock dir), the copy over `ssh ... tar`, and
verify-life on the copied lives. No network, no Pi, no waiting.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "tools" / "smoke_pi.sh"
BOOT = ROOT / "tools" / "headless_boot_check.sh"

FAKES = {
    "ssh": """
while [ "${1:-}" = -o ]; do shift 2; done
shift
printf '%s\\n' "$*" >> "$FAKE_DIR/ssh.log"
exec bash -c "$*"
""",
    "sudo": """
[ "${1:-}" = -n ] && shift
exec "$@"
""",
    "systemd-run": """
[ -z "${FAKE_SYSTEMD_RUN_RC:-}" ] || exit "$FAKE_SYSTEMD_RUN_RC"
printf '%s\n' "$*" > "$FAKE_DIR/systemd-run.args"
while [ "${1#--}" != "$1" ]; do shift; done
"$@"
""",
    "journalctl": """
echo "fake journal: $*"
""",
    "systemctl": """
cmd="$1"; shift
unit=""
for a in "$@"; do case "$a" in -*) ;; *) unit="$a" ;; esac; done
case "$cmd" in
  is-active)
    if [ "$unit" = epitaph-controller ] && [ -f "$FAKE_DIR/controller.active" ]; then
      echo active; exit 0
    fi
    echo inactive; exit 3 ;;
  show) [ -f "$FAKE_DIR/show-$unit" ] && cat "$FAKE_DIR/show-$unit"; exit 0 ;;
  stop|start)
    echo "$cmd $unit" >> "$FAKE_DIR/systemctl.log"
    if [ "$unit" = epitaph-controller ]; then
      if [ "$cmd" = stop ]; then rm -f "$FAKE_DIR/controller.active"
      else touch "$FAKE_DIR/controller.active"; fi
    fi ;;
  is-system-running) echo running ;;
  list-units) ;;
esac
""",
    "epitaph": """
[ "$1" = run ] || exit 2
lives=1
while [ $# -gt 0 ]; do [ "$1" = --lives ] && lives="$2"; shift; done
mkdir -p "$EPITAPH_PI_STATE/lives"
for d in $(ls "$FAKE_LIVES" | sort | head -n "$lives"); do
  cp -r "$FAKE_LIVES/$d" "$EPITAPH_PI_STATE/lives/"
done
exit "${FAKE_EPITAPH_RC:-0}"
""",
}


@pytest.fixture
def fake_pi(tmp_path: Path, recorded_life) -> dict[str, str]:
    """Environment for the scripts with the fake Pi on PATH; FAKE_DIR is tmp_path."""
    if shutil.which("flock") is None or shutil.which("timeout") is None:
        pytest.skip("needs flock and timeout (util-linux, coreutils)")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in FAKES.items():
        path = bin_dir / name
        path.write_text("#!/usr/bin/env bash\n" + body.lstrip())
        path.chmod(0o755)
    source = recorded_life("pi4/smoke-300", lives=2)
    lives = tmp_path / "sim-lives"
    shutil.copytree(source / "lives", lives)
    for sub in ("pi-state", "pi-tmp", "locks"):
        (tmp_path / sub).mkdir()
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),  # the Pi's ~/epitaph/.venv is looked up under HOME
        "FAKE_DIR": str(tmp_path),
        "FAKE_LIVES": str(lives),
        "PI_HOST": "pi",
        "EPITAPH_SSH": str(bin_dir / "ssh"),
        "EPITAPH_PI_BIN": str(bin_dir / "epitaph"),
        "EPITAPH_PI_STATE": str(tmp_path / "pi-state"),
        "EPITAPH_PI_TMP": str(tmp_path / "pi-tmp"),
        "EPITAPH_LOCK_DIR": str(tmp_path / "locks"),
        "EPITAPH_POLL_S": "0",
        "EPITAPH_PYTHON": sys.executable,
        "AGENT": "test",
    }


def run(script: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args], env=env, capture_output=True, text=True, timeout=120
    )


# -- smoke_pi.sh --------------------------------------------------------------------------


def test_smoke_runs_copies_and_verifies_one_life(fake_pi: dict[str, str], tmp_path: Path) -> None:
    (tmp_path / "controller.active").touch()
    out = tmp_path / "out"
    res = run(SMOKE, fake_pi, "--out", str(out))
    assert res.returncode == 0, res.stderr + res.stdout
    verdict = out / "lives" / "000001" / "verify.json"
    assert verdict.is_file()
    assert '"ok": true' in verdict.read_text()
    assert '"level": "smoke"' in verdict.read_text()
    assert not (out / "lives" / "000002").exists()
    assert "fake journal" in (out / "journal.txt").read_text()
    # The installed controller stepped aside for the run and came back after it.
    assert (tmp_path / "systemctl.log").read_text().split("\n")[:2] == [
        "stop epitaph-controller",
        "start epitaph-controller",
    ]
    assert (tmp_path / "controller.active").exists()
    assert "PASS: 1 life(s) of pi4/smoke-300" in res.stderr


def test_two_lives_judge_the_next_birth(fake_pi: dict[str, str], tmp_path: Path) -> None:
    out = tmp_path / "out"
    res = run(SMOKE, fake_pi, "--out", str(out), "--lives", "2")
    assert res.returncode == 0, res.stderr + res.stdout
    first = json.loads((out / "lives" / "000001" / "verify.json").read_text())
    assert [c["status"] for c in first["checks"] if c["name"] == "next_birth"] == ["pass"]
    assert (out / "lives" / "000002" / "verify.json").is_file()
    assert not (tmp_path / "systemctl.log").exists()  # no controller was running


def test_only_new_lives_are_copied(fake_pi: dict[str, str], tmp_path: Path) -> None:
    old = Path(fake_pi["EPITAPH_PI_STATE"]) / "lives" / "000001"
    old.mkdir(parents=True)
    (old / "events.jsonl").write_text("")
    res = run(SMOKE, fake_pi, "--out", str(tmp_path / "out"))
    # The fake copies life 1 over the old one, so nothing is newer than 000001.
    assert res.returncode == 1
    assert "1 life(s) asked for, 0 recorded" in res.stderr


def test_a_failed_run_fails_the_smoke(fake_pi: dict[str, str], tmp_path: Path) -> None:
    res = run(SMOKE, {**fake_pi, "FAKE_EPITAPH_RC": "3"}, "--out", str(tmp_path / "out"))
    assert res.returncode == 1
    assert "epitaph run exited 3" in res.stderr


def test_the_unit_runs_like_the_installed_controller(
    fake_pi: dict[str, str], tmp_path: Path
) -> None:
    assert run(SMOKE, fake_pi, "--out", str(tmp_path / "out")).returncode == 0
    args = (tmp_path / "systemd-run.args").read_text()
    assert "--property=Delegate=yes" in args  # the creature's cgroup needs a delegated tree
    assert "--property=CPUAffinity=0" in args
    assert "run --profile pi4/smoke-300 --lives 1" in args


def test_a_unit_that_cannot_start_fails_and_restores_the_controller(
    fake_pi: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "controller.active").touch()
    res = run(SMOKE, {**fake_pi, "FAKE_SYSTEMD_RUN_RC": "1"}, "--out", str(tmp_path / "out"))
    assert res.returncode == 1
    assert "could not start" in res.stderr
    assert (tmp_path / "controller.active").exists()


def test_a_broken_life_fails_the_smoke(fake_pi: dict[str, str], tmp_path: Path) -> None:
    events = Path(fake_pi["FAKE_LIVES"]) / "000001" / "events.jsonl"
    kept = [line for line in events.read_text().splitlines() if '"type": "death' not in line]
    events.write_text("\n".join(kept) + "\n")
    res = run(SMOKE, fake_pi, "--out", str(tmp_path / "out"))
    assert res.returncode == 1
    assert "FAIL    duration" in res.stdout


def test_no_epitaph_on_the_pi(fake_pi: dict[str, str], tmp_path: Path) -> None:
    env = {**fake_pi, "EPITAPH_PI_BIN": ""}
    env["PATH"] = os.pathsep.join(
        p for p in env["PATH"].split(os.pathsep) if not (Path(p) / "epitaph").exists()
    )
    (tmp_path / "bin" / "epitaph").unlink()
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}"
    res = run(SMOKE, env, "--out", str(tmp_path / "out"))
    assert res.returncode == 1
    assert "no epitaph command on the Pi" in res.stderr


def test_smoke_dry_run_touches_nothing(tmp_path: Path) -> None:
    env = {**os.environ, "EPITAPH_SSH": "false", "PI_HOST": "pi", "EPITAPH_PYTHON": sys.executable}
    res = run(SMOKE, env, "--dry-run", "--profile", "pi4/skeleton-1200", "--lives", "2")
    assert res.returncode == 0, res.stderr
    assert "epitaph run --profile pi4/skeleton-1200 --lives 2" in res.stderr
    assert "sudo -n systemctl stop epitaph-controller" in res.stderr
    assert res.stderr.count("verify-life") == 2
    assert "--level" not in res.stderr  # skeleton-1200 is judged at its own level
    assert "dry run: nothing ran" in res.stderr


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--lives", "0"], "--lives must be 1-99"),
        (["--profile", "pi4/x;rm"], "bad profile name"),
        (["--bogus"], "unknown option"),
        (["--profile", "pi4/nope", "--dry-run"], "cannot read profile"),
    ],
)
def test_smoke_usage_errors(args: list[str], message: str) -> None:
    env = {**os.environ, "EPITAPH_SSH": "false", "EPITAPH_PYTHON": sys.executable}
    res = run(SMOKE, env, *args)
    assert res.returncode == 2
    assert message in res.stderr


# -- headless_boot_check.sh ---------------------------------------------------------------

SKIPPED = """LoadState=loaded
UnitFileState=enabled
ActiveState=inactive
SubState=dead
Result=exec-condition
NRestarts=0
ConditionResult=yes
ExecMainStatus=0
"""
ACTIVE = """LoadState=loaded
UnitFileState=enabled
ActiveState=active
SubState=running
Result=success
NRestarts=0
ConditionResult=yes
ExecMainStatus=0
"""


def test_boot_check_passes_a_skipped_display(fake_pi: dict[str, str], tmp_path: Path) -> None:
    (tmp_path / "show-epitaph-display").write_text(SKIPPED)
    (tmp_path / "show-epitaph-controller").write_text(ACTIVE)
    out = tmp_path / "report" / "boot.txt"
    res = run(BOOT, fake_pi, "--out", str(out))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "headless boot check on pi" in res.stdout
    assert "PASS display-skipped" in res.stdout
    assert "PASS controller-active" in res.stdout
    assert out.read_text().strip() == res.stdout.strip()


@pytest.mark.parametrize(
    ("display", "failed"),
    [
        (SKIPPED.replace("NRestarts=0", "NRestarts=5"), "display-restarts"),
        (
            SKIPPED.replace("ActiveState=inactive", "ActiveState=activating"),
            "display-not-failing",
        ),
        (SKIPPED.replace("UnitFileState=enabled", "UnitFileState=disabled"), "display-installed"),
        (SKIPPED.replace("Result=exec-condition", "Result=success"), "display-skipped"),
        (ACTIVE, "display-skipped"),
        ("", "display-installed"),
    ],
    ids=["crash-loop", "restarting", "disabled", "never-skipped", "running", "missing"],
)
def test_boot_check_fails_a_bad_display(
    fake_pi: dict[str, str], tmp_path: Path, display: str, failed: str
) -> None:
    (tmp_path / "show-epitaph-display").write_text(display)
    (tmp_path / "show-epitaph-controller").write_text(ACTIVE)
    res = run(BOOT, fake_pi)
    assert res.returncode == 1
    assert "FAIL display" in res.stdout
    assert f"FAIL {failed}" in res.stdout


def test_boot_check_fails_a_stopped_controller(fake_pi: dict[str, str], tmp_path: Path) -> None:
    (tmp_path / "show-epitaph-display").write_text(SKIPPED)
    (tmp_path / "show-epitaph-controller").write_text(SKIPPED)
    res = run(BOOT, fake_pi)
    assert res.returncode == 1
    assert "FAIL controller-active" in res.stdout


def test_boot_check_with_a_screen(fake_pi: dict[str, str], tmp_path: Path) -> None:
    (tmp_path / "show-epitaph-display").write_text(ACTIVE)
    (tmp_path / "show-epitaph-controller").write_text(ACTIVE)
    res = run(BOOT, fake_pi, "--screen")
    assert res.returncode == 0, res.stdout
    assert "PASS display-runs" in res.stdout


def test_boot_check_dry_run_reboots_nothing() -> None:
    env = {**os.environ, "EPITAPH_SSH": "false", "PI_HOST": "pi"}
    res = run(BOOT, env, "--dry-run", "--reboot")
    assert res.returncode == 0, res.stderr
    assert "+ false pi sudo -n systemctl reboot" in res.stderr
    assert "systemctl show epitaph-display" in res.stderr
    assert "dry run: nothing was checked" in res.stderr


def test_boot_check_fails_loudly_when_the_pi_does_not_answer_before_the_reboot() -> None:
    """Regression: under set -e the boot id read failed silently (exit 1, no FAIL line)."""
    env = {**os.environ, "EPITAPH_SSH": "false", "PI_HOST": "pi", "EPITAPH_PI_LOCKED": "1"}
    res = run(BOOT, env, "--reboot")
    assert res.returncode == 1
    assert "FAIL: the Pi does not answer on pi" in res.stderr


def test_boot_check_usage_error() -> None:
    res = run(BOOT, dict(os.environ), "--timeout", "soon")
    assert res.returncode == 2
