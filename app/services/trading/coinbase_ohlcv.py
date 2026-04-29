"""Coinbase public-API OHLCV fetcher (FIX 42, 2026-04-29).

Provides historical candles for crypto tickers when the primary provider
chain (Massive → Polygon → yfinance) fails — e.g., during the recurring
Massive.com TCP-refused outages. Coinbase's public ``/products/{pid}/candles``
endpoint is unauthenticated, geo-unblocked from US, and serves the same
product IDs the live-trading path already uses (BTC-USD, ETH-USD, etc.).

Granularity mapping (Coinbase only accepts these specific values, in seconds):
  1m   → 60
  5m   → 300
  15m  → 900
  1h   → 3600
  6h   → 21600
  1d   → 86400

Wired into ``market_data.fetch_ohlcv`` and ``fetch_ohlcv_df`` as the final
fallback for ``-USD`` tickers. Off by default; flip
``brain_market_data_coinbase_fallback`` to enable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)


# Coinbase Exchange API granularities (seconds). Anything else returns 400.
_GRANULARITY_MAP = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "6h": 21600,
    "1d": 86400,
}

# Period → lookback days. Loose mapping consistent with the rest of market_data.
_PERIOD_DAYS = {
    "1d": 1,
    "5d": 5,
    "1mo": 30,
    "3mo": 90,
    "6mo": 180,
    "1y": 365,
    "2y": 730,
    "max": 730,
}

# Coinbase returns at most 300 candles per request. We chunk if the requested
# range exceeds that.
_COINBASE_MAX_CANDLES = 300

# Map a -USD ticker to a Coinbase product ID. For most cases it's identity:
# "BTC-USD" → "BTC-USD". For aliases we may add overrides here.
_PRODUCT_ALIASES: dict[str, str] = {
    # e.g. "MATIC-USD" → "MATIC-USD" (already correct); keep dict for future
    # remappings if Coinbase deprecates a symbol but our seeds still use the
    # legacy name.
}


def is_crypto_usd(ticker: str) -> bool:
    """True iff the ticker matches the ``BASE-USD`` shape Coinbase accepts."""
    if not ticker or "-" not in ticker:
        return False
    base, quote = ticker.upper().split("-", 1)
    return bool(base) and quote == "USD"


def _to_product_id(ticker: str) -> str:
    """Normalize ticker to Coinbase product ID."""
    t = ticker.upper()
    return _PRODUCT_ALIASES.get(t, t)


def _resolve_window(
    *, start: str | None, end: str | None, period: str | None
) -> tuple[datetime, datetime]:
    """Return (start_dt, end_dt) UTC for the requested window."""
    if start:
        start_dt = datetime.fromisoformat(str(start)[:10]).replace(tzinfo=timezone.utc)
    else:
        days = _PERIOD_DAYS.get((period or "6mo"), 180)
        start_dt = datetime.now(tz=timezone.utc) - timedelta(days=days)
    if end:
        end_dt = datetime.fromisoformat(str(end)[:10]).replace(tzinfo=timezone.utc)
    else:
        end_dt = datetime.now(tz=timezone.utc)
    return start_dt, end_dt


def _request_chunk(
    product_id: str,
    granularity_s: int,
    start_dt: datetime,
    end_dt: datetime,
    *,
    timeout_s: float = 8.0,
) -> list[list[float]]:
    """Fetch one ≤300-candle chunk. Returns Coinbase's raw list-of-lists."""
    url = f"https://api.exchange.coinbase.com/products/{product_id}/candles"
    params = {
        "granularity": granularity_s,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
    }
    resp = requests.get(url, params=params, timeout=timeout_s)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []
    # Coinbase rows: [timestamp, low, high, open, close, volume].
    return [r for r in data if isinstance(r, list) and len(r) >= 6]


def get_ohlcv(
    ticker: str,
    interval: str = "1d",
    period: str | None = "6mo",
    *,
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    """Public entry point. Returns rows in the same shape as ``market_data``.

    Each row::

        {"time": <epoch_seconds>, "open": float, "high": float,
         "low": float, "close": float, "volume": int}

    Empty list on any failure — caller decides whether to log/fallback.
    """
    if not is_crypto_usd(ticker):
        return []
    granularity_s = _GRANULARITY_MAP.get(interval)
    if granularity_s is None:
        # Coinbase doesn't support arbitrary intervals; fail soft.
        logger.debug(
            "[coinbase_ohlcv] interval=%r not supported by Coinbase API for %s",
            interval, ticker,
        )
        return []

    product_id = _to_product_id(ticker)
    start_dt, end_dt = _resolve_window(start=start, end=end, period=period)

    # Chunk: each request can cover at most _COINBASE_MAX_CANDLES * granularity seconds.
    chunk_seconds = _COINBASE_MAX_CANDLES * granularity_s
    chunks: list[list[list[float]]] = []
    cursor = start_dt
    while cursor < end_dt:
        chunk_end = min(cursor + timedelta(seconds=chunk_seconds), end_dt)
        try:
            rows = _request_chunk(product_id, granularity_s, cursor, chunk_end)
        except requests.RequestException as e:
            logger.warning(
                "[coinbase_ohlcv] %s %s [%s..%s] request failed: %s",
                product_id, interval, cursor.date(), chunk_end.date(), e,
            )
            return []
        except Exception as e:
            logger.warning(
                "[coinbase_ohlcv] %s %s parse failed: %s", product_id, interval, e,
            )
            return []
        if rows:
            chunks.append(rows)
        cursor = chunk_end

    # Flatten + dedupe by timestamp; sort ascending.
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for chunk in chunks:
        for row in chunk:
            try:
                ts = int(row[0])
                if ts in seen:
                    continue
                seen.add(ts)
                out.append({
                    "time": ts,
                    "low": round(float(row[1]), 6),
                    "high": round(float(row[2]), 6),
                    "open": round(float(row[3]), 6),
                    "close": round(float(row[4]), 6),
                    "volume": float(row[5]),
                })
            except (ValueError, TypeError, IndexError):
                continue
    out.sort(key=lambda r: r["time"])
    return out
