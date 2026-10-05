#!/usr/bin/env python3
"""Resolve, pin, download and push model GGUFs.

  tools/download_models.py resolve --models all --quants step0      pin sha256 in config/models.lock.toml
  tools/download_models.py fetch --models llama-3.2-3b-instruct --quants ladder
  tools/pi_lock.sh run <name> 30 -- tools/download_models.py push --models qwen3-4b-instruct-2507 --quants Q4_K_M

Files land in ~/epitaph-models/<model>/<quant>.gguf and /var/lib/epitaph/models/<model>/<quant>.gguf.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from epitaph.backend.models import main

if __name__ == "__main__":
    raise SystemExit(main())
