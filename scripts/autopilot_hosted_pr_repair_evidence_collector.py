from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_hosted_pr_repair_collection_packet import (  # noqa: E402
    REQUIRED_EVIDENCE_FILES,
    _metadata_from_markdown,
)


DEFAULT_OUTPUT_ROOT = REPO_ROOT / "project_ws" / "AgentOps" / "hosted_pr_repair_evidence"
HOSTED_PR_REPAIR_EVIDENCE_COLLECTOR_SCHEMA_VERSION = "chili.hosted-pr-repair-evidence-collector.v1"
SOURCE_MANIFEST_SCHEMA_VERSION = "chili.hosted-pr-repair-source-manifest.v1"


class HostedPrRepairEvidenceCollectorError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip(".-").lower()
    return safe or "hosted-pr-repair"


def _pr_slug(pr_url: str) -> str:
    match = re.search(r"/pull/(\d+)", pr_url)
    if match:
        return f"pr-{match.group(1)}"
    return _slug(pr_url)


def _command_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _manifest_template(metadata: dict[str, str]) -> dict[str, object]:
    pr_url = metadata.get("pr") or "<hosted-pr-url>"
    head_sha = metadata.get("current head sha observed") or metadata.get("head sha inspected") or "<current-head-sha>"
    run_id = metadata.get("current hosted green run observed") or metadata.get("hosted run inspected") or "<hosted-green-run-id>"
    return {
        "schema": SOURCE_MANIFEST_SCHEMA_VERSION,
        "pr_url": pr_url,
        "branch": metadata.get("branch") or "<branch>",
        "source_run_id": "<real-hosted-pr-repair-source-run-id>",
        "repair_report": metadata.get("candidate_report") or "<candidate-report>",
        "review_thread_id": "<review-thread-id>",
        "line_thread": {
            "thread_id": "<review-thread-id>",
            "comment_id": "<line-thread-comment-id>",
            "path": "<reviewed-file-path>",
            "line": "<reviewed-line>",
        },
        "repaired_head_sha": head_sha,
        "post_repair_head_sha": head_sha,
        "current_head_sha_observed": head_sha,
        "hosted_run_id": run_id,
        "current_hosted_green_run_observed": run_id,
        "remote_publication": {
            "url": "<hosted-run-or-pr-publication-url>",
            "pr_url": pr_url,
            "commit_sha": head_sha,
        },
        "review_thread_transcript_file": "review_thread_transcript.jsonl",
        "publication_transcript_file": "publication_transcript.jsonl",
        "post_repair_check_receipt_file": "post_repair_check_receipt.json",
    }


def collect_evidence_skeleton(
    *,
    candidate_report: Path | None = None,
    output_dir: Path | None = None,
    write: bool = True,
) -> tuple[str, dict[str, object], Path]:
    metadata = _metadata_from_markdown(candidate_report)
    pr_url = metadata.get("pr") or "<hosted-pr-url>"
    target_dir = output_dir or DEFAULT_OUTPUT_ROOT / _pr_slug(pr_url)
    manifest_template = _manifest_template(metadata)
    assemble_command = (
        "python scripts/autopilot_hosted_pr_repair_artifact_assembler.py "
        f"--evidence-dir {_command_path(target_dir)} --json"
    )
    validate_command = (
        "python scripts/autopilot_hosted_pr_repair_artifact_benchmark.py "
        f"--artifact-dir {_command_path(target_dir / 'artifact')} --json"
    )
    readme_lines = [
        "# CHILI Hosted PR Repair Evidence Collection",
        "",
        f"- Schema: {HOSTED_PR_REPAIR_EVIDENCE_COLLECTOR_SCHEMA_VERSION}",
        f"- Generated UTC: {_utc_now()}",
        f"- Candidate report: {metadata.get('candidate_report') or 'missing'}",
        f"- PR: {pr_url}",
        "- Permission boundary: local evidence staging only; no git/PR mutation, runtime restart, deploy, database, broker, or live-trading action.",
        "",
        "## Required Files",
        "",
    ]
    for filename in REQUIRED_EVIDENCE_FILES:
        readme_lines.append(f"- {filename}")
    readme_lines.extend(
        [
            "",
            "## Next Commands",
            "",
            f"- Assemble: `{assemble_command}`",
            f"- Validate: `{validate_command}`",
            "",
        ]
    )
    readme = "\n".join(readme_lines)
    if write:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "README.md").write_text(readme, encoding="utf-8")
        (target_dir / "source_manifest.template.json").write_text(
            json.dumps(manifest_template, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    summary = {
        "schema": HOSTED_PR_REPAIR_EVIDENCE_COLLECTOR_SCHEMA_VERSION,
        "status": "ready",
        "candidate_report": metadata.get("candidate_report", ""),
        "pr_url": pr_url,
        "output_dir": str(target_dir),
        "required_files": list(REQUIRED_EVIDENCE_FILES),
        "manifest_template": str(target_dir / "source_manifest.template.json"),
        "artifact_assembler_command": assemble_command,
        "validation_command": validate_command,
        "permission_boundary": "local evidence staging only; no git/PR mutation",
        "written": write,
    }
    return readme, summary, target_dir


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage a hosted PR repair evidence collection skeleton.")
    parser.add_argument("--candidate-report", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    try:
        readme, summary, _ = collect_evidence_skeleton(
            candidate_report=args.candidate_report,
            output_dir=args.output_dir,
            write=not args.no_write,
        )
    except HostedPrRepairEvidenceCollectorError as exc:
        print(f"hosted PR repair evidence collector error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(readme)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
