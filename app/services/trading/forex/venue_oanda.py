"""OANDA REST venue adapter for the forex lane.

OANDA's v20 API is the retail FX leader for programmatic access. Three
endpoint groups we use:

  - /accounts/{id}/pricing       — bid/ask streaming (HTTP long-poll OK)
  - /instruments/{pair}/candles  — historical OHLC for backtests
  - /accounts/{id}/orders        — market/limit order placement

Auth: bearer token from CHILI_OANDA_ACCESS_TOKEN env. Sandbox vs live
controlled by CHILI_OANDA_LIVE.

Network failures degrade gracefully — return empty / None instead of
raising — so flag-OFF deployments without OANDA credentials don't break
anything else.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_OANDA_PRACTICE = "https://api-fxpractice.oanda.com/v3"
_OANDA_LIVE = "https://api-fxtrade.oanda.com/v3"
_TIMEOUT_SEC = 10


def _base_url() -> str:
    if os.environ.get("CHILI_OANDA_LIVE", "").lower() in ("true", "1"):
        return _OANDA_LIVE
    return _OANDA_PRACTICE


def _headers() -> Optional[dict]:
    token = os.environ.get("CHILI_OANDA_ACCESS_TOKEN", "").strip()
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _account_id() -> Optional[str]:
    return os.environ.get("CHILI_OANDA_ACCOUNT_ID", "").strip() or None


def fetch_pricing(pairs: list[str]) -> list[dict]:
    """Bid/ask snapshot for a list of OANDA-formatted pairs (e.g. 'EUR_USD').

    Empty list on auth/fetch failure.
    """
    h, acct = _headers(), _account_id()
    if not h or not acct:
        return []
    url = f"{_base_url()}/accounts/{acct}/pricing"
    try:
        r = requests.get(
            url, headers=h,
            params={"instruments": ",".join(p.upper() for p in pairs)},
            timeout=_TIMEOUT_SEC,
        )
        r.raise_for_status()
        data = r.json() or {}
        prices = data.get("prices") or []
        return [
            {
                "pair": p.get("instrument"),
                "ts": p.get("time"),
                "bid": float(p.get("bids", [{}])[0].get("price") or 0),
                "ask": float(p.get("asks", [{}])[0].get("price") or 0),
                "spread": float(p.get("asks", [{}])[0].get("price") or 0)
                          - float(p.get("bids", [{}])[0].get("price") or 0),
                "tradeable": bool(p.get("tradeable")),
            }
            for p in prices
        ]
    except Exception as e:
        logger.debug("[oanda] pricing fetch failed: %s", e)
        return []


def fetch_candles(
    pair: str,
    granularity: str = "M5",
    count: int = 500,
) -> list[dict]:
    """Historical OHLC for backtesting / signal computation.

    granularity: M1 / M5 / M15 / H1 / H4 / D / etc.
    """
    h = _headers()
    if not h:
        return []
    url = f"{_base_url()}/instruments/{pair.upper()}/candles"
    try:
        r = requests.get(
            url, headers=h,
            params={"granularity": granularity, "count": count, "price": "M"},
            timeout=_TIMEOUT_SEC,
        )
        r.raise_for_status()
        data = r.json() or {}
        candles = data.get("candles") or []
        out = []
        for c in candles:
            mid = c.get("mid") or {}
            out.append({
                "time": c.get("time"),
                "complete": c.get("complete"),
                "volume": c.get("volume"),
                "open": float(mid.get("o") or 0),
                "high": float(mid.get("h") or 0),
                "low": float(mid.get("l") or 0),
                "close": float(mid.get("c") or 0),
            })
        return out
    except Exception as e:
        logger.debug("[oanda] candles fetch failed for %s: %s", pair, e)
        return []


def place_order(
    *,
    pair: str,
    units: int,                # +N long, -N short
    order_type: str = "MARKET",
    stop_loss_price: Optional[float] = None,
    take_profit_price: Optional[float] = None,
) -> dict:
    """Submit an FX order. Paper sandbox or live based on env.

    Returns ``{ok, broker_order_id, error}``.
    """
    h, acct = _headers(), _account_id()
    if not h:
        return {"ok": False, "error": "no_credentials"}
    if not acct:
        return {"ok": False, "error": "no_account_id"}

    payload: dict = {
        "order": {
            "type": order_type,
            "instrument": pair.upper(),
            "units": str(units),
            "timeInForce": "FOK" if order_type == "MARKET" else "GTC",
            "positionFill": "DEFAULT",
        }
    }
    if stop_loss_price is not None:
        payload["order"]["stopLossOnFill"] = {
            "price": f"{stop_loss_price:.5f}",
            "timeInForce": "GTC",
        }
    if take_profit_price is not None:
        payload["order"]["takeProfitOnFill"] = {
            "price": f"{take_profit_price:.5f}",
            "timeInForce": "GTC",
        }

    url = f"{_base_url()}/accounts/{acct}/orders"
    try:
        r = requests.post(url, headers=h, json=payload, timeout=_TIMEOUT_SEC)
        r.raise_for_status()
        data = r.json() or {}
        fill = data.get("orderFillTransaction") or {}
        return {
            "ok": True,
            "broker_order_id": str(fill.get("id") or ""),
            "fill_price": fill.get("price"),
            "is_paper": not (
                os.environ.get("CHILI_OANDA_LIVE", "").lower() in ("true", "1")
            ),
        }
    except Exception as e:
        logger.warning("[oanda] place_order failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}
