"""Unified broker integration endpoints.

Supports Robinhood, Coinbase Advanced, and MetaMask (status only).
Broker credentials are stored per-user in the DB (encrypted), not in .env.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ...deps import get_db, get_identity_ctx
from ...models.core import User, Device
from ...pairing import DEVICE_COOKIE_NAME, register_device
from ...services import broker_service, coinbase_service, broker_manager
from ...services.credential_vault import (
    delete_broker_credentials,
    get_broker_credentials,
    has_broker_credentials,
    save_broker_credentials,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["trading-broker"])


def _ensure_user_id(db: Session, identity: dict, request: Request) -> tuple[int, str | None]:
    """Return a user_id, auto-creating a local user+device for guests.

    Returns (user_id, new_device_token_or_None).  If the second value is not
    None, the caller must set it as a cookie on the response.
    """
    uid = identity.get("user_id")
    if uid:
        return uid, None

    client_ip = request.client.host if request.client else "0.0.0.0"
    existing_cookie = request.cookies.get(DEVICE_COOKIE_NAME)

    if existing_cookie:
        dev = db.query(Device).filter(Device.token == existing_cookie).first()
        if dev:
            return dev.user_id, None

    user = User(name=f"Trader-{__import__('secrets').token_hex(3)}", email=None)
    db.add(user)
    db.flush()
    token = register_device(db, user.id, "auto-provisioned", client_ip)
    logger.info("[broker] Auto-provisioned user %s (id=%d) for credential storage", user.name, user.id)
    return user.id, token


@router.get("/api/trading/broker/status")
async def api_broker_status(
    request: Request,
    db: Session = Depends(get_db),
    identity: dict = Depends(get_identity_ctx),
):
    """Return connection status for ALL brokers, including per-user credential state."""
    statuses = broker_manager.get_all_broker_statuses()
    uid = identity.get("user_id")
    if not uid:
        cookie = request.cookies.get(DEVICE_COOKIE_NAME)
        if cookie:
            dev = db.query(Device).filter(Device.token == cookie).first()
            if dev:
                uid = dev.user_id
    if uid:
        statuses["robinhood"]["has_credentials"] = has_broker_credentials(db, uid, "robinhood")
        statuses["coinbase"]["has_credentials"] = has_broker_credentials(db, uid, "coinbase")
    else:
        statuses["robinhood"]["has_credentials"] = False
        statuses["coinbase"]["has_credentials"] = False
    return JSONResponse({"ok": True, "brokers": statuses})


@router.post("/api/trading/broker/credentials")
async def api_broker_save_credentials(
    request: Request,
    db: Session = Depends(get_db),
    identity: dict = Depends(get_identity_ctx),
):
    """Save (encrypted) broker credentials for the current user, then auto-connect.

    Guests are auto-provisioned a local user account so credentials persist
    across page reloads (tied to a device cookie).
    """
    user_id, new_token = _ensure_user_id(db, identity, request)
    body = await request.json()
    broker = body.get("broker", "")

    if broker == "robinhood":
        username = body.get("username", "").strip()
        password = body.get("password", "").strip()
        totp_secret = body.get("totp_secret", "").strip()
        if not username or not password:
            return JSONResponse({"ok": False, "status": "error", "message": "Username and password are required."})
        creds = {"username": username, "password": password}
        if totp_secret:
            creds["totp_secret"] = totp_secret
        save_broker_credentials(db, user_id, "robinhood", creds)
        result = broker_manager.connect_broker("robinhood", credentials=creds)

    elif broker == "coinbase":
        api_key = body.get("api_key", "").strip()
        api_secret = body.get("api_secret", "").strip()
        if not api_key or not api_secret:
            return JSONResponse({"ok": False, "status": "error", "message": "API Key and API Secret are required."})
        creds = {"api_key": api_key, "api_secret": api_secret}
        save_broker_credentials(db, user_id, "coinbase", creds)
        result = broker_manager.connect_broker("coinbase", credentials=creds)

    else:
        return JSONResponse({"ok": False, "status": "error", "message": f"Unknown broker: {broker!r}"})

    statuses = broker_manager.get_all_broker_statuses()
    statuses["robinhood"]["has_credentials"] = has_broker_credentials(db, user_id, "robinhood")
    statuses["coinbase"]["has_credentials"] = has_broker_credentials(db, user_id, "coinbase")
    resp = JSONResponse({
        "ok": result.get("status") == "connected",
        **result,
        "brokers": statuses,
    })
    if new_token:
        resp.set_cookie(
            DEVICE_COOKIE_NAME, new_token,
            max_age=60 * 60 * 24 * 365 * 2, httponly=True, samesite="lax",
            secure=request.url.scheme == "https",
        )
    return resp


@router.delete("/api/trading/broker/credentials")
async def api_broker_delete_credentials(
    request: Request,
    db: Session = Depends(get_db),
    identity: dict = Depends(get_identity_ctx),
):
    """Remove saved broker credentials for the current user."""
    user_id = identity.get("user_id")
    if not user_id:
        return JSONResponse({"ok": False, "message": "Sign in first."})

    body = await request.json()
    broker = body.get("broker", "")
    if broker not in ("robinhood", "coinbase"):
        return JSONResponse({"ok": False, "message": f"Unknown broker: {broker!r}"})

    deleted = delete_broker_credentials(db, user_id, broker)
    return JSONResponse({"ok": True, "deleted": deleted})


@router.post("/api/trading/broker/connect")
async def api_broker_connect(
    request: Request,
    db: Session = Depends(get_db),
    identity: dict = Depends(get_identity_ctx),
):
    """Connect to a specific broker. Loads per-user credentials from DB if available."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    broker = body.get("broker", "")
    if broker not in ("robinhood", "coinbase"):
        return JSONResponse({"ok": False, "status": "error", "message": f"Unknown broker: {broker!r}. Send {{\"broker\": \"robinhood\"}} or {{\"broker\": \"coinbase\"}}."})

    user_id, new_token = _ensure_user_id(db, identity, request)
    credentials = get_broker_credentials(db, user_id, broker) if user_id else None

    if not credentials:
        env_has = False
        if broker == "robinhood":
            env_has = broker_service._credentials_configured()
        elif broker == "coinbase":
            env_has = coinbase_service._credentials_configured()

        if not env_has:
            resp = JSONResponse({
                "ok": False,
                "status": "needs_credentials",
                "message": "No credentials found. Click to set up your account.",
                "brokers": broker_manager.get_all_broker_statuses(),
            })
            if new_token:
                resp.set_cookie(
                    DEVICE_COOKIE_NAME,
                    new_token,
                    max_age=60 * 60 * 24 * 365 * 2,
                    httponly=True,
                    samesite="lax",
                    secure=request.url.scheme == "https",
                )
            return resp

    result = broker_manager.connect_broker(broker, credentials=credentials)

    # On a successful connect, immediately mirror broker positions/orders into
    # the local DB so Monitor + Watchlist populate without waiting for the
    # Mon–Fri scheduled broker_sync job or a manual "Sync All" click.
    sync_result: dict | None = None
    if result.get("status") == "connected" and user_id:
        try:
            sync_result = broker_manager.sync_all(db, user_id)
            logger.info(
                "[broker] Post-connect sync (user=%d, broker=%s): %s",
                user_id, broker, sync_result,
            )
        except Exception:
            logger.warning(
                "[broker] Post-connect sync failed (user=%d, broker=%s)",
                user_id, broker, exc_info=True,
            )

    statuses = broker_manager.get_all_broker_statuses()
    statuses["robinhood"]["has_credentials"] = has_broker_credentials(db, user_id, "robinhood")
    statuses["coinbase"]["has_credentials"] = has_broker_credentials(db, user_id, "coinbase")
    payload: dict = {
        "ok": result.get("status") == "connected",
        **result,
        "brokers": statuses,
    }
    if sync_result is not None:
        payload["sync"] = sync_result
    resp = JSONResponse(payload)
    if new_token:
        resp.set_cookie(
            DEVICE_COOKIE_NAME,
            new_token,
            max_age=60 * 60 * 24 * 365 * 2,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
    return resp


@router.post("/api/trading/broker/verify")
async def api_broker_verify(
    request: Request,
    db: Session = Depends(get_db),
    identity: dict = Depends(get_identity_ctx),
):
    """Submit SMS/TOTP verification code (Robinhood only)."""
    body = await request.json()
    code = body.get("code", "")
    result = broker_service.login_step2_verify(code)
    statuses = broker_manager.get_all_broker_statuses()
    payload: dict = {
        "ok": result["status"] == "connected",
        **result,
        "brokers": statuses,
    }
    if result.get("status") == "connected":
        uid = identity.get("user_id")
        if not uid:
            cookie = request.cookies.get(DEVICE_COOKIE_NAME)
            if cookie:
                dev = db.query(Device).filter(Device.token == cookie).first()
                if dev:
                    uid = dev.user_id
        if uid:
            try:
                payload["sync"] = broker_manager.sync_all(db, uid)
                logger.info("[broker] Post-verify sync (user=%d): %s", uid, payload["sync"])
            except Exception:
                logger.warning("[broker] Post-verify sync failed (user=%d)", uid, exc_info=True)
    return JSONResponse(payload)


@router.get("/api/trading/broker/poll")
async def api_broker_poll(
    request: Request,
    db: Session = Depends(get_db),
    identity: dict = Depends(get_identity_ctx),
):
    """Poll Robinhood app approval."""
    result = broker_service.poll_app_approval()
    statuses = broker_manager.get_all_broker_statuses()
    payload: dict = {
        "ok": result["status"] == "approved",
        **result,
        "brokers": statuses,
    }
    if result.get("status") == "approved":
        uid = identity.get("user_id")
        if not uid:
            cookie = request.cookies.get(DEVICE_COOKIE_NAME)
            if cookie:
                dev = db.query(Device).filter(Device.token == cookie).first()
                if dev:
                    uid = dev.user_id
        if uid:
            try:
                payload["sync"] = broker_manager.sync_all(db, uid)
                logger.info("[broker] Post-approval sync (user=%d): %s", uid, payload["sync"])
            except Exception:
                logger.warning("[broker] Post-approval sync failed (user=%d)", uid, exc_info=True)
    return JSONResponse(payload)


@router.get("/api/trading/broker/positions")
async def api_broker_positions():
    """Return combined positions from all connected brokers."""
    positions = broker_manager.get_combined_positions()
    return JSONResponse({"ok": True, "positions": positions})


@router.get("/api/trading/broker/portfolio")
async def api_broker_portfolio():
    """Return combined portfolio from all connected brokers."""
    portfolio = broker_manager.get_combined_portfolio()
    return JSONResponse({"ok": True, "portfolio": portfolio})


@router.post("/api/trading/broker/sync")
async def api_broker_sync(
    db: Session = Depends(get_db),
    identity: dict = Depends(get_identity_ctx),
):
    """Sync trades with all connected brokers."""
    user_id = identity.get("user_id")
    result = broker_manager.sync_all(db, user_id)
    return JSONResponse({"ok": True, **result})


@router.post("/api/trading/broker/deposit-address")
async def api_save_deposit_address(
    request: Request,
    db: Session = Depends(get_db),
    identity: dict = Depends(get_identity_ctx),
):
    """Save an encrypted deposit address for a broker+network pair."""
    body = await request.json()
    broker = body.get("broker", "")
    network = body.get("network", "")
    address = body.get("address", "").strip()
    if not broker or not network or not address:
        return JSONResponse({"ok": False, "message": "broker, network, and address are required."})

    user_id, new_token = _ensure_user_id(db, identity, request)
    key = f"{broker}_deposit_{network}"
    save_broker_credentials(db, user_id, key, {"address": address, "network": network})
    resp = JSONResponse({"ok": True, "saved": True, "address": address})
    if new_token:
        resp.set_cookie(
            DEVICE_COOKIE_NAME, new_token,
            max_age=60 * 60 * 24 * 365 * 2, httponly=True, samesite="lax",
            secure=request.url.scheme == "https",
        )
    return resp


@router.get("/api/trading/broker/deposit-address")
async def api_get_deposit_address(
    request: Request,
    db: Session = Depends(get_db),
    identity: dict = Depends(get_identity_ctx),
):
    """Retrieve a deposit address: check saved first, then auto-fetch from Coinbase API."""
    broker = request.query_params.get("broker", "")
    network = request.query_params.get("network", "")
    if not broker or not network:
        return JSONResponse({"ok": False, "message": "broker and network query params required."})

    uid = identity.get("user_id")
    if not uid:
        cookie = request.cookies.get(DEVICE_COOKIE_NAME)
        if cookie:
            dev = db.query(Device).filter(Device.token == cookie).first()
            if dev:
                uid = dev.user_id

    key = f"{broker}_deposit_{network}"
    address = ""
    if uid:
        creds = get_broker_credentials(db, uid, key)
        if creds:
            address = creds.get("address", "")

    if not address and broker == "coinbase":
        result = coinbase_service.get_usdc_deposit_address()
        if result.get("ok") and result.get("address"):
            address = result["address"]
            if uid:
                save_broker_credentials(db, uid, key, {"address": address, "network": network})

    return JSONResponse({"ok": True, "address": address})


@router.get("/api/trading/broker/best")
async def api_broker_best(ticker: str = ""):
    """Return the best broker for a given ticker and all available options."""
    if not ticker:
        return JSONResponse({"ok": False, "error": "ticker parameter required"})
    best = broker_manager.get_best_broker_for(ticker)
    available = broker_manager.get_available_brokers_for(ticker)
    dupes = broker_manager.check_duplicate_position(ticker)
    return JSONResponse({
        "ok": True,
        "ticker": ticker,
        "best_broker": best,
        "available": available,
        "existing_positions_on": dupes,
    })
