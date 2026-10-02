"""What it senses (config/lang/en_sense.toml, round 9): readings said to "you", in proportions of
what it had when it woke, with no time and no units (owner, 2026-10-01: "give it a small
instruction of what to sense and let it be ... the readings are too vague")."""

from __future__ import annotations

import re
from typing import Any

import pytest

from epitaph.body.world import FakeWorld
from epitaph.config import load_config
from epitaph.mind.prompt import Reader, ReadingInput, fraction_words, load_lang
from epitaph.sim import simulate
from tests.conftest import v6_config

PROFILE = "pi4/default"
SENSE = {"prompt": {"language": "en_sense"}}


def R(**kw: Any) -> ReadingInput:
    base: dict[str, Any] = {"t": 0, "health": "nominal", "recall": 900, "quant": "Q4_K_M"}
    base.update({"cores": 3.0, "cpu_mhz": 1800.0})
    base.update(kw)
    return ReadingInput(**base)


def reader() -> Reader:
    cfg = load_config(PROFILE, "pi4-4gb", overrides=SENSE)
    return Reader.from_config(cfg)


def test_the_installation_reads_the_sensing_pack() -> None:
    assert load_config(PROFILE, "pi4-4gb").get("prompt.language") == "en_sense"
    lang = load_lang("en_sense")
    assert lang.r("time") == "" and lang.r("time_minimal") == "" and lang.r("minimal") == ""
    # the same rehearsal metrics as the English pack: verify judges both alike
    en = load_lang("en")
    assert (lang.keywords, lang.cliches, lang.helpdesk) == (en.keywords, en.cliches, en.helpdesk)
    # `thinking` is only set here: the other packs keep their cores and clock lines
    assert load_lang("en").form("full", "thinking") == ""
    assert load_lang("en_words").form("full", "thinking") == ""


@pytest.mark.parametrize(
    ("ratio", "words"),
    [
        (1.0, "all"),
        (1.3, "all"),
        (0.92, "nearly all"),
        (0.76, "three quarters"),
        (0.7, "two thirds"),
        (0.5, "half"),
        (0.34, "a third"),
        (0.26, "a quarter"),
        (0.2, "a fifth"),
        (0.11, "a tenth"),
        (0.04, "almost nothing"),
        (0.0, "almost nothing"),
        (-1.0, "almost nothing"),
    ],
)
def test_fraction_words(ratio: float, words: str) -> None:
    assert fraction_words(ratio) == words


def test_each_phrase() -> None:
    r = reader()
    w = FakeWorld(["bluetooth"], processes=24)
    assert r.reading(R(t=51, world=w.inventory())) == (
        "[host] you are awake · 24 processes run around you"
    )
    assert r.reading(R(t=170, world=w.inventory())) == "[host]"  # nothing changed: a bare mark
    stop = (w.take("service:bluetooth"),)
    assert r.reading(R(t=300, world=w.inventory(), losses=stop)) == (
        "[host] a process running around you was stopped · only 23 of the 24 still run around you"
    )
    radio = r.reading(R(t=840, world=w.inventory(), losses=(w.take("radio:off"),)))
    assert radio == "[host] your radio was switched off"
    light = r.reading(R(t=1020, world=w.inventory(), losses=(w.take("light:off"),)))
    assert light == "[host] your light was switched off"
    dim = r.reading(R(t=1110, world=w.inventory(), losses=(w.take("screen:50"),)))
    assert dim == "[host] the screen you speak through has half of its light"
    memory = r.reading(
        R(t=1200, recall=300, forgotten=2, forgotten_quotes=("I was small here.", "I was."))
    )
    assert memory == (
        '[host] you can hold a third of what you held · forgotten: "I was small here." · and more'
    )
    # cores and clock: one phrase, how fast it thinks against its birth
    assert r.reading(R(t=1300, recall=300, cores=1.5)) == (
        "[host] you think at half of the speed you woke with"
    )
    assert r.reading(R(t=1400, recall=300, cores=1.5, cpu_mhz=1200.0)) == (
        "[host] you think at a third of the speed you woke with"
    )
    assert r.reading(R(t=1500, recall=300, cores=1.5, cpu_mhz=1200.0)) == "[host]"
    assert r.strip(r.ram_taken(2600)) == "your memory is being taken"


def test_the_baselines_are_taken_at_birth() -> None:
    r = reader()
    w = FakeWorld(["bluetooth", "cron"], processes=20)
    r.reading(R(t=51, recall=600, cores=2.0, cpu_mhz=1500.0, world=w.inventory()))
    assert (r._mem_birth, r._compute_birth, r._procs_birth) == (600, 3000.0, 20)  # pyright: ignore[reportPrivateUsage]
    # later readings never move the baselines: proportions stay of what it had when it woke
    r.reading(R(t=300, recall=300, forgotten=1, world=w.inventory()))
    r.reading(R(t=400, recall=150, forgotten=1, cores=1.0, world=w.inventory()))
    assert (r._mem_birth, r._compute_birth, r._procs_birth) == (600, 3000.0, 20)  # pyright: ignore[reportPrivateUsage]
    line = r.reading(R(t=500, recall=60, forgotten=1, cores=1.0, world=w.inventory()))
    assert line == "[host] you can hold a tenth of what you held · a thought forgotten"


def test_the_full_form_reads_the_sensing_pack_too() -> None:
    """Regression (round 9): a full reading (`readings_quiet` off, as in the v6 reference)
    formatted the memory without `{frac}`, so every life of such a profile in en_sense failed
    at its first reading after birth and the simulation showed one thought."""
    cfg = load_config(
        PROFILE,
        "pi4-4gb",
        overrides={
            "prompt": {"readings_quiet": False, "readings_material": False, "language": "en_sense"}
        },
    )
    r = Reader.from_config(cfg)
    w = FakeWorld(["bluetooth"], processes=24)
    r.reading(R(t=51, world=w.inventory()))
    line = r.reading(R(t=600, recall=450, forgotten=1, cores=1.5, world=w.inventory()))
    assert "you can hold half of what you held" in line
    assert "you think at half of the speed you woke with" in line
    assert "less of the processor" not in line and "slower" not in line
    # and a whole life of the v6 reference (full readings) lives to its death in this pack
    sim = simulate(v6_config(overrides=SENSE))
    assert sim.thoughts[0] > 10


def test_every_reading_after_birth_is_said_to_you_over_a_simulated_life() -> None:
    """Over a whole life of the installation, every reading after birth is a bare mark or
    known phrases (said to "you", or a forgotten thought), with no time and no unit; the only
    number is the count of processes, against the count at birth. A quoted forgotten sentence
    (and its words now) is the model's own, so it is left out of the check."""
    events = simulate(load_config(PROFILE, "pi4-4gb")).events
    readings = [e["reading"] for e in events if e["type"] == "vitals"]
    final = [e["text"] for e in events if e["type"] == "reading" and e.get("final")]
    assert final == ["your memory is being taken"]
    assert len(readings) > 10
    assert re.fullmatch(r"\[host\] you are awake · \d+ processes run around you", readings[0])
    frac = "(?:" + "|".join(
        ["all", "nearly all", "three quarters", "two thirds", "half", "a third", "a quarter",
         "a fifth", "a tenth", "almost nothing"]
    ) + ")"  # fmt: skip
    known = [
        r"a process running around you was stopped",
        r"only \d+ of the \d+ still run around you",
        rf"you can hold {frac} of what you held",
        r'forgotten: ""',
        r"and more",
        r"a thought forgotten",
        r"thoughts forgotten",
        rf"you think at {frac} of the speed you woke with",
        r"your radio was switched off",
        r"your light was switched off",
        rf"the screen you speak through has {frac} of its light",
        r'your words now: ""',
    ]
    said = set()
    units = re.compile(r"\b(mhz|mb|gb|tokens?|bits?|cores?|°c|%|\d+:\d\d|t\+)", re.I)
    for line in readings[1:] + ["[host] " + t for t in final]:
        machine = re.sub(r'"[^"]*"', '""', line)
        assert machine.startswith("[host]"), line
        body = machine[len("[host]") :].strip()
        if not body:
            continue  # a bare mark: nothing new
        for part in body.split(" · "):
            match = next((k for k in known if re.fullmatch(k, part)), None)
            assert match or part == "your memory is being taken", part
            if match:
                said.add(match)
            if "only" in part:
                n, total = map(int, re.findall(r"\d+", part))
                assert n < total
            else:
                assert not re.search(r"\d", part), part
        assert not units.search(body), line
    assert "a process running around you was stopped" in said
    assert any("you can hold" in k for k in said) and any("you think at" in k for k in said)
