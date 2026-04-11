"""Trade lifecycle, TCA, attribution, journal, and stats endpoints.

Extracted from the main trading router to keep each sub-router focused on a
single capability cluster.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ...deps import get_db, get_identity_ctx
from ...services import broker_manager, broker_service
from ...services import trading_service as ts
from ...schemas.trading import JournalCreate, TradeClose, TradeCreate, TradeSell
from ._utils import json_safe

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trading", tags=["trading-trades"])


# ── Trades CRUD ──────────────────────────────────────────────────────────────

@router.get("/trades")
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


@router.post("/trades")
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

    from ...db import SessionLocal

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


@router.post("/trades/{trade_id}/close")
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

    if background_tasks:
        from ...db import SessionLocal

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


@router.delete("/trades/{trade_id}")
@router.post("/trades/{trade_id}/delete")
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


@router.post("/trades/{trade_id}/sell")
def api_sell_trade(
    trade_id: int,
    body: TradeSell,
    request: Request = None,
    db: Session = Depends(get_db),
):
    """Partial or full sell of an open position, routed through broker when connected."""
    from ...models.trading import Trade

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
                        from ...services.trading.public_api import (
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
            from ...services.trading.public_api import (
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


# ── TCA & Attribution ────────────────────────────────────────────────────────

@router.get("/tca/summary")
def api_tca_summary(
    request: Request,
    db: Session = Depends(get_db),
    days: int = Query(90, ge=1, le=730),
    limit: int = Query(50, ge=1, le=200),
):
    """Rolling TCA aggregates: mean entry slippage (bps) vs proposal/reference by ticker."""
    from ...services.trading.public_api import tca_summary_by_ticker

    ctx = get_identity_ctx(request, db)
    out = tca_summary_by_ticker(db, ctx["user_id"], days=days, limit=limit)
    return JSONResponse(json_safe(out))


@router.get("/attribution/live-vs-research")
def api_attribution_live_vs_research(
    request: Request,
    db: Session = Depends(get_db),
    days: int = Query(90, ge=1, le=730),
    limit: int = Query(50, ge=1, le=200),
):
    """Closed trades with ``scan_pattern_id`` vs pattern OOS / research stats."""
    from ...services.trading.public_api import live_vs_research_by_pattern

    ctx = get_identity_ctx(request, db)
    out = live_vs_research_by_pattern(db, ctx["user_id"], days=days, limit=limit)
    return JSONResponse(json_safe(out))


# ── Journal & Stats ──────────────────────────────────────────────────────────

@router.get("/journal")
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


@router.post("/journal")
def api_add_journal(body: JournalCreate, request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    entry = ts.add_journal_entry(db, ctx["user_id"], body.content, trade_id=body.trade_id)
    return JSONResponse({"ok": True, "id": entry.id})


@router.get("/journal/stats")
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


@router.get("/stats/calendar")
@router.get("/journal/calendar")
def api_stats_calendar(
    request: Request,
    db: Session = Depends(get_db),
    year: int = Query(...),
    month: int = Query(...),
):
    """Daily P&L for a given month. Returns { ok, days: [{ date, trade_count, pnl, trades }] }."""
    return _api_stats_calendar_impl(request, db, year, month)
