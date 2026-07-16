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
# SHOULD count toward the streak. ``cancelled_in_trade`` is explicitly post-entry,
# and ``error_exit`` is ambiguous: either may carry durable fill/economic evidence.
# They cannot be blanket-pruned by class. Safety consumers must combine this set
# with durable fill evidence and an authoritative broker label.
NEVER_ENTERED_OUTCOMES: frozenset[str] = frozenset(
    {
        OUTCOME_ARCHIVED,
        OUTCOME_CANCELLED_PRE_ENTRY,
        OUTCOME_EXPIRED_PRE_RUN,
        OUTCOME_NO_FILL,
        OUTCOME_RISK_BLOCK,
    }
)

# These labels can represent either a post-fill failure/cancel or an abort whose
# writer did not retain enough execution detail.  Class text alone is not proof.
AMBIGUOUS_ENTRY_OUTCOMES: frozenset[str] = frozenset(
    {OUTCOME_CANCELLED_IN_TRADE, OUTCOME_ERROR_EXIT}
)

# Backward-compatible private alias for older imports.  New SQL consumers use the
# public immutable set so they can exclude never-entered rows *before* applying a
# real-entry lookback limit.
_NEVER_ENTERED_OUTCOMES = NEVER_ENTERED_OUTCOMES


def is_real_entry_outcome(
    outcome_class: str | None,
    *,
    durable_entry: bool = False,
    realized_pnl_usd: float | None = None,
) -> bool:
    """Return whether the class is eligible to describe an entered trade.

    False is definitive only for always-pre-entry classes. True is deliberately
    class eligibility, not fill proof: ambiguous terminal classes such as
    ``error_exit`` remain visible to legacy metrics/risk callers that have only
    the class. Safety gates must apply their own strict durable-evidence check.

    The optional evidence arguments remain accepted for API compatibility; they
    may strengthen a strict caller's separate proof but never downgrade class
    eligibility here.
    """
    normalized = str(outcome_class or "").strip().lower()
    return normalized not in NEVER_ENTERED_OUTCOMES
