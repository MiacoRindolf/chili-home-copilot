from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_frontier_bakeoff_benchmark import _escape_cell  # noqa: E402
from scripts.autopilot_hosted_pr_repair_collection_packet import (  # noqa: E402
    PERMISSION_BOUNDARY,
    discover_candidate_reports,
)


COLLECTOR_SCHEMA_VERSION = "chili.hosted-pr-repair-evidence-collector.v1"
DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "HOSTED_PR_REPAIR_EVIDENCE_COLLECTOR.md"
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "project_ws" / "AgentOps" / "hosted_pr_repair_artifacts"
ASSEMBLER_COMMAND = (
    "python scripts/autopilot_hosted_pr_repair_artifact_assembler.py "
    "--candidate-report {candidate_report} --artifact-dir {artifact_dir} --json"
)
VALIDATOR_COMMAND = (
    "python scripts/autopilot_hosted_pr_repair_artifact_benchmark.py "
    "--artifact-dir {artifact_dir} --json"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _resolve(path: Path, *, root: Path) -> Path:
    return path if path.is_absolute() else root / path


def _pr_parts(pr_url: str) -> tuple[str, str, str]:
    match = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)",
        pr_url.strip(),
    )
    if not match:
        return "", "", ""
    return match.group(1), match.group(2), match.group(3)


def _json_preview(payload: object) -> str:
    if isinstance(payload, Mapping):
        for key in ("check_runs", "items", "nodes"):
            value = payload.get(key)
            if isinstance(value, list):
                return f"{key}={len(value)}"
        return f"keys={len(payload)}"
    if isinstance(payload, list):
        return f"items={len(payload)}"
    return type(payload).__name__


def _collect_specs(candidate: Mapping[str, object], artifact_dir: str) -> list[dict[str, str]]:
    pr_url = str(candidate.get("pr_url") or "")
    owner, repo, number = _pr_parts(pr_url)
    before_head = str(candidate.get("head_sha_inspected") or "")
    final_head = str(candidate.get("current_head_sha_observed") or "")
    repo_arg = f"{owner}/{repo}"
    return [
        {
            "label": "post_repair_pr_status",
            "endpoint": f"repos/{repo_arg}/pulls/{number}",
            "output": f"{artifact_dir}/raw_pr_status_after.json",
            "required": "post-repair PR status bound to the repaired commit",
        },
        {
            "label": "pre_repair_failed_head_checks",
            "endpoint": f"repos/{repo_arg}/commits/{before_head}/check-runs",
            "output": f"{artifact_dir}/raw_failed_head_check_runs.json",
            "required": "failing check receipts bound to the pre-repair head",
        },
        {
            "label": "current_head_success_checks",
            "endpoint": f"repos/{repo_arg}/commits/{final_head}/check-runs",
            "output": f"{artifact_dir}/raw_current_head_check_runs.json",
            "required": "successful check receipts bound to the repaired head",
        },
        {
            "label": "review_comments",
            "endpoint": f"repos/{repo_arg}/pulls/{number}/comments",
            "output": f"{artifact_dir}/raw_review_comments.json",
            "required": "optional review comment context",
        },
        {
            "label": "reviews",
            "endpoint": f"repos/{repo_arg}/pulls/{number}/reviews",
            "output": f"{artifact_dir}/raw_reviews.json",
            "required": "optional review state context",
        },
    ]


def _validate_candidate(candidate: Mapping[str, object]) -> list[str]:
    missing: list[str] = []
    pr_url = str(candidate.get("pr_url") or "")
    owner, repo, number = _pr_parts(pr_url)
    if not owner or not repo or not number:
        missing.append("candidate.pr_url")
    for key in ("head_sha_inspected", "current_head_sha_observed"):
        value = str(candidate.get(key) or "")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", value):
            missing.append(f"candidate.{key}")
    return missing


def _run_gh_api(
    gh_command: Sequence[str],
    endpoint: str,
    *,
    cwd: Path,
    timeout_seconds: int,
) -> object:
    command = [*gh_command, "api", endpoint]
    proc = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout_seconds,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"gh api {endpoint} failed with exit {proc.returncode}: {detail}")
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh api {endpoint} did not return JSON") from exc


def collect_hosted_pr_repair_evidence(
    *,
    root: Path = REPO_ROOT,
    candidate_reports: Sequence[Path] = (),
    artifact_dir: Path | None = None,
    gh_command: Sequence[str] = ("gh",),
    timeout_seconds: int = 60,
    write: bool = True,
    generated_utc: str | None = None,
) -> dict[str, object]:
    generated_utc = generated_utc or _utc_now()
    candidates = discover_candidate_reports(root=root, candidate_reports=candidate_reports)
    candidate = candidates[0] if candidates else {}
    if not candidate:
        return {
            "schema": COLLECTOR_SCHEMA_VERSION,
            "generated_utc": generated_utc,
            "status": "missing_candidate",
            "permission_boundary": PERMISSION_BOUNDARY,
        }
    missing = _validate_candidate(candidate)
    if missing:
        return {
            "schema": COLLECTOR_SCHEMA_VERSION,
            "generated_utc": generated_utc,
            "status": "invalid_candidate",
            "latest_candidate": candidate,
            "missing": missing,
            "permission_boundary": PERMISSION_BOUNDARY,
        }

    _, _, pr_number = _pr_parts(str(candidate.get("pr_url") or ""))
    resolved_dir = _resolve(
        artifact_dir or DEFAULT_ARTIFACT_ROOT / f"pr_{pr_number}",
        root=root,
    )
    artifact_dir_rel = _relative(resolved_dir, root)
    specs = _collect_specs(candidate, artifact_dir_rel)
    assembler_command = ASSEMBLER_COMMAND.format(
        candidate_report=str(candidate.get("path") or "<candidate-report>"),
        artifact_dir=artifact_dir_rel,
    )
    validator_command = VALIDATOR_COMMAND.format(artifact_dir=artifact_dir_rel)
    if not write:
        return {
            "schema": COLLECTOR_SCHEMA_VERSION,
            "generated_utc": generated_utc,
            "status": "ready_to_collect",
            "latest_candidate": candidate,
            "artifact_dir": artifact_dir_rel,
            "would_write": [spec["output"] for spec in specs],
            "collection_commands": [
                " ".join([*gh_command, "api", spec["endpoint"]]) for spec in specs
            ],
            "artifact_assembler_command": assembler_command,
            "validation_command": validator_command,
            "permission_boundary": PERMISSION_BOUNDARY,
        }

    resolved_dir.mkdir(parents=True, exist_ok=True)
    receipts: list[dict[str, object]] = []
    for spec in specs:
        payload = _run_gh_api(
            gh_command,
            spec["endpoint"],
            cwd=root,
            timeout_seconds=timeout_seconds,
        )
        output_path = _resolve(Path(spec["output"]), root=root)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        receipts.append(
            {
                "label": spec["label"],
                "endpoint": spec["endpoint"],
                "output": _relative(output_path, root),
                "preview": _json_preview(payload),
                "required": spec["required"],
            }
        )

    return {
        "schema": COLLECTOR_SCHEMA_VERSION,
        "generated_utc": generated_utc,
        "status": "collected",
        "latest_candidate": candidate,
        "artifact_dir": artifact_dir_rel,
        "collected_files": [str(receipt["output"]) for receipt in receipts],
        "receipts": receipts,
        "artifact_assembler_command": assembler_command,
        "validation_command": validator_command,
        "permission_boundary": PERMISSION_BOUNDARY,
    }


def render_summary(summary: Mapping[str, object]) -> str:
    lines = [
        "# CHILI Hosted PR Repair Evidence Collector",
        "",
        f"- Schema: {summary.get('schema')}",
        f"- Generated UTC: {summary.get('generated_utc')}",
        f"- Status: {summary.get('status')}",
        f"- Artifact dir: {summary.get('artifact_dir') or 'missing'}",
        f"- Assembler: {summary.get('artifact_assembler_command') or 'not ready'}",
        f"- Validator: {summary.get('validation_command') or 'not ready'}",
        f"- Permission boundary: {summary.get('permission_boundary')}",
        "",
    ]
    missing = summary.get("missing")
    if isinstance(missing, list) and missing:
        lines.extend(["## Missing", ""])
        for value in missing:
            lines.append(f"- {value}")
        lines.append("")
    receipts = summary.get("receipts")
    if isinstance(receipts, list) and receipts:
        lines.extend(["## Receipts", "", "| Label | Output | Preview |", "| --- | --- | --- |"])
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                continue
            lines.append(
                "| "
                f"{_escape_cell(str(receipt.get('label') or ''))} | "
                f"{_escape_cell(str(receipt.get('output') or ''))} | "
                f"{_escape_cell(str(receipt.get('preview') or ''))} |"
            )
        lines.append("")
    commands = summary.get("collection_commands")
    if isinstance(commands, list) and commands:
        lines.extend(["## Collection Commands", "", "| Step | Command |", "| --- | --- |"])
        for index, command in enumerate(commands, start=1):
            lines.append(f"| {index} | {_escape_cell(str(command))} |")
        lines.append("")
    return "\n".join(lines)


def write_summary(markdown: str, output_path: Path = DEFAULT_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect read-only raw GitHub evidence for hosted PR repair artifact assembly."
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--candidate-report", type=Path, action="append", default=[])
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--gh-command", nargs="+", default=["gh"])
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    try:
        summary = collect_hosted_pr_repair_evidence(
            root=args.root,
            candidate_reports=args.candidate_report,
            artifact_dir=args.artifact_dir,
            gh_command=tuple(args.gh_command),
            timeout_seconds=args.timeout_seconds,
            write=not args.no_write,
        )
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        summary = {
            "schema": COLLECTOR_SCHEMA_VERSION,
            "generated_utc": _utc_now(),
            "status": "collection_failed",
            "error": str(exc),
            "permission_boundary": PERMISSION_BOUNDARY,
        }
    markdown = render_summary(summary)
    if not args.no_write:
        write_summary(markdown, args.output)
    payload = {**summary, "output": str(args.output)}
    print(json.dumps(payload, indent=2, sort_keys=True) if args.json else markdown)
    return 0 if summary.get("status") in {"collected", "ready_to_collect"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
