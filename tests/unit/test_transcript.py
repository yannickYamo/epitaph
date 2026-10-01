"""Transcripts: lives/<n>/{meta.json, events.jsonl, thoughts.txt, death.json} (BUILD_PLAN 6.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from epitaph import transcript
from epitaph.state import life_dir, unfinished_lives
from epitaph.transcript import Transcript, close_interrupted, read_events, thought_block


def ev(etype: str, t: float, **f: Any) -> dict[str, Any]:
    return {"v": 1, "ts": 1000 + t, "life": 4, "type": etype, "t": t, **f}


def test_events_are_written_once_per_thought(tmp_path: Path) -> None:
    tr = Transcript(tmp_path, 4)
    tr.open({"life": 4, "profile": "pi4/smoke-300"})
    assert json.loads((life_dir(tmp_path, 4) / "meta.json").read_text())["life"] == 4
    tr.write(ev("birth_loading", 0))
    assert len(read_events(tr.events_path)) == 1  # at once: a kill during the load is seen
    tr.write(ev("gen_start", 0))
    assert len(read_events(tr.events_path)) == 1  # buffered until something that matters
    tr.write(ev("birth", 0))
    assert len(read_events(tr.events_path)) == 3
    tr.write(ev("vitals", 70, reading="[host] t+01:10 · boot complete"))
    tr.write(ev("word", 80, turn=1, i=0, text="I"))
    tr.write(ev("word", 81, turn=1, i=1, text="am."))
    assert len(read_events(tr.events_path)) == 3  # words wait for the end of the thought
    tr.write(ev("thought_end", 90, turn=1, text="I am."))
    assert len(read_events(tr.events_path)) == 7
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


def test_a_death_mid_thought_comes_after_its_reading_and_words(tmp_path: Path) -> None:
    """Regression (first Pi life): the death line came before the reading of the thought it
    cut short. Now: the reading, the words the death flush showed, then the death."""
    tr = Transcript(tmp_path, 1)
    tr.open({"life": 1})
    tr.write(ev("vitals", 293, reading="[host] t+04:53"))
    tr.write(ev("death", 300, cause="deadline", lived_s=300.0, model="m"))
    tr.write(ev("word", 301, turn=9, i=0, text="Still"))
    tr.write(ev("thought_end", 302, turn=9, text="Still"))
    tr.write(ev("death_shown", 303, last_line="Still", words_total=1))
    text = tr.thoughts_path.read_text()
    assert text.index("[host] t+04:53") < text.index("Still") < text.index("-- death")
    assert text.startswith("t+04:53  [host] t+04:53\n    Still\n\nt+05:00  -- death")


def test_a_death_whose_thought_never_ends_is_still_written(tmp_path: Path) -> None:
    tr = Transcript(tmp_path, 1)
    tr.open({"life": 1})
    tr.write(ev("vitals", 293, reading="[host] t+04:53"))
    tr.write(ev("death", 300, cause="crash", lived_s=300.0, model="m"))
    assert "-- death" not in (tr.thoughts_path.read_text() if tr.thoughts_path.exists() else "")
    tr.close({"life": 1, "cause": "crash"})
    text = tr.thoughts_path.read_text()
    assert text.index("[host] t+04:53") < text.index("-- death")


def test_a_death_between_thoughts_is_written_at_once(tmp_path: Path) -> None:
    tr = Transcript(tmp_path, 1)
    tr.open({"life": 1})
    tr.write(ev("vitals", 10, reading="[host] a"))
    tr.write(ev("thought_end", 20, turn=1, text="I am."))
    tr.write(ev("death", 30, cause="crash", lived_s=30.0, model="m"))
    assert tr.thoughts_path.read_text().endswith(
        '-- death {"cause": "crash", "lived_s": 30.0, "model": "m"}\n\n'
    )


def test_a_failed_write_marks_the_transcript_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOSPC on the SD card must not take the controller down."""
    tr = Transcript(tmp_path, 1)
    tr.open({"life": 1})

    def full(path: Path, text: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(transcript, "_append", full)
    tr.write(ev("birth", 0))
    assert tr.failed is not None and "No space" in tr.failed
    tr.write(ev("thought_end", 5, turn=1, text="x"))  # dropped, no error
    tr.close({"life": 1, "cause": "crash"})
    assert json.loads((tr.dir / "death.json").read_text())["cause"] == "crash"


def test_a_life_killed_during_the_load_is_closed_on_recovery(tmp_path: Path) -> None:
    """Only meta.json was written: the folder is unfinished and closed as interrupted."""
    d = life_dir(tmp_path, 3)
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"life": 3, "model": "qwen"}))
    assert unfinished_lives(tmp_path) == [d]
    record = close_interrupted(d)
    assert record["cause"] == "interrupted" and record["model"] == "qwen"
    assert unfinished_lives(tmp_path) == []
    (tmp_path / "lives" / "empty").mkdir()
    assert unfinished_lives(tmp_path) == []  # nothing in it: not a life
