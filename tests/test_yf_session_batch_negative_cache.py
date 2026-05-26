from __future__ import annotations

import pandas as pd
import pytest

from app.services import yf_session

GOOD_EQUITY = "AAPL"
MISSING_EQUITY = "BADX"
MISSING_CRYPTO = "MISSING-USD"


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


def test_batch_download_negative_caches_missing_equity_after_threshold(monkeypatch):
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

    assert yf_session._is_dead(MISSING_EQUITY) is True
    assert yf_session._is_dead(GOOD_EQUITY) is False

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
