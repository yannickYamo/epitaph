"""The body contract: cgroups, limits, vitals, death.

Implemented in cgroup.py, vitals.py and fake.py.
"""

from __future__ import annotations

from typing import Protocol

from epitaph.types import Cause, CreatureStatus, Knobs, MachineFacts, ProgressCounters, Vitals


class Body(Protocol):
    """The machine around the creature."""

    def reset_creature_cgroup(self) -> None:
        """Kill any leftover creature (cgroup.kill) and clear its limits."""
        ...

    def wrap_spawn(self, argv: list[str]) -> list[str]:
        """Return argv that places the child in the creature cgroup, pinned to its cores."""
        ...

    def apply(self, knobs: Knobs) -> None:
        """Apply CPU share and, when knobs.death_squeeze, the death memory limit."""
        ...

    def progress(self) -> ProgressCounters:
        """The creature's monotonic progress counters, for hang detection."""
        ...

    def kill_now(self, cause: Cause) -> None:
        """Kill the creature at once and remember `cause` for death_cause()."""
        ...

    def death_cause(self, status: CreatureStatus) -> Cause:
        """Why the creature died, from its exit status and memory.events."""
        ...

    def vitals(self) -> Vitals:
        """A snapshot of temperature, throttling, memory and effective cores."""
        ...

    def facts(self) -> MachineFacts:
        """True facts about the machine, for the persona facts line."""
        ...
