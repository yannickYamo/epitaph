"""The world taken from the outside in (ADR-031): the World contract and FakeWorld, the
profile's `world` actions, the readings that name each loss, the controller's `world` and
`reading` events, the freshness guard and the repetition metrics."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from epitaph import verify as v
from epitaph.body.world import (
    FakeWorld,
    NoWorld,
    TakeResult,
    WorldState,
    make_world,
    parse_action,
)
from epitaph.clock import Schedule
from epitaph.config import ConfigError, load_config
from epitaph.mind.prompt import Reader, ReadingInput
from epitaph.mind.sampling import freshness_bias, opening_words, sampling_for
from epitaph.sim import simulate

# -- the World contract ---------------------------------------------------------------------


def test_parse_action_knows_the_four_kinds() -> None:
    assert parse_action("service:bluetooth") == ("service", "bluetooth")
    assert parse_action("radio:off") == ("radio", "off")
    assert parse_action("light:off") == ("light", "off")
    assert parse_action("screen:70") == ("screen", "70")
    for bad in ("radio:on", "screen:101", "fan:off", "service:", "screen:dim"):
        with pytest.raises(ValueError):
            parse_action(bad)


def test_fake_world_takes_and_restores() -> None:
    w = FakeWorld(["bluetooth", "cron"], processes=24)
    assert w.inventory() == WorldState(("bluetooth", "cron"), 24, "on", "on", 100)
    assert w.take("service:bluetooth") == TakeResult("service:bluetooth", True, "stopped bluetooth")
    assert w.inventory().services == ("cron",) and w.inventory().processes == 23
    assert not w.take("service:bluetooth").performed  # already stopped
    assert not w.take("service:sshd").performed  # never running
    assert w.take("radio:off").performed and not w.take("radio:off").performed
    assert w.take("light:off").performed
    assert w.take("screen:70").performed and not w.take("screen:70").performed
    assert w.inventory() == WorldState(("cron",), 23, "off", "off", 70)
    w.restore()
    assert w.inventory() == WorldState(("bluetooth", "cron"), 24, "on", "on", 100)


def test_a_refused_action_is_not_performed() -> None:
    w = FakeWorld(["bluetooth"], fail=["radio:off"])
    res = w.take("radio:off")
    assert not res.performed and w.inventory().radio == "on"


def test_make_world_follows_the_config() -> None:
    assert make_world({"enabled": False}) is None
    fake = make_world({"enabled": True, "services": ["cron"], "fake_processes": 30})
    assert isinstance(fake, FakeWorld) and fake.inventory().processes == 30
    real = make_world({"enabled": True, "services": ["cron"], "helper": "/x/helper"})
    # without the helper's implementation nothing is ever taken: no invented loss
    assert real is not None and not real.take("service:cron").performed


def test_no_world_reports_only_true_facts(tmp_path: Path) -> None:
    w = NoWorld()
    s = w.inventory()
    assert s.radio is None and s.light is None and s.screen is None and s.services == ()
    assert not w.take("radio:off").performed


# -- the profile ---------------------------------------------------------------------------


def test_world_actions_happen_once_at_their_keyframe() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    sch = Schedule(cfg.profile)
    times = dict(sch.world_times())
    assert times[7 * 60] == ("service:bluetooth",)
    assert times[14 * 60] == ("radio:off",)
    # not carried forward: the keyframe after one with world actions has its own (or none)
    kfs = cfg.profile.keyframes
    assert kfs[1].world == ("service:bluetooth",) and kfs[2].world == ("service:cron",)
    assert kfs[3].world == ()


def _profile(tmp_path: Path, world: str) -> Path:
    root = tmp_path / "config"
    import shutil

    from epitaph.config import CONFIG_DIR

    shutil.copytree(CONFIG_DIR, root)
    (root / "profiles" / "pi4" / "w.toml").write_text(
        f"""lifespan = "10:00"
death = "none"
[[keyframe]]
at = "0:00"
phase = "a"
health = "nominal"
recall = 500
step = 0
threads = 3
cpu_share = 3.0
temperature = 0.7
min_p = 0.08
max_tokens = 70
pause_s = 3
persona_groups = 5
mechanics = true
readings = "full"
letter_ms = 165
jitter = 0.1
hesitation = 0.0
[[keyframe]]
at = "5:00"
phase = "b"
world = {world}
""",
        encoding="utf-8",
    )
    return root


def test_a_bad_action_or_an_unlisted_service_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import epitaph.config as c

    monkeypatch.setattr(c, "CONFIG_DIR", _profile(tmp_path, '["fan:off"]'))
    with pytest.raises(ConfigError, match="unknown world action"):
        load_config("pi4/w", "pi4-4gb")
    monkeypatch.setattr(c, "CONFIG_DIR", _profile(tmp_path / "b", '["service:sshd"]'))
    with pytest.raises(ConfigError, match="not in \\[world\\] services"):
        load_config("pi4/w", "pi4-4gb")


# -- the readings ---------------------------------------------------------------------------


def R(**kw: Any) -> ReadingInput:
    base: dict[str, Any] = {"t": 0, "health": "nominal", "recall": 900, "quant": "Q4_K_M"}
    base.update({"cores": 3.0, "cpu_mhz": 1800.0})
    base.update(kw)
    return ReadingInput(**base)


def test_the_birth_reading_is_the_inventory() -> None:
    r = Reader(quiet=True, material=True, precision=False)
    w = FakeWorld(["bluetooth"], processes=24).inventory()
    assert r.reading(R(world=w)) == (
        "[host] t+00:00 · awake · memory 900 tokens · cores 3 of 4 · clock 1800 MHz · radio on "
        "· light on · screen 100% · around you: 24 processes"
    )


def test_a_reading_names_each_loss_and_only_real_ones() -> None:
    r = Reader(quiet=True, material=True, precision=False)
    w = FakeWorld(["bluetooth", "cron"], processes=24)
    r.reading(R(world=w.inventory()))
    losses = (w.take("service:bluetooth"), w.take("service:sshd"), w.take("radio:off"))
    line = r.reading(R(t=430, world=w.inventory(), losses=losses))
    assert line == "[host] t+07:10 · stopped: bluetooth · radio off · around you: 23 processes"
    assert "sshd" not in line  # the truth rule: never an invented loss
    dim = r.reading(R(t=600, world=w.inventory(), losses=(w.take("screen:70"),)))
    assert dim == "[host] t+10:00 · screen 70% (was 100%)"
    assert "health" not in dim


def test_a_full_reading_thins_out_as_its_sources_go() -> None:
    r = Reader(precision=False)
    w = FakeWorld(["bluetooth"], processes=24)
    assert "radio on" in r.reading(R(world=w.inventory()))
    w.take("radio:off")
    w.take("light:off")
    later = r.reading(R(t=60, world=w.inventory()))
    assert "radio" not in later and "light" not in later and "screen 100%" in later


def test_the_ram_reading_and_the_screen_text() -> None:
    r = Reader()
    line = r.ram_taken(2650)
    assert line == "[host] ram 2650 MB taken" and r.strip(line) == "ram 2650 MB taken"


# -- the freshness guard --------------------------------------------------------------------


def test_opening_words_skip_the_pronoun_and_stop_words() -> None:
    assert opening_words(["I", "am", "still", "here,", "fading."], 3) == ["still"]
    assert opening_words(["Something", "earlier", "is", "gone"], 3) == ["something", "earlier"]
    assert opening_words(["I'm", "extraordinarily", "tired"], 3) == ["tired"]  # too long: skip


def test_freshness_bias_targets_the_last_openings() -> None:
    section = {"freshness_bias": -3.0, "freshness_thoughts": 2, "freshness_words": 3}
    recent = [["Quiet", "now."], ["I", "am", "still", "here."], ["Fading", "slowly", "I"]]
    bias = dict(freshness_bias(section, recent))
    assert set(bias) == {" still", " Still", " fading", " Fading", " slowly", " Slowly"}
    assert set(bias.values()) == {-3.0} and " quiet" not in bias  # only the last two
    assert freshness_bias({**section, "freshness_bias": 0}, recent) == ()
    assert freshness_bias(section, []) == ()


def test_the_guard_stacks_on_the_static_bias_with_twins() -> None:
    cfg = load_config("pi4/default", "pi4-4gb")
    sch = Schedule(cfg.profile)
    section = cfg.section("sampling")
    s = sampling_for(section, sch.at(0), 0, extra_bias=((" still", -3.0), (" here", -3.0)))
    bias = dict(s.logit_bias)
    # stacked on the static bias, and every spaced word with its space-less twin
    assert bias[" still"] == -9.0 and bias["still"] == -9.0 and bias[" here"] == -3.0
    assert bias["here"] == -3.0 and bias[" digital"] == -6.0 and bias["digital"] == -6.0


# -- the repetition metrics ------------------------------------------------------------------


def test_repetition_metrics_count_shared_openings_and_sentences() -> None:
    texts = [
        "I am still here. The fan hums.",
        "I am still here. Something is gone.",
        "I am still thinking. The fan hums.",
        "Quiet now, nothing moves.",
    ]
    m = v.repetition_metrics(texts)
    assert m["max_thoughts_per_opening"] == 3 and m["most_shared_opening"] == "i am still"
    assert m["repeated_opening_share"] == 0.75
    # "i am still here" twice; "the fan hums" has three words: too short to count
    assert m["repeated_sentences"] == 1
    long = ["The fan hums in the dark.", "Then: the fan hums in the dark.", "Other words here."]
    assert v.repeated_sentences(long) == []  # different sentences
    same = ["The fan hums in the dark. A.", "B. The fan hums in the dark."]
    assert v.repeated_sentences(same) == ["the fan hums in the dark"]


# -- the life: world and reading events ------------------------------------------------------


@pytest.fixture(scope="module")
def life_events() -> list[dict[str, Any]]:
    return simulate(load_config("pi4/default", "pi4-4gb"), lives=2).events


def test_world_events_come_at_their_keyframes(life_events: list[dict[str, Any]]) -> None:
    first = [e for e in life_events if e["life"] == 1]
    world = [e for e in first if e["type"] == "world"]
    assert world[0]["action"] == "service:bluetooth" and world[0]["t"] == pytest.approx(420)
    assert all(e["performed"] for e in world)
    assert world[0]["state"]["processes"] == 23


def test_each_reading_comes_right_before_its_thought(life_events: list[dict[str, Any]]) -> None:
    first = [e for e in life_events if e["life"] == 1]
    seen: set[int] = set()
    for i, e in enumerate(first):
        if e["type"] == "word" and e["turn"] not in seen:
            seen.add(e["turn"])
            before = [x for x in first[:i] if x["type"] in ("reading", "word")]
            assert before[-1]["type"] == "reading" and before[-1]["turn"] == e["turn"]
            assert not before[-1]["text"].startswith("[host]")
    losses = [e for e in first if e["type"] == "reading" and "stopped: bluetooth" in e["text"]]
    assert losses and losses[0]["t"] > 420  # the loss is shown after it happened


def test_the_ram_reading_comes_before_the_death(life_events: list[dict[str, Any]]) -> None:
    first = [e for e in life_events if e["life"] == 1]
    final = [e for e in first if e["type"] == "reading" and e.get("final")]
    death = next(i for i, e in enumerate(first) if e["type"] == "death")
    assert len(final) == 1 and final[0]["text"] == "ram 2600 MB taken"
    assert first.index(final[0]) < death


def test_the_world_is_restored_for_every_life() -> None:
    from epitaph.clock import run_virtual
    from epitaph.costmodel import load_costs
    from epitaph.sim import make_controller

    cfg = load_config("pi4/default", "pi4-4gb")
    world = FakeWorld(list(cfg.get("world.services")), fail=["light:off"])
    events: list[dict[str, Any]] = []

    async def main(clock: Any) -> None:
        ctl = make_controller(
            cfg, clock, lives=2, publish=events, costs=load_costs(cfg), world=world
        )
        await ctl.run()

    run_virtual(main)
    assert world.restores == 3  # at the start and after each death
    born = [e for e in events if e["type"] == "world" and e["life"] == 2]
    assert born[0]["state"]["processes"] == 23  # the second life starts from a whole world
    refused = [e for e in events if e["type"] == "world" and not e["performed"]]
    assert {e["action"] for e in refused} == {"light:off"}
    readings = [e["text"] for e in events if e["type"] == "reading"]
    assert not any("light off" in r for r in readings)


def test_the_pi_overlay_takes_the_real_world() -> None:
    """With a helper configured the world is PiWorld; on the laptop it is simulated."""
    from epitaph.body.pi_world import PiWorld
    from epitaph.body.world import FakeWorld, make_world
    from epitaph.config import load_config

    assert isinstance(make_world(load_config("pi4/default", "pi4-4gb").section("world")), PiWorld)
    assert isinstance(make_world(load_config("pi4/default", "dev").section("world")), FakeWorld)


def test_spare_readings_leave_less_to_recite() -> None:
    """A short birth, losses without service names, no speed (dread plan, voice round 2)."""
    from epitaph.body.world import TakeResult, WorldState
    from epitaph.mind.prompt import Reader, ReadingInput

    w = WorldState(services=("cron",), processes=24, radio="on", light="on", screen=100)
    r = Reader(quiet=True, spare_birth=True, names=False, speed=False)
    birth = r.reading(ReadingInput(51, "nominal", 900, "Q4_K_M", 3.0, tok_s=1.2, world=w))
    assert birth == "[host] t+00:51 · awake · around you: 24 processes"
    later = r.reading(
        ReadingInput(
            430,
            "nominal",
            900,
            "Q4_K_M",
            3.0,
            tok_s=0.6,
            world=WorldState(processes=22),
            losses=(TakeResult("service:cron", True), TakeResult("service:bluetooth", True)),
        )
    )
    assert "something stopped" in later and "cron" not in later and "tokens/s" not in later
    assert later.count("something stopped") == 1 and "around you: 22 processes" in later
