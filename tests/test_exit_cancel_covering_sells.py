"""Agentic exit: cancel covering SELLs before a full-position exit (2026-06-23 strand fix).

A resting partial-target SELL locks shares, so the full stop/trail/bailout is rejected
"Not enough shares to sell" -> 8 retries -> live_error -> stranded. The helper cancels ANY
working agentic SELL for the symbol first. These unit-test that it cancels only open SELLs
and is best-effort/safe.
"""
from __future__ import annotations

from app.services.trading.momentum_neural.live_runner import _cancel_agentic_covering_sells


class _FakeAdapter:
    def __init__(self, orders):
        self._orders = orders
        self.cancelled: list[str] = []

    def get_agentic_open_orders(self, *, symbol=None):
        return self._orders

    def cancel_order(self, oid):
        self.cancelled.append(str(oid))
        return {"ok": True}


def test_cancels_open_sells_only():
    orders = [
        {"id": "s1", "side": "sell", "state": "confirmed"},        # cancel
        {"id": "s2", "side": "sell", "state": "queued"},           # cancel
        {"id": "b1", "side": "buy", "state": "confirmed"},         # skip: buy
        {"id": "s3", "side": "sell", "state": "filled"},           # skip: done
        {"id": "s4", "side": "sell", "state": "cancelled"},        # skip: done
        {"id": "s5", "side": "sell", "state": "partially_filled"}, # cancel
    ]
    ad = _FakeAdapter(orders)
    assert _cancel_agentic_covering_sells(ad, "AIIO") == 3
    assert ad.cancelled == ["s1", "s2", "s5"]


def test_missing_methods_safe():
    class _Bare:
        pass

    assert _cancel_agentic_covering_sells(_Bare(), "AIIO") == 0


def test_empty_open_orders_safe():
    class _A:
        def get_agentic_open_orders(self, *, symbol=None):
            return []

        def cancel_order(self, oid):
            return {}

    assert _cancel_agentic_covering_sells(_A(), "AIIO") == 0


def test_cancel_exception_isolated():
    # one cancel raising must not abort the rest
    class _A:
        def __init__(self):
            self.cancelled = []

        def get_agentic_open_orders(self, *, symbol=None):
            return [
                {"id": "s1", "side": "sell", "state": "confirmed"},
                {"id": "s2", "side": "sell", "state": "confirmed"},
            ]

        def cancel_order(self, oid):
            if oid == "s1":
                raise RuntimeError("boom")
            self.cancelled.append(str(oid))
            return {"ok": True}

    ad = _A()
    assert _cancel_agentic_covering_sells(ad, "X") == 1  # s2 cancelled despite s1 raising
    assert ad.cancelled == ["s2"]
