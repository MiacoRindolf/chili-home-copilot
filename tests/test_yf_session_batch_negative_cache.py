from __future__ import annotations

import pandas as pd
import pytest

from app.services import yf_session

GOOD_EQUITY = "AAPL"
MISSING_EQUITY = "BADX"
MISSING_CRYPTO = "MISSING-USD"
FAST_INFO_EMPTY_ERROR = "'PriceHistory' object has no attribute '_dividends'"
EXTRA_PROBE_AFTER_THRESHOLD = 1
CACHE_EXPIRY_MARGIN_S = 1.0
FIRST_FAST_INFO_PROBE_COUNT = 1
SECOND_FAST_INFO_PROBE_COUNT = 2
BATCH_MISS_FIRST_PROBE_COUNT = 1
BATCH_MISS_SECOND_PROBE_COUNT = 2


class _RaisingFastInfoTicker:
    def __init__(self, exc: Exception):
        self._exc = exc

    @property
    def fast_info(self):
        raise self._exc


def _expire_quote_miss(symbol: str) -> None:
    key = yf_session._quote_miss_key(symbol)
    with yf_session._cache_lock:
        ts, val = yf_session._cache[key]
        yf_session._cache[key] = (
            ts - yf_session._TTL_QUOTE_MISS - CACHE_EXPIRY_MARGIN_S,
            val,
        )


def _expire_batch_miss(symbol: str) -> None:
    key = yf_session._batch_miss_key(symbol)
    with yf_session._cache_lock:
        ts, val = yf_session._cache[key]
        yf_session._cache[key] = (
            ts - yf_session._TTL_BATCH_MISS - CACHE_EXPIRY_MARGIN_S,
            val,
        )


@pytest.fixture(autouse=True)
def _reset_yf_state(monkeypatch):
    with yf_session._cache_lock:
        yf_session._cache.clear()
    with yf_session._dead_lock:
        yf_session._dead_tickers.clear()
    with yf_session._empty_lock:
        yf_session._empty_counts.clear()
    yf_session._reset_breaker_for_tests()
    monkeypatch.setattr(yf_session, "acquire", lambda: None)
    yield
    with yf_session._cache_lock:
        yf_session._cache.clear()
    with yf_session._dead_lock:
        yf_session._dead_tickers.clear()
    with yf_session._empty_lock:
        yf_session._empty_counts.clear()
    yf_session._reset_breaker_for_tests()


def test_batch_download_does_not_dead_cache_mixed_batch_missing_equity(
    monkeypatch,
):
    calls: list[tuple[str, ...]] = []

    def _download(symbols, **_kwargs):
        symbols = tuple(symbols)
        calls.append(symbols)
        if symbols == (MISSING_EQUITY,):
            return pd.DataFrame()
        columns = pd.MultiIndex.from_product([
            [GOOD_EQUITY],
            ["Open", "High", "Low", "Close", "Volume"],
        ])
        return pd.DataFrame([[1.0, 1.1, 0.9, 1.0, 1000]], columns=columns)

    monkeypatch.setattr(yf_session.yf, "download", _download)

    for _ in range(yf_session._EMPTY_THRESHOLD):
        yf_session.batch_download([GOOD_EQUITY, MISSING_EQUITY], period="5d")

    assert yf_session._is_dead(MISSING_EQUITY) is False
    assert yf_session._is_dead(GOOD_EQUITY) is False
    assert len(calls) == BATCH_MISS_FIRST_PROBE_COUNT

    _expire_batch_miss(MISSING_EQUITY)
    yf_session.batch_download([GOOD_EQUITY, MISSING_EQUITY], period="5d")

    assert yf_session._is_dead(MISSING_EQUITY) is False
    assert calls[-1] == (MISSING_EQUITY,)
    assert len(calls) == BATCH_MISS_SECOND_PROBE_COUNT


def test_batch_download_negative_caches_single_missing_equity_after_threshold(
    monkeypatch,
):
    calls: list[tuple[str, ...]] = []

    def _download(symbols, **_kwargs):
        calls.append(tuple(symbols))
        return pd.DataFrame()

    monkeypatch.setattr(yf_session.yf, "download", _download)

    for _ in range(yf_session._EMPTY_THRESHOLD):
        yf_session.batch_download([MISSING_EQUITY], period="5d")

    assert yf_session._is_dead(MISSING_EQUITY) is True

    calls_before_dead_skip = len(calls)
    yf_session.batch_download([MISSING_EQUITY], period="5d")
    assert len(calls) == calls_before_dead_skip


def test_batch_download_negative_caches_missing_crypto_with_short_crypto_ttl(
    monkeypatch,
):
    monkeypatch.setattr(
        yf_session.yf,
        "download",
        lambda *_args, **_kwargs: pd.DataFrame(),
    )

    for _ in range(yf_session._EMPTY_THRESHOLD):
        yf_session.batch_download([MISSING_CRYPTO], period="5d")

    assert yf_session._is_dead(MISSING_CRYPTO) is True


def test_fast_info_negative_caches_explicit_missing_equity_after_threshold(
    monkeypatch,
):
    calls: list[str] = []

    def _ticker(symbol, **_kwargs):
        calls.append(symbol)
        return _RaisingFastInfoTicker(
            Exception("No data found, symbol may be delisted")
        )

    monkeypatch.setattr(yf_session.yf, "Ticker", _ticker)

    for _ in range(yf_session._EMPTY_THRESHOLD):
        assert yf_session.get_fast_info(MISSING_EQUITY) is None

    assert yf_session._is_dead(MISSING_EQUITY) is True

    calls_before_dead_skip = len(calls)
    assert yf_session.get_fast_info(MISSING_EQUITY) is None
    assert len(calls) == calls_before_dead_skip


def test_fast_info_internal_empty_error_short_caches_without_dead_cache(
    monkeypatch,
):
    calls: list[str] = []

    def _ticker(symbol, **_kwargs):
        calls.append(symbol)
        return _RaisingFastInfoTicker(Exception(FAST_INFO_EMPTY_ERROR))

    monkeypatch.setattr(yf_session.yf, "Ticker", _ticker)

    for _ in range(yf_session._EMPTY_THRESHOLD + EXTRA_PROBE_AFTER_THRESHOLD):
        assert yf_session.get_fast_info(GOOD_EQUITY) is None

    assert yf_session._is_dead(GOOD_EQUITY) is False
    assert len(calls) == FIRST_FAST_INFO_PROBE_COUNT

    _expire_quote_miss(GOOD_EQUITY)
    assert yf_session.get_fast_info(GOOD_EQUITY) is None
    assert yf_session._is_dead(GOOD_EQUITY) is False
    assert len(calls) == SECOND_FAST_INFO_PROBE_COUNT


def test_fast_info_negative_caches_crypto_and_uses_fallback_after_threshold(
    monkeypatch,
):
    calls: list[str] = []
    fallback_calls: list[str] = []
    fallback_quote = {"last_price": 1.23, "previous_close": 1.11}

    def _ticker(symbol, **_kwargs):
        calls.append(symbol)
        return _RaisingFastInfoTicker(Exception(FAST_INFO_EMPTY_ERROR))

    def _fallback(symbol):
        fallback_calls.append(symbol)
        return fallback_quote

    monkeypatch.setattr(yf_session.yf, "Ticker", _ticker)
    monkeypatch.setattr(yf_session, "_coingecko_quote", _fallback)

    for _ in range(yf_session._EMPTY_THRESHOLD - 1):
        assert yf_session.get_fast_info(MISSING_CRYPTO) is None

    assert yf_session.get_fast_info(MISSING_CRYPTO) == fallback_quote
    assert yf_session._is_dead(MISSING_CRYPTO) is True
    assert fallback_calls == [MISSING_CRYPTO]

    calls_before_cached_fallback = len(calls)
    assert yf_session.get_fast_info(MISSING_CRYPTO) == fallback_quote
    assert len(calls) == calls_before_cached_fallback
