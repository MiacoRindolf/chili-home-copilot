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
    """Every paper_fill row that has not yet been closed by F5.

    F5's exit_manager writes to fast_exits with one row per closed
    entry. An entry is "open" iff no fast_exits row references its id.
    """
    sql = text("""
        SELECT e.id, e.ticker, e.alert_type, e.side, e.quantity,
               e.fill_price, e.notional_usd, e.latency_ms, e.decided_at
        FROM fast_executions e
        LEFT JOIN fast_exits x
          ON x.entry_execution_id = e.id
        WHERE e.decision = 'paper_fill'
          AND e.mode = 'paper'
          AND x.id IS NULL
        ORDER BY e.decided_at DESC
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


@router.get("/closed-trades")
def get_closed_trades(
    limit: int = Query(50, ge=1, le=200),
    include_inherited: bool = Query(False),
) -> JSONResponse:
    """Recent closed fast-path round trips (one row per fast_exits row).

    Defaults to F5-native trades only (uses the ``fast_exits_native``
    view from migration 219). Set ``include_inherited=true`` to include
    F4-era bootstrap-adopted positions whose brackets were computed
    at F5 boot rather than at entry time -- useful for a complete
    history view but contaminates P/L analysis if mixed with native.

    The ``is_native`` field on each row lets the UI color-code
    inherited rows; on the native-only view it's always true, on the
    all-inclusive view it's derived from the bracket-age gap.
    """
    if include_inherited:
        # Source = base table; classifier computed inline via the
        # ``computed_at - entered_at`` gap that migration 219's view
        # documents. < 60s => native; otherwise inherited.
        sql = text("""
            SELECT x.id, x.entry_execution_id, x.ticker, e.alert_type,
                   x.side, x.quantity, x.entry_price, x.exit_price,
                   x.exit_reason, x.realized_pnl_usd, x.realized_return_pct,
                   x.holding_period_s, x.stop_at_entry, x.target_at_entry,
                   x.entered_at, x.exited_at, x.mode,
                   COALESCE(
                     (x.brain_json ? 'computed_at') AND
                     EXTRACT(EPOCH FROM (
                       (x.brain_json->>'computed_at')::timestamp - x.entered_at
                     )) < 60,
                     FALSE
                   ) AS is_native
            FROM fast_exits x
            LEFT JOIN fast_executions e ON e.id = x.entry_execution_id
            ORDER BY x.exited_at DESC
            LIMIT :lim
        """)
    else:
        sql = text("""
            SELECT x.id, x.entry_execution_id, x.ticker, e.alert_type,
                   x.side, x.quantity, x.entry_price, x.exit_price,
                   x.exit_reason, x.realized_pnl_usd, x.realized_return_pct,
                   x.holding_period_s, x.stop_at_entry, x.target_at_entry,
                   x.entered_at, x.exited_at, x.mode,
                   TRUE AS is_native
            FROM fast_exits_native x
            LEFT JOIN fast_executions e ON e.id = x.entry_execution_id
            ORDER BY x.exited_at DESC
            LIMIT :lim
        """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"lim": int(limit)}).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        for ts_col in ("entered_at", "exited_at"):
            if d.get(ts_col):
                d[ts_col] = d[ts_col].isoformat()
        # Keep numeric types JSON-friendly; psycopg returns Decimal for
        # DOUBLE PRECISION sometimes, float for others. Coerce.
        for num_col in ("quantity", "entry_price", "exit_price",
                        "realized_pnl_usd", "realized_return_pct",
                        "holding_period_s", "stop_at_entry",
                        "target_at_entry"):
            if d.get(num_col) is not None:
                d[num_col] = float(d[num_col])
        d["is_native"] = bool(d.get("is_native"))
        out.append(d)
    return JSONResponse({
        "trades": out,
        "include_inherited": bool(include_inherited),
        "as_of": _utc_now_naive().isoformat(),
    })


@router.get("/realized-stats")
def get_realized_stats(
    include_inherited: bool = Query(False),
    since_hours: int = Query(24, ge=1, le=24 * 30),
) -> JSONResponse:
    """Aggregate realized P/L over closed fast-path round trips.

    Same native-vs-all-inclusive selector as ``/closed-trades``. The
    ``since_hours`` window is rolling: rows whose ``exited_at`` is
    within the last N hours.

    ``by_reason`` always emits the three canonical exit-reason keys
    (``stop_hit``, ``target_hit``, ``time_stop``) so the UI doesn't
    have to handle missing keys; reasons outside that set ('manual',
    'broker_error') are aggregated into a separate ``other`` bucket.
    """
    source = "fast_exits" if include_inherited else "fast_exits_native"
    since_dt = _utc_now_naive() - timedelta(hours=int(since_hours))

    # One pass for headline stats; one pass each for the by_reason
    # and by_ticker breakouts. All three queries are cheap on the
    # (ticker, exited_at DESC) and (exit_reason, exited_at DESC)
    # indexes already on fast_exits.
    headline_sql = text(f"""
        SELECT
            COUNT(*) AS round_trips,
            COUNT(*) FILTER (WHERE realized_pnl_usd > 0) AS wins,
            COUNT(*) FILTER (WHERE realized_pnl_usd <= 0) AS losses,
            COALESCE(SUM(realized_pnl_usd), 0) AS total_pnl_usd,
            AVG(realized_return_pct) AS avg_return_pct,
            AVG(holding_period_s) AS avg_holding_s,
            MAX(realized_pnl_usd) AS best_trade_pnl_usd,
            MIN(realized_pnl_usd) AS worst_trade_pnl_usd
        FROM {source}
        WHERE exited_at >= :since
    """)
    by_reason_sql = text(f"""
        SELECT exit_reason,
               COUNT(*) AS n,
               COALESCE(SUM(realized_pnl_usd), 0) AS pnl_usd
        FROM {source}
        WHERE exited_at >= :since
        GROUP BY exit_reason
    """)
    by_ticker_sql = text(f"""
        SELECT ticker,
               COUNT(*) AS n,
               COALESCE(SUM(realized_pnl_usd), 0) AS pnl_usd
        FROM {source}
        WHERE exited_at >= :since
        GROUP BY ticker
        ORDER BY n DESC
    """)
    with engine.connect() as conn:
        headline = conn.execute(headline_sql, {"since": since_dt}).mappings().one_or_none() or {}
        by_reason_rows = conn.execute(by_reason_sql, {"since": since_dt}).mappings().all()
        by_ticker_rows = conn.execute(by_ticker_sql, {"since": since_dt}).mappings().all()

    rt = int(headline.get("round_trips") or 0)
    wins = int(headline.get("wins") or 0)
    losses = int(headline.get("losses") or 0)
    total_pnl = float(headline.get("total_pnl_usd") or 0.0)

    # by_reason: always include the three primary exit reasons even at zero.
    canonical = ("stop_hit", "target_hit", "time_stop")
    by_reason: dict[str, dict[str, float]] = {
        k: {"count": 0, "total_pnl_usd": 0.0} for k in canonical
    }
    other_count = 0
    other_pnl = 0.0
    for row in by_reason_rows:
        reason = row["exit_reason"]
        if reason in by_reason:
            by_reason[reason]["count"] = int(row["n"])
            by_reason[reason]["total_pnl_usd"] = float(row["pnl_usd"] or 0.0)
        else:
            other_count += int(row["n"])
            other_pnl += float(row["pnl_usd"] or 0.0)
    if other_count:
        by_reason["other"] = {"count": other_count, "total_pnl_usd": other_pnl}

    by_ticker = {
        row["ticker"]: {
            "count": int(row["n"]),
            "total_pnl_usd": float(row["pnl_usd"] or 0.0),
        }
        for row in by_ticker_rows
    }

    return JSONResponse({
        "round_trips": rt,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": (round(100.0 * wins / rt, 2) if rt > 0 else 0.0),
        "total_pnl_usd": total_pnl,
        "avg_return_pct": (
            float(headline["avg_return_pct"])
            if headline.get("avg_return_pct") is not None else 0.0
        ),
        "avg_holding_s": (
            float(headline["avg_holding_s"])
            if headline.get("avg_holding_s") is not None else 0.0
        ),
        "best_trade_pnl_usd": (
            float(headline["best_trade_pnl_usd"])
            if headline.get("best_trade_pnl_usd") is not None else 0.0
        ),
        "worst_trade_pnl_usd": (
            float(headline["worst_trade_pnl_usd"])
            if headline.get("worst_trade_pnl_usd") is not None else 0.0
        ),
        "by_reason": by_reason,
        "by_ticker": by_ticker,
        "since_hours": int(since_hours),
        "include_inherited": bool(include_inherited),
        "as_of": _utc_now_naive().isoformat(),
    })


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
