"""Seed metadata mirrored from migration 086 (for tests / introspection)."""

from __future__ import annotations

DEFAULT_GRAPH_VERSION = 1
DEFAULT_DOMAIN = "trading"

# Core spine node ids (subset — see migration for full list)
CORE_SPINE_NODE_IDS: tuple[str, ...] = (
    "nm_snap_daily",
    "nm_event_bus",
    "nm_volatility",
    "nm_regime",
    "nm_pattern_disc",
    "nm_evidence_bt",
    "nm_action_signals",
)

INHIBITORY_EDGE = ("nm_contradiction", "nm_action_signals", "contradict")
