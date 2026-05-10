"""f-promotion-pipeline-rebalance Phase 4 (2026-05-10).

Weekly cohort auto-promote. Reads ``scan_patterns.quality_composite_score``
(populated nightly by ``pattern_quality_score.compute_and_persist_scores``)
and advances the top-N candidates per rolling 7-day window to the
``shadow_promoted`` lifecycle stage (Phase 3). NOT to ``promoted`` /
``live`` — the risk-asymmetric ramp is the whole point of Phase 4.

Eligibility filter
------------------

A pattern is eligible if and ONLY if:

- ``active=True``
- ``lifecycle_stage IN ('backtested', 'candidate')``
- ``promotion_gate_passed=True``
- ``cpcv_median_sharpe`` is non-NULL and ``>= 1.0``
- ``deflated_sharpe`` is non-NULL
- ``pbo`` is non-NULL
- ``rolling_sample_n >= 30`` (joined from ``pattern_directional_quality_v``)
- ``quality_composite_score`` is non-NULL (means scoring succeeded for
  the pattern — all five components were computable)

NULL propagation, never magic-fallback (advisor brief §2.6).

Selection + cap
---------------

Sort eligible patterns by ``quality_composite_score`` DESC, then ``id``
ASC (deterministic tiebreaker). Take top-N (default 20). Cap at
``max_per_week`` minus the count of cohort-recent transitions to
``shadow_promoted`` in the last 7 days; if zero spots remain,
short-circuit. Idempotent on re-run within the same week.

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
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models.trading import ScanPattern

logger = logging.getLogger(__name__)


COHORT_ELIGIBLE_LIFECYCLE_STAGES = ("backtested", "candidate")
DIRECTIONAL_SAMPLE_FLOOR = 30


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

    top_n = int(getattr(settings_, "chili_cohort_promote_top_n", 20))

    sql = text(
        """
        SELECT sp.id
        FROM scan_patterns sp
        INNER JOIN pattern_directional_quality_v pdq
                ON pdq.scan_pattern_id = sp.id
        WHERE sp.active IS TRUE
          AND sp.lifecycle_stage IN ('backtested', 'candidate')
          AND sp.promotion_gate_passed IS TRUE
          AND sp.cpcv_median_sharpe IS NOT NULL
          AND sp.cpcv_median_sharpe >= 1.0
          AND sp.deflated_sharpe IS NOT NULL
          AND sp.pbo IS NOT NULL
          AND sp.quality_composite_score IS NOT NULL
          AND pdq.rolling_sample_n >= :sample_floor
        ORDER BY sp.quality_composite_score DESC, sp.id ASC
        LIMIT :top_n
        """
    )
    rows = db.execute(sql, {
        "sample_floor": DIRECTIONAL_SAMPLE_FLOOR,
        "top_n": top_n,
    }).fetchall()
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
    """Weekly cohort-promote entry point.

    Selects top-N eligible patterns, caps at remaining spots in the
    rolling 7-day window, and updates ``lifecycle_stage`` to
    ``shadow_promoted`` for the cohort. Logs each transition.

    Flag-gated by ``chili_cohort_promote_enabled`` (default False).
    Phase 4 ships dormant; the operator opts in by setting the flag
    True.
    """
    if settings_ is None:
        from ...config import settings as _settings
        settings_ = _settings

    if not bool(getattr(settings_, "chili_cohort_promote_enabled", False)):
        logger.info("[pattern_cohort_promote] flag-disabled, skipping cycle")
        return {"ok": True, "skipped": "flag_disabled"}

    now = now or datetime.utcnow()
    cap = int(getattr(settings_, "chili_cohort_promote_max_per_week", 10))
    promoted_recently = count_recent_cohort_promotions(db, now=now)
    spots_remaining = max(0, cap - promoted_recently)

    if spots_remaining == 0:
        logger.info(
            "[pattern_cohort_promote] cap reached: %d/%d in last 7d, skipping",
            promoted_recently, cap,
        )
        return {
            "ok": True,
            "skipped": "cap_reached",
            "promoted_in_last_7d": promoted_recently,
            "cap": cap,
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
        "promoted_in_last_7d_before": promoted_recently,
        "cap": cap,
    }
    logger.info("[pattern_cohort_promote] cycle: %s", result)
    return result
