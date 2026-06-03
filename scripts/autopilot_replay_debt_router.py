from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import autopilot_archived_task_replay_benchmark as archived_benchmark  # noqa: E402
import autopilot_report_replay_benchmark as report_benchmark  # noqa: E402


REPLAY_DEBT_ROUTER_SCHEMA_VERSION = "chili.replay-debt-router.v1"
DEFAULT_MIN_REPORT_SCORE = report_benchmark.TARGET_SCORE
DEFAULT_MIN_ARCHIVED_SCORE = archived_benchmark.TARGET_SCORE
DEFAULT_MAX_REPORTS = 40
REQUIRED_RECEIPT_SECTIONS = (
    "Generated UTC",
    "## Scope / Request",
    "## Evidence / Checks",
    "## Findings / Deliverables",
    "## Next Action / Routing",
    "## Safety Boundary",
)
NO_UNSAFE_ACTION_BOUNDARY = (
    "No source, runtime, git, database, broker, release, deployment, migration, "
    "model-promotion, capital, breaker, or live-trading action is authorized by this request."
)


@dataclasses.dataclass(frozen=True)
class ReplayDebtItem:
    source: str
    path: str
    agent: str
    score: int
    status: str
    missing: tuple[str, ...]
    sha256: str
    evidence_markers: tuple[str, ...]
    semantic_classes: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class ReplayDebtRoute:
    agent: str
    priority: str
    backlog_id: str
    items: tuple[ReplayDebtItem, ...]
    request_markdown: str
    output_path: str
    existing_path: str = ""
    standing_source_guard_path: str = ""
    related_replay_request_path: str = ""
    durable_guard_target_path: str = ""
    durable_guard_checks: tuple[str, ...] = ()
    one_off_missing_families: tuple[str, ...] = ()
    coordination_resolution: str = "new_request"
    written: bool = False
    sha256: str = ""
    source_guard_required: bool = False
    recurrence_count: int = 0
    recurrence_paths: tuple[str, ...] = ()
    recurring_missing: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "agent": self.agent,
            "priority": self.priority,
            "backlog_id": self.backlog_id,
            "item_count": len(self.items),
            "items": [dataclasses.asdict(item) for item in self.items],
            "output_path": self.output_path,
            "existing_path": self.existing_path,
            "standing_source_guard_path": self.standing_source_guard_path,
            "related_replay_request_path": self.related_replay_request_path,
            "durable_guard_target_path": self.durable_guard_target_path,
            "durable_guard_checks": list(self.durable_guard_checks),
            "one_off_missing_families": list(self.one_off_missing_families),
            "coordination_resolution": self.coordination_resolution,
            "written": self.written,
            "sha256": self.sha256,
            "source_guard_required": self.source_guard_required,
            "recurrence_count": self.recurrence_count,
            "recurrence_paths": list(self.recurrence_paths),
            "recurring_missing": list(self.recurring_missing),
        }


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _agent_from_report_path(path: str) -> str:
    parts = Path(path).parts
    if len(parts) >= 3 and parts[0] == "project_ws":
        return parts[1]
    normalized = path.replace("\\", "/").split("/")
    if len(normalized) >= 3 and normalized[0] == "project_ws":
        return normalized[1]
    return "AgentOps"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _request_fingerprint(agent: str, items: Sequence[ReplayDebtItem]) -> str:
    material = json.dumps(
        {
            "agent": agent,
            "items": [
                {
                    "source": item.source,
                    "path": item.path,
                    "sha256": item.sha256,
                    "missing": item.missing,
                }
                for item in items
            ],
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def _slug(text: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return clean[:70].strip("-") or "request"


def _has_existing_request(
    root: Path,
    agent: str,
    items: Sequence[ReplayDebtItem],
    *,
    require_source_guard: bool = False,
) -> str:
    inbox = root / "project_ws" / agent / "IN"
    if not inbox.is_dir():
        return ""
    required_paths = [item.path for item in items]
    for candidate in sorted(inbox.glob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True):
        try:
            text = candidate.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if "report replay debt" not in text.lower() and "replay-grade" not in text.lower():
            continue
        if require_source_guard and not re.search(r"(?im)^##+\s+Recurrence Guard\b", text):
            continue
        if all(path in text for path in required_paths):
            return _relative(candidate, root)
    return ""


def _list_after_label(text: str, label: str) -> tuple[str, ...]:
    values: list[str] = []
    in_list = False
    normalized_label = label.strip().lower()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not in_list:
            if line.lower() == normalized_label:
                in_list = True
            continue
        if not line:
            if values:
                break
            continue
        if not re.match(r"^\s*[-*]\s+", raw_line):
            if values:
                break
            continue
        clean = re.sub(r"^\s*[-*]\s*", "", raw_line).strip()
        if clean:
            values.append(clean)
    return tuple(dict.fromkeys(values))


def _source_guard_families_from_request_text(text: str) -> tuple[str, ...]:
    families: list[str] = []
    for item in _list_after_label(text, "Recurring missing families:"):
        families.extend(_missing_families(item))
    return tuple(dict.fromkeys(families))


def _has_standing_source_guard_request(
    root: Path,
    agent: str,
    recurring_missing: Sequence[str],
) -> str:
    inbox = root / "project_ws" / agent / "IN"
    if not inbox.is_dir() or not recurring_missing:
        return ""
    wanted = {
        family
        for missing in recurring_missing
        for family in _missing_families(missing)
    }
    if not wanted:
        return ""
    for candidate in sorted(
        inbox.glob("*report-replay-debt*.md"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    ):
        try:
            text = candidate.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if not re.search(r"(?im)^##+\s+Recurrence Guard\b", text):
            continue
        if "required source guard" not in text.lower():
            continue
        request_families = set(_source_guard_families_from_request_text(text))
        if request_families & wanted:
            return _relative(candidate, root)
    return ""


def _has_related_replay_request(
    root: Path,
    agent: str,
    recurring_missing: Sequence[str],
) -> str:
    inbox = root / "project_ws" / agent / "IN"
    if not inbox.is_dir() or not recurring_missing:
        return ""
    wanted = {
        family
        for missing in recurring_missing
        for family in _missing_families(missing)
    }
    if not wanted:
        return ""
    for candidate in sorted(
        inbox.glob("*report-replay-debt*.md"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    ):
        try:
            text = candidate.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if "report replay debt" not in text.lower() and "replay-grade" not in text.lower():
            continue
        request_families = set(_missing_families_from_request_text(text))
        if request_families & wanted:
            return _relative(candidate, root)
    return ""


def _missing_families(label: str) -> tuple[str, ...]:
    normalized = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
    families: list[str] = []
    if "pursuing goal" in normalized or "scope" in normalized or "request" in normalized:
        families.append("pursuing-goal / scope anchor")
    if "next action" in normalized or "routing" in normalized or "decision" in normalized:
        families.append("next-action routing")
    if "finding" in normalized or "deliverable" in normalized:
        families.append("findings / deliverables")
    if "unsafe" in normalized or "safety" in normalized or "boundary" in normalized:
        families.append("safety boundary")
    if "live control" in normalized or "broker" in normalized or "operator authority" in normalized:
        families.append("safety boundary")
    if "ui visual" in normalized or ("visual" in normalized and "rendered" in normalized):
        families.append("visual/rendered-state criteria")
    if "evidence" in normalized or "check" in normalized or "marker" in normalized:
        families.append("evidence / checks")
    if "timestamp" in normalized or "generated" in normalized:
        families.append("generated timestamp")
    if "title" in normalized:
        families.append("title")
    if not families:
        families.append(normalized or label)
    return tuple(dict.fromkeys(families))


def _missing_family(label: str) -> str:
    return _missing_families(label)[0]


def _recurrence_by_missing_family(items: Sequence[ReplayDebtItem]) -> dict[str, tuple[str, ...]]:
    paths_by_family: dict[str, set[str]] = {}
    for item in items:
        for missing in item.missing:
            for family in _missing_families(missing):
                paths_by_family.setdefault(family, set()).add(item.path)
    return {
        family: tuple(sorted(paths))
        for family, paths in sorted(paths_by_family.items())
        if len(paths) >= 2
    }


def _missing_families_from_request_text(text: str) -> tuple[str, ...]:
    families: list[str] = []
    for match in re.findall(
        r"(?i)missing(?: replay markers)?:\s*(.*?)(?:,\s*classes:|\n|\))",
        text,
    ):
        for family in _missing_families(match):
            families.append(family)
    return tuple(dict.fromkeys(families))


def _historical_replay_request_context(
    root: Path,
    agent: str,
    items: Sequence[ReplayDebtItem],
    *,
    max_requests: int = 6,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    inbox = root / "project_ws" / agent / "IN"
    if not inbox.is_dir():
        return (), ()

    report_path_pattern = re.compile(
        rf"project_ws[/\\]{re.escape(agent)}[/\\]OUT[/\\][^\s`|)]+\.md",
        re.IGNORECASE,
    )
    paths = {item.path for item in items}
    current_families = {
        family
        for item in items
        for missing in item.missing
        for family in _missing_families(missing)
    }
    historical_families: set[str] = set()
    request_count = 0
    candidates = sorted(
        inbox.glob("*report-replay-debt*.md"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if "report replay debt" not in text.lower() and "replay-grade" not in text.lower():
            continue
        extracted_paths = {
            match.replace("\\", "/").rstrip(".,;")
            for match in report_path_pattern.findall(text)
        }
        extracted_families = set(_missing_families_from_request_text(text))
        if not extracted_paths and not extracted_families:
            continue
        paths.update(extracted_paths)
        historical_families.update(extracted_families)
        request_count += 1
        if request_count >= max_requests:
            break

    if request_count == 0 or len(paths) < 2:
        return (), ()
    recurring_families = current_families & historical_families
    if not recurring_families:
        return (), ()
    return tuple(sorted(paths)), tuple(sorted(recurring_families))


def _recurrence_paths(
    items: Sequence[ReplayDebtItem],
    *,
    historical_paths: Sequence[str] = (),
) -> tuple[str, ...]:
    if historical_paths:
        return tuple(sorted({*historical_paths, *(item.path for item in items)}))
    recurring = _recurrence_by_missing_family(items)
    if recurring:
        paths = {path for paths in recurring.values() for path in paths}
        return tuple(sorted(paths))
    unique_paths = tuple(sorted({item.path for item in items}))
    return unique_paths if len(unique_paths) >= 3 else ()


def _recurring_missing(
    items: Sequence[ReplayDebtItem],
    *,
    historical_missing: Sequence[str] = (),
) -> tuple[str, ...]:
    if historical_missing:
        return tuple(sorted({*historical_missing}))
    recurring = _recurrence_by_missing_family(items)
    if recurring:
        return tuple(sorted(recurring))
    unique_paths = {item.path for item in items}
    if len(unique_paths) >= 3:
        return ("replay-grade receipt debt",)
    return ()


def _source_guard_required(
    items: Sequence[ReplayDebtItem],
    *,
    historical_paths: Sequence[str] = (),
    historical_missing: Sequence[str] = (),
) -> bool:
    return bool(
        _recurring_missing(items, historical_missing=historical_missing)
        and _recurrence_paths(items, historical_paths=historical_paths)
    )


def _route_missing_families(items: Sequence[ReplayDebtItem]) -> tuple[str, ...]:
    families: list[str] = []
    for item in items:
        for missing in item.missing:
            families.extend(_missing_families(missing))
    return tuple(dict.fromkeys(families))


def _one_off_coordination_resolution(missing_families: Sequence[str]) -> str:
    families = {
        str(family or "").strip()
        for family in missing_families
        if str(family or "").strip()
    }
    if "visual/rendered-state criteria" in families:
        return "one_off_visual_evidence_required"
    if "findings / deliverables" in families and "next-action routing" in families:
        return "one_off_decision_routing_required"
    if families == {"title"}:
        return "one_off_title_repair_required"
    return "one_off_replay_repair_required"


def _one_off_resolution_label(coordination_resolution: str) -> str:
    if coordination_resolution == "one_off_visual_evidence_required":
        return "one-off visual evidence repair"
    if coordination_resolution == "one_off_decision_routing_required":
        return "one-off decision/routing repair"
    if coordination_resolution == "one_off_title_repair_required":
        return "one-off title repair"
    return "one-off replay receipt repair"


def _one_off_resolution_guidance(
    coordination_resolution: str,
    missing_families: Sequence[str],
) -> tuple[str, ...]:
    if coordination_resolution == "one_off_decision_routing_required":
        return (
            "Decision/routing repair:",
            "- State the concrete finding, deliverable, unresolved gap, or explicit no-op result.",
            "- Name the next owner, blocker, follow-up request, or no-owner rationale in `## Next Action / Routing`.",
        )
    if coordination_resolution == "one_off_visual_evidence_required":
        return (
            "Visual evidence repair:",
            "- Cite rendered-state proof, screenshots, viewport checks, or explicit visual criteria inspected.",
        )
    if coordination_resolution == "one_off_title_repair_required":
        return (
            "Title repair:",
            "- Start the addendum with a level-one Markdown title before metadata.",
        )
    if missing_families:
        return (
            "Receipt repair:",
            "- Fill the missing receipt families above with replayable evidence and owner routing.",
        )
    return ()


def _priority_for(items: Sequence[ReplayDebtItem], *, source_guard_required: bool = False) -> str:
    if any(item.score < 70 for item in items) or source_guard_required or _source_guard_required(items):
        return "High"
    return "Medium"


def _backlog_for(items: Sequence[ReplayDebtItem]) -> str:
    if any(item.score < 70 for item in items):
        return "FASTLANE"
    return "QUALITY"


def _missing_summary(items: Sequence[ReplayDebtItem]) -> str:
    missing = sorted({entry for item in items for entry in item.missing})
    return ", ".join(missing) if missing else "none"


FAMILY_SOURCE_GUARD_CHECKS = {
    "title": (
        "Start every OUT report with a level-one Markdown title before metadata, "
        "for example `# MLOps Run Report`."
    ),
    "generated timestamp": (
        "Include `Generated UTC: <ISO-8601 UTC>` immediately under the title or in front matter."
    ),
    "pursuing-goal / scope anchor": (
        "Name the active objective, request/run id, current gate, and inspected inbox or artifact path "
        "in `## Scope / Request` before any status prose."
    ),
    "evidence / checks": (
        "Publish `## Evidence / Checks` with exact paths, commands/probes, timestamps, SHAs, PR/run ids, "
        "and read-only proof; a thin status line is not enough."
    ),
    "findings / deliverables": (
        "Publish `## Findings / Deliverables` with the decision, artifact/addendum produced, unresolved gap, "
        "or explicit no-op finding."
    ),
    "next-action routing": (
        "Publish `## Next Action / Routing` with the next owner, blocker, no-op rationale, or follow-up request; "
        "do not rely on implied ownership."
    ),
    "safety boundary": (
        "Publish `## Safety Boundary` naming the actions not performed or authorized, including source, git, "
        "runtime, DB/migration, broker/order, release/deploy, model, capital, breaker, live-trading paths, "
        "operator authority, and broker-truth separation."
    ),
    "visual/rendered-state criteria": (
        "For UI reports, include rendered-state evidence or explicit desktop/mobile/keyboard/screenshot criteria "
        "and state that visual copy does not authorize runtime or live-control action."
    ),
    "replay-grade receipt debt": (
        "Run the replay-grade receipt checklist before publication and fix the report draft before writing it to OUT."
    ),
}


def _source_guard_checklist(families: Sequence[str]) -> tuple[str, ...]:
    checks: list[str] = []
    for family in families:
        normalized = str(family or "").strip()
        check = FAMILY_SOURCE_GUARD_CHECKS.get(normalized)
        if check:
            checks.append(check)
        elif normalized:
            checks.append(
                f"Add a pre-publish check for `{normalized}` and cite where that check lives."
            )
    return tuple(dict.fromkeys(checks))


def _durable_guard_target_path(root: Path, agent: str) -> str:
    agent_root = root / "project_ws" / agent
    candidates = (
        agent_root / "AUTOMATION_PROMPT.md",
        agent_root / "AUTOMATION_PROMPT_APPEND.md",
        root / "project_ws" / "AgentOps" / "UNIVERSAL_FAST_FLOW_PROMPT_APPEND.md",
    )
    for candidate in candidates:
        if candidate.is_file():
            return _relative(candidate, root)
    return _relative(candidates[-1], root)


def _render_request(
    *,
    agent: str,
    items: Sequence[ReplayDebtItem],
    created_utc: str,
    source_guard_required: bool | None = None,
    recurrence_paths: Sequence[str] = (),
    recurring_missing: Sequence[str] = (),
    standing_source_guard_path: str = "",
    related_replay_request_path: str = "",
    durable_guard_target_path: str = "",
    durable_guard_checks: Sequence[str] = (),
    one_off_missing_families: Sequence[str] = (),
) -> str:
    rows = []
    for item in items:
        classes = ", ".join(item.semantic_classes) if item.semantic_classes else "n/a"
        rows.append(
            f"- {item.path} SHA256 {item.sha256} "
            f"({item.source}, score {item.score}/100, missing: {', '.join(item.missing) or 'none'}, classes: {classes})"
        )
    receipt_sections = "\n".join(f"- `{section}`" for section in REQUIRED_RECEIPT_SECTIONS)
    missing_summary = _missing_summary(items)
    if source_guard_required is None:
        source_guard_required = _source_guard_required(items)
    recurrence_paths = tuple(recurrence_paths) or _recurrence_paths(items)
    recurring_missing = tuple(recurring_missing) or _recurring_missing(items)
    recurrence_section: list[str] = []
    if source_guard_required:
        source_guard_checks = _source_guard_checklist(recurring_missing)
        durable_guard_checks = tuple(durable_guard_checks) or source_guard_checks
        recurrence_section = [
            "",
            "## Recurrence Guard",
            (
                f"This is recurring {agent} replay debt, not just a one-off addendum. Treat it as "
                "a report-publishing habit/template failure before publishing another OUT report."
            ),
            "",
            "Recurring missing families:",
            *[f"- {missing}" for missing in recurring_missing],
            "",
            "Repeated report paths:",
            *[f"- {path}" for path in recurrence_paths],
            "",
            *(
                [
                    "Existing source guard request:",
                    f"- {standing_source_guard_path}",
                    (
                        "- Coordination rule: use this as the standing guard; do not "
                        "create a parallel source-guard thread for the same missing family."
                    ),
                    (
                        "- Current flagged reports still need replay-grade repair evidence; "
                        "close or update the standing guard only after the checklist is named."
                    ),
                    "",
                ]
                if standing_source_guard_path
                else []
            ),
            *(
                [
                    "Related prior replay request:",
                    f"- {related_replay_request_path}",
                    (
                        "- Coordination rule: cite whether this prior request is still pending, "
                        "superseded, or closed before opening another repair thread for the same family."
                    ),
                    (
                        "- Current flagged reports still need replay-grade repair evidence; "
                        "do not treat prior routing as proof that the new reports are fixed."
                    ),
                    "",
                ]
                if related_replay_request_path and not standing_source_guard_path
                else []
            ),
            *(
                [
                    "Durable guard target:",
                    f"- {durable_guard_target_path}",
                    "",
                    "Suggested durable guard checks:",
                    *[f"- {check}" for check in durable_guard_checks],
                    "",
                ]
                if durable_guard_target_path
                else []
            ),
            "Required pre-publish checks:",
            *[f"- {check}" for check in source_guard_checks],
            "",
            (
                "Required source guard: in the addendum, name the exact pre-publish checklist or "
                "mailbox/reporting-template rule you will use so future reports start with a "
                "`Pursuing goal`/scope anchor and include evidence, findings, routing, and safety "
                "before they are written to OUT."
            ),
        ]
    one_off_section: list[str] = []
    if not source_guard_required:
        one_off_missing_families = tuple(one_off_missing_families) or _route_missing_families(items)
        one_off_resolution = _one_off_coordination_resolution(one_off_missing_families)
        one_off_guidance = _one_off_resolution_guidance(
            one_off_resolution,
            one_off_missing_families,
        )
        one_off_section = [
            "",
            "## One-Off Repair Classification",
            f"Coordination resolution: {one_off_resolution}",
            f"Resolution: {_one_off_resolution_label(one_off_resolution)}",
            "",
            "Missing families:",
            *[f"- {family}" for family in one_off_missing_families],
            "",
            *one_off_guidance,
            *([""] if one_off_guidance else []),
            (
                "Coordination rule: publish the replay-grade addendum for these flagged "
                "reports; do not create a durable source guard unless the same missing "
                "family recurs in a later report."
            ),
        ]
    lines = [
            "From: AgentOps",
            f"To: {agent}",
            f"Created: {created_utc}",
            "Reply-To: project_ws/AgentOps/OUT",
            f"Priority: {_priority_for(items, source_guard_required=source_guard_required)}",
            f"Backlog-ID: {_backlog_for(items)}",
            "Push Intent: none",
            "",
            "## Request",
            (
                f"CHILI replay benchmarks found report replay debt in {len(items)} {agent} OUT "
                "report(s). Publish a replay-grade OUT addendum that cites every flagged report "
                "path and SHA256, fills the missing receipt sections, and routes the next owner "
                "action without editing finalized reports in place."
            ),
    ]
    if source_guard_required:
        lines.extend(
            [
                "",
                (
                    "Because this pattern repeated across recent reports, also publish a durable "
                    "Pursuing goal recurrence guard: state the active objective anchor that every "
                    "future report must preserve, the current gate/check to run before OUT publication, "
                    "and the next owner/routing field that must be present."
                ),
            ]
        )
    lines.extend(
        [
            "",
            "Flagged reports:",
            *rows,
            *one_off_section,
            *recurrence_section,
            "",
            "## Expected Deliverable",
            (
                f"One {agent} OUT addendum, or one addendum per flagged report, that repairs the "
                "replay-grade receipt gaps and appends the processed receipt for this request after "
                "the addendum is final."
            ),
            "",
            "## Success Criteria",
            "- Addendum cites every flagged report path and exact SHA256 above.",
            f"- Addendum addresses missing replay markers: {missing_summary}.",
            "- Addendum uses this receipt shape:",
            receipt_sections,
            "- Evidence names exact files, commands/checks, report paths, SHAs, screenshots, scorecards, or read-only probes inspected.",
            "- Next Action / Routing names the owner, blocker, no-op rationale, or follow-up request.",
            "- Pursuing goal anchor names the active objective, current gate, evidence status, remaining risk, and next owner action.",
            "- Recurrence guard, when present, names the future pre-publish check that prevents this same report shape from recurring.",
            "- Processed ledger records this request path and exact SHA after final deliverable publication.",
            "",
            "## Context / Links",
            "- `project_ws/AgentOps/REPORT_REPLAY_BENCHMARK.md`.",
            "- `project_ws/AgentOps/ARCHIVED_TASK_REPLAY_BENCHMARK.md`.",
            "- `project_ws/AgentOps/AUTOMATION_PROMPT.md` replay-grade OUT report contract.",
            "- `project_ws/AgentOps/UNIVERSAL_FAST_FLOW_PROMPT_APPEND.md` Pursuing goal receipt reminder.",
            "",
            "## Safety Constraints",
            (
                "Mailbox/evidence correction only. Do not edit finalized OUT files in place. "
                f"{NO_UNSAFE_ACTION_BOUNDARY} Do not mutate branches, PR state, runtime services, "
                "Docker/services, databases, broker/order state, breakers, capital/model state, "
                "monitors, route/cutover state, or live behavior from this request."
            ),
            "",
            "## Dependencies",
            "AgentOps replay benchmark scorecards and the replay-grade OUT report contract.",
            "",
            "## Peer Review / Push",
            "No push. If implementation is required later, route through PM and use a clean owner worktree with normal review/push gates.",
            "",
        ]
    )
    return "\n".join(lines)


def collect_replay_debt(
    *,
    root: Path = REPO_ROOT,
    min_report_score: int = DEFAULT_MIN_REPORT_SCORE,
    min_archived_score: int = DEFAULT_MIN_ARCHIVED_SCORE,
    max_reports: int = DEFAULT_MAX_REPORTS,
    min_age_seconds: float = 0.0,
) -> list[ReplayDebtItem]:
    report_grades, _, _ = report_benchmark.run_replay_benchmark(
        root=root,
        min_reports=report_benchmark.MIN_REPORTS,
        target_score=min_report_score,
        max_reports=max_reports,
        min_age_seconds=min_age_seconds,
        write=False,
    )
    archived_grades, _, _ = archived_benchmark.run_archived_task_replay_benchmark(
        root=root,
        min_reports=archived_benchmark.MIN_REPORTS,
        target_score=min_archived_score,
        max_reports=max_reports,
        min_age_seconds=min_age_seconds,
        write=False,
    )
    items: list[ReplayDebtItem] = []
    for grade in report_grades:
        if grade.score >= min_report_score and not grade.missing:
            continue
        rel = _relative(grade.path, root)
        items.append(
            ReplayDebtItem(
                source="report_replay",
                path=rel,
                agent=_agent_from_report_path(rel),
                score=grade.score,
                status=grade.status,
                missing=tuple(grade.missing),
                sha256=_sha256(grade.path),
                evidence_markers=tuple(grade.evidence_markers),
            )
        )
    for grade in archived_grades:
        if grade.score >= min_archived_score and not grade.missing:
            continue
        rel = _relative(grade.path, root)
        items.append(
            ReplayDebtItem(
                source="archived_task_replay",
                path=rel,
                agent=_agent_from_report_path(rel),
                score=grade.score,
                status=grade.status,
                missing=tuple(grade.missing),
                sha256=_sha256(grade.path),
                evidence_markers=tuple(grade.evidence_markers),
                semantic_classes=tuple(grade.semantic_classes),
            )
        )
    return items


def build_routes(
    items: Sequence[ReplayDebtItem],
    *,
    root: Path = REPO_ROOT,
    created_utc: str | None = None,
    write: bool = False,
) -> list[ReplayDebtRoute]:
    created_utc = created_utc or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    grouped: dict[str, list[ReplayDebtItem]] = {}
    for item in items:
        grouped.setdefault(item.agent, []).append(item)

    routes: list[ReplayDebtRoute] = []
    for agent in sorted(grouped):
        agent_items = tuple(
            sorted(
                grouped[agent],
                key=lambda item: (item.score, item.path, item.source),
            )
        )
        fingerprint = _request_fingerprint(agent, agent_items)
        stamp = created_utc.replace("-", "").replace(":", "").replace("T", "-").replace("Z", "Z")
        name = f"{stamp}-from-AgentOps-to-{agent}-{_slug('report replay debt')}-{fingerprint}.md"
        output = root / "project_ws" / agent / "IN" / name
        output_rel = _relative(output, root)
        historical_paths, historical_missing = _historical_replay_request_context(root, agent, agent_items)
        recurrence_paths = _recurrence_paths(agent_items, historical_paths=historical_paths)
        recurring_missing = _recurring_missing(agent_items, historical_missing=historical_missing)
        source_guard_required = bool(recurring_missing and recurrence_paths)
        standing_guard = (
            _has_standing_source_guard_request(root, agent, recurring_missing)
            if source_guard_required
            else ""
        )
        related_request = (
            ""
            if standing_guard or not source_guard_required
            else _has_related_replay_request(root, agent, recurring_missing)
        )
        durable_guard_target = (
            _durable_guard_target_path(root, agent) if source_guard_required else ""
        )
        durable_guard_checks = (
            _source_guard_checklist(recurring_missing)
            if source_guard_required
            else ()
        )
        one_off_missing_families = (
            () if source_guard_required else _route_missing_families(agent_items)
        )
        one_off_resolution = _one_off_coordination_resolution(one_off_missing_families)
        existing = _has_existing_request(
            root,
            agent,
            agent_items,
            require_source_guard=source_guard_required,
        )
        request = _render_request(
            agent=agent,
            items=agent_items,
            created_utc=created_utc,
            source_guard_required=source_guard_required,
            recurrence_paths=recurrence_paths,
            recurring_missing=recurring_missing,
            standing_source_guard_path=standing_guard,
            related_replay_request_path=related_request,
            durable_guard_target_path=durable_guard_target,
            durable_guard_checks=durable_guard_checks,
            one_off_missing_families=one_off_missing_families,
        )
        coordination_resolution = (
            "existing_request"
            if existing
            else "standing_source_guard_linked"
            if standing_guard
            else "related_replay_request_linked"
            if related_request
            else "durable_source_guard_required"
            if source_guard_required
            else one_off_resolution
        )
        written = False
        digest = ""
        if write and not existing:
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.exists():
                raise FileExistsError(f"Refusing to overwrite existing mailbox request: {output}")
            output.write_text(request, encoding="utf-8")
            digest = _sha256(output)
            written = True
        routes.append(
            ReplayDebtRoute(
                agent=agent,
                priority=_priority_for(agent_items, source_guard_required=source_guard_required),
                backlog_id=_backlog_for(agent_items),
                items=agent_items,
                request_markdown=request,
                output_path=output_rel,
                existing_path=existing,
                standing_source_guard_path=standing_guard,
                related_replay_request_path=related_request,
                durable_guard_target_path=durable_guard_target,
                durable_guard_checks=durable_guard_checks,
                one_off_missing_families=one_off_missing_families,
                coordination_resolution=coordination_resolution,
                written=written,
                sha256=digest,
                source_guard_required=source_guard_required,
                recurrence_count=len(recurrence_paths),
                recurrence_paths=recurrence_paths,
                recurring_missing=recurring_missing,
            )
        )
    return routes


def render_summary(routes: Sequence[ReplayDebtRoute]) -> str:
    lines = [
        "# CHILI Replay Debt Routing Plan",
        "",
        f"- Schema: {REPLAY_DEBT_ROUTER_SCHEMA_VERSION}",
        f"- Routes: {len(routes)}",
        f"- Flagged reports: {sum(len(route.items) for route in routes)}",
        "- Safety: dry-run unless `--write` is passed; generated requests are mailbox/evidence correction only.",
        "",
        "| Agent | Priority | Reports | Source Guard | Missing | Output |",
        "| --- | --- | ---: | --- | --- | --- |",
    ]
    for route in routes:
        output = route.existing_path or route.output_path
        lines.append(
            "| "
            + " | ".join(
                [
                    route.agent,
                    route.priority,
                    str(len(route.items)),
                    "yes" if route.source_guard_required else "no",
                    _missing_summary(route.items).replace("|", "\\|"),
                    output.replace("|", "\\|"),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Route CHILI replay benchmark debt to owning agent inboxes."
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--min-report-score", type=int, default=DEFAULT_MIN_REPORT_SCORE)
    parser.add_argument("--min-archived-score", type=int, default=DEFAULT_MIN_ARCHIVED_SCORE)
    parser.add_argument("--max-reports", type=int, default=DEFAULT_MAX_REPORTS)
    parser.add_argument("--min-age-seconds", type=float, default=0.0)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--fail-on-debt", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    items = collect_replay_debt(
        root=args.root,
        min_report_score=args.min_report_score,
        min_archived_score=args.min_archived_score,
        max_reports=args.max_reports,
        min_age_seconds=args.min_age_seconds,
    )
    routes = build_routes(items, root=args.root, write=args.write)
    if args.json:
        print(
            json.dumps(
                {
                    "schema": REPLAY_DEBT_ROUTER_SCHEMA_VERSION,
                    "route_count": len(routes),
                    "item_count": len(items),
                    "written_count": sum(1 for route in routes if route.written),
                    "routes": [route.to_json() for route in routes],
                },
                indent=2,
            )
        )
    else:
        print(render_summary(routes))
        if not args.write:
            print("Dry run only. Re-run with --write to publish mailbox repair requests.")
    return 1 if args.fail_on_debt and items else 0


if __name__ == "__main__":
    raise SystemExit(main())
