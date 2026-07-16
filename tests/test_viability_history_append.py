"""Replay v3 R1 — momentum_viability_history append-table (mig311) + write-path.

The ``live_eligible`` gate on ``momentum_symbol_viability`` is a SINGLE mutable snapshot
column with no history, so the eligibility TIME-SERIES that produced the UPC TOCTOU flicker
is not directly recorded (design R1) — Replay v3 has to reconstruct it. This adds an
append-only ``momentum_viability_history`` table that records the ``live_eligible`` value
(plus the scorer inputs to recompute/audit it) AT EACH viability write going forward, so
FUTURE replays read the exact recorded series (perfect fidelity).

This test proves the three R1 contract points:
  1. the migration is idempotent (runs twice, no error);
  2. a viability update appends a history row (flag default ON), with the live_eligible
     value + the scorer inputs faithfully captured;
  3. flag-off => NO append (byte-identical to pre-R1).

Self-contained: seeds its own variants + viability in ``chili_test``; no prod ``chili``
data. One pytest at a time (DB-truncate rule).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.migrations import _migration_311_momentum_viability_history
from app.models.trading import MomentumSymbolViability, MomentumViabilityHistory
from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.persistence import (
    ensure_momentum_strategy_variants,
    persist_neural_momentum_tick,
)
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.viability import score_viability


def _ctx():
    return build_momentum_regime_context(
        now=datetime(2026, 6, 29, 13, 8, 28, tzinfo=timezone.utc),
        atr_pct=0.05,
        meta={"spread_regime": "normal"},
    )


def _row_for(symbol: str, feats: ExecutionReadinessFeatures) -> dict:
    """Score an EQUITY symbol (so the crypto viability gate never skips it) and shape the
    persist row the live tick builds."""
    fam = get_family("impulse_breakout")
    assert fam is not None
    vr = score_viability(symbol, fam, _ctx(), feats)
    row = vr.to_public_dict()
    row["label"] = fam.label
    row["entry_style"] = fam.entry_style
    row["default_stop_logic"] = fam.default_stop_logic
    row["default_exit_logic"] = fam.default_exit_logic
    return row


def test_migration_311_is_idempotent() -> None:
    """The R1 migration creates the table + indexes and re-runs cleanly (CREATE IF NOT
    EXISTS throughout) — running it twice must not raise."""
    with engine.begin() as conn:
        _migration_311_momentum_viability_history(conn)
    with engine.begin() as conn:
        _migration_311_momentum_viability_history(conn)
    # the table is queryable after the (idempotent) migration.
    with engine.connect() as conn:
        from sqlalchemy import text

        cnt = conn.execute(
            text("SELECT COUNT(*) FROM momentum_viability_history")
        ).scalar()
    assert cnt is not None


def test_viability_update_appends_history_row(db: Session) -> None:
    """Flag default ON: a viability write appends ONE history row per persisted name, with
    the live_eligible value + the scorer inputs (rvol/change/spread) captured from the same
    sources the scorer read."""
    settings.chili_momentum_viability_history_enabled = True
    ensure_momentum_strategy_variants(db)
    db.commit()

    # a clean equity name with embedded ross_signals so rvol/change are captured.
    feats = ExecutionReadinessFeatures(
        spread_bps=42.0,
        slippage_estimate_bps=4.0,
        fee_to_target_ratio=0.08,
        meta={"ross_signals": {"UPC": {"rvol": 6.3, "daily_change_pct": 18.4}}},
    )
    row = _row_for("UPC", feats)
    observed_at = datetime(2026, 7, 13, 13, 5, tzinfo=timezone.utc)

    n = persist_neural_momentum_tick(
        db,
        row_dicts=[row],
        regime_snapshot=_ctx().to_public_dict(),
        features=feats,
        correlation_id="r1-corr",
        source_node_id="nm_test",
        observed_at=observed_at,
    )
    assert n == 1
    db.commit()

    hist = (
        db.query(MomentumViabilityHistory)
        .filter(MomentumViabilityHistory.symbol == "UPC")
        .all()
    )
    assert len(hist) == 1
    h = hist[0]
    assert h.live_eligible == bool(row.get("live_eligible", False))
    assert h.viability_score == float(row.get("viability") or 0.0)
    assert h.correlation_id == "r1-corr"
    expected_observed_at = observed_at.replace(tzinfo=None)
    assert h.observed_at == expected_observed_at
    assert h.freshness_ts == expected_observed_at
    viability = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "UPC")
        .one()
    )
    assert viability.freshness_ts == expected_observed_at
    assert viability.updated_at == expected_observed_at
    # the scorer inputs are captured from features.meta['ross_signals'] + features.spread_bps.
    assert h.rvol == 6.3
    assert h.change_pct == 18.4
    assert h.spread_bps == 42.0

    # a SECOND tick (the next scorer pass) APPENDS — append-only, not upsert. The series now
    # carries both observations so a future replay reads the time-series.
    row2 = dict(row)
    row2["viability"] = 0.99
    persist_neural_momentum_tick(
        db,
        row_dicts=[row2],
        regime_snapshot=_ctx().to_public_dict(),
        features=feats,
        correlation_id="r1-corr-2",
        source_node_id="nm_test",
    )
    db.commit()
    hist2 = (
        db.query(MomentumViabilityHistory)
        .filter(MomentumViabilityHistory.symbol == "UPC")
        .order_by(MomentumViabilityHistory.id.asc())
        .all()
    )
    assert len(hist2) == 2, "append-only: the second tick appends, never overwrites"
    assert hist2[1].correlation_id == "r1-corr-2"


def test_flag_off_no_append_byte_identical(db: Session) -> None:
    """Kill-switch OFF: the viability upsert proceeds normally but NO history row is
    appended (byte-identical to pre-R1)."""
    settings.chili_momentum_viability_history_enabled = False
    try:
        ensure_momentum_strategy_variants(db)
        db.commit()
        feats = ExecutionReadinessFeatures(spread_bps=4.0, slippage_estimate_bps=4.0)
        row = _row_for("SDOT", feats)
        n = persist_neural_momentum_tick(
            db,
            row_dicts=[row],
            regime_snapshot=_ctx().to_public_dict(),
            features=feats,
            correlation_id="off-corr",
            source_node_id="nm_test",
        )
        assert n == 1  # the viability upsert still happened
        db.commit()
        hist = (
            db.query(MomentumViabilityHistory)
            .filter(MomentumViabilityHistory.symbol == "SDOT")
            .all()
        )
        assert hist == [], "flag OFF must append zero history rows"
    finally:
        settings.chili_momentum_viability_history_enabled = True
