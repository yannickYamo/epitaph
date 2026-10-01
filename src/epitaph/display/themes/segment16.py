"""The 16-segment theme: amber LED cells on a grid, like Latent Reflection's 6 x 16 matrix.

Every cell is a 16-segment character with a decimal point. Unlit segments stay faintly
visible (`Theme.ghost`), as on real LED modules, so an empty grid still reads as a panel.
The charset is what 16 segments can draw: upper-case letters, digits and ASCII punctuation
(`layout.SEGMENT16_OK`); `layout.map_charset(text, "segment16")` maps everything else.

Segments, in a unit cell (x to the right, y down):

    a1  a2          top, left and right halves
    f h i j b       left, top-left diagonal, top centre, top-right diagonal, right
    g1  g2          middle halves
    e k l m c       left, bottom-left diagonal, bottom centre, bottom-right diagonal, right
    d1  d2  dp      bottom halves, decimal point

This module is pure Python: geometry, the glyph table and a decoder that reads characters
back from pixels (test D13 reads segment cells this way instead of with OCR). `screen.py`
draws the polygons with pygame.
"""

from __future__ import annotations

from collections.abc import Callable

from epitaph.display.themes import SEGMENT16 as THEME
from epitaph.display.themes import Rgb, contrast_ratio

__all__ = [
    "FONT",
    "SEGMENTS",
    "THEME",
    "cursor_mask",
    "decode",
    "glyph_mask",
    "polygons",
    "sample_points",
]

SEGMENTS = ("a1", "a2", "b", "c", "d1", "d2", "e", "f", "g1", "g2", "h", "i", "j", "k", "l", "m")
SEG_DP = "dp"

_P = {
    "TL": (0.0, 0.0),
    "TM": (0.5, 0.0),
    "TR": (1.0, 0.0),
    "ML": (0.0, 0.5),
    "MM": (0.5, 0.5),
    "MR": (1.0, 0.5),
    "BL": (0.0, 1.0),
    "BM": (0.5, 1.0),
    "BR": (1.0, 1.0),
}
_ENDS = {
    "a1": ("TL", "TM"),
    "a2": ("TM", "TR"),
    "b": ("TR", "MR"),
    "c": ("MR", "BR"),
    "d1": ("BL", "BM"),
    "d2": ("BM", "BR"),
    "e": ("ML", "BL"),
    "f": ("TL", "ML"),
    "g1": ("ML", "MM"),
    "g2": ("MM", "MR"),
    "h": ("TL", "MM"),
    "i": ("TM", "MM"),
    "j": ("TR", "MM"),
    "k": ("BL", "MM"),
    "l": ("BM", "MM"),
    "m": ("BR", "MM"),
}

_OUTER = "a1 a2 b c d1 d2 e f"
# Each character as the segments it lights (a classic 16-segment alphabet).
_TABLE = {
    " ": "",
    "0": _OUTER + " j k",
    "1": "b c j",
    "2": "a1 a2 b g1 g2 e d1 d2",
    "3": "a1 a2 b c d1 d2 g2",
    "4": "f g1 g2 b c",
    "5": "a1 a2 f g1 g2 c d1 d2",
    "6": "a1 a2 f e d1 d2 c g1 g2",
    "7": "a1 a2 b c",
    "8": _OUTER + " g1 g2",
    "9": "a1 a2 b c d1 d2 f g1 g2",
    "A": "a1 a2 b c e f g1 g2",
    "B": "a1 a2 b c d1 d2 i l g2",
    "C": "a1 a2 f e d1 d2",
    "D": "a1 a2 b c d1 d2 i l",
    "E": "a1 a2 f e d1 d2 g1",
    "F": "a1 a2 f e g1",
    "G": "a1 a2 f e d1 d2 c g2",
    "H": "f e b c g1 g2",
    "I": "a1 a2 i l d1 d2",
    "J": "b c d1 d2 e",
    "K": "f e g1 j m",
    "L": "f e d1 d2",
    "M": "f e h j b c",
    "N": "f e h m c b",
    "O": _OUTER,
    "P": "a1 a2 b f e g1 g2",
    "Q": _OUTER + " m",
    "R": "a1 a2 b f e g1 g2 m",
    "S": "a1 a2 h g2 c d1 d2",
    "T": "a1 a2 i l",
    "U": "f e d1 d2 c b",
    "V": "f e k j",
    "W": "f e k m c b",
    "X": "h j k m",
    "Y": "h j l",
    "Z": "a1 a2 j k d1 d2",
    "!": "i dp",
    '"': "f i",
    "#": "b c i l g1 g2 d1 d2",
    "$": "a1 a2 f g1 g2 c d1 d2 i l",
    "%": "a1 f i g1 g2 c d2 l j k",
    "&": "a1 h i g1 e d1 d2 m",
    "'": "i",
    "(": "j m",
    ")": "h k",
    "*": "g1 g2 h i j k l m",
    "+": "g1 g2 i l",
    ",": "k",
    "-": "g1 g2",
    ".": "dp",
    "/": "j k",
    ":": "i l",
    ";": "i k",
    "<": "j m",
    "=": "g1 g2 d1 d2",
    ">": "h k",
    "?": "a1 a2 b g2 l dp",
    "@": "a1 a2 b e f d1 d2 g2 i",
    "[": "a1 f e d1",
    "\\": "h m",
    "]": "a2 b c d2",
    "^": "k m",
    "_": "d1 d2",
    "`": "h",
    "{": "a2 i g1 l d2",
    "|": "i l",
    "}": "a1 i g2 l d1",
    "~": "g1 j",
}
FONT: dict[str, frozenset[str]] = {ch: frozenset(v.split()) for ch, v in _TABLE.items()}
_UNKNOWN = FONT["?"]


def glyph_mask(ch: str) -> frozenset[str]:
    """The segments lit for `ch` (upper-cased; unknown characters draw as "?")."""
    return FONT.get(ch.upper(), _UNKNOWN)


def cursor_mask() -> frozenset[str]:
    """The cursor: the bottom bar of a cell, an underscore."""
    return FONT["_"]


def _box(w: float, h: float) -> tuple[float, float, float, float, float]:
    """The glyph box inside a `w` x `h` cell: left, top, width, height, stroke width."""
    bw, bh = w * 0.62, h * 0.74
    left, top = (w - bw) / 2 - w * 0.04, (h - bh) / 2
    stroke = max(2.0, min(bw, bh) * 0.11)
    return left, top, bw, bh, stroke


def polygons(w: float, h: float) -> dict[str, list[tuple[float, float]]]:
    """Each segment (and the decimal point) as a polygon in a `w` x `h` pixel cell."""
    left, top, bw, bh, t = _box(w, h)
    out: dict[str, list[tuple[float, float]]] = {}
    gap = t * 0.75
    for name, (p, q) in _ENDS.items():
        x0, y0 = left + _P[p][0] * bw, top + _P[p][1] * bh
        x1, y1 = left + _P[q][0] * bw, top + _P[q][1] * bh
        dx, dy = x1 - x0, y1 - y0
        n = (dx * dx + dy * dy) ** 0.5 or 1.0
        ux, uy = dx / n, dy / n
        x0, y0, x1, y1 = x0 + ux * gap, y0 + uy * gap, x1 - ux * gap, y1 - uy * gap
        nx, ny = -uy * t / 2, ux * t / 2
        out[name] = [(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)]
    cx, cy = left + bw + (w - left - bw) * 0.45, top + bh
    out[SEG_DP] = [(cx - t / 2, cy - t), (cx + t / 2, cy - t), (cx + t / 2, cy), (cx - t / 2, cy)]
    return out


def sample_points(w: float, h: float) -> dict[str, tuple[int, int]]:
    """A pixel inside each segment of a `w` x `h` cell, where `decode` reads it."""
    out: dict[str, tuple[int, int]] = {}
    for name, poly in polygons(w, h).items():
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        out[name] = (round(sum(xs) / len(xs)), round(sum(ys) / len(ys)))
    return out


_REVERSE: dict[frozenset[str], str] = {}
for _ch, _mask in FONT.items():
    _REVERSE.setdefault(_mask, _ch)  # the first character wins for shared masks ("|", ":")


def decode(
    pixel: Callable[[int, int], Rgb],
    x0: float,
    y0: float,
    w: float,
    h: float,
    bg: Rgb,
) -> str:
    """Read the character drawn in the cell at (`x0`, `y0`) of size `w` x `h`.

    `pixel(x, y)` returns a colour. A segment is lit when its sample pixel stands out
    from the background by at least 3:1 (ghost segments stay well below). Returns the
    character whose segments match, "\\0" for a pattern no character has.
    """
    lit = frozenset(
        name
        for name, (sx, sy) in sample_points(w, h).items()
        if contrast_ratio(pixel(round(x0) + sx, round(y0) + sy), bg) >= 3.0
    )
    return _REVERSE.get(lit, "\0")
