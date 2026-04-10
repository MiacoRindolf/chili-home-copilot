"""Massive.com REST + WebSocket client for stocks and crypto market data.

Provides cached, rate-limit-aware wrappers around the Massive REST API
(v2/v3 endpoints, Polygon-compatible format) and an optional WebSocket
client for real-time NBBO quote streaming.

Symbol conventions:
  - US stocks:  plain ticker like ``AAPL``, ``NVDA``, ``SPY``
  - Crypto:     ``X:BTCUSD``, ``X:ETHUSD`` (Polygon-compatible crypto prefix, no hyphen)
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import requests

from ..config import settings

logger = logging.getLogger(__name__)


def _massive_ws_allowed_in_this_process() -> bool:
    """When ``CHILI_SCHEDULER_ROLE=none``, the web container is API-only; skip WS (use REST quotes)."""
    role = (getattr(settings, "chili_scheduler_role", None) or "all").strip().lower()
    return role != "none"


# ---------------------------------------------------------------------------
# In-memory TTL cache
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()

_TTL_BARS = 3600       # 1 hour for OHLCV bars (64 GB RAM — keep longer)
_TTL_QUOTE = 30        # 30 sec for live quotes
_TTL_SNAPSHOT = 60     # 1 min for snapshots
_MAX_CACHE = 30_000    # 64 GB RAM — keep ~1000 tickers × many intervals in memory

_dead_tickers: dict[str, float] = {}
_dead_lock = threading.Lock()
_TTL_DEAD = 14400      # 4 hours — skip tickers that 404'd

_NOT_FOUND = object()  # sentinel returned by _get() on HTTP 404


def _is_dead_ticker(m_ticker: str) -> bool:
    with _dead_lock:
        ts = _dead_tickers.get(m_ticker)
        if ts is None:
            return False
        if time.time() - ts > _TTL_DEAD:
            del _dead_tickers[m_ticker]
            return False
        return True


def _mark_dead_ticker(m_ticker: str) -> None:
    with _dead_lock:
        _dead_tickers[m_ticker] = time.time()


def get_dead_tickers() -> set[str]:
    """Return the set of Massive-format tickers currently in the dead cache."""
    now = time.time()
    with _dead_lock:
        alive_cutoff = now - _TTL_DEAD
        return {t for t, ts in _dead_tickers.items() if ts > alive_cutoff}

_metrics_lock = threading.Lock()
_metrics: dict[str, int] = {
    "requests": 0,
    "cache_hits": 0,
    "cache_misses": 0,
    "errors": 0,
    "rate_limits": 0,
}


def get_metrics() -> dict[str, int]:
    with _metrics_lock:
        return dict(_metrics)


def _bump(key: str = "requests"):
    with _metrics_lock:
        _metrics[key] = _metrics.get(key, 0) + 1


# ---------------------------------------------------------------------------
# Rate governor (sliding-window per-second)
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()
_request_times: deque[float] = deque()


def _rate_limit_wait() -> None:
    max_rps = max(1, settings.massive_max_rps or 100)
    while True:
        with _rate_lock:
            now = time.time()
            while _request_times and now - _request_times[0] > 1.0:
                _request_times.popleft()
            if len(_request_times) < max_rps:
                _request_times.append(now)
                return
            oldest = _request_times[0]
            wait = max(0.0, 1.0 - (now - oldest))
        if wait > 0:
            time.sleep(wait)


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
def _mount_massive_adapters(sess: requests.Session) -> None:
    """Size urllib3 pool from settings so concurrent batch workers do not exhaust it."""
    from requests.adapters import HTTPAdapter

    pc = max(10, int(getattr(settings, "massive_http_pool_connections", 128)))
    pm = max(pc, int(getattr(settings, "massive_http_pool_maxsize", 512)))
    # Block when the pool is saturated so worker bursts queue instead of opening
    # unbounded sockets that can exhaust the host networking stack.
    adapter = HTTPAdapter(pool_connections=pc, pool_maxsize=pm, pool_block=True)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    logger.info(
        "[massive] HTTP pool api.massive.com: pool_connections=%s pool_maxsize=%s pool_block=true "
        "(raise MASSIVE_HTTP_* if requests are queuing too aggressively)",
        pc,
        pm,
    )


_session = requests.Session()
_session.headers.update({"Accept": "application/json"})
_mount_massive_adapters(_session)

_MAX_RETRIES = 2
_BACKOFF_BASE = 1.0


def _api_key() -> str:
    return settings.massive_api_key


def _base() -> str:
    return settings.massive_base_url.rstrip("/")


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
                logger.warning(f"[massive] 429 rate-limited, backing off {wait:.1f}s")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                _bump("errors")
                wait = _BACKOFF_BASE * (2 ** attempt)
                logger.warning(f"[massive] {resp.status_code} server error, retry in {wait:.1f}s")
                time.sleep(wait)
                continue
            _bump("errors")
            if resp.status_code == 404:
                logger.debug(f"[massive] 404 for {url}")
                return _NOT_FOUND
            logger.warning(f"[massive] {resp.status_code} for {url}: {resp.text[:200]}")
            return None
        except requests.RequestException as e:
            _bump("errors")
            if attempt < _MAX_RETRIES:
                time.sleep(_BACKOFF_BASE)
                continue
            logger.warning(f"[massive] request failed after {_MAX_RETRIES + 1} attempts: {e}")
            return None
    return None


# ---------------------------------------------------------------------------
# Symbol helpers
# ---------------------------------------------------------------------------

def is_crypto(ticker: str) -> bool:
    """True for ``BASE-USD``, bare ``BASEUSD`` (Massive/Polygon style), not ``X:`` tickers."""
    t = ticker.upper().strip()
    if t.startswith("X:"):
        return False
    if t.endswith("-USD"):
        return True
    return _bare_concat_crypto_usd(t)


def _bare_concat_crypto_usd(ticker: str) -> bool:
    """True for symbols like ``ZKUSD`` (5+ chars) / ``BTCUSD`` (no hyphen before USD)."""
    t = ticker.upper().strip()
    if "-" in t or not t.endswith("USD"):
        return False
    base = t[:-3]
    return base.isalnum() and 2 <= len(base) <= 15


def _massive_url_ticker_ok(m_ticker: str) -> bool:
    """False when *m_ticker* is blank — must not be embedded in Massive path segments."""
    return bool((m_ticker or "").strip())


def to_massive_ticker(ticker: str) -> str:
    """Convert app-internal ticker to Massive/Polygon-compatible symbol format.

    Polygon crypto format uses ``X:BTCUSD`` (no hyphen), while the app uses
    ``BTC-USD`` (yfinance style). Bare ``ZKUSD`` is also accepted. Stocks pass through unchanged.
    """
    t = ticker.upper().strip()
    if is_crypto(t):
        return f"X:{t.replace('-', '')}"
    if _bare_concat_crypto_usd(t):
        return f"X:{t}"
    if t.startswith("X:") and "-" in t:
        return t.replace("-", "")
    return t


def _crypto_base_for_quote_variants(ticker: str) -> str | None:
    """Asset base (e.g. ``ZK``) for building ``X:ZKUSD`` / ``X:ZKUSDT`` Massive symbols."""
    t = ticker.upper().strip()
    if is_crypto(t):
        return t.replace("-", "")[:-3]
    if _bare_concat_crypto_usd(t):
        return t[:-3]
    if t.startswith("X:"):
        sym = t[2:]
        for suf in ("USDT", "USDC", "USD"):
            if sym.endswith(suf) and len(sym) > len(suf):
                return sym[: -len(suf)]
    return None


def crypto_aggregate_symbol_candidates(ticker: str) -> list[str]:
    """Massive/Polygon aggregate tickers to try for one logical crypto pair.

    Providers list the same asset under ``X:BASEUSD``, ``X:BASEUSDT``, etc.
    Stocks return a single candidate.
    """
    if not (ticker or "").strip():
        return []
    primary = to_massive_ticker(ticker)
    out: list[str] = [primary]
    base = _crypto_base_for_quote_variants(ticker)
    if not base:
        return out
    for quote in ("USD", "USDT", "USDC"):
        sym = f"X:{base}{quote}"
        if sym not in out:
            out.append(sym)
    return out


def massive_aggregate_variants_all_dead(ticker: str) -> bool:
    """True when every Massive symbol variant for *ticker* is in the dead cache."""
    for sym in crypto_aggregate_symbol_candidates(ticker):
        if not _is_dead_ticker(sym):
            return False
    return True


def from_massive_ticker(m_ticker: str) -> str:
    """Convert Polygon-format ``X:BTCUSD`` back to app-internal ``BTC-USD``."""
    t = m_ticker.upper()
    if t.startswith("X:"):
        sym = t[2:]
        if sym.endswith("USDT") and len(sym) > 4:
            return f"{sym[:-4]}-USD"
        if sym.endswith("USDC") and len(sym) > 4:
            return f"{sym[:-4]}-USD"
        if sym.endswith("USD") and len(sym) > 3:
            return f"{sym[:-3]}-USD"
        return sym
    return t


# ---------------------------------------------------------------------------
# Aggregates (OHLCV bars)
# ---------------------------------------------------------------------------

_TIMESPAN_MAP = {
    "1m": ("minute", 1), "2m": ("minute", 2), "5m": ("minute", 5),
    "15m": ("minute", 15), "30m": ("minute", 30),
    "1h": ("hour", 1), "60m": ("hour", 1), "90m": ("minute", 90),
    "1d": ("day", 1), "5d": ("day", 5),
    "1wk": ("week", 1), "1mo": ("month", 1), "3mo": ("month", 3),
}

_PERIOD_DAYS = {
    "1d": 1, "5d": 5, "1mo": 30, "3mo": 90, "6mo": 180,
    "1y": 365, "2y": 730, "5y": 1825, "ytd": None, "max": 7300,
}


def _period_to_dates(period: str) -> tuple[str, str]:
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
    """Fetch OHLCV bars from Massive aggregates endpoint.

    Either *period* **or** explicit *start*/*end* (YYYY-MM-DD) can be used.
    When *start* is given it takes precedence over *period*.

    Crypto: Massive may list the same asset as ``X:BASEUSD``, ``X:BASEUSDT``, etc.
    We try those variants (and accept bare ``ZKUSD`` as well as ``ZK-USD``).
    """
    if not (ticker or "").strip():
        return []

    if start:
        from_date = start if isinstance(start, str) else str(start)
        to_date = end or date.today().strftime("%Y-%m-%d")
    else:
        from_date, to_date = _period_to_dates(period)

    mapping = _TIMESPAN_MAP.get(interval, ("day", 1))
    timespan, multiplier = mapping

    def _try_symbol(m_ticker: str) -> list[dict[str, Any]]:
        if start:
            cache_key = f"massive:agg:{m_ticker}:{interval}:{from_date}:{to_date}"
        else:
            cache_key = f"massive:agg:{m_ticker}:{interval}:{period}"
        if timespan in ("minute", "hour"):
            cache_key = f"{cache_key}|ic"
        cache_key = f"{cache_key}:pg1"

        if not _massive_url_ticker_ok(m_ticker):
            return []

        if _is_dead_ticker(m_ticker):
            return []

        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        def _bars_from_response(data: dict[str, Any] | None) -> list[dict[str, Any]]:
            if not data or data is _NOT_FOUND:
                return []
            out: list[dict[str, Any]] = []
            for bar in data.get("results", []) or []:
                out.append({
                    "time": int(bar["t"] / 1000),
                    "open": float(bar.get("o", 0)),
                    "high": float(bar.get("h", 0)),
                    "low": float(bar.get("l", 0)),
                    "close": float(bar.get("c", 0)),
                    "volume": int(bar.get("v", 0)),
                })
            return out

        def _fetch_one_range(f_d: str, t_d: str) -> tuple[list[dict[str, Any]], bool]:
            """Return (bars, ticker_dead)."""
            url = f"{_base()}/v2/aggs/ticker/{m_ticker}/range/{multiplier}/{timespan}/{f_d}/{t_d}"
            data = _get(url, {"adjusted": "true", "sort": "asc", "limit": "50000"})
            if data is _NOT_FOUND:
                return [], True
            acc = _bars_from_response(data)
            next_u = (data or {}).get("next_url")
            pages = 0
            while next_u and pages < 200:
                pages += 1
                nu = str(next_u).strip()
                if not nu.startswith("http"):
                    nu = f"{_base()}{nu}" if nu.startswith("/") else f"{_base()}/{nu}"
                _rate_limit_wait()
                _bump("requests")
                try:
                    r2 = _session.get(nu, timeout=30)
                    if r2.status_code != 200:
                        break
                    d2 = r2.json()
                except Exception:
                    break
                acc.extend(_bars_from_response(d2))
                next_u = (d2 or {}).get("next_url")
            return acc, False

        if timespan in ("minute", "hour"):
            from .trading.ohlcv_aggregate_fetch import iter_intraday_date_chunks

            merged: list[dict[str, Any]] = []
            seen_ts: set[int] = set()
            for f_str, t_str in iter_intraday_date_chunks(from_date, to_date):
                part, dead = _fetch_one_range(f_str, t_str)
                if dead:
                    continue
                for b in part:
                    t0 = b["time"]
                    if t0 in seen_ts:
                        continue
                    seen_ts.add(t0)
                    merged.append(b)
            merged.sort(key=lambda x: x["time"])
            if not merged:
                return []
            _cache_set(cache_key, merged)
            return merged

        # Day / week / month: API may paginate via next_url; single-page fetch truncated history.
        bars, dead = _fetch_one_range(from_date, to_date)
        if dead:
            _mark_dead_ticker(m_ticker)
            return []
        if not bars:
            return []
        bars.sort(key=lambda x: x["time"])
        _cache_set(cache_key, bars)
        return bars

    for sym in crypto_aggregate_symbol_candidates(ticker):
        got = _try_symbol(sym)
        if got:
            return got
    return []


def get_aggregates_df(
    ticker: str,
    interval: str = "1d",
    period: str = "6mo",
    *,
    start: str | None = None,
    end: str | None = None,
):
    """Fetch OHLCV as a pandas DataFrame (Open/High/Low/Close/Volume columns).

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
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    }, inplace=True)
    df.drop(columns=["time"], inplace=True, errors="ignore")
    return df


# ---------------------------------------------------------------------------
# Snapshot / live quote
# ---------------------------------------------------------------------------

def get_last_quote(ticker: str) -> dict[str, Any] | None:
    """Fetch the latest quote/price for a ticker via snapshot endpoint."""
    m_ticker = to_massive_ticker(ticker)
    if not _massive_url_ticker_ok(m_ticker):
        return None

    if _is_dead_ticker(m_ticker):
        return None

    cache_key = f"massive:quote:{m_ticker}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if is_crypto(ticker):
        result = _get_crypto_snapshot(m_ticker, ticker)
    else:
        result = _get_stock_snapshot(m_ticker)

    if result:
        _cache_set(cache_key, result)
    return result


def _get_stock_snapshot(m_ticker: str) -> dict[str, Any] | None:
    if not _massive_url_ticker_ok(m_ticker):
        return None
    url = f"{_base()}/v2/snapshot/locale/us/markets/stocks/tickers/{m_ticker}"
    data = _get(url)
    if data is _NOT_FOUND:
        _mark_dead_ticker(m_ticker)
        return None
    if not data or not data.get("ticker"):
        return _get_prev_close(m_ticker)

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


def _get_crypto_snapshot(m_ticker: str, orig_ticker: str) -> dict[str, Any] | None:
    if not _massive_url_ticker_ok(m_ticker):
        return None
    url = f"{_base()}/v2/snapshot/locale/global/markets/crypto/tickers/{m_ticker}"
    data = _get(url)
    if data is _NOT_FOUND:
        _mark_dead_ticker(m_ticker)
        return None
    if not data or not data.get("ticker"):
        return _get_crypto_quote_from_aggs(orig_ticker)

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


def _get_prev_close(m_ticker: str) -> dict[str, Any] | None:
    if not _massive_url_ticker_ok(m_ticker):
        return None
    url = f"{_base()}/v2/aggs/ticker/{m_ticker}/prev"
    data = _get(url)
    if data is _NOT_FOUND or not data or not data.get("results"):
        return None
    bar = data["results"][0]
    return {
        "last_price": float(bar.get("c", 0)),
        "previous_close": float(bar.get("c", 0)),
        "day_high": float(bar.get("h")) if bar.get("h") else None,
        "day_low": float(bar.get("l")) if bar.get("l") else None,
        "volume": int(bar.get("v", 0)) if bar.get("v") else None,
        "market_cap": None, "year_high": None, "year_low": None, "avg_volume": None,
    }


def _get_crypto_quote_from_aggs(ticker: str) -> dict[str, Any] | None:
    """Derive a crypto quote from recent daily aggregate bars."""
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
        "market_cap": None, "year_high": None, "year_low": None, "avg_volume": None,
    }


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------

def get_aggregates_batch(
    tickers: list[str],
    interval: str = "1d",
    period: str = "6mo",
    *,
    max_workers: int = 0,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch OHLCV bars for many tickers concurrently.

    Saturates the rate limiter (up to ``massive_max_rps``) by dispatching
    requests through a thread pool.  Results are stored in the module-level
    cache so subsequent :func:`get_aggregates` calls are instant cache hits.
    """
    if not _api_key():
        return {}
    if max_workers <= 0:
        max_workers = min(80, max(30, settings.massive_max_rps))
    # Use at most half the urllib3 pool for this batch (scan/backtest may overlap other requests)
    _pool_cap = max(16, int(settings.massive_http_pool_maxsize) // 2)
    max_workers = min(max_workers, _pool_cap)

    uncached: list[str] = []
    results: dict[str, list[dict[str, Any]]] = {}
    _map_b = _TIMESPAN_MAP.get(interval, ("day", 1))
    _ic_suffix = "|ic" if _map_b[0] in ("minute", "hour") else ""
    for t in tickers:
        if massive_aggregate_variants_all_dead(t):
            continue
        m_ticker = to_massive_ticker(t)
        cache_key = f"massive:agg:{m_ticker}:{interval}:{period}{_ic_suffix}:pg1"
        cached = _cache_get(cache_key)
        if cached is not None:
            results[t] = cached
        else:
            uncached.append(t)

    if not uncached:
        return results

    def _fetch_one(ticker: str) -> tuple[str, list[dict[str, Any]]]:
        bars = get_aggregates(ticker, interval=interval, period=period)
        return ticker, bars

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, t): t for t in uncached}
        for fut in as_completed(futures):
            try:
                sym, bars = fut.result(timeout=30)
                if bars:
                    results[sym] = bars
            except Exception:
                pass

    return results


def get_aggregates_df_batch(
    tickers: list[str],
    interval: str = "1d",
    period: str = "6mo",
    *,
    max_workers: int = 0,
):
    """Fetch OHLCV DataFrames for many tickers concurrently.

    Returns ``{ticker: DataFrame}`` and populates the aggregates cache.
    """
    import pandas as pd

    raw = get_aggregates_batch(
        tickers, interval=interval, period=period, max_workers=max_workers,
    )
    dfs: dict[str, pd.DataFrame] = {}
    for sym, bars in raw.items():
        if not bars:
            continue
        df = pd.DataFrame(bars)
        df["Date"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("Date", inplace=True)
        df.rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume",
        }, inplace=True)
        df.drop(columns=["time"], inplace=True, errors="ignore")
        dfs[sym] = df
    return dfs


def get_quotes_batch(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch live quotes for many tickers via snapshots."""
    stocks = [t for t in tickers if not is_crypto(t)]
    cryptos = [t for t in tickers if is_crypto(t)]
    results: dict[str, dict[str, Any]] = {}

    if stocks:
        results.update(_get_stock_snapshots_bulk(stocks))

    if cryptos:
        results.update(_get_crypto_snapshots_batch(cryptos))

    return results


def _get_crypto_snapshots_batch(
    tickers: list[str], max_workers: int = 50,
) -> dict[str, dict[str, Any]]:
    """Fetch crypto quotes concurrently instead of one-by-one."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: dict[str, dict[str, Any]] = {}
    _pool_cap = max(16, int(settings.massive_http_pool_maxsize) // 2)
    max_workers = min(max_workers, _pool_cap)

    def _fetch(t: str):
        q = get_last_quote(t)
        return t, q

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch, c): c for c in tickers}
        for fut in as_completed(futures):
            try:
                sym, q = fut.result(timeout=15)
                if q and q.get("last_price"):
                    results[sym] = q
            except Exception:
                pass
    return results


def _get_stock_snapshots_bulk(tickers: list[str]) -> dict[str, dict[str, Any]]:
    ticker_param = ",".join(t.upper() for t in tickers)
    url = f"{_base()}/v2/snapshot/locale/us/markets/stocks/tickers"
    data = _get(url, {"tickers": ticker_param})
    if data is _NOT_FOUND or not data or not data.get("tickers"):
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
                "market_cap": None, "year_high": None, "year_low": None, "avg_volume": None,
            }
    return results


# ---------------------------------------------------------------------------
# WebSocket quote cache (optional real-time streaming)
# ---------------------------------------------------------------------------

@dataclass
class QuoteSnapshot:
    price: float
    bid: float | None = None
    ask: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    timestamp: float = 0.0  # time.time() when received


@dataclass
class TradeSnapshot:
    price: float
    size: int = 0
    timestamp: float = 0.0


_ws_cache: dict[str, QuoteSnapshot] = {}
_ws_cache_lock = threading.Lock()
_WS_STALENESS = 5.0  # seconds before a WS quote is considered stale

# Tick listener registry — callbacks receive (symbol, QuoteSnapshot|TradeSnapshot)
from typing import Callable
TickCallback = Callable[[str, QuoteSnapshot | TradeSnapshot], None]
_tick_listeners: dict[str, list[TickCallback]] = {}
_tick_listeners_lock = threading.Lock()


def register_tick_listener(ticker: str, callback: TickCallback) -> None:
    """Register *callback* to receive every tick for *ticker*."""
    sym = ticker.upper()
    with _tick_listeners_lock:
        _tick_listeners.setdefault(sym, []).append(callback)


def unregister_tick_listener(ticker: str, callback: TickCallback) -> None:
    """Remove a previously registered tick listener."""
    sym = ticker.upper()
    with _tick_listeners_lock:
        cbs = _tick_listeners.get(sym)
        if cbs:
            try:
                cbs.remove(callback)
            except ValueError:
                pass
            if not cbs:
                del _tick_listeners[sym]


def _fire_tick_listeners(sym: str, snap: QuoteSnapshot | TradeSnapshot) -> None:
    with _tick_listeners_lock:
        cbs = list(_tick_listeners.get(sym, []))
    for cb in cbs:
        try:
            cb(sym, snap)
        except Exception:
            pass


def get_ws_quote(ticker: str) -> QuoteSnapshot | None:
    """Return a fresh WebSocket-cached quote, or None if stale/missing."""
    with _ws_cache_lock:
        snap = _ws_cache.get(ticker.upper())
    if snap is None:
        return None
    if time.time() - snap.timestamp > _WS_STALENESS:
        return None
    return snap


class MassiveWSClient:
    """Background WebSocket client that streams NBBO quotes from Massive.

    Usage::

        ws = MassiveWSClient()
        ws.start(["AAPL", "NVDA", "TSLA"])
        # Later...
        snap = get_ws_quote("AAPL")
    """

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._ws = None
        self._subscriptions: set[str] = set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, tickers: list[str] | None = None):
        if not _massive_ws_allowed_in_this_process():
            logger.info(
                "[massive-ws] WebSocket disabled in this process (CHILI_SCHEDULER_ROLE=none)"
            )
            return
        if not settings.massive_api_key or not settings.massive_use_websocket:
            logger.info("[massive-ws] WebSocket disabled or no API key")
            return
        if self.running:
            if tickers:
                self.subscribe(tickers)
            return

        self._stop_event.clear()
        self._subscriptions = {t.upper() for t in (tickers or [])}
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="massive-ws",
        )
        self._thread.start()
        logger.info("[massive-ws] WebSocket client started")

    def stop(self):
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        logger.info("[massive-ws] WebSocket client stopped")

    def subscribe(self, tickers: list[str]):
        new = {t.upper() for t in tickers} - self._subscriptions
        if not new or not self._ws:
            self._subscriptions.update(t.upper() for t in tickers)
            return
        self._subscriptions.update(new)
        try:
            params = ",".join(
                f"Q.{t},T.{t}" for t in new
            )
            sub_msg = json.dumps({"action": "subscribe", "params": params})
            self._ws.send(sub_msg)
        except Exception as e:
            logger.warning(f"[massive-ws] subscribe error: {e}")

    def _run(self):
        try:
            import websocket as ws_lib
        except ImportError:
            logger.warning("[massive-ws] websocket-client not installed; WS disabled")
            return

        base = settings.massive_ws_url.rstrip("/")
        url = f"{base}/stocks"

        while not self._stop_event.is_set():
            try:
                self._ws = ws_lib.create_connection(url, timeout=30)
                self._authenticate()
                self._subscribe_all()

                while not self._stop_event.is_set():
                    try:
                        raw = self._ws.recv()
                    except Exception:
                        break
                    self._handle_messages(raw)

            except Exception as e:
                logger.warning(f"[massive-ws] connection error: {e}")
            finally:
                if self._ws:
                    try:
                        self._ws.close()
                    except Exception:
                        pass
                    self._ws = None
            if not self._stop_event.is_set():
                time.sleep(2)

    def _authenticate(self):
        auth_msg = json.dumps({"action": "auth", "params": settings.massive_api_key})
        self._ws.send(auth_msg)
        resp = self._ws.recv()
        logger.debug(f"[massive-ws] auth response: {resp[:200]}")

    def _subscribe_all(self):
        if not self._subscriptions:
            return
        params = ",".join(
            f"Q.{t},T.{t}" for t in self._subscriptions
        )
        self._ws.send(json.dumps({"action": "subscribe", "params": params}))

    def _handle_messages(self, raw: str):
        try:
            msgs = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(msgs, list):
            msgs = [msgs]
        now = time.time()
        for msg in msgs:
            ev = msg.get("ev")
            sym = msg.get("sym", "").upper()
            if not sym:
                continue

            if ev == "Q":
                snap = QuoteSnapshot(
                    price=(msg.get("bp", 0) + msg.get("ap", 0)) / 2 if msg.get("bp") and msg.get("ap") else msg.get("bp") or msg.get("ap") or 0,
                    bid=msg.get("bp"),
                    ask=msg.get("ap"),
                    bid_size=msg.get("bs"),
                    ask_size=msg.get("as"),
                    timestamp=now,
                )
                with _ws_cache_lock:
                    _ws_cache[sym] = snap
                _fire_tick_listeners(sym, snap)

            elif ev == "T":
                trade = TradeSnapshot(
                    price=float(msg.get("p", 0)),
                    size=int(msg.get("s", 0)),
                    timestamp=now,
                )
                _fire_tick_listeners(sym, trade)


# Singleton instance
_ws_client: MassiveWSClient | None = None


def get_ws_client() -> MassiveWSClient:
    global _ws_client
    if _ws_client is None:
        _ws_client = MassiveWSClient()
    return _ws_client


# ---------------------------------------------------------------------------
# Full Market Snapshot (for prescreener use)
# ---------------------------------------------------------------------------

_snapshot_lock = threading.Lock()
_snapshot_cache: tuple[float, list[dict[str, Any]]] | None = None
_TTL_FULL_SNAPSHOT = 1800  # 30 min

def get_full_market_snapshot(*, include_otc: bool = False) -> list[dict[str, Any]]:
    """Fetch the entire US stock market snapshot (~10K tickers) in one call.

    Returns a list of raw ticker snapshot dicts as returned by Massive.
    Cached for 30 minutes so all prescreener filters share one API call.
    """
    global _snapshot_cache
    if _snapshot_cache is not None:
        ts, data = _snapshot_cache
        if time.time() - ts < _TTL_FULL_SNAPSHOT:
            return data

    with _snapshot_lock:
        if _snapshot_cache is not None:
            ts, data = _snapshot_cache
            if time.time() - ts < _TTL_FULL_SNAPSHOT:
                return data

        url = f"{_base()}/v2/snapshot/locale/us/markets/stocks/tickers"
        params: dict[str, Any] = {}
        if include_otc:
            params["include_otc"] = "true"
        resp = _get(url, params)
        if resp is _NOT_FOUND or not resp or not resp.get("tickers"):
            logger.warning("[massive] Full market snapshot returned no tickers")
            return []

        tickers = resp["tickers"]
        logger.info("[massive] Full market snapshot: %d tickers", len(tickers))
        _snapshot_cache = (time.time(), tickers)
        return tickers


def get_top_movers(direction: str = "gainers") -> list[dict[str, Any]]:
    """Fetch top 20 gainers or losers via the dedicated endpoint.

    *direction* must be ``"gainers"`` or ``"losers"``.
    """
    cache_key = f"massive:movers:{direction}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"{_base()}/v2/snapshot/locale/us/markets/stocks/{direction}"
    data = _get(url)
    if data is _NOT_FOUND or not data or not data.get("tickers"):
        return []
    tickers = data["tickers"]
    _cache_set(cache_key, tickers)
    return tickers


# ---------------------------------------------------------------------------
# Technical indicators (RSI, SMA) — per-ticker
# ---------------------------------------------------------------------------

def get_rsi(ticker: str, *, window: int = 14, timespan: str = "day",
            limit: int = 1) -> float | None:
    """Return the latest RSI value for *ticker*, or None on failure."""
    m_ticker = to_massive_ticker(ticker)
    cache_key = f"massive:rsi:{m_ticker}:{window}:{timespan}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"{_base()}/v1/indicators/rsi/{m_ticker}"
    data = _get(url, {
        "timespan": timespan,
        "window": str(window),
        "series_type": "close",
        "limit": str(limit),
        "order": "desc",
    })
    if data is _NOT_FOUND or not data:
        return None
    results = data.get("results", {}).get("values", [])
    if not results:
        return None
    val = results[0].get("value")
    if val is not None:
        _cache_set(cache_key, float(val))
        return float(val)
    return None


def get_sma(ticker: str, *, window: int = 20, timespan: str = "day",
            limit: int = 1) -> float | None:
    """Return the latest SMA value for *ticker*, or None on failure."""
    m_ticker = to_massive_ticker(ticker)
    cache_key = f"massive:sma:{m_ticker}:{window}:{timespan}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"{_base()}/v1/indicators/sma/{m_ticker}"
    data = _get(url, {
        "timespan": timespan,
        "window": str(window),
        "series_type": "close",
        "limit": str(limit),
        "order": "desc",
    })
    if data is _NOT_FOUND or not data:
        return None
    results = data.get("results", {}).get("values", [])
    if not results:
        return None
    val = results[0].get("value")
    if val is not None:
        _cache_set(cache_key, float(val))
        return float(val)
    return None


# ---------------------------------------------------------------------------
# Benzinga partner endpoints (earnings, analyst ratings)
# ---------------------------------------------------------------------------

def get_benzinga_ratings(*, action: str = "upgrade", limit: int = 100) -> list[str]:
    """Return tickers with recent analyst rating actions via Benzinga.

    Gracefully returns ``[]`` if the Massive plan lacks Benzinga access.
    """
    cache_key = f"massive:bz_ratings:{action}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"{_base()}/v2/reference/news/benzinga/analyst_ratings"
    data = _get(url, {"action": action, "limit": str(limit)})
    if data is _NOT_FOUND or not data:
        return []
    tickers: list[str] = []
    for item in data.get("results", []):
        t = item.get("ticker")
        if t:
            tickers.append(t)
    _cache_set(cache_key, tickers)
    return tickers


def get_benzinga_earnings(*, limit: int = 100) -> list[str]:
    """Return tickers with upcoming earnings via Benzinga.

    Gracefully returns ``[]`` if the Massive plan lacks Benzinga access.
    """
    cache_key = "massive:bz_earnings"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"{_base()}/v2/reference/news/benzinga/earnings"
    data = _get(url, {"limit": str(limit)})
    if data is _NOT_FOUND or not data:
        return []
    tickers: list[str] = []
    for item in data.get("results", []):
        t = item.get("ticker")
        if t:
            tickers.append(t)
    _cache_set(cache_key, tickers)
    return tickers


# ---------------------------------------------------------------------------
# Snapshot-based screener helpers (filter the cached full snapshot)
# ---------------------------------------------------------------------------

def _snap_tickers(snaps: list[dict[str, Any]]) -> list[str]:
    """Extract ticker symbols from snapshot objects."""
    return [s.get("ticker", "") for s in snaps if s.get("ticker")]


def screen_most_active(limit: int = 200) -> list[str]:
    """Top stocks by today's volume."""
    snaps = get_full_market_snapshot()
    if not snaps:
        return []
    valid = [s for s in snaps if (s.get("day") or {}).get("v", 0) > 0]
    valid.sort(key=lambda s: s.get("day", {}).get("v", 0), reverse=True)
    return _snap_tickers(valid[:limit])


def screen_top_gainers(limit: int = 100) -> list[str]:
    """Top gaining stocks by % change today."""
    movers = get_top_movers("gainers")
    if movers:
        return _snap_tickers(movers[:limit])
    snaps = get_full_market_snapshot()
    if not snaps:
        return []
    valid = [s for s in snaps
             if s.get("todaysChangePerc") is not None
             and (s.get("day") or {}).get("v", 0) >= 10_000]
    valid.sort(key=lambda s: s.get("todaysChangePerc", 0), reverse=True)
    return _snap_tickers(valid[:limit])


def screen_top_losers(limit: int = 100) -> list[str]:
    """Top losing stocks by % change today."""
    movers = get_top_movers("losers")
    if movers:
        return _snap_tickers(movers[:limit])
    snaps = get_full_market_snapshot()
    if not snaps:
        return []
    valid = [s for s in snaps
             if s.get("todaysChangePerc") is not None
             and (s.get("day") or {}).get("v", 0) >= 10_000]
    valid.sort(key=lambda s: s.get("todaysChangePerc", 0))
    return _snap_tickers(valid[:limit])


def screen_most_volatile(limit: int = 100, min_price: float = 1.0) -> list[str]:
    """Stocks with the largest intraday range relative to close."""
    snaps = get_full_market_snapshot()
    if not snaps:
        return []
    scored: list[tuple[str, float]] = []
    for s in snaps:
        day = s.get("day") or {}
        h, l, c = day.get("h", 0), day.get("l", 0), day.get("c", 0)
        if c < min_price or h <= 0 or l <= 0:
            continue
        volatility = (h - l) / c
        scored.append((s.get("ticker", ""), volatility))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in scored[:limit]]


def screen_unusual_volume(limit: int = 200) -> list[str]:
    """Stocks where today's volume is significantly higher than previous day."""
    snaps = get_full_market_snapshot()
    if not snaps:
        return []
    scored: list[tuple[str, float]] = []
    for s in snaps:
        day_v = (s.get("day") or {}).get("v", 0)
        prev_v = (s.get("prevDay") or {}).get("v", 0)
        if prev_v < 50_000 or day_v < 10_000:
            continue
        ratio = day_v / prev_v
        if ratio > 1.5:
            scored.append((s.get("ticker", ""), ratio))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in scored[:limit]]


def screen_high_volume(limit: int = 200, min_vol: int = 1_000_000,
                       min_price: float = 5.0) -> list[str]:
    """Liquid stocks with high volume and minimum price."""
    snaps = get_full_market_snapshot()
    if not snaps:
        return []
    valid: list[tuple[str, int]] = []
    for s in snaps:
        day = s.get("day") or {}
        v = day.get("v", 0)
        c = day.get("c", 0)
        if v >= min_vol and c >= min_price:
            valid.append((s.get("ticker", ""), v))
    valid.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in valid[:limit]]


def screen_high_relative_volume(limit: int = 200, min_ratio: float = 2.0,
                                min_prev_vol: int = 200_000,
                                min_price: float = 2.0) -> list[str]:
    """Stocks with today's volume > min_ratio * previous day's volume."""
    snaps = get_full_market_snapshot()
    if not snaps:
        return []
    scored: list[tuple[str, float]] = []
    for s in snaps:
        day = s.get("day") or {}
        prev = s.get("prevDay") or {}
        day_v, prev_v = day.get("v", 0), prev.get("v", 0)
        c = day.get("c", 0)
        if prev_v < min_prev_vol or c < min_price:
            continue
        ratio = day_v / prev_v if prev_v > 0 else 0
        if ratio >= min_ratio:
            scored.append((s.get("ticker", ""), ratio))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in scored[:limit]]


def screen_new_high(limit: int = 100) -> list[str]:
    """Stocks making new highs today (today's high > previous day high, large % gain)."""
    snaps = get_full_market_snapshot()
    if not snaps:
        return []
    hits: list[tuple[str, float]] = []
    for s in snaps:
        day = s.get("day") or {}
        prev = s.get("prevDay") or {}
        pct = s.get("todaysChangePerc", 0)
        if day.get("h", 0) > prev.get("h", 0) and pct > 0 and day.get("v", 0) >= 50_000:
            hits.append((s.get("ticker", ""), pct))
    hits.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in hits[:limit]]


def screen_momentum_gappers(limit: int = 100) -> list[str]:
    """Stocks gapping up > 5% with high relative volume (day-trade setup)."""
    snaps = get_full_market_snapshot()
    if not snaps:
        return []
    scored: list[tuple[str, float]] = []
    for s in snaps:
        pct = s.get("todaysChangePerc", 0) or 0
        day = s.get("day") or {}
        prev = s.get("prevDay") or {}
        c = day.get("c", 0)
        day_v = day.get("v", 0)
        prev_v = prev.get("v", 0)
        if pct < 5 or c < 2 or c > 20 or day_v < 100_000:
            continue
        rel_vol = day_v / prev_v if prev_v > 0 else 0
        if rel_vol >= 2:
            scored.append((s.get("ticker", ""), pct))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in scored[:limit]]
