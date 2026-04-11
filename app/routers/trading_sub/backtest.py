"""Backtest endpoints: strategies list, run, all-strategies comparison, quick-cached.

Extracted from the main trading router.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ...deps import get_db, get_identity_ctx
from ...services import backtest_service as bt_svc
from ...schemas.trading import BacktestRequest
from ._utils import json_safe

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trading", tags=["trading-backtest"])


@router.get("/backtest/strategies")
def api_list_strategies():
    return JSONResponse({"ok": True, "strategies": bt_svc.list_strategies()})


@router.post("/backtest")
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
        return JSONResponse(json_safe(result), status_code=400)

    bt_svc.save_backtest(db, ctx["user_id"], result)
    return JSONResponse(json_safe(result))


@router.get("/backtest/all")
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


@router.get("/backtest/quick")
def api_quick_backtest(
    ticker: str = Query(...),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """Return a cached recent backtest or run the best-performing strategy."""
    from datetime import timedelta
    from ...models.trading import BacktestResult

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

        from ...services.trading.public_api import backtest_win_rate_db_to_display_pct

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
