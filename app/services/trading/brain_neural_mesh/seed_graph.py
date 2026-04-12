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

# Learning-cycle cluster nodes (layer 8)
LEARNING_CYCLE_CLUSTER_IDS: tuple[str, ...] = (
    "nm_lc_c_state",
    "nm_lc_c_discovery",
    "nm_lc_c_validation",
    "nm_lc_c_evolution",
    "nm_lc_c_secondary_structure",
    "nm_lc_c_secondary_outcomes",
    "nm_lc_c_secondary_signals",
    "nm_lc_c_journal",
    "nm_lc_c_meta_learning",
    "nm_lc_c_decisioning",
    "nm_lc_c_control",
)

# Learning-cycle step nodes (layer 9)
LEARNING_CYCLE_STEP_IDS: tuple[str, ...] = (
    "nm_lc_snapshots_daily",
    "nm_lc_snapshots_intraday",
    "nm_lc_backfill",
    "nm_lc_decay",
    "nm_lc_mine",
    "nm_lc_seek",
    "nm_lc_bt_insights",
    "nm_lc_bt_queue",
    "nm_lc_variants",
    "nm_lc_hypotheses",
    "nm_lc_breakout",
    "nm_lc_intraday_hv",
    "nm_lc_refine",
    "nm_lc_exit",
    "nm_lc_fakeout",
    "nm_lc_sizing",
    "nm_lc_inter_alert",
    "nm_lc_timeframe",
    "nm_lc_synergy",
    "nm_lc_journal",
    "nm_lc_signals",
    "nm_lc_ml",
    "nm_lc_pattern_engine",
    "nm_lc_proposals",
    "nm_lc_cycle_report",
    "nm_lc_depromote",
    "nm_lc_finalize",
)

# Combined for convenience
LEARNING_CYCLE_NODE_IDS: tuple[str, ...] = LEARNING_CYCLE_CLUSTER_IDS + LEARNING_CYCLE_STEP_IDS

# Execution context / provider truth nodes
VENUE_NODE_IDS: tuple[str, ...] = (
    "nm_venue_truth_coinbase",
    "nm_venue_truth_robinhood",
)

EXECUTION_CONTEXT_NODE_IDS: tuple[str, ...] = (
    "nm_exec_liquidity_regime",
    "nm_exec_spread_quality",
    "nm_exec_readiness_gate",
)
