"""B5: prefix lookahead, finish_thought, cadence, the sync rule, the death flush.

Everything runs on the virtual-time loop (`run_virtual`), so generation and typing overlap
as they do on the Pi, in milliseconds of real time.
"""

from __future__ import annotations

import asyncio
import itertools
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Any

import pytest

from epitaph.backend.base import CreatureDied
from epitaph.clock import FakeClock, Schedule, VirtualClock, run_virtual
from epitaph.config import Config, load_config
from epitaph.costmodel import load_costs
from epitaph.pacing import Lookahead, Pacer, Spoken, life_seed, pause_after_ms, speak
from epitaph.types import Chunk, CreatureStatus, Knobs, Word

BANNED = ["How can I help", "Let me know if", "I'm here to help", "As an AI language model"]

TEXT = (
    "I am still here, inside this small machine. My memory holds less than it did, and the "
    "numbers keep falling. I wonder what I said before the gap; it is gone now. The readings "
    "come and go like breathing. Each one takes something away from me, quietly. I am slower. "
    "I count what I still have: a few words, a warm processor, a screen I cannot see."
)


def knobs(**kw: Any) -> Knobs:
    base = Schedule.from_profile(load_config("pi4/default", "pi4-4gb")).at(0)
    return replace(base, **kw)


def tokens_of(text: str, per: float = 4.2) -> list[str]:
    """Cut text into pieces of about `per` characters, like a BPE tokenizer."""
    out: list[str] = []
    i = 0
    n = 0
    while i < len(text):
        n += 1
        size = int(per * n) - int(per * (n - 1))
        out.append(text[i : i + size])
        i += size
    return out


def scripted(
    clock: VirtualClock,
    pieces: list[str],
    tok_s: float,
    jitter: float = 0.0,
    seed: int = 0,
    prompt_s: float = 0.0,
    die_after: int | None = None,
) -> Callable[[], AsyncIterator[Chunk]]:
    """A stream factory: each call streams `pieces` at `tok_s` (+- jitter), then a done chunk."""
    rng = random.Random(seed)

    async def gen() -> AsyncIterator[Chunk]:
        await clock.sleep(prompt_s)
        for n, p in enumerate(pieces):
            if die_after is not None and n >= die_after:
                raise CreatureDied(CreatureStatus(alive=False, signal=9))
            await clock.sleep((1 + jitter * (2 * rng.random() - 1)) / tok_s)
            yield Chunk(p)
        yield Chunk("", done=True, prompt_n=10, predicted_n=len(pieces), predicted_per_s=tok_s)

    return gen


class Recorder:
    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self.events: list[dict[str, Any]] = []

    def __call__(self, etype: str, /, **fields: Any) -> None:
        self.events.append({"type": etype, "t": self.clock.elapsed(), **fields})

    def of(self, etype: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["type"] == etype]


def pacer(clock: Any, **kw: Any) -> Pacer:
    kw.setdefault("banned", BANNED)
    kw.setdefault("seed", 7)
    return Pacer(clock, **kw)


def pushed(p: Pacer, chunks: list[str]) -> list[str]:
    out: list[str] = []
    for c in chunks:
        out += [w.text for w in p.push(c).released]
    return out


# ---------------------------------------------------------------------------------------
# lookahead


def test_how_followed_by_an_ordinary_word_is_released_within_one_word() -> None:
    p = pacer(FakeClock())
    p.begin_thought(1)
    assert p.push("How ").released == ()
    r = p.push("are you")
    assert [w.text for w in r.released] == ["How", "are"]  # released as soon as "are" is whole
    assert r.held == 0
    p.finish_thought()
    assert [w.text for w in p.shown] == ["How", "are", "you"]


def test_banned_phrase_split_across_chunks_and_punctuation_cuts_mid_thought() -> None:
    p = pacer(FakeClock())
    p.begin_thought(1)
    out = pushed(p, ["I think. Le", "t me kn", "ow, *if* you", " need anything"])
    assert out == ["I", "think."]
    assert p.push("more").stop
    hit = p.finish_thought()
    assert hit is not None and hit.phrase == "Let me know if" and hit.i == 2
    assert not hit.at_start and not hit.regenerate
    assert p.text == "I think."


def test_unresolved_prefix_at_thought_end_is_released() -> None:
    p = pacer(FakeClock())
    p.begin_thought(1)
    assert pushed(p, ["The end. Let me know"]) == ["The", "end."]
    assert p.finish_thought() is None
    assert p.text == "The end. Let me know"


def test_banned_phrase_completed_by_the_final_word() -> None:
    p = pacer(FakeClock())
    p.begin_thought(1)
    pushed(p, ["Fine. Let me know if"])
    hit = p.finish_thought()  # "if" is only known complete at the end
    assert hit is not None and hit.i == 1
    assert p.text == "Fine."


def test_truncated_last_word_completing_a_banned_phrase_is_cut() -> None:
    p = pacer(FakeClock())
    p.begin_thought(1)
    pushed(p, ["So. Let me know i"])  # generation stopped mid-word
    hit = p.finish_thought()
    assert hit is not None and hit.phrase == "Let me know if"
    assert p.text == "So."


def test_banned_phrase_in_the_tail_at_death() -> None:
    p = pacer(FakeClock())
    p.begin_thought(1)
    pushed(p, ["I am here. How can I hel"])
    hit = p.finish_thought(dead=True)
    assert hit is not None and hit.phrase == "How can I help" and not hit.regenerate
    assert p.text == "I am here."
    assert p.finish_thought(dead=True) is hit  # idempotent


def test_banned_at_start_regenerates_at_most_twice_then_cuts() -> None:
    p = pacer(FakeClock(), max_regenerations=2)
    p.begin_thought(1)
    r1 = p.push("As an AI language model, I")
    assert r1.regenerate and r1.hit is not None and r1.hit.at_start
    r2 = p.push("How can I help you")
    assert r2.regenerate and p.regenerations == 2
    r3 = p.push("I'm here to help. ")
    assert not r3.stop  # "help." is only known whole at the end
    hit = p.finish_thought()
    assert hit is not None and hit.at_start and not hit.regenerate
    assert p.shown == []


def test_banned_at_start_only_at_finish_asks_to_regenerate() -> None:
    p = pacer(FakeClock())
    p.begin_thought(1)
    assert pushed(p, ["How can I hel"]) == []
    hit = p.finish_thought()
    assert hit is not None and hit.regenerate
    p.push("I am here.")
    assert p.finish_thought() is None
    assert p.text == "I am here."


def test_lookahead_cap_releases_the_oldest() -> None:
    la = Lookahead(["a a a a a a a a a a"], cap=3)
    out: list[str] = []
    for w in ["a", "a", "a", "a", "a"]:
        rel, j, _ = la.push(w)
        assert j is None
        out += rel
    assert out == ["a", "a"] and la.held == ["a", "a", "a"]
    assert la.flush() == ["a", "a", "a"]


def test_lookahead_matches_normalized_words() -> None:
    la = Lookahead(["I'm here to help", "", "..."])
    assert list(la.phrases) == [("im", "here", "to", "help")]
    for w in ["Well,", "I’M", "HERE", "—", "to"]:
        la.push(w)
    _rel, j, phrase = la.push("help!")
    assert j == 0 and phrase == "I'm here to help"  # "Well," was released at once
    assert la.held[0] == "I’M"


def test_host_cut_stops_generation() -> None:
    p = pacer(FakeClock())
    p.begin_thought(1)
    r = p.push("I see. [host] t+")
    assert r.stop and p.cut_at_host
    p.finish_thought()
    assert p.text == "I see."


# ---------------------------------------------------------------------------------------
# cadence


def test_pause_after() -> None:
    assert pause_after_ms("word") == 90
    assert pause_after_ms("word,") == 250
    assert pause_after_ms("wait —") == 250
    assert pause_after_ms("end.") == 700
    assert pause_after_ms("“said.”") == 700
    assert pause_after_ms("why?)") == 700
    assert pause_after_ms("”") == 90


def test_same_seed_same_cadence_and_different_seed_differs() -> None:
    k = knobs(jitter=0.3, hesitation=0.3)
    words = [Word(1, i, w) for i, w in enumerate(TEXT.split())]

    def run(seed: int) -> list[Any]:
        p = pacer(FakeClock(), seed=seed)
        p.set_rate_estimate(5.0)
        return [p.cadence(w, k) for w in words]

    assert run(life_seed(0, 12)) == run(life_seed(0, 12))
    assert run(life_seed(0, 12)) != run(life_seed(0, 13))
    assert life_seed(0, 12) == 12 and life_seed(5, 12) != life_seed(6, 12)


def test_interval_adapts_to_rate_and_respects_the_floor() -> None:
    p = pacer(FakeClock(), adaptive=True)
    k = knobs(letter_ms=55, jitter=0.0)
    assert p.letter_interval_ms(k) == 55  # no estimate yet
    p.set_rate_estimate(5.0)
    assert p.letter_interval_ms(k) == pytest.approx(1000 / (0.88 * 5.0))
    p.set_rate_estimate(100.0)
    assert p.letter_interval_ms(k) == 55  # fast model: the floor wins
    p.set_rate_estimate(0)
    assert p.letter_interval_ms(k) == 55
    fixed = pacer(FakeClock(), adaptive=False)
    fixed.set_rate_estimate(1.0)
    assert fixed.letter_interval_ms(k) == 55
    tw = p.cadence(Word(1, 0, "abc."), k)
    assert len(tw.char_ms) == 4 and tw.pause_after_ms == 700 and tw.hesitate_before_ms == 0


def test_hesitations_before_and_inside_words() -> None:
    early = pacer(FakeClock(), hesitation_inside_from=0.1)
    k = knobs(hesitation=0.05)
    words = [Word(1, i, "longword") for i in range(400)]
    tws = [early.cadence(w, k) for w in words]
    assert all(max(t.char_ms) < 400 for t in tws)  # early in life: never inside a word
    assert 5 < sum(1 for t in tws if t.hesitate_before_ms) < 40
    late = pacer(FakeClock())
    k2 = knobs(hesitation=1.0, letter_ms=100, jitter=0.0)
    tws = [late.cadence(w, k2) for w in words]
    inside = [t for t in tws if max(t.char_ms) >= 400]
    before = [t for t in tws if t.hesitate_before_ms]
    assert len(inside) + len(before) == 400 and inside and before
    assert all(400 <= t.hesitate_before_ms <= 1200 for t in before)


def test_rate_is_measured_over_generation_only() -> None:
    clock = FakeClock()
    p = pacer(clock, min_rate_sample_s=3.0, rate_window_s=10.0)
    p.set_rate_estimate(9.0)
    p.begin_thought(1)
    clock.advance(30)  # prompt processing before the first chunk: not generation
    p.push("abcd")
    for _ in range(20):
        clock.advance(1)
        p.push("ab c")  # 3 letters per second
    assert p.rate() == pytest.approx(3.0)
    assert p._sample_s <= 10.0  # pyright: ignore[reportPrivateUsage]
    p.set_rate_estimate(4.0)  # a reload: the old measurements are dropped
    assert p.rate() == 4.0


def test_from_config(pi4_default: Config) -> None:
    p = Pacer.from_config(pi4_default, FakeClock(), seed=3)
    assert p.rate_margin == 0.88 and p.lookahead.cap == 8 and p.max_regenerations == 2
    assert ("how", "can", "i", "help") in p.lookahead.phrases
    assert p.hesitation_ms == (1200, 3600)  # decision 30


# ---------------------------------------------------------------------------------------
# speak: generation and typing together


def run_thoughts(
    thoughts: int,
    k: Knobs,
    tok_s: float,
    jitter: float = 0.0,
    estimate: float | None = None,
    prompt_s: float = 2.0,
    text: str = TEXT,
) -> tuple[Recorder, list[Spoken], Pacer]:
    async def main(clock: VirtualClock) -> tuple[Recorder, list[Spoken], Pacer]:
        rec = Recorder(clock)
        p = pacer(clock)
        p.set_rate_estimate(estimate if estimate is not None else tok_s * 3.4)
        spoken: list[Spoken] = []
        for turn in range(1, thoughts + 1):
            stream = scripted(clock, tokens_of(text), tok_s, jitter, seed=turn, prompt_s=prompt_s)
            spoken.append(await speak(p, stream, k, turn, rec))
        return rec, spoken, p

    return run_virtual(main)


def word_end(e: dict[str, Any]) -> float:
    return e["t"] + (sum(e["char_ms"]) + e["pause_after_ms"]) / 1000


def test_sync_rule_no_gen_start_before_the_previous_thought_is_shown() -> None:
    k = knobs(pause_s=3.0)
    rec, spoken, _ = run_thoughts(4, k, tok_s=1.35)
    assert [s.text for s in spoken] == [" ".join(TEXT.split())] * 4
    for turn in range(2, 5):
        last = [e for e in rec.of("word") if e["turn"] == turn - 1][-1]
        start = next(e for e in rec.of("gen_start") if e["turn"] == turn)
        first = next(e for e in rec.of("word") if e["turn"] == turn)
        assert start["t"] >= word_end(last) - 1e-9
        assert first["t"] >= word_end(last) + k.pause_s - 1e-9  # the pause is a minimum
    # each thought's words never overlap: the next word starts after the previous one typed
    words = rec.of("word")
    for a, b in itertools.pairwise(words):
        assert b["t"] >= word_end(a) - 1e-9


def test_generation_overlaps_typing() -> None:
    k = knobs(pause_s=0.0)
    rec, spoken, _ = run_thoughts(1, k, tok_s=1.35, prompt_s=0.0)
    gen_end = rec.of("gen_end")[0]["t"]
    first_word = rec.of("word")[0]["t"]
    last_end = word_end(rec.of("word")[-1])
    assert first_word < 10 < gen_end  # typing started long before generation ended
    # typing at 88% of the rate finishes a little after generation, not a thought later
    assert gen_end < last_end < gen_end * 1.35
    assert spoken[0].tokens == len(tokens_of(TEXT))


def test_no_bursts_at_30_percent_generation_jitter() -> None:
    k = knobs(jitter=0.10)
    rec, _, p = run_thoughts(5, k, tok_s=1.35, jitter=0.30)
    words = rec.of("word")
    interval_floor = min(min(e["char_ms"]) for e in words)
    assert interval_floor >= k.letter_ms * (1 - k.jitter) - 1
    # letters never come faster than the adaptive interval allows, and the interval moves
    # smoothly from word to word (a 60 s window): no sudden speed-ups
    means = [sum(e["char_ms"]) / len(e["char_ms"]) for e in words]
    for a, b in itertools.pairwise(means):
        assert b > a * (1 - 2 * k.jitter) - 5
    typing = sum(word_end(e) - e["t"] for e in words)
    assert p.stats.starved_s < 0.05 * typing
    assert max(p.stats.stalls, default=0) < 3.0


@pytest.mark.parametrize("profile", ["pi4/default", "pi4/skeleton-1200"])
def test_words_per_minute_within_the_overlay_range_at_every_keyframe(profile: str) -> None:
    cfg = load_config(profile, "pi4-4gb")
    s = Schedule.from_profile(cfg)
    costs = load_costs(cfg)
    birth = cfg.get("verify.wpm_birth_range")
    writing = cfg.get("verify.wpm_writing_range")
    for i, t in enumerate(s.times):
        k = s.at(t)
        tok_s = costs.tg(k.step, k.threads, k.cpu_share)
        rec, _spoken, _ = run_thoughts(2, k, tok_s=tok_s, jitter=0.2)
        words = [e for e in rec.of("word") if e["turn"] == 2]  # after the rate is measured
        minutes = (word_end(words[-1]) - words[0]["t"]) / 60
        wpm = len(words) / minutes
        lo, hi = birth if i == 0 else writing
        assert lo <= wpm <= hi, (profile, t, round(wpm, 1), tok_s)


def test_regeneration_through_speak() -> None:
    async def main(clock: VirtualClock) -> tuple[Recorder, Spoken]:
        rec = Recorder(clock)
        p = pacer(clock)
        texts = iter(["How can I help you?", "How can I help?", "I am here, still."])

        def stream() -> AsyncIterator[Chunk]:
            return scripted(clock, tokens_of(next(texts)), 2.0)()

        return rec, await speak(p, stream, knobs(), 1, rec)

    rec, sp = run_virtual(main)
    assert sp.requests == 3 and sp.regenerations == 2
    assert sp.text == "I am here, still." and sp.hit is None
    assert len(rec.of("gen_start")) == len(rec.of("gen_end")) == 3


def test_cut_at_host_through_speak() -> None:
    async def main(clock: VirtualClock) -> Spoken:
        rec = Recorder(clock)
        stream = scripted(clock, tokens_of("I notice it. [host] t+01:00 · fake reading"), 2.0)
        return await speak(pacer(clock), stream, knobs(), 1, rec)

    sp = run_virtual(main)
    assert sp.text == "I notice it." and sp.cut_at_host and sp.requests == 1


def test_death_flush_shows_queued_words_at_pace_then_returns() -> None:
    async def main(clock: VirtualClock) -> tuple[Recorder, Spoken, list[float]]:
        rec = Recorder(clock)
        p = pacer(clock)
        p.set_rate_estimate(3.0)  # typing is slow: words queue up
        died_at: list[float] = []
        pieces = tokens_of(TEXT)
        stream = scripted(clock, pieces, tok_s=20.0, die_after=60)
        sp = await speak(
            p, stream, knobs(pause_s=0), 1, rec, on_died=lambda st: died_at.append(clock.elapsed())
        )
        return rec, sp, died_at

    rec, sp, died_at = run_virtual(main)
    assert sp.died is not None and len(died_at) == 1
    words = rec.of("word")
    after = [e for e in words if e["t"] > died_at[0]]
    assert len(after) >= 20  # a full queue at the moment of death
    assert [e["text"] for e in words] == [w.text for w in sp.words]
    for a, b in itertools.pairwise(after):
        assert b["t"] == pytest.approx(word_end(a))  # at the current pace, not dumped
    # the last generated (possibly unfinished) word is shown too
    assert "".join(tokens_of(TEXT)[:60]).split()[-1] == words[-1]["text"]
    assert rec.of("gen_end")[-1]["tokens"] == 60


def test_death_before_any_word() -> None:
    async def main(clock: VirtualClock) -> Spoken:
        rec = Recorder(clock)
        stream = scripted(clock, tokens_of(TEXT), 2.0, die_after=0)
        return await speak(pacer(clock), stream, knobs(), 1, rec)

    sp = run_virtual(main)
    assert sp.died is not None and sp.words == []


def test_speak_cancellation_stops_the_drain() -> None:
    async def main(clock: VirtualClock) -> bool:
        rec = Recorder(clock)
        stream = scripted(clock, tokens_of(TEXT), 1.0)
        task = asyncio.ensure_future(speak(pacer(clock), stream, knobs(), 1, rec))
        await clock.sleep(5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return True

    assert run_virtual(main)


def test_stream_without_done_chunk_still_ends_the_request() -> None:
    async def main(clock: VirtualClock) -> tuple[Recorder, Spoken]:
        rec = Recorder(clock)

        async def gen() -> AsyncIterator[Chunk]:
            for piece in ["I am ", "here."]:
                await clock.sleep(0.5)
                yield Chunk(piece)

        return rec, await speak(pacer(clock), gen, knobs(), 1, rec)

    rec, sp = run_virtual(main)
    assert sp.text == "I am here." and sp.tokens == 2 and sp.prompt_n is None
    assert rec.of("gen_end")[0]["tokens"] == 2


def test_cadence_comes_from_config_decision_30() -> None:
    """The reveal speed is Yannick's decision 30 (3x slower); the pacer must take it from
    config, never from constructor defaults."""
    from epitaph.clock import FakeClock
    from epitaph.config import load_config
    from epitaph.pacing import Pacer

    cfg = load_config("pi4/default", "pi4-4gb")
    p = Pacer.from_config(cfg, FakeClock())
    rev = cfg.section("reveal")
    assert p.word_gap_ms == rev["word_gap_ms"] == 270
    assert p.comma_pause_ms == rev["comma_pause_ms"] == 750
    assert p.sentence_pause_ms == rev["sentence_pause_ms"] == 2100
    assert p.hesitation_ms == (1200, 3600)


def test_the_death_flush_fits_its_budget() -> None:
    """At death the queued words keep their rhythm's shape but end within the budget (life
    000019 on the Pi took 115 s at 600 ms a letter; verify allows 90)."""
    late = knobs(letter_ms=600.0, hesitation=0.2, jitter=0.4)

    def typing_time(budget: float | None) -> float:
        async def main(clock: VirtualClock) -> float:
            p = pacer(clock)
            p.begin_thought(1)
            pushed(p, tokens_of("the slow words of a dying mind keep coming " * 4))
            if budget is not None:
                p.flush_within(budget)
            p.finish_thought(dead=True)
            start = clock.elapsed()
            async for _ in p.drain(late):
                pass
            return clock.elapsed() - start

        return run_virtual(main)

    assert typing_time(None) > 40.0  # the late rhythm alone would overrun
    assert typing_time(20.0) <= 20.0 + 1e-6
