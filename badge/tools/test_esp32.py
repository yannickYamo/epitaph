"""Check the ESP32 port (int8, C) against the badge engine (float32) on the same model.

make -C badge/esp32 && python badge/tools/test_esp32.py
"""

import subprocess
import sys
from pathlib import Path

import numpy as np

BADGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BADGE / "tufty" / "epitaph"))

import tinyllama

STEPS = 40


def main() -> None:
    out = subprocess.run(
        [str(BADGE / "esp32" / "host" / "test_host"), "logits", str(STEPS)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    rows = [ln.split() for ln in out if ln]
    tokens = [int(r[0]) for r in rows]
    c_logits = [np.array(r[1:], dtype=np.float64) for r in rows]

    model = tinyllama.Model(str(BADGE / "tufty" / "epitaph" / "assets" / "model.bin"))
    worst, same = 0.0, 0
    for tok, cl in zip(tokens, c_logits, strict=True):
        pl = model.forward(tok)
        worst = max(worst, float(np.max(np.abs(cl - pl))))
        same += int(np.argmax(cl) == np.argmax(pl))
    spread = float(np.max(pl) - np.min(pl))
    print(
        f"int8 vs float32 over {STEPS} steps: max |logit diff| {worst:.3f} "
        f"(logit range {spread:.1f}); same top token {same}/{STEPS}"
    )
    assert worst < 0.05 * spread, "the int8 port drifts too far from the float model"
    assert same >= STEPS * 0.9, "the int8 port picks different words too often"


if __name__ == "__main__":
    main()
