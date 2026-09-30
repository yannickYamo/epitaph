from __future__ import annotations

from epitaph.cli import main


def test_sim_and_estimate(capsys) -> None:
    assert main(["sim", "--profile", "pi4/smoke-300", "--hardware", "pi4-4gb", "--quiet"]) == 0
    assert "cause=deadline" in capsys.readouterr().out
    assert main(["estimate", "--profile", "pi4/default", "--hardware", "pi4-4gb"]) == 0
    assert "PASS" in capsys.readouterr().out


def test_stub_names_owner(capsys) -> None:
    assert main(["rehearse"]) == 3
    assert "part A" in capsys.readouterr().err


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
