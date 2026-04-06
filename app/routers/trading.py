"""Trading module routes: page + REST APIs for market data, indicators, watchlist, journal, AI."""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime
from typing import Any, Literal, cast

logger = logging.getLogger(__name__)

import asyncio

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..deps import get_db, get_identity_ctx
from ..logger import log_info, new_trace_id
from ..prompts import load_prompt
from ..services import trading_service as ts
from ..services.trading.scanner import validate_live_prices as _smart_pick_validate_live_prices
from ..services import trading_scheduler
from ..services import ticker_universe
from ..services import broker_service
from ..services import broker_manager
from .trading_sub import ai_router, broker_router, data_provider_router, inspect_router, web3_router
from ..schemas.trading import (
    AnalyzeRequest,
    BacktestRequest,
    PatternBacktestRequest,
    JournalCreate,
    PickRecheckRequest,
    ScanRequest,
    SmartPickRequest,
    TradeClose,
    TradeCreate,
    TradeSell,
    WatchlistAdd,
)
from ..services import backtest_service as bt_svc

router = APIRouter(tags=["trading"])
router.include_router(ai_router)
router.include_router(inspect_router)
router.include_router(broker_router)
router.include_router(data_provider_router)
router.include_router(web3_router)

# Chart/indicators: Massive only. Quotes (header, watchlist, WS poll) use
# ``allow_provider_fallback=None`` → ``settings.market_data_allow_provider_fallback`` (default True)
# so Polygon/yfinance fill when Massive has no snapshot (common for some OTC/foreign listings).
_TRADING_UI_ALLOW_PROVIDER_FALLBACK = False

_TRADING_PROMPT: str | None = None
_TRADING_PROMPT_MTIME: float = 0.0


def _get_trading_prompt() -> str:
    """Load trading analyst prompt, auto-reloading when the file changes."""
    global _TRADING_PROMPT, _TRADING_PROMPT_MTIME
    from pathlib import Path
    prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "trading_analyst.txt"
    try:
        current_mtime = prompt_path.stat().st_mtime
    except OSError:
        current_mtime = 0.0
    if _TRADING_PROMPT is None or current_mtime != _TRADING_PROMPT_MTIME:
        _TRADING_PROMPT = load_prompt("trading_analyst")
        _TRADING_PROMPT_MTIME = current_mtime
    return _TRADING_PROMPT


def _get_proposal_reminder(db: Session, ticker: str, user_id: int | None) -> str:
    """Build a user-message-level reminder about active CHILI proposals.

    Injected into the user message (not just system context) so the LLM
    cannot overlook it — LLMs are much more likely to address content in
    the user message than in a long system prompt.
    """
    from datetime import datetime, timedelta
    from ..models.trading import StrategyProposal

    ticker_up = ticker.upper()
    cutoff = datetime.utcnow() - timedelta(hours=24)
    proposals = db.query(StrategyProposal).filter(
        StrategyProposal.ticker == ticker_up,
        StrategyProposal.status.in_(["pending", "approved", "executed"]),
        StrategyProposal.proposed_at >= cutoff,
    ).order_by(StrategyProposal.proposed_at.desc()).limit(3).all()
    if not proposals:
        return ""

    lines = [
        "IMPORTANT — CHILI already recommended this ticker to me. "
        "You MUST start your analysis by acknowledging this and explain "
        "whether you agree or disagree and WHY:"
    ]
    for p in proposals:
        score_parts = []
        if p.scan_score is not None:
            score_parts.append(f"Scanner {p.scan_score:.1f}/10")
        if p.brain_score is not None:
            score_parts.append(f"Brain {p.brain_score:.1f}")
        if p.ml_probability is not None:
            score_parts.append(f"ML {p.ml_probability:.1%}")
        score_str = f", Scores: {', '.join(score_parts)}" if score_parts else ""
        lines.append(
            f"  - {p.direction.upper()} @ ${p.entry_price:.2f}, "
            f"Stop ${p.stop_loss:.2f}, Target ${p.take_profit:.2f}, "
            f"R:R {p.risk_reward_ratio:.1f}:1, "
            f"Confidence {p.confidence:.0f}%{score_str}, Status: {p.status}"
        )
        if p.signals_json:
            try:
                import json as _json
                _sigs = _json.loads(p.signals_json) if isinstance(p.signals_json, str) else p.signals_json
                if isinstance(_sigs, list) and _sigs:
                    lines.append(f"    Signals: {'; '.join(str(s) for s in _sigs[:5])}")
            except Exception:
                pass
        if p.thesis:
            lines.append(f"    Thesis: {p.thesis[:200]}")
    return "\n".join(lines)


def _json_safe(value: Any) -> Any:
    """Recursively replace non-finite floats so JSONResponse never crashes."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_json_safe(v) for v in value)
    return value


# ── Page ────────────────────────────────────────────────────────────────


def _trading_page_response(
    request: Request,
    db: Session,
    *,
    template_name: str,
    page_title: str,
):
    ctx = get_identity_ctx(request, db)
    # Brain Worker handles all learning cycles - no auto-trigger on page load

    avatar_url = ""
    if not ctx["is_guest"] and ctx["user_id"]:
        from ..models.core import User as _User
        u = db.query(_User).filter(_User.id == ctx["user_id"]).first()
        if u:
            avatar_url = u.avatar_url or ""

    from ..config import settings as _s
    google_configured = bool(_s.google_client_id and _s.google_client_secret)

    return request.app.state.templates.TemplateResponse(
        request,
        template_name,
        {
            "title": page_title,
            "is_guest": ctx["is_guest"],
            "user_name": ctx["user_name"],
            "avatar_url": avatar_url,
            "google_configured": google_configured,
        },
    )


@router.get("/trading", response_class=HTMLResponse)
def trading_page(request: Request, db: Session = Depends(get_db)):
    return _trading_page_response(
        request, db, template_name="trading.html", page_title="Trading"
    )


@router.get("/trading-backup", response_class=HTMLResponse)
def trading_backup_page(request: Request, db: Session = Depends(get_db)):
    return _trading_page_response(
        request,
        db,
        template_name="trading_backup.html",
        page_title="Trading (backup)",
    )


# ── Market Data ─────────────────────────────────────────────────────────

@router.get("/api/trading/ohlcv")
def api_ohlcv(
    ticker: str = Query(...),
    interval: str = Query("1d"),
    period: str = Query("6mo"),
):
    try:
        data = ts.fetch_ohlcv(
            ticker,
            interval=interval,
            period=period,
            allow_provider_fallback=_TRADING_UI_ALLOW_PROVIDER_FALLBACK,
        )
    except Exception:
        data = []
    return JSONResponse({"ok": True, "ticker": ticker.upper(), "data": data})


@router.get("/api/trading/quote")
def api_quote(ticker: str = Query(...)):
    quote = ts.fetch_quote(
        ticker, allow_provider_fallback=None,
    )
    if not quote:
        return JSONResponse({"ok": True, "ticker": ticker.upper(), "price": None, "change": None, "change_pct": None})
    return JSONResponse({"ok": True, **quote})


@router.get("/api/trading/quotes/batch")
def api_quotes_batch(tickers: str = Query(..., description="Comma-separated ticker list")):
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()][:50]
    results = ts.fetch_quotes_batch(
        ticker_list, allow_provider_fallback=None,
    )
    return JSONResponse({"ok": True, "quotes": results})


@router.get("/api/trading/search")
def api_search(q: str = Query(...), limit: int = Query(10)):
    results = ts.search_tickers(q, limit=limit)
    return JSONResponse({"ok": True, "results": results})


@router.get("/api/trading/ticker-info")
def api_ticker_info(ticker: str = Query(...)):
    """Compact metadata for the ticker detail strip (name, sector, mcap, P/E, description)."""
    info = ts.get_ticker_info(ticker)
    if not info:
        return JSONResponse({"ok": True, "ticker": ticker.upper(), "info": None})
    return JSONResponse({"ok": True, "ticker": ticker.upper(), "info": info})


@router.get("/api/trading/news")
def api_ticker_news(ticker: str = Query(...), limit: int = Query(5)):
    """News articles related to the selected ticker."""
    news = ts.get_ticker_news(ticker, limit=limit)
    return JSONResponse({"ok": True, "ticker": ticker.upper(), "news": news})


# ── Indicators ──────────────────────────────────────────────────────────

@router.get("/api/trading/indicators")
def api_indicators(
    ticker: str = Query(...),
    interval: str = Query("1d"),
    period: str = Query("6mo"),
    indicators: str = Query("rsi,macd,sma_20,ema_20,bbands"),
):
    ind_list = [i.strip() for i in indicators.split(",") if i.strip()]
    data = ts.compute_indicators(
        ticker,
        interval=interval,
        period=period,
        indicators=ind_list,
        allow_provider_fallback=_TRADING_UI_ALLOW_PROVIDER_FALLBACK,
    )
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
            "broker_source": t.broker_source,
            "broker_status": t.broker_status,
            "broker_order_id": t.broker_order_id,
            "filled_at": t.filled_at.isoformat() if t.filled_at else None,
            "avg_fill_price": t.avg_fill_price,
            "tca_reference_entry_price": t.tca_reference_entry_price,
            "tca_entry_slippage_bps": t.tca_entry_slippage_bps,
            "tca_reference_exit_price": t.tca_reference_exit_price,
            "tca_exit_slippage_bps": t.tca_exit_slippage_bps,
            "strategy_proposal_id": t.strategy_proposal_id,
            "scan_pattern_id": t.scan_pattern_id,
        }
        for t in trades
    ]})


@router.get("/api/trading/tca/summary")
def api_tca_summary(
    request: Request,
    db: Session = Depends(get_db),
    days: int = Query(90, ge=1, le=730),
    limit: int = Query(50, ge=1, le=200),
):
    """Rolling TCA aggregates: mean entry slippage (bps) vs proposal/reference by ticker."""
    from ..services.trading.tca_service import tca_summary_by_ticker

    ctx = get_identity_ctx(request, db)
    out = tca_summary_by_ticker(db, ctx["user_id"], days=days, limit=limit)
    return JSONResponse(_json_safe(out))


@router.get("/api/trading/attribution/live-vs-research")
def api_attribution_live_vs_research(
    request: Request,
    db: Session = Depends(get_db),
    days: int = Query(90, ge=1, le=730),
    limit: int = Query(50, ge=1, le=200),
):
    """Closed trades with ``scan_pattern_id`` vs pattern OOS / research stats."""
    from ..services.trading.attribution_service import live_vs_research_by_pattern

    ctx = get_identity_ctx(request, db)
    out = live_vs_research_by_pattern(db, ctx["user_id"], days=days, limit=limit)
    return JSONResponse(_json_safe(out))


@router.post("/api/trading/trades")
def api_create_trade(
    body: TradeCreate,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
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

    from ..db import SessionLocal

    def _auto_journal(trade_id: int):
        sdb = SessionLocal()
        try:
            t = sdb.query(ts.Trade).filter(ts.Trade.id == trade_id).first()
            if t:
                ts.auto_journal_trade_open(sdb, t)
        finally:
            sdb.close()

    background_tasks.add_task(_auto_journal, trade.id)
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
        reference_exit_price=body.reference_exit_price,
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


@router.delete("/api/trading/trades/{trade_id}")
@router.post("/api/trading/trades/{trade_id}/delete")
def api_delete_trade(
    trade_id: int,
    request: Request = None,
    db: Session = Depends(get_db),
):
    """Delete a trade (e.g. wrong or duplicate entry). Removes the trade and clears journal refs."""
    ctx = get_identity_ctx(request, db)
    err = ts.delete_trade(db, trade_id, ctx["user_id"])
    if err == "not_found":
        return JSONResponse({"ok": False, "error": "Trade not found"}, status_code=404)
    if err == "forbidden":
        return JSONResponse({"ok": False, "error": "You don't have permission to delete this trade"}, status_code=403)
    return JSONResponse({"ok": True, "id": trade_id})


@router.post("/api/trading/trades/{trade_id}/sell")
def api_sell_trade(
    trade_id: int,
    body: TradeSell,
    request: Request = None,
    db: Session = Depends(get_db),
):
    """Partial or full sell of an open position, routed through Robinhood when connected."""
    from ..models.trading import Trade
    from datetime import datetime

    ctx = get_identity_ctx(request, db)
    trade = db.query(Trade).filter(
        Trade.id == trade_id, Trade.user_id == ctx["user_id"],
    ).first()
    if not trade:
        return JSONResponse({"ok": False, "error": "Trade not found"}, status_code=404)
    if trade.status != "open":
        return JSONResponse({"ok": False, "error": f"Trade is {trade.status}, not open"}, status_code=400)
    if body.quantity > trade.quantity:
        return JSONResponse({"ok": False, "error": f"Cannot sell {body.quantity}, only {trade.quantity} held"}, status_code=400)

    is_full_exit = abs(body.quantity - trade.quantity) < 0.0001

    if trade.broker_source in ("robinhood", "coinbase"):
        broker_connected = (
            (trade.broker_source == "robinhood" and broker_service.is_connected()) or
            (trade.broker_source == "coinbase" and broker_manager.is_any_connected())
        )
        if broker_connected:
            order_type = "limit" if body.limit_price else "market"
            result = broker_manager.place_sell_order(
                ticker=trade.ticker,
                quantity=body.quantity,
                order_type=order_type,
                limit_price=body.limit_price,
                broker=trade.broker_source,
            )
            if not result.get("ok"):
                return JSONResponse({"ok": False, "error": result.get("error", "Sell failed")}, status_code=500)

            broker_state = (result.get("state") or "queued").lower()
            order_id = result.get("order_id", "")
            src = result.get("broker", trade.broker_source)

            if is_full_exit:
                if broker_state == "filled":
                    trade.status = "closed"
                    trade.exit_price = body.limit_price or trade.entry_price
                    trade.exit_date = datetime.utcnow()
                    trade.pnl = round((trade.exit_price - trade.entry_price) * trade.quantity, 2)
                    try:
                        from ..services.trading.tca_service import (
                            apply_tca_on_trade_close,
                            resolve_exit_reference_price,
                        )

                        trade.tca_reference_exit_price = resolve_exit_reference_price(
                            trade.ticker,
                            explicit=body.limit_price,
                            fill_fallback=float(trade.exit_price),
                        )
                        apply_tca_on_trade_close(trade)
                    except Exception:
                        pass
                else:
                    trade.notes = (trade.notes or "") + f"\nSell order placed (full exit), {src} order {order_id} ({broker_state})"
            else:
                remaining = round(trade.quantity - body.quantity, 6)
                exit_price = body.limit_price or trade.entry_price
                realized_pnl = round((exit_price - trade.entry_price) * body.quantity, 2)
                trade.quantity = remaining
                trade.notes = (
                    (trade.notes or "")
                    + f"\nPartial sell: {body.quantity} shares"
                    + (f" @ ${body.limit_price}" if body.limit_price else " (market)")
                    + f", {src} order {order_id} ({broker_state}), realized ~${realized_pnl}"
                )

            db.commit()
            return JSONResponse({
                "ok": True,
                "trade_id": trade.id,
                "sold_qty": body.quantity,
                "remaining_qty": round(trade.quantity, 6),
                "broker_state": broker_state,
                "order_id": order_id,
                "broker": src,
                "status": trade.status,
            })

    # Manual / paper trade — immediate simulated close
    exit_price = body.limit_price or trade.entry_price
    if is_full_exit:
        trade.status = "closed"
        trade.exit_price = exit_price
        trade.exit_date = datetime.utcnow()
        trade.pnl = round((exit_price - trade.entry_price) * trade.quantity, 2)
        try:
            from ..services.trading.tca_service import (
                apply_tca_on_trade_close,
                resolve_exit_reference_price,
            )

            trade.tca_reference_exit_price = resolve_exit_reference_price(
                trade.ticker,
                explicit=body.limit_price,
                fill_fallback=float(exit_price),
            )
            apply_tca_on_trade_close(trade)
        except Exception:
            pass
    else:
        realized_pnl = round((exit_price - trade.entry_price) * body.quantity, 2)
        trade.quantity = round(trade.quantity - body.quantity, 6)
        trade.notes = (
            (trade.notes or "")
            + f"\nPartial close: {body.quantity} @ ${exit_price}, realized ${realized_pnl}"
        )

    db.commit()
    return JSONResponse({
        "ok": True,
        "trade_id": trade.id,
        "sold_qty": body.quantity,
        "remaining_qty": round(trade.quantity, 6),
        "status": trade.status,
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
    by_source = ts.get_trade_stats_by_source(db, ctx["user_id"])
    all_stats = by_source["all"]
    return JSONResponse({
        "ok": True,
        **all_stats,
        "by_source": by_source,
    })


def _api_stats_calendar_impl(
    request: Request,
    db: Session,
    year: int,
    month: int,
):
    from datetime import datetime
    ctx = get_identity_ctx(request, db)
    start = datetime(year, month, 1, 0, 0, 0)
    if month == 12:
        end = datetime(year + 1, 1, 1, 0, 0, 0)
    else:
        end = datetime(year, month + 1, 1, 0, 0, 0)
    days = ts.get_daily_pnl(db, ctx["user_id"], start, end)
    return JSONResponse({"ok": True, "days": days, "year": year, "month": month})


@router.get("/api/trading/stats/calendar")
@router.get("/api/trading/journal/calendar")
def api_stats_calendar(
    request: Request,
    db: Session = Depends(get_db),
    year: int = Query(...),
    month: int = Query(...),
):
    """Daily P&L for a given month. Returns { ok, days: [{ date, trade_count, pnl, trades }] }."""
    return _api_stats_calendar_impl(request, db, year, month)


# ── AI Analysis ─────────────────────────────────────────────────────────

@router.post("/api/trading/analyze")
def api_analyze(body: AnalyzeRequest, request: Request, db: Session = Depends(get_db)):
    """AI-powered analysis of a ticker using indicators + journal context."""
    ctx = get_identity_ctx(request, db)
    trace_id = new_trace_id()

    ai_context = ts.build_ai_context(db, ctx["user_id"], body.ticker, body.interval)

    proposal_reminder = _get_proposal_reminder(db, body.ticker, ctx.get("user_id"))
    user_msg = body.message or f"Analyze {body.ticker} on the {body.interval} timeframe. Give me a clear verdict: should I buy, sell, or hold? Include exact entry price, stop-loss, targets, hold duration, and confidence level."
    if proposal_reminder:
        user_msg += "\n\n" + proposal_reminder

    messages = []
    if body.history:
        for h in body.history[-10:]:
            if isinstance(h, dict) and h.get("role") and h.get("content"):
                messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_msg})

    try:
        from .. import openai_client
        result = openai_client.chat(
            messages=messages,
            system_prompt=f"{_get_trading_prompt()}\n\n---\n\n{ai_context}",
            trace_id=trace_id,
            user_message=user_msg,
            max_tokens=2048,
        )
        reply = result.get("reply", "Could not generate analysis.")
    except Exception as e:
        log_info(trace_id, f"[trading] AI analysis error: {e}")
        reply = f"Analysis unavailable: {e}"

    return JSONResponse({"ok": True, "reply": reply, "ticker": body.ticker})


@router.get("/api/trading/analyze/stream")
def api_analyze_stream(
    request: Request,
    db: Session = Depends(get_db),
    ticker: str = Query("AAPL"),
    interval: str = Query("1d"),
    message: str = Query(""),
    history: str = Query("[]"),
):
    """SSE streaming endpoint for trading AI analysis."""
    ctx = get_identity_ctx(request, db)
    trace_id = new_trace_id()

    ai_context = ts.build_ai_context(db, ctx["user_id"], ticker, interval)

    proposal_reminder = _get_proposal_reminder(db, ticker, ctx.get("user_id"))
    user_msg = message or (
        f"Analyze {ticker} on the {interval} timeframe. "
        "Give me a clear verdict: should I buy, sell, or hold? "
        "Include exact entry price, stop-loss, targets, hold duration, and confidence level."
    )
    if proposal_reminder:
        user_msg += "\n\n" + proposal_reminder

    try:
        hist_list = json.loads(history) if history else []
    except (json.JSONDecodeError, TypeError):
        hist_list = []

    messages = []
    for h in hist_list[-10:]:
        if isinstance(h, dict) and h.get("role") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_msg})

    system_prompt = f"{_get_trading_prompt()}\n\n---\n\n{ai_context}"

    def _generate():
        from .. import openai_client
        try:
            _stream_had_token = False
            for tok, model in openai_client.chat_stream(
                messages=messages,
                system_prompt=system_prompt,
                trace_id=trace_id,
                user_message=user_msg,
                max_tokens=2048,
            ):
                _stream_had_token = True
                yield f"data: {json.dumps({'token': tok})}\n\n"
            if not _stream_had_token:
                _empty = (
                    "*No analysis text was returned.* The AI stream completed without content. "
                    "Please try again — if it keeps happening, check provider status or try a shorter question."
                )
                yield f"data: {json.dumps({'token': _empty})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            log_info(trace_id, f"[trading] stream error: {e}")
            err_msg = f"\n\n*Analysis error: {e}*"
            yield f"data: {json.dumps({'token': err_msg})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/trading/smart-pick")
def api_smart_pick(body: SmartPickRequest, request: Request, db: Session = Depends(get_db)):
    """Scan the entire market and return AI's top trade recommendations with exact levels."""
    ctx = get_identity_ctx(request, db)
    result = ts.smart_pick(
        db, ctx["user_id"],
        message=body.message,
        budget=body.budget,
        risk_tolerance=body.risk_tolerance,
    )
    return JSONResponse(result)


@router.get("/api/trading/smart-pick/stream")
def api_smart_pick_stream(
    request: Request,
    db: Session = Depends(get_db),
    risk_tolerance: str = Query("medium"),
    budget: float | None = Query(None),
):
    """SSE streaming endpoint for Smart Pick recommendations."""
    ctx = get_identity_ctx(request, db)
    trace_id = new_trace_id()

    # Build or reuse cached Smart Pick context, then refresh prices live.
    sp_ctx = ts.smart_pick_context(
        db,
        ctx["user_id"],
        budget=budget,
        risk_tolerance=risk_tolerance,
    )
    raw_top_picks = [dict(p) for p in sp_ctx.get("top_picks") or []]
    total_scanned = sp_ctx.get("total_scanned", 0)

    if not raw_top_picks:
        # Nothing qualified – stream a single explanatory message.
        def _empty_stream():
            msg = (
                f"I scanned {total_scanned:,} stocks and crypto and none have a strong enough setup right now. "
                "The best trade is sometimes no trade. I'll keep watching and flag opportunities as they appear."
            )
            yield f"data: {json.dumps({'token': msg})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _empty_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Live price validation (drop picks that have moved too far, update prices)
    top_picks = _smart_pick_validate_live_prices(raw_top_picks, drift_threshold_pct=5.0)
    if not top_picks:
        def _moved_stream():
            msg = (
                f"I scanned {total_scanned:,} stocks and crypto and all previously-good setups have moved too far "
                "from their ideal entries. Right now it's safer to wait for new clean setups. "
                "I'll keep scanning and surface fresh trades as they appear."
            )
            yield f"data: {json.dumps({'token': msg})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _moved_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    sp_ctx["top_picks"] = top_picks
    sp_ctx["picks_qualified"] = len(top_picks)

    full_context = ts._build_smart_pick_context_strings(db, sp_ctx)  # type: ignore[attr-defined]

    user_msg = (
        "Based on this scan, what are your top 10 stock picks I should buy RIGHT NOW? "
        "For each one, give me the exact buy-in price, sell target, stop-loss, expected hold duration, "
        "position size, and your confidence level. Rank them by conviction."
    )

    system_prompt = load_prompt("trading_analyst")
    ticker_names = ", ".join(p["ticker"] for p in top_picks)

    smart_pick_addendum = f"""

SPECIAL INSTRUCTION — SMART PICK MODE:
You scanned {total_scanned:,} stocks and crypto. The TOP candidates are: {ticker_names}
Their full indicator data and scores are in the MARKET SCAN RESULTS section below.

ABSOLUTE RULES (NEVER VIOLATE):
- You MUST list the top picks immediately. Do NOT ask the user to choose a universe, narrow down, or pick a letter. The scan is ALREADY DONE — your ONLY job is to rank and present the results.
- You MUST reference tickers BY NAME (e.g. "AAPL", "BTC-USD", "NVDA") — NEVER give a generic recommendation without naming specific tickers.
- Use the ACTUAL prices and indicator values from the data provided — do NOT make up numbers.
- If the user asked about crypto specifically, prioritize crypto tickers from the scan.
- If the user asked about stocks specifically, prioritize stock tickers.
- Do NOT refuse to list picks. If some candidates are weaker, still list them with appropriate caveats and lower confidence — the user wants a ranked list, not a refusal.

Your job: Rank and present UP TO 10 trades from this scan as a clear, specific action plan. If fewer than 10 candidates have viable setups, list only those that do — but you MUST list at least the top candidates provided.

For EACH recommended trade, format it EXACTLY like this:

## 1. TICKER — Company/Coin Name
- **Verdict**: STRONG BUY / BUY
- **Confidence**: X%
- **Current Price**: $X.XX (from the data)
- **Buy-in Price**: $X.XX (entry level)
- **Stop-Loss**: $X.XX (reason)
- **Target 1**: $X.XX (conservative)
- **Target 2**: $X.XX (optimistic)
- **Risk/Reward**: X:1
- **Hold Duration**: X days/weeks
- **Position Size**: X% of portfolio
- **Why NOW**: 2-3 bullet points using the ACTUAL indicator values
- **Exit Signal**: what would invalidate this trade

End with portfolio allocation advice and any general market context warnings.
"""

    system_prompt_full = f"{system_prompt}\n{smart_pick_addendum}\n\n---\n\n{full_context}"

    def _generate():
        from .. import openai_client

        try:
            # First send metadata so the UI can show counts immediately
            meta = {
                "scanned": int(total_scanned),
                "qualified": int(sp_ctx.get("picks_qualified", len(top_picks))),
            }
            yield f"data: {json.dumps({'meta': meta})}\n\n"

            _stream_had_token = False
            for tok, model in openai_client.chat_stream(
                messages=[{"role": "user", "content": user_msg}],
                system_prompt=system_prompt_full,
                trace_id=trace_id,
                user_message=user_msg,
                max_tokens=4096,
            ):
                _stream_had_token = True
                yield f"data: {json.dumps({'token': tok})}\n\n"
            if not _stream_had_token:
                _empty = (
                    "*Smart Pick returned no narrative text from the AI.* "
                    "Please try again in a moment."
                )
                yield f"data: {json.dumps({'token': _empty})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            log_info(trace_id, f"[trading] smart-pick stream error: {e}")
            err_msg = f"\n\n*Smart Pick error: {e}*"
            yield f"data: {json.dumps({'token': err_msg})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

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
        ticker=body.ticker,
        strategy_id=body.strategy,
        period=body.period,
        interval=body.interval,
        cash=body.cash,
        commission=body.commission,
        strategy_params=body.strategy_params,
    )
    if not result.get("ok"):
        return JSONResponse(_json_safe(result), status_code=400)

    bt_svc.save_backtest(db, ctx["user_id"], result)
    return JSONResponse(_json_safe(result))


@router.get("/api/trading/backtest/all")
def api_backtest_all_strategies(
    ticker: str = Query(...),
):
    """Run all strategies on a ticker and return summary results."""
    results = []
    for sid, info in bt_svc.STRATEGIES.items():
        try:
            r = bt_svc.run_backtest(ticker=ticker, strategy_id=sid, period="1y")
            if r.get("ok"):
                results.append({
                    "strategy_id": sid,
                    "strategy": r["strategy"],
                    "return_pct": r["return_pct"],
                    "win_rate": r["win_rate"],
                    "sharpe": r.get("sharpe"),
                    "max_drawdown": r["max_drawdown"],
                    "trade_count": r["trade_count"],
                })
        except Exception:
            continue
    results.sort(key=lambda x: x["return_pct"], reverse=True)
    return JSONResponse({"ok": True, "results": results})


@router.get("/api/trading/backtest/quick")
def api_quick_backtest(
    ticker: str = Query(...),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """Return a cached recent backtest or run the best-performing strategy."""
    from datetime import timedelta
    from ..models.trading import BacktestResult

    ctx = get_identity_ctx(request, db)
    cutoff = datetime.utcnow() - timedelta(hours=24)
    cached = (
        db.query(BacktestResult)
        .filter(
            BacktestResult.ticker == ticker.upper(),
            BacktestResult.ran_at >= cutoff,
        )
        .order_by(BacktestResult.return_pct.desc())
        .first()
    )
    if cached:
        import json as _json

        from ..services.trading.backtest_metrics import backtest_win_rate_db_to_display_pct

        eq = []
        try:
            eq = _json.loads(cached.equity_curve) if cached.equity_curve else []
        except Exception:
            pass
        return JSONResponse({
            "ok": True, "cached": True,
            "ticker": cached.ticker,
            "strategy_name": cached.strategy_name,
            "return_pct": cached.return_pct,
            "win_rate": backtest_win_rate_db_to_display_pct(cached.win_rate),
            "sharpe": cached.sharpe,
            "max_drawdown": cached.max_drawdown,
            "trade_count": cached.trade_count,
            "equity_curve": eq,
        })

    best_result = None
    for sid in bt_svc.STRATEGIES:
        try:
            r = bt_svc.run_backtest(ticker=ticker, strategy_id=sid, period="1y")
            if r.get("ok"):
                if best_result is None or r.get("return_pct", -999) > best_result.get("return_pct", -999):
                    best_result = r
        except Exception:
            continue

    if not best_result:
        return JSONResponse({"ok": False, "error": "All strategies failed"}, status_code=400)

    bt_svc.save_backtest(db, ctx["user_id"], best_result)
    return JSONResponse({
        "ok": True, "cached": False,
        "ticker": best_result["ticker"],
        "strategy_name": best_result.get("strategy", ""),
        "return_pct": best_result.get("return_pct", 0),
        "win_rate": best_result.get("win_rate", 0),
        "sharpe": best_result.get("sharpe"),
        "max_drawdown": best_result.get("max_drawdown", 0),
        "trade_count": best_result.get("trade_count", 0),
        "equity_curve": best_result.get("equity_curve", []),
        "trades": best_result.get("trades", []),
    })


# ── Crypto Breakout Scanner ────────────────────────────────────────────

@router.get("/api/trading/crypto-breakouts")
def api_crypto_breakouts(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    refresh: bool = Query(False),
):
    """Return cached crypto breakout scan or trigger a fresh scan."""
    from ..services.trading.scanner import run_crypto_breakout_scan, get_crypto_breakout_cache

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


@router.post("/api/trading/crypto-breakouts/scan")
def api_trigger_crypto_scan(
    background_tasks: BackgroundTasks,
):
    """Manually trigger a crypto breakout scan."""
    from ..services.trading.scanner import run_crypto_breakout_scan
    background_tasks.add_task(run_crypto_breakout_scan, 20)
    return JSONResponse({"ok": True, "message": "Crypto breakout scan started"})


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


# ── Custom Screener ───────────────────────────────────────────────────

@router.get("/api/trading/screener/presets")
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


from pydantic import BaseModel as _BaseModel
from typing import Optional as _Optional


class ScreenRequest(_BaseModel):
    screen_id: _Optional[str] = None
    conditions: _Optional[list[dict]] = None


@router.post("/api/trading/screener/run")
def api_run_screener(body: ScreenRequest, db: Session = Depends(get_db)):
    """Run a preset or custom screen across the full ticker universe."""
    result = ts.run_custom_screen(
        screen_id=body.screen_id,
        conditions=body.conditions,
        db=db,
    )
    return JSONResponse(result)


# ── Day-Trade & Breakout Scans ────────────────────────────────────────

@router.get("/api/trading/scan/progress")
def api_scan_progress():
    """Lightweight poll endpoint for live scan progress."""
    return JSONResponse(ts.get_intraday_scan_progress())


@router.post("/api/trading/scan/daytrade")
def api_run_daytrade_scan(background_tasks: BackgroundTasks):
    """Return cached day-trade results if fresh, else kick off BG scan and return fast."""
    from ..services.trading.scanner import (
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


@router.post("/api/trading/scan/breakouts")
def api_run_breakout_scan(background_tasks: BackgroundTasks):
    """Return cached breakout results if fresh, else kick off BG scan and return fast."""
    from ..services.trading.scanner import (
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


@router.get("/api/trading/scan/momentum")
@router.post("/api/trading/scan/momentum")
def api_run_momentum_scan():
    """Active momentum scanner — finds top intraday setups with strict filters."""
    from ..services.trading.scanner import run_momentum_scanner
    result = run_momentum_scanner()
    return JSONResponse(result)


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


@router.get("/api/trading/top-picks")
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


@router.get("/api/trading/brain/tickers")
def api_brain_tickers(db: Session = Depends(get_db)):
    """Return the brain's top known tickers — crypto and stocks — for UI population.

    Pulls from the brain's MarketSnapshot history (recently scored tickers)
    and the user's watchlist. No hardcoded lists.
    """
    from ..models.trading import MarketSnapshot
    from sqlalchemy import func, desc

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


# ── Strategy Proposals ─────────────────────────────────────────────────

@router.get("/api/trading/proposals")
def api_get_proposals(
    request: Request,
    db: Session = Depends(get_db),
    status: str | None = Query(None),
):
    ctx = get_identity_ctx(request, db)
    proposals = ts.get_proposals(db, ctx["user_id"], status=status)
    return JSONResponse({"ok": True, "proposals": proposals})


@router.post("/api/trading/proposals/{proposal_id}/approve")
async def api_approve_proposal(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    broker = None
    try:
        body = await request.json()
        broker = body.get("broker")
    except Exception:
        pass
    result = ts.approve_proposal(db, proposal_id, ctx["user_id"], broker=broker)
    return JSONResponse(result)


@router.post("/api/trading/proposals/{proposal_id}/reject")
def api_reject_proposal(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    result = ts.reject_proposal(db, proposal_id, ctx["user_id"])
    return JSONResponse(result)


@router.post("/api/trading/proposals/from-pick")
async def api_create_proposal_from_pick(
    request: Request,
    db: Session = Depends(get_db),
):
    """Create a strategy proposal from a top pick, using the latest price.
    Body: { "ticker": "DHC" } or { "ticker": "DHC", "entry_price", "stop_loss", "take_profit" }.
    """
    ctx = get_identity_ctx(request, db)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ticker = (body.get("ticker") or "").strip().upper()
    if not ticker:
        return JSONResponse({"ok": False, "error": "ticker is required"}, status_code=400)
    override = {}
    for key in ("entry_price", "stop_loss", "take_profit"):
        v = body.get(key)
        if v is not None and v != "":
            try:
                override[key] = float(v)
            except (TypeError, ValueError):
                pass
    logger.info("[from-pick] ticker=%s, override=%s, user_id=%s", ticker, override, ctx.get("user_id"))
    try:
        result, err = ts.create_proposal_from_pick(
            db, ctx["user_id"], ticker, override_levels=override if override else None
        )
    except Exception as exc:
        logger.exception("[from-pick] unexpected error for %s", ticker)
        return JSONResponse({"ok": False, "error": f"Server error: {exc!s}"}, status_code=500)
    if err:
        logger.warning("[from-pick] 400 for %s: %s", ticker, err)
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    return JSONResponse({"ok": True, "proposal": result})


@router.post("/api/trading/top-picks/recheck")
def api_recheck_pick(body: PickRecheckRequest, request: Request, db: Session = Depends(get_db)):
    """Revalidate a single pick with live price. Expects JSON body: { ticker, entry_price }."""
    ctx = get_identity_ctx(request, db)
    result = ts.recheck_pick(body.ticker, body.entry_price)
    return JSONResponse(result)


@router.post("/api/trading/proposals/{proposal_id}/recheck")
def api_recheck_proposal(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Revalidate a proposal with live price. Auto-expires if drift > 30%."""
    ctx = get_identity_ctx(request, db)
    result = ts.recheck_proposal(db, proposal_id, ctx["user_id"])
    return JSONResponse(result)


# ── Alerts ─────────────────────────────────────────────────────────────

@router.get("/api/trading/alerts/history")
def api_alert_history(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(50),
):
    ctx = get_identity_ctx(request, db)
    history = ts.get_alert_history(db, ctx["user_id"], limit=limit)
    return JSONResponse({"ok": True, "alerts": history})


@router.post("/api/trading/alerts/test")
def api_test_alert(request: Request, db: Session = Depends(get_db)):
    """Send a test SMS alert to verify the notification setup."""
    ctx = get_identity_ctx(request, db)
    from ..services.sms_service import send_sms, get_sms_status

    sms_status = get_sms_status()
    if not sms_status["configured"]:
        return JSONResponse({"ok": False, "error": "SMS not configured. Set SMS_PHONE and SMS_CARRIER (or Twilio) in .env"})

    msg = "CHILI Test Alert: SMS notifications are working! You'll receive alerts for breakouts, targets, stops, and strategy proposals."
    sent = send_sms(msg)

    if sent:
        ts.dispatch_alert(db, ctx["user_id"], "test", None, msg)

    return JSONResponse({"ok": sent, "status": sms_status})


@router.get("/api/trading/alerts/settings")
def api_alert_settings():
    """Return current alert settings for the UI."""
    from ..services.sms_service import get_sms_status
    return JSONResponse({"ok": True, **get_sms_status()})


@router.post("/api/trading/alerts/run-pattern-imminent")
def api_run_pattern_imminent_scan(
    request: Request,
    db: Session = Depends(get_db),
    dry_run: bool | None = Query(None),
):
    """Run the ScanPattern imminent-breakout scan once; returns diagnostics (candidates, skip counts).

    Use this to verify the job is working without waiting for the 15-minute scheduler.
    Alerts are still written via ``dispatch_alert`` (DB + SMS/Telegram when configured).
    Pass ``dry_run=1`` to compute candidates and summary without dispatch or BreakoutAlert insert.
    """
    ctx = get_identity_ctx(request, db)
    from ..services.trading.pattern_imminent_alerts import run_pattern_imminent_scan

    result = run_pattern_imminent_scan(db, ctx["user_id"], dry_run=dry_run)
    return JSONResponse(result)


# ── Background Learning ───────────────────────────────────────────────

def _start_learning_cycle_bg(
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    """Trigger a full learning cycle in the background."""
    ctx = get_identity_ctx(request, db)

    if ts.get_learning_status()["running"]:
        return JSONResponse({"ok": True, "message": "Learning cycle already running"})

    from ..db import SessionLocal

    def _bg_learn(user_id):
        sdb = SessionLocal()
        try:
            ts.run_learning_cycle(sdb, user_id, full_universe=True)
        finally:
            sdb.close()

    background_tasks.add_task(_bg_learn, ctx["user_id"])
    return JSONResponse({"ok": True, "message": "Full learning cycle started in background"})


@router.post("/api/trading/learn/cycle")
def api_run_learning_cycle(
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    """Trigger a full learning cycle in the background."""
    return _start_learning_cycle_bg(background_tasks, request, db)


@router.post("/api/trading/learn/snapshot", deprecated=True)
def api_take_snapshots(
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    """Deprecated — use POST /api/trading/learn/cycle instead."""
    return _start_learning_cycle_bg(background_tasks, request, db)


# ── Brain Dashboard ───────────────────────────────────────────────────


# Brain, learning, and broker endpoints are now in sub-routers
# (included via router.include_router at the top)


# ── Pattern Engine ─────────────────────────────────────────────────────

@router.get("/api/trading/patterns")
def api_list_patterns(
    active_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    from ..services.trading.pattern_engine import list_patterns
    patterns = list_patterns(db, active_only=active_only)
    return JSONResponse({"ok": True, "patterns": patterns})


class _CreatePatternBody(_BaseModel):
    name: str
    description: str = ""
    rules_json: str = "{}"
    origin: str = "user"
    asset_class: str = "all"
    score_boost: float = 0.0
    min_base_score: float = 0.0


@router.post("/api/trading/patterns")
def api_create_pattern(
    body: _CreatePatternBody,
    db: Session = Depends(get_db),
):
    from ..services.trading.pattern_engine import create_pattern
    try:
        p = create_pattern(db, body.dict())
        return JSONResponse({"ok": True, "pattern": {"id": p.id, "name": p.name}})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


class _PatternBody(_BaseModel):
    name: str | None = None
    description: str | None = None
    rules_json: str | None = None
    active: bool | None = None
    score_boost: float | None = None
    min_base_score: float | None = None
    asset_class: str | None = None


@router.put("/api/trading/patterns/{pattern_id}")
def api_update_pattern(
    pattern_id: int,
    body: _PatternBody,
    db: Session = Depends(get_db),
):
    from ..services.trading.pattern_engine import update_pattern
    data = {k: v for k, v in body.dict().items() if v is not None}
    p = update_pattern(db, pattern_id, data)
    if not p:
        return JSONResponse({"ok": False, "error": "Pattern not found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.delete("/api/trading/patterns/{pattern_id}")
def api_delete_pattern(
    pattern_id: int,
    db: Session = Depends(get_db),
):
    from ..services.trading.pattern_engine import delete_pattern
    ok = delete_pattern(db, pattern_id)
    return JSONResponse({"ok": ok})


@router.get("/api/trading/patterns/{pattern_id}/export/pine")
def api_export_pattern_pine(
    pattern_id: int,
    kind: str = "strategy",
    db: Session = Depends(get_db),
):
    """Export pattern rules as TradingView Pine Script v5 (best-effort; see ``warnings``).

    ``kind=strategy`` (default): ``strategy()`` for Strategy Tester.
    ``kind=indicator``: ``indicator()`` with plotshape / alerts.
    """
    from ..models.trading import ScanPattern
    from ..services.trading.pine_export import scan_pattern_to_pine

    k = (kind or "strategy").strip().lower()
    if k not in ("strategy", "indicator"):
        return JSONResponse(
            {"ok": False, "error": "kind must be strategy or indicator"},
            status_code=400,
        )

    p = db.query(ScanPattern).get(pattern_id)
    if not p:
        return JSONResponse({"ok": False, "error": "Pattern not found"}, status_code=404)
    pine, warnings = scan_pattern_to_pine(
        p, kind=cast(Literal["strategy", "indicator"], k)
    )
    return JSONResponse(
        {
            "ok": True,
            "pine": pine,
            "warnings": warnings,
            "pattern_id": p.id,
            "name": p.name,
            "kind": k,
        }
    )


class _SuggestPatternBody(_BaseModel):
    description: str


@router.post("/api/trading/patterns/suggest")
def api_suggest_pattern(
    body: _SuggestPatternBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Parse a natural language pattern description into a ScanPattern, TradingHypothesis, and TradingInsight."""
    from ..services.llm_caller import call_llm
    from ..services.trading.pattern_engine import create_pattern
    from ..models.trading import TradingHypothesis, TradingInsight
    import json as _json

    prompt = (
        "Convert this trading pattern description into a structured JSON pattern rule.\n\n"
        f'Description: "{body.description}"\n\n'
        "Respond with a JSON object:\n"
        '{"name": "Short descriptive name", "description": "...", '
        '"conditions": [{"indicator": "...", "op": "...", "value": ...}], '
        '"score_boost": 1.5, "min_base_score": 4.0}\n\n'
        "Available indicators: rsi_14, ema_20, ema_50, ema_100, price, bb_squeeze, adx, "
        "rel_vol, macd_hist, resistance_retests, dist_to_resistance_pct, narrow_range, "
        "vcp_count, vwap_reclaim.\n"
        "Available ops: >, >=, <, <=, ==, between, any_of.\n"
        "For 'price' comparisons use 'ref' key pointing to indicator name.\n"
        "Respond ONLY with the JSON object."
    )

    try:
        resp = call_llm(prompt, max_tokens=800)
        if not resp:
            return JSONResponse({"ok": False, "error": "LLM returned empty response"}, status_code=500)

        text = resp.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        parsed = _json.loads(text)
        conditions = parsed.get("conditions", [])
        pattern_data = {
            "name": parsed.get("name", "Custom Pattern"),
            "description": parsed.get("description", body.description),
            "rules_json": _json.dumps({"conditions": conditions}),
            "origin": "user",
            "score_boost": parsed.get("score_boost", 1.0),
            "min_base_score": parsed.get("min_base_score", 4.0),
            "confidence": 0.0,
            "active": True,
        }
        p = create_pattern(db, pattern_data)

        hypothesis_id = None
        if len(conditions) >= 2:
            cond_a_parts = {c.get("indicator", "?"): c for c in conditions}
            partial = conditions[:-1]
            cond_b_parts = {c.get("indicator", "?"): c for c in partial}

            hyp_desc = (
                f"Full pattern '{p.name}' ({len(conditions)} conditions) "
                f"outperforms partial ({len(partial)} conditions, "
                f"without {conditions[-1].get('indicator', '?')})"
            )
            existing = db.query(TradingHypothesis).filter(
                TradingHypothesis.description == hyp_desc,
            ).first()
            if not existing:
                hyp = TradingHypothesis(
                    description=hyp_desc,
                    condition_a=_json.dumps(cond_a_parts),
                    condition_b=_json.dumps(cond_b_parts),
                    expected_winner="a",
                    origin="user",
                    status="pending",
                    related_pattern_id=p.id,
                )
                db.add(hyp)
                db.commit()
                db.refresh(hyp)
                hypothesis_id = hyp.id

        ctx = get_identity_ctx(request, db)
        uid = ctx.get("user_id")
        insight_desc = (
            f"{p.name} — {p.description or body.description} [User-suggested pattern]"
        )
        existing_insight = db.query(TradingInsight).filter(
            TradingInsight.pattern_description.like(f"{p.name}%"),
            TradingInsight.user_id == uid,
        ).first()
        if not existing_insight:
            insight = TradingInsight(
                user_id=uid,
                scan_pattern_id=p.id,
                pattern_description=insight_desc,
                confidence=0.5,
                evidence_count=1,
                active=True,
                win_count=0,
                loss_count=0,
            )
            db.add(insight)
            db.commit()

        return JSONResponse({
            "ok": True,
            "pattern": {
                "id": p.id, "name": p.name, "description": p.description,
                "rules_json": p.rules_json, "score_boost": p.score_boost,
            },
            "hypothesis_id": hypothesis_id,
        })
    except _json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "Could not parse LLM response as JSON"}, status_code=500)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/trading/patterns/{pattern_id}/backtest")
def api_backtest_pattern(
    pattern_id: int,
    ticker: str = Query("AAPL"),
    interval: str = Query("1d"),
    period: str = Query("1y"),
    db: Session = Depends(get_db),
    body: PatternBacktestRequest | None = Body(default=None),
):
    from ..services.backtest_service import backtest_pattern, get_backtest_params
    from ..services.trading.pattern_resolution import resolve_to_scan_pattern

    req = body or PatternBacktestRequest()
    p = resolve_to_scan_pattern(db, pattern_id)
    if not p:
        return JSONResponse({"ok": False, "error": "Pattern not found"}, status_code=404)
    tf = getattr(p, "timeframe", "1d") or "1d"
    bt_params = get_backtest_params(tf)
    use_interval = interval if interval != "1d" else bt_params["interval"]
    use_period = period if period != "1y" else bt_params["period"]
    if req.interval:
        use_interval = req.interval
    if req.period:
        use_period = req.period
    from ..config import settings

    use_ticker = (req.ticker or ticker).strip().upper()
    cash = req.cash if req.cash is not None else 100_000.0
    commission = req.commission if req.commission is not None else float(settings.backtest_commission)
    spread = req.spread if req.spread is not None else float(settings.backtest_spread)
    result = backtest_pattern(
        ticker=use_ticker,
        pattern_name=p.name,
        rules_json=p.rules_json,
        interval=use_interval,
        period=use_period,
        exit_config=getattr(p, "exit_config", None),
        cash=cash,
        commission=commission,
        spread=spread,
        oos_holdout_fraction=req.oos_holdout_fraction,
        rules_json_override=req.rules_json_override,
        append_conditions=req.append_conditions,
        exit_config_overlay=req.exit_config,
    )
    if not result.get("ok"):
        return JSONResponse(_json_safe(result), status_code=400)
    return JSONResponse(_json_safe(result))


@router.post("/api/trading/patterns/research")
def api_trigger_pattern_research(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Trigger web pattern research manually."""
    from ..services.trading.web_pattern_researcher import run_web_pattern_research
    background_tasks.add_task(run_web_pattern_research, db=None)
    return JSONResponse({"ok": True, "message": "Web pattern research started in background"})


@router.get("/api/trading/patterns/research/status")
def api_pattern_research_status():
    """Get the current status of web pattern research."""
    from ..services.trading.web_pattern_researcher import get_research_status
    return JSONResponse({"ok": True, **get_research_status()})


# ---------------------------------------------------------------------------
# Real-time WebSocket: live chart ticks + global alert push
# ---------------------------------------------------------------------------

import threading as _th

_live_clients: set[WebSocket] = set()
_live_clients_tlock = _th.Lock()


async def broadcast_trading_alert(alert_data: dict[str, Any]) -> None:
    """Push an alert to every connected live-trading WebSocket client."""
    msg = json.dumps({"type": "alert", **alert_data})
    with _live_clients_tlock:
        clients = list(_live_clients)
    stale: list[WebSocket] = []
    for ws_c in clients:
        try:
            await ws_c.send_text(msg)
        except Exception:
            stale.append(ws_c)
    if stale:
        with _live_clients_tlock:
            for ws_c in stale:
                _live_clients.discard(ws_c)


def _broadcast_alert_sync(alert_data: dict[str, Any]) -> None:
    """Thread-safe wrapper so scheduler / alert code can call from sync context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        loop.create_task(broadcast_trading_alert(alert_data))
    else:
        try:
            asyncio.run(broadcast_trading_alert(alert_data))
        except RuntimeError:
            pass


@router.websocket("/ws/trading/live")
async def ws_trading_live(ws: WebSocket, ticker: str = "AAPL"):
    """Stream real-time price ticks for *ticker* and broadcast alerts globally."""
    await ws.accept()
    logger.info("[live-ws] Client connected for %s", ticker)

    with _live_clients_tlock:
        _live_clients.add(ws)

    tick_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    massive_available = False
    m_ticker = ""
    _on_tick = None

    try:
        from ..services.massive_client import (
            get_ws_client,
            register_tick_listener,
            unregister_tick_listener,
            to_massive_ticker,
            QuoteSnapshot,
            TradeSnapshot,
        )

        m_ticker = to_massive_ticker(ticker).upper()

        def _on_tick_cb(sym: str, snap):
            try:
                if hasattr(snap, "size") and snap.size:
                    data = {"type": "tick", "price": snap.price,
                            "size": snap.size, "time": snap.timestamp}
                else:
                    data = {"type": "tick", "price": snap.price,
                            "size": 0, "time": snap.timestamp}
                tick_queue.put_nowait(data)
            except Exception:
                pass

        _on_tick = _on_tick_cb
        ws_client = get_ws_client()
        if ws_client.running:
            ws_client.subscribe([m_ticker])
            register_tick_listener(m_ticker, _on_tick)
            massive_available = True
            logger.info("[live-ws] Subscribed to Massive WS ticks for %s", m_ticker)
        else:
            logger.info("[live-ws] Massive WS not running, using poll fallback for %s", ticker)
    except Exception as exc:
        logger.warning("[live-ws] Massive WS setup failed: %s", exc)
        massive_available = False

    async def _poll_fallback():
        """Periodic REST poll — always runs as heartbeat, faster when Massive WS is off."""
        import time as _time
        interval = 5 if massive_available else 1
        while True:
            try:
                quote = await asyncio.to_thread(
                    ts.fetch_quote,
                    ticker,
                    allow_provider_fallback=None,
                )
                if quote and quote.get("price") is not None:
                    await tick_queue.put({
                        "type": "tick",
                        "price": float(quote["price"]),
                        "size": 0,
                        "time": _time.time(),
                    })
            except Exception:
                pass
            await asyncio.sleep(interval)

    poll_task = asyncio.create_task(_poll_fallback())

    try:
        async def _sender():
            while True:
                msg = await tick_queue.get()
                try:
                    await ws.send_json(msg)
                except Exception:
                    return

        async def _receiver():
            try:
                while True:
                    msg = await ws.receive()
                    if msg.get("type") == "websocket.disconnect":
                        return
                    text = msg.get("text")
                    if text:
                        try:
                            cmd = json.loads(text)
                            if cmd.get("action") == "ping":
                                await ws.send_json({"type": "pong"})
                        except (json.JSONDecodeError, TypeError):
                            pass
            except WebSocketDisconnect:
                return

        await asyncio.gather(_sender(), _receiver())

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("[live-ws] Handler error for %s: %s", ticker, exc)
    finally:
        if poll_task:
            poll_task.cancel()
        if massive_available and _on_tick and m_ticker:
            try:
                from ..services.massive_client import unregister_tick_listener
                unregister_tick_listener(m_ticker, _on_tick)
            except Exception:
                pass
        with _live_clients_tlock:
            _live_clients.discard(ws)
        logger.info("[live-ws] Client disconnected for %s", ticker)
