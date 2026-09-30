"""The mind's memory: past turns held as text by the controller (BUILD_PLAN 5.4).

- **Recall** is the token budget for past turns only: every reading and thought already
  finished, plus the memory-gap marker once something is forgotten. It excludes the system
  prompt and the current reading.
- **The recall rule** (`fit`): when the past exceeds recall, it is trimmed down to
  `recall x trim_to` (hysteresis, so trims come every several thoughts, not every thought).
- **Order of loss:** oldest turns first; then, inside the oldest kept turn, its reading and
  then the first words of its thought. The current reading and the thought being written are
  never in the past, so they are never trimmed.
- **Reload cuts** (`cut_for_reload`) are the same rule applied during the reload silence;
  the first reading after it reports everything at once (`take_forgotten`).
- **The memory gap** is visible: once anything is forgotten, the fixed marker reading
  (`[host] earlier memory lost`) goes in front of the next reading, in the same user
  message, and belongs to that reading. When a later trim takes that reading, the marker
  goes with it and comes back in front of the next new reading. From the first loss on, the
  marker is in the model's context (except between a trim and the next reading).

**The server's cache decides two of these rules.** llama-server's `--cache-reuse` scan only
moves forward in the new prompt on a match, so any new text in front of turns it has already
read makes everything after it a re-read: spike S2f measured 80% of the prompt for a marker
inserted in front of the kept turns, about two minutes of silence on the Pi 4. So:

- the marker never appears in front of kept turns: it rides on a new reading (contract
  decision A3), which costs only its own tokens;
- a live trim (`fit`) cuts on turn boundaries: whole turns, or only the reading of the
  oldest kept turn. It cuts words only inside the last remaining turn, where the re-read is
  one short turn. The price is a trim that may go up to one turn below `recall x trim_to`.
  A reload cut goes to the word, since the new server reads everything anyway.

Messages alternate user/assistant as chat templates require: the marker and the reading it
belongs to, and readings around an empty thought, share one user message.

`fit` returns `forget` event items: `{"turn": n, "all": true}` for a whole thought, and
`{"turn": n, "upto_i": k}` when the words with index 0..k (inclusive) of thought n are gone.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from epitaph.types import Msg

__all__ = ["Forgetting", "Memory", "approx_tokens"]

TokenCounter = Callable[[str], int]


def approx_tokens(text: str) -> int:
    """About four characters per token, plus the chat template's per-message overhead."""
    return math.ceil(len(text) / 4) + 4 if text else 0


@dataclass
class _Turn:
    turn: int
    reading: str | None
    reading_tokens: int
    words: list[str] = field(default_factory=lambda: [])
    thought_tokens: int = 0
    first_i: int = 0  # index, in the shown thought, of words[0]
    touched: bool = False  # lost something already (counted once in `forgotten`)

    @property
    def tokens(self) -> int:
        return self.reading_tokens + self.thought_tokens


@dataclass
class Forgetting:
    """What one trim took."""

    items: list[dict[str, Any]] = field(default_factory=lambda: [])
    thoughts: int = 0  # thoughts that lost something
    tokens_before: int = 0
    tokens_after: int = 0
    marker_added: bool = False

    def __bool__(self) -> bool:
        """True when the trim actually freed tokens."""
        return self.tokens_after < self.tokens_before


class Memory:
    """Past turns, the system prompt, and the reading being answered."""

    def __init__(
        self,
        counter: TokenCounter = approx_tokens,
        marker: str = "[host] earlier memory lost",
        system: str = "",
    ) -> None:
        """Count tokens with counter; marker is the reading that stands in for lost memory."""
        self.count = counter
        self.marker = marker
        self.marker_tokens = counter(marker)
        self.system = system
        self.turns: list[_Turn] = []
        self.gap = False
        self.gap_turn: int | None = None  # the turn whose reading carries the marker
        self._pending: _Turn | None = None
        self._forgotten = 0
        self.forgotten_total = 0

    # -- the system prompt -----------------------------------------------------------------

    def set_system(self, text: str) -> bool:
        """Replace the system prompt; True when it changed (an erosion step)."""
        changed = text != self.system
        self.system = text
        return changed

    @property
    def system_tokens(self) -> int:
        """Tokens of the current system prompt."""
        return self.count(self.system) if self.system else 0

    # -- appending -------------------------------------------------------------------------

    def append_host(self, reading: str, turn: int, tokens: int | None = None) -> None:
        """The reading for the thought about to be written. Not part of the past (and never
        trimmed) until its thought is appended."""
        if self._pending is not None:
            raise RuntimeError("a reading is already waiting for its thought")
        self._pending = _Turn(turn, reading, self.count(reading) if tokens is None else tokens)
        if self.gap and self.gap_turn is None:
            self.gap_turn = turn  # this reading carries the marker

    def append_thought(self, words: Sequence[str], tokens: int | None = None) -> None:
        """The finished thought, as the words actually shown. Completes the turn."""
        if self._pending is None:
            raise RuntimeError("append_host must come before append_thought")
        t = self._pending
        t.words = list(words)
        text = " ".join(t.words)
        t.thought_tokens = (self.count(text) if tokens is None else tokens) if text else 0
        self.turns.append(t)
        self._pending = None

    def discard_pending(self) -> None:
        """Drop a reading whose thought never came (death before any word)."""
        self._pending = None

    # -- sizes -----------------------------------------------------------------------------

    def used(self) -> int:
        """Tokens of the past: turns plus the marker. This is what recall bounds."""
        return sum(t.tokens for t in self.turns) + (self.marker_tokens if self.gap else 0)

    @property
    def pending_tokens(self) -> int:
        """Tokens of the reading waiting for its thought; 0 if there is none."""
        return self._pending.reading_tokens if self._pending is not None else 0

    def prompt_tokens(self) -> int:
        """Everything sent with the next request: system, past and the current reading."""
        return self.system_tokens + self.used() + self.pending_tokens

    def fits(self, ctx: int, max_tokens: int, reading_tokens: int | None = None) -> bool:
        """Whether the next request fits in the context (`unbounded` dies when it does not).
        Uses the pending reading, or `reading_tokens` for one not yet appended."""
        reading = self.pending_tokens if reading_tokens is None else reading_tokens
        return self.system_tokens + self.used() + reading + max_tokens <= ctx

    # -- forgetting ------------------------------------------------------------------------

    def fit(self, recall: int, trim_to: float = 1.0) -> Forgetting:
        """The recall rule: if the past exceeds `recall`, trim it to `recall x trim_to`.

        The cut falls on a turn boundary (see the module notes), so it may go up to one turn
        deeper than the target; words are cut only inside the last remaining turn.
        """
        before = self.used()
        if before <= recall:
            return Forgetting(tokens_before=before, tokens_after=before)
        return self._trim_to(_target(recall, trim_to), before, whole_turns=True)

    def cut_for_reload(self, recall: int, trim_to: float = 1.0) -> Forgetting:
        """The reload is also a memory loss: cut to the post-reload recall during the silence.

        The cut goes to `recall x trim_to` (it delays the next trim) and removes whole turns.
        With the slot hand-over (ADR-014) the new server starts from the old one's cache, and a
        cut that ends inside a turn would force it to re-read almost everything after the cut
        (a 4-5 minute silence for a 4B model on the Pi 4, seen in rehearsal); whole turns are
        absorbed by cache reuse.
        """
        before = self.used()
        if before <= recall:
            return Forgetting(tokens_before=before, tokens_after=before)
        return self._trim_to(_target(recall, trim_to), before, whole_turns=True)

    def _trim_to(self, target: int, before: int, whole_turns: bool) -> Forgetting:
        res = Forgetting(tokens_before=before)
        if not self.gap:
            self.gap = True
            res.marker_added = True
            if self._pending is not None:
                self.gap_turn = self._pending.turn
        while self.turns and self.used() > target:
            oldest = self.turns[0]
            if self.used() - oldest.tokens >= target or (
                whole_turns and len(self.turns) > 1 and not self._reading_is_enough(target)
            ):
                self.turns.pop(0)
                if oldest.words:  # an empty thought had nothing on screen to forget
                    self._note(res, oldest)
                    res.items.append({"turn": oldest.turn, "all": True})
                continue
            # Only a turn with words gets here: an empty one is all reading, so it went whole.
            self._trim_inside(oldest, target, res)
            break
        if not self._marker_reading_kept():
            # The reading that carried the marker is gone: the marker moves to the next one.
            self.gap_turn = self._pending.turn if self._pending is not None else None
        res.tokens_after = self.used()
        return res

    def _reading_is_enough(self, target: int) -> bool:
        """Whether dropping only the oldest kept turn's reading reaches the target."""
        oldest = self.turns[0]
        return oldest.reading is not None and self.used() - oldest.reading_tokens <= target

    def _marker_reading_kept(self) -> bool:
        """Whether the reading that carries the marker is still remembered (or pending)."""
        if self.gap_turn is None:
            return True
        if self._pending is not None and self._pending.turn == self.gap_turn:
            return True
        return any(t.turn == self.gap_turn and t.reading is not None for t in self.turns)

    def _trim_inside(self, t: _Turn, target: int, res: Forgetting) -> None:
        """Trim the oldest kept turn: its reading first, then its first words."""
        if t.reading is not None:
            t.reading, t.reading_tokens = None, 0
            if self.used() <= target:
                return
        full_text = " ".join(t.words)
        full_tokens = t.thought_tokens
        full_est = self.count(full_text) or 1
        dropped = 0
        while t.words and self.used() > target:
            t.words.pop(0)
            dropped += 1
            text = " ".join(t.words)
            t.thought_tokens = round(full_tokens * self.count(text) / full_est) if text else 0
        if dropped:
            self._note(res, t)
            if t.words:
                res.items.append({"turn": t.turn, "upto_i": t.first_i + dropped - 1})
                t.first_i += dropped
            else:
                self.turns.remove(t)
                res.items.append({"turn": t.turn, "all": True})

    def _note(self, res: Forgetting, t: _Turn) -> None:
        if not t.touched:
            t.touched = True
            res.thoughts += 1
            self._forgotten += 1
            self.forgotten_total += 1

    def take_forgotten(self) -> int:
        """Thoughts that lost something since the last call; the next reading reports them."""
        n, self._forgotten = self._forgotten, 0
        return n

    # -- rendering -------------------------------------------------------------------------

    def past_messages(self) -> list[Msg]:
        """The past as chat messages, in order.

        Once anything is lost, the marker stands in front of the reading of turn `gap_turn`.
        When that reading is not in the past (it is the pending one, or not written yet),
        the marker comes last, so `messages` joins it to the front of the next reading.
        """
        out: list[Msg] = []
        placed = not self.gap
        for t in self.turns:
            if not placed and t.turn == self.gap_turn:
                out.append(Msg("user", self.marker, kind="marker"))
                placed = True
            if t.reading is not None:
                out.append(Msg("user", t.reading, t.turn, "reading"))
            if t.words:
                out.append(Msg("assistant", " ".join(t.words), t.turn, "thought"))
        if not placed:
            out.append(Msg("user", self.marker, kind="marker"))
        return out

    def messages(self) -> list[Msg]:
        """What the next chat request sends: system, past, the current reading. Consecutive
        user messages (the marker and a reading, or readings around an empty thought) are
        joined so roles alternate."""
        raw: list[Msg] = []
        if self.system:
            raw.append(Msg("system", self.system, kind="persona"))
        raw += self.past_messages()
        if self._pending is not None and self._pending.reading is not None:
            raw.append(Msg("user", self._pending.reading, self._pending.turn, "reading"))
        return merge_consecutive(raw)


def _target(recall: int, trim_to: float) -> int:
    return math.floor(recall * min(1.0, max(0.0, trim_to)))


def merge_consecutive(msgs: list[Msg]) -> list[Msg]:
    """Join neighbouring messages of the same role with a newline."""
    out: list[Msg] = []
    for m in msgs:
        if out and out[-1].role == m.role and m.role != "system":
            prev = out[-1]
            kind = "reading" if "reading" in (prev.kind, m.kind) else prev.kind
            out[-1] = Msg(prev.role, f"{prev.content}\n{m.content}", max(prev.turn, m.turn), kind)
        else:
            out.append(m)
    return out
