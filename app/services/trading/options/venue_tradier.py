"""Tradier REST venue adapter for the options lane.

Tradier was chosen over Tastytrade for the first cut because:
  - Sandbox is free + non-trivial chain depth
  - Pure REST (no WebSocket-first stack like Tasty)
  - Simpler order routing (single-leg + multi-leg supported uniformly)

This adapter handles:
  - Authentication (bearer token from CHILI_TRADIER_ACCESS_TOKEN)
  - Chain fetching (full chain or filtered by expiration)
  - Quote fetching (bid/ask/IV/greeks via Tradier's `/markets/options/strikes`
    + `/markets/options/chains` endpoints)
  - Order placement (paper-only by default, gated on CHILI_OPTIONS_LANE_LIVE)

Network failures degrade gracefully — return None / empty instead of
raising — so flag-OFF deployments without TRADIER credentials don't
break anything else.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_TRADIER_SANDBOX = "https://sandbox.tradier.com/v1"
_TRADIER_PRODUCTION = "https://api.tradier.com/v1"
_TIMEOUT_SEC = 10


def _base_url() -> str:
    """Return Tradier base URL based on env (sandbox by default)."""
    if os.environ.get("CHILI_TRADIER_LIVE", "").lower() in ("true", "1"):
        return _TRADIER_PRODUCTION
    return _TRADIER_SANDBOX


def _headers() -> Optional[dict]:
    token = os.environ.get("CHILI_TRADIER_ACCESS_TOKEN", "").strip()
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def fetch_expirations(underlying: str) -> list[str]:
    """Return list of expiration date strings (YYYY-MM-DD) for an underlying."""
    h = _headers()
    if not h:
        logger.debug("[tradier] no access token; expirations=[]")
        return []
    url = f"{_base_url()}/markets/options/expirations"
    try:
        r = requests.get(
            url, headers=h, params={"symbol": underlying.upper(), "includeAllRoots": "true"},
            timeout=_TIMEOUT_SEC,
        )
        r.raise_for_status()
        data = r.json() or {}
        exps = (data.get("expirations") or {}).get("date") or []
        if isinstance(exps, str):
            exps = [exps]
        return list(exps)
    except Exception as e:
        logger.debug("[tradier] expirations fetch failed for %s: %s", underlying, e)
        return []


def fetch_chain(
    underlying: str, expiration: str, *, with_greeks: bool = True
) -> list[dict]:
    """Return option contracts for ``underlying`` expiring on ``expiration``.

    Each dict contains: occ_symbol, strike, opt_type, bid, ask, last,
    volume, open_interest, implied_vol, delta, gamma, theta, vega.
    Empty list on auth/fetch failure.
    """
    h = _headers()
    if not h:
        return []
    url = f"{_base_url()}/markets/options/chains"
    try:
        r = requests.get(
            url, headers=h,
            params={
                "symbol": underlying.upper(),
                "expiration": expiration,
                "greeks": "true" if with_greeks else "false",
            },
            timeout=_TIMEOUT_SEC,
        )
        r.raise_for_status()
        data = r.json() or {}
        opts = (data.get("options") or {}).get("option") or []
        if isinstance(opts, dict):
            opts = [opts]
        result = []
        for o in opts:
            greeks = o.get("greeks") or {}
            result.append({
                "occ_symbol": o.get("symbol"),
                "underlying": o.get("underlying") or underlying.upper(),
                "expiration": o.get("expiration_date"),
                "strike": float(o.get("strike") or 0),
                "opt_type": (o.get("option_type") or "").lower(),
                "bid": o.get("bid"),
                "ask": o.get("ask"),
                "last": o.get("last"),
                "volume": o.get("volume"),
                "open_interest": o.get("open_interest"),
                "implied_vol": greeks.get("mid_iv") or greeks.get("smv_vol"),
                "delta": greeks.get("delta"),
                "gamma": greeks.get("gamma"),
                "theta": greeks.get("theta"),
                "vega": greeks.get("vega"),
                "rho": greeks.get("rho"),
            })
        return result
    except Exception as e:
        logger.debug("[tradier] chain fetch failed for %s/%s: %s",
                     underlying, expiration, e)
        return []


def place_order(
    *,
    occ_symbols_with_qty: list[tuple[str, int]],
    underlying: str,
    order_type: str = "market",      # 'market' | 'limit' | 'debit' | 'credit'
    duration: str = "day",
    price: Optional[float] = None,
    is_paper: bool = True,
) -> dict:
    """Submit order. Single-leg or multi-leg supported.

    Returns ``{ok, broker_order_id, error}``. Pure no-op when no
    credentials configured — returns ``{ok: False, error: 'no_credentials'}``.
    Paper sandbox orders go through the same code path; the URL changes
    based on ``CHILI_TRADIER_LIVE``.
    """
    if not occ_symbols_with_qty:
        return {"ok": False, "error": "no_legs"}
    h = _headers()
    if not h:
        return {"ok": False, "error": "no_credentials"}

    account_id = os.environ.get("CHILI_TRADIER_ACCOUNT_ID", "").strip()
    if not account_id:
        return {"ok": False, "error": "no_account_id"}

    url = f"{_base_url()}/accounts/{account_id}/orders"

    # Build payload depending on number of legs.
    payload = {
        "class": "multileg" if len(occ_symbols_with_qty) > 1 else "option",
        "symbol": underlying.upper(),
        "type": order_type,
        "duration": duration,
    }
    if price is not None and order_type in ("limit", "debit", "credit"):
        payload["price"] = price

    if len(occ_symbols_with_qty) == 1:
        sym, qty = occ_symbols_with_qty[0]
        payload["option_symbol"] = sym
        payload["side"] = "buy_to_open" if qty > 0 else "sell_to_open"
        payload["quantity"] = abs(qty)
    else:
        for i, (sym, qty) in enumerate(occ_symbols_with_qty):
            payload[f"option_symbol[{i}]"] = sym
            payload[f"side[{i}]"] = "buy_to_open" if qty > 0 else "sell_to_open"
            payload[f"quantity[{i}]"] = abs(qty)

    try:
        r = requests.post(url, headers={**h, "Content-Type": "application/x-www-form-urlencoded"},
                          data=payload, timeout=_TIMEOUT_SEC)
        r.raise_for_status()
        data = r.json() or {}
        order = data.get("order") or {}
        return {
            "ok": True,
            "broker_order_id": str(order.get("id") or ""),
            "status": order.get("status"),
            "is_paper": not (os.environ.get("CHILI_TRADIER_LIVE", "").lower() in ("true", "1")),
        }
    except Exception as e:
        logger.warning("[tradier] place_order failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}
