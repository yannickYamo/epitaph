"""mind.sampling: the thought's sampling from the knobs and `[sampling]`."""

from __future__ import annotations

from epitaph.mind.sampling import latin_only_at, sampling_for
from epitaph.types import Health, Knobs


def _knobs(temperature: float = 0.9, min_p: float = 0.05, step: int = 0) -> Knobs:
    return Knobs(
        t=0.0,
        phase="birth",
        health=Health.NOMINAL,
        recall=1000,
        step=step,
        threads=3,
        cpu_share=3.0,
        temperature=temperature,
        min_p=min_p,
        max_tokens=80,
        pause_s=3.0,
        persona_groups=5,
        mechanics=True,
        readings="full",
        letter_ms=165.0,
        jitter=0.1,
        hesitation=0.0,
    )


def test_curve_comes_from_the_knobs_and_the_rest_from_the_section() -> None:
    s = sampling_for(
        {"top_p": 0.9, "repeat_penalty": 1.2, "dry_multiplier": 0.5}, _knobs(1.3, 0.02), 0, 7
    )
    assert (s.temperature, s.min_p, s.top_p) == (1.3, 0.02, 0.9)
    assert (s.repeat_penalty, s.dry_multiplier, s.seed, s.latin_only) == (1.2, 0.5, 7, False)


def test_defaults_without_a_section() -> None:
    s = sampling_for({}, _knobs(), 0)
    assert (s.top_p, s.repeat_penalty, s.dry_multiplier, s.seed) == (1.0, 1.1, 0.8, None)


def test_latin_only_everywhere_or_from_a_step() -> None:
    assert latin_only_at({"latin_only": True}, 0)
    assert not latin_only_at({}, 2)
    assert not latin_only_at({"latin_only_from_step": 2}, 1)
    assert latin_only_at({"latin_only_from_step": 2}, 2)
    assert latin_only_at({"latin_only_from_step": 2}, 3)


def test_the_loaded_step_decides_not_the_knobs() -> None:
    # A reload that has not happened yet: the knobs say step 2, the creature runs step 1.
    section = {"latin_only_from_step": 2}
    assert not sampling_for(section, _knobs(step=2), 1).latin_only
    assert sampling_for(section, _knobs(step=2), 2).latin_only


def test_logit_bias_comes_from_the_config() -> None:
    """Silent word penalties: the clichés small models reach for, never named in the prompt."""
    s = sampling_for({"logit_bias": [[" tapestry", -10], [" realm", -5.0]]}, _knobs(), 0)
    assert s.logit_bias == ((" tapestry", -10.0), (" realm", -5.0))
    assert sampling_for({}, _knobs(), 0).logit_bias == ()
