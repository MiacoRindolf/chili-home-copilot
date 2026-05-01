"""Fast-path read API — paper trades + live P/L for the autopilot dashboard.

Read-only endpoints serving:
  - GET /api/trading/fast-path/paper-trades : open paper positions with
    current floating P/L computed against the most recent fast_orderbook
    mid for the same ticker.
  - GET /api/trading/fast-path/recent-decisions : decision feed (mix
    of fills and rejects, with reasons).
  - GET /api/trading/fast-path/summary : one-shot aggregate (total
    fills, rejects today, daily notional used, mode + live_authorized
    flag) for the page header card.

These never touch the executor in-memory state — the autopilot UI is
served by the chili web container, which is a *separate* process from
fast-data-worker. So we read from Postgres only. The fast_executions
+ fast_orderbook tables are the source of truth.

Mode/live authorization:
  - "mode" comes from the executor process env at the moment of each
    decision; we pull it from the most recent fast_executions row.
  - The page should make it visually impossible to confuse paper with
    live — the response includes a top-level mode field; the template
    must badge it loud.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ...db import engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trading/fast-path", tags=["fast-path"])


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_today_start() -> datetime:
    n = datetime.now(timezone.utc)
    return datetime(n.year, n.month, n.day, 0, 0, 0)


# ── Helpers ───────────────────────────────────────────────────────────


def _fetch_open_paper_positions(limit: int = 200) -> list[dict[str, Any]]:
    """Every paper_fill row whose execution hasn't been (yet) marked
    closed by F5. F5 isn't shipped — so for now, every paper_fill is
    treated as still open. When F5 lands and writes exit rows to
    fast_executions, swap this for a NOT EXISTS subquery."""
    sql = text("""
        SELECT id, ticker, alert_type, side, quantity, fill_price,
               notional_usd, latency_ms, decided_at
        FROM fast_executions
        WHERE decision = 'paper_fill'
          AND mode = 'paper'
        ORDER BY decided_at DESC
        LIMIT :lim
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"lim": int(limit)}).mappings().all()
    return [dict(r) for r in rows]


def _fetch_latest_book_mid_per_ticker(tickers: list[str]) -> dict[str, dict[str, float]]:
    """For each ticker, grab the most-recent fast_orderbook row and
    compute the mid. Used to estimate floating P/L on paper positions.

    Returns ``{ticker: {"mid": float, "best_bid": float, "best_ask":
    float, "spread_bps": float, "snapshot_at": datetime}}``. Tickers
    with no recent book are simply absent.
    """
    if not tickers:
        return {}
    # Distinct-on-ticker latest-row trick. Postgres `DISTINCT ON` reads
    # one row per group sorted by (ticker, snapshot_at DESC).
    sql = text("""
        SELECT DISTINCT ON (ticker)
            ticker,
            snapshot_at,
            bid_levels,
            ask_levels,
            spread_bps
        FROM fast_orderbook
        WHERE ticker = ANY(:tickers)
          AND snapshot_at > NOW() - INTERVAL '5 minutes'
        ORDER BY ticker, snapshot_at DESC
    """)
    out: dict[str, dict[str, float]] = {}
    with engine.connect() as conn:
        rows = conn.execute(sql, {"tickers": list(tickers)}).mappings().all()
        for row in rows:
            bid_levels = row["bid_levels"] or []
            ask_levels = row["ask_levels"] or []
            best_bid = float(bid_levels[0][0]) if bid_levels else 0.0
            best_ask = float(ask_levels[0][0]) if ask_levels else 0.0
            mid = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else 0.0
            out[row["ticker"]] = {
                "mid": mid,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread_bps": float(row["spread_bps"] or 0.0),
                "snapshot_at": row["snapshot_at"],
            }
    return out


def _fetch_today_aggregates() -> dict[str, Any]:
    sql = text("""
        SELECT
            COUNT(*) FILTER (WHERE decision = 'paper_fill') AS paper_fills,
            COUNT(*) FILTER (WHERE decision = 'rejected')   AS rejected,
            COUNT(*) FILTER (WHERE decision = 'live_placed') AS live_placed,
            COALESCE(SUM(CASE WHEN decision = 'paper_fill'
                              THEN notional_usd ELSE 0 END), 0) AS paper_notional_usd,
            MAX(decided_at) AS last_decision_at,
            -- Mode pulled from the most recent decision row -- single
            -- column trick: ``string_agg(... order by ... limit 1)``
            -- isn't easy in plain agg, so do a separate query above
            -- instead. We'll fetch mode below.
            MIN(decided_at) AS first_decision_at
        FROM fast_executions
        WHERE decided_at >= :since
    """)
    with engine.connect() as conn:
        row = conn.execute(sql, {"since": _utc_today_start()}).mappings().one_or_none()
        mode_row = conn.execute(text("""
            SELECT mode FROM fast_executions
            ORDER BY decided_at DESC LIMIT 1
        """)).mappings().one_or_none()
    if row is None:
        return {
            "paper_fills": 0, "rejected": 0, "live_placed": 0,
            "paper_notional_usd": 0.0,
            "last_decision_at": None, "first_decision_at": None,
            "mode": (mode_row or {}).get("mode") or "paper",
        }
    out = dict(row)
    out["paper_notional_usd"] = float(out.get("paper_notional_usd") or 0.0)
    out["mode"] = (mode_row or {}).get("mode") or "paper"
    return out


def _is_live_authorized() -> bool:
    """Mirror ``app.services.trading.fast_path.gates.is_live_authorized``
    but read directly from this process's env. The web process and the
    fast-data-worker process see different envs in principle; we
    surface the WEB process's view here so the badge reflects the
    operator's intent. The actual placement still uses the worker's
    env, so a mismatch is a real misconfiguration that shows up in the
    decision log as ``mode_live_but_not_authorized_at_place``."""
    raw = (os.environ.get("CHILI_FAST_PATH_EXEC_LIVE_AUTHORIZED") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ── Endpoints ─────────────────────────────────────────────────────────


@router.get("/paper-trades")
def get_paper_trades(limit: int = Query(50, ge=1, le=500)) -> JSONResponse:
    """Open paper positions with current floating P/L.

    The autopilot dashboard polls this every few seconds. Keep it
    cheap — two queries (fills + book lookup), no joins to multi-day
    history.
    """
    fills = _fetch_open_paper_positions(limit=limit)
    tickers = sorted({f["ticker"] for f in fills})
    books = _fetch_latest_book_mid_per_ticker(tickers)

    open_positions: list[dict[str, Any]] = []
    total_notional_in = 0.0
    total_unrealized_pnl_usd = 0.0
    for f in fills:
        ticker = f["ticker"]
        book = books.get(ticker) or {}
        entry_px = float(f.get("fill_price") or 0.0)
        qty = float(f.get("quantity") or 0.0)
        side = (f.get("side") or "buy").lower()
        notional_in = float(f.get("notional_usd") or 0.0)
        # For now executor only opens long; ``side=='buy'`` mid - entry
        # is the unrealized gain. Defensive on side != buy.
        mid = float(book.get("mid") or 0.0)
        if entry_px > 0 and qty > 0 and mid > 0 and side == "buy":
            unrealized_pnl_usd = (mid - entry_px) * qty
            unrealized_pct = (mid - entry_px) / entry_px
        else:
            unrealized_pnl_usd = 0.0
            unrealized_pct = 0.0
        total_notional_in += notional_in
        total_unrealized_pnl_usd += unrealized_pnl_usd
        open_positions.append({
            "id": f["id"],
            "ticker": ticker,
            "alert_type": f["alert_type"],
            "side": side,
            "quantity": qty,
            "entry_price": entry_px,
            "current_mid": mid,
            "best_bid": float(book.get("best_bid") or 0.0),
            "best_ask": float(book.get("best_ask") or 0.0),
            "spread_bps": float(book.get("spread_bps") or 0.0),
            "notional_in_usd": notional_in,
            "unrealized_pnl_usd": float(unrealized_pnl_usd),
            "unrealized_pct": float(unrealized_pct),
            "decided_at": f["decided_at"].isoformat() if f.get("decided_at") else None,
            "book_snapshot_at": (
                book.get("snapshot_at").isoformat()
                if book.get("snapshot_at") else None
            ),
        })

    aggregate = _fetch_today_aggregates()
    summary = {
        "open_count": len(open_positions),
        "total_notional_in_usd": float(total_notional_in),
        "total_unrealized_pnl_usd": float(total_unrealized_pnl_usd),
        "total_unrealized_pct": (
            float(total_unrealized_pnl_usd / total_notional_in)
            if total_notional_in > 0 else 0.0
        ),
        "today_paper_fills": int(aggregate.get("paper_fills") or 0),
        "today_rejected": int(aggregate.get("rejected") or 0),
        "today_live_placed": int(aggregate.get("live_placed") or 0),
        "today_paper_notional_usd": aggregate["paper_notional_usd"],
        "mode": aggregate["mode"],
        "live_authorized": _is_live_authorized(),
        "as_of": _utc_now_naive().isoformat(),
    }
    return JSONResponse({
        "open_positions": open_positions,
        "summary": summary,
    })


@router.get("/recent-decisions")
def get_recent_decisions(limit: int = Query(50, ge=1, le=200),
                         minutes: int = Query(60, ge=1, le=1440)) -> JSONResponse:
    """Most recent decisions across all decision types (paper_fill +
    rejected + live_placed). For the activity feed on the dashboard."""
    since = _utc_now_naive() - timedelta(minutes=int(minutes))
    sql = text("""
        SELECT id, ticker, alert_type, decision, reject_reason, mode,
               side, quantity, fill_price, notional_usd, latency_ms,
               decided_at
        FROM fast_executions
        WHERE decided_at >= :since
        ORDER BY decided_at DESC
        LIMIT :lim
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"since": since, "lim": int(limit)}).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("decided_at"):
            d["decided_at"] = d["decided_at"].isoformat()
        out.append(d)
    return JSONResponse({"decisions": out, "since": since.isoformat()})


@router.get("/summary")
def get_summary() -> JSONResponse:
    """Lightweight header summary — no positions list, just counters
    and mode badge. Cheap enough to poll once a second from the page."""
    aggregate = _fetch_today_aggregates()
    fills = _fetch_open_paper_positions(limit=500)
    tickers = sorted({f["ticker"] for f in fills})
    books = _fetch_latest_book_mid_per_ticker(tickers)
    total_notional_in = 0.0
    total_unrealized = 0.0
    for f in fills:
        notional = float(f.get("notional_usd") or 0.0)
        entry = float(f.get("fill_price") or 0.0)
        qty = float(f.get("quantity") or 0.0)
        mid = float((books.get(f["ticker"]) or {}).get("mid") or 0.0)
        total_notional_in += notional
        if entry > 0 and qty > 0 and mid > 0 and (f.get("side") or "").lower() == "buy":
            total_unrealized += (mid - entry) * qty
    return JSONResponse({
        "mode": aggregate.get("mode") or "paper",
        "live_authorized": _is_live_authorized(),
        "today_paper_fills": int(aggregate.get("paper_fills") or 0),
        "today_rejected": int(aggregate.get("rejected") or 0),
        "today_live_placed": int(aggregate.get("live_placed") or 0),
        "today_paper_notional_usd": aggregate["paper_notional_usd"],
        "open_count": len(fills),
        "total_notional_in_usd": float(total_notional_in),
        "total_unrealized_pnl_usd": float(total_unrealized),
        "total_unrealized_pct": (
            float(total_unrealized / total_notional_in)
            if total_notional_in > 0 else 0.0
        ),
        "as_of": _utc_now_naive().isoformat(),
    })


__all__ = ["router"]
