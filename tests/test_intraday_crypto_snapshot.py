"""Test the intraday crypto snapshot capture extracts crypto from a mixed
universe.

The scheduled snapshot job now passes the FULL universe (not the equity-ranked
top-N cap, which holds 0 crypto during market hours) to
_take_intraday_crypto_snapshots, which applies its own '-USD' filter. This pins
that filter so the crypto miner gets fresh intraday snapshots regardless of
equity ranking.
"""
import pytest

from app.services.trading import learning
from app.config import settings


def test_intraday_extracts_crypto_from_mixed_universe(monkeypatch):
    monkeypatch.setattr(settings, "brain_intraday_snapshots_enabled", True)
    monkeypatch.setattr(settings, "brain_intraday_max_tickers", 1000)
    monkeypatch.setattr(settings, "brain_intraday_intervals", "5m")

    calls = []

    def _fake_parallel(db, tickers, **kw):
        calls.append((kw.get("bar_interval"), list(tickers)))
        return len(tickers)

    monkeypatch.setattr(learning, "take_snapshots_parallel", _fake_parallel)

    mixed = ["AAPL", "MSFT", "BTC-USD", "ETH-USD", "GOOG", "SOL-USD"]
    n = learning._take_intraday_crypto_snapshots(None, mixed, max_workers=2)

    assert n == 3  # only the 3 crypto tickers
    assert calls and calls[0][0] == "5m"
    assert set(calls[0][1]) == {"BTC-USD", "ETH-USD", "SOL-USD"}


def test_intraday_disabled_returns_zero(monkeypatch):
    monkeypatch.setattr(settings, "brain_intraday_snapshots_enabled", False)
    monkeypatch.setattr(
        learning, "take_snapshots_parallel",
        lambda *a, **k: pytest.fail("should not snapshot when disabled"),
    )
    assert learning._take_intraday_crypto_snapshots(None, ["BTC-USD"], max_workers=1) == 0


def test_intraday_no_crypto_in_universe_writes_nothing(monkeypatch):
    # Equity-only universe (the bug condition) -> 0 written, no crash.
    monkeypatch.setattr(settings, "brain_intraday_snapshots_enabled", True)
    monkeypatch.setattr(settings, "brain_intraday_max_tickers", 1000)
    monkeypatch.setattr(settings, "brain_intraday_intervals", "5m,15m")
    calls = []
    monkeypatch.setattr(
        learning, "take_snapshots_parallel",
        lambda db, tickers, **kw: (calls.append(list(tickers)) or len(tickers)),
    )
    n = learning._take_intraday_crypto_snapshots(None, ["AAPL", "MSFT"], max_workers=1)
    assert n == 0
    assert calls == []  # crypto filter empty -> no snapshot call at all
