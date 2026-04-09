"""Operator read-model, promotion lineage, and readiness surfaces."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.core import User
from app.models.trading import TradingAutomationSession
from app.services.trading.momentum_neural.automation_query import get_operator_session_focus, list_automation_sessions
from app.services.trading.momentum_neural.persistence import create_trading_automation_session
from app.services.trading.momentum_neural.paper_fsm import STATE_FINISHED
from app.services.trading.momentum_neural.session_lifecycle import canonical_operator_state
from tests.test_momentum_automation_api import _variant


def _uid(db: Session) -> int:
    u = User(name=f"MomentumOpWF-{uuid.uuid4().hex[:10]}")
    db.add(u)
    db.commit()
    return int(u.id)


def _broker_patch(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.operator_readiness.get_all_broker_statuses",
        lambda: {
            "robinhood": {"connected": False},
            "coinbase": {"connected": True, "configured": True},
            "metamask": {"connected": False},
        },
    )


def test_operator_readiness_route(client) -> None:
    r = client.get("/api/trading/momentum/operator/readiness")
    assert r.status_code == 200
    j = r.json()
    assert j.get("ok") is True
    assert "operator_readiness" in j
    assert "paper_runner_enabled" in j["operator_readiness"]


def test_canonical_maps_paper_queued() -> None:
    assert canonical_operator_state(mode="paper", state="queued", risk_snapshot_json={}) == "queued"


def test_list_sessions_includes_canonical(db: Session) -> None:
    uid = _uid(db)
    v = _variant(db)
    sym = "CANOP-9933-USD"
    create_trading_automation_session(db, user_id=uid, symbol=sym, variant_id=v.id, state="draft", mode="paper")
    db.commit()
    out = list_automation_sessions(db, user_id=uid, limit=50)
    assert "operator_readiness" in out
    matches = [x for x in out["sessions"] if x["symbol"] == sym]
    assert matches, out["sessions"]
    row = matches[0]
    assert row.get("canonical_operator_state") == "draft"
    assert "blocked_reason" in row
    assert "next_action_required" in row


def test_promote_paper_to_live_arm_lineage(paired_client, db: Session, monkeypatch) -> None:
    _broker_patch(monkeypatch)
    from tests.test_momentum_operator_api import _seed_live_eligible_row

    vid, _ = _seed_live_eligible_row(db, symbol="PRO-USD")
    c, user = paired_client
    r0 = c.post(
        "/api/trading/momentum/run-paper",
        json={"symbol": "PRO-USD", "variant_id": vid},
    )
    assert r0.status_code == 200
    paper_id = r0.json()["session_id"]

    r1 = c.post("/api/trading/momentum/promote-paper", json={"paper_session_id": paper_id})
    assert r1.status_code == 200
    j = r1.json()
    assert j.get("source_paper_session_id") == paper_id
    assert j.get("arm_token")
    live_id = j["session_id"]
    db.expire_all()
    live = db.query(TradingAutomationSession).filter(TradingAutomationSession.id == live_id).one()
    assert live.source_paper_session_id == paper_id
    assert live.state == "live_arm_pending"


def test_promote_rejects_stale_finished_paper(paired_client, db: Session, monkeypatch) -> None:
    _broker_patch(monkeypatch)
    from tests.test_momentum_operator_api import _seed_live_eligible_row

    vid, _ = _seed_live_eligible_row(db, symbol="STALE-USD")
    c, user = paired_client
    uid = user.id
    paper = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="STALE-USD",
        variant_id=vid,
        state=STATE_FINISHED,
        mode="paper",
    )
    old = datetime.utcnow() - timedelta(seconds=86400)
    paper.ended_at = old
    paper.updated_at = old
    db.commit()

    r = c.post("/api/trading/momentum/promote-paper", json={"paper_session_id": paper.id})
    assert r.status_code == 400
    det = r.json().get("detail") or ""
    assert "stale" in str(det).lower() or "old" in str(det).lower() or "fresh" in str(det).lower()


def test_get_operator_session_focus_priority_live_over_paper(db: Session, monkeypatch) -> None:
    _broker_patch(monkeypatch)
    uid = _uid(db)
    v = _variant(db)
    create_trading_automation_session(
        db, user_id=uid, symbol="FOC-USD", variant_id=v.id, state="draft", mode="paper"
    )
    create_trading_automation_session(
        db,
        user_id=uid,
        symbol="FOC-USD",
        variant_id=v.id,
        state="live_arm_pending",
        mode="live",
    )
    db.commit()
    out = get_operator_session_focus(db, user_id=uid, symbol="FOC-USD")
    assert out.get("focus_session") is not None
    assert out["focus_session"]["mode"] == "live"
