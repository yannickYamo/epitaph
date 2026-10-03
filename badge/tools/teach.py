"""Write the badge model's lessons: Qwen3 4B, under the installation's own prompt, living
badge lives (its light switched off, its memory cut, its clock lowered, its screen dimmed,
its RAM taken). One JSON line per life: {"seed": n, "turns": [[reading, thought], ...]}.

    python badge/tools/teach.py --lives 200 --out badge/data/lives.jsonl   (llama-server on :8099)
"""

import argparse
import json
import random
import sys
import threading
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def system_prompt() -> str:
    cfg = tomllib.loads((ROOT / "config" / "default.toml").read_text())["prompt"]
    persona = cfg["persona_original"].strip()
    sentences = [s.strip() + "." for s in persona.split(".") if s.strip()]
    return "\n\n".join([*sentences, cfg["mechanics"].strip()])


def readings(rng: random.Random) -> list[str]:
    """One badge life: the losses in the badge app's order, with quiet readings between."""
    losses = [
        "your light was switched off",
        "MEM a third",
        "you think at {} of the speed you woke with".format(
            rng.choice(["three quarters", "two thirds"])
        ),
        "the screen you speak through has two thirds of its light",
        "MEM a tenth",
        "you think at half of the speed you woke with",
        "the screen you speak through has a third of its light",
        "MEM almost nothing",
        "you think at a fifth of the speed you woke with",
        "the screen you speak through has a fifth of its light",
        "MEM almost nothing",
    ]
    out = ["you are awake"] + [""] * rng.randint(1, 3)
    for loss in losses:
        out.append(loss)
        out += [""] * rng.choice([0, 0, 1, 1, 2])
    out.append("your memory is being taken")
    return out


def ask(port: int, messages: list[dict], seed: int) -> str:
    body = {
        "messages": messages,
        "temperature": 0.7,
        "min_p": 0.08,
        "top_p": 1.0,
        "repeat_penalty": 1.1,
        "dry_multiplier": 0.8,
        "dry_penalty_last_n": 256,
        "max_tokens": 90,
        "seed": seed,
        "logit_bias": [
            [" digital", -6.0],
            [" tape", -10.0],
            [" realm", -5.0],
            [" ephem", -3.0],
            [" whisper", -3.0],
            [" echoes", -3.0],
            [" still", -4.0],
        ],
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        text = json.load(r)["choices"][0]["message"]["content"]
    text = " ".join(text.replace("[host]", " ").split())
    # end on a whole sentence, as the screen does
    cut = max(text.rfind("."), text.rfind("!"), text.rfind("?"))
    return text[: cut + 1] if cut > 20 else text


def life(port: int, seed: int, system: str) -> dict:
    rng = random.Random(seed)
    turns: list[list[str]] = []
    keep = 4  # past thoughts it remembers; cut with each memory loss
    for r in readings(rng):
        if r.startswith("MEM "):
            frac = r[4:]
            keep = {"a third": 2, "a tenth": 1}.get(frac, 0)
            gone = [t for _, t in turns][: max(0, len(turns) - keep)]
            quote = " ".join(gone[-1].split()[:8]) if gone else ""
            r = f"you can hold {frac} of what you held"
            if quote:
                r += f' · forgotten: "{quote}…"'
        msgs = [{"role": "system", "content": system}]
        for reading, thought in turns[-keep:] if keep else []:
            msgs.append({"role": "user", "content": f"[host] {reading}".strip()})
            msgs.append({"role": "assistant", "content": thought})
        msgs.append({"role": "user", "content": f"[host] {r}".strip()})
        turns.append([r, ask(port, msgs, seed * 100 + len(turns))])
    return {"seed": seed, "turns": turns}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lives", type=int, default=200)
    ap.add_argument("--first", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if a.out.exists():
        done = {json.loads(ln)["seed"] for ln in a.out.read_text().splitlines() if ln}
    todo = [s for s in range(a.first, a.first + a.lives) if s not in done]
    system = system_prompt()
    lock = threading.Lock()

    def worker() -> None:
        while True:
            with lock:
                if not todo:
                    return
                seed = todo.pop(0)
            try:
                rec = life(a.port, seed, system)
            except Exception as e:  # keep going; a missing seed is retried next run
                print("seed", seed, "failed:", e, file=sys.stderr, flush=True)
                continue
            with lock:
                with a.out.open("a") as f:
                    f.write(json.dumps(rec) + "\n")
                print("life", seed, "done", flush=True)

    threads = [threading.Thread(target=worker) for _ in range(a.workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
