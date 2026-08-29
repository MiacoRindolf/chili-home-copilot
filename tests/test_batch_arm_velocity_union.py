"""Batch-arm velocity union — isara ang batch-pass na bulag sa velocity names.

Ang snapshot-only prefilter ay bumabagsak sa velocity-admitted (#1234) na mga
pangalan (negatibo ang day change nila), kaya BATCH arm pass ay hindi sila
nakikita — bridge lang. Ang union ay kumukuha ng sariwang viability rows na
may ``velocity_pct`` stamp sa persisted signal.

Runnable: pytest tests/test_batch_arm_velocity_union.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models.trading import MomentumSymbolViability
from app.services.trading.momentum_neural.auto_arm import (
    _velocity_qualified_symbols,
)


def _variant_id(db):
    from app.models.trading import MomentumStrategyVariant
    import uuid as _uuid

    v = MomentumStrategyVariant(
        family="momentum_pullback",
        variant_key=f"vel-union-{_uuid.uuid4().hex[:8]}",
        params_json={},
        label="vel-union-test",
        execution_family="alpaca_spot",
    )
    db.add(v)
    db.flush()
    return int(v.id)


def _row(db, sym, *, fresh_s=10, with_velocity=True):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    signal = {"ticker": sym, "source": "ws_ignition"}
    if with_velocity:
        signal["velocity_pct"] = 11.8
    db.add(MomentumSymbolViability(
        symbol=sym,
        variant_id=_variant_id(db),
        viability_score=0.8,
        paper_eligible=True,
        live_eligible=True,
        freshness_ts=now - timedelta(seconds=fresh_s),
        regime_snapshot_json={},
        execution_readiness_json={"ross_signals": {sym: signal}},
        explain_json={},
        evidence_window_json={},
        scope="symbol",
    ))
    db.flush()


def test_fresh_velocity_row_is_included(db):
    _row(db, "VELX", fresh_s=10, with_velocity=True)
    assert "VELX" in _velocity_qualified_symbols(db)


def test_stale_velocity_row_is_excluded(db):
    _row(db, "VELS", fresh_s=700, with_velocity=True)
    assert "VELS" not in _velocity_qualified_symbols(db)


def test_row_without_velocity_is_excluded(db):
    _row(db, "NOVL", fresh_s=10, with_velocity=False)
    assert "NOVL" not in _velocity_qualified_symbols(db)
