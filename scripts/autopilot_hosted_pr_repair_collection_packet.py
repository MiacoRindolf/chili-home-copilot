from __future__ import annotations

import argparse
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


DEFAULT_CANDIDATE_GLOB = REPO_ROOT / "project_ws" / "AgentOps" / "PR_*_CI_REPAIR.md"
DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "HOSTED_PR_REPAIR_COLLECTION_PACKET.md"
PACKET_SCHEMA_VERSION = "chili.hosted-pr-repair-collection-packet.v1"
ARTIFACT_SCHEMA_VERSION = "chili.hosted-pr-repair-artifact.v1"
VALIDATOR_COMMAND = (
    "python scripts/autopilot_hosted_pr_repair_artifact_benchmark.py "
    "--artifact-dir {artifact_dir} --json"
)
ASSEMBLER_COMMAND = (
    "python scripts/autopilot_hosted_pr_repair_artifact_assembler.py "
    "--candidate-report {candidate_report} --artifact-dir {artifact_dir} --json"
)
EVIDENCE_COLLECTOR_COMMAND = (
    "python scripts/autopilot_hosted_pr_repair_evidence_collector.py "
    "--candidate-report {candidate_report} --artifact-dir {artifact_dir} --json"
)
PERMISSION_BOUNDARY = (
    "read-only PR evidence collection and local artifact validation only; does not "
    "edit source/tests, mutate git or PRs, restart runtime, deploy, touch databases, "
    "call brokers, or change live-trading behavior"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _metadata(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line.startswith("- ") or ":" not in line:
            continue
        key, value = line[2:].split(":", 1)
        values[key.strip().lower()] = value.strip()
    return values


def _section_bullets(text: str, heading: str) -> list[str]:
    capture = False
    bullets: list[str] = []
    target = f"## {heading}".lower()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.lower() == target:
            capture = True
            continue
        if capture and line.startswith("## "):
            break
        if capture and line.startswith("- "):
            value = line[2:].strip()
            if value:
                bullets.append(value)
    return bullets


def _pr_parts(pr_url: str) -> dict[str, str]:
    match = re.fullmatch(
        r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>[1-9][0-9]*)",
        pr_url.strip(),
    )
    if not match:
        return {"owner": "", "repo": "", "number": ""}
    return match.groupdict()


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _candidate_from_report(path: Path, *, root: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8", errors="replace")
    metadata = _metadata(path)
    pr_url = metadata.get("pr", "")
    parts = _pr_parts(pr_url)
    return {
        "path": _relative(path, root),
        "generated_utc": metadata.get("generated utc", ""),
        "updated_utc": metadata.get("updated utc", ""),
        "pr_url": pr_url,
        "owner": parts["owner"],
        "repo": parts["repo"],
        "pr_number": parts["number"],
        "branch": metadata.get("branch", ""),
        "head_sha_inspected": metadata.get("head sha inspected", ""),
        "current_head_sha_observed": metadata.get("current head sha observed", ""),
        "hosted_run_inspected": metadata.get("hosted run inspected", ""),
        "current_hosted_green_run_observed": metadata.get("current hosted green run observed", ""),
        "evidence_status": metadata.get("evidence status", ""),
        "promotion_status": metadata.get("promotion status", ""),
        "missing_evidence": _section_bullets(text, "Remaining Hosted Evidence"),
        "_modified_at": path.stat().st_mtime,
    }


def discover_candidate_reports(
    *,
    root: Path,
    candidate_reports: Sequence[Path] = (),
) -> list[dict[str, object]]:
    paths: list[Path] = []
    for path in candidate_reports:
        resolved = path if path.is_absolute() else root / path
        if resolved.is_file():
            paths.append(resolved)
    if not paths:
        default_parent = root / "project_ws" / "AgentOps"
        paths.extend(sorted(default_parent.glob(DEFAULT_CANDIDATE_GLOB.name)))
    candidates = [_candidate_from_report(path, root=root) for path in paths if path.is_file()]
    candidates.sort(
        key=lambda item: (
            str(item.get("updated_utc") or item.get("generated_utc") or ""),
            float(item.get("_modified_at") or 0),
        ),
        reverse=True,
    )
    for candidate in candidates:
        candidate.pop("_modified_at", None)
    return candidates


def _collection_commands(candidate: Mapping[str, object], artifact_dir: str) -> list[str]:
    owner = str(candidate.get("owner") or "<owner>")
    repo = str(candidate.get("repo") or "<repo>")
    number = str(candidate.get("pr_number") or "<pr-number>")
    before_head = str(candidate.get("head_sha_inspected") or "<pre-repair-head-sha>")
    head = str(candidate.get("current_head_sha_observed") or "<current-head-sha>")
    candidate_report = str(candidate.get("path") or "<candidate-report>")
    repo_arg = f"{owner}/{repo}"
    return [
        EVIDENCE_COLLECTOR_COMMAND.format(
            candidate_report=candidate_report,
            artifact_dir=artifact_dir,
        ),
        (
            f"gh api repos/{repo_arg}/pulls/{number} "
            f"> {artifact_dir}/raw_pr_status_after.json"
        ),
        (
            f"gh api repos/{repo_arg}/pulls/{number}/comments "
            f"> {artifact_dir}/raw_review_comments.json"
        ),
        (
            f"gh api repos/{repo_arg}/pulls/{number}/reviews "
            f"> {artifact_dir}/raw_reviews.json"
        ),
        (
            f"gh api repos/{repo_arg}/commits/{before_head}/check-runs "
            f"> {artifact_dir}/raw_failed_head_check_runs.json"
        ),
        (
            f"gh api repos/{repo_arg}/commits/{head}/check-runs "
            f"> {artifact_dir}/raw_current_head_check_runs.json"
        ),
        (
            f"Write {artifact_dir}/failure.transcript.jsonl as JSONL events "
            "with pr_url, failing head, failing check name, conclusion, URL, and failure summary."
        ),
        (
            f"Write {artifact_dir}/review-thread.transcript.jsonl as JSONL events "
            "with pr_url, unresolved review_thread path, line, body, and ingestion total when review threads exist."
        ),
        (
            f"Write {artifact_dir}/publication.transcript.jsonl with operator publication, "
            "pr_published, and current-head successful check receipt events."
        ),
        (
            f"Write {artifact_dir}/repair-artifact.json from the artifact template, "
            "including sha256 hashes for both transcript files."
        ),
        ASSEMBLER_COMMAND.format(
            candidate_report=candidate_report,
            artifact_dir=artifact_dir,
        ),
        VALIDATOR_COMMAND.format(artifact_dir=artifact_dir),
    ]


def _artifact_template(candidate: Mapping[str, object], artifact_dir: str) -> dict[str, object]:
    pr_number = str(candidate.get("pr_number") or "unknown")
    head = str(candidate.get("current_head_sha_observed") or "<current-head-sha>")
    return {
        "schema": ARTIFACT_SCHEMA_VERSION,
        "artifact_id": f"hosted-pr-repair-pr-{pr_number}-<utc>",
        "hosted": True,
        "source_agent": "<real-agent-name>",
        "source_run_id": "<real-agent-run-id>",
        "collected_at": "<utc-now>",
        "pr_url": candidate.get("pr_url") or "<hosted-pr-url>",
        "evidence": {
            "failure_transcript_file": "failure.transcript.jsonl",
            "failure_transcript_sha256": "<sha256>",
            "review_thread_transcript_file": "review-thread.transcript.jsonl",
            "review_thread_transcript_sha256": "<sha256>",
            "publication_transcript_file": "publication.transcript.jsonl",
            "publication_transcript_sha256": "<sha256>",
        },
        "pr_status_before": {
            "ok": True,
            "merge_ready": False,
            "pr_url": candidate.get("pr_url") or "<hosted-pr-url>",
            "commit_sha": candidate.get("head_sha_inspected") or "<pre-repair-head-sha>",
            "failed_checks": ["<pre-repair failing check receipt objects>"],
            "review_thread_ingestion": {"ok": True, "total": "<unresolved-thread-count>"},
            "review_feedback": {"items": ["<unresolved line-level review thread objects>"]},
        },
        "repair_result": {
            "status": "success",
            "final_commit_sha": head,
            "final_files_changed": ["<changed-file>"],
            "handoff": {
                "publication_state": "repair_not_published",
                "post_repair_monitoring": {
                    "required_after_publication": True,
                    "pr_identifier": pr_number,
                    "expected_branch": candidate.get("branch") or "<branch>",
                },
            },
        },
        "publication_result": {
            "publication_state": "pr_published",
            "commit_sha": head,
            "pr_output": candidate.get("pr_url") or "<hosted-pr-url>",
            "post_repair_status": {
                "ok": True,
                "merge_ready": True,
                "pr_url": candidate.get("pr_url") or "<hosted-pr-url>",
                "commit_sha": head,
                "checks": {"total": "<count>", "passed": "<count>", "failed": 0, "pending": 0},
                "successful_checks": ["<current-head successful check receipt objects>"],
            },
        },
    }


def build_collection_packet(
    *,
    root: Path = REPO_ROOT,
    candidate_reports: Sequence[Path] = (),
    generated_utc: str | None = None,
) -> dict[str, object]:
    candidates = discover_candidate_reports(root=root, candidate_reports=candidate_reports)
    latest = candidates[0] if candidates else {}
    pr_number = str(latest.get("pr_number") or "unknown") if latest else "unknown"
    artifact_dir = f"project_ws/AgentOps/hosted_pr_repair_artifacts/pr_{pr_number}"
    commands = _collection_commands(latest, artifact_dir) if latest else []
    assembler_command = (
        ASSEMBLER_COMMAND.format(
            candidate_report=str(latest.get("path") or "<candidate-report>"),
            artifact_dir=artifact_dir,
        )
        if latest
        else ""
    )
    evidence_collector_command = (
        EVIDENCE_COLLECTOR_COMMAND.format(
            candidate_report=str(latest.get("path") or "<candidate-report>"),
            artifact_dir=artifact_dir,
        )
        if latest
        else ""
    )
    return {
        "schema": PACKET_SCHEMA_VERSION,
        "generated_utc": generated_utc or _utc_now(),
        "status": "ready" if latest else "missing_candidate",
        "candidate_count": len(candidates),
        "latest_candidate": latest,
        "artifact_dir": artifact_dir,
        "required_files": [
            f"{artifact_dir}/raw_pr_status_after.json",
            f"{artifact_dir}/raw_failed_head_check_runs.json",
            f"{artifact_dir}/raw_current_head_check_runs.json",
            f"{artifact_dir}/repair-artifact.json",
            f"{artifact_dir}/failure.transcript.jsonl",
            f"{artifact_dir}/publication.transcript.jsonl",
        ],
        "missing_evidence": latest.get("missing_evidence", []) if latest else [],
        "collection_commands": commands,
        "artifact_template": _artifact_template(latest, artifact_dir) if latest else {},
        "evidence_collector_command": evidence_collector_command,
        "artifact_assembler_command": assembler_command,
        "validation_command": VALIDATOR_COMMAND.format(artifact_dir=artifact_dir),
        "permission_boundary": PERMISSION_BOUNDARY,
    }


def render_packet(packet: Mapping[str, object]) -> str:
    latest = packet.get("latest_candidate")
    candidate = latest if isinstance(latest, Mapping) else {}
    lines = [
        "# CHILI Hosted PR Repair Collection Packet",
        "",
        f"- Schema: {packet.get('schema')}",
        f"- Generated UTC: {packet.get('generated_utc')}",
        f"- Status: {packet.get('status')}",
        f"- Candidate reports: {packet.get('candidate_count')}",
        f"- Latest report: {candidate.get('path') or 'missing'}",
        f"- PR: {candidate.get('pr_url') or 'missing'}",
        f"- Current head: {candidate.get('current_head_sha_observed') or 'missing'}",
        f"- Hosted green run: {candidate.get('current_hosted_green_run_observed') or 'missing'}",
        f"- Artifact dir: {packet.get('artifact_dir')}",
        f"- Collector: {packet.get('evidence_collector_command') or 'missing'}",
        f"- Assembler: {packet.get('artifact_assembler_command') or 'missing'}",
        f"- Validator: {packet.get('validation_command')}",
        f"- Permission boundary: {packet.get('permission_boundary')}",
        "",
        "## Required Files",
        "",
    ]
    for value in packet.get("required_files") or []:
        lines.append(f"- {value}")
    lines.extend(["", "## Missing Evidence", ""])
    missing = packet.get("missing_evidence")
    if isinstance(missing, list) and missing:
        for value in missing:
            lines.append(f"- {value}")
    else:
        lines.append("- No candidate-specific missing evidence was found; still satisfy the validator contract.")
    lines.extend(
        [
            "",
            "## Collection Commands",
            "",
            "| Step | Command |",
            "| --- | --- |",
        ]
    )
    for index, command in enumerate(packet.get("collection_commands") or [], start=1):
        lines.append(f"| {index} | {_escape_cell(str(command))} |")
    lines.extend(["", "## Artifact Template", "", "```json"])
    lines.append(json.dumps(packet.get("artifact_template") or {}, indent=2, sort_keys=True))
    lines.extend(["```", ""])
    return "\n".join(lines)


def write_packet(markdown: str, output_path: Path = DEFAULT_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a read-only collection packet for hosted PR repair real_inventory artifacts."
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--candidate-report", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    packet = build_collection_packet(root=args.root, candidate_reports=args.candidate_report)
    markdown = render_packet(packet)
    if not args.no_write:
        write_packet(markdown, args.output)
    payload = {**packet, "output": str(args.output)}
    print(json.dumps(payload, indent=2, sort_keys=True) if args.json else markdown)
    return 0 if packet["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
