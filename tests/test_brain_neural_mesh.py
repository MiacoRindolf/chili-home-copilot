"""Trading Brain neural mesh: propagation helpers, DB queue, API shape."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.trading import BrainActivationEvent, BrainGraphNode, BrainNodeState
from app.services.trading.brain_neural_mesh import propagation as prop
from app.services.trading.brain_neural_mesh import repository as repo
from app.services.trading.brain_neural_mesh.activation_runner import run_activation_batch
from app.services.trading.brain_neural_mesh.projection import build_neural_graph_projection


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


def test_compute_activation_delta_preserves_zero_edge_weight() -> None:
    e = SimpleNamespace(weight=0.0)

    assert prop.compute_activation_delta(
        e,
        confidence_delta=0.5,
        polarity="excitatory",
    ) == pytest.approx(0.0)
    assert prop.compute_activation_delta(
        e,
        confidence_delta=0.5,
        polarity="inhibitory",
    ) == pytest.approx(0.0)


def test_decay_reduces_confidence() -> None:
    st = BrainNodeState(
        node_id="n1",
        activation_score=0.5,
        confidence=0.8,
        last_activated_at=datetime.utcnow() - timedelta(seconds=900),
    )
    assert prop.apply_decay_to_state(st, half_life_seconds=300.0, now=datetime.utcnow()) is True
    assert st.confidence < 0.8


def test_projection_stale_and_cooling_accepts_naive_utc_timestamps() -> None:
    """Postgres/SQLAlchemy often returns naive UTC; projection uses aware ``now``."""
    from app.services.trading.brain_neural_mesh.projection import _node_cooling, _node_stale_flag

    now = datetime.now(timezone.utc)
    naive_recent = (now - timedelta(seconds=30)).replace(tzinfo=None)
    assert _node_stale_flag(naive_recent, now=now, stale_after_sec=480.0) is False
    assert _node_cooling(naive_recent, 60, now=now) is True


def test_should_fire_accepts_naive_utc_last_fired_with_aware_now() -> None:
    """DB DateTime columns often return naive UTC; cooldown math stays UTC-safe."""
    now = datetime.now(timezone.utc)
    node = SimpleNamespace(enabled=True, fire_threshold=0.50, cooldown_seconds=60)
    state = BrainNodeState(
        node_id="n_fire",
        activation_score=0.80,
        confidence=0.9,
        last_fired_at=(now - timedelta(seconds=90)).replace(tzinfo=None),
    )

    assert prop.should_fire(node, state, now) is True


def test_should_fire_respects_cooldown_for_naive_utc_last_fired() -> None:
    now = datetime.now(timezone.utc)
    node = SimpleNamespace(enabled=True, fire_threshold=0.50, cooldown_seconds=60)
    state = BrainNodeState(
        node_id="n_cooldown",
        activation_score=0.80,
        confidence=0.9,
        last_fired_at=(now - timedelta(seconds=30)).replace(tzinfo=None),
    )

    assert prop.should_fire(node, state, now) is False


def test_critical_mesh_alert_cooldown_setting_drives_suppression(monkeypatch) -> None:
    from app.services.trading.brain_neural_mesh import action_handlers

    signature = "trade-1|TEST|STOP_HIT|89.25|88.00"
    now = datetime.now(timezone.utc)
    action_handlers._LAST_CRITICAL_DISPATCH_AT.clear()
    action_handlers._LAST_CRITICAL_DISPATCH_AT[signature] = now - timedelta(seconds=30)

    try:
        monkeypatch.setenv("CHILI_MESH_CRITICAL_ALERT_COOLDOWN_SECONDS", "45")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        monkeypatch.setattr("app.config.settings", settings)

        assert settings.chili_mesh_critical_alert_cooldown_seconds == 45
        assert action_handlers._critical_dispatch_in_cooldown(signature, now) is True

        monkeypatch.setenv("CHILI_MESH_CRITICAL_ALERT_COOLDOWN_SECONDS", "20")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        monkeypatch.setattr("app.config.settings", settings)

        assert settings.chili_mesh_critical_alert_cooldown_seconds == 20
        assert action_handlers._critical_dispatch_in_cooldown(signature, now) is False
    finally:
        action_handlers._LAST_CRITICAL_DISPATCH_AT.pop(signature, None)


def test_brain_work_outcome_publish_rolls_back_swallowed_db_error(monkeypatch) -> None:
    """Publisher helpers must not return a DB-error-poisoned SQLAlchemy session.

    A mid-statement disconnect surfaces as a SQLAlchemy ``OperationalError``; the
    publisher must roll back so the caller's next query doesn't cascade with
    ``PendingRollbackError``. (A non-DB publish failure leaves the transaction
    healthy and must NOT trigger a rollback — see
    ``tests/test_session_rollback_on_disconnect.py``.)
    """
    from sqlalchemy.exc import OperationalError

    from app.services.trading.brain_neural_mesh import publisher

    class FakeSession:
        def __init__(self) -> None:
            self.rollbacks = 0

        def rollback(self) -> None:
            self.rollbacks += 1

    def broken_enqueue(*_args, **_kwargs) -> int:
        raise OperationalError(
            "INSERT INTO brain_activation_events ...",
            {},
            Exception("server closed the connection unexpectedly"),
        )

    fake_db = FakeSession()
    monkeypatch.setattr(publisher, "mesh_enabled", lambda: True)
    monkeypatch.setattr(publisher, "enqueue_activation", broken_enqueue)

    publisher.publish_brain_work_outcome(
        fake_db,
        outcome_type="backtest_completed",
        scan_pattern_id=537,
        extra={"work_event_id": 7569},
    )

    assert fake_db.rollbacks == 1


def test_activation_batch_throttles_global_decay(monkeypatch) -> None:
    from app.services.trading.brain_neural_mesh import activation_runner

    activation_runner._reset_activation_runner_for_tests()
    decay_calls: list[int] = []

    monkeypatch.setattr(
        activation_runner,
        "apply_global_decay",
        lambda _db, *, graph_version: decay_calls.append(graph_version) or 0,
    )
    monkeypatch.setattr(
        activation_runner.repo,
        "claim_pending_batch",
        lambda _db, *, limit: [],
    )
    monkeypatch.setattr(activation_runner, "reap_dead_events", lambda _db: 0)
    monkeypatch.setattr(
        activation_runner,
        "maybe_flush_metrics",
        lambda _db, *, graph_version: None,
    )

    first = activation_runner.run_activation_batch(
        object(),
        time_budget_sec=0.05,
        max_events=1,
        run_decay=True,
    )
    second = activation_runner.run_activation_batch(
        object(),
        time_budget_sec=0.05,
        max_events=1,
        run_decay=True,
    )

    assert len(decay_calls) == 1
    assert first["decay_ran"] is True
    assert second["decay_ran"] is False
    assert "elapsed_sec" in first


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


@pytest.mark.parametrize("protected_cause", ["brain_market_snapshots", "momentum_context_refresh"])
def test_protected_refresh_sheds_stale_imminent_eval_under_queue_pressure(
    db: Session,
    monkeypatch,
    protected_cause: str,
) -> None:
    db.query(BrainActivationEvent).delete()
    old = _naive_utcnow() - timedelta(hours=2)
    monkeypatch.setattr(repo, "MAX_PENDING_QUEUE_DEPTH", 3)
    monkeypatch.setattr(repo, "QUEUE_PRESSURE_SHED_MIN_AGE_SECONDS", 60)
    db.add_all([
        BrainActivationEvent(
            source_node_id=None,
            cause="imminent_eval",
            payload={"signal_type": "imminent_eval"},
            correlation_id=f"old-{idx}",
            status="pending",
            created_at=old + timedelta(seconds=idx),
        )
        for idx in range(3)
    ])
    db.flush()

    event_id = repo.enqueue_activation(
        db,
        source_node_id="nm_event_bus",
        cause=protected_cause,
        payload={"signal_type": protected_cause},
        correlation_id=f"protected-refresh-{protected_cause}",
    )

    assert event_id > 0
    assert repo.pending_queue_depth(db) == 3
    assert (
        db.query(BrainActivationEvent)
        .filter(BrainActivationEvent.status == "pending")
        .filter(BrainActivationEvent.cause == "imminent_eval")
        .count()
    ) == 2
    assert (
        db.query(BrainActivationEvent)
        .filter(BrainActivationEvent.status == "pending")
        .filter(BrainActivationEvent.cause == protected_cause)
        .count()
    ) == 1
    shed = (
        db.query(BrainActivationEvent)
        .filter(BrainActivationEvent.status == "dead")
        .filter(BrainActivationEvent.cause == "imminent_eval")
        .one()
    )
    assert shed.processed_at is not None
    assert (shed.payload or {}).get("_queue_pressure_shed", {}).get("shed_for_cause") == protected_cause


def test_queue_pressure_does_not_shed_for_unprotected_causes(
    db: Session,
    monkeypatch,
) -> None:
    db.query(BrainActivationEvent).delete()
    old = _naive_utcnow() - timedelta(hours=2)
    monkeypatch.setattr(repo, "MAX_PENDING_QUEUE_DEPTH", 2)
    monkeypatch.setattr(repo, "QUEUE_PRESSURE_SHED_MIN_AGE_SECONDS", 60)
    db.add_all([
        BrainActivationEvent(
            source_node_id=None,
            cause="imminent_eval",
            payload={"signal_type": "imminent_eval"},
            correlation_id=f"old-unprotected-{idx}",
            status="pending",
            created_at=old + timedelta(seconds=idx),
        )
        for idx in range(2)
    ])
    db.flush()

    event_id = repo.enqueue_activation(
        db,
        source_node_id="nm_other",
        cause="setup_vitals_change",
        payload={"signal_type": "setup_vitals_change"},
        correlation_id="unprotected",
    )

    assert event_id == -1
    assert repo.pending_queue_depth(db) == 2
    assert (
        db.query(BrainActivationEvent)
        .filter(BrainActivationEvent.cause == "imminent_eval")
        .count()
    ) == 2


def test_protected_refresh_keeps_fresh_imminent_eval_when_queue_full(
    db: Session,
    monkeypatch,
) -> None:
    db.query(BrainActivationEvent).delete()
    monkeypatch.setattr(repo, "MAX_PENDING_QUEUE_DEPTH", 2)
    monkeypatch.setattr(repo, "QUEUE_PRESSURE_SHED_MIN_AGE_SECONDS", 3600)
    db.add_all([
        BrainActivationEvent(
            source_node_id=None,
            cause="imminent_eval",
            payload={"signal_type": "imminent_eval"},
            correlation_id=f"fresh-{idx}",
            status="pending",
            created_at=_naive_utcnow(),
        )
        for idx in range(2)
    ])
    db.flush()

    event_id = repo.enqueue_activation(
        db,
        source_node_id="nm_event_bus",
        cause="momentum_context_refresh",
        payload={"signal_type": "momentum_context_refresh"},
        correlation_id="fresh-protected",
    )

    assert event_id == -1
    assert repo.pending_queue_depth(db) == 2
    assert (
        db.query(BrainActivationEvent)
        .filter(BrainActivationEvent.cause == "imminent_eval")
        .count()
    ) == 2


def test_queue_pressure_does_not_shed_when_correlation_cap_is_exhausted(
    db: Session,
    monkeypatch,
) -> None:
    db.query(BrainActivationEvent).delete()
    old = _naive_utcnow() - timedelta(hours=2)
    monkeypatch.setattr(repo, "MAX_PENDING_QUEUE_DEPTH", 2)
    monkeypatch.setattr(repo, "MAX_EVENTS_PER_CORRELATION", 2)
    monkeypatch.setattr(repo, "QUEUE_PRESSURE_SHED_MIN_AGE_SECONDS", 60)
    db.add_all([
        BrainActivationEvent(
            source_node_id=None,
            cause="imminent_eval",
            payload={"signal_type": "imminent_eval"},
            correlation_id="exhausted-correlation",
            status="pending",
            created_at=old + timedelta(seconds=idx),
        )
        for idx in range(2)
    ])
    db.flush()

    event_id = repo.enqueue_activation(
        db,
        source_node_id="nm_event_bus",
        cause="momentum_context_refresh",
        payload={"signal_type": "momentum_context_refresh"},
        correlation_id="exhausted-correlation",
    )

    assert event_id == -1
    assert repo.pending_queue_depth(db) == 2
    assert (
        db.query(BrainActivationEvent)
        .filter(BrainActivationEvent.status == "dead")
        .count()
    ) == 0


def test_queue_pressure_does_not_shed_when_queue_is_over_cap(
    db: Session,
    monkeypatch,
) -> None:
    db.query(BrainActivationEvent).delete()
    old = _naive_utcnow() - timedelta(hours=2)
    monkeypatch.setattr(repo, "MAX_PENDING_QUEUE_DEPTH", 2)
    monkeypatch.setattr(repo, "QUEUE_PRESSURE_SHED_MIN_AGE_SECONDS", 60)
    db.add_all([
        BrainActivationEvent(
            source_node_id=None,
            cause="imminent_eval",
            payload={"signal_type": "imminent_eval"},
            correlation_id=f"over-cap-{idx}",
            status="pending",
            created_at=old + timedelta(seconds=idx),
        )
        for idx in range(3)
    ])
    db.flush()

    event_id = repo.enqueue_activation(
        db,
        source_node_id="nm_event_bus",
        cause="momentum_context_refresh",
        payload={"signal_type": "momentum_context_refresh"},
        correlation_id="over-cap-protected",
    )

    assert event_id == -1
    assert repo.pending_queue_depth(db) == 3
    assert (
        db.query(BrainActivationEvent)
        .filter(BrainActivationEvent.status == "dead")
        .count()
    ) == 0


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
        last_activated_at=datetime.utcnow() - timedelta(seconds=1800),
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
