# pyright: strict
"""The outbox: every life's epitaph, kept on this machine's disk for later posts (V1.5).

No network is needed or used. The piece may run for weeks where there is none; whatever posts
the epitaphs later (a poster service, or a person with `epitaph outbox export`) reads them here.

Layout, under the state dir:

- `outbox/epitaphs.jsonl`: one JSON line per life, appended at its `death_shown` and fsynced.
  Append-only: a later change (a poster marking a life `posted`) is one more line,
  `{"life": n, "update": true, ...}`, folded onto the life's record when read. One file, not
  one per life: a life is written once every half hour or so, and a single file is one inode,
  one `tail`, one copy to a USB stick. A power cut can tear only the line being written: a
  torn or unreadable line is skipped when read, and the next append starts on a fresh line.
- `last_epitaph.txt`: the latest postable epitaph (atomic), for the next life to inherit later.

Size: every life is kept. A record is under 1 KB (the epitaph and last words are at most 240
characters each), so a year of 30-minute lives (about 17,500) is about 10 MB.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from epitaph.state import atomic_write

__all__ = ["SCHEMA", "STATUSES", "Outbox", "format_table"]

log = logging.getLogger(__name__)

SCHEMA = 1
STATUSES = ("pending", "withheld", "posted")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


class Outbox:
    """`<state_dir>/outbox/epitaphs.jsonl` and `<state_dir>/last_epitaph.txt`."""

    def __init__(self, state_dir: Path) -> None:
        """The outbox of the installation whose state lives in `state_dir`."""
        self.dir = state_dir / "outbox"
        self.path = self.dir / "epitaphs.jsonl"
        self.last_path = state_dir / "last_epitaph.txt"

    def _append(self, obj: dict[str, Any]) -> None:
        line = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
        self.dir.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            size = os.fstat(fd).st_size
            torn = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
            _write_all(fd, (b"\n" if torn else b"") + line)
            os.fsync(fd)
        finally:
            os.close(fd)
        if new:  # the new name itself must survive a power cut
            dir_fd = os.open(self.dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def append(self, record: dict[str, Any]) -> None:
        """Add one life's record (it must carry `life`). Raises OSError if the disk refuses."""
        if not isinstance(record.get("life"), int):
            raise ValueError("an outbox record needs an integer `life`")
        self._append(record)

    def update(self, life: int, **fields: Any) -> None:
        """Change a life's record later (e.g. `status="posted"`), as one more line."""
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"unknown status {fields['status']!r}")
        self._append({"life": life, "update": True, **fields})

    def lines(self) -> tuple[list[dict[str, Any]], int]:
        """Every readable line, and how many were torn or unreadable."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return [], 0
        out: list[dict[str, Any]] = []
        bad = 0
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                bad += 1
                continue
            if not isinstance(obj, dict):
                bad += 1
                continue
            rec: dict[str, Any] = obj  # pyright: ignore[reportUnknownVariableType]
            life = rec.get("life")
            if isinstance(life, int) and not isinstance(life, bool):
                out.append(rec)
            else:
                bad += 1
        if bad:
            log.warning("outbox: skipped %d torn or unreadable line(s) in %s", bad, self.path)
        return out, bad

    def records(self) -> list[dict[str, Any]]:
        """One record per life, in life order, with its later updates folded in.

        The first record of a life stands (a second one is ignored); an update with no record
        before it is ignored.
        """
        by_life: dict[int, dict[str, Any]] = {}
        for obj in self.lines()[0]:
            n = int(obj["life"])
            if obj.get("update") is True:
                if n in by_life:
                    by_life[n].update({k: v for k, v in obj.items() if k != "update"})
            elif n not in by_life:
                by_life[n] = dict(obj)
        return [by_life[n] for n in sorted(by_life)]

    def has(self, life: int) -> bool:
        """Whether a record of `life` is already kept."""
        return any(int(o["life"]) == life and o.get("update") is not True for o in self.lines()[0])

    def set_last(self, epitaph: str) -> None:
        """Atomically replace `last_epitaph.txt`."""
        atomic_write(self.last_path, epitaph + "\n")

    def last(self) -> str | None:
        """The latest postable epitaph, or None."""
        try:
            return self.last_path.read_text(encoding="utf-8").strip() or None
        except (OSError, UnicodeDecodeError):
            return None


def format_table(records: list[dict[str, Any]]) -> str:
    """Records as aligned text lines: life, status, cause, death time, epitaph.

    A death time from an unsynced clock (the Pi has no clock of its own; offline it starts
    from the last shutdown) is marked with "?".
    """
    lines: list[str] = []
    for r in records:
        when = str(r.get("died_at") or "-")
        if not r.get("clock_synced"):
            when += "?"
        status = str(r.get("status", "?"))
        if r.get("reason"):
            status += f" ({r['reason']})"
        text = str(r.get("epitaph") or "")
        if r.get("last_words") and r.get("last_words") != text:
            text += f"  / last words: {r['last_words']}"
        lines.append(
            f"{int(r['life']):>6}  {status:<20} {r.get('cause', '')!s:<11} {when:<21} {text}"
        )
    return "\n".join(lines)
