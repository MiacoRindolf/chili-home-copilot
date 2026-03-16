"""Pattern Recognition: mining, validation, refinement, backtesting.

Re-exports pattern recognition functions from learning.py to establish a
cleaner module boundary for the pattern subsystem.
"""
from __future__ import annotations

from .learning import (
    analyze_closed_trade,
    mine_patterns,
    seek_pattern_data,
    validate_and_evolve,
    mine_intraday_patterns,
    learn_from_breakout_outcomes,
    mine_fakeout_patterns,
    learn_inter_alert_patterns,
    learn_timeframe_performance,
    decay_stale_insights,
    mine_signal_synergies,
    refine_patterns,
    deep_study,
    dedup_existing_patterns,
)

__all__ = [
    "analyze_closed_trade",
    "mine_patterns",
    "seek_pattern_data",
    "validate_and_evolve",
    "mine_intraday_patterns",
    "learn_from_breakout_outcomes",
    "mine_fakeout_patterns",
    "learn_inter_alert_patterns",
    "learn_timeframe_performance",
    "decay_stale_insights",
    "mine_signal_synergies",
    "refine_patterns",
    "deep_study",
    "dedup_existing_patterns",
]
