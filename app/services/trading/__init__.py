"""Trading package — re-exports all public functions for backward compatibility.

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

# Market data
from .market_data import (
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
    get_trades,
    get_trade_stats,
    get_trade_stats_by_pattern,
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
    PRESET_SCREENS,
    _eval_condition,
    run_custom_screen,
    run_scan,
    run_daytrade_scan,
    run_breakout_scan,
    run_momentum_scanner,
    evolve_strategy_weights,
    get_all_weights,
    get_latest_scan,
    generate_signals,
    generate_top_picks,
    get_scan_status,
    batch_score_tickers,
    run_full_market_scan,
    smart_pick,
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
    should_run_learning,
    dedup_existing_patterns,
    get_accuracy_detail,
)


# ML engine
from .ml_engine import (
    train_model as train_ml_model,
    predict_ml,
    extract_features as extract_ml_features,
    get_model_stats as get_ml_model_stats,
    load_model as load_ml_model,
    is_model_ready as is_ml_model_ready,
)

# Pre-screener (fast server-side filtering)
from .prescreener import (
    get_prescreened_candidates,
    get_daytrade_candidates,
    get_breakout_candidates,
    get_prescreen_status,
    invalidate_cache as invalidate_prescreen_cache,
    get_trending_crypto,
)

# Alerts & strategy proposals
from .alerts import (
    dispatch_alert,
    get_alert_history,
    generate_strategy_proposals,
    get_proposals,
    approve_proposal,
    reject_proposal,
    run_price_monitor,
)


def signal_shutdown():
    """Propagate shutdown signal to all sub-modules."""
    from . import scanner, learning
    scanner.signal_shutdown()
    learning.signal_shutdown()
