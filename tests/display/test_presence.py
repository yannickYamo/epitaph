"""D6: `epitaph display --screen-present`, the display unit's ExecCondition (gate G1.3)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from epitaph import cli
from epitaph.display import presence, remote

SRC = Path(__file__).resolve().parents[2] / "src"


def drm_tree(root: Path, connectors: dict[str, str]) -> Path:
    """A fake /sys/class/drm: `card1`, `renderD128`, `version`, and the given connectors."""
    drm = root / "drm"
    (drm / "card1").mkdir(parents=True)
    (drm / "renderD128").mkdir()
    (drm / "version").write_text("drm 1.1.0 20060810\n")
    for name, status in connectors.items():
        (drm / name).mkdir()
        (drm / name / "status").write_text(status + "\n")
        (drm / name / "enabled").write_text("disabled\n")
    return drm


PI4_HEADLESS = {"card1-HDMI-A-1": "disconnected", "card1-HDMI-A-2": "disconnected"}
PI4_SCREEN = {"card1-HDMI-A-1": "disconnected", "card1-HDMI-A-2": "connected"}


def no_config() -> None:
    return None


def test_headless_pi_has_no_screen(tmp_path: Path) -> None:
    drm = drm_tree(tmp_path, PI4_HEADLESS)
    p = presence.detect(drm, env={}, config_screen=no_config)
    assert not p.present and p.source == "drm"
    assert "card1-HDMI-A-1=disconnected" in p.detail
    assert p.line().startswith("screen: no (drm: ")


def test_a_connected_connector_is_a_screen(tmp_path: Path) -> None:
    drm = drm_tree(tmp_path, PI4_SCREEN)
    p = presence.detect(drm, env={}, config_screen=no_config)
    assert p.present and p.detail == "card1-HDMI-A-2"
    assert presence.connectors(drm) == [
        ("card1-HDMI-A-1", "disconnected"),
        ("card1-HDMI-A-2", "connected"),
    ]


def test_dsi_panel_counts_and_writeback_never_does(tmp_path: Path) -> None:
    drm = drm_tree(tmp_path, {"card1-Writeback-1": "connected", **PI4_HEADLESS})
    assert not presence.screen_present(drm)
    assert all("Writeback" not in n for n, _ in presence.connectors(drm))
    (drm / "card1-DSI-1").mkdir()
    (drm / "card1-DSI-1" / "status").write_text("connected\n")
    assert presence.screen_present(drm)


def test_unknown_status_is_not_a_screen(tmp_path: Path) -> None:
    drm = drm_tree(tmp_path, {"card1-Composite-1": "unknown"})
    assert not presence.screen_present(drm)


def test_missing_or_unreadable_sysfs(tmp_path: Path) -> None:
    assert presence.connectors(tmp_path / "missing") == []
    p = presence.detect(tmp_path / "missing", env={}, config_screen=no_config)
    assert not p.present and "no connectors under" in p.detail
    drm = drm_tree(tmp_path, {})
    (drm / "card1-HDMI-A-1" / "status").mkdir(parents=True)  # read_text raises
    assert presence.connectors(drm) == []


def test_environment_override_wins(tmp_path: Path) -> None:
    headless = drm_tree(tmp_path / "a", PI4_HEADLESS)
    screen = drm_tree(tmp_path / "b", PI4_SCREEN)
    p = presence.detect(headless, env={"EPITAPH_SCREEN": "yes"}, config_screen=lambda: "no")
    assert p.present and p.source == "env"
    p = presence.detect(screen, env={"EPITAPH_SCREEN": "no"}, config_screen=no_config)
    assert not p.present and p.line() == "screen: no (env: EPITAPH_SCREEN=no)"
    # auto falls through to the config and the connectors
    p = presence.detect(screen, env={"EPITAPH_SCREEN": "auto"}, config_screen=no_config)
    assert p.present and p.source == "drm"


def test_config_override(tmp_path: Path) -> None:
    headless = drm_tree(tmp_path, PI4_HEADLESS)
    p = presence.detect(headless, env={}, config_screen=lambda: "yes")
    assert p.present and p.source == "config" and "screen = 'yes'" in p.detail
    p = presence.detect(headless, env={}, config_screen=lambda: False)
    assert not p.present and p.source == "config"
    assert presence.detect(headless, env={}, config_screen=lambda: "auto").source == "drm"


def test_bad_overrides_are_reported_and_ignored(tmp_path: Path) -> None:
    screen = drm_tree(tmp_path, PI4_SCREEN)
    p = presence.detect(screen, env={"EPITAPH_SCREEN": "maybe"}, config_screen=lambda: 3)
    assert p.present and p.source == "drm"
    assert "EPITAPH_SCREEN ignored" in p.detail and "config ignored" in p.detail

    def broken() -> None:
        raise RuntimeError("config dir unreadable")

    p = presence.detect(screen, env={}, config_screen=broken)
    assert p.present and "config dir unreadable" in p.detail


@pytest.mark.parametrize(
    ("value", "want"),
    [
        ("yes", True),
        ("ON", True),
        (True, True),
        ("no", False),
        ("0", False),
        (False, False),
        ("auto", None),
        ("", None),
        (None, None),
        (" Auto ", None),
    ],
)
def test_parse_override(value: object, want: bool | None) -> None:
    assert presence.parse_override(value) is want


def test_parse_override_rejects_typos() -> None:
    with pytest.raises(ValueError, match="yes, no or auto"):
        presence.parse_override("yse")


def test_config_value_is_read_from_the_display_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import epitaph.config as config

    (tmp_path / "hardware").mkdir()
    (tmp_path / "default.toml").write_text('[display]\nscreen = "yes"\n')
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    p = presence.detect(drm_tree(tmp_path, PI4_HEADLESS), env={}, hardware="dev")
    assert p.present and p.source == "config"


def test_check_screen_prints_one_line_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("EPITAPH_SCREEN", raising=False)
    monkeypatch.setattr(presence, "_config_screen", lambda hw: None)
    monkeypatch.setattr(presence, "DRM", drm_tree(tmp_path / "a", PI4_HEADLESS))
    assert remote.check_screen() == 1
    assert capsys.readouterr().out.startswith("screen: no (drm: ")
    assert remote.check_screen(drm=drm_tree(tmp_path / "b", PI4_SCREEN)) == 0

    def boom(*a: object, **k: object) -> presence.Presence:
        raise MemoryError("no memory")

    monkeypatch.setattr(presence, "detect", boom)
    assert remote.check_screen() == 1
    assert capsys.readouterr().out.splitlines()[-1] == "screen: no (error: no memory)"


def test_cli_display_screen_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both routes into `epitaph display`: the fast pass-through and the full parser."""
    monkeypatch.setattr(presence, "_config_screen", lambda hw: None)
    monkeypatch.setattr(presence, "DRM", drm_tree(tmp_path, PI4_HEADLESS))
    monkeypatch.setenv("EPITAPH_SCREEN", "auto")
    assert cli.main(["display", "--screen-present"]) == 1
    args = cli.build_parser().parse_args(["display", "--screen-present", "--hardware", "dev"])
    assert args.fn is cli.cmd_display and args.fn(args) == 1
    monkeypatch.setenv("EPITAPH_SCREEN", "yes")
    assert cli.main(["display", "--screen-present"]) == 0
    assert args.fn(args) == 0


def test_cli_display_has_every_remote_flag() -> None:
    args = cli.build_parser().parse_args(
        ["display", "--connect", "pi", "--driver", "terminal", "--ssh", "/bin/ssh", "--fps", "5"]
    )
    assert args.connect == "pi" and args.driver == "terminal" and args.ssh == "/bin/ssh"
    assert args.fps == 5.0 and not args.screen_present


def test_pick_driver_honours_the_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(presence, "_config_screen", lambda hw: None)
    monkeypatch.setenv("EPITAPH_SCREEN", "yes")
    assert remote.pick_driver("auto", remote=False) == "screen"
    monkeypatch.setenv("EPITAPH_SCREEN", "no")
    assert remote.pick_driver("auto", remote=False) == "terminal"


@pytest.mark.parametrize(("value", "code"), [("no", 1), ("yes", 0)])
def test_the_exec_condition_as_systemd_runs_it(value: str, code: int, tmp_path: Path) -> None:
    """A fresh process, a broken config dir: exit 0 or 1, one line, never a traceback."""
    (tmp_path / "default.toml").write_text("[display\nnot toml")
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "EPITAPH_SCREEN": value,
        "EPITAPH_CONFIG_DIR": str(tmp_path),
    }
    run = subprocess.run(
        [sys.executable, "-m", "epitaph", "display", "--screen-present"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == code
    assert "Traceback" not in run.stderr + run.stdout
    assert run.stdout.startswith(f"screen: {value} (env: EPITAPH_SCREEN={value})")
    env["EPITAPH_SCREEN"] = "auto"  # the broken config is ignored, the connectors decide
    run = subprocess.run(
        [sys.executable, "-m", "epitaph", "display", "--screen-present"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode in (0, 1) and "Traceback" not in run.stderr
