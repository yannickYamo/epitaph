"""The rehearsal against a real llama-server and model (10.1 rehearsal row).

One screen moment (birth, one thought): the laptop server is started with the Pi's flags,
the thought is charged at measured Pi costs, and the words come out clean. Run under the
laptop lock:
  tools/laptop_lock.sh run <name> 15 -- env PYTHONPATH=src .venv/bin/python \\
      -m pytest -m model tests/templates/test_rehearse_real.py
Model: EPITAPH_TEST_MODEL (default llama-3.2-1b-instruct:Q4_K_M; only the name is used, the
quant follows the Pi ladder) in ~/epitaph-models.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from epitaph.rehearse import main

pytestmark = pytest.mark.model

NAME = os.environ.get("EPITAPH_TEST_MODEL", "llama-3.2-1b-instruct:Q4_K_M").split(":")[0]
MODELS_DIR = Path(os.environ.get("EPITAPH_MODELS_DIR", "~/epitaph-models")).expanduser()
BIN = Path("~/llama.cpp/build/bin/llama-server").expanduser()

if not (MODELS_DIR / NAME / "Q8_0.gguf").exists() or not BIN.exists():
    pytest.skip("model or llama-server missing", allow_module_level=True)


def test_one_screen_thought_at_pi_cost(tmp_path: Path) -> None:
    rc = main(
        [
            "--stage", "screen", "--model", NAME, "--persona", "persona",
            "--moments", "birth", "--thoughts", "1", "--port", "8098",
            "--models-dir", str(MODELS_DIR), "--out", str(tmp_path),
        ]
    )  # fmt: skip
    assert rc == 0
    (folder,) = [p for p in tmp_path.iterdir() if p.is_dir()]
    (result,) = json.loads((folder / "screen.json").read_text())
    (thought,) = result["thoughts"]
    assert thought["words"] >= 5 and thought["clean"], thought
    assert thought["reading"].startswith("[host] t+")
    # A birth thought on the Pi takes tens of seconds; on the laptop it takes a few.
    assert result["pi_s"] > 20 and result["laptop_s"] < result["pi_s"]
