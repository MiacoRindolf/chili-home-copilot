"""Tests for the proactive Coinbase HTTP rate gate (``coinbase_ohlcv``).

The gate spaces ``_SESSION.get`` calls under Coinbase's public limit so bursts
(fast scan + cost gate + mining/backtest candle fetches sharing the process)
don't trip 429s + the 60s reactive backoff. Reservation is checked
deterministically (not wall-clock) to avoid timing flakiness.
"""
import time

from app.services.trading import coinbase_ohlcv as cb


def test_rate_gate_disabled_is_noop(monkeypatch):
    # 0 rps -> gate is inert (no spacing), a clean escape hatch.
    monkeypatch.setenv("CHILI_COINBASE_OHLCV_MAX_REQUESTS_PER_SECOND", "0")
    cb._RATE_GATE_NEXT_AT = 0.0
    t0 = time.monotonic()
    for _ in range(50):
        cb._coinbase_rate_gate()
    assert time.monotonic() - t0 < 0.05
    assert cb._coinbase_max_rps() == 0.0


def test_rate_gate_reserves_spaced_slots(monkeypatch):
    # 10 rps -> consecutive call slots reserved 0.1s apart.
    monkeypatch.setenv("CHILI_COINBASE_OHLCV_MAX_REQUESTS_PER_SECOND", "10")
    cb._RATE_GATE_NEXT_AT = 0.0
    cb._coinbase_rate_gate()
    first = cb._RATE_GATE_NEXT_AT
    cb._coinbase_rate_gate()
    second = cb._RATE_GATE_NEXT_AT
    assert abs((second - first) - 0.1) < 0.02
    assert cb._coinbase_max_rps() == 10.0


def test_rate_gate_bad_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CHILI_COINBASE_OHLCV_MAX_REQUESTS_PER_SECOND", "not-a-number")
    assert cb._coinbase_max_rps() == cb._COINBASE_DEFAULT_MAX_RPS
