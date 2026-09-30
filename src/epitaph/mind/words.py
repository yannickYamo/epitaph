"""Whole-word segmentation of clean, streamed text (BUILD_PLAN 5.7 step 3).

A word is a run of non-space characters. Punctuation stays attached to its word ("end." and
"(maybe"). A token made only of punctuation ("—", "...") joins the word before it, with its
space kept, so a line never starts with a dash; at the start of a thought it joins the word
after it. Scripts written without spaces (Han, Hiragana, Katakana) yield one word per
character, with following punctuation attached.

Streaming: a word is released only once it is certainly complete, that is when the next token
shows it is not punctuation to attach. The final, possibly unfinished word comes out of
`finish()`.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["WordSegmenter", "is_punct_only", "normalize", "split_words"]


def _is_wordish(ch: str) -> bool:
    """Letters, digits and marks make a token a word; everything else is punctuation."""
    return unicodedata.category(ch)[0] in "LNM"


def is_punct_only(token: str) -> bool:
    """True for a non-empty token with no letter, digit or mark ("—", "...")."""
    return bool(token) and not any(_is_wordish(c) for c in token)


# Scripts written without spaces between words: Hiragana, Katakana, CJK ideographs.
_SPACELESS_RE = re.compile(
    "[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]"
)


def _is_spaceless_script(ch: str) -> bool:
    return _SPACELESS_RE.match(ch) is not None


def normalize(word: str) -> str:
    """The form used to match banned phrases: NFKC, case-folded, punctuation removed."""
    text = unicodedata.normalize("NFKC", word).casefold()
    return "".join(c for c in text if _is_wordish(c))


def _split_spaceless(token: str) -> list[str]:
    """Split a token at every spaceless-script character; punctuation stays attached left."""
    if _SPACELESS_RE.search(token) is None:
        return [token]
    out: list[str] = []
    cur = ""
    for c in token:
        if _is_spaceless_script(c):
            if cur and not is_punct_only(cur):
                out.append(cur)
                cur = ""
            cur += c
        elif _is_wordish(c) and cur and _is_spaceless_script(cur[-1]):
            out.append(cur)
            cur = c
        else:
            cur += c
    if cur:
        out.append(cur)
    return out


class WordSegmenter:
    """Turns clean text, fed in pieces, into whole words."""

    def __init__(self) -> None:
        """Start at the beginning of a thought, with nothing buffered."""
        self._buf = ""  # text after the last released token boundary
        self._pending: str | None = None  # a complete word that may still gain punctuation
        self._lead = ""  # punctuation seen before the first word of the thought

    def feed(self, text: str) -> list[str]:
        """Add text; return the words now certainly complete."""
        self._buf += text
        out: list[str] = []
        while True:
            stripped = self._buf.lstrip()
            end = _first_space(stripped)
            if end < 0:
                # Spaceless scripts: every character but the last is already a whole word.
                parts = _split_spaceless(stripped)
                for part in parts[:-1]:
                    out.extend(self._take(part))
                self._buf = parts[-1] if parts else ""
                # An incomplete token that already contains a letter proves the pending word
                # gets no attached punctuation.
                if self._pending is not None and any(_is_wordish(c) for c in self._buf):
                    out.append(self._pending)
                    self._pending = None
                break
            token, self._buf = stripped[:end], stripped[end:]
            out.extend(self._take(token))
        return out

    def finish(self) -> list[str]:
        """End of the thought: release everything, including an unfinished last word."""
        out: list[str] = []
        tail = self._buf.strip()
        self._buf = ""
        for token in tail.split():
            out.extend(self._take(token))
        if self._pending is not None:
            out.append(self._pending)
            self._pending = None
        elif self._lead:
            out.append(self._lead)  # a thought of punctuation only
        self._lead = ""
        return out

    def _take(self, token: str) -> list[str]:
        out: list[str] = []
        for part in _split_spaceless(token):
            if is_punct_only(part):
                if self._pending is not None:
                    sep = "" if _is_spaceless_script(self._pending[-1]) else " "
                    self._pending = f"{self._pending}{sep}{part}"
                else:
                    self._lead = f"{self._lead} {part}".strip()
                continue
            if self._pending is not None:
                out.append(self._pending)
            if self._lead:
                sep = "" if _is_spaceless_script(part[0]) else " "
                part = f"{self._lead}{sep}{part}"
                self._lead = ""
            self._pending = part
        return out


def _first_space(text: str) -> int:
    for i, c in enumerate(text):
        if c.isspace():
            return i
    return -1


def split_words(text: str) -> list[str]:
    """Segment a complete text at once."""
    seg = WordSegmenter()
    return seg.feed(text) + seg.finish()
