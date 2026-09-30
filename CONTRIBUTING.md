# Contributing

Thanks for your interest. *epitaph* is an art piece first, so changes to what the viewer sees (the
prompt, the schedule, the pacing, the display) are artistic decisions; open an issue to discuss
them before sending code. Everything else (bugs, tests, new displays, new hardware, docs) is
welcome directly as a pull request.

## Setup

```sh
make venv        # Python 3.11+ virtual environment with dev and display extras
make check       # the merge gate: ruff, pyright, tests with coverage, a simulated life, the cost model
```

No Raspberry Pi and no model are needed: `epitaph sim` runs lives on a fake model and a virtual
clock. Tests that need the Pi or a real model are marked `pi` and `model` and are skipped by default.

## Standards

- **`make check` must pass.** CI runs the same steps on Python 3.11, 3.12 and 3.13.
- **Types everywhere.** pyright runs in strict mode on the core (`mind/`, `clock.py`, `pacing.py`,
  `state.py`, `events.py`, `controller.py`, `costmodel.py`, `verify.py`) and basic mode elsewhere.
- **Coverage:** at least 90% on the strict modules, 80% overall.
- **Every public module, class and function has a docstring** (ruff `D1`). Say what it does and any
  non-obvious contract: units, invariants, when it raises. Reference the build plan section when
  the behaviour comes from it, for example `(BUILD_PLAN 5.12)`.
- **Unit tests never touch the network or real time.** Use `FakeClock` or the virtual event loop.
- **Every bug fix comes with a regression test.**
- **The art lives in config.** Timing, text and thresholds belong in `config/`, not in code.
- Small commits with imperative subjects.

## Where things are

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the module map and
[docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) for the design. Interfaces between modules (events,
backend, body, config schema) are contracts: propose changes in
[docs/CONTRACT_CHANGES.md](docs/CONTRACT_CHANGES.md) as part of your pull request.

Writing a new display (a hardware panel, a web view, a printer) needs no change to the core: see
[docs/WRITING_A_DISPLAY.md](docs/WRITING_A_DISPLAY.md).

## Working on a real Pi

- Serialise Pi work with `tools/pi_lock.sh run <name> <minutes> -- <command>`.
- Read the "Lessons from step 0" in [docs/PI_FACTS.md](docs/PI_FACTS.md) first (drop-in naming,
  detached units for long jobs, the power supply).
- Log system changes in [docs/PI_CHANGES.md](docs/PI_CHANGES.md).
- Never put a password or Wi-Fi secret in a command argument, a log or a commit.
