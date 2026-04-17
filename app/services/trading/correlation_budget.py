"""DB-aware helper for the canonical Phase H position sizer.

Computes the :class:`CorrelationBudget` (and incidentally the
:class:`PortfolioBudget`) the caller should pass into
:func:`position_sizer_model.compute_proposal`. Kept separate from
the pure math module so unit tests can exercise ``compute_proposal``
without a database.

The correlation bucket key is intentionally shared with the existing
:mod:`.portfolio_allocator` so Phase H and the live-session allocator
agree on what "correlated" means. Refining the bucket granularity
(sector ETFs, factor exposures, crypto betas) is **Phase H.2**.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import Trade
from .position_sizer_model import CorrelationBudget, PortfolioBudget

logger = logging.getLogger(__name__)

__all__ = [
    "bucket_for",
    "is_crypto_symbol",
    "compute_correlation_budget",
    "compute_portfolio_budget",
]


def is_crypto_symbol(symbol: Optional[str]) -> bool:
    """Mirror :func:`portfolio_allocator._symbol_asset_family` for crypto detection."""
    sym = (symbol or "").strip().upper()
    return sym.endswith("-USD") or sym.endswith("USD") and len(sym) >= 5 and not sym.startswith("X:")


def _asset_family(symbol: Optional[str]) -> str:
    sym = (symbol or "").strip().upper()
    if sym.endswith("-USD"):
        return "crypto"
    try:
        from .backtest_engine import TICKER_TO_SECTOR  # type: ignore

        return TICKER_TO_SECTOR.get(sym, "equity")
    except Exception:
        return "equity"


def bucket_for(symbol: Optional[str], *, asset_class: Optional[str] = None) -> str:
    """Canonical correlation bucket key.

    Matches :func:`portfolio_allocator._correlation_bucket` so the two
    systems agree on bucketing. For equities the bucket is
    ``"<family>:<first-letter>"``; for crypto it is ``"crypto:<BASE>"``.
    """
    sym = (symbol or "").strip().upper()
    family = (asset_class or "").strip().lower() or _asset_family(sym)
    if sym.endswith("-USD"):
        return f"crypto:{sym.split('-')[0]}"
    return f"{family}:{sym[:1] or 'x'}"


def _bucket_cap_pct(asset_class: Optional[str]) -> float:
    family = (asset_class or "").strip().lower()
    if family == "crypto":
        return float(getattr(settings, "brain_position_sizer_crypto_bucket_cap_pct", 10.0))
    return float(getattr(settings, "brain_position_sizer_equity_bucket_cap_pct", 15.0))


def compute_correlation_budget(
    db: Session,
    *,
    user_id: Optional[int],
    ticker: str,
    capital: float,
    asset_class: Optional[str] = None,
) -> CorrelationBudget:
    """Sum open-trade notional in ``ticker``'s correlation bucket.

    Scope: trades where ``status == 'open'`` for the given ``user_id``.
    Notional is approximated by ``quantity * entry_price``; we
    intentionally use entry (not mark) so the budget is stable under
    price fluctuations within a single bar.
    """
    bucket = bucket_for(ticker, asset_class=asset_class)
    cap_pct = _bucket_cap_pct(asset_class or _asset_family(ticker))
    max_bucket_notional = max(0.0, float(capital or 0.0)) * cap_pct / 100.0

    try:
        q = (
            db.query(Trade)
            .filter(Trade.status == "open")
            .filter(Trade.ticker.isnot(None))
        )
        if user_id is not None:
            q = q.filter(Trade.user_id == user_id)
        rows = q.all()
    except Exception:
        logger.warning("[correlation_budget] failed to read open trades", exc_info=True)
        rows = []

    open_notional = 0.0
    for row in rows:
        try:
            other_bucket = bucket_for(row.ticker)
            if other_bucket != bucket:
                continue
            qty = float(row.quantity or 0.0)
            entry = float(row.entry_price or 0.0)
            if qty <= 0 or entry <= 0:
                continue
            open_notional += qty * entry
        except Exception:
            continue

    return CorrelationBudget(
        bucket=bucket,
        open_notional=round(open_notional, 6),
        max_bucket_notional=round(max_bucket_notional, 6),
    )


def compute_portfolio_budget(
    db: Session,
    *,
    user_id: Optional[int],
    ticker: str,
    capital: float,
    max_total_notional_pct: float = 100.0,
) -> PortfolioBudget:
    """Aggregate open-trade exposure for the portfolio safety floor.

    ``max_total_notional_pct`` defaults to 100% of capital - Phase H
    does not enforce a gross-exposure cap (that is Phase I's risk
    dial). It is included here so the caller can pass a tighter
    limit when testing.
    """
    try:
        q = db.query(
            func.coalesce(func.sum(Trade.quantity * Trade.entry_price), 0.0),
        ).filter(Trade.status == "open")
        if user_id is not None:
            q = q.filter(Trade.user_id == user_id)
        total_deployed = float(q.scalar() or 0.0)
    except Exception:
        logger.warning("[correlation_budget] failed to read portfolio notional", exc_info=True)
        total_deployed = 0.0

    ticker_open = 0.0
    try:
        q = (
            db.query(
                func.coalesce(func.sum(Trade.quantity * Trade.entry_price), 0.0),
            )
            .filter(Trade.status == "open")
            .filter(Trade.ticker == ticker)
        )
        if user_id is not None:
            q = q.filter(Trade.user_id == user_id)
        ticker_open = float(q.scalar() or 0.0)
    except Exception:
        logger.warning("[correlation_budget] failed to read ticker notional", exc_info=True)
        ticker_open = 0.0

    cap_total = max(0.0, float(capital or 0.0)) * max(0.0, float(max_total_notional_pct)) / 100.0
    return PortfolioBudget(
        total_capital=float(capital or 0.0),
        deployed_notional=round(total_deployed, 6),
        max_total_notional=round(cap_total, 6),
        ticker_open_notional=round(ticker_open, 6),
    )
