from __future__ import annotations

import logging

import requests

from app.services.trading import coinbase_ohlcv


MISSING_PRODUCT = "NOPE-USD"
CACHE_EXPIRY_MARGIN_S = 1.0


class _Response:
    status_code = coinbase_ohlcv._COINBASE_PRODUCT_NOT_FOUND_STATUS

    def raise_for_status(self) -> None:
        exc = requests.HTTPError("404 Client Error: Not Found")
        exc.response = self
        raise exc


class _CatalogResponse:
    status_code = 200

    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._rows


class _QuoteResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"price": "123.45", "bid": "123.40", "ask": "123.50"}


class _RateLimitResponse:
    status_code = 429
    headers = {"Retry-After": "2"}

    def raise_for_status(self) -> None:
        exc = requests.HTTPError("429 Client Error: Too Many Requests")
        exc.response = self
        raise exc


def _raise_not_found(*_args, **_kwargs):
    return _Response()


def _expire_missing_product(product_id: str) -> None:
    with coinbase_ohlcv._MISSING_PRODUCT_LOCK:
        coinbase_ohlcv._MISSING_PRODUCTS[product_id] = (
            coinbase_ohlcv.time.time() - CACHE_EXPIRY_MARGIN_S
        )


def setup_function() -> None:
    coinbase_ohlcv.reset_missing_product_cache_for_tests()


def teardown_function() -> None:
    coinbase_ohlcv.reset_missing_product_cache_for_tests()


def test_get_ohlcv_caches_coinbase_product_404(monkeypatch):
    calls: list[object] = []

    def _get(*args, **kwargs):
        calls.append((args, kwargs))
        return _raise_not_found(*args, **kwargs)

    monkeypatch.setattr(coinbase_ohlcv._SESSION, "get", _get)

    assert coinbase_ohlcv.get_ohlcv(MISSING_PRODUCT, interval="1d", period="1d") == []
    assert calls

    calls_after_first_404 = len(calls)
    assert coinbase_ohlcv.get_ohlcv(MISSING_PRODUCT, interval="1d", period="1d") == []
    assert len(calls) == calls_after_first_404


def test_get_quote_reuses_coinbase_product_404_cache(monkeypatch):
    calls: list[object] = []

    def _get(*args, **kwargs):
        calls.append((args, kwargs))
        return _raise_not_found(*args, **kwargs)

    monkeypatch.setattr(coinbase_ohlcv._SESSION, "get", _get)

    assert coinbase_ohlcv.get_quote(MISSING_PRODUCT) is None
    assert calls

    calls_after_first_404 = len(calls)
    assert coinbase_ohlcv.get_quote(MISSING_PRODUCT) is None
    assert len(calls) == calls_after_first_404


def test_missing_product_cache_expires(monkeypatch):
    calls: list[object] = []

    def _get(*args, **kwargs):
        calls.append((args, kwargs))
        return _raise_not_found(*args, **kwargs)

    monkeypatch.setattr(coinbase_ohlcv._SESSION, "get", _get)

    assert coinbase_ohlcv.get_quote(MISSING_PRODUCT) is None
    _expire_missing_product(MISSING_PRODUCT)

    assert coinbase_ohlcv.get_quote(MISSING_PRODUCT) is None
    assert len(calls) == 3


def test_public_product_catalog_blocks_known_missing_product(monkeypatch):
    calls: list[str] = []

    def _get(url, **_kwargs):
        calls.append(str(url))
        if str(url).endswith("/products"):
            return _CatalogResponse([{"id": "BTC-USD", "quote_currency": "USD"}])
        raise AssertionError(f"unexpected product-specific request: {url}")

    monkeypatch.setattr(coinbase_ohlcv._SESSION, "get", _get)

    assert coinbase_ohlcv.get_quote(MISSING_PRODUCT) is None
    assert calls == [f"{coinbase_ohlcv._COINBASE_EXCHANGE_API_BASE_URL}/products"]


def test_public_product_support_wrapper_uses_catalog(monkeypatch):
    calls: list[str] = []

    def _get(url, **_kwargs):
        calls.append(str(url))
        if str(url).endswith("/products"):
            return _CatalogResponse([{"id": "BTC-USD", "quote_currency": "USD"}])
        raise AssertionError(f"unexpected product-specific request: {url}")

    monkeypatch.setattr(coinbase_ohlcv._SESSION, "get", _get)

    assert coinbase_ohlcv.public_product_support("BTC-USD") is True
    assert coinbase_ohlcv.public_product_support(MISSING_PRODUCT) is False
    assert calls == [f"{coinbase_ohlcv._COINBASE_EXCHANGE_API_BASE_URL}/products"]


def test_public_product_catalog_allows_known_product_quote(monkeypatch):
    calls: list[str] = []

    def _get(url, **_kwargs):
        calls.append(str(url))
        if str(url).endswith("/products"):
            return _CatalogResponse([{"id": "BTC-USD", "quote_currency": "USD"}])
        if str(url).endswith("/products/BTC-USD/ticker"):
            return _QuoteResponse()
        raise AssertionError(f"unexpected request: {url}")

    monkeypatch.setattr(coinbase_ohlcv._SESSION, "get", _get)

    quote = coinbase_ohlcv.get_quote("BTC-USD")
    assert quote is not None
    assert quote["last_price"] == 123.45
    assert calls == [
        f"{coinbase_ohlcv._COINBASE_EXCHANGE_API_BASE_URL}/products",
        f"{coinbase_ohlcv._COINBASE_EXCHANGE_API_BASE_URL}/products/BTC-USD/ticker",
    ]


def test_quote_429_opens_provider_backoff(monkeypatch, caplog):
    calls: list[str] = []
    now = [100.0]

    def _get(url, **_kwargs):
        calls.append(str(url))
        if len(calls) == 1:
            return _RateLimitResponse()
        return _QuoteResponse()

    monkeypatch.setenv("CHILI_COINBASE_OHLCV_PRODUCT_PREFILTER_ENABLED", "0")
    monkeypatch.setattr(coinbase_ohlcv.time, "time", lambda: now[0])
    monkeypatch.setattr(coinbase_ohlcv._SESSION, "get", _get)
    caplog.set_level(logging.WARNING, logger="app.services.trading.coinbase_ohlcv")

    assert coinbase_ohlcv.get_quote("BTC-USD") is None
    assert coinbase_ohlcv.get_quote("ETH-USD") is None
    assert calls == [
        f"{coinbase_ohlcv._COINBASE_EXCHANGE_API_BASE_URL}/products/BTC-USD/ticker",
    ]

    now[0] += 2.1

    quote = coinbase_ohlcv.get_quote("ETH-USD")
    assert quote is not None
    assert quote["last_price"] == 123.45
    assert calls[-1] == (
        f"{coinbase_ohlcv._COINBASE_EXCHANGE_API_BASE_URL}/products/ETH-USD/ticker"
    )
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("rate-limit backoff OPEN - 429 from Coinbase" in msg for msg in messages)
    assert not any("BTC-USD quote request failed" in msg for msg in messages)


def test_catalog_429_does_not_fall_through_to_product_request(monkeypatch):
    calls: list[str] = []

    def _get(url, **_kwargs):
        calls.append(str(url))
        if str(url).endswith("/products"):
            return _RateLimitResponse()
        raise AssertionError(f"unexpected product-specific request: {url}")

    monkeypatch.setattr(coinbase_ohlcv._SESSION, "get", _get)

    assert coinbase_ohlcv.get_quote("BTC-USD") is None
    assert calls == [f"{coinbase_ohlcv._COINBASE_EXCHANGE_API_BASE_URL}/products"]
