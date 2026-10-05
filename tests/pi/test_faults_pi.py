"""The quick Pi rows of the fault matrix, through tools/fault_pi.sh. Run:

tools/pi_lock.sh run <name> 10 -- .venv/bin/python -m pytest -m pi tests/pi/test_faults_pi.py -q

The controller service must be running. The slow rows (crash, hang, controller-kill: each ends a
life and waits for the next birth) run from the script directly: `tools/fault_pi.sh all`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.pi


@pytest.mark.parametrize("row", ["netblock", "two-controllers"])
def test_fault_row(row: str) -> None:
    out = subprocess.run(
        ["tools/fault_pi.sh", row], cwd=ROOT, capture_output=True, text=True, timeout=600
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"PASS {row}:" in out.stdout
