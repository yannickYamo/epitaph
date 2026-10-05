#!/usr/bin/env python3
"""Spike S4b: carry the KV cache across a reload instead of re-reading it.

llama-server b11277 can save a slot's KV cache to a file and restore it
(`POST /slots/0?action=save|restore`, files under `--slot-save-path`). A reload then becomes:

  1. save the slot to /dev/shm (RAM; the SD card is far too slow)
  2. stop the old server
  3. start the next quant with the same --slot-save-path
  4. restore the slot: the old quant's KV cache for the whole old memory
  5. send the next request with the memory cut to the post-reload recall: cache reuse
     (`--cache-reuse`) keeps the kept turns and shifts them, so only the new reading (and
     any junction the cut left) is read

Measured: file size, save, stop, load, restore, the first request (to the first token), total
from the reload's start; whether the new quant accepts the old quant's cache; and a few real
thoughts after the restore, logged for a sanity read. --control repeats the same first request
on a fresh server without the restore (the plain re-read S4 measures), same prompt, same seed.

  python3 tools/spike/s4b_slot_handover.py --model qwen3-1.7b --from-quant Q8_0 \\
      --to-quant Q4_K_M --recall 300 --control --out bench/spike/s4b-pi4-qwen3-1.7b.json
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any

from llama import Server, model_path, reading, system_text, temp_c, throttled, write_json
from s1_fit_speed import drop_caches


def cut(srv: Server, msgs: list[dict[str, str]], budget: int) -> list[dict[str, str]]:
    """Drop the oldest whole turns until the past fits `budget` tokens (the reload cut)."""
    system, past, nxt = msgs[0], msgs[1:-1], msgs[-1]
    base = srv.count([system, nxt])
    while past and srv.count([system, *past, nxt]) - base > budget:
        past = past[2:]
    return [system, *past, nxt]


def server(args: argparse.Namespace, quant: str, threads: int) -> Server:
    return Server(
        model_path(args.model, quant),
        threads=threads,
        threads_batch=args.threads_batch,
        port=args.port,
        cache_reuse=args.cache_reuse,
        load_mode=args.load_mode,
        swa_full=args.swa_full,
        slot_save_path=args.slot_dir,
        taskset=args.taskset or None,
    )


def thought(srv: Server, msgs: list[dict[str, str]], seed: int, n: int) -> dict[str, Any]:
    return srv.chat(msgs, max_tokens=n, seed=seed, temperature=0.8, min_p=0.06)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--from-quant", required=True)
    ap.add_argument("--to-quant", required=True)
    ap.add_argument("--threads-old", type=int, default=3)
    ap.add_argument("--threads-new", type=int, default=3)
    ap.add_argument("--threads-batch", type=int, default=3)
    ap.add_argument("--recall", type=int, default=300, help="post-reload recall (x trim_to)")
    ap.add_argument("--trim-to", type=float, default=0.85)
    ap.add_argument("--turns", type=int, default=6, help="real thoughts before the reload")
    ap.add_argument("--after", type=int, default=3, help="real thoughts after the restore")
    ap.add_argument("--max-tokens", type=int, default=70)
    ap.add_argument("--cache-reuse", type=int, default=32)
    ap.add_argument("--load-mode", default="dio")
    ap.add_argument("--swa-full", action="store_true")
    ap.add_argument("--slot-dir", default="/dev/shm/epitaph-slots")
    ap.add_argument("--control", action="store_true", help="also time the plain re-read")
    ap.add_argument("--no-cold", action="store_true", help="never drop the page cache (laptop)")
    ap.add_argument("--taskset", default="1-3")
    ap.add_argument("--port", type=int, default=8096)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    Path(args.slot_dir).mkdir(parents=True, exist_ok=True)
    fname = f"{args.model}.bin"
    fpath = Path(args.slot_dir) / fname
    system = system_text(5, True)
    out: dict[str, Any] = {
        "spike": "S4b",
        "model": args.model,
        "from": args.from_quant,
        "to": args.to_quant,
        "threads_old": args.threads_old,
        "threads_new": args.threads_new,
        "threads_batch": args.threads_batch,
        "recall": args.recall,
        "load_mode": args.load_mode,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "throttled_before": throttled(),
    }

    def save() -> None:
        write_json(Path(args.out), out)

    # 1. A life before the reload: the system prompt, then real thoughts.
    old = server(args, args.from_quant, args.threads_old)
    if not args.no_cold:
        drop_caches()
    old.start()
    out["old_load_s"] = round(old.load_s, 1)
    out["old_prefill"] = old.prefill(system)
    msgs: list[dict[str, str]] = [{"role": "system", "content": system}]
    before: list[dict[str, Any]] = []
    for i in range(args.turns):
        msgs.append({"role": "user", "content": reading(i)})
        r = thought(old, msgs, i, args.max_tokens)
        before.append(r)
        msgs.append({"role": "assistant", "content": r.get("text", "")})
        print("before", i, repr(r.get("text", ""))[:120], flush=True)
    out["before"] = before
    nxt = {"role": "user", "content": reading(args.turns).replace("6-bit", "4-bit (was 6-bit)")}
    full = [*msgs, nxt]
    out["memory_tokens_before"] = old.count(full) - old.count([msgs[0], nxt])
    kept = cut(old, full, int(args.recall * args.trim_to))
    out["memory_tokens_after"] = old.count(kept) - old.count([kept[0], kept[-1]])
    out["prompt_tokens_after"] = old.count(kept)
    out["slot_before_save"] = old.slot()
    save()

    # 2. The reload with the slot carried over.
    t0 = time.monotonic()
    sv, save_s = old.slot_action("save", fname)
    out["save"] = sv
    out["save_s"] = round(save_s, 2)
    out["file_mb"] = round(fpath.stat().st_size / 2**20, 1) if fpath.exists() else None
    t1 = time.monotonic()
    old.stop()
    out["stop_s"] = round(time.monotonic() - t1, 2)
    if not args.no_cold:
        # The new quant's file is not in the page cache in a real life (dio never caches).
        drop_caches()
    new = server(args, args.to_quant, args.threads_new)
    new.start()
    out["load_s"] = round(new.load_s, 1)
    rs, restore_s = new.slot_action("restore", fname)
    out["restore"] = rs
    out["restore_s"] = round(restore_s, 2)
    out["accepted"] = "error" not in rs and int(rs.get("n_restored") or 0) > 0
    out["slot_after_restore"] = new.slot()
    first = new.chat(kept, max_tokens=1, seed=100)
    out["first"] = first
    out["first_s"] = round(first["wall_s"], 1)
    out["total_s"] = round(time.monotonic() - t0, 1)
    out["ok"] = out["total_s"] <= 180
    print({k: out[k] for k in ("file_mb", "save_s", "stop_s", "load_s", "restore_s", "accepted", "first_s", "total_s")}, flush=True)  # fmt: skip
    print(
        "first request",
        {k: first.get(k) for k in ("prompt_n", "cache_n", "prompt_tokens")},
        flush=True,
    )
    fpath.unlink(missing_ok=True)
    save()

    # 3. A few real thoughts after the restore (the first one on the same prompt).
    after: list[dict[str, Any]] = []
    msgs = kept
    for j in range(args.after):
        r = thought(new, msgs, 100 + j, args.max_tokens)
        r["slot"] = new.slot()
        after.append(r)
        print("after", j, repr(r.get("text", ""))[:160], flush=True)
        msgs = [*msgs, {"role": "assistant", "content": r.get("text", "")},
                {"role": "user", "content": reading(args.turns + 1 + j)}]  # fmt: skip
    out["after"] = after
    new.stop()
    save()

    # 4. Control: the same first request on a fresh server, no restore (a plain re-read).
    if args.control:
        ctl = server(args, args.to_quant, args.threads_new)
        ctl.start()
        pre = ctl.prefill(system)
        c1 = ctl.chat(kept, max_tokens=1, seed=100)
        c2 = thought(ctl, kept, 100, args.max_tokens)
        ctl.stop()
        out["control"] = {
            "load_s": round(ctl.load_s, 1),
            "prefill": pre,
            "prefill_s": round(pre["wall_s"], 1),
            "first": c1,
            "first_s": round(c1["wall_s"], 1),
            "reread_total_s": round(ctl.load_s + pre["wall_s"] + c1["wall_s"] + out["stop_s"], 1),
            "thought": c2,
        }
        print(
            "control", out["control"]["reread_total_s"], repr(c2.get("text", ""))[:160], flush=True
        )
    out["throttled"] = throttled()
    out["temp_c"] = temp_c()
    save()
    os.sync()


if __name__ == "__main__":
    main()
