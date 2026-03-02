"""Admin routes: dashboard, user management, pairing, exports, reset."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import csv
import io

from ..deps import get_db
from ..models import Chore, Birthday, ChatLog, User
from ..health import check_db, check_ollama, reset_demo_data
from ..metrics import latency_stats, get_counts, model_stats
from .. import openai_client
from ..pairing import generate_pair_code

router = APIRouter()
templates = None


def init_templates(t: Jinja2Templates):
    global templates
    templates = t


@router.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    db_status = check_db(db)
    ollama_status = check_ollama()
    counts = get_counts(db)
    lat = latency_stats()
    ok = bool(db_status.get("ok") and ollama_status.get("ok"))
    ms = model_stats(db)
    openai_configured = openai_client.is_configured()

    logs = db.query(ChatLog).order_by(ChatLog.id.desc()).limit(20).all()
    logs_html = "".join(
        f"<li>{l.created_at} | {l.client_ip} | {l.action_type} | <code>{l.trace_id}</code> | {l.message}</li>"
        for l in logs
    ) or "<li>No chat logs yet.</li>"

    return templates.TemplateResponse(request, "admin.html", {
        "ok": ok,
        "db_status": db_status,
        "ollama_status": ollama_status,
        "counts": counts,
        "lat": lat,
        "model_stats": ms,
        "openai_configured": openai_configured,
        "openai_model": openai_client.OPENAI_MODEL,
        "logs_html": logs_html,
    })


@router.post("/admin/reset")
def admin_reset(db: Session = Depends(get_db)):
    reset_demo_data(db)
    return RedirectResponse("/admin", status_code=303)


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.name.asc()).all()
    user_items = "".join(
        [f"<li>#{u.id} {u.name}</li>" for u in users]
    ) or "<li>No users yet.</li>"

    return templates.TemplateResponse(request, "admin_users.html", {
        "user_items": user_items,
    })


@router.post("/admin/users")
def admin_create_user(name: str = Form(...), db: Session = Depends(get_db)):
    db.add(User(name=name.strip()))
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/pair-code", response_class=HTMLResponse)
def admin_pair_code(user_id: int = Form(...), db: Session = Depends(get_db)):
    code = generate_pair_code(db, user_id=user_id, minutes_valid=10)
    return HTMLResponse(
        f"<p>Pairing code (valid 10 min): <b>{code}</b></p>"
        f"<p>Go to <code>/pair</code> on the device.</p>"
        f"<p><a href='/admin/users'>Back</a></p>"
    )


@router.get("/export/chores.csv")
def export_chores_csv(db: Session = Depends(get_db)):
    chores = db.query(Chore).order_by(Chore.id.asc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "title", "done"])
    for c in chores:
        writer.writerow([c.id, c.title, c.done])
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
