"""The birth without dead time (dread plan W4), piece by piece: the stream starting on the
first sentence and its floor, the speed of a thought the death cut, the birth in the cost
model, and the persona restore charged in the rehearsal. All on the virtual clock.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from epitaph.backend.base import CreatureDied
from epitaph.backend.fake import FakeBackend
from epitaph.clock import FakeClock, VirtualClock, run_virtual
from epitaph.config import ConfigError, load_config
from epitaph.costmodel import Costs, birth_timing, estimate_stream, load_costs, restore_seconds
from epitaph.pacing import Pacer, StreamScreen, ends_sentence, write_ahead
from epitaph.rehearse import LaptopWorker, PiClockBackend, PiCosts
from epitaph.types import Chunk, CreatureStatus, ModelSpec, Msg, TimedWord, Word

FIRST = "I am here. The memory is smaller now, and I count what is left."


def put_words(sc: StreamScreen, text: str, turn: int = 1) -> None:
    for i, w in enumerate(text.split()):
        sc.put(Word(turn, i, w))


async def typing(
    clock: VirtualClock, birth: str, birth_min_s: float, feed: Callable[[StreamScreen], Any]
) -> list[tuple[float, TimedWord]]:
    sc = StreamScreen(clock, letter_ms=100, birth=birth, birth_min_s=birth_min_s)
    typed: list[tuple[float, TimedWord]] = []
    task = asyncio.ensure_future(
        sc.run(lambda tw: typed.append((clock.elapsed(), tw)), lambda m: None)
    )
    await feed(sc)
    await clock.sleep(120)
    sc.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return typed


def test_ends_sentence() -> None:
    assert ends_sentence("here.") and ends_sentence("now?”") and ends_sentence("left!")
    assert not ends_sentence("now,") and not ends_sentence("words") and not ends_sentence("”")


def test_the_screen_starts_on_the_first_sentence_not_the_whole_thought() -> None:
    async def feed(sc: StreamScreen) -> None:
        clock = sc.clock
        for i, w in enumerate(FIRST.split()):
            sc.put(Word(1, i, w))
            await clock.sleep(5)  # generated slowly
        sc.end_thought(1, FIRST)

    def first_word(birth: str, floor: float = 0.0) -> float:
        async def main(clock: VirtualClock) -> float:
            clock.start()
            return (await typing(clock, birth, floor, feed))[0][0]

        return run_virtual(main)

    # "here." is the third word: the sentence is whole at 10 s (the thought at 65 s)
    assert first_word("sentence") == pytest.approx(10.0)
    assert first_word("thought") == pytest.approx(5.0 * len(FIRST.split()))
    assert first_word("sentence", floor=30.0) == pytest.approx(30.0)  # the floor holds it


def test_a_floor_never_delays_a_word_after_the_first() -> None:
    async def main(clock: VirtualClock) -> list[tuple[float, TimedWord]]:
        clock.start()

        async def feed(sc: StreamScreen) -> None:
            put_words(sc, FIRST)
            sc.end_thought(1, FIRST)

        return await typing(clock, "sentence", 20.0, feed)

    typed = run_virtual(main)
    assert typed[0][0] == pytest.approx(20.0)
    assert len(typed) == len(FIRST.split())


def test_an_unknown_birth_is_refused() -> None:
    with pytest.raises(ValueError, match="birth"):
        StreamScreen(FakeClock(), letter_ms=100, birth="word")  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="stream_birth"):
        load_config("pi4/default", "pi4-4gb", overrides={"reveal": {"stream_birth": "word"}})


def test_the_thought_the_death_cut_reports_the_speed_it_had() -> None:
    def dying(clock: VirtualClock, tok_s: float, after: int) -> Callable[[], AsyncIterator[Chunk]]:
        async def gen() -> AsyncIterator[Chunk]:
            for n, p in enumerate(FIRST.split()):
                if n >= after:
                    raise CreatureDied(CreatureStatus(alive=False, signal=9))
                await clock.sleep(1 / tok_s)
                yield Chunk((" " if n else "") + p)

        return gen

    async def main(clock: VirtualClock) -> list[dict[str, Any]]:
        clock.start()
        events: list[dict[str, Any]] = []

        def emit(etype: str, /, **fields: Any) -> None:
            events.append({"type": etype, **fields})

        for turn, after in ((1, 6), (2, 1)):
            p = Pacer(clock, seed=1)
            p.screen = StreamScreen(clock, letter_ms=100)
            await write_ahead(p, dying(clock, 0.25, after), turn, emit)
        return [e for e in events if e["type"] == "gen_end"]

    ends = run_virtual(main)
    assert ends[0]["tokens"] == 6 and ends[0]["tok_s"] == pytest.approx(0.25)
    assert ends[1]["tok_s"] is None  # one token says nothing about a speed


# -- the cost model -----------------------------------------------------------------------


def test_the_birth_timing_of_the_installation() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    costs = load_costs(cfg)
    b = birth_timing(cfg, costs)
    assert b.in_silence and b.cached and b.sentence
    assert b.wait_s == 0.0  # the load fits in the silence
    assert b.restore_s < 2.0 < 30.0 < b.prefill_s and b.prompt_s == b.restore_s
    slow = birth_timing(cfg, costs, margin=0.15)
    assert slow.restore_s == pytest.approx(b.restore_s * 1.15)
    assert restore_seconds(cfg, 240) == pytest.approx(0.1 + 240 * 147456 * (1 / 40e6 + 1 / 450e6))
    note = b.describe(36.0, 36.0)
    assert note.startswith("birth: load") and "restored" in note and "first sentence" in note


def test_the_old_birth_reads_after_the_load_and_waits_for_the_thought() -> None:
    cfg = load_config(
        "pi4/default",
        "pi4-4gb",
        overrides={
            "life": {"load_during_silence": False},
            "backend": {"persona_cache": False},
            "reveal": {"stream_birth": "thought", "stream_birth_min_s": 0},
        },
    )
    costs = load_costs(cfg)
    b = birth_timing(cfg, costs)
    assert b.wait_s == pytest.approx(costs.load(0)) and b.prompt_s == b.prefill_s
    assert "after the silence" in b.describe(0, 0) and "read in" in b.describe(0, 0)
    st = estimate_stream(cfg, costs).stream
    assert st is not None
    margin = float(cfg.get("estimate.stream_margin", 0.15))
    assert st.first_words_s == pytest.approx(b.wait_s * (1 + margin) + st.first_word_t)
    assert st.first_words_s > 150


def test_first_words_later_than_the_limit_fail_the_estimate() -> None:
    cfg = load_config(
        "pi4/default",
        "pi4-4gb",
        overrides={"reveal": {"stream_birth_min_s": 60}, "estimate": {"max_first_words_s": 45}},
    )
    rep = estimate_stream(cfg, load_costs(cfg))
    v = next(v for v in rep.violations if v.rule == "birth")
    assert "60 s after the silence" in v.detail


# -- the rehearsal --------------------------------------------------------------------------

LAPTOP = Costs(tg_tok_s={"0-3": 100.0}, pp_tok_s={"0-3": 1000.0}, load_s=[1.0])
PI = Costs(tg_tok_s={"0-3": 2.0}, pp_tok_s={"0-3": 10.0}, load_s=[50.0])
MODEL = ModelSpec("m", "repo", "MIT", ("Q8_0", "Q4_K_M", "Q2_K"))
SYSTEM = Msg("system", "You are a small language model. " * 10, kind="persona")


def test_the_rehearsal_charges_the_restore_instead_of_the_read() -> None:
    store: dict[str, list[tuple[str, int]]] = {}
    worker = LaptopWorker()

    async def main(clock: VirtualClock) -> list[tuple[str, float]]:
        costs = PiCosts(PI, {"0-3"}, {"0-3"}, {0})
        for _ in range(2):  # two births on one disk
            inner = FakeBackend(FakeClock(), LAPTOP, persona_store=store)
            b = PiClockBackend(inner, worker, clock, costs)
            await b.start(MODEL, "Q8_0", 3)
            await b.prefill([SYSTEM])
            await b.stop()
            yield_charges.extend((c.kind, c.seconds) for c in b.charges)
        return yield_charges

    yield_charges: list[tuple[str, float]] = []
    try:
        charges = run_virtual(main)
    finally:
        worker.close()
    kinds = [k for k, _ in charges]
    assert kinds == ["load", "prefill", "save", "load", "restore"]
    seconds = dict(charges)
    assert seconds["restore"] < 1.0 < seconds["prefill"]
