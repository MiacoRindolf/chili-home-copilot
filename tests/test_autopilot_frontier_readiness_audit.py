from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "autopilot_frontier_readiness_audit.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "autopilot_frontier_readiness_audit",
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


def _point_at_tmp(audit, tmp_path: Path, monkeypatch) -> Path:
    agentops = tmp_path / "project_ws" / "AgentOps"
    monkeypatch.setattr(audit, "AGENTOPS_ROOT", agentops)
    monkeypatch.setattr(audit, "DEFAULT_OUTPUT", agentops / "FRONTIER_READINESS_AUDIT.md")
    monkeypatch.setattr(audit, "CODING_SCORECARD", agentops / "CODING_BENCHMARK_SCORECARD.md")
    monkeypatch.setattr(audit, "SOURCE_CHURN_DIAGNOSTICS", agentops / "SOURCE_CHURN_DIAGNOSTICS.md")
    monkeypatch.setattr(audit, "SYNTHETIC_REPO_REPAIR_SCORECARD", agentops / "SYNTHETIC_REPO_REPAIR_BENCHMARK.md")
    monkeypatch.setattr(audit, "MODEL_PROMOTION_SCORECARD", agentops / "MODEL_PROMOTION_REPLAY_BENCHMARK.md")
    monkeypatch.setattr(audit, "MODEL_SHADOW_SCORECARD", agentops / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md")
    monkeypatch.setattr(audit, "MODEL_TOURNAMENT_SCORECARD", agentops / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md")
    monkeypatch.setattr(audit, "HOSTED_PR_REPAIR_SCORECARD", agentops / "HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md")
    monkeypatch.setattr(audit, "OFFLINE_AUTONOMY_SCORECARD", agentops / "OFFLINE_PROJECT_AUTONOMY_BENCHMARK.md")
    monkeypatch.setattr(audit, "HOSTED_PR_REPAIR_CANDIDATE_SCAN", agentops / "HOSTED_PR_REPAIR_CANDIDATE_SCAN.md")
    monkeypatch.setattr(audit, "FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS", agentops / "FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS.md")
    monkeypatch.setattr(audit, "FRONTIER_MODEL_EVIDENCE_INTAKE", agentops / "FRONTIER_MODEL_EVIDENCE_INTAKE.md")
    monkeypatch.setattr(audit, "LOCAL_MODEL_CANDIDATE_RUN", agentops / "LOCAL_MODEL_CANDIDATE_RUN.md")
    monkeypatch.setattr(
        audit,
        "selected_pytest_runtime",
        lambda: audit.PytestRuntimeEvidence(
            actual="8.4.2",
            python=str(tmp_path / ".pytest_venv" / "Scripts" / "python.exe"),
            source=".pytest_venv",
            isolation_status="isolated",
        ),
    )
    monkeypatch.setattr(audit, "benchmark_harness_missing_refs", lambda: [])
    return agentops


def test_frontier_readiness_audit_passes_with_current_real_evidence(tmp_path, monkeypatch):
    audit = _load_module()
    agentops = _point_at_tmp(audit, tmp_path, monkeypatch)
    _write(
        agentops / "CODING_BENCHMARK_SCORECARD.md",
        [
            "- Status: passed",
            "- Overall score: 100/100",
            "- Scenarios: 56",
            "- Pass rate: 56/56",
            "- Source stability: stable",
            "- Source changes during run: 0",
            "- Required capabilities: robust plan JSON extraction, current source freshness gate",
            "- Capability coverage: robust plan JSON extraction, current source freshness gate",
        ],
    )
    _write(
        agentops / "SOURCE_CHURN_DIAGNOSTICS.md",
        [
            "- Status: passed",
            "- Current source freshness: current",
            "- Source changes after scorecard: 0",
            "- Next action: none",
        ],
    )
    _write(agentops / "SYNTHETIC_REPO_REPAIR_BENCHMARK.md", ["- Status: passed"])
    _write(agentops / "MODEL_PROMOTION_REPLAY_BENCHMARK.md", ["- Status: passed"])
    _write(
        agentops / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md",
        ["- Status: passed", "- Checks: 7", "- Evidence mode: real_manifest"],
    )
    _write(
        agentops / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md",
        ["- Status: passed", "- Cases: 6", "- Evidence mode: real_artifacts"],
    )
    _write(
        agentops / "HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md",
        [
            "- Status: passed",
            "- Checks: 18",
            "- Evidence mode: real_inventory",
            "- Promotion eligible: true",
        ],
    )
    _write(
        agentops / "OFFLINE_PROJECT_AUTONOMY_BENCHMARK.md",
        [
            "- Status: passed",
            "- Average score: 100/100",
            "| local_dependency_policy | 100 | premium_models_required=false |",
            "| offline_local_plan_edit_test_review | 100 | premium_calls=0 |",
        ],
    )
    _write(agentops / "FRONTIER_MODEL_EVIDENCE_INTAKE.md", ["- Status: passed"])

    markdown, payload = audit.render_audit(audit.build_requirements())

    assert payload["status"] == "passed"
    assert payload["readiness_score"] == 100
    assert payload["blockers"] == 0
    assert all(row["next_action"] == "none" for row in payload["results"])
    assert "| coding_scorecard_status | passed |" in markdown
    assert "| model_tournament_real_artifacts_mode | passed |" in markdown
    assert "| offline_project_autonomy_zero_premium_calls | passed |" in markdown


def test_frontier_readiness_blocks_when_offline_receipt_has_premium_calls(
    tmp_path,
    monkeypatch,
):
    audit = _load_module()
    agentops = _point_at_tmp(audit, tmp_path, monkeypatch)
    _write(
        agentops / "OFFLINE_PROJECT_AUTONOMY_BENCHMARK.md",
        [
            "- Status: passed",
            "- Average score: 100/100",
            "| local_dependency_policy | 100 | premium_models_required=false |",
            "| offline_local_plan_edit_test_review | 100 | premium_calls=1 |",
        ],
    )

    _, payload = audit.render_audit(audit.build_requirements())
    by_requirement = {row["requirement"]: row for row in payload["results"]}

    premium = by_requirement["offline_project_autonomy_zero_premium_calls"]
    assert premium["status"] == "warning"
    assert premium["actual"] == "premium-disconnected receipt missing"
    assert "autopilot_offline_project_autonomy_benchmark.py" in premium["next_action"]


def test_frontier_readiness_audit_surfaces_missing_scorecard_with_churn_recovery(
    tmp_path,
    monkeypatch,
):
    audit = _load_module()
    agentops = _point_at_tmp(audit, tmp_path, monkeypatch)
    _write(
        agentops / "SOURCE_CHURN_DIAGNOSTICS.md",
        [
            "- Status: blocked",
            "- Current source freshness: scorecard_missing",
            "- Source changes after scorecard: 0",
            "- Next action: Generate the coding benchmark scorecard with a source quiet preflight.",
        ],
    )

    markdown, payload = audit.render_audit(audit.build_requirements())
    by_requirement = {row["requirement"]: row for row in payload["results"]}

    assert payload["status"] == "warning"
    assert payload["blockers"] > 0
    assert by_requirement["coding_scorecard_status"]["status"] == "warning"
    assert by_requirement["coding_scorecard_status"]["actual"] == "missing"
    assert "Generate the coding benchmark scorecard" in by_requirement["coding_scorecard_status"]["next_action"]
    assert by_requirement["coding_scorecard_current_source_freshness"]["actual"].startswith("scorecard_missing")
    assert "SOURCE_CHURN_DIAGNOSTICS.md" in markdown


def test_frontier_readiness_audit_surfaces_failed_local_model_candidate_run(
    tmp_path,
    monkeypatch,
):
    audit = _load_module()
    agentops = _point_at_tmp(audit, tmp_path, monkeypatch)
    _write(
        agentops / "CODING_BENCHMARK_SCORECARD.md",
        [
            "- Status: passed",
            "- Overall score: 100/100",
            "- Scenarios: 56",
            "- Pass rate: 56/56",
            "- Source stability: stable",
            "- Source changes during run: 0",
            "- Required capabilities: robust plan JSON extraction",
            "- Capability coverage: robust plan JSON extraction",
        ],
    )
    _write(
        agentops / "SOURCE_CHURN_DIAGNOSTICS.md",
        [
            "- Status: passed",
            "- Current source freshness: current",
            "- Source changes after scorecard: 0",
            "- Next action: none",
        ],
    )
    _write(agentops / "SYNTHETIC_REPO_REPAIR_BENCHMARK.md", ["- Status: passed"])
    _write(agentops / "MODEL_PROMOTION_REPLAY_BENCHMARK.md", ["- Status: passed"])
    _write(
        agentops / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md",
        ["- Status: failed", "- Checks: 0", "- Evidence mode: partial_real_manifest"],
    )
    _write(
        agentops / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md",
        ["- Status: failed", "- Cases: 0", "- Evidence mode: self_test"],
    )
    _write(
        agentops / "LOCAL_MODEL_CANDIDATE_RUN.md",
        [
            "- Status: failed",
            "- Promotion ready: False",
            "- Run id: local-suite-1",
            "- Failure stage: parse",
            "- Failed case: real-chili-preflight-candidate-wins",
            "- Failure reason: model response did not contain a valid JSON object",
            "- Next action: Retry the failed case or import a corrected response.",
        ],
    )

    _, payload = audit.render_audit(audit.build_requirements())
    by_requirement = {row["requirement"]: row for row in payload["results"]}
    local = by_requirement["local_model_candidate_run_status"]

    assert local["status"] == "warning"
    assert "status=failed" in local["actual"]
    assert "failed_case=real-chili-preflight-candidate-wins" in local["actual"]
    assert "model response did not contain a valid JSON object" in local["actual"]
    assert local["next_action"] == "Retry the failed case or import a corrected response."


def test_frontier_readiness_uses_current_intake_partial_scorecards(
    tmp_path,
    monkeypatch,
):
    audit = _load_module()
    agentops = _point_at_tmp(audit, tmp_path, monkeypatch)
    _write(
        agentops / "CODING_BENCHMARK_SCORECARD.md",
        [
            "- Status: passed",
            "- Overall score: 100/100",
            "- Scenarios: 56",
            "- Pass rate: 56/56",
            "- Source stability: stable",
            "- Source changes during run: 0",
            "- Required capabilities: robust plan JSON extraction",
            "- Capability coverage: robust plan JSON extraction",
        ],
    )
    _write(
        agentops / "SOURCE_CHURN_DIAGNOSTICS.md",
        [
            "- Status: passed",
            "- Current source freshness: current",
            "- Source changes after scorecard: 0",
            "- Next action: none",
        ],
    )
    _write(agentops / "SYNTHETIC_REPO_REPAIR_BENCHMARK.md", ["- Status: passed"])
    _write(agentops / "MODEL_PROMOTION_REPLAY_BENCHMARK.md", ["- Status: passed"])
    _write(
        agentops / "LOCAL_MODEL_CANDIDATE_RUN.md",
        [
            "- Status: passed",
            "- Promotion ready: False",
            "- Run id: local-model-run-1",
        ],
    )
    _write(
        agentops / "FRONTIER_MODEL_EVIDENCE_INTAKE.md",
        [
            "- Status: warning",
            "- Source kinds: local_model",
            "- Ready sources: 1/3",
            "- Missing/incomplete sources: codex, claude",
            "- Shadow evidence mode: partial_real_manifest",
            "- Shadow status: failed",
            "- Tournament evidence mode: real_artifacts",
            "- Tournament status: failed",
            "",
            "## Source Readiness",
            "",
            "| Source | Path | Status | Raw drops | Missing files | Next action |",
            "| --- | --- | --- | ---: | --- | --- |",
            (
                "| claude | claude | partial | 0 | claude/raw/*.json | "
                "Availability recovery: Re-authenticate. Then build/use the collection packet "
                "and import evidence: Automated source runner: python scripts/autopilot_frontier_source_runner.py "
                "--source-kind claude --source-auth-mode auto --json; if it passes, validate intake with "
                "python scripts/autopilot_frontier_model_evidence_intake.py --input-root "
                "project_ws/AgentOps/frontier_model_evidence_intake/raw_sources --allow-partial --json --no-write. "
                "Manual fallback: python scripts/autopilot_frontier_source_collection_packet.py "
                "--source-kind claude --json |"
            ),
        ],
    )
    _write(
        agentops
        / "frontier_model_evidence_intake"
        / "scorecards"
        / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md",
        [
            "- Status: failed",
            "- Checks: 7",
            "- Evidence mode: partial_real_manifest",
        ],
    )
    _write(
        agentops
        / "frontier_model_evidence_intake"
        / "scorecards"
        / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md",
        [
            "- Status: failed",
            "- Cases: 1",
            "- Evidence mode: real_artifacts",
        ],
    )

    markdown, payload = audit.render_audit(audit.build_requirements())
    by_requirement = {row["requirement"]: row for row in payload["results"]}

    assert by_requirement["local_model_candidate_run_status"]["status"] == "passed"
    assert by_requirement["local_model_candidate_run_status"]["actual"] == "local_model source drop imported"
    assert by_requirement["model_shadow_check_count"]["status"] == "passed"
    assert by_requirement["model_shadow_check_count"]["actual"] == "7"
    assert by_requirement["model_tournament_real_artifacts_mode"]["status"] == "passed"
    assert by_requirement["model_tournament_case_count"]["actual"] == "1"
    assert by_requirement["model_tournament_scorecard_status"]["status"] == "warning"
    assert "missing/incomplete sources: codex, claude" in by_requirement["model_tournament_scorecard_status"]["next_action"]
    assert "frontier_model_evidence_intake/scorecards/MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md" in markdown


def test_frontier_readiness_surfaces_local_model_tournament_failures(
    tmp_path,
    monkeypatch,
):
    audit = _load_module()
    agentops = _point_at_tmp(audit, tmp_path, monkeypatch)
    _write(
        agentops / "CODING_BENCHMARK_SCORECARD.md",
        [
            "- Status: passed",
            "- Overall score: 100/100",
            "- Scenarios: 56",
            "- Pass rate: 56/56",
            "- Source stability: stable",
            "- Source changes during run: 0",
            "- Required capabilities: robust plan JSON extraction",
            "- Capability coverage: robust plan JSON extraction",
        ],
    )
    _write(
        agentops / "SOURCE_CHURN_DIAGNOSTICS.md",
        [
            "- Status: passed",
            "- Current source freshness: current",
            "- Source changes after scorecard: 0",
            "- Next action: none",
        ],
    )
    _write(agentops / "SYNTHETIC_REPO_REPAIR_BENCHMARK.md", ["- Status: passed"])
    _write(agentops / "MODEL_PROMOTION_REPLAY_BENCHMARK.md", ["- Status: passed"])
    _write(
        agentops / "FRONTIER_MODEL_EVIDENCE_INTAKE.md",
        [
            "- Status: warning",
            "- Source kinds: codex, local_model",
            "- Ready sources: 2/3",
            "- Missing/incomplete sources: claude",
            "- Shadow evidence mode: partial_real_manifest",
            "- Shadow status: failed",
            "- Tournament evidence mode: real_artifacts",
            "- Tournament status: failed",
        ],
    )
    _write(
        agentops
        / "frontier_model_evidence_intake"
        / "scorecards"
        / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md",
        [
            "- Status: failed",
            "- Checks: 7",
            "- Evidence mode: partial_real_manifest",
        ],
    )
    _write(
        agentops
        / "frontier_model_evidence_intake"
        / "scorecards"
        / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md",
        [
            "- Status: failed",
            "- Cases: 2",
            "- Evidence mode: real_artifacts",
            "| Case | Comparison Class | Winner | Score | Evidence |",
            "| --- | --- | --- | ---: | --- |",
            (
                "| real-chili-a | strict_candidate_win | none | 0 | "
                "reason=missing_source_kind:claude; sources=codex,local_model; "
                "passed=1; rejected=1; "
                "rejected_examples=local_model/local_model-a:failed/apply_failed |"
            ),
            (
                "| real-chili-b | startup_contract_behavior_regression | none | 0 | "
                "reason=missing_source_kind:claude; sources=codex,local_model; "
                "passed=2; rejected=0 |"
            ),
        ],
    )

    _, payload = audit.render_audit(audit.build_requirements())
    by_requirement = {row["requirement"]: row for row in payload["results"]}

    codex = by_requirement["codex_tournament_case_pass_count"]
    local = by_requirement["local_model_tournament_case_pass_count"]

    assert codex["status"] == "passed"
    assert codex["actual"] == "present=2/2; passed=2/2; rejected=0"
    assert local["status"] == "warning"
    assert local["actual"] == "present=2/2; passed=1/2; rejected=1"
    assert "stronger all-cases local-model response" in local["next_action"]


def test_frontier_readiness_does_not_reask_for_passing_local_model_tournament(
    tmp_path,
    monkeypatch,
):
    audit = _load_module()
    agentops = _point_at_tmp(audit, tmp_path, monkeypatch)
    _write(
        agentops / "CODING_BENCHMARK_SCORECARD.md",
        [
            "- Status: passed",
            "- Overall score: 100/100",
            "- Scenarios: 56",
            "- Pass rate: 56/56",
            "- Source stability: stable",
            "- Source changes during run: 0",
            "- Required capabilities: robust plan JSON extraction",
            "- Capability coverage: robust plan JSON extraction",
        ],
    )
    _write(
        agentops / "SOURCE_CHURN_DIAGNOSTICS.md",
        [
            "- Status: passed",
            "- Current source freshness: current",
            "- Source changes after scorecard: 0",
            "- Next action: none",
        ],
    )
    _write(agentops / "SYNTHETIC_REPO_REPAIR_BENCHMARK.md", ["- Status: passed"])
    _write(agentops / "MODEL_PROMOTION_REPLAY_BENCHMARK.md", ["- Status: passed"])
    _write(
        agentops / "FRONTIER_MODEL_EVIDENCE_INTAKE.md",
        [
            "- Status: warning",
            "- Source kinds: codex, local_model",
            "- Ready sources: 2/3",
            "- Missing/incomplete sources: claude",
            "- Shadow evidence mode: partial_real_manifest",
            "- Shadow status: failed",
            "- Tournament evidence mode: real_artifacts",
            "- Tournament status: failed",
        ],
    )
    _write(
        agentops
        / "frontier_model_evidence_intake"
        / "scorecards"
        / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md",
        [
            "- Status: failed",
            "- Checks: 7",
            "- Evidence mode: partial_real_manifest",
        ],
    )
    _write(
        agentops
        / "frontier_model_evidence_intake"
        / "scorecards"
        / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md",
        [
            "- Status: failed",
            "- Cases: 2",
            "- Evidence mode: real_artifacts",
            "| Case | Comparison Class | Winner | Score | Evidence |",
            "| --- | --- | --- | ---: | --- |",
            (
                "| real-chili-a | strict_candidate_win | none | 0 | "
                "reason=missing_source_kind:claude; sources=codex,local_model; "
                "passed=2; rejected=0; "
                "passed_examples=codex/codex-a:passed/behavior_tests_passed;"
                "local_model/local_model-a:passed/behavior_tests_passed; "
                "unmeasured_runtime=local_model/local_model-a |"
            ),
            (
                "| real-chili-b | startup_contract_behavior_regression | none | 0 | "
                "reason=missing_source_kind:claude; sources=codex,local_model; "
                "passed=2; rejected=0; "
                "passed_examples=codex/codex-b:passed/behavior_tests_passed;"
                "local_model/local_model-b:passed/behavior_tests_passed; "
                "unmeasured_runtime=local_model/local_model-b |"
            ),
        ],
    )

    _, payload = audit.render_audit(audit.build_requirements())
    by_requirement = {row["requirement"]: row for row in payload["results"]}
    local = by_requirement["local_model_tournament_case_pass_count"]

    assert local["status"] == "passed"
    assert local["actual"] == "present=2/2; passed=2/2; rejected=0"
    assert local["next_action"] == "none"


def test_frontier_readiness_surfaces_hosted_pr_candidate_scan_gap(
    tmp_path,
    monkeypatch,
):
    audit = _load_module()
    agentops = _point_at_tmp(audit, tmp_path, monkeypatch)
    _write(
        agentops / "CODING_BENCHMARK_SCORECARD.md",
        [
            "- Status: passed",
            "- Overall score: 100/100",
            "- Scenarios: 56",
            "- Pass rate: 56/56",
            "- Source stability: stable",
            "- Source changes during run: 0",
            "- Required capabilities: robust plan JSON extraction",
            "- Capability coverage: robust plan JSON extraction",
        ],
    )
    _write(
        agentops / "SOURCE_CHURN_DIAGNOSTICS.md",
        [
            "- Status: passed",
            "- Current source freshness: current",
            "- Source changes after scorecard: 0",
            "- Next action: none",
        ],
    )
    _write(agentops / "SYNTHETIC_REPO_REPAIR_BENCHMARK.md", ["- Status: passed"])
    _write(agentops / "MODEL_PROMOTION_REPLAY_BENCHMARK.md", ["- Status: passed"])
    _write(
        agentops / "HOSTED_PR_REPAIR_CANDIDATE_SCAN.md",
        [
            "- Status: no_review_thread_candidates",
            "- PRs scanned: 300",
            "- Review-thread candidates: 0",
            "- Next action: Find or create a hosted repair PR with review-thread line detail, publication/current-head proof, and a green post-repair check receipt.",
        ],
    )

    _, payload = audit.render_audit(audit.build_requirements())
    by_requirement = {row["requirement"]: row for row in payload["results"]}
    hosted = by_requirement["hosted_pr_repair_scorecard_status"]

    assert hosted["status"] == "warning"
    assert "0 review-thread candidates" in hosted["next_action"]
    assert "300 PRs" in hosted["next_action"]
    assert "Find or create a hosted repair PR" in hosted["next_action"]


def test_frontier_readiness_surfaces_claude_availability_diagnostics(
    tmp_path,
    monkeypatch,
):
    audit = _load_module()
    agentops = _point_at_tmp(audit, tmp_path, monkeypatch)
    _write(
        agentops / "CODING_BENCHMARK_SCORECARD.md",
        [
            "- Status: passed",
            "- Overall score: 100/100",
            "- Scenarios: 56",
            "- Pass rate: 56/56",
            "- Source stability: stable",
            "- Source changes during run: 0",
            "- Required capabilities: robust plan JSON extraction",
            "- Capability coverage: robust plan JSON extraction",
        ],
    )
    _write(
        agentops / "SOURCE_CHURN_DIAGNOSTICS.md",
        [
            "- Status: passed",
            "- Current source freshness: current",
            "- Source changes after scorecard: 0",
            "- Next action: none",
        ],
    )
    _write(agentops / "SYNTHETIC_REPO_REPAIR_BENCHMARK.md", ["- Status: passed"])
    _write(agentops / "MODEL_PROMOTION_REPLAY_BENCHMARK.md", ["- Status: passed"])
    _write(
        agentops / "FRONTIER_MODEL_EVIDENCE_INTAKE.md",
        [
            "- Status: warning",
            "- Source kinds: codex, local_model",
            "- Ready sources: 2/3",
            "- Missing/incomplete sources: claude",
            "- Shadow evidence mode: partial_real_manifest",
            "- Shadow status: failed",
            "- Tournament evidence mode: real_artifacts",
            "- Tournament status: failed",
            "",
            "## Source Readiness",
            "",
            "| Source | Path | Status | Raw drops | Missing files | Next action |",
            "| --- | --- | --- | ---: | --- | --- |",
            "| claude | claude | missing | 0 | response.json | Run python scripts/autopilot_frontier_source_runner.py --source-kind claude --source-auth-mode auto --json. Manual fallback: collect the exact Fable 5 response bundle. |",
        ],
    )
    _write(
        agentops / "FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS.md",
        [
            "- Status: warning",
            "- Claude probe status: auth_failed",
            "- Claude blocker: claude_auth_failed",
            "- Claude next action: Re-authenticate the Claude CLI or provide valid Anthropic credentials; rerun source availability diagnostics with --source-kind claude --probe-live.",
        ],
    )

    _, payload = audit.render_audit(audit.build_requirements())
    by_requirement = {row["requirement"]: row for row in payload["results"]}
    shadow = by_requirement["model_shadow_scorecard_status"]

    assert shadow["status"] == "warning"
    assert "missing/incomplete sources: claude" in shadow["next_action"]
    assert "Claude availability: auth_failed" in shadow["next_action"]
    assert "claude_auth_failed" in shadow["next_action"]
    assert "Re-authenticate the Claude CLI" in shadow["next_action"]
    assert "Intake source action for claude" in shadow["next_action"]
    assert "autopilot_frontier_source_runner.py" in shadow["next_action"]
    assert "--source-auth-mode auto" in shadow["next_action"]
    assert "Manual fallback" in shadow["next_action"]
    assert "probe-live.." not in shadow["next_action"]
