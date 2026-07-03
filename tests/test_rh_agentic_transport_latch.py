"""STEP-D #14: RH Agentic rail transport-outage latch + half-open + keep-warm telemetry.

Pure-logic tests over the module-level latch (mock time) — no live rail, no network.
The latch is PROCESS-WIDE module state, so each test resets it first.
"""
from __future__ import annotations

import app.services.trading.venue.robinhood_mcp as rh


class _Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def monotonic(self) -> float:
        return self.t


def _reset_latch() -> None:
    rh._clear_rail_transport_outage()
    rh._half_open_release()
    rh._RAIL_LAST_UNAVAILABLE_DETAIL = {}


class _TransportExc(Exception):
    """Stand-in for an RhMcpError transport outage."""

    def __init__(self, code: str = "refresh_transport"):
        super().__init__(code)
        self.code = code


def test_transport_outage_classification() -> None:
    assert rh._is_transport_outage_exc(_TransportExc("refresh_transport")) is True
    assert rh._is_transport_outage_exc(_TransportExc("refresh_http_503")) is True
    assert rh._is_transport_outage_exc(Exception("connection timed out")) is True
    # A rate-limit is NOT a transport outage (the governor owns 429 backoff).
    assert rh._is_transport_outage_exc(_TransportExc("http_429")) is False
    # NeedsReauth is NOT a transport outage (is_enabled reports disabled directly).
    assert rh._is_transport_outage_exc(rh.NeedsReauth("reauth")) is False


def test_record_latches_and_backs_off(monkeypatch) -> None:
    _reset_latch()
    clock = _Clock(1000.0)
    monkeypatch.setattr(rh, "_time", clock)

    assert rh.rail_transport_outage_active() is False
    rh._record_rail_transport_outage(_TransportExc("refresh_transport"))
    assert rh.rail_transport_outage_active() is True
    # Base backoff (5s) from a single outage.
    assert rh._rail_transport_outage_remaining() == 5.0

    # A second outage grows the backoff (streak 2 -> 10s) and extends the deadline.
    rh._record_rail_transport_outage(_TransportExc("refresh_http_503"))
    assert rh._rail_transport_outage_remaining() == 10.0

    detail = rh.venue_unavailable_detail()
    assert detail["kind"] == "transport_outage"
    assert detail["outage_streak"] == 2
    assert detail["latch_active"] is True
    _reset_latch()


def test_latch_expires_then_half_open_single_flight(monkeypatch) -> None:
    _reset_latch()
    clock = _Clock(1000.0)
    monkeypatch.setattr(rh, "_time", clock)

    rh._record_rail_transport_outage(_TransportExc("refresh_transport"))  # 5s latch
    # While hot, no half-open probe is allowed.
    assert rh._half_open_try_acquire() is False

    # Advance past the latch — exactly ONE caller wins the half-open probe.
    clock.t = 1006.0
    assert rh.rail_transport_outage_active() is False
    first = rh._half_open_try_acquire()
    second = rh._half_open_try_acquire()
    assert first is True and second is False  # single-flight
    # Releasing frees the slot for the next expiry.
    rh._half_open_release()
    assert rh._half_open_try_acquire() is True
    _reset_latch()


def test_clear_resets_streak_and_detail(monkeypatch) -> None:
    _reset_latch()
    clock = _Clock(1000.0)
    monkeypatch.setattr(rh, "_time", clock)

    rh._record_rail_transport_outage(_TransportExc("refresh_transport"))
    rh._record_rail_transport_outage(_TransportExc("refresh_transport"))
    assert rh._RAIL_OUTAGE_STREAK == 2
    rh._clear_rail_transport_outage()
    assert rh._RAIL_OUTAGE_STREAK == 0
    assert rh.rail_transport_outage_active() is False
    assert rh.venue_unavailable_detail() == {}
    _reset_latch()


def test_backoff_is_bounded_by_max(monkeypatch) -> None:
    _reset_latch()
    clock = _Clock(1000.0)
    monkeypatch.setattr(rh, "_time", clock)
    # Many consecutive outages must clamp the backoff at the documented max (60s).
    for _ in range(12):
        rh._record_rail_transport_outage(_TransportExc("refresh_transport"))
    assert rh._rail_transport_outage_remaining() == rh._RAIL_OUTAGE_MAX_SEC
    _reset_latch()


def test_keepwarm_flag_off_is_noop(monkeypatch) -> None:
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "chili_robinhood_agentic_probe_keepwarm_enabled", False, raising=False)
    res = rh.probe_keepwarm()
    assert res == {"ok": True, "skipped": "flag_off"}


def test_venue_unavailable_detail_payload_shape(monkeypatch) -> None:
    """The telemetry payload carries the failure detail callers attach to events."""
    _reset_latch()
    clock = _Clock(1000.0)
    monkeypatch.setattr(rh, "_time", clock)

    rh._record_rail_transport_outage(_TransportExc("refresh_http_502"))
    detail = rh.venue_unavailable_detail()
    assert detail["reason"] == "refresh_http_502"
    assert "latch_remaining_sec" in detail
    assert detail["latch_active"] is True
    _reset_latch()
