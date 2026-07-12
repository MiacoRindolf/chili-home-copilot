from __future__ import annotations

import argparse
import inspect
import json
import sys
from pathlib import Path

import pytest

from scripts import autopilot_diagnosis_to_fix_benchmark as benchmark


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "autonomy_diagnosis_to_fix"


def test_fixture_manifest_keeps_hidden_oracles_out_of_cases_and_labels_evaluation_role():
    manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["reference_family"] == "claude-fable-5"
    assert len(manifest["cases"]) >= 13
    languages = set()
    for entry in manifest["cases"]:
        assert entry["evaluation_role"] in {
            "development_regression",
            "blinded_holdout",
        }
        assert not entry["split"].startswith("holdout")
        case = json.loads((FIXTURE_ROOT / entry["case"]).read_text(encoding="utf-8"))
        oracle = json.loads((FIXTURE_ROOT / entry["oracle"]).read_text(encoding="utf-8"))
        languages.add(case.get("language", "python"))
        assert "expected_dimension" not in case
        assert "expected_file" not in case
        assert "expected_files" not in case
        assert "hidden_files" not in case
        assert oracle["case_id"] == case["case_id"]
    assert languages >= {"python", "typescript", "dart", "sql"}
    assert all(
        entry["evaluation_role"] == "development_regression"
        for entry in manifest["cases"]
    )


def test_all_legacy_repair_fixtures_pass_public_and_fail_feedback_and_final(tmp_path):
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

    assert result["schema"] == "chili.diagnosis-to-fix-fixture-validation.v3"
    assert result["valid"] is True
    assert all(item["public_passed"] for item in result["cases"])
    assert all(item["feedback_failed"] for item in result["cases"])
    assert all(item["final_failed"] for item in result["cases"])
    assert all("hidden_failed" not in item for item in result["cases"])


def test_sealed_test_runner_rejects_fixture_supplied_commands():
    assert benchmark._case_test_runner({}) == "pytest"
    assert benchmark._case_test_runner({"test_runner": "node_test"}) == "node_test"

    try:
        benchmark._case_test_runner({"test_runner": "shell", "command": "curl example.com"})
    except ValueError as exc:
        assert "Unknown sealed test runner" in str(exc)
    else:
        raise AssertionError("arbitrary runner was accepted")


def test_node_and_dart_test_discovery_is_bounded_and_public_scoped(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/public.test.ts").write_text("", encoding="utf-8")
    (tmp_path / "tests/hidden.test.ts").write_text("", encoding="utf-8")
    (tmp_path / "tests/public_cache_test.dart").write_text("", encoding="utf-8")
    (tmp_path / "tests/hidden_cache_test.dart").write_text("", encoding="utf-8")

    node_public = benchmark._bounded_test_files(
        tmp_path,
        suffixes=(".test.ts",),
        public_only=True,
    )
    dart_all = benchmark._bounded_test_files(
        tmp_path,
        suffixes=("_test.dart",),
        public_only=False,
    )

    assert node_public == ["tests/public.test.ts"]
    assert dart_all == ["tests/hidden_cache_test.dart", "tests/public_cache_test.dart"]


def test_multifile_budget_defaults_to_candidate_breadth_and_honors_explicit_cap():
    assert benchmark._case_max_files({"candidate_paths": ["a.py", "b.py"]}) == 2
    assert (
        benchmark._case_max_files(
            {"candidate_paths": ["a.py", "b.py"], "max_files": 1}
        )
        == 1
    )
    assert benchmark._case_max_files(
        {"candidate_paths": [f"owner_{index}.py" for index in range(8)]}
    ) == 4
    assert benchmark._plan_dimension({"dimension": "test harness"}) == "test_harness"


def test_local_escalation_schedule_is_bounded_by_global_repair_cap():
    schedule = benchmark._repair_model_schedule(
        argparse.Namespace(
            model="local-fast",
            max_repairs=3,
            escalation_model="local-deep",
            max_escalation_repairs=4,
        )
    )

    assert schedule == [
        "local-fast",
        "local-fast",
        "local-fast",
        "local-deep",
        "local-deep",
    ]
    assert len(schedule) == benchmark.MAX_REPAIR_ROUNDS


def test_fixture_ownership_preflight_rejects_impossible_edit_budget():
    case = {
        "candidate_paths": ["producer.py", "consumer.py"],
        "max_files": 1,
    }
    oracle = {
        "expected_files": ["producer.py", "consumer.py"],
    }

    with pytest.raises(ValueError, match="exceeds max_files budget"):
        benchmark._validate_expected_ownership(case, oracle)

    case.pop("max_files")
    benchmark._validate_expected_ownership(case, oracle)


def test_subprocess_output_is_decoded_as_utf8(tmp_path):
    code, output, _duration = benchmark._run(
        [
            sys.executable,
            "-c",
            "import sys;sys.stdout.buffer.write('caf\\u00e9 \\u2713\\n'.encode('utf-8'))",
        ],
        tmp_path,
    )

    assert code == 0
    assert output == "caf\u00e9 \u2713"


def test_invalid_diagnostic_json_gets_one_compact_retry(monkeypatch):
    responses = iter(
        [
            '{"hypotheses":[',
            '{"hypotheses":[],"experiments":[],"conclusion":{}}',
        ]
    )
    calls = []

    def fake_local_call(*_args, stage, calls, **_kwargs):
        response = next(responses)
        calls.append({"stage": stage, "response": response})
        return response

    monkeypatch.setattr(benchmark, "_local_call", fake_local_call)

    response = benchmark._diagnostic_json_call(
        "local-model",
        "judge",
        "Diagnose this case.",
        calls,
        1.0,
    )

    assert json.loads(response)["hypotheses"] == []
    assert [item["stage"] for item in calls] == [
        "diagnosis_judge",
        "diagnosis_judge_json_retry",
    ]
    assert calls[0]["json_object_valid"] is False
    assert calls[1]["json_object_valid"] is True


def test_scoring_requires_real_final_repair_and_correct_ownership():
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
    assert weak_checks["final_tests"] is False


def test_unaccepted_repair_plan_cannot_revise_diagnostic_conclusion():
    oracle = {"expected_dimension": "config", "expected_file": "owner.py"}
    diagnosis = {"report": {"conclusion": {"dimension": "runtime"}}}
    patch = {
        "diagnosis_dimension": "config",
        "changed_files": ["owner.py"],
        "patch_applied": True,
    }

    score, checks = benchmark._score_case(
        oracle,
        diagnosis,
        patch,
        {"passed": False},
        {"passed": True},
        {"passed": True},
    )

    assert score == 85
    assert checks["diagnosis"] is False

    diagnosis["accepted_conclusion"] = {
        "dimension": "config",
        "stage": "repair_1_validated",
        "accepted": True,
    }
    accepted_score, accepted_checks = benchmark._score_case(
        oracle,
        diagnosis,
        patch,
        {"passed": False},
        {"passed": True},
        {"passed": True},
    )

    assert accepted_score == 100
    assert accepted_checks["diagnosis"] is True


def test_shadow_verdict_requires_every_case_check_not_only_high_average():
    passing = {"checks": {"diagnosis": True, "final_tests": True}}
    one_miss = {"checks": {"diagnosis": False, "final_tests": True}}

    assert benchmark._verdict([passing, passing]) == "shadow_ready"
    assert benchmark._verdict([passing, one_miss]) == "needs_improvement"
    assert benchmark._verdict([]) == "needs_improvement"


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
    assert extra == 90
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


def test_multifile_edit_group_continues_past_already_satisfied_member(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "first.py").write_text("VALUE = 10\n", encoding="utf-8")
    (repo / "second.py").write_text("VALUE = 2\n", encoding="utf-8")

    def fake_edit(repo_path, selected, *_args, **_kwargs):
        if selected == "first.py":
            return {
                "patch_applied": False,
                "already_satisfied": True,
                "warnings": ["already satisfied"],
            }
        (repo_path / selected).write_text("VALUE = 20\n", encoding="utf-8")
        return {"patch_applied": True, "warnings": []}

    monkeypatch.setattr(benchmark, "_apply_local_edit", fake_edit)
    outcome = benchmark._apply_planned_edits(
        repo,
        {
            "prompt": "Coordinate both modules.",
            "candidate_paths": ["first.py", "second.py"],
        },
        {},
        [
            {"path": "first.py", "description": "Keep producer contract."},
            {"path": "second.py", "description": "Update consumer."},
        ],
        {"report": {}},
        "local-model",
        [],
        1.0,
        stage_prefix="edit",
    )

    assert outcome["patch_applied"] is True
    assert outcome["satisfied_files"] == ["first.py"]
    assert outcome["applied_files"] == ["second.py"]
    assert (repo / "second.py").read_text(encoding="utf-8") == "VALUE = 20\n"


def test_optional_owner_rejection_does_not_cancel_required_sibling(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "required.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "context.py").write_text("VALUE = 2\n", encoding="utf-8")

    def fake_edit(repo_path, selected, *_args, **_kwargs):
        if selected == "required.py":
            (repo_path / selected).write_text("VALUE = 10\n", encoding="utf-8")
            return {"patch_applied": True, "warnings": []}
        return {"patch_applied": False, "warnings": ["no change needed"]}

    monkeypatch.setattr(benchmark, "_apply_local_edit", fake_edit)
    outcome = benchmark._apply_planned_edits(
        repo,
        {
            "prompt": "Repair the owner.",
            "candidate_paths": ["required.py", "context.py"],
        },
        {},
        [
            {"path": "required.py", "description": "Repair the contract."},
            {
                "path": "context.py",
                "description": "Only change if causal.",
                "optional": True,
            },
        ],
        {"report": {}},
        "local-model",
        [],
        1.0,
        stage_prefix="edit",
    )

    assert outcome["patch_applied"] is True
    assert outcome["applied_files"] == ["required.py"]
    assert outcome["skipped_optional_files"] == ["context.py"]
    assert outcome["optional_rejected_diffs"] == [
        {
            "path": "context.py",
            "reason": "local edit adapter rejected the optional candidate",
            "attempted_diff": "",
            "validation_output": "no change needed",
        }
    ]
    assert (repo / "required.py").read_text(encoding="utf-8") == "VALUE = 10\n"
    assert (repo / "context.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_optional_owner_regression_is_restored_without_losing_required_edit(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "required.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "optional.py").write_text("VALUE = 2\n", encoding="utf-8")

    def fake_edit(repo_path, selected, *_args, **_kwargs):
        value = "10" if selected == "required.py" else "20"
        (repo_path / selected).write_text(f"VALUE = {value}\n", encoding="utf-8")
        return {"patch_applied": True, "warnings": []}

    public_calls = 0
    feedback_calls = 0

    def fake_tests(_repo, _case, *, public_only):
        nonlocal public_calls, feedback_calls
        if public_only:
            public_calls += 1
            return {"passed": public_calls == 1, "exit_code": 0 if public_calls == 1 else 1}
        feedback_calls += 1
        output = (
            "tests/test_contract.py::test_alpha PASSED [ 50%]\n"
            "tests/test_contract.py::test_beta FAILED [100%]\n"
            if feedback_calls == 1
            else (
                "tests/test_contract.py::test_alpha FAILED [ 50%]\n"
                "tests/test_contract.py::test_beta PASSED [100%]\n"
            )
        )
        return {"passed": False, "exit_code": 1, "runner": "pytest", "output": output}

    monkeypatch.setattr(benchmark, "_apply_local_edit", fake_edit)
    monkeypatch.setattr(benchmark, "_run_case_tests", fake_tests)
    outcome = benchmark._apply_planned_edits(
        repo,
        {
            "prompt": "Repair the owner.",
            "candidate_paths": ["required.py", "optional.py"],
        },
        {},
        [
            {"path": "required.py", "description": "Repair required behavior."},
            {
                "path": "optional.py",
                "description": "Only change if causal.",
                "optional": True,
            },
        ],
        {"report": {}},
        "local-model",
        [],
        1.0,
        stage_prefix="edit",
        failure_output="one contract failed",
    )

    assert outcome["patch_applied"] is True
    assert outcome["applied_files"] == ["required.py"]
    assert outcome["skipped_optional_files"] == ["optional.py"]
    assert outcome["optional_rejected_diffs"][0]["path"] == "optional.py"
    assert "previously passing contract" in outcome["optional_rejected_diffs"][0]["reason"]
    assert "-VALUE = 2" in outcome["optional_rejected_diffs"][0]["attempted_diff"]
    assert "+VALUE = 20" in outcome["optional_rejected_diffs"][0]["attempted_diff"]
    assert (repo / "required.py").read_text(encoding="utf-8") == "VALUE = 10\n"
    assert (repo / "optional.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_edit_group_rolls_back_changed_file_syntax_failure(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    owner = repo / "owner.py"
    owner.write_text("VALUE = 1\n", encoding="utf-8")

    def fake_edit(repo_path, selected, *_args, **_kwargs):
        (repo_path / selected).write_text("def broken(:\n", encoding="utf-8")
        return {"patch_applied": True, "warnings": []}

    monkeypatch.setattr(benchmark, "_apply_local_edit", fake_edit)
    outcome = benchmark._apply_planned_edits(
        repo,
        {"prompt": "Repair the owner.", "candidate_paths": ["owner.py"]},
        {},
        [{"path": "owner.py", "description": "Repair behavior."}],
        {"report": {}},
        "local-model",
        [],
        1.0,
        stage_prefix="edit",
    )

    assert outcome["patch_applied"] is False
    assert any("syntax validation failed" in value for value in outcome["warnings"])
    assert owner.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_edit_group_gets_one_bounded_compiler_guided_correction(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    owner = repo / "owner.py"
    owner.write_text("VALUE = 1\n", encoding="utf-8")
    stages: list[str] = []

    def fake_edit(repo_path, selected, *_args, **kwargs):
        stage = str(kwargs.get("stage") or "")
        stages.append(stage)
        if "compiler_correction" in stage:
            (repo_path / selected).write_text("VALUE = 2\n", encoding="utf-8")
        else:
            (repo_path / selected).write_text("def broken(:\n", encoding="utf-8")
        return {"patch_applied": True, "warnings": []}

    monkeypatch.setattr(benchmark, "_apply_local_edit", fake_edit)
    outcome = benchmark._apply_planned_edits(
        repo,
        {"prompt": "Repair the owner.", "candidate_paths": ["owner.py"]},
        {},
        [{"path": "owner.py", "description": "Repair behavior."}],
        {"report": {}},
        "local-model",
        [],
        1.0,
        stage_prefix="edit",
    )

    assert outcome["patch_applied"] is True
    assert owner.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert any("compiler_correction" in stage for stage in stages)
    assert any("compiler-guided correction" in value for value in outcome["warnings"])


def test_edit_group_rolls_back_when_result_contradicts_mechanism_contract(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    owner = repo / "inflight.ts"
    owner.write_text(
        "const pending = new Map();\nexport async function run(key, task) { return task(); }\n",
        encoding="utf-8",
    )

    def fake_edit(repo_path, selected, *_args, **_kwargs):
        (repo_path / selected).write_text(
            "const pending = new Map();\n"
            "export async function run(key, task) { try { return await task(); } "
            "catch (error) { pending.set(key, { error }); throw error; } }\n",
            encoding="utf-8",
        )
        return {"patch_applied": True, "warnings": []}

    monkeypatch.setattr(benchmark, "_apply_local_edit", fake_edit)
    outcome = benchmark._apply_planned_edits(
        repo,
        {
            "prompt": "A single-flight promise poisons later retry for the same key.",
            "candidate_paths": ["inflight.ts"],
        },
        {},
        [{"path": "inflight.ts", "description": "Repair failed state."}],
        {"report": {}},
        "local-model",
        [],
        1.0,
        stage_prefix="edit",
    )

    assert outcome["patch_applied"] is False
    assert any("contract invariant guard" in value for value in outcome["warnings"])
    assert owner.read_text(encoding="utf-8").endswith("return task(); }\n")


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


def test_initial_plan_receives_deterministic_mechanism_invariants():
    prompt = benchmark._plan_prompt(
        "A single-flight promise poisons later retry for the same key.",
        ["inflight.ts"],
        "### inflight.ts\nconst pending = new Map();",
        {"conclusion": {"dimension": "state"}},
        1,
    )

    assert "Deterministic mechanism invariants" in prompt
    assert "evicted by the state owner" in prompt
    assert '"dimension":"state"' not in prompt
    assert "contract_coverage" in prompt


def test_repair_prompt_source_has_no_state_anchored_schema_or_owner_language():
    source = inspect.getsource(benchmark._repair_after_failure)

    assert '"dimension":"state"' not in source
    assert "state/interface owner" not in source


def test_validation_failure_context_keeps_public_and_feedback_failures():
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
    assert "REPAIR-FEEDBACK FAILURE" in context
    assert "not final adjudication" in context


def test_validation_quality_never_prefers_regression_or_timeout():
    passed = {"passed": True, "exit_code": 0}
    assertion_failure = {"passed": False, "exit_code": 1}
    timeout = {"passed": False, "exit_code": 124}

    assert benchmark._validation_quality(passed, passed) == 3
    assert benchmark._validation_quality(passed, assertion_failure) == 2
    assert benchmark._validation_quality(passed, timeout) == 1
    assert benchmark._validation_quality(assertion_failure, assertion_failure) == 0


def test_validation_progress_rejects_earlier_exception_and_identical_failure():
    public = {"passed": True, "exit_code": 0}
    assertion = {
        "passed": False,
        "exit_code": 1,
        "output": (
            "C:/Temp/chili-fix-one/repo/tests/test_feedback.py:10\n"
            "1 passed, 1 failed\nAssertionError: expected retained row"
        ),
    }
    earlier_exception = {
        "passed": False,
        "exit_code": 1,
        "output": "1 failed\nsqlite3.IntegrityError: FOREIGN KEY constraint failed",
    }
    same_elsewhere = {
        "passed": False,
        "exit_code": 1,
        "output": (
            "C:/Temp/chili-fix-other/repo/tests/test_feedback.py:99\n"
            "1 passed, 1 failed\nAssertionError: expected retained row"
        ),
    }

    assert benchmark._validation_advanced(
        public,
        assertion,
        public,
        earlier_exception,
    ) is False
    assert benchmark._validation_advanced(
        public,
        assertion,
        public,
        same_elsewhere,
    ) is False


def test_validation_progress_rejects_test_swap_and_accepts_stable_resolution():
    public = {"passed": True, "exit_code": 0}
    before = {
        "passed": False,
        "exit_code": 1,
        "runner": "pytest",
        "output": (
            "tests/test_contract.py::test_alpha PASSED [ 33%]\n"
            "tests/test_contract.py::test_beta FAILED [ 66%]\n"
            "tests/test_contract.py::test_gamma FAILED [100%]\n"
        ),
    }
    swapped = {
        "passed": False,
        "exit_code": 1,
        "runner": "pytest",
        "output": (
            "tests/test_contract.py::test_alpha FAILED [ 33%]\n"
            "tests/test_contract.py::test_beta PASSED [ 66%]\n"
            "tests/test_contract.py::test_gamma PASSED [100%]\n"
        ),
    }
    progressed = {
        "passed": False,
        "exit_code": 1,
        "runner": "pytest",
        "output": (
            "tests/test_contract.py::test_alpha PASSED [ 33%]\n"
            "tests/test_contract.py::test_beta PASSED [ 66%]\n"
            "tests/test_contract.py::test_gamma FAILED [100%]\n"
        ),
    }

    assert benchmark._validation_advanced(public, before, public, swapped) is False
    assert benchmark._validation_advanced(public, before, public, progressed) is True


def test_failure_signature_ignores_volatile_object_addresses():
    first = {
        "output": "owner=<inventory.Ledger object at 0x000001ABCDEF0123> failed"
    }
    second = {
        "output": "owner=<inventory.Ledger object at 0x0000099999999999> failed"
    }

    assert benchmark._normalized_failure_signature(first) == benchmark._normalized_failure_signature(
        second
    )


def test_dart_bad_state_contract_can_advance_without_forgetting_prior_passes():
    public = {"passed": True, "exit_code": 0}
    before = {
        "passed": False,
        "exit_code": 255,
        "runner": "dart",
        "test_contract_status": {
            "tests/sync_test.dart::first": "failed",
        },
        "test_contracts_complete": False,
        "output": "Bad state: first",
    }
    after = {
        "passed": False,
        "exit_code": 255,
        "runner": "dart",
        "test_contract_status": {
            "tests/sync_test.dart::first": "passed",
            "tests/sync_test.dart::second": "failed",
        },
        "test_contracts_complete": False,
        "output": "Bad state: second",
    }

    assert benchmark._validation_progress(public, before)[1] == 2
    assert benchmark._validation_advanced(public, before, public, after) is True


def test_attempt_ledger_preserves_rejected_diff_and_adapter_evidence():
    before = {"owner.py": "VALUE = 1\n"}
    after = {"owner.py": "VALUE = 2\n"}
    attempted_diff = benchmark._snapshot_diff(before, after)
    ledger = benchmark._attempt_ledger_context(
        [
            {
                "round": 1,
                "selected_files": ["owner.py"],
                "attempted_diff": attempted_diff,
                "optional_rejected_diffs": [
                    {
                        "path": "context.py",
                        "reason": "regressed a passing contract",
                        "attempted_diff": "-OLD\n+BAD\n",
                        "validation_output": "test_context FAILED",
                    }
                ],
                "adapter_rejection": "SEARCH text was stale",
                "validation_output": "PUBLIC REGRESSION: omitted value changed",
                "warnings": ["rejected"],
            }
        ]
    )

    assert "-VALUE = 1" in ledger
    assert "+VALUE = 2" in ledger
    assert "SEARCH text was stale" in ledger
    assert "omitted value changed" in ledger
    assert "regressed a passing contract" in ledger
    assert "test_context FAILED" in ledger


def test_read_only_feedback_context_is_bounded_and_test_scoped(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_feedback.py").write_text(
        "assert contract()\n",
        encoding="utf-8",
    )
    (tmp_path / "owner.py").write_text("SECRET = True\n", encoding="utf-8")

    context = benchmark._read_only_test_context(
        tmp_path,
        ["tests/test_feedback.py", "owner.py", "../outside.py"],
        max_chars=100,
    )

    assert "assert contract()" in context
    assert "read-only repair feedback" in context
    assert "SECRET" not in context


def test_feedback_imports_map_to_directly_exercised_candidate_boundaries():
    context = (
        "from settings.cli import CliOptions\n"
        'import { Session } from "../src/session.ts";\n'
        'final schema = root / "schema.sql";\n'
    )

    exercised = benchmark._feedback_exercised_candidates(
        context,
        ["settings/cli.py", "src/session.ts", "sql/schema.sql", "unused.py"],
    )

    assert exercised == ["settings/cli.py", "src/session.ts", "sql/schema.sql"]


def test_oracle_test_partitions_require_disjoint_sealed_final_contracts():
    legacy = benchmark._oracle_test_partitions(
        {"hidden_files": {"tests/test_hidden.py": "assert False\n"}}
    )
    sealed = benchmark._oracle_test_partitions(
        {
            "feedback_files": {
                "tests/test_feedback.py": "assert feature() == 'feedback'\n"
            },
            "final_files": {
                "tests/test_final.py": "assert feature() == 'final'\n"
            },
        },
        require_sealed=True,
    )
    external = benchmark._oracle_test_partitions(
        {
            "feedback_files": {
                "tests/test_feedback.py": "assert feature() == 'feedback'\n"
            }
        },
        final_oracle={
            "final_files": {
                "tests/test_final.py": "assert feature() == 'final'\n"
            }
        },
        require_sealed=True,
        require_external_final=True,
    )

    assert legacy["sealed"] is False
    assert legacy["feedback_files"] == legacy["final_files"]
    assert sealed["sealed"] is True
    assert sealed["external_final"] is False
    assert external["external_final"] is True
    assert set(sealed["feedback_files"]) == {"tests/test_feedback.py"}
    assert set(sealed["final_files"]) == {"tests/test_final.py"}

    with pytest.raises(ValueError, match="require disjoint"):
        benchmark._oracle_test_partitions(
            {"hidden_files": {"tests/test_hidden.py": "assert False\n"}},
            require_sealed=True,
        )
    with pytest.raises(ValueError, match="separately loaded final_oracle"):
        benchmark._oracle_test_partitions(
            {
                "feedback_files": {
                    "tests/test_feedback.py": "assert feature() == 'feedback'\n"
                },
                "final_files": {
                    "tests/test_final.py": "assert feature() == 'final'\n"
                },
            },
            require_sealed=True,
            require_external_final=True,
        )
    with pytest.raises(ValueError, match="overlap"):
        benchmark._oracle_test_partitions(
            {
                "feedback_files": {"tests/test_contract.py": "assert False\n"},
                "final_files": {"tests/test_contract.py": "assert False\n"},
            }
        )
    with pytest.raises(ValueError, match="overlap"):
        benchmark._oracle_test_partitions(
            {
                "feedback_files": {"tests/test_contract.py": "assert False\n"},
                "final_files": {"tests/TEST_CONTRACT.PY": "assert True\n"},
            }
        )
    with pytest.raises(ValueError, match="under tests"):
        benchmark._oracle_test_partitions(
            {
                "feedback_files": {"src/owner.py": "raise AssertionError\n"},
                "final_files": {"tests/test_final.py": "assert False\n"},
            }
        )


def test_oracle_tests_cannot_overwrite_seeded_public_contracts():
    case = {
        "repo_files": {
            "owner.py": "VALUE = 0\n",
            "tests/test_public.py": "def test_public():\n    assert True\n",
        }
    }

    with pytest.raises(ValueError, match="Repair-feedback.*overwrite seeded"):
        benchmark._validate_oracle_test_paths(
            case,
            {
                "feedback_files": {
                    "tests/test_public.py": "def test_public():\n    assert False\n"
                }
            },
        )
    with pytest.raises(ValueError, match="Final adjudication.*overwrite seeded"):
        benchmark._validate_oracle_test_paths(
            case,
            {
                "final_files": {
                    "tests/TEST_PUBLIC.PY": "def test_public():\n    assert False\n"
                }
            },
        )


def test_oracle_partitions_require_discoverable_tests_and_respect_runner_cap():
    pytest_case = {
        "test_runner": "pytest",
        "repo_files": {"tests/test_public.py": "def test_public():\n    assert True\n"},
    }
    with pytest.raises(ValueError, match="no discoverable pytest"):
        benchmark._validate_oracle_test_paths(
            pytest_case,
            {"final_files": {"tests/final_helper.py": "VALUE = 1\n"}},
        )

    node_case = {
        "test_runner": "node_test",
        "repo_files": {
            **{
                f"tests/seed-{index}.test.js": "// seeded\n"
                for index in range(benchmark.MAX_TEST_FILES)
            },
        },
    }
    with pytest.raises(ValueError, match="test-file cap"):
        benchmark._validate_oracle_test_paths(
            node_case,
            {"final_files": {"tests/final.test.js": "// final\n"}},
        )


def test_final_adjudication_uses_fresh_repo_without_feedback_tests(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "owner.py").write_text("VALUE = 2\n", encoding="utf-8")
    (candidate / "tests").mkdir()
    (candidate / "tests/test_feedback.py").write_text(
        "raise AssertionError('feedback test must not enter final repo')\n",
        encoding="utf-8",
    )
    case = {
        "candidate_paths": ["owner.py"],
        "repo_files": {
            "owner.py": "VALUE = 0\n",
            "tests/test_public.py": "from owner import VALUE\n\ndef test_public():\n    assert VALUE >= 0\n",
        },
    }
    final_files = {
        "tests/test_final.py": (
            "from pathlib import Path\n"
            "from owner import VALUE\n\n"
            "def test_final():\n"
            "    assert VALUE == 2\n"
            "    assert not Path('tests/test_feedback.py').exists()\n"
        )
    }

    result = benchmark._run_final_adjudication(
        case,
        final_files,
        candidate_repo=candidate,
    )

    assert result["passed"] is True, result["output"]
    assert result["isolated_final_repo"] is True


def test_final_adjudication_fails_closed_when_candidate_overlay_is_missing(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    case = {
        "candidate_paths": ["owner.py"],
        "repo_files": {
            "owner.py": "VALUE = 0\n",
            "tests/test_public.py": "def test_public():\n    assert True\n",
        },
    }

    with pytest.raises(RuntimeError, match="cannot overlay candidate source"):
        benchmark._run_final_adjudication(
            case,
            {"tests/test_final.py": "def test_final():\n    assert True\n"},
            candidate_repo=candidate,
        )


def test_score_uses_final_adjudication_not_green_feedback():
    oracle = {"expected_dimension": "state", "expected_file": "owner.py"}
    diagnosis = {"report": {"conclusion": {"dimension": "state"}}}
    patch = {"changed_files": ["owner.py"], "patch_applied": True}
    passed = {"passed": True}
    failed = {"passed": False}

    score, checks = benchmark._score_case(
        oracle,
        diagnosis,
        patch,
        failed,
        passed,
        failed,
    )

    assert score == 55
    assert checks["baseline_final_failure"] is True
    assert checks["final_tests"] is False


def test_final_adjudication_occurs_after_repair_loop_and_before_no_more_model_calls():
    source = inspect.getsource(benchmark.run)

    repair_loop = source.index("for repair_round, repair_model in enumerate")
    final_oracle_read = source.index("final_oracle = _read_json")
    final_start = source.index("final_tests = _run_final_adjudication")
    model_call_guard = source.index("A model call occurred after final adjudication began")

    assert repair_loop < final_oracle_read < final_start < model_call_guard


def test_blinded_run_rejects_missing_external_final_oracle_before_model_access(
    tmp_path,
    monkeypatch,
):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "manifest.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case": "cases/one.json",
                        "oracle": "oracles/one.json",
                        "evaluation_role": "blinded_holdout",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        benchmark.ollama_client,
        "list_models",
        lambda: (_ for _ in ()).throw(AssertionError("model registry was accessed")),
    )
    args = argparse.Namespace(
        fixture_root=str(fixture),
        case=[],
        validate_fixtures=False,
        model="local-model",
    )

    with pytest.raises(SystemExit, match="separate final_oracle"):
        benchmark.run(args)


def test_fixture_paths_are_contained_and_exist_before_model_access(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture"
    (fixture / "cases").mkdir(parents=True)
    (fixture / "oracles").mkdir()
    (fixture / "cases/one.json").write_text("{}\n", encoding="utf-8")
    (fixture / "oracles/one.json").write_text("{}\n", encoding="utf-8")
    (fixture / "manifest.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case": "cases/one.json",
                        "oracle": "oracles/one.json",
                        "final_oracle": "final/missing.json",
                        "evaluation_role": "blinded_holdout",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        benchmark.ollama_client,
        "list_models",
        lambda: (_ for _ in ()).throw(AssertionError("model registry was accessed")),
    )
    args = argparse.Namespace(
        fixture_root=str(fixture),
        case=[],
        validate_fixtures=False,
        model="local-model",
    )

    with pytest.raises(ValueError, match="does not exist"):
        benchmark.run(args)
    with pytest.raises(ValueError, match="Unsafe or missing"):
        benchmark._fixture_path(fixture, "../outside.json", "case")


def test_candidate_snapshot_restores_only_manifest_approved_sources(tmp_path):
    (tmp_path / "owner.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "unrelated.py").write_text("leave = True\n", encoding="utf-8")
    case = {"candidate_paths": ["owner.py"]}
    snapshot = benchmark._candidate_snapshot(tmp_path, case)

    (tmp_path / "owner.py").write_text("value = 2\n", encoding="utf-8")
    (tmp_path / "unrelated.py").write_text("leave = False\n", encoding="utf-8")
    benchmark._restore_candidate_snapshot(tmp_path, snapshot)

    assert (tmp_path / "owner.py").read_text(encoding="utf-8") == "value = 1\n"
    assert (tmp_path / "unrelated.py").read_text(encoding="utf-8") == "leave = False\n"


def test_deterministic_contract_repairs_pass_real_sealed_contracts(tmp_path):
    expected = {
        "ts-singleflight-401": {"src/inflight.ts", "src/user_service.ts"},
        "ts-abort-chain-402": {"src/provider.ts", "src/retry.ts"},
        "dart-cache-clock-403": {"lib/cache.dart", "lib/cache_entry.dart"},
        "dart-subscription-404": {"lib/subscription.dart", "lib/worker.dart"},
        "sql-partial-unique-405": {"schema.sql"},
        "sql-join-aggregate-406": {"report.sql"},
    }
    for case_id, expected_files in expected.items():
        case = json.loads(
            (FIXTURE_ROOT / "cases" / f"{case_id}.json").read_text(encoding="utf-8")
        )
        oracle = json.loads(
            (FIXTURE_ROOT / "oracles" / f"{case_id}.json").read_text(encoding="utf-8")
        )
        repo = tmp_path / case_id
        benchmark._init_repo(repo, case["repo_files"])
        benchmark._write_files(repo, oracle["hidden_files"])

        repair = benchmark._apply_deterministic_contract_repair(repo, case)
        public = benchmark._run_case_tests(repo, case, public_only=True)
        hidden = benchmark._run_case_tests(repo, case, public_only=False)

        assert repair["patch_applied"] is True, repair
        assert set(repair["selected_files"]) == expected_files
        assert public["passed"] is True, public["output"]
        assert hidden["passed"] is True, hidden["output"]


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


def test_local_edit_retries_one_stale_search_against_current_file(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "owner.ts"
    target.write_text("export const value = 1;\n", encoding="utf-8")
    responses = iter(
        [
            "<<<<<<< SEARCH\nexport const value = 0;\n=======\nexport const value = 2;\n>>>>>>> REPLACE",
            "<<<<<<< SEARCH\nexport const value = 1;\n=======\nexport const value = 2;\n>>>>>>> REPLACE",
        ]
    )
    stages = []

    def fake_call(*_args, stage, **_kwargs):
        stages.append(stage)
        return next(responses)

    monkeypatch.setattr(benchmark, "_local_call", fake_call)

    result = benchmark._apply_local_edit(
        repo,
        "owner.ts",
        "Update the value.",
        "local-model",
        [],
        1.0,
        stage="edit",
    )

    assert result["patch_applied"] is True
    assert stages == ["edit", "edit_retry"]
    assert target.read_text(encoding="utf-8") == "export const value = 2;\n"


def test_local_edit_recognizes_replacement_already_satisfied(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "owner.sql"
    target.write_text("VALUE TEXT NOT NULL\n", encoding="utf-8")
    monkeypatch.setattr(
        benchmark,
        "_local_call",
        lambda *_args, **_kwargs: (
            "<<<<<<< SEARCH\nVALUE INTEGER NOT NULL\n=======\n"
            "VALUE TEXT NOT NULL\n>>>>>>> REPLACE"
        ),
    )

    result = benchmark._apply_local_edit(
        repo,
        "owner.sql",
        "Preserve textual values.",
        "local-model",
        [],
        1.0,
        stage="edit",
    )

    assert result["patch_applied"] is False
    assert result["already_satisfied"] is True
    assert target.read_text(encoding="utf-8") == "VALUE TEXT NOT NULL\n"


def test_local_edit_uses_guarded_full_file_after_repeated_stale_search(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "owner.ts"
    target.write_text("export const value = 1;\n", encoding="utf-8")
    responses = iter(
        [
            "<<<<<<< SEARCH\nexport const value = 0;\n=======\n"
            "export const value = 2;\n>>>>>>> REPLACE",
            "<<<<<<< SEARCH\nexport const value = 3;\n=======\n"
            "export const value = 2;\n>>>>>>> REPLACE",
            "```ts\nexport const value = 2;\n```",
        ]
    )
    stages = []

    def fake_call(*_args, stage, **_kwargs):
        stages.append(stage)
        return next(responses)

    monkeypatch.setattr(benchmark, "_local_call", fake_call)

    result = benchmark._apply_local_edit(
        repo,
        "owner.ts",
        "Update the value.",
        "local-model",
        [],
        1.0,
        stage="edit",
    )

    assert result["patch_applied"] is True
    assert stages == ["edit", "edit_retry", "edit_full_file_retry"]
    assert any("guarded full-file" in value for value in result["warnings"])
    assert target.read_text(encoding="utf-8") == "export const value = 2;\n"


def test_benchmark_model_path_imports_no_cloud_client():
    source = inspect.getsource(benchmark)

    assert "openai_client" not in source
    assert "gateway_chat" not in source
    assert '"premium_calls": 0' in source
