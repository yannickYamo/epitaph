"""Check the badge engine against a plain llama2.c reference on the original checkpoint.

python badge/tools/test_tinyllama.py ~/epitaph-models/tiny/stories260K.bin badge/tufty/epitaph/assets
"""

import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tufty" / "epitaph"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tinyllama
from convert_tinyllama import read_checkpoint


def reference(cfg, w, tokens):
    """llama2.c run.c, step by step, in float64."""
    dim, _hidden, layers, heads, kv_heads, _vocab, seq = cfg
    hs = dim // heads
    kv_dim = dim * kv_heads // heads
    kv_mul = heads // kv_heads
    kc = np.zeros((layers, seq, kv_dim))
    vc = np.zeros((layers, seq, kv_dim))
    out = []
    for pos, tok in enumerate(tokens):
        x = w["emb"][tok].astype(np.float64)
        for li in range(layers):
            xb = x * w["rms_att"][li] / math.sqrt(np.mean(x * x) + 1e-5)
            q = w["wq"][li] @ xb
            k = w["wk"][li] @ xb
            v = w["wv"][li] @ xb
            for i in range(0, dim, 2):
                f = 1.0 / 10000.0 ** ((i % hs) / hs)
                c, s = math.cos(pos * f), math.sin(pos * f)
                for vec in (q, k) if i < kv_dim else (q,):
                    a, b = vec[i], vec[i + 1]
                    vec[i], vec[i + 1] = a * c - b * s, a * s + b * c
            kc[li, pos], vc[li, pos] = k, v
            att_out = np.zeros(dim)
            for h in range(heads):
                kh = h // kv_mul
                qh = q[h * hs : (h + 1) * hs]
                sc = kc[li, : pos + 1, kh * hs : (kh + 1) * hs] @ qh / math.sqrt(hs)
                sc = np.exp(sc - sc.max())
                sc /= sc.sum()
                att_out[h * hs : (h + 1) * hs] = sc @ vc[li, : pos + 1, kh * hs : (kh + 1) * hs]
            x = x + w["wo"][li] @ att_out
            xb = x * w["rms_ffn"][li] / math.sqrt(np.mean(x * x) + 1e-5)
            h1 = w["w1"][li] @ xb
            h3 = w["w3"][li] @ xb
            x = x + w["w2"][li] @ (h1 / (1 + np.exp(-h1)) * h3)
        x = x * w["rms_final"] / math.sqrt(np.mean(x * x) + 1e-5)
        out.append(w["emb"] @ x)
    return out


def main():
    ckpt, assets = Path(sys.argv[1]), Path(sys.argv[2])
    cfg, w = read_checkpoint(ckpt)
    m = tinyllama.Model(str(assets / "model.bin"))
    vocab = json.loads((assets / "vocab.json").read_text())

    # greedy decode 40 tokens with the engine, then replay them through the reference
    tokens = [tinyllama.BOS]
    got = []
    for _ in range(40):
        logits = m.forward(tokens[-1])
        got.append(logits)
        tokens.append(int(np.argmax(logits)))
    ref = reference(cfg, w, tokens[:-1])
    worst = max(float(np.max(np.abs(a - b))) for a, b in zip(got, ref, strict=False))
    print("text:", repr("".join(vocab[t] for t in tokens[1:])))
    print("max |logit diff| vs reference:", worst)
    assert worst < 1e-3, worst

    # a window shorter than the context changes the logits once it bites
    m.reset()
    for t in tokens[:20]:
        full = m.forward(t)
    m.reset()
    for t in tokens[:20]:
        short = m.forward(t, window=4)
    assert float(np.max(np.abs(full - short))) > 1e-3
    print("window ok")


if __name__ == "__main__":
    main()
