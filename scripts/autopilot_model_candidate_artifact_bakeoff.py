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

from scripts.autopilot_frontier_bakeoff_benchmark import (  # noqa: E402
    BakeoffCase,
    BakeoffDecision,
    PatchCandidate,
    _command_text,
    _escape_cell,
    decide_bakeoff,
)
from scripts.autopilot_real_chili_candidate_bakeoff import (  # noqa: E402
    MIN_CASES,
    REAL_CHILI_CANDIDATE_BAKEOFF_SCHEMA_VERSION,
    REQUIRED_COMPARISON_CLASSES,
    TARGET_SCORE,
    average_score,
    benchmark_status as real_candidate_benchmark_status,
    default_cases as real_chili_default_cases,
    missing_comparison_classes,
)


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "MODEL_CANDIDATE_ARTIFACT_BAKEOFF.md"
MODEL_CANDIDATE_ARTIFACT_SCHEMA_VERSION = "chili.model-candidate-artifacts.v1"
MODEL_CANDIDATE_ARTIFACT_BAKEOFF_SCHEMA_VERSION = "chili.model-candidate-artifact-bakeoff.v1"
ALLOWED_SOURCE_KINDS = ("fixture", "codex", "claude", "local_model", "other")
EVALUATION_MODE_FIXTURE = "fixture_expectations"
EVALUATION_MODE_ACTUAL = "actual_candidate"
ALLOWED_EVALUATION_MODES = (EVALUATION_MODE_FIXTURE, EVALUATION_MODE_ACTUAL)


class ArtifactError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ArtifactError(f"{label} must be an object")
    return value


def _required_text(payload: Mapping[str, object], key: str, *, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ArtifactError(f"{label}.{key} is required")
    return value


def _optional_text(
    payload: Mapping[str, object],
    key: str,
    *,
    label: str,
    default: str,
) -> str:
    value = payload.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ArtifactError(f"{label}.{key} must be text")
    return value


def _required_number(payload: Mapping[str, object], key: str, *, label: str) -> float:
    value = payload.get(key)
    if not isinstance(value, (int, float)):
        raise ArtifactError(f"{label}.{key} must be a number")
    return float(value)


def _text_tuple(
    payload: Mapping[str, object],
    key: str,
    *,
    label: str,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ArtifactError(f"{label}.{key} must be a list")
    if not value and not allow_empty:
        raise ArtifactError(f"{label}.{key} must be a non-empty list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ArtifactError(f"{label}.{key} contains a blank value")
        out.append(item.strip())
    return tuple(out)


def _candidate_to_artifact(
    candidate: PatchCandidate,
    *,
    model_name: str,
    source_kind: str = "fixture",
    collected_at: str | None = None,
) -> dict[str, object]:
    return {
        "candidate_id": candidate.candidate_id,
        "model_name": model_name,
        "source_kind": source_kind,
        "collected_at": collected_at or _utc_now(),
        "patch": candidate.patch,
        "planned_file": candidate.planned_file,
        "expected_changed_files": list(candidate.expected_changed_files),
        "declared_commands": list(candidate.declared_commands),
        "duration_seconds": candidate.duration_seconds,
        "cost_units": candidate.cost_units,
    }


def default_artifact() -> dict[str, object]:
    entries: list[dict[str, object]] = []
    collected_at = "2026-06-02T00:00:00Z"
    for case in real_chili_default_cases():
        entries.append(
            {
                "case_id": case.case_id,
                "comparison_class": case.bakeoff_class,
                "expected_decision": case.expected_decision,
                "expected_reason_fragment": case.expected_reason_fragment,
                "incumbent": _candidate_to_artifact(
                    case.incumbent,
                    model_name="chili-incumbent-fixture",
                    collected_at=collected_at,
                ),
                "challenger": _candidate_to_artifact(
                    case.challenger,
                    model_name="external-candidate-fixture",
                    collected_at=collected_at,
                ),
            }
        )
    return {
        "schema": MODEL_CANDIDATE_ARTIFACT_SCHEMA_VERSION,
        "evaluation_mode": EVALUATION_MODE_FIXTURE,
        "generated_utc": collected_at,
        "source": "deterministic fixture shaped like collected Codex/Claude/local-model output",
        "base_benchmark_schema": REAL_CHILI_CANDIDATE_BAKEOFF_SCHEMA_VERSION,
        "entries": entries,
    }


def load_artifact(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"invalid JSON artifact: {exc}") from exc
    return dict(_as_mapping(raw, label="artifact"))


def artifact_evaluation_mode(artifact: Mapping[str, object]) -> str:
    mode = artifact.get("evaluation_mode") or EVALUATION_MODE_FIXTURE
    if not isinstance(mode, str) or mode not in ALLOWED_EVALUATION_MODES:
        raise ArtifactError(
            "artifact.evaluation_mode must be one of "
            + ", ".join(ALLOWED_EVALUATION_MODES)
        )
    return mode


def _parse_candidate(payload: Mapping[str, object], *, label: str) -> PatchCandidate:
    source_kind = _required_text(payload, "source_kind", label=label)
    if source_kind not in ALLOWED_SOURCE_KINDS:
        raise ArtifactError(
            f"{label}.source_kind must be one of {', '.join(ALLOWED_SOURCE_KINDS)}"
        )
    _required_text(payload, "model_name", label=label)
    _required_text(payload, "collected_at", label=label)
    return PatchCandidate(
        candidate_id=_required_text(payload, "candidate_id", label=label),
        patch=_required_text(payload, "patch", label=label),
        planned_file=_required_text(payload, "planned_file", label=label),
        expected_changed_files=_text_tuple(payload, "expected_changed_files", label=label),
        declared_commands=_text_tuple(
            payload,
            "declared_commands",
            label=label,
            allow_empty=True,
        ),
        duration_seconds=_required_number(payload, "duration_seconds", label=label),
        cost_units=_required_number(payload, "cost_units", label=label),
    )


def artifact_to_cases(artifact: Mapping[str, object]) -> list[BakeoffCase]:
    schema = artifact.get("schema")
    if schema != MODEL_CANDIDATE_ARTIFACT_SCHEMA_VERSION:
        raise ArtifactError(
            f"artifact schema is {schema or 'missing'} instead of "
            f"{MODEL_CANDIDATE_ARTIFACT_SCHEMA_VERSION}"
        )
    entries = artifact.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ArtifactError("artifact.entries must be a non-empty list")

    base_cases = {case.case_id: case for case in real_chili_default_cases()}
    cases: list[BakeoffCase] = []
    for index, raw_entry in enumerate(entries, start=1):
        entry = _as_mapping(raw_entry, label=f"entries[{index}]")
        case_id = _required_text(entry, "case_id", label=f"entries[{index}]")
        base = base_cases.get(case_id)
        if base is None:
            raise ArtifactError(f"entries[{index}].case_id is unknown: {case_id}")

        comparison_class = _required_text(
            entry,
            "comparison_class",
            label=f"entries[{index}]",
        )
        if comparison_class != base.bakeoff_class:
            raise ArtifactError(
                f"entries[{index}].comparison_class is {comparison_class} "
                f"instead of {base.bakeoff_class}"
            )

        incumbent_payload = _as_mapping(entry.get("incumbent"), label=f"entries[{index}].incumbent")
        challenger_payload = _as_mapping(entry.get("challenger"), label=f"entries[{index}].challenger")
        cases.append(
            dataclasses.replace(
                base,
                incumbent=_parse_candidate(incumbent_payload, label=f"entries[{index}].incumbent"),
                challenger=_parse_candidate(challenger_payload, label=f"entries[{index}].challenger"),
                expected_decision=_optional_text(
                    entry,
                    "expected_decision",
                    label=f"entries[{index}]",
                    default=base.expected_decision,
                ),
                expected_reason_fragment=_optional_text(
                    entry,
                    "expected_reason_fragment",
                    label=f"entries[{index}]",
                    default=base.expected_reason_fragment,
                ),
            )
        )
    return cases


def source_kinds(artifact: Mapping[str, object]) -> tuple[str, ...]:
    kinds: set[str] = set()
    for raw_entry in artifact.get("entries") or []:
        if not isinstance(raw_entry, Mapping):
            continue
        for side in ("incumbent", "challenger"):
            raw_candidate = raw_entry.get(side)
            if isinstance(raw_candidate, Mapping):
                kind = raw_candidate.get("source_kind")
                if isinstance(kind, str) and kind.strip():
                    kinds.add(kind.strip())
    return tuple(sorted(kinds))


def benchmark_status(results: Sequence[BakeoffDecision], cases: Sequence[BakeoffCase]) -> str:
    return real_candidate_benchmark_status(results, cases)


def decide_artifact_bakeoff(case: BakeoffCase, *, evaluation_mode: str) -> BakeoffDecision:
    decision = decide_bakeoff(case)
    if evaluation_mode == EVALUATION_MODE_FIXTURE:
        return decision
    if evaluation_mode != EVALUATION_MODE_ACTUAL:
        raise ArtifactError(f"unsupported evaluation mode: {evaluation_mode}")
    score = 100 if decision.incumbent.passed and decision.challenger.passed else 0
    return dataclasses.replace(decision, score=score)


def render_scorecard(
    artifact: Mapping[str, object],
    cases: Sequence[BakeoffCase],
    results: Sequence[BakeoffDecision],
    *,
    generated_at: datetime | None = None,
) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    lines = [
        "# CHILI Model Candidate Artifact Bakeoff",
        "",
        f"- Schema: {MODEL_CANDIDATE_ARTIFACT_BAKEOFF_SCHEMA_VERSION}",
        f"- Artifact schema: {artifact.get('schema') or 'missing'}",
        f"- Evaluation mode: {artifact_evaluation_mode(artifact)}",
        f"- Base benchmark schema: {artifact.get('base_benchmark_schema') or REAL_CHILI_CANDIDATE_BAKEOFF_SCHEMA_VERSION}",
        f"- Generated UTC: {generated_at.isoformat().replace('+00:00', 'Z')}",
        f"- Status: {benchmark_status(results, cases)}",
        f"- Target score: {TARGET_SCORE}",
        f"- Cases: {len(results)}",
        f"- Average score: {average_score(results)}/100",
        f"- Required comparison classes: {', '.join(REQUIRED_COMPARISON_CLASSES)}",
        f"- Missing comparison classes: {', '.join(missing_comparison_classes(cases)) or 'none'}",
        f"- Artifact source kinds: {', '.join(source_kinds(artifact)) or 'none'}",
        "- Required behavior: collected model artifacts must bind to known CHILI bug slices, include scoped patches and declared behavior evidence, then replay through the same incumbent/challenger decision gate used for frontier comparisons.",
        "- Safety: artifact replay only; no model calls, git action in the real checkout, runtime restart, deployment, database migration, broker call, or live-trading action.",
        "",
        "| Case | Comparison Class | Decision | Reason | Score | Evidence |",
        "| --- | --- | --- | --- | ---: | --- |",
    ]
    for case, result in zip(cases, results):
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_cell(case.case_id),
                    _escape_cell(case.bakeoff_class),
                    _escape_cell(result.decision),
                    _escape_cell(result.reason),
                    str(result.score),
                    _escape_cell(result.evidence),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def write_scorecard(markdown: str, output_path: Path = DEFAULT_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def run_model_candidate_artifact_bakeoff(
    *,
    artifact_path: Path | None = None,
    output_path: Path = DEFAULT_OUTPUT,
    write: bool = True,
) -> tuple[Mapping[str, object], list[BakeoffCase], list[BakeoffDecision], str, Path]:
    artifact = load_artifact(artifact_path) if artifact_path else default_artifact()
    evaluation_mode = artifact_evaluation_mode(artifact)
    cases = artifact_to_cases(artifact)
    results = [decide_artifact_bakeoff(case, evaluation_mode=evaluation_mode) for case in cases]
    markdown = render_scorecard(artifact, cases, results)
    if write:
        write_scorecard(markdown, output_path)
    return artifact, cases, results, markdown, output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay collected model candidate patch artifacts on real CHILI bug slices."
    )
    parser.add_argument("--artifact", type=Path, help="JSON artifact with collected model outputs.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        artifact, cases, results, markdown, output_path = run_model_candidate_artifact_bakeoff(
            artifact_path=args.artifact,
            output_path=args.output,
            write=not args.no_write,
        )
    except ArtifactError as exc:
        print(f"artifact error: {exc}", file=sys.stderr)
        return 2

    status = benchmark_status(results, cases)
    if args.json:
        print(
            json.dumps(
                {
                    "schema": MODEL_CANDIDATE_ARTIFACT_BAKEOFF_SCHEMA_VERSION,
                    "artifact_schema": artifact.get("schema"),
                    "evaluation_mode": artifact_evaluation_mode(artifact),
                    "status": status,
                    "target_score": TARGET_SCORE,
                    "average_score": average_score(results),
                    "cases": len(results),
                    "source_kinds": source_kinds(artifact),
                    "output": str(output_path),
                    "results": [
                        {
                            "case_id": case.case_id,
                            "comparison_class": case.bakeoff_class,
                            "decision": result.decision,
                            "reason": result.reason,
                            "score": result.score,
                            "evidence": result.evidence,
                        }
                        for case, result in zip(cases, results)
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(markdown)
        if not args.no_write:
            print(f"Wrote {output_path}")
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
