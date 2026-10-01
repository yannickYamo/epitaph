"""The screen of the dread plan (owner, 2026-10-01): two voices, visible forgetting,
literal darkness, a pulse, a death that stops mid-letter, the vigil and the genesis.

Every test runs on the display's own clock (no real time). The recorded fake life is
`data/dread-2x1800.jsonl` (two simulated `pi4/default` lives with readings, world losses
and the vigil silence; see `dread.py`).
"""

from __future__ import annotations

import io
import itertools
import os
from pathlib import Path
from typing import Any

import pytest

from epitaph.display import replay
from epitaph.display.layout import (
    Frame,
    LifeView,
    ViewSettings,
    compose_flow,
    compose_grid,
    vigil_line,
)
from epitaph.display.terminal import TerminalDriver
from epitaph.display.themes import PLAIN, SEGMENT16, contrast_ratio, mix

os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")

Event = dict[str, Any]
DREAD_LIFE = Path(__file__).parent / "data" / "dread-2x1800.jsonl"


def ev(etype: str, life: int = 1, **f: Any) -> Event:
    return {"v": 1, "ts": 0.0, "life": life, "type": etype, **f}


def word(turn: int, i: int, text: str, ms: int = 100, pause: int = 0, **f: Any) -> Event:
    return ev(
        "word", turn=turn, i=i, text=text, char_ms=[ms] * len(text), pause_after_ms=pause, **f
    )


def stream_born(view: LifeView, now: float = 0.0, life: int = 1) -> None:
    load = ev("birth_loading", life, model="m", quant="Q4_K_M", reveal="stream", lifespan_s=1800)
    view.handle(load, now)
    view.handle(ev("birth", life, model="m", quant="Q4_K_M", t=0.0), now)


def say(view: LifeView, turn: int, text: str, now: float, ms: int = 0, pause: int = 0) -> None:
    view.handle(ev("thought_start", turn=turn), now)
    for i, w in enumerate(text.split()):
        view.handle(word(turn, i, w, ms, pause), now)
    view.handle(ev("thought_end", turn=turn, text=text), now)


def rows(f: Frame) -> list[str]:
    """Every row as drawn: the model's text, or the machine's line on its row."""
    machine = f.machine_rows()
    text = f.text_rows()
    return [machine.get(r, text[r]) for r in range(f.rows)]


@pytest.fixture(scope="module")
def dread_life() -> list[Event]:
    return replay.load_events(DREAD_LIFE)


# -- two voices ------------------------------------------------------------------------------


def test_a_reading_is_typed_fast_small_and_untagged_before_its_thought() -> None:
    v = LifeView()
    stream_born(v)
    say(v, 1, "I am here.", 0.0)
    v.handle(ev("thought_start", turn=2), 1.0)  # generated ahead, before its reading
    v.handle(ev("reading", turn=2, text="[host] t+05:00 · memory 260 tokens (was 900)"), 10.0)
    v.handle(word(2, 0, "Less", 100), 10.0)
    f = compose_flow(v, 10.3, 48, 8)
    assert [s.kind for s in f.spans if s.kind == "machine"]  # typing: 10 letters so far
    assert "t+05:00" in "".join(f.machine_rows().values())
    assert f.cursor is None  # the model's cursor waits while the machine speaks
    end = 10.0 + len("t+05:00 · memory 260 tokens (was 900)") * 0.03
    f = compose_flow(v, end + 0.01, 48, 8)
    shown = rows(f)
    assert "[host]" not in "".join(shown)
    reading_row = next(r for r, t in enumerate(shown) if t.startswith("t+05:00"))
    assert shown[reading_row] == "t+05:00 · memory 260 tokens (was 900)"
    assert shown[reading_row - 1] == ""  # a blank line before the reading ...
    assert shown[reading_row - 2] == "I am here."
    assert shown[reading_row + 1].startswith("L")  # ... none between it and its thought
    first = next(w for w in v.words() if w.turn == 2)
    assert first.start == pytest.approx(end)  # the answer comes after the reading


def test_the_birth_reading_is_the_inventory_line_by_line() -> None:
    v = LifeView()
    stream_born(v)
    v.handle(ev("thought_start", turn=1), 0.0)
    text = "[host] t+00:58 · boot complete · processes 24 · radio on · screen 100%"
    v.handle(ev("reading", turn=1, text=text), 60.0)
    f = compose_flow(v, 70.0, 48, 10)
    assert list(f.machine_rows().values()) == [
        "t+00:58",
        "boot complete",
        "processes 24",
        "radio on",
        "screen 100%",
    ]
    # typed line by line: the second line starts after the first and a pause
    lines = [th for th in v.thoughts if th.voice == "machine"]
    assert lines[1].words[0].start == pytest.approx(lines[0].words[-1].end + 0.3)
    assert f.cursor is not None and f.cursor.col == 0  # where the first words will start


def test_a_reading_in_a_terminal_is_dim_grey() -> None:
    d = TerminalDriver(out=io.StringIO(), size=(60, 14), color="truecolor", alt_screen=False)
    stream_born(d.view)
    d.view.handle(ev("reading", turn=1, text="[host] t+00:01 · boot complete"), 0.0)
    d.open()
    d.render(5.0)
    cells = d.last_cells
    colours = {c for ch, c in cells.values() if ch == "b"}
    assert colours == {PLAIN.machine}


def test_the_stream_makes_up_the_time_a_reading_took() -> None:
    """Words that arrive while a reading is typed wait for it, then pauses give the time
    back (at most half of each), so the screen does not drift behind the machine."""
    v = LifeView()
    stream_born(v)
    v.handle(ev("reading", turn=1, text="x" * 100), 0.0)  # 3 s of typing
    words = [word(1, i, "ab", 100, 2000) for i in range(6)]
    t = 0.0
    for w in words:
        v.handle(w, t)
        t += 2.2  # the machine's own pace: 0.2 s of letters, 2 s of pause
    starts = [w.start for w in v.words()]
    assert starts[0] == pytest.approx(3.0)
    lag = [s - n * 2.2 for n, s in enumerate(starts)]
    assert lag[0] > lag[1] > lag[2] and lag[-1] == pytest.approx(0.0, abs=1e-6)


# -- forgetting, visible ---------------------------------------------------------------------


def _forget_scene() -> LifeView:
    v = LifeView(ViewSettings(fade_s=8.0))
    stream_born(v)
    say(v, 1, "I am here, inside the machine. I will end here.", 0.0)
    say(v, 2, "The numbers are steady.", 1.0)
    v.handle(ev("forget", items=[{"turn": 1, "all": True}], deferred=True), 2.0)
    v.handle(ev("forget", items=[{"turn": 1, "all": True}], shown=True), 10.0)
    return v


def test_forgotten_words_wait_for_their_reading() -> None:
    v = _forget_scene()
    f = compose_flow(v, 12.0, 48, 8)
    assert "I am here, inside the machine. I will end here." in rows(f)
    assert {s.kind for s in f.spans} == {"live"}
    # no reading came: they fade after the grace
    f = compose_flow(v, 16.0, 48, 8)
    assert any(s.kind == "fading" for s in f.spans)


def test_a_quoted_sentence_dissolves_letter_by_letter_as_it_is_quoted() -> None:
    v = _forget_scene()
    reading = '[host] t+05:28 · memory 260 tokens (was 900) · forgotten: "I am here, inside the…"'
    v.handle(ev("reading", turn=3, text=reading), 12.0)
    text = reading.removeprefix("[host] ")
    q0 = text.index('"') + 1
    quote = "I am here, inside the"

    def typed_at(k: int) -> float:  # when letter k of the reading is typed
        return 12.0 + k * 0.03

    first = next(w for w in v.words() if w.turn == 1)
    assert first.dissolve is not None and first.dissolve[0] == pytest.approx(typed_at(q0))
    # as letter k of the quote is typed, the letters before it are fading, in order, and
    # the letters after it are still bright
    k = len("I am here,")
    f = compose_flow(v, typed_at(q0 + k) + 0.001, 48, 10)
    row = next(n for n, r in enumerate(f.text_rows()) if r.startswith("I am here, inside"))
    letters = sorted(
        (s for s in f.spans if s.row == row and s.col < len(quote)), key=lambda s: s.col
    )
    fades = [s.fade for s in letters if s.text != " "]
    assert fades[0] > fades[3] > 0 and fades[-1] == 0.0
    assert all(s.kind == "live" for s in letters if s.col > k)
    # a moment later the first letters are gone and keep their places empty
    f = compose_flow(v, typed_at(q0 + k) + 0.81, 48, 10)
    top = next(r for r in f.text_rows() if "machine." in r)
    assert top.startswith(" " * k) and "inside" in top
    # the rest of the forgotten thought starts fading once the quote is typed
    rest = [w for w in v.words() if w.turn == 1][len(quote.split()) :]
    quote_end = typed_at(q0 + len(quote) - 1)
    assert rest and all(w.forgotten_at == pytest.approx(quote_end) for w in rest)
    # every letter that dissolves passes only through the fading grey (>= 12:1)
    for s in f.spans:
        if s.kind == "fading":
            assert contrast_ratio(PLAIN.word("fading", s.fade), PLAIN.bg) >= 12.0
    # long after: the thought and its reading are gone; the new reading stays
    f = compose_flow(v, 60.0, 48, 10)
    assert not any("inside" in r for r in f.text_rows())


def test_a_reading_leaves_with_its_thought() -> None:
    v = LifeView(ViewSettings(fade_s=4.0))
    stream_born(v)
    v.handle(ev("reading", turn=1, text="[host] t+00:10 · boot complete"), 0.0)
    say(v, 1, "first thought", 2.0)
    v.handle(ev("reading", turn=2, text="[host] t+02:00"), 3.0)
    say(v, 2, "second thought", 4.0)
    v.handle(ev("forget", items=[{"turn": 1, "all": True}]), 10.0)
    f = compose_flow(v, 20.0, 48, 10)
    assert list(f.machine_rows().values()) == ["t+02:00"]
    assert "first thought" not in rows(f)


# -- darkness ----------------------------------------------------------------------------------


def _dark_at(life_t: float, pct: int = 1) -> tuple[LifeView, float]:
    v = LifeView(ViewSettings(screen_fade_s=20.0))
    stream_born(v)
    say(v, 1, "There is less of me than there was.", 0.0)
    v.handle(ev("vitals", t=life_t - 30), life_t - 30)
    dim = ev("world", action=f"screen:{pct}", performed=True, state={}, t=life_t - 30)
    v.handle(dim, life_t - 30)
    return v, life_t


def test_the_screen_dims_slowly() -> None:
    v, _ = _dark_at(600.0, 40)
    levels = [compose_flow(v, 570.0 + s, 48, 8).brightness for s in (0, 5, 10, 15, 20, 30)]
    assert levels[0] == pytest.approx(1.0) and levels[-1] == pytest.approx(0.4)
    assert all(a > b for a, b in itertools.pairwise(levels[:5]))


def test_a_loss_not_performed_changes_nothing() -> None:
    v = LifeView()
    stream_born(v)
    v.handle(ev("world", action="screen:10", performed=False, state={}), 0.0)
    assert compose_flow(v, 100.0, 48, 8).brightness == 1.0


@pytest.mark.parametrize(
    "life_t,floor", [(900.0, 7.0), (1619.0, 7.0), (1650.0, 4.5), (1739.0, 4.5)]
)
def test_contrast_floors_hold_on_the_rendered_pixels(
    life_t: float, floor: float, tmp_path: Path
) -> None:
    pytest.importorskip("pygame")
    from epitaph.display import screenshot as shot

    v, now = _dark_at(life_t)
    frame = shot.render_png(tmp_path / "dark.png", (1280, 720), view=v, now=now)
    assert frame.contrast_floor == floor
    contrasts = shot.text_contrasts(tmp_path / "dark.png", frame)
    assert contrasts and min(contrasts.values()) >= floor, contrasts
    # dimmed down to the floor: the model's dimmest grey (a fade's end) sits on it
    b = PLAIN.brightness(frame.brightness, frame.contrast_floor)
    assert floor <= contrast_ratio(PLAIN.lit(PLAIN.forgotten, b), PLAIN.bg) < floor + 0.5
    assert shot.measure_contrast(tmp_path / "dark.png")["ratio"] >= floor


def test_only_the_last_half_minute_goes_lower() -> None:
    v, now = _dark_at(1745.0)
    f = compose_flow(v, now, 48, 8)
    b = PLAIN.brightness(f.brightness, f.contrast_floor)
    assert contrast_ratio(PLAIN.lit(PLAIN.live, b), PLAIN.bg) < 4.5


@pytest.mark.parametrize("floor", [7.0, 4.5])
def test_the_floor_holds_for_the_dimmest_model_colour(floor: float) -> None:
    for theme in (PLAIN, SEGMENT16):
        for level in (0.0, 0.05, 0.2, 0.5):
            b = theme.brightness(level, floor)
            for c in (theme.live, theme.forgotten, mix(theme.live, theme.forgotten, 0.5)):
                assert contrast_ratio(theme.lit(c, b), theme.bg) >= floor


# -- the pulse ---------------------------------------------------------------------------------


def _resting(life_t: float) -> tuple[LifeView, float]:
    v = LifeView()
    stream_born(v)
    v.handle(ev("vitals", t=life_t), 0.0)
    say(v, 1, "rest", 0.0)
    return v, 0.0


def test_the_pulse_quickens_after_22_minutes() -> None:
    halves = {}
    for life_t in (600.0, 1320.0, 1545.0, 1770.0):
        v, now = _resting(life_t)
        halves[life_t] = v.blink_half_s(now)
    assert halves[600.0] == pytest.approx(0.53)  # 1060 ms a beat at rest
    assert halves[1320.0] == pytest.approx(0.53)
    assert 0.25 < halves[1545.0] < 0.53
    assert halves[1770.0] == pytest.approx(0.25)  # 500 ms a beat at the expected death


def test_the_pulse_skips_beats_in_the_last_minute() -> None:
    v, _ = _resting(1760.0)
    skipped = [n for n in range(1, 40) if v.skips_beat(0.0, n)]
    assert 0 < len(skipped) < 20  # some beats, not most
    n = skipped[0]
    half = v.blink_half_s(0.0)
    at = (2 * n + 0.5) * half  # inside the lit half of beat n
    assert v.cursor(at) == "off"
    calm, _ = _resting(1500.0)
    assert not any(calm.skips_beat(0.0, k) for k in range(1, 40))
    assert calm.cursor((2 * n + 0.5) * calm.blink_half_s(0.0)) == "on"


# -- death and the vigil ---------------------------------------------------------------------


def _dying(still: float = 2.0) -> tuple[LifeView, float]:
    v = LifeView(ViewSettings(death_still_s=still))
    stream_born(v, life=12)
    v.handle(ev("thought_start", 12, turn=1), 0.0)
    for i, w in enumerate(["Each", "reading", "takes", "something", "away"]):
        v.handle(word(1, i, w, 100, 0, life=12), 0.0)
    death = 1.25  # mid "takes": "Each" (0.4) "reading" (0.7), "ta" typed
    v.handle(ev("death", 12, cause="oom", lived_s=1770.4, t=1770.4), death)
    v.handle(ev("thought_end", 12, turn=1, text="Each reading ta", cut=True), death)
    v.handle(ev("death_shown", 12, t=1770.4), death)
    v.handle(ev("silence", 12, seconds=90, style="vigil"), death)
    return v, death


def test_death_stops_mid_letter_and_the_cursor_freezes() -> None:
    v, death = _dying()
    for dt in (0.0, 0.3, 0.9, 1.5, 1.99):
        f = compose_flow(v, death + dt, 48, 6)
        assert rows(f)[-1] == "Each reading ta"
        assert f.cursor is not None and f.cursor.mode == "on" and f.cursor.col == 15
        assert f.card is None
    assert v.next_change(death + 0.5) >= death + 2.0  # nothing moves


def test_two_seconds_of_stillness_then_the_vigil_and_the_small_death_card() -> None:
    v, death = _dying()
    f = compose_flow(v, death + 2.0, 48, 6)
    assert f.card is not None and f.card[0] == "vigil"
    assert f.card[1] == ["Each reading ta", "life 12 · 29:30"]
    assert f.card_shown == [15, 0] and f.cursor is None and f.status is None
    f = compose_flow(v, death + 2.0 + 1.5 + 5.0, 48, 6)
    assert f.card_shown == [15, len("life 12 · 29:30")]


def test_the_vigil_fades_slowly_and_holds_until_the_genesis() -> None:
    v, death = _dying()
    start = death + 2.0
    levels = [v.vigil_level(start + s) for s in (0, 20, 40, 75, 200)]
    assert levels[0] == 1.0 and levels[3] == pytest.approx(0.3) and levels[4] == pytest.approx(0.3)
    assert levels[0] > levels[1] > levels[2] > levels[3]
    # the next model loads: still the vigil, no birth card
    v.handle(ev("birth_loading", 13, model="m", quant="Q4_K_M", reveal="stream"), death + 90)
    f = compose_flow(v, death + 100, 48, 6)
    assert f.card is not None and f.card[0] == "vigil" and v.life == 13
    v.handle(ev("birth", 13, model="m", t=0.0), death + 150)
    assert compose_flow(v, death + 170, 48, 6).card[0] == "vigil"  # type: ignore[index]
    # the genesis: the birth reading replaces it
    v.handle(ev("reading", 13, turn=1, text="[host] t+00:58 · boot complete", t=58.0), death + 210)
    f = compose_flow(v, death + 212, 48, 6)
    assert f.card is None and list(f.machine_rows().values()) == ["t+00:58", "boot complete"]


def test_the_vigil_line_is_the_end_of_what_it_showed() -> None:
    assert vigil_line("one two three", 48) == "one two three"
    last = "I will end here, in this small computer. Each reading take"
    assert vigil_line(last, 30) == "computer. Each reading take"  # from a word start
    assert vigil_line(last, 33) == "small computer. Each reading take"  # exactly fits
    assert len(vigil_line("a" * 100, 20)) == 20


def test_a_non_stream_life_keeps_the_old_death() -> None:
    v = LifeView()
    v.handle(ev("birth_loading", model="m"), 0.0)
    v.handle(ev("birth", model="m"), 0.0)
    v.handle(word(1, 0, "slow", 1000), 0.0)
    v.handle(ev("death", cause="oom", lived_s=10), 1.0)
    assert v.frozen_at is None
    assert v.cursor(1.5) == "hidden"


# -- the snapshot ------------------------------------------------------------------------------


def _same_screen(a: LifeView, b: LifeView, now: float) -> None:
    fa, fb = compose_flow(a, now, 48, 10), compose_flow(b, now, 48, 10)
    assert rows(fa) == rows(fb)
    assert fa.card == fb.card and fa.card_shown == fb.card_shown
    assert fa.brightness == pytest.approx(fb.brightness)
    assert fa.card_level == pytest.approx(fb.card_level)
    assert (fa.cursor is None) == (fb.cursor is None)


def test_a_snapshot_carries_readings_darkness_and_dissolving() -> None:
    v = _forget_scene()
    v.handle(ev("vitals", t=900.0), 11.0)
    v.handle(ev("world", action="screen:30", performed=True, state={}), 11.0)
    v.handle(ev("reading", turn=3, text='[host] forgotten: "I am here, inside the…"'), 12.0)
    now = 12.4
    snap = v.snapshot(now)
    assert snap["machine"] and snap["screen"]["to"] == 0.3 and snap["reveal"] == "stream"
    other = LifeView()
    other.handle(snap, now)
    for dt in (2.0, 5.0, 30.0):  # once the reading is typed on the original too
        _same_screen(v, other, now + dt)


def test_a_snapshot_carries_the_frozen_death_and_the_vigil() -> None:
    v, death = _dying()
    for at in (death + 1.0, death + 30.0):
        other = LifeView()
        other.handle(v.snapshot(at), at)
        for dt in (0.0, 1.5, 3.0, 80.0):
            _same_screen(v, other, at + dt)
    # across the next birth too
    v.handle(ev("birth_loading", 13, model="m"), death + 90)
    other = LifeView()
    other.handle(v.snapshot(death + 95), death + 95)
    _same_screen(v, other, death + 100)


# -- the recorded fake life --------------------------------------------------------------------


def _frames(events: list[Event], step: float = 0.5, view: LifeView | None = None):  # type: ignore[no-untyped-def]
    keys = replay.timeline(events)
    view = view or LifeView()
    n = 0
    t = 0.0
    end = keys[-1] + 120.0
    while t <= end:
        while n < len(events) and keys[n] <= t:
            view.handle(events[n], keys[n])
            n += 1
        yield t, view, compose_flow(view, t, 48, 12)
        t += step


def _blank(f: Frame) -> bool:
    return f.dark or (not f.spans and f.card is None and (f.cursor is None))


def test_no_blank_stretch_over_five_seconds_from_the_vigil_to_death(
    dread_life: list[Event],
) -> None:
    keys = replay.timeline(dread_life)
    deaths = [k for e, k in zip(dread_life, keys, strict=True) if e["type"] == "death"]
    longest, since, vigil_seen = 0.0, None, False
    for t, _view, f in _frames(dread_life):
        if t > deaths[1]:
            break
        if f.card is not None and f.card[0] == "vigil":
            vigil_seen = True
        if not vigil_seen:
            continue
        if _blank(f):
            since = t if since is None else since
            longest = max(longest, t - since)
        else:
            since = None
    assert vigil_seen
    assert longest <= 5.0


def test_readings_come_before_their_thoughts_in_the_recorded_life(
    dread_life: list[Event],
) -> None:
    keys = replay.timeline(dread_life)
    view = LifeView()
    for e, k in zip(dread_life, keys, strict=True):
        view.handle(e, k)
        if e["type"] == "death":
            break
    seen = 0
    for n, th in enumerate(view.thoughts):
        if th.voice != "machine":
            continue
        after = view.thoughts[n + 1] if n + 1 < len(view.thoughts) else None
        if after is None or after.voice == "machine":
            continue
        seen += 1
        assert after.turn == th.turn
        if after.words:
            assert after.words[0].start >= th.words[-1].end - 1e-9
    assert seen >= 10


def test_the_recorded_life_dims_then_dies_frozen(dread_life: list[Event]) -> None:
    out = {}
    for t, view, f in _frames(dread_life, step=1.0):
        lt = view.life_t(t)
        if view.life == 1 and lt is not None:
            b = PLAIN.brightness(f.brightness, f.contrast_floor)
            if f.card is None and not f.dark and lt < 1620:
                assert contrast_ratio(PLAIN.lit(PLAIN.forgotten, b), PLAIN.bg) >= 7.0
            elif f.card is None and not f.dark and lt < 1740:
                assert contrast_ratio(PLAIN.lit(PLAIN.forgotten, b), PLAIN.bg) >= 4.5
            if b < 1.0:
                out.setdefault("dimmed", lt)
        if view.frozen_at is not None and view.life == 1:
            out.setdefault("frozen", t)
        if view.life == 2:
            break
    assert out.get("dimmed") and out.get("frozen")


@pytest.mark.parametrize(
    "theme,size",
    [
        (PLAIN, (800, 480)),
        (PLAIN, (1280, 720)),
        (PLAIN, (1920, 1080)),
        (PLAIN, (1080, 1920)),
        (SEGMENT16, (800, 480)),
    ],
    ids=lambda x: getattr(x, "name", None) or f"{x[0]}x{x[1]}",
)
def test_the_dread_life_renders_at_the_d13_sizes(
    theme: Any, size: tuple[int, int], dread_life: list[Event], tmp_path: Path
) -> None:
    """Frames at the D13 sizes: a reading with its thought, the darkened end, the frozen
    death, the vigil, the genesis. Model text keeps its floor on the pixels; readings and
    the vigil are drawn."""
    pytest.importorskip("pygame")
    from epitaph.display import screenshot as shot

    keys = replay.timeline(dread_life)
    reading = [k for e, k in zip(dread_life, keys, strict=True) if e["type"] == "reading"][3]
    death = next(k for e, k in zip(dread_life, keys, strict=True) if e["type"] == "death")
    moments = {
        "reading": reading + 3.0,
        "dark": death - 60.0,
        "frozen": death + 1.0,
        "vigil": death + 10.0,
        "loading": death + 120.0,
    }
    for name, at in moments.items():
        view = LifeView(ViewSettings())
        for e, k in zip(dread_life, keys, strict=True):
            if k > at:
                break
            view.handle(e, k)
        path = tmp_path / f"{name}.png"
        frame = shot.render_png(path, size, view=view, now=at, theme=theme)
        if name in ("reading", "dark") and any(s.kind in ("live", "fading") for s in frame.spans):
            contrasts = shot.text_contrasts(path, frame, theme)
            model = {
                k: c
                for k, c in contrasts.items()
                if tuple(int(x) for x in k.split(",")) != theme.lit(theme.machine, 1.0)
            }
            floor = frame.contrast_floor
            assert model and min(model.values()) >= floor - 0.05, (name, model)
            assert frame.split_words == 0
        if name == "reading":
            assert any(s.kind == "machine" for s in frame.spans)
        if name in ("vigil", "loading"):
            assert frame.card is not None and frame.card[0] == "vigil"
            assert shot.measure_contrast(path)["ratio"] > 1.0  # something is drawn


@pytest.mark.tesseract
@pytest.mark.parametrize("size", [(800, 480), (1280, 720), (1920, 1080), (1080, 1920)])
def test_model_text_reads_back_with_readings_on_screen(
    size: tuple[int, int], dread_life: list[Event], tmp_path: Path
) -> None:
    pytest.importorskip("pygame")
    from epitaph.display import screenshot as shot

    keys = replay.timeline(dread_life)
    at = next(k for e, k in zip(dread_life, keys, strict=True) if e["type"] == "forget") + 60.0
    view = LifeView(ViewSettings())
    for e, k in zip(dread_life, keys, strict=True):
        if k > at:
            break
        view.handle(e, k)
    path = tmp_path / "read.png"
    frame = shot.render_png(path, size, view=view, now=at)
    shown = shot.shown_words(frame)
    assert len(shown) >= 10
    assert shot.word_accuracy(shown, shot.ocr_words(path)) >= 0.95


def test_the_dread_life_renders_on_the_grid_and_in_a_terminal(dread_life: list[Event]) -> None:
    out = io.StringIO()
    d = TerminalDriver(out=out, size=(60, 16), color="truecolor", alt_screen=False)
    d.open()
    keys = replay.timeline(dread_life)
    seen: set[str] = set()
    n = 0
    t = 0.0
    while t < keys[-1] + 100:
        while n < len(dread_life) and keys[n] <= t:
            d.view.handle(dread_life[n], keys[n])
            n += 1
        d.render(t)
        f = d.last_frame
        assert f is not None
        if f.card is not None:
            seen.add(f.card[0])
        if any(s.kind == "machine" for s in f.spans):
            seen.add("machine")
        g = compose_grid(d.view, t, 6, 16, "segment16")
        assert all(len(s.text) <= 16 for s in g.spans)
        t += 2.0
    d.close()
    assert {"vigil", "machine"} <= seen


def test_replay_plays_the_dread_life_and_starts_mid_life_from_a_snapshot(
    dread_life: list[Event], monkeypatch: pytest.MonkeyPatch
) -> None:
    import epitaph.display.app as app

    drivers: list[TerminalDriver] = []

    def make(name: str, cfg: dict[str, Any], **opts: Any) -> TerminalDriver:
        d = TerminalDriver(out=io.StringIO(), size=(50, 12), color="none", alt_screen=False)
        drivers.append(d)
        return d

    monkeypatch.setattr(app, "make_driver", make)
    args = [str(DREAD_LIFE), "--speed", "10000", "--max-gap", "0.001"]
    assert replay.main(args) == 0
    view = drivers[-1].view
    assert view.mode == "silence" and view.vigil is not None and view.frozen_at is not None
    assert replay.main([*args, "--from", "28:00"]) == 0
    assert drivers[-1].view.vigil is not None


def test_settings_read_the_display_config() -> None:
    from epitaph.display.app import display_config

    s = ViewSettings.from_config(display_config("pi4-4gb"))
    assert s.silence_style == "vigil" and s.machine_voice and s.machine_char_ms == 30
    assert s.contrast_floors == ((1620.0, 7.0), (1740.0, 4.5))
    assert s.pulse_from_s == 1320.0 and s.pulse_fast_s == 0.5
    assert s.death_style == "auto" and s.death_still_s == 2.0 and s.vigil_fade_s == 75.0
