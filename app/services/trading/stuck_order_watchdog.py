"""P0.7 — stuck-order watchdog.

Cancels orders that the broker acknowledged but never transitioned to a
terminal state within the configured timeout. Without this, a queue
hiccup at the venue can leave a Trade row in ``status='open'`` with
``broker_status='queued'`` forever — the AutoTrader rule gate keeps
counting it as an open position (blocking new entries) and the
reconciler has no ground truth to resolve it against.

Timeouts come from settings:

* ``chili_stuck_order_market_timeout_seconds`` (default 300, i.e. 5 min)
* ``chili_stuck_order_limit_timeout_seconds`` (default 1800, i.e. 30 min)

Market orders get the short timeout because anything that doesn't fill
near-instantly on Robinhood during RTH points to a broker-side problem;
limit orders get the long timeout because they're explicitly resting
against a price level and a slow fill is expected.

Flow per candidate:

1. Ask the broker for the canonical order state.
2. If terminal → update the local Trade row to match (filled/cancelled/rejected).
3. If still non-terminal and elapsed > timeout → issue a cancel, log CRITICAL.

Runs on a scheduler interval; never raises.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import AutoTraderRun, Trade

logger = logging.getLogger(__name__)


# Broker statuses that mean "live, not yet terminal". Matches the lowercased
# Robinhood + Coinbase lexicons (see order_state_machine._ACK_STATUSES /
# _PARTIAL_STATUSES).
_NON_TERMINAL = {
    "queued", "confirmed", "submitted", "pending", "accepted",
    "acknowledged", "ack", "working", "open", "active",
    "partially_filled", "partial", "partial_filled",
    "unconfirmed",
}
_TERMINAL_FILLED = {"filled", "done", "completed", "complete"}
_TERMINAL_CANCELLED = {"cancelled", "canceled", "revoked"}
_TERMINAL_REJECTED = {"rejected", "failed", "denied", "expired", "timed_out"}


def _market_timeout() -> timedelta:
    return timedelta(
        seconds=int(getattr(settings, "chili_stuck_order_market_timeout_seconds", 300))
    )


def _limit_timeout() -> timedelta:
    return timedelta(
        seconds=int(getattr(settings, "chili_stuck_order_limit_timeout_seconds", 1800))
    )


def _maker_first_fallback_timeout() -> timedelta:
    return timedelta(
        seconds=int(
            getattr(settings, "chili_coinbase_maker_first_fallback_after_seconds", 300)
        )
    )


def _maker_first_edge_thin_hold_enabled() -> bool:
    return bool(
        getattr(settings, "chili_coinbase_maker_first_edge_thin_hold_enabled", True)
    )


def _maker_first_edge_thin_hold_timeout() -> timedelta:
    default_seconds = int(_limit_timeout().total_seconds())
    return timedelta(
        seconds=int(
            getattr(
                settings,
                "chili_coinbase_maker_first_edge_thin_hold_seconds",
                default_seconds,
            )
        )
    )


def _elapsed_since_submit(t: Trade, now: datetime) -> timedelta | None:
    submit_time = _effective_submit_time(t)
    if submit_time is None:
        return None
    if submit_time.tzinfo is not None:
        submit_time = submit_time.astimezone(timezone.utc).replace(tzinfo=None)
    return now - submit_time


def _snapshot_dict(t: Trade) -> dict[str, Any]:
    snap = t.indicator_snapshot if isinstance(t.indicator_snapshot, dict) else {}
    return dict(snap)


def _entry_execution_snapshot(t: Trade) -> dict[str, Any]:
    snap = _snapshot_dict(t)
    entry = snap.get("entry_execution")
    return dict(entry) if isinstance(entry, dict) else {}


def _update_entry_execution(t: Trade, **updates: Any) -> None:
    snap = _snapshot_dict(t)
    entry = dict(snap.get("entry_execution") or {})
    entry.update(updates)
    snap["entry_execution"] = entry
    t.indicator_snapshot = snap


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _option_meta_from_trade(t: Trade) -> dict[str, Any]:
    snap = _snapshot_dict(t)
    meta = snap.get("option_meta")
    if isinstance(meta, dict) and meta:
        return dict(meta)
    breakout = snap.get("breakout_alert")
    if isinstance(breakout, str):
        try:
            import json as _json

            breakout = _json.loads(breakout)
        except Exception:
            breakout = None
    if isinstance(breakout, dict):
        meta = breakout.get("option_meta")
        if isinstance(meta, dict) and meta:
            return dict(meta)
    return {}


def _get_options_adapter() -> Any:
    from .venue.robinhood_options import RobinhoodOptionsAdapter

    return RobinhoodOptionsAdapter()


def _position_qty(pos: dict[str, Any]) -> float:
    for key in ("quantity", "long_quantity", "short_quantity"):
        qty = _safe_float(pos.get(key))
        if qty is not None:
            return abs(qty)
    return 0.0


def _position_avg_price(pos: dict[str, Any]) -> float | None:
    for key in ("average_price", "avg_price", "average_open_price"):
        price = _safe_float(pos.get(key))
        if price is not None and price > 0:
            return price
    return None


def _option_tca_reference_price(t: Trade, meta: dict[str, Any]) -> float | None:
    entry = _entry_execution_snapshot(t)
    if str(entry.get("tca_reference_domain") or "").strip().lower() == "option_premium":
        tagged_ref = _safe_float(entry.get("tca_reference_entry_price"))
        if tagged_ref is not None and tagged_ref > 0:
            return tagged_ref

    for candidate in (meta.get("limit_price"), getattr(t, "entry_price", None)):
        price = _safe_float(candidate)
        if price is not None and price > 0:
            return price

    existing_ref = _safe_float(getattr(t, "tca_reference_entry_price", None))
    if existing_ref is not None and existing_ref > 0:
        return existing_ref
    return None


def _apply_option_entry_tca(t: Trade, meta: dict[str, Any]) -> float | None:
    ref = _option_tca_reference_price(t, meta)
    if ref is None:
        return None
    t.tca_reference_entry_price = ref
    try:
        from .tca_service import apply_tca_on_trade_fill

        apply_tca_on_trade_fill(t)
    except Exception:
        logger.debug(
            "[stuck_order_watchdog] option entry TCA failed trade=%s",
            getattr(t, "id", None),
            exc_info=True,
        )
    return ref


def _position_matches_option_meta(
    pos: dict[str, Any],
    *,
    trade: Trade,
    meta: dict[str, Any],
) -> bool:
    underlying = str(meta.get("underlying") or trade.ticker or "").strip().upper()
    pos_underlying = str(
        pos.get("chain_symbol")
        or pos.get("symbol")
        or pos.get("underlying")
        or pos.get("underlying_symbol")
        or ""
    ).strip().upper()
    if pos_underlying and underlying and pos_underlying != underlying:
        return False

    comparable = 0
    matched = 0

    expected_exp = str(meta.get("expiration") or "").strip()
    pos_exp = str(pos.get("expiration_date") or pos.get("expiration") or "").strip()
    if expected_exp and pos_exp:
        comparable += 1
        matched += int(expected_exp == pos_exp)

    expected_type = str(meta.get("option_type") or "").strip().lower()
    pos_type = str(pos.get("type") or pos.get("option_type") or "").strip().lower()
    if expected_type and pos_type:
        comparable += 1
        matched += int(expected_type == pos_type)

    expected_strike = _safe_float(meta.get("strike"))
    pos_strike = _safe_float(pos.get("strike_price") or pos.get("strike"))
    if expected_strike is not None and pos_strike is not None:
        comparable += 1
        matched += int(abs(expected_strike - pos_strike) < 0.0001)

    meta_option_id = str(meta.get("option_id") or meta.get("contract_id") or "").strip()
    pos_option_id = str(pos.get("option_id") or pos.get("id") or "").strip()
    if meta_option_id and pos_option_id and meta_option_id == pos_option_id:
        return True

    return comparable >= 2 and comparable == matched


def _process_option_position_truth(db: Session, t: Trade, now: datetime) -> str:
    meta = _option_meta_from_trade(t)
    if not meta:
        return "skipped_option_trade"
    try:
        adapter = _get_options_adapter()
        if hasattr(adapter, "is_enabled") and not adapter.is_enabled():
            return "skipped_option_trade"
        positions = adapter.get_open_positions() or []
    except Exception:
        logger.debug(
            "[stuck_order_watchdog] option position truth failed trade=%s",
            getattr(t, "id", None),
            exc_info=True,
        )
        return "skipped_option_trade"

    needed_qty = abs(float(t.quantity or 0.0))
    submit_time = _effective_submit_time(t)
    if submit_time is not None and submit_time.tzinfo is not None:
        submit_time = submit_time.astimezone(timezone.utc).replace(tzinfo=None)
    elapsed = (now - submit_time) if submit_time is not None else None
    timeout = _timeout_for(t)
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        if not _position_matches_option_meta(pos, trade=t, meta=meta):
            continue
        held_qty = _position_qty(pos)
        if held_qty <= 0:
            continue
        is_partial = needed_qty > 0 and held_qty + 1e-9 < needed_qty
        partial_timed_out = bool(is_partial and elapsed is not None and elapsed >= timeout)
        residual_cancel_error = None
        residual_cancelled = False
        if partial_timed_out:
            cancel_result = _try_cancel_option(adapter, t)
            residual_cancelled = bool(cancel_result.get("ok"))
            if not residual_cancelled:
                residual_cancel_error = cancel_result.get("error") or "cancel_failed"

        avg_price = _position_avg_price(pos)
        if is_partial and not residual_cancelled:
            t.status = "working"
            t.broker_status = "partially_filled"
            remaining_qty = max(0.0, needed_qty - held_qty) if needed_qty > 0 else None
        else:
            t.status = "open"
            t.broker_status = "partially_filled_cancelled" if is_partial else "filled"
            remaining_qty = 0.0
            if is_partial:
                t.quantity = held_qty
        t.filled_quantity = held_qty
        t.remaining_quantity = remaining_qty
        t.last_broker_sync = now
        t.filled_at = t.filled_at or now
        t.first_fill_at = t.first_fill_at or now
        t.last_fill_at = now
        if avg_price is not None:
            t.avg_fill_price = avg_price
            t.entry_price = avg_price
        tca_ref = _option_tca_reference_price(t, meta)
        if tca_ref is not None:
            t.tca_reference_entry_price = tca_ref
        if avg_price is not None:
            tca_ref = _apply_option_entry_tca(t, meta)
        _update_entry_execution(
            t,
            option_position_verified=True,
            option_position_verified_at=now.isoformat(),
            option_position_partial=is_partial,
            option_position_requested_quantity=needed_qty,
            option_position_quantity=held_qty,
            option_position_avg_price=avg_price,
            option_position_remaining_quantity=remaining_qty,
            option_position_residual_cancelled=residual_cancelled,
            option_position_residual_cancel_error=residual_cancel_error,
            option_entry_cancel_reason=(
                "partial_timeout_no_full_position" if residual_cancelled else None
            ),
            tca_reference_entry_price=tca_ref,
            tca_reference_domain="option_premium",
        )
        db.commit()
        if is_partial:
            if residual_cancelled:
                return "option_partial_position_timeout_cancelled_open"
            if residual_cancel_error:
                return "option_partial_position_cancel_failed"
            return "option_position_partial"
        return "option_position_verified"

    if submit_time is None:
        return "option_position_not_found"
    if elapsed is None:
        return "option_position_not_found"
    if elapsed < timeout:
        return "option_position_not_found"

    cancel_result = _try_cancel_option(adapter, t)
    if cancel_result.get("ok"):
        t.status = "cancelled"
        t.broker_status = "cancelled"
        t.exit_reason = "option_entry_timeout_no_position"
        t.last_broker_sync = now
        _update_entry_execution(
            t,
            option_position_verified=False,
            option_position_last_checked_at=now.isoformat(),
            option_position_timeout_seconds=int(elapsed.total_seconds()),
            option_entry_cancel_reason="timeout_no_position",
        )
        db.commit()
        return "option_entry_timeout_cancelled"
    return "option_entry_timeout_cancel_failed"


def _is_coinbase_maker_first_trade(t: Trade) -> bool:
    if (t.broker_source or "").lower() != "coinbase":
        return False
    if (t.management_scope or "").lower() != "auto_trader_v1":
        return False
    entry = _entry_execution_snapshot(t)
    if entry.get("maker_first_fallback_submitted"):
        return False
    active_type = str(entry.get("active_order_type") or entry.get("order_type") or "").lower()
    if active_type == "limit_post_only":
        return True
    if bool(entry.get("coinbase_maker_only")) and not bool(
        entry.get("maker_first_fallback_attempted")
    ):
        return True
    return (
        not entry
        and bool(getattr(settings, "chili_coinbase_maker_only_enabled", False))
        and str(t.broker_status or "").lower() in _NON_TERMINAL
    )


def _effective_submit_time(t: Trade) -> Optional[datetime]:
    """Return the best-available submission timestamp for this trade.

    ``submitted_at`` is populated by broker_position_sync once it pulls
    the authoritative value from the venue; for freshly-placed AutoTrader
    trades it's still NULL and ``entry_date`` (set at INSERT) is the
    closest proxy.
    """
    if t.submitted_at is not None:
        return t.submitted_at
    return t.entry_date


def _fetch_candidates(db: Session) -> list[Trade]:
    """Find open trades whose broker order is not in a terminal state."""
    return (
        db.query(Trade)
        .filter(
            Trade.status.in_(("open", "working")),
            Trade.broker_order_id.isnot(None),
            Trade.broker_order_id != "",
            or_(
                Trade.broker_status.is_(None),
                Trade.broker_status.in_(tuple(_NON_TERMINAL)),
            ),
            Trade.broker_source.in_(("robinhood", "coinbase")),
        )
        .all()
    )


def _get_adapter(broker_source: str) -> Any:
    """Return a venue adapter instance for ``broker_source``, or None.

    Delegates to :func:`venue.factory.get_adapter` so the supported-venue
    list has one definition. The factory already logs import failures.
    """
    from .venue.factory import get_adapter

    return get_adapter(broker_source)


def _infer_is_market(broker_status: Optional[str], pending_status: Optional[str]) -> bool:
    """Best-effort inference of whether this is a market order.

    The Trade model doesn't carry order_type, so we lean on signals we do
    have. AutoTrader v1 places ONLY market orders (see auto_trader._execute_*),
    so anything with ``management_scope='auto_trader_v1'`` is market. For
    other sources we default to the limit timeout since that's the safer
    (longer-wait) choice.
    """
    # Caller checks management_scope; this helper is a light final sanity
    # default. Keeping it separate makes the call site readable.
    return False


def _timeout_for(t: Trade) -> timedelta:
    try:
        from .autopilot_scope import is_option_trade

        if is_option_trade(t):
            return _limit_timeout()
    except Exception:
        pass
    scope = (t.management_scope or "").lower()
    if scope == "auto_trader_v1":
        if _is_coinbase_maker_first_trade(t):
            if bool(getattr(settings, "chili_coinbase_maker_first_fallback_enabled", True)):
                return _maker_first_fallback_timeout()
            return _limit_timeout()
        return _market_timeout()
    # Other sources (broker_sync, manual) could be anything — use the
    # longer limit-order timeout by default so we don't over-cancel.
    return _limit_timeout()


def _apply_terminal_state(t: Trade, broker_status: str, raw: dict[str, Any] | None) -> None:
    """Mirror a terminal broker status onto the Trade row.

    Doesn't commit — the caller controls the transaction boundary so the
    whole tick is atomic per-trade.
    """
    bs = (broker_status or "").strip().lower()
    t.broker_status = bs or t.broker_status
    t.last_broker_sync = datetime.utcnow()
    if bs in _TERMINAL_FILLED:
        filled_size = _raw_order_filled_size(raw)
        if filled_size <= 0.0:
            t.broker_status = "filled_zero_quantity"
            if t.filled_quantity is None:
                t.filled_quantity = 0.0
            if str(getattr(t, "broker_source", "") or "").strip().lower() == "coinbase":
                # Coinbase has been observed returning FILLED with zero size
                # from the order endpoint while fills/account positions already
                # contain the real inventory. Leave the entry working for
                # coinbase_service's fill/position truth reconciler instead of
                # locally erasing broker-held exposure.
                return
            t.status = "cancelled"
            t.filled_quantity = 0.0
            t.remaining_quantity = 0.0
            if not t.exit_reason:
                t.exit_reason = "stuck_order_terminal_filled_zero_quantity"
            return
        t.status = "open"
        t.filled_quantity = max(float(t.filled_quantity or 0.0), filled_size)
        requested = _as_float(getattr(t, "quantity", None))
        if requested is not None and requested > 0.0:
            t.remaining_quantity = max(0.0, requested - t.filled_quantity)
        elif not getattr(t, "quantity", None):
            t.quantity = filled_size
            t.remaining_quantity = 0.0
        # Don't flip status=closed — a FILLED entry is an *opened* position.
        # The exit evaluator closes when the exit fills. Leave status='open'.
        if raw:
            avg = (
                raw.get("average_filled_price")
                or raw.get("averageFilledPrice")
                or raw.get("average_price")
                or raw.get("price")
            )
            try:
                if avg is not None:
                    t.avg_fill_price = float(avg)
            except (TypeError, ValueError):
                pass
            if t.filled_at is None:
                t.filled_at = datetime.utcnow()
        try:
            from .tca_service import apply_tca_on_trade_fill

            apply_tca_on_trade_fill(t)
        except Exception:
            logger.debug(
                "[stuck_order_watchdog] entry TCA failed trade=%s",
                getattr(t, "id", None),
            )
        return
    if bs in _TERMINAL_CANCELLED:
        t.status = "cancelled"
        if not t.exit_reason:
            t.exit_reason = f"stuck_order_terminal_cancelled:{bs}"[:50]
        return
    if bs in _TERMINAL_REJECTED:
        t.status = "rejected"
        if not t.exit_reason:
            t.exit_reason = f"stuck_order_terminal_rejected:{bs}"[:50]
        return


def _try_cancel(adapter: Any, t: Trade) -> dict[str, Any]:
    """Issue a cancel through the adapter. Never raises."""
    try:
        return adapter.cancel_order(t.broker_order_id) or {}
    except Exception as e:
        logger.warning(
            "[stuck_order_watchdog] cancel_order raised for trade=%s order=%s: %s",
            t.id, t.broker_order_id, e, exc_info=True,
        )
        return {"ok": False, "error": str(e)}


def _try_cancel_option(adapter: Any, t: Trade) -> dict[str, Any]:
    try:
        if hasattr(adapter, "cancel"):
            return adapter.cancel(t.broker_order_id) or {}
        if hasattr(adapter, "cancel_order"):
            return adapter.cancel_order(t.broker_order_id) or {}
        return {"ok": False, "error": "option_adapter_cancel_unavailable"}
    except Exception as e:
        logger.warning(
            "[stuck_order_watchdog] option cancel raised for trade=%s order=%s: %s",
            t.id,
            t.broker_order_id,
            e,
            exc_info=True,
        )
        return {"ok": False, "error": str(e)}


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _raw_order_filled_size(raw: dict[str, Any] | None) -> float:
    if not raw:
        return 0.0
    for key in (
        "filled_size",
        "filledSize",
        "filled_quantity",
        "filledQuantity",
        "cumulative_quantity",
        "processed_quantity",
        "executed_quantity",
    ):
        value = _as_float(raw.get(key))
        if value is not None:
            return max(0.0, value)
    return 0.0


def _order_filled_size(order_normalized: Any) -> float:
    direct = _as_float(getattr(order_normalized, "filled_size", None))
    if direct is not None:
        return max(0.0, direct)
    raw = getattr(order_normalized, "raw", None)
    if not isinstance(raw, dict):
        return 0.0
    for key in ("filled_size", "filled_quantity", "cumulative_quantity"):
        v = _as_float(raw.get(key))
        if v is not None:
            return max(0.0, v)
    return 0.0


def _remaining_after_fill(t: Trade, filled_size: float) -> float | None:
    requested = _as_float(getattr(t, "quantity", None))
    if requested is not None and requested > 0.0:
        return max(0.0, requested - max(0.0, filled_size))
    current_remaining = _as_float(getattr(t, "remaining_quantity", None))
    if current_remaining is not None:
        return max(0.0, current_remaining)
    return None


def _latest_rule_snapshot(db: Session, t: Trade) -> dict[str, Any]:
    try:
        row = (
            db.query(AutoTraderRun)
            .filter(AutoTraderRun.trade_id == t.id)
            .order_by(AutoTraderRun.created_at.desc(), AutoTraderRun.id.desc())
            .first()
        )
    except Exception:
        return {}
    snap = getattr(row, "rule_snapshot", None) if row is not None else None
    return dict(snap) if isinstance(snap, dict) else {}


def _pick_float(*values: Any) -> float | None:
    for value in values:
        parsed = _as_float(value)
        if parsed is not None:
            return parsed
    return None


def _edge_pct_for_fallback(db: Session, t: Trade) -> tuple[float | None, dict[str, Any]]:
    entry = _entry_execution_snapshot(t)
    rule = _latest_rule_snapshot(db, t)
    edge = _pick_float(
        entry.get("entry_edge_expected_net_pct"),
        rule.get("entry_edge_expected_net_pct"),
        rule.get("expected_net_pct"),
        (rule.get("entry_edge") or {}).get("expected_net_pct")
        if isinstance(rule.get("entry_edge"), dict)
        else None,
    )
    fee_bps = _pick_float(
        entry.get("cost_gate_fee_bps"),
        rule.get("cost_gate_fee_bps"),
        getattr(settings, "chili_coinbase_taker_fee_bps_round_trip", 120),
    )
    safety_bps = _pick_float(getattr(settings, "chili_min_edge_safety_buffer_bps", 30))
    return edge, {
        "entry_edge_expected_net_pct": edge,
        "fee_bps": fee_bps or 0.0,
        "safety_buffer_bps": safety_bps or 0.0,
        "source": "trade_snapshot" if entry.get("entry_edge_expected_net_pct") is not None else "autotrader_run",
    }


def _fallback_client_order_id(t: Trade, now: datetime) -> str:
    alert_part = int(t.related_alert_id or 0)
    return f"atv1-{alert_part or t.id}-fb-{int(now.timestamp())}"


def _record_fallback_audit(
    db: Session,
    t: Trade,
    *,
    decision: str,
    reason: str,
    snapshot: dict[str, Any],
) -> None:
    try:
        db.add(
            AutoTraderRun(
                user_id=t.user_id,
                breakout_alert_id=t.related_alert_id,
                scan_pattern_id=t.scan_pattern_id,
                ticker=t.ticker,
                decision=decision,
                reason=reason,
                rule_snapshot=snapshot,
                llm_snapshot=None,
                management_scope=t.management_scope,
                trade_id=t.id,
            )
        )
    except Exception:
        logger.debug(
            "[stuck_order_watchdog] maker-first fallback audit write failed trade=%s",
            t.id,
            exc_info=True,
        )


def _try_maker_first_fallback(
    db: Session,
    adapter: Any,
    t: Trade,
    order_normalized: Any,
    now: datetime,
) -> str:
    if not bool(getattr(settings, "chili_coinbase_maker_first_fallback_enabled", True)):
        return "maker_first_fallback_disabled"

    filled_size = _order_filled_size(order_normalized)
    if filled_size > 0.0:
        t.filled_quantity = max(float(t.filled_quantity or 0.0), filled_size)
        t.remaining_quantity = _remaining_after_fill(t, filled_size)
        t.broker_status = (getattr(order_normalized, "status", None) or "partially_filled").lower()
        t.last_broker_sync = datetime.utcnow()
        if t.remaining_quantity is not None and t.remaining_quantity <= 0.0:
            t.status = "open"
            t.broker_status = "filled"
            _update_entry_execution(
                t,
                maker_first_fallback_decision="partial_fill_complete",
                maker_first_partial_fill_size=filled_size,
                maker_first_remaining_after_partial=0.0,
                maker_first_fallback_checked_at=now.isoformat(),
            )
            db.commit()
            return "maker_first_partial_fill_complete"
        elapsed = _elapsed_since_submit(t, now)
        _update_entry_execution(
            t,
            maker_first_fallback_decision="partial_fill_deferred",
            maker_first_partial_fill_size=filled_size,
            maker_first_remaining_after_partial=t.remaining_quantity,
            maker_first_fallback_checked_at=now.isoformat(),
        )
        if elapsed is None or elapsed < _maker_first_fallback_timeout():
            db.commit()
            return "maker_first_partial_fill_deferred"
        _update_entry_execution(
            t,
            maker_first_fallback_decision="partial_fill_timeout_reprice",
            maker_first_partial_fill_timeout_seconds=int(elapsed.total_seconds()),
        )
        db.commit()

    try:
        bbo, _fresh = adapter.get_best_bid_ask(t.ticker)
    except Exception as exc:
        logger.warning(
            "[stuck_order_watchdog] maker-first fallback BBO failed trade=%s ticker=%s: %s",
            t.id,
            t.ticker,
            exc,
            exc_info=True,
        )
        return "maker_first_fallback_bbo_error"

    bid = _as_float(getattr(bbo, "bid", None) if bbo is not None else None)
    ask = _as_float(getattr(bbo, "ask", None) if bbo is not None else None)
    if ask is None or ask <= 0.0:
        return "maker_first_fallback_no_bbo"
    mid = ((bid + ask) / 2.0) if bid is not None and bid > 0.0 else ask
    spread_bps = _pick_float(getattr(bbo, "spread_bps", None))
    if spread_bps is None and bid is not None and bid > 0.0 and mid > 0.0:
        spread_bps = max(0.0, (ask - bid) / mid * 10_000.0)
    spread_bps = max(0.0, spread_bps or 0.0)

    edge_pct, edge_snapshot = _edge_pct_for_fallback(db, t)
    price_buffer_bps = max(
        0.0,
        float(getattr(settings, "chili_coinbase_maker_first_taker_price_buffer_bps", 10.0) or 0.0),
    )
    fee_bps = float(edge_snapshot.get("fee_bps") or 0.0)
    safety_bps = float(edge_snapshot.get("safety_buffer_bps") or 0.0)
    fallback_cost_pct = (fee_bps + safety_bps + spread_bps + price_buffer_bps) / 100.0
    net_after_cost_pct = None if edge_pct is None else edge_pct - fallback_cost_pct
    min_net_pct = float(
        getattr(settings, "chili_coinbase_maker_first_min_net_after_cost_pct", 0.0) or 0.0
    )
    fallback_snapshot = {
        "maker_first_fallback": {
            **edge_snapshot,
            "bid": bid,
            "ask": ask,
            "spread_bps": round(spread_bps, 6),
            "price_buffer_bps": price_buffer_bps,
            "fallback_cost_pct": round(fallback_cost_pct, 6),
            "net_after_cost_pct": (
                round(net_after_cost_pct, 6) if net_after_cost_pct is not None else None
            ),
            "min_net_after_cost_pct": min_net_pct,
        }
    }
    if net_after_cost_pct is None or net_after_cost_pct < min_net_pct:
        hold_timeout = _maker_first_edge_thin_hold_timeout()
        elapsed = _elapsed_since_submit(t, now)
        if (
            _maker_first_edge_thin_hold_enabled()
            and elapsed is not None
            and elapsed < hold_timeout
        ):
            entry_before_hold = _entry_execution_snapshot(t)
            first_hold = not bool(entry_before_hold.get("maker_first_edge_thin_hold_started_at"))
            t.broker_status = (
                getattr(order_normalized, "status", None) or t.broker_status or "open"
            ).lower()
            t.last_broker_sync = datetime.utcnow()
            _update_entry_execution(
                t,
                maker_first_fallback_decision="edge_too_thin_holding_maker",
                maker_first_fallback_checked_at=now.isoformat(),
                maker_first_fallback_costs=fallback_snapshot["maker_first_fallback"],
                maker_first_edge_thin_hold_started_at=(
                    entry_before_hold.get("maker_first_edge_thin_hold_started_at")
                    or now.isoformat()
                ),
                maker_first_edge_thin_hold_seconds=int(hold_timeout.total_seconds()),
                maker_first_edge_thin_hold_elapsed_seconds=int(elapsed.total_seconds()),
            )
            if first_hold:
                _record_fallback_audit(
                    db,
                    t,
                    decision="placed",
                    reason="maker_first_edge_too_thin_holding_maker",
                    snapshot=fallback_snapshot,
                )
            db.commit()
            return "maker_first_edge_too_thin_holding_maker"

        cancel_result = _try_cancel(adapter, t)
        if cancel_result.get("ok"):
            t.status = "cancelled"
            t.broker_status = "cancelled"
            t.exit_reason = "maker_first_edge_too_thin"
            t.last_broker_sync = datetime.utcnow()
            _update_entry_execution(
                t,
                maker_first_fallback_attempted=True,
                maker_first_fallback_decision="edge_too_thin",
                maker_first_fallback_checked_at=now.isoformat(),
                maker_first_fallback_costs=fallback_snapshot["maker_first_fallback"],
            )
            _record_fallback_audit(
                db,
                t,
                decision="blocked",
                reason="maker_first_fallback_edge_too_thin",
                snapshot=fallback_snapshot,
            )
            db.commit()
            return "maker_first_fallback_edge_too_thin_cancelled"
        return "maker_first_fallback_edge_cancel_failed"

    cancel_result = _try_cancel(adapter, t)
    if not cancel_result.get("ok"):
        return "maker_first_fallback_cancel_failed"

    qty = _pick_float(t.remaining_quantity, t.quantity)
    if qty is None or qty <= 0.0:
        t.status = "rejected"
        t.broker_status = "fallback_bad_quantity"
        t.exit_reason = "maker_first_bad_quantity"
        t.last_broker_sync = datetime.utcnow()
        db.commit()
        return "maker_first_fallback_bad_quantity"

    limit_price = ask * (1.0 + (price_buffer_bps / 10_000.0))
    client_order_id = _fallback_client_order_id(t, now)
    try:
        fallback_res = adapter.place_limit_order_gtc(
            product_id=t.ticker,
            side="buy",
            base_size=f"{qty:.12f}",
            limit_price=f"{limit_price:.12f}",
            client_order_id=client_order_id,
            post_only=False,
        ) or {}
    except Exception as exc:
        logger.warning(
            "[stuck_order_watchdog] maker-first fallback place failed trade=%s ticker=%s: %s",
            t.id,
            t.ticker,
            exc,
            exc_info=True,
        )
        fallback_res = {"ok": False, "error": str(exc), "client_order_id": client_order_id}

    if not fallback_res.get("ok") or not fallback_res.get("order_id"):
        t.status = "rejected"
        t.broker_status = "fallback_rejected"
        t.exit_reason = "maker_first_fallback_rejected"
        t.last_broker_sync = datetime.utcnow()
        _update_entry_execution(
            t,
            maker_first_fallback_attempted=True,
            maker_first_fallback_decision="rejected",
            maker_first_fallback_checked_at=now.isoformat(),
            maker_first_fallback_error=str(fallback_res.get("error") or "missing_order_id")[:500],
            maker_first_fallback_costs=fallback_snapshot["maker_first_fallback"],
        )
        _record_fallback_audit(
            db,
            t,
            decision="blocked",
            reason=f"maker_first_fallback_rejected:{fallback_res.get('error')}",
            snapshot=fallback_snapshot,
        )
        db.commit()
        return "maker_first_fallback_rejected"

    original_order_id = t.broker_order_id
    t.broker_order_id = str(fallback_res.get("order_id"))
    t.broker_status = "accepted"
    t.status = "working"
    t.submitted_at = now
    t.acknowledged_at = now
    t.last_broker_sync = datetime.utcnow()
    t.remaining_quantity = _pick_float(fallback_res.get("base_size"), qty)
    _update_entry_execution(
        t,
        active_order_type="limit_takerable",
        maker_first_fallback_attempted=True,
        maker_first_fallback_submitted=True,
        maker_first_fallback_decision="submitted",
        maker_first_fallback_checked_at=now.isoformat(),
        maker_first_original_order_id=original_order_id,
        maker_first_fallback_order_id=t.broker_order_id,
        maker_first_fallback_client_order_id=fallback_res.get("client_order_id") or client_order_id,
        maker_first_fallback_limit_price=fallback_res.get("limit_price") or limit_price,
        maker_first_fallback_base_size=fallback_res.get("base_size") or qty,
        maker_first_fallback_costs=fallback_snapshot["maker_first_fallback"],
    )
    _record_fallback_audit(
        db,
        t,
        decision="placed",
        reason="maker_first_fallback_takerable_limit",
        snapshot=fallback_snapshot,
    )
    db.commit()
    logger.warning(
        "[stuck_order_watchdog] maker-first fallback submitted trade=%s ticker=%s "
        "old_order=%s new_order=%s net_after_cost_pct=%s",
        t.id,
        t.ticker,
        original_order_id,
        t.broker_order_id,
        round(net_after_cost_pct, 6),
    )
    return "maker_first_fallback_submitted"


def _process_one(db: Session, t: Trade, now: datetime) -> str:
    """Process one stuck-order candidate. Returns a short outcome string."""
    # Option orders cannot be queried through the RH spot adapter, so use
    # option-position truth instead of order-id truth. If the contract is
    # visible in open option positions, promote the working trade to open;
    # otherwise leave it working rather than fabricating a fill.
    try:
        from .autopilot_scope import is_option_trade
        if is_option_trade(t):
            return _process_option_position_truth(db, t, now)
    except Exception:
        pass
    submit_time = _effective_submit_time(t)
    if submit_time is None:
        return "no_submit_time"

    # Normalize to naive UTC (matches DB TIMESTAMP without tz).
    if submit_time.tzinfo is not None:
        submit_time = submit_time.astimezone(timezone.utc).replace(tzinfo=None)

    elapsed = now - submit_time
    timeout = _timeout_for(t)
    if elapsed < timeout:
        return "within_timeout"

    adapter = _get_adapter(t.broker_source or "")
    if adapter is None:
        logger.warning(
            "[stuck_order_watchdog] no adapter for trade=%s broker_source=%s",
            t.id, t.broker_source,
        )
        return "no_adapter"

    # Step 1: ask the broker what state the order is actually in.
    try:
        order_normalized, _fresh = adapter.get_order(t.broker_order_id)
    except Exception as e:
        logger.warning(
            "[stuck_order_watchdog] get_order raised for trade=%s order=%s: %s",
            t.id, t.broker_order_id, e, exc_info=True,
        )
        return "get_order_error"

    if order_normalized is None:
        # KKK -- broker get_order returned None. This is NOT a confirmed
        # rejection -- it can mean (1) transient broker lookup glitch,
        # (2) wrong-typed adapter (options/crypto via spot adapter), or
        # (3) the order really vanished. Only stamp "rejected" when
        # elapsed > 10x the normal timeout AND the trade has been retried.
        # Otherwise leave broker_status="unknown" so broker_sync (with
        # GGG revive) can reconcile if the broker actually filled.
        long_elapsed = elapsed > timeout * 10
        if long_elapsed:
            logger.critical(
                "[stuck_order_watchdog] trade=%s broker_order=%s unknown at venue "
                "for >10x timeout (%ss); marking rejected (KKK guard)",
                t.id, t.broker_order_id, int(elapsed.total_seconds()),
            )
            t.status = "rejected"
            t.broker_status = "unknown"
            t.last_broker_sync = datetime.utcnow()
            db.commit()
            return "unknown_at_venue_rejected"
        else:
            # Just record the lookup-uncertainty; trade stays open. broker_sync
            # GGG revive will reconcile if the position is actually held.
            logger.warning(
                "[stuck_order_watchdog] trade=%s broker_order=%s unknown at venue "
                "after %ss; deferring rejection (KKK guard) -- broker_sync will reconcile",
                t.id, t.broker_order_id, int(elapsed.total_seconds()),
            )
            t.broker_status = "unknown"
            t.last_broker_sync = datetime.utcnow()
            db.commit()
            return "unknown_at_venue_deferred"

    broker_status = (order_normalized.status or "").lower()
    # Step 2: if the broker already has a terminal state, just mirror it.
    if broker_status in _TERMINAL_FILLED | _TERMINAL_CANCELLED | _TERMINAL_REJECTED:
        _apply_terminal_state(
            t, broker_status, order_normalized.raw if hasattr(order_normalized, "raw") else None
        )
        db.commit()
        return f"mirrored:{broker_status}"

    if _is_coinbase_maker_first_trade(t):
        return _try_maker_first_fallback(db, adapter, t, order_normalized, now)

    # Step 3: still non-terminal past the timeout → cancel + log CRITICAL.
    logger.critical(
        "[stuck_order_watchdog] trade=%s broker_order=%s stuck in %s for %ss; cancelling",
        t.id, t.broker_order_id, broker_status or "unknown",
        int(elapsed.total_seconds()),
    )
    cancel_result = _try_cancel(adapter, t)
    if cancel_result.get("ok"):
        t.status = "cancelled"
        t.broker_status = "cancelled"
        if not t.exit_reason:
            t.exit_reason = "stuck_order_watchdog_timeout"
        t.last_broker_sync = datetime.utcnow()
        db.commit()
        return "cancelled"

    # Cancel itself failed — leave the row for the next tick. Don't mark the
    # trade cancelled locally because the broker may still fill it.
    logger.critical(
        "[stuck_order_watchdog] cancel failed for trade=%s error=%s",
        t.id, cancel_result.get("error"),
    )
    return "cancel_failed"


def tick_stuck_order_watchdog(db: Session) -> dict[str, Any]:
    """One pass of the watchdog. Safe to call on an interval."""
    if not bool(getattr(settings, "chili_stuck_order_watchdog_enabled", True)):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    now = datetime.utcnow()
    candidates = _fetch_candidates(db)
    out: dict[str, Any] = {"ok": True, "inspected": 0, "outcomes": {}}

    for t in candidates:
        out["inspected"] += 1
        try:
            outcome = _process_one(db, t, now)
        except Exception:
            logger.exception(
                "[stuck_order_watchdog] unexpected error on trade=%s", t.id
            )
            outcome = "error"
        out["outcomes"][outcome] = out["outcomes"].get(outcome, 0) + 1

    return out
