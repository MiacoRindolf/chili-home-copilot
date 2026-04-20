"""Normalized execution-event capture and telemetry aggregation."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session


def _utcnow() -> datetime:
    return datetime.utcnow()


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return None
    return None


def _midpoint(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    if bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2.0


def _spread_bps(bid: float | None, ask: float | None) -> float | None:
    mid = _midpoint(bid, ask)
    if mid is None or mid <= 0:
        return None
    return ((ask - bid) / mid) * 10000.0


def _buy_side_slippage_bps(reference_price: float | None, fill_price: float | None) -> float | None:
    if reference_price is None or fill_price is None or reference_price <= 0:
        return None
    return ((fill_price - reference_price) / reference_price) * 10000.0


def _duration_ms(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds() * 1000.0)


def _normalize_event_type(status: str | None) -> str:
    st = (status or "").strip().lower()
    if st in ("filled",):
        return "fill"
    if st in ("partially_filled",):
        return "partial_fill"
    if st in ("cancelled", "canceled", "expired"):
        return "cancel"
    if st in ("rejected", "failed"):
        return "reject"
    if st in ("queued", "pending", "confirmed", "open", "unconfirmed"):
        return "ack"
    return "status"


def _execution_family_for_broker(broker_source: str | None, ticker: str | None = None) -> str:
    broker = (broker_source or "manual").strip().lower()
    if broker == "coinbase":
        return "coinbase_spot"
    if broker == "robinhood":
        return "manual_equity" if (ticker or "").endswith("-USD") else "robinhood_equity"
    return "manual_equity"


def _venue_for_broker(broker_source: str | None) -> str:
    broker = (broker_source or "manual").strip().lower()
    if broker == "coinbase":
        return "coinbase"
    if broker == "robinhood":
        return "robinhood"
    return "manual"


def _provider_truth_mode_from_broker(broker_source: str | None) -> str:
    broker = (broker_source or "manual").strip().lower()
    if broker == "coinbase":
        return "exchange_event_audited"
    if broker == "robinhood":
        return "broker_event_audited"
    if broker == "manual":
        return "manual_recorded"
    return "unknown"


def apply_execution_event_to_trade(trade: Any, event: Any) -> None:
    requested = _safe_float(getattr(event, "requested_quantity", None))
    cumulative = _safe_float(getattr(event, "cumulative_filled_quantity", None))
    avg_fill = _safe_float(getattr(event, "average_fill_price", None))
    status = (getattr(event, "status", None) or "").strip().lower()

    if getattr(event, "submitted_at", None) and getattr(trade, "submitted_at", None) is None:
        trade.submitted_at = event.submitted_at
    if getattr(event, "acknowledged_at", None):
        trade.acknowledged_at = event.acknowledged_at
    if getattr(event, "first_fill_at", None) and getattr(trade, "first_fill_at", None) is None:
        trade.first_fill_at = event.first_fill_at
    if getattr(event, "last_fill_at", None):
        trade.last_fill_at = event.last_fill_at
        trade.filled_at = event.last_fill_at
    elif status == "filled":
        trade.filled_at = getattr(event, "event_at", None) or getattr(event, "acknowledged_at", None) or _utcnow()

    if requested is not None:
        if trade.quantity in (None, 0):
            trade.quantity = requested
        if cumulative is not None:
            trade.remaining_quantity = max(0.0, requested - cumulative)
    if cumulative is not None:
        trade.filled_quantity = cumulative
        if getattr(trade, "remaining_quantity", None) is None and requested is not None:
            trade.remaining_quantity = max(0.0, requested - cumulative)
        if cumulative > 0 and status in ("filled", "partially_filled", "open"):
            trade.status = "open" if status == "filled" else "working"
    if avg_fill is not None and avg_fill > 0:
        trade.avg_fill_price = avg_fill
        if status == "filled":
            trade.entry_price = avg_fill
    if status in ("cancelled", "canceled"):
        trade.status = "cancelled"
    elif status in ("rejected", "failed", "expired"):
        trade.status = "rejected" if status in ("rejected", "failed") else "cancelled"
    elif status in ("queued", "pending", "confirmed", "unconfirmed", "open", "partially_filled"):
        trade.status = "working"
    elif status == "filled":
        trade.status = "open"
    trade.broker_status = status or trade.broker_status
    trade.last_broker_sync = _utcnow()


def record_execution_event(
    db: Session,
    *,
    user_id: int | None,
    ticker: str | None,
    trade: Any | None = None,
    proposal: Any | None = None,
    scan_pattern_id: int | None = None,
    automation_session_id: int | None = None,
    venue: str | None = None,
    execution_family: str | None = None,
    broker_source: str | None = None,
    order_id: str | None = None,
    client_order_id: str | None = None,
    product_id: str | None = None,
    event_type: str,
    status: str | None,
    requested_quantity: float | None = None,
    cumulative_filled_quantity: float | None = None,
    last_fill_quantity: float | None = None,
    average_fill_price: float | None = None,
    submitted_at: datetime | None = None,
    acknowledged_at: datetime | None = None,
    first_fill_at: datetime | None = None,
    last_fill_at: datetime | None = None,
    event_at: datetime | None = None,
    reference_price: float | None = None,
    best_bid: float | None = None,
    best_ask: float | None = None,
    spread_bps: float | None = None,
    expected_slippage_bps: float | None = None,
    realized_slippage_bps: float | None = None,
    payload_json: dict[str, Any] | None = None,
) -> Any:
    from ...models.trading import TradingExecutionEvent

    broker = (broker_source or getattr(trade, "broker_source", None) or "manual").strip().lower()
    venue_name = venue or _venue_for_broker(broker)
    family = execution_family or _execution_family_for_broker(broker, ticker=ticker)
    event_row = TradingExecutionEvent(
        user_id=user_id,
        trade_id=getattr(trade, "id", None),
        proposal_id=getattr(proposal, "id", None),
        automation_session_id=automation_session_id,
        scan_pattern_id=scan_pattern_id or getattr(trade, "scan_pattern_id", None) or getattr(proposal, "scan_pattern_id", None),
        ticker=ticker,
        venue=venue_name,
        execution_family=family,
        broker_source=broker,
        order_id=order_id or getattr(trade, "broker_order_id", None),
        client_order_id=client_order_id,
        product_id=product_id,
        event_type=event_type,
        status=status,
        requested_quantity=requested_quantity,
        cumulative_filled_quantity=cumulative_filled_quantity,
        last_fill_quantity=last_fill_quantity,
        average_fill_price=average_fill_price,
        submitted_at=submitted_at,
        acknowledged_at=acknowledged_at,
        first_fill_at=first_fill_at,
        last_fill_at=last_fill_at,
        event_at=event_at or _utcnow(),
        reference_price=reference_price,
        best_bid=best_bid,
        best_ask=best_ask,
        spread_bps=spread_bps if spread_bps is not None else _spread_bps(best_bid, best_ask),
        expected_slippage_bps=expected_slippage_bps,
        realized_slippage_bps=realized_slippage_bps,
        submit_to_ack_ms=_duration_ms(submitted_at, acknowledged_at),
        ack_to_first_fill_ms=_duration_ms(acknowledged_at, first_fill_at),
        payload_json=dict(payload_json or {}),
    )
    db.add(event_row)
    db.flush()
    if trade is not None:
        apply_execution_event_to_trade(trade, event_row)

    # P1.1 — project the broker-native status onto a canonical state and
    # write one row to ``trading_order_state_log`` per transition. This is
    # additive: the state machine is opt-in via
    # ``settings.chili_order_state_machine_enabled`` (default False), so in
    # the rollout period this is a pure no-op. Failures here must never
    # mask the authoritative event row that was just written.
    try:
        from .venue.order_state_machine import record_from_broker_status

        record_from_broker_status(
            db,
            broker_status=status,
            venue=venue_name,
            source=f"execution_audit:{event_type}"[:32],
            order_id=order_id or getattr(trade, "broker_order_id", None),
            client_order_id=client_order_id,
            raw_payload={
                "event_type": event_type,
                "broker_source": broker,
                "requested_quantity": requested_quantity,
                "cumulative_filled_quantity": cumulative_filled_quantity,
                "average_fill_price": average_fill_price,
            },
        )
    except Exception:
        # Never let the state machine crash the execution event path.
        # The event row is already flushed — that's the authoritative record.
        pass

    return event_row


def normalize_robinhood_order_event(
    *,
    order: dict[str, Any],
    trade: Any | None = None,
    proposal: Any | None = None,
    event_type: str | None = None,
) -> dict[str, Any]:
    status = (order.get("state") or "").strip().lower()
    submitted_at = _parse_dt(order.get("created_at"))
    acknowledged_at = _parse_dt(order.get("updated_at")) or submitted_at
    last_fill_at = _parse_dt(order.get("last_transaction_at")) if order.get("last_transaction_at") else None
    first_fill_at = last_fill_at if status in ("filled", "partially_filled") else None
    requested_quantity = _safe_float(order.get("quantity"))
    cumulative_filled_quantity = _safe_float(order.get("cumulative_quantity"))
    average_fill_price = _safe_float(order.get("average_price"))
    reference_price = _safe_float(
        getattr(trade, "tca_reference_entry_price", None) or getattr(proposal, "entry_price", None)
    )
    return {
        "venue": "robinhood",
        "execution_family": _execution_family_for_broker("robinhood", getattr(trade, "ticker", None)),
        "broker_source": "robinhood",
        "order_id": order.get("id") or getattr(trade, "broker_order_id", None),
        "product_id": getattr(trade, "ticker", None),
        "event_type": event_type or _normalize_event_type(status),
        "status": status,
        "requested_quantity": requested_quantity,
        "cumulative_filled_quantity": cumulative_filled_quantity,
        "last_fill_quantity": cumulative_filled_quantity,
        "average_fill_price": average_fill_price,
        "submitted_at": submitted_at,
        "acknowledged_at": acknowledged_at,
        "first_fill_at": first_fill_at,
        "last_fill_at": last_fill_at,
        "event_at": last_fill_at or acknowledged_at or _utcnow(),
        "reference_price": reference_price,
        "realized_slippage_bps": _buy_side_slippage_bps(reference_price, average_fill_price),
        "payload_json": dict(order or {}),
    }


def normalize_coinbase_order_event(
    *,
    order: dict[str, Any],
    trade: Any | None = None,
    proposal: Any | None = None,
    event_type: str | None = None,
) -> dict[str, Any]:
    status = (order.get("status") or "").strip().lower()
    submitted_at = _parse_dt(order.get("created_time") or order.get("created_at"))
    acknowledged_at = _parse_dt(order.get("completion_percentage_time")) or submitted_at
    last_fill_at = _parse_dt(order.get("last_fill_time") or order.get("completion_time"))
    first_fill_at = last_fill_at if status == "filled" else None
    requested_quantity = _safe_float(order.get("base_size") or order.get("size"))
    cumulative_filled_quantity = _safe_float(order.get("filled_size"))
    average_fill_price = _safe_float(order.get("average_filled_price"))
    reference_price = _safe_float(
        getattr(trade, "tca_reference_entry_price", None) or getattr(proposal, "entry_price", None)
    )
    return {
        "venue": "coinbase",
        "execution_family": "coinbase_spot",
        "broker_source": "coinbase",
        "order_id": order.get("order_id") or getattr(trade, "broker_order_id", None),
        "client_order_id": order.get("client_order_id"),
        "product_id": order.get("product_id") or getattr(trade, "ticker", None),
        "event_type": event_type or _normalize_event_type(status),
        "status": status,
        "requested_quantity": requested_quantity,
        "cumulative_filled_quantity": cumulative_filled_quantity,
        "last_fill_quantity": cumulative_filled_quantity,
        "average_fill_price": average_fill_price,
        "submitted_at": submitted_at,
        "acknowledged_at": acknowledged_at,
        "first_fill_at": first_fill_at,
        "last_fill_at": last_fill_at,
        "event_at": last_fill_at or acknowledged_at or _utcnow(),
        "reference_price": reference_price,
        "realized_slippage_bps": _buy_side_slippage_bps(reference_price, average_fill_price),
        "payload_json": dict(order or {}),
    }


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    rows = sorted(float(v) for v in values)
    idx = max(0, min(len(rows) - 1, int(round((len(rows) - 1) * q))))
    return rows[idx]


def aggregate_execution_events_for_pattern(
    db: Session,
    *,
    scan_pattern_id: int,
    user_id: int,
    window_days: int,
) -> dict[str, Any]:
    from ...models.trading import TradingExecutionEvent

    since = _utcnow() - timedelta(days=max(1, int(window_days)))
    rows = (
        db.query(TradingExecutionEvent)
        .filter(
            TradingExecutionEvent.scan_pattern_id == int(scan_pattern_id),
            TradingExecutionEvent.user_id == int(user_id),
            TradingExecutionEvent.recorded_at >= since,
        )
        .order_by(
            TradingExecutionEvent.order_id.asc().nullslast(),
            TradingExecutionEvent.recorded_at.asc(),
            TradingExecutionEvent.id.asc(),
        )
        .all()
    )
    groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for row in rows:
        broker = (row.broker_source or "manual").strip().lower()
        oid = row.order_id or f"trade:{row.trade_id or row.id}"
        groups[(broker, oid)].append(row)

    order_stats: list[dict[str, Any]] = []
    for (broker, order_id), evs in groups.items():
        first = evs[0]
        last = evs[-1]
        requested = max((_safe_float(e.requested_quantity) or 0.0) for e in evs) or None
        cumulative = max((_safe_float(e.cumulative_filled_quantity) or 0.0) for e in evs) or None
        had_fill = bool((cumulative or 0.0) > 0 or any((e.event_type or "").endswith("fill") for e in evs))
        realizeds = [abs(v) for e in evs if (v := _safe_float(e.realized_slippage_bps)) is not None]
        expecteds = [abs(v) for e in evs if (v := _safe_float(e.expected_slippage_bps)) is not None]
        spreads = [abs(v) for e in evs if (v := _safe_float(e.spread_bps)) is not None]
        submit_ack = [v for e in evs if (v := _safe_float(e.submit_to_ack_ms)) is not None]
        ack_fill = [v for e in evs if (v := _safe_float(e.ack_to_first_fill_ms)) is not None]
        final_status = (last.status or "").strip().lower()
        partial = False
        if requested is not None and cumulative is not None:
            partial = 0.0 < cumulative < requested
        partial = partial or any((e.event_type or "") == "partial_fill" for e in evs)
        miss = final_status in ("cancelled", "canceled", "rejected", "failed", "expired") and not had_fill
        order_stats.append(
            {
                "broker_source": broker,
                "order_id": order_id,
                "requested": requested,
                "filled": cumulative or 0.0,
                "had_fill": had_fill,
                "partial": partial,
                "miss": miss,
                "final_status": final_status,
                "realized_slippage_bps": realizeds[0] if realizeds else None,
                "expected_slippage_bps": expecteds[0] if expecteds else None,
                "spread_bps": spreads[0] if spreads else None,
                "submit_to_ack_ms": submit_ack[0] if submit_ack else None,
                "ack_to_first_fill_ms": ack_fill[0] if ack_fill else None,
                "provider_truth_mode": _provider_truth_mode_from_broker(broker),
                "ticker": first.ticker,
                "execution_family": first.execution_family,
            }
        )

    n_orders = len(order_stats)
    n_filled = sum(1 for row in order_stats if row["had_fill"])
    n_partial = sum(1 for row in order_stats if row["partial"])
    n_miss = sum(1 for row in order_stats if row["miss"])
    realized_all = [row["realized_slippage_bps"] for row in order_stats if row["realized_slippage_bps"] is not None]
    expected_all = [row["expected_slippage_bps"] for row in order_stats if row["expected_slippage_bps"] is not None]
    spread_all = [row["spread_bps"] for row in order_stats if row["spread_bps"] is not None]
    submit_ack_all = [row["submit_to_ack_ms"] for row in order_stats if row["submit_to_ack_ms"] is not None]
    ack_fill_all = [row["ack_to_first_fill_ms"] for row in order_stats if row["ack_to_first_fill_ms"] is not None]
    modes = [row["provider_truth_mode"] for row in order_stats if row["provider_truth_mode"]]
    dominant_mode = max(set(modes), key=modes.count) if modes else "unknown"
    broker_sources = [row["broker_source"] for row in order_stats if row.get("broker_source")]
    dominant_broker = max(set(broker_sources), key=broker_sources.count) if broker_sources else None
    metric_coverage = {
        "expected_slippage_ratio": round(len(expected_all) / max(1, n_orders), 4),
        "realized_slippage_ratio": round(len(realized_all) / max(1, n_orders), 4),
        "spread_ratio": round(len(spread_all) / max(1, n_orders), 4),
        "latency_submit_ack_ratio": round(len(submit_ack_all) / max(1, n_orders), 4),
        "latency_ack_fill_ratio": round(len(ack_fill_all) / max(1, n_orders), 4),
    }
    return {
        "orders": order_stats,
        "n_orders": n_orders,
        "n_filled": n_filled,
        "n_partial": n_partial,
        "n_miss": n_miss,
        "fill_rate": round(n_filled / max(1, n_orders), 4) if n_orders else None,
        "partial_fill_rate": round(n_partial / max(1, n_filled), 4) if n_filled else None,
        "miss_rate": round(n_miss / max(1, n_orders), 4) if n_orders else None,
        "cancel_reject_rate": round(n_miss / max(1, n_orders), 4) if n_orders else None,
        "avg_expected_slippage_bps": round(sum(expected_all) / len(expected_all), 2) if expected_all else None,
        "avg_realized_slippage_bps": round(sum(realized_all) / len(realized_all), 2) if realized_all else None,
        "avg_spread_bps": round(sum(spread_all) / len(spread_all), 2) if spread_all else None,
        "latency_p50_ms": round(_percentile(submit_ack_all, 0.50), 2) if submit_ack_all else None,
        "latency_p95_ms": round(_percentile(submit_ack_all, 0.95), 2) if submit_ack_all else None,
        "ack_to_fill_p50_ms": round(_percentile(ack_fill_all, 0.50), 2) if ack_fill_all else None,
        "ack_to_fill_p95_ms": round(_percentile(ack_fill_all, 0.95), 2) if ack_fill_all else None,
        "provider_truth_mode": dominant_mode,
        "dominant_broker_source": dominant_broker,
        "metric_coverage": metric_coverage,
    }
