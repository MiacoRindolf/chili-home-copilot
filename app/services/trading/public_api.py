"""Stable, explicit public surface for the trading services package.

Routers and external callers should prefer importing from here rather than
reaching into internal modules directly.  This makes refactoring internals
possible without breaking the router layer.

**Sections:**
  - Predictions / learning
  - Scanner / discovery
  - Runtime / operator
  - Risk / portfolio
  - Attribution / review
  - Thesis / evidence
  - Pattern engine
  - Backtest helpers
  - TCA
"""
from __future__ import annotations

# ── Predictions & learning ──────────────────────────────────────────────────
from .journal import weekly_performance_review
from .learning import get_current_predictions, refresh_promoted_prediction_cache
from .learning_predictions import compute_prediction, predict_confidence, predict_direction

# ── Scanner / discovery ─────────────────────────────────────────────────────
from .scanner import (
    get_crypto_breakout_cache,
    get_top_picks_freshness,
    run_crypto_breakout_scan,
    run_momentum_scanner,
    validate_live_prices,
)

# ── Runtime / operator ──────────────────────────────────────────────────────
from . import runtime_status as _runtime_status

# ── Risk / portfolio ────────────────────────────────────────────────────────
from .portfolio_risk import (
    get_breaker_status,
    get_drawdown_limits,
    get_portfolio_risk_snapshot,
    get_risk_limits,
    reset_breaker,
)

# ── Attribution / review ────────────────────────────────────────────────────
from .attribution_service import live_vs_research_by_pattern, post_trade_review

# ── Thesis / evidence ──────────────────────────────────────────────────────
from .thesis import enrich_picks_with_evidence

# ── Pattern engine ──────────────────────────────────────────────────────────
from .pattern_engine import create_pattern, delete_pattern, list_patterns, update_pattern
from .pattern_resolution import resolve_to_scan_pattern
from .pine_export import scan_pattern_to_pine
from .web_pattern_researcher import get_research_status, run_web_pattern_research

# ── Backtest helpers ────────────────────────────────────────────────────────
from .backtest_metrics import backtest_win_rate_db_to_display_pct

# ── TCA ─────────────────────────────────────────────────────────────────────
from .tca_service import (
    apply_tca_on_trade_close,
    resolve_exit_reference_price,
    tca_summary_by_ticker,
)

__all__ = [
    # predictions / learning
    "weekly_performance_review",
    "compute_prediction",
    "predict_direction",
    "predict_confidence",
    "get_current_predictions",
    "refresh_promoted_prediction_cache",
    # scanner / discovery
    "get_crypto_breakout_cache",
    "get_top_picks_freshness",
    "run_crypto_breakout_scan",
    "run_momentum_scanner",
    "validate_live_prices",
    # runtime / operator
    "get_runtime_overview",
    "get_freshness_summary",
    # risk / portfolio
    "get_portfolio_risk_snapshot",
    "get_risk_limits",
    "get_drawdown_limits",
    "get_breaker_status",
    "reset_breaker",
    # attribution / review
    "live_vs_research_by_pattern",
    "post_trade_review",
    # thesis / evidence
    "enrich_picks_with_evidence",
    # pattern engine
    "list_patterns",
    "create_pattern",
    "update_pattern",
    "delete_pattern",
    "resolve_to_scan_pattern",
    "scan_pattern_to_pine",
    "run_web_pattern_research",
    "get_research_status",
    # backtest helpers
    "backtest_win_rate_db_to_display_pct",
    # tca
    "tca_summary_by_ticker",
    "apply_tca_on_trade_close",
    "resolve_exit_reference_price",
]


def get_runtime_overview(db):
    return _runtime_status.get_runtime_overview(db)


def get_freshness_summary(db):
    return _runtime_status.get_freshness_summary(db)
