"""No dead time between lives (dread plan W4): the next creature loads in the silence, the
persona is restored at birth, and the stream starts on the first sentence.

The real controller on the fakes, in virtual time (as test_controller.py).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from epitaph.backend.fake import FakeBackend
from epitaph.clock import VirtualClock, run_virtual
from epitaph.config import Config, load_config
from epitaph.controller import Controller
from epitaph.events import Event
from epitaph.sim import make_controller
from epitaph.types import ModelSpec

SMOKE = "pi4/smoke-300"  # 5 minutes, deadline death
BACKGROUND: list[asyncio.Future[None]] = []  # tasks a test schedules beside the controller


def cfg_of(profile: str = SMOKE, **over: Any) -> Config:
    return load_config(profile, "pi4-4gb", overrides=over or None)


def run(
    cfg: Config,
    lives: int = 2,
    backend_cls: type[FakeBackend] = FakeBackend,
    setup: Any = None,
) -> tuple[Controller, list[Event], list[FakeBackend]]:
    events: list[Event] = []
    made: list[FakeBackend] = []

    class Counted(backend_cls):  # type: ignore[valid-type,misc]
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            made.append(self)

    async def main(clock: VirtualClock) -> Controller:
        ctl = make_controller(cfg, clock, lives=lives, publish=events, backend_cls=Counted)
        if setup is not None:
            await setup(ctl, clock)
        await ctl.run()
        return ctl

    return run_virtual(main), events, made


def of(events: list[Event], etype: str, life: int) -> list[Event]:
    return [e for e in events if e["type"] == etype and e["life"] == life]


def one(events: list[Event], etype: str, life: int) -> Event:
    (e,) = of(events, etype, life)
    return e


def test_the_next_creature_loads_in_the_silence_and_is_born_when_it_ends() -> None:
    cfg = cfg_of()
    silence = float(cfg.get("life.silence_seconds"))
    ctl, ev, made = run(cfg)
    assert [r.cause for r in ctl.records] == ["deadline", "deadline"]
    assert len(made) == 2 and [b.starts for b in made] == [1, 1]  # one load each, no reload
    first, second = one(ev, "birth_loading", 1), one(ev, "birth_loading", 2)
    assert "preloaded" not in first  # the first life after a start loads at its birth
    load = made[1].costs.load(int(second["step"]))
    assert second["preloaded"] is True and second["load_s"] == pytest.approx(load, abs=0.1)
    # the birth comes when the silence ends: the load was hidden in it
    born = one(ev, "birth", 2)["ts"] - one(ev, "silence", 1)["ts"]
    assert born == pytest.approx(silence, abs=0.5)
    # the silence is the life's last event; the birth_loading of the next comes at its end
    assert one(ev, "birth_loading", 2)["ts"] >= one(ev, "silence", 1)["ts"] + silence - 0.5
    # the life clock still starts at the birth
    assert one(ev, "birth", 2)["t"] == 0.0


def test_a_load_longer_than_the_silence_delays_the_birth_by_the_rest() -> None:
    cfg = cfg_of(life={"silence_seconds": 20})
    _, ev, _ = run(cfg)
    second = one(ev, "birth_loading", 2)
    born = one(ev, "birth", 2)["ts"] - one(ev, "silence", 1)["ts"]
    assert born == pytest.approx(second["load_s"], abs=0.5) and born > 20


def test_off_the_birth_loads_after_the_silence() -> None:
    cfg = cfg_of(life={"load_during_silence": False})
    silence = float(cfg.get("life.silence_seconds"))
    _, ev, made = run(cfg)
    assert "preloaded" not in one(ev, "birth_loading", 2)
    born = one(ev, "birth", 2)["ts"] - one(ev, "silence", 1)["ts"]
    load = made[1].costs.load(int(one(ev, "birth_loading", 2)["step"]))
    assert born == pytest.approx(silence + load, abs=0.5)


def test_a_load_that_fails_in_the_silence_is_done_again_at_birth() -> None:
    class CrashOnce(FakeBackend):
        crashed = False

        async def start(self, model: ModelSpec, quant: str, threads: int) -> None:
            if len(made) == 2 and not CrashOnce.crashed:  # the load in the silence
                CrashOnce.crashed = True
                self.faults.crash_on_start = True
            await super().start(model, quant, threads)

    made: list[FakeBackend] = []

    class Tracked(CrashOnce):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            made.append(self)

    async def main(clock: VirtualClock) -> list[Event]:
        events: list[Event] = []
        ctl = make_controller(cfg_of(), clock, lives=2, publish=events, backend_cls=Tracked)
        await ctl.run()
        assert [r.cause for r in ctl.records] == ["deadline", "deadline"]
        return events

    ev = run_virtual(main)
    assert len(made) == 3  # life 1, the failed load in the silence, the birth's own
    assert "preloaded" not in one(ev, "birth_loading", 2)
    assert one(ev, "death", 2)["lived_s"] == pytest.approx(300.0)


def test_a_load_that_hangs_in_the_silence_is_given_up_and_done_at_birth() -> None:
    made: list[FakeBackend] = []

    class HangSecond(FakeBackend):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            made.append(self)
            if len(made) == 2:
                self.faults.hang_on_start = True

    cfg = cfg_of()
    _, ev, _ = run(cfg, backend_cls=HangSecond)
    assert "preloaded" not in one(ev, "birth_loading", 2)
    assert one(ev, "death", 2)["cause"] == "deadline"
    # the birth waited the load limit from the load's start, then loaded again
    waited = one(ev, "birth_loading", 2)["ts"] - one(ev, "silence", 1)["ts"]
    assert waited == pytest.approx(float(cfg.get("life.load_timeout_s")), abs=1)


def test_a_new_life_asked_in_the_silence_drops_a_creature_that_is_not_its_own() -> None:
    other = "llama-3.2-3b-instruct"

    async def setup(ctl: Controller, clock: VirtualClock) -> None:
        async def later() -> None:
            await asyncio.sleep(300 + 64.2 + 30)  # in the silence after life 1
            assert ctl.state == "silence"
            await ctl.ctl_new_life({"model": other})

        BACKGROUND.append(asyncio.ensure_future(later()))

    ctl, ev, made = run(cfg_of(), setup=setup)
    assert [r.model for r in ctl.records][1] == other
    loading = one(ev, "birth_loading", 2)
    assert loading["model"] == other and "preloaded" not in loading
    assert len(made) == 3 and not made[1].alive  # the one loaded in the silence was stopped


def test_the_persona_is_restored_from_the_second_birth() -> None:
    cfg = cfg_of()
    _, _, made = run(cfg, lives=3)
    modes = [b.last_prefill.mode for b in made]
    assert modes == ["prefill", "restore", "restore"]
    assert made[0].last_prefill.saved


def test_the_installation_starts_typing_on_the_first_sentence_at_45_s() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    _, ev, _ = run(cfg, lives=2)
    # life 1 reads its persona (nothing cached yet); life 2 restores it, and its screen
    # starts on the first sentence, before the first thought is generated, at the floor
    words = of(ev, "word", 2)
    first_end = min(e["t"] for e in of(ev, "gen_end", 2))
    assert words[0]["t"] == pytest.approx(45.0, abs=0.05) and words[0]["t"] < first_end
    assert of(ev, "word", 1)[0]["t"] > 45.0
    for life in (1, 2):
        assert not of(ev, "starved", life)
    # the silence is the only dark time: life 2's first word 45 s after its end
    vigil_end = one(ev, "silence", 1)["ts"] + float(cfg.get("life.silence_seconds"))
    assert words[0]["ts"] - vigil_end == pytest.approx(45.0, abs=0.5)


def test_a_creature_that_dies_after_its_load_in_the_silence_is_loaded_again() -> None:
    made: list[FakeBackend] = []

    class DiesWaiting(FakeBackend):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            made.append(self)

        async def start(self, model: ModelSpec, quant: str, threads: int) -> None:
            await super().start(model, quant, threads)
            if len(made) == 2:  # loaded in the silence, then gone before the birth
                asyncio.get_running_loop().call_later(5.0, self.crash)

    ctl, ev, _ = run(cfg_of(), backend_cls=DiesWaiting)
    assert [r.cause for r in ctl.records] == ["deadline", "deadline"]
    assert len(made) == 3 and "preloaded" not in one(ev, "birth_loading", 2)
    assert not of(ev, "death", 1)[1:]  # the dead creature was not this life's death


def test_a_controller_stopped_in_the_silence_leaves_no_creature_running() -> None:
    made: list[FakeBackend] = []

    class Tracked(FakeBackend):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            made.append(self)

    async def main(clock: VirtualClock) -> Controller:
        ctl = make_controller(cfg_of(), clock, lives=None, backend_cls=Tracked)
        task = asyncio.ensure_future(ctl.run())
        while ctl.state != "silence":
            await asyncio.sleep(1)
        await asyncio.sleep(10)  # the next creature is loading
        assert len(made) == 2 and ctl._preload is not None  # pyright: ignore[reportPrivateUsage]
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return ctl

    ctl = run_virtual(main)
    assert ctl._preload is None  # pyright: ignore[reportPrivateUsage]
    assert not any(b.alive for b in made)
