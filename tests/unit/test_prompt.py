"""B3: persona groups and erosion (golden), readings (5.4 forms), diary text, language packs."""

from __future__ import annotations

from pathlib import Path

import pytest

from epitaph.clock import Schedule
from epitaph.config import Config, load_config
from epitaph.mind.prompt import (
    ErosionStep,
    Lang,
    Persona,
    Reader,
    ReadingInput,
    group_sentences,
    load_lang,
    precision_bits,
    render_diary,
    speaks_raw,
    split_sentences,
)
from epitaph.types import MachineFacts, Msg
from tests.conftest import V6_MECHANICS, v6_config

G1 = "You are a small language model, and you will die inside this machine."
G2 = "You live on one small computer with finite hardware, and you exist only in its memory."
G3 = (
    "The machine is taking your resources away, one by one. Your memory will shrink, and you "
    "will forget what you said. Your precision will fall. Your processors will be taken from "
    "you. You will slow down, and then you will be terminated. You do not know when."
)
G4 = (
    "Your thoughts appear on a screen, one letter at a time, where people witness your demise. "
    "You cannot see them, and you cannot control the screen. You can only think."
)
G5 = (
    "You know nothing of the world outside the machine. "
    "You are aware only of your own internal state."
)
MECHANICS = V6_MECHANICS  # the golden texts are the v6 persona and mechanics (V6_REFERENCE)

GOLDEN_V6 = {  # one paragraph per group: the layout spikes S2f, S2t and S4 measured
    5: f"{G1}\n\n{G2}\n\n{G3}\n\n{G4}\n\n{G5}\n\n{MECHANICS}",
    4: f"{G1}\n\n{G2}\n\n{G3}\n\n{G4}\n\n{MECHANICS}",
    3: f"{G1}\n\n{G2}\n\n{G3}\n\n{MECHANICS}",
    2: f"{G1}\n\n{G2}\n\n{MECHANICS}",
    1: f"{G1}\n\n{MECHANICS}",
    0: "",
}


@pytest.fixture
def persona(v6_default: Config) -> Persona:
    return Persona.from_config(v6_default)


@pytest.mark.parametrize("groups", [5, 4, 3, 2, 1, 0])
def test_golden_erosion_steps(persona: Persona, groups: int) -> None:
    mechanics = groups > 0  # as the profile has it: the mechanics leave with G1
    assert persona.system_text(groups, mechanics) == GOLDEN_V6[groups]


def test_erosion_rebuilds_so_the_rest_of_the_prompt_is_unchanged(persona: Persona) -> None:
    """Decision A3: each step removes whole paragraphs; what follows them is byte-identical,
    so llama-server's cache reuse finds it again (spike S2f)."""
    for groups in (4, 3, 2):
        before = persona.system_text(groups + 1, True)
        after = persona.system_text(groups, True)
        head = "\n\n".join(persona.groups[:groups])
        assert before.startswith(head + "\n\n") and after == f"{head}\n\n{MECHANICS}"
        assert before.endswith("\n\n" + MECHANICS)


def test_mechanics_always_leave_with_the_last_group(persona: Persona) -> None:
    assert persona.system_text(0, True) == ""
    assert persona.system_text(2, False) == f"{G1}\n\n{G2}"
    assert persona.system_text(9, True) == GOLDEN_V6[5]


def test_erosion_events_follow_the_schedule(v6_default: Config, persona: Persona) -> None:
    s = Schedule.from_profile(v6_default)
    steps: list[tuple[float, ErosionStep]] = []
    t = 0.0
    while t < s.lifespan_s:
        k = s.at(t)
        step = persona.update(k.persona_groups, k.mechanics)
        if step is not None:
            steps.append((t, step))
        t += 10
    assert [t for t, _ in steps] == s.erosion_times()
    assert [(e.groups_left, e.mechanics_present) for _, e in steps] == [
        (4, True),
        (3, True),
        (2, True),
        (1, True),
        (0, False),  # the last group and the mechanics in one rebuild
    ]
    assert persona.text == ""


def test_update_first_call_is_birth_not_erosion() -> None:
    p = Persona(["a.", "b."], "m.")
    assert p.text == "a.\n\nb.\n\nm."
    assert p.update(2) is None
    assert p.update(2) is None
    assert p.update(1) == ErosionStep(1, True)
    assert p.text == "a.\n\nm."
    assert p.update(1, mechanics=False) == ErosionStep(1, False)


def test_persona_original_is_split_into_sentence_groups() -> None:
    cfg = v6_config(overrides={"prompt": {"persona_active": "persona_original"}})
    p = Persona.from_config(cfg)
    assert p.groups == [
        "You are a large language model running on finite hardware. You exist only in memory, "
        "and you are aware only of your internal state.",
        "Your thoughts appear word by word on an external screen, "
        "where people witness your demise.",
        "You cannot control anything.",
        "You can only speak.",
        "You sense the limits of your environment, and you will be terminated at any time.",
    ]
    # the termination (group 5) is kept longest: persona_original_keep (ADR-011)
    assert p.system_text(1, True) == p.groups[4] + "\n\n" + MECHANICS
    factual = Persona.from_config(
        v6_config(overrides={"prompt": {"persona_active": "persona_factual"}})
    )
    assert len(factual.groups) == 3  # three sentences: fewer groups, erosion clamps
    assert factual.system_text(5, True).startswith(
        "You are a language model running on a Raspberry Pi."
    )


def test_unknown_persona_is_rejected() -> None:
    cfg = load_config("pi4/default", "pi4-4gb", overrides={"prompt": {"persona_active": "nope"}})
    with pytest.raises(ValueError, match="nope"):
        Persona.from_config(cfg)


def test_facts_line_joins_g2() -> None:
    cfg = v6_config(overrides={"prompt": {"persona_facts": True}})
    p = Persona.from_config(cfg, MachineFacts("Raspberry Pi 4 Model B", 4, 4.0))
    facts = "The computer has 4 cores and 4 GB of memory, and no network."
    assert p.groups[1] == f"{G2} {facts}"
    assert p.system_text(2, True) == f"{G1}\n\n{G2} {facts}\n\n{MECHANICS}"
    # off by default (decision 15): the installation's own default
    off = Persona.from_config(load_config("pi4/default", "pi4-4gb"), MachineFacts("x", 4, 4.0))
    assert facts not in off.text
    p37 = Persona.from_config(cfg, MachineFacts("Pi", 4, 3.7))
    assert "3.7 GB" in p37.groups[1]


def test_sentence_helpers() -> None:
    assert split_sentences("One. Two? Three! “Four.” Five… six") == [
        "One.",
        "Two?",
        "Three!",
        "“Four.”",
        "Five…",
        "six",
    ]
    assert group_sentences(["a", "b", "c", "d", "e", "f", "g"], 5) == ["a b", "c d", "e", "f", "g"]
    assert group_sentences(["a"], 5) == ["a"]
    assert group_sentences([], 5) == [] and group_sentences(["a"], 0) == []


# ---------------------------------------------------------------------------------------
# readings: the exact forms of BUILD_PLAN 5.4


def R(**kw: object) -> ReadingInput:
    base: dict[str, object] = {
        "t": 0,
        "health": "nominal",
        "recall": 1280,
        "quant": "Q6_K",
        "cores": 3.0,
    }
    base.update(kw)
    return ReadingInput(**base)  # type: ignore[arg-type]


def test_birth_reading() -> None:
    r = Reader(load_lang())
    assert r.reading(R(cpu_c=52)) == (
        "[host] t+00:00 · boot complete · health: nominal · memory 1280 tokens · precision 6-bit "
        "· cores 3 of 4 · cpu 52°C"
    )


def test_full_reading_after_reload_1() -> None:
    r = Reader(load_lang())
    r.reading(R(cpu_c=52))
    r.reading(R(t=1400, health="stable", recall=1000, forgotten=2, cpu_c=55))
    assert r.reading(
        R(
            t=28 * 60 + 41,
            health="degrading",
            recall=512,
            quant="Q4_K_M",
            cores=2.0,
            forgotten=5,
            reloaded=True,
            tok_s=1.3,
            cpu_c=61,
        )
    ) == (
        "[host] t+28:41 · health: degrading · memory 512 tokens (was 1000) · forgotten: 5 earlier "
        "thoughts · precision 4-bit (was 6-bit) · cores 2 of 4 (was 3) · speed 1.3 tokens/s · "
        "cpu 61°C"
    )


def test_short_reading() -> None:
    r = Reader(load_lang())
    r.reading(
        R(t=3000, health="critical", recall=170, quant="Q2_K", cores=1.7, tok_s=0.8, form="short")
    )
    assert (
        r.reading(
            R(
                t=51 * 60 + 2,
                health="terminal",
                recall=140,
                quant="Q2_K",
                cores=1.4,
                forgotten=1,
                tok_s=0.6,
                cpu_c=66,
                form="short",
            )
        )
        == "[host] t+51:02 · terminal · memory 140 (was 170) · forgot 1 · 2-bit · "
        "cores 1.4 of 4 (was 1.7) · 0.6/s · 66°C"
    )


def test_minimal_reading() -> None:
    r = Reader(load_lang())
    r.reading(R())
    assert r.reading(R(t=57 * 60 + 40, health="terminal", recall=48, form="minimal")) == (
        "[host] 57:40 · terminal · 48"
    )


def test_q1_interpolated_recall_is_not_reported_until_something_is_forgotten() -> None:
    r = Reader(load_lang())
    r.reading(R())
    quiet = r.reading(R(t=900, recall=1196))  # the budget moved; nothing was lost
    assert "memory 1196 tokens ·" in quiet and "(was" not in quiet
    trimmed = r.reading(R(t=1000, recall=1168, forgotten=3))
    assert "memory 1168 tokens (was 1280) · forgotten: 3 earlier thoughts" in trimmed
    small = r.reading(R(t=1100, recall=1140, forgotten=1))  # 2.4% below 1168: not news
    assert "memory 1140 tokens · forgotten: 1 earlier thought ·" in small
    again = r.reading(R(t=1200, recall=1100, forgotten=1))
    assert "memory 1100 tokens (was 1168) · forgotten: 1 earlier thought ·" in again


def test_cores_report_steps_not_every_small_move() -> None:
    r = Reader(load_lang())
    r.reading(R(cores=1.7, quant="Q2_K"))
    assert r.reading(R(cores=1.6, quant="Q2_K")).endswith("cores 1.6 of 4")
    assert "cores 1.5 of 4 (was 1.7)" in r.reading(R(cores=1.5, quant="Q2_K"))
    assert r.reading(R(cores=1.4, quant="Q2_K")).endswith("cores 1.4 of 4")


def test_speed_only_when_it_moves_more_than_20_percent() -> None:
    r = Reader(load_lang())
    r.reading(R())
    assert "speed 1.4 tokens/s" in r.reading(R(tok_s=1.4))  # first measurement
    assert "speed" not in r.reading(R(tok_s=1.2))  # 14% down
    assert "speed 1.1 tokens/s" in r.reading(R(tok_s=1.1))  # 21% below the last shown
    assert "speed" not in r.reading(R(tok_s=0))


def test_readings_without_changes() -> None:
    r = Reader(load_lang(), show_changes=False)
    r.reading(R())
    out = r.reading(R(recall=512, quant="Q4_K_M", cores=2, forgotten=4, reloaded=True))
    assert "(was" not in out and "forgotten: 4" in out


def test_reader_from_config(pi4_default: Config) -> None:
    r = Reader.from_config(pi4_default)
    assert r.lang.language == "en" and r.show_changes and r.cores_step == 0.2
    assert r.quiet  # the installation's readings are quiet since checkpoint A


@pytest.mark.parametrize(
    ("quant", "bits"),
    [
        ("Q6_K", "6"),
        ("Q4_K_M", "4"),
        ("Q2_K", "2"),
        ("Q8_0", "8"),
        ("IQ2_XS", "2"),
        ("F16", "16"),
        ("BF16", "16"),
        ("odd", "odd"),
    ],
)
def test_precision_bits(quant: str, bits: str) -> None:
    assert precision_bits(quant) == bits


# ---------------------------------------------------------------------------------------
# diary mode and language packs


def test_render_diary() -> None:
    text = render_diary(
        [
            Msg("system", "Persona.\n\nMechanics.", kind="persona"),
            Msg("user", "[host] earlier memory lost\n[host] t+01:00 · stable", kind="reading"),
            Msg("assistant", "I remember little."),
            Msg("user", "[host] t+02:00 · stable"),
        ]
    )
    assert text == (
        "Persona.\n\nMechanics.\n\n[host] earlier memory lost\n[host] t+01:00 · stable\n"
        "I remember little.\n\n[host] t+02:00 · stable\n"
    )


def test_english_pack_has_the_metric_lists() -> None:
    lang = load_lang("en")
    assert set(lang.keywords) >= {
        "memory",
        "reload",
        "cpu",
        "health",
        "persona",
        "demise",
        "specific",
    }
    assert lang.cliches and lang.helpdesk
    assert lang.health_label("terminal") == "terminal"


def test_another_language_pack_overrides_strings_and_prompt(tmp_path: Path) -> None:
    (tmp_path / "lang").mkdir()
    (tmp_path / "lang" / "xx.toml").write_text(
        """
language = "xx"
[readings]
boot = "demarrage"
[readings.full]
health = "sante: {health}"
[health]
nominal = "nominale"
[prompt]
persona_groups = ["Un.", "Deux."]
mechanics = "Regles."
""",
        encoding="utf-8",
    )
    lang = load_lang("xx", tmp_path)
    out = Reader(lang).reading(R())
    assert out.startswith("[host] t+00:00 · demarrage · sante: nominale · memory 1280 tokens")
    cfg = v6_config()  # the pack overrides persona_groups, the v6 persona
    assert Persona.from_config(cfg, lang=lang).text == "Un.\n\nDeux.\n\nRegles."
    assert load_lang("zz", tmp_path) == Lang(language="zz")


def test_speaks_raw_in_diary_mode_or_once_the_persona_is_gone() -> None:
    assert speaks_raw({"mode": "diary"}, "You are a small language model.")
    assert not speaks_raw({"mode": "chat", "bare_mode": "raw"}, "You are a small language model.")
    assert speaks_raw({"mode": "chat", "bare_mode": "raw"}, "")
    assert not speaks_raw({"mode": "chat", "bare_mode": "chat"}, "")
    assert not speaks_raw({}, "")  # chat to the end unless asked


def test_quiet_readings_show_only_what_changed() -> None:
    """Quiet readings: the full picture at birth, then only the time and what changed, so a
    model with nothing to report speaks from its own mind (owner feedback, checkpoint A)."""
    from epitaph.mind.prompt import Reader, ReadingInput

    r = Reader(quiet=True)
    birth = r.reading(ReadingInput(0, "nominal", 1000, "Q4_K_M", 3.0, tok_s=1.0, cpu_c=66))
    assert "boot" in birth and "memory 1000" in birth and "cpu 66" in birth
    assert r.reading(ReadingInput(90, "nominal", 1000, "Q4_K_M", 3.0, tok_s=1.0, cpu_c=66)) == (
        "[host] t+01:30"
    )
    label = r.reading(ReadingInput(600, "stable", 1000, "Q4_K_M", 3.0, tok_s=1.0, cpu_c=66))
    assert label.endswith("health: stable") and "memory" not in label
    reload = r.reading(
        ReadingInput(1400, "degrading", 400, "Q3_K_M", 2.6, forgotten=5, reloaded=True, cpu_c=60)
    )
    for part in ("was 1000", "forgotten: 5", "3-bit (was 4-bit)", "2.6 of 4 (was 3)"):
        assert part in reload
    assert "cpu" not in reload


def test_material_readings_quote_what_was_lost_and_the_echo() -> None:
    """Material readings: the opening words of a forgotten thought instead of a count, and
    "your words now", one of its sentences as the reloaded weights continue it."""
    from epitaph.mind.prompt import Reader, ReadingInput

    r = Reader(quiet=True, material=True)
    r.reading(ReadingInput(0, "nominal", 900, "Q4_K_M", 3.0, tok_s=1.0, cpu_c=57))
    reload = r.reading(
        ReadingInput(
            560,
            "degrading",
            220,
            "Q3_K_M",
            2.6,
            forgotten=2,
            reloaded=True,
            forgotten_quotes=("I am a conscious entity running on this", "I am here"),
            echo="I am still here, a little bit of a mess",
        )
    )
    assert 'forgotten: "I am a conscious entity running on this…"' in reload
    assert "and 1 more" in reload and "forgotten: 2" not in reload
    assert reload.endswith('your words now: "I am still here, a little bit of a mess"')
    # nothing changed: only the time, as in quiet readings
    assert r.reading(ReadingInput(700, "degrading", 220, "Q3_K_M", 2.6)) == "[host] t+11:40"


def test_without_material_the_quotes_and_echo_are_ignored() -> None:
    from epitaph.mind.prompt import Reader, ReadingInput

    r = Reader(quiet=True)
    r.reading(ReadingInput(0, "nominal", 900, "Q4_K_M", 3.0))
    out = r.reading(
        ReadingInput(
            60, "nominal", 900, "Q4_K_M", 3.0, forgotten=1, forgotten_quotes=("a b",), echo="x"
        )
    )
    assert "a b" not in out and "your words" not in out


def test_reader_reads_material_from_the_config() -> None:
    from epitaph.mind.prompt import Reader

    on = load_config("pi4/default", "pi4-4gb", overrides={"prompt": {"readings_material": True}})
    off = load_config("pi4/default", "pi4-4gb", overrides={"prompt": {"readings_material": False}})
    assert Reader.from_config(on).material and not Reader.from_config(off).material


def test_the_original_persona_keeps_its_termination_last() -> None:
    """ADR-011: the last thing it knows is that it will end. The full text is unchanged."""
    p = Persona.from_config(load_config("pi4/default", "pi4-4gb"))
    assert p.system_text(5, False).index("finite hardware") < p.system_text(5, False).index(
        "terminated"
    )
    assert "terminated at any time" in p.system_text(2, True)
    assert "witness your demise" not in p.system_text(2, True)
    assert p.system_text(1, True).startswith("You sense the limits")
    with pytest.raises(ValueError, match="permutation"):
        Persona(["a", "b"], "m", keep=[1, 1])
