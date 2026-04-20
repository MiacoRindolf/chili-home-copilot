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

# ── Phase 2A: Operational clusters (layer 8, node_type='operational_cluster') ──
# Group the L1-L7 operational spine into named clusters for audit/gating symmetry
# with the L8 learning-cycle clusters. Source-of-truth for projection + tests;
# mirrors migration 152. Changes here must be reflected in a new migration.

OPERATIONAL_CLUSTER_NODE_IDS: tuple[str, ...] = (
    "nm_c_sensing",
    "nm_c_features",
    "nm_c_market_state",
    "nm_c_pattern_inference",
    "nm_c_evidence",
    "nm_c_decision",
    "nm_c_reactive_sensors",
    "nm_c_meta_ops",
    # Phase 2B: portfolio + exit axis
    "nm_c_portfolio",
    "nm_c_exit_execution",
)

# Canonical (cluster_id, label, member_node_ids). Membership is declarative;
# missing member nodes in a given DB are silently skipped by migrations 152/153.
OPERATIONAL_CLUSTERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("nm_c_sensing", "Sensing", (
        "nm_snap_daily", "nm_snap_intraday", "nm_snap_crypto", "nm_universe_scan",
        "nm_venue_truth_coinbase", "nm_venue_truth_robinhood",
    )),
    ("nm_c_features", "Feature Extraction", (
        "nm_volatility", "nm_momentum", "nm_anomaly",
        "nm_liquidity_state", "nm_breadth_state", "nm_intermarket_state",
    )),
    ("nm_c_market_state", "Latent Market State", (
        "nm_event_bus", "nm_working_memory", "nm_regime", "nm_contradiction",
        "nm_active_thesis_state", "nm_confidence_accumulator",
        "nm_memory_freshness", "nm_exec_liquidity_regime",
    )),
    ("nm_c_pattern_inference", "Pattern Inference", (
        "nm_pattern_disc", "nm_similarity", "nm_trade_context",
    )),
    ("nm_c_evidence", "Evidence & Verification", (
        "nm_evidence_bt", "nm_evidence_replay", "nm_evidence_quality",
        "nm_counterfactual_challenger", "nm_contradiction_verifier",
        "nm_exec_spread_quality",
    )),
    ("nm_c_decision", "Decision & Expression", (
        "nm_action_signals", "nm_action_alerts", "nm_risk_gate",
        "nm_sizing_policy", "nm_exec_readiness_gate",
        "nm_observer_journal", "nm_observer_playbook",
    )),
    ("nm_c_reactive_sensors", "Reactive Sensors", (
        "nm_stop_eval", "nm_pattern_health", "nm_imminent_eval",
    )),
    ("nm_c_meta_ops", "Operational Meta", (
        "nm_meta_reweight", "nm_meta_decay",
        "nm_threshold_tuner", "nm_promotion_demotion_monitor",
    )),
    ("nm_c_portfolio", "Portfolio State", (
        "nm_portfolio_state", "nm_exposure_heat", "nm_trade_lifecycle_hub",
        "nm_pending_orders", "nm_pdt_state",
    )),
    ("nm_c_exit_execution", "Exit Execution", (
        "nm_exit_policy", "nm_trail_engine", "nm_target_engine", "nm_exit_trigger",
    )),
)


def operational_cluster_for_node(node_id: str) -> str | None:
    """Return the operational_cluster_id owning ``node_id``, else None."""
    for cid, _label, members in OPERATIONAL_CLUSTERS:
        if node_id in members:
            return cid
    return None
