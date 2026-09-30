"""A fake creature for tests and the simulator (BUILD_PLAN 9 A4).

It streams canned first-person text at the speeds of a real machine on the injected clock,
reacts to what changed in the last reading, degrades with precision and temperature, and
can be told to die (OOM, crash, hang). Minimal in phase 0a; part A extends it.
"""

from __future__ import annotations

import random
import re
from collections.abc import AsyncIterator, Callable

from epitaph.backend.base import CreatureDied
from epitaph.clock import FakeClock
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


class FakeBackend:
    """Deterministic, clock-driven stand-in for llama-server."""

    def __init__(self, clock: FakeClock, costs: Costs, seed: int = 0) -> None:
        self.clock = clock
        self.costs = costs
        self.rng = random.Random(seed)
        self.alive = False
        self.step = 0
        self.threads = 3
        self.cpu_share = 3.0
        self.fail_after_tokens: int | None = None
        self.fail_signal = 9
        self._on_death: list[Callable[[CreatureStatus], None]] = []
        self._tokens = 0

    async def start(self, model: ModelSpec, quant: str, threads: int) -> None:
        self.step = list(model.ladder).index(quant) if quant in model.ladder else 0
        self.threads = threads
        self.cpu_share = float(threads)
        await self.clock.sleep(self.costs.load(self.step))
        self.alive = True

    async def stop(self, hard: bool = False) -> None:
        self.alive = False

    def set_cpu_share(self, share: float) -> None:
        self.cpu_share = share

    def kill(self, signal: int = 9) -> None:
        """Simulate the process dying (OOM kill, crash)."""
        if not self.alive:
            return
        self.alive = False
        status = CreatureStatus(alive=False, exit_code=None, signal=signal)
        for fn in self._on_death:
            fn(status)

    def on_death(self, fn: Callable[[CreatureStatus], None]) -> None:
        self._on_death.append(fn)

    def status(self) -> CreatureStatus:
        return CreatureStatus(
            alive=self.alive, tok_s=self.costs.tg(self.step, self.threads, self.cpu_share)
        )

    async def count_past_tokens(self, messages: list[Msg]) -> int:
        return sum(len(m.content) // 4 + 4 for m in messages)

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

    async def chat(
        self, messages: list[Msg], sampling: Sampling, max_tokens: int
    ) -> AsyncIterator[Chunk]:
        if not self.alive:
            raise CreatureDied(self.status())
        reading = next((m.content for m in reversed(messages) if m.role == "user"), "")
        prompt_tokens = await self.count_past_tokens(messages[-1:])
        await self.clock.sleep(
            prompt_tokens / self.costs.pp(self.step, self.threads, self.cpu_share)
        )
        pieces = re.findall(r"\S+\s*", self._text(reading, sampling, max_tokens))
        tg = self.costs.tg(self.step, self.threads, self.cpu_share)
        n = 0
        for piece in pieces:
            for tok in _split_tokens(piece):
                if n >= max_tokens:
                    break
                if not self.alive:
                    raise CreatureDied(CreatureStatus(alive=False, signal=self.fail_signal))
                if self.fail_after_tokens is not None and self._tokens >= self.fail_after_tokens:
                    self.kill(self.fail_signal)
                    raise CreatureDied(CreatureStatus(alive=False, signal=self.fail_signal))
                await self.clock.sleep(1.0 / tg)
                n += 1
                self._tokens += 1
                yield Chunk(tok)
        yield Chunk("", done=True, prompt_n=prompt_tokens, predicted_n=n, predicted_per_s=tg)

    async def complete(
        self, prompt: str, sampling: Sampling, max_tokens: int
    ) -> AsyncIterator[Chunk]:
        async for c in self.chat([Msg("user", prompt[-400:])], sampling, max_tokens):
            yield c


def _split_tokens(piece: str) -> list[str]:
    """Roughly 4 letters per token, like a real tokenizer."""
    return [piece[i : i + 4] for i in range(0, len(piece), 4)] or [piece]
