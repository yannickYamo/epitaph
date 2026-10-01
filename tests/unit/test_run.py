"""`epitaph run` on the laptop: the real controller with the fake creature, in virtual time."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from epitaph.cli import main
from epitaph.state import InstanceLock, LifeCounter, life_dir

RUN = ["run", "--profile", "pi4/smoke-300", "--hardware", "pi4-4gb", "--backend", "fake"]
FAST = ["--clock", "fake", "--port", "0"]


def test_one_smoke_life_in_the_terminal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = [*RUN, "--display", "terminal", "--lives", "1", *FAST, "--state-dir", str(tmp_path)]
    assert main(argv) == 0
    out = capsys.readouterr()
    assert "events on 127.0.0.1:" in out.err
    shown = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out.out)
    record = json.loads((life_dir(tmp_path, 1) / "death.json").read_text())
    assert record["cause"] == "deadline" and record["lived_s"] == pytest.approx(300.0)
    assert record["last_line"].split()[-1] in shown  # the last word reached the terminal
    assert LifeCounter(tmp_path).current() == 1
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "stopped" and status["lives_run"] == 1
    # a second run goes on counting
    assert (
        main([*RUN, "--display", "none", "--lives", "1", *FAST, "--state-dir", str(tmp_path)]) == 0
    )
    assert LifeCounter(tmp_path).current() == 2


def test_a_second_controller_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with InstanceLock(tmp_path):
        assert main([*RUN, "--lives", "1", *FAST, "--state-dir", str(tmp_path)]) == 1
    assert "ctl new-life" in capsys.readouterr().err


def test_the_fake_clock_needs_the_fake_creature(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["run", "--profile", "pi4/smoke-300", "--hardware", "pi4-4gb", "--backend"]
    argv += ["llama_server", *FAST, "--state-dir", str(tmp_path)]
    assert main(argv) == 2
    assert "--backend fake" in capsys.readouterr().err
