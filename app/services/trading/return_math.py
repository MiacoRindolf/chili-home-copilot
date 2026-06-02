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

try:
    from .asset_class import (
        PATTERN_ASSET_CLASS_OPTIONS,
        normalize_pattern_asset_class,
    )
except Exception:  # pragma: no cover - import guard for partial bootstrap paths
    PATTERN_ASSET_CLASS_OPTIONS = "options"

    def normalize_pattern_asset_class(value: object) -> str:
        raw = str(value or "").strip().lower().replace("-", "_")
        if raw in {
            "option",
            "options",
            "option_contract",
            "option_contracts",
            "options_contract",
            "options_contracts",
            "contract_option",
            "contract_options",
            "equity_option",
            "equity_options",
            "stock_option",
            "stock_options",
            "option_spread",
            "options_spread",
            "option_spreads",
            "options_spreads",
            "optionspread",
            "optionspreads",
            "robinhood_option",
            "robinhood_options",
        }:
            return PATTERN_ASSET_CLASS_OPTIONS
        return raw


_OPTION_PRICE_FALLBACK_MAX_RATIO = 50.0
_OPTION_ASSET_CLASS_ALIASES = frozenset(
    {
        "option",
        "options",
        "option_contract",
        "option_contracts",
        "options_contract",
        "options_contracts",
        "contract_option",
        "contract_options",
        "equity_option",
        "equity_options",
        "stock_option",
        "stock_options",
        "option_spread",
        "options_spread",
        "option_spreads",
        "options_spreads",
        "optionspread",
        "optionspreads",
        "robinhood_option",
        "robinhood_options",
    }
)


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _finite_result(value: float) -> float | None:
    return value if math.isfinite(value) else None


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


def _mapping_has_option_price_domain(source: Any) -> bool:
    """True when a sparse signal proves prices are option premiums."""
    snap = _as_mapping(source)
    if not isinstance(snap, Mapping):
        return False

    price_domains = _nested_mapping(snap, "price_domains")
    if isinstance(price_domains, Mapping):
        for key in ("entry_price", "exit_price", "limit_price"):
            if _price_domain_is_option_premium(price_domains.get(key)):
                return True

    if _price_domain_is_option_premium(snap.get("option_price_domain")):
        return True

    option_meta = _nested_mapping(snap, "option_meta")
    if isinstance(option_meta, Mapping) and _price_domain_is_option_premium(
        option_meta.get("price_domain")
    ):
        return True

    entry_execution = _nested_mapping(snap, "entry_execution")
    return bool(
        isinstance(entry_execution, Mapping)
        and _price_domain_is_option_premium(
            entry_execution.get("option_price_domain")
        )
    )


def _contract_multiplier_is_option(value: Any) -> bool:
    multiplier = _float_or_none(value)
    return (
        multiplier is not None
        and abs(multiplier - OPTION_CONTRACT_MULTIPLIER) < 1e-9
    )


def _asset_value_is_option(value: Any) -> bool:
    raw = str(value or "").strip().lower().replace("-", "_")
    if not raw:
        return False
    try:
        return normalize_pattern_asset_class(raw) == PATTERN_ASSET_CLASS_OPTIONS
    except Exception:
        return raw in _OPTION_ASSET_CLASS_ALIASES


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

    paper_meta = _nested_mapping(snap, "_paper_meta")
    if isinstance(paper_meta, Mapping) and _option_price_domain_confirmed(
        paper_meta,
        price_field,
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
        return _finite_result(((entry - exit_) / entry) * 100.0)
    return _finite_result(((exit_ - entry) / entry) * 100.0)


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
    opening_notional = entry * qty * mult
    if not math.isfinite(opening_notional) or opening_notional <= 0:
        return None
    return _finite_result((pnl_f / opening_notional) * 100.0)


def _quantity_for_notional(row: Any) -> float | None:
    filled = _float_or_none(getattr(row, "filled_quantity", None))
    if filled is not None and filled > 0:
        return filled
    qty = _float_or_none(getattr(row, "quantity", None))
    return qty if qty is not None and qty > 0 else None


def _partial_leg_declared(row: Any) -> bool:
    if _truthy(getattr(row, "partial_taken", None)):
        return True
    return (
        getattr(row, "partial_taken_qty", None) is not None
        or getattr(row, "partial_taken_price", None) is not None
    )


def _partial_leg_pnl(row: Any, *, entry_price: float, contract_multiplier: float) -> tuple[float, float] | None:
    """Return (partial_pnl, partial_qty), or None when partial evidence is incomplete."""
    partial_qty = _float_or_none(getattr(row, "partial_taken_qty", None))
    partial_price = _float_or_none(getattr(row, "partial_taken_price", None))
    if partial_qty is None or partial_qty <= 0 or partial_price is None or partial_price <= 0:
        return None
    if _is_short(getattr(row, "direction", None)):
        partial_pnl = (entry_price - partial_price) * partial_qty * contract_multiplier
    else:
        partial_pnl = (partial_price - entry_price) * partial_qty * contract_multiplier
    if not math.isfinite(partial_pnl):
        return None
    return partial_pnl, partial_qty


def realized_return_pct(row: Any, *, contract_multiplier: float = 1.0) -> float | None:
    """Signed realized return, including a recorded partial leg when present.

    Local partial-close paths reduce ``quantity`` before the final close and store
    the earlier realized leg in ``partial_taken_*``. If that leg is declared but
    incomplete, the safe learning answer is no return sample.
    """
    pnl_f = _float_or_none(getattr(row, "pnl", None))
    entry = _float_or_none(getattr(row, "entry_price", None))
    mult = _float_or_none(contract_multiplier)
    if pnl_f is None or entry is None or entry <= 0 or mult is None or mult <= 0:
        return None

    if _partial_leg_declared(row):
        current_qty = _float_or_none(getattr(row, "quantity", None))
        partial = _partial_leg_pnl(row, entry_price=entry, contract_multiplier=mult)
        if current_qty is None or current_qty <= 0 or partial is None:
            return None
        partial_pnl, partial_qty = partial
        opening_qty = current_qty + partial_qty
        opening_notional = entry * opening_qty * mult
        if (
            not math.isfinite(opening_qty)
            or opening_qty <= 0
            or not math.isfinite(opening_notional)
            or opening_notional <= 0
        ):
            return None
        return _finite_result(((pnl_f + partial_pnl) / opening_notional) * 100.0)

    qty = _quantity_for_notional(row)
    if qty is None:
        return None
    return notional_return_pct(
        pnl_f,
        entry,
        qty,
        contract_multiplier=mult,
    )


def realized_pnl(row: Any, *, contract_multiplier: float = 1.0) -> float | None:
    """Signed realized dollar P&L, including a recorded partial leg when present."""
    pnl_f = _float_or_none(getattr(row, "pnl", None))
    if pnl_f is None:
        return None
    if not _partial_leg_declared(row):
        return pnl_f

    entry = _float_or_none(getattr(row, "entry_price", None))
    mult = _float_or_none(contract_multiplier)
    if entry is None or entry <= 0 or mult is None or mult <= 0:
        return None
    partial = _partial_leg_pnl(row, entry_price=entry, contract_multiplier=mult)
    if partial is None:
        return None
    partial_pnl, _partial_qty = partial
    return _finite_result(pnl_f + partial_pnl)


def trade_contract_multiplier(trade: Any) -> float:
    try:
        from .autopilot_scope import is_option_trade

        if is_option_trade(trade):
            return OPTION_CONTRACT_MULTIPLIER
    except Exception:
        pass
    if _signal_json_is_option(getattr(trade, "indicator_snapshot", None)):
        return OPTION_CONTRACT_MULTIPLIER
    return 1.0


def _signal_json_is_option(signal: Any) -> bool:
    signal = _as_mapping(signal)
    if not isinstance(signal, Mapping):
        return False
    if signal.get("option_meta"):
        return True
    if _mapping_has_option_price_domain(signal):
        return True
    if _asset_value_is_option(signal.get("asset_kind")):
        return True
    if _asset_value_is_option(signal.get("asset_type")):
        return True
    if _asset_value_is_option(signal.get("asset_class")):
        return True
    if _truthy(signal.get("options_path")):
        return True
    if _contract_multiplier_is_option(signal.get("option_contract_multiplier")):
        return True
    if _contract_multiplier_is_option(signal.get("contract_multiplier")):
        return True
    paper_meta = _nested_mapping(signal, "_paper_meta")
    if isinstance(paper_meta, Mapping):
        if paper_meta.get("option_meta"):
            return True
        if _mapping_has_option_price_domain(paper_meta):
            return True
        if _truthy(paper_meta.get("options_path")):
            return True
        if _asset_value_is_option(paper_meta.get("asset_kind")):
            return True
        if _asset_value_is_option(paper_meta.get("asset_type")):
            return True
        if _asset_value_is_option(paper_meta.get("asset_class")):
            return True
        if _contract_multiplier_is_option(paper_meta.get("option_contract_multiplier")):
            return True
        if _contract_multiplier_is_option(paper_meta.get("contract_multiplier")):
            return True
    breakout = _as_mapping(signal.get("breakout_alert"))
    if isinstance(breakout, Mapping):
        if breakout.get("option_meta"):
            return True
        if _mapping_has_option_price_domain(breakout):
            return True
        if _asset_value_is_option(breakout.get("asset_kind")):
            return True
        if _asset_value_is_option(breakout.get("asset_type")):
            return True
        if _asset_value_is_option(breakout.get("asset_class")):
            return True
        if _truthy(breakout.get("options_path")):
            return True
        if _contract_multiplier_is_option(breakout.get("option_contract_multiplier")):
            return True
        if _contract_multiplier_is_option(breakout.get("contract_multiplier")):
            return True
    return False


def paper_trade_contract_multiplier(paper_trade: Any) -> float:
    if _signal_json_is_option(getattr(paper_trade, "signal_json", None)):
        return OPTION_CONTRACT_MULTIPLIER
    return 1.0


def paper_trade_realized_pnl(paper_trade: Any) -> float | None:
    return realized_pnl(
        paper_trade,
        contract_multiplier=paper_trade_contract_multiplier(paper_trade),
    )


def trade_realized_pnl(trade: Any) -> float | None:
    return realized_pnl(
        trade,
        contract_multiplier=trade_contract_multiplier(trade),
    )


def trade_return_pct(trade: Any) -> float | None:
    """Realized signed return for a live trade.

    Prefer recorded P&L because it already carries short-side sign, fill
    corrections, and option contract multiplier dollars. Local partial-close
    evidence is folded back in before the return is normalized. Fall back to
    price return only for older rows without P&L.
    """
    pnl_ret = realized_return_pct(
        trade,
        contract_multiplier=trade_contract_multiplier(trade),
    )
    if pnl_ret is not None:
        return pnl_ret
    if _partial_leg_declared(trade):
        return None
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
    pnl_ret = realized_return_pct(
        paper_trade,
        contract_multiplier=(
            OPTION_CONTRACT_MULTIPLIER if is_option else 1.0
        ),
    )
    if pnl_ret is not None:
        return pnl_ret
    if _partial_leg_declared(paper_trade):
        return None
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
