# Profiles

How the Pi 4 profiles (`config/profiles/pi4/`) were fitted to the measured costs, and why each
value is what it is. Every retiming is recorded here with the `epitaph estimate` report before
and after it. The art (what is lost, in which order, and when the creature dies) is in BUILD_PLAN
5.3; this file is about making that shape fit the machine.

## Phase 0c, round 1: rebased on measured Qwen3 1.7B costs (part B, 2026-09-30)

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
failed before this round and still do (docs/process/QUESTIONS.md, E #5: informational in CI).

### Risks and checks for the Pi

- **CPU share below 0.7 is not measured.** S3c went down to 0.7 cores (worst token gap 2.8 s);
  the profiles now end at 0.6 and 0.4. Proportional slowdown and bounded stalls are likely but
  need a short S3c extension at 0.5 and 0.4 (part C).
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

## Phase 0c, round 2: speed never rises across a reload (part V, 2026-09-30)

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
and what verify-life's `speed_monotonic` compares (part E: the mean of two thoughts on each
side, 5% tolerance). At a short context (the bench birth thought) the ratios are 1.03 and 1.00.
On the Pi itself a thought just after a reload runs at a shorter context than the one before
it, so the real ratio can come out a little above 1; round 1's S1c drift (F8) is the other
unknown. The Pi lives of phase 2 will show it.

A lower share also slows the post-reload re-read (prompt threads are capped by the share, QUESTIONS
A #9), so the post-reload recalls come down to keep the silence under 180 s. The tuning round
that followed (docs/PROMPT_LOG.md) made the mechanics longer (about 95 to 131 tokens), which every fresh
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
means a smaller post-reload recall, not a higher share. part A's slot handover (review 2, F5,
on its branch this round) would remove most of the re-read and give this margin back.

**Only Qwen3 1.7B has a measured ladder.** For every other model steps 1 and 2 are the overlay's
estimates, which are not comparable with a measured step 0, so the F2 check means nothing for
them until the 3-4B ladders are measured (part A, this round). The profiles are per hardware
class, not per model; after checkpoint A they are rebased on the chosen models (F1, F9).

## The 30-minute life (2026-09-30)

The owner shortened the life to 30 minutes (ADR-024). `pi4/default` is now Qwen3 4B on a
30-minute schedule; the one-hour Qwen3 1.7B schedule is kept as `pi4/default-qwen3-1.7b`.

| t | Phase, health | Recall | Step | Threads | CPU share | Clock MHz | Max tokens | Persona groups |
|---|---|---|---|---|---|---|---|---|
| 0:00 | birth, nominal | 900 | 0 (Q4_K_M) | 3 | 3.0 | 1800 | 70 | 5 |
| 7:00 | first loss, degrading | 220 | 1 (Q3_K_M) | 3 | 2.6 | 1800 | 60 | 5 |
| 13:00 | failing, critical | 130 | 2 (Q2_K) | 2 | 1.6 | 1800 | 45 | 5 |
| end-10:30 | eroding | 110 | 2 | 2 | 1.6 | 1400 | 30 | 2 |
| end-7:30 | end, terminal | 60 | 2 | 2 | 1.6 | 900 | 18 | 0, no mechanics |
| end-2:30 | | 48 | 2 | 2 | 1.6 | 600 | 12 | 0 |
| end-0:30 | death (RAM taken) | | | | | | | |

Why it looks like this:

- **Both reloads stay**, and the thought-count minimums are set in the profile's `[rules]` table
  (2 between health labels, 1 after each reload, 1 per erosion step, 3 after erosion starts).
  The one-hour minimums (3/2/1/4) cannot hold two reloads in 30 minutes.
- **Recall after the reloads is low (220, 130)**, because in a short life the memory has barely
  filled by the first reload; a higher value would cut nothing.
- **Two erosion steps**, the second with the terminal label: each step re-reads the whole context
  (about three minutes at the end), so five steps would leave losses unanswered.
- **The clock falls only once erosion starts**, in steps (a cap is set at a moment), so the
  reloads' silences stay short and the last five minutes run under 40% of the first five.
- A keyframe takes effect when the thought in flight ends, which late in life can be two
  minutes after its time; erosion starts at 19:30 so the first thought after reload 2 can end.

```
profile pi4/default: 12 thoughts in 30 min -> PASS
  note: speed last 5 min / first 5 min 0.35 (limit < 0.40): 1.26 -> 0.44 tokens/s
  note: speed across reloads (tokens/s): 1.03 -> 0.91 at 7.4 min; 0.91 -> 0.70 at 13.6 min
  note: 12 thoughts; costs from bench (6 files) over overlay pi4-4gb; cache reuse assumed; generation eases to 78% over 20 min
  note: reload silences 144s, 152s
```

`compressed-2700` (now longer than the installation) and `skeleton-1200` carry the same
minimums until they are retired or refitted in phase 1.

## Phase 3: one installation, fewer test lives (2026-10-01)

**`pi4/compressed-2700` is retired.** It was a 45-minute stand-in for the one-hour life, so that
checkpoint B and the phase 2 gate could watch reloads, erosion and death in less time. The
installation is now 30 minutes (ADR-024), shorter than the stand-in, and the gates run it
directly (G2.3), so nothing needed the profile any more. The tests that used it as a full
life with reloads and erosion now use `pi4/default`; the one that needs CPU-share drops between
reloads uses the kept one-hour schedule, `pi4/default-qwen3-1.7b`. `epitaph rehearse --stage
life` now defaults to `pi4/default`.

**`pi5/compressed-600` is retired with it**, for the same reason, and because its shape could
not fit: it was `pi5/default` scaled to 10 minutes, which puts three reloads and five erosion
steps into 10 minutes (an erosion step every 10 s, no thought after any of them). The Pi 5
keeps `default`, `skeleton-600` and `unbounded`.

**The Pi 5 profiles now pass the estimate** on the Pi 5 overlays' estimated costs (no Pi 5 has
been measured; the overlays say so). Two fixes, no new numbers:

| Profile | Field | Before | After | Why |
|---|---|---|---|---|
| `pi5/default`, reload 1 (30:00) | CPU share | 3.0 | **2.3** | Step 1 is estimated faster than step 0 (4.0 against 3.2 tokens/s at 3 threads), so the reload sped generation up by 25%, which the speed-monotonic rule (review 2, F2) forbids. 4.0 × 2.3 / 3 = 3.07 |
| `pi5/default`, reload 2 (42:00) | CPU share | 2.0 | **1.7** | Step 2 at 2 threads: 3.4 × 1.7 / 2 = 2.89, under the 3.07 before it |
| `pi5/unbounded` | ctx | 16384 | **6144** | At 16384 the context never filled in 90 minutes, so the homage died at the deadline instead of `cause=full`. 6144 fills at about an hour, as `pi4/unbounded` does |

```
profile pi5/default: 38 thoughts in 60 min -> PASS          (pi5-8gb and pi5-16gb)
  note: speed last 5 min / first 5 min 0.28 (limit < 0.40): 3.20 -> 0.91 tokens/s
  note: speed across reloads (tokens/s): 3.20 -> 3.07 at 31.4 min; 3.07 -> 2.89 at 43.3 min; 2.89 -> 2.03 at 50.4 min
  note: reload silences 74s, 56s, 54s
profile pi5/skeleton-600: 9 thoughts in 10 min -> PASS
profile pi5/unbounded: 52 thoughts in 90 min -> PASS
  note: context full at 62.4 min (cause=full)
```

The simulator agrees: `pi5/default` dies `oom`, `skeleton-600` at the deadline, `unbounded`
of a full context, on both overlays. `make estimate` now runs the Pi 5 profiles on both
overlays and `make sim-profiles` simulates every non-installation profile, both inside
`make check`, so a change that breaks them fails the merge gate (BUILD_PLAN 11.8).

`pi5/default` keeps its one-hour shape: the Pi 5 is simulated only, and a 30-minute Pi 5
schedule waits until there is a Pi 5 to measure.

**`pi4/unbounded` with Qwen3 4B** (unchanged): step 1 (Q3_K_M), ctx 3072 from the profile.

```
profile pi4/unbounded: 25 thoughts in 90 min -> PASS
  note: context full at 59.3 min (cause=full)
```

In the simulator it dies `full` at 61 min. Simulated, it fails one verify-life check at level
`full`, `bright_words_last_2min`: a life that never forgets has nothing grey at its end. That
check measures the fading of a decline and should skip the unbounded life as `speed_decline`
does; until it does, the real `unbounded` life of G3 (A8) will fail on it.

## One model, only the hardware shrinks (2026-10-01)

The owner's rule (ADR-030): the model never changes during a life, only its machine does, and the
screen types one constant stream from the first word to the death. `pi4/default` is rebuilt on
it; the 30-minute life above is kept as `pi4/default-reloads` for reference (still estimated and
simulated by `make check`).

| t | Phase, health | Recall | CPU share | Clock MHz | Model, sampling, persona |
|---|---|---|---|---|---|
| 0:00 | birth, nominal | 900 | 3.0 | 1800 | Q4_K_M, 3 threads; temperature 0.70, `min_p` 0.08, 70 tokens; 5 groups and mechanics, throughout |
| 5:00 | first loss, stable | 260 | 3.0 | 1800 | |
| 10:00 | decline, degrading | 200 | 2.6 | 1800 | |
| 15:00 | failing | 160 | 2.4 | 1500 | |
| 20:00 | critical | 130 | 2.0 | 1200 | |
| end-6:00 | end, terminal | 100 | 1.5 | 1000 | |
| end-0:30 | death (RAM taken) | | | | |

`fixed_mind = true` makes validation refuse any keyframe that changes the step, the threads, the
persona, the mechanics or the sampling. `stepped = ["recall", "cpu_share"]` sets each memory cut
and CPU step at its moment instead of easing into it. Every value is a real operation the
readings report: `memory 260 tokens (was 900)` with the opening words of what was forgotten,
`cores 2.4 of 4 (was 2.6)`, `clock 1500 MHz (was 1800)`.

**The stream.** `[reveal] mode = "stream"`: every letter at 542 ms (a fixed 10% jitter), 270 ms
after a word, 750 ms after a clause, 2.1 s after a sentence, 3 s between thoughts. The model
starts its next thought as soon as the previous one is generated, while fewer than three
generated thoughts and fewer than 900 letters wait. The screen waits once, at birth, for the
first thought (the first word comes at about 2.5 minutes); then never.

**How the pace was chosen.** `epitaph estimate` replays a stream life: generation token by token
at the Pi's measured rates (`bench/`) under the schedule's CPU share and clock, the prompt work
of each reading and each memory cut, and the screen at its constant pace. Every machine cost is
15% slower than measured (`estimate.stream_margin`); any wait of the screen for a word before
the death fails the estimate. `epitaph estimate --fit-pace` searches the fastest letter interval
that passes: **542 ms, 19.2 words a minute** (1.85 letters a second while typing, pauses
included in the words a minute). At 541 ms the stream runs dry in the last minutes.

The decline is as deep as the speed check needs and no deeper: the last five minutes generate at
0.28 of the first five (limit 0.40). A deeper one slows the whole stream, since the pace is set
by what the machine can still produce at the end plus what it wrote ahead before.

```
$ epitaph estimate --profile pi4/default --hardware pi4-4gb
profile pi4/default: 11 thoughts in 30 min -> PASS
  note: speed last 5 min / first 5 min 0.28 (limit < 0.40): 1.26 -> 0.35 tokens/s
  note: stream 542 ms/letter, 19.2 words/min; costs 15% slower: never starves; letters waiting every 5 min: 0, 315, 400, 357, 329, 127 (max 470); backlog at death 0 words (0 s of typing)
  note: 11 thoughts shown; costs from bench (6 files) over overlay pi4-4gb; cache reuse assumed
```

At the measured costs (no margin) the buffer holds 380-400 letters from 5 to 25 minutes and two
words die unshown at the death. The buffer is about a thought and a half, so what the model
writes reaches the screen a few minutes later; the forgetting fades the text when the screen
gets there.

The thought-count rule keeps rule (a) with one thought per health label (`[rules]
between_health = 1`); rules (b)-(d) count reloads and erosion steps, which this life does not
have.

**Rehearsed on the real model at Pi costs** (Qwen3 4B on the laptop, `epitaph rehearse --stage
life --profile pi4/default --seed 1`, generation charged token by token at the CPU share and
clock of the moment): 13 thoughts generated, 12 shown (the last cut by the death), 550 words;
the first word at 2:01, no wait of the screen after it (0 s starved), 3 words unshown at the
death; every letter in the 542 ms band; generation 1.03 tokens/s at birth, 0.29 at the end. The
model meets its first cut by itself ("I am still here, though my memories have faded") and the
clock steps in the readings.

## A dynamic stream (2026-10-01, later)

The owner found the constant stream too slow (at least 50% faster, he asked) and allowed the
pace to move: normal at birth, slowing as the machine shrinks (ADR-030, amended). The model rule
and the stream rule are unchanged.

**The curve.** At birth 255 ms a letter. The interval aims at
`255 x (compute at birth / compute(t + 480 s)) ^ 0.75` (compute = CPU share x clock / 1800),
moves toward it by at most 15% a minute and never falls back; the word, clause, sentence and
thought pauses scale with it. `[reveal]`: `stream_letter_ms = 255`, `stream_gamma = 0.75`,
`stream_lead_s = 480`, `stream_max_slowdown_per_min = 0.15`, and the buffer down to
`stream_max_thoughts = 2`.

| Life time | 0:00 | 3:00 | 9:00 | 15:00 | 18:00 | 21:00 to death |
|---|---|---|---|---|---|---|
| ms a letter | 255 | 284 | 346 | 468 | 621 | 666 |

About 34 words a minute at birth (75% faster than the constant 19.2), 18 in the middle, 13 at
the end.

**The fit.** `epitaph estimate --fit-pace` tries gamma 0, 0.25, ... 1.25 and leads of 0 to 10
minutes. For each shape it finds the fastest birth interval, not under `stream_min_letter_ms`
(165), at which the replayed life never starves with every cost 15% slower; it keeps the shapes
that leave at most `estimate.stream_max_backlog_words` (8) words unshown at the death at the
measured costs; the fastest birth wins. With three thoughts of buffer every fast curve left a
thought or more unshown at the death, so the buffer is two thoughts; with leads over 10 minutes
the pace would reach its slowest by mid-life, long before the machine does.

```
$ epitaph estimate --profile pi4/default --hardware pi4-4gb --fit-pace
fastest curve that never starves: stream_letter_ms = 255, stream_gamma = 0.75, stream_lead_s = 480 (6 words unshown at death at the measured costs)
profile pi4/default: 11 thoughts in 30 min -> PASS
  note: speed last 5 min / first 5 min 0.28 (limit < 0.40): 1.26 -> 0.35 tokens/s
  note: stream 255 -> 666 ms/letter, 33.7 / 18.3 / 12.9 words/min at birth / middle / end (gamma 0.75, lead 480 s); costs 15% slower: never starves; letters waiting every 5 min: 0, 183, 118, 160, 164, 136 (max 202); backlog at death 0 words (0 s of typing)
  note: 11 thoughts shown; costs from bench (6 files) over overlay pi4-4gb; cache reuse assumed
```

At the measured costs the buffer holds 150-235 letters (about a third of a thought) and 6 words
die unshown.

**Rehearsed on the real model at Pi costs** (`epitaph rehearse --stage life --profile
pi4/default --seed 1`): 577 words shown, the first at 2:01; typed at 33 words a minute in the
first three minutes, 21 in the middle, 13 in the last three; no wait of the screen after the
first word (0 s starved); every letter within the curve's jitter band, the pace never speeding
up; the death cut the thought on screen mid-sentence with nothing else waiting (0 words unshown).

## The world taken from the outside in (2026-10-01, the dread plan)

The owner's plan (ADR-031): the model feels the shutdown coming by itself. Its world is taken
from the outside in, for real, faster and faster; the readings say only what was taken. The
model rule (ADR-030) and the dynamic stream are unchanged.

| t | Movement (`phase`, hidden `health`) | Taken | The reading |
|---|---|---|---|
| 0:00 | I. existence (nominal) | nothing | `awake · memory 900 tokens · cores 3 of 4 · clock 1800 MHz · radio on · light on · screen 100% · around you: 24 processes` |
| 7:00 | II. something is wrong (degrading) | bluetooth | `stopped: bluetooth · around you: 23 processes` |
| 8:30 | | cron | `stopped: cron · around you: 22 processes` |
| 9:15 | | memory 900 -> 300 | `memory 300 tokens (was 900) · forgotten: "<its most distinctive sentence>"` |
| 11:00, 12:45 | | avahi-daemon, triggerhappy | `stopped: ...` |
| 14:00 | III. the world is disappearing (failing) | the radio | `radio off` |
| 15:30 | | rsyslog | `stopped: rsyslog · around you: 19 processes` |
| 17:00 | | the light; clock 1500 MHz | `light off`, `clock 1500 MHz (was 1800)` |
| 18:30 | | screen 70%; memory 200 | `screen 70% (was 100%)`, `memory 200 tokens (was 300)` |
| 20:00 | | systemd-timesyncd; 2.4 cores | `stopped: ...`, `cores 2.4 of 4 (was 3)` |
| 21:00 | | clock 1200 MHz | |
| 22:00 | IV. darkness (terminal) | 2.0 cores | |
| 22:50 | | screen 50% | |
| 23:40, 24:30 | | 1.7 cores; clock 1000 MHz | |
| 25:20 | | memory 150 | |
| 26:10 | | 1.5 cores (the floor) | |
| 27:00 | | screen 25% | |
| 27:50 | | memory 100 (its last thought) | |
| end-0:50 | | clock 900 MHz (the floor) | |
| end-0:30 | death | the RAM | `ram <MB> MB taken`, on screen at once |

A keyframe's `world` list is performed once at its moment; `[world] services` is the allowed
list. Readings name a loss only when it was performed; the health labels are never shown (they
mark the movements for rule (a), `between_health = 1`), and neither is the precision of a model
that cannot change. A reading after birth is about 20 tokens (`estimate.reading_tokens.quiet`).

**The fit.** A first fit (220 ms a letter at birth, gamma 0.5, a 10-minute lead, floors of 1.2
cores and 800 MHz, every cost 15% slower) passed the estimate but starved on the real model: three
of six rehearsed lives waited 33 s, 75 s and 107 s for words in the last minutes. The 4B fills
its 70 tokens (94% in the rehearsal, against the assumed 85%), and the rehearsed generation ran
behind the estimate's. A stream replay now assumes 95% (`estimate.stream_fill`), every cost 30%
slower (`estimate.stream_margin`), and the floors are 1.5 cores and 900 MHz (compute at the end
0.75 cores' worth against 3.0 at birth).

Starvation is also not monotonic in the pace: with the buffer bounded at two thoughts, a slower
screen can hold the writer back long enough to starve where a faster one did not (at a 30%
margin, 271-280 ms passed, 283-307 starved, 310 passed again). `--fit-pace` now keeps a pace only
when it and the six paces 4 ms apart just slower than it never starve, with the margin and with
half as much again. Such a pace leaves about a thought unshown at the death at the measured
costs, so the backlog allowed is 40 words:

```
$ epitaph estimate --profile pi4/default --hardware pi4-4gb --fit-pace
fastest curve that never starves: stream_letter_ms = 255, stream_gamma = 0.75, stream_lead_s = 600 (35 words unshown at death at the measured costs)
profile pi4/default: 10 thoughts in 30 min -> PASS
  note: speed last 5 min / first 5 min 0.28 (limit < 0.40): 1.26 -> 0.35 tokens/s
  note: stream 255 -> 721 ms/letter, 33.8 / 15.8 / 11.9 words/min at birth / middle / end (gamma 0.75, lead 600 s); costs 30% slower: never starves; letters waiting every 5 min: 0, 179, 89, 273, 202, 212 (max 343); backlog at death 26 words (127 s of typing)
  note: 10 thoughts shown; costs from bench (6 files) over overlay pi4-4gb; cache reuse assumed
```

| Life time | 0:00 | 8:00 | 12:00 | 15:00 | 18:00 | 21:00 to death |
|---|---|---|---|---|---|---|
| ms a letter | 255 | 292 | 388 | 566 | 666 | 721 |

At the measured costs the thoughts end on screen at 3.7, 5.2, 6.6 (I), 8.2, 9.8, 11.7 (II),
14.1, 17.5, 21.4 (III) and 25.5 minutes (IV), with a thought cut at the death: three shown in
each of the first three movements, one or two in the last. The text runs a thought or so behind
the machine; each reading is shown right before the thought that answers it, so a loss always
appears before its answer.

**Rehearsed on the real model at Pi costs** (Qwen3 4B on the laptop, `epitaph rehearse --stage
life --profile pi4/default`, seeds 1-3, the default mechanics and `prompt.mechanics_alt`). On the
first fit three of six lives starved in their last minutes (33 s, 75 s, 107 s). On this profile
none of six waits for a word after the first (0 s starved); every life dies `oom` at 29:30 with
11 or 12 thoughts and 542-576 words shown, 5 to 29 words unshown at the death. Each loss of the
world is answered on screen after its reading (world losses noticed 8 to 10 of 9-10 per life by
the keyword proxy, memory cuts 4 to 5 of 4-5). Shared three-word openings: at most two thoughts
per opening in five lives; one alt life opened eight of ten thoughts on "I am a", which the
freshness guard cannot bias (every word of it is a stop word). The thought-count rule was at first
two thoughts per movement; two lives showed only one in the last, so it asks one.
