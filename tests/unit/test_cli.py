from __future__ import annotations

from typing import Any

import pytest

from epitaph.cli import main
from epitaph.mind.prompt import Reader


def test_sim_and_estimate(capsys) -> None:
    assert main(["sim", "--profile", "pi4/smoke-300", "--hardware", "pi4-4gb", "--quiet"]) == 0
    assert "cause=deadline" in capsys.readouterr().out
    assert main(["estimate", "--profile", "pi4/default", "--hardware", "pi4-4gb"]) == 0
    assert "PASS" in capsys.readouterr().out


def test_sim_fails_when_the_loop_fails(capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (round 9): a reading that raised (KeyError on a pack's `{frac}`) killed every
    life at its second reading, and `epitaph sim` still exited 0, so the gate passed."""
    real = Reader.reading

    def broken(self: Reader, x: Any) -> str:
        if self.count >= 1:
            raise KeyError("frac")
        return real(self, x)

    monkeypatch.setattr(Reader, "reading", broken)
    argv = ["sim", "--profile", "pi4/smoke-300", "--hardware", "pi4-4gb", "--quiet"]
    assert main(argv) == 1
    assert "the loop failed: KeyError('frac')" in capsys.readouterr().err


def test_stub_names_owner(capsys) -> None:
    assert main(["bench"]) == 3
    assert "planned for phase 2" in capsys.readouterr().err


def test_config_error_exit_code(capsys) -> None:
    assert (
        main(
            ["estimate", "--profile", "pi4/default", "--hardware", "pi4-4gb", "--lifespan", "10:00"]
        )
        == 2
    )
    assert "config error" in capsys.readouterr().err


def test_ctl_without_controller(capsys) -> None:
    assert main(["ctl", "status", "--port", "1"]) == 2


def test_display_and_replay_are_wired(capsys) -> None:
    import pytest

    for cmd in ("display", "replay"):
        with pytest.raises(SystemExit) as e:
            main([cmd, "--help"])
        assert e.value.code == 0
        assert "usage" in capsys.readouterr().out


def test_verify_life_is_wired(tmp_path, capsys) -> None:
    import contextlib
    import io
    import json

    from epitaph.config import load_config
    from epitaph.sim import simulate

    r = simulate(load_config("pi4/smoke-300", "pi4-4gb"))
    f = tmp_path / "events.jsonl"
    f.write_text("".join(json.dumps(e) + "\n" for e in r.events))
    with contextlib.redirect_stdout(io.StringIO()):
        rc = main(
            [
                "verify-life",
                str(f),
                "--profile",
                "pi4/smoke-300",
                "--hardware",
                "pi4-4gb",
                "--no-write",
            ]
        )
    assert rc == 0


def test_rehearse_is_wired_and_estimate_takes_a_bench_dir(tmp_path, capsys) -> None:
    import pytest

    with pytest.raises(SystemExit) as e:
        main(["rehearse", "--help"])
    assert e.value.code == 0
    capsys.readouterr()
    rc = main(
        ["estimate", "--profile", "pi4/default", "--hardware", "pi4-4gb", "--bench", str(tmp_path)]
    )
    assert rc in (0, 1) and "costs from" in capsys.readouterr().out
