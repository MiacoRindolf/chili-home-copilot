"""Kraken Futures perpetuals adapter (US-regulated, geo-accessible).

The first centralized US-regulated venue in the perps lane (Hyperliquid
and dYdX v4 are both DEXs). Useful for cross-venue funding-rate
divergence signals.

Endpoints (no auth needed for public market data):

  GET https://futures.kraken.com/derivatives/api/v3/tickers
      Returns ``{tickers: [{symbol, markPrice, indexPrice, fundingRate,
      openInterest, ...}, ...]}`` for ALL contracts. Perpetuals are
      identified by symbol prefix ``PF_*``; dated contracts use ``FI_*``
      (we filter to PF_ only).

  GET https://futures.kraken.com/derivatives/api/v3/historical-funding-rates
      ?symbol=PF_XBTUSD
      Returns ``{rates: [{timestamp, fundingRate, relativeFundingRate},
      ...]}``. ``relativeFundingRate`` is the per-period rate matching
      what other venues call "funding rate" — that's the field we map.
      ``fundingRate`` on Kraken is dollars-per-contract; ignore.

Symbol quirks vs other venues:

  * Kraken uses 'PF_XBTUSD' format (PF_ prefix + XBT not BTC).
    perp_contracts rows for venue='kraken_futures' use the literal
    Kraken symbol; the adapter does NOT renormalize to 'BTC'.
  * Funding cadence is hourly (matches Hyperliquid + dYdX), so
    funding_interval_hours=1.
  * markPrice and indexPrice are both populated; basis is real.

Network failures degrade gracefully (return empty / None).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_KRAKEN_API = "https://futures.kraken.com/derivatives/api/v3"
_TIMEOUT_SEC = 10


def _normalize_ticker(t: dict) -> Optional[dict]:
    """Translate Kraken ticker dict into the Binance-shaped record our
    ingestion module already understands.

    Returns None for non-perpetual rows (caller filters those out
    anyway via the PF_ prefix check), suspended contracts, or rows with
    unparseable prices.
    """
    sym = t.get("symbol") or ""
    if not sym.startswith("PF_"):
        return None
    if t.get("suspended"):
        return None
    try:
        mark = float(t.get("markPrice") or 0)
        index = float(t.get("indexPrice") or 0)
        funding = float(t.get("fundingRate") or 0)  # kraken's spot funding
        oi = float(t.get("openInterest") or 0)
        vol = float(t.get("volumeQuote") or 0)
    except (TypeError, ValueError):
        return None
    if mark <= 0 or index <= 0:
        return None
    now_ms = int(time.time() * 1000)
    return {
        "symbol": sym,
        "mark_price": mark,
        "index_price": index,
        "estimated_settle_price": mark,
        # Kraken's "fundingRate" on the ticker is the current rate they
        # would charge at the next funding event. Map it directly so
        # downstream code that asks for "current funding" sees a live
        # number per pass.
        "last_funding_rate": funding,
        "open_interest": oi,
        "open_interest_usd": oi * mark if oi and mark else 0,
        "next_funding_time": None,
        "interest_rate": 0.0,
        "ts": now_ms,
        "day_notional_volume": vol,
    }


def fetch_premium_index(symbol: Optional[str] = None) -> list[dict]:
    """All active Kraken Futures perpetuals, normalized to Binance shape.

    ``symbol`` filters in-process; the bulk call returns every ticker
    in one request regardless.
    """
    try:
        r = requests.get(f"{_KRAKEN_API}/tickers", timeout=_TIMEOUT_SEC)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.debug("[kraken_futures] tickers fetch failed: %s", e)
        return []
    rows = (data or {}).get("tickers") or []
    out: list[dict] = []
    for t in rows:
        norm = _normalize_ticker(t)
        if norm is not None:
            out.append(norm)
    if symbol:
        s = symbol.upper()
        out = [r for r in out if (r.get("symbol") or "").upper() == s]
    return out


def fetch_open_interest(symbol: str) -> Optional[dict]:
    """Current OI. Derived from the same bulk tickers call."""
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
        clean = s.rstrip("Z")
        if "." in clean:
            clean = clean.split(".")[0]
        dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def fetch_funding_history(symbol: str, limit: int = 100) -> list[dict]:
    """Last N hourly funding rates. Kraken returns the ENTIRE history
    in chronological order (oldest-first) and doesn't support a limit
    parameter on this endpoint, so we slice in-process to honor
    ``limit``.

    Returned rows are in venue_binance shape:
    {symbol, funding_time (ms-epoch), funding_rate, mark_at_funding}.
    funding_rate maps from Kraken's ``relativeFundingRate`` (per-period
    rate); the adapter ignores ``fundingRate`` (dollars-per-contract).
    """
    sym = symbol.upper()
    try:
        r = requests.get(
            f"{_KRAKEN_API}/historical-funding-rates",
            params={"symbol": sym},
            timeout=_TIMEOUT_SEC,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.debug(
            "[kraken_futures] historical funding fetch failed for %s: %s",
            sym, e,
        )
        return []
    rates = (data or {}).get("rates") or []
    if not rates:
        return []
    # Slice to last `limit` (Kraken returns oldest-first; tail is most recent)
    sliced = rates[-max(1, min(limit, len(rates))):]
    out: list[dict] = []
    for row in sliced:
        ms = _iso_to_ms(row.get("timestamp"))
        if ms is None:
            continue
        try:
            rate = float(row.get("relativeFundingRate") or 0)
        except (TypeError, ValueError):
            continue
        out.append({
            "symbol": sym,
            "funding_time": ms,
            "funding_rate": rate,
            "mark_at_funding": None,  # not provided by this endpoint
        })
    return out
