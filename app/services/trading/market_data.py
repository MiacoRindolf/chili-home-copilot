"""Market data: OHLCV, quotes, search, and technical indicators.

Data provider hierarchy:
  1. Massive.com  – primary real-time provider (REST + optional WebSocket)
  2. Polygon.io   – secondary fallback (if configured)
  3. yfinance      – free fallback for stocks/indices/crypto
"""
from __future__ import annotations

import logging
import threading
import time as _time
from typing import Any

import numpy as np
import pandas as pd
from ...config import settings
from ..yf_session import (
    get_ticker as _yf_ticker,
    get_history as _yf_history,
    get_fast_info as _yf_fast_info,
    acquire as _yf_acquire,
)

logger = logging.getLogger(__name__)

def _log_ohlcv_outcome(
    ticker: str,
    interval: str,
    *,
    provider: str,
    reason: str,
    row_count: int | None = None,
) -> None:
    rc = row_count if row_count is not None else "n/a"
    logger.debug(
        "[market_data_ohlcv] ticker=%s interval=%s provider=%s reason=%s row_count=%s",
        ticker,
        interval,
        provider,
        reason,
        rc,
    )



# --- Massive (primary) ---
_massive_available = False
try:
    from .. import massive_client as _massive
    _massive_available = True
except ImportError:
    _massive = None  # type: ignore[assignment]

# --- Polygon (secondary) ---
_polygon_available = False
try:
    from .. import polygon_client as _poly
    _polygon_available = True
except ImportError:
    _poly = None  # type: ignore[assignment]


def _use_massive() -> bool:
    """Check if Massive is available and configured."""
    return _massive_available and bool(settings.massive_api_key)


def _use_polygon() -> bool:
    """Check if Polygon is enabled and configured (secondary fallback)."""
    return (
        _polygon_available
        and settings.use_polygon
        and bool(settings.polygon_api_key)
    )


def _effective_allow_fallback(allow_provider_fallback: bool | None) -> bool:
    """None → ``settings.market_data_allow_provider_fallback``; explicit bool wins."""
    if allow_provider_fallback is not None:
        return allow_provider_fallback
    return settings.market_data_allow_provider_fallback


def smart_round(value: float | None, fallback: int = 2, *, crypto: bool = False) -> float | None:
    """Round a price to an appropriate number of decimals based on magnitude.

    For regular assets:
        >= $1000    -> 2 decimals   (45231.89)
        >= $1       -> 2 decimals   (12.34)
        >= $0.01    -> 4 decimals   (0.0543)
        >= $0.0001  -> 6 decimals   (0.000123)
        < $0.0001   -> 8 decimals   (0.00000012)

    For crypto (crypto=True), precision is increased so that
    stablecoins near $1 (e.g. USDF-USD, USDD-USD) show enough
    decimals to distinguish entry/stop/target:
        >= $100     -> 2 decimals
        >= $1       -> 6 decimals   (1.000234)
        >= $0.01    -> 6 decimals   (0.054321)
        >= $0.0001  -> 8 decimals
        < $0.0001   -> 10 decimals
    """
    if value is None:
        return None
    abs_v = abs(value)
    if crypto:
        if abs_v >= 100:
            d = 2
        elif abs_v >= 1:
            d = 6
        elif abs_v >= 0.01:
            d = 6
        elif abs_v >= 0.0001:
            d = 8
        else:
            d = 10
    else:
        if abs_v >= 1:
            d = 2
        elif abs_v >= 0.01:
            d = 4
        elif abs_v >= 0.0001:
            d = 6
        else:
            d = 8
    return round(value, d)


# ── Interval / period validation ──────────────────────────────────────

_VALID_INTERVALS = {
    "1m", "2m", "5m", "15m", "30m", "60m", "90m",
    "1h", "1d", "5d", "1wk", "1mo", "3mo",
}
_VALID_PERIODS = {
    "1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max",
}

_INTERVAL_MAX_PERIOD: dict[str, list[str]] = {
    "1m": ["1d", "5d"],
    "2m": ["1d", "5d"],
    "5m": ["1d", "5d", "1mo"],
    "15m": ["1d", "5d", "1mo"],
    "30m": ["1d", "5d", "1mo"],
    "1h": ["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y"],
    "60m": ["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y"],
    "90m": ["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y"],
}
_INTERVAL_DEFAULT_PERIOD: dict[str, str] = {
    "1m": "1d", "2m": "5d", "5m": "5d", "15m": "1mo", "30m": "1mo",
    "1h": "3mo", "60m": "3mo", "90m": "3mo",
}


def _clamp_period(interval: str, period: str) -> str:
    """Ensure the requested period is valid for the given interval (yfinance limits)."""
    allowed = _INTERVAL_MAX_PERIOD.get(interval)
    if allowed is None:
        return period
    if period == "max":
        return allowed[-1]
    if period in allowed:
        return period
    return _INTERVAL_DEFAULT_PERIOD.get(interval, allowed[-1])


# ── OHLCV ─────────────────────────────────────────────────────────────

def fetch_ohlcv(
    ticker: str,
    interval: str = "1d",
    period: str = "6mo",
    *,
    start: str | None = None,
    end: str | None = None,
    allow_provider_fallback: bool | None = None,
) -> list[dict[str, Any]]:
    """Fetch OHLCV candle data.  Massive → Polygon → yfinance.

    Either *period* **or** explicit *start*/*end* (YYYY-MM-DD or datetime)
    can be used.  When *start* is given it takes precedence over *period*.

    *allow_provider_fallback* ``None`` uses ``settings.market_data_allow_provider_fallback``.
    ``False`` forces Massive-only (trading chart routes use this).
    """
    fb = _effective_allow_fallback(allow_provider_fallback)
    if interval not in _VALID_INTERVALS:
        interval = "1d"
    if not start and period not in _VALID_PERIODS:
        period = "6mo"
    if not start:
        period = _clamp_period(interval, period)

    _start_str = str(start)[:10] if start else None
    _end_str = str(end)[:10] if end else None

    # --- Massive path (primary) ---
    _massive_dead = False
    if _use_massive():
        try:
            bars = _massive.get_aggregates(
                ticker, interval=interval, period=period,
                start=_start_str, end=_end_str,
            )
            if bars:
                _log_ohlcv_outcome(
                    ticker, interval, provider="massive", reason="ok", row_count=len(bars),
                )
                return bars
            if _massive.massive_aggregate_variants_all_dead(ticker):
                _massive_dead = True
            if not _massive_dead:
                logger.debug(f"[market_data] Massive returned empty for {ticker}, falling back")
                _log_ohlcv_outcome(
                    ticker, interval, provider="massive", reason="empty_try_fallback", row_count=0,
                )
        except Exception as e:
            logger.warning(f"[market_data] Massive OHLCV failed for {ticker}: {e}")
            _log_ohlcv_outcome(
                ticker, interval, provider="massive", reason="error", row_count=0,
            )

    if _massive_dead:
        _log_ohlcv_outcome(
            ticker, interval, provider="massive", reason="all_variants_dead", row_count=0,
        )
        return []

    if not fb:
        _log_ohlcv_outcome(
            ticker, interval, provider="none", reason="fallback_disabled", row_count=0,
        )
        return []

    # --- Polygon path (secondary) — still try for *-USD when Massive returned empty ---
    if _use_polygon():
        try:
            bars = _poly.get_aggregates(
                ticker, interval=interval, period=period,
                start=_start_str, end=_end_str,
            )
            if bars:
                _log_ohlcv_outcome(
                    ticker, interval, provider="polygon", reason="ok", row_count=len(bars),
                )
                return bars
            logger.debug(f"[market_data] Polygon returned empty for {ticker}, falling back to yfinance")
            _log_ohlcv_outcome(
                ticker, interval, provider="polygon", reason="empty_try_fallback", row_count=0,
            )
        except Exception as e:
            logger.warning(f"[market_data] Polygon OHLCV failed for {ticker}: {e}")
            _log_ohlcv_outcome(
                ticker, interval, provider="polygon", reason="error", row_count=0,
            )

    # --- yfinance fallback (skip crypto: symbols rarely match Massive/Polygon) ---
    _is_crypto = ticker.upper().endswith("-USD")
    if _is_crypto:
        _log_ohlcv_outcome(
            ticker, interval, provider="yfinance", reason="skipped_crypto_list_path", row_count=0,
        )
        return []

    if _start_str:
        df = _yf_history(ticker, start=_start_str, period="15d", interval=interval)
    else:
        period = _clamp_period(interval, period)
        df = _yf_history(ticker, period=period, interval=interval)

    if df.empty:
        _log_ohlcv_outcome(
            ticker, interval, provider="yfinance", reason="empty", row_count=0,
        )
        return []

    records: list[dict[str, Any]] = []
    for ts, row in df.iterrows():
        epoch = int(pd.Timestamp(ts).timestamp())
        records.append({
            "time": epoch,
            "open": round(float(row["Open"]), 4),
            "high": round(float(row["High"]), 4),
            "low": round(float(row["Low"]), 4),
            "close": round(float(row["Close"]), 4),
            "volume": int(row["Volume"]),
        })
    _log_ohlcv_outcome(
        ticker, interval, provider="yfinance", reason="ok", row_count=len(records),
    )
    return records


_ohlcv_df_cache: dict[str, tuple[float, "pd.DataFrame"]] = {}
_ohlcv_df_lock = threading.Lock()
_OHLCV_DF_TTL = 600  # 10 min
_OHLCV_DF_MAX = 300


def clear_ohlcv_cache() -> None:
    """Drop the in-memory OHLCV DataFrame cache."""
    with _ohlcv_df_lock:
        _ohlcv_df_cache.clear()


def invalidate_ohlcv_cache_for_ticker(ticker: str) -> int:
    """Remove all cached OHLCV entries for *ticker*. Returns count removed."""
    prefix = f"{ticker.upper()}|"
    with _ohlcv_df_lock:
        keys = [k for k in _ohlcv_df_cache if k.startswith(prefix)]
        for k in keys:
            del _ohlcv_df_cache[k]
        return len(keys)


def fetch_ohlcv_df(
    ticker: str,
    interval: str = "1d",
    period: str = "6mo",
    *,
    start: str | None = None,
    end: str | None = None,
    allow_provider_fallback: bool | None = None,
) -> pd.DataFrame:
    """Fetch OHLCV as a pandas DataFrame (Open/High/Low/Close/Volume columns).

    Provider order: Massive → Polygon → yfinance.
    This is the preferred entry-point for indicator computation and scanner scoring.

    Either *period* **or** explicit *start*/*end* (YYYY-MM-DD or datetime)
    can be used.  When *start* is given it takes precedence over *period*.

    *allow_provider_fallback* ``None`` uses ``settings.market_data_allow_provider_fallback``.
    """
    fb = _effective_allow_fallback(allow_provider_fallback)
    if interval not in _VALID_INTERVALS:
        interval = "1d"
    if not start and period not in _VALID_PERIODS:
        period = "6mo"
    if not start:
        period = _clamp_period(interval, period)

    _cache_key = f"{ticker}|{interval}|{period}|{start}|{end}"
    _now = _time.time()
    with _ohlcv_df_lock:
        _hit = _ohlcv_df_cache.get(_cache_key)
        if _hit and _now - _hit[0] < _OHLCV_DF_TTL:
            return _hit[1].copy()

    _start_str = str(start)[:10] if start else None
    _end_str = str(end)[:10] if end else None

    def _store_and_return(result: pd.DataFrame) -> pd.DataFrame:
        if not result.empty:
            with _ohlcv_df_lock:
                if len(_ohlcv_df_cache) >= _OHLCV_DF_MAX:
                    _ohlcv_df_cache.clear()
                _ohlcv_df_cache[_cache_key] = (_time.time(), result)
        return result

    # --- Massive path (primary) ---
    _massive_dead = False
    if _use_massive():
        try:
            df = _massive.get_aggregates_df(
                ticker, interval=interval, period=period,
                start=_start_str, end=_end_str,
            )
            if not df.empty:
                _log_ohlcv_outcome(
                    ticker, interval, provider="massive", reason="ok", row_count=len(df),
                )
                return _store_and_return(df)
            if _massive.massive_aggregate_variants_all_dead(ticker):
                _massive_dead = True
            else:
                _log_ohlcv_outcome(
                    ticker, interval, provider="massive", reason="empty_try_fallback", row_count=0,
                )
        except Exception as e:
            logger.warning(f"[market_data] Massive DF failed for {ticker}: {e}")
            _log_ohlcv_outcome(
                ticker, interval, provider="massive", reason="error", row_count=0,
            )

    if _massive_dead:
        _log_ohlcv_outcome(
            ticker, interval, provider="massive", reason="all_variants_dead", row_count=0,
        )
        return pd.DataFrame()

    if not fb:
        _log_ohlcv_outcome(
            ticker, interval, provider="none", reason="fallback_disabled", row_count=0,
        )
        return pd.DataFrame()

    # --- Polygon path (secondary) — still try for *-USD when Massive returned empty ---
    if _use_polygon():
        try:
            df = _poly.get_aggregates_df(
                ticker, interval=interval, period=period,
                start=_start_str, end=_end_str,
            )
            if not df.empty:
                _log_ohlcv_outcome(
                    ticker, interval, provider="polygon", reason="ok", row_count=len(df),
                )
                return _store_and_return(df)
            _log_ohlcv_outcome(
                ticker, interval, provider="polygon", reason="empty_try_fallback", row_count=0,
            )
        except Exception as e:
            logger.warning(f"[market_data] Polygon DF failed for {ticker}: {e}")
            _log_ohlcv_outcome(
                ticker, interval, provider="polygon", reason="error", row_count=0,
            )

    # --- yfinance fallback (skip crypto: symbols rarely match Massive/Polygon) ---
    _is_crypto = ticker.upper().endswith("-USD")
    if _is_crypto:
        _log_ohlcv_outcome(
            ticker, interval, provider="yfinance", reason="skipped_crypto_df_path", row_count=0,
        )
        return pd.DataFrame()

    if _start_str:
        df = _yf_history(ticker, start=_start_str, period="15d", interval=interval)
    else:
        period = _clamp_period(interval, period)
        df = _yf_history(ticker, period=period, interval=interval)
    _df = df if df is not None else pd.DataFrame()
    if _df.empty:
        _log_ohlcv_outcome(
            ticker, interval, provider="yfinance", reason="empty", row_count=0,
        )
    else:
        _log_ohlcv_outcome(
            ticker, interval, provider="yfinance", reason="ok", row_count=len(_df),
        )
    return _store_and_return(_df)


def fetch_ohlcv_batch(
    tickers: list[str],
    interval: str = "1d",
    period: str = "6mo",
    *,
    allow_provider_fallback: bool | None = None,
) -> dict[str, pd.DataFrame]:
    """Fetch OHLCV DataFrames for many tickers in parallel.

    Uses Massive batch (concurrent thread pool) when available, otherwise
    Polygon then yfinance ``batch_download`` + per-ticker history (if fallback allowed).
    """
    fb = _effective_allow_fallback(allow_provider_fallback)
    results: dict[str, pd.DataFrame] = {}

    if _use_massive():
        try:
            dfs = _massive.get_aggregates_df_batch(
                tickers, interval=interval, period=period,
            )
            results.update(dfs)
        except Exception as e:
            logger.warning(f"[market_data] Massive batch OHLCV failed: {e}")

        missing = [
            t for t in tickers
            if t not in results
            and not _massive.massive_aggregate_variants_all_dead(t)
        ]
        if not missing:
            return results
        tickers = missing

    if not fb:
        return results

    if _use_polygon():
        from concurrent.futures import ThreadPoolExecutor
        def _poly_one(t):
            try:
                df = _poly.get_aggregates_df(t, interval=interval, period=period)
                return t, df
            except Exception:
                return t, pd.DataFrame()
        with ThreadPoolExecutor(max_workers=48) as pool:
            for t, df in pool.map(_poly_one, tickers):
                if not df.empty:
                    results[t] = df
        missing = [t for t in tickers if t not in results]
        if not missing:
            return results
        tickers = missing

    from ..yf_session import batch_download
    batch_download(tickers, period=period, interval=interval)
    for t in tickers:
        if t in results:
            continue
        p = _clamp_period(interval, period)
        df = _yf_history(t, period=p, interval=interval)
        if df is not None and not df.empty:
            results[t] = df

    return results


# ── Quote ──────────────────────────────────────────────────────────────

def fetch_quote(ticker: str, *, allow_provider_fallback: bool | None = None) -> dict[str, Any] | None:
    """Current price + enriched info.  Massive WS → Massive REST → Polygon → yfinance.

    *allow_provider_fallback* ``None`` uses ``settings.market_data_allow_provider_fallback``.
    ``False`` forces Massive-only (WS + REST).
    """
    fb = _effective_allow_fallback(allow_provider_fallback)
    fi: dict[str, Any] | None = None

    # --- Massive WebSocket cache (fastest path) ---
    if _use_massive():
        try:
            ws_snap = _massive.get_ws_quote(ticker)
            if ws_snap and ws_snap.price:
                fi = {
                    "last_price": ws_snap.price,
                    "previous_close": None,
                    "bid": ws_snap.bid,
                    "ask": ws_snap.ask,
                }
                return _build_quote_result(ticker, fi)
        except Exception:
            pass

    # --- Massive REST (primary) ---
    _massive_dead = False
    if _use_massive():
        try:
            fi = _massive.get_last_quote(ticker)
            if fi and fi.get("last_price") is not None:
                return _build_quote_result(ticker, fi)
            if _massive.massive_aggregate_variants_all_dead(ticker):
                _massive_dead = True
            if not _massive_dead:
                logger.debug(f"[market_data] Massive quote empty for {ticker}, falling back")
            fi = None
        except Exception as e:
            logger.warning(f"[market_data] Massive quote failed for {ticker}: {e}")

    if _massive_dead:
        return None

    if not fb:
        return None

    # --- Polygon path (secondary) ---
    if _use_polygon():
        try:
            fi = _poly.get_last_quote(ticker)
            if fi and fi.get("last_price") is not None:
                return _build_quote_result(ticker, fi)
            logger.debug(f"[market_data] Polygon quote empty for {ticker}, falling back")
            fi = None
        except Exception as e:
            logger.warning(f"[market_data] Polygon quote failed for {ticker}: {e}")

    # --- yfinance / CoinGecko fallback ---
    fi = _yf_fast_info(ticker)
    if fi is None or fi.get("last_price") is None:
        return None
    return _build_quote_result(ticker, fi)


def _build_quote_result(ticker: str, fi: dict[str, Any]) -> dict[str, Any] | None:
    """Assemble a standardised quote dict from raw provider data."""
    price = fi.get("last_price")
    if price is None:
        return None
    prev = fi.get("previous_close")
    _cr = ticker.upper().endswith("-USD")
    result: dict[str, Any] = {
        "ticker": ticker.upper(),
        "price": smart_round(price, crypto=_cr),
        "previous_close": smart_round(prev, crypto=_cr) if prev else None,
        "change": smart_round(price - prev, crypto=_cr) if prev else None,
        "change_pct": round((price - prev) / prev * 100, 2) if prev else None,
        "market_cap": int(fi["market_cap"]) if fi.get("market_cap") else None,
        "currency": "USD",
    }
    if fi.get("day_high"):
        result["day_high"] = smart_round(fi["day_high"], crypto=_cr)
    if fi.get("day_low"):
        result["day_low"] = smart_round(fi["day_low"], crypto=_cr)
    if fi.get("volume"):
        result["volume"] = fi["volume"]
    if fi.get("year_high"):
        result["year_high"] = smart_round(fi["year_high"], crypto=_cr)
    if fi.get("year_low"):
        result["year_low"] = smart_round(fi["year_low"], crypto=_cr)
    if fi.get("avg_volume"):
        result["avg_volume"] = fi["avg_volume"]
    return result


def fetch_quotes_batch(
    tickers: list[str], *, allow_provider_fallback: bool | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch quotes for multiple tickers.  Massive → Polygon → yfinance.

    *allow_provider_fallback* ``None`` uses ``settings.market_data_allow_provider_fallback``.
    """
    fb = _effective_allow_fallback(allow_provider_fallback)
    results: dict[str, dict[str, Any]] = {}

    # --- Massive path (primary) ---
    if _use_massive():
        try:
            raw = _massive.get_quotes_batch(tickers)
            for sym, fi in raw.items():
                if fi and fi.get("last_price") is not None:
                    q = _build_quote_result(sym, fi)
                    if q:
                        results[sym] = q
        except Exception as e:
            logger.warning(f"[market_data] Massive batch quotes failed: {e}")

        missing = [t for t in tickers if t not in results]
        if not missing:
            return results
        tickers = missing

    if not fb:
        return results

    # --- Polygon path (secondary) ---
    if _use_polygon():
        try:
            raw = _poly.get_quotes_batch(tickers)
            for sym, fi in raw.items():
                if fi and fi.get("last_price") is not None:
                    q = _build_quote_result(sym, fi)
                    if q:
                        results[sym] = q
        except Exception as e:
            logger.warning(f"[market_data] Polygon batch quotes failed: {e}")

        missing = [t for t in tickers if t not in results]
        if not missing:
            return results
        logger.debug(f"[market_data] {len(missing)} tickers missing from Polygon, trying yfinance")
        tickers = missing

    # --- yfinance fallback ---
    from ..yf_session import batch_download
    batch_download(tickers, period="3mo", interval="1d")
    for t in tickers:
        if t in results:
            continue
        q = _yf_fast_info(t)
        if q and q.get("last_price") is not None:
            built = _build_quote_result(t, q)
            if built:
                results[t] = built
    return results


# ── Ticker search ─────────────────────────────────────────────────────

def search_tickers(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Search for tickers matching a query string."""
    try:
        import yfinance as _yf_mod
        _yf_acquire()
        results = _yf_mod.search(query, max_results=limit)
        quotes = results.get("quotes", []) if isinstance(results, dict) else []
        return [
            {
                "ticker": q.get("symbol", ""),
                "name": q.get("shortname") or q.get("longname", ""),
                "exchange": q.get("exchange", ""),
                "type": q.get("quoteType", ""),
            }
            for q in quotes
            if q.get("symbol")
        ]
    except Exception:
        return []


# ── Technical indicators ──────────────────────────────────────────────

_ind_cache: dict[tuple, tuple[float, dict]] = {}
_ind_cache_lock = threading.Lock()
_IND_CACHE_TTL = 1800  # 30 min — learning cycles reuse; 64 GB RAM keeps longer
_IND_CACHE_MAX = 5000  # 64 GB RAM — cache indicators for full universe without thrashing


def compute_indicators(
    ticker: str,
    interval: str = "1d",
    period: str = "6mo",
    indicators: list[str] | None = None,
    *,
    allow_provider_fallback: bool | None = None,
) -> dict[str, Any]:
    """Compute requested technical indicators for a ticker.

    Uses the ``ta`` library (technical-analysis).  Returns a dict keyed by
    indicator name, each value a list of {time, value} or multi-key dicts.

    Results are cached for 5 minutes keyed on (ticker, interval, period,
    indicators) since the underlying OHLCV data is itself cached for 30 min.
    """
    fb = _effective_allow_fallback(allow_provider_fallback)
    if indicators is None:
        indicators = ["rsi", "macd", "sma_20", "ema_20", "bbands"]
    period = _clamp_period(interval, period)
    cache_key = (ticker.upper(), interval, period, frozenset(indicators), fb)

    now = _time.time()
    with _ind_cache_lock:
        entry = _ind_cache.get(cache_key)
        if entry and now - entry[0] < _IND_CACHE_TTL:
            return entry[1]

    result = _compute_indicators_fresh(
        ticker, interval, period, indicators, allow_provider_fallback=fb,
    )

    with _ind_cache_lock:
        if len(_ind_cache) >= _IND_CACHE_MAX:
            cutoff = now - _IND_CACHE_TTL
            stale = [k for k, v in _ind_cache.items() if v[0] < cutoff]
            for k in stale:
                del _ind_cache[k]
        _ind_cache[cache_key] = (now, result)
    return result


def _compute_indicators_fresh(
    ticker: str,
    interval: str,
    period: str,
    indicators: list[str],
    *,
    allow_provider_fallback: bool = True,
) -> dict[str, Any]:
    """Actual indicator computation (no cache).

    Uses fetch_ohlcv_df() with the same provider fallback policy.
    """
    df = fetch_ohlcv_df(
        ticker, interval=interval, period=period, allow_provider_fallback=allow_provider_fallback,
    )
    if df.empty:
        return {}

    df.index = pd.to_datetime(df.index)
    timestamps = [int(pd.Timestamp(ts).timestamp()) for ts in df.index]
    result: dict[str, Any] = {}

    for ind in indicators:
        ind_lower = ind.lower().strip()
        try:
            data = _compute_single_indicator(df, timestamps, ind_lower)
            if data is not None:
                result[ind_lower] = data
        except Exception:
            continue

    return result


def _compute_single_indicator(
    df: pd.DataFrame, timestamps: list[int], name: str,
) -> list[dict] | None:
    """Compute one indicator using the ``ta`` library."""
    from ta.momentum import (
        RSIIndicator,
        ROCIndicator,
        StochRSIIndicator,
        StochasticOscillator,
        WilliamsRIndicator,
    )
    from ta.trend import MACD, SMAIndicator, EMAIndicator, ADXIndicator, PSARIndicator, CCIIndicator, IchimokuIndicator
    from ta.volatility import BollingerBands, AverageTrueRange
    from ta.volume import OnBalanceVolumeIndicator, MFIIndicator, VolumeWeightedAveragePrice

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]

    if name == "rsi" or name.startswith("rsi_"):
        period = int(name.split("_")[1]) if "_" in name else 14
        s = RSIIndicator(close=close, window=period).rsi()
        return _series_to_records(timestamps, s, "value")

    if name == "macd":
        m = MACD(close=close)
        macd_line = m.macd()
        signal_line = m.macd_signal()
        histogram = m.macd_diff()
        out = []
        for i, ts in enumerate(timestamps):
            rec: dict[str, Any] = {"time": ts}
            has = False
            if pd.notna(macd_line.iloc[i]):
                rec["macd"] = round(float(macd_line.iloc[i]), 4)
                has = True
            if pd.notna(signal_line.iloc[i]):
                rec["signal"] = round(float(signal_line.iloc[i]), 4)
                has = True
            if pd.notna(histogram.iloc[i]):
                rec["histogram"] = round(float(histogram.iloc[i]), 4)
                has = True
            if has:
                out.append(rec)
        return out

    if name.startswith("sma"):
        period = int(name.split("_")[1]) if "_" in name else 20
        s = SMAIndicator(close=close, window=period).sma_indicator()
        return _series_to_records(timestamps, s, "value")

    if name.startswith("ema"):
        period = int(name.split("_")[1]) if "_" in name else 20
        s = EMAIndicator(close=close, window=period).ema_indicator()
        return _series_to_records(timestamps, s, "value")

    if name in ("bbands", "bb", "bollinger"):
        bb = BollingerBands(close=close, window=20, window_dev=2)
        upper = bb.bollinger_hband()
        middle = bb.bollinger_mavg()
        lower = bb.bollinger_lband()
        out = []
        for i, ts in enumerate(timestamps):
            rec: dict[str, Any] = {"time": ts}
            has = False
            if pd.notna(upper.iloc[i]):
                rec["upper"] = round(float(upper.iloc[i]), 4)
                has = True
            if pd.notna(middle.iloc[i]):
                rec["middle"] = round(float(middle.iloc[i]), 4)
                has = True
            if pd.notna(lower.iloc[i]):
                rec["lower"] = round(float(lower.iloc[i]), 4)
                has = True
            if has:
                out.append(rec)
        return out

    if name in ("stoch", "stochastic"):
        st = StochasticOscillator(high=high, low=low, close=close)
        k = st.stoch()
        d = st.stoch_signal()
        out = []
        for i, ts in enumerate(timestamps):
            rec: dict[str, Any] = {"time": ts}
            has = False
            if pd.notna(k.iloc[i]):
                rec["k"] = round(float(k.iloc[i]), 4)
                has = True
            if pd.notna(d.iloc[i]):
                rec["d"] = round(float(d.iloc[i]), 4)
                has = True
            if has:
                out.append(rec)
        return out

    if name == "adx":
        a = ADXIndicator(high=high, low=low, close=close)
        adx_val = a.adx()
        dmp = a.adx_pos()
        dmn = a.adx_neg()
        out = []
        for i, ts in enumerate(timestamps):
            rec: dict[str, Any] = {"time": ts}
            has = False
            if pd.notna(adx_val.iloc[i]):
                rec["adx"] = round(float(adx_val.iloc[i]), 4)
                has = True
            if pd.notna(dmp.iloc[i]):
                rec["dmp"] = round(float(dmp.iloc[i]), 4)
                has = True
            if pd.notna(dmn.iloc[i]):
                rec["dmn"] = round(float(dmn.iloc[i]), 4)
                has = True
            if has:
                out.append(rec)
        return out

    if name == "atr":
        s = AverageTrueRange(high=high, low=low, close=close).average_true_range()
        return _series_to_records(timestamps, s, "value")

    if name == "cci":
        s = CCIIndicator(high=high, low=low, close=close).cci()
        return _series_to_records(timestamps, s, "value")

    if name in ("willr", "williams"):
        s = WilliamsRIndicator(high=high, low=low, close=close).williams_r()
        return _series_to_records(timestamps, s, "value")

    if name == "obv":
        s = OnBalanceVolumeIndicator(close=close, volume=volume).on_balance_volume()
        return _series_to_records(timestamps, s, "value")

    if name == "mfi":
        s = MFIIndicator(high=high, low=low, close=close, volume=volume).money_flow_index()
        return _series_to_records(timestamps, s, "value")

    if name == "vwap":
        s = VolumeWeightedAveragePrice(high=high, low=low, close=close, volume=volume).volume_weighted_average_price()
        return _series_to_records(timestamps, s, "value")

    if name in ("psar", "sar"):
        p = PSARIndicator(high=high, low=low, close=close)
        psar_up = p.psar_up()
        psar_down = p.psar_down()
        out = []
        for i, ts in enumerate(timestamps):
            rec: dict[str, Any] = {"time": ts}
            has = False
            if pd.notna(psar_up.iloc[i]):
                rec["long"] = round(float(psar_up.iloc[i]), 4)
                has = True
            if pd.notna(psar_down.iloc[i]):
                rec["short"] = round(float(psar_down.iloc[i]), 4)
                has = True
            if has:
                out.append(rec)
        return out

    if name == "ichimoku":
        ich = IchimokuIndicator(high=high, low=low, window1=9, window2=26, window3=52)
        tenkan = ich.ichimoku_conversion_line()
        kijun = ich.ichimoku_base_line()
        senkou_a = ich.ichimoku_a()
        senkou_b = ich.ichimoku_b()
        chikou = close.shift(26)
        out = []
        for i, ts in enumerate(timestamps):
            rec: dict[str, Any] = {"time": ts}
            has = False
            if pd.notna(tenkan.iloc[i]):
                rec["tenkan"] = round(float(tenkan.iloc[i]), 4)
                has = True
            if pd.notna(kijun.iloc[i]):
                rec["kijun"] = round(float(kijun.iloc[i]), 4)
                has = True
            if pd.notna(senkou_a.iloc[i]):
                rec["senkou_a"] = round(float(senkou_a.iloc[i]), 4)
                has = True
            if pd.notna(senkou_b.iloc[i]):
                rec["senkou_b"] = round(float(senkou_b.iloc[i]), 4)
                has = True
            if pd.notna(chikou.iloc[i]):
                rec["chikou"] = round(float(chikou.iloc[i]), 4)
                has = True
            if has:
                out.append(rec)
        return out

    if name == "supertrend":
        atr_period = 10
        multiplier = 3
        atr = AverageTrueRange(high=high, low=low, close=close, window=atr_period).average_true_range()
        hl2 = (high + low) / 2
        upper_band = hl2 + multiplier * atr
        lower_band = hl2 - multiplier * atr
        out = []
        for i, ts in enumerate(timestamps):
            if i == 0:
                continue
            prev_upper = upper_band.iloc[i - 1]
            c = close.iloc[i]
            u = upper_band.iloc[i]
            l_ = lower_band.iloc[i]
            if pd.isna(prev_upper) or pd.isna(c) or pd.isna(u) or pd.isna(l_):
                continue
            if c > prev_upper:
                trend = 1
                st = l_
            else:
                trend = -1
                st = u
            rec: dict[str, Any] = {"time": ts, "value": round(float(st), 4), "trend": trend}
            out.append(rec)
        return out

    if name in ("pivot", "pivots"):
        out = []
        for i, ts in enumerate(timestamps):
            if i == 0:
                continue
            h = float(high.iloc[i - 1])
            l_ = float(low.iloc[i - 1])
            c = float(close.iloc[i - 1])
            p = (h + l_ + c) / 3
            s1 = 2 * p - h
            r1 = 2 * p - l_
            s2 = p - (h - l_)
            r2 = p + (h - l_)
            s3 = l_ - 2 * (h - p)
            r3 = h + 2 * (p - l_)
            rec: dict[str, Any] = {
                "time": ts,
                "pivot": round(p, 4),
                "r1": round(r1, 4),
                "r2": round(r2, 4),
                "r3": round(r3, 4),
                "s1": round(s1, 4),
                "s2": round(s2, 4),
                "s3": round(s3, 4),
            }
            out.append(rec)
        return out

    if name in ("vol_profile", "volume_profile"):
        n_bins = 30
        price_min = float(low.min())
        price_max = float(high.max())
        if price_min == price_max:
            return None
        bin_size = (price_max - price_min) / n_bins
        bins = [0.0] * n_bins
        for i in range(len(df)):
            bar_vol = float(volume.iloc[i]) if pd.notna(volume.iloc[i]) else 0
            bar_low = float(low.iloc[i])
            bar_high = float(high.iloc[i])
            for b in range(n_bins):
                lvl_lo = price_min + b * bin_size
                lvl_hi = lvl_lo + bin_size
                if bar_low <= lvl_hi and bar_high >= lvl_lo:
                    overlap = (min(bar_high, lvl_hi) - max(bar_low, lvl_lo)) / max(bar_high - bar_low, 1e-10)
                    bins[b] += bar_vol * overlap
        max_vol = max(bins) if bins else 1
        out = []
        for b in range(n_bins):
            lvl = price_min + b * bin_size + bin_size / 2
            out.append({"price": round(lvl, 4), "volume": round(bins[b], 2), "pct": round(bins[b] / max_vol, 4) if max_vol > 0 else 0})
        return out

    if name == "roc_10":
        s = ROCIndicator(close=close, window=10).roc()
        return _series_to_records(timestamps, s, "value")

    if name == "realized_vol_20":
        lr = np.log(close.astype(float) / close.astype(float).shift(1))
        ann = lr.rolling(20).std() * np.sqrt(252)
        return _series_to_records(timestamps, ann, "value")

    if name == "volume_z_20":
        vm = volume.rolling(20).mean()
        vs = volume.rolling(20).std()
        z = (volume.astype(float) - vm) / vs.replace(0, np.nan)
        return _series_to_records(timestamps, z, "value")

    if name == "volume_z_60":
        if len(volume) < 61:
            return None
        vm = volume.rolling(60).mean()
        vs = volume.rolling(60).std()
        z = (volume.astype(float) - vm) / vs.replace(0, np.nan)
        return _series_to_records(timestamps, z, "value")

    if name == "obv_slope_5":
        obv_ser = OnBalanceVolumeIndicator(close=close, volume=volume).on_balance_volume()
        slope = obv_ser.diff(5) / 5.0
        return _series_to_records(timestamps, slope, "value")

    if name == "atr_percentile_60":
        atr_ser = AverageTrueRange(high=high, low=low, close=close).average_true_range()
        out = []
        for i, ts in enumerate(timestamps):
            if i < 60:
                continue
            w = atr_ser.iloc[i - 59:i + 1].dropna()
            if len(w) < 30:
                continue
            last = atr_ser.iloc[i]
            if pd.isna(last):
                continue
            pct = float((w < last).sum()) / float(len(w))
            out.append({"time": ts, "value": round(pct, 4)})
        return out or None

    if name == "macd_hist_slope_3":
        m = MACD(close=close)
        hist = m.macd_diff()
        slope = (hist - hist.shift(3)) / 3.0
        return _series_to_records(timestamps, slope, "value")

    if name in ("bb_pct_b", "bb_percent_b"):
        bb = BollingerBands(close=close, window=20, window_dev=2)
        u = bb.bollinger_hband()
        l_ = bb.bollinger_lband()
        denom = (u - l_).replace(0, np.nan)
        pctb = (close.astype(float) - l_) / denom
        return _series_to_records(timestamps, pctb, "value")

    return None


def _series_to_records(timestamps: list[int], s: pd.Series, key: str) -> list[dict]:
    out = []
    for ts, val in zip(timestamps, s):
        if pd.notna(val):
            out.append({"time": ts, key: round(float(val), 4)})
    return out


def _snapshot_learned_v1_block(df: pd.DataFrame) -> dict[str, Any]:
    """Versioned OHLC-derived features for mining / meta-learning (no DB)."""
    out: dict[str, Any] = {"schema_version": 1}
    try:
        close = df["Close"].astype(float)
        if len(close) < 25:
            return out
        rets = np.log(close / close.shift(1)).dropna()
        tail = rets.iloc[-20:]
        if len(tail) > 5:
            out["return_skew_20"] = round(float(tail.skew()), 4)
            out["return_kurt_20"] = round(float(tail.kurt()), 4)
            vov = tail.abs().rolling(5).std().iloc[-1]
            if pd.notna(vov):
                out["vol_of_abs_ret_20"] = round(float(vov), 6)
        hi = df["High"].astype(float).iloc[-20:]
        lo = df["Low"].astype(float).iloc[-20:]
        last = float(close.iloc[-1])
        if last > 0:
            out["range_pct_20d"] = round(float((hi.max() - lo.min()) / last * 100.0), 4)
    except Exception:
        return {"schema_version": 1}
    return out


def get_indicator_snapshot(ticker: str, interval: str = "1d") -> dict[str, Any]:
    """Get latest indicator values (used for journal snapshots and AI context).

    Includes stochastic, EMA 20/50/100, extended vol/volume/momentum fields,
    optional ``equity_regime`` (SPY-based), and ``learned_v1`` OHLC stats.
    """
    result = compute_indicators(
        ticker, interval=interval, period="3mo",
        indicators=[
            "rsi", "rsi_7", "macd", "sma_20", "ema_20", "ema_50", "ema_100",
            "bbands", "bb_pct_b", "stoch", "adx", "atr", "obv",
            "roc_10", "realized_vol_20", "volume_z_20", "volume_z_60",
            "obv_slope_5", "atr_percentile_60", "macd_hist_slope_3",
        ],
    )
    snapshot: dict[str, Any] = {"ticker": ticker, "interval": interval}
    for ind_name, records in result.items():
        if records:
            latest = records[-1]
            snapshot[ind_name] = {k: v for k, v in latest.items() if k != "time"}

    try:
        from .learning import _get_historical_regime_map

        today = str(pd.Timestamp.utcnow().date())
        rm = _get_historical_regime_map()
        info = rm.get(today) or {}
        if info:
            snapshot["equity_regime"] = {
                "regime": info.get("regime", "unknown"),
                "spy_mom_5d": info.get("spy_mom_5d"),
                "as_of": today,
            }
    except Exception:
        pass

    if getattr(settings, "brain_snapshot_learned_v1_enabled", True):
        try:
            ldf = fetch_ohlcv_df(ticker, interval=interval, period="3mo")
            if ldf is not None and not ldf.empty:
                snapshot["learned_v1"] = _snapshot_learned_v1_block(ldf)
        except Exception:
            pass

    return snapshot


# ── Ticker lists ──────────────────────────────────────────────────────

DEFAULT_SCAN_TICKERS = [
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO", "ORCL", "CRM",
    "ADBE", "AMD", "INTC", "QCOM", "TXN", "NFLX", "CSCO", "IBM", "NOW", "INTU",
    "AMAT", "LRCX", "MU", "KLAC", "MRVL", "SNPS", "CDNS", "PANW", "CRWD", "FTNT",
    # Cloud / SaaS / AI
    "DDOG", "NET", "SNOW", "PLTR", "SHOP", "SQ", "PYPL", "COIN", "UBER", "ABNB",
    "DASH", "RBLX", "TTD", "PINS", "SNAP", "ROKU", "SPOT", "ZM", "OKTA", "TWLO",
    "MDB", "HUBS", "TEAM", "WDAY", "VEEV", "PATH", "BILL", "ESTC", "MNDY", "TOST",
    # Finance
    "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "C", "AXP", "BLK",
    "SCHW", "CME", "ICE", "COF", "DFS", "ALLY", "HOOD", "SOFI",
    # Healthcare / Pharma
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR", "BMY",
    "AMGN", "GILD", "VRTX", "REGN", "ISRG", "SYK", "MDT", "BSX", "MRNA", "DXCM",
    # Consumer
    "WMT", "COST", "HD", "LOW", "TGT", "PG", "KO", "PEP", "MCD", "SBUX",
    "NKE", "LULU", "TJX", "CMG", "DPZ", "YUM", "EL", "MDLZ", "KMB", "GIS",
    # Industrial / Defense
    "CAT", "DE", "HON", "UPS", "FDX", "BA", "LMT", "RTX", "GE", "EMR",
    "ETN", "ROK", "CMI", "PH", "ITW", "GD", "NOC", "HII", "TDG", "AXON",
    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "VLO", "PSX", "OXY", "HAL",
    "DVN", "FANG", "HES", "BKR", "KMI", "WMB", "LNG", "TRGP",
    # REITs / Telecom / Utilities
    "PLD", "AMT", "CCI", "EQIX", "SPG", "O", "DLR", "DIS", "CMCSA", "T",
    "VZ", "TMUS", "NEE", "DUK", "SO", "D", "AEP", "SRE",
    # Materials
    "LIN", "APD", "SHW", "ECL", "FCX", "NEM", "NUE", "CF", "MOS",
    # ETFs
    "SPY", "QQQ", "IWM", "DIA", "VTI", "ARKK", "XLF", "XLE", "XLK", "XLV",
    # Growth / momentum small/mid
    "SMCI", "ARM", "CELH", "DUOL", "MNST", "ENPH", "FSLR", "DKNG", "BKNG", "EXPE",
    "RIVN", "LCID", "NIO", "XPEV", "LI", "IONQ", "AFRM", "UPST", "CAVA", "BRK-B",
    "PM", "ACN", "MCO", "SPGI",
]

DEFAULT_CRYPTO_TICKERS = [
    "BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "XRP-USD",
    "ADA-USD", "DOGE-USD", "AVAX-USD", "DOT-USD", "LINK-USD",
    "MATIC-USD", "ATOM-USD", "UNI-USD", "LTC-USD", "NEAR-USD",
    "FIL-USD", "ARB-USD", "OP-USD", "ICP-USD", "HBAR-USD",
    "VET-USD", "ALGO-USD", "AAVE-USD", "GRT-USD", "MKR-USD",
    "SNX-USD", "LDO-USD", "FTM-USD", "RUNE-USD", "INJ-USD",
    "SEI-USD", "SHIB-USD", "FET-USD", "STX-USD", "IMX-USD",
    "RENDER-USD", "TRX-USD", "TON-USD", "DYDX-USD", "PENDLE-USD",
]

ALL_SCAN_TICKERS = DEFAULT_SCAN_TICKERS + DEFAULT_CRYPTO_TICKERS


def ticker_display_name(ticker: str) -> str:
    """Strip -USD suffix for crypto display."""
    return ticker.replace("-USD", "") if ticker.endswith("-USD") else ticker


def is_crypto(ticker: str) -> bool:
    return ticker.upper().endswith("-USD")


# ── VIX / Volatility Regime ──────────────────────────────────────────

_vix_cache: dict[str, Any] = {"value": None, "ts": 0}
_VIX_CACHE_TTL = 900  # 15 minutes

def get_vix() -> float | None:
    """Fetch current VIX value with 15-minute caching."""
    import time as _t
    now = _t.time()
    if _vix_cache["value"] is not None and now - _vix_cache["ts"] < _VIX_CACHE_TTL:
        return _vix_cache["value"]

    # VIX is only available via yfinance (not covered by Massive/Polygon)
    try:
        fi = _yf_fast_info("^VIX")
        if fi and fi.get("last_price"):
            val = round(float(fi["last_price"]), 2)
            _vix_cache["value"] = val
            _vix_cache["ts"] = now
            return val
    except Exception:
        pass
    return _vix_cache.get("value")


def get_volatility_regime(vix: float | None = None) -> dict[str, Any]:
    """Classify the current volatility regime from VIX."""
    if vix is None:
        vix = get_vix()
    if vix is None:
        return {"regime": "unknown", "vix": None, "label": "Unknown"}

    if vix < 15:
        regime, label = "low", "Low Volatility"
    elif vix < 20:
        regime, label = "normal", "Normal"
    elif vix < 30:
        regime, label = "elevated", "Elevated"
    else:
        regime, label = "extreme", "Extreme"

    return {"regime": regime, "vix": vix, "label": label}


_market_regime_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_MARKET_REGIME_TTL = 300  # 5 minutes


def get_market_regime() -> dict[str, Any]:
    """Return combined SPY/VIX market regime, cached for 5 minutes.

    Returns:
        dict with keys: spy_direction, spy_momentum_5d, vix, vix_regime,
        regime (risk_on | cautious | risk_off), regime_numeric (1 / 0 / -1)
    """
    import time as _t

    now = _t.time()
    cached = _market_regime_cache
    if cached["data"] is not None and now - cached["ts"] < _MARKET_REGIME_TTL:
        return cached["data"]

    vix_data = get_volatility_regime()
    vix_val = vix_data.get("vix")
    vix_regime = vix_data.get("regime", "unknown")

    spy_direction = "flat"
    spy_momentum_5d = 0.0
    try:
        spy_quote = fetch_quote("SPY")
        if spy_quote:
            chg = spy_quote.get("change_pct", 0.0) or 0.0
            if chg > 0.3:
                spy_direction = "up"
            elif chg < -0.3:
                spy_direction = "down"
    except Exception:
        pass

    try:
        df = fetch_ohlcv_df("SPY", period="1mo", interval="1d")
        if df is not None and len(df) >= 5:
            close = df["Close"]
            spy_momentum_5d = round(
                (float(close.iloc[-1]) - float(close.iloc[-5])) / float(close.iloc[-5]) * 100, 2
            )
    except Exception:
        pass

    if vix_regime in ("low", "normal") and spy_direction != "down":
        regime = "risk_on"
        regime_numeric = 1
    elif vix_regime in ("elevated", "extreme") or spy_direction == "down":
        regime = "risk_off"
        regime_numeric = -1
    else:
        regime = "cautious"
        regime_numeric = 0

    result = {
        "spy_direction": spy_direction,
        "spy_momentum_5d": spy_momentum_5d,
        "vix": vix_val,
        "vix_regime": vix_regime,
        "regime": regime,
        "regime_numeric": regime_numeric,
    }
    _market_regime_cache["data"] = result
    _market_regime_cache["ts"] = now
    return result


# ── BTC Leading Indicator ──────────────────────────────────────────────

_btc_state_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_BTC_STATE_TTL = 300  # 5 minutes


def get_btc_state() -> dict[str, Any]:
    """Return BTC trend/momentum state for use as a crypto leading indicator.

    Returns dict with: btc_price, btc_change_pct, btc_1h_momentum,
    btc_4h_momentum, btc_trend ("up"/"down"/"flat").
    """
    import time as _t

    now = _t.time()
    if _btc_state_cache["data"] is not None and now - _btc_state_cache["ts"] < _BTC_STATE_TTL:
        return _btc_state_cache["data"]

    result: dict[str, Any] = {
        "btc_price": None, "btc_change_pct": 0.0,
        "btc_1h_momentum": 0.0, "btc_4h_momentum": 0.0, "btc_trend": "flat",
    }

    try:
        q = fetch_quote("BTC-USD")
        if q:
            result["btc_price"] = q.get("price")
            result["btc_change_pct"] = q.get("change_pct", 0.0) or 0.0
    except Exception:
        pass

    try:
        df = fetch_ohlcv_df("BTC-USD", period="5d", interval="1h")
        if df is not None and len(df) >= 4:
            c = df["Close"].dropna()
            if len(c) >= 4 and float(c.iloc[-2]) != 0:
                result["btc_1h_momentum"] = round(
                    (float(c.iloc[-1]) - float(c.iloc[-2])) / float(c.iloc[-2]) * 100, 2
                )
            if len(c) >= 5 and float(c.iloc[-4]) != 0:
                result["btc_4h_momentum"] = round(
                    (float(c.iloc[-1]) - float(c.iloc[-4])) / float(c.iloc[-4]) * 100, 2
                )
    except Exception:
        pass

    chg = result["btc_change_pct"] or 0.0
    mom4 = result["btc_4h_momentum"] or 0.0
    if chg > 1 and mom4 > 0.5:
        result["btc_trend"] = "up"
    elif chg < -1 and mom4 < -0.5:
        result["btc_trend"] = "down"
    else:
        result["btc_trend"] = "flat"

    _btc_state_cache["data"] = result
    _btc_state_cache["ts"] = now
    return result


def assess_ohlcv_bar_quality(
    df: pd.DataFrame,
    *,
    max_gap_multiplier: float = 5.0,
) -> dict[str, Any]:
    """Heuristic bar continuity check for miners and backtests.

    Flags large gaps between consecutive bar timestamps vs median delta.
    Does not detect halts or corporate actions — see docs/DATA_SURVIVORSHIP_BIAS.md.
    """
    out: dict[str, Any] = {
        "ok": True,
        "bars": 0,
        "median_delta_s": None,
        "gap_events": 0,
        "max_gap_bars_equiv": 0.0,
        "issues": [],
    }
    if df is None or df.empty or len(df) < 3:
        out["ok"] = False
        out["issues"].append("too_few_bars")
        return out
    try:
        idx = pd.to_datetime(df.index, utc=True, errors="coerce")
        deltas = idx.to_series().diff().dt.total_seconds().dropna()
        deltas = deltas[deltas > 0]
        if deltas.empty:
            out["issues"].append("no_positive_deltas")
            return out
        med = float(deltas.median())
        out["bars"] = int(len(df))
        out["median_delta_s"] = round(med, 3)
        if med <= 0:
            return out
        for i, dt in enumerate(deltas):
            if float(dt) > med * max_gap_multiplier:
                out["gap_events"] += 1
                equiv = float(dt) / med
                out["max_gap_bars_equiv"] = max(out["max_gap_bars_equiv"], equiv)
        if out["gap_events"] > 0:
            out["issues"].append("large_timestamp_gaps")
        from ...config import settings as _cfg

        _max_ev = int(getattr(_cfg, "brain_bar_quality_max_gap_bars", 5))
        if out["gap_events"] > _max_ev:
            out["ok"] = False
    except Exception as ex:
        out["ok"] = False
        out["issues"].append(f"error:{type(ex).__name__}")
    return out
