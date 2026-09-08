"""The bench must be able to run PATH B before it can judge it.

The wall PATH B exists to get past is reproduced faithfully in replay — 97
`alpaca_scale_out_suppressed_for_deadman` and 95
`tranche_oco_skipped_extended_hours` across 15 receipts — but the mock had no
`replace_order_qty`, so wiring the fix would have called a method that is not
there and the partial would never have filled. The bench would then have reported
"no effect" for a change it never ran. That is the mock-as-gate failure this
harness has already paid for once, and these tests exist so it cannot repeat
silently.

Every assertion below mirrors a REFUSAL or a shape of the real verb
(venue/alpaca_spot.py:4166, probe 2026-09-01 on paper). A mock that only says yes
is worse than no mock at all: it would certify a wiring the venue rejects.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural import replay_mock_broker as rmb


def _adapter():
    return rmb.MockBrokerAdapter()


def _rest(a, *, qty=1000.0, status="open", filled=0.0, oid="mock-1"):
    o = rmb._RestingOrder(
        order_id=oid, client_order_id="cid-1", product_id="CANF", side="sell",
        order_type="stop", base_size=qty, limit_price=None,
        created_time="2026-09-08T00:00:00Z", status=status, filled_size=filled,
        stop_price=4.20, time_in_force="gtc", position_intent="sell_to_close",
    )
    a._orders[oid] = o
    return o


def test_a_working_stop_shrinks_and_mints_a_new_id():
    """The shape the caller reads: a NEW order id, the old one named."""
    a = _adapter(); _rest(a, qty=1000.0)
    r = a.replace_order_qty(order_id="mock-1", new_qty="700")
    assert r["ok"] is True
    assert r["replaced_order_id"] == "mock-1"
    assert r["order_id"] != "mock-1"
    assert a._orders[r["order_id"]].base_size == pytest.approx(700.0)


def test_the_predecessor_is_left_replaced_not_deleted():
    """It is NOT atomic. The old order stays, terminal, so the runner's lineage
    proof sees the same two-order shape it sees live and must still wait."""
    a = _adapter(); _rest(a, qty=1000.0)
    r = a.replace_order_qty(order_id="mock-1", new_qty="700")
    pred = a._orders["mock-1"]
    succ = a._orders[r["order_id"]]
    assert pred.status == "replaced"
    assert pred.replaced_by == r["order_id"]
    assert succ.replaces == "mock-1"


def test_the_lineage_reaches_the_raw_shape_the_runner_parses():
    """`replaced_by` / `replaces` were hard-coded None. The certification readers
    parse the raw dict, so a lineage the raw shape cannot carry is invisible."""
    a = _adapter(); _rest(a, qty=1000.0)
    r = a.replace_order_qty(order_id="mock-1", new_qty="700")
    pred_raw = a._orders["mock-1"].to_normalized(alpaca_raw=True).raw
    succ_raw = a._orders[r["order_id"]].to_normalized(alpaca_raw=True).raw
    assert pred_raw["replaced_by"] == r["order_id"]
    assert succ_raw["replaces"] == "mock-1"


@pytest.mark.parametrize("status", ["accepted", "pending_new", "pending_cancel",
                                    "pending_replace", "filled", "canceled"])
def test_only_a_working_order_may_be_replaced(status):
    """The venue's 422. A caller that skips the lifecycle check must fail HERE, the
    way it would fail live — not sail through the bench and break in production."""
    a = _adapter(); _rest(a, qty=1000.0, status=status)
    r = a.replace_order_qty(order_id="mock-1", new_qty="700")
    assert r["ok"] is False
    assert r["error"] == "replace_rejected_not_working"


def test_releasing_shares_that_are_already_gone_is_refused():
    """A new qty below what the order already filled would release shares that no
    longer exist — the oversell this whole path exists to prevent."""
    a = _adapter(); _rest(a, qty=1000.0, filled=800.0)
    r = a.replace_order_qty(order_id="mock-1", new_qty="700")
    assert r["ok"] is False
    assert r["error"] == "replace_rejected_below_filled"


def test_growing_the_order_is_refused():
    """PATH B only ever shrinks. A larger successor would arm a stop for shares the
    position does not hold."""
    a = _adapter(); _rest(a, qty=1000.0)
    r = a.replace_order_qty(order_id="mock-1", new_qty="1200")
    assert r["ok"] is False
    assert r["error"] == "replace_rejected_qty_increase"


@pytest.mark.parametrize("bad", ["0", "-5", "abc", ""])
def test_a_nonsense_quantity_is_refused(bad):
    a = _adapter(); _rest(a, qty=1000.0)
    assert a.replace_order_qty(order_id="mock-1", new_qty=bad)["ok"] is False


def test_an_unknown_order_is_refused():
    a = _adapter()
    r = a.replace_order_qty(order_id="nope", new_qty="700")
    assert r["ok"] is False and r["error"] == "order_not_found"


def test_the_successor_keeps_every_protective_field():
    """Shrinking must change the SIZE and nothing else: a successor that quietly
    lost its stop price or its close intent is not the same protection."""
    a = _adapter(); _rest(a, qty=1000.0)
    r = a.replace_order_qty(order_id="mock-1", new_qty="700")
    s = a._orders[r["order_id"]]
    assert s.order_type == "stop"
    assert s.stop_price == pytest.approx(4.20)
    assert s.position_intent == "sell_to_close"
    assert s.time_in_force == "gtc"
    assert s.side == "sell"
    assert s.product_id == "CANF"


def test_the_wait_can_be_exercised_rather_than_assumed_away():
    """The real replace is not instant. A harness must be able to hold the
    successor un-acked so the caller's wait path actually runs."""
    a = _adapter(); a._replace_ack_ticks = 3
    _rest(a, qty=1000.0)
    r = a.replace_order_qty(order_id="mock-1", new_qty="700")
    assert a._orders[r["order_id"]].ack_delay_remaining == 3
