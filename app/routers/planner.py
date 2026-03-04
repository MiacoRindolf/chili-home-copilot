"""Project planner routes: page + JSON API for projects & tasks."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import json as json_mod

from ..deps import get_db
from ..pairing import DEVICE_COOKIE_NAME, get_identity_record
from ..services import planner_service
from ..models import User

router = APIRouter()
templates = None


def init_templates(t: Jinja2Templates):
    global templates
    templates = t


def _require_user(request: Request, db: Session) -> dict | None:
    identity = get_identity_record(db, request.cookies.get(DEVICE_COOKIE_NAME))
    if identity["is_guest"] or not identity.get("user_id"):
        return None
    return identity


# ── Page ──────────────────────────────────────────────────────────────────────

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

    return templates.TemplateResponse(request, "planner.html", {
        "user_name": identity["user_name"],
        "user_id": user_id,
        "is_guest": False,
        "initial_data": json_mod.dumps({
            "projects": projects,
            "users": users,
            "user_summaries": user_summaries,
        }),
    })


# ── Project API ───────────────────────────────────────────────────────────────

class ProjectBody(BaseModel):
    name: str
    description: Optional[str] = ""
    color: Optional[str] = "#6366f1"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: Optional[str] = None


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
    )
    if not p:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"ok": True, "project": p}


@router.delete("/api/planner/projects/{project_id}", response_class=JSONResponse)
def api_delete_project(project_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    ok = planner_service.delete_project(db, project_id, identity["user_id"])
    if not ok:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"ok": True}


# ── Task API ──────────────────────────────────────────────────────────────────

class TaskBody(BaseModel):
    title: str
    description: Optional[str] = ""
    priority: Optional[str] = "medium"
    status: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    assigned_to: Optional[int] = None
    depends_on: Optional[int] = None
    progress: Optional[int] = None
    sort_order: Optional[int] = None


@router.get("/api/planner/projects/{project_id}/tasks", response_class=JSONResponse)
def api_list_tasks(project_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    return {"tasks": planner_service.list_tasks(db, project_id, identity["user_id"])}


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
    )
    if not t:
        return JSONResponse({"error": "Project not found"}, status_code=404)
    return {"ok": True, "task": t}


@router.put("/api/planner/tasks/{task_id}", response_class=JSONResponse)
def api_update_task(task_id: int, body: TaskBody, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    kwargs = body.model_dump(exclude_unset=True)
    t = planner_service.update_task(db, task_id, identity["user_id"], **kwargs)
    if not t:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"ok": True, "task": t}


@router.delete("/api/planner/tasks/{task_id}", response_class=JSONResponse)
def api_delete_task(task_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    ok = planner_service.delete_task(db, task_id, identity["user_id"])
    if not ok:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"ok": True}


# ── Summary for AI context ────────────────────────────────────────────────────

@router.get("/api/planner/summary", response_class=JSONResponse)
def api_summary(request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Not paired"}, status_code=403)
    return {"summaries": planner_service.get_all_users_task_summary(db)}
