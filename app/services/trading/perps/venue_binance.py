"""Binance USDS-M futures REST adapter.

Public endpoints (no auth needed):
  - /fapi/v1/premiumIndex      : mark vs index, current funding rate
  - /fapi/v1/fundingRate       : historical funding rates
  - /fapi/v1/openInterest      : current open interest
  - /futures/data/openInterestHist : historical OI (rate-limited)

Auth-required (only if placing orders, future work):
  - /fapi/v1/order             : place / cancel orders
  - /fapi/v2/account           : account balance + positions

Network failures degrade gracefully (return empty / None).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BINANCE_FAPI = "https://fapi.binance.com"
_TIMEOUT_SEC = 10


def fetch_premium_index(symbol: Optional[str] = None) -> list[dict]:
    """Mark vs index price + current funding rate.

    If symbol is None, returns ALL contracts. Otherwise filters.
    """
    url = f"{_BINANCE_FAPI}/fapi/v1/premiumIndex"
    params = {"symbol": symbol.upper()} if symbol else {}
    try:
        r = requests.get(url, params=params, timeout=_TIMEOUT_SEC)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            data = [data]
        return [
            {
                "symbol": d.get("symbol"),
                "mark_price": float(d.get("markPrice") or 0),
                "index_price": float(d.get("indexPrice") or 0),
                "estimated_settle_price": float(d.get("estimatedSettlePrice") or 0),
                "last_funding_rate": float(d.get("lastFundingRate") or 0),
                "next_funding_time": d.get("nextFundingTime"),
                "interest_rate": float(d.get("interestRate") or 0),
                "ts": d.get("time"),
            }
            for d in data or []
        ]
    except Exception as e:
        logger.debug("[binance] premiumIndex fetch failed: %s", e)
        return []


def fetch_funding_history(
    symbol: str, limit: int = 100
) -> list[dict]:
    """Last N funding rates for a symbol (8h interval, returned newest first)."""
    url = f"{_BINANCE_FAPI}/fapi/v1/fundingRate"
    try:
        r = requests.get(
            url, params={"symbol": symbol.upper(), "limit": limit},
            timeout=_TIMEOUT_SEC,
        )
        r.raise_for_status()
        data = r.json() or []
        return [
            {
                "symbol": d.get("symbol"),
                "funding_time": d.get("fundingTime"),
                "funding_rate": float(d.get("fundingRate") or 0),
                "mark_at_funding": float(d.get("markPrice") or 0),
            }
            for d in data
        ]
    except Exception as e:
        logger.debug("[binance] fundingRate fetch failed for %s: %s", symbol, e)
        return []


def fetch_open_interest(symbol: str) -> Optional[dict]:
    """Current OI for a symbol."""
    url = f"{_BINANCE_FAPI}/fapi/v1/openInterest"
    try:
        r = requests.get(
            url, params={"symbol": symbol.upper()}, timeout=_TIMEOUT_SEC,
        )
        r.raise_for_status()
        d = r.json() or {}
        return {
            "symbol": d.get("symbol"),
            "open_interest": float(d.get("openInterest") or 0),
            "ts": d.get("time"),
        }
    except Exception as e:
        logger.debug("[binance] openInterest fetch failed for %s: %s", symbol, e)
        return None


def fetch_open_interest_history(
    symbol: str, period: str = "1h", limit: int = 100
) -> list[dict]:
    """Historical OI for trend analysis. period: 5m / 15m / 30m / 1h / 4h / 1d."""
    url = f"{_BINANCE_FAPI}/futures/data/openInterestHist"
    try:
        r = requests.get(
            url, params={
                "symbol": symbol.upper(),
                "period": period,
                "limit": limit,
            },
            timeout=_TIMEOUT_SEC,
        )
        r.raise_for_status()
        data = r.json() or []
        return [
            {
                "symbol": d.get("symbol"),
                "ts": d.get("timestamp"),
                "open_interest": float(d.get("sumOpenInterest") or 0),
                "open_interest_usd": float(d.get("sumOpenInterestValue") or 0),
            }
            for d in data
        ]
    except Exception as e:
        logger.debug("[binance] openInterestHist fetch failed for %s: %s", symbol, e)
        return []
