# Writing a display

A display is a separate process that subscribes to the controller's events and draws them.
Everything a display needs to know about a life already lives in
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
| `display/screenshot.py` | Offscreen PNGs; OCR, contrast and whole-word checks |
| `display/bench.py` | CPU share of the pygame screen while typing and during a fade |

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

## Rules every driver keeps

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
  in the silence, while loading or while a reading types; at a frozen death it stays `on`
  where the stream stopped until the vigil.
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
- **Silence styles** (`silence_style`): `dark`, `death_card` (the card stays),
  `last_words` (no death fade; the words come back after the card), `idle` (dark, with
  one dim mark, `Frame.idle`, resting `idle_step_seconds` in each place) and `vigil`
  (the installation's default; see the next section).
- **Snapshots:** a `snapshot` event resets the view to exactly what it describes; after a
  reconnect or an overflow, redraw everything. Besides the words it carries `mode`, each
  fading word's `fade`, the current `reload`, `groups_left`, `quant`, and after death the
  `death` record and `death_shown_ago`, so a display that connects mid-reload, mid-fade or
  on the death card draws the same screen as one that saw every event.

## The installation's screen

The screen of an installation life speaks in two voices and goes dark with its machine.
`LifeView` does all of it from the events of the shared interface; a driver only draws
the new parts of the `Frame`.

- **Two voices.** A `reading` event {turn, text} is the machine's line. The controller
  emits it in stream order just before the first word of the thought it precedes; the
  view types it at `machine_char_ms` (30 ms) without its `[host]` tag, as one or more
  "machine" thoughts placed right above the thought of its `turn` (no blank line), and
  the words that follow wait for it. The pauses after them give that time back (at most
  half of each), so the screen does not drift behind the machine. The life's first
  reading (turn 1), when it lists several facts, is typed one fact a line (`machine_line_pause_ms`
  between lines). Its spans are `kind = "machine"`, `col` counted in the machine's own
  cells (`Frame.machine_cols`): the screen draws them `machine_scale` (0.55) of the text's
  size in `Theme.machine` (a dim grey, 7.5:1), a terminal in the same cells. While a
  reading types, the model's cursor is hidden; then it waits on the line below. A
  reading leaves the screen with its thought. `Frame.text_rows()` is the model's text
  only (OCR ground truth); `Frame.machine_rows()` the readings.
- **Forgetting is visible.** In a stream life the `forget` copy marked `shown` holds the
  words `forget_grace_seconds` for the reading that reports the loss. When that reading
  quotes a forgotten sentence still on screen (`forgotten: "I am here, inside the…"`),
  the sentence dissolves letter by letter in step with the quote: letter k starts fading
  when the quote's letter k is typed and is gone `dissolve_letter_seconds` later (spans
  of one letter, `kind = "fading"`, through the same grey as any fade, so >= 12:1 until
  gone). The rest of the forgotten words fade once the quote is typed.
- **Darkness is literal.** A `world` event whose `action` is `screen:<N>` (and was
  `performed`) dims the whole screen to N% over `screen_fade_seconds`.
  `Frame.brightness` is the level asked for and `Frame.contrast_floor` the contrast the
  model's text keeps whatever it asks (`contrast_floors`: 7:1 until 27:00 of the life,
  4.5:1 until 29:00, nothing in the last 30 s). Draw every colour with
  `theme.lit(colour, theme.brightness(frame.brightness, frame.contrast_floor))`; the
  floor is taken on the dimmest colour of the model's text (the `forgotten` grey), and
  the brightness moves in 1/64 steps (each a full repaint). Other world losses (services,
  radio, light) are only reported by the readings.
- **The pulse.** At rest between thoughts the cursor blinks twice `cursor_blink_ms` a
  beat (1060 ms). From `pulse_from` (22:00) it quickens smoothly to `pulse_fastest_ms`
  (500 ms) at the expected death (the lifespan from `birth_loading` minus 30 s); in the
  last `pulse_skip_seconds` it skips about one beat in four (`LifeView.skips_beat`).
- **Death.** In a stream life (`birth_loading` `reveal = "stream"`, or `death_style =
  "freeze"`) the `death` event stops the screen where it is, mid-word: letters not typed
  yet die with it. The cursor stays lit and still for `death_still_seconds` (2 s).
- **The vigil** (`silence_style = "vigil"`). Then `Frame.card = ("vigil", [last line,
  "life N · 29:30"])`: the end of the last thought shown, cut to the line at a word start,
  centred and dim (`Theme.machine` at `Frame.card_level`), the small death card typed
  under it `vigil_card_delay_seconds` later. It fades over `vigil_fade_seconds` (75 s) to
  `vigil_floor` and holds there while the next model loads and is born (no birth card),
  until the next life's first reading or word: the genesis, the inventory typed by the
  machine. From the vigil to the next death the screen is never blank for more than 5 s.
- **Snapshots** carry all of it: `machine` (the readings with their turns and fades),
  each dissolving word's `dissolve` and each held word's `forget_in`, `screen` (the
  transition: from, to, since, over), `reveal`, `lifespan_s`, `readings`, `frozen_ago`
  and `vigil` (text, card, ago; it outlives the reset of the next birth).

`tests/display/data/dread-2x1800.jsonl` is two simulated `pi4/default` lives with
readings, world losses and the vigil (`python -m tests.display.dread OUT`), the stand-in
`tests/display/test_dread.py` draws at the four test sizes, in a terminal, on the grid and
through `epitaph replay`.

## The 16-segment theme

`theme = "segment16"` draws the grid layout (6 x 16 by default, like Latent Reflection's
matrix) as amber 16-segment LED cells with a decimal point; unlit segments stay faintly
visible, as on real modules. It implies `layout = "grid"` and `charset = "segment16"`:
upper-case letters, digits and ASCII punctuation, everything else mapped (`é` → `E`, `…` →
`...`, unknown → `?`). The cursor is a lit underscore, the memory gauge a row of lit dashes,
the idle mark a lone decimal point. In a terminal the theme only gives the colours and the
charset. Cells are cached per character and colour, so a typed letter costs one blit.

## Cheap redraws

The Pi has four cores and the creature gets three, so a display must stay well under 5% of
one core. `drive` does not redraw a still screen: it sleeps until `LifeView.next_change`
(the next letter, the end of typing, the next cursor blink), a new event, or at most
0.25 s. Inside a frame, draw only what changed: `screen.py` compares each text row's items
(text, column, colour) with the last frame's and repaints only the changed columns, then
sends just those rectangles to the display. A terminal driver gets the same effect by
diffing cells.

## Watching the Pi from the laptop

`epitaph display --connect <host> --driver terminal` opens `ssh -N -L <free port>:127.0.0.1:7707`
(keys only, `BatchMode`), subscribes through it and draws in the terminal. `<host>` is an SSH
alias; with a comma-separated list, every (re)start of the tunnel tries each in order. When the
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

- `python -m epitaph.display.screenshot --out DIR [--events LIFE.jsonl]`: readability at 800×480,
  1280×720, 1920×1080 and 1080×1920 (OCR ≥ 95%, contrast ≥ 12:1, no split words), on the
  built-in sample and on a recorded life (a simulated `pi4/skeleton-1200` life by default)
  at two moments: a full screen before the first forgetting, and just before death.
- The same command also checks the 16-segment theme at both moments (every cell read back
  from the pixels segment by segment, `screenshot.readability_segments`) and the birth and
  death cards (OCR and the contrast of every line, `screenshot.readability_card`).
- `pytest -m display tests/display`: the same as tests. `tests/display/data/skeleton-1200.jsonl`
  is the recorded fake life (`epitaph sim --profile pi4/skeleton-1200 --hardware pi4-4gb
  --events --seed 0`). `tests/display/data/default-1800.jsonl` is a 30-minute life of the
  earlier reload design (now `pi4/default-reloads`: two reloads that forget, erosion, an OOM
  death), standing in for a real Pi life: `test_full_life.py` draws all of it frame by frame on the plain screen,
  the portrait screen, the 16-segment grid and the terminal, and plays it through `epitaph
  replay`. OCR tests carry the `tesseract` marker: they skip when tesseract is missing,
  except in CI (`CI` set), where they fail instead.
- `epitaph sim --events > life.jsonl`, then `epitaph replay life.jsonl --speed 20 --driver ...`.
- `SDL_VIDEODRIVER=offscreen python -m epitaph.display.bench --full`: the CPU share at
  800×480, 1280×720 and 1920×1080, new drawing against whole-frame painting; scenarios
  `typing`, `fade` (a reload) and `death` (the death fade and the typed card), `--theme
  segment16` for the LED grid. Run it on the Pi pinned to one core (`taskset -c 0`) for the
  numbers that matter. On a laptop at 1280×720 every case stays under 1% of one core.
