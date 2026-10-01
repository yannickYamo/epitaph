"""Sampling for one thought: the profile's curve plus the `[sampling]` settings (BUILD_PLAN 5.5).

Temperature and `min_p` come from the schedule's knobs, so they follow the profile's curve.
The rest comes from `[sampling]` in the config:

- `top_p`, `repeat_penalty`, `dry_multiplier`: the same for the whole life.
- `latin_only`: restrict generation to Latin letters, digits and plain punctuation for the
  whole life, or, with `latin_only_from_step = N`, only from ladder step N on. At the lowest
  precision a small model can drift into other scripts (phase 0c, round 1: Qwen3 1.7B at
  Q2_K wrote "и" and "và" mid-sentence); the constraint is then worth its cost.

The DRY window (`dry_penalty_last_n`) is a server setting and lives in the backend.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from epitaph.types import Knobs, Sampling

__all__ = ["latin_only_at", "sampling_for"]


def latin_only_at(section: Mapping[str, Any], step: int) -> bool:
    """Whether generation is limited to Latin script at this ladder step.

    True when `latin_only` is set, or when `latin_only_from_step` is set and `step` has
    reached it.
    """
    if bool(section.get("latin_only", False)):
        return True
    from_step = section.get("latin_only_from_step")
    return from_step is not None and step >= int(from_step)


def sampling_for(
    section: Mapping[str, Any], knobs: Knobs, step: int, seed: int | None = None
) -> Sampling:
    """The sampling for a thought at these knobs, on the ladder step the creature runs now.

    `section` is the config's `[sampling]` table; `step` is the loaded step, which can lag
    the knobs' step while a reload is pending.
    """
    return Sampling(
        temperature=knobs.temperature,
        min_p=knobs.min_p,
        top_p=float(section.get("top_p", 1.0)),
        repeat_penalty=float(section.get("repeat_penalty", 1.1)),
        dry_multiplier=float(section.get("dry_multiplier", 0.8)),
        seed=seed,
        latin_only=latin_only_at(section, step),
        logit_bias=tuple((str(w), float(b)) for w, b in section.get("logit_bias", [])),
    )
