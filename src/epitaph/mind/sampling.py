"""Sampling for one thought: the profile's curve plus the `[sampling]` settings (BUILD_PLAN 5.5).

Temperature and `min_p` come from the schedule's knobs, so they follow the profile's curve.
The rest comes from `[sampling]` in the config:

- `top_p`, `repeat_penalty`, `dry_multiplier`: the same for the whole life.
- `latin_only`: restrict generation to Latin letters, digits and plain punctuation for the
  whole life, or, with `latin_only_from_step = N`, only from ladder step N on. At the lowest
  precision a small model can drift into other scripts (phase 0c, round 1: Qwen3 1.7B at
  Q2_K wrote "и" and "và" mid-sentence); the constraint is then worth its cost.

The DRY window (`dry_penalty_last_n`) is a server setting and lives in the backend.

**The freshness guard** (`freshness_bias`): for each request, a temporary negative bias on the
distinctive first words of the last few thoughts' openings ("still", "fading", "here"), so a
thought does not open the way the last ones did. The pronoun "I" and stop words are skipped.
The bias is per request and never stops the stream; the sampling is otherwise unchanged. A
string is biased token by token by the server, so only short alphabetic words are biased (a
long word may split into pieces that other words share).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from epitaph.types import Knobs, Sampling

__all__ = ["freshness_bias", "latin_only_at", "opening_words", "sampling_for"]


def latin_only_at(section: Mapping[str, Any], step: int) -> bool:
    """Whether generation is limited to Latin script at this ladder step.

    True when `latin_only` is set, or when `latin_only_from_step` is set and `step` has
    reached it.
    """
    if bool(section.get("latin_only", False)):
        return True
    from_step = section.get("latin_only_from_step")
    return from_step is not None and step >= int(from_step)


# Words a thought's opening is never biased on: the pronoun and the glue around it.
DEFAULT_STOP_WORDS = (
    "i", "im", "ive", "id", "ill", "me", "my", "myself", "am", "is", "are", "was", "were", "be",
    "been", "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "for", "with",
    "it", "its", "this", "that", "these", "those", "there", "so", "as", "if", "now",
)  # fmt: skip
MAX_BIAS_LETTERS = 9  # longer words may split into pieces other words share
_WORD_RE = re.compile(r"[a-z]+")


def opening_words(
    words: Sequence[str], n: int, stop: Iterable[str] = DEFAULT_STOP_WORDS
) -> list[str]:
    """The distinctive words among a thought's first `n`: lower case, letters only, without
    stop words, the pronoun "I" and words too long to bias safely."""
    skip = set(stop)
    out: list[str] = []
    for w in words[: max(0, n)]:
        bare = "".join(_WORD_RE.findall(w.lower().replace("’", "").replace("'", "")))
        if bare and bare not in skip and len(bare) <= MAX_BIAS_LETTERS and bare not in out:
            out.append(bare)
    return out


def freshness_bias(
    section: Mapping[str, Any],
    recent: Sequence[Sequence[str]],
    stop: Iterable[str] = DEFAULT_STOP_WORDS,
) -> tuple[tuple[str, float], ...]:
    """The freshness guard's biases for the next request (see the module notes).

    `recent` holds the words of the thoughts already written, oldest first; the last
    `freshness_thoughts` are looked at, their first `freshness_words` words each. Each
    distinctive word is biased by `freshness_bias` as " word" (mid-text) and "Word" (the
    opening token). Nothing when the bias is 0 or no thought was written yet.
    """
    bias = float(section.get("freshness_bias", 0.0))
    k = int(section.get("freshness_thoughts", 3))
    n = int(section.get("freshness_words", 3))
    if bias == 0 or k <= 0 or not recent:
        return ()
    words: list[str] = []
    for thought in recent[-k:]:
        for w in opening_words(thought, n, stop):
            if w not in words:
                words.append(w)
    out: list[tuple[str, float]] = []
    for w in words:
        out += [(f" {w}", bias), (w.capitalize(), bias)]
    return tuple(out)


def _merge_bias(
    static: Iterable[tuple[str, float]], extra: Iterable[tuple[str, float]]
) -> tuple[tuple[str, float], ...]:
    """The static biases with the guard's added; a string in both keeps the stronger one."""
    merged: dict[str, float] = {}
    for w, b in (*static, *extra):
        merged[w] = min(b, merged[w]) if w in merged else b
    return tuple(merged.items())


def sampling_for(
    section: Mapping[str, Any],
    knobs: Knobs,
    step: int,
    seed: int | None = None,
    extra_bias: Iterable[tuple[str, float]] = (),
) -> Sampling:
    """The sampling for a thought at these knobs, on the ladder step the creature runs now.

    `section` is the config's `[sampling]` table; `step` is the loaded step, which can lag
    the knobs' step while a reload is pending. `extra_bias` is this request's freshness guard
    (`freshness_bias`), merged into the static `logit_bias`.
    """
    static = tuple((str(w), float(b)) for w, b in section.get("logit_bias", []))
    return Sampling(
        temperature=knobs.temperature,
        min_p=knobs.min_p,
        top_p=float(section.get("top_p", 1.0)),
        repeat_penalty=float(section.get("repeat_penalty", 1.1)),
        dry_multiplier=float(section.get("dry_multiplier", 0.8)),
        seed=seed,
        latin_only=latin_only_at(section, step),
        logit_bias=_merge_bias(static, extra_bias),
    )
