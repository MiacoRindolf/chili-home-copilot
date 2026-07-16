from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.models import ProjectAutonomyArtifact, ProjectAutonomyRun
from app.models.code_brain import CodeRepo
from app.services.project_autonomy import orchestrator
from app.services.project_autonomy.workflow_skills import propose_workflow_skill_patch
from scripts import autopilot_macro_long_horizon_tournament as macro
from scripts import autopilot_offline_project_autonomy_benchmark as offline_benchmark


@pytest.mark.parametrize("task", macro.default_tasks(), ids=lambda task: task.task_id)
def test_workflow_skill_completes_all_macro_milestones_without_test_edits(tmp_path, task):
    macro._init_task_repo(task, tmp_path)
    visible: dict[str, str] = {}
    hidden: dict[str, str] = {}

    for phase in task.phases:
        visible.update(phase.visible_tests)
        hidden.update(phase.hidden_tests)
        macro._write_files(tmp_path, phase.visible_tests)
        before = {path: (tmp_path / path).read_bytes() for path in visible}

        patch = propose_workflow_skill_patch(tmp_path, phase.goal, task.allowed_files)

        assert patch is not None
        assert patch.milestone == int(phase.phase_id.rsplit("-", 1)[-1])
        assert patch.evidence["model_calls_required"] == 0
        assert patch.evidence["premium_models_required"] is False
        assert set(patch.changed_files).issubset(task.allowed_files)
        applied = orchestrator._git(
            tmp_path,
            ["apply"],
            input_text=patch.diff,
            timeout=60,
        )
        assert applied.returncode == 0, applied.stderr or applied.stdout
        assert before == {path: (tmp_path / path).read_bytes() for path in visible}

        macro._write_files(tmp_path, hidden)
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "hidden_tests", "-q"],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        shutil.rmtree(tmp_path / "hidden_tests")


def test_workflow_skill_matches_ast_symbols_in_renamed_paths(tmp_path):
    task = macro.default_tasks()[0]
    renamed = {
        "pkg/flags.py": task.source_files["app/config.py"],
        "pkg/buckets.py": task.source_files["app/cohort.py"],
        "pkg/policy.py": task.source_files["app/decision.py"],
        "pkg/entrypoint.py": task.source_files["app/service.py"],
    }
    for path, content in renamed.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    patch = propose_workflow_skill_patch(tmp_path, task.phases[0].goal, tuple(renamed))

    assert patch is not None
    assert patch.skill_id == "python.progressive-rollout.v1"
    assert patch.changed_files == tuple(sorted(renamed))


def test_workflow_skill_rejects_ambiguous_or_non_milestone_request(tmp_path):
    task = macro.default_tasks()[0]
    macro._init_task_repo(task, tmp_path)

    assert propose_workflow_skill_patch(tmp_path, "Improve rollout behavior.", task.allowed_files) is None
    assert propose_workflow_skill_patch(
        tmp_path,
        task.phases[0].goal,
        (*task.allowed_files, "tests/test_rollout_m1.py"),
    ) is None


def test_orchestrator_uses_workflow_skill_before_model_selection(tmp_path, monkeypatch):
    task = macro.default_tasks()[0]
    phase = task.phases[0]
    macro._init_task_repo(task, tmp_path)
    macro._write_files(tmp_path, phase.visible_tests)
    db = offline_benchmark._session()
    try:
        repo = CodeRepo(path=str(tmp_path), name="workflow-skill", active=True)
        db.add(repo)
        db.commit()
        run = ProjectAutonomyRun(
            run_id="workflow_skill_no_model",
            repo_id=repo.id,
            prompt=phase.goal,
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

        plan = orchestrator.build_local_plan(
            db,
            run,
            repo,
            context={"relevant_files": []},
            repo_path=tmp_path,
        )
        assert {item["path"] for item in plan["files"]} == set(task.allowed_files)
        diffs = orchestrator.generate_diffs_from_plan(
            db,
            run,
            tmp_path,
            [
                {"path": path, "action": "modify", "description": phase.goal}
                for path in task.allowed_files
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
        assert artifact.name == "deterministic_workflow_skill_patch"
        assert "python.progressive-rollout.v1" in str(artifact.content_json)
    finally:
        db.close()

