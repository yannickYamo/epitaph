"""Spike S8: a core taken from a running llama-server (CPU affinity narrowed mid-life).

One llama-server as the creature runs it (Qwen3 4B, 3 threads, `dio`, ctx 2048, pinned to
cores 1-3) generates without pause while its affinity is narrowed with `taskset -a -p`: cores
1-3, then 1-2, then 1, then 1-3 again (recovery). Each phase streams chat completions for at
least `--phase-s` seconds and records the arrival time of every token. Output: one JSON line
per phase (tokens, generation tok/s, inter-token gaps, the worst stall, gaps over 2 s and
5 s, temperature, throttling) on stdout and in `--out`.

Runs on the Pi with the controller stopped (it would compete for cores 1-3), as a transient
unit (tools/spike/_pi_unit.sh). Stdlib only.

    python3 tools/spike/s8_affinity.py [--phase-s 180] [--out bench/spike/s8_affinity.jsonl]
"""

from __future__ import annotations

import argparse
import itertools
import json
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from llama import Server, model_path, reading, system_text, throttled

PHASES = ["1-3", "1-2", "1", "1-3"]


def temp_c() -> float | None:
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        return None


def stream(
    server: Server, messages: list[dict[str, str]], max_tokens: int, seed: int
) -> list[float]:
    """One streamed chat completion: the monotonic arrival time of every content chunk."""
    body = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.8,
        "min_p": 0.05,
        "seed": seed,
        "cache_prompt": True,
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        server.url("/v1/chat/completions"),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    times: list[float] = []
    with urllib.request.urlopen(req, timeout=3600) as r:
        for raw in r:
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            choices = chunk.get("choices") or [{}]
            if (choices[0].get("delta") or {}).get("content"):
                times.append(time.monotonic())
    return times


def pin(pid: int, cpus: str) -> str:
    """Narrow (or widen) every thread of pid to cpus; returns taskset's report."""
    out = subprocess.run(
        ["taskset", "-a", "-p", "-c", cpus, str(pid)], capture_output=True, text=True, check=True
    )
    return out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""


def summary(
    cpus: str, gaps: list[float], gen_s: float, tokens: int, t_switch: float
) -> dict[str, Any]:
    gs = sorted(gaps)

    def q(p: float) -> float | None:
        return round(gs[min(len(gs) - 1, int(p * len(gs)))], 3) if gs else None

    return {
        "cpus": cpus,
        "tokens": tokens,
        "gen_s": round(gen_s, 1),
        "tg_tok_s": round(tokens / gen_s, 3) if gen_s else None,
        "gap_p50_s": q(0.5),
        "gap_p90_s": q(0.9),
        "gap_p99_s": q(0.99),
        "gap_max_s": round(gs[-1], 3) if gs else None,
        "gap_mean_s": round(statistics.fmean(gs), 3) if gs else None,
        "gaps_over_2s": sum(g > 2 for g in gs),
        "gaps_over_5s": sum(g > 5 for g in gs),
        "first_gap_after_switch_s": round(t_switch, 3),
        "temp_c": temp_c(),
        "throttled": throttled(),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3-4b-instruct-2507")
    ap.add_argument("--quant", default="Q4_K_M")
    ap.add_argument("--phase-s", type=float, default=180.0)
    ap.add_argument("--max-tokens", type=int, default=48)
    ap.add_argument("--out", default="bench/spike/s8_affinity.jsonl")
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    server = Server(
        model_path(args.model, args.quant),
        threads=3,
        threads_batch=3,
        ctx=2048,
        cache_reuse=32,
        load_mode="dio",
        taskset="1-3",
        port=8098,
    )
    system = system_text(groups=5)
    load_s = server.start()
    assert server.proc is not None
    # taskset execs llama-server, so the child pid is llama-server itself.
    pid = server.proc.pid
    print(
        json.dumps({"load_s": round(load_s, 1), "pid": pid, "throttled": throttled()}), flush=True
    )
    seed = 1
    turn = 0
    try:
        for cpus in PHASES:
            report = pin(pid, cpus)
            switched = time.monotonic()
            gaps: list[float] = []
            gen_s = 0.0
            tokens = 0
            first_after_switch = 0.0
            while time.monotonic() - switched < args.phase_s:
                msgs = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": reading(turn)},
                ]
                t0 = time.monotonic()
                times = stream(server, msgs, args.max_tokens, seed)
                seed += 1
                turn += 1
                if not times:
                    continue
                if not first_after_switch:
                    first_after_switch = times[0] - t0
                gaps += [b - a for a, b in itertools.pairwise(times)]
                gen_s += times[-1] - times[0]
                tokens += len(times) - 1
            row = summary(cpus, gaps, gen_s, tokens, first_after_switch)
            row["taskset"] = report
            print(json.dumps(row), flush=True)
            with out.open("a") as f:
                f.write(json.dumps(row) + "\n")
    finally:
        server.stop()


if __name__ == "__main__":
    main()
