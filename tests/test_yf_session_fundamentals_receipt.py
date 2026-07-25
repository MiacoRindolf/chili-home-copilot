from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.services import yf_session
from app.services.yf_session import (
    FundamentalsProviderState,
    FundamentalsReceiptOrigin,
    FundamentalsReceiptStatus,
)


@pytest.fixture(autouse=True)
def _reset_fundamentals_state(monkeypatch):
    with yf_session._cache_lock:
        yf_session._cache.clear()
        yf_session._fundamentals_cache_metadata.clear()
    yf_session._reset_breaker_for_tests()
    monkeypatch.setattr(yf_session, "acquire", lambda: None)
    yield
    with yf_session._cache_lock:
        yf_session._cache.clear()
        yf_session._fundamentals_cache_metadata.clear()
    yf_session._reset_breaker_for_tests()


def test_fresh_network_and_cache_receipts_preserve_origin_and_age(monkeypatch) -> None:
    calls: list[str] = []

    def ticker(symbol: str, *, session):
        calls.append(symbol)
        return SimpleNamespace(
            info={
                "shortName": "Actuate Therapeutics Inc.",
                "marketCap": 123_000_000,
            }
        )

    monkeypatch.setattr(yf_session.yf, "Ticker", ticker)
    network = yf_session.get_fundamentals_receipt("ACTU")
    cached = yf_session.get_fundamentals_receipt("ACTU")

    assert calls == ["ACTU"]
    assert network.status is FundamentalsReceiptStatus.FRESH_DATA
    assert network.provider_state is FundamentalsProviderState.AVAILABLE
    assert network.origin is FundamentalsReceiptOrigin.NETWORK
    assert network.cache_age_seconds is None
    assert network.classification_usable is True
    assert network.data is not None
    assert network.data["short_name"] == "Actuate Therapeutics Inc."
    assert cached.status is FundamentalsReceiptStatus.FRESH_DATA
    assert cached.origin is FundamentalsReceiptOrigin.CACHE
    assert cached.cache_age_seconds is not None
    assert cached.cache_age_seconds <= cached.cache_ttl_seconds
    assert cached.classification_usable is True
    assert yf_session.get_fundamentals("ACTU")["short_name"] == (
        "Actuate Therapeutics Inc."
    )


def test_authoritative_empty_is_distinct_from_provider_error(monkeypatch) -> None:
    class NoRecordTicker:
        @property
        def info(self):
            raise RuntimeError("no data: delisted")

    monkeypatch.setattr(
        yf_session.yf,
        "Ticker",
        lambda _symbol, *, session: NoRecordTicker(),
    )
    authoritative = yf_session.get_fundamentals_receipt("EMPTY")

    with yf_session._cache_lock:
        yf_session._cache.clear()
        yf_session._fundamentals_cache_metadata.clear()

    class ErrorTicker:
        @property
        def info(self):
            raise TimeoutError("provider timeout")

    monkeypatch.setattr(
        yf_session.yf,
        "Ticker",
        lambda _symbol, *, session: ErrorTicker(),
    )
    provider_error = yf_session.get_fundamentals_receipt("ERROR")

    assert authoritative.status is FundamentalsReceiptStatus.AUTHORITATIVE_EMPTY
    assert authoritative.provider_state is FundamentalsProviderState.AVAILABLE
    assert authoritative.classification_usable is False
    assert provider_error.status is FundamentalsReceiptStatus.UNAVAILABLE
    assert provider_error.provider_state is FundamentalsProviderState.ERROR
    assert provider_error.classification_usable is False


def test_ambiguous_empty_and_open_circuit_have_distinct_receipts(monkeypatch) -> None:
    monkeypatch.setattr(
        yf_session.yf,
        "Ticker",
        lambda _symbol, *, session: SimpleNamespace(info={}),
    )
    ambiguous = yf_session.get_fundamentals_receipt("AMB")

    monkeypatch.setattr(yf_session, "_breaker_should_short_circuit", lambda: True)
    circuit = yf_session.get_fundamentals_receipt("CIRC")

    assert ambiguous.status is FundamentalsReceiptStatus.AMBIGUOUS_EMPTY
    assert ambiguous.provider_state is FundamentalsProviderState.UNAVAILABLE
    assert ambiguous.origin is FundamentalsReceiptOrigin.NETWORK
    assert circuit.status is FundamentalsReceiptStatus.UNAVAILABLE
    assert circuit.provider_state is FundamentalsProviderState.CIRCUIT_OPEN
    assert circuit.origin is FundamentalsReceiptOrigin.NONE
    assert circuit.classification_usable is False


def test_stale_cache_is_not_reclassified_as_fresh_when_circuit_is_open(
    monkeypatch,
) -> None:
    cache_key = "fund:STALE"
    old = time.time() - float(yf_session._TTL_FUNDAMENTALS) - 5.0
    with yf_session._cache_lock:
        yf_session._cache[cache_key] = (
            old,
            {"short_name": "Direxion Daily Example Bull 3X Shares"},
        )
    monkeypatch.setattr(yf_session, "_breaker_should_short_circuit", lambda: True)

    receipt = yf_session.get_fundamentals_receipt("STALE")

    assert receipt.status is FundamentalsReceiptStatus.STALE
    assert receipt.provider_state is FundamentalsProviderState.CIRCUIT_OPEN
    assert receipt.origin is FundamentalsReceiptOrigin.CACHE
    assert receipt.cache_age_seconds is not None
    assert receipt.cache_age_seconds > receipt.cache_ttl_seconds
    assert receipt.data is not None
    assert receipt.classification_usable is False
    assert yf_session.get_fundamentals("STALE") is None
