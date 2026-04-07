"""Trading Brain neural mesh: propagation helpers, DB queue, API shape."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.models.trading import BrainGraphNode, BrainNodeState
from app.services.trading.brain_neural_mesh import propagation as prop
from app.services.trading.brain_neural_mesh import repository as repo
from app.services.trading.brain_neural_mesh.activation_runner import run_activation_batch
from app.services.trading.brain_neural_mesh.projection import build_neural_graph_projection


def test_gate_allows_wildcard_and_specific() -> None:
    e = SimpleNamespace(signal_type="snapshot_refresh", gate_config=None)
    assert prop.gate_allows(e, "snapshot_refresh") is True
    assert prop.gate_allows(e, "*") is True
    assert prop.gate_allows(e, "other") is False


def test_compute_activation_delta_polarity() -> None:
    e = SimpleNamespace(weight=1.0)
    ex = prop.compute_activation_delta(e, confidence_delta=0.5, polarity="excitatory")
    inh = prop.compute_activation_delta(e, confidence_delta=0.5, polarity="inhibitory")
    assert ex > 0
    assert inh < 0


def test_decay_reduces_confidence() -> None:
    st = BrainNodeState(
        node_id="n1",
        activation_score=0.5,
        confidence=0.8,
        staleness_at=datetime.utcnow() - timedelta(seconds=900),
    )
    assert prop.apply_decay_to_state(st, half_life_seconds=300.0, now=datetime.utcnow()) is True
    assert st.confidence < 0.8


@pytest.mark.usefixtures("db")
def test_enqueue_and_activation_batch(db: Session) -> None:
    src = db.query(BrainGraphNode).filter(BrainGraphNode.id == "nm_snap_daily").one_or_none()
    if src is None:
        pytest.skip("migration 086 neural mesh seed not present")
    repo.enqueue_activation(
        db,
        source_node_id="nm_snap_daily",
        cause="test",
        payload={"signal_type": "snapshot_refresh"},
        confidence_delta=0.9,
        propagation_depth=0,
        correlation_id="test-corr-1",
    )
    db.commit()
    out = run_activation_batch(db, time_budget_sec=3.0, max_events=10, run_decay=False)
    db.commit()
    assert out.get("processed", 0) >= 1


@pytest.mark.usefixtures("db")
def test_neural_projection_shape(db: Session) -> None:
    if db.query(BrainGraphNode).count() == 0:
        pytest.skip("no mesh nodes")
    data = build_neural_graph_projection(db)
    assert data["ok"] is True
    assert data["meta"]["view"] == "neural"
    assert len(data["nodes"]) >= 10
    assert len(data["edges"]) >= 5
    n0 = data["nodes"][0]
    assert "activation_score" in n0
    assert "layer" in n0
    e0 = data["edges"][0]
    assert e0.get("polarity") in ("excitatory", "inhibitory")


def test_brain_worker_lists_activation_loop_mode() -> None:
    txt = (Path(__file__).resolve().parents[1] / "scripts" / "brain_worker.py").read_text(encoding="utf-8")
    assert "activation-loop" in txt
    assert "_run_activation_loop" in txt
    assert "_run_mining_loop" in txt
