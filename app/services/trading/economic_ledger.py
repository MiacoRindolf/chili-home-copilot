"""Phase A: canonical economic-truth ledger.

Append-only, idempotent recording of economic events (entry fills, exit
fills, fees, adjustments) with explicit ``cash_delta`` and
``realized_pnl_delta``. Parallel to legacy live/paper management rows;
legacy ``pnl`` columns remain authoritative until a later cutover phase.

Rollout ladder (matches Phase B/E):
    off -> shadow -> compare -> authoritative

In any mode != ``authoritative`` the ledger MUST NOT mutate legacy pnl,
cash, or position tables. It observes and logs.

Safety properties:
- Idempotent: partial unique indexes on (paper_trade_id, event_type) and
  (trade_id, event_type) for 'entry_fill'/'exit_fill' prevent duplicate
  rows. Python-side pre-check short-circuits so we never rely solely on
  DB integrity errors for the hot path.
- Pure ledger math: cash_delta and realized_pnl_delta are computed from
  arguments only, no network, no quote lookups, no RNG.
- Shadow-safe: raising inside a hook must never break the legacy caller.
  Callers are expected to wrap in try/except. This module still guards
  internally where it makes sense.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import EconomicLedgerEvent, LedgerParityLog, MomentumAutomationOutcome
from ...trading_brain.infrastructure.ledger_ops_log import (
    EVENT_ENTRY_FILL,
    EVENT_EXIT_FILL,
    EVENT_PARTIAL_FILL,
    EVENT_RECONCILE,
    MODE_AUTHORITATIVE,
    MODE_OFF,
    SOURCE_AUTOMATION,
    format_ledger_ops_line,
)

logger = logging.getLogger(__name__)

_VALID_MODES = {"off", "shadow", "compare", "authoritative"}
_VALID_SOURCES = {"paper", "live", "broker_sync", SOURCE_AUTOMATION}
_VALID_DIRECTIONS = {"long", "short"}


def _current_mode() -> str:
    m = str(getattr(settings, "brain_economic_ledger_mode", "off") or "off").strip().lower()
    return m if m in _VALID_MODES else "off"


def mode_is_active() -> bool:
    """True when the ledger should record events (shadow/compare/authoritative)."""
    return _current_mode() != MODE_OFF


def _ops_log_enabled() -> bool:
    return bool(getattr(settings, "brain_economic_ledger_ops_log_enabled", True))


def _parity_tolerance_usd() -> float:
    try:
        return float(getattr(settings, "brain_economic_ledger_parity_tolerance_usd", 0.01) or 0.01)
    except Exception:
        return 0.01


def _normalize_direction(direction: str | None) -> str:
    d = (direction or "long").strip().lower()
    return d if d in _VALID_DIRECTIONS else "long"


def _trade_ref(source: str, trade_id: int | None, paper_trade_id: int | None) -> str:
    if source == "paper" and paper_trade_id is not None:
        return f"paper:{int(paper_trade_id)}"
    if trade_id is not None:
        return f"{source}:{int(trade_id)}"
    return f"{source}:none"


def _automation_trade_id(session_id: int) -> int:
    """Use negative ids so automation-session ledger rows never collide with live row ids."""
    return -abs(int(session_id))


def automation_trade_id(session_id: int) -> int:
    """Public stable trade id for automation-session ledger/parity rows."""
    return _automation_trade_id(session_id)


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _existing_event(
    db: Session,
    *,
    source: str,
    trade_id: int | None,
    paper_trade_id: int | None,
    event_type: str,
) -> EconomicLedgerEvent | None:
    """Python-side idempotency check."""
    q = db.query(EconomicLedgerEvent).filter(
        EconomicLedgerEvent.event_type == event_type,
        EconomicLedgerEvent.source == source,
    )
    if source == "paper" and paper_trade_id is not None:
        q = q.filter(EconomicLedgerEvent.paper_trade_id == int(paper_trade_id))
    elif trade_id is not None:
        q = q.filter(EconomicLedgerEvent.trade_id == int(trade_id))
    else:
        return None
    return q.first()


def _emit_ops(
    *,
    mode: str,
    source: str,
    event_type: str,
    trade_ref: str,
    ticker: str,
    quantity: float | None,
    price: float | None,
    cash_delta: float | None,
    realized_pnl_delta: float | None,
    agree: bool | None = None,
) -> None:
    if not _ops_log_enabled():
        return
    try:
        line = format_ledger_ops_line(
            mode=mode,
            source=source,
            event_type=event_type,
            trade_ref=trade_ref,
            ticker=ticker,
            quantity=quantity,
            price=price,
            cash_delta=cash_delta,
            realized_pnl_delta=realized_pnl_delta,
            agree=agree,
        )
        logger.info(line)
    except Exception:
        logger.debug("[economic_ledger] ops log emit failed", exc_info=True)


def _compute_entry_cash_delta(
    *, direction: str, quantity: float, price: float, fee: float
) -> float:
    """Long entry: cash out. Short entry: cash in (short sale proceeds)."""
    notional = float(quantity) * float(price)
    if direction == "long":
        return -(notional + float(fee))
    return notional - float(fee)


def _compute_exit(
    *,
    direction: str,
    quantity: float,
    exit_price: float,
    entry_price: float,
    fee: float,
) -> tuple[float, float]:
    """Return (cash_delta, realized_pnl_delta) for the exit leg.

    Fees on this exit leg are subtracted from realized PnL. Entry-leg fees
    are already captured in the entry row's cash_delta but do not appear
    in realized_pnl_delta of entry (which is 0 by contract). To keep the
    two parity invariants simple we surface entry fee into the exit
    realized_pnl_delta via provenance, not math — callers who want
    fee-net-of-entry should pass ``fee = exit_fee + entry_fee_attrib``.
    """
    qty = float(quantity)
    xp = float(exit_price)
    ep = float(entry_price)
    f = float(fee)
    if direction == "long":
        cash_delta = qty * xp - f
        realized = qty * (xp - ep) - f
    else:
        cash_delta = -(qty * xp) - f
        realized = qty * (ep - xp) - f
    return cash_delta, realized


def record_entry_fill(
    db: Session,
    *,
    source: str,
    trade_id: int | None = None,
    paper_trade_id: int | None = None,
    user_id: int | None = None,
    scan_pattern_id: int | None = None,
    ticker: str,
    direction: str,
    quantity: float,
    fill_price: float,
    fee: float = 0.0,
    venue: str | None = None,
    broker_source: str | None = None,
    event_ts: datetime | None = None,
    mode: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> EconomicLedgerEvent | None:
    """Record an entry fill. Idempotent on (source, trade_ref, event_type='entry_fill')."""
    eff_mode = (mode or _current_mode()).strip().lower()
    if eff_mode == MODE_OFF:
        return None
    if source not in _VALID_SOURCES:
        logger.debug("[economic_ledger] unknown source=%s; skip", source)
        return None
    if quantity is None or fill_price is None or float(quantity) <= 0 or float(fill_price) <= 0:
        logger.debug("[economic_ledger] invalid entry qty=%s price=%s; skip", quantity, fill_price)
        return None

    direction = _normalize_direction(direction)
    qty = float(quantity)
    price = float(fill_price)
    fee_v = float(fee or 0.0)

    existing = _existing_event(
        db,
        source=source,
        trade_id=trade_id,
        paper_trade_id=paper_trade_id,
        event_type=EVENT_ENTRY_FILL,
    )
    if existing is not None:
        return existing

    cash_delta = _compute_entry_cash_delta(
        direction=direction, quantity=qty, price=price, fee=fee_v
    )
    row = EconomicLedgerEvent(
        source=source,
        trade_id=int(trade_id) if trade_id is not None else None,
        paper_trade_id=int(paper_trade_id) if paper_trade_id is not None else None,
        user_id=int(user_id) if user_id is not None else None,
        scan_pattern_id=int(scan_pattern_id) if scan_pattern_id is not None else None,
        ticker=str(ticker).upper()[:32],
        event_type=EVENT_ENTRY_FILL,
        direction=direction,
        quantity=qty,
        price=price,
        fee=fee_v,
        cash_delta=round(cash_delta, 6),
        realized_pnl_delta=0.0,
        position_qty_after=qty if direction == "long" else -qty,
        position_cost_basis_after=price,
        venue=(venue or None),
        broker_source=(broker_source or None),
        event_ts=event_ts,
        mode=eff_mode,
        provenance_json=provenance or None,
    )
    db.add(row)
    db.flush()

    _emit_ops(
        mode=eff_mode,
        source=source,
        event_type=EVENT_ENTRY_FILL,
        trade_ref=_trade_ref(source, trade_id, paper_trade_id),
        ticker=str(ticker),
        quantity=qty,
        price=price,
        cash_delta=cash_delta,
        realized_pnl_delta=0.0,
    )
    return row


def record_exit_fill(
    db: Session,
    *,
    source: str,
    trade_id: int | None = None,
    paper_trade_id: int | None = None,
    user_id: int | None = None,
    scan_pattern_id: int | None = None,
    ticker: str,
    direction: str,
    quantity: float,
    fill_price: float,
    entry_price: float,
    fee: float = 0.0,
    venue: str | None = None,
    broker_source: str | None = None,
    event_ts: datetime | None = None,
    mode: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> EconomicLedgerEvent | None:
    """Record an exit fill. Idempotent on (source, trade_ref, event_type='exit_fill')."""
    eff_mode = (mode or _current_mode()).strip().lower()
    if eff_mode == MODE_OFF:
        return None
    if source not in _VALID_SOURCES:
        logger.debug("[economic_ledger] unknown source=%s; skip", source)
        return None
    if (
        quantity is None
        or fill_price is None
        or entry_price is None
        or float(quantity) <= 0
        or float(fill_price) <= 0
        or float(entry_price) <= 0
    ):
        logger.debug(
            "[economic_ledger] invalid exit qty=%s fill=%s entry=%s; skip",
            quantity,
            fill_price,
            entry_price,
        )
        return None

    direction = _normalize_direction(direction)
    qty = float(quantity)
    xp = float(fill_price)
    ep = float(entry_price)
    fee_v = float(fee or 0.0)

    existing = _existing_event(
        db,
        source=source,
        trade_id=trade_id,
        paper_trade_id=paper_trade_id,
        event_type=EVENT_EXIT_FILL,
    )
    if existing is not None:
        return existing

    cash_delta, realized = _compute_exit(
        direction=direction,
        quantity=qty,
        exit_price=xp,
        entry_price=ep,
        fee=fee_v,
    )
    row = EconomicLedgerEvent(
        source=source,
        trade_id=int(trade_id) if trade_id is not None else None,
        paper_trade_id=int(paper_trade_id) if paper_trade_id is not None else None,
        user_id=int(user_id) if user_id is not None else None,
        scan_pattern_id=int(scan_pattern_id) if scan_pattern_id is not None else None,
        ticker=str(ticker).upper()[:32],
        event_type=EVENT_EXIT_FILL,
        direction=direction,
        quantity=qty,
        price=xp,
        fee=fee_v,
        cash_delta=round(cash_delta, 6),
        realized_pnl_delta=round(realized, 6),
        position_qty_after=0.0,
        position_cost_basis_after=None,
        venue=(venue or None),
        broker_source=(broker_source or None),
        event_ts=event_ts,
        mode=eff_mode,
        provenance_json=provenance or None,
    )
    db.add(row)
    db.flush()

    _emit_ops(
        mode=eff_mode,
        source=source,
        event_type=EVENT_EXIT_FILL,
        trade_ref=_trade_ref(source, trade_id, paper_trade_id),
        ticker=str(ticker),
        quantity=qty,
        price=xp,
        cash_delta=cash_delta,
        realized_pnl_delta=realized,
    )
    return row


def _sum_realized_for_trade(
    db: Session,
    *,
    source: str,
    trade_id: int | None,
    paper_trade_id: int | None,
) -> float:
    q = db.query(func.coalesce(func.sum(EconomicLedgerEvent.realized_pnl_delta), 0.0)).filter(
        EconomicLedgerEvent.source == source
    )
    if source == "paper" and paper_trade_id is not None:
        q = q.filter(EconomicLedgerEvent.paper_trade_id == int(paper_trade_id))
    elif trade_id is not None:
        q = q.filter(EconomicLedgerEvent.trade_id == int(trade_id))
    else:
        return 0.0
    return float(q.scalar() or 0.0)


def reconcile_trade(
    db: Session,
    *,
    source: str,
    trade_id: int | None = None,
    paper_trade_id: int | None = None,
    user_id: int | None = None,
    scan_pattern_id: int | None = None,
    ticker: str,
    legacy_pnl: float | None,
    mode: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> LedgerParityLog | None:
    """Write one parity row comparing legacy PnL vs ledger-derived PnL.

    Always writes a row when the ledger is active (off short-circuits).
    Agreement is ``|delta| <= tolerance`` on the absolute dollar PnL.
    """
    eff_mode = (mode or _current_mode()).strip().lower()
    if eff_mode == MODE_OFF:
        return None
    if source not in _VALID_SOURCES:
        return None

    ledger_pnl = _sum_realized_for_trade(
        db, source=source, trade_id=trade_id, paper_trade_id=paper_trade_id
    )
    legacy_v = float(legacy_pnl) if legacy_pnl is not None else None
    delta = None
    delta_abs = None
    tol = _parity_tolerance_usd()
    agree = False
    if legacy_v is not None:
        delta = ledger_pnl - legacy_v
        delta_abs = abs(delta)
        agree = delta_abs <= tol

    row = LedgerParityLog(
        source=source,
        trade_id=int(trade_id) if trade_id is not None else None,
        paper_trade_id=int(paper_trade_id) if paper_trade_id is not None else None,
        user_id=int(user_id) if user_id is not None else None,
        scan_pattern_id=int(scan_pattern_id) if scan_pattern_id is not None else None,
        ticker=str(ticker).upper()[:32],
        legacy_pnl=legacy_v,
        ledger_pnl=round(ledger_pnl, 6),
        delta_pnl=round(delta, 6) if delta is not None else None,
        delta_abs=round(delta_abs, 6) if delta_abs is not None else None,
        agree_bool=bool(agree),
        tolerance_usd=tol,
        mode=eff_mode,
        provenance_json=provenance or None,
    )
    db.add(row)
    db.flush()

    _emit_ops(
        mode=eff_mode,
        source=source,
        event_type=EVENT_RECONCILE,
        trade_ref=_trade_ref(source, trade_id, paper_trade_id),
        ticker=str(ticker),
        quantity=None,
        price=None,
        cash_delta=None,
        realized_pnl_delta=ledger_pnl,
        agree=agree,
    )
    return row


def record_automation_session_entry_fill(
    db: Session,
    *,
    session_id: int,
    user_id: int | None = None,
    scan_pattern_id: int | None = None,
    ticker: str,
    quantity: float,
    fill_price: float,
    fee: float = 0.0,
    venue: str | None = None,
    event_ts: datetime | None = None,
    mode: str = "paper",
    decision_packet_id: int | None = None,
    provenance: dict[str, Any] | None = None,
) -> EconomicLedgerEvent | None:
    """Record a momentum automation entry fill keyed by automation session."""
    prov = dict(provenance or {})
    prov["automation_session_id"] = int(session_id)
    if decision_packet_id is not None:
        prov["decision_packet_id"] = int(decision_packet_id)
    return record_entry_fill(
        db,
        source=SOURCE_AUTOMATION,
        trade_id=_automation_trade_id(session_id),
        user_id=user_id,
        scan_pattern_id=scan_pattern_id,
        ticker=ticker,
        direction="long",
        quantity=quantity,
        fill_price=fill_price,
        fee=fee,
        venue=venue,
        broker_source="momentum_automation",
        event_ts=event_ts,
        mode=mode,
        provenance=prov,
    )


def record_automation_session_exit_fill(
    db: Session,
    *,
    session_id: int,
    user_id: int | None = None,
    scan_pattern_id: int | None = None,
    ticker: str,
    quantity: float,
    fill_price: float,
    entry_price: float,
    fee: float = 0.0,
    venue: str | None = None,
    event_ts: datetime | None = None,
    mode: str = "paper",
    decision_packet_id: int | None = None,
    realized_pnl_usd: float | None = None,
    provenance: dict[str, Any] | None = None,
) -> EconomicLedgerEvent | None:
    """Record a terminal momentum automation exit fill keyed by automation session."""
    qty = _safe_float(quantity)
    fill = _safe_float(fill_price)
    entry = _safe_float(entry_price)
    fee_v = _safe_float(fee, 0.0) or 0.0
    pnl_v = _safe_float(realized_pnl_usd)
    if pnl_v is not None and qty and fill is not None and entry is not None:
        # The runner's realized PnL is the learning target. Back-solve the
        # fee field so ledger realized_pnl_delta matches that target exactly.
        fee_v = max(0.0, qty * (fill - entry) - pnl_v)
    prov = dict(provenance or {})
    prov["automation_session_id"] = int(session_id)
    if decision_packet_id is not None:
        prov["decision_packet_id"] = int(decision_packet_id)
    if realized_pnl_usd is not None:
        prov["runner_realized_pnl_usd"] = float(realized_pnl_usd)
    return record_exit_fill(
        db,
        source=SOURCE_AUTOMATION,
        trade_id=_automation_trade_id(session_id),
        user_id=user_id,
        scan_pattern_id=scan_pattern_id,
        ticker=ticker,
        direction="long",
        quantity=quantity,
        fill_price=fill_price,
        entry_price=entry_price,
        fee=fee_v,
        venue=venue,
        broker_source="momentum_automation",
        event_ts=event_ts,
        mode=mode,
        provenance=prov,
    )


def record_automation_session_partial_exit_fill(
    db: Session,
    *,
    session_id: int,
    user_id: int | None = None,
    scan_pattern_id: int | None = None,
    ticker: str,
    quantity: float,
    fill_price: float,
    entry_price: float,
    fee: float = 0.0,
    venue: str | None = None,
    event_ts: datetime | None = None,
    mode: str = "paper",
    decision_packet_id: int | None = None,
    realized_pnl_usd: float | None = None,
    provenance: dict[str, Any] | None = None,
) -> EconomicLedgerEvent | None:
    """Record a non-terminal momentum partial exit.

    This intentionally uses ``partial_fill`` rather than ``exit_fill`` so the
    terminal full-exit row remains idempotent per automation session.
    """
    eff_mode = (mode or _current_mode()).strip().lower()
    if eff_mode == MODE_OFF:
        return None
    qty = _safe_float(quantity)
    fill = _safe_float(fill_price)
    entry = _safe_float(entry_price)
    if qty is None or fill is None or entry is None or qty <= 0 or fill <= 0 or entry <= 0:
        return None
    fee_v = _safe_float(fee, 0.0) or 0.0
    pnl_v = _safe_float(realized_pnl_usd)
    if pnl_v is not None:
        fee_v = max(0.0, qty * (fill - entry) - pnl_v)
    cash_delta = qty * fill - fee_v
    realized = pnl_v if pnl_v is not None else qty * (fill - entry) - fee_v
    prov = dict(provenance or {})
    prov["automation_session_id"] = int(session_id)
    if decision_packet_id is not None:
        prov["decision_packet_id"] = int(decision_packet_id)
    if realized_pnl_usd is not None:
        prov["runner_realized_pnl_usd"] = float(realized_pnl_usd)

    row = EconomicLedgerEvent(
        source=SOURCE_AUTOMATION,
        trade_id=_automation_trade_id(session_id),
        paper_trade_id=None,
        user_id=int(user_id) if user_id is not None else None,
        scan_pattern_id=int(scan_pattern_id) if scan_pattern_id is not None else None,
        ticker=str(ticker).upper()[:32],
        event_type=EVENT_PARTIAL_FILL,
        direction="long",
        quantity=qty,
        price=fill,
        fee=fee_v,
        cash_delta=round(cash_delta, 6),
        realized_pnl_delta=round(realized, 6),
        position_qty_after=None,
        position_cost_basis_after=entry,
        venue=(venue or None),
        broker_source="momentum_automation",
        event_ts=event_ts,
        mode=eff_mode,
        provenance_json=prov,
    )
    db.add(row)
    db.flush()
    _emit_ops(
        mode=eff_mode,
        source=SOURCE_AUTOMATION,
        event_type=EVENT_PARTIAL_FILL,
        trade_ref=_trade_ref(SOURCE_AUTOMATION, _automation_trade_id(session_id), None),
        ticker=str(ticker),
        quantity=qty,
        price=fill,
        cash_delta=cash_delta,
        realized_pnl_delta=realized,
    )
    return row


def reconcile_automation_session(
    db: Session,
    *,
    session_id: int,
    user_id: int | None = None,
    scan_pattern_id: int | None = None,
    ticker: str,
    legacy_pnl: float | None,
    mode: str = "paper",
    provenance: dict[str, Any] | None = None,
) -> LedgerParityLog | None:
    prov = dict(provenance or {})
    prov["automation_session_id"] = int(session_id)
    return reconcile_trade(
        db,
        source=SOURCE_AUTOMATION,
        trade_id=_automation_trade_id(session_id),
        user_id=user_id,
        scan_pattern_id=scan_pattern_id,
        ticker=ticker,
        legacy_pnl=legacy_pnl,
        mode=mode,
        provenance=prov,
    )


def _latest_automation_parity(
    db: Session,
    *,
    session_id: int,
) -> LedgerParityLog | None:
    return (
        db.query(LedgerParityLog)
        .filter(
            LedgerParityLog.source == SOURCE_AUTOMATION,
            LedgerParityLog.trade_id == _automation_trade_id(session_id),
        )
        .order_by(LedgerParityLog.created_at.desc(), LedgerParityLog.id.desc())
        .first()
    )


def _automation_fill_event_counts(db: Session, *, session_id: int) -> tuple[int, int]:
    rows = (
        db.query(EconomicLedgerEvent.event_type, func.count(EconomicLedgerEvent.id))
        .filter(
            EconomicLedgerEvent.source == SOURCE_AUTOMATION,
            EconomicLedgerEvent.trade_id == _automation_trade_id(session_id),
            EconomicLedgerEvent.event_type.in_(
                [EVENT_ENTRY_FILL, EVENT_EXIT_FILL, EVENT_PARTIAL_FILL]
            ),
        )
        .group_by(EconomicLedgerEvent.event_type)
        .all()
    )
    counts = {str(event_type): int(n or 0) for event_type, n in rows}
    fill_count = sum(counts.values())
    realized_count = counts.get(EVENT_EXIT_FILL, 0) + counts.get(EVENT_PARTIAL_FILL, 0)
    return int(fill_count), int(realized_count)


def _dt_to_iso(value: Any) -> str | None:
    return value.isoformat() + "Z" if isinstance(value, datetime) else None


def _automation_parity_candidate_item(
    db: Session,
    row: MomentumAutomationOutcome,
    *,
    reason: str,
    ledger_event_count: int,
    realized_event_count: int,
    latest_parity: LedgerParityLog | None,
) -> dict[str, Any]:
    trade_id = _automation_trade_id(row.session_id)
    ledger_pnl = _sum_realized_for_trade(
        db,
        source=SOURCE_AUTOMATION,
        trade_id=trade_id,
        paper_trade_id=None,
    )
    return {
        "outcome_id": int(row.id),
        "session_id": int(row.session_id),
        "trade_id": int(trade_id),
        "user_id": int(row.user_id) if row.user_id is not None else None,
        "symbol": row.symbol,
        "mode": row.mode,
        "execution_family": row.execution_family,
        "terminal_at": _dt_to_iso(row.terminal_at),
        "legacy_pnl": float(row.realized_pnl_usd)
        if row.realized_pnl_usd is not None
        else None,
        "ledger_pnl": round(float(ledger_pnl), 6),
        "ledger_event_count": int(ledger_event_count),
        "realized_ledger_event_count": int(realized_event_count),
        "reason": reason,
        "latest_parity_id": int(latest_parity.id) if latest_parity is not None else None,
        "latest_parity_agree": bool(latest_parity.agree_bool)
        if latest_parity is not None
        else None,
        "latest_parity_created_at": _dt_to_iso(latest_parity.created_at)
        if latest_parity is not None
        else None,
    }


def _automation_parity_applied_item(
    candidate: dict[str, Any],
    parity: LedgerParityLog | None,
) -> dict[str, Any]:
    out = dict(candidate)
    out.update(
        {
            "parity_id": int(parity.id) if parity is not None else None,
            "agree_bool": bool(parity.agree_bool) if parity is not None else None,
            "delta_pnl": parity.delta_pnl if parity is not None else None,
            "delta_abs": parity.delta_abs if parity is not None else None,
            "tolerance_usd": parity.tolerance_usd if parity is not None else None,
            "created_at": _dt_to_iso(parity.created_at) if parity is not None else None,
        }
    )
    return out


def reconcile_missing_automation_outcome_parity(
    db: Session,
    *,
    days: int = 30,
    user_id: int | None = None,
    limit: int = 500,
    dry_run: bool = True,
    include_disagreed: bool = False,
) -> dict[str, Any]:
    """Backfill automation outcome parity rows when ledger fills already exist.

    This is an additive repair pass for historical momentum outcomes. It does
    not mutate trades, outcomes, positions, or neural weights. Apply mode only
    writes ``LedgerParityLog`` rows, after which the separate evolution-credit
    regrade can decide whether a row is now training-grade.
    """
    eff_mode = _current_mode()
    if eff_mode == MODE_OFF:
        return {
            "ok": True,
            "skipped": "ledger_inactive",
            "dry_run": bool(dry_run),
            "processed": 0,
            "candidate_count": 0,
            "applied_count": 0,
        }

    window_days = max(1, min(int(days or 30), 365))
    limit_i = max(0, min(int(limit or 0), 5000))
    since = datetime.utcnow() - timedelta(days=window_days)

    query = db.query(MomentumAutomationOutcome).filter(
        MomentumAutomationOutcome.terminal_at >= since,
        MomentumAutomationOutcome.realized_pnl_usd.isnot(None),
    )
    if user_id is not None:
        query = query.filter(MomentumAutomationOutcome.user_id == int(user_id))
    rows = (
        query.order_by(MomentumAutomationOutcome.terminal_at.desc(), MomentumAutomationOutcome.id.desc())
        .limit(limit_i)
        .all()
    )

    candidates: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    skipped_without_realized_ledger_events = 0
    skipped_existing_agree = 0
    skipped_existing_disagree = 0
    for row in rows:
        latest = _latest_automation_parity(db, session_id=int(row.session_id))
        if latest is not None and latest.agree_bool is True:
            skipped_existing_agree += 1
            continue
        if latest is not None and latest.agree_bool is False and not include_disagreed:
            skipped_existing_disagree += 1
            continue

        fill_count, realized_count = _automation_fill_event_counts(
            db,
            session_id=int(row.session_id),
        )
        if realized_count <= 0:
            skipped_without_realized_ledger_events += 1
            continue

        reason = "parity_missing" if latest is None else "parity_disagreed"
        candidate = _automation_parity_candidate_item(
            db,
            row,
            reason=reason,
            ledger_event_count=fill_count,
            realized_event_count=realized_count,
            latest_parity=latest,
        )
        candidates.append(candidate)
        if dry_run:
            continue

        parity = reconcile_automation_session(
            db,
            session_id=int(row.session_id),
            user_id=int(row.user_id) if row.user_id is not None else None,
            scan_pattern_id=None,
            ticker=str(row.symbol),
            legacy_pnl=row.realized_pnl_usd,
            mode=str(row.mode or eff_mode),
            provenance={
                "repair": "automation_outcome_parity_reconcile",
                "outcome_id": int(row.id),
                "reason": reason,
                "previous_parity_id": int(latest.id) if latest is not None else None,
            },
        )
        applied.append(_automation_parity_applied_item(candidate, parity))

    if not dry_run:
        try:
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            raise

    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "mode": "dry_run" if dry_run else "apply",
        "ledger_mode": eff_mode,
        "window_days": window_days,
        "limit": limit_i,
        "user_id": int(user_id) if user_id is not None else None,
        "include_disagreed": bool(include_disagreed),
        "processed": len(rows),
        "candidate_count": len(candidates),
        "applied_count": len(applied),
        "skipped_existing_agree": skipped_existing_agree,
        "skipped_existing_disagree": skipped_existing_disagree,
        "skipped_without_realized_ledger_events": skipped_without_realized_ledger_events,
        "note": "Apply mode only writes ledger parity rows; run evolution-credit regrade separately for learning credit.",
        "candidates": candidates if dry_run else [],
        "applied": applied if not dry_run else [],
    }


def ledger_summary(
    db: Session, *, lookback_hours: int = 24, source_filter: str | None = None
) -> dict[str, Any]:
    """Aggregated diagnostics for the ledger over the last N hours."""
    from datetime import timedelta

    since = datetime.utcnow() - timedelta(hours=max(1, int(lookback_hours)))
    eq = db.query(EconomicLedgerEvent).filter(EconomicLedgerEvent.created_at >= since)
    pq = db.query(LedgerParityLog).filter(LedgerParityLog.created_at >= since)
    if source_filter in _VALID_SOURCES:
        eq = eq.filter(EconomicLedgerEvent.source == source_filter)
        pq = pq.filter(LedgerParityLog.source == source_filter)

    events_total = eq.count()

    type_rows = (
        eq.with_entities(EconomicLedgerEvent.event_type, func.count(EconomicLedgerEvent.id))
        .group_by(EconomicLedgerEvent.event_type)
        .all()
    )
    events_by_type = {str(t): int(n) for t, n in type_rows}

    source_rows = (
        eq.with_entities(EconomicLedgerEvent.source, func.count(EconomicLedgerEvent.id))
        .group_by(EconomicLedgerEvent.source)
        .all()
    )
    events_by_source = {str(s): int(n) for s, n in source_rows}

    fill_events_q = eq.filter(
        EconomicLedgerEvent.event_type.in_(
            [EVENT_ENTRY_FILL, EVENT_EXIT_FILL, EVENT_PARTIAL_FILL]
        )
    )
    fill_events_total = fill_events_q.count()
    fill_events_with_packet = fill_events_q.filter(
        EconomicLedgerEvent.provenance_json.op("?")("decision_packet_id")
    ).count()
    packet_lineage_rate = (
        fill_events_with_packet / fill_events_total
        if fill_events_total
        else None
    )

    parity_total = pq.count()
    parity_agree = pq.filter(LedgerParityLog.agree_bool.is_(True)).count()
    parity_rate = (parity_agree / parity_total) if parity_total else None

    mean_abs_delta = (
        pq.with_entities(func.coalesce(func.avg(LedgerParityLog.delta_abs), 0.0)).scalar() or 0.0
    )
    max_abs_delta = (
        pq.with_entities(func.coalesce(func.max(LedgerParityLog.delta_abs), 0.0)).scalar() or 0.0
    )

    top_disagreements = (
        db.query(LedgerParityLog)
        .filter(LedgerParityLog.created_at >= since, LedgerParityLog.agree_bool.is_(False))
        .order_by(LedgerParityLog.delta_abs.desc().nullslast())
        .limit(10)
        .all()
    )
    top_list = [
        {
            "id": int(r.id),
            "source": r.source,
            "ticker": r.ticker,
            "trade_id": r.trade_id,
            "paper_trade_id": r.paper_trade_id,
            "legacy_pnl": r.legacy_pnl,
            "ledger_pnl": r.ledger_pnl,
            "delta_pnl": r.delta_pnl,
        }
        for r in top_disagreements
    ]

    return {
        "mode": _current_mode(),
        "lookback_hours": int(lookback_hours),
        "tolerance_usd": _parity_tolerance_usd(),
        "events_total": int(events_total),
        "events_by_type": events_by_type,
        "events_by_source": events_by_source,
        "fill_events_total": int(fill_events_total),
        "fill_events_with_decision_packet": int(fill_events_with_packet),
        "fill_event_packet_lineage_rate": packet_lineage_rate,
        "parity_total": int(parity_total),
        "parity_agree": int(parity_agree),
        "parity_disagree": int(parity_total - parity_agree),
        "parity_rate": parity_rate,
        "mean_abs_delta_usd": round(float(mean_abs_delta), 6),
        "max_abs_delta_usd": round(float(max_abs_delta), 6),
        "top_disagreements": top_list,
    }


__all__ = [
    "automation_trade_id",
    "mode_is_active",
    "record_entry_fill",
    "record_exit_fill",
    "record_automation_session_entry_fill",
    "record_automation_session_exit_fill",
    "record_automation_session_partial_exit_fill",
    "reconcile_automation_session",
    "reconcile_missing_automation_outcome_parity",
    "reconcile_trade",
    "ledger_summary",
]
