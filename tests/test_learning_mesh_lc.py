"""Learning-cycle ↔ neural mesh alignment (indices, cluster completion map)."""

from __future__ import annotations

from app.services.trading.brain_neural_mesh import publisher as mesh_pub
from app.services.trading.brain_neural_mesh.projection import mesh_lc_indices_for_node_id
from app.services.trading.learning_cycle_architecture import (
    SCHEDULER_ONLY_LEARNING_CYCLE_CLUSTER_ID,
    TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS,
)


def test_mesh_lc_indices_match_status_enumeration() -> None:
    """Same cluster index as ``_set_cycle_graph_node_fields`` / ``apply_learning_cycle_step_status``."""
    for ci, cluster in enumerate(TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS):
        if cluster.id == SCHEDULER_ONLY_LEARNING_CYCLE_CLUSTER_ID:
            continue
        assert mesh_lc_indices_for_node_id(f"nm_lc_{cluster.id}") == (ci, -1)
        for si, st in enumerate(cluster.steps):
            assert mesh_lc_indices_for_node_id(f"nm_lc_{st.sid}") == (ci, si)


def test_mesh_lc_indices_unknown() -> None:
    assert mesh_lc_indices_for_node_id("nm_event_bus") == (-1, -1)
    assert mesh_lc_indices_for_node_id("nm_lc_not_a_real_node") == (-1, -1)


def test_publisher_cluster_last_step_matches_architecture() -> None:
    """``_CLUSTER_LAST_STEP`` must match the last step sid per cluster (excl. scheduler-only)."""
    for cluster in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS:
        if cluster.id == SCHEDULER_ONLY_LEARNING_CYCLE_CLUSTER_ID:
            assert cluster.id not in mesh_pub._CLUSTER_LAST_STEP
            continue
        expected_last = cluster.steps[-1].sid
        assert mesh_pub._CLUSTER_LAST_STEP.get(cluster.id) == expected_last, cluster.id


def test_notify_learning_cycle_step_committed_calls_publish(monkeypatch) -> None:
    called: list[tuple[str, str]] = []

    def _stub(db, *, cluster_id: str, step_sid: str, elapsed_sec: float, extra: str = "", correlation_id=None):
        called.append((cluster_id, step_sid))

    monkeypatch.setattr(mesh_pub, "publish_learning_step_completed", _stub)
    mesh_pub.notify_learning_cycle_step_committed(
        None,  # db unused by stub
        cluster_id="c_state",
        step_sid="backfill",
        elapsed_sec=1.23,
        extra="x",
        correlation_id="corr",
    )
    assert called == [("c_state", "backfill")]
