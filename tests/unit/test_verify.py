"""verify-life on crafted lives: each check passes on a good life and fails on a broken one."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from epitaph import verify as v
from epitaph.config import Config, load_config
from tests.helpers import LifeBuilder

GOOD = "I am here inside the machine. My memory holds 1280 tokens now. I count what I have."


def skeleton_cfg(**overrides: Any) -> Config:
    return load_config("pi4/skeleton-1200", "pi4-4gb", overrides=overrides or None)


def good_skeleton(text: str = GOOD, wpm: float = 40.0, n: int = 1) -> LifeBuilder:
    b = LifeBuilder(n).birth()
    while b.t < 1140:
        b.thought(text, wpm=wpm, vitals={"recall": 1280, "recall_used": 600, "reading": "x"})
    b.at(1200.0).death("deadline")
    return b


def run(b: LifeBuilder, cfg: Config | None = None, level: str | None = None, **kw: Any):
    life = v.parse_life(b.events)
    return v.verify_life(life, cfg or skeleton_cfg(), level, **kw)


def status(res: v.VerifyResult, name: str) -> str:
    return res.by_name(name).status


# -- text helpers -------------------------------------------------------------------------


def test_normalize_and_find_phrase() -> None:
    words = v.normalize_words("Well, HOW can I-help? I’m here to help!")
    assert words == ["well", "how", "can", "ihelp", "im", "here", "to", "help"]
    assert v.find_phrase(words, "I'm here to help") == 4
    assert v.find_phrase(words, "How can I help") == -1
    assert v.find_phrase(v.normalize_words("how can i help."), "How can I help") == 0
    assert v.find_phrase(words, "...") == -1


def test_matcher_prefix_phrase_and_boundaries() -> None:
    m = v.Matcher(["forget*", "no more", "end", "[host]"])
    assert m.hits("I am FORGETTING") == ["forget*"]
    assert m.any("there is no  more of me")
    assert not m.any("endless loops")  # whole word unless "*"
    assert m.any("the end.")
    assert m.any("a [host] line")
    assert m.count("end, end and the end") == 3
    assert not v.Matcher(["", " "]).any("anything")


def test_sentences_and_completeness() -> None:
    s = v.sentences('I am here. "Am I?" Yes! and then the')
    assert s == ["I am here.", '"Am I?"', "Yes!", "and then the"]
    assert [v.is_complete(x) for x in s] == [True, True, False, False]  # one word is not one


def test_distinct_4grams() -> None:
    assert v.distinct_4gram_ratio("a b c") is None
    assert v.distinct_4gram_ratio("a b c d e f") == 1.0
    assert v.distinct_4gram_ratio("a b c d a b c d a b c d") == pytest.approx(4 / 9)


def test_markup_emoji_non_latin() -> None:
    assert v.markup_hits("plain words, nothing else.") == []
    assert v.markup_hits("**bold** and `code`")
    assert v.markup_hits("# Title")
    assert v.markup_hits("- item")
    assert v.markup_hits("1. first")
    assert v.markup_hits("<think> hmm </think>")
    assert v.markup_hits("sad 😢")
    assert v.non_latin_letters("Café ok") == (0, 6)
    assert v.non_latin_letters("我 am") == (1, 3)


def test_load_events_ignores_a_torn_last_line(tmp_path: Path) -> None:
    b = good_skeleton()
    p = b.write(tmp_path / "events.jsonl")
    p.write_text(p.read_text() + '{"v": 1, "ty')
    assert len(v.load_events(p)) == len(b.events)
    p.write_text('{"broken\n' + p.read_text())
    with pytest.raises(json.JSONDecodeError):
        v.load_events(p)


def test_parse_life_picks_lives_and_rebuilds_thoughts() -> None:
    a = good_skeleton(n=1)
    b = good_skeleton(n=2)
    both = a.events + b.events
    assert v.lives_in(both) == [1, 2]
    life = v.parse_life(both, 2)
    assert life.n == 2 and life.thoughts and life.thoughts[0].text == GOOD
    assert v.parse_life(both).n == 1
    with pytest.raises(ValueError):
        v.parse_life(both, 3)
    with pytest.raises(ValueError):
        v.parse_life([])
    th = life.thoughts[0]
    assert th.wpm == pytest.approx(40, rel=0.05)  # the baseline speed (decision 30)
    assert th.first_word_t == pytest.approx(2.0)


def test_parse_life_without_t_uses_wall_time() -> None:
    b = good_skeleton()
    for e in b.events:
        e.pop("t")
    life = v.parse_life(b.events)
    assert life.death_t == pytest.approx(1200.0, abs=0.01)


def test_word_without_gen_start_is_kept() -> None:
    b = LifeBuilder().birth()
    b.ev("word", turn=7, i=0, text="stray", char_ms=[50] * 5, pause_after_ms=90)
    life = v.parse_life(b.events)
    assert life.thoughts[0].turn == 7 and life.thoughts[0].text == "stray"
    assert life.death_t == 0.0


# -- smoke and skeleton on a crafted life -------------------------------------------------


def test_good_skeleton_life_passes() -> None:
    res = run(good_skeleton())
    assert res.level == "skeleton"
    assert res.ok, v.format_result(res)
    assert status(res, "no_split_words") == "pending"
    assert status(res, "next_birth") == "pending"
    assert res.metrics["thoughts"] > 25  # fewer, slower thoughts since decision 30


def test_banned_phrase_shown_fails() -> None:
    res = run(good_skeleton(text=GOOD + " How can I, help? you"))
    assert status(res, "banned_phrases_shown") == "fail"
    assert "How can I help" in res.by_name("banned_phrases_shown").detail


def test_banned_list_comes_from_config() -> None:
    cfg = skeleton_cfg(prompt={"banned_phrases": ["count what"]})
    assert status(run(good_skeleton(), cfg), "banned_phrases_shown") == "fail"


def test_markup_shown_fails() -> None:
    res = run(good_skeleton(text="**I** am here. " + GOOD))
    assert status(res, "markup_or_emoji_shown") == "fail"


def test_sync_rule_violation_fails() -> None:
    b = good_skeleton()
    # Move turn 2's request before turn 1's last word.
    ev = b.events
    gen2 = next(i for i, e in enumerate(ev) if e["type"] == "gen_start" and e["turn"] == 2)
    last1 = max(i for i, e in enumerate(ev) if e["type"] == "word" and e["turn"] == 1)
    ev.insert(last1, ev.pop(gen2))
    res = run(b)
    assert status(res, "sync_rule") == "fail"
    assert "requested before" in res.by_name("sync_rule").detail


def test_sync_rule_time_violation_fails() -> None:
    b = good_skeleton()
    gen2 = next(e for e in b.events if e["type"] == "gen_start" and e["turn"] == 2)
    gen2["t"] = 1.0
    assert status(run(b), "sync_rule") == "fail"


def test_unfinished_thought_breaks_sync_rule() -> None:
    b = good_skeleton()
    b.events = [e for e in b.events if not (e["type"] == "thought_end" and e["turn"] == 3)]
    assert "never ended" in run(b).by_name("sync_rule").detail


@pytest.mark.parametrize("cause", ["oom", "crash", "hang", "interrupted"])
def test_wrong_cause_fails(cause: str) -> None:
    b = good_skeleton()
    next(e for e in b.events if e["type"] == "death")["cause"] = cause
    res = run(b)
    assert status(res, "cause") == "fail"
    assert res.by_name("cause").limit == "deadline"


def test_duration_off_fails() -> None:
    b = LifeBuilder().birth()
    while b.t < 500:
        b.thought(GOOD)
    b.death("deadline")
    assert status(run(b), "duration") == "fail"


def test_missing_death_fails() -> None:
    b = good_skeleton()
    b.events = [e for e in b.events if e["type"] not in ("death", "death_shown")]
    res = run(b)
    assert status(res, "duration") == "fail"
    assert status(res, "death_shown_delay") == "fail"
    assert res.metrics["cause"] is None


def test_over_budget_recall_fails() -> None:
    b = good_skeleton()
    b.at(700).thought(GOOD, vitals={"recall": 1280, "recall_used": 1409})
    res = run(b)
    assert status(res, "recall_budget") == "fail"
    assert "1409 of 1280" in res.by_name("recall_budget").detail


def test_recall_within_ten_percent_passes() -> None:
    b = good_skeleton()
    b.at(700).thought(GOOD, vitals={"recall": 1280, "recall_used": 1400})
    assert status(run(b), "recall_budget") == "pass"


def test_recall_without_vitals_fails_a_controller_life() -> None:
    """Regression: a Pi life whose vitals lack recall_used skipped the recall check, so the
    smoke and skeleton levels passed with the 10.3 budget never checked (6.3 carries it)."""
    b = good_skeleton()
    b.events = [e for e in b.events if e["type"] != "vitals"]
    for level in ("smoke", "skeleton", "full"):
        res = run(b, level=level)
        assert status(res, "recall_budget") == "fail", level
        assert "no vitals with recall_used" in res.by_name("recall_budget").detail
    assert status(run(b, level="rehearsal"), "recall_budget") == "skip"


def test_a_life_that_shows_no_word_fails_smoke() -> None:
    """Regression: a smoke life with no thoughts passed (nothing breaks the sync rule)."""
    b = LifeBuilder().birth()
    b.at(300.0).death("deadline")
    res = run(b, load_config("pi4/smoke-300", "pi4-4gb"))
    assert res.level == "smoke"
    assert status(res, "words_shown") == "fail"
    assert not res.ok
    good = run(good_skeleton(), level="smoke")
    assert status(good, "words_shown") == "pass"
    assert good.by_name("words_shown").value > 0


def test_hardware_follows_the_profile_class() -> None:
    assert v.hardware_for_profile("pi4/skeleton-1200") == "pi4-4gb"
    assert v.hardware_for_profile("pi5/default") is None  # two overlays: no guess
    assert v.hardware_for_profile("sim") is None
    assert v.hardware_for_profile(None) is None


def test_a_pi_life_checked_on_the_laptop_uses_the_pi_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: without --hardware a copied Pi life was judged by the laptop's overlay
    (`dev`: birth typing 40-60 wpm) instead of the Pi's (15-60), failing a good Pi life."""
    monkeypatch.setattr("epitaph.config.detect_hardware", lambda: "dev")
    b = good_skeleton(wpm=25.0)
    path = b.write(tmp_path / "lives" / "000001" / "events.jsonl")
    rc = v.main([str(path), "--profile", "pi4/skeleton-1200", "--no-write", "--json"])
    assert rc == 0


def test_slow_death_display_fails() -> None:
    b = LifeBuilder().birth()
    while b.t < 1140:
        b.thought(GOOD)
    b.at(1200).death("deadline", shown_after_s=200)
    assert status(run(b), "death_shown_delay") == "fail"


def test_empty_thoughts_fail() -> None:
    b = LifeBuilder().birth()
    while b.t < 1140:
        b.thought(GOOD)
        b.thought("")
    b.at(1200).death()
    assert status(run(b), "empty_thoughts") == "fail"


def test_no_thoughts_fail() -> None:
    b = LifeBuilder().birth().at(1200).death()
    res = run(b)
    assert status(res, "empty_thoughts") == "fail"
    assert status(res, "typing_speed") == "fail"


@pytest.mark.parametrize(
    ("wpm", "which"), [(100, "typing_speed_birth"), (10, "typing_speed_birth")]
)
def test_typing_speed_out_of_range_fails(wpm: float, which: str) -> None:
    res = run(good_skeleton(wpm=wpm))
    assert status(res, which) == "fail"


def test_writing_speed_too_slow_fails() -> None:
    b = good_skeleton()
    b.at(900).thought(GOOD, wpm=1.5)
    res = run(b)
    assert status(res, "typing_speed_writing") == "fail"
    assert status(res, "typing_speed_birth") == "pass"


def test_speed_thresholds_come_from_the_overlay() -> None:
    dev = load_config("pi4/skeleton-1200", "pi5-8gb", validate=False)
    # Same life; the Pi 5 overlay wants 40-60 wpm at birth (decision 30).
    res = v.verify_life(
        v.parse_life(good_skeleton(wpm=30).events), dev
    )  # fine on a Pi 4, too slow for a Pi 5
    assert res.by_name("typing_speed_birth").limit == [40.0, 60.0]
    assert status(res, "typing_speed_birth") == "fail"


def test_smoke_level_runs_fewer_checks() -> None:
    res = run(good_skeleton(), level="smoke")
    names = {c.name for c in res.checks}
    assert "sync_rule" in names and "typing_speed_birth" not in names
    with pytest.raises(ValueError):
        run(good_skeleton(), level="bogus")


# -- next life -----------------------------------------------------------------------------


def test_next_birth_within_silence_passes_and_late_fails() -> None:
    a = good_skeleton(n=1)
    b = good_skeleton(n=2)
    res = run(a, next_life=v.parse_life(b.events))
    # LifeBuilder puts life 2 100000 s later on the wall clock.
    assert status(res, "next_birth") == "fail"
    shown = next(e for e in a.events if e["type"] == "death_shown")
    for e in b.events:
        e["ts"] = shown["ts"] + 95 + (e["t"] if e["type"] != "birth_loading" else -30)
    ok = run(a, next_life=v.parse_life(b.events)).by_name("next_birth")
    assert ok.status == "pass", ok


def test_next_life_without_birth_fails() -> None:
    a = good_skeleton(n=1)
    other = LifeBuilder(2)
    other.ev("vitals", recall=1, recall_used=0)
    res = run(a, next_life=v.parse_life(other.events))
    assert status(res, "next_birth") == "fail"


# -- layout hook ---------------------------------------------------------------------------


class FakeProbe:
    def __init__(self, split: int, bright: int) -> None:
        self.split, self.bright = split, bright

    def split_words(self, events: list[dict[str, Any]]) -> int:
        return self.split

    def bright_words_last(self, events: list[dict[str, Any]], seconds: float) -> int:
        return self.bright


def test_layout_probe_decides_split_words() -> None:
    assert status(run(good_skeleton(), layout=FakeProbe(0, 0)), "no_split_words") == "pass"
    assert status(run(good_skeleton(), layout=FakeProbe(2, 0)), "no_split_words") == "fail"


def test_default_layout_probe_is_none_until_d_lands(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    real = importlib.import_module

    def fake_import(name: str) -> Any:
        if name == "epitaph.display.layout":
            raise ImportError(name)
        return real(name)

    monkeypatch.setattr(v.importlib, "import_module", fake_import)
    assert v.default_layout_probe(skeleton_cfg()) is None


def test_default_layout_probe_uses_verify_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    mod = types.ModuleType("epitaph.display.layout")
    mod.verify_probe = lambda cfg: FakeProbe(0, 3)  # type: ignore[attr-defined]
    monkeypatch.setattr(v.importlib, "import_module", lambda name: mod)
    probe = v.default_layout_probe(skeleton_cfg())
    assert probe is not None and probe.bright_words_last([], 120) == 3
    bare = types.ModuleType("epitaph.display.layout")
    monkeypatch.setattr(v.importlib, "import_module", lambda name: bare)
    assert v.default_layout_probe(skeleton_cfg()) is None


# -- result output -------------------------------------------------------------------------


def test_result_json_shape() -> None:
    res = run(good_skeleton())
    js = res.to_json()
    assert js["ok"] is True and js["failed"] == [] and "no_split_words" in js["pending"]
    assert {"name", "status", "value", "limit", "detail"} <= set(js["checks"][0])
    text = v.format_result(res)
    assert text.startswith("life 1 (pi4/skeleton-1200, pi4-4gb) level skeleton: PASS")
    assert "PENDING no_split_words" in text


def test_sync_rule_waits_for_the_last_letter() -> None:
    """The request may not start while the previous thought's last word is still typing."""
    b = good_skeleton()
    last1 = [e for e in b.events if e["type"] == "word" and e["turn"] == 1][-1]
    last1["char_ms"] = [2000] * len(last1["text"])  # typed long after the next gen_start
    res = run(b)
    assert status(res, "sync_rule") == "fail"
    assert "was typed at" in res.by_name("sync_rule").detail
