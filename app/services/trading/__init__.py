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
)

# Portfolio
from .portfolio import (
    get_watchlist,
    add_to_watchlist,
    remove_from_watchlist,
    create_trade,
    close_trade,
    get_trades,
    get_trade_stats,
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
    PRESET_SCREENS,
    _eval_condition,
    run_custom_screen,
    run_scan,
    get_latest_scan,
    generate_signals,
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
    mine_patterns,
    deep_study,
    get_brain_stats,
    get_confidence_history,
    get_learning_status,
    run_learning_cycle,
    should_run_learning,
)


def signal_shutdown():
    """Propagate shutdown signal to all sub-modules."""
    from . import scanner, learning
    scanner.signal_shutdown()
    learning.signal_shutdown()
