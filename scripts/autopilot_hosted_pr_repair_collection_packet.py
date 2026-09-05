from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_frontier_bakeoff_benchmark import _escape_cell  # noqa: E402


DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "HOSTED_PR_REPAIR_COLLECTION_PACKET.md"
HOSTED_PR_REPAIR_COLLECTION_PACKET_SCHEMA_VERSION = "chili.hosted-pr-repair-collection-packet.v1"
REQUIRED_EVIDENCE_FILES = (
    "review_thread_transcript.jsonl",
    "publication_transcript.jsonl",
    "post_repair_check_receipt.json",
    "source_manifest.json",
)


class HostedPrRepairCollectionPacketError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _metadata_from_markdown(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    if not path.is_file():
        raise HostedPrRepairCollectionPacketError(f"candidate report does not exist: {path}")
    metadata: dict[str, str] = {"candidate_report": _command_path(path)}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line.startswith("- ") or ":" not in line:
            continue
        key, value = line[2:].split(":", 1)
        metadata[key.strip().lower()] = value.strip()
    return metadata


def _command_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def build_collection_packet(
    *,
    candidate_report: Path | None = None,
    output_path: Path = DEFAULT_OUTPUT,
    write: bool = True,
) -> tuple[str, dict[str, object], Path]:
    metadata = _metadata_from_markdown(candidate_report)
    pr_url = metadata.get("pr") or "<hosted-pr-url>"
    branch = metadata.get("branch") or "<branch>"
    current_head = metadata.get("current head sha observed") or metadata.get("head sha inspected") or "<current-head-sha>"
    current_run = metadata.get("current hosted green run observed") or metadata.get("hosted run inspected") or "<hosted-green-run-id>"
    candidate_arg = (
        f" --candidate-report {metadata['candidate_report']}"
        if metadata.get("candidate_report")
        else ""
    )
    evidence_dir = "project_ws/AgentOps/hosted_pr_repair_evidence/<pr-slug>"
    collection_command = (
        "python scripts/autopilot_hosted_pr_repair_evidence_collector.py"
        f"{candidate_arg} --output-dir {evidence_dir} --json"
    )
    assemble_command = (
        "python scripts/autopilot_hosted_pr_repair_artifact_assembler.py "
        f"--evidence-dir {evidence_dir} --json"
    )
    validate_command = (
        "python scripts/autopilot_hosted_pr_repair_artifact_benchmark.py "
        f"--artifact-dir {evidence_dir}/artifact --json"
    )
    lines = [
        "# CHILI Hosted PR Repair Collection Packet",
        "",
        f"- Schema: {HOSTED_PR_REPAIR_COLLECTION_PACKET_SCHEMA_VERSION}",
        f"- Generated UTC: {_utc_now()}",
        f"- Candidate report: {metadata.get('candidate_report') or 'missing'}",
        f"- PR: {pr_url}",
        f"- Branch: {branch}",
        f"- Current head SHA observed: {current_head}",
        f"- Current hosted green run observed: {current_run}",
        "- Permission boundary: evidence collection and local validation only; no git/PR mutation, runtime restart, deploy, database, broker, or live-trading action.",
        "",
        "## Required Evidence Files",
        "",
        "| File | Purpose |",
        "| --- | --- |",
    ]
    purposes = {
        "review_thread_transcript.jsonl": "review thread and line-comment transcript bound to PR URL and thread id",
        "publication_transcript.jsonl": "publication/current-head transcript bound to PR URL and repaired commit",
        "post_repair_check_receipt.json": "hosted check receipt bound to current head and green run id",
        "source_manifest.json": "operator-filled manifest that names the collected files and IDs",
    }
    for filename in REQUIRED_EVIDENCE_FILES:
        lines.append(f"| {_escape_cell(filename)} | {_escape_cell(purposes[filename])} |")
    lines.extend(
        [
            "",
            "## Commands",
            "",
            f"- Collect evidence checklist: `{collection_command}`",
            f"- Assemble artifact inventory: `{assemble_command}`",
            f"- Validate real inventory: `{validate_command}`",
            "",
            "## Operator Fill-Ins",
            "",
            f"- PR URL: {pr_url}",
            f"- Branch: {branch}",
            f"- Post-repair/current head SHA: {current_head}",
            f"- Current hosted green run id: {current_run}",
            "- Review thread id: <review-thread-id>",
            "- Line-thread comment id: <line-thread-comment-id>",
            "",
        ]
    )
    markdown = "\n".join(lines)
    if write:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
    summary = {
        "schema": HOSTED_PR_REPAIR_COLLECTION_PACKET_SCHEMA_VERSION,
        "status": "ready",
        "candidate_report": metadata.get("candidate_report", ""),
        "pr_url": pr_url,
        "required_files": list(REQUIRED_EVIDENCE_FILES),
        "collection_command": collection_command,
        "artifact_assembler_command": assemble_command,
        "validation_command": validate_command,
        "permission_boundary": "evidence collection and local validation only; no git/PR mutation",
        "output": str(output_path),
        "written": write,
    }
    return markdown, summary, output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a copy-ready hosted PR repair evidence packet.")
    parser.add_argument("--candidate-report", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    try:
        markdown, summary, _ = build_collection_packet(
            candidate_report=args.candidate_report,
            output_path=args.output,
            write=not args.no_write,
        )
    except HostedPrRepairCollectionPacketError as exc:
        print(f"hosted PR repair collection packet error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
