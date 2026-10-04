"""`epitaph probe`: what a machine has for a life to lose, read without changing anything."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from epitaph.body import probe
from epitaph.cli import main


def machine(
    root: Path, controllers: str = "cpu io memory", cpufreq: bool = True
) -> dict[str, Path]:
    (root / "cgroup").mkdir(parents=True)
    (root / "cgroup" / "cgroup.controllers").write_text(controllers + "\n")
    if cpufreq:
        policy = root / "cpufreq" / "policy0"
        policy.mkdir(parents=True)
        (policy / "cpuinfo_min_freq").write_text("600000\n")
        (policy / "cpuinfo_max_freq").write_text("1800000\n")
    for led in ("ACT", "mmc0"):
        (root / "leds" / led).mkdir(parents=True)
        (root / "leds" / led / "brightness").write_text("1\n")
    (root / "drm" / "card1-HDMI-A-1").mkdir(parents=True)
    (root / "drm" / "card1-HDMI-A-1" / "status").write_text("connected\n")
    for pid in (1, 2, 3):
        (root / "proc" / str(pid)).mkdir(parents=True)
        (root / "proc" / str(pid) / "cmdline").write_bytes(b"thing\x00")
    (root / "proc" / "meminfo").write_text("MemTotal:        3884000 kB\n")
    return {
        "cgroup": root / "cgroup",
        "cpufreq": root / "cpufreq",
        "leds": root / "leds",
        "drm": root / "drm",
        "proc": root / "proc",
    }


def query(argv: Sequence[str]) -> tuple[int, str]:
    if argv[0] == "systemctl":  # cron and bluetooth run; the others do not
        return 0, "\n".join(
            "active" if a in ("cron.service", "bluetooth.service") else "inactive" for a in argv[2:]
        )
    return 0, "enabled\n"


def test_a_board_with_everything(tmp_path: Path) -> None:
    found = probe.probe(query, **machine(tmp_path))
    assert all(f.ok for f in found)
    by = {f.what: f.detail for f in found}
    assert by["services"] == "cron, bluetooth"
    assert by["processes around it"] == "3 run now"
    assert by["light"] == "ACT" and by["CPU clock"] == "600 to 1800 MHz"
    lines: list[str] = []
    assert probe.report(found, "orange", lines.append) == 0
    text = "\n".join(lines)
    assert 'services = ["cron", "bluetooth"]' in text
    assert 'clock_helper = "/usr/local/sbin/epitaph-clock"' in text
    assert "config/hardware/orange.toml" in text


def test_a_bare_board_says_what_is_missing(tmp_path: Path) -> None:
    paths = machine(tmp_path, controllers="cpu", cpufreq=False)
    (paths["leds"] / "ACT" / "brightness").unlink()
    (paths["drm"] / "card1-HDMI-A-1" / "status").write_text("disconnected\n")
    found = probe.probe(lambda argv: (127, ""), **paths)
    by = {f.what: f for f in found}
    assert [f.what for f in found if f.ok] == ["processes around it", "CPU share"]
    assert "name yours" in by["services"].detail and "nmcli" in by["radio"].detail
    assert "mmc0" in by["light"].detail and "never lowered" in by["CPU clock"].detail
    assert "cgroup_enable=memory" in by["RAM (the death)"].detail
    lines: list[str] = []
    assert probe.report(found, "bare", lines.append) == 1  # no RAM death: not ready
    text = "\n".join(lines)
    assert 'clock_helper = ""' in text and "services = []" in text and "fix that first" in text
    assert by["radio"].line().startswith("no ")


def test_the_command_reads_this_machine(capsys: object) -> None:
    assert main(["probe", "--name", "here"]) in (0, 1)  # read-only: any machine answers
