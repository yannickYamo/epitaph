#!/usr/bin/env python3
"""Spike S4: reload start to the first token, cold and warm (BUILD_PLAN 8.5).

A reload stops the creature at one ladder step and starts it one step down; the memory is cut
to the post-reload recall and re-read in full by the fresh server. Measured per case:
  stop_s   SIGTERM to exit of the old server
  load_s   spawn to /health of the new one (cold: page cache dropped first)
  reread_s the first request: system + recall + reading, up to the first token
  total_s  stop + load + reread (go: at most 180 s)

  python3 tools/spike/s4_reload.py --model llama-3.2-3b-instruct \
      --case Q4_K_M:2:512 --case Q2_K:2:200 --from-quant Q6_K --out bench/spike/s4-pi4-llama.json
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from llama import Server, model_path, running, system_text, temp_c, throttled, write_json
from s1_fit_speed import drop_caches, memory_messages


def one(args: argparse.Namespace, quant: str, threads: int, recall: int, cold: bool) -> dict[str, Any]:
    old = Server(model_path(args.model, args.from_quant), threads=3, port=args.port, taskset="1-3")
    system = system_text(5, True)
    with running(old):
        old.chat(memory_messages(system, 300), max_tokens=5)
        t0 = time.monotonic()
        old.stop()
        stop_s = time.monotonic() - t0
    if cold:
        drop_caches()
    else:  # warm: the new file was read recently
        Path(model_path(args.model, quant)).read_bytes()
    new = Server(model_path(args.model, quant), threads=threads, port=args.port, taskset="1-3")
    msgs = memory_messages(system, len(system) // 4 + recall)
    with running(new):
        r = new.chat(msgs, max_tokens=1)
    rec = {
        "quant": quant,
        "threads": threads,
        "recall": recall,
        "cold": cold,
        "stop_s": round(stop_s, 1),
        "load_s": round(new.load_s, 1),
        "reread_tokens": r.get("prompt_n"),
        "reread_s": round(r["wall_s"], 1),
        "pp_tok_s": r.get("prompt_per_s"),
        "total_s": round(stop_s + new.load_s + r["wall_s"], 1),
        "throttled": throttled(),
        "temp_c": temp_c(),
    }
    rec["ok"] = rec["total_s"] <= 180
    print(rec, flush=True)
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--from-quant", required=True)
    ap.add_argument("--case", action="append", required=True, help="QUANT:THREADS:RECALL")
    ap.add_argument("--port", type=int, default=8094)
    ap.add_argument("--warm", action="store_true", help="also measure warm reloads")
    ap.add_argument("--out")
    args = ap.parse_args()
    results = []
    for c in args.case:
        q, th, rc = c.split(":")
        results.append(one(args, q, int(th), int(rc), cold=True))
        if args.warm:
            results.append(one(args, q, int(th), int(rc), cold=False))
    out = {"spike": "S4", "model": args.model, "from": args.from_quant, "cases": results}
    if args.out:
        write_json(Path(args.out), out)


if __name__ == "__main__":
    main()
