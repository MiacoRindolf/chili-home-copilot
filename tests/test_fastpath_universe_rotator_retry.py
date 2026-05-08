"""Tests for f-fastpath-rotator-http-retry (2026-05-08).

Pins the retry-with-backoff policy in
``universe_rotator._http_get_json``:

  * ConnectionError / Timeout / 503 / 429 -> retry up to 3 times.
  * 4xx (other than 429) / JSON decode -> fail-fast (don't retry).
  * Returns None only after all retries exhaust.

Helper-level. We patch ``time.sleep`` to skip the backoff so the
suite stays sub-second; the real backoff timing is validated
implicitly by the worst-case bound (``sum(_HTTP_RETRY_BACKOFFS_S) +
3*timeout``) -- not asserted here because that's runtime physics, not
testable logic.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_response(*, status: int = 200, json_payload=None,
                   raise_json: bool = False):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    if raise_json:
        resp.json.side_effect = ValueError("not JSON")
    else:
        resp.json.return_value = json_payload if json_payload is not None else {}
    return resp


# ---------------------------------------------------------------------------
# Connection error retries
# ---------------------------------------------------------------------------

def test_retry_succeeds_after_two_connection_errors():
    """ConnectionError x2 then 200 -> returns the third response's JSON."""
    from app.services.trading.fast_path import universe_rotator as ur

    expected = {"ok": True, "n": 7}
    side_effects = [
        requests.exceptions.ConnectionError("Errno 101"),
        requests.exceptions.ConnectionError("Errno 101"),
        _fake_response(status=200, json_payload=expected),
    ]
    with patch.object(ur.requests, "get", side_effect=side_effects), \
         patch.object(ur.time, "sleep") as sleep_mock:
        result = ur._http_get_json("https://example/x")
    assert result == expected
    # Backoff was applied for the two retries (0.5 + 1.0).
    assert sleep_mock.call_count >= 2


def test_retry_exhausts_returns_none_on_all_connection_error():
    """All 3 attempts fail with ConnectionError -> None."""
    from app.services.trading.fast_path import universe_rotator as ur

    with patch.object(
        ur.requests, "get",
        side_effect=requests.exceptions.ConnectionError("Errno 101"),
    ), patch.object(ur.time, "sleep"):
        result = ur._http_get_json("https://example/x")
    assert result is None


# ---------------------------------------------------------------------------
# Timeout retries
# ---------------------------------------------------------------------------

def test_retry_succeeds_after_one_timeout():
    """Timeout once then 200 -> returns the response's JSON."""
    from app.services.trading.fast_path import universe_rotator as ur

    side_effects = [
        requests.exceptions.Timeout("read timed out"),
        _fake_response(status=200, json_payload={"after": "timeout"}),
    ]
    with patch.object(ur.requests, "get", side_effect=side_effects), \
         patch.object(ur.time, "sleep"):
        result = ur._http_get_json("https://example/x")
    assert result == {"after": "timeout"}


# ---------------------------------------------------------------------------
# Retryable HTTP status codes (429, 503)
# ---------------------------------------------------------------------------

def test_retry_on_503_then_succeeds():
    from app.services.trading.fast_path import universe_rotator as ur

    side_effects = [
        _fake_response(status=503),
        _fake_response(status=200, json_payload={"recovered": True}),
    ]
    with patch.object(ur.requests, "get", side_effect=side_effects), \
         patch.object(ur.time, "sleep"):
        result = ur._http_get_json("https://example/x")
    assert result == {"recovered": True}


def test_retry_on_429_then_succeeds():
    from app.services.trading.fast_path import universe_rotator as ur

    side_effects = [
        _fake_response(status=429),
        _fake_response(status=429),
        _fake_response(status=200, json_payload={"backed_off": True}),
    ]
    with patch.object(ur.requests, "get", side_effect=side_effects), \
         patch.object(ur.time, "sleep"):
        result = ur._http_get_json("https://example/x")
    assert result == {"backed_off": True}


# ---------------------------------------------------------------------------
# Non-retryable: 4xx (except 429) -> fail-fast
# ---------------------------------------------------------------------------

def test_404_returns_none_without_retry():
    """404 is non-retryable -> single GET call, no retries."""
    from app.services.trading.fast_path import universe_rotator as ur

    get_mock = MagicMock(return_value=_fake_response(status=404))
    with patch.object(ur.requests, "get", get_mock), \
         patch.object(ur.time, "sleep"):
        result = ur._http_get_json("https://example/x")
    assert result is None
    assert get_mock.call_count == 1  # no retry


def test_403_returns_none_without_retry():
    """403 is non-retryable -> single GET call."""
    from app.services.trading.fast_path import universe_rotator as ur

    get_mock = MagicMock(return_value=_fake_response(status=403))
    with patch.object(ur.requests, "get", get_mock), \
         patch.object(ur.time, "sleep"):
        result = ur._http_get_json("https://example/x")
    assert result is None
    assert get_mock.call_count == 1


def test_400_returns_none_without_retry():
    """400 is non-retryable -> single GET call."""
    from app.services.trading.fast_path import universe_rotator as ur

    get_mock = MagicMock(return_value=_fake_response(status=400))
    with patch.object(ur.requests, "get", get_mock), \
         patch.object(ur.time, "sleep"):
        result = ur._http_get_json("https://example/x")
    assert result is None
    assert get_mock.call_count == 1


# ---------------------------------------------------------------------------
# Non-retryable: JSON decode error -> fail-fast
# ---------------------------------------------------------------------------

def test_json_decode_failure_no_retry():
    """200 with non-JSON body -> None, single GET call."""
    from app.services.trading.fast_path import universe_rotator as ur

    get_mock = MagicMock(return_value=_fake_response(
        status=200, raise_json=True,
    ))
    with patch.object(ur.requests, "get", get_mock), \
         patch.object(ur.time, "sleep"):
        result = ur._http_get_json("https://example/x")
    assert result is None
    assert get_mock.call_count == 1


# ---------------------------------------------------------------------------
# Backoff sleep policy: 0.5 / 1.0 / 2.0
# ---------------------------------------------------------------------------

def test_backoff_sequence_is_05_10_20():
    """Verify the documented backoff sequence is what's actually slept."""
    from app.services.trading.fast_path import universe_rotator as ur

    with patch.object(
        ur.requests, "get",
        side_effect=requests.exceptions.ConnectionError("e"),
    ), patch.object(ur.time, "sleep") as sleep_mock:
        ur._http_get_json("https://example/x")
    # First attempt has no backoff; the three retries sleep 0.5/1.0/2.0.
    sleep_args = [c.args[0] for c in sleep_mock.call_args_list]
    assert sleep_args == [0.5, 1.0, 2.0]


# ---------------------------------------------------------------------------
# Successful first attempt -> no retry overhead
# ---------------------------------------------------------------------------

def test_success_on_first_attempt_no_retry_calls():
    from app.services.trading.fast_path import universe_rotator as ur

    get_mock = MagicMock(return_value=_fake_response(
        status=200, json_payload={"first_try": True},
    ))
    with patch.object(ur.requests, "get", get_mock), \
         patch.object(ur.time, "sleep"):
        result = ur._http_get_json("https://example/x")
    assert result == {"first_try": True}
    assert get_mock.call_count == 1
