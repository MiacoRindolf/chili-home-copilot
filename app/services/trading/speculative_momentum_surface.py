"""Backward-compatible re-exports for the speculative momentum engine.

Prefer importing from ``app.services.trading.speculative_momentum_engine``.
"""

from __future__ import annotations

from .speculative_momentum_engine import build_speculative_momentum_slice
from .speculative_momentum_engine.schema import ENGINE_CORE_REPEATABLE_EDGE, ENGINE_ID

ENGINE_SPECULATIVE_MOMENTUM = ENGINE_ID

__all__ = [
    "ENGINE_CORE_REPEATABLE_EDGE",
    "ENGINE_SPECULATIVE_MOMENTUM",
    "build_speculative_momentum_slice",
]
