from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "autopilot_meso_project_workflow_tournament.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "autopilot_meso_project_workflow_tournament",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _result(module, source_kind: str, *, score: int = 100, seconds: float = 10.0):
    return module.ContestantResult(
        task_id="task",
        source_kind=source_kind,
        model_name=module.MODEL_NAMES[source_kind],
        duration_seconds=seconds,
        attempts=1,
        quality_score=score,
        behavior_passed=True,
        scope_valid=True,
        tests_unchanged=True,
        coordinated_files_changed=True,
        semantic_review_passed=True,
        premium_calls=0 if source_kind == "local_model" else None,
        changed_files=("app/a.py", "app/b.py"),
        validation_output="2 passed",
        failure="",
        final_diff="patch",
    )


def _tiny_task(module):
    return module.WorkflowTask(
        task_id="tiny-contract",
        title="Tiny contract",
        goal=(
            "normalize must trim nonblank values and render must return value:<normalized>. "
            "Keep tests unchanged."
        ),
        source_files={
            "app/a.py": "def normalize(value: str) -> str:\n    return value\n",
            "app/b.py": (
                "from app.a import normalize\n\n\n"
                "def render(value: str) -> str:\n"
                "    return normalize(value)\n"
            ),
        },
        visible_tests={
            "tests/test_contract.py": (
                "from app.b import render\n\n\n"
                "def test_render_normalizes():\n"
                "    assert render('  Ada  ') == 'value:Ada'\n"
            ),
        },
        hidden_tests={
            "hidden_tests/test_edges.py": (
                "import pytest\n\n"
                "from app.a import normalize\n\n\n"
                "def test_blank_is_rejected():\n"
                "    with pytest.raises(ValueError, match='blank'):\n"
                "        normalize('   ')\n"
            ),
        },
        required_files=("app/a.py", "app/b.py"),
    )


def test_default_meso_tasks_cover_three_distinct_multi_file_contracts():
    module = _load_module()
    tasks = module.default_tasks()

    assert len(tasks) == 3
    assert len({task.task_id for task in tasks}) == 3
    assert all(len(task.required_files) == 3 for task in tasks)
    assert all(set(task.required_files) == set(task.source_files) for task in tasks)
    assert all(task.hidden_tests for task in tasks)


def test_frontier_prompt_does_not_leak_held_out_tests(tmp_path):
    module = _load_module()
    task = _tiny_task(module)
    module._init_task_repo(task, tmp_path)

    prompt = module.render_frontier_prompt(task, tmp_path)

    assert "tests/test_contract.py" in prompt
    assert "hidden_tests/test_edges.py" not in prompt
    assert "test_blank_is_rejected" not in prompt


def test_extract_unified_diff_accepts_fenced_patch_and_scope_rejects_tests(tmp_path):
    module = _load_module()
    task = _tiny_task(module)
    module._init_task_repo(task, tmp_path)
    test_patch = module.orchestrator._unified_diff(
        "tests/test_contract.py",
        task.visible_tests["tests/test_contract.py"],
        task.visible_tests["tests/test_contract.py"].replace("value:Ada", "Ada"),
    )
    response = "Here is the repair:\n```diff\n" + test_patch + "```\n"

    assert module.extract_unified_diff(response) == test_patch
    applied, evidence, _patch = module._apply_scoped_patch(tmp_path, task, response)

    assert applied is False
    assert "scope mismatch" in evidence
    assert "tests/test_contract.py" in evidence


def test_frontier_contestant_applies_multi_file_patch_and_passes_hidden_tests(tmp_path):
    module = _load_module()
    task = _tiny_task(module)
    module._init_task_repo(task, tmp_path)
    fixed_a = (
        "def normalize(value: str) -> str:\n"
        "    normalized = value.strip()\n"
        "    if not normalized:\n"
        "        raise ValueError('blank value')\n"
        "    return normalized\n"
    )
    fixed_b = (
        "from app.a import normalize\n\n\n"
        "def render(value: str) -> str:\n"
        "    return f\"value:{normalize(value)}\"\n"
    )
    patch = (
        module.orchestrator._unified_diff("app/a.py", task.source_files["app/a.py"], fixed_a)
        + module.orchestrator._unified_diff("app/b.py", task.source_files["app/b.py"], fixed_b)
    )

    def fake_call(source_kind, prompt, timeout_seconds, max_budget_usd):
        assert source_kind == "codex"
        assert "hidden_tests" not in prompt
        assert timeout_seconds == 90
        assert max_budget_usd == 1.0
        return f"```diff\n{patch}```\n", 1.25, "codex fake"

    result = module.run_frontier_contestant(
        task,
        "codex",
        tmp_path,
        call=fake_call,
        timeout_seconds=90,
        max_budget_usd=1.0,
    )

    assert result.eligible is True
    assert result.quality_score == 100
    assert result.changed_files == ("app/a.py", "app/b.py")
    assert result.tests_unchanged is True
    assert result.behavior_passed is True
    assert "2 passed" in result.validation_output


def test_winner_rule_prefers_quality_before_local_independence():
    module = _load_module()
    local = _result(module, "local_model", score=95, seconds=1.0)
    codex = _result(module, "codex", score=100, seconds=20.0)

    winner = module.choose_winner([local, codex])

    assert winner is codex


def test_winner_rule_uses_zero_premium_local_only_for_exact_quality_tie():
    module = _load_module()
    local = _result(module, "local_model", score=100, seconds=40.0)
    codex = _result(module, "codex", score=100, seconds=10.0)
    claude = _result(module, "claude", score=100, seconds=20.0)

    winner = module.choose_winner([codex, local, claude])

    assert winner is local


def test_frontier_command_failure_marks_collection_incomplete():
    module = _load_module()
    failed = module.dataclasses.replace(
        _result(module, "claude"),
        behavior_passed=False,
        scope_valid=False,
        coordinated_files_changed=False,
        semantic_review_passed=False,
        quality_score=10,
        changed_files=(),
        validation_output="validation not run",
        failure="RuntimeError: Exceeded USD budget",
        final_diff="",
    )

    assert module._collection_failure(failed) is True


def test_report_emits_gap_matrix_metadata_contract():
    module = _load_module()
    summary = {
        "generated_utc": "2026-07-11T00:00:00Z",
        "status": "passed",
        "evidence_mode": "real_artifacts",
        "run_id": "meso-proof",
        "tasks": 3,
        "source_kinds": list(module.SOURCE_KINDS),
        "winner_counts": {"local_model": 3, "codex": 0, "claude": 0, "none": 0},
        "runtime_measurements": {"measured": 9, "unmeasured": 0},
        "premium_independent_local_results": 3,
        "safety": "isolated",
        "task_results": [],
    }

    report = module.render_report(summary)

    assert "- Status: passed" in report
    assert "- Evidence mode: real_artifacts" in report
    assert "- Tasks: 3" in report
    assert "- Winner counts: local_model=3, codex=0, claude=0, none=0" in report
    assert "- Runtime measurements: measured=9, unmeasured=0" in report
