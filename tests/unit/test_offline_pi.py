"""tools/offline_pi.sh against a fake Pi (the offline round).

The fake Pi is a folder of stand-in commands first on PATH, as in test_pi_tools.py: `ssh` runs
the remote command here (and fails like an unreachable host while the fake networking is off),
`sudo` drops itself, `systemctl` keeps unit state in files, `nmcli` keeps the networking state
in a file, `systemd-run` runs its command at once. The rescue "fires" after a few unanswered
probes. Every command the script would run on the Pi has a fake: nothing here touches the
laptop's own networking or systemd. No network, no Pi, no waiting.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OFFLINE = ROOT / "tools" / "offline_pi.sh"
RESCUE = "epitaph-offline-rescue"

FAKES = {
    "ssh": """
while [ "${1:-}" = -o ]; do shift 2; done
shift
printf '%s\\n' "$*" >> "$FAKE_DIR/ssh.log"
if [ "$(cat "$FAKE_DIR/networking" 2>/dev/null)" = disabled ]; then
  n=$(( $(cat "$FAKE_DIR/unanswered" 2>/dev/null || echo 0) + 1 ))
  echo "$n" > "$FAKE_DIR/unanswered"
  if [ "$n" -ge "${FAKE_RESCUE_AFTER:-2}" ]; then  # the rescue timer fires on the Pi
    echo enabled > "$FAKE_DIR/networking"
    [ -n "${FAKE_NO_RESCUE:-}" ] || rm -f "$FAKE_DIR/units/epitaph-offline-rescue.timer.enabled"
  fi
  echo "ssh: connect to host pi port 22: No route to host" >&2
  exit 255
fi
exec bash -c "$*"
""",
    "sudo": """
[ "${1:-}" = -n ] && shift
exec "$@"
""",
    "nmcli": """
printf '%s\\n' "$*" >> "$FAKE_DIR/nmcli.log"
[ "${1:-}" = networking ] || exit 2
case "${2:-}" in
  '') cat "$FAKE_DIR/networking" 2>/dev/null || echo enabled ;;
  on) echo enabled > "$FAKE_DIR/networking" ;;
  off) [ -n "${FAKE_NMCLI_IGNORE_OFF:-}" ] || echo disabled > "$FAKE_DIR/networking" ;;
esac
""",
    "systemctl": """
printf '%s\\n' "$*" >> "$FAKE_DIR/systemctl.log"
U="$FAKE_DIR/units"; mkdir -p "$U"
cmd="$1"; shift
unit=""; now=0
for a in "$@"; do case "$a" in --now) now=1 ;; -*) ;; *) unit="$a" ;; esac; done
case "$cmd" in
  daemon-reload|reboot) ;;
  enable) [ -n "${FAKE_ENABLE_FAILS:-}" ] || touch "$U/$unit.enabled" ;;
  disable) rm -f "$U/$unit.enabled"; [ "$now" = 0 ] || rm -f "$U/$unit.active" ;;
  start|restart) touch "$U/$unit.active" ;;
  stop) rm -f "$U/$unit.active" ;;
  is-enabled)
    if [ -f "$U/$unit.enabled" ]; then echo enabled; exit 0; fi
    echo disabled; exit 1 ;;
  is-active)
    if [ -f "$U/$unit.active" ]; then echo active; exit 0; fi
    echo inactive; exit 3 ;;
  show)
    case " $* " in
      *NextElapseUSecMonotonic*) [ -f "$U/$unit.active" ] && echo "1h 15min" || echo infinity ;;
      *) echo "ActiveState=active"; echo "NRestarts=0" ;;
    esac ;;
esac
""",
    "systemd-analyze": """
[ -z "${FAKE_VERIFY_OUT:-}" ] || echo "$FAKE_VERIFY_OUT" >&2
""",
    "systemd-run": """
printf '%s\\n' "$*" > "$FAKE_DIR/systemd-run.args"
while [ "${1#--}" != "$1" ]; do shift; done
"$@"
""",
    "journalctl": """
echo "fake journal: $*"
""",
    "timedatectl": """
echo no
""",
}


@pytest.fixture
def pi(tmp_path: Path) -> dict[str, str]:
    """Environment for offline_pi.sh with the fake Pi on PATH; FAKE_DIR is tmp_path."""
    if shutil.which("flock") is None or shutil.which("timeout") is None:
        pytest.skip("needs flock and timeout (util-linux, coreutils)")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in FAKES.items():
        path = bin_dir / name
        path.write_text("#!/usr/bin/env bash\n" + body.lstrip())
        path.chmod(0o755)
    for sub in ("units", "unit-dir", "locks"):
        (tmp_path / sub).mkdir()
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_DIR": str(tmp_path),
        "PI_HOST": "pi",
        "EPITAPH_SSH": str(bin_dir / "ssh"),
        "EPITAPH_PI_UNIT_DIR": str(tmp_path / "unit-dir"),
        "EPITAPH_LOCK_DIR": str(tmp_path / "locks"),
        "EPITAPH_POLL_S": "0",
        "AGENT": "test",
    }


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(OFFLINE), *args], env=env, capture_output=True, text=True, timeout=60
    )


def lines(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def test_arms_the_rescue_then_goes_offline_and_comes_back(
    pi: dict[str, str], tmp_path: Path
) -> None:
    res = run(pi)
    assert res.returncode == 0, res.stderr + res.stdout
    unit_dir = tmp_path / "unit-dir"
    service = (unit_dir / f"{RESCUE}.service").read_text()
    timer = (unit_dir / f"{RESCUE}.timer").read_text()
    nmcli = str(tmp_path / "bin" / "nmcli")
    assert f"ExecStart={nmcli} networking on" in service
    assert "Restart=on-failure" in service  # NetworkManager not up yet: try again
    assert f"disable --no-reload {RESCUE}.timer" in service  # it fires once
    assert "OnActiveSec=15min" in timer and "WantedBy=timers.target" in timer
    # The rescue was enabled (a reboot keeps it) and started before networking went off.
    log = lines(tmp_path / "systemctl.log")
    assert log.index(f"enable --quiet {RESCUE}.timer") < log.index(f"restart {RESCUE}.timer")
    assert lines(tmp_path / "nmcli.log").count("networking off") == 1
    assert "--on-active=5" in (tmp_path / "systemd-run.args").read_text()
    assert "rescue verified" in res.stdout
    assert "in 5 s" in res.stdout and "networking off" in res.stdout
    assert "offline: the Pi no longer answers" in res.stderr
    assert "PASS: offline for about" in res.stderr
    # The report after the return.
    assert "epitaph-controller:" in res.stdout and "NTPSynchronized=" in res.stdout
    assert (tmp_path / "networking").read_text().strip() == "enabled"


def test_reboot_follows_networking_off(pi: dict[str, str], tmp_path: Path) -> None:
    res = run(pi, "--reboot", "--minutes", "20")
    assert res.returncode == 0, res.stderr + res.stdout
    assert "OnActiveSec=20min" in (tmp_path / "unit-dir" / f"{RESCUE}.timer").read_text()
    cmd = (tmp_path / "systemd-run.args").read_text()
    assert cmd.index("networking off") < cmd.index("systemctl reboot")
    assert "reboot" in lines(tmp_path / "systemctl.log")


@pytest.mark.parametrize(
    ("fault", "why"),
    [
        ({"FAKE_VERIFY_OUT": "bad unit"}, "systemd-analyze verify: bad unit"),
        ({"FAKE_ENABLE_FAILS": "1"}, "is-enabled=disabled"),
    ],
    ids=["verify-fails", "not-enabled"],
)
def test_refuses_to_go_offline_without_a_verified_rescue(
    pi: dict[str, str], tmp_path: Path, fault: dict[str, str], why: str
) -> None:
    res = run({**pi, **fault})
    assert res.returncode == 1
    assert why in res.stdout
    assert "networking left on" in res.stdout + res.stderr
    assert "networking off" not in lines(tmp_path / "nmcli.log")
    assert not (tmp_path / "systemd-run.args").exists()


def test_fails_when_the_pi_does_not_come_back(pi: dict[str, str], tmp_path: Path) -> None:
    env = {**pi, "FAKE_RESCUE_AFTER": "1000", "EPITAPH_OFFLINE_BUDGET_S": "0"}
    res = run(env)
    assert res.returncode == 1
    assert "did not come back" in res.stderr


def test_fails_when_networking_never_goes_down(pi: dict[str, str], tmp_path: Path) -> None:
    env = {**pi, "FAKE_NMCLI_IGNORE_OFF": "1", "EPITAPH_OFFLINE_DOWN_S": "0"}
    res = run(env)
    assert res.returncode == 1
    assert "still answers" in res.stderr and "the rescue stays armed" in res.stderr


def test_a_return_before_the_rescue_is_not_an_offline_run(
    pi: dict[str, str], tmp_path: Path
) -> None:
    # Networking came back by itself (e.g. NetworkManager forgot `networking off` at boot).
    res = run({**pi, "FAKE_NO_RESCUE": "1"}, "--reboot")
    assert res.returncode == 1
    assert "came back before the rescue ran" in res.stderr


def test_no_wait_returns_offline(pi: dict[str, str], tmp_path: Path) -> None:
    res = run(pi, "--no-wait")
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "networking").read_text().strip() == "disabled"
    assert "networking comes back on after 15 min" in res.stderr


def test_status_changes_nothing(pi: dict[str, str], tmp_path: Path) -> None:
    res = run(pi, "--status")
    assert res.returncode == 0, res.stderr
    assert "networking: enabled" in res.stdout and "rescue timer: is-enabled=disabled" in res.stdout
    assert not lines(tmp_path / "nmcli.log")[1:]  # only the read
    assert not list((tmp_path / "unit-dir").iterdir())


def test_disarm_turns_networking_on_and_removes_the_rescue(
    pi: dict[str, str], tmp_path: Path
) -> None:
    assert run({**pi, "FAKE_RESCUE_AFTER": "1000", "EPITAPH_OFFLINE_BUDGET_S": "0"}).returncode
    assert (tmp_path / "unit-dir" / f"{RESCUE}.timer").exists()
    (tmp_path / "networking").write_text("enabled\n")  # reachable again (by the cable, say)
    res = run(pi, "--disarm")
    assert res.returncode == 0, res.stderr
    assert not list((tmp_path / "unit-dir").iterdir())
    assert f"disable --now --quiet {RESCUE}.timer" in lines(tmp_path / "systemctl.log")
    assert lines(tmp_path / "nmcli.log")[-1] == "networking on"


def test_dry_run_touches_nothing(tmp_path: Path) -> None:
    env = {**os.environ, "EPITAPH_SSH": "false", "PI_HOST": "pi"}
    res = run(env, "--dry-run", "--reboot")
    assert res.returncode == 0, res.stderr
    assert "+ false pi sudo -n bash -s -- arm 15 1 /etc/systemd/system" in res.stderr
    assert "-- down 15 1" in res.stderr
    assert "dry run: nothing ran" in res.stderr


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--minutes", "3"], "--minutes must be 5-240"),
        (["--minutes", "soon"], "--minutes must be 5-240"),
        (["--minutes", "241"], "--minutes must be 5-240"),
        (["--bogus"], "unknown option"),
    ],
)
def test_usage_errors(args: list[str], message: str) -> None:
    res = run({**os.environ, "EPITAPH_SSH": "false"}, *args)
    assert res.returncode == 2
    assert message in res.stderr


def test_script_parses_and_is_executable() -> None:
    assert OFFLINE.stat().st_mode & 0o111
    assert subprocess.run(["bash", "-n", str(OFFLINE)], check=False).returncode == 0
