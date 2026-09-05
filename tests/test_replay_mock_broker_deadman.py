"""The replay mock's Alpaca dead-man stop (alpaca canon gate #10, 2026-09-05).

Batch 1 of the alpaca sweep held every position to the window's end: ``place_deadman_stop``
did not exist on the mock, the AttributeError became ``deadman_submit_indeterminate``, the
strict CID read resolved ``unknown``, and live_runner.py:42598 returned on every held tick
before any exit logic. These tests pin the contract the runner certifies against."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.trading.momentum_neural.replay_mock_broker import (
    MockBrokerAdapter,
    RecordedQuote,
)

PREMARKET = datetime(2026, 7, 22, 12, 3, tzinfo=timezone.utc)   # 08:03 ET
REGULAR = datetime(2026, 7, 22, 14, 0, tzinfo=timezone.utc)     # 10:00 ET


def _broker() -> MockBrokerAdapter:
    b = MockBrokerAdapter(slippage_bps=0.0, venue_rt_bps=100.0, resting_limit_fills=True,
                          freshness_mode="sim")
    b.set_clock(PREMARKET)
    b.set_quote("LABT", RecordedQuote(bid=2.07, ask=2.09, last=2.08))
    assert b.place_market_order(product_id="LABT", side="buy", base_size="166",
                                client_order_id="entry-1")["ok"] is True
    return b


def test_refusals_mirror_the_real_adapter_pre_submit_errors() -> None:
    b = _broker()
    no_cid = b.place_deadman_stop(product_id="LABT", base_size="166", stop_price=1.9)
    assert no_cid["ok"] is False and no_cid["pre_submit_blocked"] is True
    assert no_cid["error"] == "alpaca_deadman_instruction_not_certified"
    frac = b.place_deadman_stop(product_id="LABT", base_size="166.5", stop_price=1.9,
                                client_order_id="dm-frac")
    assert frac["error"] == "alpaca_fractional_deadman_not_certified"
    crypto = b.place_deadman_stop(product_id="BTC-USD", base_size="1", stop_price=1.0,
                                  client_order_id="dm-crypto")
    assert crypto["error"] == "alpaca_deadman_instruction_not_certified"
    assert b.place_deadman_stop(product_id="LABT", base_size="166", stop_price=0.0,
                                client_order_id="dm-zero")["ok"] is False


def test_a_placed_stop_is_found_by_strict_cid_lookup_in_the_broker_raw_shape() -> None:
    b = _broker()
    placed = b.place_deadman_stop(product_id="LABT", base_size="166", stop_price=1.9,
                                  client_order_id="dm-1")
    assert placed["ok"] is True and placed["status"] == "new"
    assert placed["order_request"]["order_type"] == "stop"
    assert placed["order_request"]["time_in_force"] == "gtc"
    truth = b.get_order_by_client_order_id_truth("dm-1")
    assert truth["readable"] is True and truth["found"] is True
    o = truth["order"]
    assert o.order_id == placed["order_id"] and o.client_order_id == "dm-1"
    assert o.side == "sell" and o.order_type == "stop" and o.status == "open"
    assert o.raw["alpaca_status"] == "new"
    assert o.raw["qty"] == 166.0 and o.raw["stop_price"] == 1.9
    assert o.raw["time_in_force"] == "gtc" and o.raw["position_intent"] == "sell_to_close"
    assert o.raw["extended_hours"] is False and o.raw["limit_price"] is None
    assert o.raw["filled_size"] == 0 and o.raw["fill_truth_readable"] is True
    absent = b.get_order_by_client_order_id_truth("never-placed")
    assert absent == {"readable": True, "found": False, "order": None}
    dup = b.place_deadman_stop(product_id="LABT", base_size="166", stop_price=1.9,
                               client_order_id="dm-1")
    assert dup["ok"] is False and dup["submit_outcome"] == "broker_rejected"


def test_the_runner_certifies_the_mock_stop_with_its_own_matcher() -> None:
    from app.services.trading.momentum_neural.live_runner import (
        _alpaca_protective_order_is_certifiably_active,
        _owner_transport_order_matches,
    )
    b = _broker()
    placed = b.place_deadman_stop(product_id="LABT", base_size="166", stop_price=1.9,
                                  client_order_id="dm-1")
    transport = {"client_order_id": "dm-1", "order_request": placed["order_request"]}
    order = b.get_order_by_client_order_id_truth("dm-1")["order"]
    assert _owner_transport_order_matches(order, transport) is True
    assert _alpaca_protective_order_is_certifiably_active(order) is True


def test_the_stop_is_inert_premarket_and_triggers_in_the_regular_session() -> None:
    b = _broker()
    b.place_deadman_stop(product_id="LABT", base_size="166", stop_price=1.9,
                         client_order_id="dm-1")
    # premarket: bid through the stop, nothing happens (Alpaca queues it until the open)
    b.set_quote("LABT", RecordedQuote(bid=1.80, ask=1.82, last=1.81))
    o = b.get_order_by_client_order_id_truth("dm-1")["order"]
    assert o.status == "open" and o.raw["alpaca_status"] == "new"
    fills, _ = b.get_fills(product_id="LABT", limit=10)
    assert [f.side for f in fills] == ["buy"]
    # regular session: the same bid triggers; fill = min(stop, bid) = the bid
    b.set_clock(REGULAR)
    o = b.get_order_by_client_order_id_truth("dm-1")["order"]
    assert o.status == "filled" and o.raw["alpaca_status"] == "filled"
    assert o.filled_size == 166.0 and o.average_filled_price == pytest.approx(1.80)
    assert o.raw["filled_size"] == 166 and o.raw["filled_at"] is not None
    fills, _ = b.get_fills(product_id="LABT", limit=10)
    assert [f.side for f in fills] == ["buy", "sell"] and fills[-1].price == pytest.approx(1.80)
    assert b.get_position_quantity_truth("LABT")["quantity"] == pytest.approx(0.0)


def test_a_stop_above_the_bid_rests_until_the_bid_reaches_it() -> None:
    b = _broker()
    b.set_clock(REGULAR)
    b.set_quote("LABT", RecordedQuote(bid=2.50, ask=2.52, last=2.51))
    b.place_deadman_stop(product_id="LABT", base_size="166", stop_price=1.9,
                         client_order_id="dm-1")
    assert b.get_order_by_client_order_id_truth("dm-1")["order"].status == "open"
    b.set_quote("LABT", RecordedQuote(bid=1.95, ask=1.97, last=1.96))
    assert b.get_order_by_client_order_id_truth("dm-1")["order"].status == "open"
    b.set_quote("LABT", RecordedQuote(bid=1.90, ask=1.92, last=1.91))
    o = b.get_order_by_client_order_id_truth("dm-1")["order"]
    assert o.status == "filled" and o.average_filled_price == pytest.approx(1.90)


def test_cancel_stops_the_stop_from_ever_filling() -> None:
    b = _broker()
    placed = b.place_deadman_stop(product_id="LABT", base_size="166", stop_price=1.9,
                                  client_order_id="dm-1")
    assert b.cancel_order(placed["order_id"])["ok"] is True
    b.set_clock(REGULAR)
    b.set_quote("LABT", RecordedQuote(bid=1.50, ask=1.52, last=1.51))
    o = b.get_order_by_client_order_id_truth("dm-1")["order"]
    assert o.status in ("canceled", "cancelled") and o.raw["alpaca_status"] == "canceled"
    assert o.filled_size == 0.0


def test_non_stop_orders_keep_their_raw_shape_byte_identical() -> None:
    b = _broker()
    o, _ = b.get_order(b._orders and next(iter(b._orders)))
    assert set(o.raw) == {"venue", "fee"}


def test_cancel_by_id_is_the_runner_verb_and_truth_is_reread_after_it() -> None:
    b = _broker()
    placed = b.place_deadman_stop(product_id="LABT", base_size="166", stop_price=1.9,
                                  client_order_id="dm-1")
    assert b.cancel_order_by_id(placed["order_id"]) is True
    o = b.get_order_by_client_order_id_truth("dm-1")["order"]
    assert o.status in ("canceled", "cancelled") and o.raw["alpaca_status"] == "canceled"
    assert b.cancel_order_by_id("replay_mock-99999999") is False
    assert b.get_order_by_client_order_id("dm-1").order_id == placed["order_id"]
    assert b.get_order_by_client_order_id("never") is None


def test_position_quantity_is_the_signed_book_and_zero_when_flat() -> None:
    b = _broker()
    assert b.get_position_quantity("LABT") == pytest.approx(166.0)
    assert b.get_position_quantity("NOPE") == 0.0
    b.set_clock(REGULAR)
    b.set_quote("LABT", RecordedQuote(bid=2.30, ask=2.32, last=2.31))
    assert b.place_market_order(product_id="LABT", side="sell", base_size="166",
                                client_order_id="exit-1")["ok"] is True
    assert b.get_position_quantity("LABT") == pytest.approx(0.0)


def test_the_release_path_preconditions_hold_on_the_mock() -> None:
    # the three adapter facts live_runner.py:15004-15035 and :12512 need before an exit
    # can post while a deadman rests: cancel verb, strict CID truth terminal after it,
    # readable position quantity.
    b = _broker()
    placed = b.place_deadman_stop(product_id="LABT", base_size="166", stop_price=1.9,
                                  client_order_id="dm-1")
    assert hasattr(b, "cancel_order_by_id") and hasattr(b, "get_position_quantity")
    assert b.cancel_order_by_id(placed["order_id"]) is True
    truth = b.get_order_by_client_order_id_truth("dm-1")
    assert truth["found"] is True and truth["order"].status in ("canceled", "cancelled")
    assert b.get_position_quantity("LABT") == pytest.approx(166.0)   # still held: exit is next
