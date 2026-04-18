"""Shared Robinhood equity exit handling for AutoTrader and desk close-now."""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import AutoTraderRun, Trade

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc
_OFF_HOURS_MAX_SPREAD_BPS = 400.0
_OFF_HOURS_MAX_QUOTE_AGE_SECONDS = 45.0
_ACTIVE_PENDING_EXIT_STATES = {
    "queued",
    "pending",
    "confirmed",
    "unconfirmed",
    "partially_filled",
    "open",
    "working",
    "submitted",
}
_TERMINAL_PENDING_EXIT_STATES = {"filled", "cancelled", "canceled", "rejected", "failed", "expired"}


def _coerce_utc(now_utc: datetime | None = None) -> datetime:
    now = now_utc or datetime.now(_UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=_UTC)
    return now.astimezone(_UTC)


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=_UTC)
        return value.astimezone(_UTC)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=_UTC)
        return dt.astimezone(_UTC)
    return None


def _extract_market_mic(raw_product: dict[str, Any]) -> str | None:
    market_url = str(raw_product.get("market") or "").strip()
    if not market_url:
        return None
    parts = [p for p in market_url.rstrip("/").split("/") if p]
    if not parts:
        return None
    return parts[-1].upper()


def _round_limit_price(price: float) -> float:
    if price <= 0:
        return 0.0
    if price < 1.0:
        return round(price, 4)
    return round(price, 2)


def _is_whole_share_quantity(quantity: float) -> bool:
    return abs(float(quantity) - round(float(quantity))) < 1e-9


def _opened_today_et(entry_date: datetime | None) -> bool:
    if entry_date is None:
        return False
    now_et = datetime.now(_ET)
    if entry_date.tzinfo is None:
        entry_et = entry_date.replace(tzinfo=_UTC).astimezone(_ET)
    else:
        entry_et = entry_date.astimezone(_ET)
    return entry_et.date() == now_et.date()


def has_active_pending_exit(trade: Trade) -> bool:
    return (trade.pending_exit_order_id or "") != "" and (
        (trade.pending_exit_status or "").strip().lower() in _ACTIVE_PENDING_EXIT_STATES
    )


def clear_pending_exit_fields(trade: Trade) -> None:
    trade.pending_exit_order_id = None
    trade.pending_exit_status = None
    trade.pending_exit_requested_at = None
    trade.pending_exit_reason = None
    trade.pending_exit_limit_price = None


def _load_market_hours(raw_product: dict[str, Any], *, now_utc: datetime) -> dict[str, Any] | None:
    mic = _extract_market_mic(raw_product)
    if not mic:
        return None
    try:
        import robin_stocks.robinhood as rh

        return rh.markets.get_market_hours(mic, now_utc.astimezone(_ET).date().isoformat())
    except Exception:
        logger.debug("[rh_exit] market hours lookup failed for %s", mic, exc_info=True)
        return None


def _static_session_window(*, overnight_eligible: bool, now_utc: datetime) -> dict[str, Any]:
    now_et = now_utc.astimezone(_ET)
    wd = now_et.weekday()
    t = now_et.time()
    session = "closed"
    label = "Market closed"
    market_hours = None

    if wd == 5:
        session = "closed_weekend"
        label = "Weekend closed"
    elif wd == 6:
        if t >= time(20, 0) and overnight_eligible:
            session = "overnight_24h"
            label = "24 Hour Market"
            market_hours = "all_day_hours"
        else:
            session = "closed_weekend"
            label = "Weekend closed"
    else:
        if t < time(7, 0):
            if overnight_eligible:
                session = "overnight_24h"
                label = "24 Hour Market"
                market_hours = "all_day_hours"
            else:
                session = "closed"
                label = "Waiting for extended hours"
        elif t < time(9, 30):
            session = "extended_hours"
            label = "Extended hours"
            market_hours = "extended_hours"
        elif t < time(16, 0):
            session = "regular_hours"
            label = "Regular session"
            market_hours = "regular_hours"
        elif t < time(20, 0):
            session = "extended_hours"
            label = "Extended hours"
            market_hours = "extended_hours"
        elif overnight_eligible and wd < 4:
            session = "overnight_24h"
            label = "24 Hour Market"
            market_hours = "all_day_hours"
        else:
            session = "closed_weekend" if wd == 4 else "closed"
            label = "Weekend closed" if wd == 4 else "Market closed"

    next_at = _static_next_eligible_start(now_utc=now_utc, overnight_eligible=overnight_eligible)
    return {
        "session": session,
        "session_label": label,
        "market_hours": market_hours,
        "next_eligible_session_at": next_at,
    }


def _static_next_eligible_start(*, now_utc: datetime, overnight_eligible: bool) -> datetime:
    now_et = now_utc.astimezone(_ET)
    wd = now_et.weekday()
    t = now_et.time()

    def _dt_for(day_offset: int, hh: int, mm: int) -> datetime:
        base = now_et.date() + timedelta(days=day_offset)
        return datetime.combine(base, time(hh, mm), tzinfo=_ET).astimezone(_UTC)

    if wd == 5:
        return _dt_for(1, 20, 0) if overnight_eligible else _dt_for(2, 7, 0)
    if wd == 6:
        if t < time(20, 0):
            return _dt_for(0, 20, 0) if overnight_eligible else _dt_for(1, 7, 0)
        return _dt_for(0, 20, 0) if overnight_eligible else _dt_for(1, 7, 0)
    if t < time(7, 0):
        return now_utc if overnight_eligible else _dt_for(0, 7, 0)
    if t < time(9, 30):
        return now_utc
    if t < time(16, 0):
        return now_utc
    if t < time(20, 0):
        return now_utc
    if overnight_eligible and wd < 4:
        return now_utc
    if wd == 4:
        return _dt_for(2, 20, 0) if overnight_eligible else _dt_for(3, 7, 0)
    return _dt_for(1, 20, 0) if overnight_eligible else _dt_for(1, 7, 0)


def describe_robinhood_equity_execution_window(
    ticker: str,
    *,
    adapter: Any | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    from .venue.robinhood_spot import RobinhoodSpotAdapter

    now = _coerce_utc(now_utc)
    adapter = adapter or RobinhoodSpotAdapter()
    product, _fresh = adapter.get_product(ticker)
    raw_product = dict(getattr(product, "raw", {}) or {})
    overnight_eligible = str(raw_product.get("all_day_tradability") or "").lower() == "tradable"

    session = _static_session_window(overnight_eligible=overnight_eligible, now_utc=now)
    hours = _load_market_hours(raw_product, now_utc=now)
    if hours:
        reg_open = _parse_dt(hours.get("opens_at"))
        reg_close = _parse_dt(hours.get("closes_at"))
        ext_open = _parse_dt(hours.get("extended_opens_at"))
        ext_close = _parse_dt(hours.get("extended_closes_at"))
        all_open = _parse_dt(hours.get("all_day_opens_at"))
        all_close = _parse_dt(hours.get("all_day_closes_at"))
        next_today = [dt for dt in (all_open if overnight_eligible else None, ext_open, reg_open) if dt and dt > now]
        if reg_open and reg_close and reg_open <= now < reg_close:
            session.update({
                "session": "regular_hours",
                "session_label": "Regular session",
                "market_hours": "regular_hours",
                "next_eligible_session_at": now,
            })
        elif ext_open and ext_close and ext_open <= now < ext_close:
            session.update({
                "session": "extended_hours",
                "session_label": "Extended hours",
                "market_hours": "extended_hours",
                "next_eligible_session_at": now,
            })
        elif overnight_eligible and all_open and all_close and all_open <= now < all_close:
            session.update({
                "session": "overnight_24h",
                "session_label": "24 Hour Market",
                "market_hours": "all_day_hours",
                "next_eligible_session_at": now,
            })
        elif next_today:
            session["next_eligible_session_at"] = min(next_today)
            if session["session"] == "closed" and hours.get("is_open") is False:
                session["session_label"] = "Exchange closed"
        elif hours.get("is_open") is False and not any((reg_open, ext_open, all_open)):
            session["session"] = "closed_weekend"
            session["session_label"] = "Exchange closed"

    allow_off_hours = bool(getattr(settings, "chili_autotrader_allow_extended_hours", False))
    can_submit_now = session["session"] == "regular_hours" or (
        allow_off_hours and session["session"] in ("extended_hours", "overnight_24h")
    )
    execution_reason = session["session_label"]
    if session["session"] == "overnight_24h" and not overnight_eligible:
        can_submit_now = False
        execution_reason = "Symbol not eligible for 24 Hour Market"
    if not allow_off_hours and session["session"] != "regular_hours":
        execution_reason = "Off-hours exits disabled"

    return {
        **session,
        "ticker": (ticker or "").upper(),
        "overnight_eligible": overnight_eligible,
        "can_submit_now": bool(can_submit_now),
        "execution_reason": execution_reason,
    }


def _mark_deferred_exit(
    db: Session,
    trade: Trade,
    *,
    exit_reason: str,
    execution_reason: str,
    now_utc: datetime,
) -> None:
    trade.pending_exit_order_id = None
    trade.pending_exit_status = "deferred"
    trade.pending_exit_requested_at = now_utc.replace(tzinfo=None)
    trade.pending_exit_reason = exit_reason
    trade.pending_exit_limit_price = None
    db.add(trade)
    db.commit()


def _mark_pending_exit_order(
    db: Session,
    trade: Trade,
    *,
    order_id: str,
    status: str,
    exit_reason: str,
    requested_at: datetime,
    limit_price: float | None,
) -> None:
    trade.pending_exit_order_id = str(order_id or "") or None
    trade.pending_exit_status = status or "submitted"
    trade.pending_exit_requested_at = requested_at.replace(tzinfo=None)
    trade.pending_exit_reason = exit_reason
    trade.pending_exit_limit_price = float(limit_price) if limit_price is not None else None
    trade.last_broker_sync = requested_at.replace(tzinfo=None)
    db.add(trade)
    db.commit()


def _finalize_filled_exit(
    db: Session,
    trade: Trade,
    *,
    raw_order: dict[str, Any],
    exit_reason: str,
    fallback_price: float | None,
    filled_at: datetime,
) -> float:
    qty = float(trade.quantity or 0.0)
    exit_px = _safe_float(raw_order.get("average_price")) or _safe_float(raw_order.get("price")) or fallback_price or float(trade.entry_price)
    entry = float(trade.entry_price or 0.0)
    pnl = (float(exit_px) - entry) * qty
    trade.status = "closed"
    trade.exit_price = float(exit_px)
    trade.exit_date = filled_at.replace(tzinfo=None)
    trade.pnl = round(pnl, 4)
    trade.exit_reason = exit_reason
    trade.pending_exit_order_id = None
    trade.pending_exit_status = None
    trade.pending_exit_requested_at = None
    trade.pending_exit_reason = None
    trade.pending_exit_limit_price = None
    trade.last_broker_sync = filled_at.replace(tzinfo=None)
    trade.broker_status = (raw_order.get("state") or raw_order.get("status") or trade.broker_status or "filled")
    db.add(trade)
    db.commit()
    try:
        from .auto_trader_position_overrides import clear_position_overrides

        clear_position_overrides(db, "trade", int(trade.id))
    except Exception:
        logger.debug("[rh_exit] clear_position_overrides failed for trade=%s", trade.id, exc_info=True)
    return round(pnl, 4)


def _record_autotrader_run(
    db: Session,
    trade: Trade,
    *,
    decision: str,
    reason: str,
    snapshot: dict[str, Any] | None = None,
) -> None:
    row = AutoTraderRun(
        user_id=trade.user_id,
        breakout_alert_id=trade.related_alert_id,
        scan_pattern_id=trade.scan_pattern_id,
        ticker=(trade.ticker or "").upper(),
        decision=decision,
        reason=reason,
        trade_id=int(trade.id),
        rule_snapshot=dict(snapshot or {}),
    )
    db.add(row)
    db.commit()


def _audit_snapshot(
    trade: Trade,
    *,
    exit_reason: str,
    monitor_exit_meta: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    opened_today = _opened_today_et(trade.entry_date)
    out: dict[str, Any] = {
        "exit_reason": exit_reason,
        "opened_today_et": opened_today,
        "would_be_day_trade": opened_today and (trade.direction or "long") == "long",
    }
    if monitor_exit_meta is not None:
        out["monitor_decision"] = dict(monitor_exit_meta)
    if extra:
        out.update(extra)
    return out


def _prepare_offhours_limit(
    ticker: str,
    *,
    adapter: Any,
) -> dict[str, Any]:
    tick, fresh = adapter.get_best_bid_ask(ticker)
    if tick is None:
        return {"ok": False, "error": "no_quote"}
    age_seconds = fresh.age_seconds() if fresh is not None else None
    if age_seconds is not None and age_seconds > _OFF_HOURS_MAX_QUOTE_AGE_SECONDS:
        return {"ok": False, "error": "stale_quote", "age_seconds": round(age_seconds, 3)}
    bid = _safe_float(getattr(tick, "bid", None))
    ask = _safe_float(getattr(tick, "ask", None))
    spread_bps = _safe_float(getattr(tick, "spread_bps", None))
    if bid is None or bid <= 0:
        return {"ok": False, "error": "bad_bid"}
    if ask is None or ask <= 0:
        return {"ok": False, "error": "bad_ask"}
    if spread_bps is None or spread_bps > _OFF_HOURS_MAX_SPREAD_BPS:
        return {
            "ok": False,
            "error": "wide_spread",
            "spread_bps": round(float(spread_bps or 0.0), 3),
        }
    return {
        "ok": True,
        "limit_price": _round_limit_price(float(bid)),
        "reference_price": _safe_float(getattr(tick, "mid", None)) or _safe_float(getattr(tick, "last_price", None)) or float(bid),
        "spread_bps": round(float(spread_bps), 3),
    }


def cancel_pending_exit_order(
    db: Session,
    trade: Trade,
    *,
    reason: str,
    audit_decision_prefix: str,
    adapter: Any | None = None,
    monitor_exit_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .venue.robinhood_spot import RobinhoodSpotAdapter

    if not trade.pending_exit_order_id:
        clear_pending_exit_fields(trade)
        db.add(trade)
        db.commit()
        return {"ok": True, "state": "cleared"}

    adapter = adapter or RobinhoodSpotAdapter()
    res = adapter.cancel_order(str(trade.pending_exit_order_id))
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "cancel_failed"}

    prior = {
        "pending_exit_order_id": trade.pending_exit_order_id,
        "pending_exit_status": trade.pending_exit_status,
        "pending_exit_reason": trade.pending_exit_reason,
    }
    clear_pending_exit_fields(trade)
    db.add(trade)
    db.commit()
    _record_autotrader_run(
        db,
        trade,
        decision=f"{audit_decision_prefix}_cancelled",
        reason=reason,
        snapshot=_audit_snapshot(
            trade,
            exit_reason=str(prior.get("pending_exit_reason") or ""),
            monitor_exit_meta=monitor_exit_meta,
            extra=prior,
        ),
    )
    return {"ok": True, "state": "cancelled"}


def submit_robinhood_trade_exit(
    db: Session,
    trade: Trade,
    *,
    exit_reason: str,
    audit_decision_prefix: str,
    client_order_id: str,
    adapter: Any | None = None,
    monitor_exit_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .venue.robinhood_spot import RobinhoodSpotAdapter

    now = _coerce_utc()
    adapter = adapter or RobinhoodSpotAdapter()
    qty = float(trade.quantity or 0.0)
    if qty <= 0:
        return {"ok": False, "error": "bad_qty"}

    if has_active_pending_exit(trade):
        active_reason = (trade.pending_exit_reason or "").strip().lower()
        if active_reason == (exit_reason or "").strip().lower():
            return {
                "ok": True,
                "state": "working",
                "reason": "existing_pending_exit",
                "order_id": trade.pending_exit_order_id,
            }
        cancelled = cancel_pending_exit_order(
            db,
            trade,
            reason=f"replace_exit_reason:{exit_reason}",
            audit_decision_prefix=audit_decision_prefix,
            adapter=adapter,
            monitor_exit_meta=monitor_exit_meta,
        )
        if not cancelled.get("ok"):
            return {"ok": False, "error": f"cancel_pending_exit:{cancelled.get('error')}"}

    window = describe_robinhood_equity_execution_window(
        trade.ticker,
        adapter=adapter,
        now_utc=now,
    )
    if not window.get("can_submit_now"):
        _mark_deferred_exit(
            db,
            trade,
            exit_reason=exit_reason,
            execution_reason=str(window.get("execution_reason") or "deferred"),
            now_utc=now,
        )
        _record_autotrader_run(
            db,
            trade,
            decision=f"{audit_decision_prefix}_deferred",
            reason=str(window.get("execution_reason") or "deferred"),
            snapshot=_audit_snapshot(
                trade,
                exit_reason=exit_reason,
                monitor_exit_meta=monitor_exit_meta,
                extra={
                    "execution_window": window,
                },
            ),
        )
        return {
            "ok": True,
            "state": "deferred",
            "execution_reason": window.get("execution_reason"),
            "next_eligible_session_at": window.get("next_eligible_session_at"),
        }

    order_type = "market"
    limit_price: float | None = None
    reference_price: float | None = None
    submit_base_size = str(qty)
    submit_kwargs: dict[str, Any] = {
        "market_hours_override": str(window.get("market_hours") or "regular_hours"),
        "extended_hours_override": bool(window.get("market_hours") != "regular_hours"),
    }
    if window.get("session") != "regular_hours":
        if not _is_whole_share_quantity(qty):
            _mark_deferred_exit(
                db,
                trade,
                exit_reason=exit_reason,
                execution_reason="Off-hours exit requires whole shares",
                now_utc=now,
            )
            _record_autotrader_run(
                db,
                trade,
                decision=f"{audit_decision_prefix}_deferred",
                reason="whole_shares_required",
                snapshot=_audit_snapshot(
                    trade,
                    exit_reason=exit_reason,
                    monitor_exit_meta=monitor_exit_meta,
                    extra={"execution_window": window},
                ),
            )
            return {"ok": True, "state": "deferred", "execution_reason": "whole_shares_required"}
        limit_ctx = _prepare_offhours_limit(trade.ticker, adapter=adapter)
        if not limit_ctx.get("ok"):
            _mark_deferred_exit(
                db,
                trade,
                exit_reason=exit_reason,
                execution_reason=str(limit_ctx.get("error") or "offhours_quote_rejected"),
                now_utc=now,
            )
            _record_autotrader_run(
                db,
                trade,
                decision=f"{audit_decision_prefix}_deferred",
                reason=str(limit_ctx.get("error") or "offhours_quote_rejected"),
                snapshot=_audit_snapshot(
                    trade,
                    exit_reason=exit_reason,
                    monitor_exit_meta=monitor_exit_meta,
                    extra={**window, **limit_ctx},
                ),
            )
            return {"ok": True, "state": "deferred", "execution_reason": limit_ctx.get("error")}
        order_type = "limit"
        limit_price = float(limit_ctx["limit_price"])
        reference_price = _safe_float(limit_ctx.get("reference_price"))
        submit_base_size = str(int(round(qty)))

    if reference_price is None:
        try:
            reference_price = float(adapter.get_quote_price(trade.ticker) or 0.0) or None
        except Exception:
            reference_price = None
    if reference_price is not None:
        trade.tca_reference_exit_price = float(reference_price)

    try:
        if order_type == "limit":
            res = adapter.place_limit_order_gtc(
                product_id=trade.ticker,
                side="sell",
                base_size=submit_base_size,
                limit_price=str(limit_price),
                client_order_id=client_order_id,
                **submit_kwargs,
            )
        else:
            res = adapter.place_market_order(
                product_id=trade.ticker,
                side="sell",
                base_size=submit_base_size,
                client_order_id=client_order_id,
                **submit_kwargs,
            )
    except Exception as exc:
        logger.exception("[rh_exit] submit sell failed trade=%s", trade.id)
        return {"ok": False, "error": str(exc)}

    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "sell_failed"}

    raw = dict(res.get("raw") or {})
    state = str(res.get("state") or raw.get("state") or "submitted").lower()
    order_id = str(res.get("order_id") or raw.get("id") or "")
    if state == "filled":
        pnl = _finalize_filled_exit(
            db,
            trade,
            raw_order=raw,
            exit_reason=exit_reason,
            fallback_price=reference_price,
            filled_at=now,
        )
        _record_autotrader_run(
            db,
            trade,
            decision=f"{audit_decision_prefix}_filled",
            reason=exit_reason,
            snapshot=_audit_snapshot(
                trade,
                exit_reason=exit_reason,
                monitor_exit_meta=monitor_exit_meta,
                extra={
                    "broker_state": state,
                    "order_type": order_type,
                    "limit_price": limit_price,
                    "pnl": pnl,
                },
            ),
        )
        return {"ok": True, "state": "filled", "pnl": pnl, "order_id": order_id}

    _mark_pending_exit_order(
        db,
        trade,
        order_id=order_id,
        status=state,
        exit_reason=exit_reason,
        requested_at=now,
        limit_price=limit_price,
    )
    _record_autotrader_run(
        db,
        trade,
        decision=f"{audit_decision_prefix}_submitted",
        reason=exit_reason,
        snapshot=_audit_snapshot(
            trade,
            exit_reason=exit_reason,
            monitor_exit_meta=monitor_exit_meta,
            extra={
                "broker_state": state,
                "order_type": order_type,
                "order_id": order_id,
                "limit_price": limit_price,
                "execution_window": window,
            },
        ),
    )
    return {"ok": True, "state": "working", "broker_state": state, "order_id": order_id}


def sync_pending_exit_order(
    db: Session,
    trade: Trade,
    *,
    order: dict[str, Any],
    audit_decision_prefix: str = "monitor_exit",
) -> dict[str, Any]:
    state = str(order.get("state") or "").lower()
    now = _coerce_utc()
    trade.pending_exit_status = state or trade.pending_exit_status
    trade.last_broker_sync = now.replace(tzinfo=None)
    db.add(trade)
    db.commit()

    if state == "filled":
        exit_reason = str(trade.pending_exit_reason or "pending_exit")
        pnl = _finalize_filled_exit(
            db,
            trade,
            raw_order=order,
            exit_reason=exit_reason,
            fallback_price=trade.pending_exit_limit_price,
            filled_at=_parse_dt(order.get("last_transaction_at")) or now,
        )
        _record_autotrader_run(
            db,
            trade,
            decision=f"{audit_decision_prefix}_filled",
            reason=exit_reason,
            snapshot=_audit_snapshot(
                trade,
                exit_reason=exit_reason,
                extra={"broker_state": state, "order_id": order.get("id"), "pnl": pnl},
            ),
        )
        return {"state": "filled", "pnl": pnl}

    if state in ("cancelled", "canceled", "rejected", "failed", "expired"):
        prior_reason = str(trade.pending_exit_reason or "")
        prior_order_id = str(trade.pending_exit_order_id or "")
        clear_pending_exit_fields(trade)
        db.add(trade)
        db.commit()
        _record_autotrader_run(
            db,
            trade,
            decision=f"{audit_decision_prefix}_cancelled",
            reason=state,
            snapshot=_audit_snapshot(
                trade,
                exit_reason=prior_reason,
                extra={"broker_state": state, "order_id": prior_order_id},
            ),
        )
        return {"state": state}

    return {"state": state or "working"}


def describe_trade_execution_state(
    trade: Trade,
    *,
    latest_monitor_action: str | None = None,
    adapter: Any | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    pending_status = (trade.pending_exit_status or "").strip().lower()
    need_window = bool(
        trade.pending_exit_status
        or (latest_monitor_action or "").strip().lower() == "exit_now"
    )
    window = (
        describe_robinhood_equity_execution_window(
            trade.ticker,
            adapter=adapter,
            now_utc=now_utc,
        )
        if need_window and (trade.ticker or "")
        else None
    )

    if has_active_pending_exit(trade):
        return {
            "execution_state": "exit_order_working",
            "execution_label": "EXIT ORDER WORKING",
            "execution_reason": f"Pending exit order: {pending_status or 'working'}",
            "pending_exit_status": trade.pending_exit_status,
            "pending_exit_order_id": trade.pending_exit_order_id,
            "pending_exit_limit_price": trade.pending_exit_limit_price,
            "next_eligible_session_at": None,
        }

    if pending_status == "deferred":
        label = "DEFERRED"
        if window is not None and window.get("session") == "closed_weekend":
            label = "WEEKEND CLOSED"
        return {
            "execution_state": "deferred",
            "execution_label": label,
            "execution_reason": (
                str(window.get("execution_reason"))
                if window is not None and window.get("execution_reason")
                else "Waiting for next eligible session"
            ),
            "pending_exit_status": trade.pending_exit_status,
            "pending_exit_order_id": trade.pending_exit_order_id,
            "pending_exit_limit_price": trade.pending_exit_limit_price,
            "next_eligible_session_at": (
                window.get("next_eligible_session_at").isoformat()
                if window is not None and window.get("next_eligible_session_at") is not None
                else None
            ),
        }

    if (latest_monitor_action or "").strip().lower() == "exit_now" and window is not None and not window.get("can_submit_now"):
        label = "WEEKEND CLOSED" if window.get("session") == "closed_weekend" else "DEFERRED"
        return {
            "execution_state": "deferred",
            "execution_label": label,
            "execution_reason": str(window.get("execution_reason") or "Waiting for next eligible session"),
            "pending_exit_status": trade.pending_exit_status,
            "pending_exit_order_id": trade.pending_exit_order_id,
            "pending_exit_limit_price": trade.pending_exit_limit_price,
            "next_eligible_session_at": (
                window.get("next_eligible_session_at").isoformat()
                if window.get("next_eligible_session_at") is not None
                else None
            ),
        }

    return {
        "execution_state": "idle",
        "execution_label": None,
        "execution_reason": None,
        "pending_exit_status": trade.pending_exit_status,
        "pending_exit_order_id": trade.pending_exit_order_id,
        "pending_exit_limit_price": trade.pending_exit_limit_price,
        "next_eligible_session_at": None,
    }
