"""Service layer for project planning & task management."""
from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from ..models import PlanProject, PlanTask, User
from . import home_service


def _project_dict(p: PlanProject, include_tasks: bool = False) -> dict:
    d = {
        "id": p.id,
        "user_id": p.user_id,
        "name": p.name,
        "description": p.description or "",
        "status": p.status or "active",
        "color": p.color or "#6366f1",
        "start_date": p.start_date.isoformat() if p.start_date else None,
        "end_date": p.end_date.isoformat() if p.end_date else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        "task_count": len(p.tasks) if p.tasks else 0,
        "done_count": sum(1 for t in p.tasks if t.status == "done") if p.tasks else 0,
    }
    if include_tasks:
        d["tasks"] = [_task_dict(t) for t in sorted(p.tasks, key=lambda t: t.sort_order)]
    return d


def _task_dict(t: PlanTask) -> dict:
    return {
        "id": t.id,
        "project_id": t.project_id,
        "title": t.title,
        "description": t.description or "",
        "status": t.status or "todo",
        "priority": t.priority or "medium",
        "start_date": t.start_date.isoformat() if t.start_date else None,
        "end_date": t.end_date.isoformat() if t.end_date else None,
        "assigned_to": t.assigned_to,
        "assignee_name": t.assignee.name if t.assigned_to and t.assignee else "",
        "depends_on": t.depends_on,
        "progress": t.progress or 0,
        "sort_order": t.sort_order or 0,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


# ── Project CRUD ──────────────────────────────────────────────────────────────

def list_projects(db: Session, user_id: int) -> list[dict]:
    projects = (
        db.query(PlanProject)
        .filter(PlanProject.user_id == user_id)
        .order_by(PlanProject.updated_at.desc())
        .all()
    )
    return [_project_dict(p) for p in projects]


def get_project(db: Session, project_id: int, user_id: int) -> dict | None:
    p = db.query(PlanProject).filter(
        PlanProject.id == project_id,
        PlanProject.user_id == user_id,
    ).first()
    if not p:
        return None
    return _project_dict(p, include_tasks=True)


def create_project(
    db: Session, user_id: int, name: str,
    description: str = "", color: str = "#6366f1",
    start_date: str | None = None, end_date: str | None = None,
) -> dict:
    p = PlanProject(
        user_id=user_id,
        name=name.strip(),
        description=description.strip() if description else "",
        color=color,
        start_date=date.fromisoformat(start_date) if start_date else None,
        end_date=date.fromisoformat(end_date) if end_date else None,
    )
    db.add(p)
    db.commit()
    db.refresh(p)

    user = db.query(User).filter(User.id == user_id).first()
    home_service.log_activity(
        db, "project_created", f'Created project "{p.name}"',
        user_id=user_id, user_name=user.name if user else "", icon="project",
    )

    return _project_dict(p)


def update_project(db: Session, project_id: int, user_id: int, **kwargs) -> dict | None:
    p = db.query(PlanProject).filter(
        PlanProject.id == project_id,
        PlanProject.user_id == user_id,
    ).first()
    if not p:
        return None

    for key in ("name", "description", "status", "color"):
        if key in kwargs and kwargs[key] is not None:
            setattr(p, key, kwargs[key].strip() if isinstance(kwargs[key], str) else kwargs[key])

    for date_field in ("start_date", "end_date"):
        if date_field in kwargs:
            val = kwargs[date_field]
            if val:
                try:
                    setattr(p, date_field, date.fromisoformat(val))
                except ValueError:
                    pass
            else:
                setattr(p, date_field, None)

    p.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(p)
    return _project_dict(p, include_tasks=True)


def delete_project(db: Session, project_id: int, user_id: int) -> bool:
    p = db.query(PlanProject).filter(
        PlanProject.id == project_id,
        PlanProject.user_id == user_id,
    ).first()
    if not p:
        return False
    db.delete(p)
    db.commit()
    return True


# ── Task CRUD ─────────────────────────────────────────────────────────────────

def list_tasks(db: Session, project_id: int, user_id: int) -> list[dict]:
    p = db.query(PlanProject).filter(
        PlanProject.id == project_id,
        PlanProject.user_id == user_id,
    ).first()
    if not p:
        return []
    tasks = (
        db.query(PlanTask)
        .filter(PlanTask.project_id == project_id)
        .order_by(PlanTask.sort_order, PlanTask.id)
        .all()
    )
    return [_task_dict(t) for t in tasks]


def create_task(
    db: Session, project_id: int, user_id: int, title: str,
    description: str = "", priority: str = "medium",
    start_date: str | None = None, end_date: str | None = None,
    assigned_to: int | None = None, depends_on: int | None = None,
) -> dict | None:
    p = db.query(PlanProject).filter(
        PlanProject.id == project_id,
        PlanProject.user_id == user_id,
    ).first()
    if not p:
        return None

    max_order = db.query(func.max(PlanTask.sort_order)).filter(
        PlanTask.project_id == project_id,
    ).scalar() or 0

    t = PlanTask(
        project_id=project_id,
        title=title.strip(),
        description=description.strip() if description else "",
        priority=priority if priority in ("low", "medium", "high", "critical") else "medium",
        start_date=date.fromisoformat(start_date) if start_date else None,
        end_date=date.fromisoformat(end_date) if end_date else None,
        assigned_to=assigned_to,
        depends_on=depends_on,
        sort_order=max_order + 1,
    )
    db.add(t)
    db.commit()
    db.refresh(t)

    p.updated_at = datetime.utcnow()
    db.commit()

    user = db.query(User).filter(User.id == user_id).first()
    home_service.log_activity(
        db, "task_added", f'Added task "{t.title}" to {p.name}',
        user_id=user_id, user_name=user.name if user else "", icon="task",
    )

    return _task_dict(t)


def update_task(db: Session, task_id: int, user_id: int, **kwargs) -> dict | None:
    t = db.query(PlanTask).join(PlanProject).filter(
        PlanTask.id == task_id,
        PlanProject.user_id == user_id,
    ).first()
    if not t:
        return None

    for key in ("title", "description", "status", "priority"):
        if key in kwargs and kwargs[key] is not None:
            setattr(t, key, kwargs[key].strip() if isinstance(kwargs[key], str) else kwargs[key])

    for date_field in ("start_date", "end_date"):
        if date_field in kwargs:
            val = kwargs[date_field]
            if val:
                try:
                    setattr(t, date_field, date.fromisoformat(val))
                except ValueError:
                    pass
            else:
                setattr(t, date_field, None)

    for int_field in ("assigned_to", "depends_on", "progress", "sort_order"):
        if int_field in kwargs and kwargs[int_field] is not None:
            setattr(t, int_field, kwargs[int_field])

    t.updated_at = datetime.utcnow()
    t.project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(t)
    return _task_dict(t)


def delete_task(db: Session, task_id: int, user_id: int) -> bool:
    t = db.query(PlanTask).join(PlanProject).filter(
        PlanTask.id == task_id,
        PlanProject.user_id == user_id,
    ).first()
    if not t:
        return False
    t.project.updated_at = datetime.utcnow()
    db.delete(t)
    db.commit()
    return True


# ── Stats for dashboard / AI context ─────────────────────────────────────────

def get_user_project_summary(db: Session, user_id: int) -> str:
    """Build a text summary of all active projects/tasks for the AI planner context."""
    projects = (
        db.query(PlanProject)
        .filter(PlanProject.user_id == user_id, PlanProject.status == "active")
        .all()
    )
    if not projects:
        return ""

    lines = []
    for p in projects:
        tasks = sorted(p.tasks, key=lambda t: t.sort_order)
        total = len(tasks)
        done = sum(1 for t in tasks if t.status == "done")
        in_prog = sum(1 for t in tasks if t.status == "in_progress")
        blocked = sum(1 for t in tasks if t.status == "blocked")

        lines.append(f"Project: {p.name} (ID {p.id}) — {done}/{total} tasks done, {in_prog} in progress, {blocked} blocked")
        if p.end_date:
            days_left = (p.end_date - date.today()).days
            lines.append(f"  Deadline: {p.end_date.isoformat()} ({days_left} days {'remaining' if days_left >= 0 else 'overdue'})")

        for t in tasks:
            if t.status != "done":
                tag = f"[{t.status}]"
                if t.priority in ("high", "critical"):
                    tag += f" [{t.priority}]"
                due = f" due {t.end_date.isoformat()}" if t.end_date else ""
                assignee = f" @{t.assignee.name}" if t.assigned_to and t.assignee else ""
                lines.append(f"  - {tag} {t.title}{due}{assignee}")

    return "\n".join(lines)


def get_all_users_task_summary(db: Session) -> list[dict]:
    """Per-user summary for the planner page overview."""
    users = db.query(User).all()
    result = []
    for u in users:
        projects = db.query(PlanProject).filter(PlanProject.user_id == u.id).all()
        total_tasks = 0
        done_tasks = 0
        overdue_tasks = 0
        today = date.today()
        for p in projects:
            for t in p.tasks:
                total_tasks += 1
                if t.status == "done":
                    done_tasks += 1
                elif t.end_date and t.end_date < today and t.status != "done":
                    overdue_tasks += 1
        result.append({
            "user_id": u.id,
            "user_name": u.name,
            "project_count": len(projects),
            "total_tasks": total_tasks,
            "done_tasks": done_tasks,
            "overdue_tasks": overdue_tasks,
        })
    return result
