"""Shared fixtures (BUILD_PLAN 9 E1). part E extends this file."""

from __future__ import annotations

import pytest

from epitaph.config import Config, load_config


@pytest.fixture
def pi4_default() -> Config:
    return load_config("pi4/default", "pi4-4gb")


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d
