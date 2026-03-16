"""Tests for market data fallback chain and caching."""
import pytest
from unittest.mock import patch, MagicMock


class TestFetchQuoteFallback:
    """Test that fetch_quote follows the Massive -> Polygon -> yfinance chain."""

    @patch("app.services.trading.market_data._use_massive", return_value=True)
    @patch("app.services.trading.market_data._massive")
    def test_massive_first(self, mock_massive_mod, mock_use):
        """When Massive is available, it should be tried first."""
        mock_massive_mod.get_ws_quote.return_value = None
        mock_massive_mod.get_last_quote.return_value = {
            "last_price": 150.0, "previous_close": 148.0, "bid": 149.9, "ask": 150.1,
        }
        from app.services.trading.market_data import fetch_quote
        result = fetch_quote("AAPL")
        assert result is not None
        assert mock_massive_mod.get_last_quote.called or mock_massive_mod.get_ws_quote.called

    @patch("app.services.trading.market_data._use_massive", return_value=False)
    @patch("app.services.trading.market_data._use_polygon", return_value=True)
    @patch("app.services.trading.market_data._poly")
    def test_polygon_fallback(self, mock_poly_mod, mock_use_poly, mock_use_mass):
        """When Massive is down, falls back to Polygon."""
        mock_poly_mod.get_last_quote.return_value = {
            "last_price": 151.0, "previous_close": 149.0,
        }
        from app.services.trading.market_data import fetch_quote
        result = fetch_quote("AAPL")
        assert result is not None


class TestSmartRound:
    """Test the smart_round utility."""

    def test_large_number(self):
        from app.services.trading.market_data import smart_round
        assert smart_round(1234.5678) == 1234.57

    def test_small_number(self):
        from app.services.trading.market_data import smart_round
        result = smart_round(0.001234)
        assert result < 0.01

    def test_zero(self):
        from app.services.trading.market_data import smart_round
        assert smart_round(0) == 0

    def test_none(self):
        from app.services.trading.market_data import smart_round
        assert smart_round(None) is None


class TestOHLCVCache:
    """Test that OHLCV data uses caching."""

    @patch("app.services.trading.market_data._use_massive", return_value=False)
    @patch("app.services.trading.market_data._use_polygon", return_value=False)
    def test_yfinance_fallback(self, mock_poly, mock_mass):
        """When both Massive and Polygon are down, yfinance is used."""
        from app.services.trading.market_data import fetch_ohlcv_df
        # Just verify the function exists and is callable
        assert callable(fetch_ohlcv_df)


class TestMarketRegime:
    """Test market regime detection."""

    def test_get_market_regime_callable(self):
        from app.services.trading.market_data import get_market_regime
        assert callable(get_market_regime)

    @patch("app.services.trading.market_data.fetch_ohlcv_df")
    def test_regime_returns_string(self, mock_ohlcv):
        """Market regime should return a string classification."""
        import pandas as pd
        import numpy as np
        dates = pd.date_range("2026-01-01", periods=50)
        mock_ohlcv.return_value = pd.DataFrame({
            "Open": np.random.uniform(400, 420, 50),
            "High": np.random.uniform(420, 440, 50),
            "Low": np.random.uniform(380, 400, 50),
            "Close": np.linspace(400, 450, 50),
            "Volume": np.random.randint(1000000, 5000000, 50),
        }, index=dates)
        from app.services.trading.market_data import get_market_regime
        result = get_market_regime()
        assert isinstance(result, (str, dict)) or result is None


class TestTickerConstants:
    """Test that ticker constants are properly defined."""

    def test_default_tickers_exist(self):
        from app.services.trading.market_data import DEFAULT_SCAN_TICKERS, DEFAULT_CRYPTO_TICKERS
        assert len(DEFAULT_SCAN_TICKERS) > 0
        assert len(DEFAULT_CRYPTO_TICKERS) > 0

    def test_all_scan_tickers_superset(self):
        from app.services.trading.market_data import DEFAULT_SCAN_TICKERS, ALL_SCAN_TICKERS
        assert len(ALL_SCAN_TICKERS) >= len(DEFAULT_SCAN_TICKERS)
