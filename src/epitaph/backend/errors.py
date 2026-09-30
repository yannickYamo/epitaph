"""Compatibility re-export: `ContextFull` and `BackendError` now live in `backend/base.py`.

Import them from `epitaph.backend.base`; this module stays so older imports keep working.
"""

from __future__ import annotations

from epitaph.backend.base import BackendError, ContextFull

__all__ = ["BackendError", "ContextFull"]
