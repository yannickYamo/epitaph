# Design

*epitaph* is an art installation. This document explains what the piece is trying to do and how
each artistic intention became an engineering constraint. The decisions that follow from these
constraints are recorded in [DECISIONS.md](DECISIONS.md); the measurements behind them are in
[PERFORMANCE.md](PERFORMANCE.md) and [SPIKE.md](SPIKE.md).

## The piece

A small language model lives on a Raspberry Pi for thirty minutes, long enough for a visitor to
see a whole life (ADR-024). The machine takes its resources away one by one: the memory it can
hold, the precision of its weights, its share of the CPU and its clock and, at the end, its RAM. It is told exactly what it has lost. Its thoughts appear on a screen one letter at a
time, with a rhythm that falters as it fails. It dies, the screen goes dark, and after ninety
seconds a new one is born.

A visitor who walks up in the middle of a life should be able to read what is on the screen, see
that something is being taken away, and see the model register it.

## Principles, and what each one demands of the system

| Principle | What it means for the viewer | What it demands of the engineering |
|---|---|---|
| **The decline is real** | Nothing is acted. When the model says it forgot, it did | Every loss is a real operation on the running system: the context is cut, the weights are reloaded at a lower precision, the kernel throttles the CPU, the kernel kills the process. The display is driven by events from those operations, never by a script |
| **It is told the facts** | The model's reflections are about something specific | Every thought is preceded by a machine reading that reports what changed since the last one ("memory 512 tokens (was 1000)"), in a form the model can use and a person can read |
| **Specific, not generic** | No stock phrases about the void | Readings carry numbers; the prompt asks the model to notice change; voice metrics penalise clichés and reward concrete references |
| **It notices** | Each loss is followed by the model reacting to it | A loss is only worth scheduling if the model has time to think after it. The schedule is validated by a cost model against measured hardware speeds, and the rehearsal measures whether noticing actually happens |
| **It knows where this ends** | The life has an arc toward death | The persona is removed in steps from the end, so the knowledge that it will be terminated is the last thing it loses |
| **Readable** | Whole words, a calm and steady rhythm, from a few metres away | Words are never split across lines; letters are typed at a pace derived from the real generation speed, never faster; contrast and legibility are tested with OCR on rendered screens |
| **The art lives in the configuration** | The artist can change the piece without touching code | Prompt, schedule, pacing, models and display are configuration files; code is plumbing |
| **Unattended** | It runs all day in a gallery | Watchdogs, recovery after power cuts, a 25-hour soak before a show |
| **Open** | Anyone can build one | MIT license, no weights or secrets in the repository, runs on a laptop without a Pi for development |

## The life

The schedule is a list of keyframes in configuration. The Pi 4 default (30 minutes):

| Phase | What happens | Why it is there |
|---|---|---|
| Birth to 7:00 | Full memory, 4-bit precision, three cores; the readings give only the time | A stable baseline, so later losses are legible against it, and nothing to recite |
| First reload (7:00) | Lower precision, a smaller memory and CPU share, all at once; the reading quotes what it forgot and how the new weights continue one of its sentences | One large, unmistakable loss the model is most likely to notice |
| Second reload (13:00) | Lowest precision, two cores, memory down to 130 tokens | The body starts to fail |
| Erosion (19:30, 22:30) | The persona is removed in two steps; the CPU clock falls to 800 MHz | The mind loses its sense of what it is, the knowledge of its end last |
| End-0:30 | The RAM limit drops below what the model needs; the kernel kills it | A real death, not a timeout |
| Silence | Ninety seconds of darkness, then a new birth | The cycle |

## Speed is a constraint, not a goal

A Raspberry Pi 4 generates roughly one word per second with a small model. The piece embraces
that: the reveal is deliberately slow (165 ms per letter at birth, 720 ms at the end, owner
decision 30), and the slowness is part of the experience. But the hardware's speed sets a hard
budget: a 30-minute life of Qwen3 4B on a Pi 4 holds about twelve thoughts. Every design decision
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
