from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path

from scripts import autopilot_diagnosis_to_fix_benchmark as benchmark


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "autonomy_diagnosis_to_fix"


def test_holdout_manifest_keeps_hidden_oracles_out_of_cases():
    manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["reference_family"] == "claude-fable-5"
    assert len(manifest["cases"]) >= 3
    for entry in manifest["cases"]:
        case = json.loads((FIXTURE_ROOT / entry["case"]).read_text(encoding="utf-8"))
        oracle = json.loads((FIXTURE_ROOT / entry["oracle"]).read_text(encoding="utf-8"))
        assert "expected_dimension" not in case
        assert "expected_file" not in case
        assert "expected_files" not in case
        assert "hidden_files" not in case
        assert oracle["case_id"] == case["case_id"]


def test_all_repair_fixtures_pass_public_and_fail_hidden_baseline(tmp_path):
    args = argparse.Namespace(
        fixture_root=str(FIXTURE_ROOT),
        model="qwen2.5-coder:7b",
        case=[],
        timeout=1.0,
        max_repairs=0,
        validate_fixtures=True,
        report=str(tmp_path / "unused.md"),
        results_json=str(tmp_path / "unused.json"),
        json=False,
    )

    result = benchmark.run(args)

    assert result["valid"] is True
    assert all(item["public_passed"] for item in result["cases"])
    assert all(item["hidden_failed"] for item in result["cases"])


def test_scoring_requires_real_hidden_repair_and_correct_ownership():
    oracle = {"expected_dimension": "clock", "expected_file": "session_gate.py"}
    diagnosis = {"report": {"conclusion": {"dimension": "clock"}}}
    patch = {"changed_files": ["session_gate.py"], "patch_applied": True}
    passed = {"passed": True}
    failed = {"passed": False}

    full_score, checks = benchmark._score_case(
        oracle,
        diagnosis,
        patch,
        failed,
        passed,
        passed,
    )
    weak_score, weak_checks = benchmark._score_case(
        oracle,
        diagnosis,
        {"changed_files": ["formatting.py"], "patch_applied": True},
        failed,
        passed,
        failed,
    )

    assert full_score == 100
    assert all(checks.values())
    assert weak_score < 70
    assert weak_checks["file_selection"] is False
    assert weak_checks["hidden_tests"] is False


def test_multifile_scoring_requires_exact_changed_file_set():
    oracle = {
        "expected_dimension": "data",
        "expected_files": ["producer.py", "consumer.py"],
    }
    diagnosis = {"report": {"conclusion": {"dimension": "data"}}}
    result = {"passed": True}
    baseline = {"passed": False}

    exact, exact_checks = benchmark._score_case(
        oracle,
        diagnosis,
        {
            "changed_files": ["consumer.py", "producer.py"],
            "patch_applied": True,
        },
        baseline,
        result,
        result,
    )
    extra, extra_checks = benchmark._score_case(
        oracle,
        diagnosis,
        {
            "changed_files": ["consumer.py", "producer.py", "metrics.py"],
            "patch_applied": True,
        },
        baseline,
        result,
        result,
    )

    assert exact == 100
    assert exact_checks["file_selection"] is True
    assert extra == 85
    assert extra_checks["file_selection"] is False


def test_multifile_edit_group_rolls_back_when_one_member_is_rejected(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "first.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "second.py").write_text("VALUE = 2\n", encoding="utf-8")

    def fake_edit(repo_path, selected, *_args, **_kwargs):
        if selected == "first.py":
            (repo_path / selected).write_text("VALUE = 10\n", encoding="utf-8")
            return {"patch_applied": True, "warnings": []}
        return {"patch_applied": False, "warnings": ["rejected second edit"]}

    monkeypatch.setattr(benchmark, "_apply_local_edit", fake_edit)
    outcome = benchmark._apply_planned_edits(
        repo,
        {
            "prompt": "Coordinate both modules.",
            "candidate_paths": ["first.py", "second.py"],
        },
        {},
        [
            {"path": "first.py", "description": "Update producer."},
            {"path": "second.py", "description": "Update consumer."},
        ],
        {"report": {}},
        "local-model",
        [],
        1.0,
        stage_prefix="edit",
    )

    assert outcome["patch_applied"] is False
    assert outcome["applied_files"] == []
    assert (repo / "first.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (repo / "second.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_plan_file_filter_drops_explicit_noop_members():
    selected = benchmark._plan_file_items(
        {
            "files": [
                {"path": "producer.py", "description": "Copy rows defensively."},
                {"path": "consumer.py", "description": "No changes needed in this file."},
                {
                    "path": "review_only.py",
                    "action": "review",
                    "description": "Inspect this related contract.",
                },
            ]
        },
        ["producer.py", "consumer.py", "review_only.py"],
        3,
    )

    assert selected == [
        {"path": "producer.py", "description": "Copy rows defensively."}
    ]


def test_validation_failure_context_keeps_public_and_hidden_failures():
    context = benchmark._validation_failure_context(
        {
            "passed": False,
            "output": '>       assert sink["XYZ"] == []\nE       KeyError: \'XYZ\'',
        },
        {
            "passed": False,
            "output": 'assert sink["XYZ"] is not source["XYZ"]',
        },
    )

    assert context.startswith("NON-NEGOTIABLE VALIDATION CONTRACTS")
    assert 'assert sink["XYZ"] == []' in context
    assert 'assert sink["XYZ"] is not source["XYZ"]' in context
    assert "observed: KeyError" in context
    assert "PUBLIC REGRESSION" in context
    assert "HELD-OUT BEHAVIOR FAILURE" in context


def test_initial_patch_generation_does_not_run_repair_reviewer(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "owner.py").write_text("VALUE = 1\n", encoding="utf-8")
    stages = []

    def fake_call(*_args, stage, **_kwargs):
        stages.append(stage)
        return json.dumps(
            {
                "analysis": "Owner is explicit.",
                "files": [
                    {
                        "path": "owner.py",
                        "action": "modify",
                        "description": "Update the owner.",
                    }
                ],
                "notes": "",
            }
        )

    monkeypatch.setattr(benchmark, "_local_call", fake_call)
    monkeypatch.setattr(
        benchmark,
        "_apply_planned_edits",
        lambda *_args, **_kwargs: {
            "patch_applied": True,
            "selected_files": ["owner.py"],
            "applied_files": ["owner.py"],
            "warnings": [],
        },
    )

    result = benchmark._generate_patch(
        repo,
        {
            "prompt": "Fix the owner.",
            "candidate_paths": ["owner.py"],
            "max_files": 1,
        },
        {"report": {}},
        "local-model",
        [],
        1.0,
    )

    assert result["patch_applied"] is True
    assert stages == ["plan"]


def test_local_edit_rejects_semantically_inverted_true_values(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "feature_gate.py"
    original = '_TRUE_VALUES = {"1", "true"}\n'
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        benchmark,
        "_local_call",
        lambda *_args, **_kwargs: (
            "```\n<<<<<<< SEARCH\n"
            '_TRUE_VALUES = {"1", "true"}\n'
            "=======\n"
            '_TRUE_VALUES = {"1", "true", "0"}\n'
            ">>>>>>> REPLACE\n```"
        ),
    )

    result = benchmark._apply_local_edit(
        repo,
        "feature_gate.py",
        "Interpret false values correctly.",
        "local-model",
        [],
        1.0,
        stage="edit",
    )

    assert result["patch_applied"] is False
    assert any("semantic polarity guard" in value for value in result["warnings"])
    assert target.read_text(encoding="utf-8") == original


def test_benchmark_model_path_imports_no_cloud_client():
    source = inspect.getsource(benchmark)

    assert "openai_client" not in source
    assert "gateway_chat" not in source
    assert '"premium_calls": 0' in source
