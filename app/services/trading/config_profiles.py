"""
Pre-defined configuration profiles for the trading brain.

Each profile is a dict of Settings field names -> values.  Only brain_*
fields that meaningfully differ between risk appetites are included;
everything else keeps its current (or default) value.
"""
from __future__ import annotations

from typing import Any

PROFILES: dict[str, dict[str, Any]] = {
    "conservative": {
        # Risk limits
        "brain_max_open_per_sector": 1,

        # Quality gates — demand more evidence before trading
        "brain_tradeable_min_oos_wr": 62.0,
        "brain_tradeable_min_oos_trades": 25,
        "brain_oos_min_win_rate_pct": 55.0,
        "brain_oos_holdout_fraction": 0.30,
        "brain_bench_require_stress_pass": True,

        # Mining — higher bar for discoveries
        "brain_evolution_min_trades": 10,

        # Backtesting
        "brain_budget_ohlcv_per_cycle": 200,

        # Signals — fewer, higher quality
        "brain_fast_eval_max_tickers": 200,
        "brain_fast_eval_interval_minutes": 15,
        "brain_queue_exploration_max": 20,

        # Tradeable limit
        "brain_tradeable_limit": 8,
    },

    "moderate": {
        "brain_max_open_per_sector": 2,

        "brain_tradeable_min_oos_wr": 52.0,
        "brain_tradeable_min_oos_trades": 12,
        "brain_oos_min_win_rate_pct": 48.0,
        "brain_oos_holdout_fraction": 0.25,
        "brain_bench_require_stress_pass": False,

        "brain_evolution_min_trades": 5,

        "brain_budget_ohlcv_per_cycle": 280,

        "brain_fast_eval_max_tickers": 400,
        "brain_fast_eval_interval_minutes": 10,
        "brain_queue_exploration_max": 40,

        "brain_tradeable_limit": 20,
    },

    "aggressive": {
        "brain_max_open_per_sector": 4,

        "brain_tradeable_min_oos_wr": 45.0,
        "brain_tradeable_min_oos_trades": 5,
        "brain_oos_min_win_rate_pct": 42.0,
        "brain_oos_holdout_fraction": 0.20,
        "brain_bench_require_stress_pass": False,

        "brain_evolution_min_trades": 3,

        "brain_budget_ohlcv_per_cycle": 400,

        "brain_fast_eval_max_tickers": 600,
        "brain_fast_eval_interval_minutes": 5,
        "brain_queue_exploration_max": 80,

        "brain_tradeable_limit": 40,
    },
}

PROFILE_NAMES = list(PROFILES.keys())


def get_profile(name: str) -> dict[str, Any]:
    """Return a profile dict by name, or raise KeyError."""
    return PROFILES[name]


def list_profiles() -> list[dict[str, Any]]:
    """Return summary list suitable for API responses."""
    result = []
    for name, settings in PROFILES.items():
        result.append({
            "name": name,
            "description": _DESCRIPTIONS.get(name, ""),
            "setting_count": len(settings),
            "settings": settings,
        })
    return result


_DESCRIPTIONS: dict[str, str] = {
    "conservative": (
        "Higher evidence thresholds, fewer positions, stricter quality gates. "
        "Best for preserving capital and building confidence in the system."
    ),
    "moderate": (
        "Balanced defaults. Reasonable quality gates with enough flexibility "
        "to discover and trade a broad pattern set."
    ),
    "aggressive": (
        "Lower thresholds, more positions, faster scanning. "
        "Maximises signal surface area — higher risk, more opportunities."
    ),
}
