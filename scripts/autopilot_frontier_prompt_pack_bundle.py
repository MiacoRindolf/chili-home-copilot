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

from scripts.autopilot_frontier_bakeoff_benchmark import _escape_cell  # noqa: E402
from scripts.autopilot_model_candidate_artifact_builder import (  # noqa: E402
    ArtifactBuildError,
    MODEL_CANDIDATE_DROP_PROMPT_PACK_SCHEMA_VERSION,
    render_prompt_pack,
    sha256_file,
    validate_prompt_pack_markdown,
)
from scripts.autopilot_model_candidate_tournament_benchmark import (  # noqa: E402
    REQUIRED_SOURCE_KINDS,
    frontier_model_target_error,
    frontier_model_targets_summary,
)


DEFAULT_OUTPUT_DIR = REPO_ROOT / "project_ws" / "AgentOps" / "frontier_model_prompt_packs"
DEFAULT_SUMMARY_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "FRONTIER_MODEL_PROMPT_PACK_BUNDLE.md"
FRONTIER_PROMPT_PACK_BUNDLE_SCHEMA_VERSION = "chili.frontier-prompt-pack-bundle.v1"
DEFAULT_MODEL_NAMES = {
    "codex": "gpt-5.5",
    "claude": "claude-fable-5",
    "local_model": "qwen3:4b",
}
PROMPT_PACK_FILE = "prompt_pack.md"
MANIFEST_FILE = "manifest.json"


class PromptPackBundleError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class PromptPackEntry:
    source_kind: str
    model_name: str
    path: str
    sha256: str
    bytes: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PromptPackBundleError(f"{label} must be an object")
    return value


def _required_text(payload: Mapping[str, object], key: str, *, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PromptPackBundleError(f"{label}.{key} is required")
    return value.strip()


def _model_names(*, local_model: str) -> dict[str, str]:
    names = dict(DEFAULT_MODEL_NAMES)
    names["local_model"] = local_model
    return names


def _validate_prompt_pack(markdown: str, *, source_kind: str, model_name: str) -> None:
    try:
        validate_prompt_pack_markdown(
            markdown,
            source_kind=source_kind,
            model_name=model_name,
            label=f"{source_kind} prompt pack",
            response_only=True,
        )
    except ArtifactBuildError as exc:
        raise PromptPackBundleError(str(exc)) from exc


def build_prompt_pack_bundle(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    local_model: str = DEFAULT_MODEL_NAMES["local_model"],
    write: bool = True,
) -> dict[str, object]:
    generated_utc = _utc_now()
    model_names = _model_names(local_model=local_model)
    entries: list[PromptPackEntry] = []
    for source_kind in REQUIRED_SOURCE_KINDS:
        model_name = model_names[source_kind]
        target_error = frontier_model_target_error(
            source_kind,
            model_name,
            label=f"model_names.{source_kind}",
        )
        if target_error:
            raise PromptPackBundleError(target_error)
        markdown = render_prompt_pack(
            source_kind=source_kind,
            model_name=model_name,
            response_only=True,
        )
        _validate_prompt_pack(markdown, source_kind=source_kind, model_name=model_name)
        relative_path = f"{source_kind}/{PROMPT_PACK_FILE}"
        if write:
            path = output_dir / source_kind / PROMPT_PACK_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(markdown, encoding="utf-8")
            digest = sha256_file(path)
            size = path.stat().st_size
        else:
            import hashlib

            digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
            size = len(markdown.encode("utf-8"))
        entries.append(
            PromptPackEntry(
                source_kind=source_kind,
                model_name=model_name,
                path=relative_path,
                sha256=digest,
                bytes=size,
            )
        )
    manifest = {
        "schema": FRONTIER_PROMPT_PACK_BUNDLE_SCHEMA_VERSION,
        "prompt_pack_schema": MODEL_CANDIDATE_DROP_PROMPT_PACK_SCHEMA_VERSION,
        "generated_utc": generated_utc,
        "required_source_kinds": list(REQUIRED_SOURCE_KINDS),
        "required_frontier_model_targets": frontier_model_targets_summary(),
        "output_dir": str(output_dir),
        "entries": [dataclasses.asdict(entry) for entry in entries],
    }
    if write:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / MANIFEST_FILE).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return manifest


def load_bundle_manifest(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PromptPackBundleError(f"{path}: invalid JSON: {exc}") from exc
    return dict(_as_mapping(raw, label=str(path)))


def validate_bundle_manifest(manifest_path: Path) -> dict[str, object]:
    manifest = load_bundle_manifest(manifest_path)
    schema = manifest.get("schema")
    if schema != FRONTIER_PROMPT_PACK_BUNDLE_SCHEMA_VERSION:
        raise PromptPackBundleError(
            f"manifest schema is {schema or 'missing'} instead of "
            f"{FRONTIER_PROMPT_PACK_BUNDLE_SCHEMA_VERSION}"
        )
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise PromptPackBundleError("manifest.entries must be a list")
    by_source: dict[str, Mapping[str, object]] = {}
    for index, raw_entry in enumerate(entries, start=1):
        entry = _as_mapping(raw_entry, label=f"entries[{index}]")
        source_kind = _required_text(entry, "source_kind", label=f"entries[{index}]")
        model_name = _required_text(entry, "model_name", label=f"entries[{index}]")
        target_error = frontier_model_target_error(
            source_kind,
            model_name,
            label=f"entries[{index}]",
        )
        if target_error:
            raise PromptPackBundleError(target_error)
        raw_path = _required_text(entry, "path", label=f"entries[{index}]")
        prompt_pack = (manifest_path.parent / raw_path).resolve()
        base = manifest_path.parent.resolve()
        if base not in prompt_pack.parents and prompt_pack != base:
            raise PromptPackBundleError(f"entries[{index}].path escapes bundle directory")
        if not prompt_pack.is_file():
            raise PromptPackBundleError(f"prompt pack does not exist: {raw_path}")
        expected_sha = _required_text(entry, "sha256", label=f"entries[{index}]").lower()
        actual_sha = sha256_file(prompt_pack)
        if actual_sha != expected_sha:
            raise PromptPackBundleError(f"{source_kind} prompt pack sha256 mismatch")
        markdown = prompt_pack.read_text(encoding="utf-8", errors="replace")
        _validate_prompt_pack(markdown, source_kind=source_kind, model_name=model_name)
        by_source[source_kind] = entry
    missing = [source_kind for source_kind in REQUIRED_SOURCE_KINDS if source_kind not in by_source]
    if missing:
        raise PromptPackBundleError("missing source prompt packs: " + ", ".join(missing))
    manifest["validated"] = True
    return manifest


def render_bundle_summary(manifest: Mapping[str, object]) -> str:
    entries = manifest.get("entries") if isinstance(manifest.get("entries"), list) else []
    lines = [
        "# CHILI Frontier Model Prompt Pack Bundle",
        "",
        f"- Schema: {manifest.get('schema')}",
        f"- Prompt pack schema: {manifest.get('prompt_pack_schema')}",
        f"- Generated UTC: {manifest.get('generated_utc')}",
        f"- Source kinds: {', '.join(str(item.get('source_kind')) for item in entries if isinstance(item, Mapping))}",
        f"- Required frontier model targets: {manifest.get('required_frontier_model_targets')}",
        "- Required behavior: each model source receives a prompt pack with matching source/model identity before real transcript collection.",
        "",
        "| Source | Model | Prompt Pack | SHA-256 | Bytes |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for raw in entries:
        if not isinstance(raw, Mapping):
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_cell(str(raw.get("source_kind") or "")),
                    _escape_cell(str(raw.get("model_name") or "")),
                    _escape_cell(str(raw.get("path") or "")),
                    _escape_cell(str(raw.get("sha256") or "")),
                    str(raw.get("bytes") or 0),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def write_bundle_summary(markdown: str, output_path: Path = DEFAULT_SUMMARY_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate source-specific prompt packs for real frontier model evidence collection."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--summary-output", type=Path, default=DEFAULT_SUMMARY_OUTPUT)
    parser.add_argument("--local-model", default=DEFAULT_MODEL_NAMES["local_model"])
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.validate:
            manifest = validate_bundle_manifest(args.output_dir / MANIFEST_FILE)
        else:
            manifest = build_prompt_pack_bundle(
                output_dir=args.output_dir,
                local_model=args.local_model,
                write=not args.no_write,
            )
            if not args.no_write:
                write_bundle_summary(render_bundle_summary(manifest), args.summary_output)
    except PromptPackBundleError as exc:
        print(f"prompt pack bundle error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    else:
        print(render_bundle_summary(manifest))
        if not args.no_write and not args.validate:
            print(f"Wrote {args.output_dir}")
            print(f"Wrote {args.summary_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
