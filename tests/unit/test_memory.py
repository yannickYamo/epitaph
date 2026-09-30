"""B2: memory, the recall rule, reload cuts, the marker, fits()."""

from __future__ import annotations

import itertools

import pytest

from epitaph.mind.memory import Memory, approx_tokens, merge_consecutive
from epitaph.types import Msg


def words(n: int, tag: str = "w") -> list[str]:
    return [f"{tag}{i}" for i in range(n)]


def per_word(text: str) -> int:
    """Counter for exact arithmetic: one token per word."""
    return len(text.split())


def mem_with(turns: int, reading_tokens: int = 2, thought_words: int = 8) -> Memory:
    m = Memory(counter=per_word, marker="[host] earlier memory lost")  # marker = 4 tokens
    for t in range(1, turns + 1):
        m.append_host(" ".join(["r"] * reading_tokens), turn=t)
        m.append_thought(words(thought_words, f"t{t}w"))
    return m


def test_approx_tokens() -> None:
    assert approx_tokens("") == 0
    assert approx_tokens("abcd") == 5
    assert approx_tokens("abcde") == 6


def test_no_trim_under_recall() -> None:
    m = mem_with(3)  # 3 x (2 + 8) = 30
    f = m.fit(30, 0.5)
    assert not f and f.items == [] and m.used() == 30 and not m.gap
    assert m.take_forgotten() == 0


def test_hysteresis_trims_oldest_turns_first_to_recall_times_trim_to() -> None:
    m = mem_with(6)  # 60 tokens
    f = m.fit(50, 0.8)  # over 50 -> trim to 40, marker counts (4)
    assert f
    assert m.used() <= 40
    assert f.marker_added and m.gap
    # turns 1 and 2 go whole; the rest fits with a word-level trim of turn 3
    assert f.items[:2] == [{"turn": 1, "all": True}, {"turn": 2, "all": True}]
    kept = [t.turn for t in m.turns]
    assert kept[-1] == 6 and kept == sorted(kept)
    assert f.tokens_before == 60 and f.tokens_after == m.used()
    # a second fit under the same recall does nothing: hysteresis
    assert not m.fit(50, 0.8)


def test_word_level_trim_inside_the_oldest_kept_turn() -> None:
    m = mem_with(3)  # 30
    f = m.fit(29, 0.9)  # target 26 incl. marker 4 -> past turns 22
    # turn 1 cannot go whole (30 - 10 + 4 = 24 < 26), so its reading goes, then 6 words
    assert f.items == [{"turn": 1, "upto_i": 5}]
    first = m.turns[0]
    assert first.turn == 1 and first.reading is None and first.words == ["t1w6", "t1w7"]
    assert m.used() == 26
    assert m.take_forgotten() == 1
    # a later trim of the same thought continues the word indices
    f2 = m.fit(25, 1.0)
    assert f2.items == [{"turn": 1, "upto_i": 6}]
    f3 = m.fit(24, 1.0)
    assert f3.items == [{"turn": 1, "all": True}]
    assert m.take_forgotten() == 0  # already counted as forgotten once
    msgs = m.past_messages()
    assert msgs[0] == Msg("user", "[host] earlier memory lost", kind="marker")


def test_partial_word_trim_reports_upto_i_across_trims() -> None:
    m = mem_with(2, thought_words=20)  # 44
    f1 = m.fit(43, 1.0)  # target 43 incl marker: 44 + 4 - 2 (reading) = 46 -> drop 3 words
    assert f1.items == [{"turn": 1, "upto_i": 2}]
    f2 = m.fit(40, 1.0)
    assert f2.items == [{"turn": 1, "upto_i": 5}]
    assert m.turns[0].words[0] == "t1w6"


def test_newest_reading_and_current_thought_are_never_trimmed() -> None:
    m = mem_with(2)
    m.append_host("r r r r r", turn=3)
    before = m.pending_tokens
    f = m.fit(0, 0.85)
    assert {i["turn"] for i in f.items} == {1, 2}
    assert m.turns == []
    assert m.pending_tokens == before == 5
    assert m.messages()[-1].content == "[host] earlier memory lost\nr r r r r"
    m.append_thought(["still", "here"])
    assert m.turns[-1].words == ["still", "here"]


def test_reload_cut_is_reported_by_the_next_reading() -> None:
    m = mem_with(10)  # 100
    f = m.cut_for_reload(50, 0.85)
    assert f.thoughts >= 5
    assert m.used() <= 42
    n = m.take_forgotten()
    assert n == f.thoughts
    assert m.take_forgotten() == 0
    assert m.forgotten_total == n


def test_marker_precedes_the_oldest_remembered_message_and_roles_alternate() -> None:
    m = mem_with(4)
    m.set_system("persona")
    m.fit(25, 1.0)
    m.append_host("r now", turn=5)
    msgs = m.messages()
    roles = [x.role for x in msgs]
    assert roles[0] == "system"
    assert all(a != b for a, b in itertools.pairwise(roles))
    assert msgs[1].content.startswith("[host] earlier memory lost")
    assert msgs[-1].role == "user" and msgs[-1].content.endswith("r now")


def test_marker_joins_a_kept_reading() -> None:
    m = mem_with(3)
    m.fit(24, 1.0)  # turn 1 goes whole; turn 2 keeps its reading
    msgs = m.messages()
    assert msgs[0].content == "[host] earlier memory lost\nr r"
    assert msgs[0].kind == "reading"


def test_empty_thoughts_and_merging() -> None:
    m = Memory(counter=per_word, marker="lost")
    m.append_host("a b", turn=1)
    m.append_thought([])
    m.append_host("c d", turn=2)
    assert [x.content for x in m.messages()] == ["a b\nc d"]
    # an empty thought is dropped whole, with no forget item and not counted as forgotten
    m.append_thought(["x"])
    f = m.fit(4, 1.0)  # 1 marker + 2 + 3 = 6 -> turn 1 (reading only) goes
    assert f.items == [] and f.thoughts == 0
    assert [t.turn for t in m.turns] == [2] and m.used() == 4
    assert merge_consecutive([Msg("system", "a"), Msg("system", "b")]) == [
        Msg("system", "a"),
        Msg("system", "b"),
    ]


def test_trim_to_everything() -> None:
    m = mem_with(3)
    f = m.fit(2, 0.5)  # target 1, below the marker itself
    assert [i["all"] for i in f.items] == [True, True, True]
    assert m.turns == [] and m.gap
    assert m.used() == m.marker_tokens


def test_fits_for_unbounded() -> None:
    m = mem_with(3)  # 30
    m.set_system("a b c d e")  # 5
    assert m.fits(ctx=45, max_tokens=10)
    assert not m.fits(ctx=44, max_tokens=10)
    assert m.fits(ctx=48, max_tokens=10, reading_tokens=3)
    m.append_host("x y z", turn=4)
    assert m.fits(ctx=48, max_tokens=10) and not m.fits(ctx=47, max_tokens=10)
    assert m.prompt_tokens() == 38


def test_external_token_counts_and_proportional_word_trim() -> None:
    m = Memory()
    m.append_host("[host] reading", turn=1, tokens=10)
    m.append_thought(words(40), tokens=100)
    m.append_host("[host] reading", turn=2, tokens=10)
    m.append_thought(words(4), tokens=10)
    assert m.used() == 130
    m.fit(100, 1.0)
    assert m.used() <= 100
    assert m.turns[0].turn == 1 and 0 < len(m.turns[0].words) < 40


def test_set_system_reports_change_and_order_errors() -> None:
    m = Memory()
    assert m.set_system("a") and not m.set_system("a")
    assert m.system_tokens == approx_tokens("a")
    with pytest.raises(RuntimeError):
        m.append_thought(["x"])
    m.append_host("r", turn=1)
    with pytest.raises(RuntimeError):
        m.append_host("r", turn=2)
    m.discard_pending()
    assert m.pending_tokens == 0
    Memory().fit(10)  # empty memory never trims


def test_messages_without_system() -> None:
    m = mem_with(1)
    assert [x.kind for x in m.messages()] == ["reading", "thought"]
