"""Lightweight portfolio optimization — equal risk contribution and correlation persistence.

Implements:
- Equal-risk-contribution (ERC) allocation across active patterns
- Rolling correlation matrix computation and storage
- Portfolio-level drawdown enforcement distinct from per-trade stops
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from ...models.trading import PaperTrade, ScanPattern

logger = logging.getLogger(__name__)


def compute_rolling_correlations(
    db: Session,
    window_days: int = 60,
) -> dict[str, Any]:
    """Compute pairwise return correlations across all active pattern tickers."""
    from .market_data import fetch_ohlcv_df

    active = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.lifecycle_stage.in_(("promoted", "live")),
        )
        .all()
    )

    tickers: set[str] = set()
    for pat in active:
        if pat.scope_tickers:
            try:
                tks = pat.scope_tickers if isinstance(pat.scope_tickers, list) else []
                tickers.update(tks[:5])
            except Exception:
                pass

    open_paper = db.query(PaperTrade).filter(PaperTrade.status == "open").all()
    for pt in open_paper:
        tickers.add(pt.ticker)

    if len(tickers) < 2:
        return {"ok": True, "tickers": list(tickers), "correlation_matrix": {}}

    ticker_list = sorted(tickers)[:20]
    returns_map: dict[str, list[float]] = {}

    for ticker in ticker_list:
        try:
            df = fetch_ohlcv_df(ticker, period=f"{window_days}d", interval="1d")
            if df is not None and len(df) >= 10:
                close = df["Close"].values
                rets = list(np.diff(np.log(close.astype(float))))
                returns_map[ticker] = rets
        except Exception:
            continue

    valid_tickers = [t for t in ticker_list if t in returns_map]
    if len(valid_tickers) < 2:
        return {"ok": True, "tickers": valid_tickers, "correlation_matrix": {}}

    min_len = min(len(returns_map[t]) for t in valid_tickers)
    matrix = np.array([returns_map[t][-min_len:] for t in valid_tickers])
    corr = np.corrcoef(matrix)

    corr_dict = {}
    for i, t1 in enumerate(valid_tickers):
        for j, t2 in enumerate(valid_tickers):
            if i < j:
                val = float(corr[i, j])
                if not math.isnan(val):
                    corr_dict[f"{t1}:{t2}"] = round(val, 4)

    return {
        "ok": True,
        "tickers": valid_tickers,
        "window_days": window_days,
        "computed_at": datetime.utcnow().isoformat() + "Z",
        "correlation_matrix": corr_dict,
    }


def equal_risk_contribution(
    db: Session,
    capital: float = 100_000.0,
    max_portfolio_dd_pct: float = 15.0,
) -> dict[str, Any]:
    """Allocate capital using equal-risk-contribution across active patterns.

    Each pattern gets capital proportional to 1/volatility, so that the
    risk contribution from each position is roughly equal.
    """
    from .market_data import fetch_ohlcv_df
    from .indicator_core import compute_atr

    active = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.lifecycle_stage.in_(("promoted", "live")),
        )
        .all()
    )

    if not active:
        return {"ok": True, "allocations": [], "method": "erc"}

    pattern_vols: list[tuple[ScanPattern, float]] = []
    for pat in active:
        ticker = _primary_ticker(pat)
        if not ticker:
            continue
        try:
            df = fetch_ohlcv_df(ticker, period="3mo", interval="1d")
            if df is None or len(df) < 20:
                continue
            atr = compute_atr(df["High"].values, df["Low"].values, df["Close"].values, period=14)
            atr_val = float(atr[-1])
            price = float(df["Close"].iloc[-1])
            if atr_val > 0 and price > 0:
                vol_pct = atr_val / price
                pattern_vols.append((pat, vol_pct))
        except Exception:
            continue

    if not pattern_vols:
        return {"ok": True, "allocations": [], "method": "erc", "reason": "no_volatility_data"}

    inv_vols = [1.0 / v for _, v in pattern_vols]
    total_inv = sum(inv_vols)

    allocations = []
    for i, (pat, vol) in enumerate(pattern_vols):
        fraction = inv_vols[i] / total_inv if total_inv > 0 else 1.0 / len(pattern_vols)
        alloc_capital = capital * fraction

        allocations.append({
            "pattern_id": pat.id,
            "pattern_name": pat.name,
            "volatility_pct": round(vol * 100, 2),
            "weight": round(fraction, 4),
            "capital": round(alloc_capital, 2),
        })

    allocations.sort(key=lambda a: a["weight"], reverse=True)

    return {
        "ok": True,
        "method": "erc",
        "total_capital": capital,
        "max_portfolio_dd_pct": max_portfolio_dd_pct,
        "allocated": round(sum(a["capital"] for a in allocations), 2),
        "n_patterns": len(allocations),
        "allocations": allocations,
    }


def check_portfolio_drawdown(
    db: Session,
    user_id: int | None = None,
    capital: float = 100_000.0,
    max_dd_pct: float = 15.0,
) -> dict[str, Any]:
    """Check portfolio-level drawdown across all open positions."""
    from .market_data import fetch_quote

    open_trades = db.query(PaperTrade).filter(PaperTrade.status == "open")
    if user_id is not None:
        open_trades = open_trades.filter(PaperTrade.user_id == user_id)
    positions = open_trades.all()

    total_unrealized = 0.0
    for pos in positions:
        try:
            q = fetch_quote(pos.ticker)
            if q and q.get("price"):
                price = float(q["price"])
                if pos.direction == "long":
                    total_unrealized += (price - pos.entry_price) * pos.quantity
                else:
                    total_unrealized += (pos.entry_price - price) * pos.quantity
        except Exception:
            continue

    cutoff = datetime.utcnow() - timedelta(days=30)
    closed_pnl = 0.0
    closed = db.query(PaperTrade).filter(
        PaperTrade.status == "closed",
        PaperTrade.exit_date >= cutoff,
    )
    if user_id is not None:
        closed = closed.filter(PaperTrade.user_id == user_id)
    for t in closed.all():
        closed_pnl += t.pnl or 0

    total_pnl = total_unrealized + closed_pnl
    dd_pct = (total_pnl / capital * 100) if capital > 0 else 0

    breached = dd_pct < -max_dd_pct

    if breached:
        logger.warning(
            "[portfolio_opt] Portfolio DD breached: %.1f%% (limit -%.1f%%)",
            dd_pct, max_dd_pct,
        )

    return {
        "ok": True,
        "unrealized_pnl": round(total_unrealized, 2),
        "closed_30d_pnl": round(closed_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "dd_pct": round(dd_pct, 2),
        "max_dd_pct": max_dd_pct,
        "breached": breached,
        "open_positions": len(positions),
    }


def _primary_ticker(pat: ScanPattern) -> str | None:
    """Get the primary ticker for a pattern."""
    if pat.scope_tickers:
        try:
            tks = pat.scope_tickers if isinstance(pat.scope_tickers, list) else []
            if tks:
                return tks[0]
        except Exception:
            pass
    return None
