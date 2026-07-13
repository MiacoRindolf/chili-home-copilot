from __future__ import annotations

import argparse
import inspect
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import autopilot_diagnosis_to_fix_benchmark as benchmark


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "autonomy_diagnosis_to_fix"
TENTH_FIXTURE_ROOT = (
    Path(__file__).parent / "fixtures" / "autonomy_diagnosis_to_fix_blinded_tenth"
)
ELEVENTH_FIXTURE_ROOT = (
    Path(__file__).parent / "fixtures" / "autonomy_diagnosis_to_fix_blinded_eleventh"
)
TWELFTH_FIXTURE_ROOT = (
    Path(__file__).parent / "fixtures" / "autonomy_diagnosis_to_fix_blinded_twelfth"
)
FOURTEENTH_FIXTURE_ROOT = (
    Path(__file__).parent / "fixtures" / "autonomy_diagnosis_to_fix_blinded_fourteenth"
)
FIFTEENTH_FIXTURE_ROOT = (
    Path(__file__).parent / "fixtures" / "autonomy_diagnosis_to_fix_blinded_fifteenth"
)


def _write_protocol_fixture(
    root: Path,
    *,
    role: str = "blinded_holdout",
    split: str = "holdout-sealed-python-singlefile",
    feedback_source: str | None = None,
    final_source: str | None = None,
) -> dict[str, Path]:
    (root / "cases").mkdir(parents=True)
    (root / "oracles").mkdir()
    (root / "final_oracles").mkdir()
    case = {
        "case_id": "integrity-case",
        "language": "python",
        "test_runner": "pytest",
        "prompt": "Repair the explicit value contract without changing the public API.",
        "candidate_paths": ["owner.py"],
        "max_files": 1,
        "repo_files": {
            "owner.py": "VALUE = 1\n",
            "tests/test_public.py": (
                "from owner import VALUE\n\n"
                "def test_public():\n"
                "    assert VALUE in {1, 2}\n"
            ),
        },
    }
    oracle = {
        "case_id": "integrity-case",
        "expected_dimension": "code",
        "expected_file": "owner.py",
        "feedback_files": {
            "tests/test_feedback.py": feedback_source
            or (
                "from owner import VALUE\n\n"
                "def test_feedback():\n"
                "    assert VALUE == 2\n"
            )
        },
    }
    final_oracle = {
        "case_id": "integrity-case",
        "final_files": {
            "tests/test_final.py": final_source
            or (
                "from owner import VALUE\n\n"
                "def test_final():\n"
                "    assert VALUE == 2\n"
            )
        },
    }
    manifest = {
        "reference_family": "claude-fable-5",
        "cases": [
            {
                "case": "cases/integrity-case.json",
                "oracle": "oracles/integrity-case.json",
                "final_oracle": "final_oracles/integrity-case.json",
                "evaluation_role": role,
                "split": split,
            }
        ],
    }
    paths = {
        "manifest": root / "manifest.json",
        "case": root / "cases/integrity-case.json",
        "oracle": root / "oracles/integrity-case.json",
        "final": root / "final_oracles/integrity-case.json",
    }
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    paths["case"].write_text(json.dumps(case), encoding="utf-8")
    paths["oracle"].write_text(json.dumps(oracle), encoding="utf-8")
    paths["final"].write_text(json.dumps(final_oracle), encoding="utf-8")
    return paths


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
        evaluation_context="disclosed_replay",
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


def test_local_call_uses_one_total_case_model_deadline(monkeypatch):
    calls = benchmark._ModelCallLedger(deadline=time.monotonic() - 1)
    invoked = False

    def unexpected_chat(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("expired case budget must stop before transport")

    monkeypatch.setattr(benchmark.ollama_client, "chat", unexpected_chat)

    response = benchmark._local_call(
        "local-model",
        [{"role": "user", "content": "repair"}],
        stage="repair",
        calls=calls,
        timeout=240.0,
        num_predict=100,
        json_mode=False,
    )

    assert response == ""
    assert invoked is False
    assert calls[-1]["budget_exhausted"] is True
    assert calls[-1]["error"] == "case_model_time_budget_exhausted"


def test_local_call_records_prompt_timing_and_distinguishes_call_timeout(monkeypatch):
    monkeypatch.setattr(
        benchmark.ollama_client,
        "chat",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=False,
            text="",
            latency_ms=1000,
            tokens_out=0,
            error="TimeoutError: timed out",
            raw={"prompt_eval_count": 77, "prompt_eval_duration": 1234},
        ),
    )
    calls = benchmark._ModelCallLedger(model_time_budget=300.0)

    benchmark._local_call(
        "local-model",
        [{"role": "user", "content": "derive the repair"}],
        stage="repair_plan",
        calls=calls,
        timeout=180.0,
        num_predict=100,
        json_mode=True,
    )

    assert calls[-1]["error_kind"] == "call_timeout"
    assert calls[-1]["prompt_chars"] == len("derive the repair")
    assert calls[-1]["prompt_eval_count"] == 77
    assert calls[-1]["prompt_eval_duration_ns"] == 1234
    assert calls.model_time_used >= 0


def test_qwen3_thinking_is_reserved_for_hypothesis_generation(monkeypatch):
    captured = []

    def fake_chat(*_args, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            ok=True,
            text='{"files":[]}',
            latency_ms=1,
            tokens_out=5,
            error=None,
            raw={"message": {"thinking": "bounded causal analysis"}},
        )

    monkeypatch.setattr(benchmark.ollama_client, "chat", fake_chat)
    calls = benchmark._ModelCallLedger(model_time_budget=30.0)

    benchmark._local_call(
        "qwen3:8b",
        [{"role": "user", "content": "diagnose"}],
        stage="diagnosis_investigator",
        calls=calls,
        timeout=10.0,
        num_predict=100,
        json_mode=True,
    )
    benchmark._local_call(
        "qwen3:8b",
        [{"role": "user", "content": "judge"}],
        stage="diagnosis_judge",
        calls=calls,
        timeout=10.0,
        num_predict=100,
        json_mode=True,
    )
    benchmark._local_call(
        "qwen3:8b",
        [{"role": "user", "content": "plan"}],
        stage="plan",
        calls=calls,
        timeout=10.0,
        num_predict=100,
        json_mode=True,
    )

    assert captured[0]["think"] is True
    assert captured[1]["think"] is False
    assert captured[2]["think"] is False
    assert calls[0]["thinking_enabled"] is True
    assert calls[0]["thinking_chars"] == len("bounded causal analysis")
    assert calls[1]["thinking_enabled"] is False


def test_local_call_labels_deadline_clamped_timeout_as_case_budget(monkeypatch):
    monkeypatch.setattr(
        benchmark.ollama_client,
        "chat",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=False,
            text="",
            latency_ms=1,
            tokens_out=0,
            error="TimeoutError: timed out",
            raw={},
        ),
    )
    calls = benchmark._ModelCallLedger(model_time_budget=0.5)

    benchmark._local_call(
        "local-model",
        [{"role": "user", "content": "repair"}],
        stage="repair_edit",
        calls=calls,
        timeout=180.0,
        num_predict=100,
        json_mode=False,
    )

    assert calls[-1]["case_deadline_clamped"] is True
    assert calls[-1]["error_kind"] == "case_budget_timeout"


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


def test_repair_plan_label_cannot_overwrite_accepted_causal_family():
    diagnosis = {
        "report": {"conclusion": {"dimension": "dependency"}},
        "accepted_conclusion": {
            "dimension": "dependency",
            "stage": "diagnostic_judge",
            "accepted": True,
        },
        "diagnosis_history": [],
    }

    accepted = benchmark._accept_diagnosis_proposal(
        diagnosis,
        "code",
        stage="repair_1_validated",
        validation_evidence="one visible assertion improved",
    )

    assert accepted is False
    assert diagnosis["accepted_conclusion"]["dimension"] == "dependency"
    assert diagnosis["diagnosis_history"][-1]["accepted"] is False
    assert "not independent causal evidence" in diagnosis["diagnosis_history"][-1][
        "rejection_reason"
    ]


def test_initial_diagnosis_keeps_prompt_taxonomy_advisory_without_causal_evidence():
    diagnosis = {
        "report": {"conclusion": {"dimension": "code"}},
        "case": {
            "problem_statement": (
                "Numeric Retry-After exceeds the remaining retry budget and queue time "
                "uses the wrong granted delay."
            )
        },
    }

    benchmark._initialize_accepted_diagnosis(diagnosis)

    assert diagnosis["accepted_conclusion"]["dimension"] == "code"
    assert diagnosis["accepted_conclusion"]["stage"] == "diagnostic_judge"
    assert diagnosis["accepted_conclusion"]["accepted"] is False
    assert diagnosis["accepted_conclusion"]["causal_status"] == "working_hypothesis"
    assert diagnosis["accepted_conclusion"]["taxonomy_advisory_dimension"] == "clock"


def test_decisive_taxonomy_does_not_override_isolated_causal_evidence():
    diagnosis = {
        "report": {
            "conclusion": {
                "dimension": "runtime",
                "causal_sufficiency": "isolated",
                "status": "confirmed",
            }
        },
        "case": {
            "problem_statement": (
                "Numeric Retry-After exceeds the remaining retry budget and queue time "
                "uses the wrong granted delay."
            )
        },
    }

    benchmark._initialize_accepted_diagnosis(diagnosis)

    assert diagnosis["accepted_conclusion"]["dimension"] == "runtime"
    assert diagnosis["accepted_conclusion"]["stage"] == "diagnostic_judge"
    assert diagnosis["accepted_conclusion"]["accepted"] is True


def test_same_family_partial_repair_progress_does_not_claim_causal_acceptance():
    diagnosis = {
        "report": {"conclusion": {"dimension": "state"}},
        "accepted_conclusion": {
            "dimension": "state",
            "stage": "diagnostic_judge",
            "accepted": False,
        },
        "diagnosis_history": [],
    }

    recorded = benchmark._accept_diagnosis_proposal(
        diagnosis,
        "state",
        stage="repair_1_validated",
        validation_evidence="one feedback contract improved while another remains red",
    )

    assert recorded is True
    assert diagnosis["accepted_conclusion"]["accepted"] is False
    assert diagnosis["accepted_conclusion"]["repair_progress_validated"] is True
    assert diagnosis["accepted_conclusion"]["causal_status"] == "working_hypothesis"
    assert diagnosis["accepted_conclusion"]["stage"] == "repair_1_validated"


def test_completed_source_intervention_accepts_revised_causal_family():
    diagnosis = {
        "report": {"conclusion": {"dimension": "code"}},
        "accepted_conclusion": {
            "dimension": "code",
            "stage": "diagnostic_judge",
            "accepted": False,
        },
        "diagnosis_history": [],
    }

    accepted = benchmark._accept_validated_contract_repair_diagnosis(
        diagnosis,
        "dependency",
        stage="generative_repair_2_validated",
        validation_evidence="public stayed green and every red feedback contract became green",
    )

    assert accepted is True
    assert diagnosis["accepted_conclusion"]["dimension"] == "dependency"
    assert diagnosis["accepted_conclusion"]["accepted"] is True
    assert diagnosis["accepted_conclusion"]["causal_sufficiency"] == "isolated"
    assert diagnosis["accepted_conclusion"]["repair_progress_validated"] is True


def test_validated_repair_dimension_prefers_proven_mechanism_family():
    case = {
        "prompt": (
            "Package lengths use a supported unit table; volume and oversized routing "
            "must normalize every non-centimeter dimension."
        )
    }

    assert benchmark._validated_repair_dimension(case, "code") == "data"
    assert benchmark._validated_repair_dimension(
        {"prompt": "A generic source defect."},
        "code",
    ) == "code"


def test_completed_repair_cannot_override_independently_accepted_family():
    diagnosis = {
        "report": {"conclusion": {"dimension": "runtime"}},
        "accepted_conclusion": {
            "dimension": "runtime",
            "stage": "diagnostic_judge",
            "accepted": True,
        },
        "diagnosis_history": [],
    }

    accepted = benchmark._accept_validated_contract_repair_diagnosis(
        diagnosis,
        "code",
        stage="generative_repair_1_validated",
        validation_evidence="feedback passed",
    )

    assert accepted is False
    assert diagnosis["accepted_conclusion"]["dimension"] == "runtime"
    assert "did not override" in diagnosis["diagnosis_history"][-1][
        "rejection_reason"
    ]


def test_sealed_failure_retracts_validated_repair_causality():
    diagnosis = {
        "accepted_conclusion": {
            "dimension": "state",
            "stage": "generative_repair_1_validated",
            "accepted": True,
            "causal_status": "accepted",
            "repair_progress_validated": True,
        },
        "diagnosis_history": [],
    }

    retracted = benchmark._retract_unclosed_validated_diagnosis(
        diagnosis,
        {"passed": False},
    )

    assert retracted is True
    assert diagnosis["accepted_conclusion"]["accepted"] is False
    assert diagnosis["accepted_conclusion"]["causal_status"] == "retracted"
    assert "sealed final" in diagnosis["accepted_conclusion"]["retraction_reason"]


def test_prompt_contract_closure_surfaces_hidden_boundary_without_final_oracle(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "settings.py").write_text(
        "def reload_config(payload):\n    current.update(payload)\n",
        encoding="utf-8",
    )
    case = {
        "prompt": "A replacement config reload must clear omitted overrides.",
        "candidate_paths": ["settings.py"],
    }

    unresolved = benchmark._prompt_contract_closure(repo, case)

    assert len(unresolved) == 1
    contract_id, detail = next(iter(unresolved.items()))
    assert contract_id.startswith("prompt_contract::")
    assert "retained configuration" in detail

    (repo / "settings.py").write_text(
        "def reload_config(payload):\n    current = {**DEFAULTS, **payload}\n",
        encoding="utf-8",
    )
    assert benchmark._prompt_contract_closure(repo, case) == {}


def test_shadow_verdict_requires_every_case_check_not_only_high_average():
    passing = {
        "checks": {"diagnosis": True, "final_tests": True},
        "live_reasoning_qualified": True,
    }
    one_miss = {
        "checks": {"diagnosis": False, "final_tests": True},
        "live_reasoning_qualified": True,
    }
    deterministic_only = {
        "checks": {"diagnosis": True, "final_tests": True},
        "live_reasoning_qualified": False,
    }

    assert benchmark._verdict([passing, passing]) == "shadow_ready"
    assert benchmark._verdict([passing, one_miss]) == "needs_improvement"
    assert benchmark._verdict([deterministic_only]) == "needs_improvement"
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

    def fake_bundle(repo_path, paths, *_args, **_kwargs):
        (repo_path / paths[0]).write_text("VALUE = 10\n", encoding="utf-8")
        return {
            "patch_applied": False,
            "applied_files": [],
            "satisfied_files": [],
            "warnings": ["rejected second edit"],
        }

    monkeypatch.setattr(benchmark, "_apply_local_edit_bundle", fake_bundle)
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
        allow_model_recovery=False,
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

    def fake_bundle(repo_path, paths, *_args, **_kwargs):
        (repo_path / paths[1]).write_text("VALUE = 20\n", encoding="utf-8")
        return {
            "patch_applied": True,
            "applied_files": ["second.py"],
            "satisfied_files": ["first.py"],
            "warnings": [],
        }

    monkeypatch.setattr(benchmark, "_apply_local_edit_bundle", fake_bundle)
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
        allow_model_recovery=False,
    )

    assert outcome["patch_applied"] is True
    assert outcome["satisfied_files"] == ["first.py"]
    assert outcome["applied_files"] == ["second.py"]
    assert (repo / "second.py").read_text(encoding="utf-8") == "VALUE = 20\n"


def test_failed_multifile_group_salvages_only_isolated_contract_progress(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "first.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "second.py").write_text("VALUE = 2\n", encoding="utf-8")
    originals = {"first.py": "VALUE = 1\n", "second.py": "VALUE = 2\n"}
    attempted = {"first.py": "VALUE = 10\n", "second.py": "BROKEN =\n"}

    def fake_syntax(_repo, *, changed_files):
        failed = "second.py" in changed_files
        return SimpleNamespace(exit_code=1 if failed else 0, timed_out=False)

    def fake_tests(repo_path, _case, *, public_only):
        first_changed = (repo_path / "first.py").read_text(encoding="utf-8") == "VALUE = 10\n"
        if public_only:
            return {"passed": True, "exit_code": 0, "test_contract_status": {"public": "passed"}}
        return {
            "passed": first_changed,
            "exit_code": 0 if first_changed else 1,
            "test_contract_status": {
                "tests/test_contract.py::test_value": "passed" if first_changed else "failed"
            },
            "test_contracts_complete": True,
        }

    monkeypatch.setattr(benchmark.validator_runner, "run_ast_syntax", fake_syntax)
    monkeypatch.setattr(benchmark, "_run_case_tests", fake_tests)

    result = benchmark._salvage_progressing_edit_subset(
        repo,
        {"candidate_paths": ["first.py", "second.py"]},
        originals,
        attempted,
        {"passed": True, "exit_code": 0, "test_contract_status": {"public": "passed"}},
        {
            "passed": False,
            "exit_code": 1,
            "test_contract_status": {"tests/test_contract.py::test_value": "failed"},
            "test_contracts_complete": True,
        },
    )

    assert result["applied_files"] == ["first.py"]
    assert (repo / "first.py").read_text(encoding="utf-8") == "VALUE = 10\n"
    assert (repo / "second.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_multifile_bundle_is_generated_once_and_applied_atomically(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "producer.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "consumer.py").write_text("SEEN = 1\n", encoding="utf-8")
    stages: list[str] = []

    def fake_call(*_args, stage, **_kwargs):
        stages.append(stage)
        return json.dumps(
            {
                "edits": [
                    {
                        "path": "producer.py",
                        "blocks": [{"search": "VALUE = 1", "replace": "VALUE = 2"}],
                    },
                    {
                        "path": "consumer.py",
                        "blocks": [{"search": "SEEN = 1", "replace": "SEEN = 2"}],
                    },
                ]
            }
        )

    monkeypatch.setattr(benchmark, "_local_call", fake_call)

    outcome = benchmark._apply_local_edit_bundle(
        repo,
        ["producer.py", "consumer.py"],
        "Keep producer and consumer on the same generation.",
        "local-model",
        [],
        1.0,
        stage="coordinated_bundle",
    )

    assert stages == ["coordinated_bundle"]
    assert outcome["patch_applied"] is True
    assert outcome["applied_files"] == ["producer.py", "consumer.py"]
    assert (repo / "producer.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (repo / "consumer.py").read_text(encoding="utf-8") == "SEEN = 2\n"


def test_multifile_bundle_recovers_once_from_stale_search(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "producer.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "consumer.py").write_text("SEEN = 1\n", encoding="utf-8")
    stages: list[str] = []

    def fake_call(*_args, stage, **_kwargs):
        stages.append(stage)
        producer_search = "VALUE = stale" if len(stages) == 1 else "VALUE = 1"
        return json.dumps(
            {
                "edits": [
                    {
                        "path": "producer.py",
                        "blocks": [
                            {"search": producer_search, "replace": "VALUE = 2"}
                        ],
                    },
                    {
                        "path": "consumer.py",
                        "blocks": [{"search": "SEEN = 1", "replace": "SEEN = 2"}],
                    },
                ]
            }
        )

    monkeypatch.setattr(benchmark, "_local_call", fake_call)

    outcome = benchmark._apply_local_edit_bundle(
        repo,
        ["producer.py", "consumer.py"],
        "Keep producer and consumer on the same generation.",
        "local-model",
        [],
        1.0,
        stage="coordinated_bundle",
    )

    assert stages == ["coordinated_bundle", "coordinated_bundle_adapter_retry"]
    assert outcome["patch_applied"] is True
    assert "Recovered the atomic bundle" in outcome["warnings"][0]
    assert (repo / "producer.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (repo / "consumer.py").read_text(encoding="utf-8") == "SEEN = 2\n"


def test_ambiguous_search_rejection_is_retryable():
    assert benchmark._retryable_edit_adapter_rejection(
        ["block 1: SEARCH text matches 14 times - not unique, add surrounding lines"]
    ) is True


def test_multifile_bundle_recovers_once_from_nonunique_search(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "producer.py").write_text(
        "PRIMARY = 1\nMARK = 1\nSECONDARY = 1\nMARK = 1\n",
        encoding="utf-8",
    )
    (repo / "consumer.py").write_text("SEEN = 1\n", encoding="utf-8")
    stages: list[str] = []
    prompts: list[list[dict[str, str]]] = []

    def fake_call(*args, stage, **_kwargs):
        stages.append(stage)
        prompts.append(args[1])
        producer_search = (
            "MARK = 1"
            if len(stages) == 1
            else "PRIMARY = 1\nMARK = 1"
        )
        producer_replace = (
            "MARK = 2"
            if len(stages) == 1
            else "PRIMARY = 2\nMARK = 2"
        )
        return json.dumps(
            {
                "edits": [
                    {
                        "path": "producer.py",
                        "blocks": [
                            {"search": producer_search, "replace": producer_replace}
                        ],
                    },
                    {
                        "path": "consumer.py",
                        "blocks": [{"search": "SEEN = 1", "replace": "SEEN = 2"}],
                    },
                ]
            }
        )

    monkeypatch.setattr(benchmark, "_local_call", fake_call)

    outcome = benchmark._apply_local_edit_bundle(
        repo,
        ["producer.py", "consumer.py"],
        "Keep producer and consumer on the same generation.",
        "local-model",
        [],
        1.0,
        stage="coordinated_bundle",
    )

    assert stages == ["coordinated_bundle", "coordinated_bundle_adapter_retry"]
    assert outcome["patch_applied"] is True
    assert "Recovered the atomic bundle" in outcome["warnings"][0]
    assert "must match exactly once" in prompts[0][0]["content"]
    assert "stale or ambiguous SEARCH" in prompts[1][1]["content"]
    assert (repo / "producer.py").read_text(encoding="utf-8") == (
        "PRIMARY = 2\nMARK = 2\nSECONDARY = 1\nMARK = 1\n"
    )
    assert (repo / "consumer.py").read_text(encoding="utf-8") == "SEEN = 2\n"


def test_multifile_bundle_rejects_omitted_owner_without_partial_write(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "producer.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "consumer.py").write_text("SEEN = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        benchmark,
        "_local_call",
        lambda *_args, **_kwargs: json.dumps(
            {
                "edits": [
                    {
                        "path": "producer.py",
                        "blocks": [{"search": "VALUE = 1", "replace": "VALUE = 2"}],
                    }
                ]
            }
        ),
    )

    outcome = benchmark._apply_local_edit_bundle(
        repo,
        ["producer.py", "consumer.py"],
        "Coordinate both owners.",
        "local-model",
        [],
        1.0,
        stage="coordinated_bundle",
    )

    assert outcome["patch_applied"] is False
    assert any("omitted required path consumer.py" in value for value in outcome["warnings"])
    assert (repo / "producer.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_base_multifile_repairs_use_one_atomic_bundle_with_adapter_recovery(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "producer.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "consumer.py").write_text("SEEN = 1\n", encoding="utf-8")
    bundled: list[list[str]] = []

    def fake_bundle(repo_path, paths, *_args, allow_adapter_recovery, **_kwargs):
        bundled.append(list(paths))
        assert allow_adapter_recovery is True
        for selected in paths:
            target = repo_path / selected
            target.write_text(
                target.read_text(encoding="utf-8").replace("1", "2"),
                encoding="utf-8",
            )
        return {
            "patch_applied": True,
            "applied_files": list(paths),
            "satisfied_files": [],
            "warnings": [],
        }

    monkeypatch.setattr(
        benchmark,
        "_apply_local_edit_bundle",
        fake_bundle,
    )
    monkeypatch.setattr(
        benchmark,
        "_apply_local_edit",
        lambda *_args, **_kwargs: pytest.fail("multi-owner repair must stay coordinated"),
    )

    outcome = benchmark._apply_planned_edits(
        repo,
        {
            "prompt": "Coordinate both modules.",
            "candidate_paths": ["producer.py", "consumer.py"],
        },
        {},
        [
            {"path": "producer.py", "description": "Update producer."},
            {"path": "consumer.py", "description": "Update consumer."},
        ],
        {"report": {}},
        "local-model",
        [],
        1.0,
        stage_prefix="edit",
        allow_model_recovery=True,
    )

    assert outcome["patch_applied"] is True
    assert bundled == [["producer.py", "consumer.py"]]


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


def test_safe_dart_compiler_repair_qualifies_undefined_max(tmp_path):
    repo = tmp_path / "repo"
    (repo / "lib").mkdir(parents=True)
    target = repo / "lib" / "join.dart"
    target.write_text(
        "import 'types.dart';\n\nint join(int left, int right) => max(left, right);\n",
        encoding="utf-8",
    )
    diagnostics = (
        "ERROR|COMPILE_TIME_ERROR|UNDEFINED_FUNCTION|lib/join.dart|3|"
        "The function 'max' isn't defined."
    )

    warnings = benchmark._apply_safe_compiler_repair(
        repo,
        "lib/join.dart",
        diagnostics,
    )

    repaired = target.read_text(encoding="utf-8")
    assert warnings
    assert "import 'dart:math' as math;" in repaired
    assert "math.max(left, right)" in repaired
    assert "Map<String, int>" in benchmark._compiler_diagnostic_guidance(
        "lib/join.dart",
        "Map<dynamic, dynamic> cannot be assigned",
    )


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
                {
                    "path": "producer.py",
                    "action": "modify",
                    "description": "Copy rows defensively.",
                },
                {
                    "path": "consumer.py",
                    "action": "modify",
                    "description": "No changes needed in this file.",
                },
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
    assert "required_primitives" in prompt
    assert "forbidden_shortcuts" in prompt
    assert "executable ordered algorithm" in prompt


def test_repair_prompt_source_has_no_state_anchored_schema_or_owner_language():
    source = inspect.getsource(benchmark._repair_after_failure)

    assert '"dimension":"state"' not in source
    assert "state/interface owner" not in source


def test_repair_review_skip_requires_complete_stable_contract_ownership():
    plan = {
        "files": [
            {"path": "producer.py", "action": "modify", "description": "Fix producer."},
            {"path": "consumer.py", "action": "modify", "description": "Fix consumer."},
        ],
        "contract_coverage": [
            {
                "contract": "test_alpha preserves every repeated value",
                "owner_paths": ["producer.py"],
                "postcondition": "All repeated values survive canonicalization.",
            },
            {
                "contract": "test_beta uses the normalized consumer key",
                "owner_paths": ["consumer.py"],
                "postcondition": "Lookup and storage use the same normalized key.",
            },
        ],
    }
    evidence = {
        "failed_ids": [
            "tests/test_contract.py::test_alpha",
            "tests/test_contract.py::test_beta",
        ]
    }

    assert benchmark._repair_plan_has_complete_contract_coverage(
        plan,
        ["producer.py", "consumer.py"],
        evidence,
    ) is True
    assert benchmark._repair_plan_has_complete_contract_coverage(
        {**plan, "contract_coverage": plan["contract_coverage"][:1]},
        ["producer.py", "consumer.py"],
        evidence,
    ) is False
    assert benchmark._repair_plan_has_complete_contract_coverage(
        {
            **plan,
            "contract_coverage": [
                {
                    **plan["contract_coverage"][0],
                    "owner_paths": ["tests/test_contract.py"],
                },
                plan["contract_coverage"][1],
            ],
        },
        ["producer.py", "consumer.py"],
        evidence,
    ) is False


def test_contract_coverage_deduplicates_pytest_error_suffix_and_accepts_unique_short_ids():
    plan = {
        "files": [
            {"path": "owner.py", "action": "modify", "description": "Fix both contracts."}
        ],
        "contract_coverage": [
            {
                "contract": "test_alpha preserves provenance",
                "owner_paths": ["owner.py"],
                "postcondition": "Provenance survives a checkpoint round trip.",
            },
            {
                "contract": "tests/test_owner.py::test_beta",
                "owner_paths": ["owner.py"],
                "postcondition": "Replacement input is read from its beginning.",
            },
        ],
    }
    evidence = {
        "failed_ids": [
            "tests/test_owner.py::test_alpha",
            "tests/test_owner.py::test_alpha - TypeError: old signature",
            "tests/test_owner.py::test_beta",
        ]
    }

    assert benchmark._repair_plan_has_complete_contract_coverage(
        plan,
        ["owner.py"],
        evidence,
    ) is True


def test_contract_coverage_count_cannot_hide_an_unmapped_failure():
    plan = {
        "files": [
            {"path": "owner.py", "action": "modify", "description": "Fix alpha."}
        ],
        "contract_coverage": [
            {
                "contract": "test_alpha first assertion",
                "owner_paths": ["owner.py"],
                "postcondition": "The alpha output is retained.",
            },
            {
                "contract": "test_alpha second assertion",
                "owner_paths": ["owner.py"],
                "postcondition": "The alpha identity is retained.",
            },
        ],
    }

    assert benchmark._repair_plan_has_complete_contract_coverage(
        plan,
        ["owner.py"],
        {
            "failed_ids": [
                "tests/test_owner.py::test_alpha",
                "tests/test_owner.py::test_beta",
            ]
        },
    ) is False


def test_generic_contract_coverage_is_bound_to_explicit_failed_ids_with_owner_evidence():
    plan = {
        "files": [
            {"path": "first.sql", "action": "modify", "description": "Fix first."},
            {"path": "second.sql", "action": "modify", "description": "Fix second."},
        ],
        "contract_coverage": [
            {
                "contract": "configured delimiter invariant",
                "owner_paths": ["first.sql", "second.sql"],
                "postcondition": "Both renderers use the configured delimiter consistently.",
            }
        ],
    }
    evidence = {
        "failed_ids": [
            "tests/test_rows.py::test_first_row",
            "tests/test_rows.py::test_second_row",
        ]
    }

    canonical = benchmark._canonicalize_generic_repair_contract_coverage(
        plan,
        ["first.sql", "second.sql", "distractor.sql"],
        evidence,
        ["first.sql", "second.sql"],
    )

    assert benchmark._repair_plan_has_complete_contract_coverage(
        canonical,
        ["first.sql", "second.sql", "distractor.sql"],
        evidence,
    ) is True
    assert [item["contract"] for item in canonical["contract_coverage"]] == [
        "configured delimiter invariant",
        "tests/test_rows.py::test_first_row",
        "tests/test_rows.py::test_second_row",
    ]


def test_partial_explicit_contract_mapping_is_not_auto_completed():
    plan = {
        "files": [
            {"path": "owner.py", "action": "modify", "description": "Fix alpha."}
        ],
        "contract_coverage": [
            {
                "contract": "tests/test_owner.py::test_alpha",
                "owner_paths": ["owner.py"],
                "postcondition": "The alpha value is retained.",
            }
        ],
    }
    evidence = {
        "failed_ids": [
            "tests/test_owner.py::test_alpha",
            "tests/test_owner.py::test_beta",
        ]
    }

    canonical = benchmark._canonicalize_generic_repair_contract_coverage(
        plan,
        ["owner.py"],
        evidence,
        ["owner.py"],
    )

    assert canonical == plan
    assert benchmark._repair_plan_has_complete_contract_coverage(
        canonical,
        ["owner.py"],
        evidence,
    ) is False


def test_prompt_contract_closure_uses_semantic_guard_not_a_fake_test_identity():
    plan = {
        "files": [
            {"path": "owner.py", "action": "modify", "description": "Close the invariant."}
        ],
        "contract_coverage": [
            {
                "contract": "replacement reload boundary",
                "owner_paths": ["owner.py"],
                "postcondition": "Omitted overrides no longer survive a replacement reload.",
            }
        ],
    }

    assert benchmark._repair_plan_has_complete_contract_coverage(
        plan,
        ["owner.py"],
        {
            "failed_ids": [],
            "prompt_contract_details": {
                "prompt_contract::abc": "replacement reload retained old configuration"
            },
        },
    ) is True


def test_prompt_obligation_coverage_requires_stable_ids_and_negative_polarity():
    prompt = (
        "A compact envelope uses a varint prefix and advances a cursor. Malformed or non-canonical "
        "prefixes must remain rejected."
    )
    obligations = benchmark._prompt_contract_obligations(prompt)
    assert {item["polarity"] for item in obligations.values()} == {
        "required",
        "forbidden",
    }
    coverage = [
        {
            "contract": obligation_id,
            "owner_paths": ["owner.dart"],
            "postcondition": detail["statement"],
            "polarity": detail["polarity"],
        }
        for obligation_id, detail in obligations.items()
    ]
    plan = {
        "files": [
            {"path": "owner.dart", "action": "modify", "description": "Repair framing."}
        ],
        "contract_coverage": coverage,
    }
    evidence = {"prompt_obligation_details": obligations}

    assert benchmark._repair_plan_has_complete_contract_coverage(
        plan, ["owner.dart"], evidence
    ) is True
    assert benchmark._repair_plan_has_complete_contract_coverage(
        {**plan, "contract_coverage": coverage[:1]}, ["owner.dart"], evidence
    ) is False
    wrong_polarity = [dict(item) for item in coverage]
    wrong_polarity[-1]["polarity"] = "required"
    assert benchmark._repair_plan_has_complete_contract_coverage(
        {**plan, "contract_coverage": wrong_polarity}, ["owner.dart"], evidence
    ) is False


def test_contract_owner_mapping_replaces_unowned_draft_file_selection():
    plan = benchmark._align_plan_files_to_contract_coverage(
        {
            "files": [
                {
                    "path": "distractor.py",
                    "action": "modify",
                    "description": "Change the wrapper.",
                }
            ],
            "contract_coverage": [
                {
                    "contract": "tests/test_owner.py::test_value",
                    "owner_paths": ["owner.py"],
                    "postcondition": "The owner returns the required value.",
                }
            ],
        },
        ["owner.py", "distractor.py"],
        2,
    )

    assert plan["files"] == [
        {
            "path": "owner.py",
            "action": "modify",
            "description": "Implement the owned validation contracts: The owner returns the required value.",
        }
    ]


def test_contract_owner_alignment_refuses_to_truncate_required_owner_union():
    draft = {
        "files": [
            {"path": "first.py", "action": "modify", "description": "Fix first."},
            {"path": "second.py", "action": "modify", "description": "Fix second."},
        ],
        "contract_coverage": [
            {
                "contract": "tests/test_contract.py::test_cross_file",
                "owner_paths": ["first.py", "second.py"],
                "postcondition": "Both sides of the interface change together.",
            }
        ],
    }

    aligned = benchmark._align_plan_files_to_contract_coverage(
        draft,
        ["first.py", "second.py"],
        1,
    )

    assert [item["path"] for item in aligned["files"]] == ["first.py", "second.py"]
    assert aligned["contract_owner_budget_exceeded"] == {
        "max_files": 1,
        "required_owner_paths": ["first.py", "second.py"],
    }
    assert benchmark._repair_plan_has_complete_contract_coverage(
        aligned,
        ["first.py", "second.py"],
        {"failed_ids": ["tests/test_contract.py::test_cross_file"]},
    ) is False


def test_partial_validation_progress_is_explicitly_provisional_not_completed():
    patch = {"changed_files": ["owner.py"], "warnings": []}

    benchmark._mark_repair_completion(
        patch,
        {"passed": True},
        {"passed": False},
        {},
    )

    assert patch["patch_applied"] is False
    assert patch["repair_contract_complete"] is False
    assert patch["provisional_patch_applied"] is True
    assert any("provisional evidence" in warning for warning in patch["warnings"])

    benchmark._mark_repair_completion(
        patch,
        {"passed": True},
        {"passed": True},
        {},
    )
    assert patch["patch_applied"] is True
    assert patch["repair_contract_complete"] is True
    assert patch["provisional_patch_applied"] is False


def test_contract_alignment_preserves_executable_editor_handoff_fields():
    draft = {
        "files": [
            {
                "path": "owner.py",
                "action": "modify",
                "description": "Repair the owner.",
                "algorithm": "Normalize once, then publish the validated value.",
                "required_primitives": ["normalize_value"],
                "forbidden_shortcuts": ["normalizing only at the caller"],
            }
        ],
        "contract_coverage": [
            {
                "contract": "tests/test_owner.py::test_value",
                "owner_paths": ["owner.py"],
                "postcondition": "The normalized value is published once.",
            }
        ],
    }

    aligned = benchmark._align_plan_files_to_contract_coverage(
        draft,
        ["owner.py"],
        1,
    )
    changed_algorithm = {
        **aligned,
        "files": [
            {
                **aligned["files"][0],
                "algorithm": "Publish first, then normalize the visible value.",
            }
        ],
    }

    assert aligned["files"][0]["required_primitives"] == ["normalize_value"]
    assert aligned["files"][0]["forbidden_shortcuts"] == [
        "normalizing only at the caller"
    ]
    assert benchmark._repair_plan_fingerprint(aligned) != benchmark._repair_plan_fingerprint(
        changed_algorithm
    )


def test_complete_contract_repair_plan_skips_redundant_model_review(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "owner.py").write_text("VALUE = 1\n", encoding="utf-8")
    stages: list[str] = []
    prompts: list[str] = []

    def fake_call(*args, stage, **_kwargs):
        stages.append(stage)
        prompts.append(args[1][-1]["content"])
        if "repair_review" in stage:
            raise AssertionError("complete contract plan should not invoke reviewer")
        return json.dumps(
            {
                "dimension": "clock",
                "analysis": "The owner violates the numeric delay contract.",
                "files": [
                    {
                        "path": "owner.py",
                        "action": "modify",
                        "description": "Convert the numeric delay once.",
                    }
                ],
                "contract_coverage": [
                    {
                        "contract": "test_numeric_delay converts seconds",
                        "owner_paths": ["owner.py"],
                        "postcondition": "Numeric delay is converted exactly once.",
                    }
                ],
            }
        )

    monkeypatch.setattr(benchmark, "_local_call", fake_call)
    monkeypatch.setattr(
        benchmark,
        "_apply_planned_edits",
        lambda *_args, **_kwargs: {
            "patch_applied": True,
            "applied_files": ["owner.py"],
            "warnings": [],
        },
    )

    result = benchmark._repair_after_failure(
        repo,
        {
            "prompt": "Numeric Retry-After delay is interpreted in the wrong clock unit.",
            "candidate_paths": ["owner.py"],
            "max_files": 1,
        },
        {"report": {"conclusion": {"dimension": "clock"}}},
        {"selected_files": ["owner.py"]},
        "tests/test_delay.py::test_numeric_delay FAILED",
        "local-model",
        [],
        1.0,
        1,
        feedback_context="from owner import VALUE",
        contract_evidence={
            "failed_ids": ["tests/test_delay.py::test_numeric_delay"],
        },
    )

    assert stages == ["repair_plan_1"]
    assert result["plan"]["review_skipped_reason"]
    assert result["patch_applied"] is True
    assert "copy each id verbatim" in prompts[0]
    assert "tests/test_delay.py::test_numeric_delay" in prompts[0]
    assert "Feedback source-reference hints" in prompts[0]
    assert '"owner.py"' in prompts[0]


def test_empty_repair_plan_stops_before_review_and_cannot_gain_feedback_edit_authority(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "owner.py").write_text("VALUE = 1\n", encoding="utf-8")
    stages: list[str] = []

    def failed_call(*_args, stage, **_kwargs):
        stages.append(stage)
        return ""

    monkeypatch.setattr(benchmark, "_local_call", failed_call)
    monkeypatch.setattr(
        benchmark,
        "_apply_planned_edits",
        lambda *_args, **_kwargs: pytest.fail("empty plan must not authorize edits"),
    )

    result = benchmark._repair_after_failure(
        repo,
        {"prompt": "Repair the owner.", "candidate_paths": ["owner.py"]},
        {"report": {"conclusion": {"dimension": "code"}}},
        {},
        "tests/test_owner.py::test_value FAILED",
        "local-model",
        [],
        1.0,
        1,
        feedback_context="from owner import VALUE",
        contract_evidence={"failed_ids": ["tests/test_owner.py::test_value"]},
    )

    assert stages == ["repair_plan_1"]
    assert result["transport_failed"] is True
    assert result["selected_files"] == []


def test_repeated_plan_fingerprint_does_not_fan_out_edits(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "owner.py").write_text("VALUE = 1\n", encoding="utf-8")
    plan = {
        "dimension": "code",
        "analysis": "The value owner violates the visible contract.",
        "files": [
            {
                "path": "owner.py",
                "action": "modify",
                "description": "Set the required value.",
            }
        ],
        "contract_coverage": [
            {
                "contract": "tests/test_owner.py::test_value",
                "owner_paths": ["owner.py"],
                "postcondition": "The owner returns the required value.",
            }
        ],
    }
    edit_calls = 0

    def fake_apply(*_args, **_kwargs):
        nonlocal edit_calls
        edit_calls += 1
        return {"patch_applied": True, "applied_files": ["owner.py"], "warnings": []}

    monkeypatch.setattr(
        benchmark,
        "_local_call",
        lambda *_args, **_kwargs: json.dumps(plan),
    )
    monkeypatch.setattr(benchmark, "_apply_planned_edits", fake_apply)
    fingerprints: set[str] = set()
    kwargs = {
        "feedback_context": "from owner import VALUE",
        "contract_evidence": {"failed_ids": ["tests/test_owner.py::test_value"]},
        "failure_signature": "same-red-contract",
        "attempted_plan_fingerprints": fingerprints,
    }

    first = benchmark._repair_after_failure(
        repo,
        {"prompt": "Repair the value.", "candidate_paths": ["owner.py"]},
        {"report": {"conclusion": {"dimension": "code"}}},
        {},
        "tests/test_owner.py::test_value FAILED",
        "local-model",
        [],
        1.0,
        1,
        **kwargs,
    )
    second = benchmark._repair_after_failure(
        repo,
        {"prompt": "Repair the value.", "candidate_paths": ["owner.py"]},
        {"report": {"conclusion": {"dimension": "code"}}},
        {},
        "tests/test_owner.py::test_value FAILED",
        "local-model",
        [],
        1.0,
        2,
        **kwargs,
    )

    assert first["patch_applied"] is True
    assert second["patch_applied"] is False
    assert second["duplicate_plan"] is True
    assert edit_calls == 1


def test_contract_owner_plan_edits_mapped_owner_instead_of_distractor(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "owner.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "distractor.py").write_text("OTHER = 1\n", encoding="utf-8")
    plan = {
        "dimension": "code",
        "files": [
            {
                "path": "distractor.py",
                "action": "modify",
                "description": "Change the unrelated wrapper.",
            }
        ],
        "contract_coverage": [
            {
                "contract": "test_value",
                "owner_paths": ["owner.py"],
                "postcondition": "The owner returns the required value.",
            }
        ],
    }
    monkeypatch.setattr(
        benchmark,
        "_local_call",
        lambda *_args, **_kwargs: json.dumps(plan),
    )
    captured: dict[str, object] = {}

    def fake_apply(_repo, _case, _plan, selected, *_args, **_kwargs):
        captured["selected"] = selected
        return {"patch_applied": True, "applied_files": ["owner.py"], "warnings": []}

    monkeypatch.setattr(benchmark, "_apply_planned_edits", fake_apply)

    result = benchmark._repair_after_failure(
        repo,
        {
            "prompt": "Repair the owner.",
            "candidate_paths": ["owner.py", "distractor.py"],
            "max_files": 2,
        },
        {"report": {"conclusion": {"dimension": "code"}}},
        {},
        "tests/test_owner.py::test_value FAILED",
        "local-model",
        [],
        1.0,
        1,
        contract_evidence={"failed_ids": ["tests/test_owner.py::test_value"]},
    )

    assert result["patch_applied"] is True
    assert result["selected_files"] == ["owner.py"]
    assert captured["selected"] == [
        {
            "path": "owner.py",
            "description": "Implement the owned validation contracts: The owner returns the required value.",
        }
    ]


def test_empty_local_edit_response_does_not_trigger_adapter_retry(tmp_path, monkeypatch):
    (tmp_path / "owner.py").write_text("VALUE = 1\n", encoding="utf-8")
    stages: list[str] = []

    def failed_call(*_args, stage, **_kwargs):
        stages.append(stage)
        return ""

    monkeypatch.setattr(benchmark, "_local_call", failed_call)

    result = benchmark._apply_local_edit(
        tmp_path,
        "owner.py",
        "Set VALUE to 2.",
        "local-model",
        [],
        1.0,
        stage="repair_edit_1",
    )

    assert stages == ["repair_edit_1"]
    assert result["transport_failed"] is True
    assert (tmp_path / "owner.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_compact_escalation_skips_review_and_per_file_recovery(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "owner.py").write_text("VALUE = 1\n", encoding="utf-8")
    stages: list[str] = []
    plan = {
        "dimension": "code",
        "files": [
            {
                "path": "owner.py",
                "action": "modify",
                "description": "Set the required value.",
            }
        ],
        "contract_coverage": [
            {
                "contract": "test_value",
                "owner_paths": ["owner.py"],
                "postcondition": "The owner returns the required value.",
            }
        ],
    }

    def fake_call(*_args, stage, **_kwargs):
        stages.append(stage)
        return json.dumps(plan)

    captured: dict[str, object] = {}

    def fake_apply(*_args, **kwargs):
        captured.update(kwargs)
        return {"patch_applied": True, "applied_files": ["owner.py"], "warnings": []}

    monkeypatch.setattr(benchmark, "_local_call", fake_call)
    monkeypatch.setattr(benchmark, "_apply_planned_edits", fake_apply)

    result = benchmark._repair_after_failure(
        repo,
        {"prompt": "Repair the returned value.", "candidate_paths": ["owner.py"]},
        {"report": {"conclusion": {"dimension": "code"}}},
        {},
        "tests/test_owner.py::test_value FAILED",
        "local-escalation-model",
        [],
        1.0,
        1,
        contract_evidence={"failed_ids": ["tests/test_owner.py::test_value"]},
        compact_escalation=True,
    )

    assert stages == ["repair_plan_1"]
    assert result["plan"]["review_skipped_reason"].startswith("compact local escalation")
    assert captured["allow_model_recovery"] is False


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

    assert context.startswith("STRUCTURED FAILURE DELTAS")
    assert "NON-NEGOTIABLE VALIDATION CONTRACTS" in context
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
    ledger_freeze = source.index("model_calls_before_final = calls.freeze()")
    final_oracle_read = source.index("final_oracle = _read_bound_json")
    final_start = source.index("final_tests = _run_final_adjudication")
    model_call_guard = source.index("A model call occurred after final adjudication began")

    assert repair_loop < ledger_freeze < final_oracle_read < final_start < model_call_guard


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
                        "split": "holdout-sealed-python-singlefile",
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

    with pytest.raises(ValueError, match="separate final_oracle"):
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
                        "split": "holdout-sealed-python-singlefile",
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


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (
            {
                "evaluation_role": None,
                "split": "holdout-sealed-python-singlefile",
            },
            "evaluation_role",
        ),
        (
            {
                "evaluation_role": "future_role",
                "split": "holdout-sealed-python-singlefile",
            },
            "evaluation_role",
        ),
        (
            {
                "evaluation_role": "development_regression",
                "split": "holdout-sealed-python-singlefile",
            },
            "cannot use a holdout split",
        ),
        (
            {
                "evaluation_role": "blinded_holdout",
                "split": "development-regression",
                "final_oracle": "final.json",
            },
            "canonical holdout split",
        ),
        (
            {
                "evaluation_role": "blinded_holdout",
                "split": "holdout-python-singlefile",
                "final_oracle": "final.json",
            },
            "holdout-sealed split",
        ),
        (
            {
                "evaluation_role": "development_regression",
                "split": "development-regression",
            },
            "only exact blinded_holdout",
        ),
    ],
)
def test_protocol_manifest_metadata_fails_closed(entry, message):
    with pytest.raises(ValueError, match=message):
        benchmark._validate_evaluation_entry(entry, evaluation_context="protocol")


def test_malformed_protocol_role_is_rejected_before_model_registry_access(
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
                        "evaluation_role": "blinded-holdout",
                        "split": "holdout-sealed-python-singlefile",
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

    with pytest.raises(ValueError, match="evaluation_role"):
        benchmark.run(
            argparse.Namespace(
                fixture_root=str(fixture),
                case=[],
                validate_fixtures=False,
                evaluation_context="protocol",
                model="local-model",
            )
        )


@pytest.mark.parametrize(
    "feedback_source",
    [
        "import subprocess\n\ndef test_feedback():\n    subprocess.run(['echo'])\n",
        "import socket\n\ndef test_feedback():\n    socket.socket()\n",
        (
            "from pathlib import Path\n\n"
            "def test_feedback():\n"
            "    Path('D:/dev/fixture/final.json').read_text()\n"
        ),
        (
            "from pathlib import Path\n\n"
            "def test_feedback():\n"
            "    Path('.git/config').read_text()\n"
        ),
        (
            "from pathlib import Path\n\n"
            "def test_feedback():\n"
            "    list(Path(__file__).resolve().parents[3].rglob('final_oracles/*'))\n"
        ),
        (
            "from pathlib import Path\n\n"
            "def test_feedback():\n"
            "    Path('owner.py').write_text('VALUE = 9\\n')\n"
        ),
    ],
)
def test_static_test_safety_scan_rejects_dangerous_feedback_sources(
    feedback_source,
):
    case = {
        "repo_files": {
            "owner.py": "VALUE = 1\n",
            "tests/test_public.py": "def test_public():\n    assert True\n",
        }
    }
    partitions = {
        "feedback_files": {"tests/test_feedback.py": feedback_source},
        "final_files": {
            "tests/test_final.py": "def test_final():\n    assert True\n"
        },
    }

    with pytest.raises(benchmark.FixtureIntegrityError, match="Unsafe sealed test"):
        benchmark._validate_test_source_safety(case, partitions)


def test_static_scan_allows_domain_tokens_temp_writes_and_repo_local_sql_reads():
    safe_source = (
        "from pathlib import Path\n\n"
        "requests = {'socket': 'websocket lifecycle'}\n\n"
        "def test_safe(tmp_path):\n"
        "    (tmp_path / 'checkpoint.json').write_text('ok')\n"
        "    query = (Path(__file__).resolve().parents[1] / 'report.sql').read_text()\n"
        "    assert requests and query\n"
    )
    case = {
        "repo_files": {
            "report.sql": "select 1;\n",
            "tests/test_public.py": safe_source,
        }
    }
    partitions = {
        "feedback_files": {"tests/test_feedback.py": safe_source},
        "final_files": {"tests/test_final.py": safe_source},
    }

    result = benchmark._validate_test_source_safety(case, partitions)

    assert result["static_safety_scan_passed"] is True
    assert result["scanned_test_source_count"] == 3


def test_unsafe_feedback_is_rejected_before_model_registry_access(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture"
    _write_protocol_fixture(
        fixture,
        feedback_source=(
            "import subprocess\n\n"
            "def test_feedback():\n"
            "    subprocess.run(['echo', 'sealed'])\n"
        ),
    )
    monkeypatch.setattr(
        benchmark.ollama_client,
        "list_models",
        lambda: (_ for _ in ()).throw(AssertionError("model registry was accessed")),
    )

    with pytest.raises(benchmark.FixtureIntegrityError, match="Unsafe sealed test"):
        benchmark.run(
            argparse.Namespace(
                fixture_root=str(fixture),
                case=[],
                validate_fixtures=False,
                evaluation_context="protocol",
                model="local-model",
            )
        )


def test_public_test_process_cannot_mutate_seeded_repository_files(tmp_path):
    case = {
        "test_runner": "pytest",
        "repo_files": {
            "owner.py": "VALUE = 1\n",
            "tests/test_public.py": (
                "from pathlib import Path\n\n"
                "def test_public():\n"
                "    Path('owner.py').write_text('VALUE = 9\\n')\n"
            ),
        },
    }
    repo = tmp_path / "repo"
    benchmark._init_repo(repo, case["repo_files"])

    with pytest.raises(benchmark.FixtureIntegrityError, match="mutated seeded"):
        benchmark._run_case_tests(repo, case, public_only=True)


def test_final_test_process_cannot_mutate_seeded_repository_files(tmp_path):
    case = {
        "test_runner": "pytest",
        "repo_files": {
            "owner.py": "VALUE = 1\n",
            "tests/test_public.py": "def test_public():\n    assert True\n",
        },
    }
    final_files = {
        "tests/test_final.py": (
            "from pathlib import Path\n\n"
            "def test_final():\n"
            "    Path('owner.py').write_text('VALUE = 9\\n')\n"
        )
    }

    with pytest.raises(benchmark.FixtureIntegrityError, match="mutated seeded"):
        benchmark._run_final_adjudication(case, final_files)


def test_bound_final_oracle_digest_change_fails_closed_and_is_audited(tmp_path):
    fixture = tmp_path / "fixture"
    paths = _write_protocol_fixture(fixture)
    events = []
    binding, _payload = benchmark._bind_fixture_artifact(
        fixture,
        paths["final"],
        artifact="final_oracle:integrity-case",
        events=events,
    )
    paths["final"].write_text(
        json.dumps({"case_id": "integrity-case", "final_files": {}}),
        encoding="utf-8",
    )

    with pytest.raises(benchmark.FixtureIntegrityError, match="digest changed"):
        benchmark._read_bound_json(
            binding,
            events=events,
            phase="sealed_final_oracle_read",
            case_id="integrity-case",
        )

    assert events[-1]["event"] == "fixture_digest_mismatch"
    assert events[-1]["phase"] == "sealed_final_oracle_read"


def test_model_call_ledger_rejects_appends_after_freeze():
    calls = benchmark._ModelCallLedger()
    calls.append({"stage": "diagnosis_investigator", "ok": True})

    assert calls.freeze() == 1
    with pytest.raises(benchmark.FixtureIntegrityError, match="ledger is frozen"):
        calls.append({"stage": "repair_after_final", "ok": True})


def test_frozen_ledger_blocks_model_transport_before_invocation(monkeypatch):
    calls = benchmark._ModelCallLedger()
    calls.freeze()
    invoked = False

    def forbidden_chat(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("model transport was invoked")

    monkeypatch.setattr(benchmark.ollama_client, "chat", forbidden_chat)

    with pytest.raises(benchmark.FixtureIntegrityError, match="model access is forbidden"):
        benchmark._local_call(
            "local-model",
            [{"role": "user", "content": "must not run"}],
            stage="repair_after_final",
            calls=calls,
            timeout=1.0,
            num_predict=32,
            json_mode=True,
        )

    assert invoked is False


def test_protocol_run_records_ordered_integrity_audit_and_live_reasoning_metrics(
    tmp_path,
    monkeypatch,
):
    fixture = tmp_path / "fixture"
    _write_protocol_fixture(fixture)
    monkeypatch.setattr(benchmark.ollama_client, "list_models", lambda: ["local-model"])

    def fake_diagnose(_repo, case, _model, calls, _timeout, **_kwargs):
        calls.append(
            {
                "stage": "diagnosis_investigator",
                "model": "local-model",
                "ok": True,
                "response": "{}",
            }
        )
        conclusion = {
            "dimension": "code",
            "status": "confirmed",
            "causal_sufficiency": "isolated",
        }
        return {
            "report": {"conclusion": conclusion},
            "packet": {},
            "stages": [
                {
                    "stage": "investigator",
                    "accepted": True,
                    "conclusion": conclusion,
                }
            ],
            "case": {"problem_statement": case["prompt"]},
        }

    def fake_contract_repair(repo, _case):
        (repo / "owner.py").write_text("VALUE = 2\n", encoding="utf-8")
        return {
            "attempted": True,
            "patch_applied": True,
            "selected_files": ["owner.py"],
            "warnings": [],
            "proposed_dimension": "code",
        }

    monkeypatch.setattr(benchmark, "_diagnose", fake_diagnose)
    monkeypatch.setattr(
        benchmark,
        "_apply_deterministic_contract_repair",
        fake_contract_repair,
    )
    args = argparse.Namespace(
        fixture_root=str(fixture),
        model="local-model",
        reasoning_model="local-model",
        escalation_model="",
        case=[],
        timeout=1.0,
        max_repairs=0,
        max_escalation_repairs=0,
        validate_fixtures=False,
        evaluation_context="protocol",
        report=str(tmp_path / "report.md"),
        results_json=str(tmp_path / "result.json"),
        json=False,
    )

    result = benchmark.run(args)
    case_result = result["cases"][0]
    events = case_result["integrity_audit_events"]

    def event_sequence(name, *, phase=None):
        return next(
            item["sequence"]
            for item in events
            if item["event"] == name
            and (phase is None or item.get("phase") == phase)
        )

    assert (
        event_sequence("model_call_ledger_frozen")
        < event_sequence("fixture_digest_verified", phase="sealed_final_oracle_read")
        < event_sequence("final_oracle_opened")
        < event_sequence("final_adjudication_started")
        < event_sequence("final_adjudication_completed")
        < event_sequence("post_final_model_call_count_verified")
    )
    assert all(item["timestamp_utc"] for item in events)
    assert case_result["score"] == 100
    assert case_result["live_reasoning_qualified"] is True
    assert case_result["deterministic_only"] is False
    assert case_result["fable5_class_reasoning_claim_eligible"] is True
    assert result["verdict"] == "shadow_ready"
    assert result["live_reasoning_qualified_case_count"] == 1
    assert result["deterministic_only_case_count"] == 0
    assert result["fable5_class_reasoning_claim_supported"] is False
    assert len(result["fixture_digest_inventory"]["manifest"]["sha256"]) == 64
    assert len(
        result["fixture_digest_inventory"]["cases"][0]["final_oracle"]["sha256"]
    ) == 64
    assert result["test_subprocess_assurance"]["hostile_process_proof"] is False


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


@pytest.mark.parametrize(
    "case_id",
    [
        "py_relay_rotation_window",
        "py_reservation_retry_scope",
        "ts_http_vary_isolation",
        "ts_retry_budget_clock",
        "dart_offline_tombstone_join",
        "dart_resumable_chunk_boundaries",
        "sql_tenant_grant_intervals",
        "sql_telemetry_correction_rollup",
    ],
)
def test_disclosed_tenth_contract_operators_pass_feedback_and_final(
    tmp_path,
    case_id,
):
    case = json.loads(
        (TENTH_FIXTURE_ROOT / "cases" / f"{case_id}.json").read_text(
            encoding="utf-8"
        )
    )
    oracle = json.loads(
        (TENTH_FIXTURE_ROOT / "oracles" / f"{case_id}.json").read_text(
            encoding="utf-8"
        )
    )
    final_oracle = json.loads(
        (TENTH_FIXTURE_ROOT / "final_oracles" / f"{case_id}.json").read_text(
            encoding="utf-8"
        )
    )
    repo = tmp_path / case_id
    benchmark._init_repo(repo, case["repo_files"])
    benchmark._write_files(repo, oracle["feedback_files"])

    repair = benchmark._apply_deterministic_contract_repair(repo, case)
    public = benchmark._run_case_tests(repo, case, public_only=True)
    feedback = benchmark._run_case_tests(repo, case, public_only=False)
    final = benchmark._run_final_adjudication(
        case,
        final_oracle["final_files"],
        candidate_repo=repo,
    )

    assert repair["patch_applied"] is True, repair
    assert set(repair["selected_files"]) == set(oracle["expected_files"])
    assert public["passed"] is True, public["output"]
    assert feedback["passed"] is True, feedback["output"]
    assert final["passed"] is True, final["output"]


def test_disclosed_fourteenth_node_esm_operator_passes_feedback_and_final(tmp_path):
    case_id = "th14_node_esm_plugin_loading"
    case = benchmark._read_json(
        FOURTEENTH_FIXTURE_ROOT / "cases" / f"{case_id}.json"
    )
    oracle = benchmark._read_json(
        FOURTEENTH_FIXTURE_ROOT / "oracles" / f"{case_id}.json"
    )
    final_oracle = benchmark._read_json(
        FOURTEENTH_FIXTURE_ROOT / "final_oracles" / f"{case_id}.json"
    )
    repo = tmp_path / case_id
    benchmark._init_repo(repo, case["repo_files"])

    repair = benchmark._apply_deterministic_contract_repair(repo, case)
    benchmark._write_files(repo, oracle["feedback_files"])
    public = benchmark._run_case_tests(repo, case, public_only=True)
    feedback = benchmark._run_case_tests(repo, case, public_only=False)
    final = benchmark._run_final_adjudication(
        case,
        final_oracle["final_files"],
        candidate_repo=repo,
    )

    assert repair["patch_applied"] is True, repair
    assert set(repair["selected_files"]) == set(oracle["expected_files"])
    assert repair["proposed_dimension"] == oracle["expected_dimension"]
    assert public["passed"] is True, public["output"]
    assert feedback["passed"] is True, feedback["output"]
    assert final["passed"] is True, final["output"]


def test_disclosed_fourteenth_dart_semver_operator_passes_feedback_and_final(
    tmp_path,
):
    case_id = "th14_dart_semver_selection"
    case = benchmark._read_json(
        FOURTEENTH_FIXTURE_ROOT / "cases" / f"{case_id}.json"
    )
    oracle = benchmark._read_json(
        FOURTEENTH_FIXTURE_ROOT / "oracles" / f"{case_id}.json"
    )
    final_oracle = benchmark._read_json(
        FOURTEENTH_FIXTURE_ROOT / "final_oracles" / f"{case_id}.json"
    )
    repo = tmp_path / case_id
    benchmark._init_repo(repo, case["repo_files"])

    repair = benchmark._apply_deterministic_contract_repair(repo, case)
    benchmark._write_files(repo, oracle["feedback_files"])
    public = benchmark._run_case_tests(repo, case, public_only=True)
    feedback = benchmark._run_case_tests(repo, case, public_only=False)
    final = benchmark._run_final_adjudication(
        case,
        final_oracle["final_files"],
        candidate_repo=repo,
    )

    assert repair["patch_applied"] is True, repair
    assert set(repair["selected_files"]) == set(oracle["expected_files"])
    assert repair["proposed_dimension"] == oracle["expected_dimension"]
    assert public["passed"] is True, public["output"]
    assert feedback["passed"] is True, feedback["output"]
    assert final["passed"] is True, final["output"]


def test_disclosed_fourteenth_python_handler_operator_passes_feedback_and_final(
    tmp_path,
):
    case_id = "th14_py_decorated_handlers"
    case = benchmark._read_json(
        FOURTEENTH_FIXTURE_ROOT / "cases" / f"{case_id}.json"
    )
    oracle = benchmark._read_json(
        FOURTEENTH_FIXTURE_ROOT / "oracles" / f"{case_id}.json"
    )
    final_oracle = benchmark._read_json(
        FOURTEENTH_FIXTURE_ROOT / "final_oracles" / f"{case_id}.json"
    )
    repo = tmp_path / case_id
    benchmark._init_repo(repo, case["repo_files"])

    repair = benchmark._apply_deterministic_contract_repair(repo, case)
    benchmark._write_files(repo, oracle["feedback_files"])
    public = benchmark._run_case_tests(repo, case, public_only=True)
    feedback = benchmark._run_case_tests(repo, case, public_only=False)
    final = benchmark._run_final_adjudication(
        case,
        final_oracle["final_files"],
        candidate_repo=repo,
    )

    assert repair["patch_applied"] is True, repair
    assert set(repair["selected_files"]) == set(oracle["expected_files"])
    assert repair["proposed_dimension"] == oracle["expected_dimension"]
    assert public["passed"] is True, public["output"]
    assert feedback["passed"] is True, feedback["output"]
    assert final["passed"] is True, final["output"]


def test_disclosed_fourteenth_sql_suppression_operator_passes_feedback_and_final(
    tmp_path,
):
    case_id = "th14_sql_suppression_batches"
    case = benchmark._read_json(
        FOURTEENTH_FIXTURE_ROOT / "cases" / f"{case_id}.json"
    )
    oracle = benchmark._read_json(
        FOURTEENTH_FIXTURE_ROOT / "oracles" / f"{case_id}.json"
    )
    final_oracle = benchmark._read_json(
        FOURTEENTH_FIXTURE_ROOT / "final_oracles" / f"{case_id}.json"
    )
    repo = tmp_path / case_id
    benchmark._init_repo(repo, case["repo_files"])

    repair = benchmark._apply_deterministic_contract_repair(repo, case)
    benchmark._write_files(repo, oracle["feedback_files"])
    public = benchmark._run_case_tests(repo, case, public_only=True)
    feedback = benchmark._run_case_tests(repo, case, public_only=False)
    final = benchmark._run_final_adjudication(
        case,
        final_oracle["final_files"],
        candidate_repo=repo,
    )

    assert repair["patch_applied"] is True, repair
    assert set(repair["selected_files"]) == set(oracle["expected_files"])
    assert repair["proposed_dimension"] == oracle["expected_dimension"]
    assert public["passed"] is True, public["output"]
    assert feedback["passed"] is True, feedback["output"]
    assert final["passed"] is True, final["output"]


@pytest.mark.parametrize(
    "case_id",
    [
        "node-cache-locale-order-retry-slot",
        "sql_out_of_order_document_heads",
    ],
)
def test_disclosed_eleventh_structural_repairs_pass_feedback_and_final(
    tmp_path,
    case_id,
):
    case = benchmark._read_json(
        ELEVENTH_FIXTURE_ROOT / "cases" / f"{case_id}.json"
    )
    oracle = benchmark._read_json(
        ELEVENTH_FIXTURE_ROOT / "oracles" / f"{case_id}.json"
    )
    final_oracle = benchmark._read_json(
        ELEVENTH_FIXTURE_ROOT / "final_oracles" / f"{case_id}.json"
    )
    repo = tmp_path / case_id
    benchmark._init_repo(repo, case["repo_files"])

    repair = benchmark._apply_deterministic_contract_repair(repo, case)
    public = benchmark._run_case_tests(repo, case, public_only=True)
    benchmark._write_files(repo, oracle["feedback_files"])
    feedback = benchmark._run_case_tests(repo, case, public_only=False)
    final = benchmark._run_final_adjudication(
        case,
        final_oracle["final_files"],
        candidate_repo=repo,
    )

    assert repair["patch_applied"] is True, repair
    assert set(repair["selected_files"]) == set(oracle["expected_files"])
    assert public["passed"] is True, public["output"]
    assert feedback["passed"] is True, feedback["output"]
    assert final["passed"] is True, final["output"]


@pytest.mark.parametrize(
    "case_id",
    [
        "dart_decimal_apportionment",
        "dart_release_reader_lifecycle",
        "dart_trusted_proxy_chain",
        "node_base64url_blob_ids",
        "node_policy_reload_consistency",
        "node_tls_client_auth_config",
        "py_config_reload",
        "py_tail_checkpoint",
        "py_unordered_category_hierarchy",
        "sql_notification_override_tristate",
        "sql_tenant_stock_ownership",
        "sql_ticket_archive_transitions",
    ],
)
def test_disclosed_twelfth_structural_repairs_pass_feedback_and_final(
    tmp_path,
    case_id,
):
    case = benchmark._read_json(
        TWELFTH_FIXTURE_ROOT / "cases" / f"{case_id}.json"
    )
    oracle = benchmark._read_json(
        TWELFTH_FIXTURE_ROOT / "oracles" / f"{case_id}.json"
    )
    final_oracle = benchmark._read_json(
        TWELFTH_FIXTURE_ROOT / "final_oracles" / f"{case_id}.json"
    )
    repo = tmp_path / case_id
    benchmark._init_repo(repo, case["repo_files"])

    repair = benchmark._apply_deterministic_contract_repair(repo, case)
    public = benchmark._run_case_tests(repo, case, public_only=True)
    benchmark._write_files(repo, oracle["feedback_files"])
    feedback = benchmark._run_case_tests(repo, case, public_only=False)
    final = benchmark._run_final_adjudication(
        case,
        final_oracle["final_files"],
        candidate_repo=repo,
    )

    assert repair["patch_applied"] is True, repair
    assert repair["proposed_dimension"] == oracle["expected_dimension"]
    assert set(repair["selected_files"]) == set(oracle["expected_files"])
    assert public["passed"] is True, public["output"]
    assert feedback["passed"] is True, feedback["output"]
    assert final["passed"] is True, final["output"]


def test_recognized_contract_uses_initial_non_generative_edit_lane(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        benchmark.ollama_client,
        "list_models",
        lambda: ["qwen2.5-coder:7b"],
    )
    monkeypatch.setattr(
        benchmark,
        "_diagnose",
        lambda _repo, case, *_args, **_kwargs: {
            "report": {
                "conclusion": {
                    "dimension": "clock",
                    "causal_sufficiency": "direct_artifact",
                }
            },
            "packet": {},
            "stages": [],
            "case": {"problem_statement": case["prompt"]},
        },
    )
    monkeypatch.setattr(
        benchmark,
        "_generate_patch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("recognized contract should bypass generative editing")
        ),
    )
    args = argparse.Namespace(
        fixture_root=str(TENTH_FIXTURE_ROOT),
        model="qwen2.5-coder:7b",
        escalation_model="",
        case=["ts_retry_budget_clock"],
        timeout=1.0,
        max_repairs=0,
        max_escalation_repairs=0,
        validate_fixtures=False,
        evaluation_context="disclosed_replay",
        report=str(tmp_path / "report.md"),
        results_json=str(tmp_path / "result.json"),
        json=False,
    )

    result = benchmark.run(args)
    case_result = result["cases"][0]

    assert case_result["score"] == 100
    assert case_result["functional_repair_passed"] is True
    assert case_result["model_calls"] == []
    assert case_result["deterministic_contract_repair"]["patch_applied"] is True
    assert case_result["deterministic_only"] is True
    assert case_result["live_reasoning_qualified"] is False
    assert result["verdict"] == "needs_improvement"
    assert result["evaluation_verdict"] == "disclosed_replay_failed"


def test_validated_structural_intervention_revises_family_and_labels_disclosed_replay(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        benchmark.ollama_client,
        "list_models",
        lambda: ["qwen2.5-coder:7b"],
    )
    monkeypatch.setattr(
        benchmark,
        "_diagnose",
        lambda _repo, case, *_args, **_kwargs: {
            "report": {
                "conclusion": {
                    "dimension": "state",
                    "causal_sufficiency": "observational",
                    "status": "provisional",
                }
            },
            "packet": {},
            "stages": [],
            "case": {"problem_statement": case["prompt"]},
        },
    )
    monkeypatch.setattr(
        benchmark,
        "_generate_patch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("recognized structural contract should bypass generation")
        ),
    )
    args = argparse.Namespace(
        fixture_root=str(TWELFTH_FIXTURE_ROOT),
        model="qwen2.5-coder:7b",
        escalation_model="",
        case=["node_tls_client_auth_config"],
        timeout=1.0,
        max_repairs=0,
        max_escalation_repairs=0,
        validate_fixtures=False,
        evaluation_context="disclosed_replay",
        report=str(tmp_path / "report.md"),
        results_json=str(tmp_path / "result.json"),
        json=False,
    )

    result = benchmark.run(args)
    case_result = result["cases"][0]

    assert result["evaluation_context"] == "disclosed_replay"
    assert result["evaluation_verdict"] == "disclosed_replay_failed"
    assert result["blinded_holdout_case_count"] == 0
    assert case_result["original_evaluation_role"] == "blinded_holdout"
    assert case_result["evaluation_role"] == "development_regression"
    assert case_result["diagnosis_dimension"] == "config"
    assert case_result["retained_diagnosis_dimension"] == "config"
    assert case_result["accepted_diagnosis_conclusion"]["accepted"] is True
    assert case_result["functional_repair_passed"] is True
    assert case_result["score"] == 100
    assert case_result["deterministic_only"] is True
    assert case_result["live_reasoning_qualified"] is False


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


def test_local_edit_reformats_one_mixed_adapter_response(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "scheduler.mjs"
    target.write_text("export const delay = 1;\n", encoding="utf-8")
    responses = iter(
        [
            "```js\n<<<<<<< SEARCH export const delay = 1;\n=======\n"
            "export const delay = 2;\n>>>>>>> REPLACE\n```",
            "<<<<<<< SEARCH\nexport const delay = 1;\n=======\n"
            "export const delay = 2;\n>>>>>>> REPLACE",
        ]
    )
    stages: list[str] = []

    def fake_call(*_args, stage, **_kwargs):
        stages.append(stage)
        return next(responses)

    monkeypatch.setattr(benchmark, "_local_call", fake_call)

    result = benchmark._apply_local_edit(
        repo,
        "scheduler.mjs",
        "Use the granted delay.",
        "local-model",
        [],
        1.0,
        stage="edit",
    )

    assert result["patch_applied"] is True
    assert stages == ["edit", "edit_adapter_retry"]
    assert any("mixed edit-format" in value for value in result["warnings"])
    assert target.read_text(encoding="utf-8") == "export const delay = 2;\n"


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


def _current_run_policy(
    tmp_path: Path,
    *,
    fixture_root: Path,
    implementation_commit: str,
    languages: dict[str, int],
) -> Path:
    implementation_tree = benchmark._require_git_success(
        "rev-parse",
        f"{implementation_commit}^{{tree}}",
        label="test implementation tree",
    )
    policy = {
        "schema": "chili.diagnosis-to-fix-run-policy.v1",
        "fixture_root": str(fixture_root),
        "implementation_commit": implementation_commit,
        "implementation_tree": implementation_tree,
        "primary_model": "qwen2.5-coder:7b",
        "reasoning_model": "qwen3:8b",
        "escalation_model": "disabled",
        "max_base_repairs": 2,
        "max_escalation_repairs": 0,
        "per_call_timeout_sec": 150,
        "case_model_time_budget_sec": 480,
        "premium_calls_allowed": 0,
        "evaluation_context": "protocol",
        "expected_case_count": sum(languages.values()),
        "expected_language_counts": languages,
        "sealed_final_required": True,
        "external_final_oracle_required": True,
        "mechanism_disjoint_from_training_regressions": True,
        "independent_fixture_author_required": True,
        "independent_fixture_validator_required": True,
        "source_edits_after_fixture_freeze_allowed": False,
        "runner_sha256": benchmark._sha256_bytes(
            Path(benchmark.__file__).read_bytes()
        ),
        "diagnostic_reasoning_sha256": benchmark._sha256_bytes(
            Path(benchmark.diagnostic_reasoning.__file__).read_bytes()
        ),
    }
    path = tmp_path / "run-policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    return path


def test_run_policy_enforces_models_budgets_case_mix_and_source_freeze(
    tmp_path,
    monkeypatch,
):
    implementation_commit = benchmark._require_git_success(
        "rev-parse", "HEAD", label="test HEAD"
    )
    policy = _current_run_policy(
        tmp_path,
        fixture_root=FIXTURE_ROOT,
        implementation_commit=implementation_commit,
        languages={"node": 1, "python": 1},
    )
    args = SimpleNamespace(
        model="qwen2.5-coder:7b",
        reasoning_model="qwen3:8b",
        escalation_model="",
        max_repairs=2,
        max_escalation_repairs=0,
        timeout=150.0,
        case_model_time_budget=480.0,
    )
    events = []
    monkeypatch.setattr(benchmark, "_run_policy_path", lambda _value: policy)

    binding = benchmark._validate_run_policy(
        policy,
        args=args,
        fixture_root=FIXTURE_ROOT,
        prepared_entries=[
            {"language": "typescript"},
            {"language": "python"},
        ],
        evaluation_context="protocol",
        reasoning_model="qwen3:8b",
        repair_schedule=["qwen2.5-coder:7b", "qwen2.5-coder:7b"],
        events=events,
    )
    benchmark._verify_run_policy_unchanged(binding, events=events)

    assert binding["enforced"] is True
    assert binding["language_counts"] == {"node": 1, "python": 1}
    assert [item["event"] for item in events] == [
        "run_policy_verified",
        "run_policy_digest_reverified",
    ]


def test_run_policy_rejects_model_and_language_drift(tmp_path, monkeypatch):
    implementation_commit = benchmark._require_git_success(
        "rev-parse", "HEAD", label="test HEAD"
    )
    policy = _current_run_policy(
        tmp_path,
        fixture_root=FIXTURE_ROOT,
        implementation_commit=implementation_commit,
        languages={"node": 1},
    )
    args = SimpleNamespace(
        model="qwen2.5-coder:7b",
        escalation_model="",
        max_repairs=2,
        max_escalation_repairs=0,
        timeout=150.0,
        case_model_time_budget=480.0,
    )
    common = {
        "args": args,
        "fixture_root": FIXTURE_ROOT,
        "prepared_entries": [{"language": "python"}],
        "evaluation_context": "protocol",
        "reasoning_model": "qwen3:8b",
        "repair_schedule": ["qwen2.5-coder:7b", "qwen2.5-coder:7b"],
        "events": [],
    }
    monkeypatch.setattr(benchmark, "_run_policy_path", lambda _value: policy)

    with pytest.raises(benchmark.FixtureIntegrityError, match="language distribution"):
        benchmark._validate_run_policy(policy, **common)

    value = json.loads(policy.read_text(encoding="utf-8"))
    value["primary_model"] = "another-model"
    policy.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(benchmark.FixtureIntegrityError, match="primary_model mismatch"):
        benchmark._validate_run_policy(policy, **common)


@pytest.mark.parametrize(
    "case_id",
    [
        "th15_d01",
        "th15_n01",
        "th15_n02",
        "th15_p01",
        "th15_p02",
        "th15_s01",
        "th15_s02",
    ],
)
def test_disclosed_fifteenth_contract_operators_pass_feedback_and_final(
    tmp_path,
    case_id,
):
    case = benchmark._read_json(
        FIFTEENTH_FIXTURE_ROOT / "cases" / f"{case_id}.json"
    )
    oracle = benchmark._read_json(
        FIFTEENTH_FIXTURE_ROOT / "oracles" / f"{case_id}.json"
    )
    final_oracle = benchmark._read_json(
        FIFTEENTH_FIXTURE_ROOT / "final_oracles" / f"{case_id}.json"
    )
    repo = tmp_path / case_id
    benchmark._init_repo(repo, case["repo_files"])

    repair = benchmark._apply_deterministic_contract_repair(repo, case)
    benchmark._write_files(repo, oracle["feedback_files"])
    public = benchmark._run_case_tests(repo, case, public_only=True)
    feedback = benchmark._run_case_tests(repo, case, public_only=False)
    final = benchmark._run_final_adjudication(
        case,
        final_oracle["final_files"],
        candidate_repo=repo,
    )

    assert repair["patch_applied"] is True, repair
    assert set(repair["selected_files"]) == set(oracle["expected_files"])
    assert repair["proposed_dimension"] == oracle["expected_dimension"]
    assert public["passed"] is True, public["output"]
    assert feedback["passed"] is True, feedback["output"]
    assert final["passed"] is True, final["output"]
