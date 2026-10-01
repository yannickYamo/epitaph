# Changelog

## Phase 0a (integrator)
- Contracts in code: `types.py`, `config.py` (profiles with `extends`, fractional and end-anchored keyframes, overlays, fail-fast validation), `clock.py` (Real, Fake, Rehearsal clocks; Schedule with recall and CPU share held until reloads), `costmodel.py` (`epitaph estimate`: the thought-count rule), `state.py`, `events.py` (bus with per-subscriber bounded queues, control channel), `backend/base.py`, `body/base.py`, fakes, `sim.py` (reference loop), `cli.py`.
- Profiles retimed by the cost model: `compressed-2700` failed rules (a) and (b) with estimated costs; decline moved to 20:00, reload 2 to end-20:30, erosion from end-13:00. `skeleton-1200` last change moved to 16:00.
- Protocol version 1.

## Checkpoint A (2026-09-30)
- The owner chose **Qwen3 4B Instruct 2507**, the owner's original persona and chat mode, after reading
  rehearsed lives of eight models (docs/CHECKPOINT_A.md).
- The 4B runs on the Pi 4 on its own schedule with the memory carried across reloads: 29 thoughts
  in the rehearsed hour, reload silences 101 s and 116 s.
- Next: a voice closer to Latent Reflection's (introspective, questioning, poetic, facing its end
  without announcing it), then the schedule fitted to the 4B for every profile.

## The voice and the 30-minute life (2026-09-30)
- A thin prompt (50 words) and quiet readings: the full picture at birth, then only what was
  taken (ADR-023; prompt log round 4).
- Material readings: a reading quotes the opening words of each forgotten thought, and after a
  reload, five of its own words as the degraded weights continue them (ADR-026).
- The life is 30 minutes; thought-count minimums are set per profile (ADR-024, PROFILES.md).
- The CPU clock is a decay knob, linear in speed on the Pi 4 (spike S7, ADR-025).
- No temperature in the readings; silent word penalties against clichés.
- Blind panel of three judge models: the three new 30-minute lives scored 39.3, 29.7 and 29.0 of
  60 against 23.7 for the best one-hour life (prompt log round 5).


## Phase 1: the walking skeleton on the Pi (2026-10-01)
- The controller: the life loop with every death cause, hang detection, a watchdog that
  tracks the loop's progress, recovery after a power cut, the control channel; the simulator
  and the rehearsal run on it.
- Installed as systemd services on the Pi (`deploy/install.sh`, `tools/pi_deploy.sh`), with a
  narrow helper for the CPU clock and `epitaph selftest`.
- The local display starts only when a screen is connected; the remote view works over SSH.
- Gate G1 passed on the Pi: a smoke life, two 20-minute lives, the remote view live, a headless
  reboot (docs/process/reports/1-L.md).

## Phase 2 closed, phase 3 and offline (2026-10-01)
- Gate G2 on the Pi: three consecutive 30-minute lives pass at the full level; the fault
  matrix (network block, crash, hang, controller killed, two controllers) passes.
- Fixes from real lives: each keyframe's clock and CPU share applied on time; the last words
  on screen within the display limit after a death; a letter ceiling at 2-bit.
- Offline: the Pi boots and lives with no network; a screen plugged in later starts the
  display; each life's last words are kept on disk for future posts (docs/AFTERLIFE.md).
- Exhibition hours; the arm64 install test; the soak tools; user docs (README, CONFIG,
  INSTALLATION, CONTRIBUTING).
- The original persona keeps its knowledge of its end to the last erosion step (ADR-011);
  no 25-hour soak (ADR-029).
