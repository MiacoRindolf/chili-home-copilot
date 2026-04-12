"""Tests for CandleAggregator (trade ticks → OHLCV bars)."""
from __future__ import annotations

from app.services.massive_client import (
    CandleAggregator,
    OHLCVBar,
    TradeSnapshot,
)


def _trade(price: float, size: int = 100, ts: float = 1000.0) -> TradeSnapshot:
    return TradeSnapshot(price=price, size=size, timestamp=ts)


def test_single_trade_creates_bar():
    agg = CandleAggregator(interval_seconds=60)
    agg.on_trade("AAPL", _trade(150.0, 100, ts=1000.0))

    assert "AAPL" in agg._bars
    bar = agg._bars["AAPL"]
    assert bar.open == 150.0
    assert bar.high == 150.0
    assert bar.low == 150.0
    assert bar.close == 150.0
    assert bar.volume == 100.0
    assert bar.trade_count == 1
    assert not bar.closed


def test_multiple_trades_update_ohlc():
    agg = CandleAggregator(interval_seconds=60)
    agg.on_trade("AAPL", _trade(150.0, 100, ts=1000.0))
    agg.on_trade("AAPL", _trade(152.0, 200, ts=1010.0))
    agg.on_trade("AAPL", _trade(149.0, 50, ts=1020.0))
    agg.on_trade("AAPL", _trade(151.0, 75, ts=1030.0))

    bar = agg._bars["AAPL"]
    assert bar.open == 150.0
    assert bar.high == 152.0
    assert bar.low == 149.0
    assert bar.close == 151.0
    assert bar.volume == 425.0
    assert bar.trade_count == 4


def test_bucket_boundary_emits_closed_bar():
    emitted: list[OHLCVBar] = []

    agg = CandleAggregator(interval_seconds=60)
    agg.register_candle_listener("AAPL", lambda sym, bar: emitted.append(bar))

    # First bucket: 960-1019
    agg.on_trade("AAPL", _trade(150.0, 100, ts=960.0))
    agg.on_trade("AAPL", _trade(152.0, 200, ts=980.0))

    assert len(emitted) == 0  # no emission yet

    # New bucket starts at 1020 → previous bar emitted
    agg.on_trade("AAPL", _trade(155.0, 300, ts=1020.0))

    assert len(emitted) == 1
    closed = emitted[0]
    assert closed.closed is True
    assert closed.open == 150.0
    assert closed.high == 152.0
    assert closed.close == 152.0
    assert closed.volume == 300.0
    assert closed.trade_count == 2

    # New bar is open
    current = agg._bars["AAPL"]
    assert current.open == 155.0
    assert current.bucket_start == 1020.0


def test_listener_registration_and_unregistration():
    calls = []
    cb = lambda sym, bar: calls.append((sym, bar))

    agg = CandleAggregator(interval_seconds=60)
    agg.register_candle_listener("AAPL", cb)

    agg.on_trade("AAPL", _trade(150.0, 100, ts=960.0))
    agg.on_trade("AAPL", _trade(151.0, 100, ts=1020.0))  # bucket boundary
    assert len(calls) == 1

    agg.unregister_candle_listener("AAPL", cb)

    agg.on_trade("AAPL", _trade(152.0, 100, ts=1080.0))  # another boundary
    assert len(calls) == 1  # no new emission after unregister


def test_multiple_tickers_independent():
    agg = CandleAggregator(interval_seconds=60)
    agg.on_trade("AAPL", _trade(150.0, 100, ts=1000.0))
    agg.on_trade("MSFT", _trade(300.0, 50, ts=1000.0))

    assert "AAPL" in agg._bars
    assert "MSFT" in agg._bars
    assert agg._bars["AAPL"].open == 150.0
    assert agg._bars["MSFT"].open == 300.0


def test_invalidate_ohlcv_cache():
    from app.services.trading.market_data import (
        _ohlcv_df_cache,
        _ohlcv_df_lock,
        invalidate_ohlcv_cache_for_ticker,
    )
    import time

    with _ohlcv_df_lock:
        _ohlcv_df_cache["AAPL|1d|6mo|None|None"] = (time.time(), None)
        _ohlcv_df_cache["AAPL|5m|1mo|None|None"] = (time.time(), None)
        _ohlcv_df_cache["MSFT|1d|6mo|None|None"] = (time.time(), None)

    removed = invalidate_ohlcv_cache_for_ticker("AAPL")
    assert removed == 2

    with _ohlcv_df_lock:
        assert "MSFT|1d|6mo|None|None" in _ohlcv_df_cache
        assert "AAPL|1d|6mo|None|None" not in _ohlcv_df_cache
        # cleanup
        _ohlcv_df_cache.clear()
