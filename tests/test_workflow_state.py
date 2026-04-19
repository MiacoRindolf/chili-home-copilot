"""B1 workflow state machine: unit coverage for ``compute_state`` transitions
and for the 409 behavior of ``assert_transition_allowed``.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.models import PlanProject, PlanTask, ProjectMember, User
from app.models.code_brain import CodeRepo
from app.models.coding_task import (
    CodingAgentSuggestion,
    CodingAgentSuggestionApply,
    CodingTaskValidationRun,
    PlanTaskCodingProfile,
)
from app.services.coding_task.workflow_state import (
    STATES,
    WorkflowTransitionBlocked,
    assert_transition_allowed,
    compute_state,
)


def _user(db) -> User:
    u = User(name="wsu")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _task(db, user, *, with_profile: bool = False, repo: CodeRepo | None = None) -> PlanTask:
    p = PlanProject(user_id=user.id, name="WSP", key="WSP")
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    t = PlanTask(project_id=p.id, title="ws", reporter_id=user.id)
    db.add(t)
    db.flush()
    if with_profile:
        db.add(
            PlanTaskCodingProfile(
                task_id=t.id,
                code_repo_id=repo.id if repo else None,
                repo_index=0,
                sub_path="",
            )
        )
    db.commit()
    return t


def _indexed_repo(db, user) -> CodeRepo:
    repo = CodeRepo(
        user_id=user.id,
        path=str(Path(__file__).resolve().parents[1]),
        name="ws-repo",
        file_count=5,
        total_lines=99,
        last_indexed=datetime.utcnow(),
        active=True,
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return repo


def test_compute_state_not_bound_when_no_profile(db):
    user = _user(db)
    task = _task(db, user, with_profile=False)
    snap = compute_state(db, task, user_id=user.id)
    assert snap.state == "not_bound"
    assert snap.workspace_bound is False


def test_compute_state_bound_but_not_indexed(db):
    user = _user(db)
    repo = CodeRepo(user_id=user.id, path="/nowhere/does-not-matter", name="fresh", active=True)
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = _task(db, user, with_profile=True, repo=repo)
    snap = compute_state(db, task, user_id=user.id)
    assert snap.state == "bound"
    assert snap.workspace_indexed is False


def test_compute_state_indexed_when_repo_has_files(db):
    user = _user(db)
    repo = _indexed_repo(db, user)
    task = _task(db, user, with_profile=True, repo=repo)
    snap = compute_state(db, task, user_id=user.id)
    assert snap.state == "indexed"


def test_compute_state_snapshot_saved_after_suggestion_row(db):
    user = _user(db)
    repo = _indexed_repo(db, user)
    task = _task(db, user, with_profile=True, repo=repo)
    db.add(
        CodingAgentSuggestion(
            task_id=task.id,
            user_id=user.id,
            model="m",
            response_text="r",
            diffs_json=json.dumps(["--- a\n+++ b\n"]),
        )
    )
    db.commit()
    snap = compute_state(db, task, user_id=user.id)
    assert snap.state == "snapshot_saved"


def test_compute_state_dry_run_then_applied_then_validated(db):
    user = _user(db)
    repo = _indexed_repo(db, user)
    task = _task(db, user, with_profile=True, repo=repo)
    snap = CodingAgentSuggestion(
        task_id=task.id,
        user_id=user.id,
        model="m",
        response_text="r",
        diffs_json=json.dumps(["x"]),
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)
    db.add(
        CodingAgentSuggestionApply(
            suggestion_id=snap.id, task_id=task.id, user_id=user.id,
            dry_run=True, status="completed", message="dry ok",
        )
    )
    db.commit()
    assert compute_state(db, task, user_id=user.id).state == "dry_run_passed"

    db.add(
        CodingAgentSuggestionApply(
            suggestion_id=snap.id, task_id=task.id, user_id=user.id,
            dry_run=False, status="completed", message="real ok",
        )
    )
    db.commit()
    assert compute_state(db, task, user_id=user.id).state == "applied"

    db.add(
        CodingTaskValidationRun(
            task_id=task.id,
            trigger_source="post_apply",
            status="completed",
            exit_code=0,
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
    )
    db.commit()
    assert compute_state(db, task, user_id=user.id).state == "validated"


def test_assert_transition_rejects_apply_before_dry_run(db):
    user = _user(db)
    repo = _indexed_repo(db, user)
    task = _task(db, user, with_profile=True, repo=repo)
    db.add(
        CodingAgentSuggestion(
            task_id=task.id, user_id=user.id, model="m", response_text="r",
            diffs_json=json.dumps(["x"]),
        )
    )
    db.commit()
    with pytest.raises(WorkflowTransitionBlocked) as exc:
        assert_transition_allowed(db, task, "apply", user_id=user.id)
    assert exc.value.action == "apply"
    assert exc.value.required == "dry_run_passed"


def test_assert_transition_allows_dry_run_at_snapshot_saved(db):
    user = _user(db)
    repo = _indexed_repo(db, user)
    task = _task(db, user, with_profile=True, repo=repo)
    db.add(
        CodingAgentSuggestion(
            task_id=task.id, user_id=user.id, model="m", response_text="r",
            diffs_json=json.dumps(["x"]),
        )
    )
    db.commit()
    # Should not raise
    snap = assert_transition_allowed(db, task, "dry_run", user_id=user.id)
    assert snap.state == "snapshot_saved"


def test_states_tuple_is_ordered_and_unique():
    assert len(STATES) == len(set(STATES))
    assert STATES.index("bound") < STATES.index("indexed") < STATES.index("applied")
