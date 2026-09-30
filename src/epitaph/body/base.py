"""The body contract: cgroups, limits, vitals, death (BUILD_PLAN 6.4).

Owned by the integrator; implemented by agent C (cgroup.py, vitals.py, fake.py).
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

    def progress(self) -> ProgressCounters: ...

    def kill_now(self, cause: Cause) -> None: ...

    def death_cause(self, status: CreatureStatus) -> Cause:
        """Why the creature died, from its exit status and memory.events."""
        ...

    def vitals(self) -> Vitals: ...

    def facts(self) -> MachineFacts: ...
