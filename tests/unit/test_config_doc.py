"""docs/CONFIG.md cannot drift from the configuration files (BUILD_PLAN 9 E8).

Every key in config/default.toml, the hardware overlays, the profiles and config/models.toml has
a row in CONFIG.md; every row names a key that exists; a default written as one TOML value
equals the value in default.toml.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"
DOC = ROOT / "docs" / "CONFIG.md"

_SECTION = re.compile(r"`\[\[?([a-z_]+)(?:\.\"[^\"]*\")?\]\]?`")
_ROW = re.compile(r"^\|\s*`([a-z_0-9]+)`\s*\|\s*(.*?)\s*\|")
_ONE_CODE = re.compile(r"^`([^`]+)`$")


def documented() -> dict[str, str]:
    """Namespaced key ("life.profile", "keyframe.recall", "class") -> its Default cell."""
    out: dict[str, str] = {}
    ns = ""
    for line in DOC.read_text().splitlines():
        if line.startswith("#"):
            m = _SECTION.search(line)
            ns = m.group(1) if m else ""
            continue
        m = _ROW.match(line)
        if m:
            key = f"{ns}.{m.group(1)}" if ns else m.group(1)
            assert key not in out, f"CONFIG.md documents {key} twice"
            out[key] = m.group(2)
    return out


def _load(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def _flat(data: dict[str, Any]) -> set[str]:
    """Top-level values as "k", one level of tables as "section.k"."""
    out: set[str] = set()
    for k, v in data.items():
        if isinstance(v, dict):
            out |= {f"{k}.{kk}" for kk in v}
        else:
            out.add(k)
    return out


def default_keys() -> dict[str, Any]:
    data = _load(CONFIG / "default.toml")
    return {f"{s}.{k}": v for s, t in data.items() for k, v in t.items()}


def overlay_keys() -> set[str]:
    out: set[str] = set()
    for f in sorted((CONFIG / "hardware").glob("*.toml")):
        out |= _flat(_load(f))
    return out


def profile_keys() -> set[str]:
    out: set[str] = set()
    for f in sorted((CONFIG / "profiles").rglob("*.toml")):
        data = _load(f)
        for k, v in data.items():
            if k == "keyframe":
                out |= {f"keyframe.{kk}" for row in v for kk in row}
            elif isinstance(v, dict):
                out |= {f"{k}.{kk}" for kk in v}
            else:
                out.add(k)
    return out


def model_keys() -> set[str]:
    data = _load(CONFIG / "models.toml")
    out = {k for k, v in data.items() if not isinstance(v, dict)}
    for spec in data.get("models", {}).values():
        out |= {f"models.{k}" for k in spec}
    return out


def test_every_key_in_the_files_is_documented() -> None:
    doc = documented()
    files = {
        "config/default.toml": set(default_keys()),
        "config/hardware/*.toml": overlay_keys(),
        "config/profiles/**/*.toml": profile_keys(),
        "config/models.toml": model_keys(),
    }
    missing = {where: sorted(keys - set(doc)) for where, keys in files.items()}
    assert not any(missing.values()), f"keys without a row in docs/CONFIG.md: {missing}"


def test_every_documented_key_exists() -> None:
    known = set(default_keys()) | overlay_keys() | profile_keys() | model_keys()
    stale = sorted(set(documented()) - known)
    assert not stale, f"docs/CONFIG.md documents keys no file has: {stale}"


def test_documented_defaults_match_default_toml() -> None:
    values = default_keys()
    doc = documented()
    checked = 0
    for key, cell in doc.items():
        m = _ONE_CODE.match(cell)
        if key not in values or not m:
            continue
        got = tomllib.loads(f"v = {m.group(1)}")["v"]
        assert got == values[key], (
            f"CONFIG.md says {key} = {m.group(1)}, default.toml has {values[key]!r}"
        )
        checked += 1
    assert checked > 100  # nearly every default is written as its TOML value
