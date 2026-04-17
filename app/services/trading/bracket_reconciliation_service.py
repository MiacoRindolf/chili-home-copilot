"""Phase G - periodic reconciliation sweep (read-only, shadow-safe).

Reads open ``Trade`` rows + their ``BracketIntent`` rows, asks the
broker for its view via an injectable ``broker_view_fn``, classifies
each (trade, broker) pair through ``bracket_reconciler.classify_discrepancy``,
and persists one ``BracketReconciliationLog`` row per comparison.

This service is **strictly read-only against the broker** in Phase G.
It never submits, cancels, or modifies any broker order. Running with
``brain_live_brackets_mode=authoritative`` raises immediately so that
Phase G.2 must explicitly wire a writer path before enabling it.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.bracket_reconciliation_ops_log import (
    format_bracket_reconciliation_ops_line,
)
from .bracket_intent_writer import mark_reconciled
from .bracket_reconciler import (
    BrokerView,
    LocalView,
    ReconciliationDecision,
    Tolerances,
    classify_discrepancy,
)

logger = logging.getLogger(__name__)
_ALLOWED_MODES = ("off", "shadow", "compare", "authoritative")


def _effective_mode(override: str | None = None) -> str:
    m = (override or getattr(settings, "brain_live_brackets_mode", "off") or "off").lower()
    return m if m in _ALLOWED_MODES else "off"


def _ops_log_enabled() -> bool:
    return bool(getattr(settings, "brain_live_brackets_ops_log_enabled", True))


def _tolerances_from_settings() -> Tolerances:
    return Tolerances(
        price_drift_bps=float(getattr(settings, "brain_live_brackets_price_drift_bps", 25.0)),
        qty_drift_abs=float(getattr(settings, "brain_live_brackets_qty_drift_abs", 1e-6)),
    )


# ── Broker view provider (injectable) ──────────────────────────────────


BrokerViewFn = Callable[[list[dict[str, Any]]], list[BrokerView]]


def _noop_broker_view_fn(local_rows: list[dict[str, Any]]) -> list[BrokerView]:
    """Default broker provider: flags every ticker as ``available=False``.

    The scheduler job supplies a real provider that reads open orders +
    positions from ``broker_manager``; tests supply synthetic providers.
    Returning ``broker_down`` here by default means the sweep is safe
    even if the scheduler wires things up in the wrong order.
    """
    return [
        BrokerView(available=False, ticker=r.get("ticker"), broker_source=r.get("broker_source"))
        for r in local_rows
    ]


def broker_manager_view_fn(local_rows: list[dict[str, Any]]) -> list[BrokerView]:
    """Phase G default broker provider: reads combined positions across
    Robinhood + Coinbase via ``broker_manager.get_combined_positions``.

    In Phase G the broker has no bracket / stop primitives wired (that's
    Phase G.2), so ``stop_order_id`` / ``target_order_id`` are always
    ``None``. A live trade with a local ``BracketIntent`` will therefore
    classify as ``missing_stop`` (truthful: Phase G never places a
    server-side stop). Positions we cannot reach are flagged
    ``available=False`` so the reconciler emits ``broker_down``.
    """
    views: list[BrokerView] = []
    try:
        from ..broker_manager import get_combined_positions  # local import
        positions = get_combined_positions() or []
    except Exception:  # pragma: no cover - defensive
        logger.warning("[bracket_reconciliation] broker_manager_view_fn: unavailable", exc_info=True)
        positions = None

    if positions is None:
        return [
            BrokerView(
                available=False,
                ticker=r.get("ticker"),
                broker_source=r.get("broker_source"),
            )
            for r in local_rows
        ]

    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for p in positions:
        tkr = (p.get("ticker") or p.get("symbol") or "").upper() or None
        src = p.get("broker_source")
        if not tkr or not src:
            continue
        by_key[(tkr, src)] = p

    for row in local_rows:
        tkr = (row.get("ticker") or "").upper() or None
        src = row.get("broker_source")
        p = by_key.get((tkr, src)) if tkr and src else None
        if p is None:
            views.append(BrokerView(
                available=True,
                ticker=tkr,
                broker_source=src,
                position_quantity=0.0,
            ))
            continue
        qty = p.get("quantity") or p.get("qty") or p.get("shares") or 0
        try:
            qty_f = float(qty or 0)
        except Exception:
            qty_f = 0.0
        views.append(BrokerView(
            available=True,
            ticker=tkr,
            broker_source=src,
            position_quantity=qty_f,
        ))
    return views


# ── Result shape ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class SweepSummary:
    sweep_id: str
    mode: str
    trades_scanned: int
    brackets_checked: int
    agree: int
    orphan_stop: int
    missing_stop: int
    qty_drift: int
    state_drift: int
    price_drift: int
    broker_down: int
    unreconciled: int
    took_ms: float
    rows_written: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sweep_id": self.sweep_id,
            "mode": self.mode,
            "trades_scanned": self.trades_scanned,
            "brackets_checked": self.brackets_checked,
            "agree": self.agree,
            "orphan_stop": self.orphan_stop,
            "missing_stop": self.missing_stop,
            "qty_drift": self.qty_drift,
            "state_drift": self.state_drift,
            "price_drift": self.price_drift,
            "broker_down": self.broker_down,
            "unreconciled": self.unreconciled,
            "took_ms": self.took_ms,
            "rows_written": self.rows_written,
        }


# ── Main entry point ───────────────────────────────────────────────────


def run_reconciliation_sweep(
    db: Session,
    *,
    user_id: int | None = None,
    broker_view_fn: BrokerViewFn | None = None,
    mode_override: str | None = None,
) -> SweepSummary:
    """Run a single reconciliation sweep across open live trades.

    * Off mode → returns an empty summary without touching the DB or the
      broker.
    * Authoritative mode → raises ``RuntimeError``; Phase G.2 will wire
      a dedicated writer path and flip this gate.
    * Shadow / compare → reads broker view via ``broker_view_fn``, writes
      one row per comparison to ``trading_bracket_reconciliation_log``.
    """
    mode = _effective_mode(mode_override)
    sweep_id = str(uuid.uuid4())

    if mode == "off":
        return SweepSummary(
            sweep_id=sweep_id,
            mode="off",
            trades_scanned=0,
            brackets_checked=0,
            agree=0,
            orphan_stop=0,
            missing_stop=0,
            qty_drift=0,
            state_drift=0,
            price_drift=0,
            broker_down=0,
            unreconciled=0,
            took_ms=0.0,
            rows_written=0,
        )

    if mode == "authoritative":
        raise RuntimeError(
            "bracket_reconciliation_service.run_reconciliation_sweep refuses to run "
            "in authoritative mode during Phase G; that cutover belongs to Phase G.2."
        )

    broker_view_fn = broker_view_fn or _noop_broker_view_fn
    tolerances = _tolerances_from_settings()
    start = time.perf_counter()

    local_rows = _load_local_view(db, user_id=user_id)

    broker_input: list[dict[str, Any]] = [
        {"ticker": r["ticker"], "broker_source": r["broker_source"]}
        for r in local_rows
    ]
    broker_views = broker_view_fn(broker_input)
    broker_by_ticker = {(bv.ticker, bv.broker_source): bv for bv in broker_views}

    counts = {
        "agree": 0,
        "orphan_stop": 0,
        "missing_stop": 0,
        "qty_drift": 0,
        "state_drift": 0,
        "price_drift": 0,
        "broker_down": 0,
        "unreconciled": 0,
    }
    brackets_checked = 0
    rows_written = 0
    decisions: list[dict[str, Any]] = []

    for row in local_rows:
        local = LocalView(
            trade_id=row.get("trade_id"),
            bracket_intent_id=row.get("bracket_intent_id"),
            ticker=row.get("ticker"),
            direction=row.get("direction"),
            quantity=row.get("quantity"),
            intent_state=row.get("intent_state"),
            stop_price=row.get("stop_price"),
            target_price=row.get("target_price"),
            broker_source=row.get("broker_source"),
            trade_status=row.get("trade_status"),
        )

        broker = broker_by_ticker.get((local.ticker, local.broker_source))
        if broker is None:
            broker = BrokerView(
                available=False,
                ticker=local.ticker,
                broker_source=local.broker_source,
            )

        try:
            decision: ReconciliationDecision = classify_discrepancy(
                local, broker, tolerances=tolerances,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "[bracket_reconciliation] classify_discrepancy failed for trade %s: %s",
                local.trade_id, exc,
            )
            decision = ReconciliationDecision(
                kind="unreconciled", severity="error",
                delta_payload={"error": str(exc)},
            )

        counts[decision.kind] = counts.get(decision.kind, 0) + 1
        if local.bracket_intent_id is not None:
            brackets_checked += 1

        try:
            _write_reconciliation_row(
                db,
                sweep_id=sweep_id,
                mode=mode,
                local=local,
                broker=broker,
                decision=decision,
            )
            rows_written += 1
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "[bracket_reconciliation] failed to write log row for trade %s",
                local.trade_id, exc_info=True,
            )

        if decision.kind == "agree" and local.bracket_intent_id is not None:
            try:
                mark_reconciled(
                    db,
                    int(local.bracket_intent_id),
                    reason="agree",
                    mode_override=mode,
                )
            except Exception:  # pragma: no cover - defensive
                logger.debug(
                    "[bracket_reconciliation] mark_reconciled failed for intent %s",
                    local.bracket_intent_id,
                )

        decisions.append({
            "trade_id": local.trade_id,
            "bracket_intent_id": local.bracket_intent_id,
            "ticker": local.ticker,
            "broker_source": local.broker_source,
            "kind": decision.kind,
            "severity": decision.severity,
            "delta_payload": decision.delta_payload,
        })

        if _ops_log_enabled() and decision.kind != "agree":
            logger.info(
                format_bracket_reconciliation_ops_line(
                    event="discrepancy",
                    mode=mode,
                    sweep_id=sweep_id,
                    trade_id=local.trade_id,
                    bracket_intent_id=local.bracket_intent_id,
                    ticker=local.ticker,
                    broker_source=local.broker_source,
                    kind=decision.kind,
                    severity=decision.severity,
                )
            )

    try:
        db.commit()
    except Exception:  # pragma: no cover - defensive
        db.rollback()
        logger.warning("[bracket_reconciliation] failed to commit sweep %s", sweep_id)

    took_ms = (time.perf_counter() - start) * 1000.0
    summary = SweepSummary(
        sweep_id=sweep_id,
        mode=mode,
        trades_scanned=len(local_rows),
        brackets_checked=brackets_checked,
        agree=counts["agree"],
        orphan_stop=counts["orphan_stop"],
        missing_stop=counts["missing_stop"],
        qty_drift=counts["qty_drift"],
        state_drift=counts["state_drift"],
        price_drift=counts["price_drift"],
        broker_down=counts["broker_down"],
        unreconciled=counts["unreconciled"],
        took_ms=took_ms,
        rows_written=rows_written,
        decisions=decisions,
    )

    if _ops_log_enabled():
        logger.info(
            format_bracket_reconciliation_ops_line(
                event="sweep_summary",
                mode=mode,
                sweep_id=sweep_id,
                trades_scanned=summary.trades_scanned,
                brackets_checked=summary.brackets_checked,
                agree_count=summary.agree,
                orphan_stop=summary.orphan_stop,
                missing_stop=summary.missing_stop,
                qty_drift=summary.qty_drift,
                state_drift=summary.state_drift,
                price_drift=summary.price_drift,
                broker_down=summary.broker_down,
                unreconciled=summary.unreconciled,
                took_ms=summary.took_ms,
            )
        )

    return summary


# ── Local view loader ──────────────────────────────────────────────────


def _load_local_view(
    db: Session,
    *,
    user_id: int | None = None,
) -> list[dict[str, Any]]:
    """Load one row per open live ``Trade`` + its bracket intent.

    Returned dicts feed both ``LocalView`` and the broker-view provider.
    Paper trades (``broker_source IS NULL``) are excluded on purpose:
    paper state is authoritative locally and needs no broker check.
    """
    params: dict[str, Any] = {}
    filters = [
        "t.status = 'open'",
        "t.broker_source IS NOT NULL",
    ]
    if user_id is not None:
        filters.append("t.user_id = :uid")
        params["uid"] = int(user_id)

    sql = text(f"""
        SELECT
            t.id AS trade_id,
            t.user_id,
            t.ticker,
            t.direction,
            t.quantity,
            t.status AS trade_status,
            t.broker_source,
            bi.id AS bracket_intent_id,
            bi.intent_state,
            bi.stop_price,
            bi.target_price
        FROM trading_trades AS t
        LEFT JOIN trading_bracket_intents AS bi
          ON bi.trade_id = t.id
        WHERE {' AND '.join(filters)}
        ORDER BY t.id
    """)
    rows = db.execute(sql, params).fetchall()
    return [
        {
            "trade_id": int(r[0]),
            "user_id": r[1],
            "ticker": r[2],
            "direction": r[3],
            "quantity": float(r[4]) if r[4] is not None else None,
            "trade_status": r[5],
            "broker_source": r[6],
            "bracket_intent_id": int(r[7]) if r[7] is not None else None,
            "intent_state": r[8],
            "stop_price": float(r[9]) if r[9] is not None else None,
            "target_price": float(r[10]) if r[10] is not None else None,
        }
        for r in rows
    ]


def _write_reconciliation_row(
    db: Session,
    *,
    sweep_id: str,
    mode: str,
    local: LocalView,
    broker: BrokerView,
    decision: ReconciliationDecision,
) -> None:
    local_payload: dict[str, Any] = {
        "intent_state": local.intent_state,
        "stop_price": local.stop_price,
        "target_price": local.target_price,
        "quantity": local.quantity,
        "trade_status": local.trade_status,
    }
    broker_payload: dict[str, Any] = {
        "available": broker.available,
        "position_quantity": broker.position_quantity,
        "stop_order_id": broker.stop_order_id,
        "stop_order_state": broker.stop_order_state,
        "stop_order_price": broker.stop_order_price,
        "target_order_id": broker.target_order_id,
        "target_order_state": broker.target_order_state,
        "target_order_price": broker.target_order_price,
    }

    db.execute(text("""
        INSERT INTO trading_bracket_reconciliation_log (
            sweep_id, trade_id, bracket_intent_id, ticker, broker_source,
            kind, severity, local_payload, broker_payload, delta_payload,
            mode, observed_at
        ) VALUES (
            :sweep_id, :trade_id, :bracket_intent_id, :ticker, :broker_source,
            :kind, :severity,
            CAST(:local_payload AS JSONB),
            CAST(:broker_payload AS JSONB),
            CAST(:delta_payload AS JSONB),
            :mode, NOW()
        )
    """), {
        "sweep_id": sweep_id,
        "trade_id": local.trade_id,
        "bracket_intent_id": local.bracket_intent_id,
        "ticker": local.ticker,
        "broker_source": local.broker_source,
        "kind": decision.kind,
        "severity": decision.severity,
        "local_payload": _json_dumps(local_payload),
        "broker_payload": _json_dumps(broker_payload),
        "delta_payload": _json_dumps(decision.delta_payload),
        "mode": mode,
    })


# ── Diagnostics summary ────────────────────────────────────────────────


def bracket_reconciliation_summary(
    db: Session,
    *,
    lookback_hours: int = 24,
    recent_sweeps: int = 20,
) -> dict[str, Any]:
    """Frozen-shape summary for ``/brain/bracket-reconciliation/diagnostics``.

    Keys (stable):
        mode, lookback_hours, recent_sweeps_requested,
        rows_total, by_kind, by_severity,
        last_sweep_id, last_observed_at, sweeps_recent
    """
    mode = _effective_mode()

    rows = db.execute(text("""
        SELECT kind, COUNT(*)
        FROM trading_bracket_reconciliation_log
        WHERE observed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
        GROUP BY kind
    """), {"lh": int(lookback_hours)}).fetchall()
    by_kind = {r[0]: int(r[1]) for r in rows}
    rows_total = sum(by_kind.values())

    sev_rows = db.execute(text("""
        SELECT severity, COUNT(*)
        FROM trading_bracket_reconciliation_log
        WHERE observed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
        GROUP BY severity
    """), {"lh": int(lookback_hours)}).fetchall()
    by_severity = {r[0]: int(r[1]) for r in sev_rows}

    last_sweep = db.execute(text("""
        SELECT sweep_id, MAX(observed_at)
        FROM trading_bracket_reconciliation_log
        GROUP BY sweep_id
        ORDER BY MAX(observed_at) DESC
        LIMIT 1
    """)).fetchone()

    last_sweep_id = last_sweep[0] if last_sweep else None
    last_observed_at = (
        last_sweep[1].isoformat() if last_sweep and last_sweep[1] else None
    )

    sweeps_rows = db.execute(text("""
        SELECT sweep_id, MAX(observed_at) AS ts, COUNT(*) AS rows
        FROM trading_bracket_reconciliation_log
        GROUP BY sweep_id
        ORDER BY MAX(observed_at) DESC
        LIMIT :lim
    """), {"lim": int(recent_sweeps)}).fetchall()
    sweeps_recent = [
        {
            "sweep_id": r[0],
            "observed_at": r[1].isoformat() if r[1] else None,
            "rows": int(r[2]),
        }
        for r in sweeps_rows
    ]

    return {
        "mode": mode,
        "lookback_hours": int(lookback_hours),
        "recent_sweeps_requested": int(recent_sweeps),
        "rows_total": rows_total,
        "by_kind": by_kind,
        "by_severity": by_severity,
        "last_sweep_id": last_sweep_id,
        "last_observed_at": last_observed_at,
        "sweeps_recent": sweeps_recent,
    }


def _json_dumps(value: Any) -> str:
    import json
    return json.dumps(value, default=str, separators=(",", ":"))


__all__ = [
    "SweepSummary",
    "bracket_reconciliation_summary",
    "run_reconciliation_sweep",
]
