"""Operator FLATTEN: manual exits must flow through the system's own order
chain, never race it from the broker app (2026-06-11 CPSH/SNDG)."""

from __future__ import annotations

from app import models
from app.models.trading import MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.momentum_neural.automation_query import request_flatten_session


def _sess(db, user_id, variant_id, state, mode="live"):
    s = TradingAutomationSession(
        user_id=user_id, symbol="INDP", mode=mode, variant_id=variant_id, state=state,
        risk_snapshot_json={"momentum_live_execution": {"position": {"quantity": 10}}},
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _setup(db):
    u = models.User(name="flatten")
    db.add(u)
    db.flush()
    v = MomentumStrategyVariant(family="fl", variant_key="fl_v", label="fl", params_json={})
    db.add(v)
    db.flush()
    return u, v


def test_flatten_sets_flag_and_emits_event(db) -> None:
    u, v = _setup(db)
    s = _sess(db, u.id, v.id, "live_entered")
    out = request_flatten_session(db, user_id=u.id, session_id=s.id)
    assert out["ok"], out
    db.refresh(s)
    le = s.risk_snapshot_json["momentum_live_execution"]
    assert le.get("operator_flatten_requested_utc")
    from sqlalchemy import text

    n = db.execute(text(
        "SELECT count(*) FROM trading_automation_events "
        "WHERE session_id=:sid AND event_type='operator_flatten_requested'"
    ), {"sid": s.id}).scalar()
    assert n == 1


def test_flatten_rejects_non_held_states(db) -> None:
    u, v = _setup(db)
    s = _sess(db, u.id, v.id, "watching_live")
    out = request_flatten_session(db, user_id=u.id, session_id=s.id)
    assert not out["ok"] and out["error"] == "not_flattenable"
    s2 = _sess(db, u.id, v.id, "entered", mode="paper")
    out2 = request_flatten_session(db, user_id=u.id, session_id=s2.id)
    assert not out2["ok"]


def test_flatten_runner_hook_exists() -> None:
    import inspect

    from app.services.trading.momentum_neural import live_runner as lr

    src = inspect.getsource(lr)
    assert "operator_flatten_requested_utc" in src
    assert 'flatten_reason="operator_flatten"' in src
