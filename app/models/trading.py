from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from ..db import Base


class WatchlistItem(Base):
    __tablename__ = "trading_watchlist"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ticker: str = Column(String(20), nullable=False)
    added_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class Trade(Base):
    __tablename__ = "trading_trades"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ticker: str = Column(String(20), nullable=False)
    sector: Optional[str] = Column(String(80), nullable=True, index=True)
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
    indicator_snapshot: Optional[dict] = Column(JSONB, nullable=True)
    broker_source: Optional[str] = Column(String(20), nullable=True)  # "robinhood" / "manual" / None
    broker_order_id: Optional[str] = Column(String(100), nullable=True)
    broker_status: Optional[str] = Column(String(30), nullable=True)  # raw RH state: queued / confirmed / partially_filled / filled / cancelled / rejected / failed
    last_broker_sync: Optional[datetime] = Column(DateTime, nullable=True)
    filled_at: Optional[datetime] = Column(DateTime, nullable=True)
    avg_fill_price: Optional[float] = Column(Float, nullable=True)
    filled_quantity: Optional[float] = Column(Float, nullable=True)
    remaining_quantity: Optional[float] = Column(Float, nullable=True)
    submitted_at: Optional[datetime] = Column(DateTime, nullable=True)
    acknowledged_at: Optional[datetime] = Column(DateTime, nullable=True)
    first_fill_at: Optional[datetime] = Column(DateTime, nullable=True)
    last_fill_at: Optional[datetime] = Column(DateTime, nullable=True)
    # TCA: reference = signal/proposal limit at submit; slippage set when fill is known
    tca_reference_entry_price: Optional[float] = Column(Float, nullable=True)
    tca_entry_slippage_bps: Optional[float] = Column(Float, nullable=True)
    # Exit TCA: reference = mid/quote (or explicit) at close decision; fill = exit_price
    tca_reference_exit_price: Optional[float] = Column(Float, nullable=True)
    tca_exit_slippage_bps: Optional[float] = Column(Float, nullable=True)
    # Attribution: link live trades to proposal + promoted scan pattern
    strategy_proposal_id: Optional[int] = Column(
        Integer, ForeignKey("trading_proposals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scan_pattern_id: Optional[int] = Column(
        Integer, ForeignKey("scan_patterns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    pattern_tags: Optional[str] = Column(String(500), nullable=True)
    # Stop engine: first-class stop/target/trail columns (migration 113)
    stop_loss: Optional[float] = Column(Float, nullable=True)
    take_profit: Optional[float] = Column(Float, nullable=True)
    trail_stop: Optional[float] = Column(Float, nullable=True)
    high_watermark: Optional[float] = Column(Float, nullable=True)
    stop_model: Optional[str] = Column(String(30), nullable=True)
    exit_reason: Optional[str] = Column(String(50), nullable=True)
    trade_type: Optional[str] = Column(String(30), nullable=True)
    related_alert_id: Optional[int] = Column(
        Integer, ForeignKey("trading_breakout_alerts.id", ondelete="SET NULL"), nullable=True, index=True
    )


class PatternMonitorDecision(Base):
    """Logs each pattern-health evaluation and resulting stop/target adjustment."""
    __tablename__ = "trading_pattern_monitor_decisions"

    id: int = Column(Integer, primary_key=True, index=True)
    trade_id: int = Column(Integer, ForeignKey("trading_trades.id", ondelete="CASCADE"), nullable=False)
    breakout_alert_id: Optional[int] = Column(
        Integer, ForeignKey("trading_breakout_alerts.id", ondelete="SET NULL"), nullable=True
    )
    scan_pattern_id: Optional[int] = Column(
        Integer, ForeignKey("scan_patterns.id", ondelete="SET NULL"), nullable=True
    )
    health_score: float = Column(Float, nullable=False)
    health_delta: Optional[float] = Column(Float, nullable=True)
    conditions_snapshot: Optional[dict] = Column(JSONB, nullable=True)
    action: str = Column(String(30), nullable=False)
    old_stop: Optional[float] = Column(Float, nullable=True)
    new_stop: Optional[float] = Column(Float, nullable=True)
    old_target: Optional[float] = Column(Float, nullable=True)
    new_target: Optional[float] = Column(Float, nullable=True)
    llm_confidence: Optional[float] = Column(Float, nullable=True)
    llm_reasoning: Optional[str] = Column(Text, nullable=True)
    mechanical_action: Optional[str] = Column(String(30), nullable=True)
    mechanical_stop: Optional[float] = Column(Float, nullable=True)
    mechanical_target: Optional[float] = Column(Float, nullable=True)
    decision_source: Optional[str] = Column(String(20), nullable=True)
    price_at_decision: Optional[float] = Column(Float, nullable=True)
    price_after_1h: Optional[float] = Column(Float, nullable=True)
    price_after_4h: Optional[float] = Column(Float, nullable=True)
    was_beneficial: Optional[bool] = Column(Boolean, nullable=True)
    vitals_composite: Optional[float] = Column(Float, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class TickerVitals(Base):
    """Cached setup vitals per ticker/interval (trajectory scores from snapshot history)."""

    __tablename__ = "trading_ticker_vitals"
    __table_args__ = (Index("ix_ticker_vitals_computed", "computed_at"),)

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    ticker: str = Column(String(32), nullable=False)
    bar_interval: str = Column(String(16), nullable=False, default="1d")
    momentum_score: Optional[float] = Column(Float, nullable=True)
    volume_score: Optional[float] = Column(Float, nullable=True)
    trend_score: Optional[float] = Column(Float, nullable=True)
    overextension_risk: Optional[float] = Column(Float, nullable=True)
    composite_health: Optional[float] = Column(Float, nullable=True)
    trajectory_json: Optional[dict] = Column(JSONB, nullable=True)
    divergences_json: Optional[list] = Column(JSONB, nullable=True)
    computed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class SetupVitalsHistory(Base):
    """Per-trade vitals snapshots over time for degradation analysis and learning."""

    __tablename__ = "trading_setup_vitals_history"
    __table_args__ = (
        Index("ix_setup_vitals_hist_trade_created", "trade_id", "created_at"),
    )

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    trade_id: Optional[int] = Column(Integer, ForeignKey("trading_trades.id", ondelete="CASCADE"), nullable=True, index=True)
    breakout_alert_id: Optional[int] = Column(
        Integer, ForeignKey("trading_breakout_alerts.id", ondelete="SET NULL"), nullable=True
    )
    momentum_score: Optional[float] = Column(Float, nullable=True)
    volume_score: Optional[float] = Column(Float, nullable=True)
    trend_score: Optional[float] = Column(Float, nullable=True)
    overextension_risk: Optional[float] = Column(Float, nullable=True)
    composite_health: Optional[float] = Column(Float, nullable=True)
    price_at_check: Optional[float] = Column(Float, nullable=True)
    degradation_flags: Optional[dict] = Column(JSONB, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class TradingExecutionEvent(Base):
    """Normalized venue execution/order lifecycle audit across brokers."""

    __tablename__ = "trading_execution_events"
    __table_args__ = (
        Index("ix_trading_execution_events_trade_ts", "trade_id", "recorded_at"),
        Index("ix_trading_execution_events_order_ts", "broker_source", "order_id", "recorded_at"),
        Index("ix_trading_execution_events_pattern_ts", "scan_pattern_id", "recorded_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    trade_id: Optional[int] = Column(
        Integer, ForeignKey("trading_trades.id", ondelete="CASCADE"), nullable=True, index=True
    )
    proposal_id: Optional[int] = Column(
        Integer, ForeignKey("trading_proposals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    automation_session_id: Optional[int] = Column(
        Integer, ForeignKey("trading_automation_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scan_pattern_id: Optional[int] = Column(
        Integer, ForeignKey("scan_patterns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ticker: Optional[str] = Column(String(36), nullable=True, index=True)
    venue: Optional[str] = Column(String(32), nullable=True, index=True)
    execution_family: Optional[str] = Column(String(32), nullable=True, index=True)
    broker_source: Optional[str] = Column(String(32), nullable=True, index=True)
    order_id: Optional[str] = Column(String(128), nullable=True, index=True)
    client_order_id: Optional[str] = Column(String(128), nullable=True, index=True)
    product_id: Optional[str] = Column(String(64), nullable=True, index=True)
    event_type: str = Column(String(32), nullable=False, index=True)
    status: Optional[str] = Column(String(32), nullable=True, index=True)
    requested_quantity: Optional[float] = Column(Float, nullable=True)
    cumulative_filled_quantity: Optional[float] = Column(Float, nullable=True)
    last_fill_quantity: Optional[float] = Column(Float, nullable=True)
    average_fill_price: Optional[float] = Column(Float, nullable=True)
    submitted_at: Optional[datetime] = Column(DateTime, nullable=True)
    acknowledged_at: Optional[datetime] = Column(DateTime, nullable=True)
    first_fill_at: Optional[datetime] = Column(DateTime, nullable=True)
    last_fill_at: Optional[datetime] = Column(DateTime, nullable=True)
    event_at: Optional[datetime] = Column(DateTime, nullable=True)
    recorded_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    reference_price: Optional[float] = Column(Float, nullable=True)
    best_bid: Optional[float] = Column(Float, nullable=True)
    best_ask: Optional[float] = Column(Float, nullable=True)
    spread_bps: Optional[float] = Column(Float, nullable=True)
    expected_slippage_bps: Optional[float] = Column(Float, nullable=True)
    realized_slippage_bps: Optional[float] = Column(Float, nullable=True)
    submit_to_ack_ms: Optional[float] = Column(Float, nullable=True)
    ack_to_first_fill_ms: Optional[float] = Column(Float, nullable=True)
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})


class JournalEntry(Base):
    __tablename__ = "trading_journal"

    id: int = Column(Integer, primary_key=True, index=True)
    trade_id: Optional[int] = Column(
        Integer, ForeignKey("trading_trades.id", ondelete="CASCADE"), nullable=True, index=True
    )
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
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
    hypothesis_family: Optional[str] = Column(String(32), nullable=True)
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
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ticker: str = Column(String(20), nullable=False, index=True)
    score: float = Column(Float, nullable=False)  # 1-10 confluence score
    signal: str = Column(String(10), nullable=False)  # buy / sell / hold
    entry_price: Optional[float] = Column(Float, nullable=True)
    stop_loss: Optional[float] = Column(Float, nullable=True)
    take_profit: Optional[float] = Column(Float, nullable=True)
    risk_level: str = Column(String(10), nullable=False, default="medium")  # low / medium / high
    rationale: str = Column(Text, nullable=False, default="")
    indicator_data: Optional[dict] = Column(JSONB, nullable=True)
    scanned_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class BacktestParamSet(Base):
    """Deduplicated canonical backtest params / provenance JSON (hash-keyed).

    Rows in ``trading_backtests`` keep a shadow copy in ``params`` for compatibility;
    ``param_set_id`` references one shared payload when the canonical hash matches.
    """

    __tablename__ = "trading_backtest_param_sets"

    id: int = Column(Integer, primary_key=True, index=True)
    param_hash: str = Column(String(64), nullable=False, unique=True, index=True)
    params_json: dict = Column(JSONB, nullable=False)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class BrainValidationSliceLedger(Base):
    """Append-only usage of validation slices for repeatable-edge research (burn accounting).

    ``research_run_key`` dedupes accidental retries; ``slice_key`` aggregates actual evaluated context.
    """

    __tablename__ = "brain_validation_slice_ledger"
    __table_args__ = (
        UniqueConstraint("research_run_key", name="uq_bvsl_research_run_key"),
        Index("ix_bvsl_slice_key", "slice_key"),
        Index("ix_bvsl_scan_pattern_id", "scan_pattern_id"),
    )

    id: int = Column(Integer, primary_key=True, index=True)
    research_run_key: str = Column(String(64), nullable=False)
    slice_key: str = Column(String(64), nullable=False)
    scan_pattern_id: int = Column(
        Integer, ForeignKey("scan_patterns.id", ondelete="CASCADE"), nullable=False
    )
    rules_fingerprint: Optional[str] = Column(String(32), nullable=True)
    param_hash: Optional[str] = Column(String(64), nullable=True)
    recorded_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class BacktestResult(Base):
    """Stored backtest run results for strategy comparison."""
    __tablename__ = "trading_backtests"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    ticker: str = Column(String(20), nullable=False)
    strategy_name: str = Column(String(100), nullable=False)
    params: Optional[dict] = Column(JSONB, nullable=True)
    param_set_id: Optional[int] = Column(
        Integer,
        ForeignKey("trading_backtest_param_sets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    return_pct: float = Column(Float, nullable=False, default=0.0)
    win_rate: float = Column(Float, nullable=False, default=0.0)
    sharpe: Optional[float] = Column(Float, nullable=True)
    max_drawdown: float = Column(Float, nullable=False, default=0.0)
    trade_count: int = Column(Integer, nullable=False, default=0)
    equity_curve: Optional[list] = Column(JSONB, nullable=True)
    ran_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    related_insight_id: Optional[int] = Column(Integer, nullable=True, index=True)
    scan_pattern_id: Optional[int] = Column(Integer, nullable=True, index=True)
    # OOS walk-forward fields
    oos_win_rate: Optional[float] = Column(Float, nullable=True)
    oos_return_pct: Optional[float] = Column(Float, nullable=True)
    oos_trade_count: Optional[int] = Column(Integer, nullable=True)
    oos_holdout_fraction: Optional[float] = Column(Float, nullable=True)
    in_sample_bars: Optional[int] = Column(Integer, nullable=True)
    out_of_sample_bars: Optional[int] = Column(Integer, nullable=True)


class MarketSnapshot(Base):
    """Indicator snapshot keyed by completed OHLCV bar (ticker, bar_interval, bar_start_at)."""
    __tablename__ = "trading_snapshots"

    id: int = Column(Integer, primary_key=True, index=True)
    ticker: str = Column(String(20), nullable=False, index=True)
    snapshot_date: datetime = Column(DateTime, nullable=False, index=True)
    close_price: float = Column(Float, nullable=False)
    indicator_data: Optional[dict] = Column(JSONB, nullable=True)
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
    # Canonical bar identity (UTC). When set, upserts dedupe on (ticker, bar_interval, bar_start_at).
    bar_interval: Optional[str] = Column(String(16), nullable=True, index=True)
    bar_start_at: Optional[datetime] = Column(DateTime, nullable=True, index=True)
    snapshot_legacy: bool = Column(Boolean, nullable=False, default=True)


class TradingInsightEvidence(Base):
    """Credits one independent sample (bar) toward a TradingInsight — prevents double-counting."""
    __tablename__ = "trading_insight_evidence"

    id: int = Column(Integer, primary_key=True, index=True)
    insight_id: int = Column(
        Integer, ForeignKey("trading_insights.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    ticker: str = Column(String(20), nullable=False)
    bar_interval: str = Column(String(16), nullable=False)
    bar_start_utc: datetime = Column(DateTime, nullable=False)
    source: str = Column(String(24), nullable=False)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class LearningEvent(Base):
    """Tracks every AI learning action for the Brain dashboard."""
    __tablename__ = "trading_learning_events"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: str = Column(String(30), nullable=False)
    description: str = Column(Text, nullable=False)
    confidence_before: Optional[float] = Column(Float, nullable=True)
    confidence_after: Optional[float] = Column(Float, nullable=True)
    related_insight_id: Optional[int] = Column(
        Integer, ForeignKey("trading_insights.id", ondelete="SET NULL"), nullable=True
    )
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class AlertHistory(Base):
    """Log of all SMS/notification alerts sent to the user."""
    __tablename__ = "trading_alerts"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    alert_type: str = Column(String(30), nullable=False)
    ticker: str = Column(String(20), nullable=True)
    message: str = Column(Text, nullable=False)
    trade_type: Optional[str] = Column(String(30), nullable=True)
    duration_estimate: Optional[str] = Column(String(60), nullable=True)
    scan_pattern_id: Optional[int] = Column(
        Integer, ForeignKey("scan_patterns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    content_signature: Optional[str] = Column(String(512), nullable=True)
    sent_via: str = Column(String(20), nullable=False, default="email_gateway")
    success: bool = Column(Boolean, nullable=False, default=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class StopDecision(Base):
    """Audit log of every stop-engine evaluation / stop change per trade."""
    __tablename__ = "trading_stop_decisions"
    __table_args__ = (
        Index("ix_tsd_trade_ts", "trade_id", "as_of_ts"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    trade_id: int = Column(Integer, ForeignKey("trading_trades.id", ondelete="CASCADE"), nullable=False, index=True)
    as_of_ts: datetime = Column(DateTime, nullable=False, default=datetime.utcnow)
    state: str = Column(String(24), nullable=False)
    old_stop: Optional[float] = Column(Float, nullable=True)
    new_stop: Optional[float] = Column(Float, nullable=True)
    trigger: Optional[str] = Column(String(50), nullable=True)
    inputs_json: Optional[dict] = Column(JSONB, nullable=True, default=dict)
    reason: Optional[str] = Column(Text, nullable=True, default="")
    executed: bool = Column(Boolean, nullable=False, default=False)


class AlertDeliveryAttempt(Base):
    """Per-channel delivery attempt tracking with retry support."""
    __tablename__ = "trading_alert_delivery_attempts"
    __table_args__ = (
        Index("ix_tada_alert_status", "alert_id", "status"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    alert_id: int = Column(Integer, ForeignKey("trading_alerts.id", ondelete="CASCADE"), nullable=False, index=True)
    channel: str = Column(String(30), nullable=False)
    provider_msg_id: Optional[str] = Column(String(200), nullable=True)
    status: str = Column(String(20), nullable=False, default="queued")
    attempt_n: int = Column(Integer, nullable=False, default=1)
    next_retry_at: Optional[datetime] = Column(DateTime, nullable=True)
    last_error: Optional[str] = Column(Text, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class BreakoutAlert(Base):
    """Tracks each breakout alert with outcome data for the learning feedback loop."""
    __tablename__ = "trading_breakout_alerts"

    id: int = Column(Integer, primary_key=True, index=True)
    ticker: str = Column(String(20), nullable=False, index=True)
    asset_type: str = Column(String(10), nullable=False, default="crypto")
    alert_tier: str = Column(String(50), nullable=False)
    score_at_alert: float = Column(Float, nullable=False)
    indicator_snapshot: Optional[dict] = Column(JSONB, nullable=True)
    price_at_alert: float = Column(Float, nullable=False)
    entry_price: Optional[float] = Column(Float, nullable=True)
    stop_loss: Optional[float] = Column(Float, nullable=True)
    target_price: Optional[float] = Column(Float, nullable=True)
    signals_snapshot: Optional[dict] = Column(JSONB, nullable=True)
    alerted_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    price_1h: Optional[float] = Column(Float, nullable=True)
    price_4h: Optional[float] = Column(Float, nullable=True)
    price_24h: Optional[float] = Column(Float, nullable=True)
    breakout_occurred: Optional[bool] = Column(Boolean, nullable=True)
    max_gain_pct: Optional[float] = Column(Float, nullable=True)
    max_drawdown_pct: Optional[float] = Column(Float, nullable=True)
    outcome: str = Column(String(20), nullable=False, default="pending")
    outcome_checked_at: Optional[datetime] = Column(DateTime, nullable=True)
    outcome_notes: Optional[str] = Column(Text, nullable=True)

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

    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scan_pattern_id: Optional[int] = Column(
        Integer, ForeignKey("scan_patterns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    related_insight_id: Optional[int] = Column(
        Integer, ForeignKey("trading_insights.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trade_plan: Optional[dict] = Column(JSONB, nullable=True)
    trade_plan_mechanical: Optional[dict] = Column(JSONB, nullable=True)


class MonitorDecisionRule(Base):
    """Learned decision rule mapping a signal signature to an action.

    Populated by the learning cycle from PatternMonitorDecision outcomes.
    Used by the rules engine to make mechanical decisions without LLM.
    """
    __tablename__ = "trading_monitor_decision_rules"

    id: int = Column(Integer, primary_key=True, index=True)
    pattern_type: str = Column(String(120), nullable=False, index=True)
    signal_signature: str = Column(String(200), nullable=False, index=True)
    action: str = Column(String(30), nullable=False)
    stop_ratio: Optional[float] = Column(Float, nullable=True)
    target_ratio: Optional[float] = Column(Float, nullable=True)
    sample_count: int = Column(Integer, nullable=False, default=0)
    benefit_rate: float = Column(Float, nullable=False, default=0.0)
    llm_agreement_rate: float = Column(Float, nullable=False, default=0.0)
    graduation_status: str = Column(
        String(20), nullable=False, default="bootstrap",
    )
    rolling_benefit: Optional[dict] = Column(JSONB, nullable=True)
    updated_at: datetime = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False,
    )
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class MonitorPlanAccuracy(Base):
    """Tracks LLM vs mechanical trade-plan accuracy per pattern type.

    Each row represents a pattern type + complexity band.  The learning cycle
    updates counts after outcome scoring.
    """
    __tablename__ = "trading_monitor_plan_accuracy"

    id: int = Column(Integer, primary_key=True, index=True)
    pattern_type: str = Column(String(120), nullable=False, index=True)
    complexity_band: str = Column(String(20), nullable=False, default="simple")
    llm_correct_count: int = Column(Integer, nullable=False, default=0)
    mechanical_correct_count: int = Column(Integer, nullable=False, default=0)
    agreement_count: int = Column(Integer, nullable=False, default=0)
    total_count: int = Column(Integer, nullable=False, default=0)
    graduation_status: str = Column(
        String(20), nullable=False, default="bootstrap",
    )
    updated_at: datetime = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False,
    )
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class StrategyProposal(Base):
    """AI-generated trade proposal for user review and optional auto-execution."""
    __tablename__ = "trading_proposals"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ticker: str = Column(String(20), nullable=False, index=True)
    direction: str = Column(String(10), nullable=False, default="long")
    status: str = Column(String(20), nullable=False, default="pending")

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
    signals_json: Optional[dict] = Column(JSONB, nullable=True)
    indicator_json: Optional[dict] = Column(JSONB, nullable=True)

    brain_score: Optional[float] = Column(Float, nullable=True)
    ml_probability: Optional[float] = Column(Float, nullable=True)
    scan_score: Optional[float] = Column(Float, nullable=True)

    proposed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    reviewed_at: Optional[datetime] = Column(DateTime, nullable=True)
    executed_at: Optional[datetime] = Column(DateTime, nullable=True)
    expires_at: Optional[datetime] = Column(DateTime, nullable=True)

    broker_order_id: Optional[str] = Column(String(100), nullable=True)
    trade_id: Optional[int] = Column(
        Integer, ForeignKey("trading_trades.id", ondelete="SET NULL"), nullable=True
    )
    scan_pattern_id: Optional[int] = Column(
        Integer, ForeignKey("scan_patterns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    allocation_decision_json: dict = Column(JSONB, nullable=False, default=lambda: {})


class ScanPattern(Base):
    """Composable breakout/screener pattern — builtin, user-submitted, or brain-discovered."""
    __tablename__ = "scan_patterns"

    id: int = Column(Integer, primary_key=True, index=True)
    name: str = Column(String(120), nullable=False)
    description: Optional[str] = Column(Text, nullable=True)
    rules_json: dict = Column(JSONB, nullable=False, default=lambda: {})
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
    parent_id: Optional[int] = Column(
        Integer, ForeignKey("scan_patterns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    exit_config: Optional[dict] = Column(JSONB, nullable=True)
    variant_label: Optional[str] = Column(String(40), nullable=True)
    generation: int = Column(Integer, nullable=False, default=0)
    ticker_scope: str = Column(String(20), nullable=False, default="universal")
    scope_tickers: Optional[str] = Column(Text, nullable=True)
    trade_count: int = Column(Integer, nullable=False, default=0)
    backtest_priority: int = Column(Integer, nullable=False, default=0)
    last_backtest_at: Optional[datetime] = Column(DateTime, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Out-of-sample / promotion (see brain_oos_* settings and learning.py gates).
    # Deprecated: prefer lifecycle_stage; kept for backwards compatibility and reads.
    promotion_status: str = Column(String(32), nullable=False, default="legacy")
    oos_win_rate: Optional[float] = Column(Float, nullable=True)
    oos_avg_return_pct: Optional[float] = Column(Float, nullable=True)
    oos_trade_count: Optional[int] = Column(Integer, nullable=True)
    backtest_spread_used: Optional[float] = Column(Float, nullable=True)
    backtest_commission_used: Optional[float] = Column(Float, nullable=True)
    oos_evaluated_at: Optional[datetime] = Column(DateTime, nullable=True)
    bench_walk_forward_json: Optional[dict] = Column(JSONB, nullable=True)
    # Miner / evolution taxonomy: compression_expansion vs high_vol_regime (separate OOS gates).
    hypothesis_family: Optional[str] = Column(String(32), nullable=True)

    # Quant research: multi-holdout / bootstrap stats, two-tier queue (prescreen -> full), paper shadow book.
    oos_validation_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    queue_tier: str = Column(String(16), nullable=False, default="full")
    paper_book_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    # Regime affinity: win-rate / avg-return breakdown by market regime
    # e.g. {"risk_on": {"win_rate": 0.65, "n": 30}, "risk_off": {"win_rate": 0.40, "n": 12}}
    regime_affinity_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    # Lifecycle FSM: candidate -> backtested -> validated | challenged -> promoted -> live -> decayed -> retired
    # ``challenged`` = repeatable-edge research (edge_evidence); inspectable, not live-eligible (see governance).
    lifecycle_stage: str = Column(String(20), nullable=False, default="candidate")
    lifecycle_changed_at: Optional[datetime] = Column(DateTime, nullable=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    trading_insights = relationship("TradingInsight", back_populates="scan_pattern")


class PatternTradeRow(Base):
    """One simulated or historical pattern occurrence for trade-level analytics.

    See docs/PATTERN_TRADE_ANALYTICS.md for unit of observation and schema versions.
    """

    __tablename__ = "trading_pattern_trades"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(Integer, nullable=True, index=True)
    scan_pattern_id: Optional[int] = Column(
        Integer, ForeignKey("scan_patterns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    related_insight_id: Optional[int] = Column(
        Integer, ForeignKey("trading_insights.id", ondelete="SET NULL"), nullable=True, index=True
    )
    backtest_result_id: Optional[int] = Column(
        Integer, ForeignKey("trading_backtests.id", ondelete="SET NULL"), nullable=True, index=True
    )
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
    scan_pattern_id: int = Column(
        Integer, ForeignKey("scan_patterns.id", ondelete="SET NULL"), nullable=False, index=True
    )
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
    last_result_json: Optional[dict] = Column(JSONB, nullable=True)
    related_weight: Optional[str] = Column(String(80), nullable=True)
    related_pattern_id: Optional[int] = Column(Integer, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_tested_at: Optional[datetime] = Column(DateTime, nullable=True)


class PrescreenSnapshot(Base):
    """One row per scheduled prescreen run (external screens + internal brain signals)."""

    __tablename__ = "trading_prescreen_snapshots"

    id: int = Column(Integer, primary_key=True, index=True, autoincrement=True)
    run_id: str = Column(String(64), nullable=False, unique=True, index=True)
    run_started_at: datetime = Column(DateTime, nullable=False)
    run_finished_at: Optional[datetime] = Column(DateTime, nullable=True)
    timezone_label: str = Column(String(64), nullable=False, default="America/Los_Angeles")
    settings_json: Optional[dict] = Column(JSONB, nullable=True)
    status_json: Optional[dict] = Column(JSONB, nullable=True)
    source_map_json: Optional[dict] = Column(JSONB, nullable=True)
    inclusion_summary_json: Optional[dict] = Column(JSONB, nullable=True)
    candidate_count: int = Column(Integer, nullable=False, default=0)


class PrescreenCandidate(Base):
    """Durable prescreen universe: global (user_id NULL) or per-user extensions."""

    __tablename__ = "trading_prescreen_candidates"

    id: int = Column(Integer, primary_key=True, index=True, autoincrement=True)
    snapshot_id: Optional[int] = Column(
        Integer, ForeignKey("trading_prescreen_snapshots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_id: Optional[int] = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    ticker: str = Column(String(32), nullable=False)
    ticker_norm: str = Column(String(36), nullable=False, index=True)
    # crypto | stock — set on upsert from ticker convention (see massive_client.is_crypto)
    asset_universe: str = Column(String(16), nullable=False, default="stock")
    active: bool = Column(Boolean, nullable=False, default=True)
    first_seen_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    modified_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    entry_reasons: list = Column(JSONB, nullable=False, default=lambda: [])
    sources_json: Optional[dict] = Column(JSONB, nullable=True)


class PaperTrade(Base):
    """Simulated trade for paper-trading promoted patterns before going live."""

    __tablename__ = "trading_paper_trades"

    id: int = Column(Integer, primary_key=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scan_pattern_id: Optional[int] = Column(
        Integer, ForeignKey("scan_patterns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ticker: str = Column(String(32), nullable=False)
    direction: str = Column(String(8), nullable=False, default="long")
    entry_price: float = Column(Float, nullable=False)
    stop_price: Optional[float] = Column(Float, nullable=True)
    target_price: Optional[float] = Column(Float, nullable=True)
    quantity: int = Column(Integer, nullable=False, default=1)
    status: str = Column(String(16), nullable=False, default="open")  # open, closed, expired
    entry_date: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    exit_date: Optional[datetime] = Column(DateTime, nullable=True)
    exit_price: Optional[float] = Column(Float, nullable=True)
    exit_reason: Optional[str] = Column(String(32), nullable=True)  # stop, target, expired, manual
    pnl: Optional[float] = Column(Float, nullable=True)
    pnl_pct: Optional[float] = Column(Float, nullable=True)
    signal_json: Optional[dict] = Column(JSONB, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class BrainBatchJob(Base):
    """Audit row for a batch / scheduled brain job run (start/end, type, outcome)."""

    __tablename__ = "brain_batch_jobs"

    id: str = Column(String(36), primary_key=True)
    job_type: str = Column(String(64), nullable=False, index=True)
    status: str = Column(String(24), nullable=False, default="running")
    started_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    ended_at: Optional[datetime] = Column(DateTime, nullable=True)
    error_message: Optional[str] = Column(Text, nullable=True)
    meta_json: Optional[dict] = Column(JSONB, nullable=True)
    payload_json: Optional[dict] = Column(JSONB, nullable=True)
    user_id: Optional[int] = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class BrainGraphNode(Base):
    """Trading Brain neural mesh: static node definition (per domain / graph_version)."""

    __tablename__ = "brain_graph_nodes"

    id: str = Column(String(80), primary_key=True)
    domain: str = Column(String(32), nullable=False, default="trading", index=True)
    graph_version: int = Column(Integer, nullable=False, default=1)
    node_type: str = Column(String(64), nullable=False)
    layer: int = Column(Integer, nullable=False, index=True)
    label: str = Column(String(256), nullable=False)
    fire_threshold: float = Column(Float, nullable=False, default=0.55)
    cooldown_seconds: int = Column(Integer, nullable=False, default=120)
    enabled: bool = Column(Boolean, nullable=False, default=True)
    version: int = Column(Integer, nullable=False, default=1)
    is_observer: bool = Column(Boolean, nullable=False, default=False)
    display_meta: Optional[dict] = Column(JSONB, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class BrainGraphEdge(Base):
    """Directed edge between mesh nodes (excitatory / inhibitory, typed)."""

    __tablename__ = "brain_graph_edges"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    source_node_id: str = Column(String(80), ForeignKey("brain_graph_nodes.id", ondelete="CASCADE"), nullable=False)
    target_node_id: str = Column(String(80), ForeignKey("brain_graph_nodes.id", ondelete="CASCADE"), nullable=False)
    signal_type: str = Column(String(64), nullable=False, default="*")
    weight: float = Column(Float, nullable=False, default=1.0)
    polarity: str = Column(String(16), nullable=False, default="excitatory")
    edge_type: str = Column(String(32), nullable=False, default="dataflow")
    delay_ms: int = Column(Integer, nullable=False, default=0)
    decay_half_life_seconds: Optional[int] = Column(Integer, nullable=True)
    gate_config: Optional[dict] = Column(JSONB, nullable=True)
    min_confidence: float = Column(Float, nullable=False, default=0.0)
    min_source_confidence: float = Column(Float, nullable=False, default=0.0)
    enabled: bool = Column(Boolean, nullable=False, default=True)
    graph_version: int = Column(Integer, nullable=False, default=1)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class BrainNodeState(Base):
    """Runtime activation / confidence for a mesh node."""

    __tablename__ = "brain_node_states"

    node_id: str = Column(String(80), ForeignKey("brain_graph_nodes.id", ondelete="CASCADE"), primary_key=True)
    activation_score: float = Column(Float, nullable=False, default=0.0)
    confidence: float = Column(Float, nullable=False, default=0.5)
    local_state: Optional[dict] = Column(JSONB, nullable=True)
    last_fired_at: Optional[datetime] = Column(DateTime, nullable=True)
    last_activated_at: Optional[datetime] = Column(DateTime, nullable=True)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class BrainWorkEvent(Base):
    """Durable work / outcome ledger for event-first Trading Brain (not mesh activations)."""

    __tablename__ = "brain_work_events"

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    domain: str = Column(String(32), nullable=False, default="trading", index=True)
    event_type: str = Column(String(64), nullable=False, index=True)
    event_kind: str = Column(String(16), nullable=False, default="work")
    payload: Optional[dict] = Column(JSONB, nullable=True)
    dedupe_key: str = Column(String(256), nullable=False, index=True)
    lease_scope: str = Column(String(32), nullable=False, default="general", index=True)
    status: str = Column(String(16), nullable=False, default="pending", index=True)
    attempts: int = Column(Integer, nullable=False, default=0)
    max_attempts: int = Column(Integer, nullable=False, default=5)
    next_run_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    lease_holder: Optional[str] = Column(String(128), nullable=True)
    lease_expires_at: Optional[datetime] = Column(DateTime, nullable=True)
    last_error: Optional[str] = Column(Text, nullable=True)
    correlation_id: Optional[str] = Column(String(64), nullable=True, index=True)
    parent_event_id: Optional[int] = Column(
        BigInteger, ForeignKey("brain_work_events.id", ondelete="SET NULL"), nullable=True
    )
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    processed_at: Optional[datetime] = Column(DateTime, nullable=True)


class BrainActivationEvent(Base):
    """Postgres-backed activation queue row."""

    __tablename__ = "brain_activation_events"

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    source_node_id: Optional[str] = Column(
        String(80), ForeignKey("brain_graph_nodes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    cause: str = Column(String(128), nullable=False)
    payload: Optional[dict] = Column(JSONB, nullable=True)
    confidence_delta: float = Column(Float, nullable=False, default=0.0)
    propagation_depth: int = Column(Integer, nullable=False, default=0)
    correlation_id: Optional[str] = Column(String(64), nullable=True, index=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    processed_at: Optional[datetime] = Column(DateTime, nullable=True)
    status: str = Column(String(16), nullable=False, default="pending", index=True)


class BrainFireLog(Base):
    """Append-only log when a node fires."""

    __tablename__ = "brain_fire_log"

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    node_id: str = Column(String(80), ForeignKey("brain_graph_nodes.id", ondelete="CASCADE"), nullable=False, index=True)
    fired_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    activation_score: float = Column(Float, nullable=False, default=0.0)
    confidence: float = Column(Float, nullable=False, default=0.0)
    correlation_id: Optional[str] = Column(String(64), nullable=True)
    summary: Optional[str] = Column(Text, nullable=True)


class BrainGraphSnapshot(Base):
    """Optional full-graph JSON snapshots for audit / debug."""

    __tablename__ = "brain_graph_snapshots"

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    graph_version: int = Column(Integer, nullable=False)
    domain: str = Column(String(32), nullable=False)
    snapshot_json: dict = Column(JSONB, nullable=False)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class BrainGraphMetric(Base):
    """Keyed counters / gauges for mesh observability."""

    __tablename__ = "brain_graph_metrics"

    domain: str = Column(String(32), primary_key=True)
    graph_version: int = Column(Integer, primary_key=True)
    metric_key: str = Column(String(64), primary_key=True)
    value_num: float = Column(Float, nullable=False, default=0.0)
    extra: Optional[dict] = Column(JSONB, nullable=True)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class MomentumStrategyVariant(Base):
    """Versioned momentum strategy family row (neural momentum / automation; not learning-cycle)."""

    __tablename__ = "momentum_strategy_variants"
    __table_args__ = (
        UniqueConstraint("family", "variant_key", "version", name="uq_momentum_strategy_variant_fkv"),
    )

    id: int = Column(Integer, primary_key=True, index=True)
    family: str = Column(String(64), nullable=False, index=True)
    variant_key: str = Column(String(64), nullable=False, index=True)
    version: int = Column(Integer, nullable=False, default=1)
    label: str = Column(String(256), nullable=False)
    params_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    is_active: bool = Column(Boolean, nullable=False, default=True)
    execution_family: str = Column(String(32), nullable=False, default="coinbase_spot")
    parent_variant_id: Optional[int] = Column(
        Integer,
        ForeignKey("momentum_strategy_variants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    refinement_meta_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    scan_pattern_id: Optional[int] = Column(
        Integer, ForeignKey("scan_patterns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class MomentumSymbolViability(Base):
    """Durable symbol × variant viability snapshot (backing store; BrainNodeState stays hot path)."""

    __tablename__ = "momentum_symbol_viability"
    __table_args__ = (
        UniqueConstraint("symbol", "variant_id", name="uq_momentum_symbol_viability_sym_var"),
    )

    id: int = Column(Integer, primary_key=True, index=True)
    symbol: str = Column(String(36), nullable=False, index=True)
    scope: str = Column(String(16), nullable=False, default="symbol", index=True)
    variant_id: int = Column(
        Integer, ForeignKey("momentum_strategy_variants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    viability_score: float = Column(Float, nullable=False)
    paper_eligible: bool = Column(Boolean, nullable=False, default=True)
    live_eligible: bool = Column(Boolean, nullable=False, default=False)
    freshness_ts: datetime = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    regime_snapshot_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    execution_readiness_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    explain_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    evidence_window_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    source_node_id: Optional[str] = Column(String(80), nullable=True, index=True)
    correlation_id: Optional[str] = Column(String(64), nullable=True, index=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class TradingAutomationSession(Base):
    """Automation runner session (persistence only in Phase 2 — no runner logic here)."""

    __tablename__ = "trading_automation_sessions"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: Optional[int] = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    venue: str = Column(String(32), nullable=False, default="coinbase")
    execution_family: str = Column(String(32), nullable=False, default="coinbase_spot")
    mode: str = Column(String(16), nullable=False, default="paper")
    symbol: str = Column(String(36), nullable=False, index=True)
    variant_id: int = Column(
        Integer, ForeignKey("momentum_strategy_variants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    state: str = Column(String(32), nullable=False, default="idle")
    risk_snapshot_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    allocation_decision_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    correlation_id: Optional[str] = Column(String(64), nullable=True, index=True)
    source_node_id: Optional[str] = Column(String(80), nullable=True, index=True)
    source_paper_session_id: Optional[int] = Column(
        Integer, ForeignKey("trading_automation_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    started_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    ended_at: Optional[datetime] = Column(DateTime, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class TradingAutomationEvent(Base):
    """Append-only automation audit trail."""

    __tablename__ = "trading_automation_events"

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id: int = Column(
        Integer, ForeignKey("trading_automation_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ts: datetime = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    event_type: str = Column(String(64), nullable=False, index=True)
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    correlation_id: Optional[str] = Column(String(64), nullable=True, index=True)
    source_node_id: Optional[str] = Column(String(80), nullable=True, index=True)


class TradingAutomationRuntimeSnapshot(Base):
    """Current-state read model for the Autopilot runtime dashboard."""

    __tablename__ = "trading_automation_runtime_snapshots"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_trading_automation_runtime_snapshot_session"),
        Index("ix_tars_user_updated", "user_id", "updated_at"),
        Index("ix_tars_lane_state", "lane", "state"),
    )

    id: int = Column(Integer, primary_key=True, index=True)
    session_id: int = Column(
        Integer, ForeignKey("trading_automation_sessions.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    user_id: Optional[int] = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    symbol: str = Column(String(36), nullable=False, index=True)
    mode: str = Column(String(16), nullable=False, default="paper")
    lane: str = Column(String(24), nullable=False, default="simulation", index=True)
    state: str = Column(String(32), nullable=False, default="idle", index=True)
    strategy_family: Optional[str] = Column(String(64), nullable=True, index=True)
    strategy_label: Optional[str] = Column(String(256), nullable=True)
    thesis: Optional[str] = Column(Text, nullable=True)
    confidence: Optional[float] = Column(Float, nullable=True)
    conviction: Optional[float] = Column(Float, nullable=True)
    current_position_state: Optional[str] = Column(String(24), nullable=True)
    last_action: Optional[str] = Column(String(64), nullable=True)
    runtime_seconds: Optional[int] = Column(Integer, nullable=True)
    simulated_pnl_usd: Optional[float] = Column(Float, nullable=True)
    trade_count: int = Column(Integer, nullable=False, default=0)
    last_price: Optional[float] = Column(Float, nullable=True)
    execution_readiness_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    latest_levels_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    metrics_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False, index=True)


class TradingAutomationSessionBinding(Base):
    """Per-session market-data/provider truth binding for Autopilot."""

    __tablename__ = "trading_automation_session_bindings"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_trading_automation_session_binding_session"),
        Index("ix_tasb_truth_provider", "source_of_truth_provider", "source_of_truth_exchange"),
    )

    id: int = Column(Integer, primary_key=True, index=True)
    session_id: int = Column(
        Integer, ForeignKey("trading_automation_sessions.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    discovery_provider: Optional[str] = Column(String(32), nullable=True)
    chart_provider: Optional[str] = Column(String(32), nullable=True)
    signal_provider: Optional[str] = Column(String(32), nullable=True)
    source_of_truth_provider: Optional[str] = Column(String(32), nullable=True)
    source_of_truth_exchange: Optional[str] = Column(String(32), nullable=True)
    bar_builder: Optional[str] = Column(String(48), nullable=True)
    latency_class: Optional[str] = Column(String(48), nullable=True)
    simulation_fidelity: Optional[str] = Column(String(48), nullable=True)
    gating_reason: Optional[str] = Column(Text, nullable=True)
    meta_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False, index=True)


class TradingAutomationSimulatedFill(Base):
    """Append-only simulated order/fill audit for paper/runtime replay."""

    __tablename__ = "trading_automation_simulated_fills"
    __table_args__ = (
        Index("ix_tasf_session_ts", "session_id", "ts"),
        Index("ix_tasf_symbol_ts", "symbol", "ts"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id: int = Column(
        Integer, ForeignKey("trading_automation_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ts: datetime = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    symbol: str = Column(String(36), nullable=False, index=True)
    lane: str = Column(String(24), nullable=False, default="simulation")
    side: Optional[str] = Column(String(16), nullable=True)
    action: str = Column(String(32), nullable=False)
    fill_type: Optional[str] = Column(String(32), nullable=True)
    quantity: Optional[float] = Column(Float, nullable=True)
    price: Optional[float] = Column(Float, nullable=True)
    reference_price: Optional[float] = Column(Float, nullable=True)
    fees_usd: Optional[float] = Column(Float, nullable=True)
    pnl_usd: Optional[float] = Column(Float, nullable=True)
    position_state_before: Optional[str] = Column(String(24), nullable=True)
    position_state_after: Optional[str] = Column(String(24), nullable=True)
    reason: Optional[str] = Column(String(64), nullable=True)
    marker_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Links the first durable execution artifact for momentum autopilot (paper) to the decision ledger.
    # For rows in trading_trades, use TradingDecisionPacket.linked_trade_id instead.
    decision_packet_id: Optional[int] = Column(
        BigInteger, ForeignKey("trading_decision_packets.id", ondelete="SET NULL"), nullable=True, index=True
    )


class TradingDecisionPacket(Base):
    """Canonical persisted decision at entry/execution boundary (autopilot + brain).

    linked_trade_id references trading_trades only. Momentum paper/live entry fills link via
    trading_automation_simulated_fills.decision_packet_id (and live venue path may have no Trade row).
    """

    __tablename__ = "trading_decision_packets"
    __table_args__ = (
        Index("ix_tdp_user_created", "user_id", "created_at"),
        Index("ix_tdp_session_created", "automation_session_id", "created_at"),
        Index("ix_tdp_ticker_created", "chosen_ticker", "created_at"),
        Index("ix_tdp_pattern_created", "scan_pattern_id", "created_at"),
        Index("ix_tdp_mode_stage", "execution_mode", "deployment_stage"),
        Index("ix_tdp_outcome", "outcome_status", "created_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    user_id: Optional[int] = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    automation_session_id: Optional[int] = Column(
        Integer, ForeignKey("trading_automation_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scan_pattern_id: Optional[int] = Column(
        Integer, ForeignKey("scan_patterns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    chosen_ticker: Optional[str] = Column(String(36), nullable=True, index=True)
    decision_type: str = Column(String(24), nullable=False, default="trade")
    execution_mode: str = Column(String(16), nullable=False, default="paper")
    deployment_stage: str = Column(String(24), nullable=False, default="paper")
    regime_snapshot_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    allocator_input_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    allocator_output_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    portfolio_context_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    expected_edge_gross: Optional[float] = Column(Float, nullable=True)
    expected_edge_net: Optional[float] = Column(Float, nullable=True)
    expected_slippage_bps: Optional[float] = Column(Float, nullable=True)
    expected_fill_probability: Optional[float] = Column(Float, nullable=True)
    expected_partial_fill_probability: Optional[float] = Column(Float, nullable=True)
    expected_missed_fill_probability: Optional[float] = Column(Float, nullable=True)
    risk_budget_pct: Optional[float] = Column(Float, nullable=True)
    size_notional: Optional[float] = Column(Float, nullable=True)
    size_shares_or_qty: Optional[float] = Column(Float, nullable=True)
    abstain_reason_code: Optional[str] = Column(String(64), nullable=True)
    abstain_reason_text: Optional[str] = Column(Text, nullable=True)
    selected_candidate_rank: Optional[int] = Column(Integer, nullable=True)
    candidate_count: int = Column(Integer, nullable=False, default=0)
    capacity_blocked: bool = Column(Boolean, nullable=False, default=False)
    capacity_reason_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    correlation_penalty: Optional[float] = Column(Float, nullable=True)
    uncertainty_haircut: Optional[float] = Column(Float, nullable=True)
    execution_penalty: Optional[float] = Column(Float, nullable=True)
    final_score: Optional[float] = Column(Float, nullable=True)
    source_surface: str = Column(String(32), nullable=False, default="autopilot")
    research_vs_live_context_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    linked_trade_id: Optional[int] = Column(
        Integer, ForeignKey("trading_trades.id", ondelete="SET NULL"), nullable=True, index=True
    )
    outcome_status: str = Column(String(24), nullable=False, default="pending")
    shadow_advisory_only: bool = Column(Boolean, nullable=False, default=True)


class TradingDecisionCandidate(Base):
    __tablename__ = "trading_decision_candidates"
    __table_args__ = (Index("ix_tdc_packet_rank", "decision_packet_id", "rank"),)

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    decision_packet_id: int = Column(
        BigInteger, ForeignKey("trading_decision_packets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rank: int = Column(Integer, nullable=False, default=0)
    ticker: str = Column(String(36), nullable=False)
    scan_pattern_id: Optional[int] = Column(
        Integer, ForeignKey("scan_patterns.id", ondelete="SET NULL"), nullable=True
    )
    candidate_score_raw: Optional[float] = Column(Float, nullable=True)
    candidate_score_net: Optional[float] = Column(Float, nullable=True)
    expected_edge_gross: Optional[float] = Column(Float, nullable=True)
    expected_edge_net: Optional[float] = Column(Float, nullable=True)
    expected_slippage_bps: Optional[float] = Column(Float, nullable=True)
    expected_fill_probability: Optional[float] = Column(Float, nullable=True)
    size_cap_notional: Optional[float] = Column(Float, nullable=True)
    was_selected: bool = Column(Boolean, nullable=False, default=False)
    reject_reason_code: Optional[str] = Column(String(64), nullable=True)
    reject_reason_text: Optional[str] = Column(Text, nullable=True)
    reject_detail_json: dict = Column(JSONB, nullable=False, default=lambda: {})


class TradingDeploymentState(Base):
    __tablename__ = "trading_deployment_states"
    __table_args__ = (
        UniqueConstraint("scope_type", "scope_key", name="uq_trading_deployment_scope"),
        Index("ix_tds_user_stage", "user_id", "current_stage"),
    )

    id: int = Column(Integer, primary_key=True, index=True)
    scope_type: str = Column(String(32), nullable=False)
    scope_key: str = Column(String(256), nullable=False)
    user_id: Optional[int] = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    current_stage: str = Column(String(24), nullable=False, default="paper")
    promoted_at: Optional[datetime] = Column(DateTime, nullable=True)
    degraded_at: Optional[datetime] = Column(DateTime, nullable=True)
    disabled_at: Optional[datetime] = Column(DateTime, nullable=True)
    stage_metrics_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    live_trade_count: int = Column(Integer, nullable=False, default=0)
    paper_trade_count: int = Column(Integer, nullable=False, default=0)
    rolling_win_rate: Optional[float] = Column(Float, nullable=True)
    rolling_expectancy_net: Optional[float] = Column(Float, nullable=True)
    rolling_slippage_bps: Optional[float] = Column(Float, nullable=True)
    rolling_drawdown_pct: Optional[float] = Column(Float, nullable=True)
    rolling_missed_fill_rate: Optional[float] = Column(Float, nullable=True)
    rolling_partial_fill_rate: Optional[float] = Column(Float, nullable=True)
    last_reason_code: Optional[str] = Column(String(64), nullable=True)
    last_reason_text: Optional[str] = Column(Text, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class MomentumAutomationOutcome(Base):
    """Durable closed-loop outcome row for neural evolution (Phase 9); one row per automation session."""

    __tablename__ = "momentum_automation_outcomes"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_momentum_automation_outcome_session"),
        Index("ix_mao_variant_created", "variant_id", "created_at"),
        Index("ix_mao_symbol_mode_created", "symbol", "mode", "created_at"),
        Index("ix_mao_user_created", "user_id", "created_at"),
    )

    id: int = Column(Integer, primary_key=True, index=True)
    session_id: int = Column(
        Integer, ForeignKey("trading_automation_sessions.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    user_id: Optional[int] = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    variant_id: int = Column(
        Integer, ForeignKey("momentum_strategy_variants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    symbol: str = Column(String(36), nullable=False, index=True)
    mode: str = Column(String(16), nullable=False, index=True)
    execution_family: str = Column(String(32), nullable=False, default="coinbase_spot")
    terminal_state: str = Column(String(32), nullable=False)
    terminal_at: datetime = Column(DateTime, nullable=False, index=True)
    outcome_class: str = Column(String(48), nullable=False, index=True)
    realized_pnl_usd: Optional[float] = Column(Float, nullable=True)
    return_bps: Optional[float] = Column(Float, nullable=True)
    hold_seconds: Optional[int] = Column(Integer, nullable=True)
    exit_reason: Optional[str] = Column(String(64), nullable=True)
    regime_snapshot_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    entry_regime_snapshot_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    exit_regime_snapshot_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    readiness_snapshot_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    admission_snapshot_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    governance_context_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    extracted_summary_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    evidence_weight: float = Column(Float, nullable=False, default=1.0)
    contributes_to_evolution: bool = Column(Boolean, nullable=False, default=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class TradingGovernanceApproval(Base):
    """Persistent governance approval/rejection record for trading actions."""

    __tablename__ = "trading_governance_approvals"
    __table_args__ = (
        Index("ix_tga_status_submitted", "status", "submitted_at"),
        Index("ix_tga_action_status", "action_type", "status"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    action_type: str = Column(String(64), nullable=False)
    details_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    submitted_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    status: str = Column(String(24), nullable=False, default="pending")
    decision: Optional[str] = Column(String(24), nullable=True)
    decided_at: Optional[datetime] = Column(DateTime, nullable=True)
    notes: str = Column(Text, nullable=False, default="")
