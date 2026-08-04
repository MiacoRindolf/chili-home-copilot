from __future__ import annotations

import time
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import yf_session
from app.services.yf_session import (
    FundamentalsProviderState,
    FundamentalsRefreshState,
    FundamentalsReceiptOrigin,
    FundamentalsReceiptStatus,
)

UTC = timezone.utc


def _wait_for_refresh_idle(timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with yf_session._fundamentals_refresh_lock:
            if (
                not yf_session._fundamentals_refresh_inflight
                and not yf_session._fundamentals_refresh_pending
                and all(
                    not worker.is_alive()
                    for worker in yf_session._fundamentals_refresh_threads.values()
                )
            ):
                return
        time.sleep(0.01)
    raise AssertionError("fundamentals refresh worker did not become idle")


@pytest.fixture(autouse=True)
def _reset_fundamentals_state(monkeypatch):
    _wait_for_refresh_idle()
    yf_session._reset_fundamentals_refresh_for_tests()
    with yf_session._cache_lock:
        yf_session._cache.clear()
        yf_session._fundamentals_cache_metadata.clear()
    yf_session._reset_breaker_for_tests()
    monkeypatch.setattr(yf_session, "acquire", lambda: None)
    yield
    _wait_for_refresh_idle()
    yf_session._reset_fundamentals_refresh_for_tests()
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


def test_cache_only_miss_returns_without_waiting_and_singleflights_refresh(
    monkeypatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    caller_thread = threading.get_ident()
    provider_threads: list[int] = []
    calls: list[str] = []

    class BlockingTicker:
        @property
        def info(self):
            provider_threads.append(threading.get_ident())
            entered.set()
            assert release.wait(timeout=5.0)
            return {"shortName": "Actuate Therapeutics Inc."}

    def ticker(symbol: str, *, session):
        calls.append(symbol)
        return BlockingTicker()

    monkeypatch.setattr(yf_session.yf, "Ticker", ticker)
    started = time.monotonic()
    first = yf_session.get_cached_fundamentals_receipt("ACTU")
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert first.status is FundamentalsReceiptStatus.UNAVAILABLE
    assert first.reason == "fundamentals_cache_miss"
    assert first.refresh_state is FundamentalsRefreshState.SCHEDULED
    assert entered.wait(timeout=2.0)
    second = yf_session.get_cached_fundamentals_receipt("ACTU")
    assert second.refresh_state is FundamentalsRefreshState.IN_FLIGHT
    assert calls == ["ACTU"]
    assert provider_threads and provider_threads[0] != caller_thread

    release.set()
    _wait_for_refresh_idle()
    cached = yf_session.get_cached_fundamentals_receipt("ACTU")
    assert cached.status is FundamentalsReceiptStatus.FRESH_DATA
    assert cached.origin is FundamentalsReceiptOrigin.CACHE
    assert cached.refresh_state is FundamentalsRefreshState.CACHE_CURRENT
    assert cached.data is not None
    assert cached.data["short_name"] == "Actuate Therapeutics Inc."
    assert calls == ["ACTU"]


def test_proactive_refresh_failure_preserves_verified_value_and_backs_off(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def success_ticker(symbol: str, *, session):
        calls.append(symbol)
        return SimpleNamespace(
            info={"shortName": "Actuate Therapeutics Inc."}
        )

    monkeypatch.setattr(yf_session.yf, "Ticker", success_ticker)
    seeded = yf_session.get_fundamentals_receipt("ACTU")
    assert seeded.status is FundamentalsReceiptStatus.FRESH_DATA

    cache_key = "fund:ACTU"
    old_epoch = time.time() - (float(yf_session._TTL_FUNDAMENTALS) * 0.6)
    with yf_session._cache_lock:
        _current_epoch, cached_value = yf_session._cache[cache_key]
        metadata = yf_session._fundamentals_cache_metadata[cache_key]
        yf_session._cache[cache_key] = (old_epoch, cached_value)
        yf_session._fundamentals_cache_metadata[cache_key] = replace(
            metadata,
            cache_timestamp=old_epoch,
            cache_monotonic=(
                time.monotonic()
                - (float(yf_session._TTL_FUNDAMENTALS) * 0.6)
            ),
            fetched_at=datetime.now(UTC)
            - timedelta(seconds=float(yf_session._TTL_FUNDAMENTALS) * 0.6),
        )

    class ErrorTicker:
        @property
        def info(self):
            raise TimeoutError("provider timeout")

    def error_ticker(symbol: str, *, session):
        calls.append(symbol)
        return ErrorTicker()

    monkeypatch.setattr(yf_session.yf, "Ticker", error_ticker)
    scheduled = yf_session.get_cached_fundamentals_receipt("ACTU")
    assert scheduled.status is FundamentalsReceiptStatus.FRESH_DATA
    assert scheduled.refresh_state is FundamentalsRefreshState.SCHEDULED
    _wait_for_refresh_idle()

    preserved = yf_session.get_cached_fundamentals_receipt("ACTU")
    assert preserved.status is FundamentalsReceiptStatus.FRESH_DATA
    assert preserved.data is not None
    assert preserved.data["short_name"] == "Actuate Therapeutics Inc."
    assert preserved.refresh_state is FundamentalsRefreshState.BACKOFF
    assert preserved.refresh_reason == "fundamentals_provider_error"
    assert preserved.next_refresh_at is not None
    assert preserved.last_refresh_attempt is not None
    assert (
        preserved.last_refresh_attempt.provider_state
        is FundamentalsProviderState.ERROR
    )
    assert (
        preserved.last_refresh_attempt.status
        is FundamentalsReceiptStatus.UNAVAILABLE
    )
    assert (
        preserved.last_refresh_attempt.reason
        == "fundamentals_provider_error"
    )
    assert calls == ["ACTU", "ACTU"]


def test_failing_background_refresh_cannot_clobber_concurrent_verified_write(
    monkeypatch,
) -> None:
    background_entered = threading.Event()
    release_background = threading.Event()
    call_lock = threading.Lock()
    call_count = 0

    class BackgroundFailureTicker:
        @property
        def info(self):
            background_entered.set()
            assert release_background.wait(timeout=5.0)
            raise TimeoutError("provider timeout")

    class ForegroundSuccessTicker:
        @property
        def info(self):
            return {"shortName": "Concurrent Verified Incorporated"}

    def ticker(_symbol: str, *, session):
        nonlocal call_count
        with call_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            return BackgroundFailureTicker()
        return ForegroundSuccessTicker()

    monkeypatch.setattr(yf_session.yf, "Ticker", ticker)
    scheduled = yf_session.get_cached_fundamentals_receipt("RACE")
    assert scheduled.refresh_state is FundamentalsRefreshState.SCHEDULED
    assert background_entered.wait(timeout=2.0)

    foreground = yf_session.get_fundamentals_receipt("RACE")
    assert foreground.status is FundamentalsReceiptStatus.FRESH_DATA
    release_background.set()
    _wait_for_refresh_idle()

    retained = yf_session.get_cached_fundamentals_receipt("RACE")
    assert retained.status is FundamentalsReceiptStatus.FRESH_DATA
    assert retained.data is not None
    assert retained.data["short_name"] == "Concurrent Verified Incorporated"
    assert retained.last_refresh_attempt is not None
    assert (
        retained.last_refresh_attempt.provider_state
        is FundamentalsProviderState.ERROR
    )
    assert call_count == 2


@pytest.mark.parametrize(
    ("error", "expected_state", "expected_reason"),
    (
        (
            RuntimeError("429 Too Many Requests"),
            FundamentalsProviderState.RATE_LIMITED,
            "fundamentals_provider_rate_limited",
        ),
        (
            RuntimeError("403 Forbidden"),
            FundamentalsProviderState.AUTH_ERROR,
            "fundamentals_provider_auth_error",
        ),
    ),
)
def test_provider_rate_limit_and_auth_failures_are_typed(
    monkeypatch,
    error: Exception,
    expected_state: FundamentalsProviderState,
    expected_reason: str,
) -> None:
    class ErrorTicker:
        @property
        def info(self):
            raise error

    monkeypatch.setattr(
        yf_session.yf,
        "Ticker",
        lambda _symbol, *, session: ErrorTicker(),
    )
    receipt = yf_session.get_fundamentals_receipt("ACTU")
    assert receipt.status is FundamentalsReceiptStatus.UNAVAILABLE
    assert receipt.provider_state is expected_state
    assert receipt.reason == expected_reason
    assert receipt.provider_latency_seconds is not None


def test_bounded_refresh_queue_drains_cold_symbols_without_dropping(
    monkeypatch,
) -> None:
    entered: set[str] = set()
    entered_lock = threading.Lock()
    first_entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class BlockingTicker:
        def __init__(self, symbol: str):
            self.symbol = symbol

        @property
        def info(self):
            with entered_lock:
                entered.add(self.symbol)
                first_entered.set()
            assert release.wait(timeout=5.0)
            return {"shortName": f"{self.symbol} Incorporated"}

    def ticker(symbol: str, *, session):
        calls.append(symbol)
        return BlockingTicker(symbol)

    monkeypatch.setattr(yf_session.yf, "Ticker", ticker)
    first = yf_session.get_cached_fundamentals_receipt("AAA")
    second = yf_session.get_cached_fundamentals_receipt("BBB")
    assert first.refresh_state is FundamentalsRefreshState.SCHEDULED
    assert second.refresh_state is FundamentalsRefreshState.QUEUED
    assert first_entered.wait(timeout=2.0)

    third = yf_session.get_cached_fundamentals_receipt("CCC")
    assert third.refresh_state is FundamentalsRefreshState.QUEUED
    assert third.refresh_reason == "fundamentals_refresh_queued"

    release.set()
    _wait_for_refresh_idle()
    assert calls == ["AAA", "BBB", "CCC"]
    assert (
        yf_session.get_cached_fundamentals_receipt("CCC").status
        is FundamentalsReceiptStatus.FRESH_DATA
    )


def test_refresh_owner_close_joins_worker_and_blocks_new_intake(
    monkeypatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingTicker:
        @property
        def info(self):
            entered.set()
            assert release.wait(timeout=5.0)
            return {"shortName": "Actuate Therapeutics Inc."}

    monkeypatch.setattr(
        yf_session.yf,
        "Ticker",
        lambda _symbol, *, session: BlockingTicker(),
    )
    scheduled = yf_session.get_cached_fundamentals_receipt("ACTU")
    assert scheduled.refresh_state is FundamentalsRefreshState.SCHEDULED
    assert entered.wait(timeout=2.0)

    close_errors: list[BaseException] = []

    def close_owner() -> None:
        try:
            yf_session.close_fundamentals_refresh(timeout_seconds=2.0)
        except BaseException as exc:
            close_errors.append(exc)

    closer = threading.Thread(target=close_owner)
    closer.start()
    time.sleep(0.05)
    assert closer.is_alive()
    release.set()
    closer.join(timeout=2.0)
    assert not closer.is_alive()
    assert close_errors == []

    closed = yf_session.get_cached_fundamentals_receipt("MISS")
    assert closed.refresh_state is FundamentalsRefreshState.BACKOFF
    assert closed.refresh_reason == "fundamentals_refresh_manager_closed"
    yf_session.open_fundamentals_refresh()


def test_refresh_owner_close_waits_through_worker_queue_drain(
    monkeypatch,
) -> None:
    provider_entered = threading.Event()
    release_provider = threading.Event()
    drain_entered = threading.Event()
    release_drain = threading.Event()

    class BlockingTicker:
        @property
        def info(self):
            provider_entered.set()
            assert release_provider.wait(timeout=5.0)
            return {"shortName": "Actuate Therapeutics Inc."}

    real_drain = yf_session._drain_fundamentals_refresh_queue

    def blocking_drain() -> None:
        drain_entered.set()
        assert release_drain.wait(timeout=5.0)
        real_drain()

    monkeypatch.setattr(
        yf_session.yf,
        "Ticker",
        lambda _symbol, *, session: BlockingTicker(),
    )
    monkeypatch.setattr(
        yf_session,
        "_drain_fundamentals_refresh_queue",
        blocking_drain,
    )
    scheduled = yf_session.get_cached_fundamentals_receipt("ACTU")
    assert scheduled.refresh_state is FundamentalsRefreshState.SCHEDULED
    assert provider_entered.wait(timeout=2.0)
    release_provider.set()
    assert drain_entered.wait(timeout=2.0)

    close_errors: list[BaseException] = []

    def close_owner() -> None:
        try:
            yf_session.close_fundamentals_refresh(timeout_seconds=2.0)
        except BaseException as exc:
            close_errors.append(exc)

    closer = threading.Thread(target=close_owner)
    closer.start()
    time.sleep(0.05)
    assert closer.is_alive()
    release_drain.set()
    closer.join(timeout=2.0)
    assert not closer.is_alive()
    assert close_errors == []
    yf_session.open_fundamentals_refresh()


def test_circuit_short_circuit_records_no_current_provider_request(
    monkeypatch,
) -> None:
    cache_key = "fund:STALE"
    old_epoch = time.time() - float(yf_session._TTL_FUNDAMENTALS) - 5.0
    with yf_session._cache_lock:
        yf_session._cache[cache_key] = (
            old_epoch,
            {"short_name": "Stale Example Incorporated"},
        )
        yf_session._fundamentals_cache_metadata[cache_key] = (
            yf_session._FundamentalsCacheMetadata(
                cache_timestamp=old_epoch,
                cache_monotonic=(
                    time.monotonic()
                    - float(yf_session._TTL_FUNDAMENTALS)
                    - 5.0
                ),
                status=FundamentalsReceiptStatus.FRESH_DATA,
                provider_state=FundamentalsProviderState.AVAILABLE,
                cache_ttl_seconds=float(yf_session._TTL_FUNDAMENTALS),
                reason=None,
                fetched_at=datetime.now(UTC)
                - timedelta(
                    seconds=float(yf_session._TTL_FUNDAMENTALS) + 5.0
                ),
                provider_latency_seconds=9.0,
                provider_limiter_wait_seconds=4.0,
            )
        )
    monkeypatch.setattr(yf_session, "_breaker_should_short_circuit", lambda: True)

    scheduled = yf_session.get_cached_fundamentals_receipt("STALE")
    assert scheduled.refresh_state is FundamentalsRefreshState.SCHEDULED
    _wait_for_refresh_idle()
    observed = yf_session.get_cached_fundamentals_receipt("STALE")

    assert observed.provider_latency_seconds == 9.0
    assert observed.provider_limiter_wait_seconds == 4.0
    assert observed.last_refresh_attempt is not None
    assert observed.last_refresh_attempt.provider_request_performed is False
    assert observed.last_refresh_attempt.provider_latency_seconds is None
    assert observed.last_refresh_attempt.limiter_wait_seconds is None
    assert (
        observed.last_refresh_attempt.provider_state
        is FundamentalsProviderState.CIRCUIT_OPEN
    )


def test_breaker_half_open_allows_exactly_one_probe() -> None:
    with yf_session._breaker_lock:
        yf_session._breaker_state = "OPEN"
        yf_session._breaker_opened_at = (
            time.monotonic()
            - float(yf_session._BREAKER_HALF_OPEN_TTL_S)
            - 1.0
        )
        yf_session._breaker_half_open_probe_inflight = False

    assert yf_session._breaker_should_short_circuit() is False
    assert yf_session._breaker_should_short_circuit() is True
    yf_session._breaker_on_failure()
    with yf_session._breaker_lock:
        assert yf_session._breaker_state == "OPEN"
        assert yf_session._breaker_half_open_probe_inflight is False


def test_half_open_probe_success_drains_queued_symbol_without_backoff(
    monkeypatch,
) -> None:
    probe_entered = threading.Event()
    release_probe = threading.Event()
    calls: list[str] = []

    class Ticker:
        def __init__(self, symbol: str):
            self.symbol = symbol

        @property
        def info(self):
            if self.symbol == "AAA":
                probe_entered.set()
                assert release_probe.wait(timeout=5.0)
            return {"shortName": f"{self.symbol} Incorporated"}

    def ticker(symbol: str, *, session):
        calls.append(symbol)
        return Ticker(symbol)

    monkeypatch.setattr(yf_session.yf, "Ticker", ticker)
    with yf_session._breaker_lock:
        yf_session._breaker_state = "OPEN"
        yf_session._breaker_opened_at = (
            time.monotonic()
            - float(yf_session._BREAKER_HALF_OPEN_TTL_S)
            - 1.0
        )
        yf_session._breaker_half_open_probe_inflight = False

    probe = yf_session.get_cached_fundamentals_receipt("AAA")
    assert probe.refresh_state is FundamentalsRefreshState.SCHEDULED
    assert probe_entered.wait(timeout=2.0)
    queued = yf_session.get_cached_fundamentals_receipt("BBB")
    assert queued.refresh_state is FundamentalsRefreshState.QUEUED
    release_probe.set()
    _wait_for_refresh_idle()

    assert calls == ["AAA", "BBB"]
    refreshed = yf_session.get_cached_fundamentals_receipt("BBB")
    assert refreshed.status is FundamentalsReceiptStatus.FRESH_DATA
    assert refreshed.refresh_state is FundamentalsRefreshState.CACHE_CURRENT
    assert refreshed.last_refresh_attempt is not None
    assert (
        refreshed.last_refresh_attempt.provider_state
        is FundamentalsProviderState.AVAILABLE
    )
    with yf_session._breaker_lock:
        assert yf_session._breaker_state == "CLOSED"
