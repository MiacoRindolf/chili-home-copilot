from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.project_autonomy import orchestrator  # noqa: E402
from scripts.autopilot_frontier_bakeoff_benchmark import _escape_cell  # noqa: E402
from scripts import pytest_adaptive  # noqa: E402


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "FRONTIER_READINESS_AUDIT.md"
FRONTIER_READINESS_AUDIT_SCHEMA_VERSION = "chili.frontier-readiness-audit.v1"
STATUS_PASSED = "passed"
STATUS_WARNING = "warning"


@dataclasses.dataclass(frozen=True)
class Requirement:
    requirement_id: str
    status: str
    required: str
    actual: str
    evidence: str
    next_action: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _metadata(root: Path, rel_path: str) -> dict[str, str]:
    return orchestrator._scorecard_metadata(root / Path(rel_path))


def _text(metadata: Mapping[str, str], key: str, default: str = "") -> str:
    return orchestrator._scorecard_text(metadata, key, default)


def _int(metadata: Mapping[str, str], key: str, default: int = 0) -> int:
    return orchestrator._scorecard_int(metadata, key, default)


def _status_from(ok: bool) -> str:
    return STATUS_PASSED if ok else STATUS_WARNING


def _requirement(
    requirement_id: str,
    *,
    ok: bool,
    required: str,
    actual: str,
    evidence: str,
    next_action: str,
) -> Requirement:
    return Requirement(
        requirement_id=requirement_id,
        status=_status_from(ok),
        required=required,
        actual=actual,
        evidence=evidence,
        next_action=next_action if not ok else "none",
    )


def _runner_environment_requirements(root: Path) -> list[Requirement]:
    contract = pytest_adaptive.pytest_runtime_contract(root)
    return [
        _requirement(
            "runner_pytest_supported_version",
            ok=contract.passed,
            required=contract.required,
            actual=f"{contract.actual} via {contract.source}",
            evidence=f"{contract.source}: {contract.python}",
            next_action=contract.recovery,
        ),
        _requirement(
            "runner_pytest_runtime_isolation",
            ok=contract.isolation_status == "isolated",
            required="isolated repo-local pytest runtime",
            actual=contract.isolation_status,
            evidence=f"{contract.source}: {contract.python}",
            next_action=contract.isolation_recovery,
        ),
    ]


def _coding_requirements(root: Path) -> list[Requirement]:
    rel_path = orchestrator.AGENT_CODING_BENCHMARK_SCORECARD_REL_PATH
    metadata = _metadata(root, rel_path)
    if not metadata:
        return [
            _requirement(
                "coding_scorecard_present",
                ok=False,
                required=rel_path,
                actual="missing",
                evidence="primary coding benchmark scorecard is missing",
                next_action="Run scripts/autopilot_coding_benchmark.py after the current source tree settles.",
            )
        ]

    status = _text(metadata, "status").lower()
    score = _int(metadata, "overall score")
    scenarios = _int(metadata, "scenarios")
    pass_rate = _text(metadata, "pass rate", "0/0")
    passed = 0
    if "/" in pass_rate:
        try:
            passed = int(pass_rate.split("/", 1)[0].strip())
        except ValueError:
            passed = 0
    source_stability = _text(metadata, "source stability", "unknown")
    source_changes = _int(metadata, "source changes during run")
    source_freshness = orchestrator._scorecard_source_freshness(root, metadata)
    source_freshness_status = str(source_freshness.get("status") or "missing")
    source_changes_after = int(source_freshness.get("source_changes_after_scorecard") or 0)
    source_preview_after = str(
        source_freshness.get("source_change_preview_after_scorecard") or "none"
    )
    missing_capabilities = orchestrator._scorecard_missing_capabilities(metadata)
    return [
        _requirement(
            "coding_scorecard_status",
            ok=status == STATUS_PASSED,
            required=STATUS_PASSED,
            actual=status or "missing",
            evidence=rel_path,
            next_action="Rerun the full coding benchmark and inspect failed scenario rows.",
        ),
        _requirement(
            "coding_score",
            ok=score >= orchestrator.AGENT_CODING_BENCHMARK_TARGET_SCORE,
            required=f">={orchestrator.AGENT_CODING_BENCHMARK_TARGET_SCORE}",
            actual=str(score),
            evidence=f"{rel_path} Overall score",
            next_action="Repair failing benchmark dimensions before frontier promotion.",
        ),
        _requirement(
            "coding_scenario_count",
            ok=scenarios >= orchestrator.AGENT_CODING_BENCHMARK_MIN_SCENARIOS,
            required=f">={orchestrator.AGENT_CODING_BENCHMARK_MIN_SCENARIOS}",
            actual=str(scenarios),
            evidence=f"{rel_path} Scenarios",
            next_action="Run a full benchmark profile, not a partial smoke run.",
        ),
        _requirement(
            "coding_pass_rate",
            ok=passed == scenarios and scenarios > 0,
            required="all scenarios passed",
            actual=pass_rate,
            evidence=f"{rel_path} Pass rate",
            next_action="Open the failed scenario row and repair the underlying behavior.",
        ),
        _requirement(
            "source_stability",
            ok=source_stability == "stable" and source_changes == 0,
            required="stable with 0 source changes",
            actual=f"{source_stability}; changes={source_changes}",
            evidence=f"{rel_path} Source stability",
            next_action="Rerun the benchmark after source/test churn stops.",
        ),
        _requirement(
            "coding_scorecard_current_source_freshness",
            ok=source_freshness_status == "current",
            required="no source/test files newer than Generated UTC",
            actual=f"{source_freshness_status}; changes={source_changes_after}",
            evidence=(
                f"{rel_path} Generated UTC"
                if source_preview_after == "none"
                else f"{rel_path} Generated UTC; {source_preview_after}"
            ),
            next_action="Rerun the coding benchmark after current source changes settle.",
        ),
        _requirement(
            "required_capability_coverage",
            ok=not missing_capabilities,
            required="all required capabilities covered",
            actual=(
                "none missing"
                if not missing_capabilities
                else ", ".join(missing_capabilities[:8])
            ),
            evidence=f"{len(orchestrator.AGENT_CODING_BENCHMARK_REQUIRED_CAPABILITIES)} required capabilities",
            next_action="Add or rerun benchmark scenarios for missing capability coverage.",
        ),
    ]


def _dependent_status_requirement(
    *,
    root: Path,
    rel_path: str,
    requirement_id: str,
    required_status: str = STATUS_PASSED,
    next_action: str,
) -> Requirement:
    metadata = _metadata(root, rel_path)
    status = _text(metadata, "status").lower() if metadata else "missing"
    return _requirement(
        requirement_id,
        ok=status == required_status,
        required=required_status,
        actual=status,
        evidence=rel_path,
        next_action=next_action,
    )


def _evidence_mode_requirement(
    *,
    root: Path,
    rel_path: str,
    requirement_id: str,
    required_mode: str,
    next_action: str,
) -> Requirement:
    metadata = _metadata(root, rel_path)
    mode = _text(metadata, "evidence mode") if metadata else "missing"
    return _requirement(
        requirement_id,
        ok=mode == required_mode,
        required=required_mode,
        actual=mode or "missing",
        evidence=rel_path,
        next_action=next_action,
    )


def _metadata_value_requirement(
    *,
    root: Path,
    rel_path: str,
    requirement_id: str,
    key: str,
    required_value: str,
    next_action: str,
) -> Requirement:
    metadata = _metadata(root, rel_path)
    value = _text(metadata, key) if metadata else "missing"
    return _requirement(
        requirement_id,
        ok=value == required_value,
        required=required_value,
        actual=value or "missing",
        evidence=f"{rel_path} {key}",
        next_action=next_action,
    )


def _minimum_count_requirement(
    *,
    root: Path,
    rel_path: str,
    requirement_id: str,
    count_key: str,
    required_count: int,
    next_action: str,
) -> Requirement:
    metadata = _metadata(root, rel_path)
    count = _int(metadata, count_key) if metadata else 0
    return _requirement(
        requirement_id,
        ok=count >= required_count,
        required=f"{count_key}>={required_count}",
        actual=str(count),
        evidence=rel_path,
        next_action=next_action,
    )


def collect_requirements(root: Path) -> list[Requirement]:
    requirements: list[Requirement] = []
    requirements.extend(_runner_environment_requirements(root))
    requirements.extend(_coding_requirements(root))
    requirements.extend(
        [
            _dependent_status_requirement(
                root=root,
                rel_path=orchestrator.AGENT_SYNTHETIC_REPO_REPAIR_SCORECARD_REL_PATH,
                requirement_id="synthetic_repo_repair_scorecard_status",
                next_action="Regenerate the synthetic repo repair benchmark scorecard.",
            ),
            _dependent_status_requirement(
                root=root,
                rel_path=orchestrator.AGENT_MODEL_PROMOTION_SCORECARD_REL_PATH,
                requirement_id="model_promotion_scorecard_status",
                next_action="Regenerate the model promotion replay scorecard.",
            ),
            _dependent_status_requirement(
                root=root,
                rel_path=orchestrator.AGENT_MODEL_SHADOW_EVIDENCE_SCORECARD_REL_PATH,
                requirement_id="model_shadow_scorecard_status",
                next_action="Regenerate model shadow evidence after collecting real manifests.",
            ),
            _minimum_count_requirement(
                root=root,
                rel_path=orchestrator.AGENT_MODEL_SHADOW_EVIDENCE_SCORECARD_REL_PATH,
                requirement_id="model_shadow_check_count",
                count_key="checks",
                required_count=orchestrator.AGENT_MODEL_SHADOW_EVIDENCE_MIN_CHECKS,
                next_action="Run the full model shadow evidence validator.",
            ),
            _evidence_mode_requirement(
                root=root,
                rel_path=orchestrator.AGENT_MODEL_SHADOW_EVIDENCE_SCORECARD_REL_PATH,
                requirement_id="model_shadow_real_manifest_mode",
                required_mode=orchestrator.AGENT_MODEL_SHADOW_REQUIRED_EVIDENCE_MODE,
                next_action="Collect real Codex, Claude, and local-model manifests with verified transcripts.",
            ),
            _dependent_status_requirement(
                root=root,
                rel_path=orchestrator.AGENT_MODEL_CANDIDATE_TOURNAMENT_SCORECARD_REL_PATH,
                requirement_id="model_tournament_scorecard_status",
                next_action="Regenerate the tournament scorecard from real model artifacts.",
            ),
            _minimum_count_requirement(
                root=root,
                rel_path=orchestrator.AGENT_MODEL_CANDIDATE_TOURNAMENT_SCORECARD_REL_PATH,
                requirement_id="model_tournament_case_count",
                count_key="cases",
                required_count=orchestrator.AGENT_MODEL_CANDIDATE_TOURNAMENT_MIN_CASES,
                next_action="Collect candidate drops for all required comparison classes.",
            ),
            _evidence_mode_requirement(
                root=root,
                rel_path=orchestrator.AGENT_MODEL_CANDIDATE_TOURNAMENT_SCORECARD_REL_PATH,
                requirement_id="model_tournament_real_artifacts_mode",
                required_mode=orchestrator.AGENT_MODEL_CANDIDATE_TOURNAMENT_REQUIRED_EVIDENCE_MODE,
                next_action="Build the tournament from transcript-verified Codex, Claude, and local-model drops.",
            ),
            _dependent_status_requirement(
                root=root,
                rel_path=orchestrator.AGENT_HOSTED_PR_REPAIR_SCORECARD_REL_PATH,
                requirement_id="hosted_pr_repair_scorecard_status",
                next_action="Regenerate hosted PR repair evidence from real artifact inventory.",
            ),
            _minimum_count_requirement(
                root=root,
                rel_path=orchestrator.AGENT_HOSTED_PR_REPAIR_SCORECARD_REL_PATH,
                requirement_id="hosted_pr_repair_check_count",
                count_key="checks",
                required_count=orchestrator.AGENT_HOSTED_PR_REPAIR_MIN_CHECKS,
                next_action="Run the refreshed 18-check hosted PR repair validator.",
            ),
            _evidence_mode_requirement(
                root=root,
                rel_path=orchestrator.AGENT_HOSTED_PR_REPAIR_SCORECARD_REL_PATH,
                requirement_id="hosted_pr_repair_real_inventory_mode",
                required_mode=orchestrator.AGENT_HOSTED_PR_REPAIR_REQUIRED_EVIDENCE_MODE,
                next_action="Collect real hosted PR repair artifacts with transcript-bound PR evidence.",
            ),
            _metadata_value_requirement(
                root=root,
                rel_path=orchestrator.AGENT_HOSTED_PR_REPAIR_SCORECARD_REL_PATH,
                requirement_id="hosted_pr_repair_promotion_eligible",
                key="promotion eligible",
                required_value="true",
                next_action="Regenerate hosted PR repair evidence from real transcript-bound inventory; self-test validator passes are not promotion evidence.",
            ),
        ]
    )
    return requirements


def audit_frontier_readiness(root: Path) -> dict[str, object]:
    requirements = collect_requirements(root)
    blockers = [requirement for requirement in requirements if requirement.status != STATUS_PASSED]
    status = STATUS_PASSED if not blockers else STATUS_WARNING
    return {
        "schema": FRONTIER_READINESS_AUDIT_SCHEMA_VERSION,
        "generated_utc": _utc_now(),
        "status": status,
        "readiness_score": round((len(requirements) - len(blockers)) / len(requirements) * 100) if requirements else 0,
        "requirements": len(requirements),
        "blockers": len(blockers),
        "blocker_ids": [blocker.requirement_id for blocker in blockers],
        "next_actions": [blocker.next_action for blocker in blockers[:8]],
        "requirement_results": [dataclasses.asdict(requirement) for requirement in requirements],
        "signal": orchestrator._agent_coding_benchmark_signal(root),
    }


def render_audit(audit: Mapping[str, object]) -> str:
    rows = audit.get("requirement_results")
    requirements = rows if isinstance(rows, list) else []
    lines = [
        "# CHILI Frontier Readiness Audit",
        "",
        f"- Schema: {audit.get('schema')}",
        f"- Generated UTC: {audit.get('generated_utc')}",
        f"- Status: {audit.get('status')}",
        f"- Readiness score: {audit.get('readiness_score')}/100",
        f"- Requirements: {audit.get('requirements')}",
        f"- Blockers: {audit.get('blockers')}",
        "- Required behavior: Codex/Claude-class promotion must be backed by stable all-up coding evidence plus real model-shadow, real tournament, and real hosted PR repair artifacts.",
        "",
        "| Requirement | Status | Required | Actual | Evidence | Next action |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for raw in requirements:
        if not isinstance(raw, Mapping):
            continue
        lines.append(
            "| "
            + " | ".join(
                _escape_cell(str(raw.get(key) or ""))
                for key in ("requirement_id", "status", "required", "actual", "evidence", "next_action")
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def write_audit(markdown: str, output_path: Path = DEFAULT_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit whether CHILI has real frontier coding-readiness evidence.")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--fail-on-warning", action="store_true")
    args = parser.parse_args(argv)

    audit = audit_frontier_readiness(args.root)
    markdown = render_audit(audit)
    if not args.no_write:
        write_audit(markdown, args.output)
    if args.json:
        print(json.dumps(audit, indent=2, sort_keys=True))
    else:
        print(markdown)
        if not args.no_write:
            print(f"Wrote {args.output}")
    if args.fail_on_warning and audit.get("status") != STATUS_PASSED:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
