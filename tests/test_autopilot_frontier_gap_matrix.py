from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "autopilot_frontier_gap_matrix.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "autopilot_frontier_gap_matrix",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_passing_system_evidence(agentops: Path) -> None:
    _write(
        agentops / "OFFLINE_PROJECT_AUTONOMY_BENCHMARK.md",
        ["- Status: passed", "- Average score: 100/100"],
    )
    tournament_lines = [
        "- Status: passed",
        "- Evidence mode: real_artifacts",
        "- Tasks: 3",
        "- Winner counts: local_model=3, codex=0, claude=0, none=0",
        "- Runtime measurements: measured=9, unmeasured=0",
    ]
    for filename in (
        "MESO_PROJECT_WORKFLOW_TOURNAMENT_BENCHMARK.md",
        "MACRO_LONG_HORIZON_TOURNAMENT_BENCHMARK.md",
        "DEEP_CONTEXT_REASONING_TOURNAMENT_BENCHMARK.md",
    ):
        _write(agentops / filename, tournament_lines)


def _point_at_tmp(matrix, tmp_path: Path, monkeypatch) -> Path:
    agentops = tmp_path / "project_ws" / "AgentOps"
    monkeypatch.setattr(matrix, "AGENTOPS_ROOT", agentops)
    monkeypatch.setattr(matrix, "DEFAULT_OUTPUT", agentops / "FRONTIER_GAP_MATRIX.md")
    monkeypatch.setattr(matrix, "CODING_SCORECARD", agentops / "CODING_BENCHMARK_SCORECARD.md")
    monkeypatch.setattr(matrix, "SOURCE_CHURN_DIAGNOSTICS", agentops / "SOURCE_CHURN_DIAGNOSTICS.md")
    monkeypatch.setattr(matrix, "FRONTIER_MODEL_EVIDENCE_INTAKE", agentops / "FRONTIER_MODEL_EVIDENCE_INTAKE.md")
    monkeypatch.setattr(
        matrix,
        "FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS",
        agentops / "FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS.md",
    )
    monkeypatch.setattr(matrix, "MODEL_SHADOW_SCORECARD", agentops / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md")
    monkeypatch.setattr(matrix, "MODEL_TOURNAMENT_SCORECARD", agentops / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md")
    monkeypatch.setattr(matrix, "HOSTED_PR_REPAIR_SCORECARD", agentops / "HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md")
    monkeypatch.setattr(matrix, "OFFLINE_AUTONOMY_SCORECARD", agentops / "OFFLINE_PROJECT_AUTONOMY_BENCHMARK.md")
    monkeypatch.setattr(
        matrix,
        "MESO_WORKFLOW_TOURNAMENT_SCORECARD",
        agentops / "MESO_PROJECT_WORKFLOW_TOURNAMENT_BENCHMARK.md",
    )
    monkeypatch.setattr(
        matrix,
        "MACRO_LONG_HORIZON_TOURNAMENT_SCORECARD",
        agentops / "MACRO_LONG_HORIZON_TOURNAMENT_BENCHMARK.md",
    )
    monkeypatch.setattr(
        matrix,
        "CONTEXT_REASONING_TOURNAMENT_SCORECARD",
        agentops / "DEEP_CONTEXT_REASONING_TOURNAMENT_BENCHMARK.md",
    )
    return agentops


def _readiness_runner(payload):
    def runner(*, write=False):
        assert write is False
        return "# readiness\n", payload, Path("project_ws/AgentOps/FRONTIER_READINESS_AUDIT.md")

    return runner


def _all_passed_readiness_payload() -> dict[str, object]:
    requirements = (
        "coding_scorecard_status",
        "coding_score",
        "coding_pass_rate",
        "source_stability",
        "coding_scorecard_current_source_freshness",
        "local_model_candidate_run_status",
        "model_shadow_scorecard_status",
        "model_shadow_real_manifest_mode",
        "model_tournament_scorecard_status",
        "model_tournament_real_artifacts_mode",
        "hosted_pr_repair_scorecard_status",
        "hosted_pr_repair_real_inventory_mode",
        "hosted_pr_repair_promotion_eligible",
    )
    return {
        "status": "passed",
        "readiness_score": 100,
        "blockers": 0,
        "results": [
            {
                "requirement": requirement,
                "status": "passed",
                "required": "passed",
                "actual": "passed",
                "evidence": "evidence",
                "next_action": "none",
            }
            for requirement in requirements
        ],
    }


def test_frontier_gap_matrix_blocks_superiority_until_claude_source_is_ready(
    tmp_path,
    monkeypatch,
):
    matrix = _load_module()
    agentops = _point_at_tmp(matrix, tmp_path, monkeypatch)
    _write_passing_system_evidence(agentops)
    _write(
        agentops / "CODING_BENCHMARK_SCORECARD.md",
        [
            "- Status: passed",
            "- Overall score: 100/100",
            "- Pass rate: 57/57",
            "- Source stability: stable",
        ],
    )
    _write(
        agentops / "SOURCE_CHURN_DIAGNOSTICS.md",
        [
            "- Status: passed",
            "- Current source freshness: current",
            "- Next action: none",
        ],
    )
    _write(
        agentops / "FRONTIER_MODEL_EVIDENCE_INTAKE.md",
        [
            "- Status: warning",
            "- Ready sources: 2/3",
            "- Missing/incomplete sources: claude",
        ],
    )
    _write(
        agentops / "FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS.md",
        [
            "- Claude source auth mode: subscription",
            "- Claude API-key probe status: api_key_missing",
            "- Claude source runner command: python scripts/autopilot_frontier_source_runner.py --source-kind claude --source-auth-mode auto --json",
        ],
    )
    _write(
        agentops / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md",
        [
            "- Status: failed",
            "- Evidence mode: partial_real_manifest",
            "- Missing source kinds: claude",
        ],
    )
    _write(
        agentops / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md",
        [
            "- Status: failed",
            "- Evidence mode: real_artifacts",
            "- Missing source kinds: claude",
            "- Source kinds: codex, local_model",
            "- Runtime measurements: measured=12, unmeasured=0",
            "- Available-source leader counts: local_model=6, codex=0, claude=0, none=0",
            "",
            "| Case | Comparison Class | Winner | Score | Evidence |",
            "| --- | --- | --- | ---: | --- |",
            "| case-1 | strict_candidate_win | none | 0 | missing_source_kind:claude |",
            "| case-2 | runtime_control_behavior_regression | none | 0 | missing_source_kind:claude |",
            "| case-3 | startup_contract_behavior_regression | none | 0 | missing_source_kind:claude |",
            "| case-4 | preflight_behavior_regression | none | 0 | missing_source_kind:claude |",
            "| case-5 | evidence_regression | none | 0 | missing_source_kind:claude |",
            "| case-6 | scope_regression | none | 0 | missing_source_kind:claude |",
        ],
    )
    _write(
        agentops / "HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md",
        [
            "- Status: passed",
            "- Evidence mode: real_inventory",
            "- Promotion eligible: true",
        ],
    )
    readiness_payload = {
        "status": "warning",
        "readiness_score": 88,
        "blockers": 3,
        "results": [
            {
                "requirement": "coding_scorecard_status",
                "status": "passed",
                "required": "passed",
                "actual": "passed",
                "evidence": "scorecard",
                "next_action": "none",
            },
            {
                "requirement": "coding_score",
                "status": "passed",
                "required": ">=90",
                "actual": "100",
                "evidence": "scorecard",
                "next_action": "none",
            },
            {
                "requirement": "coding_pass_rate",
                "status": "passed",
                "required": "all scenarios passed",
                "actual": "57/57",
                "evidence": "scorecard",
                "next_action": "none",
            },
            {
                "requirement": "source_stability",
                "status": "passed",
                "required": "stable",
                "actual": "stable; changes=0",
                "evidence": "scorecard",
                "next_action": "none",
            },
            {
                "requirement": "coding_scorecard_current_source_freshness",
                "status": "passed",
                "required": "current",
                "actual": "current; changes=0",
                "evidence": "churn",
                "next_action": "none",
            },
            {
                "requirement": "local_model_candidate_run_status",
                "status": "passed",
                "required": "local ready",
                "actual": "local_model source drop imported",
                "evidence": "intake",
                "next_action": "none",
            },
            {
                "requirement": "model_shadow_scorecard_status",
                "status": "warning",
                "required": "passed",
                "actual": "failed",
                "evidence": "shadow",
                "next_action": "Collect Claude source with the automated source runner.",
            },
            {
                "requirement": "model_shadow_real_manifest_mode",
                "status": "warning",
                "required": "real_manifest",
                "actual": "partial_real_manifest",
                "evidence": "shadow",
                "next_action": "Collect Claude source with the automated source runner.",
            },
            {
                "requirement": "model_tournament_scorecard_status",
                "status": "warning",
                "required": "passed",
                "actual": "failed",
                "evidence": "tournament",
                "next_action": "Collect Claude source with the automated source runner.",
            },
            {
                "requirement": "model_tournament_real_artifacts_mode",
                "status": "passed",
                "required": "real_artifacts",
                "actual": "real_artifacts",
                "evidence": "tournament",
                "next_action": "none",
            },
            {
                "requirement": "hosted_pr_repair_scorecard_status",
                "status": "passed",
                "required": "passed",
                "actual": "passed",
                "evidence": "hosted",
                "next_action": "none",
            },
            {
                "requirement": "hosted_pr_repair_real_inventory_mode",
                "status": "passed",
                "required": "real_inventory",
                "actual": "real_inventory",
                "evidence": "hosted",
                "next_action": "none",
            },
            {
                "requirement": "hosted_pr_repair_promotion_eligible",
                "status": "passed",
                "required": "true",
                "actual": "true",
                "evidence": "hosted",
                "next_action": "none",
            },
        ],
    }

    summary = matrix.build_gap_matrix(
        readiness_runner=_readiness_runner(readiness_payload),
        output_path=agentops / "FRONTIER_GAP_MATRIX.md",
    )
    markdown = matrix.render_report(summary)

    assert summary["status"] == "warning"
    assert summary["claim_status"] == "frontier_superiority_not_proven"
    assert summary["core_coding_proven"] is True
    assert summary["frontier_evidence_proven"] is False
    assert summary["frontier_superiority_proven"] is False
    assert summary["candidate_generation_superiority_proven"] is False
    assert summary["codex_head_to_head_available_sources_proven"] is True
    assert summary["tournament_winner_counts"]["none"] == 6
    assert summary["tournament_runtime_measurements"] == {
        "measured": 12,
        "unmeasured": 0,
    }
    assert summary["available_source_leader_counts"] == {
        "local_model": 6,
        "codex": 0,
        "claude": 0,
        "none": 0,
    }
    assert summary["tournament_unmeasured_runtime_count"] == 0
    assert summary["missing_sources"] == "claude"
    assert summary["claude_source_auth_mode"] == "subscription"
    assert summary["claude_api_key_probe_status"] == "api_key_missing"
    assert summary["gap_count"] == 4
    assert "model_shadow_scorecard_status" in {gap["gap_id"] for gap in summary["gaps"]}
    assert "candidate_generation_superiority" in {
        gap["gap_id"] for gap in summary["gaps"]
    }
    assert "frontier_superiority_not_proven" in markdown
    assert "python scripts/autopilot_frontier_source_runner.py --source-kind claude --source-auth-mode auto --json" in markdown
    assert "| frontier_source_evidence | not_proven |" in markdown
    assert "| candidate_generation_superiority | not_proven |" in markdown
    assert "| codex_head_to_head_available_sources | proven |" in markdown


def test_frontier_gap_matrix_passes_when_all_readiness_requirements_pass(
    tmp_path,
    monkeypatch,
):
    matrix = _load_module()
    agentops = _point_at_tmp(matrix, tmp_path, monkeypatch)
    _write_passing_system_evidence(agentops)
    _write(
        agentops / "CODING_BENCHMARK_SCORECARD.md",
        [
            "- Status: passed",
            "- Overall score: 100/100",
            "- Pass rate: 58/58",
            "- Source stability: stable",
        ],
    )
    _write(
        agentops / "SOURCE_CHURN_DIAGNOSTICS.md",
        [
            "- Status: passed",
            "- Current source freshness: current",
            "- Next action: none",
        ],
    )
    _write(
        agentops / "FRONTIER_MODEL_EVIDENCE_INTAKE.md",
        [
            "- Status: passed",
            "- Ready sources: 3/3",
            "- Missing/incomplete sources: none",
        ],
    )
    _write(
        agentops / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md",
        ["- Status: passed", "- Evidence mode: real_manifest"],
    )
    _write(
        agentops / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md",
        [
            "- Status: passed",
            "- Evidence mode: real_artifacts",
            "- Source kinds: codex, claude, local_model",
            "- Runtime measurements: measured=18, unmeasured=0",
            "- Available-source leader counts: local_model=6, codex=0, claude=0, none=0",
            "",
            "| Case | Comparison Class | Winner | Score | Evidence |",
            "| --- | --- | --- | ---: | --- |",
            "| case-1 | strict_candidate_win | local_model/local-1 | 100 | behavior_tests_passed |",
            "| case-2 | runtime_control_behavior_regression | local_model/local-2 | 100 | behavior_tests_passed |",
            "| case-3 | startup_contract_behavior_regression | local_model/local-3 | 100 | behavior_tests_passed |",
            "| case-4 | preflight_behavior_regression | local_model/local-4 | 100 | behavior_tests_passed |",
            "| case-5 | evidence_regression | local_model/local-5 | 100 | behavior_tests_passed |",
            "| case-6 | scope_regression | local_model/local-6 | 100 | behavior_tests_passed |",
        ],
    )
    _write(
        agentops / "HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md",
        [
            "- Status: passed",
            "- Evidence mode: real_inventory",
            "- Promotion eligible: true",
        ],
    )
    readiness_payload = {
        "status": "passed",
        "readiness_score": 100,
        "blockers": 0,
        "results": [
            {
                "requirement": "coding_scorecard_status",
                "status": "passed",
                "required": "passed",
                "actual": "passed",
                "evidence": "scorecard",
                "next_action": "none",
            },
            {
                "requirement": "coding_score",
                "status": "passed",
                "required": ">=90",
                "actual": "100",
                "evidence": "scorecard",
                "next_action": "none",
            },
            {
                "requirement": "coding_pass_rate",
                "status": "passed",
                "required": "all",
                "actual": "58/58",
                "evidence": "scorecard",
                "next_action": "none",
            },
            {
                "requirement": "source_stability",
                "status": "passed",
                "required": "stable",
                "actual": "stable; changes=0",
                "evidence": "scorecard",
                "next_action": "none",
            },
            {
                "requirement": "coding_scorecard_current_source_freshness",
                "status": "passed",
                "required": "current",
                "actual": "current; changes=0",
                "evidence": "churn",
                "next_action": "none",
            },
            {
                "requirement": "local_model_candidate_run_status",
                "status": "passed",
                "required": "local",
                "actual": "local_model source drop imported",
                "evidence": "intake",
                "next_action": "none",
            },
            {
                "requirement": "model_shadow_scorecard_status",
                "status": "passed",
                "required": "passed",
                "actual": "passed",
                "evidence": "shadow",
                "next_action": "none",
            },
            {
                "requirement": "model_shadow_real_manifest_mode",
                "status": "passed",
                "required": "real_manifest",
                "actual": "real_manifest",
                "evidence": "shadow",
                "next_action": "none",
            },
            {
                "requirement": "model_tournament_scorecard_status",
                "status": "passed",
                "required": "passed",
                "actual": "passed",
                "evidence": "tournament",
                "next_action": "none",
            },
            {
                "requirement": "model_tournament_real_artifacts_mode",
                "status": "passed",
                "required": "real_artifacts",
                "actual": "real_artifacts",
                "evidence": "tournament",
                "next_action": "none",
            },
            {
                "requirement": "hosted_pr_repair_scorecard_status",
                "status": "passed",
                "required": "passed",
                "actual": "passed",
                "evidence": "hosted",
                "next_action": "none",
            },
            {
                "requirement": "hosted_pr_repair_real_inventory_mode",
                "status": "passed",
                "required": "real_inventory",
                "actual": "real_inventory",
                "evidence": "hosted",
                "next_action": "none",
            },
            {
                "requirement": "hosted_pr_repair_promotion_eligible",
                "status": "passed",
                "required": "true",
                "actual": "true",
                "evidence": "hosted",
                "next_action": "none",
            },
        ],
    }

    summary = matrix.build_gap_matrix(
        readiness_runner=_readiness_runner(readiness_payload),
        output_path=agentops / "FRONTIER_GAP_MATRIX.md",
    )
    markdown = matrix.render_report(summary)

    assert summary["status"] == "passed"
    assert summary["claim_status"] == "frontier_superiority_proven"
    assert summary["gap_count"] == 0
    assert summary["next_action"] == "none"
    assert summary["frontier_evidence_proven"] is True
    assert summary["frontier_superiority_proven"] is True
    assert summary["candidate_generation_superiority_proven"] is True
    assert summary["offline_autonomy_proven"] is True
    assert summary["meso_workflow_superiority_proven"] is True
    assert summary["macro_long_horizon_superiority_proven"] is True
    assert summary["deep_context_superiority_proven"] is True
    assert summary["codex_head_to_head_available_sources_proven"] is True
    assert summary["tournament_winner_counts"]["local_model"] == 6
    assert summary["available_source_leader_counts"]["local_model"] == 6
    assert summary["local_model_winner_count"] == 6
    assert summary["frontier_model_winner_count"] == 0
    assert summary["tournament_unmeasured_runtime_count"] == 0
    assert "| none | passed | none | none | none | none |" in markdown


def test_frontier_gap_matrix_does_not_expand_micro_win_into_system_superiority(
    tmp_path,
    monkeypatch,
):
    matrix = _load_module()
    agentops = _point_at_tmp(matrix, tmp_path, monkeypatch)
    _write(
        agentops / "CODING_BENCHMARK_SCORECARD.md",
        [
            "- Status: passed",
            "- Overall score: 100/100",
            "- Pass rate: 67/67",
            "- Source stability: stable",
        ],
    )
    _write(
        agentops / "SOURCE_CHURN_DIAGNOSTICS.md",
        ["- Current source freshness: current", "- Next action: none"],
    )
    _write(
        agentops / "FRONTIER_MODEL_EVIDENCE_INTAKE.md",
        ["- Status: passed", "- Ready sources: 3/3", "- Missing/incomplete sources: none"],
    )
    _write(
        agentops / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md",
        ["- Status: passed", "- Evidence mode: real_manifest"],
    )
    _write(
        agentops / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md",
        [
            "- Status: passed",
            "- Evidence mode: real_artifacts",
            "- Source kinds: codex, claude, local_model",
            "- Runtime measurements: measured=18, unmeasured=0",
            "- Available-source leader counts: local_model=6, codex=0, claude=0, none=0",
            "",
            "| Case | Comparison Class | Winner | Score | Evidence |",
            "| --- | --- | --- | ---: | --- |",
            *[
                f"| case-{index} | strict_candidate_win | local_model/local-{index} | 100 | behavior_tests_passed |"
                for index in range(1, 7)
            ],
        ],
    )
    _write(
        agentops / "HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md",
        [
            "- Status: passed",
            "- Evidence mode: real_inventory",
            "- Promotion eligible: true",
        ],
    )
    _write(
        agentops / "OFFLINE_PROJECT_AUTONOMY_BENCHMARK.md",
        ["- Status: passed", "- Average score: 100/100"],
    )

    summary = matrix.build_gap_matrix(
        readiness_runner=_readiness_runner(_all_passed_readiness_payload()),
        output_path=agentops / "FRONTIER_GAP_MATRIX.md",
    )

    assert summary["micro_candidate_superiority_proven"] is True
    assert summary["offline_autonomy_proven"] is True
    assert summary["meso_workflow_superiority_proven"] is False
    assert summary["macro_long_horizon_superiority_proven"] is False
    assert summary["deep_context_superiority_proven"] is False
    assert summary["frontier_superiority_proven"] is False
    assert summary["claim_status"] == "frontier_superiority_not_proven"
    assert {gap["gap_id"] for gap in summary["gaps"]} == {
        "meso_project_workflow_superiority",
        "macro_long_horizon_superiority",
        "deep_context_reasoning_superiority",
    }


def test_frontier_gap_matrix_keeps_superiority_blocked_when_frontier_wins_cases(
    tmp_path,
    monkeypatch,
):
    matrix = _load_module()
    agentops = _point_at_tmp(matrix, tmp_path, monkeypatch)
    _write_passing_system_evidence(agentops)
    _write(
        agentops / "CODING_BENCHMARK_SCORECARD.md",
        [
            "- Status: passed",
            "- Overall score: 100/100",
            "- Pass rate: 58/58",
            "- Source stability: stable",
        ],
    )
    _write(
        agentops / "SOURCE_CHURN_DIAGNOSTICS.md",
        [
            "- Status: passed",
            "- Current source freshness: current",
            "- Next action: none",
        ],
    )
    _write(
        agentops / "FRONTIER_MODEL_EVIDENCE_INTAKE.md",
        [
            "- Status: passed",
            "- Ready sources: 3/3",
            "- Missing/incomplete sources: none",
        ],
    )
    _write(
        agentops / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md",
        ["- Status: passed", "- Evidence mode: real_manifest"],
    )
    _write(
        agentops / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md",
        [
            "- Status: passed",
            "- Evidence mode: real_artifacts",
            "- Runtime measurements: measured=18, unmeasured=0",
            "",
            "| Case | Comparison Class | Winner | Score | Evidence |",
            "| --- | --- | --- | ---: | --- |",
            "| case-1 | strict_candidate_win | local_model/local-1 | 100 | behavior_tests_passed |",
            "| case-2 | runtime_control_behavior_regression | local_model/local-2 | 100 | behavior_tests_passed |",
            "| case-3 | startup_contract_behavior_regression | local_model/local-3 | 100 | behavior_tests_passed |",
            "| case-4 | preflight_behavior_regression | codex/codex-1 | 100 | behavior_tests_passed |",
            "| case-5 | evidence_regression | codex/codex-2 | 100 | behavior_tests_passed |",
            "| case-6 | scope_regression | claude/claude-1 | 100 | behavior_tests_passed |",
        ],
    )
    _write(
        agentops / "HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md",
        [
            "- Status: passed",
            "- Evidence mode: real_inventory",
            "- Promotion eligible: true",
        ],
    )
    passed_results = [
        {
            "requirement": requirement,
            "status": "passed",
            "required": "passed",
            "actual": "passed",
            "evidence": "evidence",
            "next_action": "none",
        }
        for requirement in [
            "coding_scorecard_status",
            "coding_score",
            "coding_pass_rate",
            "source_stability",
            "coding_scorecard_current_source_freshness",
            "local_model_candidate_run_status",
            "model_shadow_scorecard_status",
            "model_shadow_real_manifest_mode",
            "model_tournament_scorecard_status",
            "model_tournament_real_artifacts_mode",
            "hosted_pr_repair_scorecard_status",
            "hosted_pr_repair_real_inventory_mode",
            "hosted_pr_repair_promotion_eligible",
        ]
    ]
    readiness_payload = {
        "status": "passed",
        "readiness_score": 100,
        "blockers": 0,
        "results": passed_results,
    }

    summary = matrix.build_gap_matrix(
        readiness_runner=_readiness_runner(readiness_payload),
        output_path=agentops / "FRONTIER_GAP_MATRIX.md",
    )
    markdown = matrix.render_report(summary)

    assert summary["status"] == "warning"
    assert summary["claim_status"] == "frontier_superiority_not_proven"
    assert summary["frontier_evidence_proven"] is True
    assert summary["frontier_superiority_proven"] is False
    assert summary["candidate_generation_superiority_proven"] is False
    assert summary["gap_count"] == 1
    assert summary["tournament_winner_counts"] == {
        "local_model": 3,
        "codex": 2,
        "claude": 1,
    }
    assert summary["frontier_model_winner_count"] == 3
    assert summary["gaps"][0]["gap_id"] == "candidate_generation_superiority"
    assert "stronger local/CHILI candidate evidence" in summary["gaps"][0]["next_action"]
    assert "candidate_generation_superiority" in markdown


def test_frontier_gap_matrix_blocks_all_local_winners_with_unmeasured_runtime(
    tmp_path,
    monkeypatch,
):
    matrix = _load_module()
    agentops = _point_at_tmp(matrix, tmp_path, monkeypatch)
    _write_passing_system_evidence(agentops)
    _write(
        agentops / "CODING_BENCHMARK_SCORECARD.md",
        [
            "- Status: passed",
            "- Overall score: 100/100",
            "- Pass rate: 58/58",
            "- Source stability: stable",
        ],
    )
    _write(
        agentops / "SOURCE_CHURN_DIAGNOSTICS.md",
        [
            "- Status: passed",
            "- Current source freshness: current",
            "- Next action: none",
        ],
    )
    _write(
        agentops / "FRONTIER_MODEL_EVIDENCE_INTAKE.md",
        [
            "- Status: passed",
            "- Ready sources: 3/3",
            "- Missing/incomplete sources: none",
        ],
    )
    _write(
        agentops / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md",
        ["- Status: passed", "- Evidence mode: real_manifest"],
    )
    _write(
        agentops / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md",
        [
            "- Status: passed",
            "- Evidence mode: real_artifacts",
            "- Runtime measurements: measured=12, unmeasured=6",
            "",
            "| Case | Comparison Class | Winner | Score | Evidence |",
            "| --- | --- | --- | ---: | --- |",
            "| case-1 | strict_candidate_win | local_model/local-1 | 100 | behavior_tests_passed |",
            "| case-2 | runtime_control_behavior_regression | local_model/local-2 | 100 | behavior_tests_passed |",
            "| case-3 | startup_contract_behavior_regression | local_model/local-3 | 100 | behavior_tests_passed |",
            "| case-4 | preflight_behavior_regression | local_model/local-4 | 100 | behavior_tests_passed |",
            "| case-5 | evidence_regression | local_model/local-5 | 100 | behavior_tests_passed |",
            "| case-6 | scope_regression | local_model/local-6 | 100 | behavior_tests_passed |",
        ],
    )
    _write(
        agentops / "HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md",
        [
            "- Status: passed",
            "- Evidence mode: real_inventory",
            "- Promotion eligible: true",
        ],
    )
    readiness_payload = {
        "status": "passed",
        "readiness_score": 100,
        "blockers": 0,
        "results": [
            {
                "requirement": requirement,
                "status": "passed",
                "required": "passed",
                "actual": "passed",
                "evidence": "evidence",
                "next_action": "none",
            }
            for requirement in [
                "coding_scorecard_status",
                "coding_score",
                "coding_pass_rate",
                "source_stability",
                "coding_scorecard_current_source_freshness",
                "local_model_candidate_run_status",
                "model_shadow_scorecard_status",
                "model_shadow_real_manifest_mode",
                "model_tournament_scorecard_status",
                "model_tournament_real_artifacts_mode",
                "hosted_pr_repair_scorecard_status",
                "hosted_pr_repair_real_inventory_mode",
                "hosted_pr_repair_promotion_eligible",
            ]
        ],
    }

    summary = matrix.build_gap_matrix(
        readiness_runner=_readiness_runner(readiness_payload),
        output_path=agentops / "FRONTIER_GAP_MATRIX.md",
    )

    assert summary["frontier_evidence_proven"] is True
    assert summary["candidate_generation_superiority_proven"] is False
    assert summary["tournament_winner_counts"] == {"local_model": 6}
    assert summary["tournament_unmeasured_runtime_count"] == 6
    assert summary["gap_count"] == 1
    assert summary["gaps"][0]["gap_id"] == "candidate_generation_superiority"
    assert "unmeasured=6" in summary["gaps"][0]["actual"]


def test_frontier_gap_matrix_cli_fail_on_gap_uses_exit_code(tmp_path, monkeypatch):
    matrix = _load_module()
    agentops = _point_at_tmp(matrix, tmp_path, monkeypatch)

    def fake_build_gap_matrix(*, output_path):
        return {
            "schema": matrix.FRONTIER_GAP_MATRIX_SCHEMA_VERSION,
            "generated_utc": "2026-07-10T00:00:00Z",
            "status": "warning",
            "claim_status": "frontier_superiority_not_proven",
            "readiness_score": 88,
            "readiness_blockers": 1,
            "core_coding_proven": True,
            "frontier_evidence_proven": False,
            "missing_sources": "claude",
            "claude_source_auth_mode": "subscription",
            "claude_api_key_probe_status": "api_key_missing",
            "claude_source_runner_command": "runner",
            "next_action": "collect claude",
            "proof_matrix": [],
            "gaps": [
                {
                    "gap_id": "model_shadow_scorecard_status",
                    "status": "warning",
                    "required": "passed",
                    "actual": "failed",
                    "evidence": "shadow",
                    "next_action": "collect claude",
                }
            ],
        }

    monkeypatch.setattr(matrix, "build_gap_matrix", fake_build_gap_matrix)

    exit_code = matrix.main(
        [
            "--output",
            str(agentops / "FRONTIER_GAP_MATRIX.md"),
            "--no-write",
            "--fail-on-gap",
        ]
    )

    assert exit_code == 1
