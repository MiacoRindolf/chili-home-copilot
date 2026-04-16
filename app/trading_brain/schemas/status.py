from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LearningStatusDTO(BaseModel):
    """Aggregate status for brain cycle UI — mesh-native progress."""

    model_config = ConfigDict(extra="forbid")

    running: bool = False
    cycle_run_id: int | None = None
    correlation_id: str | None = None
    phase: str = "idle"
    current_step: str = ""
    nodes_completed: int = 0
    total_nodes: int = 0
    clusters_completed: int = 0
    total_clusters: int = 0
    started_at: str | None = None
    step_timings: dict[str, float] = Field(default_factory=dict)
    data_provider: str | None = None
    last_cycle_funnel: dict[str, Any] | None = None
