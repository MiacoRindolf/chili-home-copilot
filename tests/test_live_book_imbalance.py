"""_live_book_imbalance — venue order-flow producer for viability's Phase 4a rules.

Crypto reads the Coinbase level2 ring buffer (ratio convention, >1 bid-heavy);
equities read the Massive WS NBBO displayed sizes. Output is the SIGNED [-1, 1]
imbalance viability already scores (>0.12 boost, <-0.18 penalty).
"""
from __future__ import annotations

import time

import pytest

import app.services.massive_client as mc
from app.services.trading.momentum_neural.pipeline import _live_book_imbalance


def test_equity_imbalance_from_ws_sizes():
    with mc._ws_cache_lock:
        mc._ws_cache["IMBX"] = mc.QuoteSnapshot(
            price=5.0, bid=4.99, ask=5.01, bid_size=900, ask_size=300, timestamp=time.time())
    # (900-300)/1200 = +0.5 bid-heavy
    assert _live_book_imbalance("IMBX") == pytest.approx(0.5)


def test_equity_missing_or_stale_feed_returns_none():
    assert _live_book_imbalance("NOFEEDX") is None
    with mc._ws_cache_lock:
        mc._ws_cache["STALEX"] = mc.QuoteSnapshot(
            price=5.0, bid=4.99, ask=5.01, bid_size=900, ask_size=300, timestamp=time.time() - 60)
    assert _live_book_imbalance("STALEX") is None


def test_crypto_ratio_converts_to_signed(monkeypatch):
    import app.services.trading.microstructure as ms

    class _F:
        bid_ask_imbalance = 3.0  # heavily bid-heavy ratio

    monkeypatch.setattr(ms, "get_features", lambda pid: _F())
    # (3-1)/(3+1) = +0.5
    assert _live_book_imbalance("BTC-USD") == pytest.approx(0.5)

    class _F2:
        bid_ask_imbalance = 0.25  # ask-heavy

    monkeypatch.setattr(ms, "get_features", lambda pid: _F2())
    # (0.25-1)/(0.25+1) = -0.6
    assert _live_book_imbalance("BTC-USD") == pytest.approx(-0.6)


def test_crypto_empty_buffer_returns_none(monkeypatch):
    import app.services.trading.microstructure as ms

    class _F:
        bid_ask_imbalance = None

    monkeypatch.setattr(ms, "get_features", lambda pid: _F())
    assert _live_book_imbalance("ETH-USD") is None


def test_blank_symbol_safe():
    assert _live_book_imbalance("") is None


class _FakeDb:
    def __init__(self, row):
        self._row = row

    def execute(self, *a, **k):
        class _R:
            def __init__(self, row): self._row = row
            def fetchone(self): return self._row
        return _R(self._row)


def test_equity_prefers_fresh_iqfeed_depth_over_l1():
    with mc._ws_cache_lock:
        mc._ws_cache["DEEPX"] = mc.QuoteSnapshot(
            price=5.0, bid=4.99, ask=5.01, bid_size=900, ask_size=300, timestamp=time.time())
    # L1 would say +0.5; the fresh depth row says -0.31 -> depth wins
    assert _live_book_imbalance("DEEPX", db=_FakeDb((-0.31,))) == pytest.approx(-0.31)


def test_equity_no_depth_row_falls_back_to_l1():
    with mc._ws_cache_lock:
        mc._ws_cache["DEEPY"] = mc.QuoteSnapshot(
            price=5.0, bid=4.99, ask=5.01, bid_size=900, ask_size=300, timestamp=time.time())
    assert _live_book_imbalance("DEEPY", db=_FakeDb(None)) == pytest.approx(0.5)


def test_equity_depth_query_error_fails_open():
    class _BoomDb:
        def execute(self, *a, **k): raise RuntimeError("no table")
    with mc._ws_cache_lock:
        mc._ws_cache["DEEPZ"] = mc.QuoteSnapshot(
            price=5.0, bid=4.99, ask=5.01, bid_size=600, ask_size=600, timestamp=time.time())
    assert _live_book_imbalance("DEEPZ", db=_BoomDb()) == pytest.approx(0.0)
