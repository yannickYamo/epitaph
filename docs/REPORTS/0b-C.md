# Phase 0b, round 1: agent C (body and Pi)

Branch `ws/c-body`. The Pi ran on the official 5.1 V / 3 A supply: `vcgencmd get_throttled`
was `0x0` before and after every load test in this round.

## Built

- **C1 `tools/pi_bootstrap.sh`**: reproduces the Pi part of 8.6 and the step-0 log in
  `PI_CHANGES.md`. It covers sudoers, the Wi-Fi country and rfkill, Wi-Fi powersave and
  priority, cable `never-default`, cloud-init off, no credential left in `user-data`,
  hostname and `/etc/hosts`, avahi on `wlan0`, the cmdline flags, console boot, the
  watchdog (checked; a drop-in only if the OS default disappears), the persistent journal
  `90-` drop-in (checked against the effective config), the timezone, NTP, sshd
  `10-epitaph.conf` (checked with `sshd -T` on both links), the state dirs and `nftables`.
  - Modes: `--check` (exit 1 on drift), `--apply`, and `--state`, which prints the effective
    state so two dumps can be diffed. `--local` runs it on the Pi.
  - It reboots only when a boot-time item changed, then re-checks. `--no-reboot` skips the
    reboot.
  - It handles no secrets. For the Wi-Fi connect, the password and the first sudoers entry
    it prints the command for Yannick to run in his own terminal.
- **C1 `docs/PI_LOCK.md`**. `sd_backup.sh` and `sd_restore.sh` are unchanged: L tested the
  restore into a loop file in step 0.
- **C2 spikes**: `tools/spike/s3_probe.py` and `tools/spike/s3_run.sh`. Each spike runs as a
  throwaway `Delegate=yes` unit (`epitaph-spike-*`, user `pi`) and uses the real body code.
  The raw JSON is in `tools/spike/s3_results/`, the write-up in `docs/SPIKE.md` (C sections).
- **C3 `src/epitaph/body/cgroup.py`**:
  - `CgroupBody`: supervisor leaf; `+memory +cpu +io`; a creature leaf with
    `memory.swap.max=0` and `memory.oom.group=1`; a spawn wrapper (`sh` joins
    `cgroup.procs`, then `exec taskset -c 1-3 …`, so the pid is kept); `cpu.max` from the CPU
    share; the death `memory.max` (0.5 × anon, once per life); progress counters;
    `cgroup.kill`; `death_cause` from `memory.events`.
  - `PlainBody` for the laptop, the `make_body(cfg)` factory and `drop_page_cache()`.
  - It adopts only a unit named `epitaph*`.
- **C4 `src/epitaph/body/vitals.py`**: CPU temperature, throttling bits (sysfs, then
  `vcgencmd`, cached), meminfo, and `machine_facts()` (board name, cores, nominal GB).
- **Tests**: `tests/unit/test_body_cgroup.py` and `test_body_vitals.py` run against a fake
  cgroupfs and fake `/sys` in tmp dirs. `tests/pi/test_body_pi.py` is marked `pi`: it
  checks for bootstrap drift and runs S3b as a regression test.
- **`config/hardware/pi4-4gb.toml`**: `load_mode = "dio"`, `mmap = false`,
  `death_mode = "oom"`, `death_fraction = 0.5`, `cpu_share = true`, `cpu_period_us`.

## Tested

| Command | Result |
|---|---|
| `make check` | green: ruff, pyright 0 errors, 86 passed / 2 deselected, total coverage 90%, sim and estimate PASS on every Pi 4 profile. Body coverage: `cgroup.py` 98%, `vitals.py` 98% |
| `pi_lock run C -- pi_bootstrap.sh --check / --state / --apply --no-reboot / --state / --apply / --state` | `changed: 0, drift: 0, failed: 0`; no reboot; the three state dumps are **identical** |
| Drift test: timezone set to UTC and the journald drop-in deleted by hand, then `--apply` | `--check` reported both as DRIFT (rc 1); `--apply` gave `changed: 2`; effective state equal to the reference |
| `s3_run.sh s3b` (Pi) | every step ok: 0.033 s OOM kill, 0.016 s `cgroup.kill`, nft blocks the creature's outbound traffic and allows the supervisor and localhost |
| `s3_run.sh s3` ×4 (Pi, 3B Q4_K_M) | see the spike numbers below; the first run was invalid (see below) |
| `s3_run.sh s3c` (Pi) | speed tracks `cpu.max`; worst gap 2.79 s |
| `systemd-run --user … s3_probe.py s3b` (laptop dry run) | every step ok except `io.stat`: the user manager does not delegate `io` |
| `pytest -m pi tests/pi` under the lock | 2 passed in 12.3 s (bootstrap no drift; S3b regression). Before it: no `epitaph-*` units, no nft tables, no epitaph cgroups left on the Pi; `get_throttled` 0x0 |

## Spike numbers (details in docs/SPIKE.md)

- **S3b: GO.** Delegation works without root. The nft rule `socket cgroupv2 level 3
  "system.slice/<unit>/creature" oifname != "lo" reject` refuses the creature's outbound
  traffic (TCP ECONNREFUSED, DNS fails). The supervisor and localhost still work. nft binds
  the cgroup *id* when the rule loads, so the creature leaf must be kept (C8).
  **Network result: blocked.**
- **S3c: GO.** 2 threads, 3B Q4_K_M, from `cpu.max` 200% down to 70%: 1.751, 1.488, 1.225,
  0.965, 0.755 and 0.579 tok/s. That is 1.00, 0.85, 0.70, 0.55, 0.43 and 0.33 of the 200%
  speed, against 1.00, 0.85, 0.70, 0.55, 0.45 and 0.35 ideal. The worst token gap is 2.79 s
  at 70% (limit 20 s). **cpu_share = true**, and threads stay at 2.
- **S3: GO, `death_mode = "oom"`.** With the limit below the anonymous memory:
  - `dio`: 5/5 kills in 0.33-0.35 s, and 5/5 in 0.34-0.38 s through `CgroupBody`.
  - `none`: 5/5 in 0.72-0.74 s.
  - mmap through `CgroupBody`: 5/5 in 0.81-0.83 s.

  With mmap and a limit at 0.5 × `memory.current`, which is above anon, 0/5 died in 30 s:
  the creature thrashed and re-read 1.2 GB per 30 s from the card.
  **mmap mode: `--load-mode dio`** (weights in anon, no page-cache copy: 2.26 GB against
  3.5 GB for `none`; load 51.6 s against 48.9 s).
- **Eviction probe** (mmap, `memory.high` below a 2237 MB working set): at −1% (22 MB) the
  speed falls to 23%; at −5% it falls to 4.6%, with 5.2 GB read in 162 s. This confirms V3:
  no gradual squeeze on this card.

## Left

- C3 is started, not finished for P1:
  - The controller must still call `make_body()` and `apply()`.
  - The spawn path must be wired into A's `llama_server.py`: `wrap_spawn`, then
    `drop_page_cache` when mmap is used.
  - The body keeps no life number in its log lines yet.
- C5 (units and watchdog), C6 (selftest), C7 (calibrate), C8 (netblock), C9 (thermal) are
  not started. The S3b rule and probe are their base.
- `pi_bootstrap.sh` never exercised its reboot path on the Pi, because nothing needed one.
  It also never exercised the Wi-Fi, cmdline or cloud-init fix branches, since those were
  already in place. Only the check branches ran for them.
- The Pi `pi` password is still locked. The bootstrap prints Yannick's command
  (`ssh -t pi sudo passwd pi`).
- 2 of the 5 non-mmap S3 trials the card asked for were first run with the wrong flag. Every
  mode that counts now has 5/5, but S3 used the 3B Q4_K_M only. S1a (A) should confirm the
  step-0 Q6_K with `dio` fits and still leaves 300 MB free.

## Contract proposals (docs/CONTRACT_CHANGES.md)

1. `[body]` keys `death_limit_mb`, `death_fraction`, `cpu_period_us`.
2. `make_body(cfg) -> Body` as the controller's only factory. The controller unit's name
   must start with `epitaph`.
3. `[backend] load_mode` (`--load-mode`). b11277 rejects `--no-mmap`. `pi4-4gb` sets `dio`.
   For mmap, call `drop_page_cache(model)` before the spawn.

## Questions (docs/QUESTIONS.md)

- Q2: which user is the service user? Default: `pi`.
- Q3, **please read**: a laptop incident. An early unit test ran `CgroupBody.delegated()` for
  real. The laptop terminal's delegated scope (`vte-spawn-13670d8e-….scope`) had its
  processes moved into a `supervisor/` child, and an empty `creature/` child was created,
  with no limits set.
  - The code now refuses any unit not named `epitaph*`, and the tests only use a fake
    cgroupfs.
  - My cleanup of the scope was refused by the permission classifier, so it is still there.
  - It is harmless and disappears when that terminal closes. The cleanup commands are in Q3.

## Notes for other agents

- **A**: prompt processing at 2 threads on the Pi measured about 2.3 tok/s for a 55-token
  prompt at full share. That scales down with the share: about 0.6 tok/s at 70%. Please
  check this in S1b/S2t. If it holds, late re-reads must stay very small, and the
  first-token hang limit must use the share-scaled prompt speed.
- **A**: `dio` loads are always cold (about 51 s for 2 GB). S4 should time reloads with
  `dio`.
- **B**: mmap warm loads take 4 s, but they cannot die reliably. Use `dio` loads, and keep
  the cost model's load estimate at 45-60 s.
