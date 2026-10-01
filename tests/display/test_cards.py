"""D7: cards, the death fade, silence styles and redraw from a snapshot (BUILD_PLAN 5.12)."""

from __future__ import annotations

import math
from typing import Any

import pytest

from epitaph.display import cards
from epitaph.display.app import display_config, make_driver
from epitaph.display.layout import LifeView, ViewSettings, compose_flow, compose_grid
from epitaph.display.themes import PLAIN, contrast_ratio

from .test_layout import born, ev, thought, word

STYLE = cards.CardStyle(char_ms=100, word_gap_ms=300, line_pause_ms=700)


# -- what the cards say --------------------------------------------------------------------


def test_birth_card_names_the_life_only_when_revealed() -> None:
    hidden = cards.CardStyle()
    assert cards.birth_lines(7, "qwen", "Q4_K_M", hidden) == ["qwen · Q4_K_M", "waking"]
    shown = cards.CardStyle(reveal_life_number=True, show_model=False)
    assert cards.birth_lines(7, "qwen", "Q4_K_M", shown) == ["life 7", "waking"]
    assert cards.birth_lines(0, "", "", shown) == ["waking"]


def test_death_card_says_how_long_and_how() -> None:
    assert cards.death_lines(3, 1770.9, "oom", cards.CardStyle()) == [
        "lived 29:30",
        "its memory was taken",
    ]
    revealed = cards.CardStyle(reveal_life_number=True)
    assert cards.death_lines(3, None, "odd", revealed) == ["life 3", "odd"]
    assert cards.death_lines(0, None, "", revealed) == ["ended"]


# -- the reveal rhythm -----------------------------------------------------------------------


def test_cards_are_typed_with_the_reveal_rhythm() -> None:
    card = cards.type_card("birth", ["ab c", "d"], 10.0, STYLE)
    # letters every 100 ms, a word gap of 300 ms after the space, 700 ms between lines
    assert card.times == pytest.approx((10.0, 10.1, 10.2, 10.5, 10.6, 11.3))
    assert card.flat == "ab c\nd"
    assert card.shown(9.0) == [0, 0]
    assert card.shown(10.0) == [1, 0]
    assert card.shown(10.55) == [4, 0]
    assert card.shown(11.0) == [4, 0]  # the newline has appeared, the next line waits
    assert card.shown(11.3) == [4, 1]
    assert card.end == pytest.approx(11.3)
    assert card.next_change(10.05) == pytest.approx(10.1)
    assert card.next_change(12.0) == math.inf
    slow = cards.type_card("death", ["ab"], 0.0, STYLE, char_ms=720)
    assert slow.times == pytest.approx((0.0, 0.72))


def test_wrap_card_keeps_offsets_for_the_typing_count() -> None:
    pieces = cards.wrap_card(["lived 29:30", "its memory was taken"], 10)
    assert pieces == [("lived", 0), ("29:30", 6), ("its memory", 12), ("was taken", 23)]
    flat = "lived 29:30\nits memory was taken"
    assert all(flat[off : off + len(t)] == t for t, off in pieces)
    assert cards.shown_per_piece(pieces, 14) == [5, 5, 2, 0]
    assert cards.wrap_card(["abcdefghij"], 4) == [("abcd", 0), ("efgh", 4), ("ij", 8)]


def test_the_death_card_types_at_the_last_cadence() -> None:
    assert cards.card_char_ms([(700, 720), (710,)], 165) == 710
    assert cards.card_char_ms([(0, 0)], 165) == 165  # caught up: no cadence left
    assert cards.card_char_ms([(60, 60)], 165) == 165  # never faster than the birth card


def test_idle_mark_wanders_deterministically() -> None:
    seen = {cards.idle_position(t, 6, 16, 4.0) for t in range(0, 400, 4)}
    assert len(seen) > 20 and all(0 <= r < 6 and 0 <= c < 16 for r, c in seen)
    assert cards.idle_position(1.0, 6, 16, 4.0) == cards.idle_position(3.9, 6, 16, 4.0)
    assert cards.idle_position(5.0, 6, 16, 4.0, seed=1) == cards.idle_position(5, 6, 16, 4, 1)


# -- cards in the view -------------------------------------------------------------------------


def test_birth_card_types_while_loading_and_names_what_config_allows() -> None:
    v = LifeView(ViewSettings(card_model=False))
    v.handle(ev("birth_loading", life=9, model="qwen", quant="Q4"), 0.0)
    f = compose_flow(v, 0.0, 30, 4)
    assert f.card == ("birth", ["waking"]) and f.card_shown == [1]
    assert v.next_change(0.0) == pytest.approx(0.165)  # the next letter of the card
    assert compose_flow(v, 5.0, 30, 4).card_shown == [6]
    assert compose_flow(v, 5.0, 30, 4).cursor is None


def test_death_fades_the_text_then_types_the_card_then_the_silence() -> None:
    v = LifeView(ViewSettings(fade_s=4.0, death_card_s=2.0))
    born(v)
    thought(v, 1, "first words", 0.0)
    v.handle(word(2, 0, "last", 500), 1.0)  # typed until 3.0
    v.handle(ev("death", cause="deadline", lived_s=1800), 1.5)
    v.handle(ev("death_shown", last_line="last"), 1.5)
    v.handle(ev("silence", seconds=90, style="dark"), 1.5)
    assert v.cursor(2.0) == "hidden"
    assert v.death_end() == pytest.approx(3.0) and v.death_fade_start() == pytest.approx(3.0)
    f = compose_flow(v, 5.0, 30, 6)
    assert {s.kind for s in f.spans} == {"fading"} and f.card is None
    # every fading colour stays at least 12:1 until the word is gone
    for s in f.spans:
        assert contrast_ratio(PLAIN.word(s.kind, s.fade), PLAIN.bg) >= 12
    assert not v.dark(6.9)
    card = v.card(7.0)
    assert card is not None and card.kind == "death" and card.start == pytest.approx(7.0)
    assert card.times[1] - card.times[0] == pytest.approx(0.5)  # the life's last cadence
    assert v.card(card.end + 1.9) is not None
    assert v.card(card.end + 2.1) is None and v.dark(card.end + 2.1)


def test_last_words_style_keeps_the_text_unfaded() -> None:
    v = LifeView(ViewSettings(fade_s=4.0, death_card_s=1.0, silence_style="last_words"))
    born(v)
    thought(v, 1, "stay here", 0.0)
    v.handle(ev("death", cause="oom"), 1.0)
    v.handle(ev("death_shown"), 1.0)
    v.handle(ev("silence", seconds=90, style="last_words"), 1.0)
    assert v.death_fade_start() is None
    assert v.card(1.0) is not None  # straight to the card
    f = compose_flow(v, 60.0, 30, 2)
    assert f.text_rows()[-1] == "stay here" and {s.kind for s in f.spans} == {"live"}


def test_idle_silence_draws_one_wandering_mark() -> None:
    v = LifeView(ViewSettings(fade_s=1.0, death_card_s=1.0, idle_step_s=2.0))
    born(v)
    thought(v, 1, "goodbye", 0.0)
    v.handle(ev("death", cause="oom"), 1.0)
    v.handle(ev("death_shown"), 1.0)
    v.handle(ev("silence", seconds=90, style="idle"), 1.0)
    assert not v.idle(1.5)  # still fading
    t = 30.0
    f = compose_flow(v, t, 20, 5)
    assert f.idle and not f.dark and f.status is None and f.cursor is None
    assert [(s.text, s.kind) for s in f.spans] == [(cards.IDLE_MARK, "idle")]
    marks = {(s.row, s.col) for k in range(40) for s in compose_flow(v, t + 2 * k, 20, 5).spans}
    assert len(marks) > 10
    g = compose_grid(v, t, 6, 16, "segment16")
    assert g.idle and [s.text for s in g.spans] == ["."]
    assert PLAIN.word("idle") == PLAIN.status


def test_the_grid_gauge_goes_at_death() -> None:
    v = LifeView(ViewSettings(fade_s=1.0))
    born(v)
    v.handle(ev("vitals", recall=100, recall_used=50), 0.0)
    thought(v, 1, "words", 0.0)
    assert any(s.kind == "gauge" for s in compose_grid(v, 0.5, 6, 16).spans)
    v.handle(ev("death", cause="oom"), 1.0)
    v.handle(ev("death_shown"), 1.0)
    assert not any(s.kind == "gauge" for s in compose_grid(v, 1.5, 6, 16).spans)


# -- redraw from a snapshot ------------------------------------------------------------------


def _same_screen(a: LifeView, at_a: float, b: LifeView, at_b: float) -> None:
    fa, fb = compose_flow(a, at_a, 30, 8), compose_flow(b, at_b, 30, 8)
    assert fa.text_rows() == fb.text_rows()
    assert [s.kind for s in fa.spans] == [s.kind for s in fb.spans]
    assert [s.fade for s in fa.spans] == pytest.approx([s.fade for s in fb.spans], abs=0.01)
    assert (fa.card, fa.card_shown, fa.dark, fa.idle) == (fb.card, fb.card_shown, fb.dark, fb.idle)
    # the cursor is in the same place and state (its blink phase restarts on a redraw)
    ca, cb = fa.cursor, fb.cursor
    assert (ca is None) == (cb is None)
    if ca is not None and cb is not None:
        assert (ca.row, ca.col, ca.mode == "dim") == (cb.row, cb.col, cb.mode == "dim")


@pytest.mark.parametrize("at", [20.0, 31.0, 36.0, 41.5, 44.0, 60.0])
def test_a_snapshot_redraws_the_same_screen(at: float) -> None:
    """Living, mid-reload with a fade, mid death fade, on the death card, and in the
    silence: a display that connects then draws what a display fed from the start draws."""
    events: list[tuple[float, dict[str, Any]]] = [
        (0.0, ev("birth_loading", model="m", quant="Q6_K")),
        (0.0, ev("birth", model="m", quant="Q6_K")),
        (1.0, ev("vitals", t=1.0, recall=100, recall_used=40, health="nominal")),
        (2.0, ev("thought_start", turn=1)),
        *[(2.0, word(1, i, w, 0)) for i, w in enumerate(["one", "two", "three"])],
        (3.0, ev("thought_end", turn=1)),
        *[(10.0, word(2, i, w, 0)) for i, w in enumerate(["four", "five", "six"])],
        (30.0, ev("reload", **{"from": "Q6_K", "to": "Q4_K_M"})),
        (30.0, ev("forget", items=[{"turn": 1, "all": True}])),
        (33.0, ev("reload_done", seconds=3)),
        (34.0, ev("erosion", groups_left=2, mechanics_present=True)),
        (35.0, ev("death", cause="oom", lived_s=35.0)),
        (35.0, ev("death_shown", last_line="six")),
        (35.0, ev("silence", seconds=90, style="dark")),
    ]
    settings = ViewSettings(fade_s=4.0, death_card_s=3.0)
    live = LifeView(settings)
    for t, e in events:
        if t <= at:
            live.handle(e, t)
    snap = live.snapshot(at)
    again = LifeView(ViewSettings(fade_s=4.0, death_card_s=3.0))
    again.handle(snap, 1000.0)  # any display clock
    for dt in (0.0, 0.7, 1.9):
        _same_screen(live, at + dt, again, 1000.0 + dt)
    assert again.status_line(1000.0).split(" · ")[2:] == live.status_line(at).split(" · ")[2:]


# -- config -----------------------------------------------------------------------------------


def test_card_and_silence_settings_from_config() -> None:
    s = ViewSettings.from_config(
        {
            "reveal_life_number": True,
            "birth_card_model": False,
            "card_char_ms": 200,
            "card_word_gap_ms": 100,
            "card_line_pause_ms": 900,
            "reload_dim_text": True,
            "death_fade": False,
            "idle_step_seconds": 2,
        }
    )
    assert s.card_style == cards.CardStyle(True, False, 200, 100, 900)
    assert (s.reload_dim_text, s.death_fade, s.idle_step_s) == (True, False, 2.0)


def test_display_config_brings_the_life_number_and_the_reveal_rhythm() -> None:
    cfg = display_config("dev")
    assert cfg["reveal_life_number"] is False
    assert cfg["card_word_gap_ms"] == 270 and cfg["card_line_pause_ms"] == 750
    assert cfg["silence_style"] in cards.SILENCE_STYLES
    assert cfg["death_fade"] is True and cfg["reload_dim_text"] is False


def test_the_segment16_theme_makes_a_grid() -> None:
    d = make_driver("terminal", {"theme": "segment16", "layout": "flow"}, size=(40, 12))
    assert (d.layout, d.charset) == ("grid", "segment16")  # type: ignore[attr-defined]
