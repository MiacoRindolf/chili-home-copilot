"""Rate-limited yfinance wrapper with in-memory response caching.

Modern yfinance (>=0.2.40) uses curl_cffi internally and rejects injected
requests-cache sessions.  Instead we rate-limit and cache at the wrapper level:

- A ``pyrate_limiter.Limiter`` gates all Yahoo Finance requests to 12 per 5 seconds.
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
            _limiter = Limiter(Rate(12, Duration.SECOND * 5))
            logger.info("[yf_session] Rate limiter active (12 req/5s)")
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

_TTL_HISTORY = 3600    # 1 hour for OHLCV / indicator data (64 GB RAM)
_TTL_QUOTE = 30        # 30 seconds for live price
_TTL_SEARCH = 3600     # 1 hour for search results
_TTL_FUNDAMENTALS = 86400  # 24 hours for fundamental data
_TTL_TICKER_INFO = 3600   # 1 hour for ticker info strip
_TTL_NEWS = 600        # 10 minutes for ticker news
_TTL_DEAD = 14400      # 4 hours for known-bad tickers
_MAX_CACHE_SIZE = 10_000   # 64 GB RAM — keep much more in memory

_dead_tickers: dict[str, float] = {}
_dead_lock = threading.Lock()


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
    if key.startswith("fund:"):
        return _TTL_FUNDAMENTALS
    if key.startswith("ticker_info:"):
        return _TTL_TICKER_INFO
    if key.startswith("news:"):
        return _TTL_NEWS
    return _TTL_HISTORY


def _is_dead(symbol: str) -> bool:
    """Check if a ticker is in the negative cache (known bad)."""
    with _dead_lock:
        ts = _dead_tickers.get(symbol)
        if ts is None:
            return False
        if time.time() - ts > _TTL_DEAD:
            del _dead_tickers[symbol]
            return False
        return True


def _mark_dead(symbol: str) -> None:
    """Add ticker to the negative cache after confirmed failure."""
    with _dead_lock:
        _dead_tickers[symbol] = time.time()
    logger.info(f"[yf_session] Marked {symbol} as dead (skip for {_TTL_DEAD}s)")


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

    Returns a DataFrame (possibly empty on error). Skips known-dead tickers.
    """
    import pandas as pd

    if _is_dead(symbol):
        return pd.DataFrame()

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

    if df.empty:
        _mark_dead(symbol)

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
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if _is_dead(symbol) and symbol.upper().endswith("-USD"):
        result = _coingecko_quote(symbol)
        if result:
            _cache_set(cache_key, result)
        return result

    if _is_dead(symbol):
        return None

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
            "year_high": float(info.year_high) if hasattr(info, "year_high") and info.year_high else None,
            "year_low": float(info.year_low) if hasattr(info, "year_low") and info.year_low else None,
            "avg_volume": int(info.three_month_average_volume) if hasattr(info, "three_month_average_volume") and info.three_month_average_volume else None,
        }
    except Exception as e:
        logger.warning(f"[yf_session] fast_info({symbol}) failed: {e}")
        result = None

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


def _coingecko_quote(symbol: str) -> dict[str, Any] | None:
    """Fallback: fetch price from CoinGecko for crypto tickers yfinance can't resolve."""
    try:
        import requests
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
                search_resp = requests.get(
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
        resp = requests.get(
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
    for sym in symbols:
        if _is_dead(sym):
            continue
        key = f"hist:{sym}:{period}:{interval}:None"
        cached = _cache_get(key)
        if cached is not None:
            result[sym] = cached
        else:
            uncached.append(sym)

    if not uncached:
        return result

    acquire()
    try:
        df = yf.download(uncached, period=period, interval=interval, group_by="ticker",
                         threads=True, progress=False)
    except Exception as e:
        logger.warning(f"[yf_session] batch_download failed: {e}")
        return result

    if df.empty:
        return result

    if len(uncached) == 1:
        sym = uncached[0]
        key = f"hist:{sym}:{period}:{interval}:None"
        _cache_set(key, df)
        result[sym] = df
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
                        key = f"hist:{sym}:{period}:{interval}:None"
                        _cache_set(key, ticker_df)
                        result[sym] = ticker_df
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

    logger.info(f"[yf_session] batch_download: {len(uncached)} requested, {len(result)} returned")
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

    acquire()
    try:
        t = yf.Ticker(symbol)
        info = t.info
        if not info or not info.get("shortName"):
            _cache_set(cache_key, _FUND_EMPTY)
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
        return None

    _cache_set(cache_key, result)
    return result


def get_ticker_info(symbol: str) -> dict[str, Any] | None:
    """Compact ticker metadata for the detail strip: name, sector/type, mcap, P/E, description.

    Works for both stocks (sector, industry) and crypto (category). Cached 1 hour.
    """
    cache_key = f"ticker_info:{symbol}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    acquire()
    try:
        t = yf.Ticker(symbol)
        info = t.info
        if not info:
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
        return result
    except Exception as e:
        logger.debug(f"[yf_session] ticker_info({symbol}) failed: {e}")
        return None


def get_ticker_news(symbol: str, limit: int = 5) -> list[dict[str, Any]]:
    """News for the given ticker. Uses yfinance Ticker.news; fallback DDGS news search."""
    cache_key = f"news:{symbol}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    out: list[dict[str, Any]] = []
    try:
        acquire()
        t = yf.Ticker(symbol)
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
    except Exception as e:
        logger.debug(f"[yf_session] ticker news({symbol}) failed: {e}")

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
