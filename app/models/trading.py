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
    status: str = Column(String(10), nullable=False, default="open")  # open / closed
    pnl: Optional[float] = Column(Float, nullable=True)
    tags: Optional[str] = Column(String(500), nullable=True)
    notes: Optional[str] = Column(Text, nullable=True)
    indicator_snapshot: Optional[str] = Column(Text, nullable=True)  # JSON blob


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
