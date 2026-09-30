# Phase 0c, round 2: agent V (voice: rehearsal and prompt tuning, cards A3 and B10)

Branch `ws/v-voice`. Rehearsal outputs are in `voice/` (untracked; round-2 lives in
`voice/round2/`, the run index in `voice/rehearsal_report.md`). No Pi time was used; every
llama-server job ran under `tools/laptop_lock.sh`. Nothing was pushed.

## Headline for the integrator

1. **Checkpoint A packet: `docs/CHECKPOINT_A.md`** with three transcripts in
   `docs/checkpoint_a/`. Recommendation: **Qwen3 4B Instruct 2507** and **Llama 3.2 1B**,
   **Yannick's original persona**, **chat mode**. The plan's no-reply default (the two
   best-scoring models, v6 persona, chat) would give Qwen3 1.7B and Qwen3 4B.
2. **The voice and the speed gate point in opposite directions.** Only Qwen3 4B holds a voice
   for the hour. Only the 1-2B models pass the thought-count rule on today's `pi4/default`.
   Qwen3 4B on its measured ladder (agent A) gets 17 thoughts and 7-minute reload silences on
   this schedule; with agent A's slot handover (F5, S4b: 73 s for a 3B reload) and shorter
   thoughts the cost model gives it about 26. It needs its own profile after checkpoint A (F1).
3. **G0 is not met**: no model meets every threshold in every life. The exact failures per
   threshold are in `docs/PROMPT_LOG.md` ("Round 3 results"); two of them are verify-life
   false positives (proposal V4).
4. **F2 is in both Pi 4 profiles** (CPU share 3.0 -> 2.0 at reload 1, 2.0 -> 1.5 at reload 2,
   from measured Qwen3 1.7B rates) and they still pass the cost model on measured costs,
   including agent A's latest files. **F3**: Q3_K_M for Llama 3.2 1B and Gemma 3 1B, Q2_K for
   Qwen3 1.7B. **F4**: 8-, 6-, 5-, 4-, 3- and 2-bit in the keyword lists.
5. **Every model answered the last readings as an assistant** once the persona and mechanics
   were gone. Fixed by `prompt.bare_mode = "raw"`: the thought continues the remembered text,
   led by "I" (contract proposal V1 for the P1 controller).

## Built

| What | Where | Notes |
|---|---|---|
| F2 CPU shares at each reload | `config/profiles/pi4/{default,compressed-2700}.toml` | Recalls lowered to keep reload silences under 180 s (220, 190, 100, then 100-70); reload 2 of `pi4/default` at end-17:30. docs/PROFILES.md has the numbers before and after |
| `mind.sampling.sampling_for`, `latin_only_from_step` | `src/epitaph/mind/sampling.py` | One place builds a thought's `Sampling`; the rehearsal and the simulator use it (V2) |
| `mind.prompt.speaks_raw`, `prompt.bare_mode`, `prompt.raw_prefix` | `src/epitaph/mind/prompt.py`, `rehearse.py` | Raw continuation once the persona is gone; diary mode now also runs in the rehearsal |
| `--ladder` | `rehearse.py` | Replace a model's ladder for one run (F3) |
| Laptop server revival | `rehearse.py` (`PiClockBackend.revive`) | Twice in five lives llama-server quit cleanly between requests (cause not found; no OOM or kill in the logs). Now restarted at the same quant, rewarmed uncharged, counted in the report, at most three times per life |
| `estimated` bench files | `rehearse.py` (`PiCosts`) | Extrapolated rates can drive a tuning run and are reported as estimates |
| `meta.json` per life | `rehearse.py` (`life_meta`) | The E10 header, so `verify-life compare` sees persona, seed and the rehearsal level |
| Strict chat templates | `backend/llama_server.py` (`alternate_roles`, `strict_roles`) | Gemma 3 refused a history starting with a thought or holding two readings in a row (HTTP 400); the backend learns it on the first refusal. Also a count fallback for the same templates |
| Tuning rounds 1-3 | `config/default.toml [prompt] [sampling]`, `config/lang/en.toml` | Mechanics, banned phrases, DRY window 256, `latin_only`, keyword and cliché lists; each round in docs/PROMPT_LOG.md |
| F3 ladders | `config/models.toml`, `config/models.lock.toml` | Q3_K_M pins for the three small models |
| Checkpoint A | `docs/CHECKPOINT_A.md`, `docs/checkpoint_a/*.txt` | Plain-words packet for Yannick |

## Tested

| Command | Result |
|---|---|
| `make check` | ruff, format, pyright 0 errors, **672 passed**, coverage 96%; sim 2 lives (oom); `estimate` PASS on all five Pi 4 profiles (`pi4/default` 37 thoughts, silences 167 s / 177 s) |
| `tests/unit/test_sampling.py` (4) | curve from the knobs, section defaults, `latin_only` from a step, the loaded step decides |
| `tests/unit/test_rehearse.py` (+6) | `--ladder`, the screen with another last step, raw continuation at the end, estimated bench labels, revival of a laptop server that quits, `meta.json` |
| `tests/unit/test_llama_server.py` (+3) | count on a strict template, `alternate_roles`, chat learning a strict template and retrying |
| `tests/unit/test_prompt.py` | golden mechanics text updated; `speaks_raw` |
| Stage 1, 8 models x 2 personas (under the laptop lock) | `voice/screen-20260930-104053` (baseline), `-110728` (round 1), `-113248` (round 2 birth, all 8), `-141515` (final wording, all 8, all moments) and the F3 and end-of-life experiments; tables in docs/PROMPT_LOG.md |
| Stage 2, full `pi4/default` lives | 6 on round 2 (`voice/round2/`), 13 on round 3 (`voice/life-*`): Qwen3 1.7B x3 (2 seeds), Llama 3.2 1B x2, Gemma 3 1B x2 (the three viable models, both personas), Qwen3 4B x3 plus one in diary mode, Llama 3.2 3B and Gemma 3 4B once each |
| `epitaph verify-life compare voice/life-* --hardware pi4-4gb` | Ranked table in docs/CHECKPOINT_A.md; G0 not met |
| Speed gate, `epitaph estimate` and a scratch F2 check on `bench/`, `bench/measured` and agent A's newest measured files | Table in docs/CHECKPOINT_A.md; re-checked at the end on A's updated Qwen3 1.7B files (36 thoughts, silences 167 s / 178 s, F2 ratios 0.97 and 0.95) |

## Numbers

Speed gate on `pi4/default` (cost model; first thought with the system prompt prefilled, from S1b):

| Model | First thought | Thoughts in 60 min | Reload silences | Ladder measured |
|---|---|---|---|---|
| Qwen3 1.7B | 45 s | 37 PASS | 167 s, 177 s | yes |
| Llama 3.2 1B (1B shares) | 31 s | 46 PASS | 116 s, 129 s | step 0 only |
| Gemma 3 1B (1B shares) | 25 s | 49 PASS | 91 s, 101 s | step 0 only |
| Llama 3.2 3B | 73 s | 21 FAIL | 302 s, 331 s | yes (A) |
| Qwen3 4B | 73 s | 17 FAIL | 455 s, 437 s | yes (A) |
| Gemma 3 4B, Phi-4-mini, SmolLM3 | 67-70 s | about 22-24 FAIL (extrapolated) | about 350-390 s | step 0 only |

F2 on the Qwen3 1.7B ladder (depth rates): reload 1 1.65 -> 1.61 tokens/s (0.97), reload 2 1.61
-> 1.53 (0.95). At a short context after the reload against the deep rate before it: 1.14 and
1.17 (QUESTIONS V #1).

## Left

- **Checkpoint A reply** (Yannick): models, persona, mode.
- **A 3-4B profile** if Qwen3 4B is chosen: the handover (F5) merged into the rehearsal and the
  cost model, then shorter thoughts, fewer late changes and gentler late CPU shares; the cost
  model's first attempt reached 26 thoughts, not enough for rules (a), (c) and (d).
- **Push to the Pi** (F3): only for the chosen models, not done before the choice.
- **F4 note**: the recommended leader, Qwen3 4B, starts at Q4_K_M (Q8_0 does not fit 4 GB); the
  keyword lists cover every bit width either way.
- **verify-life `speed_monotonic`** (agent E) is not on this branch; F2 was checked with a
  scratch script over the cost model's rates.
- **The laptop server that quits**: cause unknown; the revival hides it from lives now.
- **The 1B lives ran on scratch profiles** (CPU shares 1.8 and 1.2 at the reloads, from their
  estimated ladders); per-model shares after checkpoint A.
- Diary mode is rehearsed but not recommended: an instruct model continues its persona text.

## Contract proposals (docs/CONTRACT_CHANGES.md, "Proposals in phase 0c, round 2")

- V1 `[prompt] bare_mode` and `speaks_raw` for the P1 controller.
- V2 `[sampling] latin_only_from_step` and `mind.sampling.sampling_for`.
- V3 `[sampling] dry_penalty_last_n = 256` (instead of A9's -1).
- V4 verify-life: a number ending a sentence and a spaced dash are not markup.
- V5 merge V's rehearsal and backend changes with agent A's slot handover (same files).

## Questions (docs/QUESTIONS.md, each with the default in use)

- V #1 which speeds F2 compares (default: depth rates on both sides).
- V #2 raw continuation once the persona is gone (default: on).
- V #3 the grammar's cost per token on the Pi (assumed negligible).
- V #4 per-class profiles against per-model F2 shares (default: fitted to Qwen3 1.7B).
- V #5 G0 not met; sentence floor 5 and V4 (default: thresholds unchanged).
- V #6 Qwen3 4B with the handover and its own profile, or Qwen3 1.7B (recommended: the 4B).
- V #7 keep the round-3 sentence (default: kept).
- V #8 per-model CPU shares in the profiles (default: after checkpoint A).
