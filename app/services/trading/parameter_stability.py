"""Parameter stability / plateau scoring for repeatable-edge ScanPatterns (v1).

Uses a **small local neighborhood** of rule JSON variants and cheap backtests on a
deterministic, ticker-order-agnostic subset of already-successful evaluation tickers.

Honest limitations are first-class in the persisted contract (``approximation_note``,
``skip_reason``, counts).
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

STABILITY_VERSION = 1

APPROXIMATION_NOTE = (
    "CHILI v1 stability: local numeric neighborhood only; partial ticker subset; "
    "not a full parameter grid or walk-forward robustness proof."
)

NUMERIC_OPS = frozenset({">", "<", ">=", "<=", "==", "!="})


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def pick_stability_tickers(
    evaluated_upper: list[str],
    *,
    k: int,
    seed: int,
) -> tuple[list[str], str]:
    """Deterministic subset: sort alphabetically, then hash-guided picks (not first-k)."""
    s = sorted({(t or "").strip().upper() for t in evaluated_upper if t})
    n = len(s)
    if n == 0:
        return [], "no_evaluated_tickers"
    take = min(max(1, k), n)
    if n <= take:
        return s, "full_evaluated_set"
    h = hashlib.sha256(f"stab_tick|{seed}|{','.join(s)}".encode()).digest()
    picks: list[str] = []
    used_idx: set[int] = set()
    pos = 0
    while len(picks) < take and pos < 512:
        b = h[pos % len(h)]
        idx = (b + pos * 31 + seed * 17) % n
        if idx not in used_idx:
            used_idx.add(idx)
            picks.append(s[idx])
        pos += 1
    # fill deterministically if collisions exhausted
    for i in range(n):
        if len(picks) >= take:
            break
        if i not in used_idx:
            used_idx.add(i)
            picks.append(s[i])
    return picks[:take], "hash_subset_sorted_universe"


def discover_tuned_axes(conditions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return axis descriptors: path, base value, step, min_v, max_v."""
    axes: list[dict[str, Any]] = []
    for i, cond in enumerate(conditions):
        if not isinstance(cond, dict):
            continue
        op = (cond.get("op") or "").strip()
        ind = (cond.get("indicator") or "").strip()
        if op in NUMERIC_OPS and "value" in cond:
            v = cond.get("value")
            if isinstance(v, bool):
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if math.isnan(fv) or math.isinf(fv):
                continue
            step = 1.0 if abs(fv) > 25 or float(fv).is_integer() else 0.5
            lo, hi = (-1e9, 1e9)
            if "rsi" in ind.lower():
                lo, hi = 0.0, 100.0
            axes.append(
                {
                    "path": f"conditions[{i}].value",
                    "kind": "value",
                    "index": i,
                    "base": fv,
                    "step": step,
                    "min_v": lo,
                    "max_v": hi,
                }
            )
        params = cond.get("params")
        if isinstance(params, dict):
            for pk in ("lookback", "tolerance_pct"):
                if pk not in params:
                    continue
                pv = params.get(pk)
                try:
                    iv = int(float(pv))
                except (TypeError, ValueError):
                    continue
                if iv < 1 or iv > 500:
                    continue
                axes.append(
                    {
                        "path": f"conditions[{i}].params.{pk}",
                        "kind": "params",
                        "index": i,
                        "param_key": pk,
                        "base": float(iv),
                        "step": 1.0,
                        "min_v": 1.0,
                        "max_v": 250.0,
                    }
                )
    return axes[:3]


def _apply_axis(rules: dict[str, Any], axis: dict[str, Any], new_val: float) -> bool:
    conds = rules.get("conditions")
    if not isinstance(conds, list):
        return False
    idx = int(axis["index"])
    if idx < 0 or idx >= len(conds):
        return False
    c = conds[idx]
    if not isinstance(c, dict):
        return False
    if axis["kind"] == "value":
        c["value"] = new_val
        return True
    pk = axis.get("param_key")
    if axis["kind"] == "params" and pk:
        params = c.get("params")
        if not isinstance(params, dict):
            params = {}
            c["params"] = params
        if pk == "lookback":
            params[pk] = int(round(new_val))
        else:
            params[pk] = float(new_val)
        return True
    return False


def generate_neighbor_variants(
    rules_json: Any,
    axes: list[dict[str, Any]],
    *,
    max_variants: int,
) -> list[tuple[str, dict[str, Any]]]:
    """Return (label, rules_dict) variants excluding exact duplicate of baseline."""
    base_rules: dict[str, Any]
    if isinstance(rules_json, dict):
        base_rules = copy.deepcopy(rules_json)
    else:
        try:
            base_rules = json.loads(rules_json or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    if not isinstance(base_rules.get("conditions"), list):
        return []

    out: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    raw0 = json.dumps(base_rules, sort_keys=True, default=str)
    seen.add(hashlib.sha256(raw0.encode()).hexdigest()[:24])

    def _try_add(label: str, r: dict[str, Any]) -> None:
        nonlocal out
        if len(out) >= max_variants:
            return
        h = hashlib.sha256(json.dumps(r, sort_keys=True, default=str).encode()).hexdigest()[:24]
        if h in seen:
            return
        seen.add(h)
        out.append((label, r))

    for ax in axes:
        if len(out) >= max_variants:
            break
        base = float(ax["base"])
        step = float(ax["step"])
        mn = float(ax["min_v"])
        mx = float(ax["max_v"])
        for sign, lab in ((-1, "minus"), (1, "plus")):
            if len(out) >= max_variants:
                break
            nv = base + sign * step
            nv = max(mn, min(mx, nv))
            if abs(nv - base) < 1e-9:
                continue
            r = copy.deepcopy(base_rules)
            if _apply_axis(r, ax, nv):
                _try_add(f"{ax['path']}:{lab}", r)
    return out


def _score_from_result(result: dict[str, Any]) -> float | None:
    if not result.get("ok"):
        return None
    if result.get("oos_ok") and result.get("oos_win_rate") is not None:
        try:
            return float(result["oos_win_rate"])
        except (TypeError, ValueError):
            pass
    isl = result.get("in_sample")
    if isinstance(isl, dict) and isl.get("win_rate") is not None:
        try:
            return float(isl["win_rate"])
        except (TypeError, ValueError):
            pass
    try:
        return float(result.get("win_rate") or 0)
    except (TypeError, ValueError):
        return None


def compute_parameter_stability(
    *,
    pattern_name: str,
    rules_json: Any,
    stability_tickers: list[str],
    baseline_score: float | None,
    backtest_pattern_fn: Any,
    bt_params: dict[str, Any],
    bt_kw: dict[str, Any],
    exit_config: Any,
    scan_pattern_id: int,
    max_variant_evals: int,
    rel_pass_tol: float,
    abs_floor: float,
) -> dict[str, Any]:
    """Run neighbor backtests; return full ``parameter_stability`` contract."""
    conditions: list[dict[str, Any]] = []
    if isinstance(rules_json, dict):
        conditions = list(rules_json.get("conditions") or [])
    else:
        try:
            conditions = list(json.loads(rules_json or "{}").get("conditions") or [])
        except (json.JSONDecodeError, TypeError, ValueError):
            conditions = []
    if not conditions:
        return _empty_contract(
            skip_reason="no_conditions",
            fragility_flags=["no_conditions"],
        )
    tuned_axis_paths: list[str] = []
    skip_reason: str | None = None
    attempted = 0
    evaluated = 0
    neighbor_scores: list[float] = []
    pass_c = 0
    fail_c = 0
    fragility_flags: list[str] = []

    if not stability_tickers:
        return _empty_contract(
            skip_reason="no_stability_ticker_subset",
            fragility_flags=["no_tickers"],
        )

    axes = discover_tuned_axes(conditions)
    tuned_axis_paths = [a["path"] for a in axes]
    if not axes:
        return _empty_contract(
            skip_reason="no_tunable_numeric_params",
            fragility_flags=["no_tunable_axes"],
        )

    base_rules_dict: dict[str, Any]
    if isinstance(rules_json, dict):
        base_rules_dict = rules_json
    else:
        try:
            base_rules_dict = json.loads(rules_json or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            return _empty_contract(
                skip_reason="invalid_rules_json",
                fragility_flags=["invalid_rules"],
            )

    variants = generate_neighbor_variants(base_rules_dict, axes, max_variants=max_variant_evals)
    attempted = len(variants)
    if attempted == 0:
        return _empty_contract(
            skip_reason="no_neighbor_variants_generated",
            fragility_flags=["no_variants"],
            tuned_axis_paths=tuned_axis_paths,
        )

    for _label, vr in variants:
        sub_scores: list[float] = []
        rules_arg = vr
        for t in stability_tickers:
            try:
                res = backtest_pattern_fn(
                    ticker=t,
                    pattern_name=pattern_name,
                    rules_json=rules_arg,
                    interval=bt_params["interval"],
                    period=bt_params["period"],
                    exit_config=exit_config,
                    scan_pattern_id=scan_pattern_id,
                    **bt_kw,
                )
                sc = _score_from_result(res if isinstance(res, dict) else {})
                if sc is not None:
                    sub_scores.append(sc)
            except Exception as e:
                logger.debug("[parameter_stability] neighbor backtest failed: %s", e)
        if not sub_scores:
            fail_c += 1
            continue
        evaluated += 1
        m = sum(sub_scores) / len(sub_scores)
        neighbor_scores.append(m)
        if baseline_score is None:
            pass_c += 1
            continue
        thr = max(abs_floor, baseline_score * (1.0 - rel_pass_tol))
        if m >= thr - 1e-6:
            pass_c += 1
        else:
            fail_c += 1

    if evaluated == 0:
        skip_reason = skip_reason or "no_successful_neighbor_evals"
        fragility_flags.append("neighbor_eval_all_failed")

    peak = max(neighbor_scores) if neighbor_scores else None
    med = _median(neighbor_scores)
    gap = None
    if peak is not None and med is not None:
        gap = round(float(peak - med), 6)

    ff = list(fragility_flags)
    stability_score, stability_tier = _stability_score_tier(
        neighbor_scores=neighbor_scores,
        pass_c=pass_c,
        fail_c=fail_c,
        peak=peak,
        med=med,
        fragility_flags=ff,
    )

    return {
        "stability_version": STABILITY_VERSION,
        "stability_score": stability_score,
        "stability_tier": stability_tier,
        "neighborhood_size": int(attempted),
        "neighbor_pass_count": pass_c,
        "neighbor_fail_count": fail_c,
        "local_peak_score": round(peak, 4) if peak is not None else None,
        "local_median_score": round(med, 4) if med is not None else None,
        "peak_to_median_gap": gap,
        "fragility_flags": ff,
        "evaluated_at": _utc_iso(),
        "tuned_axis_paths": tuned_axis_paths,
        "score_basis": "mean_oos_win_rate_pct_per_neighbor_else_in_sample_or_headline_wr_pct",
        "attempted_neighbor_count": attempted,
        "evaluated_neighbor_count": evaluated,
        "skip_reason": skip_reason if skip_reason else None,
        "approximation_note": APPROXIMATION_NOTE,
    }


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    if len(s) % 2:
        return float(s[m])
    return float((s[m - 1] + s[m]) / 2)


def _empty_contract(
    *,
    skip_reason: str,
    fragility_flags: list[str],
    tuned_axis_paths: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "stability_version": STABILITY_VERSION,
        "stability_score": 0.0,
        "stability_tier": "n/a",
        "neighborhood_size": 0,
        "neighbor_pass_count": 0,
        "neighbor_fail_count": 0,
        "local_peak_score": None,
        "local_median_score": None,
        "peak_to_median_gap": None,
        "fragility_flags": fragility_flags,
        "evaluated_at": _utc_iso(),
        "tuned_axis_paths": tuned_axis_paths or [],
        "score_basis": "mean_oos_win_rate_pct_per_neighbor_else_in_sample_or_headline_wr_pct",
        "attempted_neighbor_count": 0,
        "evaluated_neighbor_count": 0,
        "skip_reason": skip_reason,
        "approximation_note": APPROXIMATION_NOTE,
    }


def _stability_score_tier(
    *,
    neighbor_scores: list[float],
    pass_c: int,
    fail_c: int,
    peak: float | None,
    med: float | None,
    fragility_flags: list[str],
) -> tuple[float, str]:
    n = len(neighbor_scores)
    if n == 0:
        return 0.0, "n/a"
    pass_ratio = pass_c / max(1, pass_c + fail_c)
    score = 0.55 * pass_ratio + 0.45 * min(1.0, n / 6.0)
    if peak is not None and med is not None and med > 1e-6:
        rel_spread = (peak - med) / max(med, 1.0)
        if rel_spread > 0.25:
            score *= 0.75
            fragility_flags.append("high_peak_to_median_spread")
    if pass_c < fail_c:
        fragility_flags.append("neighbor_fail_majority")
        score *= 0.65
    tier = "plateau"
    if score >= 0.62:
        tier = "plateau"
    elif score >= 0.38:
        tier = "mixed"
    else:
        tier = "fragile"
    return round(min(1.0, max(0.0, score)), 4), tier
