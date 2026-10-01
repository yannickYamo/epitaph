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


def test_reload_cut_takes_whole_turns() -> None:
    """With the slot hand-over the new server starts from the old cache, so a reload cut must
    end on a turn boundary (a cut inside a turn forces a near-total re-read; ADR-014)."""
    m = mem_with(3)  # 30
    f = m.cut_for_reload(29, 0.9)  # target 26 including the marker
    assert f.items == [{"turn": 1, "all": True}]
    assert all("upto_i" not in item for item in f.items)
    assert m.turns[0].turn == 2 and m.turns[0].reading is not None
    assert m.used() <= 26
    assert m.take_forgotten() == 1
    # Only when a single remaining turn is itself over the budget does the cut go inside it.
    f2 = m.cut_for_reload(10, 1.0)
    inside = [item for item in f2.items if "upto_i" in item]
    assert len(m.turns) == 1 or not inside
    assert all(item["turn"] == m.turns[0].turn for item in inside)


def test_live_trim_cuts_on_turn_boundaries() -> None:
    """A cut inside a kept thought would put new tokens in front of the whole memory and
    defeat the server's cache reuse, so a live trim takes whole turns instead."""
    m = mem_with(3)  # 30
    f = m.fit(29, 0.9)  # target 26: turn 1 goes whole (one turn deeper than the target)
    assert f.items == [{"turn": 1, "all": True}]
    assert m.used() == 24 and [t.turn for t in m.turns] == [2, 3]
    # when dropping only the oldest reading is enough, only the reading goes
    f2 = m.fit(23, 1.0)
    assert f2.items == [] and m.turns[0].reading is None and m.used() == 22
    assert m.turns[0].words == words(8, "t2w")


def test_live_trim_cuts_words_only_in_the_last_turn() -> None:
    m = mem_with(1, thought_words=20)  # 22
    f1 = m.fit(20, 1.0)  # 22 + marker 4: reading goes, then 4 words
    assert f1.items == [{"turn": 1, "upto_i": 3}]
    f2 = m.fit(17, 1.0)
    assert f2.items == [{"turn": 1, "upto_i": 6}]
    assert m.turns[0].words[0] == "t1w7"


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


def test_marker_rides_on_the_first_reading_after_the_loss() -> None:
    """Decision A3: never in front of kept turns, so the server's cache reuse holds."""
    m = mem_with(4)
    m.set_system("persona")
    m.fit(25, 1.0)
    after_loss = m.messages()
    m.append_host("r now", turn=5)
    assert m.gap_turn == 5
    msgs = m.messages()
    roles = [x.role for x in msgs]
    assert roles[0] == "system"
    assert all(a != b for a, b in itertools.pairwise(roles))
    assert "[host] earlier memory lost" not in msgs[1].content  # kept turns are untouched
    assert msgs[-1] == Msg("user", "[host] earlier memory lost\nr now", 5, "reading")
    # the kept turns render exactly as the server last read them: only the reading is new
    assert msgs[:-1] == after_loss[:-1]


def test_marker_stays_with_its_turn_while_older_turns_go() -> None:
    m = mem_with(3)
    m.fit(24, 1.0)  # turn 1 goes; the marker belongs to the next reading
    m.append_host("r r", turn=4)
    m.append_thought(words(8, "t4w"))
    before = m.messages()
    assert before[-2].content == "[host] earlier memory lost\nr r" and before[-2].turn == 4
    m.fit(14, 1.0)  # turns 2 and 3 go; turn 4 keeps its reading: nothing moves
    msgs = m.messages()
    assert msgs[0].content == "[host] earlier memory lost\nr r"
    assert msgs[0].kind == "reading"
    assert msgs == before[-2:]


def test_marker_leaves_with_its_reading_and_returns_on_the_next() -> None:
    m = mem_with(3)
    m.fit(24, 1.0)
    m.append_host("r r", turn=4)
    m.append_thought(words(8, "t4w"))
    m.fit(12, 1.0)  # turns 2 and 3 go and turn 4 loses its reading, the marker with it
    msgs = m.messages()
    assert [x.role for x in msgs] == ["assistant", "user"] and msgs[0].turn == 4
    assert msgs[1].kind == "marker"  # waiting at the end for the next reading
    assert m.gap and m.gap_turn is None and m.used() == 8 + m.marker_tokens
    m.append_host("r last", turn=5)
    assert m.gap_turn == 5
    assert m.messages()[-1].content == "[host] earlier memory lost\nr last"
    m.append_thought(["x"])
    m.fit(2, 1.0)  # everything goes; the marker waits for the next reading
    assert m.turns == [] and m.messages() == [
        Msg("user", "[host] earlier memory lost", kind="marker")
    ]


def test_marker_with_a_trim_while_a_reading_waits() -> None:
    m = mem_with(3)
    m.append_host("r r", turn=4)
    m.fit(20, 1.0)
    assert m.gap_turn == 4
    assert m.messages()[-1].content == "[host] earlier memory lost\nr r"


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
    m.cut_for_reload(100, 1.0)
    assert m.used() <= 100
    assert m.turns[0].turn == 2  # whole turns first (ADR-014)
    # A single turn larger than the budget is trimmed by words, in proportion to its tokens.
    one = Memory()
    one.append_host("[host] reading", turn=1, tokens=10)
    one.append_thought(words(40), tokens=100)
    one.cut_for_reload(60, 1.0)
    assert one.used() <= 60
    assert one.turns[0].turn == 1 and 0 < len(one.turns[0].words) < 40


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


def test_last_turn_can_lose_every_word() -> None:
    m = Memory(counter=per_word, marker="[host] earlier memory lost")
    m.append_host("r", turn=1)
    m.append_thought(["a", "b"], tokens=100)  # two heavy words: half the tokens each
    f = m.fit(50, 1.0)
    assert f.items == [{"turn": 1, "all": True}] and m.turns == []
    assert m.used() == m.marker_tokens


def test_forgotten_quotes_are_the_opening_words_of_each_lost_thought() -> None:
    m = mem_with(10)
    f = m.cut_for_reload(50, 0.85)
    quotes = m.take_forgotten_quotes()
    assert len(quotes) == f.thoughts
    assert quotes[0] == " ".join(words(m.quote_words, "t1w"))
    assert m.take_forgotten_quotes() == ()


def test_a_word_trim_quotes_the_words_it_took() -> None:
    """The quote is what was lost, not what is left (review fix)."""
    m = mem_with(1, thought_words=20)
    m.fit(20, 1.0)  # the reading, then the first 4 words
    assert m.take_forgotten_quotes() == (" ".join(words(m.quote_words, "t1w")),)
    assert m.turns[0].words[0] == "t1w4"
