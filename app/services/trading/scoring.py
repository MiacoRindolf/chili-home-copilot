"""Scoring: signal evaluation, pattern detection, composite scoring.

This module re-exports scoring functions from scanner.py to establish a
cleaner module boundary. Functions will be migrated here incrementally.
"""
from __future__ import annotations

# Re-export scoring functions from scanner for clean import paths.
# Other modules should import from here: `from .scoring import ...`
from .scanner import (
    get_adaptive_weight,
    get_all_weights,
    evolve_strategy_weights,
    _score_ticker,
    _score_ticker_impl,
    _score_ticker_intraday,
    _score_crypto_breakout,
    _score_breakout,
    _detect_vcp,
    _detect_narrow_range,
    _detect_accumulation,
    _detect_divergence,
    _detect_candle_pattern,
    _detect_vwap_reclaim,
    _eval_condition,
)

__all__ = [
    "get_adaptive_weight",
    "get_all_weights",
    "evolve_strategy_weights",
    "_score_ticker",
    "_score_ticker_impl",
    "_score_ticker_intraday",
    "_score_crypto_breakout",
    "_score_breakout",
    "_detect_vcp",
    "_detect_narrow_range",
    "_detect_accumulation",
    "_detect_divergence",
    "_detect_candle_pattern",
    "_detect_vwap_reclaim",
    "_eval_condition",
]
