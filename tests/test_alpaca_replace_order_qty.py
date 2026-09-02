"""Adapter capability: bawasan ang qty ng nakaupong sell order via PATCH (#1276).

ANG KONTEKSTO. Ang buong-qty na resting deadman stop ay kumukonsumo ng buong
``qty_available`` sa Alpaca, kaya ang partial exit ay imposible:
``live_partial_exit_filled`` = ZERO mula 2026-08-01, at LAHAT ng 6 fill noong
2026-09-01 ay premarket — kung saan ang OCO tranche na landas (#1212) ay
haharangin ng broker (40310000 oco_rth_only; "extended_hours must be false").

MGA NASUKAT NA KATOTOHANAN (probe 2026-09-01, paper account, never-fill na
buy limit F @ 2.00):
  * PATCH round-trip: 263ms (una), 258ms (pangalawa).
  * PATCH ay 422 habang ang order ay ``accepted`` (naka-queue, sarado ang
    market). Working (``new``) na order lamang ang mapapalitan.
  * Ang PATCH ay gumagawa ng BAGONG order id; ang luma ay nagiging
    ``replaced``. Hindi atomic; ang reservation habang lumilipat ay ang mas
    malaki sa dalawa — DAPAT hintayin ng caller ang terminal na kumpirmasyon
    bago isumite ang partial sell.

Runnable: pytest tests/test_alpaca_replace_order_qty.py -v
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter


@pytest.fixture()
def adapter():
    a = AlpacaSpotAdapter.__new__(AlpacaSpotAdapter)   # walang network sa __init__
    return a


class _FakeClient:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def replace_order_by_id(self, order_id, req):
        self.calls.append((order_id, req))
        if self.exc:
            raise self.exc
        return self.result


def _wire(adapter, client):
    adapter._account_client = lambda: client
    return client


def test_happy_path_returns_the_new_order_id(adapter):
    """Ang PATCH ay gumagawa ng BAGONG id — dapat itong iuwi ng caller."""
    fake = _wire(adapter, _FakeClient(result=SimpleNamespace(
        id="new-order-uuid", status=SimpleNamespace(value="new"),
    )))
    out = adapter.replace_order_qty(order_id="old-order-uuid", new_qty="1")
    assert out["ok"] is True
    assert out["order_id"] == "new-order-uuid"
    assert out["replaced_order_id"] == "old-order-uuid"
    assert out["new_qty"] == "1"
    (oid, req), = fake.calls
    assert oid == "old-order-uuid"
    assert req.qty == 1


def test_fractional_qty_is_refused_before_transport(adapter):
    """Kapareho ng deadman: ang fractional ay hindi sertipikado."""
    _wire(adapter, _FakeClient(exc=AssertionError("hindi dapat maabot")))
    out = adapter.replace_order_qty(order_id="x", new_qty="1.5")
    assert out["ok"] is False
    assert out["error"] == "alpaca_fractional_replace_not_certified"
    assert out["pre_submit_blocked"] is True


@pytest.mark.parametrize("bad_qty", ["0", "-1", "kalokohan", None, "nan"])
def test_unusable_qty_is_refused_before_transport(adapter, bad_qty):
    fake = _wire(adapter, _FakeClient(exc=AssertionError("hindi dapat maabot")))
    out = adapter.replace_order_qty(order_id="x", new_qty=bad_qty)
    assert out["ok"] is False
    assert out["pre_submit_blocked"] is True
    assert fake.calls == []


def test_missing_order_id_is_refused_before_transport(adapter):
    fake = _wire(adapter, _FakeClient(exc=AssertionError("hindi dapat maabot")))
    for oid in ("", None, "   "):
        out = adapter.replace_order_qty(order_id=oid, new_qty="1")
        assert out["ok"] is False
        assert out["pre_submit_blocked"] is True
    assert fake.calls == []


def test_the_measured_422_shape_fails_open_with_the_error(adapter):
    """Ang nasukat na kaso: PATCH sa `accepted` na order ⇒ 422 mula sa broker.

    Ang adapter ay HINDI dapat sumabog — ibinabalik ang error para ang caller
    (na may hawak pa rin ng LUMANG order) ay makapag-fallback nang malinis.
    """
    _wire(adapter, _FakeClient(exc=RuntimeError(
        '{"code":42210000,"message":"unable to replace order in current state"}'
    )))
    out = adapter.replace_order_qty(order_id="queued-order", new_qty="1")
    assert out["ok"] is False
    assert "unable to replace" in out["error"]
    assert out["replaced_order_id"] == "queued-order"
    assert out["order_id"] is None, "walang bagong order ⇒ hawak pa rin ang luma"


def test_client_order_id_is_passed_through_when_given(adapter):
    fake = _wire(adapter, _FakeClient(result=SimpleNamespace(
        id="n", status=SimpleNamespace(value="new"),
    )))
    adapter.replace_order_qty(
        order_id="o", new_qty="2", client_order_id="chili_rp_123",
    )
    (_oid, req), = fake.calls
    assert req.client_order_id == "chili_rp_123"
    assert req.qty == 2
