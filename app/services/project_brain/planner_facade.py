"""A8: typed read facade for the planner module.

Before this facade the PM agent reached directly into ``planner_service``
with dict-shaped return values, so any change to planner task/project field
names would break the agent silently. The facade gives us:

1. A typed ``ProjectSummary`` / ``TaskSummary`` dataclass surface.
2. A single seam to stub in tests.
3. A place to centralize defensive null-handling (``status or "todo"``,
   ``start_date or end_date`` parsing) so the agent code stays short.

The facade is intentionally read-only — PM agent is a consumer, not an editor.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

_log = logging.getLogger("chili.project_brain.planner_facade")


@dataclass(frozen=True)
class ProjectSummary:
    id: int
    name: str
    status: str
    raw: dict


@dataclass(frozen=True)
class TaskSummary:
    id: int
    project_id: int | None
    title: str
    status: str
    priority: str | None
    start_date: date | None
    end_date: date | None
    raw: dict


def _parse_iso_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def list_user_projects(db: Session, user_id: int) -> list[ProjectSummary]:
    """Return the user's planner projects as typed ``ProjectSummary`` rows.
    Returns ``[]`` on any backend error — the PM agent is a passive consumer
    and should never crash because planner_service changed.
    """
    try:
        from ..planner_service import list_projects
        raw = list_projects(db, user_id) or []
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("list_user_projects failed: %s", exc)
        return []
    out: list[ProjectSummary] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        if pid is None:
            continue
        out.append(
            ProjectSummary(
                id=int(pid),
                name=str(p.get("name") or ""),
                status=str(p.get("status") or "active"),
                raw=p,
            )
        )
    return out


def list_project_tasks(
    db: Session, user_id: int, project_id: int | None
) -> list[TaskSummary]:
    """Return a project's tasks as typed ``TaskSummary`` rows, or ``[]`` on
    failure / missing project_id. Never raises.
    """
    if project_id is None:
        return []
    try:
        from ..planner_service import list_tasks
        raw = list_tasks(db, int(project_id), user_id) or []
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("list_project_tasks failed for project %s: %s", project_id, exc)
        return []
    out: list[TaskSummary] = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        tid = t.get("id")
        if tid is None:
            continue
        out.append(
            TaskSummary(
                id=int(tid),
                project_id=t.get("project_id") if isinstance(t.get("project_id"), int) else project_id,
                title=str(t.get("title") or ""),
                status=str(t.get("status") or "todo"),
                priority=t.get("priority"),
                start_date=_parse_iso_date(t.get("start_date")),
                end_date=_parse_iso_date(t.get("end_date")),
                raw=t,
            )
        )
    return out


def get_user_project_summary_text(db: Session, user_id: int) -> str:
    """Return the planner's free-form project summary text, or ``""`` on error."""
    try:
        from ..planner_service import get_user_project_summary
        return get_user_project_summary(db, user_id) or ""
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("get_user_project_summary failed: %s", exc)
        return ""


__all__ = [
    "ProjectSummary",
    "TaskSummary",
    "list_user_projects",
    "list_project_tasks",
    "get_user_project_summary_text",
]
