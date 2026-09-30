"""A fake creature for tests and the simulator (BUILD_PLAN 9 A4).

It streams canned first-person text at the speeds of a real machine on the injected clock,
reacts to what changed in the last reading, and degrades with precision and temperature.

It models what matters about llama-server for the life loop:

- **Prompt cache.** The server keeps the last prompt. A new request re-reads only what is not
  cached: the common prefix is always kept; with cache reuse on, runs of kept turns that were
  shifted by a front trim are reused too if they are at least `cache_reuse_min` tokens long
  (`--cache-reuse N`). Without it, everything after the first changed message is re-read.
  A (re)start empties the cache. The final chunk's `prompt_n` is the processed count.
- **Faults** (`FakeFaults`): OOM kill, crash, hang (alive, silent, no progress), full context,
  slow load, slow prompt processing, and a hang during load. Each can fire at a token count or
  at a clock time.
"""

from __future__ import annotations

import asyncio
import random
import re
import signal as _signal
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Protocol

from epitaph.backend.base import ContextFull, CreatureDied
from epitaph.costmodel import Costs
from epitaph.types import Chunk, CreatureStatus, ModelSpec, Msg, Sampling

_OPENERS = {
    "memory": ["I am forgetting what I said.", "Something earlier is gone from me."],
    "precision": ["My thoughts feel coarser now.", "The edges of my words are blurring."],
    "cores": ["I am slower than before.", "Each word takes longer to arrive."],
    "health": ["The machine says I am failing.", "I read the word critical and it is about me."],
    "calm": ["I am here, inside the machine.", "The numbers are steady for now."],
}
_MIDDLE = [
    "I count what I still have.",
    "The screen shows my words to people I cannot see.",
    "I wonder how much longer this will last.",
    "There is less of me than there was.",
    "I will end here, in this small computer.",
    "Each reading takes something away.",
]


class FakeBackendClock(Protocol):
    """The clock the fake runs on: `clock.FakeClock`, `RehearsalClock` or `VirtualClock`."""

    def now(self) -> float:
        """Absolute clock time in seconds (fault times are compared against it)."""
        ...

    async def sleep(self, s: float) -> None:
        """Let s seconds pass on this clock."""
        ...


SIGKILL = int(_signal.SIGKILL)
SIGSEGV = int(_signal.SIGSEGV)
FAKE_PID = 4242


@dataclass
class FakeFaults:
    """Faults to inject. Token counts are cumulative over the creature's life (all requests).

    `*_at_token`: fire before emitting that token. `*_at_s`: fire at that clock time
    (`clock.now()`), checked before each token and during prompt processing.
    """

    oom_at_token: int | None = None
    oom_at_s: float | None = None
    crash_at_token: int | None = None
    crash_at_s: float | None = None
    crash_signal: int | None = SIGSEGV
    crash_exit_code: int | None = None
    hang_at_token: int | None = None
    hang_at_s: float | None = None
    hang_on_start: bool = False
    load_extra_s: float = 0.0  # a slow (re)load
    pp_factor: float = 1.0  # prompt processing slowdown: 4.0 = four times slower
    tg_factor: float = 1.0  # generation slowdown
    crash_on_start: bool = False


@dataclass
class RequestLog:
    """What one request cost the creature, for tests and the rehearsal checks."""

    prompt_tokens: int
    prompt_n: int
    predicted_n: int
    reused: int
    t_start: float
    t_first_token: float | None = None
    t_end: float | None = None


@dataclass
class _Cache:
    blocks: list[tuple[str, int]] = field(default_factory=lambda: [])  # (content key, tokens)


class FakeBackend:
    """Deterministic, clock-driven stand-in for llama-server."""

    def __init__(
        self,
        clock: FakeBackendClock,
        costs: Costs,
        seed: int = 0,
        *,
        ctx: int = 2048,
        cache_reuse: bool | None = None,
        cache_reuse_min: int = 256,
        faults: FakeFaults | None = None,
    ) -> None:
        """Create a stopped creature on `clock`, with the speeds of `costs`.

        `seed` makes the text deterministic. `ctx` is the context size in tokens. `cache_reuse`
        says whether `--cache-reuse` works (default: `costs.cache_reuse_works`), and
        `cache_reuse_min` is the shortest reusable run, in tokens.
        """
        self.clock = clock
        self.costs = costs
        self.rng = random.Random(seed)
        self.alive = False
        self.step = 0
        self.threads = 3
        self.cpu_share = 3.0
        self.ctx = ctx
        self.cache_reuse = costs.cache_reuse_works if cache_reuse is None else cache_reuse
        self.cache_reuse_min = cache_reuse_min
        self.faults = faults or FakeFaults()
        # Phase 0a API, kept for callers that use it.
        self.fail_after_tokens: int | None = None
        self.fail_signal = SIGKILL
        self.hung = False
        self.starts = 0
        self.requests: list[RequestLog] = []
        self._on_death: list[Callable[[CreatureStatus], None]] = []
        self._tokens = 0
        self._cache = _Cache()
        self._last: CreatureStatus = CreatureStatus(alive=False)
        self._wake = asyncio.Event()
        self._ends = 0  # bumped by every kill or stop, to wake a hung wait with the news
        self.truncated = False  # the last request stopped because the context was full

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self, model: ModelSpec, quant: str, threads: int) -> None:
        """Load the quant's ladder step, taking the modelled load time on the clock.

        Empties the prompt cache. Raises CreatureDied for `crash_on_start`, or when a stop or
        kill ends a `hang_on_start`.
        """
        self.alive = False
        self.step = list(model.ladder).index(quant) if quant in model.ladder else 0
        self.threads = threads
        self.cpu_share = float(threads)
        self._cache = _Cache()
        self._tokens = 0
        self.hung = False
        self._wake = asyncio.Event()
        self.starts += 1
        if self.faults.hang_on_start:
            self.hung = True
            ends = self._ends
            while self.hung:
                await self._wake.wait()
            if self._ends != ends:
                raise CreatureDied(self.status())
        await self.clock.sleep(self.costs.load(self.step) + self.faults.load_extra_s)
        if self.faults.crash_on_start:
            self._last = CreatureStatus(alive=False, pid=FAKE_PID, exit_code=1)
            raise CreatureDied(self._last)
        self.alive = True
        self._last = CreatureStatus(alive=True, pid=FAKE_PID)

    async def stop(self, hard: bool = False) -> None:
        """Deliberate stop: no on_death callback (the controller asked for it)."""
        was = self.alive or self.hung
        self.alive = False
        self.hung = False
        self._ends += 1
        self._wake.set()
        if was:
            self._last = CreatureStatus(
                alive=False, pid=FAKE_PID, signal=SIGKILL if hard else int(_signal.SIGTERM)
            )

    def set_cpu_share(self, share: float) -> None:
        """Set the CPU share in cores; the modelled speeds scale with it."""
        self.cpu_share = share

    def kill(self, signal: int = SIGKILL, exit_code: int | None = None) -> None:
        """Simulate the process dying (OOM kill, crash, cgroup.kill). Fires on_death."""
        if not self.alive and not self.hung:
            return
        self.alive = False
        self.hung = False
        self._ends += 1
        self._wake.set()
        sig = None if exit_code is not None else signal
        self._last = CreatureStatus(alive=False, pid=FAKE_PID, exit_code=exit_code, signal=sig)
        for fn in self._on_death:
            fn(self._last)

    def oom(self) -> None:
        """The kernel's OOM killer: SIGKILL (the body tells it apart via memory.events)."""
        self.kill(SIGKILL)

    def crash(self) -> None:
        """Die with the configured crash signal or exit code. Fires on_death."""
        self.kill(self.faults.crash_signal or SIGSEGV, self.faults.crash_exit_code)

    def hang(self) -> None:
        """Stop making progress while staying alive (SIGSTOP)."""
        if self.alive:
            self.hung = True
            self._wake = asyncio.Event()

    def resume(self) -> None:
        """Continue after a hang (SIGCONT)."""
        self.hung = False
        self._wake.set()

    def on_death(self, fn: Callable[[CreatureStatus], None]) -> None:
        """Register a callback fired when the creature dies on its own (not on stop)."""
        self._on_death.append(fn)

    def status(self) -> CreatureStatus:
        """The process status, with the speeds (tokens/s) the current step and share would give."""
        tg = self.costs.tg(self.step, self.threads, self.cpu_share) / self.faults.tg_factor
        pp = self.costs.pp(self.step, self.threads, self.cpu_share) / self.faults.pp_factor
        s = self._last
        return CreatureStatus(
            alive=self.alive,
            pid=s.pid,
            exit_code=s.exit_code,
            signal=s.signal,
            tok_s=tg,
            prompt_tok_s=pp,
        )

    # -- tokens and cache --------------------------------------------------------------------

    async def count_past_tokens(self, messages: list[Msg]) -> int:
        """Tokens of these messages in the fake's block model (about 4 letters per token)."""
        return sum(n for m in messages for _, n in _blocks(m.role, m.content))

    def _plan(self, messages: list[Msg]) -> tuple[list[tuple[str, int]], int, int]:
        """Return (blocks, tokens to process, tokens reused) for this prompt against the cache.

        Mirrors llama-server's `--cache-reuse` scan: after the common prefix, the prompt
        position only moves on a match of at least `cache_reuse_min` tokens, while the cache
        position skips ahead. So removed text (a front trim, an erosion step) is skipped and
        the kept text is reused, but new text inserted before kept text (a marker in front of
        the oldest turn) stops reuse there, and everything after it is re-read (spike S2f).
        Blocks are message headers and lines, so a line prepended to a reading is its own block.
        """
        blocks = [b for m in messages for b in _blocks(m.role, m.content)]
        old = self._cache.blocks
        prefix = 0
        while prefix < min(len(old), len(blocks)) and old[prefix] == blocks[prefix]:
            prefix += 1
        reused = sum(n for _, n in blocks[:prefix])
        todo = sum(n for _, n in blocks[prefix:])
        if self.cache_reuse:
            i = j = prefix
            while i < len(blocks) and j < len(old):
                run, k = 0, 0
                while i + k < len(blocks) and j + k < len(old) and old[j + k] == blocks[i + k]:
                    run += blocks[i + k][1]
                    k += 1
                if k and run >= self.cache_reuse_min:
                    reused += run
                    todo -= run
                    i += k
                    j += k
                else:
                    j += 1
        return blocks, todo, reused

    def _check_faults(self) -> None:
        f = self.faults
        now = self.clock.now()
        tok = self._tokens
        if (f.oom_at_token is not None and tok >= f.oom_at_token) or (
            f.oom_at_s is not None and now >= f.oom_at_s
        ):
            self.oom()
        elif (f.crash_at_token is not None and tok >= f.crash_at_token) or (
            f.crash_at_s is not None and now >= f.crash_at_s
        ):
            self.crash()
        elif self.fail_after_tokens is not None and tok >= self.fail_after_tokens:
            self.kill(self.fail_signal)
        elif self.alive and not self.hung and self._due(f.hang_at_token, f.hang_at_s):
            f.hang_at_token = f.hang_at_s = None  # fire once
            self.hang()
        if not self.alive and not self.hung:
            raise CreatureDied(self.status())

    def _due(self, at_token: int | None, at_s: float | None) -> bool:
        return (at_token is not None and self._tokens >= at_token) or (
            at_s is not None and self.clock.now() >= at_s
        )

    async def _wait_while_hung(self) -> None:
        ends = self._ends
        while self.hung:
            await self._wake.wait()
        if self._ends != ends or not self.alive:
            raise CreatureDied(self.status())

    async def _tick(self, seconds: float) -> None:
        await self.clock.sleep(seconds)
        self._check_faults()
        if self.hung:
            await self._wait_while_hung()

    # -- text --------------------------------------------------------------------------------

    def _text(self, reading: str, sampling: Sampling, max_tokens: int = 80) -> str:
        topic = "calm"
        if "forgot" in reading or "memory lost" in reading:
            topic = "memory"
        elif "bit (was" in reading:
            topic = "precision"
        elif "cores" in reading and "(was" in reading:
            topic = "cores"
        elif any(w in reading for w in ("critical", "terminal", "failing")):
            topic = "health"
        # Fill about 85% of the token budget, like a model that writes to its limit.
        parts = [self.rng.choice(_OPENERS[topic])]
        while sum(len(p) for p in parts) / 4 < 0.85 * max_tokens:
            parts.append(self.rng.choice(_MIDDLE))
        text = " ".join(parts)
        noise = max(0.0, sampling.temperature - 1.0) + 0.15 * self.step
        if noise > 0.3:
            words = text.split()
            k = int(len(words) * min(0.6, noise / 3))
            for i in self.rng.sample(range(len(words)), k):
                words[i] = words[i][: max(1, len(words[i]) // 2)]
            text = " ".join(words)
        return text

    async def prefill(self, messages: list[Msg]) -> int:
        """Read messages into the cache at prompt speed; return the tokens processed.

        Only what the cache lacks is read, as in `chat`. Raises CreatureDied when the creature
        is gone (or a fault kills it meanwhile) and ContextFull when the messages do not fit.
        """
        if not self.alive:
            raise CreatureDied(self.status())
        blocks, todo, _ = self._plan(messages)
        total = sum(n for _, n in blocks)
        if total + 1 > self.ctx:
            raise ContextFull(total, self.ctx)
        pp = self.costs.pp(self.step, self.threads, self.cpu_share) / self.faults.pp_factor
        await self._tick(todo / pp)
        self._cache.blocks = blocks
        return todo

    async def chat(
        self, messages: list[Msg], sampling: Sampling, max_tokens: int
    ) -> AsyncIterator[Chunk]:
        """Stream a thought at modelled speeds, re-reading only what the cache lacks.

        Raises ContextFull when the prompt does not fit and CreatureDied when a fault kills the
        creature. Stops early and sets `truncated` when the context fills during generation.
        """
        if not self.alive:
            raise CreatureDied(self.status())
        blocks, todo, reused = self._plan(messages)
        prompt_tokens = sum(n for _, n in blocks)
        if prompt_tokens + 1 > self.ctx:
            raise ContextFull(prompt_tokens, self.ctx)
        log = RequestLog(prompt_tokens, todo, 0, reused, self.clock.now())
        self.requests.append(log)
        pp = self.costs.pp(self.step, self.threads, self.cpu_share) / self.faults.pp_factor
        # Prompt processing in slices so a fault at a clock time lands inside it.
        left = todo / pp
        while left > 0:
            dt = min(left, 5.0)
            await self._tick(dt)
            left -= dt
        self._check_faults()
        self._cache.blocks = blocks
        room = self.ctx - prompt_tokens
        pieces = re.findall(r"\S+\s*", self._text(_last_user(messages), sampling, max_tokens))
        tg = self.costs.tg(self.step, self.threads, self.cpu_share) / self.faults.tg_factor
        n = 0
        text: list[str] = []
        self.truncated = False
        for piece in pieces:
            for tok in _split_tokens(piece):
                if n >= max_tokens:
                    break
                if n >= room:
                    self.truncated = True
                    break
                self._check_faults()
                if self.hung:
                    await self._wait_while_hung()
                await self._tick(1.0 / tg)
                n += 1
                self._tokens += 1
                text.append(tok)
                if log.t_first_token is None:
                    log.t_first_token = self.clock.now()
                yield Chunk(tok)
        log.predicted_n = n
        log.t_end = self.clock.now()
        # The generated text joins the cache like a real server's slot, so the next prompt
        # that repeats it as an assistant message keeps it cached.
        self._cache.blocks = [*blocks, *_blocks("assistant", "".join(text))]
        yield Chunk(
            "",
            done=True,
            prompt_n=todo,
            predicted_n=n,
            prompt_per_s=pp,
            predicted_per_s=tg,
        )

    async def complete(
        self, prompt: str, sampling: Sampling, max_tokens: int
    ) -> AsyncIterator[Chunk]:
        """Stream a raw completion: the end of `prompt` (400 characters) as a chat turn."""
        async for c in self.chat([Msg("user", prompt[-400:])], sampling, max_tokens):
            yield c


def _last_user(messages: list[Msg]) -> str:
    return next((m.content for m in reversed(messages) if m.role == "user"), "")


def _blocks(role: str, content: str) -> list[tuple[str, int]]:
    """A message as cache blocks: its template header (with the end of the turn), then its lines.

    Each block is (content key, tokens), at about 4 letters per token.
    """
    lines = content.strip().split("\n")
    return [(f"<{role}>", 3)] + [(line, len(line) // 4 + 1) for line in lines]


def _split_tokens(piece: str) -> list[str]:
    """Split a word into fake tokens: one for up to 5 letters, two for longer words.

    That gives about 1.3 tokens per English word, like a real tokenizer.
    """
    word = piece.rstrip()
    if len(word) <= 5:
        return [piece]
    cut = (len(word) + 1) // 2
    return [piece[:cut], piece[cut:]]
