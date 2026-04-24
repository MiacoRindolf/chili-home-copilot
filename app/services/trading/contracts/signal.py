"""Q1.T3 unified scanner signal contract (phase 1 — additive payload only)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

Horizon = Literal["scalp", "intraday", "day", "swing", "position", "options", "fx", "perp"]
Side = Literal["long", "short", "flat"]
GateStatus = Literal["proposed", "gated_ok", "gated_reject"]


class Signal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: str
    scanner: str
    strategy_family: str
    pattern_id: Optional[str] = None
    symbol: str
    venue: str
    side: Side
    horizon: Horizon
    created_at: datetime
    expires_at: datetime
    entry_price: Decimal
    stop_price: Decimal
    take_profit_price: Optional[Decimal] = None
    atr: Decimal
    expected_return: Decimal
    expected_vol: Decimal
    confidence: float = Field(..., ge=0, le=1)
    deflated_sharpe: Optional[float] = None
    pbo: Optional[float] = None
    regime: Optional[str] = None
    regime_posterior: Optional[dict] = None
    llm_rationale: Optional[str] = None
    features: dict
    rule_fires: list[str]
    gate_status: GateStatus = "proposed"
    gate_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _expires_after_created(self) -> Signal:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self
