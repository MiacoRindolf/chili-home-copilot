"""Transaction cost analysis (TCA): reference price vs fill, slippage bps, aggregates."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from .execution_cost_builder import _usable_tca_bps

logger = logging.getLogger(__name__)


def _positive_finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out <= 0:
        return None
    return out


def entry_slippage_bps(
    reference_price: float,
    fill_price: float,
    direction: str = "long",
) -> float | None:
    """Slippage in basis points: positive = paid more (long) / worse fill."""
    ref = _positive_finite_float(reference_price)
    fil = _positive_finite_float(fill_price)
    if ref is None or fil is None:
        return None
    d = (direction or "long").strip().lower()
    if d == "short":
        return round((ref - fil) / ref * 10000.0, 2)
    return round((fil - ref) / ref * 10000.0, 2)


def apply_tca_on_trade_fill(trade, *, fill_price: Any | None = None) -> None:
    """Set entry slippage only from broker fill truth, never planned entry."""
    ref = _positive_finite_float(getattr(trade, "tca_reference_entry_price", None))
    fill = (
        _positive_finite_float(fill_price)
        if fill_price is not None
        else _positive_finite_float(getattr(trade, "avg_fill_price", None))
    )
    if ref is None or fill is None:
        return
    bps = entry_slippage_bps(ref, fill, getattr(trade, "direction", None) or "long")
    if bps is not None:
        trade.tca_entry_slippage_bps = bps


def exit_slippage_bps(
    reference_price: float,
    fill_price: float,
    direction: str = "long",
) -> float | None:
    """Exit slippage in bps. Long exit: positive = received less than ref (worse)."""
    ref = _positive_finite_float(reference_price)
    fil = _positive_finite_float(fill_price)
    if ref is None or fil is None:
        return None
    d = (direction or "long").strip().lower()
    if d == "short":
        # Cover short: paid more than ref -> worse
        return round((fil - ref) / ref * 10000.0, 2)
    return round((ref - fil) / ref * 10000.0, 2)


def resolve_arrival_price(
    ticker: str,
    *,
    signal_price: float | None = None,
) -> dict[str, Any]:
    """Resolve a consistent arrival price for IS decomposition.

    Priority: live mid-quote > signal_price > last close.
    Returns dict with price, source, and bid/ask when available.
    """
    result: dict[str, Any] = {"ticker": ticker, "source": "unknown"}

    try:
        from .market_data import fetch_quote
        q = fetch_quote(ticker)
        if q:
            price = _positive_finite_float(q.get("price"))
            bid = _positive_finite_float(q.get("bid"))
            ask = _positive_finite_float(q.get("ask"))
            if bid is not None and ask is not None and ask >= bid:
                mid = (bid + ask) / 2
                result["arrival_price"] = mid
                result["source"] = "mid_quote"
                result["bid"] = bid
                result["ask"] = ask
                result["quoted_spread_bps"] = round(
                    (ask - bid) / mid * 10000, 2,
                )
                return result
            elif price is not None:
                result["arrival_price"] = price
                result["source"] = "last_trade"
                return result
    except Exception:
        pass

    signal = _positive_finite_float(signal_price)
    if signal is not None:
        result["arrival_price"] = signal
        result["source"] = "signal_price"
        return result

    result["arrival_price"] = None
    result["source"] = "unavailable"
    return result


def resolve_exit_reference_price(
    ticker: str,
    *,
    explicit: float | None = None,
    fill_fallback: float,
) -> float:
    """Reference price for exit TCA: explicit, else live quote, else fill (0 bps)."""
    explicit_price = _positive_finite_float(explicit)
    if explicit_price is not None:
        return explicit_price
    try:
        from .market_data import fetch_quote

        q = fetch_quote(ticker)
        quote_price = _positive_finite_float(q.get("price")) if q else None
        if quote_price is not None:
            return quote_price
    except Exception as e:
        logger.debug("[tca] exit reference quote failed for %s: %s", ticker, e)
    return _positive_finite_float(fill_fallback) or 0.0


def apply_tca_on_trade_close(trade) -> None:
    """Set ``tca_exit_slippage_bps`` when reference and exit fill exist."""
    ref = getattr(trade, "tca_reference_exit_price", None)
    fill = getattr(trade, "exit_price", None)
    if ref is None or fill is None:
        return
    bps = exit_slippage_bps(
        ref, fill, getattr(trade, "direction", None) or "long",
    )
    if bps is not None:
        trade.tca_exit_slippage_bps = bps


def _avg_bps(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _group_usable_tca(
    rows: list[Any],
    *,
    attr: str,
    count_key: str,
    raw_count_key: str,
    avg_key: str,
) -> tuple[list[dict[str, Any]], list[float], int]:
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"values": [], raw_count_key: 0, "excluded_tca_samples": 0}
    )
    overall_values: list[float] = []
    excluded = 0
    for trade in rows:
        ticker = str(getattr(trade, "ticker", "") or "").strip().upper()
        bucket = grouped[ticker]
        bucket["ticker"] = ticker
        bucket[raw_count_key] += 1
        usable = _usable_tca_bps(trade, attr)
        if usable is None:
            bucket["excluded_tca_samples"] += 1
            excluded += 1
            continue
        bucket["values"].append(usable)
        overall_values.append(usable)

    out: list[dict[str, Any]] = []
    for bucket in grouped.values():
        values = list(bucket.pop("values"))
        bucket[count_key] = len(values)
        bucket[avg_key] = _avg_bps(values)
        out.append(bucket)
    out.sort(
        key=lambda row: (
            int(row.get(count_key) or 0),
            int(row.get(raw_count_key) or 0),
            str(row.get("ticker") or ""),
        ),
        reverse=True,
    )
    return out, overall_values, excluded


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
            "raw_overall_fills": 0,
            "entry_excluded_tca_samples": 0,
            "overall_avg_entry_slippage_bps": None,
            "by_ticker": [],
            "exit_overall_closes": 0,
            "raw_exit_overall_closes": 0,
            "exit_excluded_tca_samples": 0,
            "exit_overall_avg_slippage_bps": None,
            "exit_by_ticker": [],
        }

    since = datetime.utcnow() - timedelta(days=max(1, int(days)))
    entry_rows = (
        db.query(Trade)
        .filter(
            Trade.tca_entry_slippage_bps.isnot(None),
            Trade.filled_at.isnot(None),
            Trade.filled_at >= since,
            Trade.user_id == user_id,
        )
        .all()
    )
    by_ticker, entry_values, entry_excluded = _group_usable_tca(
        entry_rows,
        attr="tca_entry_slippage_bps",
        count_key="fills",
        raw_count_key="raw_fills",
        avg_key="avg_entry_slippage_bps",
    )
    by_ticker = by_ticker[: max(1, min(int(limit or 50), 200))]

    exit_rows = (
        db.query(Trade)
        .filter(
            Trade.tca_exit_slippage_bps.isnot(None),
            Trade.status == "closed",
            Trade.exit_date.isnot(None),
            Trade.exit_date >= since,
            Trade.user_id == user_id,
        )
        .all()
    )
    exit_by_ticker, exit_values, exit_excluded = _group_usable_tca(
        exit_rows,
        attr="tca_exit_slippage_bps",
        count_key="closes",
        raw_count_key="raw_closes",
        avg_key="avg_exit_slippage_bps",
    )
    exit_by_ticker = exit_by_ticker[: max(1, min(int(limit or 50), 200))]

    return {
        "ok": True,
        "window_days": days,
        "overall_fills": len(entry_values),
        "raw_overall_fills": len(entry_rows),
        "entry_excluded_tca_samples": entry_excluded,
        "overall_avg_entry_slippage_bps": _avg_bps(entry_values),
        "by_ticker": by_ticker,
        "exit_overall_closes": len(exit_values),
        "raw_exit_overall_closes": len(exit_rows),
        "exit_excluded_tca_samples": exit_excluded,
        "exit_overall_avg_slippage_bps": _avg_bps(exit_values),
        "exit_by_ticker": exit_by_ticker,
    }
