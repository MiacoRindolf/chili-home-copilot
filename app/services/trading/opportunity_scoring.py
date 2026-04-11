"""Single source of truth for imminent alerts and opportunity board scoring.

Invariant: composite and coverage *math* lives here only. Callers differ by thresholds,
eligibility filters (lifecycle, session), and tier assignment — not duplicate formulas.
"""
from __future__ import annotations

import json
import math
from typing import Any

from ...config import settings
from ...models.trading import ScanPattern
from .pattern_engine import _condition_has_data, _eval_condition
from .pattern_ml import compute_condition_strength


def condition_field_keys(cond: dict[str, Any]) -> set[str]:
    """Indicators/refs a single rule clause needs for evaluability."""
    keys: set[str] = set()
    ind = cond.get("indicator")
    if ind:
        keys.add(str(ind))
    ref = cond.get("ref")
    if ref:
        keys.add(str(ref))
    return keys


def pattern_rule_field_keys(conditions: list[dict[str, Any]]) -> set[str]:
    """Union of all field keys referenced by pattern conditions."""
    out: set[str] = set()
    for c in conditions:
        out |= condition_field_keys(c)
    return out


def feature_coverage_detail(
    conditions: list[dict[str, Any]],
    flat: dict[str, Any],
) -> tuple[list[dict[str, Any]], int, int, float, list[str]]:
    """Return (evaluable_conds, n_eval, n_total, ratio, missing_indicators).

    *missing_indicators* lists condition ``indicator`` keys with no data in *flat*
    (first occurrence order).
    """
    if not conditions:
        return [], 0, 0, 0.0, []

    evaluable = [c for c in conditions if _condition_has_data(c, flat)]
    n_total = len(conditions)
    n_eval = len(evaluable)
    ratio = n_eval / n_total if n_total else 0.0

    missing: list[str] = []
    seen_m: set[str] = set()
    for c in conditions:
        if _condition_has_data(c, flat):
            continue
        ik = c.get("indicator")
        if ik and ik not in seen_m:
            seen_m.add(str(ik))
            missing.append(str(ik))

    return evaluable, n_eval, n_total, ratio, missing


def readiness_from_evaluable(
    evaluable: list[dict[str, Any]],
    flat: dict[str, Any],
) -> tuple[float, bool]:
    """Mean condition strength and whether all evaluable pass strictly."""
    if not evaluable:
        return 0.0, False
    strengths = [compute_condition_strength(c, flat) for c in evaluable]
    readiness = sum(strengths) / len(strengths)
    all_pass = all(_eval_condition(c, flat) for c in evaluable)
    return readiness, all_pass


def evaluate_readiness_with_gates(
    conditions: list[dict[str, Any]],
    flat: dict[str, Any],
    *,
    min_coverage_ratio: float,
    min_evaluable_absolute: int,
    allow_shortcut_two_evaluable: bool,
) -> tuple[float | None, bool, float, list[str]]:
    """Return (readiness|None, all_pass, coverage_ratio, missing_indicators).

    If coverage is insufficient and shortcut disabled, return (None, …).
    Shortcut: if ``allow_shortcut_two_evaluable`` and at least 2 evaluable clauses,
    proceed even when ratio < min_coverage_ratio (legacy imminent behavior).
    """
    if not conditions:
        return None, False, 0.0, []

    _ev, n_eval, _n_tot, ratio, missing = feature_coverage_detail(conditions, flat)
    if n_eval == 0:
        return None, False, ratio, missing

    ok_coverage = ratio >= min_coverage_ratio
    ok_shortcut = bool(allow_shortcut_two_evaluable and n_eval >= min_evaluable_absolute)
    if not ok_coverage and not ok_shortcut:
        return None, False, ratio, missing

    readiness, all_pass = readiness_from_evaluable(_ev, flat)
    return readiness, all_pass, ratio, missing


def pattern_quality_score(pat: ScanPattern) -> float:
    """Map stored pattern stats to ~0..1 (deterministic, no ML)."""
    wr = pat.win_rate
    if wr is not None and wr > 1:
        wr = wr / 100.0
    wr = float(wr) if wr is not None else 0.0
    wr_c = max(0.0, min(1.0, wr))

    ev = int(pat.evidence_count or 0)
    ev_c = min(1.0, math.log1p(ev) / math.log1p(50))

    bt = int(pat.backtest_count or 0)
    bt_c = min(1.0, math.log1p(bt) / math.log1p(30))

    oos_wr = pat.oos_win_rate
    if oos_wr is not None and oos_wr > 1:
        oos_wr = oos_wr / 100.0
    oos_c = max(0.0, min(1.0, float(oos_wr))) if oos_wr is not None else 0.35

    conf = float(pat.confidence or 0.0)
    conf_c = max(0.0, min(1.0, conf))

    life = (getattr(pat, "lifecycle_stage", None) or "candidate").strip().lower()
    promo = (getattr(pat, "promotion_status", None) or "").strip().lower()
    tier_boost = 0.0
    if life in ("live", "promoted"):
        tier_boost = 0.25
    elif promo == "promoted":
        tier_boost = 0.2
    elif life == "validated":
        tier_boost = 0.1
    elif life == "challenged":
        tier_boost = 0.05
    elif life == "backtested":
        tier_boost = 0.08

    # Weighted blend
    raw = (
        0.22 * wr_c
        + 0.18 * ev_c
        + 0.15 * bt_c
        + 0.2 * oos_c
        + 0.15 * conf_c
        + tier_boost
    )
    return max(0.0, min(1.0, raw))


def risk_reward_score(
    entry: float | None,
    stop: float | None,
    target: float | None,
    *,
    side_long: bool = True,
) -> float:
    """0..1 sanity: reward/risk from levels; 0.5 if unknown."""
    try:
        e = float(entry or 0)
        s = float(stop or 0)
        t = float(target or 0)
    except (TypeError, ValueError):
        return 0.5
    if e <= 0 or s <= 0 or t <= 0:
        return 0.5
    if side_long:
        risk = e - s
        reward = t - e
    else:
        risk = s - e
        reward = e - t
    if risk <= 0 or reward <= 0:
        return 0.2
    rr = reward / risk
    return max(0.0, min(1.0, rr / 4.0))


def overextension_penalty(flat: dict[str, Any]) -> float:
    """Subtract up to ~0.12 from composite when RSI stretched (long bias)."""
    rsi = flat.get("rsi_14")
    try:
        r = float(rsi) if rsi is not None else 50.0
    except (TypeError, ValueError):
        return 0.0
    if r >= 78:
        return 0.12
    if r >= 72:
        return 0.07
    if r <= 22:
        return 0.05
    return 0.0


def eta_timeliness_score(eta_hi_hours: float, max_eta_hours: float) -> float:
    """Slight preference for sooner setups; secondary to quality. 0..~0.15."""
    if max_eta_hours <= 0:
        return 0.0
    t = max(0.0, min(1.0, 1.0 - (eta_hi_hours / max_eta_hours)))
    return 0.15 * t


def compute_composite_score(
    *,
    readiness: float,
    coverage_ratio: float,
    pattern_quality: float,
    rr_score: float,
    eta_score: float,
    overext_subtract: float,
) -> tuple[float, dict[str, float]]:
    """Weighted composite 0..~1 + breakdown (deterministic)."""
    wr = float(getattr(settings, "opportunity_weight_readiness", 0.28))
    wc = float(getattr(settings, "opportunity_weight_coverage", 0.22))
    wp = float(getattr(settings, "opportunity_weight_pattern_quality", 0.22))
    wrr = float(getattr(settings, "opportunity_weight_risk_reward", 0.13))
    we = float(getattr(settings, "opportunity_weight_eta", 0.15))

    eta_norm = max(0.0, min(1.0, eta_score / 0.15)) if eta_score else 0.0
    parts = {
        "readiness": wr * max(0.0, min(1.0, readiness)),
        "coverage": wc * max(0.0, min(1.0, coverage_ratio)),
        "pattern_quality": wp * max(0.0, min(1.0, pattern_quality)),
        "risk_reward": wrr * max(0.0, min(1.0, rr_score)),
        "eta": we * eta_norm,
    }
    total = sum(parts.values()) - overext_subtract
    total = max(0.0, min(1.0, total))
    parts["overextension_penalty"] = overext_subtract
    parts["composite"] = total
    return total, parts


def scan_pattern_eligible_main_imminent(pat: ScanPattern) -> bool:
    """Promoted / live quality for Telegram main channel (legacy promotion_status OR)."""
    life = (getattr(pat, "lifecycle_stage", None) or "").strip().lower()
    promo = (getattr(pat, "promotion_status", None) or "").strip().lower()
    if life in ("promoted", "live"):
        return True
    if promo == "promoted":
        return True
    return False


def scan_pattern_eligible_research(pat: ScanPattern) -> bool:
    """Broader: backtested / candidate for Tier D / near-miss."""
    life = (getattr(pat, "lifecycle_stage", None) or "").strip().lower()
    if life in ("candidate", "backtested", "validated", "challenged", "promoted", "live"):
        return pat.active
    return pat.active


def parse_pattern_conditions(rules_json: Any) -> list[dict[str, Any]]:
    if isinstance(rules_json, dict):
        data = rules_json
    else:
        try:
            data = json.loads(rules_json or "{}")
        except (json.JSONDecodeError, TypeError):
            return []
    conds = data.get("conditions") or []
    return conds if isinstance(conds, list) else []
