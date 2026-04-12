"""Factories for trading-brain repositories (no global singletons)."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from .infrastructure.integration_sqlalchemy import SqlAlchemyBrainIntegrationEventStore
from .infrastructure.lease_sqlalchemy import SqlAlchemyBrainCycleLeasePort
from .infrastructure.learning_status_sqlalchemy import (
    SqlAlchemyBrainLearningStatusReader,
)
from .infrastructure.repositories.cycle_sqlalchemy import (
    SqlAlchemyBrainLearningCycleRunRepository,
    SqlAlchemyBrainStageJobRepository,
)
from .infrastructure.repositories.prediction_read_sqlalchemy import (
    SqlAlchemyBrainPredictionReadRepository,
)
from .infrastructure.repositories.prediction_snapshot_sqlalchemy import (
    SqlAlchemyBrainPredictionSnapshotRepository,
)
from .schemas.status import LearningStatusDTO

logger = logging.getLogger(__name__)


def make_cycle_run_repo() -> SqlAlchemyBrainLearningCycleRunRepository:
    return SqlAlchemyBrainLearningCycleRunRepository()


def make_stage_job_repo() -> SqlAlchemyBrainStageJobRepository:
    return SqlAlchemyBrainStageJobRepository()


def make_lease_port() -> SqlAlchemyBrainCycleLeasePort:
    return SqlAlchemyBrainCycleLeasePort()


def make_integration_store() -> SqlAlchemyBrainIntegrationEventStore:
    return SqlAlchemyBrainIntegrationEventStore()


def make_learning_status_reader() -> SqlAlchemyBrainLearningStatusReader:
    return SqlAlchemyBrainLearningStatusReader()


def make_prediction_snapshot_repo() -> SqlAlchemyBrainPredictionSnapshotRepository:
    return SqlAlchemyBrainPredictionSnapshotRepository()


def make_prediction_read_repo() -> SqlAlchemyBrainPredictionReadRepository:
    return SqlAlchemyBrainPredictionReadRepository()


__all__ = [
    "make_cycle_run_repo",
    "make_integration_store",
    "make_lease_port",
    "make_learning_status_reader",
    "make_stage_job_repo",
    "LearningStatusDTO",
]

# Re-export Phase 3 lease helpers (implementation lives in lease_dedicated_session).
from .infrastructure.lease_dedicated_session import (  # noqa: E402
    brain_lease_enforcement_log_peer_on_denial,
    brain_lease_enforcement_refresh_soft_dedicated,
    brain_lease_enforcement_release_dedicated,
    brain_lease_enforcement_try_acquire_dedicated,
    brain_lease_holder_id,
)

__all__ += [
    "brain_lease_enforcement_log_peer_on_denial",
    "brain_lease_enforcement_refresh_soft_dedicated",
    "brain_lease_enforcement_release_dedicated",
    "brain_lease_enforcement_try_acquire_dedicated",
    "brain_lease_holder_id",
]
