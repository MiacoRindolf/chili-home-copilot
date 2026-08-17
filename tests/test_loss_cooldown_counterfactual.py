"""Loss-cooldown leader counterfactual telemetry (2026-08-17, Ross Aral #23).

Sukat muna bago luwag ([[feedback_evolve_not_devolve]]): kapag ang board #1 ay
naka-first-strike loss-cooldown (HINDI 2-strike), gumagawa ng durable event
(`arm_loss_cooldown_leader_counterfactual`) na nakakabit sa pinakahuling
terminal session ng symbol — ang scoring ay maghahambing sa tape ng susunod na
60 minuto. Telemetry lamang; walang binabagong gating.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text

import app.services.trading.momentum_neural.auto_arm as AA


def _seed_terminal_session(db, symbol: str) -> int:
    from app import models
    from app.models.trading import TradingAutomationSession
    import uuid

    user = models.User(name=f"cf-{uuid.uuid4().hex[:10]}")
    db.add(user)
    db.flush()
    from app.models.trading import MomentumStrategyVariant

    variant = MomentumStrategyVariant(
        family="cf-test", variant_key=f"cf_{uuid.uuid4().hex[:8]}",
        label="cf", params_json={},
    )
    db.add(variant)
    db.flush()
    sess = TradingAutomationSession(
        user_id=user.id, venue="alpaca", execution_family="alpaca_spot",
        mode="live", symbol=symbol, variant_id=variant.id,
        state="live_cancelled",
        ended_at=datetime.utcnow() - timedelta(minutes=3),
        risk_snapshot_json={},
    )
    db.add(sess)
    db.commit()
    return int(sess.id)


def test_emits_durable_event_once_per_window(db):
    AA._LOSS_CF_EMITTED.clear()
    sid = _seed_terminal_session(db, "CFLD")
    until = datetime.utcnow() + timedelta(minutes=4)
    AA._emit_loss_cooldown_leader_counterfactual(
        db, symbol="CFLD", cooldown_until=until, pass_as_of=datetime.utcnow(),
    )
    AA._emit_loss_cooldown_leader_counterfactual(
        db, symbol="CFLD", cooldown_until=until, pass_as_of=datetime.utcnow(),
    )
    db.commit()
    rows = db.execute(text(
        "SELECT session_id, payload_json FROM trading_automation_events "
        "WHERE event_type='arm_loss_cooldown_leader_counterfactual'"
    )).fetchall()
    assert len(rows) == 1, "dapat isang event kada cooldown window (dedupe)"
    assert int(rows[0][0]) == sid
    assert rows[0][1]["symbol"] == "CFLD"
    assert rows[0][1]["cooldown_until_utc"] == until.isoformat()


def test_new_window_emits_again(db):
    AA._LOSS_CF_EMITTED.clear()
    _seed_terminal_session(db, "CFLE")
    u1 = datetime.utcnow() + timedelta(minutes=4)
    u2 = datetime.utcnow() + timedelta(minutes=9)
    AA._emit_loss_cooldown_leader_counterfactual(
        db, symbol="CFLE", cooldown_until=u1, pass_as_of=datetime.utcnow(),
    )
    AA._emit_loss_cooldown_leader_counterfactual(
        db, symbol="CFLE", cooldown_until=u2, pass_as_of=datetime.utcnow(),
    )
    db.commit()
    n = db.execute(text(
        "SELECT count(*) FROM trading_automation_events "
        "WHERE event_type='arm_loss_cooldown_leader_counterfactual'"
    )).scalar()
    assert int(n) == 2


def test_no_terminal_session_is_silent_noop(db):
    AA._LOSS_CF_EMITTED.clear()
    AA._emit_loss_cooldown_leader_counterfactual(
        db, symbol="WALAXX",
        cooldown_until=datetime.utcnow() + timedelta(minutes=4),
        pass_as_of=datetime.utcnow(),
    )
    db.commit()
    n = db.execute(text(
        "SELECT count(*) FROM trading_automation_events "
        "WHERE event_type='arm_loss_cooldown_leader_counterfactual'"
    )).scalar()
    assert int(n) == 0
