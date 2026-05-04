"""f-leak-3 (2026-05-04) — unit tests for the process-level circuit
breaker in ``app.services.yf_session``.

Six scenarios (no DB; pure in-memory):

    1. Default state is CLOSED.
    2. N-1 failures stay CLOSED.
    3. Nth failure trips OPEN; subsequent calls short-circuit.
    4. After TTL, transitions to HALF_OPEN; one probe is allowed through.
    5. HALF_OPEN success closes the breaker; counter resets.
    6. HALF_OPEN failure re-opens; one extra failure does not double-trip.

Run with ``pytest tests/test_yf_breaker.py -p no:asyncio``.
"""
from __future__ import annotations

import time

import pytest

from app.services import yf_session


@pytest.fixture(autouse=True)
def _reset_breaker():
    yf_session._reset_breaker_for_tests()
    yield
    yf_session._reset_breaker_for_tests()


def test_breaker_starts_closed():
    assert yf_session._breaker_state == "CLOSED"
    assert yf_session._breaker_should_short_circuit() is False


def test_breaker_below_threshold_stays_closed():
    threshold = yf_session._BREAKER_CONSECUTIVE_FAILURE_THRESHOLD
    for _ in range(threshold - 1):
        yf_session._breaker_on_failure()
    assert yf_session._breaker_state == "CLOSED"
    assert yf_session._breaker_should_short_circuit() is False


def test_breaker_trips_open_at_threshold():
    threshold = yf_session._BREAKER_CONSECUTIVE_FAILURE_THRESHOLD
    for _ in range(threshold):
        yf_session._breaker_on_failure()
    assert yf_session._breaker_state == "OPEN"
    assert yf_session._breaker_should_short_circuit() is True


def test_breaker_half_open_after_ttl(monkeypatch):
    threshold = yf_session._BREAKER_CONSECUTIVE_FAILURE_THRESHOLD
    for _ in range(threshold):
        yf_session._breaker_on_failure()
    assert yf_session._breaker_state == "OPEN"

    # Advance the monotonic clock past the TTL by setting opened_at
    # backward — equivalent to time.monotonic() advancing past TTL.
    yf_session._breaker_opened_at -= yf_session._BREAKER_HALF_OPEN_TTL_S + 1.0
    # The next short-circuit check should transition OPEN -> HALF_OPEN
    # and let one probe through.
    assert yf_session._breaker_should_short_circuit() is False
    assert yf_session._breaker_state == "HALF_OPEN"


def test_breaker_half_open_success_closes():
    threshold = yf_session._BREAKER_CONSECUTIVE_FAILURE_THRESHOLD
    for _ in range(threshold):
        yf_session._breaker_on_failure()
    yf_session._breaker_opened_at -= yf_session._BREAKER_HALF_OPEN_TTL_S + 1.0
    yf_session._breaker_should_short_circuit()  # transition to HALF_OPEN
    assert yf_session._breaker_state == "HALF_OPEN"

    yf_session._breaker_on_success()
    assert yf_session._breaker_state == "CLOSED"
    assert yf_session._breaker_consecutive_failures == 0


def test_breaker_half_open_failure_reopens():
    threshold = yf_session._BREAKER_CONSECUTIVE_FAILURE_THRESHOLD
    for _ in range(threshold):
        yf_session._breaker_on_failure()
    yf_session._breaker_opened_at -= yf_session._BREAKER_HALF_OPEN_TTL_S + 1.0
    yf_session._breaker_should_short_circuit()  # HALF_OPEN
    assert yf_session._breaker_state == "HALF_OPEN"

    yf_session._breaker_on_failure()
    assert yf_session._breaker_state == "OPEN"
