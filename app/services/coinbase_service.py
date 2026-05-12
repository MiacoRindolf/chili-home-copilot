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
from .trading.broker_position_sync import (
    acquire_broker_position_sync_lock,
    collapse_open_broker_position_duplicates,
    dedupe_positions_by_ticker,
)
from .trading.tick_normalizer import normalize_price, normalize_quantity

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
    if not _cb_available:
        return False
    # Accept either env-var credentials or a prior connect_with_credentials session
    if not _credentials_configured() and not _connected:
        return False
    if _connected and (time.time() - _last_check) < _CHECK_TTL:
        return True
    if _client is not None:
        # Vault-connected client exists; verify it still works
        try:
            resp = _client.get_accounts(limit=1)
            accounts = resp.get("accounts", []) if isinstance(resp, dict) else getattr(resp, "accounts", [])
            if accounts is not None:
                _connected = True
                _last_check = time.time()
                return True
        except Exception:
            _connected = False
            return False
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
        hold_bal = acc.get("hold", {})
        hold_val = _safe_float(hold_bal.get("value") if isinstance(hold_bal, dict) else getattr(hold_bal, "value", 0))
        currency = (
            acc.get("currency")
            or (bal.get("currency") if isinstance(bal, dict) else getattr(bal, "currency", ""))
            or ""
        ).upper()
        if currency == "USD":
            available_cash += val
        total_value += val + hold_val
    result = {
        "equity": round(total_value, 2),
        "buying_power": round(available_cash, 2),
        "cash": round(available_cash, 2),
        "last_updated": datetime.utcnow().isoformat(),
    }
    _cache_set("portfolio", result)
    return result


def _get_cost_basis_from_fills(product_id: str) -> float:
    """
    Compute weighted-average cost basis for a product from historical fills.
    Returns average buy price per unit, or 0 if unavailable.
    """
    cached = _cache_get(f"cost_basis_{product_id}")
    if cached is not None:
        return cached
    client = _get_client()
    if not client:
        return 0.0
    try:
        resp = client.get_fills(product_id=product_id, limit=100)
        fills_raw = resp.get("fills", []) if isinstance(resp, dict) else getattr(resp, "fills", [])
        total_cost = 0.0
        total_qty = 0.0
        for f in (fills_raw or []):
            fd = f if isinstance(f, dict) else f.__dict__ if hasattr(f, "__dict__") else {}
            side = fd.get("side", "")
            price = _safe_float(fd.get("price", 0))
            size = _safe_float(fd.get("size", 0))
            if side == "BUY" and price > 0 and size > 0:
                total_cost += price * size
                total_qty += size
        avg = round(total_cost / total_qty, 8) if total_qty > 0 else 0.0
        _cache_set(f"cost_basis_{product_id}", avg)
        return avg
    except Exception as e:
        logger.debug("[coinbase] cost basis from fills failed for %s: %s", product_id, e)
        return 0.0


def get_positions() -> list[dict[str, Any]]:
    """Current crypto holdings with non-zero balances, with cost basis from fills."""
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
        currency = (
            acc.get("currency")
            or (bal.get("currency") if isinstance(bal, dict) else getattr(bal, "currency", ""))
            or ""
        ).upper()
        if currency == "USD":
            continue
        hold_bal = acc.get("hold", {})
        hold_val = _safe_float(hold_bal.get("value") if isinstance(hold_bal, dict) else getattr(hold_bal, "value", 0))
        total_qty = val + hold_val
        if total_qty <= 0:
            continue
        ticker = f"{currency}-USD"
        avg_price = _get_cost_basis_from_fills(ticker)
        positions.append({
            "ticker": ticker,
            "quantity": total_qty,
            "average_buy_price": avg_price,
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


def get_open_orders(product_ids: list[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Open Coinbase Advanced Trade orders, optionally scoped to products."""
    client = _get_client()
    if not client or not is_connected():
        return []
    try:
        resp = client.list_orders(
            product_ids=product_ids,
            order_status=["OPEN"],
            limit=limit,
        )
        raw_orders = resp.get("orders", []) if isinstance(resp, dict) else getattr(resp, "orders", [])
        out: list[dict[str, Any]] = []
        for order in raw_orders or []:
            out.append(order if isinstance(order, dict) else getattr(order, "__dict__", {}))
        return out
    except Exception as e:
        logger.warning(f"[coinbase] get_open_orders failed: {e}")
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
    post_only: bool = False,
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
            # Phase 1 (2026-05-01): venue-aware tick + qty normalization.
            # Coinbase pairs are crypto; the previous round(price, 2) silently
            # truncated sub-penny prices like 0.10984 → 0.11.
            #
            # f-fastpath-maker-only-executor (2026-05-08): when post_only is
            # set, prefer the SDK's *_post_only variant if present (so the
            # order is rejected by the venue rather than crossing as a
            # taker). Some SDK versions expose post_only as a kwarg on the
            # standard variant; we try the dedicated method first and fall
            # back to the kwarg path.
            buy_kwargs = dict(
                client_order_id=client_order_id,
                product_id=product_id,
                base_size=str(normalize_quantity(quantity, ticker)),
                limit_price=str(normalize_price(limit_price, ticker, asset_class="crypto")),
            )
            if post_only:
                post_only_fn = getattr(client, "limit_order_gtc_buy_post_only", None)
                if callable(post_only_fn):
                    resp = post_only_fn(**buy_kwargs)
                else:
                    resp = client.limit_order_gtc_buy(post_only=True, **buy_kwargs)
            else:
                resp = client.limit_order_gtc_buy(**buy_kwargs)
        else:
            resp = client.market_order_buy(
                client_order_id=client_order_id,
                product_id=product_id,
                base_size=str(normalize_quantity(quantity, ticker)),
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
    post_only: bool = False,
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
            # Phase 1 (2026-05-01): see place_buy_order for context.
            # f-fastpath-maker-only-executor (2026-05-08): post_only support
            # — see place_buy_order for the SDK-variant dispatch rationale.
            sell_kwargs = dict(
                client_order_id=client_order_id,
                product_id=product_id,
                base_size=str(normalize_quantity(quantity, ticker)),
                limit_price=str(normalize_price(limit_price, ticker, asset_class="crypto")),
            )
            if post_only:
                post_only_fn = getattr(client, "limit_order_gtc_sell_post_only", None)
                if callable(post_only_fn):
                    resp = post_only_fn(**sell_kwargs)
                else:
                    resp = client.limit_order_gtc_sell(post_only=True, **sell_kwargs)
            else:
                resp = client.limit_order_gtc_sell(**sell_kwargs)
        else:
            resp = client.market_order_sell(
                client_order_id=client_order_id,
                product_id=product_id,
                base_size=str(normalize_quantity(quantity, ticker)),
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
        if isinstance(order_data, dict):
            return order_data
        if hasattr(order_data, "__dict__"):
            return dict(order_data.__dict__)
        return {}
    except Exception as e:
        logger.debug(f"[coinbase] get_order_by_id({order_id}) failed: {e}")
        return None


def cancel_order_by_id(order_id: str) -> dict[str, Any]:
    """Cancel a single open Coinbase order by id.

    f-fastpath-maker-only-executor (2026-05-08): the maker-only
    executor schedules a cancel-on-timeout asyncio task that calls
    this helper when the resting limit hasn't filled within
    ``settings.maker_cancel_on_timeout_s`` seconds. Returns
    ``{"ok": True}`` on success, ``{"ok": False, "error": ...}`` on
    any failure path so the caller can record the outcome regardless.

    The Coinbase Advanced Trade SDK takes a list of order_ids
    (``cancel_orders``) and returns a response with per-order
    success flags. We single-id this for executor simplicity; if a
    future brief needs batch-cancel, lift the wrapper accordingly.
    """
    client = _get_client()
    if not client:
        return {"ok": False, "error": "Coinbase client not available"}
    if not order_id:
        return {"ok": False, "error": "missing order_id"}
    try:
        resp = client.cancel_orders(order_ids=[order_id])
        rd = resp if isinstance(resp, dict) else resp.__dict__ if hasattr(resp, "__dict__") else {}
        results = rd.get("results") or rd.get("orders") or []
        if isinstance(results, list) and results:
            r0 = results[0] if isinstance(results[0], dict) else getattr(results[0], "__dict__", {})
            if r0.get("success"):
                logger.info(f"[coinbase] CANCEL accepted order_id={order_id}")
                return {"ok": True, "order_id": order_id, "raw": rd}
            err = r0.get("failure_reason") or r0.get("error") or str(r0)
            return {"ok": False, "error": str(err)}
        # SDK shape variant: top-level success bool.
        if rd.get("success"):
            return {"ok": True, "order_id": order_id, "raw": rd}
        return {"ok": False, "error": str(rd) or "unknown_cancel_failure"}
    except Exception as e:
        logger.warning(f"[coinbase] cancel_order_by_id({order_id}) exception: {e}")
        return {"ok": False, "error": str(e)}


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


def _parse_cb_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _clear_pending_exit_fields(trade: Any) -> None:
    trade.pending_exit_order_id = None
    trade.pending_exit_status = None
    trade.pending_exit_requested_at = None
    trade.pending_exit_reason = None
    trade.pending_exit_limit_price = None


def _finalize_coinbase_pending_exit(db: Session, trade: Any, order: dict[str, Any]) -> float:
    filled_qty = _safe_float(order.get("filled_size") or order.get("base_size"))
    qty = filled_qty if filled_qty and filled_qty > 0 else float(trade.quantity or 0.0)
    exit_px = (
        _safe_float(order.get("average_filled_price"))
        or _safe_float(order.get("average_price"))
        or _safe_float(order.get("price"))
        or _safe_float(trade.pending_exit_limit_price)
        or float(trade.entry_price or 0.0)
    )
    exit_at = (
        _parse_cb_datetime(order.get("last_fill_time"))
        or _parse_cb_datetime(order.get("completion_time"))
        or _parse_cb_datetime(order.get("filled_at"))
        or datetime.utcnow()
    )
    exit_reason = str(trade.pending_exit_reason or "pending_exit")
    entry = float(trade.entry_price or 0.0)
    pnl = (float(exit_px) - entry) * float(qty or 0.0)

    trade.status = "closed"
    if qty and qty > 0:
        trade.quantity = float(qty)
    trade.exit_price = float(exit_px)
    trade.exit_date = exit_at
    trade.pnl = round(pnl, 4)
    trade.exit_reason = exit_reason
    trade.broker_status = str(order.get("status") or order.get("state") or "filled").lower()
    trade.last_broker_sync = exit_at
    _clear_pending_exit_fields(trade)
    db.add(trade)
    db.commit()

    try:
        from .trading.brain_work.execution_hooks import on_live_trade_closed

        on_live_trade_closed(db, trade, source="coinbase_exit_execution")
    except Exception:
        logger.debug(
            "[coinbase] on_live_trade_closed failed for trade=%s",
            getattr(trade, "id", None),
            exc_info=True,
        )
    try:
        from .trading.auto_trader_position_overrides import clear_position_overrides

        clear_position_overrides(db, "trade", int(trade.id))
    except Exception:
        logger.debug(
            "[coinbase] clear_position_overrides failed for trade=%s",
            getattr(trade, "id", None),
            exc_info=True,
        )
    return round(pnl, 4)


# ── Order sync (Coinbase → local DB) ────────────────────────────────

def sync_orders_to_db(db: Session, user_id: int | None) -> dict[str, int]:
    """Reconcile local trades with broker_source='coinbase' against Coinbase."""
    from ..models.trading import Trade, StrategyProposal
    from .trading.execution_audit import normalize_coinbase_order_event, record_execution_event

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
    open_with_pending_exit = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.broker_source == "coinbase",
            Trade.pending_exit_order_id.isnot(None),
            Trade.status == "open",
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
            normalized = normalize_coinbase_order_event(
                order={**cb_order, "order_id": trade.broker_order_id},
                trade=trade,
                event_type="status",
            )
            normalized.setdefault("submitted_at", getattr(trade, "submitted_at", None) or now)
            normalized.setdefault("acknowledged_at", now)
            record_execution_event(
                db,
                user_id=trade.user_id,
                ticker=trade.ticker,
                trade=trade,
                scan_pattern_id=getattr(trade, "scan_pattern_id", None),
                **normalized,
            )
            trade.last_broker_sync = now

            if cb_state == "filled":
                filled += 1
                logger.info(
                    f"[coinbase] Order {trade.broker_order_id} for {trade.ticker} FILLED @ ${trade.avg_fill_price}"
                )
                _update_proposal_on_fill(db, trade)

            elif cb_state in _CB_TERMINAL_STATES and cb_state != "filled":
                cancelled += 1
                logger.info(f"[coinbase] Order {trade.broker_order_id} for {trade.ticker} {cb_state}")
                _update_proposal_on_cancel(db, trade, cb_state)

            synced += 1

        except Exception as e:
            logger.warning(f"[coinbase] Order sync failed for {trade.ticker}: {e}")
            errors += 1

    for trade in open_with_pending_exit:
        try:
            pending_order_id = str(trade.pending_exit_order_id or "")
            cb_order = get_order_by_id(pending_order_id)
            if not cb_order:
                errors += 1
                continue

            cb_state = (cb_order.get("status") or cb_order.get("state") or "").lower()
            now = datetime.utcnow()
            trade.pending_exit_status = cb_state or trade.pending_exit_status
            trade.last_broker_sync = now
            db.add(trade)

            if cb_state == "filled":
                order_payload = {**cb_order, "order_id": pending_order_id}
                pnl = _finalize_coinbase_pending_exit(db, trade, order_payload)
                filled += 1
                logger.info(
                    "[coinbase] Pending exit %s for %s FILLED pnl=%s",
                    pending_order_id,
                    trade.ticker,
                    pnl,
                )
            elif cb_state in _CB_TERMINAL_STATES and cb_state != "filled":
                _clear_pending_exit_fields(trade)
                trade.broker_status = cb_state
                trade.last_broker_sync = now
                db.add(trade)
                cancelled += 1
                logger.info(
                    "[coinbase] Pending exit %s for %s %s; cleared pending exit",
                    pending_order_id,
                    trade.ticker,
                    cb_state,
                )

            synced += 1

        except Exception as e:
            logger.warning(f"[coinbase] Pending exit sync failed for {trade.ticker}: {e}")
            errors += 1

    if synced:
        db.commit()

    logger.info(f"[coinbase] Order sync: {synced} checked, {filled} filled, {cancelled} cancelled, {errors} errors")
    return {"synced": synced, "filled": filled, "cancelled": cancelled, "errors": errors}


def _link_latest_alert(db: Session, trade) -> None:
    """Link a broker-synced trade to its most recent imminent/pending alert if one exists."""
    from ..models.trading import BreakoutAlert

    if trade.related_alert_id:
        return
    try:
        alert = (
            db.query(BreakoutAlert)
            .filter(
                BreakoutAlert.ticker == trade.ticker,
                BreakoutAlert.outcome == "pending",
                BreakoutAlert.scan_pattern_id.isnot(None),
            )
            .order_by(BreakoutAlert.alerted_at.desc())
            .first()
        )
        if alert:
            trade.related_alert_id = alert.id
            trade.scan_pattern_id = alert.scan_pattern_id
            if not trade.stop_loss and alert.stop_loss:
                trade.stop_loss = float(alert.stop_loss)
            if not trade.take_profit and alert.target_price:
                trade.take_profit = float(alert.target_price)
            logger.debug("[coinbase] Linked %s to alert %s (pattern %s)",
                         trade.ticker, alert.id, alert.scan_pattern_id)
    except Exception:
        logger.debug("[coinbase] _link_latest_alert failed for %s", trade.ticker, exc_info=True)


def sync_positions_to_db(db: Session, user_id: int | None) -> dict[str, int]:
    """Sync Coinbase positions into local Trade model."""
    from ..models.trading import BreakoutAlert, Trade

    if not is_connected():
        return {"created": 0, "updated": 0, "closed": 0}

    acquire_broker_position_sync_lock(db, broker_source="coinbase", user_id=user_id)
    cleanup = collapse_open_broker_position_duplicates(
        db, broker_source="coinbase", user_id=user_id,
    )

    all_positions = dedupe_positions_by_ticker(get_positions())
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
            avg_price = pos.get("average_buy_price", 0)
            if avg_price and avg_price > 0 and (not existing.entry_price or existing.entry_price == 0):
                existing.entry_price = avg_price
            if not existing.related_alert_id:
                _link_latest_alert(db, existing)
            updated += 1
        else:
            avg_price = pos.get("average_buy_price", 0)
            if not avg_price or avg_price <= 0:
                logger.warning(
                    "[coinbase] Skipping auto-create for %s: cost basis unavailable",
                    ticker,
                )
                continue
            trade = Trade(
                user_id=user_id,
                ticker=ticker,
                direction="long",
                entry_price=avg_price,
                quantity=qty,
                status="open",
                broker_source="coinbase",
                tags="coinbase-sync",
                stop_model="atr_crypto_breakout",
                notes=f"Auto-synced from Coinbase on {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            )
            db.add(trade)
            db.flush()
            _link_latest_alert(db, trade)
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
        if not trade.exit_reason:
            trade.exit_reason = "coinbase_position_sync_gone"
        trade.notes = (
            (trade.notes or "")
            + f"\nAuto-closed: position no longer on Coinbase ({datetime.utcnow().strftime('%Y-%m-%d %H:%M')})"
        )
        try:
            from .trading.brain_work.execution_hooks import on_broker_reconciled_close

            on_broker_reconciled_close(db, trade, source="coinbase_position_sync")
        except Exception:
            logger.debug("[coinbase] brain_work broker close hook failed", exc_info=True)
        closed += 1

    db.commit()
    logger.info(
        "[coinbase] Position sync: %d created, %d updated, %d closed, %d duplicates cancelled",
        created,
        updated,
        closed,
        cleanup["cancelled"],
    )
    return {
        "created": created,
        "updated": updated,
        "closed": closed,
        "deduped": cleanup["cancelled"],
        "_live_tickers": cb_tickers,
    }


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
