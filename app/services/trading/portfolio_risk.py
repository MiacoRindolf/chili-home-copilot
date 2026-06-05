"""Portfolio-level risk controls for the trading brain.

Enforces hard caps before any new position is opened:
- Max concurrent open positions
- Portfolio heat cap (total risk across all open positions)
- Sector/asset-class concentration limits
- Per-trade risk sizing (fixed fractional)
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import Trade
from .asset_class import (
    PATTERN_ASSET_CLASS_CRYPTO,
    PATTERN_ASSET_CLASS_OPTIONS,
    PATTERN_ASSET_CLASS_STOCKS,
    normalize_pattern_asset_class,
)
from .return_math import trade_realized_pnl

logger = logging.getLogger(__name__)
OPTION_HEAT_STOP_PCT_DEFAULT = 50.0
OPTION_HEAT_STOP_PCT_MIN = 10.0
OPTION_HEAT_STOP_PCT_MAX = 80.0


def _finite_float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _trade_realized_pnl_with_raw_fallback(trade: Any) -> float | None:
    pnl = trade_realized_pnl(trade)
    if pnl is not None:
        return pnl
    return _finite_float_or_none(getattr(trade, "pnl", None))


def _sum_trade_realized_pnl(trades: list[Any]) -> float:
    return sum(
        pnl
        for pnl in (_trade_realized_pnl_with_raw_fallback(t) for t in trades)
        if pnl is not None
    )


@dataclass
class RiskBudget:
    """Snapshot of current portfolio risk exposure."""
    open_positions: int = 0
    crypto_positions: int = 0
    stock_positions: int = 0
    option_positions: int = 0
    total_heat_pct: float = 0.0
    available_heat_pct: float = 0.0
    can_open_new: bool = True
    rejection_reason: str | None = None
    sector_exposure: dict[str, float] = field(default_factory=dict)
    portfolio_var_pct: float | None = None
    portfolio_cvar_pct: float | None = None


@dataclass
class RiskLimits:
    max_open_positions: int = 10
    max_crypto_positions: int = 5
    max_stock_positions: int = 8
    max_portfolio_heat_pct: float = 6.0  # total risk as % of capital
    max_risk_per_trade_pct: float = 1.0  # max 1% capital per trade
    max_same_ticker: int = 2
    max_sector_pct: float = 40.0         # max % of open positions in one sector
    max_avg_correlation: float = 0.75    # reject if avg correl with open positions exceeds this


def get_risk_limits(settings: Any | None = None) -> RiskLimits:
    """Build limits from app settings, falling back to conservative defaults."""
    if settings is None:
        from ...config import settings as _s
        settings = _s
    defaults = RiskLimits()
    return RiskLimits(
        max_open_positions=_risk_limit_int(
            settings,
            "brain_risk_max_positions",
            defaults.max_open_positions,
        ),
        max_crypto_positions=_risk_limit_int(
            settings,
            "brain_risk_max_crypto",
            defaults.max_crypto_positions,
        ),
        max_stock_positions=_risk_limit_int(
            settings,
            "brain_risk_max_stocks",
            defaults.max_stock_positions,
        ),
        max_portfolio_heat_pct=_risk_limit_float(
            settings,
            "brain_risk_max_heat_pct",
            defaults.max_portfolio_heat_pct,
            max_value=100.0,
        ),
        max_risk_per_trade_pct=_risk_limit_float(
            settings,
            "brain_risk_max_risk_per_trade_pct",
            defaults.max_risk_per_trade_pct,
            max_value=100.0,
            fallback_names=("brain_risk_per_trade_pct",),
        ),
        max_same_ticker=_risk_limit_int(
            settings,
            "brain_risk_max_same_ticker",
            defaults.max_same_ticker,
        ),
        max_sector_pct=_risk_limit_float(
            settings,
            "brain_risk_max_sector_pct",
            defaults.max_sector_pct,
            max_value=100.0,
        ),
        max_avg_correlation=_risk_limit_float(
            settings,
            "brain_risk_max_avg_correlation",
            defaults.max_avg_correlation,
            max_value=1.0,
        ),
    )


def _risk_limit_float(
    settings: Any,
    name: str,
    default: float,
    *,
    min_value: float = 0.0,
    max_value: float | None = None,
    fallback_names: tuple[str, ...] = (),
) -> float:
    raw = getattr(settings, name, None)
    if raw in (None, ""):
        for fallback_name in fallback_names:
            raw = getattr(settings, fallback_name, None)
            if raw not in (None, ""):
                break
    value = _risk_limit_number(raw)
    if value is None or value < min_value:
        return float(default)
    if max_value is not None and value > max_value:
        return float(default)
    return value


def _risk_limit_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        if value in (None, ""):
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _risk_limit_int(
    settings: Any,
    name: str,
    default: int,
    *,
    min_value: int = 0,
) -> int:
    raw = getattr(settings, name, None)
    value = _risk_limit_number(raw)
    if value is None or value < min_value:
        return int(default)
    return int(value)


def compute_trade_risk_pct(
    entry_price: float,
    stop_price: float,
    quantity: float,
    capital: float,
    *,
    direction: str = "long",
) -> float:
    """Return risk as a percentage of capital for a single trade."""
    entry = _float_or_none(entry_price)
    stop = _float_or_none(stop_price)
    qty = _float_or_none(quantity)
    capital_f = _float_or_none(capital)
    if entry is None or stop is None or qty is None or capital_f is None:
        return 0.0
    side = str(direction or "long").strip().lower()
    if side == "short":
        risk_per_share = max(stop - entry, 0.0)
    else:
        risk_per_share = max(entry - stop, 0.0)
    total_risk = risk_per_share * qty
    return round(total_risk / capital_f * 100, 4)


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        if value in (None, ""):
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out > 0 else None


def _is_option_trade_safe(trade: Any) -> bool:
    try:
        from .autopilot_scope import is_option_trade

        return bool(is_option_trade(trade))
    except Exception:
        return False


def _canonical_asset_kind(value: Any) -> str | None:
    normalized = normalize_pattern_asset_class(value)
    if normalized == PATTERN_ASSET_CLASS_OPTIONS:
        return "option"
    if normalized == PATTERN_ASSET_CLASS_STOCKS:
        return "equity"
    if normalized == PATTERN_ASSET_CLASS_CRYPTO:
        return "crypto"
    raw = str(value or "").strip().lower()
    if raw in {"equity", "equities"}:
        return "equity"
    if raw in {"cryptocurrency", "coin"}:
        return "crypto"
    return None


def _ticker_asset_kind(ticker: Any) -> str:
    symbol = str(ticker or "").strip().upper()
    if symbol.endswith("-USD") or (
        len(symbol) > 3
        and symbol.endswith("USD")
        and "-" not in symbol
        and symbol[:-3].isalnum()
    ):
        return "crypto"
    return "equity"


def _trade_asset_kind(trade: Any) -> str:
    if _is_option_trade_safe(trade):
        return "option"
    explicit = _canonical_asset_kind(getattr(trade, "asset_kind", None))
    if explicit:
        return explicit
    return _ticker_asset_kind(getattr(trade, "ticker", None))


def _new_trade_asset_kind(ticker: str, asset_type: Any = None) -> str:
    explicit = _canonical_asset_kind(asset_type)
    if explicit:
        return explicit
    return _ticker_asset_kind(ticker)


def _trade_contract_multiplier(trade: Any) -> float:
    return 100.0 if _is_option_trade_safe(trade) else 1.0


def _option_heat_stop_fraction() -> float:
    try:
        from ...config import settings

        stop_pct = _float_or_none(
            getattr(settings, "chili_autotrader_options_exit_stop_pct", None)
        )
    except Exception:
        stop_pct = None
    if (
        stop_pct is None
        or stop_pct < OPTION_HEAT_STOP_PCT_MIN
        or stop_pct > OPTION_HEAT_STOP_PCT_MAX
    ):
        stop_pct = OPTION_HEAT_STOP_PCT_DEFAULT
    return stop_pct / 100.0


def _trade_entry_notional(trade: Any) -> float:
    entry = _float_or_none(getattr(trade, "entry_price", None))
    qty = _float_or_none(getattr(trade, "quantity", None))
    if entry is None or qty is None:
        return 0.0
    return entry * qty * _trade_contract_multiplier(trade)


def _option_premium_risk_dollars(trade: Any) -> float | None:
    """Dollar heat for option rows whose prices are stored as premiums."""
    if not _is_option_trade_safe(trade):
        return None
    entry = _float_or_none(getattr(trade, "entry_price", None))
    qty = _float_or_none(getattr(trade, "quantity", None))
    if entry is None or qty is None:
        return 0.0

    direction = str(getattr(trade, "direction", "") or "long").strip().lower()
    explicit_stop = _float_or_none(getattr(trade, "stop_loss", None))
    risk_per_contract: float | None = None
    if explicit_stop is not None:
        if direction == "short" and explicit_stop > entry:
            risk_per_contract = explicit_stop - entry
        elif direction != "short" and 0 < explicit_stop < entry:
            risk_per_contract = entry - explicit_stop

    if risk_per_contract is None:
        stop_fraction = _option_heat_stop_fraction()
        if direction == "short":
            risk_fraction = max(stop_fraction, 1.0)
        else:
            risk_fraction = min(max(stop_fraction, 0.0), 1.0) or 1.0
        risk_per_contract = entry * risk_fraction

    return risk_per_contract * qty * 100.0


def _trade_risk_dollars(trade: Any) -> float:
    option_risk = _option_premium_risk_dollars(trade)
    if option_risk is not None:
        return option_risk
    stop = _infer_stop(trade)
    entry = _float_or_none(getattr(trade, "entry_price", None))
    qty = _float_or_none(getattr(trade, "quantity", None))
    if stop and entry is not None and qty is not None:
        direction = str(getattr(trade, "direction", "") or "long").strip().lower()
        if direction == "short":
            return max(stop - entry, 0.0) * qty
        return max(entry - stop, 0.0) * qty
    return 0.0


def _open_trade_query(db: Session, user_id: int | None):
    return db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "open",
    )


def _broker_live_open_trades(
    db: Session,
    user_id: int | None,
    *,
    query: Any | None = None,
) -> list[Trade]:
    rows = (query or _open_trade_query(db, user_id)).all()
    try:
        from .broker_position_truth import filter_broker_stale_open_trades

        live, stale = filter_broker_stale_open_trades(db, rows)
        if stale:
            logger.info(
                "[risk] excluded %d stale broker-local open trade(s) from exposure",
                len(stale),
            )
        return live
    except Exception:
        logger.debug("[risk] broker-truth exposure filter failed", exc_info=True)
        return rows


def get_portfolio_risk_snapshot(
    db: Session,
    user_id: int | None,
    capital: float = 100_000.0,
    limits: RiskLimits | None = None,
) -> RiskBudget:
    """Calculate current portfolio risk exposure from open trades."""
    if limits is None:
        limits = get_risk_limits()

    open_trades = _broker_live_open_trades(db, user_id)

    budget = RiskBudget()
    budget.open_positions = len(open_trades)
    asset_counts = {"equity": 0, "crypto": 0, "option": 0}
    for t in open_trades:
        kind = _trade_asset_kind(t)
        asset_counts[kind] = asset_counts.get(kind, 0) + 1
    budget.crypto_positions = asset_counts.get("crypto", 0)
    budget.stock_positions = asset_counts.get("equity", 0)
    budget.option_positions = asset_counts.get("option", 0)

    capital_f = _float_or_none(capital)
    if capital_f is None:
        budget.can_open_new = False
        budget.rejection_reason = "invalid_capital"
        budget.available_heat_pct = 0.0
        return budget

    total_heat = 0.0
    for t in open_trades:
        risk = _float_or_none(_trade_risk_dollars(t)) or 0.0
        total_heat += risk / capital_f * 100
    budget.total_heat_pct = round(total_heat, 2)
    budget.available_heat_pct = round(max(0, limits.max_portfolio_heat_pct - total_heat), 2)

    if budget.open_positions >= limits.max_open_positions:
        budget.can_open_new = False
        budget.rejection_reason = f"Max positions ({limits.max_open_positions}) reached"
    elif budget.total_heat_pct >= limits.max_portfolio_heat_pct:
        budget.can_open_new = False
        budget.rejection_reason = f"Portfolio heat {budget.total_heat_pct:.1f}% >= cap {limits.max_portfolio_heat_pct}%"

    # Enrich with sector exposure (lightweight — no market data fetch)
    try:
        budget.sector_exposure = compute_sector_exposure(db, user_id)
    except Exception:
        pass

    # VaR is expensive (fetches OHLCV); compute only when positions exist
    if budget.open_positions > 0:
        try:
            budget.portfolio_var_pct = estimate_portfolio_var(db, user_id, capital_f)
        except Exception:
            pass
        try:
            budget.portfolio_cvar_pct = estimate_portfolio_cvar(db, user_id, capital_f)
        except Exception:
            pass

    return budget


def circuit_breaker_entry_block_reason(
    db: Session,
    *,
    user_id: int | None,
) -> str | None:
    """Return an entry-block reason when the durable breaker is active."""
    persisted_breaker = _refresh_breaker_from_db_for_gate(db, user_id=user_id)
    if persisted_breaker is not None:
        persisted_tripped, persisted_reason = persisted_breaker
        if persisted_tripped:
            return f"Circuit breaker active: {persisted_reason}"
        return None
    if is_breaker_tripped():
        return f"Circuit breaker active: {_breaker_reason}"
    return None


def check_live_portfolio_drawdown(
    db: Session,
    user_id: int | None = None,
    capital: float = 100_000.0,
    max_dd_pct: float = 15.0,
) -> dict[str, Any]:
    """Live broker portfolio drawdown — measured against the user's REAL broker
    positions and REAL equity, NOT the paper/shadow book.

    This is the gate used for LIVE auto-trader entries. The paper-shadow gate
    (``check_portfolio_drawdown`` in ``portfolio_optimizer``) queries
    ``PaperTrade`` positions; routing the live entry path through it meant a
    paper-shadow drawdown (paper PnL ÷ the caller's real equity) — or stranded
    unpriceable sim positions — wrongly blocked real trading. ``capital`` is the
    caller's resolved real equity (``auto_trader_rules`` passes
    ``resolve_effective_capital``); the 100k default is a non-authoritative
    fallback for callers that cannot resolve equity (the live path blocks
    upstream on unproven capital, so it never relies on the fallback).
    """
    from datetime import datetime, timedelta

    try:
        from .market_data import fetch_quote
    except Exception:
        fetch_quote = None  # type: ignore[assignment]

    open_trades = _broker_live_open_trades(db, user_id)
    total_unrealized = 0.0
    valuation_missing_count = 0
    for t in open_trades:
        try:
            entry = _float_or_none(getattr(t, "entry_price", None))
            qty = _float_or_none(getattr(t, "quantity", None))
            if entry is None or qty is None:
                valuation_missing_count += 1
                continue
            price: float | None = None
            if callable(fetch_quote):
                q = fetch_quote(t.ticker)
                if q and q.get("price"):
                    price = _float_or_none(q.get("price"))
            if price is None:
                valuation_missing_count += 1
                continue
            if (getattr(t, "direction", "") or "").lower() == "short":
                total_unrealized += (entry - price) * qty
            else:
                total_unrealized += (price - entry) * qty
        except Exception:
            valuation_missing_count += 1
            continue

    cutoff = datetime.utcnow() - timedelta(days=30)
    closed_q = db.query(Trade).filter(
        Trade.status == "closed",
        Trade.exit_date >= cutoff,
    )
    if user_id is not None:
        closed_q = closed_q.filter(Trade.user_id == user_id)
    closed_pnl = 0.0
    for t in closed_q.all():
        pnl = _finite_float_or_none(getattr(t, "pnl", None))
        if pnl is not None:
            closed_pnl += pnl

    total_pnl = total_unrealized + closed_pnl
    capital_f = _float_or_none(capital)
    if capital_f is None or capital_f <= 0:
        return {
            "ok": False,
            "scope": "live_broker",
            "reason": "invalid_capital",
            "unrealized_pnl": round(total_unrealized, 2),
            "closed_30d_pnl": round(closed_pnl, 2),
            "total_pnl": round(total_pnl, 2),
            "dd_pct": 0.0,
            "max_dd_pct": max_dd_pct,
            "breached": True,
            "valuation_missing_count": valuation_missing_count,
            "valuation_complete": valuation_missing_count == 0,
            "open_positions": len(open_trades),
        }

    dd_pct = total_pnl / capital_f * 100
    breached = dd_pct < -max_dd_pct
    reason = "drawdown_breached" if breached else None
    if valuation_missing_count > 0:
        breached = True
        reason = "valuation_unavailable"

    if breached:
        logger.warning(
            "[risk] Live portfolio DD blocked: reason=%s dd=%.1f%% "
            "(limit -%.1f%%) valuation_missing=%d open=%d",
            reason, dd_pct, max_dd_pct, valuation_missing_count, len(open_trades),
        )

    return {
        "ok": True,
        "scope": "live_broker",
        "reason": reason,
        "unrealized_pnl": round(total_unrealized, 2),
        "closed_30d_pnl": round(closed_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "dd_pct": round(dd_pct, 2),
        "max_dd_pct": max_dd_pct,
        "breached": breached,
        "valuation_missing_count": valuation_missing_count,
        "valuation_complete": valuation_missing_count == 0,
        "open_positions": len(open_trades),
    }


def check_new_trade_allowed(
    db: Session,
    user_id: int | None,
    ticker: str,
    capital: float = 100_000.0,
    limits: RiskLimits | None = None,
    *,
    asset_type: str | None = None,
) -> tuple[bool, str]:
    """Return (allowed, reason) for opening a new position in *ticker*."""
    try:
        from .governance import is_kill_switch_active
        if is_kill_switch_active():
            return False, "Kill switch is active — all trading halted"
    except Exception:
        logger.error("[risk] Kill-switch check failed — blocking trade as precaution", exc_info=True)
        return False, "Kill-switch check failed — trade blocked as safety precaution"

    breaker_reason = circuit_breaker_entry_block_reason(db, user_id=user_id)
    if breaker_reason is not None:
        return False, breaker_reason

    capital_f = _float_or_none(capital)
    if capital_f is None:
        return False, "invalid_capital"

    tripped, reason = check_drawdown_breaker(db, user_id, capital_f)
    if tripped:
        return False, f"Circuit breaker triggered: {reason}"

    if limits is None:
        limits = get_risk_limits()

    budget = get_portfolio_risk_snapshot(db, user_id, capital_f, limits)
    if not budget.can_open_new:
        return False, budget.rejection_reason or "Risk limit exceeded"

    asset_kind = _new_trade_asset_kind(ticker, asset_type)
    if asset_kind == "crypto" and budget.crypto_positions >= limits.max_crypto_positions:
        return False, f"Crypto cap ({limits.max_crypto_positions}) reached"
    if asset_kind == "equity" and budget.stock_positions >= limits.max_stock_positions:
        return False, f"Stock cap ({limits.max_stock_positions}) reached"

    same_ticker_count = sum(
        1
        for row in _broker_live_open_trades(db, user_id)
        if str(row.ticker or "").upper() == ticker.upper()
    )
    if same_ticker_count >= limits.max_same_ticker:
        return False, f"Already {same_ticker_count} open positions in {ticker}"

    # Sector concentration check (scoped to user to prevent cross-user leakage)
    try:
        sector_q = db.query(Trade.sector).filter(
            Trade.ticker == ticker.upper(),
        )
        if user_id is not None:
            sector_q = sector_q.filter(Trade.user_id == user_id)
        new_trade_sector = sector_q.order_by(Trade.id.desc()).first()
        sector_label = new_trade_sector[0] if new_trade_sector else None
        allowed, reason = check_sector_concentration(db, user_id, sector_label, limits)
        if not allowed:
            return False, reason
    except Exception:
        logger.warning(
            "[risk] sector concentration check failed; blocking as precaution",
            exc_info=True,
        )
        return False, "sector_check_unavailable"

    # Correlation risk check
    try:
        allowed, reason = check_correlation_risk(db, user_id, ticker, limits)
        if not allowed:
            return False, reason
    except Exception:
        logger.warning(
            "[risk] correlation check failed; blocking as precaution",
            exc_info=True,
        )
        return False, "correlation_check_unavailable"

    # Live broker drawdown — measured against REAL positions/equity, not the
    # paper-shadow book (the paper gate wrongly blocked live entries on paper PnL).
    try:
        dd = check_live_portfolio_drawdown(db, user_id, capital_f)
        if dd.get("reason") in {"valuation_unavailable", "invalid_capital"}:
            return False, "portfolio_drawdown_unavailable"
        if dd.get("breached"):
            return False, f"Portfolio drawdown {dd.get('dd_pct', 0.0):.1f}% breached limit"
    except Exception as exc:
        logger.warning("[risk] Portfolio drawdown unavailable; blocking as precaution: %s", exc)
        return False, "portfolio_drawdown_unavailable"

    return True, "ok"


def size_position(
    capital: float,
    entry_price: float,
    stop_price: float,
    risk_pct: float | None = None,
    limits: RiskLimits | None = None,
    direction: str = "long",
) -> int:
    """Calculate position size (shares) for a given directional risk budget.

    Uses fixed-fractional sizing: risk_pct% of capital = max loss.
    """
    if limits is None:
        limits = get_risk_limits()
    if risk_pct is None:
        risk_pct = limits.max_risk_per_trade_pct

    try:
        capital_f = float(capital)
        entry_f = float(entry_price)
        stop_f = float(stop_price)
        risk_pct_f = float(risk_pct)
    except (TypeError, ValueError):
        return 0
    if (
        not math.isfinite(capital_f)
        or not math.isfinite(entry_f)
        or not math.isfinite(stop_f)
        or not math.isfinite(risk_pct_f)
        or capital_f <= 0
        or entry_f <= 0
        or stop_f <= 0
        or risk_pct_f <= 0
    ):
        return 0

    risk_amount = capital_f * (risk_pct_f / 100)
    side = str(direction or "long").strip().lower()
    risk_per_share = stop_f - entry_f if side == "short" else entry_f - stop_f
    if risk_per_share <= 0:
        return 0

    shares = int(risk_amount / risk_per_share)
    max_notional = capital_f * 0.20  # never more than 20% of capital in one position
    max_by_notional = int(max_notional / entry_f)
    return max(0, min(shares, max_by_notional))


def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """Calculate the Kelly Criterion fraction for optimal bet sizing.

    Returns a fraction of capital to risk (0 to 1). Capped at 25% (quarter-Kelly
    is standard practice to reduce variance).

    f* = (p * b - q) / b
    where p = win probability, q = loss probability, b = avg_win / avg_loss
    """
    if win_rate <= 0 or win_rate >= 1 or avg_win <= 0 or avg_loss <= 0:
        return 0.0

    p = win_rate
    q = 1 - p
    b = avg_win / avg_loss

    kelly = (p * b - q) / b
    kelly = max(0.0, kelly)

    # Quarter-Kelly for safety
    return min(0.25, kelly * 0.25)


def size_position_kelly(
    capital: float,
    entry_price: float,
    stop_price: float,
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
    *,
    max_kelly_pct: float = 5.0,
    limits: RiskLimits | None = None,
    # Phase H shadow-emit context (all optional). Passing ``db`` + ``ticker``
    # enables a Phase H canonical sizer log row for divergence analysis.
    db: "Session | None" = None,
    user_id: int | None = None,
    ticker: str | None = None,
    pattern_id: int | None = None,
    target_price: float | None = None,
    regime: str | None = None,
    asset_class: str | None = None,
) -> dict[str, Any]:
    """Kelly-optimized position sizing with drawdown scaling.

    Returns dict with shares, kelly_fraction, risk_pct, and sizing rationale.
    """
    if limits is None:
        limits = get_risk_limits()

    kf = kelly_fraction(win_rate, avg_win_pct, avg_loss_pct)
    risk_pct = min(kf * 100, max_kelly_pct, limits.max_risk_per_trade_pct)

    shares = size_position(capital, entry_price, stop_price, risk_pct=risk_pct, limits=limits)

    # Phase H shadow hook. Defensive: never breaks Kelly sizing.
    if db is not None and ticker:
        try:
            from .position_sizer_emitter import EmitterSignal, emit_shadow_proposal
            from .position_sizer_writer import LegacySizing, mode_is_active

            if mode_is_active():
                _legacy_notional = (
                    float(shares) * float(entry_price)
                    if shares > 0 and entry_price > 0
                    else None
                )
                # Infer a rough calibrated-prob from the legacy win_rate.
                emit_shadow_proposal(
                    db,
                    signal=EmitterSignal(
                        source="portfolio_risk.size_position_kelly",
                        ticker=ticker,
                        direction="long",
                        entry_price=float(entry_price),
                        stop_price=float(stop_price),
                        capital=float(capital),
                        target_price=float(target_price) if target_price else None,
                        asset_class=asset_class,
                        user_id=user_id,
                        pattern_id=pattern_id,
                        regime=regime,
                        confidence=float(win_rate) if win_rate is not None else None,
                    ),
                    legacy=LegacySizing(
                        notional=_legacy_notional,
                        quantity=float(shares) if shares else None,
                        source="portfolio_risk.size_position_kelly",
                    ),
                )
        except Exception:  # pragma: no cover - defensive
            logger.debug("[portfolio_risk.kelly] phase H shadow emit failed", exc_info=True)

    return {
        "shares": shares,
        "kelly_fraction": round(kf, 4),
        "risk_pct": round(risk_pct, 3),
        "notional": round(shares * entry_price, 2) if shares > 0 else 0,
        "risk_amount": round(capital * risk_pct / 100, 2),
        "method": "kelly_quarter",
    }


def size_with_drawdown_scaling(
    db: Session,
    user_id: int | None,
    capital: float,
    entry_price: float,
    stop_price: float,
    win_rate: float = 0.55,
    avg_win_pct: float = 2.0,
    avg_loss_pct: float = 1.0,
    limits: RiskLimits | None = None,
    # Phase H shadow-emit context (optional). Supply ``ticker`` to turn on.
    ticker: str | None = None,
    pattern_id: int | None = None,
    target_price: float | None = None,
    regime: str | None = None,
    asset_class: str | None = None,
) -> dict[str, Any]:
    """Kelly sizing scaled down by current drawdown severity.

    If the account is in drawdown, reduce position size proportionally.
    """
    from datetime import datetime, timedelta

    if limits is None:
        limits = get_risk_limits()

    # Note: we deliberately DO NOT pass the Phase H shadow-emit kwargs into
    # ``size_position_kelly`` here; the DD-scaled variant owns the final
    # ``scaled_shares``, so we emit once after scaling rather than twice.
    base = size_position_kelly(
        capital, entry_price, stop_price,
        win_rate, avg_win_pct, avg_loss_pct,
        limits=limits,
    )

    # Calculate drawdown scaling factor
    now = datetime.utcnow()
    thirty_days_ago = now - timedelta(days=30)
    recent = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        Trade.exit_date >= thirty_days_ago,
    ).all()

    pnl_30d = _sum_trade_realized_pnl(recent)
    dd_pct = abs(pnl_30d / capital * 100) if capital > 0 and pnl_30d < 0 else 0

    # Scale factor: 1.0 at no DD, 0.5 at 4% DD, 0.25 at 8% DD
    if dd_pct <= 0:
        scale = 1.0
    elif dd_pct >= 8:
        scale = 0.25
    else:
        scale = max(0.25, 1.0 - (dd_pct / 8.0) * 0.75)

    scaled_shares = max(0, int(base["shares"] * scale))
    base["dd_scale_factor"] = round(scale, 3)
    base["drawdown_30d_pct"] = round(dd_pct, 2)
    base["shares_before_dd_scale"] = base["shares"]
    base["shares"] = scaled_shares
    base["notional"] = round(scaled_shares * entry_price, 2)
    base["method"] = "kelly_quarter_dd_scaled"

    # Phase H shadow hook.
    if ticker:
        try:
            from .position_sizer_emitter import EmitterSignal, emit_shadow_proposal
            from .position_sizer_writer import LegacySizing, mode_is_active

            if mode_is_active():
                _legacy_notional = (
                    float(scaled_shares) * float(entry_price)
                    if scaled_shares > 0 and entry_price > 0
                    else None
                )
                emit_shadow_proposal(
                    db,
                    signal=EmitterSignal(
                        source="portfolio_risk.size_with_drawdown_scaling",
                        ticker=ticker,
                        direction="long",
                        entry_price=float(entry_price),
                        stop_price=float(stop_price),
                        capital=float(capital),
                        target_price=float(target_price) if target_price else None,
                        asset_class=asset_class,
                        user_id=user_id,
                        pattern_id=pattern_id,
                        regime=regime,
                        confidence=float(win_rate) if win_rate is not None else None,
                    ),
                    legacy=LegacySizing(
                        notional=_legacy_notional,
                        quantity=float(scaled_shares) if scaled_shares else None,
                        source="portfolio_risk.size_with_drawdown_scaling",
                    ),
                )
        except Exception:  # pragma: no cover - defensive
            logger.debug(
                "[portfolio_risk.dd] phase H shadow emit failed", exc_info=True
            )

    return base


# ── Portfolio-level correlation & sector risk ────────────────────────

def compute_sector_exposure(
    db: Session,
    user_id: int | None,
) -> dict[str, float]:
    """Return % of open positions in each sector.

    Uses the ``Trade.sector`` column.  Tickers with no sector assigned
    are grouped under "unknown".
    """
    open_trades = _broker_live_open_trades(db, user_id)

    if not open_trades:
        return {}

    counts: dict[str, int] = {}
    for t in open_trades:
        sector = (t.sector or "unknown").strip().lower()
        counts[sector] = counts.get(sector, 0) + 1

    total = len(open_trades)
    return {s: round(c / total * 100, 1) for s, c in counts.items()}


def compute_pairwise_correlation(
    tickers: list[str],
    lookback: int = 60,
) -> dict[str, dict[str, float]]:
    """Compute Pearson correlation matrix of daily returns for *tickers*.

    Returns nested dict: ``{ticker_a: {ticker_b: corr, ...}, ...}``.
    Tickers with insufficient data are silently excluded.
    """
    import numpy as np

    from .market_data import fetch_ohlcv_df

    if len(tickers) < 2:
        return {}

    returns_by_ticker: dict[str, list[float]] = {}
    for ticker in tickers:
        try:
            df = fetch_ohlcv_df(ticker, interval="1d", period=f"{lookback + 10}d")
            if df is None or len(df) < 20:
                continue
            close = df["Close"].values
            rets = list((close[1:] - close[:-1]) / close[:-1])
            returns_by_ticker[ticker] = rets[-lookback:]
        except Exception:
            continue

    valid = list(returns_by_ticker.keys())
    if len(valid) < 2:
        return {}

    # Align lengths to the shortest series
    min_len = min(len(returns_by_ticker[t]) for t in valid)
    matrix: dict[str, dict[str, float]] = {}
    for a in valid:
        matrix[a] = {}
        ra = np.array(returns_by_ticker[a][-min_len:])
        for b in valid:
            if a == b:
                matrix[a][b] = 1.0
                continue
            rb = np.array(returns_by_ticker[b][-min_len:])
            if np.std(ra) == 0 or np.std(rb) == 0:
                matrix[a][b] = 0.0
            else:
                matrix[a][b] = round(float(np.corrcoef(ra, rb)[0, 1]), 4)

    return matrix


def check_correlation_risk(
    db: Session,
    user_id: int | None,
    new_ticker: str,
    limits: RiskLimits | None = None,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` after checking correlation with open positions.

    If the average absolute correlation between *new_ticker* and all
    currently open tickers exceeds ``limits.max_avg_correlation``, the
    trade is rejected.
    """
    if limits is None:
        limits = get_risk_limits()

    open_trades = _broker_live_open_trades(db, user_id)

    existing_tickers = list({t.ticker for t in open_trades})
    if len(existing_tickers) < 2:
        return True, "ok"

    try:
        all_tickers = existing_tickers + [new_ticker.upper()]
        corr_matrix = compute_pairwise_correlation(all_tickers)
        if not corr_matrix or new_ticker.upper() not in corr_matrix:
            return True, "ok"  # insufficient data, allow

        new_corrs = corr_matrix[new_ticker.upper()]
        peer_corrs = [
            abs(new_corrs.get(t, 0.0))
            for t in existing_tickers
            if t in new_corrs and t != new_ticker.upper()
        ]
        if not peer_corrs:
            return True, "ok"

        avg_corr = sum(peer_corrs) / len(peer_corrs)
        if avg_corr > limits.max_avg_correlation:
            return False, (
                f"Avg correlation {avg_corr:.2f} with open positions "
                f"exceeds limit {limits.max_avg_correlation}"
            )
    except Exception:
        logger.warning(
            "[risk] correlation check failed; blocking as precaution",
            exc_info=True,
        )
        return False, "correlation_check_unavailable"

    return True, "ok"


def check_sector_concentration(
    db: Session,
    user_id: int | None,
    new_ticker_sector: str | None,
    limits: RiskLimits | None = None,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` after checking sector concentration.

    Projects what sector exposure would look like after adding one more
    position in *new_ticker_sector* and rejects if any sector would
    exceed ``limits.max_sector_pct``.
    """
    if limits is None:
        limits = get_risk_limits()

    sector = (new_ticker_sector or "unknown").strip().lower()

    open_trades = _broker_live_open_trades(db, user_id)

    if not open_trades:
        return True, "ok"

    # Skip the sector cap entirely when the proposed trade has no known
    # sector. Treating "unknown" as a real sector bucket causes false
    # rejections whenever existing positions (e.g. broker-sync imported
    # rows without sector enrichment) dominate the "unknown" bucket. The
    # concentration gate exists to limit exposure to a REAL named sector;
    # "unknown" is a data-quality artifact, not a risk dimension.
    if sector == "unknown":
        return True, "ok"

    total_after = len(open_trades) + 1
    same_sector = sum(
        1 for t in open_trades
        if (t.sector or "unknown").strip().lower() == sector
    ) + 1  # +1 for the proposed trade

    projected_pct = same_sector / total_after * 100
    if projected_pct > limits.max_sector_pct:
        return False, (
            f"Sector '{sector}' would reach {projected_pct:.0f}% "
            f"({same_sector}/{total_after}), exceeds {limits.max_sector_pct}% cap"
        )

    return True, "ok"


def estimate_portfolio_var(
    db: Session,
    user_id: int | None,
    capital: float = 100_000.0,
    confidence: float = 0.95,
    lookback: int = 60,
) -> float | None:
    """Estimate parametric Value-at-Risk (%) for current open positions.

    Uses a covariance-based approach on daily returns, assuming normal
    distribution.  Returns the estimated 1-day loss as a percentage of
    capital, or None if insufficient data.
    """
    import numpy as np
    from scipy.stats import norm

    open_trades = _broker_live_open_trades(db, user_id)

    if not open_trades or capital <= 0:
        return None

    # Collect per-position weights (notional / capital) and returns
    from .market_data import fetch_ohlcv_df

    tickers = list({t.ticker for t in open_trades})
    weights: dict[str, float] = {}
    for t in open_trades:
        weights[t.ticker] = weights.get(t.ticker, 0.0) + _trade_entry_notional(t) / capital

    returns_data: dict[str, list[float]] = {}
    for ticker in tickers:
        try:
            df = fetch_ohlcv_df(ticker, interval="1d", period=f"{lookback + 10}d")
            if df is None or len(df) < 20:
                continue
            close = df["Close"].values
            rets = list((close[1:] - close[:-1]) / close[:-1])
            returns_data[ticker] = rets[-lookback:]
        except Exception:
            continue

    valid = [t for t in tickers if t in returns_data]
    if len(valid) < 1:
        return None

    min_len = min(len(returns_data[t]) for t in valid)
    ret_matrix = np.array([returns_data[t][-min_len:] for t in valid])
    w = np.array([weights.get(t, 0.0) for t in valid])

    cov = np.cov(ret_matrix)
    if cov.ndim == 0:
        cov = np.array([[float(cov)]])

    portfolio_var = float(np.sqrt(w @ cov @ w))
    z = norm.ppf(confidence)
    var_pct = round(portfolio_var * z * 100, 4)

    return var_pct


def estimate_portfolio_cvar(
    db: Session,
    user_id: int | None,
    capital: float = 100_000.0,
    confidence: float = 0.95,
    lookback: int = 60,
) -> float | None:
    """Estimate historical portfolio CVaR (%) from open-position return series."""
    import numpy as np
    from .market_data import fetch_ohlcv_df

    open_trades = _broker_live_open_trades(db, user_id)
    if not open_trades or capital <= 0:
        return None

    tickers = list({t.ticker for t in open_trades})
    weights: dict[str, float] = {}
    for t in open_trades:
        weights[t.ticker] = weights.get(t.ticker, 0.0) + _trade_entry_notional(t) / capital

    returns_data: dict[str, list[float]] = {}
    for ticker in tickers:
        try:
            df = fetch_ohlcv_df(ticker, interval="1d", period=f"{lookback + 10}d")
            if df is None or len(df) < 20:
                continue
            close = df["Close"].values
            rets = list((close[1:] - close[:-1]) / close[:-1])
            returns_data[ticker] = rets[-lookback:]
        except Exception:
            continue

    valid = [t for t in tickers if t in returns_data]
    if not valid:
        return None
    min_len = min(len(returns_data[t]) for t in valid)
    if min_len < 5:
        return None
    mat = np.array([returns_data[t][-min_len:] for t in valid])
    w = np.array([weights.get(t, 0.0) for t in valid])
    portfolio_returns = np.dot(mat.T, w)
    losses = -portfolio_returns
    var_cut = np.quantile(losses, confidence)
    tail = losses[losses >= var_cut]
    if len(tail) == 0:
        return None
    return round(float(np.mean(tail) * 100.0), 4)


def _infer_stop(trade: Trade) -> float | None:
    """Infer stop-loss price from trade tags/notes or use a default ATR-based estimate."""
    import json
    explicit_stop = _float_or_none(getattr(trade, "stop_loss", None))
    if explicit_stop is not None:
        return explicit_stop
    if trade.tags:
        try:
            tag_data = json.loads(trade.tags) if trade.tags.startswith("{") else {}
            sl = tag_data.get("stop_loss") or tag_data.get("stop")
            sl_f = _float_or_none(sl)
            if sl_f is not None:
                return sl_f
        except Exception:
            pass
    if trade.indicator_snapshot:
        try:
            snap = (
                json.loads(trade.indicator_snapshot)
                if isinstance(trade.indicator_snapshot, str)
                else trade.indicator_snapshot
            )
            if not isinstance(snap, dict):
                snap = {}
            atr = snap.get("atr", {}).get("value")
            atr_f = _float_or_none(atr)
            entry = _float_or_none(getattr(trade, "entry_price", None))
            if atr_f is not None and entry is not None:
                inferred = entry - (2.0 * atr_f)
                return inferred if inferred > 0 else None
        except Exception:
            pass
    # Default: assume 2% stop from entry
    entry = _float_or_none(getattr(trade, "entry_price", None))
    return entry * 0.98 if entry is not None else None


# ── Drawdown Circuit Breaker ──────────────────────────────────────────

_breaker_tripped = False
_breaker_reason: str | None = None


@dataclass
class DrawdownLimits:
    max_5day_dd_pct: float = 3.0   # pause if 5-day P&L drops below -3%
    max_30day_dd_pct: float = 8.0  # pause if 30-day P&L drops below -8%
    max_consecutive_losses: int = 5
    cooldown_hours: int = 24       # how long the breaker stays tripped


# Phase 2: regime multipliers applied to the base drawdown thresholds.
# The baseline (env / default) matches the prior behavior in ``cautious``.
# ``risk_on`` tolerates more normal-volatility drawdown before tripping;
# ``risk_off`` trips earlier because early-stage losses in choppy regimes
# are stronger danger signals.
_REGIME_DD_MULTIPLIERS: dict[str, float] = {
    "risk_on": 1.5,
    "cautious": 1.0,
    "risk_off": 0.75,
}


def _read_current_regime(db: Session) -> str | None:
    try:
        from .runtime_surface_state import read_runtime_surface_state

        surface = read_runtime_surface_state(db, surface="regime")
        if not surface:
            return None
        r = str(surface.get("regime") or "").strip().lower()
        return r if r in _REGIME_DD_MULTIPLIERS else None
    except Exception:
        return None


def get_drawdown_limits(
    settings: Any | None = None,
    *,
    db: Session | None = None,
) -> DrawdownLimits:
    """Resolve effective drawdown limits, regime-scaled when a DB is supplied.

    Backwards-compatible: callers who don't pass ``db`` get the static
    settings-driven limits (same as before). The circuit-breaker path now
    always supplies ``db`` so thresholds track the brain's regime surface
    instead of being a flat env constant.
    """
    if settings is None:
        from ...config import settings as _s
        settings = _s
    base_5d = float(getattr(settings, "brain_risk_max_5d_dd_pct", 3.0))
    base_30d = float(getattr(settings, "brain_risk_max_30d_dd_pct", 8.0))
    mult = 1.0
    if db is not None:
        regime = _read_current_regime(db)
        if regime is not None:
            mult = _REGIME_DD_MULTIPLIERS.get(regime, 1.0)
    return DrawdownLimits(
        max_5day_dd_pct=base_5d * mult,
        max_30day_dd_pct=base_30d * mult,
        max_consecutive_losses=int(getattr(settings, "brain_risk_max_consec_losses", 5)),
        cooldown_hours=int(getattr(settings, "brain_risk_cooldown_hours", 24)),
    )


# YY — drawdown breaker scope. The breaker should be measuring CHILI's
# OWN P&L, not the user's pre-existing manual positions. The user opened
# this scope distinction after the breaker tripped at -9.8% on a pool of
# realized losses that included manual pre-CHILI trades — unfair to
# CHILI, blocked all autotrader entries, and was a false positive.
#
# When ``chili_breaker_scope_autotrader_only`` is True (default), every
# query in ``check_drawdown_breaker`` filters to rows that CHILI placed
# (``auto_trader_version IS NOT NULL`` OR ``management_scope`` matches
# the autotrader scope). Manual positions are invisible to the breaker.
#
# Set to False to preserve the legacy "everything counts" behavior.

def _breaker_trade_filter(query):
    """Apply the autotrader-only filter to a Trade query when the flag is on.

    Idempotent — when the flag is off, returns the query untouched. The
    canonical "this row was placed by the autotrader" signal is
    ``auto_trader_version IS NOT NULL``; ``management_scope`` is the
    secondary signal carried on a few legacy rows. We OR them so the
    filter matches both populations.
    """
    try:
        from ...config import settings
        scope_only = bool(getattr(settings, "chili_breaker_scope_autotrader_only", True))
    except Exception:
        scope_only = True
    if not scope_only:
        return query
    from sqlalchemy import or_
    from .management_scope import MANAGEMENT_SCOPE_AUTO_TRADER_V1
    return query.filter(
        or_(
            Trade.auto_trader_version.isnot(None),
            Trade.management_scope == MANAGEMENT_SCOPE_AUTO_TRADER_V1,
        )
    )


def _compute_unrealized_pnl(db: Session, user_id: int | None) -> float:
    """Compute mark-to-market unrealized P&L across open trades.

    YY — only counts CHILI-placed trades when
    ``chili_breaker_scope_autotrader_only`` is True (default). Manual
    positions are excluded because the breaker should measure CHILI's
    risk, not the user's overall portfolio.
    """
    q = _open_trade_query(db, user_id)
    q = _breaker_trade_filter(q)
    open_trades = _broker_live_open_trades(db, user_id, query=q)
    if not open_trades:
        return 0.0

    total_unrealized = 0.0
    try:
        from .market_data import fetch_quote
    except ImportError:
        return 0.0

    for t in open_trades:
        try:
            try:
                from .autopilot_scope import is_option_trade
            except Exception:
                is_option_trade = None
            if callable(is_option_trade) and is_option_trade(t):
                current_premium = _float_or_none(_option_unrealized_mark_price(t))
                if current_premium is None:
                    continue
                qty = _float_or_none(getattr(t, "quantity", None))
                entry = _float_or_none(getattr(t, "entry_price", None))
                if qty is None or entry is None:
                    continue
                if (getattr(t, "direction", "") or "").lower() == "short":
                    total_unrealized += (entry - current_premium) * qty * 100.0
                else:
                    total_unrealized += (current_premium - entry) * qty * 100.0
                continue
            q = fetch_quote(t.ticker)
            if q and q.get("price"):
                current_price = _float_or_none(q["price"])
                qty = _float_or_none(getattr(t, "quantity", None))
                entry = _float_or_none(getattr(t, "entry_price", None))
                if current_price is not None and qty is not None and entry is not None:
                    total_unrealized += (current_price - entry) * qty
        except Exception:
            continue

    return total_unrealized


def _option_unrealized_mark_price(trade: Trade) -> float | None:
    """Return the current option premium mark for MTM, never underlying spot."""
    try:
        from .options.exit_monitor import (
            _opt_meta,
            _option_exit_quote_price,
            _option_quote_has_malformed_price,
            _option_quote_is_crossed,
        )
        from .options.contracts import normalize_expiration, normalize_option_type
        from .venue.robinhood_options import RobinhoodOptionsAdapter

        meta = _opt_meta(trade)
        expiration = normalize_expiration(meta.get("expiration"))
        strike = _float_or_none(meta.get("strike"))
        option_type = normalize_option_type(meta.get("option_type"))
        if not (expiration and strike is not None and option_type in ("call", "put")):
            return None
        underlying = str(meta.get("underlying") or trade.ticker or "").strip().upper()
        if not underlying:
            return None
        adapter = RobinhoodOptionsAdapter()
        if not adapter.is_enabled():
            return None
        contract = adapter.find_contract(underlying, expiration, strike, option_type)
        if not contract:
            return None
        contract_id = str(contract.get("id") or "").strip()
        if not contract_id:
            return None
        quote = adapter.get_quote(contract_id)
        if not quote:
            return None
        if _option_quote_has_malformed_price(quote) or _option_quote_is_crossed(quote):
            return None
        for key in ("mark_price", "adjusted_mark_price", "last_trade_price"):
            value, malformed = _option_exit_quote_price(quote, key)
            if malformed:
                return None
            if value is not None and value > 0:
                return value
        bid, bid_malformed = _option_exit_quote_price(quote, "bid_price", "bid")
        ask, ask_malformed = _option_exit_quote_price(quote, "ask_price", "ask")
        if bid_malformed or ask_malformed:
            return None
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return (bid + ask) / 2.0
    except Exception:
        logger.debug("[portfolio_risk] option MTM mark failed", exc_info=True)
    return None


def _monthly_dd_threshold(
    db: Session,
    user_id: int | None,
    settings_obj: Any | None = None,
) -> tuple[float | None, int]:
    """Empirical Gaussian lower-bound on 30-day realized PnL.

    Computed from CHILI-attributed live history (``scan_pattern_id IS NOT
    NULL AND scan_pattern_id != -1``). Returns ``(threshold_usd,
    n_days_observed)``. When ``n_days_observed < 30`` the threshold is
    ``None`` and the breaker MUST skip the check (no fallback dollar
    value -- see f-phase3-stop-bleed D1 + COWORK_ADVISOR_BRIEF §2.6).

    The K-sigma multiplier is loaded from settings, not hardcoded.

    Formula::

        threshold = 30 * mean(daily_pnl) - K * sqrt(30) * std(daily_pnl)

    where ``daily_pnl`` is the per-day SUM of realized PnL on CHILI-
    attributed closed trades over the trailing 180 days, treated as iid
    Gaussian for the monthly aggregate.
    """
    from sqlalchemy import text

    rows = db.execute(
        text(
            """
            SELECT DATE_TRUNC('day',
                       COALESCE(exit_date, last_fill_at, filled_at))::date AS d,
                   COALESCE(SUM(pnl), 0)::float AS daily_pnl
              FROM trading_trades
             WHERE user_id = :uid
               AND status = 'closed'
               AND pnl IS NOT NULL
               AND scan_pattern_id IS NOT NULL
               AND scan_pattern_id != -1
               AND COALESCE(exit_date, last_fill_at, filled_at)
                   >= now() - interval '180 days'
             GROUP BY 1
            """
        ),
        {"uid": user_id},
    ).fetchall()

    n = len(rows)
    if n < 30:
        return None, n

    daily = [float(r.daily_pnl or 0.0) for r in rows]
    mean_d = sum(daily) / n
    var_d = sum((p - mean_d) ** 2 for p in daily) / max(n - 1, 1)
    std_d = var_d ** 0.5

    if settings_obj is None:
        from ...config import settings as _s
        settings_obj = _s
    k = float(getattr(settings_obj, "chili_pattern_dd_breaker_lower_bound_sigmas", 2.0))

    threshold = (30.0 * mean_d) - k * ((30.0 ** 0.5) * std_d)
    return threshold, n


def _monthly_attributed_pnl(
    db: Session,
    user_id: int | None,
) -> float:
    """Sum of realized PnL on CHILI-attributed closed trades over the
    trailing 30 days.

    Matches :func:`_monthly_dd_threshold`'s attribution scope
    (``scan_pattern_id IS NOT NULL AND scan_pattern_id != -1``) so the
    monthly DD breaker's numerator and denominator are calibrated against
    the same population. Without this filter, no_pattern bleed (manual /
    legacy / reconciler-imported trades with NULL or -1 scan_pattern_id)
    inflates the numerator without widening the threshold's variance
    estimate -- tripping the breaker on losses the threshold cannot
    statistically see.

    Background: ARCHITECT-FLAG raised by the 2026-05-16 monthly-DD-breaker
    arming-watch report (docs/STRATEGY/CC_REPORTS/
    2026-05-16_phase3-monthly-dd-breaker-arming-watch.md). Threshold was
    attributed-only, numerator was all-closed -- a definitional asymmetry
    one SQL clause wide. This helper closes it.
    """
    from sqlalchemy import text

    result = db.execute(
        text(
            """
            SELECT COALESCE(SUM(pnl), 0)::float
              FROM trading_trades
             WHERE user_id = :uid
               AND status = 'closed'
               AND pnl IS NOT NULL
               AND scan_pattern_id IS NOT NULL
               AND scan_pattern_id != -1
               AND COALESCE(exit_date, last_fill_at, filled_at)
                   >= now() - interval '30 days'
            """
        ),
        {"uid": user_id},
    ).scalar()
    return float(result or 0.0)


def _portfolio_dd_threshold(
    db: Session,
    user_id: int | None,
    settings_obj: Any | None = None,
) -> tuple[float | None, int]:
    """Empirical Gaussian lower-bound on 30-day realized PnL, ALL-CLOSED scope.

    Parallel to :func:`_monthly_dd_threshold` except the WHERE clause has
    NO ``scan_pattern_id`` predicates -- this tier's distribution is
    every closed trade (attributed, no_pattern, manual,
    reconcile-inferred). Returns ``(threshold_usd, n_days_observed)``.
    When ``n_days_observed < 30`` the threshold is ``None`` and the
    portfolio breaker MUST skip the check, matching the pattern tier's
    n<30 behavior.

    The K-sigma multiplier is loaded from
    ``chili_portfolio_dd_breaker_lower_bound_sigmas`` (default 2.0) so
    the two tiers tune independently. Tier separation is per
    f-portfolio-vs-pattern-breaker-separation -- the pattern tier's lever
    halts CHILI-attributed entries, the portfolio tier's lever halts
    EVERY entry path; each tier's distribution must be drawn from the
    population its lever can act on for the K*sigma math to remain
    coherent.

    When ``user_id`` is None (the venue-adapter gate's default) the
    query drops the user filter and aggregates across the entire
    household account. CHILI is a single-account-per-broker app, so
    "portfolio" semantically means the broker account regardless of
    which household user initiated the trade.
    """
    from sqlalchemy import text

    rows = db.execute(
        text(
            """
            SELECT DATE_TRUNC('day',
                       COALESCE(exit_date, last_fill_at, filled_at))::date AS d,
                   COALESCE(SUM(pnl), 0)::float AS daily_pnl
              FROM trading_trades
             WHERE (:uid IS NULL OR user_id = :uid)
               AND status = 'closed'
               AND pnl IS NOT NULL
               AND COALESCE(exit_date, last_fill_at, filled_at)
                   >= now() - interval '180 days'
             GROUP BY 1
            """
        ),
        {"uid": user_id},
    ).fetchall()

    n = len(rows)
    if n < 30:
        return None, n

    daily = [float(r.daily_pnl or 0.0) for r in rows]
    mean_d = sum(daily) / n
    var_d = sum((p - mean_d) ** 2 for p in daily) / max(n - 1, 1)
    std_d = var_d ** 0.5

    if settings_obj is None:
        from ...config import settings as _s
        settings_obj = _s
    k = float(getattr(
        settings_obj, "chili_portfolio_dd_breaker_lower_bound_sigmas", 2.0,
    ))

    threshold = (30.0 * mean_d) - k * ((30.0 ** 0.5) * std_d)
    return threshold, n


def _monthly_total_pnl(
    db: Session,
    user_id: int | None,
) -> float:
    """Sum of realized PnL on ALL closed trades over the trailing 30 days.

    Parallel to :func:`_monthly_attributed_pnl` but without the
    ``scan_pattern_id`` filter. Numerator for the portfolio tier; sampled
    from the same population as :func:`_portfolio_dd_threshold` so the
    numerator/denominator attribution-symmetry that
    f-monthly-dd-breaker-numerator-symmetrize enforced for the pattern
    tier is preserved here for the portfolio tier.

    When ``user_id`` is None (the venue-adapter gate's default) the
    query drops the user filter and aggregates across the entire
    household account. See :func:`_portfolio_dd_threshold` for the
    rationale.
    """
    from sqlalchemy import text

    result = db.execute(
        text(
            """
            SELECT COALESCE(SUM(pnl), 0)::float
              FROM trading_trades
             WHERE (:uid IS NULL OR user_id = :uid)
               AND status = 'closed'
               AND pnl IS NOT NULL
               AND COALESCE(exit_date, last_fill_at, filled_at)
                   >= now() - interval '30 days'
            """
        ),
        {"uid": user_id},
    ).scalar()
    return float(result or 0.0)


def _persist_portfolio_breaker_state(
    tripped: bool,
    reason: str | None,
    regime: str,
) -> None:
    """Write portfolio-tier breaker state to ``trading_risk_state``.

    Mirrors :func:`_persist_breaker_state` (the pattern tier helper) but
    takes ``regime`` as a parameter so shadow rows
    (regime='portfolio_breaker_shadow') and live trip rows
    (regime='portfolio_breaker') are distinguishable in audit queries
    without a schema migration. Same rollback-before-close discipline.
    """
    try:
        from ...db import SessionLocal
        from sqlalchemy import text
        sess = SessionLocal()
        try:
            sess.execute(text(
                "INSERT INTO trading_risk_state (user_id, snapshot_date, "
                "breaker_tripped, breaker_reason, regime, capital) "
                "VALUES (:uid, NOW(), :tripped, :reason, :regime, 0)"
            ), {
                "uid": None,
                "tripped": tripped,
                "reason": (reason or "")[:500],
                "regime": regime,
            })
            sess.commit()
        finally:
            try:
                sess.rollback()
            except Exception:
                pass
            sess.close()
    except Exception:
        logger.debug(
            "[portfolio_breaker] persist failed", exc_info=True,
        )


def check_portfolio_drawdown_breaker(
    db: Session,
    user_id: int | None,
) -> tuple[bool, str | None]:
    """Portfolio-tier drawdown breaker.

    Independent of :func:`check_drawdown_breaker` (which gates the
    pattern tier). This function:

    - Returns ``(False, None)`` immediately when
      ``chili_portfolio_dd_breaker_enabled`` is False.
    - Computes threshold via :func:`_portfolio_dd_threshold` and
      numerator via :func:`_monthly_total_pnl`.
    - Skips with a WARNING when n_days_observed < 30 (matches the
      pattern tier's n<30 behavior; the portfolio tier may be dormant
      while the pattern tier is active or vice versa -- each tier's
      history is scoped independently).
    - In **shadow mode** (enabled=True, live=False): computes the
      would-have-tripped decision, persists a shadow row to
      ``trading_risk_state`` with regime='portfolio_breaker_shadow' and
      emits a structured INFO log line, but ALWAYS returns
      ``(False, None)`` so entries proceed. This is the 7-day soak path
      operators run before flipping the live flag.
    - In **live mode** (enabled=True, live=True): on trip, persists a
      live row with regime='portfolio_breaker' and returns
      ``(True, reason)`` -- the venue-adapter gate then blocks the entry.

    Returns ``(tripped: bool, reason: str | None)``.
    """
    from ...config import settings as _s
    if not bool(getattr(_s, "chili_portfolio_dd_breaker_enabled", False)):
        return False, None
    live = bool(getattr(_s, "chili_portfolio_dd_breaker_live", False))

    try:
        threshold, n_obs = _portfolio_dd_threshold(db, user_id, settings_obj=_s)
    except Exception:
        reason = "portfolio_dd_breaker_unavailable:threshold"
        logger.warning(
            "[portfolio_breaker] _portfolio_dd_threshold failed; %s",
            "blocking live entries" if live else "skipping check",
            exc_info=True,
        )
        if live:
            _persist_portfolio_breaker_state(
                tripped=True,
                reason=reason,
                regime="portfolio_breaker",
            )
            return True, reason
        return False, None

    if threshold is None:
        logger.warning(
            "[portfolio_breaker] enabled but only %dd all-closed history "
            "(<30 required); skipping check. The breaker activates "
            "organically once history accumulates.", n_obs,
        )
        return False, None

    try:
        monthly_pnl = _monthly_total_pnl(db, user_id)
    except Exception:
        reason = "portfolio_dd_breaker_unavailable:monthly_total_pnl"
        logger.warning(
            "[portfolio_breaker] _monthly_total_pnl failed; %s",
            "blocking live entries" if live else "skipping check",
            exc_info=True,
        )
        if live:
            _persist_portfolio_breaker_state(
                tripped=True,
                reason=reason,
                regime="portfolio_breaker",
            )
            return True, reason
        return False, None

    if float(monthly_pnl) > float(threshold):
        return False, None

    k_val = float(getattr(
        _s, "chili_portfolio_dd_breaker_lower_bound_sigmas", 2.0,
    ))
    reason = (
        f"portfolio_dd_breaker: 30-day realized PnL "
        f"${float(monthly_pnl):.2f} (ALL closed trades) "
        f"<= empirical Gaussian lower-bound ${float(threshold):.2f} "
        f"(K={k_val}σ, computed from {n_obs}d ALL-closed history)"
    )

    if not live:
        if bool(getattr(_s, "chili_portfolio_dd_breaker_shadow_log_enabled", True)):
            _persist_portfolio_breaker_state(
                tripped=False,
                reason=f"SHADOW: {reason}",
                regime="portfolio_breaker_shadow",
            )
            logger.info(
                "[portfolio_breaker_shadow] would_have_tripped=true "
                "threshold=%.2f monthly_total_pnl=%.2f n_obs=%d "
                "k_sigma=%.1f reason=%s",
                threshold, monthly_pnl, n_obs, k_val, reason,
            )
        return False, None

    _persist_portfolio_breaker_state(
        tripped=True,
        reason=reason,
        regime="portfolio_breaker",
    )
    logger.warning("[portfolio_breaker] TRIPPED: %s", reason)
    return True, reason


def _assert_portfolio_breaker_ok(
    user_id: int | None = None,
) -> tuple[bool, str | None]:
    """Venue-adapter entry boundary gate for the portfolio breaker.

    Single call site for both ``robinhood_spot`` and ``coinbase_spot``
    buy-entry methods. Self-managed DB session because venue adapters
    don't have request-scoped DB context — do NOT try to "optimize" this
    by threading a session through the venue adapter signature; the
    rate-limiter and idempotency-store already make DB-shape calls in
    the same hot path, so this is consistent with the existing adapter
    cost profile.

    Returns ``(True, None)`` when the breaker is disabled, in shadow
    mode, has insufficient history, or is not tripped — i.e. when the
    entry should proceed. Returns ``(False, reason)`` ONLY when both
    ``chili_portfolio_dd_breaker_enabled`` AND
    ``chili_portfolio_dd_breaker_live`` are True AND the breaker's trip
    condition is met.

    In live mode, DB/session/check failures block with an auditable
    ``portfolio_dd_breaker_unavailable`` reason. In disabled/shadow mode,
    failures remain pass-through so a shadow-soak outage does not halt
    entries.
    """
    enabled = False
    live = False
    try:
        from ...config import settings as _s
        enabled = bool(getattr(_s, "chili_portfolio_dd_breaker_enabled", False))
        live = bool(getattr(_s, "chili_portfolio_dd_breaker_live", False))
        if not enabled:
            return True, None
        from ...db import SessionLocal
        sess = SessionLocal()
        try:
            tripped, reason = check_portfolio_drawdown_breaker(sess, user_id)
            if not tripped:
                return True, None
            return False, reason
        finally:
            try:
                sess.rollback()
            except Exception:
                pass
            sess.close()
    except Exception:
        action = "failing CLOSED" if enabled and live else "failing OPEN"
        logger.warning(
            "[portfolio_breaker] gate check raised; %s",
            action,
            exc_info=True,
        )
        if enabled and live:
            return False, "portfolio_dd_breaker_unavailable:gate_exception"
        return True, None


def check_drawdown_breaker(
    db: Session,
    user_id: int | None,
    capital: float = 100_000.0,
    limits: DrawdownLimits | None = None,
) -> tuple[bool, str | None]:
    """Check if the circuit breaker should be tripped.

    Includes both realized (closed) and **unrealized (mark-to-market)** P&L
    to prevent the breaker from being blind to open losing positions.

    Returns (tripped: bool, reason: str | None).
    """
    global _breaker_tripped, _breaker_reason
    from datetime import datetime, timedelta

    if limits is None:
        # Phase 2: pass db so the regime multiplier kicks in (risk_off trips
        # earlier, risk_on more tolerant). Unchanged when no regime surface.
        limits = get_drawdown_limits(db=db)

    now = datetime.utcnow()

    # Mark-to-market unrealized P&L
    unrealized_pnl = 0.0
    try:
        unrealized_pnl = _compute_unrealized_pnl(db, user_id)
    except Exception:
        logger.debug("[circuit_breaker] Unrealized P&L computation failed", exc_info=True)

    unrealized_pct = (unrealized_pnl / capital * 100) if capital > 0 else 0

    # Trip immediately if unrealized loss exceeds 30-day threshold
    if unrealized_pct < -limits.max_30day_dd_pct:
        _breaker_tripped = True
        _breaker_reason = (
            f"Unrealized MTM drawdown {unrealized_pct:.1f}% "
            f"exceeds -{limits.max_30day_dd_pct}% limit"
        )
        _persist_breaker_state(
            True,
            _breaker_reason,
            user_id=user_id,
            capital=capital,
        )
        logger.warning("[circuit_breaker] TRIPPED (MTM): %s", _breaker_reason)
        return True, _breaker_reason

    # 5-day rolling P&L (realized + unrealized) — YY: CHILI-placed only
    five_days_ago = now - timedelta(days=5)
    q5 = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        Trade.exit_date >= five_days_ago,
    )
    recent_5d = _breaker_trade_filter(q5).all()
    pnl_5d_realized = _sum_trade_realized_pnl(recent_5d)
    pnl_5d_total = pnl_5d_realized + unrealized_pnl
    pnl_5d_pct = (pnl_5d_total / capital * 100) if capital > 0 else 0

    if pnl_5d_pct < -limits.max_5day_dd_pct:
        _breaker_tripped = True
        _breaker_reason = (
            f"5-day drawdown {pnl_5d_pct:.1f}% (realized={pnl_5d_realized:.0f}, "
            f"unrealized={unrealized_pnl:.0f}) exceeds -{limits.max_5day_dd_pct}% limit"
        )
        _persist_breaker_state(
            True,
            _breaker_reason,
            user_id=user_id,
            capital=capital,
        )
        logger.warning("[circuit_breaker] TRIPPED: %s", _breaker_reason)
        return True, _breaker_reason

    # 30-day rolling P&L (realized + unrealized) — YY: CHILI-placed only
    thirty_days_ago = now - timedelta(days=30)
    q30 = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        Trade.exit_date >= thirty_days_ago,
    )
    recent_30d = _breaker_trade_filter(q30).all()
    pnl_30d_realized = _sum_trade_realized_pnl(recent_30d)
    pnl_30d_total = pnl_30d_realized + unrealized_pnl
    pnl_30d_pct = (pnl_30d_total / capital * 100) if capital > 0 else 0

    if pnl_30d_pct < -limits.max_30day_dd_pct:
        _breaker_tripped = True
        _breaker_reason = (
            f"30-day drawdown {pnl_30d_pct:.1f}% (realized={pnl_30d_realized:.0f}, "
            f"unrealized={unrealized_pnl:.0f}) exceeds -{limits.max_30day_dd_pct}% limit"
        )
        _persist_breaker_state(
            True,
            _breaker_reason,
            user_id=user_id,
            capital=capital,
        )
        logger.warning("[circuit_breaker] TRIPPED: %s", _breaker_reason)
        return True, _breaker_reason

    # f-phase3-stop-bleed D1 — empirical monthly DD breaker.
    # Default OFF until walk-forward shows trip ~2026-04-22 in
    # docs/STRATEGY/CC_REPORTS/2026-05-15_phase3-stop-bleed.md, after
    # which the operator flips the flag ON. The threshold is data-driven;
    # if <30d of CHILI-attributed history exists the helper returns None
    # and this check is skipped (no fallback dollar value).
    try:
        from ...config import settings as _s_dd
        flag_enabled = bool(getattr(_s_dd, "chili_pattern_dd_breaker_enabled", False))
    except Exception:
        flag_enabled = False
    if flag_enabled:
        try:
            threshold, n_obs = _monthly_dd_threshold(db, user_id, settings_obj=_s_dd)
        except Exception:
            logger.warning(
                "[circuit_breaker] _monthly_dd_threshold failed; skipping check",
                exc_info=True,
            )
            threshold, n_obs = None, 0
        if threshold is None:
            logger.warning(
                "[circuit_breaker] monthly_dd_breaker enabled but only %dd "
                "CHILI-attributed history (<30 required); skipping check. "
                "The breaker activates organically once history accumulates.",
                n_obs,
            )
        else:
            # Attribution-symmetric numerator: same scope as the threshold
            # (scan_pattern_id IS NOT NULL AND != -1). Without this, no_pattern
            # bleed pushes the numerator down without widening the threshold's
            # variance estimate -- see ARCHITECT-FLAG in
            # 2026-05-16_phase3-monthly-dd-breaker-arming-watch.md.
            monthly_pnl = _monthly_attributed_pnl(db, user_id)
            if float(monthly_pnl) < float(threshold):
                _breaker_tripped = True
                k_val = float(getattr(
                    _s_dd, "chili_pattern_dd_breaker_lower_bound_sigmas", 2.0
                ))
                _breaker_reason = (
                    f"monthly_dd_breaker: 30-day realized PnL "
                    f"${float(monthly_pnl):.2f} (CHILI-attributed only) "
                    f"< empirical Gaussian lower-bound "
                    f"${float(threshold):.2f} "
                    f"(K={k_val}σ, computed from {n_obs}d CHILI history)"
                )
                _persist_breaker_state(
                    True,
                    _breaker_reason,
                    user_id=user_id,
                    capital=capital,
                )
                logger.warning("[circuit_breaker] TRIPPED: %s", _breaker_reason)
                return True, _breaker_reason

    # Consecutive losses — YY: CHILI-placed only.
    #
    # R31 (2026-04-30): two refinements after a false trip on 5 micro-
    # losses summing to -$3.90 (-0.016% of capital):
    #
    # 1. Exclude synthetic / reconcile-only exits. These are NOT CHILI
    #    decisions -- broker_sync inferred the close after the position
    #    disappeared, often with crude price estimates. A streak of
    #    those looks like consecutive losses but reflects book-keeping
    #    reconciliation, not bad signals.
    # 2. Apply a magnitude floor. A streak of N micro-losses summing
    #    to less than ``brain_risk_min_streak_loss_pct`` of capital is
    #    statistically normal and not a circuit-breaker event. Only
    #    material streaks fire.
    #
    # Per-pattern attribution lives in a different layer (Phase 1
    # Flags 5 and 6 -- chili_pattern_survival_sizing_enabled +
    # chili_pattern_survival_demote_enabled). The breaker is the LAST
    # defense, not the per-strategy adaptation surface.
    SYNTHETIC_EXIT_REASONS = (
        "broker_reconcile_position_gone",
        "broker_reconcile_no_exit_price",
        "phantom_no_broker_id",
        "phantom_no_broker_id_205",
        "phantom_zero_entry_price",
        "zombie_reconcile_orphan",
        "sync_duplicate",
    )
    qcons = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        Trade.exit_date.isnot(None),
        # exit_reason can be NULL on legitimate closes, so the test must
        # accept NULL.
        (Trade.exit_reason.is_(None) | Trade.exit_reason.notin_(SYNTHETIC_EXIT_REASONS)),
    )
    qcons = _breaker_trade_filter(qcons)
    last_n = (
        qcons.order_by(Trade.exit_date.desc().nulls_last())
        .limit(limits.max_consecutive_losses)
        .all()
    )
    if len(last_n) >= limits.max_consecutive_losses:
        last_pnls = [_trade_realized_pnl_with_raw_fallback(t) for t in last_n]
        if all(pnl is not None and pnl < 0 for pnl in last_pnls):
            streak_loss = sum(float(pnl) for pnl in last_pnls if pnl is not None)
            try:
                from ...config import settings as _s
                min_pct = float(getattr(_s, "brain_risk_min_streak_loss_pct", 1.0))
            except Exception:
                min_pct = 1.0
            min_loss_dollars = abs(min_pct / 100.0 * (capital or 0.0))
            if abs(streak_loss) > min_loss_dollars:
                _breaker_tripped = True
                _breaker_reason = (
                    f"{limits.max_consecutive_losses} consecutive losing trades "
                    f"(real CHILI decisions, total=${streak_loss:.2f}, "
                    f"floor=${min_loss_dollars:.2f})"
                )
                _persist_breaker_state(
                    True,
                    _breaker_reason,
                    user_id=user_id,
                    capital=capital,
                )
                logger.warning("[circuit_breaker] TRIPPED: %s", _breaker_reason)
                return True, _breaker_reason
            else:
                logger.info(
                    "[circuit_breaker] consecutive-loss streak ignored: "
                    "sum=$%.2f below %s%% floor=$%.2f (synthetic exits filtered)",
                    streak_loss, min_pct, min_loss_dollars,
                )

    _breaker_tripped = False
    _breaker_reason = None
    return False, None


def is_breaker_tripped() -> bool:
    return _breaker_tripped


def _refresh_breaker_from_db_for_gate(
    db: Session,
    *,
    user_id: int | None = None,
) -> tuple[bool, str | None] | None:
    """Synchronize entry gates with durable circuit-breaker state.

    User-scoped breaker trips block only that user. ``user_id IS NULL``
    rows are explicit global reset/trip events and apply to every user.
    """
    execute = getattr(db, "execute", None)
    if not callable(execute):
        return None

    global _breaker_tripped, _breaker_reason
    try:
        from sqlalchemy import text

        row = execute(text(
            "SELECT breaker_tripped, breaker_reason "
            "FROM trading_risk_state "
            "WHERE regime = 'circuit_breaker' "
            "  AND ("
            "      (:uid IS NULL AND user_id IS NULL) "
            "      OR (:uid IS NOT NULL AND (user_id = :uid OR user_id IS NULL))"
            "  ) "
            "ORDER BY created_at DESC, id DESC "
            "LIMIT 1"
        ), {"uid": user_id}).fetchone()
    except Exception:
        logger.warning(
            "[circuit_breaker] durable state read failed; blocking entries",
            exc_info=True,
        )
        _breaker_tripped = True
        _breaker_reason = "durable_breaker_state_unavailable"
        return True, _breaker_reason

    if row is None:
        return None

    tripped = bool(row[0])
    reason = str(row[1] or "restored from DB") if tripped else None
    _breaker_tripped = tripped
    _breaker_reason = reason
    return tripped, reason


def get_breaker_status() -> dict[str, Any]:
    return {
        "tripped": _breaker_tripped,
        "reason": _breaker_reason,
    }


def reset_breaker() -> None:
    """Manually reset the circuit breaker (admin action). Persists to DB."""
    global _breaker_tripped, _breaker_reason
    _breaker_tripped = False
    _breaker_reason = None
    _persist_breaker_state(False, None)
    logger.info("[circuit_breaker] Manually reset")


def _persist_breaker_state(
    tripped: bool,
    reason: str | None,
    *,
    user_id: int | None = None,
    capital: float | None = None,
) -> None:
    """Write circuit breaker state to trading_risk_state so it survives restarts."""
    try:
        from ...db import SessionLocal
        from sqlalchemy import text
        capital_f = _float_or_none(capital) or 0.0
        sess = SessionLocal()
        try:
            sess.execute(text(
                "INSERT INTO trading_risk_state (user_id, snapshot_date, breaker_tripped, breaker_reason, regime, capital) "
                "VALUES (:uid, NOW(), :tripped, :reason, 'circuit_breaker', :capital) "
            ), {
                "uid": user_id,
                "tripped": tripped,
                "reason": reason or "",
                "capital": capital_f,
            })
            sess.commit()
        finally:
            # FIX 46 pattern: rollback to end implicit read txn before close.
            try:
                sess.rollback()
            except Exception:
                pass
            sess.close()
    except Exception:
        logger.debug("[circuit_breaker] Failed to persist breaker state to DB", exc_info=True)


def write_daily_breaker_liveness_snapshot(db: Session) -> dict[str, Any]:
    """FIX G-1 (2026-04-29 third-pass audit): periodic liveness snapshot to
    ``trading_risk_state``.

    The breaker is event-driven (only writes on trip/reset), so a healthy
    system shows a stale ``trading_risk_state`` row. The audit flagged
    this as ``trading_risk_state 2 days stale`` and worried that Hard
    Rule 2 was silently permissive. It isn't -- ``check_drawdown_breaker``
    runs live on every ``unified_risk_check`` call. But ops cannot tell
    "breaker is alive and not tripped" from "breaker writer is dead"
    without a heartbeat row.

    This function writes one snapshot per call -- intended to be called
    from a daily scheduler job. The row is tagged ``regime='breaker_heartbeat'``
    (distinct from the ``regime='circuit_breaker'`` row that
    ``_persist_breaker_state`` uses on actual trips/resets) so the trip
    log stays clean.

    Returns the computed snapshot for logging.
    """
    from datetime import datetime

    from sqlalchemy import text
    snapshot: dict[str, Any] = {}
    try:
        # Compute current breaker state non-destructively. Use the same
        # entry point the gate uses; if it trips, _persist_breaker_state
        # already wrote a 'circuit_breaker' row -- our heartbeat row is
        # ADDITIONAL.
        capital_basis = 100_000.0
        tripped, reason = check_drawdown_breaker(
            db, user_id=None, capital=capital_basis,
        )
        snapshot = {
            "tripped": bool(tripped),
            "reason": reason,
            "capital": capital_basis,
            "computed_at": datetime.utcnow().isoformat(timespec="seconds"),
        }
        db.execute(text(
            "INSERT INTO trading_risk_state "
            "(user_id, snapshot_date, breaker_tripped, breaker_reason, regime, capital) "
            "VALUES (:uid, NOW(), :tripped, :reason, 'breaker_heartbeat', :capital)"
        ), {
            "uid": None,
            "tripped": bool(tripped),
            "reason": (reason or "alive")[:200],
            "capital": capital_basis,
        })
        db.commit()
        logger.info(
            "[circuit_breaker] heartbeat snapshot written: tripped=%s reason=%s",
            bool(tripped), reason,
        )
    except Exception:
        logger.warning(
            "[circuit_breaker] heartbeat snapshot write FAILED",
            exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            pass
    return snapshot


def restore_breaker_from_db() -> None:
    """Restore circuit breaker state from DB on startup."""
    global _breaker_tripped, _breaker_reason
    try:
        from ...db import SessionLocal
        sess = SessionLocal()
        try:
            restored = _refresh_breaker_from_db_for_gate(sess)
            if restored and restored[0]:
                logger.warning("[circuit_breaker] Breaker restored from DB: %s", _breaker_reason)
        finally:
            # FIX 46 pattern: rollback to end implicit read txn before close.
            try:
                sess.rollback()
            except Exception:
                pass
            sess.close()
    except Exception:
        logger.debug("[circuit_breaker] Could not restore breaker from DB", exc_info=True)


# ── Unified Risk Gate ─────────────────────────────────────────────────


def unified_risk_check(
    db: Session,
    user_id: int | None,
    ticker: str,
    *,
    capital: float = 100_000.0,
    asset_type: str | None = None,
    entry_price: float | None = None,
    stop_price: float | None = None,
    execution_path: str = "unknown",
) -> tuple[bool, str, dict[str, Any]]:
    """Single risk gate for ALL execution paths (proposals, momentum, paper, live).

    Returns (allowed: bool, reason: str, detail: dict).
    This replaces the need for each path to call different risk functions.
    """
    detail: dict[str, Any] = {"execution_path": execution_path}

    # 1. Kill switch
    try:
        from .governance import is_kill_switch_active
        if is_kill_switch_active():
            return False, "Kill switch active", detail
    except Exception:
        return False, "Kill-switch check failed — blocking as precaution", detail

    # 2. Circuit breaker (now includes MTM)
    capital_f = _float_or_none(capital)
    if capital_f is None:
        detail["capital_valid"] = False
        return False, "invalid_capital", detail

    persisted_breaker = _refresh_breaker_from_db_for_gate(db, user_id=user_id)
    if persisted_breaker is not None:
        persisted_tripped, persisted_reason = persisted_breaker
        if persisted_tripped:
            detail["breaker_reason"] = persisted_reason
            return False, f"Circuit breaker: {persisted_reason}", detail
    elif is_breaker_tripped():
        detail["breaker_reason"] = _breaker_reason
        return False, f"Circuit breaker: {_breaker_reason}", detail

    tripped, reason = check_drawdown_breaker(db, user_id, capital_f)
    if tripped:
        detail["breaker_reason"] = reason
        return False, f"Circuit breaker: {reason}", detail

    # 3. Position limits
    limits = get_risk_limits()
    budget = get_portfolio_risk_snapshot(db, user_id, capital_f, limits)
    detail["portfolio_heat_pct"] = budget.total_heat_pct
    detail["open_positions"] = budget.open_positions

    if not budget.can_open_new:
        return False, budget.rejection_reason or "Risk limit exceeded", detail

    # 4. Asset-class caps
    asset_kind = _new_trade_asset_kind(ticker, asset_type)
    detail["asset_kind"] = asset_kind
    if asset_kind == "crypto" and budget.crypto_positions >= limits.max_crypto_positions:
        return False, f"Crypto cap ({limits.max_crypto_positions}) reached", detail
    if asset_kind == "equity" and budget.stock_positions >= limits.max_stock_positions:
        return False, f"Stock cap ({limits.max_stock_positions}) reached", detail

    # 5. Same-ticker limit
    same_count = sum(
        1
        for row in _broker_live_open_trades(db, user_id)
        if str(row.ticker or "").upper() == ticker.upper()
    )
    if same_count >= limits.max_same_ticker:
        return False, f"Already {same_count} open in {ticker}", detail

    # 6. Sector concentration (user-scoped)
    try:
        sector_q = db.query(Trade.sector).filter(Trade.ticker == ticker.upper())
        if user_id is not None:
            sector_q = sector_q.filter(Trade.user_id == user_id)
        sect = sector_q.order_by(Trade.id.desc()).first()
        sect_label = sect[0] if sect else None
        allowed, reason = check_sector_concentration(db, user_id, sect_label, limits)
        if not allowed:
            return False, reason, detail
    except Exception:
        detail["sector_check_reason"] = "check_failed"
        return False, "sector_check_unavailable", detail

    # 7. Correlation
    try:
        allowed, reason = check_correlation_risk(db, user_id, ticker, limits)
        if not allowed:
            return False, reason, detail
    except Exception:
        detail["correlation_check_reason"] = "check_failed"
        return False, "correlation_check_unavailable", detail

    # 8. Portfolio-level drawdown check (live broker scope, not paper-shadow)
    try:
        dd = check_live_portfolio_drawdown(db, user_id, capital_f)
        detail["portfolio_dd_pct"] = dd.get("dd_pct")
        detail["portfolio_drawdown_reason"] = dd.get("reason")
        detail["portfolio_valuation_missing_count"] = dd.get("valuation_missing_count")
        if dd.get("reason") in {"valuation_unavailable", "invalid_capital"}:
            return False, "portfolio_drawdown_unavailable", detail
        if dd.get("breached"):
            return False, f"Portfolio drawdown {dd.get('dd_pct', 0.0):.1f}% breached limit", detail
    except Exception as exc:
        logger.warning("[risk] Portfolio drawdown unavailable; blocking as precaution: %s", exc)
        detail["portfolio_drawdown_reason"] = "check_failed"
        return False, "portfolio_drawdown_unavailable", detail

    # 9. Compute recommended size if entry/stop provided
    if entry_price and stop_price and entry_price > 0 and stop_price > 0:
        qty = size_position(capital, entry_price, stop_price, limits=limits)
        detail["recommended_quantity"] = qty
        if qty <= 0:
            return False, "Position size would be zero — risk too large", detail

    detail["allowed"] = True
    return True, "ok", detail
