"""A whole recorded 30-minute life replayed through every driver and theme (D7, D13).

`data/default-1800.jsonl` is a simulated `pi4/default` life: birth card, two reloads that
forget, erosion, the clock falling, an OOM death, the silence. It stands in for a real Pi
life (the events are the same contract), so the tests depend on nothing outside the repo.
Each driver draws it on the life's own clock, frame by frame, and must never fail; the
visuals of each phase must appear in order.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import pytest

from epitaph.display import replay
from epitaph.display.layout import Frame, ViewSettings, life_times
from epitaph.display.terminal import TerminalDriver
from epitaph.display.themes import PLAIN, SEGMENT16, Theme

os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")

Event = dict[str, Any]


def _moments(events: list[Event]) -> list[tuple[float, list[Event]]]:
    """(time, events due then) on the life clock, with extra frames every second around
    the reloads, the death and the silence so every visual is drawn."""
    times = life_times(events)
    marks = [t for e, t in zip(events, times, strict=True) if e["type"] in ("reload", "death")]
    extra = {round(m + k * 0.5, 2) for m in marks for k in range(0, 2 * 140)}
    out: dict[float, list[Event]] = {t: [] for t in extra}
    for e, t in zip(events, times, strict=True):
        out.setdefault(t, []).append(e)
    return sorted(out.items())


def _seen(frames: list[tuple[float, Frame]]) -> dict[str, float]:
    """When each visual was first drawn."""
    first: dict[str, float] = {}

    def see(name: str, t: float) -> None:
        first.setdefault(name, t)

    for t, f in frames:
        if f.card is not None:
            see(f"card:{f.card[0]}", t)
        if f.dark:
            see("dark", t)
        if f.cursor is not None:
            see(f"cursor:{f.cursor.mode}", t)
        for s in f.spans:
            see(f"span:{s.kind}", t)
    return first


def _check_phases(first: dict[str, float], death_t: float, fades: bool = True) -> None:
    assert first["card:birth"] < first["span:live"]
    assert first["cursor:dim"] < death_t  # the reload
    if fades:
        assert first["span:fading"] < death_t  # forgotten words fade
    else:
        assert "span:fading" not in first  # a grid drops them at once
    assert first["card:death"] > death_t
    assert first["dark"] > first["card:death"]  # the silence comes after the card
    assert "span:forgotten" not in first  # forgotten words are gone, never drawn


@pytest.mark.parametrize(
    "theme,size,orientation",
    [
        (PLAIN, (1280, 720), "landscape"),
        (PLAIN, (800, 480), "portrait"),
        (SEGMENT16, (800, 480), "landscape"),
    ],
    ids=["plain-1280x720", "plain-portrait", "segment16-800x480"],
)
def test_the_whole_life_renders_on_the_screen(
    theme: Theme,
    size: tuple[int, int],
    orientation: str,
    default_life: list[Event],
    tmp_path: Path,
) -> None:
    pytest.importorskip("pygame")
    from epitaph.display.screen import ScreenDriver

    d = ScreenDriver(settings=ViewSettings(), theme=theme, size=size, orientation=orientation)
    d.open()
    frames: list[tuple[float, Frame]] = []
    try:
        for t, due in _moments(default_life):
            for e in due:
                d.view.handle(e, t)
            d.render(t)
            assert d.last_frame is not None
            frames.append((t, d.last_frame))
            if any(e["type"] == "death" for e in due):
                d.screenshot(str(tmp_path / "death.png"))
    finally:
        d.close()
    death_t = next(float(e["t"]) for e in default_life if e["type"] == "death")
    _check_phases(_seen(frames), death_t, fades=theme.look != "segment16")
    assert (tmp_path / "death.png").stat().st_size > 0


def test_the_whole_life_renders_in_a_terminal(default_life: list[Event]) -> None:
    out = io.StringIO()
    d = TerminalDriver(out=out, size=(60, 16), color="truecolor", alt_screen=False)
    d.open()
    frames: list[tuple[float, Frame]] = []
    for t, due in _moments(default_life):
        for e in due:
            d.view.handle(e, t)
        d.render(t)
        assert d.last_frame is not None
        frames.append((t, d.last_frame))
    d.close()
    death_t = next(float(e["t"]) for e in default_life if e["type"] == "death")
    _check_phases(_seen(frames), death_t)
    assert any(f.card is not None and "its memory was taken" in f.card[1] for _, f in frames)
    assert out.getvalue().count("\x1b[2J") == 1  # one clear, then only changed cells


def test_epitaph_replay_plays_the_whole_life(
    default_life: list[Event], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`epitaph replay data/default-1800.jsonl --speed 10000` in a terminal, end to end."""
    import epitaph.display.app as app

    from .conftest import DEFAULT_LIFE

    drivers: list[TerminalDriver] = []

    def make(name: str, cfg: dict[str, Any], **opts: Any) -> TerminalDriver:
        d = TerminalDriver(out=io.StringIO(), size=(50, 12), color="none", alt_screen=False)
        drivers.append(d)
        return d

    monkeypatch.setattr(app, "make_driver", make)
    assert replay.main([str(DEFAULT_LIFE), "--speed", "10000", "--max-gap", "0.001"]) == 0
    view = drivers[0].view
    assert view.mode == "silence" and view.death.get("cause") == "oom"
    assert sum(1 for _ in view.words()) == sum(e["type"] == "word" for e in default_life)


def test_a_mid_life_replay_starts_from_a_snapshot(
    default_life: list[Event], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--from` inside the first reload: the screen starts dimmed-cursor, words fading."""
    import epitaph.display.app as app

    from .conftest import DEFAULT_LIFE

    drivers: list[TerminalDriver] = []

    def make(name: str, cfg: dict[str, Any], **opts: Any) -> TerminalDriver:
        d = TerminalDriver(out=io.StringIO(), size=(50, 12), color="none", alt_screen=False)
        drivers.append(d)
        return d

    monkeypatch.setattr(app, "make_driver", make)
    reload_t = next(float(e["t"]) for e in default_life if e["type"] == "reload")
    start = f"{int(reload_t + 2) // 60}:{int(reload_t + 2) % 60:02d}"
    assert (
        replay.main([str(DEFAULT_LIFE), "--speed", "5000", "--max-gap", "0.001", "--from", start])
        == 0
    )
    assert drivers and drivers[0].view.death.get("cause") == "oom"
