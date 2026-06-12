"""Dispatch repair pass: failure evidence reaches the repair prompt."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.models import CodingTaskValidationRun, CodingValidationArtifact
from app.models.planner import PlanProject, PlanTask
from app.services.code_dispatch.runner import validation_failure_text


@pytest.fixture
def task_id(db, paired_client):
    _client, user = paired_client
    p = PlanProject(user_id=user.id, name="RepairProj", key="RPR")
    db.add(p)
    db.flush()
    t = PlanTask(project_id=p.id, title="repair target")
    db.add(t)
    db.flush()
    return int(t.id)


def _seed_run(db, task_id, *artifacts):
    run = CodingTaskValidationRun(
        task_id=task_id, trigger_source="test", status="failed", started_at=datetime.utcnow(),
    )
    db.add(run)
    db.flush()
    for step_key, kind, content in artifacts:
        db.add(CodingValidationArtifact(
            run_id=run.id, step_key=step_key, kind=kind,
            content=content, byte_length=len(content),
        ))
    db.commit()
    return int(run.id)


def test_failure_text_includes_failing_steps_and_skips_noise(db, task_id):
    rid = _seed_run(
        db, task_id,
        ("ast_syntax", "log", "SyntaxError mod.py: invalid syntax line 3"),
        ("ruff_check", "skip", "[ruff not installed]"),
        ("pytest_collect", "log", "1 error during collection"),
    )
    text = validation_failure_text(db, rid)
    assert "SyntaxError mod.py" in text
    assert "collection" in text
    assert "ruff not installed" not in text  # skipped steps are noise


def test_failure_text_bounded_and_safe_on_empty(db, task_id):
    rid = _seed_run(db, task_id, ("ast_syntax", "log", "x" * 20000))
    assert len(validation_failure_text(db, rid, max_bytes=500)) <= 500
    rid2 = _seed_run(db, task_id)
    assert "no failure artifacts" in validation_failure_text(db, rid2)


def test_draft_dispatcher_threads_extra_instructions():
    """Signature contract: the repair pass depends on this parameter."""
    import inspect

    from app.services.code_dispatch.cycle import _dispatch_draft_suggestion

    params = inspect.signature(_dispatch_draft_suggestion).parameters
    assert "extra_instructions" in params
