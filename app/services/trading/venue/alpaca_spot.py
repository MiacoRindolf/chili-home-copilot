"""Alpaca equities VenueAdapter — the DMA-style limit-posting upgrade over Robinhood.

Robinhood routes via PFOF with no direct market access, so CHILI is forced to CROSS the
3.6%-median spreads of Ross low-float names (0 clean fills ever — project_momentum_zero_
fills_root_cause). Alpaca is API-first (built for bots), commission-free, has a FREE paper
sandbox, and its LIMIT orders route to the market and can REST on the book (the post-inside-
the-spread capability RH lacks). This adapter implements the venue ``VenueAdapter`` Protocol
so the momentum FSM (limit-entry #553, software stop, liquidity-bias #552, auto-arm) runs
through Alpaca unchanged — only the venue changes. (docs/DESIGN/ALPACA_LANE.md)

Paper-first: ``CHILI_ALPACA_PAPER`` defaults True → the paper endpoint, zero risk, until the
fills are proven. ``alpaca-py`` is imported LAZILY so this module loads even before the SDK is
installed (``is_enabled`` returns False, every call returns a safe error envelope).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from ....config import settings
from .protocol import (
    FreshnessMeta,
    NormalizedFill,
    NormalizedOrder,
    NormalizedProduct,
    NormalizedTicker,
)

logger = logging.getLogger(__name__)

_VENUE = "alpaca"
# Alpaca order statuses -> the lowercase vocabulary the runner's _order_done_for_entry /
# _order_open helpers understand (#550/#551). Working states map to "open" so the fill
# poll keeps going; terminal states map to their canonical terminal words.
_STATUS_MAP = {
    "filled": "filled",
    "partially_filled": "open",          # still working toward full fill
    "new": "open",
    "accepted": "open",
    "pending_new": "open",
    "accepted_for_bidding": "open",
    "held": "open",
    "calculated": "open",
    "stopped": "open",
    "suspended": "open",
    "pending_cancel": "pending",
    "pending_replace": "pending",
    "replaced": "canceled",
    "canceled": "canceled",
    "cancelled": "canceled",
    "expired": "expired",
    "done_for_day": "expired",
    "rejected": "rejected",
}


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _norm_status(raw: Any) -> str:
    s = getattr(raw, "value", raw)
    s = str(s or "").strip().lower()
    return _STATUS_MAP.get(s, s or "unknown")


def _to_symbol(product_id: str) -> str:
    """Equity product_id is the bare ticker (AAPL); Alpaca uses the same. Crypto pairs
    keep their slash form (BTC/USD) — not the momentum equity path, but harmless."""
    return str(product_id or "").strip().upper()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fresh(seconds: float | None = None) -> FreshnessMeta:
    max_age = float(seconds if seconds is not None
                    else getattr(settings, "chili_alpaca_quote_max_age_seconds", 60.0) or 60.0)
    return FreshnessMeta(retrieved_at_utc=_now(), max_age_seconds=max_age)


# ── lazy SDK clients (cached) ─────────────────────────────────────────────────
_clients: dict[str, Any] = {}


def _keys() -> tuple[str, str]:
    return (
        str(getattr(settings, "chili_alpaca_api_key", "") or ""),
        str(getattr(settings, "chili_alpaca_api_secret", "") or ""),
    )


def _paper() -> bool:
    return bool(getattr(settings, "chili_alpaca_paper", True))


def _data_feed():
    """The DataFeed enum value (iex free / sip paid). Falls back to a plain string."""
    want = str(getattr(settings, "chili_alpaca_data_feed", "iex") or "iex").strip().lower()
    try:
        from alpaca.data.enums import DataFeed
        return DataFeed.SIP if want == "sip" else DataFeed.IEX
    except Exception:
        return want


def _trading_client():
    if "trading" not in _clients:
        from alpaca.trading.client import TradingClient
        key, secret = _keys()
        _clients["trading"] = TradingClient(key, secret, paper=_paper())
    return _clients["trading"]


def _data_client():
    if "data" not in _clients:
        from alpaca.data.historical import StockHistoricalDataClient
        key, secret = _keys()
        _clients["data"] = StockHistoricalDataClient(key, secret)
    return _clients["data"]


def reset_clients_for_tests() -> None:
    _clients.clear()


class AlpacaSpotAdapter:
    """VenueAdapter for Alpaca US equities (paper or live, per CHILI_ALPACA_PAPER)."""

    # ── availability ─────────────────────────────────────────────────────────
    def is_enabled(self) -> bool:
        if not bool(getattr(settings, "chili_alpaca_enabled", False)):
            return False
        key, secret = _keys()
        if not key or not secret:
            return False
        try:
            import alpaca  # noqa: F401
            return True
        except Exception:
            logger.warning("[alpaca_spot] alpaca-py not installed — adapter disabled")
            return False

    # ── market data ──────────────────────────────────────────────────────────
    def get_best_bid_ask(self, product_id: str):
        sym = _to_symbol(product_id)
        try:
            from alpaca.data.requests import StockLatestQuoteRequest
            req = StockLatestQuoteRequest(symbol_or_symbols=sym, feed=_data_feed())
            q = _data_client().get_stock_latest_quote(req).get(sym)
            if q is None:
                return None, _fresh()
            bid = _f(getattr(q, "bid_price", None)); ask = _f(getattr(q, "ask_price", None))
            mid = ((bid + ask) / 2.0) if (bid and ask) else None
            spread_bps = ((ask - bid) / mid * 10_000.0) if (bid and ask and mid and mid > 0 and ask >= bid) else None
            ts = getattr(q, "timestamp", None)
            meta = FreshnessMeta(
                retrieved_at_utc=ts if isinstance(ts, datetime) else _now(),
                max_age_seconds=float(getattr(settings, "chili_alpaca_quote_max_age_seconds", 60.0) or 60.0),
            )
            return NormalizedTicker(
                product_id=sym, bid=bid, ask=ask, mid=mid, spread_bps=spread_bps,
                bid_size=_f(getattr(q, "bid_size", None)), ask_size=_f(getattr(q, "ask_size", None)),
                freshness=meta, raw={"feed": str(_data_feed())},
            ), meta
        except Exception as exc:
            logger.debug("[alpaca_spot] get_best_bid_ask(%s) failed: %s", sym, exc)
            return None, _fresh()

    def get_ticker(self, product_id: str):
        return self.get_best_bid_ask(product_id)

    def get_recent_trades(self, product_id: str, *, limit: int = 50):
        sym = _to_symbol(product_id)
        try:
            from alpaca.data.requests import StockLatestTradeRequest
            t = _data_client().get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=sym, feed=_data_feed())
            ).get(sym)
            if t is None:
                return [], _fresh()
            return [{"price": _f(getattr(t, "price", None)), "size": _f(getattr(t, "size", None)),
                     "time": str(getattr(t, "timestamp", ""))}], _fresh()
        except Exception as exc:
            logger.debug("[alpaca_spot] get_recent_trades(%s) failed: %s", sym, exc)
            return [], _fresh()

    # ── products / assets ────────────────────────────────────────────────────
    def get_product(self, product_id: str):
        sym = _to_symbol(product_id)
        try:
            a = _trading_client().get_asset(sym)
            tradable = bool(getattr(a, "tradable", False))
            status = str(getattr(getattr(a, "status", None), "value", getattr(a, "status", "")) or "").lower()
            fractionable = bool(getattr(a, "fractionable", False))
            base_inc = _f(getattr(a, "min_trade_increment", None)) or (0.000001 if fractionable else 1.0)
            min_sz = _f(getattr(a, "min_order_size", None)) or base_inc
            price_inc = _f(getattr(a, "price_increment", None)) or 0.01
            prod = NormalizedProduct(
                product_id=sym, base_currency=sym, quote_currency="USD",
                status=status or ("active" if tradable else "inactive"),
                trading_disabled=not tradable, cancel_only=False, limit_only=False,
                post_only=False, auction_mode=False,
                base_min_size=min_sz, base_increment=base_inc, price_increment=price_inc,
                product_type="equity", raw={"fractionable": fractionable, "exchange": str(getattr(a, "exchange", ""))},
            )
            return prod, _fresh(3600.0)
        except Exception as exc:
            logger.debug("[alpaca_spot] get_product(%s) failed: %s", sym, exc)
            return None, _fresh(3600.0)

    def get_products(self):
        try:
            from alpaca.trading.requests import GetAssetsRequest
            from alpaca.trading.enums import AssetClass, AssetStatus
            assets = _trading_client().get_all_assets(
                GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
            )
            out = []
            for a in assets or []:
                if not bool(getattr(a, "tradable", False)):
                    continue
                sym = _to_symbol(getattr(a, "symbol", ""))
                if not sym:
                    continue
                out.append(NormalizedProduct(
                    product_id=sym, base_currency=sym, quote_currency="USD", status="active",
                    trading_disabled=False, cancel_only=False, limit_only=False, post_only=False,
                    auction_mode=False, base_increment=(0.000001 if getattr(a, "fractionable", False) else 1.0),
                    price_increment=0.01, product_type="equity", raw={},
                ))
            return out, _fresh(3600.0)
        except Exception as exc:
            logger.debug("[alpaca_spot] get_products failed: %s", exc)
            return [], _fresh(3600.0)

    # ── orders ───────────────────────────────────────────────────────────────
    def _normalize_order(self, o: Any) -> NormalizedOrder:
        return NormalizedOrder(
            order_id=str(getattr(o, "id", "") or ""),
            client_order_id=getattr(o, "client_order_id", None),
            product_id=_to_symbol(getattr(o, "symbol", "")),
            side=str(getattr(getattr(o, "side", None), "value", getattr(o, "side", "")) or "").lower(),
            status=_norm_status(getattr(o, "status", None)),
            order_type=str(getattr(getattr(o, "order_type", None), "value",
                                   getattr(o, "type", "") or getattr(o, "order_type", "")) or "").lower(),
            filled_size=_f(getattr(o, "filled_qty", None)) or 0.0,
            average_filled_price=_f(getattr(o, "filled_avg_price", None)),
            created_time=str(getattr(o, "created_at", "") or ""),
            raw={"alpaca_status": str(getattr(getattr(o, "status", None), "value", getattr(o, "status", "")))},
        )

    def get_order(self, order_id: str):
        try:
            o = _trading_client().get_order_by_id(str(order_id))
            return self._normalize_order(o), _fresh(5.0)
        except Exception as exc:
            logger.debug("[alpaca_spot] get_order(%s) failed: %s", order_id, exc)
            return None, _fresh(5.0)

    def list_open_orders(self, *, product_id: Optional[str] = None, limit: int = 50):
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=int(limit),
                                   symbols=[_to_symbol(product_id)] if product_id else None)
            orders = _trading_client().get_orders(filter=req)
            return [self._normalize_order(o) for o in (orders or [])], _fresh(5.0)
        except Exception as exc:
            logger.debug("[alpaca_spot] list_open_orders failed: %s", exc)
            return [], _fresh(5.0)

    def get_fills(self, *, product_id: Optional[str] = None, order_id: Optional[str] = None, limit: int = 50):
        # Alpaca exposes fills via account activities; the runner reads avg_fill_price off the
        # order itself, so a thin best-effort implementation is sufficient for v1.
        try:
            o = _trading_client().get_order_by_id(str(order_id)) if order_id else None
            if o is None:
                return [], _fresh(5.0)
            fp = _f(getattr(o, "filled_avg_price", None)); fq = _f(getattr(o, "filled_qty", None))
            if not fp or not fq:
                return [], _fresh(5.0)
            return [NormalizedFill(
                fill_id=None, order_id=str(getattr(o, "id", "")), product_id=_to_symbol(getattr(o, "symbol", "")),
                side=str(getattr(getattr(o, "side", None), "value", "")).lower(), size=fq, price=fp,
                trade_time=str(getattr(o, "filled_at", "") or ""),
            )], _fresh(5.0)
        except Exception as exc:
            logger.debug("[alpaca_spot] get_fills failed: %s", exc)
            return [], _fresh(5.0)

    def place_market_order(self, *, product_id: str, side: str, base_size: str,
                           client_order_id: Optional[str] = None, **_ignored) -> dict[str, Any]:
        return self._submit(product_id, side, base_size, client_order_id, limit_price=None)

    def place_limit_order_gtc(self, *, product_id: str, side: str, base_size: str,
                              limit_price: str, client_order_id: Optional[str] = None,
                              **_ignored) -> dict[str, Any]:
        return self._submit(product_id, side, base_size, client_order_id, limit_price=limit_price)

    def _submit(self, product_id, side, base_size, client_order_id, *, limit_price) -> dict[str, Any]:
        sym = _to_symbol(product_id)
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
            _side = OrderSide.BUY if str(side).lower() == "buy" else OrderSide.SELL
            qty = float(base_size)
            if limit_price is not None:
                # GTC marketable/posting limit; extended_hours requires a limit + DAY tif, so use
                # DAY when extended hours might be in play, GTC otherwise. RTH default = GTC.
                req = LimitOrderRequest(symbol=sym, qty=qty, side=_side, time_in_force=TimeInForce.GTC,
                                        limit_price=float(limit_price), client_order_id=client_order_id)
            else:
                req = MarketOrderRequest(symbol=sym, qty=qty, side=_side, time_in_force=TimeInForce.DAY,
                                         client_order_id=client_order_id)
            o = _trading_client().submit_order(order_data=req)
            return {"ok": True, "order_id": str(getattr(o, "id", "") or ""),
                    "client_order_id": getattr(o, "client_order_id", None) or client_order_id,
                    "status": _norm_status(getattr(o, "status", None))}
        except Exception as exc:
            logger.warning("[alpaca_spot] submit order failed sym=%s side=%s limit=%s: %s",
                           sym, side, limit_price, exc)
            return {"ok": False, "error": str(exc)[:200], "client_order_id": client_order_id}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        try:
            _trading_client().cancel_order_by_id(str(order_id))
            return {"ok": True, "order_id": str(order_id)}
        except Exception as exc:
            logger.debug("[alpaca_spot] cancel_order(%s) failed: %s", order_id, exc)
            return {"ok": False, "error": str(exc)[:200], "order_id": str(order_id)}

    def preview_market_order(self, *, product_id: str, side: str, base_size: str, **_ignored) -> dict[str, Any]:
        # Alpaca has no order-preview endpoint; estimate locally from the latest quote.
        tick, _ = self.get_best_bid_ask(product_id)
        px = None
        if tick is not None:
            px = tick.ask if str(side).lower() == "buy" else tick.bid
        return {"ok": True, "estimated_price": px, "base_size": base_size, "note": "local estimate (no preview API)"}

    # ── account ──────────────────────────────────────────────────────────────
    def get_account_snapshot(self) -> dict[str, Any]:
        try:
            a = _trading_client().get_account()
            return {"ok": True, "equity": _f(getattr(a, "equity", None)),
                    "buying_power": _f(getattr(a, "buying_power", None)),
                    "cash": _f(getattr(a, "cash", None)),
                    "status": str(getattr(getattr(a, "status", None), "value", getattr(a, "status", "")) or ""),
                    "paper": _paper()}
        except Exception as exc:
            logger.debug("[alpaca_spot] get_account_snapshot failed: %s", exc)
            return {"ok": False, "error": str(exc)[:200]}
