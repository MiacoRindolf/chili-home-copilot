from __future__ import annotations

import dataclasses
from pathlib import Path

from scripts import autopilot_macro_long_horizon_tournament as macro


def _eligible_result(task_id: str, source: str, *, seconds: float = 1.0):
    premium_calls = 0 if source == "local_model" else None
    return macro.MacroContestantResult(
        task_id=task_id,
        source_kind=source,
        model_name=macro.MODEL_NAMES[source],
        duration_seconds=seconds,
        attempts=3,
        quality_score=100,
        behavior_passed=True,
        phases_passed=3,
        phase_count=3,
        scope_valid=True,
        tests_unchanged=True,
        required_files_changed=True,
        continuity_valid=True,
        semantic_review_passed=True,
        premium_calls=premium_calls,
        changed_files=("app/a.py",),
        validation_output="passed",
        failure="",
        final_diff="--- a/app/a.py\n+++ b/app/a.py\n@@ -1 +1 @@\n-a\n+b\n",
    )


def test_default_macro_tasks_are_three_distinct_three_phase_projects():
    tasks = macro.default_tasks()

    assert len(tasks) == 3
    assert len({task.task_id for task in tasks}) == 3
    assert all(len(task.phases) == 3 for task in tasks)
    assert all(4 <= len(task.source_files) <= 8 for task in tasks)
    assert all("Milestone 1/3" in task.phases[0].goal for task in tasks)
    assert macro.MODEL_NAMES == {
        "local_model": "qwen2.5-coder:7b",
        "codex": "gpt-5.6-sol",
        "claude": "claude-fable-5",
    }


def test_frontier_prompt_excludes_hidden_tests_and_carries_continuity(tmp_path):
    task = macro.default_tasks()[0]
    macro._init_task_repo(task, tmp_path)
    macro._prepare_phase(tmp_path, task, 0)
    patch = macro.orchestrator.generate_diffs_from_plan
    assert patch is not None
    prompt = macro.render_frontier_prompt(task, 0, tmp_path)

    assert "hidden_tests" not in prompt
    assert "Completed milestones: none" in prompt
    assert "claude-fable-5" not in prompt
    assert "gpt-5.6-sol" not in prompt


def test_winner_prefers_zero_premium_system_only_after_quality_tie():
    local = _eligible_result("task", "local_model", seconds=100)
    codex = _eligible_result("task", "codex", seconds=1)
    weaker_local = dataclasses.replace(local, quality_score=95)

    assert macro.choose_winner([local, codex]) is local
    assert macro.choose_winner([weaker_local, codex]) is codex


def test_tournament_summary_contract_with_equal_quality_fake_contestants(tmp_path, monkeypatch):
    monkeypatch.setattr(
        macro,
        "run_chili_contestant",
        lambda task, root, **kwargs: _eligible_result(task.task_id, "local_model", seconds=0.1),
    )
    monkeypatch.setattr(
        macro,
        "run_frontier_contestant",
        lambda task, source_kind, root, **kwargs: _eligible_result(task.task_id, source_kind, seconds=1.0),
    )

    summary = macro.run_tournament(
        artifact_root=tmp_path / "artifacts",
        run_id="contract",
        write=False,
    )
    report = macro.render_report(summary)

    assert summary["status"] == "passed"
    assert summary["evidence_mode"] == "real_artifacts"
    assert summary["tasks"] == 3
    assert summary["winner_counts"]["local_model"] == 3
    assert summary["winner_counts"]["codex"] == 0
    assert summary["winner_counts"]["claude"] == 0
    assert summary["runtime_measurements"] == {"measured": 9, "unmeasured": 0}
    assert "- Tasks: 3" in report
    assert "- Winner counts: local_model=3, codex=0, claude=0, none=0" in report


def test_macro_quality_requires_cumulative_behavior():
    assert macro.quality_score(
        behavior_passed=True,
        scope_valid=True,
        tests_unchanged=True,
        required_files_changed=True,
        continuity_valid=True,
        semantic_review_passed=True,
    ) == 100
    assert macro.quality_score(
        behavior_passed=False,
        scope_valid=True,
        tests_unchanged=True,
        required_files_changed=True,
        continuity_valid=False,
        semantic_review_passed=True,
    ) < 50


def test_regrade_does_not_penalize_an_already_correct_untouched_file(tmp_path):
    task = macro.default_tasks()[0]
    rows = []
    for source in macro.SOURCE_KINDS:
        result = dataclasses.asdict(_eligible_result(task.task_id, source))
        result["eligible"] = True
        result["phase_results"] = [
            {"phase_id": f"milestone-{index}", "changed_files": ["app/a.py"]}
            for index in (1, 2, 3)
        ]
        rows.append(result)
    rows[1]["required_files_changed"] = False
    rows[1]["quality_score"] = 95
    rows[1]["eligible"] = False
    run_dir = tmp_path / "raw"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text(
        macro.json.dumps(
            {
                "schema": macro.SCHEMA,
                "evidence_mode": "real_artifacts",
                "status": "passed",
                "tasks": 3,
                "collection_failures": [],
                "task_results": [
                    {"task_id": task.task_id, "results": rows},
                    {"task_id": "two", "results": [dict(item) for item in rows]},
                    {"task_id": "three", "results": [dict(item) for item in rows]},
                ],
            }
        ),
        encoding="utf-8",
    )

    summary = macro.regrade_artifact_run(run_dir, write=False)

    regraded_codex = summary["task_results"][0]["results"][1]
    assert regraded_codex["quality_score"] == 100
    assert regraded_codex["eligible"] is True
    assert regraded_codex["phase_changes_present"] is True
    assert summary["winner_counts"]["local_model"] == 3
