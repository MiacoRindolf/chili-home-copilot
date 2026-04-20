"""Durable project-domain runs and operator timeline helpers."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ..models import ProjectDomainRun

RUN_KINDS = frozenset({"index", "analysis", "suggest", "dry_run", "apply", "validate"})


def _json_text(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, default=str)


def start_run(
    db: Session,
    run_kind: str,
    *,
    user_id: int | None = None,
    task_id: int | None = None,
    repo_id: int | None = None,
    trigger_source: str = "manual",
    title: str | None = None,
    detail: dict[str, Any] | None = None,
) -> ProjectDomainRun:
    row = ProjectDomainRun(
        user_id=user_id,
        task_id=task_id,
        repo_id=repo_id,
        run_kind=run_kind,
        status="running",
        trigger_source=trigger_source,
        title=title,
        detail_json=_json_text(detail),
        started_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def finish_run(
    db: Session,
    run: ProjectDomainRun,
    *,
    status: str,
    detail: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> ProjectDomainRun:
    run.status = status
    run.finished_at = datetime.utcnow()
    if detail is not None:
        run.detail_json = _json_text(detail)
    run.error_message = error_message
    db.flush()
    return run


def record_completed_run(
    db: Session,
    run_kind: str,
    *,
    status: str,
    user_id: int | None = None,
    task_id: int | None = None,
    repo_id: int | None = None,
    trigger_source: str = "manual",
    title: str | None = None,
    detail: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> ProjectDomainRun:
    row = start_run(
        db,
        run_kind,
        user_id=user_id,
        task_id=task_id,
        repo_id=repo_id,
        trigger_source=trigger_source,
        title=title,
        detail=detail,
    )
    return finish_run(db, row, status=status, detail=detail, error_message=error_message)


def _run_payload(row: ProjectDomainRun) -> dict[str, Any]:
    try:
        detail = json.loads(row.detail_json) if row.detail_json else {}
    except Exception:
        detail = {}
    return {
        "id": row.id,
        "run_kind": row.run_kind,
        "status": row.status,
        "trigger_source": row.trigger_source,
        "title": row.title,
        "detail": detail,
        "error_message": row.error_message,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "task_id": row.task_id,
        "repo_id": row.repo_id,
    }


def latest_run(
    db: Session,
    *,
    user_id: int | None = None,
    run_kind: str | None = None,
    task_id: int | None = None,
) -> ProjectDomainRun | None:
    q = db.query(ProjectDomainRun)
    if user_id is not None:
        q = q.filter(ProjectDomainRun.user_id == user_id)
    if run_kind:
        q = q.filter(ProjectDomainRun.run_kind == run_kind)
    if task_id is not None:
        q = q.filter(ProjectDomainRun.task_id == task_id)
    return q.order_by(ProjectDomainRun.created_at.desc(), ProjectDomainRun.id.desc()).first()


def status_payload(db: Session, *, user_id: int | None = None) -> dict[str, Any]:
    q = db.query(ProjectDomainRun).filter(ProjectDomainRun.status == "running")
    if user_id is not None:
        q = q.filter(ProjectDomainRun.user_id == user_id)
    running = q.order_by(ProjectDomainRun.started_at.desc(), ProjectDomainRun.id.desc()).first()
    latest = latest_run(db, user_id=user_id)
    if running is not None:
        return {
            "running": True,
            "last_run": latest.finished_at.isoformat() if latest and latest.finished_at else None,
            "phase": running.run_kind,
            "step": running.title or running.run_kind,
            "run_kind": running.run_kind,
            "run_id": running.id,
            "progress": 0.5,
            "error": None,
        }
    return {
        "running": False,
        "last_run": latest.finished_at.isoformat() if latest and latest.finished_at else None,
        "phase": latest.run_kind if latest else "idle",
        "step": latest.title if latest else "",
        "run_kind": latest.run_kind if latest else None,
        "run_id": latest.id if latest else None,
        "progress": 0.0,
        "error": latest.error_message if latest and latest.status == "failed" else None,
    }


def kind_status_payload(db: Session, run_kind: str, *, user_id: int | None = None) -> dict[str, Any]:
    latest = latest_run(db, user_id=user_id, run_kind=run_kind)
    q = db.query(ProjectDomainRun).filter(
        ProjectDomainRun.run_kind == run_kind,
        ProjectDomainRun.status == "running",
    )
    if user_id is not None:
        q = q.filter(ProjectDomainRun.user_id == user_id)
    running = q.order_by(ProjectDomainRun.started_at.desc(), ProjectDomainRun.id.desc()).first()
    return {
        "running": running is not None,
        "last_run": latest.finished_at.isoformat() if latest and latest.finished_at else None,
        "phase": run_kind if latest else "idle",
        "step": (running or latest).title if (running or latest) is not None else "",
        "error": latest.error_message if latest and latest.status == "failed" else None,
        "latest": _run_payload(latest) if latest is not None else None,
    }


def list_timeline(
    db: Session,
    *,
    user_id: int | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    q = db.query(ProjectDomainRun)
    if user_id is not None:
        q = q.filter(ProjectDomainRun.user_id == user_id)
    rows = q.order_by(ProjectDomainRun.created_at.desc(), ProjectDomainRun.id.desc()).limit(limit).all()
    return [_run_payload(row) for row in rows]
