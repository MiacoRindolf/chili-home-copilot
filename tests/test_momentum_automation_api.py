"""Phase 5: automation monitor API (sessions, events, summary, cancel)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.models.trading import (
    BrainActivationEvent,
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationEvent,
    TradingAutomationSession,
)
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
from app.services.trading.momentum_neural.evolution import maybe_publish_refined_variant
from app.services.trading.momentum_neural.persistence import (
    append_trading_automation_event,
    create_trading_automation_session,
    ensure_momentum_strategy_variants,
)

pytestmark = pytest.mark.usefixtures("_asgi_test_client")


def _variant(db: Session) -> MomentumStrategyVariant:
    ensure_momentum_strategy_variants(db)
    db.commit()
    return (
        db.query(MomentumStrategyVariant)
        .filter(
            MomentumStrategyVariant.family == "impulse_breakout",
            MomentumStrategyVariant.parent_variant_id.is_(None),
        )
        .order_by(MomentumStrategyVariant.version.asc(), MomentumStrategyVariant.id.asc())
        .first()
    )


def _uid(db: Session) -> int:
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    u = User(name=f"AutoMonTest-{stamp}")
    db.add(u)
    db.commit()
    db.refresh(u)
    return int(u.id)


def _seed_viability(
    db: Session,
    *,
    symbol: str,
    variant_id: int,
    viability_score: float = 0.62,
    paper_eligible: bool = True,
    live_eligible: bool = False,
    product_tradable: bool = True,
    scope: str = "symbol",
) -> MomentumSymbolViability:
    row = MomentumSymbolViability(
        symbol=symbol,
        scope=scope,
        variant_id=variant_id,
        viability_score=viability_score,
        paper_eligible=paper_eligible,
        live_eligible=live_eligible,
        freshness_ts=datetime.utcnow(),
        regime_snapshot_json={"phase": "test"},
        execution_readiness_json={"product_tradable": product_tradable},
        explain_json={"warnings": []},
        evidence_window_json={},
        source_node_id="test_seed",
        correlation_id="test-seed",
    )
    db.add(row)
    db.flush()
    return row


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
    assert "viability_pipeline" in s and "pending_refresh_count" in s["viability_pipeline"]


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


def test_session_payload_includes_controls_and_runner_health(db: Session) -> None:
    uid = _uid(db)
    v = _variant(db)
    _seed_viability(db, symbol="CTRL-USD", variant_id=v.id, paper_eligible=True, live_eligible=True)
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="CTRL-USD",
        variant_id=v.id,
        state="queued",
        mode="paper",
    )
    append_trading_automation_event(db, sess.id, "queued", {"hello": "world"})
    db.commit()

    out = list_automation_sessions(db, user_id=uid, limit=20)
    row = next(x for x in out["sessions"] if x["id"] == sess.id)
    assert "controls" in row and row["controls"]["run"]["label"] == "Run"
    assert "runner_health" in row and "blocked_reason" in row["runner_health"]
    assert "pause_info" in row
    assert "strategy_params_summary" in row and "entry_viability_min" in row["strategy_params_summary"]
    assert "refinement_info" in row and "is_refined" in row["refinement_info"]
    assert row["asset_class"] == "crypto"
    assert row["market_open_now"] is True


def test_opportunities_route_merges_stock_and_crypto_sources(paired_client, db: Session, monkeypatch) -> None:
    c, _user = paired_client
    v = _variant(db)
    _seed_viability(db, symbol="BTC-USD", variant_id=v.id, viability_score=0.83, paper_eligible=True, live_eligible=True)
    _seed_viability(db, symbol="AAPL", variant_id=v.id, viability_score=0.71, paper_eligible=True, live_eligible=False)
    db.commit()

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.opportunities.run_momentum_scanner",
        lambda max_results=20: {
            "results": [
                {"ticker": "AAPL", "signal": "buy", "score": 0.74, "label": "stock scanner"},
            ]
        },
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.opportunities.get_crypto_breakout_cache",
        lambda: {
            "results": [
                {"symbol": "BTC-USD", "signal": "breakout", "score": 0.91, "label": "crypto breakout"},
            ]
        },
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.opportunities.market_open_now",
        lambda symbol: True,
    )

    r = c.get("/api/trading/momentum/opportunities?mode=paper&asset_class=all&limit=20")
    assert r.status_code == 200
    rows = r.json()["opportunities"]
    meta = r.json()["metadata"]
    assert any(row["symbol"] == "BTC-USD" and row["asset_class"] == "crypto" for row in rows)
    assert any(row["symbol"] == "AAPL" and row["asset_class"] == "stock" for row in rows)
    btc = next(row for row in rows if row["symbol"] == "BTC-USD")
    aapl = next(row for row in rows if row["symbol"] == "AAPL")
    assert btc["paper_ready"] is True
    assert btc["live_ready"] is True
    assert btc["can_create_paper_draft"] is True
    assert btc["can_run_paper"] is False
    assert btc["paper_action"]["label"] == "Create draft"
    assert aapl["live_ready"] is False
    assert btc["top_variant"]["strategy_params_summary"]["entry_viability_min"] is not None
    assert meta["hidden_scan_only_count"] == 0


def test_opportunities_hide_scan_only_without_symbol_viability(paired_client, db: Session, monkeypatch) -> None:
    c, _user = paired_client
    v = _variant(db)
    _seed_viability(
        db,
        symbol="__aggregate__",
        scope="aggregate",
        variant_id=v.id,
        viability_score=0.88,
        paper_eligible=True,
        live_eligible=True,
    )
    db.commit()

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.opportunities.run_momentum_scanner",
        lambda max_results=20: {
            "results": [
                {"ticker": "AAPL", "signal": "buy", "score": 0.74, "label": "stock scanner"},
            ]
        },
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.opportunities.get_crypto_breakout_cache",
        lambda: {"results": []},
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.opportunities.market_open_now",
        lambda symbol: True,
    )

    r = c.get("/api/trading/momentum/opportunities?mode=paper&asset_class=all&limit=20")
    assert r.status_code == 200
    body = r.json()
    assert body["opportunities"] == []
    assert body["metadata"]["hidden_scan_only_count"] == 1
    assert body["metadata"]["viability_symbol_count"] == 0


def test_opportunities_metadata_marks_stale_refresh_backlog(paired_client, db: Session, monkeypatch) -> None:
    c, _user = paired_client
    db.add(
        BrainActivationEvent(
            source_node_id=None,
            cause="momentum_context_refresh",
            payload={"signal_type": "momentum_context_refresh", "meta": {"tickers": ["BTC-USD"]}},
            confidence_delta=0.12,
            propagation_depth=0,
            correlation_id="stale-op-test",
            created_at=datetime.utcnow() - timedelta(minutes=20),
            status="pending",
        )
    )
    db.commit()

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.opportunities.run_momentum_scanner",
        lambda max_results=20: {"results": []},
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.opportunities.get_crypto_breakout_cache",
        lambda: {"results": []},
    )

    r = c.get("/api/trading/momentum/opportunities?mode=paper&asset_class=all&limit=20")
    assert r.status_code == 200
    meta = r.json()["metadata"]
    assert meta["pending_refresh_count"] >= 1
    assert meta["viability_pipeline_stale"] is True

    summary = c.get("/api/trading/momentum/automation/summary")
    assert summary.status_code == 200
    assert summary.json()["viability_pipeline"]["viability_pipeline_stale"] is True


def test_session_action_routes_run_pause_resume_stop_delete(paired_client, db: Session, monkeypatch) -> None:
    c, user = paired_client
    v = _variant(db)
    sess = create_trading_automation_session(
        db,
        user_id=user.id,
        symbol="ACT-USD",
        variant_id=v.id,
        state="draft",
        mode="paper",
    )
    db.commit()

    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.automation_query.tick_paper_session",
        lambda db, session_id: {"ok": True, "session_id": session_id},
    )

    r1 = c.post(f"/api/trading/momentum/automation/sessions/{sess.id}/run")
    assert r1.status_code == 200
    db.refresh(sess)
    assert sess.state == "queued"

    r2 = c.post(f"/api/trading/momentum/automation/sessions/{sess.id}/pause")
    assert r2.status_code == 200
    db.refresh(sess)
    assert isinstance(sess.risk_snapshot_json, dict) and sess.risk_snapshot_json.get("operator_pause", {}).get("active") is True

    r3 = c.post(f"/api/trading/momentum/automation/sessions/{sess.id}/resume")
    assert r3.status_code == 200
    db.refresh(sess)
    assert sess.risk_snapshot_json.get("operator_pause", {}).get("active") in (False, None)

    r4 = c.post(f"/api/trading/momentum/automation/sessions/{sess.id}/stop")
    assert r4.status_code == 200
    db.refresh(sess)
    assert sess.state == STATE_CANCELLED

    r5 = c.post(f"/api/trading/momentum/automation/sessions/{sess.id}/delete")
    assert r5.status_code == 200
    db.refresh(sess)
    assert sess.state == STATE_ARCHIVED


def test_brain_refinement_creates_child_variant_and_clones_viability(db: Session) -> None:
    uid = _uid(db)
    v = _variant(db)
    _seed_viability(db, symbol="RFN-USD", variant_id=v.id, viability_score=0.68, paper_eligible=True, live_eligible=True)
    now = datetime.utcnow()
    for idx in range(5):
        sess = create_trading_automation_session(
            db,
            user_id=uid,
            symbol="RFN-USD",
            variant_id=v.id,
            state="finished",
            mode="paper",
        )
        sess.ended_at = now - timedelta(minutes=idx)
        sess.updated_at = now - timedelta(minutes=idx)
        db.add(
            MomentumAutomationOutcome(
                session_id=sess.id,
                user_id=uid,
                variant_id=v.id,
                symbol="RFN-USD",
                mode="paper",
                execution_family="coinbase_spot",
                terminal_state="finished",
                terminal_at=now - timedelta(minutes=idx),
                outcome_class="target_hit",
                realized_pnl_usd=20.0 + idx,
                return_bps=35.0 + idx,
                hold_seconds=900 + idx * 10,
                exit_reason="target",
                regime_snapshot_json={},
                readiness_snapshot_json={},
                admission_snapshot_json={},
                governance_context_json={},
                extracted_summary_json={},
                evidence_weight=1.0,
                contributes_to_evolution=True,
                created_at=now - timedelta(minutes=idx),
            )
        )
    db.commit()

    out = maybe_publish_refined_variant(db, variant_id=v.id)
    assert out["ok"] is True
    assert out["created"] is True
    db.commit()
    db.refresh(v)
    assert v.is_active is False

    child = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.parent_variant_id == v.id).one()
    assert child.is_active is True
    assert child.refinement_meta_json["source_outcome_count"] == 5
    cloned = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "RFN-USD", MomentumSymbolViability.variant_id == child.id)
        .one_or_none()
    )
    assert cloned is not None
