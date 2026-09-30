"""The output pipeline and its rhythm (BUILD_PLAN 5.7, 5.12).

Per thought: raw chunks -> `Sanitizer` -> `WordSegmenter` -> `Lookahead` (holds only what
could still become a banned phrase) -> the pacing queue -> `drain`, which releases one word
at a time with its letter cadence.

`speak()` runs one whole thought (generation and typing overlapping, regeneration on a
banned opening, the death flush) and enforces the sync rule: it returns only when the last
word has finished typing, so the next request cannot start earlier. The controller calls it
once per thought.

Timing is in the life clock's seconds; cadence values are integer milliseconds.
"""

from __future__ import annotations

import asyncio
import random
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from epitaph.backend.base import CreatureDied
from epitaph.clock import LifeClock
from epitaph.config import Config
from epitaph.mind.sanitize import Sanitizer
from epitaph.mind.words import WordSegmenter, normalize, split_words
from epitaph.types import Chunk, CreatureStatus, Knobs, TimedWord, Word

__all__ = [
    "BannedHit",
    "Lookahead",
    "Pacer",
    "PushResult",
    "Spoken",
    "life_seed",
    "pause_after_ms",
    "speak",
]

_SENTENCE_END = ".?!…"
_CLAUSE_END = ",;:—–"
_CLOSERS = "\"'”’»)]}›"


@dataclass(frozen=True)
class BannedHit:
    """A banned phrase found in a thought (BUILD_PLAN 5.7 step 4)."""

    phrase: str
    i: int  # index of the phrase's first word within the thought
    at_start: bool
    regenerate: bool  # the controller should discard the thought and ask again


@dataclass(frozen=True)
class PushResult:
    """What one pushed chunk did."""

    released: tuple[Word, ...] = ()
    held: int = 0
    stop: bool = False  # stop generating: a cut at a banned phrase or at [host]
    regenerate: bool = False  # discard this attempt and start a new request
    hit: BannedHit | None = None


def life_seed(base: int, life: int) -> int:
    """The per-life seed: `life.seed` when set, else derived from the life number."""
    return base * 1_000_003 + life if base else life


def pause_after_ms(text: str, word_gap: int = 90, comma: int = 250, sentence: int = 700) -> int:
    """The gap after a word: longer after a clause or a sentence (closing quotes ignored)."""
    core = text.rstrip(_CLOSERS)
    if not core:
        return word_gap
    if core[-1] in _SENTENCE_END:
        return sentence
    if core[-1] in _CLAUSE_END:
        return comma
    return word_gap


# ---------------------------------------------------------------------------------------
# prefix-aware lookahead


class Lookahead:
    """Holds a word only while the words from it to the newest could still begin a banned
    phrase. Matching is on normalized words (lower case, no punctuation). `cap` bounds the
    held words."""

    def __init__(self, phrases: Sequence[str], cap: int = 8) -> None:
        seen: dict[tuple[str, ...], str] = {}
        for p in phrases:
            norm = tuple(n for n in (normalize(w) for w in split_words(p)) if n)
            if norm:
                seen.setdefault(norm, p)
        self.phrases = seen
        self.cap = max(1, cap)
        self.held: list[str] = []

    def reset(self) -> None:
        self.held = []

    def push(self, word: str) -> tuple[list[str], int | None, str | None]:
        """Add a word. Returns (released words, index in `held` where a complete banned phrase
        starts or None, the phrase). On a hit nothing is released; the caller decides."""
        self.held.append(word)
        norms = [normalize(w) for w in self.held]
        live: int | None = None
        for j in range(len(self.held)):
            if not norms[j]:
                continue
            seq = tuple(n for n in norms[j:] if n)
            for p, original in self.phrases.items():
                if len(seq) >= len(p) and seq[: len(p)] == p:
                    return [], j, original
                if live is None and p[: len(seq)] == seq:
                    live = j
        cut = len(self.held) if live is None else live
        cut = max(cut, len(self.held) - self.cap)
        out, self.held = self.held[:cut], self.held[cut:]
        return out, None, None

    def flush(self) -> list[str]:
        """Release everything held: an unresolved prefix at the end is not a banned phrase."""
        out, self.held = self.held, []
        return out


# ---------------------------------------------------------------------------------------
# the pacer


@dataclass
class PacerStats:
    """What the drain saw, for tests and verify-life."""

    words: int = 0
    starved_s: float = 0.0  # time waiting for a word after the first word of a thought
    stalls: list[float] = field(default_factory=lambda: [])
    divergences: int = 0


class Pacer:
    """One life's pipeline from raw chunks to timed words."""

    def __init__(
        self,
        clock: LifeClock,
        *,
        banned: Sequence[str] = (),
        lookahead_words: int = 8,
        max_regenerations: int = 2,
        seed: int = 0,
        adaptive: bool = True,
        rate_margin: float = 0.88,
        rate_window_s: float = 60.0,
        min_rate_sample_s: float = 3.0,
        word_gap_ms: int = 90,
        comma_pause_ms: int = 250,
        sentence_pause_ms: int = 700,
        hesitation_ms: tuple[int, int] = (400, 1200),
        hesitation_inside_from: float = 0.1,
    ) -> None:
        self.clock = clock
        self.rng = random.Random(seed)
        self.max_regenerations = max_regenerations
        self.adaptive = adaptive
        self.rate_margin = rate_margin
        self.rate_window_s = rate_window_s
        self.min_rate_sample_s = min_rate_sample_s
        self.word_gap_ms = word_gap_ms
        self.comma_pause_ms = comma_pause_ms
        self.sentence_pause_ms = sentence_pause_ms
        self.hesitation_ms = hesitation_ms
        self.hesitation_inside_from = hesitation_inside_from
        self.lookahead = Lookahead(banned, lookahead_words)
        self.stats = PacerStats()

        self._estimate: float | None = None
        self._samples: deque[tuple[float, int]] = deque()
        self._sample_s = 0.0
        self._last_chunk_t: float | None = None

        self.turn = 0
        self.regenerations = 0
        self.shown: list[Word] = []  # words released to the queue in this thought
        self._queue: deque[Word] = deque()
        self._wake = asyncio.Event()
        self._finished = True
        self._stopped = False
        self._hit: BannedHit | None = None
        self._sanitizer = Sanitizer()
        self._segmenter = WordSegmenter()
        self._not_before = 0.0
        self._last_end: float | None = None

    @classmethod
    def from_config(cls, cfg: Config, clock: LifeClock, seed: int = 0) -> Pacer:
        out = cfg.section("output")
        rev = cfg.section("reveal")
        hes = list(rev.get("hesitation_ms", [400, 1200]))
        return cls(
            clock,
            banned=[str(p) for p in cfg.get("prompt.banned_phrases", [])],
            lookahead_words=int(out.get("lookahead_words", 8)),
            max_regenerations=int(out.get("max_regenerations", 2)),
            seed=seed,
            adaptive=bool(rev.get("adaptive", True)),
            rate_margin=float(rev.get("rate_margin", 0.88)),
            rate_window_s=float(rev.get("rate_window_s", 60)),
            min_rate_sample_s=float(rev.get("min_rate_sample_s", 3.0)),
            word_gap_ms=int(rev.get("word_gap_ms", 90)),
            comma_pause_ms=int(rev.get("comma_pause_ms", 250)),
            sentence_pause_ms=int(rev.get("sentence_pause_ms", 700)),
            hesitation_ms=(int(hes[0]), int(hes[1])),
            hesitation_inside_from=float(rev.get("hesitation_inside_from", 0.1)),
        )

    # -- the generation rate -------------------------------------------------------------

    def set_rate_estimate(self, letters_per_s: float) -> None:
        """Use this rate (from the bench) until enough new generation has been measured.
        Called at birth and after each reload; it forgets the old measurements."""
        self._estimate = letters_per_s if letters_per_s > 0 else None
        self._samples.clear()
        self._sample_s = 0.0

    def rate(self) -> float | None:
        """Smoothed generation rate in letters per second (BUILD_PLAN 5.12)."""
        if self._sample_s >= self.min_rate_sample_s:
            letters = sum(n for _, n in self._samples)
            return letters / self._sample_s if letters > 0 else self._estimate
        return self._estimate

    def begin_request(self) -> None:
        """A new backend request starts: the wait for its first chunk is not generation."""
        self._last_chunk_t = None

    def _observe(self, chunk: str) -> None:
        now = self.clock.elapsed()
        letters = sum(1 for c in chunk if not c.isspace())
        if self._last_chunk_t is not None:
            dt = max(0.0, now - self._last_chunk_t)
            self._samples.append((dt, letters))
            self._sample_s += dt
            while self._samples and self._sample_s - self._samples[0][0] >= self.rate_window_s:
                self._sample_s -= self._samples.popleft()[0]
        self._last_chunk_t = now

    # -- one thought -----------------------------------------------------------------------

    def begin_thought(self, turn: int, pause_s: float = 0.0) -> None:
        """Start a thought. Its first word is shown no sooner than `pause_s` after the last
        word of the previous thought finished (the pause overlaps prompt processing)."""
        self.turn = turn
        self.regenerations = 0
        self.shown = []
        self._queue.clear()
        self._finished = False
        self._hit = None
        self._reset_attempt()
        self._not_before = 0.0 if self._last_end is None else self._last_end + pause_s
        self.begin_request()

    def _reset_attempt(self) -> None:
        self.stats.divergences += self._sanitizer.divergences
        self._sanitizer = Sanitizer()
        self._segmenter = WordSegmenter()
        self.lookahead.reset()
        self._stopped = False

    @property
    def text(self) -> str:
        """The thought as released so far (what memory keeps)."""
        return " ".join(w.text for w in self.shown)

    @property
    def cut_at_host(self) -> bool:
        return self._sanitizer.cut

    def push(self, chunk: str) -> PushResult:
        """Feed a raw chunk from the backend."""
        if self._stopped or self._finished:
            return PushResult(stop=True, hit=self._hit)
        self._observe(chunk)
        clean = self._sanitizer.feed(chunk)
        released: list[Word] = []
        for w in self._segmenter.feed(clean):
            res = self._add_word(w, released, dead=False)
            if res is not None:
                return res
        return PushResult(
            released=tuple(released), held=len(self.lookahead.held), stop=self._sanitizer.cut
        )

    def _add_word(self, w: str, released: list[Word], dead: bool) -> PushResult | None:
        out, j, phrase = self.lookahead.push(w)
        for text in out:
            released.append(self._release(text))
        if j is None or phrase is None:
            return None
        i = len(self.shown) + j
        at_start = i == 0
        regen = at_start and not dead and self.regenerations < self.max_regenerations
        hit = BannedHit(phrase=phrase, i=i, at_start=at_start, regenerate=regen)
        self._hit = hit
        if regen:
            self.regenerations += 1
            self._reset_attempt()
            self.begin_request()
            self._hit = None  # only a hit that cuts the final thought is kept
            return PushResult(released=tuple(released), regenerate=True, hit=hit)
        for text in self.lookahead.held[:j]:
            released.append(self._release(text))
        self.lookahead.reset()
        self._stopped = True
        return PushResult(released=tuple(released), stop=True, hit=hit)

    def _release(self, text: str) -> Word:
        word = Word(self.turn, len(self.shown), text)
        self.shown.append(word)
        self._queue.append(word)
        self._wake.set()
        return word

    def finish_thought(self, dead: bool = False) -> BannedHit | None:
        """End of generation (normal, a cut, or death): finalize the last word, cut a
        complete banned phrase, release an unfinished prefix and queue the rest. Returns the
        banned phrase that cut the thought, if any. When the hit asks to regenerate, the
        thought is not finished: start a new request and push again."""
        if self._finished:
            return self._hit
        if not self._stopped:
            released: list[Word] = []
            words = self._segmenter.feed(self._sanitizer.finish()) + self._segmenter.finish()
            for w in words:
                res = self._add_word(w, released, dead=dead)
                if res is not None:
                    if res.regenerate:
                        return res.hit
                    break
            if not self._stopped:
                for text in self.lookahead.flush():
                    self._release(text)
        self.stats.divergences += self._sanitizer.divergences
        self._sanitizer.divergences = 0
        self._finished = True
        self._wake.set()
        return self._hit

    # -- cadence ---------------------------------------------------------------------------

    def letter_interval_ms(self, knobs: Knobs) -> float:
        """interval = max(profile floor, 1 / (margin x rate)) (BUILD_PLAN 5.12)."""
        floor = knobs.letter_ms
        r = self.rate() if self.adaptive else None
        if r is None or r <= 0:
            return floor
        return max(floor, 1000.0 / (self.rate_margin * r))

    def cadence(self, word: Word, knobs: Knobs) -> TimedWord:
        """Letter timings for one word: one entry per character of its text."""
        interval = self.letter_interval_ms(knobs)
        char_ms = [
            max(1, round(interval * (1 + knobs.jitter * (2 * self.rng.random() - 1))))
            for _ in word.text
        ]
        pause = pause_after_ms(
            word.text, self.word_gap_ms, self.comma_pause_ms, self.sentence_pause_ms
        )
        before = 0
        if knobs.hesitation > 0 and self.rng.random() < knobs.hesitation:
            h = self.rng.randint(*self.hesitation_ms)
            inside = (
                knobs.hesitation >= self.hesitation_inside_from
                and len(char_ms) >= 4
                and self.rng.random() < 0.5
            )
            if inside:
                char_ms[self.rng.randrange(1, len(char_ms) - 1)] += h
            else:
                before = h
        return TimedWord(word, tuple(char_ms), pause, before)

    async def drain(self, knobs: Knobs) -> AsyncIterator[TimedWord]:
        """Release the thought's words one at a time. Each word is yielded when it starts
        typing; the next comes after its letters and its pause. Ends when the thought is
        finished and the last word has been typed."""
        first = True
        while True:
            waited_from: float | None = None
            while not self._queue and not self._finished:
                if waited_from is None:
                    waited_from = self.clock.elapsed()
                self._wake.clear()
                await self._wake.wait()
            if not self._queue:
                break
            if waited_from is not None and not first:
                stall = self.clock.elapsed() - waited_from
                self.stats.starved_s += stall
                if stall > 0:
                    self.stats.stalls.append(stall)
            word = self._queue.popleft()
            tw = self.cadence(word, knobs)
            wait = tw.hesitate_before_ms / 1000
            if first:
                wait = max(wait, self._not_before - self.clock.elapsed())
            await self.clock.sleep(wait)
            first = False
            self.stats.words += 1
            yield tw
            await self.clock.sleep((sum(tw.char_ms) + tw.pause_after_ms) / 1000)
            self._last_end = self.clock.elapsed()
        if self._last_end is None:
            self._last_end = self.clock.elapsed()


# ---------------------------------------------------------------------------------------
# one thought, end to end


class Emit(Protocol):
    def __call__(self, etype: str, /, **fields: Any) -> None: ...


@dataclass
class Spoken:
    """The outcome of one thought."""

    turn: int
    words: list[Word]
    tokens: int = 0
    prompt_n: int | None = None
    requests: int = 0
    regenerations: int = 0
    hit: BannedHit | None = None
    cut_at_host: bool = False
    died: CreatureStatus | None = None

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


async def _close(stream: AsyncIterator[Chunk]) -> None:
    if isinstance(stream, AsyncGenerator):
        await stream.aclose()


async def speak(
    pacer: Pacer,
    stream: Callable[[], AsyncIterator[Chunk]],
    knobs: Knobs,
    turn: int,
    emit: Emit,
    on_died: Callable[[CreatureStatus], None] | None = None,
) -> Spoken:
    """Generate and show one thought (BUILD_PLAN 5.7).

    Emits `gen_start` and `gen_end` per request and `word` per shown word. Regenerates on a
    banned opening, stops the request at a cut, and on `CreatureDied` calls `on_died` at
    once (the real moment of death), then shows the words already generated at the current
    pace. Returns after the last word has finished typing (the sync rule).
    """
    pacer.begin_thought(turn, knobs.pause_s)
    result = Spoken(turn, [])

    async def show() -> None:
        async for tw in pacer.drain(knobs):
            emit(
                "word",
                turn=turn,
                i=tw.word.i,
                text=tw.word.text,
                char_ms=list(tw.char_ms),
                pause_after_ms=tw.pause_after_ms,
            )

    shower = asyncio.create_task(show())
    try:
        while True:
            result.requests += 1
            pacer.begin_request()
            emit("gen_start", turn=turn)
            chunks = stream()
            regenerate = False
            tokens = 0
            ended = False
            try:
                async for chunk in chunks:
                    if chunk.done:
                        tokens = chunk.predicted_n if chunk.predicted_n is not None else tokens
                        result.prompt_n = chunk.prompt_n
                        emit(
                            "gen_end",
                            turn=turn,
                            prompt_n=chunk.prompt_n,
                            tokens=tokens,
                            tok_s=chunk.predicted_per_s,
                        )
                        ended = True
                        break
                    tokens += 1
                    result.tokens = tokens
                    r = pacer.push(chunk.text)
                    if r.regenerate or r.stop:
                        regenerate = r.regenerate
                        break
            finally:
                await _close(chunks)
            if not ended:
                emit("gen_end", turn=turn, prompt_n=None, tokens=tokens, tok_s=None)
            result.tokens = tokens
            if regenerate:
                continue
            hit = pacer.finish_thought(dead=False)
            if hit is not None and hit.regenerate:
                continue
            break
    except CreatureDied as e:
        result.died = e.status
        emit("gen_end", turn=turn, prompt_n=None, tokens=result.tokens, tok_s=None)
        if on_died is not None:
            on_died(e.status)
        pacer.finish_thought(dead=True)
    except BaseException:
        shower.cancel()
        raise
    await shower
    result.words = list(pacer.shown)
    result.regenerations = pacer.regenerations
    result.hit = pacer.finish_thought(dead=result.died is not None)
    result.cut_at_host = pacer.cut_at_host
    return result
