"""The replay mock must model Alpaca's qty PATCH on BOTH sides (PATH B, 2026-09-06).

PATH B shrinks the resting full-qty deadman from Q to R = Q - f so the f-tranche becomes
sellable while a stop still covers the runner. Alpaca answers a PATCH by retiring the
predecessor as ``replaced`` (with ``replaced_by``) and resting a NEW order carrying
``replaces``; the runner certifies BOTH links plus the immutable stop envelope
(``_alpaca_replacement_successor_order_matches``) before it trusts the edge. A mock that
mutated ``base_size`` in place would certify nothing, and the bench would measure a
suppression that live does not have.

Runnable: pytest tests/test_replay_mock_replace_order_qty.py -v   (DB-free)
"""
from __future__ import annotations

from app.services.trading.momentum_neural import live_runner as lr
from datetime import datetime, timezone

from app.services.trading.momentum_neural.replay_mock_broker import (
    MockBrokerAdapter,
    RecordedQuote,
)

SYMBOL = "CANF"
Q, R = 355.0, 249.0
STOP = 3.91


REGULAR = datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc)  # 10:00 ET


def _broker() -> MockBrokerAdapter:
    b = MockBrokerAdapter(slippage_bps=0.0, venue_rt_bps=100.0, resting_limit_fills=True,
                          freshness_mode="sim")
    b.set_clock(REGULAR)
    b.set_quote(SYMBOL, RecordedQuote(bid=4.60, ask=4.62, last=4.61))
    assert b.place_market_order(product_id=SYMBOL, side="buy", base_size=str(int(Q)),
                                client_order_id="entry-1")["ok"] is True
    return b


def _deadman(b, cid="chili-deadman-gen7"):
    out = b.place_deadman_stop(product_id=SYMBOL, base_size=str(int(Q)), stop_price=STOP, client_order_id=cid)
    assert out["ok"] is True, out
    return out


def test_the_patch_retires_the_predecessor_and_rests_a_linked_successor():
    b = _broker()
    pred = _deadman(b)
    out = b.replace_order_qty(order_id=pred["order_id"], new_qty=str(int(R)),
                              client_order_id="chili-deadman-gen8")
    assert out["ok"] is True and out["replaces"] == pred["order_id"]
    predecessor, _ = b.get_order(pred["order_id"])
    successor, _ = b.get_order(out["order_id"])
    assert lr._alpaca_protective_order_lifecycle(predecessor) == "replaced"
    assert predecessor.raw["replaced_by"] == out["order_id"]
    assert successor.raw["replaces"] == pred["order_id"]
    assert successor.raw["qty"] == R
    # the immutable envelope is inherited verbatim — only qty and cid move
    for field in ("stop_price", "time_in_force", "position_intent", "extended_hours"):
        assert successor.raw[field] == predecessor.raw[field], field


def test_the_successor_is_certified_by_the_production_matcher():
    b = _broker()
    pred = _deadman(b, cid="chili-deadman-m1")
    out = b.replace_order_qty(order_id=pred["order_id"], new_qty=str(int(R)),
                              client_order_id="chili-deadman-m2")
    successor, _ = b.get_order(out["order_id"])
    envelope = {
        "product_id": SYMBOL, "side": "sell", "order_type": "stop",
        "base_size": R, "stop_price": STOP, "time_in_force": "gtc",
        "position_intent": "sell_to_close", "extended_hours": False,
        "client_order_id": "chili-deadman-m2",
    }
    assert lr._alpaca_replacement_successor_order_matches(
        successor,
        predecessor_broker_order_id=pred["order_id"],
        successor_broker_order_id=out["order_id"],
        successor_order_request=envelope,
    ) is True
    # the pre-PATCH envelope (base_size Q) must NOT certify the shrunk successor
    assert lr._alpaca_replacement_successor_order_matches(
        successor,
        predecessor_broker_order_id=pred["order_id"],
        successor_broker_order_id=out["order_id"],
        successor_order_request={**envelope, "base_size": Q},
    ) is False


def test_the_patch_refuses_what_the_broker_refuses():
    b = _broker()
    pred = _deadman(b, cid="chili-deadman-r1")
    assert b.replace_order_qty(order_id="nope", new_qty="10", client_order_id="x")["ok"] is False
    # growing a protective order is refused
    grow = b.replace_order_qty(order_id=pred["order_id"], new_qty=str(int(Q + 10)), client_order_id="chili-r2")
    assert grow["ok"] is False and grow["http_status"] == 422
    # a fractional protective qty is refused (the GTC protection contract)
    frac = b.replace_order_qty(order_id=pred["order_id"], new_qty="248.5", client_order_id="chili-r3")
    assert frac["ok"] is False
    # a duplicate client_order_id is refused, as at the venue
    dup = b.replace_order_qty(order_id=pred["order_id"], new_qty=str(int(R)), client_order_id="chili-deadman-r1")
    assert dup["ok"] is False and dup["http_status"] == 422
    # the real one succeeds, and the retired predecessor cannot be patched again
    ok = b.replace_order_qty(order_id=pred["order_id"], new_qty=str(int(R)), client_order_id="chili-r4")
    assert ok["ok"] is True
    again = b.replace_order_qty(order_id=pred["order_id"], new_qty="200", client_order_id="chili-r5")
    assert again["ok"] is False and again["http_status"] == 422


def test_the_shrunk_stop_still_protects_the_runner():
    """After the PATCH the successor is a live stop for R — protection is never dropped."""
    b = _broker()
    pred = _deadman(b, cid="chili-deadman-p1")
    out = b.replace_order_qty(order_id=pred["order_id"], new_qty=str(int(R)), client_order_id="chili-deadman-p2")
    successor, _ = b.get_order(out["order_id"])
    assert lr._alpaca_protective_order_is_certifiably_active(successor) is True
    assert successor.raw["stop_price"] == STOP and successor.raw["qty"] == R
