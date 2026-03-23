"""Transaction cost analysis (TCA): reference price vs fill, slippage bps, aggregates."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def entry_slippage_bps(
    reference_price: float,
    fill_price: float,
    direction: str = "long",
) -> float | None:
    """Slippage in basis points: positive = paid more (long) / worse fill."""
    if reference_price is None or fill_price is None:
        return None
    try:
        ref = float(reference_price)
        fil = float(fill_price)
    except (TypeError, ValueError):
        return None
    if ref <= 0 or fil <= 0:
        return None
    d = (direction or "long").strip().lower()
    if d == "short":
        return round((ref - fil) / ref * 10000.0, 2)
    return round((fil - ref) / ref * 10000.0, 2)


def apply_tca_on_trade_fill(trade) -> None:
    """Set ``tca_entry_slippage_bps`` when reference and fill prices exist."""
    ref = getattr(trade, "tca_reference_entry_price", None)
    fill = getattr(trade, "avg_fill_price", None) or getattr(trade, "entry_price", None)
    if ref is None or fill is None:
        return
    bps = entry_slippage_bps(ref, float(fill), getattr(trade, "direction", None) or "long")
    if bps is not None:
        trade.tca_entry_slippage_bps = bps


def exit_slippage_bps(
    reference_price: float,
    fill_price: float,
    direction: str = "long",
) -> float | None:
    """Exit slippage in bps. Long exit: positive = received less than ref (worse)."""
    if reference_price is None or fill_price is None:
        return None
    try:
        ref = float(reference_price)
        fil = float(fill_price)
    except (TypeError, ValueError):
        return None
    if ref <= 0 or fil <= 0:
        return None
    d = (direction or "long").strip().lower()
    if d == "short":
        # Cover short: paid more than ref -> worse
        return round((fil - ref) / ref * 10000.0, 2)
    return round((ref - fil) / ref * 10000.0, 2)


def resolve_exit_reference_price(
    ticker: str,
    *,
    explicit: float | None = None,
    fill_fallback: float,
) -> float:
    """Reference price for exit TCA: explicit, else live quote, else fill (0 bps)."""
    if explicit is not None and float(explicit) > 0:
        return float(explicit)
    try:
        from .market_data import fetch_quote

        q = fetch_quote(ticker)
        if q and q.get("price"):
            return float(q["price"])
    except Exception as e:
        logger.debug("[tca] exit reference quote failed for %s: %s", ticker, e)
    return float(fill_fallback)


def apply_tca_on_trade_close(trade) -> None:
    """Set ``tca_exit_slippage_bps`` when reference and exit fill exist."""
    ref = getattr(trade, "tca_reference_exit_price", None)
    fill = getattr(trade, "exit_price", None)
    if ref is None or fill is None:
        return
    bps = exit_slippage_bps(
        float(ref), float(fill), getattr(trade, "direction", None) or "long",
    )
    if bps is not None:
        trade.tca_exit_slippage_bps = bps


def tca_summary_by_ticker(
    db: Session,
    user_id: int | None,
    *,
    days: int = 90,
    limit: int = 50,
) -> dict[str, Any]:
    """Aggregate mean entry slippage (bps) and fill count per ticker.

    When *user_id* is None, returns empty aggregates (no cross-user query).
    """
    from ...models.trading import Trade

    if user_id is None:
        return {
            "ok": True,
            "window_days": days,
            "overall_fills": 0,
            "overall_avg_entry_slippage_bps": None,
            "by_ticker": [],
            "exit_overall_closes": 0,
            "exit_overall_avg_slippage_bps": None,
            "exit_by_ticker": [],
        }

    since = datetime.utcnow() - timedelta(days=max(1, int(days)))
    q = (
        db.query(
            Trade.ticker,
            sa_func.count(Trade.id),
            sa_func.avg(Trade.tca_entry_slippage_bps),
        )
        .filter(
            Trade.tca_entry_slippage_bps.isnot(None),
            Trade.filled_at.isnot(None),
            Trade.filled_at >= since,
            Trade.user_id == user_id,
        )
    )
    rows = (
        q.group_by(Trade.ticker)
        .order_by(sa_func.count(Trade.id).desc())
        .limit(limit)
        .all()
    )
    by_ticker = [
        {
            "ticker": r[0],
            "fills": int(r[1] or 0),
            "avg_entry_slippage_bps": round(float(r[2]), 2) if r[2] is not None else None,
        }
        for r in rows
    ]
    overall = (
        db.query(
            sa_func.count(Trade.id),
            sa_func.avg(Trade.tca_entry_slippage_bps),
        )
        .filter(
            Trade.tca_entry_slippage_bps.isnot(None),
            Trade.filled_at.isnot(None),
            Trade.filled_at >= since,
            Trade.user_id == user_id,
        )
    )
    oc, oavg = overall.first() or (0, None)

    qx = (
        db.query(
            Trade.ticker,
            sa_func.count(Trade.id),
            sa_func.avg(Trade.tca_exit_slippage_bps),
        )
        .filter(
            Trade.tca_exit_slippage_bps.isnot(None),
            Trade.status == "closed",
            Trade.exit_date.isnot(None),
            Trade.exit_date >= since,
            Trade.user_id == user_id,
        )
        .group_by(Trade.ticker)
        .order_by(sa_func.count(Trade.id).desc())
        .limit(limit)
        .all()
    )
    exit_by_ticker = [
        {
            "ticker": r[0],
            "closes": int(r[1] or 0),
            "avg_exit_slippage_bps": round(float(r[2]), 2) if r[2] is not None else None,
        }
        for r in qx
    ]
    ox = (
        db.query(
            sa_func.count(Trade.id),
            sa_func.avg(Trade.tca_exit_slippage_bps),
        )
        .filter(
            Trade.tca_exit_slippage_bps.isnot(None),
            Trade.status == "closed",
            Trade.exit_date.isnot(None),
            Trade.exit_date >= since,
            Trade.user_id == user_id,
        )
    )
    exc, exavg = ox.first() or (0, None)

    return {
        "ok": True,
        "window_days": days,
        "overall_fills": int(oc or 0),
        "overall_avg_entry_slippage_bps": round(float(oavg), 2) if oavg is not None else None,
        "by_ticker": by_ticker,
        "exit_overall_closes": int(exc or 0),
        "exit_overall_avg_slippage_bps": round(float(exavg), 2) if exavg is not None else None,
        "exit_by_ticker": exit_by_ticker,
    }
