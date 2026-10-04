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
clock, and `epitaph run --backend fake --display terminal --clock fake --profile pi4/default`
runs the real controller the same way. Tests that need the Pi or a real model are marked `pi`
and `model` and are skipped by default.

## `make check`

`make check` is the merge gate, and CI runs the same steps on Python 3.11, 3.12 and 3.13:

| Target | What it runs |
|---|---|
| `make lint` | `ruff check` and `ruff format --check` on `src`, `tests` and `tools` |
| `make type` | pyright |
| `make test` | pytest with coverage (the Pi and model tests are deselected) |
| `make sim` | two simulated `pi4/default` lives |
| `make estimate` | the cost model on every Pi 4 profile: each must pass the thought-count rule |

`make faults` runs the fault-matrix rows that use the fakes on their own. The Makefile sets
`PYTHONPATH` to the checkout's `src/`, so each checkout tests its own code even when the
virtual environment is shared.

## Standards

- **`make check` must pass.**
- **Types everywhere.** pyright runs in strict mode on the core (`mind/`, `clock.py`, `pacing.py`,
  `state.py`, `events.py`, `controller.py`, `costmodel.py`, `verify.py`) and basic mode elsewhere.
- **Coverage:** at least 90% on the strict modules, 80% overall.
- **Every public module, class and function has a docstring** (ruff `D1`). Say what it does and any
  non-obvious contract: units, invariants, when it raises. Reference the build plan section when
  the behaviour comes from it, for example `(BUILD_PLAN 5.12)`.
- **Unit tests never touch the network or real time.** Use `FakeClock` or the virtual event loop.
- **Every bug fix comes with a regression test.**
- **The art lives in config.** Timing, text and thresholds belong in `config/`, not in code. A
  new key gets a row in [docs/CONFIG.md](docs/CONFIG.md); a test fails until it has one.
- Small commits with imperative subjects.

## Working in parallel: worktrees and locks

These conventions keep several people working at once out of each other's way.

- **One worktree per line of work.** `tools/worktrees.sh create <name>...` makes
  `../epitaph-wt/<name>` on its own branch `ws/<name>` off `main`, sharing the main checkout's
  `.venv`. Each worktree edits only the files its task owns; files everyone touches (`cli.py`,
  the Makefile, `config/default.toml`) only for what the task adds. Branches merge through a
  pull request with `make check` green.
- **One Pi, one lock.** Every command that touches the Pi, read-only probes included, runs
  inside `tools/pi_lock.sh run <name> <minutes> -- <command>`; `tools/pi_lock.sh status` shows
  who holds it. `<minutes>` is a hard limit. The exceptions are the read-only collectors that
  leave the running service alone (`tools/collect_lives.sh`, `tools/soak_sample.sh`).
- **One llama-server on the laptop.** Real-model runs on the laptop go through
  `tools/laptop_lock.sh` the same way.
- **Long Pi jobs run detached** (a transient systemd unit), so a dropped SSH session cannot kill
  them; the lock holder polls. See [docs/PI_LOCK.md](docs/PI_LOCK.md).

## Proposing a contract change

The interfaces between modules are contracts (BUILD_PLAN section 6): the event protocol, the
control commands, the `Backend`, `Body`, `LifeClock` and `Pacer` interfaces, the configuration
schema and the repository layout. Displays, the life checker and the tools depend on them, so
they change deliberately:

1. Open an issue (or describe it in the pull request): what changes, exactly (field names,
   types, defaults), and why.
2. Code against the current contract in the meantime, or behind a default that keeps old
   readers working (a new event field is optional; a new config key has a default in code).
3. The maintainer decides, applies the change to the contract's owner module, records it in
   [docs/CHANGELOG.md](docs/CHANGELOG.md), and bumps `PROTOCOL_VERSION` in `types.py` when the
   event protocol changes.

## Where things are

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the module map,
[docs/CONFIG.md](docs/CONFIG.md) for every setting and [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md)
for the design.

Writing a new display (a hardware panel, a web view, a printer) needs no change to the core: see
[docs/WRITING_A_DISPLAY.md](docs/WRITING_A_DISPLAY.md).

## Working on a real Pi

- Serialise Pi work with `tools/pi_lock.sh` (above).
- Read the "Lessons from step 0" in [docs/PI_FACTS.md](docs/PI_FACTS.md) first (drop-in naming,
  detached units for long jobs, the power supply).
- Log system changes in [docs/PI_CHANGES.md](docs/PI_CHANGES.md).
- Never put a password or Wi-Fi secret in a command argument, a log or a commit.
