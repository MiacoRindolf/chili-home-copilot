"""Phase 5: automation monitor API (sessions, events, summary, cancel)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.core import User
from app.models.trading import MomentumStrategyVariant, TradingAutomationSession, TradingAutomationEvent
from app.services.trading.momentum_neural.automation_query import (
    STATE_ARCHIVED,
    STATE_CANCELLED,
    STATE_EXPIRED,
    STATE_LIVE_ARM_PENDING,
    archive_automation_session,
    automation_summary,
    cancel_automation_session,
    expire_stale_live_arm_sessions,
    get_automation_session_detail,
    list_automation_events,
    list_automation_sessions,
)
from app.services.trading.momentum_neural.persistence import (
    append_trading_automation_event,
    create_trading_automation_session,
    ensure_momentum_strategy_variants,
)

pytestmark = pytest.mark.usefixtures("_asgi_test_client")


def _variant(db: Session) -> MomentumStrategyVariant:
    ensure_momentum_strategy_variants(db)
    db.commit()
    return db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()


def _uid(db: Session) -> int:
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    u = User(name=f"AutoMonTest-{stamp}")
    db.add(u)
    db.commit()
    db.refresh(u)
    return int(u.id)


def test_automation_summary_shape(db: Session) -> None:
    uid = _uid(db)
    v = _variant(db)
    create_trading_automation_session(
        db, user_id=uid, symbol="T1-USD", variant_id=v.id, state="draft", mode="paper"
    )
    db.commit()
    s = automation_summary(db, user_id=uid)
    assert s["total_sessions"] >= 1
    assert "mesh_enabled" in s
    assert "limitations_note" in s
    assert "governance" in s and "kill_switch_active" in s["governance"]
    assert "risk_policy_summary" in s and "policy_version" in s["risk_policy_summary"]


def test_list_sessions_shape_and_event_count(db: Session) -> None:
    uid = _uid(db)
    v = _variant(db)
    sess = create_trading_automation_session(
        db, user_id=uid, symbol="T2-USD", variant_id=v.id, state="draft", mode="paper"
    )
    append_trading_automation_event(db, sess.id, "test_evt", {"a": 1})
    db.commit()
    out = list_automation_sessions(db, user_id=uid, limit=50)
    assert "sessions" in out
    row = next(x for x in out["sessions"] if x["id"] == sess.id)
    assert row["event_count"] >= 1
    assert row["variant"]["label"]
    assert "risk_status" in row
    assert "severity" in row["risk_status"]
    assert row["lane"] == "simulation"
    assert "thesis" in row
    assert "execution_readiness" in row
    assert "data_binding" in row
    assert "data_fidelity" in row
    assert "chart_levels" in row


def test_session_detail_joins_variant(db: Session) -> None:
    uid = _uid(db)
    v = _variant(db)
    sess = create_trading_automation_session(
        db, user_id=uid, symbol="T3-USD", variant_id=v.id, state="live_arm_pending", mode="live"
    )
    append_trading_automation_event(db, sess.id, "arm", {})
    db.commit()
    d = get_automation_session_detail(db, user_id=uid, session_id=sess.id)
    assert d is not None
    assert d["session"]["symbol"] == "T3-USD"
    assert d["session"]["variant"]["family"] == "impulse_breakout"
    assert len(d["events"]) >= 1
    assert "simulated_fills" in d
    assert "data_binding" in d["session"]
    assert "lane" in d["session"]


def test_list_events_filter(db: Session) -> None:
    uid = _uid(db)
    v = _variant(db)
    s1 = create_trading_automation_session(db, user_id=uid, symbol="A-USD", variant_id=v.id, state="draft")
    s2 = create_trading_automation_session(db, user_id=uid, symbol="B-USD", variant_id=v.id, state="draft")
    append_trading_automation_event(db, s1.id, "type_a", {})
    append_trading_automation_event(db, s2.id, "type_b", {})
    db.commit()
    evs = list_automation_events(db, user_id=uid, session_id=s1.id, limit=20)
    assert all(e["session_id"] == s1.id for e in evs["events"])


def test_cancel_allowed_state_appends_event(db: Session) -> None:
    uid = _uid(db)
    v = _variant(db)
    sess = create_trading_automation_session(
        db, user_id=uid, symbol="CX-USD", variant_id=v.id, state="draft", mode="paper"
    )
    db.commit()
    out = cancel_automation_session(db, user_id=uid, session_id=sess.id)
    assert out["ok"] is True
    db.commit()
    db.refresh(sess)
    assert sess.state == STATE_CANCELLED
    evs = db.query(TradingAutomationEvent).filter(TradingAutomationEvent.session_id == sess.id).all()
    assert any(e.event_type == "session_cancelled" for e in evs)


def test_cancel_running_state_rejected(db: Session) -> None:
    uid = _uid(db)
    v = _variant(db)
    sess = create_trading_automation_session(
        db, user_id=uid, symbol="RJ-USD", variant_id=v.id, state="cancelled", mode="paper"
    )
    db.commit()
    out = cancel_automation_session(db, user_id=uid, session_id=sess.id)
    assert out["ok"] is False
    assert out["error"] == "not_cancellable"


def test_archive_draft(db: Session) -> None:
    uid = _uid(db)
    v = _variant(db)
    sess = create_trading_automation_session(
        db, user_id=uid, symbol="AR-USD", variant_id=v.id, state="draft", mode="paper"
    )
    db.commit()
    out = archive_automation_session(db, user_id=uid, session_id=sess.id)
    assert out["ok"] is True
    db.commit()
    db.refresh(sess)
    assert sess.state == STATE_ARCHIVED


def test_expire_stale_live_arm(db: Session) -> None:
    uid = _uid(db)
    v = _variant(db)
    past = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    sess = TradingAutomationSession(
        user_id=uid,
        venue="coinbase",
        execution_family="coinbase_spot",
        mode="live",
        symbol="EX-USD",
        variant_id=v.id,
        state=STATE_LIVE_ARM_PENDING,
        risk_snapshot_json={"arm_token": "tok", "expires_at_utc": past},
        correlation_id="c1",
        source_node_id="test",
        started_at=datetime.utcnow(),
    )
    db.add(sess)
    db.flush()
    n = expire_stale_live_arm_sessions(db, user_id=uid)
    assert n == 1
    db.commit()
    db.refresh(sess)
    assert sess.state == STATE_EXPIRED


def test_automation_routes_guest_403(client) -> None:
    r = client.get("/api/trading/momentum/automation/summary")
    assert r.status_code == 403


def test_trading_automation_page_loads(client) -> None:
    r = client.get("/trading/automation")
    assert r.status_code == 200
    assert b"Trading Autopilot" in r.content or b"automation" in r.content.lower()


def test_trading_autopilot_page_loads(client) -> None:
    r = client.get("/trading/autopilot")
    assert r.status_code == 200
    assert b"Trading Autopilot" in r.content


def test_automation_routes_paired_shape(paired_client, db: Session) -> None:
    c, user = paired_client
    v = _variant(db)
    create_trading_automation_session(
        db,
        user_id=user.id,
        symbol="API-USD",
        variant_id=v.id,
        state="draft",
        mode="paper",
    )
    db.commit()

    r = c.get("/api/trading/momentum/automation/summary")
    assert r.status_code == 200
    assert r.json().get("total_sessions", 0) >= 1

    r2 = c.get("/api/trading/momentum/automation/sessions")
    assert r2.status_code == 200
    rows = r2.json().get("sessions") or []
    assert isinstance(rows, list)
    assert rows and "lane" in rows[0]
    assert "data_binding" in rows[0]
    assert "simulated_pnl" in rows[0]

    sid = rows[0]["id"]
    r3 = c.get(f"/api/trading/momentum/automation/sessions/{sid}")
    assert r3.status_code == 200
    assert "events" in r3.json()
    assert "simulated_fills" in r3.json()

    r4 = c.get("/api/trading/momentum/automation/events?limit=5")
    assert r4.status_code == 200
