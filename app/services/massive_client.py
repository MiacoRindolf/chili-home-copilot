"""Massive.com REST + WebSocket client for stocks and crypto market data.

Provides cached, rate-limit-aware wrappers around the Massive REST API
(v2/v3 endpoints, Polygon-compatible format) and an optional WebSocket
client for real-time NBBO quote streaming.

Symbol conventions:
  - US stocks:  plain ticker like ``AAPL``, ``NVDA``, ``SPY``
  - Crypto:     ``X:BTC-USD``, ``X:ETH-USD`` (Polygon-compatible crypto prefix)
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

# ---------------------------------------------------------------------------
# In-memory TTL cache
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()

_TTL_BARS = 1800       # 30 min for OHLCV bars
_TTL_QUOTE = 30        # 30 sec for live quotes
_TTL_SNAPSHOT = 60     # 1 min for snapshots
_MAX_CACHE = 8000      # support heavy learning runs (500+ tickers × multiple endpoints)

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
_session = requests.Session()
_session.headers.update({"Accept": "application/json"})
from requests.adapters import HTTPAdapter
_session.mount("https://", HTTPAdapter(pool_connections=40, pool_maxsize=40))
_session.mount("http://", HTTPAdapter(pool_connections=40, pool_maxsize=40))

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
    t = ticker.upper()
    return t.endswith("-USD") and not t.startswith("X:")


def to_massive_ticker(ticker: str) -> str:
    """Convert app-internal ticker to Massive/Polygon-compatible symbol format."""
    t = ticker.upper()
    if is_crypto(t):
        return f"X:{t}"
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
    """
    m_ticker = to_massive_ticker(ticker)

    if _is_dead_ticker(m_ticker):
        return []

    if start:
        from_date = start if isinstance(start, str) else str(start)
        to_date = end or date.today().strftime("%Y-%m-%d")
        cache_key = f"massive:agg:{m_ticker}:{interval}:{from_date}:{to_date}"
    else:
        from_date, to_date = _period_to_dates(period)
        cache_key = f"massive:agg:{m_ticker}:{interval}:{period}"

    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    mapping = _TIMESPAN_MAP.get(interval, ("day", 1))
    timespan, multiplier = mapping

    url = f"{_base()}/v2/aggs/ticker/{m_ticker}/range/{multiplier}/{timespan}/{from_date}/{to_date}"
    data = _get(url, {"adjusted": "true", "sort": "asc", "limit": "50000"})
    if data is _NOT_FOUND:
        _mark_dead_ticker(m_ticker)
        return []
    if not data or data.get("resultsCount", 0) == 0:
        return []

    bars: list[dict[str, Any]] = []
    for bar in data.get("results", []):
        bars.append({
            "time": int(bar["t"] / 1000),
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
        max_workers = min(60, max(20, settings.massive_max_rps))

    uncached: list[str] = []
    results: dict[str, list[dict[str, Any]]] = {}
    for t in tickers:
        m_ticker = to_massive_ticker(t)
        if _is_dead_ticker(m_ticker):
            continue
        cache_key = f"massive:agg:{m_ticker}:{interval}:{period}"
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
    tickers: list[str], max_workers: int = 30,
) -> dict[str, dict[str, Any]]:
    """Fetch crypto quotes concurrently instead of one-by-one."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: dict[str, dict[str, Any]] = {}

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


_ws_cache: dict[str, QuoteSnapshot] = {}
_ws_cache_lock = threading.Lock()
_WS_STALENESS = 5.0  # seconds before a WS quote is considered stale


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
            sub_msg = json.dumps({
                "action": "subscribe",
                "params": ",".join(f"Q.{t}" for t in new),
            })
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
        params = ",".join(f"Q.{t}" for t in self._subscriptions)
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
            if msg.get("ev") != "Q":
                continue
            sym = msg.get("sym", "").upper()
            if not sym:
                continue
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


# Singleton instance
_ws_client: MassiveWSClient | None = None


def get_ws_client() -> MassiveWSClient:
    global _ws_client
    if _ws_client is None:
        _ws_client = MassiveWSClient()
    return _ws_client
