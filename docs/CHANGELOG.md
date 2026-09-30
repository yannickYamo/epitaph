# Changelog

## Phase 0a (integrator)
- Contracts in code: `types.py`, `config.py` (profiles with `extends`, fractional and end-anchored keyframes, overlays, fail-fast validation), `clock.py` (Real, Fake, Rehearsal clocks; Schedule with recall and CPU share held until reloads), `costmodel.py` (`epitaph estimate`: the thought-count rule), `state.py`, `events.py` (bus with per-subscriber bounded queues, control channel), `backend/base.py`, `body/base.py`, fakes, `sim.py` (reference loop), `cli.py`.
- Profiles retimed by the cost model: `compressed-2700` failed rules (a) and (b) with estimated costs; decline moved to 20:00, reload 2 to end-20:30, erosion from end-13:00. `skeleton-1200` last change moved to 16:00.
- Protocol version 1.
