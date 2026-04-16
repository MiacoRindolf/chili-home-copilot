"""Canonical ordered stage keys derived from ``learning_cycle_architecture``.

Lists step SIDs from all learning-cycle clusters.
Legacy ``TOTAL_STAGES`` kept for backward compat (equals ``len(STAGE_KEYS)``).
"""

from __future__ import annotations

from ..services.trading.learning_cycle_architecture import (
    TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS,
    SCHEDULER_ONLY_LEARNING_CYCLE_CLUSTER_ID,
)

STAGE_KEYS: tuple[str, ...] = tuple(
    step.sid
    for cluster in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS
    if cluster.id != SCHEDULER_ONLY_LEARNING_CYCLE_CLUSTER_ID
    for step in cluster.steps
)
TOTAL_STAGES: int = len(STAGE_KEYS)
