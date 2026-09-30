"""Output sanitizer: what the creature writes, made fit for the screen (BUILD_PLAN 5.7 step 2).

Removes thinking blocks, markdown, emoji, stray tags and control characters, and cuts the
thought where the model starts writing a reading of its own (`[host]`) or leaks a template
token. Letters of every script are kept.

`sanitize_text` works on a complete text. `Sanitizer` works on a stream: it re-cleans the
whole thought on every chunk (a thought is a few hundred characters) but holds back a tail
that could still turn into something to remove (`<thi`, `[ho`, a line that may be a list
marker), so what it emits is append-only and equals `sanitize_text` of the whole thought.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["Sanitizer", "sanitize_text"]

_CUT_RE = re.compile(
    r"\[\s*host\s*\]|<\|[^|>]*\|?>?|<end_of_turn>|<start_of_turn>|</s>|<eos>|<bos>",
    re.IGNORECASE,
)
_THINK_OPEN = r"<\s*(think|thinking|thought|reasoning)\s*>"
_THINK_BLOCK_RE = re.compile(_THINK_OPEN + r".*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL)
_THINK_UNCLOSED_RE = re.compile(_THINK_OPEN + r".*\Z", re.IGNORECASE | re.DOTALL)
# Lengths are bounded to match the streaming hold windows below, so a construct that was
# already released as text is never removed later.
_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9_-]{0,15}(?:\s[^<>\n]{0,12})?/?>")
_IMAGE_RE = re.compile(r"!\[([^\]\n]{0,46})\]\([^)\n]{0,150}\)")
_LINK_RE = re.compile(r"\[([^\]\n]{0,46})\]\([^)\n]{0,150}\)")
_LINE_MARK_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*|>[ \t]*|[-*+•][ \t]+|\d{1,3}[.)][ \t]+)+", re.MULTILINE
)
_RULE_RE = re.compile(r"^[ \t]*([-*_=])(?:[ \t]*\1){2,}[ \t]*$", re.MULTILINE)
_UNDERSCORE_RE = re.compile(r"(?<![^\W_])_+|_+(?![^\W_])")
_SPACE_RE = re.compile(r"\s+")

# The degree sign and other ordinary symbols are kept; only pictographs go.
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # pictographs, emoticons, transport, flags, symbols and pictographs
    (0x2600, 0x27BF),  # miscellaneous symbols, dingbats
    (0x2B00, 0x2BFF),  # arrows and stars used as emoji
    (0x2190, 0x21FF),  # arrows
    (0x2300, 0x23FF),  # technical symbols used as emoji (watch, hourglass)
    (0x25A0, 0x25FF),  # geometric shapes
    (0xFE00, 0xFE0F),  # variation selectors
    (0xE0000, 0xE007F),  # tags
    (0x1F1E6, 0x1F1FF),  # regional indicators
)
_EMOJI_SINGLES = {0x200D, 0x20E3, 0x3030, 0x303D, 0x3297, 0x3299, 0x00A9, 0x00AE, 0x2122}
_INVISIBLE = {0x200B, 0x200C, 0x2060, 0xFEFF, 0xFFFD}

# How far back a partial construct is held while streaming.
_HOLD_TAG = 40
_HOLD_BRACKET = 48
_HOLD_LINK_URL = 152
_HOLD_LINE = 5


def _drop_char(c: str) -> bool:
    o = ord(c)
    if o in _EMOJI_SINGLES or o in _INVISIBLE:
        return True
    if any(a <= o <= b for a, b in _EMOJI_RANGES):
        return True
    cat = unicodedata.category(c)
    return (cat == "Cc" and c not in "\n\t\r ") or cat == "Co" or cat == "Cs"


def _strip_thinking(text: str) -> str:
    text = _THINK_BLOCK_RE.sub(" ", text)
    return _THINK_UNCLOSED_RE.sub(" ", text)


def _cut(text: str) -> tuple[str, bool]:
    m = _CUT_RE.search(text)
    if m is None:
        return text, False
    return text[: m.start()], True


def _clean(text: str) -> str:
    text = "".join(c for c in text if not _drop_char(c))
    text = _TAG_RE.sub(" ", text)
    text = _IMAGE_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _RULE_RE.sub(" ", text)
    text = _LINE_MARK_RE.sub("", text)
    text = text.replace("`", "").replace("*", "").replace("~~", "")
    text = _UNDERSCORE_RE.sub("", text)
    text = _SPACE_RE.sub(" ", text)
    return text.lstrip()


def sanitize_text(text: str) -> tuple[str, bool]:
    """Clean a complete thought. Returns (clean text, whether it was cut at [host] or a
    template token). Trailing space is kept so streamed output stays append-only."""
    text, cut = _cut(_strip_thinking(text))
    return _clean(text), cut


def _hold_from(raw: str) -> int:
    """Index from which the raw tail could still become something to remove."""
    n = len(raw)
    hold = n
    lt = raw.rfind("<")
    if lt >= 0 and ">" not in raw[lt:] and n - lt <= _HOLD_TAG:
        hold = min(hold, lt)
    lb = raw.rfind("[")
    if lb >= 0:
        close = raw.find("]", lb)
        start = lb - 1 if lb > 0 and raw[lb - 1] == "!" else lb
        if close < 0:
            if n - lb <= _HOLD_BRACKET:
                hold = min(hold, start)
        elif close - lb <= _HOLD_BRACKET and (
            close == n - 1
            or (
                raw[close + 1 : close + 2] == "("
                and ")" not in raw[close:]
                and n - close <= _HOLD_LINK_URL
            )
        ):
            hold = min(hold, start)
    line_start = raw.rfind("\n") + 1
    line = raw[line_start:]
    if len(line) <= _HOLD_LINE and re.fullmatch(r"[ \t#>*+\-•\d.)_=]*", line):
        hold = min(hold, line_start)
    if raw.endswith("!"):
        hold = min(hold, n - 1)  # may open an image link
    if raw.endswith("_") or raw.endswith("~"):
        hold = min(hold, len(raw.rstrip("_~")))
    return hold


class Sanitizer:
    """Streaming sanitizer for one thought."""

    def __init__(self) -> None:
        self._raw = ""
        self._emitted = ""
        self.cut = False
        self.divergences = 0  # times the clean prefix was not append-only (should stay 0)

    @property
    def text(self) -> str:
        """Everything emitted so far."""
        return self._emitted

    def feed(self, chunk: str) -> str:
        """Add raw output; return the clean text that is now safe to show."""
        if self.cut:
            return ""
        self._raw += chunk
        body = _strip_thinking(self._raw)
        body, cut = _cut(body)
        if cut:
            self.cut = True
            return self._emit(_clean(body))
        # Hold on the thinking-stripped text so an unclosed <think> never leaks.
        return self._emit(_clean(body[: _hold_from(body)]))

    def finish(self) -> str:
        """End of the thought: release the held tail, cleaned."""
        if self.cut:
            return ""
        clean, cut = sanitize_text(self._raw)
        self.cut = cut
        return self._emit(clean)

    def _emit(self, clean: str) -> str:
        if clean.startswith(self._emitted):
            delta = clean[len(self._emitted) :]
        else:
            self.divergences += 1
            common = 0
            for a, b in zip(clean, self._emitted, strict=False):
                if a != b:
                    break
                common += 1
            delta = clean[len(self._emitted) :] if common == len(self._emitted) else ""
        self._emitted += delta
        return delta
