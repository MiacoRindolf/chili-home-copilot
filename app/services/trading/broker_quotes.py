"""Broker-aware quote selection for broker-held live positions."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ...models.trading import Trade

logger = logging.getLogger(__name__)

_LIVE_BROKER_SOURCES = frozenset({"robinhood", "coinbase"})


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def open_broker_trade_for_ticker(
    db: Session,
    ticker: str,
    *,
    user_id: int | None,
) -> Trade | None:
    symbol = (ticker or "").strip().upper()
    if not symbol:
        return None
    q = db.query(Trade).filter(
        func.upper(Trade.ticker) == symbol,
        Trade.status == "open",
        Trade.broker_source.isnot(None),
    )
    if user_id is not None:
        q = q.filter(Trade.user_id == user_id)
    else:
        q = q.filter(Trade.user_id.isnot(None))
    rows = q.order_by(Trade.entry_date.desc(), Trade.id.desc()).limit(10).all()
    for trade in rows:
        broker_source = (trade.broker_source or "").strip().lower()
        if broker_source in _LIVE_BROKER_SOURCES:
            return trade
    return None


def broker_quote_for_trade(
    trade: Trade,
    *,
    purpose: str = "display",
) -> dict[str, Any]:
    broker_source = (trade.broker_source or "").strip().lower()
    ticker = (trade.ticker or "").strip().upper()
    base = {
        "ticker": ticker,
        "price": None,
        "change": None,
        "change_pct": None,
        "source": f"{broker_source or 'broker'}_unavailable",
        "broker_source": broker_source or None,
        "broker_held": True,
    }
    if not ticker:
        return base
    try:
        from .autopilot_scope import is_option_trade

        if is_option_trade(trade):
            return _option_quote_for_trade(trade, base, purpose=purpose)
    except Exception:
        logger.debug(
            "[broker_quotes] option quote detection failed ticker=%s",
            ticker,
            exc_info=True,
        )
        return {**base, "source": "robinhood_options_unavailable"}
    if not broker_source:
        return base
    try:
        from .venue.factory import get_adapter

        adapter = get_adapter(broker_source)
        if adapter is None:
            return base
        is_enabled = getattr(adapter, "is_enabled", None)
        if callable(is_enabled) and not is_enabled():
            return base

        tick = None
        fresh = None
        get_ticker = getattr(adapter, "get_ticker", None)
        if callable(get_ticker):
            raw = get_ticker(ticker)
            if isinstance(raw, tuple) and len(raw) == 2:
                tick, fresh = raw
        if tick is None:
            get_bbo = getattr(adapter, "get_best_bid_ask", None)
            if callable(get_bbo):
                raw = get_bbo(ticker)
                if isinstance(raw, tuple) and len(raw) == 2:
                    tick, fresh = raw
        if tick is None:
            return base

        fresh = fresh or getattr(tick, "freshness", None)
        age_seconds = None
        max_age_seconds = _safe_float(getattr(fresh, "max_age_seconds", None))
        age_fn = getattr(fresh, "age_seconds", None)
        if callable(age_fn):
            try:
                age_seconds = float(age_fn())
            except (TypeError, ValueError):
                age_seconds = None
        is_stale = bool(
            age_seconds is not None
            and max_age_seconds is not None
            and age_seconds > max_age_seconds
        )

        raw = getattr(tick, "raw", None) or {}
        bid = _safe_float(getattr(tick, "bid", None))
        ask = _safe_float(getattr(tick, "ask", None))
        mid = _safe_float(getattr(tick, "mid", None))
        last = _safe_float(getattr(tick, "last_price", None))
        spread_bps = _safe_float(getattr(tick, "spread_bps", None))
        volume = (
            _safe_float(getattr(tick, "base_volume_24h", None))
            or _safe_float(raw.get("volume"))
            or _safe_float(raw.get("volume_24h"))
        )
        side = (trade.direction or "long").strip().lower()
        executable = ask if side == "short" else bid
        if purpose == "exit":
            candidates = (executable, mid, last, bid, ask)
        else:
            candidates = (last, mid, executable, bid, ask)
        price = next((px for px in candidates if px is not None), None)
        if price is None:
            return base

        prev_close = (
            _safe_float(raw.get("previous_close"))
            or _safe_float(raw.get("adjusted_previous_close"))
            or _safe_float(raw.get("regular_market_previous_close"))
        )
        day_high = _safe_float(raw.get("day_high") or raw.get("high"))
        day_low = _safe_float(raw.get("day_low") or raw.get("low"))
        provider_time = getattr(fresh, "provider_time_utc", None) if fresh is not None else None
        retrieved = getattr(fresh, "retrieved_at_utc", None) if fresh is not None else None
        quote_dt = provider_time or retrieved
        if quote_dt is not None and getattr(quote_dt, "tzinfo", None) is not None:
            quote_dt = quote_dt.astimezone(timezone.utc)

        raw_source = str(raw.get("source") or "").lower()
        if raw_source == "tradingview_boats":
            try:
                from .market_data import fetch_quote

                anchor = fetch_quote(ticker, allow_provider_fallback=True) or {}
                anchor_price = _safe_float(anchor.get("price") or anchor.get("last_price"))
                if anchor_price is not None and 0.5 <= (anchor_price / float(price)) <= 1.5:
                    prev_close = anchor_price
            except Exception:
                pass
        change = None
        change_pct = None
        if prev_close:
            change = round(float(price) - float(prev_close), 6)
            change_pct = round((change / float(prev_close)) * 100.0, 4)
        source = (
            f"{broker_source}_legend_blue_ocean"
            if raw_source == "tradingview_boats"
            else broker_source
        )
        if is_stale:
            source = f"{source}_stale"
        return {
            **base,
            "price": round(float(price), 6),
            "last_price": round(float(last), 6) if last is not None else None,
            "bid": round(float(bid), 6) if bid is not None else None,
            "ask": round(float(ask), 6) if ask is not None else None,
            "mid": round(float(mid), 6) if mid is not None else None,
            "executable_price": round(float(executable), 6) if executable is not None else None,
            "previous_close": round(float(prev_close), 6) if prev_close is not None else None,
            "day_high": round(float(day_high), 6) if day_high is not None else None,
            "day_low": round(float(day_low), 6) if day_low is not None else None,
            "change": change,
            "change_pct": change_pct,
            "quote_ts": quote_dt.isoformat() if quote_dt is not None else None,
            "spread_bps": round(float(spread_bps), 6) if spread_bps is not None else None,
            "volume": round(float(volume), 6) if volume is not None else None,
            "source": source,
            "broker_source": broker_source,
            "broker_held": True,
            "stale": is_stale,
            "age_seconds": round(float(age_seconds), 3) if age_seconds is not None else None,
            "max_age_seconds": max_age_seconds,
        }
    except Exception:
        logger.debug(
            "[broker_quotes] broker quote failed broker=%s ticker=%s",
            broker_source,
            ticker,
            exc_info=True,
        )
        return base


def _option_quote_for_trade(
    trade: Trade,
    base: dict[str, Any],
    *,
    purpose: str,
) -> dict[str, Any]:
    """Option-premium quote for an option Trade row; never use underlying spot."""
    unavailable = {**base, "source": "robinhood_options_unavailable"}
    try:
        from .options.exit_monitor import _opt_meta
        from .venue.robinhood_options import RobinhoodOptionsAdapter

        meta = _opt_meta(trade)
        expiration = str(meta.get("expiration") or "").strip()
        strike = _safe_float(meta.get("strike"))
        option_type = str(meta.get("option_type") or "").strip().lower()
        if not (expiration and strike is not None and option_type in ("call", "put")):
            return unavailable

        underlying = str(meta.get("underlying") or getattr(trade, "ticker", "") or "").strip().upper()
        if not underlying:
            return unavailable

        adapter = RobinhoodOptionsAdapter()
        is_enabled = getattr(adapter, "is_enabled", None)
        if callable(is_enabled) and not is_enabled():
            return unavailable

        option_id = str(meta.get("option_id") or meta.get("contract_id") or "").strip()
        contract: dict[str, Any] | None = None
        if not option_id:
            contract = adapter.find_contract(underlying, expiration, float(strike), option_type)
            if not contract:
                return unavailable
            option_id = str(contract.get("id") or "").strip()
        if not option_id:
            return unavailable

        quote = adapter.get_quote(option_id)
        if not quote:
            return unavailable

        bid = _safe_float(quote.get("bid_price"))
        ask = _safe_float(quote.get("ask_price"))
        mid = ((bid + ask) / 2.0) if bid is not None and ask is not None else None
        mark = _safe_float(quote.get("mark_price"))
        adjusted_mark = _safe_float(quote.get("adjusted_mark_price"))
        last = _safe_float(quote.get("last_trade_price") or quote.get("last_price"))
        side = (getattr(trade, "direction", None) or "long").strip().lower()
        executable = ask if side == "short" else bid
        if purpose == "exit":
            candidates = (executable, mark, adjusted_mark, mid, last, bid, ask)
        else:
            candidates = (mark, adjusted_mark, last, mid, executable, bid, ask)
        price = next((px for px in candidates if px is not None), None)
        if price is None:
            return unavailable

        spread_bps = None
        if bid is not None and ask is not None and mid and mid > 0:
            spread_bps = ((ask - bid) / mid) * 10_000.0

        quote_dt = (
            quote.get("updated_at")
            or quote.get("last_trade_at")
            or quote.get("previous_close_date")
        )
        return {
            **base,
            "ticker": underlying,
            "price": round(float(price), 6),
            "last_price": round(float(last), 6) if last is not None else None,
            "bid": round(float(bid), 6) if bid is not None else None,
            "ask": round(float(ask), 6) if ask is not None else None,
            "mid": round(float(mid), 6) if mid is not None else None,
            "mark_price": round(float(mark), 6) if mark is not None else None,
            "adjusted_mark_price": (
                round(float(adjusted_mark), 6) if adjusted_mark is not None else None
            ),
            "executable_price": round(float(executable), 6) if executable is not None else None,
            "change": None,
            "change_pct": None,
            "quote_ts": str(quote_dt) if quote_dt else None,
            "spread_bps": round(float(spread_bps), 6) if spread_bps is not None else None,
            "volume": _safe_float(quote.get("volume")),
            "source": "robinhood_options",
            "broker_source": (getattr(trade, "broker_source", None) or "robinhood"),
            "broker_held": True,
            "option_id": option_id,
            "option_underlying": underlying,
            "option_expiration": expiration,
            "option_strike": round(float(strike), 6),
            "option_type": option_type,
        }
    except Exception:
        logger.debug(
            "[broker_quotes] option quote failed ticker=%s",
            getattr(trade, "ticker", None),
            exc_info=True,
        )
        return unavailable


def broker_quote_for_user_ticker(
    db: Session,
    *,
    user_id: int | None,
    ticker: str,
    purpose: str = "display",
) -> dict[str, Any] | None:
    trade = open_broker_trade_for_ticker(db, ticker, user_id=user_id)
    if trade is None:
        return None
    return broker_quote_for_trade(trade, purpose=purpose)


def broker_quote_for_source(
    ticker: str,
    *,
    broker_source: str | None,
    direction: str | None = "long",
    purpose: str = "display",
) -> dict[str, Any]:
    """Broker quote for code paths that only have source metadata, not a Trade row."""
    trade = SimpleNamespace(
        ticker=(ticker or "").strip().upper(),
        broker_source=(broker_source or "").strip().lower(),
        direction=(direction or "long").strip().lower(),
    )
    return broker_quote_for_trade(trade, purpose=purpose)


def broker_recent_extrema_for_source(
    ticker: str,
    *,
    broker_source: str | None,
    lookback_minutes: int | None = None,
    interval: str = "5m",
) -> dict[str, Any] | None:
    """Recent broker-session high/low for gap-aware stop/target detection."""
    source = (broker_source or "").strip().lower()
    symbol = (ticker or "").strip().upper()
    if not source or not symbol:
        return None
    minutes = int(lookback_minutes or 0)
    if minutes <= 0:
        try:
            from ...config import settings

            minutes = int(
                getattr(
                    settings,
                    "chili_broker_position_price_monitor_bar_lookback_minutes",
                    720,
                )
                or 720
            )
        except Exception:
            minutes = 720
    minutes = max(5, min(minutes, 1440))

    bars: list[dict[str, Any]] = []
    range_source = source
    if source == "robinhood":
        try:
            from .tradingview_blue_ocean import fetch_boats_ohlcv

            needed = max(20, min(500, int((minutes / 5) + 24)))
            bars = fetch_boats_ohlcv(symbol, interval=interval, bars=needed)
            range_source = "robinhood_legend_blue_ocean"
        except Exception:
            logger.debug(
                "[broker_quotes] recent extrema failed broker=%s ticker=%s",
                source,
                symbol,
                exc_info=True,
            )
            return None
    else:
        return None

    if not bars:
        return None
    cutoff = datetime.now(timezone.utc).timestamp() - minutes * 60
    recent = [b for b in bars if _safe_float(b.get("time")) and float(b["time"]) >= cutoff]
    if not recent:
        return None

    high_bar = max(recent, key=lambda b: float(b.get("high") or 0.0))
    low_bar = min(recent, key=lambda b: float(b.get("low") or float("inf")))
    last_bar = max(recent, key=lambda b: float(b.get("time") or 0.0))
    high = _safe_float(high_bar.get("high"))
    low = _safe_float(low_bar.get("low"))
    last = _safe_float(last_bar.get("close"))
    if high is None and low is None:
        return None
    return {
        "ticker": symbol,
        "source": range_source,
        "lookback_minutes": minutes,
        "bar_count": len(recent),
        "high": high,
        "low": low,
        "last": last,
        "high_ts": datetime.fromtimestamp(int(high_bar["time"]), timezone.utc).isoformat()
        if high is not None else None,
        "low_ts": datetime.fromtimestamp(int(low_bar["time"]), timezone.utc).isoformat()
        if low is not None else None,
        "last_ts": datetime.fromtimestamp(int(last_bar["time"]), timezone.utc).isoformat()
        if last is not None else None,
    }
