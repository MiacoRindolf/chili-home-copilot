"""Adaptive CPCV promotion gate (Phase 2 of f-adaptive-promotion-architecture).

Wraps :func:`promotion_gate.promotion_gate_passes` with sample-size-aware,
pool-relative thresholds. The legacy CPCV gate's hardcoded conventions
(``dsr >= 0.95``, ``pbo <= 0.2``, ``median_sharpe >= 0.5``, ``cpcv_n_paths
>= 20``, ``min_trades >= 30``) showed zero discriminatory power on the
current pattern population (Phase 0 audit: DSR pegged at 1.000, PBO at
0.000 across all 39 patterns with CPCV data). This module replaces them
with three operator-policy parameters whose semantics are explicit:

- :attr:`settings.chili_cpcv_target_promotion_pool_pct` (default 0.05) —
  "I want roughly the top 5% of patterns live by each metric." Drives
  the empirical percentile threshold.
- :attr:`settings.chili_cpcv_ci_level` (default 0.90) — "I want 90%
  confidence in the lower-bound estimate." Drives the Hansen-style DSR
  CI and the Wilson-style PBO upper CI.
- :attr:`settings.chili_portfolio_marginal_sharpe_min_bps` (default
  0.0 bps) — "Adding the pattern must improve portfolio CPCV median
  Sharpe by at least N bps." Defaults to a no-op floor (any positive
  contribution admits).

Math:

1. **Bayesian shrinkage.** Each metric is shrunk toward the pool mean
   by ``w = n / (n + n0)`` where ``n0`` is the pool's median trade-count.
   Kills the "11-trade DSR=1.000" inflation pattern 585 exhibits.
2. **Sample-size-aware lower CI.** Hansen-style closed-form approximate
   CI for the shrunken DSR; Wilson binomial bound for PBO.
3. **Empirical percentile threshold.** Eligible per-metric when
   ``lower_CI >= pool_percentile(q)`` where ``q = 1 -
   target_promotion_pool_pct``.
4. **Pareto frontier multi-objective.** Promote only if the candidate
   is not strictly dominated by any pool member across the three
   shrunken metrics simultaneously.
5. **Portfolio marginal Sharpe (lightweight proxy).** Candidate's
   shrunken median Sharpe minus the active roster's mean Sharpe, in
   bps. The full covariance-aware computation is a Phase 3+ refinement;
   this proxy is directionally informative and shadow-logged.

Flag-off default behavior (``chili_cpcv_adaptive_gate_enabled=False``):
returns the legacy ``(ok, reasons)`` tuple unchanged; still writes the
shadow-log so operators can opt into observation without flipping
authority. Flag-on returns the adaptive verdict.

The module is the SINGLE call site for adaptive logic. The only
external wiring is one call from :func:`promotion_gate.finalize_promotion_with_cpcv`.
"""
from __future__ import annotations

import logging
import math
from functools import lru_cache
from typing import Any, Iterable, Mapping, Sequence

from ...config import settings

logger = logging.getLogger(__name__)


_METRIC_NAMES = ("dsr", "pbo", "median_sharpe", "composite")


def adaptive_gate_enabled() -> bool:
    """Cheap predicate for callers that want to short-circuit before computing."""
    return bool(getattr(settings, "chili_cpcv_adaptive_gate_enabled", False))


# ── Math helpers (pure functions; no DB) ───────────────────────────────


def _bayesian_shrinkage(
    raw_value: float,
    n: int,
    pool_mean: float,
    prior_n: int,
) -> float:
    """Shrink ``raw_value`` toward ``pool_mean`` with weight ``n / (n + prior_n)``."""
    n_eff = max(0, int(n))
    n0 = max(1, int(prior_n))
    w = n_eff / (n_eff + n0)
    return w * float(raw_value) + (1.0 - w) * float(pool_mean)


@lru_cache(maxsize=32)
def _z_from_ci(ci_level: float) -> float:
    """One-sided z-score for ``ci_level`` (e.g. 0.90 → ~1.2816)."""
    try:
        from scipy.stats import norm
        return float(norm.ppf(max(0.5, min(0.9999, float(ci_level)))))
    except Exception:
        # Conservative fallback: 90% one-sided ≈ 1.2816.
        return 1.2816


def _hansen_dsr_lower_ci(
    dsr: float,
    n_observations: int,
    ci_level: float,
) -> float:
    """Hansen-style closed-form lower CI for a DSR (probability in [0, 1]).

    The exact Hansen (2005) CI is on the underlying Sharpe; we apply a
    matching closed-form to the DSR probability with SE ≈
    ``sqrt((1 - dsr*dsr) / max(n - 1, 1))``. Tighter as ``n`` grows.
    """
    n = max(1, int(n_observations) - 1)
    d = max(0.0, min(1.0, float(dsr)))
    se = math.sqrt(max(0.0, 1.0 - d * d) / n)
    z = _z_from_ci(ci_level)
    return max(0.0, d - z * se)


def _wilson_pbo_upper_ci(
    pbo: float,
    n_combos: int,
    ci_level: float,
) -> float:
    """Wilson binomial upper CI on PBO (the failure proportion in CSCV).

    ``n_combos`` is the number of combinatorial splits the PBO estimator
    used. Tighter CI as ``n_combos`` grows.
    """
    n = max(1, int(n_combos))
    p = max(0.0, min(1.0, float(pbo)))
    z = _z_from_ci(ci_level)
    denom = 1.0 + (z * z) / n
    center = p + (z * z) / (2.0 * n)
    margin = z * math.sqrt(max(0.0, (p * (1.0 - p) / n) + (z * z) / (4.0 * n * n)))
    return min(1.0, (center + margin) / denom)


def _empirical_percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated empirical quantile; returns None for empty input."""
    arr = [float(v) for v in values if v is not None and not _isnan(v)]
    if not arr:
        return None
    arr.sort()
    n = len(arr)
    if n == 1:
        return arr[0]
    pos = max(0.0, min(1.0, float(q))) * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return arr[lo]
    frac = pos - lo
    return arr[lo] * (1.0 - frac) + arr[hi] * frac


def _isnan(x: Any) -> bool:
    try:
        return math.isnan(float(x))
    except Exception:
        return True


def _pareto_dominated(
    pat: tuple[float, ...],
    pool: Iterable[tuple[float, ...]],
) -> bool:
    """True if ``pat`` is strictly dominated by any pool member.

    Convention: higher is better on every axis. PBO is passed in as its
    *negation* so the dominance semantic stays consistent. Generic over
    tuple width via ``zip`` so Phase 3's 4-tuple (with composite as the
    4th axis) drops in without a math rewrite.
    """
    pat_t = tuple(float(x) for x in pat)
    for q in pool:
        q_t = tuple(float(x) for x in q)
        if len(q_t) != len(pat_t):
            continue
        ge_all = True
        gt_any = False
        for q_i, p_i in zip(q_t, pat_t):
            if q_i < p_i:
                ge_all = False
                break
            if q_i > p_i:
                gt_any = True
        if ge_all and gt_any:
            return True
    return False


def _portfolio_marginal_sharpe_bps(
    candidate_sharpe: float,
    existing_roster_sharpes: Sequence[float],
) -> float:
    """Lightweight proxy: candidate minus mean of roster, in basis points.

    The full covariance-aware portfolio marginal lift requires a per-pattern
    returns matrix and is deferred to Phase 3+. This proxy is directionally
    informative — admits when the candidate's risk-adjusted return clears
    the active roster's average — and is shadow-logged for post-hoc audit.
    """
    cand = float(candidate_sharpe)
    arr = [float(s) for s in existing_roster_sharpes if s is not None and not _isnan(s)]
    if not arr:
        return cand * 10000.0
    return (cand - (sum(arr) / len(arr))) * 10000.0


# ── Pool loader ────────────────────────────────────────────────────────


def _load_pool_metrics(db, *, exclude_pattern_id: int | None) -> dict[str, Any]:
    """Read pool CPCV stats from ``scan_patterns``; safe under partial schema.

    Returns a dict with arrays + aggregates the wrapper consumes. When the
    DB session is unavailable or the table is missing, returns empty
    arrays so the adaptive evaluation degrades to legacy behavior rather
    than failing the gate.
    """
    pool: dict[str, Any] = {
        "n_trades": [],
        "dsr": [],
        "pbo": [],
        "median_sharpe": [],
        "composite": [],
        "lifecycle_promoted_sharpes": [],
        "prior_n": 60,
        "pool_size": 0,
    }
    if db is None:
        return pool
    try:
        from sqlalchemy import text as _text
        rows = db.execute(
            _text(
                """
                SELECT
                    id,
                    COALESCE(corrected_trade_count, trade_count, 0) AS n_trades,
                    deflated_sharpe,
                    pbo,
                    cpcv_median_sharpe,
                    lifecycle_stage,
                    quality_composite_score
                FROM scan_patterns
                WHERE cpcv_n_paths IS NOT NULL
                """
            )
        ).fetchall()
    except Exception as exc:
        logger.warning("[cpcv_adaptive_gate] pool load failed: %s", exc)
        return pool

    for row in rows:
        pid = int(row[0]) if row[0] is not None else None
        if exclude_pattern_id is not None and pid == int(exclude_pattern_id):
            continue
        nt = int(row[1] or 0)
        dsr = row[2]
        pbo = row[3]
        med_sh = row[4]
        stage = (row[5] or "").lower() if row[5] is not None else ""
        comp = row[6] if len(row) > 6 else None
        pool["n_trades"].append(nt)
        if dsr is not None and not _isnan(dsr):
            pool["dsr"].append(float(dsr))
        if pbo is not None and not _isnan(pbo):
            pool["pbo"].append(float(pbo))
        if med_sh is not None and not _isnan(med_sh):
            pool["median_sharpe"].append(float(med_sh))
            if stage == "promoted":
                pool["lifecycle_promoted_sharpes"].append(float(med_sh))
        # f-composite-quality-event-driven (Phase 3, 2026-05-11): 4th
        # Pareto axis. NULL composite is excluded from the pool array
        # — the evaluator imputes pool_mean when the candidate is NULL.
        if comp is not None and not _isnan(comp):
            pool["composite"].append(float(comp))

    pool["pool_size"] = len(pool["n_trades"])
    if pool["n_trades"]:
        sorted_n = sorted(pool["n_trades"])
        mid = len(sorted_n) // 2
        pool["prior_n"] = int(max(1, sorted_n[mid]))
    return pool


# ── Shadow-log writer ──────────────────────────────────────────────────


def _write_eval_log(
    db,
    *,
    scan_pattern_id: int,
    metric_rows: list[dict[str, Any]],
    summary_row: dict[str, Any],
) -> None:
    """Append ~4 audit rows (3 metrics + 1 summary) to ``cpcv_adaptive_eval_log``."""
    if db is None or scan_pattern_id is None:
        return
    try:
        from sqlalchemy import text as _text
        stmt = _text(
            """
            INSERT INTO cpcv_adaptive_eval_log (
                scan_pattern_id,
                metric_name,
                raw_value,
                shrunken_value,
                lower_ci,
                pool_percentile,
                pool_threshold,
                eligible,
                pareto_dominant,
                marginal_portfolio_sharpe_bps,
                legacy_verdict_pass,
                adaptive_verdict_pass
            ) VALUES (
                :scan_pattern_id,
                :metric_name,
                :raw_value,
                :shrunken_value,
                :lower_ci,
                :pool_percentile,
                :pool_threshold,
                :eligible,
                :pareto_dominant,
                :marginal_portfolio_sharpe_bps,
                :legacy_verdict_pass,
                :adaptive_verdict_pass
            )
            """
        )
        for row in metric_rows:
            payload = {
                "scan_pattern_id": int(scan_pattern_id),
                "metric_name": row.get("metric_name"),
                "raw_value": row.get("raw_value"),
                "shrunken_value": row.get("shrunken_value"),
                "lower_ci": row.get("lower_ci"),
                "pool_percentile": row.get("pool_percentile"),
                "pool_threshold": row.get("pool_threshold"),
                "eligible": row.get("eligible"),
                "pareto_dominant": None,
                "marginal_portfolio_sharpe_bps": None,
                "legacy_verdict_pass": None,
                "adaptive_verdict_pass": None,
            }
            db.execute(stmt, payload)
        summary_payload = {
            "scan_pattern_id": int(scan_pattern_id),
            "metric_name": "summary",
            "raw_value": None,
            "shrunken_value": None,
            "lower_ci": None,
            "pool_percentile": None,
            "pool_threshold": None,
            "eligible": summary_row.get("eligible"),
            "pareto_dominant": summary_row.get("pareto_dominant"),
            "marginal_portfolio_sharpe_bps": summary_row.get(
                "marginal_portfolio_sharpe_bps"
            ),
            "legacy_verdict_pass": summary_row.get("legacy_verdict_pass"),
            "adaptive_verdict_pass": summary_row.get("adaptive_verdict_pass"),
        }
        db.execute(stmt, summary_payload)
        try:
            db.commit()
        except Exception:
            # The caller may own the transaction; leave commit to them
            # in that case but don't blow up if the rollback path here
            # would itself fail.
            try:
                db.flush()
            except Exception:
                pass
    except Exception as exc:
        logger.warning("[cpcv_adaptive_gate] shadow-log write failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass


# ── Adaptive evaluator ────────────────────────────────────────────────


def _evaluate_adaptive(
    eval_payload: Mapping[str, Any],
    *,
    pool: Mapping[str, Any],
    family_size: int = 1,
) -> tuple[bool, list[str], list[dict[str, Any]], dict[str, Any]]:
    """Compute (pass, reasons, metric_rows, summary_row) from pool-relative math.

    Phase E (2026-05-14): when ``family_size > 1`` and
    ``chili_family_fdr_enabled`` is True, the DSR pool-percentile
    threshold is replaced with its Benjamini-Hochberg adjustment
    (``family_fdr.bh_adjusted_dsr_threshold``). The raw and adjusted
    thresholds are both surfaced via the DSR metric row so the shadow
    log captures the divergence even when the flag is OFF.
    """
    pool_pct = float(
        getattr(settings, "chili_cpcv_target_promotion_pool_pct", 0.05) or 0.05
    )
    ci_level = float(getattr(settings, "chili_cpcv_ci_level", 0.90) or 0.90)
    margin_min_bps = float(
        getattr(settings, "chili_portfolio_marginal_sharpe_min_bps", 0.0) or 0.0
    )
    q = max(0.0, min(1.0, 1.0 - pool_pct))
    prior_n = int(pool.get("prior_n", 60))

    n_trades = int(
        eval_payload.get("n_trades") or eval_payload.get("n_labeled_samples") or 0
    )
    raw_dsr = eval_payload.get("deflated_sharpe")
    raw_pbo = eval_payload.get("pbo")
    raw_med_sh = eval_payload.get("cpcv_median_sharpe")
    # f-composite-quality-event-driven (Phase 3): 4th axis. The wrapper
    # reads this from ``scan_patterns.quality_composite_score`` and
    # threads it via ``eval_payload`` to avoid touching promotion_gate.
    raw_composite = eval_payload.get("quality_composite_score")

    pool_dsr = list(pool.get("dsr") or [])
    pool_pbo = list(pool.get("pbo") or [])
    pool_med = list(pool.get("median_sharpe") or [])
    pool_comp = list(pool.get("composite") or [])

    dsr_pool_mean = sum(pool_dsr) / len(pool_dsr) if pool_dsr else (raw_dsr or 0.5)
    pbo_pool_mean = sum(pool_pbo) / len(pool_pbo) if pool_pbo else (raw_pbo or 0.5)
    med_pool_mean = sum(pool_med) / len(pool_med) if pool_med else (raw_med_sh or 0.0)
    # Composite pool_mean: Bayesian-shrunken neutral for NULL composites.
    # When the pool is empty (pre-backfill state), fall back to 0.5 so
    # the neutral value sits at the [0,1] midpoint.
    comp_pool_mean = (
        sum(pool_comp) / len(pool_comp)
        if pool_comp
        else (float(raw_composite) if raw_composite is not None else 0.5)
    )

    metric_rows: list[dict[str, Any]] = []
    reasons: list[str] = []
    eligibles: list[bool] = []

    # DSR — higher is better.
    dsr_row: dict[str, Any] = {"metric_name": "dsr"}
    if raw_dsr is None:
        dsr_row.update({
            "raw_value": None,
            "shrunken_value": None,
            "lower_ci": None,
            "pool_percentile": None,
            "pool_threshold": None,
            "eligible": False,
        })
        reasons.append("adaptive_dsr_missing")
        eligibles.append(False)
        shrunk_dsr = float(dsr_pool_mean)
    else:
        shrunk_dsr = _bayesian_shrinkage(
            float(raw_dsr), n_trades, float(dsr_pool_mean), prior_n
        )
        lower = _hansen_dsr_lower_ci(shrunk_dsr, max(2, n_trades), ci_level)
        thr_naive = _empirical_percentile(pool_dsr, q) if pool_dsr else None
        # Phase E (2026-05-14): BH-adjusted family-FDR threshold. Math is
        # pure; the *use* of the adjusted threshold is flag-gated, but
        # the raw vs adjusted divergence is always surfaced into the
        # metric row so the shadow log can replay the comparison.
        try:
            from .family_fdr import (
                bh_adjusted_dsr_threshold,
                family_fdr_enabled,
            )
            fam_m = int(max(1, family_size))
            if thr_naive is not None and fam_m > 1:
                thr_bh = bh_adjusted_dsr_threshold(float(thr_naive), fam_m)
            else:
                thr_bh = (float(thr_naive) if thr_naive is not None else None)
            use_bh = bool(family_fdr_enabled() and fam_m > 1 and thr_bh is not None)
        except Exception:
            thr_bh = (float(thr_naive) if thr_naive is not None else None)
            use_bh = False
        thr = thr_bh if use_bh else thr_naive
        eligible = thr is None or lower >= float(thr)
        dsr_row.update({
            "raw_value": float(raw_dsr),
            "shrunken_value": float(shrunk_dsr),
            "lower_ci": float(lower),
            "pool_percentile": q,
            "pool_threshold": (float(thr) if thr is not None else None),
            "pool_threshold_naive": (
                float(thr_naive) if thr_naive is not None else None
            ),
            "pool_threshold_bh": (float(thr_bh) if thr_bh is not None else None),
            "family_size": int(max(1, family_size)),
            "family_fdr_applied": bool(use_bh),
            "eligible": bool(eligible),
        })
        if not eligible:
            reasons.append("adaptive_dsr_below_pool_threshold")
        eligibles.append(bool(eligible))
    metric_rows.append(dsr_row)

    # PBO — lower is better. Threshold is the (1 - q) pool percentile;
    # eligible when the upper-CI is at or below it.
    pbo_row: dict[str, Any] = {"metric_name": "pbo"}
    if raw_pbo is None:
        pbo_row.update({
            "raw_value": None,
            "shrunken_value": None,
            "lower_ci": None,
            "pool_percentile": None,
            "pool_threshold": None,
            "eligible": False,
        })
        reasons.append("adaptive_pbo_missing")
        eligibles.append(False)
        shrunk_pbo = float(pbo_pool_mean)
    else:
        shrunk_pbo = _bayesian_shrinkage(
            float(raw_pbo), n_trades, float(pbo_pool_mean), prior_n
        )
        n_combos = int(eval_payload.get("n_effective_trials") or 100)
        upper = _wilson_pbo_upper_ci(shrunk_pbo, n_combos, ci_level)
        thr = (
            _empirical_percentile(pool_pbo, max(0.0, min(1.0, 1.0 - q)))
            if pool_pbo
            else None
        )
        eligible = thr is None or upper <= float(thr)
        pbo_row.update({
            "raw_value": float(raw_pbo),
            "shrunken_value": float(shrunk_pbo),
            "lower_ci": float(upper),
            "pool_percentile": max(0.0, min(1.0, 1.0 - q)),
            "pool_threshold": (float(thr) if thr is not None else None),
            "eligible": bool(eligible),
        })
        if not eligible:
            reasons.append("adaptive_pbo_above_pool_threshold")
        eligibles.append(bool(eligible))
    metric_rows.append(pbo_row)

    # Median sharpe — higher is better.
    med_row: dict[str, Any] = {"metric_name": "median_sharpe"}
    if raw_med_sh is None:
        med_row.update({
            "raw_value": None,
            "shrunken_value": None,
            "lower_ci": None,
            "pool_percentile": None,
            "pool_threshold": None,
            "eligible": False,
        })
        reasons.append("adaptive_median_sharpe_missing")
        eligibles.append(False)
        shrunk_med = float(med_pool_mean)
    else:
        shrunk_med = _bayesian_shrinkage(
            float(raw_med_sh), n_trades, float(med_pool_mean), prior_n
        )
        # No closed-form lower CI on median Sharpe without per-path
        # variance — use the same Hansen-style scaling applied to a
        # ``tanh``-mapped value to keep math finite.
        proxy = math.tanh(shrunk_med / max(1.0, abs(shrunk_med) + 1.0))
        lower_proxy = _hansen_dsr_lower_ci(
            (proxy + 1.0) / 2.0, max(2, n_trades), ci_level
        )
        lower = (lower_proxy * 2.0 - 1.0) * max(1.0, abs(shrunk_med) + 1.0)
        thr = _empirical_percentile(pool_med, q) if pool_med else None
        eligible = thr is None or lower >= float(thr)
        med_row.update({
            "raw_value": float(raw_med_sh),
            "shrunken_value": float(shrunk_med),
            "lower_ci": float(lower),
            "pool_percentile": q,
            "pool_threshold": (float(thr) if thr is not None else None),
            "eligible": bool(eligible),
        })
        if not eligible:
            reasons.append("adaptive_median_sharpe_below_pool_threshold")
        eligibles.append(bool(eligible))
    metric_rows.append(med_row)

    # f-composite-quality-event-driven (Phase 3, 2026-05-11): 4th
    # Pareto axis. Composite is already a derived summary in [0,1];
    # we don't apply Hansen / Wilson CI to it, but we DO compare to
    # the empirical pool q-percentile so candidates that score below
    # the active pool's top tier are flagged.
    comp_row: dict[str, Any] = {"metric_name": "composite"}
    composite_present = raw_composite is not None
    if not composite_present:
        # NULL composite → impute pool_mean (Q1 default; brief §"NULL
        # composite handling"). Mark as eligible by default so a missing
        # composite doesn't block promotion during the backfill window.
        if pool_comp:
            shrunk_comp = float(comp_pool_mean)
        else:
            shrunk_comp = float(comp_pool_mean)
        thr = _empirical_percentile(pool_comp, q) if pool_comp else None
        comp_row.update({
            "raw_value": None,
            "shrunken_value": float(shrunk_comp),
            "lower_ci": None,
            "pool_percentile": q,
            "pool_threshold": (float(thr) if thr is not None else None),
            "eligible": True,
        })
        eligibles.append(True)
        if not pool_comp:
            # Log once per evaluation when both candidate and pool are
            # NULL — pre-backfill graceful degradation.
            logger.debug(
                "[cpcv_adaptive_gate] composite axis is empty (pre-backfill); "
                "treating candidate as eligible on this axis"
            )
    else:
        # Raw composite is already a normalized summary in [0,1]; no
        # n-aware shrinkage needed (the underlying components were
        # shrunk when the composite was computed). Keep as-is.
        shrunk_comp = float(raw_composite)
        thr = _empirical_percentile(pool_comp, q) if pool_comp else None
        eligible = thr is None or shrunk_comp >= float(thr)
        comp_row.update({
            "raw_value": float(raw_composite),
            "shrunken_value": float(shrunk_comp),
            "lower_ci": None,
            "pool_percentile": q,
            "pool_threshold": (float(thr) if thr is not None else None),
            "eligible": bool(eligible),
        })
        if not eligible:
            reasons.append("adaptive_composite_below_pool_threshold")
        eligibles.append(bool(eligible))
    metric_rows.append(comp_row)

    # Pareto frontier across (shrunk_dsr, -shrunk_pbo, shrunk_med,
    # shrunk_comp). Pool members with NULL composite take pool_mean so
    # the 4-D comparison is well-defined; matches the candidate's NULL
    # treatment.
    pool_quads: list[tuple[float, float, float, float]] = []
    n_pool = min(len(pool_dsr), len(pool_pbo), len(pool_med))
    for i in range(n_pool):
        comp_i = pool_comp[i] if i < len(pool_comp) else float(comp_pool_mean)
        pool_quads.append(
            (pool_dsr[i], -pool_pbo[i], pool_med[i], float(comp_i))
        )
    candidate_quad = (shrunk_dsr, -shrunk_pbo, shrunk_med, float(shrunk_comp))
    dominated = _pareto_dominated(candidate_quad, pool_quads)
    if dominated:
        reasons.append("adaptive_pareto_dominated")

    # Portfolio marginal Sharpe (lightweight proxy).
    roster_sharpes = list(pool.get("lifecycle_promoted_sharpes") or [])
    marginal_bps = _portfolio_marginal_sharpe_bps(shrunk_med, roster_sharpes)
    if marginal_bps < margin_min_bps:
        reasons.append("adaptive_portfolio_marginal_below_min")

    all_metrics_pass = all(eligibles)
    adaptive_ok = bool(all_metrics_pass and not dominated and marginal_bps >= margin_min_bps)

    summary_row: dict[str, Any] = {
        "eligible": adaptive_ok,
        "pareto_dominant": not dominated,
        "marginal_portfolio_sharpe_bps": float(marginal_bps),
        "legacy_verdict_pass": None,    # filled by wrapper
        "adaptive_verdict_pass": adaptive_ok,
    }
    return adaptive_ok, reasons, metric_rows, summary_row


# ── Public entry point ────────────────────────────────────────────────


def maybe_apply_adaptive_gate(
    eval_payload: Mapping[str, Any],
    *,
    scan_pattern_id: int | None,
    legacy_pass: bool,
    legacy_reasons: list[str],
    db_session: Any | None = None,
) -> tuple[bool, list[str]]:
    """Return the gate verdict (legacy by default; adaptive when flag is on).

    Always writes the shadow log (best-effort) so operators can observe
    legacy/adaptive divergence before flipping authority. Never raises:
    DB failures degrade to the legacy verdict.

    Parameters
    ----------
    eval_payload : Mapping
        Output of the CPCV evaluator (the dict that
        :func:`promotion_gate.finalize_promotion_with_cpcv` builds).
    scan_pattern_id : int or None
        Pattern under evaluation. ``None`` skips the DB-dependent
        path entirely (mining rows without a persisted pattern).
    legacy_pass, legacy_reasons : bool, list
        The legacy ``(ok, reasons)`` tuple from
        :func:`promotion_gate.promotion_gate_passes`. The wrapper
        records both verdicts in the shadow log; the flag controls
        which one is returned.
    db_session : optional
        Inject a session for tests. When ``None``, opens a short-lived
        ``app.db.SessionLocal()`` only if a pattern id is present and
        either the flag is on OR the shadow log is reachable.

    Returns
    -------
    (ok, reasons) : tuple[bool, list[str]]
        Drop-in replacement for
        :func:`promotion_gate.promotion_gate_passes`'s return.
    """
    if eval_payload is None:
        return legacy_pass, list(legacy_reasons or [])

    # Skip when the legacy payload itself was skipped — no metrics to
    # adapt. Preserves legacy semantics.
    if eval_payload.get("skipped"):
        return legacy_pass, list(legacy_reasons or [])

    own_session = False
    db = db_session
    if db is None and scan_pattern_id is not None:
        try:
            from app.db import SessionLocal
            db = SessionLocal()
            own_session = True
        except Exception as exc:
            logger.warning(
                "[cpcv_adaptive_gate] SessionLocal open failed: %s (continuing on legacy)",
                exc,
            )
            db = None

    try:
        pool = _load_pool_metrics(db, exclude_pattern_id=scan_pattern_id) if db is not None else {
            "n_trades": [],
            "dsr": [],
            "pbo": [],
            "median_sharpe": [],
            "composite": [],
            "lifecycle_promoted_sharpes": [],
            "prior_n": 60,
            "pool_size": 0,
        }
        # f-composite-quality-event-driven (Phase 3, 2026-05-11): the
        # wrapper reads ``quality_composite_score`` from the DB for the
        # candidate pattern and threads it into ``eval_payload``. This
        # keeps ``promotion_gate.py`` untouched per the brief's hard
        # constraint. Reads on the candidate row only (one indexed
        # lookup); no joins, no batch.
        if db is not None and scan_pattern_id is not None and (
            eval_payload.get("quality_composite_score") is None
        ):
            try:
                from sqlalchemy import text as _text
                row = db.execute(
                    _text(
                        "SELECT quality_composite_score FROM scan_patterns "
                        "WHERE id = :pid"
                    ),
                    {"pid": int(scan_pattern_id)},
                ).fetchone()
                if row is not None and row[0] is not None and not _isnan(row[0]):
                    eval_payload = dict(eval_payload)
                    eval_payload["quality_composite_score"] = float(row[0])
            except Exception as exc:
                logger.debug(
                    "[cpcv_adaptive_gate] candidate composite read failed "
                    "pid=%s: %s — falling back to pool_mean",
                    scan_pattern_id, exc,
                )
        # Phase E (2026-05-14): resolve the candidate's hypothesis-family
        # size for the BH-adjusted DSR threshold. The lookup is one
        # indexed pattern-row read and falls back to ``1`` (legacy
        # behavior) on any error.
        family_size = 1
        family_label: str | None = None
        if db is not None and scan_pattern_id is not None:
            try:
                from .family_fdr import family_size_for_pattern
                family_size = int(family_size_for_pattern(db, int(scan_pattern_id)))
            except Exception as exc:
                logger.debug(
                    "[cpcv_adaptive_gate] family_size lookup failed pid=%s: %s",
                    scan_pattern_id, exc,
                )
            try:
                from sqlalchemy import text as _text
                row = db.execute(
                    _text(
                        "SELECT hypothesis_family FROM scan_patterns "
                        "WHERE id = :pid"
                    ),
                    {"pid": int(scan_pattern_id)},
                ).fetchone()
                if row is not None and row[0]:
                    family_label = str(row[0])
            except Exception as exc:
                logger.debug(
                    "[cpcv_adaptive_gate] family label lookup failed pid=%s: %s",
                    scan_pattern_id, exc,
                )

        adaptive_ok, adaptive_reasons, metric_rows, summary_row = _evaluate_adaptive(
            eval_payload, pool=pool, family_size=family_size
        )
        summary_row["legacy_verdict_pass"] = bool(legacy_pass)

        if db is not None and scan_pattern_id is not None:
            _write_eval_log(
                db,
                scan_pattern_id=int(scan_pattern_id),
                metric_rows=metric_rows,
                summary_row=summary_row,
            )
            # Phase E: shadow-log the family-FDR trial row regardless of
            # the flag. The verdict snapshotted is the one the wrapper
            # is about to return (legacy when flag OFF, adaptive when
            # ON) so the 7-day soak shows real promotion decisions.
            try:
                from .family_fdr import family_fdr_enabled, log_family_trial
                returned_verdict = (
                    bool(adaptive_ok)
                    if adaptive_gate_enabled()
                    else bool(legacy_pass)
                )
                _ = family_fdr_enabled  # touch import so the helper is reachable in tests
                log_family_trial(
                    db,
                    hypothesis_family=family_label,
                    variant_pattern_id=int(scan_pattern_id),
                    variant_dsr=eval_payload.get("deflated_sharpe"),
                    variant_pbo=eval_payload.get("pbo"),
                    variant_promoted=returned_verdict,
                    family_variants_tested_so_far=family_size,
                )
            except Exception as exc:
                logger.debug(
                    "[cpcv_adaptive_gate] family_fdr trial-log write failed pid=%s: %s",
                    scan_pattern_id, exc,
                )

        logger.info(
            "[cpcv_adaptive_gate] pat=%s legacy_pass=%s adaptive_pass=%s "
            "pool_size=%s family_size=%s reasons=%s marginal_bps=%.3f",
            scan_pattern_id,
            legacy_pass,
            adaptive_ok,
            pool.get("pool_size"),
            family_size,
            adaptive_reasons,
            float(summary_row.get("marginal_portfolio_sharpe_bps") or 0.0),
        )

        if adaptive_gate_enabled():
            return bool(adaptive_ok), list(adaptive_reasons)
        return bool(legacy_pass), list(legacy_reasons or [])
    finally:
        if own_session and db is not None:
            try:
                db.rollback()
            except Exception:
                pass
            try:
                db.close()
            except Exception:
                pass
