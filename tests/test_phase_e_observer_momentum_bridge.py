"""Phase 2E: observer feedback + momentum-neural bridge (migration 156)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models.trading import BrainGraphEdge, BrainGraphNode


@pytest.mark.usefixtures("db")
def test_observers_no_longer_write_only(db: Session) -> None:
    if db.query(BrainGraphNode).count() == 0:
        pytest.skip("neural mesh not seeded")
    for obs_id in ("nm_observer_journal", "nm_observer_playbook"):
        node = db.query(BrainGraphNode).filter(BrainGraphNode.id == obs_id).one_or_none()
        if node is None:
            continue
        assert node.is_observer is False, f"{obs_id} should no longer be write-only"


@pytest.mark.usefixtures("db")
def test_observer_feedback_edges_to_journal_cluster(db: Session) -> None:
    if db.query(BrainGraphNode).count() == 0:
        pytest.skip("neural mesh not seeded")
    for src in ("nm_observer_journal", "nm_observer_playbook"):
        e = db.query(BrainGraphEdge).filter(
            BrainGraphEdge.source_node_id == src,
            BrainGraphEdge.target_node_id == "nm_lc_c_journal",
            BrainGraphEdge.signal_type == "observer_note",
        ).one_or_none()
        assert e is not None, f"observer feedback edge from {src} missing"
        assert e.edge_type == "feedback"


@pytest.mark.usefixtures("db")
def test_momentum_bridge_edges_to_main_spine(db: Session) -> None:
    if db.query(BrainGraphNode).count() == 0:
        pytest.skip("neural mesh not seeded")
    for tgt, etype in (("nm_momentum", "dataflow"), ("nm_regime", "evidence")):
        e = db.query(BrainGraphEdge).filter(
            BrainGraphEdge.source_node_id == "nm_momentum_crypto_intel",
            BrainGraphEdge.target_node_id == tgt,
            BrainGraphEdge.signal_type == "momentum_context_refresh",
        ).one_or_none()
        assert e is not None, f"momentum bridge edge to {tgt} missing"
        assert e.edge_type == etype
