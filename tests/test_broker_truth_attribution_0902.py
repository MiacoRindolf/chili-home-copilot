"""Broker-truth ATTRIBUTION for Alpaca momentum outcomes (2026-09-02, CANF 19471).

THE GAP. Outcome 203734 (session 19471) was stamped `reconciled` at −78.13 from
its FSM ledger (momentum_fill_outcomes: entry 552efe43 355 @ 4.34, exit af3a4b0c
355 @ 4.119915) while the broker filled TWO cycles for that session: the FSM's own
cycle-2 entry f3ed508d (165 @ 4.62, `chili_ml_e_19471_…`, never adopted → no
ledger leg) and the operator's sell ddba3ed2 (165 @ 3.960303,
`chili_ops_flat_19471_…`, placed outside the FSM → booked only as an UNPRICED
emergency leg in le["emergency_exit_accounting_pending"]). Session broker truth is
−186.98; the loss guard (risk_policy.load_current_live_loss_history reads
broker_realized_pnl_usd) undercounted the day by −108.85.

THE FIX. For Alpaca families the reconcile pass lists the symbol's broker orders
inside the session window (read-only GET via AlpacaSpotAdapter.list_symbol_orders_truth)
and attributes every filled order whose client_order_id carries the session id, plus
any closing fill that uniquely matches an unpriced emergency leg. broker_* then
covers the WHOLE session; broker_divergence_usd = broker − lane self-report.

DB-free: fakes for the ledger query and the broker listing; AST guards prove the
new read path never places/cancels/replaces an order.

Runnable: pytest tests/test_broker_truth_attribution_0902.py -v
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import outcome_reconcile as orc
from app.services.trading.momentum_neural import risk_policy as rp
from app.services.trading.venue import alpaca_spot
from app.services.trading.venue.protocol import NormalizedOrder

SID = 19471


# ── fixtures: the REAL broker listing for CANF on 2026-09-02 (from the adapter) ──
def _o(oid, cid, side, status, filled, px, filled_at, order_type="limit"):
    return NormalizedOrder(
        order_id=oid,
        client_order_id=cid,
        product_id="CANF",
        side=side,
        status=status,
        order_type=order_type,
        filled_size=float(filled),
        average_filled_price=(float(px) if px is not None else None),
        created_time=filled_at,
        raw={"filled_at": filled_at, "submitted_at": filled_at},
    )


def _canf_orders():
    return [
        _o("552efe43-395a-4f76-836a-7be3d30a8689", "chili_ml_e_19471_a7c3e32c_9523138f65", "buy", "filled",
           355, 4.34, "2026-09-02 11:10:19.535053+00:00"),
        _o("dcbc5722-c5a9-4676-ae2d-94eb9f4f740c", "chili_dm_19471_1_ce039811d2", "sell", "canceled",
           0, None, None, order_type="stop"),
        _o("af3a4b0c-d7fc-4379-8b25-ddc50ab82a31", "chili_ml_s_19471_41bee9bdc9a9", "sell", "filled",
           355, 4.119915, "2026-09-02 11:11:05.383316+00:00"),
        _o("f3ed508d-e441-47b3-b76b-2449b6f0a133", "chili_ml_e_19471_a7c3e32c_9738f4d973", "buy", "filled",
           165, 4.62, "2026-09-02 11:19:12.223684+00:00"),
        _o("ddba3ed2-6854-4c34-92be-7efdd1926fa4", "chili_ops_flat_19471_d8394610fc", "sell", "filled",
           165, 3.960303, "2026-09-02 11:34:31.991961+00:00"),
    ]


_UNPRICED_LEG = {
    "note": "broker_zero_without_exact_exit_fill", "reason": "operator_flatten", "fee_usd": 0.0,
    "quantity": 165.0, "attempt_no": 1, "fill_price": None, "order_status": None,
    "broker_order_id": None, "client_order_id": None, "recorded_at_utc": "2026-09-02T13:20:19.538227",
}

W_START = datetime(2026, 9, 2, 11, 1, 22)
W_END = datetime(2026, 9, 2, 13, 56, 8)

BROKER_PNL = 355 * (4.119915 - 4.34) + 165 * (3.960303 - 4.62)  # −186.98
BROKER_NOTIONAL = 355 * 4.34 + 165 * 4.62  # 2303.00


# ── (1) cid ownership ────────────────────────────────────────────────────────
@pytest.mark.parametrize("cid,sid", [
    ("chili_ml_e_19471_a7c3e32c_9738f4d973", 19471),
    ("chili_ml_s_19471_41bee9bdc9a9", 19471),
    ("chili_dm_19471_1_ce039811d2", 19471),
    ("chili_ml_bw_19471_0123456789ab", 19471),
    ("chili_ops_flat_19471_d8394610fc", 19471),
    ("chili_ml_e_19457_deadbeef_0123456789", 19457),
    ("8f3c2a1e-broker-generated", None),
    ("", None),
    (None, None),
    ("chili_19471_x", None),  # no alphabetic prefix token before the id
])
def test_session_id_from_client_order_id(cid, sid):
    assert orc.session_id_from_client_order_id(cid) == sid


# ── (2) the CANF 19471 replica attributes BOTH cycles ────────────────────────
def test_canf_19471_attribution_covers_both_cycles():
    a = orc.attribute_session_broker_orders(
        session_id=SID, symbol="CANF", side_long=True, orders=_canf_orders(),
        unpriced_legs=[_UNPRICED_LEG], window_start=W_START, window_end=W_END,
        ledger_order_ids={"552efe43-395a-4f76-836a-7be3d30a8689", "af3a4b0c-d7fc-4379-8b25-ddc50ab82a31"},
    )
    assert a["attr_status"] == orc.ATTR_FLAT
    assert a["opening_orders"] == 2 and a["closing_orders"] == 2
    assert a["open_qty"] == pytest.approx(520.0) and a["close_qty"] == pytest.approx(520.0)
    assert a["broker_pnl_usd"] == pytest.approx(BROKER_PNL, abs=1e-6)
    assert a["open_notional_usd"] == pytest.approx(BROKER_NOTIONAL, abs=1e-6)
    assert set(a["legs_missing_from_ledger"]) == {
        "f3ed508d-e441-47b3-b76b-2449b6f0a133", "ddba3ed2-6854-4c34-92be-7efdd1926fa4",
    }
    # the unfilled deadman stop carries no economics and is not a leg
    assert all(l["broker_order_id"] != "dcbc5722-c5a9-4676-ae2d-94eb9f4f740c" for l in a["legs"])
    # the operator sell is attributed by its session cid, not by the unpriced-leg fallback
    ops = [l for l in a["legs"] if l["client_order_id"].startswith("chili_ops_flat_")]
    assert ops and ops[0]["attribution"] == "session_cid"


def test_foreign_session_cid_is_never_attributed():
    orders = _canf_orders() + [
        _o("aaaa-foreign", "chili_ml_e_19457_deadbeef_0123456789", "buy", "filled", 100, 4.00,
           "2026-09-02 11:20:00+00:00"),
    ]
    a = orc.attribute_session_broker_orders(
        session_id=SID, symbol="CANF", side_long=True, orders=orders, unpriced_legs=[_UNPRICED_LEG],
        window_start=W_START, window_end=W_END,
    )
    assert a["attr_status"] == orc.ATTR_FLAT
    assert a["foreign_session_orders_ignored"] == 1
    assert a["open_qty"] == pytest.approx(520.0)


def test_owned_fill_outside_window_is_recorded_but_not_counted():
    orders = _canf_orders() + [
        _o("bbbb-late", "chili_ml_e_19471_zz_late", "buy", "filled", 10, 4.00, "2026-09-03 11:20:00+00:00"),
    ]
    a = orc.attribute_session_broker_orders(
        session_id=SID, symbol="CANF", side_long=True, orders=orders, unpriced_legs=[_UNPRICED_LEG],
        window_start=W_START, window_end=W_END,
    )
    late = [l for l in a["legs"] if l["broker_order_id"] == "bbbb-late"]
    assert late and late[0].get("note") == "owned_cid_outside_window" and "attribution" not in late[0]
    assert a["attr_status"] == orc.ATTR_FLAT


# ── (3) unpriced-leg fallback: unique match vs ambiguity ─────────────────────
def _ui_sell(oid, qty, px, at):
    # a sell placed from the Alpaca UI: broker-generated cid
    return _o(oid, "e9a1c2d3-ui-generated", "sell", "filled", qty, px, at)


def test_unpriced_leg_matches_a_unique_non_owned_sell():
    orders = [o for o in _canf_orders() if not o.client_order_id.startswith("chili_ops_flat_")]
    orders.append(_ui_sell("cccc-ui", 165, 3.96, "2026-09-02 11:34:31+00:00"))
    a = orc.attribute_session_broker_orders(
        session_id=SID, symbol="CANF", side_long=True, orders=orders, unpriced_legs=[_UNPRICED_LEG],
        window_start=W_START, window_end=W_END,
    )
    assert a["attr_status"] == orc.ATTR_FLAT
    m = [l for l in a["legs"] if l["broker_order_id"] == "cccc-ui"]
    assert m and m[0]["attribution"] == "unpriced_emergency_leg_match"
    assert a["broker_pnl_usd"] == pytest.approx(355 * (4.119915 - 4.34) + 165 * (3.96 - 4.62), abs=1e-6)


def test_two_candidate_sells_for_one_unpriced_leg_is_ambiguous_never_guessed():
    orders = [o for o in _canf_orders() if not o.client_order_id.startswith("chili_ops_flat_")]
    orders.append(_ui_sell("cccc-ui-1", 165, 3.96, "2026-09-02 11:34:31+00:00"))
    orders.append(_ui_sell("cccc-ui-2", 165, 3.95, "2026-09-02 11:35:31+00:00"))
    a = orc.attribute_session_broker_orders(
        session_id=SID, symbol="CANF", side_long=True, orders=orders, unpriced_legs=[_UNPRICED_LEG],
        window_start=W_START, window_end=W_END,
    )
    assert a["attr_status"] == orc.ATTR_AMBIGUOUS


def test_non_owned_sell_after_the_unpriced_leg_was_recorded_does_not_match():
    orders = [o for o in _canf_orders() if not o.client_order_id.startswith("chili_ops_flat_")]
    orders.append(_ui_sell("cccc-ui-late", 165, 3.96, "2026-09-02 13:40:00+00:00"))  # after 13:20:19 + 120s
    a = orc.attribute_session_broker_orders(
        session_id=SID, symbol="CANF", side_long=True, orders=orders, unpriced_legs=[_UNPRICED_LEG],
        window_start=W_START, window_end=W_END,
    )
    assert a["attr_status"] == orc.ATTR_RESIDUAL_OPEN  # 520 bought, 355 sold visible
    assert a["broker_pnl_usd"] is None or a["open_qty"] > a["close_qty"]


def test_residual_and_oversold_fail_closed():
    buys_only = [o for o in _canf_orders() if o.side == "buy"]
    a = orc.attribute_session_broker_orders(
        session_id=SID, symbol="CANF", side_long=True, orders=buys_only, window_start=W_START, window_end=W_END,
    )
    assert a["attr_status"] == orc.ATTR_RESIDUAL_OPEN
    over = _canf_orders() + [
        _o("dddd-extra", "chili_ml_s_19471_extra", "sell", "filled", 5, 4.0, "2026-09-02 11:40:00+00:00"),
    ]
    a2 = orc.attribute_session_broker_orders(
        session_id=SID, symbol="CANF", side_long=True, orders=over, unpriced_legs=[_UNPRICED_LEG],
        window_start=W_START, window_end=W_END,
    )
    assert a2["attr_status"] == orc.ATTR_OVERSOLD


def test_short_session_pnl_is_sign_symmetric():
    orders = [
        _o("s1", "chili_ml_e_5_ab_cd", "sell", "filled", 100, 10.0, "2026-09-02 11:00:00+00:00"),
        _o("s2", "chili_ml_s_5_ab_cd", "buy", "filled", 100, 9.0, "2026-09-02 11:05:00+00:00"),
    ]
    a = orc.attribute_session_broker_orders(session_id=5, symbol="CANF", side_long=False, orders=orders)
    assert a["attr_status"] == orc.ATTR_FLAT
    assert a["broker_pnl_usd"] == pytest.approx(100.0)
    assert a["open_notional_usd"] == pytest.approx(1000.0)


# ── (4) reconcile_one_outcome end-to-end with fakes ──────────────────────────
class _Res:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def scalar(self):
        return self._rows[0][0] if self._rows else None

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDb:
    """Serves the two ledger queries; anything else is a test failure."""

    def __init__(self, ledger_rows):
        self.ledger_rows = ledger_rows
        self.sql = []

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.sql.append(sql)
        if "broker_order_id IS NOT NULL" in sql:
            return _Res([(r[10],) for r in self.ledger_rows if r[10]])
        if "FROM momentum_fill_outcomes" in sql:
            return _Res([r[:10] for r in self.ledger_rows])
        raise AssertionError(f"unexpected SQL in DB-free test: {sql}")


# (side, leg_seq, fill_source, broker_fill_price, qty, fees_usd, settled_pnl, settled_fees, realized_pnl, entry_price, broker_order_id)
_LEDGER_19471 = [
    ("entry", 0, "broker_confirmed", 4.34, 355.0, 0.0, None, None, None, None, "552efe43-395a-4f76-836a-7be3d30a8689"),
    ("exit", 0, "broker_confirmed", 4.119915, 355.0, 0.0, None, None, -78.130175, 4.34, "af3a4b0c-d7fc-4379-8b25-ddc50ab82a31"),
]


def _outcome_and_session(*, recon_status=None, detail=None, le_extra=None, family="alpaca_spot"):
    le = {
        "trade_cycles": 1, "realized_pnl_usd": -78.130175,
        "entry_order_id": "f3ed508d-e441-47b3-b76b-2449b6f0a133",
        "entry_order_ids_all": ["f3ed508d-e441-47b3-b76b-2449b6f0a133"],
        "entry_orders_resolved": {"f3ed508d-e441-47b3-b76b-2449b6f0a133": "adopted"},
        "position": None,
        "emergency_exit_accounting_pending": {"status": "pending_cost_basis", "legs": [_UNPRICED_LEG],
                                              "unpriced_quantity": 165.0, "remaining_quantity": 0.0},
    }
    le.update(le_extra or {})
    sess = SimpleNamespace(
        id=SID, symbol="CANF", execution_family=family, mode="live", state="live_finished",
        started_at=datetime(2026, 9, 2, 11, 3, 22, 350827), ended_at=datetime(2026, 9, 2, 13, 41, 8, 405191),
        risk_snapshot_json={"momentum_live_execution": le},
    )
    outcome = SimpleNamespace(
        id=203734, session_id=SID, symbol="CANF", mode="live", execution_family=family,
        terminal_at=datetime(2026, 9, 2, 13, 41, 8, 405191), realized_pnl_usd=-78.130175, return_bps=-507.108,
        broker_recon_status=recon_status, broker_realized_pnl_usd=None, broker_return_bps=None,
        broker_notional_basis_usd=None, broker_win=None, broker_divergence_usd=None,
        broker_reconciled_at=None, broker_recon_detail_json=detail,
    )
    return outcome, sess


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_enabled", True, raising=False)
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_grace_seconds", 900, raising=False)
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_max_per_pass", 20, raising=False)


def _reader_ok(symbol, after, until):
    assert symbol == "CANF"
    assert after < datetime(2026, 9, 2, 11, 3, 22) and until > datetime(2026, 9, 2, 13, 41, 8)
    return {"readable": True, "orders": _canf_orders(), "truncated": False}


def test_reconcile_one_outcome_stamps_whole_session_broker_truth():
    o, s = _outcome_and_session()
    db = _FakeDb(_LEDGER_19471)
    detail = orc.reconcile_one_outcome(db, o, s, broker_orders_reader=_reader_ok)
    assert o.broker_recon_status == orc.STATUS_RECONCILED
    assert o.broker_realized_pnl_usd == pytest.approx(BROKER_PNL, abs=1e-6)
    assert o.broker_notional_basis_usd == pytest.approx(BROKER_NOTIONAL, abs=1e-6)
    assert o.broker_return_bps == pytest.approx(BROKER_PNL / BROKER_NOTIONAL * 1e4, abs=1e-6)
    assert o.broker_win is False
    # divergence = broker − lane self-report = the unpriced cycle-2 loss
    assert o.broker_divergence_usd == pytest.approx(-108.850005, abs=1e-6)
    assert isinstance(o.broker_reconciled_at, datetime)
    assert detail["attribution_version"] == orc.ATTRIBUTION_VERSION
    assert detail["source"] == "broker_orders_attributed"
    ba = detail["broker_attribution"]
    assert ba["attr_status"] == orc.ATTR_FLAT
    assert ba["ledger_pnl_usd"] == pytest.approx(-78.130175, abs=1e-6)
    assert set(ba["legs_missing_from_ledger"]) == {
        "f3ed508d-e441-47b3-b76b-2449b6f0a133", "ddba3ed2-6854-4c34-92be-7efdd1926fa4",
    }
    # legacy fields untouched
    assert o.realized_pnl_usd == pytest.approx(-78.130175)


def test_the_loss_guard_consumes_the_whole_session_number():
    """risk_policy._alpaca_loss_history_broker_truth admits the attributed row and
    load_current_live_loss_history carries broker_realized_pnl_usd (risk_policy:1846)
    into the day ledger — so the day's loss now includes cycle 2."""
    o, s = _outcome_and_session()
    orc.reconcile_one_outcome(_FakeDb(_LEDGER_19471), o, s, broker_orders_reader=_reader_ok)
    assert rp._alpaca_loss_history_broker_truth(o) is True
    assert o.broker_realized_pnl_usd < -180.0


def test_unreadable_broker_with_unpriced_evidence_does_not_certify_the_ledger_label():
    o, s = _outcome_and_session()

    def _down(symbol, after, until):
        return {"readable": False, "orders": [], "error": {"http_status": 503}}

    orc.reconcile_one_outcome(_FakeDb(_LEDGER_19471), o, s, broker_orders_reader=_down)
    assert o.broker_recon_status == orc.STATUS_BROKER_UNAVAILABLE  # retried next pass
    assert o.broker_realized_pnl_usd is None
    assert "attribution_version" not in o.broker_recon_detail_json


def test_unreadable_broker_without_evidence_keeps_the_ledger_verdict_for_retry():
    o, s = _outcome_and_session(le_extra={"emergency_exit_accounting_pending": None})

    def _down(symbol, after, until):
        return {"readable": False, "orders": [], "error": "adapter_disabled"}

    orc.reconcile_one_outcome(_FakeDb(_LEDGER_19471), o, s, broker_orders_reader=_down)
    assert o.broker_recon_status == orc.STATUS_RECONCILED
    assert o.broker_realized_pnl_usd == pytest.approx(-78.130175, abs=1e-6)
    assert "attribution_version" not in o.broker_recon_detail_json  # → needs_reconcile stays True


def test_residual_open_at_the_broker_fails_closed():
    o, s = _outcome_and_session()

    def _buys_only(symbol, after, until):
        return {"readable": True, "orders": [x for x in _canf_orders() if x.side == "buy"], "truncated": False}

    orc.reconcile_one_outcome(_FakeDb(_LEDGER_19471), o, s, broker_orders_reader=_buys_only)
    assert o.broker_recon_status == orc.STATUS_RESIDUAL_OPEN
    assert o.broker_realized_pnl_usd is None
    assert rp._alpaca_loss_history_broker_truth(o) is False


def test_non_alpaca_family_is_byte_identical_ledger_path():
    o, s = _outcome_and_session(family="robinhood_spot")
    calls = []

    def _never(symbol, after, until):
        calls.append(symbol)
        return {"readable": True, "orders": _canf_orders()}

    orc.reconcile_one_outcome(_FakeDb(_LEDGER_19471), o, s, broker_orders_reader=_never)
    assert calls == []
    assert o.broker_recon_status == orc.STATUS_RECONCILED
    assert o.broker_realized_pnl_usd == pytest.approx(-78.130175, abs=1e-6)
    assert "broker_attribution" not in o.broker_recon_detail_json


def test_flag_off_is_byte_identical_ledger_path(monkeypatch):
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_enabled", False, raising=False)
    o, s = _outcome_and_session()
    orc.reconcile_one_outcome(_FakeDb(_LEDGER_19471), o, s, broker_orders_reader=_reader_ok)
    assert o.broker_realized_pnl_usd == pytest.approx(-78.130175, abs=1e-6)
    assert "broker_attribution" not in o.broker_recon_detail_json


# ── (5) batch admission: the already-`reconciled` 203734 row is re-touched ONCE ──
def test_needs_reconcile_upgrades_pre_attribution_alpaca_rows_once():
    o, s = _outcome_and_session(recon_status="reconciled", detail={"status": "reconciled", "source": "ledger_confirmed"})
    assert orc.needs_reconcile(o, s) is True  # the 203734 shape: terminal but never attributed
    o2, s2 = _outcome_and_session(recon_status="reconciled",
                                  detail={"status": "reconciled", "attribution_version": orc.ATTRIBUTION_VERSION})
    assert orc.needs_reconcile(o2, s2) is False  # attributed → immutable
    o3, s3 = _outcome_and_session(recon_status="reconciled", detail={"status": "reconciled"}, family="robinhood_spot")
    assert orc.needs_reconcile(o3, s3) is False  # non-Alpaca terminal rows untouched (legacy behavior)
    o4, s4 = _outcome_and_session(recon_status=None)
    assert orc.needs_reconcile(o4, s4) is True


def test_batch_pass_honors_the_broker_read_budget(monkeypatch):
    monkeypatch.setattr(orc.settings, "chili_momentum_broker_truth_reconciliation_enabled", True, raising=False)
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_max_per_pass", 1, raising=False)
    rows = [_outcome_and_session(), _outcome_and_session()]

    class _Q:
        def join(self, *a, **k): return self
        def filter(self, *a, **k): return self
        def all(self): return rows

    class _Db:
        def query(self, *a, **k): return _Q()
        def commit(self): pass
        def rollback(self): pass

    seen = []

    def _one(db, outcome, sess, **kw):
        seen.append(outcome.id)
        outcome.broker_recon_status = "reconciled"
        outcome.broker_realized_pnl_usd = -1.0
        return {"status": "reconciled"}

    monkeypatch.setattr(orc, "reconcile_one_outcome", _one)
    out = orc.reconcile_momentum_outcomes_to_broker_truth(_Db(), lookback_days=1.0, day_net_advisory=False)
    assert out["ok"] is True
    assert out["broker_reads"] == 1 and out["skipped_broker_budget"] == 1 and out["written"] == 1


# ── (6) AST guards: the new broker read path is READ-ONLY ────────────────────
# Exact names of every order-mutating call on the Alpaca SDK client / app adapter.
_MUTATING = frozenset({
    "submit_order", "cancel_order", "cancel_orders", "cancel_order_by_id", "cancel_all_orders",
    "replace_order", "replace_order_by_id", "replace_order_qty", "close_position",
    "close_all_positions", "place_market_order", "place_limit_order_gtc", "preview_market_order",
    "post", "put", "delete", "patch",
})


def _calls_in(src: str) -> set:
    tree = ast.parse(textwrap.dedent(src))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                names.add(f.attr)
            elif isinstance(f, ast.Name):
                names.add(f.id)
    return names


def test_adapter_listing_is_read_only_status_all_nested():
    src = inspect.getsource(alpaca_spot.AlpacaSpotAdapter.list_symbol_orders_truth)
    calls = _calls_in(src)
    assert "get_orders" in calls
    assert not (calls & _MUTATING), calls & _MUTATING
    assert "QueryOrderStatus.ALL" in src and "nested=True" in src
    assert "readable" in src


def test_reconciler_module_never_places_or_cancels():
    src = inspect.getsource(orc)
    calls = _calls_in(src)
    assert not (calls & _MUTATING), calls & _MUTATING
    assert "list_symbol_orders_truth" in calls
    # the default reader goes through the app adapter only
    assert "AlpacaSpotAdapter" in src and "_account_client" not in src


def test_settings_ship_on():
    from app.config import Settings

    s = Settings()
    assert s.chili_momentum_outcome_recon_broker_attribution_enabled is True
    assert s.chili_momentum_outcome_recon_broker_attribution_max_per_pass == 20
    assert s.chili_momentum_outcome_recon_broker_attribution_grace_seconds == 900
