"""Phase 8: live automation runner (guarded Coinbase adapter path)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_ENTERED,
    STATE_LIVE_ERROR,
    STATE_LIVE_PENDING_ENTRY,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
    assert_transition_live,
    can_transition_live,
)
from app.services.trading.momentum_neural.live_runner import (
    list_runnable_live_sessions,
    summarize_live_execution,
    tick_live_session,
)
from app.services.trading.momentum_neural.paper_runner import list_runnable_paper_sessions
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
from app.services.trading.venue.coinbase_spot import reset_duplicate_client_order_guard_for_tests
from app.services.trading.venue.protocol import FreshnessMeta, NormalizedOrder, NormalizedProduct, NormalizedTicker

from tests.test_momentum_paper_runner import _seed_live_eligible_row


def _uid(db: Session, name_suffix: str) -> int:
    u = User(name=f"LiveRun_{name_suffix}")
    db.add(u)
    db.commit()
    db.refresh(u)
    return int(u.id)


def _fresh() -> FreshnessMeta:
    return FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc), max_age_seconds=120.0)


def _mk_adapter():
    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_best_bid_ask.return_value = (
        NormalizedTicker(
            product_id="SOL-USD",
            bid=99.0,
            ask=101.0,
            mid=100.0,
            spread_bps=200.0,
            freshness=_fresh(),
        ),
        _fresh(),
    )
    prod = NormalizedProduct(
        product_id="SOL-USD",
        base_currency="SOL",
        quote_currency="USD",
        status="online",
        trading_disabled=False,
        cancel_only=False,
        limit_only=False,
        post_only=False,
        auction_mode=False,
        base_increment=0.001,
        base_min_size=0.001,
    )
    ad.get_product.return_value = (prod, _fresh())
    ad.place_market_order.return_value = {"ok": True, "order_id": "ord-entry-1", "client_order_id": "cid-e1"}
    ad.get_order.return_value = (
        NormalizedOrder(
            order_id="ord-entry-1",
            client_order_id="cid-e1",
            product_id="SOL-USD",
            side="buy",
            status="FILLED",
            order_type="market",
            filled_size=0.25,
            average_filled_price=100.5,
        ),
        _fresh(),
    )
    ad.cancel_order.return_value = {"ok": True, "raw": {}}
    return ad


def test_live_fsm_transition_rules() -> None:
    assert can_transition_live("armed_pending_runner", STATE_QUEUED_LIVE)
    assert not can_transition_live(STATE_QUEUED_LIVE, "armed_pending_runner")
    assert can_transition_live(STATE_LIVE_PENDING_ENTRY, STATE_LIVE_ENTERED)
    assert not can_transition_live(STATE_QUEUED_LIVE, STATE_LIVE_ENTERED)
    with pytest.raises(ValueError):
        assert_transition_live(STATE_QUEUED_LIVE, "armed_pending_runner")


def test_summarize_live_execution_helpers() -> None:
    assert summarize_live_execution({}) == {}
    assert summarize_live_execution(None) == {}  # type: ignore[arg-type]
    snap = {
        "momentum_live_execution": {
            "tick_count": 2,
            "entry_order_id": "o1",
            "position": {"quantity": 0.1, "avg_entry_price": 50.0, "notional_usd": 5.0},
        }
    }
    s = summarize_live_execution(snap)
    assert s.get("tick_count") == 2
    assert s.get("in_position") is True
    assert s.get("avg_entry_price") == 50.0


def test_list_runnable_live_ignores_paper(db: Session) -> None:
    from app.models.trading import MomentumStrategyVariant
    from app.services.trading.momentum_neural.persistence import (
        create_trading_automation_session,
        ensure_momentum_strategy_variants,
    )
    from app.services.trading.momentum_neural.paper_fsm import STATE_QUEUED as PQ

    uid = _uid(db, "mix")
    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()
    create_trading_automation_session(
        db,
        user_id=uid,
        symbol="PAP-USD",
        variant_id=v.id,
        mode="paper",
        state=PQ,
        risk_snapshot_json={RISK_SNAPSHOT_KEY: {"allowed": True}},
    )
    create_trading_automation_session(
        db,
        user_id=uid,
        symbol="LIV-USD",
        variant_id=v.id,
        mode="live",
        state=STATE_QUEUED_LIVE,
        risk_snapshot_json={RISK_SNAPSHOT_KEY: {"allowed": True}},
    )
    db.commit()
    live_rows = list_runnable_live_sessions(db, limit=50)
    assert all(r.mode == "live" for r in live_rows)
    paper_rows = list_runnable_paper_sessions(db, limit=50)
    assert all(r.mode == "paper" for r in paper_rows)


def test_tick_live_armed_to_watching(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="TL1-USD")
    db.commit()
    uid = _uid(db, "tl1")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="TL1-USD",
        variant_id=vid,
        mode="live",
        state="armed_pending_runner",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True, "evaluated_at_utc": "2026-01-01T00:00:00+00:00"},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 50, "max_hold_seconds": 3600},
        },
        correlation_id="c-live-1",
    )
    db.commit()
    ad = _mk_adapter()

    def factory():
        return ad

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        r1 = tick_live_session(db, sess.id, adapter_factory=factory)
    assert r1.get("ok")
    db.commit()
    db.refresh(sess)
    assert sess.state == STATE_QUEUED_LIVE

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        r2 = tick_live_session(db, sess.id, adapter_factory=factory)
    db.commit()
    db.refresh(sess)
    assert sess.state == STATE_WATCHING_LIVE


def test_kill_switch_blocks_before_entry(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="KS-USD")
    db.commit()
    uid = _uid(db, "ks")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="KS-USD",
        variant_id=vid,
        mode="live",
        state="armed_pending_runner",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
        },
    )
    db.commit()
    ad = _mk_adapter()

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=True):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    assert out.get("blocked") or sess.state == STATE_LIVE_ERROR
    assert sess.state == STATE_LIVE_ERROR
    ad.place_market_order.assert_not_called()


def test_live_execution_summary_persisted(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    vid, _ = _seed_live_eligible_row(db, symbol="SNP-USD")
    db.commit()
    uid = _uid(db, "snp")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="SNP-USD",
        variant_id=vid,
        mode="live",
        state=STATE_WATCHING_LIVE,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 80, "max_hold_seconds": 3600},
        },
    )
    db.commit()
    ad = _mk_adapter()
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        tick_live_session(db, sess.id, adapter_factory=lambda: ad)
        db.commit()
        db.refresh(sess)
    snap = sess.risk_snapshot_json or {}
    assert "momentum_live_execution" in snap
    assert int(snap["momentum_live_execution"].get("tick_count") or 0) >= 1


def test_dev_tick_endpoint_gated(client) -> None:
    r = client.post("/api/trading/momentum/live-runner/tick", json={"session_id": 1})
    assert r.status_code == 404
