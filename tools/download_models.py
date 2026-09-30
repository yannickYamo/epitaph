#!/usr/bin/env python3
"""Resolve, pin, download and push model GGUFs (BUILD_PLAN 9 A8).

  tools/download_models.py resolve --models all --quants step0      pin sha256 in config/models.lock.toml
  tools/download_models.py fetch --models llama-3.2-3b-instruct --quants ladder
  tools/pi_lock.sh run A 30 -- tools/download_models.py push --models llama-3.2-1b-instruct --quants Q4_K_M

Files land in ~/epitaph-models/<model>/<quant>.gguf and /var/lib/epitaph/models/<model>/<quant>.gguf.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from epitaph.backend.models import main

if __name__ == "__main__":
    raise SystemExit(main())
