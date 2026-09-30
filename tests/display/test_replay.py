"""D3: replay republishes events.jsonl at the original cadence (BUILD_PLAN 4 decision 8)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from epitaph.display import replay
from epitaph.display.layout import LifeView, compose_flow
from epitaph.events import EventBus, subscribe


def write_jsonl(path: Path, events: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path


class FakeSleep:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, s: float) -> None:
        self.waits.append(s)


def test_resolve_events(tmp_path: Path) -> None:
    life = tmp_path / "lives" / "000012"
    life.mkdir(parents=True)
    f = write_jsonl(life / "events.jsonl", [{"type": "birth", "life": 12}])
    assert replay.resolve_events("12", tmp_path) == f
    assert replay.resolve_events(str(life), tmp_path) == f
    assert replay.resolve_events(str(f)) == f
    with pytest.raises(FileNotFoundError, match="life 13"):
        replay.resolve_events("13", tmp_path)
    with pytest.raises(FileNotFoundError):
        replay.resolve_events("no/such/thing", tmp_path)


def test_default_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EPITAPH_STATE_DIR", str(tmp_path))
    assert replay.default_state_dir() == tmp_path
    monkeypatch.delenv("EPITAPH_STATE_DIR")
    assert replay.default_state_dir().name in ("epitaph",)


def test_load_events_skips_torn_lines(tmp_path: Path) -> None:
    p = tmp_path / "e.jsonl"
    p.write_text(
        '{"type": "birth", "life": 1}\n\n[1, 2]\n{"type": "word", "text": "a"}\n{"type": "wo'
    )
    assert [e["type"] for e in replay.load_events(p)] == ["birth", "word"]


def test_timeline_follows_the_life_clock(sim_events: list[dict[str, Any]]) -> None:
    keys = replay.timeline(sim_events)
    assert keys == sorted(keys)
    ts = [float(e["t"]) for e in sim_events]
    # within the life the gaps equal the life clock's gaps (the load before birth adds nothing)
    for n in range(2, len(ts)):
        if ts[n] >= ts[n - 1]:
            assert keys[n] - keys[n - 1] == pytest.approx(ts[n] - ts[n - 1])
    assert keys[-1] == pytest.approx(ts[-1] - ts[1] + keys[1])


def test_timeline_between_lives_uses_the_silence(
    sim_events_two_lives: list[dict[str, Any]],
) -> None:
    ev = sim_events_two_lives
    keys = replay.timeline(ev)
    first2 = next(n for n, e in enumerate(ev) if e["life"] == 2)
    silence = next(e for e in ev if e["type"] == "silence")
    assert keys[first2] - keys[first2 - 1] == pytest.approx(silence["seconds"])


def test_timeline_from_ts_when_no_t() -> None:
    ev = [
        {"type": "birth", "life": 1, "ts": 100.0},
        {"type": "word", "life": 1, "ts": 101.5},
        {"type": "word", "life": 1, "ts": 101.0},  # clock skew never goes backwards
        {"type": "birth", "life": 2, "ts": 200.0},
    ]
    assert replay.timeline(ev) == [0.0, 1.5, 1.5, 100.5]


def test_start_index_and_scaled() -> None:
    ev = [
        {"type": "birth_loading", "t": 9999.0},
        {"type": "birth", "t": 0.0},
        {"type": "word", "t": 10.0},
        {"type": "word", "t": 30.0},
    ]
    assert replay.start_index(ev, 0) == 0
    assert replay.start_index(ev, 20) == 3
    assert replay.start_index(ev, 99999) == 4
    w = {"type": "word", "char_ms": [100, 50], "pause_after_ms": 700, "hesitate_before_ms": 400}
    assert replay.scaled(w, 2) == {
        "type": "word",
        "char_ms": [50, 25],
        "pause_after_ms": 350,
        "hesitate_before_ms": 200,
    }
    assert replay.scaled(w, 1) is w
    assert replay.scaled({"type": "birth"}, 2) == {"type": "birth"}


async def test_republish_keeps_cadence_scaled_by_speed(sim_events: list[dict[str, Any]]) -> None:
    sleep = FakeSleep()
    out: list[dict[str, Any]] = []
    n = await replay.republish(sim_events, out.append, speed=4.0, sleep=sleep)
    assert n == len(sim_events) == len(out)
    keys = replay.timeline(sim_events)
    assert sum(sleep.waits) == pytest.approx((keys[-1] - keys[0]) / 4.0)
    words_in = [e for e in sim_events if e["type"] == "word"]
    words_out = [e for e in out if e["type"] == "word"]
    assert words_out[0]["char_ms"] == [round(x / 4) for x in words_in[0]["char_ms"]]
    with pytest.raises(ValueError):
        await replay.republish(sim_events, out.append, speed=0)


async def test_republish_from_starts_with_a_snapshot_of_that_moment(
    sim_events: list[dict[str, Any]],
) -> None:
    sleep = FakeSleep()
    out: list[dict[str, Any]] = []
    from_s = 20 * 60
    await replay.republish(
        sim_events, out.append, speed=1.0, from_s=from_s, sleep=sleep, max_gap_s=5.0
    )
    snap = out[0]
    assert snap["type"] == "snapshot" and snap["life"] == 1
    assert snap["words"] and snap["t"] >= from_s - 120
    assert out[1].get("t", 0) >= from_s
    assert max(sleep.waits) <= 5.0
    # the screen at the start of the replay equals the screen of a full play at that moment
    first = replay.start_index(sim_events, from_s)
    full = LifeView()
    for e in sim_events[:first]:
        full.handle(e, float(e["t"]))
    part = LifeView()
    part.handle(snap, 0.0)
    now_full = float(sim_events[first]["t"]) + 1e4
    assert (
        compose_flow(full, now_full, 48, 12).text_rows()
        == compose_flow(part, 1e4, 48, 12).text_rows()
    )


async def test_republish_to_a_local_bus(sim_events: list[dict[str, Any]]) -> None:
    mirror = LifeView()
    bus = EventBus(port=0, snapshot=lambda: mirror.snapshot(0.0))
    await bus.start()
    got: list[dict[str, Any]] = []
    wanted = len(sim_events[:300]) + 1

    async def reader() -> None:
        async for e in subscribe(port=bus.port):
            got.append(e)
            if len(got) == wanted:
                return

    task = asyncio.create_task(reader())
    while not bus.subscriber_count:
        await asyncio.sleep(0.01)
    await replay.republish(sim_events[:300], bus.publish, speed=1e6, mirror=mirror)
    await asyncio.wait_for(task, 5)
    assert got[0]["type"] == "snapshot"
    assert [e["type"] for e in got[1:]] == [e["type"] for e in sim_events[:300]]
    assert mirror.life == 1 and any(True for _ in mirror.words())
    await bus.stop()


def test_main_serves_and_ends(
    tmp_path: Path, sim_events: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    f = write_jsonl(tmp_path / "events.jsonl", sim_events[:200])
    assert (
        replay.main(
            [str(f), "--driver", "none", "--speed", "1000", "--max-gap", "0.001", "--port", "0"]
        )
        == 0
    )
    assert "replay serving on 127.0.0.1:" in capsys.readouterr().err


def test_main_runs_a_terminal_driver(
    tmp_path: Path, sim_events: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    import epitaph.display.app as app
    from epitaph.display.terminal import TerminalDriver

    drivers: list[TerminalDriver] = []

    def make(name: str, cfg: dict[str, Any], **opts: Any) -> TerminalDriver:
        d = TerminalDriver(out=io.StringIO(), size=(50, 10), color="none")
        drivers.append(d)
        return d

    monkeypatch.setattr(app, "make_driver", make)
    f = write_jsonl(tmp_path / "events.jsonl", sim_events[:60])
    assert replay.main([str(f), "--speed", "1000", "--max-gap", "0.001"]) == 0
    assert drivers and any(True for _ in drivers[0].view.words())


def test_main_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert replay.main([str(tmp_path / "nope.jsonl")]) == 2
    assert replay.main([str(write_jsonl(tmp_path / "x.jsonl", [])), "--from", "bad"]) == 2
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n")
    assert replay.main([str(empty)]) == 2
    assert "no events" in capsys.readouterr().err
