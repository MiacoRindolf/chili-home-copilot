"""Hyperliquid perpetuals REST adapter (geo-unrestricted).

Single public endpoint: ``POST https://api.hyperliquid.xyz/info`` with a
JSON body that selects the request type. Used here for three calls:

  - ``{"type": "metaAndAssetCtxs"}``
        Returns ``[meta, ctxs]`` where ``meta.universe`` is a list of
        coin metadata and ``ctxs`` is the parallel list of per-coin
        market state (mark price, oracle/index price, premium, current
        funding, open interest, daily volume). One call gets everything.

  - ``{"type": "fundingHistory", "coin": "BTC", "startTime": ms, "endTime": ms}``
        Hourly funding rate history.

  - ``{"type": "openInterest"}``  — derivable from metaAndAssetCtxs;
        kept implicit through that call.

Key differences from venue_binance:
  * Symbols are bare ('BTC', 'ETH', 'SOL') — no USDT suffix. The
    ``perp_contracts`` rows for hyperliquid use the bare symbol.
  * Funding cadence is 1 hour, not 8. ``perp_contracts.funding_interval_hours``
    is set to 1 for these rows so the annualizer in features.py uses
    the correct multiplier.
  * Mark/oracle prices come pre-computed; ``premium`` is already
    (markPx - oraclePx) / oraclePx from the venue. Spread vs spot can
    be approximated by ``(markPx - oraclePx)`` when no separate spot
    feed is wired (Hyperliquid's oracle is Pyth, which IS a spot
    aggregator — close enough for basis).

Network failures degrade gracefully (return empty / None).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_HL_API = "https://api.hyperliquid.xyz/info"
_TIMEOUT_SEC = 10


def _normalize_to_binance_shape(meta: dict, ctxs: list[dict]) -> list[dict]:
    """Translate Hyperliquid ctxs into the Binance-shaped dicts our
    ingestion module already understands.

    Binance fields used downstream: symbol, mark_price, index_price,
    last_funding_rate, ts.
    """
    universe = meta.get("universe") or []
    out: list[dict] = []
    now_ms = int(time.time() * 1000)
    for i, u in enumerate(universe):
        if i >= len(ctxs):
            break
        c = ctxs[i] or {}
        symbol = u.get("name")
        if not symbol:
            continue
        try:
            mark = float(c.get("markPx") or 0)
            oracle = float(c.get("oraclePx") or 0)
            funding = float(c.get("funding") or 0)
            oi = float(c.get("openInterest") or 0)
            day_ntl_vlm = float(c.get("dayNtlVlm") or 0)
        except (TypeError, ValueError):
            continue
        out.append({
            "symbol": symbol,
            "mark_price": mark,
            "index_price": oracle,
            "estimated_settle_price": mark,
            "last_funding_rate": funding,
            "open_interest": oi,
            "open_interest_usd": oi * mark if oi and mark else 0,
            "next_funding_time": None,
            "interest_rate": 0.0,
            "ts": now_ms,
            "day_notional_volume": day_ntl_vlm,
        })
    return out


def _post_info(payload: dict) -> Optional[dict | list]:
    """Single POST /info wrapper with timeout + error logging."""
    try:
        r = requests.post(_HL_API, json=payload, timeout=_TIMEOUT_SEC)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug("[hyperliquid] %s POST failed: %s",
                     payload.get("type"), e)
        return None


def fetch_premium_index(symbol: Optional[str] = None) -> list[dict]:
    """Mark vs oracle price + current funding rate for all contracts.

    Mirrors venue_binance.fetch_premium_index. ``symbol`` filters the
    output but the network call is the same (Hyperliquid bulk-returns
    every coin in one POST).
    """
    data = _post_info({"type": "metaAndAssetCtxs"})
    if not data or not isinstance(data, list) or len(data) < 2:
        return []
    meta, ctxs = data[0], data[1]
    if not isinstance(meta, dict) or not isinstance(ctxs, list):
        return []
    rows = _normalize_to_binance_shape(meta, ctxs)
    if symbol:
        s = symbol.upper()
        rows = [r for r in rows if (r.get("symbol") or "").upper() == s]
    return rows


def fetch_open_interest(symbol: str) -> Optional[dict]:
    """Current OI for a symbol. Derived from the same bulk endpoint."""
    rows = fetch_premium_index(symbol=symbol)
    if not rows:
        return None
    r = rows[0]
    return {
        "symbol": r["symbol"],
        "open_interest": r.get("open_interest", 0),
        "open_interest_usd": r.get("open_interest_usd", 0),
        "ts": r.get("ts"),
    }


def fetch_funding_history(symbol: str, limit: int = 100) -> list[dict]:
    """Last N hourly funding rates. Hyperliquid funds every hour, so
    ``limit`` rows ≈ ``limit`` hours back.

    Returns rows shaped like venue_binance: {symbol, funding_time
    (ms-epoch), funding_rate, mark_at_funding (None — Hyperliquid
    doesn't return the mark at the funding instant)}.
    """
    now_ms = int(time.time() * 1000)
    # Pull a bit more than `limit` hours and trim — funding occurs every
    # hour but we want to be safe vs missed periods.
    start_ms = now_ms - (max(limit, 1) * 3600 * 1000)
    data = _post_info({
        "type": "fundingHistory",
        "coin": symbol.upper(),
        "startTime": start_ms,
        "endTime": now_ms,
    })
    if not data or not isinstance(data, list):
        return []
    out: list[dict] = []
    for row in data[-limit:]:
        try:
            out.append({
                "symbol": row.get("coin"),
                "funding_time": row.get("time"),
                "funding_rate": float(row.get("fundingRate") or 0),
                "mark_at_funding": None,
            })
        except (TypeError, ValueError):
            continue
    return out
