"""Structural sanity for the Trading Brain learning-cycle architecture (no DB)."""

from __future__ import annotations

from app.services.trading.learning_cycle_architecture import (
    TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS,
)
from app.services.trading.brain_neural_mesh.seed_graph import (
    LEARNING_CYCLE_CLUSTER_IDS,
    LEARNING_CYCLE_STEP_IDS,
    VENUE_NODE_IDS,
    EXECUTION_CONTEXT_NODE_IDS,
)


def test_learning_cycle_cluster_split_has_no_c_meta() -> None:
    """c_meta has been split into c_meta_learning, c_decisioning, c_control."""
    cluster_ids = {c.id for c in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS}
    assert "c_meta" not in cluster_ids
    assert "c_meta_learning" in cluster_ids
    assert "c_decisioning" in cluster_ids
    assert "c_control" in cluster_ids


def test_seed_graph_cluster_ids_match_architecture() -> None:
    """Seed graph cluster IDs must match learning_cycle_architecture (minus c_universe)."""
    arch_cluster_ids = {c.id for c in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS if c.id != "c_universe"}
    seed_cluster_ids = {nid.replace("nm_lc_", "") for nid in LEARNING_CYCLE_CLUSTER_IDS}
    assert arch_cluster_ids == seed_cluster_ids


def test_seed_graph_step_ids_cover_architecture() -> None:
    """Every step sid in learning_cycle_architecture should have a corresponding nm_lc_ node."""
    arch_step_sids = set()
    for c in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS:
        if c.id == "c_universe":
            continue
        for s in c.steps:
            arch_step_sids.add(s.sid)
    seed_step_sids = {nid.replace("nm_lc_", "") for nid in LEARNING_CYCLE_STEP_IDS}
    assert arch_step_sids == seed_step_sids


def test_venue_and_execution_node_ids_defined() -> None:
    """Provider truth and execution context nodes must be defined."""
    assert "nm_venue_truth_coinbase" in VENUE_NODE_IDS
    assert "nm_venue_truth_robinhood" in VENUE_NODE_IDS
    assert len(EXECUTION_CONTEXT_NODE_IDS) >= 3
