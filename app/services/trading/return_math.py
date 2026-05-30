"""Shared realized-return helpers.

The trading stack stores option fills as per-contract premiums while P&L is
stored as total dollars. Learning code should therefore normalize realized P&L
by entry premium * contracts * contract multiplier whenever P&L is available.
"""
from __future__ import annotations

import json
import math
from typing import Any, Mapping


try:
    from .options.contracts import (
        OPTION_CONTRACT_MULTIPLIER,
        PRICE_DOMAIN_OPTION_PREMIUM,
    )
except Exception:  # pragma: no cover - import guard for partial bootstrap paths
    OPTION_CONTRACT_MULTIPLIER = 100.0
    PRICE_DOMAIN_OPTION_PREMIUM = "option_premium"


_OPTION_PRICE_FALLBACK_MAX_RATIO = 50.0


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


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return None


def _nested_mapping(source: Mapping[str, Any] | None, key: str) -> Mapping[str, Any] | None:
    if not isinstance(source, Mapping):
        return None
    return _as_mapping(source.get(key))


def _price_domain_is_option_premium(value: Any) -> bool:
    return str(value or "").strip().lower() == PRICE_DOMAIN_OPTION_PREMIUM


def _option_price_domain_confirmed(source: Any, price_field: str) -> bool:
    """True only when option price fallback is explicitly in premium space."""
    snap = _as_mapping(source)
    if not snap:
        return False

    price_domains = _nested_mapping(snap, "price_domains")
    if _price_domain_is_option_premium(
        price_domains.get(price_field) if price_domains else None
    ):
        return True

    option_meta = _nested_mapping(snap, "option_meta")
    if _price_domain_is_option_premium(
        option_meta.get("price_domain") if option_meta else None
    ):
        return True

    entry_execution = _nested_mapping(snap, "entry_execution")
    if _price_domain_is_option_premium(
        entry_execution.get("option_price_domain") if entry_execution else None
    ):
        return True

    breakout = _nested_mapping(snap, "breakout_alert")
    return bool(breakout and _option_price_domain_confirmed(breakout, price_field))


def _price_ratio_is_plausible(entry_price: Any, exit_price: Any) -> bool:
    entry = _float_or_none(entry_price)
    exit_ = _float_or_none(exit_price)
    if entry is None or entry <= 0 or exit_ is None or exit_ <= 0:
        return False
    ratio = exit_ / entry
    return (
        ratio <= _OPTION_PRICE_FALLBACK_MAX_RATIO
        and ratio >= 1.0 / _OPTION_PRICE_FALLBACK_MAX_RATIO
    )


def _option_price_return_pct(
    entry_price: Any,
    exit_price: Any,
    direction: Any,
    *,
    source: Any,
) -> float | None:
    if not (
        _option_price_domain_confirmed(source, "entry_price")
        and _option_price_domain_confirmed(source, "exit_price")
    ):
        return None
    if not _price_ratio_is_plausible(entry_price, exit_price):
        return None
    return price_return_pct(entry_price, exit_price, direction)


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
    signal = _as_mapping(signal)
    if not isinstance(signal, Mapping):
        return False
    if signal.get("option_meta"):
        return True
    if str(signal.get("asset_kind") or "").strip().lower() in {"option", "options"}:
        return True
    if str(signal.get("asset_type") or "").strip().lower() in {"option", "options"}:
        return True
    if _truthy(signal.get("options_path")):
        return True
    breakout = _as_mapping(signal.get("breakout_alert"))
    if isinstance(breakout, Mapping):
        if breakout.get("option_meta"):
            return True
        if str(breakout.get("asset_kind") or "").strip().lower() in {"option", "options"}:
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
    try:
        if trade_contract_multiplier(trade) == OPTION_CONTRACT_MULTIPLIER:
            return _option_price_return_pct(
                getattr(trade, "entry_price", None),
                getattr(trade, "exit_price", None),
                getattr(trade, "direction", "long"),
                source=getattr(trade, "indicator_snapshot", None),
            )
    except Exception:
        return None
    return price_return_pct(
        getattr(trade, "entry_price", None),
        getattr(trade, "exit_price", None),
        getattr(trade, "direction", "long"),
    )


def paper_trade_return_pct(paper_trade: Any) -> float | None:
    """Realized signed return for a paper trade."""
    is_option = (
        paper_trade_contract_multiplier(paper_trade)
        == OPTION_CONTRACT_MULTIPLIER
    )
    pnl_ret = notional_return_pct(
        getattr(paper_trade, "pnl", None),
        getattr(paper_trade, "entry_price", None),
        getattr(paper_trade, "quantity", None),
        contract_multiplier=(
            OPTION_CONTRACT_MULTIPLIER if is_option else 1.0
        ),
    )
    if pnl_ret is not None:
        return pnl_ret
    signal_json = getattr(paper_trade, "signal_json", None)
    if is_option:
        return _option_price_return_pct(
            getattr(paper_trade, "entry_price", None),
            getattr(paper_trade, "exit_price", None),
            getattr(paper_trade, "direction", "long"),
            source=signal_json,
        )
    stored_pct = _float_or_none(getattr(paper_trade, "pnl_pct", None))
    if stored_pct is not None:
        return stored_pct
    return price_return_pct(
        getattr(paper_trade, "entry_price", None),
        getattr(paper_trade, "exit_price", None),
        getattr(paper_trade, "direction", "long"),
    )
