"""Signal explainability — feature contributions per signal and importance drift tracking.

For ML-based signals: uses feature importance from the meta-learner.
For rule-based patterns: computes condition-by-condition pass/fail with margin-to-threshold.
Tracks feature importance drift across retraining cycles.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import ScanPattern

logger = logging.getLogger(__name__)


def explain_signal(
    db: Session,
    ticker: str,
    scan_pattern_id: int | None = None,
    indicator_values: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Generate a per-signal explanation of which indicators contributed."""
    from .pattern_ml import get_meta_learner

    explanation: dict[str, Any] = {
        "ticker": ticker,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "contributions": [],
    }

    if scan_pattern_id:
        pat = db.query(ScanPattern).filter(ScanPattern.id == scan_pattern_id).first()
        if pat:
            explanation["pattern_name"] = pat.name
            rule_contribs = _explain_rule_based(pat, indicator_values or {})
            explanation["contributions"] = rule_contribs
            explanation["method"] = "rule_based"

    ml = get_meta_learner()
    if ml.is_ready() and indicator_values:
        ml_contribs = _explain_ml_based(ml, indicator_values)
        if ml_contribs:
            explanation["ml_contributions"] = ml_contribs
            if not explanation["contributions"]:
                explanation["contributions"] = ml_contribs
                explanation["method"] = "ml_importance"

    if explanation["contributions"]:
        explanation["contributions"].sort(key=lambda c: abs(c.get("strength", 0)), reverse=True)
        explanation["summary"] = _make_natural_language_summary(explanation["contributions"])

    return explanation


def _explain_rule_based(
    pattern: ScanPattern,
    indicators: dict[str, float],
) -> list[dict[str, Any]]:
    """For each condition in a rule-based pattern, compute pass/fail + margin."""
    try:
        rj = json.loads(pattern.rules_json) if isinstance(pattern.rules_json, str) else (pattern.rules_json or {})
        conditions = rj.get("conditions", [])
    except Exception:
        return []

    contribs = []
    for cond in conditions:
        ind_name = cond.get("indicator", "")
        op = cond.get("op", "")
        threshold = cond.get("value")
        ref = cond.get("ref")

        actual = indicators.get(ind_name)
        if actual is None:
            contribs.append({
                "indicator": ind_name,
                "op": op,
                "threshold": threshold or ref,
                "status": "missing",
                "strength": 0,
            })
            continue

        compare_to = threshold
        if ref:
            compare_to = indicators.get(ref)
            if compare_to is None:
                contribs.append({
                    "indicator": ind_name,
                    "op": op,
                    "threshold": ref,
                    "status": "ref_missing",
                    "strength": 0,
                })
                continue

        passed = _eval_simple_condition(actual, op, compare_to)

        margin = 0
        if compare_to is not None and compare_to != 0:
            margin = (actual - compare_to) / abs(compare_to)

        strength_label = "strong" if abs(margin) > 0.3 else ("moderate" if abs(margin) > 0.1 else "weak")

        contribs.append({
            "indicator": ind_name,
            "actual": round(actual, 4),
            "op": op,
            "threshold": round(compare_to, 4) if compare_to is not None else None,
            "passed": passed,
            "margin": round(margin, 4),
            "strength": round(abs(margin), 4),
            "strength_label": strength_label,
        })

    return contribs


def _explain_ml_based(ml, indicators: dict[str, float]) -> list[dict[str, Any]]:
    """Use feature importances from the meta-learner to explain a signal."""
    stats = ml.get_stats()
    importances = stats.get("feature_importances", {})
    if not importances:
        return []

    contribs = []
    for fname, imp in importances.items():
        if imp < 0.01:
            continue
        actual = indicators.get(fname)
        strength_label = "strong" if imp > 0.05 else ("moderate" if imp > 0.02 else "weak")
        contribs.append({
            "indicator": fname,
            "actual": round(actual, 4) if actual is not None else None,
            "importance": round(imp, 4),
            "strength": round(imp, 4),
            "strength_label": strength_label,
        })

    contribs.sort(key=lambda c: c["importance"], reverse=True)
    return contribs[:10]


def _eval_simple_condition(actual: float, op: str, threshold: float) -> bool:
    """Quick condition evaluation for explainability."""
    if op == ">":
        return actual > threshold
    if op == ">=":
        return actual >= threshold
    if op == "<":
        return actual < threshold
    if op == "<=":
        return actual <= threshold
    if op == "==":
        return actual == threshold
    if op == "!=":
        return actual != threshold
    return False


def _make_natural_language_summary(contributions: list[dict[str, Any]]) -> str:
    """Generate a human-readable summary of signal contributions."""
    parts = []
    for c in contributions[:3]:
        ind = c.get("indicator", "")
        label = c.get("strength_label", "")
        passed = c.get("passed")
        if passed is not None:
            status = "passed" if passed else "failed"
            parts.append(f"{ind} ({label}, {status})")
        else:
            parts.append(f"{ind} ({label})")

    if not parts:
        return "No significant contributors identified."

    return "Signal driven by: " + ", ".join(parts)


def track_importance_drift(db: Session) -> dict[str, Any]:
    """Compare current feature importances with historical to detect drift."""
    from .pattern_ml import get_meta_learner
    from .experiment_tracker import query_experiments

    ml = get_meta_learner()
    current = ml.get_stats().get("feature_importances", {})
    if not current:
        return {"ok": False, "reason": "no_current_importances"}

    cycles = query_experiments(limit=5)
    historical_imps: dict[str, list[float]] = {}
    for c in cycles:
        ml_metrics = c.get("results", {}).get("ml_metrics", {})
        imps = ml_metrics.get("feature_importances", {})
        for fname, imp in imps.items():
            historical_imps.setdefault(fname, []).append(float(imp))

    if not historical_imps:
        return {"ok": True, "reason": "no_historical_data", "drift": []}

    drift_report = []
    for fname, current_imp in current.items():
        hist = historical_imps.get(fname, [])
        if len(hist) < 2:
            continue
        hist_mean = sum(hist) / len(hist)
        hist_std = math.sqrt(sum((h - hist_mean) ** 2 for h in hist) / max(len(hist) - 1, 1))
        if hist_std > 0:
            z = (current_imp - hist_mean) / hist_std
            if abs(z) > 2.0:
                drift_report.append({
                    "feature": fname,
                    "current": round(current_imp, 4),
                    "historical_mean": round(hist_mean, 4),
                    "z_score": round(z, 2),
                    "direction": "increased" if z > 0 else "decreased",
                })

    drift_report.sort(key=lambda d: abs(d["z_score"]), reverse=True)

    return {
        "ok": True,
        "features_checked": len(current),
        "features_drifted": len(drift_report),
        "drift": drift_report,
    }
