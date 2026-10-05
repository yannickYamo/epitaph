"""tools/soak_report.py and tools/soak_sample.sh on synthetic soaks.

Each soak is built as collect_lives.sh writes it: `lives/NNNNNN/` with events.jsonl,
meta.json, death.json and verify.json, plus a controller journal and soak_sample.sh lines.
No Pi, no network, no waiting.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SAMPLER = ROOT / "tools" / "soak_sample.sh"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("soak_report", ROOT / "tools" / "soak_report.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["soak_report"] = mod
    spec.loader.exec_module(mod)
    return mod


sr = _load()

T0 = 1_790_000_000.0
LOAD, LIVED, SHOWN_AFTER, SILENCE = 60.0, 1770.0, 60.0, 90.0
PERIOD = LOAD + LIVED + SHOWN_AFTER + SILENCE  # 1980 s from one load to the next


def write_life(
    root: Path,
    n: int,
    start: float,
    *,
    cause: str = "oom",
    ok: bool = True,
    shown: bool = True,
    finished: bool = True,
    verify: bool = True,
    thermal_s: float = 0.0,
    load_s: float = LOAD,
) -> float:
    """One life folder; returns when the next life starts loading (after the silence)."""
    d = root / "lives" / f"{n:06d}"
    d.mkdir(parents=True, exist_ok=True)
    birth = start + load_s
    death = birth + LIVED
    ev: list[dict[str, Any]] = [
        {"type": "birth_loading", "ts": start, "profile": "pi4/default", "model": "m"},
        {"type": "birth", "ts": birth},
    ]
    if thermal_s:
        ev.append({"type": "thermal", "ts": birth + 100, "pause_s": thermal_s})
    if finished:
        ev.append({"type": "death", "ts": death, "cause": cause, "lived_s": LIVED})
        if shown:
            ev.append({"type": "death_shown", "ts": death + SHOWN_AFTER})
            ev.append({"type": "silence", "ts": death + SHOWN_AFTER, "seconds": SILENCE})
    lines = [json.dumps({"v": 1, "life": n, **e}) for e in ev]
    (d / "events.jsonl").write_text("\n".join(lines) + "\n")
    (d / "meta.json").write_text(json.dumps({"life": n, "profile": "pi4/default", "model": "m"}))
    if finished:
        (d / "death.json").write_text(
            json.dumps({"cause": cause, "lived_s": LIVED, "closed_ts": death + SHOWN_AFTER})
        )
        if verify:
            failed = [] if ok else ["notice_reloads"]
            (d / "verify.json").write_text(
                json.dumps({"ok": ok, "failed": failed, "advisory": ["specific"], "level": "full"})
            )
    return death + SHOWN_AFTER + SILENCE


def soak(root: Path, lives: int = 48, **over: dict[str, Any]) -> list[float]:
    """`lives` consecutive lives from life 1; `over` maps a life number to write_life options."""
    starts = []
    t = T0
    for n in range(1, lives + 1):
        starts.append(t)
        t = write_life(root, n, t, **over.get(str(n), {}))
    return starts


def samples(
    hours: float = 27.0,
    *,
    rss_growth_kb: float = 2048.0,
    throttled: str = "0x0",
    pid_change_at: int | None = None,
    disk_kb_per_day: float = 30 * 1024,
    every: float = 600.0,
) -> str:
    """soak_sample.sh output: a header and one line every `every` seconds."""
    n = int(hours * 3600 / every)
    rows = ["# epoch\tpid\trss_kb\ttemp_c\tthrottled\tstate_kb\troot_used_kb\tnrestarts"]
    for i in range(n):
        t = T0 + i * every
        pid = 4000 if pid_change_at is None or i < pid_change_at else 4100
        rss = 80_000 + rss_growth_kb * i / max(1, n - 1)
        disk = 7_000_000 + disk_kb_per_day * (t - T0) / 86400
        rows.append(f"{t:.0f}\t{pid}\t{rss:.0f}\t48.3\t{throttled}\t{disk / 10:.0f}\t{disk:.0f}\t0")
    return "\n".join(rows) + "\n"


UNIT = "epitaph-controller.service"


def journal(*lines: str) -> str:
    """`journalctl -o short-iso` lines from systemd about the controller unit."""
    return "".join(f"2026-09-30T10:00:00-0700 epitaph systemd[1]: {m}\n" for m in lines)


CLEAN_JOURNAL = journal(
    f"Starting {UNIT} - epitaph controller...",
    f"Started {UNIT} - epitaph controller.",
    f"{UNIT}: A process of this unit has been killed by the OOM killer.",
    f"{UNIT}: A process of this unit has been killed by the OOM killer.",
)
CRASH_JOURNAL = CLEAN_JOURNAL + journal(
    f"{UNIT}: Watchdog timeout (limit 30s)!",
    f"{UNIT}: Main process exited, code=dumped, status=6/ABRT",
    f"{UNIT}: Failed with result 'watchdog'.",
    f"{UNIT}: Scheduled restart job, restart counter is at 1.",
    f"Started {UNIT} - epitaph controller.",
)
DEPLOY_JOURNAL = CLEAN_JOURNAL + journal(
    f"Stopping {UNIT} - epitaph controller...",
    f"{UNIT}: Main process exited, code=killed, status=15/TERM",
    f"Stopped {UNIT} - epitaph controller.",
    f"Started {UNIT} - epitaph controller.",
)


def report(
    tmp_path: Path,
    journal: str | None = CLEAN_JOURNAL,
    sample_text: str | None = None,
    extra: list[str] | None = None,
) -> tuple[int, str]:
    args = [str(tmp_path / "soak")]
    if journal is not None:
        (tmp_path / "journal.txt").write_text(journal)
        args += ["--journal", str(tmp_path / "journal.txt")]
    if sample_text is not None:
        (tmp_path / "samples.tsv").write_text(sample_text)
        args += ["--samples", str(tmp_path / "samples.tsv")]
    out = tmp_path / "report.md"
    rc = sr.main([*args, *(extra or []), "--out", str(out)])
    return rc, out.read_text() if out.exists() else ""


def row(text: str, name: str) -> str:
    for line in text.splitlines():
        if line.startswith(f"| {name}"):
            return line
    raise AssertionError(f"no row {name!r} in\n{text}")


# ---------------------------------------------------------------------------------------
# the whole report


def test_a_clean_soak_passes_every_criterion(tmp_path: Path) -> None:
    soak(tmp_path / "soak")
    rc, text = report(tmp_path, sample_text=samples())
    assert rc == 0, text
    assert text.startswith("# Soak report")
    assert "**PASS.** 48 finished lives (lives 000001 to 000048)" in text
    for name in (
        "Soak of at least 25",
        "Every life",
        "No missed life",
        "Zero controller",
        "Controller memory",
        "Disk growth",
        "No under-voltage",
        "Throttling",
    ):
        assert "| PASS |" in row(text, name), row(text, name)
    assert "+2.0 MB" in row(text, "Controller memory")
    assert "root filesystem 30 MB/day" in row(text, "Disk growth")
    assert "47 of 47 gaps within the limit" in row(text, "No missed life")
    assert "Causes of death: oom 48." in text
    assert "2 OOM kill(s) in the unit's subtree" in text
    # every life has a row, the last one's gap pending
    assert row(text, "000048").count("the last life in the soak") == 1
    assert "| 000001 | pi4/default | oom | 29:30 | PASS |  | specific | 150 s | 450 s |" in text


def test_a_short_soak_fails_the_duration(tmp_path: Path) -> None:
    soak(tmp_path / "soak", lives=10)
    rc, text = report(tmp_path, sample_text=samples(hours=5.5))
    assert rc == 1
    assert "| FAIL |" in row(text, "Soak of at least 25")
    assert rc == 1 and "**FAIL.**" in text


def test_min_hours_and_the_life_window(tmp_path: Path) -> None:
    soak(tmp_path / "soak", lives=10)
    rc, text = report(
        tmp_path, sample_text=None, extra=["--min-hours", "1", "--first", "3", "--last", "6"]
    )
    assert "| PASS |" in row(text, "Soak of at least 1 hours")
    assert "(lives 000003 to 000006)" in text
    assert "3 of 3 gaps" in row(text, "No missed life")
    assert rc == 1  # no samples: memory, disk and power have no data
    assert "| no data |" in row(text, "Controller memory")


def test_a_late_birth_is_a_missed_life(tmp_path: Path) -> None:
    root = tmp_path / "soak"
    t = T0
    for n in range(1, 6):
        t = write_life(root, n, t + (400 if n == 4 else 0))  # life 4 starts 400 s late
    rc, text = report(tmp_path, extra=["--min-hours", "0"], sample_text=samples(hours=3))
    assert rc == 1
    line = row(text, "No missed life")
    assert "| FAIL |" in line and "fail after 000003" in line
    assert "longest 550 s after life 000003 (limit 450 s)" in line
    assert "550 s (FAIL)" in row(text, "000003")


def test_a_slow_load_widens_the_limit(tmp_path: Path) -> None:
    """The limit is silence + the next life's measured load + 5 min (verify-life next_birth)."""
    root = tmp_path / "soak"
    t = write_life(root, 1, T0)
    write_life(root, 2, t, load_s=400)
    lives = list(sr.load_lives([root]).values())
    g = sr.gaps(lives)[0]
    assert g.status == "PASS" and g.gap_s == pytest.approx(490) and g.limit_s == pytest.approx(790)


def test_a_life_missing_from_the_folders_fails_the_rule(tmp_path: Path) -> None:
    root = tmp_path / "soak"
    soak(root, lives=5)
    import shutil

    shutil.rmtree(root / "lives" / "000003")
    gs = sr.gaps(list(sr.load_lives([root]).values()))
    assert [(g.life, g.status) for g in gs] == [
        (1, "PASS"),
        (2, "FAIL"),
        (4, "PASS"),
        (5, "pending"),
    ]
    assert gs[1].detail == "life 3 is not in the folders"


def test_a_life_without_death_shown_fails_the_rule(tmp_path: Path) -> None:
    root = tmp_path / "soak"
    soak(root, lives=3, **{"2": {"shown": False, "cause": "interrupted"}})
    gs = sr.gaps(list(sr.load_lives([root]).values()))
    assert gs[1].status == "FAIL" and gs[1].detail == "no death_shown (cause interrupted)"


def test_the_life_in_progress_is_pending_not_failed(tmp_path: Path) -> None:
    root = tmp_path / "soak"
    soak(root, lives=4, **{"4": {"finished": False}})
    lives = sr.load_lives([root])
    assert not lives[4].finished
    gs = sr.gaps(list(lives.values()))
    assert [g.status for g in gs] == ["PASS", "PASS", "PASS", "pending"]
    rc, text = report(tmp_path, extra=["--min-hours", "0"], sample_text=samples(hours=3))
    assert "| in progress |" in row(text, "000004")
    assert "3 of 3 finished lives pass" in row(text, "Every life")
    assert rc == 0, text


def test_a_failed_or_unverified_life_fails_the_verify_criterion(tmp_path: Path) -> None:
    soak(tmp_path / "soak", lives=4, **{"2": {"ok": False}, "3": {"verify": False}})
    rc, text = report(tmp_path, extra=["--min-hours", "0"], sample_text=samples(hours=3))
    line = row(text, "Every life")
    assert "| FAIL |" in line and "2 of 4 finished lives pass" in line
    assert "fail: 000002" in line and "not verified: 000003" in line
    assert "| FAIL | notice_reloads | specific |" in row(text, "000002")
    assert "| not verified |" in row(text, "000003")
    assert rc == 1


# ---------------------------------------------------------------------------------------
# the controller


def test_a_watchdog_kill_is_a_crash(tmp_path: Path) -> None:
    soak(tmp_path / "soak", lives=3)
    rc, text = report(
        tmp_path, journal=CRASH_JOURNAL, extra=["--min-hours", "0"], sample_text=samples(hours=2)
    )
    line = row(text, "Zero controller")
    assert "| FAIL |" in line and "journal: 1 failure(s), 1 automatic restart(s)" in line
    assert f"systemd[1]: {UNIT}: Watchdog timeout" in text
    assert rc == 1


def test_a_deliberate_stop_is_not_a_crash() -> None:
    j = sr.parse_journal(DEPLOY_JOURNAL)
    assert j.failures == 0 and j.restarts == 0 and j.deliberate_stops == 1 and j.starts == 2
    assert j.oom_kills == 2


def test_an_unannounced_exit_counts_without_a_failed_line() -> None:
    j = sr.parse_journal(
        journal(
            f"{UNIT}: Main process exited, code=killed, status=9/KILL",
            f"{UNIT}: Main process exited, code=exited, status=0/SUCCESS",
        )
        + "x python[3008]: Traceback (most recent call last):\n"
        "x kernel: hwmon hwmon1: Undervoltage detected!\n"
    )
    assert j.failures == 1 and j.tracebacks == 1 and j.undervoltage == 1


def test_a_new_controller_pid_fails_unless_a_stop_explains_it(tmp_path: Path) -> None:
    soak(tmp_path / "soak", lives=3)
    rc, text = report(
        tmp_path, extra=["--min-hours", "0"], sample_text=samples(hours=2, pid_change_at=5)
    )
    assert "| FAIL |" in row(text, "Zero controller") and "controller pids: 2" in row(
        text, "Zero controller"
    )
    assert rc == 1
    rc, text = report(
        tmp_path,
        journal=DEPLOY_JOURNAL,
        extra=["--min-hours", "0"],
        sample_text=samples(hours=2, pid_change_at=5),
    )
    assert "| PASS |" in row(text, "Zero controller")


def test_an_interrupted_life_without_a_journal_is_a_crash(tmp_path: Path) -> None:
    soak(tmp_path / "soak", lives=3, **{"2": {"cause": "interrupted", "shown": False}})
    rc, text = report(tmp_path, journal=None, extra=["--min-hours", "0"])
    assert "| FAIL |" in row(text, "Zero controller")
    assert "lives closed `interrupted`: 1" in row(text, "Zero controller")
    assert rc == 1


def test_no_controller_evidence_is_no_data(tmp_path: Path) -> None:
    soak(tmp_path / "soak", lives=3)
    _, text = report(tmp_path, journal=None, extra=["--min-hours", "0"])
    assert "| no data |" in row(text, "Zero controller")
    assert "Journal: not provided." in text


def test_controller_txt_and_status_are_reported(tmp_path: Path) -> None:
    soak(tmp_path / "soak", lives=3)
    (tmp_path / "soak" / "controller.txt").write_text("NRestarts=0\nActiveState=active\n")
    status = {"state": "living", "pid": 3008, "lives_run": 3, "life": 4, "t": 12.5}
    (tmp_path / "status.json").write_text(json.dumps(status))
    _, text = report(
        tmp_path, extra=["--min-hours", "0", "--status", str(tmp_path / "status.json")]
    )
    assert "systemd: ActiveState=active; NRestarts=0." in text
    assert (
        "Status at the end: state living, pid 3008, 3 lives run since the controller started, "
        "life 4 at t=12.5 s." in text
    )


# ---------------------------------------------------------------------------------------
# the machine


def test_memory_growth_over_the_limit_fails(tmp_path: Path) -> None:
    soak(tmp_path / "soak", lives=3)
    rc, text = report(
        tmp_path, extra=["--min-hours", "0"], sample_text=samples(hours=3, rss_growth_kb=25 * 1024)
    )
    assert "| FAIL |" in row(text, "Controller memory") and "+22.1 MB" in row(
        text, "Controller memory"
    )
    assert rc == 1


def test_disk_growth_over_the_limit_fails(tmp_path: Path) -> None:
    soak(tmp_path / "soak", lives=3)
    _, text = report(
        tmp_path,
        extra=["--min-hours", "0"],
        sample_text=samples(hours=3, disk_kb_per_day=150 * 1024),
    )
    line = row(text, "Disk growth")
    assert (
        "| FAIL |" in line
        and "root filesystem 150 MB/day" in line
        and "state directory 15 MB/day" in line
    )


@pytest.mark.parametrize("bits", ["0x50000", "0x1", "throttled=0x10000"])
def test_any_under_voltage_bit_fails(tmp_path: Path, bits: str) -> None:
    soak(tmp_path / "soak", lives=3)
    _, text = report(
        tmp_path, extra=["--min-hours", "0"], sample_text=samples(hours=3, throttled=bits)
    )
    assert "| FAIL |" in row(text, "No under-voltage")


def test_under_voltage_in_the_kernel_log_fails(tmp_path: Path) -> None:
    soak(tmp_path / "soak", lives=3)
    journal = (
        CLEAN_JOURNAL
        + "2026-09-30T12:00:00-0700 epitaph kernel: hwmon hwmon1: Undervoltage detected!\n"
    )
    _, text = report(
        tmp_path, journal=journal, extra=["--min-hours", "0"], sample_text=samples(hours=3)
    )
    line = row(text, "No under-voltage")
    assert "| FAIL |" in line and "1 under-voltage line(s) in the log" in line


def test_throttling_time_counts_samples_and_thermal_pauses(tmp_path: Path) -> None:
    soak(tmp_path / "soak", lives=3)
    _, text = report(
        tmp_path, extra=["--min-hours", "0"], sample_text=samples(hours=3, throttled="0x4")
    )
    assert "| FAIL |" in row(text, "Throttling") and "throttling in 100.0% of samples" in row(
        text, "Throttling"
    )
    # a sticky "occurred" bit alone is not time spent throttled
    _, text = report(
        tmp_path, extra=["--min-hours", "0"], sample_text=samples(hours=3, throttled="0x40000")
    )
    assert "| PASS |" in row(text, "Throttling")


def test_thermal_pauses_alone_can_fail_the_throttling_time(tmp_path: Path) -> None:
    soak(tmp_path / "soak", lives=3, **{"2": {"thermal_s": 900.0}})
    _, text = report(tmp_path, extra=["--min-hours", "0"], sample_text=samples(hours=1.6))
    line = row(text, "Throttling")
    assert "| FAIL |" in line and "thermal pauses 900 s" in line


def test_holes_in_the_sampling_are_listed(tmp_path: Path) -> None:
    soak(tmp_path / "soak", lives=3)
    text = samples(hours=3)
    lines = text.splitlines()
    del lines[5:9]  # 40 minutes without an answer
    _, out = report(tmp_path, extra=["--min-hours", "0"], sample_text="\n".join(lines) + "\n")
    assert "1 hole(s) in the sampling" in out and "for 50 min" in out


def test_parse_samples_tolerates_dashes_and_no_header() -> None:
    got = sr.parse_samples("1790000000\t-\t-\t47.2\t0x0\t-\t100\t-\nbad line\n")
    assert len(got) == 1
    s = got[0]
    assert s.pid is None and s.rss_kb is None and s.temp_c == 47.2 and s.throttled == 0
    assert s.root_used_kb == 100 and s.nrestarts is None
    assert sr.rss_growth_mb(got) is None and sr.disk_rate_mb_per_day(got, "root_used_kb") is None


# ---------------------------------------------------------------------------------------
# inputs


def test_a_life_found_twice_keeps_the_judged_copy(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    write_life(a, 7, T0, verify=False)
    write_life(b, 7, T0)
    lives = sr.load_lives([a, b / "lives", a])
    assert lives[7].verify is not None and lives[7].path.parent.parent == b


def test_one_life_folder_and_a_torn_line(tmp_path: Path) -> None:
    write_life(tmp_path, 3, T0)
    folder = tmp_path / "lives" / "000003"
    with (folder / "events.jsonl").open("a") as f:
        f.write('{"v": 1, "life": 3, "type": "wo')
    lives = sr.load_lives([folder])
    assert list(lives) == [3] and lives[3].shown_ts is not None


def test_usage_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert sr.main([str(tmp_path / "missing")]) == 2
    (tmp_path / "empty").mkdir()
    assert sr.main([str(tmp_path / "empty")]) == 2
    assert "no life folders" in capsys.readouterr().err


def test_stdout_when_no_out(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    soak(tmp_path / "soak", lives=2)
    assert sr.main([str(tmp_path / "soak"), "--min-hours", "0"]) == 1
    assert "# Soak report" in capsys.readouterr().out


def test_the_script_runs_from_the_command_line(tmp_path: Path) -> None:
    soak(tmp_path / "soak", lives=2)
    r = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "soak_report.py"),
            str(tmp_path / "soak"),
            "--title",
            "Soak 1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 1 and r.stdout.startswith("# Soak 1")


# ---------------------------------------------------------------------------------------
# the sampler


def _fake_pi(tmp_path: Path, ssh_rc: int | None = None) -> dict[str, str]:
    """A PATH where `ssh` runs the command locally, and systemctl and vcgencmd answer."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    ssh = 'while [ "${1:-}" = -o ]; do shift 2; done\nshift\n'
    ssh += f"exit {ssh_rc}\n" if ssh_rc is not None else 'exec bash -c "$*"\n'
    fakes = {
        "ssh": ssh,
        "systemctl": 'case "$*" in *MainPID*) echo "$FAKE_PID" ;; *NRestarts*) echo 2 ;; esac\n',
        "vcgencmd": "echo throttled=0x50000\n",
    }
    for name, body in fakes.items():
        f = bin_ / name
        f.write_text("#!/usr/bin/env bash\n" + body)
        f.chmod(0o755)
    state = tmp_path / "state"
    (state / "lives").mkdir(parents=True)
    (state / "lives" / "x").write_bytes(b"0" * 4096)
    env = dict(os.environ)
    env.update(
        PATH=f"{bin_}:{env['PATH']}",
        PI_HOST="pi",
        EPITAPH_PI_STATE=str(state),
        FAKE_PID=str(os.getpid()),
    )
    return env


@pytest.mark.skipif(sys.platform != "linux", reason="the Pi's tools and body are Linux's")
def test_the_sampler_appends_one_line_per_sample(tmp_path: Path) -> None:
    env = _fake_pi(tmp_path)
    out = tmp_path / "samples.tsv"
    for _ in range(2):
        r = subprocess.run(
            [str(SAMPLER), "--out", str(out), "--count", "1"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert r.returncode == 0, r.stderr
    lines = out.read_text().splitlines()
    assert lines[0] == "# epoch\tpid\trss_kb\ttemp_c\tthrottled\tstate_kb\troot_used_kb\tnrestarts"
    assert len(lines) == 3
    got = sr.parse_samples(out.read_text())
    s = got[-1]
    assert s.pid == os.getpid() and s.rss_kb is not None and s.rss_kb > 0
    assert s.throttled == 0x50000 and s.nrestarts == 2
    assert s.state_kb is not None and s.state_kb >= 4 and s.root_used_kb is not None
    assert abs(s.epoch - __import__("time").time()) < 60


def test_the_sampler_skips_a_sample_the_pi_does_not_answer(tmp_path: Path) -> None:
    env = _fake_pi(tmp_path, ssh_rc=255)
    out = tmp_path / "samples.tsv"
    r = subprocess.run(
        [str(SAMPLER), "--out", str(out), "--count", "1"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0
    assert "no answer from the Pi" in r.stderr
    assert out.read_text().count("\n") == 1  # the header only


def test_the_sampler_local_mode_without_the_unit(tmp_path: Path) -> None:
    env = _fake_pi(tmp_path)
    env["FAKE_PID"] = "0"
    out = tmp_path / "samples.tsv"
    r = subprocess.run(
        [str(SAMPLER), "--local", "--out", str(out), "--count", "1"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    s = sr.parse_samples(out.read_text())[0]
    assert s.pid is None and s.rss_kb is None


@pytest.mark.parametrize("args", [["--every", "0"], ["--count", "x"], ["--bogus"]])
def test_the_sampler_usage(args: list[str]) -> None:
    r = subprocess.run([str(SAMPLER), *args], capture_output=True, text=True, check=False)
    assert r.returncode == 2


def test_the_sampler_dry_run_prints_the_probe(tmp_path: Path) -> None:
    r = subprocess.run(
        [str(SAMPLER), "--dry-run", "--out", str(tmp_path / "x")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0 and "get_throttled" in r.stdout and not (tmp_path / "x").exists()


def test_a_journal_alone_cannot_pass_the_power_criterion(tmp_path: Path) -> None:
    soak(tmp_path / "soak", lives=3)
    _, text = report(tmp_path, extra=["--min-hours", "0"])
    assert "| no data |" in row(text, "No under-voltage")
