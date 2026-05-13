"""f-promotion-pipeline-rebalance Phase 4 (2026-05-10).

Adaptive cohort auto-promote. Ranks eligible candidates by composite score
when available, then CPCV strength, and advances every adaptive-gate-passed
candidate to broker-blocked observation. Candidates move first to
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

Selection + observation
-----------------------

Sort eligible patterns by ``quality_composite_score`` DESC NULLS LAST, then
CPCV strength and ``id`` ASC (deterministic tiebreaker). Stage all eligible
patterns into ``shadow_promoted`` because shadow is not broker exposure; it is
the evidence-collection lane. Downstream shadow vetting applies the adaptive
target roster policy before a pattern can move to broker-eligible pilot or full
promotion.

Public API
----------

- ``select_cohort_candidates(db, *, settings_=None) -> list[ScanPattern]``:
  pure read; returns the eligibility set ranked by score.
- ``run_cohort_promote_cycle(db, *, now=None, settings_=None) -> dict``:
  the entry point. Flag-gated by ``chili_cohort_promote_enabled``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models.trading import ScanPattern

logger = logging.getLogger(__name__)


COHORT_ELIGIBLE_LIFECYCLE_STAGES = ("backtested", "candidate", "challenged")


def select_cohort_candidates(
    db: Session,
    *,
    settings_: Any = None,
) -> list[ScanPattern]:
    """Return the eligibility set ranked by ``quality_composite_score``.

    Pure read — no DB writes. The list is bounded by
    This is the broker-blocked observation lane, so the result is intentionally
    uncapped.
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


def run_cohort_promote_cycle(
    db: Session,
    *,
    now: Optional[datetime] = None,
    settings_: Any = None,
) -> dict:
    """Adaptive cohort-promote entry point.

    Selects ranked eligible patterns and updates ``lifecycle_stage`` to
    ``shadow_promoted`` for observation. Logs each transition. This step has
    no portfolio cap because it does not create broker exposure.

    Flag-gated by ``chili_cohort_promote_enabled``.
    """
    if settings_ is None:
        from ...config import settings as _settings
        settings_ = _settings

    if not bool(getattr(settings_, "chili_cohort_promote_enabled", False)):
        logger.info("[pattern_cohort_promote] flag-disabled, skipping cycle")
        return {"ok": True, "skipped": "flag_disabled"}

    now = now or datetime.utcnow()
    candidates = select_cohort_candidates(db, settings_=settings_)

    promoted_ids: list[int] = []
    for pat in candidates:
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
        "observation_stage_uncapped": True,
    }
    logger.info("[pattern_cohort_promote] cycle: %s", result)
    return result
