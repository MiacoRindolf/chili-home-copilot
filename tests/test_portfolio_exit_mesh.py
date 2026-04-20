"""Phase 2B: portfolio + exit execution sub-graph (migration 153)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models.trading import BrainGraphEdge, BrainGraphNode
from app.services.trading.brain_neural_mesh import publisher as mesh_pub
from app.services.trading.brain_neural_mesh.seed_graph import (
    OPERATIONAL_CLUSTERS,
    operational_cluster_for_node,
)


PORTFOLIO_MEMBERS = (
    "nm_portfolio_state", "nm_exposure_heat", "nm_trade_lifecycle_hub",
    "nm_pending_orders", "nm_pdt_state",
)
EXIT_MEMBERS = (
    "nm_exit_policy", "nm_trail_engine", "nm_target_engine", "nm_exit_trigger",
)


def test_registry_includes_portfolio_and_exit_clusters() -> None:
    ids = {c[0] for c in OPERATIONAL_CLUSTERS}
    assert "nm_c_portfolio" in ids
    assert "nm_c_exit_execution" in ids


def test_registry_memberships_resolve() -> None:
    for mid in PORTFOLIO_MEMBERS:
        assert operational_cluster_for_node(mid) == "nm_c_portfolio"
    for mid in EXIT_MEMBERS:
        assert operational_cluster_for_node(mid) == "nm_c_exit_execution"


def test_publishers_exist_and_are_callable() -> None:
    """Helpers are importable and no-op safely when mesh is disabled."""
    assert callable(mesh_pub.publish_trade_lifecycle)
    assert callable(mesh_pub.publish_exposure_update)
    assert callable(mesh_pub.publish_exit_decision)


def test_publishers_noop_when_mesh_disabled(monkeypatch) -> None:
    """When mesh_enabled is False, publishers must not call enqueue_activation."""
    called: list[str] = []

    def _mark_enqueue(*args, **kwargs):
        called.append("enqueue")

    monkeypatch.setattr(mesh_pub, "enqueue_activation", _mark_enqueue)
    monkeypatch.setattr(mesh_pub, "mesh_enabled", lambda: False)

    mesh_pub.publish_trade_lifecycle(
        None, trade_id=1, ticker="AAPL", transition="entry",
    )
    mesh_pub.publish_exposure_update(None, heat_score=0.9, over_limit=True)
    mesh_pub.publish_exit_decision(
        None, trade_id=1, ticker="AAPL", reason="stop_hit", source="exit_trigger",
    )
    assert called == []


def test_publish_exposure_update_routes_over_limit_to_heat_node(monkeypatch) -> None:
    """over_limit=True publishes from nm_exposure_heat; False from nm_portfolio_state."""
    captured: list[dict] = []

    def _capture_enqueue(db, *, source_node_id, cause, payload, confidence_delta,
                        propagation_depth, correlation_id):
        captured.append({
            "source": source_node_id,
            "cause": cause,
            "payload": payload,
            "delta": confidence_delta,
        })
        return 1

    monkeypatch.setattr(mesh_pub, "enqueue_activation", _capture_enqueue)
    monkeypatch.setattr(mesh_pub, "mesh_enabled", lambda: True)

    mesh_pub.publish_exposure_update(None, heat_score=0.92, over_limit=True)
    mesh_pub.publish_exposure_update(None, heat_score=0.30, over_limit=False)

    assert len(captured) == 2
    assert captured[0]["source"] == "nm_exposure_heat"
    assert captured[0]["payload"]["signal_type"] == "exposure_exceeded"
    assert captured[0]["delta"] > captured[1]["delta"]
    assert captured[1]["source"] == "nm_portfolio_state"
    assert captured[1]["payload"]["signal_type"] == "portfolio_update"


def test_publish_exit_decision_maps_source_to_node(monkeypatch) -> None:
    captured: list[str] = []

    def _capture_enqueue(db, *, source_node_id, **kwargs):
        captured.append(source_node_id)
        return 1

    monkeypatch.setattr(mesh_pub, "enqueue_activation", _capture_enqueue)
    monkeypatch.setattr(mesh_pub, "mesh_enabled", lambda: True)

    for src, expected in (
        ("trail_engine", "nm_trail_engine"),
        ("target_engine", "nm_target_engine"),
        ("exit_trigger", "nm_exit_trigger"),
        ("unknown_source", "nm_exit_trigger"),  # default
    ):
        mesh_pub.publish_exit_decision(
            None, trade_id=1, ticker="AAPL", reason="x", source=src,
        )
    assert captured == [
        "nm_trail_engine", "nm_target_engine", "nm_exit_trigger", "nm_exit_trigger",
    ]


def test_publish_trade_lifecycle_close_has_higher_delta_than_entry(monkeypatch) -> None:
    captured: list[float] = []

    def _capture_enqueue(db, *, confidence_delta, **kwargs):
        captured.append(float(confidence_delta))
        return 1

    monkeypatch.setattr(mesh_pub, "enqueue_activation", _capture_enqueue)
    monkeypatch.setattr(mesh_pub, "mesh_enabled", lambda: True)

    mesh_pub.publish_trade_lifecycle(None, trade_id=1, ticker="X", transition="entry")
    mesh_pub.publish_trade_lifecycle(None, trade_id=1, ticker="X", transition="close", pnl=50.0)
    assert len(captured) == 2
    assert captured[1] > captured[0]


@pytest.mark.usefixtures("db")
def test_migration_153_creates_all_portfolio_and_exit_nodes(db: Session) -> None:
    if db.query(BrainGraphNode).count() == 0:
        pytest.skip("neural mesh not seeded")
    for cid in ("nm_c_portfolio", "nm_c_exit_execution"):
        node = db.query(BrainGraphNode).filter(BrainGraphNode.id == cid).one_or_none()
        assert node is not None, f"cluster {cid} missing"
        assert node.node_type == "operational_cluster"
        assert int(node.layer) == 8

    for mid in PORTFOLIO_MEMBERS + EXIT_MEMBERS:
        node = db.query(BrainGraphNode).filter(BrainGraphNode.id == mid).one_or_none()
        assert node is not None, f"member {mid} missing"
        dmeta = node.display_meta if isinstance(node.display_meta, dict) else {}
        assert dmeta.get("operational_cluster_id") in ("nm_c_portfolio", "nm_c_exit_execution")


@pytest.mark.usefixtures("db")
def test_migration_153_creates_behavioral_veto_edges(db: Session) -> None:
    if db.query(BrainGraphNode).count() == 0:
        pytest.skip("neural mesh not seeded")
    # Key behavioral edges: exposure_heat → risk_gate veto and pdt → risk_gate veto
    for (src, tgt, sig) in (
        ("nm_exposure_heat", "nm_risk_gate", "exposure_exceeded"),
        ("nm_pdt_state", "nm_risk_gate", "pdt_blocked"),
    ):
        e = db.query(BrainGraphEdge).filter(
            BrainGraphEdge.source_node_id == src,
            BrainGraphEdge.target_node_id == tgt,
            BrainGraphEdge.signal_type == sig,
        ).one_or_none()
        assert e is not None, f"veto edge {src} -> {tgt} missing"
        assert e.polarity == "inhibitory"
        assert e.edge_type == "veto"


@pytest.mark.usefixtures("db")
def test_migration_153_creates_exit_chain_edges(db: Session) -> None:
    if db.query(BrainGraphNode).count() == 0:
        pytest.skip("neural mesh not seeded")
    required_chain = (
        ("nm_stop_eval", "nm_exit_policy", "stop_eval"),
        ("nm_exit_policy", "nm_trail_engine", "exit_policy"),
        ("nm_exit_policy", "nm_target_engine", "exit_policy"),
        ("nm_trail_engine", "nm_exit_trigger", "trail_move"),
        ("nm_target_engine", "nm_exit_trigger", "target_hit"),
        ("nm_exit_trigger", "nm_action_signals", "exit_intent"),
    )
    for src, tgt, sig in required_chain:
        e = db.query(BrainGraphEdge).filter(
            BrainGraphEdge.source_node_id == src,
            BrainGraphEdge.target_node_id == tgt,
            BrainGraphEdge.signal_type == sig,
        ).one_or_none()
        assert e is not None, f"exit chain edge {src} -> {tgt} ({sig}) missing"
