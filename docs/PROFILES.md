# Profiles

How the Pi 4 profiles (`config/profiles/pi4/`) were fitted to the measured costs, and why each
value is what it is. Every retiming is recorded here with the `epitaph estimate` report before
and after it. The art (what is lost, in which order, and when the creature dies) is in BUILD_PLAN
5.3; this file is about making that shape fit the machine.

## Phase 0c, round 1: rebased on measured Qwen3 1.7B costs (agent B, 2026-09-30)

### What changed in the inputs

| Input | Before | After | Source |
|---|---|---|---|
| `life.models` (provisional) | `llama-3.2-3b-instruct` | `qwen3-1.7b` | S1b: the Pi 4 speed leader (prompt 6.0 tokens/s, generation 1.65-1.84) and the only candidate under the 90 s birth test without prefill. Checkpoint A decides the real list |
| `bench/pi4-qwen3-1.7b-*.json` | none (overlay estimates) | the five measured files from `bench/measured/` | S1b, S4 (step 0 at 2 and 3 threads, step 1 at 2 and 3, step 2 at 2) |
| Late generation speed | not modelled | `tg_tok_s_late = 1.424` on the step-0 file, reached after 30 min | S1c: 1.81 -> 1.42 tokens/s over 30 min, not thermal |

### What changed in the cost model (`src/epitaph/costmodel.py`)

- **Prompt threads.** Prompt processing runs on `backend.threads_batch` (3) threads, limited by
  the CPU share (S4).
- **Late speed.** Every generation rate eases down to the S1c late share (86% of the bench value)
  over the first 30 minutes of the life and stays there. Whether a reload resets it is not
  measured, so it is not assumed.
- **System prompt.** Sized from the real persona text at each erosion step (265 tokens with five
  groups and the mechanics; S1b measured 262-280 on the Pi) instead of 30 per group plus 70.
- **The memory-gap marker** (decision A3) rides on the reading after the first loss: it adds its
  own tokens (11) and no re-read. The first marker in front of the kept turns would have cost an
  80% re-read (S2f).
- **Two new failures**, because a profile is where they are won or lost:
  - `silence`: a reload's silence (load plus the fresh server's full re-read) over
    `verify.max_reload_silence_s` (180 s on the Pi 4);
  - `speed`: for full-level profiles, generation in the last 5 minutes at or above
    `verify.max_speed_ratio_end_vs_start` (40%) of the first 5. Both windows use the
    short-context speed (`tg_tok_s_birth`) and no late drift, so the estimate errs on the fast
    side at the end, where the check is hard to pass.

### The retiming

Before (phase 0b profiles, measured costs, the new cost model):

```
profile pi4/default: 40 thoughts in 60 min -> FAIL
  note: speed last 5 min / first 5 min 0.52 (limit < 0.40): 1.84 -> 0.96 tokens/s
  note: reload silences 219s, 154s
  rule (silence) at 28.9 min: reload at 28.9 min is silent for 219 s (limit 180 s)
  rule (speed) at 54.5 min: last 5 min at 52% of the first 5 (need < 40%)
profile pi4/smoke-300: 5 thoughts in 5 min -> PASS
profile pi4/skeleton-1200: 15 thoughts in 20 min -> PASS
profile pi4/compressed-2700: 32 thoughts in 45 min -> FAIL
  note: speed last 5 min / first 5 min 0.51 (limit < 0.40): 1.84 -> 0.93 tokens/s
  note: reload silences 218s, 154s
  rule (silence) at 12.4 min: reload at 12.4 min is silent for 218 s (limit 180 s)
  rule (speed) at 39.5 min: last 5 min at 51% of the first 5 (need < 40%)
profile pi4/unbounded: 42 thoughts in 90 min -> PASS
  note: context full at 53.6 min (cause=full)
```

For the record, the phase 0b cost model on the same measured costs passed every profile
(`pi4/default` 42 thoughts, reload silences 200 s and 139 s), because it checked neither the
silence nor the speed decline; on the old estimated costs `pi4/default` had 47 thoughts and
silences of 130 s and 77 s.

Changes (`pi4/default` and `pi4/compressed-2700` alike):

| Keyframe | Field | Before | After | Why |
|---|---|---|---|---|
| reload 1 (28:00; 11:15) | recall | 512 | **300** | S4: the fresh server re-reads system + memory + reading. At 512 that is 181 s even at 3 prompt threads; at 300, 135 s |
| reload 1 | threads, CPU share | 2, 2.0 | **3, 3.0** (unchanged from birth) | With 2 generation threads the profile's CPU share may not exceed 2.0 (config validation), and 3 prompt threads on 2 cores read no faster than 2: the reload silence was 219 s. Keeping 3 threads to reload 2 lets the re-read use them: 128 s. Generation hardly changes (S1b: 3 -> 2 threads is 1.65 -> 1.60 tokens/s), so the thread drop was never a visible slowdown |
| decline (36:00; 20:00) | recall | 380 | **260** | Recall cannot grow after the reload cut; it keeps shrinking toward reload 2 |
| reload 2 (end-17:00; end-20:30) | threads, CPU share | held 2, 2.0 | **2, 2.0** (the thread drop moves here) | The first reading after reload 2 now reports cores 2 of 4 (was 3) as well as 2-bit (was 4-bit). Silence 153 s at recall 200 |
| erosion steps 2-5 | CPU share | 1.4, 1.1, 0.9, 0.7 | **1.3, 0.9, 0.6, 0.4** | Q2_K generates faster than Q8_0 (S1b: 2.5 against 1.8 tokens/s at a short context), so the old slope ended at 52% of the birth speed; verify-life wants under 40%. Now 32% |

After:

```
profile pi4/default: 40 thoughts in 60 min -> PASS
  note: speed last 5 min / first 5 min 0.32 (limit < 0.40): 1.84 -> 0.59 tokens/s
  note: 40 thoughts; costs from bench (5 files) over overlay pi4-4gb; cache reuse assumed; generation eases to 86% over 30 min
  note: reload silences 128s, 153s
profile pi4/smoke-300: 5 thoughts in 5 min -> PASS
profile pi4/skeleton-1200: 15 thoughts in 20 min -> PASS
profile pi4/compressed-2700: 32 thoughts in 45 min -> PASS
  note: speed last 5 min / first 5 min 0.32 (limit < 0.40): 1.84 -> 0.59 tokens/s
  note: reload silences 128s, 154s
profile pi4/unbounded: 42 thoughts in 90 min -> PASS
  note: context full at 53.6 min (cause=full)
```

Margins under the thought-count rule (thoughts per window, from the estimate):

| Profile | (a) between health changes (need 3) | (b) after each reload (need 2) | (c) after each erosion step (need 1) | (d) after erosion starts (need 4) |
|---|---|---|---|---|
| `pi4/default` | 8, 10, 5, 5, 5, 7 | 4, 3 | 1, 2, 1, 2, 2 | 8 |
| `pi4/compressed-2700` | 4, 4, 5, 4, 6, 9 | 4, 3 | 2, 2, 2, 2, 3 | 11 |
| `pi4/skeleton-1200` | 7, 4, 4 | | | |

The thinnest margin is rule (c) in `pi4/default`: one thought after the first and third erosion
steps (2-minute windows). A late thought takes about a minute: decision 30's letter floor (270-510
ms) sets most of it, not the machine. If the rehearsal shows a step with no thought, the fix is a
shorter `max_tokens` late, or 2:30 windows as in `compressed-2700`.

The simulator agrees (`epitaph sim`, now on the real mind modules): `pi4/default` lives have 42
thoughts and die of `oom`; verify-life passes `speed_decline` (0.33), `reload_silence` (128 s,
149 s), the thought-count rule, the recall budget and the sync rule.

Also: `pi5/default` gets the same end slope (CPU share 0.8 -> 0.3 over the erosion) and passes
its estimate on both Pi 5 overlays (speed 0.29). `pi5/skeleton-600` and `pi5/compressed-600`
failed before this round and still do (docs/QUESTIONS.md, E #5: informational in CI).

### Risks and checks for the Pi

- **CPU share below 0.7 is not measured.** S3c went down to 0.7 cores (worst token gap 2.8 s);
  the profiles now end at 0.6 and 0.4. Proportional slowdown and bounded stalls are likely but
  need a short S3c extension at 0.5 and 0.4 (agent C).
- **The reload silences are estimates.** 128 s at reload 1 matches S4's 135 s (Q4_K_M, recall
  300, 3 prompt threads, measured). Reload 2 (Q2_K, recall 200, 3 prompt threads on a 2.0 share)
  is estimated at 153 s; S4 measured 123 s at 3 prompt threads without a share limit and 170 s at
  2 threads.
- **The birth speed in the speed check is the short-context rate** (1.84 tokens/s); a real life's
  first minutes run a little slower as the memory grows, which only lowers the ratio.

### Decision A3 on a real server (laptop, Qwen3 1.7B Q8_0, `--cache-reuse 32`)

The mind's `Memory` and `Persona` driven through a life-like sequence against the real
`llama-server` (scratch script under the laptop lock; tokens re-read per request, `prompt_n`):

| Step | re-read / prompt |
|---|---|
| a normal turn (9 fills) | 54-55 tokens (6-12%) |
| first trim, marker on the new reading | 62 / 963 (6%) |
| trims while the marker's turn is kept | 55 / 766, 55 / 683 |
| trim that takes the marker's reading (the marker moves to the next reading) | 62 / 638, 62 / 438 |
| erosion step (G5, then G4 removed; the prompt is rebuilt, one paragraph per group) | 55 / 518, 55 / 582 |

Every edit re-reads only the new reading (plus the marker when it moves). The fake backend's
cache model agrees (`tests/sim/test_mind_life.py::test_cache_reuse_holds_through_every_loss`).
It also showed a second trap, fixed in `mind/memory.py`: a live trim that cuts words inside a
kept thought puts new tokens in front of everything after it, like the old marker, so live
trims now cut on turn boundaries (words only inside the last remaining turn).

## Phase 0c, round 2: speed never rises across a reload (agent V, 2026-09-30)

Review 2, F2 (binding): the generation speed after a reload may be at most the speed before
it. Lower precision is faster on the Pi 4 (generation is memory-bound), so without a change
the round-1 profile went from 1.65 to 2.41 tokens/s at reload 1 (Q8_0 to Q4_K_M), which the
viewer would read as the creature getting better. Each reload keyframe now sets the CPU share
from the measured Qwen3 1.7B rates so the new step is no faster than the old one:

| Reload | Before | After (depth rate) | CPU share | Why this share |
|---|---|---|---|---|
| 1 (28:00; 11:15) | Q8_0, 3 threads, 3.0 cores: 1.65 tokens/s | Q4_K_M, 3 threads: 2.41 at 3.0 cores, **1.61 at 2.0** | 3.0 -> **2.0** | 3 x 1.653 / 2.412 = 2.06 is the most that keeps 2.41 x share / 3 under 1.65 |
| 2 (end-17:00; end-20:30) | Q4_K_M at 2.0 cores: 1.61 | Q2_K, 2 threads: 2.04 at 2.0 cores, **1.53 at 1.5** | 2.0 -> **1.5** | 2 x 1.608 / 2.036 = 1.58 |

The comparison uses the bench's depth rates (`tg_tok_s`), which is what the rehearsal charges
and what verify-life's `speed_monotonic` compares (agent E: the mean of two thoughts on each
side, 5% tolerance). At a short context (the bench birth thought) the ratios are 1.03 and 1.00.
On the Pi itself a thought just after a reload runs at a shorter context than the one before
it, so the real ratio can come out a little above 1; round 1's S1c drift (F8) is the other
unknown. The Pi lives of phase 2 will show it.

A lower share also slows the post-reload re-read (prompt threads are capped by the share, QUESTIONS
A #9), so the post-reload recalls come down to keep the silence under 180 s. The tuning round
that followed (docs/PROMPT_LOG.md) made the mechanics 25 tokens longer, which every fresh
server re-reads, so the recalls came down once more and reload 2 of `pi4/default` moved 30 s
earlier (a thought in progress at 43:00 pushed the reload to 44:06, leaving one thought before
erosion, rule (b)):

| Keyframe | Field | Before | After |
|---|---|---|---|
| reload 1 | recall | 300 | **220** |
| decline (36:00; 20:00) | recall | 260 | **190** |
| reload 2 | recall | 200 | **100** |
| reload 2 (`pi4/default`) | time | end-17:00 | **end-17:30** (42:30; still after the 45-minute rescale's 27:00 decline) |
| erosion steps 1-4 | recall | 170, 140, 110, 80 | **100, 90, 80, 70** |
| erosion steps 1-3 | CPU share | 1.7, 1.3, 0.9 | **1.4, 1.1, 0.8** (never above the 1.5 of reload 2) |

The shares only fall from birth to death, so the speed never rises at any other keyframe either.

```
profile pi4/default: 38 thoughts in 60 min -> PASS
  note: speed last 5 min / first 5 min 0.30 (limit < 0.40): 1.84 -> 0.55 tokens/s
  note: reload silences 166s, 175s
profile pi4/compressed-2700: 29 thoughts in 45 min -> PASS
  note: speed last 5 min / first 5 min 0.32 (limit < 0.40): 1.84 -> 0.58 tokens/s
  note: reload silences 166s, 175s
```

(before: 40 and 32 thoughts, silences 128 s / 153 s and 128 s / 154 s). The other Pi 4
profiles have no reload. The margins under 180 s are thin (5-14 s): a slower re-read on the Pi
means a smaller post-reload recall, not a higher share. Agent A's slot handover (review 2, F5,
on its branch this round) would remove most of the re-read and give this margin back.

**Only Qwen3 1.7B has a measured ladder.** For every other model steps 1 and 2 are the overlay's
estimates, which are not comparable with a measured step 0, so the F2 check means nothing for
them until the 3-4B ladders are measured (agent A, this round). The profiles are per hardware
class, not per model; after checkpoint A they are rebased on the chosen models (F1, F9).
