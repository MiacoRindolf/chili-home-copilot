"""ROSS RISK GAP 3 — account-wide consecutive-loss ARM HALT (the tilt rule).

Ross's tilt rule: 2-3 reds in a row = walk away. The streak dial only de-SIZES (never halts),
the COUNT day-blocks are PER-SYMBOL, and the account-wide halts are DOLLAR-based — so N small
losses across N different tickers trip no halt (death by a thousand papercuts).

``account_wide_consecutive_losses`` counts the run across all symbols in one exact
user/family/account generation over TODAY's ET session (most-recent first).
``consecutive_loss_halt_decision`` turns it into a HALT once >= the configured count.

  (a) N consecutive account-wide losses -> halt;
  (b) a WIN resets the run -> no halt;
  (c) flag OFF -> never halts (byte-identical);
  (d) exits are NOT blocked (the decision is a read used ONLY in the arm guard).
"""
from __future__ import annotations

import datetime as _dt

import pytest

from app.config import settings
from app.models.core import User
from app.models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    TradingAutomationEvent,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural.risk_policy import (
    account_wide_consecutive_losses,
    consecutive_loss_halt_decision,
)

_HALT_N = int(settings.chili_momentum_consecutive_loss_halt_count)  # 4
_USER_ID = 8123
_FAMILY = "robinhood_spot"
_ACCOUNT_IDENTITY = "test-robinhood-account-v1"
_UNSET = object()

_seq = 0


def _variant_id(db):
    """A fresh strategy variant (unique key) to satisfy the session FK (NOT NULL variant_id)."""
    global _seq
    _seq += 1
    v = MomentumStrategyVariant(
        family="reclaim", variant_key=f"cl_{_seq}_{id(db) & 0xffff}", label="consec-loss test",
        params_json={}, is_active=True,
    )
    db.add(v)
    db.flush()
    return v.id


def _seed(db, *, pnl, oc="stop_loss", fam=_FAMILY, symbol="AAA", mins_ago=1,
          variant_id=None, user_id=_USER_ID, account_id=_UNSET, terminal_at=None,
          created_at=None, session_created_at=None,
          broker_pnl=_UNSET, broker_status=_UNSET,
          broker_reconciled_at=_UNSET, broker_return_bps=_UNSET,
          broker_notional=_UNSET, broker_win=_UNSET,
          legacy_return_bps=_UNSET):
    """Seed one terminated LIVE outcome today. ``mins_ago`` controls recency (smaller =
    more recent) so we can order the consecutive run deterministically."""
    global _seq
    _seq += 1
    if variant_id is None:
        variant_id = _variant_id(db)
    if db.get(User, user_id) is None:
        db.add(User(id=user_id, name=f"loss-guard-user-{user_id}"))
        db.flush()
    resolved_terminal_at = (
        terminal_at
        if terminal_at is not None
        else _dt.datetime.utcnow() - _dt.timedelta(minutes=mins_ago)
    )
    resolved_account_id = (
        _ACCOUNT_IDENTITY if account_id is _UNSET else account_id
    )
    if fam in {"alpaca_spot", "alpaca_short"}:
        account_snapshot = (
            {
                "alpaca_account_scope": "alpaca:paper",
                "alpaca_account_id": resolved_account_id,
            }
            if resolved_account_id is not None
            else {}
        )
    else:
        account_snapshot = (
            {"non_alpaca_account_identity": resolved_account_id}
            if resolved_account_id is not None
            else {}
        )
    resolved_created_at = created_at or resolved_terminal_at
    resolved_session_created_at = (
        session_created_at
        or resolved_terminal_at - _dt.timedelta(minutes=5)
    )
    sess = TradingAutomationSession(
        user_id=user_id, venue="test", execution_family=fam, mode="live",
        symbol=symbol, variant_id=variant_id, state="live_finished",
        risk_snapshot_json=account_snapshot,
        correlation_id=f"corr-cl-{_seq}",
        started_at=resolved_session_created_at,
        ended_at=resolved_terminal_at,
        created_at=resolved_session_created_at,
        updated_at=resolved_terminal_at,
    )
    db.add(sess)
    db.flush()
    always_pre_entry = oc in {
        "archived",
        "cancelled_pre_entry",
        "expired_pre_run",
        "no_fill",
        "risk_block",
    }
    resolved_broker_pnl = (
        (None if always_pre_entry else pnl)
        if broker_pnl is _UNSET
        else broker_pnl
    )
    resolved_broker_status = (
        (None if always_pre_entry else "reconciled")
        if broker_status is _UNSET
        else broker_status
    )
    resolved_broker_at = (
        (None if always_pre_entry else resolved_terminal_at)
        if broker_reconciled_at is _UNSET
        else broker_reconciled_at
    )
    resolved_broker_bps = (
        (None if always_pre_entry else float(pnl or 0.0) * 10.0)
        if broker_return_bps is _UNSET
        else broker_return_bps
    )
    o = MomentumAutomationOutcome(
        session_id=sess.id, user_id=user_id, variant_id=variant_id, symbol=symbol, mode="live",
        execution_family=fam, terminal_state="live_finished",
        terminal_at=resolved_terminal_at,
        created_at=resolved_created_at,
        outcome_class=oc, realized_pnl_usd=pnl,
        return_bps=(None if legacy_return_bps is _UNSET else legacy_return_bps),
        broker_recon_status=resolved_broker_status,
        broker_realized_pnl_usd=resolved_broker_pnl,
        broker_return_bps=resolved_broker_bps,
        broker_reconciled_at=resolved_broker_at,
        broker_notional_basis_usd=(
            None if broker_notional is _UNSET else broker_notional
        ),
        broker_win=(None if broker_win is _UNSET else broker_win),
    )
    db.add(o)
    db.flush()
    return o


def _count(db, **kwargs):
    return account_wide_consecutive_losses(
        db,
        user_id=kwargs.pop("user_id", _USER_ID),
        execution_family=kwargs.pop("execution_family", _FAMILY),
        account_identity=kwargs.pop("account_identity", _ACCOUNT_IDENTITY),
        **kwargs,
    )


def _halt(db, **kwargs):
    return consecutive_loss_halt_decision(
        db,
        user_id=kwargs.pop("user_id", _USER_ID),
        execution_family=kwargs.pop("execution_family", _FAMILY),
        account_identity=kwargs.pop("account_identity", _ACCOUNT_IDENTITY),
        **kwargs,
    )


def test_no_history_no_halt(db):
    consec, meta = _count(db)
    assert consec == 0
    halted, _ = _halt(db)
    assert halted is False


def test_n_consecutive_losses_halt(db):
    # (a) N losses across N DIFFERENT tickers in this account/family -> halt.
    for i in range(_HALT_N):
        _seed(db, pnl=-5.0, symbol=f"S{i}", mins_ago=_HALT_N - i)
    db.commit()
    consec, meta = _count(db)
    assert consec >= _HALT_N
    halted, hmeta = _halt(db)
    assert halted is True
    assert hmeta["consecutive_losses"] >= _HALT_N
    assert hmeta["halt_count"] == _HALT_N


def test_below_threshold_no_halt(db):
    # N-1 consecutive losses -> under the bar -> no halt.
    for i in range(_HALT_N - 1):
        _seed(db, pnl=-5.0, symbol=f"S{i}", mins_ago=_HALT_N - i)
    db.commit()
    consec, _ = _count(db)
    assert consec == _HALT_N - 1
    halted, _ = _halt(db)
    assert halted is False


def test_a_win_resets_the_run(db):
    # (b) The MOST-RECENT outcome is a WIN -> the consecutive-loss run is 0 even with many
    # older losses -> no halt. (Most-recent-first; the win breaks the run immediately.)
    # Older losses (further back in time).
    for i in range(_HALT_N + 2):
        _seed(db, pnl=-5.0, symbol=f"OLD{i}", mins_ago=100 + i)
    # The newest outcome is a WIN.
    _seed(db, pnl=+12.0, oc="success", symbol="WIN", mins_ago=1)
    db.commit()
    consec, _ = _count(db)
    assert consec == 0
    halted, _ = _halt(db)
    assert halted is False


def test_never_entered_rows_do_not_count_or_reset(db):
    # Never-entered churn (cancelled_pre_entry / no_fill, realized=0.0) sits between losses
    # in time but must NEITHER count as a loss NOR reset the consecutive run.
    # Newest: a never-entered cancel; then N real losses behind it.
    _seed(db, pnl=0.0, oc="cancelled_pre_entry", symbol="CXL", mins_ago=1)
    _seed(db, pnl=0.0, oc="no_fill", symbol="NOF", mins_ago=2)
    for i in range(_HALT_N):
        _seed(db, pnl=-5.0, symbol=f"L{i}", mins_ago=10 + i)
    db.commit()
    consec, _ = _count(db)
    # The two never-entered rows are pruned -> the run is the N real losses behind them.
    assert consec >= _HALT_N
    halted, _ = _halt(db)
    assert halted is True


def test_never_entered_overflow_cannot_hide_real_loss_streak(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    variant_id = _variant_id(db)
    for i in range(_HALT_N):
        _seed(
            db,
            pnl=-5.0,
            symbol=f"LOSS{i}",
            variant_id=variant_id,
            terminal_at=frontier - _dt.timedelta(seconds=120 + i),
        )
    # More than the raw lookback used to push every real loss out of the query
    # before the Python never-entered filter ran.
    for i in range(41):
        _seed(
            db,
            pnl=0.0,
            oc="no_fill",
            symbol=f"NF{i}",
            variant_id=variant_id,
            terminal_at=frontier - _dt.timedelta(seconds=1 + i),
        )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier, lookback=40)
    assert consec == _HALT_N
    assert meta["real_entries_today_seen"] == _HALT_N


def test_equal_terminal_time_uses_outcome_id_as_deterministic_tiebreak(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    tied = frontier - _dt.timedelta(minutes=1)
    variant_id = _variant_id(db)
    _seed(
        db,
        pnl=-5.0,
        symbol="LOSS",
        variant_id=variant_id,
        terminal_at=tied,
    )
    # Inserted second => larger outcome id => newest ledger row for an equal event
    # timestamp.  It must deterministically reset the loss run.
    _seed(
        db,
        pnl=5.0,
        oc="success",
        symbol="WIN",
        variant_id=variant_id,
        terminal_at=tied,
    )
    db.commit()

    assert _count(db, decision_as_of=frontier)[0] == 0
    assert _count(db, decision_as_of=frontier)[0] == 0


def test_backfilled_outcome_is_coverage_unavailable_before_created_at(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    variant_id = _variant_id(db)
    _seed(
        db,
        pnl=-5.0,
        symbol="AVAILABLE",
        variant_id=variant_id,
        terminal_at=frontier - _dt.timedelta(minutes=2),
        created_at=frontier - _dt.timedelta(minutes=1),
    )
    _seed(
        db,
        pnl=-5.0,
        symbol="LATE_BACKFILL",
        variant_id=variant_id,
        terminal_at=frontier - _dt.timedelta(minutes=3),
        created_at=frontier + _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_terminal_outcome_unavailable"


def test_broker_truth_not_legacy_self_report_controls_loss_streak(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    variant_id = _variant_id(db)
    for i in range(_HALT_N):
        _seed(
            db,
            pnl=100.0,  # legacy lane self-report says win
            broker_pnl=-5.0,  # reconciled broker truth says loss
            broker_return_bps=-50.0,
            symbol=f"DIVERGE{i}",
            variant_id=variant_id,
            terminal_at=frontier - _dt.timedelta(minutes=1 + i),
        )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == _HALT_N
    assert meta["label_source"] == "momentum_automation_outcomes.broker_*"
    assert meta["legacy_pnl_fallback_used"] is False


def test_broker_reconciliation_must_be_available_at_frontier(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=-5.0,
        broker_pnl=-5.0,
        broker_reconciled_at=frontier + _dt.timedelta(seconds=1),
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_broker_label_not_available_as_of"


def test_entered_terminal_session_missing_outcome_is_coverage_unavailable(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    variant_id = _variant_id(db)
    if db.get(User, _USER_ID) is None:
        db.add(User(id=_USER_ID, name=f"loss-guard-user-{_USER_ID}"))
        db.flush()
    sess = TradingAutomationSession(
        user_id=_USER_ID,
        venue="test",
        execution_family=_FAMILY,
        mode="live",
        symbol="MISSING",
        variant_id=variant_id,
        state="live_finished",
        risk_snapshot_json={
            "non_alpaca_account_identity": _ACCOUNT_IDENTITY,
            "momentum_live_execution": {"realized_pnl_usd": -12.0},
        },
        correlation_id="missing-outcome-loss-guard",
        started_at=frontier - _dt.timedelta(minutes=3),
        ended_at=frontier - _dt.timedelta(minutes=1),
        created_at=frontier - _dt.timedelta(minutes=3),
        updated_at=frontier - _dt.timedelta(minutes=1),
    )
    db.add(sess)
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_terminal_outcome_unavailable"


def test_recent_live_exited_without_outcome_is_coverage_unavailable(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    variant_id = _variant_id(db)
    if db.get(User, _USER_ID) is None:
        db.add(User(id=_USER_ID, name=f"loss-guard-user-{_USER_ID}"))
        db.flush()
    session = TradingAutomationSession(
        user_id=_USER_ID,
        venue="test",
        execution_family=_FAMILY,
        mode="live",
        symbol="RECENTEXIT",
        variant_id=variant_id,
        state="live_exited",
        risk_snapshot_json={
            "non_alpaca_account_identity": _ACCOUNT_IDENTITY,
        },
        correlation_id="recent-exit-missing-outcome-loss-guard",
        started_at=frontier - _dt.timedelta(minutes=3),
        ended_at=frontier - _dt.timedelta(seconds=30),
        created_at=frontier - _dt.timedelta(minutes=3),
        updated_at=frontier - _dt.timedelta(seconds=30),
    )
    db.add(session)
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_terminal_outcome_unavailable"


def test_entered_outcome_with_unknown_account_generation_is_unavailable(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=-5.0,
        account_id=None,
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_account_generation_unknown"


def test_known_other_account_generation_is_excluded_not_unknown(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=-5.0,
        account_id="known-other-generation",
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_available"] is True


def test_pending_broker_reconciliation_is_coverage_unavailable(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=-5.0,
        broker_status="unreconciled_broker_unavailable",
        broker_pnl=None,
        broker_reconciled_at=None,
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_broker_reconciliation_unavailable"


@pytest.mark.parametrize("broker_pnl", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_broker_pnl_is_coverage_unavailable(db, broker_pnl):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=-5.0,
        broker_pnl=broker_pnl,
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_broker_pnl_nonfinite_or_unavailable"


def test_nonfinite_broker_return_bps_uses_logged_fixed_cooldown_fallback(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=-5.0,
        broker_return_bps=float("nan"),
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 1
    assert meta["history_available"] is True
    assert meta["broker_return_bps_fixed_cooldown_fallbacks"] == 1


def test_contradictory_broker_pnl_and_return_sign_is_coverage_unavailable(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=5.0,
        oc="success",
        broker_pnl=5.0,
        broker_return_bps=-25.0,
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_broker_label_sign_mismatch"


def test_contradictory_broker_win_is_coverage_unavailable(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=5.0,
        oc="success",
        broker_pnl=5.0,
        broker_return_bps=25.0,
        broker_win=False,
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_broker_win_mismatch"


def test_broker_return_formula_mismatch_is_coverage_unavailable(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=5.0,
        oc="success",
        broker_pnl=5.0,
        broker_return_bps=25.0,
        broker_notional=1_000.0,
        broker_win=True,
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_broker_return_formula_mismatch"


def test_true_no_fill_reconciled_to_broker_zero_is_excluded(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=0.0,
        oc="no_fill",
        broker_status="reconciled",
        broker_pnl=0.0,
        broker_return_bps=0.0,
        broker_reconciled_at=frontier - _dt.timedelta(seconds=30),
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_available"] is True
    assert meta["durable_entered_outcomes"] == 0


def test_no_fill_with_signed_broker_return_is_classification_conflict(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=0.0,
        oc="no_fill",
        broker_status="reconciled",
        broker_pnl=0.0,
        broker_return_bps=-25.0,
        broker_reconciled_at=frontier - _dt.timedelta(seconds=30),
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_entry_classification_conflict"


def test_cancelled_in_trade_zero_requires_and_accepts_durable_fill_proof(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    outcome = _seed(
        db,
        pnl=0.0,
        oc="cancelled_in_trade",
        broker_pnl=0.0,
        broker_return_bps=0.0,
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.add(
        TradingAutomationEvent(
            session_id=int(outcome.session_id),
            ts=frontier - _dt.timedelta(minutes=2),
            event_type="live_entry_filled",
            payload_json={"filled_size": 100},
        )
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_available"] is True
    assert meta["real_entries_today_seen"] == 1


def test_positive_broker_notional_proves_ambiguous_breakeven_round_trip(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=0.0,
        oc="error_exit",
        broker_pnl=0.0,
        broker_return_bps=0.0,
        broker_notional=1_000.0,
        broker_win=False,
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_available"] is True
    assert meta["real_entries_today_seen"] == 1


def test_submission_only_does_not_prove_ambiguous_entry(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    outcome = _seed(
        db,
        pnl=0.0,
        oc="error_exit",
        broker_pnl=0.0,
        broker_return_bps=0.0,
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.add(
        TradingAutomationEvent(
            session_id=int(outcome.session_id),
            ts=frontier - _dt.timedelta(minutes=2),
            event_type="live_exit_submitted",
            payload_json={"status": "submitted"},
        )
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_entry_classification_unknown"


@pytest.mark.parametrize("event_offset_seconds", [-361, 30])
def test_out_of_session_fill_event_does_not_prove_ambiguous_entry(
    db, event_offset_seconds
):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    terminal = frontier - _dt.timedelta(minutes=1)
    outcome = _seed(
        db,
        pnl=0.0,
        oc="error_exit",
        broker_pnl=0.0,
        broker_return_bps=0.0,
        terminal_at=terminal,
    )
    db.add(
        TradingAutomationEvent(
            session_id=int(outcome.session_id),
            ts=terminal + _dt.timedelta(seconds=event_offset_seconds),
            event_type="live_entry_filled",
            payload_json={"filled_size": 100},
        )
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_entry_classification_unknown"


def test_invalid_broker_return_cannot_prove_ambiguous_entry(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=0.0,
        oc="error_exit",
        broker_pnl=0.0,
        broker_return_bps=float("nan"),
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_entry_classification_conflict"


def test_nonpositive_broker_notional_is_classification_conflict(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=0.0,
        oc="error_exit",
        broker_pnl=0.0,
        broker_return_bps=0.0,
        broker_notional=0.0,
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_entry_classification_conflict"


def test_ambiguous_zero_outcome_without_entry_proof_is_coverage_unavailable(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=0.0,
        oc="error_exit",
        broker_pnl=0.0,
        broker_return_bps=0.0,
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_entry_classification_unknown"


@pytest.mark.parametrize("outcome_class", ["", "future_unregistered_label"])
def test_unknown_outcome_class_cannot_reset_loss_streak(db, outcome_class):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=0.0,
        oc=outcome_class,
        broker_pnl=0.0,
        broker_return_bps=0.0,
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_entry_classification_unknown"


def test_entered_outcome_missing_symbol_is_coverage_unavailable(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=-5.0,
        symbol="",
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_symbol_unavailable"


def test_outcome_and_session_symbol_mismatch_is_coverage_unavailable(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    outcome = _seed(
        db,
        pnl=-5.0,
        symbol="AAA",
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    outcome.symbol = "BBB"
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_symbol_mismatch"


def test_outcome_attached_to_nonterminal_session_is_coverage_unavailable(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    outcome = _seed(
        db,
        pnl=-5.0,
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    session = db.get(TradingAutomationSession, int(outcome.session_id))
    session.state = "live_entered"
    session.updated_at = frontier - _dt.timedelta(seconds=30)
    db.flush()
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_outcome_session_state_mismatch"


def test_outcome_and_session_terminal_clock_mismatch_is_unavailable(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    outcome = _seed(
        db,
        pnl=-5.0,
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    session = db.get(TradingAutomationSession, int(outcome.session_id))
    session.ended_at = outcome.terminal_at + _dt.timedelta(seconds=1)
    session.updated_at = session.ended_at
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_outcome_session_terminal_clock_mismatch"


def test_ambiguous_cancel_and_error_with_economics_are_real_entries(db):
    frontier = _dt.datetime(2026, 7, 14, 16, 0, 0)
    _seed(
        db,
        pnl=-3.0,
        oc="cancelled_in_trade",
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    _seed(
        db,
        pnl=-4.0,
        oc="error_exit",
        terminal_at=frontier - _dt.timedelta(minutes=2),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 2
    assert meta["real_entries_today_seen"] == 2


def test_break_even_ends_the_run(db):
    # A real-entered BREAK-EVEN ($0 realized) entry is a non-loss -> it ends the run (a red in
    # a row means realized < 0). Newest is a break-even success; losses behind it don't count.
    for i in range(_HALT_N + 1):
        _seed(db, pnl=-5.0, symbol=f"L{i}", mins_ago=10 + i)
    _seed(db, pnl=0.0, oc="success", symbol="BE", mins_ago=1)
    db.commit()
    consec, _ = _count(db)
    assert consec == 0


def test_other_execution_family_does_not_contaminate_selected_family(db):
    # Alpaca paper losses belong to their own account/family policy and must not
    # contaminate the selected Robinhood lane.
    for i in range(_HALT_N + 2):
        _seed(
            db,
            pnl=-9.0,
            fam="alpaca_spot",
            account_id="paper-other",
            symbol=f"PAP{i}",
            mins_ago=1 + i,
        )
    db.commit()
    consec, _ = _count(db)
    assert consec == 0
    halted, _ = _halt(db)
    assert halted is False


def test_alpaca_legacy_session_pnl_is_unavailable_without_cycle_settlement(db):
    account_id = "paper-selected"
    for i in range(_HALT_N):
        _seed(
            db,
            pnl=-9.0,
            fam="alpaca_spot",
            account_id=account_id,
            symbol=f"PAP{i}",
            mins_ago=_HALT_N - i,
        )
    db.commit()

    kwargs = {
        "execution_family": "ALPACA_SPOT",
        "account_scope": "alpaca:paper",
        "account_identity": account_id,
    }
    consec, meta = _count(db, **kwargs)
    assert consec == 0
    assert meta["account_identity_bound"] is True
    assert meta["history_unavailable"] is True
    assert meta["coverage_grade"] == "COVERAGE_UNAVAILABLE"
    assert meta["reason"] == "loss_guard_alpaca_cycle_settlement_unavailable"
    assert meta["coverage_gap_counts"] == {
        "loss_guard_alpaca_cycle_settlement_unavailable": _HALT_N,
    }
    halted, hmeta = _halt(db, **kwargs)
    assert halted is True
    assert hmeta["consecutive_losses"] == 0
    assert hmeta["history_unavailable"] is True
    assert hmeta["reason"] == "loss_guard_alpaca_cycle_settlement_unavailable"
    assert hmeta["config_provenance"]["halt_count"]["source"] == (
        "settings.chili_momentum_consecutive_loss_halt_count"
    )


def test_alpaca_other_user_and_account_do_not_contaminate(db):
    selected = "paper-isolation-selected"
    for i in range(_HALT_N):
        _seed(
            db,
            pnl=-9.0,
            fam="alpaca_spot",
            account_id="paper-other",
            symbol=f"A{i}",
            mins_ago=20 + i,
        )
        _seed(
            db,
            pnl=-9.0,
            fam="alpaca_spot",
            account_id=selected,
            user_id=_USER_ID + 1,
            symbol=f"U{i}",
            mins_ago=40 + i,
        )
    selected_outcome = _seed(
        db,
        pnl=-9.0,
        fam="alpaca_spot",
        account_id=selected,
        symbol="OWN",
        mins_ago=1,
    )
    db.commit()

    consec, meta = _count(
        db,
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
        account_identity=selected,
    )
    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_alpaca_cycle_settlement_unavailable"
    # Rows from another account generation/user are excluded before coverage is
    # graded; only the selected account's unsupported legacy cycle is reported.
    assert meta["coverage_gap_session_ids"] == [selected_outcome.session_id]


def test_forged_alpaca_settled_detail_cannot_upgrade_legacy_session_row(db):
    account_id = "paper-selected-forged-settlement"
    outcome = _seed(
        db,
        pnl=1.0,
        fam="alpaca_spot",
        account_id=account_id,
        symbol="FORGE",
        mins_ago=1,
    )
    outcome.broker_recon_detail_json = {
        "source": "ledger_settled",
        "fees_status": "settled",
        "status": "reconciled",
    }
    db.commit()

    consec, meta = _count(
        db,
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
        account_identity=account_id,
    )

    assert consec == 0
    assert meta["history_unavailable"] is True
    assert meta["reason"] == "loss_guard_alpaca_cycle_settlement_unavailable"
    assert meta["coverage_gap_session_ids"] == [outcome.session_id]


def test_alpaca_missing_account_identity_halts_fail_closed(db):
    halted, meta = _halt(
        db,
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
        account_identity=None,
    )
    assert halted is True
    assert meta["required_scope_unavailable"] is True
    assert meta["reason"] == "alpaca_loss_guard_identity_unavailable"


def test_explicit_replay_frontier_uses_its_et_day_and_ignores_future(db):
    frontier = _dt.datetime(2026, 7, 14, 15, 0, 0)
    _seed(
        db,
        pnl=-5.0,
        symbol="PAST",
        terminal_at=frontier - _dt.timedelta(minutes=1),
    )
    _seed(
        db,
        pnl=-5.0,
        symbol="FUTURE",
        terminal_at=frontier + _dt.timedelta(minutes=1),
    )
    # This is earlier in UTC but belongs to the preceding ET calendar day.
    _seed(
        db,
        pnl=-5.0,
        symbol="PRIOR_ET_DAY",
        terminal_at=_dt.datetime(2026, 7, 14, 3, 59, 59),
    )
    db.commit()

    consec, meta = _count(db, decision_as_of=frontier)
    assert consec == 1
    assert meta["real_entries_today_seen"] == 1
    assert meta["decision_as_of_utc"].startswith("2026-07-14T15:00:00")


def test_flag_off_never_halts(db, monkeypatch):
    # (c) Flag OFF -> the decision returns not-halted even with N+ consecutive losses
    # (byte-identical: arming is never blocked by this guard).
    for i in range(_HALT_N + 2):
        _seed(db, pnl=-5.0, symbol=f"S{i}", mins_ago=_HALT_N + 2 - i)
    db.commit()
    monkeypatch.setattr(settings, "chili_momentum_consecutive_loss_halt_enabled", False)
    halted, meta = _halt(db)
    assert halted is False
    assert meta["reason"] == "disabled"


def test_halt_decision_fails_closed_on_error(db, monkeypatch):
    # Missing loss history cannot authorize another new arm.
    import app.services.trading.momentum_neural.risk_policy as rp

    def _boom(db, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(rp, "account_wide_consecutive_losses", _boom)
    halted, meta = _halt(db)
    assert halted is True
    assert meta["reason"] == "loss_guard_history_unavailable"
    assert meta["history_unavailable"] is True


def test_db_query_failure_is_history_unavailable():
    class _Boom:
        def query(self, *args, **kwargs):
            raise RuntimeError("db down")

    halted, meta = consecutive_loss_halt_decision(
        _Boom(),
        user_id=_USER_ID,
        execution_family=_FAMILY,
        account_identity=_ACCOUNT_IDENTITY,
        decision_as_of=_dt.datetime(2026, 7, 14, 16, 0, 0),
    )

    assert halted is True
    assert meta["reason"] == "loss_guard_history_unavailable"
    assert meta["history_unavailable"] is True


def test_halt_is_arming_only_not_an_exit_gate(db):
    # (d) The consecutive-loss halt is a READ consumed ONLY by the arm guard. It exposes NO
    # exit/position-management surface: it never imports, reads, or mutates an order/position
    # and is not referenced on any exit path. Assert the decision is a pure read that returns
    # a (bool, meta) verdict — there is no exit-blocking side effect to invoke.
    for i in range(_HALT_N):
        _seed(db, pnl=-5.0, symbol=f"S{i}", mins_ago=_HALT_N - i)
    db.commit()
    halted, meta = _halt(db)
    assert halted is True
    # The verdict is advisory to the ARM path only; calling it does not touch positions.
    # Re-calling is idempotent (read-only) and yields the same verdict.
    halted2, _ = _halt(db)
    assert halted2 is True
    # Confirm the auto_arm guard consumes it (the ONLY wiring), and the live runner exit
    # paths never reference it.
    import inspect
    from app.services.trading.momentum_neural import auto_arm, live_runner
    assert "consecutive_loss_halt_decision" in inspect.getsource(auto_arm)
    assert "consecutive_loss_halt_decision" not in inspect.getsource(live_runner)
