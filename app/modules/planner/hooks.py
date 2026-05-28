from __future__ import annotations

import re
from datetime import date, timedelta
from math import ceil

from sqlalchemy.orm import Session

from ...logger import log_info
from ...services import planner_service


_CREATE_PROJECT_AND_TASKS = re.compile(
    r"(?i)\b(?:create|make)\s+(?:a\s+)?project\s+(?:for\s+(?:me\s+)?)?(?:for\s+)?([^!.\n]+?)(?:\s*[!.]|\s+also\s+add|\s+and\s+add|\s+add\s+in\s+the\s+tasks|\s*$)",
)
_ADD_TASKS_PATTERN = re.compile(
    r"(?i)\b(?:add\s+(?:in\s+)?the\s+)?tasks?\b|tasks?\s+(?:you\s+think\s+)?(?:i\s+)?need\s+to\s+do|add\s+in\s+the\s+tasks|suggest\s+tasks|tasks?\s+in\s+which|what\s+tasks?\s+(?:i\s+)?(?:need\s+to\s+)?do"
)
_PROJECT_TEMPLATE_PATTERNS: tuple[tuple[re.Pattern[str], tuple[tuple[str, str, float], ...]], ...] = (
    (
        re.compile(r"(?i)\b(job|career|resume|cv|interview|apply|hiring)\b"),
        (
            (
                "Clarify target roles and success criteria",
                "Complexity: Low. Duration: 1-2 hours. Reasoning: Clear role filters prevent wasted applications and make later tailoring faster.",
                0.25,
            ),
            (
                "Refresh resume and core profile",
                "Complexity: Medium. Duration: 2-4 hours. Reasoning: A current ATS-friendly resume and profile are the reusable base for high-quality outreach.",
                0.5,
            ),
            (
                "Build a prioritized company list",
                "Complexity: Medium. Duration: 2-3 hours. Reasoning: Ranking targets by fit, timing, and referrals improves focus before applying.",
                0.5,
            ),
            (
                "Tailor the first application packet",
                "Complexity: Medium. Duration: 1-2 hours. Reasoning: A tailored resume and short cover note become the model for subsequent applications.",
                0.5,
            ),
            (
                "Submit the first application batch",
                "Complexity: Medium. Duration: 5-10 hours. Reasoning: Quality applications usually take focused research and customization.",
                1.5,
            ),
            (
                "Prepare interview stories and examples",
                "Complexity: Medium. Duration: 3-5 hours. Reasoning: STAR-format examples reduce interview friction and improve answer quality.",
                1.0,
            ),
            (
                "Set up tracking and follow-up cadence",
                "Complexity: Low. Duration: 1 hour. Reasoning: A tracker keeps next actions visible and prevents missed follow-ups.",
                0.25,
            ),
        ),
    ),
    (
        re.compile(r"(?i)\b(app|application|software|website|web\s*site|mobile|api|dashboard|saas|feature|prototype|mvp)\b"),
        (
            (
                "Define users, outcome, and scope",
                "Complexity: Medium. Duration: 2-4 hours. Reasoning: A narrow outcome and explicit non-goals keep the build from sprawling.",
                0.5,
            ),
            (
                "Map core user flows",
                "Complexity: Medium. Duration: 2-3 hours. Reasoning: Flow mapping exposes missing states before engineering starts.",
                0.5,
            ),
            (
                "Design the data model and interfaces",
                "Complexity: Medium. Duration: 3-5 hours. Reasoning: Stable data contracts reduce rework across frontend, backend, and tests.",
                1.0,
            ),
            (
                "Build the smallest usable version",
                "Complexity: High. Duration: 2-5 days. Reasoning: A thin end-to-end slice proves the architecture and creates something testable.",
                3.0,
            ),
            (
                "Add validation, empty states, and errors",
                "Complexity: Medium. Duration: 1-2 days. Reasoning: Production usability depends on predictable behavior outside the happy path.",
                1.5,
            ),
            (
                "Write focused tests for the main workflow",
                "Complexity: Medium. Duration: 1 day. Reasoning: Workflow tests catch regressions where users actually spend time.",
                1.0,
            ),
            (
                "Ship a review build and collect feedback",
                "Complexity: Low. Duration: 0.5-1 day. Reasoning: A review build turns assumptions into concrete fixes.",
                1.0,
            ),
        ),
    ),
    (
        re.compile(r"(?i)\b(trip|travel|vacation|holiday|itinerary|flight|hotel)\b"),
        (
            (
                "Set budget, dates, and constraints",
                "Complexity: Low. Duration: 1 hour. Reasoning: Budget and date boundaries make every later booking decision easier.",
                0.25,
            ),
            (
                "Research destinations and neighborhoods",
                "Complexity: Medium. Duration: 2-4 hours. Reasoning: Location choice drives cost, transit time, and daily experience.",
                0.5,
            ),
            (
                "Book transport and lodging",
                "Complexity: Medium. Duration: 2-3 hours. Reasoning: Booking the anchors early reduces price drift and schedule risk.",
                0.5,
            ),
            (
                "Draft day-by-day itinerary",
                "Complexity: Medium. Duration: 2-4 hours. Reasoning: A light itinerary balances must-do activities with recovery time.",
                0.5,
            ),
            (
                "Handle documents, insurance, and reservations",
                "Complexity: Medium. Duration: 1-2 hours. Reasoning: Confirming paperwork and reservations prevents avoidable travel issues.",
                0.5,
            ),
            (
                "Prepare packing and departure checklist",
                "Complexity: Low. Duration: 1 hour. Reasoning: A checklist reduces last-minute misses.",
                0.25,
            ),
        ),
    ),
    (
        re.compile(r"(?i)\b(move|moving|relocat|apartment|house|home|renovat|remodel)\b"),
        (
            (
                "Define scope, budget, and deadline",
                "Complexity: Low. Duration: 1-2 hours. Reasoning: Scope and budget control tradeoffs before vendors or purchases enter the picture.",
                0.25,
            ),
            (
                "Inventory spaces, items, and constraints",
                "Complexity: Medium. Duration: 2-4 hours. Reasoning: A clear inventory prevents underestimating labor, supplies, and dependencies.",
                0.5,
            ),
            (
                "Get quotes or reserve required help",
                "Complexity: Medium. Duration: 2-3 hours. Reasoning: Movers, contractors, and helpers often become the schedule bottleneck.",
                0.5,
            ),
            (
                "Order supplies and prepare staging areas",
                "Complexity: Low. Duration: 1-2 hours. Reasoning: Supplies and staging reduce friction once execution starts.",
                0.25,
            ),
            (
                "Execute the main work block",
                "Complexity: High. Duration: 1-3 days. Reasoning: The core move or renovation work needs protected time and coordination.",
                2.0,
            ),
            (
                "Inspect, clean up, and close loose ends",
                "Complexity: Medium. Duration: 0.5-1 day. Reasoning: A closeout pass catches damage, missing items, and unfinished details.",
                1.0,
            ),
        ),
    ),
)
_GENERIC_PROJECT_TEMPLATE: tuple[tuple[str, str, float], ...] = (
    (
        "Define the outcome and success criteria",
        "Complexity: Low. Duration: 1-2 hours. Reasoning: A concrete target, deadline, and definition of done prevent the project from turning into vague activity.",
        0.25,
    ),
    (
        "Capture constraints, assumptions, and unknowns",
        "Complexity: Low. Duration: 1-2 hours. Reasoning: Listing limits, risks, and open questions early makes the first execution pass sharper and cheaper.",
        0.25,
    ),
    (
        "Break the work into deliverables",
        "Complexity: Medium. Duration: 2-4 hours. Reasoning: Deliverable-level planning exposes dependencies and gives each work block a clear output.",
        0.5,
    ),
    (
        "Gather required materials and references",
        "Complexity: Medium. Duration: 2-4 hours. Reasoning: Preparing inputs before execution reduces context switching and prevents blocked work sessions.",
        0.5,
    ),
    (
        "Complete the first usable version",
        "Complexity: High. Duration: 1-3 days. Reasoning: A first complete pass turns the plan into something inspectable and makes remaining uncertainty concrete.",
        2.0,
    ),
    (
        "Review quality and close gaps",
        "Complexity: Medium. Duration: 0.5-1 day. Reasoning: A structured review catches missing pieces, unclear decisions, and places where expectations drifted.",
        1.0,
    ),
    (
        "Ship, share, or archive the result",
        "Complexity: Low. Duration: 1-2 hours. Reasoning: Closing the loop preserves the project output and creates a clear next action if more work is needed.",
        0.25,
    ),
)


def detect_create_project_with_tasks_intent(message: str) -> tuple[bool, str | None]:
    """Detect prompts like 'create a project for X and add the tasks I need to do'."""
    msg = (message or "").strip()
    if not msg or not _ADD_TASKS_PATTERN.search(msg):
        return False, None
    m = _CREATE_PROJECT_AND_TASKS.search(msg)
    if not m:
        return False, None
    name = m.group(1).strip()
    if len(name) < 2:
        return False, None
    return True, name.title()


def _mechanical_task_suggestions(project_name: str) -> list[dict]:
    """Return deterministic task suggestions for any named project."""
    name = (project_name or "").strip()
    if not name:
        return []

    for pattern, template in _PROJECT_TEMPLATE_PATTERNS:
        if not pattern.search(name):
            continue
        return [
            {
                "title": title,
                "description": f"{desc} Project: {name}.",
                "estimated_days": days,
            }
            for title, desc, days in template
        ]
    return [
        {
            "title": title,
            "description": f"{desc} Project: {name}.",
            "estimated_days": days,
        }
        for title, desc, days in _GENERIC_PROJECT_TEMPLATE
    ]


def generate_tasks_for_project(
    db: Session,
    project_id: int,
    project_name: str,
    user_id: int,
    trace_id: str,
) -> int:
    """Create deterministic suggested tasks without paying for planner LLM fallback."""
    items = _mechanical_task_suggestions(project_name)
    if items:
        log_info(trace_id, f"planner_mechanical_tasks project_id={project_id} count={len(items)}")
    else:
        return 0

    cursor = date.today()
    added = 0

    for item in items[:20]:
        if isinstance(item, dict) and item.get("title"):
            title = str(item.get("title", "")).strip()
            desc = str(item.get("description", "")).strip()
            raw_days = item.get("estimated_days")
            try:
                days = max(0.25, min(365, float(raw_days))) if raw_days is not None else 1.0
            except (TypeError, ValueError):
                days = 1.0
            task_start = cursor
            span_days = max(1, int(ceil(days)))  # at least 1 calendar day so bar is visible
            task_end = cursor + timedelta(days=span_days - 1)
            cursor = task_end + timedelta(days=1)
            start_str = task_start.isoformat()
            end_str = task_end.isoformat()
            if title and planner_service.create_task(
                db,
                project_id,
                user_id,
                title,
                description=desc,
                start_date=start_str,
                end_date=end_str,
            ):
                added += 1
        elif isinstance(item, str) and item.strip():
            start_str = cursor.isoformat()
            end_str = cursor.isoformat()
            if planner_service.create_task(
                db,
                project_id,
                user_id,
                item.strip(),
                description="",
                start_date=start_str,
                end_date=end_str,
            ):
                added += 1
                cursor += timedelta(days=1)

    return added

