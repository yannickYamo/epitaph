"""The pixel driver (pygame): a window on the laptop, a full screen on the Pi (KMSDRM),
or offscreen for screenshots and tests (`SDL_VIDEODRIVER=offscreen`) (BUILD_PLAN 9 D4).

It draws the same `Frame` as the terminal: letters typed one by one with each word's
cadence, a block cursor (solid while typing, blinking in pauses, dim in a reload, gone at
death), forgotten words fading through grey, the whole text dimmed during a reload, a
small status strip above the text, and the birth and death cards.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
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


def _pygame() -> Any:
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    import pygame

    return pygame


def headless() -> bool:
    return os.environ.get("SDL_VIDEODRIVER", "") in ("offscreen", "dummy")


class ScreenDriver:
    """Draws a `LifeView` with pygame. `size=None` means the full screen (or a 1280x720
    window on a desktop session)."""

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
        self._fonts: dict[int, Any] = {}
        self.font_path: Path | None = None

    # -- setup ------------------------------------------------------------------------------

    def open(self) -> None:
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
        pg = self.pg
        w, h = physical
        self.rotate = 90 if self.orientation == "portrait" and w > h else 0
        logical = (h, w) if self.rotate else (w, h)
        self.surface = pg.Surface(logical) if self.rotate else self.window
        self._cache.clear()
        self._fonts.clear()
        self._sig = None
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
        px = max(6, int(px))
        f = self._fonts.get(px)
        if f is None:
            path = self.font_path
            f = self.pg.font.Font(str(path) if path else None, px)
            self._fonts[px] = f
        return f

    def close(self) -> None:
        if self.pg is not None:
            self.pg.display.quit()
            self.pg = None

    # -- Display protocol -------------------------------------------------------------------

    def handle(self, event: dict[str, Any]) -> None:
        self.view.handle(event, self.clock())

    def run(self, source: Any = None, fps: float = 30.0) -> None:
        import asyncio

        from epitaph.display.app import drive

        if source is None:
            raise ValueError("ScreenDriver.run needs an event source")
        asyncio.run(drive(self, source, fps=fps))

    def _ensure(self) -> Any:
        if self.pg is None:
            self.open()
        return self.pg

    def screenshot(self, path: str) -> None:
        pg = self._ensure()
        if self.last_frame is None:
            self.draw(self.clock(), force=True)
        pg.image.save(self.window, path)

    def render(self, now: float | None = None) -> None:
        pg = self._ensure()
        for ev in pg.event.get():
            if ev.type == pg.QUIT or (ev.type == pg.KEYDOWN and ev.key in (pg.K_q, pg.K_ESCAPE)):
                self.closed = True
            elif ev.type == pg.VIDEORESIZE and not self.rotate:
                self._setup(self.window.get_size())
        if self.draw(self.clock() if now is None else now):
            pg.display.flip()

    # -- drawing ----------------------------------------------------------------------------

    def _glyphs(self, px: int, text: str, colour: Rgb) -> Any:
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
        """Paint the view at `now`. Returns False (and paints nothing) when the frame is
        the same as the last one painted, so a still screen costs no drawing or flip."""
        pg = self._ensure()
        assert self.metrics is not None
        frame = self.compose(now)
        self.last_frame = frame
        sig = _signature(frame)
        if not force and sig == self._sig:
            return False
        self._sig = sig
        m = self.metrics
        surf = self.surface
        surf.fill(self.theme.bg)
        if not frame.dark:
            if self.layout == "grid":
                _, _, margin, cell_w, line_h, px = self._grid_geometry()
                if frame.card is not None:
                    self._draw_card(frame.card[1], px)
                else:
                    self._draw_cells(frame, margin, margin, cell_w, line_h, px)
            else:
                if frame.status:
                    self._draw_status(frame.status)
                if frame.card is not None:
                    self._draw_card(frame.card[1], m.font_px)
                else:
                    left = m.margin_x + (m.width - 2 * m.margin_x - m.cols * m.cell_w) / 2
                    top = m.margin_y + m.strip_h
                    self._draw_cells(frame, left, top, m.cell_w, m.line_h, m.font_px)
        if self.rotate:
            self.window.blit(pg.transform.rotate(surf, self.rotate), (0, 0))
        return True

    def _draw_status(self, text: str) -> None:
        m = self.metrics
        assert m is not None
        px = max(12, round(m.font_px * 0.45))
        font = self.font(px)
        max_chars = max(1, int((m.width - 2 * m.margin_x) // max(1, font.size("M")[0])))
        glyphs = self._glyphs(px, fit_status(text, max_chars), self.theme.status)
        self.surface.blit(glyphs, (m.margin_x, m.margin_y))

    def _draw_card(self, lines: list[str], px: int) -> None:
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

    def _draw_cells(
        self, frame: Frame, left: float, top: float, cell_w: float, line_h: float, px: int
    ) -> None:
        th = self.theme
        font_h = self.font(px).get_height()
        pad = (line_h - font_h) / 2
        for s in frame.spans:
            x = left + s.col * cell_w
            y = top + s.row * line_h + pad
            if s.kind == "gauge":
                self._draw_gauge(frame.gauge, x, y, frame.cols * cell_w, font_h)
                continue
            colour = th.word(s.kind, s.fade, frame.dim)
            self.surface.blit(self._glyphs(px, s.text, colour), (round(x), round(y)))
        cur = frame.cursor
        if cur is not None and cur.mode in ("on", "dim"):
            colour = th.dimmed(th.live) if cur.mode == "dim" else th.live
            rect = (
                round(left + cur.col * cell_w),
                round(top + cur.row * line_h + pad),
                round(cell_w),
                font_h,
            )
            self.pg.draw.rect(self.surface, colour, rect)

    def _draw_gauge(
        self, fraction: float | None, x: float, y: float, width: float, h: float
    ) -> None:
        th = self.theme
        pg = self.pg
        bar_h = max(2, round(h * 0.3))
        y0 = round(y + (h - bar_h) / 2)
        pg.draw.rect(self.surface, th.dimmed(th.gauge), (round(x), y0, round(width), bar_h), 1)
        pg.draw.rect(
            self.surface, th.gauge, (round(x), y0, round(width * (fraction or 0.0)), bar_h)
        )

    def _grid_geometry(self) -> tuple[int, int, int, float, float, int]:
        """rows, cols, margin, cell width, line height and font size for the grid."""
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


def _signature(frame: Frame) -> tuple[Any, ...]:
    card = (frame.card[0], tuple(frame.card[1])) if frame.card is not None else None
    return (
        tuple(frame.spans),
        frame.cursor,
        frame.status,
        card,
        frame.dark,
        frame.dim,
        frame.gauge,
    )


def _console() -> bool:
    """On the Pi's console (no desktop session) the screen driver goes full screen."""
    return not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")


def _font_path(theme: Theme) -> Path | None:
    env = os.environ.get("EPITAPH_FONT")
    if env and Path(env).exists():
        return Path(env)
    if theme.font.exists():
        return theme.font
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",):
        if Path(p).exists():
            return Path(p)
    return None
