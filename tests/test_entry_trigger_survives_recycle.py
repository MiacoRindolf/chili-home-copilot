"""The trade's entry trigger must still be readable after the recycle.

Every post-hoc reader runs at SESSION end, after any recycle, and the recycle
clears `entry_trigger_reason` — correctly, so the next trade cannot inherit the
last one's identity. The result: 230 of 269 live trades carrying -$8,658 record
NO entry trigger at all, so "is the trigger vocabulary break-only?" cannot be
asked of the trades themselves. Only ~39 trades name a trigger.

Same defect shape as the decision packet ([16], d466e77ef), and the same fix: a
durable copy that the recycle does not touch.

DB-free.
"""
from __future__ import annotations

from app.services.trading.momentum_neural.live_runner import (
    _RECYCLE_ENTRY_STATE_KEYS,
    _persist_entry_trigger_identity,
)


def test_the_per_decision_keys_are_still_cleared_on_recycle():
    """The clear is correct — the next trade must not inherit an identity."""
    assert "entry_trigger_reason" in _RECYCLE_ENTRY_STATE_KEYS
    assert "entry_trigger_debug" in _RECYCLE_ENTRY_STATE_KEYS


def test_the_durable_copies_are_never_cleared():
    """...which is exactly why the evidence copy must outlive it."""
    assert "last_entry_trigger_reason" not in _RECYCLE_ENTRY_STATE_KEYS
    assert "last_entry_trigger_debug" not in _RECYCLE_ENTRY_STATE_KEYS


def test_the_trigger_survives_a_recycle():
    le: dict = {}
    _persist_entry_trigger_identity(
        le, reason="tick_first_pullback_scalp", debug={"dip_low": 11.6}
    )
    assert le["entry_trigger_reason"] == "tick_first_pullback_scalp"

    for key in _RECYCLE_ENTRY_STATE_KEYS:      # the recycle, faithfully
        le.pop(key, None)

    assert "entry_trigger_reason" not in le
    assert le["last_entry_trigger_reason"] == "tick_first_pullback_scalp"
    assert le["last_entry_trigger_debug"] == {"dip_low": 11.6}


def test_a_later_trade_overwrites_the_evidence_of_the_earlier_one():
    """The durable copy tracks the LAST trade, not the first — it is a snapshot,
    not a log, and a reader at session end wants the trade that just closed."""
    le: dict = {}
    _persist_entry_trigger_identity(le, reason="abcd_break_tick_ok", debug={"n": 1})
    _persist_entry_trigger_identity(le, reason="wick_reclaim", debug={"n": 2})
    assert le["last_entry_trigger_reason"] == "wick_reclaim"
    assert le["last_entry_trigger_debug"] == {"n": 2}


def test_an_empty_reason_does_not_erase_a_real_one():
    """A blank pass must not wipe the evidence of the trade that did trigger."""
    le: dict = {}
    _persist_entry_trigger_identity(le, reason="vwap_reclaim", debug={"a": 1})
    _persist_entry_trigger_identity(le, reason=None, debug=None)
    assert le["entry_trigger_reason"] == ""          # per-decision: cleared
    assert le["last_entry_trigger_reason"] == "vwap_reclaim"   # evidence: kept


def test_the_debug_copy_is_deep_not_aliased():
    """A later mutation of the caller's dict must not rewrite recorded history."""
    debug = {"levels": [1.0]}
    le: dict = {}
    _persist_entry_trigger_identity(le, reason="orb_break", debug=debug)
    debug["levels"].append(2.0)
    assert le["last_entry_trigger_debug"] == {"levels": [1.0]}


def test_a_non_dict_debug_is_normalised():
    le: dict = {}
    _persist_entry_trigger_identity(le, reason="momentum_ok_tick_surge", debug="oops")
    assert le["last_entry_trigger_debug"] == {}
