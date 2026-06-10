"""Venue-aware live-arm dedup — the same name can hold one live session PER VENUE (the
robinhood_spot-real vs alpaca_spot-paper A/B enabler). Same-venue still dedups (no double-arm).
(docs/DESIGN/ALPACA_LANE.md)"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.trading.momentum_neural.live_fsm import STATE_QUEUED_LIVE
from app.services.trading.momentum_neural.operator_actions import begin_live_arm
from app.services.trading.momentum_neural.persistence import create_trading_automation_session

from tests.test_momentum_live_runner import _uid
from tests.test_momentum_paper_runner import _seed_live_eligible_row


def _seed_live_session(db, *, uid, symbol, variant_id, execution_family):
    return create_trading_automation_session(
        db, user_id=uid, symbol=symbol, variant_id=variant_id, mode="live",
        execution_family=execution_family, state=STATE_QUEUED_LIVE,
        risk_snapshot_json={"arm_token": f"tok-{execution_family}"},
    )


def test_begin_live_arm_dedup_is_venue_aware(db: Session) -> None:
    uid = _uid(db, "venuededup")
    vid, _ = _seed_live_eligible_row(db, symbol="XDEDUP")  # valid variant FK + live-eligible viability
    rh = _seed_live_session(db, uid=uid, symbol="XDEDUP", variant_id=vid, execution_family="robinhood_spot")
    db.commit()

    # SAME venue -> dedups to the existing RH session (no double-arm / double real money).
    r_rh = begin_live_arm(db, user_id=uid, symbol="XDEDUP", variant_id=vid, execution_family="robinhood_spot")
    assert r_rh.get("deduped") is True, r_rh
    assert int(r_rh.get("session_id")) == int(rh.id)

    # DIFFERENT venue (Alpaca paper) -> must NOT dedup to the RH session (it passed the venue
    # dedup). Whether it then creates a new alpaca session or fails at a later gate, the point
    # is it did NOT return the robinhood_spot session.
    r_al = begin_live_arm(db, user_id=uid, symbol="XDEDUP", variant_id=vid, execution_family="alpaca_spot")
    assert not (r_al.get("deduped") and int(r_al.get("session_id") or -1) == int(rh.id)), r_al


def test_rh_arm_not_deduped_by_an_existing_alpaca_session(db: Session) -> None:
    # Order-independence: an Alpaca session must not satisfy an RH dedup (and vice versa).
    uid = _uid(db, "venuededup2")
    vid, _ = _seed_live_eligible_row(db, symbol="YDEDUP")
    al = _seed_live_session(db, uid=uid, symbol="YDEDUP", variant_id=vid, execution_family="alpaca_spot")
    db.commit()
    r_rh = begin_live_arm(db, user_id=uid, symbol="YDEDUP", variant_id=vid, execution_family="robinhood_spot")
    assert not (r_rh.get("deduped") and int(r_rh.get("session_id") or -1) == int(al.id)), r_rh
