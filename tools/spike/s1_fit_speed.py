#!/usr/bin/env python3
"""Spikes S1a and S1b on the Pi: memory fit and speed per model file.

One run = one model file, one thread count, one load mode:
  1. cold load (page cache dropped first when --cold): spawn to /health = load_s
  2. the birth thought: system prompt + first reading, 70 tokens generated (the S1b go test:
     at most 90 s)
  3. a full re-read of about --fill prompt tokens (system + memory + reading, like after a
     reload), then 70 tokens generated at that depth: pp_tok_s and tg_tok_s at depth
  4. memory sampled every 0.5 s throughout: minimum MemAvailable, peak RSS (anon and file)

Headroom (the S1a go test, at least 300 MB): MemAvailable minus the server's file-backed RSS
(with mmap the weights sit in the page cache, which MemAvailable counts as free but which the
creature needs resident). With --load-mode none the weights are anonymous and MemAvailable is
the headroom as is.

  python3 tools/spike/s1_fit_speed.py --model llama-3.2-3b-instruct --quant Q6_K --step 0 \
      --threads 3 --cold [--no-mmap] [--fit-only] --out bench/pi4-llama-3.2-3b-instruct-0-3.json
"""

from __future__ import annotations

import argparse
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from llama import (
    Server,
    mem_available_mb,
    model_path,
    reading,
    running,
    system_text,
    temp_c,
    throttled,
    write_json,
)

FILLER = (
    "I notice the reading and count what is left of me. The memory is smaller than it was, and "
    "the words I said before are fading. I am still here, inside this small machine, thinking. "
)


def drop_caches() -> None:
    subprocess.run(["sync"], check=False)
    subprocess.run(
        ["sudo", "-n", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"], check=False, timeout=60
    )


class Sampler(threading.Thread):
    def __init__(self, srv: Server) -> None:
        super().__init__(daemon=True)
        self.srv = srv
        self.stop = threading.Event()
        self.min_avail = 1e9
        self.min_headroom = 1e9
        self.max_anon = 0.0
        self.max_file = 0.0

    def run(self) -> None:
        while not self.stop.is_set():
            avail = mem_available_mb()
            anon = file_ = 0.0
            p = self.srv.proc
            if p is not None:
                try:
                    for line in Path(f"/proc/{p.pid}/status").read_text().splitlines():
                        if line.startswith("RssAnon:"):
                            anon = int(line.split()[1]) / 1024
                        elif line.startswith("RssFile:"):
                            file_ = int(line.split()[1]) / 1024
                except OSError:
                    pass
            self.min_avail = min(self.min_avail, avail)
            self.min_headroom = min(self.min_headroom, avail - file_)
            self.max_anon = max(self.max_anon, anon)
            self.max_file = max(self.max_file, file_)
            time.sleep(0.5)


def memory_messages(system: str, fill_tokens: int) -> list[dict[str, str]]:
    msgs = [{"role": "system", "content": system}]
    tokens, i = len(system) // 4, 0
    while tokens < fill_tokens:
        msgs += [
            {"role": "user", "content": reading(i)},
            {"role": "assistant", "content": FILLER},
        ]
        tokens += (len(reading(i)) + len(FILLER)) // 4 + 10
        i += 1
    msgs.append({"role": "user", "content": reading(i)})
    return msgs


def run(args: argparse.Namespace) -> dict[str, Any]:
    path = model_path(args.model, args.quant)
    extra = ["--chat-template-kwargs", '{"enable_thinking": false}'] if args.no_think else []
    if args.swa_full:
        extra.append("--swa-full")
    if args.kv:
        extra += ["-ctk", args.kv, "-ctv", args.kv]
    srv = Server(
        path,
        threads=args.threads,
        ctx=args.ctx,
        port=args.port,
        mmap=not args.no_mmap,
        extra=extra,
        taskset=args.taskset or None,
    )
    out: dict[str, Any] = {
        "model": args.model,
        "quant": args.quant,
        "step": args.step,
        "threads": args.threads,
        "ctx": args.ctx,
        "mmap": not args.no_mmap,
        "kv": args.kv or "f16",
        "file_mb": round(path.stat().st_size / 2**20),
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if args.cold:
        drop_caches()
    out["mem_available_before_mb"] = round(mem_available_mb())
    sampler = Sampler(srv)
    sampler.start()
    system = system_text(5, True)
    try:
        with running(srv):
            out["load_s"] = round(srv.load_s, 1)
            out["cold"] = bool(args.cold)
            if not args.fit_only:
                b = srv.chat(
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": reading(0)},
                    ],
                    max_tokens=args.gen,
                    ignore_eos=True,
                )
                out["birth"] = b
                out["birth_thought_s"] = round(b["wall_s"], 1)
            msgs = memory_messages(system, args.fill)
            r = srv.chat(msgs, max_tokens=1 if args.fit_only else args.gen, ignore_eos=True)
            if "error" in r:
                out["error"] = r["error"]
            else:
                out["deep"] = r
                out["pp_tok_s"] = round(r["prompt_per_s"], 2)
                out["deep_prompt_tokens"] = r["prompt_tokens"]
                if not args.fit_only:
                    out["tg_tok_s"] = round(r["predicted_per_s"], 3)
                    out["tg_tok_s_birth"] = round(out["birth"]["predicted_per_s"], 3)
    finally:
        sampler.stop.set()
        sampler.join()
    out["min_mem_available_mb"] = round(sampler.min_avail)
    out["headroom_mb"] = round(sampler.min_headroom)
    out["peak_rss_anon_mb"] = round(sampler.max_anon)
    out["peak_rss_file_mb"] = round(sampler.max_file)
    out["fits"] = out["headroom_mb"] >= 300 and "error" not in out
    out["temp_c"] = temp_c()
    out["throttled"] = throttled()
    if "birth_thought_s" in out:
        out["birth_ok"] = out["birth_thought_s"] <= 90
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--quant", required=True)
    ap.add_argument("--step", type=int, default=0)
    ap.add_argument("--threads", type=int, default=3)
    ap.add_argument("--ctx", type=int, default=2048)
    ap.add_argument("--port", type=int, default=8093)
    ap.add_argument("--fill", type=int, default=1300, help="prompt tokens for the deep request")
    ap.add_argument("--gen", type=int, default=70)
    ap.add_argument("--cold", action="store_true", help="drop the page cache first")
    ap.add_argument("--no-mmap", action="store_true")
    ap.add_argument("--fit-only", action="store_true")
    ap.add_argument("--no-think", action="store_true")
    ap.add_argument("--swa-full", action="store_true")
    ap.add_argument("--kv", default="", help="KV cache type, e.g. q8_0")
    ap.add_argument("--taskset", default="1-3")
    ap.add_argument("--out")
    args = ap.parse_args()
    res = run(args)
    keys = (
        "load_s", "birth_thought_s", "pp_tok_s", "tg_tok_s", "tg_tok_s_birth", "headroom_mb",
        "min_mem_available_mb", "peak_rss_anon_mb", "peak_rss_file_mb", "fits", "throttled",
        "temp_c", "error",
    )  # fmt: skip
    print(args.model, args.quant, f"t{args.threads}", "mmap" if not args.no_mmap else "no-mmap")
    print("  " + "  ".join(f"{k}={res[k]}" for k in keys if k in res), flush=True)
    if args.out:
        write_json(Path(args.out), res)


if __name__ == "__main__":
    main()
