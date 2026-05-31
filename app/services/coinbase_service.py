"""Coinbase Advanced Trade integration via coinbase-advanced-py SDK.

Mirrors the broker_service.py interface so the broker_manager can dispatch
to either Robinhood or Coinbase transparently.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, text
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
_client_source = ""
_connected = False
_last_check: float = 0
_CHECK_TTL = 600

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 300
_TRANSACTION_SUMMARY_CACHE_TTL = 60
_COINBASE_UNAVAILABLE_PRODUCT_TTL_SECONDS = int(
    getattr(settings, "chili_coinbase_unavailable_product_ttl_seconds", 6 * 60 * 60)
)
_unavailable_products: dict[str, float] = {}

# f-coinbase-dust-auto-create-skip (2026-05-19): minimum dollar notional
# below which ``sync_positions_to_db`` will refuse to auto-create a Trade
# row from a Coinbase wallet holding. Coinbase's ``quote_min_size`` is
# typically $1 for spot products; we set this slightly higher to also
# screen out exposure-only-on-paper holdings the autotrader couldn't
# round-trip. Operator can raise via env override if needed; staying
# hardcoded for now keeps the contract obvious.
_MIN_AUTO_CREATE_NOTIONAL_USD = 1.0

# Coinbase has the same partial-list failure mode Robinhood had: one
# truncated non-empty wallet snapshot can omit an otherwise-live product.
# Reuse the RH guard knob so both broker reconcilers require consecutive
# misses before marking a local management envelope closed.
_COINBASE_RECONCILE_MISSING_STREAK_MIN = int(
    getattr(settings, "chili_reconcile_partial_list_streak_min", 2)
)
_COINBASE_RECONCILE_CONFIRM_WINDOW = int(
    getattr(settings, "broker_reconcile_confirm_seconds", 300)
)
_ABSENT_NO_FILL_STREAK_MULTIPLIER = 6
_ABSENT_NO_FILL_STREAK_FLOOR = 12
_ABSENT_NO_FILL_MIN_AGE_MULTIPLIER = 3
_ABSENT_NO_FILL_MIN_AGE_FLOOR_SECONDS = 1800
_COINBASE_ABSENT_NO_FILL_STREAK_MIN = int(
    getattr(
        settings,
        "chili_coinbase_absent_no_fill_reconcile_streak_min",
        max(
            _ABSENT_NO_FILL_STREAK_FLOOR,
            _COINBASE_RECONCILE_MISSING_STREAK_MIN
            * _ABSENT_NO_FILL_STREAK_MULTIPLIER,
        ),
    )
)
_COINBASE_ABSENT_NO_FILL_MIN_AGE_SECONDS = int(
    getattr(
        settings,
        "chili_coinbase_absent_no_fill_reconcile_min_age_seconds",
        max(
            _ABSENT_NO_FILL_MIN_AGE_FLOOR_SECONDS,
            _COINBASE_RECONCILE_CONFIRM_WINDOW
            * _ABSENT_NO_FILL_MIN_AGE_MULTIPLIER,
        ),
    )
)

# Recent bookkeeping closes are eligible for inverse reconcile when Coinbase
# still reports the position and no real sell fill is recorded.
_COINBASE_BOOKKEEPING_REOPEN_LOOKBACK_HOURS = 72
_USD_PEGGED_ACCOUNT_CURRENCIES = frozenset({"USDC"})


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
    global _client, _client_source
    if _client is not None:
        return _client
    if not _cb_available or not _credentials_configured():
        return None
    try:
        from coinbase.rest import RESTClient as CB
        secret = settings.coinbase_api_secret.replace("\\n", "\n")
        _client = CB(api_key=settings.coinbase_api_key, api_secret=secret)
        _client_source = "env"
        return _client
    except Exception as e:
        logger.error(f"[coinbase] Failed to create client: {e}")
        return None


def _get_env_client():
    if not _cb_available or not _credentials_configured():
        return None
    if _client is not None and _client_source == "env":
        return _client
    try:
        from coinbase.rest import RESTClient as CB
        secret = settings.coinbase_api_secret.replace("\\n", "\n")
        return CB(api_key=settings.coinbase_api_key, api_secret=secret)
    except Exception as e:
        logger.error(f"[coinbase] Failed to create env client: {e}")
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


def connect_env_credentials() -> dict[str, Any]:
    """Make the configured environment Coinbase credentials the active client."""
    global _client, _client_source, _connected, _last_check
    if not _credentials_configured():
        return {
            "status": "needs_credentials",
            "message": "Coinbase environment credentials are not configured.",
        }
    _client = None
    _client_source = ""
    _connected = False
    _last_check = 0
    return connect()


def connect_with_credentials(api_key: str, api_secret: str) -> dict[str, Any]:
    """Connect using explicitly provided credentials (from DB vault)."""
    global _client, _client_source, _connected, _last_check
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
            _client_source = "explicit"
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


def get_transaction_summary_raw(
    *, prefer_env_credentials: bool = False,
) -> dict[str, Any]:
    """Fetch Coinbase account transaction summary, cached.

    Callers should only expose the specific fields they need. The raw
    response can include balances and fee totals.
    """
    cache_key = (
        "transaction_summary_env"
        if prefer_env_credentials
        else "transaction_summary"
    )
    cached_entry = _cache.get(cache_key)
    if (
        cached_entry is not None
        and (time.time() - cached_entry[0]) < _TRANSACTION_SUMMARY_CACHE_TTL
    ):
        return cached_entry[1]
    client = _get_env_client() if prefer_env_credentials else _get_client()
    if not client:
        return {}
    if not prefer_env_credentials and not is_connected():
        return {}
    try:
        resp = client.get_transaction_summary()
        summary = (
            resp if isinstance(resp, dict)
            else resp.__dict__ if hasattr(resp, "__dict__")
            else {}
        )
        _cache[cache_key] = (time.time(), summary)
        return summary
    except Exception as e:
        logger.warning("[coinbase] get_transaction_summary failed: %s", e)
        return {}


def _fee_rate_to_bps(value: Any) -> float:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return 0.0
    if rate <= 0.0:
        return 0.0
    return rate * 10_000.0 if rate < 1.0 else rate


def get_fee_rates_bps(*, prefer_env_credentials: bool = False) -> dict[str, Any]:
    """Return the current Coinbase maker/taker fee rates in bps."""
    summary = get_transaction_summary_raw(
        prefer_env_credentials=prefer_env_credentials,
    )
    tier = None
    if isinstance(summary, dict):
        tier_without_promo = summary.get("fee_tier_without_promotion")
        if isinstance(tier_without_promo, dict):
            tier = tier_without_promo.get("current_tier")
        if not isinstance(tier, dict):
            tier = summary.get("fee_tier")
    if not isinstance(tier, dict):
        return {}
    maker_fee_bps = _fee_rate_to_bps(tier.get("maker_fee_rate"))
    taker_fee_bps = _fee_rate_to_bps(tier.get("taker_fee_rate"))
    if maker_fee_bps <= 0.0 and taker_fee_bps <= 0.0:
        return {}
    return {
        "maker_fee_bps": maker_fee_bps,
        "taker_fee_bps": taker_fee_bps,
        "pricing_tier": tier.get("pricing_tier") or "",
    }


# ── Data fetching ────────────────────────────────────────────────────

def get_accounts_raw(*, use_cache: bool = True) -> list[dict]:
    """Fetch all Coinbase accounts (wallets). Cached."""
    if use_cache:
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


def _balance_component_value(component: Any) -> float:
    if isinstance(component, dict):
        return _safe_float(component.get("value"))
    return _safe_float(getattr(component, "value", 0))


def _account_currency(acc: dict[str, Any], balance: Any | None = None) -> str:
    if balance is None:
        balance = acc.get("available_balance", {})
    currency = acc.get("currency")
    if not currency:
        if isinstance(balance, dict):
            currency = balance.get("currency")
        else:
            currency = getattr(balance, "currency", "")
    return str(currency or "").upper()


def _quote_price_from_pricebook_entry(entry: Any) -> float:
    if isinstance(entry, dict):
        for key in ("price", "price_level"):
            price = _safe_float(entry.get(key))
            if price > 0:
                return price
        return 0.0
    for key in ("price", "price_level"):
        price = _safe_float(getattr(entry, key, 0))
        if price > 0:
            return price
    return 0.0


def _extract_pricebooks(resp: Any) -> list[Any]:
    if isinstance(resp, dict):
        raw = resp.get("pricebooks") or resp.get("pricebook") or []
    else:
        raw = getattr(resp, "pricebooks", None) or getattr(resp, "pricebook", None) or []
    if isinstance(raw, list):
        return raw
    return [raw] if raw else []


def _coinbase_product_unavailable_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "productid" in text and "could not be found" in text
    ) or (
        "invalid product_id" in text
    )


def _coinbase_product_key(product_id: str | None) -> str:
    return str(product_id or "").strip().upper()


def _coinbase_product_marked_unavailable(product_id: str | None) -> bool:
    product_key = _coinbase_product_key(product_id)
    if not product_key:
        return False
    marked_at = _unavailable_products.get(product_key)
    if marked_at is None:
        return False
    ttl = max(0, int(_COINBASE_UNAVAILABLE_PRODUCT_TTL_SECONDS or 0))
    if ttl <= 0:
        return False
    if (time.time() - marked_at) <= ttl:
        return True
    _unavailable_products.pop(product_key, None)
    return False


def _mark_coinbase_product_unavailable(product_id: str | None) -> None:
    product_key = _coinbase_product_key(product_id)
    if not product_key:
        return
    _unavailable_products[product_key] = time.time()
    _cache_set(f"product_price:{product_key}", 0.0)


def _coinbase_public_product_support(product_id: str) -> bool | None:
    try:
        from .trading.coinbase_ohlcv import public_product_support

        return public_product_support(product_id)
    except Exception:
        return None


def _coinbase_current_price(
    product_id: str,
    price_cache: dict[str, float] | None = None,
) -> float:
    """Return a fresh USD mark for a Coinbase product.

    Coinbase account balances are unit truth, but the UI and portfolio need a
    USD mark. Prefer the last trade; fall back to BBO mid/bid/ask if trades are
    unavailable. A zero return means "unknown", never "free".
    """
    product_key = _coinbase_product_key(product_id)
    if not product_key:
        return 0.0
    base_currency = product_key.split("-", 1)[0]
    if base_currency in _USD_PEGGED_ACCOUNT_CURRENCIES:
        return 1.0
    if _coinbase_product_marked_unavailable(product_key):
        if price_cache is not None:
            price_cache[product_key] = 0.0
        return 0.0
    if price_cache is not None and product_key in price_cache:
        return float(price_cache[product_key] or 0.0)
    cached = _cache_get(f"product_price:{product_key}")
    if cached is not None:
        price = float(cached or 0.0)
        if price_cache is not None:
            price_cache[product_key] = price
        return price

    support = _coinbase_public_product_support(product_key)
    if support is False:
        _mark_coinbase_product_unavailable(product_key)
        if price_cache is not None:
            price_cache[product_key] = 0.0
        return 0.0

    client = _get_client()
    if not client:
        return 0.0

    candidates: list[float] = []
    try:
        resp = client.get_market_trades(product_id=product_key, limit=1)
        trades = (
            resp.get("trades", [])
            if isinstance(resp, dict)
            else getattr(resp, "trades", [])
        )
        for trade in trades or []:
            if isinstance(trade, dict):
                price = _safe_float(trade.get("price"))
            else:
                price = _safe_float(getattr(trade, "price", 0))
            if price > 0:
                candidates.append(price)
                break
    except Exception as exc:
        if _coinbase_product_unavailable_error(exc):
            if price_cache is not None:
                price_cache[product_key] = 0.0
            _mark_coinbase_product_unavailable(product_key)
            return 0.0
        logger.debug("[coinbase] market trade price unavailable for %s", product_key, exc_info=True)

    try:
        resp = client.get_best_bid_ask(product_ids=[product_key])
        for book in _extract_pricebooks(resp):
            bids = book.get("bids", []) if isinstance(book, dict) else getattr(book, "bids", [])
            asks = book.get("asks", []) if isinstance(book, dict) else getattr(book, "asks", [])
            bid = _quote_price_from_pricebook_entry((bids or [None])[0])
            ask = _quote_price_from_pricebook_entry((asks or [None])[0])
            if bid > 0 and ask > 0:
                candidates.append((bid + ask) / 2.0)
            if bid > 0:
                candidates.append(bid)
            if ask > 0:
                candidates.append(ask)
            if candidates:
                break
    except Exception as exc:
        if _coinbase_product_unavailable_error(exc):
            _mark_coinbase_product_unavailable(product_key)
            if price_cache is not None:
                price_cache[product_key] = 0.0
            return 0.0
        logger.debug("[coinbase] best bid/ask price unavailable for %s", product_key, exc_info=True)

    price = next((float(p) for p in candidates if p and p > 0), 0.0)
    if price_cache is not None:
        price_cache[product_key] = price
    _cache_set(f"product_price:{product_key}", price)
    return price


def get_portfolio(*, use_cache: bool = True) -> dict[str, Any]:
    """Account balances: total value and available cash."""
    if use_cache:
        cached = _cache_get("portfolio")
        if cached is not None:
            return cached
    accounts = get_accounts_raw(use_cache=use_cache)
    if not accounts:
        return {}
    total_value = 0.0
    available_cash = 0.0
    price_cache: dict[str, float] = {}
    for acc in accounts:
        bal = acc.get("available_balance", {})
        val = _balance_component_value(bal)
        hold_bal = acc.get("hold", {})
        hold_val = _balance_component_value(hold_bal)
        currency = _account_currency(acc, bal)
        if currency == "USD":
            available_cash += val
            total_value += val + hold_val
            continue
        total_qty = val + hold_val
        if total_qty <= 0:
            continue
        price = _coinbase_current_price(f"{currency}-USD", price_cache)
        if price > 0:
            total_value += total_qty * price
    result = {
        "equity": round(total_value, 2),
        "buying_power": round(available_cash, 2),
        "cash": round(available_cash, 2),
        "last_updated": datetime.utcnow().isoformat(),
    }
    if use_cache:
        _cache_set("portfolio", result)
    return result


def _fill_payload(fill: Any) -> dict[str, Any]:
    if isinstance(fill, dict):
        return fill
    return getattr(fill, "__dict__", {}) if hasattr(fill, "__dict__") else {}


def _fill_commission_quote(fd: dict[str, Any]) -> float:
    detail = fd.get("commission_detail_total")
    if isinstance(detail, dict):
        total = _safe_float(detail.get("total_commission"))
        if total > 0:
            return total
    return _safe_float(fd.get("commission") or fd.get("fee"))


def _current_lot_average_from_fills(
    fills_raw: list[Any],
    *,
    current_qty: float | None,
) -> float:
    """Reconstruct current-position average entry from Coinbase fills.

    Coinbase account balances are position truth. A naive average of all BUY
    fills overstates entry after a fully or partially sold lot. Keep FIFO lots,
    deplete them with SELL fills, and include quote commissions in BUY cost so
    local P&L lines up with the broker's displayed average entry.
    """
    fills = [_fill_payload(fill) for fill in fills_raw or []]
    fills.sort(
        key=lambda fd: (
            _parse_cb_datetime(
                fd.get("trade_time")
                or fd.get("sequence_timestamp")
                or fd.get("created_time")
            )
            or datetime.min,
            str(fd.get("entry_id") or fd.get("trade_id") or fd.get("order_id") or ""),
        )
    )
    lots: list[list[float]] = []
    for fd in fills:
        side = str(fd.get("side") or fd.get("order_side") or "").strip().upper()
        price = _safe_float(fd.get("price") or fd.get("trade_price"))
        qty = _safe_float(fd.get("size") or fd.get("trade_size") or fd.get("filled_size"))
        if price <= 0 or qty <= 0:
            continue
        if side == "BUY":
            fee = _fill_commission_quote(fd)
            lots.append([qty, (price * qty + fee) / qty])
            continue
        if side != "SELL":
            continue
        remaining_sell = qty
        while remaining_sell > 0 and lots:
            lot_qty, lot_cost = lots[0]
            take = min(lot_qty, remaining_sell)
            lot_qty -= take
            remaining_sell -= take
            if lot_qty <= 1e-12:
                lots.pop(0)
            else:
                lots[0] = [lot_qty, lot_cost]

    target_qty = float(current_qty or 0.0)
    if target_qty > 0:
        lot_qty = sum(qty for qty, _cost in lots)
        excess = lot_qty - target_qty
        tolerance = max(1e-8, target_qty * 1e-6)
        if excess > tolerance:
            # If the fill window still contains too much inventory, trim the
            # oldest lots first. That matches FIFO depletion and avoids letting
            # stale sold lots leak back into the displayed broker-average cost.
            remaining_trim = excess
            while remaining_trim > 0 and lots:
                lot_qty, lot_cost = lots[0]
                take = min(lot_qty, remaining_trim)
                lot_qty -= take
                remaining_trim -= take
                if lot_qty <= 1e-12:
                    lots.pop(0)
                else:
                    lots[0] = [lot_qty, lot_cost]

    total_qty = sum(qty for qty, _cost in lots)
    total_cost = sum(qty * cost for qty, cost in lots)
    if total_qty <= 0 or total_cost <= 0:
        return 0.0
    return round(total_cost / total_qty, 8)


def _get_cost_basis_from_fills(product_id: str, current_qty: float | None = None) -> float:
    """
    Compute weighted-average cost basis for the currently held product lots.
    Returns average buy price per unit, or 0 if unavailable.
    """
    product_key = _coinbase_product_key(product_id)
    if _coinbase_product_marked_unavailable(product_key):
        return 0.0
    qty_key = "" if current_qty is None else f":{round(float(current_qty or 0.0), 8)}"
    cached = _cache_get(f"cost_basis_{product_key}{qty_key}")
    if cached is not None:
        return cached
    client = _get_client()
    if not client:
        return 0.0
    try:
        resp = client.get_fills(product_id=product_key, limit=250)
        fills_raw = resp.get("fills", []) if isinstance(resp, dict) else getattr(resp, "fills", [])
        avg = _current_lot_average_from_fills(
            list(fills_raw or []),
            current_qty=current_qty,
        )
        _cache_set(f"cost_basis_{product_key}{qty_key}", avg)
        return avg
    except Exception as e:
        if _coinbase_product_unavailable_error(e):
            _mark_coinbase_product_unavailable(product_key)
            _cache_set(f"cost_basis_{product_key}{qty_key}", 0.0)
            return 0.0
        logger.debug("[coinbase] cost basis from fills failed for %s: %s", product_key, e)
        return 0.0


def get_positions(*, use_cache: bool = True) -> list[dict[str, Any]]:
    """Current crypto holdings with non-zero balances, with cost basis from fills."""
    if use_cache:
        cached = _cache_get("positions")
        if cached is not None:
            return cached
    accounts = get_accounts_raw(use_cache=use_cache)
    if not accounts:
        return []
    positions = []
    price_cache: dict[str, float] = {}
    for acc in accounts:
        bal = acc.get("available_balance", {})
        val = _balance_component_value(bal)
        currency = _account_currency(acc, bal)
        if currency == "USD":
            continue
        hold_bal = acc.get("hold", {})
        hold_val = _balance_component_value(hold_bal)
        total_qty = val + hold_val
        if total_qty <= 0:
            continue
        ticker = f"{currency}-USD"
        avg_price = _get_cost_basis_from_fills(ticker, current_qty=total_qty)
        current_price = _coinbase_current_price(ticker, price_cache)
        equity = total_qty * current_price if current_price > 0 else 0.0
        equity_change = (
            (current_price - avg_price) * total_qty
            if current_price > 0 and avg_price > 0
            else 0.0
        )
        percent_change = (
            ((current_price - avg_price) / avg_price) * 100.0
            if current_price > 0 and avg_price > 0
            else 0.0
        )
        positions.append({
            "ticker": ticker,
            "quantity": total_qty,
            "available_quantity": val,
            "held_quantity": hold_val,
            "average_buy_price": avg_price,
            "average_buy_price_source": "coinbase_current_lot_fills_fifo_with_fees",
            "equity": round(equity, 8),
            "market_value": round(equity, 8),
            "current_price": current_price,
            "equity_change": round(equity_change, 8),
            "percent_change": round(percent_change, 8),
            "name": acc.get("name", currency),
            "type": "crypto",
            "broker_source": "coinbase",
        })
    positions.sort(
        key=lambda p: (p.get("equity", 0) or 0, p.get("quantity", 0) or 0),
        reverse=True,
    )
    if use_cache:
        _cache_set("positions", positions)
    return positions


def get_fresh_positions() -> list[dict[str, Any]]:
    """Fresh Coinbase holdings for pre-trade broker-truth checks."""
    return get_positions(use_cache=False)


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


def _cancel_coinbase_stale_buy_orders_after_exit(
    ticker: str | None,
    *,
    exit_at: datetime | None,
) -> int:
    """Cancel stale zero-fill Coinbase BUY orders once a local exit fills.

    Scale-in orders can outlive the position they were meant to add to. After a
    confirmed SELL fill closes the broker position, any older zero-fill BUY for
    the same product is stale risk, not candidate supply. Newer buys are left
    alone because they may be a separate post-exit entry decision.
    """
    product_id = _to_product_id(str(ticker or ""))
    if not product_id:
        return 0
    cutoff = exit_at or datetime.utcnow()
    cancelled = 0
    try:
        orders = get_open_orders(product_ids=[product_id])
    except Exception:
        logger.warning(
            "[coinbase] stale BUY cleanup failed to list open orders for %s",
            product_id,
            exc_info=True,
        )
        return 0

    for order in orders or []:
        raw = _dictish(order)
        if not raw:
            continue
        oid = str(raw.get("order_id") or raw.get("id") or "").strip()
        if not oid:
            continue
        side = str(raw.get("side") or "").strip().upper()
        state = str(raw.get("status") or raw.get("state") or "").strip().lower()
        if side != "BUY" or state in _CB_TERMINAL_STATES:
            continue
        filled = _safe_float(
            raw.get("filled_size")
            or raw.get("filled_quantity")
            or raw.get("cumulative_quantity")
        )
        if filled > 0:
            continue
        created_at = _parse_cb_datetime(raw.get("created_time") or raw.get("created_at"))
        if created_at is not None and created_at > cutoff:
            continue
        result = cancel_order_by_id(oid)
        if isinstance(result, dict) and result.get("ok"):
            cancelled += 1
            logger.warning(
                "[coinbase] cancelled stale zero-fill BUY order %s for %s after exit_at=%s",
                oid,
                product_id,
                cutoff.isoformat() if hasattr(cutoff, "isoformat") else cutoff,
            )
        else:
            logger.warning(
                "[coinbase] stale zero-fill BUY cancel failed for %s %s: %s",
                product_id,
                oid,
                (result or {}).get("error") if isinstance(result, dict) else result,
            )
    return cancelled


def _mark_coinbase_trade_closed_sidecars(
    db: Session,
    trade: Any,
    *,
    reason: str,
) -> None:
    """Close bracket and position sidecars after a confirmed Coinbase exit."""
    try:
        from .trading.bracket_intent_writer import mark_closed

        intent_ids = db.execute(
            text(
                "SELECT id FROM trading_bracket_intents "
                "WHERE trade_id = :tid AND intent_state <> 'closed'"
            ),
            {"tid": int(getattr(trade, "id"))},
        ).scalars().all()
        for intent_id in intent_ids:
            mark_closed(db, int(intent_id), reason=reason[:128])
    except Exception:
        logger.debug(
            "[coinbase] bracket intent close failed for trade#%s",
            getattr(trade, "id", None),
            exc_info=True,
        )
    _close_coinbase_position_identity_for_trade(db, trade)


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
    stale_buys = _cancel_coinbase_stale_buy_orders_after_exit(
        getattr(trade, "ticker", None),
        exit_at=exit_at,
    )
    if stale_buys:
        trade.notes = (
            (trade.notes or "").rstrip()
            + "\nCancelled "
            + str(stale_buys)
            + " stale zero-fill Coinbase BUY order(s) after confirmed exit fill."
        ).strip()
        db.add(trade)
    _mark_coinbase_trade_closed_sidecars(
        db,
        trade,
        reason=str(exit_reason or "coinbase_pending_exit_filled"),
    )
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


def sync_pending_exit_for_trade(db: Session, trade: Any) -> dict[str, Any]:
    """Poll and reconcile a single Coinbase pending-exit order.

    This is intentionally callable from both broker sync and the live crypto
    exit monitor. A submitted exit is not enough to keep monitoring the local
    envelope forever; once Coinbase reports the SELL filled, broker truth closes
    the trade and clears stale pending state immediately.
    """
    pending_order_id = str(getattr(trade, "pending_exit_order_id", "") or "").strip()
    if not pending_order_id:
        return {"checked": False, "reason": "no_pending_exit_order"}
    cb_order = get_order_by_id(pending_order_id)
    if not cb_order:
        return {"checked": True, "error": "order_not_found"}

    cb_state = str(cb_order.get("status") or cb_order.get("state") or "").lower()
    now = datetime.utcnow()
    trade.pending_exit_status = cb_state or getattr(trade, "pending_exit_status", None)
    trade.last_broker_sync = now
    db.add(trade)

    if cb_state == "filled":
        pnl = _finalize_coinbase_pending_exit(
            db,
            trade,
            {**cb_order, "order_id": pending_order_id},
        )
        return {"checked": True, "closed": True, "pnl": pnl, "status": cb_state}

    if cb_state in _CB_TERMINAL_STATES:
        _clear_pending_exit_fields(trade)
        trade.broker_status = cb_state
        trade.last_broker_sync = now
        db.add(trade)
        db.commit()
        return {"checked": True, "cleared": True, "status": cb_state}

    db.commit()
    return {"checked": True, "pending": True, "status": cb_state}


def _dictish(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    return {}


def _coinbase_stale_close_fill(trade: Any) -> dict[str, Any] | None:
    """Best-effort broker-fill evidence for a position-sync inferred close.

    Position sync can observe "no position" before the order-sync path sees
    the matching exit order. In that case, use broker-side SELL fills rather
    than closing the local Trade with NULL exit/pnl.
    """
    ticker = str(getattr(trade, "ticker", "") or "").strip()
    if not ticker:
        return None
    product_id = _to_product_id(ticker)
    target_qty = _safe_float(getattr(trade, "quantity", None))
    entry_at = getattr(trade, "entry_date", None)

    pending_order_id = str(getattr(trade, "pending_exit_order_id", "") or "").strip()
    if pending_order_id:
        order = get_order_by_id(pending_order_id) or {}
        status = str(order.get("status") or order.get("state") or "").lower()
        side = str(order.get("side") or "").lower()
        px = (
            _safe_float(order.get("average_filled_price"))
            or _safe_float(order.get("average_price"))
            or _safe_float(order.get("price"))
        )
        qty = _safe_float(order.get("filled_size") or order.get("base_size")) or target_qty
        if px > 0 and qty > 0 and (not side or side == "sell") and status in ("filled", "done"):
            return {
                "price": px,
                "quantity": qty,
                "exit_at": (
                    _parse_cb_datetime(order.get("last_fill_time"))
                    or _parse_cb_datetime(order.get("completion_time"))
                    or _parse_cb_datetime(order.get("filled_at"))
                    or datetime.utcnow()
                ),
                "source": "pending_exit_order",
            }

    client = _get_client()
    if not client:
        return None
    try:
        resp = client.get_fills(product_id=product_id, limit=100)
    except Exception as exc:
        logger.debug("[coinbase] stale close fill lookup failed for %s: %s", product_id, exc)
        return None

    rd = _dictish(resp)
    raw_fills = (
        rd.get("fills")
        or rd.get("orders")
        or getattr(resp, "fills", None)
        or getattr(resp, "orders", None)
        or []
    )
    if not isinstance(raw_fills, list):
        return None

    sells: list[dict[str, Any]] = []
    for raw in raw_fills:
        fd = _dictish(raw)
        if not fd:
            continue
        fill_product = str(fd.get("product_id") or "").upper()
        if fill_product and fill_product != product_id.upper():
            continue
        if str(fd.get("side") or "").lower() != "sell":
            continue
        px = _safe_float(fd.get("price") or fd.get("trade_price"))
        qty = _safe_float(fd.get("size") or fd.get("trade_size"))
        if px <= 0 or qty <= 0:
            continue
        fill_at = (
            _parse_cb_datetime(fd.get("trade_time"))
            or _parse_cb_datetime(fd.get("created_time"))
            or _parse_cb_datetime(fd.get("filled_at"))
        )
        if entry_at is not None and fill_at is not None and fill_at < entry_at:
            continue
        sells.append({"price": px, "quantity": qty, "exit_at": fill_at or datetime.utcnow()})

    if not sells:
        return None
    sells.sort(key=lambda row: row["exit_at"], reverse=True)

    remaining = target_qty if target_qty > 0 else None
    total_qty = 0.0
    total_value = 0.0
    latest_at = sells[0]["exit_at"]
    for fill in sells:
        qty = float(fill["quantity"])
        if remaining is not None:
            if remaining <= 0:
                break
            qty = min(qty, remaining)
            remaining -= qty
        total_qty += qty
        total_value += qty * float(fill["price"])

    if total_qty <= 0 or total_value <= 0:
        return None
    if target_qty > 0 and total_qty < (target_qty * 0.95):
        logger.debug(
            "[coinbase] stale close sell fills below coverage for %s: %.8f < %.8f",
            product_id,
            total_qty,
            target_qty,
        )
        return None
    return {
        "price": total_value / total_qty,
        "quantity": total_qty,
        "exit_at": latest_at,
        "source": "recent_sell_fills",
    }


# ── Order sync (Coinbase → local DB) ────────────────────────────────

def sync_orders_to_db(db: Session, user_id: int | None) -> dict[str, int]:
    """Reconcile local trades with broker_source='coinbase' against Coinbase."""
    from ..models.trading import Trade, StrategyProposal
    from .trading.decision_ledger import mark_linked_trade_packets_executed, mark_linked_trade_packets_terminal
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
        ticker_for_log = getattr(trade, "ticker", "?")
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
            normalized_filled_qty = _safe_float(normalized.get("cumulative_filled_quantity"))
            terminal_zero_fill = cb_state == "filled" and normalized_filled_qty <= 0
            record_execution_event(
                db,
                user_id=trade.user_id,
                ticker=trade.ticker,
                trade=trade,
                scan_pattern_id=getattr(trade, "scan_pattern_id", None),
                apply_to_trade=not terminal_zero_fill,
                **normalized,
            )
            trade.last_broker_sync = now

            if terminal_zero_fill:
                fill_truth = _coinbase_order_fill_truth(trade.broker_order_id, trade.ticker)
                position_truth = _coinbase_position_truth_for_ticker(trade.ticker)
                broker_truth = position_truth or fill_truth
                if broker_truth is not None:
                    position_id = _ensure_coinbase_position_identity(
                        db,
                        trade=trade,
                        broker_payload=broker_truth,
                    )
                    _align_coinbase_trade_to_position_truth(
                        db,
                        trade=trade,
                        broker_payload=broker_truth,
                        position_id=position_id,
                    )
                    _ensure_coinbase_sync_entry_event(
                        db,
                        trade=trade,
                        broker_payload=broker_truth,
                        position_id=position_id,
                    )
                    mark_linked_trade_packets_executed(
                        db,
                        trade_id=int(trade.id),
                        source="coinbase_order_sync_broker_truth",
                        broker_order_id=trade.broker_order_id,
                    )
                    filled += 1
                    logger.warning(
                        "[coinbase] Order %s for %s reported FILLED with zero filled_size, "
                        "but %s showed qty=%s avg=%s; adopting broker truth",
                        trade.broker_order_id,
                        trade.ticker,
                        "position truth" if position_truth is not None else "fill truth",
                        broker_truth.get("quantity"),
                        broker_truth.get("average_buy_price"),
                    )
                    _update_proposal_on_fill(db, trade)
                    synced += 1
                    continue

                trade.status = "cancelled"
                trade.broker_status = "filled_zero_quantity"
                trade.filled_quantity = 0.0
                trade.remaining_quantity = 0.0
                trade.exit_reason = "coinbase_order_filled_zero_quantity"
                trade.last_broker_sync = now
                db.add(trade)
                mark_linked_trade_packets_terminal(
                    db,
                    trade_id=int(trade.id),
                    outcome_status="cancelled",
                    source="coinbase_order_sync",
                    reason_code="coinbase_order_filled_zero_quantity",
                    reason_text="Coinbase reported filled but filled_size was zero",
                    broker_order_id=trade.broker_order_id,
                )
                cancelled += 1
                logger.warning(
                    "[coinbase] Order %s for %s reported FILLED with zero filled_size; "
                    "treating as no-fill terminal",
                    trade.broker_order_id,
                    trade.ticker,
                )
                _update_proposal_on_cancel(db, trade, "filled_zero_quantity")

            elif cb_state == "filled":
                mark_linked_trade_packets_executed(
                    db,
                    trade_id=int(trade.id),
                    source="coinbase_order_sync",
                    broker_order_id=trade.broker_order_id,
                )
                filled += 1
                logger.info(
                    f"[coinbase] Order {trade.broker_order_id} for {trade.ticker} FILLED @ ${trade.avg_fill_price}"
                )
                _update_proposal_on_fill(db, trade)

            elif cb_state in _CB_TERMINAL_STATES and cb_state != "filled":
                mark_linked_trade_packets_terminal(
                    db,
                    trade_id=int(trade.id),
                    outcome_status="cancelled" if cb_state in ("cancelled", "expired") else "rejected",
                    source="coinbase_order_sync",
                    reason_code=f"coinbase_order_{cb_state}",
                    reason_text=f"Coinbase order ended {cb_state} with no fill",
                    broker_order_id=trade.broker_order_id,
                )
                cancelled += 1
                logger.info(f"[coinbase] Order {trade.broker_order_id} for {trade.ticker} {cb_state}")
                _update_proposal_on_cancel(db, trade, cb_state)

            synced += 1

        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning(f"[coinbase] Order sync failed for {ticker_for_log}: {e}")
            errors += 1

    for trade in open_with_pending_exit:
        ticker_for_log = getattr(trade, "ticker", "?")
        try:
            pending_order_id = str(trade.pending_exit_order_id or "")
            result = sync_pending_exit_for_trade(db, trade)
            if result.get("error"):
                errors += 1
                continue

            cb_state = str(result.get("status") or "").lower()
            if result.get("closed"):
                filled += 1
                logger.info(
                    "[coinbase] Pending exit %s for %s FILLED pnl=%s",
                    pending_order_id,
                    trade.ticker,
                    result.get("pnl"),
                )
            elif result.get("cleared"):
                cancelled += 1
                logger.info(
                    "[coinbase] Pending exit %s for %s %s; cleared pending exit",
                    pending_order_id,
                    trade.ticker,
                    cb_state,
                )

            synced += 1

        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning(f"[coinbase] Pending exit sync failed for {ticker_for_log}: {e}")
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


def _is_coinbase_sync_trade(trade: Any) -> bool:
    tags = str(getattr(trade, "tags", "") or "").lower()
    notes = str(getattr(trade, "notes", "") or "").lower()
    return (
        "coinbase-sync" in tags
        or "auto-synced from coinbase" in notes
        or not getattr(trade, "broker_order_id", None)
    )


def _canonical_coinbase_user_id(db: Session, requested_user_id: int | None) -> int | None:
    """Resolve the singleton Coinbase account's local owner id.

    Scheduler paths sometimes pass ``user_id=1`` while operator/manual syncs
    pass ``None``. Coinbase itself is one connected account here, so creating
    a NULL-user Trade when a user-owned Coinbase history exists splits one
    broker position into two local envelopes. Prefer the requested id; when
    absent, reuse the most recent non-NULL Coinbase owner if one exists.
    """
    if requested_user_id is not None:
        return requested_user_id
    try:
        from ..models.trading import Trade

        row = (
            db.query(Trade.user_id)
            .filter(Trade.broker_source == "coinbase", Trade.user_id.isnot(None))
            .order_by(Trade.id.desc())
            .first()
        )
        return int(row[0]) if row and row[0] is not None else None
    except Exception:
        return requested_user_id


def _coinbase_position_truth_for_ticker(ticker: str | None) -> dict[str, Any] | None:
    raw_ticker = str(ticker or "").strip()
    if not raw_ticker:
        return None
    product_id = _to_product_id(raw_ticker)
    if not product_id:
        return None
    try:
        for pos in dedupe_positions_by_ticker(get_positions()):
            if str(pos.get("ticker") or "").upper() != product_id:
                continue
            if _safe_float(pos.get("quantity")) > 0:
                return pos
    except Exception:
        logger.warning(
            "[coinbase] position-truth lookup failed for %s; deferring zero-fill adjudication",
            product_id,
            exc_info=True,
        )
    return None


def _coinbase_order_fill_truth(
    order_id: str | None,
    product_id: str | None,
) -> dict[str, Any] | None:
    """Return positive fill truth for an order when get_order is incomplete."""
    oid = str(order_id or "").strip()
    raw_product = str(product_id or "").strip()
    if not oid or not raw_product:
        return None
    product = _to_product_id(raw_product)
    if not product:
        return None
    client = _get_client()
    if not client:
        return None
    try:
        resp = client.get_fills(product_id=product, limit=250)
        fills_raw = resp.get("fills", []) if isinstance(resp, dict) else getattr(resp, "fills", [])
    except Exception:
        logger.warning(
            "[coinbase] fill-truth lookup failed order_id=%s product=%s",
            oid,
            product,
            exc_info=True,
        )
        return None

    matched = [
        _fill_payload(fill)
        for fill in fills_raw or []
        if str(_fill_payload(fill).get("order_id") or "") == oid
    ]
    buy_fills = [
        fill for fill in matched
        if str(fill.get("side") or "").strip().upper() == "BUY"
    ]
    if not buy_fills:
        return None
    qty = 0.0
    cost = 0.0
    latest_at: datetime | None = None
    for fill in buy_fills:
        size = _safe_float(fill.get("size") or fill.get("trade_size") or fill.get("filled_size"))
        price = _safe_float(fill.get("price") or fill.get("trade_price"))
        if size <= 0 or price <= 0:
            continue
        qty += size
        cost += price * size + _fill_commission_quote(fill)
        fill_at = _parse_cb_datetime(
            fill.get("trade_time")
            or fill.get("sequence_timestamp")
            or fill.get("created_time")
        )
        if fill_at is not None and (latest_at is None or fill_at > latest_at):
            latest_at = fill_at
    if qty <= 0 or cost <= 0:
        return None
    return {
        "ticker": product,
        "quantity": qty,
        "average_buy_price": round(cost / qty, 8),
        "average_buy_price_source": "coinbase_order_fills_with_fees",
        "broker_source": "coinbase",
        "order_id": oid,
        "filled_at": latest_at,
    }


def _ensure_coinbase_position_identity(
    db: Session,
    *,
    trade: Any,
    broker_payload: dict[str, Any] | None,
) -> int | None:
    """Mirror a Coinbase broker-observed position into position identity.

    Coinbase ``sync_positions_to_db`` can legitimately discover an open
    exchange position whose original order/fill event was missed by CHILI
    (manual order, transient worker outage, or maker-only order lifecycle
    race). Pre-fix, that path created/updated only the Trade envelope and
    left ``trading_positions`` plus ``position_id`` NULL, so Phase 4/5
    readers had no stable broker-position identity for the same inventory.

    This helper reuses the Phase 1 shadow writer from ``broker_service`` and
    then stamps the current Trade envelope onto the matched position row.
    Best-effort only: failures roll back the position sidecar writes and the
    caller continues syncing the Trade envelope.
    """
    try:
        from . import broker_service as _bs
        from .trading.position_resolver import resolve_position_id

        ticker = str(getattr(trade, "ticker", "") or "").strip()
        if not ticker:
            return None
        qty = float(
            (broker_payload or {}).get("quantity")
            or getattr(trade, "quantity", None)
            or 0.0
        )
        avg = (
            (broker_payload or {}).get("average_buy_price")
            or getattr(trade, "entry_price", None)
            or getattr(trade, "avg_fill_price", None)
        )
        avg_f = float(avg) if avg not in (None, "") else None
        direction = str(getattr(trade, "direction", None) or "long").lower()
        user_id = getattr(trade, "user_id", None)

        _bs._phase1_record_position_observation(
            db,
            user_id=user_id,
            broker_source="coinbase",
            account_type=_bs._resolve_account_type_for_position("coinbase", ticker),
            ticker=ticker,
            direction=direction,
            asset_kind=_bs._infer_asset_kind_for_position(ticker),
            broker_qty=qty,
            broker_avg=avg_f,
            broker_payload=broker_payload,
        )
        position_id = resolve_position_id(
            db,
            trade=trade,
            user_id=user_id,
            ticker=ticker,
            broker_source="coinbase",
            direction=direction,
        )
        if position_id is None:
            return None
        db.execute(
            text(
                "UPDATE trading_positions "
                "SET current_envelope_id = :trade_id, "
                "    current_quantity = :qty, "
                "    current_avg_price = COALESCE(:avg, current_avg_price), "
                "    state = 'open', "
                "    last_observed_at = NOW(), "
                "    updated_at = NOW() "
                "WHERE id = :pid"
            ),
            {
                "pid": int(position_id),
                "trade_id": int(getattr(trade, "id")),
                "qty": qty,
                "avg": avg_f,
            },
        )
        db.execute(
            text(
                "UPDATE trading_trades "
                "SET position_id = :pid "
                "WHERE id = :trade_id "
                "  AND (position_id IS NULL OR position_id <> :pid)"
            ),
            {"pid": int(position_id), "trade_id": int(getattr(trade, "id"))},
        )
        try:
            setattr(trade, "position_id", int(position_id))
        except Exception:
            pass
        db.execute(
            text(
                "UPDATE trading_bracket_intents "
                "SET position_id = :pid, updated_at = NOW() "
                "WHERE trade_id = :trade_id "
                "  AND (position_id IS NULL OR position_id <> :pid)"
            ),
            {"pid": int(position_id), "trade_id": int(getattr(trade, "id"))},
        )
        db.commit()
        return int(position_id)
    except Exception:
        logger.debug(
            "[coinbase] position-identity sidecar write failed for trade#%s",
            getattr(trade, "id", None),
            exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _ensure_coinbase_sync_entry_event(
    db: Session,
    *,
    trade: Any,
    broker_payload: dict[str, Any] | None,
    position_id: int | None,
) -> None:
    """Write one synthetic BUY fill for Coinbase auto-synced envelopes.

    The row is explicitly tagged ``synthetic`` so it is not mistaken for a
    broker websocket/order event, but it gives TCA, Phase 4, and Phase 5 a
    visible entry lineage for inventory CHILI discovered by broker sync.
    """
    if not _is_coinbase_sync_trade(trade):
        return
    try:
        exists = db.execute(
            text(
                "SELECT id FROM trading_execution_events "
                "WHERE trade_id = :trade_id "
                "  AND event_type = 'coinbase_position_sync_entry' "
                "LIMIT 1"
            ),
            {"trade_id": int(getattr(trade, "id"))},
        ).first()
        if exists:
            if position_id is not None:
                db.execute(
                    text(
                        "UPDATE trading_execution_events "
                        "SET position_id = :pid "
                        "WHERE id = :event_id "
                        "  AND position_id IS NULL"
                    ),
                    {"pid": int(position_id), "event_id": int(exists[0])},
                )
                db.commit()
            return
        from .trading.execution_audit import record_execution_event

        qty = float(
            getattr(trade, "quantity", None)
            or (broker_payload or {}).get("quantity")
            or 0.0
        )
        avg = (
            getattr(trade, "entry_price", None)
            or (broker_payload or {}).get("average_buy_price")
            or getattr(trade, "avg_fill_price", None)
        )
        avg_f = float(avg) if avg not in (None, "") else None
        payload = {
            "side": "buy",
            "source": "coinbase_position_sync",
            "synthetic": True,
            "position_id": position_id,
            "broker_position": broker_payload or {},
        }
        record_execution_event(
            db,
            user_id=getattr(trade, "user_id", None),
            ticker=getattr(trade, "ticker", None),
            trade=trade,
            scan_pattern_id=getattr(trade, "scan_pattern_id", None),
            broker_source="coinbase",
            event_type="coinbase_position_sync_entry",
            status="filled",
            requested_quantity=qty,
            cumulative_filled_quantity=qty,
            last_fill_quantity=qty,
            average_fill_price=avg_f,
            event_at=getattr(trade, "entry_date", None) or datetime.utcnow(),
            payload_json=payload,
        )
        db.commit()
    except Exception:
        logger.debug(
            "[coinbase] synthetic sync-entry event write failed for trade#%s",
            getattr(trade, "id", None),
            exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            pass


def _align_coinbase_trade_to_position_truth(
    db: Session,
    *,
    trade: Any,
    broker_payload: dict[str, Any] | None,
    position_id: int | None,
) -> bool:
    """Project Coinbase position truth onto the open management envelope.

    Order sync can miss or partially describe a Coinbase fill, but position
    sync has the broker-held inventory. Keep the Trade envelope aligned so
    UI P&L, exposure, scale-ins, and exit sizing do not run from stale
    ``filled_quantity`` or ``remaining_quantity`` fields.
    """
    payload = broker_payload or {}
    qty = _safe_float(payload.get("quantity"))
    if qty <= 0:
        return False
    status = str(getattr(trade, "status", "") or "").strip().lower()
    if status in {"closed", "cancelled", "rejected"}:
        return False

    avg = _safe_float(payload.get("average_buy_price"))
    now = datetime.utcnow()
    trade.quantity = qty
    trade.filled_quantity = qty
    trade.remaining_quantity = 0.0
    trade.status = "open"
    trade.broker_status = "filled"
    trade.last_broker_sync = now
    if getattr(trade, "filled_at", None) is None:
        trade.filled_at = now
    if avg > 0:
        trade.entry_price = avg
        trade.avg_fill_price = avg
        try:
            from .trading.tca_service import apply_tca_on_trade_fill

            apply_tca_on_trade_fill(trade)
        except Exception as exc:
            logger.debug(
                "[coinbase] entry TCA projection failed for trade#%s: %s",
                getattr(trade, "id", None),
                type(exc).__name__,
            )
    if position_id is not None:
        try:
            trade.position_id = int(position_id)
        except Exception:
            pass
    db.add(trade)
    try:
        db.execute(
            text(
                """
                UPDATE trading_bracket_intents
                   SET quantity = :qty,
                       entry_price = COALESCE(:avg, entry_price),
                       position_id = COALESCE(:pid, position_id),
                       last_diff_reason = CASE
                           WHEN ABS(COALESCE(quantity, 0) - :qty) > 1e-8
                               THEN 'coinbase_position_truth_quantity'
                           ELSE last_diff_reason
                       END,
                       updated_at = NOW()
                 WHERE trade_id = :trade_id
                   AND intent_state NOT IN ('closed', 'reconciled', 'terminal_reject')
                """
            ),
            {
                "trade_id": int(getattr(trade, "id")),
                "qty": qty,
                "avg": avg if avg > 0 else None,
                "pid": int(position_id) if position_id is not None else None,
            },
        )
    except Exception:
        logger.debug(
            "[coinbase] bracket-intent quantity projection failed for trade#%s",
            getattr(trade, "id", None),
            exc_info=True,
        )

    try:
        exists = db.execute(
            text(
                """
                SELECT id
                FROM trading_execution_events
                WHERE trade_id = :trade_id
                  AND event_type = 'coinbase_position_sync_inventory'
                LIMIT 1
                """
            ),
            {"trade_id": int(getattr(trade, "id"))},
        ).first()
        if exists is None:
            from .trading.execution_audit import record_execution_event

            record_execution_event(
                db,
                user_id=getattr(trade, "user_id", None),
                ticker=getattr(trade, "ticker", None),
                trade=trade,
                scan_pattern_id=getattr(trade, "scan_pattern_id", None),
                broker_source="coinbase",
                event_type="coinbase_position_sync_inventory",
                status="filled",
                requested_quantity=qty,
                cumulative_filled_quantity=qty,
                last_fill_quantity=qty,
                average_fill_price=avg if avg > 0 else None,
                event_at=now,
                payload_json={
                    "side": "buy",
                    "source": "coinbase_position_sync",
                    "synthetic": True,
                    "position_id": position_id,
                    "broker_position": payload,
                },
                apply_to_trade=False,
            )
    except Exception:
        logger.debug(
            "[coinbase] position-truth execution event failed for trade#%s",
            getattr(trade, "id", None),
            exc_info=True,
        )
    return True


def _coinbase_has_working_sell_orders(ticker: str | None) -> bool:
    """Best-effort stale-close guard for Coinbase ticker-level flaps."""
    if not ticker:
        return False
    try:
        from .trading.venue.factory import get_adapter

        adapter = get_adapter("coinbase")
        if adapter is None:
            return True
        orders, _fresh = adapter.list_open_orders(product_id=str(ticker), limit=50)
        for order in orders or []:
            side = str(getattr(order, "side", "") or "").lower()
            status = str(getattr(order, "status", "") or "").lower()
            if side != "sell":
                continue
            if status in (
                "open", "active", "working", "queued", "confirmed", "pending",
                "submitted", "accepted", "partially_filled", "unconfirmed",
            ):
                return True
    except Exception:
        logger.warning(
            "[coinbase] open-order stale-close guard failed for %s; "
            "treating broker order state as unknown and refusing stale-close",
            ticker,
            exc_info=True,
        )
        return True
    return False


def _coinbase_trade_has_recorded_real_sell(db: Session, trade: Any) -> bool:
    """True only when a non-synthetic broker sell fill is attached to trade."""
    try:
        row = db.execute(
            text(
                """
                SELECT 1 FROM trading_execution_events
                WHERE trade_id = :tid
                  AND status = 'filled'
                  AND LOWER(payload_json->>'side') = 'sell'
                  AND COALESCE(LOWER(payload_json->>'synthetic'), 'false') NOT IN (
                      'true', '1', 'yes'
                  )
                  AND COALESCE(LOWER(payload_json->>'source'), '') NOT IN (
                      'coinbase_position_sync_gone',
                      'broker_reconcile_position_gone',
                      'broker_reconcile_no_exit_price',
                      'forced_unwind_reconcile',
                      'zombie_reconcile_orphan'
                  )
                LIMIT 1
                """
            ),
            {"tid": int(getattr(trade, "id"))},
        ).first()
        return row is not None
    except Exception:
        logger.debug(
            "[coinbase] real-sell check failed for trade#%s",
            getattr(trade, "id", None),
            exc_info=True,
        )
        return True


def _coinbase_position_has_recorded_real_sell(db: Session, trade: Any) -> bool:
    """Position-level real-sell check; falls back to trade-level audit."""
    try:
        from .trading.position_resolver import (
            position_has_recorded_sell,
            resolve_position_id,
        )

        position_id = getattr(trade, "position_id", None)
        if position_id is None:
            position_id = resolve_position_id(
                db,
                trade=trade,
                user_id=getattr(trade, "user_id", None),
                ticker=getattr(trade, "ticker", None),
                broker_source="coinbase",
                direction=getattr(trade, "direction", None) or "long",
            )
        if position_id is not None:
            return bool(position_has_recorded_sell(db, int(position_id)))
    except Exception:
        logger.debug(
            "[coinbase] position real-sell check failed for trade#%s",
            getattr(trade, "id", None),
            exc_info=True,
        )
        return True
    return _coinbase_trade_has_recorded_real_sell(db, trade)


def _try_reopen_coinbase_bookkeeping_trade(
    db: Session,
    *,
    canonical_user_id: int | None,
    ticker: str,
    qty: float,
    avg_price: float | None,
    broker_payload: dict[str, Any] | None,
) -> Any | None:
    """Inverse-reconcile a recent Coinbase bookkeeping close.

    If Coinbase still reports inventory for ``ticker`` and the most recent
    local row was closed only by ``coinbase_position_sync_gone`` with no real
    sell fill recorded, reopen the existing management envelope rather than
    creating a new one. This preserves the pattern/bracket lineage that the
    monitor and learning loops need.
    """
    try:
        from ..models.trading import Trade

        cutoff = datetime.utcnow() - timedelta(
            hours=_COINBASE_BOOKKEEPING_REOPEN_LOOKBACK_HOURS
        )
        q = db.query(Trade).filter(
            Trade.ticker == ticker,
            Trade.broker_source == "coinbase",
            Trade.status == "closed",
            Trade.exit_reason == "coinbase_position_sync_gone",
            Trade.exit_date.isnot(None),
            Trade.exit_date >= cutoff,
        )
        if canonical_user_id is not None:
            q = q.filter(or_(Trade.user_id == canonical_user_id, Trade.user_id.is_(None)))
        else:
            q = q.filter(Trade.user_id.is_(None))
        candidate = q.order_by(Trade.exit_date.desc(), Trade.id.desc()).first()
        if candidate is None:
            return None

        local_qty = float(getattr(candidate, "quantity", 0.0) or 0.0)
        broker_qty = float(qty or 0.0)
        qty_match = abs(local_qty - broker_qty) <= max(1e-8, broker_qty * 1e-6)
        if not qty_match:
            logger.warning(
                "[coinbase] inverse reconcile skipped for %s trade#%s: "
                "qty mismatch local=%s broker=%s",
                ticker,
                candidate.id,
                local_qty,
                broker_qty,
            )
            return None

        if _coinbase_position_has_recorded_real_sell(db, candidate):
            logger.error(
                "[coinbase] inverse reconcile contradiction for %s trade#%s: "
                "Coinbase reports qty=%s but a real sell is recorded. "
                "Leaving row closed for operator review.",
                ticker,
                candidate.id,
                broker_qty,
            )
            return None

        prior_exit_reason = candidate.exit_reason or "<unset>"
        candidate.status = "open"
        candidate.exit_date = None
        candidate.exit_price = None
        candidate.exit_reason = None
        candidate.pnl = None
        candidate.quantity = broker_qty
        if avg_price is not None and avg_price > 0:
            candidate.entry_price = float(avg_price)
        candidate.broker_status = "filled"
        candidate.last_broker_sync = datetime.utcnow()
        candidate.broker_sync_missing_streak = 0
        if candidate.user_id is None and canonical_user_id is not None:
            candidate.user_id = canonical_user_id
        _clear_pending_exit_fields(candidate)
        db.add(candidate)
        db.flush()

        if not candidate.related_alert_id:
            _link_latest_alert(db, candidate)
        position_id = _ensure_coinbase_position_identity(
            db,
            trade=candidate,
            broker_payload=broker_payload,
        )
        _ensure_coinbase_sync_entry_event(
            db,
            trade=candidate,
            broker_payload=broker_payload,
            position_id=position_id,
        )
        db.execute(
            text(
                "UPDATE trading_bracket_intents "
                "SET intent_state = 'intent', "
                "    quantity = :qty, "
                "    entry_price = COALESCE(:avg, entry_price), "
                "    position_id = COALESCE(:pid, position_id), "
                "    last_diff_reason = 'coinbase_inverse_reconcile_reopen', "
                "    updated_at = NOW() "
                "WHERE trade_id = :tid "
                "  AND intent_state IN ('closed','reconciled','terminal_reject')"
            ),
            {
                "tid": int(candidate.id),
                "qty": broker_qty,
                "avg": float(avg_price) if avg_price is not None and avg_price > 0 else None,
                "pid": int(position_id) if position_id is not None else None,
            },
        )
        logger.warning(
            "[coinbase] INVERSE RECONCILE: re-opened trade#%s %s qty=%s avg=%s "
            "(prior exit_reason=%s; broker still reports position)",
            candidate.id,
            ticker,
            broker_qty,
            avg_price,
            prior_exit_reason,
        )
        return candidate
    except Exception:
        logger.exception("[coinbase] inverse reconcile failed for %s", ticker)
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _close_coinbase_position_identity_for_trade(db: Session, trade: Any) -> None:
    """Close the position-identity row only after a confirmed Trade close."""
    try:
        from .trading.position_resolver import resolve_position_id

        position_id = getattr(trade, "position_id", None)
        if position_id is None:
            position_id = resolve_position_id(
                db,
                trade=trade,
                user_id=getattr(trade, "user_id", None),
                ticker=getattr(trade, "ticker", None),
                broker_source="coinbase",
                direction=getattr(trade, "direction", None) or "long",
            )
        if position_id is None:
            return
        db.execute(
            text(
                "UPDATE trading_positions "
                "SET state = 'closed', current_quantity = 0, "
                "    last_state_transition_at = NOW(), updated_at = NOW() "
                "WHERE id = :pid AND state = 'open'"
            ),
            {"pid": int(position_id)},
        )
        db.execute(
            text(
                "INSERT INTO trading_position_events ("
                "  position_id, event_type, transition_reason, quantity, "
                "  envelope_id, observed_at"
                ") VALUES ("
                "  :pid, 'closed', 'coinbase_position_sync_gone', 0, "
                "  :tid, NOW()"
                ")"
            ),
            {"pid": int(position_id), "tid": int(getattr(trade, "id"))},
        )
    except Exception:
        logger.debug(
            "[coinbase] position-identity close failed for trade#%s",
            getattr(trade, "id", None),
            exc_info=True,
        )


def _close_coinbase_absent_no_fill_trade(
    db: Session,
    trade: Any,
    *,
    missing_streak: int,
    live_ticker_count: int,
) -> None:
    """Close stale local Coinbase bookkeeping without inventing PnL.

    This is the strong-evidence fallback after repeated non-empty Coinbase
    snapshots omit the ticker, no working sell order exists, and no sell fill is
    recoverable. The broker is flat, but the execution price is unknown.
    """
    now = datetime.utcnow()
    trade.status = "closed"
    trade.exit_date = now
    trade.exit_price = None
    trade.pnl = None
    trade.exit_reason = "broker_reconcile_no_exit_price"
    trade.broker_status = "no_position"
    trade.last_broker_sync = now
    _clear_pending_exit_fields(trade)
    trade.notes = (
        (trade.notes or "")
        + "\nAuto-closed: Coinbase omitted this asset from repeated non-empty "
        + f"broker snapshots at {now.strftime('%Y-%m-%d %H:%M')} UTC "
        + f"(missing_streak={missing_streak}, live_tickers={live_ticker_count}) "
        + "and no confirming sell fill was recoverable. Exit price unknown; "
        + "PnL left NULL."
    )
    db.add(trade)

    try:
        from .trading.brain_work.execution_hooks import on_broker_reconciled_close

        on_broker_reconciled_close(db, trade, source="coinbase_absent_no_fill")
    except Exception:
        logger.debug(
            "[coinbase] no-fill broker close hook failed for trade#%s",
            getattr(trade, "id", None),
            exc_info=True,
        )

    try:
        from .trading.execution_audit import record_execution_event

        record_execution_event(
            db,
            user_id=getattr(trade, "user_id", None),
            ticker=getattr(trade, "ticker", None),
            trade=trade,
            scan_pattern_id=getattr(trade, "scan_pattern_id", None),
            broker_source="coinbase",
            order_id=None,
            event_type="coinbase_position_absent_no_fill_close",
            status="closed",
            requested_quantity=float(getattr(trade, "quantity", 0.0) or 0.0),
            cumulative_filled_quantity=0.0,
            average_fill_price=None,
            payload_json={
                "side": "sell",
                "synthetic": True,
                "source": "coinbase_position_sync",
                "reason": "broker_reconcile_no_exit_price",
                "confirmation": "repeated_non_empty_snapshot_absent",
                "missing_streak": missing_streak,
                "live_ticker_count": live_ticker_count,
            },
        )
    except Exception:
        logger.debug(
            "[coinbase] no-fill close execution_event failed for trade#%s",
            getattr(trade, "id", None),
            exc_info=True,
        )

    try:
        from .trading.bracket_intent_writer import mark_closed

        intent_ids = db.execute(
            text(
                "SELECT id FROM trading_bracket_intents "
                "WHERE trade_id = :tid AND intent_state <> 'closed'"
            ),
            {"tid": int(getattr(trade, "id"))},
        ).scalars().all()
        for intent_id in intent_ids:
            mark_closed(
                db,
                int(intent_id),
                reason="coinbase_absent_no_fill_reconcile",
            )
    except Exception:
        logger.debug(
            "[coinbase] no-fill bracket close failed for trade#%s",
            getattr(trade, "id", None),
            exc_info=True,
        )

    trade.broker_status = "no_position"
    trade.exit_reason = "broker_reconcile_no_exit_price"
    _close_coinbase_position_identity_for_trade(db, trade)


def _cancel_duplicate_coinbase_entry_orders(
    db: Session,
    *,
    canonical_user_id: int | None,
    ticker: str,
    broker_position_qty: float,
    canonical_trade_id: int | None,
) -> int:
    """Cancel zero-fill Coinbase BUY orders once broker position exists.

    Scale-ins mutate the existing open Trade envelope. A separate working BUY
    for the same Coinbase product while the broker already holds inventory is
    a duplicate-entry risk, especially after order-status/position-status races.
    Cancel only if Coinbase still reports the order open, BUY-side, and with no
    filled quantity.
    """
    from ..models.trading import Trade

    product_id = _to_product_id(ticker)
    q = db.query(Trade).filter(
        Trade.ticker == product_id,
        Trade.broker_source == "coinbase",
        Trade.status == "working",
        Trade.broker_order_id.isnot(None),
    )
    if canonical_user_id is not None:
        q = q.filter(or_(Trade.user_id == canonical_user_id, Trade.user_id.is_(None)))
    else:
        q = q.filter(Trade.user_id.is_(None))
    candidates = q.order_by(Trade.id.desc()).all()
    cancelled = 0
    for trade in candidates:
        if canonical_trade_id is not None and int(trade.id) == int(canonical_trade_id):
            continue
        if _safe_float(getattr(trade, "filled_quantity", None)) > 0:
            continue
        order = get_order_by_id(str(trade.broker_order_id))
        if not order:
            continue
        side = str(order.get("side") or "").strip().upper()
        if side and side != "BUY":
            continue
        filled_qty = _safe_float(
            order.get("filled_size")
            or order.get("filled_quantity")
            or order.get("cumulative_quantity")
        )
        if filled_qty > 0:
            continue
        state = str(order.get("status") or order.get("state") or "").strip().lower()
        if state in {"filled", "done"}:
            # The order endpoint is contradictory; let the zero-fill/fill-truth
            # path adjudicate instead of cancelling a possibly filled order.
            continue
        if state in _CB_TERMINAL_STATES:
            trade.status = "cancelled" if state in {"cancelled", "expired"} else "rejected"
            trade.broker_status = state
            trade.exit_reason = f"coinbase_duplicate_entry_terminal:{state}"[:50]
            trade.last_broker_sync = datetime.utcnow()
            db.add(trade)
            cancelled += 1
            continue
        result = cancel_order_by_id(str(trade.broker_order_id))
        if not isinstance(result, dict) or not result.get("ok"):
            logger.warning(
                "[coinbase] duplicate BUY cancel failed ticker=%s trade#%s order=%s err=%s",
                product_id,
                trade.id,
                trade.broker_order_id,
                (result or {}).get("error") if isinstance(result, dict) else result,
            )
            continue
        now = datetime.utcnow()
        trade.status = "cancelled"
        trade.broker_status = "cancelled"
        trade.remaining_quantity = _safe_float(getattr(trade, "remaining_quantity", None))
        trade.exit_reason = "coinbase_duplicate_entry_live_position"
        trade.last_broker_sync = now
        note = (
            f"Cancelled duplicate Coinbase BUY on {now.strftime('%Y-%m-%d %H:%M')} "
            f"because broker already held {broker_position_qty:g} {product_id}"
            + (
                f" on trade_id={canonical_trade_id}."
                if canonical_trade_id is not None
                else "."
            )
        )
        trade.notes = ((trade.notes or "").rstrip() + "\n" + note).strip()
        db.add(trade)
        _update_proposal_on_cancel(db, trade, "duplicate_entry_live_position")
        logger.warning(
            "[coinbase] cancelled duplicate BUY order ticker=%s trade#%s order=%s; "
            "broker position qty=%s canonical_trade_id=%s",
            product_id,
            trade.id,
            trade.broker_order_id,
            broker_position_qty,
            canonical_trade_id,
        )
        cancelled += 1
    return cancelled


def sync_positions_to_db(db: Session, user_id: int | None) -> dict[str, int]:
    """Sync Coinbase positions into local Trade model."""
    from ..models.trading import BreakoutAlert, Trade

    if not is_connected():
        return {"created": 0, "updated": 0, "closed": 0, "reopened": 0}

    canonical_user_id = _canonical_coinbase_user_id(db, user_id)

    acquire_broker_position_sync_lock(
        db, broker_source="coinbase", user_id=canonical_user_id,
    )
    cleanup = collapse_open_broker_position_duplicates(
        db, broker_source="coinbase", user_id=canonical_user_id,
    )

    all_positions = dedupe_positions_by_ticker(get_positions())
    created = updated = closed = reopened = duplicate_entry_cancelled = 0
    cb_tickers: set[str] = set()

    for pos in all_positions:
        ticker = pos["ticker"]
        cb_tickers.add(ticker)
        qty = pos.get("quantity", 0)
        if not qty or qty <= 0:
            continue

        # Coinbase is one broker account in this app. Some scheduler paths
        # call with user_id=1 while operator/manual syncs call with
        # user_id=None; matching strictly on user_id split the same exchange
        # position into duplicate Trade envelopes. Prefer the requested user
        # when present, but fall back to any open Coinbase envelope for the
        # same ticker before creating a new one.
        existing_q = db.query(Trade).filter(
            Trade.ticker == ticker,
            Trade.broker_source == "coinbase",
            Trade.status == "open",
        )
        existing = None
        if canonical_user_id is not None:
            existing = existing_q.filter(Trade.user_id == canonical_user_id).first()
        if existing is None:
            existing = existing_q.order_by(Trade.id.desc()).first()

        if existing:
            if existing.user_id is None and canonical_user_id is not None:
                existing.user_id = canonical_user_id
            existing.quantity = qty
            existing.last_broker_sync = datetime.utcnow()
            avg_price = pos.get("average_buy_price", 0)
            if avg_price and avg_price > 0 and (not existing.entry_price or existing.entry_price == 0):
                existing.entry_price = avg_price
            if not existing.related_alert_id:
                _link_latest_alert(db, existing)
            position_id = _ensure_coinbase_position_identity(
                db, trade=existing, broker_payload=pos,
            )
            _ensure_coinbase_sync_entry_event(
                db, trade=existing, broker_payload=pos, position_id=position_id,
            )
            _align_coinbase_trade_to_position_truth(
                db, trade=existing, broker_payload=pos, position_id=position_id,
            )
            duplicate_entry_cancelled += _cancel_duplicate_coinbase_entry_orders(
                db,
                canonical_user_id=canonical_user_id,
                ticker=ticker,
                broker_position_qty=float(qty or 0.0),
                canonical_trade_id=int(existing.id),
            )
            updated += 1
        else:
            avg_price = pos.get("average_buy_price", 0)
            if not avg_price or avg_price <= 0:
                logger.warning(
                    "[coinbase] Skipping auto-create for %s: cost basis unavailable",
                    ticker,
                )
                continue
            reopened_trade = _try_reopen_coinbase_bookkeeping_trade(
                db,
                canonical_user_id=canonical_user_id,
                ticker=ticker,
                qty=qty,
                avg_price=avg_price,
                broker_payload=pos,
            )
            if reopened_trade is not None:
                duplicate_entry_cancelled += _cancel_duplicate_coinbase_entry_orders(
                    db,
                    canonical_user_id=canonical_user_id,
                    ticker=ticker,
                    broker_position_qty=float(qty or 0.0),
                    canonical_trade_id=int(reopened_trade.id),
                )
                reopened += 1
                continue
            # f-coinbase-dust-auto-create-skip (2026-05-19): refuse auto-
            # create when the notional dollar value (avg_price * qty) is
            # below ``_MIN_AUTO_CREATE_NOTIONAL_USD``. Coinbase's
            # ``quote_min_size`` is typically $1; positions below this
            # cannot place new orders against them. Without this guard,
            # operator-side dust holdings (e.g. 0.269 ACS at $0.00019 =
            # $0.00005 notional, or 2.18e-06 BNB at $680 = $0.0015) got
            # auto-created as trades that subsequently got closed via
            # ``coinbase_position_sync_gone`` whenever the wallet's tiny
            # holding transiently dropped out of ``cb_tickers`` -- the
            # phantom round-trip cycle that burned the autotrader's
            # ``coinbase_cap:venue_notional_cap_exceeded`` quota (133
            # blocks in 24h) and obscured the autotrader's real intent.
            notional_usd = float(avg_price) * float(qty)
            if notional_usd < _MIN_AUTO_CREATE_NOTIONAL_USD:
                logger.warning(
                    "[coinbase] Skipping auto-create for %s: dust notional "
                    "$%.5f < $%.2f min (qty=%s, avg_price=%s)",
                    ticker,
                    notional_usd,
                    _MIN_AUTO_CREATE_NOTIONAL_USD,
                    qty,
                    avg_price,
                )
                continue
            trade = Trade(
                user_id=canonical_user_id,
                ticker=ticker,
                direction="long",
                entry_price=avg_price,
                quantity=qty,
                status="open",
                broker_source="coinbase",
                tags="coinbase-sync",
                stop_model="atr_crypto_breakout",
                last_broker_sync=datetime.utcnow(),
                notes=f"Auto-synced from Coinbase on {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            )
            db.add(trade)
            db.flush()
            _link_latest_alert(db, trade)
            position_id = _ensure_coinbase_position_identity(
                db, trade=trade, broker_payload=pos,
            )
            _ensure_coinbase_sync_entry_event(
                db, trade=trade, broker_payload=pos, position_id=position_id,
            )
            duplicate_entry_cancelled += _cancel_duplicate_coinbase_entry_orders(
                db,
                canonical_user_id=canonical_user_id,
                ticker=ticker,
                broker_position_qty=float(qty or 0.0),
                canonical_trade_id=int(trade.id),
            )
            created += 1

    if cb_tickers:
        try:
            db.query(Trade).filter(
                Trade.broker_source == "coinbase",
                Trade.status == "open",
                Trade.ticker.notin_(cb_tickers),
            ).update(
                {
                    Trade.broker_sync_missing_streak:
                        func.coalesce(Trade.broker_sync_missing_streak, 0) + 1
                },
                synchronize_session=False,
            )
            db.query(Trade).filter(
                Trade.broker_source == "coinbase",
                Trade.status == "open",
                Trade.ticker.in_(cb_tickers),
            ).update(
                {Trade.broker_sync_missing_streak: 0},
                synchronize_session=False,
            )
            logger.debug(
                "[coinbase] partial-list streak updated; "
                "cb_tickers_size=%d threshold=%d",
                len(cb_tickers),
                _COINBASE_RECONCILE_MISSING_STREAK_MIN,
            )
        except Exception:
            logger.warning(
                "[coinbase] partial-list streak bulk UPDATE failed; "
                "stale-close gate will use existing streak values",
                exc_info=True,
            )

    if cb_tickers:
        stale = (
            db.query(Trade)
            .filter(
                Trade.broker_source == "coinbase",
                Trade.status == "open",
                Trade.ticker.notin_(cb_tickers),
            )
            .all()
        )
    else:
        # R32-equivalent Coinbase guard: never close every open local
        # Coinbase envelope off a single all-empty broker snapshot. Coinbase
        # can legitimately return [] during transient API/egress failures;
        # mass-closing would then free caps, recreate the same position on
        # the next healthy sweep, and manufacture phantom PnL. A real "all
        # positions sold" event can be reconciled once a non-empty broker
        # snapshot returns or by operator/manual close.
        open_count = (
            db.query(Trade)
            .filter(
                Trade.broker_source == "coinbase",
                Trade.status == "open",
            )
            .count()
        )
        if open_count:
            logger.warning(
                "[coinbase] Position sync returned zero live tickers while "
                "%d local Coinbase trade(s) are open; skipping stale-close "
                "this sweep to avoid an all-empty snapshot wipeout.",
                open_count,
            )
        stale = []

    for trade in stale:
        streak = getattr(trade, "broker_sync_missing_streak", 0) or 0
        if streak < _COINBASE_RECONCILE_MISSING_STREAK_MIN:
            logger.debug(
                "[coinbase] %s missing from Coinbase but streak=%d < threshold=%d "
                "(partial-list guard) -- deferring close",
                trade.ticker,
                streak,
                _COINBASE_RECONCILE_MISSING_STREAK_MIN,
            )
            continue
        refs = [
            getattr(trade, "last_broker_sync", None),
            getattr(trade, "submitted_at", None),
            getattr(trade, "entry_date", None),
        ]
        ref_ts = max((r for r in refs if r is not None), default=None)
        if ref_ts is not None and (
            (datetime.utcnow() - ref_ts).total_seconds()
            < _COINBASE_RECONCILE_CONFIRM_WINDOW
        ):
            logger.debug(
                "[coinbase] %s missing from Coinbase but within %ds confirm "
                "window -- deferring close",
                trade.ticker,
                _COINBASE_RECONCILE_CONFIRM_WINDOW,
            )
            continue
        if _coinbase_has_working_sell_orders(trade.ticker):
            logger.warning(
                "[coinbase] Skipping stale-close for %s trade#%s: ticker "
                "missing from positions snapshot but Coinbase still reports "
                "working sell order(s). Treating as transient broker snapshot "
                "gap, not a closed position.",
                trade.ticker,
                trade.id,
            )
            continue
        close_fill = _coinbase_stale_close_fill(trade)
        if close_fill is None:
            absent_no_fill_streak = int(
                getattr(trade, "broker_sync_missing_streak", 0) or 0
            )
            ref_age_ok = True
            if ref_ts is not None:
                ref_age_ok = (
                    (datetime.utcnow() - ref_ts).total_seconds()
                    >= _COINBASE_ABSENT_NO_FILL_MIN_AGE_SECONDS
                )
            if (
                absent_no_fill_streak >= _COINBASE_ABSENT_NO_FILL_STREAK_MIN
                and ref_age_ok
            ):
                _close_coinbase_absent_no_fill_trade(
                    db,
                    trade,
                    missing_streak=absent_no_fill_streak,
                    live_ticker_count=len(cb_tickers),
                )
                closed += 1
                continue
            logger.warning(
                "[coinbase] Skipping stale-close for %s trade#%s: missing "
                "from current Coinbase position snapshot but no confirming "
                "sell fill was found. Keeping trade open/monitored because "
                "position snapshots can be partial or stale. "
                "missing_streak=%s absent_no_fill_threshold=%s min_age_sec=%s "
                "ref_age_ok=%s",
                trade.ticker,
                trade.id,
                absent_no_fill_streak,
                _COINBASE_ABSENT_NO_FILL_STREAK_MIN,
                _COINBASE_ABSENT_NO_FILL_MIN_AGE_SECONDS,
                ref_age_ok,
            )
            continue
        trade.status = "closed"
        trade.exit_date = (
            close_fill.get("exit_at") if close_fill is not None else datetime.utcnow()
        )
        if close_fill is not None:
            exit_px = float(close_fill["price"])
            qty = float(close_fill.get("quantity") or trade.quantity or 0.0)
            entry = float(trade.entry_price or 0.0)
            trade.exit_price = exit_px
            if qty > 0:
                trade.quantity = qty
            if entry > 0 and qty > 0:
                pnl = (exit_px - entry) * qty
                if str(trade.direction or "long").lower() == "short":
                    pnl = -pnl
                trade.pnl = round(pnl, 4)
            trade.broker_status = "filled"
            trade.last_broker_sync = trade.exit_date
            _clear_pending_exit_fields(trade)
        if not trade.exit_reason:
            trade.exit_reason = "coinbase_position_sync_gone"
        trade.notes = (
            (trade.notes or "")
            + f"\nAuto-closed: position no longer on Coinbase ({datetime.utcnow().strftime('%Y-%m-%d %H:%M')})"
        )
        if close_fill is not None:
            trade.notes += f" [exit priced from {close_fill['source']}]"
        _clear_pending_exit_fields(trade)
        try:
            from .trading.brain_work.execution_hooks import on_broker_reconciled_close

            on_broker_reconciled_close(db, trade, source="coinbase_position_sync")
        except Exception:
            logger.debug("[coinbase] brain_work broker close hook failed", exc_info=True)

        # f-coinbase-exit-side-recording (2026-05-19): write a
        # synthetic sell-side execution_events row for this auto-close.
        # The position vanished from Coinbase (could be a real fill we
        # didn't observe, a manual sell, a transfer, etc.). For the
        # Phase 4 helper ``position_has_recorded_sell``'s purposes we
        # treat it as a sell -- the position IS closed at the broker.
        # Wrapped in try/except: this is observability-only and must
        # never block the close.
        _event_trade_id = int(getattr(trade, "id", 0) or 0)
        try:
            from .trading.execution_audit import record_execution_event

            _exit_px = float(close_fill["price"]) if close_fill is not None else None
            _exit_qty = (
                float(close_fill.get("quantity") or trade.quantity or 0.0)
                if close_fill is not None
                else (float(trade.quantity or 0.0) if trade.quantity else None)
            )
            _payload = {
                "side": "sell",
                "source": "coinbase_position_sync_gone",
                "trade_id": _event_trade_id,
                "exit_reason": trade.exit_reason,
                "synthetic": True,
            }
            if close_fill is not None:
                _payload["close_fill_source"] = close_fill.get("source")
            with db.begin_nested():
                record_execution_event(
                    db,
                    user_id=trade.user_id,
                    ticker=trade.ticker,
                    trade=trade,
                    scan_pattern_id=getattr(trade, "scan_pattern_id", None),
                    broker_source="coinbase",
                    event_type="coinbase_sync_gone_close",
                    status="filled",
                    average_fill_price=_exit_px,
                    cumulative_filled_quantity=_exit_qty,
                    payload_json=_payload,
                )
        except Exception:
            logger.debug(
                "[coinbase] sell-side execution_event write failed for trade#%s "
                "(non-fatal — Phase 4 visibility only)",
                _event_trade_id, exc_info=True,
            )

        try:
            from .trading.bracket_intent_writer import mark_closed

            _intent_ids = db.execute(
                text(
                    "SELECT id FROM trading_bracket_intents "
                    "WHERE trade_id = :tid AND intent_state <> 'closed'"
                ),
                {"tid": int(getattr(trade, "id"))},
            ).scalars().all()
            for _intent_id in _intent_ids:
                mark_closed(
                    db,
                    int(_intent_id),
                    reason=str(trade.exit_reason or "coinbase_position_sync_close")[:128],
                )
        except Exception:
            logger.debug(
                "[coinbase] bracket intent close failed for trade#%s",
                getattr(trade, "id", None),
                exc_info=True,
            )

        _close_coinbase_position_identity_for_trade(db, trade)
        closed += 1

    db.commit()
    logger.info(
        "[coinbase] Position sync: %d created, %d updated, %d reopened, %d closed, "
        "%d duplicates cancelled, %d duplicate entry orders cancelled",
        created,
        updated,
        reopened,
        closed,
        cleanup["cancelled"],
        duplicate_entry_cancelled,
    )
    return {
        "created": created,
        "updated": updated,
        "reopened": reopened,
        "closed": closed,
        "deduped": cleanup["cancelled"],
        "duplicate_entry_orders_cancelled": duplicate_entry_cancelled,
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
    _unavailable_products.clear()
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
