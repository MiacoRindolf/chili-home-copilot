"""Tests for the P0.7 stuck-order watchdog.

Covers the three outcomes:
  * still-pending past timeout → cancel is issued
  * broker says terminal → local Trade mirrors it, no cancel
  * broker doesn't know the order → Trade marked rejected
And two non-actions:
  * within-timeout → no broker call, no state change
  * disabled feature flag → no-op tick
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy import text

from app import models
from app.models.trading import AutoTraderRun, Trade, TradingExecutionEvent
from app.services.trading import stuck_order_watchdog as wd
from app.services.trading.venue.protocol import FreshnessMeta, NormalizedOrder, NormalizedTicker


def _make_trade(
    db,
    *,
    user_id,
    broker_order_id,
    broker_status,
    entry_date,
    scope="auto_trader_v1",
    broker_source="robinhood",
    status="open",
    quantity=1.0,
    remaining_quantity=None,
    indicator_snapshot=None,
):
    t = Trade(
        user_id=user_id,
        ticker="ZZTEST",
        direction="long",
        entry_price=10.0,
        quantity=quantity,
        entry_date=entry_date,
        status=status,
        broker_source=broker_source,
        broker_order_id=broker_order_id,
        broker_status=broker_status,
        remaining_quantity=remaining_quantity,
        management_scope=scope,
        indicator_snapshot=indicator_snapshot,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _fake_normalized(
    status: str,
    order_id: str = "abc",
    *,
    filled_size: float = 0.0,
) -> NormalizedOrder:
    return NormalizedOrder(
        order_id=order_id,
        client_order_id=None,
        product_id="ZZTEST",
        side="buy",
        status=status,
        order_type="market",
        filled_size=filled_size,
        average_filled_price=None,
        created_time=None,
        raw={"state": status},
    )


def _allow_replacement_buy_entry_gate(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.trading.governance.is_kill_switch_active",
        lambda: False,
    )
    monkeypatch.setattr(
        "app.services.trading.portfolio_risk.circuit_breaker_entry_block_reason",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        wd,
        "coinbase_live_probation_check",
        lambda *_args, **_kwargs: SimpleNamespace(
            allowed=True,
            reason="coinbase_probation:passed",
            snapshot={},
        ),
    )


def test_watchdog_disabled_returns_skipped(db, monkeypatch):
    cfg = SimpleNamespace(chili_stuck_order_watchdog_enabled=False)
    monkeypatch.setattr(wd, "settings", cfg)

    out = wd.tick_stuck_order_watchdog(db)
    assert out.get("skipped") is True
    assert out.get("reason") == "disabled"


def test_within_timeout_leaves_trade_alone(db, monkeypatch):
    u = models.User(name="stuck_wd_u1")
    db.add(u)
    db.flush()

    # Entry 60s ago; market timeout is 300s — still within window.
    t = _make_trade(
        db,
        user_id=u.id,
        broker_order_id="rh-within",
        broker_status="queued",
        entry_date=datetime.utcnow() - timedelta(seconds=60),
    )

    cfg = SimpleNamespace(
        chili_stuck_order_watchdog_enabled=True,
        chili_stuck_order_market_timeout_seconds=300,
        chili_stuck_order_limit_timeout_seconds=1800,
    )
    monkeypatch.setattr(wd, "settings", cfg)

    # Adapter should never be consulted for a within-timeout candidate.
    fake_adapter = MagicMock()
    monkeypatch.setattr(wd, "_get_adapter", lambda _src: fake_adapter)

    out = wd.tick_stuck_order_watchdog(db)

    assert out["inspected"] == 1
    assert out["outcomes"].get("within_timeout") == 1
    fake_adapter.get_order.assert_not_called()
    fake_adapter.cancel_order.assert_not_called()

    db.refresh(t)
    assert t.status == "open"
    assert t.broker_status == "queued"


def test_past_timeout_still_pending_triggers_cancel(db, monkeypatch):
    u = models.User(name="stuck_wd_u2")
    db.add(u)
    db.flush()

    t = _make_trade(
        db,
        user_id=u.id,
        broker_order_id="rh-stuck",
        broker_status="queued",
        entry_date=datetime.utcnow() - timedelta(seconds=900),  # 15 minutes ago
    )

    cfg = SimpleNamespace(
        chili_stuck_order_watchdog_enabled=True,
        chili_stuck_order_market_timeout_seconds=300,
        chili_stuck_order_limit_timeout_seconds=1800,
    )
    monkeypatch.setattr(wd, "settings", cfg)

    fake_adapter = MagicMock()
    fake_adapter.get_order.return_value = (
        _fake_normalized("ack", order_id="rh-stuck"),
        FreshnessMeta(retrieved_at_utc=datetime.utcnow(), max_age_seconds=15.0),
    )
    fake_adapter.cancel_order.return_value = {"ok": True, "raw": {}}
    monkeypatch.setattr(wd, "_get_adapter", lambda _src: fake_adapter)

    out = wd.tick_stuck_order_watchdog(db)

    assert out["outcomes"].get("cancel_requested") == 1
    fake_adapter.cancel_order.assert_called_once_with("rh-stuck")

    db.refresh(t)
    assert t.status == "working"
    assert t.broker_status == "cancel_requested"
    assert t.last_broker_sync is not None
    entry = t.indicator_snapshot["entry_execution"]
    assert entry["cancel_requested"] is True
    assert entry["cancel_request_reason"] == "stuck_order_watchdog_timeout"
    assert entry["cancel_request_broker_truth"] == "unknown"
    event = (
        db.query(TradingExecutionEvent)
        .filter(TradingExecutionEvent.trade_id == t.id)
        .one()
    )
    assert event.event_type == "cancel_request"
    assert event.status == "cancel_requested"
    assert event.payload_json["broker_truth"] == "unknown"

    second = wd.tick_stuck_order_watchdog(db)

    assert second["outcomes"].get("cancel_request_pending") == 1
    fake_adapter.cancel_order.assert_called_once_with("rh-stuck")


def test_broker_reports_terminal_mirrors_state_no_cancel(db, monkeypatch):
    """If the broker already has a terminal state, the watchdog should just
    update the local row — never issue a cancel (that would error on an
    already-filled order)."""
    u = models.User(name="stuck_wd_u3")
    db.add(u)
    db.flush()

    t = _make_trade(
        db,
        user_id=u.id,
        broker_order_id="rh-filled",
        broker_status="queued",
        entry_date=datetime.utcnow() - timedelta(seconds=900),
    )

    cfg = SimpleNamespace(
        chili_stuck_order_watchdog_enabled=True,
        chili_stuck_order_market_timeout_seconds=300,
        chili_stuck_order_limit_timeout_seconds=1800,
    )
    monkeypatch.setattr(wd, "settings", cfg)

    fake_adapter = MagicMock()
    fake_adapter.get_order.return_value = (
        _fake_normalized("filled", order_id="rh-filled"),
        FreshnessMeta(retrieved_at_utc=datetime.utcnow(), max_age_seconds=15.0),
    )
    monkeypatch.setattr(wd, "_get_adapter", lambda _src: fake_adapter)

    out = wd.tick_stuck_order_watchdog(db)

    assert out["outcomes"].get("mirrored:filled") == 1
    fake_adapter.cancel_order.assert_not_called()

    db.refresh(t)
    # Filled entry stays status='open' (position is open); only broker_status
    # flips.
    assert t.status == "open"
    assert t.broker_status == "filled"


def test_option_working_order_promotes_to_open_from_position_truth(monkeypatch):
    commits = {"count": 0}
    fake_db = SimpleNamespace(commit=lambda: commits.__setitem__("count", commits["count"] + 1))
    now = datetime.utcnow()
    t = SimpleNamespace(
        id=90210,
        ticker="ZZTEST",
        broker_order_id="rh-opt-accepted",
        management_scope="auto_trader_v1",
        direction="long",
        status="working",
        broker_status="accepted",
        quantity=1.0,
        filled_quantity=None,
        remaining_quantity=1.0,
        submitted_at=now,
        entry_date=now,
        entry_price=1.25,
        avg_fill_price=None,
        tca_reference_entry_price=715.37,
        tca_entry_slippage_bps=None,
        last_broker_sync=None,
        filled_at=None,
        first_fill_at=None,
        last_fill_at=None,
        indicator_snapshot={
            "breakout_alert": {
                "asset_type": "options",
                "option_meta": {
                    "underlying": "ZZTEST",
                    "expiration": "2026-06-19",
                    "strike": 105.0,
                    "option_type": "call",
                    "limit_price": 1.25,
                },
            },
            "entry_execution": {"active_order_type": "option_limit"},
        },
    )
    monkeypatch.setattr(
        wd,
        "_get_options_adapter",
        lambda: SimpleNamespace(
            is_enabled=lambda: True,
            get_open_positions=lambda: [
                {
                    "chain_symbol": "ZZTEST",
                    "expiration_date": "2026-06-19",
                    "strike_price": "105.0",
                    "type": "call",
                    "quantity": "1",
                    "average_price": "1.23",
                }
            ],
        ),
    )

    outcome = wd._process_option_position_truth(fake_db, t, now)

    assert outcome == "option_position_verified"
    assert commits["count"] == 1
    assert t.status == "open"
    assert t.broker_status == "filled"
    assert t.filled_quantity == 1.0
    assert t.remaining_quantity == 0.0
    assert t.entry_price == 1.23
    assert t.tca_reference_entry_price == 1.25
    assert t.tca_entry_slippage_bps == -160.0
    entry = t.indicator_snapshot["entry_execution"]
    assert entry["option_position_verified"] is True
    assert entry["tca_reference_entry_price"] == 1.25
    assert entry["tca_reference_domain"] == "option_premium"


def test_option_working_order_without_position_stays_working(monkeypatch):
    commits = {"count": 0}
    fake_db = SimpleNamespace(commit=lambda: commits.__setitem__("count", commits["count"] + 1))
    now = datetime.utcnow()
    t = SimpleNamespace(
        id=90211,
        ticker="ZZTEST",
        broker_order_id="rh-opt-resting",
        management_scope="auto_trader_v1",
        status="working",
        broker_status="accepted",
        quantity=1.0,
        filled_quantity=None,
        remaining_quantity=1.0,
        submitted_at=now,
        entry_date=now,
        entry_price=1.25,
        avg_fill_price=None,
        last_broker_sync=None,
        filled_at=None,
        first_fill_at=None,
        last_fill_at=None,
        indicator_snapshot={
            "breakout_alert": {
                "asset_type": "options",
                "option_meta": {
                    "underlying": "ZZTEST",
                    "expiration": "2026-06-19",
                    "strike": 105.0,
                    "option_type": "call",
                    "limit_price": 1.25,
                },
            },
            "entry_execution": {"active_order_type": "option_limit"},
        },
    )
    monkeypatch.setattr(
        wd,
        "_get_options_adapter",
        lambda: SimpleNamespace(
            is_enabled=lambda: True,
            cancel=lambda _order_id: (_ for _ in ()).throw(
                AssertionError("fresh option order should not be cancelled")
            ),
            get_open_positions=lambda: [],
        ),
    )

    outcome = wd._process_option_position_truth(fake_db, t, now)

    assert outcome == "option_position_not_found"
    assert commits["count"] == 0
    assert t.status == "working"
    assert t.broker_status == "accepted"
    assert t.filled_quantity is None
    assert t.remaining_quantity == 1.0


def test_option_working_order_times_out_and_cancels_when_no_position(monkeypatch):
    commits = {"count": 0}
    fake_db = SimpleNamespace(commit=lambda: commits.__setitem__("count", commits["count"] + 1))
    now = datetime.utcnow()
    submitted_at = now - timedelta(seconds=3600)
    cancelled = {"order_id": None}
    t = SimpleNamespace(
        id=90212,
        ticker="ZZTEST",
        broker_order_id="rh-opt-timeout",
        management_scope="auto_trader_v1",
        status="working",
        broker_status="accepted",
        quantity=1.0,
        filled_quantity=None,
        remaining_quantity=1.0,
        submitted_at=submitted_at,
        entry_date=submitted_at,
        entry_price=1.25,
        avg_fill_price=None,
        last_broker_sync=None,
        filled_at=None,
        first_fill_at=None,
        last_fill_at=None,
        exit_reason=None,
        indicator_snapshot={
            "breakout_alert": {
                "asset_type": "options",
                "option_meta": {
                    "underlying": "ZZTEST",
                    "expiration": "2026-06-19",
                    "strike": 105.0,
                    "option_type": "call",
                    "limit_price": 1.25,
                },
            },
            "entry_execution": {"active_order_type": "option_limit"},
        },
    )

    def _cancel(order_id: str):
        cancelled["order_id"] = order_id
        return {"ok": True}

    monkeypatch.setattr(
        wd,
        "_get_options_adapter",
        lambda: SimpleNamespace(
            is_enabled=lambda: True,
            get_open_positions=lambda: [],
            cancel=_cancel,
        ),
    )

    outcome = wd._process_option_position_truth(fake_db, t, now)

    assert outcome == "option_entry_timeout_cancelled"
    assert cancelled["order_id"] == "rh-opt-timeout"
    assert commits["count"] == 1
    assert t.status == "cancelled"
    assert t.broker_status == "cancelled"
    assert t.exit_reason == "option_entry_timeout_no_position"
    entry = t.indicator_snapshot["entry_execution"]
    assert entry["option_entry_cancel_reason"] == "timeout_no_position"


def test_option_partial_position_timeout_cancels_residual_and_keeps_held_contract(
    monkeypatch,
):
    commits = {"count": 0}
    fake_db = SimpleNamespace(commit=lambda: commits.__setitem__("count", commits["count"] + 1))
    now = datetime.utcnow()
    submitted_at = now - timedelta(seconds=3600)
    cancelled = {"order_id": None}
    t = SimpleNamespace(
        id=90213,
        ticker="ZZTEST",
        broker_order_id="rh-opt-partial-timeout",
        management_scope="auto_trader_v1",
        status="working",
        broker_status="partially_filled",
        quantity=2.0,
        filled_quantity=1.0,
        remaining_quantity=1.0,
        submitted_at=submitted_at,
        entry_date=submitted_at,
        entry_price=1.25,
        avg_fill_price=None,
        last_broker_sync=None,
        filled_at=None,
        first_fill_at=None,
        last_fill_at=None,
        exit_reason=None,
        indicator_snapshot={
            "breakout_alert": {
                "asset_type": "options",
                "option_meta": {
                    "underlying": "ZZTEST",
                    "expiration": "2026-06-19",
                    "strike": 105.0,
                    "option_type": "call",
                    "limit_price": 1.25,
                },
            },
            "entry_execution": {"active_order_type": "option_limit"},
        },
    )

    def _cancel(order_id: str):
        cancelled["order_id"] = order_id
        return {"ok": True}

    monkeypatch.setattr(
        wd,
        "_get_options_adapter",
        lambda: SimpleNamespace(
            is_enabled=lambda: True,
            get_open_positions=lambda: [
                {
                    "chain_symbol": "ZZTEST",
                    "expiration_date": "2026-06-19",
                    "strike_price": "105.0",
                    "type": "call",
                    "quantity": "1",
                    "average_price": "1.23",
                }
            ],
            cancel=_cancel,
        ),
    )

    outcome = wd._process_option_position_truth(fake_db, t, now)

    assert outcome == "option_partial_position_timeout_cancelled_open"
    assert cancelled["order_id"] == "rh-opt-partial-timeout"
    assert commits["count"] == 1
    assert t.status == "open"
    assert t.broker_status == "partially_filled_cancelled"
    assert t.quantity == 1.0
    assert t.filled_quantity == 1.0
    assert t.remaining_quantity == 0.0
    assert t.entry_price == 1.23
    entry = t.indicator_snapshot["entry_execution"]
    assert entry["option_position_partial"] is True
    assert entry["option_position_requested_quantity"] == 2.0
    assert entry["option_position_quantity"] == 1.0
    assert entry["option_position_residual_cancelled"] is True
    assert entry["option_entry_cancel_reason"] == "partial_timeout_no_full_position"


def test_broker_unknown_order_marks_rejected(db, monkeypatch):
    u = models.User(name="stuck_wd_u4")
    db.add(u)
    db.flush()

    t = _make_trade(
        db,
        user_id=u.id,
        broker_order_id="rh-ghost",
        broker_status="queued",
        entry_date=datetime.utcnow() - timedelta(seconds=3601),
    )

    cfg = SimpleNamespace(
        chili_stuck_order_watchdog_enabled=True,
        chili_stuck_order_market_timeout_seconds=300,
        chili_stuck_order_limit_timeout_seconds=1800,
    )
    monkeypatch.setattr(wd, "settings", cfg)

    fake_adapter = MagicMock()
    fake_adapter.get_order.return_value = (
        None,
        FreshnessMeta(retrieved_at_utc=datetime.utcnow(), max_age_seconds=15.0),
    )
    monkeypatch.setattr(wd, "_get_adapter", lambda _src: fake_adapter)

    out = wd.tick_stuck_order_watchdog(db)

    assert out["outcomes"].get("unknown_at_venue_rejected") == 1
    fake_adapter.cancel_order.assert_not_called()

    db.refresh(t)
    assert t.status == "rejected"
    assert t.broker_status == "unknown"


def test_limit_timeout_applies_to_non_autotrader_scope(db, monkeypatch):
    """A manual/broker_sync trade should use the 30-min limit timeout, not
    the 5-min market timeout."""
    u = models.User(name="stuck_wd_u5")
    db.add(u)
    db.flush()

    # 400s ago — past the market (300s) timeout but within the limit (1800s).
    t = _make_trade(
        db,
        user_id=u.id,
        broker_order_id="rh-manual-1",
        broker_status="queued",
        entry_date=datetime.utcnow() - timedelta(seconds=400),
        scope="manual",
    )

    cfg = SimpleNamespace(
        chili_stuck_order_watchdog_enabled=True,
        chili_stuck_order_market_timeout_seconds=300,
        chili_stuck_order_limit_timeout_seconds=1800,
    )
    monkeypatch.setattr(wd, "settings", cfg)

    fake_adapter = MagicMock()
    monkeypatch.setattr(wd, "_get_adapter", lambda _src: fake_adapter)

    out = wd.tick_stuck_order_watchdog(db)

    assert out["outcomes"].get("within_timeout") == 1
    fake_adapter.get_order.assert_not_called()

    db.refresh(t)
    assert t.status == "open"


def test_coinbase_maker_first_waits_for_fallback_timeout(db, monkeypatch):
    u = models.User(name="stuck_wd_cb_wait")
    db.add(u)
    db.flush()

    t = _make_trade(
        db,
        user_id=u.id,
        broker_order_id="cb-maker-wait",
        broker_status="accepted",
        entry_date=datetime.utcnow() - timedelta(seconds=60),
        broker_source="coinbase",
        status="working",
        remaining_quantity=1.0,
        indicator_snapshot={
            "entry_execution": {
                "active_order_type": "limit_post_only",
                "coinbase_maker_only": True,
                "entry_edge_expected_net_pct": 5.0,
                "cost_gate_fee_bps": 120,
            }
        },
    )

    cfg = SimpleNamespace(
        chili_stuck_order_watchdog_enabled=True,
        chili_stuck_order_market_timeout_seconds=300,
        chili_stuck_order_limit_timeout_seconds=1800,
        chili_coinbase_maker_first_fallback_enabled=True,
        chili_coinbase_maker_first_fallback_after_seconds=120,
    )
    monkeypatch.setattr(wd, "settings", cfg)

    fake_adapter = MagicMock()
    monkeypatch.setattr(wd, "_get_adapter", lambda _src: fake_adapter)

    out = wd.tick_stuck_order_watchdog(db)

    assert out["outcomes"].get("within_timeout") == 1
    fake_adapter.get_order.assert_not_called()
    db.refresh(t)
    assert t.broker_order_id == "cb-maker-wait"


def test_coinbase_maker_first_falls_back_to_takerable_limit_when_edge_survives(db, monkeypatch):
    _allow_replacement_buy_entry_gate(monkeypatch)
    u = models.User(name="stuck_wd_cb_fallback")
    db.add(u)
    db.flush()

    t = _make_trade(
        db,
        user_id=u.id,
        broker_order_id="cb-maker-old",
        broker_status="accepted",
        entry_date=datetime.utcnow() - timedelta(seconds=180),
        broker_source="coinbase",
        status="working",
        quantity=2.0,
        remaining_quantity=2.0,
        indicator_snapshot={
            "entry_execution": {
                "active_order_type": "limit_post_only",
                "coinbase_maker_only": True,
                "entry_edge_expected_net_pct": 5.0,
                "cost_gate_fee_bps": 120,
            }
        },
    )
    db.add(
        AutoTraderRun(
            user_id=u.id,
            ticker="ZZTEST",
            decision="placed",
            reason="submitted",
            trade_id=t.id,
            rule_snapshot={"entry_edge_expected_net_pct": 5.0, "cost_gate_fee_bps": 120},
            management_scope="auto_trader_v1",
        )
    )
    db.commit()

    cfg = SimpleNamespace(
        chili_stuck_order_watchdog_enabled=True,
        chili_stuck_order_market_timeout_seconds=300,
        chili_stuck_order_limit_timeout_seconds=1800,
        chili_coinbase_maker_first_fallback_enabled=True,
        chili_coinbase_maker_first_fallback_after_seconds=120,
        chili_coinbase_maker_first_min_net_after_cost_pct=0.0,
        chili_coinbase_maker_first_taker_price_buffer_bps=10.0,
        chili_coinbase_taker_fee_bps_round_trip=120,
        chili_min_edge_safety_buffer_bps=30,
    )
    monkeypatch.setattr(wd, "settings", cfg)

    fake_adapter = MagicMock()
    fake_adapter.get_order.return_value = (
        _fake_normalized("open", order_id="cb-maker-old"),
        FreshnessMeta(retrieved_at_utc=datetime.utcnow(), max_age_seconds=15.0),
    )
    fake_adapter.get_best_bid_ask.return_value = (
        NormalizedTicker(product_id="ZZTEST", bid=100.0, ask=100.1, spread_bps=10.0),
        FreshnessMeta(retrieved_at_utc=datetime.utcnow(), max_age_seconds=15.0),
    )
    fake_adapter.cancel_order.return_value = {"ok": True}
    fake_adapter.place_limit_order_gtc.return_value = {
        "ok": True,
        "order_id": "cb-fallback-new",
        "client_order_id": "fb-client",
        "base_size": "2.0",
        "limit_price": "100.2001",
    }
    monkeypatch.setattr(wd, "_get_adapter", lambda _src: fake_adapter)

    out = wd.tick_stuck_order_watchdog(db)

    assert out["outcomes"].get("maker_first_fallback_submitted") == 1
    fake_adapter.cancel_order.assert_called_once_with("cb-maker-old")
    fake_adapter.place_limit_order_gtc.assert_called_once()

    db.refresh(t)
    assert t.status == "working"
    assert t.broker_status == "accepted"
    assert t.broker_order_id == "cb-fallback-new"
    entry = t.indicator_snapshot["entry_execution"]
    assert entry["active_order_type"] == "limit_takerable"
    assert entry["maker_first_fallback_submitted"] is True
    assert entry["maker_first_original_order_id"] == "cb-maker-old"


def test_coinbase_maker_first_blocks_replacement_buy_when_breaker_active(db, monkeypatch):
    u = models.User(name="stuck_wd_cb_breaker_block")
    db.add(u)
    db.flush()

    t = _make_trade(
        db,
        user_id=u.id,
        broker_order_id="cb-maker-breaker",
        broker_status="accepted",
        entry_date=datetime.utcnow() - timedelta(seconds=180),
        broker_source="coinbase",
        status="working",
        quantity=2.0,
        remaining_quantity=2.0,
        indicator_snapshot={
            "entry_execution": {
                "active_order_type": "limit_post_only",
                "coinbase_maker_only": True,
                "entry_edge_expected_net_pct": 5.0,
                "cost_gate_fee_bps": 120,
            }
        },
    )

    cfg = SimpleNamespace(
        chili_stuck_order_watchdog_enabled=True,
        chili_stuck_order_market_timeout_seconds=300,
        chili_stuck_order_limit_timeout_seconds=1800,
        chili_coinbase_maker_first_fallback_enabled=True,
        chili_coinbase_maker_first_fallback_after_seconds=120,
    )
    monkeypatch.setattr(wd, "settings", cfg)
    monkeypatch.setattr(
        "app.services.trading.governance.is_kill_switch_active",
        lambda: False,
    )
    monkeypatch.setattr(
        "app.services.trading.portfolio_risk.circuit_breaker_entry_block_reason",
        lambda *_args, **_kwargs: "Circuit breaker active: test_breaker",
    )

    fake_adapter = MagicMock()
    fake_adapter.get_order.return_value = (
        _fake_normalized("open", order_id="cb-maker-breaker"),
        FreshnessMeta(retrieved_at_utc=datetime.utcnow(), max_age_seconds=15.0),
    )
    monkeypatch.setattr(wd, "_get_adapter", lambda _src: fake_adapter)

    out = wd.tick_stuck_order_watchdog(db)

    assert out["outcomes"].get("maker_first_fallback_blocked_entry_gate") == 1
    fake_adapter.get_best_bid_ask.assert_not_called()
    fake_adapter.cancel_order.assert_not_called()
    fake_adapter.place_limit_order_gtc.assert_not_called()

    db.refresh(t)
    assert t.status == "working"
    assert t.broker_status == "accepted"
    assert t.broker_order_id == "cb-maker-breaker"
    entry = t.indicator_snapshot["entry_execution"]
    assert entry["maker_first_fallback_decision"] == "blocked_entry_gate"
    assert entry["maker_first_fallback_blocked_reason"] == (
        "Circuit breaker active: test_breaker"
    )
    audit = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.trade_id == t.id)
        .order_by(AutoTraderRun.id.desc())
        .first()
    )
    assert audit is not None
    assert audit.decision == "blocked"
    assert audit.reason == (
        "maker_first_fallback_blocked:Circuit breaker active: test_breaker"
    )


def test_coinbase_maker_first_blocks_taker_fallback_on_probation(db, monkeypatch):
    u = models.User(name="stuck_wd_cb_probation_block")
    db.add(u)
    db.flush()

    t = _make_trade(
        db,
        user_id=u.id,
        broker_order_id="cb-maker-probation",
        broker_status="accepted",
        entry_date=datetime.utcnow() - timedelta(seconds=180),
        broker_source="coinbase",
        status="working",
        quantity=2.0,
        remaining_quantity=2.0,
        indicator_snapshot={
            "entry_execution": {
                "active_order_type": "limit_post_only",
                "coinbase_maker_only": True,
                "entry_edge_expected_net_pct": 5.0,
                "cost_gate_fee_bps": 120,
            }
        },
    )

    cfg = SimpleNamespace(
        chili_stuck_order_watchdog_enabled=True,
        chili_stuck_order_market_timeout_seconds=300,
        chili_stuck_order_limit_timeout_seconds=1800,
        chili_coinbase_maker_first_fallback_enabled=True,
        chili_coinbase_maker_first_fallback_after_seconds=120,
    )
    monkeypatch.setattr(wd, "settings", cfg)
    monkeypatch.setattr(
        "app.services.trading.governance.is_kill_switch_active",
        lambda: False,
    )
    monkeypatch.setattr(
        "app.services.trading.portfolio_risk.circuit_breaker_entry_block_reason",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        wd,
        "coinbase_live_probation_check",
        lambda *_args, **_kwargs: SimpleNamespace(
            allowed=False,
            reason="coinbase_probation:low_confidence_exit_provenance",
            snapshot={"closed_count": 264},
        ),
    )

    fake_adapter = MagicMock()
    fake_adapter.get_order.return_value = (
        _fake_normalized("open", order_id="cb-maker-probation"),
        FreshnessMeta(retrieved_at_utc=datetime.utcnow(), max_age_seconds=15.0),
    )
    monkeypatch.setattr(wd, "_get_adapter", lambda _src: fake_adapter)

    out = wd.tick_stuck_order_watchdog(db)

    assert out["outcomes"].get("maker_first_fallback_blocked_entry_gate") == 1
    fake_adapter.get_best_bid_ask.assert_not_called()
    fake_adapter.cancel_order.assert_not_called()
    fake_adapter.place_limit_order_gtc.assert_not_called()

    db.refresh(t)
    assert t.status == "working"
    assert t.broker_status == "accepted"
    assert t.broker_order_id == "cb-maker-probation"
    entry = t.indicator_snapshot["entry_execution"]
    assert entry["maker_first_fallback_decision"] == "blocked_entry_gate"
    assert entry["maker_first_fallback_blocked_reason"] == (
        "coinbase_probation:low_confidence_exit_provenance"
    )


def test_coinbase_maker_first_partial_fill_reprices_remaining_after_timeout(db, monkeypatch):
    _allow_replacement_buy_entry_gate(monkeypatch)
    u = models.User(name="stuck_wd_cb_partial_reprice")
    db.add(u)
    db.flush()

    t = _make_trade(
        db,
        user_id=u.id,
        broker_order_id="cb-maker-partial",
        broker_status="open",
        entry_date=datetime.utcnow() - timedelta(seconds=180),
        broker_source="coinbase",
        status="working",
        quantity=2.0,
        remaining_quantity=2.0,
        indicator_snapshot={
            "entry_execution": {
                "active_order_type": "limit_post_only",
                "coinbase_maker_only": True,
                "entry_edge_expected_net_pct": 5.0,
                "cost_gate_fee_bps": 120,
            }
        },
    )
    db.add(
        AutoTraderRun(
            user_id=u.id,
            ticker="ZZTEST",
            decision="placed",
            reason="submitted",
            trade_id=t.id,
            rule_snapshot={"entry_edge_expected_net_pct": 5.0, "cost_gate_fee_bps": 120},
            management_scope="auto_trader_v1",
        )
    )
    db.commit()

    cfg = SimpleNamespace(
        chili_stuck_order_watchdog_enabled=True,
        chili_stuck_order_market_timeout_seconds=300,
        chili_stuck_order_limit_timeout_seconds=1800,
        chili_coinbase_maker_first_fallback_enabled=True,
        chili_coinbase_maker_first_fallback_after_seconds=120,
        chili_coinbase_maker_first_min_net_after_cost_pct=0.0,
        chili_coinbase_maker_first_taker_price_buffer_bps=10.0,
        chili_coinbase_taker_fee_bps_round_trip=120,
        chili_min_edge_safety_buffer_bps=30,
    )
    monkeypatch.setattr(wd, "settings", cfg)

    fake_adapter = MagicMock()
    fake_adapter.get_order.return_value = (
        _fake_normalized("open", order_id="cb-maker-partial", filled_size=1.0),
        FreshnessMeta(retrieved_at_utc=datetime.utcnow(), max_age_seconds=15.0),
    )
    fake_adapter.get_best_bid_ask.return_value = (
        NormalizedTicker(product_id="ZZTEST", bid=100.0, ask=100.1, spread_bps=10.0),
        FreshnessMeta(retrieved_at_utc=datetime.utcnow(), max_age_seconds=15.0),
    )
    fake_adapter.cancel_order.return_value = {"ok": True}
    fake_adapter.place_limit_order_gtc.return_value = {
        "ok": True,
        "order_id": "cb-partial-fallback",
        "client_order_id": "fb-partial",
        "base_size": "1.0",
        "limit_price": "100.2001",
    }
    monkeypatch.setattr(wd, "_get_adapter", lambda _src: fake_adapter)

    out = wd.tick_stuck_order_watchdog(db)

    assert out["outcomes"].get("maker_first_fallback_submitted") == 1
    fake_adapter.cancel_order.assert_called_once_with("cb-maker-partial")
    assert float(fake_adapter.place_limit_order_gtc.call_args.kwargs["base_size"]) == 1.0

    db.refresh(t)
    assert t.status == "working"
    assert t.filled_quantity == 1.0
    assert t.remaining_quantity == 1.0
    assert t.broker_order_id == "cb-partial-fallback"
    entry = t.indicator_snapshot["entry_execution"]
    assert entry["maker_first_partial_fill_size"] == 1.0
    assert entry["maker_first_remaining_after_partial"] == 1.0
    assert entry["maker_first_fallback_submitted"] is True


def test_coinbase_maker_first_cancels_when_fallback_edge_is_too_thin(db, monkeypatch):
    _allow_replacement_buy_entry_gate(monkeypatch)
    u = models.User(name="stuck_wd_cb_thin")
    db.add(u)
    db.flush()
    db.execute(
        text("DELETE FROM venue_order_idempotency WHERE client_order_id = :cid"),
        {"cid": "atv1-thin-buy"},
    )

    t = _make_trade(
        db,
        user_id=u.id,
        broker_order_id="cb-maker-thin",
        broker_status="accepted",
        entry_date=datetime.utcnow() - timedelta(seconds=180),
        broker_source="coinbase",
        status="working",
        remaining_quantity=1.0,
        indicator_snapshot={
            "entry_execution": {
                "active_order_type": "limit_post_only",
                "coinbase_maker_only": True,
                "client_order_id": "atv1-thin-buy",
                "entry_edge_expected_net_pct": 1.0,
                "cost_gate_fee_bps": 120,
            }
        },
    )
    db.execute(
        text(
            "INSERT INTO venue_order_idempotency "
            "(client_order_id, venue, symbol, side, qty, broker_order_id, status, ttl_expires_at) "
            "VALUES (:cid, 'coinbase', 'ZZTEST', 'buy', 1.0, :oid, 'submitted', "
            "NOW() + INTERVAL '1 day')"
        ),
        {"cid": "atv1-thin-buy", "oid": "cb-maker-thin"},
    )
    db.commit()

    cfg = SimpleNamespace(
        chili_stuck_order_watchdog_enabled=True,
        chili_stuck_order_market_timeout_seconds=300,
        chili_stuck_order_limit_timeout_seconds=1800,
        chili_coinbase_maker_first_fallback_enabled=True,
        chili_coinbase_maker_first_fallback_after_seconds=120,
        chili_coinbase_maker_first_min_net_after_cost_pct=0.0,
        chili_coinbase_maker_first_taker_price_buffer_bps=10.0,
        chili_coinbase_maker_first_edge_thin_hold_enabled=False,
        chili_coinbase_taker_fee_bps_round_trip=120,
        chili_min_edge_safety_buffer_bps=30,
    )
    monkeypatch.setattr(wd, "settings", cfg)

    fake_adapter = MagicMock()
    fake_adapter.get_order.return_value = (
        _fake_normalized("open", order_id="cb-maker-thin"),
        FreshnessMeta(retrieved_at_utc=datetime.utcnow(), max_age_seconds=15.0),
    )
    fake_adapter.get_best_bid_ask.return_value = (
        NormalizedTicker(product_id="ZZTEST", bid=100.0, ask=100.1, spread_bps=10.0),
        FreshnessMeta(retrieved_at_utc=datetime.utcnow(), max_age_seconds=15.0),
    )
    fake_adapter.cancel_order.return_value = {"ok": True}
    monkeypatch.setattr(wd, "_get_adapter", lambda _src: fake_adapter)

    out = wd.tick_stuck_order_watchdog(db)

    assert out["outcomes"].get("maker_first_fallback_edge_too_thin_cancel_requested") == 1
    fake_adapter.cancel_order.assert_called_once_with("cb-maker-thin")
    fake_adapter.place_limit_order_gtc.assert_not_called()

    db.refresh(t)
    assert t.status == "working"
    assert t.broker_status == "cancel_requested"
    assert t.exit_reason is None
    entry = t.indicator_snapshot["entry_execution"]
    assert entry["maker_first_fallback_decision"] == "edge_too_thin"
    assert entry["cancel_requested"] is True
    assert entry["cancel_request_reason"] == "maker_first_edge_too_thin"
    assert entry["cancel_request_broker_truth"] == "unknown"
    assert entry["cancel_request_client_order_id"] == "atv1-thin-buy"
    event = (
        db.query(TradingExecutionEvent)
        .filter(TradingExecutionEvent.trade_id == t.id)
        .one()
    )
    assert event.event_type == "cancel_request"
    assert event.status == "cancel_requested"
    assert event.client_order_id == "atv1-thin-buy"
    idem_status = db.execute(
        text(
            "SELECT status FROM venue_order_idempotency "
            "WHERE client_order_id = :cid"
        ),
        {"cid": "atv1-thin-buy"},
    ).scalar_one()
    assert idem_status == "cancel_requested"
    db.execute(
        text("DELETE FROM venue_order_idempotency WHERE client_order_id = :cid"),
        {"cid": "atv1-thin-buy"},
    )
    db.commit()


def test_coinbase_maker_first_holds_thin_edge_maker_until_hold_timeout(db, monkeypatch):
    _allow_replacement_buy_entry_gate(monkeypatch)
    u = models.User(name="stuck_wd_cb_thin_hold")
    db.add(u)
    db.flush()

    t = _make_trade(
        db,
        user_id=u.id,
        broker_order_id="cb-maker-thin-hold",
        broker_status="accepted",
        entry_date=datetime.utcnow() - timedelta(seconds=180),
        broker_source="coinbase",
        status="working",
        remaining_quantity=1.0,
        indicator_snapshot={
            "entry_execution": {
                "active_order_type": "limit_post_only",
                "coinbase_maker_only": True,
                "entry_edge_expected_net_pct": 1.0,
                "cost_gate_fee_bps": 120,
            }
        },
    )

    cfg = SimpleNamespace(
        chili_stuck_order_watchdog_enabled=True,
        chili_stuck_order_market_timeout_seconds=300,
        chili_stuck_order_limit_timeout_seconds=1800,
        chili_coinbase_maker_first_fallback_enabled=True,
        chili_coinbase_maker_first_fallback_after_seconds=120,
        chili_coinbase_maker_first_min_net_after_cost_pct=0.0,
        chili_coinbase_maker_first_taker_price_buffer_bps=10.0,
        chili_coinbase_maker_first_edge_thin_hold_enabled=True,
        chili_coinbase_maker_first_edge_thin_hold_seconds=600,
        chili_coinbase_taker_fee_bps_round_trip=120,
        chili_min_edge_safety_buffer_bps=30,
    )
    monkeypatch.setattr(wd, "settings", cfg)

    fake_adapter = MagicMock()
    fake_adapter.get_order.return_value = (
        _fake_normalized("open", order_id="cb-maker-thin-hold"),
        FreshnessMeta(retrieved_at_utc=datetime.utcnow(), max_age_seconds=15.0),
    )
    fake_adapter.get_best_bid_ask.return_value = (
        NormalizedTicker(product_id="ZZTEST", bid=100.0, ask=100.1, spread_bps=10.0),
        FreshnessMeta(retrieved_at_utc=datetime.utcnow(), max_age_seconds=15.0),
    )
    monkeypatch.setattr(wd, "_get_adapter", lambda _src: fake_adapter)

    out = wd.tick_stuck_order_watchdog(db)

    assert out["outcomes"].get("maker_first_edge_too_thin_holding_maker") == 1
    fake_adapter.cancel_order.assert_not_called()
    fake_adapter.place_limit_order_gtc.assert_not_called()

    db.refresh(t)
    assert t.status == "working"
    assert t.broker_status == "open"
    assert t.broker_order_id == "cb-maker-thin-hold"
    entry = t.indicator_snapshot["entry_execution"]
    assert entry["maker_first_fallback_decision"] == "edge_too_thin_holding_maker"
    assert entry["maker_first_edge_thin_hold_seconds"] == 600
    assert "maker_first_fallback_attempted" not in entry
