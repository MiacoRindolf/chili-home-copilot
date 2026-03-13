"""Trading module routes: page + REST APIs for market data, indicators, watchlist, journal, AI."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from ..deps import get_db, get_identity_ctx
from ..logger import log_info, new_trace_id
from ..prompts import load_prompt
from ..services import trading_service as ts
from ..services.trading.scanner import _validate_live_prices as _smart_pick_validate_live_prices
from ..services import trading_scheduler
from ..services import ticker_universe
from ..services import broker_service
from .trading_sub import ai_router, broker_router, web3_router
from ..schemas.trading import (
    AnalyzeRequest,
    BacktestRequest,
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
router.include_router(broker_router)
router.include_router(web3_router)

_TRADING_PROMPT: str | None = None


def _get_trading_prompt() -> str:
    global _TRADING_PROMPT
    if _TRADING_PROMPT is None:
        _TRADING_PROMPT = load_prompt("trading_analyst")
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


# ── Page ────────────────────────────────────────────────────────────────

@router.get("/trading", response_class=HTMLResponse)
def trading_page(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)

    # Auto-trigger learning cycle on page load if stale (>1 hr since last run)
    if ts.should_run_learning():
        from ..db import SessionLocal

        def _bg_learn(user_id):
            sdb = SessionLocal()
            try:
                ts.run_learning_cycle(sdb, user_id, full_universe=True)
            finally:
                sdb.close()

        background_tasks.add_task(_bg_learn, ctx["user_id"])

    return request.app.state.templates.TemplateResponse(
        request, "trading.html",
        {
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
    try:
        data = ts.fetch_ohlcv(ticker, interval=interval, period=period)
    except Exception:
        data = []
    return JSONResponse({"ok": True, "ticker": ticker.upper(), "data": data})


@router.get("/api/trading/quote")
def api_quote(ticker: str = Query(...)):
    quote = ts.fetch_quote(ticker)
    if not quote:
        return JSONResponse({"ok": True, "ticker": ticker.upper(), "price": None, "change": None, "change_pct": None})
    return JSONResponse({"ok": True, **quote})


@router.get("/api/trading/quotes/batch")
def api_quotes_batch(tickers: str = Query(..., description="Comma-separated ticker list")):
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()][:50]
    results = ts.fetch_quotes_batch(ticker_list)
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
            "broker_source": t.broker_source,
            "broker_status": t.broker_status,
            "broker_order_id": t.broker_order_id,
        }
        for t in trades
    ]})


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

    if trade.broker_source == "robinhood" and broker_service.is_connected():
        order_type = "limit" if body.limit_price else "market"
        result = broker_service.place_sell_order(
            ticker=trade.ticker,
            quantity=body.quantity,
            order_type=order_type,
            limit_price=body.limit_price,
        )
        if not result.get("ok"):
            return JSONResponse({"ok": False, "error": result.get("error", "Sell failed")}, status_code=500)

        rh_state = (result.get("state") or "queued").lower()
        order_id = result.get("order_id", "")

        if is_full_exit:
            if rh_state == "filled":
                trade.status = "closed"
                trade.exit_price = body.limit_price or trade.entry_price
                trade.exit_date = datetime.utcnow()
                trade.pnl = round((trade.exit_price - trade.entry_price) * trade.quantity, 2)
            else:
                trade.notes = (trade.notes or "") + f"\nSell order placed (full exit), RH order {order_id} ({rh_state})"
        else:
            remaining = round(trade.quantity - body.quantity, 6)
            exit_price = body.limit_price or trade.entry_price
            realized_pnl = round((exit_price - trade.entry_price) * body.quantity, 2)
            trade.quantity = remaining
            trade.notes = (
                (trade.notes or "")
                + f"\nPartial sell: {body.quantity} shares"
                + (f" @ ${body.limit_price}" if body.limit_price else " (market)")
                + f", RH order {order_id} ({rh_state}), realized ~${realized_pnl}"
            )

        db.commit()
        return JSONResponse({
            "ok": True,
            "trade_id": trade.id,
            "sold_qty": body.quantity,
            "remaining_qty": round(trade.quantity, 6),
            "rh_state": rh_state,
            "order_id": order_id,
            "status": trade.status,
        })

    # Manual / paper trade — immediate simulated close
    exit_price = body.limit_price or trade.entry_price
    if is_full_exit:
        trade.status = "closed"
        trade.exit_price = exit_price
        trade.exit_date = datetime.utcnow()
        trade.pnl = round((exit_price - trade.entry_price) * trade.quantity, 2)
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
            for tok, model in openai_client.chat_stream(
                messages=messages,
                system_prompt=system_prompt,
                trace_id=trace_id,
                user_message=user_msg,
                max_tokens=2048,
            ):
                yield f"data: {json.dumps({'token': tok})}\n\n"
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

            for tok, model in openai_client.chat_stream(
                messages=[{"role": "user", "content": user_msg}],
                system_prompt=system_prompt_full,
                trace_id=trace_id,
                user_message=user_msg,
                max_tokens=4096,
            ):
                yield f"data: {json.dumps({'token': tok})}\n\n"
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
def api_run_screener(body: ScreenRequest):
    """Run a preset or custom screen across the full ticker universe."""
    result = ts.run_custom_screen(
        screen_id=body.screen_id,
        conditions=body.conditions,
    )
    return JSONResponse(result)


# ── Day-Trade & Breakout Scans ────────────────────────────────────────

@router.post("/api/trading/scan/daytrade")
def api_run_daytrade_scan():
    """Scan for day-trade opportunities using intraday data."""
    result = ts.run_daytrade_scan()
    return JSONResponse(result)


@router.post("/api/trading/scan/breakouts")
def api_run_breakout_scan():
    """Scan for stocks consolidating near resistance — breakout watchlist."""
    result = ts.run_breakout_scan()
    return JSONResponse(result)


@router.get("/api/trading/scan/momentum")
@router.post("/api/trading/scan/momentum")
def api_run_momentum_scan():
    """Active momentum scanner — finds top intraday setups with strict filters."""
    result = ts.run_momentum_scanner()
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
def api_approve_proposal(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    result = ts.approve_proposal(db, proposal_id, ctx["user_id"])
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


# ── Background Learning ───────────────────────────────────────────────

@router.post("/api/trading/learn/snapshot")
def api_take_snapshots(
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    """Trigger a full learning cycle (replaces the old snapshot-only endpoint)."""
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


# ── Data Provider Status ──────────────────────────────────────────────

@router.get("/api/trading/data-provider/status")
def api_data_provider_status():
    """Return data-provider usage metrics and feature-flag status."""
    from ..config import settings

    massive_enabled = bool(settings.massive_api_key)
    massive_metrics = {}
    if massive_enabled:
        try:
            from ..services.massive_client import get_metrics as get_massive_metrics
            massive_metrics = get_massive_metrics()
        except Exception:
            pass

    polygon_enabled = settings.use_polygon and bool(settings.polygon_api_key)
    polygon_metrics = {}
    if polygon_enabled:
        try:
            from ..services.polygon_client import get_metrics
            polygon_metrics = get_metrics()
        except Exception:
            pass

    return JSONResponse({
        "massive_enabled": massive_enabled,
        "massive_base_url": settings.massive_base_url if massive_enabled else None,
        "massive_websocket": settings.massive_use_websocket if massive_enabled else False,
        "massive_metrics": massive_metrics,
        "polygon_enabled": polygon_enabled,
        "polygon_base_url": settings.polygon_base_url if polygon_enabled else None,
        "polygon_metrics": polygon_metrics,
        "provider_order": [
            p for p, enabled in [
                ("massive", massive_enabled),
                ("polygon", polygon_enabled),
                ("yfinance", True),
            ] if enabled
        ],
    })


# ── Brain Dashboard ───────────────────────────────────────────────────


# Brain, learning, and broker endpoints are now in sub-routers
# (included via router.include_router at the top)
