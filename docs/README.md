# Documentation

## Start here

| Document | What it answers |
|---|---|
| [DESIGN.md](DESIGN.md) | What the piece is, and how each artistic principle becomes an engineering constraint |
| [DECISIONS.md](DECISIONS.md) | Why the system is built the way it is: 31 decision records with their evidence and trade-offs |
| [PERFORMANCE.md](PERFORMANCE.md) | What we measured on the Raspberry Pi 4, what we changed, and what it bought |

## Running it

| Document | Contents |
|---|---|
| [INSTALLATION.md](INSTALLATION.md) | Setting the piece up in a room: hardware, placement, power, cooling, network, exhibition hours, wall label, credits, the install on a Pi |
| [CONFIG.md](CONFIG.md) | Every configuration key, its default and what it does, kept in step with the files by a test |
| [WRITING_A_DISPLAY.md](WRITING_A_DISPLAY.md) | How to build a new display on the event stream |
| [AFTERLIFE.md](AFTERLIFE.md) | Each life's last words, kept on the card for posting later, with no network needed |
| [badge/README.md](../badge/README.md) | The piece on small chips: a Tufty 2350 badge and an ESP32 |

## Specification and evidence

| Document | Contents |
|---|---|
| [BUILD_PLAN.md](BUILD_PLAN.md) | The original specification the piece was built from (historical: the decisions records supersede it) |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Module map |
| [SPIKE.md](SPIKE.md) | Every risky assumption, measured on the Pi 4 with a go criterion set in advance |
| [PROFILES.md](PROFILES.md) | How each life schedule was fitted to measured costs |
| [PROMPT_LOG.md](PROMPT_LOG.md) | Every change to the prompt and sampling, with the metrics it produced |
| [CHECKPOINT_A.md](CHECKPOINT_A.md) | The model and persona choice, with real transcripts from rehearsed lives |
| [GATES.md](GATES.md) | Every acceptance criterion and the command that proves it, including the soak report (`tools/soak_report.py`) |

## Operating the Pi

| Document | Contents |
|---|---|
| [PI_FACTS.md](PI_FACTS.md) | The target machine, the installed services, and lessons learned operating it |
| [PI_CHANGES.md](PI_CHANGES.md) | Every system change made to the Pi |
| [PI_LOCK.md](PI_LOCK.md) | How work on the single Pi is serialised |

## History

[CHANGELOG.md](CHANGELOG.md) summarises each phase. [CONTRIBUTING.md](../CONTRIBUTING.md) describes how to work on the code.
