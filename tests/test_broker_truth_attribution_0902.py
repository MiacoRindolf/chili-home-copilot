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
from datetime import datetime, timedelta
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
    """Serves the ledger queries + the cross-outcome collision probe; anything
    else is a test failure."""

    def __init__(self, ledger_rows, *, collisions=None, probe_raises=False):
        self.ledger_rows = ledger_rows
        self.collisions = collisions or []      # [(other_session_id, broker_order_id)]
        self.probe_raises = probe_raises
        self.sql = []

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.sql.append(sql)
        if "broker_order_id IS NOT NULL" in sql:
            return _Res([(r[10], r[11]) for r in self.ledger_rows if r[10]])
        if "FROM momentum_fill_outcomes" in sql:
            return _Res([r[:10] for r in self.ledger_rows])
        if "momentum_automation_outcomes" in sql:
            if self.probe_raises:
                raise RuntimeError("probe unreadable")
            return _Res(list(self.collisions))
        raise AssertionError(f"unexpected SQL in DB-free test: {sql}")


_ENTRY_FILL_TS = datetime(2026, 9, 2, 11, 10, 19)
_EXIT_FILL_TS = datetime(2026, 9, 2, 11, 11, 5)
# (side, leg_seq, fill_source, broker_fill_price, qty, fees_usd, settled_pnl, settled_fees, realized_pnl, entry_price, broker_order_id, fill_ts)
_LEDGER_19471 = [
    ("entry", 0, "broker_confirmed", 4.34, 355.0, 0.0, None, None, None, None,
     "552efe43-395a-4f76-836a-7be3d30a8689", _ENTRY_FILL_TS),
    ("exit", 0, "broker_confirmed", 4.119915, 355.0, 0.0, None, None, -78.130175, 4.34,
     "af3a4b0c-d7fc-4379-8b25-ddc50ab82a31", _EXIT_FILL_TS),
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

    def _no_closing_sell(symbol, after, until):
        # cycle-2 sell never appears; BOTH ledger legs are present so the read is
        # complete — the session really is 165 shares open.
        keep = [x for x in _canf_orders() if not x.client_order_id.startswith("chili_ops_flat_")]
        return {"readable": True, "orders": keep, "truncated": False}

    orc.reconcile_one_outcome(_FakeDb(_LEDGER_19471), o, s, broker_orders_reader=_no_closing_sell)
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
        def order_by(self, *a, **k): return self
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
    assert s.chili_momentum_outcome_recon_broker_attribution_no_fill_backoff_seconds == 1800


# ═══════════════════════════════════════════════════════════════════════════════
# ADVERSARIAL-REVIEW HARDENING (2026-09-02). One test per MUST CHANGE.
# ═══════════════════════════════════════════════════════════════════════════════

# ── (A) BUDGET: a no-fill row must never starve a newly terminal FILLED session ──
def _no_fill_pair(idx):
    """A `cancelled_pre_entry` shape: NO entry evidence anywhere on the envelope.
    115 of these were live in a 2-day lookback on 09-02."""
    le = {"trade_cycles": 0, "position": None}
    sess = SimpleNamespace(
        id=90000 + idx, symbol="AAME", execution_family="alpaca_spot", mode="live",
        state="live_cancelled", started_at=datetime(2026, 9, 2, 10, 0, 0),
        ended_at=datetime(2026, 9, 2, 10, 5, 0),
        risk_snapshot_json={"momentum_live_execution": le},
    )
    outcome = SimpleNamespace(
        id=700000 + idx, session_id=90000 + idx, symbol="AAME", mode="live",
        execution_family="alpaca_spot", terminal_at=datetime(2026, 9, 2, 10, 5, 0),
        realized_pnl_usd=None, return_bps=None,
        broker_recon_status=orc.STATUS_NO_FILLS, broker_realized_pnl_usd=None,
        broker_return_bps=None, broker_notional_basis_usd=None, broker_win=None,
        broker_divergence_usd=None, broker_reconciled_at=None,
        broker_recon_detail_json={"status": orc.STATUS_NO_FILLS, "source": "ledger"},
    )
    return outcome, sess


def test_no_entry_evidence_rows_never_spend_a_broker_read():
    """THE #1287 LANDMINE SHAPE: `needs_reconcile` admits every non-terminal row
    each 60 s pass and the old loop charged ONE broker GET per Alpaca row — 115
    no-fill rows against a 20-read budget starve the one row the loss guard
    needs, forever (~95 `skipped_broker_budget` per pass by construction)."""
    for i in range(3):
        o, s = _no_fill_pair(i)
        assert orc.needs_reconcile(o, s) is True, "still reconciled from the ledger"
        plan = orc.broker_read_plan(o, s)
        assert plan["read"] is False
        assert plan["reason"] == orc.ATTR_SKIPPED_NO_ENTRY_EVIDENCE


def test_115_no_fill_rows_do_not_starve_a_new_reconciled_row(monkeypatch):
    """The refuter's exact census: 115 no-fill rows + 1 newly terminal row that
    the loss guard is waiting on, budget 20. The new row must be read on the
    FIRST pass and the no-fill rows must consume ZERO reads."""
    monkeypatch.setattr(orc.settings, "chili_momentum_broker_truth_reconciliation_enabled", True, raising=False)
    monkeypatch.setattr(orc.settings, "chili_momentum_outcome_recon_broker_attribution_max_per_pass", 20, raising=False)
    rows = [_no_fill_pair(i) for i in range(115)]
    # the FILLED session: terminal `reconciled` but never attributed (the 203734 shape)
    hot = _outcome_and_session(recon_status="reconciled", detail={"status": "reconciled", "source": "ledger_confirmed"})
    rows.insert(57, hot)  # buried in the middle of the heap, as it would be

    class _Q:
        def join(self, *a, **k): return self
        def filter(self, *a, **k): return self
        def order_by(self, *a, **k): return self
        def all(self): return list(rows)

    class _Db:
        def query(self, *a, **k): return _Q()
        def commit(self): pass
        def rollback(self): pass

    read_sessions = []

    def _one(db, outcome, sess, **kw):
        if orc.broker_read_plan(outcome, sess).get("read"):
            read_sessions.append(outcome.session_id)
        outcome.broker_recon_status = outcome.broker_recon_status or orc.STATUS_NO_FILLS
        return {"status": outcome.broker_recon_status}

    monkeypatch.setattr(orc, "reconcile_one_outcome", _one)
    out = orc.reconcile_momentum_outcomes_to_broker_truth(_Db(), lookback_days=2.0, day_net_advisory=False)
    assert out["ok"] is True
    assert out["broker_reads"] == 1, out
    assert out["skipped_broker_budget"] == 0, "nothing is starved"
    assert out["skipped_no_broker_read_needed"] == 115
    assert read_sessions == [SID], "the loss-guard row got the budget on pass 1"


def test_the_loss_guard_row_is_ordered_first(monkeypatch):
    """Never unordered `.all()`: the batch query has no natural order and the
    per-pass UPDATEs reshuffle the heap."""
    never = _outcome_and_session(recon_status=None)
    upgrade = _outcome_and_session(recon_status="reconciled", detail={"status": "reconciled"})
    attributed = _outcome_and_session(
        recon_status="reconciled",
        detail={"status": "reconciled", "attribution_version": orc.ATTRIBUTION_VERSION})
    non_terminal = _outcome_and_session(recon_status=orc.STATUS_RESIDUAL_OPEN, detail={})
    assert orc._attribution_priority(*never) == 0
    assert orc._attribution_priority(*upgrade) == 0
    assert orc._attribution_priority(*non_terminal) == 1
    assert orc._attribution_priority(*attributed) == 2


def test_no_owned_fills_backs_off_instead_of_re_reading_every_pass():
    """The broker was READABLE and owned nothing. Broker fills are immutable, so
    re-listing every 60 s cannot change the answer — it only eats the budget."""
    o, s = _outcome_and_session(le_extra={"emergency_exit_accounting_pending": None})

    def _empty(symbol, after, until):
        return {"readable": True, "orders": [], "truncated": False}

    # no ledger legs → nothing contradicts an empty listing
    orc.reconcile_one_outcome(_FakeDb([]), o, s, broker_orders_reader=_empty)
    d = o.broker_recon_detail_json
    assert d["broker_attribution"]["attr_status"] == orc.ATTR_NO_OWNED_FILLS
    assert d["attribution_version"] == orc.ATTRIBUTION_VERSION
    assert d["attribution_next_retry_utc"] > datetime.utcnow().isoformat()
    plan = orc.broker_read_plan(o, s)
    assert plan["read"] is False and plan["reason"] == orc.ATTR_SKIPPED_BACKOFF
    horizon = d["attribution_next_retry_utc"]

    # THE BACKOFF MUST SURVIVE ITS OWN SKIP: a skipped pass still rewrites
    # broker_recon_detail_json, so it has to carry the horizon (and the marker
    # the plan reads) forward verbatim — otherwise the row is read again next pass.
    calls = []
    orc.reconcile_one_outcome(_FakeDb([]), o, s,
                              broker_orders_reader=lambda *a: calls.append(a) or {"readable": True, "orders": []})
    assert calls == []
    d2 = o.broker_recon_detail_json
    assert d2["broker_attribution"]["attr_status"] == orc.ATTR_SKIPPED_BACKOFF
    assert d2["attribution_next_retry_utc"] == horizon, "the horizon is never pushed out by a skip"
    assert orc.broker_read_plan(o, s)["reason"] == orc.ATTR_SKIPPED_BACKOFF

    # a NEW terminal_at (the row genuinely changed) re-opens the read immediately
    o.terminal_at = datetime(2026, 9, 2, 15, 0, 0)
    assert orc.broker_read_plan(o, s)["read"] is True


def test_backoff_expires_and_a_skipped_row_keeps_its_ledger_label():
    o, s = _outcome_and_session(le_extra={"emergency_exit_accounting_pending": None})
    o.broker_recon_detail_json = {
        "broker_attribution": {"attr_status": orc.ATTR_NO_OWNED_FILLS},
        "attribution_next_retry_utc": (datetime.utcnow() - timedelta(seconds=1)).isoformat(),
        "attribution_terminal_at": o.terminal_at.isoformat(),
    }
    assert orc.broker_read_plan(o, s)["read"] is True

    o2, s2 = _no_fill_pair(1)
    calls = []
    orc.reconcile_one_outcome(_FakeDb([]), o2, s2,
                              broker_orders_reader=lambda *a: calls.append(a) or {"readable": True, "orders": []})
    assert calls == [], "no broker read was spent"
    assert o2.broker_recon_status == orc.STATUS_NO_FILLS
    assert "attribution_version" not in o2.broker_recon_detail_json
    assert o2.broker_recon_detail_json["broker_attribution"]["broker_read"] is False


# ── (B) a PARTIAL listing never certifies ────────────────────────────────────
def test_partial_listing_that_misses_ledger_legs_never_certifies():
    """The refuter's exact case: the listing returns only cycle 2 for 19471. The
    ledger's own broker_confirmed legs (552efe43 / af3a4b0c) are absent, so the
    read provably does not cover the session — wrong bound account generation,
    wrong window, empty page. It must NOT be stamped reconciled at −108.85."""
    o, s = _outcome_and_session()

    def _cycle2_only(symbol, after, until):
        keep = {"f3ed508d-e441-47b3-b76b-2449b6f0a133", "ddba3ed2-6854-4c34-92be-7efdd1926fa4"}
        return {"readable": True, "orders": [x for x in _canf_orders() if x.order_id in keep],
                "truncated": False}

    orc.reconcile_one_outcome(_FakeDb(_LEDGER_19471), o, s, broker_orders_reader=_cycle2_only)
    assert o.broker_recon_status == orc.STATUS_BROKER_UNAVAILABLE
    assert o.broker_realized_pnl_usd is None and o.broker_notional_basis_usd is None
    d = o.broker_recon_detail_json
    assert d["broker_attribution"]["attr_status"] == orc.ATTR_LISTING_INCOMPLETE
    assert set(d["broker_attribution"]["ledger_ids_missing_from_broker"]) == {
        "552efe43-395a-4f76-836a-7be3d30a8689", "af3a4b0c-d7fc-4379-8b25-ddc50ab82a31",
    }
    assert "attribution_version" not in d, "never terminal off an incomplete read"
    assert rp._alpaca_loss_history_broker_truth(o) is False


def test_empty_listing_with_ledger_legs_is_incomplete_not_no_owned_fills():
    """`no_owned_fills` while the ledger holds broker_confirmed legs is the same
    inconsistency: the old code stamped attribution_version and froze the row."""
    o, s = _outcome_and_session()
    orc.reconcile_one_outcome(_FakeDb(_LEDGER_19471), o, s,
                              broker_orders_reader=lambda *a: {"readable": True, "orders": [], "truncated": False})
    assert o.broker_recon_status == orc.STATUS_BROKER_UNAVAILABLE
    d = o.broker_recon_detail_json["broker_attribution"]
    assert d["attr_status"] == orc.ATTR_LISTING_INCOMPLETE
    assert set(d["ledger_ids_missing_from_broker"]) == {
        "552efe43-395a-4f76-836a-7be3d30a8689", "af3a4b0c-d7fc-4379-8b25-ddc50ab82a31"}
    assert "attribution_version" not in o.broker_recon_detail_json


def test_a_ledger_leg_outside_the_window_is_not_a_false_incompleteness():
    """Precision: only a ledger leg whose fill falls INSIDE the queried window is
    expected in the listing, so a legitimately out-of-window leg never fabricates
    an incomplete read (which would freeze the row on broker_unavailable)."""
    ledger = list(_LEDGER_19471) + [
        ("exit", 1, "broker_confirmed", 3.9, 1.0, 0.0, None, None, -0.5, 4.34,
         "zzzz-way-later", datetime(2026, 9, 3, 20, 0, 0)),
    ]
    o, s = _outcome_and_session()
    orc.reconcile_one_outcome(_FakeDb(ledger), o, s, broker_orders_reader=_reader_ok)
    assert o.broker_recon_status == orc.STATUS_RECONCILED
    assert o.broker_recon_detail_json["broker_attribution"]["ledger_ids_missing_from_broker"] == []


# ── (C) OCO child legs ───────────────────────────────────────────────────────
def _toco_parent(oid, cid, side, filled, px, at, legs):
    o = _o(oid, cid, side, "canceled" if not filled else "filled", filled, px, at)
    o.raw["legs"] = legs
    o.raw["order_class"] = "oco"
    return o


def test_oco_stop_leg_fill_is_attributed_through_raw_legs():
    """Alpaca gives NO client_order_id to a child leg. When the STOP leg fires the
    parent `chili_ml_toco_<sid>_` reads canceled with filled_qty 0 and the fill
    lives in raw.legs[0]. Blind to that, EVERY stop-leg exit is a permanent
    `residual_open` → the account's loss history goes unavailable for the day."""
    orders = [
        _o("e1", "chili_ml_e_19471_a_b", "buy", "filled", 100, 5.00, "2026-09-02 11:10:00+00:00"),
        _toco_parent(
            "toco-parent", "chili_ml_toco_19471_abc", "sell", 0, None, "2026-09-02 11:20:00+00:00",
            legs=[{"id": "leg-stop", "status": "filled", "order_type": "stop",
                   "qty": 100.0, "filled_qty": 100.0, "filled_avg_price": 4.50,
                   "stop_price": 4.55, "limit_price": None}],
        ),
    ]
    a = orc.attribute_session_broker_orders(
        session_id=SID, symbol="CANF", side_long=True, orders=orders,
        window_start=W_START, window_end=W_END,
    )
    assert a["attr_status"] == orc.ATTR_FLAT
    leg = [l for l in a["legs"] if l["broker_order_id"] == "leg-stop"]
    assert leg and leg[0]["attribution"] == "session_cid_oco_leg"
    assert leg[0]["parent_broker_order_id"] == "toco-parent"
    assert leg[0]["side"] == "sell", "an OCO child is the SAME side as its parent"
    assert a["broker_pnl_usd"] == pytest.approx(100 * (4.50 - 5.00))


def test_oco_take_profit_parent_fill_is_not_double_counted_with_its_legs():
    orders = [
        _o("e1", "chili_ml_e_19471_a_b", "buy", "filled", 100, 5.00, "2026-09-02 11:10:00+00:00"),
        _toco_parent(
            "toco-parent", "chili_ml_toco_19471_abc", "sell", 100, 5.40, "2026-09-02 11:20:00+00:00",
            legs=[{"id": "leg-stop", "status": "canceled", "order_type": "stop",
                   "qty": 100.0, "filled_qty": 0.0, "filled_avg_price": None}],
        ),
    ]
    a = orc.attribute_session_broker_orders(
        session_id=SID, symbol="CANF", side_long=True, orders=orders,
        window_start=W_START, window_end=W_END,
    )
    assert a["attr_status"] == orc.ATTR_FLAT
    assert a["close_qty"] == pytest.approx(100.0)
    assert a["broker_pnl_usd"] == pytest.approx(40.0)


def test_a_foreign_sessions_oco_legs_are_never_walked():
    orders = [
        _o("e1", "chili_ml_e_19471_a_b", "buy", "filled", 100, 5.00, "2026-09-02 11:10:00+00:00"),
        _toco_parent(
            "foreign-parent", "chili_ml_toco_19457_abc", "sell", 0, None, "2026-09-02 11:20:00+00:00",
            legs=[{"id": "foreign-leg", "status": "filled", "qty": 100.0,
                   "filled_qty": 100.0, "filled_avg_price": 4.50}],
        ),
    ]
    a = orc.attribute_session_broker_orders(
        session_id=SID, symbol="CANF", side_long=True, orders=orders,
        window_start=W_START, window_end=W_END,
    )
    assert a["attr_status"] == orc.ATTR_RESIDUAL_OPEN
    assert all(l["broker_order_id"] != "foreign-leg" for l in a["legs"])


# ── (D) ownership by broker_order_id + label preservation ────────────────────
def test_orphan_repair_close_is_owned_by_order_id_not_cid():
    """`orphrec-<symbol>-<digest>` is a cid `_SESSION_CID_RE` cannot own. Without
    id-ownership the repaired row lands residual_open and its broker_* is NULLed."""
    orders = [
        _o("entry-oid", "chili_ml_e_19471_a_b", "buy", "filled", 100, 5.00, "2026-09-02 11:10:00+00:00"),
        _o("orphrec-oid", "orphrec-CANF-9f3a2c", "sell", "filled", 100, 4.80, "2026-09-02 11:40:00+00:00"),
    ]
    a = orc.attribute_session_broker_orders(
        session_id=SID, symbol="CANF", side_long=True, orders=orders,
        window_start=W_START, window_end=W_END,
        owned_order_ids={"entry-oid", "orphrec-oid"},
    )
    assert a["attr_status"] == orc.ATTR_FLAT
    close = [l for l in a["legs"] if l["broker_order_id"] == "orphrec-oid"][0]
    assert close["attribution"] == "session_broker_order_id"
    assert a["broker_pnl_usd"] == pytest.approx(-20.0)


def test_owned_order_ids_are_read_from_the_orphan_repair_envelope():
    le = {
        "entry_order_id": "entry-oid",
        "orphan_reconcile_truth": {"entry_order_id": "entry-oid", "exit_order_id": "orphrec-oid"},
        "emergency_exit_authority": {"order_id": "auth-oid"},
        "entry_orders_resolved": {"resolved-oid": "adopted"},
        "entry_order_ids_all": ["ids-all-oid"],
    }
    got = orc._owned_order_ids_from_le(le)
    assert {"entry-oid", "orphrec-oid", "auth-oid", "resolved-oid", "ids-all-oid"} <= got


def test_a_terminal_fee_unconfirmed_row_is_never_demoted_without_positive_evidence():
    """The orphan-repair label (`fee_unconfirmed` + broker pnl from a
    broker-verified entry + orphan close) must survive the one-time re-touch."""
    o, s = _outcome_and_session(
        recon_status=orc.STATUS_FEE_UNCONFIRMED,
        detail={"status": orc.STATUS_FEE_UNCONFIRMED},
        le_extra={"emergency_exit_accounting_pending": None},
    )
    o.broker_realized_pnl_usd = -20.0
    o.broker_notional_basis_usd = 500.0

    def _entry_only(symbol, after, until):
        # the orphan close is invisible (a cid we cannot own and an id we were not told)
        return {"readable": True, "orders": [x for x in _canf_orders() if x.side == "buy"],
                "truncated": False}

    orc.reconcile_one_outcome(_FakeDb([]), o, s, broker_orders_reader=_entry_only)
    assert o.broker_recon_status == orc.STATUS_FEE_UNCONFIRMED, "label preserved"
    assert o.broker_realized_pnl_usd == pytest.approx(-20.0)
    assert o.broker_recon_detail_json.get("attribution_residual_open_label_preserved") is True
    assert o.broker_recon_detail_json["attribution_version"] == orc.ATTRIBUTION_VERSION


def test_attribution_never_promotes_a_fee_unconfirmed_row_to_reconciled():
    """Fee truth was deliberately marked unsettled; sharpening the numbers must
    not silently admit the row into learning."""
    o, s = _outcome_and_session(recon_status=orc.STATUS_FEE_UNCONFIRMED,
                                detail={"status": orc.STATUS_FEE_UNCONFIRMED})
    orc.reconcile_one_outcome(_FakeDb(_LEDGER_19471), o, s, broker_orders_reader=_reader_ok)
    assert o.broker_recon_status == orc.STATUS_FEE_UNCONFIRMED
    assert o.broker_realized_pnl_usd == pytest.approx(BROKER_PNL, abs=1e-6)
    assert o.broker_recon_detail_json["prior_label_preserved_fee_unconfirmed"] is True


def test_a_never_reconciled_row_still_demotes_normally():
    o, s = _outcome_and_session(recon_status=None)

    def _no_closing_sell(symbol, after, until):
        keep = [x for x in _canf_orders() if not x.client_order_id.startswith("chili_ops_flat_")]
        return {"readable": True, "orders": keep, "truncated": False}

    orc.reconcile_one_outcome(_FakeDb(_LEDGER_19471), o, s, broker_orders_reader=_no_closing_sell)
    assert o.broker_recon_status == orc.STATUS_RESIDUAL_OPEN


# ── (E) unpriced-leg fallback hardening ──────────────────────────────────────
def test_a_close_that_filled_before_this_session_opened_is_not_ours():
    """(b)-matching keyed on qty + window only: a sell that filled BEFORE this
    session's own opening fill belongs to the earlier overlapping session."""
    orders = [
        _ui_sell("early-ui", 165, 4.90, "2026-09-02 11:05:00+00:00"),  # before the 11:10 entry
        _o("552efe43-395a-4f76-836a-7be3d30a8689", "chili_ml_e_19471_a_b", "buy", "filled",
           355, 4.34, "2026-09-02 11:10:19+00:00"),
        _o("af3a4b0c-d7fc-4379-8b25-ddc50ab82a31", "chili_ml_s_19471_c", "sell", "filled",
           355, 4.119915, "2026-09-02 11:11:05+00:00"),
        _o("f3ed508d-e441-47b3-b76b-2449b6f0a133", "chili_ml_e_19471_a_d", "buy", "filled",
           165, 4.62, "2026-09-02 11:19:12+00:00"),
    ]
    a = orc.attribute_session_broker_orders(
        session_id=SID, symbol="CANF", side_long=True, orders=orders,
        unpriced_legs=[_UNPRICED_LEG], window_start=W_START, window_end=W_END,
    )
    assert a["attr_status"] == orc.ATTR_RESIDUAL_OPEN
    assert all(l["broker_order_id"] != "early-ui" for l in a["legs"] if l.get("attribution"))


def test_an_unpriced_leg_already_attributed_by_its_own_cid_absorbs_nothing():
    """Live rows carry `chili_ml_x_<sid>_` on the unpriced leg. Once (a) matched
    that order, searching non-owned candidates for the SAME leg can only absorb a
    stranger's same-qty sell → OVERSOLD instead of flat."""
    unpriced = dict(_UNPRICED_LEG)
    unpriced["client_order_id"] = "chili_ml_x_19471_deadbeef"
    orders = [
        _o("e1", "chili_ml_e_19471_a_b", "buy", "filled", 165, 4.62, "2026-09-02 11:19:12+00:00"),
        _o("x1", "chili_ml_x_19471_deadbeef", "sell", "filled", 165, 3.96, "2026-09-02 11:34:31+00:00"),
        _ui_sell("stranger", 165, 3.90, "2026-09-02 11:35:00+00:00"),
    ]
    a = orc.attribute_session_broker_orders(
        session_id=SID, symbol="CANF", side_long=True, orders=orders,
        unpriced_legs=[unpriced], window_start=W_START, window_end=W_END,
    )
    assert a["attr_status"] == orc.ATTR_FLAT
    assert all(l["broker_order_id"] != "stranger" for l in a["legs"] if l.get("attribution"))


def test_an_order_id_another_outcome_already_claims_is_never_attributed_twice():
    """Two overlapping same-symbol sessions each holding an equal-qty unpriced leg
    would BOTH claim one UI sell → the loss guard sums both → double count."""
    o, s = _outcome_and_session()

    def _ui_instead_of_ops(symbol, after, until):
        keep = [x for x in _canf_orders() if not x.client_order_id.startswith("chili_ops_flat_")]
        keep.append(_ui_sell("shared-ui-sell", 165, 3.96, "2026-09-02 11:34:31+00:00"))
        return {"readable": True, "orders": keep, "truncated": False}

    db = _FakeDb(_LEDGER_19471, collisions=[(19457, "shared-ui-sell")])
    orc.reconcile_one_outcome(db, o, s, broker_orders_reader=_ui_instead_of_ops)
    assert o.broker_recon_status == orc.STATUS_AMBIGUOUS_TRADE
    assert o.broker_realized_pnl_usd is None
    assert o.broker_recon_detail_json["broker_attribution"]["unpriced_collision"] == {
        "shared-ui-sell": 19457}


def test_an_unreadable_collision_probe_fails_closed():
    o, s = _outcome_and_session()

    def _ui_instead_of_ops(symbol, after, until):
        keep = [x for x in _canf_orders() if not x.client_order_id.startswith("chili_ops_flat_")]
        keep.append(_ui_sell("shared-ui-sell", 165, 3.96, "2026-09-02 11:34:31+00:00"))
        return {"readable": True, "orders": keep, "truncated": False}

    db = _FakeDb(_LEDGER_19471, probe_raises=True)
    orc.reconcile_one_outcome(db, o, s, broker_orders_reader=_ui_instead_of_ops)
    assert o.broker_recon_status == orc.STATUS_AMBIGUOUS_TRADE
    assert o.broker_recon_detail_json["broker_attribution"]["unpriced_collision_probe"] == "unreadable"


def test_the_cid_matched_operator_sell_needs_no_collision_probe():
    """The 19471 path itself: the operator sell carries the session cid, so it is
    attributed by (a) and the probe is never consulted."""
    db = _FakeDb(_LEDGER_19471, probe_raises=True)
    o, s = _outcome_and_session()
    orc.reconcile_one_outcome(db, o, s, broker_orders_reader=_reader_ok)
    assert o.broker_recon_status == orc.STATUS_RECONCILED
    assert not any("momentum_automation_outcomes" in q for q in db.sql)


# ── (F) ONE divergence event per outcome ─────────────────────────────────────
def test_divergence_event_is_emitted_once_per_outcome(monkeypatch):
    from app.services.trading.momentum_neural import persistence as _p

    events = []
    monkeypatch.setattr(
        _p, "append_trading_automation_event",
        lambda db, sid, et, payload, **kw: events.append((sid, et, payload)),
        raising=False,
    )
    o, s = _outcome_and_session()
    orc.reconcile_one_outcome(_FakeDb(_LEDGER_19471), o, s, broker_orders_reader=_reader_ok)
    assert len(events) == 1
    sid, et, payload = events[0]
    assert sid == SID and et == "broker_truth_attribution_divergence"
    assert set(payload["legs_missing_from_ledger"]) == {
        "f3ed508d-e441-47b3-b76b-2449b6f0a133", "ddba3ed2-6854-4c34-92be-7efdd1926fa4"}
    assert payload["divergence_usd"] == pytest.approx(-108.850005, abs=1e-6)
    # idempotency: the marker rides in the detail json the same pass wrote
    assert o.broker_recon_detail_json["divergence_event_emitted"] is True
    orc.reconcile_one_outcome(_FakeDb(_LEDGER_19471), o, s, broker_orders_reader=_reader_ok)
    assert len(events) == 1, "never a second event for the same outcome"


def test_no_divergence_event_when_the_ledger_already_had_every_leg(monkeypatch):
    from app.services.trading.momentum_neural import persistence as _p

    events = []
    monkeypatch.setattr(
        _p, "append_trading_automation_event",
        lambda db, sid, et, payload, **kw: events.append(et), raising=False)
    full_ledger = list(_LEDGER_19471) + [
        ("entry", 1, "broker_confirmed", 4.62, 165.0, 0.0, None, None, None, None,
         "f3ed508d-e441-47b3-b76b-2449b6f0a133", datetime(2026, 9, 2, 11, 19, 12)),
        ("exit", 1, "broker_confirmed", 3.960303, 165.0, 0.0, None, None, -108.85, 4.62,
         "ddba3ed2-6854-4c34-92be-7efdd1926fa4", datetime(2026, 9, 2, 11, 34, 31)),
    ]
    o, s = _outcome_and_session()
    orc.reconcile_one_outcome(_FakeDb(full_ledger), o, s, broker_orders_reader=_reader_ok)
    assert events == []
    assert o.broker_recon_status == orc.STATUS_RECONCILED


# ── read-only posture of the NEW query surface ───────────────────────────────
def test_the_new_db_reads_are_selects_only():
    for fn in (orc._ledger_legs_for_attribution, orc._broker_order_ids_attributed_elsewhere):
        src = inspect.getsource(fn)
        upper = src.upper()
        for verb in ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE", "ALTER ", "DROP "):
            assert verb not in upper, (fn.__name__, verb)
        assert "SELECT" in upper
