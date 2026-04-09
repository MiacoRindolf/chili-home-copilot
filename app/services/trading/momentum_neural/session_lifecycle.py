"""Canonical operator lifecycle labels derived from persisted FSM state (raw state unchanged)."""

from __future__ import annotations

from typing import Any, Optional

from .live_fsm import (
    LIVE_RUNNER_ACTIVE_SUMMARY_STATES,
    LIVE_RUNNER_TERMINAL_STATES,
    STATE_LIVE_CANCELLED,
    STATE_LIVE_COOLDOWN,
    STATE_LIVE_ERROR,
    STATE_LIVE_EXITED,
    STATE_LIVE_FINISHED,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
)
from .paper_fsm import (
    PAPER_RUNNER_TERMINAL_STATES,
    STATE_ARCHIVED,
    STATE_ARMED_PENDING_RUNNER,
    STATE_LIVE_ARM_PENDING,
    STATE_BAILOUT,
    STATE_CANCELLED,
    STATE_COOLDOWN,
    STATE_DRAFT,
    STATE_ENTERED,
    STATE_ENTRY_CANDIDATE,
    STATE_ERROR,
    STATE_EXITED,
    STATE_EXPIRED,
    STATE_FINISHED,
    STATE_IDLE,
    STATE_LIVE_ARM_PENDING,
    STATE_PENDING_ENTRY,
    STATE_QUEUED,
    STATE_SCALING_OUT,
    STATE_TRAILING,
    STATE_WATCHING,
)

# Canonical labels for UI / read-model (persisted state stays granular).
CANON_DRAFT = "draft"
CANON_QUEUED = "queued"
CANON_PAPER_RUNNING = "paper_running"
CANON_PAPER_COMPLETED = "paper_completed"
CANON_LIVE_ARM_PENDING = "live_arm_pending"
CANON_ARMED_PENDING_RUNNER = "armed_pending_runner"
CANON_QUEUED_LIVE = "queued_live"
CANON_LIVE_RUNNING = "live_running"
CANON_COMPLETED = "completed"
CANON_CANCELLED = "cancelled"
CANON_ARCHIVED = "archived"
CANON_FAILED = "failed"

_PHASE_EXITING = "exiting"


def _paper_active_runner_states() -> frozenset[str]:
    return frozenset(
        {
            STATE_WATCHING,
            STATE_ENTRY_CANDIDATE,
            STATE_PENDING_ENTRY,
            STATE_ENTERED,
            STATE_SCALING_OUT,
            STATE_TRAILING,
            STATE_BAILOUT,
        }
    )


def canonical_operator_state(
    *,
    mode: str,
    state: str,
    risk_snapshot_json: Optional[dict[str, Any]] = None,
) -> str:
    """Map persisted (mode, state) to a stable operator-facing lifecycle label."""
    m = (mode or "paper").strip().lower()
    st = (state or "").strip()
    snap = risk_snapshot_json if isinstance(risk_snapshot_json, dict) else {}

    if st == STATE_ARCHIVED:
        return CANON_ARCHIVED

    if m == "live":
        if st == STATE_LIVE_ARM_PENDING:
            return CANON_LIVE_ARM_PENDING
        if st == STATE_ARMED_PENDING_RUNNER:
            return CANON_ARMED_PENDING_RUNNER
        if st == STATE_QUEUED_LIVE:
            return CANON_QUEUED_LIVE
        if st == STATE_LIVE_FINISHED:
            return CANON_COMPLETED
        if st == STATE_LIVE_CANCELLED:
            return CANON_CANCELLED
        if st == STATE_LIVE_ERROR:
            return CANON_FAILED
        if st in (STATE_LIVE_EXITED, STATE_LIVE_COOLDOWN):
            return CANON_LIVE_RUNNING
        if st in LIVE_RUNNER_ACTIVE_SUMMARY_STATES:
            return CANON_LIVE_RUNNING
        if st in LIVE_RUNNER_TERMINAL_STATES:
            return CANON_FAILED if st == STATE_LIVE_ERROR else CANON_CANCELLED
        if st.startswith("live_") or st == STATE_WATCHING_LIVE:
            return CANON_LIVE_RUNNING
        return CANON_FAILED

    # paper mode
    if st == STATE_DRAFT or st == STATE_IDLE:
        return CANON_DRAFT
    if st == STATE_QUEUED:
        return CANON_QUEUED
    if st == STATE_FINISHED:
        return CANON_PAPER_COMPLETED
    if st == STATE_CANCELLED:
        return CANON_CANCELLED
    if st == STATE_EXPIRED:
        return CANON_FAILED
    if st == STATE_ERROR:
        return CANON_FAILED
    if st in _paper_active_runner_states():
        return CANON_PAPER_RUNNING
    if st in (STATE_EXITED, STATE_COOLDOWN):
        return CANON_PAPER_RUNNING
    if st in PAPER_RUNNER_TERMINAL_STATES:
        if st == STATE_ERROR:
            return CANON_FAILED
        if st == STATE_EXPIRED:
            return CANON_FAILED
        if st == STATE_CANCELLED:
            return CANON_CANCELLED
        if st == STATE_ARCHIVED:
            return CANON_ARCHIVED
        if st == STATE_FINISHED:
            return CANON_PAPER_COMPLETED
    return CANON_DRAFT


def phase_hint(
    *,
    mode: str,
    state: str,
    risk_snapshot_json: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Sub-phase for UI when canonical state groups multiple FSM states."""
    st = (state or "").strip()
    m = (mode or "paper").strip().lower()
    if m == "paper" and st in (STATE_EXITED, STATE_COOLDOWN):
        return _PHASE_EXITING
    if m == "live" and st in (STATE_LIVE_EXITED, STATE_LIVE_COOLDOWN):
        return _PHASE_EXITING
    if m == "live" and st == STATE_QUEUED_LIVE:
        return "queued_for_runner"
    if m == "live" and st == STATE_ARMED_PENDING_RUNNER:
        return "armed_awaiting_runner_tick"
    _ = risk_snapshot_json  # reserved for future operator_pause etc.
    return None


def is_live_orders_active(*, mode: str, state: str) -> bool:
    """True when venue position / live orders may be in play (not merely armed or queued)."""
    if (mode or "").lower() != "live":
        return False
    return state in LIVE_RUNNER_ACTIVE_SUMMARY_STATES


def is_armed_only_live(*, mode: str, state: str) -> bool:
    """Confirmed arm but not yet in live runner active execution."""
    if (mode or "").lower() != "live":
        return False
    return state in (STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE)


def session_state_machine_doc() -> dict[str, Any]:
    """Static documentation for API consumers (persisted states unchanged)."""
    return {
        "persisted_paper_states": sorted(
            {
                STATE_DRAFT,
                STATE_QUEUED,
                STATE_WATCHING,
                STATE_ENTRY_CANDIDATE,
                STATE_PENDING_ENTRY,
                STATE_ENTERED,
                STATE_SCALING_OUT,
                STATE_TRAILING,
                STATE_BAILOUT,
                STATE_EXITED,
                STATE_COOLDOWN,
                STATE_FINISHED,
                STATE_CANCELLED,
                STATE_ARCHIVED,
                STATE_EXPIRED,
                STATE_ERROR,
                STATE_IDLE,
            }
        ),
        "persisted_live_states": sorted(
            {
                STATE_LIVE_ARM_PENDING,  # shared string with paper_fsm
                STATE_ARMED_PENDING_RUNNER,
                STATE_QUEUED_LIVE,
                STATE_WATCHING_LIVE,
                STATE_LIVE_ENTRY_CANDIDATE,
                STATE_LIVE_PENDING_ENTRY,
                STATE_LIVE_ENTERED,
                STATE_LIVE_SCALING_OUT,
                STATE_LIVE_TRAILING,
                STATE_LIVE_BAILOUT,
                STATE_LIVE_EXITED,
                STATE_LIVE_COOLDOWN,
                STATE_LIVE_FINISHED,
                STATE_LIVE_CANCELLED,
                STATE_LIVE_ERROR,
            }
        ),
        "canonical_operator_states": [
            CANON_DRAFT,
            CANON_QUEUED,
            CANON_PAPER_RUNNING,
            CANON_PAPER_COMPLETED,
            CANON_LIVE_ARM_PENDING,
            CANON_ARMED_PENDING_RUNNER,
            CANON_QUEUED_LIVE,
            CANON_LIVE_RUNNING,
            CANON_COMPLETED,
            CANON_CANCELLED,
            CANON_ARCHIVED,
            CANON_FAILED,
        ],
        "note": (
            "Canonical labels are derived from persisted mode+state; "
            "runners continue to use paper_fsm/live_fsm string states."
        ),
    }
