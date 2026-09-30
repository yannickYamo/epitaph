"""Backend errors beyond the contract's CreatureDied (proposed for base.py, CONTRACT_CHANGES #1)."""

from __future__ import annotations


class ContextFull(RuntimeError):
    """The prompt no longer fits the context (`unbounded` dies of this: cause=full)."""

    def __init__(self, tokens: int, ctx: int) -> None:
        super().__init__(f"context full: {tokens} tokens > ctx {ctx}")
        self.tokens = tokens
        self.ctx = ctx
