"""Stub event shapes for Part C.2 — no runtime ingest in Phase 1."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class IntegrationEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    occurred_at: datetime
    idempotency_key: str
    correlation_id: str
    schema_version: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


# --- Stub payloads (minimal fields; align to `event_type` strings in Part C.2) ---


class ProposalPublishedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: int
    prediction_snapshot_id: int | None = None
    line_ids: list[int] = Field(default_factory=list)
    universe_id: str | None = None


class ProposalStatusChangedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: int
    from_status: str
    to_status: str
    actor_id: str | None = None


class ExecutionIntentRecordedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: int
    intent_id: str
    account_mode: str
    target_qty: float | None = None
    limit_px: float | None = None


class ExecutionOrderUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_id: str
    broker_order_id: str | None = None
    status: str = ""
    filled_qty: float = 0.0
    avg_px: float | None = None
    updated_at: datetime | None = None


class ExecutionFillPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fill_id: str
    intent_id: str
    qty: float
    px: float
    fees: float | None = None
    liquidity: str | None = None


class ExecutionPositionClosedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_id: str | None = None
    trade_id: int | None = None
    exit_px: float | None = None
    exit_ts: datetime | None = None
    realized_pnl: float | None = None
    holding_bars: int | None = None


class ProposalCancelledPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: int
    reason: str = ""
