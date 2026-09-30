"""Body checks on the real Pi (BUILD_PLAN 10.1, 10.4). Run from the laptop under the lock:

tools/pi_lock.sh run C 20 -- .venv/bin/python -m pytest -m pi tests/pi -q
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.pi


def run(*argv: str, timeout: float = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )


def test_bootstrap_has_no_drift() -> None:
    """The Pi still matches step 0 (pi_bootstrap --check changes nothing)."""
    out = run("tools/pi_bootstrap.sh", "--check")  # `pi`, else the cable
    assert out.returncode == 0, out.stdout + out.stderr
    assert "drift: 0" in out.stdout


def test_delegated_cgroups_and_network_block() -> None:
    """S3b as a regression test: every delegated-cgroup step works; outbound is refused."""
    out = run("tools/spike/s3_run.sh", "s3b")
    assert out.returncode == 0, out.stdout + out.stderr
    path = out.stdout.strip().splitlines()[-2].removeprefix("result: ")
    res = json.loads(Path(path).read_text())
    assert res["ok"], res["steps"]
    steps = res["steps"]
    assert steps["death_cause_oom"]["value"] == "oom"
    assert steps["net_creature_before"]["value"] == "connected"
    assert steps["net_creature_after"]["value"].startswith("refused")
    assert steps["net_supervisor_after"]["value"] == "connected"
    assert steps["net_creature_localhost"]["value"] == "connected"
