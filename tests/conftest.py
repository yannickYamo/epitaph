"""Shared fixtures (BUILD_PLAN 9 E1, 10). part E owns this file.

- configs: `pi4_default`, `load_cfg` (any profile and overlay)
- fakes: `fake_clock`, `pi4_costs`, `fake_backend`, `fake_body`
- events: `recorder` (EventRecorder), `life_builder` (crafted lives)
- recorded lives: `recorded_life(profile, lives=1, seed=0)` writes `epitaph sim --events`
  output to lives/<n>/events.jsonl (one folder per life, like the state dir) and returns the
  state dir; results are cached for the session
- `state_dir`: an empty state dir

Directory markers: tests under tests/pi are marked `pi`, tests/display `display`,
tests/templates `model`, so `pytest -m 'not pi and not model'` (the default addopts) skips
what needs hardware or a model file.
"""

from __future__ import annotations

import contextlib
import io
from collections.abc import Callable
from pathlib import Path

import pytest

from epitaph.backend.fake import FakeBackend
from epitaph.body.fake import FakeBody
from epitaph.cli import main as cli_main
from epitaph.clock import FakeClock
from epitaph.config import Config, load_config
from epitaph.costmodel import Costs, load_costs
from tests.helpers import EventRecorder, LifeBuilder, read_events, write_events

_DIR_MARKS = {"pi": "pi", "display": "display", "templates": "model"}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    root = Path(__file__).parent
    for item in items:
        try:
            rel = Path(str(item.fspath)).relative_to(root)
        except ValueError:
            continue
        mark = _DIR_MARKS.get(rel.parts[0]) if len(rel.parts) > 1 else None
        if mark:
            item.add_marker(getattr(pytest.mark, mark))


# -- configs ------------------------------------------------------------------------------


@pytest.fixture
def pi4_default() -> Config:
    return load_config("pi4/default", "pi4-4gb")


@pytest.fixture
def load_cfg() -> Callable[..., Config]:
    """load_cfg("pi4/skeleton-1200", "pi4-4gb", overrides={...})"""
    return load_config


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


# -- fakes --------------------------------------------------------------------------------


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def pi4_costs(pi4_default: Config) -> Costs:
    return load_costs(pi4_default)


@pytest.fixture
def fake_backend(fake_clock: FakeClock, pi4_costs: Costs) -> FakeBackend:
    return FakeBackend(fake_clock, pi4_costs, seed=0)


@pytest.fixture
def fake_body() -> FakeBody:
    return FakeBody()


# -- events -------------------------------------------------------------------------------


@pytest.fixture
def recorder() -> EventRecorder:
    return EventRecorder()


@pytest.fixture
def life_builder() -> Callable[..., LifeBuilder]:
    return LifeBuilder


RecordedLife = Callable[..., Path]


@pytest.fixture(scope="session")
def recorded_life(tmp_path_factory: pytest.TempPathFactory) -> RecordedLife:
    """Record simulated lives with `epitaph sim --events`; returns the state dir holding
    lives/<n>/events.jsonl. The files are shared by the session: copy before editing."""
    cache: dict[tuple[str, str, int, int], Path] = {}

    def record(
        profile: str = "pi4/skeleton-1200", hardware: str = "pi4-4gb", lives: int = 1, seed: int = 0
    ) -> Path:
        key = (profile, hardware, lives, seed)
        if key in cache:
            return cache[key]
        out = io.StringIO()
        argv = ["sim", "--profile", profile, "--hardware", hardware]
        argv += ["--lives", str(lives), "--seed", str(seed), "--events"]
        with contextlib.redirect_stdout(out):
            assert cli_main(argv) == 0
        state = tmp_path_factory.mktemp("lives-" + profile.replace("/", "-"))
        all_path = write_events(state / "all.jsonl", [])
        all_path.write_text(out.getvalue())
        events = read_events(all_path)
        for n in sorted({int(e["life"]) for e in events}):
            write_events(
                state / "lives" / f"{n:06d}" / "events.jsonl",
                [e for e in events if e["life"] == n],
            )
        cache[key] = state
        return state

    return record
