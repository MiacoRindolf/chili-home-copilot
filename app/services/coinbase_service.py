"""Coinbase Advanced Trade integration via coinbase-advanced-py SDK.

Mirrors the broker_service.py interface so the broker_manager can dispatch
to either Robinhood or Coinbase transparently.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ..config import settings

logger = logging.getLogger(__name__)

_cb_available = True
try:
    from coinbase.rest import RESTClient  # noqa: F401
except ImportError:
    _cb_available = False
    logger.info("[coinbase] coinbase-advanced-py not installed — Coinbase integration disabled")

_client: Any | None = None
_connected = False
_last_check: float = 0
_CHECK_TTL = 600

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 300


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.time(), value)


def _credentials_configured() -> bool:
    return bool(settings.coinbase_api_key and settings.coinbase_api_secret)


def coinbase_sdk_and_credentials_configured() -> bool:
    """True if ``coinbase-advanced-py`` is installed and env/keys are set (no live ping)."""
    return bool(_cb_available and _credentials_configured())


def get_coinbase_rest_client() -> Any | None:
    """Authenticated Advanced Trade REST client, or ``None`` (used by ``CoinbaseSpotAdapter``)."""
    return _get_client()


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not _cb_available or not _credentials_configured():
        return None
    try:
        from coinbase.rest import RESTClient as CB
        secret = settings.coinbase_api_secret.replace("\\n", "\n")
        _client = CB(api_key=settings.coinbase_api_key, api_secret=secret)
        return _client
    except Exception as e:
        logger.error(f"[coinbase] Failed to create client: {e}")
        return None


# ── Connection ────────────────────────────────────────────────────────

def connect() -> dict[str, Any]:
    """Validate credentials by fetching accounts."""
    global _connected, _last_check
    client = _get_client()
    if not client:
        if not _cb_available:
            return {"status": "error", "message": "Coinbase SDK not installed. Ask admin to run: pip install coinbase-advanced-py"}
        return {
            "status": "needs_credentials",
            "message": "Click to set up your Coinbase Advanced API keys.",
        }
    try:
        resp = client.get_accounts(limit=1)
        accounts = resp.get("accounts", []) if isinstance(resp, dict) else getattr(resp, "accounts", [])
        if accounts is not None:
            _connected = True
            _last_check = time.time()
            return {"status": "connected", "message": "Connected to Coinbase Advanced"}
        return {"status": "error", "message": "Could not verify Coinbase credentials"}
    except Exception as e:
        _connected = False
        logger.error(f"[coinbase] Connect failed: {e}")
        return {"status": "error", "message": f"Connection failed: {e}"}


def connect_with_credentials(api_key: str, api_secret: str) -> dict[str, Any]:
    """Connect using explicitly provided credentials (from DB vault)."""
    global _client, _connected, _last_check
    if not _cb_available:
        return {"status": "error", "message": "Coinbase SDK not installed. Run: pip install coinbase-advanced-py"}
    if not api_key or not api_secret:
        return {"status": "error", "message": "API Key and API Secret are required"}
    try:
        from coinbase.rest import RESTClient as CB
        secret = api_secret.replace("\\n", "\n")
        client = CB(api_key=api_key, api_secret=secret)
        resp = client.get_accounts(limit=1)
        accounts = resp.get("accounts", []) if isinstance(resp, dict) else getattr(resp, "accounts", [])
        if accounts is not None:
            _client = client
            _connected = True
            _last_check = time.time()
            return {"status": "connected", "message": "Connected to Coinbase Advanced"}
        return {"status": "error", "message": "Could not verify Coinbase credentials"}
    except Exception as e:
        _connected = False
        logger.error(f"[coinbase] Connect with credentials failed: {e}")
        return {"status": "error", "message": f"Connection failed: {e}"}


def is_connected() -> bool:
    global _connected, _last_check
    if not _cb_available or not _credentials_configured():
        return False
    if _connected and (time.time() - _last_check) < _CHECK_TTL:
        return True
    result = connect()
    return result.get("status") == "connected"


def get_connection_status() -> dict[str, Any]:
    configured = _credentials_configured()
    connected = _connected and (time.time() - _last_check) < _CHECK_TTL
    return {
        "configured": configured,
        "connected": connected,
        "cb_available": _cb_available,
        "api_key_set": bool(settings.coinbase_api_key),
    }


# ── Data fetching ────────────────────────────────────────────────────

def get_accounts_raw() -> list[dict]:
    """Fetch all Coinbase accounts (wallets). Cached."""
    cached = _cache_get("accounts")
    if cached is not None:
        return cached
    client = _get_client()
    if not client or not is_connected():
        return []
    try:
        resp = client.get_accounts(limit=250)
        accounts = resp.get("accounts", []) if isinstance(resp, dict) else getattr(resp, "accounts", [])
        result = []
        for a in (accounts or []):
            acc = a if isinstance(a, dict) else a.__dict__ if hasattr(a, "__dict__") else {}
            result.append(acc)
        _cache_set("accounts", result)
        return result
    except Exception as e:
        logger.error(f"[coinbase] get_accounts failed: {e}")
        return []


def get_portfolio() -> dict[str, Any]:
    """Account balances: total value and available cash."""
    cached = _cache_get("portfolio")
    if cached is not None:
        return cached
    accounts = get_accounts_raw()
    if not accounts:
        return {}
    total_value = 0.0
    available_cash = 0.0
    for acc in accounts:
        bal = acc.get("available_balance", {})
        val = _safe_float(bal.get("value") if isinstance(bal, dict) else getattr(bal, "value", 0))
        currency = bal.get("currency") if isinstance(bal, dict) else getattr(bal, "currency", "")
        if currency == "USD":
            available_cash += val
        total_value += val
    result = {
        "equity": round(total_value, 2),
        "buying_power": round(available_cash, 2),
        "cash": round(available_cash, 2),
        "last_updated": datetime.utcnow().isoformat(),
    }
    _cache_set("portfolio", result)
    return result


def get_positions() -> list[dict[str, Any]]:
    """Current crypto holdings with non-zero balances."""
    cached = _cache_get("positions")
    if cached is not None:
        return cached
    accounts = get_accounts_raw()
    if not accounts:
        return []
    positions = []
    for acc in accounts:
        bal = acc.get("available_balance", {})
        val = _safe_float(bal.get("value") if isinstance(bal, dict) else getattr(bal, "value", 0))
        currency = bal.get("currency") if isinstance(bal, dict) else getattr(bal, "currency", "")
        if val <= 0 or currency == "USD":
            continue
        hold_bal = acc.get("hold", {})
        hold_val = _safe_float(hold_bal.get("value") if isinstance(hold_bal, dict) else getattr(hold_bal, "value", 0))
        total_qty = val + hold_val
        positions.append({
            "ticker": f"{currency}-USD",
            "quantity": total_qty,
            "average_buy_price": 0,
            "equity": 0,
            "current_price": 0,
            "name": acc.get("name", currency),
            "type": "crypto",
            "broker_source": "coinbase",
        })
    positions.sort(key=lambda p: p.get("quantity", 0), reverse=True)
    _cache_set("positions", positions)
    return positions


def get_recent_orders(limit: int = 20) -> list[dict[str, Any]]:
    """Recent order history."""
    cached = _cache_get("recent_orders")
    if cached is not None:
        return cached[:limit]
    client = _get_client()
    if not client or not is_connected():
        return []
    try:
        resp = client.list_orders(limit=limit)
        raw_orders = resp.get("orders", []) if isinstance(resp, dict) else getattr(resp, "orders", [])
        orders = []
        for o in (raw_orders or [])[:limit]:
            od = o if isinstance(o, dict) else o.__dict__ if hasattr(o, "__dict__") else {}
            orders.append({
                "id": od.get("order_id", ""),
                "product_id": od.get("product_id", ""),
                "ticker": (od.get("product_id", "") or "").replace("-USD", "") + "-USD" if od.get("product_id") else "",
                "side": od.get("side", ""),
                "quantity": _safe_float(od.get("filled_size") or od.get("base_size", 0)),
                "price": _safe_float(od.get("average_filled_price", 0)),
                "state": od.get("status", ""),
                "created_at": od.get("created_time", ""),
            })
        _cache_set("recent_orders", orders)
        return orders[:limit]
    except Exception as e:
        logger.error(f"[coinbase] get_recent_orders failed: {e}")
        return []


# ── Order Placement ──────────────────────────────────────────────────

def _to_product_id(ticker: str) -> str:
    """Convert ticker like 'BTC-USD' or 'BTC' to Coinbase product_id 'BTC-USD'."""
    t = ticker.upper().strip()
    if not t.endswith("-USD"):
        t = t + "-USD"
    return t


def place_buy_order(
    ticker: str,
    quantity: float,
    order_type: str = "market",
    limit_price: float | None = None,
) -> dict[str, Any]:
    client = _get_client()
    if not client:
        return {"ok": False, "error": "Coinbase client not available"}
    if not is_connected():
        return {"ok": False, "error": "Not connected to Coinbase"}

    product_id = _to_product_id(ticker)
    client_order_id = str(uuid.uuid4())

    try:
        if order_type == "limit" and limit_price:
            resp = client.limit_order_gtc_buy(
                client_order_id=client_order_id,
                product_id=product_id,
                base_size=str(round(quantity, 8)),
                limit_price=str(round(limit_price, 2)),
            )
        else:
            resp = client.market_order_buy(
                client_order_id=client_order_id,
                product_id=product_id,
                base_size=str(round(quantity, 8)),
            )

        rd = resp if isinstance(resp, dict) else resp.__dict__ if hasattr(resp, "__dict__") else {}
        success = rd.get("success", False)
        success_resp = rd.get("success_response", {})
        error_resp = rd.get("error_response", {})

        if isinstance(success_resp, str):
            success_resp = {}
        if isinstance(error_resp, str):
            error_resp = {}

        if success or (success_resp and success_resp.get("order_id")):
            sr = success_resp if isinstance(success_resp, dict) else getattr(success_resp, "__dict__", {})
            order_id = sr.get("order_id", client_order_id)
            logger.info(f"[coinbase] BUY order placed: {product_id} x{quantity} ({order_type}) → {order_id}")
            _cache.pop("positions", None)
            _cache.pop("portfolio", None)
            _cache.pop("accounts", None)
            return {"ok": True, "order_id": order_id, "state": "pending", "raw": rd}
        else:
            er = error_resp if isinstance(error_resp, dict) else getattr(error_resp, "__dict__", {})
            msg = er.get("message", "") or er.get("error", "") or str(rd)
            logger.error(f"[coinbase] BUY order failed for {product_id}: {msg}")
            return {"ok": False, "error": msg}

    except Exception as e:
        logger.error(f"[coinbase] BUY order exception for {product_id}: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


def place_sell_order(
    ticker: str,
    quantity: float,
    order_type: str = "market",
    limit_price: float | None = None,
) -> dict[str, Any]:
    client = _get_client()
    if not client:
        return {"ok": False, "error": "Coinbase client not available"}
    if not is_connected():
        return {"ok": False, "error": "Not connected to Coinbase"}

    product_id = _to_product_id(ticker)
    client_order_id = str(uuid.uuid4())

    try:
        if order_type == "limit" and limit_price:
            resp = client.limit_order_gtc_sell(
                client_order_id=client_order_id,
                product_id=product_id,
                base_size=str(round(quantity, 8)),
                limit_price=str(round(limit_price, 2)),
            )
        else:
            resp = client.market_order_sell(
                client_order_id=client_order_id,
                product_id=product_id,
                base_size=str(round(quantity, 8)),
            )

        rd = resp if isinstance(resp, dict) else resp.__dict__ if hasattr(resp, "__dict__") else {}
        success = rd.get("success", False)
        success_resp = rd.get("success_response", {})
        error_resp = rd.get("error_response", {})

        if isinstance(success_resp, str):
            success_resp = {}
        if isinstance(error_resp, str):
            error_resp = {}

        if success or (success_resp and success_resp.get("order_id")):
            sr = success_resp if isinstance(success_resp, dict) else getattr(success_resp, "__dict__", {})
            order_id = sr.get("order_id", client_order_id)
            logger.info(f"[coinbase] SELL order placed: {product_id} x{quantity} ({order_type}) → {order_id}")
            _cache.pop("positions", None)
            _cache.pop("portfolio", None)
            _cache.pop("accounts", None)
            return {"ok": True, "order_id": order_id, "state": "pending", "raw": rd}
        else:
            er = error_resp if isinstance(error_resp, dict) else getattr(error_resp, "__dict__", {})
            msg = er.get("message", "") or er.get("error", "") or str(rd)
            logger.error(f"[coinbase] SELL order failed for {product_id}: {msg}")
            return {"ok": False, "error": msg}

    except Exception as e:
        logger.error(f"[coinbase] SELL order exception for {product_id}: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


# ── Order lookup ─────────────────────────────────────────────────────

def get_order_by_id(order_id: str) -> dict[str, Any] | None:
    client = _get_client()
    if not client or not order_id:
        return None
    try:
        resp = client.get_order(order_id=order_id)
        od = resp if isinstance(resp, dict) else resp.__dict__ if hasattr(resp, "__dict__") else {}
        order_data = od.get("order", od)
        return order_data if isinstance(order_data, dict) else {}
    except Exception as e:
        logger.debug(f"[coinbase] get_order_by_id({order_id}) failed: {e}")
        return None


# ── Status mapping ───────────────────────────────────────────────────

_CB_TO_CHILI_STATUS = {
    "open":             "working",
    "pending":          "working",
    "filled":           "open",
    "cancelled":        "cancelled",
    "expired":          "cancelled",
    "failed":           "rejected",
}

_CB_TERMINAL_STATES = {"filled", "cancelled", "expired", "failed"}


def map_cb_status(cb_state: str | None) -> str:
    if not cb_state:
        return "working"
    return _CB_TO_CHILI_STATUS.get(cb_state.lower(), "working")


def is_cb_terminal(cb_state: str | None) -> bool:
    return (cb_state or "").lower() in _CB_TERMINAL_STATES


# ── Order sync (Coinbase → local DB) ────────────────────────────────

def sync_orders_to_db(db: Session, user_id: int | None) -> dict[str, int]:
    """Reconcile local trades with broker_source='coinbase' against Coinbase."""
    from ..models.trading import Trade, StrategyProposal

    if not is_connected():
        return {"synced": 0, "filled": 0, "cancelled": 0, "errors": 0}

    working_trades = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.broker_source == "coinbase",
            Trade.broker_order_id.isnot(None),
            Trade.status.in_(["working"]),
        )
        .all()
    )

    synced = filled = cancelled = errors = 0

    for trade in working_trades:
        try:
            cb_order = get_order_by_id(trade.broker_order_id)
            if not cb_order:
                errors += 1
                continue

            cb_state = (cb_order.get("status") or "").lower()
            now = datetime.utcnow()

            trade.broker_status = cb_state
            trade.last_broker_sync = now

            if cb_state == "filled":
                trade.status = "open"
                avg_price = _safe_float(cb_order.get("average_filled_price"))
                if avg_price:
                    trade.avg_fill_price = avg_price
                    trade.entry_price = avg_price
                filled_qty = _safe_float(cb_order.get("filled_size"))
                if filled_qty:
                    trade.quantity = filled_qty
                trade.filled_at = now
                filled += 1
                logger.info(f"[coinbase] Order {trade.broker_order_id} for {trade.ticker} FILLED @ ${avg_price}")
                _update_proposal_on_fill(db, trade)

            elif cb_state in _CB_TERMINAL_STATES and cb_state != "filled":
                trade.status = "cancelled"
                cancelled += 1
                logger.info(f"[coinbase] Order {trade.broker_order_id} for {trade.ticker} {cb_state}")
                _update_proposal_on_cancel(db, trade, cb_state)

            synced += 1

        except Exception as e:
            logger.warning(f"[coinbase] Order sync failed for {trade.ticker}: {e}")
            errors += 1

    if synced:
        db.commit()

    logger.info(f"[coinbase] Order sync: {synced} checked, {filled} filled, {cancelled} cancelled, {errors} errors")
    return {"synced": synced, "filled": filled, "cancelled": cancelled, "errors": errors}


def sync_positions_to_db(db: Session, user_id: int | None) -> dict[str, int]:
    """Sync Coinbase positions into local Trade model."""
    from ..models.trading import Trade

    if not is_connected():
        return {"created": 0, "updated": 0, "closed": 0}

    all_positions = get_positions()
    created = updated = closed = 0
    cb_tickers: set[str] = set()

    for pos in all_positions:
        ticker = pos["ticker"]
        cb_tickers.add(ticker)
        qty = pos.get("quantity", 0)
        if not qty or qty <= 0:
            continue

        existing = (
            db.query(Trade)
            .filter(
                Trade.user_id == user_id,
                Trade.ticker == ticker,
                Trade.broker_source == "coinbase",
                Trade.status == "open",
            )
            .first()
        )

        if existing:
            existing.quantity = qty
            updated += 1
        else:
            trade = Trade(
                user_id=user_id,
                ticker=ticker,
                direction="long",
                entry_price=pos.get("average_buy_price", 0),
                quantity=qty,
                status="open",
                broker_source="coinbase",
                tags="coinbase-sync",
                notes=f"Auto-synced from Coinbase on {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            )
            db.add(trade)
            created += 1

    stale = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.broker_source == "coinbase",
            Trade.status == "open",
            Trade.ticker.notin_(cb_tickers) if cb_tickers else True,
        )
        .all()
    )

    for trade in stale:
        trade.status = "closed"
        trade.exit_date = datetime.utcnow()
        trade.notes = (
            (trade.notes or "")
            + f"\nAuto-closed: position no longer on Coinbase ({datetime.utcnow().strftime('%Y-%m-%d %H:%M')})"
        )
        closed += 1

    db.commit()
    logger.info(f"[coinbase] Position sync: {created} created, {updated} updated, {closed} closed")
    return {"created": created, "updated": updated, "closed": closed, "_live_tickers": cb_tickers}


def _update_proposal_on_fill(db: Session, trade) -> None:
    from ..models.trading import StrategyProposal
    if not trade.broker_order_id:
        return
    proposal = (
        db.query(StrategyProposal)
        .filter(StrategyProposal.broker_order_id == trade.broker_order_id)
        .first()
    )
    if proposal and proposal.status == "working":
        proposal.status = "executed"
        proposal.executed_at = datetime.utcnow()


def _update_proposal_on_cancel(db: Session, trade, state: str) -> None:
    from ..models.trading import StrategyProposal
    if not trade.broker_order_id:
        return
    proposal = (
        db.query(StrategyProposal)
        .filter(StrategyProposal.broker_order_id == trade.broker_order_id)
        .first()
    )
    if proposal and proposal.status == "working":
        proposal.status = "approved"
        proposal.reviewed_at = datetime.utcnow()
        logger.info(f"[coinbase] Proposal #{proposal.id} reverted to 'approved' after order {state}")


def build_portfolio_context() -> str:
    """Build a text summary of the Coinbase portfolio for AI context."""
    if not is_connected():
        return ""
    portfolio = get_portfolio()
    positions = get_positions()
    if not portfolio and not positions:
        return ""

    lines = ["## COINBASE ADVANCED PORTFOLIO"]
    if portfolio:
        lines.append(
            f"Total value: ${portfolio.get('equity', 0):,.2f} | "
            f"Available cash: ${portfolio.get('cash', 0):,.2f}"
        )
    if positions:
        lines.append("POSITIONS:")
        for p in positions:
            lines.append(f"  - {p['ticker']}: {p['quantity']:.6f} units")
    return "\n".join(lines)


def get_usdc_deposit_address() -> dict[str, Any]:
    """Try to fetch or create a USDC deposit address from Coinbase.

    Tries the v2 API endpoints via the SDK's generic get/post helpers.
    Returns {"ok": True, "address": "0x..."} or {"ok": False, "error": "..."}.
    """
    client = _get_client()
    if not client or not _connected:
        return {"ok": False, "error": "Coinbase not connected"}

    try:
        accounts = get_accounts_raw()
        usdc_acc = None
        for acc in accounts:
            currency = acc.get("currency") or ""
            if currency.upper() == "USDC":
                usdc_acc = acc
                break
        if not usdc_acc:
            return {"ok": False, "error": "No USDC account found on Coinbase"}

        acc_uuid = usdc_acc.get("uuid") or usdc_acc.get("id", "")
        if not acc_uuid:
            return {"ok": False, "error": "Could not determine USDC account ID"}

        try:
            resp = client.get(f"/v2/accounts/{acc_uuid}/addresses")
            data = resp if isinstance(resp, dict) else getattr(resp, "__dict__", {})
            addresses = data.get("data", [])
            for addr in addresses:
                a = addr if isinstance(addr, dict) else getattr(addr, "__dict__", {})
                network_name = (a.get("network") or "").lower()
                address_val = a.get("address", "")
                if address_val and ("ethereum" in network_name or "erc20" in network_name or network_name == ""):
                    return {"ok": True, "address": address_val}
            if addresses:
                first = addresses[0] if isinstance(addresses[0], dict) else getattr(addresses[0], "__dict__", {})
                if first.get("address"):
                    return {"ok": True, "address": first["address"]}
        except Exception as e:
            logger.debug(f"[coinbase] GET addresses failed: {e}")

        try:
            resp = client.post(f"/v2/accounts/{acc_uuid}/addresses", data={})
            data = resp if isinstance(resp, dict) else getattr(resp, "__dict__", {})
            addr_data = data.get("data", {})
            if isinstance(addr_data, dict) and addr_data.get("address"):
                return {"ok": True, "address": addr_data["address"]}
        except Exception as e:
            logger.debug(f"[coinbase] POST create address failed: {e}")

        return {"ok": False, "error": "Could not retrieve USDC deposit address from Coinbase API. Please provide it manually."}

    except Exception as e:
        logger.warning(f"[coinbase] get_usdc_deposit_address failed: {e}")
        return {"ok": False, "error": str(e)}


def clear_cache() -> None:
    _cache.clear()
    global _client, _connected, _last_check
    _client = None
    _connected = False
    _last_check = 0


def _safe_float(val: Any) -> float:
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0
