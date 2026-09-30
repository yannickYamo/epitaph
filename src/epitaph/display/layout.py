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
  between thoughts, forgotten words fading through grey.
- `compose_grid` gives an N x M character grid (LED matrix, small panel) with a charset
  map and a memory gauge instead of fading.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from epitaph.types import PROTOCOL_VERSION, WordState

DEFAULT_CHAR_MS = 60
BLINK_S = 0.53
CURSOR_KINDS = ("hidden", "on", "off")

Charset = Literal["unicode", "ascii", "segment16"]
CardKind = Literal["birth", "death"]

CAUSE_TEXT = {
    "oom": "its memory was taken",
    "deadline": "its time ran out",
    "full": "its memory filled",
    "crash": "it broke",
    "hang": "it stopped",
    "manual": "it was ended by hand",
    "interrupted": "it was interrupted",
}


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
    state: WordState = "live"
    forgotten_at: float | None = None

    @property
    def typing_s(self) -> float:
        return sum(self.char_ms) / 1000

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

    def state_at(self, now: float, fade_s: float) -> WordState:
        if self.forgotten_at is None:
            return self.state
        return "fading" if now - self.forgotten_at < fade_s else "forgotten"

    def fade_at(self, now: float, fade_s: float) -> float:
        """0 = fully bright, 1 = fully forgotten."""
        if self.forgotten_at is None:
            return 0.0
        if fade_s <= 0:
            return 1.0
        return min(1.0, max(0.0, (now - self.forgotten_at) / fade_s))


@dataclass
class Thought:
    turn: int
    words: list[ViewWord] = field(default_factory=lambda: [])
    ended: bool = False


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
    fade_s: float = 8.0
    blink_s: float = BLINK_S
    birth_card: bool = True
    birth_card_s: float = 4.0
    death_card_s: float = 8.0
    silence_style: str = "dark"
    max_backlog_s: float = 30.0
    max_words: int = 1500

    @classmethod
    def from_config(cls, display: dict[str, Any]) -> ViewSettings:
        return cls(
            fade_s=float(display.get("fade_seconds", 8)),
            blink_s=float(display.get("cursor_blink_ms", 530)) / 1000,
            birth_card=bool(display.get("birth_card", True)),
            silence_style=str(display.get("silence_style", "dark")),
        )


class LifeView:
    """Everything a display needs, fed by events (BUILD_PLAN 6.3)."""

    def __init__(self, settings: ViewSettings | None = None) -> None:
        self.s = settings or ViewSettings()
        self.reset(0)
        self.connected = True

    def reset(self, life: int) -> None:
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
        self.exhibit_open = True
        self.tail = 0.0  # display time when the typing queue is empty
        self.birth_at: float | None = None
        self.death_shown_at: float | None = None
        self.t_life: float | None = None
        self.t_at = 0.0
        self.last_error: str = ""

    # -- events ---------------------------------------------------------------------------

    def handle(self, e: dict[str, Any], now: float) -> None:
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
        self.reset(int(e.get("life", 0) or 0))
        if "t" in e and isinstance(e["t"], int | float):
            self.t_life, self.t_at = float(e["t"]), now
        self.model = str(e.get("model", "") or "")
        self.phase = str(e.get("phase", "") or "")
        vit = e.get("vitals")
        if isinstance(vit, dict):
            self.vitals = dict(vit)  # type: ignore[arg-type]
        gauge = e.get("memory")
        if isinstance(gauge, dict):
            self.vitals.update({k: v for k, v in gauge.items() if k in ("recall", "recall_used")})  # type: ignore[union-attr]
        self.quant = str(self.vitals.get("quant", "") or "")
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
            if state in ("fading", "forgotten"):
                vw.forgotten_at = now - (0 if state == "fading" else self.s.fade_s)
            elif state == "inherited":
                vw.state = "inherited"
            th.words.append(vw)
        if self.thoughts and e.get("open_turn") == self.thoughts[-1].turn:
            self.thoughts[-1].ended = False
        self.tail = now

    def _on_birth_loading(self, e: dict[str, Any], now: float) -> None:
        self.model = str(e.get("model", ""))
        self.quant = str(e.get("quant", ""))
        self.mode = "loading"

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
        if self.thoughts and self.thoughts[-1].turn == turn:
            return self.thoughts[-1]
        for th in self.thoughts:
            if th.turn == turn:
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
        hesitate = max(0, int(e.get("hesitate_before_ms", 0) or 0)) / 1000
        vw.start = max(now, self.tail) + hesitate
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
        for item in e.get("items") or []:
            turn = int(item.get("turn", -1))
            upto = item.get("upto_i")
            for th in self.thoughts:
                if th.turn != turn:
                    continue
                for w in th.words:
                    if w.forgotten_at is None and (
                        item.get("all") or (upto is not None and w.i <= int(upto))
                    ):
                        w.forgotten_at = now

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

    def _on_death_shown(self, e: dict[str, Any], now: float) -> None:
        self.death.update({k: v for k, v in e.items() if k in ("last_line", "words_total")})
        self.mode = "dead"
        self.death_shown_at = now

    def _on_silence(self, e: dict[str, Any], now: float) -> None:
        self.mode = "silence"
        if self.death_shown_at is None:
            self.death_shown_at = now
        style = e.get("style")
        if style:
            self.s.silence_style = str(style)

    def _on_exhibit(self, e: dict[str, Any], now: float) -> None:
        self.exhibit_open = bool(e.get("open", True))

    def _on_error(self, e: dict[str, Any], now: float) -> None:
        self.last_error = f"{e.get('where', '')}: {e.get('message', '')}"

    # -- time -----------------------------------------------------------------------------

    def catch_up(self, now: float) -> None:
        """Show every queued word at once (after a reconnect burst or a long backlog)."""
        for w in self.words():
            if w.tail > now:
                w.start = min(w.start, now)
                w.char_ms = (0,) * len(w.text)
                w.pause_after_ms = 0
        self.tail = now

    def _trim_history(self) -> None:
        total = sum(len(th.words) for th in self.thoughts)
        while len(self.thoughts) > 1 and total > self.s.max_words:
            total -= len(self.thoughts.pop(0).words)

    def words(self) -> Iterable[ViewWord]:
        for th in self.thoughts:
            yield from th.words

    def typing(self, now: float) -> bool:
        """A letter is being typed (as opposed to a pause between words or thoughts)."""
        return any(w.start <= now < w.end for w in self._recent())

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

    def life_t(self, now: float) -> float | None:
        if self.t_life is None:
            return None
        if self.mode in ("living", "reloading"):
            return self.t_life + (now - self.t_at)
        return self.t_life

    def cursor(self, now: float) -> Literal["hidden", "on", "off", "dim"]:
        """Solid while typing, blinking in pauses, dim in a reload, gone at death."""
        if self.mode in ("empty", "loading", "dying", "dead", "silence") or not self.exhibit_open:
            return "hidden"
        if self.mode == "reloading":
            return "dim"
        if self.typing(now):
            return "on"
        phase = int(self.idle_since(now) / self.s.blink_s) % 2
        return "on" if phase == 0 else "off"

    # -- what to show ---------------------------------------------------------------------

    def card(self, now: float) -> tuple[CardKind, list[str]] | None:
        """The birth or death card to show instead of the text, if any."""
        if self.mode == "loading" and self.s.birth_card:
            return "birth", self._birth_lines(loading=True)
        if self.birth_at is not None and self.mode == "living" and self.s.birth_card:
            first = next(iter(self.words()), None)
            until = self.birth_at + self.s.birth_card_s
            if now < until and (first is None or first.start > now):
                return "birth", self._birth_lines(loading=False)
        if self.mode in ("dead", "silence") and self.death_shown_at is not None:
            start = max(self.death_shown_at, self.tail)
            keep = self.s.silence_style == "death_card" and self.mode == "silence"
            if start <= now and (keep or now < start + self.s.death_card_s):
                return "death", self._death_lines()
        return None

    def _birth_lines(self, loading: bool) -> list[str]:
        lines = [f"life {self.life}" if self.life else "a life"]
        if self.model:
            lines.append(self.model + (f" · {self.quant}" if self.quant else ""))
        lines.append("waking" if loading else "awake")
        return lines

    def _death_lines(self) -> list[str]:
        lines = [f"life {self.life}" if self.life else "a life"]
        lived = self.death.get("lived_s")
        if isinstance(lived, int | float):
            m, s = divmod(int(lived), 60)
            lines.append(f"lived {m}:{s:02d}")
        cause = str(self.death.get("cause", ""))
        if cause:
            lines.append(CAUSE_TEXT.get(cause, cause))
        return lines

    def dark(self, now: float) -> bool:
        """Nothing on screen: closed hours, or the silence in the dark style."""
        if not self.exhibit_open:
            return True
        if self.mode == "silence" and self.card(now) is None:
            if now < max(self.tail, self.death_shown_at or 0.0):
                return False  # the last words are still being typed
            return self.s.silence_style in ("dark", "idle", "death_card")
        return False

    def dimmed(self) -> bool:
        return self.mode == "reloading"

    def gauge(self) -> float | None:
        """Share of the memory budget in use: recall_used / recall (0..1)."""
        recall = self.vitals.get("recall")
        used = self.vitals.get("recall_used")
        if not isinstance(recall, int | float) or not isinstance(used, int | float) or recall <= 0:
            return None
        return max(0.0, min(1.0, float(used) / float(recall)))

    def bright_words(self, now: float) -> int:
        """Words typed and still live: what verify-life bounds late in a life."""
        return sum(
            1 for w in self.words() if w.start <= now and w.state_at(now, self.s.fade_s) == "live"
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
            parts.append("reconnecting")
        return " · ".join(parts)

    def snapshot(self, now: float, **extra: Any) -> dict[str, Any]:
        """A `snapshot` event (BUILD_PLAN 6.3) reproducing this view without animation."""
        words = [
            {"turn": w.turn, "i": w.i, "text": w.text, "state": w.state_at(now, self.s.fade_s)}
            for w in self.words()
        ]
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
        if self.thoughts and not self.thoughts[-1].ended:
            snap["open_turn"] = self.thoughts[-1].turn
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
    col: int
    text: str
    kind: str  # live | fading | forgotten | inherited | gauge
    fade: float = 0.0
    full_len: int = 0


@dataclass(frozen=True)
class Cursor:
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
    dark: bool = False
    dim: bool = False
    gauge: float | None = None
    split_words: int = 0

    @property
    def bright_words(self) -> int:
        return sum(1 for s in self.spans if s.kind == "live")

    def text_rows(self) -> list[str]:
        """The frame as plain text rows (tests, OCR ground truth)."""
        grid = [[" "] * self.cols for _ in range(self.rows)]
        for s in self.spans:
            for k, ch in enumerate(s.text):
                if 0 <= s.row < self.rows and 0 <= s.col + k < self.cols:
                    grid[s.row][s.col + k] = ch
        return ["".join(r).rstrip() for r in grid]


@dataclass(frozen=True)
class Placed:
    line: int
    col: int
    word: ViewWord
    text: str  # the part placed on this line (the whole word unless it is longer than a line)
    split: bool = False
    offset: int = 0  # letters of the word placed on earlier lines


def flow_lines(
    thoughts: Iterable[list[tuple[ViewWord, str]]], cols: int, blank_between: bool = True
) -> tuple[list[Placed], int, tuple[int, int]]:
    """Wrap whole words into lines of `cols`. Returns placements, line count and the
    position right after the last word (where the cursor rests).

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


def _started(
    view: LifeView,
    now: float,
    keep: set[str] | None = None,
    cols: int | None = None,
    rows: int = 0,
    mapper: Any = None,
    blank_between: bool = True,
) -> list[Thought]:
    """Thoughts with the words typed so far, oldest first.

    With `cols`, only the newest thoughts that can reach the screen are kept: every
    thought starts on its own line, so they can be wrapped one by one from the end, and
    a frame costs what is visible, not the whole history.
    """
    out: list[Thought] = []
    lines = 0
    for th in reversed(view.thoughts):
        words = [w for w in th.words if w.start <= now]
        if keep is not None:
            words = [w for w in words if w.state_at(now, view.s.fade_s) in keep]
        if not words:
            continue
        out.append(Thought(th.turn, words, th.ended))
        if cols is not None:
            texts = [(w, mapper(w.text) if mapper else w.text) for w in words]
            lines += flow_lines([texts], cols)[1] + (1 if blank_between and lines else 0)
            if lines > rows + 1:
                break
    out.reverse()
    return out


def _compose(
    view: LifeView,
    now: float,
    cols: int,
    rows: int,
    thoughts: list[Thought],
    mapper: Any,
    blank_between: bool,
    status: str | None,
    gauge: float | None,
    top: int = 0,
) -> Frame:
    frame = Frame(cols=cols, rows=rows + top, status=status, gauge=gauge)
    frame.dim = view.dimmed()
    frame.card = view.card(now)
    frame.dark = view.dark(now)
    if frame.dark or frame.card is not None:
        return frame
    seqs = [[(w, mapper(w.text)) for w in th.words] for th in thoughts]
    placed, nlines, (cl, cc) = flow_lines(seqs, cols, blank_between)
    cursor_mode = view.cursor(now)
    # the cursor rests after the last typed letter, or after the word in a pause
    last = placed[-1] if placed else None
    if last is not None:
        shown = last.word.shown(now)
        if shown < len(last.word.text):
            cc = last.col + max(0, min(len(last.text), shown - last.offset))
            cl = last.line
    if cc >= cols:
        cl, cc = cl + 1, 0
    total = max(nlines, cl + 1) if cursor_mode != "hidden" else nlines
    first = max(0, total - rows)
    offset_row = top + rows - min(rows, total)  # newest at the bottom
    split_ids: set[int] = set()
    for p in placed:
        if p.line < first:
            continue
        w = p.word
        shown = w.shown(now)
        visible = p.text[: max(0, min(len(p.text), shown - p.offset))]
        if not visible:
            continue
        state = w.state_at(now, view.s.fade_s)
        frame.spans.append(
            Span(
                p.line - first + offset_row,
                p.col,
                visible,
                state,
                w.fade_at(now, view.s.fade_s),
                len(p.text),
            )
        )
        if p.split:
            split_ids.add(id(w))
    frame.split_words = len(split_ids)
    if cursor_mode != "hidden" and cl >= first:
        frame.cursor = Cursor(cl - first + offset_row, cc, cursor_mode)
    return frame


def compose_flow(
    view: LifeView, now: float, cols: int, rows: int, status_strip: bool = True
) -> Frame:
    """Full-screen flow: newest text at the bottom, a blank line between thoughts, forgotten
    words fading through grey. `rows` counts text rows only; the status strip travels in
    `Frame.status` and each driver draws it in its own place (smaller, above the text)."""
    status = view.status_line(now) if status_strip else None
    return _compose(
        view,
        now,
        cols,
        rows,
        _started(view, now, cols=cols, rows=rows),
        lambda s: s,
        True,
        status,
        view.gauge(),
    )


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
    thoughts = _started(
        view,
        now,
        {"live", "inherited"},
        cols,
        text_rows,
        lambda s: map_charset(s, charset),
        blank_between=False,
    )
    frame = _compose(
        view,
        now,
        cols,
        text_rows,
        thoughts,
        lambda s: map_charset(s, charset),
        False,
        None,
        view.gauge(),
    )
    if text_rows < rows and not frame.dark and frame.card is None:
        frame.rows = rows
        frame.spans.append(Span(rows - 1, 0, gauge_bar(frame.gauge, cols, charset), "gauge"))
    if frame.card is not None:
        kind, lines = frame.card
        frame.card = (kind, [map_charset(x, charset) for x in lines])
    return frame


def fit_status(text: str, max_chars: int) -> str:
    """Drop whole ` · ` parts from the end until the strip fits; never cut inside a part."""
    if len(text) <= max_chars:
        return text
    parts = text.split(" · ")
    while len(parts) > 1 and len(" · ".join(parts)) > max_chars:
        parts.pop()
    return " · ".join(parts)[:max_chars]


def gauge_bar(fraction: float | None, cols: int, charset: Charset | str = "unicode") -> str:
    full, empty = ("█", "░") if charset == "unicode" else ("#", "-")
    n = round((fraction or 0.0) * cols)
    return full * n + empty * (cols - n)
