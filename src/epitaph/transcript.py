"""Transcripts: one folder per life, `lives/<n>/` (BUILD_PLAN 6.1, 7).

- `meta.json`: what the life runs with (atomic, at birth).
- `events.jsonl`: every event of the life, one JSON line each. Events are buffered and
  written (and fsynced) once per thought, at the moments that matter (birth, a reload done,
  a death) and at the close, so the SD card sees a few writes a minute, never a torn file
  that recovery cannot read.
- `thoughts.txt`: each thought with the reading it answered, appended once per thought.
- `death.json`: the death record (atomic). A folder with events and no death record is a life
  the controller never closed: recovery closes it as `interrupted` (`close_interrupted`).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from epitaph.state import atomic_write_json, life_dir
from epitaph.types import PROTOCOL_VERSION

__all__ = ["FLUSH_ON", "Transcript", "close_interrupted", "read_events", "thought_block"]

# Events after which the buffer goes to disk.
FLUSH_ON = frozenset({"birth", "thought_end", "reload_done", "death", "death_shown", "silence"})


def _fmt_t(t: float) -> str:
    m, s = divmod(max(0, int(t)), 60)
    return f"{m:02d}:{s:02d}"


def thought_block(t: float, reading: str, text: str) -> str:
    """One thought in `thoughts.txt`: the time and reading, then the words, then a blank line."""
    return f"t+{_fmt_t(t)}  {reading}\n    {text}".rstrip() + "\n\n"


def read_events(path: Path) -> list[dict[str, Any]]:
    """Every event in an events.jsonl; a torn or unreadable line is skipped."""
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict):
            out.append(e)  # pyright: ignore[reportUnknownArgumentType]
    return out


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


class Transcript:
    """The files of one life. `write` every event; `close` with the death record."""

    def __init__(self, state_dir: Path, n: int) -> None:
        """The folder `state_dir/lives/<n>` (created at `open`)."""
        self.n = n
        self.dir = life_dir(state_dir, n)
        self.events_path = self.dir / "events.jsonl"
        self.thoughts_path = self.dir / "thoughts.txt"
        self._buf: list[str] = []
        self._reading = ""
        self._reading_t = 0.0
        self.closed = False

    def open(self, meta: dict[str, Any]) -> None:
        """Create the folder and write `meta.json`."""
        self.dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.dir / "meta.json", meta)

    def write(self, e: dict[str, Any]) -> None:
        """Take one event; flush at the moments in FLUSH_ON."""
        if self.closed:
            return
        self._buf.append(json.dumps(e) + "\n")
        etype = e.get("type")
        if etype == "vitals":
            self._reading = str(e.get("reading", ""))
            self._reading_t = float(e.get("t", 0.0))
        elif etype == "thought_end":
            self.flush()
            _append(
                self.thoughts_path,
                thought_block(self._reading_t, self._reading, str(e.get("text", ""))),
            )
            return
        elif etype == "death":
            extra = {k: v for k, v in e.items() if k in ("cause", "lived_s", "model")}
            line = f"t+{_fmt_t(float(e.get('t', 0.0)))}  -- death {json.dumps(extra)}\n\n"
            self.flush()
            _append(self.thoughts_path, line)
            return
        if etype in FLUSH_ON:
            self.flush()

    def flush(self) -> None:
        """Append the buffered events to events.jsonl and fsync it."""
        if not self._buf:
            return
        _append(self.events_path, "".join(self._buf))
        self._buf = []

    def close(self, record: dict[str, Any]) -> None:
        """Flush, then write the death record `death.json` atomically."""
        if self.closed:
            return
        self.flush()
        atomic_write_json(self.dir / "death.json", {**record, "closed_ts": round(time.time(), 3)})
        self.closed = True


def close_interrupted(d: Path) -> dict[str, Any]:
    """Close a life folder the controller never closed, and return its death record.

    If its events already hold a `death` (the controller was killed between the death and
    the record), that cause stands; otherwise the life is `interrupted`, and a `death` event
    is appended so replay and verify-life see the end.
    """
    events = read_events(d / "events.jsonl")
    try:
        n = int(d.name)
    except ValueError:
        n = int(events[0].get("life", 0)) if events else 0
    last_t = max((float(e.get("t", 0.0) or 0.0) for e in events), default=0.0)
    death = next((e for e in reversed(events) if e.get("type") == "death"), None)
    model = next((str(e.get("model", "")) for e in events if e.get("type") == "birth_loading"), "")
    if death is None:
        e = {
            "v": PROTOCOL_VERSION,
            "ts": round(time.time(), 3),
            "life": n,
            "type": "death",
            "t": round(last_t, 3),
            "cause": "interrupted",
            "lived_s": round(last_t, 1),
            "model": model,
            "recovered": True,
        }
        _append(d / "events.jsonl", json.dumps(e) + "\n")
        cause, lived = "interrupted", round(last_t, 1)
    else:
        cause = str(death.get("cause", "interrupted"))
        lived = float(death.get("lived_s", last_t))
    words = sum(1 for e in events if e.get("type") == "word")
    record = {
        "life": n,
        "model": model,
        "cause": cause,
        "lived_s": lived,
        "thoughts": sum(1 for e in events if e.get("type") == "thought_end"),
        "words": words,
        "recovered": True,
        "closed_ts": round(time.time(), 3),
    }
    atomic_write_json(d / "death.json", record)
    return record
