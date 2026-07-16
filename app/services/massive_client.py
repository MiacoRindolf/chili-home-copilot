"""Massive.com REST + WebSocket client for stocks and crypto market data.

Provides cached, rate-limit-aware wrappers around the Massive REST API
(v2/v3 endpoints, Polygon-compatible format) and an optional WebSocket
client for real-time NBBO quote streaming.

Symbol conventions:
  - US stocks:  plain ticker like ``AAPL``, ``NVDA``, ``SPY``
  - Crypto:     ``X:BTCUSD``, ``X:ETHUSD`` (Polygon-compatible crypto prefix, no hyphen)
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator, Protocol, runtime_checkable

import requests

from ..config import settings
from .socket_budget import mount_bounded_http_adapters

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

_entitlement_denied: dict[str, float] = {}
_entitlement_log_throttle_until: dict[str, float] = {}
_entitlement_lock = threading.Lock()
_TTL_ENTITLEMENT_DENIED = 21600  # 6 hours: plan entitlement failures are stable

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
    "entitlement_blocks": 0,
    "ws_events": 0,
    "ws_malformed_events": 0,
    "ws_missing_event_clock": 0,
    "ws_missing_sequence": 0,
}


def get_metrics() -> dict[str, int]:
    with _metrics_lock:
        return dict(_metrics)


def _bump(key: str = "requests"):
    with _metrics_lock:
        _metrics[key] = _metrics.get(key, 0) + 1


def _request_cache_key(url: str, params: dict[str, Any] | None) -> str:
    safe_params = [
        (str(k), str(v))
        for k, v in sorted((params or {}).items())
        if str(k) != "apiKey"
    ]
    return json.dumps([url, safe_params], sort_keys=True, separators=(",", ":"))


def _looks_like_entitlement_denied(status_code: int, body: str) -> bool:
    if status_code != 403:
        return False
    text = (body or "").lower()
    return (
        "not_authorized" in text
        or "not entitled" in text
        or "upgrade your plan" in text
    )


def _entitlement_denied_active(key: str, url: str) -> bool:
    now = time.time()
    with _entitlement_lock:
        expires_at = _entitlement_denied.get(key)
        if expires_at is None:
            return False
        if expires_at <= now:
            _entitlement_denied.pop(key, None)
            _entitlement_log_throttle_until.pop(key, None)
            return False
        log_at = _entitlement_log_throttle_until.get(key, 0.0)
        if now >= log_at:
            logger.warning(
                "[massive] entitlement-denied cache active for %s (%ds remaining)",
                url,
                int(expires_at - now),
            )
            _entitlement_log_throttle_until[key] = now + 300
        return True


def _mark_entitlement_denied(key: str, url: str, body: str) -> None:
    now = time.time()
    with _entitlement_lock:
        _entitlement_denied[key] = now + _TTL_ENTITLEMENT_DENIED
        _entitlement_log_throttle_until[key] = now + 300
    logger.warning(
        "[massive] 403 entitlement denied for %s; suppressing matching calls for %ds: %s",
        url,
        _TTL_ENTITLEMENT_DENIED,
        (body or "")[:200],
    )


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


def invalidate_agg_cache_for_ticker(ticker: str) -> int:
    """Drop every cached aggregate-bars entry for ``ticker`` (all intervals/periods).

    Called on WS candle close so the next ``get_aggs`` read includes the
    just-closed bar instead of serving up to ``_TTL_BARS`` (1h) of staleness.
    """
    t = (ticker or "").strip().upper()
    if not t:
        return 0
    pref = f"massive:agg:{t}:"
    with _cache_lock:
        keys = [k for k in _cache if k.startswith(pref)]
        for k in keys:
            del _cache[k]
    return len(keys)


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
    pc = max(10, int(getattr(settings, "massive_http_pool_connections", 128)))
    pm = max(pc, int(getattr(settings, "massive_http_pool_maxsize", 512)))
    mount_bounded_http_adapters(
        sess,
        pool_connections=pc,
        pool_maxsize=pm,
        pool_block=True,
    )
    logger.info(
        "[massive] HTTP pool api.massive.com: pool_connections=%s pool_maxsize=%s pool_block=True "
        "(raise MASSIVE_HTTP_* if you still see urllib3 'pool is full')",
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


# ---------------------------------------------------------------------------
# Circuit breaker (incident 2026-04-19: residential IP added to Massive's
# edge denylist after a volume spike; CHILI's continued retries kept the
# abuse-trigger pattern hot, so any support-side unblock got re-tripped
# within hours).
#
# Closed   : normal operation, count consecutive connection-class failures.
# Open     : skip all Massive calls for `cooldown_sec`, return None.
# Half-open: cooldown elapsed; allow probes through. First success closes
#            the breaker; first failure re-opens it and resets cooldown.
#
# Only "connection-class" failures count (TCP refused, DNS, timeout).
# HTTP 4xx/5xx mean the upstream IS reachable; those don't trip the breaker.
# ---------------------------------------------------------------------------
_breaker_lock = threading.Lock()
_breaker_state = "closed"  # one of: closed, open, half_open
_breaker_consecutive_failures = 0
_breaker_opened_at = 0.0
_breaker_log_throttle_until = 0.0


def _breaker_threshold() -> int:
    return max(1, int(getattr(settings, "massive_breaker_failure_threshold", 5)))


def _breaker_cooldown_sec() -> int:
    return max(30, int(getattr(settings, "massive_breaker_cooldown_sec", 900)))


def _breaker_allow_request() -> bool:
    """Return True if the call should proceed, False if breaker is open."""
    global _breaker_state, _breaker_log_throttle_until
    with _breaker_lock:
        if _breaker_state in ("closed", "half_open"):
            return True
        # state == "open"
        now = time.time()
        cooldown = _breaker_cooldown_sec()
        elapsed = now - _breaker_opened_at
        if elapsed >= cooldown:
            _breaker_state = "half_open"
            logger.info(
                "[massive] circuit breaker entering half_open after %ds cooldown - probing",
                int(elapsed),
            )
            return True
        # Still cooling down. Throttle the "skipping" log to once/minute.
        if now >= _breaker_log_throttle_until:
            logger.warning(
                "[massive] circuit breaker OPEN - skipping request (%ds remaining)",
                int(cooldown - elapsed),
            )
            _breaker_log_throttle_until = now + 60
        return False


def _breaker_record_success() -> None:
    """Reachability succeeded (200 or 404). Closes the breaker."""
    global _breaker_state, _breaker_consecutive_failures
    with _breaker_lock:
        if _breaker_state in ("open", "half_open"):
            logger.info(
                "[massive] circuit breaker CLOSED - probe succeeded after %d failures, resuming traffic",
                _breaker_consecutive_failures,
            )
        _breaker_state = "closed"
        _breaker_consecutive_failures = 0


def _breaker_record_failure() -> None:
    """Connection-class failure. May trip or re-open the breaker."""
    global _breaker_state, _breaker_consecutive_failures, _breaker_opened_at
    with _breaker_lock:
        _breaker_consecutive_failures += 1
        if _breaker_state == "half_open":
            _breaker_state = "open"
            _breaker_opened_at = time.time()
            logger.warning(
                "[massive] circuit breaker re-OPEN - probe failed, cooldown %ds",
                _breaker_cooldown_sec(),
            )
            return
        if (
            _breaker_state == "closed"
            and _breaker_consecutive_failures >= _breaker_threshold()
        ):
            _breaker_state = "open"
            _breaker_opened_at = time.time()
            logger.error(
                "[massive] circuit breaker OPEN - %d consecutive connection failures, "
                "skipping calls for %ds (prevents abuse-trigger re-block)",
                _breaker_consecutive_failures,
                _breaker_cooldown_sec(),
            )


def get_breaker_status() -> dict[str, Any]:
    """Observability hook for status endpoints."""
    with _breaker_lock:
        now = time.time()
        cooldown = _breaker_cooldown_sec()
        return {
            "state": _breaker_state,
            "consecutive_failures": _breaker_consecutive_failures,
            "opened_at": _breaker_opened_at,
            "cooldown_remaining_sec": (
                max(0, int(cooldown - (now - _breaker_opened_at)))
                if _breaker_state == "open"
                else 0
            ),
            "threshold": _breaker_threshold(),
            "cooldown_sec": cooldown,
        }



def _get(url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """GET with retries, backoff, and rate-limit awareness."""
    api_key = _api_key()
    if not api_key:
        return None
    if not _breaker_allow_request():
        return None
    params = dict(params or {})
    entitlement_key = _request_cache_key(url, params)
    if _entitlement_denied_active(entitlement_key, url):
        _bump("entitlement_blocks")
        return None
    params["apiKey"] = api_key

    for attempt in range(_MAX_RETRIES + 1):
        try:
            _rate_limit_wait()
            _bump("requests")
            resp = _session.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                _breaker_record_success()
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
                _breaker_record_success()
                return _NOT_FOUND
            if _looks_like_entitlement_denied(resp.status_code, resp.text):
                _mark_entitlement_denied(entitlement_key, url, resp.text)
                _bump("entitlement_blocks")
                return None
            logger.warning(f"[massive] {resp.status_code} for {url}: {resp.text[:200]}")
            return None
        except requests.RequestException as e:
            _bump("errors")
            _breaker_record_failure()
            if not _breaker_allow_request():
                logger.warning(
                    "[massive] request failed; breaker opened after attempt %d/%d: %s",
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    e,
                )
                return None
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

    Polygon/Massive crypto format uses ``X:BTCUSD`` (no hyphen), while the
    app uses ``BTC-USD`` (yfinance style). Indices use the ``I:`` prefix on
    Massive (``I:VIX``, ``I:SPX``, ``I:DJI``); the app stores them in
    yfinance form (``^VIX``). Bare ``ZKUSD`` is also accepted. Stocks pass
    through unchanged.

    2026-04-28 leak fix: previously ``^VIX`` was passed straight to
    Massive's stocks endpoint, which silently returned 0 rows and broke
    the HMM regime classifier downstream.
    """
    t = ticker.upper().strip()
    if is_crypto(t):
        return f"X:{t.replace('-', '')}"
    if _bare_concat_crypto_usd(t):
        return f"X:{t}"
    if t.startswith("X:") and "-" in t:
        return t.replace("-", "")
    # Indices: yfinance ``^VIX`` -> Massive ``I:VIX``.
    if t.startswith("^") and len(t) > 1:
        return "I:" + t[1:]
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
    # Backward-compatible local receipt clock used by existing freshness checks.
    # Do not use it as market/event time.
    timestamp: float = 0.0
    provider_event_at: float | None = None
    received_at: float | None = None
    available_at: float | None = None
    provider_timestamp_ms: int | None = None
    sequence: int | None = None
    bid_exchange: int | None = None
    ask_exchange: int | None = None
    condition: int | None = None
    indicators: tuple[int, ...] = ()
    tape: int | None = None
    bridge_run_id: str | None = None
    connection_generation: int | None = None


@dataclass
class TradeSnapshot:
    price: float
    size: int = 0
    timestamp: float = 0.0
    provider_event_at: float | None = None
    received_at: float | None = None
    available_at: float | None = None
    provider_timestamp_ms: int | None = None
    participant_timestamp_ms: int | None = None
    trf_timestamp_ms: int | None = None
    sequence: int | None = None
    exchange: int | None = None
    trade_id: str | None = None
    tape: int | None = None
    conditions: tuple[int, ...] = ()
    trf_id: int | None = None
    fractional_size: str | None = None
    bridge_run_id: str | None = None
    connection_generation: int | None = None


@runtime_checkable
class MassiveWSCaptureSink(Protocol):
    """Bounded exact-frame handoff called before operational fan-out.

    Implementations must return after an in-memory admission attempt.  Slow
    serialization and durable capture work belongs on their own bounded worker;
    an admission overflow must be latched as an explicit coverage gap.
    """

    def on_massive_ws_subscription(self, evidence: dict[str, Any]) -> None: ...

    def on_massive_ws_frame(
        self, symbol: str, snapshot: QuoteSnapshot | TradeSnapshot
    ) -> bool: ...

    def on_massive_ws_gap(
        self,
        *,
        reason: str,
        symbol: str | None,
        received_at: float,
        lost_count: int,
    ) -> None: ...


_ws_cache: dict[str, QuoteSnapshot] = {}
_ws_cache_lock = threading.Lock()
_WS_STALENESS = 5.0  # seconds before a WS quote is considered stale
_WS_CLOCK_FUTURE_TOLERANCE = 1.0
_MASSIVE_WS_RUN_ID = str(uuid.uuid4())


def _massive_unix_ms(value: Any) -> tuple[int, float] | None:
    """Return exact Massive Unix-ms and seconds without wall-clock fallback."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        milliseconds = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if milliseconds <= 0:
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    if isinstance(value, str) and value.strip() not in {
        str(milliseconds),
        f"{milliseconds}.0",
    }:
        return None
    return milliseconds, milliseconds / 1000.0


def _massive_sequence(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        sequence = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    if isinstance(value, str) and value.strip() not in {
        str(sequence),
        f"{sequence}.0",
    }:
        return None
    return sequence if sequence >= 0 else None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _integer_tuple(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[int] = []
    for item in value:
        parsed = _optional_int(item)
        if parsed is not None:
            out.append(parsed)
    return tuple(out)

# Tick listener registry — callbacks receive (symbol, QuoteSnapshot|TradeSnapshot)
from typing import Callable
TickCallback = Callable[[str, QuoteSnapshot | TradeSnapshot], None]
_tick_listeners: dict[str, list[TickCallback]] = {}
_tick_listeners_lock = threading.Lock()


def register_tick_listener(ticker: str, callback: TickCallback) -> None:
    """Register *callback* to receive every tick for *ticker*.

    Also SUBSCRIBES the symbol on the live WS connection — a listener without
    a subscription never fires (2026-06-12 IPO morning: newly armed equities
    sat behind stale_bbo forever because only boot-time symbols were
    subscribed; RYAM's freshest quote was 43 minutes old while Ross traded
    the same tape live)."""
    sym = ticker.upper()
    with _tick_listeners_lock:
        _tick_listeners.setdefault(sym, []).append(callback)
    try:
        global _ws_client
        if _ws_client is not None and _ws_client._thread is not None:
            _ws_client.subscribe([sym])
    except Exception:
        logger.debug("[massive-ws] subscribe-on-listen failed for %s", sym, exc_info=True)


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


# ── Candle aggregation (trade ticks → OHLCV bars) ────────────────────────────


@dataclass
class OHLCVBar:
    """A single time-bucketed OHLCV bar assembled from trade ticks."""
    ticker: str
    interval_seconds: int
    bucket_start: float  # unix timestamp (start of interval)
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int
    closed: bool = False


CandleCallback = Callable[[str, OHLCVBar], None]


class CandleAggregator:
    """Accumulates TradeSnapshot ticks into time-bucketed OHLCV bars."""

    def __init__(self, interval_seconds: int = 60):
        self._interval = interval_seconds
        self._bars: dict[str, OHLCVBar] = {}  # ticker -> current open bar
        self._last_sequence: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()
        self._listeners: dict[str, list[CandleCallback]] = {}
        self._listeners_lock = threading.Lock()

    @property
    def interval(self) -> int:
        return self._interval

    def on_trade(self, sym: str, snap: TradeSnapshot) -> None:
        """Process a trade tick — bucket into bars and emit on close."""
        event_at = (
            snap.provider_event_at
            if snap.provider_event_at is not None
            else snap.timestamp
        )
        bucket_start = (event_at // self._interval) * self._interval
        with self._lock:
            if snap.sequence is not None:
                event_day = int(event_at // 86_400)
                prior = self._last_sequence.get(sym)
                if prior is not None and prior[0] == event_day and snap.sequence <= prior[1]:
                    return
                self._last_sequence[sym] = (event_day, snap.sequence)
            bar = self._bars.get(sym)
            # Raw capture retains late releases, but a live derived bar must not
            # reopen a bucket the FSM has already observed as closed.
            if bar is not None and bucket_start < bar.bucket_start:
                return
            if bar is None or bar.bucket_start != bucket_start:
                # Close previous bar if exists
                if bar is not None:
                    bar.closed = True
                    self._emit(sym, bar)
                # Open new bar
                self._bars[sym] = OHLCVBar(
                    ticker=sym,
                    interval_seconds=self._interval,
                    bucket_start=bucket_start,
                    open=snap.price,
                    high=snap.price,
                    low=snap.price,
                    close=snap.price,
                    volume=float(snap.size),
                    trade_count=1,
                )
            else:
                bar.high = max(bar.high, snap.price)
                bar.low = min(bar.low, snap.price)
                bar.close = snap.price
                bar.volume += float(snap.size)
                bar.trade_count += 1

    def register_candle_listener(self, ticker: str, cb: CandleCallback) -> None:
        sym = ticker.upper()
        with self._listeners_lock:
            self._listeners.setdefault(sym, []).append(cb)

    def unregister_candle_listener(self, ticker: str, cb: CandleCallback) -> None:
        sym = ticker.upper()
        with self._listeners_lock:
            cbs = self._listeners.get(sym)
            if cbs:
                try:
                    cbs.remove(cb)
                except ValueError:
                    pass
                if not cbs:
                    del self._listeners[sym]

    def _emit(self, sym: str, bar: OHLCVBar) -> None:
        with self._listeners_lock:
            cbs = list(self._listeners.get(sym, []))
        for cb in cbs:
            try:
                cb(sym, bar)
            except Exception:
                pass
        # Invalidate BOTH bar-cache layers so the next trigger evaluation includes
        # the just-closed minute. Without the massive-layer drop, _TTL_BARS (1h)
        # left "live" triggers reading bars up to an hour old — fatal for a 1m
        # entry timeframe.
        try:
            from .trading.market_data import invalidate_ohlcv_cache_for_ticker

            invalidate_ohlcv_cache_for_ticker(sym)
        except Exception:
            pass
        try:
            invalidate_agg_cache_for_ticker(sym)
        except Exception:
            pass


# Module-level candle aggregator registry (interval_seconds -> aggregator)
_candle_aggregators: dict[int, CandleAggregator] = {}
_candle_agg_lock = threading.Lock()


def get_candle_aggregator(interval_seconds: int = 60) -> CandleAggregator:
    """Get or create a CandleAggregator for the given interval."""
    with _candle_agg_lock:
        if interval_seconds not in _candle_aggregators:
            _candle_aggregators[interval_seconds] = CandleAggregator(interval_seconds)
        return _candle_aggregators[interval_seconds]


def register_candle_listener(ticker: str, interval_seconds: int, cb: CandleCallback) -> None:
    get_candle_aggregator(interval_seconds).register_candle_listener(ticker, cb)


def unregister_candle_listener(ticker: str, interval_seconds: int, cb: CandleCallback) -> None:
    agg = _candle_aggregators.get(interval_seconds)
    if agg:
        agg.unregister_candle_listener(ticker, cb)


def get_ws_quote(ticker: str) -> QuoteSnapshot | None:
    """Return a fresh WebSocket-cached quote, or None if stale/missing."""
    with _ws_cache_lock:
        snap = _ws_cache.get(ticker.upper())
    if snap is None:
        return None
    now = time.time()
    received_at = snap.received_at if snap.received_at is not None else snap.timestamp
    received_age = now - received_at
    if received_age < -_WS_CLOCK_FUTURE_TOLERANCE or received_age > _WS_STALENESS:
        return None
    if snap.provider_event_at is not None:
        provider_age = now - snap.provider_event_at
        if provider_age < -_WS_CLOCK_FUTURE_TOLERANCE or provider_age > _WS_STALENESS:
            return None
    if snap.available_at is not None:
        available_age = now - snap.available_at
        if available_age < -_WS_CLOCK_FUTURE_TOLERANCE or available_age > _WS_STALENESS:
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
        # Exact provider channels requested per symbol.  A symbol-only set is
        # insufficient here: reconnecting a Q-only certifying producer as Q+T
        # would let uncaptured trades reach listeners and candle builders.
        self._subscriptions: dict[str, set[str]] = {}
        self._connection_generation = 0
        self._authenticated_generation: int | None = None
        self._capture_sinks: list[MassiveWSCaptureSink] = []
        self._capture_sink_rosters: dict[
            int, tuple[frozenset[str], frozenset[str], int]
        ] = {}
        # A capture guard outlives detach.  The process owner must explicitly
        # release it after the consuming FSM has stopped; otherwise provider
        # frames could slip through between producer close and RUN_CLOSED.
        self._capture_guard_symbols: set[str] = set()
        self._capture_sinks_lock = threading.RLock()
        self._capture_sinks_condition = threading.Condition(
            self._capture_sinks_lock
        )
        self._capture_callback_gates: dict[int, threading.Lock] = {}
        # Copy-on-write parser views.  The websocket parser never waits on the
        # control-plane lock held by attach/detach/subscription work.
        self._capture_route_snapshot: tuple[
            tuple[
                MassiveWSCaptureSink,
                frozenset[str],
                frozenset[str],
                int,
                threading.Lock,
            ],
            ...,
        ] = ()
        self._capture_guard_snapshot: frozenset[str] = frozenset()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def capture_source_identity(self) -> dict[str, Any]:
        """Exact source identity a RUN_OPEN producer spec must declare."""

        return {
            "provider": "massive_ws",
            "instance_id": _MASSIVE_WS_RUN_ID,
            "connection_generation": self._connection_generation,
            "authenticated": bool(
                self._authenticated_generation == self._connection_generation
                and self._connection_generation > 0
            ),
        }

    def attach_capture_sink(self, sink: "MassiveWSCaptureSink") -> None:
        """Reject roster-less capture bindings.

        A sink that does not declare its exact Q/T ownership cannot protect the
        operational fan-out.  Call ``attach_capture_sink_for_symbols`` instead.
        """

        del sink
        raise RuntimeError(
            "Massive capture sinks require an exact symbol/channel roster"
        )

    @staticmethod
    def _normalize_capture_roster(
        tickers: list[str], channels: tuple[str, ...]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        symbols = tuple(
            sorted(
                {
                    str(ticker).strip().upper()
                    for ticker in tickers
                    if str(ticker).strip()
                }
            )
        )
        if not symbols:
            raise ValueError("capture subscription symbols are empty")
        normalized_channels = tuple(
            sorted({str(channel).strip().upper() for channel in channels})
        )
        if not normalized_channels or any(
            channel not in {"Q", "T"} for channel in normalized_channels
        ):
            raise ValueError("capture subscription channels are invalid")
        return symbols, normalized_channels

    def _owned_capture_channels_locked(
        self, symbol: str, *, generation: int | None = None
    ) -> set[str]:
        owned: set[str] = set()
        for symbols, channels, bound_generation in self._capture_sink_rosters.values():
            if symbol not in symbols:
                continue
            if generation is not None and bound_generation != generation:
                continue
            owned.update(channels)
        return owned

    def _refresh_capture_snapshots_locked(self) -> None:
        self._capture_route_snapshot = tuple(
            (
                sink,
                self._capture_sink_rosters[id(sink)][0],
                self._capture_sink_rosters[id(sink)][1],
                self._capture_sink_rosters[id(sink)][2],
                self._capture_callback_gates[id(sink)],
            )
            for sink in self._capture_sinks
        )
        self._capture_guard_snapshot = frozenset(self._capture_guard_symbols)

    def attach_capture_sink_for_symbols(
        self,
        sink: "MassiveWSCaptureSink",
        tickers: list[str],
        *,
        channels: tuple[str, ...],
    ) -> dict[str, Any]:
        """Atomically install a sink and hand it the exact sent request."""

        if not isinstance(sink, MassiveWSCaptureSink):
            raise TypeError("Massive capture sink does not implement the contract")
        symbols, normalized_channels = self._normalize_capture_roster(
            tickers, channels
        )
        with self._capture_sinks_lock:
            if sink in self._capture_sinks:
                raise RuntimeError("Massive capture sink is already attached")
            if (
                self._connection_generation <= 0
                or self._authenticated_generation != self._connection_generation
            ):
                raise RuntimeError(
                    "Massive capture sink requires an authenticated connection generation"
                )
            for symbol in symbols:
                owned_before = self._owned_capture_channels_locked(
                    symbol, generation=self._connection_generation
                )
                overlap = owned_before.intersection(normalized_channels)
                if overlap:
                    raise RuntimeError(
                        "Massive capture channel already has a certifying owner: "
                        + ",".join(
                            f"{channel}.{symbol}" for channel in sorted(overlap)
                        )
                    )
                existing = set(self._subscriptions.get(symbol, set()))
                uncovered = existing - owned_before - set(normalized_channels)
                if uncovered:
                    raise RuntimeError(
                        "active Massive subscription contains uncaptured channels: "
                        + ",".join(
                            f"{channel}.{symbol}" for channel in sorted(uncovered)
                        )
                    )
            self._capture_guard_symbols.update(symbols)
            self._capture_sinks.append(sink)
            self._capture_sink_rosters[id(sink)] = (
                frozenset(symbols),
                frozenset(normalized_channels),
                self._connection_generation,
            )
            self._capture_callback_gates[id(sink)] = threading.Lock()
            self._refresh_capture_snapshots_locked()
            try:
                evidence = self.subscribe_for_capture(
                    list(symbols), channels=normalized_channels
                )
                sink.on_massive_ws_subscription(evidence)
            except BaseException:
                self._capture_sinks.remove(sink)
                self._capture_sink_rosters.pop(id(sink), None)
                self._capture_callback_gates.pop(id(sink), None)
                self._refresh_capture_snapshots_locked()
                raise
            return evidence

    def detach_capture_sink(self, sink: "MassiveWSCaptureSink") -> None:
        # Atomically remove the parser route, then wait behind the one callback
        # gate.  Parser callbacks acquire this gate nonblocking, so control
        # plane shutdown can wait without ever stalling market-data parsing.
        with self._capture_sinks_lock:
            gate = self._capture_callback_gates.get(id(sink))
            try:
                self._capture_sinks.remove(sink)
            except ValueError:
                pass
            self._capture_sink_rosters.pop(id(sink), None)
            self._refresh_capture_snapshots_locked()
        if gate is not None:
            gate.acquire()
            gate.release()
            with self._capture_sinks_lock:
                self._capture_callback_gates.pop(id(sink), None)

    def release_capture_guard(self, tickers: list[str]) -> None:
        """Release a fail-closed symbol guard after its consuming FSM stopped."""

        symbols = {
            str(ticker).strip().upper()
            for ticker in tickers
            if str(ticker).strip()
        }
        with self._capture_sinks_condition:
            for symbol in symbols:
                if self._owned_capture_channels_locked(symbol):
                    raise RuntimeError(
                        f"cannot release Massive capture guard with active owner: {symbol}"
                    )
            self._capture_guard_symbols.difference_update(symbols)
            self._refresh_capture_snapshots_locked()

    def _reserve_capture_callbacks(
        self, *, symbol: str | None = None, channel: str | None = None
    ) -> tuple[tuple["MassiveWSCaptureSink", threading.Lock], ...]:
        routes = self._capture_route_snapshot
        return tuple(
            (sink, gate)
            for sink, symbols, channels, _generation, gate in routes
            if (
                symbol is None
                or (
                    symbol in symbols
                    and (channel is None or channel in channels)
                )
            )
        )

    def _publish_capture_frame(
        self, symbol: str, snapshot: QuoteSnapshot | TradeSnapshot
    ) -> bool:
        channel = "Q" if isinstance(snapshot, QuoteSnapshot) else "T"
        guarded = symbol in self._capture_guard_snapshot
        if not guarded:
            return True
        sinks = self._reserve_capture_callbacks(symbol=symbol, channel=channel)
        if not sinks:
            self._publish_capture_gap(
                reason=f"massive_ws_unowned_{channel.lower()}_frame",
                symbol=symbol,
                received_at=(
                    snapshot.received_at
                    if snapshot.received_at is not None
                    else time.time()
                ),
            )
            return False
        admitted = True
        for sink, gate in sinks:
            if not gate.acquire(blocking=False):
                admitted = False
                continue
            try:
                if sink.on_massive_ws_frame(symbol, snapshot) is not True:
                    admitted = False
            except BaseException as exc:
                admitted = False
                logger.error(
                    "[massive-ws] certifying capture sink rejected frame symbol=%s: %s",
                    symbol,
                    exc,
                    exc_info=True,
                )
                try:
                    sink.on_massive_ws_gap(
                        reason="massive_ws_capture_sink_rejected",
                        symbol=symbol,
                        received_at=(
                            snapshot.received_at
                            if snapshot.received_at is not None
                            else time.time()
                        ),
                        lost_count=1,
                    )
                except BaseException:
                    logger.critical(
                        "[massive-ws] capture sink could not persist its rejection gap",
                        exc_info=True,
                    )
            finally:
                gate.release()
        return admitted

    def _publish_capture_gap(
        self,
        *,
        reason: str,
        symbol: str | None,
        received_at: float,
        lost_count: int = 1,
    ) -> None:
        sinks = self._reserve_capture_callbacks(symbol=symbol)
        for sink, gate in sinks:
            if not gate.acquire(blocking=False):
                logger.critical(
                    "[massive-ws] capture callback gate busy while reporting gap=%s",
                    reason,
                )
                continue
            try:
                sink.on_massive_ws_gap(
                    reason=reason,
                    symbol=symbol,
                    received_at=received_at,
                    lost_count=lost_count,
                )
            except BaseException:
                logger.critical(
                    "[massive-ws] capture sink could not persist provider gap=%s",
                    reason,
                    exc_info=True,
                )
            finally:
                gate.release()

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
        with self._capture_sinks_lock:
            self._subscriptions = {
                str(ticker).strip().upper(): {"Q", "T"}
                for ticker in (tickers or [])
                if str(ticker).strip()
            }
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
        self._authenticated_generation = None
        logger.info("[massive-ws] WebSocket client stopped")

    def subscribe(self, tickers: list[str]):
        symbols = tuple(
            sorted(
                {
                    str(ticker).strip().upper()
                    for ticker in tickers
                    if str(ticker).strip()
                }
            )
        )
        if not symbols:
            return
        additions: list[tuple[str, str]] = []
        rejected: list[tuple[str, str]] = []
        with self._capture_sinks_lock:
            for symbol in symbols:
                requested = {"Q", "T"}
                if symbol in self._capture_guard_symbols:
                    owned = self._owned_capture_channels_locked(
                        symbol, generation=self._connection_generation
                    )
                    rejected.extend(
                        (symbol, channel) for channel in sorted(requested - owned)
                    )
                    requested.intersection_update(owned)
                current = self._subscriptions.setdefault(symbol, set())
                additions.extend(
                    (symbol, channel)
                    for channel in sorted(requested - current)
                )
                current.update(requested)
            socket = self._ws
        for symbol, channel in rejected:
            self._publish_capture_gap(
                reason=f"massive_ws_unowned_{channel.lower()}_subscription",
                symbol=symbol,
                received_at=time.time(),
            )
        if not additions or socket is None:
            return
        params = ",".join(
            f"{channel}.{symbol}" for symbol, channel in additions
        )
        try:
            sub_msg = json.dumps({"action": "subscribe", "params": params})
            socket.send(sub_msg)
        except Exception as e:
            logger.warning(f"[massive-ws] subscribe error: {e}")
            for symbol, _channel in additions:
                with self._capture_sinks_lock:
                    guarded = symbol in self._capture_guard_symbols
                if guarded:
                    self._publish_capture_gap(
                        reason="massive_ws_subscription_send_failed",
                        symbol=symbol,
                        received_at=time.time(),
                    )

    def subscribe_for_capture(
        self, tickers: list[str], *, channels: tuple[str, ...]
    ) -> dict[str, Any]:
        """Send an exact subscription request or fail without claiming ACK.

        Massive does not provide a durable per-symbol subscription receipt on
        this socket.  The certifying producer therefore uses the first exact
        Q/T frame as the acknowledgement, but it still needs proof that the
        request was sent on the declared authenticated generation.
        """

        symbols, normalized_channels = self._normalize_capture_roster(
            tickers, channels
        )
        with self._capture_sinks_lock:
            if (
                self._ws is None
                or self._connection_generation <= 0
                or self._authenticated_generation != self._connection_generation
            ):
                raise RuntimeError(
                    "capture subscription requires an authenticated Massive socket"
                )
            for symbol in symbols:
                owned = self._owned_capture_channels_locked(
                    symbol, generation=self._connection_generation
                )
                if not set(normalized_channels).issubset(owned):
                    raise RuntimeError(
                        "capture subscription exceeds the bound producer roster"
                    )
            params = ",".join(
                f"{channel}.{symbol}"
                for symbol in symbols
                for channel in normalized_channels
            )
            request = {"action": "subscribe", "params": params}
            socket = self._ws
            socket.send(
                json.dumps(request, separators=(",", ":"), sort_keys=True)
            )
            for symbol in symbols:
                self._subscriptions.setdefault(symbol, set()).update(
                    normalized_channels
                )
        return {
            "provider": "massive_ws",
            "instance_id": _MASSIVE_WS_RUN_ID,
            "connection_generation": self._connection_generation,
            "symbols": list(symbols),
            "channels": list(normalized_channels),
            "request": request,
            "acknowledgement": "first_exact_provider_frame_required",
        }

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
                self._connection_generation += 1
                self._authenticate()
                self._subscribe_all()

                while not self._stop_event.is_set():
                    try:
                        raw = self._ws.recv()
                    except Exception:
                        break
                    self._handle_messages(raw, received_at=time.time())

            except Exception as e:
                logger.warning(f"[massive-ws] connection error: {e}")
            finally:
                if self._connection_generation > 0:
                    self._publish_capture_gap(
                        reason="massive_ws_connection_closed",
                        symbol=None,
                        received_at=time.time(),
                    )
                self._authenticated_generation = None
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
        try:
            rows = json.loads(resp)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Massive authentication response was malformed") from exc
        if not isinstance(rows, list):
            rows = [rows]
        authenticated = any(
            isinstance(row, dict)
            and str(row.get("status") or "").strip().lower() == "auth_success"
            for row in rows
        )
        if not authenticated:
            raise RuntimeError("Massive authentication was not acknowledged")
        self._authenticated_generation = self._connection_generation
        logger.debug("[massive-ws] authentication acknowledged")

    def _subscribe_all(self):
        with self._capture_sinks_lock:
            pairs: list[tuple[str, str]] = []
            for symbol in sorted(self._subscriptions):
                channels = set(self._subscriptions[symbol])
                if symbol in self._capture_guard_symbols:
                    channels.intersection_update(
                        self._owned_capture_channels_locked(
                            symbol, generation=self._connection_generation
                        )
                    )
                pairs.extend(
                    (symbol, channel) for channel in sorted(channels)
                )
            socket = self._ws
        if not pairs or socket is None:
            return
        params = ",".join(
            f"{channel}.{symbol}" for symbol, channel in pairs
        )
        socket.send(json.dumps({"action": "subscribe", "params": params}))

    def _handle_messages(self, raw: str, *, received_at: float | None = None):
        frame_received_at = time.time() if received_at is None else float(received_at)
        try:
            msgs = json.loads(raw)
        except json.JSONDecodeError:
            _bump("ws_malformed_events")
            self._publish_capture_gap(
                reason="massive_ws_malformed_frame",
                symbol=None,
                received_at=frame_received_at,
            )
            return
        if not isinstance(msgs, list):
            msgs = [msgs]
        for msg in msgs:
            if not isinstance(msg, dict):
                _bump("ws_malformed_events")
                self._publish_capture_gap(
                    reason="massive_ws_malformed_event",
                    symbol=None,
                    received_at=frame_received_at,
                )
                continue
            ev = msg.get("ev")
            raw_symbol = msg.get("sym")
            sym = (
                raw_symbol.strip().upper()
                if isinstance(raw_symbol, str)
                else ""
            )
            if not sym:
                if ev in {"Q", "T"}:
                    _bump("ws_malformed_events")
                    self._publish_capture_gap(
                        reason="massive_ws_missing_symbol",
                        symbol=None,
                        received_at=frame_received_at,
                    )
                continue

            if ev not in {"Q", "T"}:
                continue
            _bump("ws_events")
            provider_clock = _massive_unix_ms(msg.get("t"))
            if provider_clock is None:
                _bump("ws_missing_event_clock")
                self._publish_capture_gap(
                    reason="massive_ws_missing_event_clock",
                    symbol=sym,
                    received_at=frame_received_at,
                )
                continue
            provider_timestamp_ms, provider_event_at = provider_clock
            sequence = _massive_sequence(msg.get("q"))
            if sequence is None:
                _bump("ws_missing_sequence")
                self._publish_capture_gap(
                    reason="massive_ws_missing_sequence",
                    symbol=sym,
                    received_at=frame_received_at,
                )
                continue

            # Stamp release immediately before publishing to the shared
            # cache/listener graph; never substitute it for provider event time.
            available_at = time.time()

            if ev == "Q":
                snap = QuoteSnapshot(
                    price=(msg.get("bp", 0) + msg.get("ap", 0)) / 2 if msg.get("bp") and msg.get("ap") else msg.get("bp") or msg.get("ap") or 0,
                    bid=msg.get("bp"),
                    ask=msg.get("ap"),
                    bid_size=msg.get("bs"),
                    ask_size=msg.get("as"),
                    timestamp=frame_received_at,
                    provider_event_at=provider_event_at,
                    received_at=frame_received_at,
                    available_at=available_at,
                    provider_timestamp_ms=provider_timestamp_ms,
                    sequence=sequence,
                    bid_exchange=_optional_int(msg.get("bx")),
                    ask_exchange=_optional_int(msg.get("ax")),
                    condition=_optional_int(msg.get("c")),
                    indicators=_integer_tuple(msg.get("i")),
                    tape=_optional_int(msg.get("z")),
                    bridge_run_id=_MASSIVE_WS_RUN_ID,
                    connection_generation=self._connection_generation,
                )
                # Capture observes the parsed provider frame before cache or
                # strategy listeners.  Its synchronous endpoint gives the FSM
                # no path to consume this frame before the append-only ingress
                # has accepted it (or latched a coverage gap).
                if not self._publish_capture_frame(sym, snap):
                    continue
                with _ws_cache_lock:
                    _ws_cache[sym] = snap
                _fire_tick_listeners(sym, snap)

            elif ev == "T":
                participant_clock = _massive_unix_ms(msg.get("pt"))
                trf_clock = _massive_unix_ms(msg.get("trft"))
                trade = TradeSnapshot(
                    price=float(msg.get("p", 0)),
                    size=int(msg.get("s", 0)),
                    timestamp=frame_received_at,
                    provider_event_at=provider_event_at,
                    received_at=frame_received_at,
                    available_at=available_at,
                    provider_timestamp_ms=provider_timestamp_ms,
                    participant_timestamp_ms=(
                        participant_clock[0] if participant_clock is not None else None
                    ),
                    trf_timestamp_ms=(trf_clock[0] if trf_clock is not None else None),
                    sequence=sequence,
                    exchange=_optional_int(msg.get("x")),
                    trade_id=(str(msg.get("i")) if msg.get("i") is not None else None),
                    tape=_optional_int(msg.get("z")),
                    conditions=_integer_tuple(msg.get("c")),
                    trf_id=_optional_int(msg.get("trfi")),
                    fractional_size=(
                        str(msg.get("ds")) if msg.get("ds") is not None else None
                    ),
                    bridge_run_id=_MASSIVE_WS_RUN_ID,
                    connection_generation=self._connection_generation,
                )
                if not self._publish_capture_frame(sym, trade):
                    continue
                _fire_tick_listeners(sym, trade)
                # Feed trade ticks into candle aggregators
                with _candle_agg_lock:
                    aggs = list(_candle_aggregators.values())
                for agg in aggs:
                    try:
                        agg.on_trade(sym, trade)
                    except Exception:
                        pass


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


class MassiveFullSnapshotCaptureError(RuntimeError):
    """A capture-required full-snapshot read could not be durably receipted."""


@runtime_checkable
class MassiveFullSnapshotCaptureSink(Protocol):
    """Capture-only observer for the exact result already read by this client."""

    def on_massive_full_snapshot(
        self,
        *,
        include_otc: bool,
        max_age_seconds: float | None,
        provider_cache_ttl_seconds: float,
        requested_at: datetime,
        returned_at: datetime,
        cache_hit: bool,
        cache_age_seconds: float | None,
        rows: list[dict[str, Any]],
    ) -> bool: ...


_MASSIVE_FULL_SNAPSHOT_CAPTURE_SINK: contextvars.ContextVar[
    MassiveFullSnapshotCaptureSink | None
] = contextvars.ContextVar(
    "_chili_massive_full_snapshot_capture_sink",
    default=None,
)


@contextlib.contextmanager
def massive_full_snapshot_capture_sink(
    sink: MassiveFullSnapshotCaptureSink | None,
) -> Iterator[None]:
    """Install a process-local capture sink without changing provider routing."""

    if sink is not None and not isinstance(sink, MassiveFullSnapshotCaptureSink):
        raise TypeError("Massive full-snapshot capture sink is malformed")
    token = _MASSIVE_FULL_SNAPSHOT_CAPTURE_SINK.set(sink)
    try:
        yield
    finally:
        _MASSIVE_FULL_SNAPSHOT_CAPTURE_SINK.reset(token)


def _full_snapshot_effective_ttl(max_age_seconds: float | None) -> float:
    ttl = float(_TTL_FULL_SNAPSHOT)
    if max_age_seconds is not None:
        try:
            ttl = min(
                float(_TTL_FULL_SNAPSHOT),
                max(60.0, float(max_age_seconds)),
            )
        except (TypeError, ValueError):
            ttl = float(_TTL_FULL_SNAPSHOT)
    return ttl


def _return_captured_full_snapshot(
    rows: list[dict[str, Any]],
    *,
    sink: MassiveFullSnapshotCaptureSink | None,
    include_otc: bool,
    max_age_seconds: float | None,
    provider_cache_ttl_seconds: float,
    requested_at: datetime | None,
    cache_hit: bool,
    cache_age_seconds: float | None,
) -> list[dict[str, Any]]:
    if sink is None:
        return rows
    assert requested_at is not None
    returned_at = datetime.now(timezone.utc)
    try:
        accepted = sink.on_massive_full_snapshot(
            include_otc=include_otc,
            max_age_seconds=max_age_seconds,
            provider_cache_ttl_seconds=provider_cache_ttl_seconds,
            requested_at=requested_at,
            returned_at=returned_at,
            cache_hit=cache_hit,
            cache_age_seconds=cache_age_seconds,
            rows=rows,
        )
    except Exception as exc:
        raise MassiveFullSnapshotCaptureError(
            "Massive full-snapshot capture sink failed"
        ) from exc
    if accepted is not True:
        raise MassiveFullSnapshotCaptureError(
            "Massive full-snapshot capture sink did not durably accept the read"
        )
    return rows

def get_full_market_snapshot(
    *, include_otc: bool = False, max_age_seconds: float | None = None
) -> list[dict[str, Any]]:
    """Fetch the entire US stock market snapshot (~10K tickers) in one call.

    Returns a list of raw ticker snapshot dicts as returned by Massive.
    Cached for 30 minutes by default so all prescreener filters share one API
    call. ``max_age_seconds`` lets a freshness-sensitive caller (the momentum
    universe builder) force a fresher pull: it tightens the effective TTL to
    ``min(30min, max(60s, max_age_seconds))`` so a name that *started moving in
    the last few minutes* shows up while a clean first-pullback entry still
    exists — the screener was otherwise seeing igniters up to 30 min late.
    Other callers (default) keep the 30-min cache; the fresher pull this caller
    triggers simply benefits them too (one shared snapshot).
    """
    global _snapshot_cache
    sink = _MASSIVE_FULL_SNAPSHOT_CAPTURE_SINK.get()
    requested_at = datetime.now(timezone.utc) if sink is not None else None
    ttl = _full_snapshot_effective_ttl(max_age_seconds)
    if _snapshot_cache is not None:
        ts, data = _snapshot_cache
        age = time.time() - ts
        if 0.0 <= age < ttl:
            return _return_captured_full_snapshot(
                data,
                sink=sink,
                include_otc=include_otc,
                max_age_seconds=max_age_seconds,
                provider_cache_ttl_seconds=ttl,
                requested_at=requested_at,
                cache_hit=True,
                cache_age_seconds=age,
            )

    with _snapshot_lock:
        if _snapshot_cache is not None:
            ts, data = _snapshot_cache
            age = time.time() - ts
            if 0.0 <= age < ttl:
                return _return_captured_full_snapshot(
                    data,
                    sink=sink,
                    include_otc=include_otc,
                    max_age_seconds=max_age_seconds,
                    provider_cache_ttl_seconds=ttl,
                    requested_at=requested_at,
                    cache_hit=True,
                    cache_age_seconds=age,
                )

        url = f"{_base()}/v2/snapshot/locale/us/markets/stocks/tickers"
        params: dict[str, Any] = {}
        if include_otc:
            params["include_otc"] = "true"
        resp = _get(url, params)
        if resp is _NOT_FOUND or not resp or not resp.get("tickers"):
            logger.warning("[massive] Full market snapshot returned no tickers")
            return _return_captured_full_snapshot(
                [],
                sink=sink,
                include_otc=include_otc,
                max_age_seconds=max_age_seconds,
                provider_cache_ttl_seconds=ttl,
                requested_at=requested_at,
                cache_hit=False,
                cache_age_seconds=None,
            )

        tickers = resp["tickers"]
        logger.info("[massive] Full market snapshot: %d tickers", len(tickers))
        _snapshot_cache = (time.time(), tickers)
        return _return_captured_full_snapshot(
            tickers,
            sink=sink,
            include_otc=include_otc,
            max_age_seconds=max_age_seconds,
            provider_cache_ttl_seconds=ttl,
            requested_at=requested_at,
            cache_hit=False,
            cache_age_seconds=None,
        )


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


def get_recent_news_tickers(*, limit: int = 200, max_age_min: int = 120) -> list[str]:
    """Tickers with a FRESH general news headline (Polygon ``/v2/reference/news``).

    The ignition for Ross-style sympathy/theme momentum is a fresh, strong NEWS
    headline on a (usually low-float) mover — not just scheduled earnings. Returns
    the de-duped tickers whose latest news is within ``max_age_min`` minutes, because
    FRESHNESS is the edge (stale news is not a catalyst). Each result carries a
    ``tickers`` list + ``published_utc``. Gracefully returns ``[]`` when the data plan
    lacks news access, so the catalyst tilt stays a no-op. docs/DESIGN/MOMENTUM_LANE.md
    """
    cache_key = f"massive:recent_news:{int(max_age_min)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"{_base()}/v2/reference/news"
    data = _get(url, {"limit": str(limit), "order": "desc", "sort": "published_utc"})
    if data is _NOT_FOUND or not data:
        return []
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, int(max_age_min)))
    out: list[str] = []
    seen: set[str] = set()
    for item in data.get("results", []):
        ts = item.get("published_utc")
        if ts:
            try:
                pub = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if pub < cutoff:
                    continue  # stale headline — not a fresh catalyst
            except (TypeError, ValueError):
                pass  # unparseable timestamp — keep (fail-open on freshness)
        for t in item.get("tickers") or []:
            tt = str(t or "").upper().strip()
            if tt and tt not in seen:
                seen.add(tt)
                out.append(tt)
    _cache_set(cache_key, out)
    return out


def get_recent_news_items(*, limit: int = 200, max_age_min: int = 120) -> list[tuple[str, str]]:
    """Like :func:`get_recent_news_tickers` but KEEPS the headline title — ``(ticker,
    title)`` pairs for each fresh Polygon news result (within ``max_age_min``). The title
    is what lets the catalyst tilt GRADE the catalyst TYPE (trial/M&A = strong, compliance/
    vague = weak) instead of a binary present/absent. De-dupes to the FIRST (freshest, sort
    desc) headline per ticker. Gracefully ``[]`` when the data plan lacks news access."""
    cache_key = f"massive:recent_news_items:{int(max_age_min)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    url = f"{_base()}/v2/reference/news"
    data = _get(url, {"limit": str(limit), "order": "desc", "sort": "published_utc"})
    if data is _NOT_FOUND or not data:
        return []
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, int(max_age_min)))
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in data.get("results", []):
        ts = item.get("published_utc")
        if ts:
            try:
                if datetime.fromisoformat(str(ts).replace("Z", "+00:00")) < cutoff:
                    continue
            except (TypeError, ValueError):
                pass
        title = str(item.get("title") or "").strip()
        for t in item.get("tickers") or []:
            tt = str(t or "").upper().strip()
            if tt and tt not in seen:
                seen.add(tt)
                out.append((tt, title))
    _cache_set(cache_key, out)
    return out


def get_per_ticker_news_items(
    tickers, *, per_ticker_limit: int = 5, max_age_min: int = 120, max_tickers: int = 40
) -> list[tuple[str, str]]:
    """FIX E: ``(ticker, title)`` pairs for the IN-PLAY movers, queried PER TICKER instead of
    off the global ``/v2/reference/news`` firehose.

    The global firehose (``get_recent_news_items``) is sorted by ``published_utc desc`` and
    capped at ``limit`` (~200) items — on a busy tape the low-float micro-caps Ross trades get
    BURIED under higher-volume large-cap news and never appear, even when Polygon DOES carry a
    fresh headline for them (verified: ILLR/NXTT/NVCT/SDOT/SKYQ each have per-ticker news the
    firehose omitted). Querying ``/v2/reference/news?ticker=<T>`` for the names the lane is
    actually arming surfaces that coverage. FRESHNESS is still enforced (``max_age_min``) so a
    stale headline never tags — that residual lag is a provider constraint, not a code gap.

    Bounded to ``max_tickers`` per pass (one HTTP call each; the lane's in-play set is small).
    De-dupes to the freshest headline per ticker. Each source is independently fail-open: a
    miss / absent plan / error on one ticker never zeroes the others. Returns ``[]`` when the
    plan lacks news access. docs/STRATEGY/CC_REPORTS — FIX E catalyst-tagging repair."""
    syms: list[str] = []
    seen_in: set[str] = set()
    for t in (tickers or []):
        tt = str(t or "").upper().strip()
        # equities only — crypto (-USD) never has reference news
        if not tt or tt.endswith("-USD") or tt == "__AGGREGATE__" or tt in seen_in:
            continue
        seen_in.add(tt)
        syms.append(tt)
        if len(syms) >= max(1, int(max_tickers)):
            break
    if not syms:
        return []
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, int(max_age_min)))
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tk in syms:
        cache_key = f"massive:per_ticker_news:{tk}:{int(max_age_min)}"
        cached = _cache_get(cache_key)
        if cached is not None:
            for pair in cached:
                if pair[0] not in seen:
                    seen.add(pair[0])
                    out.append((pair[0], pair[1]))
            continue
        url = f"{_base()}/v2/reference/news"
        data = _get(url, {
            "ticker": tk, "limit": str(int(per_ticker_limit)),
            "order": "desc", "sort": "published_utc",
        })
        rows: list[tuple[str, str]] = []
        if data is not _NOT_FOUND and data:
            for item in data.get("results", []):
                ts = item.get("published_utc")
                if ts:
                    try:
                        if datetime.fromisoformat(str(ts).replace("Z", "+00:00")) < cutoff:
                            continue  # stale -> not a fresh catalyst
                    except (TypeError, ValueError):
                        pass  # unparseable -> keep (fail-open on freshness)
                title = str(item.get("title") or "").strip()
                rows.append((tk, title))
                break  # freshest fresh headline per ticker is enough to TAG it
        _cache_set(cache_key, rows)
        for pair in rows:
            if pair[0] not in seen:
                seen.add(pair[0])
                out.append((pair[0], pair[1]))
    return out


# Sector (SIC) is STATIC per ticker — a dedicated process-lifetime cache (re-warms on a
# restart) so the theme/sympathy clusterer never re-fetches. ``None`` = looked up, absent.
_SECTOR_CACHE: dict[str, str | None] = {}


def get_ticker_sector(ticker: str) -> str | None:
    """SIC sector/industry description for a ticker — the input to the theme/sympathy
    clusterer (gap #4). Reads Polygon ``/v3/reference/tickers/{t}`` ``sic_description``
    (reliable SEC SIC classification, covers low-float small-caps; verified on STI ->
    "Misc Electrical Machinery"). Cached for the process lifetime (sector is static).
    ``None`` when the ticker / field is unavailable (fail-open — never raises)."""
    tk = str(ticker or "").upper().strip()
    if not tk:
        return None
    if tk in _SECTOR_CACHE:
        return _SECTOR_CACHE[tk]
    sec: str | None = None
    try:
        d = _get(f"{_base()}/v3/reference/tickers/{tk}", {})
        if isinstance(d, dict):
            r = d.get("results") if isinstance(d.get("results"), dict) else {}
            sec = (str(r.get("sic_description") or "").strip() or None)
    except Exception:
        sec = None
    _SECTOR_CACHE[tk] = sec
    return sec


_FLOAT_CACHE: dict[str, float | None] = {}


def get_ticker_float(ticker: str) -> float | None:
    """REAL share count for the low-float (squeeze-ability) pillar, from the SAME
    ``/v3/reference/tickers/{t}`` endpoint as ``get_ticker_sector``:
    ``share_class_shares_outstanding`` (fallback ``weighted_shares_outstanding``). This is
    the actual SHARE COUNT the pillar wants — not the market_cap ``$`` proxy it currently
    falls back to. Cached for the process lifetime (~static; reverse-splits are rare).
    ``None`` when the ticker / field is unavailable (caller keeps the proxy; fail-open)."""
    tk = str(ticker or "").upper().strip()
    if not tk:
        return None
    if tk in _FLOAT_CACHE:
        return _FLOAT_CACHE[tk]
    val: float | None = None
    try:
        d = _get(f"{_base()}/v3/reference/tickers/{tk}", {})
        if isinstance(d, dict):
            r = d.get("results") if isinstance(d.get("results"), dict) else {}
            for k in ("share_class_shares_outstanding", "weighted_shares_outstanding"):
                v = r.get(k)
                if v is None:
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if fv > 0:
                    val = fv
                    break
    except Exception:
        val = None
    _FLOAT_CACHE[tk] = val
    return val


def get_recent_reverse_split_dates(*, max_age_days: int = 30, limit: int = 1000) -> dict[str, str]:
    """``{ticker: execution_date}`` for REVERSE stock splits executed within ``max_age_days``
    (Polygon ``/v3/reference/splits``). A reverse split is ``split_to < split_from`` (e.g.
    1-for-10 -> to=1, from=10); a forward split (2-for-1) is excluded. Ross's SS101 low-float
    squeeze targets a RECENT reverse split (the share count just collapsed). Cached briefly;
    fail-open to ``{}`` when the plan lacks the splits endpoint, so the corp-action refinement
    stays a no-op rather than an error. docs/DESIGN/MOMENTUM_LANE.md"""
    cache_key = f"massive:reverse_splits:{int(max_age_days)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    from datetime import date, timedelta

    cutoff = (date.today() - timedelta(days=max(1, int(max_age_days)))).isoformat()
    url = f"{_base()}/v3/reference/splits"
    data = _get(url, {
        "execution_date.gte": cutoff,
        "order": "desc", "sort": "execution_date", "limit": str(int(limit)),
    })
    out: dict[str, str] = {}
    if data is _NOT_FOUND or not data:
        _cache_set(cache_key, out)
        return out
    for row in (data.get("results") or []):
        try:
            tk = str(row.get("ticker") or "").upper().strip()
            sto = float(row.get("split_to") or 0.0)
            sfrom = float(row.get("split_from") or 0.0)
            xdate = str(row.get("execution_date") or "")
        except (TypeError, ValueError):
            continue
        if not tk or sto <= 0 or sfrom <= 0 or xdate < cutoff:
            continue
        if sto < sfrom and tk not in out:  # reverse split, keep the freshest (sort desc)
            out[tk] = xdate
    _cache_set(cache_key, out)
    return out


def get_theme_news_tickers(keywords: list[str], *, limit: int = 200, max_age_min: int = 240) -> list[str]:
    """Tickers whose FRESH headline matches the active EVENT THEME keywords.

    The sympathy-theme play (SpaceX IPO week: space/satellite/rocket/...) needs
    to know WHICH news names belong to the day's dominant theme — those keep
    their catalyst boost even when the hot-tape regime neutralizes generic news.
    Keyword-driven (no hardcoded ticker lists); matches title + description,
    case-insensitive. Wider freshness than the generic catalyst (a theme runs
    all session). Fail-open ``[]``."""
    kws = [str(k).strip().lower() for k in (keywords or []) if str(k).strip()]
    if not kws:
        return []
    cache_key = f"massive:theme_news:{','.join(sorted(kws))[:80]}:{int(max_age_min)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"{_base()}/v2/reference/news"
    data = _get(url, {"limit": str(limit), "order": "desc", "sort": "published_utc"})
    if data is _NOT_FOUND or not data:
        return []
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, int(max_age_min)))
    out: list[str] = []
    seen: set[str] = set()
    for item in data.get("results", []):
        ts = item.get("published_utc")
        if ts:
            try:
                pub = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if pub < cutoff:
                    continue
            except (TypeError, ValueError):
                pass
        text_blob = f"{item.get('title') or ''} {item.get('description') or ''}".lower()
        if not any(k in text_blob for k in kws):
            continue
        for t in item.get("tickers") or []:
            tt = str(t or "").upper().strip()
            if tt and tt not in seen:
                seen.add(tt)
                out.append(tt)
    _cache_set(cache_key, out)
    return out


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
