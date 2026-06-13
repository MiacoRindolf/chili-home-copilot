"""Paper-draft dedup is venue-aware + venue is derived from the execution family.

A crypto name must hold both its coinbase primary paper session AND an alpaca
paper twin (the fill-quality A/B); without the execution_family in the dedup
key the twin collapses into the primary and never spawns. And the session's
venue must reflect its execution family, not a hardcoded "coinbase" (which
mislabels alpaca/robinhood paper sessions in the autopilot rollup).
(docs/DESIGN/ALPACA_LANE.md)
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.trading.momentum_neural.operator_actions import create_paper_draft_session
from app.services.trading.momentum_neural.persistence import create_trading_automation_session

from tests.test_momentum_live_runner import _uid
from tests.test_momentum_paper_runner import _seed_live_eligible_row


def test_paper_draft_dedup_is_venue_aware(db: Session) -> None:
    uid = _uid(db, "paperdedup")
    vid, _ = _seed_live_eligible_row(db, symbol="PDEDUP-USD")
    db.commit()

    primary = create_paper_draft_session(
        db, user_id=uid, symbol="PDEDUP-USD", variant_id=vid,
        execution_family="coinbase_spot",
    )
    assert primary.get("ok") and not primary.get("deduped"), primary

    # SAME venue -> dedups to the existing coinbase paper session.
    again = create_paper_draft_session(
        db, user_id=uid, symbol="PDEDUP-USD", variant_id=vid,
        execution_family="coinbase_spot",
    )
    assert again.get("deduped") is True, again
    assert int(again["session_id"]) == int(primary["session_id"])

    # DIFFERENT venue (alpaca paper twin) -> must NOT dedup to the coinbase one.
    twin = create_paper_draft_session(
        db, user_id=uid, symbol="PDEDUP-USD", variant_id=vid,
        execution_family="alpaca_spot",
    )
    assert not (twin.get("deduped") and int(twin.get("session_id") or -1) == int(primary["session_id"])), twin


def test_paper_draft_venue_matches_execution_family(db: Session) -> None:
    from app.models.trading import TradingAutomationSession

    uid = _uid(db, "papervenue")
    vid, _ = _seed_live_eligible_row(db, symbol="VMATCH")  # equity ticker
    db.commit()

    res = create_paper_draft_session(
        db, user_id=uid, symbol="VMATCH", variant_id=vid,
        execution_family="alpaca_spot",
    )
    assert res.get("ok"), res
    sess = db.get(TradingAutomationSession, int(res["session_id"]))
    assert sess.execution_family == "alpaca_spot"
    assert sess.venue == "alpaca"  # NOT the old hardcoded "coinbase"
