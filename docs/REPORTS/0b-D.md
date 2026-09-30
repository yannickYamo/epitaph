# Phase 0b, round 1: part D (display)

Branch `ws/d-display`. Card: BUILD_PLAN 9 D1-D4, D5 started; 5.12, 6.3.

## Built

| Item | Files | Notes |
|---|---|---|
| D1 layout | `src/epitaph/display/layout.py` | Pure Python, fed by events, time passed in. `LifeView`: thoughts and words, the typing timeline from `char_ms` / `pause_after_ms` / `hesitate_before_ms`, word states live / fading / forgotten / inherited, cursor (solid while typing, blinking in pauses, dim in a reload, gone at death), birth and death cards, silence styles, exhibit dark, vitals, memory gauge, status strip, bright-word count, backlog catch-up, bounded history, `snapshot()`. `flow_metrics` (font from `line_chars` and `min_font_px`, any resolution), `derive_grid` (grid shrunk for small panels), `map_charset` (unicode, ascii, segment16), `flow_lines` (whole-word wrap; a word is placed at full length before its first letter), `compose_flow` (newest at the bottom, blank line between thoughts, fading) and `compose_grid` (live words only, gauge row). Only the thoughts that can reach the screen are laid out |
| Themes and font | `display/themes/{__init__,plain,segment16}.py`, `assets/fonts/` | IBM Plex Mono Regular 1.1.0 with `OFL.txt` and a README (source, sha256). WCAG contrast helpers |
| D2 terminal | `display/terminal.py` | ANSI only (alternate screen, hidden cursor, restored on exit). Cell buffer with diffs, so a letter costs a few bytes over SSH. 24-bit colour or the 256-colour grey ramp. Flow or grid |
| Driver loop | `display/app.py` | `drive(driver, source)`: events feed the view while a render loop redraws at a fixed rate. `make_driver`, `display_config` (default.toml + overlay, no profile needed) |
| D3 remote view | `display/remote.py` | `main(argv)` for `epitaph display [--connect HOST]`: `ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=5 -o BatchMode=yes -L <free port>:127.0.0.1:7707 HOST`; reconnects with backoff, restarts ssh, redraws from the snapshot; "reconnecting" in the status strip. `--screen-present` (D6 ExecCondition) reads `/sys/class/drm/*/status`. `driver = auto`: terminal for a remote view, screen when a local screen is connected |
| D3 replay | `display/replay.py` | `main(argv)` for `epitaph replay LIFE --speed --from mm:ss --driver terminal\|screen\|none --port`: a life number, folder or `events.jsonl`; original cadence from the life clock `t` (else `ts`), divided by the speed, word typing scaled too; `--from` folds earlier events into one snapshot; also serves a local EventBus |
| D4 screen | `display/screen.py` | pygame: window (x11/wayland on the laptop), full screen on a console (KMSDRM), offscreen. Letter-by-letter typing, block cursor, fades, reload dimming, status strip, birth and death cards, grid with a gauge bar, portrait rotation. Unchanged frames are neither painted nor flipped |
| D5 (started) | `display/screenshot.py` | `render_png`, `ocr_words` (tesseract), `word_accuracy`, `measure_contrast` (from the pixels), `readability`; `python -m epitaph.display.screenshot --out DIR` runs D13 at the four sizes |
| Docs | `docs/WRITING_A_DISPLAY.md`, `docs/CONTRACT_CHANGES.md` (D1-D7), `docs/QUESTIONS.md` (2-8) | |
| Tests | `tests/display/` (5 test files and a conftest, 91 tests) | layout, terminal, remote, replay, screen and D13 |

## Tested (commands and results)

- `make check` (final, in the worktree): ruff and format clean; pyright 0 errors; **150 passed**; total coverage 94%. Display modules: layout 99%, app 99%, replay 99%, screenshot 99%, themes 98%, terminal 96%, remote 93% (screen.py is excluded from coverage by pyproject, but tested). `epitaph sim` and `estimate` on every Pi 4 profile pass.
- **D13 readability** (`python -m epitaph.display.screenshot`, plain theme, offscreen):

  | Size | cols × rows | Words shown | OCR accuracy | Contrast (pixels) | Split words |
  |---|---|---|---|---|---|
  | 800×480 | 32 × 7 (font 36 px, the floor) | 32 | 1.00 | 16.89:1 | 0 |
  | 1280×720 | 48 × 10 (40 px) | 53 | 1.00 | 16.89:1 | 0 |
  | 1920×1080 | 48 × 10 (60 px) | 53 | 1.00 | 16.89:1 | 0 |
  | 1080×1920 | 44 × 33 (36 px) | 138 | 1.00 | 16.89:1 | 0 |

  Fading text stays at least 4.5:1 at every step (test); forgotten grey 4.7:1; status strip 6.8:1; dimmed (reload) about 4:1.
- **Whole words:** a randomised test (4 widths × 30 layouts) finds no split word that fits a line; a whole simulated pi4/default life (2,160 words) at 48 columns gives 0 split words.
- **Against `epitaph sim --events` output:** the view, the terminal driver and replay run a whole simulated life. Replay keeps the cadence: total wait = life span / speed; the `--from 20:00` snapshot redraws exactly the screen a full play shows at that moment (test).
- **Against a local EventBus:** replay publishes to a bus and a subscriber gets the snapshot, then every event in order. Reconnect test: the bus stops and restarts on the same port; the view reports down/up and redraws from the new snapshot. The fault-matrix row "redraw within 5 s of reconnect" is a test.
- **Tunnel with a stand-in for ssh** (a local TCP forwarder): the tunnel starts, forwards and restarts after being killed. An ssh failure (stderr) and a tunnel that never gets ready are both reported.
- **Real SSH tunnel to the Pi** (`tools/pi_lock.sh run D 5 -- …`): the real `EventBus` and `replay.republish` ran on the Pi (Python 3.13, files copied to `/tmp`, then removed), replaying a simulated life at 5×. The laptop ran `remote.Tunnel("pi")` + `reconnecting` + `TerminalDriver`. After 12 s I killed the ssh tunnel: **the view came back with a fresh ssh and redrew from a snapshot 1.01 s later** (51 words before, 76 at the end, 2 tunnel starts). `throttled=0x0`.
- **Real pty:** `script -c "… replay sim.jsonl --speed 40"` at 80×24 in truecolor. Rebuilding the screen from the ANSI stream shows the status strip, whole-word lines and the blank line between thoughts; alternate screen and cursor were restored (1 on / 1 off each).
- **Laptop window:** pygame chose the `x11` video driver; 960×540 window, a replay at 20×, about 26 fps. Screenshot checked by eye.
- **Pi rendering bench** (offscreen pygame on the Pi, system `python3-pygame` 2.6.1, pinned to core 0, `throttled=0x0`, 45 °C). Full paint = CPU per frame when every frame changes; typing = 10 s at 30 fps while a thought is typed at 60 ms a letter (only changed frames are painted: 59 of 300):

  | Size | Full paint, CPU ms | Typing, share of one core |
  |---|---|---|
  | 800×480 | 5.1 | 4.7% |
  | 1280×720 | 9.8 | 8.5% |
  | 1920×1080 | 19.0 | 13.8% |

  Laptop, same bench: 3.9% / 3.9% / 11.6%. D7's target (under 5% of a core) is met at 800×480; larger screens need dirty rectangles (D7, P2).
- Laptop layout cost: `compose_flow` with a 1,475-word history went from 2.07 ms to 0.37 ms a frame (visible thoughts only; a regression test checks the result against a full layout).

## Spike numbers

S5 is pending until a screen is connected. The Pi reports both HDMI connectors `disconnected`, so `--screen-present` would exit 1. Display-path numbers from this round: tunnel redraw after a kill 1.01 s; the Pi CPU table above.

## Pi changes

None. Two short jobs under the Pi lock copied files to `/tmp/epitaph-d` and removed them. They ran read-only checks (`apt-cache policy python3-pygame`: 2.6.1 installed; `/sys/class/drm` statuses) and nothing was installed, so there is no entry in `PI_CHANGES.md`. I also ran one read-only `ssh pi 'python3 --version; uptime'` without the lock before I had read `lock.sh`.

## Left

- **D5 (P1):** screenshots on `screenshot_on` events into `lives/<n>/screenshots/`, and `epitaph ctl screenshot` wiring (it needs the controller). D13 is also ready to run in CI (tesseract), which is E's job.
- **D6 (P1):** the systemd `ExecCondition` itself belongs to C. `--screen-present` is done.
- **D7 (P2):** `cards.py` as its own module (cards are basic and live in `LifeView.card` today), the idle silence style, the segment16 renderer (colours only for now), dirty rectangles for CPU under 5% at 720p and up.
- **D8:** S5 on a physical screen (KMSDRM, blanking, panel power).
- `tty.py` (the Linux console without pygame) is not started.
- The cli.py wiring (proposal D1) is for L at merge. Until then, run `python -c 'from epitaph.display.replay import main; main([...])'`, or the same with `remote.main`.

## Contract proposals (docs/CONTRACT_CHANGES.md)

1. **D1:** wire `epitaph display` → `epitaph.display.remote.main(argv[1:])` and `epitaph replay` → `epitaph.display.replay.main(argv[1:])` (each parses its own flags).
2. **D2:** `t` (life clock seconds) on every event.
3. **D3:** a fixed snapshot shape (`mode`, `open_turn`, `memory`, …). Offer: the controller builds snapshots with `LifeView` via `bus.add_local`, so the snapshot always matches the screen.
4. **D4:** `word` gets optional `hesitate_before_ms` and `state: "inherited"`.
5. **D5 (bug in `sim.py`, matters for B's controller):** the reload memory cut emits no `forget`. The display cannot fade the life's biggest loss, and in a simulated pi4/default life 285 words are still live at death, so verify-life's "bright words ≤ 40" check would fail.
6. **D6:** `sim.py` stamps `birth_loading.t` with the time since the previous life's clock start.
7. **D7:** `[display] birth_card_seconds = 4`, `death_card_seconds = 8`.

## Questions (docs/QUESTIONS.md, each with the default in use)

2 death card timing (8 s after the last letter, then the silence style) · 3 what "bright words" means for verify-life (`LifeView.bright_words`: typed and still live, independent of screen size) · 4 an empty screen fills from the bottom · 5 status strip at the top, 45% size, dropped whole parts · 6 grid: forgotten words vanish, gauge row · 7 OCR preprocessing (inverted grey, `--psm 6`) · 8 orientation (only an explicit `portrait` on a landscape panel rotates).
