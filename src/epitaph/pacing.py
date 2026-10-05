"""The output pipeline and its rhythm.

Per thought: raw chunks -> `Sanitizer` -> `WordSegmenter` -> `Lookahead` (holds only what
could still become a banned phrase) -> the pacing queue -> `drain`, which releases one word
at a time with its letter cadence.

`speak()` runs one whole thought (generation and typing overlapping, regeneration on a
banned opening, the death flush) and enforces the sync rule: it returns only when the last
word has finished typing, so the next request cannot start earlier. The controller calls it
once per thought.

In the stream mode (`[reveal] mode = "stream"`, ADR-030) the screen is one `StreamScreen`
for the whole life: it types every word at one constant pace, thought after thought, while
`write_ahead()` generates the next thought as soon as the previous one is generated, into a
bounded buffer. The display never waits except at birth; when it must (the buffer ran dry
while the creature lives) that is starvation, measured and reported. At death the stream
stops where it is.

Timing is in the life clock's seconds; cadence values are integer milliseconds.
"""

from __future__ import annotations

import asyncio
import random
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from epitaph.backend.base import CreatureDied
from epitaph.clock import LifeClock, Schedule
from epitaph.config import Config
from epitaph.mind.sanitize import Sanitizer
from epitaph.mind.words import WordSegmenter, normalize, split_words
from epitaph.types import Chunk, CreatureStatus, Knobs, TimedWord, Word

__all__ = [
    "BannedHit",
    "Lookahead",
    "Pacer",
    "PushResult",
    "ScreenEvent",
    "Spoken",
    "StreamCurve",
    "StreamScreen",
    "StreamStats",
    "ThoughtMark",
    "life_seed",
    "pause_after_ms",
    "speak",
    "write_ahead",
]

_SENTENCE_END = ".?!…"
_CLAUSE_END = ",;:—–"
_CLOSERS = "\"'”’»)]}›"
STREAM_BIRTHS = ("thought", "sentence")


def ends_sentence(text: str) -> bool:
    """Whether a word ends a sentence (closing quotes ignored)."""
    core = text.rstrip(_CLOSERS)
    return bool(core) and core[-1] in _SENTENCE_END


@dataclass(frozen=True)
class BannedHit:
    """A banned phrase found in a thought."""

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
        """Match against phrases; duplicates after normalization keep their first spelling."""
        seen: dict[tuple[str, ...], str] = {}
        for p in phrases:
            norm = tuple(n for n in (normalize(w) for w in split_words(p)) if n)
            if norm:
                seen.setdefault(norm, p)
        self.phrases = seen
        self.cap = max(1, cap)
        self.held: list[str] = []

    def reset(self) -> None:
        """Drop every held word, for a new attempt at a thought."""
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

    def truncated_hit(self, last: str) -> tuple[int, str] | None:
        """At the end of generation the last word may be cut off ("How can I hel"). If the
        held words would complete a banned phrase with it, report where that phrase starts
        (an index into `held`)."""
        norms = [normalize(w) for w in [*self.held, last]]
        if not norms or not norms[-1]:
            return None
        for j in range(len(norms)):
            if not norms[j]:
                continue
            seq = tuple(n for n in norms[j:] if n)
            for p, original in self.phrases.items():
                if len(seq) == len(p) and seq[:-1] == p[:-1] and p[-1].startswith(seq[-1]):
                    return j, original
        return None

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
        max_letter_ms: float = 1200.0,
    ) -> None:
        """Build the pipeline; the keyword arguments mirror config's [output] and [reveal].

        rate_margin scales the measured generation rate so typing stays a little slower than
        generation; the rate is averaged over the last rate_window_s seconds of chunks and
        trusted once min_rate_sample_s seconds are measured. With adaptive off, letters follow
        the profile's floor alone. Pauses and hesitations are in milliseconds; a hesitation
        may fall inside a word once the knob's hesitation reaches hesitation_inside_from.
        max_letter_ms caps the adaptive interval: at 2-bit a run of digits comes one token a
        letter, and following the rate would type it at about a word a minute.
        """
        self.clock = clock
        self.rng = random.Random(seed)
        self.max_regenerations = max_regenerations
        self.adaptive = adaptive
        self.rate_margin = rate_margin
        self.max_letter_ms = max_letter_ms
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
        self._sample_letters = 0
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
        self._flush_by: float | None = None  # at death, when the last word must be typed
        self._last_end: float | None = None
        # The stream mode's screen: released words go to it instead of this thought's queue.
        self.screen: StreamScreen | None = None

    @classmethod
    def from_config(cls, cfg: Config, clock: LifeClock, seed: int = 0) -> Pacer:
        """A pacer set up from the [output], [reveal] and prompt.banned_phrases settings."""
        out = cfg.section("output")
        rev = cfg.section("reveal")
        hes = list(rev.get("hesitation_ms", [1200, 3600]))
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
            word_gap_ms=int(rev.get("word_gap_ms", 270)),
            comma_pause_ms=int(rev.get("comma_pause_ms", 750)),
            sentence_pause_ms=int(rev.get("sentence_pause_ms", 2100)),
            hesitation_ms=(int(hes[0]), int(hes[1])),
            hesitation_inside_from=float(rev.get("hesitation_inside_from", 0.1)),
            max_letter_ms=float(rev.get("max_letter_ms", 1200)),
        )

    # -- the generation rate -------------------------------------------------------------

    def set_rate_estimate(self, letters_per_s: float) -> None:
        """Use this rate (from the bench) until enough new generation has been measured.
        Called at birth and after each reload; it forgets the old measurements."""
        self._estimate = letters_per_s if letters_per_s > 0 else None
        self._samples.clear()
        self._sample_s = 0.0
        self._sample_letters = 0

    def rate(self) -> float | None:
        """Smoothed generation rate in letters per second."""
        if self._sample_s >= self.min_rate_sample_s and self._sample_letters > 0:
            return self._sample_letters / self._sample_s
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
            self._sample_letters += letters
            while self._samples and self._sample_s - self._samples[0][0] >= self.rate_window_s:
                old_dt, old_letters = self._samples.popleft()
                self._sample_s -= old_dt
                self._sample_letters -= old_letters
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
        self._flush_by = None
        self.begin_request()

    def flush_within(self, seconds: float) -> None:
        """At death: the words still queued finish typing within `seconds` from now. Their
        rhythm keeps its shape, only faster when it would not fit (
        verify's max_death_display_delay_s)."""
        by = self.clock.elapsed() + max(0.0, seconds)
        self._flush_by = by if self._flush_by is None else min(self._flush_by, by)

    def _fit(self, tw: TimedWord) -> TimedWord:
        """Scale a word's cadence so it and the words after it end by the flush deadline."""
        if self._flush_by is None:
            return tw
        own = sum(tw.char_ms) + tw.pause_after_ms + tw.hesitate_before_ms
        # an equal share of what is left for this word and each word after it, so the last
        # one ends by the deadline whatever their lengths
        left_ms = (self._flush_by - self.clock.elapsed()) * 1000
        share = max(left_ms, 0.0) / (1 + len(self._queue))
        if own <= share or own <= 0:
            return tw
        k = share / own
        return replace(
            tw,
            char_ms=tuple(round(c * k) for c in tw.char_ms),
            pause_after_ms=round(tw.pause_after_ms * k),
            hesitate_before_ms=round(tw.hesitate_before_ms * k),
        )

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
        """True once the thought was cut at a `[host]` line or a chat template token."""
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
        return self._hit_at(j, phrase, released, dead)

    def _hit_at(self, j: int, phrase: str, released: list[Word], dead: bool) -> PushResult:
        """A banned phrase starts at held[j]: regenerate (at the start) or cut there."""
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
        if self.screen is not None:
            self.screen.put(word)
            return word
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
            for n, w in enumerate(words):
                cut = self.lookahead.truncated_hit(w) if n == len(words) - 1 else None
                if cut is not None and cut[0] < len(self.lookahead.held):
                    res = self._hit_at(cut[0], cut[1], released, dead)
                else:
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
        if self.screen is not None:
            self.screen.end_thought(self.turn, self.text)
        return self._hit

    # -- cadence ---------------------------------------------------------------------------

    def letter_interval_ms(self, knobs: Knobs) -> float:
        """interval = max(profile floor, 1 / (margin x rate))."""
        floor = knobs.letter_ms
        r = self.rate() if self.adaptive else None
        if r is None or r <= 0:
            return floor
        return max(floor, min(1000.0 / (self.rate_margin * r), self.max_letter_ms))

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
            tw = self._fit(self.cadence(word, knobs))
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
# the stream screen (ADR-030)


@dataclass(frozen=True)
class ThoughtMark:
    """The end of a generated thought in the stream: what its `thought_end` will say."""

    turn: int
    text: str


@dataclass
class StreamStats:
    """What the stream screen did, for the death record, the tests and verify-life."""

    words: int = 0
    letters: int = 0
    first_word_t: float | None = None
    # (life time the screen was ready for the next word, seconds it waited for it)
    stalls: list[tuple[float, float]] = field(default_factory=lambda: [])
    max_backlog_letters: int = 0
    dropped_words: int = 0
    dropped_letters: int = 0

    @property
    def starved_s(self) -> float:
        """Seconds the screen waited for a word after the first one, in all."""
        return sum(s for _, s in self.stalls)

    @property
    def max_stall_s(self) -> float:
        """The longest single wait for a word after the first one."""
        return max((s for _, s in self.stalls), default=0.0)


@dataclass(frozen=True)
class ScreenEvent:
    """An event for the screen that waits its turn in the stream (a forgetting fades the
    text when the screen reaches the moment it happened, not while it is still typing what
    came before)."""

    etype: str
    fields: dict[str, Any]


StreamItem = Word | ThoughtMark | ScreenEvent
STALL_EPS_S = 1e-6


class StreamCurve:
    """The stream's letter interval over life time (ADR-030, amended 2026-10-01).

    At birth the stream types at `birth_ms` a letter. As the machine shrinks it slows,
    smoothly and never back: the target interval follows the hardware,
    `birth_ms x (compute at birth / compute(t + lead_s)) ^ gamma` (compute = CPU share x
    clock), and the curve moves toward it by at most `max_slowdown_per_min` a minute, so a
    step of the hardware is a gentle slope on screen. `lead_s` lets the slope start before
    the step: the screen shows text written minutes earlier. The curve never falls, and never
    goes over `max_ms`. With `gamma` 0 the pace is constant.
    """

    def __init__(self, birth_ms: float, values: Sequence[float], step_s: float = 1.0) -> None:
        """A curve sampled every `step_s` seconds of life from birth (`values[0]`)."""
        if birth_ms <= 0:
            raise ValueError("the stream's letter interval must be above 0")
        self.birth_ms = birth_ms
        self.values = list(values) or [birth_ms]
        self.step_s = step_s

    @classmethod
    def constant(cls, letter_ms: float) -> StreamCurve:
        """One pace for the whole life."""
        return cls(letter_ms, [letter_ms])

    @classmethod
    def build(
        cls,
        sch: Schedule,
        birth_ms: float,
        *,
        gamma: float = 0.0,
        lead_s: float = 0.0,
        max_slowdown_per_min: float = 0.15,
        max_ms: float = 2000.0,
        step_s: float = 1.0,
    ) -> StreamCurve:
        """The curve of `sch` (see the class)."""
        c0 = sch.at(0).compute
        n = int(sch.lifespan_s / step_s) + 2
        grow = (1 + max(0.0, max_slowdown_per_min)) ** (step_s / 60)
        out: list[float] = [birth_ms]
        for i in range(1, n):
            ahead = min(i * step_s + lead_s, sch.lifespan_s)
            c = max(sch.at(ahead).compute, 1e-6)
            target = min(max_ms, birth_ms * (c0 / c) ** gamma) if gamma else birth_ms
            prev = out[-1]
            out.append(max(prev, min(target, prev * grow)))
        return cls(birth_ms, out, step_s)

    @classmethod
    def from_config(cls, cfg: Config, sch: Schedule | None = None) -> StreamCurve:
        """The curve from `[reveal]`: `stream_letter_ms` at birth, `stream_gamma`,
        `stream_lead_s`, `stream_max_slowdown_per_min` and `stream_max_letter_ms`."""
        rev = cfg.section("reveal")
        return cls.build(
            sch or Schedule(cfg.profile),
            float(rev.get("stream_letter_ms", 165)),
            gamma=float(rev.get("stream_gamma", 0.0)),
            lead_s=float(rev.get("stream_lead_s", 0.0)),
            max_slowdown_per_min=float(rev.get("stream_max_slowdown_per_min", 0.15)),
            max_ms=float(rev.get("stream_max_letter_ms", 2000)),
        )

    def at(self, t: float) -> float:
        """The letter interval (ms) at life time `t`."""
        x = max(0.0, t) / self.step_s
        i = int(x)
        if i + 1 >= len(self.values):
            return self.values[-1]
        a, b = self.values[i], self.values[i + 1]
        return a + (b - a) * (x - i)

    def scale(self, t: float) -> float:
        """How much slower than at birth the stream is at `t` (pauses scale with it)."""
        return self.at(t) / self.birth_ms


class StreamScreen:
    """One stream of words for a whole life (ADR-030).

    Words are put in as they are generated; `run` types them one after another at the
    curve's letter interval at that moment (`curve`, with a small fixed `jitter`; constant
    `letter_ms` without one), the pauses after words, clauses and sentences, and
    `thought_pause_ms` between thoughts, all scaled with the curve. No hesitation; the pace
    only ever slows, smoothly, as the machine shrinks. When the next word is
    not there when the screen is ready for it, the screen waits: a stall (starvation),
    recorded with its length. The writer keeps the buffer bounded with `wait_for_room`: at
    most `max_thoughts` generated thoughts and fewer than `max_letters` letters waiting.
    At birth the screen waits until `birth_thoughts` thoughts are generated, or with
    `birth = "sentence"` until the first sentence is (dread plan W4: the first words come
    sooner; the cost model checks that the rest keeps up), and never before `birth_min_s`
    of life (a head start for a faster birth pace): the only wait the stream allows.
    `stop` (the death) ends the stream where it is: the words still waiting die with it.
    """

    def __init__(
        self,
        clock: LifeClock,
        *,
        letter_ms: float,
        jitter: float = 0.0,
        word_gap_ms: int = 270,
        comma_pause_ms: int = 750,
        sentence_pause_ms: int = 2100,
        thought_pause_ms: int = 3000,
        max_thoughts: int = 3,
        max_letters: int = 600,
        birth_thoughts: int = 1,
        stall_report_s: float = 0.5,
        seed: int = 0,
        curve: StreamCurve | None = None,
        birth: str = "thought",
        birth_min_s: float = 0.0,
    ) -> None:
        """A screen on `clock` typing at this pace; see the class for the bounds."""
        if letter_ms <= 0:
            raise ValueError("the stream's letter_ms must be above 0")
        if birth not in STREAM_BIRTHS:
            raise ValueError(f"the stream's birth must be one of {STREAM_BIRTHS}, not {birth!r}")
        self.birth = birth
        self.birth_min_s = max(0.0, birth_min_s)
        self._sentence = False  # a whole sentence is generated (birth = "sentence")
        self.clock = clock
        self.letter_ms = letter_ms
        self.curve = curve or StreamCurve.constant(letter_ms)
        self.jitter = jitter
        self.word_gap_ms = word_gap_ms
        self.comma_pause_ms = comma_pause_ms
        self.sentence_pause_ms = sentence_pause_ms
        self.thought_pause_ms = thought_pause_ms
        self.max_thoughts = max(1, max_thoughts)
        self.max_letters = max(1, max_letters)
        self.birth_thoughts = max(0, min(birth_thoughts, self.max_thoughts))
        self.stall_report_s = stall_report_s
        self.rng = random.Random(seed)
        self.stats = StreamStats()
        self.stopped = False
        self._items: deque[StreamItem] = deque()
        self._letters = 0
        self._marks = 0
        self._wake = asyncio.Event()  # something to type, or the stop
        self._room = asyncio.Event()  # something left the buffer, or the stop
        # The thought on screen whose end is not reached yet, and its words shown so far.
        self.open_turn: int | None = None
        self.open_words: list[str] = []
        self._ready_at: float | None = None  # when the screen was ready for its next word

    @classmethod
    def from_config(
        cls, cfg: Config, clock: LifeClock, seed: int = 0, schedule: Schedule | None = None
    ) -> StreamScreen:
        """A screen set up from `[reveal]`: the curve (`StreamCurve.from_config`),
        `stream_jitter`, the word, comma and sentence pauses, `stream_thought_pause_ms` and
        the buffer bounds."""
        rev = cfg.section("reveal")
        return cls(
            clock,
            letter_ms=float(rev.get("stream_letter_ms", 165)),
            jitter=float(rev.get("stream_jitter", 0.0)),
            word_gap_ms=int(rev.get("word_gap_ms", 270)),
            comma_pause_ms=int(rev.get("comma_pause_ms", 750)),
            sentence_pause_ms=int(rev.get("sentence_pause_ms", 2100)),
            thought_pause_ms=int(rev.get("stream_thought_pause_ms", 3000)),
            max_thoughts=int(rev.get("stream_max_thoughts", 3)),
            max_letters=int(rev.get("stream_max_letters", 600)),
            birth_thoughts=int(rev.get("stream_birth_thoughts", 1)),
            stall_report_s=float(rev.get("stream_stall_report_s", 0.5)),
            seed=seed,
            curve=StreamCurve.from_config(cfg, schedule),
            birth=str(rev.get("stream_birth", "thought")),
            birth_min_s=float(rev.get("stream_birth_min_s", 0.0)),
        )

    # -- the writer's side ---------------------------------------------------------------

    def put(self, word: Word) -> None:
        """A generated word joins the stream (ignored once the stream has stopped)."""
        if self.stopped:
            return
        self._items.append(word)
        self._sentence = self._sentence or ends_sentence(word.text)
        self._letters += len(word.text)
        self.stats.max_backlog_letters = max(self.stats.max_backlog_letters, self._letters)
        self._wake.set()

    def end_thought(self, turn: int, text: str) -> None:
        """The thought `turn` is fully generated: its end follows its last word."""
        if self.stopped:
            return
        self._items.append(ThoughtMark(turn, text))
        self._marks += 1
        self._wake.set()

    def put_event(self, etype: str, **fields: Any) -> None:
        """An event the screen emits when it reaches this point of the stream."""
        if self.stopped:
            return
        self._items.append(ScreenEvent(etype, fields))
        self._wake.set()

    @property
    def letters(self) -> int:
        """Letters generated and not yet begun on the screen."""
        return self._letters

    @property
    def thoughts(self) -> int:
        """Generated thoughts whose end the screen has not reached yet."""
        return self._marks

    def has_room(self) -> bool:
        """Whether the writer may start the next thought (always, once stopped)."""
        return self.stopped or (
            self._letters < self.max_letters and self._marks < self.max_thoughts
        )

    async def wait_for_room(self) -> None:
        """Wait until the buffer has room for another thought, or the stream stopped."""
        while not self.has_room():
            self._room.clear()
            await self._room.wait()

    # -- the screen's side -----------------------------------------------------------------

    def cadence(self, word: Word, t: float | None = None) -> TimedWord:
        """The pace at life time `t` (now by default): the curve's interval per letter
        within the jitter, and the pauses scaled with it."""
        at = self.clock.elapsed() if t is None else t
        interval = self.curve.at(at)
        k = self.curve.scale(at)
        char_ms = tuple(
            max(1, round(interval * (1 + self.jitter * (2 * self.rng.random() - 1))))
            for _ in word.text
        )
        pause = pause_after_ms(
            word.text,
            round(self.word_gap_ms * k),
            round(self.comma_pause_ms * k),
            round(self.sentence_pause_ms * k),
        )
        return TimedWord(word, char_ms, pause, 0)

    async def run(
        self,
        on_word: Callable[[TimedWord], None],
        on_end: Callable[[ThoughtMark], None],
        on_stall: Callable[[float, float], None] | None = None,
        on_event: Callable[[ScreenEvent], None] | None = None,
    ) -> None:
        """Type the stream until `stop`.

        `on_word` is called as each word starts typing, `on_end` when the screen reaches a
        thought's end, `on_event` when it reaches a `put_event`, and `on_stall(ready_at,
        seconds)` after a wait of at least `stall_report_s` for a word.
        """
        cursor: float | None = None  # when the screen is ready for the next word
        typed_end: float | None = None  # when the last letter so far was typed
        after_mark = False
        born = self.birth_thoughts == 0
        while not self.stopped:
            # born once enough is written, or once the writer cannot write more
            born = born or self._marks >= self.birth_thoughts or not self.has_room()
            born = born or (self.birth == "sentence" and (self._sentence or self._marks > 0))
            if not self._items or not born:
                self._wake.clear()
                await self._wake.wait()
                continue
            now = self.clock.elapsed()
            if cursor is not None and now < cursor - STALL_EPS_S:
                await self.clock.sleep(cursor - now)
                continue
            if cursor is None and now < self.birth_min_s - STALL_EPS_S:
                await self.clock.sleep(self.birth_min_s - now)  # the first word's floor
                continue
            item = self._items.popleft()
            self._room.set()
            if isinstance(item, ScreenEvent):
                if on_event is not None:
                    on_event(item)
                continue
            if isinstance(item, ThoughtMark):
                self._marks -= 1
                self.open_turn, self.open_words = None, []
                on_end(item)
                if typed_end is not None and not after_mark:
                    k = self.curve.scale(typed_end)
                    cursor = self._ready_at = typed_end + round(self.thought_pause_ms * k) / 1000
                after_mark = True
                continue
            self._letters -= len(item.text)
            if cursor is not None and now > cursor + STALL_EPS_S:
                waited = now - cursor
                self.stats.stalls.append((cursor, waited))
                if on_stall is not None and waited >= self.stall_report_s:
                    on_stall(cursor, waited)
            tw = self.cadence(item, now)
            if self.stats.first_word_t is None:
                self.stats.first_word_t = now
            self.stats.words += 1
            self.stats.letters += len(item.text)
            if self.open_turn != item.turn:
                self.open_turn, self.open_words = item.turn, []
            on_word(tw)
            self._ready_at = None  # typing: the screen is not waiting
            await self.clock.sleep(sum(tw.char_ms) / 1000)
            # shown only once its last letter is typed: a word the death cuts in half is in
            # neither the thought's shown text nor the last line
            self.open_words.append(item.text)
            typed_end = self.clock.elapsed()
            cursor = self._ready_at = typed_end + tw.pause_after_ms / 1000
            after_mark = False

    def stop(self) -> None:
        """The death: the stream stops where it is; what is still waiting is dropped."""
        if self.stopped:
            return
        self.stopped = True
        waiting = not any(isinstance(item, Word) for item in self._items)
        now = self.clock.elapsed()
        if waiting and self._ready_at is not None and now > self._ready_at + STALL_EPS_S:
            # the screen was waiting for a word when it died: that blank is a stall too
            self.stats.stalls.append((self._ready_at, now - self._ready_at))
        for item in self._items:
            if isinstance(item, Word):
                self.stats.dropped_words += 1
                self.stats.dropped_letters += len(item.text)
        self._items.clear()
        self._letters = 0
        self._marks = 0
        self._wake.set()
        self._room.set()


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
    # life times of the first and the latest token of the current request
    first_token_t: float | None = None
    last_token_t: float | None = None

    def partial_tok_s(self) -> float | None:
        """Tokens/s of a request cut by the death, from its own token times (None if unknown)."""
        a, b = self.first_token_t, self.last_token_t
        if a is None or b is None or b <= a or self.tokens < 2:
            return None
        return round((self.tokens - 1) / (b - a), 3)

    @property
    def text(self) -> str:
        """The words shown, joined by single spaces."""
        return " ".join(w.text for w in self.words)


async def _close(stream: AsyncIterator[Chunk]) -> None:
    if isinstance(stream, AsyncGenerator):
        await stream.aclose()


async def _requests(
    pacer: Pacer,
    stream: Callable[[], AsyncIterator[Chunk]],
    turn: int,
    emit: Emit,
    result: Spoken,
) -> None:
    """The requests of one thought: regenerate on a banned opening, stop at a cut, finish."""
    while True:
        result.requests += 1
        pacer.begin_request()
        emit("gen_start", turn=turn)
        chunks = stream()
        regenerate = False
        tokens = 0
        ended = False
        result.first_token_t = result.last_token_t = None
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
                result.last_token_t = pacer.clock.elapsed()
                if result.first_token_t is None:
                    result.first_token_t = result.last_token_t
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
        return


def _outcome(pacer: Pacer, result: Spoken) -> Spoken:
    result.words = list(pacer.shown)
    result.regenerations = pacer.regenerations
    result.hit = pacer.finish_thought(dead=result.died is not None)
    result.cut_at_host = pacer.cut_at_host
    return result


async def speak(
    pacer: Pacer,
    stream: Callable[[], AsyncIterator[Chunk]],
    knobs: Knobs,
    turn: int,
    emit: Emit,
    on_died: Callable[[CreatureStatus], None] | None = None,
    death_flush_s: float = 75.0,
) -> Spoken:
    """Generate and show one thought.

    Emits `gen_start` and `gen_end` per request and `word` per shown word. Regenerates on a
    banned opening, stops the request at a cut, and on `CreatureDied` calls `on_died` at
    once (the real moment of death), then shows the words already generated at the current
    pace, compressed when needed so they end within `death_flush_s`. Returns after the last
    word has finished typing (the sync rule).
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
        await _requests(pacer, stream, turn, emit, result)
    except CreatureDied as e:
        result.died = e.status
        emit("gen_end", turn=turn, prompt_n=None, tokens=result.tokens, tok_s=None)
        if on_died is not None:
            on_died(e.status)
        pacer.flush_within(death_flush_s)
        pacer.finish_thought(dead=True)
    except BaseException:
        shower.cancel()
        raise
    await shower
    return _outcome(pacer, result)


async def write_ahead(
    pacer: Pacer,
    stream: Callable[[], AsyncIterator[Chunk]],
    turn: int,
    emit: Emit,
    on_died: Callable[[CreatureStatus], None] | None = None,
) -> Spoken:
    """Generate one thought into the stream screen (ADR-030); return once it is generated.

    The pacer's `screen` types the words at the stream's pace while this returns as soon as
    generation ends, so the next thought can start. On `CreatureDied` it calls `on_died` at
    once; nothing is flushed: the stream stops where it is.
    """
    if pacer.screen is None:
        raise ValueError("write_ahead needs a pacer with a stream screen")
    pacer.begin_thought(turn)
    result = Spoken(turn, [])
    try:
        await _requests(pacer, stream, turn, emit, result)
    except CreatureDied as e:
        result.died = e.status
        # the thought the death cut still measured the machine's last speed (the stream's
        # last minutes often end no thought: speed_decline needs this sample)
        tok_s = result.partial_tok_s()
        emit("gen_end", turn=turn, prompt_n=None, tokens=result.tokens, tok_s=tok_s)
        if on_died is not None:
            on_died(e.status)
        pacer.finish_thought(dead=True)
    return _outcome(pacer, result)
