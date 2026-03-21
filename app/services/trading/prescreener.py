"""Pre-screener: fast server-side filtering via Massive.com + yfinance screener.

Instead of fetching OHLCV and computing indicators for 5000+ tickers,
this module uses the paid Massive.com API (full-market snapshot in one
call, technical indicators, Benzinga partner data) and Yahoo Finance's
server-side screener API to narrow the universe to ~200-300 interesting
candidates in seconds.  The scanner then only deep-scores this short list.

FinViz web-scraping is retained ONLY as a silent fallback for chart-pattern
screens (Double Bottom, Multiple Tops, Wedge, Channel Up) that have no
Massive equivalent.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

logger = logging.getLogger(__name__)

# Crypto symbols to exclude: stablecoins, wrapped USD, tokenized treasuries,
# money-market funds, and other non-tradeable / illiquid tokens that always
# 404 on Massive and waste API calls.
_CRYPTO_EXCLUDE: set[str] = {
    # stablecoins
    "usdt", "usdc", "dai", "busd", "tusd", "usdp", "frax", "gusd",
    "fdusd", "pyusd", "eurc", "crvusd", "usde", "usad", "usdq",
    "usd0", "usd1", "usdon", "usdg", "gho", "rlusd", "bfusd",
    "usdd", "eurs", "xsgd", "bidr", "idrt", "lusd",
    # tokenized treasuries / money-market / RWA that have no exchange data
    "ustb", "jaaa", "jtrsy", "ylds", "eutbl", "stable", "clbr-u",
    # wrapped / bridged duplicates
    "wbtc", "weth", "steth", "wsteth", "cbeth", "reth",
    "wbnb", "wmatic", "wavax", "wtrx",
    # gold-backed / commodity tokens
    "xaut", "paxg",
}

# ── Cache ──────────────────────────────────────────────────────────────

_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 3600  # 1 hour — keep prescreener results longer (64 GB RAM)
_finviz_sem = threading.Semaphore(2)  # only used for chart-pattern fallback

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
        stored_ts, val, ttl = entry if len(entry) == 3 else (entry[0], entry[1], _CACHE_TTL)
        if time.time() - stored_ts > ttl:
            del _cache[key]
            return None
        return val


def _cache_set(key: str, val: Any, ttl: int = _CACHE_TTL) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), val, ttl)


# ── Massive.com Screener Queries (primary) ─────────────────────────────

def _massive_most_active() -> list[str]:
    from ..massive_client import screen_most_active
    return screen_most_active(limit=500)  # Premium: increased from 200


def _massive_top_gainers() -> list[str]:
    from ..massive_client import screen_top_gainers
    return screen_top_gainers(limit=300)  # Premium: increased from 100


def _massive_top_losers() -> list[str]:
    from ..massive_client import screen_top_losers
    return screen_top_losers(limit=300)  # Premium: increased from 100


def _massive_new_high() -> list[str]:
    from ..massive_client import screen_new_high
    return screen_new_high(limit=300)  # Premium: increased from 100


def _massive_unusual_volume() -> list[str]:
    from ..massive_client import screen_unusual_volume
    return screen_unusual_volume(limit=500)  # Premium: increased from 200


def _massive_most_volatile() -> list[str]:
    from ..massive_client import screen_most_volatile
    return screen_most_volatile(limit=300, min_price=1.0)  # Premium: increased from 100


def _massive_high_volume() -> list[str]:
    from ..massive_client import screen_high_volume
    return screen_high_volume(limit=500, min_vol=1_000_000, min_price=5.0)  # Premium: increased from 200


def _massive_high_rel_volume() -> list[str]:
    from ..massive_client import screen_high_relative_volume
    return screen_high_relative_volume(limit=500, min_ratio=2.0,
                                       min_prev_vol=200_000, min_price=2.0)  # Premium: increased from 200


def _massive_momentum_gappers() -> list[str]:
    from ..massive_client import screen_momentum_gappers
    return screen_momentum_gappers(limit=300)  # Premium: increased from 100


def _massive_upgrades() -> list[str]:
    from ..massive_client import get_benzinga_ratings
    return get_benzinga_ratings(action="upgrade", limit=100)


def _massive_earnings() -> list[str]:
    from ..massive_client import get_benzinga_earnings
    return get_benzinga_earnings(limit=100)


# ── FinViz fallback (chart-pattern screens only) ──────────────────────

def _finviz_screen(filters_dict: dict | None = None,
                   signal: str = "",
                   limit: int = 200) -> list[str]:
    """Run a FinViz screen (used ONLY for chart-pattern signals with no
    Massive equivalent). Returns [] gracefully on rate-limit."""
    cache_key = f"finviz_{signal or ''}_{'_'.join(sorted((filters_dict or {}).keys()))}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    with _finviz_sem:
        rechecked = _cache_get(cache_key)
        if rechecked is not None:
            return rechecked
        try:
            from finvizfinance.screener.overview import Overview

            screener = Overview()
            screener.set_filter(signal=signal, filters_dict=filters_dict or {})
            df = screener.screener_view(limit=limit, verbose=0, sleep_sec=0)
            if df is not None and not df.empty:
                col = "Ticker" if "Ticker" in df.columns else df.columns[0]
                result = df[col].tolist()
                _cache_set(cache_key, result, ttl=3600)
                return result
        except Exception as e:
            logger.debug(f"[prescreener] FinViz pattern screen ({signal}): {e}")
        finally:
            time.sleep(0.3)
    return []


def _finviz_double_bottom() -> list[str]:
    return _finviz_screen(signal="Double Bottom", limit=100)


def _finviz_multiple_tops() -> list[str]:
    return _finviz_screen(signal="Multiple Top", limit=100)


# ── Yahoo Finance Screener ────────────────────────────────────────────

def _yf_screen(query_name: str, count: int = 100) -> list[str]:
    """Run a yfinance predefined screener query (cached 60 min per query)."""
    cache_key = f"yf_{query_name}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        import yfinance as yf
        response = yf.screen(query_name, count=min(count, 250))
        if response and "quotes" in response:
            result = [q["symbol"] for q in response["quotes"] if q.get("symbol")]
            _cache_set(cache_key, result, ttl=3600)
            return result
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
    """Top crypto tickers by market cap, excluding stablecoins and junk."""
    from .market_data import DEFAULT_CRYPTO_TICKERS
    from ..ticker_universe import get_all_crypto_tickers
    try:
        raw = get_all_crypto_tickers(n=200)
    except Exception:
        raw = list(DEFAULT_CRYPTO_TICKERS)
    return [t for t in raw if t.split("-")[0].lower() not in _CRYPTO_EXCLUDE]


# ── Static fallback pool (used when live sources underperform) ────────

_STATIC_ACTIVE_STOCKS = [
    # Large-cap tech
    "MSFT", "AAPL", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AMD", "INTC",
    "CRM", "ADBE", "ORCL", "IBM", "CSCO", "QCOM", "AVGO", "TXN", "MU", "AMAT",
    "LRCX", "KLAC", "MRVL", "SNPS", "CDNS", "PANW", "CRWD", "ZS", "FTNT",
    # Cloud / SaaS / AI
    "NOW", "SNOW", "DDOG", "NET", "PLTR", "AI", "PATH", "MDB", "TEAM", "HUBS",
    "WDAY", "ZM", "DOCU", "OKTA", "TWLO", "SQ", "SHOP", "MELI", "SE",
    # Finance
    "JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "SCHW", "AXP", "V", "MA",
    "PYPL", "COF", "USB", "PNC", "TFC", "ALLY",
    # Healthcare / Biotech
    "UNH", "JNJ", "PFE", "MRK", "ABBV", "LLY", "TMO", "DHR", "ABT", "BMY",
    "GILD", "AMGN", "REGN", "VRTX", "ISRG", "MDT", "SYK", "BDX", "ZBH",
    "MRNA", "BNTX", "ILMN",
    # Consumer
    "COST", "WMT", "HD", "LOW", "TGT", "SBUX", "MCD", "NKE", "DIS", "NFLX",
    "ABNB", "BKNG", "MAR", "PG", "KO", "PEP", "CL", "EL", "PM",
    # Industrial / Energy
    "CAT", "DE", "GE", "HON", "MMM", "BA", "RTX", "LMT", "NOC", "GD",
    "XOM", "CVX", "COP", "SLB", "HAL", "OXY", "DVN", "MPC", "VLO", "PSX",
    # Growth / Momentum mid-cap
    "RKLB", "LUNR", "ASTS", "SMCI", "AFRM", "HOOD", "SOFI", "UPST", "RIVN",
    "LCID", "DKNG", "PENN", "DASH", "UBER", "LYFT", "GRAB", "NU", "CPNG",
    "DUOL", "RDDT", "IONQ", "RGTI", "QUBT", "SOUN", "JOBY", "ACHR",
    # REITs / Utilities / Telecom
    "AMT", "PLD", "CCI", "EQIX", "SPG", "O", "NEE", "DUK", "SO", "D",
    "T", "VZ", "TMUS",
    # ETFs for diversity
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "XLV", "XLI",
    "ARKK", "ARKG", "TLT", "HYG", "GLD", "SLV", "USO", "KWEB",
]


def _static_active_stocks() -> list[str]:
    """Fallback pool of ~200 popular tickers used when live sources
    are rate-limited or returning few results."""
    return list(_STATIC_ACTIVE_STOCKS)


# ── Core watchlist / fallback tickers ─────────────────────────────────

def _core_tickers() -> list[str]:
    """Always include the default blue-chip + popular tickers."""
    from .market_data import DEFAULT_SCAN_TICKERS
    return list(DEFAULT_SCAN_TICKERS)


# ── Main entry point ──────────────────────────────────────────────────

def get_prescreened_candidates(
    include_crypto: bool = True,
    max_total: int = 3000,  # Premium: increased from 1500
) -> list[str]:
    """Return a de-duplicated list of pre-screened candidate tickers.

    Combines Massive.com snapshot filters + yfinance screens + crypto in
    parallel.  Results are cached for 1 hour.

    With premium Massive API: 1500-3000 unique tickers, gathered in 2-5 seconds.
    """
    cached = _cache_get("prescreened_candidates")
    if cached is not None:
        result = cached[:max_total] if max_total < len(cached) else cached
        logger.info(f"[prescreener] Returning {len(result)} cached candidates (of {len(cached)} total)")
        _prescreen_status["candidates"] = len(result)
        return result

    _prescreen_status["running"] = True
    _prescreen_status["sources"] = {}
    start = time.time()

    sources: dict[str, Any] = {
        # Massive.com (primary — one cached snapshot, many filters)
        "massive_most_active": _massive_most_active,
        "massive_top_gainers": _massive_top_gainers,
        "massive_new_high": _massive_new_high,
        "massive_unusual_volume": _massive_unusual_volume,
        "massive_most_volatile": _massive_most_volatile,
        "massive_high_volume": _massive_high_volume,
        "massive_high_rel_volume": _massive_high_rel_volume,
        "massive_upgrades": _massive_upgrades,
        "massive_earnings": _massive_earnings,
        # FinViz (fallback — chart-pattern screens only)
        "finviz_double_bottom": _finviz_double_bottom,
        "finviz_multiple_tops": _finviz_multiple_tops,
        # yfinance server-side screeners (supplementary)
        "yf_most_actives": _yf_most_actives,
        "yf_day_gainers": _yf_day_gainers,
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

    with ThreadPoolExecutor(max_workers=20) as executor:
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

    if len(combined) < 1000:  # Premium: increased threshold from 600
        fallback = _static_active_stocks()
        results_by_source["static_fallback"] = fallback
        for t in fallback:
            if t not in seen:
                seen.add(t)
                combined.append(t)

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
    return combined[:max_total] if max_total < len(combined) else combined


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
        tickers = []
        for coin in data:
            sym = coin.get("symbol", "").lower()
            if sym in _CRYPTO_EXCLUDE:
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
            if sym and sym not in _CRYPTO_EXCLUDE:
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


def get_daytrade_candidates() -> tuple[list[str], int]:
    """Return (tickers, total_found) suited for intraday / day-trade scanning.

    Combines high-activity Massive.com snapshot filters (most active,
    top gainers, unusual volume, momentum gappers) plus yfinance screens
    and crypto movers for stocks that are moving *today*.
    Returns the **full** deduplicated universe; downstream callers decide
    how many to score.  Cached for 15 minutes (shorter TTL than swing).
    """
    cached = _cache_get("daytrade_candidates")
    if cached is not None:
        tickers, total = cached
        logger.info(f"[prescreener] Returning {len(tickers)} cached day-trade candidates")
        return tickers, total

    start = time.time()
    sources: dict[str, Any] = {
        "massive_most_active": _massive_most_active,
        "massive_top_gainers": _massive_top_gainers,
        "massive_unusual_volume": _massive_unusual_volume,
        "massive_momentum_gappers": _massive_momentum_gappers,
        "yf_most_actives": _yf_most_actives,
        "yf_day_gainers": _yf_day_gainers,
        "yf_small_cap_gainers": _yf_small_cap_gainers,
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

    with ThreadPoolExecutor(max_workers=20) as executor:
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

    total_found = len(combined)
    elapsed = time.time() - start
    logger.info(f"[prescreener] Day-trade pre-screen: {total_found} candidates in {elapsed:.1f}s")

    with _cache_lock:
        _cache["daytrade_candidates"] = (time.time(), (combined, total_found))
    return combined, total_found


# ── Breakout Candidates ───────────────────────────────────────────────

def _massive_low_volatility() -> list[str]:
    """Low-volatility stocks with decent volume (potential breakout buildup)."""
    from ..massive_client import get_full_market_snapshot
    snaps = get_full_market_snapshot()
    if not snaps:
        return []
    scored: list[tuple[str, float]] = []
    for s in snaps:
        day = s.get("day") or {}
        h, l, c = day.get("h", 0), day.get("l", 0), day.get("c", 0)
        v = day.get("v", 0)
        if c < 5.0 or v < 500_000 or h <= 0 or l <= 0:
            continue
        volatility = (h - l) / c
        if volatility < 0.03:
            scored.append((s.get("ticker", ""), volatility))
    scored.sort(key=lambda x: x[1])
    return [t for t, _ in scored[:150]]


def _finviz_channel_up() -> list[str]:
    """Chart-pattern fallback: rising channel."""
    return _finviz_screen(signal="Channel Up", limit=100)


def _finviz_wedge() -> list[str]:
    """Chart-pattern fallback: wedge."""
    return _finviz_screen(signal="Wedge", limit=100)


def _massive_near_52w_high() -> list[str]:
    """Stocks whose current price is within 5% of their recent high."""
    from ..massive_client import get_full_market_snapshot
    snaps = get_full_market_snapshot()
    if not snaps:
        return []
    hits: list[tuple[str, float]] = []
    for s in snaps:
        day = s.get("day") or {}
        prev = s.get("prevDay") or {}
        c = day.get("c", 0)
        h = day.get("h", 0)
        v = day.get("v", 0)
        if c < 5.0 or v < 300_000:
            continue
        recent_max = max(h, prev.get("h", 0))
        if recent_max > 0 and c >= recent_max * 0.95:
            hits.append((s.get("ticker", ""), c / recent_max))
    hits.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in hits[:150]]


def get_breakout_candidates() -> tuple[list[str], int]:
    """Return (tickers, total_found) suited for breakout / consolidation scanning.

    Combines low-volatility stocks near resistance, channel patterns,
    wedges, near-52-week-high stocks, and crypto (which can consolidate
    and break out just like equities).  Returns the full universe;
    downstream callers decide how many to score.  Cached for 30 minutes.
    """
    cached = _cache_get("breakout_candidates")
    if cached is not None:
        tickers, total = cached
        logger.info(f"[prescreener] Returning {len(tickers)} cached breakout candidates")
        return tickers, total

    start = time.time()
    sources: dict[str, Any] = {
        "massive_low_volatility": _massive_low_volatility,
        "massive_near_52w_high": _massive_near_52w_high,
        "massive_new_high": _massive_new_high,
        # Chart-pattern fallback (FinViz — graceful on 429)
        "finviz_channel_up": _finviz_channel_up,
        "finviz_wedge": _finviz_wedge,
        "crypto_base": _crypto_candidates,
        "crypto_trending": get_trending_crypto,
    }

    seen: set[str] = set()
    combined: list[str] = []

    core = _core_tickers()
    for t in core:
        if t not in seen:
            seen.add(t)
            combined.append(t)

    with ThreadPoolExecutor(max_workers=20) as executor:
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

    total_found = len(combined)
    crypto_count = sum(1 for t in combined if t.endswith("-USD"))
    elapsed = time.time() - start
    logger.info(f"[prescreener] Breakout pre-screen: {total_found} candidates ({crypto_count} crypto) in {elapsed:.1f}s")

    _cache_set("breakout_candidates", (combined, total_found))
    return combined, total_found
