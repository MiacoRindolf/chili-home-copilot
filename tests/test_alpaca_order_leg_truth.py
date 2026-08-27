"""Ang OCO child legs ay dating invisible sa normalization.

OCO SUBSTRATE, item 1 ng SAFE_WITH_CHANGES list (2026-08-27 audit). Ang child
legs ng isang OCO ay WALANG client_order_id -- ang parent lamang ang binibigyan
ng Alpaca -- kaya ang TANGING identity chain ng stop leg ay
``parent_cid -> parent_oid -> leg_id``. Bago ito, ang ``NormalizedOrder.raw`` ay
hindi kailanman naglalaman ng ``order_class`` o ``legs``, kaya ang bawat
downstream na sertipikasyon ay bulag sa kanila. Sabi ng audit: "Nothing else in
this list is implementable without it."

Runnable: pytest tests/test_alpaca_order_leg_truth.py -v
"""
from __future__ import annotations

import pytest

from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter


class _Leg:
    def __init__(self, oid, status, otype, qty, fq, favg, stop=None, limit=None):
        self.id = oid
        self.status = type("_S", (), {"value": status})()
        self.order_type = type("_T", (), {"value": otype})()
        self.qty = qty
        self.filled_qty = fq
        self.filled_avg_price = favg
        self.stop_price = stop
        self.limit_price = limit


class _Order:
    def __init__(self, legs=None, order_class=None):
        self.id = "parent-1"
        self.client_order_id = "cid-parent"
        self.symbol = "XPON"
        self.side = type("_Sd", (), {"value": "sell"})()
        self.status = type("_St", (), {"value": "new"})()
        self.order_type = type("_Ot", (), {"value": "limit"})()
        self.filled_qty = 0
        self.filled_avg_price = None
        self.created_at = "2026-08-27T15:00:00Z"
        self.qty = 47
        self.notional = None
        self.limit_price = 6.4
        self.stop_price = None
        self.time_in_force = type("_Tf", (), {"value": "gtc"})()
        self.extended_hours = False
        self.position_intent = None
        self.account_id = "acct"
        self.asset_class = type("_Ac", (), {"value": "us_equity"})()
        self.legs = legs
        self.order_class = (
            type("_Oc", (), {"value": order_class})() if order_class else None
        )


def _norm(o):
    a = AlpacaSpotAdapter()
    return a._normalize_order(o)


def test_an_oco_parent_carries_both_leg_identities():
    """ANG PANGUNAHING KASO. parent_cid -> parent_oid -> leg_id ang tanging
    paraan para mapangalanan ang stop leg pagkatapos ng restart."""
    o = _Order(order_class="oco", legs=[
        _Leg("leg-tp", "new", "limit", 47, 0, None, limit=6.40),
        _Leg("leg-sl", "held", "stop", 47, 0, None, stop=5.85),
    ])
    n = _norm(o)
    assert n.raw.get("order_class") == "oco"
    legs = n.raw.get("legs")
    assert [l["id"] for l in legs] == ["leg-tp", "leg-sl"]
    sl = legs[1]
    assert sl["status"] == "held"
    assert sl["stop_price"] == pytest.approx(5.85)
    assert sl["qty"] == pytest.approx(47.0)


def test_leg_fills_are_readable():
    """⚠️ Item 4 ng audit ay aampon mula sa leg's OWN filled_qty at
    filled_avg_price -- kailangang nasa raw ang mga iyon."""
    o = _Order(order_class="oco", legs=[
        _Leg("leg-tp", "filled", "limit", 47, 47, 6.41, limit=6.40),
        _Leg("leg-sl", "canceled", "stop", 47, 0, None, stop=5.85),
    ])
    legs = _norm(o).raw["legs"]
    assert legs[0]["filled_qty"] == pytest.approx(47.0)
    assert legs[0]["filled_avg_price"] == pytest.approx(6.41)


def test_a_simple_order_still_emits_the_fields_explicitly():
    """⚠️ Laging naka-emit: ang mambabasa ay hindi dapat manghula kung ang
    pagkawala ay 'walang legs' o 'lumang normalization'."""
    n = _norm(_Order())
    assert "order_class" in n.raw and n.raw["order_class"] is None
    assert n.raw["legs"] == []


def test_the_existing_fields_are_untouched():
    """Purong pagdagdag — walang umiiral na echo na nagbago."""
    n = _norm(_Order())
    assert n.raw["broker_client_order_id_echo"] == "cid-parent"
    assert n.raw["stop_price"] is None
    assert n.order_id == "parent-1"
