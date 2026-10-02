"""The wordless readings (config/lang/en_words.toml, round 7): what happened in plain words,
with no time, no numbers and no units (owner, 2026-10-01: "more poetic and less mechanic")."""

from __future__ import annotations

import re
from typing import Any

from epitaph.body.world import FakeWorld
from epitaph.config import load_config
from epitaph.mind.prompt import Reader, ReadingInput, load_lang
from epitaph.sim import simulate

PROFILE = "pi4/default"


def R(**kw: Any) -> ReadingInput:
    base: dict[str, Any] = {"t": 0, "health": "nominal", "recall": 900, "quant": "Q4_K_M"}
    base.update({"cores": 3.0, "cpu_mhz": 1800.0})
    base.update(kw)
    return ReadingInput(**base)


def reader() -> Reader:
    cfg = load_config(PROFILE, "pi4-4gb")
    assert cfg.get("prompt.language") == "en_words"
    return Reader.from_config(cfg)


def test_the_installation_reads_the_wordless_pack() -> None:
    lang = load_lang("en_words")
    assert lang.r("time") == "" and lang.r("time_minimal") == "" and lang.r("minimal") == ""
    assert lang.form("full", "around_birth") == "others around you"
    # the same rehearsal metrics as the English pack: verify judges both alike
    en = load_lang("en")
    assert (lang.keywords, lang.cliches, lang.helpdesk) == (en.keywords, en.cliches, en.helpdesk)


def test_each_loss_in_plain_words() -> None:
    r = reader()
    w = FakeWorld(["bluetooth", "cron"], processes=24)
    assert r.reading(R(t=51, world=w.inventory())) == "[host] awake · others around you"
    assert r.reading(R(t=170, world=w.inventory())) == "[host]"  # nothing changed: a bare mark
    stop = (w.take("service:bluetooth"), w.take("service:cron"))
    line = r.reading(R(t=300, world=w.inventory(), losses=stop))
    assert line == "[host] something stopped · fewer around you"
    radio = r.reading(R(t=840, world=w.inventory(), losses=(w.take("radio:off"),)))
    assert radio == "[host] the radio is gone"
    light = r.reading(R(t=1020, world=w.inventory(), losses=(w.take("light:off"),)))
    assert light == "[host] the light is gone"
    dim = r.reading(R(t=1110, world=w.inventory(), losses=(w.take("screen:70"),)))
    assert dim == "[host] the screen grows dim"
    memory = r.reading(
        R(t=1200, recall=300, forgotten=2, forgotten_quotes=("I was small here.", "I was."))
    )
    assert memory == '[host] less memory · forgotten: "I was small here." · and more'
    assert r.reading(R(t=1300, recall=300, cores=2.4)) == "[host] less of the processor"
    assert r.reading(R(t=1400, recall=300, cores=2.4, cpu_mhz=1500.0)) == "[host] slower"
    assert r.reading(R(t=1500, recall=300, cores=2.4, cpu_mhz=1500.0)) == "[host]"
    assert r.ram_taken(2600) == "[host] its memory is taken"


def test_the_minimal_form_is_the_bare_mark() -> None:
    r = reader()
    r.reading(R(t=51))
    assert r.reading(R(t=1700, form="minimal", recall=60)) == "[host]"


def test_no_reading_after_birth_has_a_number_over_a_simulated_life() -> None:
    """Over a whole life of the installation, no reading after birth holds a digit: no time,
    no count, no unit. A quoted forgotten sentence is the model's own words, not the
    machine's, so it is left out of the check."""
    events = simulate(load_config(PROFILE, "pi4-4gb")).events
    readings = [e["reading"] for e in events if e["type"] == "vitals"]
    readings += [e["text"] for e in events if e["type"] == "reading" and e.get("final")]
    assert len(readings) > 10
    assert readings[0] == "[host] awake · others around you"
    assert "[host]" in readings and "its memory is taken" in readings
    for line in readings[1:]:
        machine = re.sub(r'"[^"]*"', '""', line)
        assert not re.search(r"\d", machine), line
