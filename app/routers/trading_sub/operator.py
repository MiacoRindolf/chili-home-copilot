"""Operator-facing trading endpoints: runtime health, risk controls, and research integrity.

These endpoints are additive — they do not change existing route shapes or DB state.
They expose internal operational state that was previously only visible in logs or docs.

Routes:
  GET /api/trading/status/overview      – full runtime health snapshot
  GET /api/trading/status/freshness     – lightweight data-age summary for UI banners
  GET /api/trading/risk/budget          – current portfolio risk exposure
  GET /api/trading/risk/limits          – configured risk limits
  POST /api/trading/risk/breaker/reset  – manually reset the circuit breaker
  GET /api/trading/research/integrity   – pattern research provenance summary
  GET /api/trading/research/review      – post-trade "what worked / what failed" review
  GET /api/trading/top-picks/evidence   – top picks with attached evidence panels
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ...deps import get_db, get_identity_ctx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trading", tags=["trading-operator"])


# ── Status / health ─────────────────────────────────────────────────────────

@router.get("/status/overview")
def operator_status_overview(db: Session = Depends(get_db)):
    """Full runtime health snapshot across all key surfaces.

    Returns per-surface ok/stale flags so the operator dashboard can
    immediately see what is healthy, degraded, or disconnected.
    """
    try:
        from ...services.trading.public_api import get_runtime_overview
        return JSONResponse(get_runtime_overview(db))
    except Exception as e:
        logger.exception("[operator] status/overview error")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/status/freshness")
def operator_status_freshness(db: Session = Depends(get_db)):
    """Lightweight data-age summary for UI stale-data banners.

    Returns as_of and age_seconds for each major cache surface without
    performing connectivity checks. Suitable for polling on a short interval.
    """
    try:
        from ...services.trading.public_api import get_freshness_summary
        return JSONResponse(get_freshness_summary(db))
    except Exception as e:
        logger.exception("[operator] status/freshness error")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── Risk controls ────────────────────────────────────────────────────────────

@router.get("/risk/budget")
def operator_risk_budget(request: Request, db: Session = Depends(get_db)):
    """Current portfolio risk exposure snapshot.

    Returns open position counts, sector breakdown, total portfolio heat,
    available heat budget, and whether a new position is currently allowed.
    """
    try:
        from ...services.trading.public_api import (
            get_portfolio_risk_snapshot,
            get_risk_limits,
            get_breaker_status,
        )
        ctx = get_identity_ctx(request, db)
        user_id = ctx.get("user_id")
        capital = float(ctx.get("capital") or 100_000.0)

        limits = get_risk_limits()
        budget = get_portfolio_risk_snapshot(db, user_id, capital, limits)
        breaker = get_breaker_status()

        return JSONResponse({
            "ok": True,
            "can_open_new": budget.can_open_new,
            "rejection_reason": budget.rejection_reason,
            "open_positions": budget.open_positions,
            "stock_positions": budget.stock_positions,
            "crypto_positions": budget.crypto_positions,
            "total_heat_pct": budget.total_heat_pct,
            "available_heat_pct": budget.available_heat_pct,
            "capital": capital,
            "circuit_breaker": {
                "tripped": breaker["tripped"],
                "reason": breaker.get("reason"),
            },
        })
    except Exception as e:
        logger.exception("[operator] risk/budget error")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/risk/limits")
def operator_risk_limits():
    """Configured risk limits (from settings with defaults).

    Returns the effective limit values used by the risk gate so the
    operator can verify what rules are actually active.
    """
    try:
        from ...services.trading.public_api import get_risk_limits, get_drawdown_limits
        limits = get_risk_limits()
        dd_limits = get_drawdown_limits()
        return JSONResponse({
            "ok": True,
            "position_limits": {
                "max_open_positions": limits.max_open_positions,
                "max_crypto_positions": limits.max_crypto_positions,
                "max_stock_positions": limits.max_stock_positions,
                "max_same_ticker": limits.max_same_ticker,
            },
            "risk_sizing": {
                "max_portfolio_heat_pct": limits.max_portfolio_heat_pct,
                "max_risk_per_trade_pct": limits.max_risk_per_trade_pct,
            },
            "drawdown_breaker": {
                "max_5day_dd_pct": dd_limits.max_5day_dd_pct,
                "max_30day_dd_pct": dd_limits.max_30day_dd_pct,
                "max_consecutive_losses": dd_limits.max_consecutive_losses,
                "cooldown_hours": dd_limits.cooldown_hours,
            },
        })
    except Exception as e:
        logger.exception("[operator] risk/limits error")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/risk/breaker/reset")
def operator_reset_breaker(request: Request):
    """Manually reset the circuit breaker (admin action).

    Only use this after reviewing the root cause of the breaker trip.
    The action is logged for the audit trail.
    """
    try:
        from ...services.trading.public_api import reset_breaker
        reset_breaker()
        logger.warning("[operator] Circuit breaker manually reset by request from %s", request.client)
        return JSONResponse({"ok": True, "message": "Circuit breaker reset"})
    except Exception as e:
        logger.exception("[operator] risk/breaker/reset error")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── Research integrity ────────────────────────────────────────────────────────

@router.get("/research/integrity")
def operator_research_integrity(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
):
    """Pattern research provenance summary.

    For each pattern with live trades in the last 90 days, returns:
    - Research win rate (backtest / OOS)
    - OOS avg return
    - Live win rate (actual closed trades)
    - Delta between live and research
    - Promotion status
    - Confidence signal (aligned / lagging / overperforming)

    Useful for spotting overfitted patterns or market-regime drift.
    """
    try:
        from ...services.trading.public_api import live_vs_research_by_pattern
        ctx = get_identity_ctx(request, db)
        data = live_vs_research_by_pattern(db, ctx.get("user_id"), days=90, limit=limit)
        patterns = data.get("patterns", [])

        # Annotate with integrity signal
        for p in patterns:
            live_wr = p.get("live_win_rate_pct") or 0
            research_wr = p.get("research_oos_win_rate_pct") or p.get("research_win_rate_pct") or 0
            n = p.get("live_closed_trades", 0)
            if n < 3 or research_wr == 0:
                p["integrity_signal"] = "insufficient_data"
            elif live_wr >= research_wr - 5:
                p["integrity_signal"] = "aligned"
            elif live_wr >= research_wr - 15:
                p["integrity_signal"] = "lagging"
            else:
                p["integrity_signal"] = "degraded"

        return JSONResponse({
            "ok": True,
            "window_days": 90,
            "total_patterns": len(patterns),
            "patterns": patterns,
        })
    except Exception as e:
        logger.exception("[operator] research/integrity error")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/research/review")
def operator_post_trade_review(
    request: Request,
    db: Session = Depends(get_db),
    days: int = Query(30, ge=1, le=365),
):
    """Post-trade "what worked, what failed, and why" review.

    Returns a structured review over the last *days* covering:
    - Overall win rate, P&L, consecutive losses
    - High-slippage trades (TCA)
    - Outperforming and underperforming patterns vs research
    - Plain-English takeaways
    - Feedback signals (upweight / downweight pattern suggestions)

    This is the learning loop input — the takeaways and feedback_signals
    should inform pattern weight adjustments in the next learning cycle.
    """
    try:
        from ...services.trading.public_api import post_trade_review
        ctx = get_identity_ctx(request, db)
        return JSONResponse(
            post_trade_review(db, ctx.get("user_id"), days=days)
        )
    except Exception as e:
        logger.exception("[operator] research/review error")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── Evidence-enriched top picks ──────────────────────────────────────────────

@router.get("/top-picks/evidence")
def operator_top_picks_with_evidence(request: Request, db: Session = Depends(get_db)):
    """Top picks with attached evidence panels and freshness metadata.

    Same data as /api/trading/top-picks but each pick includes:
    - evidence_summary.why_ranked      — 1-sentence ranked explanation
    - evidence_summary.key_contributors — top signals/indicators with weights
    - evidence_summary.invalidation    — conditions that would kill the thesis
    - evidence_summary.state_note      — freshness and provenance note

    Use this endpoint for the full operator research desk view.
    """
    try:
        from ...services import trading_service as ts
        from ...services.trading.public_api import get_top_picks_freshness, enrich_picks_with_evidence

        ctx = get_identity_ctx(request, db)
        picks = ts.generate_top_picks(db, ctx["user_id"])
        freshness = get_top_picks_freshness(stale_threshold_seconds=600)

        # Attach freshness to each pick so evidence builder can reference it
        for p in picks:
            p["as_of"] = freshness.get("as_of")
            p["is_stale"] = freshness.get("is_stale", False)

        enrich_picks_with_evidence(picks)

        return JSONResponse({
            "ok": True,
            "picks": picks,
            "as_of": freshness["as_of"],
            "age_seconds": freshness["age_seconds"],
            "is_stale": freshness["is_stale"],
        })
    except Exception as e:
        logger.exception("[operator] top-picks/evidence error")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
