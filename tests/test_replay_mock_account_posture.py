"""Gate #8: the Alpaca entry seam's strict account posture read must be answerable by the mock.

MEASURED 2026-09-04 (SDOT 2026-06-26 canon run, seven gates answered): 19 of 26 entry
attempts deferred with ``alpaca_account_posture_unreadable`` -- ``_strict_alpaca_empty_
entry_posture`` requires ``adapter.list_positions()`` and ``adapter.list_open_orders(strict=
True)``; the mock had no ``list_positions`` and its ``list_open_orders`` refused the
``strict`` keyword (TypeError -> unreadable). These tests drive the REAL posture check.
DB-free.
"""
from __future__ import annotations

from datetime import datetime

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.replay_mock_broker import (
    MockBrokerAdapter,
    RecordedQuote,
)
from app.services.trading.venue.protocol import NormalizedFill


T = datetime(2026, 6, 26, 13, 18, 12)


def _mock():
    m = MockBrokerAdapter(resting_limit_fills=True, volume_cap_enabled=True, freshness_mode="wall")
    m.set_clock(T)
    m.set_quote("SDOT", RecordedQuote(bid=10.00, ask=10.02))
    return m


def test_flat_book_is_strictly_flat_for_the_real_posture_check():
    with lr.replay_clock(T):
        ok, info = lr._strict_alpaca_empty_entry_posture(_mock(), max_age_seconds=2.0)
    assert ok is True, info
    assert info["reason"] == "broker_account_strictly_flat"
    assert info["position_count"] == 0 and info["open_order_count"] == 0


def test_an_open_long_is_reported_as_position_exposure():
    m = _mock()
    m._fills.append(NormalizedFill(fill_id="f1", order_id="o1", product_id="SDOT", side="buy",
                                   size=10.0, price=10.02))
    with lr.replay_clock(T):
        ok, info = lr._strict_alpaca_empty_entry_posture(m, max_age_seconds=2.0)
    assert ok is False
    assert info["reason"] == "alpaca_account_position_exposure_present"
    assert info["position_count"] == 1


def test_list_positions_matches_the_real_adapter_row_shape_and_nets_fills():
    m = _mock()
    m._fills.append(NormalizedFill(fill_id="f1", order_id="o1", product_id="SDOT", side="buy",
                                   size=10.0, price=10.02))
    m._fills.append(NormalizedFill(fill_id="f2", order_id="o2", product_id="SDOT", side="sell",
                                   size=4.0, price=10.10))
    rows, meta = m.list_positions()
    assert rows == [{"product_id": "SDOT", "raw_symbol": "SDOT", "qty": 6.0, "side": "long",
                     "raw": {"venue": "replay_mock", "source": "replay_mock_book"}}]
    assert hasattr(meta, "age_seconds")


def test_list_open_orders_accepts_strict():
    orders, meta = _mock().list_open_orders(strict=True)
    assert orders == [] and hasattr(meta, "age_seconds")
