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


def _finite_float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _positive_float_or_none(value: Any) -> float | None:
    out = _finite_float_or_none(value)
    return out if out is not None and out > 0 else None


def _paper_unrealized_pnl(
    pos: PaperTrade,
    *,
    current_price: Any,
    multiplier: Any,
) -> float | None:
    entry = _positive_float_or_none(getattr(pos, "entry_price", None))
    price = _positive_float_or_none(current_price)
    qty = _positive_float_or_none(getattr(pos, "quantity", None))
    mult = _positive_float_or_none(multiplier)
    if entry is None or price is None or qty is None or mult is None:
        return None

    if str(getattr(pos, "direction", "") or "").strip().lower() == "short":
        return (entry - price) * qty * mult
    return (price - entry) * qty * mult


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
    try:
        from .paper_trading import (
            _is_option_paper_trade,
            _paper_contract_multiplier,
            _paper_current_mark_price,
        )
    except Exception:
        _is_option_paper_trade = None
        _paper_contract_multiplier = None
        _paper_current_mark_price = None

    open_trades = db.query(PaperTrade).filter(PaperTrade.status == "open")
    if user_id is not None:
        open_trades = open_trades.filter(PaperTrade.user_id == user_id)
    positions = open_trades.all()

    total_unrealized = 0.0
    valuation_missing_count = 0
    for pos in positions:
        try:
            multiplier = 1.0
            price: float | None = None
            is_option = callable(_is_option_paper_trade) and _is_option_paper_trade(pos)
            if is_option:
                if callable(_paper_contract_multiplier):
                    multiplier = _paper_contract_multiplier(pos)
                if callable(_paper_current_mark_price):
                    mark = _paper_current_mark_price(pos, purpose="portfolio_drawdown")
                    price = _positive_float_or_none(mark)
            else:
                q = fetch_quote(pos.ticker)
                if q and q.get("price"):
                    price = _positive_float_or_none(q["price"])
            if price is None:
                valuation_missing_count += 1
                continue
            pnl = _paper_unrealized_pnl(pos, current_price=price, multiplier=multiplier)
            if pnl is not None:
                total_unrealized += pnl
            else:
                valuation_missing_count += 1
        except Exception:
            valuation_missing_count += 1
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
        pnl = _finite_float_or_none(getattr(t, "pnl", None))
        if pnl is not None:
            closed_pnl += pnl

    total_pnl = total_unrealized + closed_pnl
    capital_f = _positive_float_or_none(capital)
    if capital_f is None:
        return {
            "ok": False,
            "reason": "invalid_capital",
            "unrealized_pnl": round(total_unrealized, 2),
            "closed_30d_pnl": round(closed_pnl, 2),
            "total_pnl": round(total_pnl, 2),
            "dd_pct": 0.0,
            "max_dd_pct": max_dd_pct,
            "breached": True,
            "valuation_missing_count": valuation_missing_count,
            "valuation_complete": valuation_missing_count == 0,
            "open_positions": len(positions),
        }

    dd_pct = total_pnl / capital_f * 100

    breached = dd_pct < -max_dd_pct
    reason = "drawdown_breached" if breached else None
    if valuation_missing_count > 0:
        breached = True
        reason = "valuation_unavailable"

    if breached:
        logger.warning(
            "[portfolio_opt] Portfolio DD blocked: reason=%s dd=%.1f%% "
            "(limit -%.1f%%) valuation_missing=%d",
            reason,
            dd_pct,
            max_dd_pct,
            valuation_missing_count,
        )

    return {
        "ok": not breached,
        "reason": reason,
        "unrealized_pnl": round(total_unrealized, 2),
        "closed_30d_pnl": round(closed_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "dd_pct": round(dd_pct, 2),
        "max_dd_pct": max_dd_pct,
        "breached": breached,
        "valuation_missing_count": valuation_missing_count,
        "valuation_complete": valuation_missing_count == 0,
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
