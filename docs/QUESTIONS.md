# Questions

Anyone appends. Each question states the default already in use (BUILD_PLAN 0.8).

| # | Who | Question | Default in use | Answer |
|---|---|---|---|---|
| 1 | L | Readings interpolate recall between keyframes (e.g. 1280 → 1000 from 12:00 to 22:00), so "memory N (was M)" appears on almost every reading. Should small budget moves be reported? | B decides in P0b: report "(was X)" for memory only when a trim actually forgot something or at a reload | |
| 2 | C | Which user is "the service user" for the controller (Delegate=yes unit, S3b)? | `pi`: llama.cpp lives in ~pi, `/var/lib/epitaph` is owned by `pi`. A dedicated `epitaph` system user can come with `install.sh` (C10) | |
| 3 | C | Laptop incident, 0b: an early `make_body` unit test ran `CgroupBody.delegated()` on the real laptop; the terminal's scope (`vte-spawn-13670d8e-….scope`) is delegated to the user, so its processes were moved into a `supervisor/` child and an empty `creature/` child was created (no limits set; accounting only). Fixed in code (only `epitaph*` units are adopted; tests use a fake cgroupfs). My cleanup was refused by the permission classifier. | Harmless and gone when that terminal closes. To clean up now: `echo "-memory -cpu -io" > <scope>/cgroup.subtree_control`, move the pids from `supervisor/cgroup.procs` back to `<scope>/cgroup.procs`, `rmdir supervisor creature` | |
