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
from datetime import datetime
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
from ..symbol_hygiene import clean_equity_universe, normalize_equity_symbol

logger = logging.getLogger(__name__)

_MIN_PROVIDER_WORKERS = 1
_OHLCV_QUALITY_REJECT_LOG_TTL = 300.0
_OHLCV_QUALITY_REJECT_CACHE_ATTR = "quality_rejected"
_ohlcv_quality_reject_log_cache: dict[str, float] = {}
_ohlcv_quality_reject_log_lock = threading.Lock()


def _is_crypto_ticker(ticker: str) -> bool:
    return str(ticker or "").upper().endswith("-USD")


def _is_unsupported_equity_ticker(ticker: str) -> bool:
    return bool(ticker) and not _is_crypto_ticker(ticker) and not normalize_equity_symbol(ticker)


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


def _quality_rejected_df(
    *,
    ticker: str,
    interval: str,
    provider: str,
    integrity: dict[str, Any],
) -> pd.DataFrame:
    out = pd.DataFrame()
    out.attrs["integrity_ok"] = False
    out.attrs[_OHLCV_QUALITY_REJECT_CACHE_ATTR] = True
    out.attrs["provider"] = provider
    out.attrs["ticker"] = ticker.upper()
    out.attrs["interval"] = interval
    out.attrs["quality_issues"] = list(integrity.get("issues") or [])
    out.attrs["fetched_at_utc"] = datetime.utcnow().isoformat() + "Z"
    return out


def _is_quality_rejected_df(df: pd.DataFrame) -> bool:
    return bool(df is not None and df.empty and df.attrs.get(_OHLCV_QUALITY_REJECT_CACHE_ATTR))


def _log_ohlcv_integrity_failure(
    *,
    ticker: str,
    interval: str,
    provider: str,
    integrity: dict[str, Any],
) -> None:
    issues = ",".join(str(x) for x in (integrity.get("issues") or []))
    key = f"{ticker.upper()}|{interval}|{provider}|{issues}"
    now = _time.monotonic()
    with _ohlcv_quality_reject_log_lock:
        last = _ohlcv_quality_reject_log_cache.get(key)
        if last is not None and now - last < _OHLCV_QUALITY_REJECT_LOG_TTL:
            return
        _ohlcv_quality_reject_log_cache[key] = now
    logger.warning(
        "[market_data] OHLCV integrity failed ticker=%s interval=%s provider=%s: %s",
        ticker,
        interval,
        provider,
        integrity,
    )


def _finalize_ohlcv_df(df: pd.DataFrame, *, ticker: str, interval: str, provider: str) -> pd.DataFrame:
    """Clean, validate, and annotate OHLCV data with provenance metadata."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    try:
        from .data_quality import clean_ohlcv, validate_ohlcv_integrity

        # Round-21 FIX (2026-04-30): pass ticker so clean_ohlcv can skip
        # zero-volume rejection for index series (^VIX, ^GSPC, I:VIX).
        out = clean_ohlcv(out, symbol=ticker)
        integrity = validate_ohlcv_integrity(out, symbol=ticker, interval=interval)
        if not integrity.get("clean", False):
            _log_ohlcv_integrity_failure(
                ticker=ticker,
                interval=interval,
                provider=provider,
                integrity=integrity,
            )
            return _quality_rejected_df(
                ticker=ticker,
                interval=interval,
                provider=provider,
                integrity=integrity,
            )
        out.attrs["integrity_ok"] = True
    except Exception:
        out.attrs["integrity_ok"] = False
    out.attrs["provider"] = provider
    out.attrs["fetched_at_utc"] = datetime.utcnow().isoformat() + "Z"
    out.attrs["ticker"] = ticker.upper()
    out.attrs["interval"] = interval
    return out



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

    if _massive_dead and _massive.is_crypto(ticker):
        # FIX 42 (2026-04-29): instead of returning empty when Massive is
        # exhausted for crypto, try Coinbase's public candles endpoint.
        # Coinbase is the live-trading venue; same product IDs (BTC-USD,
        # ETH-USD, AAVE-USD, etc.). Public endpoint, no auth, geo-clean
        # from US. Off by default; enable via brain_market_data_coinbase_fallback.
        if getattr(settings, "brain_market_data_coinbase_fallback", True):
            try:
                from .coinbase_ohlcv import get_ohlcv as _cb_get_ohlcv

                bars = _cb_get_ohlcv(
                    ticker, interval=interval, period=period,
                    start=_start_str, end=_end_str,
                )
                if bars:
                    _log_ohlcv_outcome(
                        ticker, interval, provider="coinbase",
                        reason="ok_massive_exhausted", row_count=len(bars),
                    )
                    return bars
                _log_ohlcv_outcome(
                    ticker, interval, provider="coinbase",
                    reason="empty_after_massive_dead", row_count=0,
                )
            except Exception as e:
                logger.warning("[market_data] Coinbase fallback failed for %s: %s", ticker, e)
                _log_ohlcv_outcome(
                    ticker, interval, provider="coinbase", reason="error", row_count=0,
                )
        # Phase F fix: only crypto tickers legitimately exhaust all Massive
        # variants (X:BASEUSD / X:BASEUSDT / X:BASEUSDC). For equities the
        # single candidate is the ticker itself and Polygon/yfinance are
        # entirely separate pipes — do not short-circuit them.
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
        # FIX 42 (2026-04-29): try Coinbase public candles before giving up
        # on crypto. yfinance crypto symbols rarely match, but Coinbase's
        # product IDs are exactly the BASE-USD shape we use.
        if getattr(settings, "brain_market_data_coinbase_fallback", True):
            try:
                from .coinbase_ohlcv import get_ohlcv as _cb_get_ohlcv

                bars = _cb_get_ohlcv(
                    ticker, interval=interval, period=period,
                    start=_start_str, end=_end_str,
                )
                if bars:
                    _log_ohlcv_outcome(
                        ticker, interval, provider="coinbase",
                        reason="ok_after_polygon_empty", row_count=len(bars),
                    )
                    return bars
            except Exception as e:
                logger.warning("[market_data] Coinbase fallback failed for %s: %s", ticker, e)
        _log_ohlcv_outcome(
            ticker, interval, provider="yfinance", reason="skipped_crypto_list_path", row_count=0,
        )
        return []

    if _start_str:
        df = (_yf_history(ticker, start=_start_str, end=_end_str, interval=interval) if _end_str else _yf_history(ticker, start=_start_str, interval=interval))
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
    with _ohlcv_quality_reject_log_lock:
        _ohlcv_quality_reject_log_cache.clear()


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
        if not result.empty or _is_quality_rejected_df(result):
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
            quality_rejected = False
            if not df.empty:
                _log_ohlcv_outcome(
                    ticker, interval, provider="massive", reason="ok", row_count=len(df),
                )
                finalized = _finalize_ohlcv_df(
                    df, ticker=ticker, interval=interval, provider="massive"
                )
                if not finalized.empty:
                    return _store_and_return(finalized)
                if _is_quality_rejected_df(finalized):
                    _log_ohlcv_outcome(
                        ticker,
                        interval,
                        provider="massive",
                        reason="quality_rejected_try_fallback" if fb else "quality_rejected_no_fallback",
                        row_count=0,
                    )
                    if not fb:
                        return _store_and_return(finalized)
                    quality_rejected = True
            if _massive.massive_aggregate_variants_all_dead(ticker):
                _massive_dead = True
            elif not quality_rejected:
                _log_ohlcv_outcome(
                    ticker, interval, provider="massive", reason="empty_try_fallback", row_count=0,
                )
        except Exception as e:
            logger.warning(f"[market_data] Massive DF failed for {ticker}: {e}")
            _log_ohlcv_outcome(
                ticker, interval, provider="massive", reason="error", row_count=0,
            )

    if _massive_dead and _massive.is_crypto(ticker):
        # FIX 42 (2026-04-29): Coinbase fallback for DataFrame path too.
        if getattr(settings, "brain_market_data_coinbase_fallback", True):
            try:
                from .coinbase_ohlcv import get_ohlcv as _cb_get_ohlcv

                bars = _cb_get_ohlcv(
                    ticker, interval=interval, period=period,
                    start=_start_str, end=_end_str,
                )
                if bars:
                    _df = pd.DataFrame([
                        {
                            "Open": r["open"], "High": r["high"], "Low": r["low"],
                            "Close": r["close"], "Volume": r["volume"],
                        }
                        for r in bars
                    ], index=pd.to_datetime([r["time"] for r in bars], unit="s", utc=True))
                    _log_ohlcv_outcome(
                        ticker, interval, provider="coinbase",
                        reason="ok_massive_exhausted_df", row_count=len(_df),
                    )
                    return _store_and_return(
                        _finalize_ohlcv_df(_df, ticker=ticker, interval=interval, provider="coinbase")
                    )
                _log_ohlcv_outcome(
                    ticker, interval, provider="coinbase",
                    reason="empty_after_massive_dead_df", row_count=0,
                )
            except Exception as e:
                logger.warning("[market_data] Coinbase DF fallback failed for %s: %s", ticker, e)
                _log_ohlcv_outcome(
                    ticker, interval, provider="coinbase", reason="error_df", row_count=0,
                )
        # Phase F fix: see fetch_ohlcv. Equities fall through to Polygon/yfinance.
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
            quality_rejected = False
            if not df.empty:
                _log_ohlcv_outcome(
                    ticker, interval, provider="polygon", reason="ok", row_count=len(df),
                )
                finalized = _finalize_ohlcv_df(
                    df, ticker=ticker, interval=interval, provider="polygon"
                )
                if not finalized.empty:
                    return _store_and_return(finalized)
                if _is_quality_rejected_df(finalized):
                    _log_ohlcv_outcome(
                        ticker,
                        interval,
                        provider="polygon",
                        reason="quality_rejected_try_fallback",
                        row_count=0,
                    )
                    quality_rejected = True
            if not quality_rejected:
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
        # FIX 42 (2026-04-29): Coinbase fallback for crypto in DataFrame path.
        if getattr(settings, "brain_market_data_coinbase_fallback", True):
            try:
                from .coinbase_ohlcv import get_ohlcv as _cb_get_ohlcv

                bars = _cb_get_ohlcv(
                    ticker, interval=interval, period=period,
                    start=_start_str, end=_end_str,
                )
                if bars:
                    _df = pd.DataFrame([
                        {
                            "Open": r["open"], "High": r["high"], "Low": r["low"],
                            "Close": r["close"], "Volume": r["volume"],
                        }
                        for r in bars
                    ], index=pd.to_datetime([r["time"] for r in bars], unit="s", utc=True))
                    _log_ohlcv_outcome(
                        ticker, interval, provider="coinbase",
                        reason="ok_after_polygon_empty_df", row_count=len(_df),
                    )
                    return _store_and_return(
                        _finalize_ohlcv_df(_df, ticker=ticker, interval=interval, provider="coinbase")
                    )
            except Exception as e:
                logger.warning("[market_data] Coinbase DF fallback failed for %s: %s", ticker, e)
        _log_ohlcv_outcome(
            ticker, interval, provider="yfinance", reason="skipped_crypto_df_path", row_count=0,
        )
        return pd.DataFrame()

    if _start_str:
        # 2026-04-28 leak fix: yfinance truncates to ~10 rows when given
        # both start AND period together. Pass start (and end if set) only.
        if _end_str:
            df = _yf_history(ticker, start=_start_str, end=_end_str, interval=interval)
        else:
            df = _yf_history(ticker, start=_start_str, interval=interval)
        # 2026-04-28: yfinance has a quirk with ^-prefixed index tickers
        # (^VIX, ^GSPC) where start-based queries return empty. Fall back to
        # period-mode and slice locally if we got nothing for an index.
        if (df is None or df.empty) and ticker.startswith("^"):
            _yf_period = _clamp_period(interval, "1y")
            df_full = _yf_history(ticker, period=_yf_period, interval=interval)
            if df_full is not None and not df_full.empty:
                try:
                    import pandas as _pd
                    _start_ts = _pd.Timestamp(_start_str)
                    if df_full.index.tz is not None:
                        _start_ts = _start_ts.tz_localize("UTC")
                    df = df_full[df_full.index >= _start_ts]
                except Exception:
                    df = df_full
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
    return _store_and_return(
        _finalize_ohlcv_df(_df, ticker=ticker, interval=interval, provider="yfinance")
    )


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
            and (
                not _massive.is_crypto(t)
                or not _massive.massive_aggregate_variants_all_dead(t)
            )
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
        worker_count = max(
            _MIN_PROVIDER_WORKERS,
            int(settings.market_data_polygon_batch_workers),
        )
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            for t, df in pool.map(_poly_one, tickers):
                if not df.empty:
                    results[t] = df
        missing = [t for t in tickers if t not in results]
        if not missing:
            return results
        tickers = missing

    crypto_tickers = [t for t in tickers if _is_crypto_ticker(t)]
    if crypto_tickers and getattr(settings, "brain_market_data_coinbase_fallback", True):
        try:
            from .coinbase_ohlcv import get_ohlcv as _cb_get_ohlcv

            for t in crypto_tickers:
                if t in results:
                    continue
                bars = _cb_get_ohlcv(t, interval=interval, period=period)
                if not bars:
                    continue
                df = pd.DataFrame([
                    {
                        "Open": r["open"],
                        "High": r["high"],
                        "Low": r["low"],
                        "Close": r["close"],
                        "Volume": r["volume"],
                    }
                    for r in bars
                ], index=pd.to_datetime([r["time"] for r in bars], unit="s", utc=True))
                finalized = _finalize_ohlcv_df(
                    df,
                    ticker=t,
                    interval=interval,
                    provider="coinbase",
                )
                if finalized.empty:
                    continue
                results[t] = finalized
                _log_ohlcv_outcome(
                    t,
                    interval,
                    provider="coinbase",
                    reason="ok_after_polygon_empty_batch",
                    row_count=len(results[t]),
                )
        except Exception as e:
            logger.warning("[market_data] Coinbase batch fallback failed: %s", e)

    yahoo_tickers = [
        t for t in tickers
        if t not in results
        and not _is_crypto_ticker(t)
        and not _is_unsupported_equity_ticker(t)
    ]
    for t in tickers:
        if t not in results and _is_crypto_ticker(t):
            _log_ohlcv_outcome(
                t,
                interval,
                provider="yfinance",
                reason="skipped_crypto_batch_path",
                row_count=0,
            )

    if not yahoo_tickers:
        return results

    from ..yf_session import batch_download
    batch_results = batch_download(yahoo_tickers, period=period, interval=interval)
    for t, df in batch_results.items():
        if df is None or df.empty:
            continue
        finalized = _finalize_ohlcv_df(
            df,
            ticker=t,
            interval=interval,
            provider="yfinance",
        )
        if not finalized.empty:
            results[t] = finalized

    for t in yahoo_tickers:
        if t in results:
            continue
        p = _clamp_period(interval, period)
        df = _yf_history(t, period=p, interval=interval)
        if df is not None and not df.empty:
            results[t] = df

    return results


# ── Quote ──────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────
# Implausible-quote boundary guard
# ─────────────────────────────────────────────────────────────────────
#
# f-trump-usd-poisoned-quote-source-audit (2026-05-07): the
# implausible-quote check (formerly enforced only at the exit-monitor
# layer via ``_exit_monitor_common.is_implausible_quote``) now also
# fires at the ``fetch_quote`` boundary. Two incidents (ARB-USD
# 2026-05-04 and TRUMP-USD 2026-05-06) showed identical bad prices
# repeating across hours of decisions -- a stale singleton-cache
# fingerprint in ``price_bus`` or the Massive WS subscriber. The
# exit-monitor guard kept the engine from acting, but every other
# consumer of ``fetch_quote`` (UI surfaces, feature engineering,
# regime classifier inputs) was silently exposed.
#
# Defense-in-depth: by validating at the data boundary, no consumer
# past ``fetch_quote`` ever sees implausible data. Returns ``None``
# (per the no-hardcoded-fallback rule) -- consumers handle absence,
# not substitute prices.
#
# Plausibility uses the SAME 0.1x-10x ratio bound as the three
# exit-monitor lanes share (``_exit_monitor_common.is_implausible_quote``).
# Anchor priority for the ratio comparison:
#   1. Per-ticker last-known-good cache (in-memory, this module).
#   2. Most-recent open Trade's ``entry_price`` for the same ticker.
#   3. None -- with no anchor we can't judge plausibility, so we
#      ACCEPT the quote and seed the cache with it.
#
# Rejection telemetry: each rejection is recorded per
# ``(ticker, source)``. After ``_REJECTION_THRESHOLD`` rejections in
# ``_REJECTION_WINDOW_S`` seconds, we write a ``degraded`` row to
# ``runtime_surface_state.market_data`` for the alert pipeline. This
# replaces the visibility we previously had via ``trading_stop_decisions``
# DATA_IMPLAUSIBLE rows -- those will now stop appearing because
# ``fetch_quote`` will return ``None`` before the exit-monitor sees
# the bad value.
from collections import deque as _deque
from threading import Lock as _Lock

_KNOWN_GOOD_CACHE: dict[str, float] = {}
_KNOWN_GOOD_LOCK = _Lock()

_REJECTION_WINDOW_S: float = 600.0  # 10 minutes
_REJECTION_THRESHOLD: int = 5
_REJECTIONS: dict[tuple[str, str], "_deque[float]"] = {}
_REJECTIONS_LOCK = _Lock()


def _resolve_implausibility_anchor(ticker: str) -> float | None:
    """Anchor for the implausibility ratio: cache → open Trade → None."""
    tk = (ticker or "").upper()
    if not tk:
        return None
    with _KNOWN_GOOD_LOCK:
        cached = _KNOWN_GOOD_CACHE.get(tk)
    if cached is not None and cached > 0:
        return cached
    try:
        from ...db import SessionLocal
        from ...models.trading import Trade

        db = SessionLocal()
        try:
            row = (
                db.query(Trade)
                .filter(Trade.ticker == tk, Trade.status == "open")
                .order_by(Trade.entry_date.desc())
                .first()
            )
            if row and row.entry_price and float(row.entry_price) > 0:
                return float(row.entry_price)
        finally:
            # FIX 46 pattern (canonical: scanner.py:1064-1074): explicit rollback
            # to end the implicit read-only transaction. SQLAlchemy's
            # session.close() returns the connection to the pool but doesn't
            # ROLLBACK by default — leaving it as 'idle in transaction' in
            # pg_stat_activity. Without this, every fetch_quote cache miss leaks
            # one idle-in-tx session until the pool exhausts or postgres
            # terminates the connection.
            try:
                db.rollback()
            except Exception:
                pass
            db.close()
    except Exception:
        pass
    return None


def _record_implausible_rejection(
    ticker: str, source: str, bad_price: float, anchor: float
) -> None:
    """Record a rejection; emit degraded surface state if threshold hit."""
    key = ((ticker or "").upper(), source or "unknown")
    now = _time.time()
    with _REJECTIONS_LOCK:
        dq = _REJECTIONS.setdefault(key, _deque(maxlen=64))
        dq.append(now)
        while dq and dq[0] < now - _REJECTION_WINDOW_S:
            dq.popleft()
        count = len(dq)

    if count >= _REJECTION_THRESHOLD:
        try:
            from .runtime_surface_state import persist_runtime_surface_now

            persist_runtime_surface_now(
                surface="market_data",
                state="degraded",
                source=source or "unknown",
                details={
                    "reason": "implausible_quote_burst",
                    "ticker": key[0],
                    "bad_price": bad_price,
                    "anchor": anchor,
                    "rejection_count": count,
                    "rejection_window_s": _REJECTION_WINDOW_S,
                    "rejection_threshold": _REJECTION_THRESHOLD,
                },
                updated_by="market_data.boundary_guard",
            )
        except Exception:
            pass


def _accept_known_good_price(ticker: str, price: float) -> None:
    """Update the per-ticker last-known-good cache after a clean fetch."""
    tk = (ticker or "").upper()
    if not tk or not price or price <= 0:
        return
    with _KNOWN_GOOD_LOCK:
        _KNOWN_GOOD_CACHE[tk] = float(price)


def _apply_boundary_guard(
    ticker: str, quote: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Validate a fetched quote against the per-ticker plausibility anchor.

    Returns ``None`` (and records the rejection for alerting) when the
    quote's price is implausibly far from the anchor. Otherwise updates
    the last-known-good cache and returns the quote unchanged.
    """
    if not quote:
        return quote
    try:
        price = float(quote.get("price") or 0.0)
    except (TypeError, ValueError):
        return quote
    if price <= 0:
        return quote

    anchor = _resolve_implausibility_anchor(ticker)
    if anchor is None or anchor <= 0:
        # No anchor available -- accept and seed the cache.
        _accept_known_good_price(ticker, price)
        return quote

    from ._exit_monitor_common import is_implausible_quote

    if is_implausible_quote(price, anchor):
        source = str(quote.get("source") or "unknown")
        ratio = price / anchor
        logger.warning(
            "[market_data] boundary guard REJECTED ticker=%s price=%s "
            "anchor=%s ratio=%.4f source=%s -- abstaining (returning None)",
            ticker, price, anchor, ratio, source,
        )
        _record_implausible_rejection(ticker, source, price, anchor)
        return None

    _accept_known_good_price(ticker, price)
    return quote


def _coinbase_quote_fallback(ticker: str, *, reason: str) -> dict[str, Any] | None:
    """Use Coinbase public ticker data as the final crypto quote source."""
    if not getattr(settings, "brain_market_data_coinbase_fallback", True):
        return None
    try:
        from .coinbase_ohlcv import get_quote as _cb_get_quote, is_crypto_usd

        if not is_crypto_usd(ticker):
            return None
        fi = _cb_get_quote(ticker)
        if fi and fi.get("last_price") is not None:
            return _build_quote_result(ticker, fi)
        logger.debug(
            "[market_data] Coinbase quote fallback empty for %s reason=%s",
            ticker,
            reason,
        )
    except Exception as e:
        logger.warning(
            "[market_data] Coinbase quote fallback failed for %s reason=%s: %s",
            ticker,
            reason,
            e,
        )
    return None


def fetch_quote(ticker: str, *, allow_provider_fallback: bool | None = None) -> dict[str, Any] | None:
    """Current price + enriched info, with implausible-quote boundary guard.

    Wraps the upstream cascade (price_bus → Massive WS → Massive REST →
    Polygon → yfinance) and applies ``_apply_boundary_guard`` to the
    result. Implausible-vs-anchor returns ``None`` rather than passing
    poisoned data downstream.
    """
    return _apply_boundary_guard(
        ticker, _fetch_quote_unguarded(ticker, allow_provider_fallback=allow_provider_fallback),
    )


def _fetch_quote_unguarded(
    ticker: str, *, allow_provider_fallback: bool | None = None
) -> dict[str, Any] | None:
    """Underlying fetch (no boundary guard).  Price bus → Massive WS → Massive REST → Polygon → yfinance.

    *allow_provider_fallback* ``None`` uses ``settings.market_data_allow_provider_fallback``.
    ``False`` forces Massive-only (WS + REST).
    """
    if _is_unsupported_equity_ticker(ticker):
        return None

    fb = _effective_allow_fallback(allow_provider_fallback)
    fi: dict[str, Any] | None = None

    # --- Price bus (fastest path — unified WS cache) ---
    try:
        from .price_bus import get_live_quote
        bus_q = get_live_quote(ticker)
        if bus_q and bus_q.get("price"):
            try:
                from .runtime_surface_state import persist_runtime_surface_now

                persist_runtime_surface_now(
                    surface="market_data",
                    state="ok",
                    source=str(bus_q.get("source") or "price_bus"),
                    as_of=datetime.utcnow(),
                    details={"provider": str(bus_q.get("source") or "price_bus")},
                    updated_by="market_data",
                )
            except Exception:
                pass
            from ..massive_client import is_crypto
            _cr = is_crypto(ticker)
            return {
                "ticker": ticker.upper(),
                "price": smart_round(bus_q["price"], crypto=_cr),
                "bid": bus_q.get("bid"),
                "ask": bus_q.get("ask"),
                "source": bus_q.get("source", "price_bus"),
            }
    except Exception:
        pass

    # --- Massive WebSocket cache (fastest path) ---
    if _use_massive():
        try:
            ws_snap = _massive.get_ws_quote(ticker)
            if ws_snap and ws_snap.price:
                from datetime import datetime as _dt
                fi = {
                    "last_price": ws_snap.price,
                    "previous_close": None,
                    "bid": ws_snap.bid,
                    "ask": ws_snap.ask,
                    "bid_size": ws_snap.bid_size,
                    "ask_size": ws_snap.ask_size,
                    "quote_ts": _dt.utcfromtimestamp(ws_snap.timestamp) if ws_snap.timestamp else None,
                    "provider": "massive_ws",
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
                fi = {**fi, "provider": "massive"}
                return _build_quote_result(ticker, fi)
            if _massive.massive_aggregate_variants_all_dead(ticker):
                _massive_dead = True
            if not _massive_dead:
                logger.debug(f"[market_data] Massive quote empty for {ticker}, falling back")
            fi = None
        except Exception as e:
            logger.warning(f"[market_data] Massive quote failed for {ticker}: {e}")

    if _massive_dead and _massive.is_crypto(ticker):
        cb_quote = _coinbase_quote_fallback(ticker, reason="massive_dead_crypto")
        if cb_quote:
            return cb_quote
        # Phase F fix: see fetch_ohlcv. Equities fall through to Polygon/yfinance.
        return None

    if not fb:
        return None

    # --- Polygon path (secondary) ---
    if _use_polygon():
        try:
            fi = _poly.get_last_quote(ticker)
            if fi and fi.get("last_price") is not None:
                fi = {**fi, "provider": "polygon"}
                return _build_quote_result(ticker, fi)
            logger.debug(f"[market_data] Polygon quote empty for {ticker}, falling back")
            fi = None
        except Exception as e:
            logger.warning(f"[market_data] Polygon quote failed for {ticker}: {e}")

    cb_quote = _coinbase_quote_fallback(ticker, reason="provider_chain_empty")
    if cb_quote:
        return cb_quote
    if _is_crypto_ticker(ticker):
        return None

    # --- yfinance / CoinGecko fallback ---
    fi = _yf_fast_info(ticker)
    if fi is None or fi.get("last_price") is None:
        return None
    fi = {**fi, "provider": "yfinance"}
    return _build_quote_result(ticker, fi)


def _serialize_ts(val: Any) -> str | None:
    """Convert a datetime/date to ISO string; pass through strings and None."""
    from datetime import datetime as _dt, date as _d
    if val is None:
        return None
    if isinstance(val, (_dt, _d)):
        return val.isoformat()
    return str(val)


def _parse_runtime_dt(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    raw = str(val).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _build_quote_result(ticker: str, fi: dict[str, Any]) -> dict[str, Any] | None:
    """Assemble a standardised quote dict from raw provider data."""
    from datetime import datetime as _dt

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
        "quote_ts": _serialize_ts(fi.get("quote_ts") or _dt.utcnow()),
        "source": fi.get("provider") or fi.get("source") or "market_data",
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
    if fi.get("bid") is not None:
        result["bid"] = fi["bid"]
    if fi.get("ask") is not None:
        result["ask"] = fi["ask"]
    if fi.get("bid_size") is not None:
        result["bid_size"] = fi["bid_size"]
    if fi.get("ask_size") is not None:
        result["ask_size"] = fi["ask_size"]
    try:
        from .emergency_liquidation import record_price_heartbeat

        record_price_heartbeat()
    except Exception:
        pass
    try:
        from .runtime_surface_state import persist_runtime_surface_now

        persist_runtime_surface_now(
            surface="market_data",
            state="ok",
            source=str(result.get("source") or "market_data"),
            as_of=_parse_runtime_dt(result.get("quote_ts")) or _dt.utcnow(),
            details={"provider": result.get("source"), "ticker": ticker.upper()},
            updated_by="market_data",
        )
    except Exception:
        pass
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

    crypto_missing = [t for t in tickers if _is_crypto_ticker(t)]
    for t in crypto_missing:
        if t in results:
            continue
        cb_quote = _coinbase_quote_fallback(t, reason="batch_provider_chain_empty")
        if cb_quote:
            results[t] = cb_quote

    yahoo_tickers = [
        t for t in tickers
        if t not in results
        and not _is_crypto_ticker(t)
        and not _is_unsupported_equity_ticker(t)
    ]
    if not yahoo_tickers:
        return results

    # --- yfinance fallback ---
    from ..yf_session import batch_download
    batch_download(yahoo_tickers, period="3mo", interval="1d")
    for t in yahoo_tickers:
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
    preloaded_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Compute requested technical indicators for a ticker.

    Uses the ``ta`` library (technical-analysis).  Returns a dict keyed by
    indicator name, each value a list of {time, value} or multi-key dicts.

    Results are cached for 5 minutes keyed on (ticker, interval, period,
    indicators) since the underlying OHLCV data is itself cached for 30 min.
    When *preloaded_df* is passed, the LRU cache is bypassed (caller-owned OHLCV).
    """
    fb = _effective_allow_fallback(allow_provider_fallback)
    if indicators is None:
        indicators = ["rsi", "macd", "sma_20", "ema_20", "bbands"]
    period = _clamp_period(interval, period)
    cache_key = (ticker.upper(), interval, period, frozenset(indicators), fb)

    now = _time.time()
    if preloaded_df is None:
        with _ind_cache_lock:
            entry = _ind_cache.get(cache_key)
            if entry and now - entry[0] < _IND_CACHE_TTL:
                return entry[1]

    result = _compute_indicators_fresh(
        ticker,
        interval,
        period,
        indicators,
        allow_provider_fallback=fb,
        preloaded_df=preloaded_df,
    )

    if preloaded_df is None:
        with _ind_cache_lock:
            if len(_ind_cache) >= _IND_CACHE_MAX:
                # FIX 50 (2026-05-01) — eviction had a hot-path bug: when ALL
                # entries are within TTL (e.g. learning cycle is currently
                # iterating through 10k+ tickers in <30 min), `stale` is empty,
                # zero entries are deleted, and the next line still adds a new
                # one. The dict grows unbounded beyond _IND_CACHE_MAX. Over
                # 9 hours this was the primary scheduler-worker leak source —
                # mem_watcher snapshots showed dict / list / function counts
                # growing monotonically with the indicator-cache fingerprint.
                # Falling back to `clear()` when no stale entries exist
                # guarantees the size cap. (The same fallback already exists
                # in scanner.py:_cache_put for the same reason.)
                cutoff = now - _IND_CACHE_TTL
                stale = [k for k, v in _ind_cache.items() if v[0] < cutoff]
                for k in stale:
                    del _ind_cache[k]
                if len(_ind_cache) >= _IND_CACHE_MAX:
                    _ind_cache.clear()
            _ind_cache[cache_key] = (now, result)
    return result


def _compute_indicators_fresh(
    ticker: str,
    interval: str,
    period: str,
    indicators: list[str],
    *,
    allow_provider_fallback: bool = True,
    preloaded_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Actual indicator computation (no cache).

    Uses fetch_ohlcv_df() with the same provider fallback policy unless
    *preloaded_df* is set (avoids duplicate network for snapshot batches).
    """
    if preloaded_df is None:
        df = fetch_ohlcv_df(
            ticker, interval=interval, period=period, allow_provider_fallback=allow_provider_fallback,
        )
    else:
        df = preloaded_df
    if df is None or df.empty:
        return {}

    df = df.copy()
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

    if name in ("volume_ratio", "rel_vol"):
        from .indicator_core import compute_relative_volume

        s = compute_relative_volume(volume.astype(float))
        return _series_to_records(timestamps, s, "value")

    if name == "gap_pct":
        from .indicator_core import compute_gap_pct

        s = compute_gap_pct(df["Open"].astype(float), close.astype(float))
        return _series_to_records(timestamps, s, "value")

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


def get_indicator_snapshot(
    ticker: str,
    interval: str = "1d",
    *,
    ohlcv_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Get latest indicator values (used for journal snapshots and AI context).

    Includes stochastic, EMA 20/50/100, extended vol/volume/momentum fields,
    optional ``equity_regime`` (SPY-based), and ``learned_v1`` OHLC stats.

    When *ohlcv_df* is provided, indicators and ``learned_v1`` reuse that frame
    (one provider pull per ticker in snapshot batches).
    """
    _inds = [
        "rsi", "rsi_7", "macd", "sma_20", "ema_20", "ema_50", "ema_100",
        "bbands", "bb_pct_b", "stoch", "adx", "atr", "obv",
        "roc_10", "realized_vol_20", "volume_z_20", "volume_z_60",
        "obv_slope_5", "atr_percentile_60", "macd_hist_slope_3",
        "volume_ratio", "gap_pct",
    ]
    result = compute_indicators(
        ticker,
        interval=interval,
        period="3mo",
        indicators=_inds,
        preloaded_df=ohlcv_df,
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
            ldf = ohlcv_df
            if ldf is None:
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
DEFAULT_SCAN_TICKERS = clean_equity_universe(DEFAULT_SCAN_TICKERS)

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


def _percentile_rank_percent(values: list[float], current: float) -> float | None:
    if not values:
        return None
    rank = sum(1 for value in values if value <= current)
    return round(rank / len(values) * 100, 1)


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

    # Enhanced: trend regime from SPY EMA cross + ADX
    trend_regime = "ranging"
    volatility_percentile: float | None = None
    try:
        df = fetch_ohlcv_df("SPY", period="3mo", interval="1d")
        if df is not None and len(df) >= 50:
            close = df["Close"]
            ema20 = close.ewm(span=20, adjust=False).mean()
            ema50 = close.ewm(span=50, adjust=False).mean()
            if float(ema20.iloc[-1]) > float(ema50.iloc[-1]):
                trend_regime = "trending_up"
            elif float(ema20.iloc[-1]) < float(ema50.iloc[-1]):
                trend_regime = "trending_down"
    except Exception:
        pass

    try:
        if vix_val is not None:
            vix_df = fetch_ohlcv_df("^VIX", period="1y", interval="1d")
            if vix_df is not None and len(vix_df) >= 20:
                vix_hist = vix_df["Close"].dropna().tolist()
                volatility_percentile = _percentile_rank_percent(vix_hist, vix_val)
    except Exception:
        pass

    result = {
        "spy_direction": spy_direction,
        "spy_momentum_5d": spy_momentum_5d,
        "vix": vix_val,
        "vix_regime": vix_regime,
        "regime": regime,
        "regime_numeric": regime_numeric,
        "trend_regime": trend_regime,
        "volatility_percentile": volatility_percentile,
    }
    _market_regime_cache["data"] = result
    _market_regime_cache["ts"] = now
    try:
        from .runtime_surface_state import persist_runtime_surface_now

        persist_runtime_surface_now(
            surface="regime",
            state="ok",
            source="get_market_regime",
            as_of=datetime.utcnow(),
            details={
                "regime": result.get("regime"),
                "vix_regime": result.get("vix_regime"),
                "spy_direction": result.get("spy_direction"),
            },
            updated_by="market_data",
        )
    except Exception:
        pass
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
