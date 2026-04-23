"""Purged / segmented validation for mined pattern candidates (lightweight CPCV-style).

Includes:
- Time-segmented validation (purged CPCV lite)
- Bootstrap resampling for confidence intervals
- Ensemble confirmation (2-of-3 methods must agree for promotion)
- Multiple hypothesis correction (Bonferroni-style)
- Deflated Sharpe Ratio (DSR) — Bailey & Lopez de Prado
- Probability of Backtest Overfitting (PBO) via CSCV
- Temporal holdout enforcement (mining ≠ validation data)
- Per-run trial counting for honest multiple-testing correction
"""
from __future__ import annotations

import logging
import math
import random
from datetime import datetime
from typing import Any, Callable

import numpy as np
from scipy import stats as sp_stats

logger = logging.getLogger(__name__)

MIN_TRADES_FOR_PROMOTION = 30

# ── Per-run trial counter (reset each mine_patterns call) ─────────────

_current_run_trial_count: int = 0


def reset_trial_counter() -> None:
    """Reset at the start of each mining run."""
    global _current_run_trial_count
    _current_run_trial_count = 0


def increment_trial_counter() -> int:
    """Call once per candidate filter evaluated. Returns new count."""
    global _current_run_trial_count
    _current_run_trial_count += 1
    return _current_run_trial_count


def get_trial_count() -> int:
    return _current_run_trial_count


def _row_time_key(row: dict[str, Any]) -> float:
    b = row.get("bar_start_utc")
    if isinstance(b, datetime):
        return b.timestamp()
    if hasattr(b, "timestamp"):
        try:
            return float(b.timestamp())  # type: ignore[no-any-return]
        except Exception:
            pass
    return 0.0


def mined_candidate_passes_purged_segments(
    filtered: list[dict[str, Any]],
    *,
    n_segments: int = 3,
    min_samples_per_segment: int = 5,
    min_positive_segments: int = 3,
    min_segment_mean_5d_pct: float = 0.05,
) -> tuple[bool, dict[str, Any]]:
    """Time-ordered segments: require positive mean 5d return in enough segments.

    Approximates combinatorial purged CV for discovery: unstable edges fail when
    performance concentrates in one era.
    """
    if not filtered:
        return False, {"reason": "empty"}

    ordered = sorted(filtered, key=_row_time_key)
    n = len(ordered)
    segs = max(2, int(n_segments))
    seg_len = max(1, n // segs)
    detail: dict[str, Any] = {"segments": []}
    positive = 0
    for s in range(segs):
        lo = s * seg_len
        hi = (s + 1) * seg_len if s < segs - 1 else n
        chunk = ordered[lo:hi]
        if not chunk:
            detail["segments"].append({"index": s, "n": 0, "skipped": True})
            continue
        if len(chunk) < min_samples_per_segment:
            detail["segments"].append({"index": s, "n": len(chunk), "skipped": True})
            continue
        avg_5d = sum(float(r.get("ret_5d") or 0) for r in chunk) / len(chunk)
        seg_ok = avg_5d > min_segment_mean_5d_pct
        if seg_ok:
            positive += 1
        detail["segments"].append({
            "index": s,
            "n": len(chunk),
            "avg_5d": round(avg_5d, 4),
            "positive": seg_ok,
        })

    ok = positive >= min_positive_segments
    detail["positive_segments"] = positive
    detail["passes"] = ok
    return ok, detail


def filter_with_purged_gate(
    rows: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
    **kw: Any,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
    """Apply predicate then purged-segment gate to the filtered subset."""
    filt = [r for r in rows if predicate(r)]
    ok, meta = mined_candidate_passes_purged_segments(filt, **kw)
    return filt, ok, meta


def bootstrap_win_rate_ci(
    filtered: list[dict[str, Any]],
    *,
    n_resamples: int = 1000,
    ci_level: float = 0.95,
    return_key: str = "ret_5d",
) -> dict[str, Any]:
    """Bootstrap resample to get confidence interval on win-rate and avg return.

    Returns {win_rate_mean, win_rate_lower, win_rate_upper, avg_return_mean,
    avg_return_lower, avg_return_upper, n, passes_50pct_wr}.
    """
    if len(filtered) < 5:
        return {"error": "too_few_samples", "n": len(filtered)}
    if n_resamples < 1:
        return {"error": "invalid_n_resamples", "n": len(filtered)}

    rets = [float(r.get(return_key) or 0) for r in filtered]
    n = len(rets)
    if n < 1:
        return {"error": "too_few_samples", "n": 0}

    boot_wrs: list[float] = []
    boot_rets: list[float] = []

    rng = random.Random(42)
    for _ in range(n_resamples):
        sample = rng.choices(rets, k=n)
        w = sum(1 for r in sample if r > 0)
        boot_wrs.append(w / n * 100)
        boot_rets.append(sum(sample) / n)

    boot_wrs.sort()
    boot_rets.sort()

    alpha = (1 - ci_level) / 2
    lo_idx = max(0, min(int(alpha * n_resamples), n_resamples - 1))
    hi_idx = max(0, min(int((1 - alpha) * n_resamples) - 1, n_resamples - 1))

    wr_mean = sum(boot_wrs) / n_resamples
    ret_mean = sum(boot_rets) / n_resamples

    return {
        "n": n,
        "win_rate_mean": round(wr_mean, 2),
        "win_rate_lower": round(boot_wrs[lo_idx], 2),
        "win_rate_upper": round(boot_wrs[hi_idx], 2),
        "avg_return_mean": round(ret_mean, 4),
        "avg_return_lower": round(boot_rets[lo_idx], 4),
        "avg_return_upper": round(boot_rets[hi_idx], 4),
        "passes_50pct_wr": boot_wrs[lo_idx] > 50.0,
    }


def ensemble_promotion_check(
    filtered: list[dict[str, Any]],
    *,
    min_agree: int = 2,
) -> tuple[bool, dict[str, Any]]:
    """Run 3 independent validation methods. Promote only if >=min_agree pass.

    Methods:
    1. Purged segment validation (all 3 segments positive)
    2. Bootstrap CI (lower bound of win-rate > 50%)
    3. Walk-forward half-split (both halves profitable)
    """
    results: dict[str, Any] = {"methods": {}}
    votes = 0

    # Method 1: Purged segments
    seg_ok, seg_detail = mined_candidate_passes_purged_segments(filtered)
    results["methods"]["purged_segments"] = {"pass": seg_ok, **seg_detail}
    if seg_ok:
        votes += 1

    # Method 2: Bootstrap CI
    boot = bootstrap_win_rate_ci(filtered)
    boot_ok = boot.get("passes_50pct_wr", False) and boot.get("avg_return_lower", -1) > 0
    results["methods"]["bootstrap_ci"] = {"pass": boot_ok, **boot}
    if boot_ok:
        votes += 1

    # Method 3: Walk-forward half-split
    ordered = sorted(filtered, key=_row_time_key)
    mid = len(ordered) // 2
    first_half = ordered[:mid]
    second_half = ordered[mid:]
    wf_ok = False
    wf_detail: dict[str, Any] = {}
    n1, n2 = len(first_half), len(second_half)
    if n1 >= 5 and n2 >= 5 and n1 > 0 and n2 > 0:
        avg_1 = sum(float(r.get("ret_5d") or 0) for r in first_half) / n1
        avg_2 = sum(float(r.get("ret_5d") or 0) for r in second_half) / n2
        wr_1 = sum(1 for r in first_half if float(r.get("ret_5d") or 0) > 0) / n1 * 100
        wr_2 = sum(1 for r in second_half if float(r.get("ret_5d") or 0) > 0) / n2 * 100
        wf_ok = avg_1 > 0 and avg_2 > 0 and wr_2 >= 50
        wf_detail = {
            "first_half_avg": round(avg_1, 4), "second_half_avg": round(avg_2, 4),
            "first_half_wr": round(wr_1, 1), "second_half_wr": round(wr_2, 1),
            "first_n": len(first_half), "second_n": len(second_half),
        }
    results["methods"]["walk_forward_split"] = {"pass": wf_ok, **wf_detail}
    if wf_ok:
        votes += 1

    results["votes"] = votes
    results["required"] = min_agree
    results["promoted"] = votes >= min_agree
    return votes >= min_agree, results


def decay_signals_from_walk_forward_windows(
    windows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fold-level spread and simple late-vs-early decay hints from bench windows."""
    rets: list[float] = []
    wrs: list[float] = []
    for w in windows:
        if not w.get("ok"):
            continue
        rp = w.get("return_pct")
        if rp is not None:
            rets.append(float(rp))
        wr = w.get("win_rate")
        if wr is not None:
            wrs.append(float(wr))
    if len(rets) < 2:
        return {}
    spread = max(rets) - min(rets)
    slope_neg = bool(rets[-1] < rets[0])
    out: dict[str, Any] = {
        "fold_return_spread": round(spread, 4),
        "first_last_return_slope_neg": slope_neg,
    }
    if len(wrs) >= 2:
        out["fold_wr_spread"] = round(max(wrs) - min(wrs), 4)
    return out


def _promotion_min_ensemble_hypothesis(
    filtered: list[dict[str, Any]],
    *,
    min_trades: int,
    n_hypotheses_tested: int,
) -> tuple[bool, dict[str, Any]]:
    """Minimum sample + ensemble + optional multiple-hypothesis correction (no CPCV)."""
    detail: dict[str, Any] = {}

    if len(filtered) < min_trades:
        detail["blocked"] = "insufficient_samples"
        detail["n"] = len(filtered)
        detail["min_required"] = min_trades
        return False, detail

    ensemble_ok, ensemble_detail = ensemble_promotion_check(filtered)
    detail["ensemble"] = ensemble_detail

    if not ensemble_ok:
        detail["blocked"] = "ensemble_failed"
        return False, detail

    if n_hypotheses_tested > 1:
        boot = bootstrap_win_rate_ci(filtered)
        raw_wr_lower = boot.get("win_rate_lower", 0)
        corrected_threshold = 50.0 + (n_hypotheses_tested * 0.1)
        corrected_threshold = min(corrected_threshold, 65.0)
        passes_corrected = raw_wr_lower > corrected_threshold
        detail["hypothesis_correction"] = {
            "n_tested": n_hypotheses_tested,
            "raw_wr_lower": raw_wr_lower,
            "corrected_threshold": round(corrected_threshold, 1),
            "passes": passes_corrected,
        }
        if not passes_corrected:
            detail["blocked"] = "hypothesis_correction_failed"
            return False, detail

    return True, detail


def _finalize_cpcv_promotion_ready(
    detail: dict[str, Any],
    filtered: list[dict[str, Any]],
    *,
    n_hypotheses_tested: int,
) -> tuple[bool, dict[str, Any]]:
    from .promotion_gate import finalize_promotion_with_cpcv

    detail = finalize_promotion_with_cpcv(
        detail,
        filtered,
        n_hypotheses_tested=n_hypotheses_tested,
    )
    if detail.get("blocked") == "cpcv_promotion_gate_failed":
        return False, detail
    detail["ready"] = True
    return True, detail


def check_promotion_ready(
    filtered: list[dict[str, Any]],
    *,
    min_trades: int = MIN_TRADES_FOR_PROMOTION,
    n_hypotheses_tested: int = 1,
) -> tuple[bool, dict[str, Any]]:
    """Combined promotion gate: minimum sample + ensemble + multiple hypothesis correction.

    Args:
        filtered: Historical row dicts with ret_5d.
        min_trades: Minimum evidence count before promotion is considered.
        n_hypotheses_tested: How many patterns were tested in this cycle
            (for Bonferroni-style alpha correction).

    Returns:
        (ready, detail_dict) where ready is True only if all gates pass.
    """
    ok, detail = _promotion_min_ensemble_hypothesis(
        filtered,
        min_trades=min_trades,
        n_hypotheses_tested=n_hypotheses_tested,
    )
    if not ok:
        return False, detail
    return _finalize_cpcv_promotion_ready(
        detail,
        filtered,
        n_hypotheses_tested=n_hypotheses_tested,
    )


# ── Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014) ────────────


def compute_deflated_sharpe_ratio(
    returns: list[float],
    *,
    n_trials: int,
    risk_free_rate: float = 0.0,
    annualization: float = 252.0,
) -> dict[str, Any]:
    """Compute the Deflated Sharpe Ratio that accounts for selection bias.

    DSR tests the null hypothesis that the observed Sharpe ratio is no
    better than the expected maximum Sharpe under the null of *n_trials*
    independent strategies with zero true Sharpe.

    Uses the Bailey & Lopez de Prado (2014) formula:
        E[max(SR)] ≈ (1 - γ) * Φ^{-1}(1 - 1/N) + γ * Φ^{-1}(1 - 1/(N*e))
    where γ ≈ 0.5772 (Euler-Mascheroni), N = n_trials.
    Then DSR = Φ( (SR_obs - E[max(SR)]) / SE(SR_obs) ).
    """
    n = len(returns)
    if n < 10 or n_trials < 1:
        return {"dsr": None, "sharpe_observed": None, "reason": "insufficient_data"}

    arr = np.array(returns, dtype=float)
    excess = arr - risk_free_rate / annualization
    mean_r = float(np.mean(excess))
    std_r = float(np.std(excess, ddof=1))
    if std_r < 1e-12:
        return {"dsr": None, "sharpe_observed": 0.0, "reason": "zero_variance"}

    sr_obs = mean_r / std_r * math.sqrt(annualization)
    skew = float(sp_stats.skew(excess))
    kurt = float(sp_stats.kurtosis(excess, fisher=True))

    # SE of Sharpe accounting for non-normality (Lo, 2002)
    se_sr = math.sqrt(
        (1.0 + 0.5 * sr_obs**2 - skew * sr_obs + (kurt / 4.0) * sr_obs**2) / max(n - 1, 1)
    )

    # Expected max Sharpe under null (Bailey & Lopez de Prado approximation)
    gamma = 0.5772156649  # Euler-Mascheroni
    if n_trials <= 1:
        e_max_sr = 0.0
    else:
        z1 = sp_stats.norm.ppf(1.0 - 1.0 / max(n_trials, 2))
        z2 = sp_stats.norm.ppf(1.0 - 1.0 / (max(n_trials, 2) * math.e))
        e_max_sr = float((1 - gamma) * z1 + gamma * z2)

    if se_sr < 1e-12:
        dsr_val = 0.0
    else:
        dsr_val = float(sp_stats.norm.cdf((sr_obs - e_max_sr) / se_sr))

    return {
        "dsr": round(dsr_val, 6),
        "sharpe_observed": round(sr_obs, 4),
        "sharpe_expected_max_null": round(e_max_sr, 4),
        "se_sharpe": round(se_sr, 4),
        "skewness": round(skew, 4),
        "excess_kurtosis": round(kurt, 4),
        "n_trials": n_trials,
        "n_observations": n,
        "passes": dsr_val > 0.95,
    }


# ── Probability of Backtest Overfitting (CSCV) ───────────────────────


def compute_pbo(
    returns_matrix: np.ndarray,
    *,
    n_partitions: int = 8,
    n_combos: int = 100,
    rng_seed: int = 42,
) -> dict[str, Any]:
    """Estimate Probability of Backtest Overfitting via CSCV.

    *returns_matrix* has shape (n_bars, n_strategies) — each column is a
    candidate strategy's per-bar returns.  Typically built from the same
    pattern evaluated with different parameter sets or exit configs.

    For a single-strategy evaluation, pass a (n_bars, 1) matrix and the
    result degenerates to a stability check (PBO will be ~0.5 — neutral).

    Returns PBO in [0, 1]: probability that the IS-selected best strategy
    underperforms the median strategy OOS.
    """
    n_bars, n_strats = returns_matrix.shape
    if n_strats < 2 or n_bars < 2 * n_partitions:
        return {"pbo": None, "reason": "insufficient_strategies_or_bars"}

    rng = np.random.default_rng(rng_seed)
    n_per_partition = n_bars // n_partitions
    partition_indices = list(range(n_partitions))

    logit_ranks: list[float] = []
    half = n_partitions // 2

    for _ in range(n_combos):
        rng.shuffle(partition_indices)
        is_parts = partition_indices[:half]
        oos_parts = partition_indices[half:]

        is_mask = np.zeros(n_bars, dtype=bool)
        oos_mask = np.zeros(n_bars, dtype=bool)
        for p in is_parts:
            start = p * n_per_partition
            end = start + n_per_partition
            is_mask[start:end] = True
        for p in oos_parts:
            start = p * n_per_partition
            end = start + n_per_partition
            oos_mask[start:end] = True

        is_perf = returns_matrix[is_mask].sum(axis=0)
        oos_perf = returns_matrix[oos_mask].sum(axis=0)

        best_is_idx = int(np.argmax(is_perf))
        oos_rank = float(sp_stats.rankdata(oos_perf)[best_is_idx])
        # Normalize rank to [0, 1]
        normalized_rank = oos_rank / n_strats

        # Logit of rank (clamp away from 0/1)
        clamped = max(0.01, min(0.99, normalized_rank))
        logit_ranks.append(math.log(clamped / (1.0 - clamped)))

    pbo = float(np.mean([1.0 if lr < 0 else 0.0 for lr in logit_ranks]))
    mean_logit = float(np.mean(logit_ranks))

    return {
        "pbo": round(pbo, 4),
        "mean_logit_rank": round(mean_logit, 4),
        "n_combos": n_combos,
        "n_partitions": n_partitions,
        "n_strategies": n_strats,
        "n_bars": n_bars,
        "passes": pbo < 0.50,
    }


# ── Temporal holdout: split mining data from validation data ──────────


def temporal_holdout_split(
    rows: list[dict[str, Any]],
    *,
    holdout_fraction: float = 0.25,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split rows chronologically into discovery (early) and validation (late).

    The discovery set is used for filter evaluation / mining.
    The validation set is a locked-out forward period for promotion gates.
    """
    if not rows:
        return [], []
    ordered = sorted(rows, key=_row_time_key)
    split_idx = max(1, int(len(ordered) * (1.0 - holdout_fraction)))
    return ordered[:split_idx], ordered[split_idx:]


def validate_on_holdout(
    holdout_rows: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
    *,
    min_samples: int = 10,
    min_win_rate: float = 0.50,
    return_key: str = "ret_5d",
) -> tuple[bool, dict[str, Any]]:
    """Run promotion-style checks on the locked-out holdout set only.

    This ensures the validation data was never seen during discovery/mining.
    """
    filtered = [r for r in holdout_rows if predicate(r)]
    detail: dict[str, Any] = {"holdout_n": len(filtered), "holdout_total": len(holdout_rows)}

    if len(filtered) < min_samples:
        detail["blocked"] = "insufficient_holdout_samples"
        return False, detail

    rets = [float(r.get(return_key) or 0) for r in filtered]
    wins = sum(1 for r in rets if r > 0)
    wr = wins / len(rets) if rets else 0.0
    avg_ret = sum(rets) / len(rets) if rets else 0.0

    detail["holdout_win_rate"] = round(wr * 100, 2)
    detail["holdout_avg_return"] = round(avg_ret, 4)
    detail["holdout_wins"] = wins
    detail["holdout_losses"] = len(rets) - wins

    passes = wr >= min_win_rate and avg_ret > 0
    detail["passes"] = passes
    return passes, detail


# ── Enhanced promotion gate with DSR + trial tracking ─────────────────


def check_promotion_ready_v2(
    filtered: list[dict[str, Any]],
    *,
    min_trades: int = MIN_TRADES_FOR_PROMOTION,
    n_hypotheses_tested: int | None = None,
    returns_for_dsr: list[float] | None = None,
    dsr_threshold: float = 0.95,
    holdout_rows: list[dict[str, Any]] | None = None,
    holdout_predicate: Callable[[dict[str, Any]], bool] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """V2 promotion gate: ensemble + DSR + temporal holdout.

    Falls through to v1 logic when DSR data or holdout is unavailable,
    but records the gap so operators can see what was skipped.
    """
    if n_hypotheses_tested is None:
        n_hypotheses_tested = max(get_trial_count(), 1)

    ok_base, detail = _promotion_min_ensemble_hypothesis(
        filtered,
        min_trades=min_trades,
        n_hypotheses_tested=n_hypotheses_tested,
    )
    if not ok_base:
        return False, detail

    # DSR gate (hard when data available)
    if returns_for_dsr and len(returns_for_dsr) >= 10:
        dsr_result = compute_deflated_sharpe_ratio(
            returns_for_dsr,
            n_trials=max(n_hypotheses_tested, 1),
        )
        detail["deflated_sharpe"] = dsr_result
        if dsr_result.get("dsr") is not None and dsr_result["dsr"] < dsr_threshold:
            detail["blocked"] = "dsr_below_threshold"
            return False, detail
    else:
        detail["deflated_sharpe"] = {"skipped": True, "reason": "no_returns_series"}

    # Temporal holdout gate (hard when holdout provided)
    if holdout_rows is not None and holdout_predicate is not None:
        holdout_ok, holdout_detail = validate_on_holdout(
            holdout_rows,
            holdout_predicate,
            min_samples=max(5, min_trades // 3),
        )
        detail["temporal_holdout"] = holdout_detail
        if not holdout_ok:
            detail["blocked"] = "temporal_holdout_failed"
            return False, detail
    else:
        detail["temporal_holdout"] = {"skipped": True, "reason": "no_holdout_provided"}

    detail["promotion_version"] = "v2"
    return _finalize_cpcv_promotion_ready(
        detail,
        filtered,
        n_hypotheses_tested=n_hypotheses_tested,
    )
