"""D1: the display model and layout."""

from __future__ import annotations

import itertools
import random
from typing import Any

import pytest

from epitaph.display.layout import (
    LifeView,
    ViewSettings,
    ViewWord,
    char_ms_for,
    compose_flow,
    compose_grid,
    derive_grid,
    fit_status,
    flow_lines,
    flow_metrics,
    gauge_bar,
    map_charset,
)


def ev(etype: str, life: int = 1, **f: Any) -> dict[str, Any]:
    return {"v": 1, "ts": 0.0, "life": life, "type": etype, **f}


def word(turn: int, i: int, text: str, ms: int = 100, pause: int = 0, **f: Any) -> dict[str, Any]:
    return ev(
        "word", turn=turn, i=i, text=text, char_ms=[ms] * len(text), pause_after_ms=pause, **f
    )


def born(view: LifeView, now: float = 0.0) -> None:
    view.handle(ev("birth_loading", model="m", quant="Q6_K"), now)
    view.handle(ev("birth", model="m", quant="Q6_K"), now)


def thought(view: LifeView, turn: int, text: str, now: float, ms: int = 0, pause: int = 0) -> None:
    view.handle(ev("thought_start", turn=turn), now)
    for i, w in enumerate(text.split()):
        view.handle(word(turn, i, w, ms, pause), now)
    view.handle(ev("thought_end", turn=turn, text=text), now)


# -- words and typing ------------------------------------------------------------------------


def test_char_ms_padding_and_cutting() -> None:
    assert char_ms_for("abc", [10]) == (10, 10, 10)
    assert char_ms_for("ab", [10, 20, 30]) == (10, 20)
    assert char_ms_for("ab", None) == (60, 60)
    assert char_ms_for("ab", [-5, 7]) == (0, 7)


def test_letters_appear_with_char_ms() -> None:
    w = ViewWord(1, 0, "abc", (100, 200, 300), 500, start=10.0)
    assert [w.shown(t) for t in (9.99, 10.0, 10.099, 10.1, 10.3, 10.6)] == [0, 1, 1, 2, 3, 3]
    assert w.end == pytest.approx(10.6)
    assert w.tail == pytest.approx(11.1)


def test_words_queue_one_after_another_with_pauses_and_hesitation() -> None:
    v = LifeView()
    born(v)
    v.handle(word(1, 0, "ab", 100, pause=250), 0.0)
    v.handle(word(1, 1, "cd", 100, hesitate_before_ms=400), 0.0)
    a, b = list(v.words())
    assert a.start == 0.0
    assert b.start == pytest.approx(0.2 + 0.25 + 0.4)
    assert v.tail == pytest.approx(b.end)
    # a word arriving after the queue emptied starts at once
    v.handle(word(1, 2, "ef", 100), 10.0)
    assert list(v.words())[-1].start == 10.0


def test_backlog_catches_up() -> None:
    v = LifeView(ViewSettings(max_backlog_s=1.0))
    born(v)
    for i in range(20):
        v.handle(word(1, i, "abcdef", 100), 0.0)
    assert v.tail <= 1.0
    assert all(w.shown(1.0) == len(w.text) for w in v.words())


def test_history_is_bounded() -> None:
    v = LifeView(ViewSettings(max_words=10))
    born(v)
    for turn in range(1, 8):
        thought(v, turn, "one two three", 0.0)
    assert sum(1 for _ in v.words()) <= 12
    assert v.thoughts[-1].turn == 7


# -- whole-word wrapping --------------------------------------------------------------------


def test_flow_lines_never_split_a_word_that_fits() -> None:
    rng = random.Random(4)
    for cols in (8, 16, 33, 48):
        for _ in range(30):
            thoughts = []
            for turn in range(rng.randint(1, 4)):
                ws = ["x" * rng.randint(1, cols) for _ in range(rng.randint(1, 25))]
                thoughts.append([(ViewWord(turn, i, w, ()), w) for i, w in enumerate(ws)])
            placed, nlines, _ = flow_lines(thoughts, cols)
            assert not any(p.split for p in placed)
            assert all(p.text == p.word.text for p in placed)
            by_line: dict[int, list[Any]] = {}
            for p in placed:
                by_line.setdefault(p.line, []).append(p)
                assert p.col + len(p.text) <= cols
            for items in by_line.values():  # words on one line do not overlap
                items.sort(key=lambda p: p.col)
                for a, b in itertools.pairwise(items):
                    assert a.col + len(a.text) < b.col
            assert nlines == max(p.line for p in placed) + 1


def test_blank_line_between_thoughts() -> None:
    a = [(ViewWord(1, 0, "one", ()), "one")]
    b = [(ViewWord(2, 0, "two", ()), "two")]
    placed, nlines, after = flow_lines([a, b], 10)
    assert [p.line for p in placed] == [0, 2]
    assert nlines == 3 and after == (2, 3)
    placed, _, _ = flow_lines([a, b], 10, blank_between=False)
    assert [p.line for p in placed] == [0, 1]
    assert flow_lines([], 10)[1] == 0


def test_word_longer_than_a_line_is_the_only_split() -> None:
    w = ViewWord(1, 0, "abcdefghijkl", ())
    placed, nlines, _ = flow_lines([[(ViewWord(1, 1, "hi", ()), "hi"), (w, w.text)]], 5)
    pieces = [p for p in placed if p.word is w]
    # it starts on a fresh line, then fills whole lines
    assert [p.text for p in pieces] == ["abcde", "fghij", "kl"]
    assert [p.offset for p in pieces] == [0, 5, 10]
    assert all(p.split for p in pieces)
    assert nlines == 4


# -- flow frames ------------------------------------------------------------------------------


def test_newest_text_at_the_bottom_and_scrolls() -> None:
    v = LifeView()
    born(v)
    for turn in range(1, 6):
        thought(v, turn, f"thought number {turn} is here", 0.0)
    f = compose_flow(v, 100.0, 20, 4)
    rows = f.text_rows()
    assert rows[-2:] == ["thought number 5 is", "here"]
    assert len(rows) == 4
    assert f.bright_words == sum(1 for s in f.spans if s.kind == "live")
    assert f.split_words == 0


def test_short_text_is_bottom_anchored() -> None:
    v = LifeView()
    born(v)
    thought(v, 1, "hello", 0.0)
    rows = compose_flow(v, 100.0, 20, 5).text_rows()
    assert rows == ["", "", "", "", "hello"]


def test_typing_mid_word_and_cursor() -> None:
    v = LifeView()
    born(v)
    v.handle(word(1, 0, "hello", 100, pause=1000), 0.0)
    f = compose_flow(v, 0.25, 20, 3)
    assert f.text_rows()[-1] == "hel"
    assert f.cursor is not None and (f.cursor.col, f.cursor.mode) == (3, "on")
    # in the pause the cursor rests after the word and blinks
    f = compose_flow(v, 0.6, 20, 3)
    assert f.text_rows()[-1] == "hello"
    assert f.cursor is not None and f.cursor.col == 5 and f.cursor.mode == "on"
    assert v.cursor(0.5 + 0.6) == "off"
    assert v.cursor(0.5 + 1.1) == "on"


def test_cursor_wraps_to_next_line_when_line_full() -> None:
    v = LifeView()
    born(v)
    v.handle(word(1, 0, "abcd", 0), 0.0)
    f = compose_flow(v, 1.0, 4, 3)
    assert f.cursor is not None and f.cursor.col == 0 and f.cursor.row == 2
    assert f.text_rows()[1] == "abcd"


def test_cursor_on_split_word_piece() -> None:
    v = LifeView()
    born(v)
    v.handle(word(1, 0, "abcdefgh", 100), 0.0)
    f = compose_flow(v, 0.55, 5, 3)
    assert f.text_rows()[-2:] == ["abcde", "f"]
    assert f.cursor is not None and f.cursor.col == 1
    assert f.split_words == 1


def test_cursor_modes_through_a_life() -> None:
    v = LifeView()
    assert v.cursor(0) == "hidden"
    v.handle(ev("birth_loading", model="m"), 0)
    assert v.cursor(0) == "hidden"
    v.handle(ev("birth", model="m"), 1)
    assert v.cursor(1) == "on"
    v.handle(ev("reload", **{"from": "Q6_K", "to": "Q4_K_M"}), 2)
    assert v.cursor(2) == "dim" and not v.dimmed()  # the cursor dims; the text stays readable
    f = compose_flow(v, 2.0, 20, 3)
    assert not f.dim and f.cursor is not None and f.cursor.mode == "dim"
    v.s.reload_dim_text = True
    assert v.dimmed() and compose_flow(v, 2.0, 20, 3).dim
    v.s.reload_dim_text = False
    v.handle(ev("reload_done", seconds=50), 3)
    assert v.cursor(3) != "dim" and not v.dimmed() and v.quant == "Q4_K_M"
    v.handle(ev("reload", **{"from": "Q4_K_M", "to": "Q2_K"}), 4)
    v.handle(ev("reload_skipped", skipped=True), 5)
    assert v.mode == "living"
    v.handle(ev("death", cause="oom", lived_s=3570.2, model="m"), 6)
    assert v.cursor(6) == "hidden"
    assert compose_flow(v, 6.0, 20, 3).cursor is None


def test_forget_fades_then_forgotten() -> None:
    v = LifeView(ViewSettings(fade_s=8))
    born(v)
    thought(v, 1, "one two three", 0.0)
    thought(v, 2, "four five", 0.0)
    v.handle(ev("forget", items=[{"turn": 1, "upto_i": 1}]), 10.0)
    states = [w.state_at(10.0, 8) for w in v.words()]
    assert states == ["fading", "fading", "live", "live", "live"]
    f = compose_flow(v, 14.0, 40, 4)
    fading = [s for s in f.spans if s.kind == "fading"]
    assert [s.fade for s in fading] == [0.5, 0.5]
    assert v.bright_words(14.0) == 3
    v.handle(ev("forget", items=[{"turn": 1, "all": True}, {"turn": 99, "all": True}]), 12.0)
    assert [w.state_at(19.0, 8) for w in v.words()][:3] == ["forgotten", "forgotten", "fading"]
    assert [v.word_state(w, 19.0)[0] for w in v.words()][:3] == ["forgotten"] * 2 + ["fading"]
    # once faded, forgotten words are gone; the thought that held them leaves the screen
    f = compose_flow(v, 30.0, 40, 4)
    assert [s.kind for s in f.spans] == ["live", "live"]
    assert f.text_rows() == ["", "", "", "four five"]
    assert next(iter(v.words())).fade_at(30.0, 0) == 1.0


def test_a_partly_forgotten_thought_keeps_its_holes() -> None:
    """Forgotten words leave an empty place, so the words kept never move."""
    v = LifeView(ViewSettings(fade_s=2))
    born(v)
    thought(v, 1, "one two three", 0.0)
    v.handle(ev("forget", items=[{"turn": 1, "upto_i": 0}]), 1.0)
    assert compose_flow(v, 2.0, 20, 1).text_rows() == ["one two three"]
    assert compose_flow(v, 3.0, 20, 1).text_rows() == ["    two three"]
    assert [s.kind for s in compose_flow(v, 2.0, 20, 1).spans] == ["fading", "live", "live"]


def test_inherited_words_keep_their_state() -> None:
    v = LifeView()
    born(v)
    v.handle(word(1, 0, "before", 0, state="inherited"), 0.0)
    f = compose_flow(v, 1.0, 20, 2)
    assert f.spans[0].kind == "inherited"
    assert v.bright_words(1.0) == 0


# -- cards, silence, status ------------------------------------------------------------------


def test_birth_card_until_first_word() -> None:
    v = LifeView(ViewSettings(birth_card_s=4, reveal_life_number=True))
    v.handle(ev("birth_loading", life=7, model="llama", quant="Q6_K"), 0)
    f = compose_flow(v, 0, 20, 3)
    assert f.card is not None and f.card[0] == "birth" and f.card[1][0] == "life 7"
    assert f.card[1] == ["life 7", "llama · Q6_K", "waking"]
    assert f.card_shown == [1, 0, 0]  # typed letter by letter
    assert compose_flow(v, 30, 20, 3).card_shown == [6, 12, 6]
    v.handle(ev("birth", life=7, model="llama", quant="Q6_K"), 10)
    card = v.card(11)
    assert card is not None and card.lines[-1] == "waking" and card.start == 0
    assert v.card(15) is None
    v.handle(word(1, 0, "hi", 0), 12)
    assert v.card(12) is None
    assert LifeView(ViewSettings(birth_card=False)).card(0) is None


def test_death_card_after_the_last_letter_then_dark() -> None:
    v = LifeView(ViewSettings(death_card_s=8))
    born(v)
    v.handle(word(1, 0, "last", 1000), 0.0)  # typed until 4.0
    v.handle(ev("death", cause="oom", lived_s=3570.4, model="m"), 1.0)
    v.handle(ev("death_shown", last_line="last", words_total=1), 1.0)
    v.handle(ev("silence", seconds=90, style="dark"), 1.0)
    assert v.card(2.0) is None and not v.dark(2.0)  # the last word is still typing
    # the last word fades for fade_s (8 s) after its last letter (4.0), then the card
    assert v.card(5.0) is None and compose_flow(v, 5.0, 20, 3).spans[0].kind == "fading"
    card = v.card(12.0)
    assert card is not None and list(card.lines) == ["lived 59:30", "its memory was taken"]
    assert card.start == 12.0 and card.shown(12.0) == [1, 0]
    # typed at the life's last cadence (1000 ms a letter), held 8 s after its last letter
    assert card.times[1] - card.times[0] == pytest.approx(1.0)
    assert v.card(card.end + 7.9) is not None
    assert v.card(card.end + 8.1) is None and v.dark(card.end + 8.1)
    f = compose_flow(v, card.end + 8.1, 20, 3)
    assert f.dark and not f.spans


def test_silence_styles() -> None:
    v = LifeView(ViewSettings(death_card_s=1))
    born(v)
    thought(v, 1, "words", 0.0)
    v.handle(ev("death", cause="weird"), 1)
    v.handle(ev("silence", seconds=90, style="death_card"), 1)
    card = v.card(50)
    assert card is not None and card.lines[-1] == "weird"
    v.s.silence_style = "last_words"
    assert v.card(50) is None and not v.dark(50)
    assert compose_flow(v, 50, 20, 2).text_rows()[-1] == "words"
    assert compose_flow(v, 50, 20, 2).spans[0].kind == "live"  # the last words never fade


def test_exhibit_closed_is_dark() -> None:
    v = LifeView()
    born(v)
    v.handle(ev("exhibit", open=False), 1)
    assert v.dark(1) and v.cursor(1) == "hidden"
    v.handle(ev("exhibit", open=True), 2)
    assert not v.dark(2)


def test_status_line_and_life_clock() -> None:
    v = LifeView()
    born(v)
    v.handle(
        ev(
            "vitals",
            t=61.0,
            phase="birth",
            health="nominal",
            recall=1280,
            recall_used=300,
            quant="Q6_K",
            cores_effective=3.0,
            tok_s=1.35,
            cpu_c=52.4,
        ),
        100.0,
    )
    v.handle(ev("erosion", groups_left=4, mechanics_present=True), 100.0)
    line = v.status_line(110.0)
    assert line.startswith("life 1 · 01:11 · nominal · memory 300/1280 · Q6_K")
    assert "cores 3.0" in line and "1.35 tok/s" in line and "52°C" in line and "persona 4/5" in line
    assert v.gauge() == pytest.approx(300 / 1280)
    v.handle(ev("reload", **{"from": "Q6_K", "to": "Q4_K_M"}), 111.0)
    assert "reloading Q6_K → Q4_K_M" in v.status_line(111.0)
    v.connected = False
    assert v.status_line(111.0).startswith("life 1 · reconnecting · 01:12")
    # regression (found on the Pi): a long strip dropped `reconnecting`, the last part, first
    assert fit_status(v.status_line(111.0), 30) == "life 1 · reconnecting · 01:12"
    v.handle(ev("death", cause="deadline", lived_s=3600), 112.0)
    assert "dead (deadline)" in v.status_line(200.0)
    assert v.life_t(200.0) == v.t_life
    assert LifeView().status_line(0) == "epitaph"
    v.handle(ev("error", where="backend", message="slow"), 1)
    assert v.last_error == "backend: slow"


def test_fit_status_drops_whole_parts() -> None:
    s = "life 1 · 01:11 · nominal · memory 300/1280"
    assert fit_status(s, 100) == s
    assert fit_status(s, 30) == "life 1 · 01:11 · nominal"
    assert fit_status("abcdefgh", 3) == "abc"


def test_gauge_needs_numbers() -> None:
    v = LifeView()
    assert v.gauge() is None
    v.vitals = {"recall": 0, "recall_used": 5}
    assert v.gauge() is None
    v.vitals = {"recall": 100, "recall_used": 500}
    assert v.gauge() == 1.0


# -- snapshots ------------------------------------------------------------------------------------


def test_snapshot_round_trip_redraws_the_same_screen() -> None:
    v = LifeView(ViewSettings(fade_s=8))
    born(v)
    v.handle(ev("vitals", t=30.0, health="stable", recall=1000, recall_used=400, quant="Q6_K"), 0.0)
    thought(v, 1, "the first thought is here", 0.0)
    thought(v, 2, "and a second one", 0.0)
    v.handle(ev("thought_start", turn=3), 0.0)
    v.handle(word(3, 0, "open", 0), 0.0)
    v.handle(ev("forget", items=[{"turn": 1, "all": True}]), 5.0)
    snap = v.snapshot(20.0, extra_field=1)
    assert snap["type"] == "snapshot" and snap["open_turn"] == 3 and snap["extra_field"] == 1
    assert snap["memory"] == {"recall": 1000, "recall_used": 400}
    assert {w["state"] for w in snap["words"]} == {"forgotten", "live"}

    w = LifeView(ViewSettings(fade_s=8))
    w.handle(snap, 100.0)
    a = compose_flow(v, 20.0, 30, 6)
    b = compose_flow(w, 100.0, 30, 6)
    assert a.text_rows() == b.text_rows()
    assert [s.kind for s in a.spans] == [s.kind for s in b.spans]
    assert w.status_line(100.0).split(" · ")[2:] == v.status_line(20.0).split(" · ")[2:]
    assert not w.thoughts[-1].ended and w.thoughts[0].ended


def test_snapshot_with_fading_and_inherited_words_and_odd_mode() -> None:
    w = LifeView(ViewSettings(fade_s=8))
    w.handle(
        ev(
            "snapshot",
            life=4,
            mode="bogus",
            words=[
                {"turn": 0, "i": 0, "text": "old", "state": "inherited"},
                {"turn": 1, "i": 0, "text": "gone", "state": "fading"},
            ],
        ),
        10.0,
    )
    assert w.mode == "living" and w.life == 4
    assert [x.state_at(10.0, 8) for x in w.words()] == ["inherited", "fading"]
    empty = LifeView()
    empty.handle(ev("snapshot", life=2), 0.0)
    assert empty.mode == "empty" and empty.cursor(0) == "hidden"


def test_a_new_life_resets_the_view() -> None:
    v = LifeView()
    born(v)
    thought(v, 1, "old life", 0.0)
    v.handle(word(1, 0, "new", 0, life=2), 5.0)
    assert v.life == 2 and [w.text for w in v.words()] == ["new"]
    v.handle(ev("unknown_future_event", life=2), 6.0)  # ignored


# -- geometry and grids --------------------------------------------------------------------


@pytest.mark.parametrize("size", [(800, 480), (1280, 720), (1920, 1080), (1080, 1920)])
def test_flow_metrics_any_resolution(size: tuple[int, int]) -> None:
    m = flow_metrics(*size, line_chars=48, min_font_px=36)
    assert m.font_px >= 36
    assert 1 <= m.cols <= 48
    assert m.cols * m.cell_w <= size[0] - 2 * m.margin_x + 1e-6
    assert m.margin_y * 2 + m.strip_h + m.rows * m.line_h <= size[1] + 1e-6
    assert m.rows >= 3


def test_flow_metrics_font_from_width_and_floor() -> None:
    wide = flow_metrics(1920, 1080, 48, 36)
    assert wide.cols == 48 and wide.font_px == 60
    narrow = flow_metrics(800, 480, 48, 36)
    assert narrow.font_px == 36 and narrow.cols < 48
    tiny = flow_metrics(40, 30, 48, 36, status_strip=False)
    assert tiny.cols == 1 and tiny.rows == 1 and tiny.strip_h == 0
    with pytest.raises(ValueError):
        flow_metrics(0, 10)


def test_derive_grid_for_small_panels() -> None:
    assert derive_grid(None, None, (6, 16)) == (6, 16)
    assert derive_grid(1920, 1080, (6, 16), 36) == (6, 16)
    assert derive_grid(320, 240, (6, 16), 36) == (5, 14)
    assert derive_grid(10, 10, (6, 16), 36) == (1, 1)


def test_charset_maps() -> None:
    s = "“Déjà vu” — 52°C… naïve"
    assert map_charset(s, "unicode") == s
    assert map_charset(s, "ascii") == '"Deja vu" - 52*C... naive'
    assert map_charset(s, "segment16") == '"DEJA VU" - 52*C... NAIVE'
    assert map_charset("日本", "ascii") == "??"
    assert map_charset("é€", "segment16") == "E?"


def test_grid_shows_live_words_and_a_gauge() -> None:
    v = LifeView()
    born(v)
    v.handle(ev("vitals", recall=100, recall_used=50), 0.0)
    thought(v, 1, "forgotten words", 0.0)
    thought(v, 2, "Naïve café lives", 0.0)
    v.handle(ev("forget", items=[{"turn": 1, "all": True}]), 1.0)
    f = compose_grid(v, 2.0, 4, 8, "segment16")
    rows = f.text_rows()
    assert rows[:3] == ["NAIVE", "CAFE", "LIVES"]
    assert rows[3] == "####----"
    assert "FORGOTTEN" not in "".join(rows)
    g = compose_grid(v, 2.0, 2, 16, "unicode", gauge_row=True)  # too few rows for a gauge
    assert g.rows == 2 and all(s.kind != "gauge" for s in g.spans)
    assert gauge_bar(None, 4) == "░░░░"


def test_grid_card_is_mapped_and_wrapped() -> None:
    v = LifeView(ViewSettings(reveal_life_number=True))
    v.handle(ev("birth_loading", life=3, model="naïve-model-with-a-long-name", quant="Q4"), 0)
    f = compose_grid(v, 60, 6, 16, "segment16")
    assert f.card is not None and f.card[1][0] == "LIFE 3"
    assert f.card[1][1:] == ["NAIVE-MODEL-WITH", "-A-LONG-NAME .", "Q4", "WAKING"]
    assert f.card_shown == [len(x) for x in f.card[1]]
    early = compose_grid(v, 1.0, 6, 16, "segment16")  # typing: "life 3" done, then the model
    assert early.card_shown is not None and early.card_shown[0] == 6
    assert early.card_shown[-1] == 0


# -- a whole simulated life ------------------------------------------------------------------------


def test_a_simulated_life_plays_through_the_view(sim_events: list[dict[str, Any]]) -> None:
    v = LifeView()
    words = 0
    split = 0
    for e in sim_events:
        now = float(e.get("t", 0.0))
        v.handle(e, now)
        if e["type"] == "word":
            words += 1
            f = compose_flow(v, now + 0.5, 48, 10)
            split += f.split_words
    assert words > 100
    assert split == 0
    assert v.mode == "silence" and v.death.get("cause") == "oom"
    last_t = float(sim_events[-1].get("t", 0.0))
    if v.s.silence_style == "vigil":  # the installation's silence: the vigil holds
        card = v.card(last_t + 1e4)
        assert card is not None and card.kind == "vigil" and not v.dark(last_t + 1e4)
    else:
        assert v.card(last_t + 1e4) is None and v.dark(last_t + 1e4)


def test_only_visible_thoughts_are_laid_out_and_the_screen_is_the_same() -> None:
    """compose_flow wraps from the newest thought back; it must match a full layout."""
    rng = random.Random(7)
    v = LifeView()
    born(v)
    for turn in range(1, 40):
        text = " ".join("x" * rng.randint(1, 9) for _ in range(rng.randint(1, 30)))
        thought(v, turn, text, 0.0)
    for cols, rows in ((48, 11), (20, 30), (33, 3)):
        seqs = [[(w, w.text) for w in th.words] for th in v.thoughts]
        placed, nlines, _ = flow_lines(seqs, cols)
        full = [[" "] * cols for _ in range(nlines)]
        for p in placed:
            full[p.line][p.col : p.col + len(p.text)] = list(p.text)
        want = ["".join(r).rstrip() for r in full][-rows:]
        f = compose_flow(v, 1e6, cols, rows)
        got = f.text_rows()
        # the cursor may sit on a virtual line under the text
        assert got in (want, [*want[1:], ""]), (cols, rows)


# -- when to draw next ------------------------------------------------------------------


def test_next_change_is_the_next_letter_then_the_cursor_blink() -> None:
    import math

    from epitaph.display.app import next_frame

    v = LifeView(ViewSettings(blink_s=0.5, birth_card=False))
    born(v)
    v.handle(word(1, 0, "abc", 100, pause=1000), 1.0)
    assert v.next_change(0.5) == pytest.approx(1.0)  # the first letter
    assert v.next_change(1.05) == pytest.approx(1.1)  # the second
    assert v.next_change(1.25) == pytest.approx(1.3)  # typing stops: the cursor starts blinking
    assert v.next_change(1.4) == pytest.approx(1.8)  # blink: 0.5 s after the last letter
    assert next_frame(v, 1.05, fps=30) == pytest.approx(1.1)
    assert next_frame(v, 1.09, fps=30) == pytest.approx(1.09 + 1 / 30)  # never above fps
    assert next_frame(v, 1.4, fps=30, max_idle_s=0.25) == pytest.approx(1.65)  # at least 4/s
    v.handle(ev("death", cause="oom"), 5.0)
    assert v.next_change(5.0) == math.inf  # no cursor, nothing typed: only the idle redraw


def test_closed_hours_outlive_a_new_life_and_a_reconnect() -> None:
    """The screen stays dark across a birth and through a snapshot."""
    v = LifeView()
    v.handle({"type": "exhibit", "life": 1, "open": False}, 0.0)
    v.handle({"type": "birth_loading", "life": 2}, 1.0)
    assert v.exhibit_open is False
    snap = v.snapshot(2.0)
    assert snap["exhibit_open"] is False
    fresh = LifeView()
    fresh.handle(snap, 3.0)
    assert fresh.exhibit_open is False
    v.handle({"type": "exhibit", "life": 2, "open": True}, 4.0)
    assert "exhibit_open" not in v.snapshot(5.0)
