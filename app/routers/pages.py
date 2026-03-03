"""Page routes: home, profile, pair, chores, birthdays form handlers."""
from fastapi import APIRouter, Depends, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import date, datetime, timedelta
from pydantic import BaseModel
from typing import Optional
import json as json_mod

from ..deps import get_db
from ..models import Chore, Birthday, HousemateProfile, User, UserStatus, UserMemory
from ..pairing import (
    DEVICE_COOKIE_NAME, redeem_pair_code, register_device,
    get_identity_record, generate_pair_code,
)
from .. import email_service
from ..services import home_service

router = APIRouter()
templates = None


def init_templates(t: Jinja2Templates):
    global templates
    templates = t


def _days_until(bday_date: date) -> int:
    """Days until the next occurrence of this birthday."""
    today = date.today()
    this_year = bday_date.replace(year=today.year)
    if this_year < today:
        this_year = this_year.replace(year=today.year + 1)
    return (this_year - today).days


def _greeting() -> str:
    hour = datetime.now().hour
    if hour < 12:
        return "Good morning"
    elif hour < 17:
        return "Good afternoon"
    return "Good evening"


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    dashboard = home_service.get_dashboard_data(db, identity)

    return templates.TemplateResponse(request, "home.html", {
        "greeting": _greeting(),
        "user_name": identity["user_name"],
        "is_guest": identity["is_guest"],
        "dashboard_json": json_mod.dumps(dashboard),
    })


@router.post("/chores")
def add_chore(title: str = Form(...), db: Session = Depends(get_db)):
    db.add(Chore(title=title, done=False))
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/chores/{chore_id}/done")
def mark_chore_done(chore_id: int, db: Session = Depends(get_db)):
    chore = db.query(Chore).filter(Chore.id == chore_id).first()
    if chore:
        chore.done = True
        db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/birthdays")
def add_birthday_form(
    name: str = Form(...),
    date: date = Form(...),
    db: Session = Depends(get_db),
):
    db.add(Birthday(name=name, date=date))
    db.commit()
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# JSON API for AJAX home page interactions
# ---------------------------------------------------------------------------

class AddChoreBody(BaseModel):
    title: str
    priority: str = "medium"
    due_date: Optional[str] = None
    recurrence: str = "none"
    assigned_to: Optional[int] = None

class UpdateChoreBody(BaseModel):
    title: Optional[str] = None
    priority: Optional[str] = None
    due_date: Optional[str] = None
    recurrence: Optional[str] = None
    assigned_to: Optional[int] = None

class AddBirthdayBody(BaseModel):
    name: str
    date: str


def _chore_dict(c: Chore) -> dict:
    return {
        "id": c.id, "title": c.title, "done": c.done,
        "priority": c.priority or "medium",
        "due_date": c.due_date.isoformat() if c.due_date else None,
        "recurrence": c.recurrence or "none",
        "assigned_to": c.assigned_to,
        "assignee_name": c.assignee.name if c.assigned_to and c.assignee else "",
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
    }


@router.post("/api/chores", response_class=JSONResponse)
def api_add_chore(body: AddChoreBody, request: Request, db: Session = Depends(get_db)):
    due = None
    if body.due_date:
        try:
            due = date.fromisoformat(body.due_date)
        except ValueError:
            pass
    c = Chore(
        title=body.title.strip(), done=False,
        priority=body.priority if body.priority in ("low", "medium", "high") else "medium",
        due_date=due, recurrence=body.recurrence if body.recurrence in ("none", "daily", "weekly", "monthly") else "none",
        assigned_to=body.assigned_to, created_at=datetime.utcnow(),
    )
    db.add(c)
    db.commit()
    db.refresh(c)

    identity = get_identity_record(db, request.cookies.get(DEVICE_COOKIE_NAME))
    home_service.log_activity(
        db, "chore_added", f'Added chore "{c.title}"',
        user_id=identity.get("user_id"), user_name=identity["user_name"], icon="plus",
    )

    return {"ok": True, "chore": _chore_dict(c)}


@router.put("/api/chores/{chore_id}", response_class=JSONResponse)
def api_chore_update(chore_id: int, body: UpdateChoreBody, db: Session = Depends(get_db)):
    c = db.query(Chore).filter(Chore.id == chore_id).first()
    if not c:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    if body.title is not None:
        c.title = body.title.strip()
    if body.priority is not None and body.priority in ("low", "medium", "high"):
        c.priority = body.priority
    if body.due_date is not None:
        try:
            c.due_date = date.fromisoformat(body.due_date) if body.due_date else None
        except ValueError:
            pass
    if body.recurrence is not None and body.recurrence in ("none", "daily", "weekly", "monthly"):
        c.recurrence = body.recurrence
    if body.assigned_to is not None:
        c.assigned_to = body.assigned_to if body.assigned_to > 0 else None
    db.commit()
    db.refresh(c)
    return {"ok": True, "chore": _chore_dict(c)}


@router.post("/api/chores/{chore_id}/done", response_class=JSONResponse)
def api_chore_done(chore_id: int, request: Request, db: Session = Depends(get_db)):
    c = db.query(Chore).filter(Chore.id == chore_id).first()
    if not c:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    c.done = not c.done
    c.completed_at = datetime.utcnow() if c.done else None

    if c.done and c.recurrence and c.recurrence != "none" and c.due_date:
        _spawn_recurring(c, db)

    db.commit()

    if c.done:
        identity = get_identity_record(db, request.cookies.get(DEVICE_COOKIE_NAME))
        home_service.log_activity(
            db, "chore_done", f'Completed "{c.title}"',
            user_id=identity.get("user_id"), user_name=identity["user_name"], icon="check",
        )

    return {"ok": True, "chore": _chore_dict(c)}


def _spawn_recurring(chore: Chore, db: Session):
    """Create the next occurrence of a recurring chore."""
    from dateutil.relativedelta import relativedelta
    deltas = {
        "daily": timedelta(days=1),
        "weekly": timedelta(weeks=1),
        "monthly": relativedelta(months=1),
    }
    delta = deltas.get(chore.recurrence)
    if not delta or not chore.due_date:
        return
    next_date = chore.due_date + delta
    exists = db.query(Chore).filter(
        Chore.title == chore.title,
        Chore.due_date == next_date,
        Chore.done == False,
    ).first()
    if not exists:
        db.add(Chore(
            title=chore.title, done=False, priority=chore.priority,
            due_date=next_date, recurrence=chore.recurrence,
            assigned_to=chore.assigned_to, created_at=datetime.utcnow(),
        ))


@router.delete("/api/chores/{chore_id}", response_class=JSONResponse)
def api_chore_delete(chore_id: int, db: Session = Depends(get_db)):
    c = db.query(Chore).filter(Chore.id == chore_id).first()
    if not c:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    db.delete(c)
    db.commit()
    return {"ok": True}


@router.post("/api/birthdays", response_class=JSONResponse)
def api_add_birthday(body: AddBirthdayBody, request: Request, db: Session = Depends(get_db)):
    try:
        d = date.fromisoformat(body.date)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid date"}, status_code=400)
    b = Birthday(name=body.name.strip(), date=d)
    db.add(b)
    db.commit()
    db.refresh(b)
    days = _days_until(b.date)

    identity = get_identity_record(db, request.cookies.get(DEVICE_COOKIE_NAME))
    home_service.log_activity(
        db, "birthday_added", f"Added {b.name}'s birthday",
        user_id=identity.get("user_id"), user_name=identity["user_name"], icon="cake",
    )

    return {"ok": True, "birthday": {"id": b.id, "name": b.name, "date": b.date.isoformat(), "days_until": days}}


@router.delete("/api/birthdays/{bday_id}", response_class=JSONResponse)
def api_birthday_delete(bday_id: int, db: Session = Depends(get_db)):
    b = db.query(Birthday).filter(Birthday.id == bday_id).first()
    if not b:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    db.delete(b)
    db.commit()
    return {"ok": True}


# ── Dashboard & Activity APIs ─────────────────────────────────────────────────

@router.get("/api/dashboard", response_class=JSONResponse)
def dashboard_api(request: Request, db: Session = Depends(get_db)):
    identity = get_identity_record(db, request.cookies.get(DEVICE_COOKIE_NAME))
    return home_service.get_dashboard_data(db, identity)


@router.get("/api/activity", response_class=JSONResponse)
def activity_api(
    request: Request,
    limit: int = Query(20, ge=1, le=50),
    before_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    return {"events": home_service.get_activity_feed(db, limit=limit, before_id=before_id)}


@router.get("/api/calendar", response_class=JSONResponse)
def calendar_api(
    year: int = Query(None),
    month: int = Query(None),
    db: Session = Depends(get_db),
):
    today = date.today()
    y = year or today.year
    m = month or today.month
    return {"events": home_service.get_calendar_events(db, y, m), "year": y, "month": m}


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db)):
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)

    if identity["is_guest"] or not identity["user_id"]:
        return HTMLResponse(
            "<html><body style='font-family:Arial;max-width:800px;margin:40px auto;'>"
            "<h1>Profile</h1><p>You need to be a paired housemate to view your profile.</p>"
            "<p><a href='/pair'>Pair your device</a> | <a href='/chat'>Back to Chat</a></p>"
            "</body></html>"
        )

    return templates.TemplateResponse(request, "profile.html", {
        "user_name": identity["user_name"],
    })


@router.get("/api/profile", response_class=JSONResponse)
def profile_api(request: Request, db: Session = Depends(get_db)):
    """Full profile data for the profile page."""
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)

    if identity["is_guest"] or not identity["user_id"]:
        return JSONResponse({"error": "Not paired"}, status_code=403)

    user_id = identity["user_id"]
    user = db.query(User).filter(User.id == user_id).first()
    profile = db.query(HousemateProfile).filter(HousemateProfile.user_id == user_id).first()

    interests_list = []
    if profile and profile.interests:
        try:
            interests_list = json_mod.loads(profile.interests)
        except json_mod.JSONDecodeError:
            interests_list = [profile.interests]

    from .. import memory as memory_module
    breakdown = memory_module.get_interest_breakdown(user_id, db)
    memory_count = db.query(UserMemory).filter(
        UserMemory.user_id == user_id, UserMemory.superseded == False
    ).count()

    return {
        "user_name": user.name if user else identity["user_name"],
        "member_since": user.id if user else None,
        "profile": {
            "interests": interests_list,
            "dietary": profile.dietary if profile else "",
            "tone": profile.tone if profile else "",
            "notes": profile.notes if profile else "",
            "last_updated": (
                profile.last_extracted_at.strftime("%B %d, %Y %H:%M")
                if profile and profile.last_extracted_at else "Never"
            ),
        },
        "memory_count": memory_count,
        "interest_breakdown": breakdown,
    }


@router.post("/api/profile", response_class=JSONResponse)
def profile_save_api(request: Request, db: Session = Depends(get_db)):
    """Save profile fields via JSON body."""
    import asyncio
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)

    if identity["is_guest"] or not identity["user_id"]:
        return JSONResponse({"error": "Not paired"}, status_code=403)

    return JSONResponse({"ok": True})


@router.post("/profile")
def profile_save(
    request: Request,
    interests: str = Form(""),
    dietary: str = Form(""),
    tone: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)

    if identity["is_guest"] or not identity["user_id"]:
        return RedirectResponse("/profile", status_code=303)

    user_id = identity["user_id"]
    interests_list = [i.strip() for i in interests.split(",") if i.strip()]
    interests_json = json_mod.dumps(interests_list)

    profile = db.query(HousemateProfile).filter(HousemateProfile.user_id == user_id).first()
    if profile:
        profile.interests = interests_json
        profile.dietary = dietary.strip()
        profile.tone = tone.strip()
        profile.notes = notes.strip()
    else:
        db.add(HousemateProfile(
            user_id=user_id,
            interests=interests_json,
            dietary=dietary.strip(),
            tone=tone.strip(),
            notes=notes.strip(),
        ))
    db.commit()
    return RedirectResponse("/profile", status_code=303)


@router.get("/api/profile/memories", response_class=JSONResponse)
def profile_memories(
    request: Request,
    page: int = 1,
    db: Session = Depends(get_db),
):
    """Paginated memories for the profile timeline."""
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)

    if identity["is_guest"] or not identity["user_id"]:
        return JSONResponse({"error": "Not paired"}, status_code=403)

    from .. import memory as memory_module
    return memory_module.get_memories_paginated(identity["user_id"], db, page=page)


@router.delete("/api/profile/memories/{memory_id}", response_class=JSONResponse)
def delete_memory(memory_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete a specific memory."""
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)

    if identity["is_guest"] or not identity["user_id"]:
        return JSONResponse({"error": "Not paired"}, status_code=403)

    from .. import memory as memory_module
    ok = memory_module.delete_memory(memory_id, identity["user_id"], db)
    if not ok:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"ok": True}


@router.get("/api/profile/interests", response_class=JSONResponse)
def profile_interests(request: Request, db: Session = Depends(get_db)):
    """Interest/category breakdown for chart."""
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)

    if identity["is_guest"] or not identity["user_id"]:
        return JSONResponse({"error": "Not paired"}, status_code=403)

    from .. import memory as memory_module
    return {"breakdown": memory_module.get_interest_breakdown(identity["user_id"], db)}


@router.get("/pair", response_class=HTMLResponse)
def pair_page(request: Request):
    return templates.TemplateResponse(request, "pair.html")


@router.post("/pair")
def pair_submit(
    request: Request,
    code: str = Form(...),
    label: str = Form(...),
    db: Session = Depends(get_db),
):
    client_ip = request.client.host
    pc = redeem_pair_code(db, code.strip())
    if not pc:
        return HTMLResponse(
            "<p>Invalid/expired code. Ask admin for a new one.</p><p><a href='/pair'>Back</a></p>",
            status_code=400,
        )

    token = register_device(db, user_id=pc.user_id, label=label.strip(), client_ip=client_ip)
    resp = RedirectResponse("/chat", status_code=303)
    resp.set_cookie(DEVICE_COOKIE_NAME, token, httponly=True, samesite="lax")
    return resp


# ---------------------------------------------------------------------------
# Self-service email pairing API
# ---------------------------------------------------------------------------

class PairRequestBody(BaseModel):
    email: str

class PairVerifyBody(BaseModel):
    code: str
    label: str = "Unknown Device"


@router.post("/api/pair/request")
def pair_request(body: PairRequestBody, db: Session = Depends(get_db)):
    """Guest sends their email to receive a pairing code."""
    email = body.email.strip().lower()
    if not email:
        return JSONResponse({"ok": False, "error": "Email is required."}, status_code=400)

    user = db.query(User).filter(User.email == email).first()
    if not user:
        return JSONResponse({
            "ok": False,
            "error": "This email isn't registered. Ask the admin to add you as a housemate.",
        }, status_code=404)

    code = generate_pair_code(db, user_id=user.id, minutes_valid=10, numeric=True)

    if email_service.is_configured():
        sent = email_service.send_pairing_code(email, code, user.name)
        if not sent:
            return JSONResponse({
                "ok": False,
                "error": "Could not send email. Ask the admin to check email settings.",
            }, status_code=500)
        return JSONResponse({"ok": True, "message": f"Code sent to {email}."})
    else:
        # Email not configured -- return code directly (dev/local mode)
        return JSONResponse({
            "ok": True,
            "message": f"Email not configured. Your code is: {code}",
            "dev_code": code,
        })


@router.post("/api/pair/verify")
def pair_verify(body: PairVerifyBody, request: Request, db: Session = Depends(get_db)):
    """Guest submits the code to pair their device."""
    code = body.code.strip()
    label = body.label.strip() or "Unknown Device"

    pc = redeem_pair_code(db, code)
    if not pc:
        return JSONResponse({
            "ok": False,
            "error": "Invalid or expired code. Request a new one.",
        }, status_code=400)

    client_ip = request.client.host
    token = register_device(db, user_id=pc.user_id, label=label, client_ip=client_ip)

    user = db.query(User).filter(User.id == pc.user_id).first()
    resp = JSONResponse({"ok": True, "user_name": user.name if user else "Housemate"})
    resp.set_cookie(DEVICE_COOKIE_NAME, token, httponly=True, samesite="lax")
    return resp
