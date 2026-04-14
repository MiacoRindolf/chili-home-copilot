"""VenueAdapter implementation for Robinhood equities via robin_stocks.

Delegates to ``broker_service`` for authentication, order placement, and position queries.
Symbol convention: plain tickers (``AAPL``), not crypto-style product IDs (``BTC-USD``).
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional

from .protocol import (
    FreshnessMeta,
    NormalizedFill,
    NormalizedOrder,
    NormalizedProduct,
    NormalizedTicker,
    VenueAdapterError,
)

logger = logging.getLogger(__name__)

# ── Duplicate-order guard (mirrors coinbase_spot pattern) ──────────────
_recent_client_orders: OrderedDict[str, float] = OrderedDict()
_GUARD_TTL_SEC = 300.0
_GUARD_MAX = 512


def _guard_remember(key: str) -> None:
    import time

    _recent_client_orders[key] = time.monotonic()
    while len(_recent_client_orders) > _GUARD_MAX:
        _recent_client_orders.popitem(last=False)


def _guard_is_duplicate(key: str | None) -> bool:
    if not key:
        return False
    import time

    ts = _recent_client_orders.get(key)
    if ts is None:
        return False
    return (time.monotonic() - ts) < _GUARD_TTL_SEC


# ── Helpers ────────────────────────────────────────────────────────────


def _sf(x: Any) -> Optional[float]:
    """Safe float conversion."""
    if x is None:
        return None
    try:
        return float(x)
    except (ValueError, TypeError):
        return None


def _to_ticker(product_id: str) -> str:
    """Normalize product_id to a plain stock ticker (strip -USD suffix if present)."""
    s = (product_id or "").strip().upper()
    if s.endswith("-USD"):
        s = s[:-4]
    return s


def _now_freshness(max_age: float = 15.0) -> FreshnessMeta:
    return FreshnessMeta(
        retrieved_at_utc=datetime.now(timezone.utc),
        max_age_seconds=max_age,
    )


def _normalize_rh_order(od: dict[str, Any]) -> NormalizedOrder:
    """Map a Robinhood order dict to NormalizedOrder."""
    from ...broker_service import map_rh_status

    rh_state = (od.get("state") or "").lower()
    filled_qty = _sf(od.get("cumulative_quantity")) or 0.0
    avg_price = _sf(od.get("average_price"))
    # Robinhood uses instrument URL, not ticker directly on the order dict.
    # The caller may need to supply the ticker via the ``instrument`` lookup or context.
    ticker = od.get("symbol") or od.get("chain_symbol") or ""

    return NormalizedOrder(
        order_id=od.get("id", ""),
        client_order_id=None,  # Robinhood doesn't support client_order_id
        product_id=ticker,
        side=od.get("side", "buy"),
        status=map_rh_status(rh_state),
        order_type=od.get("type", "market"),
        filled_size=filled_qty,
        average_filled_price=avg_price,
        created_time=od.get("created_at"),
        raw=od,
    )


def _normalize_rh_fill(od: dict[str, Any]) -> NormalizedFill:
    """Extract fill info from a filled Robinhood order dict."""
    return NormalizedFill(
        fill_id=od.get("id"),
        order_id=od.get("id"),
        product_id=od.get("symbol") or od.get("chain_symbol") or "",
        side=od.get("side", "buy"),
        size=float(od.get("cumulative_quantity") or 0),
        price=float(od.get("average_price") or 0),
        fee=_sf(od.get("fees")),
        trade_time=od.get("last_transaction_at") or od.get("updated_at"),
        raw=od,
    )


# ── Adapter ────────────────────────────────────────────────────────────


class RobinhoodSpotAdapter:
    """VenueAdapter for Robinhood equities via broker_service + robin_stocks."""

    def is_enabled(self) -> bool:
        from ....config import settings
        from ...broker_service import is_connected

        return bool(
            getattr(settings, "chili_robinhood_spot_adapter_enabled", False)
        ) and is_connected()

    # ── Product / Market Data ──────────────────────────────────────────

    def get_product(self, product_id: str) -> tuple[Optional[NormalizedProduct], FreshnessMeta]:
        ticker = _to_ticker(product_id)
        fresh = _now_freshness()
        try:
            import robin_stocks.robinhood as rh

            instruments = rh.stocks.get_instruments_by_symbols([ticker])
            inst = instruments[0] if instruments else None
            if not inst or not isinstance(inst, dict):
                return None, fresh

            tradeable = bool(inst.get("tradeable", False))
            return NormalizedProduct(
                product_id=ticker,
                base_currency=ticker,
                quote_currency="USD",
                status="active" if tradeable else "inactive",
                trading_disabled=not tradeable,
                cancel_only=False,
                limit_only=False,
                post_only=False,
                auction_mode=False,
                base_min_size=1.0,
                base_max_size=None,
                base_increment=1.0,
                quote_increment=0.01,
                price_increment=0.01,
                product_type="equity",
                raw=inst,
            ), fresh
        except Exception as e:
            logger.warning("[rh_adapter] get_product(%s) failed: %s", ticker, e)
            return None, fresh

    def get_products(self) -> tuple[list[NormalizedProduct], FreshnessMeta]:
        fresh = _now_freshness()
        try:
            from ...broker_service import get_positions

            positions = get_positions()
            products = []
            for pos in positions:
                ticker = pos.get("ticker", "")
                if not ticker:
                    continue
                products.append(NormalizedProduct(
                    product_id=ticker,
                    base_currency=ticker,
                    quote_currency="USD",
                    status="active",
                    trading_disabled=False,
                    cancel_only=False,
                    limit_only=False,
                    post_only=False,
                    auction_mode=False,
                    base_min_size=1.0,
                    base_increment=1.0,
                    quote_increment=0.01,
                    price_increment=0.01,
                    product_type="equity",
                    raw=pos,
                ))
            return products, fresh
        except Exception as e:
            logger.warning("[rh_adapter] get_products failed: %s", e)
            return [], fresh

    def get_best_bid_ask(self, product_id: str) -> tuple[Optional[NormalizedTicker], FreshnessMeta]:
        ticker = _to_ticker(product_id)
        fresh = _now_freshness()
        try:
            import robin_stocks.robinhood as rh

            quotes = rh.stocks.get_quotes([ticker])
            q = quotes[0] if quotes else None
            if not q or not isinstance(q, dict):
                return None, fresh

            bid = _sf(q.get("bid_price"))
            ask = _sf(q.get("ask_price"))
            last = _sf(q.get("last_trade_price"))
            bid_size = _sf(q.get("bid_size"))
            ask_size = _sf(q.get("ask_size"))

            if bid and ask and bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
                spread_abs = ask - bid
                spread_bps = (spread_abs / mid) * 10_000 if mid > 0 else None
            elif last and last > 0:
                mid = last
                bid = bid or last
                ask = ask or last
                spread_abs = (ask or last) - (bid or last)
                spread_bps = (spread_abs / mid) * 10_000 if mid > 0 and spread_abs else 0.0
            else:
                return None, fresh

            return NormalizedTicker(
                product_id=ticker,
                bid=bid,
                ask=ask,
                mid=mid,
                spread_abs=spread_abs,
                spread_bps=spread_bps,
                last_price=last,
                last_size=None,
                bid_size=bid_size,
                ask_size=ask_size,
                base_volume_24h=None,
                quote_volume_24h=None,
                freshness=fresh,
            ), fresh
        except Exception as e:
            logger.warning("[rh_adapter] get_best_bid_ask(%s) failed: %s", ticker, e)
            return None, fresh

    def get_ticker(self, product_id: str) -> tuple[Optional[NormalizedTicker], FreshnessMeta]:
        return self.get_best_bid_ask(product_id)

    def get_recent_trades(self, product_id: str, *, limit: int = 50) -> tuple[list[dict[str, Any]], FreshnessMeta]:
        # robin_stocks has no public trade tape endpoint
        return [], _now_freshness()

    # ── Orders ─────────────────────────────────────────────────────────

    def list_open_orders(
        self,
        *,
        product_id: Optional[str] = None,
        limit: int = 50,
    ) -> tuple[list[NormalizedOrder], FreshnessMeta]:
        fresh = _now_freshness()
        try:
            import robin_stocks.robinhood as rh

            raw_orders = rh.orders.get_all_open_stock_orders() or []
            orders = [_normalize_rh_order(o) for o in raw_orders if isinstance(o, dict)]
            if product_id:
                t = _to_ticker(product_id)
                orders = [o for o in orders if o.product_id.upper() == t]
            return orders[:limit], fresh
        except Exception as e:
            logger.warning("[rh_adapter] list_open_orders failed: %s", e)
            return [], fresh

    def get_order(self, order_id: str) -> tuple[Optional[NormalizedOrder], FreshnessMeta]:
        fresh = _now_freshness()
        try:
            from ...broker_service import get_order_by_id

            od = get_order_by_id(order_id)
            if not od:
                return None, fresh
            return _normalize_rh_order(od), fresh
        except Exception as e:
            logger.warning("[rh_adapter] get_order(%s) failed: %s", order_id, e)
            return None, fresh

    def get_fills(
        self,
        *,
        product_id: Optional[str] = None,
        limit: int = 50,
    ) -> tuple[list[NormalizedFill], FreshnessMeta]:
        fresh = _now_freshness()
        try:
            from ...broker_service import get_recent_orders

            raw = get_recent_orders(limit=limit * 2)
            filled = [o for o in raw if (o.get("state") or "").lower() == "filled"]
            fills = [_normalize_rh_fill(o) for o in filled]
            if product_id:
                t = _to_ticker(product_id)
                fills = [f for f in fills if f.product_id.upper() == t]
            return fills[:limit], fresh
        except Exception as e:
            logger.warning("[rh_adapter] get_fills failed: %s", e)
            return [], fresh

    # ── Order Placement ────────────────────────────────────────────────

    def place_market_order(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        ticker = _to_ticker(product_id)
        qty = float(base_size)

        if _guard_is_duplicate(client_order_id):
            return {"ok": False, "error": "duplicate_client_order_id", "client_order_id": client_order_id}

        from ...broker_service import place_buy_order, place_sell_order

        if side.lower() == "buy":
            result = place_buy_order(ticker, qty, order_type="market")
        elif side.lower() == "sell":
            result = place_sell_order(ticker, qty, order_type="market")
        else:
            return {"ok": False, "error": f"unknown side: {side}"}

        if result.get("ok") and client_order_id:
            _guard_remember(client_order_id)

        if result.get("ok"):
            return {
                "ok": True,
                "order_id": result.get("order_id", ""),
                "client_order_id": client_order_id,
                "raw": result.get("raw", {}),
            }
        return result

    def place_limit_order_gtc(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        limit_price: str,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        ticker = _to_ticker(product_id)
        qty = float(base_size)
        price = float(limit_price)

        if _guard_is_duplicate(client_order_id):
            return {"ok": False, "error": "duplicate_client_order_id", "client_order_id": client_order_id}

        from ...broker_service import place_buy_order, place_sell_order

        if side.lower() == "buy":
            result = place_buy_order(ticker, qty, order_type="limit", limit_price=price)
        elif side.lower() == "sell":
            result = place_sell_order(ticker, qty, order_type="limit", limit_price=price)
        else:
            return {"ok": False, "error": f"unknown side: {side}"}

        if result.get("ok") and client_order_id:
            _guard_remember(client_order_id)

        if result.get("ok"):
            return {
                "ok": True,
                "order_id": result.get("order_id", ""),
                "client_order_id": client_order_id,
                "raw": result.get("raw", {}),
            }
        return result

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        try:
            import robin_stocks.robinhood as rh

            result = rh.orders.cancel_stock_order(order_id)
            return {"ok": True, "raw": result or {}}
        except Exception as e:
            logger.warning("[rh_adapter] cancel_order(%s) failed: %s", order_id, e)
            return {"ok": False, "error": str(e)}

    def preview_market_order(
        self,
        *,
        product_id: str,
        side: str,
        base_size: Optional[str] = None,
        quote_size: Optional[str] = None,
    ) -> dict[str, Any]:
        ticker = _to_ticker(product_id)
        tick, _ = self.get_best_bid_ask(ticker)
        if not tick or not tick.mid:
            return {"ok": False, "error": "no_quote"}
        qty = float(base_size or 0)
        notional = qty * tick.mid if qty > 0 else float(quote_size or 0)
        return {
            "ok": True,
            "estimated_price": tick.mid,
            "estimated_notional": notional,
            "spread_bps": tick.spread_bps,
            "fees": 0.0,  # Robinhood has zero commissions on equities
        }

    # ── Account ────────────────────────────────────────────────────────

    def get_account_snapshot(self) -> dict[str, Any]:
        try:
            from ...broker_service import get_portfolio

            port = get_portfolio()
            return {
                "ok": True,
                "portfolio": port,
                "freshness": {
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                },
            }
        except Exception as e:
            logger.warning("[rh_adapter] get_account_snapshot failed: %s", e)
            return {"ok": False, "error": str(e)}
