"""Canonical ordered stage keys derived from ``learning_cycle_architecture`` (single source of truth).

``STAGE_KEYS`` lists every in-cycle step SID in execution order.
``TOTAL_STAGES`` equals the number of steps that bump the progress counter.
"""

from __future__ import annotations

from ..services.trading.learning_cycle_architecture import (
    TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS,
    count_cycle_progress_steps,
)

_SCHEDULED_CLUSTER = "c_scheduled"

STAGE_KEYS: tuple[str, ...] = tuple(
    step.sid
    for cluster in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS
    if cluster.id != _SCHEDULED_CLUSTER
    for step in cluster.steps
)

TOTAL_STAGES: int = count_cycle_progress_steps()
