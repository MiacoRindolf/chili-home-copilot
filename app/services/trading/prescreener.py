"""Pre-screener: fast server-side filtering via FinViz + yfinance screener.

Instead of fetching OHLCV and computing indicators for 5000+ tickers,
this module uses FinViz's free pre-computed screener data and Yahoo
Finance's server-side screener API to narrow the universe to ~200-300
interesting candidates in seconds.  The scanner then only deep-scores
this short list.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

logger = logging.getLogger(__name__)

# ── Cache ──────────────────────────────────────────────────────────────

_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 1800  # 30 minutes

_prescreen_status: dict[str, Any] = {
    "running": False,
    "candidates": 0,
    "sources": {},
    "last_run": None,
    "last_duration_s": None,
}


def get_prescreen_status() -> dict[str, Any]:
    return dict(_prescreen_status)


def _cache_get(key: str) -> Any | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        ts, val = entry
        if time.time() - ts > _CACHE_TTL:
            del _cache[key]
            return None
        return val


def _cache_set(key: str, val: Any) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), val)


# ── FinViz Screener Queries ───────────────────────────────────────────

def _finviz_screen(filters_dict: dict | None = None,
                   signal: str = "",
                   limit: int = 200) -> list[str]:
    """Run a FinViz screen and return a list of ticker strings."""
    try:
        from finvizfinance.screener.overview import Overview

        screener = Overview()
        screener.set_filter(signal=signal, filters_dict=filters_dict or {})
        df = screener.screener_view(limit=limit, verbose=0, sleep_sec=0)
        if df is not None and not df.empty:
            col = "Ticker" if "Ticker" in df.columns else df.columns[0]
            return df[col].tolist()
    except Exception as e:
        logger.warning(f"[prescreener] FinViz screen failed ({signal or filters_dict}): {e}")
    return []


def _finviz_most_active() -> list[str]:
    return _finviz_screen(signal="ta_mostactive", limit=200)


def _finviz_top_gainers() -> list[str]:
    return _finviz_screen(signal="ta_topgainers", limit=100)


def _finviz_new_high() -> list[str]:
    return _finviz_screen(signal="ta_newhigh", limit=100)


def _finviz_oversold() -> list[str]:
    return _finviz_screen(signal="ta_oversold", limit=100)


def _finviz_unusual_volume() -> list[str]:
    return _finviz_screen(signal="ta_unusualvolume", limit=100)


def _finviz_bullish_sma() -> list[str]:
    """Stocks where SMA20 is above SMA50 and price above SMA20."""
    return _finviz_screen(
        filters_dict={
            "20-Day Simple Moving Average": "Price above SMA20",
            "50-Day Simple Moving Average": "SMA50 below SMA20",
        },
        limit=100,
    )


def _finviz_high_volume_tradeable() -> list[str]:
    """Liquid stocks with average volume > 1M and price > $5."""
    return _finviz_screen(
        filters_dict={
            "Average Volume": "Over 1M",
            "Price": "Over $5",
        },
        limit=200,
    )


# ── Yahoo Finance Screener ────────────────────────────────────────────

def _yf_screen(query_name: str, count: int = 100) -> list[str]:
    """Run a yfinance predefined screener query."""
    try:
        import yfinance as yf
        response = yf.screen(query_name, count=min(count, 250))
        if response and "quotes" in response:
            return [q["symbol"] for q in response["quotes"] if q.get("symbol")]
    except Exception as e:
        logger.warning(f"[prescreener] yfinance screen '{query_name}' failed: {e}")
    return []


def _yf_most_actives() -> list[str]:
    return _yf_screen("most_actives", 100)


def _yf_day_gainers() -> list[str]:
    return _yf_screen("day_gainers", 100)


def _yf_undervalued_growth() -> list[str]:
    return _yf_screen("undervalued_growth_stocks", 100)


def _yf_aggressive_small_caps() -> list[str]:
    return _yf_screen("aggressive_small_caps", 50)


# ── Crypto candidates ─────────────────────────────────────────────────

def _crypto_candidates() -> list[str]:
    """Top crypto tickers -- fast, no API call needed for the list."""
    from .market_data import DEFAULT_CRYPTO_TICKERS
    from ..ticker_universe import get_all_crypto_tickers
    try:
        return get_all_crypto_tickers(n=50)
    except Exception:
        return list(DEFAULT_CRYPTO_TICKERS)


# ── Core watchlist / fallback tickers ─────────────────────────────────

def _core_tickers() -> list[str]:
    """Always include the default blue-chip + popular tickers."""
    from .market_data import DEFAULT_SCAN_TICKERS
    return list(DEFAULT_SCAN_TICKERS)


# ── Main entry point ──────────────────────────────────────────────────

def get_prescreened_candidates(
    include_crypto: bool = True,
    max_total: int = 500,
) -> list[str]:
    """Return a de-duplicated list of pre-screened candidate tickers.

    Combines multiple FinViz signals + yfinance screens + crypto in
    parallel.  Results are cached for 30 minutes.

    Typical output: 200-400 unique tickers, gathered in 5-15 seconds.
    """
    cached = _cache_get("prescreened_candidates")
    if cached is not None:
        logger.info(f"[prescreener] Returning {len(cached)} cached candidates")
        _prescreen_status["candidates"] = len(cached)
        return cached

    _prescreen_status["running"] = True
    _prescreen_status["sources"] = {}
    start = time.time()

    sources: dict[str, Any] = {
        "finviz_most_active": _finviz_most_active,
        "finviz_top_gainers": _finviz_top_gainers,
        "finviz_new_high": _finviz_new_high,
        "finviz_oversold": _finviz_oversold,
        "finviz_unusual_volume": _finviz_unusual_volume,
        "finviz_bullish_sma": _finviz_bullish_sma,
        "finviz_high_volume": _finviz_high_volume_tradeable,
        "yf_most_actives": _yf_most_actives,
        "yf_day_gainers": _yf_day_gainers,
        "yf_undervalued_growth": _yf_undervalued_growth,
        "yf_small_caps": _yf_aggressive_small_caps,
    }

    results_by_source: dict[str, list[str]] = {}
    seen: set[str] = set()
    combined: list[str] = []

    # Always include core tickers
    core = _core_tickers()
    for t in core:
        if t not in seen:
            seen.add(t)
            combined.append(t)

    with ThreadPoolExecutor(max_workers=6) as executor:
        future_map = {
            executor.submit(fn): name for name, fn in sources.items()
        }
        for future in as_completed(future_map):
            name = future_map[future]
            try:
                tickers = future.result()
                results_by_source[name] = tickers
                for t in tickers:
                    t_upper = t.upper().strip()
                    if t_upper and t_upper not in seen:
                        seen.add(t_upper)
                        combined.append(t_upper)
            except Exception as e:
                logger.warning(f"[prescreener] Source '{name}' failed: {e}")
                results_by_source[name] = []

    if include_crypto:
        crypto = _crypto_candidates()
        results_by_source["crypto"] = crypto
        for t in crypto:
            t_upper = t.upper().strip()
            if t_upper and t_upper not in seen:
                seen.add(t_upper)
                combined.append(t_upper)

    combined = combined[:max_total]

    elapsed = time.time() - start
    source_counts = {k: len(v) for k, v in results_by_source.items()}
    logger.info(
        f"[prescreener] Pre-screened {len(combined)} unique candidates "
        f"from {len(sources) + 1} sources in {elapsed:.1f}s: {source_counts}"
    )

    _prescreen_status["running"] = False
    _prescreen_status["candidates"] = len(combined)
    _prescreen_status["sources"] = source_counts
    _prescreen_status["last_run"] = time.time()
    _prescreen_status["last_duration_s"] = round(elapsed, 1)

    _cache_set("prescreened_candidates", combined)
    return combined


def invalidate_cache() -> None:
    """Force the next call to re-fetch from sources."""
    with _cache_lock:
        _cache.pop("prescreened_candidates", None)
