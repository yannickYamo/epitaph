# Phase 0b, round 1: agent E (QA and release)

Branch `ws/e-qa`. Cards E1-E3 plus the gate checklist. `make check` is green.

## Built

**E3, `src/epitaph/verify.py`** (`python -m epitaph.verify`; `verify.main(argv)`, plus
`add_arguments`/`run` for the CLI wiring in CONTRACT_CHANGES E1)

- It replays a life's `events.jsonl`: it picks one life from a file that may hold several,
  rebuilds each thought (its words, `gen_start`, `thought_end` and the vitals it saw) and the
  changes the model was told about. It then rebuilds the display timeline: a word types when
  it is released and the previous word, with its pause, has finished (5.12).
- Levels are `smoke`, `skeleton`, `full` (all of 10.3) and `rehearsal` (the 5.11 metrics and
  the thought-count rule, for A's laptop lives). The default is the profile's `verify_level`.
- Every 10.3 row is implemented except the two layout rows. `no_split_words` and
  `bright_words_last_2min` call a `LayoutProbe` (`epitaph.display.layout.verify_probe(cfg)`,
  proposal E8) and report `pending` until D provides it. A pending check never fails a life.
- 5.11 metrics:
  - notice rate per change type: memory trimmed, reload, CPU-share drop, health change,
    persona group removed; "noticed" means one of the next 2 thoughts contains a keyword
    from that type's list
  - reload noticing: the first thought after each reload mentions a loss
  - demise rate after erosion starts
  - specific, not generic: a reading keyword, or a number taken from that thought's reading
  - clichés per 200 words
  - complete sentences and mean sentence length before erosion
  - voice hygiene: helpdesk phrases, answering the readings, thinking tags, the non-Latin
    ratio, plus the banned-phrase and markup/emoji checks at every level
  - distinct 4-gram ratio per thought
  - the thought-count rule on real thought times and reload windows, using costmodel's own
    rule logic (`_check_rules`; proposal E5 makes it public)
- Thresholds come from `[verify]` after the hardware overlay is applied. A test shows the
  Pi 5 overlay changing the birth wpm range. Keyword and cliché lists are read from
  `[verify.keywords]`, `verify.cliches`, `verify.helpdesk_phrases` and
  `verify.answering_phrases`, with documented defaults in `verify.py` until B's lists land.
- Output is `verify.json` (atomic write) next to the events, or `--out`/`--no-write`. Exit
  codes: 0 pass, 1 fail, 2 usage or config error. The target can be a life number (resolved
  in the state dir), a life folder, or a `.jsonl` file (`--life N` picks a life). The next
  life is found in the same file or in the sibling folder `lives/<n+1>` for the `next_birth`
  check. A torn last line (power cut) is ignored.
- The file carries `# pyright: strict` and passes it.

**E1, test harness**

- `tests/conftest.py` keeps `pi4_default` and `state_dir` and adds:
  - `load_cfg`
  - fakes: `fake_clock`, `pi4_costs`, `fake_backend`, `fake_body`
  - `recorder` (an `EventRecorder` that can attach to the bus)
  - `life_builder` (crafted lives)
  - `recorded_life(profile, hardware, lives, seed)`: session-cached; it runs `epitaph sim
    --events` in-process and writes `all.jsonl` plus `lives/<n>/events.jsonl` like a state dir
- Directory auto-markers: `tests/pi` → `pi`, `tests/display` → `display`,
  `tests/templates` → `model`.
- `tests/helpers.py` holds `EventRecorder`, `LifeBuilder` (a life on a hand-moved clock with
  cadence-correct `word` events) and editors: `retext` rewrites what thoughts say while
  keeping their timing, and `replace_first`.
- Directories: `tests/{unit,sim,faults}/__init__.py` (unique module names), and
  `tests/{pi,display,templates}/.gitkeep`. The `.gitkeep` files are empty, so merging with
  C, D or A adding files there cannot conflict.
- `tests/faults/test_fake_faults.py` covers the 10.4 rows the fakes can inject today: crash
  (then verify-life fails `cause` and `duration`, and `next_birth` passes), deadline during
  a reload, full context in a small ctx, and OOM at the squeeze.
- Coverage config: pyproject belongs to L, so it is proposed (E7) and enforced in CI now:
  80% overall and 90% per strict module and `verify.py`, checked file by file.

**E2, `.github/workflows/ci.yml`** (Ubuntu 24.04; Python 3.11, 3.12, 3.13)

- The `make check` parts come first, as separate steps: `make venv`, `lint`, `type`,
  `test`, `timeout 90 make sim`, `make estimate`.
- Then:
  - the coverage floors
  - `estimate` on the sim profile
  - `estimate` on every Pi 5 profile and overlay (informational; see Q5)
  - simulation of every Pi 5 profile and `pi4/unbounded` (hard gate)
  - verify-life on simulated smoke and skeleton lives
  - headless display tests (`SDL_VIDEODRIVER=dummy`, `tesseract-ocr` installed; exit code 5,
    "no tests yet", is a notice)
  - the evidence uploaded as an artifact

**`docs/GATES.md`** turns S0, G0, G1, G2, G3 (acceptance items 1-12), the fault matrix,
every card's "done when" and the 10.3 table into rows. Each row has the command that proves
it, a status and an evidence column. S0 rows carry the read-only probe below.

## Tested

| Command | Result |
|---|---|
| `make check` (worktree) | green: ruff clean, pyright 0 errors, **134 passed**, total coverage 94%, `verify.py` 99%, `make sim` 2 × 67 thoughts (`oom`), `make estimate` PASS on all 5 Pi 4 profiles |
| `python -m epitaph.verify <sim life>` for each Pi 4 profile (2 lives each) | smoke-300: PASS (smoke). skeleton-1200: PASS (skeleton; `no_split_words` pending). compressed-2700 and default (full): every structural check passes (duration, cause, recall, sync, banned, thought-count rule, reload silence 51/36 s, reload count 2/2, reload noticing 2/2, persona groups at death 0). They fail `speed_decline` (0.45, Q6) and, on compressed, `demise_rate` 0.15: the fake backend's canned text, not a pipeline fault. unbounded: duration/cause `full` pass; speed decline skipped |
| Crafted failing lives (tests/unit/test_verify.py, tests/sim/test_verify_sim.py) | each fails its check: banned phrase shown (split by punctuation and case), banned list from config, markup, sync rule (event order, time, and **last letter still typing**), unfinished thought, wrong cause (oom, crash, hang, interrupted), duration, missing death, over-budget recall (1409 of 1280 fails; 1400 passes), slow death display, empty thoughts, no thoughts, typing speed (birth too fast or slow, writing too slow), next birth late, next life without birth, split words and bright words through a probe, thought-count rule (thoughts after erosion removed: rules c and d), reload silence, reload count (and `reload_skipped` accounted), speed decline, persona left at death, notice/reload-noticing/demise/specific, clichés, helpdesk, answering, think tags, non-Latin (ratio, and one sentence under 1% passes), readability, repetition, keyword lists from config, CPU-drop threshold from config |
| CI dry run: every `run:` step of ci.yml (except apt and venv) under bash `-e` with the CI env, in a clean `git archive` copy | all pass. The Pi 5 estimate step exits 1 (`pi5/compressed-600`), as expected and allowed. Display step: "no display tests yet". The per-file coverage loop fails when the floor is raised to 99.5% (negative check) |
| `yaml.safe_load(ci.yml)`; every `make` target used exists in the Makefile | ok (16 steps; targets venv, lint, type, test, sim, estimate) |
| `tools/pi_lock.sh run E 3 -- s0probe.sh` (read-only) | `hostname=epitaph`, `multi-user.target`, controllers `cpuset cpu io memory pids`, passwordless sudo, `get_throttled=0x0`, 0 under-voltage lines in dmesg (uptime 26 min, after A's build job), 42.8 °C, NTP synced, cloud-init disabled, default route `wlan0` metric 600, watchdog 1min |

## Spike numbers

None: E has no spike. The S0 probe above is evidence for GATES.md, not a spike. No Pi
system changes were made, so there is nothing to add to PI_CHANGES.

## Findings for the integrator

1. **Speed decline fails on estimated costs** (Q6). In `pi4-4gb.toml`, step 2 (1.6 tok/s at
   2 threads) is faster than step 0 (1.35 at 3). The simulated default life ends at about
   45% of its starting speed, above the 40% limit. S1b decides.
2. **Birth speed is marginal** (Q7): the median is 48 wpm against a floor of 45 on estimated
   costs. A slower model fails `typing_speed_birth`.
3. **`pi5/compressed-600` fails `estimate`** on both Pi 5 overlays (Q5; B's profile).
4. The simulator emits `death_shown` with an empty `last_line` and `words_total` 0, and
   `gen_end` without `tok_s`/`prompt_n` (E4). verify-life copes with both.

## Left

- The layout-based rows (`no_split_words`, `bright_words_last_2min`) wait on D's
  `verify_probe`.
- The CLI wiring of `epitaph verify-life` waits on L (E1).
- Keyword and cliché lists wait on B. They are tuned after the first rehearsal (5.11: "tuned
  after the first run").
- CI has never run on GitHub (no push). It is validated locally only.
- Most fault matrix rows need the controller (B, P1-P2) or the Pi (C9). GATES.md tracks
  them.
- P1 onwards: E4 (`tools/smoke_pi.sh`, the headless boot test, G1 sign-off), E6 (full level
  on real Pi lives, the fault matrix automated), E7 (`soak_report.py`), E8 (docs).

## Contract proposals (docs/process/CONTRACT_CHANGES.md)

- E1: wire `verify-life` in `cli.py` with `verify.add_arguments`/`verify.run`.
- E2: `birth_loading` carries `profile`, `hardware`, `lifespan_s`.
- E3: every event carries life-clock `t`.
- E4: `gen_end` carries `prompt_n` and `tok_s`; `vitals` carries `reading`.
- E5: `costmodel._check_rules` becomes public `check_rules`.
- E6: `[verify.keywords]` and the verify list and tunable keys.
- E7: pyproject gets `verify.py` in pyright strict and `fail_under = 80`; optional Makefile
  targets `verify-sim` and `coverage`.
- E8: D's `display.layout.verify_probe(cfg)` LayoutProbe.

## Questions (docs/process/QUESTIONS.md, each with the default in use)

- Q2: birth speed is judged on the phase median; writing speed on every thought.
- Q3: a CPU-share drop counts as a change when it is at least 0.25 cores.
- Q4: every thought of 8+ words before erosion must reach the distinct 4-gram ratio.
- Q5: the Pi 5 estimates are informational in CI.
- Q6: the speed-decline finding.
- Q7: the marginal birth speed.
