# pyright: strict
"""A life's epitaph, taken from the words it actually showed (BUILD_PLAN 13), and the filter
that runs before anything is stored as postable.

Extraction works on the shown thoughts in order (each one the words of a thought joined by
single spaces, as the screen showed them):

- `last_sentence` (default): the last complete sentence of the last thought that has one.
- `last_words`: the very last unit shown: the final fragment if the life ended mid-sentence,
  else the final thought's last sentence.
- `last_thought`: the whole final thought.

In every mode a final fragment (late lives often end mid-sentence: "I'm not made") is kept as
`last_words`. A text over the limit keeps its end, after "…": the last words are the point.

Lengths are measured as X counts them (`x_length`): one for Latin letters and most
punctuation, two for anything else (CJK, emoji, "…").
"""

from __future__ import annotations

import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from epitaph.config import CONFIG_DIR, ConfigError

__all__ = [
    "MODES",
    "Blocklist",
    "Extract",
    "Filtered",
    "clean",
    "extract",
    "load_blocklist",
    "parse_thought",
    "truncate_tail",
    "x_length",
]

MODES = ("last_sentence", "last_words", "last_thought")
ELLIPSIS = "…"

# A word that ends a sentence: terminal punctuation, then any closing quotes or brackets.
_ENDS = re.compile(r"[.!?…]+[\"'”’»)\]]*$")
_ALNUM = re.compile(r"\w")

# X's weighting (twitter-text v3): these code point ranges weigh 1, everything else 2.
_LIGHT = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))


def x_length(text: str) -> int:
    """The length of `text` as X counts it toward its 280 limit (URLs aside: we strip them)."""
    n = 0
    for ch in unicodedata.normalize("NFC", text):
        cp = ord(ch)
        n += 1 if any(lo <= cp <= hi for lo, hi in _LIGHT) else 2
    return n


def truncate_tail(text: str, limit: int) -> tuple[str, bool]:
    """`text` within `limit` (X-weighted), keeping its end from a word boundary after "…".

    Returns the text and whether it was cut.
    """
    if x_length(text) <= limit:
        return text, False
    budget = limit - x_length(ELLIPSIS)
    kept: list[str] = []
    used = 0
    for word in reversed(text.split()):
        need = x_length(word) + (1 if kept else 0)
        if used + need > budget:
            break
        kept.append(word)
        used += need
    if not kept:  # one word longer than the limit: cut inside it
        tail = text.strip()
        while tail and x_length(tail) > budget:
            tail = tail[1:]
        return ELLIPSIS + tail, True
    return ELLIPSIS + " ".join(reversed(kept)), True


def parse_thought(text: str) -> tuple[list[str], str]:
    """The complete sentences of one shown thought, and the fragment after the last of them.

    A sentence ends at a word ending in . ! ? or … (closing quotes and brackets may follow), so
    "3.5" or "t+12:30" inside a sentence never cut it. A "sentence" with no letter or digit
    ("...") is not one.
    """
    sentences: list[str] = []
    cur: list[str] = []
    for word in text.split():
        cur.append(word)
        if _ENDS.search(word):
            s = " ".join(cur)
            if _ALNUM.search(s):
                sentences.append(s)
            cur = []
    fragment = " ".join(cur)
    return sentences, fragment if _ALNUM.search(fragment) else ""


@dataclass(frozen=True)
class Extract:
    """What a life leaves: the epitaph, and the final fragment if it ended mid-sentence."""

    epitaph: str
    last_words: str | None
    truncated: bool = False


def extract(thoughts: list[str], mode: str = "last_sentence", max_chars: int = 240) -> Extract:
    """The epitaph of a life from its shown thoughts, in order (empty thoughts are skipped)."""
    if mode not in MODES:
        raise ValueError(f"unknown epitaph mode {mode!r} (one of {', '.join(MODES)})")
    shown = [" ".join(t.split()) for t in thoughts if t.strip()]
    if not shown:
        return Extract("", None)
    parsed = [parse_thought(t) for t in shown]
    final_sentences, fragment = parsed[-1]
    last_sentence = next((s[-1] for s, _ in reversed(parsed) if s), "")
    if mode == "last_sentence":
        text = last_sentence or fragment
    elif mode == "last_words":
        text = fragment or (final_sentences[-1] if final_sentences else "")
    else:
        text = shown[-1]
    epitaph, cut = truncate_tail(text, max_chars)
    last_words = truncate_tail(fragment, max_chars)[0] if fragment else None
    return Extract(epitaph, last_words, cut)


# ---------------------------------------------------------------------------------------
# the filter


# Links, e-mail addresses and bare domains: nothing posted may point anywhere.
_LINK = re.compile(
    r"(?i)(?:https?://|www\.)\S+"
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    r"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|ai|co|me|ly|gg|app|dev|xyz|info|biz)\b"
    r"(?:/\S*)?"
)
_TAGS = re.compile(r"[@#＠＃]")


@dataclass(frozen=True)
class Blocklist:
    """Lower-case words or phrases that withhold an epitaph; a trailing * matches any ending."""

    entries: tuple[str, ...] = ()
    _patterns: tuple[re.Pattern[str], ...] = field(default=(), repr=False, compare=False)

    @classmethod
    def of(cls, entries: list[str] | tuple[str, ...]) -> Blocklist:
        """Compile `entries` (whole words; "kill*" matches "killing")."""
        pats: list[re.Pattern[str]] = []
        for entry in entries:
            words = entry.strip().lower().split()
            if not words:
                continue
            body = r"\s+".join(
                re.escape(w[:-1]) + r"\w*" if w.endswith("*") else re.escape(w) for w in words
            )
            pats.append(re.compile(rf"(?<!\w){body}(?!\w)"))
        return cls(tuple(entries), tuple(pats))

    def hit(self, text: str) -> bool:
        """Whether `text` contains any entry."""
        low = text.lower()
        return any(p.search(low) for p in self._patterns)


def load_blocklist(language: str = "en", config_dir: Path | None = None) -> Blocklist:
    """`[afterlife] blocklist` of the language pack; empty when the pack has none.

    Raises ConfigError for a pack that is not valid TOML or a blocklist that is not a list.
    """
    path = (config_dir or CONFIG_DIR) / "lang" / f"{language}.toml"
    if not path.exists():
        return Blocklist()
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    section: object = raw.get("afterlife", {})
    entries: object = []
    if isinstance(section, dict):
        entries = section.get("blocklist", [])  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: [afterlife] blocklist must be a list of strings")
    items: list[object] = entries  # pyright: ignore[reportUnknownVariableType]
    return Blocklist.of([str(e) for e in items])


@dataclass(frozen=True)
class Filtered:
    """The cleaned text, and why it is withheld (None: it may be posted)."""

    text: str
    reason: str | None
    stripped: tuple[str, ...] = ()


def clean(text: str, blocklist: Blocklist) -> Filtered:
    """Strip links, @ and #; withhold a text left empty or naming a blocklisted word."""
    stripped: list[str] = []
    out = unicodedata.normalize("NFC", text)
    if _LINK.search(out):
        out = _LINK.sub("", out)
        stripped.append("link")
    if _TAGS.search(out):
        out = _TAGS.sub("", out)
        stripped.append("tag")
    out = " ".join(out.split())
    if not re.search(r"[^\W\d_]", out):
        return Filtered(out, "empty", tuple(stripped))
    if blocklist.hit(out):
        return Filtered(out, "blocklist", tuple(stripped))
    return Filtered(out, None, tuple(stripped))
