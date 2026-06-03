from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "autopilot_frontier_readiness_audit.py"


def _load_audit_module():
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


def _set_pytest_version(audit, monkeypatch, version: str) -> None:
    monkeypatch.setattr(
        audit.pytest_adaptive,
        "pytest_version_for_python",
        lambda _path: version,
    )
    monkeypatch.setattr(
        audit.pytest_adaptive,
        "pytest_runtime_isolation",
        lambda _path, *, source: ("isolated", False),
    )


def _generated_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_lines(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_primary_scorecard(audit, root: Path, *, capabilities: list[str] | None = None) -> Path:
    required = list(audit.orchestrator.AGENT_CODING_BENCHMARK_REQUIRED_CAPABILITIES)
    coverage = capabilities if capabilities is not None else required
    return _write_lines(
        root / audit.orchestrator.AGENT_CODING_BENCHMARK_SCORECARD_REL_PATH,
        [
            "# CHILI Coding Benchmark Scorecard",
            "",
            "- Schema: chili.coding-benchmark.v1",
            "- Profile: core",
            f"- Generated UTC: {_generated_utc()}",
            "- Status: passed",
            "- Target score: 90",
            "- Minimum scenarios: 6",
            "- Overall score: 100/100",
            f"- Scenarios: {len(required)}",
            f"- Pass rate: {len(required)}/{len(required)}",
            "- Source stability: stable",
            "- Source files scanned: 12",
            "- Source changes during run: 0",
            "- Source change preview: none",
            "- Required capabilities: " + ", ".join(required),
            "- Capability coverage: " + ", ".join(coverage),
        ],
    )


def _write_dependency_scorecards(audit, root: Path, *, real: bool) -> None:
    _write_lines(
        root / audit.orchestrator.AGENT_SYNTHETIC_REPO_REPAIR_SCORECARD_REL_PATH,
        [
            "# CHILI Synthetic Repo Repair Benchmark",
            f"- Generated UTC: {_generated_utc()}",
            "- Status: passed",
            "- Cases: 6",
            "- Average score: 100/100",
        ],
    )
    _write_lines(
        root / audit.orchestrator.AGENT_MODEL_PROMOTION_SCORECARD_REL_PATH,
        [
            "# CHILI Model Promotion Replay Benchmark",
            f"- Generated UTC: {_generated_utc()}",
            "- Status: passed",
            "- Cases: 7",
            "- Average score: 100/100",
        ],
    )
    _write_lines(
        root / audit.orchestrator.AGENT_MODEL_SHADOW_EVIDENCE_SCORECARD_REL_PATH,
        [
            "# CHILI Model Shadow Evidence Benchmark",
            f"- Generated UTC: {_generated_utc()}",
            "- Status: passed",
            f"- Evidence mode: {'real_manifest' if real else 'self_test'}",
            "- Checks: 7",
            "- Average score: 100/100",
            "- Missing checks: none",
        ],
    )
    _write_lines(
        root / audit.orchestrator.AGENT_MODEL_CANDIDATE_TOURNAMENT_SCORECARD_REL_PATH,
        [
            "# CHILI Model Candidate Tournament Benchmark",
            f"- Generated UTC: {_generated_utc()}",
            "- Status: passed",
            f"- Evidence mode: {'real_artifacts' if real else 'self_test'}",
            "- Cases: 6",
            "- Average score: 100/100",
            "- Missing source kinds: none",
            "- Missing comparison classes: none",
        ],
    )
    _write_lines(
        root / audit.orchestrator.AGENT_HOSTED_PR_REPAIR_SCORECARD_REL_PATH,
        [
            "# CHILI Hosted PR Repair Artifact Benchmark",
            f"- Generated UTC: {_generated_utc()}",
            "- Status: passed",
            f"- Evidence mode: {'real_inventory' if real else 'self_test'}",
            f"- Checks: {18 if real else 14}",
            "- Average score: 100/100",
            "- Missing checks: none",
            f"- Promotion eligible: {'true' if real else 'false'}",
        ],
    )


def test_frontier_readiness_audit_passes_with_real_evidence_fixture(tmp_path, monkeypatch):
    audit = _load_audit_module()
    _set_pytest_version(audit, monkeypatch, "8.4.2")
    _write_primary_scorecard(audit, tmp_path)
    _write_dependency_scorecards(audit, tmp_path, real=True)

    result = audit.audit_frontier_readiness(tmp_path)
    markdown = audit.render_audit(result)

    assert result["status"] == "passed"
    assert result["blockers"] == 0
    assert result["readiness_score"] == 100
    assert "| model_tournament_real_artifacts_mode | passed | real_artifacts | real_artifacts |" in markdown
    assert "| hosted_pr_repair_real_inventory_mode | passed | real_inventory | real_inventory |" in markdown
    assert "| hosted_pr_repair_promotion_eligible | passed | true | true |" in markdown


def test_frontier_readiness_audit_flags_self_test_evidence_and_missing_capability(tmp_path, monkeypatch):
    audit = _load_audit_module()
    _set_pytest_version(audit, monkeypatch, "8.4.2")
    required = list(audit.orchestrator.AGENT_CODING_BENCHMARK_REQUIRED_CAPABILITIES)
    _write_primary_scorecard(
        audit,
        tmp_path,
        capabilities=[
            capability
            for capability in required
            if capability != "real model tournament evidence mode gate"
        ],
    )
    _write_dependency_scorecards(audit, tmp_path, real=False)

    result = audit.audit_frontier_readiness(tmp_path)
    blocker_ids = set(result["blocker_ids"])

    assert result["status"] == "warning"
    assert "required_capability_coverage" in blocker_ids
    assert "model_shadow_real_manifest_mode" in blocker_ids
    assert "model_tournament_real_artifacts_mode" in blocker_ids
    assert "hosted_pr_repair_check_count" in blocker_ids
    assert "hosted_pr_repair_real_inventory_mode" in blocker_ids
    assert "hosted_pr_repair_promotion_eligible" in blocker_ids


def test_frontier_readiness_audit_flags_scorecard_stale_after_source_change(tmp_path, monkeypatch):
    audit = _load_audit_module()
    _set_pytest_version(audit, monkeypatch, "8.4.2")
    _write_primary_scorecard(audit, tmp_path)
    _write_dependency_scorecards(audit, tmp_path, real=True)
    changed_file = tmp_path / "app" / "services" / "current_change.py"
    changed_file.parent.mkdir(parents=True)
    changed_file.write_text("VALUE = 1\n", encoding="utf-8")
    os.utime(changed_file, (4102444800, 4102444800))

    result = audit.audit_frontier_readiness(tmp_path)
    markdown = audit.render_audit(result)

    assert result["status"] == "warning"
    assert "coding_scorecard_current_source_freshness" in result["blocker_ids"]
    assert result["signal"]["scorecard_freshness"] == "stale"
    assert result["signal"]["source_changes_after_scorecard"] == 1
    assert "app/services/current_change.py" in markdown


def test_frontier_readiness_audit_flags_unsupported_pytest_runner(tmp_path, monkeypatch):
    audit = _load_audit_module()
    _set_pytest_version(audit, monkeypatch, "9.0.2")
    _write_primary_scorecard(audit, tmp_path)
    _write_dependency_scorecards(audit, tmp_path, real=True)

    result = audit.audit_frontier_readiness(tmp_path)

    assert result["status"] == "warning"
    assert "runner_pytest_supported_version" in result["blocker_ids"]


def test_frontier_readiness_audit_flags_non_isolated_pytest_runner(
    tmp_path,
    monkeypatch,
):
    audit = _load_audit_module()
    _set_pytest_version(audit, monkeypatch, "8.4.2")
    monkeypatch.setattr(
        audit.pytest_adaptive,
        "pytest_runtime_isolation",
        lambda _path, *, source: ("shared_site_packages", True),
    )
    _write_primary_scorecard(audit, tmp_path)
    _write_dependency_scorecards(audit, tmp_path, real=True)

    result = audit.audit_frontier_readiness(tmp_path)

    assert result["status"] == "warning"
    assert "runner_pytest_runtime_isolation" in result["blocker_ids"]


def test_frontier_readiness_audit_cli_json_and_fail_on_warning(tmp_path, capsys, monkeypatch):
    audit = _load_audit_module()
    _set_pytest_version(audit, monkeypatch, "8.4.2")
    _write_primary_scorecard(audit, tmp_path)
    _write_dependency_scorecards(audit, tmp_path, real=False)
    output = tmp_path / "audit.md"

    exit_code = audit.main(["--root", str(tmp_path), "--output", str(output), "--json", "--no-write"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert '"schema": "chili.frontier-readiness-audit.v1"' in captured.out
    assert '"status": "warning"' in captured.out
    assert not output.exists()
    assert audit.main(["--root", str(tmp_path), "--no-write", "--fail-on-warning"]) == 1
