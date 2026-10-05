"""Is a screen connected? The display unit's `ExecCondition` (9 D6).

    epitaph display --screen-present     # exit 0: a screen is connected; 1: none

systemd runs the display unit only when this exits 0, so a headless Pi skips the unit
cleanly instead of crash-looping (gate G1.3). The answer comes from, in order:

1. `EPITAPH_SCREEN` in the environment (`yes`, `no` or `auto`), for a unit drop-in;
2. `[display] screen` in the config (`"yes"`, `"no"` or `"auto"`, default `"auto"`), for a
   panel whose connector never reports `connected` (some SPI and composite panels) or a
   screen that should be ignored;
3. the kernel's DRM connectors: any `/sys/class/drm/card*-*/status` reading `connected`
   (writeback connectors excluded: they are not screens).

Everything here is cheap and never raises: an unreadable config or sysfs tree counts as
"auto" and "no connector", and the command prints one line saying why.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DRM = Path("/sys/class/drm")
ENV = "EPITAPH_SCREEN"

_YES = {"yes", "true", "1", "on", "present"}
_NO = {"no", "false", "0", "off", "absent"}


@dataclass(frozen=True)
class Presence:
    """The answer and where it came from (`env`, `config` or `drm`)."""

    present: bool
    source: str
    detail: str

    def line(self) -> str:
        """One line for the journal, e.g. `screen: yes (drm: card1-HDMI-A-1)`."""
        return f"screen: {'yes' if self.present else 'no'} ({self.source}: {self.detail})"


def parse_override(value: Any) -> bool | None:
    """`yes`/`no`/`auto` (or a TOML boolean) as True, False or None (auto).

    Raises ValueError for anything else, so a typo in the config is reported, not ignored.
    """
    if value is None or isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("", "auto"):
        return None
    if text in _YES:
        return True
    if text in _NO:
        return False
    raise ValueError(f"screen must be yes, no or auto, not {value!r}")


def connectors(drm: Path | None = None) -> list[tuple[str, str]]:
    """Every DRM connector under `drm` (default `DRM`) as (name, status), sorted.

    Empty when sysfs is not there. Writeback connectors are left out: they report `unknown`
    today, but they are never a screen whatever they report.
    """
    drm = DRM if drm is None else drm
    out: list[tuple[str, str]] = []
    try:
        paths = sorted(drm.glob("card*-*/status"))
    except OSError:
        return out
    for status in paths:
        name = status.parent.name
        if "-Writeback-" in name:
            continue
        try:
            out.append((name, status.read_text(errors="replace").strip()))
        except OSError:
            continue
    return out


def screen_present(drm: Path | None = None) -> bool:
    """True when a DRM connector reports `connected` (no overrides)."""
    return any(state == "connected" for _, state in connectors(drm))


def _config_screen(hardware: str | None) -> Any:
    from epitaph.display.app import display_config

    return display_config(hardware).get("screen")


def detect(
    drm: Path | None = None,
    env: Mapping[str, str] | None = None,
    config_screen: Callable[[], Any] | None = None,
    hardware: str | None = None,
) -> Presence:
    """Decide whether a screen is connected: environment, then config, then DRM.

    `config_screen` returns the `[display] screen` value (default: read from the config
    files for `hardware`). A bad override value is reported in `detail` and treated as
    `auto`, so the DRM connectors still decide.
    """
    env = os.environ if env is None else env
    notes: list[str] = []
    raw_env = env.get(ENV)
    if raw_env is not None:
        try:
            forced = parse_override(raw_env)
        except ValueError as e:
            notes.append(f"{ENV} ignored: {e}")
        else:
            if forced is not None:
                return Presence(forced, "env", f"{ENV}={raw_env}")
    try:
        raw_cfg = (config_screen or (lambda: _config_screen(hardware)))()
        forced = parse_override(raw_cfg)
    except Exception as e:  # an unreadable or odd config must not stop the decision
        notes.append(f"config ignored: {e}")
        forced = None
    if forced is not None:
        return Presence(forced, "config", f"[display] screen = {raw_cfg!r}")
    found = connectors(drm)
    live = [name for name, state in found if state == "connected"]
    if live:
        detail = ", ".join(live)
    elif found:
        detail = "no connector connected (" + ", ".join(f"{n}={s}" for n, s in found) + ")"
    else:
        detail = f"no connectors under {DRM if drm is None else drm}"
    return Presence(bool(live), "drm", "; ".join([detail, *notes]))
