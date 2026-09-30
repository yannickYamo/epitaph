"""Display test fixtures: simulator output as the `epitaph sim --events` JSON lines."""

from __future__ import annotations

import json
from typing import Any

import pytest

from epitaph.config import load_config
from epitaph.sim import simulate


@pytest.fixture(scope="session")
def sim_events() -> list[dict[str, Any]]:
    """One full pi4/default life, round-tripped through JSON like `epitaph sim --events`."""
    result = simulate(load_config("pi4/default", "pi4-4gb"), lives=1)
    return [json.loads(json.dumps(e)) for e in result.events]


@pytest.fixture(scope="session")
def sim_events_two_lives() -> list[dict[str, Any]]:
    result = simulate(load_config("pi4/smoke-300", "pi4-4gb"), lives=2)
    return [json.loads(json.dumps(e)) for e in result.events]
