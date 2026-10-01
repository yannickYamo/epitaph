# Writing a display

A display is a separate process that subscribes to the controller's events and draws them
(BUILD_PLAN 4, 6.3). Everything a display needs to know about a life already lives in
`epitaph.display.layout`, which is pure Python, so a new display only draws.

## The pieces

| Module | What it does |
|---|---|
| `display/layout.py` | `LifeView` (events in; words, states, typing timeline, cursor, cards, vitals, gauge, snapshot out), `compose_flow` / `compose_grid` (a `Frame` of cells), `flow_metrics`, `derive_grid`, `map_charset`, `verify_probe` (verify-life's split and bright-word checks) |
| `display/themes/` | Colours per word state, the font (IBM Plex Mono, OFL), WCAG contrast helpers |
| `display/app.py` | `drive(driver, source)`: feeds events, redraws when something is due (`next_frame`); `make_driver` |
| `display/terminal.py` | ANSI driver (any terminal, over SSH) |
| `display/screen.py` | pygame driver (window, full screen, offscreen) |
| `display/remote.py` | `epitaph display [--connect HOST]`: SSH tunnel, reconnect, snapshot redraw |
| `display/presence.py` | `epitaph display --screen-present`: is a screen connected (the unit's `ExecCondition`) |
| `display/replay.py` | `epitaph replay LIFE --speed --from`: republish `events.jsonl` |
| `display/screenshot.py` | Offscreen PNGs; OCR, contrast and whole-word checks (test D13) |
| `display/bench.py` | CPU share of the pygame screen while typing and during a fade (D7) |

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
  first word, at most `birth_card_seconds`; death after the last letter, for
  `death_card_seconds`).
- **Fades** last `fade_seconds`, including the words a reload forgets (a `forget` during the
  reload silence fades under the dimming and keeps fading after it).
- **Contrast:** live text at least 12:1 on the rendered pixels; fading text stays at least
  4.5:1.
- **Snapshots:** a `snapshot` event resets the view to exactly what it describes; after a
  reconnect or an overflow, redraw everything.

## Cheap redraws (BUILD_PLAN 9 D7)

The Pi has four cores and the creature gets three, so a display must stay well under 5% of
one core. `drive` does not redraw a still screen: it sleeps until `LifeView.next_change`
(the next letter, the end of typing, the next cursor blink), a new event, or at most
0.25 s. Inside a frame, draw only what changed: `screen.py` compares each text row's items
(text, column, colour) with the last frame's and repaints only the changed columns, then
sends just those rectangles to the display. A terminal driver gets the same effect by
diffing cells.

## Watching the Pi from the laptop

`epitaph display --connect pi --driver terminal` opens `ssh -N -L <free port>:127.0.0.1:7707`
(keys only, `BatchMode`), subscribes through it and draws in the terminal. `pi` means
`pi,pi-eth`: every (re)start of the tunnel tries Wi-Fi first, then the cable. When the
tunnel or the controller goes away the status strip says `reconnecting`; the view retries
with backoff (0.5 s doubling to 5 s), starts a new ssh when the old one has died, and
redraws everything from the snapshot the bus sends first on every subscription. If no
tunnel can be set up at all at the start (unknown alias, no key), it exits 2 with every
host's ssh error instead of retrying silently. `--ssh PROGRAM` swaps the ssh binary.

## Is a screen connected? (`--screen-present`)

`epitaph display --screen-present` exits 0 when a screen is connected and 1 when not,
printing one line (`screen: no (drm: no connector connected (card1-HDMI-A-1=disconnected,
...))`) and never a traceback. The display unit runs it as its `ExecCondition`, so a
headless Pi skips the unit instead of crash-looping. The answer comes from, in order:

1. `EPITAPH_SCREEN=yes|no|auto` in the environment (a unit drop-in);
2. `[display] screen = "yes" | "no" | "auto"` in the config (default `auto`);
3. any `/sys/class/drm/card*-*/status` reading `connected` (writeback connectors ignored).

A bad override value is reported on the line and treated as `auto`. `driver = "auto"`
uses the same answer to choose between the screen and the terminal.

## Checking a display

- `python -m epitaph.display.screenshot --out DIR [--events LIFE.jsonl]`: D13 at 800×480,
  1280×720, 1920×1080 and 1080×1920 (OCR ≥ 95%, contrast ≥ 12:1, no split words), on the
  built-in sample and on a recorded life (a simulated `pi4/skeleton-1200` life by default)
  at two moments: a full screen before the first forgetting, and just before death.
- `pytest -m display tests/display`: the same as tests. `tests/display/data/skeleton-1200.jsonl`
  is the recorded fake life (`epitaph sim --profile pi4/skeleton-1200 --hardware pi4-4gb
  --events --seed 0`). OCR tests carry the `tesseract` marker: they skip when tesseract is
  missing, except in CI (`CI` set), where they fail instead.
- `epitaph sim --events > life.jsonl`, then `epitaph replay life.jsonl --speed 20 --driver ...`.
- `SDL_VIDEODRIVER=offscreen python -m epitaph.display.bench --full`: the CPU share at
  800×480, 1280×720 and 1920×1080, new drawing against whole-frame painting. Run it on the
  Pi pinned to one core (`taskset -c 0`) for the numbers that matter.
