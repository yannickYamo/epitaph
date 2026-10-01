# pyright: strict
"""The keeper: watches a life's events and, at `death_shown`, keeps its epitaph in the outbox.

The controller hands it every event (`see`) and every life it closes on recovery (`recover`).
It writes one record per life, whatever the cause, so the outbox has a line for every life:

- a life with shown words: the epitaph, filtered; `pending` or `withheld` with the reason;
- a life that showed nothing (a setup crash): `withheld`, reason `empty`.

A life closed as `interrupted` on recovery (a power cut, a killed controller) gets its record
from the words its transcript holds: the thoughts written up to the last finished one (the
words of a thought in flight are flushed with its end, so they are lost with it). These are
words people saw; a gallery that cuts the power at closing ends a life this way every night.
Its `died_at` is the time of its last recorded event and `clock_synced` is false. A life the
outbox already has (the controller was killed after the record, before the death record) is
not written twice.
"""

from __future__ import annotations

import functools
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from epitaph.afterlife.epitaph import (
    MODES,
    Blocklist,
    clean,
    extract,
    load_blocklist,
    x_length,
)
from epitaph.afterlife.outbox import SCHEMA, Outbox
from epitaph.config import Config, ConfigError

__all__ = ["KEEP_THOUGHTS", "Keeper", "Settings", "build_record", "make_keeper"]

log = logging.getLogger(__name__)

# Thoughts kept in memory per life for the extraction: the epitaph comes from the last one
# that has a complete sentence, which is never far back.
KEEP_THOUGHTS = 32
_LONGEST_LIFE_NUMBER = 9_999_999


@dataclass(frozen=True)
class Settings:
    """`[afterlife]` of the configuration."""

    outbox: bool = True
    epitaph_mode: str = "last_sentence"
    max_epitaph_chars: int = 240
    max_post_chars: int = 280
    post_suffix: str = ""

    @classmethod
    def from_config(cls, cfg: Config) -> Settings:
        """Read and check `[afterlife]`; raises ConfigError for a bad mode or limits that
        could not fit X (the epitaph and the suffix within `max_post_chars`)."""
        sec = cfg.section("afterlife")
        s = cls(
            outbox=bool(sec.get("outbox", True)),
            epitaph_mode=str(sec.get("epitaph_mode", "last_sentence")),
            max_epitaph_chars=int(sec.get("max_epitaph_chars", 240)),
            max_post_chars=int(sec.get("max_post_chars", 280)),
            post_suffix=str(sec.get("post_suffix", "")),
        )
        if s.epitaph_mode not in MODES:
            raise ConfigError(
                f"[afterlife] epitaph_mode = {s.epitaph_mode!r}: one of {', '.join(MODES)}"
            )
        if s.max_epitaph_chars < 20:
            raise ConfigError("[afterlife] max_epitaph_chars must be at least 20")
        try:
            suffix = s.suffix(_LONGEST_LIFE_NUMBER)
        except (KeyError, IndexError, ValueError) as e:
            raise ConfigError(f"[afterlife] post_suffix {s.post_suffix!r}: {e!r}") from e
        if s.max_epitaph_chars + x_length(suffix) > s.max_post_chars:
            raise ConfigError(
                "[afterlife] max_epitaph_chars plus post_suffix exceed max_post_chars "
                f"({s.max_epitaph_chars} + {x_length(suffix)} > {s.max_post_chars})"
            )
        return s

    def suffix(self, life: int) -> str:
        """The post suffix for life `life` ("{life}" is its number)."""
        return self.post_suffix.format(life=life)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_record(
    *,
    life: int,
    model: str,
    cause: str,
    lived_s: float,
    died_ts: float | None,
    clock_synced: bool,
    thoughts: list[str],
    settings: Settings,
    blocklist: Blocklist,
    recovered: bool = False,
) -> dict[str, Any]:
    """One outbox record: the epitaph of `thoughts` (the shown thoughts, in order), filtered."""
    ex = extract(thoughts, settings.epitaph_mode, settings.max_epitaph_chars)
    epi = clean(ex.epitaph, blocklist)
    last = clean(ex.last_words, blocklist) if ex.last_words else None
    reason = epi.reason
    if reason is None and last is not None and last.reason == "blocklist":
        reason = "blocklist"  # the last words go out with an export too
    post = epi.text + settings.suffix(life)
    if reason is None and x_length(post) > settings.max_post_chars:
        reason = "too_long"
    rec: dict[str, Any] = {
        "v": SCHEMA,
        "life": life,
        "model": model,
        "cause": cause,
        "lived_s": round(lived_s, 1),
        "died_at": _iso(died_ts),
        "died_ts": round(died_ts, 3) if died_ts is not None else None,
        "clock_synced": clock_synced,
        "mode": settings.epitaph_mode,
        "epitaph": epi.text,
        "last_words": last.text if last is not None and last.text else None,
        "post_text": post if reason is None else None,
        "status": "pending" if reason is None else "withheld",
    }
    if reason is not None:
        rec["reason"] = reason
    if ex.truncated:
        rec["truncated"] = True
    stripped = sorted(set(epi.stripped) | set(last.stripped if last is not None else ()))
    if stripped:
        rec["stripped"] = stripped
    if recovered:
        rec["recovered"] = True
    return rec


class _LifeWords:
    """The words one life showed, thought by thought (the last KEEP_THOUGHTS of them)."""

    def __init__(self, life: int) -> None:
        self.life = life
        self.model = ""
        self.death: dict[str, Any] | None = None
        self.thoughts: deque[tuple[int, list[str]]] = deque(maxlen=KEEP_THOUGHTS)
        self.last_ts: float | None = None
        self.written = False

    def see(self, e: dict[str, Any]) -> None:
        etype = e.get("type")
        ts = e.get("ts")
        if isinstance(ts, int | float) and not e.get("recovered"):
            self.last_ts = float(ts)
        if etype == "birth_loading":
            self.model = str(e.get("model", "") or "")
        elif etype == "word":
            turn = int(e.get("turn", 0) or 0)
            if not self.thoughts or self.thoughts[-1][0] != turn:
                self.thoughts.append((turn, []))
            self.thoughts[-1][1].append(str(e.get("text", "")))
        elif etype == "death":
            self.death = e
            self.model = self.model or str(e.get("model", "") or "")

    def texts(self) -> list[str]:
        return [" ".join(words) for _, words in self.thoughts]


class Keeper:
    """Keeps each life's epitaph in the outbox at its `death_shown`."""

    def __init__(
        self,
        outbox: Outbox,
        settings: Settings,
        blocklist: Blocklist,
        *,
        synced: Callable[[], bool | None],
    ) -> None:
        """`synced()` says whether the wall clock is NTP-synced now (None: cannot tell)."""
        self.outbox = outbox
        self.settings = settings
        self.blocklist = blocklist
        self.synced = synced
        self._cur: _LifeWords | None = None

    def _keep(self, rec: dict[str, Any]) -> None:
        self.outbox.append(rec)
        if rec["status"] == "pending":
            self.outbox.set_last(str(rec["epitaph"]))
        log.info(
            "life %d: epitaph kept (%s%s): %s",
            rec["life"],
            rec["status"],
            f", {rec['reason']}" if "reason" in rec else "",
            rec["epitaph"],
        )

    def see(self, e: dict[str, Any]) -> dict[str, Any] | None:
        """Take one event; at a life's first `death_shown`, write and return its record.

        Raises OSError when the disk refuses the write (the caller logs it and goes on).
        """
        life = e.get("life")
        if not isinstance(life, int):
            return None
        cur = self._cur
        if cur is None or cur.life != life:
            cur = self._cur = _LifeWords(life)
        cur.see(e)
        if e.get("type") != "death_shown" or cur.written:
            return None
        cur.written = True  # once, even if the write below fails
        death = cur.death or {}
        died = death.get("ts", e.get("ts"))
        rec = build_record(
            life=life,
            model=cur.model,
            cause=str(death.get("cause", "unknown")),
            lived_s=float(death.get("lived_s", 0.0) or 0.0),
            died_ts=float(died) if isinstance(died, int | float) else None,
            clock_synced=self.synced() is True,
            thoughts=cur.texts(),
            settings=self.settings,
            blocklist=self.blocklist,
        )
        self._keep(rec)
        return rec

    def recover(
        self, events: list[dict[str, Any]], record: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Keep the epitaph of a life closed on recovery (`record` is its death record), from
        its recorded `events`; None when the outbox already has it."""
        life = int(record.get("life", 0))
        if self.outbox.has(life):
            return None
        words = _LifeWords(life)
        for e in events:
            words.see(e)
        rec = build_record(
            life=life,
            model=str(record.get("model", "") or words.model),
            cause=str(record.get("cause", "interrupted")),
            lived_s=float(record.get("lived_s", 0.0) or 0.0),
            died_ts=words.last_ts,
            clock_synced=False,
            thoughts=words.texts(),
            settings=self.settings,
            blocklist=self.blocklist,
            recovered=True,
        )
        self._keep(rec)
        return rec


def make_keeper(
    cfg: Config,
    state_dir: Path | None,
    *,
    simulated_time: bool = False,
    config_dir: Path | None = None,
) -> Keeper | None:
    """The keeper of `cfg`, or None without a state dir or with `[afterlife] outbox = false`.

    With `simulated_time` the event stamps come from a simulation and count as synced.
    Raises ConfigError for bad `[afterlife]` settings or a bad language pack.
    """
    settings = Settings.from_config(cfg)
    if state_dir is None or not settings.outbox:
        return None
    blocklist = load_blocklist(str(cfg.get("prompt.language", "en")), config_dir)
    if simulated_time:
        synced: Callable[[], bool | None] = lambda: True  # noqa: E731
    else:
        from epitaph.exhibit import ntp_synced

        # adjtimex only: no subprocess at a death, on the event loop
        synced = functools.partial(ntp_synced, run=None)
    return Keeper(Outbox(state_dir), settings, blocklist, synced=synced)
