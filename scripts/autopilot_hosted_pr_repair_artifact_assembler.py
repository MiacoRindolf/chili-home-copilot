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
from scripts.autopilot_hosted_pr_repair_artifact_benchmark import (  # noqa: E402
    HostedPrRepairEvidenceError,
    sha256_file,
    validate_hosted_pr_repair_artifact,
    write_artifact,
)
from scripts.autopilot_hosted_pr_repair_collection_packet import (  # noqa: E402
    ARTIFACT_SCHEMA_VERSION,
    PERMISSION_BOUNDARY,
    discover_candidate_reports,
)


ASSEMBLER_SCHEMA_VERSION = "chili.hosted-pr-repair-artifact-assembler.v1"
DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "HOSTED_PR_REPAIR_ARTIFACT_ASSEMBLER.md"
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "project_ws" / "AgentOps" / "hosted_pr_repair_artifacts"
VALIDATOR_COMMAND = (
    "python scripts/autopilot_hosted_pr_repair_artifact_benchmark.py "
    "--artifact-dir {artifact_dir} --json"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug_timestamp(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", value)[:18] or "utc"


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _resolve_dir(path: Path, *, root: Path) -> Path:
    return path if path.is_absolute() else root / path


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_runs_from_payload(payload: object) -> list[Mapping[str, object]]:
    if isinstance(payload, Mapping):
        for key in ("check_runs", "checks", "workflow_runs", "nodes"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, Mapping)]
        return [payload]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    return []


def _status_payload(path: Path) -> Mapping[str, object]:
    if not path.is_file():
        return {}
    payload = _read_json(path)
    return payload if isinstance(payload, Mapping) else {}


def _pr_parts(pr_url: str) -> tuple[str, str, str]:
    match = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)",
        pr_url.strip(),
    )
    if not match:
        return "", "", ""
    return match.group(1), match.group(2), match.group(3)


def _check_run_url(
    row: Mapping[str, object],
    *,
    owner: str,
    repo: str,
    fallback_run_id: str,
) -> str:
    for key in ("html_url", "details_url", "url"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    run_id = str(row.get("run_id") or row.get("workflow_run_id") or fallback_run_id or "").strip()
    if owner and repo and run_id:
        return f"https://github.com/{owner}/{repo}/actions/runs/{run_id}"
    return ""


def _normalize_check_run(
    row: Mapping[str, object],
    *,
    owner: str,
    repo: str,
    default_head: str,
    fallback_run_id: str,
) -> dict[str, object]:
    name = str(
        row.get("name")
        or row.get("check_name")
        or row.get("workflow")
        or row.get("workflow_name")
        or "test"
    ).strip()
    conclusion = str(row.get("conclusion") or row.get("result") or row.get("state") or "").strip()
    status = str(row.get("status") or "COMPLETED").strip()
    head = str(
        row.get("head_sha")
        or row.get("head_ref_oid")
        or row.get("commit_sha")
        or row.get("sha")
        or default_head
    ).strip()
    url = _check_run_url(row, owner=owner, repo=repo, fallback_run_id=fallback_run_id)
    run_id = str(row.get("run_id") or row.get("workflow_run_id") or fallback_run_id or "").strip()
    completed_at = str(row.get("completed_at") or row.get("updated_at") or row.get("timestamp_utc") or "").strip()
    receipt: dict[str, object] = {
        "name": name,
        "status": status.upper(),
        "conclusion": conclusion.upper(),
        "head_sha": head,
        "source": "github_check_run_raw_capture",
    }
    if url:
        receipt["url"] = url
    if run_id:
        receipt["run_id"] = run_id
    if completed_at:
        receipt["completed_at"] = completed_at
    return receipt


def _is_failure(receipt: Mapping[str, object]) -> bool:
    conclusion = str(receipt.get("conclusion") or "").lower()
    return conclusion in {"failure", "failed", "error", "timed_out", "cancelled", "action_required"}


def _is_success(receipt: Mapping[str, object]) -> bool:
    conclusion = str(receipt.get("conclusion") or "").lower()
    return conclusion in {"success", "successful", "passed", "pass", "green"}


def _load_receipts(
    path: Path,
    *,
    owner: str,
    repo: str,
    default_head: str,
    fallback_run_id: str,
) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    return [
        _normalize_check_run(
            row,
            owner=owner,
            repo=repo,
            default_head=default_head,
            fallback_run_id=fallback_run_id,
        )
        for row in _check_runs_from_payload(_read_json(path))
    ]


def _write_jsonl(path: Path, events: Sequence[Mapping[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
        encoding="utf-8",
    )
    return path


def _failure_events(
    *,
    pr_url: str,
    before_head: str,
    failed_checks: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    first = failed_checks[0]
    return [
        {
            "event": "hosted_ci_failure_captured",
            "pr_url": pr_url,
            "commit_sha": before_head,
            "name": first.get("name"),
            "conclusion": "failure",
            "url": first.get("url", ""),
            "run_id": first.get("run_id", ""),
        },
        {
            "event": "failed_check_log",
            "pr_url": pr_url,
            "head_sha": before_head,
            "name": first.get("name"),
            "failure": f"{first.get('name')} failed on hosted PR head {before_head}",
            "source": "github_check_run_raw_capture",
        },
        {
            "event": "failure_ingested",
            "pr_url": pr_url,
            "head_sha": before_head,
            "source": "github_check_run_raw_capture",
            "failed_checks": len(failed_checks),
        },
    ]


def _publication_events(
    *,
    pr_url: str,
    final_head: str,
    successful_checks: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    first = successful_checks[0]
    return [
        {
            "event": "operator_approved_repaired_publication",
            "pr_url": pr_url,
            "commit_sha": final_head,
            "post_repair_merge_ready": True,
        },
        {
            "event": "pr_published",
            "publication_state": "pr_published",
            "pr_url": pr_url,
            "commit_sha": final_head,
        },
        {
            "event": "post_repair_check_receipt",
            "pr_url": pr_url,
            "name": first.get("name"),
            "conclusion": "success",
            "head_sha": final_head,
            "url": first.get("url", ""),
            "run_id": first.get("run_id", ""),
            "completed_at": first.get("completed_at", ""),
        },
    ]


def _artifact_payload(
    *,
    candidate: Mapping[str, object],
    artifact_dir: Path,
    source_agent: str,
    source_run_id: str,
    failed_checks: Sequence[Mapping[str, object]],
    successful_checks: Sequence[Mapping[str, object]],
    pr_status_after: Mapping[str, object],
    generated_utc: str,
) -> dict[str, object]:
    pr_url = str(candidate.get("pr_url") or "")
    _, _, pr_number = _pr_parts(pr_url)
    before_head = str(candidate.get("head_sha_inspected") or "").strip()
    final_head = str(candidate.get("current_head_sha_observed") or "").strip()
    slug = _slug_timestamp(generated_utc)
    failure_transcript = artifact_dir / "failure.transcript.jsonl"
    publication_transcript = artifact_dir / "publication.transcript.jsonl"
    return {
        "schema": ARTIFACT_SCHEMA_VERSION,
        "artifact_id": f"hosted-pr-repair-pr-{pr_number}-{slug}",
        "hosted": True,
        "source_agent": source_agent,
        "source_run_id": source_run_id or f"{source_agent}-hosted-pr-repair-pr-{pr_number}-{slug}",
        "collected_at": generated_utc,
        "pr_url": pr_url,
        "evidence": {
            "failure_transcript_file": failure_transcript.name,
            "failure_transcript_sha256": sha256_file(failure_transcript),
            "publication_transcript_file": publication_transcript.name,
            "publication_transcript_sha256": sha256_file(publication_transcript),
        },
        "pr_status_before": {
            "ok": True,
            "merge_ready": False,
            "pr_url": pr_url,
            "commit_sha": before_head,
            "blockers": ["checks_failed"],
            "review_feedback": {"total": 0, "items": []},
            "failed_checks": list(failed_checks),
        },
        "repair_result": {
            "status": "success",
            "final_commit_sha": final_head,
            "final_files_changed": ["unknown"],
            "handoff": {
                "publication_state": "repair_not_published",
                "post_repair_monitoring": {
                    "required_after_publication": True,
                    "pr_identifier": pr_number,
                    "expected_branch": candidate.get("branch") or "",
                },
            },
        },
        "publication_result": {
            "publication_state": "pr_published",
            "commit_sha": final_head,
            "pr_output": pr_url,
            "post_repair_status": {
                "ok": True,
                "merge_ready": bool(pr_status_after.get("merged") is True or pr_status_after.get("mergeable_state") in {"clean", "has_hooks", "unstable"}),
                "pr_url": pr_url,
                "commit_sha": final_head,
                "checks": {
                    "total": len(successful_checks),
                    "passed": len(successful_checks),
                    "failed": 0,
                    "pending": 0,
                },
                "successful_checks": list(successful_checks),
            },
        },
    }


def _missing_required(candidate: Mapping[str, object], artifact_dir: Path) -> list[str]:
    missing: list[str] = []
    for key in ("pr_url", "head_sha_inspected", "current_head_sha_observed"):
        if not str(candidate.get(key) or "").strip():
            missing.append(f"candidate.{key}")
    for path in (
        artifact_dir / "raw_failed_head_check_runs.json",
        artifact_dir / "raw_current_head_check_runs.json",
        artifact_dir / "raw_pr_status_after.json",
    ):
        if not path.is_file():
            missing.append(_relative(path, REPO_ROOT))
    return missing


def assemble_hosted_pr_repair_artifact(
    *,
    root: Path = REPO_ROOT,
    candidate_reports: Sequence[Path] = (),
    artifact_dir: Path | None = None,
    source_agent: str = "codex",
    source_run_id: str = "",
    generated_utc: str | None = None,
    write: bool = True,
) -> dict[str, object]:
    candidates = discover_candidate_reports(root=root, candidate_reports=candidate_reports)
    candidate = candidates[0] if candidates else {}
    if not candidate:
        return {
            "schema": ASSEMBLER_SCHEMA_VERSION,
            "generated_utc": generated_utc or _utc_now(),
            "status": "missing_candidate",
            "permission_boundary": PERMISSION_BOUNDARY,
        }

    pr_url = str(candidate.get("pr_url") or "")
    owner, repo, pr_number = _pr_parts(pr_url)
    resolved_artifact_dir = _resolve_dir(
        artifact_dir or DEFAULT_ARTIFACT_ROOT / f"pr_{pr_number or 'unknown'}",
        root=root,
    )
    generated_utc = generated_utc or _utc_now()
    missing = _missing_required(candidate, resolved_artifact_dir)
    if missing:
        return {
            "schema": ASSEMBLER_SCHEMA_VERSION,
            "generated_utc": generated_utc,
            "status": "missing_evidence",
            "latest_candidate": candidate,
            "artifact_dir": _relative(resolved_artifact_dir, root),
            "missing": missing,
            "permission_boundary": PERMISSION_BOUNDARY,
        }

    before_head = str(candidate.get("head_sha_inspected") or "")
    final_head = str(candidate.get("current_head_sha_observed") or "")
    failed_receipts = [
        receipt
        for receipt in _load_receipts(
            resolved_artifact_dir / "raw_failed_head_check_runs.json",
            owner=owner,
            repo=repo,
            default_head=before_head,
            fallback_run_id=str(candidate.get("hosted_run_inspected") or ""),
        )
        if _is_failure(receipt)
    ]
    successful_receipts = [
        receipt
        for receipt in _load_receipts(
            resolved_artifact_dir / "raw_current_head_check_runs.json",
            owner=owner,
            repo=repo,
            default_head=final_head,
            fallback_run_id=str(candidate.get("current_hosted_green_run_observed") or ""),
        )
        if _is_success(receipt)
    ]
    missing_receipts: list[str] = []
    if not failed_receipts:
        missing_receipts.append("raw_failed_head_check_runs.json failed check receipt")
    if not successful_receipts:
        missing_receipts.append("raw_current_head_check_runs.json successful check receipt")
    if missing_receipts:
        return {
            "schema": ASSEMBLER_SCHEMA_VERSION,
            "generated_utc": generated_utc,
            "status": "missing_evidence",
            "latest_candidate": candidate,
            "artifact_dir": _relative(resolved_artifact_dir, root),
            "missing": missing_receipts,
            "permission_boundary": PERMISSION_BOUNDARY,
        }

    artifact_path = resolved_artifact_dir / "repair-artifact.json"
    failure_transcript = resolved_artifact_dir / "failure.transcript.jsonl"
    publication_transcript = resolved_artifact_dir / "publication.transcript.jsonl"
    validator_command = VALIDATOR_COMMAND.format(artifact_dir=_relative(resolved_artifact_dir, root))
    if not write:
        return {
            "schema": ASSEMBLER_SCHEMA_VERSION,
            "generated_utc": generated_utc,
            "status": "ready_to_assemble",
            "latest_candidate": candidate,
            "artifact_dir": _relative(resolved_artifact_dir, root),
            "would_write": [
                _relative(artifact_path, root),
                _relative(failure_transcript, root),
                _relative(publication_transcript, root),
            ],
            "failed_check_receipts": len(failed_receipts),
            "successful_check_receipts": len(successful_receipts),
            "validation_command": validator_command,
            "permission_boundary": PERMISSION_BOUNDARY,
        }

    _write_jsonl(
        failure_transcript,
        _failure_events(pr_url=pr_url, before_head=before_head, failed_checks=failed_receipts),
    )
    _write_jsonl(
        publication_transcript,
        _publication_events(
            pr_url=pr_url,
            final_head=final_head,
            successful_checks=successful_receipts,
        ),
    )
    artifact = _artifact_payload(
        candidate=candidate,
        artifact_dir=resolved_artifact_dir,
        source_agent=source_agent,
        source_run_id=source_run_id,
        failed_checks=failed_receipts,
        successful_checks=successful_receipts,
        pr_status_after=_status_payload(resolved_artifact_dir / "raw_pr_status_after.json"),
        generated_utc=generated_utc,
    )
    write_artifact(artifact_path, artifact)
    validation = validate_hosted_pr_repair_artifact(artifact, base_dir=resolved_artifact_dir)
    return {
        "schema": ASSEMBLER_SCHEMA_VERSION,
        "generated_utc": generated_utc,
        "status": "assembled",
        "latest_candidate": candidate,
        "artifact_dir": _relative(resolved_artifact_dir, root),
        "artifact_path": _relative(artifact_path, root),
        "failure_transcript": _relative(failure_transcript, root),
        "publication_transcript": _relative(publication_transcript, root),
        "failed_check_receipts": len(failed_receipts),
        "successful_check_receipts": len(successful_receipts),
        "validation_command": validator_command,
        "validation": validation,
        "permission_boundary": PERMISSION_BOUNDARY,
    }


def render_summary(summary: Mapping[str, object]) -> str:
    lines = [
        "# CHILI Hosted PR Repair Artifact Assembler",
        "",
        f"- Schema: {summary.get('schema')}",
        f"- Generated UTC: {summary.get('generated_utc')}",
        f"- Status: {summary.get('status')}",
        f"- Artifact dir: {summary.get('artifact_dir') or 'missing'}",
        f"- Artifact: {summary.get('artifact_path') or 'not written'}",
        f"- Failed check receipts: {summary.get('failed_check_receipts') or 0}",
        f"- Successful check receipts: {summary.get('successful_check_receipts') or 0}",
        f"- Validator: {summary.get('validation_command') or 'not ready'}",
        f"- Permission boundary: {summary.get('permission_boundary')}",
        "",
    ]
    missing = summary.get("missing")
    if isinstance(missing, list) and missing:
        lines.extend(["## Missing Evidence", ""])
        for value in missing:
            lines.append(f"- {value}")
        lines.append("")
    validation = summary.get("validation")
    if isinstance(validation, Mapping):
        lines.extend(["## Validation", "", "| Field | Value |", "| --- | --- |"])
        for key in (
            "repair_evidence_mode",
            "pr_url",
            "final_commit_sha",
            "post_repair_check_receipts",
        ):
            lines.append(f"| {key} | {_escape_cell(str(validation.get(key) or ''))} |")
        lines.append("")
    return "\n".join(lines)


def write_summary(markdown: str, output_path: Path = DEFAULT_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble transcript-bound hosted PR repair artifacts from captured raw hosted evidence."
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--candidate-report", type=Path, action="append", default=[])
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--source-agent", default="codex")
    parser.add_argument("--source-run-id", default="")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    try:
        summary = assemble_hosted_pr_repair_artifact(
            root=args.root,
            candidate_reports=args.candidate_report,
            artifact_dir=args.artifact_dir,
            source_agent=args.source_agent,
            source_run_id=args.source_run_id,
            write=not args.no_write,
        )
    except (HostedPrRepairEvidenceError, json.JSONDecodeError, OSError) as exc:
        summary = {
            "schema": ASSEMBLER_SCHEMA_VERSION,
            "generated_utc": _utc_now(),
            "status": "invalid_evidence",
            "error": str(exc),
            "permission_boundary": PERMISSION_BOUNDARY,
        }
    markdown = render_summary(summary)
    if not args.no_write:
        write_summary(markdown, args.output)
    payload = {**summary, "output": str(args.output)}
    print(json.dumps(payload, indent=2, sort_keys=True) if args.json else markdown)
    return 0 if summary.get("status") in {"assembled", "ready_to_assemble"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
