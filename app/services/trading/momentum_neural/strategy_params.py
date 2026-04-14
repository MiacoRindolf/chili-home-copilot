"""Stable momentum strategy params used by runners and neural refinement."""

from __future__ import annotations

import math
from typing import Any

from ....models.trading import MomentumAutomationOutcome

PARAM_SCHEMA_VERSION = 1

_PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "entry_viability_min": (0.35, 0.95),
    "entry_revalidate_floor": (0.20, 0.90),
    "bailout_viability_floor": (0.10, 0.80),
    "stop_atr_mult": (0.20, 3.50),
    "target_atr_mult": (0.20, 6.00),
    "trail_activate_return_bps": (5.0, 250.0),
    "trail_floor_return_bps": (1.0, 180.0),
    "max_hold_seconds": (60.0, 172800.0),
}

_DEFAULTS: dict[str, dict[str, float]] = {
    "default": {
        "entry_viability_min": 0.52,
        "entry_revalidate_floor": 0.48,
        "bailout_viability_floor": 0.38,
        "stop_atr_mult": 0.60,
        "target_atr_mult": 0.90,
        "trail_activate_return_bps": 50.0,
        "trail_floor_return_bps": 30.0,
        "max_hold_seconds": 86400.0,
    },
    "impulse_breakout": {
        "entry_viability_min": 0.56,
        "entry_revalidate_floor": 0.51,
        "bailout_viability_floor": 0.40,
        "stop_atr_mult": 0.65,
        "target_atr_mult": 1.10,
        "trail_activate_return_bps": 55.0,
        "trail_floor_return_bps": 28.0,
        "max_hold_seconds": 14400.0,
    },
    "micro_pullback_continuation": {
        "entry_viability_min": 0.54,
        "entry_revalidate_floor": 0.50,
        "bailout_viability_floor": 0.39,
        "stop_atr_mult": 0.55,
        "target_atr_mult": 0.95,
        "trail_activate_return_bps": 48.0,
        "trail_floor_return_bps": 22.0,
        "max_hold_seconds": 10800.0,
    },
    "rolling_range_high_breakout": {
        "entry_viability_min": 0.53,
        "entry_revalidate_floor": 0.48,
        "bailout_viability_floor": 0.37,
        "stop_atr_mult": 0.75,
        "target_atr_mult": 1.25,
        "trail_activate_return_bps": 62.0,
        "trail_floor_return_bps": 32.0,
        "max_hold_seconds": 21600.0,
    },
    "breakout_reclaim": {
        "entry_viability_min": 0.52,
        "entry_revalidate_floor": 0.48,
        "bailout_viability_floor": 0.36,
        "stop_atr_mult": 0.62,
        "target_atr_mult": 1.00,
        "trail_activate_return_bps": 52.0,
        "trail_floor_return_bps": 24.0,
        "max_hold_seconds": 14400.0,
    },
    "vwap_reclaim_continuation": {
        "entry_viability_min": 0.53,
        "entry_revalidate_floor": 0.49,
        "bailout_viability_floor": 0.38,
        "stop_atr_mult": 0.58,
        "target_atr_mult": 0.92,
        "trail_activate_return_bps": 50.0,
        "trail_floor_return_bps": 22.0,
        "max_hold_seconds": 10800.0,
    },
    "ema_reclaim_continuation": {
        "entry_viability_min": 0.52,
        "entry_revalidate_floor": 0.48,
        "bailout_viability_floor": 0.37,
        "stop_atr_mult": 0.56,
        "target_atr_mult": 0.88,
        "trail_activate_return_bps": 48.0,
        "trail_floor_return_bps": 20.0,
        "max_hold_seconds": 10800.0,
    },
    "compression_expansion_breakout": {
        "entry_viability_min": 0.55,
        "entry_revalidate_floor": 0.50,
        "bailout_viability_floor": 0.40,
        "stop_atr_mult": 0.68,
        "target_atr_mult": 1.20,
        "trail_activate_return_bps": 58.0,
        "trail_floor_return_bps": 26.0,
        "max_hold_seconds": 18000.0,
    },
    "momentum_follow_through_scalp": {
        "entry_viability_min": 0.57,
        "entry_revalidate_floor": 0.52,
        "bailout_viability_floor": 0.41,
        "stop_atr_mult": 0.50,
        "target_atr_mult": 0.82,
        "trail_activate_return_bps": 45.0,
        "trail_floor_return_bps": 18.0,
        "max_hold_seconds": 7200.0,
    },
    "failed_breakout_bailout": {
        "entry_viability_min": 0.60,
        "entry_revalidate_floor": 0.55,
        "bailout_viability_floor": 0.43,
        "stop_atr_mult": 0.45,
        "target_atr_mult": 0.70,
        "trail_activate_return_bps": 38.0,
        "trail_floor_return_bps": 14.0,
        "max_hold_seconds": 5400.0,
    },
    "no_follow_through_exit": {
        "entry_viability_min": 0.58,
        "entry_revalidate_floor": 0.53,
        "bailout_viability_floor": 0.44,
        "stop_atr_mult": 0.48,
        "target_atr_mult": 0.78,
        "trail_activate_return_bps": 40.0,
        "trail_floor_return_bps": 14.0,
        "max_hold_seconds": 3600.0,
    },
}


def family_default_params(family_id: str | None) -> dict[str, Any]:
    base = _DEFAULTS.get((family_id or "").strip().lower()) or _DEFAULTS["default"]
    out = {"schema_version": PARAM_SCHEMA_VERSION}
    out.update(base)
    return out


def _coerce_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def normalize_strategy_params(raw: Any, *, family_id: str | None = None) -> dict[str, Any]:
    base = family_default_params(family_id)
    src = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {"schema_version": PARAM_SCHEMA_VERSION}
    for key, fallback in base.items():
        if key == "schema_version":
            continue
        low, high = _PARAM_BOUNDS[key]
        val = _coerce_float(src.get(key), float(fallback))
        val = max(low, min(high, val))
        if key == "max_hold_seconds":
            out[key] = int(round(val))
        else:
            out[key] = round(val, 6)
    if out["entry_revalidate_floor"] > out["entry_viability_min"] - 0.005:
        out["entry_revalidate_floor"] = round(max(0.0, out["entry_viability_min"] - 0.005), 6)
    if out["bailout_viability_floor"] > out["entry_revalidate_floor"] - 0.005:
        out["bailout_viability_floor"] = round(max(0.0, out["entry_revalidate_floor"] - 0.005), 6)
    if out["trail_floor_return_bps"] > out["trail_activate_return_bps"] - 1.0:
        out["trail_floor_return_bps"] = round(max(1.0, out["trail_activate_return_bps"] - 1.0), 6)
    return out


def summarize_strategy_params(params: Any) -> dict[str, Any]:
    p = normalize_strategy_params(params)
    return {
        "entry_viability_min": p["entry_viability_min"],
        "entry_revalidate_floor": p["entry_revalidate_floor"],
        "bailout_viability_floor": p["bailout_viability_floor"],
        "stop_atr_mult": p["stop_atr_mult"],
        "target_atr_mult": p["target_atr_mult"],
        "trail_activate_return_bps": p["trail_activate_return_bps"],
        "trail_floor_return_bps": p["trail_floor_return_bps"],
        "max_hold_seconds": p["max_hold_seconds"],
    }


def params_signature(params: Any) -> tuple[Any, ...]:
    p = normalize_strategy_params(params)
    return tuple(p[k] for k in (
        "entry_viability_min",
        "entry_revalidate_floor",
        "bailout_viability_floor",
        "stop_atr_mult",
        "target_atr_mult",
        "trail_activate_return_bps",
        "trail_floor_return_bps",
        "max_hold_seconds",
    ))


def refine_strategy_params(
    base_params: Any,
    outcomes: list[MomentumAutomationOutcome],
) -> tuple[dict[str, Any], dict[str, Any]]:
    base = normalize_strategy_params(base_params)
    if not outcomes:
        return base, {"eligible": False, "reason": "no_outcomes"}

    considered = [row for row in outcomes if row.return_bps is not None]
    if len(considered) < 4:
        return base, {"eligible": False, "reason": "insufficient_outcomes", "sample_size": len(considered)}

    wins = 0
    losses = 0
    return_sum = 0.0
    hold_sum = 0.0
    live_n = 0
    live_return_sum = 0.0
    live_loss_heavy = 0
    for row in considered:
        rb = float(row.return_bps or 0.0)
        return_sum += rb
        if rb > 0:
            wins += 1
        elif rb < 0:
            losses += 1
        if row.hold_seconds is not None:
            hold_sum += float(row.hold_seconds)
        if (row.mode or "").lower() == "live":
            live_n += 1
            live_return_sum += rb
            if rb <= -35.0:
                live_loss_heavy += 1

    sample_n = len(considered)
    mean_return_bps = return_sum / sample_n
    mean_hold_seconds = hold_sum / sample_n if hold_sum > 0 else float(base["max_hold_seconds"])
    win_rate = wins / sample_n if sample_n else 0.0
    live_mean_return_bps = live_return_sum / live_n if live_n else None

    refined = dict(base)
    quality = math.tanh(mean_return_bps / 80.0)
    caution = max(0.0, -quality)
    confidence = max(0.0, quality)

    refined["entry_viability_min"] += 0.025 * caution - 0.012 * confidence
    refined["entry_revalidate_floor"] += 0.020 * caution - 0.010 * confidence
    refined["bailout_viability_floor"] += 0.018 * caution - 0.008 * confidence
    refined["stop_atr_mult"] += 0.10 * confidence - 0.08 * caution
    refined["target_atr_mult"] += 0.18 * confidence - 0.10 * caution
    refined["trail_activate_return_bps"] += -18.0 * caution + 10.0 * confidence
    refined["trail_floor_return_bps"] += -8.0 * caution + 4.0 * confidence

    if win_rate < 0.45:
        refined["max_hold_seconds"] *= 0.82
    elif win_rate > 0.58 and mean_return_bps > 10.0:
        refined["max_hold_seconds"] *= 1.12

    if mean_hold_seconds < base["max_hold_seconds"] * 0.55 and mean_return_bps < 5.0:
        refined["max_hold_seconds"] *= 0.85
    if live_n and live_mean_return_bps is not None and live_mean_return_bps < -20.0:
        refined["entry_viability_min"] += 0.015
        refined["bailout_viability_floor"] += 0.015
        refined["trail_activate_return_bps"] -= 6.0
    if live_loss_heavy >= 2:
        refined["stop_atr_mult"] -= 0.05
        refined["max_hold_seconds"] *= 0.9

    normalized = normalize_strategy_params(refined)
    changed = params_signature(normalized) != params_signature(base)
    meta = {
        "eligible": changed,
        "sample_size": sample_n,
        "win_rate": round(win_rate, 4),
        "mean_return_bps": round(mean_return_bps, 4),
        "mean_hold_seconds": int(round(mean_hold_seconds)),
        "live_sample_size": live_n,
        "live_mean_return_bps": round(live_mean_return_bps, 4) if live_mean_return_bps is not None else None,
    }
    if not changed:
        meta["reason"] = "no_material_change"
    return normalized, meta
