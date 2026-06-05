"""Unit tests for the pure trades-API contract helper.

No DB required (pure function), but conftest still enforces a ``_test`` DB URL.
"""
from __future__ import annotations

from datetime import datetime

from app.services.trading.trades_api_contract import _stable_trades_shadow_mismatches


def _current(**over):
    base = {
        "id": 1,
        "local_entry_price": 10.0,
        "local_quantity": 5.0,
        "ticker": "AAA",
        "status": "open",
    }
    base.update(over)
    return base


def _envelope(**over):
    base = {
        "id": 1,
        "entry_price": 10.0,
        "quantity": 5.0,
        "ticker": "AAA",
        "status": "open",
    }
    base.update(over)
    return base


def test_matching_rows_have_no_mismatch():
    assert _stable_trades_shadow_mismatches([_current()], [_envelope()]) == []


def test_entry_price_mismatch_uses_local_entry_price():
    out = _stable_trades_shadow_mismatches(
        [_current(local_entry_price=11.0)], [_envelope(entry_price=10.0)]
    )
    assert out == [
        {"id": 1, "field": "entry_price", "current": 11.0, "envelope": 10.0}
    ]


def test_quantity_mismatch_uses_local_quantity():
    out = _stable_trades_shadow_mismatches(
        [_current(local_quantity=4.0)], [_envelope(quantity=5.0)]
    )
    assert out == [{"id": 1, "field": "quantity", "current": 4.0, "envelope": 5.0}]


def test_shadow_field_mismatch_is_reported():
    out = _stable_trades_shadow_mismatches(
        [_current(status="closed")], [_envelope(status="open")]
    )
    assert len(out) == 1 and out[0]["field"] == "status"


def test_missing_envelope_flags_id_sentinel():
    out = _stable_trades_shadow_mismatches([_current(id=7)], [])
    assert out == [{"id": 7, "field": "id", "current": "present", "envelope": None}]


def test_datetime_envelope_compared_as_isoformat():
    dt = datetime(2026, 6, 5, 1, 2, 3)
    # current carries the isoformat string; envelope carries the datetime object —
    # the helper isoformats the envelope side, so these MATCH.
    cur = _current(exit_date=dt.isoformat())
    env = _envelope(exit_date=dt)
    assert _stable_trades_shadow_mismatches([cur], [env]) == []


def test_datetime_mismatch_is_detected_after_isoformat():
    cur = _current(exit_date="2026-06-05T01:02:03")
    env = _envelope(exit_date=datetime(2026, 6, 5, 9, 9, 9))
    out = _stable_trades_shadow_mismatches([cur], [env])
    assert len(out) == 1 and out[0]["field"] == "exit_date"


def test_row_with_none_id_is_skipped():
    assert _stable_trades_shadow_mismatches([_current(id=None)], [_envelope()]) == []


def test_at_most_one_mismatch_per_row():
    # two differing fields; entry_price is compared first, so only it is reported.
    out = _stable_trades_shadow_mismatches(
        [_current(local_entry_price=99.0, status="closed")], [_envelope()]
    )
    assert len(out) == 1 and out[0]["field"] == "entry_price"


def test_envelope_indexed_by_id_across_rows():
    cur = [_current(id=1), _current(id=2, status="closed")]
    env = [_envelope(id=2, status="open"), _envelope(id=1)]
    out = _stable_trades_shadow_mismatches(cur, env)
    assert out == [{"id": 2, "field": "status", "current": "closed", "envelope": "open"}]
