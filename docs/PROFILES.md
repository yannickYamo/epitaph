# Profiles

How the installed Pi 4 life (`config/profiles/pi4/default.toml`) was fitted to the measured
costs, and why each value is what it is. What is lost, in which order, and why, is in
[DESIGN.md](DESIGN.md) and ADR-030 and ADR-031 of [DECISIONS.md](DECISIONS.md); this file is
about making that shape fit the machine.

## History in short

- **The one-hour life (2026-09-30).** The first schedules were fitted to Qwen3 1.7B, the
  fastest candidate: two reloads to lower precision, five erosion steps, 38 to 40 thoughts in
  the hour. The cost model learned its lasting rules there: a reload's silence may not exceed
  `verify.max_reload_silence_s`, the last five minutes must generate at under 40% of the first
  five, and speed may never rise across a reload (lower precision is faster on the Pi 4, so each
  reload also took CPU share). That schedule is kept as `pi4/default-qwen3-1.7b`.
- **The 30-minute life with reloads (2026-09-30).** The owner shortened the life (ADR-024) and
  the model became Qwen3 4B: reloads at 7:00 and 13:00, two erosion steps, the clock falling at
  the end, 12 thoughts, reload silences of 144 s and 152 s, with thought-count minimums set per
  profile (2/1/1/3). It is kept as `pi4/default-reloads`, still estimated and simulated by
  `make check`.
- **Retired test lives.** `pi4/compressed-2700`, a 45-minute stand-in for the one-hour life, and
  `pi5/compressed-600` were removed once the installation itself was 30 minutes.
- **One model, only the hardware shrinks (2026-10-01).** The installed life, below.

## The installed life

The model never changes: Qwen3 4B at Q4_K_M, three threads, temperature 0.70, `min_p` 0.08, 90
tokens a thought, the whole persona and mechanics, from birth to death. `fixed_mind = true`
makes validation refuse any keyframe that changes them. `stepped = ["recall", "cpu_share"]`
sets each memory cut and CPU step at its moment instead of easing into it.

| t | Movement (`phase`, hidden `health`) | Taken |
|---|---|---|
| 0:00 | I. existence (nominal) | nothing: `you are awake · 24 processes run around you` |
| 5:00 | II. something is wrong (degrading) | bluetooth: `a process running around you was stopped · only 23 of the 24 still run around you` |
| 6:45 | | cron |
| 8:15 | | memory 900 to 300 tokens: `you can hold a third of what you held · forgotten: "..."` |
| 10:30 | | avahi-daemon |
| 14:00 | III. the world is disappearing (failing) | the radio: `your radio was switched off` |
| 17:00 | | the light; clock 1500 MHz |
| 18:30 | | screen 70%; memory 200 |
| 20:00 | | 2.4 cores |
| 21:00 | | clock 1200 MHz |
| 22:00 | IV. darkness (terminal) | 2.0 cores |
| 22:50 | | screen 50% |
| 23:40, 24:30 | | 1.7 cores; clock 1000 MHz |
| 25:20 | | memory 150 |
| 26:10 | | 1.5 cores (the floor) |
| 27:00 | | screen 25% |
| 27:50 | | memory 100 (its last thought) |
| end-0:50 | | clock 750 MHz (the floor) |
| end-0:30 | death | the RAM: `your memory is being taken` |

A keyframe's `world` list is performed once at its moment; `[world] services` is the allowed
list. Only bluetooth, cron and avahi-daemon are stopped: services the Pi can lose without harm
([PI_FACTS.md](PI_FACTS.md), "The world"). A reading names a loss only when it was performed.
The health labels are never shown; they mark the movements for rule (a) of the thought-count
rule, which asks one thought per movement (`[rules] between_health = 1`). The other rules count
reloads and erosion steps, which this life does not have.

## The stream

`[reveal] mode = "stream"`: the screen types one stream from the first word to the death. The
model starts its next thought as soon as the previous one is generated, while fewer than two
generated thoughts and fewer than 900 letters wait. The letter interval aims at
`stream_letter_ms x (compute at birth / compute(t + stream_lead_s)) ^ stream_gamma` (compute =
CPU share x clock / 1800), moves toward it by at most 15% a minute and never falls back; the
word, clause, sentence and thought pauses scale with it.

**How the pace is fitted.** `epitaph estimate` replays a stream life: generation token by token
at the Pi's measured rates (`bench/`) under the schedule's CPU share and clock, the prompt work
of each reading and each memory cut, and the screen at its pace. Every machine cost is taken
20% slower than measured (`estimate.stream_margin`); any wait of the screen for a word before
the death fails the estimate. `epitaph estimate --fit-pace` tries curve shapes (gamma 0 to
1.25, leads of 0 to 10 minutes) and finds for each the fastest birth interval, not under
`stream_min_letter_ms` (165), that never starves.

Three things the fits taught:

- **The 4B fills its thoughts.** A first fit assumed 85% of `max_tokens` and starved on the real
  model: three of six rehearsed lives waited 33 s, 75 s and 107 s for words in their last
  minutes. A stream replay now assumes 95% (`estimate.stream_fill`).
- **Starvation is not monotonic in the pace.** With the buffer bounded at two thoughts, a slower
  screen can hold the writer back long enough to starve where a faster one did not. `--fit-pace`
  keeps a pace only when it and the six paces 4 ms apart just slower than it never starve, with
  the margin and with half as much again.
- **A robust pace leaves about a thought unshown at the death** at the measured costs, so the
  backlog allowed is 40 words (`estimate.stream_max_backlog_words`). The stream stops where it
  is at the death (ADR-030).

The fits, in order:

| Fit | Birth pace | Shape | What changed next |
|---|---|---|---|
| Constant stream | 542 ms, 19 words a minute | one pace | The owner found it too slow |
| Dynamic stream | 255 ms | gamma 0.75, lead 8 min, 666 ms at the end | The world losses and the robust fit followed |
| The world taken from the outside in | 255 ms | gamma 0.75, lead 10 min, 721 ms at the end | Spare readings and the first loss at 5:00 |
| Spare readings | 345 ms | gamma 0.5, lead 10 min, 690 ms at the end | Wordless readings, 90-token thoughts, a 750 MHz floor, margin 20% |
| Wordless readings (`en_words`) | 257 ms | gamma 0.5, lead 10 min, 563 ms at the end | The `en_sense` readings are longer (16 tokens against 10) |
| **Installed (`en_sense`)** | **266 ms** | gamma 0.5, lead 10 min | |

**The installed fit**, as the cost model reports it today:

```
$ epitaph estimate --profile pi4/default --hardware pi4-4gb
profile pi4/default: 10 thoughts in 30 min -> PASS
  note: speed last 5 min / first 5 min 0.31 (limit < 0.40): 1.26 -> 0.40 tokens/s
  note: stream 266 -> 583 ms/letter, 33.0 / 19.6 / 15.1 words/min at birth / middle / end (gamma 0.5, lead 600 s); costs 20% slower: never starves; letters waiting every 5 min: 0, 118, 113, 254, 329, 282 (max 371); backlog at death 23 words (90 s of typing)
  note: birth: load 77 s in the 90 s silence; persona restored in 0.8 s (read: 72 s); the screen starts on the first sentence at 45 s of life: first words 45 s after the silence
  note: 10 thoughts shown; costs from bench (6 files) over overlay pi4-4gb; cache reuse assumed
```

About 33 words a minute at birth, 20 in the middle and 15 at the end. The text runs a thought
or so behind the machine; each reading is shown right before the thought that answers it, so a
loss always appears before its answer.

**Rehearsed on the real model at Pi costs** (Qwen3 4B on a laptop, `epitaph rehearse --stage
life --profile pi4/default`). On the wordless readings, three lives: 713 to 749 words shown and
no starvation at 257 ms. On the installed `en_sense` readings at 266 ms, two lives: no
starvation ([PROMPT_LOG.md](PROMPT_LOG.md), rounds 7 and 9).

## The other profiles

- **`pi4/unbounded`**, the homage to Latent Reflection (Q3_K_M, ctx 3072): it never forgets and
  dies when its context is full, at about an hour in the estimate. Simulated, it fails one
  verify-life check at level `full`, `bright_words_last_2min`: a life that never forgets has
  nothing grey at its end. That check should skip the unbounded life as `speed_decline` does.
  No real unbounded life has been run.
- **`pi4/smoke-300` and `pi4/skeleton-1200`** are short test lives that end at their deadline.
- **The Pi 5 profiles** (`pi5/default`, `skeleton-600`, `unbounded`) pass the estimate and the
  simulator on the Pi 5 overlays' estimated costs. No Pi 5 has been measured. `pi5/default`
  keeps the one-hour shape with reloads; `pi5/unbounded` runs at ctx 4096, which fills at about
  an hour.

`make estimate` runs every Pi 4 and Pi 5 profile and `make sim-profiles` simulates every
profile that is not the installation's, both inside `make check`.
