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
_finviz_lock = threading.Lock()  # serialize FinViz requests to avoid 429

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
    """Run a FinViz screen and return a list of ticker strings.

    Uses a module-level lock so only one FinViz request runs at a time,
    preventing HTTP 429 rate-limit errors.
    """
    with _finviz_lock:
        try:
            from finvizfinance.screener.overview import Overview

            screener = Overview()
            screener.set_filter(signal=signal, filters_dict=filters_dict or {})
            df = screener.screener_view(limit=limit, verbose=0, sleep_sec=1)
            if df is not None and not df.empty:
                col = "Ticker" if "Ticker" in df.columns else df.columns[0]
                return df[col].tolist()
        except Exception as e:
            logger.warning(f"[prescreener] FinViz screen failed ({signal or filters_dict}): {e}")
        finally:
            time.sleep(0.5)
    return []


def _finviz_most_active() -> list[str]:
    return _finviz_screen(signal="Most Active", limit=200)


def _finviz_top_gainers() -> list[str]:
    return _finviz_screen(signal="Top Gainers", limit=100)


def _finviz_new_high() -> list[str]:
    return _finviz_screen(signal="New High", limit=100)


def _finviz_oversold() -> list[str]:
    return _finviz_screen(signal="Oversold", limit=100)


def _finviz_unusual_volume() -> list[str]:
    return _finviz_screen(signal="Unusual Volume", limit=100)


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


def _finviz_overbought() -> list[str]:
    return _finviz_screen(signal="Overbought", limit=100)


def _finviz_most_volatile() -> list[str]:
    return _finviz_screen(signal="Most Volatile", limit=100)


def _finviz_top_losers() -> list[str]:
    return _finviz_screen(signal="Top Losers", limit=100)


def _finviz_double_bottom() -> list[str]:
    return _finviz_screen(signal="Double Bottom", limit=100)


def _finviz_multiple_tops() -> list[str]:
    return _finviz_screen(signal="Multiple Top", limit=100)


def _finviz_upgrades() -> list[str]:
    return _finviz_screen(signal="Upgrade", limit=100)


def _finviz_earnings_before() -> list[str]:
    return _finviz_screen(signal="Earnings Before", limit=100)


def _finviz_recent_insider_buying() -> list[str]:
    return _finviz_screen(signal="Insider Buying", limit=100)


def _finviz_high_relative_volume() -> list[str]:
    """Stocks with today's volume > 2x average — something is happening."""
    return _finviz_screen(
        filters_dict={
            "Relative Volume": "Over 2",
            "Average Volume": "Over 200K",
            "Price": "Over $2",
        },
        limit=200,
    )


def _finviz_small_cap_momentum() -> list[str]:
    """Small-cap stocks with upward price momentum."""
    return _finviz_screen(
        filters_dict={
            "Market Cap.": "Small ($300mln to $2bln)",
            "Performance": "Week Up",
            "Average Volume": "Over 200K",
        },
        limit=150,
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
    return _yf_screen("aggressive_small_caps", 100)


def _yf_day_losers() -> list[str]:
    return _yf_screen("day_losers", 100)


def _yf_growth_tech() -> list[str]:
    return _yf_screen("growth_technology_stocks", 100)


def _yf_undervalued_large() -> list[str]:
    return _yf_screen("undervalued_large_caps", 100)


def _yf_conservative_foreign() -> list[str]:
    return _yf_screen("conservative_foreign_funds", 50)


def _yf_small_cap_gainers() -> list[str]:
    return _yf_screen("small_cap_gainers", 100)


# ── Crypto candidates ─────────────────────────────────────────────────

def _crypto_candidates() -> list[str]:
    """Top crypto tickers by market cap."""
    from .market_data import DEFAULT_CRYPTO_TICKERS
    from ..ticker_universe import get_all_crypto_tickers
    try:
        return get_all_crypto_tickers(n=200)
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
    max_total: int = 1500,
) -> list[str]:
    """Return a de-duplicated list of pre-screened candidate tickers.

    Combines multiple FinViz signals + yfinance screens + crypto in
    parallel.  Results are cached for 30 minutes.

    Typical output: 600-1200 unique tickers, gathered in 10-30 seconds.
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
        "finviz_top_losers": _finviz_top_losers,
        "finviz_new_high": _finviz_new_high,
        "finviz_oversold": _finviz_oversold,
        "finviz_overbought": _finviz_overbought,
        "finviz_unusual_volume": _finviz_unusual_volume,
        "finviz_most_volatile": _finviz_most_volatile,
        "finviz_bullish_sma": _finviz_bullish_sma,
        "finviz_high_volume": _finviz_high_volume_tradeable,
        "finviz_high_rel_volume": _finviz_high_relative_volume,
        "finviz_double_bottom": _finviz_double_bottom,
        "finviz_multiple_tops": _finviz_multiple_tops,
        "finviz_upgrades": _finviz_upgrades,
        "finviz_earnings_before": _finviz_earnings_before,
        "finviz_insider_buying": _finviz_recent_insider_buying,
        "finviz_smallcap_momentum": _finviz_small_cap_momentum,
        "yf_most_actives": _yf_most_actives,
        "yf_day_gainers": _yf_day_gainers,
        "yf_day_losers": _yf_day_losers,
        "yf_undervalued_growth": _yf_undervalued_growth,
        "yf_small_caps": _yf_aggressive_small_caps,
        "yf_growth_tech": _yf_growth_tech,
        "yf_undervalued_large": _yf_undervalued_large,
        "yf_small_cap_gainers": _yf_small_cap_gainers,
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

    with ThreadPoolExecutor(max_workers=10) as executor:
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
        _cache.pop("daytrade_candidates", None)
        _cache.pop("breakout_candidates", None)


# ── Day-Trade Candidates ──────────────────────────────────────────────

def _crypto_top_movers() -> list[str]:
    """Fetch top crypto movers by 24h volume via CoinGecko (free, cached)."""
    cached = _cache_get("crypto_top_movers")
    if cached is not None:
        return cached
    try:
        import requests
        resp = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "volume_desc",
                "per_page": 100,
                "page": 1,
                "sparkline": "false",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        stables = {"usdt", "usdc", "dai", "busd", "tusd", "usdp", "frax", "gusd"}
        tickers = []
        for coin in data:
            sym = coin.get("symbol", "").lower()
            if sym in stables:
                continue
            tickers.append(sym.upper() + "-USD")
        _cache_set("crypto_top_movers", tickers)
        return tickers
    except Exception as e:
        logger.warning(f"[prescreener] CoinGecko top movers failed: {e}")
        from .market_data import DEFAULT_CRYPTO_TICKERS
        return list(DEFAULT_CRYPTO_TICKERS)


def get_trending_crypto() -> list[str]:
    """Fetch currently trending crypto from CoinGecko search/trending.

    Returns yfinance-compatible SYMBOL-USD tickers. Merged with top movers
    for a broader discovery set that captures new/viral tokens (including
    those available on MetaMask DEXes).
    """
    cached = _cache_get("trending_crypto")
    if cached is not None:
        return cached

    trending: list[str] = []
    stables = {"usdt", "usdc", "dai", "busd", "tusd", "usdp", "frax", "gusd"}
    try:
        import requests
        resp = requests.get(
            "https://api.coingecko.com/api/v3/search/trending",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("coins", []):
            coin = item.get("item", {})
            sym = coin.get("symbol", "").lower()
            if sym and sym not in stables:
                trending.append(sym.upper() + "-USD")
    except Exception as e:
        logger.warning(f"[prescreener] CoinGecko trending failed: {e}")

    movers = _crypto_top_movers()
    seen: set[str] = set()
    merged: list[str] = []
    for t in trending + movers:
        if t not in seen:
            seen.add(t)
            merged.append(t)
    _cache_set("trending_crypto", merged)
    return merged


def get_daytrade_candidates(max_total: int = 300) -> list[str]:
    """Return tickers suited for intraday / day-trade scanning.

    Combines high-activity FinViz signals (most active, top gainers,
    top losers, most volatile, unusual volume) for stocks that are
    moving *today*, plus top crypto movers (crypto trades 24/7).
    Cached for 15 minutes (shorter TTL than swing).
    """
    cached = _cache_get("daytrade_candidates")
    if cached is not None:
        logger.info(f"[prescreener] Returning {len(cached)} cached day-trade candidates")
        return cached

    start = time.time()
    sources: dict[str, Any] = {
        "finviz_most_active": _finviz_most_active,
        "finviz_top_gainers": _finviz_top_gainers,
        "finviz_top_losers": _finviz_top_losers,
        "finviz_most_volatile": _finviz_most_volatile,
        "finviz_unusual_volume": _finviz_unusual_volume,
        "yf_most_actives": _yf_most_actives,
        "yf_day_gainers": _yf_day_gainers,
        "yf_day_losers": _yf_day_losers,
        "crypto_movers": _crypto_top_movers,
        "crypto_base": _crypto_candidates,
    }

    seen: set[str] = set()
    combined: list[str] = []

    core = _core_tickers()
    for t in core:
        if t not in seen:
            seen.add(t)
            combined.append(t)

    with ThreadPoolExecutor(max_workers=6) as executor:
        future_map = {executor.submit(fn): name for name, fn in sources.items()}
        for future in as_completed(future_map):
            name = future_map[future]
            try:
                tickers = future.result()
                for t in tickers:
                    t_upper = t.upper().strip()
                    if t_upper and t_upper not in seen:
                        seen.add(t_upper)
                        combined.append(t_upper)
            except Exception as e:
                logger.warning(f"[prescreener] Day-trade source '{name}' failed: {e}")

    combined = combined[:max_total]
    elapsed = time.time() - start
    logger.info(f"[prescreener] Day-trade pre-screen: {len(combined)} candidates in {elapsed:.1f}s")

    with _cache_lock:
        _cache["daytrade_candidates"] = (time.time(), combined)
    return combined


# ── Breakout Candidates ───────────────────────────────────────────────

def _finviz_consolidation() -> list[str]:
    """Low-volatility stocks that may be building up for a breakout."""
    return _finviz_screen(
        filters_dict={
            "Volatility": "Week - Under 3%",
            "Average Volume": "Over 500K",
            "Price": "Over $5",
            "20-Day Simple Moving Average": "Price above SMA20",
        },
        limit=150,
    )


def _finviz_channel_up() -> list[str]:
    """Stocks currently in a rising channel pattern."""
    return _finviz_screen(signal="Channel Up", limit=100)


def _finviz_wedge() -> list[str]:
    """Stocks in wedge patterns (often precede breakouts)."""
    return _finviz_screen(signal="Wedge", limit=100)


def _finviz_near_52w_high() -> list[str]:
    """Stocks within 5% of 52-week high — potential breakout through."""
    return _finviz_screen(
        filters_dict={
            "52-Week High/Low": "0-5% below High",
            "Average Volume": "Over 300K",
        },
        limit=150,
    )


def get_breakout_candidates(max_total: int = 300) -> list[str]:
    """Return tickers suited for breakout / consolidation scanning.

    Combines low-volatility stocks near resistance, channel patterns,
    wedges, near-52-week-high stocks, and crypto (which can consolidate
    and break out just like equities).  Cached for 30 minutes.
    """
    cached = _cache_get("breakout_candidates")
    if cached is not None:
        logger.info(f"[prescreener] Returning {len(cached)} cached breakout candidates")
        return cached

    start = time.time()
    sources: dict[str, Any] = {
        "finviz_consolidation": _finviz_consolidation,
        "finviz_channel_up": _finviz_channel_up,
        "finviz_wedge": _finviz_wedge,
        "finviz_near_52w_high": _finviz_near_52w_high,
        "finviz_new_high": _finviz_new_high,
        "crypto_base": _crypto_candidates,
    }

    seen: set[str] = set()
    combined: list[str] = []

    core = _core_tickers()
    for t in core:
        if t not in seen:
            seen.add(t)
            combined.append(t)

    with ThreadPoolExecutor(max_workers=6) as executor:
        future_map = {executor.submit(fn): name for name, fn in sources.items()}
        for future in as_completed(future_map):
            name = future_map[future]
            try:
                tickers = future.result()
                for t in tickers:
                    t_upper = t.upper().strip()
                    if t_upper and t_upper not in seen:
                        seen.add(t_upper)
                        combined.append(t_upper)
            except Exception as e:
                logger.warning(f"[prescreener] Breakout source '{name}' failed: {e}")

    combined = combined[:max_total]
    elapsed = time.time() - start
    logger.info(f"[prescreener] Breakout pre-screen: {len(combined)} candidates in {elapsed:.1f}s")

    _cache_set("breakout_candidates", combined)
    return combined
