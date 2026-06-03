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
import math
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import Trade
from .asset_class import PATTERN_ASSET_CLASS_OPTIONS, normalize_pattern_asset_class
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
    if normalize_pattern_asset_class(family) == PATTERN_ASSET_CLASS_OPTIONS:
        family = _asset_family(sym)
    if sym.endswith("-USD"):
        return f"crypto:{sym.split('-')[0]}"
    return f"{family}:{sym[:1] or 'x'}"


def _finite_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        if value is None or value == "":
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _positive_float(value: object) -> float | None:
    out = _finite_float(value, 0.0)
    return out if out > 0.0 else None


def _bucket_cap_pct(asset_class: Optional[str]) -> float:
    family = (asset_class or "").strip().lower()
    if family == "crypto":
        return max(
            0.0,
            _finite_float(
                getattr(settings, "brain_position_sizer_crypto_bucket_cap_pct", 10.0),
                10.0,
            ),
        )
    return max(
        0.0,
        _finite_float(
            getattr(settings, "brain_position_sizer_equity_bucket_cap_pct", 15.0),
            15.0,
        ),
    )


def _trade_budget_field(row: Any, field: str, index: int) -> Any:
    if isinstance(row, (tuple, list)):
        return row[index] if len(row) > index else None
    return getattr(row, field, None)


def _trade_budget_row(row: Any) -> Any:
    if not isinstance(row, (tuple, list)):
        return row
    return SimpleNamespace(
        ticker=_trade_budget_field(row, "ticker", 0),
        quantity=_trade_budget_field(row, "quantity", 1),
        entry_price=_trade_budget_field(row, "entry_price", 2),
        asset_kind=_trade_budget_field(row, "asset_kind", 3),
        tags=_trade_budget_field(row, "tags", 4),
        indicator_snapshot=_trade_budget_field(row, "indicator_snapshot", 5),
    )


def _is_option_trade_safe(trade: Any) -> bool:
    try:
        from .autopilot_scope import is_option_trade

        return bool(is_option_trade(trade))
    except Exception:
        return False


def _trade_notional_usd(trade: Any) -> float:
    trade = _trade_budget_row(trade)
    qty = _positive_float(getattr(trade, "quantity", None))
    entry = _positive_float(getattr(trade, "entry_price", None))
    if qty is None or entry is None:
        return 0.0
    multiplier = 100.0 if _is_option_trade_safe(trade) else 1.0
    return abs(qty * entry * multiplier)


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
    Notional is approximated by ``quantity * entry_price`` for spot
    assets and ``contracts * premium * 100`` for options. We intentionally
    use entry (not mark) so the budget is stable under price fluctuations
    within a single bar.
    """
    bucket = bucket_for(ticker, asset_class=asset_class)
    cap_pct = _bucket_cap_pct(asset_class or _asset_family(ticker))
    capital_f = max(0.0, _finite_float(capital, 0.0))
    max_bucket_notional = capital_f * cap_pct / 100.0

    try:
        q = (
            db.query(
                Trade.ticker,
                Trade.quantity,
                Trade.entry_price,
                Trade.asset_kind,
                Trade.tags,
                Trade.indicator_snapshot,
            )
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
            other_bucket = bucket_for(_trade_budget_field(row, "ticker", 0))
            if other_bucket != bucket:
                continue
            open_notional += _trade_notional_usd(row)
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
    rows: list[Any] = []
    try:
        q = db.query(
            Trade.ticker,
            Trade.quantity,
            Trade.entry_price,
            Trade.asset_kind,
            Trade.tags,
            Trade.indicator_snapshot,
        ).filter(Trade.status == "open")
        if user_id is not None:
            q = q.filter(Trade.user_id == user_id)
        rows = q.all()
        total_deployed = sum(_trade_notional_usd(row) for row in rows)
    except Exception:
        logger.warning("[correlation_budget] failed to read portfolio notional", exc_info=True)
        total_deployed = 0.0

    wanted_ticker = str(ticker or "").strip().upper()
    ticker_open = sum(
        _trade_notional_usd(row)
        for row in rows
        if str(_trade_budget_field(row, "ticker", 0) or "").strip().upper() == wanted_ticker
    )

    capital_f = max(0.0, _finite_float(capital, 0.0))
    cap_pct = max(0.0, _finite_float(max_total_notional_pct, 0.0))
    cap_total = capital_f * cap_pct / 100.0
    return PortfolioBudget(
        total_capital=capital_f,
        deployed_notional=round(total_deployed, 6),
        max_total_notional=round(cap_total, 6),
        ticker_open_notional=round(ticker_open, 6),
    )
