"""A fake body for tests, the simulator and machines without cgroups (BUILD_PLAN 9 C)."""

from __future__ import annotations

import os

from epitaph.types import Cause, CreatureStatus, Knobs, MachineFacts, ProgressCounters, Vitals


class FakeBody:
    """Records what the controller asked for; reports plausible vitals."""

    def __init__(
        self, cores: int = 4, ram_gb: float = 4.0, model: str = "Raspberry Pi 4 Model B"
    ) -> None:
        self._facts = MachineFacts(model=model, cores=cores, ram_gb=ram_gb)
        self.applied: list[Knobs] = []
        self.killed: list[Cause] = []
        self.cpu_share = float(cores - 1)
        self.death_squeeze = False
        self._cpu_usec = 0

    def reset_creature_cgroup(self) -> None:
        self.death_squeeze = False

    def wrap_spawn(self, argv: list[str]) -> list[str]:
        return list(argv)

    def apply(self, knobs: Knobs) -> None:
        self.applied.append(knobs)
        self.cpu_share = knobs.cpu_share
        self.death_squeeze = knobs.death_squeeze

    def progress(self) -> ProgressCounters:
        self._cpu_usec += 1000
        return ProgressCounters(cpu_usec=self._cpu_usec)

    def kill_now(self, cause: Cause) -> None:
        self.killed.append(cause)

    def death_cause(self, status: CreatureStatus) -> Cause:
        if self.killed:
            return self.killed[-1]
        if self.death_squeeze and status.signal == 9:
            return Cause.OOM
        return Cause.CRASH

    def vitals(self) -> Vitals:
        temp = 48.0 + 6.0 * self.cpu_share
        return Vitals(cpu_c=round(temp, 1), cores_effective=self.cpu_share)

    def facts(self) -> MachineFacts:
        return self._facts


def local_facts() -> MachineFacts:
    """Facts about the machine this runs on (used on the laptop)."""
    return MachineFacts(model=os.uname().machine, cores=os.cpu_count() or 1, ram_gb=0.0)
