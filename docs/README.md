# Documentation

## Start here

| Document | What it answers |
|---|---|
| [DESIGN.md](DESIGN.md) | What the piece is, and how each artistic principle becomes an engineering constraint |
| [DECISIONS.md](DECISIONS.md) | Why the system is built the way it is: 22 decision records with their evidence and trade-offs |
| [PERFORMANCE.md](PERFORMANCE.md) | What we measured on the Raspberry Pi 4, what we changed, and what it bought |

## Specification and evidence

| Document | Contents |
|---|---|
| [BUILD_PLAN.md](BUILD_PLAN.md) | The full specification: life cycle, schedule, contracts, test strategy, acceptance criteria, review record |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Module map and ownership |
| [SPIKE.md](SPIKE.md) | Every risky assumption, measured on the Pi 4 with a go criterion set in advance |
| [PROFILES.md](PROFILES.md) | How each life schedule was fitted to measured costs |
| [PROMPT_LOG.md](PROMPT_LOG.md) | Every change to the prompt and sampling, with the metrics it produced |
| [CHECKPOINT_A.md](CHECKPOINT_A.md) | The model and persona choice, with real transcripts from rehearsed lives |
| [GATES.md](GATES.md) | Every acceptance criterion and the command that proves it |
| [WRITING_A_DISPLAY.md](WRITING_A_DISPLAY.md) | How to build a new display on the event stream |

## Operating the Pi

| Document | Contents |
|---|---|
| [PI_FACTS.md](PI_FACTS.md) | The target machine, and lessons learned operating it |
| [PI_CHANGES.md](PI_CHANGES.md) | Every system change made to the Pi |
| [PI_LOCK.md](PI_LOCK.md) | How work on the single Pi is serialised |

## Process record

The project was built by a team of AI coding agents working in parallel from the build plan.
Their phase reports, open questions and contract proposals are kept in [process/](process/):
[reports](process/reports/), [QUESTIONS.md](process/QUESTIONS.md),
[CONTRACT_CHANGES.md](process/CONTRACT_CHANGES.md). [CHANGELOG.md](CHANGELOG.md) summarises each
phase.
