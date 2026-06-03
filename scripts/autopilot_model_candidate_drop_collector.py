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

from scripts.autopilot_model_candidate_artifact_bakeoff import ALLOWED_SOURCE_KINDS  # noqa: E402
from scripts.autopilot_model_candidate_artifact_builder import (  # noqa: E402
    ArtifactBuildError,
    MODEL_CANDIDATE_DROP_PROMPT_PACK_SCHEMA_VERSION,
    MODEL_CANDIDATE_DROP_PROVENANCE_SCHEMA_VERSION,
    MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
    build_artifact,
    load_drops,
    render_prompt_pack,
    sha256_file,
    synthetic_drops,
)


DEFAULT_OUTPUT_DIR = REPO_ROOT / "project_ws" / "AgentOps" / "model_candidate_collected_drops"
DEFAULT_MANIFEST_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "MODEL_CANDIDATE_DROP_COLLECTION_MANIFEST.json"
MODEL_CANDIDATE_DROP_COLLECTOR_SCHEMA_VERSION = "chili.model-candidate-drop-collector.v1"
COLLECTOR_NAME = "autopilot_model_candidate_drop_collector"
TRANSCRIPT_MIN_EVENTS = 3


class DropCollectionError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DropCollectionError(f"{label} must be an object")
    return value


def _required_text(payload: Mapping[str, object], key: str, *, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DropCollectionError(f"{label}.{key} is required")
    return value.strip()


def _safe_relative_file(base_dir: Path, raw_path: str, *, field_name: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        raise DropCollectionError(f"{field_name} must be relative to its drop JSON")
    resolved_base = base_dir.resolve()
    resolved = (resolved_base / candidate).resolve()
    if resolved_base not in resolved.parents and resolved != resolved_base:
        raise DropCollectionError(f"{field_name} escapes its drop directory")
    if not resolved.is_file():
        raise DropCollectionError(f"{field_name} does not exist: {raw_path}")
    return resolved


def _safe_name(value: object, *, fallback: str) -> str:
    raw = str(value or fallback).strip().lower()
    safe = re.sub(r"[^a-z0-9._-]+", "-", raw).strip(".-")
    return safe or fallback


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == destination.resolve():
        return
    shutil.copyfile(source, destination)


def _validate_transcript_quality(
    transcript_path: Path,
    *,
    run_id: str,
    model_name: str,
    source_kind: str,
) -> int:
    lines = [
        line.strip()
        for line in transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    if len(lines) < TRANSCRIPT_MIN_EVENTS:
        raise DropCollectionError(
            f"transcript must contain at least {TRANSCRIPT_MIN_EVENTS} non-empty events"
        )
    combined = "\n".join(lines).lower()
    if not any(marker in combined for marker in ("prompt", "user", "input")):
        raise DropCollectionError("transcript must include prompt or user input evidence")
    if not any(marker in combined for marker in ("assistant", "response", "completion", "output", "patch")):
        raise DropCollectionError("transcript must include model response evidence")
    for field_name, expected in (
        ("run_id", run_id),
        ("model_name", model_name),
        ("source_kind", source_kind),
    ):
        if expected.lower() not in combined:
            raise DropCollectionError(f"transcript must include {field_name} evidence: {expected}")
    return len(lines)


def _copy_patch_file(drop: Mapping[str, object], *, output_dir: Path, label: str) -> str | None:
    raw_patch_file = drop.get("patch_file")
    if not isinstance(raw_patch_file, str) or not raw_patch_file.strip():
        return None
    drop_dir = Path(str(drop.get("_drop_dir") or "."))
    source = _safe_relative_file(drop_dir, raw_patch_file, field_name=f"{label}.patch_file")
    case_id = _safe_name(drop.get("case_id"), fallback=f"case-{label}")
    destination_name = f"{case_id}.patch"
    _copy_file(source, output_dir / destination_name)
    return destination_name


def _drop_without_internal_fields(drop: Mapping[str, object]) -> dict[str, object]:
    return {
        str(key): value
        for key, value in drop.items()
        if not str(key).startswith("_") and str(key) != "provenance"
    }


def _require_raw_drop_identity(
    payload: Mapping[str, object],
    *,
    label: str,
    source_kind: str,
    model_name: str,
) -> None:
    raw_source_kind = _required_text(payload, "source_kind", label=label)
    if raw_source_kind != source_kind:
        raise DropCollectionError(
            f"{label}.source_kind {raw_source_kind} does not match collection source_kind {source_kind}"
        )
    raw_model_name = _required_text(payload, "model_name", label=label)
    if raw_model_name != model_name:
        raise DropCollectionError(
            f"{label}.model_name {raw_model_name} does not match collection model_name {model_name}"
        )


def _stamp_drop(
    drop: Mapping[str, object],
    *,
    index: int,
    output_dir: Path,
    prompt_pack_path: Path,
    prompt_pack_name: str,
    transcript_name: str,
    transcript_sha256: str,
    transcript_events: int,
    source_kind: str,
    model_name: str,
    run_id: str,
    source_command: str,
    collected_at: str,
) -> dict[str, object]:
    label = f"drop[{index}]"
    payload = _drop_without_internal_fields(drop)
    schema = payload.get("schema") or MODEL_CANDIDATE_DROP_SCHEMA_VERSION
    if schema != MODEL_CANDIDATE_DROP_SCHEMA_VERSION:
        raise DropCollectionError(
            f"{label}.schema is {schema} instead of {MODEL_CANDIDATE_DROP_SCHEMA_VERSION}"
        )
    _require_raw_drop_identity(
        payload,
        label=label,
        source_kind=source_kind,
        model_name=model_name,
    )
    case_id = _required_text(payload, "case_id", label=label)
    patch_file = _copy_patch_file(drop, output_dir=output_dir, label=label)
    if patch_file:
        payload.pop("patch", None)
        payload["patch_file"] = patch_file

    payload["schema"] = MODEL_CANDIDATE_DROP_SCHEMA_VERSION
    payload["case_id"] = case_id
    payload["candidate_id"] = str(payload.get("candidate_id") or f"{source_kind}-{case_id}")
    payload["model_name"] = model_name
    payload["source_kind"] = source_kind
    payload["collected_at"] = str(payload.get("collected_at") or collected_at)
    payload["provenance"] = {
        "schema": MODEL_CANDIDATE_DROP_PROVENANCE_SCHEMA_VERSION,
        "prompt_pack_schema": MODEL_CANDIDATE_DROP_PROMPT_PACK_SCHEMA_VERSION,
        "prompt_pack_file": prompt_pack_name,
        "prompt_pack_sha256": sha256_file(prompt_pack_path),
        "run_id": run_id,
        "collector": COLLECTOR_NAME,
        "source_command": source_command,
        "transcript_file": transcript_name,
        "transcript_sha256": transcript_sha256,
        "transcript_events": transcript_events,
    }
    return payload


def collect_candidate_drops(
    *,
    input_dir: Path,
    output_dir: Path,
    prompt_pack_path: Path,
    transcript_path: Path,
    source_kind: str,
    model_name: str,
    run_id: str,
    source_command: str,
    allow_partial: bool = False,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    if source_kind not in ALLOWED_SOURCE_KINDS or source_kind == "fixture":
        raise DropCollectionError("source_kind must be one of codex, claude, local_model, other")
    model_name = _required_text({"model_name": model_name}, "model_name", label="collector")
    run_id = _required_text({"run_id": run_id}, "run_id", label="collector")
    source_command = _required_text(
        {"source_command": source_command},
        "source_command",
        label="collector",
    )
    if not prompt_pack_path.is_file():
        raise DropCollectionError(f"prompt pack does not exist: {prompt_pack_path}")
    if not transcript_path.is_file():
        raise DropCollectionError(f"transcript does not exist: {transcript_path}")
    transcript_events = _validate_transcript_quality(
        transcript_path,
        run_id=run_id,
        model_name=model_name,
        source_kind=source_kind,
    )
    drops = load_drops(input_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_pack_name = f"{_safe_name(run_id, fallback='model-run')}.prompt-pack.md"
    _copy_file(prompt_pack_path, output_dir / prompt_pack_name)
    transcript_name = f"{_safe_name(run_id, fallback='model-run')}.transcript.jsonl"
    _copy_file(transcript_path, output_dir / transcript_name)
    transcript_sha256 = sha256_file(transcript_path)
    collected_at = _utc_now()
    stamped: list[dict[str, object]] = []
    for index, drop in enumerate(drops, start=1):
        stamped_drop = _stamp_drop(
            drop,
            index=index,
            output_dir=output_dir,
            prompt_pack_path=prompt_pack_path,
            prompt_pack_name=prompt_pack_name,
            transcript_name=transcript_name,
            transcript_sha256=transcript_sha256,
            transcript_events=transcript_events,
            source_kind=source_kind,
            model_name=model_name,
            run_id=run_id,
            source_command=source_command,
            collected_at=collected_at,
        )
        stamped.append(stamped_drop)
        case_id = _safe_name(stamped_drop.get("case_id"), fallback=f"case-{index}")
        output_path = output_dir / f"{source_kind}-{case_id}.json"
        output_path.write_text(
            json.dumps(stamped_drop, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    validation_drops = [
        {**drop, "_drop_dir": str(output_dir), "_drop_path": "<collector>"}
        for drop in stamped
    ]
    artifact = build_artifact(
        validation_drops,
        allow_partial=allow_partial,
        require_provenance=True,
        prompt_pack_path=prompt_pack_path,
    )
    manifest = {
        "schema": MODEL_CANDIDATE_DROP_COLLECTOR_SCHEMA_VERSION,
        "generated_utc": collected_at,
        "source_kind": source_kind,
        "model_name": model_name,
        "run_id": run_id,
        "source_command": source_command,
        "collector": COLLECTOR_NAME,
        "prompt_pack": str(prompt_pack_path),
        "prompt_pack_file": prompt_pack_name,
        "prompt_pack_sha256": sha256_file(prompt_pack_path),
        "transcript_file": transcript_name,
        "transcript_sha256": transcript_sha256,
        "transcript_events": transcript_events,
        "output_dir": str(output_dir),
        "cases": len(stamped),
        "artifact_schema": artifact.get("schema"),
        "evaluation_mode": artifact.get("evaluation_mode"),
        "validated_with_provenance": True,
    }
    return stamped, manifest


def write_manifest(manifest: Mapping[str, object], output_path: Path = DEFAULT_MANIFEST_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def _self_test_context(root: Path) -> tuple[Path, Path, Path, Path]:
    input_dir = root / "raw"
    output_dir = root / "collected"
    input_dir.mkdir(parents=True)
    drop = dict(synthetic_drops()[0])
    drop["model_name"] = "codex-collector-self-test"
    prompt_pack = root / "prompt_pack.md"
    prompt_pack.write_text(
        render_prompt_pack(
            source_kind=str(drop["source_kind"]),
            model_name=str(drop["model_name"]),
        ),
        encoding="utf-8",
    )
    transcript = root / "run.transcript.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(event, sort_keys=True)
            for event in (
                {
                    "run_id": "collector-self-test",
                    "source_kind": "codex",
                    "model_name": "codex-collector-self-test",
                    "case_id": drop["case_id"],
                    "candidate_id": drop["candidate_id"],
                    "event": "prompt_sent",
                    "role": "user",
                    "content": "Run the CHILI model candidate prompt pack.",
                },
                {
                    "run_id": "collector-self-test",
                    "source_kind": "codex",
                    "model_name": "codex-collector-self-test",
                    "event": "assistant_response",
                    "role": "assistant",
                    "content": "I will emit the candidate patch drop.",
                },
                {
                    "run_id": "collector-self-test",
                    "source_kind": "codex",
                    "model_name": "codex-collector-self-test",
                    "event": "model_output_received",
                    "candidate_id": drop["candidate_id"],
                    "output": "candidate drop JSON plus patch file written",
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    patch_path = input_dir / "candidate.patch"
    patch_path.write_text(str(drop.pop("patch")), encoding="utf-8")
    drop["patch_file"] = patch_path.name
    drop.pop("provenance", None)
    (input_dir / "drop.json").write_text(
        json.dumps(_drop_without_internal_fields(drop), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return input_dir, output_dir, prompt_pack, transcript


def run_self_test() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="chili_model_candidate_collector_") as raw_root:
        input_dir, output_dir, prompt_pack, transcript = _self_test_context(Path(raw_root))
        _, manifest = collect_candidate_drops(
            input_dir=input_dir,
            output_dir=output_dir,
            prompt_pack_path=prompt_pack,
            transcript_path=transcript,
            source_kind="codex",
            model_name="codex-collector-self-test",
            run_id="collector-self-test",
            source_command="codex --prompt prompt_pack.md",
            allow_partial=True,
        )
        return dict(manifest)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect raw model candidate drops into a provenance-stamped replay bundle."
    )
    parser.add_argument("--input-dir", type=Path, help="Directory with raw candidate-drop JSON files.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--prompt-pack", type=Path)
    parser.add_argument("--transcript", type=Path)
    parser.add_argument("--source-kind", default="codex")
    parser.add_argument("--model-name", default="candidate-model")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--source-command", default="")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--manifest-output", type=Path, default=DEFAULT_MANIFEST_OUTPUT)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true", help="Validate without writing collected drops.")
    args = parser.parse_args(argv)

    try:
        if args.self_test:
            manifest = run_self_test()
        else:
            if not args.input_dir:
                raise DropCollectionError("--input-dir is required unless --self-test is used")
            if not args.prompt_pack:
                raise DropCollectionError("--prompt-pack is required unless --self-test is used")
            if not args.transcript:
                raise DropCollectionError("--transcript is required unless --self-test is used")
            run_id = args.run_id.strip() or f"{args.source_kind}-{_utc_now()}"
            source_command = args.source_command.strip() or f"{args.source_kind} model run"
            if args.no_write:
                with tempfile.TemporaryDirectory(prefix="chili_model_candidate_collector_dry_run_") as raw_output:
                    _, manifest = collect_candidate_drops(
                        input_dir=args.input_dir,
                        output_dir=Path(raw_output) / "collected",
                        prompt_pack_path=args.prompt_pack,
                        transcript_path=args.transcript,
                        source_kind=args.source_kind,
                        model_name=args.model_name,
                        run_id=run_id,
                        source_command=source_command,
                        allow_partial=args.allow_partial,
                    )
            else:
                _, manifest = collect_candidate_drops(
                    input_dir=args.input_dir,
                    output_dir=args.output_dir,
                    prompt_pack_path=args.prompt_pack,
                    transcript_path=args.transcript,
                    source_kind=args.source_kind,
                    model_name=args.model_name,
                    run_id=run_id,
                    source_command=source_command,
                    allow_partial=args.allow_partial,
                )
                write_manifest(manifest, args.manifest_output)
    except (DropCollectionError, ArtifactBuildError) as exc:
        print(f"drop collection error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    else:
        print(
            "Collected model candidate drops: "
            f"{manifest['cases']} cases; source_kind={manifest['source_kind']}; "
            f"validated_with_provenance={manifest['validated_with_provenance']}"
        )
        if not args.no_write and not args.self_test:
            print(f"Wrote {args.output_dir}")
            print(f"Wrote {args.manifest_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
