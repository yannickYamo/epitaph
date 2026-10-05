"""Build the wheel, install it into a clean environment and live a simulated life with it,
from a directory that is not the repository: what someone gets from `pip install epitaph`.

    python tools/check_package.py

The wheel must carry everything a life reads at run time (the configuration, the measured
costs, the fonts); a checkout hides a missing file, an installed package does not.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*argv: str, cwd: Path) -> str:
    """Run argv in `cwd`; its output, or exit with it when the command fails."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}  # nothing of the checkout
    res = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        sys.exit(f"FAILED: {' '.join(argv)}\n{res.stdout}{res.stderr}")
    return res.stdout


def main() -> None:
    """Build, install, simulate."""
    with tempfile.TemporaryDirectory(prefix="epitaph-package-") as tmp:
        work = Path(tmp)
        run(sys.executable, "-m", "build", "--wheel", "--outdir", str(work / "dist"), cwd=ROOT)
        (wheel,) = (work / "dist").glob("epitaph-*.whl")
        venv.create(work / "venv", with_pip=True)
        bin_dir = work / "venv" / ("Scripts" if sys.platform == "win32" else "bin")
        run(str(bin_dir / "python"), "-m", "pip", "install", "-q", str(wheel), cwd=work)
        epitaph = str(bin_dir / "epitaph")
        out = run(
            epitaph, "sim", "--profile", "pi4/default", "--hardware", "pi4-4gb", "--quiet", cwd=work
        )
        if "cause=oom" not in out:
            sys.exit(f"FAILED: the installed package did not live a whole life:\n{out}")
        run(epitaph, "estimate", "--profile", "pi4/default", "--hardware", "pi4-4gb", cwd=work)
        print(f"package: {wheel.name} installs and lives a simulated life outside the repository")


if __name__ == "__main__":
    main()
