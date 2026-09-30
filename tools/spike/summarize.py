#!/usr/bin/env python3
"""Print markdown tables of part A's spike results (for docs/SPIKE.md).

  python3 tools/spike/summarize.py [s1|s2|s6|s4|s1c|bench]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "bench"


def load(pattern: str) -> list[tuple[str, dict]]:
    return [(p.name, json.loads(p.read_text())) for p in sorted(ROOT.glob(pattern))]


def s2() -> None:
    print("| Run | Model | reuse | first marker | warm trim | erosion | late trim (tokens) | late erosion | reload re-read | works |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for name, d in load("spike/s2-*.json"):
        by = {s["kind"]: s for s in d["steps"]}
        lt = by.get("late_trim", {})
        print(
            f"| {name.removeprefix('s2-').removesuffix('.json')} | {d['model']} {d['quant']} "
            f"| {d['cache_reuse']}{' swa-full' if d.get('swa_full') else ''} "
            f"| {d['first_marker_share']:.0%} | {d['warm_trim_share']:.0%} | {d['erosion_share']:.0%} "
            f"| {lt.get('share', 0):.0%} ({lt.get('prompt_n')}) | {by['late_erosion']['share']:.0%} "
            f"| {by['reload']['prompt_n']} tok | {'yes' if d['cache_reuse_works'] else 'no'} |"
        )


def s6() -> None:
    print("| Model | system kept | 2 user turns | count exact | overhead/msg | DRY | grammar | prefill | raw | hygiene |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for _, d in load("spike/s6-*.json"):
        c = d["count"]
        yn = lambda v: "yes" if v else "no"  # noqa: E731
        print(
            f"| {d['model']} {d['quant']} | {yn(d['system_text_in_prompt'])} | {yn(d['two_user_turns_ok'])} "
            f"| {yn(c['render_matches_server'])} ({c['render_tokenize']} vs {c['server_prompt_tokens']}) "
            f"| {c['overhead_per_message']} | {yn(d['dry_and_penalties_ok'])} | {yn(d['grammar_ok'])} "
            f"| {yn(d['prefill_renders_open'])} | {yn(d['raw_completion_ok'])} "
            f"| {', '.join(d['hygiene_flags']) or 'clean'} |"
        )


def s1() -> None:
    print("| Model | quant | thr | mmap | load s | birth thought s | pp tok/s | tg tok/s (birth / depth) | headroom MB | fits | throttled |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    rows = load("measured/pi4-*.json") + load("spike/s1*-pi4-*.json")
    for _, d in rows:
        if "quant" not in d or "headroom_mb" not in d:
            continue
        print(
            f"| {d['model']} | {d['quant']} | {d['threads']} | {'mmap' if d['mmap'] else 'none'} "
            f"| {d.get('load_s')} | {d.get('birth_thought_s', '')} | {d.get('pp_tok_s', '')} "
            f"| {d.get('tg_tok_s_birth', '')} / {d.get('tg_tok_s', '')} | {d['headroom_mb']} "
            f"| {'yes' if d['fits'] else 'NO'} | {d.get('throttled', '').replace('throttled=', '')} |"
        )


def bench() -> None:
    p = ROOT / "spike" / "s1b-pi4-llama-bench.jsonl"
    print("| Model file | pp128 tok/s | tg32 tok/s |")
    print("|---|---|---|")
    rows: dict[str, dict[str, float]] = {}
    for line in p.read_text().splitlines() if p.exists() else []:
        if not line.startswith("{"):
            continue
        d = json.loads(line)
        key = Path(d["model_filename"]).parent.name + "/" + Path(d["model_filename"]).stem
        kind = "pp" if d["n_prompt"] else "tg"
        rows.setdefault(key, {})[kind] = d["avg_ts"]
    for k, v in rows.items():
        print(f"| {k} | {v.get('pp', 0):.2f} | {v.get('tg', 0):.2f} |")


def s4() -> None:
    print("| Case | cold | stop s | load s | re-read tokens | re-read s | total s | go (<=180 s) |")
    print("|---|---|---|---|---|---|---|---|")
    for _, d in load("spike/s4-*.json"):
        for c in d["cases"]:
            print(
                f"| {d['model']} {c['quant']} t{c['threads']} recall {c['recall']} | {c['cold']} "
                f"| {c['stop_s']} | {c['load_s']} | {c['reread_tokens']} | {c['reread_s']} "
                f"| {c['total_s']} | {'yes' if c['ok'] else 'no'} |"
            )


def s1c() -> None:
    for _, d in load("spike/s1c-*.json"):
        print({k: v for k, v in d.items() if k not in ("samples", "per_thought")})


if __name__ == "__main__":
    which = sys.argv[1:] or ["s6", "s2", "bench", "s1", "s4", "s1c"]
    for w in which:
        print(f"\n### {w}\n")
        globals()[w]()
