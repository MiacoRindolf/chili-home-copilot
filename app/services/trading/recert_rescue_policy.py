"""Shared recert-rescue backpressure vocabulary."""
from __future__ import annotations

RECENT_RECERT_RESCUE_BLOCKER_ACTION_LIST = (
    "complete_oos_recert_and_quality_refresh",
    "inspect_recert_backtest_no_oos_evidence_keep_live_blocked",
    "wait_for_recert_backtest_cooldown_keep_live_blocked",
    "live_blocked_recert_debt_no_refresh",
)

RECENT_RECERT_RESCUE_BLOCKER_REASON_LIST = (
    "recent_recert_backtest_cooldown",
    "recert_backtest_refresh_already_open",
    "no_recert_refresh_needed",
)

RECENT_RECERT_RESCUE_BLOCKER_ACTIONS = frozenset(
    RECENT_RECERT_RESCUE_BLOCKER_ACTION_LIST
)
RECENT_RECERT_RESCUE_BLOCKER_REASONS = frozenset(
    RECENT_RECERT_RESCUE_BLOCKER_REASON_LIST
)


def recert_rescue_blocker_actions() -> list[str]:
    return list(RECENT_RECERT_RESCUE_BLOCKER_ACTION_LIST)


def recert_rescue_blocker_reasons() -> list[str]:
    return list(RECENT_RECERT_RESCUE_BLOCKER_REASON_LIST)
