"""Unified operator feed for the Project domain cockpit."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import AgentMessage, CodeLearningEvent, ProjectDomainRun
from .code_brain import indexer as cb_indexer
from .code_brain.events import learning_event_visibility_clause


def _json_load(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _truncate(text: str | None, *, limit: int = 180) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 3:
        return cleaned[:limit]
    return cleaned[: limit - 3].rstrip() + "..."


def _labelize(value: str | None) -> str:
    raw = (value or "").replace("_", " ").strip()
    if not raw:
        return "Event"
    replacements = {
        "ai": "AI",
        "devops": "DevOps",
        "pm": "PM",
        "po": "PO",
        "qa": "QA",
        "ux": "UX",
    }
    words = [replacements.get(part.lower(), part.title()) for part in raw.split()]
    return " ".join(words)


def _learning_event_status(event_type: str | None) -> str:
    kind = (event_type or "").strip().lower()
    if "error" in kind or "fail" in kind:
        return "failed"
    if kind in {"cycle", "index", "analyze", "graph", "review", "deps", "search"}:
        return "completed"
    return "observed"


def _agent_message_summary(row: AgentMessage) -> str:
    content = _json_load(row.content_json)
    for key in ("title", "summary", "question", "description"):
        value = content.get(key)
        if isinstance(value, str) and value.strip():
            return _truncate(value)

    if row.message_type == "cycle_summary":
        interesting = (
            ("tasks_created", "tasks"),
            ("health", "health"),
            ("completion_pct", "completion"),
            ("files_analyzed", "files"),
            ("pattern_issues", "issues"),
            ("llm_files", "llm files"),
            ("prompt_issues", "prompt issues"),
            ("test_cases", "tests"),
            ("bugs", "bugs"),
            ("confidence", "confidence"),
        )
        metrics: list[str] = []
        for key, label in interesting:
            value = content.get(key)
            if value is None:
                continue
            if key == "completion_pct":
                metrics.append(f"{label} {value}%")
            elif key == "confidence":
                try:
                    metrics.append(f"{label} {float(value):.2f}")
                except Exception:
                    metrics.append(f"{label} {value}")
            else:
                metrics.append(f"{label} {value}")
            if len(metrics) >= 2:
                break
        prefix = f"{_labelize(row.from_agent)} cycle"
        if metrics:
            return _truncate(f"{prefix}: {', '.join(metrics)}")
        return f"{prefix} summary"

    bits: list[str] = []
    for key, value in content.items():
        if isinstance(value, (str, int, float, bool)) and value not in ("", None):
            bits.append(f"{key.replace('_', ' ')} {value}")
        if len(bits) >= 2:
            break
    prefix = f"{_labelize(row.from_agent)} {_labelize(row.message_type).lower()}"
    if bits:
        return _truncate(f"{prefix}: {', '.join(bits)}")
    return prefix


def _learning_event_summary(row: CodeLearningEvent) -> str:
    if row.repo_id is None and row.event_type == "cycle":
        return "Code learning cycle completed."
    if row.description:
        return _truncate(row.description)
    return _labelize(row.event_type)


def _run_feed_item(row: ProjectDomainRun) -> dict[str, Any]:
    detail = _json_load(row.detail_json)
    return {
        "id": row.id,
        "source": "project_run",
        "from": "system",
        "to": "operator",
        "type": row.run_kind,
        "summary": row.title or _labelize(row.run_kind),
        "status": row.status,
        "acknowledged": True,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "task_id": row.task_id,
        "repo_id": row.repo_id,
        "detail": detail,
        "_created_at": row.created_at or datetime.min,
        "_sort_id": int(row.id or 0),
        "_source_weight": 1,
    }


def _learning_feed_item(row: CodeLearningEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "source": "code_learning",
        "from": "code_brain",
        "to": "operator",
        "type": row.event_type,
        "summary": _learning_event_summary(row),
        "status": _learning_event_status(row.event_type),
        "acknowledged": True,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "task_id": None,
        "repo_id": row.repo_id,
        "detail": None,
        "_created_at": row.created_at or datetime.min,
        "_sort_id": int(row.id or 0),
        "_source_weight": 2,
    }


def _message_feed_item(row: AgentMessage) -> dict[str, Any]:
    return {
        "id": row.id,
        "source": "agent_message",
        "from": row.from_agent,
        "to": row.to_agent,
        "type": row.message_type,
        "summary": _agent_message_summary(row),
        "status": "unread" if not row.acknowledged else "read",
        "acknowledged": bool(row.acknowledged),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "task_id": None,
        "repo_id": None,
        "detail": _json_load(row.content_json),
        "_created_at": row.created_at or datetime.min,
        "_sort_id": int(row.id or 0),
        "_source_weight": 3,
    }


def count_unread_operator_messages(db: Session, *, user_id: int | None) -> int:
    if user_id is None:
        return 0
    return (
        db.query(func.count(AgentMessage.id))
        .filter(
            AgentMessage.user_id == user_id,
            AgentMessage.acknowledged.is_(False),
        )
        .scalar()
        or 0
    )


def list_operator_feed(
    db: Session,
    *,
    user_id: int | None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    if user_id is None:
        return []

    accessible_repo_ids = cb_indexer.get_accessible_repo_ids(
        db,
        user_id=user_id,
        include_shared=True,
    )

    run_rows = (
        db.query(ProjectDomainRun)
        .filter(ProjectDomainRun.user_id == user_id)
        .order_by(ProjectDomainRun.created_at.desc(), ProjectDomainRun.id.desc())
        .limit(limit)
        .all()
    )

    learning_filter = learning_event_visibility_clause(
        user_id=user_id,
        repo_ids=accessible_repo_ids,
    )
    learning_rows = (
        db.query(CodeLearningEvent)
        .filter(learning_filter)
        .order_by(CodeLearningEvent.created_at.desc(), CodeLearningEvent.id.desc())
        .limit(limit)
        .all()
    )

    message_rows = (
        db.query(AgentMessage)
        .filter(AgentMessage.user_id == user_id)
        .order_by(AgentMessage.created_at.desc(), AgentMessage.id.desc())
        .limit(limit)
        .all()
    )

    items = (
        [_run_feed_item(row) for row in run_rows]
        + [_learning_feed_item(row) for row in learning_rows]
        + [_message_feed_item(row) for row in message_rows]
    )
    items.sort(
        key=lambda item: (item["_created_at"], item["_source_weight"], item["_sort_id"]),
        reverse=True,
    )

    merged: list[dict[str, Any]] = []
    for item in items[:limit]:
        merged.append({key: value for key, value in item.items() if not key.startswith("_")})
    return merged
