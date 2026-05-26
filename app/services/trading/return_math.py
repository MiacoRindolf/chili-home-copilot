"""Shared realized-return helpers.

The trading stack stores option fills as per-contract premiums while P&L is
stored as total dollars. Learning code should therefore normalize realized P&L
by entry premium * contracts * contract multiplier whenever P&L is available.
"""
from __future__ import annotations

import math
from typing import Any


OPTION_CONTRACT_MULTIPLIER = 100.0


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _is_short(direction: Any) -> bool:
    return str(direction or "").strip().lower() == "short"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def price_return_pct(
    entry_price: Any,
    exit_price: Any,
    direction: Any = "long",
) -> float | None:
    """Signed return from prices, direction-aware."""
    entry = _float_or_none(entry_price)
    exit_ = _float_or_none(exit_price)
    if entry is None or entry <= 0 or exit_ is None:
        return None
    if _is_short(direction):
        return ((entry - exit_) / entry) * 100.0
    return ((exit_ - entry) / entry) * 100.0


def notional_return_pct(
    pnl: Any,
    entry_price: Any,
    quantity: Any,
    *,
    contract_multiplier: float = 1.0,
) -> float | None:
    """Signed return from realized P&L normalized by opening notional."""
    pnl_f = _float_or_none(pnl)
    entry = _float_or_none(entry_price)
    qty = _float_or_none(quantity)
    mult = _float_or_none(contract_multiplier)
    if (
        pnl_f is None
        or entry is None
        or entry <= 0
        or qty is None
        or qty <= 0
        or mult is None
        or mult <= 0
    ):
        return None
    return (pnl_f / (entry * qty * mult)) * 100.0


def trade_contract_multiplier(trade: Any) -> float:
    try:
        from .autopilot_scope import is_option_trade

        if is_option_trade(trade):
            return OPTION_CONTRACT_MULTIPLIER
    except Exception:
        pass
    return 1.0


def _signal_json_is_option(signal: Any) -> bool:
    if not isinstance(signal, dict):
        return False
    if signal.get("option_meta"):
        return True
    if str(signal.get("asset_type") or "").strip().lower() in {"option", "options"}:
        return True
    if _truthy(signal.get("options_path")):
        return True
    breakout = signal.get("breakout_alert")
    if isinstance(breakout, dict):
        if breakout.get("option_meta"):
            return True
        if str(breakout.get("asset_type") or "").strip().lower() in {"option", "options"}:
            return True
        if _truthy(breakout.get("options_path")):
            return True
    return False


def paper_trade_contract_multiplier(paper_trade: Any) -> float:
    if _signal_json_is_option(getattr(paper_trade, "signal_json", None)):
        return OPTION_CONTRACT_MULTIPLIER
    return 1.0


def trade_return_pct(trade: Any) -> float | None:
    """Realized signed return for a live trade.

    Prefer recorded P&L because it already carries short-side sign, partials,
    fill corrections, and option contract multiplier dollars. Fall back to
    price return only for older rows without P&L.
    """
    pnl_ret = notional_return_pct(
        getattr(trade, "pnl", None),
        getattr(trade, "entry_price", None),
        getattr(trade, "quantity", None),
        contract_multiplier=trade_contract_multiplier(trade),
    )
    if pnl_ret is not None:
        return pnl_ret
    return price_return_pct(
        getattr(trade, "entry_price", None),
        getattr(trade, "exit_price", None),
        getattr(trade, "direction", "long"),
    )


def paper_trade_return_pct(paper_trade: Any) -> float | None:
    """Realized signed return for a paper trade."""
    pnl_ret = notional_return_pct(
        getattr(paper_trade, "pnl", None),
        getattr(paper_trade, "entry_price", None),
        getattr(paper_trade, "quantity", None),
        contract_multiplier=paper_trade_contract_multiplier(paper_trade),
    )
    if pnl_ret is not None:
        return pnl_ret
    stored_pct = _float_or_none(getattr(paper_trade, "pnl_pct", None))
    if stored_pct is not None:
        return stored_pct
    return price_return_pct(
        getattr(paper_trade, "entry_price", None),
        getattr(paper_trade, "exit_price", None),
        getattr(paper_trade, "direction", "long"),
    )
