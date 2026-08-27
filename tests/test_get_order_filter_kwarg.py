"""Ang get_order_by_id kwarg ay `filter=`, HINDI `options=` (2026-08-27 P0).

ANG INSIDENTE: ang #1212 nested=True change ay gumamit ng `options=` — ang SDK
ay `filter=` — kaya BAWAT get_order/get_order_truth call sa loob ng ~4 na oras
ay TypeError => None. Ang CELU 236sh @ 2.09 fill ay hindi nakita ng confirm
loop nang ~55 minuto habang bumabagsak ang presyo -12%; kinailangan ng manual
state repair + quote-independent flatten. Ang leksyon: ang source-assertion na
"nasa code ang GetOrderByIdRequest(nested=True)" ay HINDI pagsubok ng TAWAG.

Runnable: pytest tests/test_get_order_filter_kwarg.py -v
"""
from __future__ import annotations

import inspect

from types import SimpleNamespace

from app.services.trading.venue import alpaca_spot as AS


def test_the_sdk_signature_accepts_our_kwarg():
    """ANG PANGUNAHING KASO: ang kwarg na ginagamit namin ay dapat TALAGANG
    nasa SDK signature — behavioral, hindi text assertion."""
    from alpaca.trading.client import TradingClient

    params = inspect.signature(TradingClient.get_order_by_id).parameters
    src = inspect.getsource(AS.AlpacaSpotAdapter.get_order) + inspect.getsource(
        AS.AlpacaSpotAdapter.get_order_truth
    )
    assert "filter=GetOrderByIdRequest" in src
    assert "filter" in params, "nawala ang filter param sa SDK?!"
    assert "options=GetOrderByIdRequest" not in src, "ang sirang kwarg ay bawal bumalik"


def test_get_order_actually_calls_through(monkeypatch):
    """Behavioral: ang get_order ay dapat nakakarating sa client nang WALANG
    TypeError at naibabalik ang normalized order."""
    calls = []

    class _FakeClient:
        def get_order_by_id(self, order_id, filter=None):
            calls.append((order_id, filter))
            return SimpleNamespace(id="oid-1", status=SimpleNamespace(value="filled"))

    a = AS.AlpacaSpotAdapter.__new__(AS.AlpacaSpotAdapter)
    monkeypatch.setattr(a, "_account_client", lambda: _FakeClient(), raising=False)
    monkeypatch.setattr(
        AS.AlpacaSpotAdapter, "_normalize_order", lambda self, o: {"ok": True}, raising=False
    )
    out, _fresh = a.get_order("oid-1")
    assert out == {"ok": True}, "ang tawag ay dapat nagtagumpay"
    assert calls and calls[0][0] == "oid-1"
    assert calls[0][1] is not None and getattr(calls[0][1], "nested", None) is True
