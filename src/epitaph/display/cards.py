"""Birth and death cards, and the silence between lives (BUILD_PLAN 5.12, 9 D7).

Pure Python, no I/O and no real time, like `layout`. A card replaces the text: the birth
card while the model loads and until its first word, the death card after the last word
has been typed and has faded. Cards are typed letter by letter with the reveal rhythm
(a letter interval, the word gap between words, the comma pause between lines), so they
read like the life itself. The death card is typed at the life's last cadence.

- The birth card names the life only when `[life] reveal_life_number` is on, and the
  model only when `[display] birth_card_model` is on; otherwise it says only "waking".
- The death card says how long it lived and how it died.
- Silence styles (`[display] silence_style`): `dark` (default; the screen goes black after
  the death card), `death_card` (the card stays for the whole silence), `last_words` (the
  last words stay, unfaded) and `idle` (dark, with one dim mark wandering slowly over the
  screen: the piece is waiting, not dead).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Literal

CardKind = Literal["birth", "death"]

SILENCE_STYLES = ("dark", "death_card", "last_words", "idle")

CAUSE_TEXT = {
    "oom": "its memory was taken",
    "deadline": "its time ran out",
    "full": "its memory filled",
    "crash": "it broke",
    "hang": "it stopped",
    "manual": "it was ended by hand",
    "interrupted": "it was interrupted",
}

IDLE_MARK = "·"


@dataclass(frozen=True)
class CardStyle:
    """What a card shows and how fast it is typed (milliseconds)."""

    reveal_life_number: bool = False
    show_model: bool = True
    char_ms: int = 165
    word_gap_ms: int = 270
    line_pause_ms: int = 750


def birth_lines(life: int, model: str, quant: str, style: CardStyle) -> list[str]:
    """The birth card: [life N], [model · quant], then "waking"."""
    lines: list[str] = []
    if style.reveal_life_number and life:
        lines.append(f"life {life}")
    if style.show_model and model:
        lines.append(model + (f" · {quant}" if quant else ""))
    lines.append("waking")
    return lines


def death_lines(life: int, lived_s: float | None, cause: str, style: CardStyle) -> list[str]:
    """The death card: [life N], "lived m:ss", then the cause in words."""
    lines: list[str] = []
    if style.reveal_life_number and life:
        lines.append(f"life {life}")
    if lived_s is not None:
        m, s = divmod(int(lived_s), 60)
        lines.append(f"lived {m}:{s:02d}")
    if cause:
        lines.append(CAUSE_TEXT.get(cause, cause))
    return lines or ["ended"]


@dataclass(frozen=True)
class Card:
    """A card typed letter by letter from `start` (display seconds).

    `times[n]` is when letter n of `flat` appears; `flat` is the lines joined by newlines,
    and a newline or a space appears with the letter after it.
    """

    kind: CardKind
    lines: tuple[str, ...]
    start: float
    times: tuple[float, ...]

    @property
    def flat(self) -> str:
        """The lines joined by newlines: what `typed` counts in."""
        return "\n".join(self.lines)

    @property
    def end(self) -> float:
        """When the last letter has appeared."""
        return self.times[-1] if self.times else self.start

    def typed(self, now: float) -> int:
        """Letters of `flat` visible at `now` (newlines and spaces count as letters)."""
        if now < self.start:
            return 0
        lo, hi = 0, len(self.times)
        while lo < hi:  # the first letter still to come
            mid = (lo + hi) // 2
            if self.times[mid] <= now:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def shown(self, now: float) -> list[int]:
        """Letters visible per line at `now`."""
        return shown_per_piece(_offsets(self.lines), self.typed(now))

    def next_change(self, now: float) -> float:
        """When the next letter appears after `now`; `math.inf` once typed."""
        k = self.typed(now)
        return self.times[k] if k < len(self.times) else math.inf


def _offsets(lines: tuple[str, ...] | list[str]) -> list[tuple[str, int]]:
    """Each line with its offset in the newline-joined text."""
    out: list[tuple[str, int]] = []
    off = 0
    for line in lines:
        out.append((line, off))
        off += len(line) + 1
    return out


def type_card(
    kind: CardKind, lines: list[str], start: float, style: CardStyle, char_ms: int | None = None
) -> Card:
    """Schedule `lines` letter by letter from `start` with the reveal rhythm of `style`.

    `char_ms` overrides the letter interval (the death card uses the life's last one).
    """
    ms = max(0, style.char_ms if char_ms is None else char_ms) / 1000
    gap = max(0, style.word_gap_ms) / 1000
    pause = max(0, style.line_pause_ms) / 1000
    times: list[float] = []
    t = start
    for n, line in enumerate(lines):
        if n:
            times.append(t)  # the newline: a pause, then the next line's first letter
            t += pause
        for ch in line:
            times.append(t)
            t += gap if ch == " " else ms
    return Card(kind, tuple(lines), start, tuple(times))


def wrap_card(lines: tuple[str, ...] | list[str], cols: int) -> list[tuple[str, int]]:
    """Wrap card lines to `cols` characters at spaces, keeping each piece's offset in the
    newline-joined text so the typing count still maps onto it. A word longer than `cols`
    is cut (a model name on a 16-column grid)."""
    cols = max(1, cols)
    out: list[tuple[str, int]] = []
    for line, off in _offsets(lines):
        pos = 0
        while True:
            rest = line[pos:]
            if len(rest) <= cols:
                out.append((rest, off + pos))
                break
            cut = rest.rfind(" ", 0, cols + 1)
            if cut <= 0:
                out.append((rest[:cols], off + pos))
                pos += cols
            else:
                out.append((rest[:cut], off + pos))
                pos += cut + 1
    return out


def shown_per_piece(pieces: list[tuple[str, int]], typed: int) -> list[int]:
    """Letters visible in each (text, offset) piece when `typed` letters have appeared."""
    return [max(0, min(len(text), typed - off)) for text, off in pieces]


def card_char_ms(recent: list[tuple[int, ...]], floor: int) -> int:
    """The death card's letter interval: the median of the life's last letters, at least
    `floor` (the card is never typed faster than the birth card)."""
    vals = sorted(ms for word in recent for ms in word if ms > 0)
    if not vals:
        return floor
    return max(floor, vals[len(vals) // 2])


def idle_position(t: float, rows: int, cols: int, step_s: float, seed: int = 0) -> tuple[int, int]:
    """Where the idle mark rests `t` seconds into the silence: a new cell every `step_s`
    seconds, chosen by a hash of the step number (deterministic, so tests and replays
    agree)."""
    step = int(max(0.0, t) // max(0.1, step_s))
    h = hashlib.blake2b(f"{seed}:{step}".encode(), digest_size=8).digest()
    n = int.from_bytes(h, "big")
    return n % max(1, rows), (n // max(1, rows)) % max(1, cols)
