"""Tests for the GET /api/trading/fast-path/maker-stats endpoint
(f-fastpath-maker-only-executor, 2026-05-08).

Helper-level. The endpoint's only DB hit is one aggregation query
against `fast_path_maker_attempts`; we patch the engine context
manager so a stub `execute()` returns canned aggregated rows.

Pinned behavior:
  * Settings keys present (execution_mode, fee, both timeouts).
  * Per-pair shape matches the brief.
  * fill_rate < 0.25 -> advisory: 'uneconomic for maker-only'.
  * fill_rate >= 0.25 -> advisory is null.
  * Empty result set -> ok=true with empty per_pair, totals zeroed.
  * DB exception -> ok=false with the error surfaced.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


def _stub_engine_with_rows(rows):
    """Build a fake `engine` whose `.begin()` is a context manager and
    whose `conn.execute(...).mappings().all()` returns *rows*.

    We monkey-patch the *module-level* `engine` import in
    `fast_path_api`, so the FastAPI dispatch goes through our fake.
    """
    class _Result:
        def __init__(self, data):
            self._data = data
        def mappings(self):
            return self
        def all(self):
            return self._data

    class _Conn:
        def execute(self, *a, **kw):
            return _Result(rows)

    @contextmanager
    def _begin():
        yield _Conn()

    eng = MagicMock(name="engine")
    eng.begin = _begin
    return eng


def _stub_settings(execution_mode="taker"):
    from app.services.trading.fast_path.settings import FastPathSettings
    return FastPathSettings(
        enabled=True, execution_mode=execution_mode,
        cost_aware_maker_fee_bps=40.0,
        maker_cancel_on_timeout_s=10,
        maker_first_taker_fallback_s=5,
    )


def _call_endpoint(rows, *, execution_mode="taker"):
    """Invoke the endpoint function directly (bypassing FastAPI
    dispatch) and return the parsed JSON payload."""
    import json
    from app.routers.trading_sub import fast_path_api as mod

    with patch.object(mod, "engine", _stub_engine_with_rows(rows)), \
         patch(
            "app.services.trading.fast_path.settings.load",
            return_value=_stub_settings(execution_mode=execution_mode),
         ):
        resp = mod.get_maker_stats()
    return json.loads(bytes(resp.body).decode("utf-8"))


# ---------------------------------------------------------------------------
# Settings keys present
# ---------------------------------------------------------------------------

def test_response_includes_all_maker_settings():
    payload = _call_endpoint([])
    assert payload["ok"] is True
    s = payload["settings"]
    for key in (
        "execution_mode",
        "cost_aware_maker_fee_bps",
        "maker_cancel_on_timeout_s",
        "maker_first_taker_fallback_s",
    ):
        assert key in s


def test_settings_carry_through_overrides():
    payload = _call_endpoint([], execution_mode="maker_only")
    assert payload["settings"]["execution_mode"] == "maker_only"
    assert payload["settings"]["cost_aware_maker_fee_bps"] == 40.0
    assert payload["settings"]["maker_cancel_on_timeout_s"] == 10
    assert payload["settings"]["maker_first_taker_fallback_s"] == 5


# ---------------------------------------------------------------------------
# Per-pair shape
# ---------------------------------------------------------------------------

def test_per_pair_shape_matches_brief():
    payload = _call_endpoint([
        {"ticker": "BTC-USD", "attempts": 100, "fills": 30,
         "cancels": 60, "replaced": 8, "rejected": 2},
    ])
    assert payload["ok"] is True
    assert len(payload["per_pair"]) == 1
    row = payload["per_pair"][0]
    for key in ("ticker", "attempts", "fills", "cancels", "replaced",
                "rejected", "fill_rate", "advisory"):
        assert key in row
    assert row["ticker"] == "BTC-USD"
    assert row["fill_rate"] == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# Advisory: fill_rate < 0.25
# ---------------------------------------------------------------------------

def test_advisory_set_when_fill_rate_below_threshold():
    payload = _call_endpoint([
        {"ticker": "DOGE-USD", "attempts": 100, "fills": 20,
         "cancels": 75, "replaced": 5, "rejected": 0},
    ])
    row = payload["per_pair"][0]
    assert row["fill_rate"] == pytest.approx(0.20)
    assert row["advisory"] == "uneconomic for maker-only"


def test_advisory_unset_at_or_above_threshold():
    payload = _call_endpoint([
        {"ticker": "BTC-USD", "attempts": 100, "fills": 25,
         "cancels": 70, "replaced": 5, "rejected": 0},
    ])
    row = payload["per_pair"][0]
    assert row["fill_rate"] == pytest.approx(0.25)
    assert row["advisory"] is None


def test_advisory_unset_for_high_fill_rate():
    payload = _call_endpoint([
        {"ticker": "BTC-USD", "attempts": 100, "fills": 80,
         "cancels": 15, "replaced": 5, "rejected": 0},
    ])
    row = payload["per_pair"][0]
    assert row["fill_rate"] == pytest.approx(0.80)
    assert row["advisory"] is None


# ---------------------------------------------------------------------------
# Multi-pair totals + sort order
# ---------------------------------------------------------------------------

def test_totals_aggregate_across_pairs():
    payload = _call_endpoint([
        {"ticker": "BTC-USD", "attempts": 100, "fills": 30,
         "cancels": 60, "replaced": 8, "rejected": 2},
        {"ticker": "ETH-USD", "attempts": 50, "fills": 10,
         "cancels": 35, "replaced": 5, "rejected": 0},
    ])
    t = payload["totals"]
    assert t["attempts"] == 150
    assert t["fills"] == 40
    assert t["cancels"] == 95
    assert t["replaced"] == 13
    assert t["fill_rate"] == pytest.approx(40.0 / 150.0)


# ---------------------------------------------------------------------------
# Empty -> ok with zeroed totals
# ---------------------------------------------------------------------------

def test_empty_result_set_returns_zeroed_totals():
    payload = _call_endpoint([])
    assert payload["ok"] is True
    assert payload["per_pair"] == []
    assert payload["totals"]["attempts"] == 0
    assert payload["totals"]["fill_rate"] is None


# ---------------------------------------------------------------------------
# DB error path
# ---------------------------------------------------------------------------

def test_db_exception_surfaces_in_payload():
    """If the aggregation query raises, the endpoint returns ok=false +
    error string rather than propagating the exception."""
    import json
    from app.routers.trading_sub import fast_path_api as mod

    @contextmanager
    def _broken_begin():
        raise RuntimeError("boom")
        yield  # pragma: no cover

    eng = MagicMock(name="engine")
    eng.begin = _broken_begin

    with patch.object(mod, "engine", eng), \
         patch(
            "app.services.trading.fast_path.settings.load",
            return_value=_stub_settings(),
         ):
        resp = mod.get_maker_stats()
    payload = json.loads(bytes(resp.body).decode("utf-8"))
    assert payload["ok"] is False
    assert "error" in payload
    assert "boom" in payload["error"]
