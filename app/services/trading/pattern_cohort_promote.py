"""f-promotion-pipeline-rebalance Phase 4 (2026-05-10).

Adaptive cohort auto-promote. Ranks eligible candidates by composite score
when available, then CPCV strength, and advances enough candidates to fill the
operator's target promotion roster. Candidates move first to
``shadow_promoted``; they do not jump directly to ``promoted`` / ``live``.

Eligibility filter
------------------

A pattern is eligible if and ONLY if:

- ``active=True``
- ``lifecycle_stage IN ('backtested', 'candidate')``, plus stale
  ``challenged`` rows whose adaptive CPCV verdict now passes
- ``promotion_gate_passed=True``
- ``cpcv_median_sharpe`` is non-NULL
- ``deflated_sharpe`` is non-NULL
- ``pbo`` is non-NULL
- directional outcomes are NOT required; ``shadow_promoted`` is the
  broker-blocked observation stage that collects them
- ``quality_composite_score`` is optional; scored candidates rank first,
  CPCV-only candidates can bootstrap observation

Selection + target roster
-------------------------

Sort eligible patterns by ``quality_composite_score`` DESC NULLS LAST, then
CPCV strength and ``id`` ASC (deterministic tiebreaker). Fill remaining slots
under ``chili_cpcv_target_promotion_pool_pct`` of the active pattern pool.
Existing staged/live roster rows count toward that target, so reruns are
naturally idempotent.

Public API
----------

- ``select_cohort_candidates(db, *, settings_=None) -> list[ScanPattern]``:
  pure read; returns the eligibility set ranked by score.
- ``count_recent_cohort_promotions(db, *, since_hours=168) -> int``:
  count of transitions to ``shadow_promoted`` in the rolling window.
- ``run_cohort_promote_cycle(db, *, now=None, settings_=None) -> dict``:
  the weekly entry point. Flag-gated by ``chili_cohort_promote_enabled``
  (default False).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models.trading import ScanPattern

logger = logging.getLogger(__name__)


COHORT_ELIGIBLE_LIFECYCLE_STAGES = ("backtested", "candidate", "challenged")
PROMOTION_ROSTER_LIFECYCLE_STAGES = (
    "shadow_promoted",
    "pilot_promoted",
    "promoted",
    "live",
)


def select_cohort_candidates(
    db: Session,
    *,
    settings_: Any = None,
) -> list[ScanPattern]:
    """Return the eligibility set ranked by ``quality_composite_score``.

    Pure read — no DB writes. The list is bounded by
    ``chili_cohort_promote_top_n`` (default 20).
    """
    if settings_ is None:
        from ...config import settings as _settings
        settings_ = _settings

    # Do not require directional outcomes here. shadow_promoted is the
    # observation stage that lets a pattern collect those outcomes without
    # broker exposure; requiring them here creates a bootstrap deadlock.
    # Stale ``challenged`` rows are eligible only when the current adaptive
    # verdict says they pass.
    sql = text(
        """
        SELECT sp.id
        FROM scan_patterns sp
        WHERE sp.active IS TRUE
          AND sp.lifecycle_stage IN ('backtested', 'candidate', 'challenged')
          AND sp.promotion_gate_passed IS TRUE
          AND sp.cpcv_median_sharpe IS NOT NULL
          AND sp.deflated_sharpe IS NOT NULL
          AND sp.pbo IS NOT NULL
        ORDER BY
          sp.quality_composite_score DESC NULLS LAST,
          sp.cpcv_median_sharpe DESC NULLS LAST,
          sp.deflated_sharpe DESC NULLS LAST,
          sp.pbo ASC NULLS LAST,
          sp.id ASC
        """
    )
    rows = db.execute(sql).fetchall()
    ids = [int(r[0]) for r in rows]
    if not ids:
        return []
    pats = (
        db.query(ScanPattern)
          .filter(ScanPattern.id.in_(ids))
          .all()
    )
    pat_by_id = {int(p.id): p for p in pats}
    return [pat_by_id[i] for i in ids if i in pat_by_id]


def count_recent_cohort_promotions(
    db: Session,
    *,
    now: Optional[datetime] = None,
    since_hours: int = 168,
) -> int:
    """Count transitions to ``shadow_promoted`` within the rolling window.

    Counts ALL transitions (cohort-auto + operator-manual), per the
    plan: the cap is "net advances per ~week period", regardless of
    source. If the operator manually moves a pattern to
    ``shadow_promoted``, it counts toward the cap for that week.
    """
    now = now or datetime.utcnow()
    since = now - timedelta(hours=since_hours)
    return (
        db.query(ScanPattern)
          .filter(ScanPattern.lifecycle_stage == "shadow_promoted")
          .filter(ScanPattern.lifecycle_changed_at.isnot(None))
          .filter(ScanPattern.lifecycle_changed_at >= since)
          .count()
    )


def count_active_patterns(db: Session) -> int:
    """Count active patterns in the current promotion universe."""
    return (
        db.query(ScanPattern)
          .filter(ScanPattern.active.is_(True))
          .count()
    )


def count_current_promotion_roster(db: Session) -> int:
    """Count active patterns already staged, pilot, promoted, or live."""
    return (
        db.query(ScanPattern)
          .filter(ScanPattern.active.is_(True))
          .filter(ScanPattern.lifecycle_stage.in_(PROMOTION_ROSTER_LIFECYCLE_STAGES))
          .count()
    )


def target_promotion_roster_size(db: Session, *, settings_: Any) -> int:
    """Derive the target roster from operator policy and active pool size."""
    pct_raw = getattr(settings_, "chili_cpcv_target_promotion_pool_pct", None)
    if pct_raw is None:
        return 0
    pct = max(0.0, min(1.0, float(pct_raw)))
    return int(math.ceil(count_active_patterns(db) * pct))


def run_cohort_promote_cycle(
    db: Session,
    *,
    now: Optional[datetime] = None,
    settings_: Any = None,
) -> dict:
    """Adaptive cohort-promote entry point.

    Selects ranked eligible patterns, fills remaining slots under the
    target promotion roster, and updates ``lifecycle_stage`` to
    ``shadow_promoted`` for the cohort. Logs each transition.

    Flag-gated by ``chili_cohort_promote_enabled``.
    """
    if settings_ is None:
        from ...config import settings as _settings
        settings_ = _settings

    if not bool(getattr(settings_, "chili_cohort_promote_enabled", False)):
        logger.info("[pattern_cohort_promote] flag-disabled, skipping cycle")
        return {"ok": True, "skipped": "flag_disabled"}

    now = now or datetime.utcnow()
    target_roster_size = target_promotion_roster_size(db, settings_=settings_)
    current_roster_size = count_current_promotion_roster(db)
    spots_remaining = max(0, target_roster_size - current_roster_size)

    if spots_remaining == 0:
        logger.info(
            "[pattern_cohort_promote] target roster reached: %d/%d, skipping",
            current_roster_size, target_roster_size,
        )
        return {
            "ok": True,
            "skipped": "target_roster_reached",
            "candidates_eligible": 0,
            "promoted_count": 0,
            "promoted_ids": [],
            "spots_remaining_before": 0,
            "target_roster_size": target_roster_size,
            "current_roster_size": current_roster_size,
        }

    candidates = select_cohort_candidates(db, settings_=settings_)
    selected = candidates[:spots_remaining]

    promoted_ids: list[int] = []
    for pat in selected:
        pat.lifecycle_stage = "shadow_promoted"
        pat.lifecycle_changed_at = now
        promoted_ids.append(int(pat.id))
        logger.info(
            "[pattern_cohort_promote] pid=%s name=%r score=%.4f "
            "→ shadow_promoted (cohort)",
            pat.id, pat.name, float(pat.quality_composite_score or 0.0),
        )

    if promoted_ids:
        db.flush()
        db.commit()

    result = {
        "ok": True,
        "candidates_eligible": len(candidates),
        "promoted_count": len(promoted_ids),
        "promoted_ids": promoted_ids,
        "spots_remaining_before": spots_remaining,
        "target_roster_size": target_roster_size,
        "current_roster_size_before": current_roster_size,
    }
    logger.info("[pattern_cohort_promote] cycle: %s", result)
    return result
