"""Page routes: home, profile, pair, chores, birthdays form handlers."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import date
import json as json_mod

from ..deps import get_db
from ..models import Chore, Birthday, HousemateProfile
from ..pairing import (
    DEVICE_COOKIE_NAME, redeem_pair_code, register_device,
    get_identity_record,
)

router = APIRouter()
templates = None


def init_templates(t: Jinja2Templates):
    global templates
    templates = t


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    chores = db.query(Chore).order_by(Chore.id.desc()).all()
    birthdays = db.query(Birthday).order_by(Birthday.date.asc()).all()

    chore_items = "".join(
        f"<li>{'✅' if c.done else '⬜'} {c.title} "
        f"<a href='/chores/{c.id}/done'>mark done</a></li>"
        for c in chores
    ) or "<li>No chores yet.</li>"

    bday_items = "".join(
        f"<li>🎂 {b.name} — {b.date.isoformat()}</li>"
        for b in birthdays
    ) or "<li>No birthdays yet.</li>"

    return templates.TemplateResponse(request, "home.html", {
        "chore_items": chore_items,
        "bday_items": bday_items,
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

    user_id = identity["user_id"]
    user_name = identity["user_name"]
    profile = db.query(HousemateProfile).filter(HousemateProfile.user_id == user_id).first()

    interests_str = ""
    if profile and profile.interests:
        try:
            interests_list = json_mod.loads(profile.interests)
            interests_str = ", ".join(interests_list)
        except json_mod.JSONDecodeError:
            interests_str = profile.interests

    dietary = profile.dietary if profile else ""
    tone = profile.tone if profile else ""
    notes = profile.notes if profile else ""
    last_updated = (
        profile.last_extracted_at.strftime("%B %d, %Y %H:%M")
        if profile and profile.last_extracted_at else "Never"
    )

    return templates.TemplateResponse(request, "profile.html", {
        "user_name": user_name,
        "interests_str": interests_str,
        "dietary": dietary,
        "tone": tone,
        "notes": notes,
        "last_updated": last_updated,
    })


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
