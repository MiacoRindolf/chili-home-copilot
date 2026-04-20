"""Phase G - persistence layer for bracket intents (shadow-safe).

Upserts one row into ``trading_bracket_intents`` per live ``Trade`` with
the bracket we would have placed at the broker. In shadow mode the
broker child order ids stay NULL and ``intent_state`` transitions only
through ``intent`` / ``shadow_logged`` / ``reconciled``.

The writer does **not** call the broker, does not compute the bracket
(that's the pure ``bracket_intent.compute_bracket_intent``), and does
not run the reconciliation sweep. It is the single persistence surface
callers should use from the emitter call-site.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.bracket_intent_ops_log import (
    format_bracket_intent_ops_line,
)
from .bracket_intent import BracketIntentInput, BracketIntentResult, compute_bracket_intent

logger = logging.getLogger(__name__)
_ALLOWED_MODES = ("off", "shadow", "compare", "authoritative")


def _effective_mode(override: str | None = None) -> str:
    m = (override or getattr(settings, "brain_live_brackets_mode", "off") or "off").lower()
    return m if m in _ALLOWED_MODES else "off"


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) != "off"


def _ops_log_enabled() -> bool:
    return bool(getattr(settings, "brain_live_brackets_ops_log_enabled", True))


# ── Dataclasses ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UpsertResult:
    intent_id: int
    created: bool
    state: str
    stop_price: float | None
    target_price: float | None


# ── Upsert ──────────────────────────────────────────────────────────────


def upsert_bracket_intent(
    db: Session,
    *,
    trade_id: int,
    user_id: int | None,
    bracket_input: BracketIntentInput,
    bracket_result: BracketIntentResult | None = None,
    broker_source: str | None = None,
    mode_override: str | None = None,
) -> UpsertResult | None:
    """Idempotent upsert for the bracket intent of a live Trade.

    * Mode ``off`` short-circuits and returns ``None``.
    * Shadow mode writes ``intent_state='shadow_logged'``.
    * Compare / authoritative leave the state_write path to Phase G.2;
      in this phase we refuse to overwrite any row already in
      ``authoritative_submitted`` state and log an ops line instead.

    Returns an ``UpsertResult`` describing what changed, or ``None`` when
    the mode is off.
    """
    mode = _effective_mode(mode_override)
    if mode == "off":
        return None

    if bracket_result is None:
        bracket_result = compute_bracket_intent(bracket_input)

    new_state = "shadow_logged" if mode == "shadow" else "intent"
    shadow_flag = mode in ("shadow", "compare")

    existing = db.execute(text("""
        SELECT id, intent_state FROM trading_bracket_intents WHERE trade_id = :tid
    """), {"tid": int(trade_id)}).fetchone()

    if existing and (existing[1] or "").startswith("authoritative"):
        if _ops_log_enabled():
            logger.info(
                format_bracket_intent_ops_line(
                    event="intent_write_skipped",
                    mode=mode,
                    trade_id=trade_id,
                    bracket_intent_id=int(existing[0]),
                    ticker=bracket_input.ticker,
                    intent_state=existing[1],
                    reason="authoritative_state_protected",
                )
            )
        return UpsertResult(
            intent_id=int(existing[0]),
            created=False,
            state=existing[1] or "",
            stop_price=bracket_result.stop_price,
            target_price=bracket_result.target_price,
        )

    payload_json: dict[str, Any] = {
        "reasoning": bracket_result.reasoning,
        "brain_summary": bracket_result.brain_summary,
        "stop_model_resolved": bracket_result.stop_model_resolved,
    }

    now = datetime.utcnow()
    if existing is None:
        row = db.execute(text("""
            INSERT INTO trading_bracket_intents (
                trade_id, user_id, ticker, direction, quantity, entry_price,
                stop_price, target_price, stop_model, pattern_id, regime,
                intent_state, shadow_mode, broker_source,
                payload_json, created_at, updated_at
            ) VALUES (
                :trade_id, :user_id, :ticker, :direction, :quantity, :entry_price,
                :stop_price, :target_price, :stop_model, :pattern_id, :regime,
                :intent_state, :shadow_mode, :broker_source,
                CAST(:payload_json AS JSONB), :now, :now
            )
            RETURNING id
        """), {
            "trade_id": int(trade_id),
            "user_id": user_id,
            "ticker": bracket_input.ticker,
            "direction": bracket_input.direction,
            "quantity": float(bracket_input.quantity),
            "entry_price": float(bracket_input.entry_price),
            "stop_price": bracket_result.stop_price,
            "target_price": bracket_result.target_price,
            "stop_model": bracket_result.stop_model_resolved,
            "pattern_id": bracket_input.pattern_id,
            "regime": bracket_input.regime,
            "intent_state": new_state,
            "shadow_mode": shadow_flag,
            "broker_source": broker_source,
            "payload_json": _json_dumps(payload_json),
            "now": now,
        })
        new_id = int(row.scalar_one())
        created = True
    else:
        new_id = int(existing[0])
        created = False
        db.execute(text("""
            UPDATE trading_bracket_intents
            SET
                user_id = :user_id,
                ticker = :ticker,
                direction = :direction,
                quantity = :quantity,
                entry_price = :entry_price,
                stop_price = :stop_price,
                target_price = :target_price,
                stop_model = :stop_model,
                pattern_id = :pattern_id,
                regime = :regime,
                intent_state = :intent_state,
                shadow_mode = :shadow_mode,
                broker_source = :broker_source,
                payload_json = CAST(:payload_json AS JSONB),
                updated_at = :now
            WHERE id = :id
        """), {
            "id": new_id,
            "user_id": user_id,
            "ticker": bracket_input.ticker,
            "direction": bracket_input.direction,
            "quantity": float(bracket_input.quantity),
            "entry_price": float(bracket_input.entry_price),
            "stop_price": bracket_result.stop_price,
            "target_price": bracket_result.target_price,
            "stop_model": bracket_result.stop_model_resolved,
            "pattern_id": bracket_input.pattern_id,
            "regime": bracket_input.regime,
            "intent_state": new_state,
            "shadow_mode": shadow_flag,
            "broker_source": broker_source,
            "payload_json": _json_dumps(payload_json),
            "now": now,
        })
    db.commit()

    if _ops_log_enabled():
        logger.info(
            format_bracket_intent_ops_line(
                event="intent_write",
                mode=mode,
                trade_id=trade_id,
                bracket_intent_id=new_id,
                ticker=bracket_input.ticker,
                direction=bracket_input.direction,
                quantity=bracket_input.quantity,
                entry_price=bracket_input.entry_price,
                stop_price=bracket_result.stop_price,
                target_price=bracket_result.target_price,
                stop_model=bracket_result.stop_model_resolved,
                pattern_id=bracket_input.pattern_id,
                regime=bracket_input.regime,
                intent_state=new_state,
                broker_source=broker_source,
            )
        )

    return UpsertResult(
        intent_id=new_id,
        created=created,
        state=new_state,
        stop_price=bracket_result.stop_price,
        target_price=bracket_result.target_price,
    )


def mark_reconciled(
    db: Session,
    intent_id: int,
    *,
    reason: str,
    mode_override: str | None = None,
) -> bool:
    """Flip a bracket intent into ``reconciled`` state.

    Used by the reconciliation service when the broker now agrees with
    our local view (or when the Trade has closed). Returns True when a
    row was updated.
    """
    mode = _effective_mode(mode_override)
    if mode == "off":
        return False

    result = db.execute(text("""
        UPDATE trading_bracket_intents
        SET intent_state = 'reconciled',
            last_observed_at = NOW(),
            last_diff_reason = :reason,
            updated_at = NOW()
        WHERE id = :id
          AND intent_state NOT LIKE 'authoritative%'
    """), {"id": int(intent_id), "reason": reason[:128] if reason else None})
    db.commit()

    updated = bool(result.rowcount)
    if updated and _ops_log_enabled():
        logger.info(
            format_bracket_intent_ops_line(
                event="mark_reconciled",
                mode=mode,
                bracket_intent_id=intent_id,
                reason=reason[:128] if reason else None,
            )
        )
    return updated


def bump_last_observed(
    db: Session,
    intent_id: int,
    *,
    diff_reason: str | None = None,
) -> bool:
    """P0.5 — mark a bracket intent as observed by the current reconciliation
    sweep *without* changing its ``intent_state``.

    This is the crash-recovery signal: the watchdog looks at
    ``NOW() - last_observed_at`` to detect intents whose sweeps have been
    silently dropping (reconciler crash, scheduler stall, DB connectivity
    issue). ``mark_reconciled`` keeps doing its transition; this helper is
    called for *every* row the sweep classifies, including ``missing_stop`` /
    ``orphan_stop`` / ``qty_drift`` — i.e. rows that will stay in
    non-terminal state after the sweep.

    Never advances state and never commits; the caller commits at end of
    the sweep so a single transaction covers the batch. Returns True when
    the target row exists and was touched.
    """
    result = db.execute(text("""
        UPDATE trading_bracket_intents
        SET last_observed_at = NOW(),
            last_diff_reason = COALESCE(:diff_reason, last_diff_reason)
        WHERE id = :id
    """), {
        "id": int(intent_id),
        "diff_reason": (diff_reason or "")[:128] or None,
    })
    return bool(result.rowcount)


# ── Diagnostics summary ────────────────────────────────────────────────


def bracket_intent_summary(
    db: Session,
    *,
    lookback_hours: int = 24,
) -> dict[str, Any]:
    """Frozen-shape summary for the diagnostics endpoint.

    Keys (stable, order-preserving):
        mode, lookback_hours, intents_total,
        by_state {state: count}, by_broker_source {src: count},
        latest_intent {trade_id, ticker, intent_state, updated_at}
    """
    mode = _effective_mode()
    rows = db.execute(text("""
        SELECT intent_state, COUNT(*)
        FROM trading_bracket_intents
        WHERE updated_at >= (NOW() - (:lh || ' hours')::INTERVAL)
        GROUP BY intent_state
    """), {"lh": int(lookback_hours)}).fetchall()
    by_state = {r[0]: int(r[1]) for r in rows}
    total = sum(by_state.values())

    brk_rows = db.execute(text("""
        SELECT COALESCE(broker_source, '_none_'), COUNT(*)
        FROM trading_bracket_intents
        WHERE updated_at >= (NOW() - (:lh || ' hours')::INTERVAL)
        GROUP BY COALESCE(broker_source, '_none_')
    """), {"lh": int(lookback_hours)}).fetchall()
    by_broker = {r[0]: int(r[1]) for r in brk_rows}

    latest = db.execute(text("""
        SELECT trade_id, ticker, intent_state, updated_at
        FROM trading_bracket_intents
        ORDER BY updated_at DESC
        LIMIT 1
    """)).fetchone()
    latest_payload: dict[str, Any] | None = None
    if latest:
        latest_payload = {
            "trade_id": int(latest[0]),
            "ticker": latest[1],
            "intent_state": latest[2],
            "updated_at": latest[3].isoformat() if latest[3] else None,
        }

    return {
        "mode": mode,
        "lookback_hours": int(lookback_hours),
        "intents_total": total,
        "by_state": by_state,
        "by_broker_source": by_broker,
        "latest_intent": latest_payload,
    }


def _json_dumps(value: Any) -> str:
    import json
    return json.dumps(value, default=str, separators=(",", ":"))


__all__ = [
    "UpsertResult",
    "bracket_intent_summary",
    "bump_last_observed",
    "mark_reconciled",
    "mode_is_active",
    "upsert_bracket_intent",
]
