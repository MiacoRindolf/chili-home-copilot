"""Coinbase Advanced Trade spot adapter — thin over REST SDK; normalized DTOs."""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from typing import Any, Callable, Optional

from ....config import settings
from ... import coinbase_service as cb
from ..portfolio_risk import _assert_portfolio_breaker_ok
from . import idempotency_store, order_state_machine, rate_limiter, venue_health
from .protocol import (
    FreshnessMeta,
    NormalizedFill,
    NormalizedOrder,
    NormalizedProduct,
    NormalizedTicker,
    VenueAdapter,
    VenueAdapterError,
)

_log = logging.getLogger(__name__)

_VENUE = "coinbase"


# Product-info cache (f-coinbase-tick-size-precision-fix, 2026-05-10).
# Coinbase rejects orders whose stop_price / limit_price / base_size carry
# more decimals than the product's quote_increment / base_increment. Cache
# the per-product NormalizedProduct in-process with a 1-hour TTL so the
# REST round-trip happens at most once per hour per symbol.
_PRODUCT_INFO_CACHE: "dict[str, tuple[NormalizedProduct, float]]" = {}
_PRODUCT_INFO_CACHE_LOCK = threading.Lock()
_PRODUCT_INFO_TTL_SEC = 3600.0


def reset_product_info_cache_for_tests() -> None:
    """Clear product-info cache (pytest only)."""
    with _PRODUCT_INFO_CACHE_LOCK:
        _PRODUCT_INFO_CACHE.clear()


# f-phase3-stop-bleed D3 — pre-send product_id validator.
# Coinbase Advanced Trade rejects malformed product_ids with HTTP 400
# INVALID_ARGUMENT. The 2026-05-15 audit's last-7d rejection histogram
# shows 48 such errors. Rejecting locally avoids the round-trip and the
# rate-limit charge, and the original (un-normalized) value is preserved
# in the ValueError so the upstream producer bug is easy to find.
_VALID_PRODUCT_ID = re.compile(r"^[A-Z0-9]+-(USD|USDC)$")


def _normalize_product_id(product_id: str | None) -> str:
    """Validate Coinbase product_id and return the upper-cased canonical form.

    Accepts inputs like ``"btc-usd"`` and returns ``"BTC-USD"``. Raises
    ``ValueError`` on any of the common drift patterns seen in the audit:
    missing quote suffix (``"BTC"``), USD without separator
    (``"BTCUSD"``), slash separator from CCXT conventions (``"BTC/USD"``),
    or empty / None input. We do NOT auto-correct — the producer is the
    bug; refuse so the broker doesn't see it.
    """
    pid = (product_id or "").strip().upper()
    if not _VALID_PRODUCT_ID.match(pid):
        raise ValueError(
            f"coinbase_spot: invalid product_id {product_id!r}; "
            f"expected '<BASE>-USD' or '<BASE>-USDC'"
        )
    return pid


def _coinbase_preflight_cash_check(
    *,
    product_id: str,
    base_size: str,
    limit_price: str,
) -> Optional[dict[str, Any]]:
    """f-phase3-stop-bleed D4 — local BUY pre-flight against Coinbase
    buying-power cache.

    The 2026-05-15 audit's last-7d rejection histogram shows 830
    ``broker:Insufficient balance`` errors. Many are races between our
    resolver and the placement call. A local refuse is cheaper than a
    broker round-trip + rate-limit charge.

    Returns ``None`` on pass-through (let the placement proceed). Returns
    an envelope ``{"ok": False, "error": "...", "preflight_refused":
    True}`` when local buying power is strictly below the order's
    required notional. When the cache is stale beyond
    ``chili_coinbase_preflight_max_stale_seconds``, returns None with a
    logged warning so the broker remains the final authority.

    Fee slack (``chili_coinbase_preflight_fee_slack_bps``) and stale
    threshold (``chili_coinbase_preflight_max_stale_seconds``) are both
    settings-sourced — no magic constants (COWORK_ADVISOR_BRIEF §2.6).
    """
    try:
        from ..cost_aware_gate import resolve_coinbase_buying_power
    except Exception:
        _log.warning(
            "[coinbase_spot] D4 preflight: resolver import failed; allowing",
            exc_info=True,
        )
        return None
    try:
        bp = resolve_coinbase_buying_power()
    except Exception:
        _log.warning(
            "[coinbase_spot] D4 preflight: resolver raised; allowing",
            exc_info=True,
        )
        return None

    fee_slack_bps = float(getattr(
        settings, "chili_coinbase_preflight_fee_slack_bps", 50.0,
    ))
    max_stale_s = float(getattr(
        settings, "chili_coinbase_preflight_max_stale_seconds", 5.0,
    ))

    last_updated = float(bp.get("last_updated") or 0.0)
    if last_updated > 0:
        stale_age_s = time.time() - last_updated
        if stale_age_s > max_stale_s:
            _log.warning(
                "[coinbase_spot] D4 preflight: buying_power cache stale by "
                "%.1fs (max %.1fs); allowing through, broker is final check",
                stale_age_s, max_stale_s,
            )
            return None

    try:
        required_usd = (
            float(base_size) * float(limit_price)
            * (1.0 + fee_slack_bps / 10000.0)
        )
    except (TypeError, ValueError):
        _log.warning(
            "[coinbase_spot] D4 preflight: non-numeric base_size/limit_price "
            "(base_size=%r limit_price=%r); allowing",
            base_size, limit_price,
        )
        return None

    total_usd = float(bp.get("total") or 0.0)
    if total_usd < required_usd:
        msg = (
            f"local buying_power ${total_usd:.2f} < required "
            f"${required_usd:.2f} for {product_id} "
            f"(fee_slack={fee_slack_bps:.0f}bps)"
        )
        _log.info("[coinbase_spot] D4 preflight refused: %s", msg)
        return {"ok": False, "error": msg, "preflight_refused": True}

    return None


def _quantize_price(value: float, increment: float, *, mode: str) -> str:
    """Quantize *value* to *increment* using exact Decimal arithmetic.

    mode='down' → ROUND_DOWN (SELL stops: trigger sits at-or-below intent —
                              wider stop band, more conservative for longs)
    mode='up'   → ROUND_UP   (BUY stops: trigger sits at-or-above intent —
                              more conservative for shorts/breakout entries)

    Returns a string formatted to *increment*'s decimal precision so the
    Coinbase SDK does not re-introduce trailing-zero noise via float repr.

    Raises ValueError on non-positive increment, non-finite value, or
    invalid mode.
    """
    if mode not in ("down", "up"):
        raise ValueError(f"mode must be 'down' or 'up', got {mode!r}")
    try:
        d_val = Decimal(str(value))
        d_inc = Decimal(str(increment))
    except (InvalidOperation, TypeError, ValueError) as e:
        raise ValueError(
            f"non-decimal input: value={value!r} increment={increment!r}: {e}"
        )
    if not d_val.is_finite():
        raise ValueError(f"value must be finite, got {value!r}")
    if not d_inc.is_finite() or d_inc <= 0:
        raise ValueError(f"increment must be > 0, got {increment!r}")
    rounding = ROUND_DOWN if mode == "down" else ROUND_UP
    quotient = (d_val / d_inc).to_integral_value(rounding=rounding)
    snapped = (quotient * d_inc).quantize(d_inc)
    return format(snapped, "f")


def _quantize_size(value: float, increment: float) -> str:
    """Quantize *value* DOWN to *increment* (never order more than intended).

    Returns a decimal string. Raises ValueError on bad input.
    """
    return _quantize_price(value, increment, mode="down")


def reset_duplicate_client_order_guard_for_tests() -> None:
    """Clear in-process duplicate client_order_id cache (pytest only).

    Also resets the shared ``rate_limiter`` bucket state. Tests that
    exercise ``place_market_order`` / ``place_limit_order`` paths all
    consume rate-limiter tokens; without resetting, a long test suite
    would occasionally see spurious ``rate_limited`` responses once
    bucket budget exhausted across sibling tests. Phase B adds this
    reset so fault-injection tests can assume a pristine rate-limiter
    state at entry.

    Does NOT truncate the DB idempotency table — tests that need a
    clean DB row set should use their own fixtures.
    """
    idempotency_store.reset_for_tests()
    rate_limiter.reset_for_tests()


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


class CoinbaseSpotAdapter(VenueAdapter):
    """Coinbase spot execution + market data normalization.

    Explicitly declares the ``VenueAdapter`` protocol so static type
    checkers validate every method signature — mirroring
    :class:`RobinhoodSpotAdapter`.
    """

    def __init__(
        self,
        client_factory: Optional[Callable[[], Any]] = None,
    ):
        self._client_factory = client_factory or cb.get_coinbase_rest_client

    def list_usd_spot_universe_entries(
        self,
        *,
        exclude_bases_lower: frozenset[str] | set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Tradable USD spot products as CHILI crypto entries for ``ticker_universe``.

        Uses REST credentials only (``coinbase_sdk_and_credentials_configured()``);
        does not require ``coinbase_service.connect()`` UI session.
        """
        out: list[dict[str, Any]] = []
        if not cb.coinbase_sdk_and_credentials_configured():
            return out
        c = self._client()
        if not c:
            return out
        excl = exclude_bases_lower or frozenset()
        try:
            resp = c.get_products()
            d = _as_dict(resp)
            products = d.get("products") or d.get("data") or []
            if not isinstance(products, list):
                return out
            seen: set[str] = set()
            for p in products:
                pd = p if isinstance(p, dict) else _as_dict(p)
                if not (pd.get("product_id") or pd.get("productId")):
                    continue
                norm = _normalize_product(pd)
                if not norm.tradable_for_spot_momentum():
                    continue
                pid = str(norm.product_id or "").strip().upper()
                if not pid or pid in seen:
                    continue
                if not pid.endswith("-USD"):
                    continue
                qc = (norm.quote_currency or "").upper()
                if qc and qc != "USD":
                    continue
                pt = (norm.product_type or "").upper()
                if pt and "FUTURE" in pt:
                    continue
                base = pid.split("-")[0].lower()
                if base in excl:
                    continue
                seen.add(pid)
                display = (norm.base_currency or base.upper() or pid.replace("-USD", "")).upper()
                out.append({
                    "ticker": pid,
                    "name": f"{display} (Coinbase)",
                    "type": "crypto",
                    "volume_usd": 0.0,
                    "source": "coinbase",
                })
            if out:
                _log.info("[coinbase_spot] universe merge: %s USD spot products", len(out))
        except Exception as e:
            _log.warning("[coinbase_spot] list_usd_spot_universe_entries failed: %s", e)
        return out

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

    def _get_product_info_cached(self, product_id: str) -> NormalizedProduct:
        """Return cached NormalizedProduct or fetch fresh.

        Raises VenueAdapterError if fetch fails or returns invalid
        increments. NEVER falls back to a guessed tick_size — per
        COWORK_ADVISOR_BRIEF §2.6 (no magic-fallback values).
        """
        pid = _to_product_id(product_id)
        now = time.time()
        with _PRODUCT_INFO_CACHE_LOCK:
            hit = _PRODUCT_INFO_CACHE.get(pid)
            if hit and hit[1] > now:
                return hit[0]
        prod, _fresh = self.get_product(pid)
        if prod is None:
            raise VenueAdapterError(
                f"product info fetch failed for {pid} — refusing to place "
                f"stop with unquantized price (no magic-fallback policy)",
                code="product_info_unavailable",
            )
        if (
            prod.quote_increment is None or prod.quote_increment <= 0
            or prod.base_increment is None or prod.base_increment <= 0
        ):
            raise VenueAdapterError(
                f"product {pid} returned invalid increments: "
                f"quote_increment={prod.quote_increment} "
                f"base_increment={prod.base_increment} — refusing to place",
                code="product_info_invalid",
            )
        with _PRODUCT_INFO_CACHE_LOCK:
            _PRODUCT_INFO_CACHE[pid] = (prod, now + _PRODUCT_INFO_TTL_SEC)
        return prod

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
            # f-coinbase-list-open-orders-status-fix (2026-05-10): Coinbase
            # Advanced Trade rejects multi-status queries with "Cannot pass
            # multiple statuses with OPEN" (400 INVALID_ARGUMENT). Single
            # OPEN is what we want for the orphan-adoption + verify paths;
            # PENDING orders flip to OPEN within seconds of submission.
            kwargs: dict[str, Any] = {"order_status": ["OPEN"], "limit": limit}
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

    # f-coinbase-post-place-verify-routing-fix (2026-05-10): single-shot
    # broker-side state read for the bracket writer's post-place verify
    # loop. Coinbase Advanced Trade order states normalize to the same
    # vocabulary broker_service.verify_order_landed uses for Robinhood,
    # so the writer's verify state machine doesn't fork by venue.
    #
    # Mapping (Coinbase upper-case -> normalized lower-case):
    #
    #   PENDING   -> "unconfirmed"   (broker has not yet acked)
    #   OPEN      -> "confirmed"     (resting at broker; this is success)
    #   FILLED    -> "filled"
    #   CANCELLED -> "cancelled"
    #   EXPIRED   -> "cancelled"
    #   FAILED    -> "failed"
    #   (anything else) -> raw lower-cased so the caller can decide
    #
    # Adapter-disabled / 404 / transport errors return ok=False with
    # state=None; the writer treats that the same as a polled "no
    # observation" tick (matches Robinhood verify_order_landed's
    # behaviour while state is still "unconfirmed"). No magic-fallback
    # values: we never fabricate a "resting" state on missing data.
    _COINBASE_STATE_MAP = {
        "pending": "unconfirmed",
        "open": "confirmed",
        "filled": "filled",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "expired": "cancelled",
        "failed": "failed",
        "rejected": "rejected",
    }

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        """Return current broker-side state of *order_id* in the
        normalized Robinhood-compatible vocabulary.

        Returns:
            {"ok": True,  "state": "<normalized>", "raw": {...}}
            {"ok": False, "error": "not_found",    "state": None}
            {"ok": False, "error": "<reason>",     "state": None}

        Never raises into the caller for known broker-side errors; the
        Coinbase SDK's exceptions are caught and packaged so the
        writer's verify loop can keep polling. Hard adapter-config
        failures (no client / missing creds) come back as ok=False
        with an error string, NOT a fabricated state.
        """
        if not self.is_enabled():
            return {"ok": False, "error": "adapter_disabled", "state": None}
        if not order_id:
            return {"ok": False, "error": "empty_order_id", "state": None}
        allowed, retry_after = rate_limiter.try_acquire(_VENUE)
        if not allowed:
            try:
                venue_health.record_rate_limit_event(
                    venue=_VENUE, source="cb_get_order_status",
                )
            except Exception:
                pass
            return {
                "ok": False,
                "error": f"rate_limited:retry_after={retry_after}",
                "state": None,
            }
        try:
            c = self._require_client()
            resp = c.get_order(order_id=order_id)
            d = _as_dict(resp)
            inner = d.get("order") if isinstance(d.get("order"), dict) else d
            if not isinstance(inner, dict) or not inner:
                return {"ok": False, "error": "not_found", "state": None}
            # SDK is inconsistent across versions: newer surfaces use
            # `status`, older use `state`. Try `status` first, fall
            # back to `state`. Lower-case for vocabulary mapping.
            raw_state = inner.get("status") or inner.get("state")
            if not isinstance(raw_state, str) or not raw_state.strip():
                return {"ok": False, "error": "not_found", "state": None}
            key = raw_state.strip().lower()
            normalized = self._COINBASE_STATE_MAP.get(key, key)
            return {"ok": True, "state": normalized, "raw": inner}
        except VenueAdapterError as e:
            err = str(e)
            if "not found" in err.lower() or "404" in err:
                return {"ok": False, "error": "not_found", "state": None}
            return {"ok": False, "error": err, "state": None}
        except Exception as e:
            err = str(e)
            if "not found" in err.lower() or "404" in err:
                return {"ok": False, "error": "not_found", "state": None}
            _log.debug("[coinbase_spot] get_order_status failed: %s", e)
            return {"ok": False, "error": err, "state": None}

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
        # f-phase3-stop-bleed D3 — refuse malformed product_id locally so the
        # broker never sees it (avoids the 400 INVALID_ARGUMENT round-trip).
        try:
            product_id = _normalize_product_id(product_id)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        side_l = side.lower()
        if side_l not in ("buy", "sell"):
            return {"ok": False, "error": f"invalid side: {side}", "client_order_id": client_order_id}
        # f-portfolio-vs-pattern-breaker-separation — BUY-only gate. Portfolio
        # tier blocks every entry path when live + tripped; pass-through when
        # disabled, in shadow mode, or insufficient history (fail-OPEN).
        if side_l == "buy":
            _ok, _br_reason = _assert_portfolio_breaker_ok()
            if not _ok:
                return {
                    "ok": False,
                    "error": f"portfolio_breaker:{_br_reason}",
                    "client_order_id": client_order_id,
                }
        cid = client_order_id or str(uuid.uuid4())
        if idempotency_store.is_duplicate(cid, venue=_VENUE):
            return {"ok": False, "error": "duplicate client_order_id (recent)", "client_order_id": cid}
        allowed, retry_after = rate_limiter.try_acquire(_VENUE)
        if not allowed:
            # P1.2 — record the rate-limit exhaustion as an execution event so
            # the venue-health breaker can see hot-loop retry storms.
            try:
                venue_health.record_rate_limit_event(
                    venue=_VENUE, ticker=product_id, source="cb_place_market",
                )
            except Exception:
                pass
            return rate_limiter.rate_limited_response(_VENUE, retry_after, client_order_id=cid)
        pid = _to_product_id(product_id)
        try:
            prod = self._get_product_info_cached(pid)
        except VenueAdapterError as e:
            return {
                "ok": False,
                "error": str(e),
                "client_order_id": cid,
                "code": getattr(e, "code", None),
            }
        try:
            base_size = _quantize_size(float(base_size), prod.base_increment)
        except (TypeError, ValueError) as e:
            return {
                "ok": False,
                "error": f"base_size quantization failed: {e}",
                "client_order_id": cid,
            }
        if Decimal(base_size) <= 0:
            return {
                "ok": False,
                "error": f"quantized base_size <= 0 ({base_size})",
                "client_order_id": cid,
            }
        if prod.base_min_size is not None and Decimal(base_size) < Decimal(str(prod.base_min_size)):
            return {
                "ok": False,
                "error": f"base_size {base_size} below product base_min_size {prod.base_min_size}",
                "client_order_id": cid,
            }
        # P1.1 — about to hit the broker; pre-submit SUBMITTING transition so
        # the state machine sees the order exist before any ACK. Safe no-op
        # when the feature flag is off (standalone helper short-circuits).
        try:
            order_state_machine.record_transition_standalone(
                to_state=order_state_machine.OrderState.SUBMITTING,
                venue=_VENUE,
                source="cb_place_market",
                client_order_id=cid,
                raw_payload={"product_id": pid, "side": side.lower(), "base_size": str(base_size)},
            )
        except Exception:
            pass
        try:
            c = self._require_client()
            if side_l == "buy":
                resp = c.market_order_buy(client_order_id=cid, product_id=pid, base_size=str(base_size))
            elif side_l == "sell":
                resp = c.market_order_sell(client_order_id=cid, product_id=pid, base_size=str(base_size))
            rd = _as_dict(resp)
            ok = bool(rd.get("success"))
            sr = _as_dict(rd.get("success_response"))
            # Broker-provided order_id on success; may be missing on some partial
            # acknowledgements. Phase B (tech-debt) fix: do NOT fall back to
            # ``cid`` here — that made ``if ok or oid:`` below evaluate True for
            # every call (cid is always non-empty), silently turning broker-side
            # 429s and validation errors into ok=True. Real success requires the
            # broker's ``success`` flag.
            broker_oid = sr.get("order_id") or None
            if ok and broker_oid:
                oid = broker_oid
                idempotency_store.remember(
                    cid,
                    venue=_VENUE,
                    symbol=pid,
                    side=side_l,
                    qty=float(base_size or 0.0),
                    broker_order_id=broker_oid,
                    status="submitted",
                )
                # Broker accepted → emit ACK transition keyed on the
                # (now-known) broker order_id plus client_order_id.
                try:
                    order_state_machine.record_transition_standalone(
                        to_state=order_state_machine.OrderState.ACK,
                        venue=_VENUE,
                        source="cb_place_market",
                        order_id=broker_oid,
                        client_order_id=cid,
                        broker_status="accepted",
                        raw_payload={"product_id": pid, "order_id": oid},
                    )
                except Exception:
                    pass
                cb.clear_cache()
                return {
                    "ok": True,
                    "order_id": oid,
                    "client_order_id": cid,
                    "base_size": base_size,
                    "raw": rd,
                }
            if ok and not broker_oid:
                try:
                    order_state_machine.record_transition_standalone(
                        to_state=order_state_machine.OrderState.REJECTED,
                        venue=_VENUE,
                        source="cb_place_market",
                        client_order_id=cid,
                        broker_status="missing_order_id",
                        raw_payload={"product_id": pid, "response": rd},
                    )
                except Exception:
                    pass
                return {
                    "ok": False,
                    "error": "broker success response missing order_id",
                    "client_order_id": cid,
                    "raw": rd,
                }
            er = _as_dict(rd.get("error_response"))
            # Broker refused at submit — REJECTED.
            try:
                order_state_machine.record_transition_standalone(
                    to_state=order_state_machine.OrderState.REJECTED,
                    venue=_VENUE,
                    source="cb_place_market",
                    client_order_id=cid,
                    broker_status="rejected",
                    raw_payload={"error": er.get("message") or er.get("error") or ""},
                )
            except Exception:
                pass
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
        post_only: bool = False,
    ) -> dict[str, Any]:
        if not getattr(settings, "chili_coinbase_spot_adapter_enabled", True):
            return {"ok": False, "error": "adapter disabled"}
        # f-phase3-stop-bleed D3 — refuse malformed product_id locally so the
        # broker never sees it (avoids the 400 INVALID_ARGUMENT round-trip).
        try:
            product_id = _normalize_product_id(product_id)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        side_l = side.lower()
        if side_l not in ("buy", "sell"):
            return {"ok": False, "error": f"invalid side: {side}", "client_order_id": client_order_id}
        cid = client_order_id or str(uuid.uuid4())
        if idempotency_store.is_duplicate(cid, venue=_VENUE):
            return {"ok": False, "error": "duplicate client_order_id (recent)", "client_order_id": cid}
        pid = _to_product_id(product_id)
        try:
            prod = self._get_product_info_cached(pid)
        except VenueAdapterError as e:
            return {
                "ok": False,
                "error": str(e),
                "client_order_id": cid,
                "code": getattr(e, "code", None),
            }
        try:
            base_size = _quantize_size(float(base_size), prod.base_increment)
            price_mode = "down" if side_l == "buy" else "up"
            limit_price = _quantize_price(float(limit_price), prod.quote_increment, mode=price_mode)
        except (TypeError, ValueError) as e:
            return {
                "ok": False,
                "error": f"limit order quantization failed: {e}",
                "client_order_id": cid,
            }
        if Decimal(base_size) <= 0:
            return {
                "ok": False,
                "error": f"quantized base_size <= 0 ({base_size})",
                "client_order_id": cid,
            }
        if prod.base_min_size is not None and Decimal(base_size) < Decimal(str(prod.base_min_size)):
            return {
                "ok": False,
                "error": f"base_size {base_size} below product base_min_size {prod.base_min_size}",
                "client_order_id": cid,
            }
        if Decimal(limit_price) <= 0:
            return {
                "ok": False,
                "error": f"quantized limit_price <= 0 ({limit_price})",
                "client_order_id": cid,
            }
        if prod.quote_min_size is not None:
            notional = Decimal(base_size) * Decimal(limit_price)
            if notional < Decimal(str(prod.quote_min_size)):
                return {
                    "ok": False,
                    "error": f"notional {notional} below product quote_min_size {prod.quote_min_size}",
                    "client_order_id": cid,
                }
        # f-phase3-stop-bleed D4 — BUY pre-flight against local
        # buying-power cache. Refuses orders the broker would reject as
        # Insufficient balance, sparing the round-trip + rate-limit charge.
        if side_l == "buy":
            _preflight_refusal = _coinbase_preflight_cash_check(
                product_id=product_id,
                base_size=base_size,
                limit_price=limit_price,
            )
            if _preflight_refusal is not None:
                return _preflight_refusal
        # f-portfolio-vs-pattern-breaker-separation — BUY-only gate. Portfolio
        # tier runs AFTER the local cash preflight so the cheaper guard fires
        # first; the breaker still short-circuits before idempotency/rate-limit.
        if side_l == "buy":
            _ok, _br_reason = _assert_portfolio_breaker_ok()
            if not _ok:
                return {
                    "ok": False,
                    "error": f"portfolio_breaker:{_br_reason}",
                    "client_order_id": cid,
                }
        allowed, retry_after = rate_limiter.try_acquire(_VENUE)
        if not allowed:
            # P1.2 — record rate-limit exhaustion for the health breaker.
            try:
                venue_health.record_rate_limit_event(
                    venue=_VENUE, ticker=product_id, source="cb_place_limit",
                )
            except Exception:
                pass
            return rate_limiter.rate_limited_response(_VENUE, retry_after, client_order_id=cid)
        # P1.1 — SUBMITTING before broker call.
        try:
            order_state_machine.record_transition_standalone(
                to_state=order_state_machine.OrderState.SUBMITTING,
                venue=_VENUE,
                source="cb_place_limit",
                client_order_id=cid,
                raw_payload={
                    "product_id": pid,
                    "side": side.lower(),
                    "base_size": str(base_size),
                    "limit_price": str(limit_price),
                },
            )
        except Exception:
            pass
        try:
            c = self._require_client()
            # f-coinbase-maker-only-routing (2026-05-19): when post_only is
            # True, prefer the SDK's *_post_only variant so the broker
            # rejects orders that would cross as taker. Falls back to the
            # post_only=True kwarg form on older SDKs. Mirrors the dispatch
            # in coinbase_service.place_buy_order (added 2026-05-08 for
            # the fast-path maker-only executor).
            _common_kwargs = dict(
                client_order_id=cid,
                product_id=pid,
                base_size=str(base_size),
                limit_price=str(limit_price),
            )
            if side_l == "buy":
                if post_only:
                    _po_fn = getattr(c, "limit_order_gtc_buy_post_only", None)
                    if callable(_po_fn):
                        resp = _po_fn(**_common_kwargs)
                    else:
                        resp = c.limit_order_gtc_buy(post_only=True, **_common_kwargs)
                else:
                    resp = c.limit_order_gtc_buy(**_common_kwargs)
            elif side_l == "sell":
                if post_only:
                    _po_fn = getattr(c, "limit_order_gtc_sell_post_only", None)
                    if callable(_po_fn):
                        resp = _po_fn(**_common_kwargs)
                    else:
                        resp = c.limit_order_gtc_sell(post_only=True, **_common_kwargs)
                else:
                    resp = c.limit_order_gtc_sell(**_common_kwargs)
            rd = _as_dict(resp)
            ok = bool(rd.get("success"))
            sr = _as_dict(rd.get("success_response"))
            broker_oid = sr.get("order_id") or None
            if ok and broker_oid:
                idempotency_store.remember(
                    cid,
                    venue=_VENUE,
                    symbol=pid,
                    side=side_l,
                    qty=float(base_size or 0.0),
                    broker_order_id=broker_oid,
                    status="submitted",
                )
                try:
                    order_state_machine.record_transition_standalone(
                        to_state=order_state_machine.OrderState.ACK,
                        venue=_VENUE,
                        source="cb_place_limit",
                        order_id=broker_oid,
                        client_order_id=cid,
                        broker_status="accepted",
                        raw_payload={"product_id": pid, "order_id": broker_oid},
                    )
                except Exception:
                    pass
                cb.clear_cache()
                return {
                    "ok": True,
                    "order_id": broker_oid,
                    "client_order_id": cid,
                    "base_size": base_size,
                    "limit_price": limit_price,
                    "raw": rd,
                }
            if ok and not broker_oid:
                try:
                    order_state_machine.record_transition_standalone(
                        to_state=order_state_machine.OrderState.REJECTED,
                        venue=_VENUE,
                        source="cb_place_limit",
                        client_order_id=cid,
                        broker_status="missing_order_id",
                        raw_payload={"product_id": pid, "response": rd},
                    )
                except Exception:
                    pass
                return {
                    "ok": False,
                    "error": "broker success response missing order_id",
                    "client_order_id": cid,
                    "raw": rd,
                }
            er = _as_dict(rd.get("error_response"))
            try:
                order_state_machine.record_transition_standalone(
                    to_state=order_state_machine.OrderState.REJECTED,
                    venue=_VENUE,
                    source="cb_place_limit",
                    client_order_id=cid,
                    broker_status="rejected",
                    raw_payload={"error": er.get("message") or er.get("error") or ""},
                )
            except Exception:
                pass
            return {"ok": False, "error": er.get("message") or er.get("error") or str(rd), "client_order_id": cid}
        except VenueAdapterError as e:
            return {"ok": False, "error": str(e), "client_order_id": cid}
        except Exception as e:
            _log.exception("[coinbase_spot] place_limit_order_gtc failed")
            return {"ok": False, "error": str(e), "client_order_id": cid}

    def place_stop_limit_order_gtc(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        stop_price: str,
        limit_price: str,
        client_order_id: Optional[str] = None,
        stop_direction: Optional[str] = None,
    ) -> dict[str, Any]:
        """f-coinbase-autotrader-enablement-phase-4 (2026-05-09).

        Place a stop-limit GTC order via Coinbase Advanced Trade.

        Mirrors :meth:`place_limit_order_gtc`'s envelope shape so the
        bracket writer can dispatch uniformly:
            {"ok": True,  "order_id": "...", "client_order_id": "...", "raw": {...}}
            {"ok": False, "error": "...", "client_order_id": "..."}

        SDK call: ``stop_limit_order_gtc_buy`` /
        ``stop_limit_order_gtc_sell`` per
        https://docs.cdp.coinbase.com/advanced-trade/reference/createorder
        (config_key=``stop_limit_stop_limit_gtc``). The bracket writer's
        SELL stop-loss case uses ``side='sell'`` +
        ``stop_direction='STOP_DIRECTION_STOP_DOWN'`` (default when
        side='sell' and ``stop_direction is None``).

        ``limit_price`` should be set slightly below ``stop_price`` for
        SELL stops so the limit accepts a fill on the trigger move.
        Caller responsibility (the bracket writer applies a
        settings-tunable buffer).
        """
        if not getattr(settings, "chili_coinbase_spot_adapter_enabled", True):
            return {"ok": False, "error": "adapter disabled"}
        # f-phase3-stop-bleed D3 — refuse malformed product_id locally so the
        # broker never sees it (avoids the 400 INVALID_ARGUMENT round-trip).
        try:
            product_id = _normalize_product_id(product_id)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        # f-phase3-stop-bleed D4 — BUY pre-flight against local
        # buying-power cache. Stop-limit BUYs are rare (this method is
        # typically used for SELL stop-losses) but the guard covers them.
        if side.upper() == "BUY":
            _preflight_refusal = _coinbase_preflight_cash_check(
                product_id=product_id,
                base_size=base_size,
                limit_price=limit_price,
            )
            if _preflight_refusal is not None:
                return _preflight_refusal
        # f-portfolio-vs-pattern-breaker-separation — BUY-only gate. Rare
        # entry path (this method is mostly SELL stop-losses) but if it ever
        # gates a BUY stop the portfolio tier still applies.
        if side.lower() == "buy":
            _ok, _br_reason = _assert_portfolio_breaker_ok()
            if not _ok:
                return {
                    "ok": False,
                    "error": f"portfolio_breaker:{_br_reason}",
                    "client_order_id": client_order_id,
                }
        cid = client_order_id or str(uuid.uuid4())
        if idempotency_store.is_duplicate(cid, venue=_VENUE):
            return {
                "ok": False,
                "error": "duplicate client_order_id (recent)",
                "client_order_id": cid,
            }
        allowed, retry_after = rate_limiter.try_acquire(_VENUE)
        if not allowed:
            try:
                venue_health.record_rate_limit_event(
                    venue=_VENUE, ticker=product_id, source="cb_place_stop_limit",
                )
            except Exception:
                pass
            return rate_limiter.rate_limited_response(
                _VENUE, retry_after, client_order_id=cid,
            )
        pid = _to_product_id(product_id)
        side_l = side.lower()
        if side_l not in ("buy", "sell"):
            return {"ok": False, "error": f"invalid side: {side}", "client_order_id": cid}
        # Default stop_direction: SELL stop-loss triggers on price-DOWN;
        # BUY stop (less common) triggers on price-UP. Per Coinbase
        # Advanced Trade docs.
        sd = (stop_direction or "").strip().upper()
        if not sd:
            sd = (
                "STOP_DIRECTION_STOP_DOWN"
                if side_l == "sell"
                else "STOP_DIRECTION_STOP_UP"
            )

        # Quantize prices and size to product increments. Coinbase rejects
        # any field finer than the product's quote_increment / base_increment
        # (smoking gun: ALEPH-USD "Too many decimals in order price",
        # 2026-05-10). Fetch via cached helper; if product info is
        # unavailable or invalid, refuse rather than guess (no
        # magic-fallback per COWORK_ADVISOR_BRIEF §2.6).
        try:
            prod = self._get_product_info_cached(pid)
        except VenueAdapterError as e:
            return {
                "ok": False,
                "error": str(e),
                "client_order_id": cid,
                "code": getattr(e, "code", None),
            }
        try:
            base_size_in = float(base_size)
            stop_price_in = float(stop_price)
            limit_price_in = float(limit_price)
        except (TypeError, ValueError) as e:
            return {
                "ok": False,
                "error": f"non-numeric input: {e}",
                "client_order_id": cid,
            }
        try:
            price_mode = "down" if side_l == "sell" else "up"
            q_size = _quantize_size(base_size_in, prod.base_increment)
            q_stop = _quantize_price(stop_price_in, prod.quote_increment, mode=price_mode)
            q_limit = _quantize_price(limit_price_in, prod.quote_increment, mode=price_mode)
        except ValueError as e:
            return {
                "ok": False,
                "error": f"quantization failed: {e}",
                "client_order_id": cid,
            }
        if Decimal(q_stop) <= 0:
            return {
                "ok": False,
                "error": f"quantized stop_price <= 0 ({q_stop})",
                "client_order_id": cid,
            }
        # Preserve SELL: limit ≤ stop; BUY: limit ≥ stop. If quantization
        # collapsed the buffer, step limit one increment further from stop.
        d_stop = Decimal(q_stop)
        d_limit = Decimal(q_limit)
        d_inc = Decimal(str(prod.quote_increment))
        if side_l == "sell" and d_limit > d_stop:
            nudged = (d_stop - d_inc).quantize(d_inc)
            _log.debug(
                "[coinbase_spot] sell stop nudged limit %s → %s (one tick "
                "below stop %s) for %s", q_limit, format(nudged, "f"), q_stop, pid,
            )
            q_limit = format(nudged, "f")
        elif side_l == "buy" and d_limit < d_stop:
            nudged = (d_stop + d_inc).quantize(d_inc)
            _log.debug(
                "[coinbase_spot] buy stop nudged limit %s → %s (one tick "
                "above stop %s) for %s", q_limit, format(nudged, "f"), q_stop, pid,
            )
            q_limit = format(nudged, "f")
        # min_market_funds enforcement: reject below-minimum notional with
        # explicit log (cap-to-min would silently mutate intent; that's a
        # data-integrity violation per the no-magic-fallback rule).
        notional = float(Decimal(q_size) * Decimal(q_stop))
        if prod.min_market_funds is not None and notional < prod.min_market_funds:
            _log.warning(
                "[coinbase_spot] place_stop_limit_order_gtc rejected: "
                "notional %.6f USD < min_market_funds %.6f USD for %s "
                "(size=%s × stop=%s)",
                notional, prod.min_market_funds, pid, q_size, q_stop,
            )
            return {
                "ok": False,
                "error": (
                    f"notional {notional:.6f} below product min_market_funds "
                    f"{prod.min_market_funds:.6f}"
                ),
                "client_order_id": cid,
            }
        base_size = q_size
        stop_price = q_stop
        limit_price = q_limit

        try:
            order_state_machine.record_transition_standalone(
                to_state=order_state_machine.OrderState.SUBMITTING,
                venue=_VENUE,
                source="cb_place_stop_limit",
                client_order_id=cid,
                raw_payload={
                    "product_id": pid,
                    "side": side_l,
                    "base_size": str(base_size),
                    "stop_price": str(stop_price),
                    "limit_price": str(limit_price),
                    "stop_direction": sd,
                },
            )
        except Exception:
            pass

        try:
            c = self._require_client()
            kwargs = dict(
                client_order_id=cid,
                product_id=pid,
                base_size=str(base_size),
                limit_price=str(limit_price),
                stop_price=str(stop_price),
                stop_direction=sd,
            )
            if side_l == "buy":
                resp = c.stop_limit_order_gtc_buy(**kwargs)
            else:
                resp = c.stop_limit_order_gtc_sell(**kwargs)
            rd = _as_dict(resp)
            ok = bool(rd.get("success"))
            sr = _as_dict(rd.get("success_response"))
            oid = sr.get("order_id") or cid
            if ok or (oid and oid != cid):
                idempotency_store.remember(
                    cid,
                    venue=_VENUE,
                    symbol=pid,
                    side=side_l,
                    qty=float(base_size or 0.0),
                    broker_order_id=oid if oid != cid else None,
                    status="submitted",
                )
                try:
                    order_state_machine.record_transition_standalone(
                        to_state=order_state_machine.OrderState.ACK,
                        venue=_VENUE,
                        source="cb_place_stop_limit",
                        order_id=oid if oid != cid else None,
                        client_order_id=cid,
                        broker_status="accepted",
                        raw_payload={"product_id": pid, "order_id": oid},
                    )
                except Exception:
                    pass
                cb.clear_cache()
                return {
                    "ok": True, "order_id": oid,
                    "client_order_id": cid, "raw": rd,
                }
            er = _as_dict(rd.get("error_response"))
            try:
                order_state_machine.record_transition_standalone(
                    to_state=order_state_machine.OrderState.REJECTED,
                    venue=_VENUE,
                    source="cb_place_stop_limit",
                    client_order_id=cid,
                    broker_status="rejected",
                    raw_payload={
                        "error": er.get("message") or er.get("error") or "",
                    },
                )
            except Exception:
                pass
            return {
                "ok": False,
                "error": er.get("message") or er.get("error") or str(rd),
                "client_order_id": cid,
            }
        except VenueAdapterError as e:
            return {"ok": False, "error": str(e), "client_order_id": cid}
        except Exception as e:
            _log.exception("[coinbase_spot] place_stop_limit_order_gtc failed")
            return {"ok": False, "error": str(e), "client_order_id": cid}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        if not self.is_enabled():
            return {"ok": False, "error": "adapter disabled"}
        allowed, retry_after = rate_limiter.try_acquire(_VENUE)
        if not allowed:
            # P1.2 — record rate-limit exhaustion for the health breaker.
            try:
                venue_health.record_rate_limit_event(
                    venue=_VENUE, source="cb_cancel",
                )
            except Exception:
                pass
            return rate_limiter.rate_limited_response(_VENUE, retry_after)
        try:
            c = self._require_client()
            resp = c.cancel_orders(order_ids=[order_id])
            d = _as_dict(resp)
            cb.clear_cache()
            # P1.1 — record CANCELLED transition so reconciliation / latency
            # rollups can see the order's terminal state. Broker-side cancel
            # might be async; we're optimistic here (the transition table
            # treats CANCELLED as terminal so any later ACK re-poll is a no-op).
            try:
                order_state_machine.record_transition_standalone(
                    to_state=order_state_machine.OrderState.CANCELLED,
                    venue=_VENUE,
                    source="cb_cancel",
                    order_id=order_id,
                    broker_status="cancelled",
                    raw_payload={"cancel_response": d},
                )
            except Exception:
                pass
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
