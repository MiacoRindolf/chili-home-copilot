"""Purged / segmented validation for mined pattern candidates (lightweight CPCV-style).

Includes:
- Time-segmented validation (purged CPCV lite)
- Bootstrap resampling for confidence intervals
- Ensemble confirmation (2-of-3 methods must agree for promotion)
- Multiple hypothesis correction (Bonferroni-style)
"""
from __future__ import annotations

import logging
import random
from datetime import datetime
from typing import Any, Callable

logger = logging.getLogger(__name__)

MIN_TRADES_FOR_PROMOTION = 30


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

    detail["ready"] = True
    return True, detail
