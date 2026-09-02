"""Bug B3 ng 09-02 CANF 19471: ang paused session na may naisumiteng entry ay
tahimik na bumabalik kada tick — hindi kailanman inaampon ang fill.

ANG INSIDENTE. Ang cancel_automation_session (mula sa reaper) ay tumama sa
durable-claim branch: entry_submitted=True + pointers + operator_pause
(resume_state=live_pending_entry), non-terminal. Ang tick preamble ay
bumabalik sa `{"skipped": "operator_paused"}` nang WALANG event at WALANG log
(ang legacy time-share claim ay pass-through na active=False) — kaya ang
pending-branch poll/adopt ay hindi tumakbo. Kahit pagkatapos ng manu-manong
state=live_pending_entry, ang heal ay nag-emit kada 2s pero walang adoption.

ANG AYOS: adopt-only attempt sa ilalim ng pause (_adopt_submitted_entry_fill_
while_paused — walang place, walang cancel) na sumasalamin sa adaptive-claim
path; kapag na-adopt: LIVE_ENTERED + operator_flatten_requested_utc → ang
quote-independent emergency exit ang nagpa-flatten sa parehong tick. Kapag
hindi: MINSAN kada signature na `live_tick_operator_paused_block`. Ang
zero-fill void ay gated sa durable claim resolution (walang ping-pong).

Runnable: pytest tests/test_naked_position_0902_paused.py -v
"""
from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural.live_fsm import assert_transition_live


# ── fakes ────────────────────────────────────────────────────────────────────


class _Db:
    def flush(self):
        pass


class _Order:
    def __init__(self, oid="f3ed508d", filled=165.0, avg=4.62, status="filled", cid="chili-19471"):
        self.order_id = oid
        self.client_order_id = cid
        self.filled_size = filled
        self.average_filled_price = avg
        self.status = status


class _Adapter:
    """Read-only. Every broker MUTATION raises — pause means no orders."""

    def __init__(self, order):
        self._order = order
        self.lookups = 0

    def get_order(self, oid):
        self.lookups += 1
        return (self._order, None)

    def _no(self, *a, **k):
        raise AssertionError("no broker mutation while paused")

    place_limit_order_gtc = _no
    place_market_order = _no
    place_limit_order = _no
    cancel_order = _no
    replace_order_qty = _no


def _sess(state, le, *, family="alpaca_spot"):
    return SimpleNamespace(
        id=19471, symbol="CANF", state=state, execution_family=family, mode="live",
        correlation_id="corr", updated_at=None, ended_at=None,
        risk_snapshot_json={
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_symbol_claim_token": "tok-19471",
            "momentum_live_execution": dict(le),
            "operator_pause": {"active": True, "resume_state": state},
        },
    )


@pytest.fixture
def harness(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(LR, "_emit", lambda db, sess, et, payload: events.append((et, payload)))

    def _fake_transition(db, sess, new_state):
        if sess.state == new_state:
            return
        assert_transition_live(sess.state, new_state)
        sess.state = new_state

    monkeypatch.setattr(LR, "_safe_transition", _fake_transition)
    return events


def _run(sess, adapter):
    le = sess.risk_snapshot_json["momentum_live_execution"]
    out = LR._adopt_submitted_entry_fill_while_paused(
        _Db(), sess, adapter, le=le, product_id="CANF",
    )
    return out, le


def _events(harness, name):
    return [p for et, p in harness if et == name]


# ── 1: the 19471 shape — paused + submitted + filled terminal ────────────────


def test_paused_submitted_filled_is_adopted_and_flatten_requested(harness):
    sess = _sess("watching_live", {"entry_submitted": True, "entry_order_id": "f3ed508d",
                                   "entry_client_order_id": "chili-19471"})
    adapter = _Adapter(_Order())
    out, le = _run(sess, adapter)
    assert out["adopted"] is True and out["reason"] == "adopted"
    assert le["position"]["quantity"] == 165.0
    assert le["position"]["avg_entry_price"] == 4.62
    assert le["operator_flatten_requested_utc"]
    assert le["entry_orders_resolved"]["f3ed508d"] == "adopted"
    assert le["alpaca_entry_claim_resolution_pending"]["broker_order_id"] == "f3ed508d"
    assert sess.state == "live_entered"
    assert LR._paused_session_has_exit_authority(sess) is True
    assert len(_events(harness, "live_entry_fill_adopted_while_paused")) == 1
    assert len(_events(harness, "alpaca_owner_claim_primary_fill_adopted")) == 1


def test_paused_pending_entry_state_also_adopts(harness):
    sess = _sess("live_pending_entry", {"entry_submitted": True, "entry_order_id": "f3ed508d"})
    out, le = _run(sess, _Adapter(_Order()))
    assert out["adopted"] is True
    assert sess.state == "live_entered"


# ── 2/3: open order is left resting — no cancel; partial fill is NAMED ───────


def test_open_unfilled_order_is_left_paused(harness):
    le0 = {"entry_submitted": True, "entry_order_id": "f3ed508d"}
    sess = _sess("watching_live", dict(le0))
    out, le = _run(sess, _Adapter(_Order(filled=0.0, status="new")))
    assert out["reason"] == "order_open_leave_paused"
    assert out["adopted"] is False
    assert sess.state == "watching_live"
    assert le == le0
    assert harness == []


def test_open_partially_filled_order_is_named_unowned(harness):
    le0 = {"entry_submitted": True, "entry_order_id": "f3ed508d"}
    sess = _sess("watching_live", dict(le0))
    out, le = _run(sess, _Adapter(_Order(filled=40.0, status="partially_filled")))
    assert out["reason"] == "order_open_partial_fill_unowned"
    assert out["filled_size"] == 40.0
    assert sess.state == "watching_live"
    assert le == le0
    assert harness == []


# ── 4/7: preconditions ───────────────────────────────────────────────────────


def test_not_submitted_does_no_lookup(harness):
    sess = _sess("watching_live", {"entry_submitted": False, "entry_order_id": "f3ed508d"})
    adapter = _Adapter(_Order())
    out, _ = _run(sess, adapter)
    assert out["reason"] == "not_submitted"
    assert adapter.lookups == 0


def test_position_present_does_no_lookup(harness):
    sess = _sess("live_pending_entry", {"entry_submitted": True, "entry_order_id": "f3ed508d",
                                        "position": {"quantity": 165}})
    adapter = _Adapter(_Order())
    out, _ = _run(sess, adapter)
    assert out["reason"] == "position_present"
    assert adapter.lookups == 0


def test_held_state_is_not_pre_entry(harness):
    sess = _sess("live_cooldown", {"entry_submitted": True, "entry_order_id": "f3ed508d"})
    adapter = _Adapter(_Order())
    out, _ = _run(sess, adapter)
    assert out["reason"] == "state_not_pre_entry"
    assert adapter.lookups == 0


def test_no_order_identity(harness):
    sess = _sess("watching_live", {"entry_submitted": True})
    adapter = _Adapter(_Order())
    out, _ = _run(sess, adapter)
    assert out["reason"] == "no_order_identity"
    assert adapter.lookups == 0


# ── 5: terminal zero fill, claim RESOLVED => void once ───────────────────────


def test_terminal_zero_fill_resolved_voids_once(harness, monkeypatch):
    monkeypatch.setattr(
        LR, "_resolve_alpaca_entry_claim_from_terminal_order",
        lambda sess, order, *, le, durable_adopted: True,
    )
    sess = _sess("live_pending_entry", {
        "entry_submitted": True, "entry_order_id": "f3ed508d",
        "entry_client_order_id": "chili-19471",
        "entry_reconcile_pending_client_order_id": "chili-19471",
    })
    adapter = _Adapter(_Order(filled=0.0, status="canceled"))
    out, le = _run(sess, adapter)
    assert out["reason"] == "terminal_zero_fill_voided"
    assert "entry_order_id" not in le
    assert "entry_client_order_id" not in le
    assert "entry_reconcile_pending_client_order_id" not in le
    assert le["entry_submitted"] is False
    assert le["entry_orders_resolved"]["f3ed508d"] == "void"
    assert len(_events(harness, "live_entry_void_while_paused")) == 1
    # second tick: nothing submitted any more, no second event
    out2, _ = _run(sess, adapter)
    assert out2["reason"] == "not_submitted"
    assert len(_events(harness, "live_entry_void_while_paused")) == 1


# ── 6: terminal zero fill, claim NOT resolved => le untouched, blocked once ──


def test_terminal_zero_fill_unresolved_leaves_pointers_and_blocks_once(harness, monkeypatch):
    monkeypatch.setattr(
        LR, "_resolve_alpaca_entry_claim_from_terminal_order",
        lambda sess, order, *, le, durable_adopted: False,
    )
    le0 = {
        "entry_submitted": True, "entry_order_id": "f3ed508d",
        "entry_client_order_id": "chili-19471",
        "entry_orders_resolved": {"old": "void"},
    }
    sess = _sess("live_pending_entry", dict(le0))
    adapter = _Adapter(_Order(filled=0.0, status="canceled"))
    out, le = _run(sess, adapter)
    assert out["reason"] == "terminal_zero_fill_claim_unresolved"
    assert le["entry_order_id"] == "f3ed508d"
    assert le["entry_client_order_id"] == "chili-19471"
    assert le["entry_submitted"] is True
    assert le["entry_orders_resolved"] == {"old": "void"}
    blocked = _events(harness, "live_entry_void_while_paused_blocked")
    assert len(blocked) == 1
    assert blocked[0]["reason"] == "claim_unresolved"
    assert blocked[0]["order_id"] == "f3ed508d"
    assert _events(harness, "live_entry_void_while_paused") == []
    out2, _ = _run(sess, adapter)
    assert out2["reason"] == "terminal_zero_fill_claim_unresolved"
    assert len(_events(harness, "live_entry_void_while_paused_blocked")) == 1


# ── 8: AST — the tick wires it, the helper never mutates the broker ──────────


def _tick_src() -> str:
    tree = ast.parse(inspect.getsource(LR.tick_live_session))
    return ast.unparse(tree)


def test_preamble_paused_return_attempts_adoption_and_emits_once():
    src = _tick_src()
    marker = "'skipped': 'operator_paused'"
    i1 = src.index(marker)
    before = src[max(0, i1 - 3000):i1]
    assert "_adopt_submitted_entry_fill_while_paused" in before
    assert "operator_paused_block_sig" in before
    assert "live_tick_operator_paused_block" in before
    assert before.index("operator_paused_block_sig") < before.index("live_tick_operator_paused_block")
    i2 = src.index(marker, i1 + 1)
    before2 = src[max(0, i2 - 1500):i2]
    assert "paused_post_emergency" in before2
    assert "live_tick_operator_paused_block" in before2


def test_helper_never_places_or_cancels_and_voids_only_after_resolution():
    tree = ast.parse(inspect.getsource(LR._adopt_submitted_entry_fill_while_paused))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                called.add(f.attr)
            elif isinstance(f, ast.Name):
                called.add(f.id)
    for forbidden in ("_governed_place", "place_limit_order_gtc", "place_market_order",
                      "place_limit_order", "cancel_order", "replace_order_qty"):
        assert forbidden not in called, forbidden
    src = ast.unparse(tree)
    assert src.index("_resolve_alpaca_entry_claim_from_terminal_order") < src.index("le.pop(")
    assert "terminal_zero_fill_claim_unresolved" in src
    assert "order_open_partial_fill_unowned" in src
