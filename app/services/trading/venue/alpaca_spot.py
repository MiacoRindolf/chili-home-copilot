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


def _opt_bool(v: Any) -> Optional[bool]:
    """None-preserving bool coercion. Returns None when the field is absent so a
    missing short signal fails CLOSED at the gate (not silently treated as False)."""
    if v is None:
        return None
    return bool(v)


def _norm_status(raw: Any) -> str:
    s = getattr(raw, "value", raw)
    s = str(s or "").strip().lower()
    return _STATUS_MAP.get(s, s or "unknown")


def _is_crypto_pid(product_id: str) -> bool:
    """The lane's crypto convention: BASE-USD (BTC-USD, KAIO-USD)."""
    return str(product_id or "").strip().upper().endswith("-USD")


def _to_symbol(product_id: str) -> str:
    """Equity product_id is the bare ticker (AAPL); Alpaca uses the same. The
    lane's crypto pairs are dash-form (BTC-USD) — Alpaca's crypto API wants the
    slash form (BTC/USD)."""
    pid = str(product_id or "").strip().upper()
    if pid.endswith("-USD"):
        return pid[:-4] + "/USD"
    return pid


def _from_alpaca_symbol(sym: str) -> str:
    """Normalize an Alpaca order/asset symbol back to the lane's product_id:
    crypto BTC/USD -> BTC-USD; equities unchanged."""
    s2 = str(sym or "").strip().upper()
    return s2.replace("/", "-") if "/" in s2 else s2


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fresh(seconds: float | None = None) -> FreshnessMeta:
    max_age = float(seconds if seconds is not None
                    else getattr(settings, "chili_alpaca_quote_max_age_seconds", 60.0) or 60.0)
    return FreshnessMeta(retrieved_at_utc=_now(), max_age_seconds=max_age)


# ── lazy SDK clients (cached) ─────────────────────────────────────────────────
_clients: dict[str, Any] = {}


def _keys() -> tuple[str, str]:
    """Posture-selected key pair (2026-07-10): paper -> the base pair; LIVE -> the
    dedicated live pair (CHILI_ALPACA_LIVE_API_KEY/SECRET), falling back to the base
    pair only when the live pair is unset — so the paper->live switch is ONE flag
    flip with both credential sets already resting in the deploy .env."""
    if not _paper():
        lk = str(getattr(settings, "chili_alpaca_live_api_key", "") or "")
        ls = str(getattr(settings, "chili_alpaca_live_api_secret", "") or "")
        if lk and ls:
            return (lk, ls)
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


def _crypto_data_client():
    if "crypto_data" not in _clients:
        from alpaca.data.historical import CryptoHistoricalDataClient
        key, secret = _keys()
        _clients["crypto_data"] = CryptoHistoricalDataClient(key, secret)
    return _clients["crypto_data"]


def reset_clients_for_tests() -> None:
    _clients.clear()
    _LISTED_CACHE.clear()


# Per-process listing cache (listings change rarely; a probe is one HTTP call).
_LISTED_CACHE: dict[str, bool] = {}


def alpaca_lists_symbol(product_id: str) -> bool:
    """True when Alpaca has a TRADABLE asset for this lane symbol (equity ticker or
    crypto BASE-USD -> BASE/USD). Cached per process. FAIL-CLOSED (False) on any probe
    error — callers route the symbol to its default venue instead. Used by the
    crypto->alpaca-paper router: only Alpaca-LISTED majors go to the paper account;
    unlisted low-cap alts stay on their default (and the arm-side guard skips them
    while the paper posture is on)."""
    sym = str(product_id or "").strip().upper()
    if not sym:
        return False
    if sym in _LISTED_CACHE:
        return _LISTED_CACHE[sym]
    listed = False
    try:
        prod, _ = AlpacaSpotAdapter().get_product(sym)
        listed = prod is not None and not bool(getattr(prod, "trading_disabled", True))
    except Exception:
        listed = False
    _LISTED_CACHE[sym] = listed
    return listed


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
    def _iqfeed_l1_quote(self, sym: str):
        """Freshest IQFeed L1 quote from momentum_nbbo_spread_tape (the SAME feed the live lane
        uses), so the Alpaca lane decisions on IQFeed data — NOT the thin Alpaca-IEX feed that made
        2199 sessions go dormant (06-18) on stale/no BBO. Self-contained short-lived read of ONE
        indexed (symbol, observed_at) row. Returns (NormalizedTicker, FreshnessMeta) or None on
        miss / stale-beyond-window / error => caller falls back to Alpaca-IEX. Pure read."""
        try:
            from ....db import SessionLocal
            from sqlalchemy import text

            max_age = float(getattr(settings, "chili_alpaca_quote_max_age_seconds", 60.0) or 60.0)
            with SessionLocal() as _db:
                row = _db.execute(text(
                    "SELECT bid, ask, mid, spread_bps, observed_at "
                    "FROM momentum_nbbo_spread_tape "
                    "WHERE symbol = :s AND mid > 0 ORDER BY observed_at DESC LIMIT 1"
                ), {"s": str(sym or "").upper()}).fetchone()
            if row is None:
                return None
            bid = _f(row[0]); ask = _f(row[1]); mid = _f(row[2])
            if not (bid and ask and mid and mid > 0):
                return None
            ts = row[4]
            if isinstance(ts, datetime):
                _ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
                if (_now() - _ts).total_seconds() > max_age:
                    return None  # stale IQFeed row -> let Alpaca-IEX try
            else:
                _ts = _now()
            spread_bps = _f(row[3])
            if spread_bps is None and ask >= bid:
                spread_bps = (ask - bid) / mid * 10_000.0
            meta = FreshnessMeta(retrieved_at_utc=_ts, max_age_seconds=max_age)
            return NormalizedTicker(
                product_id=sym, bid=bid, ask=ask, mid=mid, spread_bps=spread_bps,
                bid_size=None, ask_size=None, freshness=meta, raw={"feed": "iqfeed_l1"},
            ), meta
        except Exception as exc:
            logger.debug("[alpaca_spot] _iqfeed_l1_quote(%s) failed: %s", sym, exc)
            return None

    def get_best_bid_ask(self, product_id: str):
        sym = _to_symbol(product_id)
        # DATA/EXECUTION DECOUPLING (2026-07-07): Alpaca is EXECUTION-only. Alpaca-IEX quotes have
        # thin small-cap coverage — the dormancy root cause (stale_bbo/no_bbo on Ross low-float
        # names since 06-18). Prefer IQFeed L1 (momentum_nbbo_spread_tape, same feed the live lane
        # uses, ~0.26s fresh); fall back to Alpaca-IEX only on a miss. Kill-switch
        # chili_alpaca_quotes_via_iqfeed (default True). Equities only. See ALPACA_PAPER_ENABLE_PLAN.md.
        if not _is_crypto_pid(product_id) and bool(
            getattr(settings, "chili_alpaca_quotes_via_iqfeed", True)
        ):
            _iq = self._iqfeed_l1_quote(sym)
            if _iq is not None:
                return _iq
        try:
            if _is_crypto_pid(product_id):
                from alpaca.data.requests import CryptoLatestQuoteRequest
                req = CryptoLatestQuoteRequest(symbol_or_symbols=sym)
                q = _crypto_data_client().get_crypto_latest_quote(req).get(sym)
            else:
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
            if _is_crypto_pid(product_id):
                from alpaca.data.requests import CryptoLatestTradeRequest
                t = _crypto_data_client().get_crypto_latest_trade(
                    CryptoLatestTradeRequest(symbol_or_symbols=sym)
                ).get(sym)
            else:
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
                product_type="crypto" if _is_crypto_pid(product_id) else "equity",
                raw={
                    "fractionable": fractionable,
                    "exchange": str(getattr(a, "exchange", "")),
                    # Short-lane locate-feasibility surfacing (SHORT_SIDE_LANE.md P0).
                    # Asset-level borrow signals so the short-entry gate can fail-closed
                    # on a not-shortable / hard-to-borrow name. (None when the SDK/asset
                    # doesn't expose them — fail-closed at the gate, not here.)
                    "shortable": _opt_bool(getattr(a, "shortable", None)),
                    "easy_to_borrow": _opt_bool(getattr(a, "easy_to_borrow", None)),
                },
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
                sym = _from_alpaca_symbol(getattr(a, "symbol", ""))
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
            product_id=_from_alpaca_symbol(getattr(o, "symbol", "")),
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

    def list_open_orders(self, *, product_id: Optional[str] = None, limit: int = 50,
                         strict: bool = False):
        """Open orders. strict=True returns (None, meta) on a READ FAILURE so safety-
        critical callers (the orphan reconciler's in-flight guard) can distinguish
        'no open orders' from 'unreadable' and fail-open; default keeps the legacy
        ([], meta)-on-error contract for existing callers."""
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=int(limit),
                                   symbols=[_to_symbol(product_id)] if product_id else None)
            orders = _trading_client().get_orders(filter=req)
            return [self._normalize_order(o) for o in (orders or [])], _fresh(5.0)
        except Exception as exc:
            logger.debug("[alpaca_spot] list_open_orders failed: %s", exc)
            return (None if strict else []), _fresh(5.0)

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
                fill_id=None, order_id=str(getattr(o, "id", "")), product_id=_from_alpaca_symbol(getattr(o, "symbol", "")),
                side=str(getattr(getattr(o, "side", None), "value", "")).lower(), size=fq, price=fp,
                trade_time=str(getattr(o, "filled_at", "") or ""),
            )], _fresh(5.0)
        except Exception as exc:
            logger.debug("[alpaca_spot] get_fills failed: %s", exc)
            return [], _fresh(5.0)

    def list_positions(self):
        """ALL account positions, normalized to plain dicts (read-only; feeds the orphan
        reconciler + ops views). Returns (list, meta) — or (None, meta) when the account
        read FAILED, so callers can distinguish 'flat' ([]) from 'unreadable' (None) and
        fail-open (take no action) on the latter."""
        try:
            rows = _trading_client().get_all_positions() or []
            out = []
            for p in rows:
                out.append({
                    "product_id": _from_alpaca_symbol(str(getattr(p, "symbol", "") or "")),
                    "raw_symbol": str(getattr(p, "symbol", "") or ""),
                    "qty": _f(getattr(p, "qty", None)) or 0.0,
                    "avg_entry_price": _f(getattr(p, "avg_entry_price", None)),
                    "market_value": _f(getattr(p, "market_value", None)),
                    "unrealized_pl": _f(getattr(p, "unrealized_pl", None)),
                    "asset_class": str(
                        getattr(getattr(p, "asset_class", None), "value", "")
                        or getattr(p, "asset_class", "")
                        or ""
                    ).lower(),
                })
            return out, _fresh(5.0)
        except Exception as exc:
            logger.debug("[alpaca_spot] list_positions failed: %s", exc)
            return None, _fresh(5.0)

    def place_market_order(self, *, product_id: str, side: str, base_size: str,
                           client_order_id: Optional[str] = None,
                           position_intent: Optional[str] = None, **_ignored) -> dict[str, Any]:
        return self._submit(product_id, side, base_size, client_order_id, limit_price=None,
                            position_intent=position_intent)

    def place_limit_order_gtc(self, *, product_id: str, side: str, base_size: str,
                              limit_price: str, client_order_id: Optional[str] = None,
                              extended_hours: bool = False,
                              position_intent: Optional[str] = None, **_ignored) -> dict[str, Any]:
        return self._submit(product_id, side, base_size, client_order_id,
                            limit_price=limit_price, extended_hours=bool(extended_hours),
                            position_intent=position_intent)

    def place_deadman_stop(self, *, product_id: str, base_size: str, stop_price: float,
                           client_order_id: Optional[str] = None) -> dict[str, Any]:
        """DEAD-MAN protective stop (2026-07-10, the GMM -$16k orphan incident): a
        RESTING GTC STOP order at the BROKER itself, placed BELOW the software stop —
        not the primary exit (the FSM manages the position), but the FLOOR when the
        whole machine dies or loses network while holding (the exact incident: TCP
        ephemeral-port exhaustion -> the worker was alive but could not reach Alpaca
        -> GMM collapsed unprotected). ``sell_to_close`` means even a double-fire
        alongside a software exit can never flip the position short. Equity-only
        (Alpaca equities support stop orders; the crypto lane is separate)."""
        try:
            from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
            from alpaca.trading.requests import StopOrderRequest

            req = StopOrderRequest(
                symbol=_to_symbol(product_id),
                qty=base_size,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=round(float(stop_price), 2),
                client_order_id=client_order_id,
                position_intent=PositionIntent.SELL_TO_CLOSE,
            )
            o = _trading_client().submit_order(req)
            return {"ok": True, "order_id": str(getattr(o, "id", "") or ""),
                    "status": str(getattr(getattr(o, "status", None), "value", "") or "")}
        except Exception as exc:
            logger.warning("[alpaca_spot] deadman stop place failed for %s: %s", product_id, exc)
            return {"ok": False, "error": str(exc)}

    def cancel_order_by_id(self, order_id: str) -> bool:
        """Cancel one resting order by broker id (the dead-man release path).
        True = cancelled or already gone; False = a real cancel failure."""
        try:
            _trading_client().cancel_order_by_id(order_id)
            return True
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "unable to be cancel" in msg or "filled" in msg:
                return True  # already gone / already terminal — released either way
            logger.warning("[alpaca_spot] cancel_order_by_id(%s) failed: %s", order_id, exc)
            return False

    def _resolve_position_intent(self, position_intent):
        """Map the lane's intent string to the alpaca-py ``PositionIntent`` enum.

        The intent DISAMBIGUATES an otherwise-ambiguous ``SELL`` (open-short vs
        close-long) — the #1 short-lane adapter change (SHORT_SIDE_LANE.md P0):

          - short ENTRY  → ``OrderSide.SELL`` + ``SELL_TO_OPEN``
          - short COVER  → ``OrderSide.BUY``  + ``BUY_TO_CLOSE``
          - long open/close keep ``BUY_TO_OPEN`` / ``SELL_TO_CLOSE``.

        ``None`` (the long-lane default) returns ``None`` so the request is built
        WITHOUT the field — byte-identical to today. Accepts either the enum name
        (``"sell_to_open"``) or the raw enum.
        """
        if position_intent is None:
            return None
        try:
            from alpaca.trading.enums import PositionIntent
        except Exception:
            return None
        if isinstance(position_intent, PositionIntent):
            return position_intent
        key = str(position_intent).strip().lower()
        _MAP = {
            "buy_to_open": PositionIntent.BUY_TO_OPEN,
            "buy_to_close": PositionIntent.BUY_TO_CLOSE,
            "sell_to_open": PositionIntent.SELL_TO_OPEN,
            "sell_to_close": PositionIntent.SELL_TO_CLOSE,
        }
        return _MAP.get(key)

    @staticmethod
    def _equity_limit_price(price, side) -> float:
        """Alpaca EQUITY sub-penny rule (reject 42210000 'sub-penny increment does not
        fulfill minimum pricing criteria'): >= $1.00 -> $0.01 increments, < $1.00 ->
        $0.0001. The lane's trail/target math emits raw floats (1.5345426..., 5.544) —
        Alpaca REJECTED every such EXIT for 2 days (2026-07-07/08: ~38 failed exit
        submissions across every symbol; VTAK bled -40%/-$3,390 while its stop,
        scale-out AND trail submissions all bounced; even winners' scale-outs failed).
        Entries passed only because 2-decimal quotes fed them. Round TOWARD
        MARKETABILITY (SELL -> floor, BUY -> ceiling) so a protective exit is never
        stranded over a fraction of a cent. Decimal-quantized (no float artifacts).
        Equities only — crypto increments differ and that path is untouched."""
        from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

        d = Decimal(str(float(price)))
        tick = Decimal("0.01") if d >= Decimal("1") else Decimal("0.0001")
        rounding = ROUND_CEILING if str(side).lower() == "buy" else ROUND_FLOOR
        return float(d.quantize(tick, rounding=rounding))

    def _submit(self, product_id, side, base_size, client_order_id, *, limit_price,
                extended_hours: bool = False, position_intent=None) -> dict[str, Any]:
        sym = _to_symbol(product_id)
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
            _side = OrderSide.BUY if str(side).lower() == "buy" else OrderSide.SELL
            qty = float(base_size)
            # Optional position-intent (short lane). None ⇒ omit the field entirely
            # so the long-path request is byte-identical to today.
            _intent = self._resolve_position_intent(position_intent)
            _intent_kw = {"position_intent": _intent} if _intent is not None else {}
            if _is_crypto_pid(product_id):
                # Crypto orders: 24/7, no extended-hours concept, and Alpaca
                # accepts only GTC/IOC TIFs (DAY is rejected). Crypto cannot be
                # shorted on Alpaca, so position_intent is never set here.
                if limit_price is not None:
                    req = LimitOrderRequest(symbol=sym, qty=qty, side=_side,
                                            time_in_force=TimeInForce.GTC,
                                            limit_price=float(limit_price),
                                            client_order_id=client_order_id)
                else:
                    req = MarketOrderRequest(symbol=sym, qty=qty, side=_side,
                                             time_in_force=TimeInForce.GTC,
                                             client_order_id=client_order_id)
                o = _trading_client().submit_order(order_data=req)
                return {"ok": True, "order_id": str(getattr(o, "id", "") or ""),
                        "client_order_id": getattr(o, "client_order_id", None) or client_order_id,
                        "status": _norm_status(getattr(o, "status", None))}
            if limit_price is not None:
                # Sub-penny normalization (see _equity_limit_price) — MUST precede the
                # request build; every raw-float exit limit was rejected 42210000.
                _lp = self._equity_limit_price(limit_price, side)
                # Marketable/posting limit. Alpaca rejects extended_hours unless the order
                # is a LIMIT with DAY tif — so for pre-/after-market (Ross's gap-and-go) we
                # send DAY + extended_hours=True; the RTH default stays a plain GTC.
                if extended_hours:
                    req = LimitOrderRequest(symbol=sym, qty=qty, side=_side, time_in_force=TimeInForce.DAY,
                                            limit_price=_lp, client_order_id=client_order_id,
                                            extended_hours=True, **_intent_kw)
                else:
                    # Fractional-qty orders REQUIRE DAY tif on Alpaca (GTC is
                    # rejected) — 25% of twin entries died on this (2026-06-12
                    # quant pass v2 A6). Whole-share orders keep GTC.
                    _tif = TimeInForce.GTC
                    try:
                        if abs(float(qty) - round(float(qty))) > 1e-9:
                            _tif = TimeInForce.DAY
                    except (TypeError, ValueError):
                        pass
                    req = LimitOrderRequest(symbol=sym, qty=qty, side=_side, time_in_force=_tif,
                                            limit_price=_lp, client_order_id=client_order_id,
                                            **_intent_kw)
            else:
                req = MarketOrderRequest(symbol=sym, qty=qty, side=_side, time_in_force=TimeInForce.DAY,
                                         client_order_id=client_order_id, **_intent_kw)
            o = _trading_client().submit_order(order_data=req)
            res = {"ok": True, "order_id": str(getattr(o, "id", "") or ""),
                   "client_order_id": getattr(o, "client_order_id", None) or client_order_id,
                   "status": _norm_status(getattr(o, "status", None))}
            # Surface the resolved short intent + the broker's signed position-intent
            # echo so the runner can confirm a short opened/covered as expected.
            if _intent is not None:
                res["position_intent"] = str(getattr(_intent, "value", _intent))
                pi_echo = getattr(o, "position_intent", None)
                if pi_echo is not None:
                    res["position_intent_echo"] = str(getattr(pi_echo, "value", pi_echo))
            return res
        except Exception as exc:
            msg = str(exc)
            # Distinctly surface SSR / borrow-locate rejections so the runner can DEFER
            # (post an up-bid limit / skip) rather than blind-retry into a venue wall.
            low = msg.lower()
            reject_kind = None
            if ("short" in low and ("restrict" in low or "ssr" in low or "uptick" in low)) or "regulation sho" in low:
                reject_kind = "ssr"
            elif "borrow" in low or "locate" in low or "not shortable" in low or "htb" in low:
                reject_kind = "borrow"
            logger.warning("[alpaca_spot] submit order failed sym=%s side=%s limit=%s intent=%s reject=%s: %s",
                           sym, side, limit_price, position_intent, reject_kind, exc)
            out = {"ok": False, "error": msg[:200], "client_order_id": client_order_id}
            if reject_kind:
                out["reject_kind"] = reject_kind
            return out

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
                    # Short-lane capability surfacing (SHORT_SIDE_LANE.md P0): the lane must
                    # never arm a short on a cash / no-margin account. multiplier>1 ⇒ margin;
                    # shorting_enabled is the explicit account capability flag.
                    "shorting_enabled": _opt_bool(getattr(a, "shorting_enabled", None)),
                    "multiplier": _f(getattr(a, "multiplier", None)),
                    "paper": _paper()}
        except Exception as exc:
            logger.debug("[alpaca_spot] get_account_snapshot failed: %s", exc)
            return {"ok": False, "error": str(exc)[:200]}
