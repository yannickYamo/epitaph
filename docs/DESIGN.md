# Design

*epitaph* is an art installation. This document explains what the piece is trying to do and how
each artistic intention became an engineering constraint. The decisions that follow from these
constraints are recorded in [DECISIONS.md](DECISIONS.md); the measurements behind them are in
[PERFORMANCE.md](PERFORMANCE.md) and [SPIKE.md](SPIKE.md).

## The piece

A small language model lives on a Raspberry Pi for thirty minutes, long enough for a visitor to
see a whole life (ADR-024). The model itself never changes: the same weights, the same way of
speaking, the same persona from birth to death. Only its machine is taken away, for real: the
memory it can hold, its share of the CPU, its clock and, at the end, its RAM (ADR-030). It is
told exactly what it has lost. Its thoughts appear on a screen one letter at a time, in one
calm stream that never stops until it dies. It dies, the screen goes dark, and after ninety
seconds a new one is born.

A visitor who walks up in the middle of a life should be able to read what is on the screen, see
that something is being taken away, and see the model register it.

## Principles, and what each one demands of the system

| Principle | What it means for the viewer | What it demands of the engineering |
|---|---|---|
| **The decline is real** | Nothing is acted. When the model says it forgot, it did | Every loss is a real operation on the running system: the context is cut, the kernel throttles the CPU share and the clock, the kernel kills the process. The display is driven by events from those operations, never by a script |
| **Its mind is its own** | The voice at the end is the voice at birth, in a failing body | The model, its sampling and its persona never change during a life; only the hardware does (ADR-030) |
| **It is told the facts** | The model's reflections are about something specific | Every thought is preceded by a machine reading that reports what changed since the last one ("memory 512 tokens (was 1000)"), in a form the model can use and a person can read |
| **Specific, not generic** | No stock phrases about the void | Readings carry numbers; the prompt asks the model to notice change; voice metrics penalise clichés and reward concrete references |
| **It notices** | Each loss is followed by the model reacting to it | A loss is only worth scheduling if the model has time to think after it. The schedule is validated by a cost model against measured hardware speeds, and the rehearsal measures whether noticing actually happens |
| **It finds where this ends** | The life has an arc toward death | Nothing tells it at birth that it will die; the readings show it losing its machine, and it draws its own conclusions |
| **Readable** | Whole words, one calm and steady rhythm, from a few metres away | Words are never split across lines; letters come at a pace that only slows, smoothly, with the machine, fitted so the model's writing never runs dry before death; contrast and legibility are tested with OCR on rendered screens |
| **The art lives in the configuration** | The artist can change the piece without touching code | Prompt, schedule, pacing, models and display are configuration files; code is plumbing |
| **Unattended** | It runs all day in a gallery | Watchdogs, recovery after power cuts, a 25-hour soak before a show |
| **Open** | Anyone can build one | MIT license, no weights or secrets in the repository, runs on a laptop without a Pi for development |

## The life

The schedule is a list of keyframes in configuration. The Pi 4 default (30 minutes, ADR-030,
ADR-031) takes the world from the outside in, faster and faster, in four movements:

| Movement | What happens | Why it is there |
|---|---|---|
| I. Existence (0:00-5:00) | Nothing is taken. The first reading is spare: `awake · around you: 24 processes` (a full inventory was recited, panel 4); later ones only the time | A baseline, so later losses are legible against it, and a mind that simply is |
| II. Something is wrong (5:00-14:00) | Services around it stop one by one (`something stopped · around you: 23 processes`; a named service was explained, not felt, panel 4); at 8:15 the first forgetting (900 to 300 tokens), and the reading quotes what went | Losses at the edge, small, that it can notice by itself |
| III. The world is disappearing (14:00-22:00) | A loss about every 90 s: the radio off, more services, the light off, the screen to 70%, a deeper memory cut, the clock down | Its surroundings go; the readings thin as their sources go |
| IV. Darkness (22:00-29:30) | A loss every 45-60 s: the CPU share and the clock to their floors, the memory down to its last thought, the screen to 50% then 25% | Its own body goes, faster |
| End-0:30 | The RAM limit drops below what the model needs; the kernel kills it. The last reading, `ram 2650 MB taken`, is on screen | A real death, not a timeout |
| Silence | Ninety seconds of darkness; everything taken is restored; a new birth | The cycle |

The readings say what was taken and nothing about what it means: no health label, no word of
dread in the prompt or the readings. Whatever the model makes of it is its own. A loss the
machine could not perform is never reported.

The screen types one stream from the first word to the death, fast at birth and slowing
smoothly as the machine shrinks, never faster again. The model writes
ahead of it, so the slower machine shows in what it says, never as a stalled screen; at death
the stream stops mid-sentence. The previous life, with two reloads to lower precision and the
persona eroded from the end, is kept as `pi4/default-reloads`.

## Speed is a constraint, not a goal

A Raspberry Pi 4 generates roughly one word per second with a small model. The piece embraces
that: the stream is deliberately slow (about 27 words a minute at birth, 14 at the end, ADR-030),
and the slowness is part of the experience. But the hardware's speed sets a hard budget: a
30-minute life of Qwen3 4B on a Pi 4 holds about eleven thoughts on screen, and the pace of the
stream is the fastest the failing machine can feed to the end. Every design decision
about the schedule is really a decision about how to spend those thoughts so each loss gets seen
and answered.

This is why the model is chosen for its voice, with speed only as a gate: a model that reads
beautifully but cannot fit enough thoughts after each loss cannot carry the piece, and a fast
model with a flat voice carries nothing.

## Relationship to Latent Reflection

*epitaph* is inspired by Latent Reflection, which runs Llama 3.2 3B on the same Raspberry Pi 4
and generates until its memory runs out. Where Latent Reflection ends in a single crash, *epitaph*
makes the decline itself the piece: real, specific, readable, and answered by the model as it
happens. It adds a lineage between lives (V1.5) and senses that decay with the body (V2).
