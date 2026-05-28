"""Best-effort persistence for option chain and quote snapshots."""
from __future__ import annotations

import json
import logging
import math
from contextlib import nullcontext
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

from .contracts import normalize_expiration, normalize_option_meta

logger = logging.getLogger(__name__)


def _float_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _int_or_none(value: Any) -> int | None:
    try:
        out = int(float(value))
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _quote_float(quote: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float_or_none(quote.get(key))
        if value is not None:
            return value
        greeks = quote.get("greeks")
        if isinstance(greeks, Mapping):
            value = _float_or_none(greeks.get(key))
            if value is not None:
                return value
    return None


def _quote_int(quote: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = _int_or_none(quote.get(key))
        if value is not None:
            return value
    return None


def _premium_quote_is_persistable(quote: Mapping[str, Any]) -> bool:
    bid = _quote_float(quote, "bid_price", "bid")
    ask = _quote_float(quote, "ask_price", "ask")
    last = _quote_float(
        quote,
        "last_trade_price",
        "last_price",
        "mark_price",
        "adjusted_mark_price",
        "mark",
    )
    prices = (bid, ask, last)
    if any(value is not None and value < 0 for value in prices):
        return False
    if bid is not None and ask is not None and bid > 0 and ask > 0 and bid > ask:
        return False
    return any(value is not None and value > 0 for value in prices)


def _best_effort_write_scope(db: Session):
    begin_nested = getattr(db, "begin_nested", None)
    if callable(begin_nested):
        return begin_nested()
    return nullcontext()


def create_chain_snapshot(
    db: Session | None,
    *,
    underlying: str,
    expiration: str | None,
    venue: str,
    spot_price: float | None,
    n_contracts: int | None,
) -> int | None:
    """Insert a lightweight chain snapshot row and return its id.

    The write is deliberately best-effort. Missing tables, read-only test DBs,
    or transient database failures must not block option selection.
    """
    if db is None:
        return None
    sym = (underlying or "").strip().upper()
    if not sym:
        return None
    exp = normalize_expiration(expiration)
    try:
        with _best_effort_write_scope(db):
            row = db.execute(
                text(
                    """
                    INSERT INTO options_chains (
                        underlying, venue, expirations_json, n_contracts, spot_price
                    ) VALUES (
                        :underlying, :venue, CAST(:expirations_json AS JSONB),
                        :n_contracts, :spot_price
                    )
                    RETURNING id
                    """
                ),
                {
                    "underlying": sym,
                    "venue": (venue or "unknown").strip().lower(),
                    "expirations_json": json.dumps([exp] if exp else []),
                    "n_contracts": n_contracts,
                    "spot_price": spot_price,
                },
            ).first()
        return int(row[0]) if row else None
    except Exception as exc:
        logger.debug("[options.quote_store] chain snapshot write failed: %s", exc)
        return None


def record_quote_snapshot(
    db: Session | None,
    *,
    chain_id: int | None,
    option_meta: Mapping[str, Any],
    quote: Mapping[str, Any],
) -> bool:
    """Append one option quote snapshot.

    Returns True only when the row was written. The caller should treat False
    as telemetry loss, not as a trading signal.
    """
    if db is None or not isinstance(quote, Mapping):
        return False
    if not _premium_quote_is_persistable(quote):
        return False
    meta = normalize_option_meta(option_meta, quote=quote)
    required = (
        meta.get("occ_symbol"),
        meta.get("underlying"),
        meta.get("expiration"),
        meta.get("strike"),
        meta.get("option_type"),
    )
    if not all(required):
        return False
    try:
        with _best_effort_write_scope(db):
            db.execute(
                text(
                    """
                    INSERT INTO options_quotes (
                        chain_id, occ_symbol, underlying, expiration, strike,
                        opt_type, bid, ask, last, volume, open_interest,
                        implied_vol, delta, gamma, theta, vega, rho
                    ) VALUES (
                        :chain_id, :occ_symbol, :underlying, :expiration, :strike,
                        :opt_type, :bid, :ask, :last, :volume, :open_interest,
                        :implied_vol, :delta, :gamma, :theta, :vega, :rho
                    )
                    """
                ),
                {
                    "chain_id": chain_id,
                    "occ_symbol": meta.get("occ_symbol"),
                    "underlying": meta.get("underlying"),
                    "expiration": meta.get("expiration"),
                    "strike": meta.get("strike"),
                    "opt_type": meta.get("option_type"),
                    "bid": _quote_float(quote, "bid_price", "bid"),
                    "ask": _quote_float(quote, "ask_price", "ask"),
                    "last": _quote_float(
                        quote,
                        "last_trade_price",
                        "last_price",
                        "mark_price",
                        "adjusted_mark_price",
                        "mark",
                    ),
                    "volume": _quote_int(quote, "volume"),
                    "open_interest": _quote_int(quote, "open_interest"),
                    "implied_vol": _quote_float(
                        quote,
                        "implied_volatility",
                        "implied_vol",
                        "iv",
                    ),
                    "delta": _quote_float(quote, "delta"),
                    "gamma": _quote_float(quote, "gamma"),
                    "theta": _quote_float(quote, "theta"),
                    "vega": _quote_float(quote, "vega"),
                    "rho": _quote_float(quote, "rho"),
                },
            )
        return True
    except Exception as exc:
        logger.debug("[options.quote_store] quote snapshot write failed: %s", exc)
        return False


__all__ = ["create_chain_snapshot", "record_quote_snapshot"]
