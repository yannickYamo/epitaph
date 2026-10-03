"""Teach the 260K story model epitaph's voice: fine-tune it on badge lives written by Qwen3 4B
under the installation's prompt (teach.py) and on real Pi thoughts, then write a llama2.c
checkpoint for convert_tinyllama.py.

    python badge/tools/finetune.py stories260K.bin tok512.bin badge/data/lives.jsonl out.bin \
        badge/data/pi_thoughts.txt [--steps 200]
"""

import argparse
import json
import math
import struct
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

sys.path.insert(0, str(Path(__file__).resolve().parent))
from convert_tinyllama import read_checkpoint

ASCII = {"’": "'", "‘": "'", "“": '"', "”": '"', "—": ", ", "–": "-", "…": "...", " ": " "}


def clean(s: str) -> str:
    for a, b in ASCII.items():
        s = s.replace(a, b)
    return "".join(c for c in s if 32 <= ord(c) < 127 or c == "\n").replace(" ,", ",")


class Tok:
    """llama2.c's encoder: characters, then the best-scoring merges."""

    def __init__(self, path: Path) -> None:
        raw = path.read_bytes()
        at = 4
        self.pieces, self.scores = [], []
        for _ in range(512):
            score, n = struct.unpack("fi", raw[at : at + 8])
            at += 8
            self.pieces.append(raw[at : at + n].decode("utf-8", "replace"))
            self.scores.append(score)
            at += n
        self.ids = {p: i for i, p in enumerate(self.pieces)}

    def encode(self, text: str) -> list[int]:
        toks = []
        for ch in text:
            if ch in self.ids:
                toks.append(self.ids[ch])
            else:
                toks += [b + 3 for b in ch.encode()]
        while True:
            best, at = -1e10, -1
            for i in range(len(toks) - 1):
                m = self.ids.get(self.pieces[toks[i]] + self.pieces[toks[i + 1]])
                if m is not None and self.scores[m] > best:
                    best, at, mid = self.scores[m], i, m
            if at < 0:
                return toks
            toks[at : at + 2] = [mid]


class Llama(torch.nn.Module):
    def __init__(self, cfg, w):
        super().__init__()
        self.cfg = cfg
        self.p = torch.nn.ParameterDict(
            {
                k: torch.nn.Parameter(torch.tensor(np.array(v), dtype=torch.float32))
                for k, v in w.items()
            }
        )

    def forward(self, idx):
        dim, _hidden, layers, heads, kv_heads, _vocab, _seq = self.cfg
        hs = dim // heads
        B, T = idx.shape
        p = self.p
        x = p["emb"][idx]
        pos = torch.arange(T)
        freq = 1.0 / 10000 ** (torch.arange(0, hs, 2).float() / hs)
        ang = pos[:, None] * freq[None]
        cos, sin = ang.cos(), ang.sin()

        def rope(t):  # t: B, T, H, hs ; pairs (2i, 2i+1)
            a, b = t[..., 0::2], t[..., 1::2]
            return torch.stack(
                (
                    a * cos[None, :, None] - b * sin[None, :, None],
                    a * sin[None, :, None] + b * cos[None, :, None],
                ),
                -1,
            ).flatten(-2)

        def rms(x, w):
            return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + 1e-5) * w

        mask = torch.full((T, T), float("-inf")).triu(1)
        for li in range(layers):
            xb = rms(x, p["rms_att"][li])
            q = (xb @ p["wq"][li].T).view(B, T, heads, hs)
            k = (xb @ p["wk"][li].T).view(B, T, kv_heads, hs)
            v = (xb @ p["wv"][li].T).view(B, T, kv_heads, hs)
            q, k = rope(q), rope(k)
            rep = heads // kv_heads
            k = k.repeat_interleave(rep, 2)
            v = v.repeat_interleave(rep, 2)
            att = (q.transpose(1, 2) @ k.transpose(1, 2).transpose(-1, -2)) / math.sqrt(hs) + mask
            o = (att.softmax(-1) @ v.transpose(1, 2)).transpose(1, 2).reshape(B, T, dim)
            x = x + o @ p["wo"][li].T
            xb = rms(x, p["rms_ffn"][li])
            x = x + (F.silu(xb @ p["w1"][li].T) * (xb @ p["w3"][li].T)) @ p["w2"][li].T
        x = rms(x, p["rms_final"])
        return x @ p["emb"].T


def corpus(lives: Path, extra: list[Path]) -> list[str]:
    docs = []
    for ln in lives.read_text().splitlines():
        if not ln.strip():
            continue
        rec = json.loads(ln)
        docs.append(
            "".join(
                f"[host] {r}".rstrip() + "\n" + clean(t).strip() + "\n" for r, t in rec["turns"]
            )
        )
    for p in extra:
        thoughts = [clean(t).strip() for t in p.read_text().splitlines() if t.startswith("    ")]
        docs.append("".join("[host]\n" + t + "\n" for t in thoughts if len(t) > 30))
    return [clean(d) for d in docs]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("checkpoint", type=Path)
    ap.add_argument("tokenizer", type=Path)
    ap.add_argument("lives", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("thoughts", type=Path, nargs="*")
    ap.add_argument("--steps", type=int, default=200)  # past ~200 it learns the text by heart
    a = ap.parse_args()
    torch.manual_seed(0)
    ckpt, tokp, lives, out, extra = a.checkpoint, a.tokenizer, a.lives, a.out, a.thoughts
    cfg, w = read_checkpoint(ckpt)
    w = {k: v.copy() for k, v in w.items() if k != "wcls"}
    tok = Tok(tokp)
    docs = corpus(lives, extra)
    stream = []
    for d in docs:  # line by line: the merge loop is quadratic in the text's length
        stream.append(1)
        for line in d.split("\n")[:-1]:
            stream += [*tok.encode(line), 13]
    data = torch.tensor(stream)
    print(f"{len(docs)} docs, {len(stream)} tokens", flush=True)
    model = Llama(cfg, w)
    opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=0.01)
    T, B = 256, 32
    steps = a.steps
    t0 = time.time()
    for step in range(steps):
        lr = (
            1e-3
            * min(1, (step + 1) / 50)
            * (0.1 + 0.9 * (1 + math.cos(math.pi * step / steps)) / 2)
        )
        for g in opt.param_groups:
            g["lr"] = lr
        ix = torch.randint(0, len(data) - T - 1, (B,))
        xb = torch.stack([data[i : i + T] for i in ix])
        yb = torch.stack([data[i + 1 : i + T + 1] for i in ix])
        loss = F.cross_entropy(model(xb).view(-1, cfg[5]), yb.view(-1))
        opt.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        if step % 100 == 0:
            print(f"step {step} loss {loss.item():.3f} {time.time() - t0:.0f}s", flush=True)
    # llama2.c legacy layout, shared classifier, with the rotary tables it expects
    dim, _hidden, _layers, heads, _kv_heads, _vocab, seq = cfg
    p = {k: v.detach().numpy().astype("<f4") for k, v in model.p.items()}
    hs = dim // heads
    freq = 1.0 / 10000 ** (np.arange(0, hs, 2) / hs)
    ang = np.outer(np.arange(seq), freq)
    parts = [
        p[k]
        for k in (
            "emb",
            "rms_att",
            "wq",
            "wk",
            "wv",
            "wo",
            "rms_ffn",
            "w1",
            "w2",
            "w3",
            "rms_final",
        )
    ]
    parts += [np.cos(ang).astype("<f4"), np.sin(ang).astype("<f4")]
    out.write_bytes(struct.pack("7i", *cfg) + b"".join(a.tobytes() for a in parts))
    print("wrote", out)


if __name__ == "__main__":
    main()
