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

from app import models
from app.models.trading import Trade
from app.services.trading import stuck_order_watchdog as wd
from app.services.trading.venue.protocol import FreshnessMeta, NormalizedOrder


def _make_trade(db, *, user_id, broker_order_id, broker_status, entry_date, scope="auto_trader_v1"):
    t = Trade(
        user_id=user_id,
        ticker="ZZTEST",
        direction="long",
        entry_price=10.0,
        quantity=1.0,
        entry_date=entry_date,
        status="open",
        broker_source="robinhood",
        broker_order_id=broker_order_id,
        broker_status=broker_status,
        management_scope=scope,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _fake_normalized(status: str, order_id: str = "abc") -> NormalizedOrder:
    return NormalizedOrder(
        order_id=order_id,
        client_order_id=None,
        product_id="ZZTEST",
        side="buy",
        status=status,
        order_type="market",
        filled_size=0.0,
        average_filled_price=None,
        created_time=None,
        raw={"state": status},
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

    assert out["outcomes"].get("cancelled") == 1
    fake_adapter.cancel_order.assert_called_once_with("rh-stuck")

    db.refresh(t)
    assert t.status == "cancelled"
    assert t.broker_status == "cancelled"
    assert t.last_broker_sync is not None


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


def test_broker_unknown_order_marks_rejected(db, monkeypatch):
    u = models.User(name="stuck_wd_u4")
    db.add(u)
    db.flush()

    t = _make_trade(
        db,
        user_id=u.id,
        broker_order_id="rh-ghost",
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
