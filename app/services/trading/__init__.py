"""Trading package — re-exports all public functions for backward compatibility.

**New code:** prefer stable symbols from ``app.services.trading.public_api`` where
listed; avoid new imports of underscore-prefixed names from this package or
``trading_service``.

The original monolithic ``trading_service.py`` has been split into focused modules:

- ``market_data``  — OHLCV, quotes, search, technical indicators
- ``portfolio``    — watchlist, trades, P&L analytics, insights
- ``journal``      — journal entries, auto-journaling, reviews
- ``scanner``      — ticker scoring, custom screener, batch scanning, smart pick
- ``ai_context``   — AI context building, market thesis
- ``learning``     — pattern mining, learning cycles, brain stats

Callers that do ``from app.services import trading_service as ts`` and use
``ts.fetch_ohlcv(...)`` etc. will continue to work unchanged.
"""

from ...models.trading import Trade  # noqa: F401 — used by routers as ts.Trade

# Market data
from .market_data import (
    _clamp_period,
    fetch_ohlcv,
    fetch_quote,
    fetch_quotes_batch,
    search_tickers,
    compute_indicators,
    get_indicator_snapshot,
    ticker_display_name,
    is_crypto,
    DEFAULT_SCAN_TICKERS,
    DEFAULT_CRYPTO_TICKERS,
    ALL_SCAN_TICKERS,
    get_vix,
    get_volatility_regime,
    get_market_regime,
)
from ..yf_session import get_ticker_info, get_ticker_news

# Portfolio
from .portfolio import (
    get_watchlist,
    add_to_watchlist,
    remove_from_watchlist,
    create_trade,
    close_trade,
    delete_trade,
    assign_scan_pattern_to_trade,
    get_trades,
    get_trade_stats,
    get_trade_stats_by_source,
    get_trade_stats_by_pattern,
    get_daily_pnl,
    get_insights,
    save_insight,
    get_portfolio_summary,
)

# Journal
from .journal import (
    add_journal_entry,
    get_journal,
    auto_journal_trade_open,
    daily_market_journal,
    check_signal_events,
    weekly_performance_review,
)

# Scanner
from .scanner import (
    _score_ticker,
    _score_ticker_intraday,
    _score_breakout,
    smart_pick_context,
    _build_smart_pick_context_strings,
    _validate_live_prices,
    PRESET_SCREENS,
    _eval_condition,
    run_custom_screen,
    run_scan,
    run_daytrade_scan,
    run_breakout_scan,
    run_momentum_scanner,
    get_daytrade_cache,
    get_breakout_cache,
    evolve_strategy_weights,
    get_all_weights,
    get_latest_scan,
    generate_signals,
    generate_top_picks,
    get_top_picks_freshness,
    recheck_pick,
    get_scan_status,
    get_intraday_scan_progress,
    batch_score_tickers,
    run_full_market_scan,
    smart_pick,
    classify_trade_type,
)

# AI context
from .ai_context import (
    build_ai_context,
    build_market_context,
    generate_market_thesis,
)

# Learning
from .learning import (
    log_learning_event,
    get_learning_events,
    analyze_closed_trade,
    take_market_snapshot,
    take_all_snapshots,
    backfill_future_returns,
    backfill_predicted_scores,
    mine_patterns,
    seek_pattern_data,
    deep_study,
    validate_and_evolve,
    refine_patterns,
    get_brain_stats,
    get_confidence_history,
    get_learning_status,
    get_current_predictions,
    run_learning_cycle,
    run_promoted_pattern_fast_eval,
    dedup_existing_patterns,
    get_accuracy_detail,
)


# ML engine (deprecated stubs — real logic lives in pattern_ml)
from .ml_engine import (
    train_model as train_ml_model,
    extract_features as extract_ml_features,
    get_model_stats as get_ml_model_stats,
    load_model as load_ml_model,
    is_model_ready as is_ml_model_ready,
)

# Pattern-driven ML
from .pattern_ml import (
    get_meta_learner,
    load_meta_learner,
    apply_ml_feedback,
)

# Pre-screener: DB-backed universe via prescreen_job; live providers only as cold-start fallback
from .prescreen_job import prescreen_candidates_for_universe, run_daily_prescreen_job
from .prescreener import (
    get_daytrade_candidates,
    get_breakout_candidates,
    get_prescreen_status,
    invalidate_cache as invalidate_auxiliary_prescreen_caches,
    get_trending_crypto,
)

# Alerts & strategy proposals
from .alerts import (
    dispatch_alert,
    get_alert_history,
    generate_strategy_proposals,
    create_proposal_from_pick,
    get_proposals,
    approve_proposal,
    reject_proposal,
    recheck_proposal,
    run_price_monitor,
)


# Portfolio risk controls
from .portfolio_risk import (
    get_portfolio_risk_snapshot,
    check_new_trade_allowed,
    size_position,
    get_risk_limits,
    check_drawdown_breaker,
    is_breaker_tripped,
    get_breaker_status,
    reset_breaker,
)


def signal_shutdown():
    """Propagate shutdown signal to all sub-modules."""
    from . import scanner, learning
    scanner.signal_shutdown()
    learning.signal_shutdown()
