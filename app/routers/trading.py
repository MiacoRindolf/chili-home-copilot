"""Trading module routes: page + REST APIs for market data, indicators, watchlist, journal, AI."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from ..deps import get_db, get_identity_ctx
from ..logger import log_info, new_trace_id
from ..prompts import load_prompt
from ..services import trading_service as ts
from ..schemas.trading import (
    AnalyzeRequest,
    BacktestRequest,
    JournalCreate,
    ScanRequest,
    TradeClose,
    TradeCreate,
    WatchlistAdd,
)
from ..services import backtest_service as bt_svc

router = APIRouter(tags=["trading"])

_TRADING_PROMPT: str | None = None


def _get_trading_prompt() -> str:
    global _TRADING_PROMPT
    if _TRADING_PROMPT is None:
        _TRADING_PROMPT = load_prompt("trading_analyst")
    return _TRADING_PROMPT


# ── Page ────────────────────────────────────────────────────────────────

@router.get("/trading", response_class=HTMLResponse)
def trading_page(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    return request.app.state.templates.TemplateResponse(
        "trading.html",
        {
            "request": request,
            "title": "Trading",
            "is_guest": ctx["is_guest"],
            "user_name": ctx["user_name"],
        },
    )


# ── Market Data ─────────────────────────────────────────────────────────

@router.get("/api/trading/ohlcv")
def api_ohlcv(
    ticker: str = Query(...),
    interval: str = Query("1d"),
    period: str = Query("6mo"),
):
    data = ts.fetch_ohlcv(ticker, interval=interval, period=period)
    return JSONResponse({"ok": True, "ticker": ticker.upper(), "data": data})


@router.get("/api/trading/quote")
def api_quote(ticker: str = Query(...)):
    quote = ts.fetch_quote(ticker)
    if not quote:
        return JSONResponse({"ok": False, "error": "Ticker not found"}, status_code=404)
    return JSONResponse({"ok": True, **quote})


@router.get("/api/trading/search")
def api_search(q: str = Query(...), limit: int = Query(10)):
    results = ts.search_tickers(q, limit=limit)
    return JSONResponse({"ok": True, "results": results})


# ── Indicators ──────────────────────────────────────────────────────────

@router.get("/api/trading/indicators")
def api_indicators(
    ticker: str = Query(...),
    interval: str = Query("1d"),
    period: str = Query("6mo"),
    indicators: str = Query("rsi,macd,sma_20,ema_20,bbands"),
):
    ind_list = [i.strip() for i in indicators.split(",") if i.strip()]
    data = ts.compute_indicators(ticker, interval=interval, period=period, indicators=ind_list)
    return JSONResponse({"ok": True, "ticker": ticker.upper(), "indicators": data})


# ── Watchlist ───────────────────────────────────────────────────────────

@router.get("/api/trading/watchlist")
def api_get_watchlist(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    items = ts.get_watchlist(db, ctx["user_id"])
    return JSONResponse({"ok": True, "items": [
        {"id": w.id, "ticker": w.ticker, "added_at": w.added_at.isoformat()}
        for w in items
    ]})


@router.post("/api/trading/watchlist")
def api_add_watchlist(body: WatchlistAdd, request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    item = ts.add_to_watchlist(db, ctx["user_id"], body.ticker)
    return JSONResponse({"ok": True, "id": item.id, "ticker": item.ticker})


@router.delete("/api/trading/watchlist")
def api_remove_watchlist(
    ticker: str = Query(...),
    request: Request = None,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    removed = ts.remove_from_watchlist(db, ctx["user_id"], ticker)
    return JSONResponse({"ok": removed})


# ── Trades ──────────────────────────────────────────────────────────────

@router.get("/api/trading/trades")
def api_get_trades(
    request: Request,
    db: Session = Depends(get_db),
    status: str | None = Query(None),
):
    ctx = get_identity_ctx(request, db)
    trades = ts.get_trades(db, ctx["user_id"], status=status)
    return JSONResponse({"ok": True, "trades": [
        {
            "id": t.id, "ticker": t.ticker, "direction": t.direction,
            "entry_price": t.entry_price, "exit_price": t.exit_price,
            "quantity": t.quantity,
            "entry_date": t.entry_date.isoformat() if t.entry_date else None,
            "exit_date": t.exit_date.isoformat() if t.exit_date else None,
            "status": t.status, "pnl": t.pnl, "tags": t.tags, "notes": t.notes,
        }
        for t in trades
    ]})


@router.post("/api/trading/trades")
def api_create_trade(body: TradeCreate, request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    trade = ts.create_trade(
        db, ctx["user_id"],
        ticker=body.ticker.upper(),
        direction=body.direction,
        entry_price=body.entry_price,
        quantity=body.quantity,
        entry_date=body.entry_date,
        tags=body.tags,
        notes=body.notes,
    )
    return JSONResponse({"ok": True, "id": trade.id, "ticker": trade.ticker})


@router.post("/api/trading/trades/{trade_id}/close")
def api_close_trade(
    trade_id: int, body: TradeClose,
    background_tasks: BackgroundTasks = None,
    request: Request = None, db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    trade = ts.close_trade(
        db, trade_id, ctx["user_id"],
        exit_price=body.exit_price,
        exit_date=body.exit_date,
        notes=body.notes,
    )
    if not trade:
        return JSONResponse({"ok": False, "error": "Trade not found or already closed"}, status_code=404)

    # Trigger AI self-learning in background (non-blocking).
    if background_tasks:
        from ..db import SessionLocal

        def _learn(trade_id: int, user_id):
            learn_db = SessionLocal()
            try:
                t = learn_db.query(ts.Trade).filter(ts.Trade.id == trade_id).first()
                if t:
                    ts.analyze_closed_trade(learn_db, t)
            finally:
                learn_db.close()

        background_tasks.add_task(_learn, trade.id, ctx["user_id"])

    return JSONResponse({
        "ok": True, "id": trade.id, "pnl": trade.pnl, "status": trade.status,
    })


# ── Journal ─────────────────────────────────────────────────────────────

@router.get("/api/trading/journal")
def api_get_journal(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    entries = ts.get_journal(db, ctx["user_id"])
    return JSONResponse({"ok": True, "entries": [
        {
            "id": e.id, "trade_id": e.trade_id, "content": e.content,
            "indicator_snapshot": e.indicator_snapshot,
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ]})


@router.post("/api/trading/journal")
def api_add_journal(body: JournalCreate, request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    entry = ts.add_journal_entry(db, ctx["user_id"], body.content, trade_id=body.trade_id)
    return JSONResponse({"ok": True, "id": entry.id})


@router.get("/api/trading/journal/stats")
def api_trade_stats(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    stats = ts.get_trade_stats(db, ctx["user_id"])
    return JSONResponse({"ok": True, **stats})


# ── AI Analysis ─────────────────────────────────────────────────────────

@router.post("/api/trading/analyze")
def api_analyze(body: AnalyzeRequest, request: Request, db: Session = Depends(get_db)):
    """AI-powered analysis of a ticker using indicators + journal context."""
    ctx = get_identity_ctx(request, db)
    trace_id = new_trace_id()

    ai_context = ts.build_ai_context(db, ctx["user_id"], body.ticker, body.interval)

    user_msg = body.message or f"Analyze {body.ticker} on the {body.interval} timeframe. What's the setup?"

    messages = [
        {"role": "system", "content": f"{_get_trading_prompt()}\n\n---\n\n{ai_context}"},
        {"role": "user", "content": user_msg},
    ]

    try:
        from .. import openai_client
        result = openai_client.chat(
            messages=[{"role": "user", "content": user_msg}],
            system_prompt=f"{_get_trading_prompt()}\n\n---\n\n{ai_context}",
            trace_id=trace_id,
            user_message=user_msg,
        )
        reply = result.get("reply", "Could not generate analysis.")
    except Exception as e:
        log_info(trace_id, f"[trading] AI analysis error: {e}")
        reply = f"Analysis unavailable: {e}"

    return JSONResponse({"ok": True, "reply": reply, "ticker": body.ticker})


@router.get("/api/trading/insights")
def api_get_insights(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    insights = ts.get_insights(db, ctx["user_id"])
    return JSONResponse({"ok": True, "insights": [
        {
            "id": i.id,
            "pattern_description": i.pattern_description,
            "confidence": i.confidence,
            "evidence_count": i.evidence_count,
            "last_seen": i.last_seen.isoformat(),
        }
        for i in insights
    ]})


# ── Backtest ───────────────────────────────────────────────────────────

@router.get("/api/trading/backtest/strategies")
def api_list_strategies():
    return JSONResponse({"ok": True, "strategies": bt_svc.list_strategies()})


@router.post("/api/trading/backtest")
def api_run_backtest(
    body: BacktestRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    result = bt_svc.run_backtest(
        ticker=body.ticker, strategy_id=body.strategy, period=body.period,
        cash=body.cash, commission=body.commission,
    )
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)

    bt_svc.save_backtest(db, ctx["user_id"], result)
    return JSONResponse(result)


# ── Scanner ────────────────────────────────────────────────────────────

@router.post("/api/trading/scan")
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


@router.get("/api/trading/scan/results")
def api_scan_results(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    results = ts.get_latest_scan(db, ctx["user_id"])
    return JSONResponse({"ok": True, "results": results})


# ── Portfolio ──────────────────────────────────────────────────────────

@router.get("/api/trading/portfolio")
def api_portfolio(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    summary = ts.get_portfolio_summary(db, ctx["user_id"])
    return JSONResponse({"ok": True, **summary})


# ── Signals ────────────────────────────────────────────────────────────

@router.get("/api/trading/signals")
def api_signals(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    signals = ts.generate_signals(db, ctx["user_id"])
    return JSONResponse({"ok": True, "signals": signals})


# ── Background Learning ───────────────────────────────────────────────

@router.post("/api/trading/learn/snapshot")
def api_take_snapshots(
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    """Manually trigger market snapshots (also runs on schedule)."""
    ctx = get_identity_ctx(request, db)

    from ..db import SessionLocal

    def _bg_snapshot(user_id):
        sdb = SessionLocal()
        try:
            count = ts.take_all_snapshots(sdb, user_id)
            ts.backfill_future_returns(sdb)
            ts.mine_patterns(sdb, user_id)
        finally:
            sdb.close()

    background_tasks.add_task(_bg_snapshot, ctx["user_id"])
    return JSONResponse({"ok": True, "message": "Snapshot + learning started in background"})
