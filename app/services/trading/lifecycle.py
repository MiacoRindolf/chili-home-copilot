"""Strategy lifecycle state machine for ScanPattern.

States: candidate -> backtested -> promoted -> live -> decayed -> retired
Each transition is enforced and timestamped.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import ScanPattern

logger = logging.getLogger(__name__)

VALID_STAGES = ("candidate", "backtested", "promoted", "live", "decayed", "retired")

_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "candidate":  ("backtested", "retired"),
    "backtested": ("promoted", "candidate", "retired"),
    "promoted":   ("live", "decayed", "retired", "backtested"),
    "live":       ("decayed", "retired"),
    "decayed":    ("retired", "backtested", "candidate"),
    "retired":    ("candidate",),  # allow resurrection for re-mining
}


class LifecycleError(ValueError):
    """Raised when an invalid lifecycle transition is attempted."""


def transition(
    db: Session,
    pattern: ScanPattern,
    target_stage: str,
    *,
    reason: str | None = None,
    commit: bool = True,
) -> ScanPattern:
    """Move *pattern* to *target_stage* if the transition is allowed."""
    current = pattern.lifecycle_stage or "candidate"
    if target_stage not in VALID_STAGES:
        raise LifecycleError(f"Unknown stage: {target_stage}")
    if target_stage == current:
        return pattern
    allowed = _ALLOWED_TRANSITIONS.get(current, ())
    if target_stage not in allowed:
        raise LifecycleError(
            f"Cannot transition {pattern.name!r} (id={pattern.id}) "
            f"from {current!r} to {target_stage!r}. "
            f"Allowed: {allowed}"
        )
    prev = current
    pattern.lifecycle_stage = target_stage
    pattern.lifecycle_changed_at = datetime.utcnow()

    _sync_legacy_fields(pattern, target_stage)

    if commit:
        db.flush()

    logger.info(
        "[lifecycle] %s (id=%s): %s -> %s%s",
        pattern.name, pattern.id, prev, target_stage,
        f" ({reason})" if reason else "",
    )
    return pattern


def _sync_legacy_fields(pattern: ScanPattern, stage: str) -> None:
    """Keep promotion_status and active in sync with the new lifecycle_stage."""
    if stage == "promoted":
        pattern.promotion_status = "promoted"
        pattern.active = True
    elif stage == "live":
        pattern.promotion_status = "promoted"
        pattern.active = True
    elif stage == "decayed":
        pattern.promotion_status = "degraded_live"
        pattern.active = True
    elif stage == "retired":
        pattern.active = False
        if pattern.promotion_status not in ("rejected_oos", "rejected_sample"):
            pattern.promotion_status = "retired"
    elif stage == "backtested":
        if pattern.promotion_status in ("legacy", "candidate"):
            pattern.promotion_status = "backtested"
    elif stage == "candidate":
        if pattern.promotion_status in ("retired", "degraded_live"):
            pattern.promotion_status = "candidate"
            pattern.active = True


def transition_on_backtest(db: Session, pattern: ScanPattern, oos_pass: bool) -> ScanPattern:
    """After a backtest completes, advance lifecycle accordingly."""
    current = pattern.lifecycle_stage or "candidate"
    if current in ("candidate",):
        if oos_pass:
            return transition(db, pattern, "backtested", reason="backtest_passed_oos")
        return pattern
    return pattern


def transition_on_promotion(db: Session, pattern: ScanPattern) -> ScanPattern:
    """After passing promotion gates, advance to promoted."""
    current = pattern.lifecycle_stage or "candidate"
    if current in ("backtested", "candidate"):
        return transition(db, pattern, "promoted", reason="promotion_gates_passed")
    return pattern


def transition_to_live(db: Session, pattern: ScanPattern) -> ScanPattern:
    """Mark a promoted pattern as live (generating signals)."""
    current = pattern.lifecycle_stage or "candidate"
    if current == "promoted":
        return transition(db, pattern, "live", reason="signals_active")
    return pattern


def transition_on_decay(db: Session, pattern: ScanPattern, reason: str = "alpha_decay") -> ScanPattern:
    """Demote a live/promoted pattern due to performance decay."""
    current = pattern.lifecycle_stage or "candidate"
    if current in ("live", "promoted"):
        return transition(db, pattern, "decayed", reason=reason)
    return pattern


def retire(db: Session, pattern: ScanPattern, reason: str = "manual") -> ScanPattern:
    """Permanently retire a pattern."""
    return transition(db, pattern, "retired", reason=reason)


def get_lifecycle_summary(db: Session) -> dict[str, int]:
    """Count patterns by lifecycle stage."""
    from sqlalchemy import func
    rows = (
        db.query(ScanPattern.lifecycle_stage, func.count(ScanPattern.id))
        .group_by(ScanPattern.lifecycle_stage)
        .all()
    )
    return {stage: cnt for stage, cnt in rows}
