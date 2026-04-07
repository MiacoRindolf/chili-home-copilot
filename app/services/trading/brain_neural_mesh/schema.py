"""Constants and mode helpers for the neural mesh."""

from __future__ import annotations

from typing import Any, Literal

from ....config import settings

LOG_PREFIX = "[brain_neural_mesh]"
DEFAULT_DOMAIN = "trading"
DEFAULT_GRAPH_VERSION = 1

GraphMode = Literal["legacy", "neural"]


def effective_graph_mode() -> GraphMode:
    """Server-side policy: UI should not guess legacy vs neural."""
    if not getattr(settings, "trading_brain_neural_mesh_enabled", False):
        return "legacy"
    raw = (getattr(settings, "trading_brain_graph_mode", None) or "neural").strip().lower()
    if raw in ("legacy", "old", "pipeline"):
        return "legacy"
    return "neural"


def mesh_enabled() -> bool:
    return bool(getattr(settings, "trading_brain_neural_mesh_enabled", False))


def desk_graph_boot_config() -> dict[str, Any]:
    """Single server-side truth for Trading Brain desk graph boot (SSR + /graph/config)."""
    eff = effective_graph_mode()
    mesh = mesh_enabled()
    setting = getattr(settings, "trading_brain_graph_mode", "neural")
    # Desk never silently substitutes legacy JSON when neural is the effective policy.
    silent_legacy = not (mesh and eff == "neural")
    return {
        "mesh_enabled": mesh,
        "trading_brain_neural_mesh_enabled": mesh,
        "effective_graph_mode": eff,
        "trading_brain_graph_mode_setting": setting,
        "desk_boot": "api",
        "recommended_graph_url": "/api/trading/brain/graph",
        "legacy_graph_url": "/api/brain/trading/network-graph",
        "silent_legacy_fallback": silent_legacy,
    }
