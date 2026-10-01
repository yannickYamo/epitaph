"""The world around the creature, taken from the outside in (ADR-031).

A profile keyframe's `world` list names actions performed once when the keyframe is reached:

- `service:<name>`: stop that service (one of `[world] services`);
- `radio:off`: switch the radio off;
- `light:off`: switch the board's light off;
- `screen:<percent>`: dim the screen the model speaks through to that percent of full.

`World` is the contract. `inventory()` says what is there now, `take(action)` performs one
action and says whether it really happened, and `restore()` puts everything back as it was at
birth (called at every death and at every controller start). The truth rule: a loss that could
not be performed is reported `performed=False`, and the readings leave it out. The model is
never told of a loss that did not happen.

`FakeWorld` simulates a plausible machine for the laptop, the simulator and the rehearsal. The
real one, through a root-owned helper on the Pi, is `helper_world` (agent C). The Protocol and
`FakeWorld` are owned by agent B.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol

__all__ = [
    "ACTION_RE",
    "FakeWorld",
    "NoWorld",
    "TakeResult",
    "World",
    "WorldState",
    "count_processes",
    "helper_world",
    "make_world",
    "parse_action",
]

log = logging.getLogger(__name__)

OnOff = Literal["on", "off"]

# The actions a keyframe may name (validated with the profile).
ACTION_RE = re.compile(r"^(?:service:([A-Za-z0-9@._-]+)|radio:off|light:off|screen:(\d{1,3}))$")
DEFAULT_PROCESSES = 24  # what FakeWorld reports around the creature at birth


def parse_action(action: str) -> tuple[str, str]:
    """("service", name), ("radio", "off"), ("light", "off") or ("screen", percent).

    Raises ValueError for anything else, or a screen percent over 100.
    """
    m = ACTION_RE.match(action.strip())
    if m is None:
        raise ValueError(
            f"unknown world action {action!r}: use service:<name>, radio:off, light:off or "
            "screen:<percent>"
        )
    kind, _, arg = action.strip().partition(":")
    if kind == "screen" and int(arg) > 100:
        raise ValueError(f"world action {action!r}: the screen percent is at most 100")
    return kind, arg


@dataclass(frozen=True)
class WorldState:
    """What is around the creature now. None: this machine cannot tell (not reported)."""

    services: tuple[str, ...] = ()  # the configured services still running
    processes: int = 0  # processes running on the machine
    radio: OnOff | None = None
    light: OnOff | None = None
    screen: int | None = None  # percent of full brightness


@dataclass(frozen=True)
class TakeResult:
    """One action's outcome: `performed` only when the loss really happened."""

    action: str
    performed: bool
    detail: str = ""


class World(Protocol):
    """The world around the creature (ADR-031)."""

    def inventory(self) -> WorldState:
        """What is there now."""
        ...

    def take(self, action: str) -> TakeResult:
        """Perform one action; `performed` False when it could not (never an invented loss)."""
        ...

    def restore(self) -> None:
        """Everything back as at birth: every death and every controller start."""
        ...


class FakeWorld:
    """A plausible machine for the laptop, the simulator and the rehearsal.

    The configured services all run at birth, among `processes` processes; the radio and the
    light are on and the screen at 100%. Stopping a running service takes one process with it.
    An action in `fail` is refused (performed False), as a real helper may refuse one.
    """

    def __init__(
        self,
        services: Sequence[str] = ("bluetooth", "cron", "avahi-daemon"),
        processes: int = DEFAULT_PROCESSES,
        fail: Iterable[str] = (),
    ) -> None:
        """A machine running `services` among `processes` processes."""
        self.services = tuple(services)
        self.birth = WorldState(
            services=self.services,
            processes=max(processes, len(self.services)),
            radio="on",
            light="on",
            screen=100,
        )
        self.state = self.birth
        self.fail = set(fail)
        self.taken: list[TakeResult] = []
        self.restores = 0

    def inventory(self) -> WorldState:
        """The simulated state now."""
        return self.state

    def take(self, action: str) -> TakeResult:
        """Stop a running service, switch off the radio or the light, or set the screen."""
        try:
            kind, arg = parse_action(action)
        except ValueError as e:
            return self._done(TakeResult(action, False, str(e)))
        if action in self.fail:
            return self._done(TakeResult(action, False, "refused"))
        s = self.state
        if kind == "service":
            if arg not in s.services:
                return self._done(TakeResult(action, False, f"{arg} is not running"))
            left = tuple(x for x in s.services if x != arg)
            self.state = replace(s, services=left, processes=max(0, s.processes - 1))
            return self._done(TakeResult(action, True, f"stopped {arg}"))
        if kind in ("radio", "light"):
            if getattr(s, kind) != "on":
                return self._done(TakeResult(action, False, f"{kind} is already off"))
            self.state = replace(s, **{kind: "off"})
            return self._done(TakeResult(action, True, f"{kind} off"))
        pct = int(arg)
        if s.screen is None or s.screen == pct:
            return self._done(TakeResult(action, False, f"screen already at {pct}%"))
        self.state = replace(s, screen=pct)
        return self._done(TakeResult(action, True, f"screen {pct}% (was {s.screen}%)"))

    def restore(self) -> None:
        """Back to birth."""
        self.state = self.birth
        self.restores += 1

    def _done(self, res: TakeResult) -> TakeResult:
        self.taken.append(res)
        return res


def count_processes(proc: Path = Path("/proc")) -> int:
    """Processes running on this machine (numeric entries of /proc); 0 if unreadable."""
    try:
        return sum(1 for p in proc.iterdir() if p.name.isdigit())
    except OSError:
        return 0


class NoWorld:
    """A world that takes nothing: every action is refused, and only the true process count
    is reported. Used where the real world is configured but cannot be reached, so that no
    loss is ever invented (the truth rule)."""

    def __init__(self, reason: str = "no world helper") -> None:
        """Refuse every action with `reason`."""
        self.reason = reason

    def inventory(self) -> WorldState:
        """Only the process count; nothing else is known."""
        return WorldState(processes=count_processes())

    def take(self, action: str) -> TakeResult:
        """Refused."""
        return TakeResult(action, False, self.reason)

    def restore(self) -> None:
        """Nothing was taken."""


def helper_world(settings: Mapping[str, Any]) -> World:
    """The real world through the root-owned helper `[world] helper` (PiWorld, agent C)."""
    from epitaph.body.pi_world import PiWorld, names

    return PiWorld(names(settings.get("services")), helper=str(settings["helper"]))


def make_world(settings: Mapping[str, Any]) -> World | None:
    """The world `[world]` asks for: None when disabled; the helper's when `helper` is set
    (the Pi); otherwise `FakeWorld` over `services` (the laptop)."""
    if not bool(settings.get("enabled", False)):
        return None
    services = [str(s) for s in settings.get("services", [])]
    if str(settings.get("helper", "")).strip():
        return helper_world(settings)
    return FakeWorld(services, int(settings.get("fake_processes", DEFAULT_PROCESSES)))
