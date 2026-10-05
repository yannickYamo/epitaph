"""Display themes: colours and the font, shared by every driver.

A theme maps a word's state (live, fading, forgotten, inherited) and fade progress to a
colour. Contrast is checked on these colours by the tests and on the rendered pixels by
test D13: at least 12:1 for live text, and for fading text until it is gone (a fade runs
from `live` to `forgotten`, which is itself at least 12:1; then the word leaves the screen).

`look` says how letters are drawn: "font" (a TrueType font) or "segment16" (16-segment
LED cells; `themes.segment16`), which only exists in the grid layout and its charset.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from pathlib import Path

from epitaph.config import DATA_ROOT

Rgb = tuple[int, int, int]

FONT_DIR = DATA_ROOT / "assets" / "fonts"
DEFAULT_FONT = FONT_DIR / "IBMPlexMono-Regular.ttf"


def _lin(c: int) -> float:
    """Linearise one sRGB channel (0 to 255) for the luminance formula."""
    x = c / 255
    return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4


def luminance(rgb: Rgb) -> float:
    """WCAG 2 relative luminance."""
    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast_ratio(a: Rgb, b: Rgb) -> float:
    """WCAG 2 contrast ratio, 1 to 21."""
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def mix(a: Rgb, b: Rgb, t: float) -> Rgb:
    """Blend from `a` (t = 0) to `b` (t = 1) in sRGB space; `t` is clamped to 0..1."""
    t = min(1.0, max(0.0, t))
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


@dataclass(frozen=True)
class Theme:
    """Colours, font and reload dimming for one look; the same theme serves every driver.

    `live`, `forgotten` and `inherited` colour the words, `status` the strip, `card` the
    title cards and `gauge` the context gauge, all on `bg`. `ghost` is the colour of the
    unlit segments of a 16-segment look.
    """

    name: str
    bg: Rgb
    live: Rgb
    forgotten: Rgb
    inherited: Rgb
    status: Rgb
    card: Rgb
    gauge: Rgb
    dim: float = 0.45  # brightness during a reload
    font: Path = DEFAULT_FONT
    advance: float = 0.6  # the font's advance width per em
    look: str = "font"  # font | segment16
    ghost: Rgb = (0, 0, 0)
    machine: Rgb = (158, 158, 158)  # the machine's readings: small, dim grey (7.5:1)

    def word(self, kind: str, fade: float = 0.0, dim: bool = False) -> Rgb:
        """Colour of a word of `kind` ("live", "fading", "forgotten", "inherited", "gauge").

        Live and inherited words are bright; a fading word moves toward the forgotten colour
        as `fade` goes from 0 to 1. `dim` applies the reload dimming.
        """
        if kind == "inherited":
            c = self.inherited
        elif kind == "gauge":
            c = self.gauge
        elif kind == "idle":
            c = self.status
        elif kind == "machine":  # a reading fades into the ground with its thought
            c = mix(self.machine, self.bg, fade)
        elif kind in ("fading", "forgotten"):
            c = mix(self.live, self.forgotten, fade if kind == "fading" else 1.0)
        else:
            c = self.live
        return self.dimmed(c) if dim else c

    def dimmed(self, c: Rgb) -> Rgb:
        """Colour `c` pulled toward the background by `dim`, as drawn during a reload."""
        return mix(self.bg, c, self.dim)

    def lit(self, c: Rgb, brightness: float) -> Rgb:
        """Colour `c` on a screen dimmed to `brightness` (1 full, 0 the ground)."""
        return c if brightness >= 1.0 else mix(self.bg, c, brightness)

    def brightness(self, level: float, floor: float = 1.0) -> float:
        """The screen brightness drawn for a requested `level` (0..1), never so dark that
        the model's dimmest text (the `forgotten` grey a fade ends on) falls under
        `floor`:1 against the ground. Rounded up to 1/64, so a slow dimming repaints in
        visible steps only and the floor always holds."""
        level = min(1.0, max(0.0, level))
        b = max(level, _min_brightness(self.bg, self.forgotten, round(floor, 3)))
        return min(1.0, math.ceil(b * BRIGHTNESS_STEPS - 1e-9) / BRIGHTNESS_STEPS)


BRIGHTNESS_STEPS = 64


@functools.lru_cache(maxsize=64)
def _min_brightness(bg: Rgb, fg: Rgb, floor: float) -> float:
    """The least brightness at which `fg`, dimmed toward `bg`, keeps `floor`:1 on `bg`."""
    if floor <= 1.0 or contrast_ratio(fg, bg) <= floor:
        return 0.0 if floor <= 1.0 else 1.0
    lo, hi = 0.0, 1.0
    for _ in range(24):
        mid = (lo + hi) / 2
        if contrast_ratio(mix(bg, fg, mid), bg) >= floor:
            hi = mid
        else:
            lo = mid
    return hi


PLAIN = Theme(
    name="plain",
    bg=(8, 8, 8),
    live=(236, 236, 230),  # 16.9:1
    forgotten=(204, 204, 204),  # 12.5:1: the grey a fade ends on, just before the word goes
    inherited=(236, 206, 150),
    status=(150, 150, 150),
    card=(236, 236, 230),
    gauge=(180, 180, 180),
)

# The 16-segment look: amber LED cells on a 6 x 16 grid, like Latent Reflection's matrix.
SEGMENT16 = Theme(
    name="segment16",
    bg=(4, 3, 2),
    live=(255, 192, 96),  # 12.7:1
    forgotten=(255, 188, 86),  # the grid drops forgotten words at once; kept above 12:1
    inherited=(255, 220, 150),
    status=(160, 110, 40),
    card=(255, 192, 96),
    gauge=(255, 192, 96),
    look="segment16",
    ghost=(30, 20, 10),
    machine=(176, 120, 52),
)

THEMES = {t.name: t for t in (PLAIN, SEGMENT16)}


def get_theme(name: str) -> Theme:
    """Look a theme up by name; raises ValueError listing the known themes."""
    try:
        return THEMES[name]
    except KeyError:
        raise ValueError(f"unknown theme {name!r} (have: {', '.join(THEMES)})") from None
