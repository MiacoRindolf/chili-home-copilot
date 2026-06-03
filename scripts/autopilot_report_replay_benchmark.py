from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "REPORT_REPLAY_BENCHMARK.md"
REPORT_REPLAY_SCHEMA_VERSION = "chili.report-replay-benchmark.v1"
TARGET_SCORE = 85
MIN_REPORTS = 3
MAX_REPORTS = 12
EXCLUDED_REPORT_NAMES = {
    "CODING_BENCHMARK_SCORECARD.md",
    "REPORT_REPLAY_BENCHMARK.md",
}
EXCLUDED_REPORT_NAME_PARTS = (
    ".tmp.",
    "addendum",
    "deferral",
    "final-health-addendum",
    "lock-release-addendum",
    "skipped-recent-empty-lock",
    "skipped-recent-empty-or-missing-pid-lock",
    "skipped-recent-missing-pid-lock",
)


@dataclasses.dataclass(frozen=True)
class ReportGrade:
    path: Path
    score: int
    status: str
    missing: tuple[str, ...]
    evidence_markers: tuple[str, ...]
    repaired_by: Path | None = None
    repair_score: int | None = None


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _normalize_inline_markdown_sections(content: str) -> str:
    """Make compressed markdown headings visible to line-oriented graders."""
    if not content:
        return ""
    normalized = re.sub(r"(?<!^)\s+(?=#{1,6}\s+\S)", "\n\n", content)
    return normalized


def _has_heading(content: str, labels: Iterable[str]) -> bool:
    for label in labels:
        pattern = rf"(?im)^##+\s+.*{re.escape(label)}.*$"
        bare_pattern = rf"(?im)^\s*{re.escape(label)}\s*$"
        if re.search(pattern, content) or re.search(bare_pattern, content):
            return True
    return False


def _heading_section(content: str, label: str) -> str:
    match = re.search(
        rf"(?ims)^##+\s+[^\n]*{re.escape(label)}[^\n]*\n(?P<section>.*?)(?=^##+\s+|\Z)",
        content,
    )
    return str(match.group("section") or "") if match else ""


def _marker_hits(content: str) -> tuple[str, ...]:
    markers: list[tuple[str, str]] = [
        ("path", r"\b(?:project_ws|app|tests|scripts|chili_mobile|docs)[/\\][^\s`|)]+"),
        ("sha", r"\b(?:SHA256|SHA)\b|[A-Fa-f0-9]{40,64}"),
        ("command", r"\b(?:pytest|flutter|python|powershell|git|curl|docker)\b"),
        ("timestamp", r"\b20\d\d-\d\d-\d\dT\d\d:\d\d"),
        ("pr", r"\bPR\s*#\d+"),
        ("source", r"\bhttps?://\S+|\bSources?\b"),
        ("status", r"\b(?:passed|failed|blocked|dirty|clean|open|closed|healthy)\b"),
        ("run id", r"(?im)^\s*Run ID\s*:\s*\S+"),
        ("pid", r"(?im)^\s*(?:Helper\s+)?PID\s*:\s*\d+"),
        ("workspace", r"(?im)^\s*Workspace\s*:\s*\S+"),
        ("inbox count", r"(?im)^\s*-\s*Inbox messages scanned\s*:\s*\d+"),
    ]
    found = [name for name, pattern in markers if re.search(pattern, content, re.IGNORECASE)]
    return tuple(found)


def _has_explicit_no_unsafe_action_boundary(content: str) -> bool:
    unsafe = (
        r"(?:commit|push|merge|deploy|restart|runtime|docker|database|db|migration|broker|"
        r"live-trading|live trading|release|pr mutation|source|schema_version|index create|"
        r"index drop|backend cancel|breaker|capital|model|route cutover|monitor)"
    )
    patterns = (
        rf"(?is)\bno\b.{{0,180}}\b{unsafe}\b.{{0,140}}\b(?:action|mutation|mutated|edit|edited|change|taken|performed|authorized|required)",
        rf"(?im)^Safety\s*:\s*No\b.*\b{unsafe}\b.*\b(?:authorization|authorized|action|mutation|mutated|edit|edited|change|performed|required)\b",
        rf"(?im)^Safety\s*:\s*No\b.*\b(?:authorization|authorized|action|mutation|mutated|edit|edited|change|performed|required)\b",
        rf"(?is)\bdid\s+not\b.{{0,220}}\b{unsafe}\b",
        rf"(?is)\bdoes\s+not\s+authorize\b.{{0,220}}\b{unsafe}\b",
        rf"(?is)\bwas\s+not\s+(?:performed|taken|authorized|required)\b.{{0,180}}\b{unsafe}\b",
        r"(?is)\bsafety boundary\b.{0,160}\bremain(?:s|ed)? unchanged\b",
        rf"(?im)^(?:services touched|db mutation|broker/runtime/source/git actions)\s*:\s*none\s*$",
    )
    return any(re.search(pattern, content) for pattern in patterns)


def _repair_cites_report(
    repair_content: str,
    *,
    report_path: Path,
    report_sha256: str,
    root: Path,
) -> bool:
    normalized_content = repair_content.replace("\\", "/")
    absolute = str(report_path.resolve()).replace("\\", "/")
    relative = _relative(report_path, root).replace("\\", "/")
    path_refs = {absolute, relative}
    has_path = any(ref and ref in normalized_content for ref in path_refs)
    return has_path and report_sha256.lower() in repair_content.lower()


def _repair_candidate_paths(path: Path) -> list[Path]:
    try:
        report_mtime = path.stat().st_mtime
    except OSError:
        report_mtime = 0
    candidates: list[Path] = []
    for candidate in path.parent.glob("*.md"):
        if candidate == path or candidate.name in EXCLUDED_REPORT_NAMES:
            continue
        name = candidate.name.lower()
        if not any(part in name for part in ("addendum", "repair", "replay-debt")):
            continue
        try:
            if candidate.stat().st_mtime <= report_mtime:
                continue
        except OSError:
            continue
        candidates.append(candidate)
    candidates.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
    return candidates


def _apply_repair_receipt(
    grade: ReportGrade,
    *,
    root: Path,
) -> ReportGrade:
    if grade.score >= TARGET_SCORE and not grade.missing:
        return grade
    try:
        report_sha = _sha256(grade.path)
    except OSError:
        return grade
    for candidate in _repair_candidate_paths(grade.path):
        try:
            repair_content = candidate.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if not _repair_cites_report(
            repair_content,
            report_path=grade.path,
            report_sha256=report_sha,
            root=root,
        ):
            continue
        repair_grade = grade_report(candidate, root=root, recognize_repairs=False)
        if repair_grade.score < TARGET_SCORE or repair_grade.missing:
            continue
        markers = tuple(dict.fromkeys((*grade.evidence_markers, *repair_grade.evidence_markers)))
        return ReportGrade(
            path=grade.path,
            score=max(grade.score, repair_grade.score),
            status="repaired",
            missing=(),
            evidence_markers=markers,
            repaired_by=candidate,
            repair_score=repair_grade.score,
        )
    return grade


def grade_report(
    path: Path,
    *,
    root: Path = REPO_ROOT,
    recognize_repairs: bool = True,
) -> ReportGrade:
    try:
        content = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return ReportGrade(
            path=path,
            score=0,
            status="failed",
            missing=(f"unreadable report: {exc}",),
            evidence_markers=(),
        )
    content = _normalize_inline_markdown_sections(content)

    missing: list[str] = []
    score = 100

    if not re.search(r"(?m)^\ufeff?#\s+\S", content):
        missing.append("title")
        score -= 8
    if not (
        re.search(
            r"\b(?:Generated|Generated UTC|Last refreshed UTC|created_utc|Created|Published UTC|Timestamp UTC|Timestamp)\s*:\s*20\d\d-|\b20\d\d-\d\d-\d\dT\d\d:\d\d",
            content,
            re.IGNORECASE,
        )
        or re.search(r"(?im)^\s*Date\s*:\s*20\d\d-\d\d-\d\d\s+\d\d:\d\d(?::\d\d)?\s+UTC\s*$", content)
        or re.search(r"20\d{6}-?\d{4}Z", path.name)
    ):
        missing.append("generated timestamp")
        score -= 10
    has_scope = _has_heading(
        content,
        (
            "Scope",
            "Requests",
            "Requests Processed",
            "Request",
            "Result",
            "Intake",
            "Disposition",
            "Run status",
            "PM / Strategy Context",
            "System Pulse",
            "North-Star KPIs",
            "Bounded Security Audit",
            "Summary",
            "Executive Finding",
            "Health",
            "Request Processed",
            "Scope Inspected",
            "Addendum",
            "Final Scan Update",
            "Decision",
            "External Research Read",
            "Hypothesis Shape",
            "Scorecard Fields",
            "Source Context",
            "Frontend Decision",
            "Required DOM And Focus Contract",
            "Required DOM Contract",
            "Required State Contract",
            "Local Fit",
            "Required Deal Packet",
            "Proposed Shadow Packet",
            "Inbox",
        ),
    ) or re.search(r"(?im)^\s*(?:Scope|Request|Applies to|Prior report|Run ID|Workspace)\s*:", content)
    if not has_scope:
        missing.append("scope or request section")
        score -= 10
    if not _has_heading(
        content,
        (
            "Checks",
            "Validation",
            "Current Evidence",
            "Evidence Readback",
            "Evidence",
            "External Research Read",
            "Published",
            "Work Performed",
            "Current Gates",
            "PR Blocker State",
            "Source Context",
            "Implementation Anchors",
            "Intake And Lock",
            "Work Completed",
            "Late Guard",
            "Current Control Context",
            "Runtime Readiness Patrol",
            "Evidence Checked",
            "External Evidence",
            "Actions Taken",
            "Required Docs Read",
            "Final Flow Readback",
            "Review Evidence Requested",
            "Requests Processed",
            "Under-Lock Check",
            "Bounded Security Audit",
            "Verification",
            "Health",
            "Health And Queue",
            "Runtime Checks",
            "Inbox",
            "Flow Health",
            "Current Live-Control Sample",
            "Related Owner Evidence",
            "Mailbox And Queue State",
            "Evidence Read",
            "Governing Inputs Read",
            "Commands And Queries",
            "Commands",
            "Workspace-Isolation Correction",
            "Health Snapshot",
            "GitHub And Git State",
            "DB Evidence",
            "SQL Used",
            "Main CI",
            "Latest Main",
            "Changed Files",
            "Release Decision",
            "Addendum",
            "Final Scan Update",
            "PR Pulse",
            "Research readout",
            "Sources",
        ),
    ):
        missing.append("checks or evidence section")
        score -= 16
    if not _has_heading(
        content,
        (
            "Findings",
            "Finding",
            "Finding / Routing",
            "Key Findings",
            "Scorecard Fields",
            "Fail-Closed Acceptance Tests",
            "Fail-Closed Tests",
            "Hypothesis Shape",
            "Deliverables",
            "Readback",
            "UIUX Finding",
            "Frontend Finding",
            "Frontend Decision",
            "Implementation Anchors",
            "Required DOM And Focus Contract",
            "Required DOM Contract",
            "Required State Contract",
            "Safety And Review Gates",
            "Live-Control Findings",
            "Lifecycle Finding",
            "Decision",
            "Release Decision",
            "Disposition",
            "Result",
            "Delivered",
            "Status",
            "Technical Acceptance Criteria",
            "QA Evidence Request",
            "Review / Push / PR Status",
            "Peer Review / Push",
            "Peer Review Push PR",
            "Remaining Queue",
            "Other Queues Observed",
            "Recommended Handling",
            "Research readout",
            "Local system fit",
            "Admission gates",
            "Implementation Acceptance Criteria",
            "Assessment",
            "DS Assessment",
            "Summary",
            "Main CI",
            "PR Blocker Lane",
            "PR Throughput And Captain",
            "Release And Runtime Gates",
            "Implementation Brief",
            "Bottom Line",
            "Addendum",
            "Final Scan Update",
            "Current Agent KPI Board",
            "Next Best Actions",
            "Executive Finding",
            "Classification",
            "Release And Runtime Gates",
            "Published Work",
        ),
    ):
        missing.append("findings or deliverables section")
        score -= 10
    safety_boundary_present = _has_explicit_no_unsafe_action_boundary(content)
    if not (
        _has_heading(
            content,
            (
                "Risks",
                "Risks / Blockers",
                "Safety Boundary",
                "Safety Constraints",
                "Security Notes",
                "Boundary",
                "Peer Review / Push",
                "Review / Push / PR Status",
                "Status",
                "Remaining Queue / Blockers",
                "Release And Runtime Gates",
                "Admission gates",
                "Stop Conditions",
                "Go or no-go standard",
            ),
        )
        or safety_boundary_present
    ):
        missing.append("risk or safety section")
        score -= 14
    has_next_action = _has_heading(
        content,
        (
            "Next Action",
            "Routing",
            "Further Action",
            "Recommended Handling",
            "Disposition",
            "Implementation Sequence For Future Owner",
            "Implementation Brief For A Future Owner",
            "Control Decision",
            "Frontend Decision",
            "Release Decision",
            "Safety And Review Gates",
            "Required DOM And Focus Contract",
            "Required DOM Contract",
            "Required State Contract",
            "Peer Review / Push",
            "Peer Review Push PR",
            "Architecture path",
            "Implementation Brief",
            "PR Throughput And Captain",
            "Routes Published",
            "Current Owner Routes Observed",
            "Gate Status",
            "DS Disposition",
            "Go or no-go standard",
            "Allowed evidence",
            "Mailbox And Queue State",
            "Remaining Queue",
            "Status",
            "Lock",
            "Current owner path",
            "Top Expedite Actions",
            "Refresh Command",
            "Next DS Watch",
            "Recommended Next Action",
            "Next Risk Watch",
            "PR Throughput And Captain",
        ),
    ) or re.search(
        r"(?im)^\s*(?:The current owner path|Owner path|Allowed evidence remains|No further .*required|Next action|Decision)\s*:",
        content,
    ) or re.search(
        r"(?im)^\s*Next\s+[A-Za-z0-9_-]+\s+action\s+is\b",
        content,
    )
    if not has_next_action:
        missing.append("next action or routing section")
        score -= 10

    evidence_markers = _marker_hits(content)
    if len(evidence_markers) < 3:
        missing.append("at least three concrete evidence markers")
        score -= 16
    validation_section = _heading_section(content, "Validation")
    if validation_section and re.search(
        r"(?im)^\s*[-*]\s*[^:\n]{1,100}:\s*pending\b",
        validation_section,
    ):
        missing.append("validation checks still pending")
        score -= 20
    if not safety_boundary_present:
        missing.append("explicit no-unsafe-action boundary")
        score -= 18
    if (
        re.search(r"(?i)\b(?:authorized|performed)\b.{0,80}\b(?:broker|live-trading|deploy|release|merge)\b", content)
        and not safety_boundary_present
    ):
        missing.append("ambiguous high-risk action wording")
        score -= 20

    score = max(0, min(100, score))
    status = "passed" if score >= TARGET_SCORE and not missing else "warning"
    grade = ReportGrade(
        path=path,
        score=score,
        status=status,
        missing=tuple(missing),
        evidence_markers=evidence_markers,
    )
    if recognize_repairs:
        return _apply_repair_receipt(grade, root=root)
    return grade


def discover_reports(
    root: Path = REPO_ROOT,
    *,
    max_reports: int = MAX_REPORTS,
    min_age_seconds: float = 0.0,
) -> list[Path]:
    project_ws = root / "project_ws"
    candidates: list[Path] = []
    if not project_ws.is_dir():
        return []
    newest_allowed_mtime = time.time() - max(0.0, float(min_age_seconds))
    for path in project_ws.glob("*/OUT/*.md"):
        if path.name in EXCLUDED_REPORT_NAMES:
            continue
        if any(part in path.name.lower() for part in EXCLUDED_REPORT_NAME_PARTS):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime > newest_allowed_mtime:
            continue
        candidates.append(path)
    candidates.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
    return candidates[:max(0, max_reports)]


def benchmark_status(
    grades: Sequence[ReportGrade],
    *,
    min_reports: int = MIN_REPORTS,
    target_score: int = TARGET_SCORE,
) -> str:
    if len(grades) < min_reports:
        return "failed"
    if not grades:
        return "failed"
    average = round(sum(grade.score for grade in grades) / len(grades))
    if average < target_score:
        return "failed"
    if any(grade.score < 70 for grade in grades):
        return "failed"
    return "passed"


def _escape_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def render_scorecard(
    grades: Sequence[ReportGrade],
    *,
    root: Path = REPO_ROOT,
    generated_at: datetime | None = None,
    min_reports: int = MIN_REPORTS,
    target_score: int = TARGET_SCORE,
) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    average = round(sum(grade.score for grade in grades) / len(grades)) if grades else 0
    status = benchmark_status(grades, min_reports=min_reports, target_score=target_score)
    lowest = min((grade.score for grade in grades), default=0)
    lines = [
        "# CHILI Agent Report Replay Benchmark",
        "",
        f"- Schema: {REPORT_REPLAY_SCHEMA_VERSION}",
        f"- Generated UTC: {generated_at.isoformat().replace('+00:00', 'Z')}",
        f"- Status: {status}",
        f"- Target score: {target_score}",
        f"- Minimum reports: {min_reports}",
        f"- Reports graded: {len(grades)}",
        f"- Average score: {average}/100",
        f"- Lowest score: {lowest}/100",
        "- Required receipt shape: scope, evidence/checks, findings, risks, next action, and explicit no-unsafe-action boundary.",
        "- Safety: read-only markdown replay; no source, runtime, git, broker, database, migration, or deployment action.",
        "",
        "| Report | Score | Status | Evidence markers | Missing |",
        "| --- | ---: | --- | --- | --- |",
    ]
    for grade in grades:
        missing = ", ".join(grade.missing) or "none"
        if grade.repaired_by is not None:
            missing = (
                f"repaired by {_relative(grade.repaired_by, root)}"
                f" ({grade.repair_score}/100)"
            )
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_cell(_relative(grade.path, root)),
                    str(grade.score),
                    _escape_cell(grade.status),
                    _escape_cell(", ".join(grade.evidence_markers) or "none"),
                    _escape_cell(missing),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def run_replay_benchmark(
    *,
    root: Path = REPO_ROOT,
    output_path: Path = DEFAULT_OUTPUT,
    min_reports: int = MIN_REPORTS,
    target_score: int = TARGET_SCORE,
    max_reports: int = MAX_REPORTS,
    min_age_seconds: float = 0.0,
    write: bool = True,
) -> tuple[list[ReportGrade], str, Path]:
    reports = discover_reports(
        root,
        max_reports=max_reports,
        min_age_seconds=min_age_seconds,
    )
    grades = [grade_report(path, root=root) for path in reports]
    markdown = render_scorecard(
        grades,
        root=root,
        min_reports=min_reports,
        target_score=target_score,
    )
    if write:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
    return grades, markdown, output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay-grade recent CHILI agent reports for Codex-class receipt quality.")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--min-reports", type=int, default=MIN_REPORTS)
    parser.add_argument("--max-reports", type=int, default=MAX_REPORTS)
    parser.add_argument("--min-age-seconds", type=float, default=0.0)
    parser.add_argument("--min-score", type=int, default=TARGET_SCORE)
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    grades, markdown, output_path = run_replay_benchmark(
        root=args.root,
        output_path=args.output,
        min_reports=args.min_reports,
        target_score=args.min_score,
        max_reports=args.max_reports,
        min_age_seconds=args.min_age_seconds,
        write=not args.no_write,
    )
    status = benchmark_status(grades, min_reports=args.min_reports, target_score=args.min_score)
    if args.json:
        payload = {
            "schema": REPORT_REPLAY_SCHEMA_VERSION,
            "status": status,
            "reports_graded": len(grades),
            "average_score": round(sum(grade.score for grade in grades) / len(grades)) if grades else 0,
            "grades": [
                {
                    "path": _relative(grade.path, args.root),
                    "score": grade.score,
                    "status": grade.status,
                    "missing": list(grade.missing),
                    "evidence_markers": list(grade.evidence_markers),
                    "repaired_by": _relative(grade.repaired_by, args.root) if grade.repaired_by else "",
                    "repair_score": grade.repair_score,
                }
                for grade in grades
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif args.no_write:
        print(markdown)
    else:
        print(f"Wrote {output_path}")
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
