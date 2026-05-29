"""Canonical option contract metadata helpers.

The options lane receives metadata from several places: synthesis, manual
alerts, broker responses, and legacy nested trade snapshots. This module keeps
the contract identity and price-domain markers consistent without making a DB
schema migration a prerequisite for safer execution.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any, Mapping


OPTION_CONTRACT_MULTIPLIER: float = 100.0
PRICE_DOMAIN_OPTION_PREMIUM = "option_premium"
PRICE_DOMAIN_UNDERLYING_SPOT = "underlying_spot"
_QUOTE_SNAPSHOT_VOLATILE_KEYS = frozenset(
    ("bid", "ask", "mid", "mark", "spread_pct", "quote_ts")
)


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _positive_float_or_none(value: Any) -> float | None:
    out = _float_or_none(value)
    if out is None or out <= 0:
        return None
    return out


def _nonnegative_float_or_none(value: Any) -> float | None:
    out = _float_or_none(value)
    if out is None or out < 0:
        return None
    return out


def _positive_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    out = _positive_float_or_none(value)
    if out is None:
        return None
    if not float(out).is_integer():
        return None
    qty = int(out)
    return qty if qty >= 1 else None


def normalize_option_type(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if raw in {"c", "call", "calls"}:
        return "call"
    if raw in {"p", "put", "puts"}:
        return "put"
    return None


def normalize_expiration(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except Exception:
            continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return None


def expiration_is_expired(value: Any, *, as_of: date | None = None) -> bool | None:
    exp = normalize_expiration(value)
    if exp is None:
        return None
    try:
        exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
    except Exception:
        return None
    today = as_of or datetime.now(timezone.utc).date()
    return exp_date < today


def option_contract_key(
    *,
    underlying: Any,
    expiration: Any,
    strike: Any,
    option_type: Any,
) -> str | None:
    under = str(underlying or "").strip().upper()
    exp = normalize_expiration(expiration)
    strike_f = _positive_float_or_none(strike)
    opt_type = normalize_option_type(option_type)
    if not (under and exp and strike_f is not None and opt_type):
        return None
    return f"{under}:{exp}:{opt_type}:{strike_f:.3f}"


def occ_symbol(
    *,
    underlying: Any,
    expiration: Any,
    strike: Any,
    option_type: Any,
) -> str | None:
    """Return a compact OCC/OSI-style symbol, e.g. SPY260619C00729000."""
    under = str(underlying or "").strip().upper()
    exp = normalize_expiration(expiration)
    strike_f = _positive_float_or_none(strike)
    opt_type = normalize_option_type(option_type)
    if not (under and exp and strike_f is not None and opt_type):
        return None
    try:
        exp_dt = datetime.strptime(exp, "%Y-%m-%d").date()
    except Exception:
        return None
    cp = "C" if opt_type == "call" else "P"
    strike_code = int(round(strike_f * 1000.0))
    return f"{under}{exp_dt:%y%m%d}{cp}{strike_code:08d}"


def _quote_float(
    quote: Mapping[str, Any] | None,
    *keys: str,
    allow_zero: bool = False,
) -> float | None:
    if not isinstance(quote, Mapping):
        return None
    for key in keys:
        out = (
            _nonnegative_float_or_none(quote.get(key))
            if allow_zero
            else _positive_float_or_none(quote.get(key))
        )
        if out is not None:
            return out
    return None


def _quote_timestamp(quote: Mapping[str, Any] | None) -> Any | None:
    if not isinstance(quote, Mapping):
        return None
    for key in (
        "quote_ts",
        "timestamp",
        "updated_at",
        "last_updated_at",
        "last_trade_at",
    ):
        value = quote.get(key)
        if value:
            return value
    return None


def _quote_greek(quote: Mapping[str, Any] | None, key: str) -> float | None:
    if not isinstance(quote, Mapping):
        return None
    greeks = quote.get("greeks")
    if isinstance(greeks, Mapping):
        out = _float_or_none(greeks.get(key))
        if out is not None:
            return out
    return _float_or_none(quote.get(key))


def normalize_option_meta(
    meta: Mapping[str, Any] | None,
    *,
    underlying: Any | None = None,
    current_underlying_price: Any | None = None,
    quote: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a normalized copy of option metadata.

    The function is deliberately permissive: it preserves unknown keys but
    normalizes the keys that define contract identity and price domains.
    """
    src = dict(meta or {})
    under = str(src.get("underlying") or underlying or "").strip().upper()
    exp = normalize_expiration(src.get("expiration"))
    strike = _positive_float_or_none(src.get("strike"))
    opt_type = normalize_option_type(src.get("option_type") or src.get("type"))
    limit_price = _positive_float_or_none(src.get("limit_price"))
    quantity = _positive_int_or_none(src.get("quantity"))

    if under:
        src["underlying"] = under
    if exp:
        src["expiration"] = exp
    if strike is not None:
        src["strike"] = strike
    elif isinstance(src.get("strike"), bool):
        src.pop("strike", None)
    if opt_type:
        src["option_type"] = opt_type
    if limit_price is not None:
        src["limit_price"] = limit_price
    elif isinstance(src.get("limit_price"), bool):
        src.pop("limit_price", None)
    if quantity is not None:
        src["quantity"] = quantity
    elif isinstance(src.get("quantity"), bool):
        src.pop("quantity", None)

    key = option_contract_key(
        underlying=under,
        expiration=exp,
        strike=strike,
        option_type=opt_type,
    )
    occ = occ_symbol(
        underlying=under,
        expiration=exp,
        strike=strike,
        option_type=opt_type,
    )
    if key:
        src["contract_key"] = key
        src["option_contract_key"] = key
    else:
        src.pop("contract_key", None)
        src.pop("option_contract_key", None)
    if occ:
        src["occ_symbol"] = occ
    else:
        src.pop("occ_symbol", None)

    src["contract_multiplier"] = OPTION_CONTRACT_MULTIPLIER
    src["price_domain"] = PRICE_DOMAIN_OPTION_PREMIUM
    src["underlying_price_domain"] = PRICE_DOMAIN_UNDERLYING_SPOT

    underlying_px = _positive_float_or_none(current_underlying_price)
    if underlying_px is not None:
        src["underlying_price_at_entry"] = underlying_px

    bid = _quote_float(quote, "bid_price", "bid", allow_zero=True)
    ask = _quote_float(quote, "ask_price", "ask")
    mark = _quote_float(quote, "mark_price", "mark", "last_price")
    mid = _quote_float(quote, "mid_price", "mid")
    crossed_bbo = (
        bid is not None
        and ask is not None
        and bid > 0
        and ask > 0
        and bid > ask
    )
    if crossed_bbo:
        bid = ask = mark = mid = None
    if mid is None and bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
    quote_snapshot: dict[str, Any] = {}
    if bid is not None:
        quote_snapshot["bid"] = bid
    if ask is not None:
        quote_snapshot["ask"] = ask
    if mid is not None:
        quote_snapshot["mid"] = mid
    if mark is not None:
        quote_snapshot["mark"] = mark
    if bid is not None and ask is not None and mid and mid > 0:
        quote_snapshot["spread_pct"] = round((ask - bid) / mid * 100.0, 4)
    ts = _quote_timestamp(quote)
    if ts is not None:
        quote_snapshot["quote_ts"] = ts
    if quote_snapshot or crossed_bbo:
        existing_quote_snapshot = src.get("quote_snapshot")
        if not isinstance(existing_quote_snapshot, Mapping):
            existing_quote_snapshot = {}
        existing_quote_snapshot = {
            key: value
            for key, value in existing_quote_snapshot.items()
            if key not in _QUOTE_SNAPSHOT_VOLATILE_KEYS
        }
        merged_quote_snapshot = {**existing_quote_snapshot, **quote_snapshot}
        if merged_quote_snapshot:
            src["quote_snapshot"] = merged_quote_snapshot
        else:
            src.pop("quote_snapshot", None)

    for greek in ("delta", "gamma", "theta", "vega"):
        value = _quote_greek(quote, greek)
        if value is not None:
            src[greek] = value

    return src


def validate_single_leg_option_meta(meta: Mapping[str, Any] | None) -> list[str]:
    """Return validation problems for normalized single-leg option metadata."""
    src = normalize_option_meta(meta)
    missing: list[str] = []
    if not str(src.get("underlying") or "").strip():
        missing.append("underlying")
    exp = normalize_expiration(src.get("expiration"))
    if exp is None:
        missing.append("expiration")
    elif expiration_is_expired(exp):
        missing.append("expiration_expired")
    if _positive_float_or_none(src.get("strike")) is None:
        missing.append("strike")
    if normalize_option_type(src.get("option_type")) is None:
        missing.append("option_type")
    if not src.get("contract_key"):
        missing.append("contract_key")
    if _positive_float_or_none(src.get("limit_price")) is None:
        missing.append("limit_price")
    qty = _positive_int_or_none(src.get("quantity"))
    if qty is None:
        missing.append("quantity")
    return missing


def parse_contract_quantity(value: Any) -> int | None:
    """Return a positive whole-contract quantity, or None if invalid."""
    return _positive_int_or_none(value)


def finite_greek(value: Any) -> float | None:
    return _float_or_none(value)


def missing_greeks(meta: Mapping[str, Any] | None) -> list[str]:
    src = meta or {}
    missing: list[str] = []
    for greek in ("delta", "gamma", "theta", "vega"):
        value = src.get(greek)
        if value is None and isinstance(src.get("quote_snapshot"), Mapping):
            value = src["quote_snapshot"].get(greek)
        if finite_greek(value) is None:
            missing.append(greek)
    return missing


def complete_greeks(meta: Mapping[str, Any] | None) -> bool:
    return not missing_greeks(meta)


def option_price_domains_snapshot() -> dict[str, str]:
    return {
        "entry_price": PRICE_DOMAIN_OPTION_PREMIUM,
        "exit_price": PRICE_DOMAIN_OPTION_PREMIUM,
        "limit_price": PRICE_DOMAIN_OPTION_PREMIUM,
        "stop_loss": PRICE_DOMAIN_UNDERLYING_SPOT,
        "take_profit": PRICE_DOMAIN_UNDERLYING_SPOT,
        "current_price": PRICE_DOMAIN_UNDERLYING_SPOT,
    }


__all__ = [
    "OPTION_CONTRACT_MULTIPLIER",
    "PRICE_DOMAIN_OPTION_PREMIUM",
    "PRICE_DOMAIN_UNDERLYING_SPOT",
    "normalize_option_meta",
    "normalize_option_type",
    "normalize_expiration",
    "expiration_is_expired",
    "occ_symbol",
    "option_contract_key",
    "option_price_domains_snapshot",
    "parse_contract_quantity",
    "complete_greeks",
    "finite_greek",
    "missing_greeks",
    "validate_single_leg_option_meta",
]
