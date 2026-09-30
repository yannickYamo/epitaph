"""Test helpers: an event recorder, a builder for crafted lives, and editors for recorded ones.

Fixtures in conftest.py hand these out; tests may also import them directly
(`from tests.helpers import LifeBuilder`).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from epitaph.types import PROTOCOL_VERSION

Event = dict[str, Any]
BASE_TS = 1_790_000_000.0


class EventRecorder:
    """Collects events (a bus local listener, an `emit` callback) and answers questions."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def attach(self, bus: Any) -> EventRecorder:
        bus.add_local(self)
        return self

    def types(self, life: int | None = None) -> list[str]:
        return [e["type"] for e in self.events if life is None or e.get("life") == life]

    def of(self, etype: str, life: int | None = None) -> list[Event]:
        return [
            e for e in self.events if e["type"] == etype and (life is None or e.get("life") == life)
        ]

    def lives(self) -> list[int]:
        out: list[int] = []
        for e in self.events:
            if e.get("life") not in out:
                out.append(e.get("life"))  # type: ignore[arg-type]
        return out

    def write(self, path: Path) -> Path:
        return write_events(path, self.events)


def write_events(path: Path, events: Iterable[Event]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps({k: v for k, v in e.items() if not k.startswith("_")}) + "\n")
    return path


def read_events(path: Path) -> list[Event]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class LifeBuilder:
    """Builds a crafted life event by event, on a life clock you move by hand.

    Typing follows 5.12: each word gets char_ms per letter and a pause after it, so a thought
    typed at `wpm` words per minute takes len(words) * 60 / wpm seconds.
    """

    def __init__(self, n: int = 1, model: str = "fake-model") -> None:
        self.n = n
        self.model = model
        self.t = 0.0
        self.turn = 0
        self.events: list[Event] = []

    def ev(self, etype: str, **fields: Any) -> Event:
        e: Event = {
            "v": PROTOCOL_VERSION,
            "ts": round(BASE_TS + self.n * 100_000 + self.t, 3),
            "life": self.n,
            "type": etype,
            **fields,
            "t": round(self.t, 3),
        }
        self.events.append(e)
        return e

    def at(self, t: float) -> LifeBuilder:
        self.t = t
        return self

    def wait(self, s: float) -> LifeBuilder:
        self.t += s
        return self

    def birth(self, step: int = 0, threads: int = 3, load_s: float = 0.0) -> LifeBuilder:
        self.t = -load_s
        self.ev("birth_loading", model=self.model, step=step, quant=f"q{step}", facts={})
        self.t = 0.0
        self.ev("birth", model=self.model, step=step, quant=f"q{step}", threads=threads)
        return self

    def vitals(
        self,
        health: str = "nominal",
        recall: int = 1280,
        recall_used: int = 0,
        cpu_share: float = 3.0,
        tok_s: float = 1.35,
        reading: str = "",
        **extra: Any,
    ) -> LifeBuilder:
        self.ev(
            "vitals",
            phase=extra.pop("phase", "birth"),
            health=health,
            recall=recall,
            recall_used=recall_used,
            forgotten_since_last=extra.pop("forgotten_since_last", 0),
            step=extra.pop("step", 0),
            threads=extra.pop("threads", 3),
            cpu_share=cpu_share,
            cores_effective=cpu_share,
            tok_s=tok_s,
            cpu_c=extra.pop("cpu_c", 55.0),
            reading=reading,
            **extra,
        )
        return self

    def thought(
        self,
        text: str,
        wpm: float = 60.0,
        first_word_after_s: float = 2.0,
        pause_s: float = 3.0,
        tok_s: float | None = None,
        vitals: dict[str, Any] | None = None,
    ) -> LifeBuilder:
        if vitals is not None:
            self.vitals(**vitals)
        self.turn += 1
        turn = self.turn
        self.ev("gen_start", turn=turn)
        self.ev("thought_start", turn=turn)
        self.t += first_word_after_s
        words = text.split()
        per_word = 60.0 / wpm
        for i, w in enumerate(words):
            pause = 90
            letters = max(1, len(w))
            char = max(1, int((per_word * 1000 - pause) / letters))
            self.ev(
                "word",
                turn=turn,
                i=i,
                text=w,
                char_ms=[char] * letters,
                pause_after_ms=pause,
            )
            self.t += per_word
        end_fields: dict[str, Any] = {"turn": turn, "tokens": len(words)}
        if tok_s is not None:
            end_fields["tok_s"] = tok_s
        self.ev("gen_end", **end_fields)
        self.ev("thought_end", turn=turn, text=" ".join(words))
        self.t += pause_s
        return self

    def forget(self, turns: Iterable[int] = (1,)) -> LifeBuilder:
        self.ev("forget", items=[{"turn": t, "all": True} for t in turns])
        return self

    def reload(self, frm: str = "q0", to: str = "q1", seconds: float = 60.0) -> LifeBuilder:
        self.ev(
            "reload", threads=2, recall_before=1000, recall_after=500, **{"from": frm, "to": to}
        )
        self.t += seconds
        self.ev("reload_done", seconds=seconds)
        return self

    def erosion(self, groups_left: int, mechanics: bool = True) -> LifeBuilder:
        self.ev("erosion", groups_left=groups_left, mechanics_present=mechanics)
        return self

    def death(self, cause: str = "deadline", shown_after_s: float = 1.0) -> LifeBuilder:
        self.ev("death", cause=cause, lived_s=round(self.t, 1), model=self.model)
        self.t += shown_after_s
        self.ev("death_shown", last_line="", words_total=0)
        self.ev("silence", seconds=90.0, style="dark")
        return self

    def write(self, path: Path) -> Path:
        return write_events(path, self.events)


# ---------------------------------------------------------------------------------------
# editing recorded lives


def life_events(events: list[Event], n: int = 1) -> list[Event]:
    return [copy.deepcopy(e) for e in events if e.get("life") == n]


def retext(events: list[Event], fn: Callable[[int, str], str]) -> list[Event]:
    """Rewrite what each thought says, keeping its timing: fn(turn, old_text) -> new_text.

    The new words take the old words' release times (extra words reuse the last one's)."""
    out: list[Event] = []
    by_turn: dict[int, list[Event]] = {}
    for e in events:
        if e["type"] == "word":
            by_turn.setdefault(int(e["turn"]), []).append(e)
    emitted: set[int] = set()
    for e in events:
        if e["type"] == "word":
            turn = int(e["turn"])
            if turn in emitted:
                continue
            emitted.add(turn)
            old = by_turn[turn]
            new_words = fn(turn, " ".join(str(w["text"]) for w in old)).split()
            for i, w in enumerate(new_words):
                src = old[min(i, len(old) - 1)]
                out.append({**src, "i": i, "text": w, "char_ms": [55] * len(w)})
        elif e["type"] == "thought_end":
            turn = int(e["turn"])
            words = by_turn.get(turn, [])
            new = fn(turn, " ".join(str(w["text"]) for w in words)) if words else ""
            out.append({**e, "text": new})
        else:
            out.append(e)
    return out


def replace_first(events: list[Event], etype: str, **fields: Any) -> list[Event]:
    """Change fields of the first event of a type."""
    out = [dict(e) for e in events]
    for e in out:
        if e["type"] == etype:
            e.update(fields)
            break
    return out
