from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from ..db import Base


class WatchlistItem(Base):
    __tablename__ = "trading_watchlist"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    ticker: str = Column(String(20), nullable=False)
    added_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class Trade(Base):
    __tablename__ = "trading_trades"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    ticker: str = Column(String(20), nullable=False)
    direction: str = Column(String(10), nullable=False, default="long")  # long / short
    entry_price: float = Column(Float, nullable=False)
    exit_price: Optional[float] = Column(Float, nullable=True)
    quantity: float = Column(Float, nullable=False, default=1.0)
    entry_date: datetime = Column(DateTime, nullable=False, default=datetime.utcnow)
    exit_date: Optional[datetime] = Column(DateTime, nullable=True)
    status: str = Column(String(20), nullable=False, default="open")  # open / working / closed / cancelled / rejected
    pnl: Optional[float] = Column(Float, nullable=True)
    tags: Optional[str] = Column(String(500), nullable=True)
    notes: Optional[str] = Column(Text, nullable=True)
    indicator_snapshot: Optional[str] = Column(Text, nullable=True)  # JSON blob
    broker_source: Optional[str] = Column(String(20), nullable=True)  # "robinhood" / "manual" / None
    broker_order_id: Optional[str] = Column(String(100), nullable=True)
    broker_status: Optional[str] = Column(String(30), nullable=True)  # raw RH state: queued / confirmed / partially_filled / filled / cancelled / rejected / failed
    last_broker_sync: Optional[datetime] = Column(DateTime, nullable=True)
    filled_at: Optional[datetime] = Column(DateTime, nullable=True)
    avg_fill_price: Optional[float] = Column(Float, nullable=True)
    # TCA: reference = signal/proposal limit at submit; slippage set when fill is known
    tca_reference_entry_price: Optional[float] = Column(Float, nullable=True)
    tca_entry_slippage_bps: Optional[float] = Column(Float, nullable=True)
    # Exit TCA: reference = mid/quote (or explicit) at close decision; fill = exit_price
    tca_reference_exit_price: Optional[float] = Column(Float, nullable=True)
    tca_exit_slippage_bps: Optional[float] = Column(Float, nullable=True)
    # Attribution: link live trades to proposal + promoted scan pattern
    strategy_proposal_id: Optional[int] = Column(Integer, nullable=True, index=True)
    scan_pattern_id: Optional[int] = Column(Integer, nullable=True, index=True)
    pattern_tags: Optional[str] = Column(String(500), nullable=True)  # comma-separated insight/pattern labels


class JournalEntry(Base):
    __tablename__ = "trading_journal"

    id: int = Column(Integer, primary_key=True, index=True)
    trade_id: Optional[int] = Column(Integer, nullable=True, index=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    content: str = Column(Text, nullable=False)
    indicator_snapshot: Optional[str] = Column(Text, nullable=True)  # JSON blob
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class TradingInsight(Base):
    """AI-generated trading insights learned from the journal over time."""
    __tablename__ = "trading_insights"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    scan_pattern_id: int = Column(
        Integer, ForeignKey("scan_patterns.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    scan_pattern = relationship("ScanPattern", back_populates="trading_insights")
    pattern_description: str = Column(Text, nullable=False)
    confidence: float = Column(Float, nullable=False, default=0.5)
    evidence_count: int = Column(Integer, nullable=False, default=1)
    win_count: int = Column(Integer, nullable=False, default=0)
    loss_count: int = Column(Integer, nullable=False, default=0)
    last_seen: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    active: bool = Column(Boolean, default=True, nullable=False)


class ScanResult(Base):
    """AI scanner output: a scored stock pick with entry/exit levels."""
    __tablename__ = "trading_scans"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    ticker: str = Column(String(20), nullable=False, index=True)
    score: float = Column(Float, nullable=False)  # 1-10 confluence score
    signal: str = Column(String(10), nullable=False)  # buy / sell / hold
    entry_price: Optional[float] = Column(Float, nullable=True)
    stop_loss: Optional[float] = Column(Float, nullable=True)
    take_profit: Optional[float] = Column(Float, nullable=True)
    risk_level: str = Column(String(10), nullable=False, default="medium")  # low / medium / high
    rationale: str = Column(Text, nullable=False, default="")
    indicator_data: Optional[str] = Column(Text, nullable=True)  # JSON blob
    scanned_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class BacktestResult(Base):
    """Stored backtest run results for strategy comparison."""
    __tablename__ = "trading_backtests"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    ticker: str = Column(String(20), nullable=False)
    strategy_name: str = Column(String(100), nullable=False)
    params: Optional[str] = Column(Text, nullable=True)  # JSON blob
    return_pct: float = Column(Float, nullable=False, default=0.0)
    win_rate: float = Column(Float, nullable=False, default=0.0)
    sharpe: Optional[float] = Column(Float, nullable=True)
    max_drawdown: float = Column(Float, nullable=False, default=0.0)
    trade_count: int = Column(Integer, nullable=False, default=0)
    equity_curve: Optional[str] = Column(Text, nullable=True)  # JSON list
    ran_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    related_insight_id: Optional[int] = Column(Integer, nullable=True, index=True)
    scan_pattern_id: Optional[int] = Column(Integer, nullable=True, index=True)


class MarketSnapshot(Base):
    """Daily indicator snapshot for continuous pattern mining."""
    __tablename__ = "trading_snapshots"

    id: int = Column(Integer, primary_key=True, index=True)
    ticker: str = Column(String(20), nullable=False, index=True)
    snapshot_date: datetime = Column(DateTime, nullable=False, index=True)
    close_price: float = Column(Float, nullable=False)
    indicator_data: Optional[str] = Column(Text, nullable=True)  # JSON blob
    predicted_score: Optional[float] = Column(Float, nullable=True)  # -10 bearish to +10 bullish
    vix_at_snapshot: Optional[float] = Column(Float, nullable=True)
    future_return_1d: Optional[float] = Column(Float, nullable=True)
    future_return_3d: Optional[float] = Column(Float, nullable=True)
    future_return_5d: Optional[float] = Column(Float, nullable=True)  # filled later
    future_return_10d: Optional[float] = Column(Float, nullable=True)
    news_sentiment: Optional[float] = Column(Float, nullable=True)
    news_count: Optional[int] = Column(Integer, nullable=True)
    pe_ratio: Optional[float] = Column(Float, nullable=True)
    market_cap_b: Optional[float] = Column(Float, nullable=True)  # billions


class LearningEvent(Base):
    """Tracks every AI learning action for the Brain dashboard."""
    __tablename__ = "trading_learning_events"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    event_type: str = Column(String(30), nullable=False)  # discovery / update / demotion / review / journal
    description: str = Column(Text, nullable=False)
    confidence_before: Optional[float] = Column(Float, nullable=True)
    confidence_after: Optional[float] = Column(Float, nullable=True)
    related_insight_id: Optional[int] = Column(Integer, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class AlertHistory(Base):
    """Log of all SMS/notification alerts sent to the user."""
    __tablename__ = "trading_alerts"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    alert_type: str = Column(String(30), nullable=False)
    ticker: str = Column(String(20), nullable=True)
    message: str = Column(Text, nullable=False)
    trade_type: Optional[str] = Column(String(30), nullable=True)
    duration_estimate: Optional[str] = Column(String(60), nullable=True)
    sent_via: str = Column(String(20), nullable=False, default="email_gateway")
    success: bool = Column(Boolean, nullable=False, default=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class BreakoutAlert(Base):
    """Tracks each breakout alert with outcome data for the learning feedback loop."""
    __tablename__ = "trading_breakout_alerts"

    id: int = Column(Integer, primary_key=True, index=True)
    ticker: str = Column(String(20), nullable=False, index=True)
    asset_type: str = Column(String(10), nullable=False, default="crypto")
    alert_tier: str = Column(String(50), nullable=False)
    score_at_alert: float = Column(Float, nullable=False)
    indicator_snapshot: Optional[str] = Column(Text, nullable=True)
    price_at_alert: float = Column(Float, nullable=False)
    entry_price: Optional[float] = Column(Float, nullable=True)
    stop_loss: Optional[float] = Column(Float, nullable=True)
    target_price: Optional[float] = Column(Float, nullable=True)
    signals_snapshot: Optional[str] = Column(Text, nullable=True)
    alerted_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    price_1h: Optional[float] = Column(Float, nullable=True)
    price_4h: Optional[float] = Column(Float, nullable=True)
    price_24h: Optional[float] = Column(Float, nullable=True)
    breakout_occurred: Optional[bool] = Column(Boolean, nullable=True)
    max_gain_pct: Optional[float] = Column(Float, nullable=True)
    max_drawdown_pct: Optional[float] = Column(Float, nullable=True)
    outcome: str = Column(String(20), nullable=False, default="pending")
    outcome_checked_at: Optional[datetime] = Column(DateTime, nullable=True)

    # Exit optimization fields
    time_to_peak_hours: Optional[float] = Column(Float, nullable=True)
    time_to_stop_hours: Optional[float] = Column(Float, nullable=True)
    price_at_peak: Optional[float] = Column(Float, nullable=True)
    optimal_exit_pct: Optional[float] = Column(Float, nullable=True)

    # Context at alert time
    regime_at_alert: Optional[str] = Column(String(20), nullable=True)
    scan_cycle_id: Optional[str] = Column(String(40), nullable=True, index=True)
    timeframe: Optional[str] = Column(String(10), nullable=True)
    sector: Optional[str] = Column(String(60), nullable=True)
    news_sentiment_at_alert: Optional[float] = Column(Float, nullable=True)


class StrategyProposal(Base):
    """AI-generated trade proposal for user review and optional auto-execution."""
    __tablename__ = "trading_proposals"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    ticker: str = Column(String(20), nullable=False, index=True)
    direction: str = Column(String(10), nullable=False, default="long")  # long / short
    status: str = Column(String(20), nullable=False, default="pending")  # pending / approved / rejected / executed / expired

    entry_price: float = Column(Float, nullable=False)
    stop_loss: float = Column(Float, nullable=False)
    take_profit: float = Column(Float, nullable=False)
    quantity: Optional[float] = Column(Float, nullable=True)
    position_size_pct: Optional[float] = Column(Float, nullable=True)

    projected_profit_pct: float = Column(Float, nullable=False, default=0.0)
    projected_loss_pct: float = Column(Float, nullable=False, default=0.0)
    risk_reward_ratio: float = Column(Float, nullable=False, default=0.0)
    confidence: float = Column(Float, nullable=False, default=0.0)

    timeframe: str = Column(String(30), nullable=False, default="swing")
    thesis: str = Column(Text, nullable=False, default="")
    signals_json: Optional[str] = Column(Text, nullable=True)
    indicator_json: Optional[str] = Column(Text, nullable=True)

    brain_score: Optional[float] = Column(Float, nullable=True)
    ml_probability: Optional[float] = Column(Float, nullable=True)
    scan_score: Optional[float] = Column(Float, nullable=True)

    proposed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    reviewed_at: Optional[datetime] = Column(DateTime, nullable=True)
    executed_at: Optional[datetime] = Column(DateTime, nullable=True)
    expires_at: Optional[datetime] = Column(DateTime, nullable=True)

    broker_order_id: Optional[str] = Column(String(100), nullable=True)
    trade_id: Optional[int] = Column(Integer, nullable=True)
    scan_pattern_id: Optional[int] = Column(Integer, nullable=True, index=True)


class ScanPattern(Base):
    """Composable breakout/screener pattern — builtin, user-submitted, or brain-discovered."""
    __tablename__ = "scan_patterns"

    id: int = Column(Integer, primary_key=True, index=True)
    name: str = Column(String(120), nullable=False)
    description: Optional[str] = Column(Text, nullable=True)
    rules_json: str = Column(Text, nullable=False, default="{}")
    origin: str = Column(String(30), nullable=False, default="user")
    asset_class: str = Column(String(20), nullable=False, default="all")
    timeframe: str = Column(String(10), nullable=False, default="1d")
    confidence: float = Column(Float, nullable=False, default=0.0)
    evidence_count: int = Column(Integer, nullable=False, default=0)
    win_rate: Optional[float] = Column(Float, nullable=True)
    avg_return_pct: Optional[float] = Column(Float, nullable=True)
    backtest_count: int = Column(Integer, nullable=False, default=0)
    score_boost: float = Column(Float, nullable=False, default=0.0)
    min_base_score: float = Column(Float, nullable=False, default=0.0)
    active: bool = Column(Boolean, nullable=False, default=True)
    parent_id: Optional[int] = Column(Integer, nullable=True, index=True)
    exit_config: Optional[str] = Column(Text, nullable=True)
    variant_label: Optional[str] = Column(String(40), nullable=True)
    generation: int = Column(Integer, nullable=False, default=0)
    ticker_scope: str = Column(String(20), nullable=False, default="universal")
    scope_tickers: Optional[str] = Column(Text, nullable=True)
    trade_count: int = Column(Integer, nullable=False, default=0)
    backtest_priority: int = Column(Integer, nullable=False, default=0)
    last_backtest_at: Optional[datetime] = Column(DateTime, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Out-of-sample / promotion (see brain_oos_* settings and learning.py gates)
    promotion_status: str = Column(String(32), nullable=False, default="legacy")
    oos_win_rate: Optional[float] = Column(Float, nullable=True)
    oos_avg_return_pct: Optional[float] = Column(Float, nullable=True)
    oos_trade_count: Optional[int] = Column(Integer, nullable=True)
    backtest_spread_used: Optional[float] = Column(Float, nullable=True)
    backtest_commission_used: Optional[float] = Column(Float, nullable=True)
    oos_evaluated_at: Optional[datetime] = Column(DateTime, nullable=True)
    bench_walk_forward_json: Optional[dict] = Column(JSONB, nullable=True)

    trading_insights = relationship("TradingInsight", back_populates="scan_pattern")


class PatternTradeRow(Base):
    """One simulated or historical pattern occurrence for trade-level analytics.

    See docs/PATTERN_TRADE_ANALYTICS.md for unit of observation and schema versions.
    """

    __tablename__ = "trading_pattern_trades"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    scan_pattern_id: Optional[int] = Column(Integer, nullable=True, index=True)
    related_insight_id: Optional[int] = Column(Integer, nullable=True, index=True)
    backtest_result_id: Optional[int] = Column(Integer, nullable=True, index=True)
    ticker: str = Column(String(20), nullable=False, index=True)
    as_of_ts: datetime = Column(DateTime, nullable=False, index=True)
    timeframe: str = Column(String(10), nullable=False, default="1d")
    asset_class: str = Column(String(20), nullable=False, default="stock")
    fwd_ret_1b: Optional[float] = Column(Float, nullable=True)
    fwd_ret_3b: Optional[float] = Column(Float, nullable=True)
    fwd_ret_5b: Optional[float] = Column(Float, nullable=True)
    fwd_ret_10b: Optional[float] = Column(Float, nullable=True)
    mfe_pct: Optional[float] = Column(Float, nullable=True)
    mae_pct: Optional[float] = Column(Float, nullable=True)
    hold_bars: Optional[int] = Column(Integer, nullable=True)
    r_multiple: Optional[float] = Column(Float, nullable=True)
    outcome_return_pct: Optional[float] = Column(Float, nullable=True)
    label_win: Optional[bool] = Column(Boolean, nullable=True)
    features_json: dict = Column(JSONB, nullable=False)  # always set at insert
    source: str = Column(String(40), nullable=False, default="queue_backtest")
    feature_schema_version: str = Column(String(20), nullable=False, default="1")
    code_version: Optional[str] = Column(String(40), nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class PatternEvidenceHypothesis(Base):
    """Evidence card from pattern trade analytics (distinct from TradingHypothesis A/B pool)."""

    __tablename__ = "trading_pattern_evidence_hypotheses"

    id: int = Column(Integer, primary_key=True, index=True)
    scan_pattern_id: int = Column(Integer, nullable=False, index=True)
    title: str = Column(String(200), nullable=False)
    predicate_json: dict = Column(JSONB, nullable=False)
    status: str = Column(String(20), nullable=False, default="proposed")
    metrics_json: dict = Column(JSONB, nullable=False)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class LearningCycleAiReport(Base):
    """LLM-generated (or fallback) summary of one completed learning cycle."""

    __tablename__ = "trading_learning_cycle_ai_reports"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    content: str = Column(Text, nullable=False)
    metrics_json: dict = Column(JSONB, nullable=False, default=lambda: {})


class TradingHypothesis(Base):
    """Dynamic A/B hypothesis for the brain's self-validation loop.

    Each row represents a testable claim like 'RSI > 65 + EMA stack + retests >= 3
    outperforms RSI > 65 + EMA stack alone'.  The learning cycle evaluates these
    against mined historical data each run and tracks confirmation rate over time.
    """
    __tablename__ = "trading_hypotheses"

    id: int = Column(Integer, primary_key=True, index=True)
    description: str = Column(Text, nullable=False)
    condition_a: str = Column(Text, nullable=False)
    condition_b: str = Column(Text, nullable=False)
    expected_winner: str = Column(String(5), nullable=False, default="a")
    origin: str = Column(String(30), nullable=False, default="llm_generated")
    status: str = Column(String(20), nullable=False, default="pending")
    times_tested: int = Column(Integer, nullable=False, default=0)
    times_confirmed: int = Column(Integer, nullable=False, default=0)
    times_rejected: int = Column(Integer, nullable=False, default=0)
    last_result_json: Optional[str] = Column(Text, nullable=True)
    related_weight: Optional[str] = Column(String(80), nullable=True)
    related_pattern_id: Optional[int] = Column(Integer, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_tested_at: Optional[datetime] = Column(DateTime, nullable=True)
