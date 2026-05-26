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

TEST_CRYPTO_TICKER = "BTC-USD"
TEST_COINBASE_PRICE = 42_000.25
TEST_COINBASE_BID = 42_000.0
TEST_COINBASE_ASK = 42_000.5
TEST_COINBASE_VOLUME = 123.45
TEST_COINBASE_QUOTE_TIME = "2026-05-25T22:45:00Z"
TEST_COINBASE_TIMEOUT_S = 3.5


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
        monkeypatch.setattr(market_data.settings, "brain_market_data_coinbase_fallback", False)

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
        monkeypatch.setattr(market_data.settings, "brain_market_data_coinbase_fallback", False)

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


class TestFetchQuoteDeadCacheCrypto:
    def test_crypto_quote_uses_coinbase_when_massive_dead(self, monkeypatch):
        """Massive exhausted + crypto quote -> Coinbase public ticker fallback."""
        from app.services.trading import coinbase_ohlcv

        monkeypatch.setattr("app.services.trading.price_bus.get_live_quote", lambda _t: None)
        monkeypatch.setattr(market_data, "_use_massive", lambda: True)
        monkeypatch.setattr(market_data, "_use_polygon", lambda: False)
        monkeypatch.setattr(market_data, "_effective_allow_fallback", lambda _x: True)
        monkeypatch.setattr(market_data.settings, "brain_market_data_coinbase_fallback", True)

        m = market_data._massive
        monkeypatch.setattr(m, "get_ws_quote", lambda _t: None)
        monkeypatch.setattr(m, "get_last_quote", lambda _t: None)
        monkeypatch.setattr(m, "massive_aggregate_variants_all_dead", lambda _t: True)
        monkeypatch.setattr(m, "is_crypto", lambda _t: True)

        monkeypatch.setattr(
            coinbase_ohlcv,
            "get_quote",
            lambda _t: {
                "last_price": TEST_COINBASE_PRICE,
                "bid": TEST_COINBASE_BID,
                "ask": TEST_COINBASE_ASK,
                "provider": "coinbase_public",
            },
        )
        monkeypatch.setattr(
            market_data,
            "_yf_fast_info",
            lambda _t: (_ for _ in ()).throw(
                AssertionError("yfinance should not be needed after Coinbase quote fallback")
            ),
        )

        quote = market_data.fetch_quote(TEST_CRYPTO_TICKER)
        assert quote is not None
        assert quote["price"] == TEST_COINBASE_PRICE
        assert quote["bid"] == TEST_COINBASE_BID
        assert quote["ask"] == TEST_COINBASE_ASK
        assert quote["source"] == "coinbase_public"


class TestCoinbasePublicQuote:
    def test_get_quote_normalizes_public_ticker_payload(self, monkeypatch):
        from app.services.trading import coinbase_ohlcv

        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {
                    "price": str(TEST_COINBASE_PRICE),
                    "bid": str(TEST_COINBASE_BID),
                    "ask": str(TEST_COINBASE_ASK),
                    "volume": str(TEST_COINBASE_VOLUME),
                    "time": TEST_COINBASE_QUOTE_TIME,
                }

        seen: dict[str, object] = {}

        def _fake_get(url: str, *, timeout: float):
            seen["url"] = url
            seen["timeout"] = timeout
            return _Response()

        monkeypatch.setenv(
            "CHILI_COINBASE_MARKET_DATA_TIMEOUT_SECONDS",
            str(TEST_COINBASE_TIMEOUT_S),
        )
        monkeypatch.setattr(coinbase_ohlcv, "_CIRCUIT_OPEN_UNTIL", 0.0)
        monkeypatch.setattr(coinbase_ohlcv._SESSION, "get", _fake_get)

        quote = coinbase_ohlcv.get_quote(TEST_CRYPTO_TICKER.lower())
        assert quote == {
            "last_price": TEST_COINBASE_PRICE,
            "provider": "coinbase_public",
            "bid": TEST_COINBASE_BID,
            "ask": TEST_COINBASE_ASK,
            "volume": TEST_COINBASE_VOLUME,
            "quote_ts": TEST_COINBASE_QUOTE_TIME,
        }
        assert str(seen["url"]).endswith(f"/products/{TEST_CRYPTO_TICKER}/ticker")
        assert seen["timeout"] == TEST_COINBASE_TIMEOUT_S
