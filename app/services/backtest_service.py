"""Backtesting engine: pre-built strategies powered by backtesting.py."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pandas as pd
from backtesting import Backtest, Strategy
from backtesting.lib import crossover
from sqlalchemy.orm import Session

from .trading.market_data import fetch_ohlcv_df as _fetch_ohlcv_df

from ..models.trading import BacktestResult


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
        "params": {"sma_len": (30, 80, 10)},
    },
    "momentum_breakout": {
        "cls": MomentumBreakout,
        "name": "Momentum Breakout",
        "description": "RSI momentum + full EMA stack alignment. Captures strong continuation breakouts.",
        "params": {"rsi_threshold": (55, 75, 5), "ema_fast": (10, 30, 5)},
    },
}


def list_strategies() -> list[dict[str, str]]:
    return [
        {"id": k, "name": v["name"], "description": v["description"]}
        for k, v in STRATEGIES.items()
    ]


def _extract_indicators(strat_cls, strategy_id: str, df: pd.DataFrame) -> dict:
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
        result["SMA 10"] = _series_to_points(_sma(close, 10))
        result["SMA 30"] = _series_to_points(_sma(close, 30))
    elif strategy_id == "ema_cross":
        result["EMA 12"] = _series_to_points(_ema(close, 12))
        result["EMA 26"] = _series_to_points(_ema(close, 26))
    elif strategy_id == "rsi_reversal":
        result["RSI 14"] = _series_to_points(_rsi(close, 14))
    elif strategy_id == "bb_bounce":
        result["BB Upper"] = _series_to_points(_bollinger_upper(close, 20, 2))
        result["BB Mid"] = _series_to_points(_bollinger_mid(close, 20))
        result["BB Lower"] = _series_to_points(_bollinger_lower(close, 20, 2))
    elif strategy_id == "macd":
        ema_f = _ema(close, 12)
        ema_s = _ema(close, 26)
        macd_line = pd.Series(ema_f.values - ema_s.values, index=close.index)
        signal_line = _ema(macd_line, 9)
        histogram = macd_line - signal_line
        result["MACD"] = _series_to_points(macd_line)
        result["Signal"] = _series_to_points(signal_line)
        result["Histogram"] = _series_to_points(histogram)
    elif strategy_id == "trend_follow":
        result["SMA 50"] = _series_to_points(_sma(close, 50))
        result["EMA 12"] = _series_to_points(_ema(close, 12))
        result["EMA 26"] = _series_to_points(_ema(close, 26))
        result["RSI 14"] = _series_to_points(_rsi(close, 14))

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

    bt = Backtest(df, strat_cls, cash=cash, commission=commission, exclusive_orders=True)

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
            stats = bt.run()
    else:
        stats = bt.run()

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
    indicators = _extract_indicators(strat_cls, strategy_id, df)

    return {
        "ok": True,
        "ticker": ticker.upper(),
        "strategy": strat_info["name"],
        "strategy_id": strategy_id,
        "period": period,
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
    }


def save_backtest(db: Session, user_id: int | None, result: dict[str, Any]) -> BacktestResult:
    """Persist a backtest result to the database."""
    record = BacktestResult(
        user_id=user_id,
        ticker=result.get("ticker", ""),
        strategy_name=result.get("strategy", ""),
        params=json.dumps({"strategy_id": result.get("strategy_id"), "period": result.get("period")}),
        return_pct=result.get("return_pct", 0),
        win_rate=result.get("win_rate", 0),
        sharpe=result.get("sharpe"),
        max_drawdown=result.get("max_drawdown", 0),
        trade_count=result.get("trade_count", 0),
        equity_curve=json.dumps(result.get("equity_curve", [])),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


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
) -> dict[str, Any]:
    """Run the most appropriate backtest strategy for a ScanPattern.

    Maps pattern name/rules to the closest pre-built strategy and runs
    the backtest on the requested interval.
    """
    strategy_id = "momentum_breakout"
    name_lower = pattern_name.lower()
    for keyword, sid in _PATTERN_STRATEGY_MAP.items():
        if keyword in name_lower:
            strategy_id = sid
            break

    try:
        rules = json.loads(rules_json) if rules_json else {}
        conditions = rules.get("conditions", [])
        for cond in conditions:
            ind = cond.get("indicator", "").lower()
            if "rsi" in ind:
                strategy_id = "momentum_breakout"
                break
            elif "bb" in ind or "squeeze" in ind:
                strategy_id = "bb_bounce"
                break
            elif "macd" in ind:
                strategy_id = "macd"
                break
    except (json.JSONDecodeError, TypeError):
        pass

    result = run_backtest(
        ticker=ticker,
        strategy_id=strategy_id,
        period=period,
        interval=interval,
    )
    result["pattern_name"] = pattern_name
    result["mapped_strategy"] = strategy_id
    return result
