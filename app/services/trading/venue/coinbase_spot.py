"""Coinbase Advanced Trade spot adapter — thin over REST SDK; normalized DTOs."""

from __future__ import annotations

import logging
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ....config import settings
from ... import coinbase_service as cb
from .protocol import (
    FreshnessMeta,
    NormalizedFill,
    NormalizedOrder,
    NormalizedProduct,
    NormalizedTicker,
    VenueAdapterError,
)

_log = logging.getLogger(__name__)

# Recent client_order_id -> monotonic time of submission (duplicate guard)
_recent_client_orders: OrderedDict[str, float] = OrderedDict()
_GUARD_TTL_SEC = 300.0
_GUARD_MAX = 512


def reset_duplicate_client_order_guard_for_tests() -> None:
    """Clear in-process duplicate client_order_id guard (pytest only)."""
    _recent_client_orders.clear()


def _monotonic_now() -> float:
    import time

    return time.monotonic()


def _guard_remember(client_order_id: str) -> None:
    now_m = _monotonic_now()
    while len(_recent_client_orders) >= _GUARD_MAX:
        _recent_client_orders.popitem(last=False)
    _recent_client_orders[client_order_id] = now_m
    # prune stale
    cutoff = now_m - _GUARD_TTL_SEC
    for k in list(_recent_client_orders.keys()):
        if _recent_client_orders[k] < cutoff:
            del _recent_client_orders[k]
        else:
            break


def _guard_is_duplicate(client_order_id: str) -> bool:
    now_m = _monotonic_now()
    cutoff = now_m - _GUARD_TTL_SEC
    ts = _recent_client_orders.get(client_order_id)
    if ts is None:
        return False
    return ts >= cutoff


def _as_dict(resp: Any) -> dict[str, Any]:
    if resp is None:
        return {}
    if isinstance(resp, dict):
        return dict(resp)
    to_dict = getattr(resp, "to_dict", None)
    if callable(to_dict):
        try:
            d = to_dict()
            return dict(d) if isinstance(d, dict) else {}
        except Exception:
            pass
    if hasattr(resp, "__dict__"):
        return {k: v for k, v in resp.__dict__.items() if not str(k).startswith("_")}
    return {}


def _sf(x: Any) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _now_freshness() -> FreshnessMeta:
    max_age = float(getattr(settings, "chili_coinbase_market_data_max_age_sec", 15.0))
    return FreshnessMeta(
        retrieved_at_utc=datetime.now(timezone.utc),
        max_age_seconds=max_age,
    )


def _normalize_product(inner: dict[str, Any]) -> NormalizedProduct:
    pid = str(inner.get("product_id") or inner.get("productId") or "")
    base = str(inner.get("base_currency_id") or inner.get("base_currency") or inner.get("base") or "")
    quote = str(inner.get("quote_currency_id") or inner.get("quote_currency") or inner.get("quote") or "")
    return NormalizedProduct(
        product_id=pid,
        base_currency=base,
        quote_currency=quote,
        status=str(inner.get("status") or "unknown"),
        trading_disabled=bool(inner.get("trading_disabled") or inner.get("is_disabled")),
        cancel_only=bool(inner.get("cancel_only")),
        limit_only=bool(inner.get("limit_only")),
        post_only=bool(inner.get("post_only")),
        auction_mode=bool(inner.get("auction_mode")),
        base_min_size=_sf(inner.get("base_min_size")),
        base_max_size=_sf(inner.get("base_max_size")),
        quote_min_size=_sf(inner.get("quote_min_size")),
        quote_max_size=_sf(inner.get("quote_max_size")),
        min_market_funds=_sf(inner.get("min_market_funds")),
        max_market_funds=_sf(inner.get("max_market_funds")),
        base_increment=_sf(inner.get("base_increment")),
        quote_increment=_sf(inner.get("quote_increment")),
        price_increment=_sf(inner.get("price_increment") or inner.get("quote_increment")),
        product_type=str(inner.get("product_type") or inner.get("type") or "") or None,
        raw=dict(inner),
    )


def _first_book_price(levels: Any) -> Optional[float]:
    if not levels or not isinstance(levels, list):
        return None
    lv0 = levels[0]
    if isinstance(lv0, dict):
        return _sf(lv0.get("price") or lv0.get("price_level"))
    if hasattr(lv0, "price"):
        return _sf(getattr(lv0, "price", None))
    return None


def _normalize_bbo(resp: Any, product_id: str, fresh: FreshnessMeta) -> Optional[NormalizedTicker]:
    d = _as_dict(resp)
    books = d.get("pricebooks") or []
    bid = ask = None
    for pb in books:
        pbd = _as_dict(pb)
        if str(pbd.get("product_id") or "") != product_id:
            continue
        bid = _first_book_price(pbd.get("bids"))
        ask = _first_book_price(pbd.get("asks"))
        break
    mid = None
    spread_abs = None
    spread_bps = None
    if bid is not None and ask is not None and ask >= bid:
        mid = (bid + ask) / 2.0
        spread_abs = ask - bid
        if mid > 0:
            spread_bps = (spread_abs / mid) * 10000.0
    return NormalizedTicker(
        product_id=product_id,
        bid=bid,
        ask=ask,
        mid=mid,
        spread_abs=spread_abs,
        spread_bps=spread_bps,
        freshness=fresh,
    )


def _normalize_order(od: dict[str, Any]) -> NormalizedOrder:
    return NormalizedOrder(
        order_id=str(od.get("order_id") or od.get("orderId") or ""),
        client_order_id=(str(od.get("client_order_id") or od.get("clientOrderId") or "") or None),
        product_id=str(od.get("product_id") or od.get("productId") or ""),
        side=str(od.get("side") or "").lower(),
        status=str(od.get("status") or ""),
        order_type=str(od.get("order_type") or od.get("orderType") or "unknown"),
        filled_size=float(_sf(od.get("filled_size") or od.get("filledSize")) or 0.0),
        average_filled_price=_sf(od.get("average_filled_price") or od.get("averageFilledPrice")),
        created_time=str(od.get("created_time") or od.get("createdTime") or "") or None,
        raw=dict(od),
    )


def _normalize_fill(fd: dict[str, Any]) -> NormalizedFill:
    return NormalizedFill(
        fill_id=str(fd.get("entry_id") or fd.get("trade_id") or fd.get("fill_id") or "") or None,
        order_id=str(fd.get("order_id") or "") or None,
        product_id=str(fd.get("product_id") or ""),
        side=str(fd.get("side") or "").lower(),
        size=float(_sf(fd.get("size") or fd.get("trade_size")) or 0.0),
        price=float(_sf(fd.get("price") or fd.get("trade_price")) or 0.0),
        fee=_sf(fd.get("commission") or fd.get("fee")),
        trade_time=str(fd.get("trade_time") or fd.get("created_time") or "") or None,
        raw=dict(fd),
    )


def _to_product_id(symbol: str) -> str:
    t = symbol.upper().strip()
    if not t.endswith("-USD"):
        t = f"{t}-USD"
    return t


class CoinbaseSpotAdapter:
    """Coinbase spot execution + market data normalization."""

    def __init__(
        self,
        client_factory: Optional[Callable[[], Any]] = None,
    ):
        self._client_factory = client_factory or cb.get_coinbase_rest_client

    def is_enabled(self) -> bool:
        if not getattr(settings, "chili_coinbase_spot_adapter_enabled", True):
            return False
        return bool(cb.coinbase_sdk_and_credentials_configured())

    def _client(self) -> Any:
        return self._client_factory()

    def _require_client(self) -> Any:
        c = self._client()
        if not c:
            raise VenueAdapterError("Coinbase REST client unavailable")
        if not cb.is_connected():
            raise VenueAdapterError("Coinbase not connected")
        return c

    def get_product(self, product_id: str) -> tuple[Optional[NormalizedProduct], FreshnessMeta]:
        fresh = _now_freshness()
        if not self.is_enabled():
            return None, fresh
        try:
            c = self._require_client()
            resp = c.get_product(product_id=_to_product_id(product_id), get_tradability_status=True)
            d = _as_dict(resp)
            inner = d.get("product") if isinstance(d.get("product"), dict) else d
            if not isinstance(inner, dict):
                return None, fresh
            return _normalize_product(inner), fresh
        except VenueAdapterError:
            raise
        except Exception as e:
            _log.warning("[coinbase_spot] get_product failed: %s", e)
            return None, fresh

    def get_products(self) -> tuple[list[NormalizedProduct], FreshnessMeta]:
        fresh = _now_freshness()
        out: list[NormalizedProduct] = []
        if not self.is_enabled():
            return out, fresh
        try:
            c = self._require_client()
            resp = c.get_products()
            d = _as_dict(resp)
            products = d.get("products") or d.get("data") or []
            if not isinstance(products, list):
                return out, fresh
            for p in products:
                pd = p if isinstance(p, dict) else _as_dict(p)
                if pd.get("product_id") or pd.get("productId"):
                    out.append(_normalize_product(pd))
            return out, fresh
        except VenueAdapterError:
            return out, fresh
        except Exception as e:
            _log.warning("[coinbase_spot] get_products failed: %s", e)
            return out, fresh

    def get_best_bid_ask(self, product_id: str) -> tuple[Optional[NormalizedTicker], FreshnessMeta]:
        fresh = _now_freshness()
        pid = _to_product_id(product_id)
        if not self.is_enabled():
            return None, fresh
        try:
            c = self._require_client()
            resp = c.get_best_bid_ask(product_ids=[pid])
            tick = _normalize_bbo(resp, pid, fresh)
            return tick, fresh
        except VenueAdapterError:
            raise
        except Exception as e:
            _log.warning("[coinbase_spot] get_best_bid_ask failed: %s", e)
            return None, fresh

    def get_ticker(self, product_id: str) -> tuple[Optional[NormalizedTicker], FreshnessMeta]:
        bbo, fr = self.get_best_bid_ask(product_id)
        if not self.is_enabled():
            return bbo, fr
        pid = _to_product_id(product_id)
        try:
            c = self._require_client()
            resp = c.get_market_trades(product_id=pid, limit=1)
            d = _as_dict(resp)
            trades = d.get("trades") or []
            last_px = None
            last_sz = None
            if trades:
                t0 = trades[0] if isinstance(trades[0], dict) else _as_dict(trades[0])
                last_px = _sf(t0.get("price"))
                last_sz = _sf(t0.get("size"))
            if bbo is None:
                return (
                    NormalizedTicker(
                        product_id=pid,
                        last_price=last_px,
                        last_size=last_sz,
                        freshness=fr,
                    ),
                    fr,
                )
            return (
                NormalizedTicker(
                    product_id=bbo.product_id,
                    bid=bbo.bid,
                    ask=bbo.ask,
                    mid=bbo.mid,
                    spread_abs=bbo.spread_abs,
                    spread_bps=bbo.spread_bps,
                    last_price=last_px,
                    last_size=last_sz,
                    freshness=fr,
                ),
                fr,
            )
        except Exception as e:
            _log.debug("[coinbase_spot] get_ticker last trade failed: %s", e)
            return bbo, fr

    def get_recent_trades(self, product_id: str, *, limit: int = 50) -> tuple[list[dict[str, Any]], FreshnessMeta]:
        fresh = _now_freshness()
        pid = _to_product_id(product_id)
        if not self.is_enabled():
            return [], fresh
        try:
            c = self._require_client()
            resp = c.get_market_trades(product_id=pid, limit=min(limit, 1000))
            d = _as_dict(resp)
            trades = d.get("trades") or []
            out = [t if isinstance(t, dict) else _as_dict(t) for t in trades[:limit]]
            return out, fresh
        except Exception as e:
            _log.warning("[coinbase_spot] get_recent_trades failed: %s", e)
            return [], fresh

    def list_open_orders(
        self, *, product_id: Optional[str] = None, limit: int = 50
    ) -> tuple[list[NormalizedOrder], FreshnessMeta]:
        fresh = _now_freshness()
        if not self.is_enabled():
            return [], fresh
        try:
            c = self._require_client()
            kwargs: dict[str, Any] = {"order_status": ["OPEN", "PENDING"], "limit": limit}
            if product_id:
                kwargs["product_ids"] = [_to_product_id(product_id)]
            resp = c.list_orders(**kwargs)
            d = _as_dict(resp)
            orders = d.get("orders") or []
            return [_normalize_order(o if isinstance(o, dict) else _as_dict(o)) for o in orders], fresh
        except Exception as e:
            _log.warning("[coinbase_spot] list_open_orders failed: %s", e)
            return [], fresh

    def get_order(self, order_id: str) -> tuple[Optional[NormalizedOrder], FreshnessMeta]:
        fresh = _now_freshness()
        if not self.is_enabled():
            return None, fresh
        try:
            c = self._require_client()
            resp = c.get_order(order_id=order_id)
            d = _as_dict(resp)
            inner = d.get("order") if isinstance(d.get("order"), dict) else d
            if not isinstance(inner, dict):
                return None, fresh
            return _normalize_order(inner), fresh
        except Exception as e:
            _log.debug("[coinbase_spot] get_order failed: %s", e)
            return None, fresh

    def get_fills(
        self,
        *,
        product_id: Optional[str] = None,
        limit: int = 50,
    ) -> tuple[list[NormalizedFill], FreshnessMeta]:
        fresh = _now_freshness()
        if not self.is_enabled():
            return [], fresh
        try:
            c = self._require_client()
            kwargs: dict[str, Any] = {"limit": limit}
            if product_id:
                kwargs["product_ids"] = [_to_product_id(product_id)]
            resp = c.get_fills(**kwargs)
            d = _as_dict(resp)
            fills = d.get("fills") or d.get("orders") or []
            if not isinstance(fills, list):
                return [], fresh
            return [_normalize_fill(f if isinstance(f, dict) else _as_dict(f)) for f in fills[:limit]], fresh
        except Exception as e:
            _log.warning("[coinbase_spot] get_fills failed: %s", e)
            return [], fresh

    def place_market_order(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if not getattr(settings, "chili_coinbase_spot_adapter_enabled", True):
            return {"ok": False, "error": "adapter disabled"}
        cid = client_order_id or str(uuid.uuid4())
        if _guard_is_duplicate(cid):
            return {"ok": False, "error": "duplicate client_order_id (recent)", "client_order_id": cid}
        pid = _to_product_id(product_id)
        try:
            c = self._require_client()
            side_l = side.lower()
            if side_l == "buy":
                resp = c.market_order_buy(client_order_id=cid, product_id=pid, base_size=str(base_size))
            elif side_l == "sell":
                resp = c.market_order_sell(client_order_id=cid, product_id=pid, base_size=str(base_size))
            else:
                return {"ok": False, "error": f"invalid side: {side}"}
            rd = _as_dict(resp)
            ok = bool(rd.get("success"))
            sr = _as_dict(rd.get("success_response"))
            oid = sr.get("order_id") or cid
            if ok or oid:
                _guard_remember(cid)
                cb.clear_cache()
                return {"ok": True, "order_id": oid, "client_order_id": cid, "raw": rd}
            er = _as_dict(rd.get("error_response"))
            return {"ok": False, "error": er.get("message") or er.get("error") or str(rd), "client_order_id": cid}
        except VenueAdapterError as e:
            return {"ok": False, "error": str(e), "client_order_id": cid}
        except Exception as e:
            _log.exception("[coinbase_spot] place_market_order failed")
            return {"ok": False, "error": str(e), "client_order_id": cid}

    def place_limit_order_gtc(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        limit_price: str,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if not getattr(settings, "chili_coinbase_spot_adapter_enabled", True):
            return {"ok": False, "error": "adapter disabled"}
        cid = client_order_id or str(uuid.uuid4())
        if _guard_is_duplicate(cid):
            return {"ok": False, "error": "duplicate client_order_id (recent)", "client_order_id": cid}
        pid = _to_product_id(product_id)
        try:
            c = self._require_client()
            side_l = side.lower()
            if side_l == "buy":
                resp = c.limit_order_gtc_buy(
                    client_order_id=cid,
                    product_id=pid,
                    base_size=str(base_size),
                    limit_price=str(limit_price),
                )
            elif side_l == "sell":
                resp = c.limit_order_gtc_sell(
                    client_order_id=cid,
                    product_id=pid,
                    base_size=str(base_size),
                    limit_price=str(limit_price),
                )
            else:
                return {"ok": False, "error": f"invalid side: {side}"}
            rd = _as_dict(resp)
            ok = bool(rd.get("success"))
            sr = _as_dict(rd.get("success_response"))
            oid = sr.get("order_id") or cid
            if ok or oid:
                _guard_remember(cid)
                cb.clear_cache()
                return {"ok": True, "order_id": oid, "client_order_id": cid, "raw": rd}
            er = _as_dict(rd.get("error_response"))
            return {"ok": False, "error": er.get("message") or er.get("error") or str(rd), "client_order_id": cid}
        except VenueAdapterError as e:
            return {"ok": False, "error": str(e), "client_order_id": cid}
        except Exception as e:
            _log.exception("[coinbase_spot] place_limit_order_gtc failed")
            return {"ok": False, "error": str(e), "client_order_id": cid}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        if not self.is_enabled():
            return {"ok": False, "error": "adapter disabled"}
        try:
            c = self._require_client()
            resp = c.cancel_orders(order_ids=[order_id])
            d = _as_dict(resp)
            cb.clear_cache()
            return {"ok": True, "raw": d}
        except VenueAdapterError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def preview_market_order(
        self,
        *,
        product_id: str,
        side: str,
        base_size: Optional[str] = None,
        quote_size: Optional[str] = None,
    ) -> dict[str, Any]:
        if not self.is_enabled():
            return {"ok": False, "error": "adapter disabled", "todo": "connect coinbase"}
        pid = _to_product_id(product_id)
        try:
            c = self._require_client()
            resp = c.preview_market_order(
                product_id=pid,
                side=side.upper(),
                base_size=base_size,
                quote_size=quote_size,
            )
            return {"ok": True, "raw": _as_dict(resp)}
        except VenueAdapterError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            _log.debug("[coinbase_spot] preview_market_order: %s", e)
            return {"ok": False, "error": str(e), "todo": "preview unsupported or params invalid"}

    def get_account_snapshot(self) -> dict[str, Any]:
        fresh = _now_freshness()
        snap: dict[str, Any] = {
            "ok": True,
            "freshness": {"retrieved_at_utc": fresh.retrieved_at_utc.isoformat()},
        }
        try:
            snap["portfolio"] = cb.get_portfolio()
            snap["positions"] = cb.get_positions()
        except Exception as e:
            snap["ok"] = False
            snap["error"] = str(e)
        return snap


class CoinbaseWebSocketSeam:
    """Feature-flagged placeholder for a future WS loop (no implementation in Phase 3)."""

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "chili_coinbase_ws_enabled", False))

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "status": "stub",
            "message": "WebSocket trading loop not implemented (Phase 3 seam only).",
        }
