"""tools/collect_lives.sh and tools/fault_matrix_pi.sh against a fake Pi.

The fake Pi is tests/unit/test_pi_tools.py's: stand-in `ssh`, `sudo` and `systemctl` on PATH,
a temporary lock dir, a fake state dir holding simulated lives. A fake `tools/fault_pi.sh`
(its interface: one row per call, a PASS or FAIL line, exit 0 or 1)
answers each row from a file. Also: the fault table in docs/GATES.md, the laptop rows in
tests/faults and the driver's rows agree. No network, no Pi.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.faults import test_fake_faults as laptop_faults
from tests.unit.test_pi_tools import FAKES

ROOT = Path(__file__).resolve().parents[2]
COLLECT = ROOT / "tools" / "collect_lives.sh"
MATRIX = ROOT / "tools" / "fault_matrix_pi.sh"
GATES = ROOT / "docs" / "GATES.md"


@pytest.fixture
def pi(tmp_path: Path, recorded_life) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """Environment with the fake Pi on PATH and three finished smoke lives in its state dir."""
    if shutil.which("flock") is None or shutil.which("timeout") is None:
        pytest.skip("needs flock and timeout (util-linux, coreutils)")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in FAKES.items():
        path = bin_dir / name
        path.write_text("#!/usr/bin/env bash\n" + body.lstrip())
        path.chmod(0o755)
    state = tmp_path / "pi-state"
    shutil.copytree(recorded_life("pi4/smoke-300", lives=3) / "lives", state / "lives")
    for d in sorted((state / "lives").iterdir()):
        finish(d, "deadline", "pi4/smoke-300")
    (tmp_path / "locks").mkdir()
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DIR": str(tmp_path),
        "PI_HOST": "pi",
        "EPITAPH_SSH": str(bin_dir / "ssh"),
        "EPITAPH_PI_STATE": str(state),
        "EPITAPH_LOCK_DIR": str(tmp_path / "locks"),
        "EPITAPH_PYTHON": sys.executable,
        "HOLDER": "test",
    }


def finish(d: Path, cause: str | None, profile: str) -> None:
    """Give a life folder the controller's meta.json and, unless cause is None, death.json."""
    (d / "meta.json").write_text(json.dumps({"profile": profile, "hardware": "pi4-4gb"}))
    if cause is not None:
        (d / "death.json").write_text(json.dumps({"cause": cause, "life": int(d.name)}, indent=1))
    else:
        (d / "death.json").unlink(missing_ok=True)


def run(script: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args], env=env, capture_output=True, text=True, timeout=180
    )


def lives_dir(env: dict[str, str]) -> Path:
    return Path(env["EPITAPH_PI_STATE"]) / "lives"


# -- collect_lives.sh ---------------------------------------------------------------------


def test_collect_judges_the_newest_finished_lives(pi: dict[str, str], tmp_path: Path) -> None:
    finish(lives_dir(pi) / "000003", None, "pi4/smoke-300")  # the life in progress
    out = tmp_path / "out"
    res = run(COLLECT, pi, "--lives", "2", "--out", str(out))
    assert res.returncode == 0, res.stderr + res.stdout
    assert "life 000003 is in progress: not judged" in res.stderr
    for n in ("000001", "000002"):
        verdict = json.loads((out / "lives" / n / "verify.json").read_text())
        assert verdict["ok"] and verdict["level"] == "smoke"
    # the life in progress is copied as context for life 2's next_birth, never judged
    assert (out / "lives" / "000003" / "events.jsonl").is_file()
    assert not (out / "lives" / "000003" / "verify.json").exists()
    life2 = json.loads((out / "lives" / "000002" / "verify.json").read_text())
    assert [c["status"] for c in life2["checks"] if c["name"] == "next_birth"] == ["pass"]
    summary = (out / "summary.md").read_text()
    assert "| 000002 | pi4/smoke-300 | smoke | deadline |" in summary
    assert "Consecutive lives: yes" in summary
    assert "PASS: 2 life(s) from the service" in res.stderr


def test_collect_skips_interrupted_lives_unless_asked(pi: dict[str, str], tmp_path: Path) -> None:
    finish(lives_dir(pi) / "000003", "interrupted", "pi4/smoke-300")
    out = tmp_path / "out"
    res = run(COLLECT, pi, "--lives", "1", "--out", str(out))
    assert res.returncode == 0, res.stderr
    assert "life 000003 was interrupted" in res.stderr
    assert (out / "lives" / "000002" / "verify.json").is_file()
    assert "Skipped as interrupted: 000003." in (out / "summary.md").read_text()

    o2 = tmp_path / "o2"
    res = run(COLLECT, pi, "--lives", "1", "--include-interrupted", "--out", str(o2))
    assert res.returncode == 0, res.stderr  # its events end in a real deadline death here
    assert (o2 / "lives" / "000003" / "verify.json").is_file()  # judged this time
    assert not (o2 / "lives" / "000002").exists()


def test_collect_fails_when_too_few_lives_are_finished(pi: dict[str, str], tmp_path: Path) -> None:
    res = run(COLLECT, pi, "--lives", "5", "--out", str(tmp_path / "out"))
    assert res.returncode == 1
    assert "5 finished life(s) asked for, 3 found" in res.stderr


def test_collect_fails_a_life_that_fails_verify(pi: dict[str, str], tmp_path: Path) -> None:
    events = lives_dir(pi) / "000003" / "events.jsonl"
    kept = [ln for ln in events.read_text().splitlines() if '"type": "death' not in ln]
    events.write_text("\n".join(kept) + "\n")
    out = tmp_path / "out"
    res = run(COLLECT, pi, "--lives", "1", "--out", str(out))
    assert res.returncode == 1
    assert "| 000003 | pi4/smoke-300 | smoke |" in (out / "summary.md").read_text()
    assert "FAIL" in (out / "summary.md").read_text()


def test_collect_takes_only_the_profile_asked_for(pi: dict[str, str], tmp_path: Path) -> None:
    finish(lives_dir(pi) / "000003", "deadline", "pi4/skeleton-1200")
    out = tmp_path / "out"
    res = run(COLLECT, pi, "--lives", "1", "--profile", "pi4/smoke-300", "--out", str(out))
    assert res.returncode == 0, res.stderr
    assert "life 000003 ran pi4/skeleton-1200, not pi4/smoke-300: skipped" in res.stderr
    assert (out / "lives" / "000002" / "verify.json").is_file()


def test_collect_with_no_lives_fails(pi: dict[str, str], tmp_path: Path) -> None:
    shutil.rmtree(lives_dir(pi))
    res = run(COLLECT, pi, "--out", str(tmp_path / "out"))
    assert res.returncode == 1 and "no finished life" in res.stderr


def test_collect_dry_run_touches_nothing(tmp_path: Path) -> None:
    env = {**os.environ, "EPITAPH_SSH": "false", "PI_HOST": "pi", "EPITAPH_PYTHON": sys.executable}
    res = run(COLLECT, env, "--dry-run", "--lives", "2", "--out", str(tmp_path / "out"))
    assert res.returncode == 0, res.stderr
    assert res.stderr.count("verify-life") == 2
    assert "dry run: nothing ran" in res.stderr
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--lives", "0"], "--lives must be 1-99"),
        (["--profile", "pi4/x;rm"], "bad profile name"),
        (["--bogus"], "unknown option"),
    ],
)
def test_collect_usage_errors(args: list[str], message: str) -> None:
    res = run(COLLECT, {**os.environ, "EPITAPH_SSH": "false"}, *args)
    assert res.returncode == 2 and message in res.stderr


# -- fault_matrix_pi.sh -------------------------------------------------------------------

FAULT_PI = """#!/usr/bin/env bash
# A stand-in for tools/fault_pi.sh: answers from $FAKE_DIR/faults/<row>.
echo "$1 EPITAPH_PI_LOCKED=${EPITAPH_PI_LOCKED:-}" >> "$FAKE_DIR/fault_pi.log"
f="$FAKE_DIR/faults/$1"
[ -f "$f" ] || { echo "fault_pi: unknown row $1" >&2; exit 2; }
case "$(cat "$f")" in
  pass) echo "injecting"; echo "PASS: $1 as expected"; exit 0 ;;
  fail) echo "FAIL: $1 broke"; exit 1 ;;
  stop) rm -f "$FAKE_DIR/controller.active"; echo "FAIL: left it stopped"; exit 1 ;;
  lock) exit 75 ;;
  silent) exit 0 ;;
esac
"""


@pytest.fixture
def matrix(pi: dict[str, str], tmp_path: Path) -> dict[str, str]:
    script = tmp_path / "fault_pi.sh"
    script.write_text(FAULT_PI)
    script.chmod(0o755)
    (tmp_path / "faults").mkdir()
    (tmp_path / "controller.active").touch()
    return {**pi, "FAULT_PI": str(script)}


def answer(tmp_path: Path, **rows: str) -> None:
    for row, how in rows.items():
        (tmp_path / "faults" / row.replace("_", "-")).write_text(how)


def table(out: Path) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in out.read_text().splitlines():
        m = re.match(r"\| `([a-z-]+)` \|", line)
        if m:
            rows[m.group(1)] = [c.strip() for c in line.strip("|").split("|")]
    return rows


def test_matrix_runs_each_row_and_writes_the_table(matrix: dict[str, str], tmp_path: Path) -> None:
    answer(tmp_path, crash="pass", hang="pass")
    out = tmp_path / "faults.md"
    res = run(MATRIX, matrix, "--rows", "crash,hang,power-cut", "--out", str(out))
    assert res.returncode == 0, res.stderr
    rows = table(out)
    assert rows["crash"][3] == "**PASS**" and rows["crash"][5] == "PASS: crash as expected"
    assert rows["power-cut"][3] == "**owner**"
    assert "After the rows: controller=active" in out.read_text()
    assert (tmp_path / "faults" / "crash").exists()
    assert "injecting" in (tmp_path / "faults" / "crash.log").read_text()
    # one hold of the Pi lock for the whole matrix; fault_pi.sh is told it holds it
    assert (tmp_path / "fault_pi.log").read_text().splitlines() == [
        "crash EPITAPH_PI_LOCKED=1",
        "hang EPITAPH_PI_LOCKED=1",
    ]


def test_matrix_fails_on_a_failing_row(matrix: dict[str, str], tmp_path: Path) -> None:
    answer(tmp_path, crash="pass", hang="fail")
    out = tmp_path / "faults.md"
    res = run(MATRIX, matrix, "--rows", "crash,hang", "--out", str(out))
    assert res.returncode == 1
    assert table(out)["hang"][3] == "**FAIL**"


@pytest.mark.parametrize(
    ("how", "result", "evidence"),
    [
        (None, "**not run**", "does not know this row"),
        ("lock", "**error**", "must honour EPITAPH_PI_LOCKED=1"),
        ("silent", "**error**", "exit 0 without a PASS line"),
    ],
)
def test_matrix_rows_that_do_not_answer_fail_it(
    matrix: dict[str, str], tmp_path: Path, how: str | None, result: str, evidence: str
) -> None:
    if how is not None:
        answer(tmp_path, crash=how)
    out = tmp_path / "faults.md"
    res = run(MATRIX, matrix, "--rows", "crash", "--out", str(out))
    assert res.returncode == 1
    row = table(out)["crash"]
    assert row[3] == result and evidence in row[5]


def test_matrix_without_fault_pi_runs_nothing_and_fails(
    matrix: dict[str, str], tmp_path: Path
) -> None:
    out = tmp_path / "faults.md"
    env = {**matrix, "FAULT_PI": str(tmp_path / "missing.sh")}
    res = run(MATRIX, env, "--rows", "crash", "--out", str(out))
    assert res.returncode == 1
    assert table(out)["crash"][3] == "**not run**"


def test_matrix_starts_the_controller_again(matrix: dict[str, str], tmp_path: Path) -> None:
    answer(tmp_path, crash="stop")
    res = run(MATRIX, matrix, "--rows", "crash", "--out", str(tmp_path / "faults.md"))
    assert res.returncode == 1
    assert (tmp_path / "controller.active").exists()
    assert "start epitaph-controller" in (tmp_path / "systemctl.log").read_text()


def test_matrix_two_holders_row_proves_the_lock_queues(
    matrix: dict[str, str], tmp_path: Path
) -> None:
    out = tmp_path / "faults.md"
    res = run(MATRIX, matrix, "--rows", "two-holders", "--out", str(out))
    assert res.returncode == 0, res.stderr
    assert table(out)["two-holders"][3] == "**PASS**"
    assert "held by: test" in (tmp_path / "faults" / "two-holders.log").read_text()


def test_matrix_owner_rows_alone_take_no_lock(tmp_path: Path) -> None:
    env = {**os.environ, "EPITAPH_SSH": "false", "PI_HOST": "pi"}
    env["EPITAPH_LOCK_DIR"] = str(tmp_path / "no-locks")
    res = run(MATRIX, env, "--rows", "power-cut", "--dry-run")
    assert res.returncode == 0 and "owner: power-cut" in res.stderr


def test_matrix_dry_run_and_list(tmp_path: Path) -> None:
    env = {**os.environ, "EPITAPH_SSH": "false", "PI_HOST": "pi"}
    res = run(MATRIX, env, "--dry-run", "--out", str(tmp_path / "f.md"))
    assert res.returncode == 0, res.stderr
    assert "fault_pi.sh crash" in res.stderr and "(native) headless-boot" in res.stderr
    assert not (tmp_path / "f.md").exists()
    listed = run(MATRIX, env, "--list").stdout.split("\n")
    assert any(line.startswith("power-cut") and " owner " in line for line in listed)


@pytest.mark.parametrize(
    ("args", "message"),
    [(["--rows", "nope"], "unknown row nope"), (["--row-min", "x"], "--row-min"), (["-x"], "")],
)
def test_matrix_usage_errors(args: list[str], message: str) -> None:
    res = run(MATRIX, {**os.environ, "EPITAPH_SSH": "false"}, *args)
    assert res.returncode == 2 and message in res.stderr


# -- the fault table agrees with the tests and the driver ---------------------------------


def fault_table() -> list[list[str]]:
    text = GATES.read_text()
    section = text[text.index("## Fault matrix") :]
    section = section[: section.index("\n---")]
    return [
        [c.strip() for c in line.strip().strip("|").split("|")]
        for line in section.splitlines()
        if line.startswith("| ") and not line.startswith("| Fault ")
    ]


def driver_rows() -> dict[str, str]:
    out = subprocess.run(["bash", str(MATRIX), "--list"], capture_output=True, text=True).stdout
    return {line.split()[0]: line.split()[1] for line in out.splitlines() if line.strip()}


def test_every_laptop_row_names_an_existing_test() -> None:
    tests = {name for name in dir(laptop_faults) if name.startswith("test_")}
    assert set(laptop_faults.ROWS.values()) <= tests
    for row in fault_table():
        fault, laptop = row[0], row[2]
        for name in re.findall(r"test_fake_faults\.py::(test_\w+)", laptop):
            assert name in tests, f"{fault}: {name}"
        if fault in laptop_faults.ROWS:
            assert laptop_faults.ROWS[fault] in laptop, fault


def test_every_pi_row_in_the_table_is_one_the_driver_runs() -> None:
    rows = driver_rows()
    named = set()
    for row in fault_table():
        for name in re.findall(r"`pi:([a-z-]+)`", row[3]):
            assert name in rows, f"{row[0]}: no driver row {name}"
            named.add(name)
            if rows[name] == "owner":
                assert "owner" in row[4], f"{row[0]}: an owner row"
    assert named == set(rows)  # and the driver runs nothing the table does not list
