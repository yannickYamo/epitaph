"""Persistent state: atomic writes, the life counter, the single-instance lock (BUILD_PLAN 6.1).

Every state file is written to a temporary file, fsynced, then renamed over the target, so a
power cut leaves either the old or the new content, never a torn file.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import IO, Any


def atomic_write(path: Path, data: str | bytes) -> None:
    """Write data to path so that a crash leaves either the old or the new file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    raw = data.encode() if isinstance(data, str) else data
    with open(tmp, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def atomic_write_json(path: Path, obj: Any) -> None:
    """Atomically write obj as indented, key-sorted JSON with a trailing newline."""
    atomic_write(path, json.dumps(obj, indent=1, sort_keys=True) + "\n")


class LifeCounter:
    """The number of the current life; incremented and persisted before anything else at birth."""

    def __init__(self, state_dir: Path) -> None:
        """Keep the counter in `state_dir/life_counter`."""
        self.path = state_dir / "life_counter"

    def current(self) -> int:
        """The last life number handed out; 0 if there is none yet or the file is unreadable."""
        try:
            return int(self.path.read_text().strip() or 0)
        except (FileNotFoundError, ValueError):
            return 0

    def next(self) -> int:
        """Increment the counter, persist it atomically, and return the new life number."""
        n = self.current() + 1
        atomic_write(self.path, f"{n}\n")
        return n


class AlreadyRunning(RuntimeError):
    """Another controller holds the single-instance lock."""


class InstanceLock:
    """An exclusive flock on controller.lock; released automatically if the process dies."""

    def __init__(self, state_dir: Path) -> None:
        """Lock `state_dir/controller.lock`; nothing is taken until `acquire`."""
        self.path = state_dir / "controller.lock"
        self._f: IO[str] | None = None

    def acquire(self) -> None:
        """Take the lock without blocking and write our pid into the lock file.

        Raises AlreadyRunning, naming the holder's pid, if another controller has it.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+")  # noqa: SIM115 - held for the process lifetime
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            f.seek(0)
            holder = f.read().strip() or "unknown"
            f.close()
            raise AlreadyRunning(
                f"a controller is already running (pid {holder}); "
                "use `epitaph ctl new-life` to start a new life instead"
            ) from e
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        self._f = f

    def release(self) -> None:
        """Drop the lock and close the file; safe to call when it is not held."""
        if self._f is not None:
            fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
            self._f.close()
            self._f = None

    def __enter__(self) -> InstanceLock:
        """Acquire the lock."""
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        """Release the lock."""
        self.release()


def life_dir(state_dir: Path, n: int) -> Path:
    """The folder of life n, `lives/NNNNNN`, zero-padded so names sort in life order."""
    return state_dir / "lives" / f"{n:06d}"


def write_status(state_dir: Path, status: dict[str, Any]) -> None:
    """Atomically replace `status.json`, the controller's current status snapshot."""
    atomic_write_json(state_dir / "status.json", status)


def unfinished_lives(state_dir: Path) -> list[Path]:
    """Life folders with events but no death record: closed as `interrupted` on recovery."""
    root = state_dir / "lives"
    if not root.exists():
        return []
    out: list[Path] = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / "events.jsonl").exists() and not (d / "death.json").exists():
            out.append(d)
    return out
