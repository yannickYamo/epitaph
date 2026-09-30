#!/usr/bin/env python3
"""Late-life generation speed from a bench file's two depths (review item F8).

Generation slows as the context grows: every token attends over the KV cache's used cells,
so the time per token grows about linearly with them (S1c round 2: tokens/s against the
slot's n_past and the cache's high-water mark). With a birth thought (short context) and a
deep one (long context) in a bench file, the line through the two
    seconds per token = a + b * context
gives the speed at any context. After trims the cache keeps holes below its high-water mark
(llama.cpp's n_kv = the highest used cell + 1, padded to 256; nothing compacts it), so the late
speed is taken at the full context (--ctx, 2048 on the Pi 4).

Writes into each file: tg_tok_s_late, late_after_s (when the memory has grown to it; the cost
model eases the rate down over that time), late_ctx, and tg_ctx_model {a, b} for a cost model
that wants the speed at any context.

  python3 tools/spike/late_speed.py bench/measured/pi4-llama-3.2-3b-instruct-*.json --write
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def context_line(rec: dict[str, Any]) -> tuple[float, float] | None:
    """(a, b) with seconds per token = a + b * context, from the birth and deep thoughts."""
    b_tok, d_tok = rec.get("tg_tok_s_birth"), rec.get("tg_tok_s")
    b_ctx = (rec.get("birth") or {}).get("prompt_tokens") or rec.get("birth_prompt_tokens")
    d_ctx = rec.get("deep_prompt_tokens")
    if not (b_tok and d_tok and b_ctx and d_ctx) or d_ctx <= b_ctx:
        return None
    half = (rec.get("gen") or 70) / 2  # the mean context while generating
    x1, y1 = b_ctx + half, 1 / b_tok
    x2, y2 = d_ctx + half, 1 / d_tok
    b = (y2 - y1) / (x2 - x1)
    return y1 - b * x1, b


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--ctx", type=int, default=2048, help="context for the late speed")
    ap.add_argument("--after-s", type=float, default=1200, help="when the memory is full")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    for f in args.files:
        path = Path(f)
        rec = json.loads(path.read_text())
        line = context_line(rec)
        if line is None:
            print(f"{path.name}: no birth/deep pair, skipped")
            continue
        a, b = line
        late = 1 / (a + b * args.ctx)
        print(
            f"{path.name}: {rec['tg_tok_s_birth']:.3f} (birth) {rec['tg_tok_s']:.3f} (deep) "
            f"-> {late:.3f} tokens/s at context {args.ctx} ({late / rec['tg_tok_s']:.0%} of deep)"
        )
        if args.write:
            rec["tg_ctx_model"] = {"a_s": round(a, 5), "b_s_per_token": round(b, 8)}
            rec["late_ctx"] = args.ctx
            rec["tg_tok_s_late"] = round(late, 3)
            rec["late_after_s"] = args.after_s
            rec["late_note"] = (
                "S1c round 2 (F8): generation slows with the context attention runs over; "
                "tg_tok_s_late is the birth/deep line at the full context (the KV cache keeps "
                "holes up to its high-water mark), reached as the memory fills."
            )
            path.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
