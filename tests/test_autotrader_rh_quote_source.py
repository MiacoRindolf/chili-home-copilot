"""AutoTrader v1 uses Robinhood's own feed for live stock exits + close-now.

Research confirmation: ``robin_stocks.robinhood.stocks.get_quotes`` is the
canonical real-time quote endpoint (bid_price / ask_price / last_trade_price).
The existing ``RobinhoodSpotAdapter.get_best_bid_ask`` already calls it, so the
monitor + close-now now route through that same venue to eliminate
Massive/Polygon/RH drift.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import models
from app.models.trading import Trade


# ── Adapter unit ──────────────────────────────────────────────────────────


def test_get_quote_price_uses_mid_when_bid_ask_present() -> None:
    from app.services.trading.venue.robinhood_spot import RobinhoodSpotAdapter

    fake_ticker = MagicMock()
    fake_ticker.mid = 101.5
    fake_ticker.last_price = 101.0
    fake_ticker.freshness = None
    adapter = RobinhoodSpotAdapter()
    with patch.object(adapter, "get_best_bid_ask", return_value=(fake_ticker, MagicMock())):
        assert adapter.get_quote_price("AAPL") == 101.5


def test_get_quote_price_falls_back_to_last() -> None:
    from app.services.trading.venue.robinhood_spot import RobinhoodSpotAdapter

    fake_ticker = MagicMock()
    fake_ticker.mid = 0
    fake_ticker.last_price = 99.4
    fake_ticker.freshness = None
    adapter = RobinhoodSpotAdapter()
    with patch.object(adapter, "get_best_bid_ask", return_value=(fake_ticker, MagicMock())):
        assert adapter.get_quote_price("AAPL") == 99.4


def test_get_quote_price_falls_back_to_extended_hours_last() -> None:
    import robin_stocks.robinhood as _rh

    from app.services.trading.venue.robinhood_spot import RobinhoodSpotAdapter

    adapter = RobinhoodSpotAdapter()
    fake_stocks = MagicMock()
    fake_stocks.get_quotes.return_value = [
        {
            "bid_price": None,
            "ask_price": None,
            "last_trade_price": None,
            "last_extended_hours_trade_price": "82.10",
        }
    ]
    with patch.object(_rh, "stocks", fake_stocks):
        assert adapter.get_quote_price("ACMR") == 82.10


def test_get_quote_price_rejects_stale_provider_timestamp() -> None:
    import robin_stocks.robinhood as _rh

    from app.services.trading.venue.robinhood_spot import RobinhoodSpotAdapter

    adapter = RobinhoodSpotAdapter()
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    fake_stocks = MagicMock()
    fake_stocks.get_quotes.return_value = [
        {
            "bid_price": "83.00",
            "ask_price": "84.00",
            "last_trade_price": "83.50",
            "last_extended_hours_trade_price": "84.10",
            "venue_bid_time": old,
            "venue_ask_time": old,
            "updated_at": old,
        }
    ]
    with patch(
        "app.services.trading.tradingview_blue_ocean.fetch_boats_quote",
        return_value=None,
    ), patch.object(_rh, "stocks", fake_stocks):
        assert adapter.get_quote_price("ACMR") is None

    fake_stocks.get_quotes.reset_mock()
    with patch(
        "app.services.trading.tradingview_blue_ocean.fetch_boats_quote",
        return_value=None,
    ), patch.object(_rh, "stocks", fake_stocks):
        assert adapter.get_quote_prices_batch(["ACMR"]) == {}


def test_get_quote_price_uses_blue_ocean_when_robinhood_quote_is_stale() -> None:
    import robin_stocks.robinhood as _rh

    from app.services.trading.venue.robinhood_spot import RobinhoodSpotAdapter

    adapter = RobinhoodSpotAdapter()
    old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    now = datetime.now(timezone.utc)
    fake_stocks = MagicMock()
    fake_stocks.get_quotes.return_value = [
        {
            "bid_price": "73.00",
            "ask_price": "76.00",
            "last_trade_price": "73.50",
            "last_extended_hours_trade_price": "73.98",
            "venue_bid_time": old,
            "venue_ask_time": old,
            "updated_at": old,
        }
    ]
    boats = {
        "price": 80.0,
        "last_price": 80.0,
        "provider_time_utc": now,
        "quote_ts": now.isoformat(),
        "volume": 100.0,
    }

    with patch(
        "app.services.trading.tradingview_blue_ocean.fetch_boats_quote",
        return_value=boats,
    ), patch.object(_rh, "stocks", fake_stocks):
        assert adapter.get_quote_price("ACMR") == 80.0


def test_autotrader_desk_hides_stale_broker_quote() -> None:
    from app.services.trading.autotrader_desk import _broker_quote_price_for_trade
    from app.services.trading.venue.protocol import FreshnessMeta, NormalizedTicker

    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    fresh = FreshnessMeta(
        retrieved_at_utc=datetime.now(timezone.utc),
        provider_time_utc=old,
        max_age_seconds=15.0,
    )
    tick = NormalizedTicker(
        product_id="ACMR",
        bid=90.0,
        ask=91.0,
        mid=90.5,
        last_price=91.0,
        freshness=fresh,
    )
    adapter = MagicMock()
    adapter.is_enabled.return_value = True
    adapter.get_ticker.return_value = (tick, fresh)
    trade = SimpleNamespace(broker_source="robinhood", ticker="ACMR", direction="long")

    with patch("app.services.trading.venue.factory.get_adapter", return_value=adapter):
        assert _broker_quote_price_for_trade(trade) == (None, "robinhood_stale")


def test_get_quote_price_returns_none_when_feed_empty() -> None:
    from app.services.trading.venue.robinhood_spot import RobinhoodSpotAdapter

    adapter = RobinhoodSpotAdapter()
    with patch.object(adapter, "get_best_bid_ask", return_value=(None, MagicMock())):
        assert adapter.get_quote_price("AAPL") is None


def test_get_quote_prices_batch_uses_rh_stocks_get_quotes() -> None:
    """One round-trip to ``rh.stocks.get_quotes`` for the whole desk."""
    import robin_stocks.robinhood as _rh

    from app.services.trading.venue.robinhood_spot import RobinhoodSpotAdapter

    adapter = RobinhoodSpotAdapter()
    fake_stocks = MagicMock()
    fake_stocks.get_quotes.return_value = [
        {"bid_price": "9.98", "ask_price": "10.02", "last_trade_price": "10.00"},
        {"bid_price": None, "ask_price": None, "last_trade_price": "55.30"},
    ]
    with patch.object(_rh, "stocks", fake_stocks):
        out = adapter.get_quote_prices_batch(["AAA", "BBB"])
    fake_stocks.get_quotes.assert_called_once()
    assert out["AAA"] == 10.0
    assert out["BBB"] == 55.3


# ── Monitor integration ───────────────────────────────────────────────────


def test_monitor_prefers_rh_feed_over_market_data(db) -> None:
    """Monitor asks the RH adapter first, never falls back when RH returns."""
    from app.services.trading.auto_trader_monitor import tick_auto_trader_monitor

    u = models.User(name="rh_src_u")
    db.add(u)
    db.flush()
    t = Trade(
        user_id=u.id,
        ticker="RHQ",
        direction="long",
        entry_price=20.0,
        quantity=5.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=15.0,
        take_profit=30.0,
        auto_trader_version="v1",
        broker_source="robinhood",
    )
    db.add(t)
    db.commit()

    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_quote_price.return_value = 22.5  # not a stop/target hit
    ad.place_market_order.return_value = {"ok": True, "order_id": "x", "raw": {}}

    fallback_called = []

    def _fail_fetch_quote(_ticker):
        fallback_called.append(_ticker)
        return {"price": 50.0}  # would wrongly hit target if used

    with patch("app.services.trading.auto_trader_monitor.settings") as s:
        s.chili_autotrader_enabled = True
        s.chili_autotrader_rth_only = False
        s.chili_autotrader_live_enabled = True
        s.chili_autotrader_daily_loss_cap_usd = 500.0
        with patch(
            "app.services.trading.venue.robinhood_spot.RobinhoodSpotAdapter",
            return_value=ad,
        ), patch(
            "app.services.trading.market_data.fetch_quote", side_effect=_fail_fetch_quote
        ), patch(
            "app.services.trading.auto_trader_monitor._maybe_trip_daily_loss_kill_switch"
        ):
            out = tick_auto_trader_monitor(db)

    assert fallback_called == [], "Fallback was hit even though RH returned a price"
    assert out.get("checked") == 1
    assert out.get("closed") == 0  # 22.5 is between stop 15 and target 30
    assert out.get("quote_sources", {}).get("RHQ") == "robinhood"
    ad.get_quote_price.assert_called_with("RHQ")


def test_monitor_does_not_cross_feed_when_broker_quote_empty(db) -> None:
    """If broker data is empty/stale, do not substitute a generic feed."""
    from app.services.trading.auto_trader_monitor import tick_auto_trader_monitor

    u = models.User(name="rh_src_fb")
    db.add(u)
    db.flush()
    t = Trade(
        user_id=u.id,
        ticker="HLT",
        direction="long",
        entry_price=10.0,
        quantity=2.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=9.0,
        take_profit=15.0,
        auto_trader_version="v1",
        broker_source="robinhood",
    )
    db.add(t)
    db.commit()

    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_quote_price.return_value = None  # RH halted / no data
    ad.place_market_order.return_value = {
        "ok": True,
        "state": "filled",
        "order_id": "y",
        "raw": {"average_price": 9.0, "state": "filled"},
    }

    fallback_called = []

    def _fail_fetch_quote(_ticker):
        fallback_called.append(_ticker)
        return {"price": 8.5}

    with patch("app.services.trading.auto_trader_monitor.settings") as s:
        s.chili_autotrader_enabled = True
        s.chili_autotrader_rth_only = False
        s.chili_autotrader_live_enabled = True
        s.chili_autotrader_daily_loss_cap_usd = 500.0
        with patch(
            "app.services.trading.venue.robinhood_spot.RobinhoodSpotAdapter",
            return_value=ad,
        ), patch(
            "app.services.trading.market_data.fetch_quote", side_effect=_fail_fetch_quote
        ), patch(
            "app.services.trading.auto_trader_monitor._maybe_trip_daily_loss_kill_switch"
        ), patch(
            "app.services.trading.robinhood_exit_execution."
            "describe_robinhood_equity_execution_window",
            return_value={
                "ticker": "HLT",
                "session": "regular_hours",
                "session_label": "Regular session",
                "market_hours": "regular_hours",
                "next_eligible_session_at": None,
                "overnight_eligible": False,
                "can_submit_now": True,
                "execution_reason": "Regular session",
            },
        ), patch(
            "app.services.broker_service.is_connected",
            return_value=True,
        ), patch(
            "app.services.broker_service.get_positions",
            return_value=[{"ticker": "HLT", "quantity": "2"}],
        ):
            out = tick_auto_trader_monitor(db)

    assert fallback_called == []
    assert out.get("closed") == 0
    assert "no_quote:HLT" in (out.get("errors") or [])
    assert out.get("quote_sources", {}).get("HLT") is None


# ── Close-now path ────────────────────────────────────────────────────────


def test_current_quote_price_prefers_rh_when_requested() -> None:
    """``_current_quote_price(prefer_rh=True)`` routes through the RH adapter."""
    from app.services.trading.auto_trader_position_overrides import _current_quote_price

    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_quote_price.return_value = 42.0

    with patch(
        "app.services.trading.venue.robinhood_spot.RobinhoodSpotAdapter",
        return_value=ad,
    ), patch(
        "app.services.trading.market_data.fetch_quote"
    ) as mock_mkt:
        px = _current_quote_price("ABC", prefer_rh=True)

    assert px == 42.0
    mock_mkt.assert_not_called()


def test_current_quote_price_without_flag_uses_market_data() -> None:
    """Paper close-now keeps generic fetch_quote (simulation, not venue-bound)."""
    from app.services.trading.auto_trader_position_overrides import _current_quote_price

    with patch(
        "app.services.trading.market_data.fetch_quote",
        return_value={"price": 7.25},
    ) as mock_mkt:
        px = _current_quote_price("ABC")  # default prefer_rh=False

    assert px == 7.25
    mock_mkt.assert_called_once_with("ABC")
