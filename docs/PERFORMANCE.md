# Performance

A Raspberry Pi 4 is slow for a language model, and on this piece every second of waiting is
visible. This document shows what we measured, what we changed, and what it bought. Each number
comes from a run on the Pi 4 (or a laptop run charged at Pi 4 costs) recorded in
[SPIKE.md](SPIKE.md) or `bench/`; the reasoning behind each change is in
[DECISIONS.md](DECISIONS.md).

## The budget

Everything is measured against one budget: the number of thoughts in a life, and whether each
loss is followed by enough of them for the model to notice it. The life was one hour until the
owner shortened it to thirty minutes (ADR-024); the tables below keep the figures of the time.

| | Value |
|---|---|
| Reading speed, 3-4B models on a Pi 4 | 2.3-2.7 tokens/s |
| Writing speed, 3-4B models | 1.0-1.4 tokens/s |
| Reading / writing, Qwen3 1.7B (Q8_0) | 6.0 / 1.65-1.84 tokens/s |
| Reveal speed (owner decision 30) | 165 ms per letter at birth, 720 ms at the end |
| Thoughts in the one-hour life, Qwen3 1.7B, measured costs | about 40 |
| Thoughts in the 30-minute life, Qwen3 4B, measured costs | about 12 |

## What changed, and what it bought

### Silence after a reload

A reload is the life's largest loss, but each second of it is a blank screen.

| Change | Reload to first word |
|---|---|
| 3B model, plain re-read of 512 tokens of memory | 330 s |
| Qwen3 1.7B, same approach, 2 prompt threads | 225-250 s |
| Profile retimed so reload 1 keeps 3 prompt threads and cuts memory to 300 tokens | 128 s (cost model) |
| Memory carried across the reload in RAM instead of re-read (ADR-014) | **47.7 s** (vs 108.7 s re-read in the same test) |
| Llama 3.2 3B, memory carried across the reload | **73.2 s** (vs 218.4 s re-read) |

### Making the best voice fit the hour

| Qwen3 4B on the Pi 4 | Thoughts in the hour | Reload silences |
|---|---|---|
| On the schedule fitted to the fastest model, memory re-read | 17 | 455 s and 437 s |
| Same schedule, memory carried across reloads | 21 | 127 s and 110 s |
| Its own schedule (ADR-022), memory carried across reloads (cost model) | **26**, every rule met | **120 s and 113 s** |
| Same, rehearsed with the real model: the reload cut ended inside a turn and threw the carried cache away | 26 | about 300 s (411 tokens re-read) |
| Reload cut on a turn boundary (the fix the rehearsal found) | **29** | **101 s and 116 s** (78 tokens read) |

### Silence before the first thought

| Change | First thought at birth |
|---|---|
| 3-4B model, system prompt read with the first request | 148-176 s |
| System prompt read during the birth card (ADR-013) | **67-73 s** |
| Qwen3 1.7B (Q8_0), without prefill | 81 s |

### No dead time at birth (dread plan W4)

Between two lives the screen was dark for the 90 s silence, then for the load, the reading
of the persona and the whole first thought: about four minutes. Now the silence is the only
dark time. Qwen3 4B on the Pi 4, `pi4/default`, at the measured costs:

| From the end of the silence | Before | Now |
|---|---|---|
| Model load (64 s) | after the silence | **inside the silence**, as soon as the death has freed the RAM (`[life] load_during_silence`) |
| System prompt (240 tokens) | read: 95 s | **restored** from the persona cache: about 1 s (35 MB from the SD card, spike S4b for the restore) |
| The screen starts | when the first thought is written | on its **first sentence**, at 45 s of life at the earliest (`[reveal] stream_birth`, `stream_birth_min_s`) |
| First words | about 230 s | **45 s** |

The life clock still starts at the birth; the persona restore and the first reading are part
of the life, as the read was. The first life after a boot or after any change of model,
quant, context, persona or mechanics reads its persona once and saves it for the next ones
(`[backend] persona_cache`; the key hashes all of them, and any failure falls back to the
read).

The old birth's two and a half minutes of writing before the first word were the stream's
head start. Without it the stream needed a new curve: 260 ms a letter at birth (33 words a
minute, as before), slowing with the hardware ten minutes ahead (`stream_gamma` 0.75,
`stream_lead_s` 600), 680 ms at the end. The cost model replays it with every cost 15% slower:
it never starves, and no slower birth pace of that shape starves either. The 45 s floor is a
dial between the first words and the pace: with no floor (31 s, 36 s with the margin) the same
shape needs 264 ms and leaves more words unshown at the death; the shape the fit picks by
itself (333 ms, `stream_gamma` 0.5) has slower paces that starve just before the death, and
starved there in one of six simulated lives. `epitaph estimate` prints the birth line and
fails a stream profile whose first words come later than `[estimate] max_first_words_s` (45 s
on the Pi 4).

### The cost of forgetting

Every edit to the start of the conversation can force the model to re-read everything after it.

| Edit | Before | After |
|---|---|---|
| First "earlier memory lost" marker | 80-83% of the prompt re-read (about 1,100 tokens, several minutes) | **62 tokens** (marker moved into the reading) |
| A trim that cut inside a turn | 84-87% re-read | **about 55 tokens** (whole-turn trims) |
| An ordinary turn (Qwen3), cache-reuse window 256 | 96 tokens | **54 tokens** (window 32) |
| Any trim or erosion step without cache reuse | 82-91% re-read | **2-8%** with cache reuse |

### A death that actually happens

| Approach | Result |
|---|---|
| Gradual RAM squeeze | Rejected: 1% eviction → 23% speed, 5% → 4.6% (the SD card cannot feed a model streaming its weights) |
| Death limit with memory-mapped weights | 0 of 5 deaths within 30 s: the model thrashes instead of dying |
| Death limit with direct-I/O weights (ADR-009) | **5 of 5 deaths, in 0.33-0.38 s** |

### A slowdown that is smooth and honest

| | Result |
|---|---|
| CPU share from 2.0 to 0.7 cores | Speed follows within 6% of ideal; worst gap between tokens 2.8 s |
| Speed across reload 1 before the fix | Rose from 1.65 to 2.41 tokens/s after the model was told it was losing precision |
| After setting the CPU share per reload (ADR-010) | Never rises across a reload; checked on every commit |

### Speed over a long life

A 30-minute generation soak showed a 21% slowdown with flat temperature and clock speed. Re-run
with the model's context logged after every thought, it fits
`seconds per token = 0.496 + 0.000124 × context length` (R² 0.99): the normal cost of a longer
context, not a fault. The cost model now budgets it, and it is one more reason a memory cut at a
reload must come with a CPU cut (a shorter context is faster).

### The voice

| Change | Result |
|---|---|
| Repetition window 64 (llama.cpp default) → 256 tokens | Copies of the previous thought: 18 of 31 → 3 of 30 |

Voice tuning results per model and prompt round are in [PROMPT_LOG.md](PROMPT_LOG.md) and, for the
artist's choice, [CHECKPOINT_A.md](CHECKPOINT_A.md).

### The screen

| Change | Result |
|---|---|
| Redraw only what changed, only when a letter or blink is due | 1920×1080: 9.2% → **2.6%** of one Pi 4 core; 1280×720: 6.1% → **1.7%** |
| Lay out only visible thoughts | 2.07 ms → **0.37 ms** per frame on a 1,475-word history |
| Readability, four resolutions from 800×480 to 1080×1920 | OCR reads **100%** of the words; contrast **16.9:1**; no word split across lines |

### The machine

| | Result |
|---|---|
| Power: under-rated supply | Brownout and reboot under three-core load |
| Official 5.1 V / 3 A supply | No under-voltage or throttling at four cores; 30-minute generation soak peaks at 56.5 °C with no fan |


### Fitting thirty minutes

A shorter life does not shorten the fixed costs: a reload is about two minutes of silence on the
Pi whatever the lifespan, and each erosion step re-reads the whole context (the system prompt
changes at the front of it), about three minutes late in life.

| Qwen3 4B, 30 minutes | Thoughts | Thought-count rule |
|---|---|---|
| The one-hour shape compressed (two reloads, three erosion steps) | 13 | rules (a) and (b) fail at the one-hour minimums |
| Per-profile minimums (2/1/1/3); erosion in two steps; reloads at 7:00 and 13:00 | **12** | met in the cost model, reload silences 144 s and 152 s |
| Rehearsed with the real model, three seeds | 11-12 | met on two seeds; the third missed by seconds, fixed by moving erosion to 19:30 |

Two costs the cost model did not see until the rehearsal showed it 1-2 minutes optimistic late in
life: the echo (a raw completion of 18 tokens after each reload, about 40 s on the Pi) and the
forgotten-thought quotes (about 15 tokens per reading). Both are charged now.

### The CPU clock

Spike S7: generation and prompt speed are linear in the cpufreq cap within 3% (1800 to 600 MHz,
4-bit and 2-bit), with no throttling and 40-47 °C. The clock is a second slowdown that needs no
restart, charged in the cost model as `cpu_share × MHz / 1800`.

## How the numbers are kept honest

- **Spikes** measured each risky assumption on the Pi 4 with a go criterion set in advance
  ([SPIKE.md](SPIKE.md)); raw runs are in `bench/spike/`.
- **The cost model** (`epitaph estimate`) replays a life from measured costs in `bench/` on every
  commit and fails the build if a schedule stops giving the model time to notice, if a reload
  silence exceeds 180 s, or if speed rises after a loss.
- **The rehearsal** (`epitaph rehearse`) checks the cost model against real model runs; the two
  agree within about 10% on thought counts.
- **The life checker** (`epitaph verify-life`) applies the same rules, plus typing speed and voice
  metrics, to every recorded life, simulated, rehearsed or real.
