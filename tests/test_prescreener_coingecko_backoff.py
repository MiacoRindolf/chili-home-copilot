from __future__ import annotations

import pytest
import requests

from app.services.trading import prescreener


COINGECKO_RATE_LIMIT_PAYLOAD = {"error": "rate limited"}
CACHE_EXPIRY_MARGIN_S = 1


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code < prescreener._COINGECKO_RATE_LIMIT_STATUS:
            return
        exc = requests.HTTPError(str(self._payload))
        exc.response = self
        raise exc

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _reset_prescreener_cache():
    with prescreener._cache_lock:
        prescreener._cache.clear()
    yield
    with prescreener._cache_lock:
        prescreener._cache.clear()


def _expire_top_movers_fresh_cache() -> None:
    with prescreener._cache_lock:
        ts, val, ttl = prescreener._cache[
            prescreener._COINGECKO_TOP_MOVERS_CACHE_KEY
        ]
        prescreener._cache[prescreener._COINGECKO_TOP_MOVERS_CACHE_KEY] = (
            ts - ttl - CACHE_EXPIRY_MARGIN_S,
            val,
            ttl,
        )


def test_crypto_top_movers_caches_fresh_and_stale_candidates(monkeypatch):
    response = _FakeResponse(
        200,
        [
            {"symbol": "btc"},
            {"symbol": "usdt"},
            {"symbol": "sol"},
        ],
    )
    monkeypatch.setattr(prescreener._HTTP_SESSION, "get", lambda *_a, **_k: response)

    assert prescreener._crypto_top_movers() == ["BTC-USD", "SOL-USD"]

    assert prescreener._cache_get(
        prescreener._COINGECKO_TOP_MOVERS_CACHE_KEY
    ) == ["BTC-USD", "SOL-USD"]
    assert prescreener._cache_get(
        prescreener._COINGECKO_TOP_MOVERS_STALE_CACHE_KEY
    ) == ["BTC-USD", "SOL-USD"]


def test_crypto_top_movers_uses_stale_candidates_during_429_backoff(
    monkeypatch,
):
    calls: list[object] = []
    responses = [
        _FakeResponse(200, [{"symbol": "btc"}]),
        _FakeResponse(
            prescreener._COINGECKO_RATE_LIMIT_STATUS,
            COINGECKO_RATE_LIMIT_PAYLOAD,
        ),
    ]

    def _get(*_args, **_kwargs):
        calls.append(object())
        return responses.pop(0)

    monkeypatch.setattr(prescreener._HTTP_SESSION, "get", _get)

    assert prescreener._crypto_top_movers() == ["BTC-USD"]
    _expire_top_movers_fresh_cache()

    assert prescreener._crypto_top_movers() == ["BTC-USD"]
    calls_before_backoff_hit = len(calls)

    assert prescreener._crypto_top_movers() == ["BTC-USD"]
    assert len(calls) == calls_before_backoff_hit
    assert prescreener._coingecko_top_movers_backoff_remaining_s() > 0


def test_crypto_top_movers_empty_response_uses_default_without_caching_empty(
    monkeypatch,
):
    response = _FakeResponse(200, [{"symbol": "usdt"}])
    monkeypatch.setattr(prescreener._HTTP_SESSION, "get", lambda *_a, **_k: response)
    fallback = ["BTC-USD", "ETH-USD"]
    monkeypatch.setattr(
        prescreener,
        "_crypto_top_movers_stale_or_default",
        lambda: fallback,
    )

    assert prescreener._crypto_top_movers() == fallback
    assert prescreener._cache_get(
        prescreener._COINGECKO_TOP_MOVERS_CACHE_KEY
    ) is None
