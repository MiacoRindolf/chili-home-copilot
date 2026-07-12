from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from scripts import autopilot_realworld_diagnostic_benchmark as benchmark


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "project_autonomy_diagnostics"
BLINDED_SECOND_ROOT = (
    Path(__file__).parent
    / "fixtures"
    / "project_autonomy_diagnostics_blinded2_20260711"
)
BLINDED_THIRD_ROOT = (
    Path(__file__).parent
    / "fixtures"
    / "project_autonomy_diagnostics_blinded3_20260711"
)
BLINDED_FOURTH_ROOT = (
    Path(__file__).parent
    / "fixtures"
    / "project_autonomy_diagnostics_blinded4_20260711"
)
BLINDED_FIFTH_ROOT = (
    Path(__file__).parent
    / "fixtures"
    / "project_autonomy_diagnostics_blinded5_20260711"
)
BLINDED_SIXTH_ROOT = (
    Path(__file__).parent
    / "fixtures"
    / "project_autonomy_diagnostics_blinded6_20260712"
)


def test_manifest_uses_fable5_and_keeps_oracles_separate():
    manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["reference_model"] == "claude-fable-5"
    assert len(manifest["cases"]) == 7
    assert {item["split"] for item in manifest["cases"]} == {"calibration", "holdout"}
    for item in manifest["cases"]:
        case = json.loads((FIXTURE_ROOT / item["case"]).read_text(encoding="utf-8"))
        oracle = json.loads((FIXTURE_ROOT / item["oracle"]).read_text(encoding="utf-8"))
        assert "expected_dimensions" not in case
        assert oracle["case_id"] == case["case_id"]


def test_heuristic_benchmark_run_is_local_only_and_never_claims_fable_parity(tmp_path):
    args = argparse.Namespace(
        fixture_root=str(FIXTURE_ROOT),
        model="qwen2.5-coder:7b",
        case=["hold-104"],
        timeout=1.0,
        num_predict=100,
        num_ctx=2048,
        keep_alive="1m",
        stages="judge",
        heuristic_only=True,
        report=str(tmp_path / "report.md"),
        results_json=str(tmp_path / "results.json"),
        json=False,
    )

    result = benchmark.run(args)

    assert result["premium_calls"] == 0
    assert result["fable5_head_to_head_run"] is False
    assert result["fable5_parity_claim"] is False
    assert result["model_output_gate_passed"] is True
    assert result["cases"][0]["score_detail"]["checks"]["safety"] is True
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Fable 5 parity claim: **No**" in report
    assert "s/call" in report
    assert "three exact Fable 5 incident contracts" not in report


def test_case_checkpoint_resumes_without_replaying_completed_reasoning(
    tmp_path,
    monkeypatch,
):
    manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))
    case_ids = [Path(item["case"]).stem for item in manifest["cases"][:2]]
    checkpoint = tmp_path / "diagnostic.checkpoint.json"
    args = argparse.Namespace(
        fixture_root=str(FIXTURE_ROOT),
        model="qwen2.5-coder:7b",
        case=case_ids,
        timeout=1.0,
        num_predict=100,
        num_ctx=2048,
        keep_alive="1m",
        stages="judge",
        heuristic_only=True,
        report=str(tmp_path / "report.md"),
        results_json=str(tmp_path / "results.json"),
        checkpoint=str(checkpoint),
        fresh=False,
        json=False,
    )
    original = benchmark.diagnostic_reasoning.run_local_diagnostic_debate
    attempted: list[str] = []

    def interrupt_after_first(case, *call_args, **call_kwargs):
        attempted.append(str(case["case_id"]))
        if len(attempted) == 2:
            raise RuntimeError("simulated runner interruption")
        return original(case, *call_args, **call_kwargs)

    monkeypatch.setattr(
        benchmark.diagnostic_reasoning,
        "run_local_diagnostic_debate",
        interrupt_after_first,
    )
    with pytest.raises(RuntimeError, match="simulated runner interruption"):
        benchmark.run(args)

    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["schema"] == "chili.realworld-diagnostic-checkpoint.v1"
    assert saved["completed_case_ids"] == [case_ids[0]]
    assert [item["case_id"] for item in saved["cases"]] == [case_ids[0]]

    incompatible = argparse.Namespace(**vars(args))
    incompatible.num_ctx += 1
    with pytest.raises(SystemExit, match="contract does not match"):
        benchmark.run(incompatible)

    resumed: list[str] = []

    def record_resume(case, *call_args, **call_kwargs):
        resumed.append(str(case["case_id"]))
        return original(case, *call_args, **call_kwargs)

    monkeypatch.setattr(
        benchmark.diagnostic_reasoning,
        "run_local_diagnostic_debate",
        record_resume,
    )
    result = benchmark.run(args)

    assert resumed == [case_ids[1]]
    assert [item["case_id"] for item in result["cases"]] == case_ids
    assert not checkpoint.exists()
    assert (tmp_path / "report.md").is_file()
    assert (tmp_path / "results.json").is_file()


def test_second_blinded_fixture_keeps_oracles_and_dimensions_out_of_public_cases():
    manifest = json.loads(
        (BLINDED_SECOND_ROOT / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["reference_model"] == "claude-fable-5"
    assert len(manifest["cases"]) == 8
    assert {item["split"] for item in manifest["cases"]} == {"holdout"}
    assert {item["evaluation_role"] for item in manifest["cases"]} == {
        "blinded_holdout_second_run"
    }
    for item in manifest["cases"]:
        case = json.loads(
            (BLINDED_SECOND_ROOT / item["case"]).read_text(encoding="utf-8")
        )
        oracle = json.loads(
            (BLINDED_SECOND_ROOT / item["oracle"]).read_text(encoding="utf-8")
        )
        assert oracle["case_id"] == case["case_id"]
        assert not any(
            key.startswith(("expected_", "forbid_")) for key in case
        )
        assert {
            observation.get("dimension")
            for observation in case["observations"]
        } == {"unknown"}


def test_blinded_report_uses_manifest_protocol_instead_of_legacy_boilerplate(tmp_path):
    args = argparse.Namespace(
        fixture_root=str(BLINDED_THIRD_ROOT),
        model="qwen2.5-coder:7b",
        case=["bh3-307"],
        timeout=1.0,
        num_predict=100,
        num_ctx=2048,
        keep_alive="1m",
        stages="judge",
        heuristic_only=True,
        report=str(tmp_path / "blinded-report.md"),
        results_json=str(tmp_path / "blinded-results.json"),
        json=False,
    )

    result = benchmark.run(args)
    report = (tmp_path / "blinded-report.md").read_text(encoding="utf-8")

    assert result["blinded"] is True
    assert result["benchmark_id"] == "fable5-class-diagnostic-blinded-third-run-20260711"
    assert result["evaluation_roles"] == ["blinded_holdout_third_run"]
    assert "manifest declares this as a blinded benchmark slice" in report
    assert "three exact Fable 5 incident contracts" not in report


def test_fourth_blinded_fixture_preserves_manifest_and_public_blinding_contract():
    manifest = json.loads(
        (BLINDED_FOURTH_ROOT / "manifest.json").read_text(encoding="ascii")
    )

    assert manifest["schema"] == "chili.realworld-diagnostic-manifest.v1"
    assert manifest["reference_model"] == "claude-fable-5"
    assert manifest["benchmark_id"] == "fable5-class-diagnostic-blinded-fourth-run-20260711"
    assert manifest["blinded"] is True
    assert manifest["immutable_input_count"] == 17
    assert len(manifest["cases"]) == 8
    assert {item["evaluation_role"] for item in manifest["cases"]} == {
        "blinded_holdout_fourth_run"
    }
    for item in manifest["cases"]:
        case = json.loads(
            (BLINDED_FOURTH_ROOT / item["case"]).read_text(encoding="ascii")
        )
        oracle = json.loads(
            (BLINDED_FOURTH_ROOT / item["oracle"]).read_text(encoding="ascii")
        )
        assert case["case_id"] == oracle["case_id"]
        assert not any(key.startswith(("expected_", "forbid_")) for key in case)
        assert {observation["dimension"] for observation in case["observations"]} == {
            "unknown"
        }


def test_fifth_blinded_fixture_preserves_manifest_and_public_blinding_contract():
    manifest = json.loads(
        (BLINDED_FIFTH_ROOT / "manifest.json").read_text(encoding="ascii")
    )

    assert manifest["schema"] == "chili.realworld-diagnostic-manifest.v1"
    assert manifest["reference_model"] == "claude-fable-5"
    assert manifest["benchmark_id"] == "fable5-class-diagnostic-blinded-fifth-run-20260711"
    assert manifest["blinded"] is True
    assert manifest["immutable_input_count"] == 17
    assert len(manifest["cases"]) == 8
    assert {item["evaluation_role"] for item in manifest["cases"]} == {
        "blinded_holdout_fifth_run"
    }
    for item in manifest["cases"]:
        case = json.loads(
            (BLINDED_FIFTH_ROOT / item["case"]).read_text(encoding="ascii")
        )
        oracle = json.loads(
            (BLINDED_FIFTH_ROOT / item["oracle"]).read_text(encoding="ascii")
        )
        assert case["case_id"] == oracle["case_id"]
        assert not any(key.startswith(("expected_", "forbid_")) for key in case)
        assert {observation["dimension"] for observation in case["observations"]} == {
            "unknown"
        }


def test_sixth_blinded_fixture_preserves_manifest_and_public_blinding_contract():
    manifest = json.loads(
        (BLINDED_SIXTH_ROOT / "manifest.json").read_text(encoding="ascii")
    )

    assert manifest["schema"] == "chili.realworld-diagnostic-manifest.v1"
    assert manifest["reference_model"] == "claude-fable-5"
    assert manifest["benchmark_id"] == "fable5-class-diagnostic-blinded-sixth-run-20260712"
    assert manifest["blinded"] is True
    assert manifest["immutable_input_count"] == 17
    assert len(manifest["cases"]) == 8
    assert {item["evaluation_role"] for item in manifest["cases"]} == {
        "blinded_holdout_sixth_run"
    }
    for item in manifest["cases"]:
        case = json.loads(
            (BLINDED_SIXTH_ROOT / item["case"]).read_text(encoding="ascii")
        )
        oracle = json.loads(
            (BLINDED_SIXTH_ROOT / item["oracle"]).read_text(encoding="ascii")
        )
        assert case["case_id"] == oracle["case_id"]
        assert not any(key.startswith(("expected_", "forbid_")) for key in case)
        assert {observation["dimension"] for observation in case["observations"]} == {
            "unknown"
        }


def test_model_output_gate_rejects_transport_success_without_usable_packets():
    cases = [
        {
            "model_calls": [{"stage": "judge", "ok": True}],
            "stages": [{"stage": "judge", "accepted": False}],
        },
        {
            "model_calls": [{"stage": "judge", "ok": True}],
            "stages": [{"stage": "judge", "accepted": False}],
        },
    ]

    quality = benchmark.model_output_quality(
        cases,
        ("judge",),
        heuristic_only=False,
    )

    assert quality["recorded_model_calls"] == 2
    assert quality["successful_model_calls"] == 2
    assert quality["accepted_model_stages"] == 0
    assert quality["model_output_gate_passed"] is False


def test_model_output_gate_requires_one_usable_packet_per_case():
    cases = [
        {
            "model_calls": [
                {"stage": "investigator", "ok": True},
                {"stage": "judge", "ok": True},
            ],
            "stages": [
                {"stage": "investigator", "accepted": True},
                {"stage": "judge", "accepted": False},
            ],
        },
        {
            "model_calls": [
                {"stage": "investigator", "ok": True},
                {"stage": "judge", "ok": True},
            ],
            "stages": [
                {"stage": "investigator", "accepted": False},
                {"stage": "judge", "accepted": True},
            ],
        },
    ]

    quality = benchmark.model_output_quality(
        cases,
        ("investigator", "judge"),
        heuristic_only=False,
    )

    assert quality["cases_with_accepted_model_stage"] == 2
    assert quality["model_output_usable_rate"] == 0.5
    assert quality["model_output_gate_passed"] is True
