"""Service layer for collaborative project planning & task management."""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from ..models import (
    PlanProject, PlanTask, ProjectMember, TaskComment,
    TaskActivity, PlanLabel, TaskLabel, TaskWatcher, User,
)
from . import home_service


# ── Access helpers ───────────────────────────────────────────────────────────

def _user_membership(db: Session, project_id: int, user_id: int) -> ProjectMember | None:
    return db.query(ProjectMember).filter(
        ProjectMember.project_id == project_id,
        ProjectMember.user_id == user_id,
    ).first()


def _user_can_access(db: Session, project_id: int, user_id: int) -> bool:
    return _user_membership(db, project_id, user_id) is not None


def _user_can_edit(db: Session, project_id: int, user_id: int) -> bool:
    m = _user_membership(db, project_id, user_id)
    return m is not None and m.role in ("owner", "editor")


def _user_is_owner(db: Session, project_id: int, user_id: int) -> bool:
    m = _user_membership(db, project_id, user_id)
    return m is not None and m.role == "owner"


# ── Dict serializers ────────────────────────────────────────────────────────

def _member_dict(m: ProjectMember) -> dict:
    return {
        "id": m.id,
        "user_id": m.user_id,
        "user_name": m.user.name if m.user else "",
        "role": m.role,
        "joined_at": m.joined_at.isoformat() if m.joined_at else None,
    }


def _label_dict(lb: PlanLabel) -> dict:
    return {
        "id": lb.id,
        "project_id": lb.project_id,
        "name": lb.name,
        "color": lb.color,
    }


def _comment_dict(c: TaskComment) -> dict:
    return {
        "id": c.id,
        "task_id": c.task_id,
        "user_id": c.user_id,
        "user_name": c.user.name if c.user else "",
        "content": c.content,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _activity_dict(a: TaskActivity) -> dict:
    return {
        "id": a.id,
        "task_id": a.task_id,
        "user_id": a.user_id,
        "user_name": a.user.name if a.user else "",
        "action": a.action,
        "detail": a.detail,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _project_dict(p: PlanProject, include_tasks: bool = False) -> dict:
    d = {
        "id": p.id,
        "user_id": p.user_id,
        "key": p.key or "",
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
        "members": [_member_dict(m) for m in (p.members or [])],
        "labels": [_label_dict(lb) for lb in (p.labels or [])],
    }
    if include_tasks:
        top_tasks = [t for t in p.tasks if not t.parent_id]
        d["tasks"] = [_task_dict(t) for t in sorted(top_tasks, key=lambda t: t.sort_order)]
    return d


def _task_dict(t: PlanTask) -> dict:
    labels = [_label_dict(tl.label) for tl in (t.task_labels or []) if tl.label]
    watcher_ids = [w.user_id for w in (t.watchers or [])]
    subtask_list = sorted((t.subtasks or []), key=lambda s: s.sort_order)
    return {
        "id": t.id,
        "project_id": t.project_id,
        "parent_id": t.parent_id,
        "title": t.title,
        "description": t.description or "",
        "status": t.status or "todo",
        "priority": t.priority or "medium",
        "start_date": t.start_date.isoformat() if t.start_date else None,
        "end_date": t.end_date.isoformat() if t.end_date else None,
        "assigned_to": t.assigned_to,
        "assignee_name": t.assignee.name if t.assigned_to and t.assignee else "",
        "reporter_id": t.reporter_id,
        "reporter_name": t.reporter.name if t.reporter_id and t.reporter else "",
        "depends_on": t.depends_on,
        "progress": t.progress or 0,
        "sort_order": t.sort_order or 0,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "labels": labels,
        "watcher_ids": watcher_ids,
        "subtask_count": len(subtask_list),
        "subtasks": [_task_dict(s) for s in subtask_list],
    }


# ── Activity logging ────────────────────────────────────────────────────────

def _log_activity(db: Session, task_id: int, user_id: int | None, action: str, detail: str = ""):
    db.add(TaskActivity(task_id=task_id, user_id=user_id, action=action, detail=detail))
    db.flush()


# ── Project CRUD ────────────────────────────────────────────────────────────

def list_projects(db: Session, user_id: int) -> list[dict]:
    projects = (
        db.query(PlanProject)
        .join(ProjectMember, ProjectMember.project_id == PlanProject.id)
        .filter(ProjectMember.user_id == user_id)
        .order_by(PlanProject.updated_at.desc())
        .all()
    )
    return [_project_dict(p) for p in projects]


def get_project(db: Session, project_id: int, user_id: int) -> dict | None:
    if not _user_can_access(db, project_id, user_id):
        return None
    p = db.query(PlanProject).filter(PlanProject.id == project_id).first()
    if not p:
        return None
    return _project_dict(p, include_tasks=True)


def _generate_project_key(name: str) -> str:
    words = name.strip().split()
    if len(words) >= 2:
        return "".join(w[0] for w in words[:3]).upper()
    return name[:3].upper()


def create_project(
    db: Session, user_id: int, name: str,
    description: str = "", color: str = "#6366f1",
    start_date: str | None = None, end_date: str | None = None,
    key: str | None = None,
) -> dict:
    proj_key = (key or _generate_project_key(name)).upper()[:6]
    p = PlanProject(
        user_id=user_id,
        key=proj_key,
        name=name.strip(),
        description=description.strip() if description else "",
        color=color,
        start_date=date.fromisoformat(start_date) if start_date else None,
        end_date=date.fromisoformat(end_date) if end_date else None,
    )
    db.add(p)
    db.flush()

    db.add(ProjectMember(project_id=p.id, user_id=user_id, role="owner"))
    db.commit()
    db.refresh(p)

    user = db.query(User).filter(User.id == user_id).first()
    home_service.log_activity(
        db, "project_created", f'Created project "{p.name}"',
        user_id=user_id, user_name=user.name if user else "", icon="project",
    )

    return _project_dict(p)


def update_project(db: Session, project_id: int, user_id: int, **kwargs) -> dict | None:
    if not _user_is_owner(db, project_id, user_id):
        return None
    p = db.query(PlanProject).filter(PlanProject.id == project_id).first()
    if not p:
        return None

    for key_name in ("name", "description", "status", "color", "key"):
        if key_name in kwargs and kwargs[key_name] is not None:
            val = kwargs[key_name]
            setattr(p, key_name, val.strip() if isinstance(val, str) else val)

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
    if not _user_is_owner(db, project_id, user_id):
        return False
    p = db.query(PlanProject).filter(PlanProject.id == project_id).first()
    if not p:
        return False
    db.delete(p)
    db.commit()
    return True


# ── Member management ───────────────────────────────────────────────────────

def list_members(db: Session, project_id: int, user_id: int) -> list[dict] | None:
    if not _user_can_access(db, project_id, user_id):
        return None
    members = db.query(ProjectMember).filter(ProjectMember.project_id == project_id).all()
    return [_member_dict(m) for m in members]


def add_member(
    db: Session, project_id: int, user_id: int,
    target_user_id: int, role: str = "editor",
) -> dict | None:
    if not _user_is_owner(db, project_id, user_id):
        return None
    if role not in ("editor", "viewer"):
        role = "editor"
    existing = _user_membership(db, project_id, target_user_id)
    if existing:
        return _member_dict(existing)
    target_user = db.query(User).filter(User.id == target_user_id).first()
    if not target_user:
        return None
    m = ProjectMember(project_id=project_id, user_id=target_user_id, role=role)
    db.add(m)
    db.commit()
    db.refresh(m)

    _log_activity_project(db, project_id, user_id, "member_added",
                          f"Added {target_user.name} as {role}")
    return _member_dict(m)


def remove_member(db: Session, project_id: int, user_id: int, target_user_id: int) -> bool:
    if not _user_is_owner(db, project_id, user_id):
        return False
    m = _user_membership(db, project_id, target_user_id)
    if not m or m.role == "owner":
        return False
    db.delete(m)
    db.commit()
    return True


def update_member_role(
    db: Session, project_id: int, user_id: int,
    target_user_id: int, role: str,
) -> dict | None:
    if not _user_is_owner(db, project_id, user_id):
        return None
    if role not in ("editor", "viewer"):
        return None
    m = _user_membership(db, project_id, target_user_id)
    if not m or m.role == "owner":
        return None
    m.role = role
    db.commit()
    db.refresh(m)
    return _member_dict(m)


def _log_activity_project(db: Session, project_id: int, user_id: int, action: str, detail: str):
    """Log an activity on the first task of a project (or skip if no tasks)."""
    first_task = db.query(PlanTask).filter(PlanTask.project_id == project_id).first()
    if first_task:
        _log_activity(db, first_task.id, user_id, action, detail)
    db.commit()


# ── Task CRUD ───────────────────────────────────────────────────────────────

def list_tasks(db: Session, project_id: int, user_id: int) -> list[dict]:
    if not _user_can_access(db, project_id, user_id):
        return []
    tasks = (
        db.query(PlanTask)
        .filter(PlanTask.project_id == project_id, PlanTask.parent_id.is_(None))
        .order_by(PlanTask.sort_order, PlanTask.id)
        .all()
    )
    return [_task_dict(t) for t in tasks]


def list_tasks_filtered(
    db: Session, project_id: int, user_id: int,
    assignee: int | None = None, status: str | None = None,
    priority: str | None = None, label_id: int | None = None,
    search: str | None = None,
) -> list[dict]:
    if not _user_can_access(db, project_id, user_id):
        return []
    q = db.query(PlanTask).filter(PlanTask.project_id == project_id)
    if assignee is not None:
        q = q.filter(PlanTask.assigned_to == assignee)
    if status:
        q = q.filter(PlanTask.status == status)
    if priority:
        q = q.filter(PlanTask.priority == priority)
    if label_id is not None:
        q = q.join(TaskLabel, TaskLabel.task_id == PlanTask.id).filter(TaskLabel.label_id == label_id)
    if search:
        term = f"%{search}%"
        q = q.filter(or_(PlanTask.title.ilike(term), PlanTask.description.ilike(term)))
    return [_task_dict(t) for t in q.order_by(PlanTask.sort_order, PlanTask.id).all()]


def create_task(
    db: Session, project_id: int, user_id: int, title: str,
    description: str = "", priority: str = "medium",
    start_date: str | None = None, end_date: str | None = None,
    assigned_to: int | None = None, depends_on: int | None = None,
    parent_id: int | None = None,
) -> dict | None:
    if not _user_can_edit(db, project_id, user_id):
        return None

    max_order = db.query(func.max(PlanTask.sort_order)).filter(
        PlanTask.project_id == project_id,
    ).scalar() or 0

    t = PlanTask(
        project_id=project_id,
        parent_id=parent_id,
        title=title.strip(),
        description=description.strip() if description else "",
        priority=priority if priority in ("low", "medium", "high", "critical") else "medium",
        start_date=date.fromisoformat(start_date) if start_date else None,
        end_date=date.fromisoformat(end_date) if end_date else None,
        assigned_to=assigned_to,
        reporter_id=user_id,
        depends_on=depends_on,
        sort_order=max_order + 1,
    )
    db.add(t)
    db.flush()

    _log_activity(db, t.id, user_id, "created", f'Created task "{t.title}"')

    # Auto-watch reporter
    db.add(TaskWatcher(task_id=t.id, user_id=user_id))
    if assigned_to and assigned_to != user_id:
        db.add(TaskWatcher(task_id=t.id, user_id=assigned_to))

    p = db.query(PlanProject).filter(PlanProject.id == project_id).first()
    p.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(t)

    user = db.query(User).filter(User.id == user_id).first()
    home_service.log_activity(
        db, "task_added", f'Added task "{t.title}" to {p.name}',
        user_id=user_id, user_name=user.name if user else "", icon="task",
    )

    return _task_dict(t)


def update_task(db: Session, task_id: int, user_id: int, **kwargs) -> dict | None:
    t = db.query(PlanTask).filter(PlanTask.id == task_id).first()
    if not t:
        return None
    if not _user_can_edit(db, t.project_id, user_id):
        return None

    changes = []
    for key in ("title", "description", "status", "priority"):
        if key in kwargs and kwargs[key] is not None:
            old_val = getattr(t, key)
            new_val = kwargs[key].strip() if isinstance(kwargs[key], str) else kwargs[key]
            if old_val != new_val:
                changes.append(f"{key}: {old_val} → {new_val}")
            setattr(t, key, new_val)

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

    for int_field in ("assigned_to", "depends_on", "progress", "sort_order", "parent_id"):
        if int_field in kwargs and kwargs[int_field] is not None:
            old_val = getattr(t, int_field)
            new_val = kwargs[int_field]
            if old_val != new_val:
                changes.append(f"{int_field}: {old_val} → {new_val}")
            setattr(t, int_field, new_val)

    if changes:
        _log_activity(db, task_id, user_id, "updated", "; ".join(changes))

    if "assigned_to" in kwargs and kwargs["assigned_to"]:
        new_assignee = kwargs["assigned_to"]
        existing_watcher = db.query(TaskWatcher).filter(
            TaskWatcher.task_id == task_id, TaskWatcher.user_id == new_assignee
        ).first()
        if not existing_watcher:
            db.add(TaskWatcher(task_id=task_id, user_id=new_assignee))

    t.updated_at = datetime.utcnow()
    t.project.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(t)
    return _task_dict(t)


def delete_task(db: Session, task_id: int, user_id: int) -> bool:
    t = db.query(PlanTask).filter(PlanTask.id == task_id).first()
    if not t:
        return False
    if not _user_can_edit(db, t.project_id, user_id):
        return False
    t.project.updated_at = datetime.utcnow()
    db.delete(t)
    db.commit()
    return True


def get_task(db: Session, task_id: int, user_id: int) -> dict | None:
    t = db.query(PlanTask).filter(PlanTask.id == task_id).first()
    if not t:
        return None
    if not _user_can_access(db, t.project_id, user_id):
        return None
    return _task_dict(t)


# ── Comments ────────────────────────────────────────────────────────────────

def add_comment(db: Session, task_id: int, user_id: int, content: str) -> dict | None:
    t = db.query(PlanTask).filter(PlanTask.id == task_id).first()
    if not t or not _user_can_access(db, t.project_id, user_id):
        return None
    c = TaskComment(task_id=task_id, user_id=user_id, content=content.strip())
    db.add(c)
    db.flush()
    _log_activity(db, task_id, user_id, "comment_added", content[:120])

    existing_watcher = db.query(TaskWatcher).filter(
        TaskWatcher.task_id == task_id, TaskWatcher.user_id == user_id
    ).first()
    if not existing_watcher:
        db.add(TaskWatcher(task_id=task_id, user_id=user_id))

    db.commit()
    db.refresh(c)
    return _comment_dict(c)


def list_comments(db: Session, task_id: int, user_id: int) -> list[dict] | None:
    t = db.query(PlanTask).filter(PlanTask.id == task_id).first()
    if not t or not _user_can_access(db, t.project_id, user_id):
        return None
    comments = (
        db.query(TaskComment)
        .filter(TaskComment.task_id == task_id)
        .order_by(TaskComment.created_at.asc())
        .all()
    )
    return [_comment_dict(c) for c in comments]


def update_comment(db: Session, comment_id: int, user_id: int, content: str) -> dict | None:
    c = db.query(TaskComment).filter(TaskComment.id == comment_id, TaskComment.user_id == user_id).first()
    if not c:
        return None
    c.content = content.strip()
    c.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(c)
    return _comment_dict(c)


def delete_comment(db: Session, comment_id: int, user_id: int) -> bool:
    c = db.query(TaskComment).filter(TaskComment.id == comment_id, TaskComment.user_id == user_id).first()
    if not c:
        return False
    _log_activity(db, c.task_id, user_id, "comment_deleted", "")
    db.delete(c)
    db.commit()
    return True


# ── Activity feed ───────────────────────────────────────────────────────────

def get_task_activity(db: Session, task_id: int, user_id: int) -> list[dict] | None:
    t = db.query(PlanTask).filter(PlanTask.id == task_id).first()
    if not t or not _user_can_access(db, t.project_id, user_id):
        return None
    activities = (
        db.query(TaskActivity)
        .filter(TaskActivity.task_id == task_id)
        .order_by(TaskActivity.created_at.desc())
        .limit(50)
        .all()
    )
    return [_activity_dict(a) for a in activities]


# ── Labels ──────────────────────────────────────────────────────────────────

def create_label(db: Session, project_id: int, user_id: int, name: str, color: str = "#6366f1") -> dict | None:
    if not _user_can_edit(db, project_id, user_id):
        return None
    lb = PlanLabel(project_id=project_id, name=name.strip(), color=color)
    db.add(lb)
    db.commit()
    db.refresh(lb)
    return _label_dict(lb)


def list_labels(db: Session, project_id: int, user_id: int) -> list[dict] | None:
    if not _user_can_access(db, project_id, user_id):
        return None
    labels = db.query(PlanLabel).filter(PlanLabel.project_id == project_id).all()
    return [_label_dict(lb) for lb in labels]


def delete_label(db: Session, label_id: int, user_id: int) -> bool:
    lb = db.query(PlanLabel).filter(PlanLabel.id == label_id).first()
    if not lb or not _user_can_edit(db, lb.project_id, user_id):
        return False
    db.query(TaskLabel).filter(TaskLabel.label_id == label_id).delete()
    db.delete(lb)
    db.commit()
    return True


def add_task_label(db: Session, task_id: int, label_id: int, user_id: int) -> bool:
    t = db.query(PlanTask).filter(PlanTask.id == task_id).first()
    if not t or not _user_can_edit(db, t.project_id, user_id):
        return False
    existing = db.query(TaskLabel).filter(TaskLabel.task_id == task_id, TaskLabel.label_id == label_id).first()
    if existing:
        return True
    db.add(TaskLabel(task_id=task_id, label_id=label_id))
    lb = db.query(PlanLabel).filter(PlanLabel.id == label_id).first()
    _log_activity(db, task_id, user_id, "label_added", lb.name if lb else "")
    db.commit()
    return True


def remove_task_label(db: Session, task_id: int, label_id: int, user_id: int) -> bool:
    t = db.query(PlanTask).filter(PlanTask.id == task_id).first()
    if not t or not _user_can_edit(db, t.project_id, user_id):
        return False
    tl = db.query(TaskLabel).filter(TaskLabel.task_id == task_id, TaskLabel.label_id == label_id).first()
    if not tl:
        return False
    lb = db.query(PlanLabel).filter(PlanLabel.id == label_id).first()
    _log_activity(db, task_id, user_id, "label_removed", lb.name if lb else "")
    db.delete(tl)
    db.commit()
    return True


# ── Watchers ────────────────────────────────────────────────────────────────

def watch_task(db: Session, task_id: int, user_id: int) -> bool:
    t = db.query(PlanTask).filter(PlanTask.id == task_id).first()
    if not t or not _user_can_access(db, t.project_id, user_id):
        return False
    existing = db.query(TaskWatcher).filter(
        TaskWatcher.task_id == task_id, TaskWatcher.user_id == user_id
    ).first()
    if existing:
        return True
    db.add(TaskWatcher(task_id=task_id, user_id=user_id))
    db.commit()
    return True


def unwatch_task(db: Session, task_id: int, user_id: int) -> bool:
    w = db.query(TaskWatcher).filter(
        TaskWatcher.task_id == task_id, TaskWatcher.user_id == user_id
    ).first()
    if not w:
        return False
    db.delete(w)
    db.commit()
    return True


def list_watchers(db: Session, task_id: int, user_id: int) -> list[dict] | None:
    t = db.query(PlanTask).filter(PlanTask.id == task_id).first()
    if not t or not _user_can_access(db, t.project_id, user_id):
        return None
    watchers = db.query(TaskWatcher).filter(TaskWatcher.task_id == task_id).all()
    return [
        {"user_id": w.user_id, "user_name": w.user.name if w.user else ""}
        for w in watchers
    ]


# ── Stats for dashboard / AI context ───────────────────────────────────────

def get_user_project_summary(db: Session, user_id: int) -> str:
    projects = (
        db.query(PlanProject)
        .join(ProjectMember, ProjectMember.project_id == PlanProject.id)
        .filter(ProjectMember.user_id == user_id, PlanProject.status == "active")
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

        lines.append(f"Project: {p.name} ({p.key or 'ID ' + str(p.id)}) — {done}/{total} tasks done, {in_prog} in progress, {blocked} blocked")
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
    users = db.query(User).all()
    result = []
    for u in users:
        member_projects = (
            db.query(PlanProject)
            .join(ProjectMember, ProjectMember.project_id == PlanProject.id)
            .filter(ProjectMember.user_id == u.id)
            .all()
        )
        total_tasks = 0
        done_tasks = 0
        overdue_tasks = 0
        today = date.today()
        for p in member_projects:
            for t in p.tasks:
                total_tasks += 1
                if t.status == "done":
                    done_tasks += 1
                elif t.end_date and t.end_date < today and t.status != "done":
                    overdue_tasks += 1
        result.append({
            "user_id": u.id,
            "user_name": u.name,
            "project_count": len(member_projects),
            "total_tasks": total_tasks,
            "done_tasks": done_tasks,
            "overdue_tasks": overdue_tasks,
        })
    return result
