# Gates

The acceptance criteria of the piece, each with the command that proves it, its status and its
evidence. Gates G1 and G2 were passed on the Pi before v1.0, on the earlier reload design (now
the profile `pi4/default-reloads`). The installed v1.0 life (one model, no reload, ADR-030 and
ADR-031) runs on the Pi but has no three-life gate of its own yet.

**Status values:** `open` (not checked yet), `pass`, `fail`, `pending` (waits on hardware; never
counts as a failure), `waived`. Every `pass` names its evidence.

**Conventions in the commands:**

- `$PY` = `PYTHONPATH=src .venv/bin/python` in the checkout being checked (as the Makefile).
- Pi commands run through the lock: `tools/pi_lock.sh run gate <min> -- <cmd>`. Read-only probes
  still take the lock. `ssh pi` stands for your SSH alias of the Pi.
- `verify-life` is `$PY -m epitaph verify-life` or, equally, `$PY -m epitaph.verify`.
  Exit 0 = pass, 1 = a check failed, 2 = usage or config error. It writes `verify.json` next
  to the life's `events.jsonl` (`--no-write` to skip). `--summary` prints the voice metrics as
  one JSON line.
- `<life>` is a life number (resolved in the state dir), a life folder or an events file.
- `verify-life compare <dir>...` verifies every life below the folders (rehearsal runs: one
  folder per life) and prints a Markdown table ranked by the voice metrics, then a per-model
  verdict.

---

## G1: walking skeleton on the Pi

**How the Pi rows run.** Every Pi target takes the Pi lock itself (`HOLDER=<name>` names the
holder) and writes its evidence under `logs/pi/` (untracked); the summary lines there are the evidence.

- `make pi-smoke` and `make pi-life` both call `tools/smoke_pi.sh`. In one lock hold, it:
  - finds the Pi (`tools/pi_host.sh`) and the installed `epitaph`;
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
- The order for the gate: `make pi-deploy`, then G1.4, G1.1 with G1.2 alongside, and
  G1.3 last, because it reboots.
- Budgets (the lock's hard limit): `pi-smoke` 33 min. `pi-life PROFILE=pi4/skeleton-1200
  LIVES=2` 77 min (2 × (20 min + 5 min load + 3 min) + the 90 s silence + 5 min, plus
  15 min for the copy and the checks). `pi-boot-check` 17 min.

| # | Item | Command that proves it | Status | Evidence |
|---|---|---|---|---|
| G1.0 | The simulator runs the real controller on the fakes | `grep -n controller src/epitaph/sim.py` shows it driving `controller.py`; `make sim` exit 0 | pass (2026-10-01) | `sim.py` drives `controller.py`; `make sim` green |
| G1.1 | Two consecutive `skeleton-1200` lives pass `verify-life --level skeleton` | `make pi-life PROFILE=pi4/skeleton-1200 LIVES=2` exits 0, and its last line reads `PASS: 2 life(s) of pi4/skeleton-1200`. Re-check on the laptop: `$PY -m epitaph verify-life logs/pi/<run>/lives/<n> --profile pi4/skeleton-1200 --hardware pi4-4gb --level skeleton` exits 0 for `<n>` and `<n+1>`, and the `next_birth` check in `<n>`'s `verify.json` is `pass` | pass (2026-10-01) | lives 000002 and 000003: skeleton PASS, 569 and 584 words, deadline at 1200 s, next birth 154 s |
| G1.2 | The remote view shows them live | During the G1.1 run, under the same lock hold (the tunnel only reads the bus): `$PY -m epitaph display --connect <host> --driver terminal`. It shows birth, words typed letter by letter, the death and the next birth | pass (2026-10-01) | the remote view in a terminal during life 000002: status strip, letters typed live, 1.2-1.4 tok/s |
| G1.3 | Headless boot: no display crash loop | No screen connected. `make pi-boot-check` exits 0 (it reboots: `tools/headless_boot_check.sh --reboot`, waits for a new `boot_id` over SSH and for `systemctl is-system-running --wait`). It passes when `epitaph-display` is `loaded`, `enabled`, not `failed` or `activating`, has `NRestarts=0` and was skipped by its `ExecCondition` (`Result=exec-condition`; `ConditionResult=no` also counts, for a `Condition*=` line), and `epitaph-controller` is `enabled` and `active`. `REBOOT=0 make pi-boot-check` runs the same checks on the current boot | pass (2026-10-01) | reboot to SSH 31 s; display skipped by its ExecCondition, 0 restarts; controller active, 0 restarts; throttled 0x0 |
| G1.4 | `tools/smoke_pi.sh` passes (`smoke-300`, level smoke) | `make pi-smoke` exits 0, and its last line reads `PASS: 1 life(s) of pi4/smoke-300` | pass (2026-10-01) | life 000001: smoke PASS, 4 thoughts, 132 words, deadline at 300 s |
| G1.5 | Code review done on the gate diff | review note | pass (2026-10-01) | 11 findings: ten fixed with tests, one accepted as ADR-027 |

## G2: full decline

**These rows ran on the earlier reload design.** On 2026-10-01 `pi4/default` was the 30-minute
life with two reloads and erosion (ADR-024), which is now `pi4/default-reloads`. G2.3 and G2.6
judge those lives. The commands below still run today; with the installed `pi4/default` the
reload checks have nothing to count.

**How the G2 rows run.** Two ways to get lives, both judged by `verify-life` at the profile's
level (`full` for `pi4/default`), both writing `verify.json` next to each life's `events.jsonl`
under `logs/pi/` (untracked); the summary lines there are the evidence.

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
  `make pi-faults` runs the Pi rows (`tools/fault_matrix_pi.sh`, one lock hold, each
  row through `tools/fault_pi.sh <row>`) and writes `logs/pi/faults-<stamp>.md` with each
  row's log in `logs/pi/faults-<stamp>/`. `ROWS=a,b` runs a subset; `--list` names them.

| # | Item | Command that proves it | Status | Evidence |
|---|---|---|---|---|
| G2.1 | Selftest passes under the installed service | `tools/pi_lock.sh run gate 10 -- ssh pi /opt/epitaph/venv/bin/epitaph selftest --user pi` exit 0 (it relaunches itself as a transient `Delegate=yes` unit for the service user; its clock round trip moves the CPU clock, so stop `epitaph-controller` around it and start it again after, or take the fault row `make pi-faults ROWS=delegated-cgroups`) | pass (2026-10-01) | `epitaph selftest` 12/12 through `deploy/install.sh` (transient Delegate=yes unit as the service user), network check included |
| G2.2 | The fault matrix passes on the Pi (rows that apply) | `make pi-faults` exit 0 and its last line reads `PASS: every row that ran passed`; the table `logs/pi/faults-<stamp>.md` has every `pi` and `native` row **PASS** and the `owner` rows listed (the fault table below, Pi column) | pass (2026-10-01) | `make pi-faults`: creature-network, two-controllers, crash, controller-killed, hang, two-holders all PASS; RAM death and delegated cgroups proven by real lives, calibration and selftest; owner rows wait |
| G2.3 | Three consecutive `pi4/default` lives pass `verify-life --level full` | `make pi-life PROFILE=pi4/default LIVES=3` exits 0 and its last line reads `PASS: 3 life(s) of pi4/default`. Or, from the running service: `make pi-collect PROFILE=pi4/default LIVES=3` exits 0 and `logs/pi/service-<stamp>/summary.md` reads `Consecutive lives: yes`. Re-check on the laptop: `$PY -m epitaph verify-life logs/pi/<run>/lives/<n> --profile pi4/default --hardware pi4-4gb` (level `full`, the profile's own) exits 0 for each `<n>` | pass (2026-10-01, reload design) | lives 000028-000030, consecutive on the service: full level PASS each; OOM at 1770.8-1770.9 s; last words shown 1.9-25.8 s after the death; speed decline 0.29-0.31; voice proxies advisory only |
| G2.4 | Code review done | review note | pass (2026-10-01) | 10 findings, all fixed with tests |
| G2.5 | The owner's reply after watching lives ("good" or a list) | CHANGELOG | pass (2026-10-01) | the owner's reply after watching lives on the remote view: "good to close on my side the voice looks good" |
| G2.6 | Speed never rises across a reload on the Pi | In each G2.3 life's `verify.json`, `speed_monotonic` is `pass`, with a value ≤ 1.05, from `gen_end` rates | pass (2026-10-01, reload design) | `speed_monotonic` in lives 000028-000030: 0.924, 1.016, 0.986 (limit 1.05) |
| G2.7 | Three consecutive lives of the installed v1.0 `pi4/default` pass `verify-life --level full` | as G2.3 | open | the v1.0 life runs on the Pi; no three-life run has been judged and recorded here |

## G3: hardening and acceptance

**The soak is waived (ADR-029); if one is run later, this is how.** The soak is the installed
service itself, on Wi-Fi with no maintenance cable, for at least 25 hours; nothing is deployed
and nothing takes the Pi lock meanwhile. Three read-only steps, none of which stops the service:

1. At the start, on the laptop: `nohup tools/soak_sample.sh --out logs/pi/soak-<stamp>/samples.tsv
   2>logs/pi/soak-<stamp>/sample.log &`. Every 10 minutes it appends the controller's pid, RSS
   and NRestarts, the CPU temperature, `vcgencmd get_throttled` and the disk use of
   `/var/lib/epitaph` and `/` (over SSH; a sample the Pi does not answer is skipped and the
   report lists the hole). It reads only `/proc`, `/sys`, `systemctl show`, `du` and `df`. If
   the laptop cannot stay awake for a day, `tools/soak_sample.sh --local` takes the same samples
   on the Pi itself.
2. At the end: `tools/collect_lives.sh --lives <N> --include-interrupted --out
   logs/pi/soak-<stamp>/collect`, with N the lives since the soak began (a life and its silence
   take about 32 minutes, so about 47 for 25 hours). It copies and judges every finished life of
   the soak, keeping interrupted ones so the report can count them. Then
   `ssh pi journalctl -u epitaph-controller -o short-iso --since '<soak start>' >
   logs/pi/soak-<stamp>/journal.txt`, `ssh pi journalctl -k -o short-iso --since '<soak start>'
   >> logs/pi/soak-<stamp>/journal.txt` (kernel under-voltage lines) and
   `ssh pi cat /var/lib/epitaph/status.json > logs/pi/soak-<stamp>/status.json`.
3. `$PY tools/soak_report.py logs/pi/soak-<stamp>/collect --journal logs/pi/soak-<stamp>/journal.txt
   --status logs/pi/soak-<stamp>/status.json --samples logs/pi/soak-<stamp>/samples.tsv
   --first <first life> --out logs/pi/soak-<stamp>/report.md` exits 0 when every soak row below
   passes. A criterion without its input reads `no data` and fails.

| # | Item | Command that proves it | Status | Evidence |
|---|---|---|---|---|
| A1 | `make check` green on the laptop and in CI; coverage met; every Pi 4 profile passes `estimate` on measured costs | `make check`; CI run; coverage step (80% overall, 90% per strict module and `verify.py`) | pass (2026-10-03) | `make check` green on the laptop and in CI (Python 3.11-3.13), small-chip checks included |
| A2 | Every life in the soak passes `verify-life --level full` | `soak_report.py` row "Every life passes `verify-life`" | waived (owner, 2026-10-01) | ADR-029; every collected life is still judged by `verify-life` |
| A3 | The fault matrix passes on the Pi (applicable rows) | the fault table below | pass (2026-10-01) | G2.2: every applicable row PASS on the Pi |
| A4a | Soak ≥ 25 h: no missed life (every `death_shown` → next birth within silence + load + 5 min) | `soak_report.py` rows "Soak of at least 25 hours" and "No missed life" | waived (owner, 2026-10-01) | ADR-029. `tools/soak_sample.sh` and `tools/soak_report.py` stay for a long run later |
| A4b | Zero controller crashes | `soak_report.py` row "Zero controller crashes": no `Failed with result` or abnormal main-process exit and no `Scheduled restart` in the journal, NRestarts constant and one controller pid in the samples, no life closed `interrupted` | waived (owner, 2026-10-01) | ADR-029 |
| A4c | Controller memory growth < 20 MB; disk < 100 MB/day | `soak_report.py` rows: median RSS of the last 3 samples minus the first 3 within one controller pid; growth of the root filesystem's used space per day | waived (owner, 2026-10-01) | ADR-029 |
| A4d | No under-voltage bits; throttling or thermal pauses < 10% of the time | `soak_report.py` rows: no sample with `get_throttled` bit 0 or 16 and no kernel under-voltage line; samples with bit 1, 2 or 3 set plus the controller's `thermal` pauses, over the soak | waived (owner, 2026-10-01) | ADR-029 |
| A5 | Power on to first shown word ≤ `first_word_after_boot_s` (240 s) | reboot test: boot time from `journalctl --list-boots`, first `word` event `ts` | open | |
| A6 | `install.sh` idempotent on the Pi and in a clean arm64 Debian container | `deploy/install.sh` twice on the Pi (second run changes nothing); `make install-test-arm64` | partial (2026-10-01) | Pi: second run `changed: 0`; no arm64 container run is recorded |
| A7 | `epitaph sim` and `epitaph run --backend fake --display terminal` work with no model | `$PY -m epitaph sim --profile pi4/default`; `$PY -m epitaph run --backend fake --display terminal --clock fake --profile pi4/default --lives 1` (and in real time with `--profile pi4/smoke-300`) | pass (2026-10-01, laptop) | both exit 0 on the laptop with no flags (`config/profiles/dev/default.toml` extends `pi4/default`) |
| A8 | `pi4/unbounded` and the Pi 5 profiles pass simulation; `pi4/unbounded` passes one real life | `make sim-profiles` and `make estimate` (both in `make check`: every Pi 5 profile on both overlays); one Pi life + `verify-life --level full` | open | simulation and estimate pass. The real life has not been run; verify-life's `bright_words_last_2min` fails a simulated unbounded life (it never forgets, so nothing fades) and needs the unbounded skip that `speed_decline` has |
| A9 | The screen is readable at every tested resolution | `$PY -m pytest -m display tests/display` (CI step "Headless display tests") | pass (2026-10-03) | `pytest -m display tests/display`: 269 passed, OCR at four resolutions |
| A10 | `tools/sd_restore.sh` has restored an image at least once | a restore log | open | a backup image was restored into a loop file once; no restore to a card is recorded |
| A11 | The owner watched whole lives on the remote view | the owner | pass (2026-10-01) | the owner watched whole lives on the remote view and approved the voice ("good to close") |
| A12 | Soak report acceptable | the owner reads `logs/pi/soak-<stamp>/report.md` | waived (owner, 2026-10-01) | ADR-029 |
| A13 | Final code review done | review note | open | |
| A14 | Docs: README (laptop quickstart, then the Pi), CONFIG, INSTALLATION, CONTRIBUTING, model licenses | `tests/unit/test_config_doc.py` (every key in `config/` documented, defaults equal); the README quickstart commands run on the laptop | pass (2026-10-01, laptop) | README quickstart commands each exit 0 on the laptop; `test_config_doc.py` passes |
| later | The piece on a physical screen | | pending | no screen connected yet |

---

## Fault matrix

Laptop rows run on the fakes in `tests/faults/test_fake_faults.py` (`make faults`): each
injects the fault into the real controller on `pi4/default` and lets verify-life judge the
recorded life; `ROWS` in that file maps each row to its test.
Pi rows run through `tools/fault_matrix_pi.sh` (`make pi-faults`; `pi:<row>` below is its row
name): `pi` rows call `tools/fault_pi.sh <row>` (one row per call, a line that starts with
`PASS` or `FAIL`, exit 0 or 1; exit 2 for a row it does not know; it must not take the Pi lock
when `EPITAPH_PI_LOCKED=1`, because the driver holds it), `native` rows are the driver's own,
and `owner` rows need a person or a fresh SD image and are never run by the tools.
`tests/unit/test_fault_matrix_pi.py` checks that this table, the laptop tests and the driver's
rows agree. (The heading keeps its old section number because that test looks it up.)

| Fault | Expected | Laptop (fakes) | Pi | Status |
|---|---|---|---|---|
| RAM death (`death_mode = oom`) | `cause=oom` within 10 s; next life after the silence | `test_fake_faults.py::test_oom_death_at_the_squeeze` (and `death_time` on every full life) | `pi:ram-death`; every service life (`make pi-collect`) | laptop pass; Pi pass (2026-10-01, real lives, G2.3) |
| Delegated cgroups | every selftest step passes | | `pi:delegated-cgroups` (`epitaph selftest` under the service, G2.1) | Pi pass (2026-10-01, selftest, G2.1) |
| Creature network blocked | refused | | `pi:creature-network` | Pi pass (2026-10-01) |
| Crash | `cause=crash`; next life | `test_fake_faults.py::test_crash_is_recorded_and_fails_verify` | `pi:crash` (`kill -9 <creature>`) | laptop pass; Pi pass (2026-10-01) |
| Hang | `cause=hang` after the timeout; cgroup killed | `test_fake_faults.py::test_hang_kills_the_creature_and_the_next_life_follows` | `pi:hang` (`kill -STOP <creature>`) | laptop pass; Pi pass (2026-10-01) |
| Slow first token at low CPU share | no false `hang` | `test_fake_faults.py::test_slow_first_token_at_low_cpu_share_is_not_a_hang` | `pi:slow-first-token` (CPU share 0.7, 1000-token prompt) | laptop pass |
| Waiting on the SD card | no false `hang` | `test_fake_faults.py::test_waiting_on_the_sd_card_is_not_a_hang` | `pi:sd-card-wait` (`memory.high` probe 60 s) | laptop pass |
| Full context (`unbounded`) | `cause=full` | `test_fake_faults.py::test_full_context_in_a_small_ctx` | `pi:full-context` (small-ctx test profile) | laptop pass |
| Reload longer than a keyframe gap | `reload_skipped`, current target loaded | `test_fake_faults.py::test_reload_longer_than_a_keyframe_gap` | `pi:reload-longer-than-gap` (cold reload) | laptop pass |
| Deadline during a reload | `cause=deadline`, nothing left running | `test_fake_faults.py::test_deadline_during_a_reload` | `pi:deadline-during-reload` (short lifespan on the Pi) | laptop pass |
| Death with a full pacing queue | words flushed at pace, then `death_shown` | `test_fake_faults.py::test_death_with_a_full_pacing_queue` | (fakes only) | laptop pass |
| Controller killed | restarted; previous life `interrupted`; no creature left; counter + 1 | `test_fake_faults.py::test_controller_killed_mid_life_is_recovered` | `pi:controller-killed` (`systemctl kill -s KILL epitaph-controller`) | laptop pass; Pi pass (2026-10-01) |
| Controller stops pinging | systemd restarts it | `tests/unit/test_controller_resilience.py::test_a_stuck_loop_kills_its_creature_then_loses_its_pings` | `pi:controller-stops-pinging` (test hook) | laptop pass |
| Power cut | as a controller kill; state intact | | `pi:power-cut` (`echo b > /proc/sysrq-trigger` after a fresh image) | owner |
| Clean reboot | services active, words within `first_word_after_boot_s` | | `pi:clean-reboot` (`make pi-boot-check`; the first `word` after boot, A5) | owner (G1.3 rebooted once) |
| Headless boot | display unit skipped by `ExecCondition`; controller up | | `pi:headless-boot` (current boot, no reboot; the reboot passed at G1.3) | Pi pass (2026-10-01, current boot) |
| Display or remote view killed | life continues; redraw from snapshot within 5 s | display tests | `pi:display-killed` (`systemctl kill epitaph-display`; kill the tunnel) | open |
| Slow subscriber | controller timing unchanged; snapshot after overflow | `tests/unit/test_events.py::test_slow_subscriber_never_blocks_and_gets_snapshot` | `pi:slow-subscriber` (client reading 1 event/s) | laptop pass |
| Two controllers | refuses; points to `epitaph ctl new-life` | `tests/unit/test_state.py::test_single_instance_lock` | `pi:two-controllers` (`epitaph run` while the service runs) | laptop pass; Pi pass (2026-10-01) |
| Two runs on the Pi | queues; stale lock expires | `tests/unit/test_pi_tools.py` (the lock in a temporary dir) | `pi:two-holders` (a second `pi_lock.sh run` while the driver holds it) | Pi pass (2026-10-01) |
| Wi-Fi only | SSH works; NTP; a life starts | | `pi:wifi-only` (unplug cable, reboot) | owner |
| Laptop off | Pi keeps internet and time; life continues | | `pi:laptop-off` (disconnect the laptop) | owner |
| Password login over Wi-Fi | refused; accepted over the cable | | `pi:password-over-wifi` | owner |
| Hostname persistence | still `epitaph` | | `pi:hostname-persistence` (two reboots) | owner |
| Exhibition closing | `unseen`: dark, life continues; `pause`: no birth until opening | `tests/unit/test_exhibit.py::test_unseen_lives_go_on_with_the_screen_dark`, `::test_pause_waits_for_the_opening_and_pings_meanwhile`, `::test_pause_finishes_the_life_then_waits_across_midnight`, `::test_unsynced_time_runs_lives_in_closed_hours` | | laptop pass |
| Low disk | refuses with the space needed | the arm64 container test (A6) | | open |

---

## What verify-life checks

What `verify.py` checks, and at which level. A life is judged by the hardware overlay it
records, else the one its profile's class implies (`pi4/...` → `pi4-4gb`), so a Pi life copied
to the laptop keeps the Pi's thresholds. Layout rows call
`epitaph.display.layout.verify_probe(cfg)` and are `pending` only if it cannot load. The
`rehearsal` level runs the voice metrics plus the recall budget, the sync rule, the
thought-count rule and `speed_monotonic`; `screen` (short samples) runs the text metrics only.
A life cut by a killed controller or a power cut keeps a torn line before the death record
recovery appends; verify-life reads past it.

The reload and erosion rows have nothing to count on the installed `pi4/default` (no reload, no
erosion); they judge `pi4/default-reloads`. A stream life is also checked for `stream_starvation`
(no wait for a word longer than `max_stream_stall_s`), `stream_stop` (the stop at the death) and
`shared_openings`; their thresholds are in [CONFIG.md](CONFIG.md), `[verify]`.

| Check | Check name(s) in verify.json | Levels | Notes |
|---|---|---|---|
| Duration within lifespan ± 60 s (`unbounded` ends with `full`) | `duration` | smoke, skeleton, full | |
| Cause (`deadline` for smoke/skeleton, `death_mode` for full) | `cause` | smoke, skeleton, full | |
| At least one word shown | `words_shown` | smoke, skeleton, full | |
| Past-turn tokens ≤ recall + 10% | `recall_budget` | all but screen | from `vitals.recall_used`; a smoke, skeleton or full life whose vitals never carry it fails; a rehearsal life skips |
| Banned phrases, markup or emoji shown: 0 | `banned_phrases_shown`, `markup_or_emoji_shown` | all | |
| Sync rule | `sync_rule` | all but screen | |
| `death_shown` within `max_death_display_delay_s` | `death_shown_delay` | smoke, skeleton, full | |
| Next life within silence + load + 5 min | `next_birth` | smoke, skeleton, full | pending until the next life is recorded |
| Empty thoughts < 10% | `empty_thoughts` | skeleton, full | |
| Typing speed in the overlay's ranges | `typing_speed_birth` (median), `typing_speed_writing` (every thought) | skeleton, full | |
| No word split across lines | `no_split_words` | skeleton, full | |
| Thought-count rule on this life | `thought_count_rule` | full, rehearsal | the cost model's rule logic |
| Reload silence; expected number of reloads | `reload_silence`, `reload_count` | full | |
| Reload noticing | `reload_noticing` | full, rehearsal, screen | |
| Bright words in the last 2 min ≤ 40 (flow) | `bright_words_last_2min` | full | |
| Tokens/s last 5 min < 40% of first 5 min | `speed_decline` | full | |
| Speed never rises across a reload | `speed_monotonic` | full, rehearsal | mean `gen_end.tok_s` of up to `speed_monotonic_thoughts` (2) thoughts after each reload ≤ (1 + `speed_monotonic_tolerance` (0.05)) × the mean before. The cost model checks the same rule (`estimate.speed_monotonic`) |
| Complete sentences ≥ 80%, 6-20 words, before erosion | `complete_sentences`, `sentence_length` | full, rehearsal, screen | |
| Notice rate ≥ 60%; demise rate ≥ 40% | `notice_rate`, `demise_rate` | full, rehearsal, screen | |
| Specific ≥ 50%; clichés ≤ 1/200 words; non-Latin < 1%; distinct 4-grams ≥ 0.5 | `specific`, `cliches`, `non_latin`, `distinct_4grams` | full, rehearsal, screen | |
| Voice hygiene | `helpdesk_voice`, `answering_readings`, `thinking_tags` (+ the banned and markup checks) | full, rehearsal, screen | lists from `config/lang/<language>.toml` |
| Persona groups left at death: 0 | `persona_groups_at_death` | full | |
| The death when the plan kills it: within `max_kill_delay_s` (10 s) of the squeeze, or of the deadline | `death_time` | full | |
| Each reload loads its keyframe's step and threads; the last reading is on the last rung | `reload_targets` | full | a reload cut by the death is listed, not judged |
| Every erosion step taken as its own step, in order | `erosion_steps` | full | one `erosion` per erosion keyframe up to the last reading, with its groups and mechanics |
| Non-fatal controller errors | `error_events` | smoke, skeleton, full | `advisory` when any, never failing (the life survives them by design) |

Voice metrics named in `verify.advisory_at_full` only advise at the `full` level (ADR-028).
