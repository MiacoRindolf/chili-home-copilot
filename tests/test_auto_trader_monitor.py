"""Tests for AutoTrader v1 monitor (stop/target, daily loss trip)."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from app import models
from app.models.trading import BreakoutAlert, PatternMonitorDecision, ScanPattern, Trade


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
        s.chili_autotrader_user_id = u.id
        s.brain_default_user_id = u.id
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


def _patch_monitor_settings(**overrides):
    """Settings patcher shared by D1 widened-scope tests.

    Accepts keyword overrides so tests can exercise the user-scope guard
    (``chili_autotrader_user_id`` / ``brain_default_user_id``) without
    repeating the full patcher each time.
    """
    s = patch("app.services.trading.auto_trader_monitor.settings").start()
    s.chili_autotrader_enabled = True
    s.chili_autotrader_rth_only = False
    s.chili_autotrader_live_enabled = True
    s.chili_autotrader_daily_loss_cap_usd = 500.0
    # Default: resolve user from the first User created in each test fixture
    # (tests that care about scope explicitly override these).
    s.chili_autotrader_user_id = overrides.get("chili_autotrader_user_id", 1)
    s.brain_default_user_id = overrides.get("brain_default_user_id", 1)
    for k, v in overrides.items():
        setattr(s, k, v)
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

    _patch_monitor_settings(chili_autotrader_user_id=u.id, brain_default_user_id=u.id)
    try:
        out = _run_tick_with_adapter(db, ad)
    finally:
        patch.stopall()

    assert out.get("closed") == 1
    db.refresh(t)
    assert t.status == "closed"
    ad.place_market_order.assert_called_once()


def test_monitor_manages_plan_level_trade_without_pattern_link(db):
    """Plan-level live trades should execute exits on stop/target just like the
    advisory pattern-position monitor already evaluates them."""
    u = models.User(name="d1_plan_levels_u")
    db.add(u)
    db.flush()
    t = Trade(
        user_id=u.id,
        ticker="PLANX",
        direction="long",
        entry_price=10.0,
        quantity=5.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=9.0,
        take_profit=12.0,
        broker_source="robinhood",
        tags="robinhood-sync",
    )
    db.add(t)
    db.commit()
    db.refresh(t)

    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_quote_price.return_value = 8.0
    ad.place_market_order.return_value = {
        "ok": True,
        "order_id": "oid-plan",
        "raw": {"average_price": 8.0, "cumulative_quantity": 5},
    }

    _patch_monitor_settings(chili_autotrader_user_id=u.id, brain_default_user_id=u.id)
    try:
        out = _run_tick_with_adapter(db, ad)
    finally:
        patch.stopall()

    assert out.get("closed") == 1
    db.refresh(t)
    assert t.status == "closed"
    ad.place_market_order.assert_called_once()


def test_monitor_closes_on_latest_pattern_exit_now_decision(db):
    """A fresh advisory EXIT_NOW should now bridge into the live sell path even
    when the hard stop/target has not been crossed yet."""
    u = models.User(name="d1_monitor_exit_u")
    db.add(u)
    db.flush()
    t = Trade(
        user_id=u.id,
        ticker="EXIT1",
        direction="long",
        entry_price=10.0,
        quantity=5.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=9.0,
        take_profit=14.0,
        broker_source="robinhood",
    )
    db.add(t)
    db.flush()
    db.add(
        PatternMonitorDecision(
            trade_id=t.id,
            health_score=0.15,
            action="exit_now",
            decision_source="plan_levels",
            price_at_decision=10.35,
            created_at=datetime.utcnow(),
        )
    )
    db.commit()
    db.refresh(t)

    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_quote_price.return_value = 10.4  # above stop and below target
    ad.place_market_order.return_value = {
        "ok": True,
        "order_id": "oid-exit-now",
        "raw": {"average_price": 10.4, "cumulative_quantity": 5},
    }

    _patch_monitor_settings(chili_autotrader_user_id=u.id, brain_default_user_id=u.id)
    try:
        out = _run_tick_with_adapter(db, ad)
    finally:
        patch.stopall()

    assert out.get("closed") == 1
    db.refresh(t)
    assert t.status == "closed"
    assert t.exit_reason == "pattern_exit_now"
    ad.place_market_order.assert_called_once()


def test_monitor_uses_latest_pattern_decision_not_stale_exit_now(db):
    """A newer HOLD decision must suppress an older EXIT_NOW recommendation."""
    u = models.User(name="d1_monitor_hold_u")
    db.add(u)
    db.flush()
    t = Trade(
        user_id=u.id,
        ticker="EXIT2",
        direction="long",
        entry_price=10.0,
        quantity=5.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=9.0,
        take_profit=14.0,
        broker_source="robinhood",
    )
    db.add(t)
    db.flush()
    db.add_all(
        [
            PatternMonitorDecision(
                trade_id=t.id,
                health_score=0.12,
                action="exit_now",
                decision_source="plan_levels",
                price_at_decision=10.2,
                created_at=datetime.utcnow() - timedelta(hours=2),
            ),
            PatternMonitorDecision(
                trade_id=t.id,
                health_score=0.68,
                action="hold",
                decision_source="plan_levels",
                price_at_decision=10.45,
                created_at=datetime.utcnow() - timedelta(minutes=5),
            ),
        ]
    )
    db.commit()
    db.refresh(t)

    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_quote_price.return_value = 10.5  # above stop and below target
    ad.place_market_order.return_value = {
        "ok": True,
        "order_id": "oid-hold-wins",
        "raw": {"average_price": 10.5, "cumulative_quantity": 5},
    }

    _patch_monitor_settings(chili_autotrader_user_id=u.id, brain_default_user_id=u.id)
    try:
        out = _run_tick_with_adapter(db, ad)
    finally:
        patch.stopall()

    assert out.get("closed") == 0
    db.refresh(t)
    assert t.status == "open"
    ad.place_market_order.assert_not_called()


def test_monitor_skips_non_robinhood_trade_even_if_exit_would_fire(db):
    """Safety: this monitor places orders through the Robinhood adapter, so
    explicit non-Robinhood rows must be skipped."""
    u = models.User(name="d1_monitor_coinbase_skip_u")
    db.add(u)
    db.flush()
    t = Trade(
        user_id=u.id,
        ticker="BTC-USD",
        direction="long",
        entry_price=50000.0,
        quantity=0.1,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=49000.0,
        take_profit=52000.0,
        broker_source="coinbase",
    )
    db.add(t)
    db.commit()
    db.refresh(t)

    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_quote_price.return_value = None
    ad.place_market_order.return_value = {
        "ok": True,
        "order_id": "oid-should-not-fire",
        "raw": {"average_price": 48000.0},
    }

    _patch_monitor_settings(chili_autotrader_user_id=u.id, brain_default_user_id=u.id)
    try:
        out = _run_tick_with_adapter(db, ad)
    finally:
        patch.stopall()

    assert out.get("closed") == 0
    assert any(
        x.get("trade_id") == int(t.id) and x.get("broker_source") == "coinbase"
        for x in (out.get("skipped_broker_source") or [])
    )
    db.refresh(t)
    assert t.status == "open"
    ad.place_market_order.assert_not_called()


def test_monitor_skips_robinhood_crypto_ticker_on_equity_adapter(db):
    """Robinhood crypto rows are synced into Trade, but this monitor's adapter is
    still the equities path and must not try to liquidate ``-USD`` tickers."""
    u = models.User(name="d1_monitor_rh_crypto_skip_u")
    db.add(u)
    db.flush()
    t = Trade(
        user_id=u.id,
        ticker="ETH-USD",
        direction="long",
        entry_price=3000.0,
        quantity=0.5,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=2900.0,
        take_profit=3300.0,
        broker_source="robinhood",
    )
    db.add(t)
    db.commit()
    db.refresh(t)

    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_quote_price.return_value = 2800.0
    ad.place_market_order.return_value = {
        "ok": True,
        "order_id": "oid-rh-crypto-should-not-fire",
        "raw": {"average_price": 2800.0},
    }

    _patch_monitor_settings(chili_autotrader_user_id=u.id, brain_default_user_id=u.id)
    try:
        out = _run_tick_with_adapter(db, ad)
    finally:
        patch.stopall()

    assert out.get("closed") == 0
    assert any(
        x.get("trade_id") == int(t.id) and x.get("ticker") == "ETH-USD"
        for x in (out.get("skipped_unsupported_ticker") or [])
    )
    db.refresh(t)
    assert t.status == "open"
    ad.place_market_order.assert_not_called()


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

    _patch_monitor_settings(chili_autotrader_user_id=u.id, brain_default_user_id=u.id)
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

    _patch_monitor_settings(chili_autotrader_user_id=u.id, brain_default_user_id=u.id)
    try:
        out = _run_tick_with_adapter(db, ad)
    finally:
        patch.stopall()

    assert int(t.id) in (out.get("skipped_no_levels") or [])
    ad.place_market_order.assert_not_called()
    db.refresh(t)
    assert t.status == "open"


def test_monitor_seeds_missing_levels_from_breakout_alert(db):
    """Regression: the 20 live Trades in production have alert-stamped stop/target
    but no pattern.rules_json.exits. Seeder must prefer BreakoutAlert as the
    authoritative source and heal them without needing pattern exits."""
    u = models.User(name="d1_alert_seed_u")
    db.add(u)
    db.flush()
    pat = ScanPattern(
        name="no-exits pattern",
        rules_json={},  # deliberately empty — mirrors prod state
        origin="user",
        asset_class="stock",
    )
    db.add(pat)
    db.flush()
    alert = BreakoutAlert(
        user_id=u.id,
        ticker="PFSI",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.55,
        price_at_alert=90.0,
        entry_price=90.38,
        stop_loss=82.37,
        target_price=99.99,
        scan_pattern_id=pat.id,
    )
    db.add(alert)
    db.flush()
    t = Trade(
        user_id=u.id,
        ticker="PFSI",
        direction="long",
        entry_price=89.64,
        quantity=4.0,
        entry_date=datetime.utcnow(),
        status="open",
        scan_pattern_id=pat.id,
        related_alert_id=alert.id,
        # Only stop populated — target missing (mirrors prod exact state).
        stop_loss=93.9,
    )
    db.add(t)
    db.commit()
    db.refresh(t)

    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_quote_price.return_value = 95.0  # between stop and seeded target
    ad.place_market_order.return_value = {"ok": True}

    _patch_monitor_settings(chili_autotrader_user_id=u.id, brain_default_user_id=u.id)
    try:
        _run_tick_with_adapter(db, ad)
    finally:
        patch.stopall()

    db.refresh(t)
    assert t.take_profit is not None and abs(float(t.take_profit) - 99.99) < 1e-6, \
        "take_profit should have been seeded from alert.target_price"
    # Pre-existing stop (93.9) should not be overwritten by alert.stop_loss (82.37).
    assert abs(float(t.stop_loss) - 93.9) < 1e-6, "existing stop must not be overwritten"
    ad.place_market_order.assert_not_called()
    assert t.status == "open"


def test_monitor_scopes_live_sweep_to_autotrader_user(db):
    """Safety: live monitor must filter open Trade rows by the configured
    autotrader user_id. Without this, trades owned by other users (linked RH
    accounts) would be eligible for market-sell on stop hits. Reproduces the
    cross-user sweep observed in production after D1."""
    from app.services.trading import auto_trader_monitor as mod

    owner = models.User(name="owner_autotrader")
    other = models.User(name="other_user_rh_linked")
    db.add_all([owner, other])
    db.flush()
    pat = ScanPattern(name="scope t", rules_json={}, origin="user", asset_class="stock")
    db.add(pat)
    db.flush()
    # Other user's pattern-linked open position.
    t_other = Trade(
        user_id=other.id,
        ticker="OTHR",
        direction="long",
        entry_price=10.0,
        quantity=10.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=9.0,
        take_profit=11.0,
        scan_pattern_id=pat.id,
    )
    # Owner's own position.
    t_own = Trade(
        user_id=owner.id,
        ticker="MINE",
        direction="long",
        entry_price=10.0,
        quantity=10.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=9.0,
        take_profit=11.0,
        scan_pattern_id=pat.id,
    )
    db.add_all([t_other, t_own])
    db.commit()

    ad = MagicMock()
    ad.is_enabled.return_value = True
    # Price far below stop — if the scope leaked, OTHR would be sold.
    ad.get_quote_price.return_value = 5.0
    ad.place_market_order.return_value = {"ok": True}

    _patch_monitor_settings(
        chili_autotrader_user_id=owner.id,
        brain_default_user_id=owner.id,
    )
    try:
        out = _run_tick_with_adapter(db, ad)
    finally:
        patch.stopall()

    # Exactly one position checked — the owner's. Other user's OTHR is invisible.
    assert out.get("checked") == 1
    sold_tickers = [c.kwargs.get("product_id") for c in ad.place_market_order.call_args_list]
    assert "OTHR" not in sold_tickers, "monitor must not market-sell other-user trades"
    db.refresh(t_other)
    assert t_other.status == "open", "other user's trade must stay open"


def test_monitor_aborts_when_no_autotrader_user_configured(db):
    """Defense-in-depth: if neither chili_autotrader_user_id nor
    brain_default_user_id is set, the live monitor must refuse to sweep."""
    from app.services.trading import auto_trader_monitor as mod

    u = models.User(name="orphan_monitor_u")
    db.add(u)
    db.flush()
    pat = ScanPattern(name="orphan", rules_json={}, origin="user", asset_class="stock")
    db.add(pat)
    db.flush()
    t = Trade(
        user_id=u.id,
        ticker="NOU",
        direction="long",
        entry_price=10.0,
        quantity=1.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=9.0,
        take_profit=11.0,
        scan_pattern_id=pat.id,
    )
    db.add(t)
    db.commit()

    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_quote_price.return_value = 5.0

    _patch_monitor_settings(
        chili_autotrader_user_id=None,
        brain_default_user_id=None,
    )
    try:
        out = _run_tick_with_adapter(db, ad)
    finally:
        patch.stopall()

    assert out.get("skipped") == "no_user_scope"
    ad.place_market_order.assert_not_called()


def test_monitor_extended_hours_gate_blocks_pure_overnight(db, monkeypatch):
    """chili_autotrader_allow_extended_hours uses 04:00-20:00 ET; outside that
    the monitor short-circuits with skipped=outside_extended_hours."""
    from app.services.trading import auto_trader_monitor as mod

    monkeypatch.setattr(mod.settings, "chili_autotrader_enabled", True, raising=False)
    monkeypatch.setattr(mod.settings, "chili_autotrader_rth_only", True, raising=False)
    monkeypatch.setattr(
        mod.settings, "chili_autotrader_allow_extended_hours", True, raising=False
    )
    with patch(
        "app.services.trading.pattern_imminent_alerts.us_stock_extended_session_open",
        return_value=False,
    ):
        out = mod.tick_auto_trader_monitor(db)
    assert out.get("skipped") == "outside_extended_hours"
