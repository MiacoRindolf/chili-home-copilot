"""A cancellation request never releases a still-working or unreadable tranche.

Exercise the real clamp and exit-submit seam using normalized broker orders;
only persistence, accounting sinks and scheduling are replaced. No broker I/O.
"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.venue.protocol import NormalizedOrder


def _order(*, status="cancelled", filled=0.0, oid="scale-1"):
    return NormalizedOrder(
        order_id=oid, client_order_id="chili_ml_sol_99_one", product_id="BATL",
        side="sell", status=status, order_type="limit", filled_size=filled,
        average_filled_price=2.3 if filled else None, raw={},
    )


class _LegacyAdapter:
    def __init__(self, order, *, cancel_raises=False):
        self.order = order
        self.cancel_raises = cancel_raises
        self.cancelled = []

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        if self.cancel_raises:
            raise RuntimeError("cancel response lost")
        return {"ok": True}

    def get_order(self, oid):
        if isinstance(self.order, Exception):
            raise self.order
        return self.order, None

    def __getattr__(self, name):
        if name.startswith("place_"):
            raise AssertionError("no order may escape this adapter")
        raise AttributeError(name)


class _StrictAdapter(_LegacyAdapter):
    def __init__(self, answer, **kwargs):
        super().__init__(None, **kwargs)
        self.answer = answer

    def get_order_truth(self, oid):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def get_order(self, oid):
        raise AssertionError("never downgrade a strict truth read")


def _strict(order, **kwargs):
    return _StrictAdapter({"readable": True, "found": True, "order": order}, **kwargs)


def _ledger(*, adopted=0.0):
    return {
        "scale_limit_order_id": "scale-1", "scale_limit_qty": 500.0,
        "scale_limit_px": 2.3, "scale_limit_adopted_qty": adopted,
        "scale_limit_client_order_id": "chili_ml_sol_99_one",
        "position": {"quantity": 1000.0 - adopted, "avg_entry_price": 2.0},
    }


def _sess(family="robinhood_spot"):
    return SimpleNamespace(id=99, symbol="BATL", execution_family=family, risk_snapshot_json={})


@pytest.fixture(autouse=True)
def sinks(monkeypatch):
    ledger_fills = []
    monkeypatch.setattr(lr, "_commit_le", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_fill_outcome_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_live_partial_exit_ledger_safe", lambda *a, **k: ledger_fills.append(k))
    monkeypatch.setattr(lr, "_schedule_exit_continuation", lambda *a, **k: None)
    return ledger_fills


def _clamp(adapter, le):
    return lr._cancel_scale_limit_and_clamp(
        None, _sess(), adapter, le=le, requested_qty=1000.0, reason="stop",
    )


@pytest.mark.parametrize("answer", [
    None, {}, {"readable": False}, {"readable": 1, "found": True, "order": _order()},
    {"readable": True}, {"readable": True, "found": False, "order": None},
    {"readable": True, "found": True, "order": None}, RuntimeError("read failed"),
    {"readable": True, "found": True, "order": _order(oid="different-order")},
])
def test_strict_uncertainty_retains_the_reservation(answer, sinks):
    le = _ledger()
    assert _clamp(_StrictAdapter(answer), le) is None
    assert le["scale_limit_order_id"] == "scale-1"
    assert le["position"]["quantity"] == 1000.0
    assert le["alpaca_scale_limit_release_block"]["reason"] == "scale_limit_cancel_not_exact_terminal"
    assert sinks == []


@pytest.mark.parametrize("order", [None, RuntimeError("lookup failed"), _order(oid="other-order")])
def test_legacy_missing_or_wrong_order_is_not_absence(order):
    le = _ledger()
    assert _clamp(_LegacyAdapter(order), le) is None
    assert le["scale_limit_order_id"] == "scale-1"


@pytest.mark.parametrize("status", ["new", "open", "pending_cancel", "partially_filled", "", "unknown", "new_broker_status"])
@pytest.mark.parametrize("factory", [_LegacyAdapter, _strict])
def test_open_and_unknown_states_cannot_release(status, factory, sinks):
    le = _ledger()
    assert _clamp(factory(_order(status=status, filled=125.0)), le) is None
    assert le["scale_limit_order_id"] == "scale-1"
    assert le["scale_limit_adopted_qty"] == 0.0
    assert sinks == []  # Like Alpaca, adoption waits for final cumulative truth.


@pytest.mark.parametrize("status", ["filled", "done", "closed", "cancelled", "canceled", "expired", "failed", "rejected", "voided"])
def test_existing_terminal_statuses_release_the_true_remainder(status, sinks):
    le = _ledger(adopted=100.0)
    assert _clamp(_LegacyAdapter(_order(status=status, filled=300.0)), le) == 700.0
    assert le["position"]["quantity"] == 700.0
    assert le["scale_limit_adopted_qty"] == 300.0
    assert sinks[0]["quantity"] == 200.0
    assert "scale_limit_order_id" not in le


@pytest.mark.parametrize("filled", [None, float("nan"), float("inf"), -1.0, 99.0])
def test_unreadable_or_regressed_fill_count_cannot_release(filled):
    le = _ledger(adopted=100.0)
    order = replace(_order(), filled_size=filled)
    assert _clamp(_strict(order), le) is None
    assert le["position"]["quantity"] == 900.0
    assert le["scale_limit_adopted_qty"] == 100.0
    assert le["scale_limit_order_id"] == "scale-1"


@pytest.mark.parametrize("factory", [_LegacyAdapter, _strict])
def test_lost_cancel_ack_can_resolve_from_terminal_truth(factory, sinks):
    le = _ledger()
    assert _clamp(factory(_order(filled=300.0), cancel_raises=True), le) == 700.0
    assert [fill["quantity"] for fill in sinks] == [300.0]
    assert "scale_limit_order_id" not in le


def test_pending_cancel_then_terminal_fill_adopts_once(sinks):
    le = _ledger(adopted=100.0)
    adapter = _strict(_order(status="pending_cancel", filled=300.0))
    assert _clamp(adapter, le) is None
    assert _clamp(adapter, le) is None
    assert sinks == []
    adapter.answer["order"] = _order(filled=400.0)
    assert _clamp(adapter, le) == 600.0
    assert "alpaca_scale_limit_release_block" not in le
    assert [fill["quantity"] for fill in sinks] == [300.0]
    # A repeated broker snapshot with its persisted watermark cannot rebook it.
    le["scale_limit_order_id"] = "scale-1"
    assert _clamp(adapter, le) == 600.0
    assert [fill["quantity"] for fill in sinks] == [300.0]


@pytest.mark.parametrize("requested, expected", [(200.0, 200.0), (1000.0, 700.0)])
def test_release_respects_requested_quantity_and_confirmed_remainder(requested, expected):
    le = _ledger()
    result = lr._cancel_scale_limit_and_clamp(
        None, _sess(), _strict(_order(filled=300.0)), le=le,
        requested_qty=requested, reason="stop",
    )
    assert result == expected
    assert le["position"]["quantity"] == 700.0


def test_receipt_failure_after_adoption_cannot_double_book_on_retry(monkeypatch, sinks):
    le = _ledger()
    adapter = _strict(_order(filled=300.0))

    def fail_receipt(*args, **kwargs):
        if args[2] == "scale_out_limit_cancelled":
            raise RuntimeError("receipt unavailable")

    monkeypatch.setattr(lr, "_emit", fail_receipt)
    assert _clamp(adapter, le) is None
    assert le["scale_limit_order_id"] == "scale-1"
    assert le["scale_limit_adopted_qty"] == 300.0
    monkeypatch.setattr(lr, "_emit", lambda *args, **kwargs: None)
    assert _clamp(adapter, le) == 700.0
    assert [fill["quantity"] for fill in sinks] == [300.0]


@pytest.mark.parametrize("family", ["robinhood_spot", "robinhood_agentic_mcp", "coinbase_spot"])
@pytest.mark.parametrize("answer", [
    {"readable": False},
    {"readable": True, "found": True, "order": _order(status="pending_cancel")},
])
def test_actual_whole_exit_seam_defers_without_placing(family, answer):
    le = _ledger()
    result = lr._submit_live_market_exit(
        None, _sess(family), _StrictAdapter(answer), le=le, product_id="BATL",
        quantity=1000.0, client_order_id="close-after-scale", reason="stop",
        bid=2.0, ask=2.01, mid=2.005,
    )
    assert result["deferred"] and result["pre_place_blocked"]
    assert result["error"] == "alpaca_scale_limit_release_unconfirmed"
    assert le["scale_limit_order_id"] == "scale-1"
    assert le["exit_submit_attempts"] == 0
    assert "exit_order_id" not in le
