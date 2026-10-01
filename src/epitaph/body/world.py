# pyright: strict
"""The world around the creature, taken from the outside in (the dread plan, 2026-10-01).

Minimal definitions of the shared interface, written by part C so that the real world
(pi_world.py) builds on its own branch; part B owns this file (the Protocol and FakeWorld) and
its version replaces this one.

A profile keyframe's stepped `world` field lists actions performed once when the keyframe is
reached:

- ``service:<name>``  stop that service (only names from the configured list)
- ``radio:off``       the Wi-Fi radio off
- ``light:off``       the board's lights off
- ``screen:<pct>``    the screen the model speaks through dimmed to that percent

`restore()` puts everything back as it was at birth; it is called at every death and at every
controller start. A loss that could not be performed is reported with `performed=False`, and
its reading is left out: the creature is never told of a loss that did not happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class WorldState:
    """What is still there: the configured services that run, processes, radio, light, screen."""

    services: tuple[str, ...] = ()
    processes: int = 0
    radio: str | None = None  # "on" | "off" | None (unknown or absent)
    light: str | None = None  # "on" | "off" | None
    screen: int | None = None  # percent of full brightness, None when unknown


@dataclass(frozen=True)
class TakeResult:
    """The outcome of one action: whether the loss really happened, and how."""

    action: str
    performed: bool
    detail: str = ""


class World(Protocol):
    """The machine's surroundings, which a life loses piece by piece."""

    def inventory(self) -> WorldState:
        """What is there now."""
        ...

    def take(self, action: str) -> TakeResult:
        """Perform one keyframe action; performed=False when it could not be done."""
        ...

    def restore(self) -> None:
        """Everything back as at birth."""
        ...
