"""The real body against a fake cgroupfs laid out in a temporary directory (BUILD_PLAN 9 C3)."""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from epitaph.body.cgroup import (
    CgroupBody,
    CgroupError,
    CgroupSettings,
    PlainBody,
    drop_page_cache,
    make_body,
    own_cgroup,
    read_flat_keyed,
    read_io_rbytes,
    wrap_argv,
)
from epitaph.body.vitals import SysPaths
from epitaph.config import load_config
from epitaph.types import Cause, CreatureStatus, Health, Knobs

REL = "system.slice/epitaph-controller.service"


def knobs(cpu_share: float = 2.0, death: bool = False, threads: int = 2) -> Knobs:
    return Knobs(
        t=0.0,
        phase="x",
        health=Health.NOMINAL,
        recall=500,
        step=0,
        threads=threads,
        cpu_share=cpu_share,
        temperature=0.7,
        min_p=0.05,
        max_tokens=80,
        pause_s=3,
        persona_groups=5,
        mechanics=True,
        readings="full",
        letter_ms=55,
        jitter=0.1,
        hesitation=0,
        death_squeeze=death,
    )


@pytest.fixture
def fs(tmp_path: Path) -> Path:
    """A delegated service cgroup holding the controller (pid 4242)."""
    fs = tmp_path / "cgroup"
    root = fs / REL
    root.mkdir(parents=True)
    (root / "cgroup.procs").write_text("4242\n")
    (root / "cgroup.controllers").write_text("cpuset cpu io memory pids\n")
    (root / "cgroup.subtree_control").write_text("\n")
    (tmp_path / "self_cgroup").write_text(f"0::/{REL}\n")
    return fs


@pytest.fixture
def sys_paths(tmp_path: Path) -> SysPaths:
    (tmp_path / "temp").write_text("51234\n")
    (tmp_path / "meminfo").write_text("MemTotal:        3880000 kB\nMemAvailable: 3600000 kB\n")
    (tmp_path / "model").write_text("Raspberry Pi 4 Model B Rev 1.5\x00")
    (tmp_path / "thr").write_text("50000\n")
    return SysPaths(
        thermal=tmp_path / "temp",
        meminfo=tmp_path / "meminfo",
        model=tmp_path / "model",
        throttled_sysfs=tmp_path / "thr",
    )


def make(fs: Path, sys_paths: SysPaths, **kw: object) -> CgroupBody:
    body = CgroupBody(fs / REL, replace(CgroupSettings(), **kw), sys_paths=sys_paths)  # type: ignore[arg-type]
    body.setup()
    return body


def test_setup_moves_controller_and_enables_controllers(fs: Path, sys_paths: SysPaths) -> None:
    body = make(fs, sys_paths)
    root = fs / REL
    assert (root / "supervisor" / "cgroup.procs").read_text() == "4242"
    # the fake file keeps the last write; the kernel would list the enabled set
    assert (root / "cgroup.subtree_control").read_text() == "+memory +cpu +io"
    assert body.controllers == {"memory", "cpu", "io"}
    c = root / "creature"
    assert (c / "memory.swap.max").read_text() == "0"
    assert (c / "memory.oom.group").read_text() == "1"
    assert (c / "memory.max").read_text() == "max"
    assert (c / "cpu.max").read_text() == "max 100000"


def test_setup_is_idempotent_and_skips_enabled_controllers(fs: Path, sys_paths: SysPaths) -> None:
    root = fs / REL
    (root / "cgroup.subtree_control").write_text("cpu io memory\n")
    body = make(fs, sys_paths)
    assert (root / "cgroup.subtree_control").read_text() == "cpu io memory\n"
    assert body.controllers == {"cpu", "io", "memory"}
    body.setup()
    assert body.death_mode == "oom"


def test_no_memory_controller_falls_back_to_deadline(fs: Path, sys_paths: SysPaths) -> None:
    root = fs / REL
    (root / "cgroup.controllers").write_text("cpu io pids\n")
    (root / "cgroup.subtree_control").write_text("cpu io\n")
    body = make(fs, sys_paths)
    assert body.death_mode == "deadline"
    assert not (root / "creature" / "memory.max").exists()
    body.apply(knobs(death=True))
    assert not (root / "creature" / "memory.max").exists()


def test_delegated_finds_own_cgroup(fs: Path, tmp_path: Path) -> None:
    (fs / REL / "cgroup.subtree_control").write_text("cpu io memory\n")
    body = CgroupBody.delegated(fs=fs, proc_cgroup=tmp_path / "self_cgroup")
    assert body.root == fs / REL
    # a second controller start finds itself in supervisor/ and uses the parent
    (tmp_path / "self_cgroup").write_text(f"0::/{REL}/supervisor\n")
    assert CgroupBody.delegated(fs=fs, proc_cgroup=tmp_path / "self_cgroup").root == fs / REL


def test_delegated_errors(tmp_path: Path) -> None:
    (tmp_path / "self_cgroup").write_text("0::/nowhere\n")
    with pytest.raises(CgroupError):
        CgroupBody.delegated(fs=tmp_path, proc_cgroup=tmp_path / "self_cgroup")
    (tmp_path / "self_cgroup").write_text("0::/epitaph-x.service\n")
    with pytest.raises(CgroupError, match="not a cgroup"):
        CgroupBody.delegated(fs=tmp_path, proc_cgroup=tmp_path / "self_cgroup")
    unit = tmp_path / "epitaph-x.service"
    unit.mkdir()
    (unit / "cgroup.procs").write_text("")
    (unit / "cgroup.subtree_control").write_text("")
    (unit / "cgroup.subtree_control").chmod(0o444)
    if not os.access(unit / "cgroup.subtree_control", os.W_OK):  # root can write anyway
        with pytest.raises(CgroupError, match="not delegated"):
            CgroupBody.delegated(fs=tmp_path, proc_cgroup=tmp_path / "self_cgroup")
    (tmp_path / "v1").write_text("1:cpu:/x\n")
    with pytest.raises(CgroupError):
        own_cgroup(tmp_path / "v1")


def test_cpu_share(fs: Path, sys_paths: SysPaths) -> None:
    body = make(fs, sys_paths)
    cpu_max = fs / REL / "creature" / "cpu.max"
    body.apply(knobs(cpu_share=1.4))
    assert cpu_max.read_text() == "140000 100000"
    body.apply(knobs(cpu_share=3.0, threads=3))
    assert cpu_max.read_text() == "max 100000"
    body.apply(knobs(cpu_share=0.001))
    assert cpu_max.read_text() == "1000 100000"
    assert body.vitals().cores_effective == 0.001


def test_cpu_share_disabled(fs: Path, sys_paths: SysPaths) -> None:
    body = make(fs, sys_paths, cpu_share=False)
    body.apply(knobs(cpu_share=0.7))
    assert (fs / REL / "creature" / "cpu.max").read_text() == "max 100000"


def test_death_squeeze_once_with_fraction(fs: Path, sys_paths: SysPaths) -> None:
    body = make(fs, sys_paths)
    c = fs / REL / "creature"
    (c / "memory.current").write_text(str(2000 * 1024 * 1024))
    body.apply(knobs(death=False))
    assert (c / "memory.max").read_text() == "max"
    body.apply(knobs(death=True))  # no memory.stat: half of memory.current
    assert (c / "memory.max").read_text() == str(1000 * 1024 * 1024)
    body.reset_creature_cgroup()
    (c / "memory.stat").write_text(f"anon {300 * 1024 * 1024}\nfile {1900 * 1024 * 1024}\n")
    body.apply(knobs(death=True))  # half of anon: below what the kernel cannot reclaim
    assert (c / "memory.max").read_text() == str(150 * 1024 * 1024)
    (c / "memory.max").write_text("sentinel")
    body.apply(knobs(death=True))  # applied once per life
    assert (c / "memory.max").read_text() == "sentinel"


def test_death_squeeze_fixed_limit_and_modes(fs: Path, sys_paths: SysPaths) -> None:
    body = make(fs, sys_paths, death_limit_mb=64)
    body.apply(knobs(death=True))
    assert (fs / REL / "creature" / "memory.max").read_text() == str(64 * 1024 * 1024)
    for kw in ({"death_mode": "deadline"}, {"squeeze": "off"}):
        b = make(fs, sys_paths, **kw)
        b.apply(knobs(death=True))
        assert (fs / REL / "creature" / "memory.max").read_text() == "max"


def test_progress_counters(fs: Path, sys_paths: SysPaths) -> None:
    body = make(fs, sys_paths)
    c = fs / REL / "creature"
    (c / "cpu.stat").write_text("usage_usec 123456\nuser_usec 100000\nsystem_usec 23456\n")
    (c / "io.stat").write_text(
        "179:0 rbytes=1000 wbytes=5 rios=3 wios=1 dbytes=0 dios=0\n"
        "8:0 rbytes=24 wbytes=0 rios=1 wios=0 dbytes=0 dios=0\n"
    )
    (c / "memory.stat").write_text("anon 5\npgmajfault 77\npgfault 900\n")
    p = body.progress()
    assert (p.cpu_usec, p.io_rbytes, p.majfault) == (123456, 1024, 77)


def test_progress_without_files(tmp_path: Path) -> None:
    assert read_io_rbytes(tmp_path / "none") == 0
    assert read_flat_keyed(tmp_path / "none") == {}


def test_death_causes(fs: Path, sys_paths: SysPaths) -> None:
    c = fs / REL / "creature"
    (c).mkdir()
    (c / "memory.events").write_text("low 0\nhigh 0\nmax 3\noom 1\noom_kill 1\noom_group_kill 0\n")
    body = make(fs, sys_paths)
    died = CreatureStatus(alive=False, pid=1, signal=signal.SIGKILL)
    assert body.death_cause(died) == Cause.CRASH  # old OOM counts are the baseline
    (c / "memory.events").write_text("max 9\noom 2\noom_kill 2\noom_group_kill 1\n")
    assert body.death_cause(died) == Cause.OOM
    body.reset_creature_cgroup()
    assert body.death_cause(died) == Cause.CRASH
    body.apply(knobs(death=True))
    assert body.death_cause(died) == Cause.OOM  # SIGKILL after the squeeze
    assert body.death_cause(CreatureStatus(alive=False, exit_code=1)) == Cause.CRASH
    body.kill_now(Cause.DEADLINE)
    assert (c / "cgroup.kill").read_text() == "1"
    assert body.death_cause(died) == Cause.DEADLINE


def test_reset_kills_leftover_creature(fs: Path, sys_paths: SysPaths) -> None:
    body = make(fs, sys_paths, kill_wait_s=0.1)
    c = fs / REL / "creature"
    (c / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    body.reset_creature_cgroup()  # the fake never empties: logs, carries on
    assert (c / "cgroup.kill").read_text() == "1"
    (c / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    assert body.wait_empty(0.1)


def test_every_life_starts_with_swap_off(fs: Path, sys_paths: SysPaths) -> None:
    """The Pi keeps zram swap for the system (F11); the creature never gets any of it."""
    body = make(fs, sys_paths)
    c = fs / REL / "creature"
    (c / "memory.swap.max").write_text("max")  # whatever a previous life or a person left
    body.reset_creature_cgroup()
    assert (c / "memory.swap.max").read_text() == "0"


def test_populated_from_procs(fs: Path, sys_paths: SysPaths) -> None:
    body = make(fs, sys_paths)
    (fs / REL / "creature" / "cgroup.procs").write_text("11\n12\n")
    assert body.pids() == [11, 12]
    assert body.populated()


def test_vitals_and_facts(fs: Path, sys_paths: SysPaths) -> None:
    body = make(fs, sys_paths)
    c = fs / REL / "creature"
    (c / "memory.current").write_text(str(1500 * 1024 * 1024))
    v = body.vitals()
    assert v.cpu_c == 51.2
    assert v.throttled == 0x50000
    assert v.ram_limit_mb is None and v.mem_used_mb == 1500
    assert v.cores_effective == 3.0
    (c / "memory.max").write_text(str(700 * 1024 * 1024))
    assert body.vitals().ram_limit_mb == 700
    f = body.facts()
    assert (f.model, f.ram_gb) == ("Raspberry Pi 4 Model B", 4.0)


def test_wrap_spawn_joins_cgroup_before_exec(tmp_path: Path) -> None:
    procs = tmp_path / "cgroup.procs"
    argv = wrap_argv(["sh", "-c", "echo $$"], procs, "")
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout.strip()
    assert procs.read_text().strip() == out  # same pid: the shell exec'd the command


def test_wrap_spawn_pins_cpus(fs: Path, sys_paths: SysPaths) -> None:
    body = make(fs, sys_paths)
    argv = body.wrap_spawn(["llama-server", "-m", "x"])
    assert argv[:3] == ["/bin/sh", "-c", 'echo $$ > "$0" && exec "$@"']
    assert argv[3] == str(fs / REL / "creature" / "cgroup.procs")
    assert argv[-3:] == ["llama-server", "-m", "x"]
    if "taskset" in argv:
        assert argv[argv.index("taskset") + 2] == "1-3"


def test_settings(pi4_default) -> None:
    s = CgroupSettings.from_config(pi4_default)
    assert s.cpu_count == 3 and s.death_mode in ("oom", "deadline")
    assert CgroupSettings(creature_cpus="").cpu_count == 0
    assert CgroupSettings(creature_cpus="0,2-3").cpu_count == 3


def test_plain_body(sys_paths: SysPaths) -> None:
    b = PlainBody(CgroupSettings(creature_cpus=""), sys_paths)
    b.reset_creature_cgroup()
    assert b.wrap_spawn(["x"]) == ["x"]
    b.apply(knobs(cpu_share=1.5))
    assert b.vitals().cores_effective == 1.5
    assert b.progress().cpu_usec == 0
    assert b.death_cause(CreatureStatus(alive=False)) == Cause.CRASH
    b.kill_now(Cause.HANG)
    assert b.death_cause(CreatureStatus(alive=False)) == Cause.HANG
    assert b.facts().cores >= 1
    assert b.death_mode == "deadline"


def test_make_body_on_laptop(fs: Path, tmp_path: Path) -> None:
    cfg = load_config("pi4/default", "dev")
    assert isinstance(make_body(cfg, fs, tmp_path / "self_cgroup"), PlainBody)  # cgroups off
    cfg.data["body"]["cgroups"] = "auto"
    (tmp_path / "self_cgroup").write_text("0::/nowhere\n")
    assert isinstance(make_body(cfg, fs, tmp_path / "self_cgroup"), PlainBody)


def test_make_body_on_pi(fs: Path, tmp_path: Path) -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    cfg.data["body"]["cgroups"] = "auto"
    # the Pi overlay blocks the creature's network through the helper, absent here: refused
    with pytest.raises(CgroupError, match="network"):
        make_body(cfg, fs, tmp_path / "self_cgroup")
    cfg.data["body"]["netblock_helper"] = ""
    assert isinstance(make_body(cfg, fs, tmp_path / "self_cgroup"), CgroupBody)
    (tmp_path / "self_cgroup").write_text("0::/nowhere\n")
    with pytest.raises(CgroupError):
        make_body(cfg, fs, tmp_path / "self_cgroup")


def test_never_adopts_a_foreign_scope(tmp_path: Path) -> None:
    """A delegated terminal scope is not the controller's: setup() must not touch it."""
    rel = "user.slice/user@1000.service/app.slice/vte-spawn-1.scope"
    root = tmp_path / "cg" / rel
    root.mkdir(parents=True)
    for name, text in (("cgroup.procs", "1\n"), ("cgroup.subtree_control", "")):
        (root / name).write_text(text)
    (tmp_path / "self").write_text(f"0::/{rel}\n")
    with pytest.raises(CgroupError, match="not an epitaph"):
        CgroupBody.delegated(fs=tmp_path / "cg", proc_cgroup=tmp_path / "self")
    assert not (root / "supervisor").exists()


def test_drop_page_cache(tmp_path: Path) -> None:
    f = tmp_path / "model.gguf"
    f.write_bytes(b"x" * 8192)
    drop_page_cache(f)  # advisory: must not fail or change the file
    assert f.read_bytes() == b"x" * 8192
