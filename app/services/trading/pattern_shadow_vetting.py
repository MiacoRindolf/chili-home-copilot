"""Finalize CHILI-vetted patterns through shadow -> pilot -> promoted.

``shadow_promoted`` is CHILI's broker-blocked observation stage: patterns
have passed CPCV and are allowed to emit pattern-imminent alerts, but the
autotrader does not place broker orders from them.

``pilot_promoted`` is the staged-ramp lifecycle. It is broker-eligible, but
the autotrader sizes it by the Bayesian pilot confidence returned here rather
than treating it like a mature ``promoted`` pattern. That lets strong CPCV
patterns prove themselves with capped exposure while directional evidence is
still thin.

The final gate is pool-relative, not an arbitrary fixed score:
``quality_composite_score`` must clear the same top-pool policy used by the
adaptive CPCV gate (``chili_cpcv_target_promotion_pool_pct``). Thin shadows
can enter pilot when their Bayesian CPCV+directional lower bound clears the
same pool-relative policy; otherwise they remain shadow-only and keep
collecting directional outcomes.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from statistics import NormalDist
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models.trading import ScanPattern

logger = logging.getLogger(__name__)
LOG_PREFIX = "[pattern_shadow_vetting]"

OBSERVATION_LIFECYCLES = ("shadow_promoted", "pilot_promoted")


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


def _clip(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _z_from_ci(ci_level: float) -> float:
    return float(NormalDist().inv_cdf(max(0.5, min(0.9999, float(ci_level)))))


def _lower_confidence_bound(p: float, n: float, ci_level: float) -> float:
    n_eff = max(1.0, float(n))
    p_eff = _clip(float(p))
    se = math.sqrt(max(0.0, p_eff * (1.0 - p_eff)) / n_eff)
    return _clip(p_eff - _z_from_ci(ci_level) * se)


def _weight(settings_: Any, key: str, default: float) -> float:
    return max(0.0, float(getattr(settings_, key, default)))


def _pilot_weights(settings_: Any) -> dict[str, float]:
    """Existing composite weights, renormalized without decay.

    No new pilot threshold is introduced: pilot uses the operator's existing
    preference weights for CPCV, DSR, PBO, and directional correctness.
    """
    raw = {
        "cpcv": _weight(settings_, "chili_cohort_score_weight_cpcv_sharpe", 0.30),
        "dsr": _weight(settings_, "chili_cohort_score_weight_deflated_sharpe", 0.20),
        "pbo": _weight(settings_, "chili_cohort_score_weight_pbo_inverse", 0.15),
        "directional": _weight(settings_, "chili_cohort_score_weight_directional_wr", 0.25),
    }
    total = sum(raw.values())
    if total <= 0:
        return {"cpcv": 0.0, "dsr": 0.0, "pbo": 0.0, "directional": 0.0}
    return {k: v / total for k, v in raw.items()}


def _load_pilot_rows(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT
                sp.id,
                sp.lifecycle_stage,
                sp.promotion_gate_passed,
                sp.cpcv_n_paths,
                sp.cpcv_median_sharpe,
                sp.deflated_sharpe,
                sp.pbo,
                COALESCE(q.rolling_sample_n, 0) AS rolling_sample_n,
                q.rolling_directional_wr,
                COALESCE(o.correct_n, 0) AS correct_n
            FROM scan_patterns sp
            LEFT JOIN pattern_directional_quality_v q
              ON q.scan_pattern_id = sp.id
            LEFT JOIN (
                SELECT scan_pattern_id,
                       SUM(CASE WHEN directional_correct THEN 1 ELSE 0 END) AS correct_n
                FROM pattern_alert_directional_outcome
                WHERE directional_correct IS NOT NULL
                GROUP BY scan_pattern_id
            ) o ON o.scan_pattern_id = sp.id
            WHERE sp.active IS TRUE
              AND sp.cpcv_n_paths IS NOT NULL
              AND sp.cpcv_median_sharpe IS NOT NULL
              AND sp.deflated_sharpe IS NOT NULL
              AND sp.pbo IS NOT NULL
            """
        )
    ).fetchall()
    return [
        {
            "scan_pattern_id": int(r[0]),
            "lifecycle_stage": (r[1] or "").strip().lower(),
            "promotion_gate_passed": bool(r[2]),
            "cpcv_n_paths": int(r[3] or 0),
            "cpcv_median_sharpe": float(r[4]),
            "deflated_sharpe": float(r[5]),
            "pbo": float(r[6]),
            "rolling_sample_n": int(r[7] or 0),
            "rolling_directional_wr": float(r[8]) if r[8] is not None else None,
            "correct_n": int(r[9] or 0),
        }
        for r in rows
    ]


def _pilot_prior_strength(rows: list[dict[str, Any]]) -> float:
    paths = sorted(
        float(r["cpcv_n_paths"]) for r in rows if int(r.get("cpcv_n_paths") or 0) > 0
    )
    if not paths:
        return 1.0
    mid = len(paths) // 2
    if len(paths) % 2:
        return paths[mid]
    return (paths[mid - 1] + paths[mid]) / 2.0


def _pilot_score_for_row(
    row: dict[str, Any],
    *,
    prior_strength: float,
    settings_: Any,
) -> float:
    weights = _pilot_weights(settings_)
    cpcv_component = _clip(float(row["cpcv_median_sharpe"]) / 2.0)
    dsr_component = _clip(float(row["deflated_sharpe"]))
    pbo_component = 1.0 - _clip(float(row["pbo"]))

    non_directional_weight = weights["cpcv"] + weights["dsr"] + weights["pbo"]
    if non_directional_weight > 0:
        cpcv_prior_mean = (
            weights["cpcv"] * cpcv_component
            + weights["dsr"] * dsr_component
            + weights["pbo"] * pbo_component
        ) / non_directional_weight
    else:
        cpcv_prior_mean = 0.0

    n_obs = int(row.get("rolling_sample_n") or 0)
    correct_n = int(row.get("correct_n") or 0)
    n_prior = min(max(1.0, float(row.get("cpcv_n_paths") or 0)), max(1.0, prior_strength))
    posterior_mean = (
        cpcv_prior_mean * n_prior + float(correct_n)
    ) / (n_prior + float(n_obs))
    directional_lcb = _lower_confidence_bound(
        posterior_mean,
        n_prior + float(n_obs),
        float(getattr(settings_, "chili_cpcv_ci_level", 0.90)),
    )
    return _clip(
        weights["cpcv"] * cpcv_component
        + weights["dsr"] * dsr_component
        + weights["pbo"] * pbo_component
        + weights["directional"] * directional_lcb
    )


def _pilot_policy(db: Session, *, settings_: Any) -> dict[str, Any]:
    rows = _load_pilot_rows(db)
    prior_strength = _pilot_prior_strength(rows)
    scored = [
        {
            **row,
            "pilot_score": _pilot_score_for_row(
                row, prior_strength=prior_strength, settings_=settings_
            ),
        }
        for row in rows
    ]
    pct = float(getattr(settings_, "chili_cpcv_target_promotion_pool_pct", 0.05))
    threshold = _empirical_percentile(
        [float(r["pilot_score"]) for r in scored],
        1.0 - max(0.0, min(1.0, pct)),
    )
    return {
        "rows": scored,
        "threshold": threshold,
        "prior_strength": prior_strength,
    }


def pilot_promoted_risk_multiplier(
    db: Session,
    scan_pattern_id: int | None,
    *,
    settings_: Any = None,
) -> float | None:
    """Return a pilot sizing multiplier in [0, 1], or None for non-pilot.

    The multiplier is the current Bayesian pilot score itself: full
    ``promoted`` patterns get normal sizing, while pilot patterns get exposure
    proportional to evidence confidence. No separate arbitrary cap is needed.
    """
    if not scan_pattern_id:
        return None
    if settings_ is None:
        from ...config import settings as _settings

        settings_ = _settings
    if not bool(getattr(settings_, "chili_pilot_promoted_enabled", True)):
        return None
    policy = _pilot_policy(db, settings_=settings_)
    for row in policy["rows"]:
        if int(row["scan_pattern_id"]) != int(scan_pattern_id):
            continue
        if row["lifecycle_stage"] != "pilot_promoted":
            return None
        threshold = policy["threshold"]
        if threshold is None or float(row["pilot_score"]) < float(threshold):
            return 0.0
        return _clip(float(row["pilot_score"]))
    return None


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
    """Read shadow/pilot patterns with their directional evidence state."""
    if settings_ is None:
        from ...config import settings as _settings

        settings_ = _settings

    threshold = _score_threshold_from_pool(db, settings_=settings_)
    pilot_policy = _pilot_policy(db, settings_=settings_)
    pilot_by_id = {int(r["scan_pattern_id"]): r for r in pilot_policy["rows"]}
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
              AND sp.lifecycle_stage IN ('shadow_promoted', 'pilot_promoted')
            ORDER BY
                sp.quality_composite_score DESC NULLS LAST,
                sp.cpcv_median_sharpe DESC NULLS LAST,
                sp.id ASC
            """
        )
    ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        pid = int(row[0])
        score = float(row[1]) if row[1] is not None else None
        pilot_row = pilot_by_id.get(pid)
        pilot_score = float(pilot_row["pilot_score"]) if pilot_row else None
        pilot_threshold = pilot_policy["threshold"]
        full_eligible = (
            score is not None
            and threshold is not None
            and score >= threshold
            and int(row[6] or 0) >= 30
            and bool(row[2])
            and all(row[i] is not None for i in (3, 4, 5))
        )
        pilot_eligible = (
            bool(getattr(settings_, "chili_pilot_promoted_enabled", True))
            and pilot_score is not None
            and pilot_threshold is not None
            and pilot_score >= float(pilot_threshold)
            and bool(row[2])
            and all(row[i] is not None for i in (3, 4, 5))
        )
        out.append(
            {
                "scan_pattern_id": pid,
                "lifecycle_stage": (pilot_row or {}).get("lifecycle_stage"),
                "quality_composite_score": score,
                "promotion_gate_passed": bool(row[2]),
                "cpcv_ready": all(row[i] is not None for i in (3, 4, 5)),
                "rolling_sample_n": int(row[6] or 0),
                "rolling_directional_wr": float(row[7]) if row[7] is not None else None,
                "score_threshold": threshold,
                "pilot_score": pilot_score,
                "pilot_score_threshold": pilot_threshold,
                "eligible": full_eligible,
                "pilot_eligible": pilot_eligible,
            }
        )
    return out


def run_shadow_vetting_cycle(
    db: Session,
    *,
    now: datetime | None = None,
    settings_: Any = None,
) -> dict[str, Any]:
    """Advance shadow/pilot patterns according to adaptive evidence policy."""
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
    pilot_ids: list[int] = []
    collecting = 0
    held = 0

    for row in candidates:
        pid = int(row["scan_pattern_id"])
        pattern = db.get(ScanPattern, pid)
        if pattern is None:
            continue
        old_lifecycle = (pattern.lifecycle_stage or "").strip().lower()
        if row["eligible"]:
            old_status = (pattern.promotion_status or "").strip()
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
        elif row["pilot_eligible"]:
            if old_lifecycle != "pilot_promoted":
                old_status = (pattern.promotion_status or "").strip()
                pattern.lifecycle_stage = "pilot_promoted"
                pattern.promotion_status = "pilot_via_shadow_vetting"
                pattern.lifecycle_changed_at = now
                pattern.active = True
                pilot_ids.append(pid)
                try:
                    from .brain_work.promotion_surface import emit_promotion_surface_change

                    emit_promotion_surface_change(
                        db,
                        scan_pattern_id=pid,
                        old_promotion_status=old_status,
                        old_lifecycle_stage=old_lifecycle,
                        new_promotion_status=pattern.promotion_status,
                        new_lifecycle_stage=pattern.lifecycle_stage,
                        source="shadow_vetting_pilot",
                        extra={
                            "pilot_score": row["pilot_score"],
                            "pilot_score_threshold": row["pilot_score_threshold"],
                            "rolling_sample_n": row["rolling_sample_n"],
                            "rolling_directional_wr": row["rolling_directional_wr"],
                        },
                    )
                except Exception:
                    logger.debug("%s pilot surface emit failed", LOG_PREFIX, exc_info=True)
            else:
                pattern.promotion_status = "pilot_collecting_ev"
        elif row["quality_composite_score"] is None:
            collecting += 1
            pattern.lifecycle_stage = "shadow_promoted"
            pattern.promotion_status = "shadow_collecting_ev"
        else:
            held += 1
            if old_lifecycle == "pilot_promoted":
                pattern.lifecycle_stage = "shadow_promoted"
                pattern.promotion_status = "pilot_ev_cooling"
            else:
                pattern.promotion_status = "shadow_vetted_hold"

    db.commit()
    result = {
        "ok": True,
        "score_result": score_result,
        "shadow_candidates": len(candidates),
        "promoted_count": len(promoted_ids),
        "promoted_ids": promoted_ids,
        "pilot_count": len(pilot_ids),
        "pilot_ids": pilot_ids,
        "collecting_ev": collecting,
        "held": held,
    }
    logger.info("%s cycle: %s", LOG_PREFIX, result)
    return result
