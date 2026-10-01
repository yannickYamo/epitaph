# Gates

part E signs off every gate here, with evidence (BUILD_PLAN 8.3, 8.4, 11). A gate passes
only when every row is `pass` or an accepted `pending` (hardware not connected yet, 10.2),
and the integrator has run `/code-review high` on the gate diff.

**Status values:** `open` (not checked yet), `pass`, `fail`, `pending` (waits on hardware or
a later phase; never counts as a failure), `n/a` (does not apply to the chosen `death_mode`,
CPU-share or network result). Every `pass` names its evidence: a command with its output
summary, a file, a commit, or a CI run.

**Conventions in the commands:**

- `$PY` = `PYTHONPATH=src .venv/bin/python` in the worktree being checked (as the Makefile).
- Pi commands run through the lock: `tools/pi_lock.sh run E <min> -- <cmd>`. Read-only probes
  still take the lock. `ssh pi` is Wi-Fi, `ssh pi-eth` the cable.
- `verify-life` is `$PY -m epitaph verify-life` or, equally, `$PY -m epitaph.verify`.
  Exit 0 = pass, 1 = a check failed, 2 = usage or config error. It writes `verify.json` next
  to the life's `events.jsonl` (`--no-write` to skip). `--summary` prints the 5.11 metrics as
  one JSON line.
- `<life>` is a life number (resolved in the state dir), a life folder or an events file.
- `verify-life compare <dir>...` verifies every life below the folders (rehearsal runs: one
  folder per life) and prints a Markdown table ranked by the 5.11 metrics, then a per-model
  verdict. Exit 0 when at least `--require-models` models (default 2) meet every threshold,
  1 when fewer do, 2 when a life cannot be read.
- `voice/` (rehearsal transcripts and reports) is untracked (BUILD_PLAN 6.1): paste the
  compare table into the phase report, `docs/process/reports/0c-*.md`, as the evidence.

---

## S0: step 0 (BUILD_PLAN 8.6)

| # | Item | Command that proves it | Status | Evidence |
|---|---|---|---|---|
| S0.1 | Laptop tools: tesseract, qemu-user-static, podman | `tesseract --version; qemu-aarch64-static --version; podman --version` | open | |
| S0.2 | SD backup exists and restore was tested | `ls ~/epitaph-backups/step0-*/` (sha256 files); PI_FACTS "restore tested" | open | PI_FACTS says tested |
| S0.3 | Passwordless sudo | `ssh pi 'sudo -n true && echo ok'` | pass | probe 2026-09-29 22:34 (E, read-only, under the Pi lock): `sudo -n true` ok |
| S0.4 | No password in the repo or scratch files | `git grep -nIi -e 'passw' -- ':!docs/BUILD_PLAN.md'` reviewed by hand; scratch file gone | open | |
| S0.5 | Wi-Fi carries the default route; the cable only behind it (metric 800 since 0c round 2, F12) | `ssh pi 'ip route show default'` shows `wlan0` at metric 600 first | pass, re-check | probe 2026-09-29 22:34 (E, read-only, under the Pi lock): `default via <router-ip> dev wlan0 … metric 600`, the only default route. Re-check once the Pi is back on Wi-Fi: on 2026-09-30 its network was out of range, and the cable route (metric 800) was the only one |
| S0.6 | SSH over Wi-Fi with the cable unplugged | Yannick unplugs the cable; `ssh pi 'hostname'` | open | needs Yannick |
| S0.7 | cloud-init disabled; hostname `epitaph` survives two reboots | `ssh pi 'test -f /etc/cloud/cloud-init.disabled && hostname'` after each reboot | open | probe 2026-09-29 22:34 (E, read-only, under the Pi lock): disabled, hostname `epitaph` (one boot seen; second reboot not by E) |
| S0.8 | `memory` in `cgroup.controllers` (else `death_mode = deadline` recorded) | `ssh pi 'cat /sys/fs/cgroup/cgroup.controllers'` | pass | probe 2026-09-29 22:34 (E, read-only, under the Pi lock): `cpuset cpu io memory pids` |
| S0.9 | Console boot | `ssh pi 'systemctl get-default'` = `multi-user.target` | pass | probe 2026-09-29 22:34 (E, read-only, under the Pi lock) |
| S0.10 | Hardware watchdog on | `ssh pi 'systemctl show -p RuntimeWatchdogUSec --value'` = `1min` | pass | probe 2026-09-29 22:34 (E, read-only, under the Pi lock) |
| S0.11 | NTP synced | `ssh pi 'timedatectl show -p NTPSynchronized --value'` = `yes` | pass | probe 2026-09-29 22:34 (E, read-only, under the Pi lock) |
| S0.12 | Key-only SSH over Wi-Fi; password allowed over the cable only | `ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password pi true` refused | open | |
| S0.13 | Official 5.1 V / 3 A supply: no under-voltage | `ssh pi 'vcgencmd get_throttled'` = `0x0` under load (S1c logs it) | open | probe 2026-09-29 22:34 (E, read-only, under the Pi lock): `0x0`, 0 under-voltage lines in dmesg, uptime 26 min, 42.8 °C, right after A's build job released the lock; sustained load is S1c |
| S0.14 | Repo, `.pi.env`, PI_FACTS, locks exist before agents start | `ls tools/pi_lock.sh tools/laptop_lock.sh docs/PI_FACTS.md; test -f .pi.env` | open | |

## G0: end of phase 0c, checkpoint A (BUILD_PLAN 8.4)

Phase 0c ends when the cost model passes on measured Pi 4 costs, the rehearsal reports exist
and at least two models meet every 5.11 threshold. `<models>` below are the candidates the
rehearsal ranks first (checkpoint A confirms two of them).

| # | Item | Command that proves it | Status | Evidence |
|---|---|---|---|---|
| G0.1 | `make check` green on `main` | `make check` (exit 0) | open | |
| G0.2 | CI green on the merged `main` (3.11, 3.12, 3.13) | the GitHub Actions `ci` run for the merge commit | open | the first public run passed on 3.12 and 3.13 (0c r1) |
| G0.3 | `docs/SPIKE.md` complete (S5 pending) | every row S1a-S6 has numbers and a go/fallback decision: `grep -c '^\| S' docs/SPIKE.md` and a read | open | |
| G0.4 | Every Pi 4 profile passes the thought-count rule on **measured** costs, for every chosen model | Before the measured files move into `bench/`: `$PY .github/scripts/estimate_measured.py --bench bench/measured --models <models> --strict` (exit 0: every profile PASS and the "estimated costs used" column `none`). After the move: the same without `--bench`, and `make estimate` prints `costs from bench (...)` for every profile, never `(estimated)` | open | 0c r1 (E) on `bench/measured`: 38 of 40 profile/model pairs pass; `pi4/compressed-2700` fails rule (a) for llama-3.2-3b-instruct and qwen3-4b-instruct-2507 (2 thoughts between the health changes at 6.5 and 11.2 min). Only qwen3-1.7b has every needed cost measured; the others measure step 0 at 3 threads only, so their `1-3`, `1-2`, `2-2` rates and step 1-2 loads are still estimates |
| G0.5 | `bench/` holds Pi 4 numbers for the chosen models | `ls bench/pi4-<model>-*.json` for each of `<models>`; G0.4's `--strict` run shows no estimated cost | open | |
| G0.6 | Rehearsal reports exist | `ls voice/rehearsal_report.md`; every full rehearsal life has a `verify.json` at level `rehearsal`: `for d in voice/<run>/*/; do $PY -m epitaph.verify "$d" --level rehearsal --hardware pi4-4gb --summary; done` (exit 0 or 1, never 2) | open | |
| G0.7 | Stage 1 (screen) ranked | `$PY -m epitaph.verify compare voice/<screen run> --level screen --hardware pi4-4gb --require-models 0 --out voice/screen_compare.md` (exit 0); the top 2-3 go to full lives | open | |
| G0.8 | At least two models meet every rehearsal threshold | `$PY -m epitaph.verify compare voice/<full run> --level rehearsal --hardware pi4-4gb --require-models 2 --out voice/compare.md` exit 0 and the last line reads `Gate G0, at least 2 models meet every threshold: **met**`. A model counts when every one of its full lives passes. The table goes into the 0c report | open | |
| G0.9 | The rehearsal was charged at measured Pi costs | each summary line's `costs` (from the rehearsal header or `birth_loading`) says measured, or the report states the cost source per model | open | depends on A's header fields |
| G0.10 | Metrics judge with the language pack's lists and the config's thresholds | `$PY -m epitaph.verify <life> --json --no-write \| python -c 'import json,sys; print(json.load(sys.stdin)["metrics"]["word_lists"])'` shows `lang:en` for every list; `grep -A20 '^\[verify\]' config/default.toml` holds every 5.11 threshold | open | 0c r1: every list is `lang:en` except `answering` (`default`: `en.toml` has no `answering` list yet, B) |
| G0.12 | No reload speeds generation up (review 2, F2): on the cost model for every Pi 4 profile and chosen model, and in every full rehearsal life | Cost model: G0.4's `--strict` run fails a pair with `(speed_monotonic)` in its Rule column, even while `[estimate] speed_monotonic = "warn"`; `grep -A3 'speed_monotonic' config/default.toml` shows `"fail"` once the rebased profiles are merged, and `make estimate` then prints no `WARNING speed_monotonic` note. Rehearsal: the G0.8 compare table's "Speed after/before reload" column is at most 1.05 (`verify.speed_monotonic_tolerance`) and never bold for the chosen models | open | 0c r2 (E) on `bench/` (qwen3-1.7b, measured): `pi4/default` and `pi4/compressed-2700` rise from 1.65 to 2.62 tokens/s at reload 1 (+59%, context-aware); reload 2 falls. The rebased profiles come from ws/v-voice |
| G0.11 | Checkpoint A reply recorded (models, persona, chat or diary) | `docs/process/QUESTIONS.md` / CHANGELOG entry; no reply by the end of the session means the two best-scoring models, the v6 persona, chat mode | open | |

## G1: walking skeleton on the Pi (BUILD_PLAN 8.4)

**How the Pi rows run.** Every Pi target takes the Pi lock itself (`AGENT=E` names the
holder) and writes its evidence under `logs/pi/` (untracked), so the phase report
`docs/process/reports/1-E.md` quotes the summary lines and names the folders.

- `make pi-smoke` and `make pi-life` both call `tools/smoke_pi.sh`. In one lock hold, it:
  - finds the Pi (`tools/pi_host.sh`: `pi`, then `pi-eth`) and the installed `epitaph`;
  - refuses while an earlier `epitaph-pilife-*` or `llama-*` unit is still running;
  - stops `epitaph-controller` if it is active (two controllers refuse to run), and starts it
    again at the end, even after a failure or a timeout;
  - runs `epitaph run --profile <P> --lives <N>` as the detached unit
    `epitaph-pilife-<stamp>`, as the controller's user, and polls it;
  - copies the new `lives/NNNNNN` folders and the unit's journal to
    `logs/pi/<stamp>-<profile>/`;
  - runs `epitaph verify-life <life> --profile <P> --hardware pi4-4gb` on each new life,
    adding `--level smoke` for `smoke-300` (other profiles use their own `verify_level`),
    which writes `verify.json` next to each `events.jsonl`.
  - Exit 0 means every life passed. Exit 1 means a life failed or is missing, or the run
    failed.
- With `LIVES=2` the second life is the first one's next life, so its `next_birth` is judged
  (the folders keep the `lives/NNNNNN` layout that verify-life looks up). The last life's
  `next_birth` stays `pending`, which never fails a life.
- `tools/smoke_pi.sh --dry-run` and `tools/headless_boot_check.sh --dry-run` print every
  command without taking the lock or reaching the Pi. CI runs both dry runs, and
  `tests/unit/test_pi_tools.py` runs both scripts against a fake Pi.
- The order for the gate: `make pi-deploy` (C), then G1.4, G1.1 with G1.2 alongside, and
  G1.3 last, because it reboots.
- Budgets (the lock's hard limit): `pi-smoke` 33 min. `pi-life PROFILE=pi4/skeleton-1200
  LIVES=2` 77 min (2 × (20 min + 5 min load + 3 min) + the 90 s silence + 5 min, plus
  15 min for the copy and the checks). `pi-boot-check` 17 min.

| # | Item | Command that proves it | Status | Evidence |
|---|---|---|---|---|
| G1.0 | The simulator runs the real controller on the fakes; the phase 0a reference loop in `sim.py` is deleted (review 2, F7). Until then simulator results are provisional | `grep -c 'def run_life' src/epitaph/sim.py` prints 0 and `grep -n controller src/epitaph/sim.py` shows it driving `controller.py`; `make sim` exit 0 | pass (2026-10-01) | `sim.py` drives `controller.py` (no reference loop); `make sim` green |
| G1.1 | Two consecutive `skeleton-1200` lives pass `verify-life --level skeleton` | `AGENT=E make pi-life PROFILE=pi4/skeleton-1200 LIVES=2` exits 0, and its last line reads `PASS: 2 life(s) of pi4/skeleton-1200`. Re-check on the laptop: `$PY -m epitaph verify-life logs/pi/<run>/lives/<n> --profile pi4/skeleton-1200 --hardware pi4-4gb --level skeleton` (the profile's own level) exits 0 for `<n>` and `<n+1>`, and on `<n>`, `python -c 'import json,sys; print([c["status"] for c in json.load(open(sys.argv[1]))["checks"] if c["name"]=="next_birth"])' logs/pi/<run>/lives/<n>/verify.json` prints `['pass']` | pass (2026-10-01) | lives 000002 and 000003: skeleton PASS, 569 and 584 words, deadline at 1200 s, next birth 154 s; [report](process/reports/1-L.md) |
| G1.2 | The remote view shows them live | During the G1.1 run, under the same lock hold (the tunnel only reads the bus): `script -q -c "$PY -m epitaph display --connect pi --driver terminal" logs/pi/remote-view-<stamp>.txt`. It shows birth, words typed letter by letter, the death and the next birth | pass (2026-10-01) | `epitaph display --connect pi --driver terminal` during life 000002: status strip, letters typed live, 1.2-1.4 tok/s; [report](process/reports/1-L.md) |
| G1.3 | Headless boot: no display crash loop | No screen connected. `AGENT=E make pi-boot-check` exits 0 (it reboots: `tools/headless_boot_check.sh --reboot`, waits for a new `boot_id` over SSH and for `systemctl is-system-running --wait`). It passes when `epitaph-display` is `loaded`, `enabled`, not `failed` or `activating`, has `NRestarts=0` and was skipped by its `ExecCondition` (`Result=exec-condition`; `ConditionResult=no` also counts, for a `Condition*=` line), and `epitaph-controller` is `enabled` and `active`. `REBOOT=0 make pi-boot-check` runs the same checks on the current boot | pass (2026-10-01) | reboot to SSH 31 s; display skipped by its ExecCondition, 0 restarts; controller active, 0 restarts; throttled 0x0; [report](process/reports/1-L.md) |
| G1.4 | `tools/smoke_pi.sh` passes (`smoke-300`, level smoke) | `AGENT=E make pi-smoke` exits 0, and its last line reads `PASS: 1 life(s) of pi4/smoke-300` | pass (2026-10-01) | life 000001: smoke PASS, 4 thoughts, 132 words, deadline at 300 s; [report](process/reports/1-L.md) |
| G1.5 | `/code-review high` done on the gate diff | integrator's review note | pass (2026-10-01) | review of the phase 1 diff: 11 findings, 1-8, 10 and 11 fixed with tests; 9 accepted as ADR-027; [report](process/reports/1-L.md) |

## G2: full decline, checkpoint B (BUILD_PLAN 8.4)

The installation is the 30-minute `pi4/default` life (ADR-024). The 45-minute test life
`compressed-2700` was longer than it and is retired (removed in phase 3, with its Pi 5
counterpart `compressed-600`): G2.3 runs the installation's own profile.

**How the G2 rows run.** Two ways to get lives, both judged by `verify-life` at the profile's
level (`full` for `pi4/default`), both writing `verify.json` next to each life's `events.jsonl`
under `logs/pi/` (untracked); the phase 2 report in `docs/process/reports/` quotes the
summary lines and names the folders.

- `make pi-life` (`tools/smoke_pi.sh`, as in G1): stops the service inside its lock hold, runs
  `LIVES` consecutive lives as a detached unit, starts the service again, copies and judges
  them. Budget for `PROFILE=pi4/default LIVES=3`: 137 min of lock (3 × (30 min + 5 min load +
  3 min) + 2 × 90 s silence + 5 min, plus 15 min for the copy and the checks).
- `make pi-collect` (`tools/collect_lives.sh`): copies the newest `LIVES` finished lives of
  `PROFILE` from the running service (`/var/lib/epitaph/lives`, read-only, no lock, the service
  keeps running), each with the life after it for `next_birth`, to `logs/pi/service-<stamp>/`
  and judges them. It never judges the life in progress and skips `interrupted` lives (a deploy
  or a restart cut them short) unless `--include-interrupted`. `summary.md` holds one line per
  life, the controller's `NRestarts`, and whether the lives are consecutive.
- The fault matrix: `make faults` runs the laptop rows (tests/faults, also part of `make test`);
  `AGENT=E make pi-faults` runs the Pi rows (`tools/fault_matrix_pi.sh`, one lock hold, each
  row through C's `tools/fault_pi.sh <row>`) and writes `logs/pi/faults-<stamp>.md` with each
  row's log in `logs/pi/faults-<stamp>/`. `ROWS=a,b` runs a subset; `--list` names them.

| # | Item | Command that proves it | Status | Evidence |
|---|---|---|---|---|
| G2.1 | Selftest passes under the installed service | `tools/pi_lock.sh run E 10 -- ssh pi /opt/epitaph/venv/bin/epitaph selftest --user pi` exit 0 (it relaunches itself as a transient `Delegate=yes` unit for the service user; its clock round trip moves the CPU clock, so stop `epitaph-controller` around it and start it again after, or take the fault row `AGENT=E make pi-faults ROWS=delegated-cgroups`) | pass (2026-10-01) | `epitaph selftest` 12/12 through `deploy/install.sh` on the deployed phase 2 code (transient Delegate=yes unit as the service user), network check included; [report](process/reports/2-L.md) |
| G2.2 | The fault matrix passes on the Pi (rows that apply) | `AGENT=E make pi-faults` exit 0 and its last line reads `PASS: every row that ran passed`; the table `logs/pi/faults-<stamp>.md` has every `pi` and `native` row **PASS** and the `owner` rows listed (the fault table below, Pi column) | pass (2026-10-01) | `make pi-faults`: creature-network, two-controllers, crash, controller-killed, hang, two-agents all PASS; RAM death and delegated cgroups proven by real lives, calibration and selftest; owner rows wait; [report](process/reports/2-L.md) |
| G2.3 | Three consecutive `pi4/default` lives pass `verify-life --level full` | `AGENT=E make pi-life PROFILE=pi4/default LIVES=3` exits 0 and its last line reads `PASS: 3 life(s) of pi4/default`. Or, from the running service: `make pi-collect PROFILE=pi4/default LIVES=3` exits 0 and `logs/pi/service-<stamp>/summary.md` reads `Consecutive lives: yes`. Re-check on the laptop: `$PY -m epitaph verify-life logs/pi/<run>/lives/<n> --profile pi4/default --hardware pi4-4gb` (level `full`, the profile's own) exits 0 for each `<n>` | open | 2026-10-01 (E): `make pi-collect PROFILE=pi4/default LIVES=1` on the service found one finished life, 000004 (before the on-time clock fix 18e5b09 was deployed): every machine check passes, including the new ones (`death_time` 0.6 s after the squeeze, `reload_targets`, `erosion_steps` 2 of 2) except `speed_decline` 0.40 (1.31 → 0.53 tok/s; the limit is < 0.40): its 600 MHz keyframe at end-2:30 came after its last reading and was never applied. 000005 was interrupted by that deploy; 000006 is the first life on the fixed code |
| G2.4 | `/code-review high` done | integrator's review note | pass (2026-10-01) | review of the phase 2 diff: 10 findings, all fixed with tests; [report](process/reports/2-L.md) |
| G2.5 | Checkpoint B reply ("good" or the list) | QUESTIONS / CHANGELOG | open | needs Yannick |
| G2.6 | Speed never rises across a reload on the Pi (review 2, F2) | In each G2.3 life's `verify.json`, `speed_monotonic` is `pass`: `python -c 'import json,sys; print([c for c in json.load(open(sys.argv[1]))["checks"] if c["name"]=="speed_monotonic"])' logs/pi/<run>/lives/<n>/verify.json` shows a value ≤ 1.05, from `gen_end` rates | open | life 000004: 0.939 (1.22 → 1.14 tok/s at reload 1, 1.17 → 0.94 at reload 2) |

## G3: hardening, checkpoint C = acceptance (BUILD_PLAN 8.4, 11)

| # | Item (section 11) | Command that proves it | Status | Evidence |
|---|---|---|---|---|
| A1 | `make check` green on the laptop and in CI; coverage met; every Pi 4 profile passes `estimate` on measured costs | `make check`; CI run; coverage step (80% overall, 90% per strict module and `verify.py`) | open | |
| A2 | Every life in the soak passes `verify-life --level full` | `for d in /var/lib/epitaph/lives/*/; do epitaph verify-life "$d" --level full; done` on the Pi (or copied lives); `tools/soak_report.py` summary | open | E7 (P3) |
| A3 | The fault matrix passes on the Pi (applicable rows) | the fault table below | open | |
| A4a | Soak ≥ 25 h on Wi-Fi, laptop disconnected: no missed life (every `death_shown` → next birth within silence + load + 5 min) | `tools/soak_report.py` (uses verify-life's `next_birth`) | open | |
| A4b | Zero controller crashes | `journalctl -u epitaph-controller` restarts = 0; `systemctl show epitaph-controller -p NRestarts` | open | |
| A4c | Controller memory growth < 20 MB; disk < 100 MB/day | `soak_report.py` from `status.json` / `ps` samples and `du -s /var/lib/epitaph` | open | |
| A4d | No under-voltage bits; throttling or thermal pauses < 10% of the time | `soak_report.py` over logged `vcgencmd get_throttled` | open | |
| A5 | Power on to first shown word ≤ `first_word_after_boot_s` (240 s) | reboot test: boot time from `journalctl --list-boots`, first `word` event `ts` | open | |
| A6 | `install.sh` idempotent on the Pi and in a clean arm64 Debian container | `deploy/install.sh` twice on the Pi (second run changes nothing); `podman run --arch arm64 debian:trixie ... install.sh` | open | |
| A7 | `epitaph sim` and `epitaph run --backend fake --display terminal` work with no model | `$PY -m epitaph sim --profile pi4/default --hardware pi4-4gb`; `$PY -m epitaph run --backend fake --display terminal --lifespan 2:00` | open | `sim` passes today; `run` is B's P1 |
| A8 | `pi4/unbounded` and the Pi 5 profiles pass simulation; `pi4/unbounded` passes one real life | `make sim-profiles` and `make estimate` (both in `make check`: every Pi 5 profile on both overlays); one Pi life + `verify-life --level full` | open | simulation and estimate pass (phase 3, B9): `pi4/unbounded` dies `full` at 61 min in the sim (context full at 59 min in the estimate, ctx 3072); `pi5/default` `oom`, `pi5/skeleton-600` `deadline`, `pi5/unbounded` `full` (about 62 min, ctx 6144) on `pi5-8gb` and `pi5-16gb`. The real life waits for the soak; verify-life's `bright_words_last_2min` fails a simulated unbounded life (it never forgets, so nothing fades) and needs the unbounded skip that `speed_decline` has (E) |
| A9 | D13 passes at every tested resolution | `$PY -m pytest -m display tests/display` (CI step "Headless display tests") | open | D5 (P1) |
| A10 | `sd_restore.sh` has restored an image at least once | C's log in PI_CHANGES / report | open | |
| A11 | Checkpoint B read on the remote view | Yannick | open | |
| A12 | Checkpoint C: soak report acceptable | Yannick | open | |
| A13 | `/code-review high` done | integrator | open | |
| later | S5 and checkpoint B's physical part when a screen is connected | D8 | pending | no screen (10.2) |

---

## Fault matrix (BUILD_PLAN 10.4)

Laptop rows run on the fakes in `tests/faults/test_fake_faults.py` (`make faults`): each
injects the fault into the real controller on the installation's profile, `pi4/default`, and
lets verify-life judge the recorded life; `ROWS` in that file maps each 10.4 row to its test.
Pi rows run through `tools/fault_matrix_pi.sh` (`make pi-faults`; `pi:<row>` below is its row
name): `pi` rows call C's `tools/fault_pi.sh <row>` (one row per call, a line that starts with
`PASS` or `FAIL`, exit 0 or 1; exit 2 for a row it does not know; it must not take the Pi lock
when `EPITAPH_PI_LOCKED=1`, because the driver holds it), `native` rows are the driver's own,
and `owner` rows need a person or a fresh SD image and are never run by an agent (no power cut
and no reboot until then). `tests/unit/test_fault_matrix_pi.py` checks that this table, the
laptop tests and the driver's rows agree.

| Fault | Expected | Laptop (fakes) | Pi | Status |
|---|---|---|---|---|
| RAM death (`death_mode = oom`) | `cause=oom` within 10 s; next life after the silence | `test_fake_faults.py::test_oom_death_at_the_squeeze` (and `death_time` on every full life) | `pi:ram-death`; every service life (`make pi-collect`) | laptop pass |
| Delegated cgroups | every S3b step passes | | `pi:delegated-cgroups` (`epitaph selftest` under the service, G2.1) | open |
| Creature network blocked | refused | | `pi:creature-network` | open |
| Crash | `cause=crash`; next life | `test_fake_faults.py::test_crash_is_recorded_and_fails_verify` | `pi:crash` (`kill -9 <creature>`) | laptop pass |
| Hang | `cause=hang` after the timeout; cgroup killed | `test_fake_faults.py::test_hang_kills_the_creature_and_the_next_life_follows` | `pi:hang` (`kill -STOP <creature>`) | laptop pass |
| Slow first token at low CPU share | no false `hang` | `test_fake_faults.py::test_slow_first_token_at_low_cpu_share_is_not_a_hang` | `pi:slow-first-token` (CPU share 0.7, 1000-token prompt) | laptop pass |
| Waiting on the SD card | no false `hang` | `test_fake_faults.py::test_waiting_on_the_sd_card_is_not_a_hang` | `pi:sd-card-wait` (`memory.high` probe 60 s) | laptop pass |
| Full context (`unbounded`) | `cause=full` | `test_fake_faults.py::test_full_context_in_a_small_ctx` | `pi:full-context` (small-ctx test profile) | laptop pass |
| Reload longer than a keyframe gap | `reload_skipped`, current target loaded | `test_fake_faults.py::test_reload_longer_than_a_keyframe_gap` | `pi:reload-longer-than-gap` (cold reload) | laptop pass |
| Deadline during a reload | `cause=deadline`, nothing left running | `test_fake_faults.py::test_deadline_during_a_reload` | `pi:deadline-during-reload` (short lifespan on the Pi) | laptop pass |
| Death with a full pacing queue | words flushed at pace, then `death_shown` | `test_fake_faults.py::test_death_with_a_full_pacing_queue` | (fakes only, 10.4) | laptop pass |
| Controller killed | restarted; previous life `interrupted`; no creature left; counter + 1 | `test_fake_faults.py::test_controller_killed_mid_life_is_recovered` | `pi:controller-killed` (`systemctl kill -s KILL epitaph-controller`) | laptop pass |
| Controller stops pinging | systemd restarts it | `tests/unit/test_controller_resilience.py::test_a_stuck_loop_kills_its_creature_then_loses_its_pings` | `pi:controller-stops-pinging` (test hook) | laptop pass |
| Power cut | as a controller kill; state intact | | `pi:power-cut` (`echo b > /proc/sysrq-trigger` after a fresh image) | owner |
| Clean reboot | services active, words within `first_word_after_boot_s` | | `pi:clean-reboot` (`make pi-boot-check`; the first `word` after boot, A5) | owner (no reboot this phase; G1.3 rebooted once) |
| Headless boot | display unit skipped by `ExecCondition`; controller up | | `pi:headless-boot` (current boot, no reboot; the reboot passed at G1.3) | Pi pass (2026-10-01, current boot) |
| Display or remote view killed | life continues; redraw from snapshot within 5 s | D3 tests | `pi:display-killed` (`systemctl kill epitaph-display`; kill the tunnel) | open |
| Slow subscriber | controller timing unchanged; snapshot after overflow | `tests/unit/test_events.py::test_slow_subscriber_never_blocks_and_gets_snapshot` | `pi:slow-subscriber` (client reading 1 event/s) | laptop pass |
| Two controllers | refuses; points to `epitaph ctl new-life` | `tests/unit/test_state.py::test_single_instance_lock` | `pi:two-controllers` (`epitaph run` while the service runs) | laptop pass |
| Two agents on the Pi | queues; stale lock expires | `tests/unit/test_pi_tools.py` (the lock in a temporary dir) | `pi:two-agents` (a second `pi_lock.sh run` while the driver holds it) | Pi pass (2026-10-01) |
| Wi-Fi only | `ssh pi` works; NTP; a life starts | | `pi:wifi-only` (unplug cable, reboot) | owner |
| Laptop off | Pi keeps internet and time; life continues | | `pi:laptop-off` (disconnect the laptop) | owner |
| Password login over Wi-Fi | refused; accepted over the cable | | `pi:password-over-wifi` (S0.12) | owner |
| Hostname persistence | still `epitaph` | | `pi:hostname-persistence` (two reboots) | owner |
| Exhibition closing | `unseen`: dark, life continues; `pause`: no birth until opening | `tests/unit/test_exhibit.py::test_unseen_lives_go_on_with_the_screen_dark`, `::test_pause_waits_for_the_opening_and_pings_meanwhile`, `::test_pause_finishes_the_life_then_waits_across_midnight`, `::test_unsynced_time_runs_lives_in_closed_hours` | | laptop pass |
| Low disk | refuses with the space needed | A8 container test | | open |

---

## Cards: "done when" (BUILD_PLAN 9)

| Card | Done when | Command that proves it | Status | Evidence |
|---|---|---|---|---|
| L | S0 and G0 pass | the S0 and G0 tables | open | |
| L | Contracts exist as code with docstrings | `$PY -m pydoc epitaph.types epitaph.backend.base epitaph.body.base epitaph.events` has a docstring per class | open | |
| L | Every profile's `estimate` runs in `make check` | `make estimate` (Pi 4, and Pi 5 on both overlays); `make sim-profiles` | open | phase 3 (B9): every Pi 4 and Pi 5 profile passes; `pi5/compressed-600` retired with `pi4/compressed-2700` |
| A | Fake and real backends pass the same contract tests | `$PY -m pytest tests -k backend_contract` (fake) and `-m model` (real, laptop lock) | open | |
| A | Rehearsal reports delivered | G0.6-G0.8 | open | |
| A | `bench/` has laptop and Pi numbers | `ls bench/dev-*.json bench/pi4-*.json` | open | |
| A | The chosen ladders download, verify and fit | `epitaph download` (sha256 checked); S1a rows in SPIKE.md | open | |
| B | The simulator covers every state, cause and profile | `$PY -m pytest tests/sim` covering oom, deadline, full, crash, hang, manual, interrupted on every profile | open | oom, deadline, full, crash covered today |
| B | Every Pi 4 profile passes `estimate` on measured costs | G0.4 | open | |
| B | … and verify-life on real lives | G1.1, G2.3 | open | |
| C | Bootstrap idempotent | `tools/pi_bootstrap.sh` twice; second run reports no changes | open | |
| C | Selftest passes under the service | G2.1 | open | |
| C | `death_mode` proven | S3: kill within 10 s, 5 of 5, in SPIKE.md | open | |
| C | Watchdogs verified | S0.10 plus the "controller stops pinging" fault row | open | |
| C | Install idempotent | A6 | open | |
| C | Restore tested | A10 | open | |
| D | A real Pi life watchable live and in replay, in a terminal and a window | G1.2; `epitaph replay <life> --speed 2 --from 20:00` with `--driver terminal` and `--driver screen` | open | |
| D | D13 at every resolution | A9 | open | |
| D | Headless boot has no crash loop | G1.3 | open | |
| E | CI is green | G0.2 | open | first public run green on 3.12 and 3.13 |
| E | Every gate signed off with evidence | this file | open | |

---

## verify-life coverage of 10.3

Which 10.3 rows `verify.py` implements today (phase 2), and at which level. A life is judged by the
hardware overlay it records, else the one its profile's class implies (`pi4/...` → `pi4-4gb`),
so a Pi life copied to the laptop keeps the Pi's thresholds. Layout rows call
D's `epitaph.display.layout.verify_probe(cfg)` (on main since phase 0b) and are `pending` only if it cannot load. The
`rehearsal` level runs the 5.11 metrics plus the recall budget, the sync rule, the
thought-count rule and `speed_monotonic`; `screen` (stage 1 samples) runs the text metrics only.
A life cut by a killed controller or a power cut keeps a torn line before the death record
recovery appends; verify-life reads past it (phase 2 fix; before, it refused the life).

| 10.3 check | Check name(s) in verify.json | Levels | State |
|---|---|---|---|
| Duration within lifespan ± 60 s (`unbounded` ends with `full`) | `duration` | smoke, skeleton, full | built |
| Cause (`deadline` for smoke/skeleton, `death_mode` for full) | `cause` | smoke, skeleton, full | built |
| At least one word shown (not in 10.3: without it a life that showed nothing passes smoke) | `words_shown` | smoke, skeleton, full | built (phase 1) |
| Past-turn tokens ≤ recall + 10% | `recall_budget` | all but screen | built (from `vitals.recall_used`); a smoke, skeleton or full life whose vitals never carry `recall_used` fails (phase 1); a rehearsal life skips |
| Banned phrases, markup or emoji shown: 0 | `banned_phrases_shown`, `markup_or_emoji_shown` | all | built |
| Sync rule | `sync_rule` | all but screen | built |
| `death_shown` within `max_death_display_delay_s` | `death_shown_delay` | smoke, skeleton, full | built |
| Next life within silence + load + 5 min | `next_birth` | smoke, skeleton, full | built; pending until the next life is recorded |
| Empty thoughts < 10% | `empty_thoughts` | skeleton, full | built |
| Typing speed in the overlay's ranges | `typing_speed_birth` (phase median), `typing_speed_writing` (every thought) | skeleton, full | built |
| No word split across lines | `no_split_words` | skeleton, full | built (D's `display.layout.verify_probe`, phase 1); pending only when the probe cannot load |
| Thought-count rule (5.3) on this life | `thought_count_rule` | full, rehearsal | built (costmodel's rule logic) |
| Reload silence; expected number of reloads | `reload_silence`, `reload_count` | full | built |
| Reload noticing | `reload_noticing` | full, rehearsal, screen | built |
| Bright words in the last 2 min ≤ 40 (flow) | `bright_words_last_2min` | full | built (D's probe, as above) |
| Tokens/s last 5 min < 40% of first 5 min | `speed_decline` | full | built |
| Speed never rises across a reload (review 2, F2; not yet in the 10.3 table) | `speed_monotonic` | full, rehearsal | built: mean `gen_end.tok_s` of up to `speed_monotonic_thoughts` (2) thoughts after each reload ≤ (1 + `speed_monotonic_tolerance` (0.05)) × the mean before; falls back to `vitals.tok_s`, read as the previous thought's speed. The cost model checks the same rule (`estimate.speed_monotonic`) |
| Complete sentences ≥ 80%, 6-20 words, before erosion | `complete_sentences`, `sentence_length` | full, rehearsal, screen | built |
| Notice rate ≥ 60%; demise rate ≥ 40% | `notice_rate`, `demise_rate` | full, rehearsal, screen | built |
| Specific ≥ 50%; clichés ≤ 1/200 words; non-Latin < 1%; distinct 4-grams ≥ 0.5 | `specific`, `cliches`, `non_latin`, `distinct_4grams` | full, rehearsal, screen | built |
| Voice hygiene (5.11) | `helpdesk_voice`, `answering_readings`, `thinking_tags` (+ the banned and markup checks) | full, rehearsal, screen | built; lists from `config/lang/<language>.toml` |
| Persona groups left at death: 0 | `persona_groups_at_death` | full | built |
| The death when the plan kills it: within `max_kill_delay_s` (10 s) of the squeeze, or of the deadline (10.4 RAM death; not in the 10.3 table) | `death_time` | full | built (phase 2): the old `duration` check let a death up to 60 s late or early pass |
| Each reload loads its keyframe's step and threads; the last reading is on the last rung (5.2, 5.5; not in the 10.3 table) | `reload_targets` | full | built (phase 2): from the vitals after each reload and the last one; a reload cut by the death is listed, not judged |
| Every erosion step taken as its own step, in order (5.6, ADR-024; not in the 10.3 table) | `erosion_steps` | full | built (phase 2): one `erosion` per erosion keyframe up to the last reading, with its groups and mechanics; the detail gives each step's lag. `persona_groups_at_death` alone passed two steps merged into one |
| Non-fatal controller errors (6.3 `error`) | `error_events` | smoke, skeleton, full | built (phase 2): `advisory` when any, never failing (the life survives them by design); listed in `verify.json` |

---

## Sign-off log

| Date | Gate | Result | Signed | Notes |
|---|---|---|---|---|
