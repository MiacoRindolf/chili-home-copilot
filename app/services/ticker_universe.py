"""Dynamic ticker universe: ALL US-listed stocks + top crypto, with local caching."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "ticker_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_STOCKS_CACHE = _CACHE_DIR / "us_stocks.json"
_CRYPTO_CACHE = _CACHE_DIR / "crypto_top.json"
_CACHE_MAX_AGE = timedelta(days=7)

_memory_cache: dict[str, Any] = {}


# ── US Stocks from SEC EDGAR ────────────────────────────────────────────

_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SEC_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"

_MAJOR_EXCHANGES = {"NYSE", "NASDAQ", "Nasdaq", "Nyse", "AMEX", "Amex", "BATS", "ARCA"}


_SEC_HEADERS = {
    "User-Agent": "CHILI-HomeCopilot admin@chili-app.local",
    "Accept-Encoding": "gzip, deflate",
}

# Wikipedia tables for reliable fallback
_WIKI_SP500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_WIKI_NASDAQ100 = "https://en.wikipedia.org/wiki/Nasdaq-100"

# Comprehensive built-in list: S&P 500 core + popular mid/small caps + sector leaders
_BUILTIN_US_TICKERS = [
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA", "AVGO", "ORCL",
    "CRM", "ADBE", "AMD", "INTC", "QCOM", "TXN", "NFLX", "CSCO", "IBM", "NOW",
    "INTU", "AMAT", "LRCX", "MU", "KLAC", "MRVL", "SNPS", "CDNS", "PANW", "CRWD",
    "FTNT", "ZS", "DDOG", "NET", "SNOW", "PLTR", "SHOP", "SQ", "PYPL", "COIN",
    "UBER", "ABNB", "DASH", "RBLX", "U", "TTD", "PINS", "SNAP", "ROKU", "SPOT",
    # Finance
    "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "C", "AXP", "BLK",
    "SCHW", "CME", "ICE", "MCO", "SPGI", "COF", "DFS", "SYF", "ALLY", "HOOD",
    # Healthcare
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR", "BMY",
    "AMGN", "GILD", "VRTX", "REGN", "ISRG", "SYK", "MDT", "BSX", "EW", "ZTS",
    "MRNA", "BNTX", "DXCM", "ILMN", "ALGN", "HOLX", "IQV", "CNC", "HCA", "CI",
    # Consumer
    "WMT", "COST", "HD", "LOW", "TGT", "AMZN", "PG", "KO", "PEP", "MCD",
    "SBUX", "NKE", "LULU", "TJX", "ROST", "DG", "DLTR", "YUM", "CMG", "DPZ",
    "EL", "CL", "KMB", "GIS", "K", "HSY", "MDLZ", "STZ", "BF-B", "TAP",
    # Industrial
    "CAT", "DE", "HON", "UPS", "FDX", "BA", "LMT", "RTX", "GE", "MMM",
    "EMR", "ETN", "ROK", "IR", "CMI", "PH", "ITW", "DOV", "SWK", "GD",
    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "VLO", "PSX", "OXY", "HAL",
    "DVN", "FANG", "PXD", "HES", "BKR", "KMI", "WMB", "OKE", "TRGP", "LNG",
    # Real Estate / REITs
    "PLD", "AMT", "CCI", "EQIX", "SPG", "O", "DLR", "PSA", "WELL", "AVB",
    # Telecom / Media
    "DIS", "CMCSA", "T", "VZ", "TMUS", "CHTR", "WBD", "PARA", "FOX", "LYV",
    # Materials
    "LIN", "APD", "SHW", "ECL", "FCX", "NEM", "NUE", "STLD", "CF", "MOS",
    # Utilities
    "NEE", "DUK", "SO", "D", "AEP", "SRE", "EXC", "XEL", "ED", "WEC",
    # ETFs (for market tracking)
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "ARKK", "XLF", "XLE", "XLK",
    # Popular small/mid caps & growth
    "SOFI", "AFRM", "UPST", "RIVN", "LCID", "NIO", "XPEV", "LI", "IONQ",
    "SMCI", "ARM", "CELH", "DUOL", "MNST", "ENPH", "SEDG", "FSLR", "RUN",
    "DKNG", "PENN", "MGM", "WYNN", "LVS", "MAR", "HLT", "EXPE", "BKNG",
    "WDAY", "VEEV", "HUBS", "TEAM", "MDB", "ESTC", "CFLT", "PATH", "BILL",
    "ZM", "DOCU", "OKTA", "TWLO", "GTLB", "MNDY", "FROG", "TOST", "CAVA",
]


def _fetch_sec_tickers() -> list[dict[str, str]]:
    """Fetch all US-listed stock tickers from SEC EDGAR."""

    # Try the exchange-filtered endpoint first
    try:
        resp = requests.get(_SEC_EXCHANGE_URL, headers=_SEC_HEADERS, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            fields = data.get("fields", [])
            rows = data.get("data", [])

            if fields and rows:
                ticker_idx = fields.index("ticker") if "ticker" in fields else 1
                name_idx = fields.index("name") if "name" in fields else 2
                exchange_idx = fields.index("exchange") if "exchange" in fields else 3

                tickers = []
                seen = set()
                for row in rows:
                    ticker = str(row[ticker_idx]).upper().strip()
                    exchange = str(row[exchange_idx]) if exchange_idx < len(row) else ""

                    if not ticker or ticker in seen:
                        continue
                    if len(ticker) > 5:
                        continue
                    if any(c in ticker for c in [".", "^", "/"]):
                        continue
                    if exchange and not any(ex in exchange for ex in _MAJOR_EXCHANGES):
                        continue

                    seen.add(ticker)
                    tickers.append({
                        "ticker": ticker,
                        "name": str(row[name_idx]) if name_idx < len(row) else "",
                        "exchange": exchange,
                    })

                logger.info(f"[ticker_universe] Fetched {len(tickers)} US tickers from SEC (exchange endpoint)")
                return tickers
    except Exception as e:
        logger.warning(f"[ticker_universe] SEC exchange endpoint failed: {e}")

    # Fallback: basic SEC tickers endpoint
    try:
        resp = requests.get(_SEC_TICKERS_URL, headers=_SEC_HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        tickers = []
        seen = set()
        for entry in data.values():
            ticker = str(entry.get("ticker", "")).upper().strip()
            if not ticker or ticker in seen:
                continue
            if len(ticker) > 5 or any(c in ticker for c in [".", "^", "/"]):
                continue
            seen.add(ticker)
            tickers.append({
                "ticker": ticker,
                "name": entry.get("title", ""),
                "exchange": "",
            })

        logger.info(f"[ticker_universe] Fetched {len(tickers)} US tickers from SEC (basic endpoint)")
        return tickers
    except Exception as e:
        logger.warning(f"[ticker_universe] SEC basic endpoint failed: {e}")

    # Fallback 2: use the comprehensive built-in list
    logger.info(f"[ticker_universe] Using built-in list of {len(_BUILTIN_US_TICKERS)} US tickers")
    return [{"ticker": t, "name": "", "exchange": ""} for t in _BUILTIN_US_TICKERS]


# ── Top Crypto from CoinGecko ──────────────────────────────────────────

_COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"

_STATIC_CRYPTO_TOP100 = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "DOT",
    "LINK", "MATIC", "ATOM", "LTC", "NEAR", "FIL", "ARB",
    "OP", "ICP", "HBAR", "VET", "ALGO", "MANA", "SAND", "AXS", "AAVE",
    "GRT", "MKR", "SNX", "CRV", "COMP", "LDO", "RPL", "FTM", "RUNE",
    "INJ", "SEI", "SHIB",
    "FET", "STX", "IMX", "GALA",
    "ENJ", "CHZ", "BAT", "ZRX", "1INCH", "SUSHI", "YFI", "UMA",
    "CELO", "ROSE", "FLOW", "MINA", "KAVA",
    "EGLD", "QNT", "THETA", "XTZ", "EOS", "NEO", "IOTA", "ZEC",
    "DASH", "XMR", "KSM", "ZIL", "ENS",
    "DYDX", "GMX", "CAKE", "PENDLE",
    "JASMY", "TRX", "TON", "RENDER",
]

_CRYPTO_BLACKLIST = {
    "USDT", "USDC", "USDS", "USDE", "USDTB", "USD1", "USDY", "USYC",
    "DAI", "FDUSD", "TUSD", "BUSD", "GUSD", "FRAX", "LUSD", "PYUSD",
    "BUIDL", "OUSG", "FIGR_HELOC", "WBTC", "WETH", "WSTETH", "STETH",
    "CC", "HYPE", "PI", "HASH", "POL", "MNT", "TAO", "PEPE",
    "UNI", "APT", "SUI", "BONK", "WIF", "FLOKI", "AGIX", "OCEAN",
    "OSMO", "JUNO", "WAVES", "LQTY", "BLUR", "JOE", "PYTH", "JTO",
    "W", "STRK", "ONDO", "ENA", "ETHFI", "EIGEN", "SAFE", "ZRO",
    "AERO", "TIA", "MORPHO",
}


def _fetch_crypto_tickers(n: int = 100) -> list[dict[str, str]]:
    """Fetch top N crypto tickers by market cap from CoinGecko."""
    try:
        resp = requests.get(
            _COINGECKO_URL,
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": n,
                "page": 1,
                "sparkline": "false",
            },
            timeout=20,
        )
        if resp.status_code == 200:
            coins = resp.json()
            tickers = []
            for coin in coins:
                symbol = coin.get("symbol", "").upper()
                if symbol and symbol not in _CRYPTO_BLACKLIST and len(symbol) <= 6:
                    tickers.append({
                        "ticker": f"{symbol}-USD",
                        "name": coin.get("name", ""),
                        "type": "crypto",
                    })
            logger.info(f"[ticker_universe] Fetched {len(tickers)} crypto tickers from CoinGecko (filtered)")
            return tickers
    except Exception as e:
        logger.warning(f"[ticker_universe] CoinGecko failed: {e}, using static list")

    return [
        {"ticker": f"{s}-USD", "name": s, "type": "crypto"}
        for s in _STATIC_CRYPTO_TOP100[:n]
    ]


# ── Cache Management ───────────────────────────────────────────────────

def _load_cache(path: Path) -> list[dict] | None:
    if not path.exists():
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if datetime.now() - mtime > _CACHE_MAX_AGE:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_cache(path: Path, data: list[dict]) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[ticker_universe] Cache write failed: {e}")


# ── Public API ──────────────────────────────────────────────────────────

def get_all_us_stock_tickers(force_refresh: bool = False) -> list[str]:
    """Return list of ALL US stock ticker strings. Cached for 7 days."""
    cache_key = "us_stocks"
    if not force_refresh and cache_key in _memory_cache:
        return _memory_cache[cache_key]

    if not force_refresh:
        cached = _load_cache(_STOCKS_CACHE)
        if cached:
            tickers = [t["ticker"] for t in cached]
            _memory_cache[cache_key] = tickers
            logger.info(f"[ticker_universe] Loaded {len(tickers)} US stocks from cache")
            return tickers

    entries = _fetch_sec_tickers()
    if entries:
        _save_cache(_STOCKS_CACHE, entries)
        tickers = [t["ticker"] for t in entries]
        _memory_cache[cache_key] = tickers
        return tickers

    # _fetch_sec_tickers always returns the built-in list as last resort,
    # but just in case:
    return list(_BUILTIN_US_TICKERS)


def get_all_crypto_tickers(n: int = 100, force_refresh: bool = False) -> list[str]:
    """Return list of top N crypto tickers (with -USD suffix). Cached for 7 days."""
    cache_key = "crypto"
    if not force_refresh and cache_key in _memory_cache:
        return _memory_cache[cache_key][:n]

    if not force_refresh:
        cached = _load_cache(_CRYPTO_CACHE)
        if cached:
            tickers = [t["ticker"] for t in cached][:n]
            _memory_cache[cache_key] = tickers
            logger.info(f"[ticker_universe] Loaded {len(tickers)} crypto from cache")
            return tickers

    entries = _fetch_crypto_tickers(n)
    if entries:
        _save_cache(_CRYPTO_CACHE, entries)
        tickers = [t["ticker"] for t in entries]
        _memory_cache[cache_key] = tickers
        return tickers[:n]

    return [f"{s}-USD" for s in _STATIC_CRYPTO_TOP100[:n]]


def get_full_ticker_universe(force_refresh: bool = False) -> list[str]:
    """Return the full scanning universe: all US stocks + top 100 crypto."""
    stocks = get_all_us_stock_tickers(force_refresh=force_refresh)
    crypto = get_all_crypto_tickers(n=100, force_refresh=force_refresh)
    combined = list(dict.fromkeys(stocks + crypto))
    logger.info(f"[ticker_universe] Full universe: {len(stocks)} stocks + {len(crypto)} crypto = {len(combined)} total")
    return combined


def get_ticker_count() -> dict[str, int]:
    """Get counts for display purposes."""
    stocks = get_all_us_stock_tickers()
    crypto = get_all_crypto_tickers()
    return {
        "stocks": len(stocks),
        "crypto": len(crypto),
        "total": len(stocks) + len(crypto),
    }


def refresh_ticker_cache() -> dict[str, int]:
    """Force refresh the entire ticker cache."""
    _memory_cache.clear()
    stocks = get_all_us_stock_tickers(force_refresh=True)
    crypto = get_all_crypto_tickers(force_refresh=True)
    return {
        "stocks": len(stocks),
        "crypto": len(crypto),
        "total": len(stocks) + len(crypto),
    }
