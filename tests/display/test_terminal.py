"""D2: the ANSI terminal driver (BUILD_PLAN 9 D2)."""

from __future__ import annotations

import io
import re
from typing import Any

from epitaph.display.app import drive, iterate, make_driver, parse_size
from epitaph.display.layout import ViewSettings
from epitaph.display.terminal import TerminalDriver, _rgb_to_256, detect_color_mode, sgr
from epitaph.display.themes import PLAIN

from .test_layout import ev, word


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def make(clock: Clock, **kw: Any) -> tuple[TerminalDriver, io.StringIO]:
    out = io.StringIO()
    d = TerminalDriver(out=out, size=(60, 12), clock=clock, color="truecolor", **kw)
    return d, out


def screen_text(d: TerminalDriver) -> list[str]:
    cols, rows = d.term_size()
    return [
        "".join(d.last_cells.get((r, c), (" ", PLAIN.bg))[0] for c in range(cols)).rstrip()
        for r in range(rows)
    ]


def test_types_letters_with_cadence_and_only_writes_changes() -> None:
    clock = Clock()
    d, out = make(clock)
    d.open()
    d.handle(ev("birth", model="m", quant="Q6_K"))
    d.handle(word(1, 0, "hello", 100, pause=600))
    clock.t = 0.25
    d.render()
    assert any(line.strip().startswith("hel█") for line in screen_text(d))
    n = len(out.getvalue())
    d.render()  # nothing changed: nothing written
    assert len(out.getvalue()) == n
    clock.t = 0.45
    d.render()
    new = out.getvalue()[n:]
    assert "l" in new and "o" in new and len(new) < 200  # a few cells, not a redraw
    assert "\x1b[2J" not in new


def test_cursor_blinks_in_pauses_dims_in_reload_and_goes_at_death() -> None:
    clock = Clock()
    d, _ = make(clock)
    d.handle(ev("birth", model="m"))
    d.handle(word(1, 0, "hi", 0, pause=5000))
    blink = d.view.s.blink_s
    clock.t = 0.01
    d.render()
    assert "█" in "".join(screen_text(d))
    clock.t = blink + 0.01
    d.render()
    assert "█" not in "".join(screen_text(d))
    d.handle(ev("reload", **{"from": "a", "to": "b"}))
    d.render()
    cur = [cell for cell in d.last_cells.values() if cell[0] == "█"]
    assert cur and cur[0][1] == PLAIN.dimmed(PLAIN.live)
    hi = [cell for cell in d.last_cells.values() if cell[0] == "h"]
    assert hi[0][1] == PLAIN.dimmed(PLAIN.live)  # the whole text dims during a reload
    d.handle(ev("reload_done", seconds=1))
    d.handle(ev("death", cause="oom", lived_s=10))
    d.render()
    assert "█" not in "".join(screen_text(d))


def test_status_strip_card_and_dark() -> None:
    clock = Clock()
    d, _ = make(clock)
    d.handle(ev("birth_loading", life=5, model="llama", quant="Q6_K"))
    d.render()
    text = screen_text(d)
    assert text[0].strip().startswith("life 5")
    assert any(line.strip() == "life 5" for line in text[1:])
    assert any("waking" in line for line in text)
    d.handle(ev("birth", life=5))
    d.handle(word(1, 0, "x", 0))
    d.handle(ev("death", life=5, cause="deadline", lived_s=60))
    d.handle(ev("death_shown", life=5))
    d.handle(ev("silence", life=5, seconds=90, style="dark"))
    clock.t = 3.0
    d.render()
    assert any("its time ran out" in line for line in screen_text(d))
    clock.t = 100.0
    d.render()
    assert not d.last_cells


def test_snapshot_forces_full_redraw_and_resize_too() -> None:
    clock = Clock()
    d, out = make(clock)
    d.handle(ev("snapshot", life=2, words=[{"turn": 1, "i": 0, "text": "again", "state": "live"}]))
    d.render()
    assert out.getvalue().count("\x1b[2J") == 1
    assert any("again" in line for line in screen_text(d))
    d.handle(ev("snapshot", life=2, words=[{"turn": 1, "i": 0, "text": "again", "state": "live"}]))
    d.render()
    assert out.getvalue().count("\x1b[2J") == 2
    d.fixed_size = (40, 10)
    d.render()
    assert out.getvalue().count("\x1b[2J") == 3


def test_open_close_and_screenshot(tmp_path) -> None:
    clock = Clock()
    d, out = make(clock)
    d.open()
    d.open()
    assert out.getvalue() == "\x1b[?1049h\x1b[?25l"
    d.handle(ev("birth"))
    d.handle(word(1, 0, "shot", 0))
    d.render()
    path = tmp_path / "s.txt"
    d.screenshot(str(path))
    assert "shot" in path.read_text()
    d.close()
    d.close()
    assert out.getvalue().endswith("\x1b[0m\x1b[?25h\x1b[?1049l")


def test_grid_layout_in_a_terminal() -> None:
    clock = Clock()
    d, _ = make(clock, layout="grid", grid=(4, 10), charset="segment16")
    d.handle(ev("birth"))
    d.handle(ev("vitals", recall=10, recall_used=5))
    for i, w in enumerate(["the", "quick", "brown", "fox"]):
        d.handle(word(1, i, w, 0))
    d.render()
    text = "\n".join(screen_text(d))
    assert "BROWN FOX" in text and "#####-----" in text


def test_colour_modes() -> None:
    assert detect_color_mode({"COLORTERM": "truecolor", "TERM": "xterm"}) == "truecolor"
    assert detect_color_mode({"TERM": "xterm-256color"}) == "256"
    assert detect_color_mode({"TERM": "dumb"}) == "none"
    assert sgr((1, 2, 3), (4, 5, 6), "truecolor") == "\x1b[38;2;1;2;3;48;2;4;5;6m"
    assert re.fullmatch(r"\x1b\[38;5;\d+;48;5;\d+m", sgr((200, 10, 10), (0, 0, 0), "256"))
    assert sgr((1, 2, 3), (4, 5, 6), "none") == ""
    assert _rgb_to_256((0, 0, 0)) == 16
    assert _rgb_to_256((255, 255, 255)) == 231
    assert 232 <= _rgb_to_256((128, 128, 128)) <= 255
    assert _rgb_to_256((255, 0, 0)) == 196


def test_256_colour_output_and_small_terminal() -> None:
    out = io.StringIO()
    d = TerminalDriver(out=out, size=(20, 4), color="256", clock=lambda: 1.0, alt_screen=False)
    d.handle(ev("birth"))
    d.handle(word(1, 0, "tiny", 0))
    d.render()
    assert "38;5;" in out.getvalue()
    assert d.last_frame is not None and d.last_frame.status is None  # no room for a strip


async def test_drive_runs_until_the_typing_is_done() -> None:
    out = io.StringIO()
    d = TerminalDriver(out=out, size=(50, 10), color="none")
    events = [ev("birth"), word(1, 0, "abc", 10), word(1, 1, "def", 10)]
    seen: list[str] = []
    await drive(
        d, iterate(events), fps=200, linger_s=0.05, on_event=lambda e: seen.append(e["type"])
    )
    assert seen == ["birth", "word", "word"]
    assert any("abc def" in line for line in screen_text(d))
    assert out.getvalue().endswith("\x1b[?1049l")


async def test_drive_stops_when_driver_closes() -> None:
    d = TerminalDriver(out=io.StringIO(), size=(50, 10))

    async def forever():
        yield ev("birth")
        while True:
            import asyncio

            await asyncio.sleep(0.01)
            d.closed = True
            yield ev("error", where="x", message="y")

    await drive(d, forever(), fps=100, exit_when_done=False)
    assert d.closed


def test_make_driver_from_config_and_sizes() -> None:
    cfg = {
        "theme": "segment16",
        "layout": "grid",
        "grid": [3, 12],
        "line_chars": 30,
        "fade_seconds": 2,
    }
    d = make_driver("terminal", cfg, size=(40, 10), out=io.StringIO())
    assert isinstance(d, TerminalDriver)
    assert d.theme.name == "segment16" and d.layout == "grid" and d.grid == (3, 12)
    assert d.view.s.fade_s == 2 and d.line_chars == 30
    d2 = make_driver("terminal", {}, layout="flow", theme="plain", out=io.StringIO())
    assert isinstance(d2, TerminalDriver) and d2.layout == "flow"
    try:
        make_driver("hologram", {})
    except ValueError as e:
        assert "hologram" in str(e)
    assert parse_size("800x480") == (800, 480)
    assert parse_size(None) is None


def test_settings_from_config() -> None:
    s = ViewSettings.from_config({"fade_seconds": 3, "cursor_blink_ms": 400, "birth_card": False})
    assert (s.fade_s, s.blink_s, s.birth_card) == (3.0, 0.4, False)
