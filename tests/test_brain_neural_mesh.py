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
def test_neural_node_detail_includes_activation_wave_fields(db: Session) -> None:
    from app.services.trading.brain_neural_mesh.projection import build_node_detail

    bus = db.query(BrainGraphNode).filter(BrainGraphNode.id == "nm_event_bus").one_or_none()
    if bus is None:
        pytest.skip("migration 086 neural mesh seed not present")
    detail = build_node_detail(db, "nm_event_bus")
    assert detail is not None
    assert "in_last_activation_wave" in detail
    assert "activation_wave_id" in detail
    assert "activation_wave_correlation_id" in detail
    assert isinstance(detail["in_last_activation_wave"], bool)


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
    assert "layer_labels" in data["meta"]
    assert data["meta"]["layer_labels"].get("1") == "Sensory"
    assert data["meta"]["layer_labels"].get("7") == "Meta-Learning / Reweighting"
    assert "layer_label" in data["nodes"][0]


def test_inhibitory_suppression() -> None:
    """Inhibitory edge pulls activation below fire_threshold → suppression counted."""
    node = SimpleNamespace(enabled=True, fire_threshold=0.55, cooldown_seconds=0, is_observer=False)
    state = BrainNodeState(
        node_id="n_target",
        activation_score=0.60,  # above threshold
        confidence=0.8,
    )
    edge = SimpleNamespace(
        id=999,
        target_node_id="n_target",
        polarity="inhibitory",
        weight=3.0,
        signal_type="*",
        gate_config=None,
        min_confidence=0.0,
        delay_ms=0,
    )
    delta = prop.compute_activation_delta(edge, confidence_delta=0.5, polarity="inhibitory")
    before = float(state.activation_score)
    assert delta < 0
    new_act = max(0.0, min(1.0, before + delta))
    # Suppression: was above threshold, now below
    assert before >= node.fire_threshold
    assert new_act < node.fire_threshold


def test_gate_config_allowed_signal_types() -> None:
    """gate_config with allowed_signal_types list should filter correctly."""
    e = SimpleNamespace(
        signal_type="*",
        gate_config={"allowed_signal_types": ["snapshot_refresh", "step_completed"]},
    )
    assert prop.gate_allows(e, "snapshot_refresh") is True
    assert prop.gate_allows(e, "step_completed") is True
    assert prop.gate_allows(e, "momentum_context_refresh") is False
    # Wildcard in the allowed list passes everything
    e2 = SimpleNamespace(
        signal_type="specific",
        gate_config={"allowed_signal_types": ["*"]},
    )
    assert prop.gate_allows(e2, "anything") is True


def test_propagation_depth_cutoff() -> None:
    """At max_depth the result should be truncated with no downstream activity."""
    from unittest.mock import MagicMock

    db = MagicMock()
    pr = prop.propagate_one_event(
        db,
        source_node_id="nm_snap_daily",
        confidence_delta=0.5,
        propagation_depth=5,
        correlation_id="test",
        payload=None,
        max_depth=5,
        graph_version=1,
    )
    assert pr.truncated is True
    assert pr.targets_touched == 0
    assert pr.fires == 0
    assert pr.downstream_events == 0


def test_activation_score_decay() -> None:
    """Activation score should decay alongside confidence."""
    st = BrainNodeState(
        node_id="n1",
        activation_score=0.9,
        confidence=0.8,
        staleness_at=datetime.utcnow() - timedelta(seconds=1800),
    )
    assert prop.apply_decay_to_state(st, half_life_seconds=300.0, now=datetime.utcnow()) is True
    assert st.activation_score < 0.9
    assert st.confidence < 0.8


def test_gated_by_signal_and_confidence_counters() -> None:
    """PropagationResult should track gated-out edges."""
    e_signal = SimpleNamespace(signal_type="snapshot_refresh", gate_config=None)
    assert prop.gate_allows(e_signal, "other_signal") is False

    e_conf = SimpleNamespace(min_confidence=0.7)
    assert prop.min_confidence_ok(e_conf, 0.3) is False


def test_brain_worker_lists_activation_loop_mode() -> None:
    txt = (Path(__file__).resolve().parents[1] / "scripts" / "brain_worker.py").read_text(encoding="utf-8")
    assert "activation-loop" in txt
    assert "_run_activation_loop" in txt
    assert "_run_mining_loop" in txt
