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
    """Coinbase Advanced Trade WebSocket for L2 book, trade tape, and ticker streaming.

    Feeds ``microstructure`` ring buffers used by scanners for breakout
    confirmation (bid/ask imbalance, trade aggression, spread quality).

    When the price bus is enabled, also updates a real-time quote cache
    from the ``ticker`` channel (best bid/ask/last) and fires tick listeners
    so the hybrid paper runner can react to price changes immediately.
    """

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "chili_coinbase_ws_enabled", False))
        self._ws = None
        self._thread: Any = None
        self._subscribed: set[str] = set()
        self._running = False
        self._quote_cache: dict[str, dict[str, Any]] = {}
        self._quote_cache_lock: Any = None  # lazy init
        self._tick_listeners: dict[str, list] = {}
        self._tick_listeners_lock: Any = None  # lazy init

    # ── lifecycle ───────────────────────────────────────────────────
    def start(self, product_ids: list[str] | None = None) -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "reason": "coinbase_ws_disabled"}
        if self._running:
            return {"ok": True, "already_running": True}

        try:
            from coinbase.websocket import WSClient
        except ImportError:
            _log.warning("[coinbase_ws] coinbase-advanced-py WSClient not available")
            return {"ok": False, "reason": "sdk_missing"}

        api_key = getattr(settings, "coinbase_api_key", None)
        api_secret = getattr(settings, "coinbase_api_secret", None)

        self._ws = WSClient(
            api_key=api_key or None,
            api_secret=api_secret or None,
            on_message=self._on_message,
            on_close=self._on_close,
            retry=True,
        )
        self._ws.open()
        self._running = True

        pids = product_ids or ["BTC-USD", "ETH-USD"]
        self.subscribe(pids)
        _log.info("[coinbase_ws] started for %s", pids)
        return {"ok": True, "product_ids": pids}

    def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._subscribed.clear()
        _log.info("[coinbase_ws] stopped")

    def subscribe(self, product_ids: list[str]) -> None:
        if not self._ws or not self._running:
            return
        new_pids = [p for p in product_ids if p not in self._subscribed]
        if not new_pids:
            return
        try:
            self._ws.level2(product_ids=new_pids)
            self._ws.market_trades(product_ids=new_pids)
            if hasattr(self._ws, "ticker"):
                self._ws.ticker(product_ids=new_pids)
            self._subscribed.update(new_pids)
        except Exception as e:
            _log.warning("[coinbase_ws] subscribe error: %s", e)

    def unsubscribe(self, product_ids: list[str]) -> None:
        if not self._ws:
            return
        try:
            self._ws.level2_unsubscribe(product_ids=product_ids)
            self._ws.market_trades_unsubscribe(product_ids=product_ids)
            if hasattr(self._ws, "ticker_unsubscribe"):
                self._ws.ticker_unsubscribe(product_ids=product_ids)
            self._subscribed -= set(product_ids)
        except Exception as e:
            _log.debug("[coinbase_ws] unsubscribe error: %s", e)

    # ── quote cache + tick listeners ─────────────────────────────────
    def _ensure_locks(self) -> None:
        import threading
        if self._quote_cache_lock is None:
            self._quote_cache_lock = threading.Lock()
        if self._tick_listeners_lock is None:
            self._tick_listeners_lock = threading.Lock()

    def get_quote(self, product_id: str) -> dict[str, Any] | None:
        """Return latest cached quote for *product_id*, or None if stale/missing."""
        import time as _time
        self._ensure_locks()
        with self._quote_cache_lock:
            snap = self._quote_cache.get(product_id)
        if snap is None:
            return None
        if _time.time() - snap.get("timestamp", 0) > 5.0:
            return None
        return snap

    def register_tick_listener(self, product_id: str, callback) -> None:
        self._ensure_locks()
        with self._tick_listeners_lock:
            self._tick_listeners.setdefault(product_id, []).append(callback)

    def unregister_tick_listener(self, product_id: str, callback) -> None:
        self._ensure_locks()
        with self._tick_listeners_lock:
            cbs = self._tick_listeners.get(product_id)
            if cbs:
                try:
                    cbs.remove(callback)
                except ValueError:
                    pass

    def _fire_tick(self, product_id: str, snap: dict[str, Any]) -> None:
        self._ensure_locks()
        with self._tick_listeners_lock:
            cbs = list(self._tick_listeners.get(product_id, []))
        for cb in cbs:
            try:
                cb(product_id, snap)
            except Exception:
                pass

    # ── message handler ─────────────────────────────────────────────
    def _on_message(self, msg: str) -> None:
        import json as _json
        try:
            data = _json.loads(msg) if isinstance(msg, str) else msg
        except Exception:
            return

        channel = data.get("channel", "")
        events = data.get("events", [])

        if channel == "l2_data":
            self._handle_l2(events)
        elif channel == "market_trades":
            self._handle_trades(events)
        elif channel == "ticker":
            self._handle_ticker(events)

    def _on_close(self) -> None:
        _log.info("[coinbase_ws] connection closed")
        self._running = False

    def _handle_l2(self, events: list[dict]) -> None:
        from ..microstructure import BookLevel, BookSnapshot, get_book_buffer
        import time as _time

        buf = get_book_buffer()
        for ev in events:
            pid = ev.get("product_id", "")
            updates = ev.get("updates", [])
            if not pid or not updates:
                continue

            bids = []
            asks = []
            for u in updates:
                side = u.get("side", "")
                px = float(u.get("price_level", 0) or u.get("new_price", 0) or 0)
                sz = float(u.get("new_quantity", 0) or u.get("qty", 0) or 0)
                if px <= 0:
                    continue
                lvl = BookLevel(price=px, size=sz, side="bid" if side == "bid" else "offer")
                if side == "bid":
                    bids.append(lvl)
                else:
                    asks.append(lvl)

            if bids or asks:
                bids.sort(key=lambda l: l.price, reverse=True)
                asks.sort(key=lambda l: l.price)
                snap = BookSnapshot(
                    product_id=pid,
                    bids=bids[:20],
                    asks=asks[:20],
                    ts=_time.time(),
                )
                buf.update(snap)

    def _handle_ticker(self, events: list[dict]) -> None:
        """Process Coinbase ``ticker`` channel: consolidated BBO + last price."""
        import time as _time
        self._ensure_locks()
        now = _time.time()
        for ev in events:
            tickers = ev.get("tickers", [])
            for t in tickers:
                pid = t.get("product_id", "")
                if not pid:
                    continue
                try:
                    bid = float(t.get("best_bid", 0) or 0)
                    ask = float(t.get("best_ask", 0) or 0)
                    last = float(t.get("price", 0) or 0)
                except (TypeError, ValueError):
                    continue
                mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else last
                if mid <= 0:
                    continue
                snap = {
                    "bid": bid, "ask": ask, "mid": mid, "last": last,
                    "product_id": pid, "timestamp": now, "source": "coinbase_ws_ticker",
                }
                with self._quote_cache_lock:
                    self._quote_cache[pid] = snap
                self._fire_tick(pid, snap)

    def _handle_trades(self, events: list[dict]) -> None:
        from ..microstructure import TapeTrade, get_trade_buffer
        import time as _time

        self._ensure_locks()
        now = _time.time()
        buf = get_trade_buffer()
        for ev in events:
            trades = ev.get("trades", [])
            for t in trades:
                pid = t.get("product_id", "")
                px = float(t.get("price", 0) or 0)
                sz = float(t.get("size", 0) or 0)
                side = t.get("side", "UNKNOWN")
                if px > 0 and sz > 0 and pid:
                    buf.append(TapeTrade(
                        product_id=pid,
                        price=px,
                        size=sz,
                        side=side,
                        ts=now,
                    ))
                    # Update quote cache from trades (fallback when ticker channel unavailable)
                    with self._quote_cache_lock:
                        existing = self._quote_cache.get(pid)
                    if existing is None or now - existing.get("timestamp", 0) > 2.0:
                        snap = {
                            "bid": None, "ask": None, "mid": px, "last": px,
                            "product_id": pid, "timestamp": now, "source": "coinbase_ws_trade",
                        }
                        with self._quote_cache_lock:
                            self._quote_cache[pid] = snap
                        self._fire_tick(pid, snap)

    # ── status ──────────────────────────────────────────────────────
    def describe(self) -> dict[str, Any]:
        self._ensure_locks()
        with self._quote_cache_lock:
            cached_symbols = sorted(self._quote_cache.keys())
        return {
            "enabled": self.enabled,
            "status": "running" if self._running else ("ready" if self.enabled else "disabled"),
            "subscribed_products": sorted(self._subscribed),
            "cached_quotes": cached_symbols,
            "message": (
                f"Streaming L2 + trades + ticker for {len(self._subscribed)} products"
                if self._running
                else "Not running"
            ),
        }


_coinbase_ws_singleton: CoinbaseWebSocketSeam | None = None


def get_coinbase_ws() -> CoinbaseWebSocketSeam:
    """Module-level singleton for the Coinbase WebSocket seam."""
    global _coinbase_ws_singleton
    if _coinbase_ws_singleton is None:
        _coinbase_ws_singleton = CoinbaseWebSocketSeam()
    return _coinbase_ws_singleton
