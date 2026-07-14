"""VenueAdapter implementation for Robinhood equities via robin_stocks.

Delegates to ``broker_service`` for authentication, order placement, and position queries.
Symbol convention: plain tickers (``AAPL``), not crypto-style product IDs (``BTC-USD``).
"""

from __future__ import annotations

import hashlib
import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

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
    is_fresh_enough,
)

logger = logging.getLogger(__name__)

_VENUE = "robinhood"
_KNOWN_NUMERIC_CRYPTO_BASES = frozenset({"00"})


def reset_duplicate_client_order_guard_for_tests() -> None:
    """Clear in-process duplicate client_order_id cache (pytest only).

    Parity with ``coinbase_spot.reset_duplicate_client_order_guard_for_tests`` —
    both venues share the same ``idempotency_store`` memory guard, but
    exposing the symbol here lets Robinhood-specific tests reset state by
    importing from ``robinhood_spot`` directly (matches the discoverability
    pattern the Coinbase tests already use).

    Also resets the shared ``rate_limiter`` bucket state — see the
    matching Coinbase helper for rationale (Phase B tech-debt).

    Does NOT truncate the DB ``venue_order_idempotency`` table — tests that
    need a clean DB row set should use their own fixtures.
    """
    idempotency_store.reset_for_tests()
    rate_limiter.reset_for_tests()


# ── Helpers ────────────────────────────────────────────────────────────


def _sf(x: Any) -> Optional[float]:
    """Safe float conversion."""
    if x is None:
        return None
    try:
        return float(x)
    except (ValueError, TypeError):
        return None


def _rh_strict_paginated_rows(
    rh: Any,
    url: str,
    *,
    payload: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Read every Robinhood page without the library's partial-page fallback."""
    rows: list[dict[str, Any]] = []
    next_url = str(url or "").strip()
    seen_urls: set[str] = set()
    params = dict(payload or {})
    for _page in range(100):
        if not next_url or next_url in seen_urls:
            raise VenueAdapterError("Robinhood pagination identity invalid")
        seen_urls.add(next_url)
        response = rh.helper.SESSION.get(next_url, params=params or None)
        response.raise_for_status()
        body = response.json()
        if not (
            isinstance(body, dict)
            and isinstance(body.get("results"), list)
            and "next" in body
        ):
            raise VenueAdapterError("Robinhood pagination payload malformed")
        for row in body["results"]:
            if not isinstance(row, dict):
                raise VenueAdapterError("Robinhood row malformed")
            rows.append(row)
        raw_next = body.get("next")
        if raw_next in (None, ""):
            return rows
        next_url = str(raw_next).strip()
        params = {}
    raise VenueAdapterError("Robinhood pagination exceeded safety bound")


def _rh_strict_instrument_symbol(rh: Any, instrument_url: str) -> str:
    url = str(instrument_url or "").strip()
    if not url:
        raise VenueAdapterError("Robinhood instrument identity missing")
    response = rh.helper.SESSION.get(url)
    response.raise_for_status()
    body = response.json()
    symbol = str(body.get("symbol") or "").strip().upper() if isinstance(body, dict) else ""
    if not symbol:
        raise VenueAdapterError("Robinhood instrument symbol unreadable")
    return symbol


def _rh_strict_account_snapshot(rh: Any) -> dict[str, Any]:
    """Return the complete authenticated Robinhood account generation."""
    rows = _rh_strict_paginated_rows(rh, rh.urls.account_profile_url())
    if not rows:
        raise VenueAdapterError("Robinhood account identity absent")
    identities: list[tuple[str, str]] = []
    seen_numbers: set[str] = set()
    seen_urls: set[str] = set()
    for row in rows:
        number = str(row.get("account_number") or "").strip()
        account_url = str(row.get("url") or "").strip()
        if (
            not number
            or not account_url
            or number in seen_numbers
            or account_url in seen_urls
        ):
            raise VenueAdapterError("Robinhood account identity malformed")
        seen_numbers.add(number)
        seen_urls.add(account_url)
        identities.append((number, account_url))
    canonical = "\n".join(
        f"account:{number}:{account_url}"
        for number, account_url in sorted(identities)
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "identity": f"robinhood_spot:v1:{digest}",
        "account_urls": seen_urls,
    }


def _to_ticker(product_id: str) -> str:
    """Normalize product_id to a plain stock ticker (strip -USD suffix if present)."""
    s = (product_id or "").strip().upper()
    if s.endswith("-USD"):
        s = s[:-4]
    return s


def _is_crypto_product(product_id: str) -> bool:
    """True when ``product_id`` looks like a crypto-USD pair ('BTC-USD').

    The convention CHILI carries through alerts/trades is ``BASE-USD`` for
    crypto. Equity tickers don't contain a dash. Numeric-prefixed tickers
    (like the 4-digit Asian ADRs that surface as e.g. ``9988-USD`` on
    some feeds) are excluded — that pattern is data noise, not a real
    crypto pair.
    """
    s = (product_id or "").strip().upper()
    if s in _KNOWN_NUMERIC_CRYPTO_BASES:
        return True
    if not s.endswith("-USD"):
        return False
    base = s[:-4]
    return bool(base) and (not base.isdigit() or base in _KNOWN_NUMERIC_CRYPTO_BASES)


def _now_freshness(
    max_age: float = 15.0,
    *,
    provider_time_utc: datetime | None = None,
) -> FreshnessMeta:
    return FreshnessMeta(
        retrieved_at_utc=datetime.now(timezone.utc),
        provider_time_utc=provider_time_utc,
        max_age_seconds=max_age,
    )


def _parse_rh_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        raw = raw.replace("Z", "+00:00")
        if "." in raw:
            head, tail = raw.split(".", 1)
            if "+" in tail:
                frac, tz = tail.split("+", 1)
                raw = f"{head}.{frac[:6]}+{tz}"
            elif "-" in tail:
                frac, tz = tail.split("-", 1)
                raw = f"{head}.{frac[:6]}-{tz}"
            else:
                raw = f"{head}.{tail[:6]}"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _latest_quote_timestamp(raw: dict[str, Any]) -> datetime | None:
    candidates = [
        _parse_rh_timestamp(raw.get(key))
        for key in (
            "venue_bid_time",
            "venue_ask_time",
            "venue_last_trade_time",
            "venue_last_non_reg_trade_time",
            "updated_at",
        )
    ]
    candidates = [dt for dt in candidates if dt is not None]
    return max(candidates) if candidates else None


def _legend_boats_ticker(
    ticker: str,
    anchor_raw: dict[str, Any] | None = None,
    *,
    allow_stale: bool = True,
) -> tuple[Optional[NormalizedTicker], FreshnessMeta]:
    """Fallback to the Blue Ocean feed that Robinhood Legend uses overnight."""
    from ....config import settings

    max_age = float(getattr(settings, "chili_robinhood_legend_quote_max_age_seconds", 1200.0))
    fresh = _now_freshness(max_age=max_age)
    if not bool(getattr(settings, "chili_robinhood_legend_quote_fallback_enabled", True)):
        return None, fresh
    try:
        from ..tradingview_blue_ocean import fetch_boats_quote

        snap = fetch_boats_quote(ticker)
    except Exception as exc:
        logger.debug("[rh_adapter] Legend/BOATS quote failed ticker=%s err=%s", ticker, exc)
        return None, fresh
    if not snap:
        return None, fresh
    provider_dt = snap.get("provider_time_utc")
    if not isinstance(provider_dt, datetime):
        provider_dt = _parse_rh_timestamp(snap.get("quote_ts"))
    fresh = _now_freshness(max_age=max_age, provider_time_utc=provider_dt)
    stale = not is_fresh_enough(fresh)
    if stale:
        logger.debug(
            "[rh_adapter] Legend/BOATS quote stale ticker=%s age=%.3fs max=%.3fs",
            ticker,
            fresh.age_seconds(),
            fresh.max_age_seconds,
        )
    last = _sf(snap.get("price")) or _sf(snap.get("last_price"))
    if last is None or last <= 0:
        return None, fresh
    if stale and not allow_stale:
        return None, fresh
    raw = dict(snap)
    if isinstance(anchor_raw, dict):
        for key in ("previous_close", "adjusted_previous_close", "regular_market_previous_close"):
            if anchor_raw.get(key) is not None and raw.get(key) is None:
                raw[key] = anchor_raw.get(key)
    raw["source"] = "tradingview_boats"
    return NormalizedTicker(
        product_id=ticker,
        bid=None,
        ask=None,
        mid=last,
        spread_abs=None,
        spread_bps=None,
        last_price=last,
        last_size=None,
        bid_size=None,
        ask_size=None,
        base_volume_24h=_sf(snap.get("volume")),
        quote_volume_24h=None,
        freshness=fresh,
        raw=raw,
    ), fresh


def _normalize_rh_equity_quote(
    ticker: str,
    q: dict[str, Any],
    fresh: FreshnessMeta,
    *,
    allow_regular_last: bool,
) -> Optional[NormalizedTicker]:
    bid = _sf(q.get("bid_price"))
    ask = _sf(q.get("ask_price"))
    regular_last = _sf(q.get("last_trade_price"))
    extended_last = _sf(q.get("last_extended_hours_trade_price"))
    non_reg_last = _sf(q.get("last_non_reg_trade_price"))
    last = extended_last or non_reg_last or (regular_last if allow_regular_last else None)
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
        return None

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
        raw=q,
    )


def _freshness_provider_time(meta: FreshnessMeta | None) -> datetime | None:
    return getattr(meta, "provider_time_utc", None) if meta is not None else None


def _iqfeed_l1_ticker(
    ticker: str,
) -> tuple[Optional[NormalizedTicker], FreshnessMeta] | None:
    """IQFeed-L1-first BBO read (gated by chili_momentum_entry_gate_iqfeed_bbo_first).

    The iqfeed_trade_bridge mirrors tick-level IQFeed L1 into momentum_nbbo_spread_tape with
    source='iqfeed_l1' (d473331). This reads the FRESHEST such row for ``ticker`` and returns a
    NormalizedTicker stamped with the row's true observed_at as provider time — so downstream
    is_fresh_enough / stale_bbo see the real quote age, not insert-time. RECENCY-GATED here by the
    same adaptive floor as the stale-quote window (chili_momentum_quote_freshness_floor_seconds,
    one documented base): a row older than the floor returns ``None`` so the caller falls through to
    Massive WS -> robin_stocks -> Legend. Returns ``None`` on absent/stale/error (fail-OPEN to the
    existing chain — this only ADDS a fresher first source, never removes a fallback). Best-effort,
    never raises."""
    from ....config import settings

    if not bool(getattr(settings, "chili_momentum_entry_gate_iqfeed_bbo_first", True)):
        return None
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    floor_s = float(
        getattr(settings, "chili_momentum_quote_freshness_floor_seconds", 15.0) or 15.0
    )
    try:
        from sqlalchemy import text as _text

        from ....db import SessionLocal
    except Exception:
        return None
    db = SessionLocal()
    try:
        row = db.execute(
            _text(
                "SELECT bid, ask, mid, observed_at "
                "FROM momentum_nbbo_spread_tape "
                "WHERE symbol = :s AND source = 'iqfeed_l1' "
                "  AND observed_at > (now() at time zone 'utc') - make_interval(secs => :w) "
                "ORDER BY observed_at DESC LIMIT 1"
            ),
            {"s": sym, "w": floor_s},
        ).fetchone()
    except Exception:
        return None
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass
    if row is None:
        return None
    try:
        bid = float(row[0]) if row[0] is not None else None
        ask = float(row[1]) if row[1] is not None else None
    except (TypeError, ValueError):
        return None
    if not (bid and ask and bid > 0 and ask > 0 and ask >= bid):
        return None  # invalid book -> fall through (do NOT fabricate a quote)
    observed_at = row[3]
    # The tape stores observed_at as UTC (naive in TIMESTAMP cols, aware in TIMESTAMPTZ); coerce
    # to aware-UTC so FreshnessMeta age math is correct. This is the IQFeed print time (real age).
    prov: datetime | None
    try:
        if isinstance(observed_at, datetime):
            prov = (
                observed_at.replace(tzinfo=timezone.utc)
                if observed_at.tzinfo is None
                else observed_at.astimezone(timezone.utc)
            )
        else:
            prov = None
    except Exception:
        prov = None
    fresh = _now_freshness(provider_time_utc=prov)
    mid = float(row[2]) if row[2] is not None else (bid + ask) / 2.0
    spread_abs = ask - bid
    spread_bps = (spread_abs / mid) * 10_000.0 if mid > 0 else None
    return (
        NormalizedTicker(
            product_id=sym,
            bid=bid,
            ask=ask,
            mid=mid,
            spread_abs=spread_abs,
            spread_bps=spread_bps,
            last_price=mid,
            last_size=None,
            bid_size=None,
            ask_size=None,
            base_volume_24h=None,
            quote_volume_24h=None,
            freshness=fresh,
            raw={"source": "iqfeed_l1"},
        ),
        fresh,
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


def _normalize_rh_order_truth(
    od: dict[str, Any],
    *,
    rh: Any | None = None,
) -> Optional[NormalizedOrder]:
    """Preserve broker order state for strict lifecycle proofs.

    The legacy normalized surface maps a broker ``filled`` order to Chili trade
    status ``open`` (meaning an open position).  Terminalization needs the broker
    order lifecycle instead, otherwise a completed order looks like it is still
    resting and cannot be proved terminal.
    """
    row = dict(od)
    if not str(row.get("symbol") or row.get("chain_symbol") or "").strip():
        instrument_url = str(row.get("instrument") or "").strip()
        if not instrument_url:
            return None
        try:
            resolved_symbol = _rh_strict_instrument_symbol(rh, instrument_url) if rh else ""
        except Exception:
            resolved_symbol = ""
        if not resolved_symbol:
            return None
        row["symbol"] = resolved_symbol
    broker_status = str(row.get("state") or row.get("status") or "").strip().lower()
    raw_filled = row.get("cumulative_quantity")
    filled = _sf(raw_filled)
    if (
        not str(row.get("id") or "").strip()
        or not broker_status
        or filled is None
        or not math.isfinite(filled)
        or filled < 0.0
    ):
        return None
    normalized = _normalize_rh_order(row)
    return NormalizedOrder(
        order_id=normalized.order_id,
        client_order_id=normalized.client_order_id,
        product_id=normalized.product_id,
        side=normalized.side,
        status=broker_status or "unknown",
        order_type=normalized.order_type,
        filled_size=normalized.filled_size,
        average_filled_price=normalized.average_filled_price,
        created_time=normalized.created_time,
        raw=normalized.raw,
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


class RobinhoodSpotAdapter(VenueAdapter):
    """VenueAdapter for Robinhood equities via broker_service + robin_stocks.

    Explicitly declares the ``VenueAdapter`` protocol so static type
    checkers validate every method signature — a mismatch here shows up
    at import time instead of as a silent method-not-found at the next
    live fill.
    """

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
        if _is_crypto_product(product_id):
            return None, fresh
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

        # Task KK — crypto bases (BTC, ETH, …) aren't in the equity
        # universe; rh.stocks.get_quotes returns garbage for them. Route
        # through the crypto endpoint so the monitor's live price tracks
        # the venue we'd actually fill on.
        if _is_crypto_product(product_id):
            try:
                from ...broker_service import get_crypto_quote
                q = get_crypto_quote(ticker) or {}
                bid = _sf(q.get("bid_price"))
                ask = _sf(q.get("ask_price"))
                last = _sf(q.get("mark_price")) or _sf(q.get("high_price"))
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
                    bid=bid, ask=ask, mid=mid,
                    spread_abs=spread_abs, spread_bps=spread_bps,
                    last_price=last,
                    last_size=None, bid_size=None, ask_size=None,
                    base_volume_24h=_sf(q.get("volume")),
                    quote_volume_24h=None,
                    freshness=fresh,
                    raw=q,
                ), fresh
            except Exception as e:
                logger.warning("[rh_adapter] get_best_bid_ask crypto(%s) failed: %s", ticker, e)
                return None, fresh

        # IQFeed-L1-FIRST (chili_momentum_entry_gate_iqfeed_bbo_first, default ON). The
        # iqfeed_trade_bridge mirrors tick-level IQFeed L1 into momentum_nbbo_spread_tape
        # (source='iqfeed_l1') at ~1-2s freshness on real movers; the entry gate read it as the
        # FRESHEST source so wide-spread names stop false-blocking on stale_bbo against a 10-270s
        # WS quote. Recency-gated to the adaptive freshness floor inside the helper; absent/stale/
        # invalid -> None -> fall through to the Massive-WS-first chain below (byte-identical when
        # the flag is OFF, since the helper short-circuits to None).
        try:
            _iq = _iqfeed_l1_ticker(ticker)
        except Exception:
            _iq = None
        if _iq is not None:
            return _iq

        # Real-time NBBO from the Massive WebSocket when the feed is live in this
        # process — beats the REST round-trip AND its quote cache. get_ws_quote
        # enforces its own 5s staleness, and the WS receive time flows into
        # FreshnessMeta so the stale_bbo gate / halt detection see push-quotes with
        # the same semantics as pulled ones. Absent/stale -> REST path below.
        try:
            from ...massive_client import get_ws_quote

            ws_q = get_ws_quote(ticker)
        except Exception:
            ws_q = None
        if ws_q is not None and ws_q.bid and ws_q.ask and ws_q.bid > 0 and ws_q.ask >= ws_q.bid:
            ws_fresh = _now_freshness(
                provider_time_utc=datetime.fromtimestamp(ws_q.timestamp, tz=timezone.utc)
            )
            mid = (ws_q.bid + ws_q.ask) / 2.0
            spread_abs = ws_q.ask - ws_q.bid
            return NormalizedTicker(
                product_id=ticker,
                bid=ws_q.bid, ask=ws_q.ask, mid=mid,
                spread_abs=spread_abs,
                spread_bps=(spread_abs / mid) * 10_000 if mid > 0 else None,
                last_price=ws_q.price,
                last_size=None, bid_size=ws_q.bid_size, ask_size=ws_q.ask_size,
                base_volume_24h=None, quote_volume_24h=None,
                freshness=ws_fresh,
                raw={"source": "massive_ws"},
            ), ws_fresh

        try:
            import robin_stocks.robinhood as rh

            quotes = rh.stocks.get_quotes([ticker])
            q = quotes[0] if quotes else None
            if not q or not isinstance(q, dict):
                boats_ticker, boats_fresh = _legend_boats_ticker(ticker, allow_stale=True)
                return boats_ticker, boats_fresh
            fresh = _now_freshness(provider_time_utc=_latest_quote_timestamp(q))
            rh_is_fresh = is_fresh_enough(fresh)
            rh_ticker = _normalize_rh_equity_quote(
                ticker,
                q,
                fresh,
                allow_regular_last=rh_is_fresh,
            )
            if not rh_is_fresh:
                boats_ticker, boats_fresh = _legend_boats_ticker(
                    ticker,
                    q,
                    allow_stale=True,
                )
                if boats_ticker is not None:
                    rh_ts = _freshness_provider_time(fresh)
                    boats_ts = _freshness_provider_time(boats_fresh)
                    if rh_ticker is None or (
                        boats_ts is not None
                        and (rh_ts is None or boats_ts > rh_ts)
                    ):
                        return boats_ticker, boats_fresh
                return rh_ticker, fresh

            return rh_ticker, fresh
        except Exception as e:
            logger.warning("[rh_adapter] get_best_bid_ask(%s) failed: %s", ticker, e)
            return None, fresh

    def get_ticker(self, product_id: str) -> tuple[Optional[NormalizedTicker], FreshnessMeta]:
        return self.get_best_bid_ask(product_id)

    def get_quote_price(self, product_id: str) -> Optional[float]:
        """Return a scalar price for ``product_id`` from Robinhood's own feed.

        Priority: mid (if both bid/ask present), else last_trade_price. Used by
        the AutoTrader v1 live monitor + close-now so exits compare against the
        same venue that would fill the order — no Massive/Polygon mismatch.
        """
        tkr, _ = self.get_best_bid_ask(product_id)
        if tkr is None:
            return None
        if tkr.freshness is not None and not is_fresh_enough(tkr.freshness):
            return None
        if tkr.mid and tkr.mid > 0:
            return float(tkr.mid)
        if tkr.last_price and tkr.last_price > 0:
            return float(tkr.last_price)
        return None

    def get_quote_prices_batch(self, product_ids: list[str]) -> dict[str, float]:
        """Batched RH quote lookup. One ``rh.stocks.get_quotes`` round-trip per call.

        Returns ``{TICKER_UPPER: price}`` only for tickers that produced a real
        price; missing / halted symbols are omitted. Falls back to per-symbol
        ``get_quote_price`` on any library exception.
        """
        # Task KK — split crypto product_ids out and route per-symbol via
        # the crypto-aware path. Equity tickers stay batched. The output
        # is keyed on the bare base (BTC), not the original BTC-USD pair —
        # callers normalize with _to_ticker on either side so this stays
        # consistent with the equity behavior.
        crypto_inputs = [p for p in product_ids if p and _is_crypto_product(p)]
        equity_inputs = [p for p in product_ids if p and not _is_crypto_product(p)]
        out: dict[str, float] = {}
        for cp in crypto_inputs:
            base = _to_ticker(cp)
            px = self.get_quote_price(cp)
            if px is not None:
                out[base] = float(px)
        if not equity_inputs:
            return out

        tickers = sorted({(_to_ticker(p) or "").upper() for p in equity_inputs if p})
        tickers = [t for t in tickers if t]
        if not tickers:
            return out
        try:
            import robin_stocks.robinhood as rh

            quotes = rh.stocks.get_quotes(tickers) or []
            for tkr, q in zip(tickers, quotes):
                if not q or not isinstance(q, dict):
                    continue
                fresh = _now_freshness(provider_time_utc=_latest_quote_timestamp(q))
                if not is_fresh_enough(fresh):
                    px = self.get_quote_price(tkr)
                    if px is not None:
                        out[tkr] = float(px)
                    continue
                bid = _sf(q.get("bid_price"))
                ask = _sf(q.get("ask_price"))
                last = _sf(q.get("last_trade_price"))
                ext = _sf(q.get("last_extended_hours_trade_price"))
                if bid and ask and bid > 0 and ask > 0:
                    px = (bid + ask) / 2.0
                elif last and last > 0:
                    px = float(last)
                elif ext and ext > 0:
                    px = float(ext)
                else:
                    continue
                out[tkr] = float(px)
            return out
        except Exception as e:
            logger.warning("[rh_adapter] get_quote_prices_batch failed, falling back: %s", e)
            for tkr in tickers:
                px = self.get_quote_price(tkr)
                if px is not None:
                    out[tkr] = float(px)
            return out

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

    def list_open_orders_truth(
        self,
        *,
        product_id: Optional[str] = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Strict open-order read used only for fail-closed terminalization."""
        try:
            import robin_stocks.robinhood as rh
            from ...broker_service import is_connected

            if not is_connected():
                return {"readable": False, "orders": None}
            account_snapshot = _rh_strict_account_snapshot(rh)
            raw_rows = _rh_strict_paginated_rows(rh, rh.urls.orders_url())
            raw_orders: list[dict[str, Any]] = []
            terminal = {"filled", "cancelled", "canceled", "rejected", "failed", "expired"}
            for row in raw_rows:
                state = str(row.get("state") or row.get("status") or "").strip().lower()
                account_url = str(row.get("account") or "").strip()
                if (
                    not state
                    or "cancel" not in row
                    or account_url not in account_snapshot["account_urls"]
                ):
                    return {"readable": False, "orders": None}
                if row.get("cancel") is not None:
                    raw_orders.append(row)
                elif state not in terminal:
                    return {"readable": False, "orders": None}
            orders: list[NormalizedOrder] = []
            for row in raw_orders:
                order = _normalize_rh_order_truth(row, rh=rh)
                if order is None:
                    return {"readable": False, "orders": None}
                orders.append(order)
            if product_id:
                ticker = _to_ticker(product_id)
                orders = [
                    order
                    for order in orders
                    if str(order.product_id or "").upper() == ticker
                ]
            return {"readable": True, "orders": orders[:limit]}
        except Exception as exc:
            logger.warning("[rh_adapter] strict open-order read failed: %s", exc)
            return {"readable": False, "orders": None}

    def get_order_truth(self, order_id: str) -> dict[str, Any]:
        """Strict order read; ``None`` is unknown, never certified absence."""
        if not str(order_id or "").strip():
            return {"readable": False, "found": False, "order": None}
        try:
            import robin_stocks.robinhood as rh
            from ...broker_service import is_connected

            if not is_connected():
                return {"readable": False, "found": False, "order": None}
            account_snapshot = _rh_strict_account_snapshot(rh)
            response = rh.helper.SESSION.get(rh.urls.orders_url(str(order_id)))
            response.raise_for_status()
            raw = response.json()
            if not isinstance(raw, dict) or not raw:
                return {"readable": False, "found": False, "order": None}
            if str(raw.get("account") or "").strip() not in account_snapshot["account_urls"]:
                return {"readable": False, "found": False, "order": None}

            order = _normalize_rh_order_truth(raw, rh=rh)
            if order is None:
                return {"readable": False, "found": False, "order": None}
            if str(order.order_id or "").strip() != str(order_id).strip():
                return {"readable": False, "found": False, "order": None}
            return {"readable": True, "found": True, "order": order}
        except Exception as exc:
            logger.warning("[rh_adapter] strict get_order(%s) failed: %s", order_id, exc)
            return {"readable": False, "found": False, "order": None}

    def get_position_quantity_truth(self, product_id: str) -> dict[str, Any]:
        """Direct, complete Robinhood position read; ambiguity is never flat."""
        symbol = _to_ticker(product_id)
        if not symbol:
            return {"readable": False, "quantity": None}
        try:
            import robin_stocks.robinhood as rh
            from ...broker_service import is_connected

            if not is_connected():
                return {"readable": False, "quantity": None}
            account_snapshot = _rh_strict_account_snapshot(rh)
            rows = _rh_strict_paginated_rows(
                rh,
                rh.urls.positions_url(),
                payload={"nonzero": "true"},
            )
            quantity = 0.0
            for row in rows:
                qty = _sf(row.get("quantity"))
                account_url = str(row.get("account") or "").strip()
                if (
                    qty is None
                    or not math.isfinite(qty)
                    or qty < 0.0
                    or account_url not in account_snapshot["account_urls"]
                ):
                    return {"readable": False, "quantity": None}
                row_symbol = str(
                    row.get("symbol") or row.get("chain_symbol") or ""
                ).strip().upper()
                if not row_symbol:
                    row_symbol = _rh_strict_instrument_symbol(
                        rh,
                        str(row.get("instrument") or ""),
                    )
                if row_symbol == symbol:
                    quantity += qty
            return {
                "readable": True,
                "quantity": quantity,
                "account_identity": account_snapshot["identity"],
            }
        except Exception as exc:
            logger.warning("[rh_adapter] strict position read failed: %s", exc)
            return {"readable": False, "quantity": None}

    def get_account_identity_truth(self) -> dict[str, Any]:
        """Strict, complete fingerprint of the authenticated RH account set."""
        try:
            import robin_stocks.robinhood as rh
            from ...broker_service import is_connected

            if not is_connected():
                return {"readable": False, "identity": None}
            snapshot = _rh_strict_account_snapshot(rh)
            return {"readable": True, "identity": snapshot["identity"]}
        except Exception as exc:
            logger.warning("[rh_adapter] strict account identity failed: %s", exc)
            return {"readable": False, "identity": None}

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
        market_hours_override: Optional[str] = None,
        extended_hours_override: Optional[bool] = None,
    ) -> dict[str, Any]:
        # Task KK — branch on -USD suffix. Equity orders strip the suffix
        # (`BTC-USD` would never get here) and call rh.orders.order().
        # Crypto orders pass the bare base ('BTC') to the rh crypto
        # endpoints, which trade 24/7 and skip the market-hours plumbing.
        is_crypto = _is_crypto_product(product_id)
        ticker = _to_ticker(product_id)
        qty = float(base_size)
        if not is_crypto:
            try:
                from ..tick_normalizer import normalize_quantity

                normalized_qty = float(normalize_quantity(qty, ticker))
            except Exception as exc:
                logger.warning(
                    "[rh_adapter] quantity normalization failed ticker=%s qty=%s err=%s",
                    ticker,
                    base_size,
                    exc,
                )
                normalized_qty = qty
            if normalized_qty <= 0.0:
                return {
                    "ok": False,
                    "error": f"invalid normalized equity quantity: {base_size!r}",
                    "client_order_id": client_order_id,
                }
            if abs(normalized_qty - qty) > 1e-12:
                logger.info(
                    "[rh_adapter] normalized equity quantity ticker=%s raw=%s normalized=%s",
                    ticker,
                    base_size,
                    normalized_qty,
                )
            qty = normalized_qty

        # f-portfolio-vs-pattern-breaker-separation — BUY-only gate. Portfolio
        # tier blocks every entry path when live + tripped/unavailable; passes
        # through when disabled, in shadow mode, or insufficient history.
        if side.lower() == "buy":
            _ok, _br_reason = _assert_portfolio_breaker_ok()
            if not _ok:
                return {
                    "ok": False,
                    "error": f"portfolio_breaker:{_br_reason}",
                    "client_order_id": client_order_id,
                }

        if idempotency_store.is_duplicate(client_order_id, venue=_VENUE):
            return {"ok": False, "error": "duplicate_client_order_id", "client_order_id": client_order_id}

        allowed, retry_after = rate_limiter.try_acquire(_VENUE)
        if not allowed:
            # P1.2 — record rate-limit exhaustion for the health breaker.
            try:
                venue_health.record_rate_limit_event(
                    venue=_VENUE, ticker=ticker, source="rh_place_market",
                )
            except Exception:
                pass
            return rate_limiter.rate_limited_response(
                _VENUE, retry_after, client_order_id=client_order_id
            )

        # P1.1 — SUBMITTING state before broker call (RH has no
        # native client_order_id, so we use whatever the caller supplied).
        try:
            order_state_machine.record_transition_standalone(
                to_state=order_state_machine.OrderState.SUBMITTING,
                venue=_VENUE,
                source="rh_place_market",
                client_order_id=client_order_id,
                raw_payload={
                    "ticker": ticker, "side": side.lower(), "qty": qty,
                    "is_crypto": is_crypto,
                },
            )
        except Exception:
            pass

        side_l = side.lower()

        if is_crypto:
            from ...broker_service import (
                place_crypto_buy_order, place_crypto_sell_order,
            )

            if side_l == "buy":
                result = place_crypto_buy_order(
                    ticker, qty, order_type="market",
                )
            elif side_l == "sell":
                result = place_crypto_sell_order(
                    ticker, qty, order_type="market",
                )
            else:
                return {"ok": False, "error": f"unknown side: {side}"}
        else:
            from ...broker_service import place_buy_order, place_sell_order

            if side_l == "buy":
                result = place_buy_order(
                    ticker,
                    qty,
                    order_type="market",
                    market_hours_override=market_hours_override,
                    extended_hours_override=extended_hours_override,
                )
            elif side_l == "sell":
                result = place_sell_order(
                    ticker,
                    qty,
                    order_type="market",
                    market_hours_override=market_hours_override,
                    extended_hours_override=extended_hours_override,
                )
            else:
                return {"ok": False, "error": f"unknown side: {side}"}

        if result.get("ok") and client_order_id:
            idempotency_store.remember(
                client_order_id,
                venue=_VENUE,
                symbol=ticker,
                side=side_l,
                qty=qty,
                broker_order_id=result.get("order_id") or None,
                status="submitted",
            )

        if result.get("ok"):
            oid = result.get("order_id", "") or None
            try:
                order_state_machine.record_transition_standalone(
                    to_state=order_state_machine.OrderState.ACK,
                    venue=_VENUE,
                    source="rh_place_market",
                    order_id=oid,
                    client_order_id=client_order_id,
                    broker_status="accepted",
                    raw_payload={"ticker": ticker, "order_id": oid},
                )
            except Exception:
                pass
            return {
                "ok": True,
                "order_id": result.get("order_id", ""),
                "client_order_id": client_order_id,
                "raw": result.get("raw", {}),
            }
        # Non-ok from broker_service → REJECTED.
        try:
            order_state_machine.record_transition_standalone(
                to_state=order_state_machine.OrderState.REJECTED,
                venue=_VENUE,
                source="rh_place_market",
                client_order_id=client_order_id,
                broker_status="rejected",
                raw_payload={"ticker": ticker, "error": str(result.get("error") or "")},
            )
        except Exception:
            pass
        return result

    def place_limit_order_gtc(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        limit_price: str,
        client_order_id: Optional[str] = None,
        market_hours_override: Optional[str] = None,
        extended_hours_override: Optional[bool] = None,
        extended_hours: bool = False,
        time_in_force: Optional[str] = None,
    ) -> dict[str, Any]:
        ticker = _to_ticker(product_id)
        qty = float(base_size)
        price = float(limit_price)

        # Pre-/after-market entry (Ross's gap-and-go): route through RH's all-day
        # session so the order isn't rejected as out-of-hours. Explicit overrides from
        # the caller still win.
        if extended_hours:
            if market_hours_override is None:
                market_hours_override = "all_day_hours"
            if extended_hours_override is None:
                extended_hours_override = True

        # f-portfolio-vs-pattern-breaker-separation — BUY-only gate. Portfolio
        # tier blocks every entry path when live + tripped; pass-through when
        # disabled, in shadow mode, or insufficient history (fail-OPEN).
        if side.lower() == "buy":
            _ok, _br_reason = _assert_portfolio_breaker_ok()
            if not _ok:
                return {
                    "ok": False,
                    "error": f"portfolio_breaker:{_br_reason}",
                    "client_order_id": client_order_id,
                }

        if idempotency_store.is_duplicate(client_order_id, venue=_VENUE):
            return {"ok": False, "error": "duplicate_client_order_id", "client_order_id": client_order_id}

        allowed, retry_after = rate_limiter.try_acquire(_VENUE)
        if not allowed:
            # P1.2 — record rate-limit exhaustion for the health breaker.
            try:
                venue_health.record_rate_limit_event(
                    venue=_VENUE, ticker=ticker, source="rh_place_limit",
                )
            except Exception:
                pass
            return rate_limiter.rate_limited_response(
                _VENUE, retry_after, client_order_id=client_order_id
            )

        # P1.1 — SUBMITTING state before broker call.
        try:
            order_state_machine.record_transition_standalone(
                to_state=order_state_machine.OrderState.SUBMITTING,
                venue=_VENUE,
                source="rh_place_limit",
                client_order_id=client_order_id,
                raw_payload={"ticker": ticker, "side": side.lower(), "qty": qty, "limit_price": price},
            )
        except Exception:
            pass

        from ...broker_service import place_buy_order, place_sell_order

        def _place(mh_override: Optional[str]) -> dict[str, Any]:
            if side.lower() == "buy":
                return place_buy_order(
                    ticker,
                    qty,
                    order_type="limit",
                    limit_price=price,
                    market_hours_override=mh_override,
                    extended_hours_override=extended_hours_override,
                    time_in_force=time_in_force,
                )
            return place_sell_order(
                ticker,
                qty,
                order_type="limit",
                limit_price=price,
                market_hours_override=mh_override,
                extended_hours_override=extended_hours_override,
            )

        side_l = side.lower()
        if side_l not in ("buy", "sell"):
            return {"ok": False, "error": f"unknown side: {side}"}
        result = _place(market_hours_override)
        # 2026-06-12 first premarket entries (OTLK/CUPR): all_day_hours is the
        # 24h-MARKET session — most small caps aren't enrolled and RH rejects
        # with "instrument is untradable for 24 hour trading". Those names ARE
        # tradable in the regular EXTENDED session (7:00-9:30 / 16:00-20:00 ET)
        # — retry once there. Applies to exits too (a held position must always
        # be closable premarket).
        if (
            not result.get("ok")
            and market_hours_override == "all_day_hours"
            and "untradable for 24 hour trading" in str(result.get("raw") or result.get("error") or "")
        ):
            logger.info(
                "[robinhood_spot] %s %s not 24h-enrolled; retrying in extended_hours session",
                side_l, ticker,
            )
            result = _place("extended_hours")

        if result.get("ok") and client_order_id:
            idempotency_store.remember(
                client_order_id,
                venue=_VENUE,
                symbol=ticker,
                side=side_l,
                qty=qty,
                broker_order_id=result.get("order_id") or None,
                status="submitted",
            )

        if result.get("ok"):
            oid = result.get("order_id", "") or None
            try:
                order_state_machine.record_transition_standalone(
                    to_state=order_state_machine.OrderState.ACK,
                    venue=_VENUE,
                    source="rh_place_limit",
                    order_id=oid,
                    client_order_id=client_order_id,
                    broker_status="accepted",
                    raw_payload={"ticker": ticker, "order_id": oid},
                )
            except Exception:
                pass
            return {
                "ok": True,
                "order_id": result.get("order_id", ""),
                "client_order_id": client_order_id,
                "raw": result.get("raw", {}),
            }
        try:
            order_state_machine.record_transition_standalone(
                to_state=order_state_machine.OrderState.REJECTED,
                venue=_VENUE,
                source="rh_place_limit",
                client_order_id=client_order_id,
                broker_status="rejected",
                raw_payload={"ticker": ticker, "error": str(result.get("error") or "")},
            )
        except Exception:
            pass
        return result

    def place_stop_loss_sell_order(
        self,
        *,
        product_id: str,
        base_size: str,
        trigger_price: str,
        client_order_id: Optional[str] = None,
        market_hours_override: Optional[str] = None,
        extended_hours_override: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Place a server-side STOP-LOSS SELL order on Robinhood equities.

        The order rests at the broker and triggers (becomes a market
        order) when the last trade prints at or below ``trigger_price``.
        This is the protective primitive used by the Phase G.2 bracket
        writer to repair ``missing_stop`` reconciliation findings.

        Symbol convention: plain stock ticker (``AAPL``); a trailing
        ``-USD`` suffix is stripped for parity with ``place_limit_order_gtc``.

        Returns the same envelope shape as the other place_* methods so
        callers can dispatch uniformly:
            {"ok": True,  "order_id": "...", "client_order_id": "...", "raw": {...}}
            {"ok": False, "error": "...", "client_order_id": "..."}
        """
        ticker = _to_ticker(product_id)
        qty = float(base_size)
        trigger = float(trigger_price)

        if idempotency_store.is_duplicate(client_order_id, venue=_VENUE):
            return {
                "ok": False,
                "error": "duplicate_client_order_id",
                "client_order_id": client_order_id,
            }

        allowed, retry_after = rate_limiter.try_acquire(_VENUE)
        if not allowed:
            # P1.2 - record rate-limit exhaustion for the health breaker.
            try:
                venue_health.record_rate_limit_event(
                    venue=_VENUE, ticker=ticker, source="rh_place_stop_loss",
                )
            except Exception:
                pass
            return rate_limiter.rate_limited_response(
                _VENUE, retry_after, client_order_id=client_order_id
            )

        # P1.1 - SUBMITTING state before broker call.
        try:
            order_state_machine.record_transition_standalone(
                to_state=order_state_machine.OrderState.SUBMITTING,
                venue=_VENUE,
                source="rh_place_stop_loss",
                client_order_id=client_order_id,
                raw_payload={
                    "ticker": ticker,
                    "side": "sell",
                    "qty": qty,
                    "trigger_price": trigger,
                    "kind": "stop_loss_market",
                },
            )
        except Exception:
            pass

        from ...broker_service import place_sell_stop_loss_order

        result = place_sell_stop_loss_order(
            ticker,
            qty,
            trigger_price=trigger,
            market_hours_override=market_hours_override,
            extended_hours_override=extended_hours_override,
        )

        if result.get("ok") and client_order_id:
            idempotency_store.remember(
                client_order_id,
                venue=_VENUE,
                symbol=ticker,
                side="sell",
                qty=qty,
                broker_order_id=result.get("order_id") or None,
                status="submitted",
            )

        if result.get("ok"):
            oid = result.get("order_id", "") or None
            try:
                order_state_machine.record_transition_standalone(
                    to_state=order_state_machine.OrderState.ACK,
                    venue=_VENUE,
                    source="rh_place_stop_loss",
                    order_id=oid,
                    client_order_id=client_order_id,
                    broker_status="accepted",
                    raw_payload={
                        "ticker": ticker,
                        "order_id": oid,
                        "trigger_price": trigger,
                    },
                )
            except Exception:
                pass
            return {
                "ok": True,
                "order_id": result.get("order_id", ""),
                "client_order_id": client_order_id,
                "raw": result.get("raw", {}),
            }
        try:
            order_state_machine.record_transition_standalone(
                to_state=order_state_machine.OrderState.REJECTED,
                venue=_VENUE,
                source="rh_place_stop_loss",
                client_order_id=client_order_id,
                broker_status="rejected",
                raw_payload={
                    "ticker": ticker,
                    "trigger_price": trigger,
                    "error": str(result.get("error") or ""),
                },
            )
        except Exception:
            pass
        return result

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        allowed, retry_after = rate_limiter.try_acquire(_VENUE)
        if not allowed:
            # P1.2 — record rate-limit exhaustion for the health breaker.
            try:
                venue_health.record_rate_limit_event(
                    venue=_VENUE, ticker=None, source="rh_cancel",
                )
            except Exception:
                pass
            return rate_limiter.rate_limited_response(_VENUE, retry_after)
        try:
            import robin_stocks.robinhood as rh

            result = rh.orders.cancel_stock_order(order_id)
            # P1.1 — record CANCELLED transition for the cancel request.
            try:
                order_state_machine.record_transition_standalone(
                    to_state=order_state_machine.OrderState.CANCELLED,
                    venue=_VENUE,
                    source="rh_cancel",
                    order_id=order_id,
                    broker_status="cancelled",
                    raw_payload={"cancel_response": result or {}},
                )
            except Exception:
                pass
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
