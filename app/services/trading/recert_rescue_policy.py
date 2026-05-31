"""Shared recert-rescue backpressure vocabulary."""
from __future__ import annotations

RECENT_RECERT_RESCUE_BLOCKER_ACTION_LIST = (
    "complete_oos_recert_and_quality_refresh",
    "keep_live_blocked_until_hard_recert_clears",
    "no_recert_action_needed",
    "inspect_recert_backtest_no_oos_evidence_keep_live_blocked",
    "wait_for_recert_backtest_cooldown_keep_live_blocked",
    "live_blocked_recert_debt_no_refresh",
)

CONDITIONAL_RECERT_RESCUE_BACKTEST_ACTION = (
    "run_recert_backtest_refresh_keep_live_blocked"
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


def _token(value: object) -> str:
    return str(value or "").strip().lower()


def recert_rescue_diagnostic_blocks_refresh(payload: object) -> bool:
    """True when a recent diagnostic proves another rescue refresh would churn."""
    if not isinstance(payload, dict):
        return False
    action = _token(payload.get("recommended_next_action"))
    if action in RECENT_RECERT_RESCUE_BLOCKER_ACTIONS:
        return True

    refresh = payload.get("recert_backtest_refresh")
    refresh_payload = refresh if isinstance(refresh, dict) else {}
    refresh_reason = _token(refresh_payload.get("reason"))
    if refresh_reason in RECENT_RECERT_RESCUE_BLOCKER_REASONS:
        return True

    if action == CONDITIONAL_RECERT_RESCUE_BACKTEST_ACTION:
        return refresh_payload.get("requested") is not True
    return False
