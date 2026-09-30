"""Display themes: colours and the font, shared by every driver (BUILD_PLAN 5.12).

A theme maps a word's state (live, fading, forgotten, inherited) and fade progress to a
colour. Contrast is checked on these colours by the tests and on the rendered pixels by
test D13 (at least 12:1 for live text).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

Rgb = tuple[int, int, int]

FONT_DIR = Path(__file__).resolve().parents[4] / "assets" / "fonts"
DEFAULT_FONT = FONT_DIR / "IBMPlexMono-Regular.ttf"


def _lin(c: int) -> float:
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
    t = min(1.0, max(0.0, t))
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


@dataclass(frozen=True)
class Theme:
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

    def word(self, kind: str, fade: float = 0.0, dim: bool = False) -> Rgb:
        """Colour of a word: live and inherited are bright; fading moves to forgotten grey."""
        if kind == "inherited":
            c = self.inherited
        elif kind == "gauge":
            c = self.gauge
        elif kind in ("fading", "forgotten"):
            c = mix(self.live, self.forgotten, fade if kind == "fading" else 1.0)
        else:
            c = self.live
        return self.dimmed(c) if dim else c

    def dimmed(self, c: Rgb) -> Rgb:
        return mix(self.bg, c, self.dim)


PLAIN = Theme(
    name="plain",
    bg=(8, 8, 8),
    live=(236, 236, 230),
    forgotten=(122, 122, 122),
    inherited=(236, 206, 150),
    status=(150, 150, 150),
    card=(236, 236, 230),
    gauge=(180, 180, 180),
)

# The 16-segment look (amber LEDs); the full segment renderer comes in P2 (D7).
SEGMENT16 = Theme(
    name="segment16",
    bg=(6, 4, 2),
    live=(255, 176, 64),
    forgotten=(120, 80, 30),
    inherited=(255, 220, 150),
    status=(160, 110, 40),
    card=(255, 176, 64),
    gauge=(255, 176, 64),
)

THEMES = {t.name: t for t in (PLAIN, SEGMENT16)}


def get_theme(name: str) -> Theme:
    try:
        return THEMES[name]
    except KeyError:
        raise ValueError(f"unknown theme {name!r} (have: {', '.join(THEMES)})") from None
