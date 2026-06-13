"""Deterministic outcome class labels for momentum automation feedback (Phase 9)."""

from __future__ import annotations

# Stable string ids for evolution / queries (snake_case).
OUTCOME_SUCCESS = "success"
OUTCOME_SMALL_WIN = "small_win"
OUTCOME_STOP_LOSS = "stop_loss"
OUTCOME_BAILOUT = "bailout"
OUTCOME_TIMED_EXIT = "timed_exit"
OUTCOME_GOVERNANCE_EXIT = "governance_exit"
OUTCOME_RISK_BLOCK = "risk_block"
OUTCOME_STALE_DATA_ABORT = "stale_data_abort"
OUTCOME_NO_FILL = "no_fill"
OUTCOME_ERROR_EXIT = "error_exit"
OUTCOME_CANCELLED_PRE_ENTRY = "cancelled_pre_entry"
OUTCOME_CANCELLED_IN_TRADE = "cancelled_in_trade"
OUTCOME_EXPIRED_PRE_RUN = "expired_pre_run"
OUTCOME_ARCHIVED = "archived"
OUTCOME_FLAT_UNKNOWN = "flat_unknown"

ALL_OUTCOME_CLASSES: frozenset[str] = frozenset(
    {
        OUTCOME_SUCCESS,
        OUTCOME_SMALL_WIN,
        OUTCOME_STOP_LOSS,
        OUTCOME_BAILOUT,
        OUTCOME_TIMED_EXIT,
        OUTCOME_GOVERNANCE_EXIT,
        OUTCOME_RISK_BLOCK,
        OUTCOME_STALE_DATA_ABORT,
        OUTCOME_NO_FILL,
        OUTCOME_ERROR_EXIT,
        OUTCOME_CANCELLED_PRE_ENTRY,
        OUTCOME_CANCELLED_IN_TRADE,
        OUTCOME_EXPIRED_PRE_RUN,
        OUTCOME_ARCHIVED,
        OUTCOME_FLAT_UNKNOWN,
    }
)

# Outcome classes where the position NEVER ENTERED the market (no real fill, no real
# strategy P&L). The streak-risk dial uses this to count only REAL entered trades: a
# $0.00 cancelled_pre_entry carries realized_pnl_usd=0.0 (NOT NULL), so it slips past
# a realized-not-null filter and gets miscounted as a loss (p<=0), spuriously bumping
# the consecutive-loss run. IMPORTANT: governance_exit and stale_data_abort are NOT
# listed here — those can be entered-then-force-closed with a REAL realized loss
# (verified live: stale_data_abort -$238.68, governance_exit -$5.39) that legitimately
# SHOULD count toward the streak; their non-entered rows carry NULL realized and are
# dropped by the realized-not-null filter instead. (error_exit/cancelled_in_trade
# carry NULL realized in practice but are listed here as durable belt-and-suspenders.)
_NEVER_ENTERED_OUTCOMES: frozenset[str] = frozenset(
    {
        OUTCOME_ARCHIVED,
        OUTCOME_CANCELLED_IN_TRADE,
        OUTCOME_CANCELLED_PRE_ENTRY,
        OUTCOME_ERROR_EXIT,
        OUTCOME_EXPIRED_PRE_RUN,
        OUTCOME_NO_FILL,
        OUTCOME_RISK_BLOCK,
    }
)


def is_real_entry_outcome(outcome_class: str | None) -> bool:
    """True when the outcome is a position that ACTUALLY ENTERED the market (carries
    real strategy P&L). False for never-entered classes (pre-entry cancels, no-fill,
    risk blocks, errors). Combine with a realized-not-null filter for the strict
    'real entered trade' test the streak-risk dial needs."""
    return str(outcome_class or "").strip().lower() not in _NEVER_ENTERED_OUTCOMES
