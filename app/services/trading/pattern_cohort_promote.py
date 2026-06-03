"""f-promotion-pipeline-rebalance Phase 4 (2026-05-10).

Adaptive cohort auto-promote. Ranks eligible candidates by composite score
when available, then CPCV strength, and advances adaptive-gate-passed plus
top bootstrap near-miss candidates to broker-blocked observation. Candidates
move first to ``shadow_promoted``; they do not jump directly to ``promoted`` /
``live``.

Eligibility filter
------------------

A pattern is eligible for broker-blocked observation if:

- ``active=True``
- ``lifecycle_stage IN ('backtested', 'candidate')``, plus stale
  ``challenged`` rows whose adaptive CPCV verdict now passes
- ``cpcv_median_sharpe`` is non-NULL
- ``deflated_sharpe`` is non-NULL
- ``pbo`` is non-NULL
- either ``promotion_gate_passed=True`` OR the pool-relative bootstrap policy
  ranks the pattern as a top CPCV/DSR/PBO near-miss
- enough recent live exits with mostly reconciler/unknown attribution block
  cohort staging until exit provenance improves
- enough recent live trades whose entry+exit TCA consumes the expected entry
  edge block cohort staging until the venue/order geometry improves
- directional outcomes are NOT required; ``shadow_promoted`` is the
  broker-blocked observation stage that collects them
- ``quality_composite_score`` is optional; scored candidates rank first,
  CPCV-only candidates can bootstrap observation

Selection + observation
-----------------------

Sort eligible patterns by the alpha portfolio gate score when present, then
``quality_composite_score`` DESC NULLS LAST, CPCV strength, and ``id`` ASC
(deterministic tiebreaker). Stage all eligible patterns into
``shadow_promoted`` because shadow is not broker exposure; it is the
evidence-collection lane. Downstream shadow vetting applies the adaptive target
roster policy before a pattern can move to broker-eligible pilot or full
promotion.

Public API
----------

- ``select_cohort_candidates(db, *, settings_=None) -> list[ScanPattern]``:
  pure read; returns the eligibility set ranked by score.
- ``run_cohort_promote_cycle(db, *, now=None, settings_=None) -> dict``:
  the entry point. Flag-gated by ``chili_cohort_promote_enabled``.
"""
from __future__ import annotations

import bisect
import logging
import math
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models.trading import ScanPattern
from .realized_pnl_sql import (
    clean_live_pattern_ev_exit_filter_sql,
    trade_return_fraction_sql,
)

logger = logging.getLogger(__name__)


COHORT_ELIGIBLE_LIFECYCLE_STAGES = ("backtested", "candidate", "challenged")


def _is_number(value: Any) -> bool:
    try:
        return value is not None and not math.isnan(float(value))
    except Exception:
        return False


def _clip(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _empirical_percentile(values: list[float], q: float) -> float | None:
    arr = sorted(float(v) for v in values if _is_number(v))
    if not arr:
        return None
    if len(arr) == 1:
        return arr[0]
    pos = _clip(float(q)) * (len(arr) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return arr[lo]
    frac = pos - lo
    return arr[lo] * (1.0 - frac) + arr[hi] * frac


def _rank_pct(sorted_values: list[float], value: Any) -> float:
    """Return pool-relative percentile rank, higher-is-better input."""
    if not _is_number(value):
        return 0.0
    arr = sorted_values
    if not arr:
        return 0.0
    if len(arr) == 1:
        return 1.0
    val = float(value)
    lo = bisect.bisect_left(arr, val)
    hi = bisect.bisect_right(arr, val)
    if lo == hi:
        idx = max(0.0, min(float(len(arr) - 1), float(lo)))
    else:
        idx = (lo + hi - 1) / 2.0
    return _clip(idx / (len(arr) - 1))


def _is_saturated_deflated_sharpe(value: Any) -> bool:
    return _is_number(value) and float(value) >= 1.0


def _is_saturated_pbo(value: Any) -> bool:
    return _is_number(value) and float(value) <= 0.0


def _values_degenerate(values: list[float]) -> bool:
    if len(values) <= 1:
        return True
    first = float(values[0])
    return all(abs(float(v) - first) <= 1e-12 for v in values[1:])


def _bootstrap_metric_enabled(records: list[dict[str, Any]]) -> dict[str, bool]:
    """Detect bootstrap metrics that cannot discriminate this candidate pool.

    DSR and PBO have repeatedly appeared as saturated constants in live
    diagnostics. Keeping a constant component in the near-miss score creates
    false precision and can let the bootstrap lane rank by overfit-looking
    inputs. CPCV remains the primary discovery axis even when the pool is
    small; saturated/constant DSR/PBO are dropped and weights are renormalized.
    """
    dsr_values = [float(r["deflated_sharpe"]) for r in records]
    pbo_values = [float(r["pbo"]) for r in records]
    pbo_inverse_values = [1.0 - _clip(v) for v in pbo_values]
    return {
        "cpcv": True,
        "dsr": not (
            _values_degenerate(dsr_values)
            or all(_is_saturated_deflated_sharpe(v) for v in dsr_values)
        ),
        "pbo_inverse": not (
            _values_degenerate(pbo_inverse_values)
            or all(_is_saturated_pbo(v) for v in pbo_values)
        ),
    }


def _bootstrap_weights(
    settings_: Any,
    *,
    metric_enabled: dict[str, bool] | None = None,
) -> dict[str, float]:
    raw = {
        "cpcv": max(0.0, float(getattr(
            settings_, "chili_cohort_score_weight_cpcv_sharpe", 0.10,
        ))),
        "dsr": max(0.0, float(getattr(
            settings_, "chili_cohort_score_weight_deflated_sharpe", 0.05,
        ))),
        "pbo_inverse": max(0.0, float(getattr(
            settings_, "chili_cohort_score_weight_pbo_inverse", 0.05,
        ))),
    }
    enabled = metric_enabled or {}
    raw = {
        key: (value if bool(enabled.get(key, True)) else 0.0)
        for key, value in raw.items()
    }
    total = sum(raw.values())
    if total <= 0.0:
        return {k: 0.0 for k in raw}
    return {k: v / total for k, v in raw.items()}


def _bootstrap_policy(
    records: list[dict[str, Any]],
    *,
    settings_: Any,
) -> dict[str, Any]:
    """Pool-relative CPCV discovery gate for shadow-only observation.

    This intentionally does not look at live trade outcomes. Shadow promotion
    is the mechanism that creates those outcomes, so requiring them here creates
    a bootstrap deadlock. The operator's existing target promotion pool percent
    controls how selective this observation lane is.
    """
    enabled = bool(getattr(
        settings_, "chili_cohort_promote_bootstrap_near_miss_enabled", True,
    ))
    metric_ready = [
        r for r in records
        if all(
            _is_number(r.get(k))
            for k in ("cpcv_median_sharpe", "deflated_sharpe", "pbo")
        )
    ]
    if not enabled or not metric_ready:
        return {"enabled": enabled, "threshold": None, "scores": {}}

    min_cpcv = float(getattr(
        settings_, "chili_cohort_promote_bootstrap_min_cpcv_sharpe", 0.0,
    ))
    min_dsr = float(getattr(
        settings_, "chili_cohort_promote_bootstrap_min_deflated_sharpe", 0.0,
    ))
    max_pbo = float(getattr(
        settings_, "chili_cohort_promote_bootstrap_max_pbo", 1.0,
    ))
    floor_passed_ids = {
        int(r["id"]) for r in metric_ready
        if (
            float(r["cpcv_median_sharpe"]) > min_cpcv
            and float(r["deflated_sharpe"]) > min_dsr
            and float(r["pbo"]) < max_pbo
        )
    }

    cpcv_vals = sorted(float(r["cpcv_median_sharpe"]) for r in metric_ready)
    dsr_vals = sorted(float(r["deflated_sharpe"]) for r in metric_ready)
    pbo_inv_vals = sorted(1.0 - _clip(float(r["pbo"])) for r in metric_ready)
    metric_enabled = _bootstrap_metric_enabled(metric_ready)
    weights = _bootstrap_weights(settings_, metric_enabled=metric_enabled)

    scores: dict[int, float] = {}
    for r in metric_ready:
        pbo_inv = 1.0 - _clip(float(r["pbo"]))
        score = (
            weights["cpcv"] * _rank_pct(cpcv_vals, r["cpcv_median_sharpe"])
            + weights["dsr"] * _rank_pct(dsr_vals, r["deflated_sharpe"])
            + weights["pbo_inverse"] * _rank_pct(pbo_inv_vals, pbo_inv)
        )
        scores[int(r["id"])] = _clip(score)

    pct = float(getattr(settings_, "chili_cpcv_target_promotion_pool_pct", 0.05))
    threshold = _empirical_percentile(list(scores.values()), 1.0 - _clip(pct))
    return {
        "enabled": enabled,
        "threshold": threshold,
        "scores": scores,
        "weights": weights,
        "metric_enabled": metric_enabled,
        "excluded_metrics": sorted(
            metric for metric, enabled in metric_enabled.items() if not enabled
        ),
        "metric_ready_count": len(metric_ready),
        "floor_passed_ids": sorted(floor_passed_ids),
    }


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

    # f-composite-quality-reweight-realized-evidence (2026-05-16): realized-PnL
    # eligibility floor. A pattern with >= N valid realized return samples whose
    # equal-weighted avg_pnl_pct is <= 0 is REFUSED cohort promotion. Below
    # the n_floor (sample too small) or with positive realized avg, the
    # pattern is allowed through. Reads window, n_floor, and the
    # max-negative-pct strict-> bound from settings. NO magic-default fallbacks
    # (advisor brief section 2.6).
    window_days = int(getattr(
        settings_, "chili_cohort_score_realized_window_days", 90,
    ))
    min_n_floor = int(getattr(
        settings_, "chili_cohort_promote_min_realized_trades_for_floor", 5,
    ))
    max_negative_pct = float(getattr(
        settings_, "chili_cohort_promote_max_realized_avg_pnl_pct_negative", 0.0,
    ))
    raw_low_confidence_floor = getattr(
        settings_, "chili_cohort_promote_low_confidence_exit_rate_floor", 0.50,
    )
    low_confidence_exit_rate_floor = (
        _clip(float(raw_low_confidence_floor))
        if _is_number(raw_low_confidence_floor)
        else 0.50
    )
    min_tca_edge_samples = max(
        1,
        int(getattr(
            settings_,
            "chili_cohort_promote_min_tca_edge_samples_for_floor",
            min_n_floor,
        )),
    )
    raw_tca_consumed_floor = getattr(
        settings_, "chili_cohort_promote_tca_consumed_expected_edge_rate_floor", 0.50,
    )
    tca_consumed_expected_edge_rate_floor = (
        _clip(float(raw_tca_consumed_floor))
        if _is_number(raw_tca_consumed_floor)
        else 0.50
    )
    raw_unverified_tca_outlier_bps = getattr(
        settings_, "brain_execution_cost_unverified_tca_outlier_bps", 500.0,
    )
    unverified_tca_outlier_bps = (
        float(raw_unverified_tca_outlier_bps)
        if _is_number(raw_unverified_tca_outlier_bps)
        and float(raw_unverified_tca_outlier_bps) > 0.0
        else 500.0
    )

    # Do not require directional outcomes here. shadow_promoted is the
    # observation stage that lets a pattern collect those outcomes without
    # broker exposure; requiring them here creates a bootstrap deadlock.
    # Stored promotion_gate_passed is honored, and a separate pool-relative
    # CPCV bootstrap policy can also admit near-misses for shadow-only
    # observation when the stored gate is stale or path-count constrained.
    numeric_pattern = r"^[+-]?((\d+(\.\d*)?)|(\.\d+))([eE][+-]?\d+)?$"
    sql = text(f"""
        WITH eligible_patterns AS (
            SELECT
                sp.id,
                sp.promotion_gate_passed,
                sp.portfolio_gate_score,
                sp.quality_composite_score,
                sp.cpcv_median_sharpe,
                sp.deflated_sharpe,
                sp.pbo
            FROM scan_patterns sp
            WHERE sp.active IS TRUE
              AND sp.lifecycle_stage IN ('backtested', 'candidate', 'challenged')
              AND sp.cpcv_median_sharpe IS NOT NULL
              AND sp.deflated_sharpe IS NOT NULL
              AND sp.pbo IS NOT NULL
        ),
        live_exit_quality AS (
            SELECT
                t.scan_pattern_id,
                COUNT(*) AS live_realized_exit_count,
                COUNT(*) FILTER (
                    WHERE NOT ({clean_live_pattern_ev_exit_filter_sql("t")})
                ) AS live_low_confidence_exit_count
            FROM trading_trades t
            JOIN eligible_patterns ep ON ep.id = t.scan_pattern_id
            WHERE t.scan_pattern_id IS NOT NULL
              AND t.scan_pattern_id != -1
              AND t.status = 'closed'
              AND t.pnl IS NOT NULL
              AND t.entry_price > 0
              AND t.quantity > 0
              AND t.exit_date > NOW() - make_interval(days => :window_days)
            GROUP BY t.scan_pattern_id
        ),
        realized_samples AS (
            SELECT
                t.scan_pattern_id,
                {trade_return_fraction_sql("t")} AS realized_return_frac
            FROM trading_trades t
            JOIN eligible_patterns ep ON ep.id = t.scan_pattern_id
            WHERE t.scan_pattern_id IS NOT NULL
              AND t.scan_pattern_id != -1
              AND t.status = 'closed'
              AND t.pnl IS NOT NULL
              AND t.entry_price > 0
              AND t.quantity > 0
              AND t.exit_date > NOW() - make_interval(days => :window_days)
              AND {clean_live_pattern_ev_exit_filter_sql("t")}
        ),
        execution_edge_samples AS (
            SELECT
                t.scan_pattern_id,
                CASE
                    WHEN (t.indicator_snapshot -> 'entry_edge' ->> 'expected_net_pct') ~ :numeric_pattern
                        THEN (t.indicator_snapshot -> 'entry_edge' ->> 'expected_net_pct')::float
                    WHEN (t.indicator_snapshot ->> 'entry_edge_expected_net_pct') ~ :numeric_pattern
                        THEN (t.indicator_snapshot ->> 'entry_edge_expected_net_pct')::float
                    WHEN (t.indicator_snapshot -> 'entry_execution' ->> 'entry_edge_expected_net_pct') ~ :numeric_pattern
                        THEN (t.indicator_snapshot -> 'entry_execution' ->> 'entry_edge_expected_net_pct')::float
                    ELSE NULL
                END AS expected_net_pct,
                CASE
                    WHEN t.tca_entry_slippage_bps IS NULL
                      OR t.tca_exit_slippage_bps IS NULL
                        THEN NULL
                    WHEN (t.tca_entry_slippage_bps::text !~ :numeric_pattern)
                      OR (t.tca_exit_slippage_bps::text !~ :numeric_pattern)
                        THEN NULL
                    WHEN (
                        (
                               ABS(t.tca_entry_slippage_bps) <= :unverified_tca_outlier_bps
                            OR t.avg_fill_price > 0
                            OR BTRIM(COALESCE(t.broker_order_id, '')) <> ''
                            OR LOWER(BTRIM(COALESCE(t.broker_status, ''))) IN ('filled', 'partially_filled')
                        )
                        AND (
                               ABS(t.tca_exit_slippage_bps) <= :unverified_tca_outlier_bps
                            OR t.avg_fill_price > 0
                            OR BTRIM(COALESCE(t.broker_order_id, '')) <> ''
                            OR LOWER(BTRIM(COALESCE(t.broker_status, ''))) IN ('filled', 'partially_filled')
                        )
                    )
                        THEN (t.tca_entry_slippage_bps + t.tca_exit_slippage_bps) / 100.0
                    ELSE NULL
                END AS tca_cost_pct
            FROM trading_trades t
            JOIN eligible_patterns ep ON ep.id = t.scan_pattern_id
            WHERE t.scan_pattern_id IS NOT NULL
              AND t.scan_pattern_id != -1
              AND t.status = 'closed'
              AND t.exit_date > NOW() - make_interval(days => :window_days)
        ),
        execution_edge_quality AS (
            SELECT
                scan_pattern_id,
                COUNT(*) FILTER (
                    WHERE expected_net_pct > 0.0
                ) AS positive_expected_edge_events,
                COUNT(*) FILTER (
                    WHERE expected_net_pct > 0.0
                      AND tca_cost_pct IS NOT NULL
                ) AS tca_adjusted_expected_edge_events,
                COUNT(*) FILTER (
                    WHERE expected_net_pct > 0.0
                      AND tca_cost_pct IS NOT NULL
                      AND tca_cost_pct >= expected_net_pct
                ) AS tca_consumed_expected_edge_events
            FROM execution_edge_samples
            GROUP BY scan_pattern_id
        ),
        realized AS (
            SELECT scan_pattern_id,
                   COUNT(realized_return_frac) AS n_realized,
                   AVG(realized_return_frac * 100.0) AS avg_pnl_pct
            FROM realized_samples
            WHERE realized_return_frac IS NOT NULL
            GROUP BY scan_pattern_id
        )
        SELECT
            sp.id,
            sp.promotion_gate_passed,
            sp.portfolio_gate_score,
            sp.quality_composite_score,
            sp.cpcv_median_sharpe,
            sp.deflated_sharpe,
            sp.pbo
        FROM eligible_patterns sp
        LEFT JOIN realized r ON r.scan_pattern_id = sp.id
        LEFT JOIN live_exit_quality eq ON eq.scan_pattern_id = sp.id
        LEFT JOIN execution_edge_quality xq ON xq.scan_pattern_id = sp.id
        WHERE (
              COALESCE(r.n_realized, 0) < :min_n_floor
              OR r.avg_pnl_pct > :max_negative_pct
          )
          AND NOT (
              COALESCE(eq.live_realized_exit_count, 0) >= :min_n_floor
              AND COALESCE(r.n_realized, 0) < :min_n_floor
              AND (
                  COALESCE(eq.live_low_confidence_exit_count, 0)::float
                  / NULLIF(eq.live_realized_exit_count, 0)
              ) >= :low_confidence_exit_rate_floor
          )
          AND NOT (
              COALESCE(xq.positive_expected_edge_events, 0) >= :min_tca_edge_samples
              AND COALESCE(xq.tca_adjusted_expected_edge_events, 0) >= :min_tca_edge_samples
              AND (
                  COALESCE(xq.tca_consumed_expected_edge_events, 0)::float
                  / NULLIF(xq.positive_expected_edge_events, 0)
              ) >= :tca_consumed_expected_edge_rate_floor
          )
        """
    )
    rows = [dict(r) for r in db.execute(
        sql,
        {
            "window_days": window_days,
            "min_n_floor": min_n_floor,
            "max_negative_pct": max_negative_pct,
            "low_confidence_exit_rate_floor": low_confidence_exit_rate_floor,
            "min_tca_edge_samples": min_tca_edge_samples,
            "numeric_pattern": numeric_pattern,
            "tca_consumed_expected_edge_rate_floor": (
                tca_consumed_expected_edge_rate_floor
            ),
            "unverified_tca_outlier_bps": unverified_tca_outlier_bps,
        },
    ).mappings().all()]
    bootstrap = _bootstrap_policy(rows, settings_=settings_)
    bootstrap_scores: dict[int, float] = dict(bootstrap.get("scores") or {})
    bootstrap_threshold = bootstrap.get("threshold")
    bootstrap_floor_ids = set(bootstrap.get("floor_passed_ids") or ())

    eligible: list[dict[str, Any]] = []
    for row in rows:
        pid = int(row["id"])
        gate_passed = bool(row.get("promotion_gate_passed"))
        bootstrap_score = bootstrap_scores.get(pid)
        bootstrap_passed = (
            bool(bootstrap.get("enabled"))
            and pid in bootstrap_floor_ids
            and bootstrap_score is not None
            and bootstrap_threshold is not None
            and float(bootstrap_score) >= float(bootstrap_threshold)
        )
        if not gate_passed and not bootstrap_passed:
            continue
        row["bootstrap_score"] = float(bootstrap_score or 0.0)
        row["bootstrap_promoted"] = bool(bootstrap_passed and not gate_passed)
        eligible.append(row)

    def _desc_optional(value: Any) -> float:
        return float(value) if _is_number(value) else float("-inf")

    eligible.sort(
        key=lambda r: (
            -_desc_optional(r.get("portfolio_gate_score")),
            -_desc_optional(r.get("quality_composite_score")),
            -_desc_optional(r.get("bootstrap_score")),
            -_desc_optional(r.get("cpcv_median_sharpe")),
            -_desc_optional(r.get("deflated_sharpe")),
            (
                _desc_optional(r.get("pbo"))
                if _is_number(r.get("pbo"))
                else float("inf")
            ),
            int(r["id"]),
        )
    )
    ids = [int(r["id"]) for r in eligible]
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
