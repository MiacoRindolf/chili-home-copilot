"""Bug B2 ng 09-02 CANF 19471: ang self-heal (#1267) ay tumawag ng ILLEGAL na
watching_live -> live_pending_entry edge at nilunok ang exception sa DEBUG.

ANG INSIDENTE. 11:19:10–11:31:57Z ZERO events, zero runner log para sa 19471
(ticked pa rin kada 10s). `_heal_unrecognized_entry_fill` ay nakakita ng
entry_submitted + entry_order_id + walang position, tinanong ang broker
(FILLED 165), nag-bind, tapos `_safe_transition(watching_live ->
live_pending_entry)` → ValueError (hindi legal sa live_fsm) → `except:
_log.debug` → WALANG event, walang WARNING. Pagkatapos ng manu-manong UPDATE
state='live_pending_entry' ay nag-emit ito kada ~2s (36 events sa 63s).

ANG AYOS: pre-entry allowlist; laktawan ang order na nasa
entry_orders_resolved na; LEGAL CHAIN MUNA
(_transition_recovered_primary_to_pending) bago ang bind; isang event kada
order id; kabiguan ay WARNING + `live_entry_fill_self_heal_failed` minsan kada
signature. Ang cooldown/held false positive ay istruktural na imposible.

Runnable: pytest tests/test_naked_position_0902_heal.py -v
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural.live_fsm import (
    assert_transition_live,
    can_transition_live,
)


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
    """get_order returns the (order, raw) TUPLE shape; no client-id lookup."""

    def __init__(self, order):
        self._order = order
        self.lookups = 0

    def get_order(self, oid):
        self.lookups += 1
        return (self._order, None)


class _AdapterPlain(_Adapter):
    def get_order(self, oid):
        self.lookups += 1
        return self._order


def _sess(state, le, *, family="alpaca_spot"):
    return SimpleNamespace(
        id=19471, symbol="CANF", state=state, execution_family=family, mode="live",
        correlation_id="corr", updated_at=None, ended_at=None,
        risk_snapshot_json={
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_symbol_claim_token": "tok-19471",
            "momentum_live_execution": dict(le),
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
    monkeypatch.setattr(LR.settings, "chili_momentum_entry_fill_self_heal_enabled", True, raising=False)
    return events


def _heal(sess, adapter):
    le = sess.risk_snapshot_json["momentum_live_execution"]
    out = LR._heal_unrecognized_entry_fill(_Db(), sess, adapter, le=le, product_id="CANF")
    return out, le


# ── 1/2: pre-entry states walk the legal chain to pending, ONE event ─────────


@pytest.mark.parametrize("state", ["watching_live", "live_entry_candidate", "queued_live"])
def test_pre_entry_state_heals_via_legal_chain(harness, state):
    sess = _sess(state, {"entry_submitted": True, "entry_order_id": "f3ed508d"})
    adapter = _Adapter(_Order())
    out, le = _heal(sess, adapter)
    assert out.get("healed") is True
    assert sess.state == "live_pending_entry"
    assert le["entry_order_id"] == "f3ed508d"
    assert le["entry_fill_self_heal_sig"] == "f3ed508d"
    healed = [p for et, p in harness if et == "live_entry_fill_self_healed"]
    assert len(healed) == 1
    assert healed[0]["chain_from"] == state
    assert healed[0]["state_before"] == state
    assert not [et for et, _ in harness if et == "live_entry_fill_self_heal_failed"]


# ── 3: held state with position None is NOT healable ────────────────────────


def test_live_entered_with_no_position_is_not_healed(harness):
    sess = _sess("live_entered", {"entry_submitted": True, "entry_order_id": "f3ed508d"})
    adapter = _Adapter(_Order())
    out, _ = _heal(sess, adapter)
    assert out == {}
    assert adapter.lookups == 0
    assert sess.state == "live_entered"
    assert harness == []


# ── 4: the completed-trade shape (cooldown / exited) never pages ─────────────


@pytest.mark.parametrize("state,resolved", [
    ("live_cooldown", {"f3ed508d": "adopted"}),
    ("live_exited", {"f3ed508d": "adopted"}),
    ("live_cooldown", None),
])
def test_completed_trade_shape_never_looks_up_or_emits(harness, state, resolved):
    le = {"entry_submitted": True, "entry_order_id": "f3ed508d", "position": None}
    if resolved is not None:
        le["entry_orders_resolved"] = resolved
    sess = _sess(state, le)
    adapter = _Adapter(_Order())
    out, _ = _heal(sess, adapter)
    assert out == {}
    assert adapter.lookups == 0
    assert sess.state == state
    assert harness == []


def test_cooldown_is_terminal_ish_for_heal():
    assert LR.STATE_LIVE_COOLDOWN in LR._TERMINAL_ISH_FOR_HEAL


# ── 5: an already-resolved order in a pre-entry state is left alone ──────────


def test_resolved_order_in_watching_is_not_healed(harness):
    sess = _sess("watching_live", {
        "entry_submitted": True, "entry_order_id": "f3ed508d",
        "entry_orders_resolved": {"f3ed508d": "adopted"},
    })
    adapter = _Adapter(_Order())
    out, _ = _heal(sess, adapter)
    assert out == {}
    assert adapter.lookups == 0
    assert harness == []


def test_all_history_resolved_without_active_pointer_is_not_healed(harness):
    sess = _sess("watching_live", {
        "entry_submitted": True, "entry_client_order_id": "chili-19471",
        "entry_order_ids_all": ["a", "b"],
        "entry_orders_resolved": {"a": "void", "b": "adopted"},
    })
    adapter = _Adapter(_Order())
    out, _ = _heal(sess, adapter)
    assert out == {}
    assert adapter.lookups == 0


# ── 6: tuple-safe get_order; plain-object get_order too ──────────────────────


def test_plain_get_order_return_also_heals(harness):
    sess = _sess("watching_live", {"entry_submitted": True, "entry_order_id": "f3ed508d"})
    out, _ = _heal(sess, _AdapterPlain(_Order()))
    assert out.get("healed") is True
    assert sess.state == "live_pending_entry"


def test_zero_fill_does_nothing(harness):
    sess = _sess("watching_live", {"entry_submitted": True, "entry_order_id": "f3ed508d"})
    out, _ = _heal(sess, _Adapter(_Order(filled=0.0, status="new")))
    assert out == {}
    assert sess.state == "watching_live"
    assert harness == []


# ── 7: dedupe on the order id — one event across two ticks ──────────────────


def test_second_tick_in_pending_does_not_re_emit(harness):
    sess = _sess("watching_live", {"entry_submitted": True, "entry_order_id": "f3ed508d"})
    adapter = _Adapter(_Order())
    out1, _ = _heal(sess, adapter)
    assert out1.get("healed") is True and sess.state == "live_pending_entry"
    out2, _ = _heal(sess, adapter)
    assert out2.get("healed") is True and out2.get("repeat") is True
    assert len([et for et, _ in harness if et == "live_entry_fill_self_healed"]) == 1


# ── 8: no legal chain => le untouched, ONE loud failure event ────────────────


def test_no_legal_chain_fails_loudly_once_and_leaves_le_untouched(harness, monkeypatch):
    monkeypatch.setattr(LR, "_transition_recovered_primary_to_pending", lambda db, sess: False)
    le0 = {"entry_submitted": True, "entry_order_id": "f3ed508d"}
    sess = _sess("watching_live", dict(le0))
    adapter = _Adapter(_Order())
    out, le = _heal(sess, adapter)
    assert out.get("heal_failed") is True
    assert out.get("exception_class") == "RuntimeError"
    assert sess.state == "watching_live"
    assert "entry_fill_self_heal_sig" not in le
    assert le["entry_order_id"] == "f3ed508d"
    assert "entry_order_ids_all" not in le, "walang bind kapag walang chain"
    failed = [p for et, p in harness if et == "live_entry_fill_self_heal_failed"]
    assert len(failed) == 1
    assert failed[0]["severity"] == "critical"
    assert failed[0]["error"].startswith("no_legal_chain_to_pending:watching_live")
    assert failed[0]["order_id"] == "f3ed508d"
    assert not [et for et, _ in harness if et == "live_entry_fill_self_healed"]
    # second tick, same signature => no second event
    out2, _ = _heal(sess, adapter)
    assert out2.get("heal_failed") is True
    assert len([et for et, _ in harness if et == "live_entry_fill_self_heal_failed"]) == 1


# ── 9: AST / structure guards ────────────────────────────────────────────────


def test_chain_precedes_bind_and_the_swallow_is_gone():
    src = inspect.getsource(LR._heal_unrecognized_entry_fill)
    assert src.index("_transition_recovered_primary_to_pending") < src.index("_bind_recovered_entry_order")
    assert "_HEALABLE_PRE_ENTRY_STATES" in src
    assert src.index("entry_orders_resolved") < src.index("_recover_entry_order_by_client_id")
    assert "live_entry_fill_self_heal_failed" in src
    i_exc = src.index("except Exception as exc")
    assert "_log.debug(" not in src[i_exc:]
    assert "_log.warning(" in src[i_exc:]
    assert "entry_fill_self_heal_sig" in src


def test_the_illegal_edge_is_still_illegal():
    """Walang bagong FSM edge — ang heal ay dapat dumaan sa chain."""
    assert not can_transition_live("watching_live", "live_pending_entry")
    assert can_transition_live("watching_live", "live_entry_candidate")
    assert can_transition_live("live_entry_candidate", "live_pending_entry")


def test_healable_states_are_exactly_the_pre_entry_set():
    assert LR._HEALABLE_PRE_ENTRY_STATES == frozenset({
        "armed_pending_runner", "queued_live", "watching_live",
        "live_entry_candidate", "live_pending_entry",
    })
