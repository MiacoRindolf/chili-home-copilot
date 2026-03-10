"""AI Brain / learning endpoints for the trading module."""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from ...deps import get_db, get_identity_ctx
from ...logger import log_info, new_trace_id
from ...prompts import load_prompt
from ...services import trading_service as ts
from ...services import trading_scheduler
from ...services import ticker_universe

router = APIRouter(tags=["trading-ai"])

_TRADING_PROMPT: str | None = None


def _get_trading_prompt() -> str:
    global _TRADING_PROMPT
    if _TRADING_PROMPT is None:
        _TRADING_PROMPT = load_prompt("trading_analyst")
    return _TRADING_PROMPT


# ── AI Analysis ────────────────────────────────────────────────────────

@router.get("/api/trading/brain/stats")
def api_brain_stats(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    stats = ts.get_brain_stats(db, ctx["user_id"])
    return JSONResponse({"ok": True, **stats})


@router.get("/api/trading/brain/confidence-history")
def api_confidence_history(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    history = ts.get_confidence_history(db, ctx["user_id"])
    return JSONResponse({"ok": True, "data": history})


@router.get("/api/trading/brain/activity")
def api_brain_activity(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    events = ts.get_learning_events(db, ctx["user_id"], limit=50)
    return JSONResponse({"ok": True, "events": [
        {
            "id": e.id,
            "event_type": e.event_type,
            "description": e.description,
            "confidence_before": e.confidence_before,
            "confidence_after": e.confidence_after,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]})


@router.get("/api/trading/brain/thesis")
def api_brain_thesis(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    thesis = ts.generate_market_thesis(db, ctx["user_id"])
    return JSONResponse({"ok": True, **thesis})


@router.post("/api/trading/learn/weekly-review")
def api_weekly_review(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    review = ts.weekly_performance_review(db, ctx["user_id"])
    return JSONResponse({"ok": True, "review": review or "No trades to review yet."})


@router.post("/api/trading/scan/full")
def api_full_scan(
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    status = ts.get_learning_status()

    if status["running"]:
        return JSONResponse({
            "ok": False,
            "message": "Learning cycle already in progress",
            "status": status,
        })

    from ...db import SessionLocal

    def _bg_full_learn(user_id):
        sdb = SessionLocal()
        try:
            ts.run_learning_cycle(sdb, user_id, full_universe=True)
        finally:
            sdb.close()

    background_tasks.add_task(_bg_full_learn, ctx["user_id"])
    return JSONResponse({
        "ok": True,
        "message": "Full market learning cycle started in background",
        "universe": ticker_universe.get_ticker_count(),
    })


@router.get("/api/trading/scan/status")
def api_scan_status():
    return JSONResponse({
        "ok": True,
        "scan": ts.get_scan_status(),
        "learning": ts.get_learning_status(),
        "scheduler": trading_scheduler.get_scheduler_info(),
    })


@router.get("/api/trading/universe")
def api_ticker_universe():
    counts = ticker_universe.get_ticker_count()
    return JSONResponse({"ok": True, **counts})


@router.post("/api/trading/universe/refresh")
def api_refresh_universe():
    counts = ticker_universe.refresh_ticker_cache()
    return JSONResponse({"ok": True, "message": "Ticker cache refreshed", **counts})


@router.post("/api/trading/learn/trigger")
def api_trigger_learning(background_tasks: BackgroundTasks, request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)

    if ts.get_learning_status()["running"]:
        return JSONResponse({"ok": False, "message": "Already running"})

    from ...db import SessionLocal

    def _bg(user_id):
        sdb = SessionLocal()
        try:
            ts.run_learning_cycle(sdb, user_id, full_universe=True)
        finally:
            sdb.close()

    background_tasks.add_task(_bg, ctx["user_id"])
    return JSONResponse({"ok": True, "message": "Learning cycle triggered"})


@router.post("/api/trading/learn/deep-study")
def api_deep_study(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    result = ts.deep_study(db, ctx["user_id"])
    return JSONResponse(result)


@router.get("/api/trading/learn/patterns")
def api_learned_patterns(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    from ...models.trading import TradingInsight
    all_insights = db.query(TradingInsight).filter(
        TradingInsight.user_id == ctx["user_id"],
    ).order_by(TradingInsight.confidence.desc()).limit(50).all()

    active = []
    demoted = []
    for ins in all_insights:
        desc = ins.pattern_description or ""
        desc_lower = desc.lower()
        if any(w in desc_lower for w in ("bullish", "oversold", "buy", "uptrend", "gained", "above")):
            signal_type = "bullish"
        elif any(w in desc_lower for w in ("bearish", "overbought", "sell", "downtrend", "lost", "below")):
            signal_type = "bearish"
        else:
            signal_type = "neutral"
        win_match = re.search(r"(\d+(?:\.\d+)?)%\s*win", desc)
        ret_match = re.search(r"([+-]?\d+(?:\.\d+)?)%\s*(?:avg|average|return)", desc)
        ticker_match = re.findall(r"\b([A-Z]{1,5}(?:-USD)?)\b", desc)
        tickers_found = [t for t in ticker_match if len(t) >= 2 and t not in {
            "RSI", "MACD", "EMA", "SMA", "ADX", "ATR", "AND", "THE", "FOR",
            "OBV", "MFI", "CCI", "SAR", "USD", "AVG", "NET", "LOW", "HIGH",
        }][:3]

        entry = {
            "id": ins.id,
            "pattern": desc,
            "confidence": round(ins.confidence * 100, 1),
            "evidence_count": ins.evidence_count,
            "active": ins.active,
            "signal_type": signal_type,
            "win_rate": float(win_match.group(1)) if win_match else None,
            "avg_return": float(ret_match.group(1)) if ret_match else None,
            "example_tickers": tickers_found,
            "created_at": ins.created_at.isoformat(),
            "last_seen": ins.last_seen.isoformat() if ins.last_seen else None,
        }
        if ins.active:
            active.append(entry)
        else:
            demoted.append(entry)

    return JSONResponse({
        "ok": True,
        "active": active,
        "demoted": demoted,
        "total_active": len(active),
        "total_demoted": len(demoted),
    })
