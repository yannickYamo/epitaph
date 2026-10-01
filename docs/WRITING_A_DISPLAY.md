# Writing a display

A display is a separate process that subscribes to the controller's events and draws them
(BUILD_PLAN 4, 6.3). Everything a display needs to know about a life already lives in
`epitaph.display.layout`, which is pure Python, so a new display only draws.

## The pieces

| Module | What it does |
|---|---|
| `display/layout.py` | `LifeView` (events in; words, states, typing timeline, cursor, cards, vitals, gauge, snapshot out), `compose_flow` / `compose_grid` (a `Frame` of cells), `flow_metrics`, `derive_grid`, `map_charset`, `verify_probe` (verify-life's split and bright-word checks) |
| `display/cards.py` | Birth and death cards (what they say, typed with the reveal rhythm), silence styles, the idle mark |
| `display/themes/` | Colours per word state, the font (IBM Plex Mono, OFL), WCAG contrast helpers |
| `display/themes/segment16.py` | The 16-segment LED look: glyph table, segment geometry, a decoder that reads cells back from pixels |
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
- **Word states:** `live` (theme colour), `fading` (`Span.fade` 0 to 1 towards the theme's
  `forgotten` grey), `inherited`. A forgotten word is gone once its fade ends: the frame has
  no span for it, and inside a thought that still shows words its place stays empty, so
  nothing moves. A grid drops forgotten words at once and shows the memory gauge
  (`Frame.gauge`, bottom row) instead; the gauge goes at death.
- **Contrast:** at least 12:1 on the rendered pixels for live text, for every step of a
  fade until the word is gone (the fade ends on a grey that is itself 12.5:1), and for every
  card line. The idle mark and the status strip are not text to read.
- **Cursor:** `Frame.cursor.mode` is `on`, `off` (blink phase) or `dim` (reload). No cursor
  at death, in the silence, or while loading.
- **Reload:** the cursor dims and the status strip says `reloading A → B`; the text stays
  readable. `Frame.dim` (dim the whole text) is set only with `reload_dim_text = true`.
- **Fades** last `fade_seconds`, including the words a reload forgets (they fade during the
  reload silence and keep fading after it).
- **Cards:** `Frame.card = (kind, lines)` replaces the text and `Frame.card_shown` says how
  many letters of each line are typed so far: draw only those, placed where the whole line
  will stand. Cards are typed with the reveal rhythm (`card_char_ms`, the `[reveal]` word gap,
  the comma pause between lines). The birth card shows while the model loads and until the
  first word (at most `birth_card_seconds` after birth); it names the life only with
  `[life] reveal_life_number` and the model only with `birth_card_model`. At death the last
  words are typed, then fade for `fade_seconds` (`death_fade`), then the death card ("lived
  29:30", "its memory was taken") is typed at the life's last cadence and stays
  `death_card_seconds` after its last letter. A grid wraps card lines to its columns.
- **Silence styles** (`silence_style`): `dark` (default), `death_card` (the card stays),
  `last_words` (no death fade; the words come back after the card) and `idle` (dark, with
  one dim mark, `Frame.idle`, resting `idle_step_seconds` in each place).
- **Snapshots:** a `snapshot` event resets the view to exactly what it describes; after a
  reconnect or an overflow, redraw everything. Besides the words it carries `mode`, each
  fading word's `fade`, the current `reload`, `groups_left`, `quant`, and after death the
  `death` record and `death_shown_ago`, so a display that connects mid-reload, mid-fade or
  on the death card draws the same screen as one that saw every event.

## The 16-segment theme

`theme = "segment16"` draws the grid layout (6 x 16 by default, like Latent Reflection's
matrix) as amber 16-segment LED cells with a decimal point; unlit segments stay faintly
visible, as on real modules. It implies `layout = "grid"` and `charset = "segment16"`:
upper-case letters, digits and ASCII punctuation, everything else mapped (`é` → `E`, `…` →
`...`, unknown → `?`). The cursor is a lit underscore, the memory gauge a row of lit dashes,
the idle mark a lone decimal point. In a terminal the theme only gives the colours and the
charset. Cells are cached per character and colour, so a typed letter costs one blit.

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
- The same command also checks the 16-segment theme at both moments (every cell read back
  from the pixels segment by segment, `screenshot.readability_segments`) and the birth and
  death cards (OCR and the contrast of every line, `screenshot.readability_card`).
- `pytest -m display tests/display`: the same as tests. `tests/display/data/skeleton-1200.jsonl`
  is the recorded fake life (`epitaph sim --profile pi4/skeleton-1200 --hardware pi4-4gb
  --events --seed 0`). `tests/display/data/default-1800.jsonl` is the 30-minute installation
  life (`--profile pi4/default`, two reloads that forget, erosion, an OOM death), standing in
  for a real Pi life: `test_full_life.py` draws all of it frame by frame on the plain screen,
  the portrait screen, the 16-segment grid and the terminal, and plays it through `epitaph
  replay`. OCR tests carry the `tesseract` marker: they skip when tesseract is missing,
  except in CI (`CI` set), where they fail instead.
- `epitaph sim --events > life.jsonl`, then `epitaph replay life.jsonl --speed 20 --driver ...`.
- `SDL_VIDEODRIVER=offscreen python -m epitaph.display.bench --full`: the CPU share at
  800×480, 1280×720 and 1920×1080, new drawing against whole-frame painting; scenarios
  `typing`, `fade` (a reload) and `death` (the death fade and the typed card), `--theme
  segment16` for the LED grid. Run it on the Pi pinned to one core (`taskset -c 0`) for the
  numbers that matter. On the laptop at 1280×720 every case stays under 1% of one core
  (phase 2 report D).
