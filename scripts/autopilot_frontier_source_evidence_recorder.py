from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_frontier_bakeoff_benchmark import _command_text, _escape_cell  # noqa: E402
from scripts.autopilot_local_model_candidate_runner import (  # noqa: E402
    LocalModelCandidateRunnerError,
    _patch_from_payload as _patch_from_model_payload,
    parse_model_response,
    parse_model_response_suite,
)
from scripts.autopilot_model_candidate_artifact_bakeoff import (  # noqa: E402
    ALLOWED_SOURCE_KINDS,
)
from scripts.autopilot_model_candidate_artifact_builder import (  # noqa: E402
    ArtifactBuildError,
    MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
    sha256_file,
    validate_prompt_pack_markdown,
)
from scripts.autopilot_model_candidate_drop_collector import (  # noqa: E402
    DropCollectionError,
    collect_candidate_drops,
)
from scripts.autopilot_real_chili_candidate_bakeoff import default_cases  # noqa: E402


DEFAULT_SOURCE_ROOT = (
    REPO_ROOT
    / "project_ws"
    / "AgentOps"
    / "frontier_model_evidence_intake"
    / "raw_sources"
)
DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "FRONTIER_SOURCE_EVIDENCE_RECORDING.md"
FRONTIER_SOURCE_EVIDENCE_RECORDER_SCHEMA_VERSION = (
    "chili.frontier-source-evidence-recorder.v1"
)
FRONTIER_MODEL_EVIDENCE_INTAKE_VALIDATE_COMMAND = (
    "python scripts/autopilot_frontier_model_evidence_intake.py "
    "--input-root {input_root} --allow-partial --json --no-write"
)
FRONTIER_MODEL_EVIDENCE_INTAKE_PUBLISH_COMMAND = (
    "python scripts/autopilot_frontier_model_evidence_intake.py "
    "--input-root {input_root} --publish-scorecards --json"
)
SOURCE_KINDS = tuple(source for source in ALLOWED_SOURCE_KINDS if source != "fixture")
PROMPT_PACK_FILE = "prompt_pack.md"
METADATA_FILE = "metadata.json"
TRANSCRIPT_FILE = "transcript.jsonl"
RAW_DIR = "raw"
PLACEHOLDER_MARKERS = (
    "<replace-with-real",
    "<paste exact",
    "<utc iso-8601",
    "<sha256",
    "<external model",
    "<collector name",
    "<model invocation",
    "<replace",
)


class FrontierSourceEvidenceRecorderError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _reject_placeholder_text(text: str, *, label: str) -> None:
    lowered = text.lower()
    for marker in PLACEHOLDER_MARKERS:
        if marker in lowered:
            raise FrontierSourceEvidenceRecorderError(
                f"{label} still contains template placeholder: {marker}"
            )


def _required_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FrontierSourceEvidenceRecorderError(f"{label} is required")
    text = value.strip()
    _reject_placeholder_text(text, label=label)
    return text


def _safe_name(value: object, *, fallback: str) -> str:
    raw = str(value or fallback).strip().lower()
    safe = re.sub(r"[^a-z0-9._-]+", "-", raw).strip(".-")
    return safe or fallback


def _source_kind(value: object) -> str:
    source_kind = _required_text(value, label="source_kind")
    if source_kind not in SOURCE_KINDS:
        raise FrontierSourceEvidenceRecorderError(
            "source_kind must be one of " + ", ".join(SOURCE_KINDS)
    )
    return source_kind


def _command_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _intake_validation_command(source_root: Path) -> str:
    input_root = _command_path(source_root)
    return FRONTIER_MODEL_EVIDENCE_INTAKE_VALIDATE_COMMAND.format(
        input_root=input_root
    )


def _intake_publish_command(source_root: Path) -> str:
    input_root = _command_path(source_root)
    return FRONTIER_MODEL_EVIDENCE_INTAKE_PUBLISH_COMMAND.format(
        input_root=input_root
    )


def _case_by_id(case_id: object):
    clean_case_id = _required_text(case_id, label="case_id")
    for case in default_cases():
        if case.case_id == clean_case_id:
            return case
    known = ", ".join(case.case_id for case in default_cases())
    raise FrontierSourceEvidenceRecorderError(
        f"unknown case_id {clean_case_id}; expected one of: {known}"
    )


def _optional_string_match(
    payload: Mapping[str, object],
    key: str,
    *,
    expected: str,
    label: str,
) -> None:
    value = payload.get(key)
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise FrontierSourceEvidenceRecorderError(f"{label}.{key} must be {expected}")
    if value.strip() != expected:
        raise FrontierSourceEvidenceRecorderError(
            f"{label}.{key} {value.strip()} does not match expected {expected}"
        )


def _optional_string_list_match(
    payload: Mapping[str, object],
    key: str,
    *,
    expected: Sequence[str],
    label: str,
) -> None:
    value = payload.get(key)
    if value is None:
        return
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise FrontierSourceEvidenceRecorderError(
            f"{label}.{key} must be a list of non-empty strings"
        )
    normalized = [item.strip() for item in value]
    if normalized != list(expected):
        raise FrontierSourceEvidenceRecorderError(
            f"{label}.{key} {normalized} does not match expected {list(expected)}"
        )


def _optional_number(payload: Mapping[str, object], key: str, *, default: float) -> float:
    value = payload.get(key)
    if value is None:
        return default
    if not isinstance(value, (int, float)):
        raise FrontierSourceEvidenceRecorderError(f"model_response.{key} must be a number")
    return float(value)


def _measured_or_reported_duration(
    payload: Mapping[str, object],
    *,
    measured_default: float | None,
) -> float:
    reported = _optional_number(payload, "duration_seconds", default=0.0)
    if reported > 0:
        return reported
    if measured_default is not None and measured_default > 0:
        return float(measured_default)
    return reported


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FrontierSourceEvidenceRecorderError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise FrontierSourceEvidenceRecorderError(f"{path}: JSON must be an object")
    return payload


def _model_name_from_prompt_pack(prompt_pack: Path) -> str:
    if not prompt_pack.is_file():
        raise FrontierSourceEvidenceRecorderError(f"prompt pack missing: {prompt_pack}")
    text = prompt_pack.read_text(encoding="utf-8", errors="replace")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.lower().startswith("- model name:"):
            return _required_text(line.split(":", 1)[1], label="prompt_pack.model_name")
    raise FrontierSourceEvidenceRecorderError(
        f"prompt pack does not declare a model name: {prompt_pack}"
    )


def _validate_prompt_pack(
    prompt_pack: Path,
    *,
    source_kind: str,
    model_name: str,
) -> str:
    if not prompt_pack.is_file():
        raise FrontierSourceEvidenceRecorderError(f"prompt pack missing: {prompt_pack}")
    text = prompt_pack.read_text(encoding="utf-8", errors="replace")
    try:
        validate_prompt_pack_markdown(
            text,
            source_kind=source_kind,
            model_name=model_name,
            label=str(prompt_pack),
        )
    except ArtifactBuildError as exc:
        raise FrontierSourceEvidenceRecorderError(str(exc)) from exc
    return sha256_file(prompt_pack)


def _safe_relative_copy_files(input_dir: Path) -> list[tuple[Path, Path]]:
    if not input_dir.is_dir():
        raise FrontierSourceEvidenceRecorderError(f"drop directory does not exist: {input_dir}")
    resolved_input = input_dir.resolve()
    files: list[tuple[Path, Path]] = []
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved_input not in resolved.parents and resolved != resolved_input:
            raise FrontierSourceEvidenceRecorderError(f"drop file escapes input directory: {path}")
        rel = path.relative_to(input_dir)
        files.append((path, rel))
    if not files:
        raise FrontierSourceEvidenceRecorderError(f"drop directory is empty: {input_dir}")
    json_files = [source for source, _rel in files if source.suffix.lower() == ".json"]
    if not json_files:
        raise FrontierSourceEvidenceRecorderError(
            f"drop directory has no {MODEL_CANDIDATE_DROP_SCHEMA_VERSION} JSON drops: {input_dir}"
        )
    return files


def _reject_placeholder_files(
    files: Sequence[tuple[Path, Path]],
    *,
    source_kind: str,
    model_name: str,
) -> None:
    for source, rel in files:
        if source.suffix.lower() not in {".json", ".jsonl", ".md", ".txt", ".patch"}:
            continue
        text = source.read_text(encoding="utf-8", errors="replace")
        _reject_placeholder_text(text, label=f"drop artifact {rel.as_posix()}")
        if source.suffix.lower() != ".json":
            continue
        payload = _read_json(source)
        if payload.get("schema") != MODEL_CANDIDATE_DROP_SCHEMA_VERSION:
            raise FrontierSourceEvidenceRecorderError(
                f"{rel.as_posix()}.schema is {payload.get('schema') or 'missing'} "
                f"instead of {MODEL_CANDIDATE_DROP_SCHEMA_VERSION}"
            )
        if payload.get("source_kind") != source_kind:
            raise FrontierSourceEvidenceRecorderError(
                f"{rel.as_posix()}.source_kind must be {source_kind}"
            )
        if payload.get("model_name") != model_name:
            raise FrontierSourceEvidenceRecorderError(
                f"{rel.as_posix()}.model_name must be {model_name}"
            )


def _case_ids_from_drop_files(files: Sequence[tuple[Path, Path]]) -> list[str]:
    case_ids: list[str] = []
    for source, _rel in files:
        if source.suffix.lower() != ".json":
            continue
        payload = _read_json(source)
        raw_case_id = payload.get("case_id")
        if isinstance(raw_case_id, str) and raw_case_id.strip():
            case_ids.append(raw_case_id.strip())
    return sorted(set(case_ids))


def _candidate_id_from_payload(
    payload: Mapping[str, object],
    *,
    source_kind: str,
    case_id: str,
    explicit_candidate_id: str | None,
) -> str:
    if explicit_candidate_id is not None:
        return _required_text(explicit_candidate_id, label="candidate_id")
    raw_candidate_id = payload.get("candidate_id")
    if isinstance(raw_candidate_id, str) and raw_candidate_id.strip():
        candidate_id = raw_candidate_id.strip()
        if candidate_id != "local_model-extracted-candidate":
            _reject_placeholder_text(candidate_id, label="model_response.candidate_id")
            return candidate_id
    return f"{source_kind}-{_safe_name(case_id, fallback='case')}"


def _build_response_drop_dir(
    *,
    response_path: Path,
    raw_dir: Path,
    source_kind: str,
    model_name: str,
    case_id: str,
    candidate_id: str | None,
    measured_run_duration_seconds: float | None = None,
) -> Path:
    if not response_path.is_file():
        raise FrontierSourceEvidenceRecorderError(f"response file does not exist: {response_path}")
    response_text = response_path.read_text(encoding="utf-8", errors="replace")
    try:
        payload = parse_model_response(response_text)
    except LocalModelCandidateRunnerError as exc:
        raise FrontierSourceEvidenceRecorderError(
            f"model response could not be converted into a candidate drop: {exc}"
        ) from exc

    case = _case_by_id(case_id)
    try:
        patch_text = _patch_from_model_payload(payload, case=case)
    except LocalModelCandidateRunnerError as exc:
        raise FrontierSourceEvidenceRecorderError(
            f"model response could not be converted into a candidate drop: {exc}"
        ) from exc
    command = _command_text(case.test_command)
    planned_file = case.incumbent.planned_file
    expected_changed_files = list(case.incumbent.expected_changed_files)
    _optional_string_match(
        payload,
        "case_id",
        expected=case.case_id,
        label="model_response",
    )
    _optional_string_match(
        payload,
        "source_kind",
        expected=source_kind,
        label="model_response",
    )
    _optional_string_match(
        payload,
        "model_name",
        expected=model_name,
        label="model_response",
    )
    _optional_string_match(
        payload,
        "planned_file",
        expected=planned_file,
        label="model_response",
    )
    _optional_string_list_match(
        payload,
        "expected_changed_files",
        expected=expected_changed_files,
        label="model_response",
    )
    _optional_string_list_match(
        payload,
        "declared_commands",
        expected=[command],
        label="model_response",
    )

    raw_dir.mkdir(parents=True, exist_ok=True)
    safe_case_id = _safe_name(case.case_id, fallback="case")
    patch_path = raw_dir / f"{safe_case_id}.patch"
    patch_path.write_text(patch_text, encoding="utf-8")
    notes = payload.get("notes") or payload.get("explanation") or ""
    drop = {
        "schema": MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
        "case_id": case.case_id,
        "candidate_id": _candidate_id_from_payload(
            payload,
            source_kind=source_kind,
            case_id=case.case_id,
            explicit_candidate_id=candidate_id,
        ),
        "model_name": model_name,
        "source_kind": source_kind,
        "collected_at": _utc_now(),
        "patch_file": patch_path.name,
        "planned_file": planned_file,
        "expected_changed_files": expected_changed_files,
        "declared_commands": [command],
        "duration_seconds": _measured_or_reported_duration(
            payload,
            measured_default=measured_run_duration_seconds,
        ),
        "cost_units": _optional_number(payload, "cost_units", default=0.0),
        "notes": str(notes).strip(),
    }
    (raw_dir / f"{safe_case_id}.json").write_text(
        json.dumps(drop, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return raw_dir


def _build_response_suite_drop_dir(
    *,
    response_path: Path,
    raw_dir: Path,
    source_kind: str,
    model_name: str,
    candidate_id: str | None,
    measured_run_duration_seconds: float | None = None,
) -> Path:
    if not response_path.is_file():
        raise FrontierSourceEvidenceRecorderError(f"response file does not exist: {response_path}")
    response_text = response_path.read_text(encoding="utf-8", errors="replace")
    case_ids = [case.case_id for case in default_cases()]
    try:
        payloads = parse_model_response_suite(response_text, case_ids=case_ids)
    except LocalModelCandidateRunnerError as exc:
        raise FrontierSourceEvidenceRecorderError(
            f"model response could not be converted into suite candidate drops: {exc}"
        ) from exc

    measured_per_case = (
        float(measured_run_duration_seconds) / len(payloads)
        if measured_run_duration_seconds is not None
        and measured_run_duration_seconds > 0
        and payloads
        else None
    )

    raw_dir.mkdir(parents=True, exist_ok=True)
    for payload in payloads:
        case_id = _required_text(payload.get("case_id"), label="model_response.case_id")
        case = _case_by_id(case_id)
        try:
            patch_text = _patch_from_model_payload(payload, case=case)
        except LocalModelCandidateRunnerError as exc:
            raise FrontierSourceEvidenceRecorderError(
                f"model response could not be converted into suite candidate drops: {exc}"
            ) from exc
        command = _command_text(case.test_command)
        planned_file = case.incumbent.planned_file
        expected_changed_files = list(case.incumbent.expected_changed_files)
        _optional_string_match(
            payload,
            "source_kind",
            expected=source_kind,
            label="model_response",
        )
        _optional_string_match(
            payload,
            "model_name",
            expected=model_name,
            label="model_response",
        )
        _optional_string_match(
            payload,
            "planned_file",
            expected=planned_file,
            label="model_response",
        )
        _optional_string_list_match(
            payload,
            "expected_changed_files",
            expected=expected_changed_files,
            label="model_response",
        )
        _optional_string_list_match(
            payload,
            "declared_commands",
            expected=[command],
            label="model_response",
        )
        safe_case_id = _safe_name(case.case_id, fallback="case")
        patch_path = raw_dir / f"{safe_case_id}.patch"
        patch_path.write_text(patch_text, encoding="utf-8")
        notes = payload.get("notes") or payload.get("explanation") or ""
        suite_candidate_id = (
            f"{_required_text(candidate_id, label='candidate_id')}-{safe_case_id}"
            if candidate_id
            else None
        )
        drop = {
            "schema": MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
            "case_id": case.case_id,
            "candidate_id": _candidate_id_from_payload(
                payload,
                source_kind=source_kind,
                case_id=case.case_id,
                explicit_candidate_id=suite_candidate_id,
            ),
            "model_name": model_name,
            "source_kind": source_kind,
            "collected_at": _utc_now(),
            "patch_file": patch_path.name,
            "planned_file": planned_file,
            "expected_changed_files": expected_changed_files,
            "declared_commands": [command],
            "duration_seconds": _measured_or_reported_duration(
                payload,
                measured_default=measured_per_case,
            ),
            "cost_units": _optional_number(payload, "cost_units", default=0.0),
            "notes": str(notes).strip(),
        }
        (raw_dir / f"{safe_case_id}.json").write_text(
            json.dumps(drop, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return raw_dir


def _target_conflicts(source_dir: Path, *, overwrite: bool) -> list[str]:
    if overwrite:
        return []
    conflicts: list[str] = []
    for path in (source_dir / METADATA_FILE, source_dir / TRANSCRIPT_FILE):
        if path.exists():
            conflicts.append(str(path))
    raw_dir = source_dir / RAW_DIR
    if raw_dir.is_dir() and any(path.is_file() for path in raw_dir.rglob("*")):
        conflicts.append(str(raw_dir))
    return conflicts


def _copy_raw_files(files: Sequence[tuple[Path, Path]], *, raw_dir: Path) -> list[str]:
    copied: list[str] = []
    raw_dir.mkdir(parents=True, exist_ok=True)
    for source, rel in files:
        destination = raw_dir / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)
        copied.append(rel.as_posix())
    return copied


def _write_metadata(
    path: Path,
    *,
    source_kind: str,
    model_name: str,
    run_id: str,
    source_command: str,
    prompt_pack_sha256: str,
    recorded_at: str,
    measured_run_duration_seconds: float | None,
    duration_attribution: str,
) -> None:
    payload = {
        "model_name": model_name,
        "prompt_pack_file": PROMPT_PACK_FILE,
        "prompt_pack_sha256": prompt_pack_sha256,
        "raw_dir": RAW_DIR,
        "recorded_at": recorded_at,
        "recorder": "autopilot_frontier_source_evidence_recorder",
        "run_id": run_id,
        "source_command": source_command,
        "source_kind": source_kind,
        "transcript_file": TRANSCRIPT_FILE,
        "measured_run_duration_seconds": measured_run_duration_seconds,
        "duration_attribution": duration_attribution,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _build_transcript_from_response(
    response_path: Path,
    *,
    source_kind: str,
    model_name: str,
    run_id: str,
    source_command: str,
    prompt_pack_sha256: str,
    raw_files: Sequence[str],
    case_ids: Sequence[str],
    measured_run_duration_seconds: float | None,
    duration_attribution: str,
) -> str:
    if not response_path.is_file():
        raise FrontierSourceEvidenceRecorderError(f"response file does not exist: {response_path}")
    response_text = response_path.read_text(encoding="utf-8", errors="replace")
    _reject_placeholder_text(response_text, label=str(response_path))
    events = [
        {
            "content": (
                "Prompt pack prompt_pack.md was sent to the frontier source for CHILI "
                f"candidate repair evidence with sha256 {prompt_pack_sha256}."
            ),
            "event": "prompt_sent",
            "case_ids": list(case_ids),
            "model_name": model_name,
            "prompt_pack_file": PROMPT_PACK_FILE,
            "prompt_pack_sha256": prompt_pack_sha256,
            "role": "user",
            "run_id": run_id,
            "source_command": source_command,
            "source_kind": source_kind,
            "measured_run_duration_seconds": measured_run_duration_seconds,
            "duration_attribution": duration_attribution,
        },
        {
            "content": response_text,
            "event": "assistant_response",
            "case_ids": list(case_ids),
            "model_name": model_name,
            "role": "assistant",
            "run_id": run_id,
            "source_kind": source_kind,
        },
        {
            "event": "model_output_recorded",
            "case_ids": list(case_ids),
            "model_name": model_name,
            "output": "raw candidate files: " + ", ".join(raw_files),
            "raw_files": list(raw_files),
            "run_id": run_id,
            "source_kind": source_kind,
        },
    ]
    return "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"


def _copy_or_build_transcript(
    *,
    transcript_path: Path | None,
    response_path: Path | None,
    destination: Path,
    source_kind: str,
    model_name: str,
    run_id: str,
    source_command: str,
    prompt_pack_sha256: str,
    raw_files: Sequence[str],
    case_ids: Sequence[str],
    measured_run_duration_seconds: float | None,
    duration_attribution: str,
) -> None:
    if transcript_path and response_path:
        raise FrontierSourceEvidenceRecorderError("use --transcript or --response, not both")
    if transcript_path:
        if not transcript_path.is_file():
            raise FrontierSourceEvidenceRecorderError(
                f"transcript does not exist: {transcript_path}"
            )
        text = transcript_path.read_text(encoding="utf-8", errors="replace")
        _reject_placeholder_text(text, label=str(transcript_path))
        destination.write_text(text, encoding="utf-8")
        return
    if response_path:
        destination.write_text(
            _build_transcript_from_response(
                response_path,
                source_kind=source_kind,
                model_name=model_name,
                run_id=run_id,
                source_command=source_command,
                prompt_pack_sha256=prompt_pack_sha256,
                raw_files=raw_files,
                case_ids=case_ids,
                measured_run_duration_seconds=measured_run_duration_seconds,
                duration_attribution=duration_attribution,
            ),
            encoding="utf-8",
        )
        return
    raise FrontierSourceEvidenceRecorderError("either --transcript or --response is required")


def _prepare_recording_target(
    *,
    target_source_dir: Path,
    prompt_pack: Path,
    drop_dir: Path,
    transcript_path: Path | None,
    response_path: Path | None,
    source_kind: str,
    model_name: str,
    run_id: str,
    source_command: str,
    prompt_pack_sha256: str,
    overwrite: bool,
    measured_run_duration_seconds: float | None,
    duration_attribution: str,
) -> dict[str, object]:
    conflicts = _target_conflicts(target_source_dir, overwrite=overwrite)
    if conflicts:
        raise FrontierSourceEvidenceRecorderError(
            "existing frontier source evidence would be overwritten; rerun with --overwrite "
            "only after reviewing: " + ", ".join(conflicts)
        )
    files = _safe_relative_copy_files(drop_dir)
    _reject_placeholder_files(files, source_kind=source_kind, model_name=model_name)
    case_ids = _case_ids_from_drop_files(files)
    target_source_dir.mkdir(parents=True, exist_ok=True)
    if prompt_pack.resolve() != (target_source_dir / PROMPT_PACK_FILE).resolve():
        shutil.copyfile(prompt_pack, target_source_dir / PROMPT_PACK_FILE)
    raw_dir = target_source_dir / RAW_DIR
    drop_is_target_raw = drop_dir.resolve() == raw_dir.resolve()
    if overwrite and raw_dir.exists() and not drop_is_target_raw:
        shutil.rmtree(raw_dir)
    raw_files = _copy_raw_files(files, raw_dir=raw_dir)
    recorded_at = _utc_now()
    _write_metadata(
        target_source_dir / METADATA_FILE,
        source_kind=source_kind,
        model_name=model_name,
        run_id=run_id,
        source_command=source_command,
        prompt_pack_sha256=prompt_pack_sha256,
        recorded_at=recorded_at,
        measured_run_duration_seconds=measured_run_duration_seconds,
        duration_attribution=duration_attribution,
    )
    _copy_or_build_transcript(
        transcript_path=transcript_path,
        response_path=response_path,
        destination=target_source_dir / TRANSCRIPT_FILE,
        source_kind=source_kind,
        model_name=model_name,
        run_id=run_id,
        source_command=source_command,
        prompt_pack_sha256=prompt_pack_sha256,
        raw_files=raw_files,
        case_ids=case_ids,
        measured_run_duration_seconds=measured_run_duration_seconds,
        duration_attribution=duration_attribution,
    )
    with tempfile.TemporaryDirectory(prefix="chili_frontier_source_recorder_validate_") as tmp:
        _drops, manifest = collect_candidate_drops(
            input_dir=raw_dir,
            output_dir=Path(tmp) / "validated",
            prompt_pack_path=target_source_dir / PROMPT_PACK_FILE,
            transcript_path=target_source_dir / TRANSCRIPT_FILE,
            source_kind=source_kind,
            model_name=model_name,
            run_id=run_id,
            source_command=source_command,
            allow_partial=True,
        )
    return {
        "recorded_at": recorded_at,
        "raw_files": raw_files,
        "raw_file_count": len(raw_files),
        "metadata": str(target_source_dir / METADATA_FILE),
        "transcript": str(target_source_dir / TRANSCRIPT_FILE),
        "raw_dir": str(raw_dir),
        "validation_manifest": manifest,
    }


def record_frontier_source_evidence(
    *,
    source_kind: str,
    source_root: Path = DEFAULT_SOURCE_ROOT,
    source_dir: Path | None = None,
    drop_dir: Path | None = None,
    prompt_pack_path: Path | None = None,
    transcript_path: Path | None = None,
    response_path: Path | None = None,
    model_name: str | None = None,
    case_id: str | None = None,
    all_cases: bool = False,
    candidate_id: str | None = None,
    run_id: str,
    source_command: str,
    write: bool = True,
    overwrite: bool = False,
    measured_run_duration_seconds: float | None = None,
) -> dict[str, object]:
    clean_source_kind = _source_kind(source_kind)
    target_source_dir = source_dir or source_root / clean_source_kind
    effective_source_root = target_source_dir.parent if source_dir is not None else source_root
    prompt_pack = prompt_pack_path or target_source_dir / PROMPT_PACK_FILE
    resolved_model_name = _required_text(
        model_name or _model_name_from_prompt_pack(prompt_pack),
        label="model_name",
    )
    clean_run_id = _required_text(run_id, label="run_id")
    clean_source_command = _required_text(source_command, label="source_command")
    measured_duration = (
        max(0.0, float(measured_run_duration_seconds))
        if measured_run_duration_seconds is not None
        else None
    )
    duration_attribution = (
        "measured_source_wall_clock_evenly_attributed_across_cases"
        if measured_duration is not None and all_cases
        else "measured_source_wall_clock"
        if measured_duration is not None
        else "source_reported_or_unmeasured"
    )
    prompt_pack_sha256 = _validate_prompt_pack(
        prompt_pack,
        source_kind=clean_source_kind,
        model_name=resolved_model_name,
    )
    generated_temp = None
    response_imported = False
    input_drop_dir = drop_dir
    effective_drop_dir = drop_dir
    if effective_drop_dir is None:
        if transcript_path is not None:
            raise FrontierSourceEvidenceRecorderError(
                "response-only recording cannot use --transcript; provide --response so "
                "the recorder can parse a patch into raw candidate files"
            )
        if response_path is None:
            raise FrontierSourceEvidenceRecorderError(
                "either --drop-dir or --response with --case-id is required"
            )
        if not case_id and not all_cases:
            raise FrontierSourceEvidenceRecorderError(
                "--case-id or --all-cases is required when --drop-dir is omitted"
            )
        generated_temp = tempfile.TemporaryDirectory(
            prefix="chili_frontier_source_response_drop_"
        )
        if all_cases:
            effective_drop_dir = _build_response_suite_drop_dir(
                response_path=response_path,
                raw_dir=Path(generated_temp.name) / "raw",
                source_kind=clean_source_kind,
                model_name=resolved_model_name,
                candidate_id=candidate_id,
                measured_run_duration_seconds=measured_duration,
            )
        else:
            effective_drop_dir = _build_response_drop_dir(
                response_path=response_path,
                raw_dir=Path(generated_temp.name) / "raw",
                source_kind=clean_source_kind,
                model_name=resolved_model_name,
                case_id=str(case_id),
                candidate_id=candidate_id,
                measured_run_duration_seconds=measured_duration,
            )
        response_imported = True

    try:
        if write:
            target_prompt_pack = target_source_dir / PROMPT_PACK_FILE
            if (
                prompt_pack_path is not None
                and target_prompt_pack.exists()
                and prompt_pack.resolve() != target_prompt_pack.resolve()
                and sha256_file(prompt_pack) != sha256_file(target_prompt_pack)
                and not overwrite
            ):
                raise FrontierSourceEvidenceRecorderError(
                    "existing prompt_pack.md would be overwritten; rerun with --overwrite "
                    "only after reviewing the run-specific prompt pack"
                )
            try:
                recording = _prepare_recording_target(
                    target_source_dir=target_source_dir,
                    prompt_pack=prompt_pack,
                    drop_dir=effective_drop_dir,
                    transcript_path=transcript_path,
                    response_path=response_path,
                    source_kind=clean_source_kind,
                    model_name=resolved_model_name,
                    run_id=clean_run_id,
                    source_command=clean_source_command,
                    prompt_pack_sha256=prompt_pack_sha256,
                    overwrite=overwrite,
                    measured_run_duration_seconds=measured_duration,
                    duration_attribution=duration_attribution,
                )
            except DropCollectionError as exc:
                raise FrontierSourceEvidenceRecorderError(str(exc)) from exc
        else:
            with tempfile.TemporaryDirectory(prefix="chili_frontier_source_recorder_dry_run_") as tmp:
                dry_source_dir = Path(tmp) / clean_source_kind
                dry_source_dir.mkdir(parents=True)
                shutil.copyfile(prompt_pack, dry_source_dir / PROMPT_PACK_FILE)
                try:
                    recording = _prepare_recording_target(
                        target_source_dir=dry_source_dir,
                        prompt_pack=dry_source_dir / PROMPT_PACK_FILE,
                        drop_dir=effective_drop_dir,
                        transcript_path=transcript_path,
                        response_path=response_path,
                        source_kind=clean_source_kind,
                        model_name=resolved_model_name,
                        run_id=clean_run_id,
                        source_command=clean_source_command,
                        prompt_pack_sha256=prompt_pack_sha256,
                        overwrite=True,
                        measured_run_duration_seconds=measured_duration,
                        duration_attribution=duration_attribution,
                    )
                except DropCollectionError as exc:
                    raise FrontierSourceEvidenceRecorderError(str(exc)) from exc
                recording = dict(recording)
                recording["metadata"] = str(target_source_dir / METADATA_FILE)
                recording["transcript"] = str(target_source_dir / TRANSCRIPT_FILE)
                recording["raw_dir"] = str(target_source_dir / RAW_DIR)
    finally:
        if generated_temp is not None:
            generated_temp.cleanup()

    manifest = recording["validation_manifest"]
    cases = int(manifest.get("cases") or 0) if isinstance(manifest, Mapping) else 0
    validation_command = _intake_validation_command(effective_source_root)
    publish_command = _intake_publish_command(effective_source_root)
    return {
        "schema": FRONTIER_SOURCE_EVIDENCE_RECORDER_SCHEMA_VERSION,
        "generated_utc": _utc_now(),
        "status": "passed",
        "write": bool(write),
        "source_kind": clean_source_kind,
        "source_root": str(effective_source_root),
        "source_dir": str(target_source_dir),
        "model_name": resolved_model_name,
        "run_id": clean_run_id,
        "source_command": clean_source_command,
        "measured_run_duration_seconds": measured_duration,
        "duration_attribution": duration_attribution,
        "input_prompt_pack": str(prompt_pack),
        "prompt_pack": str(target_source_dir / PROMPT_PACK_FILE),
        "prompt_pack_sha256": prompt_pack_sha256,
        "drop_dir": str(input_drop_dir) if input_drop_dir is not None else "generated-from-response",
        "response_imported": response_imported,
        "case_id": "all" if all_cases else case_id or "",
        "all_cases": bool(all_cases),
        "metadata": recording["metadata"],
        "transcript": recording["transcript"],
        "raw_dir": recording["raw_dir"],
        "raw_file_count": recording["raw_file_count"],
        "raw_files": recording["raw_files"],
        "cases": cases,
        "validated_with_provenance": True,
        "promotion_ready": False,
        "validation_command": validation_command,
        "publish_command": publish_command,
        "next_action": (
            "Validate frontier source readiness with "
            f"{validation_command}. Publish scorecards only after all required "
            f"sources are ready: {publish_command}."
        ),
        "permission_boundary": (
            "records and validates frontier model evidence only; it does not run models, "
            "edit source/tests, restart runtime, use git/PR tools, deploy, or touch live trading"
        ),
    }


def render_recording_summary(summary: Mapping[str, object]) -> str:
    lines = [
        "# CHILI Frontier Source Evidence Recording",
        "",
        f"- Schema: {summary.get('schema')}",
        f"- Generated UTC: {summary.get('generated_utc')}",
        f"- Status: {summary.get('status')}",
        f"- Write mode: {summary.get('write')}",
        f"- Source kind: {summary.get('source_kind')}",
        f"- Model: {summary.get('model_name')}",
        f"- Run id: {summary.get('run_id')}",
        f"- Response imported: {summary.get('response_imported')}",
        f"- Case id: {summary.get('case_id')}",
        f"- Cases: {summary.get('cases')}",
        f"- Validated with provenance: {summary.get('validated_with_provenance')}",
        f"- Promotion ready: {summary.get('promotion_ready')}",
        f"- Source dir: {summary.get('source_dir')}",
        f"- Validation command: {summary.get('validation_command')}",
        f"- Publish command: {summary.get('publish_command')}",
        f"- Next action: {summary.get('next_action')}",
        f"- Permission boundary: {summary.get('permission_boundary')}",
        "",
        "| Artifact | Path |",
        "| --- | --- |",
    ]
    for label, key in (
        ("prompt_pack", "prompt_pack"),
        ("metadata", "metadata"),
        ("transcript", "transcript"),
        ("raw_dir", "raw_dir"),
    ):
        lines.append(f"| {_escape_cell(label)} | {_escape_cell(str(summary.get(key) or ''))} |")
    lines.append("")
    raw_files = summary.get("raw_files")
    if isinstance(raw_files, list) and raw_files:
        lines.extend(["| Raw file |", "| --- |"])
        for raw_file in raw_files:
            lines.append(f"| {_escape_cell(str(raw_file))} |")
        lines.append("")
    return "\n".join(lines)


def write_summary(markdown: str, output_path: Path = DEFAULT_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Record transcript-bound Codex, Claude, local-model, or other frontier "
            "candidate drops into the evidence intake folder without running models. "
            "A hosted response can be parsed into a raw candidate drop when --drop-dir "
            "is omitted and --case-id is provided."
        )
    )
    parser.add_argument("--source-kind", required=True, choices=SOURCE_KINDS)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--drop-dir", type=Path)
    parser.add_argument("--prompt-pack", type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--transcript", type=Path)
    group.add_argument("--response", type=Path)
    parser.add_argument("--model-name")
    parser.add_argument("--case-id")
    parser.add_argument("--all-cases", action="store_true")
    parser.add_argument("--candidate-id")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-command", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    try:
        summary = record_frontier_source_evidence(
            source_kind=args.source_kind,
            source_root=args.source_root,
            source_dir=args.source_dir,
            drop_dir=args.drop_dir,
            prompt_pack_path=args.prompt_pack,
            transcript_path=args.transcript,
            response_path=args.response,
            model_name=args.model_name,
            case_id=args.case_id,
            all_cases=args.all_cases,
            candidate_id=args.candidate_id,
            run_id=args.run_id,
            source_command=args.source_command,
            write=not args.no_write,
            overwrite=args.overwrite,
        )
    except FrontierSourceEvidenceRecorderError as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "schema": FRONTIER_SOURCE_EVIDENCE_RECORDER_SCHEMA_VERSION,
                        "status": "failed",
                        "error": str(exc),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"[frontier-source-evidence-recorder] failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        markdown = render_recording_summary(summary)
        if not args.no_write:
            write_summary(markdown, args.output)
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
