#!/usr/bin/env python3
"""Spike S1c: heat, power and speed drift under 30 minutes of thoughts (BUILD_PLAN 8.5).

Thought after thought at step 0 with a rolling memory, while a sampler logs temperature,
`vcgencmd get_throttled` and the ARM clock every 5 s.
Go: no under-voltage bit ever; throttling (bits 1, 2, 3 now) under 10% of samples; tokens/s in
the last 5 minutes within 10% of the first 5.

Round 2 (review item F8) explains the drift, so every thought also logs:
  - the slot's logical context from GET /slots (n_past = prompt + decoded tokens, n_ctx);
  - with --kv-debug, the KV cache's high-water mark: the cells attention runs over
    (llama.cpp's n_kv is the highest used cell + 1, padded to 256). A trim frees cells at the
    front and cache reuse shifts the kept turns' positions, not their cells; the freed cells
    are refilled from the front, so the mark stays at the largest context held (round 2:
    speed follows the mark, R2 0.99, not n_past);
  - variants: --restart-every-min restarts the server (the next request re-reads everything
    into a fresh cache), --compact-every-min saves and restores the slot, which writes the
    cells back from cell 0 without the holes.

Memory is real: the model's own thoughts (thinking off), trimmed by whole turns from the front
once the past exceeds --memory tokens (counted by the server), down to 0.85 of it, like
`Memory` with `trim_to = 0.85`.

Run it detached on the Pi (an SSH drop must not end it), e.g. through pi_run.sh:
  tools/spike/pi_run.sh 40 s1c s1c_soak.py --model qwen3-1.7b --quant Q8_0 --kv-debug \\
      --out bench/spike/s1c-pi4-qwen3-1.7b-r2.json
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from llama import Server, model_path, reading, system_text, temp_c, throttled, write_json


def arm_mhz() -> float | None:
    try:
        out = subprocess.run(
            ["vcgencmd", "measure_clock", "arm"], capture_output=True, text=True, timeout=5
        ).stdout
        return int(out.strip().split("=")[1]) / 1e6
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--quant", required=True)
    ap.add_argument("--threads", type=int, default=3)
    ap.add_argument("--threads-batch", type=int, default=3)
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--memory", type=int, default=1000, help="past tokens before a trim")
    ap.add_argument("--max-tokens", type=int, default=70)
    ap.add_argument("--cache-reuse", type=int, default=32)
    ap.add_argument("--restart-every-min", type=float, default=0)
    ap.add_argument("--compact-every-min", type=float, default=0)
    ap.add_argument("--kv-debug", action="store_true")
    ap.add_argument("--load-mode", default="")
    ap.add_argument("--taskset", default="1-3")
    ap.add_argument("--port", type=int, default=8095)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    samples: list[dict[str, Any]] = []
    stop = threading.Event()

    def sampler() -> None:
        t0 = time.monotonic()
        while not stop.is_set():
            th = throttled() or "throttled=0x0"
            samples.append(
                {"t": round(time.monotonic() - t0, 1), "temp_c": temp_c(),
                 "throttled": int(th.split("=")[1], 16), "arm_mhz": arm_mhz()}
            )  # fmt: skip
            stop.wait(5)

    slot_dir = "/dev/shm/epitaph-s1c" if args.compact_every_min else ""
    if slot_dir:
        Path(slot_dir).mkdir(exist_ok=True)
    srv = Server(
        model_path(args.model, args.quant),
        threads=args.threads,
        threads_batch=args.threads_batch,
        cache_reuse=args.cache_reuse,
        port=args.port,
        taskset=args.taskset or None,
        load_mode=args.load_mode,
        slot_save_path=slot_dir,
        kv_debug=args.kv_debug,
        log=Path(slot_dir or "/dev/shm") / f"s1c-{args.port}.log",
    )
    system = system_text(5, True)
    thoughts: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    th = threading.Thread(target=sampler, daemon=True)
    srv.start()
    sys_tokens = int(srv.prefill(system).get("prompt_tokens") or 0)
    th.start()
    t0 = time.monotonic()
    last_restart = last_compact = t0
    turns: list[tuple[str, str]] = []
    i = 0
    try:
        while time.monotonic() - t0 < args.minutes * 60:
            now = time.monotonic()
            if args.restart_every_min and now - last_restart >= args.restart_every_min * 60:
                srv.stop()
                load = srv.start()
                events.append(
                    {"t": round(now - t0, 1), "event": "restart", "load_s": round(load, 1)}
                )
                last_restart = time.monotonic()
            if args.compact_every_min and now - last_compact >= args.compact_every_min * 60:
                before = srv.kv_marks()
                s, save_s = srv.slot_action("save", "s1c.bin")
                r, restore_s = srv.slot_action("restore", "s1c.bin")
                events.append(
                    {"t": round(now - t0, 1), "event": "compact", "save_s": round(save_s, 2),
                     "restore_s": round(restore_s, 2), "n_saved": s.get("n_saved"),
                     "n_restored": r.get("n_restored"), "bytes": s.get("n_written"),
                     "kv_high_before": max((n for n, _ in before), default=None)}
                )  # fmt: skip
                last_compact = time.monotonic()
            msgs = [{"role": "system", "content": system}]
            for rd, tx in turns:
                msgs += [{"role": "user", "content": rd}, {"role": "assistant", "content": tx}]
            msgs.append({"role": "user", "content": reading(i)})
            srv.kv_marks()  # skip what the previous step logged
            res = srv.chat(msgs, max_tokens=args.max_tokens, seed=i)
            if "error" in res:
                thoughts.append({"t": round(time.monotonic() - t0, 1), "error": res["error"]})
                break
            marks = srv.kv_marks()
            slot = srv.slot()
            thoughts.append(
                {"t": round(time.monotonic() - t0, 1), "tg_tok_s": res["predicted_per_s"],
                 "pp_tok_s": res["prompt_per_s"], "prompt_n": res["prompt_n"],
                 "prompt_tokens": res["prompt_tokens"], "predicted_n": res["predicted_n"],
                 "n_past": slot.get("n_past"), "n_ctx": slot.get("n_ctx"),
                 "kv_high": max((n for n, _ in marks), default=None),
                 "kv_used": marks[-1][1] if marks else None,
                 "turns": len(turns), "text": res["text"]}
            )  # fmt: skip
            print(
                {
                    k: thoughts[-1][k]
                    for k in ("t", "tg_tok_s", "prompt_tokens", "n_past", "kv_high")
                },
                flush=True,
            )
            turns.append((reading(i), res["text"]))
            past = int(res["prompt_tokens"] or 0) + int(res["predicted_n"] or 0) - sys_tokens
            if past > args.memory:
                per_turn = past / len(turns)
                while turns and per_turn * len(turns) > args.memory * 0.85:
                    turns.pop(0)
            i += 1
    finally:
        stop.set()
        th.join()
        srv.stop()
    dur = args.minutes * 60

    def rate(a: float, b: float) -> float:
        xs = [x["tg_tok_s"] for x in thoughts if "tg_tok_s" in x and a <= x["t"] < b]
        return statistics.fmean(xs) if xs else 0.0

    first, last = rate(0, 300), rate(dur - 300, dur + 600)
    uv = any(s["throttled"] & 0x10001 for s in samples)
    thr = sum(1 for s in samples if s["throttled"] & 0xE) / max(1, len(samples))
    out = {
        "spike": "S1c",
        "round": 2,
        "model": args.model,
        "quant": args.quant,
        "threads": args.threads,
        "threads_batch": args.threads_batch,
        "minutes": args.minutes,
        "memory_tokens": args.memory,
        "restart_every_min": args.restart_every_min,
        "compact_every_min": args.compact_every_min,
        "system_tokens": sys_tokens,
        "thoughts": len(thoughts),
        "tg_first_5min": round(first, 3),
        "tg_last_5min": round(last, 3),
        "drift": round((first - last) / first, 3) if first else None,
        "under_voltage_ever": uv,
        "throttled_share": round(thr, 3),
        "max_temp_c": max((s["temp_c"] or 0) for s in samples) if samples else None,
        "min_arm_mhz": min((s["arm_mhz"] or 0) for s in samples) if samples else None,
        "final_throttled": throttled(),
        "events": events,
        "samples": samples,
        "per_thought": thoughts,
    }
    out["ok"] = (not uv) and thr < 0.10 and (out["drift"] is not None and out["drift"] < 0.10)
    write_json(Path(args.out).expanduser(), out)
    print({k: v for k, v in out.items() if k not in ("samples", "per_thought", "events")})


if __name__ == "__main__":
    main()
