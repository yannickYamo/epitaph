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
  compare table into the phase report, `docs/REPORTS/0c-*.md`, as the evidence.

---

## S0: step 0 (BUILD_PLAN 8.6)

| # | Item | Command that proves it | Status | Evidence |
|---|---|---|---|---|
| S0.1 | Laptop tools: tesseract, qemu-user-static, podman | `tesseract --version; qemu-aarch64-static --version; podman --version` | open | |
| S0.2 | SD backup exists and restore was tested | `ls ~/epitaph-backups/step0-*/` (sha256 files); PI_FACTS "restore tested" | open | PI_FACTS says tested |
| S0.3 | Passwordless sudo | `ssh pi 'sudo -n true && echo ok'` | pass | probe 2026-09-29 22:34 (E, read-only, under the Pi lock): `sudo -n true` ok |
| S0.4 | No password in the repo or scratch files | `git grep -nIi -e 'passw' -- ':!docs/BUILD_PLAN.md'` reviewed by hand; scratch file gone | open | |
| S0.5 | Wi-Fi carries the default route; cable `never-default` | `ssh pi 'ip route show default'` shows `wlan0` | pass | probe 2026-09-29 22:34 (E, read-only, under the Pi lock): `default via <router-ip> dev wlan0 … metric 600`, the only default route |
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
| G0.11 | Checkpoint A reply recorded (models, persona, chat or diary) | `docs/QUESTIONS.md` / CHANGELOG entry; no reply by the end of the session means the two best-scoring models, the v6 persona, chat mode | open | |

## G1: walking skeleton on the Pi (BUILD_PLAN 8.4)

| # | Item | Command that proves it | Status | Evidence |
|---|---|---|---|---|
| G1.1 | Two consecutive `skeleton-1200` lives pass `verify-life --level skeleton` | `make pi-life PROFILE=pi4/skeleton-1200` twice; then `$PY -m epitaph.verify <n> --level skeleton` and `<n+1>` both exit 0; `next_birth` passes on the first | open | |
| G1.2 | The remote view shows them live | `epitaph display --connect pi --driver terminal` during the life; screenshot or transcript in `docs/REPORTS/` | open | |
| G1.3 | Headless boot: no display crash loop | `ssh pi 'sudo reboot'`; after boot `systemctl show epitaph-display -p NRestarts,ActiveState,ConditionResult` (condition false, 0 restarts) and `systemctl is-active epitaph-controller` | open | |
| G1.4 | `tools/smoke_pi.sh` passes (`smoke-300`, level smoke) | `make pi-smoke` exit 0 | open | E4 (P1) |
| G1.5 | `/code-review high` done on the gate diff | integrator's review note | open | |

## G2: full decline, checkpoint B (BUILD_PLAN 8.4)

| # | Item | Command that proves it | Status | Evidence |
|---|---|---|---|---|
| G2.1 | Selftest passes under the installed service | `ssh pi 'sudo -u epitaph epitaph selftest'` (or as the unit user) exit 0 | open | |
| G2.2 | The fault matrix passes on the Pi (rows that apply) | the fault table below, Pi column | open | |
| G2.3 | Three `compressed-2700` lives pass `verify-life --level full` | `make pi-life PROFILE=pi4/compressed-2700` ×3; `$PY -m epitaph.verify <n> --level full` exit 0 each | open | layout checks pending until D's `verify_probe` |
| G2.4 | `/code-review high` done | integrator's review note | open | |
| G2.5 | Checkpoint B reply ("good" or the list) | QUESTIONS / CHANGELOG | open | needs Yannick |

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
| A8 | `pi4/unbounded` and the Pi 5 profiles pass simulation; `pi4/unbounded` passes one real life | CI step "Pi 5 and unbounded profiles pass simulation"; one Pi life + `verify-life --level full` | open | simulation passes today (see below) |
| A9 | D13 passes at every tested resolution | `$PY -m pytest -m display tests/display` (CI step "Headless display tests") | open | D5 (P1) |
| A10 | `sd_restore.sh` has restored an image at least once | C's log in PI_CHANGES / report | open | |
| A11 | Checkpoint B read on the remote view | Yannick | open | |
| A12 | Checkpoint C: soak report acceptable | Yannick | open | |
| A13 | `/code-review high` done | integrator | open | |
| later | S5 and checkpoint B's physical part when a screen is connected | D8 | pending | no screen (10.2) |

---

## Fault matrix (BUILD_PLAN 10.4)

Laptop rows run on the fakes in `tests/faults/`; Pi rows run with C's fault scripts (C9) and
are judged by `verify-life`. `n/a` rows depend on the S3/S3b/S3c results.

| Fault | Expected | Laptop (fakes) | Pi | Status |
|---|---|---|---|---|
| RAM death (`death_mode = oom`) | `cause=oom` within 10 s; next life after the silence | `tests/faults/test_fake_faults.py::test_oom_death_at_the_squeeze` | death squeeze life + `verify-life` (`cause`, `next_birth`) | laptop pass |
| Delegated cgroups | every S3b step passes | | `epitaph selftest` under the service | open |
| Creature network blocked | refused | | outbound connect from the creature cgroup | open |
| Crash | `cause=crash`; next life | `test_crash_is_recorded_and_fails_verify` | `kill -9 <creature>` | laptop pass |
| Hang | `cause=hang` after the timeout; cgroup killed | needs B's hang detection (P2) | `kill -STOP <creature>` | open |
| Slow first token at low CPU share | no false `hang` | P2 | CPU share 0.7, 1000-token prompt | open |
| Waiting on the SD card | no false `hang` | P2 | `memory.high` probe 60 s | open |
| Full context (`unbounded`) | `cause=full` | `test_full_context_in_a_small_ctx` | small-ctx test profile | laptop pass |
| Reload longer than a keyframe gap | `reload_skipped`, current target loaded | needs the controller's skip logic (B8) | cold reload | open |
| Deadline during a reload | `cause=deadline`, nothing left running | `test_deadline_during_a_reload` | short lifespan on the Pi | laptop pass |
| Death with a full pacing queue | words flushed at pace, then `death_shown` | needs B5 pacing in the loop | | open |
| Controller killed | restarted; previous life `interrupted`; no creature left; counter + 1 | needs B6 | `systemctl kill -s KILL epitaph-controller` | open |
| Controller stops pinging | systemd restarts it | | test hook | open |
| Power cut | as a controller kill; state intact | | `echo b > /proc/sysrq-trigger` after a fresh image | open |
| Clean reboot | services active, words within `first_word_after_boot_s` | | `sudo reboot` | open |
| Headless boot | display unit skipped by `ExecCondition`; controller up | | reboot, no screen | open |
| Display or remote view killed | life continues; redraw from snapshot within 5 s | D3 tests | `systemctl kill epitaph-display`; kill the tunnel | open |
| Slow subscriber | controller timing unchanged; snapshot after overflow | `tests/unit/test_events.py` (bus overflow) | client reading 1 event/s | open |
| Two controllers | refuses; points to `epitaph ctl new-life` | `tests/unit/test_state.py` (instance lock) | `epitaph run` while the service runs | open |
| Two agents on the Pi | queues; stale lock expires | | second `pi_lock.sh run` | open |
| Wi-Fi only | `ssh pi` works; NTP; a life starts | | unplug cable, reboot | open |
| Laptop off | Pi keeps internet and time; life continues | | disconnect the laptop | open |
| Password login over Wi-Fi | refused; accepted over the cable | | S0.12 | open |
| Hostname persistence | still `epitaph` | | two reboots | open |
| Exhibition closing | `unseen`: dark, life continues; `pause`: no birth until opening | B9 tests on the fake clock | | open |
| Low disk | refuses with the space needed | A8 container test | | open |

---

## Cards: "done when" (BUILD_PLAN 9)

| Card | Done when | Command that proves it | Status | Evidence |
|---|---|---|---|---|
| L | S0 and G0 pass | the S0 and G0 tables | open | |
| L | Contracts exist as code with docstrings | `$PY -m pydoc epitaph.types epitaph.backend.base epitaph.body.base epitaph.events` has a docstring per class | open | |
| L | Every profile's `estimate` runs in `make check` | `make estimate` (Pi 4); CI runs sim and Pi 5 profiles too | open | pi5/compressed-600 fails today (QUESTIONS) |
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

Which 10.3 rows `verify.py` implements today (phase 0c), and at which level. Layout rows call
D's `epitaph.display.layout.verify_probe(cfg)` and are `pending` until it exists. The
`rehearsal` level runs the 5.11 metrics plus the recall budget, the sync rule and the
thought-count rule; `screen` (stage 1 samples) runs the text metrics only.

| 10.3 check | Check name(s) in verify.json | Levels | State |
|---|---|---|---|
| Duration within lifespan ± 60 s (`unbounded` ends with `full`) | `duration` | smoke, skeleton, full | built |
| Cause (`deadline` for smoke/skeleton, `death_mode` for full) | `cause` | smoke, skeleton, full | built |
| Past-turn tokens ≤ recall + 10% | `recall_budget` | all but screen | built (from `vitals.recall_used`) |
| Banned phrases, markup or emoji shown: 0 | `banned_phrases_shown`, `markup_or_emoji_shown` | all | built |
| Sync rule | `sync_rule` | all but screen | built |
| `death_shown` within `max_death_display_delay_s` | `death_shown_delay` | smoke, skeleton, full | built |
| Next life within silence + load + 5 min | `next_birth` | smoke, skeleton, full | built; pending until the next life is recorded |
| Empty thoughts < 10% | `empty_thoughts` | skeleton, full | built |
| Typing speed in the overlay's ranges | `typing_speed_birth` (phase median), `typing_speed_writing` (every thought) | skeleton, full | built |
| No word split across lines | `no_split_words` | skeleton, full | pending (D) |
| Thought-count rule (5.3) on this life | `thought_count_rule` | full, rehearsal | built (costmodel's rule logic) |
| Reload silence; expected number of reloads | `reload_silence`, `reload_count` | full | built |
| Reload noticing | `reload_noticing` | full, rehearsal, screen | built |
| Bright words in the last 2 min ≤ 40 (flow) | `bright_words_last_2min` | full | pending (D) |
| Tokens/s last 5 min < 40% of first 5 min | `speed_decline` | full | built |
| Complete sentences ≥ 80%, 6-20 words, before erosion | `complete_sentences`, `sentence_length` | full, rehearsal, screen | built |
| Notice rate ≥ 60%; demise rate ≥ 40% | `notice_rate`, `demise_rate` | full, rehearsal, screen | built |
| Specific ≥ 50%; clichés ≤ 1/200 words; non-Latin < 1%; distinct 4-grams ≥ 0.5 | `specific`, `cliches`, `non_latin`, `distinct_4grams` | full, rehearsal, screen | built |
| Voice hygiene (5.11) | `helpdesk_voice`, `answering_readings`, `thinking_tags` (+ the banned and markup checks) | full, rehearsal, screen | built; lists from `config/lang/<language>.toml` |
| Persona groups left at death: 0 | `persona_groups_at_death` | full | built |

---

## Sign-off log

| Date | Gate | Result | Signed | Notes |
|---|---|---|---|---|
