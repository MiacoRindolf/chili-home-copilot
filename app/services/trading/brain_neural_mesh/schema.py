"""Constants and mode helpers for the neural mesh."""

from __future__ import annotations

from typing import Literal

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
