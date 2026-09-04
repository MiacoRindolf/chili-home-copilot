"""A pending exit with no order id must never spin unbounded (ANPA 19771, 2026-09-04).

CHILI's first-ever ``live_burst_window_exit`` decided for ANPA at 08:50:40Z and its
submit DEFERRED on stand-in pricing — which by design leaves ``pending_exit_reason``
set. From the next pulse the POLL path owned the session, and its missing-order-id
branch returned "pending" unconditionally: **5,656 emissions over 5h11m**, with no
attempt counter, no backoff, no escalation, and — decisively — it never reached the
broker-zero reconciler ~50 lines below, so nothing noticed the broker had gone flat.

The deadman stop was inert until RTH (Alpaca accepts only limit orders in extended
hours), so the runner was the SOLE protection and it was spinning on a naked 49-share
position. Realised −$37.25 at the open, 44c / 985bps below the 4.47 stop, against
−$1.96 had the burst exit actually placed: **$35.29 for one unbounded ``return``**.
The session was still ``live_entered`` 4h29m after the broker went flat.

The machinery was never missing. ``_live_exit_submit_succeeded`` already computes
``missing_order_id`` and routes it to the broker-zero reconcile or to a recorded
failure that clears ``pending_exit_reason``. This path simply never called it.
"""
from __future__ import annotations

import types
from datetime import timedelta

import app.services.trading.momentum_neural.live_runner as lr


def _patch(monkeypatch):
    calls = {"transition": [], "emit": [], "payloads": {}}

    monkeypatch.setattr(lr, "_commit_le", lambda sess, le: None)
    monkeypatch.setattr(lr, "_safe_transition", lambda db, sess, state: calls["transition"].append(state))

    def _emit(db, sess, ev, payload=None):
        calls["emit"].append(ev)
        calls["payloads"][ev] = payload or {}

    monkeypatch.setattr(lr, "_emit", _emit)
    return calls


def _sess():
    return types.SimpleNamespace(id=19771, symbol="ANPA", execution_family="alpaca_spot")


def _le(*, age_seconds: float | None, attempts: int = 0):
    """A held position with a pending exit and NO exit_order_id — the ANPA shape."""
    le = {
        "position": {"quantity": 49.0, "avg_entry_price": 4.79},
        "pending_exit_reason": "burst_window_exit",
        "pending_exit_quantity": 49.0,
        "exit_submit_attempts": attempts,
    }
    if age_seconds is not None:
        stamped = lr._utcnow() - timedelta(seconds=age_seconds)
        le["pending_exit_submitted_at_utc"] = stamped.isoformat()
    return le


def _grace(attempts: int = 0) -> float:
    return max(
        lr._exit_submit_backoff_seconds(max(1, attempts)),
        lr._EXIT_SUBMIT_BACKOFF_BASE_SECONDS,
    )


def test_inside_grace_stays_pending_and_does_not_escalate(monkeypatch):
    """A submit genuinely in flight has no order id for a pulse or two — leave it be."""
    calls = _patch(monkeypatch)
    le = _le(age_seconds=0.0)

    out = lr._poll_live_exit_fill(
        None, _sess(), None, le=le, reason="burst_window_exit", quantity=49.0,
    )

    assert out["pending"] is True
    assert out["why"] == "missing_exit_order_id"
    assert "live_exit_pending_unconfirmed" in calls["emit"]
    assert "live_exit_order_id_lost" not in calls["emit"]
    # the pending exit is untouched, so the submit path keeps its retry state
    assert le["pending_exit_reason"] == "burst_window_exit"


def test_escalation_never_fabricates_a_broker_zero_claim(monkeypatch):
    """The escalation must NOT assert the broker is flat — it has not read the broker.

    Reconciling a session to EXITED is how a position leaves CHILI's book, and
    ``_live_exit_submit_succeeded`` only does it on a result carrying ``broker_zero``,
    i.e. a clamp read that actually came back zero (see
    test_live_exit_phantom_reconcile). This path has done no such read, so it passes a
    plain failure and lets the RE-SUBMIT obtain the real answer. Were it to synthesise
    ``broker_zero`` to close the ghost faster, a still-open position would be dropped
    from the book — strictly worse than the bug being fixed.
    """
    calls = _patch(monkeypatch)
    monkeypatch.setattr(lr.settings, "chili_momentum_broker_zero_confirm_reads", 1)
    monkeypatch.setattr(lr.settings, "chili_momentum_broker_zero_trust_clamp_enabled", True)
    # Even with the broker genuinely flat, this path must not shortcut to EXITED.
    monkeypatch.setattr(lr, "_broker_position_confirms_zero", lambda sess: True)
    le = _le(age_seconds=_grace() + 5.0)

    lr._poll_live_exit_fill(
        None, _sess(), None, le=le, reason="burst_window_exit", quantity=49.0,
    )

    assert "live_exit_order_id_lost" in calls["emit"]
    assert lr.STATE_LIVE_EXITED not in calls["transition"]
    assert le["position"] == {"quantity": 49.0, "avg_entry_price": 4.79}
    # What it DOES do is release the stuck pending exit, so the next pulse runs the
    # fully-guarded submit path, which reads the broker and reconciles for real.
    assert "pending_exit_reason" not in le


def test_past_grace_with_a_live_position_clears_pending_so_the_exit_can_refire(monkeypatch):
    """Broker still holds ⇒ record the failure and release the pending exit.

    Leaving ``pending_exit_reason`` set is what handed the session to the poll path in
    the first place; clearing it is what lets the exit decision fire again.
    """
    calls = _patch(monkeypatch)
    monkeypatch.setattr(lr, "_broker_position_confirms_zero", lambda sess: False)
    le = _le(age_seconds=_grace() + 5.0)

    lr._poll_live_exit_fill(
        None, _sess(), None, le=le, reason="burst_window_exit", quantity=49.0,
    )

    assert "live_exit_order_id_lost" in calls["emit"]
    assert "live_exit_submit_failed" in calls["emit"]
    assert "pending_exit_reason" not in le, "a stuck pending exit must be released"
    assert le["last_exit_submit_failed"]["result"]["error"] == "missing_exit_order_id"
    # the position is NOT touched — only the broker-confirmed-flat path may do that
    assert le["position"] == {"quantity": 49.0, "avg_entry_price": 4.79}


def test_missing_stamp_is_written_rather_than_spinning_forever(monkeypatch):
    """No timestamp must not mean no clock — otherwise the unbounded spin returns."""
    calls = _patch(monkeypatch)
    le = _le(age_seconds=None)
    assert "pending_exit_submitted_at_utc" not in le

    lr._poll_live_exit_fill(
        None, _sess(), None, le=le, reason="burst_window_exit", quantity=49.0,
    )

    assert le.get("pending_exit_submitted_at_utc"), "the stamp must be written on first sight"
    assert "live_exit_order_id_lost" not in calls["emit"], "first pulse only stamps"

    # ...and the NEXT pulse, once the grace has elapsed, escalates.
    le["pending_exit_submitted_at_utc"] = (
        lr._utcnow() - timedelta(seconds=_grace() + 5.0)
    ).isoformat()
    monkeypatch.setattr(lr, "_broker_position_confirms_zero", lambda sess: False)
    lr._poll_live_exit_fill(
        None, _sess(), None, le=le, reason="burst_window_exit", quantity=49.0,
    )
    assert "live_exit_order_id_lost" in calls["emit"]


def test_grace_tracks_the_submit_backoff_schedule(monkeypatch):
    """The grace is DERIVED from the same backoff the submit path uses.

    Pinning this stops the two drifting apart — a grace shorter than the backoff would
    escalate while a retry is legitimately still waiting its turn.
    """
    calls = _patch(monkeypatch)
    monkeypatch.setattr(lr, "_broker_position_confirms_zero", lambda sess: False)
    attempts = 3
    le = _le(age_seconds=_grace(attempts) - 1.0, attempts=attempts)

    lr._poll_live_exit_fill(
        None, _sess(), None, le=le, reason="burst_window_exit", quantity=49.0,
    )
    assert "live_exit_order_id_lost" not in calls["emit"]
    assert calls["payloads"]["live_exit_pending_unconfirmed"]["grace_seconds"] == round(
        _grace(attempts), 2
    )


def test_the_anpa_spin_is_now_impossible(monkeypatch):
    """5,656 unbounded polls must not be reachable: escalation happens once, early."""
    calls = _patch(monkeypatch)
    monkeypatch.setattr(lr, "_broker_position_confirms_zero", lambda sess: False)
    le = _le(age_seconds=None)

    for _ in range(50):
        if "pending_exit_reason" not in le:
            break
        lr._poll_live_exit_fill(
            None, _sess(), None, le=le, reason="burst_window_exit", quantity=49.0,
        )
        # age the stamp so the clock advances the way real pulses would
        le["pending_exit_submitted_at_utc"] = (
            lr._utcnow() - timedelta(seconds=_grace() + 1.0)
        ).isoformat()

    assert "live_exit_order_id_lost" in calls["emit"]
    assert "pending_exit_reason" not in le
    assert calls["emit"].count("live_exit_pending_unconfirmed") < 5, (
        "the poll path must escalate within a couple of pulses, not thousands"
    )
