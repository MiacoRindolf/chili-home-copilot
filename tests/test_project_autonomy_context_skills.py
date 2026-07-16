from __future__ import annotations

import subprocess
import sys

import pytest

from app.models import ProjectAutonomyArtifact, ProjectAutonomyRun
from app.models.code_brain import CodeRepo
from app.services.project_autonomy import orchestrator
from app.services.project_autonomy.context_skills import (
    propose_context_scope_plan,
    propose_context_skill_patch,
)
from scripts import autopilot_deep_context_reasoning_tournament as deep
from scripts import autopilot_offline_project_autonomy_benchmark as offline_benchmark


@pytest.mark.parametrize("task", deep.default_tasks(), ids=lambda task: task.task_id)
def test_context_scope_and_skill_repair_24_file_repository_without_distractor_edits(tmp_path, task):
    deep._init_task_repo(task, tmp_path)
    test_hashes = {path: (tmp_path / path).read_bytes() for path in task.visible_tests}

    scope = propose_context_scope_plan(tmp_path, task.goal, task.allowed_files)

    assert scope is not None
    assert scope.files == task.contract_files
    assert scope.evidence["candidate_file_count"] == 24
    assert scope.evidence["distractor_file_count"] == 20
    assert scope.evidence["model_calls_required"] == 0
    patch = propose_context_skill_patch(tmp_path, task.goal, scope.files)
    assert patch is not None
    assert patch.changed_files == task.contract_files
    assert patch.evidence["premium_models_required"] is False
    assert patch.evidence["distractor_files_modified"] == 0
    applied = orchestrator._git(tmp_path, ["apply"], input_text=patch.diff, timeout=60)
    assert applied.returncode == 0, applied.stderr or applied.stdout
    assert test_hashes == {path: (tmp_path / path).read_bytes() for path in task.visible_tests}
    deep._write_files(tmp_path, task.hidden_tests)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "hidden_tests", "-q"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_context_scope_rejects_vague_request_and_duplicate_symbol_owner(tmp_path):
    task = deep.default_tasks()[0]
    deep._init_task_repo(task, tmp_path)

    assert propose_context_scope_plan(tmp_path, "Improve authorization.", task.allowed_files) is None
    duplicate = tmp_path / "distractors" / "authorization_policy" / "duplicate.py"
    duplicate.write_text("class Claims:\n    pass\n", encoding="utf-8")
    assert propose_context_scope_plan(
        tmp_path,
        task.goal,
        (*task.allowed_files, "distractors/authorization_policy/duplicate.py"),
    ) is None


def test_orchestrator_resolves_deep_context_before_model_selection(tmp_path, monkeypatch):
    task = deep.default_tasks()[0]
    deep._init_task_repo(task, tmp_path)
    db = offline_benchmark._session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="context-skill", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id="context_skill_no_model",
            repo_id=repo.id,
            prompt=task.goal,
            status="running",
            current_stage="plan",
        )
        db.add(run)
        db.commit()
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: (_ for _ in ()).throw(AssertionError("model selection should not run")),
        )
        context = {
            "relevant_files": [
                {"file": path, "symbol": "", "relevance": 0.5}
                for path in sorted((*task.source_files, *task.visible_tests))
            ]
        }

        plan = orchestrator.build_local_plan(db, run, repo, context=context, repo_path=tmp_path)
        assert {item["path"] for item in plan["files"]} == set(task.contract_files)
        assert plan["context_scope_evidence"]["distractor_file_count"] == 20
        diffs = orchestrator.generate_diffs_from_plan(
            db,
            run,
            tmp_path,
            [
                {"path": path, "action": "modify", "description": task.goal}
                for path in task.contract_files
            ],
        )
        assert len(diffs) == 1
        artifact = (
            db.query(ProjectAutonomyArtifact)
            .filter(ProjectAutonomyArtifact.run_id == run.run_id)
            .order_by(ProjectAutonomyArtifact.id.desc())
            .first()
        )
        assert artifact is not None
        assert artifact.name == "deterministic_deep_context_skill_patch"
        assert "python.tenant-authorization-context.v1" in str(artifact.content_json)
    finally:
        db.close()

