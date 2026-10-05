"""Transcripts: one folder per life, `lives/<n>/`.

- `meta.json`: what the life runs with (atomic, at birth).
- `events.jsonl`: every event of the life, one JSON line each. Events are buffered and
  written (and fsynced) once per thought, at the moments that matter (birth, a reload done,
  a death) and at the close, so the SD card sees a few writes a minute, never a torn file
  that recovery cannot read. `birth_loading` is flushed at once, so a controller killed
  during the load leaves a life that recovery closes.
- `thoughts.txt`: each thought with the reading it answered, appended once per thought, in
  time order: a death that lands mid-thought is listed after the words the death flush
  showed.
- `death.json`: the death record (atomic). A folder with events and no death record is a life
  the controller never closed: recovery closes it as `interrupted` (`close_interrupted`).

A write that fails (a full or broken SD card) marks the transcript `failed` and drops what
follows; it never raises into the life loop.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from epitaph.state import atomic_write_json, life_dir
from epitaph.types import PROTOCOL_VERSION

__all__ = ["FLUSH_ON", "Transcript", "close_interrupted", "read_events", "thought_block"]

log = logging.getLogger(__name__)

# Events after which the buffer goes to disk.
FLUSH_ON = frozenset(
    {"birth_loading", "birth", "thought_end", "reload_done", "death", "death_shown", "silence"}
)


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
        self._in_thought = False  # a reading was taken and its thought has not ended
        self._death_line: str | None = None  # held until the thought in flight ends
        self.closed = False
        self.failed: str | None = None  # the error that stopped the writes

    def open(self, meta: dict[str, Any]) -> None:
        """Create the folder and write `meta.json`."""
        self.dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.dir / "meta.json", meta)

    def write(self, e: dict[str, Any]) -> None:
        """Take one event; flush at the moments in FLUSH_ON.

        Never raises on a failed write: the transcript is marked `failed` and the rest of
        the life's events are dropped (the life itself goes on).
        """
        if self.closed or self.failed is not None:
            return
        try:
            self._write(e)
        except OSError as err:
            self._fail(err)

    def _fail(self, err: OSError) -> None:
        self.failed = repr(err)
        self._buf = []
        log.error("life %d: transcript write failed, dropping its events: %s", self.n, err)

    def _write(self, e: dict[str, Any]) -> None:
        self._buf.append(json.dumps(e) + "\n")
        etype = e.get("type")
        if etype == "vitals":
            self._reading = str(e.get("reading", ""))
            self._reading_t = float(e.get("t", 0.0))
            self._in_thought = True
        elif etype == "thought_end":
            self.flush()
            self._in_thought = False
            text = thought_block(self._reading_t, self._reading, str(e.get("text", "")))
            _append(self.thoughts_path, text + self._take_death_line())
            return
        elif etype == "death":
            extra = {k: v for k, v in e.items() if k in ("cause", "lived_s", "model")}
            line = f"t+{_fmt_t(float(e.get('t', 0.0)))}  -- death {json.dumps(extra)}\n\n"
            self.flush()
            if self._in_thought:
                self._death_line = line  # after the words the death flush shows
            else:
                _append(self.thoughts_path, line)
            return
        elif etype == "death_shown":
            self._end_dead_thought()
        if etype in FLUSH_ON:
            self.flush()

    def _take_death_line(self) -> str:
        line, self._death_line = self._death_line, None
        return line or ""

    def _end_dead_thought(self) -> None:
        """A death held for a thought that never ended: its reading, then the death."""
        if self._death_line is None:
            return
        block = thought_block(self._reading_t, self._reading, "") if self._in_thought else ""
        self._in_thought = False
        _append(self.thoughts_path, block + self._take_death_line())

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
        if self.failed is None:
            try:
                self.flush()
                self._end_dead_thought()
            except OSError as err:
                self._fail(err)
        atomic_write_json(self.dir / "death.json", {**record, "closed_ts": round(time.time(), 3)})
        self.closed = True


def _meta_model(d: Path) -> str:
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(meta, dict):
        return ""
    fields: dict[str, object] = meta  # pyright: ignore[reportUnknownVariableType]
    return str(fields.get("model", ""))


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
    if not model:  # killed during the load: only meta.json was written
        model = _meta_model(d)
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
        path = d / "events.jsonl"
        torn = path.exists() and not path.read_bytes().endswith(b"\n")
        _append(path, ("\n" if torn else "") + json.dumps(e) + "\n")
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
