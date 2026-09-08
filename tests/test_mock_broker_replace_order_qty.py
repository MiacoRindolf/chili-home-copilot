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


# ══ THE RESERVATION THE VENUE ENFORCES AND NOBODY MODELLED ══════════════════════
# `qty_available` appeared in this repository only inside comments — not in the
# mock, not in the runner. A resting SELL holds its whole quantity at the broker,
# which is the single fact PATH B exists to work around: the full-qty deadman
# consumes it, so a partial cannot be placed until the stop is shrunk AND the
# shrink is terminal.
#
# Without this, a wiring that sold the partial too early would FILL in the bench
# and be REJECTED live, and the bench would have blessed it — the same
# mock-certifies-what-the-venue-rejects failure as the verb above, hiding one
# level down. These tests are what force the wiring to wait.

def _buy(a, qty, px=4.00, pid="CANF"):
    """Establish a real position through the mock's own fill ledger."""
    a._fills.append(rmb._Fill(
        product_id=pid, side="buy", size=qty, price=px, fee=0.0,
        order_id="seed", client_order_id="seed", trade_time="2026-09-08T00:00:00Z",
    ) if hasattr(rmb, "_Fill") else None)


def test_a_resting_stop_reserves_its_shares_against_a_second_sell():
    """The wall PATH B exists to get past, now present in the harness."""
    a = _adapter()
    a.get_position_quantity_truth = lambda pid: {  # type: ignore[assignment]
        "readable": True, "product_id": pid, "quantity": 1000.0, "reason": None}
    _rest(a, qty=1000.0)
    r = a.place_market_order(product_id="CANF", side="sell", base_size="300",
                             client_order_id="partial-1")
    assert r["ok"] is False
    assert r["error"] == "insufficient_qty_available"
    assert r["reserved_qty"] == pytest.approx(1000.0)
    assert r["available_qty"] == pytest.approx(0.0)


def test_shrinking_the_stop_is_what_frees_the_shares():
    """The whole of PATH B in one test: refuse, PATCH, then the same sell lands."""
    a = _adapter()
    a.get_position_quantity_truth = lambda pid: {  # type: ignore[assignment]
        "readable": True, "product_id": pid, "quantity": 1000.0, "reason": None}
    _rest(a, qty=1000.0)
    assert a.place_market_order(product_id="CANF", side="sell",
                                base_size="300")["ok"] is False
    rep = a.replace_order_qty(order_id="mock-1", new_qty="700")
    assert rep["ok"] is True
    # The predecessor is `replaced` and no longer reserves; the successor holds 700.
    r = a.place_market_order(product_id="CANF", side="sell", base_size="300",
                             client_order_id="partial-1")
    assert r.get("error") != "insufficient_qty_available"


def test_a_sell_within_the_free_remainder_is_allowed():
    a = _adapter()
    a.get_position_quantity_truth = lambda pid: {  # type: ignore[assignment]
        "readable": True, "product_id": pid, "quantity": 1000.0, "reason": None}
    _rest(a, qty=700.0)
    r = a.place_market_order(product_id="CANF", side="sell", base_size="300")
    assert r.get("error") != "insufficient_qty_available"


def test_nothing_resting_means_nothing_reserved():
    """Deliberately narrow: the claim is 'a resting sell reserves its shares', not
    'you cannot sell what you do not hold'. Suites that place sells against a
    synthetic flat book are untouched."""
    a = _adapter()
    r = a.place_market_order(product_id="CANF", side="sell", base_size="500")
    assert r.get("error") != "insufficient_qty_available"


def test_a_buy_is_never_reservation_checked():
    a = _adapter()
    a.get_position_quantity_truth = lambda pid: {  # type: ignore[assignment]
        "readable": True, "product_id": pid, "quantity": 1000.0, "reason": None}
    _rest(a, qty=1000.0)
    r = a.place_market_order(product_id="CANF", side="buy", base_size="300")
    assert r.get("error") != "insufficient_qty_available"
