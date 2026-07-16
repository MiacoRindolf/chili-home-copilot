from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.models import ProjectAutonomyArtifact, ProjectAutonomyRun
from app.models.code_brain import CodeRepo
from app.services.project_autonomy import orchestrator
from app.services.project_autonomy.contract_skills import propose_contract_skill_patch
from scripts import autopilot_offline_project_autonomy_benchmark as offline_benchmark
from scripts.autopilot_meso_project_workflow_tournament import (
    _init_task_repo,
    _write_files,
    default_tasks,
)


@pytest.mark.parametrize("task", default_tasks(), ids=lambda task: task.task_id)
def test_contract_skill_repairs_full_meso_behavior_with_no_test_edits(tmp_path, task):
    _init_task_repo(task, tmp_path)
    test_hashes = {
        path: (tmp_path / path).read_bytes()
        for path in task.visible_tests
    }

    patch = propose_contract_skill_patch(tmp_path, task.goal, task.required_files)

    assert patch is not None
    assert patch.changed_files == tuple(sorted(task.required_files))
    assert patch.evidence["model_calls_required"] == 0
    assert patch.evidence["premium_models_required"] is False
    applied = orchestrator._git(
        tmp_path,
        ["apply"],
        input_text=patch.diff,
        timeout=60,
    )
    assert applied.returncode == 0, applied.stderr or applied.stdout
    assert test_hashes == {
        path: (tmp_path / path).read_bytes()
        for path in task.visible_tests
    }
    _write_files(tmp_path, task.hidden_tests)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "hidden_tests", "-q"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_pagination_contract_skill_matches_symbols_not_fixture_paths(tmp_path):
    task = default_tasks()[0]
    renamed = {
        "lib/http_params.py": task.source_files["app/query.py"],
        "lib/list_window.py": task.source_files["app/paging.py"],
        "lib/envelope.py": task.source_files["app/api.py"],
    }
    for path, content in renamed.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    patch = propose_contract_skill_patch(tmp_path, task.goal, tuple(renamed))

    assert patch is not None
    assert patch.skill_id == "python.pagination-envelope.v1"
    assert patch.changed_files == tuple(sorted(renamed))


def test_contract_skill_rejects_unapproved_or_ambiguous_scope(tmp_path):
    task = default_tasks()[0]
    _init_task_repo(task, tmp_path)
    extra = tmp_path / "app" / "extra.py"
    extra.write_text("VALUE = 1\n", encoding="utf-8")

    assert (
        propose_contract_skill_patch(
            tmp_path,
            task.goal,
            (*task.required_files, "app/extra.py"),
        )
        is None
    )
    assert (
        propose_contract_skill_patch(
            tmp_path,
            "Make pagination better.",
            task.required_files,
        )
        is None
    )


def test_generate_diffs_uses_contract_skill_before_local_model_selection(
    tmp_path,
    monkeypatch,
):
    task = default_tasks()[0]
    _init_task_repo(task, tmp_path)
    db = offline_benchmark._session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="contract-skill", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id="contract_skill_no_model",
            repo_id=repo.id,
            prompt=task.goal,
            status="running",
            current_stage="implement",
        )
        db.add(run)
        db.commit()
        monkeypatch.setattr(
            orchestrator,
            "select_local_model",
            lambda: (_ for _ in ()).throw(AssertionError("model selection should not run")),
        )

        diffs = orchestrator.generate_diffs_from_plan(
            db,
            run,
            tmp_path,
            [
                {"path": path, "action": "modify", "description": task.goal}
                for path in task.required_files
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
        assert artifact.name == "deterministic_contract_skill_patch"
        assert "python.pagination-envelope.v1" in str(artifact.content_json)
    finally:
        db.close()
