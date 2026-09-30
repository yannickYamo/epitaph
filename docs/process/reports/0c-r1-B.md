# Phase 0c, round 1: agent B (mind and profiles)

Branch `ws/b-mind`. Card: decision A3 in `mind/memory.py` and `mind/prompt.py`; the Pi 4 profiles
rebased on measured costs; `sim.py` on the real mind (D5, E2/E3/D2/D6); the cost model. One
laptop llama-server job (under `tools/laptop_lock.sh`, about 5 minutes); no Pi time.

## Built

| What | Where | Notes |
|---|---|---|
| Decision A3: the marker | `src/epitaph/mind/memory.py` | `[host] earlier memory lost` is the first line of the reading after a loss (same user message) and belongs to it (`Memory.gap_turn`). When a trim takes that reading, the marker leaves with it and rides on the next new reading. It is never new text in front of kept turns |
| Trims on turn boundaries | `src/epitaph/mind/memory.py` | Found while testing A3: a live trim that cut words inside a kept thought put new tokens in front of everything after it, the same cache break as the old marker. `fit` now cuts whole turns (or only the oldest reading) while more than one turn is kept, and words only inside the last one; `cut_for_reload` still cuts to the word (the new server reads everything anyway) |
| The system prompt rebuilt | `src/epitaph/mind/prompt.py` | `Persona.system_text`: one paragraph per kept group, then the mechanics, the layout every spike measured. An erosion step removes whole paragraphs |
| Measured costs adopted | `bench/pi4-qwen3-1.7b-*.json`, `config/default.toml` | The five measured files copied from `bench/measured/`; S1c's late speed (`tg_tok_s_late = 1.424`, `late_after_s = 1800`) added to the step-0 file. `life.models = ["qwen3-1.7b"]` (provisional; my one edit to `default.toml`) |
| Cost model | `src/epitaph/costmodel.py` | Prompt threads from `backend.threads_batch` (limited by the share); generation eases to the S1c late speed over 30 min; the system prompt sized from the persona text per erosion step; the first marker costs only its tokens (A3). New failures: `silence` (reload silence over `verify.max_reload_silence_s`) and `speed` (full-level profiles: last 5 min at or above 40% of the first 5) |
| Profiles | `config/profiles/pi4/{default,compressed-2700}.toml`, `pi5/default.toml` | Recall 300 after reload 1 and 260 at the decline; the thread drop moved from reload 1 to reload 2 (reload 1 keeps 3 threads and share 3.0); end CPU share 1.3, 0.9, 0.6, 0.4. Reloads, erosion order and times, death at end-0:30 and decision 30's cadence unchanged. `pi5/default` gets the same end slope |
| Simulator | `src/epitaph/sim.py` | Runs on `Memory`, `Persona` and `Reader`: real prompts to the fake backend, real readings, trims, reload cuts, marker and erosion. `t` on every event (0 before birth); `birth_loading` carries `profile`, `hardware`, `lifespan_s`; every cut emits `forget`, the reload's included; `gen_end` carries `prompt_n`, `tok_s`. The body's kills are timed (death squeeze and deadline land even in prompt processing); a server-side full context ends as `full`; the fake uses the configured reuse chunk. Public API unchanged |
| Docs | `docs/PROFILES.md` (new), `docs/PROMPT_LOG.md`, `docs/process/QUESTIONS.md` B 10-13, `docs/process/CONTRACT_CHANGES.md` C-B8..C-B13 | Every profile change with the estimate before and after |

## Tested

| Command | Result |
|---|---|
| `make check` | **Pass**: ruff, format, pyright strict (0 errors), 586 tests, coverage 95%; `costmodel.py` 98%, `mind/memory.py` 100%, `mind/prompt.py` 100%, `sim.py` 99%; the 60-min sim life (42 thoughts, `oom`, twice); `epitaph estimate` PASS on all five Pi 4 profiles with measured costs |
| `tests/unit/test_memory.py` (19) | Marker on the first reading after a loss; kept turns render byte-identical to what the server read; the marker stays with its turn while older turns go, leaves with its reading and returns on the next; a trim with a reading waiting; live trims on turn boundaries; words cut only in the last turn; reload cuts to the word |
| `tests/unit/test_prompt.py` (35) | Golden text at every erosion step in the paragraph layout; each step leaves the rest of the prompt byte-identical |
| `tests/sim/test_mind_life.py` (18) | New: over whole `pi4/default`, `compressed-2700`, `skeleton-1200` lives on the fake backend's cache model, no request after birth or a reload re-reads more than 25% of its prompt (or 64 tokens), the first marker included |
| `tests/sim/test_sim.py` (14) | New: event conventions (E2/E3/D2/D6/E4), `forget` right after every reload (D5), readings from the mind, a server-side full context |
| `tests/unit/test_costmodel.py` (16) | Every Pi 4 profile passes on bench (measured) costs; prompt threads; the drift; silence and speed failures; the marker costs no thought; the persona fallback |
| Real server, laptop (`tools/laptop_lock.sh run B 20 -- …`, Qwen3 1.7B Q8_0, `--cache-reuse 32`, scratch script driving `Memory` + `Persona` through `LlamaServerBackend`) | Normal turn 54-55 tokens re-read; first trim with the marker 62 / 963 (6%); trims while the marker's turn is kept 55; trims that move the marker 62; erosion steps 55. Every edit re-reads only the new reading |
| verify-life on simulated lives | `pi4/default`: `speed_decline` 0.33 pass, `reload_silence` 128 s / 149 s pass, thought-count rule, recall budget, sync rule, duration, cause, persona at death, typing speeds pass. `compressed-2700` the same (0.30; 127 s / 151 s) |

## Numbers

Estimates (measured Qwen3 1.7B costs, new cost model):

| Profile | Before (0b profile) | After |
|---|---|---|
| `pi4/default` | FAIL: 40 thoughts; silences 219 s, 154 s; speed 0.52 | PASS: 40 thoughts; silences 128 s, 153 s; speed 0.32 |
| `pi4/compressed-2700` | FAIL: 32 thoughts; silences 218 s, 154 s; speed 0.51 | PASS: 32 thoughts; silences 128 s, 154 s; speed 0.32 |
| `pi4/skeleton-1200` | PASS: 15 | PASS: 15 |
| `pi4/smoke-300` | PASS: 5 | PASS: 5 |
| `pi4/unbounded` | PASS: 42, full at 53.6 min | PASS: 42, full at 53.6 min |

Rule margins, `pi4/default` (thoughts per window): (a) 8, 10, 5, 5, 5, 7 (need 3); (b) 4, 3 (need
2); (c) 1, 2, 1, 2, 2 (need 1); (d) 8 (need 4). `compressed-2700`: (c) 2, 2, 2, 2, 3. The thinnest
margin is rule (c) in the 60-minute life's 2-minute erosion windows.

## Left

- The rehearsal (stages 1 and 2) and the keyword and cliché lists on real transcripts (B10 with
  A): not started this round; no voice runs.
- S3c at 0.5 and 0.4 cores (C): the profiles now go below the measured 0.7.
- The P1 controller must follow what the sim now does: forget at the reload, `t` everywhere,
  `birth_loading` fields, timed kills.
- `pi5/skeleton-600` and `pi5/compressed-600` still fail their estimates (as before this round;
  informational in CI).
- The real-server A3 script lives in my scratch space; C-B13 proposes it as a template test (A).

## Contract proposals (docs/process/CONTRACT_CHANGES.md)

- **C-B8** validation: `cpu_share` up to `max(threads, threads_batch)` (optional; would let the
  thread drop return to reload 1).
- **C-B9** bench files: optional `tg_tok_s_late`, `late_after_s`; `tg_tok_s_birth` used as the
  short-context speed.
- **C-B10** `estimate` may report rules `silence` and `speed` besides (a)-(d); `check_rules`
  unchanged.
- **C-B11** BUILD_PLAN 5.3/5.4 text: marker placement, turn-boundary trims, the new Pi 4 table.
- **C-B12** `[estimate]` system-token guesses become a fallback; `reading_tokens.full` 45 -> 55.
- **C-B13** a real-server A3 regression test in `tests/templates`.

## Questions (docs/process/QUESTIONS.md)

- B 10: thread drop moved to reload 2 because validation caps the share at `threads` (default in
  use: moved).
- B 11: end CPU share 0.6 and 0.4, below S3c's 0.7 (default: kept; S3c extension asked).
- B 12: live trims on turn boundaries instead of words inside the oldest kept turn (default:
  implemented).
- B 13: where the marker goes after its reading is forgotten (default: the next reading).

Also touched outside my paths, as the card allowed: `config/default.toml` (`life.models` only),
`src/epitaph/sim.py`, `src/epitaph/costmodel.py`; and, to keep `make check` green after the
measured costs, tests I do not own: `tests/unit/test_schedule.py` (profile values),
`tests/sim/test_verify_sim.py` (`test_sim_findings`: speed decline now passes),
`tests/faults/test_fake_faults.py` (the slow-reload test sets the load on the costs, since bench
values override overlay values).
