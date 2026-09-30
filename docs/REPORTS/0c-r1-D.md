# Phase 0c, round 1: part D (display)

Branch `ws/d-display`. Card: E8 (the verify-life layout probe), D5 (fade what a reload
forgets), D7 (partial redraw under 5% of a core; `birth_card_seconds`, `death_card_seconds`).

## Built

| Item | Files | Notes |
|---|---|---|
| E8 layout probe | `display/layout.py` (`verify_probe`, `VerifyProbe`, `life_times`) | `verify_probe(cfg)` returns an object that matches `verify.LayoutProbe`. `split_words(events)` wraps every thought at `line_chars` (flow) or the grid's columns with its charset map (grid) and counts the words that break across lines. `bright_words_last(events, seconds)` replays the life through a `LifeView` on the life clock `t`, with no catch-up and no history bound, and returns the largest number of bright words at any one moment in the window. The count can only rise when a word starts and only fall at a `forget`, so it is sampled at the window edges, at each word start and just before each event. The window ends when typing stops after death. `birth_loading.t` (stale in the simulator, D6) is treated as 0. With the probe in place, `epitaph verify-life` runs `no_split_words` and `bright_words_last_2min` for real; neither is pending any more |
| D5 fading at a reload | `display/layout.py`, tests | A `forget` during a reload fades over `fade_seconds` under the reload dimming, keeps fading after `reload_done`, and survives a snapshot taken mid-reload. The layout already handled it; the tests now check it. `ViewSettings.from_config` reads `fade_seconds`, and so does the probe |
| D7 config | `display/layout.py` | `ViewSettings.from_config` reads `birth_card_seconds` and `death_card_seconds` |
| D7 partial redraw | `display/screen.py` | Each frame is reduced to the items in each text row (text, column, colour) plus the status text. When the scene is unchanged (no card, dark or dimming change), only rows whose items changed are repainted, and only the columns those items cover. Each row is clipped to its own band. Only the status text's rectangle is repainted, and only the dirty rectangles go to `display.update`. The screen is flipped whole only on a scene change. Fades are drawn in 32 colour steps (`FADE_STEPS`), and rows are compared by colour rather than fade progress, so a fading row repaints about 4 times a second instead of 30. Portrait rotation rotates only the dirty pieces |
| D7 draw only when due | `display/app.py`, `display/layout.py` | `drive` sleeps until `LifeView.next_change(now)` (the next letter, the end of typing, the next cursor blink), until an event arrives (it is woken), or for at most `max_idle_s = 0.25` (fades, the status clock, cards). It still never draws faster than `fps`. `next_frame(view, now, fps, max_idle_s)` is shared with the bench. The terminal driver gets the same savings |
| D7 cheaper compose | `display/layout.py` | Each `Thought` caches its wrapping (`laid`, keyed by the ids of the words shown), and a frame stacks the cached pieces. Before this, every visible thought was wrapped twice per frame. `ViewWord.typing_s` is cached as well |
| D7 bench | `display/bench.py` | `python -m epitaph.display.bench [--full] [--sizes ...] [--char-ms N] [--layout grid] [--budget X]` runs `typing` (a full screen, then a thought typed at `char_ms` per letter) and `fade` (every earlier thought forgotten at once, as at a reload) on a virtual clock, with frames scheduled as `drive` schedules them. It reports `core_share` and `present_share` (the CPU spent inside `display.update`/`flip`, which belongs to the video driver). `--full` also measures the old drawing: every frame composed at 30 fps, and every changed frame painted and flipped whole |
| Docs | `docs/WRITING_A_DISPLAY.md`, `docs/QUESTIONS.md` (D 9-11), `docs/CONTRACT_CHANGES.md` (D8) | Cheap redraws, the probe, the bench |
| Tests | `tests/display/test_verify_probe.py` (new, 16), `test_screen.py` (+6), `test_layout.py` (+1) | See below |

## Tested (commands and results)

- `make check` (final, in the worktree): ruff, format and D1 docstrings clean. pyright 0 errors. **591 passed**, 5 deselected. Total coverage 95%; `display/layout.py` 99%, `display/bench.py` 99%. `epitaph sim` passes, and `estimate` passes on every Pi 4 profile.
- **Probe on crafted events** (`test_verify_probe.py`):
  - `split_words`: only words longer than a line are counted, and exactly `cols` letters fits. The grid charset map is applied: at 16 columns, `…` → `...` turns a 15-letter word into 17 letters and it splits.
  - `bright_words_last`: peaks inside the window are found, as are words typed after a `forget`. Typing that continues past death extends the window. Fading words do not count. A life with no death ends at its last event.
  - `life_times`: a stale `birth_loading.t` is ignored; `ts` is used when `t` is missing; time never goes backwards.
- **Probe on simulated lives** (`epitaph.sim.simulate`, pi4/default and pi4/compressed-2700, over 500 words each). `no_split_words` passes with 0 split words.
  - With the reload `forget` added (a test helper replays the simulator's memory and forgets down to `recall_after`, which is what contract D5 asks `sim.py` to emit), `bright_words_last_2min` passes on both profiles with **10 bright words** (limit 40).
  - The simulator as it stands on main gives **314** (pi4/default) and **278** (compressed-2700), because nothing kept through a reload ever fades. The test checks both numbers, and the second assertion switches off by itself once the simulator emits the event.
- **The command:** `epitaph verify-life <sim life> --profile pi4/compressed-2700 --hardware pi4-4gb` prints `PASS no_split_words 0` and `FAIL bright_words_last_2min 278 (limit 40)` on today's simulator output. With the reload forgets added, both checks pass (tested through `verify.main`). A skeleton life (`pi4/skeleton-1200`) passes, with `no_split_words 0`.
- **D5:** a forget at a reload makes the forgotten words `fading` with `fade = 0.5` 3 s into a 6 s fade, dimmed and no longer bright. They are `forgotten` at 6 s, still fading after `reload_done` (5/6 at 5 s), and a mid-reload snapshot restores them as fading and dimmed.
- **Partial redraw is pixel-exact:** a scripted life goes through birth, three thoughts that scroll, a forget and fade, a reload with dimming, `reload_done`, new vitals, death, the death card and the dark silence. It is played at 30 fps in flow (640×360), portrait-rotated flow (800×480) and grid (640×240, ascii). After every partial repaint, the window's pixels are compared byte for byte with a full repaint of the same moment: they are identical, with over 100 partial frames per layout. This test caught a real bug: in a small grid the font is taller than the row, so glyphs bled into rows that a partial repaint did not clear. Every row is now clipped to its own band.
- A single typed letter at 1280×720 dirties one rectangle less than a quarter of the width and less than one row high (plus the status text when the clock changes). An 8 s fade repaints between 16 and 34 times.
- **CPU on the Pi 4** (the bench under `tools/pi_lock.sh run D 10`, over the cable because the mDNS name did not resolve, pinned to core 0 with `taskset -c 0`, system pygame 2.6.1, offscreen, 20 s per case). The Pi was idle (load 0.00) at 33-37 °C with `throttled=0x0` before and after, and the files were removed afterwards. The Pi 4's real typing cadence is about 180-200 ms per letter: Qwen3 1.7B generates 1.65-1.84 tok/s, which is about 6 letters/s typed at 88%. 60 ms is the fastest the cadence can go.

  | Size | Case | Old: whole frames at 30 fps | New | of which `display.update` |
  |---|---|---|---|---|
  | 800×480 | typing, 200 ms/letter | 3.7% | **1.0%** | 0.4% |
  | 1280×720 | typing, 200 ms/letter | 6.1% | **1.7%** | 0.8% |
  | 1920×1080 | typing, 200 ms/letter | 9.2% | **2.6%** | 1.7% |
  | 1280×720 | reload fade, 200 ms/letter | 8.1% | **2.5%** | 1.2% |
  | 1920×1080 | reload fade, 200 ms/letter | 12.7% | **4.5%** | 2.9% |
  | 800×480 | typing, 60 ms/letter | 6.7% | **2.3%** | 0.9% |
  | 1280×720 | typing, 60 ms/letter | 11.4% | **3.8%** | 1.8% |
  | 1920×1080 | typing, 60 ms/letter | 20.2% | **6.0%** | 3.9% |
  | 1920×1080 | reload fade, 60 ms/letter | 22.6% | 8.4% | 5.2% |
  | 1080×1920 | typing, 60 ms/letter | | 7.0% | 3.0% |
  | 1280×720 grid | typing, 60 ms/letter | | 3.6% | 2.1% |

  The target of under 5% of one core is **met at 1280×720 and 1920×1080 at the Pi 4's cadence**, including during a reload fade (a few seconds per reload). It is also met at 1280×720 at the fastest cadence. At 1920×1080 and the fastest cadence the screen takes 6.0%. Most of that (3.9%) is spent inside `display.update` on SDL's offscreen driver, which charges a fixed cost per call. Our own drawing is 2.1%.
- Laptop, same bench (a busy laptop, so these vary by about 2×): 1920×1080 typing at 60 ms went from 13.7% (old) to about 1-3% (new).
- The whole `tests/display` suite (114 tests) passes, including D13 with tesseract at all four sizes.

## Numbers

- Bright words in the last 2 minutes of a simulated life: 10 with reload forgets, 314 (pi4/default) or 278 (compressed-2700) without them.
- Split words in simulated pi4/default, compressed-2700 and skeleton-1200 lives at 48 columns: 0.
- Pi 4 screen CPU: see the table. The cut is 3.5× at 720p and 1080p at the real cadence.
- Cost per painted frame at 1920×1080 on the Pi: 6.2-6.7 ms new, against 20-22 ms before.

## Pi changes

None. Three bench jobs held the Pi lock (8-10 min each, finished in under 2 min). Each copied the display modules and font to `/tmp/epitaph-d`, ran the bench and deleted the folder. Nothing was installed. One further lock hold was a reachability check (`uptime`).

## Left

- **S5 (pending until a screen is connected):** `display.update(rects)` on KMSDRM. SDL2 may upload the whole window texture on every update, whatever the rectangles, so the present cost on a real screen could be higher or lower than offscreen. The bench's `present_share` is the number to watch there: rerun `python -m epitaph.display.bench --full` on the Pi with the panel connected.
- Once `sim.py` emits a `forget` at each reload (B, this round), the helper `with_reload_forgets` in `tests/display/test_verify_probe.py` becomes a no-op and can be removed, and E can switch the full-life test to the real probe (proposal D8).
- Still left from 0b: `cards.py` as its own module, the idle silence style, the segment16 renderer, `tty.py`, and screenshots on `screenshot_on` events (needs the controller).
- The terminal driver draws only when something is due (through `drive`) but still repaints whole frames. Its cell diff already keeps SSH traffic small; it has not been benchmarked for CPU.

## Contract proposals (docs/CONTRACT_CHANGES.md)

1. **D8:** once the simulator emits the reload `forget` (D5), E's verify-life sim tests use `default_layout_probe(cfg)` and expect `bright_words_last_2min` and `no_split_words` to pass.

No event or config changes. `birth_card_seconds` and `death_card_seconds` were already in `default.toml` (decision D7) and are now read.

## Questions (docs/QUESTIONS.md, each with the default in use)

9. `no_split_words` is judged at `line_chars` (48) for flow, or the grid's columns; the real panel's width is unknown until S5.
10. The window for `bright_words_last_2min` ends when typing stops after death. A word is bright from its first letter until a `forget` reaches it, and the check reports the peak.
11. Fades are drawn in 32 colour steps (about 4 repaints a second). This is a rendering constant, not config.
