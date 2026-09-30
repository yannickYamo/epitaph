# Contract changes

Agents propose changes to section 6 contracts here during a workflow round (BUILD_PLAN 0.3, 8.3).
The integrator decides between rounds, bumps `PROTOCOL_VERSION` in `types.py` for event changes,
and records the decision in CHANGELOG.md.

| # | Round | Agent | Proposal | Why | Decision |
|---|---|---|---|---|---|
| 1 | 0b r1 | C | Config `[body]`: add `death_limit_mb` (int, optional), `death_fraction` (float, default 0.5), `cpu_period_us` (int, default 100000). Defaults live in `CgroupSettings`; `config/hardware/pi4-4gb.toml` sets the values S3 proved | The death `memory.max` level and the CFS period are machine settings the spikes decide (8.5 S3, S3c) | |
| 2 | 0b r1 | C | `epitaph.body.cgroup.make_body(cfg) -> Body` is the one factory the controller calls (cgroup body on a Pi, plain body on the laptop; a Pi without a delegated `epitaph*` cgroup is an error). The controller unit's name must start with `epitaph` | Keeps the controller free of body details; the name guard stops the body from adopting a terminal's delegated scope | |
