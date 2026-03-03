"""Admin routes: dashboard, user management, pairing, exports."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import csv
import io
import json

from ..deps import get_db, require_paired
from ..models import Chore, Birthday, ChatLog, User, Device, HousemateProfile, UserMemory
from ..health import check_db, check_ollama
from ..metrics import (
    latency_stats, get_counts, model_stats, total_stats, user_stats, rag_stats,
    admin_dashboard_json, messages_per_day, hourly_activity, feature_usage,
    per_user_chore_stats, system_alerts, response_time_trend, top_users,
    conversation_stats, action_type_stats, latency_history,
)
from .. import openai_client
from ..pairing import generate_pair_code

router = APIRouter()
templates = None


def init_templates(t: Jinja2Templates):
    global templates
    templates = t


def _guard(ctx):
    """Return a redirect response if the user is a guest, else None."""
    if ctx is None:
        return RedirectResponse("/chat", status_code=303)
    return None


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect

    db: Session = ctx["db"]
    dashboard = admin_dashboard_json(db)

    logs = db.query(ChatLog).order_by(ChatLog.id.desc()).limit(200).all()
    log_rows = [
        {
            "time": l.created_at.strftime("%Y-%m-%d %H:%M:%S") if l.created_at else "—",
            "ip": l.client_ip,
            "action": l.action_type,
            "trace_id": l.trace_id,
            "message": (l.message[:80] + "...") if len(l.message) > 80 else l.message,
        }
        for l in logs
    ]
    action_types = sorted(set(r["action"] for r in log_rows))

    return templates.TemplateResponse(request, "admin.html", {
        "user_name": ctx["user_name"],
        "dashboard_json": json.dumps(dashboard),
        "log_rows": log_rows,
        "action_types": action_types,
    })


@router.get("/api/admin/dashboard")
def api_admin_dashboard(ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect
    db: Session = ctx["db"]
    return JSONResponse(admin_dashboard_json(db))


@router.get("/api/admin/alerts")
def api_admin_alerts(ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect
    db: Session = ctx["db"]
    return JSONResponse(system_alerts(db))


@router.get("/api/admin/logs")
def api_admin_logs(ctx=Depends(require_paired), limit: int = 200):
    redirect = _guard(ctx)
    if redirect:
        return redirect
    db: Session = ctx["db"]
    logs = db.query(ChatLog).order_by(ChatLog.id.desc()).limit(min(limit, 500)).all()
    return JSONResponse([
        {
            "time": l.created_at.strftime("%Y-%m-%d %H:%M:%S") if l.created_at else "—",
            "ip": l.client_ip,
            "action": l.action_type,
            "trace_id": l.trace_id,
            "message": (l.message[:80] + "...") if len(l.message) > 80 else l.message,
        }
        for l in logs
    ])


# ---------------------------------------------------------------------------
# User Management
# ---------------------------------------------------------------------------

@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect

    db: Session = ctx["db"]
    users = user_stats(db)
    all_users = db.query(User).order_by(User.name.asc()).all()

    return templates.TemplateResponse(request, "admin_users.html", {
        "user_name": ctx["user_name"],
        "users": users,
        "all_users": all_users,
    })


@router.post("/admin/users")
def admin_create_user(
    name: str = Form(...),
    email: str = Form(""),
    ctx=Depends(require_paired),
):
    redirect = _guard(ctx)
    if redirect:
        return redirect

    db: Session = ctx["db"]
    user = User(name=name.strip())
    if email.strip():
        user.email = email.strip().lower()
    db.add(user)
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/delete")
def admin_delete_user(user_id: int, ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect

    db: Session = ctx["db"]
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/admin/users", status_code=303)

    db.query(Device).filter(Device.user_id == user_id).delete()
    db.query(HousemateProfile).filter(HousemateProfile.user_id == user_id).delete()
    db.delete(user)
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


# ---------------------------------------------------------------------------
# User Memories API
# ---------------------------------------------------------------------------

@router.get("/api/admin/user/{user_id}/memories")
def api_user_memories(user_id: int, ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect
    db: Session = ctx["db"]
    memories = (
        db.query(UserMemory)
        .filter(UserMemory.user_id == user_id, UserMemory.superseded == False)
        .order_by(UserMemory.created_at.desc())
        .limit(50)
        .all()
    )
    return JSONResponse([
        {
            "id": m.id,
            "category": m.category,
            "content": m.content,
            "created_at": m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else "",
        }
        for m in memories
    ])


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------

@router.post("/admin/pair-code")
def admin_pair_code(request: Request, user_id: int = Form(...), ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect

    db: Session = ctx["db"]
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse({"ok": False, "error": "User not found"}, status_code=404)

    code = generate_pair_code(db, user_id=user_id, minutes_valid=10)

    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"ok": True, "code": code, "user_name": user.name})

    return JSONResponse({"ok": True, "code": code, "user_name": user.name})


# ---------------------------------------------------------------------------
# RAG Management
# ---------------------------------------------------------------------------

@router.post("/admin/rag/ingest")
def admin_rag_ingest(ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect

    from ..rag import ingest_documents
    result = ingest_documents(trace_id="admin-ingest")
    return JSONResponse(result)


@router.get("/admin/rag/status")
def admin_rag_status(ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect

    return JSONResponse(rag_stats())


# ---------------------------------------------------------------------------
# CSV Exports
# ---------------------------------------------------------------------------

@router.get("/export/chores.csv")
def export_chores_csv(db: Session = Depends(get_db)):
    chores = db.query(Chore).order_by(Chore.id.asc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "title", "done", "priority", "due_date", "recurrence", "assigned_to", "created_at", "completed_at"])
    for c in chores:
        writer.writerow([
            c.id, c.title, c.done, c.priority or "", 
            c.due_date.isoformat() if c.due_date else "",
            c.recurrence or "", c.assigned_to or "",
            c.created_at.isoformat() if c.created_at else "",
            c.completed_at.isoformat() if c.completed_at else "",
        ])
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=chores.csv"},
    )


@router.get("/export/birthdays.csv")
def export_birthdays_csv(db: Session = Depends(get_db)):
    birthdays = db.query(Birthday).order_by(Birthday.date.asc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "name", "date"])
    for b in birthdays:
        writer.writerow([b.id, b.name, b.date.isoformat()])
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=birthdays.csv"},
    )
