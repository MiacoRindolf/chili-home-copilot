from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import autopilot_frontier_readiness_audit as frontier_readiness  # noqa: E402


FRONTIER_GAP_MATRIX_SCHEMA_VERSION = "chili.frontier-gap-matrix.v1"
AGENTOPS_ROOT = REPO_ROOT / "project_ws" / "AgentOps"
DEFAULT_OUTPUT = AGENTOPS_ROOT / "FRONTIER_GAP_MATRIX.md"
CODING_SCORECARD = AGENTOPS_ROOT / "CODING_BENCHMARK_SCORECARD.md"
SOURCE_CHURN_DIAGNOSTICS = AGENTOPS_ROOT / "SOURCE_CHURN_DIAGNOSTICS.md"
FRONTIER_MODEL_EVIDENCE_INTAKE = AGENTOPS_ROOT / "FRONTIER_MODEL_EVIDENCE_INTAKE.md"
FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS = (
    AGENTOPS_ROOT / "FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS.md"
)
MODEL_SHADOW_SCORECARD = AGENTOPS_ROOT / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md"
MODEL_TOURNAMENT_SCORECARD = AGENTOPS_ROOT / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md"
HOSTED_PR_REPAIR_SCORECARD = AGENTOPS_ROOT / "HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md"
OFFLINE_AUTONOMY_SCORECARD = AGENTOPS_ROOT / "OFFLINE_PROJECT_AUTONOMY_BENCHMARK.md"
MESO_WORKFLOW_TOURNAMENT_SCORECARD = (
    AGENTOPS_ROOT / "MESO_PROJECT_WORKFLOW_TOURNAMENT_BENCHMARK.md"
)
MACRO_LONG_HORIZON_TOURNAMENT_SCORECARD = (
    AGENTOPS_ROOT / "MACRO_LONG_HORIZON_TOURNAMENT_BENCHMARK.md"
)
CONTEXT_REASONING_TOURNAMENT_SCORECARD = (
    AGENTOPS_ROOT / "DEEP_CONTEXT_REASONING_TOURNAMENT_BENCHMARK.md"
)

ReadinessRunner = Callable[..., tuple[str, dict[str, object], Path]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _escape_cell(value: object) -> str:
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


def _split_markdown_row(line: str) -> list[str]:
    clean = line.strip()
    if not clean.startswith("|") or not clean.endswith("|"):
        return []
    return [cell.strip().replace("\\|", "|") for cell in clean.strip("|").split("|")]


def tournament_winner_counts(path: Path = MODEL_TOURNAMENT_SCORECARD) -> dict[str, int]:
    if not path.is_file():
        return {}
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        cells = _split_markdown_row(line)
        if len(cells) < 3:
            continue
        if cells[0].lower() == "case" or cells[2].startswith("---"):
            continue
        winner = cells[2].strip()
        if not winner:
            continue
        source_kind = winner.split("/", 1)[0] if "/" in winner else winner
        counts[source_kind] = counts.get(source_kind, 0) + 1
    return counts


def _counts_summary(counts: Mapping[str, int], preferred: Sequence[str]) -> str:
    ordered = [
        f"{source}={counts.get(source, 0)}"
        for source in preferred
        if counts.get(source, 0) or source in preferred
    ]
    extras = sorted(source for source in counts if source not in preferred)
    ordered.extend(f"{source}={counts[source]}" for source in extras)
    return ", ".join(ordered)


def _winner_counts_summary(counts: Mapping[str, int]) -> str:
    return _counts_summary(counts, ("local_model", "codex", "claude", "none"))


def _runtime_counts_summary(counts: Mapping[str, int]) -> str:
    return _counts_summary(counts, ("measured", "unmeasured"))


def _source_kinds_set(value: str) -> set[str]:
    return {part.strip() for part in value.split(",") if part.strip()}


def _parse_keyed_counts(value: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for part in value.split(","):
        if "=" not in part:
            continue
        key, raw_value = part.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            continue
        try:
            counts[key] = int(raw_value)
        except ValueError:
            continue
    return counts


def _int_metadata(values: Mapping[str, object], key: str) -> int:
    raw = text(values, key)
    try:
        return int(raw.split("/", 1)[0].strip())
    except (TypeError, ValueError):
        return 0


def _system_tournament_proven(path: Path, *, min_tasks: int = 3) -> tuple[bool, str]:
    values = metadata(path)
    tasks = _int_metadata(values, "tasks")
    winners = _parse_keyed_counts(text(values, "winner counts"))
    runtime = _parse_keyed_counts(text(values, "runtime measurements"))
    proven = (
        text(values, "status") == "passed"
        and text(values, "evidence mode") == "real_artifacts"
        and tasks >= min_tasks
        and winners.get("local_model", 0) == tasks
        and winners.get("codex", 0) == 0
        and winners.get("claude", 0) == 0
        and winners.get("none", 0) == 0
        and runtime.get("unmeasured", 0) == 0
    )
    actual = (
        f"status={text(values, 'status', 'missing')}; "
        f"mode={text(values, 'evidence mode', 'missing')}; tasks={tasks}; "
        f"winner_counts={_winner_counts_summary(winners)}; "
        f"runtime_measurements={text(values, 'runtime measurements', 'missing')}"
    )
    return proven, actual


def text(values: Mapping[str, object], key: str, default: str = "") -> str:
    return str(values.get(key) or values.get(key.lower()) or default).strip()


def _readiness_results(payload: Mapping[str, object]) -> list[dict[str, str]]:
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []
    rows: list[dict[str, str]] = []
    for raw_row in raw_results:
        if not isinstance(raw_row, Mapping):
            continue
        rows.append({str(key): str(value) for key, value in raw_row.items()})
    return rows


def _by_requirement(results: Sequence[Mapping[str, str]]) -> dict[str, Mapping[str, str]]:
    return {str(row.get("requirement") or ""): row for row in results}


def _requirements_passed(
    by_requirement: Mapping[str, Mapping[str, str]],
    requirement_names: Sequence[str],
) -> bool:
    return all(
        text(by_requirement.get(requirement, {}), "status") == "passed"
        for requirement in requirement_names
    )


def _domain_row(
    *,
    domain: str,
    proof_status: str,
    actual: str,
    evidence: str,
    next_action: str,
) -> dict[str, str]:
    return {
        "domain": domain,
        "proof_status": proof_status,
        "actual": actual or "missing",
        "evidence": evidence or "missing",
        "next_action": next_action or "none",
    }


def _gap_rows(results: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    gaps: list[dict[str, str]] = []
    for row in results:
        if text(row, "status") == "passed":
            continue
        gaps.append(
            {
                "gap_id": text(row, "requirement"),
                "status": text(row, "status"),
                "required": text(row, "required"),
                "actual": text(row, "actual"),
                "evidence": text(row, "evidence"),
                "next_action": text(row, "next_action", "none"),
            }
        )
    return gaps


def _first_action(gaps: Sequence[Mapping[str, str]]) -> str:
    for gap in gaps:
        action = text(gap, "next_action", "none")
        if action and action != "none":
            return action
    return "none"


def _compact_gap_action(action: str, *, source_runner_command: str) -> str:
    if not action or action == "none":
        return "none"
    if "claude" in action.lower() and source_runner_command and source_runner_command != "none":
        return (
            "Repair Claude auth or provide ANTHROPIC_API_KEY; collect real Claude "
            f"all-cases evidence with `{source_runner_command}`; rerun frontier "
            "evidence intake and readiness."
        )
    if len(action) <= 420:
        return action
    return action[:417].rstrip() + "..."


def build_gap_matrix(
    *,
    readiness_runner: ReadinessRunner = frontier_readiness.run_audit,
    output_path: Path = DEFAULT_OUTPUT,
) -> dict[str, object]:
    _markdown, readiness_payload, readiness_path = readiness_runner(write=False)
    readiness_results = _readiness_results(readiness_payload)
    by_requirement = _by_requirement(readiness_results)
    readiness_gaps = _gap_rows(readiness_results)

    coding = metadata(CODING_SCORECARD)
    churn = metadata(SOURCE_CHURN_DIAGNOSTICS)
    intake = metadata(FRONTIER_MODEL_EVIDENCE_INTAKE)
    availability = metadata(FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS)
    shadow = metadata(MODEL_SHADOW_SCORECARD)
    tournament = metadata(MODEL_TOURNAMENT_SCORECARD)
    hosted = metadata(HOSTED_PR_REPAIR_SCORECARD)
    offline = metadata(OFFLINE_AUTONOMY_SCORECARD)
    tournament_winners = tournament_winner_counts(MODEL_TOURNAMENT_SCORECARD)
    tournament_winner_total = sum(tournament_winners.values())
    local_model_winner_count = tournament_winners.get("local_model", 0)
    frontier_model_winner_count = (
        tournament_winners.get("codex", 0) + tournament_winners.get("claude", 0)
    )
    tournament_runtime_measurements = _parse_keyed_counts(
        text(tournament, "runtime measurements")
    )
    available_source_leaders = _parse_keyed_counts(
        text(tournament, "available-source leader counts")
    )
    available_source_leader_total = sum(available_source_leaders.values())
    tournament_unmeasured_runtime_count = tournament_runtime_measurements.get(
        "unmeasured",
        0,
    )
    tournament_source_kinds = _source_kinds_set(text(tournament, "source kinds"))
    tournament_status_proven = (
        text(tournament, "status") == "passed"
        and text(tournament, "evidence mode") == "real_artifacts"
    )
    codex_head_to_head_available_sources_proven = (
        text(tournament, "evidence mode") == "real_artifacts"
        and {"codex", "local_model"}.issubset(tournament_source_kinds)
        and available_source_leader_total >= 6
        and available_source_leaders.get("local_model", 0) == available_source_leader_total
        and available_source_leaders.get("codex", 0) == 0
        and available_source_leaders.get("none", 0) == 0
        and tournament_unmeasured_runtime_count == 0
    )
    offline_autonomy_proven = (
        text(offline, "status") == "passed"
        and text(offline, "average score") == "100/100"
    )
    meso_workflow_superiority_proven, meso_workflow_actual = _system_tournament_proven(
        MESO_WORKFLOW_TOURNAMENT_SCORECARD
    )
    macro_long_horizon_superiority_proven, macro_long_horizon_actual = (
        _system_tournament_proven(MACRO_LONG_HORIZON_TOURNAMENT_SCORECARD)
    )
    deep_context_superiority_proven, deep_context_actual = _system_tournament_proven(
        CONTEXT_REASONING_TOURNAMENT_SCORECARD
    )

    core_requirements = (
        "coding_scorecard_status",
        "coding_score",
        "coding_pass_rate",
        "source_stability",
        "coding_scorecard_current_source_freshness",
    )
    source_requirements = (
        "local_model_candidate_run_status",
        "model_shadow_scorecard_status",
        "model_shadow_real_manifest_mode",
        "model_tournament_scorecard_status",
        "model_tournament_real_artifacts_mode",
    )
    hosted_requirements = (
        "hosted_pr_repair_scorecard_status",
        "hosted_pr_repair_real_inventory_mode",
        "hosted_pr_repair_promotion_eligible",
    )

    core_proven = _requirements_passed(by_requirement, core_requirements)
    frontier_evidence_proven = not readiness_gaps
    source_proven = _requirements_passed(by_requirement, source_requirements)
    hosted_proven = _requirements_passed(by_requirement, hosted_requirements)
    candidate_generation_superiority_proven = (
        frontier_evidence_proven
        and tournament_status_proven
        and tournament_winner_total >= 6
        and local_model_winner_count == tournament_winner_total
        and frontier_model_winner_count == 0
        and tournament_winners.get("none", 0) == 0
        and tournament_unmeasured_runtime_count == 0
    )
    frontier_proven = (
        frontier_evidence_proven
        and candidate_generation_superiority_proven
        and offline_autonomy_proven
        and meso_workflow_superiority_proven
        and macro_long_horizon_superiority_proven
        and deep_context_superiority_proven
    )

    missing_sources = text(intake, "missing/incomplete sources", "none")
    source_auth_mode = text(availability, "claude source auth mode", "none")
    api_key_probe_status = text(availability, "claude api-key probe status", "none")
    source_runner_command = text(availability, "claude source runner command", "none")
    gaps = [
        {
            **gap,
            "next_action": _compact_gap_action(
                text(gap, "next_action", "none"),
                source_runner_command=source_runner_command,
            ),
        }
        for gap in readiness_gaps
    ]
    winner_counts_actual = (
        f"winner_counts={_winner_counts_summary(tournament_winners)}; "
        "required=local_model wins all >=6 real-artifact tournament cases; "
        f"runtime_measurements={text(tournament, 'runtime measurements', 'missing')}"
    )
    available_source_actual = (
        f"available_source_leaders={_winner_counts_summary(available_source_leaders)}; "
        f"sources={text(tournament, 'source kinds', 'missing')}; "
        f"runtime_measurements={text(tournament, 'runtime measurements', 'missing')}"
    )
    candidate_generation_next_action = (
        _first_action(gaps)
        if gaps
        else (
            "Collect stronger local/CHILI candidate evidence or lower-risk repairs "
            "until local_model wins every required tournament case against Codex/Claude."
        )
    )
    if not candidate_generation_superiority_proven:
        gaps.append(
            {
                "gap_id": "candidate_generation_superiority",
                "status": "warning",
                "required": (
                    "local_model wins every required real-artifact tournament case "
                    "against Codex/Claude with zero unmeasured runtime candidates"
                ),
                "actual": winner_counts_actual,
                "evidence": str(MODEL_TOURNAMENT_SCORECARD),
                "next_action": candidate_generation_next_action,
            }
        )
    if not offline_autonomy_proven:
        gaps.append(
            {
                "gap_id": "premium_independent_local_autonomy",
                "status": "warning",
                "required": "passed offline plan/edit/test/review with zero premium model calls",
                "actual": (
                    f"status={text(offline, 'status', 'missing')}; "
                    f"score={text(offline, 'average score', 'missing')}"
                ),
                "evidence": str(OFFLINE_AUTONOMY_SCORECARD),
                "next_action": (
                    "Run `python scripts/autopilot_offline_project_autonomy_benchmark.py --json` "
                    "with the local coder installed and repair any local-only workflow failure."
                ),
            }
        )
    system_level_gaps = (
        (
            "meso_project_workflow_superiority",
            meso_workflow_superiority_proven,
            meso_workflow_actual,
            MESO_WORKFLOW_TOURNAMENT_SCORECARD,
            "multi-file project workflows",
        ),
        (
            "macro_long_horizon_superiority",
            macro_long_horizon_superiority_proven,
            macro_long_horizon_actual,
            MACRO_LONG_HORIZON_TOURNAMENT_SCORECARD,
            "long-horizon repository projects",
        ),
        (
            "deep_context_reasoning_superiority",
            deep_context_superiority_proven,
            deep_context_actual,
            CONTEXT_REASONING_TOURNAMENT_SCORECARD,
            "deep-context repository reasoning",
        ),
    )
    for gap_id, proven, actual, evidence_path, task_class in system_level_gaps:
        if proven:
            continue
        gaps.append(
            {
                "gap_id": gap_id,
                "status": "warning",
                "required": (
                    "CHILI local system wins at least 3 measured real-artifact tasks against "
                    f"Codex 5.6 Sol and Fable 5 for {task_class}"
                ),
                "actual": actual,
                "evidence": str(evidence_path),
                "next_action": (
                    f"Collect equal-goal Codex/Fable/CHILI artifacts for {task_class}, replay "
                    "behavior and review gates, then publish the measured system-level tournament."
                ),
            }
        )

    proof_matrix = [
        _domain_row(
            domain="core_coding_benchmark",
            proof_status="proven" if core_proven else "not_proven",
            actual=(
                f"status={text(coding, 'status', 'missing')}; "
                f"score={text(coding, 'overall score', 'missing')}; "
                f"pass_rate={text(coding, 'pass rate', 'missing')}; "
                f"source_stability={text(coding, 'source stability', 'missing')}; "
                f"source_freshness={text(churn, 'current source freshness', 'missing')}"
            ),
            evidence=str(CODING_SCORECARD),
            next_action="none" if core_proven else text(churn, "next action", "rerun coding benchmark"),
        ),
        _domain_row(
            domain="frontier_source_evidence",
            proof_status="proven" if source_proven else "not_proven",
            actual=(
                f"ready_sources={text(intake, 'ready sources', 'missing')}; "
                f"missing_sources={missing_sources}; "
                f"claude_auth={source_auth_mode}; "
                f"api_key_probe={api_key_probe_status}"
            ),
            evidence=str(FRONTIER_MODEL_EVIDENCE_INTAKE),
            next_action=_first_action(gaps),
        ),
        _domain_row(
            domain="model_shadow_evidence",
            proof_status=(
                "proven"
                if text(shadow, "status") == "passed"
                and text(shadow, "evidence mode") == "real_manifest"
                else "not_proven"
            ),
            actual=(
                f"status={text(shadow, 'status', 'missing')}; "
                f"mode={text(shadow, 'evidence mode', 'missing')}; "
                f"missing_sources={text(shadow, 'missing source kinds', 'none')}"
            ),
            evidence=str(MODEL_SHADOW_SCORECARD),
            next_action=_first_action(gaps),
        ),
        _domain_row(
            domain="model_candidate_tournament",
            proof_status=(
                "proven"
                if tournament_status_proven
                else "not_proven"
            ),
            actual=(
                f"status={text(tournament, 'status', 'missing')}; "
                f"mode={text(tournament, 'evidence mode', 'missing')}; "
                f"missing_sources={text(tournament, 'missing source kinds', 'none')}"
            ),
            evidence=str(MODEL_TOURNAMENT_SCORECARD),
            next_action=_first_action(gaps),
        ),
        _domain_row(
            domain="candidate_generation_superiority",
            proof_status=(
                "proven" if candidate_generation_superiority_proven else "not_proven"
            ),
            actual=winner_counts_actual,
            evidence=str(MODEL_TOURNAMENT_SCORECARD),
            next_action="none"
            if candidate_generation_superiority_proven
                else candidate_generation_next_action,
        ),
        _domain_row(
            domain="premium_independent_local_autonomy",
            proof_status="proven" if offline_autonomy_proven else "not_proven",
            actual=(
                f"status={text(offline, 'status', 'missing')}; "
                f"score={text(offline, 'average score', 'missing')}"
            ),
            evidence=str(OFFLINE_AUTONOMY_SCORECARD),
            next_action="none" if offline_autonomy_proven else _first_action(gaps),
        ),
        _domain_row(
            domain="meso_project_workflow_superiority",
            proof_status="proven" if meso_workflow_superiority_proven else "not_proven",
            actual=meso_workflow_actual,
            evidence=str(MESO_WORKFLOW_TOURNAMENT_SCORECARD),
            next_action="none" if meso_workflow_superiority_proven else _first_action(gaps),
        ),
        _domain_row(
            domain="macro_long_horizon_superiority",
            proof_status=("proven" if macro_long_horizon_superiority_proven else "not_proven"),
            actual=macro_long_horizon_actual,
            evidence=str(MACRO_LONG_HORIZON_TOURNAMENT_SCORECARD),
            next_action="none" if macro_long_horizon_superiority_proven else _first_action(gaps),
        ),
        _domain_row(
            domain="deep_context_reasoning_superiority",
            proof_status="proven" if deep_context_superiority_proven else "not_proven",
            actual=deep_context_actual,
            evidence=str(CONTEXT_REASONING_TOURNAMENT_SCORECARD),
            next_action="none" if deep_context_superiority_proven else _first_action(gaps),
        ),
        _domain_row(
            domain="codex_head_to_head_available_sources",
            proof_status=(
                "proven"
                if codex_head_to_head_available_sources_proven
                else "not_proven"
            ),
            actual=available_source_actual,
            evidence=str(MODEL_TOURNAMENT_SCORECARD),
            next_action=(
                "none"
                if codex_head_to_head_available_sources_proven
                else "Collect measured Codex and local_model tournament candidates, then rerun frontier intake."
            ),
        ),
        _domain_row(
            domain="hosted_pr_repair_evidence",
            proof_status="proven" if hosted_proven else "not_proven",
            actual=(
                f"status={text(hosted, 'status', 'missing')}; "
                f"mode={text(hosted, 'evidence mode', 'missing')}; "
                f"promotion_eligible={text(hosted, 'promotion eligible', 'missing')}"
            ),
            evidence=str(HOSTED_PR_REPAIR_SCORECARD),
            next_action="none" if hosted_proven else "Collect and validate hosted PR repair evidence.",
        ),
    ]

    claim_status = (
        "frontier_superiority_proven"
        if frontier_proven
        else "frontier_superiority_not_proven"
    )
    status = "passed" if frontier_proven else "warning"
    return {
        "schema": FRONTIER_GAP_MATRIX_SCHEMA_VERSION,
        "generated_utc": _utc_now(),
        "status": status,
        "claim_status": claim_status,
        "readiness_status": text(readiness_payload, "status", "missing"),
        "readiness_score": readiness_payload.get("readiness_score", 0),
        "readiness_blockers": readiness_payload.get("blockers", len(gaps)),
        "readiness_report": str(readiness_path),
        "output": str(output_path),
        "core_coding_proven": core_proven,
        "frontier_evidence_proven": frontier_evidence_proven,
        "frontier_superiority_proven": frontier_proven,
        "candidate_generation_superiority_proven": (
            candidate_generation_superiority_proven
        ),
        "offline_autonomy_proven": offline_autonomy_proven,
        "micro_candidate_superiority_proven": candidate_generation_superiority_proven,
        "meso_workflow_superiority_proven": meso_workflow_superiority_proven,
        "macro_long_horizon_superiority_proven": macro_long_horizon_superiority_proven,
        "deep_context_superiority_proven": deep_context_superiority_proven,
        "codex_head_to_head_available_sources_proven": (
            codex_head_to_head_available_sources_proven
        ),
        "superiority_required_winner_source": "local_model",
        "tournament_winner_counts": tournament_winners,
        "tournament_winner_total": tournament_winner_total,
        "available_source_leader_counts": available_source_leaders,
        "available_source_leader_total": available_source_leader_total,
        "local_model_winner_count": local_model_winner_count,
        "frontier_model_winner_count": frontier_model_winner_count,
        "tournament_runtime_measurements": tournament_runtime_measurements,
        "tournament_unmeasured_runtime_count": tournament_unmeasured_runtime_count,
        "missing_sources": missing_sources,
        "claude_source_auth_mode": source_auth_mode,
        "claude_api_key_probe_status": api_key_probe_status,
        "claude_source_runner_command": source_runner_command,
        "gap_count": len(gaps),
        "next_action": _first_action(gaps),
        "proof_matrix": proof_matrix,
        "gaps": gaps,
    }


def render_report(summary: Mapping[str, object]) -> str:
    proof_matrix = [
        row for row in summary.get("proof_matrix", []) if isinstance(row, Mapping)
    ]
    gaps = [row for row in summary.get("gaps", []) if isinstance(row, Mapping)]
    lines = [
        "# CHILI Frontier Gap Matrix",
        "",
        f"- Schema: {FRONTIER_GAP_MATRIX_SCHEMA_VERSION}",
        f"- Generated UTC: {summary.get('generated_utc', '')}",
        f"- Status: {summary.get('status', 'missing')}",
        f"- Claim status: {summary.get('claim_status', 'missing')}",
        f"- Readiness score: {summary.get('readiness_score', 0)}/100",
        f"- Readiness blockers: {summary.get('readiness_blockers', 0)}",
        f"- Core coding proven: {summary.get('core_coding_proven', False)}",
        f"- Frontier evidence proven: {summary.get('frontier_evidence_proven', False)}",
        f"- Frontier superiority proven: {summary.get('frontier_superiority_proven', False)}",
        f"- Candidate generation superiority proven: {summary.get('candidate_generation_superiority_proven', False)}",
        f"- Premium-independent local autonomy proven: {summary.get('offline_autonomy_proven', False)}",
        f"- Micro candidate superiority proven: {summary.get('micro_candidate_superiority_proven', False)}",
        f"- Meso project workflow superiority proven: {summary.get('meso_workflow_superiority_proven', False)}",
        f"- Macro long-horizon superiority proven: {summary.get('macro_long_horizon_superiority_proven', False)}",
        f"- Deep-context reasoning superiority proven: {summary.get('deep_context_superiority_proven', False)}",
        f"- Codex head-to-head available-source proven: {summary.get('codex_head_to_head_available_sources_proven', False)}",
        f"- Tournament winner counts: {_winner_counts_summary(summary.get('tournament_winner_counts', {})) if isinstance(summary.get('tournament_winner_counts'), Mapping) else 'missing'}",
        f"- Available-source leader counts: {_winner_counts_summary(summary.get('available_source_leader_counts', {})) if isinstance(summary.get('available_source_leader_counts'), Mapping) else 'missing'}",
        f"- Tournament runtime measurements: {_runtime_counts_summary(summary.get('tournament_runtime_measurements', {})) if isinstance(summary.get('tournament_runtime_measurements'), Mapping) else 'missing'}",
        f"- Tournament unmeasured runtime count: {summary.get('tournament_unmeasured_runtime_count', 0)}",
        f"- Superiority required winner source: {summary.get('superiority_required_winner_source', 'local_model')}",
        f"- Missing sources: {summary.get('missing_sources', 'none')}",
        f"- Claude source auth mode: {summary.get('claude_source_auth_mode', 'none')}",
        f"- Claude API-key probe status: {summary.get('claude_api_key_probe_status', 'none')}",
        f"- Claude source runner command: {summary.get('claude_source_runner_command', 'none')}",
        f"- Next action: {summary.get('next_action', 'none')}",
        "- Safety: read-only evidence synthesis only; no model calls, git action, runtime restart, deployment, database, broker, or live-trading action.",
        "",
        "## Proof Matrix",
        "",
        "| Domain | Proof status | Actual | Evidence | Next action |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in proof_matrix:
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_cell(text(row, "domain")),
                    _escape_cell(text(row, "proof_status")),
                    _escape_cell(text(row, "actual")),
                    _escape_cell(text(row, "evidence")),
                    _escape_cell(text(row, "next_action", "none")),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Blocking Gaps",
            "",
            "| Gap | Status | Required | Actual | Evidence | Next action |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    if gaps:
        for row in gaps:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _escape_cell(text(row, "gap_id")),
                        _escape_cell(text(row, "status")),
                        _escape_cell(text(row, "required")),
                        _escape_cell(text(row, "actual")),
                        _escape_cell(text(row, "evidence")),
                        _escape_cell(text(row, "next_action", "none")),
                    ]
                )
                + " |"
            )
    else:
        lines.append("| none | passed | none | none | none | none |")
    lines.append("")
    return "\n".join(lines)


def write_report(markdown: str, output_path: Path = DEFAULT_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synthesize CHILI frontier readiness into a compact gap matrix."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-gap", action="store_true")
    args = parser.parse_args(argv)

    summary = build_gap_matrix(output_path=args.output)
    markdown = render_report(summary)
    if not args.no_write:
        write_report(markdown, args.output)
    if args.json:
        payload = dict(summary)
        payload["written"] = not args.no_write
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(markdown if args.no_write else f"Wrote {args.output}")
    if args.fail_on_gap and summary.get("status") != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
