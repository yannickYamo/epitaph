"""Static checks of deploy/: the units, the sudoers rule and the scripts (BUILD_PLAN 9 C5)."""

from __future__ import annotations

import configparser
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"


def unit(name: str) -> configparser.RawConfigParser:
    p = configparser.RawConfigParser(strict=False, interpolation=None)
    p.optionxform = str  # type: ignore[assignment,method-assign]
    p.read_string((DEPLOY / "systemd" / name).read_text())
    return p


def test_controller_unit() -> None:
    s = unit("epitaph-controller.service")["Service"]
    assert s["Type"] == "notify" and s["WatchdogSec"] == "30"
    assert s["Delegate"] == "yes" and s["CPUAffinity"] == "0" and s["Restart"] == "always"
    assert s["User"] == "@USER@" and s["WorkingDirectory"] == "/var/lib/epitaph"
    assert s["ExecStart"] == "/opt/epitaph/venv/bin/epitaph run"
    # the full clock before every start and after every stop (ADR-025)
    assert s["ExecStartPre"] == "+/usr/local/sbin/epitaph-clock reset"
    assert s["ExecStopPost"] == "+/usr/local/sbin/epitaph-clock reset"
    # an OOM death of the creature must not stop the controller
    assert s["OOMPolicy"] == "continue"
    assert "NoNewPrivileges" not in s  # sudo for the clock helper needs privileges
    assert unit("epitaph-controller.service")["Unit"]["StartLimitIntervalSec"] == "0"


def test_display_unit_is_headless_safe() -> None:
    s = unit("epitaph-display.service")["Service"]
    assert s["ExecCondition"] == "/opt/epitaph/venv/bin/epitaph display --screen-present"
    assert s["Restart"] == "on-failure"
    assert "--driver screen" in s["ExecStart"]


def test_sudoers_allows_only_the_helper() -> None:
    rules = [
        line
        for line in (DEPLOY / "sudoers" / "epitaph-clock").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert rules == [
        "@USER@ ALL=(root) NOPASSWD: /usr/local/sbin/epitaph-clock reset, "
        "/usr/local/sbin/epitaph-clock ^[1-9][0-9]{2,3}$"
    ]


@pytest.mark.skipif(not Path("/usr/sbin/visudo").exists(), reason="no visudo")
def test_sudoers_parses(tmp_path: Path) -> None:
    f = tmp_path / "rule"
    f.write_text((DEPLOY / "sudoers" / "epitaph-clock").read_text().replace("@USER@", "pi"))
    out = subprocess.run(
        ["/usr/sbin/visudo", "-cf", str(f)], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.parametrize("script", ["deploy/install.sh", "tools/pi_deploy.sh"])
def test_scripts_parse(script: str) -> None:
    path = ROOT / script
    assert path.stat().st_mode & 0o111
    out = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr


def test_install_refuses_without_root() -> None:
    if os.geteuid() == 0:
        pytest.skip("running as root")
    out = subprocess.run(
        ["bash", str(DEPLOY / "install.sh"), "--check"], capture_output=True, text=True, check=False
    )
    assert out.returncode == 2 and "run as root" in out.stderr


def test_pi_deploy_refuses_without_the_lock(tmp_path: Path) -> None:
    out = subprocess.run(
        ["bash", str(ROOT / "tools" / "pi_deploy.sh")],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "EPITAPH_LOCK_DIR": str(tmp_path)},
    )
    assert out.returncode == 2 and "under the Pi lock" in out.stderr
