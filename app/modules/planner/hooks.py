from __future__ import annotations

import json as json_mod
import re
from datetime import date, timedelta
from math import ceil

from sqlalchemy.orm import Session

from ... import openai_client
from ...logger import log_info
from ...services import planner_service


_CREATE_PROJECT_AND_TASKS = re.compile(
    r"(?i)\b(?:create|make)\s+(?:a\s+)?project\s+(?:for\s+(?:me\s+)?)?(?:for\s+)?([^!.\n]+?)(?:\s*[!.]|\s+also\s+add|\s+and\s+add|\s+add\s+in\s+the\s+tasks|\s*$)",
)
_ADD_TASKS_PATTERN = re.compile(
    r"(?i)\b(?:add\s+(?:in\s+)?the\s+)?tasks?\b|tasks?\s+(?:you\s+think\s+)?(?:i\s+)?need\s+to\s+do|add\s+in\s+the\s+tasks|suggest\s+tasks|tasks?\s+in\s+which|what\s+tasks?\s+(?:i\s+)?(?:need\s+to\s+)?do"
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


def generate_tasks_for_project(
    db: Session,
    project_id: int,
    project_name: str,
    user_id: int,
    trace_id: str,
) -> int:
    """Use the cloud LLM to suggest tasks with well-researched ETAs and create them with start/end dates for Gantt."""
    if not openai_client.is_configured():
        return 0

    today = date.today().isoformat()
    prompt = (
        f'For a project called "{project_name}", suggest 6 to 12 concrete, actionable tasks. '
        "Use well-researched, realistic time estimates (industry benchmarks, common studies: "
        "e.g. resume update 2-4 hours, job application 1-2 hours each, interview prep 3-5 hours). "
        'Return ONLY a JSON array. Each object must have: "title" (string), '
        '"description" (string, include Complexity, Duration, Reasoning), and '
        '"estimated_days" (number, working days to complete). '
        "estimated_days: use decimals for part-days (e.g. 0.25 = ~2 hours, 0.5 = half day, 1 = one full day). "
        "Minimum 0.25. Be accurate based on typical task duration research. "
        f"Today is {today}. Tasks will be scheduled sequentially starting from today. "
        'Example: [{"title": "Update resume", "description": "Complexity: Low. Duration: 2-3 hours. '
        'Reasoning: ATS-friendly resume increases callback rate.", "estimated_days": 0.25}, '
        '{"title": "Apply to 5 target companies", "description": "Complexity: Medium. Duration: 5-10 hours total. '
        'Reasoning: Quality applications take 1-2 hrs each (research, tailoring).", "estimated_days": 1.5}]'
    )
    try:
        _system = (
            "You are a project planning assistant. Return only a valid JSON array. "
            "Every task must have title, description, and estimated_days (number)."
        )
        try:
            from ...services.context_brain.llm_gateway import gateway_chat
            result = gateway_chat(
                messages=[{"role": "user", "content": prompt}],
                purpose='planner_intent',
                system_prompt=_system,
                trace_id=trace_id,
            )
        except Exception:
            result = openai_client.chat(
                messages=[{"role": "user", "content": prompt}],
                system_prompt=_system,
                trace_id=trace_id,
            )
        text = (result.get("reply") or "").strip()
    except Exception as e:  # pragma: no cover - defensive logging
        log_info(trace_id, f"generate_tasks_for_project_error={e}")
        return 0

    start_idx = text.find("[")
    end_idx = text.rfind("]")
    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        return 0

    try:
        items = json_mod.loads(text[start_idx : end_idx + 1])
    except json_mod.JSONDecodeError:
        return 0
    if not isinstance(items, list):
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

