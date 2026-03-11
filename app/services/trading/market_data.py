"""Market data: OHLCV, quotes, search, and technical indicators."""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd
from ..yf_session import (
    get_ticker as _yf_ticker,
    get_history as _yf_history,
    get_fast_info as _yf_fast_info,
    acquire as _yf_acquire,
)

logger = logging.getLogger(__name__)


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
    if period in allowed:
        return period
    return _INTERVAL_DEFAULT_PERIOD.get(interval, allowed[-1])


# ── OHLCV ─────────────────────────────────────────────────────────────

def fetch_ohlcv(
    ticker: str,
    interval: str = "1d",
    period: str = "6mo",
) -> list[dict[str, Any]]:
    """Fetch OHLCV candle data from Yahoo Finance."""
    if interval not in _VALID_INTERVALS:
        interval = "1d"
    if period not in _VALID_PERIODS:
        period = "6mo"
    period = _clamp_period(interval, period)

    df = _yf_history(ticker, period=period, interval=interval)

    if df.empty:
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
    return records


# ── Quote ──────────────────────────────────────────────────────────────

def fetch_quote(ticker: str) -> dict[str, Any] | None:
    """Current price + enriched info for a ticker (single API call)."""
    fi = _yf_fast_info(ticker)
    if fi is None or fi.get("last_price") is None:
        return None
    price = fi["last_price"]
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


def fetch_quotes_batch(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch quotes for multiple tickers efficiently.

    Uses batch_download to pre-warm history + quote caches in a single HTTP
    request, then reads individual quotes from the now-warm cache.
    """
    from ..yf_session import batch_download

    batch_download(tickers, period="3mo", interval="1d")

    results: dict[str, dict[str, Any]] = {}
    for t in tickers:
        q = fetch_quote(t)
        if q and q.get("price"):
            results[t] = q
    return results


# ── Ticker search ─────────────────────────────────────────────────────

def search_tickers(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Search for tickers matching a query string."""
    try:
        _yf_acquire()
        results = yf.search(query, max_results=limit)
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

def compute_indicators(
    ticker: str,
    interval: str = "1d",
    period: str = "6mo",
    indicators: list[str] | None = None,
) -> dict[str, Any]:
    """Compute requested technical indicators for a ticker.

    Uses the ``ta`` library (technical-analysis).  Returns a dict keyed by
    indicator name, each value a list of {time, value} or multi-key dicts.
    """
    if indicators is None:
        indicators = ["rsi", "macd", "sma_20", "ema_20", "bbands"]
    period = _clamp_period(interval, period)

    df = _yf_history(ticker, period=period, interval=interval)
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
    from ta.momentum import RSIIndicator, StochRSIIndicator, StochasticOscillator, WilliamsRIndicator
    from ta.trend import MACD, SMAIndicator, EMAIndicator, ADXIndicator, PSARIndicator, CCIIndicator
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

    return None


def _series_to_records(timestamps: list[int], s: pd.Series, key: str) -> list[dict]:
    out = []
    for ts, val in zip(timestamps, s):
        if pd.notna(val):
            out.append({"time": ts, key: round(float(val), 4)})
    return out


def get_indicator_snapshot(ticker: str, interval: str = "1d") -> dict[str, Any]:
    """Get latest indicator values (used for journal snapshots and AI context).

    Includes stochastic, EMA 20/50/100 for pattern mining compatibility.
    """
    result = compute_indicators(
        ticker, interval=interval, period="3mo",
        indicators=["rsi", "macd", "sma_20", "ema_20", "ema_50", "ema_100",
                     "bbands", "stoch", "adx", "atr", "obv"],
    )
    snapshot: dict[str, Any] = {"ticker": ticker, "interval": interval}
    for ind_name, records in result.items():
        if records:
            latest = records[-1]
            snapshot[ind_name] = {k: v for k, v in latest.items() if k != "time"}
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
        df = _yf_history("SPY", period="10d", interval="1d")
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
