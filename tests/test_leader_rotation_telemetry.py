"""Leader-rotation telemetry (Ross Aral #16/#25) — durable event sa bawat
pagbabago ng board #1 sa pagitan ng arm passes. Telemetry-first: ang rotation
frequency/timing ang batayan ng anumang hinaharap na rotation-aware na lohika."""
from __future__ import annotations

from sqlalchemy import text

import app.services.trading.momentum_neural.auto_arm as AA


def _seed_session(db, symbol: str) -> int:
    import uuid

    from app import models
    from app.models.trading import MomentumStrategyVariant, TradingAutomationSession

    user = models.User(name=f"rot-{uuid.uuid4().hex[:10]}")
    db.add(user)
    db.flush()
    variant = MomentumStrategyVariant(
        family="rot-test", variant_key=f"rot_{uuid.uuid4().hex[:8]}",
        label="rot", params_json={},
    )
    db.add(variant)
    db.flush()
    sess = TradingAutomationSession(
        user_id=user.id, venue="alpaca", execution_family="alpaca_spot",
        mode="live", symbol=symbol, variant_id=variant.id,
        state="watching_live", risk_snapshot_json={},
    )
    db.add(sess)
    db.commit()
    return int(sess.id)


def _events(db):
    return db.execute(text(
        "SELECT payload_json FROM trading_automation_events "
        "WHERE event_type='arm_leader_rotation' ORDER BY ts"
    )).fetchall()


def test_first_pass_after_restart_is_silent(db):
    AA._PREV_BOARD_LEADER.clear()
    _seed_session(db, "LDRA")
    AA._emit_leader_rotation_if_changed(db, "LDRA")
    db.commit()
    assert len(_events(db)) == 0


def test_rotation_emits_once_per_change(db):
    AA._PREV_BOARD_LEADER.clear()
    _seed_session(db, "LDRB")
    _seed_session(db, "LDRC")
    AA._emit_leader_rotation_if_changed(db, "LDRB")
    AA._emit_leader_rotation_if_changed(db, "LDRB")  # walang pagbabago
    AA._emit_leader_rotation_if_changed(db, "LDRC")  # rotation!
    AA._emit_leader_rotation_if_changed(db, "LDRC")  # walang pagbabago
    db.commit()
    rows = _events(db)
    assert len(rows) == 1
    assert rows[0][0]["old_leader"] == "LDRB"
    assert rows[0][0]["new_leader"] == "LDRC"


def test_no_session_for_either_symbol_is_silent_noop(db):
    AA._PREV_BOARD_LEADER.clear()
    AA._emit_leader_rotation_if_changed(db, "WALAA")
    AA._emit_leader_rotation_if_changed(db, "WALAB")
    db.commit()
    assert len(_events(db)) == 0
