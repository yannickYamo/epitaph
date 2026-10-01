"""Install, units and clock helper on the real Pi (BUILD_PLAN 9 C5, gate G1.3). Run under the lock:

tools/pi_lock.sh run C 30 -- .venv/bin/python -m pytest -m pi tests/pi/test_install_pi.py -q

Deploys this tree (tools/pi_deploy.sh needs the lock, which the caller holds), then checks that a
second install changes nothing, the clock helper round trip, the network helper, the units and
selftest (which includes the creature's network block). It leaves the units as it found them and
the clock at 1800 MHz.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.pi
POLICY = "/sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq"
UNITS = ["epitaph-controller.service", "epitaph-display.service"]


def run(*argv: str, timeout: float = 1200) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )


def pi(cmd: str) -> subprocess.CompletedProcess[str]:
    host = run("tools/pi_host.sh").stdout.strip() or "pi"
    return run("ssh", "-o", "BatchMode=yes", host, cmd, timeout=300)


@pytest.fixture(scope="module")
def deployed() -> str:
    first = run("tools/pi_deploy.sh")
    assert first.returncode == 0, first.stdout + first.stderr
    return first.stdout


def test_second_install_changes_nothing(deployed: str) -> None:
    again = pi("sudo -n /opt/epitaph/src/deploy/install.sh --no-selftest")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "changed: 0" in again.stdout and "failed: 0" in again.stdout


def test_clock_helper_round_trip(deployed: str) -> None:
    try:
        low = pi(f"sudo -n /usr/local/sbin/epitaph-clock 1200 && cat {POLICY}")
        assert low.returncode == 0, low.stderr
        assert low.stdout.split()[-1] == "1200000"
        refused = pi("sudo -n /usr/local/sbin/epitaph-clock 2000")
        assert refused.returncode != 0
    finally:
        back = pi(f"sudo -n /usr/local/sbin/epitaph-clock reset && cat {POLICY}")
    assert back.stdout.split()[-1] == "1800000"


def test_units_verify(deployed: str) -> None:
    """The units parse; install never changes whether they are enabled (only --enable does)."""
    paths = " ".join(f"/etc/systemd/system/{u}" for u in UNITS)
    out = pi(f"systemd-analyze verify {paths}")
    assert out.returncode == 0 and not out.stderr.strip(), out.stderr
    for u in UNITS:
        assert pi(f"systemctl is-enabled {u}").stdout.strip() in ("enabled", "disabled")


def test_netblock_helper_refuses_other_paths(deployed: str) -> None:
    for arg in ("add ../../etc", "add system.slice/ssh.service/creature", "flush"):
        assert pi(f"sudo -n /usr/local/sbin/epitaph-netblock {arg}").returncode == 2
    assert pi("sudo -n /usr/local/sbin/epitaph-netblock status").returncode == 0


def test_selftest_under_a_delegated_unit(deployed: str) -> None:
    out = pi("cd / && /opt/epitaph/venv/bin/epitaph selftest --user pi")
    assert out.returncode == 0, out.stdout + out.stderr
    assert pi(f"cat {POLICY}").stdout.strip() == "1800000"
