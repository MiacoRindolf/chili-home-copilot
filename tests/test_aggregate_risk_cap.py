"""Aggregate open at-risk correlation guard: per-trade caps don't see the
pile-up — 2026-06-11's three 'independent' losses were one regime trade x3."""

from __future__ import annotations

from app import models
from app.models.trading import MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.momentum_neural.risk_evaluator import aggregate_open_risk_usd


def _held_session(db, user_id, variant_id, symbol, qty, entry, stop, state="live_entered"):
    sess = TradingAutomationSession(
        user_id=user_id, symbol=symbol, mode="live", variant_id=variant_id, state=state,
        risk_snapshot_json={"momentum_live_execution": {"position": {
            "quantity": qty, "avg_entry_price": entry, "stop_price": stop,
        }}},
    )
    db.add(sess)
    db.flush()
    return sess


def test_aggregate_sums_only_below_entry_risk(db) -> None:
    u = models.User(name="agg-risk")
    db.add(u)
    db.flush()
    v = MomentumStrategyVariant(family="agg", variant_key="agg_v", label="agg", params_json={})
    db.add(v)
    db.flush()
    _held_session(db, u.id, v.id, "AAA", qty=100, entry=10.0, stop=9.0)    # $100 at risk
    _held_session(db, u.id, v.id, "BBB", qty=200, entry=5.0, stop=4.75)   # $50 at risk
    _held_session(db, u.id, v.id, "CCC", qty=100, entry=8.0, stop=8.0)    # breakeven-locked -> 0
    _held_session(db, u.id, v.id, "DDD", qty=100, entry=8.0, stop=9.0)    # stop ABOVE entry (locked profit) -> 0
    _held_session(db, u.id, v.id, "BTC-USD", qty=1, entry=100.0, stop=90.0)  # crypto excluded
    db.commit()
    total, rows = aggregate_open_risk_usd(db, user_id=u.id)
    assert abs(total - 150.0) < 1e-9
    assert sorted(r["symbol"] for r in rows) == ["AAA", "BBB"]


def test_aggregate_zero_when_flat(db) -> None:
    u = models.User(name="agg-flat")
    db.add(u)
    db.flush()
    total, rows = aggregate_open_risk_usd(db, user_id=u.id)
    assert total == 0.0 and rows == []
