"""The backend contract: the creature process and how the controller talks to it (BUILD_PLAN 6.4).

Implemented in llama_server.py and fake.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Protocol

from epitaph.types import Chunk, CreatureStatus, ModelSpec, Msg, Sampling


class CreatureDied(RuntimeError):
    """The creature process is gone (OOM, crash, kill). Raised from a stream or a call."""

    def __init__(self, status: CreatureStatus) -> None:
        """Keep the dead process's exit status as `self.status`."""
        super().__init__(f"creature died: exit={status.exit_code} signal={status.signal}")
        self.status = status


class ContextFull(RuntimeError):
    """The prompt no longer fits the context (`unbounded` dies of this: cause=full)."""

    def __init__(self, tokens: int, ctx: int) -> None:
        """`tokens` is the prompt size and `ctx` the context size, both in tokens."""
        super().__init__(f"context full: {tokens} tokens > ctx {ctx}")
        self.tokens = tokens
        self.ctx = ctx


class BackendError(RuntimeError):
    """The server answered with an error, or a stream broke while the process lives on."""


class Backend(Protocol):
    """A running creature. start() spawns it; chat() streams one thought."""

    async def start(self, model: ModelSpec, quant: str, threads: int) -> None:
        """Spawn the creature with this model, quant and thread count; return once it serves.

        Raises CreatureDied if the process exits while loading.
        """
        ...

    async def stop(self, hard: bool = False) -> None:
        """Stop the creature (SIGTERM, or SIGKILL when `hard`); a deliberate stop is not a death."""
        ...

    def chat(
        self, messages: list[Msg], sampling: Sampling, max_tokens: int
    ) -> AsyncIterator[Chunk]:
        """Stream a thought. The final chunk has done=True and carries the timings."""
        ...

    def complete(self, prompt: str, sampling: Sampling, max_tokens: int) -> AsyncIterator[Chunk]:
        """Stream a raw completion of `prompt` (diary mode); the final chunk is as in chat()."""
        ...

    async def prefill(self, messages: list[Msg]) -> int:
        """Read `messages` into the prompt cache without showing anything; return tokens read.

        The controller calls it right after `start`, during the birth card or a reload
        silence, with the system prompt, so the first thought only has to read its reading
        (spike S1b: the system prompt alone is 37-107 s of prompt processing on the Pi 4).
        Raises CreatureDied if the process is gone.
        """
        ...

    async def count_past_tokens(self, messages: list[Msg]) -> int:
        """Tokens of these messages on the rendered chat template (BUILD_PLAN 5.4)."""
        ...

    def on_death(self, fn: Callable[[CreatureStatus], None]) -> None:
        """Register a callback fired when the process exits, even between requests."""
        ...

    def status(self) -> CreatureStatus:
        """The process status and the speeds (tokens/s) of the last finished request."""
        ...
