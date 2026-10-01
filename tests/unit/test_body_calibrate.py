"""`epitaph calibrate` against a fake cgroupfs and a fake creature (BUILD_PLAN 9 C7)."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import pytest

from epitaph.body import calibrate, selftest
from epitaph.body.calibrate import (
    MIN_FRACTION,
    StepResult,
    Trial,
    calibrate_step,
    calibration_path,
    death_step,
    load_calibration,
    parse_steps,
)
from epitaph.body.cgroup import MIB, CgroupBody, CgroupSettings, model_of
from epitaph.body.vitals import SysPaths
from epitaph.cli import main
from epitaph.config import load_config
from epitaph.types import ModelSpec
from tests.unit.test_body_cgroup import REL

MODEL = ModelSpec("qwen3-4b-instruct-2507", "src", "lic", ("Q4_K_M", "Q3_K_M", "Q2_K"))
ANON = 2000 * MIB


@pytest.fixture
def body(tmp_path: Path) -> CgroupBody:
    root = tmp_path / "cgroup" / REL
    root.mkdir(parents=True)
    (root / "cgroup.procs").write_text("4242\n")
    (root / "cgroup.controllers").write_text("cpuset cpu io memory pids\n")
    (root / "cgroup.subtree_control").write_text("\n")
    b = CgroupBody(root, CgroupSettings(kill_wait_s=0.1), sys_paths=SysPaths())
    b.setup()
    return b


class FakeCreature:
    """Loads into the fake cgroup: the anon memory appears, the cgroup is populated."""

    def __init__(self, body: CgroupBody, anon: int = ANON) -> None:
        self.body = body
        self.anon = anon
        self.loads: list[str] = []
        self.stops = 0

    async def start(self, model: ModelSpec, quant: str, threads: int) -> None:
        self.loads.append(quant)
        c = self.body.creature
        (c / "memory.stat").write_text(f"anon {self.anon}\nfile {5 * MIB}\n")
        (c / "memory.current").write_text(f"{self.anon + 5 * MIB}\n")
        (c / "memory.peak").write_text(f"{self.anon + 50 * MIB}\n")
        (c / "cgroup.events").write_text("populated 1\n")

    async def touch(self) -> None:
        pass

    async def stop(self) -> None:
        self.stops += 1


def kernel(kills_below: float, delay_s: float = 0.3, kills: bool = True):
    """A fake kernel for wait_dead: the limit kills when below `kills_below` x anon."""
    oom = {"n": 0}

    async def wait_dead(body: CgroupBody, timeout_s: float, poll_s: float = 0.02) -> bool:
        c = body.creature
        limit = int((c / "memory.max").read_text())
        anon = body.working_set().anon
        if kills and limit < kills_below * anon:
            oom["n"] += 1
            (c / "memory.events").write_text(f"oom 1\noom_kill {oom['n']}\n")
            (c / "cgroup.events").write_text("populated 0\n")
            return True
        if (c / "cgroup.kill").exists():  # the trial's own SIGKILL after giving up
            (c / "cgroup.events").write_text("populated 0\n")
            return True
        return False

    return wait_dead


def run(coro):
    return asyncio.run(coro)


def test_five_good_kills_in_a_row(body: CgroupBody, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calibrate, "wait_dead", kernel(kills_below=0.9))
    creature = FakeCreature(body)
    lines: list[str] = []
    r = run(calibrate_step(body, creature, MODEL, 2, 2, 5, 0.5, say=lines.append))
    assert r.reliable and r.good_in_a_row == 5 and len(r.trials) == 5
    assert creature.loads == ["Q2_K"] * 5 and creature.stops == 5
    assert r.anon_mb == 2000 and r.file_mb == 5 and r.peak_mb == 2050
    assert r.death_limit_mb == 1000 and r.death_fraction == 0.5
    assert all(t.ok and t.oom_kill for t in r.trials)
    assert "memory.max 1000 MiB, killed in" in lines[0] and lines[0].endswith(": ok")
    # every trial starts from clean limits, and calibration leaves them clean
    assert (body.creature / "memory.max").read_text() == "max"


def test_a_bad_trial_lowers_the_fraction(body: CgroupBody, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calibrate, "wait_dead", kernel(kills_below=0.4))
    monkeypatch.setattr(calibrate, "GIVE_UP_S", 0.01)
    r = run(calibrate_step(body, FakeCreature(body), MODEL, 2, 2, 3, 0.5, say=lambda s: None))
    assert [t.ok for t in r.trials] == [False, True, True, True]
    assert not r.trials[0].killed and r.trials[0].kill_s is None
    assert r.reliable and r.death_fraction == 0.35 and r.death_limit_mb == 700


def test_never_killed_is_not_reliable(body: CgroupBody, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calibrate, "wait_dead", kernel(kills_below=0, kills=False))
    r = run(calibrate_step(body, FakeCreature(body), MODEL, 0, 2, 2, 0.5, say=lambda s: None))
    assert not r.reliable and len(r.trials) == 4
    assert r.death_fraction == pytest.approx(MIN_FRACTION)
    data = r.to_json()
    assert data["reliable"] is False and data["good_in_a_row"] == 0
    # the creature was killed by the trial itself, not left alive
    assert (body.creature / "cgroup.events").read_text() == "populated 0\n"


def test_slow_kill_is_a_bad_trial(body: CgroupBody, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calibrate, "wait_dead", kernel(kills_below=0.9))
    clock = iter([0.0, 12.0, 20.0, 20.5])
    r = run(
        calibrate_step(
            body,
            FakeCreature(body),
            MODEL,
            1,
            2,
            1,
            0.5,
            say=lambda s: None,
            clock=lambda: next(clock),
        )
    )
    assert not r.trials[0].ok and r.trials[0].kill_s == 12.0  # killed, but after 10 s
    assert r.trials[-1].ok and r.reliable


def test_death_step_and_steps() -> None:
    assert death_step(load_config("pi4/default", "pi4-4gb")) == 2
    assert death_step(load_config("pi4/smoke-300", "pi4-4gb")) == 1  # no death: its end
    assert parse_steps(None, 3) == [0, 1, 2]
    assert parse_steps("2,0,2", 3) == [0, 2]
    with pytest.raises(ValueError, match="no ladder step"):
        parse_steps("3", 3)


def write_cal(d: Path, name: str, steps: list[dict[str, object]], model: str = MODEL.name) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps({"model": model, "steps": steps}))


def test_load_calibration(tmp_path: Path) -> None:
    state, bench = tmp_path / "state", tmp_path / "bench"
    write_cal(state, f"pi4-{MODEL.name}.json", [
        {"quant": "Q2_K", "death_limit_mb": 900, "reliable": True},
        {"quant": "Q3_K_M", "death_limit_mb": 1100, "reliable": False},
    ])  # fmt: skip
    write_cal(bench, f"pi4-{MODEL.name}.json", [
        {"quant": "Q2_K", "death_limit_mb": 1000, "reliable": True},
        {"quant": "Q4_K_M", "death_limit_mb": 1300, "reliable": True},
    ])  # fmt: skip
    write_cal(bench, "dev-x.json", [{"quant": "Q2_K", "death_limit_mb": 1, "reliable": True}])
    (bench / "pi4-broken.json").write_text("{")
    levels = load_calibration([state, bench, tmp_path / "none"], "pi4")
    assert levels == {(MODEL.name, "Q2_K"): 900, (MODEL.name, "Q4_K_M"): 1300}


def test_body_uses_the_calibrated_level(body: CgroupBody) -> None:
    argv = ["llama-server", "-m", f"/var/lib/epitaph/models/{MODEL.name}/Q2_K.gguf", "-c", "2048"]
    assert model_of(argv) == (MODEL.name, "Q2_K")
    assert model_of(["llama-server"]) is None and model_of(["x", "-m"]) is None
    body.death_levels = {(MODEL.name, "Q2_K"): 900}
    (body.creature / "memory.stat").write_text(f"anon {ANON}\n")
    assert body.death_limit_bytes() == ANON // 2  # nothing spawned yet: the fraction
    body.wrap_spawn(argv)
    assert body.death_limit_bytes() == 900 * MIB
    (body.creature / "memory.stat").write_text(f"anon {800 * MIB}\n")  # would not kill
    assert body.death_limit_bytes() == 400 * MIB
    body.wrap_spawn([*argv[:2], f"/m/{MODEL.name}/Q4_K_M.gguf"])  # not calibrated
    assert body.death_limit_bytes() == 400 * MIB


def test_make_body_reads_the_state_dir(tmp_path: Path) -> None:
    from epitaph.body.cgroup import make_body

    cfg = load_config("pi4/default", "pi4-4gb")
    cfg.data["paths"]["state_dir"] = str(tmp_path / "state")
    cfg.data["body"]["netblock_helper"] = ""
    write_cal(tmp_path / "state" / "calibration", f"pi4-{MODEL.name}.json", [
        {"quant": "Q2_K", "death_limit_mb": 777, "reliable": True},
    ])  # fmt: skip
    fs = tmp_path / "cg"
    root = fs / REL
    root.mkdir(parents=True)
    for name, text in (
        ("cgroup.procs", "1\n"),
        ("cgroup.controllers", "cpu io memory\n"),
        ("cgroup.subtree_control", ""),
    ):
        (root / name).write_text(text)
    (tmp_path / "self").write_text(f"0::/{REL}\n")
    b = make_body(cfg, fs, tmp_path / "self")
    assert isinstance(b, CgroupBody) and b.death_levels[(MODEL.name, "Q2_K")] == 777


def test_calibrate_every_step(body: CgroupBody, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calibrate, "wait_dead", kernel(kills_below=0.9))
    cfg = load_config("pi4/default", "pi4-4gb")
    args = argparse.Namespace(steps="0,2", trials=3, threads=2)
    creature = FakeCreature(body)
    record, ok = run(calibrate.calibrate(cfg, args, body, creature, say=lambda s: None))
    assert ok and creature.loads == ["Q4_K_M", "Q2_K", "Q2_K", "Q2_K"]
    assert [s["quant"] for s in record["steps"]] == ["Q4_K_M", "Q2_K"]
    assert [s["trials_wanted"] for s in record["steps"]] == [1, 3]
    assert record["hw_class"] == "pi4" and record["load_mode"] == "dio"
    assert calibration_path(Path("/s"), "pi4", "m") == Path("/s/calibration/pi4-m.json")


def test_step_result_json() -> None:
    r = StepResult(2, "Q2_K", 2, 2000, 5, 2005, None, 0.5, 1000, 2)
    r.trials += [Trial(1000, True, 0.4, True, True), Trial(1000, True, 0.3, True, True)]
    assert r.to_json()["reliable"] is True and r.to_json()["trials"][0]["kill_s"] == 0.4


def test_run_inside_refuses_beside_a_controller(tmp_path: Path) -> None:
    from epitaph.state import InstanceLock

    cfg = load_config("pi4/default", "pi4-4gb")
    cfg.data["paths"]["state_dir"] = str(tmp_path)
    lines: list[str] = []
    with InstanceLock(tmp_path):
        assert calibrate.run_inside(cfg, argparse.Namespace(), lines.append) == 1
    assert "already running" in lines[0] and "systemctl stop epitaph-controller" in lines[0]
    cfg.data["body"]["death_mode"] = "deadline"
    assert calibrate.run_inside(cfg, argparse.Namespace(), lines.append) == 1
    assert "not oom" in lines[-1]


def test_run_inside_without_delegation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    cfg.data["paths"]["state_dir"] = str(tmp_path)

    def refuse(*a: object, **k: object) -> CgroupBody:
        raise calibrate.CgroupError("not an epitaph* unit")

    monkeypatch.setattr(CgroupBody, "delegated", refuse)
    lines: list[str] = []
    assert calibrate.run_inside(cfg, argparse.Namespace(), lines.append) == 1
    assert "no delegated cgroup" in lines[0]


def test_cli_relaunches_in_a_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def relaunch(args: argparse.Namespace, **kw: object) -> int:
        calls.append(kw)
        return 0

    monkeypatch.setattr(selftest, "relaunch", relaunch)
    monkeypatch.setattr(selftest, "in_epitaph_unit", lambda: False)
    assert main(["calibrate", "--hardware", "pi4-4gb", "--steps", "2", "--trials", "5"]) == 0
    assert calls[0]["command"] == "calibrate"
    assert calls[0]["unit_prefix"] == "epitaph-calibrate"
    assert calls[0]["extra"] == ["--steps", "2", "--trials", "5", "--threads", "2"]


def test_relaunch_runs_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(selftest.shutil, "which", lambda name: "/usr/bin/" + name)
    args = argparse.Namespace(profile=None, hardware="pi4-4gb", user="root")
    rc = selftest.relaunch(
        args, lambda argv: ran.append(argv) or 0, "calibrate", ["--steps", "2"], "epitaph-cal"
    )
    assert rc == 0
    assert any(a.startswith("--unit=epitaph-cal-") for a in ran[0])
    tail = ran[0][-7:]
    assert tail == ["epitaph", "calibrate", "--inside", "--steps", "2", "--hardware", "pi4-4gb"]
