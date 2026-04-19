"""Phase 2A: operational cluster registry + projection + migration 152."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models.trading import BrainGraphEdge, BrainGraphNode
from app.services.trading.brain_neural_mesh.projection import (
    NEURAL_PROJECTION_SCHEMA_VERSION,
    build_neural_graph_projection,
    operational_clusters_registry,
)
from app.services.trading.brain_neural_mesh.seed_graph import (
    OPERATIONAL_CLUSTER_NODE_IDS,
    OPERATIONAL_CLUSTERS,
    operational_cluster_for_node,
)


PHASE_A_CLUSTERS = frozenset({
    "nm_c_sensing", "nm_c_features", "nm_c_market_state", "nm_c_pattern_inference",
    "nm_c_evidence", "nm_c_decision", "nm_c_reactive_sensors", "nm_c_meta_ops",
})


def test_registry_ids_match_node_ids() -> None:
    ids = {c[0] for c in OPERATIONAL_CLUSTERS}
    assert ids == set(OPERATIONAL_CLUSTER_NODE_IDS)


def test_registry_has_all_phase_a_clusters() -> None:
    ids = {c[0] for c in OPERATIONAL_CLUSTERS}
    assert PHASE_A_CLUSTERS.issubset(ids)


def test_registry_membership_is_disjoint() -> None:
    """A node belongs to at most one operational cluster."""
    seen: dict[str, str] = {}
    for cid, _label, members in OPERATIONAL_CLUSTERS:
        for mid in members:
            assert mid not in seen, f"{mid} assigned to {seen[mid]} and {cid}"
            seen[mid] = cid


def test_operational_cluster_for_node_resolves_known_members() -> None:
    assert operational_cluster_for_node("nm_event_bus") == "nm_c_market_state"
    assert operational_cluster_for_node("nm_action_signals") == "nm_c_decision"
    assert operational_cluster_for_node("nm_stop_eval") == "nm_c_reactive_sensors"
    assert operational_cluster_for_node("nm_venue_truth_coinbase") == "nm_c_sensing"


def test_operational_cluster_for_unknown_node_returns_none() -> None:
    assert operational_cluster_for_node("nm_not_a_real_node") is None
    # Cluster nodes themselves are not members of any cluster.
    assert operational_cluster_for_node("nm_c_sensing") is None


def test_registry_projection_shape() -> None:
    reg = operational_clusters_registry()
    assert len(reg) >= len(PHASE_A_CLUSTERS)
    first = reg[0]
    assert set(first.keys()) == {"cluster_id", "label", "member_node_ids"}
    assert isinstance(first["member_node_ids"], list)
    assert first["cluster_id"].startswith("nm_c_")


def test_projection_schema_version_bumped() -> None:
    """Bumped to 5 in Phase 2A when operational_cluster_id + registry landed."""
    assert NEURAL_PROJECTION_SCHEMA_VERSION >= 5


@pytest.mark.usefixtures("db")
def test_migration_152_creates_operational_cluster_nodes(db: Session) -> None:
    if db.query(BrainGraphNode).count() == 0:
        pytest.skip("neural mesh not seeded in this test DB")
    for cid in OPERATIONAL_CLUSTER_NODE_IDS:
        node = db.query(BrainGraphNode).filter(BrainGraphNode.id == cid).one_or_none()
        assert node is not None, f"operational cluster node {cid} missing after migration"
        assert node.node_type == "operational_cluster"
        assert int(node.layer) == 8
        # Effectively never fires on its own (membership-level signal only)
        assert float(node.fire_threshold) >= 0.95


@pytest.mark.usefixtures("db")
def test_migration_152_stamps_member_cluster_ids(db: Session) -> None:
    if db.query(BrainGraphNode).count() == 0:
        pytest.skip("neural mesh not seeded in this test DB")
    # Spot-check a few members from different clusters
    for mid, expected_cid in (
        ("nm_event_bus", "nm_c_market_state"),
        ("nm_action_signals", "nm_c_decision"),
        ("nm_evidence_bt", "nm_c_evidence"),
    ):
        node = db.query(BrainGraphNode).filter(BrainGraphNode.id == mid).one_or_none()
        if node is None:
            continue
        dmeta = node.display_meta if isinstance(node.display_meta, dict) else {}
        assert dmeta.get("operational_cluster_id") == expected_cid, (
            f"{mid} should be stamped with {expected_cid}, got {dmeta.get('operational_cluster_id')}"
        )


@pytest.mark.usefixtures("db")
def test_migration_152_creates_structural_edges(db: Session) -> None:
    if db.query(BrainGraphNode).count() == 0:
        pytest.skip("neural mesh not seeded in this test DB")
    # Every roll_up edge has a matching wake edge (bidirectional structural pair)
    roll_ups = db.query(BrainGraphEdge).filter(
        BrainGraphEdge.signal_type == "cluster_roll_up"
    ).all()
    wakes = db.query(BrainGraphEdge).filter(
        BrainGraphEdge.signal_type == "cluster_wake"
    ).all()
    if not roll_ups:
        pytest.skip("migration 152 structural edges not present (no members in this DB)")
    assert len(roll_ups) == len(wakes), "every roll_up must have a matching wake edge"
    for e in roll_ups:
        assert e.edge_type == "control"
        assert e.polarity == "excitatory"
        assert e.target_node_id in OPERATIONAL_CLUSTER_NODE_IDS


@pytest.mark.usefixtures("db")
def test_projection_surfaces_operational_cluster_info(db: Session) -> None:
    if db.query(BrainGraphNode).count() == 0:
        pytest.skip("neural mesh not seeded in this test DB")
    data = build_neural_graph_projection(db)
    assert data["ok"] is True
    assert "operational_clusters" in data["meta"]
    assert len(data["meta"]["operational_clusters"]) >= len(PHASE_A_CLUSTERS)
    # Each node payload carries the operational_cluster_id key (None for unclustered nodes)
    assert all("operational_cluster_id" in n for n in data["nodes"])
    # At least some nodes are stamped (when the membership matches what's in DB)
    stamped = [n for n in data["nodes"] if n.get("operational_cluster_id")]
    assert len(stamped) >= 1
