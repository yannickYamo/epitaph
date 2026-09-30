# Writing a display

A display is a separate process that subscribes to the controller's events and draws them
(BUILD_PLAN 4, 6.3). Everything a display needs to know about a life already lives in
`epitaph.display.layout`, which is pure Python, so a new display only draws.

## The pieces

| Module | What it does |
|---|---|
| `display/layout.py` | `LifeView` (events in; words, states, typing timeline, cursor, cards, vitals, gauge, snapshot out), `compose_flow` / `compose_grid` (a `Frame` of cells), `flow_metrics`, `derive_grid`, `map_charset` |
| `display/themes/` | Colours per word state, the font (IBM Plex Mono, OFL), WCAG contrast helpers |
| `display/app.py` | `drive(driver, source)`: feeds events, redraws at a fixed rate; `make_driver` |
| `display/terminal.py` | ANSI driver (any terminal, over SSH) |
| `display/screen.py` | pygame driver (window, full screen, offscreen) |
| `display/remote.py` | `epitaph display [--connect HOST]`: SSH tunnel, reconnect, snapshot redraw |
| `display/replay.py` | `epitaph replay LIFE --speed --from`: republish `events.jsonl` |
| `display/screenshot.py` | Offscreen PNGs; OCR, contrast and whole-word checks (test D13) |

## A new driver in four methods

```python
class MyDriver:
    def __init__(self) -> None:
        self.view = LifeView()          # one per driver
        self.closed = False

    def open(self) -> None: ...          # grab the device
    def close(self) -> None: ...         # give it back

    def handle(self, event: dict) -> None:
        self.view.handle(event, time.monotonic())

    def render(self, now: float | None = None) -> None:
        frame = compose_grid(self.view, now, rows, cols, "segment16")   # or compose_flow
        ...  # draw frame.spans, frame.cursor, frame.card, frame.status; frame.dark = blank

    def screenshot(self, path: str) -> None: ...
```

Then `asyncio.run(drive(MyDriver(), remote.reconnecting(connect)))`.

## Rules every driver keeps (BUILD_PLAN 5.12)

- **Typing runs on the display's clock.** `LifeView` schedules each word after the previous
  one, letter by letter from `char_ms`, then `pause_after_ms`. Draw what `compose_*` returns
  for `now`; never type on event arrival.
- **Whole words.** The frame places each word with its full length before its first letter.
  Only a word longer than a whole line is cut (`Frame.split_words` counts those).
- **Newest at the bottom**, a blank line between thoughts (flow). A grid starts each
  thought on a new row.
- **Word states:** `live` (theme colour), `fading` (`Span.fade` 0 to 1 towards the forgotten
  grey), `forgotten`, `inherited`. A grid drops forgotten words and shows the memory gauge
  (`Frame.gauge`, bottom row) instead.
- **Cursor:** `Frame.cursor.mode` is `on`, `off` (blink phase) or `dim` (reload). No cursor
  at death, in the silence, or while loading.
- **Reload:** `Frame.dim` dims the whole text.
- **Cards:** `Frame.card = (kind, lines)` replaces the text (birth while loading and until the
  first word; death after the last letter, for 8 s).
- **Contrast:** live text at least 12:1 on the rendered pixels; fading text stays at least
  4.5:1.
- **Snapshots:** a `snapshot` event resets the view to exactly what it describes; after a
  reconnect or an overflow, redraw everything.

## Checking a display

- `python -m epitaph.display.screenshot --out DIR`: D13 at 800×480, 1280×720, 1920×1080 and
  1080×1920 (OCR ≥ 95%, contrast ≥ 12:1, no split words).
- `epitaph sim --events > life.jsonl`, then `epitaph replay life.jsonl --speed 20 --driver ...`.
