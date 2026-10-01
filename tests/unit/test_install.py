"""Static checks of deploy/install.sh's container mode and its arm64 test (BUILD_PLAN 9 C10).

The install itself runs as root, so it is exercised end to end by tools/test_install_arm64.sh
(make install-test-arm64) in an emulated arm64 container, not here.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "deploy" / "install.sh"
ARM64 = ROOT / "tools" / "test_install_arm64.sh"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", *args], capture_output=True, text=True, check=False)


def test_container_mode_skips_only_what_needs_a_booted_pi() -> None:
    # Every `skip` is a step that needs a running systemd or the Pi's hardware; a new skip is a
    # deliberate change to this list, not a quiet way to make the container test pass.
    skipped = re.findall(r'\bskip "([^"]+)"', INSTALL.read_text())
    assert sorted(skipped) == sorted(
        [
            "systemctl daemon-reload",
            "cgroup v2 controllers memory cpu io",
            "hardware watchdog",
            "selftest",
        ]
    )


def test_container_flag_is_documented_in_the_help() -> None:
    if os.geteuid() == 0:
        pytest.skip("running as root")
    out = run(str(INSTALL), "--help")
    assert out.returncode == 0
    assert "--container" in out.stdout and "daemon-reload" in out.stdout


def test_container_mode_still_needs_root() -> None:
    if os.geteuid() == 0:
        pytest.skip("running as root")
    out = run(str(INSTALL), "--container", "--check")
    assert out.returncode == 2 and "run as root" in out.stderr


def test_install_manages_its_packages() -> None:
    # A fresh Raspberry Pi OS may lack the venv module, nftables or sudo (visudo).
    text = INSTALL.read_text()
    assert "PACKAGES=(python3-venv nftables sudo)" in text
    assert "dpkg-query -W" in text


def test_install_insists_on_the_source_path() -> None:
    # The units read config/ from /opt/epitaph/src; a clone elsewhere would install a venv the
    # units cannot use.
    assert 'realpath -m "$PREFIX/src"' in INSTALL.read_text()


def test_arm64_test_parses_and_has_help() -> None:
    assert ARM64.stat().st_mode & 0o111
    assert run("-n", str(ARM64)).returncode == 0
    out = run(str(ARM64), "--help")
    assert out.returncode == 0 and "changed: 0" in out.stdout


def test_arm64_test_rejects_unknown_arguments() -> None:
    out = run(str(ARM64), "--bogus")
    assert out.returncode == 2 and "unknown argument" in out.stderr


def test_arm64_test_checks_idempotence_and_sudoers() -> None:
    text = ARM64.read_text()
    assert "--container --enable" in text and "grep -qx 'changed: 0'" in text
    assert "--container --check" in text
    assert "visudo -cf" in text and "visudo -c " in text
    assert "--arch arm64" in text and "debian:trixie" in text


def test_make_target_is_outside_check() -> None:
    make = (ROOT / "Makefile").read_text()
    assert re.search(r"^install-test-arm64:\n\ttools/test_install_arm64\.sh$", make, flags=re.M)
    check = re.search(r"^check:(.*)$", make, flags=re.M)
    assert check is not None and "install-test" not in check.group(1)
