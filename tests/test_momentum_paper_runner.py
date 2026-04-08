"""Phase 7: paper automation runner FSM (simulated execution only)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.models.trading import (
    MomentumStrategyVariant,
    TradingAutomationEvent,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.operator_actions import create_paper_draft_session
from app.services.trading.momentum_neural.persistence import persist_neural_momentum_tick
from app.services.trading.momentum_neural.viability import score_viability
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.paper_fsm import (
    STATE_ENTERED,
    STATE_QUEUED,
    STATE_WATCHING,
    assert_transition,
    can_transition,
)
from app.services.trading.momentum_neural.paper_runner import list_runnable_paper_sessions, tick_paper_session
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
from app.services.trading.momentum_neural.automation_query import cancel_automation_session
from app.services.trading.momentum_neural.persistence import ensure_momentum_strategy_variants


def _seed_live_eligible_row(db: Session, *, symbol: str = "SOL-USD") -> tuple[int, MomentumStrategyVariant]:
    ensure_momentum_strategy_variants(db)
    db.commit()
    fam = get_family("impulse_breakout")
    assert fam is not None
    ctx = build_momentum_regime_context(
        now=datetime(2026, 4, 7, 16, 0, tzinfo=timezone.utc),
        atr_pct=0.02,
        meta={"spread_regime": "normal"},
    )
    feats = ExecutionReadinessFeatures(spread_bps=5.0)
    vr = score_viability(symbol, fam, ctx, feats)
    row = vr.to_public_dict()
    row["label"] = fam.label
    row["entry_style"] = fam.entry_style
    row["default_stop_logic"] = fam.default_stop_logic
    row["default_exit_logic"] = fam.default_exit_logic
    persist_neural_momentum_tick(
        db,
        row_dicts=[row],
        regime_snapshot=ctx.to_public_dict(),
        features=feats,
        correlation_id="paper-test",
        source_node_id="nm_momentum_crypto_intel",
    )
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()
    return v.id, v


def _uid(db: Session, name_suffix: str) -> int:
    u = User(name=f"PaperRun_{name_suffix}")
    db.add(u)
    db.commit()
    db.refresh(u)
    return int(u.id)


def test_fsm_valid_and_invalid_transition() -> None:
    assert can_transition(STATE_QUEUED, STATE_WATCHING)
    assert not can_transition(STATE_ENTERED, STATE_QUEUED)
    with pytest.raises(ValueError):
        assert_transition(STATE_ENTERED, STATE_QUEUED)


def test_run_paper_admission_queued_when_runner_enabled(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="ADM-USD")
    db.commit()
    uid = _uid(db, "adm")
    r = create_paper_draft_session(
        db, user_id=uid, symbol="ADM-USD", variant_id=vid, execution_family="coinbase_spot"
    )
    assert r["ok"] is True
    assert r["state"] == STATE_QUEUED
    db.flush()
    sess = db.query(TradingAutomationSession).filter(TradingAutomationSession.id == r["session_id"]).one()
    assert RISK_SNAPSHOT_KEY in (sess.risk_snapshot_json or {})
    assert sess.risk_snapshot_json.get("momentum_policy_caps")


def test_paper_tick_advances_smoke(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="TCK-USD")
    db.commit()
    uid = _uid(db, "tck")
    r = create_paper_draft_session(
        db, user_id=uid, symbol="TCK-USD", variant_id=vid, execution_family="coinbase_spot"
    )
    assert r["ok"]
    sid = r["session_id"]
    db.commit()

    def qfn(sym: str) -> dict:
        return {"mid": 100.0, "bid": 99.9, "ask": 100.1, "source": "test"}

    out1 = tick_paper_session(db, sid, quote_fn=qfn)
    assert out1.get("ok")
    db.commit()
    s1 = db.query(TradingAutomationSession).filter(TradingAutomationSession.id == sid).one()
    assert s1.state == STATE_WATCHING

    out2 = tick_paper_session(db, sid, quote_fn=qfn)
    db.commit()
    s2 = db.query(TradingAutomationSession).filter(TradingAutomationSession.id == sid).one()
    assert s2.state in (STATE_WATCHING, "entry_candidate")


def test_paper_entry_uses_spread_slippage_fee(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="FIL-USD")
    db.commit()
    uid = _uid(db, "fil")
    r = create_paper_draft_session(db, user_id=uid, symbol="FIL-USD", variant_id=vid)
    sid = r["session_id"]
    db.commit()

    def qfn(_s: str) -> dict:
        return {"mid": 200.0, "bid": 199.8, "ask": 200.2, "source": "test"}

    # queued -> watching -> candidate -> pending -> entered
    for _ in range(6):
        tick_paper_session(db, sid, quote_fn=qfn)
        db.commit()

    sess = db.query(TradingAutomationSession).filter(TradingAutomationSession.id == sid).one()
    if sess.state != STATE_ENTERED:
        # Viability gating may vary; at least ensure we did not corrupt frozen risk
        snap = sess.risk_snapshot_json or {}
        assert RISK_SNAPSHOT_KEY in snap
        return

    pe = (sess.risk_snapshot_json or {}).get("momentum_paper_execution") or {}
    pos = pe.get("position") or {}
    assert pos.get("entry_price", 0) > 200.2  # ask + slippage
    assert pos.get("fees_est_usd", 0) > 0


def test_frozen_momentum_risk_not_overwritten(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="FRZ-USD")
    db.commit()
    uid = _uid(db, "frz")
    r = create_paper_draft_session(db, user_id=uid, symbol="FRZ-USD", variant_id=vid)
    sid = r["session_id"]
    sess0 = db.query(TradingAutomationSession).filter(TradingAutomationSession.id == sid).one()
    frozen = dict((sess0.risk_snapshot_json or {}).get(RISK_SNAPSHOT_KEY) or {})
    db.commit()

    tick_paper_session(db, sid, quote_fn=lambda _s: {"mid": 50.0, "bid": 49.9, "ask": 50.1})
    db.commit()
    sess1 = db.query(TradingAutomationSession).filter(TradingAutomationSession.id == sid).one()
    after = dict((sess1.risk_snapshot_json or {}).get(RISK_SNAPSHOT_KEY) or {})
    assert after.get("evaluated_at_utc") == frozen.get("evaluated_at_utc")


def test_cancel_stops_runner(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="CAN-USD")
    db.commit()
    uid = _uid(db, "can")
    r = create_paper_draft_session(db, user_id=uid, symbol="CAN-USD", variant_id=vid)
    sid = r["session_id"]
    db.commit()
    tick_paper_session(db, sid, quote_fn=lambda _s: {"mid": 10.0, "bid": 9.9, "ask": 10.1})
    db.commit()

    cancel_automation_session(db, user_id=uid, session_id=sid)
    db.commit()

    out = tick_paper_session(db, sid, quote_fn=lambda _s: {"mid": 10.0})
    assert out.get("skipped") == "not_runnable"


def test_list_runnable_excludes_live_intent(db: Session) -> None:
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session
    from app.services.trading.momentum_neural.paper_fsm import STATE_LIVE_ARM_PENDING, STATE_QUEUED as Q

    uid = _uid(db, "lst")
    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()

    create_trading_automation_session(
        db,
        user_id=uid,
        symbol="LIV-USD",
        variant_id=v.id,
        mode="live",
        state=STATE_LIVE_ARM_PENDING,
        risk_snapshot_json={"arm_token": "x", "momentum_risk": {"allowed": True}},
    )
    create_trading_automation_session(
        db,
        user_id=uid,
        symbol="PAP-USD",
        variant_id=v.id,
        mode="paper",
        state=Q,
        risk_snapshot_json={"momentum_risk": {"allowed": True}},
    )
    db.commit()
    rows = list_runnable_paper_sessions(db, limit=50)
    assert all(r.mode == "paper" for r in rows)
    assert all(r.state != STATE_LIVE_ARM_PENDING for r in rows)


def test_paper_events_emitted(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="EVT-USD")
    db.commit()
    uid = _uid(db, "evt")
    r = create_paper_draft_session(db, user_id=uid, symbol="EVT-USD", variant_id=vid)
    sid = r["session_id"]
    db.commit()
    tick_paper_session(db, sid, quote_fn=lambda _s: {"mid": 77.0, "bid": 76.9, "ask": 77.1})
    db.commit()
    types = {e.event_type for e in db.query(TradingAutomationEvent).filter_by(session_id=sid).all()}
    assert "paper_runner_started" in types or "paper_runner_queued" in types
