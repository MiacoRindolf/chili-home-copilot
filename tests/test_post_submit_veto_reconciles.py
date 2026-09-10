"""A VETO AFTER SUBMIT MUST RECONCILE THE ORDER, NEVER RE-EVALUATE AS IF FLAT.

SKYQ 21605 (2026-09-10): `live_entry_submitted` 14:05:31.003 (1530 sh @ 3.50);
`live_entry_spread_risk_veto` (758 bps) 14:05:33.994 — a full pre-submit pass
running 3.0 s AFTER the submit with a `le` that did not know the order. It wrote
that stale `le` back and parked the session in WATCHING; the runner then ran seven
more candidate→pending→place climbs while the broker fill sat NAKED for 29 min
(+2.0R → −3.07R, no deadman) until the operator pause adopted it
(`live_entry_fill_self_healed` critical → `operator_flatten` −$61.20).

MEASURED (7 days to 2026-09-10, mode='live'): 34 sessions submitted; 5 had a
veto-class receipt between submit and resolution, 4 of those 5 ended in
`live_entry_fill_self_healed` (SLE 20352 never resolved); 6 had an entry
re-evaluation in that gap, 5 of 6 self-healed. Ordinary Alpaca adopt heals in
0.3–4.4 s (22 sessions); this class heals in 174–1,747 s.

The invariant under test: once an entry order crossed HTTP, a veto reconciles it
(unfilled → cancel + confirm; filled/partial → adopt via the fill-watch; unknown
→ stay pending) and emits `live_post_submit_veto_reconciled`. It never returns the
session to entry evaluation and never POSTs a second clip.

Runnable: pytest tests/test_post_submit_veto_reconciles.py -v
"""
from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import live_runner as LR


# ── fakes ────────────────────────────────────────────────────────────────────


class _Db:
    def __init__(self):
        self.flushes = 0

    def flush(self):
        self.flushes += 1


class _Order:
    def __init__(self, oid="d8c1eef5", cid="chili_ml_e_21605_a57a0bfd", filled=0.0, status="open", avg=3.5):
        self.order_id = oid
        self.client_order_id = cid
        self.filled_size = filled
        self.average_filled_price = avg
        self.status = status
        self.product_id = "SKYQ"
        self.side = "buy"


class _Adapter:
    """Strict-truth adapter double. `on_cancel` mutates the order to simulate the
    venue's answer to the cancel (void, or a fill that won the race)."""

    def __init__(self, order: _Order | None, *, on_cancel=None, cid_absent=False):
        self.order = order
        self.on_cancel = on_cancel
        self.cid_absent = cid_absent
        self.cancels: list[str] = []
        self.places = 0

    def get_order_truth(self, oid):
        if self.order is None or str(oid) != self.order.order_id:
            return {"readable": True, "found": False, "order": None}
        return {"readable": True, "found": True, "order": self.order}

    def get_order_by_client_order_id_truth(self, cid):
        if self.cid_absent or self.order is None or str(cid) != self.order.client_order_id:
            return {"readable": True, "found": False, "order": None}
        return {"readable": True, "found": True, "order": self.order}

    def cancel_order(self, oid):
        self.cancels.append(str(oid))
        if self.on_cancel is not None:
            self.on_cancel(self.order)
        return {"ok": True}

    def place_limit_order_gtc(self, **kwargs):  # must never be reached
        self.places += 1
        raise AssertionError("a post-submit veto must never place a second entry")


def _sess(state, le, *, family="robinhood_spot", sid=21605):
    snap = {
        "alpaca_account_scope": "alpaca:paper",
        "alpaca_symbol_claim_token": f"tok-{sid}",
        "momentum_live_execution": dict(le),
    }
    return SimpleNamespace(
        id=sid, symbol="SKYQ", state=state, execution_family=family, mode="live",
        correlation_id="corr", updated_at=None, ended_at=None, user_id=1,
        risk_snapshot_json=snap,
    )


SUBMITTED_LE = {
    "entry_submitted": True,
    "entry_order_id": "d8c1eef5",
    "entry_client_order_id": "chili_ml_e_21605_a57a0bfd",
    "entry_order_ids_all": ["d8c1eef5"],
    "entry_orders_resolved": {},
    "entry_submit_utc": "2026-09-10T14:05:31.003363",
    "entry_limit_price": "3.50",
    "entry_order_type": "limit",
    "entry_place_result": {"ok": True, "error": None},
    "entry_want_qty": 1530.0,
}
STALE_LE = {  # what the second pass carried: no idea an order exists
    "entry_want_qty": 1530.0,
    "entry_place_count": 2,
    "entry_spread_risk_gate": {"reason": "spread_exceeds_expected_move_budget"},
}


@pytest.fixture
def harness(monkeypatch):
    events: list[tuple[str, dict]] = []
    transitions: list[tuple[str, str]] = []
    continuations: list[int] = []
    committed: dict = {"le": None}
    claim: dict = {"row": (False, None)}

    monkeypatch.setattr(LR, "_emit", lambda db, sess, et, payload: events.append((et, payload)))

    def _fake_transition(db, sess, new_state):
        transitions.append((sess.state, new_state))
        sess.state = new_state

    monkeypatch.setattr(LR, "_safe_transition", _fake_transition)
    monkeypatch.setattr(LR, "_schedule_entry_fsm_continuation", lambda sid: continuations.append(int(sid)) or True)
    monkeypatch.setattr(LR, "_committed_live_exec_snapshot", lambda sess: committed["le"])
    monkeypatch.setattr(LR, "read_action_claim_committed", lambda **kw: claim["row"])
    monkeypatch.setattr(
        LR, "_resolve_alpaca_entry_claim_from_terminal_order",
        lambda sess, no, *, le, durable_adopted: True,
    )
    return SimpleNamespace(
        events=events, transitions=transitions, continuations=continuations,
        committed=committed, claim=claim,
    )


def _receipts(h):
    return [p for et, p in h.events if et == LR._POST_SUBMIT_VETO_EVENT]


# ── 1. submitted → veto → FILLED: adopt via the fill-watch, never re-evaluate ──


def test_filled_order_is_adopted_via_fill_watch_never_entry_evaluation(harness):
    order = _Order(filled=1530.0, status="filled")
    adapter = _Adapter(order)
    le = dict(SUBMITTED_LE)
    sess = _sess(LR.STATE_LIVE_PENDING_ENTRY, le)
    db = _Db()

    truth = LR._durable_inflight_entry_order_truth(db, sess, le)
    assert truth is not None and truth["order_id"] == "d8c1eef5"
    out = LR._reconcile_post_submit_veto(
        db, sess, adapter, le, truth=truth,
        veto_event="live_entry_spread_risk_veto",
        veto_reason="spread_exceeds_expected_move_budget",
        veto_payload={"spread_bps": 758.0175},
    )

    assert out["post_submit_veto"] == "adopt_via_fill_watch"
    assert out["order_state"] == "filled"
    assert "skipped" not in out  # the legacy veto return shape is NOT produced
    # never back to entry evaluation; identity intact for the ack-poll branch
    assert sess.state == LR.STATE_LIVE_PENDING_ENTRY
    assert not any(new == LR.STATE_WATCHING_LIVE for _, new in harness.transitions)
    assert le["entry_submitted"] is True and le["entry_order_id"] == "d8c1eef5"
    assert harness.continuations == [21605]  # fill-watch runs on the next pass
    assert adapter.cancels == [] and adapter.places == 0
    (r,) = _receipts(harness)
    assert r["veto_reason"] == "spread_exceeds_expected_move_budget"
    assert r["order_state"] == "filled" and r["action"] == "adopt_via_fill_watch"
    assert r["filled_size"] == 1530.0 and r["veto_payload"]["spread_bps"] == 758.0175
    assert sess.risk_snapshot_json["momentum_live_execution"]["entry_post_submit_veto"]["action"] == "adopt_via_fill_watch"


# ── 2. submitted → veto → OPEN: cancel, confirm zero-fill, only then release ──


def test_open_order_is_cancelled_and_confirmed_before_release(harness):
    def _venue_cancels(o):
        o.status = "cancelled"

    order = _Order(filled=0.0, status="open")
    adapter = _Adapter(order, on_cancel=_venue_cancels)
    le = dict(SUBMITTED_LE)
    sess = _sess(LR.STATE_LIVE_PENDING_ENTRY, le)
    db = _Db()

    truth = LR._durable_inflight_entry_order_truth(db, sess, le)
    out = LR._reconcile_post_submit_veto(
        db, sess, adapter, le, truth=truth,
        veto_event="live_entry_spread_risk_veto", veto_reason="spread_exceeds_expected_move_budget",
    )

    assert adapter.cancels == ["d8c1eef5"]
    assert out["post_submit_veto"] == "cancelled_void_confirmed_release_to_watching"
    assert out["order_state"] == "void"
    # cancellation CONFIRMED (terminal, zero fill) ⇒ the release is legal
    assert le["entry_submitted"] is False and "entry_order_id" not in le
    assert le["entry_orders_resolved"] == {"d8c1eef5": "void"}  # history never forgotten
    assert le["entry_order_ids_all"] == ["d8c1eef5"]
    assert sess.state == LR.STATE_WATCHING_LIVE
    assert harness.continuations == []
    (r,) = _receipts(harness)
    assert r["action"] == "cancelled_void_confirmed_release_to_watching"
    assert r["cancel"]["cancel_result"] == {"ok": True}


def test_cancel_that_loses_the_race_to_a_fill_adopts(harness):
    def _fill_wins(o):
        o.status = "filled"
        o.filled_size = 1530.0

    order = _Order(filled=0.0, status="open")
    adapter = _Adapter(order, on_cancel=_fill_wins)
    le = dict(SUBMITTED_LE)
    sess = _sess(LR.STATE_LIVE_PENDING_ENTRY, le)
    db = _Db()

    truth = LR._durable_inflight_entry_order_truth(db, sess, le)
    out = LR._reconcile_post_submit_veto(
        db, sess, adapter, le, truth=truth,
        veto_event="live_entry_spread_risk_veto", veto_reason="spread_exceeds_expected_move_budget",
    )

    assert adapter.cancels == ["d8c1eef5"]
    assert out["post_submit_veto"] == "cancel_fill_adopt_via_fill_watch"
    assert out["order_state"] == "filled"
    assert sess.state == LR.STATE_LIVE_PENDING_ENTRY
    assert le["entry_submitted"] is True and le["entry_order_id"] == "d8c1eef5"
    assert harness.continuations == [21605]


def test_unknown_order_fate_stays_pending(harness):
    """No order object from any probe ⇒ fail-closed pending (never re-evaluate)."""
    adapter = _Adapter(None)  # strict reads: not found, but oid known ⇒ unknown
    le = dict(SUBMITTED_LE)
    sess = _sess(LR.STATE_LIVE_PENDING_ENTRY, le)
    db = _Db()

    truth = LR._durable_inflight_entry_order_truth(db, sess, le)
    out = LR._reconcile_post_submit_veto(
        db, sess, adapter, le, truth=truth,
        veto_event="pre_submit_reentry", veto_reason="entry_gates_reentered_with_inflight_entry_order",
    )
    assert out["post_submit_veto"] == "reconcile_pending"
    assert out["order_state"] == "unknown"
    assert sess.state == LR.STATE_LIVE_PENDING_ENTRY
    assert le["entry_submitted"] is True and le["entry_order_id"] == "d8c1eef5"
    assert adapter.cancels == []


# ── 3. the SKYQ shape: the pass's `le` is stale; the committed row knows ──────


def test_stale_local_le_is_overridden_by_committed_row(harness):
    harness.committed["le"] = dict(SUBMITTED_LE)
    order = _Order(filled=1530.0, status="filled")
    adapter = _Adapter(order)
    le = dict(STALE_LE)
    sess = _sess(LR.STATE_LIVE_PENDING_ENTRY, le)  # ORM copy is as stale as `le`
    db = _Db()

    truth = LR._durable_inflight_entry_order_truth(db, sess, le)
    assert truth is not None
    assert truth["sources"] == ["committed_row"]
    assert truth["order_id"] == "d8c1eef5"

    out = LR._reconcile_post_submit_veto(
        db, sess, adapter, le, truth=truth,
        veto_event="live_entry_spread_risk_veto", veto_reason="spread_exceeds_expected_move_budget",
    )
    assert out["post_submit_veto"] == "adopt_via_fill_watch"
    # the submit block's stamps are RESTORED from the committed row — SKYQ's row
    # had lost entry_submit_utc / entry_limit_price / entry_place_result
    for key in ("entry_submit_utc", "entry_limit_price", "entry_order_type", "entry_place_result"):
        assert le[key] == SUBMITTED_LE[key], key
    assert le["entry_place_count"] == 2  # non-entry-identity keys of the pass survive
    (r,) = _receipts(harness)
    assert "entry_submit_utc" in r["restored_keys"]
    assert r["truth_sources"] == ["committed_row"]


def test_claim_ledger_alone_rescues_a_wiped_row(harness):
    """Row and `le` both wiped (the seven later SKYQ climbs); the Alpaca entry
    claim — the source that finally adopted SKYQ — is enough."""
    harness.claim["row"] = (True, {
        "action": "entry", "phase": "submitted", "owner_session_id": 21605,
        "client_order_id": "chili_ml_e_21605_a57a0bfd", "broker_order_id": "d8c1eef5",
    })
    order = _Order(filled=1530.0, status="filled")
    adapter = _Adapter(order)
    le = dict(STALE_LE)
    sess = _sess(LR.STATE_LIVE_PENDING_ENTRY, le, family="alpaca_spot")
    db = _Db()

    truth = LR._durable_inflight_entry_order_truth(db, sess, le)
    assert truth is not None and truth["sources"] == ["alpaca_entry_claim"]
    assert truth["claim_phase"] == "submitted"
    out = LR._reconcile_post_submit_veto(
        db, sess, adapter, le, truth=truth,
        veto_event="pre_submit_reentry", veto_reason="entry_gates_reentered_with_inflight_entry_order",
    )
    assert out["post_submit_veto"] == "adopt_via_fill_watch"
    assert le["entry_order_id"] == "d8c1eef5" and le["entry_client_order_id"] == "chili_ml_e_21605_a57a0bfd"
    assert le["entry_order_ids_all"] == ["d8c1eef5"]
    assert sess.state == LR.STATE_LIVE_PENDING_ENTRY


def test_foreign_session_claim_and_pre_http_claim_are_not_evidence(harness):
    le = dict(STALE_LE)
    sess = _sess(LR.STATE_LIVE_PENDING_ENTRY, le, family="alpaca_spot")
    db = _Db()
    # another session's order on the same symbol: never adopt it
    harness.claim["row"] = (True, {
        "action": "entry", "phase": "submitted", "owner_session_id": 21599,
        "client_order_id": "chili_ml_e_21599_x", "broker_order_id": "other",
    })
    assert LR._durable_inflight_entry_order_truth(db, sess, le) is None
    # own claim but pre-HTTP (intent_frozen, no broker id): the release seam owns it
    harness.claim["row"] = (True, {
        "action": "entry", "phase": "intent_frozen", "owner_session_id": 21605,
        "client_order_id": "chili_ml_e_21605_a57a0bfd", "broker_order_id": None,
    })
    assert LR._durable_inflight_entry_order_truth(db, sess, le) is None


def test_flat_session_returns_none_so_legacy_veto_path_runs(harness):
    le = dict(STALE_LE)
    sess = _sess(LR.STATE_LIVE_PENDING_ENTRY, le)
    assert LR._durable_inflight_entry_order_truth(_Db(), sess, le) is None
    # a legitimately RESOLVED history is flat too (ack-timeout → void)
    le2 = {"entry_submitted": False, "entry_order_ids_all": ["old"], "entry_orders_resolved": {"old": "void"}}
    sess2 = _sess(LR.STATE_LIVE_PENDING_ENTRY, le2)
    assert LR._durable_inflight_entry_order_truth(_Db(), sess2, le2) is None


def test_orm_snapshot_identity_counts_when_local_le_is_stale(harness):
    """A helper that bound the order into the ORM copy (e.g. the owner-claim
    attach) is a witness even when the pass's local `le` is stale."""
    le = dict(STALE_LE)
    sess = _sess(LR.STATE_LIVE_PENDING_ENTRY, SUBMITTED_LE)
    truth = LR._durable_inflight_entry_order_truth(_Db(), sess, le)
    assert truth is not None and truth["sources"] == ["orm_snapshot"]


# ── 4. the two call sites are fenced, in the right order ──────────────────────


def test_spread_veto_site_reconciles_before_returning_to_watching():
    src = inspect.getsource(LR.tick_live_session)
    anchor = '_emit(db, sess, "live_entry_spread_risk_veto", _spread_gate)'
    assert src.count(anchor) == 1
    tail = src[src.index(anchor):].split("\n")[:16]
    joined = "\n".join(tail)
    fence = joined.index("_durable_inflight_entry_order_truth(db, sess, le)")
    watch = joined.index("_safe_transition(db, sess, STATE_WATCHING_LIVE)")
    assert fence < watch, "the fence must run BEFORE the re-watch transition"
    assert "_reconcile_post_submit_veto(" in joined


def test_duplicate_guard_consults_durable_truth_before_the_gate_ladder():
    src = inspect.getsource(LR.tick_live_session)
    guard = src.index("# Submit entry once (duplicate guard)")
    window = src[guard: guard + 1600]
    assert "_durable_inflight_entry_order_truth(db, sess, le)" in window
    assert 'veto_event="pre_submit_reentry"' in window
    # and it sits BEFORE the first gate of the ladder (venue-health breaker)
    assert window.index("_durable_inflight_entry_order_truth") < window.index("venue_health")


# ── 5. replay parity + doctrine: no wall clock in the decision path ───────────


def test_no_wall_clock_in_the_reconcile_path():
    for fn in (
        LR._durable_inflight_entry_order_truth,
        LR._reconcile_post_submit_veto,
        LR._probe_inflight_entry_order,
        LR._classify_inflight_entry_order,
        LR._merge_committed_live_exec_into_le,
    ):
        src = inspect.getsource(fn)
        assert not re.search(r"datetime\.(utcnow|now)\(", src), fn.__name__
        assert "time.time(" not in src, fn.__name__


# ── 6. the 7-day counterfactual that justified shipping ───────────────────────


def test_seven_day_counterfactual_is_pinned():
    """Re-measure ⇒ update both the constant and this test, deliberately."""
    c = LR._POST_SUBMIT_VETO_7D_COUNTERFACTUAL
    assert c["submitted_sessions"] == 34
    assert c["veto_between_submit_and_resolution"] == 5
    assert c["veto_then_self_healed"] == 4
    assert c["reevaluation_between_submit_and_resolution"] == 6
    assert c["reevaluation_then_self_healed"] == 5
    assert c["ordinary_adopt_heal_latency_s_max"] == 4.4
    assert c["veto_class_heal_latency_s"]["SKYQ_21605"] == 1747.0
    assert min(c["veto_class_heal_latency_s"].values()) > c["ordinary_adopt_heal_latency_s_max"] * 10
