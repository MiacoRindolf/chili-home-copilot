from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_frontier_bakeoff_benchmark import _escape_cell  # noqa: E402
from scripts.autopilot_model_candidate_drop_collector import (  # noqa: E402
    DropCollectionError,
    collect_candidate_drops,
    write_manifest,
)
from scripts.autopilot_model_candidate_tournament_benchmark import (  # noqa: E402
    REAL_ARTIFACT_EVIDENCE_MODE,
    TournamentError,
    average_score as tournament_average_score,
    benchmark_status as tournament_status,
    run_tournament_benchmark,
    source_kinds as tournament_source_kinds,
    tournament_evidence_mode,
)
from scripts.autopilot_model_shadow_evidence_benchmark import (  # noqa: E402
    PARTIAL_REAL_MANIFEST_EVIDENCE_MODE,
    REAL_MANIFEST_EVIDENCE_MODE,
    REQUIRED_SOURCE_KINDS,
    ShadowEvidenceError,
    average_score as shadow_average_score,
    benchmark_status as shadow_status,
    load_manifests,
    run_shadow_evidence_validation,
)
from scripts.autopilot_model_candidate_artifact_builder import ArtifactBuildError  # noqa: E402


DEFAULT_OUTPUT_ROOT = REPO_ROOT / "project_ws" / "AgentOps" / "frontier_model_evidence_intake"
DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "FRONTIER_MODEL_EVIDENCE_INTAKE.md"
FRONTIER_MODEL_EVIDENCE_INTAKE_SCHEMA_VERSION = "chili.frontier-model-evidence-intake.v1"
SOURCE_METADATA_FILE = "metadata.json"
SOURCE_RAW_DIR = "raw"
SOURCE_PROMPT_PACK_FILE = "prompt_pack.md"
SOURCE_TRANSCRIPT_FILE = "transcript.jsonl"
FRONTIER_SOURCE_COLLECTION_PACKET_COMMAND = (
    "python scripts/autopilot_frontier_source_collection_packet.py "
    "--source-kind {source_kind} --json"
)
FRONTIER_SOURCE_EVIDENCE_RECORD_ALL_CASES_COMMAND = (
    "python scripts/autopilot_frontier_source_evidence_recorder.py "
    "--source-kind {source_kind} --all-cases "
    "--response {response_file} "
    "--run-id <real-{source_kind}-run-id> "
    "--source-command <exact-{source_kind}-command-or-session-export> --json"
)
FRONTIER_SOURCE_EVIDENCE_RECORD_SINGLE_CASE_COMMAND = (
    "python scripts/autopilot_frontier_source_evidence_recorder.py "
    "--source-kind {source_kind} --case-id {case_id} "
    "--response {response_file} "
    "--run-id <real-{source_kind}-run-id> "
    "--source-command <exact-{source_kind}-command-or-session-export> --json"
)
FRONTIER_MODEL_EVIDENCE_INTAKE_VALIDATE_COMMAND = (
    "python scripts/autopilot_frontier_model_evidence_intake.py "
    "--input-root {input_root} --allow-partial --json --no-write"
)
FRONTIER_MODEL_EVIDENCE_INTAKE_PUBLISH_COMMAND = (
    "python scripts/autopilot_frontier_model_evidence_intake.py "
    "--input-root {input_root} --publish-scorecards --json"
)
FRONTIER_EVIDENCE_PREFLIGHT_LIVE = (
    REPO_ROOT / "project_ws" / "AgentOps" / "FRONTIER_EVIDENCE_PREFLIGHT_LIVE.md"
)
FRONTIER_EVIDENCE_PREFLIGHT = (
    REPO_ROOT / "project_ws" / "AgentOps" / "FRONTIER_EVIDENCE_PREFLIGHT.md"
)
FRONTIER_SOURCE_AVAILABILITY = (
    REPO_ROOT / "project_ws" / "AgentOps" / "FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS.md"
)
FRONTIER_SOURCE_COLLECTION_PACKETS = (
    REPO_ROOT / "project_ws" / "AgentOps" / "FRONTIER_SOURCE_COLLECTION_PACKETS.md"
)
DEFAULT_SINGLE_CASE_FALLBACK_CASE_ID = "real-chili-preflight-candidate-wins"


class FrontierModelEvidenceIntakeError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class SourceBundle:
    source_kind: str
    source_dir: Path
    raw_dir: Path
    prompt_pack: Path
    transcript: Path
    model_name: str
    run_id: str
    source_command: str


@dataclasses.dataclass(frozen=True)
class SourceReadiness:
    source_kind: str
    status: str
    path: str
    raw_drop_count: int
    present_files: tuple[str, ...]
    missing_files: tuple[str, ...]
    next_action: str
    preflight_report: str = ""
    preflight_recovery_action: str = ""
    preflight_recovery_response_staging_file: str = ""
    preflight_recovery_dry_run_command: str = ""
    preflight_recovery_all_cases_command: str = ""
    preflight_recovery_single_case_fallback: str = ""
    preflight_recovery_boundary: str = ""
    preflight_recovery_validation_command: str = ""
    preflight_recovery_publish_command: str = ""
    availability_report: str = ""
    availability_probe_status: str = ""
    availability_blocker: str = ""
    availability_credential_status: str = ""
    availability_recovery_action: str = ""
    collection_packet_summary: str = ""
    source_runner_command: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise FrontierModelEvidenceIntakeError(f"{label} must be an object")
    return value


def _required_text(payload: Mapping[str, object], key: str, *, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FrontierModelEvidenceIntakeError(f"{label}.{key} is required")
    return value.strip()


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FrontierModelEvidenceIntakeError(f"{path}: invalid JSON: {exc}") from exc
    return _as_mapping(raw, label=str(path))


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _command_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _validation_command(input_root: Path) -> str:
    return FRONTIER_MODEL_EVIDENCE_INTAKE_VALIDATE_COMMAND.format(
        input_root=_command_path(input_root)
    )


def _publish_command(input_root: Path) -> str:
    return FRONTIER_MODEL_EVIDENCE_INTAKE_PUBLISH_COMMAND.format(
        input_root=_command_path(input_root)
    )


def _collection_packet_dir(input_root: Path) -> Path:
    return input_root.parent / "collection_packets"


def _all_cases_response_staging_file(input_root: Path, source_kind: str) -> Path:
    return _collection_packet_dir(input_root) / f"{source_kind}_all_cases_response.txt"


def _single_case_response_staging_file(input_root: Path, source_kind: str) -> Path:
    return _collection_packet_dir(input_root) / f"{source_kind}_single_case_response.txt"


def _case_id_from_command(command: str) -> str:
    match = re.search(r"--case-id\s+([^\s]+)", command)
    if match:
        return match.group(1).strip()
    return DEFAULT_SINGLE_CASE_FALLBACK_CASE_ID


def _record_all_cases_command(source_kind: str, input_root: Path, *, no_write: bool = False) -> str:
    command = FRONTIER_SOURCE_EVIDENCE_RECORD_ALL_CASES_COMMAND.format(
        source_kind=source_kind,
        response_file=_command_path(_all_cases_response_staging_file(input_root, source_kind)),
    )
    if no_write:
        command += " --no-write"
    return command


def _record_single_case_command(source_kind: str, input_root: Path, *, case_id: str) -> str:
    return FRONTIER_SOURCE_EVIDENCE_RECORD_SINGLE_CASE_COMMAND.format(
        source_kind=source_kind,
        case_id=case_id,
        response_file=_command_path(_single_case_response_staging_file(input_root, source_kind)),
    )


def _source_runner_intro(source_runner_command: str) -> str:
    command = str(source_runner_command or "").strip()
    if not command or command.lower() == "none":
        return ""
    return f"Automated source runner: {command}; if it passes, validate intake with "


def _source_next_action(
    source_kind: str,
    input_root: Path,
    *,
    source_runner_command: str = "",
) -> str:
    runner_intro = _source_runner_intro(source_runner_command)
    if runner_intro:
        return (
            runner_intro
            + _validation_command(input_root)
            + "; then publish only when all required sources are ready: "
            + _publish_command(input_root)
            + ". Manual fallback: build/use the collection packet with "
            + FRONTIER_SOURCE_COLLECTION_PACKET_COMMAND.format(source_kind=source_kind)
            + "; then import all cases with: "
            + _record_all_cases_command(source_kind, input_root)
        )
    return (
        FRONTIER_SOURCE_COLLECTION_PACKET_COMMAND.format(source_kind=source_kind)
        + "; then import all cases with: "
        + _record_all_cases_command(source_kind, input_root)
    )


def _default_preflight_report(input_root: Path) -> Path | None:
    try:
        input_root.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        return None
    candidates = [
        path
        for path in (FRONTIER_EVIDENCE_PREFLIGHT_LIVE, FRONTIER_EVIDENCE_PREFLIGHT)
        if path.is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _default_availability_report(input_root: Path) -> Path | None:
    try:
        input_root.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        return None
    return FRONTIER_SOURCE_AVAILABILITY if FRONTIER_SOURCE_AVAILABILITY.is_file() else None


def _default_collection_packet_summary(input_root: Path) -> Path | None:
    try:
        input_root.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        return None
    return (
        FRONTIER_SOURCE_COLLECTION_PACKETS
        if FRONTIER_SOURCE_COLLECTION_PACKETS.is_file()
        else None
    )


def _split_markdown_row(line: str) -> list[str]:
    clean = line.strip()
    if not clean.startswith("|") or not clean.endswith("|"):
        return []
    cells = clean.strip("|").split("|")
    return [cell.strip().replace("\\|", "|") for cell in cells]


def _preflight_recovery_routes(
    preflight_report: Path | None,
    *,
    input_root: Path,
) -> dict[str, Mapping[str, str]]:
    if preflight_report is None or not preflight_report.is_file():
        return {}
    lines = preflight_report.read_text(encoding="utf-8", errors="replace").splitlines()
    in_section = False
    routes: dict[str, Mapping[str, str]] = {}
    for line in lines:
        stripped = line.strip()
        if stripped == "## Recovery Routes":
            in_section = True
            continue
        if in_section and stripped.startswith("## "):
            break
        if not in_section or not stripped.startswith("|"):
            continue
        cells = _split_markdown_row(stripped)
        if len(cells) < 6 or cells[0].lower() in {"source", "---"}:
            continue
        source_kind = cells[0].strip()
        if not source_kind:
            continue
        case_id = _case_id_from_command(cells[4])
        response_staging_file = _all_cases_response_staging_file(input_root, source_kind)
        routes[source_kind] = {
            "source_kind": source_kind,
            "blocker": cells[1],
            "action": cells[2],
            "response_staging_file": str(response_staging_file),
            "dry_run_all_cases_command": _record_all_cases_command(
                source_kind,
                input_root,
                no_write=True,
            ),
            "all_cases_command": _record_all_cases_command(source_kind, input_root),
            "single_case_fallback": _record_single_case_command(
                source_kind,
                input_root,
                case_id=case_id,
            ),
            "boundary": cells[5],
            "preflight_report": str(preflight_report),
            "validation_command": _validation_command(input_root),
            "publish_command": _publish_command(input_root),
        }
    return routes


def _availability_label(source_kind: str) -> str:
    return source_kind.replace("_", " ").title()


def _source_availability_routes(
    availability_report: Path | None,
) -> dict[str, Mapping[str, str]]:
    if availability_report is None or not availability_report.is_file():
        return {}
    lines = availability_report.read_text(encoding="utf-8", errors="replace").splitlines()
    routes: dict[str, Mapping[str, str]] = {}
    for source_kind in REQUIRED_SOURCE_KINDS:
        label = _availability_label(source_kind)
        fields: dict[str, str] = {"source_kind": source_kind, "availability_report": str(availability_report)}
        prefixes = {
            f"- {label} probe status: ": "probe_status",
            f"- {label} blocker: ": "blocker",
            f"- {label} credential status: ": "credential_status",
            f"- {label} next action: ": "action",
        }
        for line in lines:
            for prefix, field in prefixes.items():
                if line.startswith(prefix):
                    fields[field] = line[len(prefix) :].strip()
        blocker = fields.get("blocker", "")
        if blocker and blocker != "none":
            routes[source_kind] = fields
    return routes


def _collection_packet_routes(
    collection_packet_summary: Path | None,
) -> dict[str, Mapping[str, str]]:
    if collection_packet_summary is None or not collection_packet_summary.is_file():
        return {}
    routes: dict[str, Mapping[str, str]] = {}
    lines = collection_packet_summary.read_text(encoding="utf-8", errors="replace").splitlines()
    headers: list[str] = []
    for line in lines:
        cells = _split_markdown_row(line)
        if not cells:
            continue
        lowered = [cell.strip().lower() for cell in cells]
        if lowered and lowered[0] == "source":
            headers = lowered
            continue
        if not headers or lowered[0] in {"---", "source"}:
            continue
        if len(cells) < len(headers):
            continue
        row = {headers[index]: cells[index] for index in range(len(headers))}
        source_kind = (row.get("source") or "").strip()
        if not source_kind:
            continue
        source_runner_command = (row.get("source runner") or "").strip()
        if source_runner_command and source_runner_command.lower() != "none":
            routes[source_kind] = {
                "source_kind": source_kind,
                "source_runner_command": source_runner_command,
                "collection_packet_summary": str(collection_packet_summary),
            }
    return routes


def _source_next_action_with_recovery(
    source_kind: str,
    fallback: str,
    route: Mapping[str, str] | None,
    *,
    validation_command: str,
    publish_command: str,
) -> str:
    if not route:
        return fallback
    action = route.get("action") or f"Import saved {source_kind} response"
    response_staging_file = route.get("response_staging_file") or ""
    dry_run = route.get("dry_run_all_cases_command") or ""
    all_cases = route.get("all_cases_command") or fallback
    single_case = route.get("single_case_fallback") or ""
    boundary = route.get("boundary") or "evidence import only"
    parts = [
        f"Preflight recovery: {action}.",
    ]
    if response_staging_file:
        parts.append(f"Save all-cases response to: {response_staging_file}.")
    if dry_run:
        parts.append(f"Dry-run import first: {dry_run}.")
    parts.append(f"All-cases import: {all_cases}.")
    if single_case:
        parts.append(f"Single-case fallback: {single_case}.")
    parts.append(f"After import validation: {validation_command}.")
    parts.append(f"Publish only when all sources are ready: {publish_command}.")
    parts.append(f"Boundary: {boundary}.")
    return " ".join(parts)


def _source_next_action_with_availability(
    source_kind: str,
    fallback: str,
    route: Mapping[str, str] | None,
) -> str:
    if not route:
        return fallback
    action = (route.get("action") or f"Resolve {source_kind} source availability").rstrip(".")
    blocker = route.get("blocker") or "source_unavailable"
    credential_status = route.get("credential_status") or "unknown"
    return (
        f"Availability recovery: {action}. Current blocker: {blocker}; "
        f"credential status: {credential_status}. Then build/use the collection packet "
        f"and import evidence: {fallback}"
    )


def _source_next_action_with_runner(
    source_kind: str,
    fallback: str,
    route: Mapping[str, str] | None,
    *,
    input_root: Path,
) -> str:
    if not route:
        return fallback
    command = route.get("source_runner_command") or ""
    runner_intro = _source_runner_intro(command)
    if not runner_intro:
        return fallback
    return (
        runner_intro
        + _validation_command(input_root)
        + "; then publish only when all required sources are ready: "
        + _publish_command(input_root)
        + ". Manual fallback: "
        + fallback
    )


def _enrich_source_readiness_with_recovery(
    readiness: Sequence[SourceReadiness],
    recovery_routes: Mapping[str, Mapping[str, str]],
    *,
    input_root: Path,
) -> list[SourceReadiness]:
    enriched: list[SourceReadiness] = []
    validation_command = _validation_command(input_root)
    publish_command = _publish_command(input_root)
    for item in readiness:
        route = recovery_routes.get(item.source_kind)
        if item.status == "ready" or not route:
            enriched.append(item)
            continue
        enriched.append(
            dataclasses.replace(
                item,
                next_action=_source_next_action_with_recovery(
                    item.source_kind,
                    item.next_action,
                    route,
                    validation_command=validation_command,
                    publish_command=publish_command,
                ),
                preflight_report=route.get("preflight_report", ""),
                preflight_recovery_action=route.get("action", ""),
                preflight_recovery_response_staging_file=route.get(
                    "response_staging_file",
                    "",
                ),
                preflight_recovery_dry_run_command=route.get(
                    "dry_run_all_cases_command",
                    "",
                ),
                preflight_recovery_all_cases_command=route.get(
                    "all_cases_command",
                    "",
                ),
                preflight_recovery_single_case_fallback=route.get(
                    "single_case_fallback",
                    "",
                ),
                preflight_recovery_boundary=route.get("boundary", ""),
                preflight_recovery_validation_command=validation_command,
                preflight_recovery_publish_command=publish_command,
            )
        )
    return enriched


def _enrich_source_readiness_with_availability(
    readiness: Sequence[SourceReadiness],
    availability_routes: Mapping[str, Mapping[str, str]],
) -> list[SourceReadiness]:
    enriched: list[SourceReadiness] = []
    for item in readiness:
        route = availability_routes.get(item.source_kind)
        if item.status == "ready" or not route:
            enriched.append(item)
            continue
        enriched.append(
            dataclasses.replace(
                item,
                next_action=_source_next_action_with_availability(
                    item.source_kind,
                    item.next_action,
                    route,
                ),
                availability_report=route.get("availability_report", ""),
                availability_probe_status=route.get("probe_status", ""),
                availability_blocker=route.get("blocker", ""),
                availability_credential_status=route.get("credential_status", ""),
                availability_recovery_action=route.get("action", ""),
            )
        )
    return enriched


def _enrich_source_readiness_with_collection_packets(
    readiness: Sequence[SourceReadiness],
    collection_packet_routes: Mapping[str, Mapping[str, str]],
    *,
    input_root: Path,
) -> list[SourceReadiness]:
    enriched: list[SourceReadiness] = []
    for item in readiness:
        route = collection_packet_routes.get(item.source_kind)
        if item.status == "ready" or not route:
            enriched.append(item)
            continue
        enriched.append(
            dataclasses.replace(
                item,
                next_action=_source_next_action_with_runner(
                    item.source_kind,
                    item.next_action,
                    route,
                    input_root=input_root,
                ),
                collection_packet_summary=route.get("collection_packet_summary", ""),
                source_runner_command=route.get("source_runner_command", ""),
            )
        )
    return enriched


def inspect_source_readiness(input_root: Path, source_kind: str) -> SourceReadiness:
    input_root = input_root.resolve()
    source_dir = input_root / source_kind
    required_paths = (
        source_dir / SOURCE_METADATA_FILE,
        source_dir / SOURCE_PROMPT_PACK_FILE,
        source_dir / SOURCE_TRANSCRIPT_FILE,
    )
    present_files: list[str] = []
    missing_files: list[str] = []
    raw_drop_count = 0
    raw_dir = source_dir / SOURCE_RAW_DIR
    if source_dir.is_dir():
        for path in required_paths:
            if path.is_file():
                present_files.append(_rel(path, input_root))
            else:
                missing_files.append(_rel(path, input_root))
        if raw_dir.is_dir():
            raw_drop_count = sum(1 for path in raw_dir.glob("*.json") if path.is_file())
            if raw_drop_count:
                present_files.append(_rel(raw_dir, input_root) + "/*.json")
            else:
                missing_files.append(_rel(raw_dir, input_root) + "/*.json")
        else:
            missing_files.append(_rel(raw_dir, input_root) + "/*.json")
    else:
        missing_files.extend(
            [
                _rel(path, input_root)
                for path in (*required_paths, raw_dir / "*.json")
            ]
        )

    ready = source_dir.is_dir() and not missing_files and raw_drop_count > 0
    status = "ready" if ready else "partial" if source_dir.exists() else "missing"
    return SourceReadiness(
        source_kind=source_kind,
        status=status,
        path=_rel(source_dir, input_root),
        raw_drop_count=raw_drop_count,
        present_files=tuple(present_files),
        missing_files=tuple(missing_files),
        next_action="none" if ready else _source_next_action(source_kind, input_root),
    )


def inspect_all_source_readiness(input_root: Path) -> list[SourceReadiness]:
    return [
        inspect_source_readiness(input_root, source_kind)
        for source_kind in REQUIRED_SOURCE_KINDS
    ]


def discover_source_bundle(input_root: Path, source_kind: str) -> SourceBundle:
    source_dir = input_root / source_kind
    if not source_dir.is_dir():
        raise FrontierModelEvidenceIntakeError(f"missing source directory: {source_dir}")
    metadata_path = source_dir / SOURCE_METADATA_FILE
    if not metadata_path.is_file():
        raise FrontierModelEvidenceIntakeError(f"missing source metadata: {metadata_path}")
    metadata = _read_json(metadata_path)
    raw_dir = source_dir / SOURCE_RAW_DIR
    if not raw_dir.is_dir():
        raise FrontierModelEvidenceIntakeError(f"missing raw drop directory: {raw_dir}")
    prompt_pack = source_dir / SOURCE_PROMPT_PACK_FILE
    if not prompt_pack.is_file():
        raise FrontierModelEvidenceIntakeError(f"missing prompt pack: {prompt_pack}")
    transcript = source_dir / SOURCE_TRANSCRIPT_FILE
    if not transcript.is_file():
        raise FrontierModelEvidenceIntakeError(f"missing transcript: {transcript}")
    return SourceBundle(
        source_kind=source_kind,
        source_dir=source_dir,
        raw_dir=raw_dir,
        prompt_pack=prompt_pack,
        transcript=transcript,
        model_name=_required_text(metadata, "model_name", label=str(metadata_path)),
        run_id=_required_text(metadata, "run_id", label=str(metadata_path)),
        source_command=_required_text(metadata, "source_command", label=str(metadata_path)),
    )


def discover_source_bundles(input_root: Path, *, allow_partial: bool = False) -> list[SourceBundle]:
    if not input_root.is_dir():
        raise FrontierModelEvidenceIntakeError(f"input root does not exist: {input_root}")
    bundles: list[SourceBundle] = []
    missing: list[str] = []
    for source_kind in REQUIRED_SOURCE_KINDS:
        if not (input_root / source_kind).exists():
            missing.append(source_kind)
            if allow_partial:
                continue
        try:
            bundles.append(discover_source_bundle(input_root, source_kind))
        except FrontierModelEvidenceIntakeError:
            if allow_partial:
                missing.append(source_kind)
                continue
            raise
    if missing and not allow_partial:
        raise FrontierModelEvidenceIntakeError(
            f"missing source directory: {input_root / missing[0]}"
        )
    if not bundles and not allow_partial:
        raise FrontierModelEvidenceIntakeError("at least one source directory is required")
    return bundles


def _scorecard_dir(output_root: Path) -> Path:
    return output_root / "scorecards"


def collect_source_bundles(
    bundles: Sequence[SourceBundle],
    *,
    output_root: Path,
    allow_partial: bool = False,
) -> list[Path]:
    manifest_dir = output_root / "manifests"
    collected_root = output_root / "collected"
    manifest_paths: list[Path] = []
    for bundle in bundles:
        output_dir = collected_root / bundle.source_kind
        _drops, manifest = collect_candidate_drops(
            input_dir=bundle.raw_dir,
            output_dir=output_dir,
            prompt_pack_path=bundle.prompt_pack,
            transcript_path=bundle.transcript,
            source_kind=bundle.source_kind,
            model_name=bundle.model_name,
            run_id=bundle.run_id,
            source_command=bundle.source_command,
            allow_partial=allow_partial,
        )
        manifest_path = manifest_dir / f"{bundle.source_kind}.manifest.json"
        write_manifest(manifest, manifest_path)
        manifest_paths.append(manifest_path)
    return manifest_paths


def run_intake(
    *,
    input_root: Path,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    write: bool = True,
    allow_partial: bool = False,
    publish_scorecards: bool = False,
    preflight_report: Path | None = None,
    availability_report: Path | None = None,
    collection_packet_summary: Path | None = None,
) -> dict[str, object]:
    input_root = input_root.resolve()
    output_root = output_root.resolve()
    if preflight_report is not None:
        preflight_report = preflight_report.resolve()
    if availability_report is not None:
        availability_report = availability_report.resolve()
    if collection_packet_summary is not None:
        collection_packet_summary = collection_packet_summary.resolve()
    if not write:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="chili_frontier_model_evidence_intake_") as raw_root:
            return run_intake(
                input_root=input_root,
                output_root=Path(raw_root) / "intake",
                write=True,
                allow_partial=allow_partial,
                publish_scorecards=publish_scorecards,
                preflight_report=preflight_report,
                availability_report=availability_report,
                collection_packet_summary=collection_packet_summary,
            )
    recovery_report = preflight_report or _default_preflight_report(input_root)
    source_availability_report = availability_report or _default_availability_report(input_root)
    source_collection_packet_summary = (
        collection_packet_summary or _default_collection_packet_summary(input_root)
    )
    preflight_recovery_routes = _preflight_recovery_routes(
        recovery_report,
        input_root=input_root,
    )
    availability_recovery_routes = _source_availability_routes(source_availability_report)
    source_runner_routes = _collection_packet_routes(source_collection_packet_summary)
    source_readiness = _enrich_source_readiness_with_recovery(
        inspect_all_source_readiness(input_root),
        preflight_recovery_routes,
        input_root=input_root,
    )
    source_readiness = _enrich_source_readiness_with_collection_packets(
        source_readiness,
        source_runner_routes,
        input_root=input_root,
    )
    source_readiness = _enrich_source_readiness_with_availability(
        source_readiness,
        availability_recovery_routes,
    )
    bundles = discover_source_bundles(input_root, allow_partial=allow_partial)
    manifest_paths = collect_source_bundles(
        bundles,
        output_root=output_root,
        allow_partial=allow_partial,
    )
    scorecard_dir = _scorecard_dir(output_root)
    scorecard_dir.mkdir(parents=True, exist_ok=True)
    shadow_output = (
        REPO_ROOT / "project_ws" / "AgentOps" / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md"
        if publish_scorecards
        else scorecard_dir / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md"
    )
    tournament_output = (
        REPO_ROOT / "project_ws" / "AgentOps" / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md"
        if publish_scorecards
        else scorecard_dir / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md"
    )
    manifest_load_dir = output_root / "manifests"
    manifests = (
        load_manifests([], manifest_dir=manifest_load_dir)
        if manifest_load_dir.is_dir()
        else []
    )
    shadow_results, _shadow_markdown, shadow_path, shadow_summary = run_shadow_evidence_validation(
        manifests,
        output_path=shadow_output,
        write=True,
        allow_partial=allow_partial,
    )
    collected_root = output_root / "collected"
    if allow_partial:
        collected_root.mkdir(parents=True, exist_ok=True)
    tournament_artifact, tournament_cases, tournament_results, _tournament_markdown, tournament_path = run_tournament_benchmark(
        drop_dir=collected_root,
        output_path=tournament_output,
        write=True,
        allow_partial=allow_partial,
        require_provenance=True,
    )
    shadow_benchmark_status = shadow_status(shadow_results)
    shadow_mode = (
        PARTIAL_REAL_MANIFEST_EVIDENCE_MODE
        if shadow_summary.get("missing_source_kinds")
        else REAL_MANIFEST_EVIDENCE_MODE
    )
    tournament_benchmark_status = tournament_status(tournament_results, tournament_cases)
    tournament_mode = tournament_evidence_mode(tournament_artifact)
    status = "passed"
    blockers: list[str] = []
    if shadow_benchmark_status != "passed":
        blockers.append("model_shadow_status")
    if shadow_mode != REAL_MANIFEST_EVIDENCE_MODE:
        blockers.append("model_shadow_evidence_mode")
    if tournament_benchmark_status != "passed":
        blockers.append("model_tournament_status")
    if tournament_mode != REAL_ARTIFACT_EVIDENCE_MODE:
        blockers.append("model_tournament_evidence_mode")
    if blockers:
        status = "warning"
    ready_sources = [
        item.source_kind for item in source_readiness if item.status == "ready"
    ]
    missing_sources = [
        item.source_kind for item in source_readiness if item.status != "ready"
    ]
    return {
        "schema": FRONTIER_MODEL_EVIDENCE_INTAKE_SCHEMA_VERSION,
        "generated_utc": _utc_now(),
        "status": status,
        "blockers": blockers,
        "input_root": str(input_root),
        "output_root": str(output_root),
        "preflight_report": str(recovery_report) if recovery_report else "",
        "preflight_recovery_routes": list(preflight_recovery_routes.values()),
        "preflight_recovery_route_count": len(preflight_recovery_routes),
        "availability_report": str(source_availability_report) if source_availability_report else "",
        "availability_recovery_routes": list(availability_recovery_routes.values()),
        "availability_recovery_route_count": len(availability_recovery_routes),
        "collection_packet_summary": (
            str(source_collection_packet_summary) if source_collection_packet_summary else ""
        ),
        "source_runner_routes": list(source_runner_routes.values()),
        "source_runner_route_count": len(source_runner_routes),
        "source_kinds": [bundle.source_kind for bundle in bundles],
        "required_source_kinds": list(REQUIRED_SOURCE_KINDS),
        "source_readiness": [dataclasses.asdict(item) for item in source_readiness],
        "ready_source_kinds": ready_sources,
        "missing_source_kinds": missing_sources,
        "ready_source_count": len(ready_sources),
        "required_source_count": len(REQUIRED_SOURCE_KINDS),
        "manifests": [str(path) for path in manifest_paths],
        "shadow": {
            "status": shadow_benchmark_status,
            "evidence_mode": shadow_mode,
            "average_score": shadow_average_score(shadow_results),
            "checks": len(shadow_results),
            "manifests": shadow_summary.get("manifests"),
            "cases": shadow_summary.get("cases"),
            "missing_source_kinds": shadow_summary.get("missing_source_kinds", []),
            "output": str(shadow_path),
        },
        "tournament": {
            "status": tournament_benchmark_status,
            "evidence_mode": tournament_mode,
            "average_score": tournament_average_score(tournament_results),
            "cases": len(tournament_results),
            "source_kinds": list(tournament_source_kinds(tournament_cases)),
            "output": str(tournament_path),
        },
        "published_scorecards": bool(publish_scorecards),
    }


def render_intake_summary(summary: Mapping[str, object]) -> str:
    shadow = _as_mapping(summary.get("shadow"), label="summary.shadow")
    tournament = _as_mapping(summary.get("tournament"), label="summary.tournament")
    manifests = summary.get("manifests") if isinstance(summary.get("manifests"), list) else []
    source_readiness = (
        summary.get("source_readiness")
        if isinstance(summary.get("source_readiness"), list)
        else []
    )
    lines = [
        "# CHILI Frontier Model Evidence Intake",
        "",
        f"- Schema: {summary.get('schema')}",
        f"- Generated UTC: {summary.get('generated_utc')}",
        f"- Status: {summary.get('status')}",
        f"- Input root: {summary.get('input_root')}",
        f"- Generated artifacts root: {summary.get('output_root')}",
        f"- Preflight report: {summary.get('preflight_report') or 'none'}",
        f"- Preflight recovery routes: {summary.get('preflight_recovery_route_count')}",
        f"- Availability report: {summary.get('availability_report') or 'none'}",
        f"- Availability recovery routes: {summary.get('availability_recovery_route_count')}",
        f"- Collection packet summary: {summary.get('collection_packet_summary') or 'none'}",
        f"- Source runner routes: {summary.get('source_runner_route_count')}",
        f"- Source kinds: {', '.join(str(source) for source in summary.get('source_kinds', []))}",
        f"- Ready sources: {summary.get('ready_source_count')}/{summary.get('required_source_count')}",
        f"- Missing/incomplete sources: {', '.join(str(source) for source in summary.get('missing_source_kinds', [])) or 'none'}",
        f"- Shadow evidence mode: {shadow.get('evidence_mode')}",
        f"- Shadow status: {shadow.get('status')}",
        f"- Tournament evidence mode: {tournament.get('evidence_mode')}",
        f"- Tournament status: {tournament.get('status')}",
        f"- Published scorecards: {summary.get('published_scorecards')}",
        "- Required behavior: one run ingests Codex, Claude, and local-model raw drops, stamps provenance, validates real shadow evidence, and runs the real-artifact tournament.",
        "",
        "| Artifact | Path |",
        "| --- | --- |",
    ]
    for path in manifests:
        lines.append(f"| manifest | {_escape_cell(str(path))} |")
    for key in ("output",):
        lines.append(f"| model shadow scorecard | {_escape_cell(str(shadow.get(key) or ''))} |")
        lines.append(f"| model tournament scorecard | {_escape_cell(str(tournament.get(key) or ''))} |")
    lines.extend(
        [
            "",
            "## Source Readiness",
            "",
            "| Source | Path | Status | Raw drops | Missing files | Next action |",
            "| --- | --- | --- | ---: | --- | --- |",
        ]
    )
    for raw_item in source_readiness:
        if not isinstance(raw_item, Mapping):
            continue
        missing_files = raw_item.get("missing_files")
        missing_text = (
            ", ".join(str(item) for item in missing_files)
            if isinstance(missing_files, Sequence) and not isinstance(missing_files, str)
            else str(missing_files or "none")
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_cell(raw_item.get("source_kind") or ""),
                    _escape_cell(raw_item.get("path") or ""),
                    _escape_cell(raw_item.get("status") or ""),
                    _escape_cell(raw_item.get("raw_drop_count") or 0),
                    _escape_cell(missing_text or "none"),
                    _escape_cell(raw_item.get("next_action") or ""),
                ]
            )
            + " |"
        )
    routes = summary.get("preflight_recovery_routes")
    if isinstance(routes, list) and routes:
        lines.extend(
            [
                "",
                "## Preflight Recovery Routes",
                "",
                "| Source | Action | Staging file | Dry-run import | Write/import | Single-case fallback | Validate | Publish | Boundary |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for raw_route in routes:
            if not isinstance(raw_route, Mapping):
                continue
            lines.append(
                "| "
                + " | ".join(
                    [
                        _escape_cell(str(raw_route.get("source_kind") or "")),
                        _escape_cell(str(raw_route.get("action") or "")),
                        _escape_cell(str(raw_route.get("response_staging_file") or "")),
                        _escape_cell(str(raw_route.get("dry_run_all_cases_command") or "")),
                        _escape_cell(str(raw_route.get("all_cases_command") or "")),
                        _escape_cell(str(raw_route.get("single_case_fallback") or "")),
                        _escape_cell(str(raw_route.get("validation_command") or "")),
                        _escape_cell(str(raw_route.get("publish_command") or "")),
                        _escape_cell(str(raw_route.get("boundary") or "")),
                    ]
                )
                + " |"
            )
    availability_routes = summary.get("availability_recovery_routes")
    if isinstance(availability_routes, list) and availability_routes:
        lines.extend(
            [
                "",
                "## Availability Recovery Routes",
                "",
                "| Source | Probe status | Blocker | Credential status | Action | Report |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for raw_route in availability_routes:
            if not isinstance(raw_route, Mapping):
                continue
            lines.append(
                "| "
                + " | ".join(
                    [
                        _escape_cell(str(raw_route.get("source_kind") or "")),
                        _escape_cell(str(raw_route.get("probe_status") or "")),
                        _escape_cell(str(raw_route.get("blocker") or "")),
                        _escape_cell(str(raw_route.get("credential_status") or "")),
                        _escape_cell(str(raw_route.get("action") or "")),
                        _escape_cell(str(raw_route.get("availability_report") or "")),
                    ]
                )
                + " |"
            )
    source_runner_routes = summary.get("source_runner_routes")
    if isinstance(source_runner_routes, list) and source_runner_routes:
        lines.extend(
            [
                "",
                "## Source Runner Routes",
                "",
                "| Source | Runner command | Packet summary |",
                "| --- | --- | --- |",
            ]
        )
        for raw_route in source_runner_routes:
            if not isinstance(raw_route, Mapping):
                continue
            lines.append(
                "| "
                + " | ".join(
                    [
                        _escape_cell(str(raw_route.get("source_kind") or "")),
                        _escape_cell(str(raw_route.get("source_runner_command") or "")),
                        _escape_cell(str(raw_route.get("collection_packet_summary") or "")),
                    ]
                )
                + " |"
            )
    lines.append("")
    return "\n".join(lines)


def write_summary(markdown: str, output_path: Path = DEFAULT_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect multi-source frontier model evidence into shadow and tournament scorecards."
    )
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--publish-scorecards", action="store_true")
    parser.add_argument(
        "--preflight-report",
        type=Path,
        help="Optional frontier preflight markdown report used to attach recovery routes.",
    )
    parser.add_argument(
        "--availability-report",
        type=Path,
        help="Optional frontier source availability markdown report used to attach source recovery routes.",
    )
    parser.add_argument(
        "--collection-packet-summary",
        type=Path,
        help="Optional source collection-packet summary used to attach automated source-runner routes.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    try:
        summary = run_intake(
            input_root=args.input_root,
            output_root=args.output_root,
            write=not args.no_write,
            allow_partial=args.allow_partial,
            publish_scorecards=args.publish_scorecards,
            preflight_report=args.preflight_report,
            availability_report=args.availability_report,
            collection_packet_summary=args.collection_packet_summary,
        )
    except (
        FrontierModelEvidenceIntakeError,
        DropCollectionError,
        ShadowEvidenceError,
        TournamentError,
        ArtifactBuildError,
    ) as exc:
        print(f"frontier model evidence intake error: {exc}", file=sys.stderr)
        return 2
    markdown = render_intake_summary(summary)
    if not args.no_write:
        write_summary(markdown, args.output)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(markdown)
        if not args.no_write:
            print(f"Wrote {args.output}")
    return 0 if summary.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
