"""Phase 2: neural momentum durable tables (PostgreSQL + migrations)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.trading import MomentumStrategyVariant, MomentumSymbolViability
from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.persistence import (
    append_trading_automation_event,
    create_trading_automation_session,
    ensure_momentum_strategy_variants,
    persist_neural_momentum_tick,
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
