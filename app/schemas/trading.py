"""Pydantic schemas for the Trading module API."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Watchlist ───────────────────────────────────────────────────────────

class WatchlistAdd(BaseModel):
    ticker: str = Field(..., max_length=20)


class WatchlistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticker: str
    added_at: datetime


# ── Trade journal ───────────────────────────────────────────────────────

class TradeCreate(BaseModel):
    ticker: str = Field(..., max_length=20)
    direction: str = Field("long", pattern=r"^(long|short)$")
    entry_price: float = Field(..., gt=0)
    quantity: float = Field(1.0, gt=0)
    entry_date: Optional[datetime] = None
    tags: Optional[str] = None
    notes: Optional[str] = None


class TradeClose(BaseModel):
    exit_price: float = Field(..., gt=0)
    exit_date: Optional[datetime] = None
    notes: Optional[str] = None


class TradeUpdate(BaseModel):
    tags: Optional[str] = None
    notes: Optional[str] = None


class TradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticker: str
    direction: str
    entry_price: float
    exit_price: Optional[float]
    quantity: float
    entry_date: datetime
    exit_date: Optional[datetime]
    status: str
    pnl: Optional[float]
    tags: Optional[str]
    notes: Optional[str]


# ── Journal entries ─────────────────────────────────────────────────────

class JournalCreate(BaseModel):
    trade_id: Optional[int] = None
    content: str = Field(..., min_length=1)


class JournalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trade_id: Optional[int]
    content: str
    indicator_snapshot: Optional[str]
    created_at: datetime


# ── AI analysis ─────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    ticker: str
    interval: str = "1d"
    message: Optional[str] = None
    history: Optional[list[dict]] = None


class InsightOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    pattern_description: str
    confidence: float
    evidence_count: int
    last_seen: datetime


# ── Backtest ────────────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    ticker: str
    strategy: str = "sma_cross"
    period: str = "1y"
    cash: float = Field(10000, gt=0)
    commission: float = Field(0.001, ge=0)


# ── Scanner ─────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    tickers: Optional[list[str]] = None


class SmartPickRequest(BaseModel):
    message: Optional[str] = None
    budget: Optional[float] = None
    risk_tolerance: str = Field("medium", pattern=r"^(low|medium|high)$")
