"""Finalize vetted ``shadow_promoted`` patterns into live ``promoted``.

``shadow_promoted`` is CHILI's broker-blocked observation stage: patterns
have passed CPCV and are allowed to emit pattern-imminent alerts, but the
autotrader does not place broker orders from them. This module closes the
loop by promoting only shadows whose directional alert evidence has matured
enough to produce ``quality_composite_score``.

The final gate is pool-relative, not an arbitrary fixed score:
``quality_composite_score`` must clear the same top-pool policy used by the
adaptive CPCV gate (``chili_cpcv_target_promotion_pool_pct``). Thin shadows
remain shadow-only and keep collecting directional outcomes.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models.trading import ScanPattern

logger = logging.getLogger(__name__)
LOG_PREFIX = "[pattern_shadow_vetting]"


def _is_number(value: Any) -> bool:
    try:
        return not math.isnan(float(value))
    except Exception:
        return False


def _empirical_percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated empirical percentile for small pattern pools."""
    arr = sorted(float(v) for v in values if _is_number(v))
    if not arr:
        return None
    if len(arr) == 1:
        return arr[0]
    pos = max(0.0, min(1.0, float(q))) * (len(arr) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return arr[lo]
    frac = pos - lo
    return arr[lo] * (1.0 - frac) + arr[hi] * frac


def _score_threshold_from_pool(db: Session, *, settings_: Any) -> float | None:
    """Return the adaptive top-pool score threshold.

    Reuses ``chili_cpcv_target_promotion_pool_pct`` as the operator policy:
    if the operator wants the top 5% CPCV pool, the finalizer also admits the
    top 5% of fully scored shadow patterns relative to the currently scored
    active population.
    """
    pct = float(getattr(settings_, "chili_cpcv_target_promotion_pool_pct", 0.05))
    q = 1.0 - max(0.0, min(1.0, pct))
    rows = db.execute(
        text(
            """
            SELECT quality_composite_score
            FROM scan_patterns
            WHERE active IS TRUE
              AND quality_composite_score IS NOT NULL
            """
        )
    ).fetchall()
    return _empirical_percentile([float(r[0]) for r in rows], q)


def select_shadow_vetting_candidates(
    db: Session,
    *,
    settings_: Any = None,
) -> list[dict[str, Any]]:
    """Read shadow-promoted patterns with their directional evidence state."""
    if settings_ is None:
        from ...config import settings as _settings

        settings_ = _settings

    threshold = _score_threshold_from_pool(db, settings_=settings_)
    rows = db.execute(
        text(
            """
            SELECT
                sp.id,
                sp.quality_composite_score,
                sp.promotion_gate_passed,
                sp.cpcv_median_sharpe,
                sp.deflated_sharpe,
                sp.pbo,
                q.rolling_sample_n,
                q.rolling_directional_wr
            FROM scan_patterns sp
            LEFT JOIN pattern_directional_quality_v q
              ON q.scan_pattern_id = sp.id
            WHERE sp.active IS TRUE
              AND sp.lifecycle_stage = 'shadow_promoted'
            ORDER BY
                sp.quality_composite_score DESC NULLS LAST,
                sp.cpcv_median_sharpe DESC NULLS LAST,
                sp.id ASC
            """
        )
    ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        score = float(row[1]) if row[1] is not None else None
        out.append(
            {
                "scan_pattern_id": int(row[0]),
                "quality_composite_score": score,
                "promotion_gate_passed": bool(row[2]),
                "cpcv_ready": all(row[i] is not None for i in (3, 4, 5)),
                "rolling_sample_n": int(row[6] or 0),
                "rolling_directional_wr": float(row[7]) if row[7] is not None else None,
                "score_threshold": threshold,
                "eligible": (
                    score is not None
                    and threshold is not None
                    and score >= threshold
                    and int(row[6] or 0) >= 30
                    and bool(row[2])
                    and all(row[i] is not None for i in (3, 4, 5))
                ),
            }
        )
    return out


def run_shadow_vetting_cycle(
    db: Session,
    *,
    now: datetime | None = None,
    settings_: Any = None,
) -> dict[str, Any]:
    """Promote fully vetted shadows that clear the adaptive score policy."""
    if settings_ is None:
        from ...config import settings as _settings

        settings_ = _settings

    if not bool(getattr(settings_, "chili_shadow_vetting_finalize_enabled", True)):
        logger.info("%s flag-disabled, skipping", LOG_PREFIX)
        return {"ok": True, "skipped": "flag_disabled"}

    # Refresh scores first so newly evaluated directional outcomes become
    # promotable without waiting for the nightly score job.
    try:
        from .pattern_quality_score import compute_and_persist_scores

        score_result = compute_and_persist_scores(db, settings_=settings_)
    except Exception as exc:
        db.rollback()
        logger.warning("%s score refresh failed: %s", LOG_PREFIX, exc, exc_info=True)
        return {"ok": False, "error": f"score_refresh_failed:{type(exc).__name__}"}

    now = now or datetime.utcnow()
    candidates = select_shadow_vetting_candidates(db, settings_=settings_)
    promoted_ids: list[int] = []
    collecting = 0
    held = 0

    for row in candidates:
        pid = int(row["scan_pattern_id"])
        pattern = db.get(ScanPattern, pid)
        if pattern is None:
            continue
        if row["eligible"]:
            old_status = (pattern.promotion_status or "").strip()
            old_lifecycle = (pattern.lifecycle_stage or "").strip()
            pattern.lifecycle_stage = "promoted"
            pattern.promotion_status = "promoted_via_shadow_vetting"
            pattern.lifecycle_changed_at = now
            pattern.active = True
            promoted_ids.append(pid)
            try:
                from .brain_work.promotion_surface import emit_promotion_surface_change

                emit_promotion_surface_change(
                    db,
                    scan_pattern_id=pid,
                    old_promotion_status=old_status,
                    old_lifecycle_stage=old_lifecycle,
                    new_promotion_status=pattern.promotion_status,
                    new_lifecycle_stage=pattern.lifecycle_stage,
                    source="shadow_vetting_finalizer",
                    extra={
                        "quality_composite_score": row["quality_composite_score"],
                        "score_threshold": row["score_threshold"],
                        "rolling_sample_n": row["rolling_sample_n"],
                        "rolling_directional_wr": row["rolling_directional_wr"],
                    },
                )
            except Exception:
                logger.debug("%s promotion_surface emit failed", LOG_PREFIX, exc_info=True)
        elif row["quality_composite_score"] is None:
            collecting += 1
            pattern.promotion_status = "shadow_collecting_ev"
        else:
            held += 1
            pattern.promotion_status = "shadow_vetted_hold"

    db.commit()
    result = {
        "ok": True,
        "score_result": score_result,
        "shadow_candidates": len(candidates),
        "promoted_count": len(promoted_ids),
        "promoted_ids": promoted_ids,
        "collecting_ev": collecting,
        "held": held,
    }
    logger.info("%s cycle: %s", LOG_PREFIX, result)
    return result
