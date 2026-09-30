# Phase 0b, round 1: agent B (mind)

Branch `ws/b-mind`. Card: B1-B5 (BUILD_PLAN 9 B), 5.4-5.8, 5.12, B10 prep. No Pi or laptop-model
time was needed or used.

## Built

| Task | Module | What |
|---|---|---|
| B1 | `src/epitaph/clock.py` | API kept (sim and costmodel unchanged). Added `VirtualEventLoop` / `VirtualClock` / `run_virtual`: an asyncio loop whose clock is virtual, so `asyncio.sleep`, `wait_for` and timeouts of several concurrent tasks overlap correctly and a life runs in milliseconds; deadlock detection. `charge()` on every clock (contract 6.4). `Schedule.from_profile(cfg, lifespan_s)` (contract 6.4), `keyframe_index`, `next_change`. |
| B2 | `src/epitaph/mind/memory.py` | `Memory`: the recall rule with `trim_to` hysteresis; reload cuts (`cut_for_reload`, reported by the next reading via `take_forgotten`); order of loss (oldest turns, then the oldest kept turn's reading, then its first words); `forget` items `{turn, all}` / `{turn, upto_i}` (inclusive); the fixed marker counted in recall; `fits(ctx, max_tokens)` for `unbounded`; the current reading and thought are never in the past, so never trimmed; messages alternate roles (marker merges with a following reading). Pluggable token counter; external token counts supported. |
| B3 | `src/epitaph/mind/prompt.py`, `config/lang/en.toml` | `Persona`: groups, erosion from the end, the mechanics leave with the last group in one rebuild (`update()` returns the `erosion` payload); `persona_original` / `persona_factual` split into sentence groups; the facts line joins G2. `Reader`: the full, short and minimal forms of 5.4 exactly, health labels, effective cores ("cores 1.4 of 4"), forgotten counts, precision bits from the quant name, change rules (Q1). `render_diary`. `Lang` language packs (strings, health labels, metric lists) with English defaults. |
| B4 | `src/epitaph/mind/sanitize.py`, `mind/words.py` | Streaming sanitizer: thinking blocks, markdown, links, tags, emoji, control and zero-width characters; cut at `[host]` and template tokens; holds back partial constructs so the output is append-only and equals the whole-text result. Word segmenter: whole words, punctuation attached (a lone dash joins the previous word), CJK per character, the final unfinished word from `finish()`; `normalize()` for phrase matching. |
| B5 | `src/epitaph/pacing.py` | `Lookahead` (prefix-aware, cap), `Pacer` (`push`, `finish_thought`, `set_rate_estimate`, `drain`, `cadence`), and `speak()`: one whole thought with generation and typing overlapping, regeneration on a banned opening (at most 2), cuts, the sync rule (returns after the last word is typed; the pause is a minimum before the next first word and overlaps prompt processing), the death flush (`on_died` fires at the real moment, then queued words are shown at pace). Cadence: interval = max(floor, 1/(0.88 r)), r smoothed over 60 s of generation only, bench estimate until 3 s are measured, per-letter jitter seeded per life (`life_seed`), 90/250/700 ms word gaps, hesitations 400-1200 ms before a word or, late in life, inside it. |
| B10 prep | `config/lang/en.toml [metrics]` | Keyword lists per change (memory, reload, cpu, health, persona, demise, specific), clichés, helpdesk phrases. |
| docs | `docs/QUESTIONS.md`, `docs/CONTRACT_CHANGES.md`, `docs/PROMPT_LOG.md` | Q1 answered; questions 2-9; proposals C-B1..C-B7; prompt log round 0 baseline. |

For the P1 controller and `sim.py`: `Persona.from_config`, `Reader.from_config`,
`Pacer.from_config`, `Memory(...)`, `speak(...)`. `tests/sim/test_mind_life.py::live` is a working
sketch of the 5.8 loop on these modules.

## Tested

| Command | Result |
|---|---|
| `make check` (ruff, format, pyright strict on mind/ clock pacing, pytest + coverage, sim, estimate on every Pi 4 profile) | **Pass.** 322 tests in 11 s; all 5 Pi 4 profiles PASS the thought-count rule |
| Coverage | `clock.py` 99%, `mind/memory.py` 100%, `mind/prompt.py` 100%, `mind/sanitize.py` 98%, `mind/words.py` 98%, `pacing.py` 99%; total 94% |
| `tests/unit/test_clock.py` (118) | Every keyframe of every Pi 4, Pi 5 and sim profile, nominal and rescaled to 20/30/45/75/120 min where valid: stepped fields exact at t and at t-ε, interpolated fields, recall and CPU share held before reloads, death squeeze edge, change times. Rescale keeps end anchors; an invalid rescale is rejected. Virtual loop: overlap, timeouts, deadlock, threads |
| `tests/unit/test_words_sanitize.py` (53) | 16 sanitizer cases; streaming equals whole-text for 300 random chunkings each, zero divergences; `[host]` and `<think>` split across chunks never leak; segmentation incl. Unicode and CJK; streaming segmentation equals whole |
| `tests/unit/test_memory.py` (15) | Hysteresis, word-level trims with `upto_i`, never trimming the pending reading, reload cut reporting, marker, alternation, fits, external counts |
| `tests/unit/test_prompt.py` (33) | Golden system text at every erosion step (5..0); erosion events follow the schedule (4,3,2,1 with mechanics; 0 without); the four 5.4 example readings reproduced exactly; Q1, cores, speed rules; original/factual splitting; facts line; diary; a second language pack |
| `tests/unit/test_pacing.py` (29) | Every B5 case: banned phrase split across chunks and punctuation; unresolved prefix at the end; banned phrase in the tail at death; "How" + ordinary word released within one word; same seed same cadence; no bursts at ±30% jitter; wpm in the overlay range at every keyframe of three profiles; no `gen_start` before the previous thought is shown. Plus regeneration, `[host]` cut, cap, death flush at pace, death before any word, cancellation |
| `tests/sim/test_mind_life.py` (15) | Whole lives of `pi4/default`, `compressed-2700`, `skeleton-1200` on the fakes: recall never exceeded at a reading, forgetting oldest first and once, marker, 5 erosion steps, sync rule over the life, OOM death with the flush before `death_shown`, the first reading after each reload reports the loss |

**Numbers** (fake backend, estimated Pi 4 costs, English text at 4.2 characters per token):

- Typing speed per keyframe, `pi4/default`: 0:00 49 wpm, 12:00 49, 22:00 49, 28:00 47, 36:00 47, 43:00 54, 49:00 47, 51:00 38, 53:00 31, 55:00 27, 57:00 21 (overlay: 45-180 at birth, 8-220 while writing). `compressed-2700` matches.
- Generation jitter ±30%: 2.9 s of starvation in 405 s of typing over 5 thoughts, longest stall 0.57 s.

## Left

- Controller (P1, B6): the loop, deadline everywhere, `vitals` events, `thought_start/end`, and a cap on the death flush (`max_death_display_delay_s`: `speak` flushes at pace with no limit today).
- `sim.py` still uses its own reading and word code; switching it to these modules is natural once the controller lands (the `live()` sketch in `tests/sim/test_mind_life.py` shows how).
- Token counting uses `approx_tokens` until A7 (`count_past_tokens`); `Memory` accepts real counts per message. Diary mode should pass a counter without the per-message overhead.
- Metric word lists are a first draft; tuned in P0c on real transcripts.

## Contract proposals (docs/CONTRACT_CHANGES.md)

C-B1 `BannedHit`/`PushResult` into `types.py`; C-B2 language packs in `config/lang/`; C-B3 new optional keys (`readings_*_step`, `min_rate_sample_s`, `hesitation_inside_from`); C-B4 cost model: reload cut to `recall × trim_to`, marker counted; C-B5 `word.char_ms` is per character of `text`, hesitation delays the event, `upto_i` inclusive; C-B6 fake backend typed on a clock protocol; C-B7 the pacer rate is non-space letters per second (estimate ≈ tok/s × 3.4).

## Questions (docs/QUESTIONS.md)

1 answered. New with defaults in use: 2 CPU share eases from reload 2 rather than from erosion (kept); 3 `upto_i` inclusive; 4 the marker counts against recall; 5 reload cut to `recall × trim_to`; 6 order of `persona_original` groups (kept); 7 a banned opening after 2 regenerations cuts to an empty thought; 8 a truncated last word completing a banned phrase is cut; 9 birth typing is about 49 wpm against a 45 floor at the estimated rate.
