# Architecture

See BUILD_PLAN section 4 for the picture. Code map (phase 0a):

| Module | Role | Owner |
|---|---|---|
| `types.py` | Shared dataclasses and enums | L |
| `config.py` | default.toml + hardware overlay + profile (+ `extends`) + models; validation | L |
| `clock.py` | Life clocks; `Schedule.at(t) -> Knobs` | B (after 0a) |
| `costmodel.py` | Thought-by-thought life estimate; thought-count rule | L |
| `events.py` | JSON-lines bus on 127.0.0.1:7707; control commands | L |
| `state.py` | Atomic writes, life counter, instance lock, status | L |
| `sim.py` | Reference life loop on the fakes (replaced by the controller in P1) | L → B |
| `backend/` | `base.py` contract; `fake.py`; `llama_server.py` (A) | A |
| `body/` | `base.py` contract; `fake.py`; cgroups and vitals (C) | C |
| `mind/`, `pacing.py`, `controller.py` | Memory, prompt, words, cadence, the loop | B |
| `display/` | Layout and drivers, remote view, replay | D |
| `verify.py` | Life checker and rehearsal metrics | E |
