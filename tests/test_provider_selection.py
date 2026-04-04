"""Tests for market data provider selection: Massive → Polygon → yfinance."""
from unittest.mock import patch, MagicMock, PropertyMock
import pytest


# ---------------------------------------------------------------------------
# Helper: build a standard "fast_info" / quote response dict
# ---------------------------------------------------------------------------

def _make_quote(price=150.0, prev_close=148.0, volume=1_000_000):
    return {
        "last_price": price,
        "previous_close": prev_close,
        "day_high": price + 2,
        "day_low": price - 2,
        "volume": volume,
        "market_cap": None,
        "year_high": None,
        "year_low": None,
        "avg_volume": None,
    }


def _make_bars():
    return [
        {"time": 1700000000, "open": 148.0, "high": 150.0,
         "low": 147.0, "close": 149.5, "volume": 500_000},
        {"time": 1700086400, "open": 149.5, "high": 152.0,
         "low": 149.0, "close": 151.0, "volume": 600_000},
    ]


# ---------------------------------------------------------------------------
# fetch_quote provider selection
# ---------------------------------------------------------------------------

class TestFetchQuoteProviderOrder:
    """Verify that fetch_quote tries Massive → Polygon → yfinance."""

    @patch("app.services.trading.market_data._use_polygon", return_value=False)
    @patch("app.services.trading.market_data._use_massive", return_value=True)
    @patch("app.services.trading.market_data._massive")
    def test_massive_success_skips_polygon_and_yfinance(
        self, mock_massive_mod, _use_m, _use_p,
    ):
        from app.services.trading.market_data import fetch_quote

        mock_massive_mod.get_ws_quote.return_value = None
        mock_massive_mod.get_last_quote.return_value = _make_quote(155.0)

        result = fetch_quote("AAPL")

        assert result is not None
        assert result["price"] == 155.0
        mock_massive_mod.get_last_quote.assert_called_once_with("AAPL")

    @patch("app.services.trading.market_data._yf_fast_info", return_value=None)
    @patch("app.services.trading.market_data._use_polygon", return_value=True)
    @patch("app.services.trading.market_data._use_massive", return_value=True)
    @patch("app.services.trading.market_data._poly")
    @patch("app.services.trading.market_data._massive")
    def test_massive_fails_falls_to_polygon(
        self, mock_massive_mod, mock_poly_mod, _use_m, _use_p, _yf,
    ):
        from app.services.trading.market_data import fetch_quote

        mock_massive_mod.get_ws_quote.return_value = None
        mock_massive_mod.get_last_quote.return_value = None  # Massive fails
        mock_massive_mod.massive_aggregate_variants_all_dead.return_value = False

        mock_poly_mod.get_last_quote.return_value = _make_quote(152.0)

        result = fetch_quote("AAPL")

        assert result is not None
        assert result["price"] == 152.0
        mock_poly_mod.get_last_quote.assert_called_once_with("AAPL")

    @patch("app.services.trading.market_data._yf_fast_info")
    @patch("app.services.trading.market_data._use_polygon", return_value=True)
    @patch("app.services.trading.market_data._use_massive", return_value=True)
    @patch("app.services.trading.market_data._poly")
    @patch("app.services.trading.market_data._massive")
    def test_massive_and_polygon_fail_falls_to_yfinance(
        self, mock_massive_mod, mock_poly_mod, _use_m, _use_p, mock_yf,
    ):
        from app.services.trading.market_data import fetch_quote

        mock_massive_mod.get_ws_quote.return_value = None
        mock_massive_mod.get_last_quote.return_value = None
        mock_massive_mod.massive_aggregate_variants_all_dead.return_value = False
        mock_poly_mod.get_last_quote.return_value = None

        mock_yf.return_value = _make_quote(149.0)

        result = fetch_quote("AAPL")

        assert result is not None
        assert result["price"] == 149.0

    @patch("app.services.trading.market_data._yf_fast_info")
    @patch("app.services.trading.market_data._use_polygon", return_value=False)
    @patch("app.services.trading.market_data._use_massive", return_value=False)
    def test_no_providers_configured_uses_yfinance(
        self, _use_m, _use_p, mock_yf,
    ):
        from app.services.trading.market_data import fetch_quote

        mock_yf.return_value = _make_quote(147.0)
        result = fetch_quote("AAPL")
        assert result is not None
        assert result["price"] == 147.0

    @patch("app.services.trading.market_data._yf_fast_info", return_value=None)
    @patch("app.services.trading.market_data._use_polygon", return_value=False)
    @patch("app.services.trading.market_data._use_massive", return_value=False)
    def test_all_providers_fail_returns_none(self, _use_m, _use_p, _yf):
        from app.services.trading.market_data import fetch_quote
        result = fetch_quote("FAKE_TICKER")
        assert result is None


# ---------------------------------------------------------------------------
# fetch_quote: WebSocket cache hit
# ---------------------------------------------------------------------------

class TestFetchQuoteWSCache:
    """Verify that a fresh WS cache hit is returned without REST calls."""

    @patch("app.services.trading.market_data._use_polygon", return_value=False)
    @patch("app.services.trading.market_data._use_massive", return_value=True)
    @patch("app.services.trading.market_data._massive")
    def test_ws_cache_hit_returns_immediately(self, mock_massive_mod, _use_m, _use_p):
        from app.services.trading.market_data import fetch_quote
        from app.services.massive_client import QuoteSnapshot

        mock_massive_mod.get_ws_quote.return_value = QuoteSnapshot(
            price=160.5, bid=160.4, ask=160.6, timestamp=9999999999.0,
        )

        result = fetch_quote("NVDA")

        assert result is not None
        assert result["price"] == 160.5
        mock_massive_mod.get_last_quote.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_ohlcv provider selection
# ---------------------------------------------------------------------------

class TestFetchOHLCVProviderOrder:
    """Verify that fetch_ohlcv tries Massive → Polygon → yfinance."""

    @patch("app.services.trading.market_data._use_polygon", return_value=False)
    @patch("app.services.trading.market_data._use_massive", return_value=True)
    @patch("app.services.trading.market_data._massive")
    def test_massive_ohlcv_success(self, mock_massive_mod, _use_m, _use_p):
        from app.services.trading.market_data import fetch_ohlcv

        mock_massive_mod.get_aggregates.return_value = _make_bars()
        result = fetch_ohlcv("AAPL", interval="1d", period="6mo")
        assert len(result) == 2
        mock_massive_mod.get_aggregates.assert_called_once()

    @patch("app.services.trading.market_data._yf_history")
    @patch("app.services.trading.market_data._use_polygon", return_value=True)
    @patch("app.services.trading.market_data._use_massive", return_value=True)
    @patch("app.services.trading.market_data._poly")
    @patch("app.services.trading.market_data._massive")
    def test_massive_ohlcv_fails_falls_to_polygon(
        self, mock_massive_mod, mock_poly_mod, _use_m, _use_p, _yf_hist,
    ):
        from app.services.trading.market_data import fetch_ohlcv

        mock_massive_mod._is_dead_ticker.return_value = False
        mock_massive_mod.massive_aggregate_variants_all_dead.return_value = False
        mock_massive_mod.get_aggregates.return_value = []  # empty
        mock_poly_mod.get_aggregates.return_value = _make_bars()

        result = fetch_ohlcv("AAPL")
        assert len(result) == 2
        mock_poly_mod.get_aggregates.assert_called_once()

    @patch("app.services.trading.market_data._yf_history")
    @patch("app.services.trading.market_data._use_polygon", return_value=True)
    @patch("app.services.trading.market_data._use_massive", return_value=True)
    @patch("app.services.trading.market_data._poly")
    @patch("app.services.trading.market_data._massive")
    def test_massive_and_polygon_ohlcv_fail_falls_to_yfinance(
        self, mock_massive_mod, mock_poly_mod, _use_m, _use_p, mock_yf_hist,
    ):
        import pandas as pd
        from app.services.trading.market_data import fetch_ohlcv

        mock_massive_mod._is_dead_ticker.return_value = False
        mock_massive_mod.massive_aggregate_variants_all_dead.return_value = False
        mock_massive_mod.get_aggregates.return_value = []
        mock_poly_mod.get_aggregates.return_value = []

        df = pd.DataFrame({
            "Open": [148.0], "High": [150.0], "Low": [147.0],
            "Close": [149.5], "Volume": [500000],
        }, index=pd.to_datetime(["2024-01-02"]))
        mock_yf_hist.return_value = df

        result = fetch_ohlcv("AAPL")
        assert len(result) == 1
        assert result[0]["close"] == 149.5

    @patch("app.services.trading.market_data._yf_history")
    @patch("app.services.trading.market_data._use_polygon", return_value=True)
    @patch("app.services.trading.market_data._use_massive", return_value=True)
    @patch("app.services.trading.market_data._poly")
    @patch("app.services.trading.market_data._massive")
    def test_crypto_massive_empty_uses_polygon(
        self, mock_massive_mod, mock_poly_mod, _use_m, _use_p, mock_yf_hist,
    ):
        from app.services.trading.market_data import fetch_ohlcv

        mock_massive_mod._is_dead_ticker.return_value = False
        mock_massive_mod.massive_aggregate_variants_all_dead.return_value = False
        mock_massive_mod.get_aggregates.return_value = []
        mock_poly_mod.get_aggregates.return_value = _make_bars()

        result = fetch_ohlcv("ZK-USD", interval="1d", period="6mo")
        assert len(result) == 2
        mock_poly_mod.get_aggregates.assert_called_once()
        mock_yf_hist.assert_not_called()

    @patch("app.services.trading.market_data._yf_history")
    @patch("app.services.trading.market_data._use_polygon", return_value=True)
    @patch("app.services.trading.market_data._use_massive", return_value=True)
    @patch("app.services.trading.market_data._poly")
    @patch("app.services.trading.market_data._massive")
    def test_crypto_skips_yfinance_when_massive_and_polygon_empty(
        self, mock_massive_mod, mock_poly_mod, _use_m, _use_p, mock_yf_hist,
    ):
        from app.services.trading.market_data import fetch_ohlcv

        mock_massive_mod._is_dead_ticker.return_value = False
        mock_massive_mod.massive_aggregate_variants_all_dead.return_value = False
        mock_massive_mod.get_aggregates.return_value = []
        mock_poly_mod.get_aggregates.return_value = []

        result = fetch_ohlcv("BTC-USD")
        assert result == []
        mock_yf_hist.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_ohlcv_batch provider fallback
# ---------------------------------------------------------------------------

class TestFetchOHLCVBatchProviderFallback:
    @patch("app.services.yf_session.batch_download")
    @patch("app.services.trading.market_data._use_polygon", return_value=True)
    @patch("app.services.trading.market_data._use_massive", return_value=True)
    @patch("app.services.trading.market_data._massive")
    def test_batch_skips_polygon_yfinance_when_setting_off(
        self, mock_massive_mod, _use_m, _use_p, mock_batch_dl, monkeypatch,
    ):
        from app.services.trading import market_data as md

        monkeypatch.setattr(md.settings, "market_data_allow_provider_fallback", False)
        mock_massive_mod.get_aggregates_df_batch.return_value = {}
        mock_massive_mod.massive_aggregate_variants_all_dead.return_value = False

        result = md.fetch_ohlcv_batch(["AAPL", "MSFT"])
        assert result == {}
        mock_batch_dl.assert_not_called()

    @patch("app.services.yf_session.batch_download")
    @patch("app.services.trading.market_data._use_polygon", return_value=True)
    @patch("app.services.trading.market_data._use_massive", return_value=True)
    @patch("app.services.trading.market_data._massive")
    def test_batch_skips_fallback_when_kwarg_false(
        self, mock_massive_mod, _use_m, _use_p, mock_batch_dl, monkeypatch,
    ):
        from app.services.trading import market_data as md

        monkeypatch.setattr(md.settings, "market_data_allow_provider_fallback", True)
        mock_massive_mod.get_aggregates_df_batch.return_value = {}
        mock_massive_mod.massive_aggregate_variants_all_dead.return_value = False

        result = md.fetch_ohlcv_batch(
            ["AAPL"], allow_provider_fallback=False,
        )
        assert result == {}
        mock_batch_dl.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_quotes_batch provider selection
# ---------------------------------------------------------------------------

class TestFetchQuotesBatchProviderOrder:

    @patch("app.services.trading.market_data._use_polygon", return_value=False)
    @patch("app.services.trading.market_data._use_massive", return_value=True)
    @patch("app.services.trading.market_data._massive")
    def test_batch_massive_success(self, mock_massive_mod, _use_m, _use_p):
        from app.services.trading.market_data import fetch_quotes_batch

        mock_massive_mod.get_quotes_batch.return_value = {
            "AAPL": _make_quote(155.0),
            "NVDA": _make_quote(800.0),
        }

        result = fetch_quotes_batch(["AAPL", "NVDA"])
        assert "AAPL" in result
        assert "NVDA" in result
        assert result["AAPL"]["price"] == 155.0

    @patch("app.services.trading.market_data._yf_fast_info")
    @patch("app.services.trading.market_data._use_polygon", return_value=True)
    @patch("app.services.trading.market_data._use_massive", return_value=True)
    @patch("app.services.trading.market_data._poly")
    @patch("app.services.trading.market_data._massive")
    def test_batch_massive_partial_falls_to_polygon(
        self, mock_massive_mod, mock_poly_mod, _use_m, _use_p, _yf,
    ):
        from app.services.trading.market_data import fetch_quotes_batch

        mock_massive_mod.get_quotes_batch.return_value = {
            "AAPL": _make_quote(155.0),
        }
        mock_poly_mod.get_quotes_batch.return_value = {
            "NVDA": _make_quote(800.0),
        }

        result = fetch_quotes_batch(["AAPL", "NVDA"])
        assert "AAPL" in result
        assert "NVDA" in result


# ---------------------------------------------------------------------------
# _use_massive / _use_polygon configuration checks
# ---------------------------------------------------------------------------

class TestProviderDetection:

    @patch("app.services.trading.market_data.settings")
    def test_use_massive_true_when_key_set(self, mock_settings):
        from app.services.trading.market_data import _use_massive
        mock_settings.massive_api_key = "some-key"
        # Need to also ensure _massive_available is True
        with patch("app.services.trading.market_data._massive_available", True):
            assert _use_massive() is True

    @patch("app.services.trading.market_data.settings")
    def test_use_massive_false_when_no_key(self, mock_settings):
        from app.services.trading.market_data import _use_massive
        mock_settings.massive_api_key = ""
        assert _use_massive() is False

    @patch("app.services.trading.market_data.settings")
    def test_use_polygon_false_when_disabled(self, mock_settings):
        from app.services.trading.market_data import _use_polygon
        mock_settings.use_polygon = False
        mock_settings.polygon_api_key = "key"
        assert _use_polygon() is False


# ---------------------------------------------------------------------------
# Massive client unit tests
# ---------------------------------------------------------------------------

class TestMassiveClient:

    @patch("app.services.massive_client._get")
    @patch("app.services.massive_client._api_key", return_value="test-key")
    def test_get_stock_snapshot_parses_response(self, _key, mock_get):
        from app.services.massive_client import _get_stock_snapshot

        mock_get.return_value = {
            "status": "OK",
            "ticker": {
                "day": {"c": 150.5, "h": 152.0, "l": 149.0, "o": 149.5, "v": 1000000},
                "prevDay": {"c": 148.0},
                "lastTrade": {"p": 150.5},
                "lastQuote": {"p": 150.4, "P": 150.6},
            },
        }

        result = _get_stock_snapshot("AAPL")

        assert result is not None
        assert result["last_price"] == 150.5
        assert result["previous_close"] == 148.0
        assert result["bid"] == 150.4
        assert result["ask"] == 150.6

    @patch("app.services.massive_client._get")
    @patch("app.services.massive_client._api_key", return_value="test-key")
    def test_get_aggregates_parses_bars(self, _key, mock_get):
        from app.services.massive_client import get_aggregates, _cache

        _cache.clear()
        mock_get.return_value = {
            "resultsCount": 2,
            "results": [
                {"t": 1700000000000, "o": 148.0, "h": 150.0, "l": 147.0, "c": 149.5, "v": 500000},
                {"t": 1700086400000, "o": 149.5, "h": 152.0, "l": 149.0, "c": 151.0, "v": 600000},
            ],
        }

        bars = get_aggregates("AAPL", interval="1d", period="5d")
        assert len(bars) == 2
        assert bars[0]["close"] == 149.5
        assert bars[1]["time"] == 1700086400

    @patch("app.services.massive_client._get")
    @patch("app.services.massive_client._api_key", return_value="test-key")
    def test_get_stock_snapshot_fallback_to_prev_close(self, _key, mock_get):
        from app.services.massive_client import _get_stock_snapshot

        mock_get.side_effect = [
            {"status": "OK"},  # no "ticker" key → fallback
            {"results": [{"c": 145.0, "h": 146.0, "l": 144.0, "v": 200000}]},
        ]

        result = _get_stock_snapshot("AAPL")
        assert result is not None
        assert result["last_price"] == 145.0

    def test_to_massive_ticker_stocks(self):
        from app.services.massive_client import to_massive_ticker
        assert to_massive_ticker("AAPL") == "AAPL"
        assert to_massive_ticker("aapl") == "AAPL"

    def test_to_massive_ticker_crypto(self):
        from app.services.massive_client import to_massive_ticker
        assert to_massive_ticker("BTC-USD") == "X:BTCUSD"
        assert to_massive_ticker("ETH-USD") == "X:ETHUSD"
        assert to_massive_ticker("ZKUSD") == "X:ZKUSD"

    def test_crypto_aggregate_symbol_candidates_order(self):
        from app.services.massive_client import crypto_aggregate_symbol_candidates
        c = crypto_aggregate_symbol_candidates("ZK-USD")
        assert c[0] == "X:ZKUSD"
        assert "X:ZKUSDT" in c
        assert c == list(dict.fromkeys(c))
        assert crypto_aggregate_symbol_candidates("AAPL") == ["AAPL"]
        assert crypto_aggregate_symbol_candidates("") == []
        assert crypto_aggregate_symbol_candidates("   ") == []

    @patch("app.services.massive_client._get")
    @patch("app.services.massive_client._api_key", return_value="test-key")
    def test_empty_ticker_never_calls_massive_http(self, _key, mock_get):
        from app.services.massive_client import (
            get_last_quote,
            get_aggregates,
            _get_stock_snapshot,
            _get_prev_close,
        )

        assert get_last_quote("") is None
        assert get_last_quote("   ") is None
        assert get_aggregates("", interval="1d", period="5d") == []
        assert _get_stock_snapshot("") is None
        assert _get_prev_close("") is None
        mock_get.assert_not_called()

    def test_is_crypto(self):
        from app.services.massive_client import is_crypto
        assert is_crypto("BTC-USD") is True
        assert is_crypto("ZKUSD") is True
        assert is_crypto("AAPL") is False
        assert is_crypto("X:BTC-USD") is False


# ---------------------------------------------------------------------------
# WebSocket cache tests
# ---------------------------------------------------------------------------

class TestWSQuoteCache:

    def test_get_ws_quote_returns_fresh_snap(self):
        import time
        from app.services.massive_client import (
            get_ws_quote, QuoteSnapshot, _ws_cache, _ws_cache_lock,
        )

        with _ws_cache_lock:
            _ws_cache["TEST"] = QuoteSnapshot(
                price=100.0, bid=99.9, ask=100.1, timestamp=time.time(),
            )

        snap = get_ws_quote("TEST")
        assert snap is not None
        assert snap.price == 100.0

    def test_get_ws_quote_returns_none_for_stale(self):
        from app.services.massive_client import (
            get_ws_quote, QuoteSnapshot, _ws_cache, _ws_cache_lock,
        )

        with _ws_cache_lock:
            _ws_cache["OLD"] = QuoteSnapshot(
                price=100.0, timestamp=0.0,  # epoch = very stale
            )

        snap = get_ws_quote("OLD")
        assert snap is None

    def test_get_ws_quote_returns_none_for_missing(self):
        from app.services.massive_client import get_ws_quote
        snap = get_ws_quote("NONEXISTENT_XYZ")
        assert snap is None


# ---------------------------------------------------------------------------
# Data provider status endpoint
# ---------------------------------------------------------------------------

class TestDataProviderStatusEndpoint:

    def test_provider_status_returns_provider_order(self, client, db):
        from app.pairing import DEVICE_COOKIE_NAME

        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)

        resp = client.get("/api/trading/data-provider/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "provider_order" in data
        assert "yfinance" in data["provider_order"]
        assert "massive_enabled" in data
        assert "polygon_enabled" in data


def _make_paired(db):
    """Create a paired user+device and return (user, token)."""
    from app.models import User, Device
    user = User(name="ProviderTestUser")
    db.add(user)
    db.commit()
    db.refresh(user)
    token = "provider-test-tok"
    db.add(Device(token=token, user_id=user.id, label="test", client_ip_last="127.0.0.1"))
    db.commit()
    return user, token
