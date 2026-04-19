"""Phase 2C: Hebbian plasticity engine + migration 155."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.trading import BrainGraphNode
from app.services.trading.brain_neural_mesh import plasticity as pl


# ── Pure-unit tests for compute_plasticity_delta ─────────────────────────────


def test_delta_is_zero_in_noise_band() -> None:
    d, new_w = pl.compute_plasticity_delta(
        pnl_r=0.05, edge_weight=1.0, edge_type="dataflow",
        source_confidence=0.8, target_confidence=0.8, learning_rate=0.05,
    )
    assert d == 0.0
    assert new_w == 1.0


def test_winning_trade_increases_weight() -> None:
    d, new_w = pl.compute_plasticity_delta(
        pnl_r=1.0, edge_weight=1.0, edge_type="dataflow",
        source_confidence=0.8, target_confidence=0.8, learning_rate=0.05,
    )
    assert d > 0
    assert new_w > 1.0


def test_losing_trade_decreases_weight() -> None:
    d, new_w = pl.compute_plasticity_delta(
        pnl_r=-0.8, edge_weight=1.0, edge_type="dataflow",
        source_confidence=0.8, target_confidence=0.8, learning_rate=0.05,
    )
    assert d < 0
    assert new_w < 1.0


def test_evidence_edge_learns_at_higher_rate() -> None:
    _, w_dataflow = pl.compute_plasticity_delta(
        pnl_r=1.0, edge_weight=1.0, edge_type="dataflow",
        source_confidence=0.8, target_confidence=0.8, learning_rate=0.05,
    )
    _, w_evidence = pl.compute_plasticity_delta(
        pnl_r=1.0, edge_weight=1.0, edge_type="evidence",
        source_confidence=0.8, target_confidence=0.8, learning_rate=0.05,
    )
    _, w_veto = pl.compute_plasticity_delta(
        pnl_r=1.0, edge_weight=1.0, edge_type="veto",
        source_confidence=0.8, target_confidence=0.8, learning_rate=0.05,
    )
    assert w_evidence > w_dataflow
    assert w_veto > w_dataflow


def test_operator_output_edges_do_not_learn() -> None:
    d, new_w = pl.compute_plasticity_delta(
        pnl_r=2.0, edge_weight=1.0, edge_type="operator_output",
        source_confidence=1.0, target_confidence=1.0, learning_rate=0.5,
    )
    assert d == 0.0
    assert new_w == 1.0


def test_weight_clamp_respects_bounds() -> None:
    # Winning floor is MIN_WEIGHT; saturating upside is MAX_WEIGHT
    _, low = pl.compute_plasticity_delta(
        pnl_r=-2.0, edge_weight=0.10, edge_type="veto",
        source_confidence=1.0, target_confidence=1.0, learning_rate=0.5,
    )
    assert low >= pl.MIN_WEIGHT
    _, high = pl.compute_plasticity_delta(
        pnl_r=2.0, edge_weight=2.95, edge_type="veto",
        source_confidence=1.0, target_confidence=1.0, learning_rate=0.5,
    )
    assert high <= pl.MAX_WEIGHT


def test_pnl_magnitude_clipped_at_2r() -> None:
    """An extreme pnl_r event should not move the weight more than a 2R event."""
    _, w_2r = pl.compute_plasticity_delta(
        pnl_r=2.0, edge_weight=1.0, edge_type="dataflow",
        source_confidence=1.0, target_confidence=1.0, learning_rate=0.05,
    )
    _, w_huge = pl.compute_plasticity_delta(
        pnl_r=50.0, edge_weight=1.0, edge_type="dataflow",
        source_confidence=1.0, target_confidence=1.0, learning_rate=0.05,
    )
    assert w_2r == w_huge


def test_low_confidence_attenuates_update() -> None:
    _, w_high = pl.compute_plasticity_delta(
        pnl_r=1.0, edge_weight=1.0, edge_type="dataflow",
        source_confidence=1.0, target_confidence=1.0, learning_rate=0.05,
    )
    _, w_low = pl.compute_plasticity_delta(
        pnl_r=1.0, edge_weight=1.0, edge_type="dataflow",
        source_confidence=0.2, target_confidence=0.2, learning_rate=0.05,
    )
    assert (w_low - 1.0) < (w_high - 1.0)


# ── DB integration ───────────────────────────────────────────────────────────


@pytest.mark.usefixtures("db")
def test_migration_155_creates_plasticity_tables_and_nodes(db: Session) -> None:
    tables_rows = db.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )).fetchall()
    tables = {r[0] for r in tables_rows}
    assert "brain_graph_edge_mutations" in tables
    assert "brain_activation_path_log" in tables

    if db.query(BrainGraphNode).count() == 0:
        pytest.skip("neural mesh not seeded")
    for nid in ("nm_plasticity_engine", "nm_plasticity_budget"):
        node = db.query(BrainGraphNode).filter(BrainGraphNode.id == nid).one_or_none()
        assert node is not None, f"{nid} missing"
        assert int(node.layer) == 7
        dmeta = node.display_meta if isinstance(node.display_meta, dict) else {}
        assert dmeta.get("operational_cluster_id") == "nm_c_meta_ops"


@pytest.mark.usefixtures("db")
def test_apply_outcome_plasticity_noop_when_disabled(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(pl.settings, "chili_mesh_plasticity_enabled", False, raising=False)
    out = pl.apply_outcome_plasticity(
        db, trade_id=1, pnl=50.0, risked_capital=100.0, correlation_id="any",
    )
    assert out["proposed"] == 0
    assert out["applied"] == 0
    # No skipped counters should move either when the whole engine is gated off.
    assert sum(v for k, v in out.items() if k.startswith("skipped_")) == 0


@pytest.mark.usefixtures("db")
def test_apply_outcome_plasticity_empty_path_is_noop(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(pl.settings, "chili_mesh_plasticity_enabled", True, raising=False)
    out = pl.apply_outcome_plasticity(
        db, trade_id=999, pnl=50.0, risked_capital=100.0,
        correlation_id="nonexistent-corr-id",
    )
    assert out["proposed"] == 0
    assert out["applied"] == 0


def test_compute_risked_capital_requires_stop() -> None:
    trade_without_stop = SimpleNamespace(
        entry_price=100.0, quantity=10.0, stop_loss=None,
    )
    assert pl.compute_risked_capital(trade_without_stop) == 0.0

    trade_with_stop = SimpleNamespace(
        entry_price=100.0, quantity=10.0, stop_loss=95.0,
    )
    # 1R = |100 - 95| * 10 = 50
    assert pl.compute_risked_capital(trade_with_stop) == pytest.approx(50.0)


def test_handle_trade_close_plasticity_requires_closed_status() -> None:
    open_trade = SimpleNamespace(
        id=1, status="open", pnl=25.0,
        entry_price=100.0, quantity=10.0, stop_loss=95.0,
        mesh_entry_correlation_id="abc",
    )
    out = pl.handle_trade_close_plasticity(None, open_trade)
    assert out["applied"] == 0
    assert out["proposed"] == 0


def test_handle_trade_close_plasticity_no_mesh_correlation_is_noop() -> None:
    closed_no_corr = SimpleNamespace(
        id=1, status="closed", pnl=25.0,
        entry_price=100.0, quantity=10.0, stop_loss=95.0,
        mesh_entry_correlation_id=None,
    )
    out = pl.handle_trade_close_plasticity(None, closed_no_corr)
    assert out["applied"] == 0


def test_handle_trade_close_plasticity_no_stop_loss_is_noop() -> None:
    closed_no_stop = SimpleNamespace(
        id=1, status="closed", pnl=25.0,
        entry_price=100.0, quantity=10.0, stop_loss=None,
        mesh_entry_correlation_id="abc",
    )
    out = pl.handle_trade_close_plasticity(None, closed_no_stop)
    assert out["applied"] == 0


@pytest.mark.usefixtures("db")
def test_migration_158_seeds_edge_weight_baseline(db: Session) -> None:
    tables_rows = db.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )).fetchall()
    tables = {r[0] for r in tables_rows}
    assert "brain_graph_edge_weight_baseline" in tables

    if db.query(BrainGraphNode).count() == 0:
        pytest.skip("mesh not seeded")
    # Every enabled edge should have a baseline row.
    bad = db.execute(text(
        """
        SELECT COUNT(*) FROM brain_graph_edges e
        WHERE e.enabled = TRUE
          AND NOT EXISTS (
            SELECT 1 FROM brain_graph_edge_weight_baseline b WHERE b.edge_id = e.id
          )
        """
    )).scalar()
    assert int(bad or 0) == 0


def test_config_defaults_are_live() -> None:
    """Sanity: flags must be live (enabled=True, dry_run=False) per user flip."""
    from app.config import settings as _s
    assert _s.chili_mesh_plasticity_enabled is True
    assert _s.chili_mesh_plasticity_dry_run is False


@pytest.mark.usefixtures("db")
def test_path_logging_only_active_when_plasticity_enabled(db: Session, monkeypatch) -> None:
    """Propagation writes path_log only when the feature flag is on."""
    from app.services.trading.brain_neural_mesh import propagation as prop
    from app.services.trading.brain_neural_mesh import repository as repo
    from app.services.trading.brain_neural_mesh.activation_runner import run_activation_batch

    if db.query(BrainGraphNode).filter(BrainGraphNode.id == "nm_snap_daily").count() == 0:
        pytest.skip("mesh seed missing")

    # Flag OFF → no rows
    monkeypatch.setattr(prop.settings, "chili_mesh_plasticity_enabled", False, raising=False)
    db.execute(text("DELETE FROM brain_activation_path_log WHERE correlation_id = :c"),
               {"c": "plas-off-test"})
    db.commit()
    repo.enqueue_activation(
        db, source_node_id="nm_snap_daily", cause="test",
        payload={"signal_type": "snapshot_refresh"},
        confidence_delta=0.9, propagation_depth=0, correlation_id="plas-off-test",
    )
    db.commit()
    run_activation_batch(db, time_budget_sec=3.0, max_events=10, run_decay=False)
    db.commit()
    n_off = db.execute(
        text("SELECT COUNT(*) FROM brain_activation_path_log WHERE correlation_id = :c"),
        {"c": "plas-off-test"},
    ).scalar()
    assert int(n_off or 0) == 0
