"""Regime-conditional capital allocator.

Shifts capital allocation across pattern families based on market regime.
Uses regime_affinity_json from ScanPattern and current regime from regime.py.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import ScanPattern

logger = logging.getLogger(__name__)

REGIME_WEIGHTS = {
    "risk_on": {
        "momentum": 1.2,
        "breakout": 1.3,
        "mean_reversion": 0.6,
        "trend_following": 1.1,
        "default": 0.8,
    },
    "cautious": {
        "momentum": 0.8,
        "breakout": 0.7,
        "mean_reversion": 1.0,
        "trend_following": 0.9,
        "default": 0.7,
    },
    "risk_off": {
        "momentum": 0.4,
        "breakout": 0.3,
        "mean_reversion": 1.3,
        "trend_following": 0.5,
        "default": 0.5,
    },
}

MAX_REGIME_CAPITAL_FRACTION = {
    "risk_on": 0.85,
    "cautious": 0.60,
    "risk_off": 0.35,
}


def _classify_pattern_style(pattern: ScanPattern) -> str:
    """Classify a pattern into a style bucket from its conditions."""
    try:
        rj = json.loads(pattern.rules_json) if isinstance(pattern.rules_json, str) else (pattern.rules_json or {})
        conditions = rj.get("conditions", [])
    except Exception:
        conditions = []

    indicators = {c.get("indicator", "") for c in conditions}
    name_lower = (pattern.name or "").lower()

    if "volume_ratio" in indicators and any(k in name_lower for k in ("breakout", "gap", "volume spike")):
        return "breakout"
    if any(k in name_lower for k in ("momentum", "macd positive", "ema stack")):
        return "momentum"
    if any(k in name_lower for k in ("oversold", "reversal", "bounce", "mean")):
        return "mean_reversion"
    if "adx" in indicators and any(k in name_lower for k in ("trend", "strong")):
        return "trend_following"

    return "default"


def compute_regime_allocations(
    db: Session,
    capital: float = 100_000.0,
) -> dict[str, Any]:
    """Compute capital allocation weights per pattern, adjusted for regime."""
    from .market_data import get_market_regime

    regime = get_market_regime()
    composite = regime.get("composite", "cautious")
    regime_weight_map = REGIME_WEIGHTS.get(composite, REGIME_WEIGHTS["cautious"])
    max_capital = capital * MAX_REGIME_CAPITAL_FRACTION.get(composite, 0.6)

    active_patterns = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.lifecycle_stage.in_(("promoted", "live")),
        )
        .all()
    )

    if not active_patterns:
        return {
            "ok": True,
            "regime": composite,
            "allocations": [],
            "total_deployed": 0,
            "max_capital": max_capital,
        }

    allocations = []
    raw_weights = []
    for pat in active_patterns:
        style = _classify_pattern_style(pat)

        affinity = {}
        if pat.regime_affinity_json:
            try:
                affinity = (
                    pat.regime_affinity_json
                    if isinstance(pat.regime_affinity_json, dict)
                    else json.loads(pat.regime_affinity_json)
                )
            except Exception:
                pass

        custom_mult = affinity.get(composite, None)
        base_weight = regime_weight_map.get(style, regime_weight_map["default"])
        if custom_mult is not None:
            weight = float(custom_mult)
        else:
            weight = base_weight

        confidence = float(pat.confidence or 0.5)
        oos_wr = float(pat.oos_win_rate or pat.win_rate or 0.5)

        score = weight * confidence * oos_wr
        raw_weights.append(score)

        allocations.append({
            "pattern_id": pat.id,
            "pattern_name": pat.name,
            "style": style,
            "regime_weight": round(weight, 3),
            "confidence": round(confidence, 3),
            "score": round(score, 4),
            "capital": 0.0,
        })

    total_score = sum(raw_weights)
    if total_score > 0:
        for i, alloc in enumerate(allocations):
            fraction = raw_weights[i] / total_score
            alloc["capital"] = round(max_capital * fraction, 2)
            alloc["fraction_pct"] = round(fraction * 100, 1)

    allocations.sort(key=lambda a: a["score"], reverse=True)

    logger.info(
        "[regime_allocator] Regime=%s, patterns=%d, max_capital=%.0f",
        composite, len(allocations), max_capital,
    )

    return {
        "ok": True,
        "regime": composite,
        "regime_weights": regime_weight_map,
        "max_capital_fraction": MAX_REGIME_CAPITAL_FRACTION.get(composite, 0.6),
        "max_capital": round(max_capital, 2),
        "total_deployed": round(sum(a["capital"] for a in allocations), 2),
        "allocations": allocations,
    }
