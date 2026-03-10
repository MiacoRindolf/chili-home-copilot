"""Rate-limited yfinance wrapper with in-memory response caching.

Modern yfinance (>=0.2.40) uses curl_cffi internally and rejects injected
requests-cache sessions.  Instead we rate-limit and cache at the wrapper level:

- A ``pyrate_limiter.Limiter`` gates all Yahoo Finance requests to 2 per 5 seconds.
- An in-memory TTL cache avoids re-fetching recently-seen data.
- ``get_ticker(symbol)`` is the single entry-point used by all services.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import yfinance as yf

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiter — 2 requests per 5 seconds (Yahoo's safe threshold)
# ---------------------------------------------------------------------------
_limiter = None
_limiter_lock = threading.Lock()


def _get_limiter():
    global _limiter
    if _limiter is not None:
        return _limiter
    with _limiter_lock:
        if _limiter is not None:
            return _limiter
        try:
            from pyrate_limiter import Duration, Rate, Limiter
            _limiter = Limiter(Rate(2, Duration.SECOND * 5))
            logger.info("[yf_session] Rate limiter active (2 req/5s)")
        except ImportError:
            _limiter = None
            logger.warning("[yf_session] pyrate-limiter not installed — no rate limiting")
        return _limiter


def acquire() -> None:
    """Block until a rate-limit token is available."""
    lim = _get_limiter()
    if lim is not None:
        try:
            lim.try_acquire("yfinance")
        except Exception:
            time.sleep(2.5)


# ---------------------------------------------------------------------------
# In-memory TTL cache for history() and fast_info results
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()

_TTL_HISTORY = 1800    # 30 minutes for OHLCV / indicator data
_TTL_QUOTE = 60        # 60 seconds for live price
_TTL_SEARCH = 3600     # 1 hour for search results
_MAX_CACHE_SIZE = 2000


def _cache_get(key: str) -> Any | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        ts, val = entry
        if time.time() - ts > _get_ttl(key):
            del _cache[key]
            return None
        return val


def _cache_set(key: str, val: Any) -> None:
    with _cache_lock:
        if len(_cache) > _MAX_CACHE_SIZE:
            cutoff = time.time() - 60
            expired = [k for k, (t, _) in _cache.items() if t < cutoff]
            for k in expired:
                del _cache[k]
        _cache[key] = (time.time(), val)


def _get_ttl(key: str) -> float:
    if key.startswith("quote:"):
        return _TTL_QUOTE
    if key.startswith("search:"):
        return _TTL_SEARCH
    return _TTL_HISTORY


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_ticker(symbol: str) -> yf.Ticker:
    """Return a plain yf.Ticker (no custom session — yfinance manages its own).

    Rate limiting happens in ``get_history`` / ``get_fast_info`` wrappers.
    For direct ``yf.Ticker`` usage the caller should call ``acquire()`` first.
    """
    acquire()
    return yf.Ticker(symbol)


def get_history(symbol: str, **kwargs) -> Any:
    """Rate-limited + cached wrapper around ``yf.Ticker(symbol).history(**kwargs)``.

    Returns a DataFrame (possibly empty on error).
    """
    import pandas as pd

    period = kwargs.get("period", "6mo")
    interval = kwargs.get("interval", "1d")
    start = kwargs.get("start")
    cache_key = f"hist:{symbol}:{period}:{interval}:{start}"

    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    acquire()
    try:
        t = yf.Ticker(symbol)
        df = t.history(**kwargs)
    except Exception as e:
        logger.warning(f"[yf_session] history({symbol}) failed: {e}")
        df = pd.DataFrame()

    _cache_set(cache_key, df)
    return df


def get_fast_info(symbol: str) -> dict[str, Any] | None:
    """Rate-limited + cached wrapper around ``yf.Ticker(symbol).fast_info``."""
    cache_key = f"quote:{symbol}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    acquire()
    try:
        t = yf.Ticker(symbol)
        info = t.fast_info
        result = {
            "last_price": float(info.last_price) if info.last_price else None,
            "previous_close": float(info.previous_close) if info.previous_close else None,
            "day_high": float(info.day_high) if hasattr(info, "day_high") and info.day_high else None,
            "day_low": float(info.day_low) if hasattr(info, "day_low") and info.day_low else None,
            "volume": int(info.last_volume) if hasattr(info, "last_volume") and info.last_volume else None,
            "market_cap": float(info.market_cap) if hasattr(info, "market_cap") and info.market_cap else None,
        }
    except Exception as e:
        logger.warning(f"[yf_session] fast_info({symbol}) failed: {e}")
        result = None

    _cache_set(cache_key, result)
    return result
