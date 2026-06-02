"""Shared fallback exit-configuration defaults for pattern exits.

Backtests and live/paper management both need an exit horizon when a
``ScanPattern`` has not learned an explicit ``exit_config`` yet. Keeping the
classifier here prevents the two paths from drifting into different holding
policies for the same pattern.
"""
from __future__ import annotations

import json
from typing import Any

EXIT_PARAMS_BY_TIMEFRAME: dict[str, dict[str, tuple[float, int, bool]]] = {
    "1m": {
        "breakout": (1.0, 120, False),  # ~2 hours of 1m bars
        "mean_rev": (0.5, 30, True),    # ~30 minutes
        "default": (0.8, 60, True),     # ~1 hour
    },
    "5m": {
        "breakout": (1.5, 78, False),
        "mean_rev": (0.8, 24, True),
        "default": (1.2, 48, True),
    },
    "15m": {
        "breakout": (2.0, 26, False),
        "mean_rev": (1.0, 8, True),
        "default": (1.5, 16, True),
    },
    "1h": {
        "breakout": (2.5, 48, False),
        "mean_rev": (1.2, 8, True),
        "default": (1.8, 24, True),
    },
    "4h": {
        "breakout": (2.8, 30, False),
        "mean_rev": (1.3, 10, True),
        "default": (2.0, 18, True),
    },
    "1d": {
        "breakout": (3.0, 50, False),
        "mean_rev": (1.5, 15, True),
        "default": (2.0, 25, True),
    },
}

BREAKOUT_INDICATORS = {
    "resistance_retests",
    "bb_squeeze",
    "bb_squeeze_firing",
    "narrow_range",
    "vcp_count",
    "dist_to_resistance_pct",
    "retest_range_tightening",
    "resistance",
}

MEAN_REV_INDICATORS = {
    "vwap_reclaim",
    "ibs",
    "pullback_stretch_entry",
}


def normalize_conditions(rules_json: Any) -> list[dict[str, Any]]:
    """Extract a clean ``conditions`` list from a rules-json-like object."""
    raw_conditions: Any
    if isinstance(rules_json, str) and rules_json.strip():
        try:
            rules_json = json.loads(rules_json)
        except (TypeError, json.JSONDecodeError):
            rules_json = {}
    if isinstance(rules_json, dict):
        raw_conditions = rules_json.get("conditions", [])
    elif isinstance(rules_json, list):
        raw_conditions = rules_json
    else:
        raw_conditions = []
    if isinstance(raw_conditions, dict):
        raw_conditions = [raw_conditions]
    if not isinstance(raw_conditions, list):
        return []
    return [dict(c) for c in raw_conditions if isinstance(c, dict) and c]


def classify_exit_params(
    conditions: list[dict[str, Any]],
    timeframe: str = "1d",
) -> tuple[float, int, bool]:
    """Infer ``(atr_mult, max_bars, use_bos)`` from conditions and timeframe."""
    breakout_score = 0
    mean_rev_score = 0

    for cond in conditions:
        ind = cond.get("indicator", "")
        op = cond.get("op", "")
        value = cond.get("value")

        if ind in BREAKOUT_INDICATORS:
            breakout_score += 2
        if ind in MEAN_REV_INDICATORS:
            mean_rev_score += 2

        if ind == "rsi_14":
            try:
                v = float(value) if value is not None else 0.0
            except (TypeError, ValueError):
                v = 0.0
            if op in (">", ">=") and v >= 60:
                breakout_score += 1
            elif op in ("<", "<=") and v <= 40:
                mean_rev_score += 1

    tf_params = EXIT_PARAMS_BY_TIMEFRAME.get(
        str(timeframe or "1d"),
        EXIT_PARAMS_BY_TIMEFRAME["1d"],
    )
    if breakout_score > mean_rev_score:
        return tf_params["breakout"]
    if mean_rev_score > breakout_score:
        return tf_params["mean_rev"]
    return tf_params["default"]


def infer_exit_config_defaults(
    rules_json: Any,
    timeframe: str | None,
) -> dict[str, Any] | None:
    """Return classifier-backed defaults, or ``None`` without usable rules."""
    conditions = normalize_conditions(rules_json)
    if not conditions:
        return None
    atr_mult, max_bars, use_bos = classify_exit_params(
        conditions,
        timeframe=str(timeframe or "1d"),
    )
    return {
        "atr_mult": float(atr_mult),
        "max_bars": int(max_bars),
        "use_bos": bool(use_bos),
        "exit_defaults_source": "backtest_classifier",
    }


__all__ = [
    "BREAKOUT_INDICATORS",
    "EXIT_PARAMS_BY_TIMEFRAME",
    "MEAN_REV_INDICATORS",
    "classify_exit_params",
    "infer_exit_config_defaults",
    "normalize_conditions",
]
