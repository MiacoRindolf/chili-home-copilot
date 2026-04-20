"""Server-side workflow state machine for the coding pipeline.

Derives the current lifecycle state for a planner task's coding profile from
existing signals (profile binding, workspace lookup, saved snapshots, apply
audits, validation runs) and rejects out-of-order mutation attempts with a
structured error that the router maps to HTTP 409.

This module does not own a new column yet — the state is computed on demand
so we can ship the fail-closed behavior without a migration. When/if a
persisted ``coding_workflow_state`` column is added, ``compute_state`` becomes
the backfill source and ``apply_transition`` the write path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.orm import Session

from ...models import PlanTask
from ...models.coding_task import (
    CodingAgentSuggestion,
    CodingAgentSuggestionApply,
    CodingTaskValidationRun,
    PlanTaskCodingProfile,
)
from ...models.project_domain import ProjectDomainRun
from .workspaces import lookup_workspace_repo_for_profile


# States, ordered from cold to done. A task may skip forward (e.g. the user
# saves a new snapshot after an apply) but must never cross a gate without the
# required prerequisite signal.
STATES = (
    "unbound",
    "bound_unindexed",
    "ready_to_suggest",
    "suggested_unsaved",
    "snapshot_saved",
    "dry_run_passed",
    "applied",
    "validated",
)

State = Literal[
    "unbound",
    "bound_unindexed",
    "ready_to_suggest",
    "suggested_unsaved",
    "snapshot_saved",
    "dry_run_passed",
    "applied",
    "validated",
]


class WorkflowTransitionBlocked(Exception):
    """Raised when a mutation's prerequisite state is missing.

    Router layer maps this to HTTP 409 with body
    ``{"workflow_blocked": true, "current_state": ..., "required_state": ..., "action": ...}``
    so the UI can show the operator which step they still need to complete.
    """

    def __init__(self, action: str, current: State, required: State) -> None:
        super().__init__(
            f"Cannot {action}: current state is {current!r}, requires at least {required!r}"
        )
        self.action = action
        self.current = current
        self.required = required


@dataclass(frozen=True)
class WorkflowSnapshot:
    state: State
    workspace_bound: bool
    workspace_indexed: bool
    has_suggested_unsaved: bool
    has_saved_snapshot: bool
    last_dry_run_passed: bool
    last_real_apply_passed: bool
    has_completed_validation: bool


def compute_state(db: Session, task: PlanTask, *, user_id: int | None = None) -> WorkflowSnapshot:
    """Derive the workflow state for ``task`` from persisted signals.

    The derivation is monotonic in practice — once a stage's signal exists it
    counts toward the highest state, and transient negative signals (failed
    apply, failed validation) do not drop the state below the last green.
    """
    profile = (
        db.query(PlanTaskCodingProfile)
        .filter(PlanTaskCodingProfile.task_id == task.id)
        .first()
    )
    repo = lookup_workspace_repo_for_profile(db, profile, user_id=user_id)
    bound = repo is not None
    indexed = bool(
        bound
        and repo
        and not repo.last_index_error
        and (
            repo.last_successful_indexed_at
            or repo.last_indexed
            or (repo.last_successful_file_count or repo.file_count or 0) > 0
        )
    )

    has_snapshot = (
        db.query(CodingAgentSuggestion.id)
        .filter(CodingAgentSuggestion.task_id == task.id)
        .first()
        is not None
    )
    latest_snapshot = (
        db.query(CodingAgentSuggestion.created_at)
        .filter(CodingAgentSuggestion.task_id == task.id)
        .order_by(CodingAgentSuggestion.created_at.desc(), CodingAgentSuggestion.id.desc())
        .first()
    )
    latest_suggest = (
        db.query(ProjectDomainRun.started_at)
        .filter(
            ProjectDomainRun.task_id == task.id,
            ProjectDomainRun.run_kind == "suggest",
            ProjectDomainRun.status == "completed",
        )
        .order_by(ProjectDomainRun.started_at.desc(), ProjectDomainRun.id.desc())
        .first()
    )
    latest_snapshot_at = latest_snapshot[0] if latest_snapshot else None
    latest_suggest_at = latest_suggest[0] if latest_suggest else None
    has_suggested_unsaved = bool(
        latest_suggest_at
        and (latest_snapshot_at is None or latest_suggest_at > latest_snapshot_at)
    )

    last_dry_run_passed = (
        db.query(CodingAgentSuggestionApply.id)
        .filter(
            CodingAgentSuggestionApply.task_id == task.id,
            CodingAgentSuggestionApply.dry_run.is_(True),
            CodingAgentSuggestionApply.status == "completed",
        )
        .first()
        is not None
    )
    last_real_apply_passed = (
        db.query(CodingAgentSuggestionApply.id)
        .filter(
            CodingAgentSuggestionApply.task_id == task.id,
            CodingAgentSuggestionApply.dry_run.is_(False),
            CodingAgentSuggestionApply.status == "completed",
        )
        .first()
        is not None
    )
    has_completed_validation = (
        db.query(CodingTaskValidationRun.id)
        .filter(
            CodingTaskValidationRun.task_id == task.id,
            CodingTaskValidationRun.status == "completed",
            CodingTaskValidationRun.exit_code == 0,
        )
        .first()
        is not None
    )

    if has_completed_validation and last_real_apply_passed:
        state: State = "validated"
    elif last_real_apply_passed:
        state = "applied"
    elif last_dry_run_passed:
        state = "dry_run_passed"
    elif has_snapshot:
        state = "snapshot_saved"
    elif has_suggested_unsaved:
        state = "suggested_unsaved"
    elif indexed:
        state = "ready_to_suggest"
    elif bound:
        state = "bound_unindexed"
    else:
        state = "unbound"

    return WorkflowSnapshot(
        state=state,
        workspace_bound=bound,
        workspace_indexed=indexed,
        has_suggested_unsaved=has_suggested_unsaved,
        has_saved_snapshot=has_snapshot,
        last_dry_run_passed=last_dry_run_passed,
        last_real_apply_passed=last_real_apply_passed,
        has_completed_validation=has_completed_validation,
    )


# Action -> minimum required state. An action is allowed when the current
# state is equal to or later than the required state in ``STATES``.
REQUIRED_STATE_FOR_ACTION: dict[str, State] = {
    "suggest": "ready_to_suggest",
    "save_snapshot": "ready_to_suggest",
    "dry_run": "snapshot_saved",
    "apply": "dry_run_passed",
    "validate": "bound_unindexed",
}


def _state_index(state: State) -> int:
    try:
        return STATES.index(state)
    except ValueError:
        return -1


def assert_transition_allowed(
    db: Session,
    task: PlanTask,
    action: str,
    *,
    user_id: int | None = None,
) -> WorkflowSnapshot:
    """Compute state and raise ``WorkflowTransitionBlocked`` if ``action`` is
    not permitted. Returns the snapshot for callers that want to log/emit it.
    """
    required = REQUIRED_STATE_FOR_ACTION.get(action)
    snap = compute_state(db, task, user_id=user_id)
    if required is None:
        return snap
    if _state_index(snap.state) < _state_index(required):
        raise WorkflowTransitionBlocked(action=action, current=snap.state, required=required)
    return snap


def sync_task_workflow_state(
    db: Session,
    task: PlanTask,
    *,
    user_id: int | None = None,
) -> WorkflowSnapshot:
    snap = compute_state(db, task, user_id=user_id)
    task.coding_workflow_state = snap.state
    task.coding_workflow_state_updated_at = datetime.utcnow()
    db.flush()
    return snap
