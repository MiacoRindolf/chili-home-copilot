"""Project planner routes: page + JSON API for collaborative projects & tasks."""
from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session
from typing import Optional
import json as json_mod

from ..deps import get_db
from ..pairing import DEVICE_COOKIE_NAME, get_identity_record
from ..schemas import ProjectBody, MemberBody, RoleBody, TaskBody, CommentBody, LabelBody
from ..services import planner_service
from ..models import User
from . import planner_coding

router = APIRouter()
router.include_router(planner_coding.router)


def _require_user(request: Request, db: Session) -> dict | None:
    identity = get_identity_record(db, request.cookies.get(DEVICE_COOKIE_NAME))
    if identity["is_guest"] or not identity.get("user_id"):
        return None
    return identity


# ── Page ────────────────────────────────────────────────────────────────────

@router.get("/planner", response_class=HTMLResponse)
def planner_page(request: Request, db: Session = Depends(get_db)):
    identity = get_identity_record(db, request.cookies.get(DEVICE_COOKIE_NAME))
    if identity["is_guest"] or not identity.get("user_id"):
        return HTMLResponse(
            "<html><body style='font-family:Arial;max-width:800px;margin:40px auto;'>"
            "<h1>Project Planner</h1><p>You need to be a paired housemate to use the planner.</p>"
            "<p><a href='/pair'>Pair your device</a> | <a href='/'>Home</a></p>"
            "</body></html>"
        )

    user_id = identity["user_id"]
    projects = planner_service.list_projects(db, user_id)
    users = [{"id": u.id, "name": u.name} for u in db.query(User).all()]
    user_summaries = planner_service.get_all_users_task_summary(db)

    return request.app.state.templates.TemplateResponse(request, "planner.html", {
        "user_name": identity["user_name"],
        "user_id": user_id,
        "is_guest": False,
        "initial_data": json_mod.dumps({
            "projects": projects,
            "users": users,
            "user_summaries": user_summaries,
        }),
    })


# ── Project API ─────────────────────────────────────────────────────────────

@router.get("/api/planner/projects", response_class=JSONResponse)
def api_list_projects(request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    return {"projects": planner_service.list_projects(db, identity["user_id"])}


@router.post("/api/planner/projects", response_class=JSONResponse)
def api_create_project(body: ProjectBody, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    if not body.name.strip():
        return JSONResponse({"error": "Name is required"}, status_code=400)
    p = planner_service.create_project(
        db, identity["user_id"], body.name,
        description=body.description or "",
        color=body.color or "#6366f1",
        start_date=body.start_date,
        end_date=body.end_date,
        key=body.key,
    )
    return {"ok": True, "project": p}


@router.get("/api/planner/projects/{project_id}", response_class=JSONResponse)
def api_get_project(project_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    p = planner_service.get_project(db, project_id, identity["user_id"])
    if not p:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"project": p}


@router.put("/api/planner/projects/{project_id}", response_class=JSONResponse)
def api_update_project(project_id: int, body: ProjectBody, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    p = planner_service.update_project(
        db, project_id, identity["user_id"],
        name=body.name, description=body.description,
        color=body.color, status=body.status,
        start_date=body.start_date, end_date=body.end_date,
        key=body.key,
    )
    if not p:
        return JSONResponse({"error": "Not found or not owner"}, status_code=404)
    return {"ok": True, "project": p}


@router.delete("/api/planner/projects/{project_id}", response_class=JSONResponse)
def api_delete_project(project_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    ok = planner_service.delete_project(db, project_id, identity["user_id"])
    if not ok:
        return JSONResponse({"error": "Not found or not owner"}, status_code=404)
    return {"ok": True}


# ── Members API ─────────────────────────────────────────────────────────────

@router.get("/api/planner/projects/{project_id}/members", response_class=JSONResponse)
def api_list_members(project_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    members = planner_service.list_members(db, project_id, identity["user_id"])
    if members is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"members": members}


@router.post("/api/planner/projects/{project_id}/members", response_class=JSONResponse)
def api_add_member(project_id: int, body: MemberBody, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    m = planner_service.add_member(db, project_id, identity["user_id"], body.user_id, body.role or "editor")
    if not m:
        return JSONResponse({"error": "Cannot add member (not owner or user not found)"}, status_code=403)
    return {"ok": True, "member": m}


@router.delete("/api/planner/projects/{project_id}/members/{target_user_id}", response_class=JSONResponse)
def api_remove_member(project_id: int, target_user_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    ok = planner_service.remove_member(db, project_id, identity["user_id"], target_user_id)
    if not ok:
        return JSONResponse({"error": "Cannot remove"}, status_code=403)
    return {"ok": True}


@router.put("/api/planner/projects/{project_id}/members/{target_user_id}", response_class=JSONResponse)
def api_update_member_role(project_id: int, target_user_id: int, body: RoleBody, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    m = planner_service.update_member_role(db, project_id, identity["user_id"], target_user_id, body.role)
    if not m:
        return JSONResponse({"error": "Cannot update"}, status_code=403)
    return {"ok": True, "member": m}


# ── Task API ────────────────────────────────────────────────────────────────

@router.get("/api/planner/projects/{project_id}/tasks", response_class=JSONResponse)
def api_list_tasks(
    project_id: int, request: Request, db: Session = Depends(get_db),
    assignee: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    label: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    has_filter = any(v is not None for v in [assignee, status, priority, label, search])
    if has_filter:
        tasks = planner_service.list_tasks_filtered(
            db, project_id, identity["user_id"],
            assignee=assignee, status=status, priority=priority,
            label_id=label, search=search,
        )
    else:
        tasks = planner_service.list_tasks(db, project_id, identity["user_id"])
    return {"tasks": tasks}


@router.post("/api/planner/projects/{project_id}/tasks", response_class=JSONResponse)
def api_create_task(project_id: int, body: TaskBody, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    if not body.title.strip():
        return JSONResponse({"error": "Title is required"}, status_code=400)
    t = planner_service.create_task(
        db, project_id, identity["user_id"], body.title,
        description=body.description or "",
        priority=body.priority or "medium",
        start_date=body.start_date,
        end_date=body.end_date,
        assigned_to=body.assigned_to,
        depends_on=body.depends_on,
        parent_id=body.parent_id,
    )
    if not t:
        return JSONResponse({"error": "Project not found or no edit access"}, status_code=404)
    return {"ok": True, "task": t}


@router.get("/api/planner/tasks/{task_id}", response_class=JSONResponse)
def api_get_task(task_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    t = planner_service.get_task(db, task_id, identity["user_id"])
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"task": t}


@router.put("/api/planner/tasks/{task_id}", response_class=JSONResponse)
def api_update_task(task_id: int, body: TaskBody, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    kwargs = body.model_dump(exclude_unset=True)
    t = planner_service.update_task(db, task_id, identity["user_id"], **kwargs)
    if not t:
        return JSONResponse({"error": "Not found or no edit access"}, status_code=404)
    return {"ok": True, "task": t}


@router.delete("/api/planner/tasks/{task_id}", response_class=JSONResponse)
def api_delete_task(task_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    ok = planner_service.delete_task(db, task_id, identity["user_id"])
    if not ok:
        return JSONResponse({"error": "Not found or no edit access"}, status_code=404)
    return {"ok": True}


# ── Comments API ────────────────────────────────────────────────────────────

@router.get("/api/planner/tasks/{task_id}/comments", response_class=JSONResponse)
def api_list_comments(task_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    comments = planner_service.list_comments(db, task_id, identity["user_id"])
    if comments is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"comments": comments}


@router.post("/api/planner/tasks/{task_id}/comments", response_class=JSONResponse)
def api_add_comment(task_id: int, body: CommentBody, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    c = planner_service.add_comment(db, task_id, identity["user_id"], body.content)
    if not c:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"ok": True, "comment": c}


@router.put("/api/planner/comments/{comment_id}", response_class=JSONResponse)
def api_update_comment(comment_id: int, body: CommentBody, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    c = planner_service.update_comment(db, comment_id, identity["user_id"], body.content)
    if not c:
        return JSONResponse({"error": "Not found or not yours"}, status_code=404)
    return {"ok": True, "comment": c}


@router.delete("/api/planner/comments/{comment_id}", response_class=JSONResponse)
def api_delete_comment(comment_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    ok = planner_service.delete_comment(db, comment_id, identity["user_id"])
    if not ok:
        return JSONResponse({"error": "Not found or not yours"}, status_code=404)
    return {"ok": True}


# ── Activity API ────────────────────────────────────────────────────────────

@router.get("/api/planner/tasks/{task_id}/activity", response_class=JSONResponse)
def api_task_activity(task_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    activity = planner_service.get_task_activity(db, task_id, identity["user_id"])
    if activity is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"activity": activity}


# ── Labels API ──────────────────────────────────────────────────────────────

@router.get("/api/planner/projects/{project_id}/labels", response_class=JSONResponse)
def api_list_labels(project_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    labels = planner_service.list_labels(db, project_id, identity["user_id"])
    if labels is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"labels": labels}


@router.post("/api/planner/projects/{project_id}/labels", response_class=JSONResponse)
def api_create_label(project_id: int, body: LabelBody, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    lb = planner_service.create_label(db, project_id, identity["user_id"], body.name, body.color or "#6366f1")
    if not lb:
        return JSONResponse({"error": "Cannot create"}, status_code=403)
    return {"ok": True, "label": lb}


@router.delete("/api/planner/labels/{label_id}", response_class=JSONResponse)
def api_delete_label(label_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    ok = planner_service.delete_label(db, label_id, identity["user_id"])
    if not ok:
        return JSONResponse({"error": "Cannot delete"}, status_code=403)
    return {"ok": True}


@router.post("/api/planner/tasks/{task_id}/labels/{label_id}", response_class=JSONResponse)
def api_add_task_label(task_id: int, label_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    ok = planner_service.add_task_label(db, task_id, label_id, identity["user_id"])
    if not ok:
        return JSONResponse({"error": "Cannot add label"}, status_code=403)
    return {"ok": True}


@router.delete("/api/planner/tasks/{task_id}/labels/{label_id}", response_class=JSONResponse)
def api_remove_task_label(task_id: int, label_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    ok = planner_service.remove_task_label(db, task_id, label_id, identity["user_id"])
    if not ok:
        return JSONResponse({"error": "Cannot remove label"}, status_code=403)
    return {"ok": True}


# ── Watch API ───────────────────────────────────────────────────────────────

@router.post("/api/planner/tasks/{task_id}/watch", response_class=JSONResponse)
def api_watch_task(task_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    ok = planner_service.watch_task(db, task_id, identity["user_id"])
    if not ok:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"ok": True}


@router.delete("/api/planner/tasks/{task_id}/watch", response_class=JSONResponse)
def api_unwatch_task(task_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    ok = planner_service.unwatch_task(db, task_id, identity["user_id"])
    if not ok:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"ok": True}


# ── Summary for AI context ─────────────────────────────────────────────────

@router.get("/api/planner/summary", response_class=JSONResponse)
def api_summary(request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    return {"summaries": planner_service.get_all_users_task_summary(db)}
