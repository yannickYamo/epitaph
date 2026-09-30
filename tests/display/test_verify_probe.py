"""The verify-life layout probe (E8) and fading at a reload (D5): BUILD_PLAN 10.3, 5.12."""

from __future__ import annotations

from functools import cache
from typing import Any

import pytest

from epitaph import verify as v
from epitaph.config import load_config
from epitaph.display.layout import (
    LifeView,
    VerifyProbe,
    ViewSettings,
    compose_flow,
    life_times,
    verify_probe,
)
from epitaph.sim import simulate

Event = dict[str, Any]


def ev(etype: str, t: float, life: int = 1, **f: Any) -> Event:
    return {"v": 1, "ts": 1000.0 + t, "life": life, "type": etype, "t": t, **f}


def words(turn: int, text: str, t: float, ms: int = 0, pause: int = 0) -> list[Event]:
    """A whole thought released at `t`: thought_start, its words, thought_end."""
    out = [ev("thought_start", t, turn=turn)]
    for i, w in enumerate(text.split()):
        out.append(
            ev("word", t, turn=turn, i=i, text=w, char_ms=[ms] * len(w), pause_after_ms=pause)
        )
    out.append(ev("thought_end", t, turn=turn, text=text))
    return out


def life(*body: list[Event] | Event, end: float) -> list[Event]:
    """A life: loading (stamped with a stale clock, like the simulator), birth, `body`, death."""
    out = [ev("birth_loading", 3600.0, model="m"), ev("birth", 0.0, model="m")]
    for part in body:
        out.extend(part if isinstance(part, list) else [part])
    out += [ev("death", end, cause="deadline"), ev("death_shown", end), ev("silence", end)]
    return out


def with_reload_forgets(events: list[Event]) -> list[Event]:
    """Add the `forget` a reload's memory cut emits (contract D5) to a simulated life.

    Replays the simulator's memory (each thought costs its tokens + 45) and, after each
    `reload`, forgets the oldest thoughts down to `recall_after`, as `sim.py` pops them. A
    life that already has these events gains nothing: its memory is already at the target.
    """
    out: list[Event] = []
    memory: list[tuple[int, int]] = []
    tokens: dict[int, int] = {}
    for e in events:
        out.append(e)
        if e["type"] == "gen_end":
            tokens[e["turn"]] = int(e.get("tokens", 0))
        elif e["type"] == "thought_end":
            memory.append((e["turn"], tokens.get(e["turn"], 0) + 45))
        elif e["type"] == "forget":
            gone = {item["turn"] for item in e["items"]}
            memory = [m for m in memory if m[0] not in gone]
        elif e["type"] == "reload":
            items: list[Event] = []
            while memory and sum(x for _, x in memory) > e["recall_after"]:
                items.append({"turn": memory.pop(0)[0], "all": True})
            if items:
                out.append({**e, "type": "forget", "items": items})
    return out


@cache
def sim_life(profile: str) -> tuple[Event, ...]:
    return tuple(simulate(load_config(profile, "pi4-4gb")).events)


# -- life times -------------------------------------------------------------------------------


def test_life_times_start_at_birth_and_never_go_back() -> None:
    events = [
        ev("birth_loading", 3600.0),
        ev("birth", 0.0),
        ev("vitals", 5.0),
        {"type": "word", "ts": 1007.0},  # no t: wall time minus the birth's
        ev("word", 6.0),  # out of order: held at the time before it
        {"type": "thought_end"},  # neither: the time before it
    ]
    assert life_times(events) == [0.0, 0.0, 5.0, 7.0, 7.0, 7.0]
    assert life_times([]) == []


# -- split words ------------------------------------------------------------------------------


def test_split_words_counts_only_words_longer_than_a_line() -> None:
    probe = VerifyProbe(cols=10)
    events = life(words(1, "a line of short words that all fit well", 1.0), end=60.0)
    assert probe.split_words(events) == 0
    long = life(words(1, "fits 0123456789abc and 0123456789", 1.0), words(2, "x" * 25, 2.0), end=60)
    assert probe.split_words(long) == 2  # 13 letters and 25 letters; exactly 10 fits


def test_split_words_uses_the_grid_charset() -> None:
    cfg = load_config(
        "pi4/compressed-2700",
        "pi4-4gb",
        overrides={"display": {"layout": "grid", "grid": [6, 16], "charset": "ascii"}},
    )
    probe = verify_probe(cfg)
    assert probe.cols == 16
    # 14 letters in unicode, 16 in ascii ("…" becomes "..."): still fits
    assert probe.split_words(life(words(1, "wait-for-it…", 1.0), end=9.0)) == 0
    assert probe.split_words(life(words(1, "waiting-for-it…", 1.0), end=9.0)) == 1


# -- bright words -----------------------------------------------------------------------------


def test_bright_words_peak_inside_the_window() -> None:
    probe = VerifyProbe(cols=48)
    events = life(
        words(1, "one two three four five", 10.0),
        words(2, "six seven eight", 200.0),
        ev("forget", 250.0, items=[{"turn": 1, "all": True}]),
        end=300.0,
    )
    # 8 bright from 200 s to 250 s, then 3 once turn 1 is forgotten
    assert probe.bright_words_last(events, 120.0) == 8
    assert probe.bright_words_last(events, 40.0) == 3
    # the window [0, 300] includes turn 1 alone at the start
    assert probe.bright_words_last(events, 1000.0) == 8
    assert probe.bright_words_last([], 120.0) == 0


def test_bright_words_count_letters_typed_not_words_released() -> None:
    probe = VerifyProbe(cols=48)
    # released at 10 s, typed one letter every 20 s: "b" starts at 30 s, "c" at 50 s
    events = life(words(1, "a b c", 10.0, ms=20_000), end=35.0)
    assert probe.end_of_life(events) == pytest.approx(70.0)  # typing goes on after death
    assert probe.bright_words_last(events, 30.0) == 3
    # a window that ends before the typing does is not what the end of a life means
    assert probe.bright_words_last(events, 25.0) == 3


def test_a_word_typed_after_a_forget_is_counted_at_its_start() -> None:
    probe = VerifyProbe(cols=48)
    events = life(
        words(1, "old words here", 1.0),
        words(2, "slow", 100.0, ms=10_000),  # types 100-140 s
        ev("forget", 110.0, items=[{"turn": 1, "all": True}]),
        end=140.0,
    )
    # before the forget: 3 old words + "slow" (started at 100 s)
    assert probe.bright_words_last(events, 60.0) == 4
    assert probe.bright_words_last(events, 25.0) == 1


def test_forgotten_words_are_not_bright_while_they_fade() -> None:
    probe = VerifyProbe(cols=48, settings=ViewSettings(fade_s=60.0))
    events = life(
        words(1, "one two three", 1.0),
        ev("forget", 50.0, items=[{"turn": 1, "upto_i": 1}]),
        end=100.0,
    )
    assert probe.bright_words_last(events, 40.0) == 1


def test_end_of_life_without_death_is_the_last_event() -> None:
    probe = VerifyProbe(cols=48)
    events = [ev("birth", 0.0), *words(1, "cut off here", 30.0)]
    assert probe.end_of_life(events) == 30.0
    assert probe.bright_words_last(events, 10.0) == 3
    with_death = [*events, ev("death", 40.0, cause="crash"), ev("vitals", 41.0)]
    assert probe.end_of_life(with_death) == 40.0


def test_verify_probe_reads_the_display_config() -> None:
    cfg = load_config(
        "pi4/compressed-2700",
        "pi4-4gb",
        overrides={"display": {"fade_seconds": 3, "line_chars": 40, "birth_card_seconds": 2}},
    )
    probe = verify_probe(cfg)
    assert probe.cols == 40
    assert probe.settings.fade_s == 3.0
    assert probe.settings.birth_card_s == 2.0
    assert v.default_layout_probe(cfg).__class__ is VerifyProbe


# -- on simulated lives -----------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["pi4/compressed-2700", "pi4/default"])
def test_simulated_lives_split_no_words(profile: str) -> None:
    cfg = load_config(profile, "pi4-4gb")
    events = list(sim_life(profile))
    assert sum(1 for e in events if e["type"] == "word") > 500
    res = v.verify_life(v.parse_life(events), cfg, "full", layout=verify_probe(cfg))
    assert res.by_name("no_split_words").status == "pass"
    assert res.by_name("no_split_words").value == 0


@pytest.mark.parametrize("profile", ["pi4/compressed-2700", "pi4/default"])
def test_simulated_lives_end_with_few_bright_words_once_reloads_forget(profile: str) -> None:
    """With a `forget` at each reload (D5) the end of a life is mostly grey.

    Without it, every thought kept through the reloads stays bright to the end."""
    cfg = load_config(profile, "pi4-4gb")
    probe = verify_probe(cfg)
    events = with_reload_forgets(list(sim_life(profile)))
    res = v.verify_life(v.parse_life(events), cfg, "full", layout=probe)
    check = res.by_name("bright_words_last_2min")
    assert check.status == "pass", check
    assert isinstance(check.value, int) and check.value <= 40
    raw = list(sim_life(profile))
    reloads = {e["t"] for e in raw if e["type"] == "reload"}
    if not reloads & {e["t"] for e in raw if e["type"] == "forget"}:
        assert probe.bright_words_last(raw, 120.0) > 40  # the simulator before contract D5


def test_verify_life_command_runs_the_probe(
    tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    events = with_reload_forgets(list(sim_life("pi4/compressed-2700")))
    f = tmp_path / "events.jsonl"
    f.write_text("".join(json.dumps(e) + "\n" for e in events))
    v.main([str(f), "--profile", "pi4/compressed-2700", "--hardware", "pi4-4gb", "--no-write"])
    out = capsys.readouterr().out
    assert "PASS    no_split_words 0" in out
    assert "PASS    bright_words_last_2min" in out
    assert "PENDING no_split_words" not in out


# -- D5: what a reload forgets fades -----------------------------------------------------------


def test_words_forgotten_in_a_reload_fade_over_fade_seconds() -> None:
    view = LifeView(ViewSettings(fade_s=6.0, birth_card=False))
    for e in [ev("birth", 0.0), *words(1, "kept long ago", 1.0), *words(2, "the last one", 2.0)]:
        view.handle(e, e["t"])
    view.handle({**ev("reload", 100.0), "from": "Q6_K", "to": "Q4_K_M"}, 100.0)
    view.handle(ev("forget", 100.0, items=[{"turn": 1, "all": True}]), 100.0)
    assert view.mode == "reloading"

    def kinds(now: float) -> list[tuple[str, str, float]]:
        frame = compose_flow(view, now, 48, 10)
        assert frame.dim  # the reload dims everything
        return [(s.text, s.kind, round(s.fade, 2)) for s in frame.spans]

    assert kinds(103.0) == [
        ("kept", "fading", 0.5),
        ("long", "fading", 0.5),
        ("ago", "fading", 0.5),
        ("the", "live", 0.0),
        ("last", "live", 0.0),
        ("one", "live", 0.0),
    ]
    assert [k for _, k, _ in kinds(106.0)[:3]] == ["forgotten"] * 3
    assert view.bright_words(103.0) == 3
    # the fade keeps going after the reload ends
    view.handle(ev("reload_done", 104.0, seconds=4.0), 104.0)
    assert view.mode == "living"
    assert compose_flow(view, 105.0, 48, 10).spans[0].fade == pytest.approx(5 / 6)


def test_a_snapshot_during_the_reload_keeps_the_fade() -> None:
    view = LifeView(ViewSettings(fade_s=6.0))
    for e in [ev("birth", 0.0), *words(1, "gone soon", 1.0)]:
        view.handle(e, e["t"])
    view.handle({**ev("reload", 50.0), "from": "a", "to": "b"}, 50.0)
    view.handle(ev("forget", 50.0, items=[{"turn": 1, "all": True}]), 50.0)
    snap = view.snapshot(52.0)
    assert snap["mode"] == "reloading"
    assert {w["state"] for w in snap["words"]} == {"fading"}
    again = LifeView(ViewSettings(fade_s=6.0))
    again.handle(snap, 0.0)
    assert again.dimmed() and again.bright_words(0.0) == 0
