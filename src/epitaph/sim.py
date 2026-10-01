"""`epitaph sim`: whole lives of the real controller on the fakes, in virtual time.

The controller (`controller.Controller`) runs exactly as on the Pi: the same loop, death
causes, death flush, silence and rebirth. Only the world around it is fake:

- the clock is a `VirtualClock`: time jumps to the next timer, so a 30-minute life takes
  well under a second and concurrent typing and generation overlap as in real time;
- the creature is a `FakeBackend` (Pi 4 speeds from `bench/` and the overlay);
- the body is `SimBody`: a `FakeBody` that kills the fake creature at the death squeeze
  (OOM) and when the controller kills it (deadline, hang, manual), and whose progress
  counters stop while the creature hangs;
- the slot (ADR-014) is kept by `FakeSlots`, so the echo never costs the carried memory;
- the world around it (ADR-031) is a `FakeWorld` over `[world] services`, whatever the
  hardware overlay's helper says.
- the persona cache (`[backend] persona_cache`) is one store shared by the lives' fakes, as
  the disk is: the first birth reads the system prompt, the next ones restore it.

Event conventions (contract decisions E2, E3, D2, D5, D6, E4): every event carries `t`, the
life clock in seconds (0 before birth); `birth_loading` carries the resolved `profile`,
`hardware` and `lifespan_s`; every memory cut emits `forget`, the reload's included;
`gen_end` carries `prompt_n` and `tok_s`. `ts` is virtual wall time, consistent with `t`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from epitaph.backend.base import Backend
from epitaph.backend.fake import SIGKILL, FakeBackend, PersonaStore
from epitaph.body.fake import FakeBody
from epitaph.body.world import DEFAULT_PROCESSES, FakeWorld, World
from epitaph.clock import VirtualClock, run_virtual
from epitaph.config import Config
from epitaph.controller import Controller, LifeRecord, SlotStore, run_inline
from epitaph.costmodel import Costs, load_costs
from epitaph.events import Event
from epitaph.types import Cause, Knobs, ModelSpec, ProgressCounters


@dataclass
class SimResult:
    """What a simulation produced: every event in order, and each life's cause and thoughts."""

    events: list[Event] = field(default_factory=lambda: [])
    causes: list[str] = field(default_factory=lambda: [])
    thoughts: list[int] = field(default_factory=lambda: [])
    records: list[LifeRecord] = field(default_factory=lambda: [])


class SimBody(FakeBody):
    """A fake body that really ends the fake creature: at the squeeze and on a kill."""

    def __init__(self, death_mode: str = "oom") -> None:
        """`death_mode` "oom" lets the squeeze kill; "deadline" leaves it to the deadline."""
        super().__init__()
        self.death_mode = death_mode
        self.creature: FakeBackend | None = None
        self._frozen: ProgressCounters | None = None

    def apply(self, knobs: Knobs) -> None:
        """Take the knobs; the death squeeze OOM-kills the creature (death_mode oom)."""
        if self.death_mode != "oom":
            knobs = replace(knobs, death_squeeze=False)
        super().apply(knobs)
        if knobs.death_squeeze and self.creature is not None:
            self.creature.oom()

    def kill_now(self, cause: Cause) -> None:
        """Record the cause and SIGKILL the creature, as cgroup.kill does."""
        super().kill_now(cause)
        if self.creature is not None:
            self.creature.kill(SIGKILL)

    def reset_creature_cgroup(self) -> None:
        """Lift the squeeze and forget the last kill cause."""
        super().reset_creature_cgroup()
        self.killed.clear()

    def progress(self) -> ProgressCounters:
        """Counters that advance, except while the creature hangs (SIGSTOP: nothing moves)."""
        if self.creature is not None and self.creature.hung:
            if self._frozen is None:
                self._frozen = super().progress()
            return self._frozen
        self._frozen = None
        return super().progress()


class FakeSlots:
    """Saves and restores the fake creature's prompt cache, like llama-server's slot files."""

    def __init__(self, backend: FakeBackend) -> None:
        """Keep slots of `backend`."""
        self.backend = backend
        self.saved: dict[str, object] = {}

    async def save(self, name: str) -> bool:
        """Copy the cache; False when the creature is not running."""
        if not self.backend.alive:
            return False
        self.saved[name] = list(self.backend._cache.blocks)  # pyright: ignore[reportPrivateUsage]
        return True

    async def restore(self, name: str) -> None:
        """Put the saved cache back."""
        blocks = self.saved.pop(name, None)
        if isinstance(blocks, list) and self.backend.alive:
            self.backend._cache.blocks = blocks  # pyright: ignore[reportPrivateUsage]


def fake_world(cfg: Config) -> FakeWorld | None:
    """The simulated world `[world]` describes; None when it is disabled."""
    w = cfg.section("world")
    if not bool(w.get("enabled", False)):
        return None
    services = [str(s) for s in w.get("services", [])]
    return FakeWorld(services, int(w.get("fake_processes", DEFAULT_PROCESSES)))


def make_controller(
    cfg: Config,
    clock: VirtualClock,
    *,
    lives: int | None = 1,
    seed: int = 0,
    publish: list[Event] | None = None,
    state_dir: Path | None = None,
    costs: Costs | None = None,
    body: SimBody | None = None,
    backend_cls: type[FakeBackend] | None = None,
    world: World | None = None,
) -> Controller:
    """The real controller wired to the fakes on `clock` (also used by `epitaph run --backend
    fake`). A new fake creature is made for each life, seeded with `seed + n`. The world is
    `world`, or the `FakeWorld` of `[world]`."""
    costs = costs or load_costs(cfg)
    sim_body = body or SimBody(str(cfg.get("body.death_mode", "oom")))
    cls = backend_cls or FakeBackend
    wall0 = time.time() - clock.now()
    persona: PersonaStore | None = {} if bool(cfg.get("backend.persona_cache", False)) else None

    def backend_for(n: int, model: ModelSpec) -> Backend:
        b = cls(
            clock,
            costs,
            seed=seed + n,
            ctx=cfg.ctx,
            cache_reuse_min=int(cfg.get("backend.cache_reuse", 32)) or 32,
            reload_handover=str(cfg.get("backend.reload_handover", "reread")),
            persona_store=persona,
        )
        sim_body.creature = b
        return b

    def slots_for(b: Backend) -> SlotStore | None:
        return FakeSlots(b) if isinstance(b, FakeBackend) else None

    events = publish
    return Controller(
        cfg,
        clock=clock,
        backend_for=backend_for,
        body=sim_body,
        costs_for=lambda _model: costs,
        publish=events.append if events is not None else None,
        state_dir=state_dir,
        lives=lives,
        seed=seed,
        slots_for=slots_for,
        ts=lambda: wall0 + clock.now(),
        offload=run_inline,
        world=world if world is not None else fake_world(cfg),
    )


def simulate(
    cfg: Config, lives: int = 1, seed: int = 0, state_dir: Path | None = None
) -> SimResult:
    """Run `lives` lives back to back on one virtual clock."""
    result = SimResult()

    async def main(clock: VirtualClock) -> list[LifeRecord]:
        ctl = make_controller(
            cfg,
            clock,
            lives=lives,
            seed=seed,
            publish=result.events,
            state_dir=state_dir,
            costs=load_costs(cfg),
            backend_cls=FakeBackend,
        )
        return await ctl.run()

    result.records = run_virtual(main)
    result.causes = [r.cause for r in result.records]
    result.thoughts = [r.thoughts for r in result.records]
    return result
