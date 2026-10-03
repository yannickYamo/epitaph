"""Convert a llama2.c checkpoint and tokenizer into the badge's model file.

The badge file is the same float32 weights in an order the engine reads without copying:
the query and key rows split into their even and odd halves (rotary pairs side by side), and
no precomputed rotary tables (the engine computes them).

    python badge/tools/convert_tinyllama.py stories260K.bin tok512.bin badge/tufty/epitaph/assets
"""

import json
import struct
import sys
from pathlib import Path

import numpy as np

MAGIC = b"EPT1"


def read_checkpoint(path: Path) -> tuple[tuple[int, ...], dict[str, np.ndarray]]:
    raw = path.read_bytes()
    dim, hidden, layers, heads, kv_heads, vocab, seq = struct.unpack("7i", raw[:28])
    shared = vocab > 0
    vocab = abs(vocab)
    head = dim // heads
    kv_dim = dim * kv_heads // heads
    w = np.frombuffer(raw, dtype="<f4", offset=28)
    out: dict[str, np.ndarray] = {}
    at = 0

    def take(name: str, *shape: int) -> None:
        nonlocal at
        n = int(np.prod(shape))
        out[name] = w[at : at + n].reshape(shape)
        at += n

    take("emb", vocab, dim)
    take("rms_att", layers, dim)
    take("wq", layers, dim, dim)
    take("wk", layers, kv_dim, dim)
    take("wv", layers, kv_dim, dim)
    take("wo", layers, dim, dim)
    take("rms_ffn", layers, dim)
    take("w1", layers, hidden, dim)
    take("w2", layers, dim, hidden)
    take("w3", layers, hidden, dim)
    take("rms_final", dim)
    at += seq * head // 2 * 2  # the legacy rotary tables
    if not shared:
        take("wcls", vocab, dim)
    if at != w.size:
        raise ValueError(f"{path}: {w.size - at} floats left over")
    return (dim, hidden, layers, heads, kv_heads, vocab, seq), out


def write_model(cfg: tuple[int, ...], w: dict[str, np.ndarray], path: Path) -> None:
    dim, hidden, layers, heads, kv_heads, vocab, seq = cfg
    parts = [w["emb"]]
    for i in range(layers):
        parts += [
            w["rms_att"][i],
            w["wq"][i][0::2],
            w["wq"][i][1::2],
            w["wk"][i][0::2],
            w["wk"][i][1::2],
            w["wv"][i],
            w["wo"][i],
            w["rms_ffn"][i],
            w["w1"][i],
            w["w2"][i],
            w["w3"][i],
        ]
    parts.append(w["rms_final"])
    if "wcls" in w:
        parts.append(w["wcls"])
    body = b"".join(np.ascontiguousarray(p, dtype="<f4").tobytes() for p in parts)
    head = MAGIC + struct.pack(
        "8i", dim, hidden, layers, heads, kv_heads, vocab, seq, int("wcls" in w)
    )
    path.write_bytes(head + body)


def write_vocab(tok: Path, vocab: int, path: Path) -> None:
    """The pieces as a JSON list. Byte tokens <0xNN> become their character when printable
    ASCII or a newline, and "" otherwise (the badge's fonts are ASCII)."""
    raw = tok.read_bytes()
    at = 4
    pieces, scores = [], []
    for _ in range(vocab):
        score, n = struct.unpack("fi", raw[at : at + 8])
        scores.append(round(score, 3))
        at += 8
        piece = raw[at : at + n].decode("utf-8", "replace")
        at += n
        if len(piece) == 6 and piece.startswith("<0x") and piece.endswith(">"):
            b = int(piece[3:5], 16)
            piece = chr(b) if b == 10 or 32 <= b < 127 else ""
        pieces.append(piece)
    path.write_text(json.dumps(pieces), encoding="utf-8")
    path.with_name("scores.json").write_text(json.dumps(scores), encoding="utf-8")


def main() -> None:
    ckpt, tok, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    out.mkdir(parents=True, exist_ok=True)
    cfg, w = read_checkpoint(ckpt)
    write_model(cfg, w, out / "model.bin")
    write_vocab(tok, cfg[5], out / "vocab.json")
    print("config", cfg, "->", out)


if __name__ == "__main__":
    main()
