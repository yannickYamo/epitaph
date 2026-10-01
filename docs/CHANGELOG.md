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

