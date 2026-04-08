"""Request/response models for momentum operator API (Phase 4)."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class MomentumRefreshBody(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=36)
    execution_family: str = Field(
        default="coinbase_spot",
        max_length=32,
        description="How/where orders route; must match variant.execution_family. Only coinbase_spot is implemented.",
    )


class MomentumRunPaperBody(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=36)
    variant_id: int = Field(..., ge=1)
    execution_family: str = Field(
        default="coinbase_spot",
        max_length=32,
        description="Execution routing; must align with momentum_strategy_variants.execution_family.",
    )


class MomentumArmLiveBody(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=36)
    variant_id: int = Field(..., ge=1)
    execution_family: str = Field(
        default="coinbase_spot",
        max_length=32,
        description="Execution routing; must align with momentum_strategy_variants.execution_family.",
    )


class MomentumConfirmLiveArmBody(BaseModel):
    arm_token: str = Field(..., min_length=8, max_length=128)
    confirm: bool = Field(..., description="Must be true — explicit operator confirmation.")


class MomentumPaperRunnerTickBody(BaseModel):
    session_id: int = Field(..., ge=1, description="Paper automation session to advance one tick.")


class MomentumLiveRunnerTickBody(BaseModel):
    session_id: int = Field(..., ge=1, description="Live automation session to advance one tick.")


class MomentumViableQuery(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=36)
    mode: Optional[Literal["paper", "live"]] = Field(
        default=None,
        description="Optional UI mode echo (session state lives client-side; server defaults paper).",
    )
