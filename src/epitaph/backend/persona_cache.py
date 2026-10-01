"""The persona cache: the system prompt read once, restored at every birth (dread plan W4).

The system prompt (persona and mechanics) is the same in every life, and reading it is the
longest wait of a birth on the Pi 4 (240 tokens at 2.4 tokens/s: about 100 s). It is read
once, the server's slot (its KV cache) is saved, and every later birth restores that file
instead (spike S4b: a slot save or restore is a fraction of a second).

A cached slot is valid only for the exact server it came from: the key hashes the model
file, the llama-server binary, the context size, the KV cache types, the SWA setting and
the messages read. Any change makes a new key, so a stale file is never restored; the
newest `keep` files are kept on disk.

Files live in `<state_dir>/cache/<key>.bin` (on disk, so a reboot keeps them). llama-server
only reads and writes slot files inside its `--slot-save-path`, so a restore copies the file
there first and removes the copy afterwards, and a save moves the server's file out.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from epitaph.types import Msg

FORMAT = 1  # bump when what goes into a slot file changes
PREFIX = "persona-"
KEEP = 4  # cached files kept (models in rotation, or a persona change being tried)


@dataclass
class PrefillInfo:
    """How the last `prefill` got the prompt into the cache (`backend.last_prefill`).

    `mode`: "prefill" (read token by token), "restore" (the persona cache), or "handover"
    (a reload's carried slot made the prefill unnecessary). `tokens` is the tokens read or
    restored; `file_bytes` the slot file's size; `saved` says a prefill was saved for the
    next births; `error` names a cache failure that fell back to the prefill.
    """

    mode: str = "prefill"
    tokens: int = 0
    file_bytes: int = 0
    seconds: float = 0.0
    saved: bool = False
    error: str | None = None


def _stat(path: Path) -> list[Any]:
    """Identity of a file for the key: its path, size and modification time (0s if absent)."""
    try:
        st = path.stat()
    except OSError:
        return [str(path), 0, 0]
    return [str(path), st.st_size, st.st_mtime_ns]


def persona_key(
    *,
    model_file: Path,
    server_bin: Path,
    model: str,
    quant: str,
    ctx: int,
    cache_type_k: str,
    cache_type_v: str,
    swa_full: bool,
    messages: Sequence[Msg],
) -> str:
    """The cache key of these messages on this server: changes with any of its inputs."""
    blob = json.dumps(
        {
            "format": FORMAT,
            "model": model,
            "quant": quant,
            "model_file": _stat(model_file),
            "server": _stat(server_bin),
            "ctx": ctx,
            "ctk": cache_type_k,
            "ctv": cache_type_v,
            "swa_full": swa_full,
            "messages": [[m.role, m.content] for m in messages],
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


class PersonaStore:
    """The cached slot files in `cache_dir`, handed to and from the server's slot directory."""

    def __init__(self, cache_dir: str | Path, slot_dir: str | Path, keep: int = KEEP) -> None:
        """Files in `cache_dir`; `slot_dir` is the server's `--slot-save-path`."""
        self.cache_dir = Path(cache_dir).expanduser()
        self.slot_dir = Path(slot_dir).expanduser()
        self.keep = max(1, keep)

    def path(self, key: str) -> Path:
        """The cached file of `key`."""
        return self.cache_dir / f"{key}.bin"

    @staticmethod
    def slot_name(key: str) -> str:
        """The file name the server reads or writes in its slot directory."""
        return f"{PREFIX}{key}.bin"

    def has(self, key: str) -> bool:
        """Whether a cached file exists for `key` (an empty file does not count)."""
        try:
            return self.path(key).stat().st_size > 0
        except OSError:
            return False

    def stage(self, key: str) -> int:
        """Copy the cached file into the slot directory for a restore; returns its size."""
        self.slot_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        dst = self.slot_dir / self.slot_name(key)
        shutil.copyfile(self.path(key), dst)
        return dst.stat().st_size

    def unstage(self, key: str) -> None:
        """Remove the slot directory's copy (it sits in RAM on the Pi)."""
        with contextlib.suppress(OSError):
            (self.slot_dir / self.slot_name(key)).unlink()

    def keep_saved(self, key: str) -> int:
        """Move the server's saved file into the cache (atomically); returns its size."""
        src = self.slot_dir / self.slot_name(key)
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.cache_dir / f".{key}.tmp"
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, self.path(key))
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
            with contextlib.suppress(OSError):
                src.unlink()
        self.prune(key)
        return self.path(key).stat().st_size

    def forget(self, key: str) -> None:
        """Drop a cached file that failed to restore, so the next birth saves a good one."""
        with contextlib.suppress(OSError):
            self.path(key).unlink()

    def prune(self, newest: str) -> None:
        """Keep the `keep` most recently written files, `newest` always among them."""
        files = [p for p in self.cache_dir.glob("*.bin") if p.stem != newest]
        files.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for old in files[self.keep - 1 :]:
            with contextlib.suppress(OSError):
                old.unlink()
