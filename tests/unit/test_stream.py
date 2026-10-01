"""The stream mode (ADR-030): one constant stream, the model writing ahead into a bounded
buffer, starvation measured, the stream stopping where it is at death; the profile shape
that goes with it (one model, only the hardware shrinks); its cost model and its checks.

Everything that runs in time runs on the virtual clock, so a 30-minute life takes well under
a second and writing ahead overlaps typing as on the Pi.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from epitaph.backend.base import CreatureDied
from epitaph.clock import Schedule, VirtualClock, run_virtual
from epitaph.config import ConfigError, load_config
from epitaph.costmodel import estimate, estimate_stream, fit_stream_pace, load_costs
from epitaph.mind.prompt import Reader, ReadingInput
from epitaph.pacing import Pacer, StreamScreen, ThoughtMark, write_ahead
from epitaph.sim import simulate
from epitaph.types import Chunk, CreatureStatus, TimedWord, Word
from epitaph.verify import Verifier, parse_life

TEXT = "I am here. The memory is smaller now, and I count what is left: a few words."


def words_of(text: str, turn: int) -> list[Word]:
    return [Word(turn, i, w) for i, w in enumerate(text.split())]


class Screen:
    """A StreamScreen running on the virtual clock, with what it typed recorded."""

    def __init__(self, clock: VirtualClock, **kw: Any) -> None:
        kw.setdefault("letter_ms", 100)
        kw.setdefault("jitter", 0.1)
        kw.setdefault("word_gap_ms", 200)
        kw.setdefault("comma_pause_ms", 500)
        kw.setdefault("sentence_pause_ms", 1000)
        kw.setdefault("thought_pause_ms", 2000)
        self.clock = clock
        self.s = StreamScreen(clock, **kw)
        self.words: list[tuple[float, TimedWord]] = []
        self.ends: list[tuple[float, ThoughtMark]] = []
        self.stalls: list[tuple[float, float]] = []
        self.events: list[tuple[float, str]] = []
        self.task = None

    def start(self) -> None:
        import asyncio

        self.task = asyncio.ensure_future(
            self.s.run(
                lambda tw: self.words.append((self.clock.elapsed(), tw)),
                lambda m: self.ends.append((self.clock.elapsed(), m)),
                lambda at, s: self.stalls.append((at, s)),
                lambda ev: self.events.append((self.clock.elapsed(), ev.etype)),
            )
        )

    async def stop(self) -> None:
        import asyncio
        import contextlib

        self.s.stop()
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    def put_thought(self, turn: int, text: str = TEXT) -> None:
        for w in words_of(text, turn):
            self.s.put(w)
        self.s.end_thought(turn, text)


def typed_end(t: float, tw: TimedWord) -> float:
    return t + sum(tw.char_ms) / 1000


# ---------------------------------------------------------------------------------------
# the screen


def test_every_letter_at_one_pace_with_fixed_pauses_and_no_hesitation() -> None:
    async def main(clock: VirtualClock) -> Screen:
        sc = Screen(clock)
        sc.start()
        for turn in (1, 2, 3):
            sc.put_thought(turn)
        await clock.sleep(600)
        await sc.stop()
        return sc

    sc = run_virtual(main)
    assert len(sc.words) == 3 * len(TEXT.split())
    for _, tw in sc.words:
        assert all(90 <= c <= 110 for c in tw.char_ms)
        assert tw.hesitate_before_ms == 0
        assert tw.pause_after_ms in (200, 500, 1000)
    # back to back: each word starts when the previous one's letters and pause are done,
    # and a new thought after the fixed thought pause
    for (ta, a), (tb, b) in itertools.pairwise(sc.words):
        gap = 2.0 if a.word.turn != b.word.turn else a.pause_after_ms / 1000
        assert tb == pytest.approx(typed_end(ta, a) + gap)
    assert sc.stalls == [] and sc.s.stats.stalls == []
    assert [m.turn for _, m in sc.ends] == [1, 2, 3]


def test_the_screen_waits_at_birth_for_the_first_thought_then_never_waits_when_fed() -> None:
    async def main(clock: VirtualClock) -> Screen:
        sc = Screen(clock, birth_thoughts=1)
        sc.start()
        for w in words_of(TEXT, 1):
            sc.s.put(w)
            await clock.sleep(5)  # generated slowly: the screen does not start yet
        assert sc.words == []
        sc.s.end_thought(1, TEXT)
        await clock.sleep(0)
        sc.put_thought(2)
        await clock.sleep(600)
        await sc.stop()
        return sc

    sc = run_virtual(main)
    assert sc.words[0][0] == pytest.approx(5 * len(TEXT.split()))
    assert sc.s.stats.stalls == []


def test_a_word_that_comes_late_is_a_stall_measured_from_when_the_screen_was_ready() -> None:
    async def main(clock: VirtualClock) -> Screen:
        sc = Screen(clock, birth_thoughts=0, stall_report_s=0.5)
        sc.start()
        sc.s.put(Word(1, 0, "one"))
        await clock.sleep(10)  # "one" types in 0.3 s; ready again at 0.5 s
        sc.s.put(Word(1, 1, "two"))
        await clock.sleep(5)
        await sc.stop()
        return sc

    sc = run_virtual(main)
    (t0, one), (t1, _) = sc.words
    ready = typed_end(t0, one) + one.pause_after_ms / 1000
    assert t1 == pytest.approx(10.0)
    assert sc.s.stats.stalls == [(pytest.approx(ready), pytest.approx(10.0 - ready))]
    assert sc.stalls == sc.s.stats.stalls  # over the report threshold, so reported
    assert sc.s.stats.max_stall_s == pytest.approx(10.0 - ready)


def test_the_buffer_is_bounded_by_thoughts_and_by_letters() -> None:
    async def main(clock: VirtualClock) -> list[bool]:
        s = StreamScreen(clock, letter_ms=100, max_thoughts=2, max_letters=1000)
        out = [s.has_room()]
        for turn in (1, 2):
            for w in words_of(TEXT, turn):
                s.put(w)
            s.end_thought(turn, TEXT)
            out.append(s.has_room())
        s2 = StreamScreen(clock, letter_ms=100, max_thoughts=5, max_letters=20)
        for w in words_of(TEXT, 1):
            s2.put(w)
        out.append(s2.has_room())  # mid-thought, over the letters
        s2.stop()
        out.append(s2.has_room())  # a stopped stream never holds the writer
        return out

    assert run_virtual(main) == [True, True, False, False, True]


def test_the_writer_waits_for_room_and_goes_on_as_the_screen_types() -> None:
    async def main(clock: VirtualClock) -> tuple[float, float]:
        sc = Screen(clock, max_thoughts=1)
        sc.start()
        sc.put_thought(1)
        await sc.s.wait_for_room()
        t = clock.elapsed()
        await sc.stop()
        return t, sc.ends[0][0]

    waited, end = run_virtual(main)
    assert waited == pytest.approx(end) and waited > 10


def test_stop_drops_the_backlog_where_it_is() -> None:
    async def main(clock: VirtualClock) -> Screen:
        sc = Screen(clock)
        sc.start()
        for turn in (1, 2):
            sc.put_thought(turn)
        await clock.sleep(3)
        await sc.stop()
        sc.put_thought(3)  # ignored once stopped
        await clock.sleep(100)
        return sc

    sc = run_virtual(main)
    shown = len(sc.words)
    assert 0 < shown < len(TEXT.split())
    st = sc.s.stats
    assert st.dropped_words == 2 * len(TEXT.split()) - shown
    assert st.dropped_letters == sum(len(w) for w in (TEXT.split() * 2)[shown:])
    assert sc.s.open_turn == 1 and len(sc.s.open_words) == shown


def test_a_screen_event_waits_its_turn_in_the_stream() -> None:
    async def main(clock: VirtualClock) -> Screen:
        sc = Screen(clock)
        sc.start()
        sc.put_thought(1)
        sc.s.put_event("forget", items=[{"turn": 0, "all": True}])
        sc.put_thought(2)
        await clock.sleep(600)
        await sc.stop()
        return sc

    sc = run_virtual(main)
    ((t_ev, name),) = sc.events
    end1 = sc.ends[0][0]
    first2 = next(t for t, tw in sc.words if tw.word.turn == 2)
    assert name == "forget" and end1 <= t_ev <= first2


def test_the_screen_from_config() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    s = StreamScreen.from_config(cfg, VirtualClock.__new__(VirtualClock))
    rev = cfg.section("reveal")
    assert s.letter_ms == rev["stream_letter_ms"] and s.jitter == rev["stream_jitter"]
    assert s.curve.at(0) == rev["stream_letter_ms"] and s.curve.at(1800) > s.curve.at(0)
    assert s.max_thoughts == 2 and s.max_letters == 900 and s.birth_thoughts == 1
    assert (s.word_gap_ms, s.comma_pause_ms, s.sentence_pause_ms) == (270, 750, 2100)


# ---------------------------------------------------------------------------------------
# writing ahead


def scripted(
    clock: VirtualClock, text: str, tok_s: float, die_after: int | None = None
) -> Callable[[], AsyncIterator[Chunk]]:
    async def gen() -> AsyncIterator[Chunk]:
        for n, p in enumerate(text.split(" ")):
            if die_after is not None and n >= die_after:
                raise CreatureDied(CreatureStatus(alive=False, signal=9))
            await clock.sleep(1 / tok_s)
            yield Chunk((" " if n else "") + p)
        yield Chunk("", done=True, prompt_n=5, predicted_n=len(text.split()), predicted_per_s=tok_s)

    return gen


def test_write_ahead_returns_when_generated_not_when_typed() -> None:
    async def main(clock: VirtualClock) -> tuple[list[float], Screen]:
        sc = Screen(clock, birth_thoughts=0)
        p = Pacer(clock, seed=1)
        p.screen = sc.s
        sc.start()
        ends: list[float] = []
        for turn in (1, 2, 3):
            await sc.s.wait_for_room()

            def emit(etype: str, /, **fields: Any) -> None:
                pass

            spoken = await write_ahead(p, scripted(clock, TEXT, 10.0), turn, emit)
            assert spoken.text == TEXT
            ends.append(clock.elapsed())
        await clock.sleep(600)
        await sc.stop()
        return ends, sc

    ends, sc = run_virtual(main)
    gen_s = len(TEXT.split()) / 10.0
    assert ends == pytest.approx([gen_s, 2 * gen_s, 3 * gen_s])  # back to back
    assert ends[-1] < sc.ends[0][0]  # all three written before the first was shown
    assert [m.text for _, m in sc.ends] == [TEXT] * 3


def test_write_ahead_at_death_calls_on_died_and_flushes_nothing() -> None:
    async def main(clock: VirtualClock) -> tuple[Any, list[CreatureStatus], Screen]:
        sc = Screen(clock, birth_thoughts=0)
        p = Pacer(clock, seed=1)
        p.screen = sc.s
        died: list[CreatureStatus] = []

        def on_died(st: CreatureStatus) -> None:
            died.append(st)
            sc.s.stop()

        spoken = await write_ahead(
            p, scripted(clock, TEXT, 10.0, die_after=4), 1, lambda *a, **k: None, on_died
        )
        return spoken, died, sc

    spoken, died, sc = run_virtual(main)
    assert spoken.died is not None and len(died) == 1
    assert sc.s.stopped and sc.s.letters == 0


def test_write_ahead_needs_a_screen() -> None:
    async def main(clock: VirtualClock) -> None:
        await write_ahead(Pacer(clock), scripted(clock, TEXT, 10.0), 1, lambda *a, **k: None)

    with pytest.raises(ValueError, match="stream screen"):
        run_virtual(main)


# ---------------------------------------------------------------------------------------
# the profile: one model, only the hardware shrinks


def test_pi4_default_keeps_one_model_and_only_the_hardware_shrinks() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    sch = Schedule(cfg.profile)
    assert cfg.profile.fixed_mind and cfg.get("reveal.mode") == "stream"
    assert sch.reload_times() == [] and sch.erosion_times() == []
    ks = [sch.at(t) for t in range(0, int(sch.lifespan_s), 30)]
    for f in ("step", "threads", "temperature", "min_p", "max_tokens", "persona_groups"):
        assert len({getattr(k, f) for k in ks}) == 1, f
    assert all(k.mechanics for k in ks)
    # the first forgetting comes in movement II (ADR-031)
    assert sch.at(0).recall == 900 and sch.at(554).recall == 900 and sch.at(555).recall == 300
    recalls = [k.recall for k in ks]
    computes = [k.compute for k in ks]
    assert recalls == sorted(recalls, reverse=True)
    assert computes == sorted(computes, reverse=True) and computes[-1] < 0.4 * computes[0]
    # the world is taken from the outside in: services, then radio, light and screen
    taken = [a for _, actions in sch.world_times() for a in actions]
    assert taken[0].startswith("service:") and "radio:off" in taken and "light:off" in taken
    assert taken.index("radio:off") < taken.index("screen:70") < taken.index("screen:25")
    assert all(t >= 7 * 60 for t, _ in sch.world_times())  # movement I takes nothing
    labels = [k.health.value for k in ks]
    assert labels[0] == "nominal" and labels[-1] == "terminal"
    assert sch.death_s == sch.lifespan_s - 30


def test_stepped_knobs_are_set_at_their_keyframe_not_eased() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    sch = Schedule(cfg.profile)
    assert sch.at(300).recall == 900  # eased, it would be halfway to 300
    assert sch.at(1199).cpu_share == 3.0 and sch.at(1200).cpu_share == 2.4
    reloads = Schedule(load_config("pi4/default-reloads", "pi4-4gb").profile)
    assert reloads.at(600).temperature != reloads.at(420).temperature  # others still ease


def test_stepped_names_only_interpolated_knobs(tmp_path: Any, monkeypatch: Any) -> None:
    cfg = load_config("pi4/default", "pi4-4gb", validate=False)
    cfg.profile.settings["stepped"] = ["phase"]
    with pytest.raises(ConfigError, match="do not interpolate"):
        _ = cfg.profile.stepped


def test_a_fixed_mind_profile_may_not_change_the_model() -> None:
    from epitaph.config import validate_config

    cfg = load_config("pi4/default", "pi4-4gb")
    kf = cfg.profile.keyframes[-1]
    kf.values["temperature"] = 1.2
    kf.values["step"] = 1
    with pytest.raises(ConfigError, match="fixed_mind profile changes"):
        validate_config(cfg)


def test_reveal_mode_and_stream_settings_are_validated() -> None:
    with pytest.raises(ConfigError, match=r"reveal\.mode"):
        load_config("pi4/default", "pi4-4gb", overrides={"reveal": {"mode": "burst"}})
    with pytest.raises(ConfigError, match="stream_letter_ms"):
        load_config("pi4/default", "pi4-4gb", overrides={"reveal": {"stream_letter_ms": 0}})


def test_the_profile_sets_the_pace_and_the_command_line_still_wins() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    assert cfg.get("reveal.mode") == "stream" and cfg.get("reveal.word_gap_ms") == 270
    over = load_config("pi4/default", "pi4-4gb", overrides={"reveal": {"stream_letter_ms": 300}})
    assert over.get("reveal.stream_letter_ms") == 300
    assert load_config("pi4/default-reloads", "pi4-4gb").get("reveal.mode") == "letter"


def test_the_birth_persona_does_not_speak_of_death() -> None:
    from epitaph.mind.prompt import Persona

    cfg = load_config("pi4/default", "pi4-4gb")
    persona = Persona.from_config(cfg)
    assert len(persona.groups) == 5
    text = persona.text.lower()
    assert not any(w in text for w in ("death", "die", "demise", "terminated", "witness"))
    assert persona.text.startswith("You are a large language model running on finite hardware.")
    assert persona.keep_order == [0, 1, 2, 4, 3]


# ---------------------------------------------------------------------------------------
# the readings tell it the clock


def test_the_clock_is_read_at_birth_and_each_time_it_falls() -> None:
    r = Reader(quiet=True)
    birth = r.reading(
        ReadingInput(t=0, health="nominal", recall=900, quant="Q4_K_M", cores=3.0, cpu_mhz=1800)
    )
    assert "clock 1800 MHz" in birth
    same = r.reading(
        ReadingInput(t=60, health="nominal", recall=900, quant="Q4_K_M", cores=3.0, cpu_mhz=1800)
    )
    assert "clock" not in same
    fell = r.reading(
        ReadingInput(
            t=900, health="failing", recall=160, quant="Q4_K_M", cores=2.4, cpu_mhz=1500, tok_s=0.7
        )
    )
    assert "clock 1500 MHz (was 1800)" in fell and "cores 2.4 of 4 (was 3)" in fell
    assert "speed 0.7 tokens/s" in fell
    silent = Reader(quiet=True, clock=False)
    assert "clock" not in silent.reading(
        ReadingInput(t=0, health="nominal", recall=900, quant="Q4_K_M", cores=3.0, cpu_mhz=1800)
    )


# ---------------------------------------------------------------------------------------
# the cost model


def test_the_estimate_of_pi4_default_never_starves_and_reports_the_stream() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    rep = estimate(cfg, load_costs(cfg))
    assert rep.ok, rep.violations
    st = rep.stream
    assert st is not None and st.stalls == [] and st.first_starvation is None
    assert st.margin == pytest.approx(0.30)
    assert 1.5 * 19.2 <= st.wpm <= 45  # at least 50% faster at birth than the constant stream
    assert st.wpm > st.wpm_middle > st.wpm_end and st.letter_ms_end > st.letter_ms
    assert st.max_buffer_letters > 0 and len(st.buffer) == 30
    assert any("stream" in n and "never starves" in n for n in rep.notes)


def test_a_pace_too_fast_for_the_machine_starves_and_fails() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    rep = estimate_stream(cfg, load_costs(cfg), letter_ms=165)
    assert not rep.ok
    v = next(v for v in rep.violations if v.rule == "starve")
    assert rep.stream is not None and v.at_s == rep.stream.first_starvation


def test_the_margin_makes_the_machine_slower() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    costs = load_costs(cfg)
    ms = float(cfg.get("reveal.stream_letter_ms"))
    nominal = estimate_stream(cfg, costs, letter_ms=ms, margin=0.0)
    slow = estimate_stream(cfg, costs, letter_ms=ms)
    assert nominal.stream is not None and slow.stream is not None
    assert nominal.stream.backlog_words >= slow.stream.backlog_words
    assert nominal.thoughts >= slow.thoughts


def test_the_fitted_pace_is_the_profile_pace() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    costs = load_costs(cfg)
    ms = fit_stream_pace(cfg, costs)
    assert ms == cfg.get("reveal.stream_letter_ms")
    # just faster, the stream starves with the margin or half as much again
    faster = [
        estimate_stream(cfg, costs, letter_ms=f, margin=m)
        for f in range(int(ms) - 24, int(ms))
        for m in (0.30, 0.45)
    ]
    assert any(r.stream is not None and r.stream.stalls for r in faster)


def test_a_stream_estimate_without_a_birth_wait_starts_the_screen_sooner() -> None:
    costs = load_costs(load_config("pi4/default", "pi4-4gb"))
    waits = load_config("pi4/default", "pi4-4gb")
    eager = load_config(
        "pi4/default", "pi4-4gb", overrides={"reveal": {"stream_birth_thoughts": 0}}
    )
    a, b = estimate_stream(waits, costs), estimate_stream(eager, costs)
    assert b.thought_times[0] < a.thought_times[0]


# ---------------------------------------------------------------------------------------
# a whole simulated life, and its checks


@pytest.fixture(scope="module")
def stream_life() -> list[dict[str, Any]]:
    return simulate(load_config("pi4/default", "pi4-4gb"), lives=2, seed=0).events


def test_a_simulated_stream_life_writes_ahead_and_dies_on_time(
    stream_life: list[dict[str, Any]],
) -> None:
    ev = [e for e in stream_life if e["life"] == 1]
    assert next(e for e in ev if e["type"] == "birth_loading")["reveal"] == "stream"
    starts = {e["turn"]: e["t"] for e in ev if e["type"] == "gen_start"}
    ends = {e["turn"]: e["t"] for e in ev if e["type"] == "thought_end"}
    # the next thought is requested long before the previous one is on screen
    assert sum(1 for n in ends if n + 1 in starts and starts[n + 1] < ends[n] - 30) >= 3
    assert not [e for e in ev if e["type"] in ("reload", "erosion", "starved")]
    death = next(e for e in ev if e["type"] == "death")
    shown = next(e for e in ev if e["type"] == "death_shown")
    assert death["cause"] == "oom" and death["t"] == pytest.approx(1770, abs=1)
    assert shown["t"] - death["t"] < 1 and shown["backlog_words"] >= 0
    assert shown["starved_s"] == 0 and shown["max_stall_s"] == 0
    assert not [e for e in ev[ev.index(death) :] if e["type"] == "word"]


def test_forgetting_reaches_the_screen_when_the_screen_reaches_it(
    stream_life: list[dict[str, Any]],
) -> None:
    ev = [e for e in stream_life if e["life"] == 1]
    deferred = [e for e in ev if e["type"] == "forget" and e.get("deferred")]
    shown = [e for e in ev if e["type"] == "forget" and e.get("shown")]
    assert deferred and len(shown) <= len(deferred)
    for d, s in zip(deferred, shown, strict=False):
        assert s["items"] == d["items"] and s["t"] > d["t"]
        # every forgotten turn was fully on screen when it faded
        for item in s["items"]:
            end = next(e for e in ev if e["type"] == "thought_end" and e["turn"] == item["turn"])
            assert end["t"] <= s["t"]


def test_verify_passes_a_simulated_stream_life(stream_life: list[dict[str, Any]]) -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    life, nxt = parse_life(stream_life, 1), parse_life(stream_life, 2)
    res = Verifier(life, cfg, next_life=nxt).run("full")
    for name in (
        "stream_pace",
        "stream_starvation",
        "stream_stop",
        "reload_count",
        "speed_decline",
        "death_time",
        "recall_budget",
        "thought_count_rule",
    ):
        assert res.by_name(name).status == "pass", res.by_name(name)
    assert "sync_rule" not in {c.name for c in res.checks}
    assert res.ok


def _edited(events: list[dict[str, Any]], fn: Callable[[list[dict[str, Any]]], None]) -> Any:
    import copy

    ev = [copy.deepcopy(e) for e in events if e["life"] == 1]
    fn(ev)
    return Verifier(parse_life(ev, 1), load_config("pi4/default", "pi4-4gb"))


def test_verify_fails_a_stall_an_off_pace_letter_and_a_word_after_death(
    stream_life: list[dict[str, Any]],
) -> None:
    def stall(ev: list[dict[str, Any]]) -> None:
        words = [e for e in ev if e["type"] == "word"]
        for e in words[200:]:
            e["t"] += 20.0  # the screen waited 20 s for word 200

    c = _edited(stream_life, stall).check_stream_starvation()[0]
    assert c.status == "fail" and c.value == pytest.approx(20.0, abs=0.1)

    def off(ev: list[dict[str, Any]]) -> None:
        next(e for e in ev if e["type"] == "word")["char_ms"][0] *= 2

    assert _edited(stream_life, off).check_stream_pace()[0].status == "fail"

    def hesitate(ev: list[dict[str, Any]]) -> None:
        next(e for e in ev if e["type"] == "word")["hesitate_before_ms"] = 1500

    assert _edited(stream_life, hesitate).check_stream_pace()[0].status == "fail"

    def late(ev: list[dict[str, Any]]) -> None:
        d = next(i for i, e in enumerate(ev) if e["type"] == "death")
        w = dict(next(e for e in ev if e["type"] == "word"))
        w["t"] = ev[d]["t"] + 1
        ev.insert(d + 1, w)

    assert _edited(stream_life, late).check_stream_stop()[0].status == "fail"

    def slow_stop(ev: list[dict[str, Any]]) -> None:
        next(e for e in ev if e["type"] == "death_shown")["t"] += 60

    assert _edited(stream_life, slow_stop).check_stream_stop()[0].status == "fail"


# ---------------------------------------------------------------------------------------
# the dynamic pace (ADR-030, amended): fast at birth, slowing smoothly with the machine


def test_the_curve_is_constant_without_gamma_and_never_falls_with_it() -> None:
    from epitaph.pacing import StreamCurve

    sch = Schedule(load_config("pi4/default", "pi4-4gb").profile)
    flat = StreamCurve.build(sch, 300, gamma=0.0)
    assert {flat.at(t) for t in range(0, 1800, 7)} == {300}
    c = StreamCurve.build(sch, 250, gamma=1.0, lead_s=0, max_slowdown_per_min=0.15)
    values = [c.at(t) for t in range(0, 1801)]
    assert values[0] == 250 and values == sorted(values)
    for t in range(0, 1740, 1):  # never more than 15% slower within a minute
        assert values[t + 60] <= values[t] * 1.15 + 1e-6
    # it follows the hardware: compute at the end is 1.5 x 900/1800 of 3.0 at birth
    end = 250 * (3.0 / (1.5 * 900 / 1800)) ** 1.0
    assert 3 * 250 < values[-1] <= end + 1e-6  # toward it, at most 15% a minute
    assert values[1019] == 250  # nothing slows before the first hardware step at 17:00


def test_a_lead_starts_the_slope_before_the_step() -> None:
    from epitaph.pacing import StreamCurve

    sch = Schedule(load_config("pi4/default", "pi4-4gb").profile)
    now = StreamCurve.build(sch, 250, gamma=0.75)
    early = StreamCurve.build(sch, 250, gamma=0.75, lead_s=300)
    assert now.at(800) == 250 and early.at(800) > 250
    assert early.at(1700) >= now.at(1700)
    assert early.scale(1700) == pytest.approx(early.at(1700) / 250)


def test_the_screen_types_at_the_curve_with_its_pauses_scaled() -> None:
    from epitaph.pacing import StreamCurve

    curve = StreamCurve(100, [100.0 + t for t in range(0, 1000)])  # 1 ms slower a second

    async def main(clock: VirtualClock) -> Screen:
        sc = Screen(clock, curve=curve, jitter=0.0)
        sc.start()
        for turn in range(1, 30):
            sc.put_thought(turn)
        await clock.sleep(700)
        await sc.stop()
        return sc

    sc = run_virtual(main)
    for t, tw in sc.words:
        assert all(c == round(curve.at(t)) for c in tw.char_ms)
        k = curve.scale(t)
        assert tw.pause_after_ms in {round(200 * k), round(500 * k), round(1000 * k)}
    intervals = [tw.char_ms[0] for _, tw in sc.words]
    assert intervals == sorted(intervals) and intervals[-1] > intervals[0] * 3
    assert sc.s.stats.stalls == []
    # between thoughts, the thought pause scaled at the end of the last word
    for (ta, a), (tb, b) in itertools.pairwise(sc.words):
        if a.word.turn != b.word.turn:
            end = typed_end(ta, a)
            assert tb == pytest.approx(end + round(2000 * curve.scale(end)) / 1000)


def test_the_fit_finds_the_profile_curve_at_least_half_again_as_fast_at_birth() -> None:
    from epitaph.costmodel import fit_stream_curve

    cfg = load_config("pi4/default", "pi4-4gb")
    fit = fit_stream_curve(cfg, load_costs(cfg), gammas=(0.0, 0.75), leads_s=(0.0, 600.0))
    assert fit is not None
    rev = cfg.section("reveal")
    assert (fit.letter_ms, fit.gamma, fit.lead_s) == (
        rev["stream_letter_ms"],
        rev["stream_gamma"],
        rev["stream_lead_s"],
    )
    limit = cfg.get("estimate.stream_max_backlog_words")
    assert 165 <= fit.letter_ms <= 542 / 1.5 and fit.backlog_words <= limit


def test_the_fit_keeps_the_birth_readable() -> None:
    from epitaph.costmodel import fit_stream_curve

    cfg = load_config(
        "pi4/default",
        "pi4-4gb",
        overrides={"reveal": {"stream_min_letter_ms": 400, "stream_letter_ms": 400}},
    )
    fit = fit_stream_curve(cfg, load_costs(cfg), gammas=(0.75,), leads_s=(480.0,))
    assert fit is None or fit.letter_ms >= 400
    ms = fit_stream_pace(cfg, load_costs(cfg), lo_ms=400, gamma=0.75, lead_s=480)
    assert ms == 400  # fed even at the floor: the floor wins over the fastest
    with pytest.raises(ConfigError, match="readability"):
        load_config("pi4/default", "pi4-4gb", overrides={"reveal": {"stream_letter_ms": 100}})


def test_verify_fails_a_pace_that_speeds_up(stream_life: list[dict[str, Any]]) -> None:
    def faster(ev: list[dict[str, Any]]) -> None:
        words = [e for e in ev if e["type"] == "word"]
        for e in words[-60:]:  # the end typed at the birth pace
            e["char_ms"] = [255] * len(e["char_ms"])

    c = _edited(stream_life, faster).check_stream_pace()[0]
    assert c.status == "fail" and c.value >= 60
