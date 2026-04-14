"""Unified broker dispatch layer.

Routes calls to the correct broker (Robinhood, Coinbase, MetaMask/Web3)
based on asset type or explicit user preference.  Provides combined
portfolio/positions views and smart order routing.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from . import broker_service
from . import coinbase_service

logger = logging.getLogger(__name__)

BROKER_ROBINHOOD = "robinhood"
BROKER_COINBASE = "coinbase"
BROKER_METAMASK = "metamask"
BROKER_MANUAL = "manual"


# ── Status aggregation ───────────────────────────────────────────────

def get_all_broker_statuses() -> dict[str, Any]:
    """Return connection status for every supported broker."""
    rh = broker_service.get_connection_status()
    cb = coinbase_service.get_connection_status()

    return {
        "robinhood": {
            **rh,
            "broker": BROKER_ROBINHOOD,
            "label": "Robinhood",
        },
        "coinbase": {
            **cb,
            "broker": BROKER_COINBASE,
            "label": "Coinbase Advanced",
        },
        "metamask": {
            "broker": BROKER_METAMASK,
            "label": "MetaMask",
            "configured": True,
            "connected": False,
            "note": "Client-side wallet — connect from the browser",
        },
    }


# ── Smart routing ────────────────────────────────────────────────────

def _is_crypto(ticker: str) -> bool:
    t = (ticker or "").upper()
    return t.endswith("-USD") and not t.replace("-USD", "").isdigit()


def get_best_broker_for(ticker: str) -> str:
    """Pick the best connected broker for a given ticker.

    Crypto tickers prefer Coinbase, then Robinhood.
    Stock tickers prefer Robinhood.
    Falls back to 'manual' if nothing is connected.
    """
    if _is_crypto(ticker):
        if coinbase_service.is_connected():
            return BROKER_COINBASE
        if broker_service.is_connected():
            return BROKER_ROBINHOOD
    else:
        if broker_service.is_connected():
            return BROKER_ROBINHOOD
    return BROKER_MANUAL


def get_available_brokers_for(ticker: str) -> list[dict[str, Any]]:
    """Return all brokers that *could* handle this ticker, with connection state."""
    brokers = []
    if _is_crypto(ticker):
        brokers.append({
            "broker": BROKER_COINBASE,
            "label": "Coinbase Advanced",
            "connected": coinbase_service.is_connected(),
            "preferred": True,
        })
        brokers.append({
            "broker": BROKER_ROBINHOOD,
            "label": "Robinhood",
            "connected": broker_service.is_connected(),
            "preferred": False,
        })
    else:
        brokers.append({
            "broker": BROKER_ROBINHOOD,
            "label": "Robinhood",
            "connected": broker_service.is_connected(),
            "preferred": True,
        })
    brokers.append({
        "broker": BROKER_MANUAL,
        "label": "Manual (paper)",
        "connected": True,
        "preferred": False,
    })
    return brokers


# ── Order dispatch ───────────────────────────────────────────────────

def place_buy_order(
    ticker: str,
    quantity: float,
    order_type: str = "market",
    limit_price: float | None = None,
    broker: str | None = None,
) -> dict[str, Any]:
    """Place a buy order via the specified (or auto-selected) broker."""
    target = broker or get_best_broker_for(ticker)

    if target == BROKER_COINBASE:
        result = coinbase_service.place_buy_order(ticker, quantity, order_type, limit_price)
        result["broker"] = BROKER_COINBASE
        return result

    if target == BROKER_ROBINHOOD:
        result = broker_service.place_buy_order(ticker, quantity, order_type, limit_price)
        result["broker"] = BROKER_ROBINHOOD
        return result

    return {"ok": False, "error": "No connected broker available", "broker": BROKER_MANUAL}


def place_sell_order(
    ticker: str,
    quantity: float,
    order_type: str = "market",
    limit_price: float | None = None,
    broker: str | None = None,
) -> dict[str, Any]:
    """Place a sell order via the specified (or auto-selected) broker."""
    target = broker or get_best_broker_for(ticker)

    if target == BROKER_COINBASE:
        result = coinbase_service.place_sell_order(ticker, quantity, order_type, limit_price)
        result["broker"] = BROKER_COINBASE
        return result

    if target == BROKER_ROBINHOOD:
        result = broker_service.place_sell_order(ticker, quantity, order_type, limit_price)
        result["broker"] = BROKER_ROBINHOOD
        return result

    return {"ok": False, "error": "No connected broker available", "broker": BROKER_MANUAL}


def is_any_connected() -> bool:
    return broker_service.is_connected() or coinbase_service.is_connected()


def map_status(broker: str, raw_state: str) -> str:
    if broker == BROKER_COINBASE:
        return coinbase_service.map_cb_status(raw_state)
    return broker_service.map_rh_status(raw_state)


# ── Combined portfolio / positions ───────────────────────────────────

def get_combined_portfolio() -> dict[str, Any]:
    """Merge portfolio data from all connected brokers."""
    result: dict[str, Any] = {
        "total_equity": 0.0,
        "total_buying_power": 0.0,
        "total_cash": 0.0,
        "brokers": {},
    }

    if broker_service.is_connected():
        rh = broker_service.get_portfolio()
        if rh:
            result["brokers"]["robinhood"] = rh
            result["total_equity"] += rh.get("equity", 0)
            result["total_buying_power"] += rh.get("buying_power", 0)
            result["total_cash"] += rh.get("cash", 0)

    if coinbase_service.is_connected():
        cb = coinbase_service.get_portfolio()
        if cb:
            result["brokers"]["coinbase"] = cb
            result["total_equity"] += cb.get("equity", 0)
            result["total_buying_power"] += cb.get("buying_power", 0)
            result["total_cash"] += cb.get("cash", 0)

    result["total_equity"] = round(result["total_equity"], 2)
    result["total_buying_power"] = round(result["total_buying_power"], 2)
    result["total_cash"] = round(result["total_cash"], 2)
    return result


def get_combined_positions() -> list[dict[str, Any]]:
    """Merge positions from all connected brokers, tagged with broker_source."""
    positions: list[dict[str, Any]] = []

    if broker_service.is_connected():
        for p in broker_service.get_positions():
            p["broker_source"] = BROKER_ROBINHOOD
            positions.append(p)
        for p in broker_service.get_crypto_positions():
            p["broker_source"] = BROKER_ROBINHOOD
            positions.append(p)

    if coinbase_service.is_connected():
        for p in coinbase_service.get_positions():
            p["broker_source"] = BROKER_COINBASE
            positions.append(p)

    positions.sort(key=lambda p: p.get("equity", 0) or 0, reverse=True)
    return positions


def check_duplicate_position(ticker: str) -> list[str]:
    """Return broker names where a position in *ticker* already exists."""
    dupes = []
    t = ticker.upper()
    for p in get_combined_positions():
        if p.get("ticker", "").upper() == t and p.get("quantity", 0) > 0:
            dupes.append(p.get("broker_source", "unknown"))
    return dupes


# ── Sync all ─────────────────────────────────────────────────────────

def sync_all(db: Session, user_id: int | None) -> dict[str, Any]:
    """Sync orders and positions from all connected brokers."""
    result: dict[str, Any] = {}

    if broker_service.is_connected():
        result["robinhood_orders"] = broker_service.sync_orders_to_db(db, user_id)
        pos_result = broker_service.sync_positions_to_db(db, user_id)
        live_tickers = pos_result.pop("_live_tickers", set())
        result["robinhood_positions"] = pos_result
        result["robinhood_manual"] = broker_service.cleanup_manual_trades(db, user_id, live_tickers)
        result["robinhood_backfill"] = broker_service.backfill_closed_trade_pnl(db, user_id)

    if coinbase_service.is_connected():
        result["coinbase_orders"] = coinbase_service.sync_orders_to_db(db, user_id)
        result["coinbase_positions"] = coinbase_service.sync_positions_to_db(db, user_id)

    return result


def connect_broker(broker: str, credentials: dict[str, Any] | None = None) -> dict[str, Any]:
    """Connect to a specific broker, optionally using per-user credentials."""
    if broker == BROKER_ROBINHOOD:
        if credentials:
            return broker_service.login_with_credentials(
                username=credentials.get("username", ""),
                password=credentials.get("password", ""),
                totp_secret=credentials.get("totp_secret"),
            )
        return broker_service.login_step1_sms()
    if broker == BROKER_COINBASE:
        if credentials:
            return coinbase_service.connect_with_credentials(
                api_key=credentials.get("api_key", ""),
                api_secret=credentials.get("api_secret", ""),
            )
        return coinbase_service.connect()
    return {"status": "error", "message": f"Unknown broker: {broker}"}


def build_combined_portfolio_context() -> str:
    """Build text summary of all broker portfolios for AI context."""
    parts = []
    rh_ctx = broker_service.build_portfolio_context()
    if rh_ctx:
        parts.append(rh_ctx)
    cb_ctx = coinbase_service.build_portfolio_context()
    if cb_ctx:
        parts.append(cb_ctx)
    return "\n\n".join(parts)


# ── Per-User Broker Session Isolation ─────────────────────────────────

import threading
import time

_user_sessions: dict[int, dict[str, Any]] = {}
_session_lock = threading.Lock()
_SESSION_TTL = 3600  # 1 hour


class UserBrokerSession:
    """Isolated broker session state per user.

    Prevents cross-user session bleed by maintaining separate cache,
    connection state, and credential references per user_id.
    """

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.created_at = time.time()
        self.last_active = time.time()
        self.connections: dict[str, bool] = {}
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_ttl = 300

    def is_expired(self) -> bool:
        return (time.time() - self.last_active) > _SESSION_TTL

    def touch(self) -> None:
        self.last_active = time.time()

    def cache_get(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry and (time.time() - entry[0]) < self._cache_ttl:
            return entry[1]
        return None

    def cache_set(self, key: str, value: Any) -> None:
        self._cache[key] = (time.time(), value)

    def mark_connected(self, broker: str) -> None:
        self.connections[broker] = True
        self.touch()

    def mark_disconnected(self, broker: str) -> None:
        self.connections[broker] = False
        self._cache.clear()

    def is_connected(self, broker: str) -> bool:
        return self.connections.get(broker, False)


def get_user_session(user_id: int) -> UserBrokerSession:
    """Get or create an isolated broker session for a user."""
    with _session_lock:
        session = _user_sessions.get(user_id)
        if session and not session.is_expired():
            session.touch()
            return session
        session = UserBrokerSession(user_id)
        _user_sessions[user_id] = session
        return session


def cleanup_expired_sessions() -> int:
    """Remove expired sessions. Call periodically from scheduler."""
    with _session_lock:
        expired = [uid for uid, s in _user_sessions.items() if s.is_expired()]
        for uid in expired:
            del _user_sessions[uid]
        return len(expired)


def get_active_session_count() -> int:
    with _session_lock:
        return sum(1 for s in _user_sessions.values() if not s.is_expired())
