"""Shared FastAPI dependencies for CHILI routes."""
from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .db import SessionLocal
from .pairing import DEVICE_COOKIE_NAME, get_identity, get_identity_record


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_convo_key(identity: dict, device_token: str | None, client_ip: str) -> str:
    if not identity["is_guest"] and identity["user_id"] is not None:
        return f"user:{identity['user_id']}"
    return f"guest:{device_token or client_ip}"


def get_identity_ctx(request: Request, db: Session = Depends(get_db)):
    """Resolve the current user's identity from cookies/IP. Returns a dict with all identity info."""
    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)
    user_name = identity["user_name"]
    is_guest = identity["is_guest"]
    return {
        "db": db,
        "identity": identity,
        "convo_key": convo_key,
        "user_name": user_name,
        "is_guest": is_guest,
        "user_id": identity.get("user_id"),
        "client_ip": client_ip,
        "device_token": device_token,
    }


def require_paired(request: Request, db: Session = Depends(get_db)):
    """Gate for admin routes -- redirects guests to /chat."""
    ctx = get_identity_ctx(request, db)
    if ctx["is_guest"]:
        return None
    return ctx
