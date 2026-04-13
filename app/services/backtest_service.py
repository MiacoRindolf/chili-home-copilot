"""Backtesting engine: pre-built strategies powered by backtesting.py."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from backtesting import Backtest, Strategy
from backtesting.lib import FractionalBacktest, crossover
from sqlalchemy.orm import Session

from .trading.market_data import fetch_ohlcv_df as _fetch_ohlcv_df
from .trading.research_integrity import (
    enrich_generic_backtest_result as _enrich_generic_bt_result,
    enrich_pattern_backtest_result as _enrich_pattern_bt_result,
)

from ..config import settings
from ..models.trading import BacktestResult, ScanPattern
from .trading.research_kpis import build_research_kpis
from .trading.scan_pattern_label_alignment import strategy_label_aligns_scan_pattern_name

logger = logging.getLogger(__name__)


# ── Helper indicators ───────────────────────────────────────────────────

def _sma(values, n):
    return pd.Series(values).rolling(n).mean()


def _ema(values, n):
    return pd.Series(values).ewm(span=n, adjust=False).mean()


def _rsi(values, n=14):
    s = pd.Series(values)
    delta = s.diff()
    gain = delta.where(delta > 0, 0.0).rolling(n).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(n).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def _bollinger_upper(values, n=20, std=2):
    s = pd.Series(values)
    return s.rolling(n).mean() + std * s.rolling(n).std()


def _bollinger_lower(values, n=20, std=2):
    s = pd.Series(values)
    return s.rolling(n).mean() - std * s.rolling(n).std()


def _bollinger_mid(values, n=20):
    return pd.Series(values).rolling(n).mean()


# ── Pre-built strategies ────────────────────────────────────────────────

class SmaCrossover(Strategy):
    """Buy when fast SMA crosses above slow SMA, sell on cross below."""
    fast = 10
    slow = 30

    def init(self):
        self.sma_fast = self.I(_sma, self.data.Close, self.fast)
        self.sma_slow = self.I(_sma, self.data.Close, self.slow)

    def next(self):
        if crossover(self.sma_fast, self.sma_slow):
            self.buy()
        elif crossover(self.sma_slow, self.sma_fast):
            self.position.close()


class EmaCrossover(Strategy):
    """Buy when fast EMA crosses above slow EMA."""
    fast = 12
    slow = 26

    def init(self):
        self.ema_fast = self.I(_ema, self.data.Close, self.fast)
        self.ema_slow = self.I(_ema, self.data.Close, self.slow)

    def next(self):
        if crossover(self.ema_fast, self.ema_slow):
            self.buy()
        elif crossover(self.ema_slow, self.ema_fast):
            self.position.close()


class RsiReversal(Strategy):
    """Buy when RSI drops below oversold, sell when RSI rises above overbought."""
    rsi_period = 14
    oversold = 30
    overbought = 70

    def init(self):
        self.rsi = self.I(_rsi, self.data.Close, self.rsi_period)

    def next(self):
        if self.rsi[-1] < self.oversold and not self.position:
            self.buy()
        elif self.rsi[-1] > self.overbought and self.position:
            self.position.close()


class BollingerBounce(Strategy):
    """Buy at lower Bollinger Band, sell at upper band."""
    bb_period = 20
    bb_std = 2

    def init(self):
        self.upper = self.I(_bollinger_upper, self.data.Close, self.bb_period, self.bb_std)
        self.lower = self.I(_bollinger_lower, self.data.Close, self.bb_period, self.bb_std)
        self.mid = self.I(_bollinger_mid, self.data.Close, self.bb_period)

    def next(self):
        if self.data.Close[-1] <= self.lower[-1] and not self.position:
            self.buy()
        elif self.data.Close[-1] >= self.upper[-1] and self.position:
            self.position.close()


class MacdStrategy(Strategy):
    """Buy on MACD line crossing above signal, sell on cross below."""
    fast = 12
    slow = 26
    signal = 9

    def init(self):
        ema_fast = self.I(_ema, self.data.Close, self.fast)
        ema_slow = self.I(_ema, self.data.Close, self.slow)
        self.macd_line = self.I(lambda: pd.Series(ema_fast) - pd.Series(ema_slow))
        self.signal_line = self.I(_ema, self.macd_line, self.signal)

    def next(self):
        if crossover(self.macd_line, self.signal_line):
            self.buy()
        elif crossover(self.signal_line, self.macd_line):
            self.position.close()


class TrendFollowing(Strategy):
    """Combined trend: buy when price > SMA50, RSI > 50, and fast EMA > slow EMA."""
    sma_len = 50
    ema_fast = 12
    ema_slow = 26

    def init(self):
        self.sma = self.I(_sma, self.data.Close, self.sma_len)
        self.ef = self.I(_ema, self.data.Close, self.ema_fast)
        self.es = self.I(_ema, self.data.Close, self.ema_slow)
        self.rsi = self.I(_rsi, self.data.Close, 14)

    def next(self):
        price = self.data.Close[-1]
        if (price > self.sma[-1] and self.ef[-1] > self.es[-1]
                and self.rsi[-1] > 50 and not self.position):
            self.buy()
        elif (price < self.sma[-1] or self.rsi[-1] < 40) and self.position:
            self.position.close()


class MomentumBreakout(Strategy):
    """Momentum breakout: RSI > threshold, price above EMA stack, volume surge.

    Models the pattern: strong RSI + full EMA alignment + volume confirmation.
    """
    rsi_threshold = 65
    ema_fast = 20
    ema_mid = 50
    ema_slow = 100
    atr_mult = 2.0

    def init(self):
        self.rsi = self.I(_rsi, self.data.Close, 14)
        self.ef = self.I(_ema, self.data.Close, self.ema_fast)
        self.em = self.I(_ema, self.data.Close, self.ema_mid)
        self.es = self.I(_ema, self.data.Close, self.ema_slow)

    def next(self):
        price = self.data.Close[-1]
        rsi_ok = self.rsi[-1] > self.rsi_threshold
        ema_stack = price > self.ef[-1] > self.em[-1] > self.es[-1]

        if rsi_ok and ema_stack and not self.position:
            self.buy()
        elif self.position:
            if self.rsi[-1] < 40 or price < self.em[-1]:
                self.position.close()


# ── Strategy registry ───────────────────────────────────────────────────

STRATEGIES: dict[str, dict[str, Any]] = {
    "sma_cross": {
        "cls": SmaCrossover,
        "name": "SMA Crossover",
        "description": "Buy when fast SMA crosses above slow SMA. Classic trend-following.",
        "params": {"fast": (5, 50, 5), "slow": (20, 100, 5)},
    },
    "ema_cross": {
        "cls": EmaCrossover,
        "name": "EMA Crossover",
        "description": "Buy when fast EMA crosses above slow EMA. More responsive than SMA.",
        "params": {"fast": (5, 30, 3), "slow": (15, 60, 3)},
    },
    "rsi_reversal": {
        "cls": RsiReversal,
        "name": "RSI Reversal",
        "description": "Buy when RSI signals oversold, sell when overbought. Mean-reversion.",
        "params": {"rsi_period": (7, 21, 2), "oversold": (20, 40, 5), "overbought": (60, 80, 5)},
    },
    "bb_bounce": {
        "cls": BollingerBounce,
        "name": "Bollinger Bounce",
        "description": "Buy at lower Bollinger Band, sell at upper. Range-bound strategy.",
        "params": {"bb_period": (10, 30, 5), "bb_std": (1.5, 3.0, 0.5)},
    },
    "macd": {
        "cls": MacdStrategy,
        "name": "MACD Crossover",
        "description": "Buy on MACD crossing signal line, sell on cross below. Momentum.",
        "params": {"fast": (8, 16, 2), "slow": (20, 32, 2), "signal": (5, 13, 2)},
    },
    "trend_follow": {
        "cls": TrendFollowing,
        "name": "Trend Following",
        "description": "Multi-indicator trend: SMA + EMA + RSI confluence for high-confidence entries.",
        "params": {"sma_len": (30, 80, 10), "ema_fast": (8, 20, 2), "ema_slow": (20, 40, 2)},
    },
    "momentum_breakout": {
        "cls": MomentumBreakout,
        "name": "Momentum Breakout",
        "description": "RSI momentum + full EMA stack alignment. Captures strong continuation breakouts.",
        "params": {
            "rsi_threshold": (55, 75, 5),
            "ema_fast": (10, 30, 5),
            "ema_mid": (30, 70, 10),
            "ema_slow": (80, 120, 10),
        },
    },
}


def _coerce_strategy_params(
    strat_cls: type,
    strategy_id: str,
    raw: dict[str, Any] | None,
) -> dict[str, Any]:
    """Filter and coerce user-provided params to types expected by backtesting.py."""
    if not raw:
        return {}
    meta = (STRATEGIES.get(strategy_id) or {}).get("params") or {}
    allowed = set(meta.keys())
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k not in allowed:
            continue
        default = getattr(strat_cls, k, None)
        spec = meta.get(k)
        try:
            if isinstance(default, bool):
                out[k] = bool(v)
            elif spec is not None and isinstance(spec[0], float):
                out[k] = float(v)
            elif isinstance(default, int) and not isinstance(default, bool):
                out[k] = int(v)
            elif isinstance(default, float):
                out[k] = float(v)
            elif isinstance(v, (int, float)):
                out[k] = int(v) if float(v) == int(float(v)) else float(v)
            else:
                out[k] = v
        except (TypeError, ValueError):
            continue
    return out


def list_strategies() -> list[dict[str, Any]]:
    """Return built-in strategies with optional tunable param metadata for the UI."""
    out: list[dict[str, Any]] = []
    for k, v in STRATEGIES.items():
        entry: dict[str, Any] = {
            "id": k,
            "name": v["name"],
            "description": v["description"],
            "kind": "builtin",
        }
        cls = v["cls"]
        params_meta = v.get("params")
        tunables: list[dict[str, Any]] = []
        if params_meta:
            for pname, spec in params_meta.items():
                lo, hi, step = spec[0], spec[1], spec[2]
                default = getattr(cls, pname, lo)
                tunables.append(
                    {
                        "name": pname,
                        "min": lo,
                        "max": hi,
                        "step": step,
                        "default": int(default) if isinstance(default, (int, float)) and float(default) == int(default) else default,
                    }
                )
        entry["tunables"] = tunables
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Auto-generate human-readable strategy name from conditions
# ---------------------------------------------------------------------------

_INDICATOR_LABELS: dict[str, str] = {
    "rsi_14": "RSI", "adx": "ADX", "macd_hist": "MACD",
    "price": "Price", "rel_vol": "RelVol", "gap_pct": "Gap%",
    "stoch_k": "Stoch", "bb_squeeze": "BB-Squeeze",
    "bb_squeeze_firing": "BB-Fire",
    "ema_9": "EMA9", "ema_20": "EMA20", "ema_50": "EMA50",
    "ema_100": "EMA100", "sma_20": "SMA20", "sma_50": "SMA50",
    "sma_100": "SMA100", "sma_200": "SMA200",
    "bb_upper": "BB-Up", "bb_lower": "BB-Low",
    "daily_change_pct": "DayChg%", "vwap_reclaim": "VWAP",
    "resistance_retests": "ResRetest", "dist_to_resistance_pct": "Dist-Res%",
    "narrow_range": "NR", "vcp_count": "VCP",
    "bullish_engulfing": "BullEngulf", "bearish_engulfing": "BearEngulf",
    "hammer": "Hammer", "inverted_hammer": "InvHammer",
    "morning_star": "MornStar", "doji": "Doji",
}


def generate_strategy_name(conditions: list[dict[str, Any]]) -> str:
    """Build a concise, human-readable strategy name from a conditions list."""
    if not conditions:
        return "Dynamic"
    parts: list[str] = []
    for c in conditions:
        ind = c.get("indicator", "")
        op = c.get("op", "")
        val = c.get("value")
        label = _INDICATOR_LABELS.get(ind, ind)
        if isinstance(val, str):
            val_label = _INDICATOR_LABELS.get(val, val.upper())
            parts.append(f"{label}{op}{val_label}")
        elif isinstance(val, list) and len(val) == 2:
            parts.append(f"{label} {val[0]}-{val[1]}")
        elif op == "==" and val == 1:
            parts.append(label)
        else:
            v = int(val) if isinstance(val, float) and val == int(val) else val
            parts.append(f"{label}{op}{v}")
    return " + ".join(parts[:6])


def _extract_indicators(
    strat_cls,
    strategy_id: str,
    df: pd.DataFrame,
    strategy_params: dict[str, Any] | None = None,
) -> dict:
    """Compute indicator overlays for charting based on the strategy used."""
    result = {}
    close = df["Close"]
    timestamps = [int(pd.Timestamp(ts).timestamp()) for ts in df.index]

    def _series_to_points(series):
        pts = []
        for i, val in enumerate(series):
            if pd.notna(val):
                pts.append({"time": timestamps[i], "value": round(float(val), 4)})
        return pts

    if strategy_id == "sma_cross":
        fast = int((strategy_params or {}).get("fast", getattr(strat_cls, "fast", 10)))
        slow = int((strategy_params or {}).get("slow", getattr(strat_cls, "slow", 30)))
        result[f"SMA {fast}"] = _series_to_points(_sma(close, fast))
        result[f"SMA {slow}"] = _series_to_points(_sma(close, slow))
    elif strategy_id == "ema_cross":
        fast = int((strategy_params or {}).get("fast", getattr(strat_cls, "fast", 12)))
        slow = int((strategy_params or {}).get("slow", getattr(strat_cls, "slow", 26)))
        result[f"EMA {fast}"] = _series_to_points(_ema(close, fast))
        result[f"EMA {slow}"] = _series_to_points(_ema(close, slow))
    elif strategy_id == "rsi_reversal":
        rsi_n = int((strategy_params or {}).get("rsi_period", getattr(strat_cls, "rsi_period", 14)))
        result[f"RSI {rsi_n}"] = _series_to_points(_rsi(close, rsi_n))
    elif strategy_id == "bb_bounce":
        bb_n = int((strategy_params or {}).get("bb_period", getattr(strat_cls, "bb_period", 20)))
        bb_s = float((strategy_params or {}).get("bb_std", getattr(strat_cls, "bb_std", 2)))
        result["BB Upper"] = _series_to_points(_bollinger_upper(close, bb_n, bb_s))
        result["BB Mid"] = _series_to_points(_bollinger_mid(close, bb_n))
        result["BB Lower"] = _series_to_points(_bollinger_lower(close, bb_n, bb_s))
    elif strategy_id == "macd":
        mfast = int((strategy_params or {}).get("fast", getattr(strat_cls, "fast", 12)))
        mslow = int((strategy_params or {}).get("slow", getattr(strat_cls, "slow", 26)))
        msig = int((strategy_params or {}).get("signal", getattr(strat_cls, "signal", 9)))
        ema_f = _ema(close, mfast)
        ema_s = _ema(close, mslow)
        macd_line = pd.Series(ema_f.values - ema_s.values, index=close.index)
        signal_line = _ema(macd_line, msig)
        histogram = macd_line - signal_line
        result["MACD"] = _series_to_points(macd_line)
        result["Signal"] = _series_to_points(signal_line)
        result["Histogram"] = _series_to_points(histogram)
    elif strategy_id == "trend_follow":
        sma_n = int((strategy_params or {}).get("sma_len", getattr(strat_cls, "sma_len", 50)))
        ef = int((strategy_params or {}).get("ema_fast", getattr(strat_cls, "ema_fast", 12)))
        es = int((strategy_params or {}).get("ema_slow", getattr(strat_cls, "ema_slow", 26)))
        result[f"SMA {sma_n}"] = _series_to_points(_sma(close, sma_n))
        result[f"EMA {ef}"] = _series_to_points(_ema(close, ef))
        result[f"EMA {es}"] = _series_to_points(_ema(close, es))
        result["RSI 14"] = _series_to_points(_rsi(close, 14))
    elif strategy_id == "momentum_breakout":
        ef = int((strategy_params or {}).get("ema_fast", getattr(strat_cls, "ema_fast", 20)))
        em = int((strategy_params or {}).get("ema_mid", getattr(strat_cls, "ema_mid", 50)))
        es = int((strategy_params or {}).get("ema_slow", getattr(strat_cls, "ema_slow", 100)))
        result["RSI 14"] = _series_to_points(_rsi(close, 14))
        result[f"EMA {ef}"] = _series_to_points(_ema(close, ef))
        result[f"EMA {em}"] = _series_to_points(_ema(close, em))
        result[f"EMA {es}"] = _series_to_points(_ema(close, es))

    return result


# ── Run backtest ────────────────────────────────────────────────────────

def run_backtest(
    ticker: str,
    strategy_id: str = "sma_cross",
    period: str = "1y",
    cash: float = 10000,
    commission: float = 0.001,
    optimize: bool = False,
    interval: str = "1d",
    strategy_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a backtest and return results dict."""
    if strategy_id not in STRATEGIES:
        return {"ok": False, "error": f"Unknown strategy: {strategy_id}"}

    df = _fetch_ohlcv_df(ticker, period=period, interval=interval)
    if df.empty or len(df) < 30:
        return {"ok": False, "error": f"Not enough data for {ticker}"}

    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    strat_info = STRATEGIES[strategy_id]
    strat_cls = strat_info["cls"]
    coerced = _coerce_strategy_params(strat_cls, strategy_id, strategy_params)

    bt = Backtest(
        df,
        strat_cls,
        cash=cash,
        commission=commission,
        exclusive_orders=True,
        finalize_trades=True,
    )

    if optimize and strat_info.get("params"):
        param_ranges = {}
        for param, (lo, hi, step) in strat_info["params"].items():
            if isinstance(lo, float):
                import numpy as np
                param_ranges[param] = list(np.arange(lo, hi + step, step))
            else:
                param_ranges[param] = range(lo, hi + 1, step)
        try:
            stats = bt.optimize(**param_ranges, maximize="Return [%]")
        except Exception:
            stats = bt.run(**coerced)
    else:
        stats = bt.run(**coerced)

    equity = stats.get("_equity_curve")
    equity_data = []
    if equity is not None and not equity.empty:
        for ts, row in equity.iterrows():
            equity_data.append({
                "time": int(pd.Timestamp(ts).timestamp()),
                "value": round(float(row["Equity"]), 2),
            })

    # OHLC candlestick data
    ohlc_data = []
    for ts, row in df.iterrows():
        ohlc_data.append({
            "time": int(pd.Timestamp(ts).timestamp()),
            "open": round(float(row["Open"]), 4),
            "high": round(float(row["High"]), 4),
            "low": round(float(row["Low"]), 4),
            "close": round(float(row["Close"]), 4),
        })

    # Trade entries/exits from backtesting.py
    # Use bar indices to get timestamps that exactly match the OHLC series
    trades_list = []
    raw_trades = stats.get("_trades")
    if raw_trades is not None and not raw_trades.empty:
        idx_timestamps = [int(pd.Timestamp(ts).timestamp()) for ts in df.index]
        for _, t in raw_trades.iterrows():
            entry_bar = int(t.get("EntryBar", 0))
            exit_bar = int(t.get("ExitBar", 0))
            entry_ts = idx_timestamps[entry_bar] if entry_bar < len(idx_timestamps) else None
            exit_ts = idx_timestamps[exit_bar] if exit_bar < len(idx_timestamps) else None
            entry_price = float(t.get("EntryPrice", 0))
            exit_price = float(t.get("ExitPrice", 0))
            pnl = float(t.get("PnL", 0))
            ret_pct = float(t.get("ReturnPct", 0)) * 100
            trades_list.append({
                "entry_time": entry_ts,
                "exit_time": exit_ts,
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "pnl": round(pnl, 2),
                "return_pct": round(ret_pct, 2),
                "size": int(t.get("Size", 0)),
            })

    # Indicator overlays (strategy-specific)
    indicators = _extract_indicators(strat_cls, strategy_id, df, coerced or None)

    _eq_df = stats.get("_equity_curve")
    _raw_tr = stats.get("_trades")
    _kpis = build_research_kpis(
        stats,
        equity_df=_eq_df if _eq_df is not None and not getattr(_eq_df, "empty", True) else None,
        close_series=df["Close"],
        interval=interval,
        raw_trades=_raw_tr if _raw_tr is not None and not _raw_tr.empty else None,
    )

    payload: dict[str, Any] = {
        "ok": True,
        "ticker": ticker.upper(),
        "strategy": strat_info["name"],
        "strategy_id": strategy_id,
        "period": period,
        "interval": interval,
        **_chart_window_meta(df),
        "return_pct": round(float(stats.get("Return [%]", 0)), 2),
        "buy_hold_pct": round(float(stats.get("Buy & Hold Return [%]", 0)), 2),
        "win_rate": round(float(stats.get("Win Rate [%]", 0)), 1),
        "sharpe": round(float(stats.get("Sharpe Ratio", 0)), 2) if stats.get("Sharpe Ratio") else None,
        "max_drawdown": round(float(stats.get("Max. Drawdown [%]", 0)), 2),
        "trade_count": int(stats.get("# Trades", 0)),
        "avg_trade_pct": round(float(stats.get("Avg. Trade [%]", 0)), 2),
        "profit_factor": round(float(stats.get("Profit Factor", 0)), 2) if stats.get("Profit Factor") else None,
        "final_equity": round(float(stats.get("Equity Final [$]", cash)), 2),
        "equity_curve": equity_data,
        "ohlc": ohlc_data,
        "trades": trades_list,
        "indicators": indicators,
        "kpis": _kpis,
    }
    _enrich_generic_bt_result(
        payload,
        df,
        ticker=ticker.upper(),
        period=period,
        interval=interval,
        strategy_id=strategy_id,
    )
    return payload


def _sanitize_float(v: Any, default: float = 0.0) -> float:
    """Convert NaN / Inf / None to a safe float for database storage (PostgreSQL)."""
    import math
    if v is None:
        return default
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _chart_window_meta(df: pd.DataFrame) -> dict[str, Any]:
    """Bar count and first/last chart timestamps (UTC unix seconds) for persisted params / UI."""
    if df is None or df.empty:
        return {"ohlc_bars": 0, "chart_time_from": None, "chart_time_to": None}
    idx = df.index
    return {
        "ohlc_bars": int(len(df)),
        "chart_time_from": int(pd.Timestamp(idx[0]).timestamp()),
        "chart_time_to": int(pd.Timestamp(idx[-1]).timestamp()),
    }


def save_backtest(
    db: Session, user_id: int | None, result: dict[str, Any],
    *,
    insight_id: int | None = None,
    scan_pattern_id: int | None = None,
    backtest_row_id: int | None = None,
) -> BacktestResult:
    """Persist a backtest result to the database.

    If a record for the same (insight, ticker, strategy) already exists the
    existing row is **updated** instead of creating a duplicate.

    When ``backtest_row_id`` is set (rerun / evidence row), that primary key is updated
    if it belongs to ``insight_id`` and matches the result ticker/strategy. Otherwise
    the natural-key lookup uses the same ordering as Pattern Evidence (trade_count,
    ran_at) so duplicates do not leave the displayed representative row stale.
    """
    ticker = result.get("ticker", "")
    strategy = result.get("strategy", "")
    resolved_sp_id = scan_pattern_id if scan_pattern_id is not None else result.get("scan_pattern_id")
    if resolved_sp_id is None and strategy:
        sp_row = (
            db.query(ScanPattern.id)
            .filter(ScanPattern.name == strategy, ScanPattern.active.is_(True))
            .first()
        )
        if sp_row:
            resolved_sp_id = sp_row.id
    from .trading.backtest_metrics import normalize_win_rate_for_db

    ret_pct = _sanitize_float(result.get("return_pct"))
    _raw_wr = result.get("win_rate")
    if _raw_wr is None:
        wr = 0.0
    else:
        _nw = normalize_win_rate_for_db(_sanitize_float(_raw_wr))
        wr = float(_nw) if _nw is not None else 0.0
    sharpe = result.get("sharpe")
    if sharpe is not None:
        sharpe = _sanitize_float(sharpe, 0.0) or None
    md = _sanitize_float(result.get("max_drawdown"))
    tc = int(result.get("trade_count", 0) or 0)
    eq = result.get("equity_curve", [])
    if tc == 0:
        eq = []
    params_obj: dict[str, Any] = {
        "strategy_id": result.get("strategy_id"),
        "period": result.get("period"),
        "interval": result.get("interval"),
        "ohlc_bars": result.get("ohlc_bars"),
        "chart_time_from": result.get("chart_time_from"),
        "chart_time_to": result.get("chart_time_to"),
        "spread_used": result.get("spread_used"),
        "commission_used": result.get("commission_used"),
        "oos_holdout_fraction": result.get("oos_holdout_fraction"),
        "oos_win_rate": result.get("oos_win_rate"),
        "oos_return_pct": result.get("oos_return_pct"),
        "oos_trade_count": result.get("oos_trade_count"),
        "in_sample_bars": result.get("in_sample_bars"),
        "out_of_sample_bars": result.get("out_of_sample_bars"),
        "kpis": result.get("kpis"),
    }
    if result.get("data_provenance") is not None:
        params_obj["data_provenance"] = dict(result["data_provenance"])
    if result.get("research_integrity") is not None:
        params_obj["research_integrity"] = result.get("research_integrity")
    if resolved_sp_id is not None and isinstance(params_obj.get("data_provenance"), dict):
        params_obj["data_provenance"]["scan_pattern_id"] = int(resolved_sp_id)
    # Keep top-level window fields in sync with data_provenance for evidence Period column.
    _dp = params_obj.get("data_provenance")
    if isinstance(_dp, dict):
        for _k in ("ohlc_bars", "chart_time_from", "chart_time_to", "period", "interval"):
            if params_obj.get(_k) is None and _dp.get(_k) is not None:
                params_obj[_k] = _dp[_k]
    params_json = json.dumps(params_obj)
    from .trading.backtest_param_sets import get_or_create_backtest_param_set

    param_set_id = get_or_create_backtest_param_set(db, params_obj)

    if insight_id:
        existing: BacktestResult | None = None
        _t_u = (ticker or "").strip().upper()
        # Explicit row id (rerun / evidence): do not require strategy string equality — DB column is 100 chars
        # and result["strategy"] may be the full ScanPattern.name, so == often failed and updates missed the UI row.
        if backtest_row_id is not None:
            row = db.get(BacktestResult, int(backtest_row_id))
            if row is not None and int(row.related_insight_id or 0) == int(insight_id):
                if (row.ticker or "").strip().upper() == _t_u:
                    if resolved_sp_id is None or row.scan_pattern_id is None:
                        existing = row
                    elif int(row.scan_pattern_id) == int(resolved_sp_id):
                        existing = row
        if existing is None:
            strat = (strategy or "").strip()
            key100 = strat[:100] if strat else ""
            cands = (
                db.query(BacktestResult)
                .filter(
                    BacktestResult.related_insight_id == int(insight_id),
                    BacktestResult.ticker == ticker,
                )
                .order_by(
                    BacktestResult.trade_count.desc().nullslast(),
                    BacktestResult.ran_at.desc().nullslast(),
                    BacktestResult.id.desc(),
                )
                .all()
            )
            for r in cands:
                rsn = (r.strategy_name or "").strip()
                if strategy_label_aligns_scan_pattern_name(rsn, strat):
                    existing = r
                    break
            if existing is None and key100:
                for r in cands:
                    if (r.strategy_name or "").strip() == key100:
                        existing = r
                        break
            if existing is None and resolved_sp_id is not None:
                sp_match = [
                    r
                    for r in cands
                    if r.scan_pattern_id is not None
                    and int(r.scan_pattern_id) == int(resolved_sp_id)
                ]
                if len(sp_match) == 1:
                    existing = sp_match[0]
        if existing:
            existing.return_pct = ret_pct
            existing.win_rate = wr
            existing.sharpe = sharpe
            existing.max_drawdown = md
            existing.trade_count = tc
            existing.equity_curve = json.dumps(eq)
            existing.params = params_json
            if param_set_id is not None:
                existing.param_set_id = int(param_set_id)
            existing.scan_pattern_id = int(resolved_sp_id) if resolved_sp_id is not None else None
            # Was missing: updates never advanced ran_at, so evidence dedupe (max trade_count, ran_at)
            # kept picking a duplicate row with a newer timestamp while the rerun target stayed stale.
            existing.ran_at = datetime.utcnow()
            if result.get("oos_win_rate") is not None:
                _ow = normalize_win_rate_for_db(_sanitize_float(result["oos_win_rate"]))
                existing.oos_win_rate = float(_ow) if _ow is not None else None
                existing.oos_return_pct = float(result.get("oos_return_pct") or 0)
                existing.oos_trade_count = int(result.get("oos_trade_count") or 0)
                existing.oos_holdout_fraction = float(result.get("oos_holdout_fraction") or 0)
                existing.in_sample_bars = int(result.get("in_sample_bars") or 0)
                existing.out_of_sample_bars = int(result.get("out_of_sample_bars") or 0)
            db.commit()
            db.refresh(existing)
            _persist_pattern_trade_analytics(
                db, user_id, resolved_sp_id, insight_id, existing, result,
            )
            return existing

    _oos_wr_raw = result.get("oos_win_rate")
    _oos_wr = (
        normalize_win_rate_for_db(_sanitize_float(_oos_wr_raw))
        if _oos_wr_raw is not None
        else None
    )
    _oos_ret = result.get("oos_return_pct")
    _oos_tc = result.get("oos_trade_count")
    _oos_frac = result.get("oos_holdout_fraction")
    _is_bars = result.get("in_sample_bars")
    _oos_bars = result.get("out_of_sample_bars")

    record = BacktestResult(
        user_id=user_id,
        ticker=ticker,
        strategy_name=strategy,
        params=params_json,
        param_set_id=int(param_set_id) if param_set_id is not None else None,
        return_pct=ret_pct,
        win_rate=wr,
        sharpe=sharpe,
        max_drawdown=md,
        trade_count=tc,
        equity_curve=json.dumps(eq),
        related_insight_id=insight_id,
        scan_pattern_id=int(resolved_sp_id) if resolved_sp_id is not None else None,
        oos_win_rate=float(_oos_wr) if _oos_wr is not None else None,
        oos_return_pct=float(_oos_ret) if _oos_ret is not None else None,
        oos_trade_count=int(_oos_tc) if _oos_tc is not None else None,
        oos_holdout_fraction=float(_oos_frac) if _oos_frac is not None else None,
        in_sample_bars=int(_is_bars) if _is_bars is not None else None,
        out_of_sample_bars=int(_oos_bars) if _oos_bars is not None else None,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    _persist_pattern_trade_analytics(
        db, user_id, resolved_sp_id, insight_id, record, result,
    )
    return record


def _persist_pattern_trade_analytics(
    db: Session,
    user_id: int | None,
    scan_pattern_id: int | None,
    insight_id: int | None,
    record: BacktestResult,
    result: dict[str, Any],
) -> None:
    if not scan_pattern_id or not result.get("trades"):
        return
    try:
        from .trading.pattern_trade_storage import persist_rows_from_backtest_result

        persist_rows_from_backtest_result(
            db,
            user_id=user_id,
            scan_pattern_id=int(scan_pattern_id),
            related_insight_id=insight_id,
            backtest_row=record,
            result=result,
            source="insight_backtest" if insight_id else "queue_backtest",
        )
    except Exception:
        pass


# ── Pattern-aware backtesting ────────────────────────────────────────

def _compute_swing_lows(df: pd.DataFrame, lookback: int = 10) -> list:
    """Compute rolling most-recent confirmed swing low (no look-ahead bias).

    A swing low at bar *i* is confirmed at bar ``i + lookback`` once we know
    ``low[i]`` is the minimum within ``[i - lookback, i + lookback]``.  The
    returned list gives the value of the most recent confirmed swing low at
    each bar, or ``None`` when no swing low has been confirmed yet.

    Uses a 10-bar lookback by default so that swing lows represent meaningful
    structural levels rather than intraday noise.

    Used by ``DynamicPatternStrategy`` for Break-of-Structure (BOS) exits:
    if price closes more than 0.3 % below the latest swing low, the uptrend
    structure is considered broken and the position is closed.
    """
    lows = df["Low"].astype(float).values
    n = len(lows)
    result: list[float | None] = [None] * n
    last_confirmed: float | None = None

    for confirm_bar in range(2 * lookback, n):
        candidate = confirm_bar - lookback
        window_start = max(0, candidate - lookback)
        window_end = min(n, candidate + lookback + 1)
        if lows[candidate] <= lows[window_start:window_end].min():
            last_confirmed = float(lows[candidate])
        result[confirm_bar] = last_confirmed

    return result


def _compute_atr_series(df: pd.DataFrame, period: int = 14) -> list:
    """Compute ATR as a plain list for exit logic in DynamicPatternStrategy."""
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return [None if pd.isna(v) else float(v) for v in atr]


def _compute_series_for_conditions(
    df: pd.DataFrame,
    conditions: list[dict[str, Any]],
) -> dict[str, list]:
    """Pre-compute full-length indicator series required by pattern conditions.

    Delegates standard indicators to ``indicator_core`` for backtest/live parity,
    then computes backtest-specific composite indicators (candlestick patterns,
    squeeze, VCP, etc.) that are only needed in the backtest context.
    """
    needed: set[str] = set()
    for cond in conditions:
        ind = cond.get("indicator", "")
        if ind:
            needed.add(ind)
        ref = cond.get("ref")
        if ref:
            needed.add(ref)

    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    volume = df["Volume"].astype(float)
    n = len(df)

    # Delegate standard indicators to the shared core for parity
    from app.services.trading.indicator_core import compute_all_from_df
    result: dict[str, list] = compute_all_from_df(df, needed=needed)

    def _safe(series: pd.Series) -> list:
        return [None if pd.isna(v) else float(v) for v in series]

    # -- Internal Bar Strength: (close - low) / (high - low), 0..1 ----------
    if "ibs" in needed:
        rng = (high - low).replace(0, np.nan)
        ibs_s = (close - low) / rng
        result["ibs"] = [None if pd.isna(v) else float(v) for v in ibs_s]

    # -- Pullback vs stretched high (reddit r/Daytrading vaanam-dev setup) ---
    # close < (10-bar high - 2.5 * (mean(high,25) - mean(low,25)))
    if "pullback_stretch_entry" in needed:
        hh10 = high.rolling(10, min_periods=10).max()
        ah25 = high.rolling(25, min_periods=25).mean()
        al25 = low.rolling(25, min_periods=25).mean()
        threshold = hh10 - 2.5 * (ah25 - al25)
        stretch: list = [None] * n
        for i in range(n):
            th = threshold.iloc[i]
            cl = close.iloc[i]
            if pd.isna(th) or pd.isna(cl):
                continue
            stretch[i] = bool(float(cl) < float(th))
        result["pullback_stretch_entry"] = stretch

    # -- Candlestick pattern indicators ------------------------------------
    _open = df["Open"]
    _body = (close - _open).abs()
    _range = (df["High"] - df["Low"]).replace(0, np.nan)
    _upper_shadow = df["High"] - pd.concat([close, _open], axis=1).max(axis=1)
    _lower_shadow = pd.concat([close, _open], axis=1).min(axis=1) - df["Low"]
    _bullish = close > _open
    _bearish = close < _open
    _prev_open = _open.shift(1)
    _prev_close = close.shift(1)
    _prev_bullish = _prev_close > _prev_open
    _prev_bearish = _prev_close < _prev_open

    if "bullish_engulfing" in needed:
        be = (_bullish & _prev_bearish &
              (_open <= _prev_close) & (close >= _prev_open) &
              (_body > _body.shift(1)))
        result["bullish_engulfing"] = _safe(be.astype(float))

    if "bearish_engulfing" in needed:
        be = (_bearish & _prev_bullish &
              (_open >= _prev_close) & (close <= _prev_open) &
              (_body > _body.shift(1)))
        result["bearish_engulfing"] = _safe(be.astype(float))

    if "hammer" in needed:
        h = ((_lower_shadow >= 2 * _body) &
             (_upper_shadow <= _body * 0.5) &
             (_body > 0))
        result["hammer"] = _safe(h.astype(float))

    if "inverted_hammer" in needed:
        ih = ((_upper_shadow >= 2 * _body) &
              (_lower_shadow <= _body * 0.5) &
              (_body > 0))
        result["inverted_hammer"] = _safe(ih.astype(float))

    if "morning_star" in needed:
        p2_open = _open.shift(1)
        p2_close = close.shift(1)
        p2_body = _body.shift(1)
        p1_close = close.shift(2)
        p1_open = _open.shift(2)
        p1_mid = (p1_close + p1_open) / 2
        ms = ((close.shift(2) < _open.shift(2)) &
              (p2_body < _body.shift(2) * 0.5) &
              _bullish &
              (close > p1_mid))
        result["morning_star"] = _safe(ms.astype(float))

    if "doji" in needed:
        d = (_body < _range * 0.1)
        result["doji"] = _safe(d.astype(float))

    # -- Bollinger Band squeeze (rolling percentile of BB width) -----------
    if "bb_squeeze" in needed or "bb_squeeze_firing" in needed:
        try:
            from ta.volatility import BollingerBands
            bb = BollingerBands(close, window=20, window_dev=2)
            bb_width = bb.bollinger_wband()
        except Exception:
            bb_mid = close.rolling(20).mean()
            bb_std_s = close.rolling(20).std()
            bb_width = 2 * bb_std_s / bb_mid.replace(0, np.nan)

        pct_20 = bb_width.rolling(50).quantile(0.20)
        sq = bb_width <= pct_20
        # Fall back for bars with < 50 history: use pct-rank approach
        for i in range(20, min(50, n)):
            bw_slice = bb_width.iloc[:i + 1].dropna()
            if len(bw_slice) >= 20:
                curr_w = bb_width.iloc[i]
                if pd.notna(curr_w):
                    pct_rank = float((bw_slice < curr_w).sum() / len(bw_slice) * 100)
                    sq.iloc[i] = pct_rank < 25

        result["bb_squeeze"] = [
            bool(v) if pd.notna(v) else None for v in sq
        ]
        if "bb_squeeze_firing" in needed:
            prev_sq = sq.shift(1)
            result["bb_squeeze_firing"] = [
                bool(v) if pd.notna(v) else None for v in (prev_sq & ~sq)
            ]

    # -- Resistance (use core-computed or recompute for retests) ----------
    resistance_s = high.rolling(20).max()

    # -- Resistance retests (rolling count of bars near resistance) --------
    if "resistance_retests" in needed:
        tol_pct = 1.5
        lookback = 20
        for cond in conditions:
            if cond.get("indicator") == "resistance_retests":
                params = cond.get("params", {})
                tol_pct = params.get("tolerance_pct", 1.5)
                lookback = params.get("lookback", 20)
                break

        retests: list = [None] * n
        for i in range(lookback, n):
            res = float(high.iloc[i - lookback: i + 1].max())
            threshold = res * (tol_pct / 100.0)
            lower_band = res - threshold
            count = sum(
                1 for j in range(i - lookback, i + 1)
                if float(high.iloc[j]) >= lower_band
            )
            retests[i] = count
        result["resistance_retests"] = retests

    # -- Retest range tightening -------------------------------------------
    if "retest_range_tightening" in needed:
        lookback = 20
        for cond in conditions:
            if cond.get("indicator") in (
                "resistance_retests", "retest_range_tightening",
            ):
                lookback = cond.get("params", {}).get("lookback", 20)
                break
        tightening: list = [None] * n
        for i in range(lookback, n):
            h_slice = high.iloc[i - lookback: i + 1]
            half = len(h_slice) // 2
            if half > 0:
                first_r = float(h_slice.iloc[:half].max() - h_slice.iloc[:half].min())
                second_r = float(h_slice.iloc[half:].max() - h_slice.iloc[half:].min())
                tightening[i] = bool(first_r > 0 and second_r < first_r * 0.75)
            else:
                tightening[i] = False
        result["retest_range_tightening"] = tightening

    # -- VWAP reclaim ------------------------------------------------------
    if "vwap_reclaim" in needed:
        cum_vol = volume.cumsum()
        vwap = (close * volume).cumsum() / cum_vol.replace(0, np.nan)
        prev_close = close.shift(1)
        vol_avg = volume.rolling(20).mean().replace(0, np.nan)
        rel_v = volume / vol_avg
        reclaim = (prev_close < vwap) & (close > vwap) & (rel_v >= 1.2)
        result["vwap_reclaim"] = [
            bool(v) if pd.notna(v) else None for v in reclaim
        ]

    # -- Narrow range (NR4 / NR7) -----------------------------------------
    if "narrow_range" in needed:
        bar_range = high - low
        nr: list = [None] * n
        for i in range(6, n):
            curr = float(bar_range.iloc[i])
            ranges_7 = [float(bar_range.iloc[i - j]) for j in range(7)]
            past_7 = ranges_7[1:]
            if past_7 and curr <= min(past_7):
                nr[i] = "NR7"
            else:
                past_4 = ranges_7[1:4]
                if past_4 and curr <= min(past_4):
                    nr[i] = "NR4"
        result["narrow_range"] = nr

    # -- VCP count (Volume Contraction Pattern) ----------------------------
    if "vcp_count" in needed:
        vcp: list = [None] * n
        vcp_lb = 40
        for i in range(vcp_lb, n):
            h_seg = high.iloc[i - vcp_lb: i + 1]
            l_seg = low.iloc[i - vcp_lb: i + 1]
            v_seg = volume.iloc[i - vcp_lb: i + 1]
            lb = len(h_seg)
            window = max(3, lb // 4)
            swings: list[tuple[float, float]] = []
            for start in range(0, lb - window + 1, window):
                end_idx = min(start + window, lb)
                seg_r = float(h_seg.iloc[start:end_idx].max() - l_seg.iloc[start:end_idx].min())
                seg_v = float(v_seg.iloc[start:end_idx].mean())
                if seg_r > 0 and seg_v > 0:
                    swings.append((seg_r, seg_v))
            contractions = 0
            for k in range(1, len(swings)):
                rp, vp = swings[k - 1]
                rc, vc = swings[k]
                if rp > 0 and rc < rp * 0.85 and vc < vp:
                    contractions += 1
                else:
                    contractions = 0
            vcp[i] = contractions
        result["vcp_count"] = vcp

    return result


# ── Condition evaluation (delegates to canonical pattern_engine) ──────

def _eval_condition_bt(cond: dict, snap: dict[str, Any]) -> bool:
    """Evaluate a single pattern condition against a bar snapshot.

    Delegates to ``pattern_engine._eval_condition`` to guarantee
    backtest/live parity — a single source of truth for condition logic.
    """
    from app.services.trading.pattern_engine import _eval_condition
    return _eval_condition(cond, snap)


# ── Dynamic pattern strategy ─────────────────────────────────────────

class DynamicPatternStrategy(Strategy):
    """Strategy that evaluates actual pattern rules_json conditions bar-by-bar.

    Class attributes are set dynamically via ``type()`` before each run so that
    ``backtesting.py`` picks up the correct conditions and pre-computed data.

    Exit logic (three-pronged):
    1. ATR trailing stop — highest-since-entry minus ``_exit_atr_mult * ATR``
    2. Max hold period — ``_exit_max_bars`` bars since entry
    3. Break of Structure (BOS) — price closes below the most recent confirmed
       swing low, signalling the uptrend structure is broken.  BOS buffer and
       grace period scale with volatility (ATR/price ratio) so that crypto's
       normal large wicks don't trigger premature exits.
    """
    _conditions: list = []
    _indicator_arrays: dict = {}
    _exit_atr_mult: float = 2.0
    _exit_max_bars: int = 20
    _atr_array: list = []
    _swing_low_array: list = []
    _explicit_bos_buffer: float | None = None
    _explicit_bos_grace: int | None = None

    def init(self):
        self._bars_in_trade = 0
        self._highest_since_entry = 0.0

        if self._explicit_bos_buffer is not None:
            self._bos_buffer_pct = self._explicit_bos_buffer
            self._bos_grace = self._explicit_bos_grace or 3
            return

        self._bos_grace = 3
        self._bos_buffer_pct = 0.003

        prices = self.data.Close
        if len(prices) > 20 and len(self._atr_array) > 20:
            recent_atr = [
                a for a in self._atr_array[-20:] if a is not None and a > 0
            ]
            if recent_atr:
                avg_atr = sum(recent_atr) / len(recent_atr)
                avg_price = float(sum(prices[-20:]) / 20)
                vol_ratio = avg_atr / max(avg_price, 1e-9)
                if vol_ratio > 0.03:
                    self._bos_grace = 6
                    self._bos_buffer_pct = 0.015
                elif vol_ratio > 0.015:
                    self._bos_grace = 4
                    self._bos_buffer_pct = 0.008

    def next(self):
        i = len(self.data.Close) - 1

        snap: dict[str, Any] = {"price": float(self.data.Close[-1])}
        for key, arr in self._indicator_arrays.items():
            if i < len(arr):
                snap[key] = arr[i]

        all_met = True
        for cond in self._conditions:
            if not _eval_condition_bt(cond, snap):
                all_met = False
                break

        if all_met and not self.position:
            self.buy()
            self._bars_in_trade = 0
            self._highest_since_entry = float(self.data.Close[-1])
        elif self.position:
            self._bars_in_trade += 1
            price = float(self.data.Close[-1])
            self._highest_since_entry = max(self._highest_since_entry, price)

            atr_val = 0.0
            if i < len(self._atr_array) and self._atr_array[i] is not None:
                atr_val = self._atr_array[i]

            trailing_stop = self._highest_since_entry - self._exit_atr_mult * atr_val

            bos_triggered = False
            if (self._bars_in_trade >= self._bos_grace
                    and i < len(self._swing_low_array)):
                swing_low = self._swing_low_array[i]
                if swing_low is not None and swing_low > 0:
                    bos_threshold = swing_low * (1 - self._bos_buffer_pct)
                    if price < bos_threshold:
                        bos_triggered = True

            if (price < trailing_stop
                    or self._bars_in_trade >= self._exit_max_bars
                    or bos_triggered):
                self.position.close()


def _extract_pattern_indicators(
    indicator_arrays: dict[str, list],
    df: pd.DataFrame,
) -> dict[str, list[dict]]:
    """Build chartable indicator overlays from the pre-computed series."""
    skip = {
        "price", "bb_squeeze", "bb_squeeze_firing", "vwap_reclaim",
        "narrow_range", "retest_range_tightening",
        "pullback_stretch_entry",
    }
    result: dict[str, list[dict]] = {}
    timestamps = [int(pd.Timestamp(ts).timestamp()) for ts in df.index]
    for key, arr in indicator_arrays.items():
        if key in skip:
            continue
        points = []
        for idx, val in enumerate(arr):
            if val is not None and idx < len(timestamps):
                try:
                    points.append({"time": timestamps[idx], "value": round(float(val), 4)})
                except (TypeError, ValueError):
                    pass
        if points:
            label = key.upper().replace("_", " ")
            result[label] = points
    return result


# ── Timeframe intelligence ────────────────────────────────────────────

_INTRADAY_INDICATORS = {
    "gap_pct", "vwap_reclaim", "daily_change_pct",
    "vol_ratio", "relative_volume", "spread",
}

_EXPLICIT_TF_HINTS: dict[str, list[str]] = {
    "1m":  ["1m ", "1-min", "1min", "one minute"],
    "5m":  ["5m ", "5-min", "5min", "five minute"],
    "15m": ["15m", "15-min", "15min", "quarter hour"],
    "1h":  ["1h ", "1-hour", "1hour", "hourly", "60m", "60min"],
    "4h":  ["4h", "4-hour", "4 hour", "4hour", "intraswing"],
    "1d":  ["daily", "1d ", "eod", "end of day"],
}

_SCALP_HINTS = {
    "scalp", "scalping", "1m", "tick",
}
_FAST_INTRADAY_HINTS = {
    "gap and go", "gap-and-go", "gapandgo",
    "opening range", "orb", "premarket", "pre-market",
    "morning", "power hour", "5m",
}
_INTRADAY_HINTS = {
    "intraday", "day trade", "daytrade",
    "micro-pullback", "micro pullback", "micropullback",
    "momentum scanner", "midday", "lunch", "15m", "30m",
}
_MID_HINTS = {
    "4h", "4-hour", "4 hour", "intraswing",
}
_SLOW_SWING_HINTS = {
    "vcp", "volume contraction",
    "swing", "multi-day", "weekly", "position",
    "52 week", "52-week",
}

_SWING_INDICATORS = {
    "vcp_count", "narrow_range",
}

_TIMEFRAME_PARAMS: dict[str, dict[str, Any]] = {
    "1m":  {"interval": "1m",  "period": "7d",   "min_bars": 30},
    "5m":  {"interval": "5m",  "period": "30d",  "min_bars": 30},
    "15m": {"interval": "15m", "period": "60d",  "min_bars": 30},
    "1h":  {"interval": "1h",  "period": "6mo",  "min_bars": 30},
    "4h":  {"interval": "1h",  "period": "1y",   "min_bars": 30},
    "1d":  {"interval": "1d",  "period": "2y",   "min_bars": 30},
}

_EXIT_PARAMS_BY_TIMEFRAME: dict[str, dict[str, tuple[float, int, bool]]] = {
    "1m": {
        "breakout": (1.0, 120, False),  # ~2 hours of 1m bars
        "mean_rev": (0.5, 30, True),    # ~30 minutes
        "default":  (0.8, 60, True),    # ~1 hour
    },
    "5m": {
        "breakout": (1.5, 78, False),
        "mean_rev": (0.8, 24, True),
        "default":  (1.2, 48, True),
    },
    "15m": {
        "breakout": (2.0, 26, False),
        "mean_rev": (1.0, 8, True),
        "default":  (1.5, 16, True),
    },
    "1h": {
        "breakout": (2.5, 48, False),
        "mean_rev": (1.2, 8, True),
        "default":  (1.8, 24, True),
    },
    "4h": {
        "breakout": (2.8, 30, False),
        "mean_rev": (1.3, 10, True),
        "default":  (2.0, 18, True),
    },
    "1d": {
        "breakout": (3.0, 50, False),
        "mean_rev": (1.5, 15, True),
        "default":  (2.0, 25, True),
    },
}


def infer_pattern_timeframe(
    conditions: list[dict[str, Any]],
    name: str = "",
    asset_class: str = "all",
    description: str = "",
) -> str:
    """Infer an initial backtesting timeframe from pattern characteristics.

    Returns one of: '1m', '5m', '15m', '1h', '4h', '1d'.

    The logic is intentionally loose — concepts like "breakout" or
    "pullback" are timeframe-agnostic and should NOT force daily.
    Only explicit timeframe mentions or strongly intraday/swing
    indicators pin the timeframe.  Evolution explores the rest.
    """
    text_lower = f"{name} {description}".lower()

    for tf, hints in _EXPLICIT_TF_HINTS.items():
        for h in hints:
            if h in text_lower:
                return tf

    indicators = {c.get("indicator", "") for c in conditions}

    intraday_score = 0
    swing_score = 0

    for ind in indicators:
        if ind in _INTRADAY_INDICATORS:
            intraday_score += 2
        if ind in _SWING_INDICATORS:
            swing_score += 2

    for h in _SCALP_HINTS:
        if h in text_lower:
            return "1m"

    for h in _FAST_INTRADAY_HINTS:
        if h in text_lower:
            intraday_score += 3
            break

    for h in _INTRADAY_HINTS:
        if h in text_lower:
            intraday_score += 2
            break

    for h in _MID_HINTS:
        if h in text_lower:
            return "4h"

    for h in _SLOW_SWING_HINTS:
        if h in text_lower:
            swing_score += 3
            break

    if asset_class == "crypto":
        intraday_score += 2

    if intraday_score > swing_score and intraday_score >= 3:
        if intraday_score >= 5:
            return "5m"
        if asset_class == "crypto":
            return "1h"
        return "15m"

    if swing_score > intraday_score and swing_score >= 4:
        if asset_class == "crypto":
            return "4h"
        return "1d"

    if asset_class == "crypto":
        return "4h"

    return "1h"


def get_backtest_params(timeframe: str) -> dict[str, Any]:
    """Return interval/period/min_bars for a given timeframe."""
    return _TIMEFRAME_PARAMS.get(timeframe, _TIMEFRAME_PARAMS["1d"]).copy()


def get_brain_backtest_window(timeframe: str) -> tuple[str, str]:
    """Return ``(period, interval)`` used by ``smart_backtest_insight`` for a linked pattern.

    The brain resolves ``ScanPattern.timeframe`` → ``get_backtest_params`` and, when no
    custom ``period`` is passed into ``smart_backtest_insight``, uses both ``period`` and
    ``interval`` from that map. Callers (UI rerun, backfill) should use this so stored
    runs match the learning scheduler / batch backtests.
    """
    bp = get_backtest_params(timeframe)
    return bp["period"], bp["interval"]


def _classify_exit_params(
    conditions: list[dict[str, Any]],
    timeframe: str = "1d",
) -> tuple[float, int, bool]:
    """Infer exit parameters from the pattern's condition indicators AND timeframe.

    Returns ``(atr_mult, max_bars, use_bos)`` tuned to both pattern type and
    timeframe so that intraday patterns exit within hours, not days.
    """
    _BREAKOUT_INDICATORS = {
        "resistance_retests", "bb_squeeze", "bb_squeeze_firing",
        "narrow_range", "vcp_count", "dist_to_resistance_pct",
        "retest_range_tightening", "resistance",
    }
    _MEAN_REV_INDICATORS = {
        "vwap_reclaim",
        "ibs",
        "pullback_stretch_entry",
    }

    breakout_score = 0
    mean_rev_score = 0

    for cond in conditions:
        ind = cond.get("indicator", "")
        op = cond.get("op", "")
        value = cond.get("value")

        if ind in _BREAKOUT_INDICATORS:
            breakout_score += 2
        if ind in _MEAN_REV_INDICATORS:
            mean_rev_score += 2

        if ind == "rsi_14":
            try:
                v = float(value) if value is not None else 0
            except (TypeError, ValueError):
                v = 0
            if op in (">", ">=") and v >= 60:
                breakout_score += 1
            elif op in ("<", "<=") and v <= 40:
                mean_rev_score += 1

    tf_params = _EXIT_PARAMS_BY_TIMEFRAME.get(
        timeframe, _EXIT_PARAMS_BY_TIMEFRAME["1d"]
    )

    if breakout_score > mean_rev_score:
        return tf_params["breakout"]
    if mean_rev_score > breakout_score:
        return tf_params["mean_rev"]
    return tf_params["default"]


def _run_dynamic_pattern_slice(
    df: pd.DataFrame,
    *,
    ticker: str,
    pattern_name: str,
    period: str,
    interval: str,
    cash: float,
    commission: float,
    spread: float,
    conditions: list[dict[str, Any]],
    exit_atr_mult: float,
    exit_max_bars: int,
    use_bos: bool,
    explicit_bos_buffer: float | None,
    explicit_bos_grace: int | None,
    include_charts: bool,
    ohlc_start: str | None = None,
    ohlc_end: str | None = None,
    scan_pattern_id: int | None = None,
) -> dict[str, Any]:
    """Execute DynamicPatternStrategy on a prepared OHLCV dataframe."""
    if df.empty or len(df) < 15:
        return {"ok": False, "error": f"Not enough bars for {ticker}"}

    work = df.copy()
    work.index = pd.to_datetime(work.index)
    if work.index.tz is not None:
        work.index = work.index.tz_localize(None)

    indicator_arrays = _compute_series_for_conditions(work, conditions)
    atr = _compute_atr_series(work)
    swing_lows = _compute_swing_lows(work) if use_bos else []

    strat_cls = type("_DynPat", (DynamicPatternStrategy,), {
        "_conditions": conditions,
        "_indicator_arrays": indicator_arrays,
        "_exit_atr_mult": exit_atr_mult,
        "_exit_max_bars": exit_max_bars,
        "_atr_array": atr,
        "_swing_low_array": swing_lows,
        "_explicit_bos_buffer": explicit_bos_buffer,
        "_explicit_bos_grace": explicit_bos_grace,
    })

    bt = FractionalBacktest(
        work,
        strat_cls,
        cash=cash,
        commission=commission,
        spread=float(spread or 0.0),
        exclusive_orders=True,
        finalize_trades=True,
    )
    stats = bt.run()

    equity_data: list[dict[str, Any]] = []
    if include_charts:
        equity = stats.get("_equity_curve")
        if equity is not None and not equity.empty:
            for ts, row in equity.iterrows():
                equity_data.append({
                    "time": int(pd.Timestamp(ts).timestamp()),
                    "value": round(float(row["Equity"]), 2),
                })

    ohlc_data: list[dict[str, Any]] = []
    if include_charts:
        for ts, row in work.iterrows():
            ohlc_data.append({
                "time": int(pd.Timestamp(ts).timestamp()),
                "open": round(float(row["Open"]), 4),
                "high": round(float(row["High"]), 4),
                "low": round(float(row["Low"]), 4),
                "close": round(float(row["Close"]), 4),
            })

    trades_list: list[dict[str, Any]] = []
    raw_trades = stats.get("_trades")
    if raw_trades is not None and not raw_trades.empty:
        idx_timestamps = [int(pd.Timestamp(ts).timestamp()) for ts in work.index]
        for _, t in raw_trades.iterrows():
            entry_bar = int(t.get("EntryBar", 0))
            exit_bar = int(t.get("ExitBar", 0))
            entry_ts = idx_timestamps[entry_bar] if entry_bar < len(idx_timestamps) else None
            exit_ts = idx_timestamps[exit_bar] if exit_bar < len(idx_timestamps) else None
            trades_list.append({
                "entry_time": entry_ts,
                "exit_time": exit_ts,
                "entry_price": round(float(t.get("EntryPrice", 0)), 4),
                "exit_price": round(float(t.get("ExitPrice", 0)), 4),
                "pnl": round(float(t.get("PnL", 0)), 2),
                "return_pct": round(float(t.get("ReturnPct", 0)) * 100, 2),
                "size": int(t.get("Size", 0)),
            })

    indicators = _extract_pattern_indicators(indicator_arrays, work)

    _eq_bt = stats.get("_equity_curve")
    _raw_tr = stats.get("_trades")
    _kpis = build_research_kpis(
        stats,
        equity_df=_eq_bt if _eq_bt is not None and not getattr(_eq_bt, "empty", True) else None,
        close_series=work["Close"],
        interval=interval,
        raw_trades=_raw_tr if _raw_tr is not None and not _raw_tr.empty else None,
    )

    payload: dict[str, Any] = {
        "ok": True,
        "ticker": ticker.upper(),
        "strategy": pattern_name,
        "strategy_id": "dynamic_pattern",
        "period": period,
        "interval": interval,
        **_chart_window_meta(work),
        "return_pct": round(float(stats.get("Return [%]", 0)), 2),
        "buy_hold_pct": round(float(stats.get("Buy & Hold Return [%]", 0)), 2),
        "win_rate": round(float(stats.get("Win Rate [%]", 0)), 1),
        "sharpe": round(float(stats.get("Sharpe Ratio", 0)), 2) if stats.get("Sharpe Ratio") else None,
        "max_drawdown": round(float(stats.get("Max. Drawdown [%]", 0)), 2),
        "trade_count": int(stats.get("# Trades", 0)),
        "avg_trade_pct": round(float(stats.get("Avg. Trade [%]", 0)), 2),
        "profit_factor": round(float(stats.get("Profit Factor", 0)), 2) if stats.get("Profit Factor") else None,
        "final_equity": round(float(stats.get("Equity Final [$]", cash)), 2),
        "equity_curve": equity_data,
        "ohlc": ohlc_data,
        "trades": trades_list,
        "indicators": indicators,
        "kpis": _kpis,
    }
    _enrich_pattern_bt_result(
        payload,
        work,
        conditions,
        ticker=ticker.upper(),
        period=period,
        interval=interval,
        ohlc_start=ohlc_start,
        ohlc_end=ohlc_end,
        scan_pattern_id=scan_pattern_id,
        indicator_arrays=indicator_arrays,
    )
    return payload


def backtest_metrics_for_promotion_gate(result: dict[str, Any]) -> tuple[float, float]:
    """Return (win_rate, return_pct) for IS vs OOS promotion logic.

    When ``oos_holdout_fraction`` was used, headline metrics are full-window;
    ``in_sample`` holds the prefix-window stats for gates.
    """
    isl = result.get("in_sample")
    if isinstance(isl, dict) and isl.get("win_rate") is not None:
        return (
            float(isl.get("win_rate") or 0),
            float(isl.get("return_pct") or 0),
        )
    return (
        float(result.get("win_rate") or 0),
        float(result.get("return_pct") or 0),
    )


def run_pattern_backtest(
    ticker: str,
    conditions: list[dict[str, Any]],
    pattern_name: str | None = None,
    period: str = "1y",
    interval: str = "1d",
    cash: float = 100_000,
    commission: float | None = None,
    exit_atr_mult: float | None = None,
    exit_max_bars: int | None = None,
    exit_config: dict[str, Any] | None = None,
    *,
    spread: float | None = None,
    oos_holdout_fraction: float | None = None,
    ohlc_start: str | None = None,
    ohlc_end: str | None = None,
    df_override: pd.DataFrame | None = None,
    scan_pattern_id: int | None = None,
) -> dict[str, Any]:
    """Run a backtest using actual pattern conditions as entry signals.

    Instead of mapping to a generic strategy, this evaluates the pattern's
    ``rules_json`` conditions bar-by-bar to generate entry signals.  Exits
    use an ATR trailing stop with a maximum hold period.

    When *exit_config* is provided (from a ScanPattern's evolved exit
    strategy), those values take priority.  Otherwise *exit_atr_mult* /
    *exit_max_bars* are used, falling back to ``_classify_exit_params``.

    *spread* defaults to ``settings.backtest_spread`` (bid/ask + slippage proxy).
    When *oos_holdout_fraction* is set (e.g. 0.25), headline ``win_rate`` /
    ``return_pct`` / ``ohlc`` / ``trades`` use the **full** fetched window so UI
    and saved backtests match the requested period.  The first ``(1 - fraction)``
    bars are also re-run without charts to populate ``in_sample`` (for promotion
    gates vs ``oos_*`` on the held-out tail).

    *df_override* supplies OHLCV instead of fetching (used for walk-forward windows).

    *ohlc_start* / *ohlc_end* (``YYYY-MM-DD``) are passed to the market-data layer so
    reruns can match a stored chart window; ignored when *df_override* is set.
    """
    if commission is None:
        commission = float(settings.backtest_commission)
    if spread is None:
        spread = float(settings.backtest_spread)

    if not pattern_name:
        pattern_name = generate_strategy_name(conditions)
    use_bos = True
    explicit_bos_buffer: float | None = None
    explicit_bos_grace: int | None = None

    if exit_config:
        exit_atr_mult = exit_config.get("atr_mult", exit_atr_mult)
        exit_max_bars = exit_config.get("max_bars", exit_max_bars)
        use_bos = exit_config.get("use_bos", True)
        if use_bos:
            explicit_bos_buffer = exit_config.get("bos_buffer_pct")
            explicit_bos_grace = exit_config.get("bos_grace_bars")

    if exit_atr_mult is None or exit_max_bars is None:
        tf_key = interval if interval in _EXIT_PARAMS_BY_TIMEFRAME else "1d"
        auto_atr, auto_bars, auto_bos = _classify_exit_params(conditions, timeframe=tf_key)
        if exit_atr_mult is None:
            exit_atr_mult = auto_atr
        if exit_max_bars is None:
            exit_max_bars = auto_bars
        if not exit_config:
            use_bos = auto_bos

    assert exit_atr_mult is not None and exit_max_bars is not None

    if df_override is not None:
        df = df_override.copy()
    else:
        df = pd.DataFrame()
        for attempt in range(2):
            df = _fetch_ohlcv_df(
                ticker,
                period=period,
                interval=interval,
                start=ohlc_start,
                end=ohlc_end,
            )
            if not df.empty and len(df) >= 30:
                break
            if attempt == 0:
                time.sleep(0.4)
        if (df.empty or len(df) < 30) and (ohlc_start or ohlc_end):
            df = _fetch_ohlcv_df(ticker, period=period, interval=interval)
    if df.empty or len(df) < 30:
        return {"ok": False, "error": f"Not enough data for {ticker}"}

    try:
        from app.services.trading.data_quality import clean_ohlcv
        df = clean_ohlcv(df)
    except Exception:
        pass

    if df.empty or len(df) < 30:
        return {"ok": False, "error": f"Not enough clean data for {ticker}"}

    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    def _run_slice(sub: pd.DataFrame, charts: bool) -> dict[str, Any]:
        return _run_dynamic_pattern_slice(
            sub,
            ticker=ticker,
            pattern_name=pattern_name,
            period=period,
            interval=interval,
            cash=cash,
            commission=commission,
            spread=spread,
            conditions=conditions,
            exit_atr_mult=float(exit_atr_mult),
            exit_max_bars=int(exit_max_bars),
            use_bos=use_bos,
            explicit_bos_buffer=explicit_bos_buffer,
            explicit_bos_grace=explicit_bos_grace,
            include_charts=charts,
            ohlc_start=ohlc_start,
            ohlc_end=ohlc_end,
            scan_pattern_id=scan_pattern_id,
        )

    oos_frac = oos_holdout_fraction
    if (
        oos_frac is not None
        and 0.05 < float(oos_frac) < 0.45
    ):
        split_i = int(len(df) * (1.0 - float(oos_frac)))
        oos_bars = len(df) - split_i
        if split_i >= 30 and oos_bars >= 12:
            r_full = _run_slice(df, True)
            r_is = _run_slice(df.iloc[:split_i], False)
            r_oos = _run_slice(df.iloc[split_i:], False)
            if not r_full.get("ok"):
                r_full["spread_used"] = spread
                r_full["commission_used"] = commission
                return r_full
            if not r_is.get("ok"):
                r_full["spread_used"] = spread
                r_full["commission_used"] = commission
                return r_full
            out = {**r_full}
            out["spread_used"] = spread
            out["commission_used"] = commission
            out["oos_holdout_fraction"] = float(oos_frac)
            out["in_sample_bars"] = split_i
            out["out_of_sample_bars"] = oos_bars
            out["in_sample"] = {
                k: r_is[k] for k in (
                    "win_rate", "return_pct", "trade_count", "sharpe",
                    "max_drawdown", "profit_factor", "kpis",
                ) if k in r_is
            }
            if r_oos.get("ok"):
                out["oos_win_rate"] = r_oos.get("win_rate")
                out["oos_return_pct"] = r_oos.get("return_pct")
                out["oos_trade_count"] = r_oos.get("trade_count")
                out["out_of_sample"] = {
                    "win_rate": r_oos.get("win_rate"),
                    "return_pct": r_oos.get("return_pct"),
                    "trade_count": r_oos.get("trade_count"),
                    "profit_factor": r_oos.get("profit_factor"),
                    "avg_trade_pct": r_oos.get("avg_trade_pct"),
                    "kpis": r_oos.get("kpis"),
                }
            else:
                out["oos_win_rate"] = None
                out["oos_return_pct"] = None
                out["oos_trade_count"] = None
                out["out_of_sample"] = None
            out["oos_ok"] = bool(r_oos.get("ok"))

            extra_raw = (getattr(settings, "brain_oos_robustness_extra_fractions", "") or "").strip()
            rob_fracs: list[float] = []
            if extra_raw:
                for part in extra_raw.split(","):
                    try:
                        fx = float(part.strip())
                        if 0.05 < fx < 0.45 and abs(fx - float(oos_frac)) > 0.015:
                            rob_fracs.append(fx)
                    except ValueError:
                        pass
            rob_wrs: list[float] = []
            rob_pfs: list[Any] = []
            rob_atps: list[Any] = []
            for fx in rob_fracs:
                si2 = int(len(df) * (1.0 - float(fx)))
                o2 = len(df) - si2
                if si2 >= 30 and o2 >= 12:
                    r2 = _run_slice(df.iloc[si2:], False)
                    if r2.get("ok"):
                        rob_wrs.append(float(r2.get("win_rate") or 0))
                        rob_pfs.append(r2.get("profit_factor"))
                        rob_atps.append(r2.get("avg_trade_pct"))
            if rob_wrs or out.get("oos_win_rate") is not None:
                primary_wr = (
                    float(out["oos_win_rate"])
                    if out.get("oos_win_rate") is not None
                    else None
                )
                all_wrs = ([primary_wr] if primary_wr is not None else []) + rob_wrs
                out["oos_robustness"] = {
                    "primary_holdout_fraction": float(oos_frac),
                    "extra_holdout_fractions": rob_fracs,
                    "oos_win_rates_extra": rob_wrs,
                    "profit_factors_extra": rob_pfs,
                    "avg_trade_pcts_extra": rob_atps,
                    "oos_wr_min": min(all_wrs) if all_wrs else None,
                }
            return out

    out = _run_slice(df, True)
    out["spread_used"] = spread
    out["commission_used"] = commission
    return out


def benchmark_walk_forward_evaluate(
    *,
    conditions: list[dict[str, Any]],
    pattern_name: str,
    exit_config: dict[str, Any] | None,
    tickers: list[str],
    period: str,
    interval: str,
    n_windows: int = 8,
    min_bars_per_window: int = 35,
    min_positive_fold_ratio: float = 0.375,
    cash: float = 100_000,
    commission: float | None = None,
    spread: float | None = None,
) -> dict[str, Any]:
    """Contiguous-window backtests on a fixed ticker set (benchmark robustness).

    Fetches full history per ticker once, splits into windows, runs
    ``run_pattern_backtest`` with *df_override* per slice (no nested OOS split).
    Sets *passes_gate* when every ticker meets *min_positive_fold_ratio* for
    folds with positive return_pct.
    """
    if commission is None:
        commission = float(settings.backtest_commission)
    if spread is None:
        spread = float(settings.backtest_spread)

    n_win = max(2, int(n_windows))
    min_chunk = max(30, int(min_bars_per_window))
    tks = [str(t).strip().upper() for t in tickers if t and str(t).strip()]
    if not tks:
        return {"ok": False, "error": "no tickers", "passes_gate": False, "tickers": {}}

    out_tickers: dict[str, Any] = {}
    for ticker in tks:
        df = _fetch_ohlcv_df(ticker, period=period, interval=interval)
        if df.empty or len(df) < min_chunk * 2:
            out_tickers[ticker] = {
                "ok": False,
                "error": "not enough history",
                "windows": [],
            }
            continue
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        max_windows = max(2, len(df) // min_chunk)
        use_n = min(n_win, max_windows)
        chunk = len(df) // use_n
        if chunk < min_chunk:
            out_tickers[ticker] = {
                "ok": False,
                "error": "windows too short",
                "windows": [],
            }
            continue

        windows: list[dict[str, Any]] = []
        for k in range(use_n):
            start = k * chunk
            end = (k + 1) * chunk if k < use_n - 1 else len(df)
            sub = df.iloc[start:end]
            if len(sub) < min_chunk:
                continue
            wres = run_pattern_backtest(
                ticker=ticker,
                conditions=conditions,
                pattern_name=pattern_name,
                period=period,
                interval=interval,
                cash=cash,
                commission=commission,
                spread=spread,
                exit_config=exit_config,
                df_override=sub,
                oos_holdout_fraction=None,
            )
            ts0 = int(pd.Timestamp(sub.index[0]).timestamp())
            ts1 = int(pd.Timestamp(sub.index[-1]).timestamp())
            rp = wres.get("return_pct")
            windows.append({
                "index": k,
                "start_time": ts0,
                "end_time": ts1,
                "bars": len(sub),
                "ok": bool(wres.get("ok")),
                "return_pct": rp,
                "win_rate": wres.get("win_rate"),
                "trade_count": wres.get("trade_count"),
                "error": wres.get("error"),
            })

        returns = [
            float(w["return_pct"])
            for w in windows
            if w.get("ok") and w.get("return_pct") is not None
        ]
        win_rates = [
            float(w["win_rate"])
            for w in windows
            if w.get("ok") and w.get("win_rate") is not None
        ]
        trades = sum(int(w.get("trade_count") or 0) for w in windows if w.get("ok"))
        pos_ret = sum(
            1 for w in windows
            if w.get("ok") and w.get("return_pct") is not None and float(w["return_pct"]) > 0
        )
        n_w = len(windows)

        def _median(xs: list[float]) -> float | None:
            if not xs:
                return None
            s = sorted(xs)
            m = len(s) // 2
            return float(s[m]) if len(s) % 2 else float((s[m - 1] + s[m]) / 2)

        ticker_ok = n_w > 0 and all(w.get("ok") for w in windows)
        ratio = (pos_ret / n_w) if n_w else 0.0
        ticker_passes = ticker_ok and ratio >= float(min_positive_fold_ratio)

        out_tickers[ticker] = {
            "ok": ticker_ok,
            "windows": windows,
            "n_windows": n_w,
            "positive_return_windows": pos_ret,
            "positive_fold_ratio": round(ratio, 4),
            "passes_ratio_gate": ticker_passes,
            "median_return_pct": round(_median(returns), 2) if returns else None,
            "median_win_rate": round(_median(win_rates), 2) if win_rates else None,
            "total_trades": trades,
        }

    passes_gate = True
    for sym, rec in out_tickers.items():
        if not rec.get("ok"):
            passes_gate = False
            continue
        if not rec.get("passes_ratio_gate"):
            passes_gate = False

    return {
        "ok": True,
        "pattern_name": pattern_name,
        "period": period,
        "interval": interval,
        "n_windows_requested": n_win,
        "min_bars_per_window": min_chunk,
        "min_positive_fold_ratio": float(min_positive_fold_ratio),
        "tickers": out_tickers,
        "passes_gate": passes_gate,
    }


# ── Legacy generic-strategy fallback for backtest_pattern ────────────

_PATTERN_STRATEGY_MAP = {
    "rsi": "momentum_breakout",
    "ema": "momentum_breakout",
    "momentum": "momentum_breakout",
    "breakout": "momentum_breakout",
    "bollinger": "bb_bounce",
    "squeeze": "bb_bounce",
    "macd": "macd",
    "trend": "trend_follow",
    "sma": "sma_cross",
    "vwap": "trend_follow",
}


def backtest_pattern(
    ticker: str,
    pattern_name: str,
    rules_json: str,
    interval: str = "1d",
    period: str = "1y",
    exit_config: str | dict[str, Any] | None = None,
    *,
    cash: float = 100_000,
    commission: float | None = None,
    spread: float | None = None,
    oos_holdout_fraction: float | None = None,
    ohlc_start: str | None = None,
    ohlc_end: str | None = None,
    rules_json_override: str | None = None,
    append_conditions: list[dict[str, Any]] | None = None,
    exit_config_overlay: dict[str, Any] | None = None,
    scan_pattern_id: int | None = None,
) -> dict[str, Any]:
    """Run a backtest for a ScanPattern.

    If the pattern has valid ``rules_json`` conditions, uses the pattern-aware
    ``run_pattern_backtest`` which evaluates the actual composite conditions
    bar-by-bar.  Falls back to a generic strategy mapping only when conditions
    are absent or unparseable.

    Optional ``rules_json_override`` replaces the pattern's stored rules entirely.
    ``append_conditions`` are AND-appended to the parsed conditions list.
    """
    raw_rules = rules_json_override if rules_json_override is not None else rules_json
    conditions: list[dict[str, Any]] = []
    try:
        rules = json.loads(raw_rules) if raw_rules else {}
        conditions = rules.get("conditions", [])
    except (json.JSONDecodeError, TypeError):
        pass

    if append_conditions:
        conditions = list(conditions) + list(append_conditions)

    exit_cfg: dict[str, Any] | None = None
    if exit_config is not None:
        if isinstance(exit_config, dict):
            exit_cfg = exit_config
        else:
            try:
                exit_cfg = json.loads(exit_config) if exit_config else None
            except (json.JSONDecodeError, TypeError):
                pass

    if exit_config_overlay:
        exit_cfg = {**(exit_cfg or {}), **exit_config_overlay}

    if conditions:
        result = run_pattern_backtest(
            ticker=ticker,
            conditions=conditions,
            pattern_name=pattern_name,
            period=period,
            interval=interval,
            cash=cash,
            commission=commission,
            exit_config=exit_cfg,
            spread=spread,
            oos_holdout_fraction=oos_holdout_fraction,
            ohlc_start=ohlc_start,
            ohlc_end=ohlc_end,
            scan_pattern_id=scan_pattern_id,
        )
        result["pattern_name"] = pattern_name
        result["mapped_strategy"] = "dynamic_pattern"
        return result

    # Fallback: map pattern keywords to a generic pre-built strategy
    strategy_id = "momentum_breakout"
    name_lower = pattern_name.lower()
    for keyword, sid in _PATTERN_STRATEGY_MAP.items():
        if keyword in name_lower:
            strategy_id = sid
            break

    _comm = commission if commission is not None else float(settings.backtest_commission)
    result = run_backtest(
        ticker=ticker,
        strategy_id=strategy_id,
        period=period,
        interval=interval,
        cash=cash,
        commission=_comm,
    )
    result["pattern_name"] = pattern_name
    result["mapped_strategy"] = strategy_id
    return result


# ── Adversarial / Stress Testing ──────────────────────────────────────

CRISIS_PERIODS: dict[str, tuple[str, str]] = {
    "covid_crash": ("2020-02-19", "2020-03-23"),
    "covid_recovery": ("2020-03-24", "2020-06-08"),
    "2022_bear": ("2022-01-03", "2022-10-12"),
    "svb_crisis": ("2023-03-08", "2023-03-15"),
    "tariff_shock_2025": ("2025-04-02", "2025-04-09"),
}


def run_stress_backtest(
    db: Session,
    pattern_id: int,
    crisis_key: str,
    *,
    tickers: list[str] | None = None,
    commission: float | None = None,
    spread: float | None = None,
) -> dict[str, Any]:
    """Run a pattern backtest restricted to a historical crisis period.

    Returns aggregated stats (win rate, max drawdown, avg return) across
    the requested tickers, or an error dict if the crisis key is unknown
    or data is insufficient.
    """
    from ..models.trading import ScanPattern

    if crisis_key not in CRISIS_PERIODS:
        return {"ok": False, "error": f"Unknown crisis key: {crisis_key}", "available": list(CRISIS_PERIODS)}

    start_date, end_date = CRISIS_PERIODS[crisis_key]
    pattern = db.query(ScanPattern).get(pattern_id)
    if pattern is None:
        return {"ok": False, "error": f"Pattern {pattern_id} not found"}

    if tickers is None:
        ac = (pattern.asset_class or "all").strip().lower()
        if ac == "crypto":
            tickers = ["BTC-USD", "ETH-USD", "SOL-USD"]
        else:
            tickers = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"]

    results: list[dict[str, Any]] = []
    for ticker in tickers:
        try:
            rules = pattern.rules_json
            if isinstance(rules, dict):
                rules = json.dumps(rules)
            r = backtest_pattern(
                ticker=ticker,
                pattern_name=pattern.name,
                rules_json=rules,
                interval=pattern.timeframe or "1d",
                period="max",
                ohlc_start=start_date,
                ohlc_end=end_date,
                commission=commission,
                spread=spread,
            )
            if r and r.get("return_pct") is not None:
                results.append({
                    "ticker": ticker,
                    "return_pct": r.get("return_pct"),
                    "win_rate": r.get("win_rate"),
                    "max_drawdown": r.get("max_drawdown"),
                    "trade_count": r.get("trade_count", 0),
                    "sharpe": r.get("sharpe"),
                })
        except Exception as e:
            logger.debug("[stress] %s/%s failed: %s", ticker, crisis_key, e)

    if not results:
        return {
            "ok": False,
            "crisis_key": crisis_key,
            "period": f"{start_date} → {end_date}",
            "error": "No usable backtest results (insufficient data for period)",
        }

    avg_return = sum(r["return_pct"] for r in results) / len(results)
    avg_wr = sum((r["win_rate"] or 0) for r in results) / len(results)
    worst_dd = min((r["max_drawdown"] or 0) for r in results)
    total_trades = sum(r["trade_count"] for r in results)
    survived = all((r["return_pct"] or 0) > -50 for r in results)

    return {
        "ok": True,
        "crisis_key": crisis_key,
        "period": f"{start_date} → {end_date}",
        "pattern_id": pattern_id,
        "pattern_name": pattern.name,
        "tickers_tested": len(results),
        "avg_return_pct": round(avg_return, 2),
        "avg_win_rate": round(avg_wr, 1),
        "worst_max_drawdown": round(worst_dd, 2),
        "total_trades": total_trades,
        "survived": survived,
        "per_ticker": results,
    }
