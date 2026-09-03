"""Behavioural half of the ledger-completeness regression.

Deliberately imports ONLY symbols that already exist on origin/main, so that on
main these fail as real ASSERTIONS about behaviour rather than as an ImportError
about a module that is not there yet. Verified 2026-09-02 at 89cb0eb: every test
below fails on main and passes on this branch.

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

  test_the_designated_backfill_books_a_filled_arm_expired_session
      main: scan_terminal_sessions_missing_feedback's hand-rolled terminal_states
            tuple omits live_arm_expired, so the ONE function designed to find
            these rows emits nothing. Replaces an earlier test that asserted on
            inspect.getsource() text and would have passed on a comment.

  test_loss_guard_gaps_on_a_session_whose_fill_it_cannot_see
      main: MOVE 19244's shape (entry_order_id set, entry_submitted true, no fill
            event, no realized P&L, no outcome row) is selected by nothing and
            proves nothing, so the guard reports a clean day and the account keeps
            trading on books that are short $364.14. Both halves are needed: the
            state must be SELECTED and submission evidence must count as DURABLE.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.models.core import User
from app.models.trading import MomentumAutomationOutcome, MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.momentum_neural import risk_policy
from app.services.trading.momentum_neural.feedback_emit import (
    scan_terminal_sessions_missing_feedback,
    try_emit_momentum_session_feedback,
)
from app.services.trading.momentum_neural.outcome_extract import (
    session_terminal_for_feedback,
)

_seq = 0

# The real paper account the audited window ran against, so the guard's
# account-generation check resolves to "current" rather than "unknown".
_ACCOUNT_ID = "c7d421e0-4fae-4219-9503-5ce051d4d923"
_GUARD_USER_ID = 9102


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


def _guard_session(db, *, state, le, symbol, terminal_at):
    """A session shaped for the loss guard: real user, bound account generation."""
    global _seq
    _seq += 1
    if db.get(User, _GUARD_USER_ID) is None:
        db.add(User(id=_GUARD_USER_ID, name="ledger-guard-user"))
        db.flush()
    v = MomentumStrategyVariant(
        family="test_family",
        variant_key=f"ledgerguard_{_seq}",
        label="ledger guard variant",
        params_json={},
    )
    db.add(v)
    db.flush()
    sess = TradingAutomationSession(
        user_id=_GUARD_USER_ID,
        venue="test",
        execution_family="alpaca_spot",
        mode="live",
        symbol=symbol,
        variant_id=v.id,
        state=state,
        risk_snapshot_json={
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": _ACCOUNT_ID,
            "momentum_live_execution": dict(le),
        },
        correlation_id="corr-ledgerguard",
        started_at=terminal_at - timedelta(minutes=30),
        ended_at=terminal_at,
        created_at=terminal_at - timedelta(minutes=30),
        updated_at=terminal_at,
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


def test_the_designated_backfill_books_a_filled_arm_expired_session(db):
    """The scanner is the ONE function designed for this defect. Prove it fires.

    This replaces a test that read ``inspect.getsource(...)`` and asserted a constant
    NAME appeared in it — which would have passed on a comment and failed on a
    harmless alias. The scanner had two independent failures (a blind state tuple AND
    zero callers); only a behavioural assertion can tell you the first one is fixed.

    Called with main's own signature (no ``session_ids``) so the failure on main is
    the BEHAVIOUR — emitted 0 because the state tuple is blind — and not a TypeError
    about a parameter that does not exist there yet. The truncating ``db`` fixture
    means this session is the only candidate in the table.
    """
    sess = _guard_session(
        db,
        state="live_arm_expired",
        symbol="SSM",
        le={"realized_pnl_usd": -25.98, "last_exit_entry_price": 4.01},
        terminal_at=datetime.utcnow() - timedelta(hours=2),
    )
    result = scan_terminal_sessions_missing_feedback(db, limit=10)
    db.flush()

    assert result.get("emitted") == 1, (
        "the designated backfill must reach live_arm_expired — its hand-rolled "
        f"state tuple omitting that state is why 291 sessions stayed unbooked: {result}"
    )
    row = (
        db.query(MomentumAutomationOutcome)
        .filter(MomentumAutomationOutcome.session_id == int(sess.id))
        .one_or_none()
    )
    assert row is not None
    assert row.realized_pnl_usd is not None

    # Idempotent: a second pass must not double-book (UNIQUE(session_id) + dedupe).
    again = scan_terminal_sessions_missing_feedback(db, limit=10)
    assert again.get("emitted") == 0


def test_loss_guard_gaps_on_a_session_whose_fill_it_cannot_see(db):
    """MOVE 19244 exactly: the broker filled it, nothing here can prove that.

    Broker order c3a60320-c282-4859-b24b-02ff55730a63, BUY 119 @ 15.05 at
    2026-08-31T13:16:34Z, closed at 11.99 for −$364.14 — the largest loss of the
    audited window. In the DB the session has an entry_order_id, ``entry_submitted``
    true, NO fill event, NO realized_pnl_usd, NO last_exit_entry_price and NO outcome
    row. Every fill-level proof the guard had reads False, so on main the day is
    reported clean and the lane keeps sizing against books short by $364.14.

    Selecting ``live_arm_expired`` alone does NOT fix this — the durability test that
    runs next is also fill-level. Both halves must hold for this to pass.
    """
    terminal_at = datetime.utcnow() - timedelta(minutes=45)
    sess = _guard_session(
        db,
        state="live_arm_expired",
        symbol="MOVE",
        le={
            "entry_order_id": "c3a60320-c282-4859-b24b-02ff55730a63",
            "entry_submitted": True,
        },
        terminal_at=terminal_at,
    )

    entries, meta = risk_policy.load_current_live_loss_history(
        db,
        user_id=_GUARD_USER_ID,
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
        account_identity=_ACCOUNT_ID,
        decision_as_of=datetime.utcnow(),
    )

    assert meta.get("history_unavailable") is True, (
        "an entry order that reached the broker with no visible outcome must make "
        "the guard say it cannot see, not report a clean day"
    )
    assert meta.get("coverage_grade") == "COVERAGE_UNAVAILABLE"
    assert int(sess.id) in (meta.get("coverage_gap_session_ids") or []), (
        f"session {sess.id} must be the reason the day is ungraded: {meta}"
    )
    assert (meta.get("coverage_gap_counts") or {}).get(
        "loss_guard_terminal_outcome_unavailable"
    ), meta
    assert entries == ()


def test_loss_guard_stays_quiet_for_a_session_that_truly_did_nothing(db):
    """The negative control: fail-loud must not mean fail-always.

    3,687 of 3,691 outcome rows in the window are clean pre-entry cancels. A session
    with no order id, no submission flag and no events is not a blind spot — it is a
    fact — and turning it into an account-wide halt would be alarm fatigue on the one
    alarm that matters.
    """
    _guard_session(
        db,
        state="live_arm_expired",
        symbol="QUIET",
        le={},
        terminal_at=datetime.utcnow() - timedelta(minutes=45),
    )

    entries, meta = risk_policy.load_current_live_loss_history(
        db,
        user_id=_GUARD_USER_ID,
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
        account_identity=_ACCOUNT_ID,
        decision_as_of=datetime.utcnow(),
    )

    assert not meta.get("history_unavailable"), meta
    assert entries == ()
