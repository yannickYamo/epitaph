"""Check that the three editions read alike: the MicroPython port (life.py) and the C port
(epitaph_tiny.c) encode every reading as the model was taught it, and say a share in the
installation's words.

    make -C badge/esp32 && python badge/tools/test_ports.py

`badge/data/reading_tokens.json` holds the readings as `finetune.Tok` encodes them with the
original tokenizer (tok512.bin, not in the repository).
"""

import json
import subprocess
import sys
from pathlib import Path

BADGE = Path(__file__).resolve().parents[1]
ROOT = BADGE.parent
sys.path.insert(0, str(BADGE / "tufty" / "epitaph"))
sys.path.insert(0, str(ROOT / "src"))

import life

from epitaph.mind.prompt import _FRACTIONS  # pyright: ignore[reportPrivateUsage]

HOST = str(BADGE / "esp32" / "host" / "test_host")
ASSETS = BADGE / "tufty" / "epitaph" / "assets"


def host(*args: str) -> list[str]:
    return subprocess.run([HOST, *args], capture_output=True, text=True, check=True).stdout.split(
        "\n"
    )[:-1]


def main() -> None:
    taught = json.loads((BADGE / "data" / "reading_tokens.json").read_text())
    tok = life.Tokenizer(
        json.loads((ASSETS / "vocab.json").read_text()),
        json.loads((ASSETS / "scores.json").read_text()),
    )
    for text, ids in taught.items():
        assert tok.encode(text) == ids, f"life.py encodes {text!r} unlike the training"
        got = [int(t) for t in host("encode", text)[0].split()]
        assert got == ids, f"the C port encodes {text!r} unlike the training"
    print(f"tokens: {len(taught)} readings encoded as the model was taught, on both ports")

    # one table of proportions on the three boards
    assert [(round(v, 6), w) for v, w in life.FRACTIONS] == [
        (round(v, 6), w) for v, w in _FRACTIONS
    ]
    falling = [1.0 - 0.013 * i for i in range(77)]
    for key, shares in enumerate(
        (falling, [0.53, 0.44, 0.38, 0.31, 0.28], [0.33, 0.125, 0.08, 0.06])
    ):
        said = life.Proportions()
        python = [said.say("k", r) for r in shares]
        c = host("say", str(key), *(f"{r:.6f}" for r in shares))
        assert python == c, f"shares {shares}: life.py says {python}, the C port {c}"
    assert python == ["a third", "a tenth", "less than a tenth", "almost nothing"]
    print("proportions: the same words on the installation, life.py and the C port")


if __name__ == "__main__":
    main()
