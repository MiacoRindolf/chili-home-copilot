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

NEVER_ENTERED_OUTCOME_CLASSES: frozenset[str] = frozenset(
    {
        OUTCOME_RISK_BLOCK,
        OUTCOME_STALE_DATA_ABORT,
        OUTCOME_NO_FILL,
        OUTCOME_CANCELLED_PRE_ENTRY,
        OUTCOME_EXPIRED_PRE_RUN,
        OUTCOME_ARCHIVED,
        OUTCOME_FLAT_UNKNOWN,
    }
)


def is_real_entry_outcome(outcome_class: object) -> bool:
    """Return True only for terminal outcomes that imply an entry actually existed.

    Feedback/risk math must not let cancelled/no-fill/pre-entry rows dilute streak,
    run-R, or expectancy windows. Unknown labels fail closed as not-real-entry.
    """
    label = str(outcome_class or "").strip().lower()
    return bool(label and label in ALL_OUTCOME_CLASSES and label not in NEVER_ENTERED_OUTCOME_CLASSES)
