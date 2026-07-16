from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


FRONTIER_READINESS_AUDIT_SCHEMA_VERSION = "chili.frontier-readiness-audit.v1"
DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "FRONTIER_READINESS_AUDIT.md"
AGENTOPS_ROOT = REPO_ROOT / "project_ws" / "AgentOps"
CODING_SCORECARD = AGENTOPS_ROOT / "CODING_BENCHMARK_SCORECARD.md"
SOURCE_CHURN_DIAGNOSTICS = AGENTOPS_ROOT / "SOURCE_CHURN_DIAGNOSTICS.md"
SYNTHETIC_REPO_REPAIR_SCORECARD = AGENTOPS_ROOT / "SYNTHETIC_REPO_REPAIR_BENCHMARK.md"
MODEL_PROMOTION_SCORECARD = AGENTOPS_ROOT / "MODEL_PROMOTION_REPLAY_BENCHMARK.md"
MODEL_SHADOW_SCORECARD = AGENTOPS_ROOT / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md"
MODEL_TOURNAMENT_SCORECARD = AGENTOPS_ROOT / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md"
HOSTED_PR_REPAIR_SCORECARD = AGENTOPS_ROOT / "HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md"
OFFLINE_AUTONOMY_SCORECARD = AGENTOPS_ROOT / "OFFLINE_PROJECT_AUTONOMY_BENCHMARK.md"
HOSTED_PR_REPAIR_CANDIDATE_SCAN = AGENTOPS_ROOT / "HOSTED_PR_REPAIR_CANDIDATE_SCAN.md"
FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS = AGENTOPS_ROOT / "FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS.md"
FRONTIER_MODEL_EVIDENCE_INTAKE = AGENTOPS_ROOT / "FRONTIER_MODEL_EVIDENCE_INTAKE.md"
LOCAL_MODEL_CANDIDATE_RUN = AGENTOPS_ROOT / "LOCAL_MODEL_CANDIDATE_RUN.md"
SOURCE_CHURN_COMMAND = "python scripts/autopilot_source_churn_diagnostics.py --watch-seconds 30 --json"
CODING_BENCHMARK_COMMAND = (
    "python scripts/autopilot_coding_benchmark.py --require-source-quiet-seconds 30"
)
FRONTIER_EVIDENCE_COMMAND = (
    "python scripts/autopilot_frontier_model_evidence_intake.py "
    "--input-root project_ws/AgentOps/frontier_model_evidence_intake/raw_sources "
    "--publish-scorecards --json"
)
FRONTIER_COLLECTION_COMMAND = (
    "python scripts/autopilot_frontier_source_collection_packet.py --source-kind all --json"
)
HOSTED_PR_COLLECTION_COMMAND = "python scripts/autopilot_hosted_pr_repair_collection_packet.py --json"
HOSTED_PR_VALIDATION_COMMAND = (
    "python scripts/autopilot_hosted_pr_repair_artifact_benchmark.py "
    "--artifact-dir project_ws/AgentOps/hosted_pr_repair_evidence/<pr-slug>/artifact --json"
)
FRONTIER_EVIDENCE_NEXT_ACTION = (
    f"Collect/import Codex, Claude, and local_model source drops with {FRONTIER_COLLECTION_COMMAND}; "
    f"then close source intake with {FRONTIER_EVIDENCE_COMMAND}."
)
HOSTED_PR_NEXT_ACTION = (
    f"Build a hosted PR evidence packet with {HOSTED_PR_COLLECTION_COMMAND}; "
    f"collect review/publication/check receipts, assemble the artifact, then validate with {HOSTED_PR_VALIDATION_COMMAND}."
)
LOCAL_MODEL_CANDIDATE_RUN_COMMAND = (
    "python scripts/autopilot_local_model_candidate_runner.py --all-cases --json"
)
REQUIRED_BEHAVIOR = (
    "Codex 5.6 Sol / Claude Fable 5-class promotion must be backed by stable "
    "all-up coding evidence plus real model-shadow, real tournament, and real "
    "hosted PR repair artifacts, while CHILI's operational coding path remains "
    "premium-independent and locally executable."
)


@dataclass(frozen=True)
class Requirement:
    requirement: str
    status: str
    required: str
    actual: str
    evidence: str
    next_action: str = "none"


@dataclass(frozen=True)
class PytestRuntimeEvidence:
    actual: str
    python: str
    source: str
    isolation_status: str
    missing_imports: tuple[str, ...] = ()


def escape_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def metadata(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line.startswith("- ") or ":" not in line:
            continue
        key, value = line[2:].split(":", 1)
        values[key.strip().lower()] = value.strip()
    return values


def file_contains_all(path: Path, tokens: Sequence[str]) -> bool:
    if not path.is_file():
        return False
    content = path.read_text(encoding="utf-8", errors="replace")
    return all(token in content for token in tokens)


def _path_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return -1.0


def intake_scorecard_path(root_scorecard: Path) -> Path:
    return AGENTOPS_ROOT / "frontier_model_evidence_intake" / "scorecards" / root_scorecard.name


def preferred_metadata(primary_path: Path, fallback_path: Path) -> tuple[dict[str, str], Path]:
    primary = metadata(primary_path)
    fallback = metadata(fallback_path)
    if fallback and (not primary or _path_mtime(fallback_path) > _path_mtime(primary_path)):
        return fallback, fallback_path
    return primary, primary_path


def text(values: Mapping[str, str], key: str, default: str = "") -> str:
    return str(values.get(key.lower()) or default).strip()


def integer(values: Mapping[str, str], key: str, default: int = 0) -> int:
    value = text(values, key)
    if "/" in value:
        value = value.split("/", 1)[0].strip()
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def fraction(value: str) -> tuple[int, int] | None:
    if "/" not in value:
        return None
    left, right = value.split("/", 1)
    try:
        return int(float(left.strip())), int(float(right.strip()))
    except ValueError:
        return None


def rel(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def req(
    requirement: str,
    passed: bool,
    required: str,
    actual: str,
    evidence: str,
    next_action: str = "none",
    *,
    missing_is_warning: bool = True,
) -> Requirement:
    actual = actual or "missing"
    status = "passed" if passed else ("warning" if missing_is_warning else "failed")
    next_action = "none" if passed else (next_action or "none")
    return Requirement(requirement, status, required, actual, evidence, next_action)


def pytest_version() -> str:
    try:
        return importlib.metadata.version("pytest")
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def pytest_supported(version: str) -> bool:
    if version == "missing":
        return False
    parts = version.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return False
    return (major, minor) >= (8, 2) and major < 9


def selected_pytest_runtime() -> PytestRuntimeEvidence:
    try:
        from scripts import pytest_adaptive

        contract = pytest_adaptive.pytest_runtime_contract(REPO_ROOT)
        return PytestRuntimeEvidence(
            actual=contract.actual,
            python=contract.python,
            source=contract.source,
            isolation_status=contract.isolation_status,
            missing_imports=tuple(contract.missing_imports),
        )
    except Exception:
        normalized = sys.executable.replace("\\", "/")
        return PytestRuntimeEvidence(
            actual=pytest_version(),
            python=sys.executable,
            source="current_interpreter",
            isolation_status="isolated" if ".pytest_venv" in normalized else "not isolated",
        )


def import_status(module_names: Sequence[str]) -> tuple[bool, str]:
    missing: list[str] = []
    for name in module_names:
        try:
            importlib.import_module(name)
        except Exception:
            missing.append(name)
    return not missing, "none missing" if not missing else ", ".join(missing)


def benchmark_harness_missing_refs() -> list[str]:
    try:
        from scripts.autopilot_coding_benchmark import default_scenarios
    except Exception as exc:
        return [f"scripts/autopilot_coding_benchmark.py import failed: {exc}"]

    missing: set[str] = set()
    for scenario in default_scenarios(include_mobile=False):
        command = list(scenario.command)
        refs: list[str] = []
        for part in command:
            if part.startswith(("tests/", "app/", "scripts/")):
                refs.append(part.split("::", 1)[0])
            elif part.endswith((".py", ".js", ".dart")) and not part.startswith("-"):
                refs.append(part.split("::", 1)[0])
        for ref in refs:
            path = REPO_ROOT / ref.replace("/", "\\")
            if not path.exists():
                missing.add(f"{scenario.scenario_id}:{ref}")
    return sorted(missing)


def scorecard_pass_rate_ok(values: Mapping[str, str]) -> tuple[bool, str]:
    raw = text(values, "pass rate")
    parsed = fraction(raw)
    if parsed is None:
        return False, raw or "missing"
    passed, total = parsed
    return total > 0 and passed == total, raw


def capability_coverage_ok(values: Mapping[str, str]) -> tuple[bool, str]:
    required = [
        item.strip().lower()
        for item in text(values, "required capabilities").split(",")
        if item.strip()
    ]
    coverage = text(values, "capability coverage").lower()
    if not required:
        return False, "required capabilities missing"
    missing = [item for item in required if item not in coverage]
    return not missing, "none missing" if not missing else ", ".join(missing[:8])


def csv_contains(value: str, expected: str) -> bool:
    return expected in {item.strip() for item in value.split(",") if item.strip()}


def split_markdown_row(line: str) -> list[str]:
    clean = line.strip()
    if not clean.startswith("|") or not clean.endswith("|"):
        return []
    return [cell.strip().replace("\\|", "|") for cell in clean.strip("|").split("|")]


def intake_source_next_actions(intake_report: Path) -> dict[str, str]:
    if not intake_report.is_file():
        return {}
    lines = intake_report.read_text(encoding="utf-8", errors="replace").splitlines()
    in_section = False
    headers: list[str] = []
    actions: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped == "## Source Readiness":
            in_section = True
            continue
        if in_section and stripped.startswith("## "):
            break
        if not in_section:
            continue
        cells = split_markdown_row(stripped)
        if not cells:
            continue
        lowered = [cell.lower() for cell in cells]
        if lowered and lowered[0] == "source":
            headers = lowered
            continue
        if not headers or lowered[0] in {"---", "source"}:
            continue
        if len(cells) < len(headers):
            continue
        row = {headers[index]: cells[index] for index in range(len(headers))}
        source = (row.get("source") or "").strip()
        action = (row.get("next action") or "").strip()
        if source and action and action.lower() != "none":
            actions[source] = action
    return actions


def frontier_evidence_next_action(
    intake: Mapping[str, str],
    availability: Mapping[str, str] | None = None,
    *,
    intake_report: Path | None = None,
) -> str:
    missing = text(intake, "missing/incomplete sources")
    ready = text(intake, "ready sources")
    parts: list[str] = []
    if ready or missing:
        prefix = f"Current intake: ready sources {ready or 'unknown'}"
        if missing:
            prefix += f"; missing/incomplete sources: {missing}"
        parts.append(prefix + ".")
    availability = availability or {}
    if "claude" in {item.strip() for item in missing.split(",")}:
        claude_probe_status = text(availability, "claude probe status")
        claude_blocker = text(availability, "claude blocker")
        claude_next_action = text(availability, "claude next action")
        if claude_probe_status and claude_probe_status not in {"source_bundle_ready"}:
            note = f"Claude availability: {claude_probe_status}"
            if claude_blocker and claude_blocker != "none":
                note += f" ({claude_blocker})"
            if claude_next_action and claude_next_action != "none":
                note += f". {claude_next_action.rstrip('.')}"
            parts.append(note.rstrip(".") + ".")
    source_actions = intake_source_next_actions(
        intake_report or FRONTIER_MODEL_EVIDENCE_INTAKE
    )
    for source_kind in [item.strip() for item in missing.split(",") if item.strip()]:
        action = source_actions.get(source_kind)
        if action:
            parts.append(f"Intake source action for {source_kind}: {action.rstrip('.')}.")
    parts.append(FRONTIER_EVIDENCE_NEXT_ACTION)
    return " ".join(parts)


def hosted_pr_next_action(candidate_scan: Mapping[str, str]) -> str:
    status = text(candidate_scan, "status").lower()
    if not status:
        return HOSTED_PR_NEXT_ACTION
    scan_action = text(candidate_scan, "next action")
    if status == "no_review_thread_candidates":
        candidates = text(candidate_scan, "review-thread candidates", "0")
        scanned = text(candidate_scan, "prs scanned")
        scope = f"{candidates} review-thread candidates"
        if scanned:
            scope += f" across {scanned} PRs"
        return f"Hosted PR candidate scan found {scope}. {scan_action or HOSTED_PR_NEXT_ACTION}"
    return f"Hosted PR candidate scan status={status}. {scan_action or HOSTED_PR_NEXT_ACTION}"


def frontier_model_evidence_complete(
    shadow: Mapping[str, str],
    tournament: Mapping[str, str],
    intake: Mapping[str, str],
) -> bool:
    shadow_status = (text(shadow, "status") or text(intake, "shadow status")).lower()
    shadow_mode = text(shadow, "evidence mode") or text(intake, "shadow evidence mode")
    tournament_status = (text(tournament, "status") or text(intake, "tournament status")).lower()
    tournament_mode = text(tournament, "evidence mode") or text(intake, "tournament evidence mode")
    return (
        shadow_status == "passed"
        and shadow_mode == "real_manifest"
        and tournament_status == "passed"
        and tournament_mode == "real_artifacts"
    )


def tournament_source_outcomes(path: Path, source_kind: str) -> tuple[int, int, int]:
    if not path.is_file():
        return 0, 0, 0
    present = 0
    passed = 0
    rejected = 0
    source_token = source_kind.strip()
    source_marker = f"{source_token}/"
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line.startswith("| real-chili-"):
            continue
        sources_match = re.search(r"(?:^|[;|]\s*)sources=([^;|]+)", line)
        if not sources_match:
            continue
        sources = {
            item.strip()
            for item in sources_match.group(1).split(",")
            if item.strip()
        }
        if source_token not in sources:
            continue
        present += 1
        rejected_match = re.search(r"(?:^|[;|]\s*)rejected_examples=([^|]+)", line)
        passed_match = re.search(r"(?:^|[;|]\s*)passed_examples=([^|]+)", line)
        rejected_examples = rejected_match.group(1) if rejected_match else ""
        passed_examples = passed_match.group(1) if passed_match else ""
        if source_marker in rejected_examples:
            rejected += 1
        elif source_marker in passed_examples:
            passed += 1
        else:
            passed += 1
    return present, passed, rejected


def tournament_source_requirement(
    *,
    source_kind: str,
    requirement: str,
    tournament_path: Path,
    tournament_cases: int,
    tournament_status: str,
    next_action: str,
) -> Requirement:
    present, passed, rejected = tournament_source_outcomes(tournament_path, source_kind)
    if present == 0 and tournament_status.lower() == "passed":
        return req(
            requirement,
            True,
            f"{source_kind} passes all tournament cases",
            "covered by passed tournament scorecard",
            rel(tournament_path),
        )
    expected_cases = max(1, tournament_cases)
    actual = f"present={present}/{expected_cases}; passed={passed}/{expected_cases}; rejected={rejected}"
    return req(
        requirement,
        tournament_cases > 0 and present >= tournament_cases and passed >= tournament_cases,
        f"{source_kind} passes {tournament_cases or 6}/{tournament_cases or 6} tournament cases",
        actual,
        rel(tournament_path),
        next_action,
    )


def local_model_candidate_requirement(
    local_run: Mapping[str, str],
    *,
    evidence_complete: bool,
    source_drop_imported: bool = False,
) -> Requirement:
    if evidence_complete:
        return req(
            "local_model_candidate_run_status",
            True,
            "local_model source drop imported or candidate run promotion ready",
            "covered by real frontier artifacts",
            rel(LOCAL_MODEL_CANDIDATE_RUN),
        )
    if source_drop_imported:
        return req(
            "local_model_candidate_run_status",
            True,
            "local_model source drop imported or candidate run promotion ready",
            "local_model source drop imported",
            rel(FRONTIER_MODEL_EVIDENCE_INTAKE),
        )
    status = text(local_run, "status")
    promotion_ready = text(local_run, "promotion ready").lower()
    run_id = text(local_run, "run id")
    failed_case = text(local_run, "failed case")
    failure_stage = text(local_run, "failure stage")
    failure_reason = text(local_run, "failure reason")
    next_action = text(local_run, "next action")
    passed = status.lower() == "passed" and promotion_ready == "true"
    details: list[str] = []
    if status:
        details.append(f"status={status}")
    if promotion_ready:
        details.append(f"promotion_ready={promotion_ready}")
    if run_id:
        details.append(f"run_id={run_id}")
    if failure_stage:
        details.append(f"failure_stage={failure_stage}")
    if failed_case:
        details.append(f"failed_case={failed_case}")
    actual = "; ".join(details) if details else "missing"
    if failure_reason:
        actual = f"{actual}; reason={failure_reason}" if actual else f"reason={failure_reason}"
    action = (
        next_action
        or f"Run {LOCAL_MODEL_CANDIDATE_RUN_COMMAND}; if a case fails, use the recovery route in {rel(LOCAL_MODEL_CANDIDATE_RUN)}."
    )
    return req(
        "local_model_candidate_run_status",
        passed,
        "local_model source drop imported or candidate run promotion ready",
        actual,
        rel(LOCAL_MODEL_CANDIDATE_RUN),
        action,
    )


def source_freshness_actual(
    coding_values: Mapping[str, str],
    churn_values: Mapping[str, str],
) -> tuple[str, str]:
    churn_status = text(churn_values, "status")
    churn_readiness = text(churn_values, "rerun readiness")
    if churn_values:
        freshness = text(churn_values, "current source freshness", "unknown")
        changes = text(churn_values, "source changes after scorecard", "0")
        return (
            f"{freshness}; changes={changes}",
            (
                f"Latest diagnostic at {rel(SOURCE_CHURN_DIAGNOSTICS)}: "
                f"{text(churn_values, 'next action') or 'rerun source diagnostics.'} "
                f"Refresh with {SOURCE_CHURN_COMMAND} if edits resume."
            ),
        )
    generated = text(coding_values, "generated utc")
    if not generated:
        return (
            "unknown; source diagnostic missing",
            f"Run {SOURCE_CHURN_COMMAND}; then rerun the coding benchmark after source/test churn settles.",
        )
    return (
        "unknown; source diagnostic missing",
        f"Run {SOURCE_CHURN_COMMAND}; then rerun the coding benchmark if the scorecard is stale.",
    )


def build_requirements() -> list[Requirement]:
    coding = metadata(CODING_SCORECARD)
    churn = metadata(SOURCE_CHURN_DIAGNOSTICS)
    synthetic = metadata(SYNTHETIC_REPO_REPAIR_SCORECARD)
    promotion = metadata(MODEL_PROMOTION_SCORECARD)
    shadow, shadow_evidence_path = preferred_metadata(
        MODEL_SHADOW_SCORECARD,
        intake_scorecard_path(MODEL_SHADOW_SCORECARD),
    )
    tournament, tournament_evidence_path = preferred_metadata(
        MODEL_TOURNAMENT_SCORECARD,
        intake_scorecard_path(MODEL_TOURNAMENT_SCORECARD),
    )
    hosted = metadata(HOSTED_PR_REPAIR_SCORECARD)
    offline = metadata(OFFLINE_AUTONOMY_SCORECARD)
    hosted_scan = metadata(HOSTED_PR_REPAIR_CANDIDATE_SCAN)
    availability = metadata(FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS)
    intake = metadata(FRONTIER_MODEL_EVIDENCE_INTAKE)
    local_run = metadata(LOCAL_MODEL_CANDIDATE_RUN)
    requirements: list[Requirement] = []

    runtime = selected_pytest_runtime()
    version = runtime.actual
    requirements.append(
        req(
            "runner_pytest_supported_version",
            pytest_supported(version),
            "pytest>=8.2,<9",
            version,
            f"{runtime.python} ({runtime.source})",
            "none" if pytest_supported(version) else "Use a repo-local Python runtime with pytest>=8.2,<9.",
        )
    )
    isolated = runtime.isolation_status == "isolated"
    requirements.append(
        req(
            "runner_pytest_runtime_isolation",
            isolated,
            "isolated repo-local pytest runtime",
            "isolated" if isolated else "not isolated",
            f"{runtime.python} ({runtime.source})",
            "Run benchmark scenarios through the repo-local pytest runtime.",
        )
    )
    imports_ok = not runtime.missing_imports
    imports_actual = (
        "none missing" if imports_ok else ", ".join(runtime.missing_imports)
    )
    requirements.append(
        req(
            "runner_pytest_required_imports",
            imports_ok,
            "all required pytest runtime imports available",
            imports_actual,
            f"{runtime.python} ({runtime.source})",
            "Install missing benchmark runner imports.",
        )
    )
    missing_harness_refs = benchmark_harness_missing_refs()
    requirements.append(
        req(
            "coding_benchmark_harness_references",
            not missing_harness_refs,
            "all scenario command files exist",
            (
                "none missing"
                if not missing_harness_refs
                else f"{len(missing_harness_refs)} missing: "
                + ", ".join(missing_harness_refs[:8])
            ),
            "scripts/autopilot_coding_benchmark.py scenario commands",
            (
                "none"
                if not missing_harness_refs
                else "Restore missing benchmark scripts/tests before running the all-up coding proof."
            ),
        )
    )

    coding_status = text(coding, "status", "missing").lower()
    requirements.append(
        req(
            "coding_scorecard_status",
            coding_status == "passed",
            "passed",
            coding_status,
            rel(CODING_SCORECARD),
            (
                text(churn, "next action")
                or f"Run {SOURCE_CHURN_COMMAND}; then {CODING_BENCHMARK_COMMAND}."
            ),
        )
    )
    score = integer(coding, "overall score", 0)
    requirements.append(
        req("coding_score", score >= 90, ">=90", str(score), f"{rel(CODING_SCORECARD)} Overall score")
    )
    scenarios = integer(coding, "scenarios", 0)
    requirements.append(
        req("coding_scenario_count", scenarios >= 6, ">=6", str(scenarios), f"{rel(CODING_SCORECARD)} Scenarios")
    )
    pass_rate_ok, pass_rate_actual = scorecard_pass_rate_ok(coding)
    requirements.append(
        req("coding_pass_rate", pass_rate_ok, "all scenarios passed", pass_rate_actual, f"{rel(CODING_SCORECARD)} Pass rate")
    )
    stability = text(coding, "source stability", "missing").lower()
    changes_during = integer(coding, "source changes during run", 0)
    requirements.append(
        req(
            "source_stability",
            stability == "stable" and changes_during == 0,
            "stable with 0 source changes",
            f"{stability}; changes={changes_during}",
            f"{rel(CODING_SCORECARD)} Source stability",
            text(churn, "next action") or f"Run {SOURCE_CHURN_COMMAND}.",
        )
    )
    freshness_actual, freshness_action = source_freshness_actual(coding, churn)
    requirements.append(
        req(
            "coding_scorecard_current_source_freshness",
            freshness_actual.startswith("current; changes=0"),
            "no source/test files newer than Generated UTC",
            freshness_actual,
            f"{rel(CODING_SCORECARD)} Generated UTC; {rel(SOURCE_CHURN_DIAGNOSTICS)}",
            freshness_action,
        )
    )
    coverage_ok, coverage_actual = capability_coverage_ok(coding)
    requirements.append(
        req(
            "required_capability_coverage",
            coverage_ok,
            "all required capabilities covered",
            coverage_actual,
            f"{len(text(coding, 'required capabilities').split(',')) if coding else 0} required capabilities",
        )
    )

    offline_status = text(offline, "status")
    offline_score = text(offline, "average score")
    offline_action = (
        "Run `python scripts/autopilot_offline_project_autonomy_benchmark.py --json` "
        "with premium routes disabled and a local coder installed."
    )
    requirements.append(
        req(
            "offline_project_autonomy_status",
            offline_status.lower() == "passed",
            "passed",
            offline_status,
            rel(OFFLINE_AUTONOMY_SCORECARD),
            offline_action,
        )
    )
    requirements.append(
        req(
            "offline_project_autonomy_score",
            offline_score == "100/100",
            "100/100",
            offline_score,
            f"{rel(OFFLINE_AUTONOMY_SCORECARD)} Average score",
            offline_action,
        )
    )
    offline_zero_premium = file_contains_all(
        OFFLINE_AUTONOMY_SCORECARD,
        ("premium_models_required=false", "premium_calls=0"),
    )
    requirements.append(
        req(
            "offline_project_autonomy_zero_premium_calls",
            offline_zero_premium,
            "premium_models_required=false and premium_calls=0",
            (
                "premium dependency absent; premium calls=0"
                if offline_zero_premium
                else "premium-disconnected receipt missing"
            ),
            rel(OFFLINE_AUTONOMY_SCORECARD),
            offline_action,
        )
    )

    requirements.append(
        req("synthetic_repo_repair_scorecard_status", text(synthetic, "status").lower() == "passed", "passed", text(synthetic, "status"), rel(SYNTHETIC_REPO_REPAIR_SCORECARD))
    )
    requirements.append(
        req("model_promotion_scorecard_status", text(promotion, "status").lower() == "passed", "passed", text(promotion, "status"), rel(MODEL_PROMOTION_SCORECARD))
    )
    model_evidence_complete = frontier_model_evidence_complete(shadow, tournament, intake)
    frontier_next_action = frontier_evidence_next_action(intake, availability)
    requirements.append(
        local_model_candidate_requirement(
            local_run,
            evidence_complete=model_evidence_complete,
            source_drop_imported=csv_contains(text(intake, "source kinds"), "local_model"),
        )
    )
    shadow_status = text(shadow, "status") or text(intake, "shadow status")
    requirements.append(
        req(
            "model_shadow_scorecard_status",
            shadow_status.lower() == "passed",
            "passed",
            shadow_status,
            rel(shadow_evidence_path),
            frontier_next_action,
        )
    )
    shadow_checks = integer(shadow, "checks", 0)
    requirements.append(
        req(
            "model_shadow_check_count",
            shadow_checks >= 7,
            "checks>=7",
            str(shadow_checks),
            rel(shadow_evidence_path),
            frontier_next_action,
        )
    )
    shadow_mode = text(shadow, "evidence mode") or text(intake, "shadow evidence mode")
    requirements.append(
        req(
            "model_shadow_real_manifest_mode",
            shadow_mode == "real_manifest",
            "real_manifest",
            shadow_mode,
            rel(shadow_evidence_path),
            frontier_next_action,
        )
    )
    tournament_status = text(tournament, "status") or text(intake, "tournament status")
    requirements.append(
        req(
            "model_tournament_scorecard_status",
            tournament_status.lower() == "passed",
            "passed",
            tournament_status,
            rel(tournament_evidence_path),
            frontier_next_action,
        )
    )
    tournament_cases = integer(tournament, "cases", 0)
    requirements.append(
        req(
            "model_tournament_case_count",
            tournament_cases >= 6,
            "cases>=6",
            str(tournament_cases),
            rel(tournament_evidence_path),
            frontier_next_action,
        )
    )
    tournament_mode = text(tournament, "evidence mode") or text(intake, "tournament evidence mode")
    requirements.append(
        req(
            "model_tournament_real_artifacts_mode",
            tournament_mode == "real_artifacts",
            "real_artifacts",
            tournament_mode,
            rel(tournament_evidence_path),
            frontier_next_action,
        )
    )
    local_present, local_passed, _ = tournament_source_outcomes(
        tournament_evidence_path,
        "local_model",
    )
    local_model_tournament_passed = (
        local_present == 0
        and tournament_status.lower() == "passed"
    ) or (
        tournament_cases > 0
        and local_present >= tournament_cases
        and local_passed >= tournament_cases
    )
    local_model_tournament_next_action = (
        "none"
        if local_model_tournament_passed
        else (
            "Run or import a stronger all-cases local-model response that applies cleanly "
            f"with {LOCAL_MODEL_CANDIDATE_RUN_COMMAND}; then rerun {FRONTIER_EVIDENCE_COMMAND}."
        )
    )
    requirements.append(
        tournament_source_requirement(
            source_kind="codex",
            requirement="codex_tournament_case_pass_count",
            tournament_path=tournament_evidence_path,
            tournament_cases=tournament_cases,
            tournament_status=tournament_status,
            next_action=frontier_next_action,
        )
    )
    requirements.append(
        tournament_source_requirement(
            source_kind="local_model",
            requirement="local_model_tournament_case_pass_count",
            tournament_path=tournament_evidence_path,
            tournament_cases=tournament_cases,
            tournament_status=tournament_status,
            next_action=local_model_tournament_next_action,
        )
    )
    hosted_status = text(hosted, "status")
    hosted_next_action = hosted_pr_next_action(hosted_scan)
    requirements.append(
        req(
            "hosted_pr_repair_scorecard_status",
            hosted_status.lower() == "passed",
            "passed",
            hosted_status,
            rel(HOSTED_PR_REPAIR_SCORECARD),
            hosted_next_action,
        )
    )
    hosted_checks = integer(hosted, "checks", 0)
    requirements.append(
        req(
            "hosted_pr_repair_check_count",
            hosted_checks >= 18,
            "checks>=18",
            str(hosted_checks),
            rel(HOSTED_PR_REPAIR_SCORECARD),
            hosted_next_action,
        )
    )
    hosted_mode = text(hosted, "evidence mode") or text(hosted, "inventory mode")
    requirements.append(
        req(
            "hosted_pr_repair_real_inventory_mode",
            hosted_mode == "real_inventory",
            "real_inventory",
            hosted_mode,
            rel(HOSTED_PR_REPAIR_SCORECARD),
            hosted_next_action,
        )
    )
    hosted_eligible = text(hosted, "promotion eligible")
    requirements.append(
        req(
            "hosted_pr_repair_promotion_eligible",
            hosted_eligible.lower() == "true",
            "true",
            hosted_eligible,
            f"{rel(HOSTED_PR_REPAIR_SCORECARD)} promotion eligible",
            hosted_next_action,
        )
    )
    return requirements


def render_audit(requirements: Sequence[Requirement]) -> tuple[str, dict[str, object]]:
    passed = sum(1 for requirement in requirements if requirement.status == "passed")
    blockers = [requirement for requirement in requirements if requirement.status != "passed"]
    score = round((passed / len(requirements)) * 100) if requirements else 0
    status = "passed" if not blockers else "warning"
    lines = [
        "# CHILI Frontier Readiness Audit",
        "",
        f"- Schema: {FRONTIER_READINESS_AUDIT_SCHEMA_VERSION}",
        f"- Status: {status}",
        f"- Readiness score: {score}/100",
        f"- Requirements: {len(requirements)}",
        f"- Blockers: {len(blockers)}",
        f"- Required behavior: {REQUIRED_BEHAVIOR}",
        "",
        "| Requirement | Status | Required | Actual | Evidence | Next action |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for requirement in requirements:
        lines.append(
            "| "
            + " | ".join(
                [
                    escape_cell(requirement.requirement),
                    escape_cell(requirement.status),
                    escape_cell(requirement.required),
                    escape_cell(requirement.actual),
                    escape_cell(requirement.evidence),
                    escape_cell(requirement.next_action or "none"),
                ]
            )
            + " |"
        )
    markdown = "\n".join(lines) + "\n"
    payload = {
        "schema": FRONTIER_READINESS_AUDIT_SCHEMA_VERSION,
        "status": status,
        "readiness_score": score,
        "requirements": len(requirements),
        "blockers": len(blockers),
        "results": [
            {
                "requirement": requirement.requirement,
                "status": requirement.status,
                "required": requirement.required,
                "actual": requirement.actual,
                "evidence": requirement.evidence,
                "next_action": requirement.next_action,
            }
            for requirement in requirements
        ],
    }
    return markdown, payload


def run_audit(*, output_path: Path = DEFAULT_OUTPUT, write: bool = True) -> tuple[str, dict[str, object], Path]:
    markdown, payload = render_audit(build_requirements())
    if write:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
    return markdown, {**payload, "path": output_path.as_posix(), "written": bool(write)}, output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit CHILI frontier coding readiness evidence.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    markdown, payload, _ = run_audit(output_path=args.output, write=not args.no_write)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
