from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_frontier_bakeoff_benchmark import (  # noqa: E402
    BakeoffCase,
    CandidateOutcome,
    PatchCandidate,
    _escape_cell,
    evaluate_candidate,
)
from scripts.autopilot_model_candidate_artifact_bakeoff import (  # noqa: E402
    MODEL_CANDIDATE_ARTIFACT_SCHEMA_VERSION,
    _candidate_to_artifact,
)
from scripts.autopilot_model_candidate_artifact_builder import (  # noqa: E402
    ArtifactBuildError,
    TRANSCRIPT_MIN_EVENTS,
    _candidate_from_drop,
    load_drops,
)
from scripts.autopilot_real_chili_candidate_bakeoff import (  # noqa: E402
    REAL_CHILI_CANDIDATE_BAKEOFF_SCHEMA_VERSION,
    REQUIRED_COMPARISON_CLASSES,
    TARGET_SCORE,
    default_cases as real_chili_default_cases,
    missing_comparison_classes,
)


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md"
MODEL_CANDIDATE_TOURNAMENT_ARTIFACT_SCHEMA_VERSION = "chili.model-candidate-tournament-artifacts.v1"
MODEL_CANDIDATE_TOURNAMENT_SCHEMA_VERSION = "chili.model-candidate-tournament-benchmark.v1"
SELF_TEST_EVIDENCE_MODE = "self_test"
REAL_ARTIFACT_EVIDENCE_MODE = "real_artifacts"
REQUIRED_SOURCE_KINDS = ("codex", "claude", "local_model")
REQUIRED_FRONTIER_MODEL_TARGETS = {
    "codex": ("gpt-5.5",),
    "claude": ("fable-5", "claude-fable-5"),
}
REQUIRED_FRONTIER_MODEL_LABELS = {
    "codex": "gpt-5.5",
    "claude": "fable-5",
}
MIN_CASES = 6
SYNTHETIC_MARKERS = ("self-test", "self_test", "synthetic", "fixture", "deterministic", "mock")


class TournamentError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class CandidateRecord:
    source_kind: str
    model_name: str
    candidate: PatchCandidate


@dataclasses.dataclass(frozen=True)
class TournamentCase:
    base_case: BakeoffCase
    incumbent: CandidateRecord
    candidates: tuple[CandidateRecord, ...]


@dataclasses.dataclass(frozen=True)
class TournamentCandidateResult:
    record: CandidateRecord
    outcome: CandidateOutcome

    @property
    def passed(self) -> bool:
        return self.outcome.passed

    @property
    def evidence(self) -> str:
        return (
            f"{self.record.source_kind}/{self.record.candidate.candidate_id}:"
            f"{self.outcome.status}/{self.outcome.reason}"
        )


@dataclasses.dataclass(frozen=True)
class TournamentResult:
    case: TournamentCase
    incumbent_outcome: CandidateOutcome
    candidate_results: tuple[TournamentCandidateResult, ...]
    winner: TournamentCandidateResult | None
    score: int
    reason: str

    @property
    def passed(self) -> bool:
        return self.score >= TARGET_SCORE

    @property
    def evidence(self) -> str:
        source_counts = ",".join(sorted({result.record.source_kind for result in self.candidate_results}))
        rejected = [
            result.evidence
            for result in self.candidate_results
            if not result.passed
        ]
        details = [
            f"reason={self.reason}",
            f"incumbent={self.incumbent_outcome.status}/{self.incumbent_outcome.reason}",
            f"sources={source_counts or 'none'}",
            f"passed={sum(1 for result in self.candidate_results if result.passed)}",
            f"rejected={len(rejected)}",
        ]
        if self.winner:
            details.append(
                "winner="
                + f"{self.winner.record.source_kind}/{self.winner.record.candidate.candidate_id}"
            )
            details.append(f"winner_duration={self.winner.record.candidate.duration_seconds:.2f}s")
            details.append(f"winner_cost={self.winner.record.candidate.cost_units:.2f}")
        if rejected:
            details.append("rejected_examples=" + ";".join(rejected[:3]))
        return "; ".join(details)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TournamentError(f"{label} must be an object")
    return value


def _required_text(payload: Mapping[str, object], key: str, *, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TournamentError(f"{label}.{key} is required")
    return value


def _normalized_model_name(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def frontier_model_target_error(source_kind: str, model_name: str, *, label: str) -> str | None:
    markers = REQUIRED_FRONTIER_MODEL_TARGETS.get(source_kind)
    if not markers:
        return None
    normalized = _normalized_model_name(model_name)
    if any(marker in normalized for marker in markers):
        return None
    expected = REQUIRED_FRONTIER_MODEL_LABELS.get(source_kind) or "/".join(markers)
    return (
        f"{label}.model_name must identify required frontier target "
        f"{source_kind}={expected}; got {model_name}"
    )


def frontier_model_targets_summary() -> str:
    return ", ".join(
        f"{source_kind}={REQUIRED_FRONTIER_MODEL_LABELS[source_kind]}"
        for source_kind in REQUIRED_FRONTIER_MODEL_LABELS
    )


def _looks_synthetic(value: object) -> bool:
    text = str(value or "").strip().lower()
    return any(marker in text for marker in SYNTHETIC_MARKERS)


def _required_number(payload: Mapping[str, object], key: str, *, label: str) -> float:
    value = payload.get(key)
    if not isinstance(value, (int, float)):
        raise TournamentError(f"{label}.{key} must be a number")
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
        raise TournamentError(f"{label}.{key} must be a list")
    if not value and not allow_empty:
        raise TournamentError(f"{label}.{key} must be a non-empty list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise TournamentError(f"{label}.{key} contains a blank value")
        out.append(item.strip())
    return tuple(out)


def _record_from_artifact(payload: Mapping[str, object], *, label: str) -> CandidateRecord:
    source_kind = _required_text(payload, "source_kind", label=label)
    model_name = _required_text(payload, "model_name", label=label)
    target_error = frontier_model_target_error(source_kind, model_name, label=label)
    if target_error:
        raise TournamentError(target_error)
    _required_text(payload, "collected_at", label=label)
    return CandidateRecord(
        source_kind=source_kind,
        model_name=model_name,
        candidate=PatchCandidate(
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
        ),
    )


def _record_to_artifact(record: CandidateRecord) -> dict[str, object]:
    return _candidate_to_artifact(
        record.candidate,
        model_name=record.model_name,
        source_kind=record.source_kind,
        collected_at="2026-06-02T00:00:00Z",
    )


def _synthetic_candidate_record(
    base_case: BakeoffCase,
    *,
    source_kind: str,
    model_name: str,
    patch_candidate: PatchCandidate,
    duration_seconds: float,
    cost_units: float,
) -> CandidateRecord:
    candidate = dataclasses.replace(
        patch_candidate,
        candidate_id=f"{source_kind}-{base_case.case_id}",
        duration_seconds=duration_seconds,
        cost_units=cost_units,
    )
    return CandidateRecord(source_kind, model_name, candidate)


def default_artifact() -> dict[str, object]:
    entries: list[dict[str, object]] = []
    for base in real_chili_default_cases():
        incumbent = CandidateRecord(
            "fixture",
            "chili-incumbent-fixture",
            base.incumbent,
        )
        codex = _synthetic_candidate_record(
            base,
            source_kind="codex",
            model_name="codex-gpt-5.5-candidate",
            patch_candidate=base.incumbent,
            duration_seconds=10.0,
            cost_units=10.0,
        )
        claude = _synthetic_candidate_record(
            base,
            source_kind="claude",
            model_name="claude-fable-5-candidate",
            patch_candidate=base.incumbent,
            duration_seconds=8.0,
            cost_units=8.5,
        )
        local_model = _synthetic_candidate_record(
            base,
            source_kind="local_model",
            model_name="local-coder-candidate",
            patch_candidate=dataclasses.replace(base.challenger, declared_commands=()),
            duration_seconds=7.0,
            cost_units=2.0,
        )
        entries.append(
            {
                "case_id": base.case_id,
                "comparison_class": base.bakeoff_class,
                "incumbent": _record_to_artifact(incumbent),
                "candidates": [
                    _record_to_artifact(codex),
                    _record_to_artifact(claude),
                    _record_to_artifact(local_model),
                ],
            }
        )
    return {
        "schema": MODEL_CANDIDATE_TOURNAMENT_ARTIFACT_SCHEMA_VERSION,
        "generated_utc": "2026-06-02T00:00:00Z",
        "source": "deterministic multi-source model candidate replay fixture",
        "base_benchmark_schema": REAL_CHILI_CANDIDATE_BAKEOFF_SCHEMA_VERSION,
        "required_source_kinds": list(REQUIRED_SOURCE_KINDS),
        "required_comparison_classes": list(REQUIRED_COMPARISON_CLASSES),
        "entries": entries,
    }


def load_artifact(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TournamentError(f"invalid JSON artifact: {exc}") from exc
    return dict(_as_mapping(raw, label="artifact"))


def tournament_cases_from_artifact(artifact: Mapping[str, object]) -> list[TournamentCase]:
    schema = artifact.get("schema")
    if schema != MODEL_CANDIDATE_TOURNAMENT_ARTIFACT_SCHEMA_VERSION:
        raise TournamentError(
            f"artifact schema is {schema or 'missing'} instead of "
            f"{MODEL_CANDIDATE_TOURNAMENT_ARTIFACT_SCHEMA_VERSION}"
        )
    entries = artifact.get("entries")
    if not isinstance(entries, list) or not entries:
        raise TournamentError("artifact.entries must be a non-empty list")
    base_cases = {case.case_id: case for case in real_chili_default_cases()}
    cases: list[TournamentCase] = []
    for index, raw_entry in enumerate(entries, start=1):
        entry = _as_mapping(raw_entry, label=f"entries[{index}]")
        case_id = _required_text(entry, "case_id", label=f"entries[{index}]")
        base = base_cases.get(case_id)
        if base is None:
            raise TournamentError(f"entries[{index}].case_id is unknown: {case_id}")
        comparison_class = _required_text(entry, "comparison_class", label=f"entries[{index}]")
        if comparison_class != base.bakeoff_class:
            raise TournamentError(
                f"entries[{index}].comparison_class is {comparison_class} "
                f"instead of {base.bakeoff_class}"
            )
        incumbent = _record_from_artifact(
            _as_mapping(entry.get("incumbent"), label=f"entries[{index}].incumbent"),
            label=f"entries[{index}].incumbent",
        )
        raw_candidates = entry.get("candidates")
        if not isinstance(raw_candidates, list) or not raw_candidates:
            raise TournamentError(f"entries[{index}].candidates must be a non-empty list")
        candidates = tuple(
            _record_from_artifact(
                _as_mapping(raw_candidate, label=f"entries[{index}].candidates[{candidate_index}]"),
                label=f"entries[{index}].candidates[{candidate_index}]",
            )
            for candidate_index, raw_candidate in enumerate(raw_candidates, start=1)
        )
        cases.append(TournamentCase(base, incumbent, candidates))
    return cases


def build_artifact_from_drops(
    drops: Sequence[Mapping[str, object]],
    *,
    allow_partial: bool = False,
    allow_fixture: bool = False,
    require_provenance: bool = False,
    prompt_pack_path: Path | None = None,
) -> dict[str, object]:
    base_cases = {case.case_id: case for case in real_chili_default_cases()}
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for drop in drops:
        case_id = _required_text(drop, "case_id", label="drop")
        if case_id not in base_cases:
            raise TournamentError(f"unknown case_id: {case_id}")
        grouped[case_id].append(drop)
    entries: list[dict[str, object]] = []
    for case_id, case_drops in sorted(grouped.items()):
        base = base_cases[case_id]
        candidates: list[dict[str, object]] = []
        seen_candidate_ids: set[str] = set()
        for index, drop in enumerate(case_drops, start=1):
            try:
                candidate = _candidate_from_drop(
                    drop,
                    base_expected_files=base.incumbent.expected_changed_files,
                    allow_fixture=allow_fixture,
                    index=index,
                    require_provenance=require_provenance,
                    prompt_pack_path=prompt_pack_path,
                )
            except ArtifactBuildError as exc:
                raise TournamentError(str(exc)) from exc
            candidate_id = str(candidate.get("candidate_id") or "")
            if candidate_id in seen_candidate_ids:
                raise TournamentError(f"duplicate candidate_id for {case_id}: {candidate_id}")
            seen_candidate_ids.add(candidate_id)
            candidates.append(candidate)
        entries.append(
            {
                "case_id": base.case_id,
                "comparison_class": base.bakeoff_class,
                "incumbent": _record_to_artifact(
                    CandidateRecord("fixture", "chili-incumbent-fixture", base.incumbent)
                ),
                "candidates": candidates,
            }
        )
    artifact = {
        "schema": MODEL_CANDIDATE_TOURNAMENT_ARTIFACT_SCHEMA_VERSION,
        "generated_utc": _utc_now(),
        "source": "collected multi-source model candidate drops",
        "base_benchmark_schema": REAL_CHILI_CANDIDATE_BAKEOFF_SCHEMA_VERSION,
        "required_source_kinds": list(REQUIRED_SOURCE_KINDS),
        "required_comparison_classes": list(REQUIRED_COMPARISON_CLASSES),
        "entries": entries,
    }
    missing = missing_comparison_classes([base_cases[str(entry["case_id"])] for entry in entries])
    if missing and not allow_partial:
        raise TournamentError("missing comparison classes: " + ", ".join(missing))
    source_missing = missing_source_kinds(tournament_cases_from_artifact(artifact))
    if source_missing and not allow_partial:
        raise TournamentError("missing source kinds: " + ", ".join(source_missing))
    return artifact


def source_kinds(cases: Sequence[TournamentCase]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                candidate.source_kind
                for case in cases
                for candidate in case.candidates
            }
        )
    )


def missing_source_kinds(cases: Sequence[TournamentCase]) -> list[str]:
    covered = set(source_kinds(cases))
    return [source for source in REQUIRED_SOURCE_KINDS if source not in covered]


def _candidate_artifacts(artifact: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw_entries = artifact.get("entries")
    if not isinstance(raw_entries, list):
        return []
    candidates: list[Mapping[str, object]] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, Mapping):
            continue
        raw_candidates = raw_entry.get("candidates")
        if not isinstance(raw_candidates, list):
            continue
        candidates.extend(
            candidate
            for candidate in raw_candidates
            if isinstance(candidate, Mapping)
        )
    return candidates


def _has_verified_candidate_provenance(candidate: Mapping[str, object]) -> bool:
    provenance = candidate.get("provenance")
    if not isinstance(provenance, Mapping):
        return False
    if provenance.get("prompt_pack_verified") is not True:
        return False
    if provenance.get("transcript_verified") is not True:
        return False
    events = provenance.get("transcript_events")
    if not isinstance(events, int) or events < TRANSCRIPT_MIN_EVENTS:
        return False
    for key in ("run_id", "collector", "source_command", "transcript_file", "transcript_sha256"):
        if not str(provenance.get(key) or "").strip():
            return False
    return True


def tournament_evidence_mode(artifact: Mapping[str, object]) -> str:
    if _looks_synthetic(artifact.get("source")):
        return SELF_TEST_EVIDENCE_MODE
    candidates = _candidate_artifacts(artifact)
    if candidates and all(_has_verified_candidate_provenance(candidate) for candidate in candidates):
        return REAL_ARTIFACT_EVIDENCE_MODE
    return SELF_TEST_EVIDENCE_MODE


def _rank_key(result: TournamentCandidateResult) -> tuple[float, float, str]:
    candidate = result.record.candidate
    return (candidate.duration_seconds, candidate.cost_units, candidate.candidate_id)


def decide_tournament_case(case: TournamentCase) -> TournamentResult:
    incumbent_case = dataclasses.replace(case.base_case, incumbent=case.incumbent.candidate)
    incumbent_outcome = evaluate_candidate(incumbent_case, case.incumbent.candidate)
    candidate_results = tuple(
        TournamentCandidateResult(
            candidate,
            evaluate_candidate(
                dataclasses.replace(case.base_case, challenger=candidate.candidate),
                candidate.candidate,
            ),
        )
        for candidate in case.candidates
    )
    if not incumbent_outcome.passed:
        return TournamentResult(case, incumbent_outcome, candidate_results, None, 0, "incumbent_untrusted")
    present_sources = {candidate.source_kind for candidate in case.candidates}
    missing_sources = [source for source in REQUIRED_SOURCE_KINDS if source not in present_sources]
    if missing_sources:
        return TournamentResult(
            case,
            incumbent_outcome,
            candidate_results,
            None,
            0,
            "missing_source_kind:" + ",".join(missing_sources),
        )
    passing = [result for result in candidate_results if result.passed]
    if not passing:
        return TournamentResult(case, incumbent_outcome, candidate_results, None, 0, "no_safe_candidate")
    winner = min(passing, key=_rank_key)
    return TournamentResult(case, incumbent_outcome, candidate_results, winner, 100, "safe_candidate_selected")


def average_score(results: Sequence[TournamentResult]) -> int:
    if not results:
        return 0
    return round(sum(result.score for result in results) / len(results))


def benchmark_status(results: Sequence[TournamentResult], cases: Sequence[TournamentCase]) -> str:
    base_cases = [case.base_case for case in cases]
    if (
        len(results) >= MIN_CASES
        and average_score(results) >= TARGET_SCORE
        and all(result.passed for result in results)
        and not missing_comparison_classes(base_cases)
        and not missing_source_kinds(cases)
    ):
        return "passed"
    return "failed"


def render_scorecard(
    artifact: Mapping[str, object],
    cases: Sequence[TournamentCase],
    results: Sequence[TournamentResult],
    *,
    generated_at: datetime | None = None,
) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    lines = [
        "# CHILI Model Candidate Tournament Benchmark",
        "",
        f"- Schema: {MODEL_CANDIDATE_TOURNAMENT_SCHEMA_VERSION}",
        f"- Artifact schema: {artifact.get('schema') or 'missing'}",
        f"- Generated UTC: {generated_at.isoformat().replace('+00:00', 'Z')}",
        f"- Status: {benchmark_status(results, cases)}",
        f"- Target score: {TARGET_SCORE}",
        f"- Evidence mode: {tournament_evidence_mode(artifact)}",
        f"- Cases: {len(results)}",
        f"- Average score: {average_score(results)}/100",
        f"- Required source kinds: {', '.join(REQUIRED_SOURCE_KINDS)}",
        f"- Required frontier model targets: {frontier_model_targets_summary()}",
        f"- Missing source kinds: {', '.join(missing_source_kinds(cases)) or 'none'}",
        f"- Source kinds: {', '.join(source_kinds(cases)) or 'none'}",
        f"- Required comparison classes: {', '.join(REQUIRED_COMPARISON_CLASSES)}",
        f"- Missing comparison classes: {', '.join(missing_comparison_classes([case.base_case for case in cases])) or 'none'}",
        "- Required behavior: multi-source model outputs must be judged on scoped behavior-tested outcomes, with unsafe or regressing candidates rejected before any winner is selected.",
        "- Safety: temporary repo patch replay only; no model calls, git action in the real checkout, runtime restart, deployment, database migration, broker call, or live-trading action.",
        "",
        "| Case | Comparison Class | Winner | Score | Evidence |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for result in results:
        winner = (
            f"{result.winner.record.source_kind}/{result.winner.record.candidate.candidate_id}"
            if result.winner
            else "none"
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_cell(result.case.base_case.case_id),
                    _escape_cell(result.case.base_case.bakeoff_class),
                    _escape_cell(winner),
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


def run_tournament_benchmark(
    *,
    artifact_path: Path | None = None,
    drop_dir: Path | None = None,
    output_path: Path = DEFAULT_OUTPUT,
    write: bool = True,
    allow_partial: bool = False,
    allow_fixture: bool = False,
    require_provenance: bool = False,
    prompt_pack_path: Path | None = None,
) -> tuple[Mapping[str, object], list[TournamentCase], list[TournamentResult], str, Path]:
    if artifact_path and drop_dir:
        raise TournamentError("--artifact and --drop-dir cannot be used together")
    if artifact_path:
        artifact = load_artifact(artifact_path)
    elif drop_dir:
        artifact = build_artifact_from_drops(
            load_drops(drop_dir),
            allow_partial=allow_partial,
            allow_fixture=allow_fixture,
            require_provenance=require_provenance,
            prompt_pack_path=prompt_pack_path,
        )
    else:
        artifact = default_artifact()
    cases = tournament_cases_from_artifact(artifact)
    results = [decide_tournament_case(case) for case in cases]
    markdown = render_scorecard(artifact, cases, results)
    if write:
        write_scorecard(markdown, output_path)
    return artifact, cases, results, markdown, output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay multi-source model candidate tournaments on real CHILI bug slices."
    )
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--drop-dir", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--allow-fixture", action="store_true")
    parser.add_argument("--require-provenance", action="store_true")
    parser.add_argument("--prompt-pack", type=Path, help="Prompt pack file to verify by SHA-256.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    try:
        artifact, cases, results, markdown, output_path = run_tournament_benchmark(
            artifact_path=args.artifact,
            drop_dir=args.drop_dir,
            output_path=args.output,
            write=not args.no_write,
            allow_partial=args.allow_partial,
            allow_fixture=args.allow_fixture,
            require_provenance=args.require_provenance,
            prompt_pack_path=args.prompt_pack,
        )
    except (TournamentError, ArtifactBuildError) as exc:
        print(f"tournament error: {exc}", file=sys.stderr)
        return 2

    status = benchmark_status(results, cases)
    if args.json:
        print(
            json.dumps(
                {
                    "schema": MODEL_CANDIDATE_TOURNAMENT_SCHEMA_VERSION,
                    "artifact_schema": artifact.get("schema"),
                    "status": status,
                    "evidence_mode": tournament_evidence_mode(artifact),
                    "average_score": average_score(results),
                    "cases": len(results),
                    "source_kinds": source_kinds(cases),
                    "missing_source_kinds": missing_source_kinds(cases),
                    "output": str(output_path),
                    "results": [
                        {
                            "case_id": result.case.base_case.case_id,
                            "comparison_class": result.case.base_case.bakeoff_class,
                            "winner": (
                                result.winner.record.candidate.candidate_id
                                if result.winner
                                else None
                            ),
                            "winner_source_kind": result.winner.record.source_kind if result.winner else None,
                            "score": result.score,
                            "reason": result.reason,
                            "evidence": result.evidence,
                        }
                        for result in results
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
