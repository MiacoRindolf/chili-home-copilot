"""Pydantic schemas for the Trading module API."""
from datetime import datetime
from typing import Any, Optional

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
    # Optional arrival/mid reference for exit TCA (defaults to live quote or fill)
    reference_exit_price: Optional[float] = Field(None, gt=0)


class TradeSell(BaseModel):
    quantity: float = Field(..., gt=0)
    limit_price: Optional[float] = Field(None, gt=0)


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
    interval: str = "1d"
    cash: float = Field(10000, gt=0)
    commission: float = Field(0.001, ge=0)
    # Optional overrides for built-in strategies (e.g. fast/slow for SMA crossover)
    strategy_params: dict[str, Any] | None = None


class PatternBacktestRequest(BaseModel):
    """Optional JSON body for POST /api/trading/patterns/{id}/backtest."""

    ticker: Optional[str] = None
    period: Optional[str] = None
    interval: Optional[str] = None
    cash: Optional[float] = Field(None, gt=0)
    commission: Optional[float] = Field(None, ge=0)
    spread: Optional[float] = Field(None, ge=0, description="Bid/ask+slippage proxy (fraction of price); default from settings")
    oos_holdout_fraction: Optional[float] = Field(
        None, ge=0.05, le=0.45,
        description=(
            "If set, last fraction is evaluated as OOS; headline return/WR/OHLC use the full window, "
            "in_sample holds prefix stats for research gates"
        ),
    )
    # Full replacement for the pattern's stored rules_json (must be valid JSON string)
    rules_json_override: Optional[str] = None
    # Extra AND conditions appended after the pattern's conditions
    append_conditions: Optional[list[dict[str, Any]]] = None
    # Merged on top of the ScanPattern's exit_config
    exit_config: Optional[dict[str, Any]] = None


# ── Scanner ─────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    tickers: Optional[list[str]] = None


class SmartPickRequest(BaseModel):
    message: Optional[str] = None
    budget: Optional[float] = None
    risk_tolerance: str = Field("medium", pattern=r"^(low|medium|high)$")


class PickRecheckRequest(BaseModel):
    ticker: str = Field(..., max_length=20)
    entry_price: float = Field(..., gt=0)
