"""Display test fixtures: simulator output as the `epitaph sim --events` JSON lines.

Tests that read pixels back with OCR carry the `tesseract` marker. They skip when the
tesseract binary or `pytesseract` is missing, except in CI (`CI` set, as on GitHub
Actions), which installs tesseract: there a missing binary fails the run instead of
quietly skipping test D13.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from epitaph.config import load_config
from epitaph.sim import simulate

DATA = Path(__file__).parent / "data"
# `epitaph sim --profile pi4/skeleton-1200 --hardware pi4-4gb --events --seed 0`, recorded
RECORDED_LIFE = DATA / "skeleton-1200.jsonl"
# `epitaph sim --profile pi4/default --hardware pi4-4gb --events --seed 0 --quiet`, recorded:
# the 30-minute installation life (two reloads that forget, erosion, the clock falling, an
# OOM death), standing in for a real Pi life so tests depend on nothing outside the repo
DEFAULT_LIFE = DATA / "default-1800.jsonl"


def have_tesseract() -> bool:
    """Whether OCR can run here: the tesseract binary and the pytesseract package."""
    return shutil.which("tesseract") is not None and (
        importlib.util.find_spec("pytesseract") is not None
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "tesseract: reads rendered pixels back with tesseract OCR")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if have_tesseract() or os.environ.get("CI"):
        return  # in CI a missing tesseract fails the OCR tests loudly
    skip = pytest.mark.skip(reason="tesseract (or pytesseract) not installed")
    for item in items:
        if "tesseract" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def sim_events() -> list[dict[str, Any]]:
    """One full pi4/default life, round-tripped through JSON like `epitaph sim --events`."""
    result = simulate(load_config("pi4/default", "pi4-4gb"), lives=1)
    return [json.loads(json.dumps(e)) for e in result.events]


@pytest.fixture(scope="session")
def sim_events_two_lives() -> list[dict[str, Any]]:
    result = simulate(load_config("pi4/smoke-300", "pi4-4gb"), lives=2)
    return [json.loads(json.dumps(e)) for e in result.events]


@pytest.fixture(scope="session")
def recorded_life() -> list[dict[str, Any]]:
    """The recorded fake life D13 draws (a file, so a simulator change cannot move it)."""
    from epitaph.display.replay import load_events

    return load_events(RECORDED_LIFE)


@pytest.fixture(scope="session")
def default_life() -> list[dict[str, Any]]:
    """The recorded 30-minute life with reloads and an OOM death (see DEFAULT_LIFE)."""
    from epitaph.display.replay import load_events

    return load_events(DEFAULT_LIFE)
