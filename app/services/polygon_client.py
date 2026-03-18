"""Polygon.io REST client for stocks and crypto market data.

Provides cached, rate-limit-aware wrappers around the Polygon v2/v3 APIs.
All Polygon-specific logic lives here so the rest of the app only talks to
the ``market_data`` abstraction layer.

Symbol conventions:
  - US stocks:  plain ticker like ``AAPL``, ``NVDA``, ``SPY``
  - Crypto:     ``X:BTCUSD``, ``X:ETHUSD`` (Polygon crypto prefix, no hyphen)
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from typing import Any

import requests

from ..config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory TTL cache (same pattern as yf_session)
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()

_TTL_BARS = 3600       # 1 hour for OHLCV bars (64 GB RAM — keep longer)
_TTL_QUOTE = 30        # 30 sec for live quotes
_TTL_SNAPSHOT = 60     # 1 min for snapshots
_MAX_CACHE = 15_000    # 64 GB RAM — generous cache

# Metrics counters for diagnostics
_metrics_lock = threading.Lock()
_metrics: dict[str, int] = {
    "requests": 0,
    "cache_hits": 0,
    "cache_misses": 0,
    "errors": 0,
    "rate_limits": 0,
    "fallbacks": 0,
}


def get_metrics() -> dict[str, int]:
    """Return a snapshot of Polygon usage metrics."""
    with _metrics_lock:
        return dict(_metrics)


def get_request_count() -> int:
    with _metrics_lock:
        return _metrics["requests"]


def _bump(key: str = "requests"):
    with _metrics_lock:
        _metrics[key] = _metrics.get(key, 0) + 1


# ---------------------------------------------------------------------------
# Simple global rate governor (per-second window)
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()
_request_times: deque[float] = deque()


def _rate_limit_wait() -> None:
    """Soft-governor to smooth bursts before hitting Polygon's hard 429 limits.

    Uses a 1-second sliding window with a configurable max RPS.
    """
    max_rps = max(1, settings.polygon_max_rps or 5)
    if max_rps <= 0:
        return

    import time as _t

    while True:
        with _rate_lock:
            now = _t.time()
            # Drop timestamps older than 1 second
            while _request_times and now - _request_times[0] > 1.0:
                _request_times.popleft()
            if len(_request_times) < max_rps:
                _request_times.append(now)
                return
            # Need to wait until the oldest entry falls out of the window
            oldest = _request_times[0]
            wait = max(0.0, 1.0 - (now - oldest))
        if wait > 0:
            _t.sleep(wait)
        else:
            # Loop will clean up and re-check
            continue


def _cache_get(key: str) -> Any | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            _bump("cache_misses")
            return None
        ts, val = entry
        ttl = _TTL_QUOTE if ":quote:" in key else (_TTL_SNAPSHOT if ":snap:" in key else _TTL_BARS)
        if time.time() - ts > ttl:
            del _cache[key]
            _bump("cache_misses")
            return None
        _bump("cache_hits")
        return val


def _cache_set(key: str, val: Any) -> None:
    with _cache_lock:
        if len(_cache) > _MAX_CACHE:
            cutoff = time.time() - 60
            expired = [k for k, (t, _) in _cache.items() if t < cutoff]
            for k in expired:
                del _cache[k]
        _cache[key] = (time.time(), val)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
_session = requests.Session()
_session.headers.update({"Accept": "application/json"})

_MAX_RETRIES = 2
_BACKOFF_BASE = 1.0


def _api_key() -> str:
    return settings.polygon_api_key


def _base() -> str:
    return settings.polygon_base_url.rstrip("/")


def _get(url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """GET with retries, backoff, and rate-limit awareness."""
    if not _api_key():
        return None
    if params is None:
        params = {}
    params["apiKey"] = _api_key()

    for attempt in range(_MAX_RETRIES + 1):
        try:
            _rate_limit_wait()
            _bump("requests")
            resp = _session.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                _bump("rate_limits")
                wait = _BACKOFF_BASE * (2 ** attempt)
                logger.warning(f"[polygon] 429 rate-limited, backing off {wait:.1f}s")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                _bump("errors")
                wait = _BACKOFF_BASE * (2 ** attempt)
                logger.warning(f"[polygon] {resp.status_code} server error, retry in {wait:.1f}s")
                time.sleep(wait)
                continue
            _bump("errors")
            logger.warning(f"[polygon] {resp.status_code} for {url}: {resp.text[:200]}")
            return None
        except requests.RequestException as e:
            _bump("errors")
            if attempt < _MAX_RETRIES:
                time.sleep(_BACKOFF_BASE)
                continue
            logger.warning(f"[polygon] request failed after {_MAX_RETRIES + 1} attempts: {e}")
            return None
    return None


# ---------------------------------------------------------------------------
# Symbol helpers
# ---------------------------------------------------------------------------

def is_crypto(ticker: str) -> bool:
    """Detect crypto tickers by convention (``BTC-USD``, ``ETH-USD``, etc.)."""
    t = ticker.upper()
    return t.endswith("-USD") and not t.startswith("X:")


def to_polygon_ticker(ticker: str) -> str:
    """Convert app-internal ticker to Polygon symbol format.

    Polygon crypto format uses ``X:BTCUSD`` (no hyphen), while the app uses
    ``BTC-USD`` (yfinance style).
    """
    t = ticker.upper()
    if is_crypto(t):
        return f"X:{t.replace('-', '')}"
    return t


# ---------------------------------------------------------------------------
# Aggregates (OHLCV bars)
# ---------------------------------------------------------------------------

_TIMESPAN_MAP = {
    "1m": ("minute", 1),
    "2m": ("minute", 2),
    "5m": ("minute", 5),
    "15m": ("minute", 15),
    "30m": ("minute", 30),
    "1h": ("hour", 1),
    "60m": ("hour", 1),
    "90m": ("minute", 90),
    "1d": ("day", 1),
    "5d": ("day", 5),
    "1wk": ("week", 1),
    "1mo": ("month", 1),
    "3mo": ("month", 3),
}

_PERIOD_DAYS = {
    "1d": 1, "5d": 5, "1mo": 30, "3mo": 90, "6mo": 180,
    "1y": 365, "2y": 730, "5y": 1825, "ytd": None, "max": 7300,
}


def _period_to_dates(period: str) -> tuple[str, str]:
    """Convert a yfinance-style period string to (from_date, to_date) YYYY-MM-DD strings."""
    today = date.today()
    to_str = today.strftime("%Y-%m-%d")

    if period == "ytd":
        from_d = date(today.year, 1, 1)
    else:
        days = _PERIOD_DAYS.get(period, 180)
        from_d = today - timedelta(days=days)

    return from_d.strftime("%Y-%m-%d"), to_str


def get_aggregates(
    ticker: str,
    interval: str = "1d",
    period: str = "6mo",
    *,
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch OHLCV bars from Polygon Aggregates endpoint.

    Returns a list of ``{time, open, high, low, close, volume}`` dicts
    compatible with the rest of CHILI's data pipeline.  Returns ``[]`` on
    failure.

    Either *period* **or** explicit *start*/*end* (YYYY-MM-DD) can be used.
    When *start* is given it takes precedence over *period*.
    """
    poly_ticker = to_polygon_ticker(ticker)

    if start:
        from_date = start if isinstance(start, str) else str(start)
        to_date = end or date.today().strftime("%Y-%m-%d")
        cache_key = f"poly:agg:{poly_ticker}:{interval}:{from_date}:{to_date}"
    else:
        from_date, to_date = _period_to_dates(period)
        cache_key = f"poly:agg:{poly_ticker}:{interval}:{period}"

    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    mapping = _TIMESPAN_MAP.get(interval, ("day", 1))
    timespan, multiplier = mapping

    url = f"{_base()}/v2/aggs/ticker/{poly_ticker}/range/{multiplier}/{timespan}/{from_date}/{to_date}"
    data = _get(url, {"adjusted": "true", "sort": "asc", "limit": "50000"})
    if not data or data.get("resultsCount", 0) == 0:
        return []

    results = data.get("results", [])
    bars: list[dict[str, Any]] = []
    for bar in results:
        bars.append({
            "time": int(bar["t"] / 1000),  # Polygon returns ms epoch
            "open": float(bar.get("o", 0)),
            "high": float(bar.get("h", 0)),
            "low": float(bar.get("l", 0)),
            "close": float(bar.get("c", 0)),
            "volume": int(bar.get("v", 0)),
        })

    _cache_set(cache_key, bars)
    return bars


def get_aggregates_df(
    ticker: str,
    interval: str = "1d",
    period: str = "6mo",
    *,
    start: str | None = None,
    end: str | None = None,
):
    """Fetch OHLCV bars and return as a pandas DataFrame matching yfinance format.

    Column names: Open, High, Low, Close, Volume.
    Index: DatetimeIndex (UTC).

    Accepts the same *start*/*end* overrides as :func:`get_aggregates`.
    """
    import pandas as pd

    bars = get_aggregates(ticker, interval=interval, period=period,
                          start=start, end=end)
    if not bars:
        return pd.DataFrame()

    df = pd.DataFrame(bars)
    df["Date"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("Date", inplace=True)
    df.rename(columns={
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }, inplace=True)
    df.drop(columns=["time"], inplace=True, errors="ignore")
    return df


# ---------------------------------------------------------------------------
# Last quote / trade (live price)
# ---------------------------------------------------------------------------

def get_last_quote(ticker: str) -> dict[str, Any] | None:
    """Fetch the latest quote/price for a ticker.

    Returns a dict with ``price``, ``previous_close``, ``day_high``,
    ``day_low``, ``volume``, etc. or ``None`` on failure.
    """
    poly_ticker = to_polygon_ticker(ticker)
    cache_key = f"poly:quote:{poly_ticker}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    _is_crypto = is_crypto(ticker)

    if _is_crypto:
        # Crypto snapshots/prev require higher plan; derive quote from latest agg bar
        result = _get_crypto_quote_from_aggs(ticker)
    else:
        result = _get_stock_snapshot(poly_ticker)

    if result:
        _cache_set(cache_key, result)
    return result


def _get_stock_snapshot(poly_ticker: str) -> dict[str, Any] | None:
    """Get stock snapshot from Polygon v2 snapshot endpoint."""
    url = f"{_base()}/v2/snapshot/locale/us/markets/stocks/tickers/{poly_ticker}"
    data = _get(url)
    if not data or not data.get("ticker"):
        # Fallback: try previous close endpoint
        return _get_stock_prev_close(poly_ticker)

    t = data["ticker"]
    day = t.get("day", {})
    prev = t.get("prevDay", {})
    last_trade = t.get("lastTrade", {})
    last_quote = t.get("lastQuote", {})

    price = last_trade.get("p") or day.get("c") or day.get("vw")
    prev_close = prev.get("c")

    return {
        "last_price": float(price) if price else None,
        "previous_close": float(prev_close) if prev_close else None,
        "day_high": float(day.get("h")) if day.get("h") else None,
        "day_low": float(day.get("l")) if day.get("l") else None,
        "volume": int(day.get("v", 0)) if day.get("v") else None,
        "market_cap": None,
        "year_high": None,
        "year_low": None,
        "avg_volume": None,
        "bid": float(last_quote.get("p", 0)) if last_quote.get("p") else None,
        "ask": float(last_quote.get("P", 0)) if last_quote.get("P") else None,
    }


def _get_stock_prev_close(poly_ticker: str) -> dict[str, Any] | None:
    """Lightweight fallback: previous day's close from Polygon."""
    url = f"{_base()}/v2/aggs/ticker/{poly_ticker}/prev"
    data = _get(url)
    if not data or not data.get("results"):
        return None
    bar = data["results"][0]
    return {
        "last_price": float(bar.get("c", 0)),
        "previous_close": float(bar.get("c", 0)),
        "day_high": float(bar.get("h")) if bar.get("h") else None,
        "day_low": float(bar.get("l")) if bar.get("l") else None,
        "volume": int(bar.get("v", 0)) if bar.get("v") else None,
        "market_cap": None,
        "year_high": None,
        "year_low": None,
        "avg_volume": None,
    }


def _get_crypto_snapshot(poly_ticker: str) -> dict[str, Any] | None:
    """Get crypto snapshot from Polygon v2 snapshot endpoint."""
    url = f"{_base()}/v2/snapshot/locale/global/markets/crypto/tickers/{poly_ticker}"
    data = _get(url)
    if not data or not data.get("ticker"):
        return _get_crypto_prev_close(poly_ticker)

    t = data["ticker"]
    day = t.get("day", {})
    prev = t.get("prevDay", {})
    last_trade = t.get("lastTrade", {})

    price = last_trade.get("p") or day.get("c") or day.get("vw")
    prev_close = prev.get("c")

    return {
        "last_price": float(price) if price else None,
        "previous_close": float(prev_close) if prev_close else None,
        "day_high": float(day.get("h")) if day.get("h") else None,
        "day_low": float(day.get("l")) if day.get("l") else None,
        "volume": int(day.get("v", 0)) if day.get("v") else None,
        "market_cap": None,
        "year_high": None,
        "year_low": None,
        "avg_volume": None,
    }


def _get_crypto_prev_close(poly_ticker: str) -> dict[str, Any] | None:
    """Lightweight fallback: previous day's close for crypto from Polygon."""
    url = f"{_base()}/v2/aggs/ticker/{poly_ticker}/prev"
    data = _get(url)
    if not data or not data.get("results"):
        return None
    bar = data["results"][0]
    return {
        "last_price": float(bar.get("c", 0)),
        "previous_close": float(bar.get("c", 0)),
        "day_high": float(bar.get("h")) if bar.get("h") else None,
        "day_low": float(bar.get("l")) if bar.get("l") else None,
        "volume": int(bar.get("v", 0)) if bar.get("v") else None,
        "market_cap": None,
        "year_high": None,
        "year_low": None,
        "avg_volume": None,
    }


def _get_crypto_quote_from_aggs(ticker: str) -> dict[str, Any] | None:
    """Derive a crypto quote from the two most recent daily aggregate bars.

    The crypto snapshot and prev-close endpoints require a higher-tier plan,
    but the aggregates endpoint works fine — so we fetch the last 5 days of
    daily bars and use the final two to compute price + previous_close.
    """
    bars = get_aggregates(ticker, interval="1d", period="5d")
    if not bars:
        return None
    last = bars[-1]
    prev_close = bars[-2]["close"] if len(bars) >= 2 else None
    return {
        "last_price": float(last["close"]),
        "previous_close": float(prev_close) if prev_close else None,
        "day_high": float(last["high"]) if last.get("high") else None,
        "day_low": float(last["low"]) if last.get("low") else None,
        "volume": int(last["volume"]) if last.get("volume") else None,
        "market_cap": None,
        "year_high": None,
        "year_low": None,
        "avg_volume": None,
    }


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------

def get_quotes_batch(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch live quotes for many tickers via Polygon snapshots.

    For stocks, uses the all-tickers snapshot endpoint (single HTTP call).
    For crypto, falls back to individual snapshot calls.
    """
    stocks = [t for t in tickers if not is_crypto(t)]
    cryptos = [t for t in tickers if is_crypto(t)]

    results: dict[str, dict[str, Any]] = {}

    # Stocks: single bulk snapshot call
    if stocks:
        stock_results = _get_stock_snapshots_bulk(stocks)
        results.update(stock_results)

    # Crypto: individual calls (Polygon doesn't have a filtered bulk crypto endpoint)
    for c in cryptos:
        q = get_last_quote(c)
        if q and q.get("last_price"):
            results[c] = q

    return results


def _get_stock_snapshots_bulk(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Get stock snapshots for many tickers in one API call."""
    ticker_param = ",".join(t.upper() for t in tickers)
    url = f"{_base()}/v2/snapshot/locale/us/markets/stocks/tickers"
    data = _get(url, {"tickers": ticker_param})
    if not data or not data.get("tickers"):
        return {}

    results: dict[str, dict[str, Any]] = {}
    for t in data["tickers"]:
        sym = t.get("ticker", "")
        day = t.get("day", {})
        prev = t.get("prevDay", {})
        last_trade = t.get("lastTrade", {})

        price = last_trade.get("p") or day.get("c") or day.get("vw")
        prev_close = prev.get("c")

        if price:
            results[sym] = {
                "last_price": float(price),
                "previous_close": float(prev_close) if prev_close else None,
                "day_high": float(day.get("h")) if day.get("h") else None,
                "day_low": float(day.get("l")) if day.get("l") else None,
                "volume": int(day.get("v", 0)) if day.get("v") else None,
                "market_cap": None,
                "year_high": None,
                "year_low": None,
                "avg_volume": None,
            }

    return results
