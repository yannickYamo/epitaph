"""The loop every display driver shares: events in, frames out (BUILD_PLAN 6.4 Display).

A driver owns a `LifeView` and knows how to draw a `Frame`. `drive` feeds it events from
any async source (the local bus, the SSH tunnel, a replay) while a render loop redraws it
at a fixed rate, so typing runs on the display's own clock and never waits on the source.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from epitaph.display.layout import LifeView

Event = dict[str, Any]


class Driver(Protocol):
    view: LifeView
    closed: bool

    def open(self) -> None: ...

    def close(self) -> None: ...

    def handle(self, event: Event) -> None: ...

    def render(self, now: float | None = None) -> None: ...

    def screenshot(self, path: str) -> None: ...


async def drive(
    driver: Driver,
    source: AsyncIterator[Event],
    fps: float = 30.0,
    exit_when_done: bool = True,
    linger_s: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    on_event: Callable[[Event], None] | None = None,
) -> None:
    """Run `driver` until it is closed, or (with `exit_when_done`) until the source has
    ended and every queued letter has been typed."""
    done = asyncio.Event()

    async def consume() -> None:
        try:
            async for event in source:
                driver.handle(event)
                if on_event is not None:
                    on_event(event)
        finally:
            done.set()

    task = asyncio.create_task(consume())
    period = 1.0 / max(1.0, fps)
    driver.open()
    try:
        while not driver.closed:
            now = clock()
            driver.render(now)
            if done.is_set() and exit_when_done and now >= driver.view.tail + linger_s:
                break
            if task.done() and task.exception() is not None:
                raise task.exception()  # type: ignore[misc]
            await asyncio.sleep(max(0.0, period - (clock() - now)))
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        driver.close()


async def iterate(events: list[Event]) -> AsyncIterator[Event]:
    """An async source from a list (tests, screenshots)."""
    for e in events:
        yield e
        await asyncio.sleep(0)


# -- building drivers from config and flags ---------------------------------------------------


def display_config(hardware: str | None = None, profile: str | None = None) -> dict[str, Any]:
    """The `[display]` section (default.toml + hardware overlay), plus `events_port`.

    No profile is needed to draw, so none is loaded (`profile` is accepted and ignored).
    Falls back to the built-in defaults when the files cannot be read.
    """
    import tomllib

    from epitaph.config import CONFIG_DIR, deep_merge, detect_hardware

    try:
        data = tomllib.loads((CONFIG_DIR / "default.toml").read_text())
        hw = hardware or str(data.get("life", {}).get("hardware", "auto"))
        if hw == "auto":
            hw = detect_hardware()
        overlay = CONFIG_DIR / "hardware" / f"{hw}.toml"
        if overlay.exists():
            data = deep_merge(data, tomllib.loads(overlay.read_text()))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    out = dict(data.get("display", {}))
    out.setdefault("events_port", int(data.get("events", {}).get("port", 7707)))
    return out


def parse_size(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    w, _, h = text.lower().partition("x")
    return int(w), int(h)


def make_driver(name: str, cfg: dict[str, Any], **opts: Any) -> Driver:
    """A driver by name ("terminal" or "screen") configured from `[display]`."""
    from epitaph.display.layout import ViewSettings
    from epitaph.display.themes import get_theme

    settings = ViewSettings.from_config(cfg)
    theme = get_theme(str(opts.pop("theme", None) or cfg.get("theme", "plain")))
    layout = str(opts.pop("layout", None) or cfg.get("layout", "flow"))
    grid_raw = cfg.get("grid", [6, 16])
    grid = (int(grid_raw[0]), int(grid_raw[1]))
    common: dict[str, Any] = {
        "settings": settings,
        "theme": theme,
        "line_chars": int(cfg.get("line_chars", 48)),
        "status_strip": bool(cfg.get("status_strip", True)),
        "layout": layout,
        "grid": grid,
        "charset": str(cfg.get("charset", "unicode")),
    }
    if name == "terminal":
        from epitaph.display.terminal import TerminalDriver

        return TerminalDriver(**common, **opts)
    if name == "screen":
        from epitaph.display.screen import ScreenDriver

        return ScreenDriver(
            **common,
            min_font_px=int(cfg.get("min_font_px", 36)),
            orientation=str(cfg.get("orientation", "landscape")),
            **opts,
        )
    raise ValueError(f"unknown display driver {name!r} (terminal or screen)")
