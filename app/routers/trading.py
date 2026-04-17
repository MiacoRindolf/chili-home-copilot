"""Trading module routes: page + REST APIs for market data, indicators, watchlist, journal, AI."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

import asyncio

# ── /api/trading/analyze/stream SSE cache (Phase C, c1) ─────────────────
# Short-TTL per-ticker replay cache. Key hashes the *stable* inputs (user,
# ticker, interval, ai_context). Hit replays previously streamed text as
# SSE tokens so the UX stays identical. Non-deterministic pieces such as
# `_get_proposal_reminder` are not part of the key — callers that want a
# strictly fresh read can bypass with a query-string variation (future).
_ANALYZE_STREAM_CACHE_MAX = 128
_ANALYZE_STREAM_TTL_BY_INTERVAL = {
    "1m": 10,
    "5m": 10,
    "15m": 30,
    "30m": 30,
    "1h": 45,
    "1d": 90,
}
_ANALYZE_STREAM_TTL_DEFAULT = 45

_analyze_stream_lock = threading.Lock()
_analyze_stream_cache: "OrderedDict[str, tuple[float, str, str]]" = OrderedDict()
_analyze_stream_stats = {"hits": 0, "misses": 0}


def _analyze_stream_ttl(interval: str) -> int:
    return _ANALYZE_STREAM_TTL_BY_INTERVAL.get((interval or "").lower(), _ANALYZE_STREAM_TTL_DEFAULT)


def _analyze_stream_key(user_id: Any, ticker: str, interval: str, ai_context: str, user_msg: str) -> str:
    h = hashlib.sha256()
    h.update(str(user_id or "").encode())
    h.update(b"|")
    h.update((ticker or "").upper().encode())
    h.update(b"|")
    h.update((interval or "").lower().encode())
    h.update(b"|")
    h.update((ai_context or "").encode("utf-8", errors="ignore"))
    h.update(b"|")
    h.update((user_msg or "").encode("utf-8", errors="ignore"))
    return h.hexdigest()


def _analyze_stream_cache_get(key: str) -> tuple[str, str] | None:
    now = time.monotonic()
    with _analyze_stream_lock:
        entry = _analyze_stream_cache.get(key)
        if entry is None:
            _analyze_stream_stats["misses"] += 1
            return None
        expiry, text, model = entry
        if expiry < now:
            _analyze_stream_cache.pop(key, None)
            _analyze_stream_stats["misses"] += 1
            return None
        _analyze_stream_cache.move_to_end(key)
        _analyze_stream_stats["hits"] += 1
        return text, model


def _analyze_stream_cache_put(key: str, text: str, model: str, interval: str) -> None:
    if not text:
        return
    ttl = _analyze_stream_ttl(interval)
    expiry = time.monotonic() + ttl
    with _analyze_stream_lock:
        _analyze_stream_cache[key] = (expiry, text, model)
        _analyze_stream_cache.move_to_end(key)
        while len(_analyze_stream_cache) > _ANALYZE_STREAM_CACHE_MAX:
            _analyze_stream_cache.popitem(last=False)


def _analyze_stream_cache_stats() -> dict[str, Any]:
    with _analyze_stream_lock:
        hits = _analyze_stream_stats["hits"]
        misses = _analyze_stream_stats["misses"]
        size = len(_analyze_stream_cache)
    total = hits + misses
    return {
        "hits": hits,
        "misses": misses,
        "size": size,
        "hit_rate": round((hits / total) if total else 0.0, 4),
        "max_entries": _ANALYZE_STREAM_CACHE_MAX,
        "ttl_by_interval": _ANALYZE_STREAM_TTL_BY_INTERVAL,
    }


def _analyze_stream_chunk_text(text: str, chunk_size: int = 32):
    """Yield cached text as small SSE chunks so the UX mirrors a live stream."""
    for i in range(0, len(text), chunk_size):
        yield text[i : i + chunk_size]

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from ..models.trading import Trade

from ..deps import get_db, get_identity_ctx
from ..logger import log_info, new_trace_id
from ..prompts import load_prompt
from ..services import trading_service as ts
from ..services.trading.scanner import validate_live_prices as _smart_pick_validate_live_prices
from .trading_sub import (
    ai_router, backtest_router, broker_router, data_provider_router,
    inspect_router, momentum_api, monitor_router, operator_router, patterns_router,
    scanning_router, trades_router, web3_router,
)
from ..schemas.trading import (
    AnalyzeRequest,
    PickRecheckRequest,
    SmartPickRequest,
    WatchlistAdd,
)

router = APIRouter(tags=["trading"])
router.include_router(ai_router)
router.include_router(momentum_api.router)
router.include_router(inspect_router)
router.include_router(broker_router)
router.include_router(data_provider_router)
router.include_router(web3_router)
router.include_router(operator_router)
router.include_router(trades_router)
router.include_router(monitor_router)
router.include_router(backtest_router)
router.include_router(scanning_router)
router.include_router(patterns_router)

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


def _get_pattern_imminent_reminder(db: Session, ticker: str, user_id: int | None) -> str:
    """Build a user-message-level reminder about pattern-imminent alerts.

    Same strategy as _get_proposal_reminder: inject into the user message
    so the LLM cannot overlook it in a long system prompt.
    Scoped to the signed-in user (guests get no reminder).
    """
    import json as _json
    from datetime import datetime
    from ..models.trading import ScanPattern

    ticker_up = ticker.upper()
    alerts = ts.get_recent_pattern_imminent_alerts_for_user(db, user_id, ticker, limit=2)
    if not alerts:
        return ""

    a = alerts[0]
    age_h = (datetime.utcnow() - a.alerted_at).total_seconds() / 3600 if a.alerted_at else 0

    pat_name = "Unknown"
    pat_desc = ""
    conditions_text = ""
    win_rate = 0
    avg_ret = 0
    if a.scan_pattern_id:
        pat = db.get(ScanPattern, a.scan_pattern_id)
        if pat:
            pat_name = pat.name or f"Pattern #{pat.id}"
            pat_desc = pat.description or ""
            win_rate = (pat.win_rate or 0) * 100
            avg_ret = pat.avg_return_pct or 0
            rj = pat.rules_json
            if isinstance(rj, str):
                try:
                    rj = _json.loads(rj)
                except Exception:
                    rj = {}
            conds = (rj or {}).get("conditions", [])
            if conds:
                cond_lines = []
                for c in conds:
                    ind = c.get("indicator", "?")
                    op = c.get("op", "?")
                    val = c.get("value") if "value" in c else c.get("ref", "?")
                    cond_lines.append(f"  - {ind} {op} {val}")
                conditions_text = "\n".join(cond_lines)

    outcomes = [al.outcome or "pending" for al in alerts]
    wins = outcomes.count("winner")
    losses = outcomes.count("loser")

    lines = [
        f"CRITICAL — MY BRAIN DETECTED A PATTERN on {ticker_up}. "
        f"You MUST reference this in your analysis:",
        f"Pattern: \"{pat_name}\" (Win rate: {win_rate:.1f}%, Avg return: {avg_ret:.1f}%)",
    ]
    if pat_desc:
        lines.append(f"What it means: {pat_desc}")
    lines.append(
        f"Alert: {age_h:.0f}h ago at ${a.price_at_alert} | "
        f"Entry: ${a.entry_price} | Stop: ${a.stop_loss} | Target: ${a.target_price}"
    )
    if wins or losses:
        lines.append(f"Track record on {ticker_up}: {wins} winner(s), {losses} loser(s)")
    if conditions_text:
        lines.append(f"Pattern conditions (explain each in plain English):\n{conditions_text}")
    lines.append(
        "Use THESE entry/stop/target levels as your primary recommendation "
        "(adjusted for any price movement since the alert). "
        "Explain each condition so I understand why this pattern works."
    )
    return "\n".join(lines)


def _build_pattern_imminent_attach_payload(
    db: Session,
    user_id: int | None,
    ticker: str,
) -> dict[str, Any] | None:
    """SSE/JSON payload for linking the latest user imminent alert to an open trade (Monitor)."""
    if not user_id:
        return None
    from ..models.trading import ScanPattern

    alerts = ts.get_recent_pattern_imminent_alerts_for_user(db, user_id, ticker, limit=1)
    if not alerts:
        return None
    open_tr = _resolve_open_trade_for_ticker(db, user_id, ticker)
    if not open_tr:
        return None
    a = alerts[0]
    pat_name = "Scan pattern"
    if a.scan_pattern_id:
        pat = db.get(ScanPattern, a.scan_pattern_id)
        if pat and pat.name:
            pat_name = pat.name
    return {
        "alert_id": a.id,
        "ticker": (ticker or "").strip().upper(),
        "trade_id": open_tr.id,
        "pattern_name": pat_name,
        "already_linked": open_tr.related_alert_id == a.id,
    }


# ── Page ────────────────────────────────────────────────────────────────


def _trading_page_response(
    request: Request,
    db: Session,
    *,
    template_name: str,
    page_title: str,
    extra_context: dict | None = None,
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

    tmpl_ctx: dict = {
        "title": page_title,
        "is_guest": ctx["is_guest"],
        "user_name": ctx["user_name"],
        "avatar_url": avatar_url,
        "google_configured": google_configured,
    }
    if extra_context:
        tmpl_ctx.update(extra_context)

    return request.app.state.templates.TemplateResponse(
        request,
        template_name,
        tmpl_ctx,
    )


@router.get("/trading", response_class=HTMLResponse)
def trading_page(request: Request, db: Session = Depends(get_db)):
    return _trading_page_response(
        request, db, template_name="trading.html", page_title="Trading"
    )


@router.get("/trading/automation", response_class=HTMLResponse)
def trading_automation_page(request: Request, db: Session = Depends(get_db)):
    """Compatibility path for the Autopilot runtime surface."""
    return _trading_page_response(
        request,
        db,
        template_name="trading_autopilot.html",
        page_title="Trading Autopilot",
        extra_context={
            "automation_page": True,
            "autopilot_page": True,
            "automation_legacy_alias": True,
            "autopilot_route_path": "/trading/automation",
        },
    )


@router.get("/trading/autopilot", response_class=HTMLResponse)
def trading_autopilot_page(request: Request, db: Session = Depends(get_db)):
    """Simulation-first trading runtime surface for live operational reading."""
    return _trading_page_response(
        request,
        db,
        template_name="trading_autopilot.html",
        page_title="Trading Autopilot",
        extra_context={
            "automation_page": True,
            "autopilot_page": True,
            "automation_legacy_alias": False,
            "autopilot_route_path": "/trading/autopilot",
        },
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
    _is_crypto = ticker.strip().upper().endswith("-USD")
    fb = True if _is_crypto else _TRADING_UI_ALLOW_PROVIDER_FALLBACK
    try:
        data = ts.fetch_ohlcv(
            ticker,
            interval=interval,
            period=period,
            allow_provider_fallback=fb,
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
    items = ts.get_effective_watchlist(db, ctx["user_id"])
    return JSONResponse({"ok": True, "items": items})


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


# ── Trades, Journal, Stats — see trading_sub/trades.py ────────────────

# -- Trades, TCA, Journal, Stats -- see trading_sub/trades.py

# ── AI Analysis ─────────────────────────────────────────────────────────

_CHART_LEVELS_RE = re.compile(
    r"```json:chart_levels\s*\n(\{.*?\})\s*```",
    re.DOTALL,
)


def _normalize_chart_levels(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep only expected keys with numeric values for chart price lines."""
    if not raw or not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for k in ("entry", "stop", "sma_20", "sma_50", "sma_200", "vwap"):
        v = raw.get(k)
        if isinstance(v, (int, float)):
            fv = float(v)
            if fv == fv:  # not NaN
                out[k] = fv
    for k in ("targets", "support", "resistance"):
        v = raw.get(k)
        if isinstance(v, list):
            arr = []
            for x in v:
                if isinstance(x, (int, float)):
                    fx = float(x)
                    if fx == fx:
                        arr.append(fx)
            if arr:
                out[k] = arr
    return out or None


def _extract_chart_levels(text: str) -> dict[str, Any] | None:
    m = _CHART_LEVELS_RE.search(text or "")
    if not m:
        return None
    try:
        parsed = json.loads(m.group(1))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return _normalize_chart_levels(parsed if isinstance(parsed, dict) else None)


def _strip_chart_levels_block(text: str) -> str:
    return _CHART_LEVELS_RE.sub("", text or "").strip()


_TRADE_PLAN_LEVELS_RE = re.compile(
    r"```json:trade_plan_levels\s*\n(\{.*?\})\s*```",
    re.DOTALL,
)


def _normalize_trade_plan_levels(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Parse AI fence for apply-levels API (stop / targets / verdict / confidence)."""
    if not raw or not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for k in ("stop_loss", "take_profit", "take_profit_trim"):
        v = raw.get(k)
        if isinstance(v, (int, float)):
            fv = float(v)
            if fv == fv and fv > 0:
                out[k] = fv
    lab = raw.get("label") or raw.get("summary")
    if isinstance(lab, str) and lab.strip():
        out["label"] = lab.strip()[:500]

    _VALID_VERDICTS = {"buy", "sell", "hold", "add", "exit", "trim"}
    verdict = raw.get("verdict")
    if isinstance(verdict, str) and verdict.strip().lower() in _VALID_VERDICTS:
        out["verdict"] = verdict.strip().lower()
    conf = raw.get("confidence")
    if isinstance(conf, (int, float)) and 0 <= float(conf) <= 1:
        out["confidence"] = round(float(conf), 4)

    if not out.get("stop_loss") and not out.get("take_profit"):
        return None
    return out


def _extract_trade_plan_levels(text: str) -> dict[str, Any] | None:
    m = _TRADE_PLAN_LEVELS_RE.search(text or "")
    if not m:
        return None
    try:
        parsed = json.loads(m.group(1))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return _normalize_trade_plan_levels(parsed if isinstance(parsed, dict) else None)


def _strip_trade_plan_levels_block(text: str) -> str:
    return _TRADE_PLAN_LEVELS_RE.sub("", text or "").strip()


def _resolve_open_trade_for_ticker(db: Session, user_id: int | None, ticker: str) -> Trade | None:
    raw = (ticker or "").strip().upper()
    if raw.startswith("$"):
        raw = raw[1:]
    if not raw:
        return None
    q = (
        db.query(Trade)
        .filter(Trade.user_id == user_id, Trade.status == "open")
        .order_by(Trade.entry_date.desc())
    )
    for tr in q.all():
        if (tr.ticker or "").upper().replace("$", "") == raw:
            return tr
    return None


@router.post("/api/trading/analyze")
def api_analyze(body: AnalyzeRequest, request: Request, db: Session = Depends(get_db)):
    """AI-powered analysis of a ticker using indicators + journal context."""
    ctx = get_identity_ctx(request, db)
    trace_id = new_trace_id()

    ai_context = ts.build_ai_context(db, ctx["user_id"], body.ticker, body.interval)

    proposal_reminder = _get_proposal_reminder(db, body.ticker, ctx.get("user_id"))
    pattern_reminder = _get_pattern_imminent_reminder(db, body.ticker, ctx.get("user_id"))
    user_msg = body.message or f"Analyze {body.ticker} on the {body.interval} timeframe. Give me a clear verdict: should I buy, sell, or hold? Include exact entry price, stop-loss, targets, hold duration, and confidence level."
    if proposal_reminder:
        user_msg += "\n\n" + proposal_reminder
    if pattern_reminder:
        user_msg += "\n\n" + pattern_reminder

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

    annotations = _extract_chart_levels(reply) if isinstance(reply, str) else None
    if annotations:
        reply = _strip_chart_levels_block(reply)

    trade_plan_levels = _extract_trade_plan_levels(reply) if isinstance(reply, str) else None
    if trade_plan_levels:
        reply = _strip_trade_plan_levels_block(reply)
        open_tr = _resolve_open_trade_for_ticker(db, ctx.get("user_id"), body.ticker)
        if open_tr:
            trade_plan_levels = dict(trade_plan_levels)
            trade_plan_levels["trade_id"] = open_tr.id

    pattern_imminent_attach = _build_pattern_imminent_attach_payload(
        db, ctx.get("user_id"), body.ticker
    )

    return JSONResponse(
        {
            "ok": True,
            "reply": reply,
            "ticker": body.ticker,
            "annotations": annotations,
            "trade_plan_levels": trade_plan_levels,
            "pattern_imminent_attach": pattern_imminent_attach,
        }
    )


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
    pattern_reminder = _get_pattern_imminent_reminder(db, ticker, ctx.get("user_id"))
    user_msg = message or (
        f"Analyze {ticker} on the {interval} timeframe. "
        "Give me a clear verdict: should I buy, sell, or hold? "
        "Include exact entry price, stop-loss, targets, hold duration, and confidence level."
    )
    if proposal_reminder:
        user_msg += "\n\n" + proposal_reminder
    if pattern_reminder:
        user_msg += "\n\n" + pattern_reminder

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

    cache_key = _analyze_stream_key(ctx.get("user_id"), ticker, interval, ai_context, user_msg)

    def _generate():
        from .. import openai_client
        full_text_parts: list[str] = []
        cached_model: str | None = None

        cached = _analyze_stream_cache_get(cache_key)
        if cached is not None:
            cached_text, cached_model = cached
            log_info(trace_id, f"[analyze_stream] cache_hit ticker={ticker} interval={interval}")
            full_text_parts.append(cached_text)
            try:
                for chunk in _analyze_stream_chunk_text(cached_text):
                    yield f"data: {json.dumps({'token': chunk})}\n\n"
                full_text = cached_text
                ann = _extract_chart_levels(full_text)
                if ann:
                    yield f"data: {json.dumps({'annotations': ann})}\n\n"
                tplan = _extract_trade_plan_levels(full_text)
                if tplan:
                    open_tr = _resolve_open_trade_for_ticker(db, ctx.get("user_id"), ticker)
                    payload = dict(tplan)
                    payload["ticker"] = ticker
                    if open_tr:
                        payload["trade_id"] = open_tr.id
                    yield f"data: {json.dumps({'trade_plan_levels': payload})}\n\n"
                patt_attach = _build_pattern_imminent_attach_payload(
                    db, ctx.get("user_id"), ticker
                )
                if patt_attach:
                    yield f"data: {json.dumps({'pattern_imminent_attach': patt_attach})}\n\n"
                yield "data: [DONE]\n\n"
                return
            except Exception as e:
                log_info(trace_id, f"[analyze_stream] cache replay error: {e}")

        try:
            _stream_had_token = False
            live_model: str | None = None
            for tok, model in openai_client.chat_stream(
                messages=messages,
                system_prompt=system_prompt,
                trace_id=trace_id,
                user_message=user_msg,
                max_tokens=2048,
            ):
                _stream_had_token = True
                live_model = model
                full_text_parts.append(tok)
                yield f"data: {json.dumps({'token': tok})}\n\n"
            if not _stream_had_token:
                _empty = (
                    "*No analysis text was returned.* The AI stream completed without content. "
                    "Please try again — if it keeps happening, check provider status or try a shorter question."
                )
                full_text_parts.append(_empty)
                yield f"data: {json.dumps({'token': _empty})}\n\n"
            full_text = "".join(full_text_parts)
            if _stream_had_token and full_text and live_model:
                try:
                    _analyze_stream_cache_put(cache_key, full_text, live_model, interval)
                except Exception as e:
                    log_info(trace_id, f"[analyze_stream] cache store error: {e}")
            ann = _extract_chart_levels(full_text)
            if ann:
                yield f"data: {json.dumps({'annotations': ann})}\n\n"
            tplan = _extract_trade_plan_levels(full_text)
            if tplan:
                open_tr = _resolve_open_trade_for_ticker(db, ctx.get("user_id"), ticker)
                payload = dict(tplan)
                payload["ticker"] = ticker
                if open_tr:
                    payload["trade_id"] = open_tr.id
                yield f"data: {json.dumps({'trade_plan_levels': payload})}\n\n"
            patt_attach = _build_pattern_imminent_attach_payload(
                db, ctx.get("user_id"), ticker
            )
            if patt_attach:
                yield f"data: {json.dumps({'pattern_imminent_attach': patt_attach})}\n\n"
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


# -- Backtest -- see trading_sub/backtest.py
# -- Scanners, Screener, Portfolio, Signals, Top Picks -- see trading_sub/scanning.py


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
    from ..services.trading.alert_formatter import format_test_alert

    sms_status = get_sms_status()
    if not sms_status["configured"]:
        return JSONResponse({"ok": False, "error": "SMS not configured. Set SMS_PHONE and SMS_CARRIER (or Twilio) in .env"})

    msg = format_test_alert()
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


# ── Stop Engine ──────────────────────────────────────────────────────

@router.get("/api/trading/stops/positions")
def api_stop_positions(
    request: Request,
    db: Session = Depends(get_db),
):
    """Return all open positions with stop-engine state for the UI."""
    ctx = get_identity_ctx(request, db)
    from ..models.trading import Trade
    trades = db.query(Trade).filter(
        Trade.user_id == ctx["user_id"],
        Trade.status == "open",
    ).order_by(Trade.entry_date.desc()).all()

    from ..services.trading.stop_engine import _build_brain_context

    result = []
    for t in trades:
        entry = t.entry_price or 0
        stop = t.stop_loss
        target = t.take_profit
        R = abs(entry - stop) if stop and entry else 0
        hwm = t.high_watermark or entry

        try:
            from ..services.trading.market_data import fetch_quote
            q = fetch_quote(t.ticker)
            price = q.get("price", 0) if q else 0
        except Exception:
            price = 0

        current_r = 0
        if R > 0 and price and entry:
            if t.direction == "long":
                current_r = round((price - entry) / R, 2)
            else:
                current_r = round((entry - price) / R, 2)

        stop_distance_pct = 0
        if stop and price and price > 0:
            stop_distance_pct = round(abs(price - stop) / price * 100, 2)

        pnl_pct = 0
        if entry and price and entry > 0:
            if t.direction == "long":
                pnl_pct = round((price - entry) / entry * 100, 2)
            else:
                pnl_pct = round((entry - price) / entry * 100, 2)

        state = "initial"
        if current_r >= 2.0:
            state = "trailing"
        elif current_r >= 1.0:
            state = "breakeven"
        if stop and price:
            if (t.direction == "long" and price <= stop) or (t.direction != "long" and price >= stop):
                state = "triggered"
            elif R > 0 and abs(price - stop) / R <= 0.25:
                state = "warn"

        brain_ctx = {}
        try:
            brain = _build_brain_context(t, db)
            brain_ctx = brain.summary_dict()
        except Exception:
            pass

        result.append({
            "id": t.id,
            "ticker": t.ticker,
            "direction": t.direction,
            "entry_price": entry,
            "current_price": price,
            "stop_loss": stop,
            "take_profit": target,
            "trail_stop": t.trail_stop,
            "high_watermark": hwm,
            "stop_model": t.stop_model,
            "quantity": t.quantity,
            "broker_source": t.broker_source,
            "R": round(R, 4),
            "current_r": current_r,
            "stop_distance_pct": stop_distance_pct,
            "pnl_pct": pnl_pct,
            "state": state,
            "entry_date": t.entry_date.isoformat() if t.entry_date else None,
            "brain": brain_ctx,
        })

    return JSONResponse({"ok": True, "positions": result})


@router.get("/api/trading/stops/decisions")
def api_stop_decisions(
    request: Request,
    db: Session = Depends(get_db),
    trade_id: int | None = Query(None),
    limit: int = Query(50),
):
    """Return recent stop decisions (audit trail)."""
    ctx = get_identity_ctx(request, db)
    from ..models.trading import StopDecision, Trade
    q = db.query(StopDecision).join(Trade, StopDecision.trade_id == Trade.id)
    if trade_id:
        q = q.filter(StopDecision.trade_id == trade_id)
    q = q.filter(Trade.user_id == ctx["user_id"])
    decisions = q.order_by(StopDecision.as_of_ts.desc()).limit(limit).all()

    result = []
    for d in decisions:
        result.append({
            "id": d.id,
            "trade_id": d.trade_id,
            "as_of_ts": d.as_of_ts.isoformat() if d.as_of_ts else None,
            "state": d.state,
            "old_stop": d.old_stop,
            "new_stop": d.new_stop,
            "trigger": d.trigger,
            "reason": d.reason,
            "executed": d.executed,
        })
    return JSONResponse({"ok": True, "decisions": result})


@router.post("/api/trading/stops/evaluate")
def api_stop_evaluate(
    request: Request,
    db: Session = Depends(get_db),
):
    """Manually trigger the stop engine for the current user."""
    ctx = get_identity_ctx(request, db)
    from ..services.trading.stop_engine import evaluate_all, dispatch_stop_alerts
    summary = evaluate_all(db, ctx["user_id"])
    dispatched = dispatch_stop_alerts(db, ctx["user_id"], summary)
    summary["dispatched"] = dispatched
    summary.pop("alerts", None)
    return JSONResponse({"ok": True, **summary})


@router.get("/api/trading/stops/review")
def api_stop_review(
    request: Request,
    db: Session = Depends(get_db),
    hours: int = Query(48),
):
    """Self-critical review of past stop alert outcomes."""
    get_identity_ctx(request, db)
    from ..services.trading.stop_engine import review_alert_outcomes
    review = review_alert_outcomes(db, lookback_hours=hours)
    return JSONResponse({"ok": True, **review})


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


# -- Pattern Engine -- see trading_sub/patterns.py

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
async def ws_trading_live(ws: WebSocket, ticker: str = "AAPL", interval: int = 0):
    """Stream real-time price ticks for *ticker* and broadcast alerts globally.

    When *interval* > 0 (seconds), also streams ``candle`` events on bucket close.
    """
    await ws.accept()
    logger.info("[live-ws] Client connected for %s (candle_interval=%d)", ticker, interval)

    with _live_clients_tlock:
        _live_clients.add(ws)

    tick_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    massive_available = False
    m_ticker = ""
    _on_tick = None
    _on_candle = None

    try:
        from ..services.massive_client import (
            get_ws_client,
            register_tick_listener,
            unregister_tick_listener,
            to_massive_ticker,
            QuoteSnapshot,
            TradeSnapshot,
            register_candle_listener,
            unregister_candle_listener,
            OHLCVBar,
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

        _on_candle = None
        if interval > 0:
            def _on_candle_cb(sym: str, bar: OHLCVBar):
                try:
                    tick_queue.put_nowait({
                        "type": "candle",
                        "o": bar.open, "h": bar.high, "l": bar.low, "c": bar.close,
                        "v": bar.volume, "t": bar.bucket_start, "interval": bar.interval_seconds,
                        "trades": bar.trade_count,
                    })
                except Exception:
                    pass
            _on_candle = _on_candle_cb

        _on_tick = _on_tick_cb
        ws_client = get_ws_client()
        if ws_client.running:
            ws_client.subscribe([m_ticker])
            register_tick_listener(m_ticker, _on_tick)
            if _on_candle and interval > 0:
                register_candle_listener(m_ticker, interval, _on_candle)
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
                from ..services.massive_client import unregister_tick_listener, unregister_candle_listener
                unregister_tick_listener(m_ticker, _on_tick)
                if interval > 0 and _on_candle:
                    unregister_candle_listener(m_ticker, interval, _on_candle)
            except Exception:
                pass
        with _live_clients_tlock:
            _live_clients.discard(ws)
        logger.info("[live-ws] Client disconnected for %s", ticker)


# ── Autopilot streaming WebSocket ────────────────────────────────────────

@router.websocket("/ws/autopilot/live")
async def ws_autopilot_live(ws: WebSocket, symbols: str = ""):
    """Stream real-time ticks + 1m candles for autopilot session symbols.

    Query params:
      symbols — comma-separated list of symbols to stream (e.g. "BTC-USD,LINK-USD")
    """
    await ws.accept()
    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not sym_list:
        await ws.send_json({"type": "error", "message": "No symbols specified"})
        await ws.close()
        return

    logger.info("[autopilot-ws] Client connected for %s", sym_list)

    tick_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    registered_cbs: list[tuple[str, Any, str]] = []  # (symbol, callback, type)
    bus_available = False

    try:
        from ..config import settings as _settings
        if _settings.chili_autopilot_price_bus_enabled:
            from ..services.trading.price_bus import get_price_bus, BusQuote, BusCandle
            bus = get_price_bus()
            bus_available = True

            def _make_tick_cb(sym: str):
                def _on_tick(_symbol: str, quote: BusQuote) -> None:
                    try:
                        tick_queue.put_nowait({
                            "type": "tick",
                            "symbol": sym,
                            "bid": quote.bid,
                            "ask": quote.ask,
                            "mid": quote.mid,
                            "last": quote.last,
                            "time": quote.timestamp,
                            "source": quote.source,
                        })
                    except asyncio.QueueFull:
                        pass
                return _on_tick

            def _make_candle_cb(sym: str):
                def _on_candle(_symbol: str, candle: BusCandle) -> None:
                    try:
                        tick_queue.put_nowait({
                            "type": "candle",
                            "symbol": sym,
                            "o": candle.open,
                            "h": candle.high,
                            "l": candle.low,
                            "c": candle.close,
                            "v": candle.volume,
                            "t": candle.bucket_start,
                            "interval": "1m",
                            "trades": candle.trade_count,
                        })
                    except asyncio.QueueFull:
                        pass
                return _on_candle

            for sym in sym_list:
                tick_cb = _make_tick_cb(sym)
                candle_cb = _make_candle_cb(sym)
                bus.subscribe_symbol(sym)
                bus.register_tick_listener(sym, tick_cb)
                bus.register_candle_listener(sym, candle_cb)
                registered_cbs.append((sym, tick_cb, "tick"))
                registered_cbs.append((sym, candle_cb, "candle"))

    except Exception as e:
        logger.debug("[autopilot-ws] Price bus setup failed: %s", e)

    if not bus_available:
        # Fallback: poll fetch_quote every 5 seconds
        async def _poll_fallback():
            while True:
                await asyncio.sleep(5.0)
                for sym in sym_list:
                    try:
                        q = ts.fetch_quote(sym)
                        if q and q.get("price"):
                            try:
                                tick_queue.put_nowait({
                                    "type": "tick",
                                    "symbol": sym,
                                    "bid": q.get("bid"),
                                    "ask": q.get("ask"),
                                    "mid": q.get("price"),
                                    "last": q.get("price"),
                                    "time": __import__("time").time(),
                                    "source": q.get("source", "rest_poll"),
                                })
                            except asyncio.QueueFull:
                                pass
                    except Exception:
                        pass

    poll_task = None
    if not bus_available:
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
                    text = await ws.receive_text()
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
        logger.warning("[autopilot-ws] Handler error: %s", exc)
    finally:
        if poll_task:
            poll_task.cancel()
        if bus_available:
            try:
                from ..services.trading.price_bus import get_price_bus
                bus = get_price_bus()
                for sym, cb, cb_type in registered_cbs:
                    if cb_type == "tick":
                        bus.unregister_tick_listener(sym, cb)
                    elif cb_type == "candle":
                        bus.unregister_candle_listener(sym, cb)
            except Exception:
                pass
        logger.info("[autopilot-ws] Client disconnected for %s", sym_list)
