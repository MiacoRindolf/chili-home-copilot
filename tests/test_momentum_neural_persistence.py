"""Phase 2: neural momentum durable tables (PostgreSQL + migrations)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.trading import (
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationRuntimeSnapshot,
    TradingAutomationSessionBinding,
    TradingAutomationSimulatedFill,
)
from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.persistence import (
    append_trading_automation_event,
    append_trading_automation_simulated_fill,
    build_runtime_snapshot_values,
    create_trading_automation_session,
    default_session_binding,
    ensure_momentum_strategy_variants,
    persist_neural_momentum_tick,
    upsert_trading_automation_runtime_snapshot,
    upsert_trading_automation_session_binding,
)
from app.services.trading.momentum_neural.viability import score_viability
from app.services.trading.momentum_neural.variants import get_family


def test_models_and_migration_tables_exist(db: Session) -> None:
    ensure_momentum_strategy_variants(db)
    db.commit()
    n = db.query(MomentumStrategyVariant).count()
    assert n >= 10


def test_ensure_variants_idempotent(db: Session) -> None:
    ensure_momentum_strategy_variants(db)
    db.commit()
    a = db.query(MomentumStrategyVariant).count()
    ensure_momentum_strategy_variants(db)
    db.commit()
    b = db.query(MomentumStrategyVariant).count()
    assert a == b


def test_viability_upsert_updates_row(db: Session) -> None:
    ensure_momentum_strategy_variants(db)
    db.commit()
    fam = get_family("impulse_breakout")
    assert fam is not None
    ctx = build_momentum_regime_context(
        now=datetime(2026, 4, 7, 16, 0, tzinfo=timezone.utc),
        atr_pct=0.02,
        meta={"spread_regime": "normal"},
    )
    feats = ExecutionReadinessFeatures(spread_bps=5.0)
    vr = score_viability("SOL-USD", fam, ctx, feats)
    row = vr.to_public_dict()
    row["label"] = fam.label
    row["entry_style"] = fam.entry_style
    row["default_stop_logic"] = fam.default_stop_logic
    row["default_exit_logic"] = fam.default_exit_logic

    n1 = persist_neural_momentum_tick(
        db,
        row_dicts=[row],
        regime_snapshot=ctx.to_public_dict(),
        features=feats,
        correlation_id="corr-a",
        source_node_id="nm_momentum_crypto_intel",
    )
    assert n1 == 1
    db.commit()

    r1 = db.query(MomentumSymbolViability).filter(MomentumSymbolViability.symbol == "SOL-USD").one()
    id1 = r1.id
    assert r1.correlation_id == "corr-a"
    assert r1.source_node_id == "nm_momentum_crypto_intel"
    assert r1.paper_eligible is True
    assert r1.execution_readiness_json.get("spread_bps") == 5.0

    row2 = dict(row)
    row2["viability"] = 0.99
    persist_neural_momentum_tick(
        db,
        row_dicts=[row2],
        regime_snapshot=ctx.to_public_dict(),
        features=feats,
        correlation_id="corr-b",
        source_node_id="nm_momentum_crypto_intel",
    )
    db.commit()

    r2 = db.query(MomentumSymbolViability).filter(MomentumSymbolViability.symbol == "SOL-USD").one()
    assert r2.id == id1
    assert r2.viability_score == 0.99
    assert r2.correlation_id == "corr-b"
    assert "rationale" in (r2.explain_json or {})


def test_automation_session_and_event(db: Session) -> None:
    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()
    sess = create_trading_automation_session(
        db,
        symbol="BTC-USD",
        variant_id=v.id,
        state="idle",
        correlation_id="sess-corr",
        source_node_id="nm_test",
    )
    db.commit()
    ev = append_trading_automation_event(
        db,
        sess.id,
        "session_created",
        {"hello": True},
        correlation_id="evt-corr",
        source_node_id="nm_test",
    )
    db.commit()
    assert ev.session_id == sess.id
    assert ev.correlation_id == "evt-corr"
    assert ev.source_node_id == "nm_test"


def test_runtime_snapshot_binding_and_fill_tables(db: Session) -> None:
    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()
    sess = create_trading_automation_session(
        db,
        symbol="ETH-USD",
        variant_id=v.id,
        state="queued",
        mode="paper",
        correlation_id="rt-corr",
        source_node_id="nm_test",
        risk_snapshot_json={"momentum_risk": {"allowed": True}},
    )
    values = build_runtime_snapshot_values(sess, variant=v, trade_count=0)
    upsert_trading_automation_runtime_snapshot(db, session_id=sess.id, values=values)
    upsert_trading_automation_session_binding(
        db,
        session_id=sess.id,
        values=default_session_binding(
            venue=sess.venue,
            mode=sess.mode,
            execution_family=sess.execution_family,
            quote_source="massive",
        ),
    )
    append_trading_automation_simulated_fill(
        db,
        session_id=sess.id,
        symbol=sess.symbol,
        lane="simulation",
        action="enter_long",
        fill_type="entry",
        quantity=1.25,
        price=2500.0,
        reference_price=2498.0,
        fees_usd=1.0,
        position_state_before="flat",
        position_state_after="long",
        reason="test_entry",
    )
    db.commit()

    snap = db.query(TradingAutomationRuntimeSnapshot).filter_by(session_id=sess.id).one()
    binding = db.query(TradingAutomationSessionBinding).filter_by(session_id=sess.id).one()
    fill = db.query(TradingAutomationSimulatedFill).filter_by(session_id=sess.id).one()
    assert snap.symbol == "ETH-USD"
    assert snap.lane == "simulation"
    assert binding.discovery_provider == "massive"
    assert fill.action == "enter_long"
