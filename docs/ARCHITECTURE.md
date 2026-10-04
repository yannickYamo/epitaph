# Architecture

See BUILD_PLAN section 4 for the picture. Code map (phase 0a):

| Module | Role |
|---|---|
| `types.py` | Shared dataclasses and enums |
| `config.py` | default.toml + hardware overlay + profile (+ `extends`) + models; validation |
| `clock.py` | Life clocks; `Schedule.at(t) -> Knobs` |
| `costmodel.py` | Thought-by-thought life estimate; thought-count rule |
| `events.py` | JSON-lines bus on 127.0.0.1:7707; control commands |
| `state.py` | Atomic writes, life counter, instance lock, status |
| `sim.py` | Reference life loop on the fakes (replaced by the controller in P1) |
| `backend/` | `base.py` contract; `fake.py`; `llama_server.py` |
| `body/` | `base.py` contract; `fake.py`; cgroups and vitals |
| `mind/`, `pacing.py`, `controller.py` | Memory, prompt, words, cadence, the loop |
| `display/` | Layout and drivers, remote view, replay |
| `verify.py` | Life checker and rehearsal metrics |
