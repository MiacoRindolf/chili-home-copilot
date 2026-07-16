from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_frontier_bakeoff_benchmark import _command_text, _escape_cell  # noqa: E402
from scripts.autopilot_frontier_prompt_pack_bundle import (  # noqa: E402
    DEFAULT_OUTPUT_DIR as DEFAULT_PROMPT_PACK_BUNDLE_DIR,
    MANIFEST_FILE,
    PromptPackBundleError,
    validate_bundle_manifest,
)
from scripts.autopilot_model_candidate_tournament_benchmark import (  # noqa: E402
    REQUIRED_SOURCE_KINDS,
)
from scripts.autopilot_real_chili_candidate_bakeoff import default_cases  # noqa: E402


DEFAULT_RAW_SOURCE_ROOT = (
    REPO_ROOT
    / "project_ws"
    / "AgentOps"
    / "frontier_model_evidence_intake"
    / "raw_sources"
)
DEFAULT_OUTPUT_DIR = (
    REPO_ROOT
    / "project_ws"
    / "AgentOps"
    / "frontier_model_evidence_intake"
    / "collection_packets"
)
DEFAULT_SUMMARY_OUTPUT = (
    REPO_ROOT / "project_ws" / "AgentOps" / "FRONTIER_SOURCE_COLLECTION_PACKETS.md"
)
DEFAULT_AVAILABILITY_REPORT = (
    REPO_ROOT / "project_ws" / "AgentOps" / "FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS.md"
)
FRONTIER_SOURCE_COLLECTION_PACKET_SCHEMA_VERSION = (
    "chili.frontier-source-collection-packet.v1"
)
FRONTIER_SOURCE_COLLECTION_PACKETS_SCHEMA_VERSION = (
    "chili.frontier-source-collection-packets.v1"
)
FRONTIER_MODEL_EVIDENCE_INTAKE_VALIDATE_COMMAND = (
    "python scripts/autopilot_frontier_model_evidence_intake.py "
    "--input-root {input_root} --allow-partial --json --no-write"
)
FRONTIER_MODEL_EVIDENCE_INTAKE_PUBLISH_COMMAND = (
    "python scripts/autopilot_frontier_model_evidence_intake.py "
    "--input-root {input_root} --publish-scorecards --json"
)
FRONTIER_SOURCE_RUNNER_COMMAND = (
    "python scripts/autopilot_frontier_source_runner.py --source-kind {source_kind} "
    "--source-auth-mode auto --json"
)
SOURCE_KINDS = tuple(REQUIRED_SOURCE_KINDS)
DEFAULT_SOURCE_KINDS = ("codex", "claude")
REQUIRED_SOURCE_FILES = ("metadata.json", "prompt_pack.md", "transcript.jsonl")


class FrontierSourceCollectionPacketError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class SourceState:
    source_kind: str
    source_dir: Path
    status: str
    missing_files: tuple[str, ...]
    raw_file_count: int


@dataclasses.dataclass(frozen=True)
class SourceAvailabilityNote:
    source_kind: str
    source_status: str = ""
    probe_status: str = ""
    blocker: str = ""
    credential_status: str = ""
    source_auth_mode: str = ""
    api_key_probe_status: str = ""
    source_runner_command: str = ""
    next_action: str = ""
    report: str = ""
    raw_source_root: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise FrontierSourceCollectionPacketError(f"{label} must be an object")
    return value


def _required_text(payload: Mapping[str, object], key: str, *, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FrontierSourceCollectionPacketError(f"{label}.{key} is required")
    return value.strip()


def _manifest_entries(manifest: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list):
        raise FrontierSourceCollectionPacketError("manifest.entries must be a list")
    entries: dict[str, Mapping[str, object]] = {}
    for index, raw_entry in enumerate(raw_entries, start=1):
        entry = _as_mapping(raw_entry, label=f"manifest.entries[{index}]")
        source_kind = _required_text(entry, "source_kind", label=f"manifest.entries[{index}]")
        entries[source_kind] = entry
    missing = [source_kind for source_kind in SOURCE_KINDS if source_kind not in entries]
    if missing:
        raise FrontierSourceCollectionPacketError(
            "manifest missing source entries: " + ", ".join(missing)
        )
    return entries


def _select_source_kinds(raw_source_kinds: Sequence[str] | None) -> tuple[str, ...]:
    if not raw_source_kinds:
        return DEFAULT_SOURCE_KINDS
    requested: list[str] = []
    for raw in raw_source_kinds:
        for part in str(raw).split(","):
            source_kind = part.strip()
            if not source_kind:
                continue
            if source_kind == "all":
                requested.extend(SOURCE_KINDS)
                continue
            if source_kind not in SOURCE_KINDS:
                raise FrontierSourceCollectionPacketError(
                    "source_kind must be one of all, " + ", ".join(SOURCE_KINDS)
                )
            requested.append(source_kind)
    unique: list[str] = []
    for source_kind in requested:
        if source_kind not in unique:
            unique.append(source_kind)
    if not unique:
        raise FrontierSourceCollectionPacketError("at least one source kind is required")
    return tuple(unique)


def _source_state(
    raw_source_root: Path,
    source_kind: str,
    *,
    expected_model_name: str,
    expected_prompt_pack_sha256: str,
) -> SourceState:
    source_dir = raw_source_root / source_kind
    missing_files: list[str] = []
    for filename in REQUIRED_SOURCE_FILES:
        path = source_dir / filename
        if not path.is_file():
            missing_files.append(str(path))
    raw_dir = source_dir / "raw"
    raw_file_count = 0
    if raw_dir.is_dir():
        raw_file_count = sum(1 for path in raw_dir.rglob("*") if path.is_file())
    if raw_file_count <= 0:
        missing_files.append(str(raw_dir / "*"))
    metadata_path = source_dir / "metadata.json"
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            missing_files.append(f"{metadata_path}: invalid metadata ({exc})")
        else:
            actual_model_name = str(metadata.get("model_name") or "")
            if actual_model_name != expected_model_name:
                missing_files.append(
                    f"{metadata_path}: model_name={actual_model_name or 'missing'} "
                    f"expected={expected_model_name}"
                )
            metadata_prompt_sha = str(metadata.get("prompt_pack_sha256") or "").lower()
            if metadata_prompt_sha != expected_prompt_pack_sha256.lower():
                missing_files.append(
                    f"{metadata_path}: prompt_pack_sha256={metadata_prompt_sha or 'missing'} "
                    f"expected={expected_prompt_pack_sha256.lower()}"
                )
    source_prompt_pack = source_dir / "prompt_pack.md"
    if source_prompt_pack.is_file():
        actual_prompt_sha = hashlib.sha256(source_prompt_pack.read_bytes()).hexdigest()
        if actual_prompt_sha != expected_prompt_pack_sha256.lower():
            missing_files.append(
                f"{source_prompt_pack}: sha256={actual_prompt_sha} "
                f"expected={expected_prompt_pack_sha256.lower()}"
            )
    status = (
        "ready"
        if not missing_files
        else "missing"
        if not source_dir.exists()
        else "partial"
    )
    return SourceState(
        source_kind=source_kind,
        source_dir=source_dir,
        status=status,
        missing_files=tuple(missing_files),
        raw_file_count=raw_file_count,
    )


def _command_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _response_staging_file(output_dir: Path, source_kind: str) -> Path:
    return output_dir / f"{source_kind}_all_cases_response.txt"


def _response_argument(response_path: Path | str) -> str:
    if isinstance(response_path, Path):
        return _command_path(response_path)
    return response_path


def _intake_validation_command(raw_source_root: Path) -> str:
    return FRONTIER_MODEL_EVIDENCE_INTAKE_VALIDATE_COMMAND.format(
        input_root=_command_path(raw_source_root)
    )


def _intake_publish_command(raw_source_root: Path) -> str:
    return FRONTIER_MODEL_EVIDENCE_INTAKE_PUBLISH_COMMAND.format(
        input_root=_command_path(raw_source_root)
    )


def _source_runner_command(source_kind: str) -> str:
    if source_kind not in {"codex", "claude", "local_model"}:
        return "none"
    return FRONTIER_SOURCE_RUNNER_COMMAND.format(source_kind=source_kind)


def _recorder_command(
    source_kind: str,
    response_path: Path | str | None = None,
    *,
    no_write: bool = False,
) -> str:
    response = response_path or f"<{source_kind}-response.txt>"
    command = (
        "python scripts/autopilot_frontier_source_evidence_recorder.py "
        f"--source-kind {source_kind} "
        "--case-id <case-id> "
        f"--response {_response_argument(response)} "
        f"--run-id <real-{source_kind}-run-id> "
        f"--source-command <exact-{source_kind}-command-or-session-export> --json"
    )
    if no_write:
        command += " --no-write"
    return command


def _all_cases_recorder_command(
    source_kind: str,
    response_path: Path | str | None = None,
    *,
    no_write: bool = False,
) -> str:
    response = response_path or f"<{source_kind}-all-cases-response.txt>"
    command = (
        "python scripts/autopilot_frontier_source_evidence_recorder.py "
        f"--source-kind {source_kind} "
        "--all-cases "
        f"--response {_response_argument(response)} "
        f"--run-id <real-{source_kind}-run-id> "
        f"--source-command <exact-{source_kind}-command-or-session-export> --json"
    )
    if no_write:
        command += " --no-write"
    return command


def _case_matrix_lines() -> list[str]:
    lines = [
        "| Case | Planned file | Required command |",
        "| --- | --- | --- |",
    ]
    for case in default_cases():
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_cell(case.case_id),
                    _escape_cell(case.incumbent.planned_file),
                    _escape_cell(_command_text(case.test_command)),
                ]
            )
            + " |"
        )
    return lines


def _packet_path(output_dir: Path, source_kind: str) -> Path:
    return output_dir / f"{source_kind}_collection_packet.md"


def _availability_label(source_kind: str) -> str:
    return source_kind.replace("_", " ").title()


def _availability_notes(
    availability_report: Path | None,
) -> dict[str, SourceAvailabilityNote]:
    if availability_report is None or not availability_report.is_file():
        return {}
    lines = availability_report.read_text(encoding="utf-8", errors="replace").splitlines()
    raw_source_root = ""
    for line in lines:
        if line.startswith("- Raw source root: "):
            raw_source_root = line.split(":", 1)[1].strip()
            break
    by_source: dict[str, dict[str, str]] = {}
    for source_kind in SOURCE_KINDS:
        label = _availability_label(source_kind)
        prefixes = {
            f"- {label} source status: ": "source_status",
            f"- {label} probe status: ": "probe_status",
            f"- {label} blocker: ": "blocker",
            f"- {label} credential status: ": "credential_status",
            f"- {label} source auth mode: ": "source_auth_mode",
            f"- {label} API-key probe status: ": "api_key_probe_status",
            f"- {label} source runner command: ": "source_runner_command",
            f"- {label} next action: ": "next_action",
        }
        for line in lines:
            for prefix, field in prefixes.items():
                if line.startswith(prefix):
                    by_source.setdefault(source_kind, {})[field] = line[len(prefix) :].strip()
    notes: dict[str, SourceAvailabilityNote] = {}
    for source_kind, fields in by_source.items():
        notes[source_kind] = SourceAvailabilityNote(
            source_kind=source_kind,
            source_status=fields.get("source_status", ""),
            probe_status=fields.get("probe_status", ""),
            blocker=fields.get("blocker", ""),
            credential_status=fields.get("credential_status", ""),
            source_auth_mode=fields.get("source_auth_mode", ""),
            api_key_probe_status=fields.get("api_key_probe_status", ""),
            source_runner_command=fields.get("source_runner_command", ""),
            next_action=fields.get("next_action", ""),
            report=str(availability_report),
            raw_source_root=raw_source_root,
        )
    return notes


def _render_packet(
    *,
    source_kind: str,
    model_name: str,
    prompt_pack: Path,
    prompt_pack_sha256: str,
    source_state: SourceState,
    availability_note: SourceAvailabilityNote | None,
    response_staging_file: Path,
    generated_utc: str,
    include_prompt: bool,
) -> str:
    raw_source_root = source_state.source_dir.parent
    dry_run_recorder_command = _all_cases_recorder_command(
        source_kind,
        response_staging_file,
        no_write=True,
    )
    write_recorder_command = _all_cases_recorder_command(source_kind, response_staging_file)
    single_case_fallback_command = _recorder_command(source_kind)
    validation_command = _intake_validation_command(raw_source_root)
    publish_command = _intake_publish_command(raw_source_root)
    source_runner_command = (
        availability_note.source_runner_command
        if availability_note and availability_note.source_runner_command
        else _source_runner_command(source_kind)
    )
    lines = [
        f"# CHILI {source_kind} Frontier Source Collection Packet",
        "",
        f"- Schema: {FRONTIER_SOURCE_COLLECTION_PACKET_SCHEMA_VERSION}",
        f"- Generated UTC: {generated_utc}",
        f"- Source kind: {source_kind}",
        f"- Model name: {model_name}",
        f"- Current source status: {source_state.status}",
        f"- Prompt pack: {prompt_pack}",
        f"- Prompt pack SHA-256: {prompt_pack_sha256}",
        f"- Raw source dir: {source_state.source_dir}",
        f"- Response staging file: {_command_path(response_staging_file)}",
        f"- Availability probe status: {availability_note.probe_status if availability_note else 'none'}",
        f"- Availability blocker: {availability_note.blocker if availability_note else 'none'}",
        f"- Availability source auth mode: {availability_note.source_auth_mode if availability_note and availability_note.source_auth_mode else 'none'}",
        f"- Availability API-key probe status: {availability_note.api_key_probe_status if availability_note and availability_note.api_key_probe_status else 'none'}",
        f"- Automated source runner command: {source_runner_command}",
        f"- Recommended recorder command: {dry_run_recorder_command}",
        f"- Write/import recorder command: {write_recorder_command}",
        f"- Single-case fallback command: {single_case_fallback_command}",
        f"- Intake validation command: {validation_command}",
        f"- Publish scorecards command: {publish_command}",
        "- Success criteria: metadata.json, transcript.jsonl, prompt_pack.md, and raw candidate artifacts validate through the frontier source recorder.",
        "- Permission boundary: evidence collection only; do not mutate source/tests, git, PR state, runtime, database, broker/API, deployment, release posture, or live trading.",
        "",
        "## Operator Steps",
        "",
        "1. Send the prompt pack listed above to the named model/source and ask it to answer every case in the pack.",
        f"2. Save the complete model response at `{_command_path(response_staging_file)}` outside the raw_sources folder.",
        "3. Run the recommended recorder command first; it includes `--no-write` so parser and provenance failures surface before evidence is changed.",
        "4. If the dry run passes, run the write/import recorder command. If the model only produced one case, use the single-case fallback command with `--case-id <case-id>`. Add `--drop-dir <drop-dir>` only when importing prebuilt raw drop files.",
        "5. Run the intake validation command and confirm no-write readiness before promotion.",
        "6. Run the publish scorecards command only after every required source is ready.",
        "7. Use `--overwrite` only after reviewing existing evidence for that source; ready sources should not be replaced casually.",
    ]
    if source_runner_command != "none":
        lines.append(
            "8. After source auth is healthy, use the automated source runner command "
            "to collect, stage, parse, and record the all-cases response in one guarded flow."
        )
    else:
        lines.append(
            "8. This source has no automated source runner; use the recorder/import commands after collecting its response."
        )
    lines.append("")
    if availability_note and availability_note.blocker and availability_note.blocker != "none":
        lines.extend(
            [
                "## Availability Recovery",
                "",
                f"- Report: {availability_note.report}",
                f"- Probe status: {availability_note.probe_status or 'none'}",
                f"- Blocker: {availability_note.blocker}",
                f"- Credential status: {availability_note.credential_status or 'none'}",
                f"- Source auth mode: {availability_note.source_auth_mode or 'none'}",
                f"- API-key probe status: {availability_note.api_key_probe_status or 'none'}",
                f"- Source runner command: {source_runner_command}",
                f"- Recovery action: {availability_note.next_action or 'none'}",
                "",
            ]
        )
    lines.extend(
        [
            "## All-Cases Response Contract",
            "",
            "- Return exactly one JSON object per case, either as JSONL or objects inside a JSON array.",
            f"- Every object must include `source_kind: {source_kind}`, `model_name: {model_name}`, `case_id`, `candidate_id`, and `patch`.",
            "- Include `planned_file`, `expected_changed_files`, and `declared_commands` exactly as listed in the case matrix when possible; CHILI verifies them when present.",
            "- The `patch` must be a unified diff scoped to the planned file for that case.",
            "- Empty or incomplete cases are allowed to be rejected by CHILI; do not invent validation results.",
            "- Do not wrap the response in Markdown fences, PR summaries, readiness claims, or placeholder template values.",
            "",
            "## Enforced Case Matrix",
            "",
        ]
    )
    lines.extend(_case_matrix_lines())
    lines.extend(
        [
            "",
            "## Post-Import Validation Loop",
            "",
            "1. Dry-run parse and provenance recording:",
            f"   `{dry_run_recorder_command}`",
            "2. Write/import only after the dry run passes:",
            f"   `{write_recorder_command}`",
            "3. Validate source readiness without writing:",
            f"   `{validation_command}`",
            "4. Publish scorecards only when all required sources are ready:",
            f"   `{publish_command}`",
            "",
            "## Required Transcript Evidence",
            "",
            "- At least 3 non-empty JSONL events.",
            f"- Include source kind `{source_kind}` and model name `{model_name}`.",
            "- Include the prompt-pack SHA-256, run id, case id, and final patch/drop decision.",
            "- Claims about PR state, readiness, or current-head status are not promotion evidence.",
            "",
            "## Missing Artifacts",
            "",
        ]
    )
    if source_state.missing_files:
        lines.extend(f"- {path}" for path in source_state.missing_files)
    else:
        lines.append(
            "- none; this source appears structurally ready, so rerun intake unless "
            "you intentionally want to replace its evidence."
        )
    if include_prompt:
        lines.extend(
            [
                "",
                "## Prompt Pack",
                "",
                "```markdown",
                prompt_pack.read_text(encoding="utf-8", errors="replace").rstrip(),
                "```",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def build_collection_packets(
    *,
    prompt_pack_bundle_dir: Path = DEFAULT_PROMPT_PACK_BUNDLE_DIR,
    raw_source_root: Path = DEFAULT_RAW_SOURCE_ROOT,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    availability_report: Path | None = DEFAULT_AVAILABILITY_REPORT,
    source_kinds: Sequence[str] | None = None,
    include_prompt: bool = False,
    write: bool = True,
) -> dict[str, object]:
    manifest_path = prompt_pack_bundle_dir / MANIFEST_FILE
    if not manifest_path.is_file():
        raise FrontierSourceCollectionPacketError(
            f"prompt-pack manifest does not exist: {manifest_path}"
        )
    try:
        manifest = validate_bundle_manifest(manifest_path)
    except (OSError, PromptPackBundleError) as exc:
        raise FrontierSourceCollectionPacketError(str(exc)) from exc
    entries = _manifest_entries(manifest)
    selected = _select_source_kinds(source_kinds)
    availability_by_source = _availability_notes(availability_report)
    generated_utc = _utc_now()
    packets: list[dict[str, object]] = []
    if write:
        output_dir.mkdir(parents=True, exist_ok=True)
    for source_kind in selected:
        entry = entries[source_kind]
        model_name = _required_text(entry, "model_name", label=f"entries.{source_kind}")
        rel_prompt_pack = _required_text(entry, "path", label=f"entries.{source_kind}")
        prompt_pack = (prompt_pack_bundle_dir / rel_prompt_pack).resolve()
        prompt_pack_sha256 = _required_text(entry, "sha256", label=f"entries.{source_kind}")
        state = _source_state(
            raw_source_root,
            source_kind,
            expected_model_name=model_name,
            expected_prompt_pack_sha256=prompt_pack_sha256,
        )
        availability_note = availability_by_source.get(source_kind)
        if (
            availability_note
            and availability_note.raw_source_root
            and Path(availability_note.raw_source_root).resolve() != raw_source_root.resolve()
        ):
            availability_note = None
        packet_path = _packet_path(output_dir, source_kind)
        response_staging_file = _response_staging_file(output_dir, source_kind)
        packet_markdown = _render_packet(
            source_kind=source_kind,
            model_name=model_name,
            prompt_pack=prompt_pack,
            prompt_pack_sha256=prompt_pack_sha256,
            source_state=state,
            availability_note=availability_note,
            response_staging_file=response_staging_file,
            generated_utc=generated_utc,
            include_prompt=include_prompt,
        )
        if write:
            packet_path.write_text(packet_markdown, encoding="utf-8")
        packets.append(
            {
                "source_kind": source_kind,
                "model_name": model_name,
                "status": state.status,
                "packet": str(packet_path),
                "prompt_pack": str(prompt_pack),
                "prompt_pack_sha256": prompt_pack_sha256,
                "raw_source_dir": str(state.source_dir),
                "response_staging_file": str(response_staging_file),
                "raw_file_count": state.raw_file_count,
                "missing_files": list(state.missing_files),
                "availability_probe_status": availability_note.probe_status
                if availability_note
                else "",
                "availability_blocker": availability_note.blocker if availability_note else "",
                "availability_source_auth_mode": availability_note.source_auth_mode
                if availability_note
                else "",
                "availability_api_key_probe_status": availability_note.api_key_probe_status
                if availability_note
                else "",
                "availability_next_action": availability_note.next_action
                if availability_note
                else "",
                "source_runner_command": (
                    availability_note.source_runner_command
                    if availability_note
                    and availability_note.source_runner_command
                    and availability_note.source_runner_command.lower() != "none"
                    else _source_runner_command(source_kind)
                ),
                "recorder_command": _recorder_command(source_kind),
                "dry_run_recorder_command": _all_cases_recorder_command(
                    source_kind,
                    response_staging_file,
                    no_write=True,
                ),
                "all_cases_recorder_command": _all_cases_recorder_command(
                    source_kind,
                    response_staging_file,
                ),
                "validation_command": _intake_validation_command(raw_source_root),
                "publish_command": _intake_publish_command(raw_source_root),
            }
        )
    return {
        "schema": FRONTIER_SOURCE_COLLECTION_PACKETS_SCHEMA_VERSION,
        "generated_utc": generated_utc,
        "status": "passed",
        "write": bool(write),
        "source_kinds": list(selected),
        "prompt_pack_manifest": str(manifest_path),
        "availability_report": str(availability_report) if availability_report else "",
        "raw_source_root": str(raw_source_root),
        "output_dir": str(output_dir),
        "packets": packets,
        "next_action": (
            "Use each non-ready packet with its named model/source, save the all-cases "
            "response to the staging file, dry-run the recorder, write/import only after "
            "that passes, validate intake with --no-write, then publish scorecards only "
            "after every required source is ready."
        ),
        "permission_boundary": (
            "writes collection instructions only; does not run models, edit source/tests, "
            "use git/PR tools, restart runtime, deploy, or touch live trading"
        ),
    }


def render_summary(summary: Mapping[str, object]) -> str:
    lines = [
        "# CHILI Frontier Source Collection Packets",
        "",
        f"- Schema: {summary.get('schema')}",
        f"- Generated UTC: {summary.get('generated_utc')}",
        f"- Status: {summary.get('status')}",
        f"- Write mode: {summary.get('write')}",
        f"- Source kinds: {', '.join(str(item) for item in summary.get('source_kinds') or [])}",
        f"- Prompt pack manifest: {summary.get('prompt_pack_manifest')}",
        f"- Availability report: {summary.get('availability_report') or 'none'}",
        f"- Raw source root: {summary.get('raw_source_root')}",
        f"- Next action: {summary.get('next_action')}",
        f"- Permission boundary: {summary.get('permission_boundary')}",
        "",
        "| Source | Model | Status | Availability | Packet | Staging file | Source runner | Dry-run recorder command | Write/import command | Intake validation | Publish command |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    packets = summary.get("packets")
    if isinstance(packets, list):
        for raw_packet in packets:
            if not isinstance(raw_packet, Mapping):
                continue
            lines.append(
                "| "
                + " | ".join(
                    [
                        _escape_cell(str(raw_packet.get("source_kind") or "")),
                        _escape_cell(str(raw_packet.get("model_name") or "")),
                        _escape_cell(str(raw_packet.get("status") or "")),
                        _escape_cell(
                            str(raw_packet.get("availability_blocker") or "")
                            or str(raw_packet.get("availability_probe_status") or "")
                        ),
                        _escape_cell(str(raw_packet.get("packet") or "")),
                        _escape_cell(str(raw_packet.get("response_staging_file") or "")),
                        _escape_cell(str(raw_packet.get("source_runner_command") or "")),
                        _escape_cell(str(raw_packet.get("dry_run_recorder_command") or "")),
                        _escape_cell(str(raw_packet.get("all_cases_recorder_command") or "")),
                        _escape_cell(str(raw_packet.get("validation_command") or "")),
                        _escape_cell(str(raw_packet.get("publish_command") or "")),
                    ]
                )
                + " |"
            )
    lines.append("")
    return "\n".join(lines)


def write_summary(markdown: str, output_path: Path = DEFAULT_SUMMARY_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build copy-ready collection packets for frontier model source evidence."
    )
    parser.add_argument("--prompt-pack-bundle-dir", type=Path, default=DEFAULT_PROMPT_PACK_BUNDLE_DIR)
    parser.add_argument("--raw-source-root", type=Path, default=DEFAULT_RAW_SOURCE_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--summary-output", type=Path, default=DEFAULT_SUMMARY_OUTPUT)
    parser.add_argument("--availability-report", type=Path, default=DEFAULT_AVAILABILITY_REPORT)
    parser.add_argument(
        "--source-kind",
        action="append",
        help="Source to packetize. May be repeated or comma-separated. Use all for every source.",
    )
    parser.add_argument("--include-prompt", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    try:
        summary = build_collection_packets(
            prompt_pack_bundle_dir=args.prompt_pack_bundle_dir,
            raw_source_root=args.raw_source_root,
            output_dir=args.output_dir,
            availability_report=args.availability_report,
            source_kinds=args.source_kind,
            include_prompt=args.include_prompt,
            write=not args.no_write,
        )
    except FrontierSourceCollectionPacketError as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "schema": FRONTIER_SOURCE_COLLECTION_PACKETS_SCHEMA_VERSION,
                        "status": "failed",
                        "error": str(exc),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"[frontier-source-collection-packet] failed: {exc}", file=sys.stderr)
        return 1

    markdown = render_summary(summary)
    if not args.no_write:
        write_summary(markdown, args.summary_output)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
