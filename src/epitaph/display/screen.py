"""The pixel driver (pygame): a window on the laptop, a full screen on the Pi (KMSDRM),
or offscreen for screenshots and tests (`SDL_VIDEODRIVER=offscreen`) (BUILD_PLAN 9 D4).

It draws the same `Frame` as the terminal: letters typed one by one with each word's
cadence, a block cursor (solid while typing, blinking in pauses, dim in a reload, gone at
death), forgotten words fading through grey, the whole text dimmed during a reload, a
small status strip above the text, and the birth and death cards.

Only what changed is repainted (D7): each frame is reduced to the items of each text row
and the status strip, and only rows whose items differ are cleared, redrawn and sent to
the display. A letter typed costs one row, not the screen. `python -m
epitaph.display.bench` measures the CPU this takes.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from epitaph.display.layout import (
    Frame,
    LifeView,
    Metrics,
    ViewSettings,
    compose_flow,
    compose_grid,
    derive_grid,
    fit_status,
    flow_metrics,
)
from epitaph.display.themes import PLAIN, Rgb, Theme

DEFAULT_WINDOW = (1280, 720)

# A fade is drawn in this many colour steps: about four repaints a second over an 8 s fade
# instead of thirty, each step a few grey levels (too small to see as a step).
FADE_STEPS = 32

Rect = tuple[int, int, int, int]
RowItem = tuple[str, int, str, Rgb]  # kind (text, gauge, cursor), column, text, colour


@dataclass(frozen=True)
class _Geometry:
    """Pixel position of cell (0, 0), the cell size and the letter size on the surface."""

    left: float
    top: float
    cell_w: float
    line_h: float
    px: int


def _pygame() -> Any:
    """Import pygame lazily and without its banner, so the terminal path never needs it."""
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    import pygame

    return pygame


def headless() -> bool:
    """Whether SDL draws offscreen (screenshots, tests) rather than to a real display."""
    return os.environ.get("SDL_VIDEODRIVER", "") in ("offscreen", "dummy")


class ScreenDriver:
    """Draws a `LifeView` with pygame and implements the `Driver` protocol.

    `size=None` means the full screen on the console, or a 1280x720 window on a desktop
    session. `orientation="portrait"` on a landscape panel draws to a rotated surface.
    `min_font_px` is the smallest letter height allowed (BUILD_PLAN 5.12); `line_chars`
    and `grid` bound the flow and grid layouts in characters.
    """

    def __init__(
        self,
        settings: ViewSettings | None = None,
        theme: Theme = PLAIN,
        line_chars: int = 48,
        status_strip: bool = True,
        layout: str = "flow",
        grid: tuple[int, int] = (6, 16),
        charset: str = "unicode",
        min_font_px: int = 36,
        orientation: str = "landscape",
        size: tuple[int, int] | None = None,
        fullscreen: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure the driver; no window is opened until `open` (or the first draw)."""
        self.view = LifeView(settings)
        self.theme = theme
        self.line_chars = line_chars
        self.status_strip = status_strip
        self.layout = layout
        self.grid = grid
        self.charset = charset
        self.min_font_px = min_font_px
        self.orientation = orientation
        self.size = size
        self.fullscreen = fullscreen
        self.clock = clock
        self.closed = False
        self.pg: Any = None
        self.window: Any = None
        self.surface: Any = None
        self.rotate = 0
        self.metrics: Metrics | None = None
        self.last_frame: Frame | None = None
        self._cache: dict[tuple[int, str, Rgb], Any] = {}
        self._sig: tuple[Any, ...] | None = None
        self._rows: dict[int, tuple[RowItem, ...]] | None = None
        self._scene: tuple[Any, ...] | None = None
        self._status: str | None = None
        self.dirty: list[Rect] | None = None
        self._fonts: dict[int, Any] = {}
        self.font_path: Path | None = None

    # -- setup ------------------------------------------------------------------------------

    def open(self) -> None:
        """Open the window (or full screen) and fit the font; does nothing if already open."""
        if self.pg is not None:
            return
        pg = self.pg = _pygame()
        pg.display.init()
        pg.font.init()
        pg.display.set_caption("epitaph")
        flags = 0
        if self.fullscreen or (self.size is None and not headless() and _console()):
            flags |= pg.FULLSCREEN
            size = (0, 0)
        else:
            size = self.size or DEFAULT_WINDOW
            if not headless():
                flags |= pg.RESIZABLE
        self.window = pg.display.set_mode(size, flags)
        if not headless():
            pg.mouse.set_visible(False)
        self._setup(self.window.get_size())

    def _setup(self, physical: tuple[int, int]) -> None:
        """Size surfaces, caches and metrics for a window of `physical` pixels."""
        pg = self.pg
        w, h = physical
        self.rotate = 90 if self.orientation == "portrait" and w > h else 0
        logical = (h, w) if self.rotate else (w, h)
        self.surface = pg.Surface(logical) if self.rotate else self.window
        self._cache.clear()
        self._fonts.clear()
        self._sig = None
        self._rows = None
        self.font_path = _font_path(self.theme)
        m = flow_metrics(
            *logical,
            self.line_chars,
            self.min_font_px,
            self.theme.advance,
            status_strip=self.status_strip,
        )
        # measure the real advance of the loaded font and refit the columns to it
        adv = self.font(m.font_px).size("M")[0]
        usable = logical[0] - 2 * m.margin_x
        cols = max(1, min(self.line_chars, usable // max(1, adv)))
        self.metrics = Metrics(
            m.width,
            m.height,
            m.font_px,
            cols,
            m.rows,
            float(adv),
            m.line_h,
            m.margin_x,
            m.margin_y,
            m.strip_h,
        )

    def font(self, px: int) -> Any:
        """The theme's font at `px` pixels (at least 6), loaded once and cached."""
        px = max(6, int(px))
        f = self._fonts.get(px)
        if f is None:
            path = self.font_path
            f = self.pg.font.Font(str(path) if path else None, px)
            self._fonts[px] = f
        return f

    def close(self) -> None:
        """Shut the pygame display down; safe to call twice."""
        if self.pg is not None:
            self.pg.display.quit()
            self.pg = None

    # -- Display protocol -------------------------------------------------------------------

    def handle(self, event: dict[str, Any]) -> None:
        """Apply `event` to the view at the driver's clock."""
        self.view.handle(event, self.clock())

    def run(self, source: Any = None, fps: float = 30.0) -> None:
        """Block, drawing events from the async iterator `source` until done or closed."""
        import asyncio

        from epitaph.display.app import drive

        if source is None:
            raise ValueError("ScreenDriver.run needs an event source")
        asyncio.run(drive(self, source, fps=fps))

    def _ensure(self) -> Any:
        """The pygame module, opening the display first if needed."""
        if self.pg is None:
            self.open()
        return self.pg

    def screenshot(self, path: str) -> None:
        """Save the window as an image at `path` (format from the extension, usually PNG)."""
        pg = self._ensure()
        if self.last_frame is None:
            self.draw(self.clock(), force=True)
        pg.image.save(self.window, path)

    def render(self, now: float | None = None) -> None:
        """Process window events (quit on close, q or Esc; refit on resize), then draw.

        The display is flipped only when the frame changed, and only the changed
        rectangles are sent to it when the rest of the screen is unchanged.
        """
        pg = self._ensure()
        for ev in pg.event.get():
            if ev.type == pg.QUIT or (ev.type == pg.KEYDOWN and ev.key in (pg.K_q, pg.K_ESCAPE)):
                self.closed = True
            elif ev.type == pg.VIDEORESIZE and not self.rotate:
                self._setup(self.window.get_size())
        if self.draw(self.clock() if now is None else now):
            if self.dirty is None:
                pg.display.flip()
            elif self.dirty:
                pg.display.update(self.dirty)

    # -- drawing ----------------------------------------------------------------------------

    def _glyphs(self, px: int, text: str, colour: Rgb) -> Any:
        """Rendered text surface, cached by size, text and colour (cleared past 5000)."""
        key = (px, text, colour)
        surf = self._cache.get(key)
        if surf is None:
            if len(self._cache) > 5000:
                self._cache.clear()
            surf = self.font(px).render(text, True, colour, self.theme.bg)
            self._cache[key] = surf
        return surf

    def compose(self, now: float) -> Frame:
        """The frame for `now` (flow or grid), without drawing it."""
        assert self.metrics is not None
        m = self.metrics
        if self.layout == "grid":
            rows, cols, *_ = self._grid_geometry()
            return compose_grid(self.view, now, rows, cols, self.charset)
        return compose_flow(self.view, now, m.cols, m.rows, self.status_strip)

    def draw(self, now: float, force: bool = False) -> bool:
        """Paint the view at `now` onto the window; returns whether anything was painted.

        Returns False, painting nothing, when the frame equals the last one painted (unless
        `force`), so a still screen costs no drawing or flip. Otherwise only the text rows
        and the status strip that changed are repainted, and `dirty` lists their rectangles
        in window pixels; `dirty` is None after a full repaint (the first frame, a card, the
        dark screen, a reload dimming the text, or `force`). The caller flips or updates.
        """
        self._ensure()
        assert self.metrics is not None
        frame = self.compose(now)
        self.last_frame = frame
        g = self._geometry()
        rows = _rows(frame, self.theme)
        scene = (frame.dark, frame.card is not None, frame.dim)
        status = self._status_text(frame.status) if frame.status and not frame.dark else None
        card = (frame.card[0], tuple(frame.card[1])) if frame.card is not None else None
        sig = (tuple(sorted(rows.items())), status, card, scene)
        if not force and sig == self._sig:
            return False
        self._sig = sig
        surf = self.surface
        full = force or self._rows is None or scene != self._scene or frame.card is not None
        if full:
            surf.fill(self.theme.bg)
            rects: list[Rect] | None = None
            if not frame.dark:
                if status:
                    self._draw_status(status)
                if frame.card is not None:
                    self._draw_card(frame.card[1], g.px)
                else:
                    for r, items in rows.items():
                        self._draw_row(r, items, g, frame)
        else:
            rects = []
            if status != self._status:
                rects.append(self._paint_band(self._status_band()))
                if status:
                    self._draw_status(status)
            old = self._rows or {}
            for r in sorted(set(rows) | set(old)):
                if rows.get(r) != old.get(r):
                    rects.append(self._paint_band(self._row_band(r, g)))
                    self._draw_row(r, rows.get(r, ()), g, frame)
        self._rows, self._scene, self._status = rows, scene, status
        self.dirty = self._to_window(rects)
        return True

    def _geometry(self) -> _Geometry:
        """Where text cells go on the logical surface, for the current layout."""
        m = self.metrics
        assert m is not None
        if self.layout == "grid":
            _, _, margin, cell_w, line_h, px = self._grid_geometry()
            return _Geometry(margin, margin, cell_w, line_h, px)
        left = m.margin_x + (m.width - 2 * m.margin_x - m.cols * m.cell_w) / 2
        return _Geometry(left, m.margin_y + m.strip_h, m.cell_w, m.line_h, m.font_px)

    def _paint_band(self, rect: Rect) -> Rect:
        """Fill `rect` with the background; returns it, clipped to the logical surface."""
        r = self.pg.Rect(rect).clip(self.surface.get_rect())
        self.surface.fill(self.theme.bg, r)
        return (r.x, r.y, r.w, r.h)

    def _row_band(self, row: int, g: _Geometry) -> Rect:
        """The full-width band of text row `row`; bands of neighbouring rows never overlap."""
        y0 = round(g.top + row * g.line_h)
        y1 = round(g.top + (row + 1) * g.line_h)
        return (0, y0, self.surface.get_width(), max(1, y1 - y0))

    def _status_band(self) -> Rect:
        """Everything above the first text row: margin and status strip."""
        m = self.metrics
        assert m is not None
        return (0, 0, self.surface.get_width(), max(1, round(m.margin_y + m.strip_h)))

    def _to_window(self, rects: list[Rect] | None) -> list[Rect] | None:
        """Copy the painted logical rectangles to the window (rotating them if needed).

        Returns the rectangles to update in window pixels, or None for the whole window.
        """
        pg = self.pg
        if not self.rotate:
            return rects
        if rects is None:
            self.window.blit(pg.transform.rotate(self.surface, self.rotate), (0, 0))
            return None
        width = self.surface.get_width()
        out: list[Rect] = []
        for x, y, w, h in rects:
            if w <= 0 or h <= 0:
                continue
            piece = pg.transform.rotate(self.surface.subsurface((x, y, w, h)), self.rotate)
            # 90 degrees counter-clockwise: logical (x, y) lands at (y, width - x)
            dest = (y, width - x - w, h, w)
            self.window.blit(piece, dest[:2])
            out.append(dest)
        return out

    def _status_text(self, text: str) -> str:
        """The status strip trimmed to whole parts so it fits the width."""
        m = self.metrics
        assert m is not None
        font = self.font(self._status_px())
        max_chars = max(1, int((m.width - 2 * m.margin_x) // max(1, font.size("M")[0])))
        return fit_status(text, max_chars)

    def _status_px(self) -> int:
        """Letter height of the status strip: 45% of the text, at least 12 pixels."""
        assert self.metrics is not None
        return max(12, round(self.metrics.font_px * 0.45))

    def _draw_status(self, text: str) -> None:
        """Draw the (already fitted) status strip in small type above the text."""
        m = self.metrics
        assert m is not None
        glyphs = self._glyphs(self._status_px(), text, self.theme.status)
        self.surface.blit(glyphs, (m.margin_x, m.margin_y))

    def _draw_card(self, lines: list[str], px: int) -> None:
        """Draw a birth or death card centred, the first line larger, shrinking to fit."""
        m = self.metrics
        assert m is not None
        big = round(px * 1.2)
        heights = [big if n == 0 else px for n in range(len(lines))]
        gap = px * 0.8
        total = sum(heights) + gap * (len(lines) - 1)
        y = (m.height - total) / 2
        for n, line in enumerate(lines):
            size = heights[n]
            colour = self.theme.card if n == 0 else self.theme.status
            g = self._glyphs(size, line, colour)
            if g.get_width() > m.width - 2 * m.margin_x:
                g = self._glyphs(
                    max(12, int(size * (m.width - 2 * m.margin_x) / g.get_width())), line, colour
                )
            self.surface.blit(g, ((m.width - g.get_width()) / 2, y))
            y += size + gap

    def _draw_row(self, row: int, items: tuple[RowItem, ...], g: _Geometry, frame: Frame) -> None:
        """Draw one text row's words, gauge and cursor (see `_rows`) at cell positions.

        Drawing is clipped to the row's band, so a font taller than the row (small grids)
        never paints into a neighbour that a partial repaint would not redraw.
        """
        font_h = self.font(g.px).get_height()
        y = g.top + row * g.line_h + (g.line_h - font_h) / 2
        self.surface.set_clip(self._row_band(row, g))
        try:
            self._draw_items(items, y, font_h, g, frame)
        finally:
            self.surface.set_clip(None)

    def _draw_items(
        self, items: tuple[RowItem, ...], y: float, font_h: int, g: _Geometry, frame: Frame
    ) -> None:
        """Draw a row's items with their tops at `y` pixels."""
        for kind, col, text, colour in items:
            x = g.left + col * g.cell_w
            if kind == "text":
                self.surface.blit(self._glyphs(g.px, text, colour), (round(x), round(y)))
            elif kind == "gauge":
                self._draw_gauge(frame.gauge, x, y, frame.cols * g.cell_w, font_h)
            else:  # the cursor
                self.pg.draw.rect(
                    self.surface, colour, (round(x), round(y), round(g.cell_w), font_h)
                )

    def _draw_gauge(
        self, fraction: float | None, x: float, y: float, width: float, h: float
    ) -> None:
        """Draw the memory gauge as an outlined bar filled to `fraction` (0..1)."""
        th = self.theme
        pg = self.pg
        bar_h = max(2, round(h * 0.3))
        y0 = round(y + (h - bar_h) / 2)
        pg.draw.rect(self.surface, th.dimmed(th.gauge), (round(x), y0, round(width), bar_h), 1)
        pg.draw.rect(
            self.surface, th.gauge, (round(x), y0, round(width * (fraction or 0.0)), bar_h)
        )

    def _grid_geometry(self) -> tuple[int, int, int, float, float, int]:
        """Rows, cols, margin, cell width, line height and font size (pixels) for the grid."""
        th = self.theme
        w, h = self.surface.get_size()
        margin = max(4, round(min(w, h) * 0.04))
        rows, cols = derive_grid(
            w - 2 * margin, h - 2 * margin, self.grid, self.min_font_px, th.advance
        )
        cell_w = (w - 2 * margin) / cols
        line_h = (h - 2 * margin) / rows
        px = int(min(cell_w / th.advance, line_h / 1.25))
        return rows, cols, margin, cell_w, line_h, px


def _rows(frame: Frame, theme: Theme) -> dict[int, tuple[RowItem, ...]]:
    """What each text row shows, as comparable items: a row is repainted when its items
    change. Colours, not fade progress, are compared, and fades move in `FADE_STEPS`
    steps, so a fading row is repainted only when its colour visibly moves."""
    rows: dict[int, list[RowItem]] = {}
    for s in frame.spans:
        if s.kind == "gauge":
            item: RowItem = ("gauge", s.col, f"{frame.gauge or 0.0:.4f}", theme.gauge)
        else:
            fade = round(s.fade * FADE_STEPS) / FADE_STEPS
            item = ("text", s.col, s.text, theme.word(s.kind, fade, frame.dim))
        rows.setdefault(s.row, []).append(item)
    cur = frame.cursor
    if cur is not None and cur.mode in ("on", "dim"):
        colour = theme.dimmed(theme.live) if cur.mode == "dim" else theme.live
        rows.setdefault(cur.row, []).append(("cursor", cur.col, "", colour))
    return {r: tuple(items) for r, items in rows.items()}


def _console() -> bool:
    """On the Pi's console (no desktop session) the screen driver goes full screen."""
    return not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")


def _font_path(theme: Theme) -> Path | None:
    """The font file: $EPITAPH_FONT, else the theme's, else DejaVu Sans Mono, else None.

    None makes pygame fall back to its built-in font.
    """
    env = os.environ.get("EPITAPH_FONT")
    if env and Path(env).exists():
        return Path(env)
    if theme.font.exists():
        return theme.font
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",):
        if Path(p).exists():
            return Path(p)
    return None
