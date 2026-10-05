"""The terminal driver: ANSI escapes only, so it works in any terminal and over SSH.

It draws the flow layout into a cell buffer and writes only the cells that changed since
the last frame, so typing a letter costs a few bytes. Letters appear with each word's
`char_ms`, the pause after a word with `pause_after_ms`; the block cursor is solid while
typing, blinks in pauses, dims during a reload and is gone at death. Forgotten words fade
through grey (24-bit colour, or the 256-colour grey ramp when the terminal lacks it), then
leave the screen; cards are typed letter by letter. Readings are typed in the machine's dim
grey (a terminal has one letter size), the colours follow the screen's dimming above the
contrast floors, and after death the vigil is drawn centred.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from collections.abc import Callable
from typing import Any, TextIO

from epitaph.display.layout import (
    Frame,
    LifeView,
    ViewSettings,
    compose_flow,
    compose_grid,
    fit_status,
)
from epitaph.display.themes import PLAIN, Rgb, Theme

CSI = "\x1b["
Cell = tuple[str, Rgb]


def detect_color_mode(env: dict[str, str] | None = None) -> str:
    """Colour support from the environment: "truecolor", "256" or "none" (a dumb terminal)."""
    env = dict(os.environ) if env is None else env
    if env.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return "truecolor"
    if env.get("TERM", "") in ("dumb", ""):
        return "none"
    return "256"


def _rgb_to_256(c: Rgb) -> int:
    """Nearest xterm-256 index: the grey ramp for near-greys, else the 6x6x6 cube."""
    r, g, b = c
    if max(r, g, b) - min(r, g, b) < 16:  # grey: use the 24-step ramp
        grey = (r + g + b) // 3
        if grey < 8:
            return 16
        if grey > 246:
            return 231
        return 232 + round((grey - 8) / 247 * 24)
    return 16 + 36 * round(r / 255 * 5) + 6 * round(g / 255 * 5) + round(b / 255 * 5)


def sgr(fg: Rgb, bg: Rgb, mode: str) -> str:
    """The SGR escape setting `fg` on `bg` in colour `mode`; empty for "none"."""
    if mode == "truecolor":
        return f"{CSI}38;2;{fg[0]};{fg[1]};{fg[2]};48;2;{bg[0]};{bg[1]};{bg[2]}m"
    if mode == "256":
        return f"{CSI}38;5;{_rgb_to_256(fg)};48;5;{_rgb_to_256(bg)}m"
    return ""


class TerminalDriver:
    """Draws a `LifeView` in a terminal and implements the `Driver` protocol.

    `layout` is "flow" or "grid". `color` is "auto", "truecolor", "256" or "none". `size`
    fixes the terminal size in (cols, rows) instead of asking the terminal (tests).
    `alt_screen` draws on the alternate screen so the shell is restored on close.
    """

    def __init__(
        self,
        out: TextIO | None = None,
        settings: ViewSettings | None = None,
        theme: Theme = PLAIN,
        line_chars: int = 48,
        status_strip: bool = True,
        layout: str = "flow",
        grid: tuple[int, int] = (6, 16),
        charset: str = "unicode",
        color: str = "auto",
        size: tuple[int, int] | None = None,
        clock: Callable[[], float] = time.monotonic,
        alt_screen: bool = True,
    ) -> None:
        """Configure the driver; nothing is written to `out` (default stdout) until `open`."""
        self.out = out or sys.stdout
        self.view = LifeView(settings)
        self.theme = theme
        self.line_chars = line_chars
        self.status_strip = status_strip
        self.layout = layout
        self.grid = grid
        self.charset = charset
        self.color = detect_color_mode() if color == "auto" else color
        self.fixed_size = size
        self.clock = clock
        self.alt_screen = alt_screen
        self.closed = False
        self._prev: dict[tuple[int, int], Cell] = {}
        self._clear = True  # the next frame clears the screen and redraws every cell
        self._size: tuple[int, int] = (0, 0)
        self._opened = False
        self.last_frame: Frame | None = None
        self.last_cells: dict[tuple[int, int], Cell] = {}

    # -- Display protocol -------------------------------------------------------------------

    def handle(self, event: dict[str, Any]) -> None:
        """Apply `event` to the view at the driver's clock; a snapshot forces a full redraw."""
        if event.get("type") == "snapshot":
            self._clear = True  # redraw everything from the snapshot
        self.view.handle(event, self.clock())

    def open(self) -> None:
        """Enter the alternate screen and hide the cursor; does nothing if already open."""
        if self._opened:
            return
        self._opened = True
        if self.alt_screen:
            self.out.write(f"{CSI}?1049h")
        self.out.write(f"{CSI}?25l")
        self.out.flush()

    def close(self) -> None:
        """Reset colours, show the cursor and leave the alternate screen; safe to call twice."""
        if not self._opened:
            return
        self._opened = False
        self.out.write(f"{CSI}0m{CSI}?25h")
        if self.alt_screen:
            self.out.write(f"{CSI}?1049l")
        self.out.flush()

    def screenshot(self, path: str) -> None:
        """Write what is on screen as plain text (the terminal has no pixels)."""
        cells = self.last_cells
        cols, rows = self._size
        lines = [
            "".join(cells.get((r, c), (" ", self.theme.bg))[0] for c in range(cols)).rstrip()
            for r in range(rows)
        ]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def run(self, source: Any = None, fps: float = 30.0) -> None:
        """Block, drawing events from the async iterator `source` until done or closed."""
        import asyncio

        from epitaph.display.app import drive

        if source is None:
            raise ValueError("TerminalDriver.run needs an event source")
        asyncio.run(drive(self, source, fps=fps))

    # -- drawing ----------------------------------------------------------------------------

    def term_size(self) -> tuple[int, int]:
        """(cols, rows): the fixed size if given, else the terminal's (80x24 if unknown)."""
        if self.fixed_size:
            return self.fixed_size
        s = shutil.get_terminal_size((80, 24))
        return s.columns, s.lines

    def cells(self, now: float) -> dict[tuple[int, int], Cell]:
        """The whole screen as {(row, col): (char, colour)}; blank cells are omitted."""
        cols, rows = self.term_size()
        th = self.theme
        out: dict[tuple[int, int], Cell] = {}
        show_strip = self.status_strip and rows >= 6
        top = 2 if show_strip else 1
        text_rows = max(1, rows - top - 1)
        if self.layout == "grid":
            g_rows, g_cols = min(self.grid[0], text_rows), min(self.grid[1], cols - 2)
            frame = compose_grid(self.view, now, g_rows, g_cols, self.charset)
            frame.status = self.view.status_line(now) if show_strip else None
        else:
            width = max(1, min(self.line_chars, cols - 4))
            frame = compose_flow(self.view, now, width, text_rows, show_strip)
        self.last_frame = frame
        left = max(1, (cols - frame.cols) // 2)
        if frame.dark:
            return out
        b = th.brightness(frame.brightness, frame.contrast_floor)
        if frame.status and show_strip:
            strip = fit_status(frame.status, cols - 2)
            for k, ch in enumerate(strip):
                out[(0, 1 + k)] = (ch, th.lit(th.status, b))
        if frame.card is not None and frame.card[0] == "vigil":
            colour = th.lit(th.machine, frame.card_level)
            shown = frame.card_shown or [len(x) for x in frame.card[1]]
            r0 = max(top, rows // 2 - 1)
            for n, line in enumerate(frame.card[1][:2]):
                line = line[: cols - 2]
                c0 = max(1, (cols - len(line)) // 2)
                for k, ch in enumerate(line[: shown[n]]):
                    out[(r0 + 2 * n, c0 + k)] = (ch, colour)
            return out
        if frame.card is not None:
            lines = frame.card[1]
            shown = frame.card_shown or [len(x) for x in lines]
            r0 = max(top, (rows - len(lines) * 2) // 2)
            for n, line in enumerate(lines):
                line = line[: cols - 2]
                c0 = max(1, (cols - len(line)) // 2)
                colour = th.card if n == 0 else th.forgotten
                for k, ch in enumerate(line[: shown[n]]):
                    out[(r0 + 2 * n, c0 + k)] = (ch, colour)
            return out
        for s in frame.spans:
            colour = th.lit(th.word(s.kind, s.fade, frame.dim), b)
            for k, ch in enumerate(s.text):
                out[(top + s.row, left + s.col + k)] = (ch, colour)
        cur = frame.cursor
        if cur is not None and cur.mode in ("on", "dim"):
            colour = th.dimmed(th.live) if cur.mode == "dim" else th.live
            out[(top + cur.row, left + cur.col)] = ("█", th.lit(colour, b))
        return out

    def render(self, now: float | None = None) -> None:
        """Write only the cells that changed since the last frame; clear on a resize."""
        now = self.clock() if now is None else now
        size = self.term_size()
        cells = self.cells(now)
        self.last_cells = cells
        bg = self.theme.bg
        buf: list[str] = []
        if size != self._size or self._clear:
            # not on every empty frame: a dark silence would clear the screen 30 times a second
            self._size = size
            self._clear = False
            buf.append(sgr(self.theme.live, bg, self.color) + f"{CSI}2J")
            self._prev = {}
        changed = [
            pos for pos in cells.keys() | self._prev.keys() if cells.get(pos) != self._prev.get(pos)
        ]
        cur_sgr = ""
        last: tuple[int, int] | None = None
        for r, c in sorted(changed):
            ch, colour = cells.get((r, c), (" ", bg))
            code = sgr(colour, bg, self.color)
            if last != (r, c - 1):
                buf.append(f"{CSI}{r + 1};{c + 1}H")
            if code != cur_sgr:
                buf.append(code)
                cur_sgr = code
            buf.append(ch)
            last = (r, c)
        self._prev = cells
        if buf:
            self.out.write("".join(buf))
            self.out.flush()
