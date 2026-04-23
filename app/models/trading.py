from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
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
    management_scope: Optional[str] = Column(String(40), nullable=True, index=True)
    related_alert_id: Optional[int] = Column(
        Integer, ForeignKey("trading_breakout_alerts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scale_in_count: int = Column(Integer, nullable=False, default=0)
    auto_trader_version: Optional[str] = Column(String(32), nullable=True, index=True)
    pending_exit_order_id: Optional[str] = Column(String(100), nullable=True)
    pending_exit_status: Optional[str] = Column(String(30), nullable=True)
    pending_exit_requested_at: Optional[datetime] = Column(DateTime, nullable=True)
    pending_exit_reason: Optional[str] = Column(String(50), nullable=True)
    pending_exit_limit_price: Optional[float] = Column(Float, nullable=True)
    # Phase 2C: neural mesh correlation id for the entry signal. Plasticity uses
    # this to look up the activation path on close.
    mesh_entry_correlation_id: Optional[str] = Column(String(64), nullable=True, index=True)


class AutoTraderRun(Base):
    """Audit row for AutoTrader v1 decisions (pattern-imminent → gates → placement)."""

    __tablename__ = "trading_autotrader_runs"
    __table_args__ = (
        Index("ix_autotrader_runs_user_created", "user_id", "created_at"),
        Index("ix_autotrader_runs_breakout_alert", "breakout_alert_id"),
    )

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    user_id: Optional[int] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    breakout_alert_id: Optional[int] = Column(
        Integer, ForeignKey("trading_breakout_alerts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scan_pattern_id: Optional[int] = Column(
        Integer, ForeignKey("scan_patterns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ticker: str = Column(String(32), nullable=False, default="")
    decision: str = Column(String(24), nullable=False)  # placed | scaled_in | skipped | blocked | error
    reason: Optional[str] = Column(Text, nullable=True)
    rule_snapshot: Optional[dict] = Column(JSONB, nullable=True)
    llm_snapshot: Optional[dict] = Column(JSONB, nullable=True)
    management_scope: Optional[str] = Column(String(40), nullable=True, index=True)
    trade_id: Optional[int] = Column(
        Integer, ForeignKey("trading_trades.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


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
    # Q1.T2: HMM regime tag at bar (nullable when classifier off or no snapshot yet)
    regime: Optional[str] = Column(String(16), nullable=True, index=True)
    regime_posterior: Optional[dict] = Column(JSONB, nullable=True)


class RegimeSnapshot(Base):
    """Daily (or bar-timed) 3-state HMM regime decode for macro features (Q1.T2)."""

    __tablename__ = "regime_snapshot"
    __table_args__ = (Index("ix_regime_snapshot_model_version", "model_version", "as_of"),)

    as_of: datetime = Column(DateTime, primary_key=True)
    regime: str = Column(String(16), nullable=False)
    posterior: dict = Column(JSONB, nullable=False)
    features: dict = Column(JSONB, nullable=False)
    model_version: str = Column(String(128), nullable=False)


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

    # CPCV / DSR / PBO promotion evidence (Q1.T1); see app/services/trading/promotion_gate.py
    cpcv_n_paths: Optional[int] = Column(Integer, nullable=True)
    cpcv_median_sharpe: Optional[float] = Column(Float, nullable=True)
    cpcv_median_sharpe_by_regime: Optional[dict] = Column(JSONB, nullable=True)
    deflated_sharpe: Optional[float] = Column(Float, nullable=True)
    pbo: Optional[float] = Column(Float, nullable=True)
    n_effective_trials: Optional[int] = Column(Integer, nullable=True)
    promotion_gate_passed: Optional[bool] = Column(Boolean, nullable=True)
    promotion_gate_reasons: Optional[list] = Column(JSONB, nullable=True)

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


class BrainGraphEdgeMutation(Base):
    """Phase 2C: audit row for every edge weight / gate change.

    Source of truth for plasticity rollback. ``reason`` records what drove the
    change ('trade_outcome' | 'learning_cycle' | 'manual' | 'budget_capped').
    ``evidence_ref`` carries enough context (trade_id, pnl, correlation_id) to
    reconstruct the update after the fact.
    """

    __tablename__ = "brain_graph_edge_mutations"

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    edge_id: int = Column(
        Integer,
        ForeignKey("brain_graph_edges.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    old_weight: float = Column(Float, nullable=False)
    new_weight: float = Column(Float, nullable=False)
    old_min_source_confidence: Optional[float] = Column(Float, nullable=True)
    new_min_source_confidence: Optional[float] = Column(Float, nullable=True)
    reason: str = Column(String(40), nullable=False)
    evidence_ref: Optional[dict] = Column(JSONB, nullable=True)
    delta_source: str = Column(String(40), nullable=False)
    applied: bool = Column(Boolean, nullable=False, default=True)
    dry_run: bool = Column(Boolean, nullable=False, default=False)
    correlation_id: Optional[str] = Column(String(64), nullable=True, index=True)
    applied_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class BrainActivationPathLog(Base):
    """Phase 2C: per-hop record of propagation paths that terminated at an action node.

    Enables plasticity to trace which edges carried the signal for a trade's
    entry correlation_id and reinforce/attenuate those specific edges.
    """

    __tablename__ = "brain_activation_path_log"

    correlation_id: str = Column(String(64), primary_key=True)
    hop_idx: int = Column(Integer, primary_key=True)
    source_node_id: str = Column(String(80), nullable=False, index=True)
    target_node_id: str = Column(String(80), nullable=False, index=True)
    edge_id: Optional[int] = Column(
        Integer,
        ForeignKey("brain_graph_edges.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    activation_before: float = Column(Float, nullable=False, default=0.0)
    activation_after: float = Column(Float, nullable=False, default=0.0)
    confidence_at_hop: float = Column(Float, nullable=False, default=0.5)
    recorded_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


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


class NetEdgeScoreLog(Base):
    """Phase E: per-decision log of NetEdgeRanker score vs heuristic (shadow-safe).

    Rows are written on every entry/proposal decision when
    ``brain_net_edge_ranker_mode`` is not ``off``. Downstream consumers must
    not gate any trading action on these rows while ``mode != "authoritative"``.
    """

    __tablename__ = "trading_net_edge_scores"
    __table_args__ = (
        Index("ix_net_edge_scores_ticker_created", "ticker", "created_at"),
        Index("ix_net_edge_scores_pattern_created", "scan_pattern_id", "created_at"),
        Index("ix_net_edge_scores_regime_created", "regime", "created_at"),
        Index("ix_net_edge_scores_mode_created", "mode", "created_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    decision_id: str = Column(Text, nullable=False)
    scan_pattern_id: Optional[int] = Column(Integer, nullable=True)
    ticker: str = Column(String(32), nullable=False)
    asset_class: Optional[str] = Column(String(16), nullable=True)
    regime: Optional[str] = Column(String(32), nullable=True)
    ctx_hash: Optional[str] = Column(String(64), nullable=True)
    calibrated_prob: Optional[float] = Column(Float, nullable=True)
    expected_payoff: Optional[float] = Column(Float, nullable=True)
    spread_cost: Optional[float] = Column(Float, nullable=True)
    slippage_cost: Optional[float] = Column(Float, nullable=True)
    fees_cost: Optional[float] = Column(Float, nullable=True)
    miss_prob_cost: Optional[float] = Column(Float, nullable=True)
    partial_fill_cost: Optional[float] = Column(Float, nullable=True)
    expected_net_pnl: Optional[float] = Column(Float, nullable=True)
    heuristic_score: Optional[float] = Column(Float, nullable=True)
    disagree_flag: bool = Column(Boolean, nullable=False, default=False)
    mode: str = Column(String(16), nullable=False)
    provenance_json: Optional[dict] = Column(JSONB, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class NetEdgeCalibrationSnapshot(Base):
    """Phase E: periodic fit of the per-regime NetEdgeRanker calibrator.

    A snapshot row represents the state of one calibration head (per asset
    class and regime bucket). Only one row per (asset_class, regime) should
    have ``is_active=True``; promotion flips the active flag.
    """

    __tablename__ = "trading_net_edge_calibration_snapshots"
    __table_args__ = (
        Index("ix_net_edge_cal_regime_fitted", "regime", "fitted_at"),
        Index("ix_net_edge_cal_active", "is_active", "fitted_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    version_id: str = Column(String(64), nullable=False)
    asset_class: Optional[str] = Column(String(16), nullable=True)
    regime: Optional[str] = Column(String(32), nullable=True)
    method: str = Column(String(32), nullable=False)
    sample_count: int = Column(Integer, nullable=False, default=0)
    reliability_json: Optional[dict] = Column(JSONB, nullable=True)
    brier_score: Optional[float] = Column(Float, nullable=True)
    log_loss: Optional[float] = Column(Float, nullable=True)
    disagreement_rate: Optional[float] = Column(Float, nullable=True)
    params_json: Optional[dict] = Column(JSONB, nullable=True)
    fitted_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    is_active: bool = Column(Boolean, nullable=False, default=False)


class ExitParityLog(Base):
    """Phase B: per-bar parity record between legacy exit paths and the canonical ExitEvaluator.

    Rows are written whenever a legacy exit path runs while
    ``brain_exit_engine_mode`` is not ``off``. The canonical evaluator does
    not decide the trade in any non-``authoritative`` mode; its output is
    logged here to measure drift before cutover.

    ``source`` is one of ``"backtest"`` or ``"live"``. ``position_id`` is the
    ``PaperTrade.id`` / ``Trade.id`` for live; for backtest it is the synthetic
    row id emitted by the backtest adapter (or NULL if not tracked).
    ``config_hash`` is a stable hash of the ``ExitConfig`` used by the canonical
    evaluator so disagreements can be grouped by config flavor.
    """

    __tablename__ = "trading_exit_parity_log"
    __table_args__ = (
        Index("ix_exit_parity_source_created", "source", "created_at"),
        Index("ix_exit_parity_ticker_created", "ticker", "created_at"),
        Index("ix_exit_parity_mode_created", "mode", "created_at"),
        Index("ix_exit_parity_agree_created", "agree_bool", "created_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    source: str = Column(String(16), nullable=False)
    position_id: Optional[int] = Column(BigInteger, nullable=True)
    scan_pattern_id: Optional[int] = Column(Integer, nullable=True)
    ticker: str = Column(String(32), nullable=False)
    bar_ts: Optional[datetime] = Column(DateTime, nullable=True)
    legacy_action: str = Column(String(32), nullable=False)
    legacy_exit_price: Optional[float] = Column(Float, nullable=True)
    canonical_action: str = Column(String(32), nullable=False)
    canonical_exit_price: Optional[float] = Column(Float, nullable=True)
    pnl_diff_pct: Optional[float] = Column(Float, nullable=True)
    agree_bool: bool = Column(Boolean, nullable=False, default=False)
    mode: str = Column(String(16), nullable=False)
    config_hash: Optional[str] = Column(String(64), nullable=True)
    provenance_json: Optional[dict] = Column(JSONB, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class EconomicLedgerEvent(Base):
    """Phase A: append-only economic event in the canonical economic-truth ledger.

    One row per fill / fee / adjustment with explicit ``cash_delta`` (signed;
    positive = cash in) and ``realized_pnl_delta`` (zero for entries, signed
    PnL contribution for exits, net of fees when attributable).

    ``event_type`` is one of ``entry_fill``, ``exit_fill``, ``partial_fill``,
    ``fee``, ``adjustment``. ``source`` is ``paper``, ``live``, or
    ``broker_sync``. Exactly one of ``trade_id`` / ``paper_trade_id`` is set.

    Idempotency: partial unique indexes enforce at most one entry_fill + one
    exit_fill per ``(paper_trade_id)`` and per ``(trade_id)``. Duplicate
    writes are a no-op.

    Legacy ``Trade.pnl`` / ``PaperTrade.pnl`` remain authoritative until the
    Phase A cutover phase; the ledger is shadow-only when
    ``brain_economic_ledger_mode != 'authoritative'``.
    """

    __tablename__ = "trading_economic_ledger"
    __table_args__ = (
        Index("ix_economic_ledger_source_created", "source", "created_at"),
        Index("ix_economic_ledger_ticker_created", "ticker", "created_at"),
        Index("ix_economic_ledger_event_type_created", "event_type", "created_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    source: str = Column(String(16), nullable=False)
    trade_id: Optional[int] = Column(BigInteger, nullable=True)
    paper_trade_id: Optional[int] = Column(BigInteger, nullable=True)
    user_id: Optional[int] = Column(Integer, nullable=True)
    scan_pattern_id: Optional[int] = Column(Integer, nullable=True)
    ticker: str = Column(String(32), nullable=False)
    event_type: str = Column(String(32), nullable=False)
    direction: Optional[str] = Column(String(8), nullable=True)
    quantity: Optional[float] = Column(Float, nullable=True)
    price: Optional[float] = Column(Float, nullable=True)
    fee: float = Column(Float, nullable=False, default=0.0)
    cash_delta: float = Column(Float, nullable=False)
    realized_pnl_delta: float = Column(Float, nullable=False, default=0.0)
    position_qty_after: Optional[float] = Column(Float, nullable=True)
    position_cost_basis_after: Optional[float] = Column(Float, nullable=True)
    venue: Optional[str] = Column(String(32), nullable=True)
    broker_source: Optional[str] = Column(String(32), nullable=True)
    event_ts: Optional[datetime] = Column(DateTime, nullable=True)
    mode: str = Column(String(16), nullable=False)
    provenance_json: Optional[dict] = Column(JSONB, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class LedgerParityLog(Base):
    """Phase A: per-closed-trade reconciliation between ledger-PnL and legacy PnL.

    Rows are written whenever a paper or live trade closes while
    ``brain_economic_ledger_mode`` is not ``off``. ``legacy_pnl`` is
    ``PaperTrade.pnl`` or ``Trade.pnl``; ``ledger_pnl`` is the sum of
    ``realized_pnl_delta`` across matching ``EconomicLedgerEvent`` rows for
    the trade. ``agree_bool`` is true when
    ``|delta_pnl| <= tolerance_usd``.
    """

    __tablename__ = "trading_ledger_parity_log"
    __table_args__ = (
        Index("ix_ledger_parity_source_created", "source", "created_at"),
        Index("ix_ledger_parity_agree_created", "agree_bool", "created_at"),
        Index("ix_ledger_parity_ticker_created", "ticker", "created_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    source: str = Column(String(16), nullable=False)
    trade_id: Optional[int] = Column(BigInteger, nullable=True)
    paper_trade_id: Optional[int] = Column(BigInteger, nullable=True)
    user_id: Optional[int] = Column(Integer, nullable=True)
    scan_pattern_id: Optional[int] = Column(Integer, nullable=True)
    ticker: str = Column(String(32), nullable=False)
    legacy_pnl: Optional[float] = Column(Float, nullable=True)
    ledger_pnl: Optional[float] = Column(Float, nullable=True)
    delta_pnl: Optional[float] = Column(Float, nullable=True)
    delta_abs: Optional[float] = Column(Float, nullable=True)
    agree_bool: bool = Column(Boolean, nullable=False, default=False)
    tolerance_usd: Optional[float] = Column(Float, nullable=True)
    mode: str = Column(String(16), nullable=False)
    provenance_json: Optional[dict] = Column(JSONB, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class PitAuditLog(Base):
    """Phase C: per-audit record of a ScanPattern's condition fields classified
    as PIT / non_pit / unknown against the canonical ``pit_contract`` lists.

    Rows are written when ``brain_pit_audit_mode`` is not ``off`` and the
    learning-cycle shadow hook runs ``pit_audit.audit_active_patterns``.
    History is preserved; multiple passes per pattern are allowed.
    ``agree_bool`` is true when ``non_pit_count + unknown_count == 0``.
    """

    __tablename__ = "trading_pit_audit_log"
    __table_args__ = (
        Index("ix_pit_audit_pattern_created", "pattern_id", "created_at"),
        Index("ix_pit_audit_agree_created", "agree_bool", "created_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    pattern_id: int = Column(Integer, nullable=False)
    name: Optional[str] = Column(String(200), nullable=True)
    origin: Optional[str] = Column(String(32), nullable=True)
    lifecycle_stage: Optional[str] = Column(String(32), nullable=True)
    pit_count: int = Column(Integer, nullable=False)
    non_pit_count: int = Column(Integer, nullable=False)
    unknown_count: int = Column(Integer, nullable=False)
    pit_fields: list = Column(JSONB, nullable=False, default=lambda: [])
    non_pit_fields: list = Column(JSONB, nullable=False, default=lambda: [])
    unknown_fields: list = Column(JSONB, nullable=False, default=lambda: [])
    agree_bool: bool = Column(Boolean, nullable=False, default=False)
    mode: str = Column(String(16), nullable=False)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class UniverseSnapshot(Base):
    """Phase C: per-day, per-ticker historical universe record.

    Lets PIT audits and backtests answer "was ticker T in our tradable universe
    on date D?" without re-fetching from live sources. Writer helper
    ``universe_snapshot.record_snapshot`` is idempotent on
    ``(as_of_date, ticker)``. No automatic backfill this phase.
    """

    __tablename__ = "trading_universe_snapshots"
    __table_args__ = (
        UniqueConstraint("as_of_date", "ticker", name="uq_universe_snapshot_date_ticker"),
        Index("ix_universe_snapshot_date", "as_of_date"),
        Index("ix_universe_snapshot_ticker_date", "ticker", "as_of_date"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    as_of_date = Column(Date, nullable=False)
    ticker: str = Column(String(32), nullable=False)
    asset_class: str = Column(String(16), nullable=False)
    status: str = Column(String(16), nullable=False)
    primary_exchange: Optional[str] = Column(String(32), nullable=True)
    source: Optional[str] = Column(String(32), nullable=True)
    provenance_json: Optional[dict] = Column(JSONB, nullable=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class TripleBarrierLabelRow(Base):
    """Phase D: triple-barrier label for a (ticker, label_date, side, barrier)
    tuple.

    One row per unique configuration so the labeler is idempotent via the
    ``uq_triple_barrier_labels`` UNIQUE index. History is preserved by
    varying barrier configs (e.g. tp=1.5/sl=1.0 vs tp=2.0/sl=1.0).

    label:
        +1 = take-profit barrier hit (winner)
        -1 = stop-loss barrier hit (loser)
         0 = timeout / missing data

    Shadow-safe: ``mode`` records whether the write happened under
    ``off`` / ``shadow`` / ``authoritative``. Only ``shadow`` rows are
    emitted until explicit cutover.
    """

    __tablename__ = "trading_triple_barrier_labels"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "label_date", "side", "tp_pct", "sl_pct", "max_bars",
            name="uq_triple_barrier_labels",
        ),
        Index("ix_triple_barrier_label_date", "label_date"),
        Index("ix_triple_barrier_ticker_date", "ticker", "label_date"),
        Index("ix_triple_barrier_snapshot", "snapshot_id"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id: Optional[int] = Column(Integer, nullable=True)
    ticker: str = Column(String(32), nullable=False)
    label_date = Column(Date, nullable=False)
    side: str = Column(String(8), nullable=False)
    tp_pct: float = Column(Float, nullable=False)
    sl_pct: float = Column(Float, nullable=False)
    max_bars: int = Column(Integer, nullable=False)
    entry_close: float = Column(Float, nullable=False)
    tp_price: float = Column(Float, nullable=False)
    sl_price: float = Column(Float, nullable=False)
    label: int = Column(SmallInteger, nullable=False)
    barrier_hit: str = Column(String(16), nullable=False)
    exit_bar_idx: int = Column(Integer, nullable=False)
    realized_return_pct: float = Column(Float, nullable=False)
    mode: str = Column(String(16), nullable=False)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class ExecutionCostEstimate(Base):
    """Phase F: per-(ticker, side, window_days) rolling execution-cost estimate.

    Computed from closed ``Trade`` rows and their TCA slippage columns;
    materialised so NetEdgeRanker / backtests can look it up cheaply
    without recomputing from scratch every call. Idempotent writes via
    ``uq_execution_cost_estimates``.

    All cost columns are in basis points (bps) of notional.
    """

    __tablename__ = "trading_execution_cost_estimates"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "side", "window_days",
            name="uq_execution_cost_estimates",
        ),
        Index("ix_execution_cost_estimates_updated", "last_updated_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    ticker: str = Column(String(32), nullable=False)
    side: str = Column(String(8), nullable=False)
    window_days: int = Column(Integer, nullable=False)
    median_spread_bps: float = Column(Float, nullable=False)
    p90_spread_bps: float = Column(Float, nullable=False)
    median_slippage_bps: float = Column(Float, nullable=False)
    p90_slippage_bps: float = Column(Float, nullable=False)
    avg_daily_volume_usd: float = Column(Float, nullable=False)
    sample_trades: int = Column(Integer, nullable=False)
    last_updated_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class VenueTruthLog(Base):
    """Phase F: per-fill observation comparing expected vs realized costs.

    One row is written at trade-close time (paper or live) by the
    venue-truth hook. All bps values are signed: positive slippage means
    we paid worse than expected. Used by the diagnostics endpoint and the
    release-blocker script to detect structural mis-calibration between
    our cost model and the venue.
    """

    __tablename__ = "trading_venue_truth_log"
    __table_args__ = (
        Index("ix_venue_truth_log_created", "created_at"),
        Index("ix_venue_truth_log_ticker_created", "ticker", "created_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    trade_id: Optional[int] = Column(Integer, nullable=True)
    ticker: str = Column(String(32), nullable=False)
    side: str = Column(String(8), nullable=False)
    notional_usd: float = Column(Float, nullable=False)
    expected_spread_bps: Optional[float] = Column(Float, nullable=True)
    realized_spread_bps: Optional[float] = Column(Float, nullable=True)
    expected_slippage_bps: Optional[float] = Column(Float, nullable=True)
    realized_slippage_bps: Optional[float] = Column(Float, nullable=True)
    expected_cost_fraction: Optional[float] = Column(Float, nullable=True)
    realized_cost_fraction: Optional[float] = Column(Float, nullable=True)
    paper_bool: bool = Column(Boolean, nullable=False, default=True)
    mode: str = Column(String(16), nullable=False)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class BracketIntent(Base):
    """Phase G: persisted bracket (stop + target) intent for a live Trade.

    One row per Trade (``uq_bracket_intents_trade_id``). In shadow mode
    the broker child order ids stay NULL; only the intended prices and
    the ``intent_state`` move. When Phase G.2 flips to authoritative,
    ``broker_stop_order_id`` / ``broker_target_order_id`` are filled in
    and ``intent_state`` transitions to ``authoritative_submitted``.
    """

    __tablename__ = "trading_bracket_intents"
    __table_args__ = (
        UniqueConstraint("trade_id", name="uq_bracket_intents_trade_id"),
        Index("ix_bracket_intents_ticker_state", "ticker", "intent_state"),
        Index("ix_bracket_intents_updated_at", "updated_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    trade_id: int = Column(
        Integer,
        ForeignKey("trading_trades.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Optional[int] = Column(Integer, nullable=True)
    ticker: str = Column(String(32), nullable=False)
    direction: str = Column(String(8), nullable=False)
    quantity: float = Column(Float, nullable=False)
    entry_price: float = Column(Float, nullable=False)
    stop_price: Optional[float] = Column(Float, nullable=True)
    target_price: Optional[float] = Column(Float, nullable=True)
    stop_model: Optional[str] = Column(String(32), nullable=True)
    pattern_id: Optional[int] = Column(Integer, nullable=True)
    regime: Optional[str] = Column(String(32), nullable=True)
    intent_state: str = Column(String(32), nullable=False, default="intent")
    shadow_mode: bool = Column(Boolean, nullable=False, default=True)
    broker_source: Optional[str] = Column(String(32), nullable=True)
    broker_stop_order_id: Optional[str] = Column(String(128), nullable=True)
    broker_target_order_id: Optional[str] = Column(String(128), nullable=True)
    last_observed_at: Optional[datetime] = Column(DateTime, nullable=True)
    last_diff_reason: Optional[str] = Column(String(128), nullable=True)
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: datetime = Column(
        DateTime, default=datetime.utcnow, nullable=False,
    )


class BracketReconciliationLog(Base):
    """Phase G: append-only sweep log comparing local bracket state vs broker.

    A sweep writes at least one row per scanned ``Trade`` / bracket intent.
    ``kind='agree'`` means local and broker match within tolerances;
    other kinds capture specific drift modes. In shadow mode this table
    is the only place reconciliation output lands - no broker writes
    happen, no user-visible alerts fire from this signal yet.
    """

    __tablename__ = "trading_bracket_reconciliation_log"
    __table_args__ = (
        Index("ix_bracket_reconciliation_sweep", "sweep_id"),
        Index("ix_bracket_reconciliation_trade", "trade_id"),
        Index("ix_bracket_reconciliation_kind_ts", "kind", "observed_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    sweep_id: str = Column(String(64), nullable=False)
    trade_id: Optional[int] = Column(
        Integer,
        ForeignKey("trading_trades.id", ondelete="SET NULL"),
        nullable=True,
    )
    bracket_intent_id: Optional[int] = Column(
        BigInteger,
        ForeignKey("trading_bracket_intents.id", ondelete="SET NULL"),
        nullable=True,
    )
    ticker: Optional[str] = Column(String(32), nullable=True)
    broker_source: Optional[str] = Column(String(32), nullable=True)
    kind: str = Column(String(32), nullable=False)
    severity: str = Column(String(16), nullable=False)
    local_payload: dict = Column(JSONB, nullable=False, default=lambda: {})
    broker_payload: dict = Column(JSONB, nullable=False, default=lambda: {})
    delta_payload: dict = Column(JSONB, nullable=False, default=lambda: {})
    mode: str = Column(String(16), nullable=False)
    observed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class OrderStateLog(Base):
    """P1.1 — formal order state machine log.

    Append-only transition log keyed on the venue's ``broker_order_id``
    (or ``client_order_id`` when we haven't yet learned the broker id).
    Each row is a single transition from one canonical :class:`OrderState`
    to another, as produced by
    ``app.services.trading.venue.order_state_machine``.

    The canonical states are
    ``DRAFT → SUBMITTING → ACK → PARTIAL → FILLED | CANCELLED | REJECTED | EXPIRED``.
    Illegal transitions (e.g. ``FILLED → PARTIAL``) are suppressed by the
    writer and never land here — the invariant that enables clean
    latency/state metrics downstream (P1.2 venue health).
    """

    __tablename__ = "trading_order_state_log"
    __table_args__ = (
        Index("ix_order_state_log_order", "order_id", "recorded_at"),
        Index("ix_order_state_log_client", "client_order_id", "recorded_at"),
        Index("ix_order_state_log_venue_ts", "venue", "recorded_at"),
        Index("ix_order_state_log_to_state_ts", "to_state", "recorded_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    # One of order_id / client_order_id is always set; venue bookkeeping may
    # know only the client id until the adapter resolves the broker id.
    order_id: Optional[str] = Column(String(128), nullable=True)
    client_order_id: Optional[str] = Column(String(128), nullable=True)
    venue: str = Column(String(32), nullable=False)
    from_state: Optional[str] = Column(String(16), nullable=True)
    to_state: str = Column(String(16), nullable=False)
    # Where this transition observation came from:
    #   poll_loop | webhook | reconciler | submit | manual | test
    source: str = Column(String(32), nullable=False)
    broker_status: Optional[str] = Column(String(32), nullable=True)
    raw_payload: dict = Column(JSONB, nullable=False, default=lambda: {})
    recorded_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class PositionSizerLog(Base):
    """Phase H: append-only shadow log for the canonical position sizer.

    For each actionable pick (alert, paper/live runner, manual proposal,
    backtest), the canonical :mod:`app.services.trading.position_sizer_model`
    writes exactly one row here *in parallel* with the legacy sizer that
    actually chose the notional. The row captures:

      * the :class:`NetEdgeScore` inputs the canonical sizer consumed
        (calibrated_prob / payoff_fraction / cost_fraction /
        expected_net_pnl),
      * the sizer outputs (Kelly fractions, proposed notional /
        quantity / risk_pct),
      * which caps fired (correlation bucket, single-notional),
      * the legacy notional / quantity for divergence tracking, and
      * the ``mode`` under which the proposal was emitted
        (``off`` / ``shadow`` / ``compare`` / ``authoritative``).

    Shadow-safe: Phase H never changes legacy sizer return values.
    Authoritative cutover is Phase H.2.
    """

    __tablename__ = "trading_position_sizer_log"
    __table_args__ = (
        Index("ix_position_sizer_log_proposal", "proposal_id"),
        Index("ix_position_sizer_log_source_ts", "source", "observed_at"),
        Index("ix_position_sizer_log_ticker_ts", "ticker", "observed_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    proposal_id: str = Column(String(64), nullable=False)
    source: str = Column(String(32), nullable=False)
    ticker: str = Column(String(32), nullable=False)
    direction: str = Column(String(8), nullable=False)
    user_id: Optional[int] = Column(Integer, nullable=True)
    pattern_id: Optional[int] = Column(Integer, nullable=True)
    asset_class: Optional[str] = Column(String(16), nullable=True)
    regime: Optional[str] = Column(String(32), nullable=True)
    entry_price: float = Column(Float, nullable=False)
    stop_price: Optional[float] = Column(Float, nullable=True)
    target_price: Optional[float] = Column(Float, nullable=True)
    capital: Optional[float] = Column(Float, nullable=True)
    calibrated_prob: Optional[float] = Column(Float, nullable=True)
    payoff_fraction: Optional[float] = Column(Float, nullable=True)
    cost_fraction: Optional[float] = Column(Float, nullable=True)
    expected_net_pnl: Optional[float] = Column(Float, nullable=True)
    kelly_fraction: Optional[float] = Column(Float, nullable=True)
    kelly_scaled_fraction: Optional[float] = Column(Float, nullable=True)
    proposed_notional: Optional[float] = Column(Float, nullable=True)
    proposed_quantity: Optional[float] = Column(Float, nullable=True)
    proposed_risk_pct: Optional[float] = Column(Float, nullable=True)
    correlation_cap_triggered: bool = Column(
        Boolean, nullable=False, default=False,
    )
    correlation_bucket: Optional[str] = Column(String(64), nullable=True)
    max_bucket_notional: Optional[float] = Column(Float, nullable=True)
    notional_cap_triggered: bool = Column(
        Boolean, nullable=False, default=False,
    )
    legacy_notional: Optional[float] = Column(Float, nullable=True)
    legacy_quantity: Optional[float] = Column(Float, nullable=True)
    legacy_source: Optional[str] = Column(String(48), nullable=True)
    divergence_bps: Optional[float] = Column(Float, nullable=True)
    mode: str = Column(String(16), nullable=False)
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    observed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    risk_dial_multiplier: Optional[float] = Column(Float, nullable=True)
    pattern_regime_tilt_multiplier: Optional[float] = Column(Float, nullable=True)
    pattern_regime_tilt_reason: Optional[str] = Column(String(48), nullable=True)


class RiskDialState(Base):
    """Phase I: append-only log of risk-dial values.

    The risk dial is a scalar in ``[0.0, brain_risk_dial_ceiling]`` that
    modulates sizing aggressiveness. A new row is inserted each time
    the dial is *resolved* for a user (via
    :mod:`app.services.trading.risk_dial_service`). The **current**
    dial is the latest row per ``user_id`` ordered by
    ``observed_at DESC``; a ``NULL`` ``user_id`` means the global
    default.

    Phase I writes these rows under ``mode='shadow'`` only; the dial
    value is not yet consumed by
    :func:`position_sizer_model.compute_proposal`. Phase I.2 will
    promote it to authoritative and apply it inside ``compute_proposal``.
    """

    __tablename__ = "trading_risk_dial_state"
    __table_args__ = (
        Index("ix_risk_dial_user_ts", "user_id", "observed_at"),
        Index("ix_risk_dial_regime_ts", "regime", "observed_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Optional[int] = Column(Integer, nullable=True)
    dial_value: float = Column(Float, nullable=False)
    regime: Optional[str] = Column(String(32), nullable=True)
    source: str = Column(String(32), nullable=False)
    reason: Optional[str] = Column(String(256), nullable=True)
    mode: str = Column(String(16), nullable=False)
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    observed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class CapitalReweightLog(Base):
    """Phase I: append-only log of weekly capital re-weight sweeps.

    The weekly APScheduler job computes a proposed per-bucket
    allocation for each user and writes one row per
    ``(user_id, as_of_date)``. The row captures:

      * the proposed allocation list (``proposed_allocations_json``) -
        one entry per bucket with the target notional / weight + a
        short rationale string.
      * the current allocation list (``current_allocations_json``) -
        the same shape, based on the user's open trades.
      * ``drift_bucket_json`` mapping each bucket to ``drift_bps``
        (abs(target-current)/max(target,1) in bps).
      * ``cap_triggers_json`` flags which single-bucket caps fired.
      * ``mode`` under which the sweep was written.

    Shadow-safe: Phase I never resizes or closes an open position.
    Authoritative rebalance orders from this log are Phase I.2.
    """

    __tablename__ = "trading_capital_reweight_log"
    __table_args__ = (
        Index("ix_capital_reweight_user_date", "user_id", "as_of_date"),
        Index("ix_capital_reweight_id", "reweight_id"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    reweight_id: str = Column(String(64), nullable=False)
    user_id: Optional[int] = Column(Integer, nullable=True)
    as_of_date: datetime = Column(Date, nullable=False)
    regime: Optional[str] = Column(String(32), nullable=True)
    total_capital: float = Column(Float, nullable=False)
    proposed_allocations_json: list = Column(
        JSONB, nullable=False, default=lambda: [],
    )
    current_allocations_json: list = Column(
        JSONB, nullable=False, default=lambda: [],
    )
    drift_bucket_json: dict = Column(
        JSONB, nullable=False, default=lambda: {},
    )
    mean_drift_bps: Optional[float] = Column(Float, nullable=True)
    p90_drift_bps: Optional[float] = Column(Float, nullable=True)
    cap_triggers_json: dict = Column(
        JSONB, nullable=False, default=lambda: {},
    )
    mode: str = Column(String(16), nullable=False)
    observed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class PatternDriftLog(Base):
    """Phase J: append-only drift score log, one row per daily sweep.

    The drift monitor computes, per scan_pattern, a Brier-style
    calibration delta (``observed_win_prob - baseline_win_prob``) and
    a CUSUM-style cumulative sum statistic over the recent closed
    sample. The severity bucket (``green`` / ``yellow`` / ``red``)
    is a deterministic function of both statistics plus sample size.

    Shadow-safe: Phase J never calls ``lifecycle.transition`` or
    modifies ``scan_patterns``; this table is write-only in J.1.
    Authoritative gating on severity is Phase J.2.
    """

    __tablename__ = "trading_pattern_drift_log"
    __table_args__ = (
        Index("ix_pattern_drift_pattern_ts", "scan_pattern_id", "sweep_at"),
        Index("ix_pattern_drift_severity_ts", "severity", "sweep_at"),
        Index("ix_pattern_drift_id", "drift_id"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    drift_id: str = Column(String(64), nullable=False)
    scan_pattern_id: int = Column(Integer, nullable=False)
    pattern_name: Optional[str] = Column(String(256), nullable=True)
    baseline_win_prob: Optional[float] = Column(Float, nullable=True)
    observed_win_prob: Optional[float] = Column(Float, nullable=True)
    brier_delta: Optional[float] = Column(Float, nullable=True)
    cusum_statistic: Optional[float] = Column(Float, nullable=True)
    cusum_threshold: Optional[float] = Column(Float, nullable=True)
    sample_size: int = Column(Integer, nullable=False, default=0)
    severity: str = Column(String(16), nullable=False)
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    mode: str = Column(String(16), nullable=False)
    sweep_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    observed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class PatternRecertLog(Base):
    """Phase J: append-only re-certification proposal queue.

    When the drift monitor emits a ``red`` severity row, or a user
    manually queues a re-cert via the diagnostics surface, the
    re-cert queue service writes one row per
    ``(scan_pattern_id, as_of_date)``. ``status`` starts as
    ``proposed`` and is updated by Phase J.2 consumers (none in J.1).
    Shadow-safe: J.1 never triggers backtests or lifecycle
    transitions from these rows.
    """

    __tablename__ = "trading_pattern_recert_log"
    __table_args__ = (
        Index("ix_pattern_recert_pattern_ts", "scan_pattern_id", "observed_at"),
        Index("ix_pattern_recert_status_ts", "status", "observed_at"),
        Index("ix_pattern_recert_id", "recert_id"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    recert_id: str = Column(String(64), nullable=False)
    scan_pattern_id: int = Column(Integer, nullable=False)
    pattern_name: Optional[str] = Column(String(256), nullable=True)
    as_of_date: datetime = Column(Date, nullable=False)
    source: str = Column(String(32), nullable=False)
    severity: Optional[str] = Column(String(16), nullable=True)
    status: str = Column(String(32), nullable=False, default="proposed")
    reason: Optional[str] = Column(String(256), nullable=True)
    drift_log_id: Optional[int] = Column(BigInteger, nullable=True)
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    mode: str = Column(String(16), nullable=False)
    observed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class PatternDivergenceLog(Base):
    """Phase K: append-only per-pattern divergence panel.

    One row per pattern per daily sweep. Aggregates divergence signals
    already persisted by Phase A/B/F/G/H tables into a per-layer
    severity + hysteresis overall severity so operators can spot
    patterns that drift across multiple substrate layers
    simultaneously. Shadow-only: K.1 never mutates lifecycle state or
    writes to ``scan_patterns``.
    """

    __tablename__ = "trading_pattern_divergence_log"
    __table_args__ = (
        Index("ix_pattern_divergence_pattern_ts", "scan_pattern_id", "sweep_at"),
        Index("ix_pattern_divergence_severity_ts", "severity", "sweep_at"),
        Index("ix_pattern_divergence_id", "divergence_id"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    divergence_id: str = Column(String(64), nullable=False)
    scan_pattern_id: int = Column(Integer, nullable=False)
    pattern_name: Optional[str] = Column(String(256), nullable=True)
    as_of_date: datetime = Column(Date, nullable=False)
    ledger_severity: Optional[str] = Column(String(16), nullable=True)
    exit_severity: Optional[str] = Column(String(16), nullable=True)
    venue_severity: Optional[str] = Column(String(16), nullable=True)
    bracket_severity: Optional[str] = Column(String(16), nullable=True)
    sizer_severity: Optional[str] = Column(String(16), nullable=True)
    severity: str = Column(String(16), nullable=False)
    score: float = Column(Float, nullable=False, default=0.0)
    layers_sampled: int = Column(Integer, nullable=False, default=0)
    layers_agreed: int = Column(Integer, nullable=False, default=0)
    layers_total: int = Column(Integer, nullable=False, default=5)
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    mode: str = Column(String(16), nullable=False)
    sweep_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    observed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class MacroRegimeSnapshot(Base):
    """Phase L.17: append-only macro regime snapshot.

    One row per daily sweep of the extended macro regime surface
    (rates/credit/USD on top of existing SPY/VIX composite). Shadow-only
    in L.17.1: no existing regime consumer reads from this table and
    ``market_data.get_market_regime()`` is not modified. Downstream
    authoritative consumption is deferred to L.17.2.
    """

    __tablename__ = "trading_macro_regime_snapshots"
    __table_args__ = (
        Index("ix_macro_regime_as_of", "as_of_date"),
        Index("ix_macro_regime_id", "regime_id"),
        Index("ix_macro_regime_label_computed", "macro_label", "computed_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    regime_id: str = Column(String(64), nullable=False)
    as_of_date: datetime = Column(Date, nullable=False)

    # equity block - mirrors the existing get_market_regime() output
    spy_direction: Optional[str] = Column(String(16), nullable=True)
    spy_momentum_5d: Optional[float] = Column(Float, nullable=True)
    vix: Optional[float] = Column(Float, nullable=True)
    vix_regime: Optional[str] = Column(String(16), nullable=True)
    volatility_percentile: Optional[float] = Column(Float, nullable=True)
    composite: Optional[str] = Column(String(16), nullable=True)
    regime_numeric: Optional[int] = Column(Integer, nullable=True)

    # rates block
    ief_trend: Optional[str] = Column(String(16), nullable=True)
    shy_trend: Optional[str] = Column(String(16), nullable=True)
    tlt_trend: Optional[str] = Column(String(16), nullable=True)
    yield_curve_slope_proxy: Optional[float] = Column(Float, nullable=True)
    rates_regime: Optional[str] = Column(String(16), nullable=True)

    # credit block
    hyg_trend: Optional[str] = Column(String(16), nullable=True)
    lqd_trend: Optional[str] = Column(String(16), nullable=True)
    credit_spread_proxy: Optional[float] = Column(Float, nullable=True)
    credit_regime: Optional[str] = Column(String(16), nullable=True)

    # usd block
    uup_trend: Optional[str] = Column(String(16), nullable=True)
    uup_momentum_20d: Optional[float] = Column(Float, nullable=True)
    usd_regime: Optional[str] = Column(String(16), nullable=True)

    # composite macro
    macro_numeric: int = Column(Integer, nullable=False, default=0)
    macro_label: str = Column(String(32), nullable=False)

    # coverage
    symbols_sampled: int = Column(Integer, nullable=False, default=0)
    symbols_missing: int = Column(Integer, nullable=False, default=0)
    coverage_score: float = Column(Float, nullable=False, default=0.0)

    # raw per-symbol readings + config echoes
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    mode: str = Column(String(16), nullable=False)
    computed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    observed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class BreadthRelstrSnapshot(Base):
    """Phase L.18: append-only breadth + cross-sectional relative-strength
    snapshot.

    One row per daily sweep. Captures:
      * ETF-basket advance/decline proxy across 11 US sector SPDRs plus
        SPY/QQQ/IWM benchmarks;
      * per-sector trend + momentum + relative strength vs SPY
        (serialized as JSONB in ``sector_json`` to avoid 30+ flat
        columns);
      * benchmark trends (SPY/QQQ/IWM) and size/style tilts
        (IWM-vs-SPY, QQQ-vs-SPY 20d RS);
      * composite breadth label in
        {broad_risk_on, mixed, broad_risk_off} + leader / laggard
        sector.

    Shadow-only in L.18.1: no existing consumer reads from this table,
    ``market_data.get_market_regime()`` is unchanged, and Phase L.17's
    ``trading_macro_regime_snapshots`` is untouched. Downstream
    authoritative consumption is deferred to L.18.2.
    """

    __tablename__ = "trading_breadth_relstr_snapshots"
    __table_args__ = (
        Index("ix_breadth_relstr_as_of", "as_of_date"),
        Index("ix_breadth_relstr_id", "snapshot_id"),
        Index("ix_breadth_relstr_label_computed", "breadth_label", "computed_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id: str = Column(String(64), nullable=False)
    as_of_date: datetime = Column(Date, nullable=False)

    # breadth block
    members_sampled: int = Column(Integer, nullable=False, default=0)
    members_advancing: int = Column(Integer, nullable=False, default=0)
    members_declining: int = Column(Integer, nullable=False, default=0)
    members_flat: int = Column(Integer, nullable=False, default=0)
    advance_ratio: float = Column(Float, nullable=False, default=0.0)
    new_highs_count: int = Column(Integer, nullable=False, default=0)
    new_lows_count: int = Column(Integer, nullable=False, default=0)

    # sector block (JSONB: {sector: {trend, momentum_20d, rs_vs_spy_20d}})
    sector_json: dict = Column(JSONB, nullable=False, default=lambda: {})

    # benchmark block
    spy_trend: Optional[str] = Column(String(16), nullable=True)
    spy_momentum_20d: Optional[float] = Column(Float, nullable=True)
    qqq_trend: Optional[str] = Column(String(16), nullable=True)
    qqq_momentum_20d: Optional[float] = Column(Float, nullable=True)
    iwm_trend: Optional[str] = Column(String(16), nullable=True)
    iwm_momentum_20d: Optional[float] = Column(Float, nullable=True)

    # tilt block
    size_tilt: Optional[float] = Column(Float, nullable=True)
    style_tilt: Optional[float] = Column(Float, nullable=True)

    # composite block
    breadth_numeric: int = Column(Integer, nullable=False, default=0)
    breadth_label: str = Column(String(32), nullable=False)
    leader_sector: Optional[str] = Column(String(32), nullable=True)
    laggard_sector: Optional[str] = Column(String(32), nullable=True)

    # coverage block
    symbols_sampled: int = Column(Integer, nullable=False, default=0)
    symbols_missing: int = Column(Integer, nullable=False, default=0)
    coverage_score: float = Column(Float, nullable=False, default=0.0)

    # raw per-symbol readings + config echoes
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    mode: str = Column(String(16), nullable=False)
    computed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    observed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class CrossAssetSnapshot(Base):
    """Phase L.19: append-only cross-asset lead/lag snapshot.

    One row per daily sweep. Captures cross-asset lead/lag features:
      * bond-vs-equity lead (TLT 5d/20d vs SPY 5d/20d);
      * credit-vs-equity lead (HYG-LQD spread change vs SPY);
      * USD-vs-crypto lead (UUP 5d/20d vs BTC-USD 5d/20d);
      * VIX shock vs breadth divergence (VIX percentile vs advance
        ratio from L.18);
      * rolling BTC-SPY beta + correlation (window configurable).

    Shadow-only in L.19.1: no existing consumer reads from this table;
    Phase L.17's ``trading_macro_regime_snapshots`` and Phase L.18's
    ``trading_breadth_relstr_snapshots`` are not mutated;
    ``market_data.get_market_regime()`` is unchanged. Downstream
    authoritative consumption is deferred to L.19.2.
    """

    __tablename__ = "trading_cross_asset_snapshots"
    __table_args__ = (
        Index("ix_cross_asset_as_of", "as_of_date"),
        Index("ix_cross_asset_id", "snapshot_id"),
        Index(
            "ix_cross_asset_label_computed",
            "cross_asset_label",
            "computed_at",
        ),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id: str = Column(String(64), nullable=False)
    as_of_date: datetime = Column(Date, nullable=False)

    # bond vs equity lead (TLT vs SPY)
    bond_equity_lead_5d: Optional[float] = Column(Float, nullable=True)
    bond_equity_lead_20d: Optional[float] = Column(Float, nullable=True)
    bond_equity_label: Optional[str] = Column(String(32), nullable=True)

    # credit vs equity lead (HYG-LQD spread change vs SPY)
    credit_equity_lead_5d: Optional[float] = Column(Float, nullable=True)
    credit_equity_lead_20d: Optional[float] = Column(Float, nullable=True)
    credit_equity_label: Optional[str] = Column(String(32), nullable=True)

    # USD vs crypto lead (UUP vs BTC-USD)
    usd_crypto_lead_5d: Optional[float] = Column(Float, nullable=True)
    usd_crypto_lead_20d: Optional[float] = Column(Float, nullable=True)
    usd_crypto_label: Optional[str] = Column(String(32), nullable=True)

    # VIX shock vs breadth divergence
    vix_level: Optional[float] = Column(Float, nullable=True)
    vix_percentile: Optional[float] = Column(Float, nullable=True)
    breadth_advance_ratio: Optional[float] = Column(Float, nullable=True)
    vix_breadth_divergence_score: Optional[float] = Column(
        Float, nullable=True
    )
    vix_breadth_label: Optional[str] = Column(String(32), nullable=True)

    # BTC-SPY rolling beta + correlation
    crypto_equity_beta: Optional[float] = Column(Float, nullable=True)
    crypto_equity_beta_window_days: Optional[int] = Column(
        Integer, nullable=True
    )
    crypto_equity_correlation: Optional[float] = Column(Float, nullable=True)

    # composite block
    cross_asset_numeric: int = Column(Integer, nullable=False, default=0)
    cross_asset_label: str = Column(String(32), nullable=False)

    # coverage block
    symbols_sampled: int = Column(Integer, nullable=False, default=0)
    symbols_missing: int = Column(Integer, nullable=False, default=0)
    coverage_score: float = Column(Float, nullable=False, default=0.0)

    # raw per-symbol readings + macro/breadth context echo + config
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    mode: str = Column(String(16), nullable=False)
    computed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    observed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class TickerRegimeSnapshot(Base):
    """Phase L.20: append-only per-ticker mean-reversion vs trend snapshot.

    One row per ``(ticker, as_of_date)`` sweep. Captures pure
    time-series regime features computed from daily OHLCV:
      * lag-1 return autocorrelation ``ac1``;
      * variance-ratio ``vr_5`` / ``vr_20`` (Lo-MacKinlay style proxy);
      * Hurst exponent ``hurst`` via rescaled-range (R/S);
      * ADX-style trend-strength proxy ``adx_proxy``;
      * realised volatility ``sigma_20d``.

    Plus a composite label in
    ``{trend_up, trend_down, mean_revert, choppy, neutral}``.

    Shadow-only in L.20.1: no existing consumer reads from this table;
    L.17/L.18/L.19 snapshots and ``market_data.get_market_regime()``
    are not mutated; ``hurst_proxy_from_closes`` in the
    momentum-neural pipeline is untouched. Downstream authoritative
    consumption (regime-aware pattern promotion bias, regime-aware
    stop policy, regime-aware sizing) is deferred to L.20.2.
    """

    __tablename__ = "trading_ticker_regime_snapshots"
    __table_args__ = (
        Index("ix_ticker_regime_as_of", "as_of_date"),
        Index("ix_ticker_regime_id", "snapshot_id"),
        Index("ix_ticker_regime_ticker_as_of", "ticker", "as_of_date"),
        Index(
            "ix_ticker_regime_label_computed",
            "ticker_regime_label",
            "computed_at",
        ),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id: str = Column(String(64), nullable=False)
    as_of_date: datetime = Column(Date, nullable=False)
    ticker: str = Column(String(32), nullable=False)
    asset_class: Optional[str] = Column(String(16), nullable=True)

    # raw features
    last_close: Optional[float] = Column(Float, nullable=True)
    sigma_20d: Optional[float] = Column(Float, nullable=True)
    ac1: Optional[float] = Column(Float, nullable=True)
    vr_5: Optional[float] = Column(Float, nullable=True)
    vr_20: Optional[float] = Column(Float, nullable=True)
    hurst: Optional[float] = Column(Float, nullable=True)
    adx_proxy: Optional[float] = Column(Float, nullable=True)

    # composite scores + label
    trend_score: Optional[float] = Column(Float, nullable=True)
    mean_revert_score: Optional[float] = Column(Float, nullable=True)
    ticker_regime_numeric: int = Column(Integer, nullable=False, default=0)
    ticker_regime_label: str = Column(String(32), nullable=False)

    # coverage block
    bars_used: int = Column(Integer, nullable=False, default=0)
    bars_missing: int = Column(Integer, nullable=False, default=0)
    coverage_score: float = Column(Float, nullable=False, default=0.0)

    # raw readings + config echo
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    mode: str = Column(String(16), nullable=False)
    computed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    observed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class VolatilityDispersionSnapshot(Base):
    """Phase L.21: append-only daily market-wide volatility term
    structure + cross-sectional dispersion snapshot.

    One row per ``as_of_date`` sweep. Captures:
      * VIX term structure — ``vixy_close`` (1M), ``vixm_close`` (4M),
        ``vxz_close`` (4-7M) + slopes (``vix_slope_4m_1m``,
        ``vix_slope_7m_1m``).
      * SPY realised vol (annualised) over 5d / 20d / 60d windows
        and the implied-realised gap (``vix_realized_gap``).
      * Cross-sectional dispersion — standard deviation of ticker-level
        daily returns averaged over 5d / 20d windows
        (``cross_section_return_std_5d`` / ``_20d``).
      * Mean absolute pairwise correlation over 20d
        (``mean_abs_corr_20d``) with ``corr_sample_size`` samples.
      * Sector-leadership churn — Spearman ``1 - rho^2`` between
        20d-return ranks today and 20d ago for 11 sector SPDRs
        (``sector_leadership_churn_20d``).

    Composite labels (shadow-only):
      * ``vol_regime_label`` ∈ ``{vol_compressed, vol_normal,
        vol_expanded, vol_spike}``;
      * ``dispersion_label`` ∈ ``{dispersion_low, dispersion_normal,
        dispersion_high}``;
      * ``correlation_label`` ∈ ``{correlation_low,
        correlation_normal, correlation_spike}``.

    Shadow-only in L.21.1: no existing consumer reads from this table;
    L.17/L.18/L.19/L.20 snapshots and
    ``market_data.get_market_regime()`` are not mutated. Authoritative
    consumption (dispersion-aware pattern promotion bias,
    vol-regime-aware sizing / stop policy) deferred to L.21.2.
    """

    __tablename__ = "trading_vol_dispersion_snapshots"
    __table_args__ = (
        Index("ix_vol_dispersion_as_of", "as_of_date"),
        Index("ix_vol_dispersion_id", "snapshot_id"),
        Index(
            "ix_vol_dispersion_vol_label",
            "vol_regime_label",
            "computed_at",
        ),
        Index(
            "ix_vol_dispersion_disp_label",
            "dispersion_label",
            "computed_at",
        ),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id: str = Column(String(64), nullable=False)
    as_of_date: datetime = Column(Date, nullable=False)

    # VIX term structure
    vixy_close: Optional[float] = Column(Float, nullable=True)
    vixm_close: Optional[float] = Column(Float, nullable=True)
    vxz_close: Optional[float] = Column(Float, nullable=True)
    vix_slope_4m_1m: Optional[float] = Column(Float, nullable=True)
    vix_slope_7m_1m: Optional[float] = Column(Float, nullable=True)

    # SPY realised vol (annualised) + implied-realised gap
    spy_realized_vol_5d: Optional[float] = Column(Float, nullable=True)
    spy_realized_vol_20d: Optional[float] = Column(Float, nullable=True)
    spy_realized_vol_60d: Optional[float] = Column(Float, nullable=True)
    vix_realized_gap: Optional[float] = Column(Float, nullable=True)

    # cross-sectional dispersion + correlation
    cross_section_return_std_5d: Optional[float] = Column(Float, nullable=True)
    cross_section_return_std_20d: Optional[float] = Column(Float, nullable=True)
    mean_abs_corr_20d: Optional[float] = Column(Float, nullable=True)
    corr_sample_size: int = Column(Integer, nullable=False, default=0)

    # sector leadership churn (Spearman 1 - rho^2)
    sector_leadership_churn_20d: Optional[float] = Column(Float, nullable=True)

    # composite labels
    vol_regime_numeric: int = Column(Integer, nullable=False, default=0)
    vol_regime_label: str = Column(String(32), nullable=False)
    dispersion_numeric: int = Column(Integer, nullable=False, default=0)
    dispersion_label: str = Column(String(32), nullable=False)
    correlation_numeric: int = Column(Integer, nullable=False, default=0)
    correlation_label: str = Column(String(32), nullable=False)

    # coverage block
    universe_size: int = Column(Integer, nullable=False, default=0)
    tickers_missing: int = Column(Integer, nullable=False, default=0)
    coverage_score: float = Column(Float, nullable=False, default=0.0)

    # raw readings + config echo
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    mode: str = Column(String(16), nullable=False)
    computed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    observed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class IntradaySessionSnapshot(Base):
    """Phase L.22: append-only daily intraday session regime snapshot.

    One row per ``as_of_date`` derived from SPY 5-minute bars. Captures
    how the US equity session unfolded intraday — opening-range (OR),
    midday compression, power-hour dynamics, gap-open magnitude, and a
    composite ``session_label`` classifying the day.

    Composite labels (shadow-only):
      * ``session_trending_up`` / ``session_trending_down`` — strong
        directional close
      * ``session_range_bound`` — OR dominates the day
      * ``session_reversal`` — midday compression + late-day push
        away from OR midpoint
      * ``session_gap_and_go`` — large gap, close in gap direction
      * ``session_gap_fade`` — large gap, close against gap direction
      * ``session_compressed`` — tight OR and tight session range
      * ``session_neutral`` — insufficient bars / degenerate data

    Shadow-only in L.22.1: no existing consumer reads from this table;
    L.17–L.21 snapshots and ``get_market_regime()`` are not mutated.
    Authoritative consumption (session-aware entry timing, size tilt,
    post-gap filters) deferred to L.22.2.
    """

    __tablename__ = "trading_intraday_session_snapshots"
    __table_args__ = (
        Index("ix_intraday_session_as_of", "as_of_date"),
        Index("ix_intraday_session_id", "snapshot_id"),
        Index(
            "ix_intraday_session_label",
            "session_label",
            "computed_at",
        ),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id: str = Column(String(64), nullable=False)
    as_of_date: datetime = Column(Date, nullable=False)
    source_symbol: str = Column(String(16), nullable=False, default="SPY")

    # session anchors
    open_price: Optional[float] = Column(Float, nullable=True)
    close_price: Optional[float] = Column(Float, nullable=True)
    session_high: Optional[float] = Column(Float, nullable=True)
    session_low: Optional[float] = Column(Float, nullable=True)
    session_range_pct: Optional[float] = Column(Float, nullable=True)

    # gap features
    prev_close: Optional[float] = Column(Float, nullable=True)
    gap_open: Optional[float] = Column(Float, nullable=True)
    gap_open_pct: Optional[float] = Column(Float, nullable=True)

    # opening range
    or_high: Optional[float] = Column(Float, nullable=True)
    or_low: Optional[float] = Column(Float, nullable=True)
    or_range_pct: Optional[float] = Column(Float, nullable=True)
    or_volume_ratio: Optional[float] = Column(Float, nullable=True)

    # midday window
    midday_range_pct: Optional[float] = Column(Float, nullable=True)
    midday_compression_ratio: Optional[float] = Column(Float, nullable=True)

    # power hour
    ph_range_pct: Optional[float] = Column(Float, nullable=True)
    ph_volume_ratio: Optional[float] = Column(Float, nullable=True)
    close_vs_or_mid_pct: Optional[float] = Column(Float, nullable=True)

    # intraday realised vol (annualised)
    intraday_rv: Optional[float] = Column(Float, nullable=True)

    # composite label
    session_numeric: int = Column(Integer, nullable=False, default=0)
    session_label: str = Column(String(32), nullable=False)

    # coverage block
    bars_observed: int = Column(Integer, nullable=False, default=0)
    coverage_score: float = Column(Float, nullable=False, default=0.0)

    # raw readings + config echo
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    mode: str = Column(String(16), nullable=False)
    computed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)
    observed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class PatternRegimePerformanceDaily(Base):
    """Phase M.1: per-pattern per-regime performance aggregate ledger.

    Append-only daily aggregate joining closed paper-trade outcomes
    (`trading_paper_trades` where `status='closed'`) against the most
    recent L.17 - L.22 regime snapshot at each trade's entry date.

    Each row represents one 1-D slice:
      ``(pattern_id, regime_dimension, regime_label)``

    Regime dimensions (8 total, 1-D slicing only in M.1):
      * ``macro_regime``      ← ``trading_macro_regime_snapshots.regime_label``
      * ``breadth_label``     ← ``trading_breadth_relstr_snapshots.breadth_composite_label``
      * ``cross_asset_label`` ← ``trading_cross_asset_snapshots.composite_label``
      * ``ticker_regime``     ← ``trading_ticker_regime_snapshots.regime_label`` (keyed by trade's ticker)
      * ``vol_regime``        ← ``trading_vol_dispersion_snapshots.vol_regime_label``
      * ``dispersion_label``  ← ``trading_vol_dispersion_snapshots.dispersion_label``
      * ``correlation_label`` ← ``trading_vol_dispersion_snapshots.correlation_label``
      * ``session_label``     ← ``trading_intraday_session_snapshots.session_label``

    Trades with no snapshot at or before the entry date contribute
    to the pseudo-label ``regime_unavailable`` under that dimension
    — explicit, not a drop.

    Shadow-only in M.1: no downstream consumer (scanner, promotion,
    sizing, alerts, NetEdgeRanker) reads this table. Authoritative
    consumption deferred to M.2 behind governance + parity window.
    """

    __tablename__ = "trading_pattern_regime_performance_daily"
    __table_args__ = (
        Index(
            "ix_pattern_regime_perf_as_of",
            "as_of_date",
        ),
        Index(
            "ix_pattern_regime_perf_lookup",
            "pattern_id",
            "regime_dimension",
            "regime_label",
            "as_of_date",
        ),
        Index(
            "ix_pattern_regime_perf_run",
            "ledger_run_id",
        ),
        Index(
            "ix_pattern_regime_perf_confident",
            "pattern_id",
            "regime_dimension",
            postgresql_where=text("has_confidence"),
        ),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    ledger_run_id: str = Column(String(64), nullable=False)
    as_of_date: datetime = Column(Date, nullable=False)
    window_days: int = Column(Integer, nullable=False, default=90)
    pattern_id: int = Column(Integer, nullable=False)
    regime_dimension: str = Column(String(32), nullable=False)
    regime_label: str = Column(String(48), nullable=False)

    # aggregate outcomes
    n_trades: int = Column(Integer, nullable=False, default=0)
    n_wins: int = Column(Integer, nullable=False, default=0)
    hit_rate: Optional[float] = Column(Float, nullable=True)
    mean_pnl_pct: Optional[float] = Column(Float, nullable=True)
    median_pnl_pct: Optional[float] = Column(Float, nullable=True)
    sum_pnl: Optional[float] = Column(Float, nullable=True)
    expectancy: Optional[float] = Column(Float, nullable=True)
    mean_win_pct: Optional[float] = Column(Float, nullable=True)
    mean_loss_pct: Optional[float] = Column(Float, nullable=True)
    profit_factor: Optional[float] = Column(Float, nullable=True)
    sharpe_proxy: Optional[float] = Column(Float, nullable=True)
    avg_hold_days: Optional[float] = Column(Float, nullable=True)

    # confidence gate (True iff n_trades >= min_trades_per_cell)
    has_confidence: bool = Column(Boolean, nullable=False, default=False)

    # raw config echo + forensic metadata
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    mode: str = Column(String(16), nullable=False)
    computed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class PatternRegimeTiltLog(Base):
    """Phase M.2.a: append-only decision log for per-call pattern x
    regime sizing tilt proposals.

    One row per ``emit_shadow_proposal`` call where the tilt slice is
    at least ``shadow``. Rows record both the baseline (non-tilted)
    and proposed notionals so ``compare`` dashboards can track drift
    without committing to a size change.
    """

    __tablename__ = "trading_pattern_regime_tilt_log"
    __table_args__ = (
        Index("ix_pr_tilt_as_of", "as_of_date"),
        Index("ix_pr_tilt_pattern", "pattern_id", "as_of_date"),
        Index(
            "ix_pr_tilt_auth",
            "pattern_id",
            "as_of_date",
            postgresql_where=text("mode = 'authoritative'"),
        ),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    evaluation_id: str = Column(String(32), nullable=False)
    as_of_date: datetime = Column(Date, nullable=False)
    pattern_id: int = Column(Integer, nullable=False)
    ticker: Optional[str] = Column(String(32), nullable=True)
    source: Optional[str] = Column(String(48), nullable=True)
    mode: str = Column(String(16), nullable=False)
    applied: bool = Column(Boolean, nullable=False, default=False)
    baseline_size_dollars: Optional[float] = Column(Float, nullable=True)
    consumer_size_dollars: Optional[float] = Column(Float, nullable=True)
    multiplier: float = Column(Float, nullable=False)
    reason_code: str = Column(String(48), nullable=False)
    diff_category: Optional[str] = Column(String(16), nullable=True)
    contributing_dimensions: dict = Column(JSONB, nullable=False, default=lambda: {})
    n_confident_dimensions: int = Column(Integer, nullable=False, default=0)
    fallback_used: bool = Column(Boolean, nullable=False, default=False)
    context_hash: Optional[str] = Column(String(16), nullable=True)
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    computed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class PatternRegimePromotionLog(Base):
    """Phase M.2.b: append-only decision log for pattern x regime
    promotion-gate evaluations.

    One row per ``governance.request_pattern_to_live`` (and supporting
    hook) call where the promotion slice is at least ``shadow``. The
    row captures whether the baseline would have allowed promotion,
    whether the regime-aware gate would allow it, and which dimensions
    (if any) block it.
    """

    __tablename__ = "trading_pattern_regime_promotion_log"
    __table_args__ = (
        Index("ix_pr_prom_as_of", "as_of_date"),
        Index("ix_pr_prom_pattern", "pattern_id", "as_of_date"),
        Index(
            "ix_pr_prom_auth",
            "pattern_id",
            "as_of_date",
            postgresql_where=text("mode = 'authoritative'"),
        ),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    evaluation_id: str = Column(String(32), nullable=False)
    as_of_date: datetime = Column(Date, nullable=False)
    pattern_id: int = Column(Integer, nullable=False)
    mode: str = Column(String(16), nullable=False)
    applied: bool = Column(Boolean, nullable=False, default=False)
    baseline_allow: Optional[bool] = Column(Boolean, nullable=True)
    consumer_allow: bool = Column(Boolean, nullable=False)
    reason_code: str = Column(String(48), nullable=False)
    diff_category: Optional[str] = Column(String(16), nullable=True)
    blocking_dimensions: dict = Column(JSONB, nullable=False, default=lambda: {})
    n_confident_dimensions: int = Column(Integer, nullable=False, default=0)
    fallback_used: bool = Column(Boolean, nullable=False, default=False)
    source: Optional[str] = Column(String(48), nullable=True)
    context_hash: Optional[str] = Column(String(16), nullable=True)
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    computed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class PatternRegimeKillSwitchLog(Base):
    """Phase M.2.c: append-only decision log for daily pattern x regime
    kill-switch evaluations.

    One row per (pattern, evaluation-day) tuple where the kill-switch
    slice is at least ``shadow``. Consecutive-day negative-expectancy
    tracking is implicit in ``consecutive_days_negative``; quarantine
    fires when that counter reaches the configured threshold.
    """

    __tablename__ = "trading_pattern_regime_killswitch_log"
    __table_args__ = (
        Index("ix_pr_kill_as_of", "as_of_date"),
        Index("ix_pr_kill_pattern", "pattern_id", "as_of_date"),
        Index(
            "ix_pr_kill_auth",
            "pattern_id",
            "as_of_date",
            postgresql_where=text("mode = 'authoritative'"),
        ),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    evaluation_id: str = Column(String(32), nullable=False)
    as_of_date: datetime = Column(Date, nullable=False)
    pattern_id: int = Column(Integer, nullable=False)
    mode: str = Column(String(16), nullable=False)
    applied: bool = Column(Boolean, nullable=False, default=False)
    baseline_status: Optional[str] = Column(String(24), nullable=True)
    consumer_quarantine: bool = Column(Boolean, nullable=False)
    reason_code: str = Column(String(48), nullable=False)
    diff_category: Optional[str] = Column(String(16), nullable=True)
    consecutive_days_negative: int = Column(Integer, nullable=False, default=0)
    worst_dimension: Optional[str] = Column(String(32), nullable=True)
    worst_expectancy: Optional[float] = Column(Float, nullable=True)
    n_confident_dimensions: int = Column(Integer, nullable=False, default=0)
    fallback_used: bool = Column(Boolean, nullable=False, default=False)
    context_hash: Optional[str] = Column(String(16), nullable=True)
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    computed_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


class BrainRuntimeMode(Base):
    """Phase M.2-autopilot: per-slice runtime mode override.

    Single row per ``slice_name`` (e.g. ``pattern_regime_tilt``).
    Consulted by each slice's ``_raw_mode()`` helper BEFORE falling
    back to the ``settings.brain_pattern_regime_*_mode`` env values.

    Absence of a row for a slice means "use env default". Autopilot
    writes rows here to advance / revert modes without touching
    ``.env`` or recreating services.
    """

    __tablename__ = "trading_brain_runtime_modes"
    __table_args__ = (
        Index("ix_brain_runtime_modes_updated", "updated_at"),
    )

    slice_name: str = Column(String(64), primary_key=True)
    mode: str = Column(String(16), nullable=False)
    updated_at: datetime = Column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_by: str = Column(
        String(64), nullable=False, default="unknown"
    )
    reason: Optional[str] = Column(String(200), nullable=True)
    payload_json: dict = Column(JSONB, nullable=False, default=lambda: {})


class PatternRegimeAutopilotLog(Base):
    """Phase M.2-autopilot: append-only audit trail of autopilot
    decisions.

    One row per (slice, evaluation_tick) where an event occurred:
    ``autopilot_advance``, ``autopilot_hold``, ``autopilot_revert``,
    ``autopilot_weekly_summary``, ``autopilot_gate_fail``,
    ``autopilot_killswitch_disabled``.

    ``gates_json`` and ``evidence_json`` capture the full gate
    evaluation payload for forensic analysis.
    """

    __tablename__ = "trading_pattern_regime_autopilot_log"
    __table_args__ = (
        Index("ix_pr_autopilot_as_of", "as_of_date"),
        Index(
            "ix_pr_autopilot_slice_event",
            "slice_name",
            "event",
            "as_of_date",
        ),
        Index("ix_pr_autopilot_evaluated", "evaluated_at"),
    )

    id: int = Column(BigInteger, primary_key=True, autoincrement=True)
    as_of_date: datetime = Column(Date, nullable=False)
    evaluated_at: datetime = Column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    slice_name: str = Column(String(64), nullable=False)
    event: str = Column(String(32), nullable=False)
    from_mode: Optional[str] = Column(String(16), nullable=True)
    to_mode: Optional[str] = Column(String(16), nullable=True)
    reason_code: str = Column(String(64), nullable=False)
    gates_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    evidence_json: dict = Column(JSONB, nullable=False, default=lambda: {})
    approval_id: Optional[int] = Column(Integer, nullable=True)
    days_in_stage: Optional[int] = Column(Integer, nullable=True)
    ops_log_excerpt: Optional[str] = Column(Text, nullable=True)
