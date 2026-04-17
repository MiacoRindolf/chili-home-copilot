"""AutoTrader v1 uses Robinhood's own feed for live stock exits + close-now.

Research confirmation: ``robin_stocks.robinhood.stocks.get_quotes`` is the
canonical real-time quote endpoint (bid_price / ask_price / last_trade_price).
The existing ``RobinhoodSpotAdapter.get_best_bid_ask`` already calls it, so the
monitor + close-now now route through that same venue to eliminate
Massive/Polygon/RH drift.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from app import models
from app.models.trading import Trade


# ── Adapter unit ──────────────────────────────────────────────────────────


def test_get_quote_price_uses_mid_when_bid_ask_present() -> None:
    from app.services.trading.venue.robinhood_spot import RobinhoodSpotAdapter

    fake_ticker = MagicMock()
    fake_ticker.mid = 101.5
    fake_ticker.last_price = 101.0
    adapter = RobinhoodSpotAdapter()
    with patch.object(adapter, "get_best_bid_ask", return_value=(fake_ticker, MagicMock())):
        assert adapter.get_quote_price("AAPL") == 101.5


def test_get_quote_price_falls_back_to_last() -> None:
    from app.services.trading.venue.robinhood_spot import RobinhoodSpotAdapter

    fake_ticker = MagicMock()
    fake_ticker.mid = 0
    fake_ticker.last_price = 99.4
    adapter = RobinhoodSpotAdapter()
    with patch.object(adapter, "get_best_bid_ask", return_value=(fake_ticker, MagicMock())):
        assert adapter.get_quote_price("AAPL") == 99.4


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


def test_monitor_falls_back_to_market_data_when_rh_empty(db) -> None:
    """If RH returns None (halt, transient), fall back to fetch_quote."""
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
    )
    db.add(t)
    db.commit()

    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_quote_price.return_value = None  # RH halted / no data
    ad.place_market_order.return_value = {
        "ok": True,
        "order_id": "y",
        "raw": {"average_price": 9.0},
    }

    with patch("app.services.trading.auto_trader_monitor.settings") as s:
        s.chili_autotrader_enabled = True
        s.chili_autotrader_rth_only = False
        s.chili_autotrader_live_enabled = True
        s.chili_autotrader_daily_loss_cap_usd = 500.0
        with patch(
            "app.services.trading.venue.robinhood_spot.RobinhoodSpotAdapter",
            return_value=ad,
        ), patch(
            "app.services.trading.market_data.fetch_quote",
            return_value={"price": 8.5},
        ), patch(
            "app.services.trading.auto_trader_monitor._maybe_trip_daily_loss_kill_switch"
        ):
            out = tick_auto_trader_monitor(db)

    assert out.get("closed") == 1
    assert out.get("quote_sources", {}).get("HLT") == "market_data"


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
