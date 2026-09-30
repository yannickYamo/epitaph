"""Backend errors beyond the contract's CreatureDied (proposed for base.py, CONTRACT_CHANGES #1)."""

from __future__ import annotations


class ContextFull(RuntimeError):
    """The prompt no longer fits the context (`unbounded` dies of this: cause=full)."""

    def __init__(self, tokens: int, ctx: int) -> None:
        """`tokens` is the prompt size and `ctx` the context size, both in tokens."""
        super().__init__(f"context full: {tokens} tokens > ctx {ctx}")
        self.tokens = tokens
        self.ctx = ctx


class BackendError(RuntimeError):
    """The server answered with an error, or a stream broke while the process lives on."""
