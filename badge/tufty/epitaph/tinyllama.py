# A llama2.c-style transformer for the badge: ulab on the badge, numpy on a laptop (tests).
#
# The model file is written by badge/tools/convert_tinyllama.py: float32 weights, read in
# place (no copy), with the query and key rows split into even and odd halves so the rotary
# embedding is two vector operations instead of a loop.

import math
import struct

try:
    from ulab import numpy as np

    FLOAT = np.float
except ImportError:  # a laptop
    import numpy as np

    FLOAT = np.float32

BOS = 1


class Model:
    def __init__(self, path_or_bytes):
        if isinstance(path_or_bytes, (bytes, bytearray)):
            raw = path_or_bytes
        else:
            with open(path_or_bytes, "rb") as f:
                raw = f.read()
        if raw[:4] != b"EPT1":
            raise ValueError("not a badge model file")
        dim, hidden, layers, heads, kv_heads, vocab, seq, unshared = struct.unpack("8i", raw[4:36])
        self.dim, self.hidden, self.layers = dim, hidden, layers
        self.heads, self.kv_heads, self.vocab, self.seq = heads, kv_heads, vocab, seq
        self.head = dim // heads
        self.kv_dim = dim * kv_heads // heads
        self._raw = raw  # the arrays below are views into it
        self._at = 36

        self.emb = self._take(vocab, dim)
        self.L = []
        half, kv_half = dim // 2, self.kv_dim // 2
        for _ in range(layers):
            self.L.append(
                (
                    self._take(dim),  # 0 rms_att
                    self._take(half, dim),  # 1 wq even rows
                    self._take(half, dim),  # 2 wq odd rows
                    self._take(kv_half, dim),  # 3 wk even rows
                    self._take(kv_half, dim),  # 4 wk odd rows
                    self._take(self.kv_dim, dim),  # 5 wv
                    self._take(dim, dim),  # 6 wo
                    self._take(dim),  # 7 rms_ffn
                    self._take(hidden, dim),  # 8 w1
                    self._take(dim, hidden),  # 9 w2
                    self._take(hidden, dim),  # 10 w3
                )
            )
        self.rms_final = self._take(dim)
        self.wcls = self._take(vocab, dim) if unshared else self.emb
        if self._at != len(raw):
            raise ValueError("model file size does not match its header")

        # rotary frequencies for the even/odd halves: pair j sits at dimension 2j of its head
        hs = self.head
        self._freq = np.array(
            [1.0 / math.pow(10000.0, ((2 * j) % hs) / hs) for j in range(half)], dtype=FLOAT
        )
        self.reset()

    def _take(self, *shape):
        n = 1
        for s in shape:
            n *= s
        a = np.frombuffer(self._raw, dtype=FLOAT, count=n, offset=self._at)
        self._at += 4 * n
        return a.reshape(shape) if len(shape) > 1 else a

    def reset(self):
        """Forget everything: a new story starts at position 0. The caches are allocated once
        and reused (a position is always written before it is read): reallocating them
        fragments the badge's heap until an allocation fails."""
        if not hasattr(self, "kE"):
            kv_half = self.kv_dim // 2
            self.kE = [np.zeros((self.seq, kv_half), dtype=FLOAT) for _ in range(self.layers)]
            self.kO = [np.zeros((self.seq, kv_half), dtype=FLOAT) for _ in range(self.layers)]
            self.v = [np.zeros((self.seq, self.kv_dim), dtype=FLOAT) for _ in range(self.layers)]
        self.pos = 0

    def _rmsnorm(self, x, w):
        return x * w * (1.0 / math.sqrt(float(np.sum(x * x)) / self.dim + 1e-5))

    def forward(self, token, window=0):
        """Logits for the next token. `window` > 0 lets attention see only the last `window`
        positions, this one included: the memory it can still hold."""
        pos = self.pos
        x = self.emb[token] * 1.0
        hs, half_hs = self.head, self.head // 2
        kv_mul = self.heads // self.kv_heads
        lo = 0 if window <= 0 else max(0, pos - window + 1)
        ang = self._freq * pos
        c, s = np.cos(ang), np.sin(ang)
        kv_half = self.kv_dim // 2
        ck, sk = c[:kv_half], s[:kv_half]
        scale = 1.0 / math.sqrt(hs)
        for li in range(self.layers):
            w = self.L[li]
            xb = self._rmsnorm(x, w[0])
            qe, qo = np.dot(w[1], xb), np.dot(w[2], xb)
            ke, ko = np.dot(w[3], xb), np.dot(w[4], xb)
            qe, qo = qe * c - qo * s, qe * s + qo * c
            self.kE[li][pos] = ke * ck - ko * sk
            self.kO[li][pos] = ke * sk + ko * ck
            self.v[li][pos] = np.dot(w[5], xb)
            KE, KO, V = (
                self.kE[li][lo : pos + 1],
                self.kO[li][lo : pos + 1],
                self.v[li][lo : pos + 1],
            )
            outs = []
            for h in range(self.heads):
                kh = h // kv_mul
                if pos == lo:  # one position: its own value (ulab returns a scalar here)
                    outs.append(V[0, kh * hs : (kh + 1) * hs])
                    continue
                a, b = h * half_hs, (h + 1) * half_hs
                ka, kb = kh * half_hs, (kh + 1) * half_hs
                sc = (np.dot(KE[:, ka:kb], qe[a:b]) + np.dot(KO[:, ka:kb], qo[a:b])) * scale
                sc = np.exp(sc - np.max(sc))
                sc = sc / np.sum(sc)
                outs.append(np.dot(V[:, kh * hs : (kh + 1) * hs].transpose(), sc))
            x = x + np.dot(w[6], np.concatenate(tuple(outs)))
            xb = self._rmsnorm(x, w[7])
            h1, h3 = np.dot(w[8], xb), np.dot(w[10], xb)
            x = x + np.dot(w[9], h1 / (1.0 + np.exp(-h1)) * h3)
        x = self._rmsnorm(x, self.rms_final)
        self.pos = pos + 1
        return np.dot(self.wcls, x)


def sample(logits, temperature, rand):
    """A token from the logits; `rand` is a float in [0, 1)."""
    if temperature <= 0:
        return int(np.argmax(logits))
    p = np.exp((logits - np.max(logits)) / temperature)
    total = float(np.sum(p))
    r = rand * total
    acc = 0.0
    n = len(p)
    for i in range(n):
        acc += float(p[i])
        if acc > r:
            return i
    return n - 1
