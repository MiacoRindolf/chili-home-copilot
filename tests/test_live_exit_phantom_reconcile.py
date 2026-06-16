"""The SLNH/LION phantom-spin fix (2026-06-16).

A held session whose exit the broker-qty clamp CONFIRMED at zero (broker_zero)
must reconcile to EXITED instead of re-submitting ``no_remaining_quantity`` every
tick forever (SLNH sess 5033 spun for hours; LION sess 4996 the same), which pins
the slot and shows a phantom position + phantom unrealized P&L in the cockpit.
"""
from __future__ import annotations

import types

import app.services.trading.momentum_neural.live_runner as lr


def _patch(monkeypatch):
    calls = {"transition": [], "emit": [], "commit": 0}

    def _commit(sess, le):
        calls["commit"] += 1

    monkeypatch.setattr(lr, "_commit_le", _commit)
    monkeypatch.setattr(lr, "_safe_transition", lambda db, sess, state: calls["transition"].append(state))
    monkeypatch.setattr(lr, "_emit", lambda db, sess, ev, payload=None: calls["emit"].append(ev))
    return calls


def test_broker_zero_confirmed_reconciles_to_exited(monkeypatch):
    calls = _patch(monkeypatch)
    monkeypatch.setattr(lr, "_broker_position_confirms_zero", lambda sess: True)
    sess = types.SimpleNamespace(id=5033, symbol="SLNH", execution_family="robinhood_spot")
    le = {"position": {"qty": 100}, "pending_exit_reason": "max_hold"}
    out = lr._live_exit_submit_succeeded(
        None, sess,
        le=le,
        result={"ok": False, "error": "no_remaining_quantity", "noop": True, "broker_zero": True},
        reason="max_hold",
    )
    assert out is True
    assert lr.STATE_LIVE_EXITED in calls["transition"]
    assert le["position"] is None
    assert str(le["last_exit_reason"]).endswith("_broker_zero_reconcile")
    assert "live_exit_reconciled_broker_zero" in calls["emit"]
    # the phantom must NOT spin / record a failed submit
    assert "live_exit_submit_failed" not in calls["emit"]
    assert le.get("pending_exit_reason") is None


def test_broker_zero_but_confirm_read_disagrees_does_not_close(monkeypatch):
    # a one-off spurious 0: the clamp said broker_zero but the second independent
    # broker read still sees the position -> must NOT close (fall through to the
    # safe retry/failed path so a real position is never falsely flattened).
    calls = _patch(monkeypatch)
    monkeypatch.setattr(lr, "_broker_position_confirms_zero", lambda sess: False)
    sess = types.SimpleNamespace(id=5033, symbol="SLNH", execution_family="robinhood_spot")
    le = {"position": {"qty": 100}}
    out = lr._live_exit_submit_succeeded(
        None, sess, le=le,
        result={"ok": False, "error": "no_remaining_quantity", "broker_zero": True},
        reason="max_hold",
    )
    assert out is False
    assert lr.STATE_LIVE_EXITED not in calls["transition"]
    assert le["position"] == {"qty": 100}  # untouched
    assert "live_exit_submit_failed" in calls["emit"]


def test_plain_no_remaining_quantity_without_broker_zero_does_not_close(monkeypatch):
    # the scale-limit-consumed noop (live_runner.py:498) returns no_remaining_quantity
    # WITHOUT broker_zero -> the phantom reconcile must require the confirmed flag and
    # NOT fire on it (even though the confirming read would say zero).
    calls = _patch(monkeypatch)
    monkeypatch.setattr(lr, "_broker_position_confirms_zero", lambda sess: True)
    sess = types.SimpleNamespace(id=1, symbol="SLNH", execution_family="robinhood_spot")
    le = {"position": {"qty": 100}}
    out = lr._live_exit_submit_succeeded(
        None, sess, le=le,
        result={"ok": False, "error": "no_remaining_quantity", "noop": True},
        reason="scale",
    )
    assert out is False
    assert lr.STATE_LIVE_EXITED not in calls["transition"]
    assert "live_exit_submit_failed" in calls["emit"]


def test_crypto_broker_zero_also_reconciles(monkeypatch):
    # family-agnostic: a coinbase broker_zero reconciles too (crypto parity).
    calls = _patch(monkeypatch)
    monkeypatch.setattr(lr, "_broker_position_confirms_zero", lambda sess: True)
    sess = types.SimpleNamespace(id=9, symbol="TAO-USD", execution_family="coinbase_spot")
    le = {"position": {"qty": 1.0}}
    out = lr._live_exit_submit_succeeded(
        None, sess, le=le,
        result={"ok": False, "error": "no_remaining_quantity", "broker_zero": True},
        reason="trail_stop",
    )
    assert out is True
    assert lr.STATE_LIVE_EXITED in calls["transition"]
    assert le["position"] is None
