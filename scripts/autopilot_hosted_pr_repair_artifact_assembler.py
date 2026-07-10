from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_hosted_pr_repair_artifact_benchmark import (  # noqa: E402
    HOSTED_PR_REPAIR_ARTIFACT_SCHEMA_VERSION,
    HOSTED_PR_REPAIR_INVENTORY_SCHEMA_VERSION,
    REAL_INVENTORY_EVIDENCE_MODE,
    sha256_file,
    validate_inventory,
)


DEFAULT_OUTPUT_ROOT = REPO_ROOT / "project_ws" / "AgentOps" / "hosted_pr_repair_evidence" / "artifact"
HOSTED_PR_REPAIR_ARTIFACT_ASSEMBLER_SCHEMA_VERSION = "chili.hosted-pr-repair-artifact-assembler.v1"
SOURCE_MANIFEST_SCHEMA_VERSION = "chili.hosted-pr-repair-source-manifest.v1"


class HostedPrRepairArtifactAssemblerError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HostedPrRepairArtifactAssemblerError(f"{label} must be an object")
    return value


def _required_text(payload: Mapping[str, object], key: str, *, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HostedPrRepairArtifactAssemblerError(f"{label}.{key} is required")
    return value.strip()


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise HostedPrRepairArtifactAssemblerError(f"{path}: invalid JSON: {exc}") from exc
    return dict(_as_mapping(payload, label=str(path)))


def _manifest_path(source_manifest: Path | None, evidence_dir: Path | None) -> Path:
    if source_manifest and evidence_dir:
        raise HostedPrRepairArtifactAssemblerError("--source-manifest and --evidence-dir cannot both be used")
    if source_manifest:
        return source_manifest
    if evidence_dir:
        candidate = evidence_dir / "source_manifest.json"
        if candidate.is_file():
            return candidate
        template = evidence_dir / "source_manifest.template.json"
        if template.is_file():
            return template
        raise HostedPrRepairArtifactAssemblerError(f"source manifest missing in {evidence_dir}")
    raise HostedPrRepairArtifactAssemblerError("--source-manifest or --evidence-dir is required")


def _copy_evidence_file(raw_path: str, *, manifest_dir: Path, output_dir: Path, write: bool) -> tuple[str, Path]:
    source = Path(raw_path)
    if not source.is_absolute():
        source = manifest_dir / source
    if not source.is_file():
        raise HostedPrRepairArtifactAssemblerError(f"evidence file does not exist: {source}")
    target_name = source.name
    target = output_dir / target_name
    if write:
        output_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return target_name, target
    return source.name, source


def build_inventory_from_source_manifest(
    source_manifest: Path,
    *,
    output_dir: Path = DEFAULT_OUTPUT_ROOT,
    write: bool = True,
) -> tuple[dict[str, object], dict[str, object], Path]:
    manifest = _read_json(source_manifest)
    schema = manifest.get("schema")
    if schema != SOURCE_MANIFEST_SCHEMA_VERSION:
        raise HostedPrRepairArtifactAssemblerError(
            f"source_manifest.schema is {schema or 'missing'} instead of {SOURCE_MANIFEST_SCHEMA_VERSION}"
        )
    manifest_dir = source_manifest.parent
    review_name, review_path = _copy_evidence_file(
        _required_text(manifest, "review_thread_transcript_file", label="source_manifest"),
        manifest_dir=manifest_dir,
        output_dir=output_dir,
        write=write,
    )
    publication_name, publication_path = _copy_evidence_file(
        _required_text(manifest, "publication_transcript_file", label="source_manifest"),
        manifest_dir=manifest_dir,
        output_dir=output_dir,
        write=write,
    )
    _, receipt_path = _copy_evidence_file(
        _required_text(manifest, "post_repair_check_receipt_file", label="source_manifest"),
        manifest_dir=manifest_dir,
        output_dir=output_dir,
        write=write,
    )
    receipt = _read_json(receipt_path)
    pr_url = _required_text(manifest, "pr_url", label="source_manifest")
    review_thread_id = _required_text(manifest, "review_thread_id", label="source_manifest")
    head_sha = _required_text(manifest, "post_repair_head_sha", label="source_manifest")
    artifact = {
        "schema": HOSTED_PR_REPAIR_ARTIFACT_SCHEMA_VERSION,
        "pr_url": pr_url,
        "branch": _required_text(manifest, "branch", label="source_manifest"),
        "source_run_id": _required_text(manifest, "source_run_id", label="source_manifest"),
        "repair_report": _required_text(manifest, "repair_report", label="source_manifest"),
        "review_thread_id": review_thread_id,
        "line_thread": dict(_as_mapping(manifest.get("line_thread"), label="source_manifest.line_thread")),
        "repaired_head_sha": _required_text(manifest, "repaired_head_sha", label="source_manifest"),
        "post_repair_head_sha": head_sha,
        "current_head_sha_observed": _required_text(manifest, "current_head_sha_observed", label="source_manifest"),
        "hosted_run_id": _required_text(manifest, "hosted_run_id", label="source_manifest"),
        "current_hosted_green_run_observed": _required_text(
            manifest,
            "current_hosted_green_run_observed",
            label="source_manifest",
        ),
        "remote_publication": dict(
            _as_mapping(manifest.get("remote_publication"), label="source_manifest.remote_publication")
        ),
        "post_repair_check_receipt": dict(
            _as_mapping(receipt, label="post_repair_check_receipt_file")
        ),
        "review_thread_transcript": {
            "path": review_name,
            "sha256": sha256_file(review_path),
            "pr_url": pr_url,
            "thread_id": review_thread_id,
        },
        "publication_transcript": {
            "path": publication_name,
            "sha256": sha256_file(publication_path),
            "pr_url": pr_url,
            "commit_sha": head_sha,
        },
    }
    inventory = {
        "schema": HOSTED_PR_REPAIR_INVENTORY_SCHEMA_VERSION,
        "generated_utc": _utc_now(),
        "evidence_mode": REAL_INVENTORY_EVIDENCE_MODE,
        "artifacts": [artifact],
    }
    validation_base = output_dir if write else manifest_dir
    summary = validate_inventory(inventory, base_dir=validation_base)
    inventory_path = output_dir / "inventory.json"
    if write:
        output_dir.mkdir(parents=True, exist_ok=True)
        inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return inventory, summary, inventory_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble a hosted PR repair artifact inventory.")
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    try:
        source_manifest = _manifest_path(args.source_manifest, args.evidence_dir)
        inventory, summary, inventory_path = build_inventory_from_source_manifest(
            source_manifest,
            output_dir=args.output_dir,
            write=not args.no_write,
        )
    except (HostedPrRepairArtifactAssemblerError, ValueError) as exc:
        print(f"hosted PR repair artifact assembler error: {exc}", file=sys.stderr)
        return 2
    payload = {
        "schema": HOSTED_PR_REPAIR_ARTIFACT_ASSEMBLER_SCHEMA_VERSION,
        "status": "passed",
        "inventory_schema": inventory.get("schema"),
        "artifacts": summary.get("artifacts"),
        "promotion_eligible": summary.get("promotion_eligible"),
        "inventory": str(inventory_path),
        "written": not args.no_write,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
