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
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from ..socket_budget import (
    DEFAULT_HTTP_POOL_CONNECTIONS,
    DEFAULT_HTTP_POOL_MAXSIZE,
    is_socket_exhaustion_error,
    mount_bounded_http_adapters,
)

logger = logging.getLogger(__name__)


# Coinbase Exchange API endpoints and request defaults.
_COINBASE_EXCHANGE_API_BASE_URL = "https://api.exchange.coinbase.com"
_COINBASE_CANDLES_PATH_TEMPLATE = "/products/{product_id}/candles"
_COINBASE_TICKER_PATH_TEMPLATE = "/products/{product_id}/ticker"
_COINBASE_TIMEOUT_ENV = "CHILI_COINBASE_MARKET_DATA_TIMEOUT_SECONDS"
_COINBASE_DEFAULT_TIMEOUT_S = 8.0
_COINBASE_MIN_TIMEOUT_S = 0.1
_COINBASE_PUBLIC_PROVIDER = "coinbase_public"
_MIN_VALID_QUOTE_PRICE = 0.0
_COINBASE_HTTP_POOL_CONNECTIONS = DEFAULT_HTTP_POOL_CONNECTIONS
_COINBASE_HTTP_POOL_MAXSIZE = DEFAULT_HTTP_POOL_MAXSIZE


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

_CIRCUIT_LOCK = threading.Lock()
_CIRCUIT_FAILS = 0
_CIRCUIT_OPEN_UNTIL = 0.0
_CIRCUIT_LAST_LOG = 0.0
_SESSION = requests.Session()
_SESSION.headers.update({"Accept": "application/json"})
mount_bounded_http_adapters(
    _SESSION,
    pool_connections=_COINBASE_HTTP_POOL_CONNECTIONS,
    pool_maxsize=_COINBASE_HTTP_POOL_MAXSIZE,
    pool_block=True,
)

# Map a -USD ticker to a Coinbase product ID. For most cases it's identity:
# "BTC-USD" → "BTC-USD". For aliases we may add overrides here.
_PRODUCT_ALIASES: dict[str, str] = {
    # e.g. "MATIC-USD" → "MATIC-USD" (already correct); keep dict for future
    # remappings if Coinbase deprecates a symbol but our seeds still use the
    # legacy name.
}


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(_COINBASE_MIN_TIMEOUT_S, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _request_timeout_s() -> float:
    return _env_float(_COINBASE_TIMEOUT_ENV, _COINBASE_DEFAULT_TIMEOUT_S)


def _circuit_is_open(product_id: str, interval: str) -> bool:
    """Return True while provider/network failures are in backoff."""
    global _CIRCUIT_LAST_LOG
    now = time.time()
    with _CIRCUIT_LOCK:
        remaining = _CIRCUIT_OPEN_UNTIL - now
        if remaining <= 0:
            return False
        if now - _CIRCUIT_LAST_LOG >= 60:
            _CIRCUIT_LAST_LOG = now
            logger.warning(
                "[coinbase_ohlcv] circuit breaker OPEN - skipping %s %s (%ss remaining)",
                product_id,
                interval,
                int(remaining),
            )
        return True


def _request_failure_counts(exc: requests.RequestException) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if is_socket_exhaustion_error(exc):
        return True
    if isinstance(exc, requests.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return status == 429 or (status is not None and int(status) >= 500)
    return False


def _record_request_success() -> None:
    global _CIRCUIT_FAILS
    with _CIRCUIT_LOCK:
        _CIRCUIT_FAILS = 0


def _record_request_failure(exc: requests.RequestException) -> None:
    global _CIRCUIT_FAILS, _CIRCUIT_OPEN_UNTIL, _CIRCUIT_LAST_LOG
    if not _request_failure_counts(exc):
        return
    trip = _env_int("CHILI_COINBASE_OHLCV_CIRCUIT_TRIP", 5)
    open_s = _env_int("CHILI_COINBASE_OHLCV_CIRCUIT_OPEN_SECONDS", 900)
    with _CIRCUIT_LOCK:
        _CIRCUIT_FAILS += 1
        if _CIRCUIT_FAILS < trip:
            return
        _CIRCUIT_FAILS = 0
        _CIRCUIT_OPEN_UNTIL = time.time() + open_s
        _CIRCUIT_LAST_LOG = time.time()
    logger.error(
        "[coinbase_ohlcv] circuit breaker OPEN - %s consecutive provider/network failures, "
        "skipping calls for %ss",
        trip,
        open_s,
    )


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
    timeout_s: float = _COINBASE_DEFAULT_TIMEOUT_S,
) -> list[list[float]]:
    """Fetch one ≤300-candle chunk. Returns Coinbase's raw list-of-lists."""
    path = _COINBASE_CANDLES_PATH_TEMPLATE.format(product_id=product_id)
    url = f"{_COINBASE_EXCHANGE_API_BASE_URL}{path}"
    params = {
        "granularity": granularity_s,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
    }
    resp = _SESSION.get(url, params=params, timeout=timeout_s)
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
    if _circuit_is_open(product_id, interval):
        return []
    start_dt, end_dt = _resolve_window(start=start, end=end, period=period)

    # Chunk: each request can cover at most _COINBASE_MAX_CANDLES * granularity seconds.
    chunk_seconds = _COINBASE_MAX_CANDLES * granularity_s
    chunks: list[list[list[float]]] = []
    cursor = start_dt
    while cursor < end_dt:
        chunk_end = min(cursor + timedelta(seconds=chunk_seconds), end_dt)
        try:
            rows = _request_chunk(
                product_id,
                granularity_s,
                cursor,
                chunk_end,
                timeout_s=_request_timeout_s(),
            )
            _record_request_success()
        except requests.RequestException as e:
            _record_request_failure(e)
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


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_quote(ticker: str) -> dict[str, Any] | None:
    """Fetch a public Coinbase ticker snapshot for ``BASE-USD`` products.

    The return shape mirrors the raw provider payload consumed by
    ``market_data._build_quote_result``. Empty or invalid responses fail closed
    with ``None`` so the caller can abstain from trading.
    """
    if not is_crypto_usd(ticker):
        return None
    product_id = _to_product_id(ticker)
    if _circuit_is_open(product_id, "quote"):
        return None
    path = _COINBASE_TICKER_PATH_TEMPLATE.format(product_id=product_id)
    url = f"{_COINBASE_EXCHANGE_API_BASE_URL}{path}"
    try:
        resp = _SESSION.get(url, timeout=_request_timeout_s())
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            return None
        price = _as_float(data.get("price"))
        if price is None or price <= _MIN_VALID_QUOTE_PRICE:
            return None
        _record_request_success()
    except requests.RequestException as e:
        _record_request_failure(e)
        logger.warning("[coinbase_ohlcv] %s quote request failed: %s", product_id, e)
        return None
    except Exception as e:
        logger.warning("[coinbase_ohlcv] %s quote parse failed: %s", product_id, e)
        return None

    out: dict[str, Any] = {
        "last_price": price,
        "provider": _COINBASE_PUBLIC_PROVIDER,
    }
    bid = _as_float(data.get("bid"))
    ask = _as_float(data.get("ask"))
    volume = _as_float(data.get("volume"))
    if bid is not None and bid > _MIN_VALID_QUOTE_PRICE:
        out["bid"] = bid
    if ask is not None and ask > _MIN_VALID_QUOTE_PRICE:
        out["ask"] = ask
    if volume is not None:
        out["volume"] = volume
    if data.get("time"):
        out["quote_ts"] = data.get("time")
    return out
