"""Sell-into-strength invariants — the resting scale-out limit must NEVER let an
exit oversell: every market exit cancels it first, adopts what it filled, and
clamps the sell to the true remainder.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.services.trading.momentum_neural.live_runner as lr


class _FakeAdapter:
    def __init__(self, order):
        self._order = order
        self.cancelled = []
        self.placed = []

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return {"ok": True}

    def get_order(self, oid):
        return self._order, None

    def place_limit_order_gtc(self, **kw):
        self.placed.append(kw)
        return {"ok": True, "order_id": "SOL123"}


@pytest.fixture
def quiet(monkeypatch):
    """Silence the DB-coupled side effects; the math under test is pure-le."""
    monkeypatch.setattr(lr, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_commit_le", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_live_partial_exit_ledger_safe", lambda *a, **k: None)


def _sess():
    return SimpleNamespace(id=99, symbol="BATL", risk_snapshot_json={})


def test_cancel_adopts_partial_fill_and_clamps(quiet):
    # position 1000sh; resting limit (500sh) already filled 300 before the stop fires
    le = {
        "scale_limit_order_id": "SOL1", "scale_limit_px": 2.30, "scale_limit_qty": 500.0,
        "scale_limit_adopted_qty": 0.0,
        "position": {"quantity": 1000.0, "avg_entry_price": 2.00},
    }
    order = SimpleNamespace(filled_size=300.0, average_filled_price=2.31)
    ad = _FakeAdapter(order)
    q = lr._cancel_scale_limit_and_clamp(None, _sess(), ad, le=le, requested_qty=1000.0, reason="stop")
    assert ad.cancelled == ["SOL1"]
    assert le.get("scale_limit_order_id") is None          # always cleared
    assert le["position"]["quantity"] == pytest.approx(700.0)  # 300 adopted
    assert q == pytest.approx(700.0)                        # clamped: no oversell
    assert le["scale_limit_adopted_qty"] == pytest.approx(300.0)


def test_cancel_with_zero_fill_keeps_full_quantity(quiet):
    le = {
        "scale_limit_order_id": "SOL2", "scale_limit_px": 2.30, "scale_limit_qty": 500.0,
        "position": {"quantity": 1000.0, "avg_entry_price": 2.00},
    }
    ad = _FakeAdapter(SimpleNamespace(filled_size=0.0, average_filled_price=0.0))
    q = lr._cancel_scale_limit_and_clamp(None, _sess(), ad, le=le, requested_qty=1000.0, reason="stop")
    assert q == pytest.approx(1000.0)
    assert le["position"]["quantity"] == pytest.approx(1000.0)


def test_no_resting_order_is_passthrough(quiet):
    le = {"position": {"quantity": 800.0, "avg_entry_price": 2.0}}
    ad = _FakeAdapter(None)
    q = lr._cancel_scale_limit_and_clamp(None, _sess(), ad, le=le, requested_qty=800.0, reason="stop")
    assert q == pytest.approx(800.0)
    assert ad.cancelled == []


def test_double_adopt_is_idempotent(quiet):
    # 300 already adopted earlier (e.g. at the target block); cancel sees the same
    # 300 on the order -> nothing re-adopted, no double-count
    le = {
        "scale_limit_order_id": "SOL3", "scale_limit_px": 2.30, "scale_limit_qty": 500.0,
        "scale_limit_adopted_qty": 300.0,
        "position": {"quantity": 700.0, "avg_entry_price": 2.00},
    }
    ad = _FakeAdapter(SimpleNamespace(filled_size=300.0, average_filled_price=2.31))
    q = lr._cancel_scale_limit_and_clamp(None, _sess(), ad, le=le, requested_qty=700.0, reason="stop")
    assert q == pytest.approx(700.0)
    assert le["position"]["quantity"] == pytest.approx(700.0)


def test_fmt_limit_price_sell_floors():
    assert lr._fmt_limit_price_sell(2.3099) == "2.30"  # never asks above the level
    assert lr._fmt_limit_price_sell(10.0) == "10.00"
    assert lr._fmt_limit_price_sell(0.4567) == "0.4567"


def test_place_scale_out_limit_stores_keys(quiet):
    le = {"position": {"quantity": 1000.0, "avg_entry_price": 2.0}}
    ad = _FakeAdapter(None)
    lr._place_scale_out_limit(
        None, _sess(), ad, le=le, product_id="BATL", target_px=2.30, filled=1000.0, prod=None
    )
    assert le.get("scale_limit_order_id") == "SOL123"
    assert le["scale_limit_px"] == pytest.approx(2.30)
    assert ad.placed and ad.placed[0]["side"] == "sell"
    assert float(ad.placed[0]["limit_price"]) <= 2.30  # penny-floored


def test_place_failure_is_fail_open(quiet):
    class _Bad(_FakeAdapter):
        def place_limit_order_gtc(self, **kw):
            return {"ok": False, "error": "rejected"}

    le = {"position": {"quantity": 1000.0, "avg_entry_price": 2.0}}
    lr._place_scale_out_limit(
        None, _sess(), _Bad(None), le=le, product_id="BATL", target_px=2.30, filled=1000.0, prod=None
    )
    assert "scale_limit_order_id" not in le  # reactive path stays in charge
