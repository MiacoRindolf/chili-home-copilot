from __future__ import annotations

import argparse
import json
import shutil
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
    MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
    sha256_file,
    validate_prompt_pack_markdown,
)
from scripts.autopilot_model_candidate_drop_collector import (  # noqa: E402
    DropCollectionError,
    collect_candidate_drops,
)


DEFAULT_SOURCE_DIR = (
    REPO_ROOT
    / "project_ws"
    / "AgentOps"
    / "frontier_model_evidence_intake"
    / "raw_sources"
    / "local_model"
)
DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "LOCAL_MODEL_EVIDENCE_RECORDING.md"
LOCAL_MODEL_EVIDENCE_RECORDER_SCHEMA_VERSION = "chili.local-model-evidence-recorder.v1"
SOURCE_KIND = "local_model"
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


class LocalModelEvidenceRecorderError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LocalModelEvidenceRecorderError(f"{label} is required")
    text = value.strip()
    _reject_placeholder_text(text, label=label)
    return text


def _reject_placeholder_text(text: str, *, label: str) -> None:
    lowered = text.lower()
    for marker in PLACEHOLDER_MARKERS:
        if marker in lowered:
            raise LocalModelEvidenceRecorderError(f"{label} still contains template placeholder: {marker}")


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LocalModelEvidenceRecorderError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise LocalModelEvidenceRecorderError(f"{path}: JSON must be an object")
    return payload


def _model_name_from_prompt_pack(prompt_pack: Path) -> str:
    if not prompt_pack.is_file():
        raise LocalModelEvidenceRecorderError(f"prompt pack missing: {prompt_pack}")
    text = prompt_pack.read_text(encoding="utf-8", errors="replace")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.lower().startswith("- model name:"):
            return _required_text(line.split(":", 1)[1], label="prompt_pack.model_name")
    raise LocalModelEvidenceRecorderError(f"prompt pack does not declare a model name: {prompt_pack}")


def _validate_prompt_pack(prompt_pack: Path, *, model_name: str) -> str:
    text = prompt_pack.read_text(encoding="utf-8", errors="replace")
    try:
        validate_prompt_pack_markdown(
            text,
            source_kind=SOURCE_KIND,
            model_name=model_name,
            label=str(prompt_pack),
        )
    except ArtifactBuildError as exc:
        raise LocalModelEvidenceRecorderError(str(exc)) from exc
    return sha256_file(prompt_pack)


def _safe_relative_copy_files(input_dir: Path) -> list[tuple[Path, Path]]:
    if not input_dir.is_dir():
        raise LocalModelEvidenceRecorderError(f"drop directory does not exist: {input_dir}")
    resolved_input = input_dir.resolve()
    files: list[tuple[Path, Path]] = []
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved_input not in resolved.parents and resolved != resolved_input:
            raise LocalModelEvidenceRecorderError(f"drop file escapes input directory: {path}")
        rel = path.relative_to(input_dir)
        files.append((path, rel))
    if not files:
        raise LocalModelEvidenceRecorderError(f"drop directory is empty: {input_dir}")
    json_files = [source for source, _rel in files if source.suffix.lower() == ".json"]
    if not json_files:
        raise LocalModelEvidenceRecorderError(
            f"drop directory has no {MODEL_CANDIDATE_DROP_SCHEMA_VERSION} JSON drops: {input_dir}"
        )
    return files


def _reject_placeholder_files(files: Sequence[tuple[Path, Path]]) -> None:
    for source, rel in files:
        if source.suffix.lower() not in {".json", ".jsonl", ".md", ".txt"}:
            continue
        text = source.read_text(encoding="utf-8", errors="replace")
        _reject_placeholder_text(text, label=f"drop artifact {rel.as_posix()}")
        if source.suffix.lower() == ".json":
            payload = _read_json(source)
            if payload.get("schema") != MODEL_CANDIDATE_DROP_SCHEMA_VERSION:
                raise LocalModelEvidenceRecorderError(
                    f"{rel.as_posix()}.schema is {payload.get('schema') or 'missing'} "
                    f"instead of {MODEL_CANDIDATE_DROP_SCHEMA_VERSION}"
                )
            if payload.get("source_kind") != SOURCE_KIND:
                raise LocalModelEvidenceRecorderError(
                    f"{rel.as_posix()}.source_kind must be {SOURCE_KIND}"
                )


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
    model_name: str,
    run_id: str,
    source_command: str,
    prompt_pack_sha256: str,
    recorded_at: str,
) -> None:
    payload = {
        "model_name": model_name,
        "prompt_pack_file": PROMPT_PACK_FILE,
        "prompt_pack_sha256": prompt_pack_sha256,
        "raw_dir": RAW_DIR,
        "recorded_at": recorded_at,
        "recorder": "autopilot_local_model_evidence_recorder",
        "run_id": run_id,
        "source_command": source_command,
        "source_kind": SOURCE_KIND,
        "transcript_file": TRANSCRIPT_FILE,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _build_transcript_from_response(
    response_path: Path,
    *,
    model_name: str,
    run_id: str,
    source_command: str,
    prompt_pack_sha256: str,
    raw_files: Sequence[str],
) -> str:
    if not response_path.is_file():
        raise LocalModelEvidenceRecorderError(f"response file does not exist: {response_path}")
    response_text = response_path.read_text(encoding="utf-8", errors="replace")
    _reject_placeholder_text(response_text, label=str(response_path))
    events = [
        {
            "content": (
                "Prompt pack prompt_pack.md was sent to the local model for CHILI "
                f"candidate repair evidence with sha256 {prompt_pack_sha256}."
            ),
            "event": "prompt_sent",
            "model_name": model_name,
            "prompt_pack_file": PROMPT_PACK_FILE,
            "prompt_pack_sha256": prompt_pack_sha256,
            "role": "user",
            "run_id": run_id,
            "source_command": source_command,
            "source_kind": SOURCE_KIND,
        },
        {
            "content": response_text,
            "event": "assistant_response",
            "model_name": model_name,
            "role": "assistant",
            "run_id": run_id,
            "source_kind": SOURCE_KIND,
        },
        {
            "event": "model_output_recorded",
            "model_name": model_name,
            "output": "raw candidate files: " + ", ".join(raw_files),
            "raw_files": list(raw_files),
            "run_id": run_id,
            "source_kind": SOURCE_KIND,
        },
    ]
    return "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"


def _copy_or_build_transcript(
    *,
    transcript_path: Path | None,
    response_path: Path | None,
    destination: Path,
    model_name: str,
    run_id: str,
    source_command: str,
    prompt_pack_sha256: str,
    raw_files: Sequence[str],
) -> None:
    if transcript_path and response_path:
        raise LocalModelEvidenceRecorderError("use --transcript or --response, not both")
    if transcript_path:
        if not transcript_path.is_file():
            raise LocalModelEvidenceRecorderError(f"transcript does not exist: {transcript_path}")
        text = transcript_path.read_text(encoding="utf-8", errors="replace")
        _reject_placeholder_text(text, label=str(transcript_path))
        destination.write_text(text, encoding="utf-8")
        return
    if response_path:
        destination.write_text(
            _build_transcript_from_response(
                response_path,
                model_name=model_name,
                run_id=run_id,
                source_command=source_command,
                prompt_pack_sha256=prompt_pack_sha256,
                raw_files=raw_files,
            ),
            encoding="utf-8",
        )
        return
    raise LocalModelEvidenceRecorderError("either --transcript or --response is required")


def _prepare_recording_target(
    *,
    target_source_dir: Path,
    prompt_pack: Path,
    drop_dir: Path,
    transcript_path: Path | None,
    response_path: Path | None,
    model_name: str,
    run_id: str,
    source_command: str,
    prompt_pack_sha256: str,
    overwrite: bool,
) -> dict[str, object]:
    conflicts = _target_conflicts(target_source_dir, overwrite=overwrite)
    if conflicts:
        raise LocalModelEvidenceRecorderError(
            "existing local-model evidence would be overwritten; rerun with --overwrite "
            "only after reviewing: " + ", ".join(conflicts)
        )
    files = _safe_relative_copy_files(drop_dir)
    _reject_placeholder_files(files)
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
        model_name=model_name,
        run_id=run_id,
        source_command=source_command,
        prompt_pack_sha256=prompt_pack_sha256,
        recorded_at=recorded_at,
    )
    _copy_or_build_transcript(
        transcript_path=transcript_path,
        response_path=response_path,
        destination=target_source_dir / TRANSCRIPT_FILE,
        model_name=model_name,
        run_id=run_id,
        source_command=source_command,
        prompt_pack_sha256=prompt_pack_sha256,
        raw_files=raw_files,
    )
    with tempfile.TemporaryDirectory(prefix="chili_local_model_recorder_validate_") as tmp:
        _drops, manifest = collect_candidate_drops(
            input_dir=raw_dir,
            output_dir=Path(tmp) / "validated",
            prompt_pack_path=target_source_dir / PROMPT_PACK_FILE,
            transcript_path=target_source_dir / TRANSCRIPT_FILE,
            source_kind=SOURCE_KIND,
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


def record_local_model_evidence(
    *,
    source_dir: Path = DEFAULT_SOURCE_DIR,
    drop_dir: Path,
    prompt_pack_path: Path | None = None,
    transcript_path: Path | None = None,
    response_path: Path | None = None,
    model_name: str | None = None,
    run_id: str,
    source_command: str,
    write: bool = True,
    overwrite: bool = False,
) -> dict[str, object]:
    prompt_pack = prompt_pack_path or source_dir / PROMPT_PACK_FILE
    resolved_model_name = _required_text(
        model_name or _model_name_from_prompt_pack(prompt_pack),
        label="model_name",
    )
    clean_run_id = _required_text(run_id, label="run_id")
    clean_source_command = _required_text(source_command, label="source_command")
    prompt_pack_sha256 = _validate_prompt_pack(prompt_pack, model_name=resolved_model_name)

    if write:
        target_source_dir = source_dir
        target_prompt_pack = target_source_dir / PROMPT_PACK_FILE
        if (
            prompt_pack_path is not None
            and target_prompt_pack.exists()
            and prompt_pack.resolve() != target_prompt_pack.resolve()
            and sha256_file(prompt_pack) != sha256_file(target_prompt_pack)
            and not overwrite
        ):
            raise LocalModelEvidenceRecorderError(
                "existing prompt_pack.md would be overwritten; rerun with --overwrite "
                "only after reviewing the run-specific prompt pack"
            )
        try:
            recording = _prepare_recording_target(
                target_source_dir=target_source_dir,
                prompt_pack=prompt_pack,
                drop_dir=drop_dir,
                transcript_path=transcript_path,
                response_path=response_path,
                model_name=resolved_model_name,
                run_id=clean_run_id,
                source_command=clean_source_command,
                prompt_pack_sha256=prompt_pack_sha256,
                overwrite=overwrite,
            )
        except DropCollectionError as exc:
            raise LocalModelEvidenceRecorderError(str(exc)) from exc
    else:
        with tempfile.TemporaryDirectory(prefix="chili_local_model_recorder_dry_run_") as tmp:
            target_source_dir = Path(tmp) / SOURCE_KIND
            target_source_dir.mkdir(parents=True)
            shutil.copyfile(prompt_pack, target_source_dir / PROMPT_PACK_FILE)
            try:
                recording = _prepare_recording_target(
                    target_source_dir=target_source_dir,
                    prompt_pack=target_source_dir / PROMPT_PACK_FILE,
                    drop_dir=drop_dir,
                    transcript_path=transcript_path,
                    response_path=response_path,
                    model_name=resolved_model_name,
                    run_id=clean_run_id,
                    source_command=clean_source_command,
                    prompt_pack_sha256=prompt_pack_sha256,
                    overwrite=True,
                )
            except DropCollectionError as exc:
                raise LocalModelEvidenceRecorderError(str(exc)) from exc
            recording = dict(recording)
            recording["metadata"] = str(source_dir / METADATA_FILE)
            recording["transcript"] = str(source_dir / TRANSCRIPT_FILE)
            recording["raw_dir"] = str(source_dir / RAW_DIR)

    manifest = recording["validation_manifest"]
    cases = int(manifest.get("cases") or 0) if isinstance(manifest, Mapping) else 0
    return {
        "schema": LOCAL_MODEL_EVIDENCE_RECORDER_SCHEMA_VERSION,
        "generated_utc": _utc_now(),
        "status": "passed",
        "write": bool(write),
        "source_kind": SOURCE_KIND,
        "source_dir": str(source_dir),
        "model_name": resolved_model_name,
        "run_id": clean_run_id,
        "source_command": clean_source_command,
        "input_prompt_pack": str(prompt_pack),
        "prompt_pack": str(source_dir / PROMPT_PACK_FILE),
        "prompt_pack_sha256": prompt_pack_sha256,
        "drop_dir": str(drop_dir),
        "metadata": recording["metadata"],
        "transcript": recording["transcript"],
        "raw_dir": recording["raw_dir"],
        "raw_file_count": recording["raw_file_count"],
        "raw_files": recording["raw_files"],
        "cases": cases,
        "validated_with_provenance": True,
        "promotion_ready": False,
        "next_action": (
            "Record matching Codex and Claude drops, then run "
            "scripts/autopilot_frontier_model_evidence_intake.py --publish-scorecards."
        ),
        "permission_boundary": (
            "records and validates local-model evidence only; it does not run models, "
            "edit source/tests, restart runtime, use git/PR tools, deploy, or touch live trading"
        ),
    }


def render_recording_summary(summary: Mapping[str, object]) -> str:
    lines = [
        "# CHILI Local Model Evidence Recording",
        "",
        f"- Schema: {summary.get('schema')}",
        f"- Generated UTC: {summary.get('generated_utc')}",
        f"- Status: {summary.get('status')}",
        f"- Write mode: {summary.get('write')}",
        f"- Source kind: {summary.get('source_kind')}",
        f"- Model: {summary.get('model_name')}",
        f"- Run id: {summary.get('run_id')}",
        f"- Cases: {summary.get('cases')}",
        f"- Validated with provenance: {summary.get('validated_with_provenance')}",
        f"- Promotion ready: {summary.get('promotion_ready')}",
        f"- Source dir: {summary.get('source_dir')}",
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
            "Record transcript-bound local-model candidate drops into the frontier "
            "evidence intake folder without running models or creating promotion evidence."
        )
    )
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--drop-dir", type=Path, required=True)
    parser.add_argument("--prompt-pack", type=Path)
    parser.add_argument("--transcript", type=Path)
    parser.add_argument("--response", type=Path)
    parser.add_argument("--model-name")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-command", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        summary = record_local_model_evidence(
            source_dir=args.source_dir,
            drop_dir=args.drop_dir,
            prompt_pack_path=args.prompt_pack,
            transcript_path=args.transcript,
            response_path=args.response,
            model_name=args.model_name,
            run_id=args.run_id,
            source_command=args.source_command,
            write=not args.no_write,
            overwrite=args.overwrite,
        )
    except LocalModelEvidenceRecorderError as exc:
        print(f"local model evidence recorder error: {exc}", file=sys.stderr)
        return 2

    markdown = render_recording_summary(summary)
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
