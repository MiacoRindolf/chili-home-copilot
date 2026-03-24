from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..stage_catalog import TOTAL_STAGES


class LearningStatusDTO(BaseModel):
    """Aggregate status for future DB-backed brain cycle UI (parity with `get_learning_status()` keys)."""

    model_config = ConfigDict(extra="forbid")

    running: bool = False
    cycle_run_id: int | None = None
    correlation_id: str | None = None
    phase: str = "idle"
    current_step: str = ""
    steps_completed: int = 0
    total_steps: int = TOTAL_STAGES
    started_at: str | None = None
    step_timings: dict[str, float] = Field(default_factory=dict)
    data_provider: str | None = None
    last_cycle_funnel: dict[str, Any] | None = None
