from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text

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
    pattern_description: str = Column(Text, nullable=False)
    confidence: float = Column(Float, nullable=False, default=0.5)
    evidence_count: int = Column(Integer, nullable=False, default=1)
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
    sent_via: str = Column(String(20), nullable=False, default="email_gateway")
    success: bool = Column(Boolean, nullable=False, default=True)
    created_at: datetime = Column(DateTime, default=datetime.utcnow, nullable=False)


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
