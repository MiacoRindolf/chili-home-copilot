"""Shelf-registration damper (2026-08-18 Ross recaps).

PFSA: 179M registered vs 605K displayed float; sinisi ni Ross ang muted na
HOD-break squeeze sa posibleng shelf tapping. Aktibong registration =
expectation/size damper, HINDI veto; fail-open sa lahat ng kawalan ng data.
Walang network sa tests — mocked ang _http_get_json.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import app.services.trading.momentum_neural.shelf_registration as sr

# prime_shelf_cache short-circuits under CHILI_PYTEST=1 (network fence #1101,
# 2026-08-21) — these tests mock the HTTP layer, so lift the fence for the
# prime call only (the two active/old-filing cases were red since #1101).
_LIFT_FENCE = {"CHILI_PYTEST": "0", "CHILI_DIAGNOSTIC_REPLAY_ISOLATED": "false"}


def _fresh(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


_TICKER_MAP = {"0": {"cik_str": 1234567, "ticker": "PFSA", "title": "Pfsa Inc"}}


def _subs(forms_dates):
    return {
        "filings": {
            "recent": {
                "form": [f for f, _ in forms_dates],
                "filingDate": [d for _, d in forms_dates],
            }
        }
    }


def _prime(subs_payload):
    sr.reset_shelf_caches_for_tests()

    def fake_get(url):
        if "company_tickers" in url:
            return _TICKER_MAP
        return subs_payload

    with patch.object(sr, "_http_get_json", side_effect=fake_get), patch.dict(os.environ, _LIFT_FENCE):
        sr.prime_shelf_cache("PFSA")
    return sr.cached_shelf_state("PFSA")


def test_active_shelf_detected_and_dampened():
    state = _prime(_subs([("S-1", _fresh(30)), ("424B4", _fresh(10)), ("8-K", _fresh(5))]))
    assert state is not None and state["shelf_active"] is True
    assert state["shelf_filing_count"] == 2  # ang 8-K ay hindi shelf form
    mult, dbg = sr.shelf_damper_multiplier(state, fraction=0.75)
    assert mult == 0.75
    assert dbg["shelf_filing_count"] == 2


def test_old_filings_outside_lookback_do_not_damp():
    state = _prime(_subs([("S-3", _fresh(4000))]))
    assert state is not None and state["shelf_active"] is False
    mult, dbg = sr.shelf_damper_multiplier(state, fraction=0.75)
    assert (mult, dbg) == (1.0, None)


def test_network_failure_is_fail_open_and_negative_cached():
    sr.reset_shelf_caches_for_tests()
    calls = {"n": 0}

    def boom(url):
        calls["n"] += 1
        raise RuntimeError("edgar down")

    with patch.object(sr, "_http_get_json", side_effect=boom), patch.dict(os.environ, _LIFT_FENCE):
        sr.prime_shelf_cache("PFSA")
        first_calls = calls["n"]
        assert first_calls >= 1  # the fence is lifted: the network WAS attempted once
        sr.prime_shelf_cache("PFSA")  # negative-cached — walang bagong network
    assert calls["n"] == first_calls
    assert sr.cached_shelf_state("PFSA") is None
    mult, dbg = sr.shelf_damper_multiplier(None, fraction=0.75)
    assert (mult, dbg) == (1.0, None)


def test_cached_read_never_networks():
    sr.reset_shelf_caches_for_tests()
    with patch.object(sr, "_http_get_json", side_effect=AssertionError("sizing must not network")):
        assert sr.cached_shelf_state("PFSA") is None


def test_multiplier_bounds_disable():
    state = {"shelf_active": True, "shelf_filing_count": 1, "newest_filing_date": "2026-08-01"}
    assert sr.shelf_damper_multiplier(state, fraction=1.0) == (1.0, None)
    assert sr.shelf_damper_multiplier(state, fraction=0.0) == (1.0, None)
    assert sr.shelf_damper_multiplier(state, fraction=float("nan"))[0] == 1.0 or True
    # NaN: hindi 0<f<1 kaya 1.0
    mult, _ = sr.shelf_damper_multiplier(state, fraction=float("nan"))
    assert mult == 1.0


def test_crypto_symbols_never_primed():
    sr.reset_shelf_caches_for_tests()
    with patch.object(sr, "_http_get_json", side_effect=AssertionError("crypto must not hit EDGAR")):
        sr.prime_shelf_cache("BTC-USD")


def test_sizing_source_wires_damper_post_floor():
    """Control-flow pin: ang damper ay post-floor (supply physics, binds on
    paper), pagkatapos ng day-open ramp block, cache-only read."""
    import inspect

    import app.services.trading.momentum_neural.live_runner as lr

    source = inspect.getsource(lr.tick_live_session)
    i_floor = source.index('"paper_full_size_floor"')
    i_ramp = source.index('"day_open_risk_ramp_post_floor"')
    i_shelf = source.index('"shelf_registration_damper"')
    assert i_floor < i_ramp < i_shelf
    shelf_block = source[i_ramp:i_shelf]
    assert "cached_shelf_state" in shelf_block
    assert "prime_shelf_cache" not in shelf_block  # sizing must not network
