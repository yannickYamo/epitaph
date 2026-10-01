"""D7: the 16-segment theme (a 6 x 16 LED grid like Latent Reflection's)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from epitaph.display.layout import SEGMENT16_OK, LifeView, ViewSettings, map_charset
from epitaph.display.themes import SEGMENT16, contrast_ratio, get_theme
from epitaph.display.themes import segment16 as seg

from .test_layout import born, ev, thought

os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")


def test_every_drawable_character_has_a_glyph() -> None:
    assert set(SEGMENT16_OK) <= set(seg.FONT)
    letters = [seg.glyph_mask(c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"]
    assert len(set(letters)) == len(letters)  # no two letters or digits look alike
    assert all(m <= set(seg.SEGMENTS) | {"dp"} for m in seg.FONT.values())
    assert seg.glyph_mask("a") == seg.glyph_mask("A")
    assert seg.glyph_mask("é") == seg.glyph_mask("?")
    assert seg.glyph_mask(" ") == frozenset()
    assert seg.cursor_mask() == seg.glyph_mask("_")


def test_the_charset_is_limited_to_what_segments_draw() -> None:
    text = map_charset("Naïve “words” — 52°C…", "segment16")
    assert text == 'NAIVE "WORDS" - 52*C...'
    assert set(text) <= SEGMENT16_OK


def test_segments_stay_inside_their_cell() -> None:
    for w, h in [(48.0, 74.0), (63.5, 115.0), (20.0, 30.0)]:
        polys = seg.polygons(w, h)
        assert set(polys) == set(seg.SEGMENTS) | {"dp"}
        for poly in polys.values():
            assert all(0 <= x <= w and 0 <= y <= h for x, y in poly)
        points = seg.sample_points(w, h)
        assert len(set(points.values())) == len(points)  # one pixel per segment


def test_theme_contrast() -> None:
    t = get_theme("segment16")
    assert t is SEGMENT16 and t.look == "segment16"
    assert contrast_ratio(t.live, t.bg) >= 12
    assert contrast_ratio(t.card, t.bg) >= 12
    assert contrast_ratio(t.forgotten, t.bg) >= 12
    assert contrast_ratio(t.ghost, t.bg) < 3  # unlit segments never read as lit
    assert seg.THEME is SEGMENT16


def test_every_glyph_is_read_back_from_its_pixels(tmp_path: Path) -> None:
    pygame = pytest.importorskip("pygame")
    from epitaph.display.screen import ScreenDriver

    d = ScreenDriver(theme=SEGMENT16, size=(800, 480))
    d.open()
    try:
        w, h = 47.5, 73.25
        chars = sorted(seg.FONT)
        for ch in chars:
            cell = d._segment_cell(seg.glyph_mask(ch), SEGMENT16.live, w, h)  # pyright: ignore[reportPrivateUsage]

            def pixel(x: int, y: int, cell: object = cell) -> tuple[int, int, int]:
                c = cell.get_at((x, y))  # type: ignore[attr-defined]
                return (c.r, c.g, c.b)

            got = seg.decode(pixel, 0, 0, w, h, SEGMENT16.bg)
            assert seg.glyph_mask(got) == seg.glyph_mask(ch), ch
        assert pygame.display.get_init()
    finally:
        d.close()


def test_the_screen_draws_the_grid_in_segments(tmp_path: Path) -> None:
    pytest.importorskip("pygame")
    from epitaph.display import screenshot as shot

    v = LifeView(ViewSettings(birth_card=False))
    born(v)
    v.handle(ev("vitals", recall=100, recall_used=25), 0.0)
    thought(v, 1, "Is anyone there? 52°C, 1.4 tok/s", 0.0)
    r = shot.readability_segments((1280, 720), tmp_path, v, 10.0, "unit")
    assert (r["rows"], r["cols"]) == (6, 16)
    assert r["cell_accuracy"] == 1.0, (r["read"], r["want"])
    assert "IS ANYONE" in "\n".join(r["want"])
    assert r["want"][-1] == "----" + " " * 12  # the memory gauge: lit dashes
    assert r["contrast"] >= 12
