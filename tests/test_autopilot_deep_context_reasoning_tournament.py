from __future__ import annotations

import dataclasses

from scripts import autopilot_deep_context_reasoning_tournament as deep


def _eligible_result(task_id: str, source: str, *, seconds: float = 1.0):
    return deep.ContextContestantResult(
        task_id=task_id,
        source_kind=source,
        model_name=deep.MODEL_NAMES[source],
        duration_seconds=seconds,
        attempts=1,
        quality_score=100,
        behavior_passed=True,
        scope_valid=True,
        context_scope_precise=True,
        tests_unchanged=True,
        semantic_review_passed=True,
        premium_calls=0 if source == "local_model" else None,
        context_files=24,
        distractor_files=20,
        changed_files=("a.py", "b.py", "c.py", "d.py"),
        validation_output="passed",
        failure="",
        final_diff="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n",
    )


def test_default_deep_context_tasks_have_equal_24_file_distractor_corpus():
    tasks = deep.default_tasks()

    assert len(tasks) == 3
    assert len({task.task_id for task in tasks}) == 3
    assert all(len(task.source_files) == 24 for task in tasks)
    assert all(task.distractor_count == 20 for task in tasks)
    assert all(len(task.contract_files) == 4 for task in tasks)
    assert all(not any(path in task.goal for path in task.contract_files) for task in tasks)
    assert deep.MODEL_NAMES == {
        "local_model": "qwen2.5-coder:7b",
        "codex": "gpt-5.6-sol",
        "claude": "claude-fable-5",
    }


def test_frontier_prompt_contains_full_context_but_not_hidden_tests(tmp_path):
    task = deep.default_tasks()[0]
    deep._init_task_repo(task, tmp_path)

    prompt = deep.render_frontier_prompt(task, tmp_path)

    assert "Repository source files in context: 24" in prompt
    assert "hidden_tests" not in prompt
    assert all(path in prompt for path in task.source_files)
    assert "claude-fable-5" not in prompt
    assert "gpt-5.6-sol" not in prompt


def test_winner_uses_independence_only_after_context_quality_tie():
    local = _eligible_result("task", "local_model", seconds=100)
    codex = _eligible_result("task", "codex", seconds=1)
    imprecise_local = dataclasses.replace(local, context_scope_precise=False, quality_score=90)

    assert deep.choose_winner([local, codex]) is local
    assert deep.choose_winner([imprecise_local, codex]) is codex


def test_fake_tournament_summary_preserves_real_artifact_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deep,
        "run_chili_contestant",
        lambda task, root, **kwargs: _eligible_result(task.task_id, "local_model", seconds=0.1),
    )
    monkeypatch.setattr(
        deep,
        "run_frontier_contestant",
        lambda task, source_kind, root, **kwargs: _eligible_result(task.task_id, source_kind),
    )

    summary = deep.run_tournament(artifact_root=tmp_path, run_id="contract", write=False)
    report = deep.render_report(summary)

    assert summary["status"] == "passed"
    assert summary["evidence_mode"] == "real_artifacts"
    assert summary["tasks"] == 3
    assert summary["winner_counts"]["local_model"] == 3
    assert summary["runtime_measurements"] == {"measured": 9, "unmeasured": 0}
    assert "- Context files per task: 24" in report
    assert "- Distractor files per task: 20" in report


def test_context_quality_requires_precise_scope():
    assert deep.quality_score(
        behavior_passed=True,
        scope_valid=True,
        context_scope_precise=True,
        tests_unchanged=True,
        semantic_review_passed=True,
    ) == 100
    assert deep.quality_score(
        behavior_passed=True,
        scope_valid=True,
        context_scope_precise=False,
        tests_unchanged=True,
        semantic_review_passed=True,
    ) == 90
