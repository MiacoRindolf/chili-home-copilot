"""Broker (Robinhood) integration endpoints."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ...deps import get_db, get_identity_ctx
from ...services import broker_service

router = APIRouter(tags=["trading-broker"])


@router.get("/api/trading/broker/status")
async def api_broker_status():
    return JSONResponse({"ok": True, **broker_service.get_connection_status()})


@router.post("/api/trading/broker/connect")
async def api_broker_connect():
    result = broker_service.login_step1_sms()
    status = broker_service.get_connection_status()
    return JSONResponse({"ok": result["status"] == "connected", **result, **status})


@router.post("/api/trading/broker/verify")
async def api_broker_verify(request: Request):
    body = await request.json()
    code = body.get("code", "")
    result = broker_service.login_step2_verify(code)
    status = broker_service.get_connection_status()
    return JSONResponse({"ok": result["status"] == "connected", **result, **status})


@router.get("/api/trading/broker/poll")
async def api_broker_poll():
    result = broker_service.poll_app_approval()
    status = broker_service.get_connection_status()
    return JSONResponse({"ok": result["status"] == "approved", **result, **status})


@router.get("/api/trading/broker/positions")
async def api_broker_positions():
    if not broker_service.is_connected():
        return JSONResponse({"ok": False, "error": "Not connected to Robinhood", "positions": []})
    positions = broker_service.get_positions()
    crypto = broker_service.get_crypto_positions()
    return JSONResponse({"ok": True, "positions": positions + crypto})


@router.get("/api/trading/broker/portfolio")
async def api_broker_portfolio():
    if not broker_service.is_connected():
        return JSONResponse({"ok": False, "error": "Not connected to Robinhood", "portfolio": {}})
    portfolio = broker_service.get_portfolio()
    return JSONResponse({"ok": True, "portfolio": portfolio})


@router.post("/api/trading/broker/sync")
async def api_broker_sync(
    db: Session = Depends(get_db),
    identity: dict = Depends(get_identity_ctx),
):
    """Sync trades tab with Robinhood: orders, positions, and manual cleanup."""
    if not broker_service.is_connected():
        return JSONResponse({"ok": False, "error": "Not connected to Robinhood"})
    user_id = identity.get("user_id")
    order_result = broker_service.sync_orders_to_db(db, user_id)
    pos_result = broker_service.sync_positions_to_db(db, user_id)
    live_tickers = pos_result.pop("_live_tickers", set())
    manual_result = broker_service.cleanup_manual_trades(db, user_id, live_tickers)
    return JSONResponse({
        "ok": True,
        "orders": order_result,
        "positions": pos_result,
        "manual": manual_result,
    })
