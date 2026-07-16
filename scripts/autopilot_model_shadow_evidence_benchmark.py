from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_frontier_bakeoff_benchmark import _escape_cell  # noqa: E402
from scripts.autopilot_model_candidate_artifact_builder import (  # noqa: E402
    ArtifactBuildError,
    build_artifact,
    load_drops,
    render_prompt_pack,
    sha256_file,
    synthetic_drops,
)
from scripts.autopilot_model_candidate_drop_collector import (  # noqa: E402
    COLLECTOR_NAME,
    MODEL_CANDIDATE_DROP_COLLECTOR_SCHEMA_VERSION,
    collect_candidate_drops,
)
from scripts.autopilot_model_candidate_tournament_benchmark import (  # noqa: E402
    REQUIRED_SOURCE_KINDS,
    frontier_model_target_error,
    frontier_model_targets_summary,
)


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md"
MODEL_SHADOW_EVIDENCE_SCHEMA_VERSION = "chili.model-shadow-evidence-benchmark.v1"
TARGET_SCORE = 100
SELF_TEST_EVIDENCE_MODE = "self_test"
REAL_MANIFEST_EVIDENCE_MODE = "real_manifest"
PARTIAL_REAL_MANIFEST_EVIDENCE_MODE = "partial_real_manifest"
REQUIRED_CHECKS = (
    "valid_multi_source_shadow_accepts",
    "self_test_manifest_rejected",
    "synthetic_model_rejected",
    "wrong_frontier_model_rejected",
    "missing_source_rejected",
    "unverified_provenance_rejected",
    "sparse_transcript_rejected",
)
SYNTHETIC_MARKERS = ("self-test", "self_test", "synthetic", "fixture", "mock", "deterministic")
TRANSCRIPT_MIN_EVENTS = 3


class ShadowEvidenceError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class ShadowCheck:
    check_id: str
    expected_status: str
    expected_fragment: str


@dataclasses.dataclass(frozen=True)
class ShadowResult:
    check: ShadowCheck
    actual_status: str
    score: int
    evidence: str

    @property
    def passed(self) -> bool:
        return self.score >= TARGET_SCORE


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ShadowEvidenceError(f"{label} must be an object")
    return value


def _required_text(payload: Mapping[str, object], key: str, *, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ShadowEvidenceError(f"{label}.{key} is required")
    return value.strip()


def _looks_synthetic(value: object) -> bool:
    text = str(value or "").strip().lower()
    return any(marker in text for marker in SYNTHETIC_MARKERS)


def _resolve_path(raw_path: object, *, base_dir: Path, label: str) -> Path:
    text = _required_text({"path": raw_path}, "path", label=label)
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate


def _resolve_relative_file(raw_path: object, *, base_dir: Path, label: str) -> Path:
    text = _required_text({"path": raw_path}, "path", label=label)
    candidate = Path(text)
    if candidate.is_absolute():
        raise ShadowEvidenceError(f"{label} must be relative")
    resolved_base = base_dir.resolve()
    resolved = (resolved_base / candidate).resolve()
    if resolved_base not in resolved.parents and resolved != resolved_base:
        raise ShadowEvidenceError(f"{label} escapes evidence directory")
    if not resolved.is_file():
        raise ShadowEvidenceError(f"{label} does not exist: {text}")
    return resolved


def _validate_transcript_quality(
    manifest: Mapping[str, object],
    *,
    output_dir: Path,
    label: str,
) -> dict[str, object]:
    transcript = _resolve_relative_file(
        manifest.get("transcript_file"),
        base_dir=output_dir,
        label=f"{label}.transcript_file",
    )
    expected_sha = _required_text(manifest, "transcript_sha256", label=label).lower()
    if len(expected_sha) != 64 or any(character not in "0123456789abcdef" for character in expected_sha):
        raise ShadowEvidenceError(f"{label}.transcript_sha256 must be a SHA-256 hex digest")
    actual_sha = sha256_file(transcript)
    if actual_sha != expected_sha:
        raise ShadowEvidenceError(f"{label}.transcript_sha256 mismatch")
    lines = [
        line.strip()
        for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    if len(lines) < TRANSCRIPT_MIN_EVENTS:
        raise ShadowEvidenceError(
            f"{label}.transcript_file must contain at least {TRANSCRIPT_MIN_EVENTS} non-empty events"
        )
    combined = "\n".join(lines).lower()
    if not any(marker in combined for marker in ("prompt", "user", "input")):
        raise ShadowEvidenceError(f"{label}.transcript_file must include prompt or user input evidence")
    if not any(marker in combined for marker in ("assistant", "response", "completion", "output", "patch")):
        raise ShadowEvidenceError(f"{label}.transcript_file must include model response evidence")
    return {
        "transcript_file": transcript.name,
        "transcript_events": len(lines),
        "transcript_sha256": actual_sha,
    }


def load_manifest(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ShadowEvidenceError(f"{path}: invalid JSON: {exc}") from exc
    payload = dict(_as_mapping(raw, label=str(path)))
    payload["_manifest_path"] = str(path)
    return payload


def load_manifests(paths: Sequence[Path], manifest_dir: Path | None = None) -> list[dict[str, object]]:
    resolved: list[Path] = []
    if manifest_dir:
        if not manifest_dir.is_dir():
            raise ShadowEvidenceError(f"manifest directory does not exist: {manifest_dir}")
        resolved.extend(sorted(path for path in manifest_dir.rglob("*.json") if path.is_file()))
    resolved.extend(paths)
    if not resolved:
        raise ShadowEvidenceError("at least one manifest is required")
    return [load_manifest(path) for path in resolved]


def validate_shadow_manifests(
    manifests: Sequence[Mapping[str, object]],
    *,
    allow_partial: bool = False,
) -> dict[str, object]:
    if not manifests and not allow_partial:
        raise ShadowEvidenceError("at least one manifest is required")
    seen_sources: set[str] = set()
    comparison_classes: set[str] = set()
    total_cases = 0
    for index, manifest in enumerate(manifests, start=1):
        label = f"manifest[{index}]"
        manifest_path = Path(str(manifest.get("_manifest_path") or "."))
        base_dir = manifest_path.parent if manifest_path != Path(".") else Path(".")
        schema = manifest.get("schema")
        if schema != MODEL_CANDIDATE_DROP_COLLECTOR_SCHEMA_VERSION:
            raise ShadowEvidenceError(
                f"{label}.schema is {schema or 'missing'} instead of "
                f"{MODEL_CANDIDATE_DROP_COLLECTOR_SCHEMA_VERSION}"
            )
        source_kind = _required_text(manifest, "source_kind", label=label)
        if source_kind not in REQUIRED_SOURCE_KINDS:
            raise ShadowEvidenceError(
                f"{label}.source_kind must be one of {', '.join(REQUIRED_SOURCE_KINDS)}"
            )
        if source_kind in seen_sources:
            raise ShadowEvidenceError(f"duplicate source_kind: {source_kind}")
        seen_sources.add(source_kind)
        if manifest.get("collector") != COLLECTOR_NAME:
            raise ShadowEvidenceError(f"{label}.collector is not {COLLECTOR_NAME}")
        if manifest.get("validated_with_provenance") is not True:
            raise ShadowEvidenceError(f"{label}.validated_with_provenance must be true")
        model_name = ""
        for key in ("run_id", "model_name", "source_command"):
            text = _required_text(manifest, key, label=label)
            if _looks_synthetic(text):
                raise ShadowEvidenceError(f"{label}.{key} looks synthetic: {text}")
            if key == "model_name":
                model_name = text
        target_error = frontier_model_target_error(source_kind, model_name, label=label)
        if target_error:
            raise ShadowEvidenceError(target_error)
        output_dir = _resolve_path(manifest.get("output_dir"), base_dir=base_dir, label=f"{label}.output_dir")
        if not output_dir.is_dir():
            raise ShadowEvidenceError(f"{label}.output_dir does not exist: {output_dir}")
        _validate_transcript_quality(manifest, output_dir=output_dir, label=label)
        drops = load_drops(output_dir)
        try:
            artifact = build_artifact(
                drops,
                allow_partial=True,
                require_provenance=True,
            )
        except ArtifactBuildError as exc:
            raise ShadowEvidenceError(str(exc)) from exc
        entries = artifact.get("entries") if isinstance(artifact.get("entries"), list) else []
        total_cases += len(entries)
        for entry in entries:
            if isinstance(entry, Mapping):
                comparison_class = entry.get("comparison_class")
                if isinstance(comparison_class, str) and comparison_class.strip():
                    comparison_classes.add(comparison_class.strip())
    missing_sources = [source for source in REQUIRED_SOURCE_KINDS if source not in seen_sources]
    if missing_sources and not allow_partial:
        raise ShadowEvidenceError("missing source kinds: " + ", ".join(missing_sources))
    return {
        "schema": MODEL_SHADOW_EVIDENCE_SCHEMA_VERSION,
        "source_kinds": sorted(seen_sources),
        "missing_source_kinds": missing_sources,
        "cases": total_cases,
        "comparison_classes": sorted(comparison_classes),
        "manifests": len(manifests),
        "validated_shadow_evidence": not missing_sources,
        "allow_partial": allow_partial,
    }


def default_checks() -> list[ShadowCheck]:
    return [
        ShadowCheck("valid_multi_source_shadow_accepts", "accepted", "validated_shadow_evidence=True"),
        ShadowCheck("self_test_manifest_rejected", "rejected", "looks synthetic"),
        ShadowCheck("synthetic_model_rejected", "rejected", "looks synthetic"),
        ShadowCheck("wrong_frontier_model_rejected", "rejected", "required frontier target"),
        ShadowCheck("missing_source_rejected", "rejected", "missing source kinds"),
        ShadowCheck("unverified_provenance_rejected", "rejected", "validated_with_provenance must be true"),
        ShadowCheck("sparse_transcript_rejected", "rejected", "at least"),
    ]


def _write_raw_drop(raw_dir: Path, *, source_kind: str = "codex", model_name: str = "codex-collected-candidate") -> dict[str, object]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    drop = dict(synthetic_drops()[0])
    drop["source_kind"] = source_kind
    drop["model_name"] = model_name
    patch_path = raw_dir / "candidate.patch"
    patch_path.write_text(str(drop.pop("patch")), encoding="utf-8")
    drop["patch_file"] = patch_path.name
    payload = {
        key: value
        for key, value in drop.items()
        if not str(key).startswith("_") and key != "provenance"
    }
    (raw_dir / "drop.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _collect_manifest(
    root: Path,
    *,
    source_kind: str,
    run_id: str,
    model_name: str,
    source_command: str,
) -> dict[str, object]:
    raw_dir = root / source_kind / "raw"
    output_dir = root / source_kind / "collected"
    _write_raw_drop(raw_dir, source_kind=source_kind, model_name=model_name)
    prompt_pack = root / source_kind / "prompt_pack.md"
    prompt_pack.write_text(
        render_prompt_pack(source_kind=source_kind, model_name=model_name),
        encoding="utf-8",
    )
    transcript = root / source_kind / "run.transcript.jsonl"
    transcript_events = [
        {
            "event": "external_session_started",
            "run_id": run_id,
            "source_kind": source_kind,
            "model_name": model_name,
            "collected_at": _utc_now(),
        },
        {
            "event": "prompt_sent",
            "role": "user",
            "content": "Run the CHILI model candidate prompt pack and emit candidate drops.",
        },
        {
            "event": "assistant_response",
            "role": "assistant",
            "content": "Produced candidate patch drops for replay validation.",
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in transcript_events) + "\n",
        encoding="utf-8",
    )
    _, manifest = collect_candidate_drops(
        input_dir=raw_dir,
        output_dir=output_dir,
        prompt_pack_path=prompt_pack,
        transcript_path=transcript,
        source_kind=source_kind,
        model_name=model_name,
        run_id=run_id,
        source_command=source_command,
        allow_partial=True,
    )
    manifest["_manifest_path"] = str(root / source_kind / "manifest.json")
    return manifest


def _valid_manifests(root: Path) -> list[dict[str, object]]:
    return [
        _collect_manifest(
            root,
            source_kind="codex",
            run_id="frontier-eval-20260602-codex",
            model_name="codex-gpt-5.6-sol-shadow",
            source_command="codex cli recorded transcript",
        ),
        _collect_manifest(
            root,
            source_kind="claude",
            run_id="frontier-eval-20260602-claude",
            model_name="claude-fable-5-shadow",
            source_command="claude code recorded transcript",
        ),
        _collect_manifest(
            root,
            source_kind="local_model",
            run_id="frontier-eval-20260602-local",
            model_name="local-coder-shadow",
            source_command="local coder recorded transcript",
        ),
    ]


def evaluate_check(check: ShadowCheck) -> ShadowResult:
    return evaluate_check_with_manifests(check)


def evaluate_check_with_manifests(
    check: ShadowCheck,
    *,
    valid_manifests: Sequence[Mapping[str, object]] | None = None,
    allow_partial: bool = True,
) -> ShadowResult:
    with tempfile.TemporaryDirectory(prefix="chili_model_shadow_evidence_") as raw_root:
        root = Path(raw_root)
        if check.check_id == "valid_multi_source_shadow_accepts" and valid_manifests is not None:
            manifests = [dict(manifest) for manifest in valid_manifests]
        else:
            manifests = _valid_manifests(root)
        if check.check_id == "self_test_manifest_rejected":
            manifests[0] = dict(manifests[0])
            manifests[0]["run_id"] = "collector-self-test"
        elif check.check_id == "synthetic_model_rejected":
            manifests[0] = dict(manifests[0])
            manifests[0]["model_name"] = "synthetic-codex-fixture"
        elif check.check_id == "wrong_frontier_model_rejected":
            manifests[1] = dict(manifests[1])
            manifests[1]["model_name"] = "claude-sonnet-shadow"
        elif check.check_id == "missing_source_rejected":
            manifests = [manifest for manifest in manifests if manifest["source_kind"] != "local_model"]
        elif check.check_id == "unverified_provenance_rejected":
            manifests[0] = dict(manifests[0])
            manifests[0]["validated_with_provenance"] = False
        elif check.check_id == "sparse_transcript_rejected":
            manifests[0] = dict(manifests[0])
            output_dir = _resolve_path(
                manifests[0].get("output_dir"),
                base_dir=Path(str(manifests[0].get("_manifest_path") or ".")).parent,
                label="manifest[1].output_dir",
            )
            transcript_path = _resolve_relative_file(
                manifests[0].get("transcript_file"),
                base_dir=output_dir,
                label="manifest[1].transcript_file",
            )
            transcript_path.write_text(
                json.dumps({"event": "metadata_only", "run_id": manifests[0].get("run_id")}, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifests[0]["transcript_sha256"] = sha256_file(transcript_path)
            for drop_path in output_dir.rglob("*.json"):
                payload = json.loads(drop_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and isinstance(payload.get("provenance"), dict):
                    payload["provenance"]["transcript_sha256"] = manifests[0]["transcript_sha256"]
                    drop_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            summary = validate_shadow_manifests(
                manifests,
                allow_partial=allow_partial and check.check_id != "missing_source_rejected",
            )
            actual_status = "accepted"
            evidence = (
                f"validated_shadow_evidence={summary['validated_shadow_evidence']}; "
                f"sources={','.join(summary['source_kinds'])}; "
                f"manifests={summary['manifests']}; cases={summary['cases']}"
            )
        except ShadowEvidenceError as exc:
            actual_status = "rejected"
            evidence = str(exc)
    passed = actual_status == check.expected_status and check.expected_fragment in evidence
    return ShadowResult(
        check=check,
        actual_status=actual_status,
        score=TARGET_SCORE if passed else 0,
        evidence=evidence,
    )


def average_score(results: Sequence[ShadowResult]) -> int:
    if not results:
        return 0
    return round(sum(result.score for result in results) / len(results))


def missing_checks(results: Sequence[ShadowResult]) -> list[str]:
    covered = {result.check.check_id for result in results}
    return [check for check in REQUIRED_CHECKS if check not in covered]


def benchmark_status(results: Sequence[ShadowResult]) -> str:
    if (
        len(results) >= len(REQUIRED_CHECKS)
        and average_score(results) >= TARGET_SCORE
        and all(result.passed for result in results)
        and not missing_checks(results)
    ):
        return "passed"
    return "failed"


def render_scorecard(
    results: Sequence[ShadowResult],
    *,
    evidence_mode: str = SELF_TEST_EVIDENCE_MODE,
    evidence_summary: Mapping[str, object] | None = None,
    generated_at: datetime | None = None,
) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    source_kinds = REQUIRED_SOURCE_KINDS
    missing_source_kinds: list[str] = []
    manifests: object = "fixture"
    cases: object = "fixture"
    if evidence_summary is not None:
        source_kinds = tuple(str(item) for item in evidence_summary.get("source_kinds", ()))
        missing_source_kinds = [
            item for item in REQUIRED_SOURCE_KINDS if item not in set(source_kinds)
        ]
        manifests = evidence_summary.get("manifests", 0)
        cases = evidence_summary.get("cases", 0)
    lines = [
        "# CHILI Model Shadow Evidence Benchmark",
        "",
        f"- Schema: {MODEL_SHADOW_EVIDENCE_SCHEMA_VERSION}",
        f"- Generated UTC: {generated_at.isoformat().replace('+00:00', 'Z')}",
        f"- Status: {benchmark_status(results)}",
        f"- Target score: {TARGET_SCORE}",
        f"- Evidence mode: {evidence_mode}",
        f"- Checks: {len(results)}",
        f"- Average score: {average_score(results)}/100",
        f"- Required source kinds: {', '.join(REQUIRED_SOURCE_KINDS)}",
        f"- Required frontier model targets: {frontier_model_targets_summary()}",
        f"- Source kinds: {', '.join(source_kinds)}",
        f"- Missing source kinds: {', '.join(missing_source_kinds) or 'none'}",
        f"- Manifests: {manifests}",
        f"- Cases: {cases}",
        f"- Required checks: {', '.join(REQUIRED_CHECKS)}",
        f"- Missing checks: {', '.join(missing_checks(results)) or 'none'}",
        "- Required behavior: synthetic, self-test, incomplete, or unverified model-run bundles must not count as real frontier shadow evidence.",
        "- Safety: deterministic manifest/hash validation only; no model calls, git action, runtime restart, deployment, database migration, broker call, or live-trading action.",
        "",
        "| Check | Expected | Actual | Score | Evidence |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for result in results:
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_cell(result.check.check_id),
                    _escape_cell(result.check.expected_status),
                    _escape_cell(result.actual_status),
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


def run_shadow_evidence_benchmark(
    *,
    output_path: Path = DEFAULT_OUTPUT,
    write: bool = True,
) -> tuple[list[ShadowResult], str, Path]:
    results = [evaluate_check(check) for check in default_checks()]
    markdown = render_scorecard(results, evidence_mode=SELF_TEST_EVIDENCE_MODE)
    if write:
        write_scorecard(markdown, output_path)
    return results, markdown, output_path


def run_shadow_evidence_validation(
    manifests: Sequence[Mapping[str, object]],
    *,
    output_path: Path = DEFAULT_OUTPUT,
    write: bool = True,
    allow_partial: bool = False,
) -> tuple[list[ShadowResult], str, Path, dict[str, object]]:
    summary = validate_shadow_manifests(manifests, allow_partial=allow_partial)
    if allow_partial and not manifests:
        missing = ", ".join(summary.get("missing_source_kinds") or REQUIRED_SOURCE_KINDS)
        check = default_checks()[0]
        results = [
            ShadowResult(
                check=check,
                actual_status="rejected",
                score=0,
                evidence=f"missing source kinds: {missing}; manifests=0; cases=0",
            )
        ]
    else:
        results = [
            evaluate_check_with_manifests(
                check,
                valid_manifests=manifests,
                allow_partial=True,
            )
            for check in default_checks()
        ]
    evidence_mode = (
        PARTIAL_REAL_MANIFEST_EVIDENCE_MODE
        if summary.get("missing_source_kinds")
        else REAL_MANIFEST_EVIDENCE_MODE
    )
    markdown = render_scorecard(
        results,
        evidence_mode=evidence_mode,
        evidence_summary=summary,
    )
    if write:
        write_scorecard(markdown, output_path)
    return results, markdown, output_path, summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate real-model shadow evidence manifests or replay the shadow gate benchmark."
    )
    parser.add_argument("--manifest", type=Path, action="append", default=[])
    parser.add_argument("--manifest-dir", type=Path)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.self_test:
            results, markdown, output_path = run_shadow_evidence_benchmark(
                output_path=args.output,
                write=not args.no_write,
            )
            status = benchmark_status(results)
            summary = {
                "schema": MODEL_SHADOW_EVIDENCE_SCHEMA_VERSION,
                "status": status,
                "evidence_mode": SELF_TEST_EVIDENCE_MODE,
                "average_score": average_score(results),
                "checks": len(results),
                "output": str(output_path),
            }
        else:
            manifests = load_manifests(args.manifest, manifest_dir=args.manifest_dir)
            results, markdown, output_path, summary = run_shadow_evidence_validation(
                manifests,
                output_path=args.output,
                write=not args.no_write,
                allow_partial=args.allow_partial,
            )
            status = benchmark_status(results)
            summary = dict(summary)
            summary.update(
                {
                    "status": status,
                    "evidence_mode": (
                        PARTIAL_REAL_MANIFEST_EVIDENCE_MODE
                        if summary.get("missing_source_kinds")
                        else REAL_MANIFEST_EVIDENCE_MODE
                    ),
                    "average_score": average_score(results),
                    "checks": len(results),
                    "output": str(output_path),
                }
            )
    except (ShadowEvidenceError, ArtifactBuildError) as exc:
        print(f"shadow evidence error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    elif args.self_test:
        print(markdown)
        if not args.no_write:
            print(f"Wrote {output_path}")
    else:
        print(
            "Validated model shadow evidence: "
            f"{summary['manifests']} manifests; sources={', '.join(summary['source_kinds'])}; "
            f"cases={summary['cases']}; wrote={output_path if not args.no_write else 'no'}"
        )
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
