"""Rate-limited yfinance wrapper with in-memory response caching.

Modern yfinance (>=0.2.40) uses curl_cffi internally. Vanilla session
injection IS still supported on yfinance >=0.2.55 / >=1.3.0 via the
``session=`` kwarg on ``yf.Ticker(...)`` and ``yf.download(...)`` --
the older docstring caveat about rejected sessions referred specifically
to ``requests-cache`` wrapping. We hoist a single module-scope curl_cffi
session and inject it everywhere; the alternative (per-call default
session) made yfinance's internal ThreadPoolExecutor / per-call Thread
spawn pattern leak a Thread closure (~30 KiB Python bookkeeping each)
for every failed call. f-leak-3 (2026-05-04) closes that leak.

Layered design:

- A sliding-window token bucket gates all Yahoo Finance requests to 12 per 5
  seconds. Pure-Python (``threading.Lock`` + ``collections.deque``) with no
  background threads -- previous ``pyrate_limiter`` implementation spawned a
  daemon ``Leaker`` thread that called ``asyncio.run(...)`` on a loop, which
  on Windows leaked ``ProactorEventLoop`` IOCP handles and self-pipe sockets
  into the non-paged kernel pool. Over long sessions this accumulated to
  ``WinError 10055`` socket-pool exhaustion that blocked every subsequent
  ``socket.connect()`` -- including the test suite's psycopg2 DB connections.
- A shared ``curl_cffi.requests.Session`` (or fallback) is injected into
  every yfinance entry-point so the per-call Thread spawn collapses into a
  single connection-pooled client.
- A process-level circuit breaker short-circuits yfinance calls when the
  upstream is wall-to-wall failing (yahoo egress block, etc.), capping
  Thread accumulation during outages without per-symbol fanout.
- An in-memory TTL cache avoids re-fetching recently-seen data.
- ``get_ticker(symbol)`` is the single entry-point used by all services.
"""
from __future__ import annotations

import collections
import contextlib
import logging
import os
import threading
import time
from typing import Any

import requests
import yfinance as yf

from .socket_budget import mount_bounded_http_adapters

logger = logging.getLogger(__name__)
_YFINANCE_PROVIDER_LOGGER_NAMES = (
    "yfinance",
    "yfinance.base",
    "yfinance.data",
    "yfinance.scrapers.history",
)


@contextlib.contextmanager
def _quiet_yfinance_provider_logs():
    """Suppress noisy third-party yfinance ERROR lines during wrapped calls."""
    if not _env_bool("CHILI_YFINANCE_SUPPRESS_PROVIDER_ERRORS", True):
        yield
        return
    loggers = [logging.getLogger(name) for name in _YFINANCE_PROVIDER_LOGGER_NAMES]
    prior_levels = [log.level for log in loggers]
    try:
        for log in loggers:
            if log.level < logging.CRITICAL:
                log.setLevel(logging.CRITICAL)
        yield
    finally:
        for log, level in zip(loggers, prior_levels, strict=False):
            log.setLevel(level)


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("[yf_session] invalid %s=%r; using default %s", name, raw, default)
        return default
    if minimum is not None:
        return max(minimum, value)
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Rate limiter — 12 requests per 5 seconds (Yahoo's safe threshold)
#
# Sliding-window semantics: we track the timestamps of the last ``_RATE_MAX``
# acquisitions; a new acquisition is allowed iff the oldest tracked timestamp
# is older than ``_RATE_WINDOW_S`` seconds. On exhaustion we sleep just long
# enough for that oldest timestamp to age out, then retry.
#
# Design tradeoffs vs the previous pyrate_limiter-based implementation:
# * No background threads, no asyncio event loops, no kernel handle churn.
# * Slightly coarser semantics (sliding window vs leaky bucket) — fine for
#   our ~12 req/5s target; the yfinance backend has its own throttles anyway.
# * Process-local only. Acceptable: we run a single scheduler process.
# ---------------------------------------------------------------------------
_RATE_MAX = 12
_RATE_WINDOW_S = 5.0

_hits: collections.deque[float] = collections.deque(maxlen=_RATE_MAX)
_hits_lock = threading.Lock()


def _reset_limiter_for_tests() -> None:
    """Clear acquisition history. Intended for unit tests only."""
    with _hits_lock:
        _hits.clear()


def acquire() -> None:
    """Block (cooperatively, via ``time.sleep``) until a rate-limit token is
    available. Always returns when a slot becomes free — never raises.

    Semantics
    ---------
    Sliding window: allows up to ``_RATE_MAX`` acquisitions within any
    ``_RATE_WINDOW_S`` interval. If the limit is currently hit, sleeps just
    long enough for the oldest acquisition to fall out of the window.
    """
    while True:
        with _hits_lock:
            now = time.monotonic()
            # Drop stale entries from the front of the deque.
            while _hits and (now - _hits[0]) >= _RATE_WINDOW_S:
                _hits.popleft()
            if len(_hits) < _RATE_MAX:
                _hits.append(now)
                return
            # Full — compute how long until the oldest entry ages out.
            wait_s = _RATE_WINDOW_S - (now - _hits[0])
        if wait_s > 0:
            # Cap the sleep so a clock jump or bookkeeping glitch can't
            # stall us forever. The loop retries after the sleep.
            time.sleep(min(wait_s, _RATE_WINDOW_S))


# ---------------------------------------------------------------------------
# Shared module-scope HTTP session (f-leak-3, 2026-05-04)
#
# Hoisting a single curl_cffi session and injecting it into every
# ``yf.Ticker(symbol, session=...)`` + ``yf.download(..., session=...)``
# call collapses yfinance's per-call Thread spawn into one connection-
# pooled client. Without this, every failed yfinance call leaked an
# ``_make_invoke_excepthook.<locals>.invoke_excepthook`` closure -- the
# mem_watcher tick at 06:19:26 UTC counted 48,014 of these (~1.4 GiB
# Python overhead) on a single chili process.
#
# Fallback chain: curl_cffi (preferred -- yfinance uses it natively when
# available) -> ``None`` (yfinance picks its default). We do NOT fall back
# to ``requests.Session`` because that re-introduces the same Thread spawn
# pattern; the failure mode of ``None`` is "yfinance default", which is at
# least as good as the pre-fix state.
# ---------------------------------------------------------------------------
def _build_shared_session():
    """Return a curl_cffi session with browser impersonation, or None."""
    try:
        from curl_cffi import requests as _cc_requests  # type: ignore
        # impersonate="chrome" mimics a real browser TLS fingerprint, which
        # Yahoo's CDN occasionally requires. Default safe; can be overridden
        # via ``yf_session._SHARED_SESSION = ...`` in tests if needed.
        return _cc_requests.Session(impersonate="chrome")
    except Exception:
        logger.debug(
            "[yf_session] curl_cffi unavailable; falling back to default session"
        )
        return None


_SHARED_SESSION = _build_shared_session()


# ---------------------------------------------------------------------------
# Process-level circuit breaker (f-leak-3, 2026-05-04)
#
# Tracks consecutive yfinance failures across ALL symbols. When N in a row
# fail (yahoo egress block, timeout storm, etc.), trip OPEN and short-
# circuit subsequent calls to the same default the function would return
# on miss (empty DataFrame / None / {}). After ``HALF_OPEN_TTL_S`` seconds,
# allow one probe call through; on success close, on failure re-open.
#
# Why this matters for the leak: yfinance internally spawns Threads for
# concurrent fetches. During an outage, the brain hits N tickers/sec, each
# spawning a Thread that fails-and-leaks. The breaker caps the leak rate
# at ``THRESHOLD`` Threads per ``HALF_OPEN_TTL_S`` window instead of
# letting the outage burn one Thread per call.
#
# Passive design: state mutates only on success/failure of explicit calls,
# no background timer thread (PROTOCOL Hard Rule, no new threads).
# ---------------------------------------------------------------------------
# Trip OPEN after this many consecutive upstream failures. 10 chosen as a
# starting seed -- tolerates a brief failure burst from a transient network
# blip but trips quickly enough that a sustained outage doesn't burn through
# many Threads. Tuning candidate; surface in CC report Open Questions if
# soak suggests a different value.
_BREAKER_CONSECUTIVE_FAILURE_THRESHOLD = 10
# Stay OPEN for this many seconds, then HALF_OPEN to probe. 60s gives
# yahoo egress / firewall / DNS time to recover without keeping the
# breaker tripped unnecessarily long. Tuning candidate.
_BREAKER_HALF_OPEN_TTL_S = 60.0

_breaker_lock = threading.Lock()
_breaker_consecutive_failures: int = 0
_breaker_state: str = "CLOSED"  # "CLOSED" | "OPEN" | "HALF_OPEN"
_breaker_opened_at: float = 0.0


def _reset_breaker_for_tests() -> None:
    """Reset the breaker. Intended for unit tests only."""
    global _breaker_consecutive_failures, _breaker_state, _breaker_opened_at
    with _breaker_lock:
        _breaker_consecutive_failures = 0
        _breaker_state = "CLOSED"
        _breaker_opened_at = 0.0


def _breaker_should_short_circuit() -> bool:
    """Return True iff the breaker is currently OPEN. Cheap, no I/O.

    On TTL expiry, transitions OPEN -> HALF_OPEN and returns False so a
    single probe call passes through; the next ``_breaker_on_success``
    closes it, ``_breaker_on_failure`` re-opens for another TTL.
    """
    global _breaker_state, _breaker_opened_at
    with _breaker_lock:
        if _breaker_state == "CLOSED":
            return False
        if _breaker_state == "OPEN":
            if time.monotonic() - _breaker_opened_at >= _BREAKER_HALF_OPEN_TTL_S:
                _breaker_state = "HALF_OPEN"
                logger.info("[yf_breaker] HALF_OPEN: probing")
                return False
            return True
        # HALF_OPEN: let the probe through; outcome handlers below own state.
        return False


def _breaker_on_success() -> None:
    """Reset failure counter on any successful upstream call. CLOSE the
    breaker if it was OPEN/HALF_OPEN."""
    global _breaker_state, _breaker_consecutive_failures
    with _breaker_lock:
        prev_state = _breaker_state
        _breaker_consecutive_failures = 0
        if prev_state != "CLOSED":
            _breaker_state = "CLOSED"
            logger.info("[yf_breaker] CLOSED: success after %s", prev_state)


def _breaker_on_failure() -> None:
    """Increment failure counter; trip OPEN at threshold or on HALF_OPEN
    probe failure."""
    global _breaker_state, _breaker_consecutive_failures, _breaker_opened_at
    with _breaker_lock:
        _breaker_consecutive_failures += 1
        should_trip = (
            _breaker_state == "HALF_OPEN"
            or (
                _breaker_state == "CLOSED"
                and _breaker_consecutive_failures
                >= _BREAKER_CONSECUTIVE_FAILURE_THRESHOLD
            )
        )
        if should_trip and _breaker_state != "OPEN":
            logger.warning(
                "[yf_breaker] OPEN: %s consecutive upstream failures",
                _breaker_consecutive_failures,
            )
            _breaker_state = "OPEN"
            _breaker_opened_at = time.monotonic()


# ---------------------------------------------------------------------------
# In-memory TTL cache for history() and fast_info results
# ---------------------------------------------------------------------------
_cache: collections.OrderedDict[str, tuple[float, Any]] = collections.OrderedDict()
_cache_lock = threading.Lock()

_TTL_HISTORY = 3600    # 1 hour for OHLCV / indicator data (64 GB RAM)
_TTL_QUOTE = 30        # 30 seconds for live price
_TTL_QUOTE_MISS = 120  # 2 minutes for transient fast_info failures
_YF_BATCH_MISS_COOLDOWN_ENV = "CHILI_YF_BATCH_MISS_COOLDOWN_S"
_BATCH_MISS_COOLDOWN_DEFAULT_S = float(_TTL_QUOTE_MISS)
_MIN_BATCH_MISS_COOLDOWN_S = 0.0
_TTL_BATCH_MISS = _env_float(
    _YF_BATCH_MISS_COOLDOWN_ENV,
    _BATCH_MISS_COOLDOWN_DEFAULT_S,
    minimum=_MIN_BATCH_MISS_COOLDOWN_S,
)
_YF_CRYPTO_HISTORY_MISS_COOLDOWN_ENV = "CHILI_YF_CRYPTO_HISTORY_MISS_COOLDOWN_S"
_CRYPTO_HISTORY_MISS_COOLDOWN_DEFAULT_S = _TTL_BATCH_MISS
_TTL_CRYPTO_HISTORY_MISS = _env_float(
    _YF_CRYPTO_HISTORY_MISS_COOLDOWN_ENV,
    _CRYPTO_HISTORY_MISS_COOLDOWN_DEFAULT_S,
    minimum=_MIN_BATCH_MISS_COOLDOWN_S,
)
_YF_ALLOW_CRYPTO_BATCH_ENV = "CHILI_YF_ALLOW_CRYPTO_BATCH"
_ALLOW_CRYPTO_BATCH = _env_bool(_YF_ALLOW_CRYPTO_BATCH_ENV, False)
_TTL_SEARCH = 3600     # 1 hour for search results
_TTL_FUNDAMENTALS = 86400  # 24 hours for fundamental data
_TTL_TICKER_INFO = 3600   # 1 hour for ticker info strip
_TTL_NEWS = 600        # 10 minutes for ticker news
_TTL_DEAD = 14400      # 4 hours for known-bad stock tickers
_TTL_DEAD_CRYPTO = 1800  # 30 minutes for crypto (they may just be new/different format)
_MAX_CACHE_SIZE = 10_000   # 64 GB RAM — keep much more in memory

_dead_tickers: collections.OrderedDict[str, float] = collections.OrderedDict()
_dead_lock = threading.Lock()
_MAX_DEAD_TICKERS = 10_000

# Consecutive-empty counter: only mark a ticker dead after N empty results in a
# row from yfinance, never on the first empty. Reason: when an upstream provider
# (e.g., Massive/Polygon) is blocked and the priority chain falls back to yf,
# yf gets hammered and returns empty for many tickers due to throttling. The
# old "any empty -> mark dead" logic mass-mis-classified live tickers as
# delisted (incident 2026-04-19, see project_massive_blocked.md). Reset on any
# non-empty result so a single recovery clears the streak.
_EMPTY_THRESHOLD = 3
_empty_counts: collections.OrderedDict[str, int] = collections.OrderedDict()
_empty_lock = threading.Lock()
_MAX_EMPTY_COUNTS = 10_000

_YF_NO_DATA_ERROR_MARKERS = (
    "delisted",
    "no data",
)
_YF_FAST_INFO_EMPTY_ERROR_MARKERS = (
    "pricehistory",
    "_dividends",
)
_QUOTE_MISS_CACHE_PREFIX = "quote_miss:"
_BATCH_MISS_CACHE_PREFIX = "batch_miss:"
_CRYPTO_HISTORY_MISS_CACHE_PREFIX = "crypto_history_miss:"


def _bump_empty(symbol: str) -> int:
    with _empty_lock:
        streak = _empty_counts.pop(symbol, 0) + 1
        _empty_counts[symbol] = streak
        while len(_empty_counts) > _MAX_EMPTY_COUNTS:
            _empty_counts.popitem(last=False)
        return _empty_counts[symbol]


def _reset_empty(symbol: str) -> None:
    with _empty_lock:
        _empty_counts.pop(symbol, None)


def _record_empty_yf_result(symbol: str, *, allow_crypto: bool = False) -> None:
    """Negative-cache confirmed Yahoo misses after the empty threshold.

    Crypto is opt-in because single-symbol history can be backed by other
    crypto-specific providers, while batch prewarm is Yahoo-only and should
    not keep retrying symbols Yahoo cannot resolve.
    """
    if _is_crypto(symbol) and not allow_crypto:
        return
    streak = _bump_empty(symbol)
    if streak >= _EMPTY_THRESHOLD:
        _mark_dead(symbol)
        _reset_empty(symbol)


def _record_yf_batch_miss(symbol: str, *, single_symbol_batch: bool) -> None:
    """Record a Yahoo batch miss only when the evidence is symbol-specific.

    Mixed Yahoo batches can omit live equities during transient provider
    trouble, so equity misses from a mixed batch are too weak for dead-cache
    evidence. Crypto misses are still useful because the dead cache is short
    lived and routes quotes toward the crypto fallback path.
    """
    if _is_crypto(symbol):
        _cache_set(_crypto_history_miss_key(symbol), True)
    if not single_symbol_batch:
        _cache_set(_batch_miss_key(symbol), True)
    if single_symbol_batch or _is_crypto(symbol):
        _record_empty_yf_result(symbol, allow_crypto=True)


def _looks_like_yf_no_data_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _YF_NO_DATA_ERROR_MARKERS)


def _looks_like_yf_fast_info_empty_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        _looks_like_yf_no_data_error(exc)
        or any(marker in text for marker in _YF_FAST_INFO_EMPTY_ERROR_MARKERS)
    )


def _is_crypto(symbol: str) -> bool:
    """Check if a symbol is a crypto ticker."""
    return symbol.upper().endswith("-USD")


def _quote_miss_key(symbol: str) -> str:
    return f"{_QUOTE_MISS_CACHE_PREFIX}{symbol}"


def _batch_miss_key(symbol: str) -> str:
    return f"{_BATCH_MISS_CACHE_PREFIX}{symbol}"


def _crypto_history_miss_key(symbol: str) -> str:
    return f"{_CRYPTO_HISTORY_MISS_CACHE_PREFIX}{symbol}"


def _should_skip_crypto_yahoo_probe(symbol: str) -> bool:
    """Avoid repeated single-symbol Yahoo probes for recent crypto misses."""
    return _is_crypto(symbol) and (
        _cache_get(_batch_miss_key(symbol)) is not None
        or _cache_get(_crypto_history_miss_key(symbol)) is not None
    )


def _cache_crypto_fallback_quote(
    symbol: str,
    *,
    cache_key: str,
    miss_key: str,
) -> dict[str, Any] | None:
    result = _coingecko_quote(symbol)
    if result:
        _cache_set(cache_key, result)
        _cache_pop(miss_key)
    else:
        _cache_set(miss_key, True)
    return result


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
        now = time.time()
        _cache.pop(key, None)
        _cache[key] = (now, val)
        if len(_cache) > _MAX_CACHE_SIZE:
            _prune_cache_locked(now)


def _prune_cache_locked(now: float) -> None:
    cutoff = now - 60
    while _cache:
        oldest = next(iter(_cache))
        ts, _value = _cache[oldest]
        if ts >= cutoff:
            break
        _cache.pop(oldest, None)
    if len(_cache) <= _MAX_CACHE_SIZE:
        return

    target_size = max(1, int(_MAX_CACHE_SIZE * 0.9))
    while len(_cache) > target_size:
        _cache.popitem(last=False)


def _cache_pop(key: str) -> None:
    with _cache_lock:
        _cache.pop(key, None)


def _get_ttl(key: str) -> float:
    if key.startswith(_QUOTE_MISS_CACHE_PREFIX):
        return _TTL_QUOTE_MISS
    if key.startswith(_BATCH_MISS_CACHE_PREFIX):
        return _TTL_BATCH_MISS
    if key.startswith(_CRYPTO_HISTORY_MISS_CACHE_PREFIX):
        return _TTL_CRYPTO_HISTORY_MISS
    if key.startswith("quote:"):
        return _TTL_QUOTE
    if key.startswith("search:"):
        return _TTL_SEARCH
    if key.startswith("fund:"):
        return _TTL_FUNDAMENTALS
    if key.startswith("ticker_info:"):
        return _TTL_TICKER_INFO
    if key.startswith("news:"):
        return _TTL_NEWS
    return _TTL_HISTORY


def _history_cache_key(
    symbol: str,
    *,
    period: Any = "6mo",
    interval: Any = "1d",
    start: Any = None,
    end: Any = None,
    prepost: Any = False,
    auto_adjust: Any = True,
    actions: Any = True,
) -> str:
    return (
        f"hist:{symbol}:{period}:{interval}:{start}:{end}"
        f":pp={prepost}:aa={auto_adjust}:ac={actions}"
    )


def _is_dead(symbol: str) -> bool:
    """Check if a ticker is in the negative cache (known bad)."""
    with _dead_lock:
        ts = _dead_tickers.get(symbol)
        if ts is None:
            return False
        ttl = _TTL_DEAD_CRYPTO if _is_crypto(symbol) else _TTL_DEAD
        if time.time() - ts > ttl:
            del _dead_tickers[symbol]
            return False
        return True


def _mark_dead(symbol: str, force: bool = False) -> None:
    """Add ticker to the negative cache after confirmed failure.
    
    For crypto tickers, use shorter TTL since they may be new coins or
    use different formats on different APIs.
    """
    if _is_crypto(symbol) and not force:
        ttl = _TTL_DEAD_CRYPTO
    else:
        ttl = _TTL_DEAD
    with _dead_lock:
        _dead_tickers.pop(symbol, None)
        _dead_tickers[symbol] = time.time()
        while len(_dead_tickers) > _MAX_DEAD_TICKERS:
            _dead_tickers.popitem(last=False)
    logger.info(f"[yf_session] Marked {symbol} as dead (skip for {ttl}s)")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_ticker(symbol: str) -> yf.Ticker:
    """Return a yf.Ticker bound to the shared module-scope session.

    Rate limiting happens in ``get_history`` / ``get_fast_info`` wrappers.
    For direct ``yf.Ticker`` usage the caller should call ``acquire()`` first.

    f-leak-3 (2026-05-04): the shared session collapses yfinance's
    per-call Thread spawn pattern. Caller surface unchanged.
    """
    acquire()
    return yf.Ticker(symbol, session=_SHARED_SESSION)


def get_history(symbol: str, **kwargs) -> Any:
    """Rate-limited + cached wrapper around ``yf.Ticker(symbol).history(**kwargs)``.

    Returns a DataFrame (possibly empty on error). Skips known-dead tickers.
    """
    import pandas as pd

    if _is_dead(symbol):
        return pd.DataFrame()
    if _should_skip_crypto_yahoo_probe(symbol):
        logger.debug(
            "[yf_session] history(%s) skipped by crypto Yahoo cooldown",
            symbol,
        )
        return pd.DataFrame()

    period = kwargs.get("period", "6mo")
    interval = kwargs.get("interval", "1d")
    start = kwargs.get("start")
    end = kwargs.get("end")
    # Round-21 FIX (2026-04-30, third-party audit HIGH): cache key now
    # includes ``end``, ``prepost``, ``auto_adjust``, ``actions`` -- any
    # parameter that changes the returned DataFrame must be in the key.
    # Prior code keyed only on (symbol, period, interval, start), so a
    # caller asking for end=2026-02-01 would silently receive a cached
    # answer to end=2026-03-01 issued seconds earlier. That's a "lying
    # cache" -- different temporal questions getting the same answer.
    prepost = kwargs.get("prepost", False)
    auto_adjust = kwargs.get("auto_adjust", True)
    actions = kwargs.get("actions", True)
    cache_key = _history_cache_key(
        symbol,
        period=period,
        interval=interval,
        start=start,
        end=end,
        prepost=prepost,
        auto_adjust=auto_adjust,
        actions=actions,
    )

    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # f-leak-3 (2026-05-04): breaker short-circuit. Cache hits and
    # _is_dead skips above don't count as either success or failure;
    # they're independent of upstream health.
    if _breaker_should_short_circuit():
        return pd.DataFrame()

    acquire()
    failed = False
    try:
        with _quiet_yfinance_provider_logs():
            t = yf.Ticker(symbol, session=_SHARED_SESSION)
            df = t.history(**kwargs)
    except Exception as e:
        logger.warning(f"[yf_session] history({symbol}) failed: {e}")
        df = pd.DataFrame()
        failed = True
        # Only mark as dead on actual errors, not just empty data
        if _looks_like_yf_no_data_error(e):
            _mark_dead(symbol)

    # For crypto, don't mark as dead just because yfinance returned empty
    # - The coin might be new or use a different format
    # - Massive or CoinGecko may still have the data
    if df.empty and not _is_crypto(symbol):
        # 2026-04-28 leak fix: respect the consecutive-empty threshold defined
        # at lines 102-109. The previous code ALWAYS marked dead on the first
        # empty response — which is exactly what the comment block warns
        # against. ^VIX was the canonical victim: a transient yfinance empty
        # would land it in the dead cache and short-circuit every subsequent
        # call until TTL expired.
        _record_empty_yf_result(symbol)
        # Empty stock response counts as a breaker failure (upstream
        # responded but with no data). Crypto-empty is non-signal and
        # is handled outside this branch, so no breaker tick.
        failed = True
    elif df.empty:
        _cache_set(_crypto_history_miss_key(symbol), True)
    elif not df.empty:
        # Reset the streak so a single recovery clears prior empties.
        _reset_empty(symbol)
        _cache_pop(_batch_miss_key(symbol))
        _cache_pop(_crypto_history_miss_key(symbol))

    if failed:
        _breaker_on_failure()
    else:
        _breaker_on_success()

    _cache_set(cache_key, df)

    # Seed the quote cache from OHLCV data so fetch_quote() is free after chart load
    if not df.empty:
        try:
            last_row = df.iloc[-1]
            quote_key = f"quote:{symbol}"
            if _cache_get(quote_key) is None:
                _cache_set(quote_key, {
                    "last_price": float(last_row["Close"]),
                    "previous_close": float(df.iloc[-2]["Close"]) if len(df) >= 2 else None,
                    "day_high": float(last_row["High"]),
                    "day_low": float(last_row["Low"]),
                    "volume": int(last_row["Volume"]) if last_row["Volume"] else None,
                    "market_cap": None,
                })
        except Exception:
            pass

    return df


def get_fast_info(symbol: str) -> dict[str, Any] | None:
    """Rate-limited + cached wrapper around ``yf.Ticker(symbol).fast_info``.

    Returns all available fields including year_high, year_low, avg_volume
    so callers don't need a separate API call for those.
    Falls back to CoinGecko for crypto tickers that yfinance can't resolve.
    """
    cache_key = f"quote:{symbol}"
    miss_key = _quote_miss_key(symbol)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    if _cache_get(miss_key) is not None:
        return None

    is_crypto = _is_crypto(symbol)

    if _is_dead(symbol) and is_crypto:
        result = _coingecko_quote(symbol)
        if result:
            _cache_set(cache_key, result)
            _cache_pop(miss_key)
        return result

    if _is_dead(symbol):
        return None

    if _should_skip_crypto_yahoo_probe(symbol):
        logger.debug(
            "[yf_session] fast_info(%s) routed to crypto fallback by Yahoo cooldown",
            symbol,
        )
        return _cache_crypto_fallback_quote(
            symbol,
            cache_key=cache_key,
            miss_key=miss_key,
        )

    if _breaker_should_short_circuit():
        return None

    acquire()
    try:
        with _quiet_yfinance_provider_logs():
            t = yf.Ticker(symbol, session=_SHARED_SESSION)
            info = t.fast_info
            last_price = info.last_price
            previous_close = info.previous_close
            day_high = info.day_high if hasattr(info, "day_high") else None
            day_low = info.day_low if hasattr(info, "day_low") else None
            last_volume = info.last_volume if hasattr(info, "last_volume") else None
            market_cap = info.market_cap if hasattr(info, "market_cap") else None
            year_high = info.year_high if hasattr(info, "year_high") else None
            year_low = info.year_low if hasattr(info, "year_low") else None
            avg_volume = (
                info.three_month_average_volume
                if hasattr(info, "three_month_average_volume")
                else None
            )
        result = {
            "last_price": float(last_price) if last_price else None,
            "previous_close": float(previous_close) if previous_close else None,
            "day_high": float(day_high) if day_high else None,
            "day_low": float(day_low) if day_low else None,
            "volume": int(last_volume) if last_volume else None,
            "market_cap": float(market_cap) if market_cap else None,
            "year_high": float(year_high) if year_high else None,
            "year_low": float(year_low) if year_low else None,
            "avg_volume": int(avg_volume) if avg_volume else None,
        }
        _cache_pop(miss_key)
        _breaker_on_success()
    except Exception as e:
        logger.warning(f"[yf_session] fast_info({symbol}) failed: {e}")
        result = None
        explicit_no_data = _looks_like_yf_no_data_error(e)
        if explicit_no_data or (
            is_crypto and _looks_like_yf_fast_info_empty_error(e)
        ):
            if is_crypto:
                _cache_set(_crypto_history_miss_key(symbol), True)
            else:
                _cache_set(miss_key, True)
            _record_empty_yf_result(symbol, allow_crypto=is_crypto)
            if is_crypto and _is_dead(symbol):
                result = _cache_crypto_fallback_quote(
                    symbol,
                    cache_key=cache_key,
                    miss_key=miss_key,
                )
        if result is None and not is_crypto and not explicit_no_data:
            _cache_set(miss_key, True)
        _breaker_on_failure()

    if result is not None:
        _cache_set(cache_key, result)
    return result


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


_COINGECKO_SYMBOL_MAP: dict[str, str] = {}
_COINGECKO_SESSION = requests.Session()
mount_bounded_http_adapters(_COINGECKO_SESSION)


def _coingecko_quote(symbol: str) -> dict[str, Any] | None:
    """Fallback: fetch price from CoinGecko for crypto tickers yfinance can't resolve."""
    try:
        coin_id = symbol.upper().replace("-USD", "").lower()
        # CoinGecko needs coin IDs, not symbols — try common mappings first
        known = {
            "btc": "bitcoin", "eth": "ethereum", "sol": "solana", "ada": "cardano",
            "xrp": "ripple", "doge": "dogecoin", "avax": "avalanche-2", "dot": "polkadot",
            "link": "chainlink", "matic": "matic-network", "shib": "shiba-inu",
            "pepe": "pepe", "sui": "sui", "tao": "bittensor", "hype": "hyperliquid",
            "pengu": "pudgy-penguins", "pi": "pi-network",
            "near": "near", "atom": "cosmos", "uni": "uniswap", "aave": "aave",
            "ape": "apecoin", "arb": "arbitrum", "op": "optimism", "ftm": "fantom",
            "fil": "filecoin", "grt": "the-graph", "inj": "injective-protocol",
            "apt": "aptos", "sei": "sei-network", "jup": "jupiter-exchange-solana",
            "wif": "dogwifcoin", "bonk": "bonk", "floki": "floki",
            "render": "render-token", "fet": "artificial-superintelligence-alliance",
            "ondo": "ondo-finance", "kas": "kaspa", "imx": "immutable-x",
        }
        cg_id = known.get(coin_id) or _COINGECKO_SYMBOL_MAP.get(coin_id)
        if not cg_id:
            try:
                search_resp = _COINGECKO_SESSION.get(
                    "https://api.coingecko.com/api/v3/search",
                    params={"query": coin_id}, timeout=6,
                )
                search_resp.raise_for_status()
                coins = search_resp.json().get("coins", [])
                for c in coins:
                    if c.get("symbol", "").upper() == coin_id.upper():
                        cg_id = c["id"]
                        _COINGECKO_SYMBOL_MAP[coin_id] = cg_id
                        break
            except Exception:
                pass
            if not cg_id:
                cg_id = coin_id
        resp = _COINGECKO_SESSION.get(
            f"https://api.coingecko.com/api/v3/simple/price",
            params={"ids": cg_id, "vs_currencies": "usd", "include_24hr_change": "true",
                    "include_24hr_vol": "true", "include_market_cap": "true"},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json().get(cg_id)
        if not data or "usd" not in data:
            return None
        return {
            "last_price": data["usd"],
            "previous_close": None,
            "day_high": None,
            "day_low": None,
            "volume": int(data.get("usd_24h_vol", 0)) or None,
            "market_cap": data.get("usd_market_cap"),
            "year_high": None,
            "year_low": None,
            "avg_volume": None,
        }
    except Exception as e:
        logger.debug(f"[yf_session] CoinGecko fallback for {symbol} failed: {e}")
        return None


def _fmt_large(val: float | None) -> str | None:
    """Format large numbers for display (e.g. 385.6B, 12.3M)."""
    if val is None:
        return None
    abs_val = abs(val)
    if abs_val >= 1e12:
        return f"${val / 1e12:.1f}T"
    if abs_val >= 1e9:
        return f"${val / 1e9:.1f}B"
    if abs_val >= 1e6:
        return f"${val / 1e6:.1f}M"
    return f"${val:,.0f}"


def batch_download(
    symbols: list[str],
    period: str = "6mo",
    interval: str = "1d",
) -> dict[str, Any]:
    """Download OHLCV data for multiple tickers in one HTTP request via yf.download().

    Returns a dict mapping symbol -> DataFrame.  Each result is individually cached
    so subsequent ``get_history()`` calls hit the cache.
    """
    import pandas as pd

    uncached: list[str] = []
    result: dict[str, Any] = {}
    mixed_request = len(symbols) > 1
    batch_miss_cooldown_skips = 0
    prepost = False
    auto_adjust = True
    actions = True
    for sym in symbols:
        if _is_dead(sym):
            continue
        if _is_crypto(sym) and not _ALLOW_CRYPTO_BATCH:
            _cache_set(_crypto_history_miss_key(sym), True)
            batch_miss_cooldown_skips += 1
            continue
        if _is_crypto(sym) and _should_skip_crypto_yahoo_probe(sym):
            batch_miss_cooldown_skips += 1
            continue
        if mixed_request and _cache_get(_batch_miss_key(sym)) is not None:
            batch_miss_cooldown_skips += 1
            continue
        key = _history_cache_key(
            sym,
            period=period,
            interval=interval,
            prepost=prepost,
            auto_adjust=auto_adjust,
            actions=actions,
        )
        cached = _cache_get(key)
        if cached is not None:
            result[sym] = cached
        else:
            uncached.append(sym)

    if not uncached:
        if batch_miss_cooldown_skips:
            logger.info(
                "[yf_session] batch_download: 0 requested, %s returned, "
                "%s skipped by batch-miss cooldown",
                len(result),
                batch_miss_cooldown_skips,
            )
        return result

    if _breaker_should_short_circuit():
        return result

    acquire()
    try:
        # f-leak-3 (2026-05-04): pass the shared session AND set
        # threads=False. yfinance's download path with threads=True
        # spawns a ThreadPoolExecutor of N workers; on a yahoo-egress
        # outage that's N leaked Thread closures per call. The shared
        # session pools connections; threads=False keeps the call
        # synchronous on the caller thread.
        with _quiet_yfinance_provider_logs():
            df = yf.download(
                uncached,
                period=period,
                interval=interval,
                group_by="ticker",
                threads=False,
                session=_SHARED_SESSION,
                progress=False,
                prepost=prepost,
                auto_adjust=auto_adjust,
                actions=actions,
            )
    except Exception as e:
        logger.warning(f"[yf_session] batch_download failed: {e}")
        _breaker_on_failure()
        return result

    if df.empty:
        # Empty batch on a non-empty input list signals an upstream
        # problem -- count as breaker failure.
        single_symbol_batch = not mixed_request
        for sym in uncached:
            _record_yf_batch_miss(sym, single_symbol_batch=single_symbol_batch)
        _breaker_on_failure()
        return result
    _breaker_on_success()

    found_symbols: set[str] = set()
    if len(uncached) == 1:
        sym = uncached[0]
        key = _history_cache_key(
            sym,
            period=period,
            interval=interval,
            prepost=prepost,
            auto_adjust=auto_adjust,
            actions=actions,
        )
        _cache_set(key, df)
        result[sym] = df
        found_symbols.add(sym)
        _reset_empty(sym)
        _cache_pop(_batch_miss_key(sym))
        # seed quote cache
        if not df.empty:
            try:
                last = df.iloc[-1]
                qk = f"quote:{sym}"
                if _cache_get(qk) is None:
                    _cache_set(qk, {
                        "last_price": float(last["Close"]),
                        "previous_close": float(df.iloc[-2]["Close"]) if len(df) >= 2 else None,
                        "day_high": float(last["High"]),
                        "day_low": float(last["Low"]),
                        "volume": int(last["Volume"]) if last["Volume"] else None,
                        "market_cap": None,
                    })
            except Exception:
                pass
    else:
        for sym in uncached:
            try:
                if sym in df.columns.get_level_values(0):
                    ticker_df = df[sym].dropna(how="all")
                    if not ticker_df.empty:
                        key = _history_cache_key(
                            sym,
                            period=period,
                            interval=interval,
                            prepost=prepost,
                            auto_adjust=auto_adjust,
                            actions=actions,
                        )
                        _cache_set(key, ticker_df)
                        result[sym] = ticker_df
                        found_symbols.add(sym)
                        _reset_empty(sym)
                        _cache_pop(_batch_miss_key(sym))
                        try:
                            last = ticker_df.iloc[-1]
                            qk = f"quote:{sym}"
                            if _cache_get(qk) is None:
                                _cache_set(qk, {
                                    "last_price": float(last["Close"]),
                                    "previous_close": float(ticker_df.iloc[-2]["Close"]) if len(ticker_df) >= 2 else None,
                                    "day_high": float(last["High"]),
                                    "day_low": float(last["Low"]),
                                    "volume": int(last["Volume"]) if last["Volume"] else None,
                                    "market_cap": None,
                                })
                        except Exception:
                            pass
            except Exception:
                continue

    for sym in uncached:
        if sym not in found_symbols:
            _record_yf_batch_miss(sym, single_symbol_batch=False)

    if batch_miss_cooldown_skips:
        logger.info(
            "[yf_session] batch_download: %s requested, %s returned, "
            "%s skipped by batch-miss cooldown",
            len(uncached),
            len(result),
            batch_miss_cooldown_skips,
        )
    else:
        logger.info(
            "[yf_session] batch_download: %s requested, %s returned",
            len(uncached),
            len(result),
        )
    return result


_FUND_EMPTY = "__no_fundamentals__"


def get_fundamentals(symbol: str) -> dict[str, Any] | None:
    """Rate-limited + cached wrapper for fundamental data via ``yf.Ticker(symbol).info``.

    Returns a normalized dict with valuation, growth, profitability, and financial
    health metrics.  Cached for 24 hours.  Returns ``None`` on error or if the
    ticker has no fundamental data (e.g. most crypto).
    """
    cache_key = f"fund:{symbol}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return None if cached == _FUND_EMPTY else cached

    if _breaker_should_short_circuit():
        return None

    acquire()
    try:
        t = yf.Ticker(symbol, session=_SHARED_SESSION)
        info = t.info
        if not info or not info.get("shortName"):
            _cache_set(cache_key, _FUND_EMPTY)
            _breaker_on_failure()
            return None

        result = {
            "short_name": info.get("shortName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "market_cap": _safe_float(info.get("marketCap")),
            "market_cap_fmt": _fmt_large(_safe_float(info.get("marketCap"))),
            # Valuation
            "pe_trailing": _safe_float(info.get("trailingPE")),
            "pe_forward": _safe_float(info.get("forwardPE")),
            "eps_trailing": _safe_float(info.get("trailingEps")),
            "eps_forward": _safe_float(info.get("forwardEps")),
            "price_to_sales": _safe_float(info.get("priceToSalesTrailing12Months")),
            "price_to_book": _safe_float(info.get("priceToBook")),
            "ev_to_ebitda": _safe_float(info.get("enterpriseToEbitda")),
            "peg_ratio": _safe_float(info.get("pegRatio")),
            # Growth
            "revenue": _safe_float(info.get("totalRevenue")),
            "revenue_fmt": _fmt_large(_safe_float(info.get("totalRevenue"))),
            "revenue_growth": _safe_float(info.get("revenueGrowth")),
            "earnings_growth": _safe_float(info.get("earningsGrowth")),
            # Profitability
            "gross_margins": _safe_float(info.get("grossMargins")),
            "operating_margins": _safe_float(info.get("operatingMargins")),
            "profit_margins": _safe_float(info.get("profitMargins")),
            "return_on_equity": _safe_float(info.get("returnOnEquity")),
            # Financial health
            "free_cash_flow": _safe_float(info.get("freeCashflow")),
            "free_cash_flow_fmt": _fmt_large(_safe_float(info.get("freeCashflow"))),
            "total_debt": _safe_float(info.get("totalDebt")),
            "total_debt_fmt": _fmt_large(_safe_float(info.get("totalDebt"))),
            "debt_to_equity": _safe_float(info.get("debtToEquity")),
            # Dividend
            "dividend_yield": _safe_float(info.get("dividendYield")),
        }
    except Exception as e:
        logger.warning(f"[yf_session] fundamentals({symbol}) failed: {e}")
        _cache_set(cache_key, _FUND_EMPTY)
        _breaker_on_failure()
        return None

    _cache_set(cache_key, result)
    _breaker_on_success()
    return result


def get_ticker_info(symbol: str) -> dict[str, Any] | None:
    """Compact ticker metadata for the detail strip: name, sector/type, mcap, P/E, description.

    Works for both stocks (sector, industry) and crypto (category). Cached 1 hour.
    """
    cache_key = f"ticker_info:{symbol}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if _breaker_should_short_circuit():
        return None

    acquire()
    try:
        t = yf.Ticker(symbol, session=_SHARED_SESSION)
        info = t.info
        if not info:
            _breaker_on_failure()
            return None

        name = info.get("shortName") or info.get("longName") or symbol
        sector = info.get("sector") or info.get("industry") or info.get("category") or "—"
        mcap = _safe_float(info.get("marketCap"))
        mcap_fmt = _fmt_large(mcap) if mcap else None
        pe = _safe_float(info.get("trailingPE"))
        desc = (info.get("longBusinessSummary") or info.get("description") or "").strip()
        if desc:
            desc = desc[:300] + "…" if len(desc) > 300 else desc
        else:
            desc = None

        result = {
            "name": name,
            "sector_or_type": sector,
            "market_cap_fmt": mcap_fmt,
            "pe": pe,
            "description": desc,
        }
        _cache_set(cache_key, result)
        _breaker_on_success()
        return result
    except Exception as e:
        logger.debug(f"[yf_session] ticker_info({symbol}) failed: {e}")
        _breaker_on_failure()
        return None


def get_ticker_news(symbol: str, limit: int = 5) -> list[dict[str, Any]]:
    """News for the given ticker. Uses yfinance Ticker.news; fallback DDGS news search."""
    cache_key = f"news:{symbol}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if _breaker_should_short_circuit():
        return []

    out: list[dict[str, Any]] = []
    try:
        acquire()
        t = yf.Ticker(symbol, session=_SHARED_SESSION)
        raw = getattr(t, "news", None)
        if callable(raw):
            raw = raw()
        if isinstance(raw, list) and raw:
            for item in raw[:limit]:
                if not isinstance(item, dict):
                    continue
                # New yfinance format: item has id + content; content has title, provider, canonicalUrl, pubDate
                content = item.get("content") or item
                if isinstance(content, dict):
                    title = content.get("title") or item.get("title") or ""
                    url = ""
                    curl = content.get("canonicalUrl") or content.get("clickThroughUrl")
                    if isinstance(curl, dict) and curl.get("url"):
                        url = curl["url"]
                    else:
                        url = content.get("link") or content.get("url") or item.get("link") or item.get("url") or ""
                    prov = content.get("provider") or {}
                    pub = prov.get("displayName", "") if isinstance(prov, dict) else (content.get("publisher") or item.get("publisher") or "")
                    pub_date = content.get("pubDate") or content.get("displayTime") or ""
                    if pub_date and "T" in str(pub_date):
                        try:
                            from datetime import datetime
                            dt = datetime.fromisoformat(str(pub_date).replace("Z", "+00:00"))
                            date_str = dt.strftime("%Y-%m-%d %H:%M")
                        except Exception:
                            date_str = str(pub_date)[:16]
                    else:
                        ts = content.get("providerPublishTime") or item.get("providerPublishTime") or 0
                        if isinstance(ts, (int, float)) and ts:
                            from datetime import datetime
                            date_str = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                        else:
                            date_str = ""
                    out.append({"title": title, "url": url, "publisher": pub, "date": date_str})
            _breaker_on_success()
        else:
            # No news returned -- treat as a soft failure for breaker
            # purposes. The DDG fallback below still runs regardless.
            _breaker_on_failure()
    except Exception as e:
        logger.debug(f"[yf_session] ticker news({symbol}) failed: {e}")
        _breaker_on_failure()

    if not out:
        try:
            from .web_search import news_search
            query = f"{symbol} stock news" if not symbol.upper().endswith("-USD") else f"{symbol.replace('-USD', '')} cryptocurrency news"
            out = news_search(query, max_results=limit, trace_id="ticker_news")
        except Exception as e:
            logger.debug(f"[yf_session] DDG news fallback failed: {e}")

    try:
        from .trading.sentiment import score_news_sentiment
        for item in out:
            s = score_news_sentiment(item.get("title", ""))
            item["sentiment"] = s["label"]
            item["sentiment_score"] = s["score"]
    except Exception as e:
        logger.debug(f"[yf_session] sentiment scoring failed: {e}")

    _cache_set(cache_key, out)
    return out
