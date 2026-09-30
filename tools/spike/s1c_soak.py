#!/usr/bin/env python3
"""Spike S1c: heat and power under 30 minutes of sustained generation (BUILD_PLAN 8.5).

Thought after thought at step 0 and 3 threads, with a rolling memory of about 1000 tokens,
while a sampler logs temperature, `vcgencmd get_throttled` and the ARM clock every 5 s.
Go: no under-voltage bit ever; throttling (bits 1, 2, 3 now) under 10% of samples; tokens/s in
the last 5 minutes within 10% of the first 5.

Run it detached on the Pi (an SSH drop must not end it):
  sudo systemd-run --unit=s1c --uid=pi --gid=pi --setenv=HOME=/home/pi \
      python3 ~/epitaph-spike/tools/spike/s1c_soak.py --model ... --quant Q6_K --minutes 30 \
      --out ~/epitaph-spike/s1c.json
"""

from __future__ import annotations

import argparse
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from llama import Server, model_path, reading, running, system_text, temp_c, throttled, write_json


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
    ap.add_argument("--minutes", type=float, default=30)
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

    srv = Server(
        model_path(args.model, args.quant), threads=args.threads, port=args.port, taskset="1-3"
    )
    system = system_text(5, True)
    thoughts: list[dict[str, Any]] = []
    th = threading.Thread(target=sampler, daemon=True)
    with running(srv):
        th.start()
        t0 = time.monotonic()
        turns: list[tuple[str, str]] = []
        i = 0
        while time.monotonic() - t0 < args.minutes * 60:
            msgs = [{"role": "system", "content": system}]
            for r, t in turns:
                msgs += [{"role": "user", "content": r}, {"role": "assistant", "content": t}]
            msgs.append({"role": "user", "content": reading(i)})
            res = srv.chat(msgs, max_tokens=70, ignore_eos=True, seed=i)
            if "error" in res:
                thoughts.append({"t": time.monotonic() - t0, "error": res["error"]})
                break
            thoughts.append(
                {"t": round(time.monotonic() - t0, 1), "tg_tok_s": res["predicted_per_s"],
                 "pp_tok_s": res["prompt_per_s"], "prompt_n": res["prompt_n"]}
            )  # fmt: skip
            turns.append((reading(i), res["text"]))
            while sum(len(a) + len(b) for a, b in turns) // 4 > 1000:
                turns.pop(0)
            i += 1
        stop.set()
        th.join()
    dur = args.minutes * 60

    def rate(a: float, b: float) -> float:
        xs = [x["tg_tok_s"] for x in thoughts if "tg_tok_s" in x and a <= x["t"] < b]
        return sum(xs) / len(xs) if xs else 0.0

    first, last = rate(0, 300), rate(dur - 300, dur + 600)
    uv = any(s["throttled"] & 0x10001 for s in samples)
    thr = sum(1 for s in samples if s["throttled"] & 0xE) / max(1, len(samples))
    out = {
        "spike": "S1c",
        "model": args.model,
        "quant": args.quant,
        "threads": args.threads,
        "minutes": args.minutes,
        "thoughts": len(thoughts),
        "tg_first_5min": round(first, 3),
        "tg_last_5min": round(last, 3),
        "drift": round((first - last) / first, 3) if first else None,
        "under_voltage_ever": uv,
        "throttled_share": round(thr, 3),
        "max_temp_c": max((s["temp_c"] or 0) for s in samples) if samples else None,
        "min_arm_mhz": min((s["arm_mhz"] or 0) for s in samples) if samples else None,
        "final_throttled": throttled(),
        "samples": samples,
        "per_thought": thoughts,
    }
    out["ok"] = (not uv) and thr < 0.10 and (out["drift"] is not None and out["drift"] < 0.10)
    write_json(Path(args.out).expanduser(), out)
    print({k: v for k, v in out.items() if k not in ("samples", "per_thought")})


if __name__ == "__main__":
    main()
