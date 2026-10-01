"""Shared data types: the vocabulary every module codes against (BUILD_PLAN 6.4).

These types are part of the contract between modules; record any change to them in
docs/process/CONTRACT_CHANGES.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

PROTOCOL_VERSION = 1

Role = Literal["system", "user", "assistant"]
WordState = Literal["live", "fading", "forgotten", "inherited"]
ReadingsForm = Literal["full", "short", "minimal"]


class Cause(StrEnum):
    """Why a life ended."""

    OOM = "oom"
    DEADLINE = "deadline"
    FULL = "full"
    CRASH = "crash"
    HANG = "hang"
    MANUAL = "manual"
    INTERRUPTED = "interrupted"


class Health(StrEnum):
    """The health label shown in the readings; steps, never interpolates."""

    NOMINAL = "nominal"
    STABLE = "stable"
    DEGRADING = "degrading"
    FAILING = "failing"
    CRITICAL = "critical"
    TERMINAL = "terminal"


FULL_MHZ = 1800.0  # the Pi 4's full clock; spike S7 measured speed linear below it


@dataclass(frozen=True)
class Knobs:
    """Every schedule-driven setting at one moment of a life (BUILD_PLAN 5.3).

    Stepped fields: phase, health, step, threads, persona_groups, readings.
    Interpolated fields: everything else.
    """

    t: float
    phase: str
    health: Health
    recall: int
    step: int
    threads: int
    cpu_share: float
    temperature: float
    min_p: float
    max_tokens: int
    pause_s: float
    persona_groups: int
    mechanics: bool
    readings: ReadingsForm
    letter_ms: float
    jitter: float
    hesitation: float
    cpu_mhz: float = FULL_MHZ  # CPU clock cap (cpufreq); speed scales with it like the share
    death_squeeze: bool = False

    @property
    def compute(self) -> float:
        """Cores' worth of full-clock compute: the CPU share scaled by the clock (spike S7)."""
        return self.cpu_share * self.cpu_mhz / FULL_MHZ


@dataclass(frozen=True)
class Sampling:
    """Sampling parameters sent to the backend for one thought."""

    temperature: float
    min_p: float
    top_p: float = 1.0
    repeat_penalty: float = 1.1
    dry_multiplier: float = 0.8
    seed: int | None = None
    latin_only: bool = False
    # Silent word penalties: (text, bias) pairs, applied by the server to the text's tokens.
    logit_bias: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class ModelSpec:
    """A model and its precision ladder for the current hardware class."""

    name: str
    source: str
    license: str
    ladder: tuple[str, ...]
    sliding_window: bool = False
    chat: bool = True

    def quant(self, step: int) -> str:
        """Quant name for a ladder step; the last step repeats if the ladder is shorter."""
        return self.ladder[min(step, len(self.ladder) - 1)]


@dataclass(frozen=True)
class Msg:
    """One chat message held in the mind's memory."""

    role: Role
    content: str
    turn: int = -1
    kind: Literal["persona", "mechanics", "reading", "thought", "marker"] = "thought"


@dataclass(frozen=True)
class Chunk:
    """A streamed piece of backend output. The last chunk of a request carries timings."""

    text: str
    done: bool = False
    prompt_n: int | None = None
    predicted_n: int | None = None
    prompt_per_s: float | None = None
    predicted_per_s: float | None = None


@dataclass(frozen=True)
class Word:
    """A whole word with attached punctuation, as released to the displays."""

    turn: int
    i: int
    text: str


@dataclass(frozen=True)
class TimedWord:
    """A released word with its typing cadence (BUILD_PLAN 5.12)."""

    word: Word
    char_ms: tuple[int, ...]
    pause_after_ms: int
    hesitate_before_ms: int = 0


@dataclass
class CreatureStatus:
    """What the backend knows about the creature process."""

    alive: bool
    pid: int | None = None
    exit_code: int | None = None
    signal: int | None = None
    tok_s: float | None = None
    prompt_tok_s: float | None = None


@dataclass(frozen=True)
class ProgressCounters:
    """Monotonic counters used for hang detection (BUILD_PLAN 5.9)."""

    cpu_usec: int = 0
    io_rbytes: int = 0
    majfault: int = 0


@dataclass(frozen=True)
class MachineFacts:
    """True facts about the machine, for the optional persona facts line."""

    model: str
    cores: int
    ram_gb: float


@dataclass(frozen=True)
class Vitals:
    """A snapshot of the body, reported in the readings."""

    cpu_c: float | None = None
    throttled: int | None = None
    ram_limit_mb: int | None = None
    mem_used_mb: int | None = None
    cores_effective: float | None = None


@dataclass
class RuleViolation:
    """One broken thought-count rule (BUILD_PLAN 5.3)."""

    rule: str
    at_s: float
    detail: str


@dataclass
class StreamEstimate:
    """The cost model's replay of a stream life (ADR-030): generation written ahead of one
    constant screen, with every machine cost `margin` slower."""

    letter_ms: float  # at birth
    wpm: float  # at birth
    margin: float
    letter_ms_end: float = 0.0
    wpm_middle: float = 0.0
    wpm_end: float = 0.0
    gamma: float = 0.0
    lead_s: float = 0.0
    buffer: list[tuple[float, int]] = field(default_factory=lambda: [])  # (t, letters waiting)
    stalls: list[tuple[float, float]] = field(default_factory=lambda: [])  # (ready at, seconds)
    backlog_letters: int = 0  # generated, not yet shown at death
    backlog_words: int = 0
    backlog_s: float = 0.0  # how long the screen would need to show the backlog
    max_buffer_letters: int = 0
    # The birth (dread plan W4): the life time of the first word, and the seconds from the
    # end of the silence before the life to it (the load left over from the silence, the
    # system prompt read or restored, the first reading and sentence or thought).
    first_word_t: float = 0.0
    first_words_s: float = 0.0
    birth_note: str = ""

    @property
    def first_starvation(self) -> float | None:
        """Life time of the first wait for a word after the first, None if it never waits."""
        return self.stalls[0][0] if self.stalls else None

    @property
    def starved_s(self) -> float:
        """Seconds the screen waits in all."""
        return sum(s for _, s in self.stalls)


@dataclass
class RuleReport:
    """The cost model's estimate of a life and the thought-count rule result."""

    profile: str
    lifespan_s: float
    thought_times: list[float] = field(default_factory=lambda: [])
    reload_windows: list[tuple[float, float]] = field(default_factory=lambda: [])
    violations: list[RuleViolation] = field(default_factory=lambda: [])
    notes: list[str] = field(default_factory=lambda: [])
    stream: StreamEstimate | None = None

    @property
    def ok(self) -> bool:
        """True when no rule is violated."""
        return not self.violations

    @property
    def thoughts(self) -> int:
        """Number of thoughts the life is estimated to have."""
        return len(self.thought_times)
