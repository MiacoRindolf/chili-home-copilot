"""Tests for AutoTrader v1 monitor (stop/target, daily loss trip)."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from app import models
from app.models.trading import ScanPattern, Trade


@patch("app.services.trading.governance.activate_kill_switch")
@patch(
    "app.services.trading.auto_trader_rules.autotrader_realized_pnl_today_et",
    return_value=-200.0,
)
def test_maybe_trip_daily_loss_kill_switch(_mock_pnl, mock_ks):
    from app.services.trading.auto_trader_monitor import _maybe_trip_daily_loss_kill_switch

    with patch("app.services.trading.auto_trader_monitor.settings") as s:
        s.chili_autotrader_daily_loss_cap_usd = 150.0
        _maybe_trip_daily_loss_kill_switch(MagicMock(), 1)
    mock_ks.assert_called_once_with("autotrader_daily_loss_cap")


def test_tick_closes_on_stop(db):
    from app.services.trading.auto_trader_monitor import tick_auto_trader_monitor

    u = models.User(name="at_mon_u")
    db.add(u)
    db.flush()
    t = Trade(
        user_id=u.id,
        ticker="ZZZ",
        direction="long",
        entry_price=10.0,
        quantity=10.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=9.0,
        take_profit=15.0,
        auto_trader_version="v1",
    )
    db.add(t)
    db.commit()
    db.refresh(t)

    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_quote_price.return_value = 8.0  # price flows from Robinhood adapter
    ad.place_market_order.return_value = {
        "ok": True,
        "order_id": "oid1",
        "raw": {"average_price": 8.0, "cumulative_quantity": 10},
    }

    with patch("app.services.trading.auto_trader_monitor.settings") as s:
        s.chili_autotrader_enabled = True
        s.chili_autotrader_rth_only = False
        s.chili_autotrader_live_enabled = True
        s.chili_autotrader_daily_loss_cap_usd = 500.0
        with patch(
            "app.services.trading.venue.robinhood_spot.RobinhoodSpotAdapter",
            return_value=ad,
        ):
            with patch(
                "app.services.trading.auto_trader_monitor._maybe_trip_daily_loss_kill_switch",
            ):
                out = tick_auto_trader_monitor(db)

    assert out.get("closed") == 1
    db.refresh(t)
    assert t.status == "closed"
    assert abs(float(t.exit_price or 0) - 8.0) < 1e-6
    ad.place_market_order.assert_called_once()
    # Confirm the venue-correct quote source was used, not market_data fallback.
    assert out.get("quote_sources", {}).get("ZZZ") == "robinhood"


def _patch_monitor_settings():
    """Settings patcher shared by D1 widened-scope tests."""
    s = patch("app.services.trading.auto_trader_monitor.settings").start()
    s.chili_autotrader_enabled = True
    s.chili_autotrader_rth_only = False
    s.chili_autotrader_live_enabled = True
    s.chili_autotrader_daily_loss_cap_usd = 500.0
    return s


def _run_tick_with_adapter(db, adapter):
    from app.services.trading.auto_trader_monitor import tick_auto_trader_monitor

    with patch(
        "app.services.trading.venue.robinhood_spot.RobinhoodSpotAdapter",
        return_value=adapter,
    ):
        with patch(
            "app.services.trading.auto_trader_monitor._maybe_trip_daily_loss_kill_switch",
        ):
            return tick_auto_trader_monitor(db)


def test_monitor_manages_non_v1_pattern_linked_trade(db):
    """D1: a plain Trade with scan_pattern_id but no auto_trader_version gets managed."""
    u = models.User(name="d1_linked_u")
    db.add(u)
    db.flush()
    pat = ScanPattern(name="d1 pat", rules_json={}, origin="user", asset_class="stock")
    db.add(pat)
    db.flush()
    t = Trade(
        user_id=u.id,
        ticker="LINK1",
        direction="long",
        entry_price=10.0,
        quantity=5.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=9.0,
        take_profit=15.0,
        scan_pattern_id=pat.id,
        # NOTE: no auto_trader_version="v1" — widened scope must still manage it.
    )
    db.add(t)
    db.commit()
    db.refresh(t)

    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_quote_price.return_value = 8.0
    ad.place_market_order.return_value = {
        "ok": True,
        "order_id": "oid-linked",
        "raw": {"average_price": 8.0, "cumulative_quantity": 5},
    }

    _patch_monitor_settings()
    try:
        out = _run_tick_with_adapter(db, ad)
    finally:
        patch.stopall()

    assert out.get("closed") == 1
    db.refresh(t)
    assert t.status == "closed"
    ad.place_market_order.assert_called_once()


def test_monitor_seeds_missing_levels_from_pattern(db):
    """D1: a linked trade with NO stop/target gets seeded from rules_json.exits."""
    u = models.User(name="d1_seed_u")
    db.add(u)
    db.flush()
    pat = ScanPattern(
        name="d1 seed pat",
        rules_json={"exits": {"stop_pct": 5.0, "target_pct": 10.0}},
        origin="user",
        asset_class="stock",
    )
    db.add(pat)
    db.flush()
    t = Trade(
        user_id=u.id,
        ticker="SEED1",
        direction="long",
        entry_price=100.0,
        quantity=3.0,
        entry_date=datetime.utcnow(),
        status="open",
        scan_pattern_id=pat.id,
        # no stop_loss / take_profit
    )
    db.add(t)
    db.commit()
    db.refresh(t)

    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_quote_price.return_value = 102.0  # between seeded stop (95) and target (110)
    ad.place_market_order.return_value = {"ok": True}

    _patch_monitor_settings()
    try:
        _run_tick_with_adapter(db, ad)
    finally:
        patch.stopall()

    db.refresh(t)
    # Seeded from 5% / 10% on $100 long entry.
    assert t.stop_loss is not None and abs(float(t.stop_loss) - 95.0) < 1e-6
    assert t.take_profit is not None and abs(float(t.take_profit) - 110.0) < 1e-6
    # Price inside the band — no order placed.
    ad.place_market_order.assert_not_called()
    # Still open.
    assert t.status == "open"


def test_monitor_skips_linked_trade_without_levels_or_pattern_hints(db):
    """D1: if seeding can't populate levels, monitor records skip and does NOT sell."""
    u = models.User(name="d1_skip_u")
    db.add(u)
    db.flush()
    pat = ScanPattern(
        name="d1 empty pat",
        rules_json={},  # no exits
        origin="user",
        asset_class="stock",
    )
    db.add(pat)
    db.flush()
    t = Trade(
        user_id=u.id,
        ticker="SKIP1",
        direction="long",
        entry_price=50.0,
        quantity=2.0,
        entry_date=datetime.utcnow(),
        status="open",
        scan_pattern_id=pat.id,
    )
    db.add(t)
    db.commit()
    db.refresh(t)

    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_quote_price.return_value = 40.0
    ad.place_market_order.return_value = {"ok": True}

    _patch_monitor_settings()
    try:
        out = _run_tick_with_adapter(db, ad)
    finally:
        patch.stopall()

    assert int(t.id) in (out.get("skipped_no_levels") or [])
    ad.place_market_order.assert_not_called()
    db.refresh(t)
    assert t.status == "open"
