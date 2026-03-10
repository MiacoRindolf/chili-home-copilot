"""Backtesting engine: pre-built strategies powered by backtesting.py."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pandas as pd
import yfinance as yf
from backtesting import Backtest, Strategy
from backtesting.lib import crossover
from sqlalchemy.orm import Session

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
}


def list_strategies() -> list[dict[str, str]]:
    return [
        {"id": k, "name": v["name"], "description": v["description"]}
        for k, v in STRATEGIES.items()
    ]


# ── Run backtest ────────────────────────────────────────────────────────

def run_backtest(
    ticker: str,
    strategy_id: str = "sma_cross",
    period: str = "1y",
    cash: float = 10000,
    commission: float = 0.001,
    optimize: bool = False,
) -> dict[str, Any]:
    """Run a backtest and return results dict."""
    if strategy_id not in STRATEGIES:
        return {"ok": False, "error": f"Unknown strategy: {strategy_id}"}

    t = yf.Ticker(ticker)
    df = t.history(period=period, interval="1d")
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
