"""Google OAuth SSO routes.

Provides /auth/google (initiate), /auth/google/callback (complete), and
/auth/logout.  Uses authlib for the server-side OAuth 2.0 flow and integrates
with the existing User / Device / cookie identity system.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..deps import get_db
from ..models.core import User, Device
from ..pairing import DEVICE_COOKIE_NAME

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])

_oauth = None


def _get_oauth():
    """Lazy-init the authlib OAuth registry so imports succeed even if authlib
    is not installed."""
    global _oauth
    if _oauth is not None:
        return _oauth
    try:
        from authlib.integrations.starlette_client import OAuth
    except ImportError:
        logger.warning("[auth] authlib not installed — Google OAuth disabled")
        return None

    _oauth = OAuth()
    _oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    return _oauth


def _google_configured() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


# ── Initiate OAuth ──────────────────────────────────────────────────────

@router.get("/auth/google")
async def auth_google(request: Request):
    """Redirect the user to Google's OAuth consent screen."""
    if not _google_configured():
        return RedirectResponse("/trading?auth_error=google_not_configured")

    oauth = _get_oauth()
    if oauth is None:
        return RedirectResponse("/trading?auth_error=authlib_missing")

    redirect_uri = str(request.url_for("auth_google_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


# ── OAuth callback ──────────────────────────────────────────────────────

@router.get("/auth/google/callback")
async def auth_google_callback(request: Request, db: Session = Depends(get_db)):
    """Exchange the auth code for tokens, upsert user, set device cookie."""
    if not _google_configured():
        return RedirectResponse("/trading?auth_error=google_not_configured")

    oauth = _get_oauth()
    if oauth is None:
        return RedirectResponse("/trading?auth_error=authlib_missing")

    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as e:
        logger.error(f"[auth] Google token exchange failed: {e}")
        return RedirectResponse("/trading?auth_error=token_exchange_failed")

    userinfo = token.get("userinfo")
    if not userinfo:
        return RedirectResponse("/trading?auth_error=no_userinfo")

    google_id = userinfo.get("sub")
    email = userinfo.get("email", "")
    name = userinfo.get("name", email.split("@")[0] if email else "User")
    avatar = userinfo.get("picture", "")

    if not google_id:
        return RedirectResponse("/trading?auth_error=no_google_id")

    user = db.query(User).filter(User.google_id == google_id).first()

    if user:
        changed = False
        if email and user.email != email:
            existing = db.query(User).filter(User.email == email, User.id != user.id).first()
            if not existing:
                user.email = email
                changed = True
        if avatar and user.avatar_url != avatar:
            user.avatar_url = avatar
            changed = True
        if changed:
            db.commit()
    else:
        existing_email = db.query(User).filter(User.email == email).first() if email else None
        if existing_email:
            existing_email.google_id = google_id
            existing_email.avatar_url = avatar or existing_email.avatar_url
            db.commit()
            user = existing_email
        else:
            base_name = name
            suffix = 0
            while db.query(User).filter(User.name == name).first():
                suffix += 1
                name = f"{base_name}_{suffix}"
            user = User(name=name, email=email or None, google_id=google_id, avatar_url=avatar)
            db.add(user)
            db.commit()
            db.refresh(user)

    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    device = None
    if device_token:
        device = db.query(Device).filter(Device.token == device_token).first()

    if device and device.user_id != user.id:
        device.user_id = user.id
        db.commit()
    elif not device:
        device_token = secrets.token_hex(24)
        client_ip = request.client.host if request.client else "unknown"
        device = Device(
            token=device_token,
            user_id=user.id,
            label=f"google-oauth-{client_ip}",
            client_ip_last=client_ip,
        )
        db.add(device)
        db.commit()

    resp = RedirectResponse("/trading", status_code=302)
    resp.set_cookie(
        DEVICE_COOKIE_NAME,
        device_token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 365,
    )
    return resp


# ── Logout ──────────────────────────────────────────────────────────────

@router.post("/auth/logout")
async def auth_logout(request: Request):
    """Clear the device cookie and redirect to trading page."""
    resp = RedirectResponse("/trading", status_code=302)
    resp.delete_cookie(DEVICE_COOKIE_NAME)
    return resp
