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
    """What `drive` needs from a display: a view to feed and a surface to draw it on.

    `closed` turns true when the user closes the window or presses the quit key; `drive`
    then stops at the next frame.
    """

    view: LifeView
    closed: bool

    def open(self) -> None:
        """Acquire the output (window, alternate screen) before the first frame."""
        ...

    def close(self) -> None:
        """Release the output and restore the terminal or display; safe to call twice."""
        ...

    def handle(self, event: Event) -> None:
        """Apply one life event to the view; never blocks on drawing."""
        ...

    def render(self, now: float | None = None) -> None:
        """Draw the view as of `now` (monotonic seconds; the current time when None)."""
        ...

    def screenshot(self, path: str) -> None:
        """Write the current frame to `path` as an image or text capture."""
        ...


async def drive(
    driver: Driver,
    source: AsyncIterator[Event],
    fps: float = 30.0,
    exit_when_done: bool = True,
    linger_s: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    on_event: Callable[[Event], None] | None = None,
    max_idle_s: float = 0.25,
) -> None:
    """Feed `driver` from `source` and redraw it until it is closed.

    Frames come at most `fps` times a second, when the view says a letter or the cursor
    is due (`LifeView.next_change`), when an event arrives, and at least every
    `max_idle_s` (fades, the status clock). A still screen costs almost nothing.

    With `exit_when_done`, also stop once the source has ended and every queued letter has
    been typed, plus `linger_s` seconds. `on_event` sees each event after the driver has.
    An exception raised by the source is re-raised here; the driver is always closed.
    """
    done = asyncio.Event()
    wake = asyncio.Event()

    async def consume() -> None:
        try:
            async for event in source:
                driver.handle(event)
                wake.set()
                if on_event is not None:
                    on_event(event)
        finally:
            done.set()
            wake.set()

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
            due = next_frame(driver.view, now, fps, max_idle_s)
            if not wake.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), max(0.0, due - clock()))
            wake.clear()
            # never faster than fps, even when events arrive in a burst
            await asyncio.sleep(max(0.0, period - (clock() - now)))
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        driver.close()


def next_frame(view: LifeView, now: float, fps: float = 30.0, max_idle_s: float = 0.25) -> float:
    """When the frame after the one drawn at `now` is due: the view's next change, but no
    sooner than one frame period and no later than `max_idle_s`."""
    period = 1.0 / max(1.0, fps)
    return min(max(view.next_change(now), now + period), now + max(period, max_idle_s))


async def iterate(events: list[Event]) -> AsyncIterator[Event]:
    """Yield `events` one at a time, letting the loop run between them (tests, screenshots)."""
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
    """Parse a "WIDTHxHEIGHT" flag such as "1920x1080" into pixels; None when empty.

    Raises ValueError when either side is not an integer.
    """
    if not text:
        return None
    w, _, h = text.lower().partition("x")
    return int(w), int(h)


def make_driver(name: str, cfg: dict[str, Any], **opts: Any) -> Driver:
    """Build the driver called `name` ("terminal" or "screen") from the `[display]` config.

    `opts` override the config (`theme`, `layout`) or pass through to the driver's
    constructor. Raises ValueError for an unknown driver or theme.
    """
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
