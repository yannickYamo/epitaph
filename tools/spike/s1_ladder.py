#!/usr/bin/env python3
"""Round 2 of S1b on the Pi: one model's whole ladder at 3 and 2 threads (BUILD_PLAN 8.5, A2).

For each ladder step, two servers in a row, both with `-tb 3` (prompt threads, as the backend
runs them) and the Pi 4's load mode (`--load-mode dio`, spike S3):
  1. 3 generation threads, the page cache dropped first: load_s (cold)
  2. 2 generation threads, started right after: load_s_warm (dio reads with O_DIRECT, so a
     warm load should cost the same; this checks it)
On each server, as the controller does at birth:
  - prefill: the system prompt read during the load (the backend's `prefill`, one token)
  - birth thought: the first reading + 70 generated tokens, with the system prompt cached
  - deep: a re-read of about --fill prompt tokens (system cached), then 70 tokens at that
    depth: pp_tok_s and tg_tok_s
  - memory sampled every 0.5 s: headroom as in s1_fit_speed.py; get_throttled after each run

Writes bench/pi4-<model>-<step>-<threads>.json (the `costmodel.load_costs` format; pi_run.sh
parks them in bench/measured/) and the raw run to --raw.

  python3 tools/spike/s1_ladder.py --model llama-3.2-3b-instruct --ladder Q6_K,Q4_K_M,Q2_K \\
      --raw bench/spike/s1r2-pi4-llama-3.2-3b-instruct.json
"""

from __future__ import annotations

import argparse
import socket
import time
from pathlib import Path
from typing import Any

from llama import Server, model_path, reading, system_text, temp_c, throttled, write_json
from s1_fit_speed import Sampler, drop_caches, memory_messages


def one_server(
    args: argparse.Namespace, quant: str, step: int, threads: int, cold: bool
) -> dict[str, Any]:
    path = model_path(args.model, quant)
    srv = Server(
        path,
        threads=threads,
        threads_batch=args.threads_batch,
        port=args.port,
        cache_reuse=args.cache_reuse,
        load_mode=args.load_mode,
        swa_full=args.swa_full,
        taskset=args.taskset or None,
    )
    if cold:
        drop_caches()
    rec: dict[str, Any] = {
        "model": args.model,
        "quant": quant,
        "step": step,
        "threads": threads,
        "threads_batch": args.threads_batch,
        "ctx": 2048,
        "load_mode": args.load_mode or "mmap",
        "swa_full": args.swa_full,
        "kv": "f16",
        "file_mb": round(path.stat().st_size / 2**20),
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "host": socket.gethostname(),
        "cold": cold,
        "throttled_before": throttled(),
    }
    sampler = Sampler(srv)
    sampler.start()
    system = system_text(5, True)
    try:
        srv.start()
        rec["load_s"] = round(srv.load_s, 1)
        pre = srv.prefill(system)
        rec["prefill"] = pre
        rec["prefill_s"] = round(pre["wall_s"], 1)
        rec["system_tokens"] = pre.get("prompt_tokens")
        birth = srv.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": reading(0)}],
            max_tokens=args.gen,
            ignore_eos=True,
        )
        rec["birth"] = birth
        rec["birth_thought_s"] = round(birth["wall_s"], 1)
        rec["tg_tok_s_birth"] = round(birth["predicted_per_s"], 3)
        deep = srv.chat(memory_messages(system, args.fill), max_tokens=args.gen, ignore_eos=True)
        rec["deep"] = deep
        if "error" in deep:
            rec["error"] = deep["error"]
        else:
            rec["pp_tok_s"] = round(deep["prompt_per_s"], 2)
            rec["deep_prompt_tokens"] = deep["prompt_tokens"]
            rec["tg_tok_s"] = round(deep["predicted_per_s"], 3)
    finally:
        srv.stop()
        sampler.stop.set()
        sampler.join()
    rec["headroom_mb"] = round(sampler.min_headroom)
    rec["min_mem_available_mb"] = round(sampler.min_avail)
    rec["peak_rss_anon_mb"] = round(sampler.max_anon)
    rec["peak_rss_file_mb"] = round(sampler.max_file)
    rec["fits"] = rec["headroom_mb"] >= 300 and "error" not in rec
    rec["temp_c"] = temp_c()
    rec["throttled"] = throttled()
    rec["birth_ok"] = rec.get("birth_thought_s", 1e9) <= 90
    rec["cache_reuse_works"] = True
    rec["cache_reuse_note"] = (
        "S2f/S2t: warm trims and erosion re-read 2-8% with --cache-reuse (Gemma 3 needs --swa-full)"
    )
    keys = ("load_s", "prefill_s", "birth_thought_s", "pp_tok_s", "tg_tok_s", "tg_tok_s_birth",
            "headroom_mb", "throttled", "temp_c", "error")  # fmt: skip
    print(
        quant,
        f"t{threads}/tb{args.threads_batch}",
        " ".join(f"{k}={rec[k]}" for k in keys if k in rec),
        flush=True,
    )
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--ladder", required=True, help="quants for steps 0, 1, 2, comma-separated")
    ap.add_argument("--steps", default="", help="only these steps, e.g. 0 (default: all)")
    ap.add_argument("--threads", default="3,2")
    ap.add_argument("--threads-batch", type=int, default=3)
    ap.add_argument("--load-mode", default="dio")
    ap.add_argument("--swa-full", action="store_true")
    ap.add_argument("--cache-reuse", type=int, default=32)
    ap.add_argument("--fill", type=int, default=800, help="prompt tokens of the deep request")
    ap.add_argument("--gen", type=int, default=70)
    ap.add_argument("--no-cold", action="store_true", help="never drop the page cache (laptop)")
    ap.add_argument("--taskset", default="1-3")
    ap.add_argument("--port", type=int, default=8093)
    ap.add_argument("--hw-class", default="pi4")
    ap.add_argument("--out-dir", default="bench")
    ap.add_argument("--raw", required=True)
    args = ap.parse_args()
    ladder = args.ladder.split(",")
    steps = [int(s) for s in args.steps.split(",")] if args.steps else list(range(len(ladder)))
    runs: list[dict[str, Any]] = []
    for step in steps:
        for i, threads in enumerate(int(t) for t in args.threads.split(",")):
            rec = one_server(args, ladder[step], step, threads, cold=(i == 0 and not args.no_cold))
            if i > 0:
                rec["load_s_note"] = "warm: started right after the previous server on this file"
            runs.append(rec)
            lean = {k: v for k, v in rec.items() if k not in ("birth", "deep", "prefill")}
            write_json(
                Path(args.out_dir) / f"{args.hw_class}-{args.model}-{step}-{threads}.json", lean
            )
            write_json(Path(args.raw), {"spike": "S1b-r2", "model": args.model, "runs": runs})
    # Warm load per step: the second server's load goes into every file of that step.
    for step in steps:
        same = [r for r in runs if r["step"] == step]
        warm = [r["load_s"] for r in same if not r["cold"]]
        cold = [r["load_s"] for r in same if r["cold"]]
        for r in same:
            if warm:
                r["load_s_warm"] = warm[0]
            if cold:
                r["load_s_cold"] = cold[0]
                r["load_s"] = cold[0]  # the cost model charges the cold load at a reload
            lean = {k: v for k, v in r.items() if k not in ("birth", "deep", "prefill")}
            write_json(
                Path(args.out_dir) / f"{args.hw_class}-{args.model}-{step}-{r['threads']}.json",
                lean,
            )
    write_json(Path(args.raw), {"spike": "S1b-r2", "model": args.model, "runs": runs})


if __name__ == "__main__":
    main()
