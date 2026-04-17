"""Phase F: verify equities don't get short-circuited by the Massive
dead-variants cache.

The old behaviour was that if ``_massive.massive_aggregate_variants_all_dead(t)``
returned True, ``fetch_ohlcv`` / ``fetch_ohlcv_df`` / ``get_quote`` would
return empty **without trying Polygon or yfinance**. That is correct for
crypto (where ``X:BTCUSD`` / ``X:BTCUSDT`` / ``X:BTCUSDC`` are the only
real aggregates) but was wrong for equities where Polygon/yfinance are
entirely separate data pipes.

These tests pin the new behaviour:
  * ``AAPL`` (equity) falls through to yfinance when Massive is marked dead.
  * ``BTC-USD`` (crypto) short-circuits to ``[]`` when Massive is marked dead.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.services.trading import market_data


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    """Ensure each test is isolated from the in-memory OHLCV df cache."""
    market_data._ohlcv_df_cache.clear()
    yield
    market_data._ohlcv_df_cache.clear()


def _mk_bar(day: int, close: float) -> dict:
    # Use YYYY-01-DD style timestamps so the order is unambiguous
    return {
        "time": 1_700_000_000 + day * 86_400,
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": 10_000,
    }


class TestFetchOhlcvDeadCacheEquity:
    def test_equity_falls_through_to_yfinance(self, monkeypatch):
        """Massive all-variants-dead + equity → yfinance is still tried."""
        monkeypatch.setattr(market_data, "_use_massive", lambda: True)
        monkeypatch.setattr(market_data, "_use_polygon", lambda: False)
        # Force fallback on to keep the test realistic even if env disables it.
        monkeypatch.setattr(market_data, "_effective_allow_fallback", lambda x: True)

        m = market_data._massive
        monkeypatch.setattr(m, "get_aggregates", lambda *a, **k: [])
        monkeypatch.setattr(m, "massive_aggregate_variants_all_dead", lambda t: True)

        # yfinance fallback returns a DataFrame with Open/High/Low/Close/Volume
        yf_df = pd.DataFrame(
            {
                "Open": [100.0, 101.0],
                "High": [101.0, 102.0],
                "Low": [99.0, 100.0],
                "Close": [100.5, 101.5],
                "Volume": [10_000, 11_000],
            },
            index=pd.to_datetime(["2026-01-02", "2026-01-03"]),
        )
        monkeypatch.setattr(market_data, "_yf_history", lambda *a, **k: yf_df)

        bars = market_data.fetch_ohlcv("AAPL", interval="1d", period="5d")
        assert bars, "equity should reach yfinance fallback even when Massive is dead"
        assert len(bars) == 2
        assert bars[0]["close"] == 100.5

    def test_crypto_short_circuits(self, monkeypatch):
        """Massive all-variants-dead + crypto → [] without hitting Polygon/yfinance."""
        monkeypatch.setattr(market_data, "_use_massive", lambda: True)
        monkeypatch.setattr(market_data, "_use_polygon", lambda: True)
        monkeypatch.setattr(market_data, "_effective_allow_fallback", lambda x: True)

        m = market_data._massive
        monkeypatch.setattr(m, "get_aggregates", lambda *a, **k: [])
        monkeypatch.setattr(m, "massive_aggregate_variants_all_dead", lambda t: True)

        polygon_called = {"count": 0}

        def _poly_spy(*_args, **_kwargs):
            polygon_called["count"] += 1
            return [_mk_bar(1, 50_000.0)]

        monkeypatch.setattr(market_data._poly, "get_aggregates", _poly_spy)

        def _yf_spy(*_args, **_kwargs):
            raise AssertionError("yfinance must NOT be called for crypto dead-cache")

        monkeypatch.setattr(market_data, "_yf_history", _yf_spy)

        bars = market_data.fetch_ohlcv("BTC-USD", interval="1d", period="5d")
        assert bars == []
        assert polygon_called["count"] == 0


class TestFetchOhlcvDfDeadCacheEquity:
    def test_equity_df_falls_through_to_yfinance(self, monkeypatch):
        monkeypatch.setattr(market_data, "_use_massive", lambda: True)
        monkeypatch.setattr(market_data, "_use_polygon", lambda: False)
        monkeypatch.setattr(market_data, "_effective_allow_fallback", lambda x: True)

        m = market_data._massive
        monkeypatch.setattr(
            m, "get_aggregates_df",
            lambda *a, **k: pd.DataFrame(),
        )
        monkeypatch.setattr(m, "massive_aggregate_variants_all_dead", lambda t: True)

        yf_df = pd.DataFrame(
            {
                "Open": [100.0, 101.0],
                "High": [101.0, 102.0],
                "Low": [99.0, 100.0],
                "Close": [100.5, 101.5],
                "Volume": [10_000, 11_000],
            },
            index=pd.to_datetime(["2026-01-02", "2026-01-03"]),
        )
        monkeypatch.setattr(market_data, "_yf_history", lambda *a, **k: yf_df)

        df = market_data.fetch_ohlcv_df("MSFT", interval="1d", period="5d")
        assert not df.empty
        assert len(df) == 2

    def test_crypto_df_short_circuits(self, monkeypatch):
        monkeypatch.setattr(market_data, "_use_massive", lambda: True)
        monkeypatch.setattr(market_data, "_use_polygon", lambda: True)
        monkeypatch.setattr(market_data, "_effective_allow_fallback", lambda x: True)

        m = market_data._massive
        monkeypatch.setattr(m, "get_aggregates_df", lambda *a, **k: pd.DataFrame())
        monkeypatch.setattr(m, "massive_aggregate_variants_all_dead", lambda t: True)

        def _poly_spy(*_args, **_kwargs):
            raise AssertionError("Polygon must NOT be called for crypto dead-cache")

        monkeypatch.setattr(
            market_data._poly, "get_aggregates_df", _poly_spy,
        )

        def _yf_spy(*_args, **_kwargs):
            raise AssertionError("yfinance must NOT be called for crypto dead-cache")

        monkeypatch.setattr(market_data, "_yf_history", _yf_spy)

        df = market_data.fetch_ohlcv_df("ETH-USD", interval="1d", period="5d")
        assert df.empty
