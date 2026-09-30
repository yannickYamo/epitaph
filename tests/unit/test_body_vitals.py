"""Vitals readers against fake /sys and /proc files (BUILD_PLAN 9 C4)."""

from __future__ import annotations

import stat
from pathlib import Path

from epitaph.body.vitals import (
    SysPaths,
    VitalsReader,
    describe_throttled,
    machine_facts,
    parse_throttled,
    read_cpu_temp,
    read_meminfo,
    read_throttled,
)


def test_missing_files_give_none(tmp_path: Path) -> None:
    p = SysPaths(
        thermal=tmp_path / "no",
        meminfo=tmp_path / "no",
        model=tmp_path / "no",
        throttled_sysfs=tmp_path / "no",
        vcgencmd="no-such-vcgencmd",
    )
    assert read_cpu_temp(p) is None
    assert read_throttled(p) is None
    assert read_meminfo(p) == {}
    f = machine_facts(p)
    assert f.ram_gb == 0.0 and f.cores >= 1 and f.model


def test_throttled_via_vcgencmd(tmp_path: Path) -> None:
    exe = tmp_path / "vcgencmd"
    exe.write_text("#!/bin/sh\necho throttled=0x50005\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    p = SysPaths(throttled_sysfs=tmp_path / "no", vcgencmd=str(exe))
    bits = read_throttled(p)
    assert bits == 0x50005
    assert describe_throttled(bits) == [
        "under-voltage now",
        "throttled now",
        "under-voltage occurred",
        "throttling occurred",
    ]
    reader = VitalsReader(p, throttle_every_s=3600)
    assert reader.read().throttled == 0x50005
    exe.write_text("#!/bin/sh\necho throttled=0x0\n")
    assert reader.read().throttled == 0x50005  # cached between calls


def test_parse_throttled() -> None:
    assert parse_throttled("throttled=0x0\n") == 0
    assert parse_throttled("garbage") is None


def test_facts_pi5_8gb(tmp_path: Path) -> None:
    (tmp_path / "m").write_text("MemTotal:        8245000 kB\n")
    (tmp_path / "model").write_text("Raspberry Pi 5 Model B Rev 1.0\x00")
    f = machine_facts(SysPaths(meminfo=tmp_path / "m", model=tmp_path / "model"))
    assert (f.model, f.ram_gb) == ("Raspberry Pi 5 Model B", 8.0)
