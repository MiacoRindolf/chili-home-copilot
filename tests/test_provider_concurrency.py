"""Tests for the provider-aware I/O concurrency model.

The redesign separates CPU-bound compute sizing (cgroup-effective CPUs) from
I/O concurrency (bound by each provider's rate budget, NOT CPU). These tests
pin the routing + sizing so a future change can't silently re-introduce the
legacy failure mode (a fast provider throttled to a slow one's pace, or a
rate-limited provider hammered into 429 backoffs).
"""
from types import SimpleNamespace

import pytest

from app.services.trading.brain_io_concurrency import (
    massive_fetch_concurrency,
    polygon_fetch_concurrency,
    coinbase_fetch_concurrency,
    yfinance_fetch_concurrency,
    io_workers_for_provider,
    ohlcv_provider_for_ticker,
    split_tickers_by_provider,
    parallel_fetch_by_provider,
    io_fanout_workers,
    cpu_workers,
)


def _settings(**kw):
    base = dict(
        massive_max_rps=100,
        massive_http_pool_maxsize=512,
        market_data_polygon_batch_workers=48,
        universe_snapshot_fetch_concurrency=4,
        coinbase_fetch_concurrency=None,
        yfinance_fetch_concurrency=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --- provider routing -------------------------------------------------------
def test_crypto_routes_to_coinbase():
    assert ohlcv_provider_for_ticker("BTC-USD") == "coinbase"
    assert ohlcv_provider_for_ticker("eth-usd") == "coinbase"


def test_equity_routes_to_massive():
    assert ohlcv_provider_for_ticker("AAPL") == "massive"
    assert ohlcv_provider_for_ticker("SPY") == "massive"


def test_split_partitions_mixed_universe():
    groups = split_tickers_by_provider(["AAPL", "BTC-USD", "MSFT", "ETH-USD", "GOOG"])
    assert set(groups["massive"]) == {"AAPL", "MSFT", "GOOG"}
    assert set(groups["coinbase"]) == {"BTC-USD", "ETH-USD"}


def test_split_equity_only_has_no_coinbase_group():
    groups = split_tickers_by_provider(["AAPL", "MSFT"])
    assert list(groups.keys()) == ["massive"]


# --- provider sizing: Massive is FAST, Coinbase is GENTLE -------------------
def test_massive_is_high_concurrency():
    # min(80, max(30, rps)) bounded by pool//2; rps=100 -> 80, pool 512//2=256 -> 80
    assert massive_fetch_concurrency(_settings()) == 80


def test_massive_bounded_by_small_pool():
    # tiny pool clamps below the rps-derived 80
    assert massive_fetch_concurrency(_settings(massive_http_pool_maxsize=40)) == 20


def test_coinbase_is_gentle():
    # matches the fast-path's proven snapshot concurrency (avoid 429 storms)
    assert coinbase_fetch_concurrency(_settings()) == 4


def test_coinbase_explicit_override():
    assert coinbase_fetch_concurrency(_settings(coinbase_fetch_concurrency=6)) == 6


def test_yfinance_is_gentle():
    assert yfinance_fetch_concurrency(_settings()) == 4


def test_polygon_uses_dedicated_setting():
    assert polygon_fetch_concurrency(_settings()) == 48


def test_coinbase_strictly_gentler_than_massive():
    s = _settings()
    assert coinbase_fetch_concurrency(s) < massive_fetch_concurrency(s)


# --- dispatch + clamping ----------------------------------------------------
def test_io_workers_for_provider_dispatch():
    s = _settings()
    assert io_workers_for_provider("massive", 1000, s) == 80
    assert io_workers_for_provider("coinbase", 1000, s) == 4
    assert io_workers_for_provider("polygon", 1000, s) == 48


def test_io_workers_clamped_to_n_items():
    s = _settings()
    # never spin more threads than there are tickers
    assert io_workers_for_provider("massive", 3, s) == 3
    assert io_workers_for_provider("coinbase", 2, s) == 2


def test_unknown_provider_falls_back_to_gentlest():
    s = _settings()
    # unknown/mixed must NOT accidentally hammer — defaults to Coinbase-gentle
    assert io_workers_for_provider("mystery", 1000, s) == coinbase_fetch_concurrency(s)


def test_io_workers_floor_is_one():
    s = _settings()
    assert io_workers_for_provider("massive", 0, s) == 1


# --- CPU-bound sizing is cgroup-aware, not host-cpu ------------------------
def test_cpu_workers_tracks_effective_budget(monkeypatch):
    import app.services.trading.brain_io_concurrency as mod
    monkeypatch.setattr(mod, "effective_cpu_budget", lambda s=None: 5.0)
    assert cpu_workers(None) == 5
    assert cpu_workers(None, multiplier=2.0) == 10
    assert cpu_workers(None, ceiling=8, multiplier=2.0) == 8
    assert cpu_workers(None, floor=3, multiplier=0.0) == 3


def test_cpu_workers_never_below_one(monkeypatch):
    import app.services.trading.brain_io_concurrency as mod
    monkeypatch.setattr(mod, "effective_cpu_budget", lambda s=None: 1.0)
    assert cpu_workers(None, multiplier=0.1) == 1


# --- io_fanout_workers: bound by task count, not CPU -----------------------
def test_io_fanout_sizes_to_task_count():
    assert io_fanout_workers(6, None, ceiling=24) == 6
    assert io_fanout_workers(40, None, ceiling=24) == 24  # capped at ceiling


def test_io_fanout_ceiling_override():
    s = SimpleNamespace(brain_io_fanout_ceiling=4)
    # a global throttle can lower it below the per-call ceiling
    assert io_fanout_workers(40, s, ceiling=24) == 4
    assert io_fanout_workers(2, s, ceiling=24) == 2  # still clamped to tasks


def test_io_fanout_floor_is_one():
    assert io_fanout_workers(0, None, ceiling=24) == 1


# --- parallel_fetch_by_provider: splits, runs, collects --------------------
def test_parallel_fetch_splits_and_collects():
    s = _settings()
    seen = {}

    def work(t):
        seen[t] = True
        return f"r:{t}"

    out = parallel_fetch_by_provider(
        ["AAPL", "BTC-USD", "MSFT", "ETH-USD"], work, s,
    )
    assert set(out) == {"r:AAPL", "r:BTC-USD", "r:MSFT", "r:ETH-USD"}
    assert len(seen) == 4  # every item ran exactly once


def test_parallel_fetch_ticker_of_routing():
    s = _settings()
    items = [{"sym": "AAPL"}, {"sym": "BTC-USD"}]
    out = parallel_fetch_by_provider(
        items, lambda it: it["sym"], s, ticker_of=lambda it: it["sym"],
    )
    assert set(out) == {"AAPL", "BTC-USD"}


def test_parallel_fetch_drops_exceptions():
    s = _settings()

    def work(t):
        if t == "BAD":
            raise ValueError("boom")
        return t

    out = parallel_fetch_by_provider(["AAPL", "BAD", "MSFT"], work, s)
    assert set(out) == {"AAPL", "MSFT"}  # exception dropped, others survive


def test_parallel_fetch_empty():
    assert parallel_fetch_by_provider([], lambda t: t, _settings()) == []
