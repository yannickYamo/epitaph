# Contract changes

Agents propose changes to section 6 contracts here during a workflow round (BUILD_PLAN 0.3, 8.3).
The integrator decides between rounds, bumps `PROTOCOL_VERSION` in `types.py` for event changes,
and records the decision in CHANGELOG.md.

| # | Round | Agent | Proposal | Why | Decision |
|---|---|---|---|---|---|
| 1 | 0b r1 | C | Config `[body]`: add `death_limit_mb` (int, optional), `death_fraction` (float, default 0.5), `cpu_period_us` (int, default 100000). Defaults live in `CgroupSettings`; `config/hardware/pi4-4gb.toml` sets the values S3 proved | The death `memory.max` level and the CFS period are machine settings the spikes decide (8.5 S3, S3c) | |
| 2 | 0b r1 | C | `epitaph.body.cgroup.make_body(cfg) -> Body` is the one factory the controller calls (cgroup body on a Pi, plain body on the laptop; a Pi without a delegated `epitaph*` cgroup is an error). The controller unit's name must start with `epitaph` | Keeps the controller free of body details; the name guard stops the body from adopting a terminal's delegated scope | |
| 3 | 0b r1 | C | Config `[backend]`: add `load_mode` (`"auto" \| "mmap" \| "none" \| "dio"`), passed as `--load-mode`; `mmap = false` alone must become `--load-mode none`, never `--no-mmap`. `pi4-4gb` sets `load_mode = "dio"`. For `mmap`, the backend calls `epitaph.body.cgroup.drop_page_cache(model)` before spawning | llama.cpp b11277 rejects `--no-mmap` ("invalid argument"). S3: `dio` puts the weights in anonymous memory with no page-cache copy (2.26 GB vs 3.5 GB for `none`) and dies in 0.35 s; mmap pages cached by another cgroup are invisible to the creature's limit | |
