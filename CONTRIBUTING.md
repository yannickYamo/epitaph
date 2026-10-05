# Contributing

Thanks for your interest. *epitaph* is an art piece first, so changes to what the viewer sees (the
prompt, the schedule, the pacing, the display) are artistic decisions; open an issue to discuss
them before sending code. Everything else (bugs, tests, new displays, new hardware, docs) is
welcome directly as a pull request.

## Setup

```sh
make venv        # Python 3.11+ virtual environment with dev and display extras
make check       # the merge gate: ruff, pyright, tests with coverage, simulated lives, the cost model, the small chips
```

No Raspberry Pi and no model are needed: `epitaph sim` runs lives on a fake model and a virtual
clock, and `epitaph run --backend fake --display terminal --clock fake --profile pi4/default`
runs the real controller the same way. Tests that need the Pi or a real model are marked `pi`
and `model` and are skipped by default.

## `make check`

`make check` is the merge gate. CI runs its steps, except `make sim-profiles`, on Python 3.11,
3.12 and 3.13:

| Target | What it runs |
|---|---|
| `make lint` | `ruff check` and `ruff format --check` on `src`, `tests`, `tools` and `badge` |
| `make type` | pyright |
| `make test` | pytest with coverage (the Pi and model tests are deselected) |
| `make sim` | two simulated `pi4/default` lives |
| `make sim-profiles` | one simulated life of every other Pi 4 and Pi 5 profile |
| `make estimate` | the cost model on every Pi 4 and Pi 5 profile: each must pass the thought-count rule |
| `make badge` | the small-chip editions: the C engine against the float model, then whole simulated lives |

`make faults` runs the fault-matrix rows that use the fakes on their own. The Makefile sets
`PYTHONPATH` to the checkout's `src/`, so each checkout tests its own code even when the
virtual environment is shared.

## Standards

- **`make check` must pass.**
- **Types everywhere.** pyright runs in strict mode on the core (`mind/`, `afterlife/`, `clock.py`,
  `pacing.py`, `state.py`, `events.py`, `controller.py`, `exhibit.py`, `costmodel.py`, `verify.py`)
  and basic mode elsewhere.
- **Coverage:** at least 90% on the strict modules, 80% overall.
- **Every public module, class and function has a docstring** (ruff `D1`). Say what it does and any
  non-obvious contract: units, invariants, when it raises.
- **Unit tests never touch the network or real time.** Use `FakeClock` or the virtual event loop.
- **Every bug fix comes with a regression test.**
- **The art lives in config.** Timing, text and thresholds belong in `config/`, not in code. A
  new key gets a row in [docs/CONFIG.md](docs/CONFIG.md); a test fails until it has one.
- Small commits with imperative subjects.

## Proposing a contract change

The interfaces between modules are contracts: the event protocol, the
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

[docs/DESIGN.md](docs/DESIGN.md) explains the piece, [docs/DECISIONS.md](docs/DECISIONS.md) why it
is built this way, and [docs/CONFIG.md](docs/CONFIG.md) lists every setting. The code is in
`src/epitaph/`:

| Module | Role |
|---|---|
| `cli.py` | The `epitaph` command and its subcommands |
| `config.py`, `types.py` | Loading and validating the configuration; shared dataclasses and enums |
| `clock.py`, `costmodel.py` | Life clocks and the schedule; the cost model behind `epitaph estimate` |
| `controller.py`, `pacing.py`, `exhibit.py` | The life loop, the pace of the text, exhibition hours |
| `mind/` | Memory, the prompt and its readings, sampling, cleaning of the words |
| `backend/` | The model: the contract, a fake, llama-server, the model files |
| `body/` | The machine: cgroups, the CPU clock, the network block, the world, heat, watchdog, selftest, calibration, probe |
| `events.py`, `state.py`, `transcript.py` | The event bus and control channel; counter, instance lock and status; the recorded life |
| `display/` | Layout, drivers, the remote view, replay |
| `afterlife/` | Each life's last words, kept in the outbox |
| `sim.py`, `rehearse.py`, `verify.py` | Simulated lives, rehearsed lives with a real model, the life checker |

Writing a new display (a hardware panel, a web view, a printer) needs no change to the core: see
[docs/WRITING_A_DISPLAY.md](docs/WRITING_A_DISPLAY.md).

## Working on a real Pi

- Read the "Lessons" in [docs/PI_FACTS.md](docs/PI_FACTS.md) first (drop-in naming, detached
  units for long jobs, the power supply).
- The tools reach the Pi over SSH; set `PI_HOST` to your SSH alias for it.
- One Pi, one lock. Run every command that touches the Pi, read-only probes included, as
  `tools/pi_lock.sh run <name> <minutes> -- <command>`; `tools/pi_lock.sh status` shows who
  holds it.
- `<minutes>` is a hard limit: a command that overruns is stopped and the lock released. A
  waiter gives up after four hours (exit code 75).
- Hold the lock per job, not per session. Start long jobs as a detached unit on the Pi
  (`systemd-run`) and poll them inside one hold, so a dropped SSH session cannot kill them.
- The read-only collectors (`tools/collect_lives.sh`, `tools/soak_sample.sh`) leave the running
  service alone and do not take the lock. `tools/laptop_lock.sh` does the same for real-model
  runs on a development machine.
- Never put a password or Wi-Fi secret in a command argument, a log or a commit.
