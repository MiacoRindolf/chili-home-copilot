"""Phase 8: live automation runner (guarded Coinbase adapter path)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_ENTERED,
    STATE_LIVE_ERROR,
    STATE_LIVE_EXITED,
    STATE_LIVE_PENDING_ENTRY,
    STATE_LIVE_SCALING_OUT,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
    assert_transition_live,
    can_transition_live,
)
from app.services.trading.momentum_neural.live_runner import (
    _adaptive_live_max_spread_bps,
    _expected_move_bps_from_ohlcv,
    _notional_guard_multiplier,
    _quote_quality_block,
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
            bid=99.95,
            ask=100.05,
            mid=100.0,
            spread_bps=10.0,
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
            "last_exit_intent": {"reason": "stop"},
            "exit_execution_intents": [{"reason": "stop"}],
            "pending_exit_reason": "stop",
            "pending_exit_quantity": 0.1,
            "last_exit_pending_confirmation": {"why": "exit_fill_pending"},
            "last_partial_exit_reason": "target",
            "last_partial_exit_price": 51.0,
            "last_exit_notional_basis_usd": 5.0,
            "last_exit_return_bps": 200.0,
            "last_partial_exit_notional_basis_usd": 1.0,
            "last_partial_exit_return_bps": 100.0,
            "position": {"quantity": 0.1, "avg_entry_price": 50.0, "notional_usd": 5.0},
        }
    }
    s = summarize_live_execution(snap)
    assert s.get("tick_count") == 2
    assert s.get("in_position") is True
    assert s.get("avg_entry_price") == 50.0
    assert s.get("last_exit_intent") == {"reason": "stop"}
    assert s.get("exit_execution_intent_count") == 1
    assert s.get("pending_exit_reason") == "stop"
    assert s.get("pending_exit_quantity") == 0.1
    assert s.get("last_exit_pending_confirmation") == {"why": "exit_fill_pending"}
    assert s.get("last_partial_exit_reason") == "target"
    assert s.get("last_partial_exit_price") == 51.0
    assert s.get("last_exit_notional_basis_usd") == 5.0
    assert s.get("last_exit_return_bps") == 200.0
    assert s.get("last_partial_exit_notional_basis_usd") == 1.0
    assert s.get("last_partial_exit_return_bps") == 100.0


def test_quote_quality_block_preserves_zero_live_spread_cap(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_max_spread_bps_live", 0.0)
    fresh = _fresh()

    gate = _quote_quality_block(
        NormalizedTicker(
            product_id="SOL-USD",
            bid=99.995,
            ask=100.005,
            mid=100.0,
            spread_bps=1.0,
            freshness=fresh,
        ),
        fresh,
    )

    assert gate is not None
    assert gate["reason"] == "wide_bbo_spread"
    assert gate["spread_bps"] == 1.0
    assert gate["max_spread_bps"] == 0.0


def test_notional_guard_multiplier_preserves_zero_bps(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_order_notional_guard_bps", 0.0)

    assert _notional_guard_multiplier() == 1.0


def test_adaptive_max_spread_bps_floor_and_loosening() -> None:
    from app.services.trading.momentum_neural.risk_policy import adaptive_max_spread_bps

    base = 12.0
    # Unknown / non-finite / non-positive expected move -> base floor (no loosen).
    assert adaptive_max_spread_bps(base, None, 0.5) == base
    assert adaptive_max_spread_bps(base, 0.0, 0.5) == base
    assert adaptive_max_spread_bps(base, -5.0, 0.5) == base
    assert adaptive_max_spread_bps(base, float("nan"), 0.5) == base
    # Bad ratio -> base floor.
    assert adaptive_max_spread_bps(base, 400.0, 0.0) == base
    assert adaptive_max_spread_bps(base, 400.0, -1.0) == base
    # Low-vol instrument: ratio*move below the floor -> keep the floor (never tighten).
    assert adaptive_max_spread_bps(base, 10.0, 0.5) == base  # 0.5*10 = 5 < 12
    # Explosive instrument: ratio*move above the floor -> loosen proportionally.
    assert adaptive_max_spread_bps(base, 400.0, 0.5) == pytest.approx(200.0)


def test_adaptive_live_max_spread_bps_reads_settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_max_spread_bps_live", 12.0)
    monkeypatch.setattr(settings, "chili_momentum_risk_spread_to_expected_move_ratio", 0.5)
    assert _adaptive_live_max_spread_bps(None) == 12.0  # no move -> floor
    assert _adaptive_live_max_spread_bps(10.0) == 12.0  # quiet -> floor
    assert _adaptive_live_max_spread_bps(300.0) == pytest.approx(150.0)  # mover -> loosen


def test_expected_move_bps_from_ohlcv() -> None:
    import pandas as pd

    assert _expected_move_bps_from_ohlcv(None) is None
    assert _expected_move_bps_from_ohlcv(pd.DataFrame()) is None
    # Steady ~2% per-bar range around 100 -> ~200 bps expected move (ATR/close).
    price = 100.0
    rows = [
        {"High": price * 1.01, "Low": price * 0.99, "Close": price, "Volume": 1000.0}
        for _ in range(30)
    ]
    em = _expected_move_bps_from_ohlcv(pd.DataFrame(rows))
    assert em is not None
    assert em == pytest.approx(200.0, rel=0.1)


def test_quote_quality_block_adaptive_override_allows_wide_spread_on_mover() -> None:
    fresh = _fresh()
    tick = NormalizedTicker(
        product_id="MOV-USD",
        bid=99.65,
        ask=100.35,
        mid=100.0,
        spread_bps=70.0,
        freshness=fresh,
    )
    # Base floor (12 bps) blocks a 70 bps spread...
    blocked = _quote_quality_block(tick, fresh, max_spread_bps=12.0)
    assert blocked is not None and blocked["reason"] == "wide_bbo_spread"
    # ...but an adaptive tolerance from a high expected move (0.5 * 300 = 150)
    # lets the explosive mover through.
    assert _quote_quality_block(tick, fresh, max_spread_bps=150.0) is None


def test_live_exit_intent_records_packet_context(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    calls: list[tuple[int | None, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "record_packet_execution_intent",
        lambda _db, packet_id, payload: calls.append((packet_id, payload)),
    )
    sess = SimpleNamespace(
        id=42,
        state=STATE_LIVE_ENTERED,
        symbol="SOL-USD",
        variant_id=7,
        venue="coinbase",
        execution_family="coinbase_spot",
    )
    le = {
        "entry_decision_packet_id": 123,
        "position": {
            "quantity": 0.25,
            "avg_entry_price": 100.0,
            "stop_price": 98.0,
            "target_price": 106.0,
            "opened_at_utc": "2026-01-01T00:00:00",
        },
    }

    live_runner_mod._record_live_exit_intent_safe(
        MagicMock(),
        sess,
        le=le,
        reason="stop",
        product_id="SOL-USD",
        quantity=0.25,
        client_order_id="cid-exit",
        bid=97.5,
        ask=98.0,
        mid=97.75,
        extra={"stop_price": 98.0},
    )

    assert calls and calls[0][0] == 123
    payload = calls[0][1]
    assert payload["surface"] == "momentum_live_runner_exit"
    assert payload["side"] == "sell"
    assert payload["reason"] == "stop"
    assert payload["client_order_id"] == "cid-exit"
    assert payload["reference_notional_usd"] == pytest.approx(24.375)
    assert le["last_exit_intent"]["reason"] == "stop"
    assert le["exit_execution_intents"][-1]["product_id"] == "SOL-USD"


def test_live_exit_submit_failure_does_not_flatten_local_position(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    sess = SimpleNamespace(
        id=43,
        state=STATE_LIVE_ENTERED,
        risk_snapshot_json={},
        correlation_id="corr-exit-fail",
    )
    le = {
        "exit_client_order_id": "cid-exit",
        "position": {
            "quantity": 0.25,
            "avg_entry_price": 100.0,
        },
    }

    ok = live_runner_mod._live_exit_submit_succeeded(
        MagicMock(),
        sess,
        le=le,
        result={"ok": False, "error": "venue_down"},
        reason="stop",
    )

    assert ok is False
    assert le["position"]["quantity"] == 0.25
    assert le["last_exit_submit_failed"]["reason"] == "stop"
    assert sess.risk_snapshot_json["momentum_live_execution"]["position"]["quantity"] == 0.25
    assert events and events[-1][0] == "live_exit_submit_failed"


def test_live_exit_poll_waits_for_open_order(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    sess = SimpleNamespace(id=44, state=STATE_LIVE_ENTERED, risk_snapshot_json={}, correlation_id="corr-pending")
    le = {
        "exit_order_id": "ord-exit-open",
        "position": {"quantity": 0.25, "avg_entry_price": 100.0},
    }
    adapter = SimpleNamespace(
        get_order=lambda _oid: (
            NormalizedOrder(
                order_id="ord-exit-open",
                client_order_id="cid-exit",
                product_id="SOL-USD",
                side="sell",
                status="OPEN",
                order_type="market",
                filled_size=0.0,
                average_filled_price=None,
            ),
            _fresh(),
        )
    )

    out = live_runner_mod._poll_live_exit_fill(
        MagicMock(),
        sess,
        adapter,
        le=le,
        reason="stop",
        quantity=0.25,
    )

    assert out["pending"] is True
    assert le["position"]["quantity"] == 0.25
    assert le["last_exit_pending_confirmation"]["why"] == "exit_fill_pending"
    assert events and events[-1][0] == "live_exit_pending_confirmation"


def test_confirmed_live_exit_is_the_only_flatten_path(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(live_runner_mod, "_record_live_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(live_runner_mod, "_finalize_live_decision_after_exit", lambda *a, **k: None)
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    sess = SimpleNamespace(
        id=45,
        state=STATE_LIVE_ENTERED,
        mode="live",
        risk_snapshot_json={},
        correlation_id="corr-confirmed",
    )
    le = {
        "exit_order_id": "ord-exit-filled",
        "pending_exit_reason": "stop",
        "pending_exit_quantity": 0.25,
        "position": {"quantity": 0.25, "avg_entry_price": 100.0},
    }

    pnl = live_runner_mod._complete_confirmed_live_exit(
        MagicMock(),
        sess,
        le=le,
        quantity=0.25,
        entry_price=100.0,
        fill_price=99.0,
        reason="stop",
        slip_bps=6.0,
    )

    assert pnl == pytest.approx(-0.25)
    assert sess.state == STATE_LIVE_EXITED
    assert le["position"] is None
    assert "pending_exit_reason" not in le
    assert le["last_exit_notional_basis_usd"] == pytest.approx(25.0)
    assert le["last_exit_return_bps"] == pytest.approx(-100.0)
    assert sess.risk_snapshot_json["momentum_live_execution"]["last_exit_reason"] == "stop"
    assert events and events[-1][0] == "live_exit_filled"


def test_terminal_partial_live_exit_reduces_position_without_flattening(monkeypatch) -> None:
    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(live_runner_mod, "_record_live_partial_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(
        live_runner_mod,
        "_emit",
        lambda _db, _sess, event_type, payload: events.append((event_type, payload)),
    )
    sess = SimpleNamespace(
        id=46,
        state=STATE_LIVE_SCALING_OUT,
        mode="live",
        risk_snapshot_json={},
        correlation_id="corr-partial",
    )
    le = {
        "exit_order_id": "ord-exit-partial",
        "pending_exit_reason": "target",
        "pending_exit_quantity": 0.25,
        "position": {"quantity": 0.25, "avg_entry_price": 100.0},
    }

    pnl = live_runner_mod._apply_confirmed_live_partial_exit(
        MagicMock(),
        sess,
        le=le,
        filled_quantity=0.1,
        entry_price=100.0,
        fill_price=101.0,
        reason="target",
    )

    assert pnl == pytest.approx(0.1)
    assert sess.state == STATE_LIVE_SCALING_OUT
    assert le["position"]["quantity"] == pytest.approx(0.15)
    assert "pending_exit_reason" not in le
    assert le["last_partial_exit_notional_basis_usd"] == pytest.approx(10.0)
    assert le["last_partial_exit_return_bps"] == pytest.approx(100.0)
    assert sess.risk_snapshot_json["momentum_live_execution"]["position"]["quantity"] == pytest.approx(0.15)
    assert events and events[-1][0] == "live_partial_exit_filled"


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


def test_wide_live_bbo_blocks_market_entry_without_error(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_risk_max_spread_bps_live", 12.0)
    vid, _ = _seed_live_eligible_row(db, symbol="WID-USD")
    db.commit()
    uid = _uid(db, "wide")
    from app.services.trading.momentum_neural.persistence import create_trading_automation_session

    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol="WID-USD",
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_PENDING_ENTRY,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
        },
    )
    db.commit()
    ad = _mk_adapter()
    fresh = _fresh()
    ad.get_best_bid_ask.return_value = (
        NormalizedTicker(
            product_id="WID-USD",
            bid=99.0,
            ask=101.0,
            mid=100.0,
            spread_bps=200.0,
            freshness=fresh,
        ),
        fresh,
    )

    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)

    assert out == {"ok": True, "blocked": True, "reason": "wide_bbo_spread"}
    assert sess.state == STATE_WATCHING_LIVE
    ad.place_market_order.assert_not_called()
    gate = (sess.risk_snapshot_json or {})["momentum_live_execution"]["last_quote_quality_gate"]
    assert gate["spread_bps"] == 200.0


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
