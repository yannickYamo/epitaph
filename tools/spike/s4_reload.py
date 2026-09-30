#!/usr/bin/env python3
"""Spike S4: reload start to the first token, cold and warm (BUILD_PLAN 8.5).

A reload stops the creature at one ladder step and starts it one step down; the memory is cut
to the post-reload recall (x trim_to, as `Memory.cut_for_reload` does) and re-read in full by
the fresh server. Measured per case:
  stop_s     SIGTERM to exit of the old server
  load_s     spawn to /health of the new one (cold: page cache dropped first)
  prefill_s  with --prefill: the system prompt read right after the load, as the controller
             does during the silence (the backend's `prefill`)
  reread_s   the first request: (system +) memory + reading, up to the first token
  total_s    stop + load + prefill + reread (go: at most 180 s)

The memory is sized with the model's own template and tokenizer (the backend's counting
method, S6), so "recall 300" means 255 tokens of past turns after the cut.

  python3 tools/spike/s4_reload.py --model llama-3.2-3b-instruct --from-quant Q6_K \\
      --case Q4_K_M:3:300 --case Q2_K:2:200:3 --prefill --load-mode dio \\
      --out bench/spike/s4-pi4-llama.json
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from llama import Server, model_path, reading, running, system_text, temp_c, throttled, write_json
from s1_fit_speed import FILLER, drop_caches


def fit_memory(srv: Server, system: str, tokens: int) -> list[dict[str, str]]:
    """System + past turns of at most `tokens` template tokens + the next reading."""
    head = [{"role": "system", "content": system}]
    base = srv.count([*head, {"role": "user", "content": reading(99)}])
    turns: list[dict[str, str]] = []
    i = 0
    while True:
        more = [*turns, {"role": "user", "content": reading(i)},
                {"role": "assistant", "content": FILLER}]  # fmt: skip
        if srv.count([*head, *more, {"role": "user", "content": reading(99)}]) - base > tokens:
            break
        turns, i = more, i + 1
    return [*head, *turns, {"role": "user", "content": reading(99)}]


def one(
    args: argparse.Namespace, quant: str, threads: int, recall: int, cold: bool, tb: int = 0
) -> dict[str, Any]:
    old = Server(
        model_path(args.model, args.from_quant),
        threads=3,
        port=args.port,
        taskset="1-3",
        load_mode=args.load_mode,
        swa_full=args.swa_full,
    )
    system = system_text(5, True)
    past = int(recall * args.trim_to)
    with running(old):
        msgs = fit_memory(old, system, past)
        mem_tokens = old.count(msgs) - old.count([msgs[0], msgs[-1]])
        old.chat([{"role": "user", "content": "x"}], max_tokens=1)  # a served creature
        t0 = time.monotonic()
        old.stop()
        stop_s = time.monotonic() - t0
    if cold:
        drop_caches()
    else:  # warm: the new file was read recently
        Path(model_path(args.model, quant)).read_bytes()
    new = Server(
        model_path(args.model, quant),
        threads=threads,
        threads_batch=tb,
        port=args.port,
        taskset="1-3",
        load_mode=args.load_mode,
        swa_full=args.swa_full,
    )
    with running(new):
        pre: dict[str, Any] = {}
        if args.prefill:
            pre = new.prefill(system)
        r = new.chat(msgs, max_tokens=1)
    prefill_s = pre.get("wall_s", 0.0)
    rec = {
        "quant": quant,
        "threads": threads,
        "threads_batch": tb or threads,
        "recall": recall,
        "memory_tokens": mem_tokens,
        "cold": cold,
        "load_mode": args.load_mode or "mmap",
        "prefill": bool(args.prefill),
        "stop_s": round(stop_s, 1),
        "load_s": round(new.load_s, 1),
        "prefill_tokens": pre.get("prompt_n"),
        "prefill_s": round(prefill_s, 1),
        "reread_tokens": r.get("prompt_n"),
        "reread_s": round(r["wall_s"], 1),
        "pp_tok_s": r.get("prompt_per_s"),
        "total_s": round(stop_s + new.load_s + prefill_s + r["wall_s"], 1),
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
    ap.add_argument(
        "--case", action="append", required=True, help="QUANT:THREADS:RECALL[:THREADS_BATCH]"
    )
    ap.add_argument("--port", type=int, default=8094)
    ap.add_argument("--warm", action="store_true", help="also measure warm reloads")
    ap.add_argument("--prefill", action="store_true", help="read the system prompt first")
    ap.add_argument("--load-mode", default="", help="mmap (default) | none | dio")
    ap.add_argument("--swa-full", action="store_true")
    ap.add_argument("--trim-to", type=float, default=0.85)
    ap.add_argument("--out")
    args = ap.parse_args()
    results = []
    for c in args.case:
        q, th, rc, *tb = c.split(":")
        t_b = int(tb[0]) if tb else 0
        results.append(one(args, q, int(th), int(rc), cold=True, tb=t_b))
        if args.out:  # keep what is done if a later case fails
            write_json(
                Path(args.out),
                {"spike": "S4", "model": args.model, "from": args.from_quant, "cases": results},
            )
        if args.warm:
            results.append(one(args, q, int(th), int(rc), cold=False, tb=t_b))
    out = {"spike": "S4", "model": args.model, "from": args.from_quant, "cases": results}
    if args.out:
        write_json(Path(args.out), out)


if __name__ == "__main__":
    main()
