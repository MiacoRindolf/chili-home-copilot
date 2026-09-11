"""A cancellation request never releases a still-working or unreadable tranche.

Exercise the real clamp and exit-submit seam using normalized broker orders;
only persistence, accounting sinks and scheduling are replaced. No broker I/O.
"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.venue.protocol import NormalizedOrder

_REAL_COMMIT_LE = lr._commit_le
_REAL_PARTIAL_EXIT = lr._apply_confirmed_live_partial_exit


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
    le = {
        "scale_limit_order_id": "scale-1", "scale_limit_qty": 500.0,
        "scale_limit_px": 2.3, "scale_limit_adopted_qty": adopted,
        "scale_limit_client_order_id": "chili_ml_sol_99_one",
        "position": {"quantity": 1000.0 - adopted, "avg_entry_price": 2.0},
    }
    if adopted:
        le["scale_limit_adopted_economics"] = {
            "order_id": "scale-1", "filled_quantity": adopted,
            "filled_notional": adopted * 2.3, "fees_usd": 0.0,
        }
    return le


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


@pytest.mark.parametrize("price", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_root_review_new_fill_requires_broker_price(price, sinks):
    le = _ledger()
    order = replace(_order(filled=300.0), average_filled_price=price)
    assert _clamp(_strict(order), le) is None
    assert le["position"]["quantity"] == 1000.0
    assert le["scale_limit_adopted_qty"] == 0.0
    assert le["scale_limit_order_id"] == "scale-1"
    assert sinks == []


@pytest.mark.parametrize("frozen_qty, held_qty, filled", [(500.0, 1000.0, 600.0), (None, 200.0, 300.0), (500.0, 200.0, 300.0)])
def test_root_review_oversized_fill_cannot_be_silently_clamped(frozen_qty, held_qty, filled, sinks):
    le = _ledger()
    le["position"]["quantity"] = held_qty
    if frozen_qty is None:
        le.pop("scale_limit_qty")
    else:
        le["scale_limit_qty"] = frozen_qty
    assert _clamp(_strict(_order(filled=filled)), le) is None
    assert le["position"]["quantity"] == held_qty
    assert le["scale_limit_adopted_qty"] == 0.0
    assert le["scale_limit_order_id"] == "scale-1"
    assert sinks == []


@pytest.mark.parametrize("changes", [
    {"product_id": "OTHER"}, {"side": "buy"}, {"client_order_id": "different-session"},
])
def test_root_review_contradictory_known_identity_cannot_book(changes, sinks):
    le = _ledger()
    order = replace(_order(filled=300.0), **changes)
    assert _clamp(_strict(order), le) is None
    assert le["position"]["quantity"] == 1000.0
    assert le["scale_limit_order_id"] == "scale-1"
    assert sinks == []


@pytest.mark.parametrize("price", [None, 0.0, float("nan")])
def test_zero_fill_terminal_requires_no_execution_price(price, sinks):
    le = _ledger()
    assert _clamp(_strict(replace(_order(), average_filled_price=price)), le) == 1000.0
    assert sinks == []


def test_rh_missing_client_id_is_not_a_reported_contradiction():
    le = _ledger()
    assert _clamp(_strict(replace(_order(filled=300.0), client_order_id=None)), le) == 700.0


def test_cumulative_price_and_fees_book_only_the_proven_increment(sinks):
    le = _ledger(adopted=100.0)
    le["scale_limit_adopted_economics"].update(filled_notional=220.0, fees_usd=1.0)
    le["fees_usd_total"] = 1.0
    le["realized_pnl_usd"] = 19.0
    order = replace(_order(filled=300.0), raw={"total_fees": 3.0})
    assert _clamp(_strict(order), le) == 700.0
    assert sinks[0]["quantity"] == 200.0
    assert sinks[0]["fill_price"] == pytest.approx(2.35)
    assert sinks[0]["fee"] == 2.0
    assert le["fees_usd_total"] == 3.0
    assert le["realized_pnl_usd"] == pytest.approx(87.0)
    assert le["scale_limit_adopted_economics"] == {
        "order_id": "scale-1", "filled_quantity": 300.0,
        "filled_notional": 690.0, "fees_usd": 3.0,
    }


def test_legacy_adopted_quantity_without_economics_stays_unresolved(sinks):
    le = _ledger(adopted=100.0)
    le.pop("scale_limit_adopted_economics")
    assert _clamp(_strict(_order(filled=300.0)), le) is None
    assert le["alpaca_scale_limit_release_block"]["detail"] == "prior_fill_economics_unproven"
    assert le["position"]["quantity"] == 900.0
    assert sinks == []


def test_fee_only_correction_is_explicitly_unresolved_not_a_fake_fill(sinks):
    le = _ledger(adopted=300.0)
    order = replace(_order(filled=300.0), raw={"total_fees": 3.0})
    assert _clamp(_strict(order), le) is None
    assert le["alpaca_scale_limit_release_block"]["detail"] == "economic_correction_requires_reconciliation"
    assert le["position"]["quantity"] == 700.0
    assert sinks == []


@pytest.mark.parametrize("leg_price", [None, 0.0, 2.1])
def test_oco_leg_requires_execution_average_not_its_stop_price(leg_price, sinks):
    le = _ledger()
    le["scale_limit_is_oco"] = True
    order = replace(_order(), raw={"legs": [{
        "filled_qty": 300.0, "filled_avg_price": leg_price, "stop_price": 2.1,
    }]})
    result = _clamp(_strict(order), le)
    if leg_price:
        assert result == 700.0
        assert sinks[0]["fill_price"] == leg_price
        assert sinks[0]["reason"] == "tranche_oco_stop_fill"
    else:
        assert result is None
        assert sinks == []


@pytest.mark.parametrize("failure_stage", ["before_mutation", "inner_receipt", "outer_receipt"])
def test_real_savepoint_keeps_accounting_and_watermarks_atomic(monkeypatch, failure_stage):
    """Temporary PostgreSQL rows expose flush-before-watermark retry failures."""
    import json
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from app.db import engine

    le = _ledger()
    sess = _sess()
    order = replace(_order(filled=300.0), raw={"total_fees": 3.0})
    adapter = _strict(order)
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text("CREATE TEMP TABLE scale_cancel_accounting (qty float8, px float8, fee float8)"))
        connection.execute(text("CREATE TEMP TABLE scale_cancel_state (snapshot jsonb)"))
        connection.execute(text("INSERT INTO scale_cancel_state VALUES ('{}')"))
        with Session(bind=connection) as db:
            def commit_state(session, ledger):
                _REAL_COMMIT_LE(session, ledger)
                db.execute(text("UPDATE scale_cancel_state SET snapshot=CAST(:s AS jsonb)"), {
                    "s": json.dumps(session.risk_snapshot_json),
                })

            def record_fill(*args, **kwargs):
                db.execute(text("INSERT INTO scale_cancel_accounting VALUES (:qty,:px,:fee)"), {
                    "qty": kwargs["quantity"], "px": kwargs["fill_price"], "fee": kwargs["fee"],
                })
                db.flush()

            def fail_before_mutation(*args, **kwargs):
                raise RuntimeError("before accounting")

            def emit(*args, **kwargs):
                event = args[2]
                if (failure_stage == "inner_receipt" and event == "live_partial_exit_filled") or (
                    failure_stage == "outer_receipt" and event == "scale_out_limit_cancelled"
                ):
                    raise RuntimeError("receipt unavailable")

            monkeypatch.setattr(lr, "_commit_le", commit_state)
            monkeypatch.setattr(lr, "_record_live_partial_exit_ledger_safe", record_fill)
            monkeypatch.setattr(lr, "_emit", emit)
            if failure_stage == "before_mutation":
                monkeypatch.setattr(lr, "_apply_confirmed_live_partial_exit", fail_before_mutation)
            commit_state(sess, le)

            def clamp():
                return lr._cancel_scale_limit_and_clamp(
                    db, sess, adapter, le=le, requested_qty=1000.0, reason="stop",
                )

            assert clamp() is None
            completed = failure_stage == "outer_receipt"
            assert le["position"]["quantity"] == (700.0 if completed else 1000.0)
            assert le["scale_limit_adopted_qty"] == (300.0 if completed else 0.0)
            persisted = db.execute(text("SELECT snapshot FROM scale_cancel_state")).scalar_one()[lr.KEY_LIVE_EXEC]
            assert persisted["position"]["quantity"] == le["position"]["quantity"]
            assert persisted["scale_limit_adopted_qty"] == le["scale_limit_adopted_qty"]
            assert db.execute(text("SELECT count(*) FROM scale_cancel_accounting")).scalar_one() == int(completed)
            monkeypatch.setattr(lr, "_apply_confirmed_live_partial_exit", _REAL_PARTIAL_EXIT)
            monkeypatch.setattr(lr, "_emit", lambda *args, **kwargs: None)
            assert clamp() == 700.0
            assert le["fees_usd_total"] == 3.0
            assert le["scale_limit_adopted_qty"] == 300.0
            assert le["scale_limit_adopted_economics"]["fees_usd"] == 3.0
            assert db.execute(text("SELECT qty,fee FROM scale_cancel_accounting")).all() == [(300.0, 3.0)]
        transaction.rollback()
