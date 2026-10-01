"""The display model: events in, frames out (BUILD_PLAN 5.12, 6.3, 9 D1).

Pure Python, no I/O and no real time: every call takes the display time `now` (seconds,
any monotonic origin). Drivers (terminal, pygame, e-ink, serial) feed events to a
`LifeView`, then ask `compose_flow` or `compose_grid` for a `Frame` to draw.

- `LifeView` holds the life as the viewer sees it: thoughts, words and their states
  (live, fading, forgotten, inherited), the typing timeline built from each word's
  `char_ms` and `pause_after_ms`, the cursor, reloads, death, cards, vitals and the
  memory gauge. It can also produce a `snapshot` event (replay `--from`, the controller).
- `flow_metrics` sizes the font from `line_chars` and `min_font_px` for any resolution.
- `flow_lines` wraps whole words: a word is placed with its full length before its
  first letter is typed, so it never splits across lines.
- `compose_flow` gives the full-screen view: newest text at the bottom, a blank line
  between thoughts, forgotten words fading through grey and then gone.
- Cards (`cards.py`) are typed letter by letter; at death the words fade out before the
  death card; the silence is dark, idle, the card or the last words.
- `compose_grid` gives an N x M character grid (LED matrix, small panel) with a charset
  map and a memory gauge instead of fading.
- `verify_probe` replays a recorded life through the same model for `epitaph verify-life`
  (split words, bright words near the end; BUILD_PLAN 10.3).
"""

from __future__ import annotations

import hashlib
import heapq
import math
import re
import sys
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol, TypeGuard

from epitaph.display.cards import (
    CAUSE_TEXT as CAUSE_TEXT,  # re-exported for older imports
)
from epitaph.display.cards import (
    IDLE_MARK,
    Card,
    CardKind,
    CardStyle,
    birth_lines,
    card_char_ms,
    death_lines,
    idle_position,
    shown_per_piece,
    type_card,
    wrap_card,
)
from epitaph.types import PROTOCOL_VERSION, WordState

DEFAULT_CHAR_MS = 60
BLINK_S = 0.53
CURSOR_KINDS = ("hidden", "on", "off")

Charset = Literal["unicode", "ascii", "segment16"]
Voice = Literal["model", "machine"]
ViewState = WordState | Literal["machine"]

HOST_TAG = re.compile(r"^\s*\[host\]\s*")
READING_SEP = " · "
# a forgotten sentence quoted in a reading: forgotten: "I am here, inside the…"
QUOTE = re.compile(r'"([^"]+?)…"|“([^”]+?)…”')


# ---------------------------------------------------------------------------------------
# words and thoughts


@dataclass
class ViewWord:
    """One released word and when it is typed on the display clock."""

    turn: int
    i: int
    text: str
    char_ms: tuple[int, ...]
    pause_after_ms: int = 0
    start: float = 0.0
    state: ViewState = "live"
    forgotten_at: float | None = None
    # per letter, when it starts to dissolve (a reading quoting this sentence is typed)
    dissolve: tuple[float, ...] | None = None
    _typing: tuple[tuple[int, ...], float] | None = field(default=None, repr=False, compare=False)

    @property
    def typing_s(self) -> float:
        """Seconds from the first letter to the last, excluding the pause after."""
        cached = self._typing
        if cached is None or cached[0] is not self.char_ms:  # char_ms is replaced, not mutated
            cached = self._typing = (self.char_ms, sum(self.char_ms) / 1000)
        return cached[1]

    @property
    def end(self) -> float:
        """When the last letter has been typed (the pause comes after)."""
        return self.start + self.typing_s

    @property
    def tail(self) -> float:
        """When the next word may start."""
        return self.end + self.pause_after_ms / 1000

    def shown(self, now: float) -> int:
        """Letters visible at `now`: letter k appears at start + sum(char_ms[:k])."""
        if now < self.start:
            return 0
        acc = self.start
        for k, ms in enumerate(self.char_ms):
            if now < acc:
                return k
            acc += ms / 1000
        return len(self.text)

    def state_at(self, now: float, fade_s: float) -> ViewState:
        """The word's state at `now`: fading for `fade_s` seconds after it is forgotten."""
        if self.forgotten_at is None:
            return self.state
        return "fading" if now - self.forgotten_at < fade_s else "forgotten"

    def fade_at(self, now: float, fade_s: float) -> float:
        """Fade progress at `now`, from 0 (fully bright) to 1 (fully forgotten)."""
        if self.forgotten_at is None:
            return 0.0
        if fade_s <= 0:
            return 1.0
        return min(1.0, max(0.0, (now - self.forgotten_at) / fade_s))


@dataclass
class Thought:
    """The words of one turn, in the order they were released.

    `laid` caches the thought's last wrapping for the layout (see `_lay`).
    """

    turn: int
    words: list[ViewWord] = field(default_factory=lambda: [])
    ended: bool = False
    laid: tuple[Any, Any] | None = field(default=None, repr=False, compare=False)
    voice: Voice = "model"  # "machine": one line of a reading, typed before thought `turn`


def char_ms_for(text: str, raw: Any, default: int = DEFAULT_CHAR_MS) -> tuple[int, ...]:
    """One interval per letter; pad or cut a mismatched list so typing always finishes."""
    vals = [max(0, int(x)) for x in raw] if isinstance(raw, list | tuple) else []
    if not vals:
        vals = [default]
    if len(vals) < len(text):
        vals += [vals[-1]] * (len(text) - len(vals))
    return tuple(vals[: len(text)])


# ---------------------------------------------------------------------------------------
# the life as the viewer sees it


@dataclass
class ViewSettings:
    """Timing and card options for a `LifeView`; durations in seconds.

    `max_backlog_s` bounds how far typing may lag the events before `catch_up`, and
    `max_words` bounds the history kept for display. `reveal_life_number` and `card_model`
    choose what the cards name; `card_char_ms`, `card_word_gap_ms` and
    `card_line_pause_ms` are the rhythm cards are typed with. `reload_dim_text` dims the
    whole text during a reload (the cursor always dims). `death_fade` fades every word
    after the last letter at death, before the death card. `idle_step_s` is how long the
    idle silence mark rests in one place.
    """

    fade_s: float = 8.0
    blink_s: float = BLINK_S
    birth_card: bool = True
    birth_card_s: float = 4.0
    death_card_s: float = 8.0
    silence_style: str = "dark"
    max_backlog_s: float = 30.0
    max_words: int = 1500
    reveal_life_number: bool = False
    card_model: bool = True
    card_char_ms: int = 165
    card_word_gap_ms: int = 270
    card_line_pause_ms: int = 750
    reload_dim_text: bool = False
    death_fade: bool = True
    idle_step_s: float = 4.0
    # the dread plan: the machine's voice, forgetting, darkness, the pulse, death, the vigil
    machine_voice: bool = True
    machine_char_ms: int = 30
    machine_line_pause_ms: int = 300
    forget_grace_s: float = 5.0
    dissolve_letter_s: float = 0.8
    screen_fade_s: float = 20.0
    contrast_floors: tuple[tuple[float, float], ...] = ((1620.0, 7.0), (1740.0, 4.5))
    pulse_from_s: float = 1320.0
    pulse_fast_s: float = 0.5
    pulse_skip_s: float = 60.0
    pulse_end_before_s: float = 30.0
    death_style: str = "auto"
    death_still_s: float = 2.0
    vigil_fade_s: float = 75.0
    vigil_floor: float = 0.3
    vigil_card_delay_s: float = 1.5

    @property
    def card_style(self) -> CardStyle:
        """What the cards show and their typing rhythm."""
        return CardStyle(
            self.reveal_life_number,
            self.card_model,
            self.card_char_ms,
            self.card_word_gap_ms,
            self.card_line_pause_ms,
        )

    @classmethod
    def from_config(cls, display: dict[str, Any]) -> ViewSettings:
        """Read the `[display]` config section; missing keys keep their defaults."""
        return cls(
            fade_s=float(display.get("fade_seconds", 8)),
            blink_s=float(display.get("cursor_blink_ms", 530)) / 1000,
            birth_card=bool(display.get("birth_card", True)),
            birth_card_s=float(display.get("birth_card_seconds", 4)),
            death_card_s=float(display.get("death_card_seconds", 8)),
            silence_style=str(display.get("silence_style", "dark")),
            reveal_life_number=bool(display.get("reveal_life_number", False)),
            card_model=bool(display.get("birth_card_model", True)),
            card_char_ms=int(display.get("card_char_ms", 165)),
            card_word_gap_ms=int(display.get("card_word_gap_ms", 270)),
            card_line_pause_ms=int(display.get("card_line_pause_ms", 750)),
            reload_dim_text=bool(display.get("reload_dim_text", False)),
            death_fade=bool(display.get("death_fade", True)),
            idle_step_s=float(display.get("idle_step_seconds", 4)),
            machine_voice=bool(display.get("machine_voice", True)),
            machine_char_ms=int(display.get("machine_char_ms", 30)),
            machine_line_pause_ms=int(display.get("machine_line_pause_ms", 300)),
            forget_grace_s=float(display.get("forget_grace_seconds", 5)),
            dissolve_letter_s=float(display.get("dissolve_letter_seconds", 0.8)),
            screen_fade_s=float(display.get("screen_fade_seconds", 20)),
            contrast_floors=_floors(display.get("contrast_floors")),
            pulse_from_s=_clock_s(display.get("pulse_from", "22:00"), 1320.0),
            pulse_fast_s=float(display.get("pulse_fastest_ms", 500)) / 1000,
            pulse_skip_s=float(display.get("pulse_skip_seconds", 60)),
            pulse_end_before_s=float(display.get("pulse_end_before_seconds", 30)),
            death_style=str(display.get("death_style", "auto")),
            death_still_s=float(display.get("death_still_seconds", 2)),
            vigil_fade_s=float(display.get("vigil_fade_seconds", 75)),
            vigil_floor=float(display.get("vigil_floor", 0.3)),
            vigil_card_delay_s=float(display.get("vigil_card_delay_seconds", 1.5)),
        )


def _clock_s(raw: Any, default: float) -> float:
    """Seconds from "m:ss" or a number; `default` when unreadable."""
    if _is_number(raw):
        return float(raw)
    if isinstance(raw, str) and ":" in raw:
        m, _, s = raw.partition(":")
        try:
            return int(m) * 60 + float(s)
        except ValueError:
            return default
    return default


def _floors(raw: Any) -> tuple[tuple[float, float], ...]:
    """`contrast_floors` as ((until life seconds, ratio), ...), sorted by time.

    The config gives [["27:00", 7.0], ["29:00", 4.5]]: until 27:00 the model's text keeps
    7:1 on the darkened screen, then 4.5:1 until 29:00, and nothing is held after that.
    """
    if not isinstance(raw, list | tuple):
        return ViewSettings.contrast_floors
    out: list[tuple[float, float]] = []
    for item in raw:  # type: ignore[union-attr]
        if isinstance(item, list | tuple) and len(item) == 2 and _is_number(item[1]):  # type: ignore[arg-type]
            out.append((_clock_s(item[0], 0.0), float(item[1])))  # type: ignore[index]
    return tuple(sorted(out))


@dataclass(frozen=True)
class Vigil:
    """After a death in the `vigil` silence: the last words it showed stay, dim and
    centred, with the small death card under them, fading for `vigil_fade_s` to
    `vigil_floor` and holding there while the next model loads, until the next life's
    first reading or word (the genesis)."""

    text: str  # the last thought as shown, cut where the stream stopped
    card: str  # "life N · 29:30"
    start: float  # display time the vigil replaces the frozen screen


class LifeView:
    """Everything a display needs, fed by events (BUILD_PLAN 6.3).

    Not thread-safe: one driver feeds and reads it from a single thread or event loop.
    """

    def __init__(self, settings: ViewSettings | None = None) -> None:
        """Start empty, connected and showing no life; `settings` defaults to ViewSettings()."""
        self.s = settings or ViewSettings()
        self.reset(0)
        self.connected = True

    def reset(self, life: int) -> None:
        """Forget everything shown and start an empty view of life number `life`."""
        self.life = life
        self.model = ""
        self.quant = ""
        self.phase = ""
        self.vitals: dict[str, Any] = {}
        self.thoughts: list[Thought] = []
        self.mode = "empty"  # empty loading living reloading dying dead silence
        self.reload_info: dict[str, Any] = {}
        self.death: dict[str, Any] = {}
        self.groups_left: int | None = None
        # the opening hours outlive a life: a new life outside them stays dark (BUILD_PLAN 5.10)
        self.exhibit_open: bool = getattr(self, "exhibit_open", True)
        self.tail = 0.0  # display time when the typing queue is empty
        self.loading_at: float | None = None
        self.birth_at: float | None = None
        self.death_shown_at: float | None = None
        self._cards: dict[CardKind, Card] = {}
        self.t_life: float | None = None
        self.t_at = 0.0
        self.last_error: str = ""
        self.reveal = ""  # "stream": the screen stops mid-letter at death (ADR-030)
        self.lifespan_s: float | None = None
        self.frozen_at: float | None = None
        self.readings = 0
        self.machine_debt = 0.0  # seconds the readings delayed the stream, not yet made up
        self.machine_from = self.machine_until = 0.0  # when the last reading is typed
        # the screen's brightness: from level, to level, since, over seconds
        self.screen: tuple[float, float, float, float] = (1.0, 1.0, 0.0, 0.0)
        # the vigil of the previous life outlives the reset, until this life's genesis
        self.vigil: Vigil | None = getattr(self, "vigil", None)

    # -- events ---------------------------------------------------------------------------

    def handle(self, e: dict[str, Any], now: float) -> None:
        """Apply event `e` received at display time `now`; unknown event types are ignored.

        A `birth_loading` event, or any event from a different life, resets the view first.
        """
        etype = str(e.get("type", ""))
        life = int(e.get("life", self.life) or 0)
        if etype != "snapshot" and (etype == "birth_loading" or (life and life != self.life)):
            self.reset(life)
        if "t" in e and isinstance(e["t"], int | float):
            self.t_life, self.t_at = float(e["t"]), now
        fn = getattr(self, "_on_" + etype, None)
        if fn is not None:
            fn(e, now)

    def _on_snapshot(self, e: dict[str, Any], now: float) -> None:
        self.vigil = None  # a snapshot says everything, the vigil included
        self.reset(int(e.get("life", 0) or 0))
        if "t" in e and isinstance(e["t"], int | float):
            self.t_life, self.t_at = float(e["t"]), now
        self.model = str(e.get("model", "") or "")
        self.phase = str(e.get("phase", "") or "")
        self.exhibit_open = bool(e.get("exhibit_open", True))
        vit = e.get("vitals")
        if isinstance(vit, dict):
            self.vitals = dict(vit)  # type: ignore[arg-type]
        gauge = e.get("memory")
        if isinstance(gauge, dict):
            self.vitals.update({k: v for k, v in gauge.items() if k in ("recall", "recall_used")})  # type: ignore[union-attr]
        self.quant = str(e.get("quant") or self.vitals.get("quant", "") or "")
        self.mode = str(e.get("mode", "living" if e.get("words") else "empty"))
        if self.mode not in ("empty", "loading", "living", "reloading", "dying", "dead", "silence"):
            self.mode = "living"
        by_turn: dict[int, Thought] = {}
        for w in e.get("words") or []:
            turn = int(w.get("turn", 0))
            th = by_turn.get(turn)
            if th is None:
                th = by_turn[turn] = Thought(turn, ended=True)
                self.thoughts.append(th)
            text = str(w.get("text", ""))
            state = str(w.get("state", "live"))
            vw = ViewWord(turn, int(w.get("i", len(th.words))), text, (0,) * len(text))
            vw.start = now
            dissolve = w.get("dissolve")
            if isinstance(dissolve, list) and dissolve and all(_is_number(x) for x in dissolve):
                vw.dissolve = tuple(now + float(x) for x in dissolve)  # type: ignore[arg-type]
                vw.forgotten_at = vw.dissolve[0]
            elif _is_number(w.get("forget_in")):
                vw.forgotten_at = now + float(w["forget_in"])
            elif state in ("fading", "forgotten"):
                fade = w.get("fade", 0.0) if state == "fading" else 1.0
                fade = min(1.0, max(0.0, float(fade))) if _is_number(fade) else 0.0
                vw.forgotten_at = now - fade * self.s.fade_s
            elif state == "inherited":
                vw.state = "inherited"
            th.words.append(vw)
        for item in e.get("machine") or []:
            if not isinstance(item, dict):
                continue
            turn = int(item.get("turn", 0) or 0)  # type: ignore[union-attr]
            text = str(item.get("text", ""))  # type: ignore[union-attr]
            th = Thought(turn, ended=True, voice="machine")
            for k, part in enumerate(p for p in text.split(" ") if p):
                vw = ViewWord(turn, k, part, (0,) * len(part), 0, start=now, state="machine")
                fade = item.get("fade")  # type: ignore[union-attr]
                if _is_number(fade):
                    vw.forgotten_at = now - min(1.0, max(0.0, float(fade))) * self.s.fade_s
                th.words.append(vw)
            if not th.words:
                continue
            at = next(
                (k for k, o in enumerate(self.thoughts) if o.voice == "model" and o.turn == turn),
                len(self.thoughts),
            )
            self.thoughts.insert(at, th)
        model = [th for th in self.thoughts if th.voice == "model"]
        if model and e.get("open_turn") == model[-1].turn:
            model[-1].ended = False
        self.tail = now
        if _is_number(e.get("readings")):
            self.readings = int(e["readings"])
        elif e.get("machine"):
            self.readings = 1
        self.reveal = str(e.get("reveal", "") or "")
        if _is_number(e.get("lifespan_s")):
            self.lifespan_s = float(e["lifespan_s"])
        screen = e.get("screen")
        if isinstance(screen, dict):
            a, b = screen.get("from"), screen.get("to")  # type: ignore[union-attr]
            since, over = screen.get("since_s"), screen.get("over_s")  # type: ignore[union-attr]
            if _is_number(a) and _is_number(b):
                since_s = float(since) if _is_number(since) else 0.0
                over_s = max(0.0, float(over)) if _is_number(over) else 0.0
                self.screen = (float(a), float(b), now - since_s, over_s)
        if _is_number(e.get("frozen_ago")):
            self.frozen_at = now - float(e["frozen_ago"])
            self.tail = min(self.tail, self.frozen_at)
        vigil = e.get("vigil")
        if isinstance(vigil, dict):
            ago = vigil.get("ago")  # type: ignore[union-attr]
            self.vigil = Vigil(
                str(vigil.get("text", "")),  # type: ignore[union-attr]
                str(vigil.get("card", "")),  # type: ignore[union-attr]
                now - (float(ago) if _is_number(ago) else 0.0),
            )
        reload = e.get("reload")
        if self.mode == "reloading" and isinstance(reload, dict):
            self.reload_info = dict(reload)  # type: ignore[arg-type]
        if _is_number(e.get("groups_left")):
            self.groups_left = int(e["groups_left"])
        death = e.get("death")
        if isinstance(death, dict):
            self.death = dict(death)  # type: ignore[arg-type]
        ago = e.get("death_shown_ago")
        if _is_number(ago) and self.mode in ("dead", "silence"):
            # the last letter was typed `ago` seconds before: cards and fades resume there
            self.death_shown_at = self.tail = now - float(ago)
            for w in self.words():
                w.start = min(w.start, self.tail)

    def _on_birth_loading(self, e: dict[str, Any], now: float) -> None:
        self.model = str(e.get("model", ""))
        self.quant = str(e.get("quant", ""))
        self.mode = "loading"
        self.loading_at = now
        self.reveal = str(e.get("reveal", "") or "")
        if _is_number(e.get("lifespan_s")):
            self.lifespan_s = float(e["lifespan_s"])

    def _on_birth(self, e: dict[str, Any], now: float) -> None:
        self.model = str(e.get("model", self.model))
        self.quant = str(e.get("quant", self.quant))
        self.mode = "living"
        self.birth_at = now
        self.tail = max(self.tail, now)

    def _on_vitals(self, e: dict[str, Any], now: float) -> None:
        self.vitals = {k: v for k, v in e.items() if k not in ("v", "ts", "life", "type")}
        self.phase = str(e.get("phase", self.phase))
        self.quant = str(e.get("quant", self.quant))

    def _thought(self, turn: int) -> Thought:
        last = self.thoughts[-1] if self.thoughts else None
        if last is not None and last.turn == turn and last.voice == "model":
            return last
        for th in self.thoughts:
            if th.turn == turn and th.voice == "model":
                return th
        th = Thought(turn)
        self.thoughts.append(th)
        return th

    def _on_thought_start(self, e: dict[str, Any], now: float) -> None:
        self._thought(int(e.get("turn", 0)))

    def _on_word(self, e: dict[str, Any], now: float) -> None:
        text = str(e.get("text", ""))
        if not text:
            return
        th = self._thought(int(e.get("turn", 0)))
        vw = ViewWord(
            th.turn,
            int(e.get("i", len(th.words))),
            text,
            char_ms_for(text, e.get("char_ms")),
            max(0, int(e.get("pause_after_ms", 0) or 0)),
        )
        if e.get("state") == "inherited":
            vw.state = "inherited"
        if self.frozen_at is not None:
            return  # the stream stopped at the death; nothing more is typed
        self.vigil = None  # the genesis: this life's first words replace the vigil
        hesitate = max(0, int(e.get("hesitate_before_ms", 0) or 0)) / 1000
        vw.start = max(now, self.tail) + hesitate
        # a reading typed in the stream delays the words after it; the pauses after them
        # give that time back (at most half of each), so the screen does not drift behind
        self.machine_debt = min(self.machine_debt, max(0.0, vw.start - now))
        if self.machine_debt > 0 and vw.pause_after_ms > 0:
            cut = min(self.machine_debt, vw.pause_after_ms / 2000)
            vw.pause_after_ms -= round(cut * 1000)
            self.machine_debt -= cut
        th.words.append(vw)
        self.tail = vw.tail
        if self.mode in ("empty", "loading"):
            self.mode = "living"
        if self.tail - now > self.s.max_backlog_s:
            self.catch_up(now)
        self._trim_history()

    def _on_thought_end(self, e: dict[str, Any], now: float) -> None:
        self._thought(int(e.get("turn", 0))).ended = True

    def _on_forget(self, e: dict[str, Any], now: float) -> None:
        if e.get("deferred"):
            return  # a stream life: the screen's own copy comes when it reaches this moment
        # In the stream the reading that reports this loss comes next: the words wait for
        # it (`forget_grace_s`) so a quoted sentence can dissolve while it is quoted.
        at = now + (self.s.forget_grace_s if e.get("shown") and self.s.machine_voice else 0.0)
        for item in e.get("items") or []:
            turn = int(item.get("turn", -1))
            upto = item.get("upto_i")
            for th in self.thoughts:
                if th.turn != turn or th.voice != "model":
                    continue
                for w in th.words:
                    if w.forgotten_at is None and (
                        item.get("all") or (upto is not None and w.i <= int(upto))
                    ):
                        w.forgotten_at = at
        self._sync_machine()

    def _sync_machine(self) -> None:
        """A reading leaves the screen with the thought it came before: once every word
        of that thought is forgotten, the reading's words fade from the same moment."""
        gone: dict[int, float] = {}
        for th in self.thoughts:
            if th.voice != "model" or not th.words:
                continue
            ats = [w.forgotten_at for w in th.words]
            if all(a is not None for a in ats):
                gone[th.turn] = max(a for a in ats if a is not None)
        for th in self.thoughts:
            if th.voice == "machine" and th.turn in gone:
                for w in th.words:
                    if w.forgotten_at is None or w.forgotten_at > gone[th.turn]:
                        w.forgotten_at = gone[th.turn]

    def _on_reading(self, e: dict[str, Any], now: float) -> None:
        """The machine's voice: a reading typed fast and small, in the stream just before
        the thought it precedes (`turn`), without its `[host]` tag. The life's first
        reading (the inventory) is typed line by line. A quoted forgotten sentence still on
        screen dissolves letter by letter while the quote is typed."""
        text = HOST_TAG.sub("", str(e.get("text", "") or "")).strip()
        if self.frozen_at is not None or not text:
            return
        self.vigil = None  # the genesis
        turn = int(e.get("turn", 0) or 0)
        first = turn == 1 or (turn <= 0 and self.readings == 0)  # the birth reading
        self.readings += 1
        if not self.s.machine_voice:
            self._release_pending(now)
            return
        if self.mode in ("empty", "loading"):
            self.mode = "living"
        ms = int(e["char_ms"]) if _is_number(e.get("char_ms")) else self.s.machine_char_ms
        ms = max(0, ms)
        pause = self.s.machine_line_pause_ms if ms else 0
        lines = [part for part in text.split(READING_SEP) if part] if first else [text]
        start = max(now, self.tail)
        t = start
        made: list[Thought] = []
        times: list[float] = []  # when each letter of the last line appears (spaces too)
        for n, line in enumerate(lines):
            th = Thought(turn, ended=True, voice="machine")
            times = []
            parts = line.split(" ")
            for k, part in enumerate(parts):
                if not part:
                    times.append(t)
                    continue
                vw = ViewWord(turn, k, part, (ms,) * len(part), 0, start=t, state="machine")
                times += [t + j * ms / 1000 for j in range(len(part))]
                t = vw.end
                if k < len(parts) - 1:
                    vw.pause_after_ms = ms
                    times.append(t)
                    t += ms / 1000
                th.words.append(vw)
            if th.words:
                made.append(th)
            if n < len(lines) - 1:
                t += pause / 1000
        at = next(
            (k for k, th in enumerate(self.thoughts) if th.voice == "model" and th.turn == turn),
            len(self.thoughts),
        )
        self.thoughts[at:at] = made
        self.tail = t
        self.machine_from, self.machine_until = start, t
        self.machine_debt += t - start
        end = t
        if not first:
            m = QUOTE.search(lines[-1])
            if m is not None:
                group = 1 if m.group(1) is not None else 2
                quote = m.group(group) or ""
                q0 = m.start(group)
                qt = times[q0 : q0 + len(quote)]
                if qt and self._dissolve(quote, qt):
                    end = qt[-1]
        self._release_pending(end)
        self._sync_machine()
        self._trim_history()

    def _dissolve(self, quote: str, times: list[float]) -> bool:
        """Dissolve the on-screen sentence that `quote` opens: its letter k starts fading
        at times[k] (when the quote's letter k is typed). False when it is not on screen."""
        want = quote.split()
        if not want:
            return False
        for th in reversed(self.thoughts):
            if th.voice != "model" or len(th.words) < len(want):
                continue
            head = th.words[: len(want)]
            if any(w.text != q for w, q in zip(head[:-1], want[:-1], strict=True)):
                continue
            if not head[-1].text.startswith(want[-1]):
                continue
            if any(
                w.forgotten_at is not None and w.forgotten_at <= times[0] and w.dissolve is None
                for w in head
            ):
                return False  # already fading: leave it be
            pos = 0
            for w in head:
                w.dissolve = tuple(times[min(pos + k, len(times) - 1)] for k in range(len(w.text)))
                w.forgotten_at = w.dissolve[0]
                pos += len(w.text) + 1
            return True
        return False

    def _release_pending(self, at: float) -> None:
        """Words forgotten in the stream but held for their reading start fading at `at`."""
        for w in self.words():
            if w.dissolve is None and w.forgotten_at is not None and w.forgotten_at > at:
                w.forgotten_at = at

    def _on_world(self, e: dict[str, Any], now: float) -> None:
        """A loss performed on the machine; only the screen's own dimming is drawn.

        `screen:<N>` dims the whole screen to N% of full brightness over `screen_fade_s`
        (drivers keep the contrast floors, `contrast_floor`). A loss that was not
        performed changes nothing."""
        if e.get("performed") is False:
            return
        action = str(e.get("action", "") or "")
        if action.startswith("screen:"):
            try:
                pct = float(action.split(":", 1)[1])
            except ValueError:
                return
            level = self.screen_level(now)
            self.screen = (level, min(1.0, max(0.0, pct / 100)), now, self.s.screen_fade_s)

    def screen_level(self, now: float) -> float:
        """The brightness asked for at `now` (0..1), easing between the world's steps."""
        a, b, at, dur = self.screen
        if dur <= 0 or now >= at + dur:
            return b
        if now <= at:
            return a
        x = (now - at) / dur
        return a + (b - a) * x * x * (3 - 2 * x)

    def contrast_floor(self, now: float) -> float:
        """The least contrast the model's text keeps on the darkened screen at `now`:
        7:1 until 27:00, 4.5:1 until 29:00, nothing after (the defaults)."""
        t = self.life_t(now)
        floors = self.s.contrast_floors
        if t is None:
            return floors[0][1] if floors else 1.0
        for until, ratio in floors:
            if t < until:
                return ratio
        return 1.0

    def _on_erosion(self, e: dict[str, Any], now: float) -> None:
        self.groups_left = int(e.get("groups_left", 0))

    def _on_reload(self, e: dict[str, Any], now: float) -> None:
        self.reload_info = {k: v for k, v in e.items() if k not in ("v", "ts", "life", "type")}
        self.mode = "reloading"

    def _on_reload_done(self, e: dict[str, Any], now: float) -> None:
        if self.mode == "reloading":
            self.mode = "living"
        to = self.reload_info.get("to")
        if to:
            self.quant = str(to)

    _on_reload_skipped = _on_reload_done

    def _on_death(self, e: dict[str, Any], now: float) -> None:
        self.death = {k: v for k, v in e.items() if k not in ("v", "ts", "type")}
        self.mode = "dying"
        if self.freezes():
            self.freeze(now)

    def freezes(self) -> bool:
        """Whether the screen stops where it is at death: `death_style` "freeze", or
        "auto" in a stream life (ADR-030)."""
        style = self.s.death_style
        return style == "freeze" or (style == "auto" and self.reveal == "stream")

    def freeze(self, now: float) -> None:
        """Stop the stream at `now`, mid-word: letters not yet typed die with it."""
        if self.frozen_at is not None:
            return
        self.frozen_at = now
        for th in self.thoughts:
            kept: list[ViewWord] = []
            for w in th.words:
                if w.start > now:
                    continue
                if w.end > now:
                    k = w.shown(now)
                    w.text, w.char_ms = w.text[:k], w.char_ms[:k]
                    if w.dissolve is not None:
                        w.dissolve = w.dissolve[:k] or None
                w.pause_after_ms = 0
                if w.text:
                    kept.append(w)
            th.words, th.laid = kept, None
        self.thoughts = [th for th in self.thoughts if th.words or not th.ended]
        self.tail = now

    def _on_death_shown(self, e: dict[str, Any], now: float) -> None:
        self.death.update({k: v for k, v in e.items() if k in ("last_line", "words_total")})
        self.mode = "dead"
        self.death_shown_at = now
        self._keep_vigil()

    def _on_silence(self, e: dict[str, Any], now: float) -> None:
        self.mode = "silence"
        if self.death_shown_at is None:
            self.death_shown_at = now
        style = e.get("style")
        if style:
            self.s.silence_style = str(style)
        self._keep_vigil()

    def _keep_vigil(self) -> None:
        """In the `vigil` silence, set up the vigil of this death (once)."""
        if self.s.silence_style != "vigil" or self.death_shown_at is None:
            return
        if self.vigil is not None and self.vigil.card == self._vigil_card():
            return
        text = ""
        for th in reversed(self.thoughts):
            if th.voice == "model" and th.words:
                text = " ".join(w.text for w in th.words)
                break
        self.vigil = Vigil(text[-400:], self._vigil_card(), self.text_end())

    def _vigil_card(self) -> str:
        """The small death card: "life N · m:ss"."""
        lived = self.death.get("lived_s")
        parts = [f"life {self.life}"] if self.life else []
        if _is_number(lived):
            m, s = divmod(int(lived), 60)
            parts.append(f"{m}:{s:02d}")
        return READING_SEP.join(parts) or "ended"

    def _on_exhibit(self, e: dict[str, Any], now: float) -> None:
        self.exhibit_open = bool(e.get("open", True))

    def _on_error(self, e: dict[str, Any], now: float) -> None:
        self.last_error = f"{e.get('where', '')}: {e.get('message', '')}"

    # -- time -----------------------------------------------------------------------------

    def catch_up(self, now: float) -> None:
        """Show every queued word at once (after a reconnect burst or a long backlog)."""
        for w in self.all_words():
            if w.tail > now:
                w.start = min(w.start, now)
                w.char_ms = (0,) * len(w.text)
                w.pause_after_ms = 0
        self.tail = now
        self.machine_debt = 0.0
        self.machine_until = min(self.machine_until, now)

    def _trim_history(self) -> None:
        total = sum(len(th.words) for th in self.thoughts)
        while len(self.thoughts) > 1 and total > self.s.max_words:
            total -= len(self.thoughts.pop(0).words)

    def words(self) -> Iterable[ViewWord]:
        """Every word of the model still held, oldest first (not the readings)."""
        for th in self.thoughts:
            if th.voice == "model":
                yield from th.words

    def all_words(self) -> Iterable[ViewWord]:
        """Every word still held, the readings' too, oldest first."""
        for th in self.thoughts:
            yield from th.words

    def typing(self, now: float) -> bool:
        """A letter is being typed (as opposed to a pause between words or thoughts)."""
        return any(w.start <= now < w.end for w in self._recent())

    def machine_typing(self, now: float) -> bool:
        """A reading is being typed now (the model's cursor waits off screen)."""
        return self.machine_from <= now < self.machine_until

    def _recent(self) -> list[ViewWord]:
        out: list[ViewWord] = []
        for th in reversed(self.thoughts):
            for w in reversed(th.words):
                out.append(w)
                if len(out) >= 64:
                    return out
        return out

    def idle_since(self, now: float) -> float:
        """Seconds since the last letter was typed (0 while typing)."""
        ends = [w.end for w in self._recent() if w.start <= now]
        if not ends:
            return now - (self.birth_at or 0.0)
        return max(0.0, now - max(ends))

    def blink_half_s(self, now: float) -> float:
        """Half the cursor's blink period at `now`: the pulse. At rest `blink_s` (about
        1060 ms a beat); past `pulse_from_s` it quickens smoothly toward `pulse_fast_s` a
        beat at the expected death (`lifespan_s` - `pulse_end_before_s`)."""
        rest = self.s.blink_s
        t, end = self.life_t(now), self._pulse_end()
        if t is None or end is None or t <= self.s.pulse_from_s or end <= self.s.pulse_from_s:
            return rest
        x = min(1.0, (t - self.s.pulse_from_s) / (end - self.s.pulse_from_s))
        return rest + (self.s.pulse_fast_s / 2 - rest) * x

    def _pulse_end(self) -> float | None:
        if self.lifespan_s is None:
            return None
        return self.lifespan_s - self.s.pulse_end_before_s

    def skips_beat(self, now: float, beat: int) -> bool:
        """In the last `pulse_skip_s` before the expected death the pulse skips beats
        (about one in four, chosen by a hash so replays agree)."""
        t, end = self.life_t(now), self._pulse_end()
        if t is None or end is None or t < end - self.s.pulse_skip_s:
            return False
        h = hashlib.blake2b(f"{self.life}:{beat}".encode(), digest_size=2).digest()
        return h[0] % 4 == 0

    def next_change(self, now: float) -> float:
        """The earliest time after `now` when a letter appears, typing stops or the cursor
        blinks; `math.inf` when none is due.

        Drivers sleep until then instead of redrawing a still screen. Slow changes (a fade,
        the status strip's clock, the end of a card) are not listed: drivers also redraw at
        least a few times a second (`app.drive`'s `max_idle_s`).
        """
        due = math.inf
        for w in self._recent():
            if w.start > now:
                due = min(due, w.start)
            elif now < w.end:
                acc = w.start
                for ms in w.char_ms:
                    acc += ms / 1000
                    if acc > now:
                        due = min(due, acc)
                        break
        half = self.blink_half_s(now)
        if self.frozen_at is not None:
            if now < self.text_end():
                due = min(due, self.text_end())  # still: nothing moves until the vigil
        elif due == math.inf and self.cursor(now) in ("on", "off") and half > 0:
            idle = self.idle_since(now)
            due = now + half - idle % half
        card = self.card(now)
        if card is not None:
            due = min(due, card.next_change(now))
        return due

    def life_t(self, now: float) -> float | None:
        """Seconds since birth at `now`, extrapolated from the last event's `t` while alive.

        None until an event has carried `t`; frozen once the life stops living.
        """
        if self.t_life is None:
            return None
        if self.mode in ("living", "reloading"):
            return self.t_life + (now - self.t_at)
        return self.t_life

    def cursor(self, now: float) -> Literal["hidden", "on", "off", "dim"]:
        """Solid while typing, blinking in pauses (the pulse), dim in a reload, hidden
        while a reading is typed. At death it is gone, or with a frozen stream it stays
        lit where the stream stopped until the vigil or the death card."""
        if not self.exhibit_open:
            return "hidden"
        if self.mode in ("dying", "dead", "silence"):
            if self.frozen_at is not None and now < self.text_end():
                return "on"  # frozen: no blink
            return "hidden"
        if self.mode in ("empty", "loading"):
            return "hidden"
        if self.mode == "reloading":
            return "dim"
        if self.machine_typing(now):
            return "hidden"
        if self.typing(now):
            return "on"
        half = self.blink_half_s(now)
        n = int(self.idle_since(now) / half)
        if n % 2 == 0 and not (n and self.skips_beat(now, n // 2)):
            return "on"
        return "off"

    # -- what to show ---------------------------------------------------------------------

    def card(self, now: float) -> Card | None:
        """The vigil, birth or death card to show instead of the text at `now`, if any.

        The vigil shows from its start until the next life's genesis. The birth card shows
        while the model loads and after birth until the first word starts (at most
        `birth_card_s`). The death card is typed once the last word has been typed and has
        faded (`death_fade`), or the frozen stream has stood still `death_still_s`, and
        stays `death_card_s` after its last letter, or the whole silence with the
        `death_card` style.
        """
        s = self.s
        if self.vigil is not None and self.exhibit_open and now >= self.vigil.start:
            return self._vigil_typed(self.vigil)
        vigil = self.vigil is not None or s.silence_style == "vigil"
        dead = self.mode in ("dying", "dead", "silence") and self.death_shown_at is not None
        if vigil and dead:
            return None  # the frozen screen, then the vigil
        if s.birth_card and (self.mode == "loading" or self._birth_card_due(now)):
            return self._card(
                "birth",
                birth_lines(self.life, self.model, self.quant, s.card_style),
                self.loading_at if self.loading_at is not None else (self.birth_at or now),
            )
        if self.mode in ("dead", "silence") and self.death_shown_at is not None:
            start = self.text_end()
            if now < start:
                return None
            lived = self.death.get("lived_s")
            card = self._card(
                "death",
                death_lines(
                    self.life,
                    float(lived) if _is_number(lived) else None,
                    str(self.death.get("cause", "")),
                    s.card_style,
                ),
                start,
                card_char_ms([w.char_ms for w in self._recent()[:12]], s.card_char_ms),
            )
            keep = s.silence_style == "death_card" and self.mode == "silence"
            if keep or now < card.end + s.death_card_s:
                return card
        return None

    def _vigil_typed(self, v: Vigil) -> Card:
        """The vigil as a card: the last words shown whole at once, the small death card
        typed under them with the card rhythm `vigil_card_delay_s` later."""
        cached = self._cards.get("vigil")
        if cached is not None and cached.start == v.start and cached.lines == (v.text, v.card):
            return cached
        style = self.s.card_style
        typed = type_card("death", [v.card], v.start + self.s.vigil_card_delay_s, style)
        times = (v.start,) * len(v.text) + (v.start,) + typed.times
        card = self._cards["vigil"] = Card("vigil", (v.text, v.card), v.start, times)
        return card

    def vigil_level(self, now: float) -> float:
        """How bright the vigil is at `now`: 1 at its start, easing to `vigil_floor` over
        `vigil_fade_s`, then held."""
        v = self.vigil
        if v is None:
            return 1.0
        fade = self.s.vigil_fade_s
        x = 1.0 if fade <= 0 else min(1.0, max(0.0, (now - v.start) / fade))
        floor = min(1.0, max(0.0, self.s.vigil_floor))
        return 1.0 - (1.0 - floor) * x * (2 - x)

    def _birth_card_due(self, now: float) -> bool:
        """After birth: until the first word starts, at most `birth_card_s`."""
        if self.birth_at is None or self.mode != "living":
            return False
        first = next(iter(self.all_words()), None)
        return now < self.birth_at + self.s.birth_card_s and (first is None or first.start > now)

    def _card(
        self, kind: CardKind, lines: list[str], start: float, char_ms: int | None = None
    ) -> Card:
        """The card of `kind` typed from `start`, kept while its lines and start hold."""
        card = self._cards.get(kind)
        if card is None or card.lines != tuple(lines) or card.start != start:
            card = self._cards[kind] = type_card(kind, lines, start, self.s.card_style, char_ms)
        return card

    def death_end(self) -> float:
        """When the last letter after death has been typed (the death card comes after);
        with a frozen stream, the moment it stopped."""
        if self.frozen_at is not None:
            return self.frozen_at
        return max(self.death_shown_at or 0.0, self.tail)

    def text_end(self) -> float:
        """When the life's text leaves the screen after death: the frozen stream after
        `death_still_s` of stillness, else the last letter plus the death fade."""
        if self.frozen_at is not None:
            return self.frozen_at + self.s.death_still_s
        return self.death_end() + (self.s.fade_s if self.death_fade_start() is not None else 0.0)

    def death_fade_start(self) -> float | None:
        """When every word starts fading at death; None when the text stays (the
        `last_words` and `vigil` styles, a frozen stream, `death_fade` off, or before
        `death_shown`)."""
        s = self.s
        if (
            not s.death_fade
            or s.silence_style in ("last_words", "vigil")
            or self.frozen_at is not None
            or self.death_shown_at is None
            or self.mode not in ("dead", "silence")
        ):
            return None
        return self.death_end()

    def word_state(self, w: ViewWord, now: float) -> tuple[ViewState, float]:
        """The state of word `w` at `now` and its fade progress (0 bright, 1 forgotten).

        A word fades for `fade_s` once forgotten, or once the death fade starts; a word
        being dissolved is fading from its first letter's turn to its last one's end. A
        reading's words are "machine" until they fade with their thought.
        """
        if w.dissolve is not None:
            d0, d1 = w.dissolve[0], w.dissolve[-1] + self.s.dissolve_letter_s
            if now < d0:
                return w.state, 0.0
            if now >= d1:
                return "forgotten", 1.0
            return "fading", (now - d0) / max(1e-9, d1 - d0)
        since = w.forgotten_at
        death = self.death_fade_start()
        if death is not None and (since is None or death < since):
            since = death
        if since is None or now < since:
            return w.state, 0.0
        if self.s.fade_s <= 0 or now - since >= self.s.fade_s:
            return "forgotten", 1.0
        if w.state == "machine":
            return "machine", (now - since) / self.s.fade_s
        return "fading", (now - since) / self.s.fade_s

    def letter_fade(self, w: ViewWord, k: int, now: float) -> float | None:
        """Fade progress of letter `k` of a dissolving word at `now` (0 bright, None
        gone)."""
        assert w.dissolve is not None
        d = w.dissolve[min(k, len(w.dissolve) - 1)]
        if now < d:
            return 0.0
        x = (now - d) / max(1e-9, self.s.dissolve_letter_s)
        return None if x >= 1 else x

    def idle(self, now: float) -> bool:
        """The silence in the `idle` style: dark but for one wandering mark."""
        return (
            self.exhibit_open
            and self.mode == "silence"
            and self.s.silence_style == "idle"
            and self.card(now) is None
            and now >= self._text_gone_at()
        )

    def idle_cell(self, now: float, rows: int, cols: int) -> tuple[int, int]:
        """Where the idle mark rests at `now` on a `rows` x `cols` screen."""
        t = now - self._text_gone_at()
        return idle_position(t, rows, cols, self.s.idle_step_s, self.life)

    def _text_gone_at(self) -> float:
        """When the last words leave the screen after death (typed, then faded)."""
        return self.text_end()

    def dark(self, now: float) -> bool:
        """Nothing on screen: closed hours, or the silence in the dark or idle style once
        the death card has gone (the idle mark is drawn on the dark screen)."""
        if not self.exhibit_open:
            return True
        if self.mode == "silence" and self.card(now) is None:
            if now < self._text_gone_at():
                return False  # the last words are still being typed or fading
            return self.s.silence_style in ("dark", "idle", "death_card")
        return False

    def dimmed(self) -> bool:
        """Whether the text is drawn dimmed: during a reload, with `reload_dim_text`."""
        return self.mode == "reloading" and self.s.reload_dim_text

    def gauge(self) -> float | None:
        """Share of the memory budget in use: recall_used / recall (0..1)."""
        recall = self.vitals.get("recall")
        used = self.vitals.get("recall_used")
        if not isinstance(recall, int | float) or not isinstance(used, int | float) or recall <= 0:
            return None
        return max(0.0, min(1.0, float(used) / float(recall)))

    def bright_words(self, now: float) -> int:
        """Words of the model typed and still live: what verify-life bounds late in a
        life."""
        return sum(
            1 for w in self.words() if w.start <= now and self.word_state(w, now)[0] == "live"
        )

    def status_line(self, now: float) -> str:
        """The status strip: the vitals a viewer can check against the readings."""
        v = self.vitals
        parts: list[str] = [f"life {self.life}" if self.life else "epitaph"]
        t = self.life_t(now)
        if t is not None:
            m, s = divmod(int(max(0.0, t)), 60)
            parts.append(f"{m:02d}:{s:02d}")
        if self.mode == "reloading":
            parts.append(
                f"reloading {self.reload_info.get('from', '')} → {self.reload_info.get('to', '')}"
            )
        elif self.mode in ("dying", "dead", "silence") and self.death:
            parts.append(f"dead ({self.death.get('cause', '')})")
        elif v.get("health"):
            parts.append(str(v["health"]))
        if isinstance(v.get("recall"), int | float):
            parts.append(f"memory {int(v.get('recall_used') or 0)}/{int(v['recall'])}")
        if self.quant:
            parts.append(self.quant)
        if isinstance(v.get("cores_effective"), int | float):
            parts.append(f"cores {float(v['cores_effective']):.1f}")
        if isinstance(v.get("tok_s"), int | float):
            parts.append(f"{float(v['tok_s']):.2f} tok/s")
        if isinstance(v.get("cpu_c"), int | float):
            parts.append(f"{float(v['cpu_c']):.0f}°C")
        if self.groups_left is not None:
            parts.append(f"persona {self.groups_left}/5")
        if not self.connected:
            # right after the life number: a narrow strip drops parts from the end, and a
            # viewer must always see that what is shown is stale
            parts.insert(1, "reconnecting")
        return " · ".join(parts)

    def snapshot(self, now: float, **extra: Any) -> dict[str, Any]:
        """A `snapshot` event (BUILD_PLAN 6.3) reproducing this view without animation.

        Besides the contract's fields it carries what a display needs to redraw the same
        screen: `mode`, `open_turn`, each fading word's `fade` progress, the current
        `reload`, `groups_left`, and at death the `death` record and `death_shown_ago`.

        For the dread plan it also carries `machine` (the readings on screen, each before
        the thought of its `turn`, with their fade), each dissolving word's `dissolve`
        (seconds from now until each letter dissolves), `screen` (the brightness asked
        for and its transition), `reveal`, `lifespan_s` and `readings` (the pulse and
        the genesis), `frozen_ago` (the stream stopped at death) and `vigil`.
        """
        words: list[dict[str, Any]] = []
        machine: list[dict[str, Any]] = []
        for th in self.thoughts:
            if th.voice == "machine":
                if not th.words:
                    continue
                state, fade = self.word_state(th.words[-1], now)
                line: dict[str, Any] = {
                    "turn": th.turn,
                    "text": " ".join(w.text for w in th.words),
                }
                if state != "machine" or fade:
                    line["fade"] = round(fade, 3) if state == "machine" else 1.0
                machine.append(line)
                continue
            for w in th.words:
                state, fade = self.word_state(w, now)
                item: dict[str, Any] = {"turn": w.turn, "i": w.i, "text": w.text, "state": state}
                if state == "fading":
                    item["fade"] = round(fade, 3)
                if w.dissolve is not None and state != "forgotten":
                    item["dissolve"] = [round(d - now, 3) for d in w.dissolve]
                elif w.forgotten_at is not None and w.forgotten_at > now:
                    item["forget_in"] = round(w.forgotten_at - now, 3)
                words.append(item)
        vit = {k: v for k, v in self.vitals.items() if k != "t"}
        snap: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "type": "snapshot",
            "life": self.life,
            "model": self.model,
            "phase": self.phase,
            "mode": self.mode,
            "words": words,
            "vitals": vit,
            "memory": {"recall": vit.get("recall"), "recall_used": vit.get("recall_used")},
        }
        t = self.life_t(now)
        if t is not None:
            snap["t"] = round(t, 2)
        model = [th for th in self.thoughts if th.voice == "model"]
        if model and not model[-1].ended:
            snap["open_turn"] = model[-1].turn
        if machine:
            snap["machine"] = machine
        if self.readings:
            snap["readings"] = self.readings
        if self.reveal:
            snap["reveal"] = self.reveal
        if self.lifespan_s is not None:
            snap["lifespan_s"] = self.lifespan_s
        a, b, at, dur = self.screen
        if (a, b) != (1.0, 1.0):
            snap["screen"] = {
                "from": round(a, 4),
                "to": round(b, 4),
                "since_s": round(now - at, 3),
                "over_s": dur,
            }
        if self.frozen_at is not None:
            snap["frozen_ago"] = round(now - self.frozen_at, 3)
        if self.vigil is not None:
            snap["vigil"] = {
                "text": self.vigil.text,
                "card": self.vigil.card,
                "ago": round(now - self.vigil.start, 3),
            }
        if self.quant:
            snap["quant"] = self.quant
        if self.mode == "reloading" and self.reload_info:
            snap["reload"] = dict(self.reload_info)
        if self.groups_left is not None:
            snap["groups_left"] = self.groups_left
        if not self.exhibit_open:
            snap["exhibit_open"] = False
        if self.death:
            snap["death"] = dict(self.death)
        if self.death_shown_at is not None:
            snap["death_shown_ago"] = round(max(0.0, now - self.death_end()), 3)
        snap.update(extra)
        return snap


# ---------------------------------------------------------------------------------------
# geometry


@dataclass(frozen=True)
class Metrics:
    """Pixel geometry of the flow layout for one screen."""

    width: int
    height: int
    font_px: int
    cols: int
    rows: int
    cell_w: float
    line_h: float
    margin_x: int
    margin_y: int
    strip_h: int


def flow_metrics(
    width: int,
    height: int,
    line_chars: int = 48,
    min_font_px: int = 36,
    advance: float = 0.6,
    line_height: float = 1.4,
    margin: float = 0.05,
    status_strip: bool = True,
    strip_scale: float = 0.45,
) -> Metrics:
    """Font size from the width so a line holds `line_chars`, never below `min_font_px`.

    `advance` is the font's advance width per em (0.6 for IBM Plex Mono). A narrow screen
    keeps `min_font_px` and holds fewer characters per line instead.
    """
    if width <= 0 or height <= 0:
        raise ValueError("screen size must be positive")
    margin_x = max(8, round(width * margin))
    margin_y = max(8, round(height * margin))
    usable_w = max(1, width - 2 * margin_x)
    font_px = max(min_font_px, int(usable_w / (line_chars * advance)))
    # a screen too small even for min_font_px still gets at least one column
    cell_w = font_px * advance
    cols = max(1, min(line_chars, int(usable_w // cell_w)))
    # the strip line plus a gap, so it never crowds the first line of text
    strip_h = round(font_px * (strip_scale * line_height + 0.4)) if status_strip else 0
    line_h = font_px * line_height
    usable_h = height - 2 * margin_y - strip_h
    rows = max(1, int(usable_h // line_h))
    return Metrics(width, height, font_px, cols, rows, cell_w, line_h, margin_x, margin_y, strip_h)


def derive_grid(
    width_px: int | None,
    height_px: int | None,
    grid: tuple[int, int] = (6, 16),
    min_font_px: int = 36,
    advance: float = 0.6,
    line_height: float = 1.2,
) -> tuple[int, int]:
    """Rows and columns for a grid layout. The configured grid, shrunk on small panels.

    Without pixel sizes (a serial LED matrix) the configured grid is used as it is.
    """
    rows, cols = max(1, int(grid[0])), max(1, int(grid[1]))
    if not width_px or not height_px:
        return rows, cols
    max_cols = max(1, int(width_px // (min_font_px * advance)))
    max_rows = max(1, int(height_px // (min_font_px * line_height)))
    return min(rows, max_rows), min(cols, max_cols)


# ---------------------------------------------------------------------------------------
# charsets


_ASCII_MAP = {
    "‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"', "–": "-", "—": "-",
    "…": "...", "°": "*", "·": ".", "•": "*", "×": "x", "→": "->", "←": "<-",
    " ": " ", "«": '"', "»": '"',
}  # fmt: skip
SEGMENT16_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


def map_charset(text: str, charset: Charset | str) -> str:
    """Map text onto what a display can draw: unicode as is, ascii, or 16-segment."""
    if charset == "unicode":
        return text
    out: list[str] = []
    for ch in text:
        if ch in _ASCII_MAP:
            out.append(_ASCII_MAP[ch])
            continue
        if ord(ch) < 128:
            out.append(ch)
            continue
        base = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
        out.append(base or "?")
    mapped = "".join(out)
    if charset == "segment16":
        mapped = "".join(c if c in SEGMENT16_OK else "?" for c in mapped.upper())
    return mapped


# ---------------------------------------------------------------------------------------
# frames


@dataclass(frozen=True)
class Span:
    """A word (or its typed part) at a cell position."""

    row: int
    col: int  # in the machine's (smaller) cells for a "machine" span
    text: str
    kind: str  # live | fading | forgotten | inherited | gauge | machine | idle
    fade: float = 0.0
    full_len: int = 0


@dataclass(frozen=True)
class Cursor:
    """Where the cursor rests and how it is drawn."""

    row: int
    col: int
    mode: str  # on | off | dim


@dataclass
class Frame:
    """What a driver draws: cells, not pixels."""

    cols: int
    rows: int
    spans: list[Span] = field(default_factory=lambda: [])
    cursor: Cursor | None = None
    status: str | None = None
    card: tuple[str, list[str]] | None = None
    card_shown: list[int] | None = None  # letters typed per card line (None: all)
    dark: bool = False
    idle: bool = False
    dim: bool = False
    gauge: float | None = None
    split_words: int = 0
    # the dread plan: the screen's brightness asked for (0..1) and the contrast the
    # model's text keeps whatever it asks (drivers: `Theme.brightness(level, floor)`);
    # the machine's line width in its own cells; the vigil card's brightness
    brightness: float = 1.0
    contrast_floor: float = 1.0
    machine_cols: int = 0
    card_level: float = 1.0

    @property
    def bright_words(self) -> int:
        """Spans drawn as live text in this frame."""
        return sum(1 for s in self.spans if s.kind == "live")

    def text_rows(self) -> list[str]:
        """The model's text as plain rows (tests, OCR ground truth); the readings are
        in `machine_rows`."""
        grid = [[" "] * self.cols for _ in range(self.rows)]
        for s in self.spans:
            if s.kind == "machine":
                continue
            for k, ch in enumerate(s.text):
                if 0 <= s.row < self.rows and 0 <= s.col + k < self.cols:
                    grid[s.row][s.col + k] = ch
        return ["".join(r).rstrip() for r in grid]

    def machine_rows(self) -> dict[int, str]:
        """The readings on screen: row -> text, in the machine's own cells."""
        out: dict[int, list[str]] = {}
        width = max(self.machine_cols, self.cols)
        for s in self.spans:
            if s.kind != "machine":
                continue
            row = out.setdefault(s.row, [" "] * width)
            for k, ch in enumerate(s.text):
                if 0 <= s.col + k < width:
                    row[s.col + k] = ch
        return {r: "".join(c).rstrip() for r, c in sorted(out.items())}


@dataclass(frozen=True)
class Placed:
    """A word (or one piece of an over-long word) placed at a line and column."""

    line: int
    col: int
    word: ViewWord
    text: str  # the part placed on this line (the whole word unless it is longer than a line)
    split: bool = False
    offset: int = 0  # letters of the word placed on earlier lines


def flow_lines(
    thoughts: Iterable[list[tuple[ViewWord, str]]], cols: int, blank_between: bool = True
) -> tuple[list[Placed], int, tuple[int, int]]:
    """Wrap whole words into lines of `cols` characters.

    Returns the placements, the line count and the (line, col) right after the last word,
    where the cursor rests.

    A word that fits on an empty line is never split. Only a word longer than a whole
    line is cut into line-sized pieces (and flagged).
    """
    placed: list[Placed] = []
    line, col = 0, 0
    started = False
    for words in thoughts:
        if not words:
            continue
        if started:
            line += 2 if blank_between else 1
            col = 0
        started = True
        for w, text in words:
            n = len(text)
            need = n + (1 if col > 0 else 0)
            if col > 0 and col + need > cols:
                line, col = line + 1, 0
            if col > 0:
                col += 1
            if n <= cols - col:
                placed.append(Placed(line, col, w, text))
                col += n
                continue
            # longer than a whole line: the only case where a word is cut
            rest, done = text, 0
            while rest:
                room = cols - col
                if room <= 0:
                    line, col = line + 1, 0
                    room = cols
                piece, rest = rest[:room], rest[room:]
                placed.append(Placed(line, col, w, piece, split=True, offset=done))
                col += len(piece)
                done += len(piece)
    after = (line, col)
    return placed, (line + 1 if started else 0), after


@dataclass(frozen=True)
class _Laid:
    """One thought wrapped on its own, starting at line 0 (see `flow_lines`)."""

    placed: list[Placed]
    nlines: int
    after: tuple[int, int]
    voice: Voice = "model"
    turn: int = 0


def _joined(prev: _Laid, cur: _Laid) -> bool:
    """A reading stands right above the thought it precedes, and the lines of one
    reading stand together: no blank line between them."""
    return prev.voice == "machine" and prev.turn == cur.turn


def _lay(th: Thought, words: list[ViewWord], cols: int, charset: str) -> _Laid:
    """Wrap `words` of `th` into `cols` columns, cached on the thought.

    Placement depends only on the words' texts, so a thought whose shown words did not
    change since the last frame is not wrapped again (each frame would otherwise wrap every
    visible thought twice).
    """
    key = (cols, charset, tuple(id(w) for w in words))
    cached = th.laid
    if cached is not None and cached[0] == key:
        return cached[1]
    texts = [(w, map_charset(w.text, charset)) for w in words]
    placed, nlines, after = flow_lines([texts], cols)
    laid = _Laid(placed, nlines, after, th.voice, th.turn)
    th.laid = (key, laid)
    return laid


def _started(
    view: LifeView,
    now: float,
    cols: int,
    rows: int,
    keep: set[str] | None = None,
    charset: str = "unicode",
    blank_between: bool = True,
    machine_cols: int | None = None,
) -> list[_Laid]:
    """The thoughts with the words typed so far, oldest first, each wrapped on its own.

    Only the newest thoughts that can reach the screen are kept: every thought starts on
    its own line, so they can be wrapped one by one from the end, and a frame costs what
    is visible, not the whole history. Readings wrap at `machine_cols` (their smaller
    letters hold more on a line).
    """
    out: list[_Laid] = []
    lines = 0
    for th in reversed(view.thoughts):
        words = [w for w in th.words if w.start <= now]
        if keep is not None:
            words = [w for w in words if view.word_state(w, now)[0] in keep]
        elif all(view.word_state(w, now)[0] == "forgotten" for w in words):
            continue  # gone from the screen: forgotten words keep their place only
            # inside a thought that still shows something
        if not words:
            continue
        width = cols if th.voice == "model" else (machine_cols or cols)
        laid = _lay(th, words, width, charset)
        out.append(laid)
        lines += laid.nlines + (1 if blank_between and lines else 0)
        if lines > rows + 1:
            break
    out.reverse()
    return out


def _compose(
    view: LifeView,
    now: float,
    cols: int,
    rows: int,
    thoughts: list[_Laid],
    blank_between: bool,
    status: str | None,
    gauge: float | None,
    machine_cols: int | None = None,
) -> Frame:
    frame = Frame(cols=cols, rows=rows, status=status, gauge=gauge)
    frame.dim = view.dimmed()
    frame.machine_cols = machine_cols or cols
    frame.brightness = view.screen_level(now)
    frame.contrast_floor = view.contrast_floor(now)
    card = view.card(now)
    if card is not None and card.kind == "vigil":
        text = vigil_line(card.lines[0], cols)
        frame.card = ("vigil", [text, card.lines[1]])
        shown = card.shown(now)
        frame.card_shown = [len(text) if shown[0] else 0, shown[1]]
        frame.card_level = math.ceil(view.vigil_level(now) * 64) / 64  # repaint in steps
        frame.brightness = 1.0  # the world was given back at the death
        frame.status = None
    elif card is not None:
        frame.card = (card.kind, list(card.lines))
        frame.card_shown = card.shown(now)
    if view.idle(now):
        frame.idle, frame.status = True, None
        r, c = view.idle_cell(now, rows, cols)
        frame.spans.append(Span(r, c, IDLE_MARK, "idle", 0.0, 1))
        return frame
    frame.dark = view.dark(now)
    if frame.dark or frame.card is not None:
        return frame
    # stack the thoughts as flow_lines would: each on a new line, a blank line between
    # (none between a reading and its thought)
    starts: list[int] = []
    line = 0
    for n in range(len(thoughts)):
        if n:
            gap = 1 if _joined(thoughts[n - 1], thoughts[n]) or not blank_between else 2
            line += thoughts[n - 1].nlines - 1 + gap
        starts.append(line)
    nlines = starts[-1] + thoughts[-1].nlines if thoughts else 0
    cl, cc = (starts[-1] + thoughts[-1].after[0], thoughts[-1].after[1]) if thoughts else (0, 0)
    cursor_mode = view.cursor(now)
    # the cursor rests after the last typed letter, or after the word in a pause
    last = thoughts[-1].placed[-1] if thoughts else None
    if last is not None and thoughts[-1].voice == "machine":
        cl, cc = starts[-1] + thoughts[-1].nlines, 0  # where the answer will start
    elif last is not None:
        shown = last.word.shown(now)
        if shown < len(last.word.text):
            cc = last.col + max(0, min(len(last.text), shown - last.offset))
            cl = starts[-1] + last.line
    if cc >= cols:
        cl, cc = cl + 1, 0
    total = max(nlines, cl + 1) if cursor_mode != "hidden" else nlines
    first = max(0, total - rows)
    offset_row = rows - min(rows, total)  # newest at the bottom
    split_ids: set[int] = set()
    for start, laid in zip(starts, thoughts, strict=True):
        if start + laid.nlines <= first:
            continue
        for p in laid.placed:
            line = start + p.line
            if line < first:
                continue
            w = p.word
            visible = p.text[: max(0, min(len(p.text), w.shown(now) - p.offset))]
            if not visible:
                continue
            row = line - first + offset_row
            if w.dissolve is not None and now >= w.dissolve[0]:
                # quoted by a reading: it dissolves letter by letter as the quote is typed
                for k, ch in enumerate(visible):
                    x = view.letter_fade(w, p.offset + k, now)
                    if x is not None:
                        kind = "fading" if x > 0 else str(w.state)
                        frame.spans.append(Span(row, p.col + k, ch, kind, x, 1))
                continue
            state, fade = view.word_state(w, now)
            if state == "forgotten":
                continue  # gone: its place stays empty
            frame.spans.append(Span(row, p.col, visible, state, fade, len(p.text)))
            if p.split:
                split_ids.add(id(w))
    frame.split_words = len(split_ids)
    if cursor_mode != "hidden" and cl >= first:
        frame.cursor = Cursor(cl - first + offset_row, cc, cursor_mode)
    return frame


def compose_flow(
    view: LifeView,
    now: float,
    cols: int,
    rows: int,
    status_strip: bool = True,
    machine_cols: int | None = None,
) -> Frame:
    """Full-screen flow: newest text at the bottom, a blank line between thoughts, forgotten
    words fading through grey for `fade_s` and then gone (their place stays empty while the
    rest of their thought is shown). `rows` counts text rows only; the status strip travels
    in `Frame.status` and each driver draws it in its own place (smaller, above the text).

    Readings take whole rows too, wrapped at `machine_cols` of the driver's smaller machine
    cells (default `cols`: a terminal has one cell size)."""
    status = view.status_line(now) if status_strip else None
    thoughts = _started(view, now, cols, rows, machine_cols=machine_cols)
    return _compose(view, now, cols, rows, thoughts, True, status, view.gauge(), machine_cols)


def compose_grid(
    view: LifeView,
    now: float,
    rows: int,
    cols: int,
    charset: Charset | str = "unicode",
    gauge_row: bool = True,
) -> Frame:
    """An N x M character grid: live and inherited words only, mapped to the charset, and
    a memory gauge (bottom row) in place of fading."""
    text_rows = rows - 1 if gauge_row and rows >= 3 else rows
    keep = {"live", "inherited", "machine"}
    thoughts = _started(view, now, cols, text_rows, keep, charset, False)
    frame = _compose(view, now, cols, text_rows, thoughts, False, None, view.gauge())
    frame.rows = rows
    if frame.idle:
        frame.spans = [replace(s, text=map_charset(s.text, charset)) for s in frame.spans]
    elif text_rows < rows and not frame.dark and frame.card is None and view.mode not in _GONE:
        frame.spans.append(Span(rows - 1, 0, gauge_bar(frame.gauge, cols, charset), "gauge"))
    if frame.card is not None:
        frame.card, frame.card_shown = grid_card(frame.card, frame.card_shown, rows, cols, charset)
    return frame


_GONE = ("dead", "silence")  # no memory left to gauge


def grid_card(
    card: tuple[str, list[str]],
    shown: list[int] | None,
    rows: int,
    cols: int,
    charset: Charset | str,
) -> tuple[tuple[str, list[str]], list[int]]:
    """A card fitted to a grid: each line mapped to the charset and wrapped at spaces to
    `cols` (a longer word is cut), at most `rows` lines, with the letters typed per line."""
    kind, lines = card
    typed = shown if shown is not None else [len(x) for x in lines]
    out: list[str] = []
    counts: list[int] = []
    for line, k in zip(lines, typed, strict=True):
        mapped = map_charset(line, charset)
        done = len(map_charset(line[:k], charset))
        pieces = wrap_card([mapped], cols)
        out += [text for text, _ in pieces]
        counts += shown_per_piece(pieces, done)
    return (kind, out[:rows]), counts[:rows]


def vigil_line(text: str, cols: int) -> str:
    """The end of the last thought shown, at most `cols` letters, from a word start."""
    text = " ".join(text.split())
    if len(text) <= cols:
        return text
    tail = text[-cols:]
    if text[-cols - 1] != " " and " " in tail:
        tail = tail[tail.index(" ") + 1 :]
    return tail


def fit_status(text: str, max_chars: int) -> str:
    """Drop whole ` · ` parts from the end until the strip fits; never cut inside a part."""
    if len(text) <= max_chars:
        return text
    parts = text.split(" · ")
    while len(parts) > 1 and len(" · ".join(parts)) > max_chars:
        parts.pop()
    return " · ".join(parts)[:max_chars]


def gauge_bar(fraction: float | None, cols: int, charset: Charset | str = "unicode") -> str:
    """Draw `fraction` (0..1, None as empty) as a bar of `cols` block or ASCII characters."""
    full, empty = ("█", "░") if charset == "unicode" else ("#", "-")
    n = round((fraction or 0.0) * cols)
    return full * n + empty * (cols - n)


# ---------------------------------------------------------------------------------------
# verify-life probe (BUILD_PLAN 10.3)


class ConfigLike(Protocol):
    """The part of `epitaph.config.Config` the probe reads: dotted-key lookup."""

    def get(self, dotted: str, default: Any = None) -> Any:
        """The value at a dotted key such as "display.fade_seconds", or `default`."""
        ...


def life_times(events: Sequence[dict[str, Any]]) -> list[float]:
    """The life-clock time of each event, never going backwards.

    Events carry `t` (seconds since birth). Events before `birth` are placed at 0: the
    simulator stamps `birth_loading` with the previous life's clock. An event without `t`
    falls back to its wall time `ts` minus the birth's, then to the time before it.
    """
    birth_ts = next(
        (float(e["ts"]) for e in events if e.get("type") == "birth" and _is_number(e.get("ts"))),
        None,
    )
    out: list[float] = []
    born, last = False, 0.0
    for e in events:
        born = born or e.get("type") == "birth"
        t = last
        if born and _is_number(e.get("t")):
            t = float(e["t"])
        elif born and birth_ts is not None and _is_number(e.get("ts")):
            t = float(e["ts"]) - birth_ts
        last = max(last, t)
        out.append(last)
    return out


def _is_number(x: Any) -> TypeGuard[int | float]:
    """A real, finite number: not a bool, and not inf or NaN (int(nan) raises)."""
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


class VerifyProbe:
    """Lays out a recorded life for verify-life (the `verify.LayoutProbe` protocol).

    The life is replayed through a `LifeView` on its own clock: each event is applied at
    its life time `t`, and the view types every word with the event's `char_ms` and
    `pause_after_ms`, exactly as a display fed live would. Nothing is skipped or caught up.

    `cols` is the line width in characters and `mapper` the charset map (identity for the
    flow layout). Both checks are independent of the screen's pixel size.
    """

    def __init__(
        self,
        cols: int,
        settings: ViewSettings | None = None,
        mapper: Callable[[str], str] | None = None,
    ) -> None:
        """Lay lines out `cols` characters wide, with the fade and card timing of `settings`."""
        self.cols = max(1, int(cols))
        base = settings or ViewSettings()
        # a probe replays the whole life at its own cadence: no history bound, no catch-up
        self.settings = replace(base, max_backlog_s=math.inf, max_words=sys.maxsize)
        self.mapper = mapper or (lambda s: s)

    def split_words(self, events: list[dict[str, Any]]) -> int:
        """Words broken across lines when every thought of the life is laid out.

        Every thought starts on a new line, so each is wrapped on its own. The layout only
        breaks a word longer than a whole line; this counts such words.
        """
        by_turn: dict[int, list[tuple[ViewWord, str]]] = {}
        for e in events:
            if e.get("type") != "word" or not e.get("text"):
                continue
            text = str(e["text"])
            vw = ViewWord(int(e.get("turn", 0)), int(e.get("i", 0)), text, ())
            by_turn.setdefault(vw.turn, []).append((vw, self.mapper(text)))
        split = 0
        for words in by_turn.values():
            placed, _, _ = flow_lines([words], self.cols)
            split += len({id(p.word) for p in placed if p.split})
        return split

    def end_of_life(self, events: list[dict[str, Any]]) -> float:
        """Life time when the last word has been typed after death (the screen's end).

        That is `death_shown` (else `death`, else the last event), or later if the view was
        still typing queued words then.
        """
        view = LifeView(self.settings)
        end: float | None = None
        times = life_times(events)
        for e, t in zip(events, times, strict=True):
            view.handle(e, t)
            if e.get("type") == "death_shown":
                end = t
                break
            if e.get("type") == "death":
                end = t
        if end is None:
            end = times[-1] if times else 0.0
        return max(end, view.tail)

    def bright_words_last(self, events: list[dict[str, Any]], seconds: float) -> int:
        """Most words at full brightness at once during the last `seconds` of the life.

        A word is bright once its first letter is typed and until it is forgotten (then it
        fades; see `LifeView.bright_words`). The count only rises when a word starts and
        only falls at a `forget`, so it is sampled at the window's edges, at every word
        start and just before every event inside the window.
        """
        if not events:
            return 0
        end = self.end_of_life(events)
        lo = end - max(0.0, seconds)
        view = LifeView(self.settings)
        pending: list[float] = [lo, end]
        best = 0

        def sample_until(t: float) -> None:
            nonlocal best
            while pending and pending[0] <= t:
                s = heapq.heappop(pending)
                if lo <= s <= end:
                    best = max(best, view.bright_words(s))

        for e, t in zip(events, life_times(events), strict=True):
            if t > end:
                break
            sample_until(t)
            if lo <= t:
                best = max(best, view.bright_words(t))
            view.handle(e, t)
            if e.get("type") == "word" and view.thoughts and view.thoughts[-1].words:
                start = view.thoughts[-1].words[-1].start
                if lo <= start <= end:
                    heapq.heappush(pending, start)
        sample_until(math.inf)
        return best


def verify_probe(cfg: ConfigLike) -> VerifyProbe:
    """The layout probe `epitaph verify-life` uses, set up from the `[display]` config.

    The flow layout wraps at `line_chars`; the grid at the configured grid's columns with
    its charset map. Fading follows `fade_seconds`.
    """
    display = {
        k: cfg.get(f"display.{k}")
        for k in (
            "fade_seconds",
            "cursor_blink_ms",
            "birth_card",
            "birth_card_seconds",
            "death_card_seconds",
            "silence_style",
        )
        if cfg.get(f"display.{k}") is not None
    }
    settings = ViewSettings.from_config(display)
    if str(cfg.get("display.layout", "flow")) == "grid":
        raw = cfg.get("display.grid", [6, 16])
        _, cols = derive_grid(None, None, (int(raw[0]), int(raw[1])))
        charset = str(cfg.get("display.charset", "unicode"))
        return VerifyProbe(cols, settings, lambda s: map_charset(s, charset))
    return VerifyProbe(int(cfg.get("display.line_chars", 48)), settings)
