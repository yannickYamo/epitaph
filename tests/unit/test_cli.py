from __future__ import annotations

from epitaph.cli import main


def test_sim_and_estimate(capsys) -> None:
    assert main(["sim", "--profile", "pi4/smoke-300", "--hardware", "pi4-4gb", "--quiet"]) == 0
    assert "cause=deadline" in capsys.readouterr().out
    assert main(["estimate", "--profile", "pi4/default", "--hardware", "pi4-4gb"]) == 0
    assert "PASS" in capsys.readouterr().out


def test_stub_names_owner(capsys) -> None:
    assert main(["rehearse"]) == 3
    assert "agent A" in capsys.readouterr().err


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
