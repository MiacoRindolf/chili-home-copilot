"""Strategy lifecycle state machine for ScanPattern.

States include ``challenged`` (repeatable-edge research: visible on Brain desk, not live-eligible),
``validated`` (paper/runtime review when governance allows), ``promoted`` (live-readiness path).
Each transition is enforced and timestamped.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import PaperTrade, ScanPattern, StrategyProposal

logger = logging.getLogger(__name__)

VALID_STAGES = (
    "candidate",
    "backtested",
    "validated",
    "challenged",
    "promoted",
    "live",
    "decayed",
    "retired",
)


def lifecycle_stage_from_promotion_status(promotion_status: str | None) -> str:
    """Map legacy ``promotion_status`` to ``lifecycle_stage`` (consolidation with Phase 7).

    New code should set ``lifecycle_stage`` directly; this supports dual-writes from
    older promotion_status strings.
    """
    s = (promotion_status or "").strip().lower()
    if s == "candidate":
        return "candidate"
    if s == "validated":
        return "validated"
    if s == "promoted":
        return "promoted"
    if s == "rejected":
        return "retired"
    if s.startswith("rejected"):
        return "retired"
    if s in ("pending_oos",):
        return "candidate"
    if s == "backtested":
        return "backtested"
    if s in ("degraded_live",):
        return "decayed"
    if s == "retired":
        return "retired"
    if s == "legacy":
        return "backtested"
    return "candidate"

_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "candidate":  ("backtested", "retired", "challenged"),
    "backtested": ("promoted", "candidate", "retired", "validated", "challenged"),
    "validated":  ("promoted", "backtested", "retired", "candidate", "challenged"),
    "challenged": ("backtested", "validated", "retired", "candidate", "promoted"),
    "promoted":   ("live", "decayed", "retired", "backtested", "challenged"),
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

    if target_stage in ("decayed", "retired"):
        db.query(StrategyProposal).filter(
            StrategyProposal.scan_pattern_id == pattern.id,
            StrategyProposal.status.in_(("pending", "approved", "working")),
        ).update({"status": "expired"}, synchronize_session=False)
        db.query(PaperTrade).filter(
            PaperTrade.scan_pattern_id == pattern.id,
            PaperTrade.status == "open",
        ).update({"status": "expired"}, synchronize_session=False)

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
    elif stage == "validated":
        pattern.promotion_status = "validated"
        pattern.active = True
    elif stage == "challenged":
        # Research-only: nuance lives in oos_validation_json.edge_evidence / promotion_block_codes.
        pattern.active = True
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
    if current in ("backtested", "candidate", "validated", "challenged"):
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
