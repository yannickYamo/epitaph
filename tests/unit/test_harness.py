"""The shared fixtures work as documented in tests/conftest.py."""

from __future__ import annotations

import asyncio
from pathlib import Path

from epitaph.events import EventBus, make_event
from epitaph.types import Msg, Sampling
from tests.helpers import EventRecorder, LifeBuilder, read_events, replace_first, retext


def test_fake_backend_on_the_fake_clock(fake_backend, fake_clock, pi4_default) -> None:
    async def go() -> str:
        model = pi4_default.model()
        await fake_backend.start(model, model.quant(0), 3)
        out = ""
        async for c in fake_backend.chat([Msg("user", "[host] t+00:00")], Sampling(0.7, 0.08), 20):
            out += c.text
        return out

    text = asyncio.run(go())
    assert text and fake_clock.elapsed() > 10  # load + prompt + 20 tokens at Pi 4 speed


def test_fake_body_records(fake_body, pi4_default) -> None:
    from epitaph.clock import Schedule

    fake_body.apply(Schedule(pi4_default.profile).at(0))
    assert fake_body.applied and fake_body.vitals().cpu_c is not None


def test_recorder_on_the_bus(recorder: EventRecorder) -> None:
    bus = EventBus(port=0)
    recorder.attach(bus)
    bus.publish(make_event("birth", 3))
    bus.publish(make_event("death", 3, cause="deadline"))
    assert recorder.types() == ["birth", "death"]
    assert recorder.of("death", life=3)[0]["cause"] == "deadline"
    assert recorder.lives() == [3]
    assert recorder.types(life=4) == []


def test_recorder_writes_jsonl(recorder: EventRecorder, tmp_path: Path) -> None:
    recorder({"type": "birth", "life": 1, "_private": 1})
    path = recorder.write(tmp_path / "x" / "events.jsonl")
    assert read_events(path) == [{"type": "birth", "life": 1}]


def test_recorded_life_layout(recorded_life) -> None:
    state = recorded_life("pi4/smoke-300", lives=2)
    assert recorded_life("pi4/smoke-300", lives=2) is state  # cached
    one = read_events(state / "lives" / "000001" / "events.jsonl")
    two = read_events(state / "lives" / "000002" / "events.jsonl")
    assert {e["life"] for e in one} == {1} and {e["life"] for e in two} == {2}
    assert one[0]["type"] == "birth_loading" and one[-1]["type"] == "silence"


def test_life_builder_and_editors(life_builder) -> None:
    b: LifeBuilder = life_builder().birth()
    b.thought("one two three.", wpm=60).forget([1]).reload().erosion(4).death("oom")
    types = [e["type"] for e in b.events]
    assert types[:4] == ["birth_loading", "birth", "gen_start", "thought_start"]
    assert {"forget", "reload", "reload_done", "erosion", "death_shown"} <= set(types)
    edited = retext(b.events, lambda turn, s: s.upper() + " MORE")
    words = [e["text"] for e in edited if e["type"] == "word"]
    assert words == ["ONE", "TWO", "THREE.", "MORE"]
    assert next(e for e in edited if e["type"] == "thought_end")["text"] == "ONE TWO THREE. MORE"
    changed = replace_first(b.events, "death", cause="crash")
    assert next(e for e in changed if e["type"] == "death")["cause"] == "crash"
    assert next(e for e in b.events if e["type"] == "death")["cause"] == "oom"
