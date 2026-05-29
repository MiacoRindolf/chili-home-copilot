"""Phase G/G.2 - sole persistence layer for bracket intents.

This module is the SINGLE place in the codebase that writes to the
``trading_bracket_intents`` table. Every state mutation goes through
the explicit state machine in ``transition()``. Illegal transitions
(e.g. ``closed`` → ``intent``) are rejected, returning a structured
result so the caller can log + give up rather than producing the kind
of cascade we saw on 2026-05-01.

The writer does **not** call the broker, does not compute the bracket
(that's the pure ``bracket_intent.compute_bracket_intent``), and does
not run the reconciliation sweep. It is the single persistence surface
callers should use from any emitter / reconciler / executor call-site.

State machine (Phase 3.1, 2026-05-01)
-------------------------------------

States:

* ``intent``               — brain decided; broker not yet engaged
* ``shadow_logged``        — shadow / dry-run mode; never acted on
* ``confirmed_at_broker``  — broker accepted the SELL stop / target orders
* ``reconciled``           — broker truth matches local intent (steady state)
* ``amending``             — drift detected; repair in flight (replaces the
                             FIX 53 post-place cooldown)
* ``exiting``              — exit order placed; awaiting fill
* ``terminal_reject``      — broker rejected with a non-retryable reason
                             (replaces the FIX 52 in-process cooldown)
* ``closed``               — trade complete (filled or cancelled at broker)

Legal transitions (anything else is rejected by ``transition()``):

* ``intent``               → confirmed_at_broker, shadow_logged, terminal_reject, closed
* ``shadow_logged``        → intent (mode flip), reconciled, closed
* ``confirmed_at_broker``  → reconciled, amending, exiting, terminal_reject, closed
* ``reconciled``           → amending, exiting, terminal_reject, closed
* ``amending``             → confirmed_at_broker, terminal_reject, closed
* ``exiting``              → closed
* ``terminal_reject``      → intent (operator override), closed
* ``closed``               → (terminal; no further transitions)

Legacy ``authoritative_*`` strings (e.g. ``"authoritative_submitted"``,
``"authoritative_reconciled"``) are recognized as synonyms during the
migration window — see ``_legacy_state_alias()``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.bracket_intent_ops_log import (
    format_bracket_intent_ops_line,
)
from .bracket_intent import BracketIntentInput, BracketIntentResult, compute_bracket_intent

logger = logging.getLogger(__name__)


class IntentMode(str, Enum):
    """Live-bracket rollout mode. Values are persisted in
    ``trading_bracket_intents.intent_state`` and referenced by the brain
    config surface (``settings.brain_live_brackets_mode``) — values must
    stay byte-identical (Phase G ops-log contract).

    Subclassing ``str`` keeps the enum JSON-serializable and lets the
    string comparisons in SQL fragments continue to work untouched.
    """

    OFF = "off"
    SHADOW = "shadow"
    COMPARE = "compare"
    AUTHORITATIVE = "authoritative"


class IntentState(str, Enum):
    """Canonical intent_state values (Phase 3.1, 2026-05-01).

    Values are byte-identical with the existing column contents so we
    don't need a migration. The ``LEGACY_*`` synonyms below are
    accepted as aliases until rows are repaired by a follow-up sweep.
    """

    INTENT = "intent"
    SHADOW_LOGGED = "shadow_logged"
    CONFIRMED_AT_BROKER = "confirmed_at_broker"
    RECONCILED = "reconciled"
    AMENDING = "amending"
    EXITING = "exiting"
    TERMINAL_REJECT = "terminal_reject"
    CLOSED = "closed"


# Legacy state strings still found in older rows. Treat them as their
# new-name synonym for transition validation. The rows are not rewritten
# in-place; that's a separate cleanup pass.
_LEGACY_STATE_ALIASES: dict[str, IntentState] = {
    "authoritative": IntentState.CONFIRMED_AT_BROKER,
    "authoritative_submitted": IntentState.CONFIRMED_AT_BROKER,
    "authoritative_reconciled": IntentState.RECONCILED,
    "authoritative_amending": IntentState.AMENDING,
    "authoritative_exiting": IntentState.EXITING,
}


def _coerce_state(raw: str | IntentState | None) -> IntentState | None:
    """Normalize a stored intent_state string to the canonical enum.

    Returns ``None`` if the value is unrecognized (caller decides whether
    that's an error or a no-op).
    """
    if raw is None:
        return None
    if isinstance(raw, IntentState):
        return raw
    s = str(raw).strip().lower()
    if not s:
        return None
    if s in _LEGACY_STATE_ALIASES:
        return _LEGACY_STATE_ALIASES[s]
    try:
        return IntentState(s)
    except ValueError:
        return None


# Phase 3.1 state machine — every entry's value is the set of states
# that are legal transitions FROM the key. Anything not in the set is
# rejected by transition().
_LEGAL_TRANSITIONS: dict[IntentState, frozenset[IntentState]] = {
    IntentState.INTENT: frozenset({
        IntentState.CONFIRMED_AT_BROKER,
        IntentState.SHADOW_LOGGED,
        IntentState.TERMINAL_REJECT,
        IntentState.CLOSED,
        IntentState.RECONCILED,  # broker already has matching orders
    }),
    IntentState.SHADOW_LOGGED: frozenset({
        IntentState.INTENT,        # mode flip from shadow → live
        IntentState.RECONCILED,    # broker truth already matches the shadow intent
        IntentState.CLOSED,
    }),
    IntentState.CONFIRMED_AT_BROKER: frozenset({
        IntentState.RECONCILED,
        IntentState.AMENDING,
        IntentState.EXITING,
        IntentState.TERMINAL_REJECT,
        IntentState.CLOSED,
    }),
    IntentState.RECONCILED: frozenset({
        IntentState.AMENDING,
        IntentState.EXITING,
        IntentState.TERMINAL_REJECT,
        IntentState.CLOSED,
    }),
    IntentState.AMENDING: frozenset({
        IntentState.CONFIRMED_AT_BROKER,
        IntentState.RECONCILED,
        IntentState.EXITING,
        IntentState.TERMINAL_REJECT,
        IntentState.CLOSED,
    }),
    IntentState.EXITING: frozenset({
        IntentState.CLOSED,
        IntentState.RECONCILED,  # exit cancelled, position still resting
    }),
    IntentState.TERMINAL_REJECT: frozenset({
        IntentState.INTENT,        # operator manually fixed the underlying issue
        IntentState.CLOSED,
    }),
    IntentState.CLOSED: frozenset(),  # terminal
}


@dataclass(frozen=True)
class TransitionResult:
    """What ``transition()`` returns. ``ok=False`` is structured rejection
    (state didn't move), not an exception. Callers log and move on."""

    ok: bool
    intent_id: int
    prev_state: IntentState | None
    new_state: IntentState | None
    reason: str  # 'ok' | 'illegal_transition' | 'no_such_intent' | 'unrecognized_state'


def is_terminal_state(state: IntentState | str | None) -> bool:
    s = _coerce_state(state)
    return s is IntentState.CLOSED


def transition(
    db: Session,
    intent_id: int,
    *,
    to_state: IntentState | str,
    reason: str = "",
    expected_from: IntentState | str | list | None = None,
) -> TransitionResult:
    """Move an intent row from its current state to ``to_state`` if the
    transition is legal. The single source of truth for intent_state
    mutation.

    Parameters
    ----------
    db : Session
        SQLAlchemy session. Caller controls commit boundary; this helper
        does NOT commit (so a sweep can batch many transitions).
    intent_id : int
        bracket_intent row id.
    to_state : IntentState | str
        Desired new state.
    reason : str
        Free-form audit reason; persisted in ``last_diff_reason``.
    expected_from : IntentState | list | None
        Optional precondition. If set, the transition is rejected when
        the current state isn't in this set. Useful when a caller's
        decision was based on an assumed state — protects against TOCTOU
        races (the row was mutated between the caller's read and our
        write).

    Returns a ``TransitionResult`` describing the outcome.
    """
    target = _coerce_state(to_state)
    if target is None:
        return TransitionResult(False, intent_id, None, None, "unrecognized_state")

    # Read current state. We use SELECT FOR UPDATE so concurrent calls
    # serialize at the row level — the second caller sees the post-
    # transition state and either takes its own legal path or rejects.
    row = db.execute(text(
        "SELECT intent_state FROM trading_bracket_intents WHERE id = :id "
        "FOR UPDATE"
    ), {"id": int(intent_id)}).fetchone()

    if row is None:
        return TransitionResult(False, intent_id, None, target, "no_such_intent")

    prev = _coerce_state(row[0])
    if prev is None:
        return TransitionResult(False, intent_id, None, target, "unrecognized_state")

    # expected_from check (caller's precondition).
    if expected_from is not None:
        if isinstance(expected_from, (list, tuple, set, frozenset)):
            allowed_from = {_coerce_state(s) for s in expected_from}
        else:
            allowed_from = {_coerce_state(expected_from)}
        allowed_from.discard(None)
        if prev not in allowed_from:
            return TransitionResult(False, intent_id, prev, target, "illegal_transition")

    # State-machine check.
    if target == prev:
        # Idempotent: requesting the current state is a no-op success.
        return TransitionResult(True, intent_id, prev, target, "ok")
    if target not in _LEGAL_TRANSITIONS.get(prev, frozenset()):
        return TransitionResult(False, intent_id, prev, target, "illegal_transition")

    db.execute(text(
        "UPDATE trading_bracket_intents "
        "SET intent_state = :new_state, "
        "    last_diff_reason = COALESCE(:reason, last_diff_reason), "
        "    last_observed_at = NOW(), "
        "    updated_at = NOW() "
        "WHERE id = :id"
    ), {
        "id": int(intent_id),
        "new_state": target.value,
        "reason": (reason or "")[:128] or None,
    })

    if _ops_log_enabled():
        logger.info(
            format_bracket_intent_ops_line(
                event="state_transition",
                mode=_effective_mode().value,
                bracket_intent_id=intent_id,
                reason=(reason or "")[:128],
                from_state=prev.value,
                to_state=target.value,
            )
        )

    return TransitionResult(True, intent_id, prev, target, "ok")


# Prefix used in persisted ``intent_state`` column values. When the live
# bracket writer lands the Phase-G.2 state, it will write strings like
# ``"authoritative_submitted"``, ``"authoritative_reconciled"``, etc. —
# anything starting with this prefix must be protected from shadow-mode
# overwrite.
_AUTHORITATIVE_STATE_PREFIX = IntentMode.AUTHORITATIVE.value

# Modes that emit a shadow/compare dual-write; neither is authoritative.
_SHADOWING_MODES: frozenset[IntentMode] = frozenset({IntentMode.SHADOW, IntentMode.COMPARE})


def _effective_mode(override: str | None = None) -> IntentMode:
    raw = (
        override
        or getattr(settings, "brain_live_brackets_mode", IntentMode.OFF.value)
        or IntentMode.OFF.value
    ).lower()
    try:
        return IntentMode(raw)
    except ValueError:
        return IntentMode.OFF


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) is not IntentMode.OFF


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
    if mode is IntentMode.OFF:
        return None

    if bracket_result is None:
        bracket_result = compute_bracket_intent(bracket_input)

    initial_state_str = "shadow_logged" if mode is IntentMode.SHADOW else "intent"
    shadow_flag = mode in _SHADOWING_MODES

    existing = db.execute(text("""
        SELECT id, intent_state FROM trading_bracket_intents WHERE trade_id = :tid
    """), {"tid": int(trade_id)}).fetchone()

    # Phase 3.1 (2026-05-01): if the row already exists in a closed or
    # legacy-authoritative state, the brain has nothing to do — the trade
    # is over (or under Phase G frozen-authority protection). Skip the
    # whole write rather than refreshing stale metadata.
    if existing:
        existing_state_raw = existing[1] or ""
        existing_state = _coerce_state(existing_state_raw)
        is_terminal = existing_state is IntentState.CLOSED
        is_legacy_authoritative = existing_state_raw.startswith(_AUTHORITATIVE_STATE_PREFIX)
        if is_terminal or is_legacy_authoritative:
            if _ops_log_enabled():
                logger.info(
                    format_bracket_intent_ops_line(
                        event="intent_write_skipped",
                        mode=mode.value,
                        trade_id=trade_id,
                        bracket_intent_id=int(existing[0]),
                        ticker=bracket_input.ticker,
                        intent_state=existing_state_raw,
                        reason="state_machine_protected",
                    )
                )
            return UpsertResult(
                intent_id=int(existing[0]),
                created=False,
                state=existing_state_raw,
                stop_price=bracket_result.stop_price,
                target_price=bracket_result.target_price,
            )

    payload_json: dict[str, Any] = {
        "reasoning": bracket_result.reasoning,
        "brain_summary": bracket_result.brain_summary,
        "stop_model_resolved": bracket_result.stop_model_resolved,
    }

    # f-position-identity-phase-3 (mig 249, 2026-05-18): double-write
    # position_id alongside trade_id. Reuses the same resolver Phase 2
    # uses in execution_audit.record_execution_event. NEVER raises;
    # resolution misses become position_id=NULL. NO READER consults
    # position_id in Phase 3 -- this is foundation for Phase 4's
    # inverse-reconcile rewrite.
    position_id_resolved: int | None = None
    try:
        from .position_resolver import resolve_position_id
        position_id_resolved = resolve_position_id(
            db,
            trade=None,
            user_id=user_id,
            ticker=bracket_input.ticker,
            broker_source=broker_source,
            direction=bracket_input.direction,
        )
    except Exception:
        # Belt-and-suspenders: resolver swallows internally, but never
        # let an import-time issue (e.g., circular) break the writer.
        position_id_resolved = None

    now = datetime.utcnow()
    if existing is None:
        row = db.execute(text("""
            INSERT INTO trading_bracket_intents (
                trade_id, position_id, user_id, ticker, direction, quantity, entry_price,
                stop_price, target_price, stop_model, pattern_id, regime,
                intent_state, shadow_mode, broker_source,
                payload_json, created_at, updated_at
            ) VALUES (
                :trade_id, :position_id, :user_id, :ticker, :direction, :quantity, :entry_price,
                :stop_price, :target_price, :stop_model, :pattern_id, :regime,
                :intent_state, :shadow_mode, :broker_source,
                CAST(:payload_json AS JSONB), :now, :now
            )
            RETURNING id
        """), {
            "trade_id": int(trade_id),
            "position_id": position_id_resolved,
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
            "intent_state": initial_state_str,
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
        # Phase 3.1 (2026-05-01): UPDATE never touches intent_state. The
        # state machine in transition() owns state transitions exclusively.
        # Upsert refreshes the brain's computed metadata (stop_price, target,
        # regime, payload_json) — the row's state is whatever it was.
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
            "shadow_mode": shadow_flag,
            "broker_source": broker_source,
            "payload_json": _json_dumps(payload_json),
            "now": now,
        })
    db.commit()

    # Phase 3.1: when updating an existing row, the state was preserved —
    # report whichever state we were in. On insert, the state is the
    # initial-state string we just wrote.
    state_for_log = (
        initial_state_str
        if created
        else (existing[1] if existing else initial_state_str)
    )

    if _ops_log_enabled():
        logger.info(
            format_bracket_intent_ops_line(
                event="intent_write",
                mode=mode.value,
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
                intent_state=state_for_log,
                broker_source=broker_source,
            )
        )

    return UpsertResult(
        intent_id=new_id,
        created=created,
        state=state_for_log,
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

    Phase 3.1 (2026-05-01): backed by ``transition()`` so illegal
    transitions (e.g. closed → reconciled) are rejected and logged
    instead of silently overwriting state.
    """
    mode = _effective_mode(mode_override)
    if mode is IntentMode.OFF:
        return False

    raw_state = db.execute(text(
        "SELECT intent_state FROM trading_bracket_intents WHERE id = :id"
    ), {"id": int(intent_id)}).scalar()
    if isinstance(raw_state, str) and raw_state.startswith(_AUTHORITATIVE_STATE_PREFIX):
        return False

    result = transition(
        db,
        int(intent_id),
        to_state=IntentState.RECONCILED,
        reason=reason or "mark_reconciled",
    )
    if result.ok and result.prev_state == result.new_state:
        # Idempotent reconciled sweeps still need to clear stale error labels
        # such as "missing_stop:error" after the broker/local view agrees again.
        db.execute(text(
            "UPDATE trading_bracket_intents "
            "SET last_diff_reason = COALESCE(:reason, last_diff_reason), "
            "    last_observed_at = NOW(), "
            "    updated_at = NOW() "
            "WHERE id = :id"
        ), {
            "id": int(intent_id),
            "reason": (reason or "mark_reconciled")[:128] or None,
        })
    if result.ok:
        db.commit()
    return bool(result.ok)


def mark_terminal_reject(
    db: Session,
    intent_id: int,
    *,
    reason: str,
    mode_override: str | None = None,
) -> bool:
    """Phase 3.1 — replace the in-process FIX 52 cooldown with a real state.

    When the broker repeatedly rejects with a non-retryable reason
    ("Not enough shares to sell.", instrument-suspended, etc.), the
    executor calls this. The reconciler then SKIPS the intent on
    subsequent sweeps (no more retries, no more notifications) until
    an operator manually transitions it back to ``intent`` after fixing
    the underlying issue (e.g. cancelling a covering limit-sell).

    Returns True when the row transitioned. False on illegal transition
    (e.g. the row is already closed).
    """
    mode = _effective_mode(mode_override)
    if mode is IntentMode.OFF:
        return False
    result = transition(
        db,
        int(intent_id),
        to_state=IntentState.TERMINAL_REJECT,
        reason=reason or "terminal_reject",
    )
    if result.ok:
        db.commit()
    return bool(result.ok)


def mark_closed(
    db: Session,
    intent_id: int,
    *,
    reason: str,
    mode_override: str | None = None,
) -> bool:
    """Move an intent to terminal ``closed`` state.

    Called when the underlying Trade has fully exited (filled stop /
    target / manual close). After this, the intent never moves again.
    """
    mode = _effective_mode(mode_override)
    if mode is IntentMode.OFF:
        return False
    result = transition(
        db,
        int(intent_id),
        to_state=IntentState.CLOSED,
        reason=reason or "mark_closed",
    )
    if result.ok:
        db.commit()
    return bool(result.ok)


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


# ── bracket-intent-stale-label-cleanup (2026-05-03) ────────────────────
# Two writers added to support the sweep-loop mirror + auto-transition.
# Both gated upstream by ``chili_bracket_intent_mirror_enabled``; the
# writers themselves are unconditional helpers (the gate lives in the
# reconciler so tests can drive the writers directly).


def sync_broker_stop_order_id_mirror(
    db: Session,
    intent_id: int,
    *,
    broker_value: str | None,
) -> tuple[bool, str | None]:
    """Mirror ``BrokerView.stop_order_id`` into the local
    ``trading_bracket_intents.broker_stop_order_id`` column.

    The local column is an **advisory cache**, not authority.
    Decision-time consumers (``_invoke_writer_for_decision`` and any
    caller it reaches) MUST continue to read from ``BrokerView``; this
    mirror exists so external readers (admin UI, audit queries,
    debugging) see broker truth without re-querying the broker.

    Returns ``(changed, prev_value)`` where ``changed`` is True only
    when the column actually moved. No-ops short-circuit (no UPDATE,
    no ``updated_at`` bump) so quiet sweeps don't churn the timestamp.

    Caller controls commit boundary; this helper does NOT commit.
    """
    row = db.execute(text(
        "SELECT broker_stop_order_id FROM trading_bracket_intents WHERE id = :id"
    ), {"id": int(intent_id)}).fetchone()
    if row is None:
        return False, None
    prev = row[0]
    # Normalize: empty string == None for change detection.
    prev_norm = prev if prev else None
    new_norm = broker_value if broker_value else None
    if prev_norm == new_norm:
        return False, prev_norm
    db.execute(text(
        "UPDATE trading_bracket_intents "
        "SET broker_stop_order_id = :v, updated_at = NOW() "
        "WHERE id = :id"
    ), {"id": int(intent_id), "v": new_norm})
    return True, prev_norm


def sync_bracket_intent_stop_from_trade(
    db: Session,
    trade_id: int,
    *,
    trade_stop_loss: float | None,
) -> tuple[bool, float | None]:
    """Mirror ``trade.stop_loss`` into the local advisory cache
    ``trading_bracket_intents.stop_price`` for the matching open intent.

    The local column is **advisory cache**, not authority. Decision-time
    consumers MUST read the actual source of truth: ``trade.stop_loss``
    (engine view) or ``BrokerView`` (broker truth). This mirror exists
    so ``place_missing_stop`` can read the brain's current stop view at
    placement time without a fresh engine evaluation, and so audit /
    admin queries see broker-aligned values.

    Behavior:
      * Returns ``(False, prev)`` when ``trade_stop_loss`` is ``None``
        or non-positive (writer guard mirrors ``_maybe_emit_bracket_intent``).
      * Skips ``CLOSED`` and ``authoritative_*``-prefixed states (Phase
        G.2 frozen-authority contract). Returns ``(False, prev)``.
      * No-ops when local already matches (within float tolerance). The
        ``UPDATE`` does not run — ``updated_at`` stays untouched.
      * Otherwise issues an UPDATE setting ``stop_price`` and
        ``updated_at``. Does NOT touch ``intent_state`` (state machine
        is owned by ``transition()``).

    Caller controls commit boundary; this helper does NOT commit.
    Returns ``(changed, prev_value)``.

    bracket-intent-stop-price-live-sync (2026-05-03).
    """
    if trade_stop_loss is None:
        return False, None
    try:
        new_value = float(trade_stop_loss)
    except (TypeError, ValueError):
        return False, None
    if new_value <= 0:
        return False, None

    row = db.execute(text(
        "SELECT id, intent_state, stop_price "
        "FROM trading_bracket_intents WHERE trade_id = :tid"
    ), {"tid": int(trade_id)}).fetchone()
    if row is None:
        return False, None

    intent_id_row = int(row[0])
    raw_state = (row[1] or "")
    prev_stop = float(row[2]) if row[2] is not None else None

    coerced = _coerce_state(raw_state)
    if coerced is IntentState.CLOSED:
        return False, prev_stop
    if raw_state.startswith(_AUTHORITATIVE_STATE_PREFIX):
        return False, prev_stop

    # Idempotent: skip the UPDATE when values match within float tolerance.
    if prev_stop is not None and abs(prev_stop - new_value) <= 1e-9:
        return False, prev_stop

    db.execute(text(
        "UPDATE trading_bracket_intents "
        "SET stop_price = :v, updated_at = NOW() "
        "WHERE id = :id"
    ), {"id": intent_id_row, "v": new_value})
    return True, prev_stop


def mark_auto_reconciled_after_terminal_reject(
    db: Session,
    intent_id: int,
) -> bool:
    """Explicit ``terminal_reject → reconciled`` transition for the case
    where broker truth subsequently agrees with our local view on a
    sweep.

    The standard ``transition()`` state machine does NOT permit this
    move (terminal_reject's only legal exits are INTENT — manual fix —
    or CLOSED). That gate is correct for the cooldown semantics
    introduced by FIX 52: a non-retryable rejection should not silently
    self-heal. But when the broker is actually reporting a working stop
    that matches our intent, the rejection is moot and the intent is
    cosmetically stuck.

    This writer is the reviewable, audited bypass: a raw UPDATE with
    ``WHERE intent_state = 'terminal_reject'`` precondition. Idempotent
    by construction — a row already in any other state is unaffected.

    Caller controls commit boundary; this helper does NOT commit.
    """
    result = db.execute(text(
        "UPDATE trading_bracket_intents "
        "SET intent_state = 'reconciled', "
        "    last_diff_reason = 'auto_reconciled_after_terminal_reject', "
        "    last_observed_at = NOW(), "
        "    updated_at = NOW() "
        "WHERE id = :id "
        "  AND intent_state = 'terminal_reject'"
    ), {"id": int(intent_id)})
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
    "IntentMode",
    "IntentState",
    "TransitionResult",
    "UpsertResult",
    "bracket_intent_summary",
    "bump_last_observed",
    "is_terminal_state",
    "mark_auto_reconciled_after_terminal_reject",
    "mark_closed",
    "mark_reconciled",
    "mark_terminal_reject",
    "mode_is_active",
    "sync_bracket_intent_stop_from_trade",
    "sync_broker_stop_order_id_mirror",
    "transition",
    "upsert_bracket_intent",
]
