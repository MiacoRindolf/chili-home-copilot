"""Constants and mode helpers for the neural mesh."""

from __future__ import annotations

from typing import Any

LOG_PREFIX = "[brain_neural_mesh]"
DEFAULT_DOMAIN = "trading"
DEFAULT_GRAPH_VERSION = 1


def effective_graph_mode() -> str:
    """Neural mesh is the only graph mode. Legacy mode has been removed."""
    return "neural"


def mesh_enabled() -> bool:
    """Neural mesh is always enabled (unified graph)."""
    return True


def desk_graph_boot_config() -> dict[str, Any]:
    """Single server-side truth for Trading Brain desk graph boot (SSR + /graph/config)."""
    return {
        "mesh_enabled": True,
        "trading_brain_neural_mesh_enabled": True,
        "effective_graph_mode": "neural",
        "desk_boot": "api",
        "recommended_graph_url": "/api/trading/brain/graph",
        # Phase 10: neural momentum desk read-model (intel / viability / evolution summaries).
        "momentum_neural_desk_url": "/api/trading/brain/momentum/desk",
    }
