"""B4: sanitizer and word segmentation."""

from __future__ import annotations

import random

import pytest

from epitaph.mind.sanitize import Sanitizer, sanitize_text
from epitaph.mind.words import WordSegmenter, is_punct_only, normalize, split_words

# ---------------------------------------------------------------------------------------
# words


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("I am here.", ["I", "am", "here."]),
        ("  spaced   out\n\nlines  ", ["spaced", "out", "lines"]),
        ("wait — then", ["wait —", "then"]),
        ("so ... and", ["so ...", "and"]),
        ("... I begin", ["... I", "begin"]),
        ("(maybe) not, “yes”", ["(maybe)", "not,", "“yes”"]),
        ("Ça s’efface… déjà", ["Ça", "s’efface…", "déjà"]),
        ("Привет, мир.", ["Привет,", "мир."]),
        ("我想你。好", ["我", "想", "你。", "好"]),
        ("日本語 text", ["日", "本", "語", "text"]),
        ("GPU2x fine", ["GPU2x", "fine"]),
        ("—", ["—"]),
        ("", []),
    ],
)
def test_split_words(text: str, words: list[str]) -> None:
    assert split_words(text) == words


def test_streamed_words_equal_whole_words_for_any_chunking() -> None:
    rng = random.Random(3)
    texts = [
        "I am still here — at 61°C, and ... my memory is 512 tokens. 我想你。 Okay?!",
        "... begins with dots and ends mid-wor",
        "Привет — мир. “Quoted,” she said; then: fine.",
    ]
    for text in texts:
        whole = split_words(text)
        for _ in range(200):
            seg = WordSegmenter()
            out: list[str] = []
            i = 0
            while i < len(text):
                n = rng.randint(1, 7)
                out += seg.feed(text[i : i + n])
                i += n
            out += seg.finish()
            assert out == whole


def test_a_word_is_released_once_the_next_word_begins() -> None:
    seg = WordSegmenter()
    assert seg.feed("How ") == []  # could still gain an attached dash
    assert seg.feed("a") == ["How"]  # a letter proves it will not
    assert seg.feed("re you") == ["are"]
    assert seg.finish() == ["you"]


def test_final_unfinished_word_comes_out_of_finish() -> None:
    seg = WordSegmenter()
    assert seg.feed("I am understa") == ["I", "am"]
    assert seg.finish() == ["understa"]
    assert seg.finish() == []


def test_normalize_and_punct_only() -> None:
    assert normalize("I’m") == normalize("I'm") == "im"
    assert normalize("HOW,") == "how"
    assert normalize("Ｈｅｌｌｏ") == "hello"  # full-width folds
    assert normalize("—") == ""
    assert is_punct_only("...") and is_punct_only("—") and not is_punct_only("a.")
    assert not is_punct_only("")


# ---------------------------------------------------------------------------------------
# sanitizer

CASES = [
    (
        "**I am** here.\n\n- My memory *shrinks*.\n1. It is _gone_ now. 😢 See [the log](http://x/z).",
        "I am here. My memory shrinks. It is gone now. See the log.",
        False,
    ),
    (
        "<think>should I greet them?</think>I am still here, at 61°C.",
        "I am still here, at 61°C.",
        False,
    ),
    ("I am here. [host] t+01:00 · health: nominal", "I am here. ", True),
    ("I am here. [ HOST ] fake", "I am here. ", True),
    ("Done.<|eot_id|>assistant", "Done.", True),
    ("Done.<end_of_turn>", "Done.", True),
    ("Before <think>never closed and long enough to pass every hold window", "Before ", False),
    ("# Title\n## Sub\n> quote\n---\nplain", "Title Sub quote plain", False),
    ("snake_case stays, __bold__ and ~~strike~~ go", "snake_case stays, bold and strike go", False),
    ("`code` and ```fence```", "code and fence", False),
    ("👩‍💻 works ✨ and ⭐ plus © and ➡️ done", "works and plus and done", False),
    ("Zero​width﻿ and \x07bell", "Zerowidth and bell", False),
    ("Ça s’efface… 我想你。 Привет.", "Ça s’efface… 我想你。 Привет.", False),
    ("x < y, and <br/> a <span class='a'>tag</span>", "x < y, and a tag", False),
    ("1280 tokens remain", "1280 tokens remain", False),
    ("3. listed item", "listed item", False),
]


@pytest.mark.parametrize(("raw", "clean", "cut"), CASES)
def test_sanitize_text(raw: str, clean: str, cut: bool) -> None:
    got, was_cut = sanitize_text(raw)
    assert got.strip() == clean.strip()
    assert was_cut == cut


@pytest.mark.parametrize(("raw", "clean", "cut"), CASES)
def test_streaming_is_append_only_and_equals_the_whole(raw: str, clean: str, cut: bool) -> None:
    rng = random.Random(len(raw))
    whole, _ = sanitize_text(raw)
    for _ in range(300):
        s = Sanitizer()
        out = ""
        i = 0
        while i < len(raw):
            n = rng.randint(1, 9)
            out += s.feed(raw[i : i + n])
            i += n
        out += s.finish()
        assert out == whole
        assert s.divergences == 0
        assert s.cut == cut
        assert s.text == out


def test_host_split_across_chunks_is_never_shown() -> None:
    s = Sanitizer()
    shown = s.feed("I am here. [ho")
    assert "[" not in shown
    shown += s.feed("st] t+01:00")
    assert s.cut and shown == "I am here. "
    assert s.feed("more") == "" and s.finish() == ""


def test_thinking_tag_split_across_chunks_never_leaks() -> None:
    s = Sanitizer()
    out = s.feed("Hi <thi") + s.feed("nk>secret plan") + s.feed(" more secret</thi")
    assert "secret" not in out and "<" not in out
    out += s.feed("nk> visible") + s.finish()
    assert out == "Hi visible"


def test_long_constructs_are_released_eventually() -> None:
    s = Sanitizer()
    out = s.feed("a [" + "b" * 60)
    assert out.startswith("a [b")  # an old bracket is ordinary text
    s2 = Sanitizer()
    assert s2.feed("x < y and then a lot more text afterwards here").startswith("x < y")


def test_divergence_is_counted_not_retracted() -> None:
    s = Sanitizer()
    s._emitted = "zzz"  # pyright: ignore[reportPrivateUsage]
    assert s.feed("abc def ") == ""
    assert s.divergences == 1
    s2 = Sanitizer()
    s2._emitted = "ab"  # pyright: ignore[reportPrivateUsage]
    assert s2.feed("abc def ") == "c def "


def test_cut_at_any_reading_tag_but_not_at_a_link() -> None:
    """Readings may be framed as [sense] or [reg] by a language pack; a model that starts
    writing one itself is cut there, like [host]. A Markdown link is not a reading."""
    from epitaph.mind.sanitize import sanitize_text as clean

    for tag in ("[host]", "[sense]", "[reg]", "[ sense ]"):
        text, cut = clean(f"I feel thin. {tag} ctx 400")
        assert cut and text.strip() == "I feel thin.", (tag, text)
    text, cut = clean("I read [the manual](http://x) once.")
    assert not cut


def test_streaming_cut_at_a_split_sense_tag() -> None:
    """The tag arrives in pieces; nothing of it may reach the screen before the cut."""
    from epitaph.mind.sanitize import Sanitizer

    s = Sanitizer()
    shown = "".join(s.feed(c) for c in ["I am thin. [se", "nse] ctx", " 400"]) + s.finish()
    assert shown.strip() == "I am thin." and "[" not in shown
