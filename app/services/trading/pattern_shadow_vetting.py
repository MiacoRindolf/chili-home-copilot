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

from ...models.trade_relation_symbols import MANAGEMENT_ENVELOPES_RELATION
from ...models.trading import PatternTradeRow, ScanPattern
from .realized_pnl_sql import (
    paper_dynamic_pattern_ev_exit_filter_sql,
    paper_trade_return_fraction_sql,
    trade_return_fraction_sql,
)

logger = logging.getLogger(__name__)
LOG_PREFIX = "[pattern_shadow_vetting]"

OBSERVATION_LIFECYCLES = ("shadow_promoted", "pilot_promoted")


def _is_number(value: Any) -> bool:
    try:
        return not math.isnan(float(value))
    except Exception:
        return False


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
        if math.isnan(out):
            return default
        return out
    except Exception:
        return default


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
    """Existing composite weights, renormalized for adaptive vetting.

    No new raw sample threshold is introduced: pilot/full staging uses the
    operator's existing preference weights for CPCV, DSR, PBO, directional
    correctness, and decay/freshness. Evidence quality affects the score
    continuously through Bayesian shrinkage and time decay.
    """
    raw = {
        "cpcv": _weight(settings_, "chili_cohort_score_weight_cpcv_sharpe", 0.30),
        "dsr": _weight(settings_, "chili_cohort_score_weight_deflated_sharpe", 0.20),
        "pbo": _weight(settings_, "chili_cohort_score_weight_pbo_inverse", 0.15),
        "directional": _weight(settings_, "chili_cohort_score_weight_directional_wr", 0.25),
        "decay": _weight(settings_, "chili_cohort_score_weight_decay_inverse", 0.10),
    }
    total = sum(raw.values())
    if total <= 0:
        return {
            "cpcv": 0.0,
            "dsr": 0.0,
            "pbo": 0.0,
            "directional": 0.0,
            "decay": 0.0,
        }
    return {k: v / total for k, v in raw.items()}


def _median(values: list[float], default: float) -> float:
    arr = sorted(v for v in values if _is_number(v) and v > 0)
    if not arr:
        return float(default)
    mid = len(arr) // 2
    if len(arr) % 2:
        return float(arr[mid])
    return float((arr[mid - 1] + arr[mid]) / 2.0)


def _weighted_rate(rows: list[dict[str, Any]], half_life_hours: float, now: datetime) -> tuple[float, float, float]:
    """Return weighted correct rate, weighted total, and Kish effective N."""
    total_w = 0.0
    total_w2 = 0.0
    correct_w = 0.0
    hl = max(1e-6, float(half_life_hours))
    for row in rows:
        ts = row.get("evaluated_at") or row.get("alert_at") or now
        age_h = max(0.0, (now - ts).total_seconds() / 3600.0)
        w = math.exp(-math.log(2.0) * age_h / hl)
        total_w += w
        total_w2 += w * w
        if bool(row.get("directional_correct")):
            correct_w += w
    if total_w <= 0:
        return 0.5, 0.0, 0.0
    kish_n = (total_w * total_w / total_w2) if total_w2 > 0 else total_w
    return correct_w / total_w, total_w, kish_n


def _load_directional_evidence(
    db: Session,
    *,
    now: datetime | None = None,
    settings_: Any = None,
) -> dict[int, dict[str, Any]]:
    """Time-decayed directional evidence per pattern.

    This replaces the old "30 rows then decide" cliff. Evidence contributes
    as soon as it exists, but stale samples decay, deteriorating recent
    behavior is penalized, and thin evidence carries wider uncertainty.
    """
    if settings_ is None:
        from ...config import settings as _settings

        settings_ = _settings
    now = now or datetime.utcnow()
    rows = db.execute(
        text(
            """
            SELECT
                scan_pattern_id,
                directional_correct,
                alert_at,
                evaluated_at,
                hold_window_hours,
                window_max_favorable_pct,
                window_max_adverse_pct
            FROM pattern_alert_directional_outcome
            WHERE directional_correct IS NOT NULL
            ORDER BY scan_pattern_id, alert_at DESC
            """
        )
    ).mappings().all()

    grouped: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        pid = int(r["scan_pattern_id"])
        grouped.setdefault(pid, []).append(
            {
                "directional_correct": bool(r["directional_correct"]),
                "alert_at": r["alert_at"],
                "evaluated_at": r["evaluated_at"],
                "hold_window_hours": _safe_float(r["hold_window_hours"], 0.0) or 0.0,
                "window_max_favorable_pct": _safe_float(r["window_max_favorable_pct"]),
                "window_max_adverse_pct": _safe_float(r["window_max_adverse_pct"]),
                "source": "directional_outcome",
            }
        )

    if bool(getattr(settings_, "chili_shadow_vetting_include_paper_dynamic_outcomes", True)):
        try:
            paper_rows = db.execute(
                text(
                    f"""
                    SELECT
                        pt.scan_pattern_id,
                        pt.pnl,
                        ({paper_trade_return_fraction_sql("pt")}) * 100.0 AS realized_return_pct,
                        pt.entry_date,
                        pt.exit_date,
                        pt.exit_reason,
                        EXTRACT(EPOCH FROM (pt.exit_date - pt.entry_date)) / 3600.0 AS hold_window_hours
                    FROM trading_paper_trades pt
                    WHERE pt.status = 'closed'
                      AND pt.scan_pattern_id IS NOT NULL
                      AND pt.scan_pattern_id != -1
                      AND pt.entry_date IS NOT NULL
                      AND pt.exit_date IS NOT NULL
                      AND pt.pnl IS NOT NULL
                      AND pt.entry_price > 0
                      AND pt.quantity > 0
                      AND {paper_dynamic_pattern_ev_exit_filter_sql("pt")}
                      AND (
                        pt.paper_shadow_of_alert_id IS NOT NULL
                        OR COALESCE(pt.signal_json, '{{}}'::jsonb) @> '{{"auto_trader_v1": true}}'::jsonb
                        OR COALESCE(pt.signal_json, '{{}}'::jsonb) @> '{{"paper_shadow": true}}'::jsonb
                      )
                    ORDER BY pt.scan_pattern_id, pt.entry_date DESC
                    """
                )
            ).mappings().all()
        except Exception:
            paper_rows = []
            logger.debug("%s paper dynamic evidence query failed", LOG_PREFIX, exc_info=True)
        for r in paper_rows:
            pid = int(r["scan_pattern_id"])
            realized_return_pct = _safe_float(r.get("realized_return_pct"))
            pnl = _safe_float(r.get("pnl"))
            if realized_return_pct is None or pnl is None:
                continue
            grouped.setdefault(pid, []).append(
                {
                    "directional_correct": pnl > 0.0,
                    "alert_at": r.get("entry_date"),
                    "evaluated_at": r.get("exit_date"),
                    "hold_window_hours": _safe_float(r.get("hold_window_hours"), 0.0) or 0.0,
                    "window_max_favorable_pct": max(0.0, realized_return_pct),
                    "window_max_adverse_pct": min(0.0, realized_return_pct),
                    "source": "autotrader_paper_dynamic",
                    "exit_reason": r.get("exit_reason"),
                }
            )

    evidence: dict[int, dict[str, Any]] = {}
    for pid, items in grouped.items():
        alert_times = sorted(
            [r["alert_at"] for r in items if r.get("alert_at") is not None],
            reverse=True,
        )
        gaps_h: list[float] = []
        for newer, older in zip(alert_times, alert_times[1:]):
            gap = (newer - older).total_seconds() / 3600.0
            if gap > 0:
                gaps_h.append(gap)
        hold_h = _median([float(r.get("hold_window_hours") or 0.0) for r in items], 24.0)
        cadence_h = _median(gaps_h, hold_h)
        # The half-life is derived from the strategy's own hold window and
        # observed alert cadence. A bursty 5m pattern forgets old evidence
        # quickly; a slower swing pattern keeps it longer.
        half_life_h = max(hold_h, cadence_h * math.log2(len(items) + 1.0))

        weighted_wr, weighted_n, effective_n = _weighted_rate(items, half_life_h, now)
        fast_wr, _, _ = _weighted_rate(items, max(1e-6, half_life_h / 2.0), now)
        slow_wr, _, _ = _weighted_rate(items, half_life_h * 2.0, now)
        directional_decay = _clip(slow_wr - fast_wr)

        latest_ts = max(
            [
                r.get("evaluated_at") or r.get("alert_at")
                for r in items
                if r.get("evaluated_at") is not None or r.get("alert_at") is not None
            ],
            default=now,
        )
        last_age_h = max(0.0, (now - latest_ts).total_seconds() / 3600.0)
        freshness = math.exp(-math.log(2.0) * last_age_h / max(1e-6, half_life_h))

        fav_weighted = 0.0
        adv_weighted = 0.0
        path_w = 0.0
        for row in items:
            fav = _safe_float(row.get("window_max_favorable_pct"))
            adv = _safe_float(row.get("window_max_adverse_pct"))
            if fav is None or adv is None:
                continue
            ts = row.get("evaluated_at") or row.get("alert_at") or now
            age_h = max(0.0, (now - ts).total_seconds() / 3600.0)
            w = math.exp(-math.log(2.0) * age_h / max(1e-6, half_life_h))
            fav_weighted += w * max(0.0, float(fav))
            adv_weighted += w * abs(min(0.0, float(adv)))
            path_w += w
        if path_w > 0 and (fav_weighted + adv_weighted) > 0:
            path_quality = fav_weighted / (fav_weighted + adv_weighted)
        else:
            path_quality = 0.5

        evidence[pid] = {
            "raw_sample_n": len(items),
            "paper_dynamic_sample_n": sum(
                1 for r in items if r.get("source") == "autotrader_paper_dynamic"
            ),
            "paper_dynamic_exit_sample_n": sum(
                1
                for r in items
                if r.get("source") == "autotrader_paper_dynamic"
                and r.get("exit_reason") == "pattern_exit_now"
            ),
            "weighted_sample_n": weighted_n,
            "effective_sample_n": effective_n,
            "weighted_directional_wr": weighted_wr,
            "recent_directional_wr": fast_wr,
            "slow_directional_wr": slow_wr,
            "directional_decay": directional_decay,
            "freshness": _clip(freshness),
            "path_quality": _clip(path_quality),
            "half_life_hours": half_life_h,
            "last_evaluated_at": latest_ts,
        }
    return evidence


def _load_pilot_rows(
    db: Session,
    *,
    now: datetime | None = None,
    settings_: Any = None,
) -> list[dict[str, Any]]:
    evidence_by_id = _load_directional_evidence(db, now=now, settings_=settings_)
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
                sp.pbo
            FROM scan_patterns sp
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
            "evidence": evidence_by_id.get(int(r[0]), {
                "raw_sample_n": 0,
                "paper_dynamic_sample_n": 0,
                "paper_dynamic_exit_sample_n": 0,
                "weighted_sample_n": 0.0,
                "effective_sample_n": 0.0,
                "weighted_directional_wr": 0.5,
                "recent_directional_wr": 0.5,
                "slow_directional_wr": 0.5,
                "directional_decay": 0.0,
                "freshness": 0.5,
                "path_quality": 0.5,
                "half_life_hours": 24.0,
                "last_evaluated_at": None,
            }),
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
    evidence = dict(row.get("evidence") or {})

    non_directional_weight = weights["cpcv"] + weights["dsr"] + weights["pbo"]
    if non_directional_weight > 0:
        cpcv_prior_mean = (
            weights["cpcv"] * cpcv_component
            + weights["dsr"] * dsr_component
            + weights["pbo"] * pbo_component
        ) / non_directional_weight
    else:
        cpcv_prior_mean = 0.0

    n_obs = max(0.0, float(evidence.get("effective_sample_n") or 0.0))
    directional_wr = _clip(float(evidence.get("weighted_directional_wr") or 0.5))
    n_prior = min(max(1.0, float(row.get("cpcv_n_paths") or 0)), max(1.0, prior_strength))
    posterior_mean = (
        cpcv_prior_mean * n_prior + directional_wr * n_obs
    ) / (n_prior + float(n_obs))
    directional_lcb = _lower_confidence_bound(
        posterior_mean,
        n_prior + float(n_obs),
        float(getattr(settings_, "chili_cpcv_ci_level", 0.90)),
    )
    temporal_component = _clip(
        float(evidence.get("freshness") or 0.0)
        * (1.0 - _clip(float(evidence.get("directional_decay") or 0.0)))
        * (0.5 + 0.5 * _clip(float(evidence.get("path_quality") or 0.5)))
    )
    return _clip(
        weights["cpcv"] * cpcv_component
        + weights["dsr"] * dsr_component
        + weights["pbo"] * pbo_component
        + weights["directional"] * directional_lcb
        + weights["decay"] * temporal_component
    )


def _evidence_maturity(row: dict[str, Any], prior_strength: float) -> float:
    evidence = dict(row.get("evidence") or {})
    n_obs = max(0.0, float(evidence.get("effective_sample_n") or 0.0))
    n_prior = min(max(1.0, float(row.get("cpcv_n_paths") or 0)), max(1.0, prior_strength))
    return _clip(n_obs / (n_obs + n_prior))


def _pilot_policy(
    db: Session,
    *,
    settings_: Any,
    now: datetime | None = None,
) -> dict[str, Any]:
    rows = _load_pilot_rows(db, now=now, settings_=settings_)
    prior_strength = _pilot_prior_strength(rows)
    scored = []
    for row in rows:
        pilot_score = _pilot_score_for_row(
            row, prior_strength=prior_strength, settings_=settings_
        )
        maturity = _evidence_maturity(row, prior_strength)
        scored.append({
            **row,
            "pilot_score": pilot_score,
            "evidence_maturity": maturity,
            "full_score": _clip(pilot_score * maturity),
        })
    pct = float(getattr(settings_, "chili_cpcv_target_promotion_pool_pct", 0.05))
    threshold = _empirical_percentile(
        [float(r["pilot_score"]) for r in scored],
        1.0 - max(0.0, min(1.0, pct)),
    )
    full_threshold = _empirical_percentile(
        [float(r["full_score"]) for r in scored],
        1.0 - max(0.0, min(1.0, pct)),
    )
    return {
        "rows": scored,
        "threshold": threshold,
        "full_threshold": full_threshold,
        "prior_strength": prior_strength,
    }


def pilot_promoted_risk_multiplier(
    db: Session,
    scan_pattern_id: int | None,
    *,
    settings_: Any = None,
) -> float | None:
    """Return a pilot sizing multiplier in [0, 1], or None for non-pilot.

    The multiplier composes the Bayesian pilot score with evidence support:
    CPCV prior support can open a small reversible pilot, and forward evidence
    increases exposure continuously. Full ``promoted`` patterns get normal
    sizing through the regular path.
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
        n_prior = min(
            max(1.0, float(row.get("cpcv_n_paths") or 0)),
            max(1.0, float(policy.get("prior_strength") or 1.0)),
        )
        prior_support = n_prior / (n_prior + max(1.0, float(policy.get("prior_strength") or 1.0)))
        forward_support = _clip(float(row.get("evidence_maturity") or 0.0))
        exposure_support = _clip(
            prior_support + forward_support - prior_support * forward_support
        )
        return _clip(float(row["pilot_score"]) * exposure_support)
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


def _apply_min_pilot_roster(
    rows: list[dict[str, Any]],
    *,
    settings_: Any,
) -> None:
    """Keep the pilot rung from being empty without weakening full promotion.

    The adaptive percentile remains authoritative. This fallback only applies
    when there are fewer current/eligible pilot rows than the configured
    minimum, and candidates must clear hard evidence floors before getting
    reversible micro-risk exposure.
    """
    if not bool(getattr(settings_, "chili_pilot_promoted_enabled", True)):
        return
    target = int(getattr(settings_, "chili_shadow_vetting_min_pilot_roster", 1) or 0)
    if target <= 0:
        return
    current_or_ready = sum(
        1
        for row in rows
        if (
            row.get("lifecycle_stage") == "pilot_promoted"
            and not bool(row.get("recert_required"))
        )
        or row.get("pilot_eligible")
    )
    slots = max(0, target - current_or_ready)
    if slots <= 0:
        return

    min_score = float(getattr(settings_, "chili_shadow_vetting_min_pilot_score", 0.70))
    threshold_ratio = float(
        getattr(settings_, "chili_shadow_vetting_min_pilot_score_threshold_ratio", 0.90)
    )
    min_effective_n = float(
        getattr(settings_, "chili_shadow_vetting_min_pilot_effective_n", 10.0)
    )
    min_weighted_wr = float(
        getattr(settings_, "chili_shadow_vetting_min_pilot_weighted_wr", 0.55)
    )
    min_recent_wr = float(
        getattr(settings_, "chili_shadow_vetting_min_pilot_recent_wr", 0.55)
    )
    min_freshness = float(
        getattr(settings_, "chili_shadow_vetting_min_pilot_freshness", 0.25)
    )
    max_decay = float(
        getattr(settings_, "chili_shadow_vetting_max_pilot_directional_decay", 0.40)
    )

    def _passes(row: dict[str, Any]) -> bool:
        if row.get("pilot_eligible") or row.get("eligible"):
            return False
        if row.get("lifecycle_stage") != "shadow_promoted":
            return False
        if bool(row.get("recert_required")):
            return False
        if not bool(row.get("promotion_gate_passed")) or not bool(row.get("cpcv_ready")):
            return False
        pilot_score = _safe_float(row.get("pilot_score"))
        if pilot_score is None:
            return False
        threshold = _safe_float(row.get("pilot_score_threshold"))
        effective_floor = min_score
        if threshold is not None:
            effective_floor = max(effective_floor, threshold * threshold_ratio)
        if pilot_score < effective_floor:
            return False
        if float(row.get("effective_sample_n") or 0.0) < min_effective_n:
            return False
        if float(row.get("weighted_directional_wr") or 0.0) < min_weighted_wr:
            return False
        if float(row.get("recent_directional_wr") or 0.0) < min_recent_wr:
            return False
        if float(row.get("freshness") or 0.0) < min_freshness:
            return False
        if float(row.get("directional_decay") or 0.0) > max_decay:
            return False
        return True

    candidates = sorted(
        [row for row in rows if _passes(row)],
        key=lambda row: (
            float(row.get("pilot_score") or 0.0),
            float(row.get("effective_sample_n") or 0.0),
            float(row.get("recent_directional_wr") or 0.0),
        ),
        reverse=True,
    )
    for row in candidates[:slots]:
        threshold = _safe_float(row.get("pilot_score_threshold"))
        effective_floor = min_score
        if threshold is not None:
            effective_floor = max(effective_floor, threshold * threshold_ratio)
        row["pilot_eligible"] = True
        row["pilot_min_roster_eligible"] = True
        row["pilot_min_roster_reason"] = "top_clean_shadow_when_pilot_roster_empty"
        row["pilot_min_roster_effective_floor"] = effective_floor


def select_shadow_vetting_candidates(
    db: Session,
    *,
    settings_: Any = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read shadow/pilot patterns with their directional evidence state."""
    if settings_ is None:
        from ...config import settings as _settings

        settings_ = _settings

    threshold = _score_threshold_from_pool(db, settings_=settings_)
    pilot_policy = _pilot_policy(db, settings_=settings_, now=now)
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
                sp.promotion_gate_reasons
                , sp.recert_required
                , sp.recert_reason
            FROM scan_patterns sp
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
        adaptive_full_score = (
            float(pilot_row["full_score"]) if pilot_row and pilot_row.get("full_score") is not None
            else None
        )
        adaptive_full_threshold = pilot_policy.get("full_threshold")
        quality_full_eligible = (
            score is not None
            and threshold is not None
            and score >= threshold
            and bool(row[2])
            and all(row[i] is not None for i in (3, 4, 5))
        )
        adaptive_full_eligible = (
            adaptive_full_score is not None
            and adaptive_full_threshold is not None
            and adaptive_full_score >= float(adaptive_full_threshold)
            and (pilot_row or {}).get("lifecycle_stage") == "pilot_promoted"
            and bool(row[2])
            and all(row[i] is not None for i in (3, 4, 5))
        )
        full_eligible = quality_full_eligible or adaptive_full_eligible
        pilot_eligible = (
            bool(getattr(settings_, "chili_pilot_promoted_enabled", True))
            and pilot_score is not None
            and pilot_threshold is not None
            and pilot_score >= float(pilot_threshold)
            and not bool(row[7])
            and bool(row[2])
            and all(row[i] is not None for i in (3, 4, 5))
        )
        evidence = dict((pilot_row or {}).get("evidence") or {})
        out.append(
            {
                "scan_pattern_id": pid,
                "lifecycle_stage": (pilot_row or {}).get("lifecycle_stage"),
                "quality_composite_score": score,
                "promotion_gate_passed": bool(row[2]),
                "promotion_gate_reasons": list(row[6] or []),
                "recert_required": bool(row[7]),
                "recert_reason": row[8],
                "cpcv_ready": all(row[i] is not None for i in (3, 4, 5)),
                "cpcv_n_paths": int((pilot_row or {}).get("cpcv_n_paths") or 0),
                "cpcv_median_sharpe": (
                    float(row[3]) if row[3] is not None else None
                ),
                "deflated_sharpe": (
                    float(row[4]) if row[4] is not None else None
                ),
                "pbo": float(row[5]) if row[5] is not None else None,
                "raw_sample_n": int(evidence.get("raw_sample_n") or 0),
                "paper_dynamic_sample_n": int(evidence.get("paper_dynamic_sample_n") or 0),
                "paper_dynamic_exit_sample_n": int(
                    evidence.get("paper_dynamic_exit_sample_n") or 0
                ),
                "effective_sample_n": float(evidence.get("effective_sample_n") or 0.0),
                "weighted_directional_wr": float(
                    evidence.get("weighted_directional_wr")
                    if evidence.get("weighted_directional_wr") is not None
                    else 0.5
                ),
                "recent_directional_wr": float(
                    evidence.get("recent_directional_wr")
                    if evidence.get("recent_directional_wr") is not None
                    else 0.5
                ),
                "directional_decay": float(evidence.get("directional_decay") or 0.0),
                "freshness": float(evidence.get("freshness") or 0.0),
                "path_quality": float(evidence.get("path_quality") or 0.5),
                "evidence_maturity": float((pilot_row or {}).get("evidence_maturity") or 0.0),
                "score_threshold": threshold,
                "pilot_score": pilot_score,
                "pilot_score_threshold": pilot_threshold,
                "adaptive_full_score": adaptive_full_score,
                "adaptive_full_threshold": adaptive_full_threshold,
                "quality_full_eligible": quality_full_eligible,
                "adaptive_full_eligible": adaptive_full_eligible,
                "eligible": full_eligible,
                "pilot_eligible": pilot_eligible,
                "pilot_min_roster_eligible": False,
                "pilot_min_roster_reason": None,
                "pilot_min_roster_effective_floor": None,
            }
        )
    _apply_min_pilot_roster(out, settings_=settings_)
    return out


def _shadow_gate_refresh_effective_floor(
    row: dict[str, Any],
    *,
    settings_: Any,
) -> float | None:
    pilot_score = _safe_float(row.get("pilot_score"))
    if pilot_score is None:
        return None
    min_score = float(
        getattr(
            settings_,
            "chili_shadow_vetting_refresh_blocked_gate_min_score",
            getattr(settings_, "chili_shadow_vetting_min_pilot_score", 0.70),
        )
    )
    threshold_ratio = float(
        getattr(
            settings_,
            "chili_shadow_vetting_refresh_blocked_gate_threshold_ratio",
            0.85,
        )
    )
    threshold = _safe_float(row.get("pilot_score_threshold"))
    if threshold is not None:
        min_score = max(min_score, threshold * threshold_ratio)
    return min_score


def _shadow_gate_refresh_candidates(
    rows: list[dict[str, Any]],
    *,
    settings_: Any,
) -> list[dict[str, Any]]:
    """Return stale-gate shadows worth spending a fresh CPCV evaluation on."""
    min_effective_n = float(
        getattr(settings_, "chili_shadow_vetting_min_pilot_effective_n", 10.0)
    )
    min_weighted_wr = float(
        getattr(settings_, "chili_shadow_vetting_min_pilot_weighted_wr", 0.55)
    )
    min_recent_wr = float(
        getattr(settings_, "chili_shadow_vetting_min_pilot_recent_wr", 0.55)
    )
    min_freshness = float(
        getattr(settings_, "chili_shadow_vetting_min_pilot_freshness", 0.25)
    )
    max_decay = float(
        getattr(settings_, "chili_shadow_vetting_max_pilot_directional_decay", 0.40)
    )

    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("lifecycle_stage") != "shadow_promoted":
            continue
        if bool(row.get("promotion_gate_passed")):
            continue
        if not bool(row.get("cpcv_ready")):
            continue
        pilot_score = _safe_float(row.get("pilot_score"))
        effective_floor = _shadow_gate_refresh_effective_floor(
            row, settings_=settings_,
        )
        if pilot_score is None or effective_floor is None or pilot_score < effective_floor:
            continue
        if float(row.get("effective_sample_n") or 0.0) < min_effective_n:
            continue
        if float(row.get("weighted_directional_wr") or 0.0) < min_weighted_wr:
            continue
        if float(row.get("recent_directional_wr") or 0.0) < min_recent_wr:
            continue
        if float(row.get("freshness") or 0.0) < min_freshness:
            continue
        if float(row.get("directional_decay") or 0.0) > max_decay:
            continue
        out.append({
            **row,
            "promotion_gate_refresh_effective_floor": effective_floor,
        })

    return sorted(
        out,
        key=lambda row: (
            float(row.get("pilot_score") or 0.0),
            float(row.get("effective_sample_n") or 0.0),
            float(row.get("recent_directional_wr") or 0.0),
        ),
        reverse=True,
    )


def _gate_rows_from_pattern_trades(
    db: Session,
    pattern: ScanPattern,
) -> list[dict[str, Any]]:
    ptr_rows = (
        db.query(PatternTradeRow)
        .filter(
            PatternTradeRow.scan_pattern_id == int(pattern.id),
            PatternTradeRow.outcome_return_pct.isnot(None),
        )
        .order_by(PatternTradeRow.as_of_ts.asc())
        .all()
    )
    if not ptr_rows:
        kind = str(
            getattr(pattern, "pattern_evidence_kind", None) or "realized_pnl"
        ).strip().lower()
        if kind == "ml_signal":
            return []
        return _gate_rows_from_realized_trade_outcomes(db, pattern)

    from .promotion_gate import normalize_ptr_row_features

    out: list[dict[str, Any]] = []
    for row in ptr_rows:
        features = row.features_json if isinstance(row.features_json, dict) else {}
        out.append(
            normalize_ptr_row_features(
                outcome_return_pct=row.outcome_return_pct,
                as_of_ts=row.as_of_ts,
                ticker=row.ticker,
                timeframe=row.timeframe,
                features_json=features,
            )
        )
    return out


def _gate_rows_from_realized_trade_outcomes(
    db: Session,
    pattern: ScanPattern,
) -> list[dict[str, Any]]:
    """Use qualified live/paper realized returns when PTR evidence is absent."""
    pid = int(getattr(pattern, "id", 0) or 0)
    if pid <= 0:
        return []
    timeframe = (getattr(pattern, "timeframe", None) or "1d").strip() or "1d"
    rows = db.execute(
        text(
            f"""
            WITH live_rows AS (
                SELECT
                    'live_trade' AS source,
                    t.ticker,
                    t.entry_date AS bar_start_utc,
                    ({trade_return_fraction_sql("t")}) AS realized_return
                FROM {MANAGEMENT_ENVELOPES_RELATION} t
                WHERE t.scan_pattern_id = :pid
                  AND t.status = 'closed'
                  AND t.entry_date IS NOT NULL
                  AND t.pnl IS NOT NULL
                  AND t.entry_price > 0
                  AND t.quantity > 0
            ),
            paper_rows AS (
                SELECT
                    'autotrader_paper_dynamic' AS source,
                    pt.ticker,
                    pt.entry_date AS bar_start_utc,
                    ({paper_trade_return_fraction_sql("pt")}) AS realized_return
                FROM trading_paper_trades pt
                WHERE pt.scan_pattern_id = :pid
                  AND pt.status = 'closed'
                  AND pt.entry_date IS NOT NULL
                  AND pt.exit_date IS NOT NULL
                  AND pt.pnl IS NOT NULL
                  AND pt.entry_price > 0
                  AND pt.quantity > 0
                  AND {paper_dynamic_pattern_ev_exit_filter_sql("pt")}
                  AND (
                    pt.paper_shadow_of_alert_id IS NOT NULL
                    OR COALESCE(pt.signal_json, '{{}}'::jsonb) @> '{{"auto_trader_v1": true}}'::jsonb
                    OR COALESCE(pt.signal_json, '{{}}'::jsonb) @> '{{"paper_shadow": true}}'::jsonb
                  )
            )
            SELECT source, ticker, bar_start_utc, realized_return
            FROM (
                SELECT * FROM live_rows
                UNION ALL
                SELECT * FROM paper_rows
            ) evidence
            WHERE realized_return IS NOT NULL
            ORDER BY bar_start_utc ASC
            """
        ),
        {"pid": pid},
    ).mappings().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        ret = _safe_float(row.get("realized_return"))
        ts = row.get("bar_start_utc")
        if ret is None or ts is None:
            continue
        out.append({
            "ret_5d": ret,
            "bar_start_utc": ts,
            "ticker": row.get("ticker") or "UNKNOWN",
            "bar_interval": timeframe,
            "source": row.get("source"),
        })
    return out


def _evaluate_shadow_gate_refresh(
    db: Session,
    pattern: ScanPattern,
    gate_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    from .promotion_gate import (
        _count_variants_in_family,
        cpcv_eval_to_scan_pattern_fields,
        finalize_promotion_with_cpcv,
        persist_cpcv_shadow_eval,
    )

    n_hypotheses = _count_variants_in_family(db, pattern)
    detail = finalize_promotion_with_cpcv(
        {},
        gate_rows,
        n_hypotheses_tested=max(1, int(n_hypotheses or 1)),
        scan_pattern=pattern,
    )
    payload = detail.get("cpcv_promotion_gate") or {}
    patch = cpcv_eval_to_scan_pattern_fields(payload)
    try:
        persist_cpcv_shadow_eval(db, pattern, payload)
    except Exception:
        logger.debug(
            "%s cpcv_shadow_log failed during stale gate refresh",
            LOG_PREFIX,
            exc_info=True,
        )
    return {"payload": payload, "patch": patch}


def refresh_blocked_shadow_promotion_gates(
    db: Session,
    *,
    candidate_rows: list[dict[str, Any]] | None = None,
    settings_: Any = None,
    now: datetime | None = None,
    execute: bool = True,
) -> dict[str, Any]:
    """Refresh stale CPCV gate fields for strong shadows before pilot vetting.

    This does not promote by itself. It only recomputes and persists the same
    promotion-gate columns the normal backtest/CPCV path owns, then the regular
    shadow-vetting cycle decides whether the refreshed pattern can enter pilot.
    """
    if settings_ is None:
        from ...config import settings as _settings

        settings_ = _settings
    if not bool(
        getattr(settings_, "chili_shadow_vetting_refresh_blocked_gate_enabled", True)
    ):
        return {"ok": True, "skipped": "flag_disabled"}

    limit = int(
        getattr(settings_, "chili_shadow_vetting_refresh_blocked_gate_limit", 4) or 0
    )
    if limit <= 0:
        return {"ok": True, "skipped": "limit_disabled"}

    rows = (
        list(candidate_rows)
        if candidate_rows is not None
        else select_shadow_vetting_candidates(db, settings_=settings_, now=now)
    )
    candidates = _shadow_gate_refresh_candidates(rows, settings_=settings_)[:limit]
    planned = [
        {
            "scan_pattern_id": int(row["scan_pattern_id"]),
            "pilot_score": row.get("pilot_score"),
            "pilot_score_threshold": row.get("pilot_score_threshold"),
            "promotion_gate_refresh_effective_floor": row.get(
                "promotion_gate_refresh_effective_floor"
            ),
            "effective_sample_n": row.get("effective_sample_n"),
            "weighted_directional_wr": row.get("weighted_directional_wr"),
            "recent_directional_wr": row.get("recent_directional_wr"),
            "freshness": row.get("freshness"),
        }
        for row in candidates
    ]
    if not candidates:
        return {
            "ok": True,
            "dry_run": not bool(execute),
            "planned": [],
            "planned_count": 0,
            "refreshed_count": 0,
            "refreshed": [],
            "skipped": [],
            "failed": [],
        }
    if not bool(execute):
        return {
            "ok": True,
            "dry_run": True,
            "planned": planned,
            "planned_count": len(planned),
            "refreshed_count": 0,
            "refreshed": [],
            "skipped": [],
            "failed": [],
        }

    min_trades = int(getattr(settings_, "chili_cpcv_min_trades", 15) or 15)
    refreshed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for row in candidates:
        pid = int(row["scan_pattern_id"])
        pattern = db.get(ScanPattern, pid)
        if pattern is None:
            skipped.append({"scan_pattern_id": pid, "reason": "pattern_not_found"})
            continue
        try:
            gate_rows = _gate_rows_from_pattern_trades(db, pattern)
            if len(gate_rows) < min_trades:
                skipped.append({
                    "scan_pattern_id": pid,
                    "reason": "insufficient_pattern_trade_rows",
                    "rows": len(gate_rows),
                    "min_required": min_trades,
                })
                continue
            eval_result = _evaluate_shadow_gate_refresh(db, pattern, gate_rows)
            payload = dict(eval_result.get("payload") or {})
            patch = dict(eval_result.get("patch") or {})
            if not patch:
                skipped.append({
                    "scan_pattern_id": pid,
                    "reason": str(payload.get("reason") or "no_cpcv_patch"),
                })
                continue

            old_gate = bool(pattern.promotion_gate_passed)
            for key, value in patch.items():
                setattr(pattern, key, value)
            db.commit()
            refreshed.append({
                "scan_pattern_id": pid,
                "old_promotion_gate_passed": old_gate,
                "new_promotion_gate_passed": bool(pattern.promotion_gate_passed),
                "promotion_gate_reasons": list(pattern.promotion_gate_reasons or []),
                "cpcv_n_paths": int(pattern.cpcv_n_paths or 0),
                "cpcv_median_sharpe": pattern.cpcv_median_sharpe,
                "deflated_sharpe": pattern.deflated_sharpe,
                "pbo": pattern.pbo,
            })
        except Exception as exc:
            db.rollback()
            logger.warning(
                "%s stale promotion-gate refresh failed pattern_id=%s: %s",
                LOG_PREFIX,
                pid,
                exc,
                exc_info=True,
            )
            failed.append({
                "scan_pattern_id": pid,
                "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
            })

    return {
        "ok": not failed,
        "dry_run": False,
        "planned": planned,
        "planned_count": len(planned),
        "refreshed_count": len(refreshed),
        "refreshed": refreshed,
        "skipped": skipped,
        "failed": failed,
    }


def _negative_realized_gate_hold_reason(
    row: dict[str, Any],
    *,
    settings_: Any,
) -> str | None:
    """Return a hold reason when realized CPCV says a shadow is economically bad."""
    if not bool(
        getattr(settings_, "chili_shadow_vetting_hold_failed_realized_gate_enabled", True)
    ):
        return None
    if row.get("lifecycle_stage") != "shadow_promoted":
        return None
    if bool(row.get("promotion_gate_passed")):
        return None
    reasons = {str(x) for x in (row.get("promotion_gate_reasons") or [])}
    if not reasons or "cpcv_n_paths_below_provisional_min" in reasons:
        return None
    if "adaptive_median_sharpe_below_pool_threshold" not in reasons:
        return None
    med_sharpe = _safe_float(row.get("cpcv_median_sharpe"))
    max_bad = float(
        getattr(settings_, "chili_shadow_vetting_failed_gate_max_median_sharpe", 0.0)
    )
    if med_sharpe is None or med_sharpe > max_bad:
        return None
    cpcv_paths = int(row.get("cpcv_n_paths") or 0)
    if cpcv_paths <= 0:
        return None
    return "negative_realized_cpcv_gate"


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
    initial_candidates = select_shadow_vetting_candidates(db, settings_=settings_, now=now)
    gate_refresh_result = refresh_blocked_shadow_promotion_gates(
        db,
        candidate_rows=initial_candidates,
        settings_=settings_,
        now=now,
    )
    score_result_after_gate_refresh = None
    if int(gate_refresh_result.get("refreshed_count") or 0) > 0:
        try:
            from .pattern_quality_score import compute_and_persist_scores

            score_result_after_gate_refresh = compute_and_persist_scores(
                db, settings_=settings_,
            )
        except Exception as exc:
            db.rollback()
            logger.warning(
                "%s post gate-refresh score refresh failed: %s",
                LOG_PREFIX,
                exc,
                exc_info=True,
            )
            return {
                "ok": False,
                "error": f"post_gate_refresh_score_failed:{type(exc).__name__}",
                "promotion_gate_refresh": gate_refresh_result,
            }
    candidates = select_shadow_vetting_candidates(db, settings_=settings_, now=now)
    alpha_gate_snapshot: dict[str, Any] | None = None
    alpha_gate_allows_full_risk = True
    alpha_gate_allows_pilot_risk = True
    alpha_gate_full_block_reasons: list[str] = []
    alpha_gate_pilot_block_reasons: list[str] = []
    if bool(getattr(settings_, "chili_alpha_portfolio_gate_enabled", False)):
        try:
            from .alpha_portfolio_gate import broker_risk_allowed

            alpha_gate_allows_full_risk, alpha_gate_snapshot = broker_risk_allowed(
                db, settings_=settings_,
            )
            alpha_gate_full_block_reasons = list(
                (alpha_gate_snapshot or {}).get("full_promotion_block_reasons") or []
            )
            # Full promoted risk still obeys the portfolio gate strictly.
            # Pilot risk is intentionally smaller and reversible; stale recert
            # debt and missing execution samples should not prevent the system
            # from collecting the very live/paper evidence that resolves them.
            pilot_hard_blocks = {
                "p90_slippage_above_limit",
                "execution_health_query_failed",
                "execution_health_not_clean",
            }
            alpha_gate_pilot_block_reasons = sorted(
                set(alpha_gate_full_block_reasons).intersection(pilot_hard_blocks)
            )
            alpha_gate_allows_pilot_risk = not alpha_gate_pilot_block_reasons
        except Exception as exc:
            db.rollback()
            logger.warning(
                "%s alpha portfolio gate failed: %s",
                LOG_PREFIX,
                exc,
                exc_info=True,
            )
            alpha_gate_allows_full_risk = False
            alpha_gate_allows_pilot_risk = False
            alpha_gate_full_block_reasons = [f"alpha_portfolio_gate_failed:{type(exc).__name__}"]
            alpha_gate_pilot_block_reasons = list(alpha_gate_full_block_reasons)
            alpha_gate_snapshot = {
                "ok": False,
                "error": f"alpha_portfolio_gate_failed:{type(exc).__name__}",
            }

    promoted_ids: list[int] = []
    pilot_ids: list[int] = []
    realized_ev_blocked_ids: list[int] = []
    realized_gate_held_ids: list[int] = []
    collecting = 0
    held = 0

    for row in candidates:
        if not alpha_gate_allows_full_risk:
            row["eligible"] = False
            row["alpha_portfolio_blocked"] = True
        if not alpha_gate_allows_pilot_risk:
            row["pilot_eligible"] = False
            row["alpha_portfolio_pilot_blocked"] = True
        pid = int(row["scan_pattern_id"])
        pattern = db.get(ScanPattern, pid)
        if pattern is None:
            continue
        old_lifecycle = (pattern.lifecycle_stage or "").strip().lower()
        old_status_for_hold = (pattern.promotion_status or "").strip()
        realized_gate_hold_reason = _negative_realized_gate_hold_reason(
            row,
            settings_=settings_,
        )
        if realized_gate_hold_reason:
            pattern.lifecycle_stage = "challenged"
            pattern.promotion_status = "shadow_realized_gate_failed"
            pattern.lifecycle_changed_at = now
            pattern.active = False
            pattern.recert_required = False
            pattern.recert_reason = realized_gate_hold_reason
            realized_gate_held_ids.append(pid)
            try:
                from .brain_work.promotion_surface import emit_promotion_surface_change

                emit_promotion_surface_change(
                    db,
                    scan_pattern_id=pid,
                    old_promotion_status=old_status_for_hold,
                    old_lifecycle_stage=old_lifecycle,
                    new_promotion_status=pattern.promotion_status,
                    new_lifecycle_stage=pattern.lifecycle_stage,
                    source="shadow_vetting_realized_gate_hold",
                    extra={
                        "reason": realized_gate_hold_reason,
                        "cpcv_n_paths": row.get("cpcv_n_paths"),
                        "cpcv_median_sharpe": row.get("cpcv_median_sharpe"),
                        "deflated_sharpe": row.get("deflated_sharpe"),
                        "pbo": row.get("pbo"),
                        "promotion_gate_reasons": row.get("promotion_gate_reasons") or [],
                        "pilot_score": row.get("pilot_score"),
                        "pilot_score_threshold": row.get("pilot_score_threshold"),
                    },
                )
            except Exception:
                logger.debug(
                    "%s realized gate hold surface emit failed",
                    LOG_PREFIX,
                    exc_info=True,
                )
            continue
        if row["eligible"] and bool(
            getattr(settings_, "chili_shadow_vetting_require_realized_ev_for_full", True)
        ):
            try:
                from .realized_ev_gate import check_realized_ev_blocking

                ev_blocked, ev_reasons, ev_snapshot = check_realized_ev_blocking(pattern)
            except Exception as exc:
                ev_blocked = True
                ev_reasons = [f"realized_ev_gate_failed:{type(exc).__name__}"]
                ev_snapshot = {"ok": False, "error": str(exc)[:500]}
                logger.warning(
                    "%s realized EV full-promotion gate failed pattern_id=%s: %s",
                    LOG_PREFIX,
                    pid,
                    exc,
                    exc_info=True,
                )
            row["realized_ev_gate"] = {
                "blocked": bool(ev_blocked),
                "reasons": list(ev_reasons),
                "snapshot": dict(ev_snapshot or {}),
            }
            if ev_blocked:
                row["eligible"] = False
                row["realized_ev_blocked"] = True
                realized_ev_blocked_ids.append(pid)
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
                        "adaptive_full_score": row["adaptive_full_score"],
                        "adaptive_full_threshold": row["adaptive_full_threshold"],
                        "quality_full_eligible": row["quality_full_eligible"],
                        "adaptive_full_eligible": row["adaptive_full_eligible"],
                        "raw_sample_n": row["raw_sample_n"],
                        "effective_sample_n": row["effective_sample_n"],
                        "weighted_directional_wr": row["weighted_directional_wr"],
                        "recent_directional_wr": row["recent_directional_wr"],
                        "directional_decay": row["directional_decay"],
                        "freshness": row["freshness"],
                        "realized_ev_gate": row.get("realized_ev_gate"),
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
                            "evidence_maturity": row["evidence_maturity"],
                            "raw_sample_n": row["raw_sample_n"],
                            "effective_sample_n": row["effective_sample_n"],
                            "weighted_directional_wr": row["weighted_directional_wr"],
                            "recent_directional_wr": row["recent_directional_wr"],
                            "directional_decay": row["directional_decay"],
                            "freshness": row["freshness"],
                            "path_quality": row["path_quality"],
                            "pilot_min_roster_eligible": row.get(
                                "pilot_min_roster_eligible"
                            ),
                            "pilot_min_roster_reason": row.get(
                                "pilot_min_roster_reason"
                            ),
                            "pilot_min_roster_effective_floor": row.get(
                                "pilot_min_roster_effective_floor"
                            ),
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
        "score_result_after_gate_refresh": score_result_after_gate_refresh,
        "promotion_gate_refresh": gate_refresh_result,
        "shadow_candidates": len(candidates),
        "promoted_count": len(promoted_ids),
        "promoted_ids": promoted_ids,
        "pilot_count": len(pilot_ids),
        "pilot_ids": pilot_ids,
        "realized_ev_blocked_count": len(realized_ev_blocked_ids),
        "realized_ev_blocked_ids": realized_ev_blocked_ids,
        "realized_gate_held_count": len(realized_gate_held_ids),
        "realized_gate_held_ids": realized_gate_held_ids,
        "collecting_ev": collecting,
        "held": held,
        "alpha_portfolio_gate": {
            "enabled": bool(getattr(settings_, "chili_alpha_portfolio_gate_enabled", False)),
            "full_risk_allowed": alpha_gate_allows_full_risk,
            "pilot_risk_allowed": alpha_gate_allows_pilot_risk,
            "full_block_reasons": alpha_gate_full_block_reasons,
            "pilot_block_reasons": alpha_gate_pilot_block_reasons,
        },
    }
    logger.info("%s cycle: %s", LOG_PREFIX, result)
    return result
