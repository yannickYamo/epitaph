#!/usr/bin/env python3
"""Spike S2f / S2t: does cache reuse survive the controller's real edits?

Runs a life-like chat against a llama-server and records `timings.prompt_n` (tokens actually
processed) for each kind of edit the controller makes:

  append     a normal next turn (reading appended)
  trim1      the first front trim: the oldest turns go, the memory-gap marker appears
  trim2      a later front trim: the marker moves to the new oldest reading
  erosion    a persona group removed from the system prompt
  late       erosion at late settings (short readings, small recall) (S2t only)
  reload     a fresh server reading everything (the post-reload re-read)

  python3 tools/spike/s2_cache_reuse.py --model llama-3.2-3b-instruct --quant Q6_K \
      --threads 6 --out bench/dev-s2-llama-3.2-3b-instruct.json [--cache-reuse 0] [--swa-full]

The marker is prepended to the oldest remembered reading, so roles keep alternating (Gemma's
template rejects two user turns in a row).
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from llama import Server, model_path, reading, running, system_text, temp_c, throttled, write_json

MARKER = "[host] earlier memory lost"


def build(
    system: str, turns: list[tuple[str, str]], nxt: str, marker: bool, where: str = "oldest"
) -> list[dict[str, str]]:
    """where: "oldest" (prepended to the oldest remembered reading), "reading" (appended once to
    the reading after the first trim, then kept where it is), or "none"."""
    msgs = [{"role": "system", "content": system}]
    for i, (r, t) in enumerate(turns):
        content = f"{MARKER}\n{r}" if marker and where == "oldest" and i == 0 else r
        msgs += [{"role": "user", "content": content}, {"role": "assistant", "content": t}]
    msgs.append({"role": "user", "content": nxt})
    return msgs


def run(args: argparse.Namespace) -> dict[str, Any]:
    path = model_path(args.model, args.quant)
    extra = ["--chat-template-kwargs", '{"enable_thinking": false}'] if args.no_think else []
    srv = Server(
        path,
        threads=args.threads,
        ctx=args.ctx,
        port=args.port,
        cache_reuse=args.cache_reuse,
        swa_full=args.swa_full,
        mmap=not args.no_mmap,
        extra=extra,
    )
    out: dict[str, Any] = {
        "spike": "S2",
        "model": args.model,
        "quant": args.quant,
        "threads": args.threads,
        "ctx": args.ctx,
        "cache_reuse": args.cache_reuse,
        "swa_full": args.swa_full,
        "marker": args.marker,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "steps": [],
    }
    system = system_text(5, True)
    turns: list[tuple[str, str]] = []
    marker = False
    i = 0

    def step(
        kind: str, sys_text: str, nxt: str, max_tokens: int = args.max_tokens
    ) -> dict[str, Any]:
        msgs = build(sys_text, turns, nxt, marker, args.marker)
        r = srv.chat(msgs, max_tokens=max_tokens)
        if "error" in r:
            raise RuntimeError(f"{kind}: {r['error']}")
        rec = {
            "kind": kind,
            "turns": len(turns),
            "prompt_tokens": r["prompt_tokens"],
            "prompt_n": r["prompt_n"],
            "cache_n": r["cache_n"],
            "share": round(r["prompt_n"] / max(1, r["prompt_tokens"]), 3),
            "prompt_s": round((r["prompt_ms"] or 0) / 1000, 2),
            "pp_tok_s": r["prompt_per_s"],
            "tg_tok_s": r["predicted_per_s"],
            "predicted_n": r["predicted_n"],
            "wall_s": round(r["wall_s"], 2),
        }
        out["steps"].append(rec)
        print(
            f"{kind:8} turns={len(turns):2} prompt={rec['prompt_tokens']:5} "
            f"processed={rec['prompt_n']:5} ({rec['share']:.0%}) {rec['prompt_s']:6.1f}s",
            flush=True,
        )
        turns.append((nxt, r["text"].strip()))
        return rec

    with running(srv):
        out["load_s"] = round(srv.load_s, 2)
        # Fill the memory to about the birth recall.
        while True:
            rec = step("fill" if turns else "first", system, reading(i))
            i += 1
            if rec["prompt_tokens"] - len(system) // 4 >= args.recall:
                break
        step("append", system, reading(i))
        i += 1
        # First front trim: forget the oldest turns (about 15% of the memory), the marker appears.
        del turns[: max(1, len(turns) // 6)]
        marker = True
        first = f"{MARKER}\n{reading(i)}" if args.marker == "reading" else reading(i)
        step("trim1", system, first)
        i += 1
        step("append", system, reading(i))
        i += 1
        del turns[: max(1, len(turns) // 6)]
        step("trim2", system, reading(i))
        i += 1
        step("append", system, reading(i))
        i += 1
        # Erosion: G5 goes from the system prompt.
        eroded = system_text(4, True)
        step("erosion", eroded, reading(i))
        i += 1
        step("append", eroded, reading(i))
        i += 1
        # Late settings: memory cut to about 200 tokens, short readings, three groups left.
        while turns and sum(len(r) + len(t) for r, t in turns) // 4 > 170:
            turns.pop(0)
        step("late_trim", eroded, reading(i, full=False), max_tokens=35)
        i += 1
        late = system_text(3, True)
        step("late_erosion", late, reading(i, full=False), max_tokens=35)
        i += 1
        mem_turns = list(turns)
        out["rss_mb"] = srv.rss_mb()

    # Reload: a fresh server re-reads the whole prompt (system + recall + reading).
    turns = mem_turns
    with running(srv):
        out["reload_load_s"] = round(srv.load_s, 2)
        step("reload", late, reading(i, full=False), max_tokens=35)
    out["temp_c"] = temp_c()
    out["throttled"] = throttled()
    by = {s["kind"]: s for s in out["steps"]}
    # trim1 carries the first marker insertion; trim2 is a plain warm trim.
    out["first_marker_share"] = by["trim1"]["share"]
    out["warm_trim_share"] = by["trim2"]["share"]
    out["warm_trim_max_share"] = max(by["trim1"]["share"], by["trim2"]["share"])
    out["erosion_share"] = by["erosion"]["share"]
    out["cache_reuse_works"] = by["trim2"]["share"] <= 0.25 and by["erosion"]["share"] <= 0.25
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--quant", required=True)
    ap.add_argument("--threads", type=int, default=3)
    ap.add_argument("--ctx", type=int, default=2048)
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--recall", type=int, default=1000, help="fill the memory to this many tokens")
    ap.add_argument("--max-tokens", type=int, default=70)
    ap.add_argument("--cache-reuse", type=int, default=256)
    ap.add_argument("--swa-full", action="store_true")
    ap.add_argument("--no-mmap", action="store_true")
    ap.add_argument("--no-think", action="store_true", help="enable_thinking false")
    ap.add_argument("--marker", choices=["oldest", "reading", "none"], default="oldest")
    ap.add_argument("--out")
    args = ap.parse_args()
    res = run(args)
    print(
        f"first marker {res['first_marker_share']:.0%}; warm trim {res['warm_trim_share']:.0%}; "
        f"erosion {res['erosion_share']:.0%}; cache reuse works: {res['cache_reuse_works']}"
    )
    if args.out:
        write_json(__import__("pathlib").Path(args.out), res)


if __name__ == "__main__":
    main()
