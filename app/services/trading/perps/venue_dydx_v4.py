"""dYdX v4 perpetuals public-indexer adapter (US-accessible).

Endpoints (no auth needed, no geo block — decentralized perp DEX):

  GET https://indexer.dydx.trade/v4/perpetualMarkets
      Returns ``{markets: {<ticker>: {...}, ...}}`` with one entry per
      contract. Per-contract: oraclePrice, nextFundingRate,
      openInterest, defaultFundingRate1H, status, baseOpenInterest.

  GET https://indexer.dydx.trade/v4/historicalFunding/{ticker}?limit=N
      Returns ``{historicalFunding: [{ticker, rate, price,
      effectiveAt, effectiveAtHeight}, ...]}``. Rows are newest-first.

Key shape differences vs Hyperliquid:

  * dYdX uses 'BTC-USD' tickers (matches the spot-pair convention).
    Hyperliquid uses bare 'BTC'. perp_contracts rows for venue='dydx'
    use the -USD suffixed form.
  * dYdX perpetuals are oracle-priced — there's no separate mark vs
    index. We populate mark_price = index_price = oraclePrice so the
    perp_basis rows always show ~0 basis_bps. The funding rate is the
    interesting signal.
  * Funding cadence is hourly (same as Hyperliquid), so
    perp_contracts.funding_interval_hours=1.
  * Funding rate field is named 'rate' on the historical endpoint
    (not 'fundingRate' like Hyperliquid).

Network failures degrade gracefully (return empty / None).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_DYDX_INDEXER = "https://indexer.dydx.trade/v4"
_TIMEOUT_SEC = 10


def _normalize_market(ticker: str, m: dict) -> Optional[dict]:
    """Translate dYdX market entry into the Binance-shaped dict our
    ingestion module already understands.
    """
    try:
        oracle = float(m.get("oraclePrice") or 0)
        funding = float(m.get("nextFundingRate") or 0)
        oi = float(m.get("openInterest") or 0)
        vol_24h = float(m.get("volume24H") or 0)
        if oracle <= 0:
            return None
    except (TypeError, ValueError):
        return None
    if (m.get("status") or "").upper() != "ACTIVE":
        return None
    now_ms = int(time.time() * 1000)
    return {
        "symbol": ticker,
        "mark_price": oracle,
        "index_price": oracle,
        "estimated_settle_price": oracle,
        "last_funding_rate": funding,
        "open_interest": oi,
        "open_interest_usd": oi * oracle if oi and oracle else 0,
        "next_funding_time": None,
        "interest_rate": 0.0,
        "ts": now_ms,
        "day_notional_volume": vol_24h,
    }


def fetch_premium_index(symbol: Optional[str] = None) -> list[dict]:
    """All active perpetualMarkets, normalized to the Binance shape.

    ``symbol`` filters in-process; the bulk call returns every market
    in one request regardless.
    """
    try:
        r = requests.get(
            f"{_DYDX_INDEXER}/perpetualMarkets", timeout=_TIMEOUT_SEC,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.debug("[dydx_v4] perpetualMarkets fetch failed: %s", e)
        return []
    markets = (data or {}).get("markets") or {}
    out: list[dict] = []
    for tkr, m in markets.items():
        norm = _normalize_market(tkr, m)
        if norm is not None:
            out.append(norm)
    if symbol:
        s = symbol.upper()
        out = [r for r in out if (r.get("symbol") or "").upper() == s]
    return out


def fetch_open_interest(symbol: str) -> Optional[dict]:
    """Current OI. Derived from the same bulk perpetualMarkets call."""
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


def _iso_to_ms(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    try:
        # Strip 'Z' for fromisoformat compatibility on Py < 3.11
        clean = s.rstrip("Z")
        if "+" not in clean and clean.count(":") >= 2:
            clean = clean.split(".")[0]  # also drop subseconds; close enough
        dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def fetch_funding_history(symbol: str, limit: int = 100) -> list[dict]:
    """Last N hourly funding rates for a market. Returns rows in
    venue_binance shape: {symbol, funding_time (ms-epoch), funding_rate,
    mark_at_funding}.
    """
    sym = symbol.upper()
    try:
        r = requests.get(
            f"{_DYDX_INDEXER}/historicalFunding/{sym}",
            params={"limit": max(1, min(limit, 100))},
            timeout=_TIMEOUT_SEC,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.debug(
            "[dydx_v4] historicalFunding fetch failed for %s: %s", sym, e
        )
        return []
    rows = (data or {}).get("historicalFunding") or []
    out: list[dict] = []
    # dYdX returns newest-first; reverse so consumers see oldest-first
    # like the Binance / Hyperliquid adapters.
    for row in reversed(rows):
        ms = _iso_to_ms(row.get("effectiveAt"))
        if ms is None:
            continue
        try:
            rate = float(row.get("rate") or 0)
            price = float(row.get("price") or 0)
        except (TypeError, ValueError):
            continue
        out.append({
            "symbol": row.get("ticker"),
            "funding_time": ms,
            "funding_rate": rate,
            "mark_at_funding": price if price > 0 else None,
        })
    return out
