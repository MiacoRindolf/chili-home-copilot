"""Scanners, screener, top picks, portfolio, and signals endpoints.

Covers all discovery / screening surfaces that were previously in the main
trading router.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from ...deps import get_db, get_identity_ctx
from ...services import trading_service as ts
from ...schemas.trading import ScanRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trading", tags=["trading-scanning"])


# ── Crypto Breakout Scanner ──────────────────────────────────────────────────

@router.get("/crypto-breakouts")
def api_crypto_breakouts(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    refresh: bool = Query(False),
):
    """Return cached crypto breakout scan or trigger a fresh scan."""
    from ...services.trading.public_api import run_crypto_breakout_scan, get_crypto_breakout_cache

    cache = get_crypto_breakout_cache()
    has_data = cache.get("age_seconds") is not None

    if refresh:
        background_tasks.add_task(run_crypto_breakout_scan, 20)
        if has_data:
            return JSONResponse({"ok": True, "refreshing": True, **cache})
        return JSONResponse({"ok": True, "refreshing": True, "results": [], "message": "Scan started"})

    if has_data:
        return JSONResponse({"ok": True, **cache})

    background_tasks.add_task(run_crypto_breakout_scan, 20)
    return JSONResponse({
        "ok": True, "warming_up": True,
        "results": [], "message": "Crypto scan started — results will appear shortly",
    })


@router.post("/crypto-breakouts/scan")
def api_trigger_crypto_scan(
    background_tasks: BackgroundTasks,
):
    """Manually trigger a crypto breakout scan."""
    from ...services.trading.public_api import run_crypto_breakout_scan
    background_tasks.add_task(run_crypto_breakout_scan, 20)
    return JSONResponse({"ok": True, "message": "Crypto breakout scan started"})


# ── Scanner ──────────────────────────────────────────────────────────────────

@router.post("/scan")
def api_run_scan(
    body: ScanRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    """Run a full market scan. This is heavy, so we return top results and continue in BG."""
    ctx = get_identity_ctx(request, db)
    results = ts.run_scan(db, ctx["user_id"], tickers=body.tickers)
    return JSONResponse({"ok": True, "count": len(results), "results": results[:20]})


@router.get("/scan/results")
def api_scan_results(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    results = ts.get_latest_scan(db, ctx["user_id"])
    return JSONResponse({"ok": True, "results": results})


# ── Custom Screener ──────────────────────────────────────────────────────────

@router.get("/screener/presets")
def api_screener_presets():
    """List all available preset screening patterns."""
    presets = []
    for sid, info in ts.PRESET_SCREENS.items():
        presets.append({
            "id": sid,
            "name": info["name"],
            "description": info["description"],
            "scan_type": info.get("scan_type", "swing"),
            "conditions": len(info.get("conditions", [])),
            "confirmations": len(info.get("confirmations", [])),
        })
    return JSONResponse({"ok": True, "presets": presets})


class ScreenRequest(BaseModel):
    screen_id: Optional[str] = None
    conditions: Optional[list[dict]] = None


@router.post("/screener/run")
def api_run_screener(body: ScreenRequest, db: Session = Depends(get_db)):
    """Run a preset or custom screen across the full ticker universe."""
    result = ts.run_custom_screen(
        screen_id=body.screen_id,
        conditions=body.conditions,
        db=db,
    )
    return JSONResponse(result)


# ── Day-Trade & Breakout Scans ───────────────────────────────────────────────

@router.get("/scan/progress")
def api_scan_progress():
    """Lightweight poll endpoint for live scan progress."""
    return JSONResponse(ts.get_intraday_scan_progress())


@router.post("/scan/daytrade")
def api_run_daytrade_scan(background_tasks: BackgroundTasks):
    """Return cached day-trade results if fresh, else kick off BG scan and return fast."""
    from ...services.trading.scanner import (
        get_daytrade_cache, run_daytrade_scan, _brain_meta,
    )
    cache = get_daytrade_cache()
    age = cache.get("age_seconds")
    has_data = age is not None

    if has_data and age < 600:
        return JSONResponse({
            "ok": True, "scan_type": "day_trade", "cached": True,
            "matches": len(cache["results"]), "results": cache["results"][:30],
            "brain": _brain_meta(),
        })
    if has_data:
        background_tasks.add_task(run_daytrade_scan, 30)
        return JSONResponse({
            "ok": True, "scan_type": "day_trade", "cached": True, "refreshing": True,
            "matches": len(cache["results"]), "results": cache["results"][:30],
            "brain": _brain_meta(),
        })
    background_tasks.add_task(run_daytrade_scan, 30)
    return JSONResponse({
        "ok": True, "scan_type": "day_trade", "warming_up": True,
        "matches": 0, "results": [],
        "message": "Scan started — results will appear shortly",
    })


@router.post("/scan/breakouts")
def api_run_breakout_scan(background_tasks: BackgroundTasks):
    """Return cached breakout results if fresh, else kick off BG scan and return fast."""
    from ...services.trading.scanner import (
        get_breakout_cache, run_breakout_scan, run_crypto_breakout_scan, _brain_meta,
    )
    cache = get_breakout_cache()
    age = cache.get("age_seconds")
    has_data = age is not None

    if has_data and age < 600:
        return JSONResponse({
            "ok": True, "scan_type": "breakout", "cached": True,
            "matches": len(cache["results"]), "results": cache["results"][:30],
            "candidates_scanned": cache.get("total_scanned", 0),
            "total_sourced": cache.get("total_scanned", 0),
            "brain": _brain_meta(),
        })
    if has_data:
        background_tasks.add_task(run_breakout_scan, 30)
        background_tasks.add_task(run_crypto_breakout_scan, 20)
        return JSONResponse({
            "ok": True, "scan_type": "breakout", "cached": True, "refreshing": True,
            "matches": len(cache["results"]), "results": cache["results"][:30],
            "candidates_scanned": cache.get("total_scanned", 0),
            "total_sourced": cache.get("total_scanned", 0),
            "brain": _brain_meta(),
        })
    background_tasks.add_task(run_breakout_scan, 30)
    background_tasks.add_task(run_crypto_breakout_scan, 20)
    return JSONResponse({
        "ok": True, "scan_type": "breakout", "warming_up": True,
        "matches": 0, "results": [],
        "message": "Scan started — results will appear shortly",
    })


@router.get("/scan/momentum")
@router.post("/scan/momentum")
def api_run_momentum_scan():
    """Active momentum scanner — finds top intraday setups with strict filters."""
    from ...services.trading.public_api import run_momentum_scanner
    result = run_momentum_scanner()
    return JSONResponse(result)


# ── Portfolio ────────────────────────────────────────────────────────────────

@router.get("/portfolio")
def api_portfolio(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    summary = ts.get_portfolio_summary(db, ctx["user_id"])
    return JSONResponse({"ok": True, **summary})


# ── Signals & Top Picks ──────────────────────────────────────────────────────

@router.get("/signals")
def api_signals(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    signals = ts.generate_signals(db, ctx["user_id"])
    return JSONResponse({"ok": True, "signals": signals})


@router.get("/top-picks")
def api_top_picks(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    picks = ts.generate_top_picks(db, ctx["user_id"])
    freshness = ts.get_top_picks_freshness(stale_threshold_seconds=600)
    return JSONResponse({
        "ok": True,
        "picks": picks,
        "as_of": freshness["as_of"],
        "age_seconds": freshness["age_seconds"],
        "is_stale": freshness["is_stale"],
    })


@router.get("/brain/tickers")
def api_brain_tickers(db: Session = Depends(get_db)):
    """Return the brain's top known tickers — crypto and stocks — for UI population."""
    from ...models.trading import MarketSnapshot

    top_crypto = (
        db.query(MarketSnapshot.ticker, func.max(MarketSnapshot.predicted_score).label("best"))
        .filter(MarketSnapshot.ticker.like("%-USD"))
        .group_by(MarketSnapshot.ticker)
        .order_by(desc("best"))
        .limit(20)
        .all()
    )
    top_stocks = (
        db.query(MarketSnapshot.ticker, func.max(MarketSnapshot.predicted_score).label("best"))
        .filter(~MarketSnapshot.ticker.like("%-USD"))
        .group_by(MarketSnapshot.ticker)
        .order_by(desc("best"))
        .limit(20)
        .all()
    )
    return JSONResponse({
        "ok": True,
        "crypto": [{"ticker": r[0], "score": round(float(r[1] or 0), 1)} for r in top_crypto],
        "stocks": [{"ticker": r[0], "score": round(float(r[1] or 0), 1)} for r in top_stocks],
    })
