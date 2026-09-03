"""Behavioural half of the ledger-completeness regression.

Deliberately imports ONLY symbols that already exist on origin/main, so that on
main these fail as real ASSERTIONS about behaviour rather than as an ImportError
about a module that is not there yet. Verified 2026-09-02 at 89cb0eb: both tests
below fail on main and pass on this branch.

  test_a_filled_arm_expired_session_is_booked_at_all
      main: try_emit_momentum_session_feedback returns
            {'skipped': 'not_terminal_for_feedback', 'state': 'live_arm_expired'}
            and writes NO row. This is the mechanism behind 291 arm-expired
            sessions with 0 outcome rows and −$916.26 of unbooked real fills.

  test_a_session_whose_fill_was_never_adopted_is_not_booked_as_a_clean_zero
      main: books MOVE 19244's shape as cancelled_pre_entry / NULL P&L with no
            marker at all — a confidently wrong row that makes the ledger LOOK
            complete. (On main it does not even get that far, because the state
            is not terminal; the assertion that fails first is the same one.)
"""

from __future__ import annotations

from app.models.trading import MomentumAutomationOutcome, MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.momentum_neural.feedback_emit import (
    try_emit_momentum_session_feedback,
)
from app.services.trading.momentum_neural.outcome_extract import (
    session_terminal_for_feedback,
)

_seq = 0


def _session(db, *, state, le, symbol):
    global _seq
    _seq += 1
    v = MomentumStrategyVariant(
        family="test_family",
        variant_key=f"ledgerbehav_{_seq}",
        label="ledger behaviour variant",
        params_json={},
    )
    db.add(v)
    db.flush()
    sess = TradingAutomationSession(
        user_id=None,
        venue="test",
        execution_family="alpaca_spot",
        mode="live",
        symbol=symbol,
        variant_id=v.id,
        state=state,
        risk_snapshot_json={"momentum_live_execution": dict(le)},
        correlation_id="corr-ledgerbehav",
    )
    db.add(sess)
    db.flush()
    return sess


def test_arm_expired_is_terminal_for_the_ledger():
    """SSM 19315 ended here with a real −$25.98 round trip behind it."""
    assert session_terminal_for_feedback("live", "live_arm_expired") is True


def test_a_filled_arm_expired_session_is_booked_at_all(db):
    sess = _session(
        db,
        state="live_arm_expired",
        symbol="SSM",
        le={
            "realized_pnl_usd": -25.98,
            "last_exit_entry_price": 4.01,
            "last_exit_reason": "stop",
        },
    )
    result = try_emit_momentum_session_feedback(db, sess)
    db.flush()

    assert result.get("skipped") != "not_terminal_for_feedback", (
        "live_arm_expired is terminal in the ledger; refusing to book it is how "
        "17 filled sessions worth -$916.26 left no trace"
    )
    row = (
        db.query(MomentumAutomationOutcome)
        .filter(MomentumAutomationOutcome.session_id == int(sess.id))
        .one_or_none()
    )
    assert row is not None
    assert row.realized_pnl_usd is not None


def test_a_session_whose_fill_was_never_adopted_is_not_booked_as_a_clean_zero(db):
    """MOVE 19244: −$364.14 at the broker, an entry_order_id and nothing else here."""
    sess = _session(
        db,
        state="live_arm_expired",
        symbol="MOVE",
        le={"entry_order_id": "c3a60320-c282-4859-b24b-02ff55730a63"},
    )
    try_emit_momentum_session_feedback(db, sess)
    db.flush()

    row = (
        db.query(MomentumAutomationOutcome)
        .filter(MomentumAutomationOutcome.session_id == int(sess.id))
        .one_or_none()
    )
    assert row is not None, "the session must stop being invisible"
    stamp = (row.extracted_summary_json or {}).get("ledger_integrity_v1") or {}
    assert stamp.get("status") == "entry_evidence_unreconciled", (
        "a row with submission evidence but no fill proof must never be presented "
        "as an authoritative zero"
    )
    assert row.contributes_to_evolution is False
