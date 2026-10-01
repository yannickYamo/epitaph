"""Transcripts: lives/<n>/{meta.json, events.jsonl, thoughts.txt, death.json} (BUILD_PLAN 6.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from epitaph.state import life_dir, unfinished_lives
from epitaph.transcript import Transcript, close_interrupted, read_events, thought_block


def ev(etype: str, t: float, **f: Any) -> dict[str, Any]:
    return {"v": 1, "ts": 1000 + t, "life": 4, "type": etype, "t": t, **f}


def test_events_are_written_once_per_thought(tmp_path: Path) -> None:
    tr = Transcript(tmp_path, 4)
    tr.open({"life": 4, "profile": "pi4/smoke-300"})
    assert json.loads((life_dir(tmp_path, 4) / "meta.json").read_text())["life"] == 4
    tr.write(ev("birth_loading", 0))
    assert not tr.events_path.exists()  # buffered until something that matters
    tr.write(ev("birth", 0))
    assert len(read_events(tr.events_path)) == 2
    tr.write(ev("vitals", 70, reading="[host] t+01:10 · boot complete"))
    tr.write(ev("word", 80, turn=1, i=0, text="I"))
    tr.write(ev("word", 81, turn=1, i=1, text="am."))
    assert len(read_events(tr.events_path)) == 2  # words wait for the end of the thought
    tr.write(ev("thought_end", 90, turn=1, text="I am."))
    assert len(read_events(tr.events_path)) == 6
    assert tr.thoughts_path.read_text() == "t+01:10  [host] t+01:10 · boot complete\n    I am.\n\n"
    tr.write(ev("death", 300, cause="deadline", lived_s=300.0, model="m"))
    assert '-- death {"cause": "deadline"' in tr.thoughts_path.read_text()
    tr.write(ev("death_shown", 301, last_line="I am.", words_total=2))
    tr.close({"life": 4, "cause": "deadline"})
    record = json.loads((tr.dir / "death.json").read_text())
    assert record["cause"] == "deadline" and "closed_ts" in record
    assert [e["type"] for e in read_events(tr.events_path)][-2:] == ["death", "death_shown"]
    tr.write(ev("silence", 302))  # closed: ignored
    tr.close({"life": 4, "cause": "other"})
    assert json.loads((tr.dir / "death.json").read_text())["cause"] == "deadline"
    assert unfinished_lives(tmp_path) == []


def test_thought_block_of_an_empty_thought() -> None:
    assert thought_block(65.0, "[host] t+01:05", "") == "t+01:05  [host] t+01:05\n\n"


def test_read_events_skips_torn_lines(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    p.write_text('{"type": "birth"}\n[1, 2]\n{"type": "wo')
    assert read_events(p) == [{"type": "birth"}]
    assert read_events(tmp_path / "missing.jsonl") == []


def test_close_interrupted_appends_the_death(tmp_path: Path) -> None:
    d = life_dir(tmp_path, 7)
    d.mkdir(parents=True)
    lines = [ev("birth_loading", 0, model="m"), ev("word", 12.5, text="I")]
    (d / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines) + '{"tor')
    record = close_interrupted(d)
    assert record["cause"] == "interrupted" and record["life"] == 7
    assert record["lived_s"] == 12.5 and record["words"] == 1 and record["model"] == "m"
    events = read_events(d / "events.jsonl")
    assert events[-1]["type"] == "death" and events[-1]["recovered"] is True
    assert json.loads((d / "death.json").read_text())["cause"] == "interrupted"


def test_close_interrupted_on_an_empty_folder(tmp_path: Path) -> None:
    d = tmp_path / "lives" / "odd"
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("")
    record = close_interrupted(d)
    assert record["life"] == 0 and record["lived_s"] == 0.0
