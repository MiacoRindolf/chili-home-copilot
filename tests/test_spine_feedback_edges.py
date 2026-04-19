"""Phase 2D: spine -> learning-cycle feedback edges (migration 154)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models.trading import BrainGraphEdge, BrainGraphNode


REQUIRED_EDGES = (
    ("nm_trade_lifecycle_hub", "nm_lc_c_meta_learning", "trade_lifecycle"),
    ("nm_exposure_heat", "nm_lc_c_control", "exposure_exceeded"),
    ("nm_lc_c_decisioning", "nm_meta_reweight", "cluster_completed"),
)


@pytest.mark.usefixtures("db")
def test_migration_154_creates_feedback_edges(db: Session) -> None:
    if db.query(BrainGraphNode).count() == 0:
        pytest.skip("neural mesh not seeded")
    for src, tgt, sig in REQUIRED_EDGES:
        e = db.query(BrainGraphEdge).filter(
            BrainGraphEdge.source_node_id == src,
            BrainGraphEdge.target_node_id == tgt,
            BrainGraphEdge.signal_type == sig,
        ).one_or_none()
        assert e is not None, f"feedback edge {src} -> {tgt} missing"
        assert e.edge_type == "feedback"
        assert e.polarity == "excitatory"
        assert float(e.weight) == pytest.approx(0.50, abs=0.01)
