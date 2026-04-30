"""Offline self-test for the R23 bracket_writer_g2 changes.

Runs the same test surface as tests/test_bracket_writer_g2.py but
without the pytest db fixture, because the host has winsock buffer
exhaustion (error 10055) blocking even localhost Postgres connections.

The writer's audit-row helper (_g2_event) catches all exceptions
internally and fails closed, so calling it with a fake Session that
raises on .get() still lets the writer return a clean WriterAction.

Exit code: 0 on all passing, 1 on any failure.
"""
import os, sys, traceback
from types import SimpleNamespace
from unittest.mock import MagicMock

# Make repo root importable.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Force minimal env so settings load.
os.environ.setdefault("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")
os.environ.setdefault("CHILI_PYTEST", "1")

from app.services.trading import bracket_writer_g2 as g2  # noqa: E402
from app.services.trading.bracket_reconciler import ReconciliationDecision  # noqa: E402


# Fake session that raises on .get to exercise the audit-row error path.
class _FakeSession:
    def get(self, *a, **kw):
        raise RuntimeError("no_db_for_offline_test")
    def add(self, *a, **kw):
        pass
    def flush(self):
        pass


def _on_cfg(**overrides):
    base = dict(
        chili_bracket_writer_g2_enabled=True,
        chili_bracket_writer_g2_partial_fill_resize=True,
        chili_bracket_writer_g2_place_missing_stop=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _off_cfg():
    return SimpleNamespace(
        chili_bracket_writer_g2_enabled=False,
        chili_bracket_writer_g2_partial_fill_resize=False,
        chili_bracket_writer_g2_place_missing_stop=False,
    )


def _partial_fill_decision(expected_qty):
    return ReconciliationDecision(
        kind="qty_drift", severity="warn",
        delta_payload={
            "drift_kind": "partial_fill", "is_partial_fill": True,
            "expected_stop_qty": expected_qty,
            "local_qty": expected_qty * 2, "broker_qty": expected_qty,
            "abs_diff": expected_qty, "fill_ratio": 0.5,
        },
    )


def _missing_stop_decision():
    return ReconciliationDecision(
        kind="missing_stop", severity="warn",
        delta_payload={"intent_state": "shadow_logged"},
    )


# Patch settings at module level for each test.
def _set_settings(cfg):
    g2.settings = cfg


PASS, FAIL = 0, 0


def case(name):
    def deco(fn):
        global PASS, FAIL
        try:
            fn()
            print(f"PASS {name}")
            PASS += 1
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            FAIL += 1
        except Exception:
            print(f"FAIL {name}: UNEXPECTED")
            traceback.print_exc()
            FAIL += 1
        return fn
    return deco


db = _FakeSession()


@case("default_off blocks resize, never calls adapter")
def t1():
    _set_settings(_off_cfg())
    factory = MagicMock()
    r = g2.resize_stop_for_partial_fill(
        db, trade_id=1, bracket_intent_id=1, ticker="OFF1",
        broker_source="robinhood", decision=_partial_fill_decision(5.0),
        prior_stop_order_id="stop-1", stop_price=95.0,
        adapter_factory=factory,
    )
    assert r.ok is False
    assert r.reason == "disabled"
    factory.assert_not_called()


@case("default_off blocks place, never calls adapter")
def t2():
    _set_settings(_off_cfg())
    factory = MagicMock()
    r = g2.place_missing_stop(
        db, trade_id=1, bracket_intent_id=1, ticker="OFF2",
        broker_source="robinhood", decision=_missing_stop_decision(),
        local_quantity=5.0, stop_price=95.0,
        adapter_factory=factory,
    )
    assert r.ok is False
    assert r.reason == "disabled"
    factory.assert_not_called()


@case("place_missing_stop happy path uses place_stop_loss_sell_order")
def t3():
    _set_settings(_on_cfg())
    adapter = MagicMock()
    adapter.place_stop_loss_sell_order.return_value = {
        "ok": True, "order_id": "new-stop-m", "raw": {}
    }
    factory = MagicMock(return_value=adapter)
    r = g2.place_missing_stop(
        db, trade_id=55, bracket_intent_id=9, ticker="MSP",
        broker_source="robinhood", decision=_missing_stop_decision(),
        local_quantity=10.0, stop_price=91.0,
        adapter_factory=factory,
    )
    assert r.ok is True, f"expected ok, got reason={r.reason}"
    adapter.cancel_order.assert_not_called()
    adapter.place_stop_loss_sell_order.assert_called_once()
    kw = adapter.place_stop_loss_sell_order.call_args.kwargs
    assert "trigger_price" in kw, f"expected trigger_price in kwargs: {kw}"
    assert "limit_price" not in kw, f"unexpected limit_price kwarg: {kw}"
    assert "client_order_id" in kw
    # Round 23 coid format: g2-miss-{intent}-{qty*1e6}-{epoch_seconds}
    coid = kw["client_order_id"]
    assert coid.startswith("g2-miss-9-"), f"coid={coid}"
    assert coid.count("-") >= 4, f"coid should include epoch suffix: {coid}"


@case("resize_stop happy path: cancel before place, uses real stop primitive")
def t4():
    _set_settings(_on_cfg())
    adapter = MagicMock()
    adapter.cancel_order.return_value = {"ok": True, "raw": {}}
    adapter.place_stop_loss_sell_order.return_value = {
        "ok": True, "order_id": "new-stop-1", "raw": {}
    }
    factory = MagicMock(return_value=adapter)
    r = g2.resize_stop_for_partial_fill(
        db, trade_id=42, bracket_intent_id=7, ticker="RSZ",
        broker_source="robinhood", decision=_partial_fill_decision(5.0),
        prior_stop_order_id="old-stop-7", stop_price=92.5,
        adapter_factory=factory,
    )
    assert r.ok is True, f"reason={r.reason}"
    calls = adapter.method_calls
    assert calls[0][0] == "cancel_order", f"first call={calls[0][0]}"
    assert calls[1][0] == "place_stop_loss_sell_order"
    place_kw = calls[1][2]
    assert "trigger_price" in place_kw
    assert "limit_price" not in place_kw


@case("resize cancel-fail does not place")
def t5():
    _set_settings(_on_cfg())
    adapter = MagicMock()
    adapter.cancel_order.return_value = {"ok": False, "error": "not_cancellable"}
    factory = MagicMock(return_value=adapter)
    r = g2.resize_stop_for_partial_fill(
        db, trade_id=1, bracket_intent_id=1, ticker="CFX",
        broker_source="robinhood", decision=_partial_fill_decision(5.0),
        prior_stop_order_id="stop-x", stop_price=92.5,
        adapter_factory=factory,
    )
    assert r.ok is False
    assert r.reason == "cancel_failed"
    adapter.place_stop_loss_sell_order.assert_not_called()


@case("resize cancel-OK + place-FAIL = unprotected window CRITICAL")
def t6():
    _set_settings(_on_cfg())
    adapter = MagicMock()
    adapter.cancel_order.return_value = {"ok": True, "raw": {}}
    adapter.place_stop_loss_sell_order.return_value = {
        "ok": False, "error": "market_closed"
    }
    factory = MagicMock(return_value=adapter)
    r = g2.resize_stop_for_partial_fill(
        db, trade_id=1, bracket_intent_id=1, ticker="DANGER",
        broker_source="robinhood", decision=_partial_fill_decision(5.0),
        prior_stop_order_id="stop-danger", stop_price=92.5,
        adapter_factory=factory,
    )
    assert r.ok is False
    assert r.reason == "place_failed"


@case("place rejects unsupported venue (coinbase)")
def t7():
    _set_settings(_on_cfg())
    factory = MagicMock()
    r = g2.place_missing_stop(
        db, trade_id=1, bracket_intent_id=1, ticker="BTC-USD",
        broker_source="coinbase", decision=_missing_stop_decision(),
        local_quantity=0.1, stop_price=50_000.0,
        adapter_factory=factory,
    )
    assert r.ok is False
    assert r.reason == "unsupported_venue"
    factory.assert_not_called()


@case("place rejects wrong decision kind")
def t8():
    _set_settings(_on_cfg())
    factory = MagicMock()
    r = g2.place_missing_stop(
        db, trade_id=1, bracket_intent_id=1, ticker="T",
        broker_source="robinhood",
        decision=ReconciliationDecision(kind="agree", severity="info"),
        local_quantity=5.0, stop_price=95.0,
        adapter_factory=factory,
    )
    assert r.ok is False
    assert r.reason == "invalid_decision"
    factory.assert_not_called()


@case("sweep service _invoke_writer_for_decision present + mode-gated")
def t9():
    from app.services.trading import bracket_reconciliation_service as svc
    from app.services.trading.bracket_reconciler import LocalView, BrokerView
    assert hasattr(svc, "_invoke_writer_for_decision")
    # mode!=authoritative -> None
    r = svc._invoke_writer_for_decision(
        db, mode="shadow", sweep_id="s",
        local=LocalView(
            trade_id=1, bracket_intent_id=1, ticker="X",
            direction="long", quantity=10.0, intent_state="intent",
            stop_price=95.0, target_price=None,
            broker_source="robinhood", trade_status="open",
        ),
        broker=BrokerView(available=True, ticker="X", broker_source="robinhood"),
        decision=_missing_stop_decision(),
    )
    assert r is None, f"shadow mode should produce None, got {r}"


@case("config flag chili_bracket_sweep_writer_enabled present + default False")
def t10():
    from app.config import settings as live_settings
    assert hasattr(live_settings, "chili_bracket_sweep_writer_enabled")
    assert live_settings.chili_bracket_sweep_writer_enabled is False, \
        f"default should be False, got {live_settings.chili_bracket_sweep_writer_enabled}"


@case("mig 214 registered after 213")
def t11():
    from app import migrations as M
    ids = [m[0] for m in M.MIGRATIONS]
    assert "213_demote_negative_ev_promoted_patterns" in ids
    assert "214_trade_table_check_constraints" in ids
    i213 = ids.index("213_demote_negative_ev_promoted_patterns")
    i214 = ids.index("214_trade_table_check_constraints")
    assert i214 == i213 + 1, f"214 should immediately follow 213, got idx {i213} vs {i214}"


@case("broker_service.place_sell_stop_loss_order present with stop semantics")
def t12():
    from app.services import broker_service
    import inspect
    assert hasattr(broker_service, "place_sell_stop_loss_order")
    sig = inspect.signature(broker_service.place_sell_stop_loss_order)
    assert "trigger_price" in sig.parameters
    src = inspect.getsource(broker_service.place_sell_stop_loss_order)
    # Must use the broker stop-order primitive, not a marketable limit.
    assert "stopPrice" in src and "trigger=\"stop\"" in src, \
        "place_sell_stop_loss_order does not call rh.orders.order with stopPrice + trigger='stop'"


print()
print(f"=== summary: {PASS} passed, {FAIL} failed ===")
sys.exit(0 if FAIL == 0 else 1)
