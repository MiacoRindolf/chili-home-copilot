"""Graph-native speculative momentum engine (isolated from core repeatable-edge tiers)."""

from .engine import build_speculative_momentum_slice
from .schema import (
    ENGINE_CORE_REPEATABLE_EDGE,
    ENGINE_ID,
    ENGINE_VERSION,
    SPECULATIVE_GRAPH_NODE_IDS,
)

__all__ = [
    "ENGINE_CORE_REPEATABLE_EDGE",
    "ENGINE_ID",
    "ENGINE_VERSION",
    "SPECULATIVE_GRAPH_NODE_IDS",
    "build_speculative_momentum_slice",
]
