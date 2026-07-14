"""Query / view-model helpers for momentum automation monitor (Phase 5 — no runner)."""

from __future__ import annotations

import logging
import math
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, inspect as sa_inspect
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import (
    BrainBatchJob,
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationEvent,
    TradingAutomationRuntimeSnapshot,
    TradingAutomationSession,
    TradingAutomationSessionBinding,
    TradingAutomationSimulatedFill,
)
from ..brain_batch_job_log import brain_batch_job_record_completed
from ..batch_job_constants import JOB_SCHEDULER_WORKER_HEARTBEAT
from ..brain_neural_mesh.schema import mesh_enabled
from ..execution_family_registry import (
    EXECUTION_FAMILY_COINBASE_SPOT,
    EXECUTION_FAMILY_ROBINHOOD_SPOT,
    normalize_execution_family,
)
from ..execution_robustness import merge_repeatable_edge_robustness_into_readiness
from ..governance import get_kill_switch_status
from ..venue.account_identity import (
    NON_ALPACA_ACCOUNT_IDENTITY_KEY,
    frozen_non_alpaca_account_identity,
    verify_frozen_non_alpaca_account_identity,
)
from .operator_actions import (
    STATE_ARMED_PENDING_RUNNER,
    STATE_DRAFT,
    STATE_LIVE_ARM_PENDING,
    STATE_QUEUED,
    _alpaca_execution_quarantine_reason,
    _lock_live_symbol_arm,
)
from .db_read_hygiene import detach_loaded_instances, end_read_only_transaction
from .paper_fsm import (
    PAPER_RUNNER_RUNNABLE_STATES,
    PAPER_RUNNER_TERMINAL_STATES,
    STATE_BAILOUT,
    STATE_COOLDOWN,
    STATE_CANCELLED,
    STATE_ENTERED,
    STATE_ENTRY_CANDIDATE,
    STATE_ERROR,
    STATE_EXITED,
    STATE_EXPIRED,
    STATE_FINISHED,
    STATE_PENDING_ENTRY,
    STATE_SCALING_OUT,
    STATE_TRAILING,
    STATE_WATCHING,
)

logger = logging.getLogger(__name__)
from .live_fsm import (
    LIVE_CANCELLABLE_STATES,
    LIVE_RUNNER_ACTIVE_SUMMARY_STATES,
    LIVE_RUNNER_RUNNABLE_STATES,
    LIVE_RUNNER_TERMINAL_STATES,
    STATE_LIVE_BAILOUT,
    STATE_LIVE_CANCELLED,
    STATE_LIVE_COOLDOWN,
    STATE_LIVE_ENTERED,
    STATE_LIVE_ENTRY_CANDIDATE,
    STATE_LIVE_ERROR,
    STATE_LIVE_EXITED,
    STATE_LIVE_FINISHED,
    STATE_LIVE_PENDING_ENTRY,
    STATE_LIVE_SCALING_OUT,
    STATE_LIVE_TRAILING,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
)
from .live_runner import (
    _is_exact_pre_http_alpaca_arm_claim,
    _retire_confirmed_pre_http_alpaca_claim_before_terminal,
    summarize_live_execution,
    tick_live_session,
)
from .paper_runner import summarize_paper_execution, tick_paper_session
from .market_profile import asset_class_for_symbol, market_open_now
from .risk_evaluator import summarize_risk_from_snapshot
from .risk_policy import effective_policy_summary
from .operator_readiness import (
    blocked_reason_for_session,
    build_momentum_operator_readiness,
    next_action_required,
)
from .session_lifecycle import (
    apply_operator_pause,
    canonical_operator_state,
    clear_operator_pause,
    is_armed_only_live,
    is_live_orders_active,
    is_operator_paused,
    operator_pause_info,
    phase_hint,
)
from .persistence import (
    KEY_LIVE_EXEC,
    append_trading_automation_event,
    build_runtime_snapshot_values,
    create_trading_automation_session,
    default_session_binding,
)
from .strategy_params import summarize_strategy_params
from .viability_health import get_viability_pipeline_health

_log = logging.getLogger(__name__)

STATE_IDLE = "idle"
STATE_CANCELLED = "cancelled"
STATE_ARCHIVED = "archived"
STATE_EXPIRED = "expired"

# Paper runner + pre-run: operator may cancel before terminal completion.
CANCELLABLE_STATES = frozenset(
    {
        STATE_DRAFT,
        STATE_QUEUED,
        STATE_LIVE_ARM_PENDING,
        STATE_ARMED_PENDING_RUNNER,
        STATE_IDLE,
        STATE_WATCHING,
        STATE_ENTRY_CANDIDATE,
        STATE_PENDING_ENTRY,
        STATE_ENTERED,
        STATE_SCALING_OUT,
        STATE_TRAILING,
        STATE_BAILOUT,
        STATE_EXITED,
        STATE_COOLDOWN,
    }
) | frozenset(LIVE_CANCELLABLE_STATES)

# Terminal-ish rows the operator may archive (hide from default list).
ARCHIVABLE_STATES = frozenset(
    {
        STATE_CANCELLED,
        STATE_EXPIRED,
        STATE_DRAFT,
        STATE_FINISHED,
        STATE_ERROR,
        STATE_LIVE_FINISHED,
        STATE_LIVE_CANCELLED,
        STATE_LIVE_ERROR,
    }
)

PAPER_RUNNER_ACTIVE_STATES = frozenset(
    {
        STATE_WATCHING,
        STATE_ENTRY_CANDIDATE,
        STATE_PENDING_ENTRY,
        STATE_ENTERED,
        STATE_SCALING_OUT,
        STATE_TRAILING,
        STATE_BAILOUT,
    }
)

LIMITATIONS_NOTE = (
    "Paper runner is simulated (CHILI_MOMENTUM_PAPER_RUNNER_ENABLED). "
    "Live runner places real orders only for the implemented execution_family (coinbase_spot today) "
    "when CHILI_MOMENTUM_LIVE_RUNNER_ENABLED — use with care."
)


def _rh_venue_unavailable_detail(execution_family: str | None) -> dict[str, Any]:
    """STEP-D #14 telemetry: for the RH Agentic rail, the reason it is currently dark
    (auth error / transport reason / remaining outage latch). Empty for other families or
    when the rail is healthy. Best-effort — never raises into the caller."""
    try:
        if str(execution_family or "") != "robinhood_agentic_mcp":
            return {}
        from ..venue.robinhood_mcp import venue_unavailable_detail

        return venue_unavailable_detail() or {}
    except Exception:
        return {}


def _tables_present(db: Session) -> bool:
    try:
        bind = db.get_bind()
        names = set(sa_inspect(bind).get_table_names())
    except Exception:
        return False
    return "trading_automation_sessions" in names


def _table_exists(db: Session, name: str) -> bool:
    try:
        bind = db.get_bind()
        return name in set(sa_inspect(bind).get_table_names())
    except Exception:
        return False


def _parse_expires(snap: dict[str, Any]) -> Optional[datetime]:
    raw = snap.get("expires_at_utc")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _frozen_alpaca_account_scope(
    sess: TradingAutomationSession,
) -> str | None:
    if normalize_execution_family(sess.execution_family) not in {
        "alpaca_spot",
        "alpaca_short",
    }:
        return None
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    scope = str(snap.get("alpaca_account_scope") or "").strip().lower()
    return scope or None


def _frozen_alpaca_account_id(
    sess: TradingAutomationSession,
) -> str | None:
    """Return the non-secret account UUID frozen with this execution generation."""
    if normalize_execution_family(sess.execution_family) not in {
        "alpaca_spot",
        "alpaca_short",
    }:
        return None
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    account_id = str(snap.get("alpaca_account_id") or "").strip()
    return account_id or None


def _bind_persisted_alpaca_adapter(
    sess: TradingAutomationSession,
    adapter: Any,
) -> bool:
    """Bind an adapter instance to the frozen session account before trading I/O."""
    if normalize_execution_family(sess.execution_family) not in {
        "alpaca_spot",
        "alpaca_short",
    }:
        return True
    bind_account = getattr(adapter, "bind_account_id", None)
    frozen_account_id = _frozen_alpaca_account_id(sess)
    return bool(
        callable(bind_account)
        and frozen_account_id
        and bind_account(frozen_account_id) is True
    )


def _persisted_alpaca_execution_quarantine_reason(
    sess: TradingAutomationSession,
) -> str | None:
    reason = _alpaca_execution_quarantine_reason(
        sess.execution_family,
        sess.symbol,
    )
    if reason is not None:
        return reason
    if normalize_execution_family(sess.execution_family) in {
        "alpaca_spot",
        "alpaca_short",
    }:
        if _frozen_alpaca_account_scope(sess) != "alpaca:paper":
            return "alpaca_account_scope_unfrozen_or_mismatched"
        # The adapter independently verifies that the active credentials still
        # resolve to this configured UUID.  This local generation check is also
        # required before reading/cancelling broker state or terminalizing an old
        # row: a newly configured account must never inherit cleanup authority over
        # a session frozen under another (or unknown) account.
        expected_account_id = str(
            getattr(settings, "chili_alpaca_expected_account_id", "") or ""
        ).strip()
        if not expected_account_id:
            return "alpaca_expected_account_id_unconfigured"
        if _frozen_alpaca_account_id(sess) != expected_account_id:
            return "alpaca_account_generation_mismatch"
    return None


def _quarantine_persisted_alpaca_execution(
    db: Session,
    sess: TradingAutomationSession,
    *,
    reason: str,
    context: str,
) -> None:
    """Persist a zero-broker-call quarantine for one unsupported stored row."""
    snap = dict(sess.risk_snapshot_json or {})
    le = dict(snap.get(KEY_LIVE_EXEC) or {})
    quarantine = {
        "reason": str(reason),
        "context": str(context),
        "execution_family": normalize_execution_family(sess.execution_family),
        "symbol": str(sess.symbol or "").strip().upper(),
    }
    prior = le.get("alpaca_execution_quarantine")
    if isinstance(prior, dict) and all(prior.get(k) == v for k, v in quarantine.items()):
        return
    quarantine["quarantined_at_utc"] = datetime.utcnow().isoformat()
    le["alpaca_execution_quarantine"] = quarantine
    snap[KEY_LIVE_EXEC] = le
    sess.risk_snapshot_json = snap
    sess.updated_at = datetime.utcnow()
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(sess, "risk_snapshot_json")
    except Exception:
        pass
    append_trading_automation_event(
        db,
        int(sess.id),
        "alpaca_execution_quarantined",
        quarantine,
        correlation_id=sess.correlation_id,
        source_node_id="momentum_automation_monitor",
    )


def _stale_arm_claim_allows_terminalization(
    db: Session,
    sess: TradingAutomationSession,
) -> bool:
    """Resolve a pre-HTTP Alpaca arm permit in its frozen paper scope."""
    if normalize_execution_family(sess.execution_family) not in {
        "alpaca_spot",
        "alpaca_short",
    }:
        return True
    if _persisted_alpaca_execution_quarantine_reason(sess) is not None:
        return False
    scope = _frozen_alpaca_account_scope(sess)
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    token = str(snap.get("alpaca_symbol_claim_token") or "").strip()
    from .alpaca_orphan_claims import read_action_claim, resolve_action_claim

    readable, claim = read_action_claim(
        db,
        symbol=sess.symbol,
        account_scope=scope,
    )
    if not readable:
        return False
    if claim is None or claim.get("phase") == "resolved":
        return True
    if (
        not token
        or claim.get("claim_token") != token
        or claim.get("owner_session_id") != int(sess.id)
        or claim.get("action") != "entry"
        or claim.get("client_order_id")
        or claim.get("broker_order_id")
    ):
        return False
    # Re-read the ambient pin immediately before mutating durable claim state.
    if _persisted_alpaca_execution_quarantine_reason(sess) is not None:
        return False
    return bool(resolve_action_claim(
        db,
        symbol=sess.symbol,
        claim_token=token,
        client_order_id=None,
        broker_order_id=None,
        broker_order_status="not_submitted",
        proven_no_transport=True,
        metadata={"reason": "stale_live_arm_expired_before_submit"},
        account_scope=scope,
    ))


def expire_stale_live_arm_sessions(db: Session, *, user_id: int) -> int:
    """Mark expired live_arm_pending rows as ``expired``; returns rows updated."""
    if not _tables_present(db):
        return 0
    now = datetime.utcnow()
    # FALLBACK cutoff for arm_pending rows that carry NO expires_at_utc (begin_live_arm
    # succeeded but confirm never landed — a risk-block or crash mid-arm): without this they
    # linger FOREVER (the multi-hour orphans found 2026-06-22 — never reaped, never expired).
    # Once older than the max a watch would ever live, the row is a definitive orphan (it
    # never progressed past arm). Adaptive — reuses the watch-max setting, no new magic
    # number. [[feedback_adaptive_no_magic]]
    _stale_no_expiry_cutoff_s = max(
        60, int(getattr(settings, "chili_momentum_auto_arm_max_watch_seconds", 1800) or 1800)
    )
    rows = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.user_id == user_id,
            TradingAutomationSession.state == STATE_LIVE_ARM_PENDING,
        )
        .all()
    )
    n = 0
    for candidate in rows:
        # Use the same per-user/symbol transaction fence as confirm_live_arm.
        # The first read above is only a cheap candidate scan; all claim reads and
        # terminal mutations happen after this lock and a fresh row lock.
        if not _lock_live_symbol_arm(
            db,
            user_id=int(user_id),
            symbol=candidate.symbol,
        ):
            continue
        locked_q = db.query(TradingAutomationSession).filter(
            TradingAutomationSession.id == int(candidate.id),
            TradingAutomationSession.user_id == int(user_id),
        )
        try:
            locked_q = locked_q.with_for_update()
        except Exception:
            pass
        try:
            locked_q = locked_q.populate_existing()
        except Exception:
            pass
        sess = locked_q.one_or_none()
        if sess is None or sess.state != STATE_LIVE_ARM_PENDING:
            continue

        # Recompute expiry from the locked generation.  A concurrent confirmer
        # may have replaced or advanced the candidate while we waited.
        snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
        exp = _parse_expires(snap)
        if exp is not None:
            if now <= exp:
                continue
            _reason = "expires_at_utc_passed"
        else:
            _created = sess.started_at or getattr(sess, "created_at", None) or sess.updated_at
            if _created is None or (now - _created).total_seconds() <= _stale_no_expiry_cutoff_s:
                continue
            _reason = "stale_arm_no_expiry"
        execution_quarantine = _persisted_alpaca_execution_quarantine_reason(sess)
        if execution_quarantine is not None:
            _quarantine_persisted_alpaca_execution(
                db,
                sess,
                reason=execution_quarantine,
                context="stale_live_arm_expiry",
            )
            continue
        # Claim inspection/resolution is now inside the shared generation fence.
        if not _stale_arm_claim_allows_terminalization(db, sess):
            # Missing/mismatched frozen scope, claim identity, or broker-side
            # evidence keeps the arm non-terminal for explicit reconciliation.
            continue
        # A configuration reload may have changed the pin while the durable claim
        # was being inspected.  Terminalization needs the same current generation.
        execution_quarantine = _persisted_alpaca_execution_quarantine_reason(sess)
        if execution_quarantine is not None:
            _quarantine_persisted_alpaca_execution(
                db,
                sess,
                reason=execution_quarantine,
                context="stale_live_arm_pre_terminal",
            )
            continue
        # Final CAS under the row lock: never overwrite a generation that was
        # advanced by confirmation or another expiry worker.
        if sess.state != STATE_LIVE_ARM_PENDING:
            continue
        sess.state = STATE_EXPIRED
        sess.ended_at = now
        sess.updated_at = now
        from .persistence import append_trading_automation_event

        append_trading_automation_event(
            db,
            sess.id,
            "live_arm_expired",
            {"reason": _reason, "arm_token_prefix": str(snap.get("arm_token", ""))[:8]},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_automation_monitor",
        )
        n += 1
        try:
            from .feedback_emit import emit_feedback_after_terminal_transition

            emit_feedback_after_terminal_transition(db, sess)
        except Exception:
            pass
    return n


def _reaper_expected_side_long(
    sess: TradingAutomationSession,
) -> tuple[bool, str | None]:
    """Return the frozen direction, or a session/family identity-drift reason."""
    fam = normalize_execution_family(sess.execution_family)
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    le = snap.get(KEY_LIVE_EXEC) if isinstance(snap, dict) else None
    le = le if isinstance(le, dict) else {}
    explicit = le.get("side_long") if "side_long" in le else None
    if fam == "alpaca_short":
        if explicit is not None and explicit is not False:
            return False, "alpaca_short_direction_metadata_mismatch"
        return False, None
    if fam == "alpaca_spot":
        if explicit is False:
            return True, "alpaca_spot_direction_metadata_mismatch"
        return True, None
    return explicit is not False, None


def _normalize_reaper_position_quantity(
    sess: TradingAutomationSession,
    quantity: Any,
) -> tuple[Optional[bool], dict[str, Any]]:
    """Classify signed broker quantity without folding wrong-way exposure to flat."""
    try:
        qty = float(quantity)
    except (TypeError, ValueError):
        return None, {"reason": "broker_position_invalid"}
    if not math.isfinite(qty):
        return None, {"reason": "broker_position_invalid"}

    side_long, metadata_error = _reaper_expected_side_long(sess)
    detail = {
        "broker_quantity": qty,
        "expected_side": "long" if side_long else "short",
    }
    if metadata_error is not None:
        return None, {**detail, "reason": metadata_error}
    if abs(qty) <= 1e-6:
        return True, detail
    if (side_long and qty < 0.0) or (not side_long and qty > 0.0):
        return None, {**detail, "reason": "broker_position_direction_mismatch"}
    return False, detail


def _reaper_broker_position_truth(
    sess: TradingAutomationSession,
) -> tuple[Optional[bool], dict[str, Any]]:
    """AREA C broker-truth gate plus signed-direction quarantine detail.

    Returns:
      * ``True``  — a SUCCESSFUL broker read confirmed this symbol is held at 0 / dust.
      * ``False`` — a SUCCESSFUL broker read found a REAL non-zero position (do NOT reap).
      * ``None``  — UNKNOWN (no adapter / API error / unrecognized payload): the caller
        MUST leave the session alone (never reap on uncertainty).

    Reuses the same per-family reads the live exit clamp trusts: robinhood_agentic_mcp
    and any adapter exposing get_position_quantity (None=unknown / 0=flat / >0=held);
    robinhood_spot via broker_service.get_open_position_quantity; coinbase_spot via the
    momentum balance/dust check. Unhandled family -> None (fail safe)."""
    quarantine_reason = _persisted_alpaca_execution_quarantine_reason(sess)
    if quarantine_reason is not None:
        return None, {
            "reason": quarantine_reason,
            "execution_quarantined": True,
            "broker_calls": 0,
        }
    fam = normalize_execution_family(sess.execution_family)
    sym = sess.symbol
    # Coinbase: balance/dust check (the proven momentum reconcile path).
    if fam == EXECUTION_FAMILY_COINBASE_SPOT:
        try:
            from .live_runner import _broker_balance_confirms_zero

            # _broker_balance_confirms_zero returns True on flat/dust, False on a
            # FAILED fetch OR a real holding — it cannot distinguish the two, so it is
            # only safe to treat True as confirmed-flat. A False is ambiguous -> UNKNOWN.
            from ...coinbase_service import get_accounts_raw

            if not get_accounts_raw():
                return None, {"reason": "broker_position_unknown"}
            return (
                True if _broker_balance_confirms_zero(sym) else False,
                {"reason": "coinbase_balance_truth"},
            )
        except Exception:
            return None, {"reason": "broker_position_unknown"}
    # Robinhood spot: open-position quantity (None=unknown / 0=flat / >0=held).
    if fam == EXECUTION_FAMILY_ROBINHOOD_SPOT:
        try:
            from ...broker_service import get_open_position_quantity

            q = get_open_position_quantity(sym)
        except Exception:
            return None, {"reason": "broker_position_unknown"}
        if q is None:
            return None, {"reason": "broker_position_unknown"}
        return _normalize_reaper_position_quantity(sess, q)
    # Robinhood agentic MCP (the live rail) + any adapter with get_position_quantity.
    try:
        from ..venue.factory import get_adapter

        adapter = get_adapter(sess.execution_family)
    except Exception:
        adapter = None
    if adapter is None or not hasattr(adapter, "get_position_quantity"):
        return None, {"reason": "broker_position_unknown"}
    if not _bind_persisted_alpaca_adapter(sess, adapter):
        return None, {
            "reason": "alpaca_adapter_account_generation_bind_failed",
            "execution_quarantined": True,
            "broker_calls": 0,
        }
    quarantine_reason = _persisted_alpaca_execution_quarantine_reason(sess)
    if quarantine_reason is not None:
        return None, {
            "reason": quarantine_reason,
            "execution_quarantined": True,
            "broker_calls": 0,
        }
    try:
        q = adapter.get_position_quantity(sym)
    except Exception:
        return None, {"reason": "broker_position_unknown"}
    if q is None:
        return None, {"reason": "broker_position_unknown"}
    return _normalize_reaper_position_quantity(sess, q)


def _reaper_broker_confirms_flat(sess: TradingAutomationSession) -> Optional[bool]:
    """Compatibility wrapper for callers that need only the tri-state flat gate."""
    flat, _detail = _reaper_broker_position_truth(sess)
    return flat


def _quarantine_reaper_direction_mismatch(
    db: Session,
    sess: TradingAutomationSession,
    detail: dict[str, Any],
) -> None:
    """Persist one visible quarantine instead of hiding wrong-way exposure as flat."""
    snap = dict(sess.risk_snapshot_json or {})
    le = dict(snap.get(KEY_LIVE_EXEC) or {})
    fingerprint = {
        "reason": detail.get("reason"),
        "broker_quantity": detail.get("broker_quantity"),
        "expected_side": detail.get("expected_side"),
    }
    prior = le.get("stale_reaper_direction_quarantine")
    if isinstance(prior, dict) and all(prior.get(k) == v for k, v in fingerprint.items()):
        return
    quarantine = {
        **fingerprint,
        "quarantined_at_utc": datetime.utcnow().isoformat(),
    }
    le["stale_reaper_direction_quarantine"] = quarantine
    snap[KEY_LIVE_EXEC] = le
    sess.risk_snapshot_json = snap
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(sess, "risk_snapshot_json")
    except Exception:
        pass
    append_trading_automation_event(
        db,
        int(sess.id),
        "stale_session_reaper_quarantined",
        quarantine,
        correlation_id=sess.correlation_id,
        source_node_id="momentum_automation_monitor",
    )


_NON_ALPACA_TERMINAL_ORDER_STATUSES = frozenset({
    "filled",
    "cancelled",
    "canceled",
    "rejected",
    "failed",
    "expired",
    "done",
    "closed",
})

# A broker submit can be accepted before its order becomes visible to every read
# surface.  Identity-loss terminalization therefore needs two identical complete
# broker scans separated by the same conservative late-ack scale used elsewhere;
# two back-to-back calls never constitute stable absence.
_NON_ALPACA_IDENTITY_LOSS_VISIBILITY_GRACE_SECONDS = 30.0


def _collect_non_alpaca_persisted_order_identities(
    sess: TradingAutomationSession,
) -> dict[str, Any]:
    """Collect every persisted broker/client order identity without guessing."""
    snapshot = sess.risk_snapshot_json
    malformed = not isinstance(snapshot, dict)
    live_exec = snapshot.get(KEY_LIVE_EXEC) if isinstance(snapshot, dict) else None
    if live_exec is None:
        live_exec = {}
    elif not isinstance(live_exec, dict):
        malformed = True
        live_exec = {}
    order_ids: list[str] = []
    client_order_ids: list[str] = []
    resolved_order_outcomes: dict[str, str] = {}
    order_expectations: dict[str, dict[str, Any]] = {}
    # These records are observations produced by this proof.  They must never
    # become a new source of broker authority on a later pulse (for example, a
    # quarantined ``detail.order_id`` must not repair otherwise-lost identity).
    audit_only_keys = {
        "non_alpaca_terminalization_quarantine",
        "non_alpaca_terminalization_proof",
        "non_alpaca_identity_loss_observation",
    }

    def _append(target: list[str], raw: Any) -> None:
        nonlocal malformed
        if raw is None:
            return
        if not isinstance(raw, (str, int)):
            malformed = True
            return
        value = str(raw).strip()
        if value and value not in target:
            target.append(value)

    def _walk(value: Any) -> None:
        nonlocal malformed
        if isinstance(value, dict):
            for raw_key, raw_value in value.items():
                key = str(raw_key or "").strip().lower()
                if key in audit_only_keys:
                    continue
                if key.endswith("orders_resolved"):
                    if isinstance(raw_value, dict):
                        for resolved_oid, raw_outcome in raw_value.items():
                            _append(order_ids, resolved_oid)
                            oid = str(resolved_oid or "").strip()
                            outcome = str(raw_outcome or "").strip().lower()
                            if oid and outcome in {"adopted", "void"}:
                                prior = resolved_order_outcomes.get(oid)
                                if prior is not None and prior != outcome:
                                    malformed = True
                                resolved_order_outcomes[oid] = outcome
                            else:
                                malformed = True
                    else:
                        malformed = True
                if "client_order_ids" in key:
                    if isinstance(raw_value, (list, tuple, set)):
                        for item in raw_value:
                            _append(client_order_ids, item)
                    elif raw_value not in (None, ""):
                        malformed = True
                elif "client_order_id" in key:
                    _append(client_order_ids, raw_value)
                elif key == "order_ids" or key.endswith("_order_ids") or key.endswith(
                    "_order_ids_all"
                ):
                    if isinstance(raw_value, (list, tuple, set)):
                        for item in raw_value:
                            _append(order_ids, item)
                    elif raw_value not in (None, ""):
                        malformed = True
                elif (
                    key == "order_id"
                    or key == "broker_order_id"
                    or key.endswith("_order_id")
                    or key.endswith("_broker_order_id")
                ):
                    _append(order_ids, raw_value)
                if isinstance(raw_value, (dict, list, tuple)):
                    _walk(raw_value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, (dict, list, tuple)):
                    _walk(item)

    side_long, direction_error = _reaper_expected_side_long(sess)

    def _expect(
        order_key: str,
        client_key: str,
        quantity_key: str,
        *,
        intent: str,
        side: str,
    ) -> None:
        oid = str(live_exec.get(order_key) or "").strip()
        if not oid:
            return
        cid = str(live_exec.get(client_key) or "").strip()
        try:
            quantity = float(live_exec.get(quantity_key))
        except (TypeError, ValueError):
            quantity = math.nan
        order_expectations[oid] = {
            "intent": intent,
            "side": side if direction_error is None else None,
            "client_order_id": cid or None,
            "quantity": quantity if math.isfinite(quantity) and quantity > 0.0 else None,
        }

    _expect(
        "entry_order_id",
        "entry_client_order_id",
        "entry_want_qty",
        intent="entry",
        side="buy" if side_long else "sell",
    )
    _expect(
        "exit_order_id",
        "exit_client_order_id",
        "pending_exit_quantity",
        intent="exit",
        side="sell" if side_long else "buy",
    )

    _walk(live_exec)
    return {
        "order_ids": order_ids,
        "client_order_ids": client_order_ids,
        "resolved_order_outcomes": resolved_order_outcomes,
        "order_expectations": order_expectations,
        "malformed": malformed,
        "identity_loss": not order_ids and not client_order_ids,
    }


def _non_alpaca_order_total_quantity(order: Any) -> float | None:
    """Return one unambiguous positive submitted quantity from broker truth."""
    raw = getattr(order, "raw", None)
    if not isinstance(raw, dict):
        return None
    values: list[float] = []

    def _walk(value: Any) -> None:
        if not isinstance(value, dict):
            return
        for raw_key, raw_value in value.items():
            key = str(raw_key or "").strip().lower()
            if key in {"quantity", "qty", "base_size", "order_quantity"}:
                try:
                    parsed = float(raw_value)
                except (TypeError, ValueError):
                    parsed = math.nan
                values.append(
                    parsed
                    if math.isfinite(parsed) and parsed > 0.0
                    else math.nan
                )
            elif isinstance(raw_value, dict):
                _walk(raw_value)

    _walk(raw)
    if not values or any(not math.isfinite(value) for value in values):
        return None
    unique: list[float] = []
    for value in values:
        if not any(
            math.isclose(value, prior, rel_tol=1e-9, abs_tol=1e-9)
            for prior in unique
        ):
            unique.append(value)
    return unique[0] if len(unique) == 1 else None


def _exact_non_alpaca_order_authority(
    order: Any,
    *,
    order_expectations: dict[str, dict[str, Any]],
    required_intent: str | None = None,
    expected_symbol: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Match broker truth to the exact persisted OID/CID/side/total quantity."""
    oid = str(getattr(order, "order_id", "") or "").strip()
    expected = order_expectations.get(oid)
    observed_cid = str(
        getattr(order, "client_order_id", "") or ""
    ).strip()
    observed_side = str(getattr(order, "side", "") or "").strip().lower()
    observed_symbol = str(
        getattr(order, "product_id", "") or ""
    ).strip().upper()
    observed_quantity = _non_alpaca_order_total_quantity(order)
    detail = {
        "order_id": oid or None,
        "required_intent": str(required_intent or "").strip().lower() or None,
        "expected": dict(expected or {}),
        "observed_client_order_id": observed_cid or None,
        "observed_side": observed_side or None,
        "observed_symbol": observed_symbol or None,
        "observed_quantity": observed_quantity,
    }
    if not isinstance(expected, dict):
        return False, detail
    expected_cid = str(expected.get("client_order_id") or "").strip()
    expected_side = str(expected.get("side") or "").strip().lower()
    expected_intent = str(expected.get("intent") or "").strip().lower()
    try:
        expected_quantity = float(expected.get("quantity"))
    except (TypeError, ValueError):
        expected_quantity = math.nan
    normalized_required_intent = str(required_intent or "").strip().lower()
    normalized_expected_symbol = str(expected_symbol or "").strip().upper()
    exact = bool(
        oid
        and expected_intent in {"entry", "exit"}
        and (
            not normalized_required_intent
            or expected_intent == normalized_required_intent
        )
        and expected_cid
        and observed_cid == expected_cid
        and expected_side in {"buy", "sell"}
        and observed_side == expected_side
        and (
            not normalized_expected_symbol
            or observed_symbol == normalized_expected_symbol
        )
        and math.isfinite(expected_quantity)
        and expected_quantity > 0.0
        and observed_quantity is not None
        and math.isclose(
            observed_quantity,
            expected_quantity,
            rel_tol=1e-6,
            abs_tol=1e-8,
        )
    )
    return exact, detail


def _canonical_non_alpaca_order_generation(
    identities: dict[str, Any],
) -> dict[str, Any]:
    """Canonicalize every persisted order authority field for proof CAS/fingerprints."""
    expectations = identities.get("order_expectations")
    expectations = expectations if isinstance(expectations, dict) else {}
    expectation_rows: list[tuple[Any, ...]] = []
    for raw_oid, raw_expected in expectations.items():
        expected = raw_expected if isinstance(raw_expected, dict) else {}
        quantity = expected.get("quantity")
        try:
            quantity = float(quantity) if quantity is not None else None
        except (TypeError, ValueError):
            quantity = None
        expectation_rows.append(
            (
                str(raw_oid or "").strip(),
                str(expected.get("intent") or "").strip().lower() or None,
                str(expected.get("client_order_id") or "").strip() or None,
                str(expected.get("side") or "").strip().lower() or None,
                quantity,
            )
        )
    resolved = identities.get("resolved_order_outcomes")
    resolved = resolved if isinstance(resolved, dict) else {}
    return {
        "order_ids": tuple(str(value) for value in identities.get("order_ids") or []),
        "client_order_ids": tuple(
            str(value) for value in identities.get("client_order_ids") or []
        ),
        "order_expectations": tuple(sorted(expectation_rows)),
        "resolved_order_outcomes": tuple(
            sorted(
                (str(oid or "").strip(), str(outcome or "").strip().lower())
                for oid, outcome in resolved.items()
            )
        ),
        "malformed": bool(identities.get("malformed")),
        "identity_loss": bool(identities.get("identity_loss")),
    }


def _json_non_alpaca_order_generation(generation: dict[str, Any]) -> dict[str, Any]:
    """Make the canonical local generation explicit and JSON-safe."""
    return {
        "persisted_order_ids": list(generation.get("order_ids") or ()),
        "persisted_client_order_ids": list(
            generation.get("client_order_ids") or ()
        ),
        "persisted_order_expectations": [
            list(row) for row in generation.get("order_expectations") or ()
        ],
        "persisted_resolved_order_outcomes": [
            list(row) for row in generation.get("resolved_order_outcomes") or ()
        ],
        "malformed_identity_json": bool(generation.get("malformed")),
        "identity_loss": bool(generation.get("identity_loss")),
    }


def _non_alpaca_terminal_generation(
    sess: TradingAutomationSession,
) -> dict[str, Any]:
    """Immutable local authority compared before and after broker I/O."""
    identities = _collect_non_alpaca_persisted_order_identities(sess)
    order_generation = _canonical_non_alpaca_order_generation(identities)
    return {
        "session_id": int(sess.id),
        "mode": str(sess.mode or ""),
        "state": str(sess.state or ""),
        "execution_family": normalize_execution_family(sess.execution_family),
        "symbol": str(sess.symbol or "").strip().upper(),
        "account_identity": frozen_non_alpaca_account_identity(sess),
        **order_generation,
    }


def _non_alpaca_terminal_generation_matches(
    sess: TradingAutomationSession,
    expected: dict[str, Any] | None,
) -> bool:
    return bool(
        isinstance(expected, dict)
        and _non_alpaca_terminal_generation(sess) == expected
    )


def _non_alpaca_terminal_proof_matches_session(
    sess: TradingAutomationSession,
    proof: Any,
) -> bool:
    if not isinstance(proof, dict):
        return False
    current = _non_alpaca_terminal_generation(sess)
    current_order_generation = _json_non_alpaca_order_generation(current)
    return bool(
        proof.get("execution_family") == current["execution_family"]
        and proof.get("symbol") == current["symbol"]
        and proof.get("account_identity") == current["account_identity"]
        and proof.get("session_state") == current["state"]
        and all(
            proof.get(key) == value
            for key, value in current_order_generation.items()
        )
    )


def _persist_non_alpaca_terminalization_quarantine(
    db: Session,
    sess: TradingAutomationSession,
    *,
    reason: str,
    context: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a visible pause/reconcile fence for uncertain terminal truth."""
    snapshot = (
        dict(sess.risk_snapshot_json)
        if isinstance(sess.risk_snapshot_json, dict)
        else {}
    )
    live_exec = snapshot.get(KEY_LIVE_EXEC)
    live_exec = dict(live_exec) if isinstance(live_exec, dict) else {}
    if str(reason) != "terminalization_identity_loss_stability_pending":
        # Any unreadable/changed broker scan invalidates a prior absence timer.
        # Only another complete, identical identity-loss scan may preserve it.
        live_exec.pop("non_alpaca_identity_loss_observation", None)
    fingerprint = {
        "reason": str(reason),
        "context": str(context),
        "execution_family": normalize_execution_family(sess.execution_family),
        "symbol": str(sess.symbol or "").strip().upper(),
    }
    prior = live_exec.get("non_alpaca_terminalization_quarantine")
    changed = not (
        isinstance(prior, dict)
        and all(prior.get(key) == value for key, value in fingerprint.items())
    )
    quarantine = {
        **fingerprint,
        "detail": dict(detail or {}),
        "quarantined_at_utc": (
            datetime.utcnow().isoformat()
            if changed
            else prior.get("quarantined_at_utc")
        ),
    }
    live_exec["non_alpaca_terminalization_quarantine"] = quarantine
    snapshot[KEY_LIVE_EXEC] = live_exec
    sess.risk_snapshot_json = apply_operator_pause(snapshot, state=sess.state)
    sess.updated_at = datetime.utcnow()
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(sess, "risk_snapshot_json")
    except Exception:
        pass
    if changed:
        append_trading_automation_event(
            db,
            int(sess.id),
            "live_terminalization_quarantined",
            quarantine,
            correlation_id=sess.correlation_id,
            source_node_id="momentum_automation_monitor",
        )
    return {
        "ok": True,
        "pending": "broker_terminal_truth_reconcile",
        "terminalization_deferred": True,
        "session_id": int(sess.id),
        "state": sess.state,
        "quarantine_reason": str(reason),
        "terminalization_truth": quarantine,
    }


def _final_non_alpaca_terminal_account_quarantine(
    db: Session,
    sess: TradingAutomationSession,
    *,
    context: str,
) -> dict[str, Any] | None:
    """Fresh account check immediately before a non-Alpaca terminal mutation."""
    family = normalize_execution_family(sess.execution_family)
    if sess.mode != "live" or family in {"alpaca_spot", "alpaca_short"}:
        return None
    account_truth = verify_frozen_non_alpaca_account_identity(sess)
    if account_truth.get("ok") is True:
        return None
    return _persist_non_alpaca_terminalization_quarantine(
        db,
        sess,
        reason=str(
            account_truth.get("reason")
            or "non_alpaca_account_identity_unknown"
        ),
        context=context,
        detail={
            "phase": "immediately_before_terminal_state_mutation",
            "frozen_identity": account_truth.get("frozen_identity"),
            "current_identity": account_truth.get("current_identity"),
        },
    )


def _strict_non_alpaca_order_truth(adapter: Any, order_id: str) -> dict[str, Any]:
    getter = getattr(adapter, "get_order_truth", None)
    if not callable(getter):
        return {"readable": False, "found": False, "order": None}
    try:
        truth = getter(str(order_id))
    except Exception:
        return {"readable": False, "found": False, "order": None}
    if not isinstance(truth, dict) or truth.get("readable") is not True:
        return {"readable": False, "found": False, "order": None}
    found = truth.get("found") is True
    order = truth.get("order") if found else None
    if found and order is None:
        return {"readable": False, "found": False, "order": None}
    return {"readable": True, "found": found, "order": order}


def _strict_non_alpaca_open_orders_truth(
    adapter: Any,
    *,
    symbol: str,
) -> dict[str, Any]:
    getter = getattr(adapter, "list_open_orders_truth", None)
    if not callable(getter):
        return {"readable": False, "orders": None}
    try:
        truth = getter(product_id=symbol, limit=250)
    except Exception:
        return {"readable": False, "orders": None}
    if not isinstance(truth, dict) or truth.get("readable") is not True:
        return {"readable": False, "orders": None}
    orders = truth.get("orders")
    if not isinstance(orders, list):
        return {"readable": False, "orders": None}
    return {"readable": True, "orders": orders}


def _strict_non_alpaca_position_truth(
    adapter: Any,
    sess: TradingAutomationSession,
) -> tuple[Optional[bool], dict[str, Any]]:
    getter = getattr(adapter, "get_position_quantity_truth", None)
    if not callable(getter):
        return None, {"reason": "broker_position_truth_adapter_missing"}
    try:
        truth = getter(str(sess.symbol or ""))
    except Exception:
        return None, {"reason": "broker_position_unknown"}
    if not isinstance(truth, dict) or truth.get("readable") is not True:
        return None, {"reason": "broker_position_unknown"}
    if truth.get("quantity") is None:
        return None, {"reason": "broker_position_unknown"}
    return _normalize_reaper_position_quantity(sess, truth.get("quantity"))


def _non_alpaca_live_terminalization_proof(
    db: Session,
    sess: TradingAutomationSession,
    *,
    context: str,
    cancelled_by: str | None = None,
) -> dict[str, Any]:
    """Prove flat + terminal identities + symbol-order absence before death.

    Reads are deliberately tri-state and strict.  Adapter methods that swallow a
    transport failure into ``None``/``[]`` do not qualify; only the explicit
    ``*_truth`` surfaces can certify absence.
    """
    family = normalize_execution_family(sess.execution_family)
    if sess.mode != "live" or family in {"alpaca_spot", "alpaca_short"}:
        return {"state": "not_applicable", "ok": True}
    try:
        from ..venue.factory import get_adapter

        adapter = get_adapter(sess.execution_family)
    except Exception:
        adapter = None
    if adapter is None:
        return _persist_non_alpaca_terminalization_quarantine(
            db,
            sess,
            reason="terminalization_adapter_missing",
            context=context,
        )

    def _account_identity_fence(phase: str) -> dict[str, Any] | None:
        account_truth = verify_frozen_non_alpaca_account_identity(
            sess,
            adapter=adapter,
        )
        if account_truth.get("ok") is True:
            return None
        return _persist_non_alpaca_terminalization_quarantine(
            db,
            sess,
            reason=str(
                account_truth.get("reason")
                or "non_alpaca_account_identity_unknown"
            ),
            context=context,
            detail={
                "phase": str(phase),
                "frozen_identity": account_truth.get("frozen_identity"),
                "current_identity": account_truth.get("current_identity"),
            },
        )

    account_quarantine = _account_identity_fence("before_terminal_proof")
    if account_quarantine is not None:
        return account_quarantine
    initial_generation = _non_alpaca_terminal_generation(sess)
    identities = _collect_non_alpaca_persisted_order_identities(sess)
    symbol = str(sess.symbol or "").strip().upper()
    flat_before, flat_before_detail = _strict_non_alpaca_position_truth(
        adapter,
        sess,
    )
    account_quarantine = _account_identity_fence("after_initial_position_read")
    if account_quarantine is not None:
        return account_quarantine
    if flat_before is not True:
        return _persist_non_alpaca_terminalization_quarantine(
            db,
            sess,
            reason=(
                "terminalization_position_held"
                if flat_before is False
                else "terminalization_position_unknown"
            ),
            context=context,
            detail=dict(flat_before_detail or {}),
        )
    account_quarantine = _account_identity_fence("before_initial_open_order_read")
    if account_quarantine is not None:
        return account_quarantine
    open_before = _strict_non_alpaca_open_orders_truth(adapter, symbol=symbol)
    account_quarantine = _account_identity_fence("after_initial_open_order_read")
    if account_quarantine is not None:
        return account_quarantine
    if open_before.get("readable") is not True:
        return _persist_non_alpaca_terminalization_quarantine(
            db,
            sess,
            reason="terminalization_symbol_orders_unknown",
            context=context,
            detail={"phase": "before_identity_reads"},
        )

    persisted_oids = list(identities["order_ids"])
    persisted_cids = list(identities["client_order_ids"])
    resolved_order_outcomes = dict(identities["resolved_order_outcomes"])
    order_expectations = dict(identities["order_expectations"])
    terminal_orders: dict[str, Any] = {}
    cid_to_oid: dict[str, str] = {}
    cancel_reread_orders: dict[str, Any] = {}

    def _validate_order(order: Any, *, expected_oid: str | None = None) -> dict[str, Any]:
        oid = str(getattr(order, "order_id", "") or "").strip()
        cid = str(getattr(order, "client_order_id", "") or "").strip()
        product = str(getattr(order, "product_id", "") or "").strip().upper()
        status = str(getattr(order, "status", "") or "").strip().lower()
        try:
            filled = float(getattr(order, "filled_size", 0.0) or 0.0)
        except (TypeError, ValueError):
            filled = math.nan
        if not (
            oid
            and (expected_oid is None or oid == expected_oid)
            and product == symbol
            and status
            and math.isfinite(filled)
            and filled >= 0.0
        ):
            return {"state": "invalid", "order_id": oid}
        if cid:
            prior_oid = cid_to_oid.get(cid)
            if prior_oid is not None and prior_oid != oid:
                return {"state": "inconsistent", "order_id": oid, "client_order_id": cid}
            cid_to_oid[cid] = oid
        if status == "filled" or filled > 1e-12:
            return {
                "state": "filled",
                "order_id": oid,
                "client_order_id": cid or None,
                "filled_size": filled,
                "status": status,
            }
        if status in _NON_ALPACA_TERMINAL_ORDER_STATUSES:
            terminal_orders[oid] = order
            return {
                "state": "terminal",
                "order_id": oid,
                "client_order_id": cid or None,
            }
        return {"state": "active", "order_id": oid, "client_order_id": cid or None}

    def _cancel_authority(order: Any) -> tuple[bool, dict[str, Any]]:
        return _exact_non_alpaca_order_authority(
            order,
            order_expectations=order_expectations,
            expected_symbol=symbol,
        )

    def _cancel_then_require_terminal(order: Any) -> dict[str, Any]:
        oid = str(getattr(order, "order_id", "") or "").strip()
        authorized, authority_detail = _cancel_authority(order)
        if not authorized:
            return {
                "state": "cancel_authority_unproven",
                **authority_detail,
            }
        cancel = getattr(adapter, "cancel_order", None)
        if not oid or not callable(cancel):
            return {"state": "cancel_failed", "order_id": oid}
        account_truth = verify_frozen_non_alpaca_account_identity(
            sess,
            adapter=adapter,
        )
        if account_truth.get("ok") is not True:
            return {
                "state": "account_identity_unproven",
                "order_id": oid,
                "phase": "immediately_before_cancel",
                "account_identity_reason": account_truth.get("reason"),
            }
        try:
            result = cancel(oid)
        except Exception:
            return {"state": "cancel_failed", "order_id": oid}
        cancel_ok = bool(
            result is True
            or (isinstance(result, dict) and result.get("ok") is True)
        )
        if not cancel_ok:
            return {"state": "cancel_failed", "order_id": oid}
        account_truth = verify_frozen_non_alpaca_account_identity(
            sess,
            adapter=adapter,
        )
        if account_truth.get("ok") is not True:
            return {
                "state": "account_identity_unproven",
                "order_id": oid,
                "phase": "after_cancel_before_reread",
                "account_identity_reason": account_truth.get("reason"),
            }
        reread = _strict_non_alpaca_order_truth(adapter, oid)
        account_truth = verify_frozen_non_alpaca_account_identity(
            sess,
            adapter=adapter,
        )
        if account_truth.get("ok") is not True:
            return {
                "state": "account_identity_unproven",
                "order_id": oid,
                "phase": "after_cancel_reread",
                "account_identity_reason": account_truth.get("reason"),
            }
        if reread.get("readable") is not True or reread.get("found") is not True:
            return {"state": "cancel_terminal_unknown", "order_id": oid}
        cancel_reread_orders[oid] = reread["order"]
        return _validate_order(reread["order"], expected_oid=oid)

    def _settle_previously_adopted_fill(
        order: Any,
        classified: dict[str, Any],
    ) -> dict[str, Any]:
        """Require the remainder of a durably adopted entry to be terminal.

        ``entry_orders_resolved[oid] == 'adopted'`` proves the fill was handed to
        the management lifecycle.  It does not prove that an unfilled remainder
        stopped resting, so an active remainder is cancelled and strictly reread.
        """
        oid = str(classified.get("order_id") or "").strip()
        if resolved_order_outcomes.get(oid) != "adopted":
            return classified
        settled = classified
        if str(classified.get("status") or "").strip().lower() not in (
            _NON_ALPACA_TERMINAL_ORDER_STATUSES
        ):
            settled = _cancel_then_require_terminal(order)
        if (
            settled.get("state") == "filled"
            and float(settled.get("filled_size") or 0.0) > 1e-12
            and str(settled.get("status") or "").strip().lower()
            in _NON_ALPACA_TERMINAL_ORDER_STATUSES
        ):
            terminal_orders[oid] = cancel_reread_orders.get(oid, order)
            return {
                "state": "terminal",
                "order_id": oid,
                "client_order_id": settled.get("client_order_id"),
                "managed_fill": True,
            }
        return settled

    def _manage_filled_order_or_quarantine(
        order: Any,
        classified: dict[str, Any],
    ) -> dict[str, Any]:
        """Adopt a positive entry fill; every other fill remains non-terminal."""
        oid = str(classified.get("order_id") or "").strip()
        try:
            filled = float(classified.get("filled_size"))
        except (TypeError, ValueError):
            filled = math.nan
        adopted = None
        if (
            math.isfinite(filled)
            and filled > 1e-12
            and sess.state in LIVE_CANCELLABLE_STATES
            and bool(
                getattr(
                    settings,
                    "chili_momentum_adopt_on_cancel_fill_enabled",
                    True,
                )
            )
        ):
            adoption_authorized, adoption_detail = (
                _exact_non_alpaca_order_authority(
                    order,
                    order_expectations=order_expectations,
                    required_intent="entry",
                    expected_symbol=symbol,
                )
            )
            if not adoption_authorized:
                return _persist_non_alpaca_terminalization_quarantine(
                    db,
                    sess,
                    reason=(
                        "terminalization_filled_entry_adoption_authority_unproven"
                    ),
                    context=context,
                    detail={**classified, **adoption_detail},
                )
            account_truth = verify_frozen_non_alpaca_account_identity(
                sess,
                adapter=adapter,
            )
            if account_truth.get("ok") is not True:
                return _persist_non_alpaca_terminalization_quarantine(
                    db,
                    sess,
                    reason=str(
                        account_truth.get("reason")
                        or "non_alpaca_account_identity_unknown"
                    ),
                    context=context,
                    detail={"phase": "before_filled_entry_adoption", **classified},
                )
            adopted = _try_adopt_filled_entry_on_cancel(
                db,
                sess,
                cancelled_by=str(cancelled_by or context),
                adapter_override=adapter,
                strict_filled_order=order,
            )
        if isinstance(adopted, dict) and adopted.get("adopted") is True:
            return {"state": "managed", "ok": True, "result": adopted}
        if isinstance(adopted, dict) and adopted.get("terminalization_deferred"):
            return adopted
        return _persist_non_alpaca_terminalization_quarantine(
            db,
            sess,
            reason="terminalization_filled_order_requires_management",
            context=context,
            detail={**classified, "order_id": oid or None},
        )

    for oid in persisted_oids:
        account_quarantine = _account_identity_fence(
            "before_persisted_order_read"
        )
        if account_quarantine is not None:
            return account_quarantine
        truth = _strict_non_alpaca_order_truth(adapter, oid)
        account_quarantine = _account_identity_fence(
            "after_persisted_order_read"
        )
        if account_quarantine is not None:
            return account_quarantine
        if truth.get("readable") is not True or truth.get("found") is not True:
            return _persist_non_alpaca_terminalization_quarantine(
                db,
                sess,
                reason="terminalization_persisted_order_unknown",
                context=context,
                detail={"order_id": oid},
            )
        classified = _validate_order(truth["order"], expected_oid=oid)
        if classified["state"] == "filled":
            classified = _settle_previously_adopted_fill(
                truth["order"],
                classified,
            )
        if classified["state"] == "filled":
            return _manage_filled_order_or_quarantine(
                truth["order"],
                classified,
            )
        if classified["state"] == "active":
            classified = _cancel_then_require_terminal(truth["order"])
        if classified["state"] == "filled":
            return _manage_filled_order_or_quarantine(
                cancel_reread_orders.get(oid, truth["order"]),
                classified,
            )
        if classified["state"] != "terminal":
            return _persist_non_alpaca_terminalization_quarantine(
                db,
                sess,
                reason=f"terminalization_order_{classified['state']}",
                context=context,
                detail=classified,
            )

    open_orders = list(open_before.get("orders") or [])
    for order in open_orders:
        classified = _validate_order(order)
        oid = str(classified.get("order_id") or "").strip()
        cid = str(classified.get("client_order_id") or "").strip()
        if oid in terminal_orders:
            # The initial OPEN snapshot can predate the successful cancel+reread
            # above.  The final strict OPEN snapshot below is the absence proof.
            continue
        owned = bool(oid in persisted_oids or (cid and cid in persisted_cids))
        if classified["state"] == "filled":
            if owned:
                classified = _settle_previously_adopted_fill(order, classified)
            if classified["state"] == "filled":
                if owned:
                    return _manage_filled_order_or_quarantine(
                        cancel_reread_orders.get(oid, order),
                        classified,
                    )
                return _persist_non_alpaca_terminalization_quarantine(
                    db,
                    sess,
                    reason="terminalization_open_order_fill_inconsistent",
                    context=context,
                    detail=classified,
                )
        if classified["state"] == "active" and owned:
            classified = _cancel_then_require_terminal(order)
        if classified["state"] == "filled" and owned:
            return _manage_filled_order_or_quarantine(
                cancel_reread_orders.get(oid, order),
                classified,
            )
        if classified["state"] == "terminal":
            continue
        return _persist_non_alpaca_terminalization_quarantine(
            db,
            sess,
            reason=(
                f"terminalization_order_{classified['state']}"
                if owned
                else "terminalization_unowned_symbol_order_working"
            ),
            context=context,
            detail=classified,
        )

    for cid in persisted_cids:
        if cid in cid_to_oid:
            continue
        # The process-global idempotency cache is deliberately not authority: it
        # is unscoped by venue/account generation.  A CID-only session can proceed
        # only through an adapter's exact broker CID lookup.
        getter = getattr(adapter, "get_order_by_client_order_id_truth", None)
        account_quarantine = _account_identity_fence("before_client_order_read")
        if account_quarantine is not None:
            return account_quarantine
        try:
            cid_truth = getter(cid) if callable(getter) else None
        except Exception:
            cid_truth = None
        account_quarantine = _account_identity_fence("after_client_order_read")
        if account_quarantine is not None:
            return account_quarantine
        if not isinstance(cid_truth, dict) or cid_truth.get("readable") is not True:
            return _persist_non_alpaca_terminalization_quarantine(
                db,
                sess,
                reason="terminalization_client_order_unknown",
                context=context,
                detail={"client_order_id": cid},
            )
        if cid_truth.get("found") is not True or cid_truth.get("order") is None:
            return _persist_non_alpaca_terminalization_quarantine(
                db,
                sess,
                reason="terminalization_client_order_absence_unproven",
                context=context,
                detail={"client_order_id": cid},
            )
        cid_order = cid_truth["order"]
        observed_cid = str(getattr(cid_order, "client_order_id", "") or "").strip()
        if observed_cid != cid:
            return _persist_non_alpaca_terminalization_quarantine(
                db,
                sess,
                reason="terminalization_client_order_generation_mismatch",
                context=context,
                detail={
                    "client_order_id": cid,
                    "observed_client_order_id": observed_cid,
                },
            )
        classified = _validate_order(cid_order)
        oid = str(classified.get("order_id") or "").strip()
        if classified["state"] == "filled":
            classified = _settle_previously_adopted_fill(cid_order, classified)
        if classified["state"] == "filled":
            return _manage_filled_order_or_quarantine(
                cancel_reread_orders.get(oid, cid_order),
                classified,
            )
        if classified["state"] == "active":
            classified = _cancel_then_require_terminal(cid_order)
        if classified["state"] == "filled":
            return _manage_filled_order_or_quarantine(
                cancel_reread_orders.get(oid, cid_order),
                classified,
            )
        if classified["state"] != "terminal":
            return _persist_non_alpaca_terminalization_quarantine(
                db,
                sess,
                reason=f"terminalization_client_order_{classified['state']}",
                context=context,
                detail={**classified, "client_order_id": cid},
            )
        cid_to_oid[cid] = oid

    account_quarantine = _account_identity_fence("before_final_open_order_read")
    if account_quarantine is not None:
        return account_quarantine
    open_after = _strict_non_alpaca_open_orders_truth(adapter, symbol=symbol)
    account_quarantine = _account_identity_fence("after_final_open_order_read")
    if account_quarantine is not None:
        return account_quarantine
    if open_after.get("readable") is not True:
        return _persist_non_alpaca_terminalization_quarantine(
            db,
            sess,
            reason="terminalization_symbol_orders_unknown",
            context=context,
            detail={"phase": "after_identity_reads"},
        )
    if open_after.get("orders"):
        return _persist_non_alpaca_terminalization_quarantine(
            db,
            sess,
            reason="terminalization_symbol_order_still_working",
            context=context,
            detail={"working_order_count": len(open_after["orders"])},
        )
    account_quarantine = _account_identity_fence("before_final_position_read")
    if account_quarantine is not None:
        return account_quarantine
    flat_after, flat_after_detail = _strict_non_alpaca_position_truth(
        adapter,
        sess,
    )
    account_quarantine = _account_identity_fence("after_final_position_read")
    if account_quarantine is not None:
        return account_quarantine
    if flat_after is not True:
        return _persist_non_alpaca_terminalization_quarantine(
            db,
            sess,
            reason=(
                "terminalization_position_changed"
                if flat_after is False
                else "terminalization_position_recheck_unknown"
            ),
            context=context,
            detail=dict(flat_after_detail or {}),
        )
    if not _non_alpaca_terminal_generation_matches(sess, initial_generation):
        return _persist_non_alpaca_terminalization_quarantine(
            db,
            sess,
            reason="terminalization_session_generation_changed",
            context=context,
            detail={"phase": "before_terminal_proof_persist"},
        )
    snapshot = (
        dict(sess.risk_snapshot_json)
        if isinstance(sess.risk_snapshot_json, dict)
        else {}
    )
    live_exec = snapshot.get(KEY_LIVE_EXEC)
    live_exec = dict(live_exec) if isinstance(live_exec, dict) else {}
    json_order_generation = _json_non_alpaca_order_generation(initial_generation)
    if identities["identity_loss"] or identities["malformed"]:
        observed_at = datetime.utcnow()
        grace_seconds = max(
            1.0,
            float(_NON_ALPACA_IDENTITY_LOSS_VISIBILITY_GRACE_SECONDS),
        )
        observation = {
            "identity_contract": "non_alpaca_identity_loss_observation_v2",
            "session_id": initial_generation["session_id"],
            "mode": initial_generation["mode"],
            "session_state": initial_generation["state"],
            "execution_family": family,
            "symbol": symbol,
            "account_identity": initial_generation["account_identity"],
            **json_order_generation,
            "broker_flat_confirmed": True,
            "working_symbol_orders_absent": True,
        }
        prior_observation = live_exec.get(
            "non_alpaca_identity_loss_observation"
        )
        prior_exact = bool(
            isinstance(prior_observation, dict)
            and all(
                prior_observation.get(key) == value
                for key, value in observation.items()
            )
        )
        first_observed_at: datetime | None = None
        if prior_exact:
            raw_first_observed_at = prior_observation.get(
                "first_observed_at_utc"
            )
            if isinstance(raw_first_observed_at, str):
                try:
                    first_observed_at = datetime.fromisoformat(
                        raw_first_observed_at.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except Exception:
                    first_observed_at = None
            if (
                first_observed_at is not None
                and first_observed_at > observed_at
            ):
                first_observed_at = None
        elapsed_seconds = (
            max(0.0, (observed_at - first_observed_at).total_seconds())
            if first_observed_at is not None
            else 0.0
        )
        visibility_stable = bool(
            prior_exact
            and first_observed_at is not None
            and elapsed_seconds >= grace_seconds
        )
        if not visibility_stable:
            if not prior_exact or first_observed_at is None:
                first_observed_at = observed_at
                elapsed_seconds = 0.0
            live_exec["non_alpaca_identity_loss_observation"] = {
                **observation,
                "first_observed_at_utc": first_observed_at.isoformat(),
                "visibility_grace_seconds": grace_seconds,
            }
            snapshot[KEY_LIVE_EXEC] = live_exec
            sess.risk_snapshot_json = snapshot
            try:
                from sqlalchemy.orm.attributes import flag_modified

                flag_modified(sess, "risk_snapshot_json")
            except Exception:
                pass
            return _persist_non_alpaca_terminalization_quarantine(
                db,
                sess,
                reason="terminalization_identity_loss_stability_pending",
                context=context,
                detail={
                    "identity_loss": bool(identities["identity_loss"]),
                    "malformed_identity_json": bool(identities["malformed"]),
                    "elapsed_seconds": elapsed_seconds,
                    "visibility_grace_seconds": grace_seconds,
                },
            )
    proof = {
        "identity_contract": "non_alpaca_live_terminalization_v2",
        "context": str(context),
        "execution_family": family,
        "symbol": symbol,
        "account_identity": initial_generation["account_identity"],
        "session_state": initial_generation["state"],
        **json_order_generation,
        "broker_flat_confirmed": True,
        "working_symbol_orders_absent": True,
        "proven_at_utc": datetime.utcnow().isoformat(),
    }
    live_exec["non_alpaca_terminalization_proof"] = proof
    live_exec.pop("non_alpaca_terminalization_quarantine", None)
    live_exec.pop("non_alpaca_identity_loss_observation", None)
    snapshot[KEY_LIVE_EXEC] = live_exec
    sess.risk_snapshot_json = snapshot
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(sess, "risk_snapshot_json")
    except Exception:
        pass
    return {"state": "safe", "ok": True, "proof": proof}


def _reaper_has_working_entry_order(sess: TradingAutomationSession) -> Optional[bool]:
    """AREA C in-flight-order gate. Returns True if ANY recorded entry order is still
    working at the broker (must NOT reap), False if all are terminal / none exist, or
    None if the broker read is UNKNOWN (fail safe -> caller skips). Mirrors the order
    sweep in cancel_automation_session (same _oids extraction + adapter.get_order)."""
    if _persisted_alpaca_execution_quarantine_reason(sess) is not None:
        return None
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    le = snap.get(KEY_LIVE_EXEC) if isinstance(snap, dict) else None
    le = le if isinstance(le, dict) else {}
    oids: list[str] = []
    for o in [le.get("entry_order_id")] + list(le.get("entry_order_ids_all") or []):
        os_ = str(o or "").strip()
        if os_ and os_ not in oids:
            oids.append(os_)
    if not oids:
        return False  # no entry order recorded -> nothing in flight
    try:
        from ..venue.factory import get_adapter

        adapter = get_adapter(sess.execution_family)
    except Exception:
        adapter = None
    if adapter is None:
        return None  # unknown -> fail safe
    if not _bind_persisted_alpaca_adapter(sess, adapter):
        return None
    for oid in oids:
        if _persisted_alpaca_execution_quarantine_reason(sess) is not None:
            return None
        try:
            no, _ = adapter.get_order(oid)
        except Exception:
            return None  # unknown -> fail safe
        if no is None:
            return None
        status = str(getattr(no, "status", "") or "").lower()
        if status not in (
            "filled", "cancelled", "canceled", "rejected", "failed", "expired", "done", "closed",
        ):
            return True  # still working -> in flight, do NOT reap
    return False


def reap_stale_live_sessions(db: Session, *, user_id: int) -> dict[str, Any]:
    """AREA C — SAFE BOUNDED reaper for dead-but-lingering live sessions.

    Terminalizes sessions that are CONFIRMED dead: (1) live_error past the TTL, and
    (2) live_bailout past the TTL whose broker position is CONFIRMED 0. NEVER closes a
    session with a real broker position OR a working entry order — every close is gated
    on a SUCCESSFUL broker-flat read + no-in-flight-order read (any UNKNOWN/failed read
    leaves the session alone). Row-locked (FOR UPDATE) so it serializes against a
    concurrent live_runner tick. live_arm_pending is handled separately by
    expire_stale_live_arm_sessions. Bounded: only CONFIRMED-stale (>TTL) rows, capped
    batch. Kill-switch chili_momentum_stale_session_reaper_enabled=False -> no-op."""
    out: dict[str, Any] = {
        "reaped": 0,
        "skipped_unknown": 0,
        "skipped_held": 0,
        "skipped_in_flight": 0,
        "skipped_direction_mismatch": 0,
        "skipped_execution_quarantine": 0,
        "candidates": 0,
    }
    if not bool(getattr(settings, "chili_momentum_stale_session_reaper_enabled", True)):
        return out
    if not _tables_present(db):
        return out
    now = datetime.utcnow()
    ttl_s = max(
        300.0,
        float(getattr(settings, "chili_momentum_stale_session_reaper_ttl_seconds", 7200.0) or 7200.0),
    )
    cutoff = now - timedelta(seconds=ttl_s)
    _reapable = (STATE_LIVE_ERROR, STATE_LIVE_BAILOUT)
    rows = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.user_id == user_id,
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.state.in_(_reapable),
            TradingAutomationSession.updated_at < cutoff,
        )
        .order_by(TradingAutomationSession.updated_at.asc())
        .limit(50)  # bounded batch — never sweep the whole table in one pass
        .all()
    )
    out["candidates"] = len(rows)
    for row in rows:
        sid = row.id
        # Re-load under a row lock so the decision serializes vs a live_runner tick.
        q = db.query(TradingAutomationSession).filter(
            TradingAutomationSession.id == sid,
            TradingAutomationSession.user_id == user_id,
        )
        try:
            q = q.with_for_update()
        except Exception:
            pass
        try:
            q = q.populate_existing()
        except Exception:
            pass
        sess = q.one_or_none()
        if sess is None:
            continue
        # Re-validate state + age UNDER the lock (a tick may have advanced it).
        if sess.state not in _reapable:
            continue
        _upd = sess.updated_at or sess.started_at or now
        if _upd >= cutoff:
            continue
        locked_generation_state = sess.state
        execution_quarantine = _persisted_alpaca_execution_quarantine_reason(sess)
        if execution_quarantine is not None:
            out["skipped_execution_quarantine"] += 1
            _quarantine_persisted_alpaca_execution(
                db,
                sess,
                reason=execution_quarantine,
                context="stale_live_session_reaper",
            )
            continue
        non_alpaca_live = normalize_execution_family(sess.execution_family) not in {
            "alpaca_spot",
            "alpaca_short",
        }
        if non_alpaca_live:
            terminal_truth = _non_alpaca_live_terminalization_proof(
                db,
                sess,
                context="stale_live_session_reaper_pre_terminal",
                cancelled_by="automation_monitor",
            )
            if terminal_truth.get("state") != "safe":
                reason = str(terminal_truth.get("quarantine_reason") or "")
                if "position_held" in reason:
                    out["skipped_held"] += 1
                elif "working" in reason or "active" in reason or "filled" in reason:
                    out["skipped_in_flight"] += 1
                else:
                    out["skipped_unknown"] += 1
                continue
            flat = True
            flat_detail = {"broker_quantity": 0.0, "strict_terminal_proof": True}
        else:
            # GATE 1 — no working entry order (fail safe on unknown).
            in_flight = _reaper_has_working_entry_order(sess)
            if in_flight is None:
                out["skipped_unknown"] += 1
                continue
            if in_flight is True:
                out["skipped_in_flight"] += 1
                continue
            # GATE 2 — broker CONFIRMS flat (fail safe on unknown / real holding).
            flat, flat_detail = _reaper_broker_position_truth(sess)
            if flat is None:
                flat_reason = str(flat_detail.get("reason") or "")
                if "direction" in flat_reason and "mismatch" in flat_reason:
                    out["skipped_direction_mismatch"] += 1
                    _quarantine_reaper_direction_mismatch(db, sess, flat_detail)
                else:
                    out["skipped_unknown"] += 1
                continue
            if flat is False:
                out["skipped_held"] += 1
                continue
        # Broker-flat truth belongs only to the account generation under which it
        # was read.  Re-check the configured pin immediately before either the
        # production cancel path or a direct terminal transition.
        execution_quarantine = _persisted_alpaca_execution_quarantine_reason(sess)
        if execution_quarantine is not None:
            out["skipped_execution_quarantine"] += 1
            _quarantine_persisted_alpaca_execution(
                db,
                sess,
                reason=execution_quarantine,
                context="stale_live_session_reaper_pre_terminal",
            )
            continue
        # A crash-surviving Alpaca entry claim is broker-side authority even when
        # session JSON lost its CID/OID.  Direct live_error terminalization is
        # permitted only after that durable seam is readable and clear.
        if sess.state == STATE_LIVE_ERROR:
            claim_readable, durable_claim = _owned_unresolved_alpaca_entry_claim(
                db,
                sess,
            )
            if not claim_readable:
                out["skipped_unknown"] += 1
                _quarantine_persisted_alpaca_execution(
                    db,
                    sess,
                    reason="alpaca_entry_claim_unreadable",
                    context="stale_live_session_reaper_pre_terminal",
                )
                continue
            if durable_claim is not None:
                out["skipped_in_flight"] += 1
                _quarantine_persisted_alpaca_execution(
                    db,
                    sess,
                    reason="alpaca_entry_claim_unresolved",
                    context="stale_live_session_reaper_pre_terminal",
                )
                continue
            execution_quarantine = _persisted_alpaca_execution_quarantine_reason(sess)
            if execution_quarantine is not None:
                out["skipped_execution_quarantine"] += 1
                _quarantine_persisted_alpaca_execution(
                    db,
                    sess,
                    reason=execution_quarantine,
                    context="stale_live_session_reaper_claim_post_read",
                )
                continue
        prev = sess.state
        if prev == STATE_LIVE_BAILOUT:
            # live_bailout is in LIVE_CANCELLABLE_STATES -> route through the
            # production terminalizer (adopt-on-cancel-fill is a safe no-op here
            # since the broker is confirmed flat + no order in flight).
            res = cancel_automation_session(db, user_id=user_id, session_id=sid)
            if res.get("ok") and sess.state == STATE_LIVE_CANCELLED:
                out["reaped"] += 1
                append_trading_automation_event(
                    db, sid, "stale_session_reaped",
                    {"previous_state": prev, "via": "cancel", "reason": "broker_flat_stale_bailout",
                     "ttl_seconds": ttl_s, "terminal_state": res.get("state")},
                    correlation_id=sess.correlation_id, source_node_id="momentum_automation_monitor",
                )
            else:
                out["skipped_unknown"] += 1
        else:
            # live_error has NO legal outgoing FSM edge — terminalize the row directly
            # (the same pattern expire_stale_live_arm_sessions uses for arm_pending).
            if non_alpaca_live and (
                sess.state != locked_generation_state
                or not _non_alpaca_terminal_proof_matches_session(
                    sess,
                    terminal_truth.get("proof"),
                )
            ):
                out["skipped_unknown"] += 1
                _persist_non_alpaca_terminalization_quarantine(
                    db,
                    sess,
                    reason="terminalization_session_generation_changed",
                    context="stale_live_session_reaper_pre_terminal_mutation",
                    detail={
                        "expected_state": locked_generation_state,
                        "current_state": sess.state,
                    },
                )
                continue
            if non_alpaca_live:
                final_account_quarantine = (
                    _final_non_alpaca_terminal_account_quarantine(
                        db,
                        sess,
                        context=(
                            "stale_live_session_reaper_final_account_fence"
                        ),
                    )
                )
                if final_account_quarantine is not None:
                    out["skipped_execution_quarantine"] += 1
                    continue
            sess.state = STATE_LIVE_CANCELLED
            sess.ended_at = now
            sess.updated_at = now
            append_trading_automation_event(
                db, sid, "stale_session_reaped",
                {"previous_state": prev, "via": "direct", "reason": "broker_flat_stale_error",
                 "ttl_seconds": ttl_s, "terminal_state": STATE_LIVE_CANCELLED},
                correlation_id=sess.correlation_id, source_node_id="momentum_automation_monitor",
            )
            out["reaped"] += 1
            try:
                from .feedback_emit import emit_feedback_after_terminal_transition

                emit_feedback_after_terminal_transition(db, sess)
            except Exception:
                pass
    return out


def _live_runner_driver_posture() -> dict[str, Any]:
    """Expose the shared live-session owner contract without starting it."""
    from .lane_health import live_runner_driver_configuration

    master_on = bool(settings.chili_momentum_live_runner_enabled)
    batch_on = bool(settings.chili_momentum_live_runner_scheduler_enabled)
    loop_on = bool(
        getattr(settings, "chili_momentum_live_runner_loop_enabled", False)
    )
    price_bus_on = bool(
        getattr(settings, "chili_autopilot_price_bus_enabled", False)
    )

    mode, configuration_error = live_runner_driver_configuration()
    blocked_reason = (
        "live_runner_disabled" if not master_on else configuration_error
    )

    return {
        "master_enabled": master_on,
        "driver_mode": mode,
        "driver_enabled": mode is not None and blocked_reason is None,
        "blocked_reason": blocked_reason,
        "legacy_batch_enabled": batch_on,
        "event_loop_enabled": loop_on,
        "price_bus_enabled": price_bus_on,
    }


def neural_config_strip() -> dict[str, Any]:
    live_driver = _live_runner_driver_posture()
    return {
        "mesh_enabled": bool(mesh_enabled()),
        "momentum_neural_enabled": bool(settings.chili_momentum_neural_enabled),
        "coinbase_spot_adapter_enabled": bool(settings.chili_coinbase_spot_adapter_enabled),
        "coinbase_ws_enabled": bool(settings.chili_coinbase_ws_enabled),
        "coinbase_strict_freshness": bool(settings.chili_coinbase_strict_freshness),
        "paper_runner_enabled": bool(settings.chili_momentum_paper_runner_enabled),
        "paper_runner_scheduler_enabled": bool(settings.chili_momentum_paper_runner_scheduler_enabled),
        "paper_runner_scheduler_interval_minutes": int(
            settings.chili_momentum_paper_runner_scheduler_interval_minutes
        ),
        "live_runner_enabled": bool(settings.chili_momentum_live_runner_enabled),
        "live_runner_scheduler_enabled": bool(settings.chili_momentum_live_runner_scheduler_enabled),
        "live_runner_loop_enabled": bool(
            getattr(settings, "chili_momentum_live_runner_loop_enabled", False)
        ),
        "live_runner_driver_mode": live_driver["driver_mode"],
        "live_runner_driver_enabled": live_driver["driver_enabled"],
        "live_runner_driver_blocked_reason": live_driver["blocked_reason"],
        "autopilot_price_bus_enabled": live_driver["price_bus_enabled"],
        "live_runner_scheduler_interval_minutes": int(
            settings.chili_momentum_live_runner_scheduler_interval_minutes
        ),
        "neural_feedback_enabled": bool(settings.chili_momentum_neural_feedback_enabled),
        "trading_automation_hud_enabled": bool(settings.chili_trading_automation_hud_enabled),
    }


def governance_strip() -> dict[str, Any]:
    g = get_kill_switch_status()
    return {"kill_switch_active": bool(g.get("active")), "kill_switch_reason": g.get("reason")}


def _variant_brief(v: MomentumStrategyVariant) -> dict[str, Any]:
    return {
        "id": v.id,
        "family": v.family,
        "strategy_family": v.family,
        "variant_key": v.variant_key,
        "label": v.label,
        "version": v.version,
        "execution_family": v.execution_family,
        "is_active": bool(v.is_active),
    }


def _status_summary(state: str) -> str:
    return {
        STATE_DRAFT: "Draft — paper intent recorded; runner disabled or not admitted.",
        STATE_QUEUED: "Queued — waiting for paper runner tick (Phase 7).",
        STATE_WATCHING: "Paper runner watching — scanning viability / quotes.",
        STATE_ENTRY_CANDIDATE: "Paper — setup detected; confirming entry.",
        STATE_PENDING_ENTRY: "Paper — simulated entry in flight.",
        STATE_ENTERED: "Paper — simulated position open.",
        STATE_SCALING_OUT: "Paper — scaling / taking profit zone.",
        STATE_TRAILING: "Paper — trailing stop armed.",
        STATE_BAILOUT: "Paper — bailout exit.",
        STATE_EXITED: "Paper — flat; entering cooldown.",
        STATE_COOLDOWN: "Paper — cooldown before finished.",
        STATE_FINISHED: "Paper — session complete (simulated).",
        STATE_ERROR: "Paper runner error — inspect events.",
        STATE_QUEUED_LIVE: "Live — queued for guarded runner.",
        STATE_WATCHING_LIVE: "Live runner watching.",
        STATE_LIVE_ENTRY_CANDIDATE: "Live — entry candidate.",
        STATE_LIVE_PENDING_ENTRY: "Live — entry order pending / reconciling.",
        STATE_LIVE_ENTERED: "Live — position open (venue).",
        STATE_LIVE_SCALING_OUT: "Live — scaling / profit zone.",
        STATE_LIVE_TRAILING: "Live — trailing stop.",
        STATE_LIVE_BAILOUT: "Live — bailout exit.",
        STATE_LIVE_EXITED: "Live — flat; cooldown.",
        STATE_LIVE_COOLDOWN: "Live — cooldown.",
        STATE_LIVE_FINISHED: "Live — session finished.",
        STATE_LIVE_CANCELLED: "Live — cancelled by operator.",
        STATE_LIVE_ERROR: "Live runner error — inspect events.",
        STATE_LIVE_ARM_PENDING: "Live arm pending — confirm in Trading or cancel here.",
        STATE_ARMED_PENDING_RUNNER: "Live armed — first live runner tick moves to queued/watching (Phase 8).",
        STATE_CANCELLED: "Cancelled by operator.",
        STATE_ARCHIVED: "Archived (hidden from default list).",
        STATE_EXPIRED: "Live arm confirmation window expired.",
        STATE_IDLE: "Idle / legacy placeholder.",
    }.get(state, "Unknown state — inspect events.")


def _session_warnings(sess: TradingAutomationSession) -> list[str]:
    w: list[str] = []
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    if sess.state == STATE_LIVE_ARM_PENDING:
        exp = _parse_expires(snap)
        if exp:
            left = (exp - datetime.utcnow()).total_seconds()
            if left < 120:
                w.append("Arm confirmation expires soon.")
    return w


_LIVE_TERMINAL_FOR_FOCUS = frozenset({STATE_LIVE_FINISHED, STATE_LIVE_CANCELLED, STATE_LIVE_ERROR})
_PAPER_TERMINAL_FOR_FOCUS = frozenset({STATE_FINISHED, STATE_CANCELLED, STATE_EXPIRED, STATE_ERROR})


def operator_fields_for_session(sess: TradingAutomationSession, readiness: dict[str, Any]) -> dict[str, Any]:
    alloc = sess.allocation_decision_json if isinstance(getattr(sess, "allocation_decision_json", None), dict) else {}
    if (
        alloc
        and not alloc.get("allowed_if_enforced", True)
        and bool(getattr(settings, "brain_allocator_live_hard_block_enabled", False))
    ):
        readiness = dict(readiness or {})
        readiness["_allocator_block_live"] = str(alloc.get("blocked_reason") or "allocator_blocked")
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    canon = canonical_operator_state(mode=sess.mode, state=sess.state, risk_snapshot_json=snap)
    hint = phase_hint(mode=sess.mode, state=sess.state, risk_snapshot_json=snap)
    blocked = blocked_reason_for_session(mode=sess.mode, readiness=readiness, canonical_state=canon)
    nxt = next_action_required(
        mode=sess.mode,
        state=sess.state,
        canonical_state=canon,
        readiness=readiness,
        blocked=blocked,
    )
    return {
        "canonical_operator_state": canon,
        "phase_hint": hint,
        "blocked_reason": blocked,
        "next_action_required": nxt,
        "is_armed_only_live": is_armed_only_live(mode=sess.mode, state=sess.state),
        "is_live_orders_active": is_live_orders_active(mode=sess.mode, state=sess.state),
    }


TERMINAL_STATES = frozenset(PAPER_RUNNER_TERMINAL_STATES) | frozenset(LIVE_RUNNER_TERMINAL_STATES)
PAUSABLE_STATES = frozenset(PAPER_RUNNER_RUNNABLE_STATES) | frozenset(LIVE_RUNNER_RUNNABLE_STATES)


def _variant_refinement_info(variant: MomentumStrategyVariant) -> dict[str, Any]:
    return {
        "is_refined": bool(getattr(variant, "parent_variant_id", None)),
        "parent_variant_id": getattr(variant, "parent_variant_id", None),
        "meta": variant.refinement_meta_json if isinstance(variant.refinement_meta_json, dict) else {},
    }


def _last_tick_from_snapshot(sess: TradingAutomationSession) -> str | None:
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    if sess.mode == "live":
        le = snap.get("momentum_live_execution")
        return le.get("last_tick_utc") if isinstance(le, dict) else None
    pe = snap.get("momentum_paper_execution")
    return pe.get("last_tick_utc") if isinstance(pe, dict) else None


def _latest_scheduler_heartbeat_at(db: Session) -> datetime | None:
    try:
        row = (
            db.query(BrainBatchJob.ended_at)
            .filter(
                BrainBatchJob.job_type == JOB_SCHEDULER_WORKER_HEARTBEAT,
                BrainBatchJob.status == "ok",
            )
            .order_by(BrainBatchJob.ended_at.desc())
            .first()
        )
        return row[0] if row and row[0] else None
    except Exception:
        return None


def _runner_health_for_mode(
    db: Session,
    *,
    mode: str,
    sess: TradingAutomationSession | None = None,
) -> dict[str, Any]:
    now = datetime.utcnow()
    m = (mode or "paper").strip().lower()
    driver_mode: str | None
    driver_enabled: bool
    legacy_batch_enabled = False
    event_loop_enabled = False
    price_bus_enabled = bool(
        getattr(settings, "chili_autopilot_price_bus_enabled", False)
    )
    driver_blocked_reason: str | None = None
    event_loop_truth: dict[str, Any] | None = None
    event_loop_stale_seconds: float | None = None
    if m == "live":
        posture = _live_runner_driver_posture()
        enabled = bool(posture["master_enabled"])
        driver_mode = posture["driver_mode"]
        driver_enabled = bool(posture["driver_enabled"])
        driver_blocked_reason = posture["blocked_reason"]
        legacy_batch_enabled = bool(posture["legacy_batch_enabled"])
        event_loop_enabled = bool(posture["event_loop_enabled"])
        price_bus_enabled = bool(posture["price_bus_enabled"])
        # Compatibility for the current cockpit: this field means an automated
        # session driver is configured, not specifically the legacy batch job.
        scheduler_enabled = driver_enabled
        interval_minutes = int(settings.chili_momentum_live_runner_scheduler_interval_minutes)
    else:
        enabled = bool(settings.chili_momentum_paper_runner_enabled)
        scheduler_enabled = bool(settings.chili_momentum_paper_runner_scheduler_enabled)
        driver_mode = "scheduled_batch" if scheduler_enabled else None
        driver_enabled = scheduler_enabled
        legacy_batch_enabled = scheduler_enabled
        driver_blocked_reason = (
            None if scheduler_enabled else "paper_runner_scheduler_disabled"
        )
        interval_minutes = int(settings.chili_momentum_paper_runner_scheduler_interval_minutes)

    health_sess = sess
    if health_sess is None:
        health_sess = (
            db.query(TradingAutomationSession)
            .filter(TradingAutomationSession.mode == m)
            .order_by(TradingAutomationSession.updated_at.desc())
            .first()
        )

    hb_at: datetime | None = None
    heartbeat_source: str | None = None
    if m == "live" and driver_mode == "event_loop":
        try:
            from .lane_health import (
                _latest_live_loop_heartbeat_status,
                live_loop_stale_seconds,
            )

            event_loop_stale_seconds = live_loop_stale_seconds()
            event_loop_truth = _latest_live_loop_heartbeat_status(
                db,
                stale_seconds=event_loop_stale_seconds,
            )
        except Exception:
            event_loop_truth = {
                "ok": False,
                "reason": "live_runner_loop_heartbeat_unreadable",
            }
        if event_loop_truth.get("ok") is True:
            value = event_loop_truth.get("heartbeat_at")
            hb_at = value if isinstance(value, datetime) else None
            if hb_at is None:
                event_loop_truth = {
                    "ok": False,
                    "reason": "live_runner_loop_heartbeat_unreadable",
                }
            else:
                heartbeat_source = "live_loop_heartbeat"
    elif (
        m == "live" and driver_mode == "scheduled_auto_arm"
    ) or m != "live":
        hb_at = _latest_scheduler_heartbeat_at(db)
        heartbeat_source = "scheduler_heartbeat" if hb_at is not None else None

    hb_age = (now - hb_at).total_seconds() if hb_at else None
    kill = get_kill_switch_status()
    kill_active = bool(kill.get("active"))
    blocked_reason = driver_blocked_reason
    if not enabled:
        blocked_reason = f"{m}_runner_disabled"
    elif not driver_enabled:
        blocked_reason = driver_blocked_reason or f"{m}_runner_driver_disabled"
    elif m == "live" and driver_mode == "event_loop" and (
        not isinstance(event_loop_truth, dict)
        or event_loop_truth.get("ok") is not True
    ):
        blocked_reason = str(
            (event_loop_truth or {}).get("reason")
            or "live_runner_loop_heartbeat_unreadable"
        )
    elif hb_age is None:
        blocked_reason = (
            "live_runner_loop_heartbeat_missing"
            if m == "live" and driver_mode == "event_loop"
            else "scheduler_worker_heartbeat_missing"
        )
    elif m == "live" and driver_mode == "event_loop" and hb_age < -1.0:
        blocked_reason = "live_runner_loop_heartbeat_future"
    elif (
        m == "live"
        and driver_mode == "event_loop"
        and event_loop_stale_seconds is not None
        and hb_age >= event_loop_stale_seconds
    ):
        blocked_reason = "live_runner_loop_heartbeat_stale"
    elif (
        driver_mode in {"scheduled_auto_arm", "scheduled_batch"}
        and hb_age > max(
            420.0,
            float(interval_minutes) * 120.0,
        )
    ):
        blocked_reason = "scheduler_worker_stale"
    elif kill_active:
        blocked_reason = "kill_switch_active"
    elif m == "live" and health_sess is not None:
        # Session health is venue-specific.  Never let an unrelated Coinbase
        # connection decide whether an Alpaca or Robinhood session is ready.
        execution_family = str(
            getattr(health_sess, "execution_family", "") or ""
        ).strip()
        symbol = str(getattr(health_sess, "symbol", "") or "").strip()
        try:
            live_readiness = build_momentum_operator_readiness(
                execution_family=execution_family,
                symbol=symbol or None,
            )
        except Exception:
            live_readiness = {}
        if not live_readiness.get("broker_ready_for_live"):
            blocked_reason = "broker_not_ready"
        elif not live_readiness.get("runnable_live_now"):
            blocked_reason = "live_execution_not_ready"

    last_tick = (
        _last_tick_from_snapshot(health_sess)
        if health_sess is not None
        else None
    )

    # When no session has ticked yet, fall back to the configured driver's durable
    # heartbeat so the UI shows liveness instead of a misleading "n/a".
    last_tick_source = "session" if last_tick else None
    if last_tick is None and hb_at is not None:
        last_tick = hb_at.isoformat()
        last_tick_source = heartbeat_source

    next_tick_eta_seconds: int | None = None
    next_tick_overdue_seconds: int | None = None
    if (
        enabled
        and driver_mode in {"scheduled_auto_arm", "scheduled_batch"}
        and hb_at is not None
    ):
        remaining = int(interval_minutes * 60 - max(0.0, hb_age or 0.0))
        if remaining >= 0:
            next_tick_eta_seconds = remaining
        else:
            next_tick_overdue_seconds = -remaining

    return {
        "mode": m,
        "enabled": enabled,
        "scheduler_enabled": scheduler_enabled,
        "driver_mode": driver_mode,
        "driver_enabled": driver_enabled,
        "legacy_batch_scheduler_enabled": legacy_batch_enabled,
        "event_loop_enabled": event_loop_enabled,
        "price_bus_enabled": price_bus_enabled,
        "execution_family": (
            str(getattr(health_sess, "execution_family", "") or "").strip()
            if health_sess is not None
            else None
        ),
        "interval_minutes": interval_minutes,
        "last_tick_utc": last_tick,
        "last_tick_source": last_tick_source,
        "scheduler_heartbeat_utc": (
            hb_at.isoformat() if hb_at and heartbeat_source == "scheduler_heartbeat" else None
        ),
        "live_loop_heartbeat_utc": (
            hb_at.isoformat() if hb_at and heartbeat_source == "live_loop_heartbeat" else None
        ),
        "heartbeat_age_seconds": round(hb_age, 1) if hb_age is not None else None,
        "live_loop_stale_seconds": event_loop_stale_seconds,
        "next_tick_eta_seconds": next_tick_eta_seconds,
        "next_tick_overdue_seconds": next_tick_overdue_seconds,
        "blocked_reason": blocked_reason,
    }


def _float_or_none_q(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _pnl_summary(sess: TradingAutomationSession, runtime_values: dict[str, Any]) -> dict[str, Any]:
    """The two numbers an operator actually scans for (2026-06-12 UX pass):
    FLOATING (unrealized: open position vs last price) and REALIZED (banked this
    session). Computed at read time from the session's own execution state —
    None when not applicable, never fabricated."""
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    ex = snap.get("momentum_live_execution") if sess.mode == "live" else snap.get("momentum_paper_execution")
    ex = ex if isinstance(ex, dict) else {}
    pos = ex.get("position") if isinstance(ex.get("position"), dict) else None
    out: dict[str, Any] = {
        "floating_usd": None, "floating_pct": None,
        "realized_usd": _float_or_none_q(ex.get("realized_pnl_usd")),
        "qty": None, "entry": None, "last": _float_or_none_q(runtime_values.get("last_price")),
    }
    if not pos:
        return out
    qty = _float_or_none_q(pos.get("quantity"))
    entry = _float_or_none_q(pos.get("avg_entry_price")) or _float_or_none_q(pos.get("entry_price"))
    last = _float_or_none_q(ex.get("last_mid")) or out["last"]
    out["qty"], out["entry"], out["last"] = qty, entry, last
    if qty and entry and last and entry > 0:
        out["floating_usd"] = round((last - entry) * qty, 2)
        out["floating_pct"] = round((last - entry) / entry * 100.0, 2)
    return out


def _controls_for_session(
    sess: TradingAutomationSession,
    *,
    paused: bool,
    runner_health: dict[str, Any],
) -> dict[str, Any]:
    runner_enabled = bool(runner_health.get("enabled"))
    is_terminal = sess.state in TERMINAL_STATES or sess.state == STATE_ARCHIVED
    run_enabled = False
    if paused:
        run_enabled = False
    elif is_terminal:
        run_enabled = runner_enabled
    elif sess.mode == "paper" and sess.state in (STATE_DRAFT, STATE_IDLE, STATE_QUEUED):
        run_enabled = runner_enabled
    elif sess.mode == "live" and sess.state in (STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE):
        run_enabled = runner_enabled

    pause_enabled = (sess.state in PAUSABLE_STATES) and not paused and sess.state not in (STATE_DRAFT, STATE_IDLE)
    resume_enabled = paused and runner_enabled
    stop_enabled = sess.state != STATE_ARCHIVED and sess.state not in TERMINAL_STATES
    delete_enabled = sess.state != STATE_ARCHIVED
    # System-mediated manual exit (2026-06-11 CPSH/SNDG: manual closes in the
    # broker app race the system's own orders) — only for a LIVE held position.
    flatten_enabled = sess.mode == "live" and sess.state in (
        "live_entered", "live_scaling_out", "live_trailing", "live_bailout",
    )
    return {
        "run": {"enabled": run_enabled, "label": "Run again" if is_terminal else "Run"},
        "pause": {"enabled": pause_enabled, "label": "Pause"},
        "resume": {"enabled": resume_enabled, "label": "Resume"},
        "stop": {"enabled": stop_enabled, "label": "Stop"},
        "flatten": {"enabled": flatten_enabled, "label": "Flatten"},
        "delete": {"enabled": delete_enabled, "label": "Delete"},
    }


def _fresh_run_snapshot(sess: TradingAutomationSession) -> dict[str, Any]:
    snap = dict(sess.risk_snapshot_json or {})
    for key in (
        "momentum_paper_execution",
        "momentum_live_execution",
        "operator_pause",
        "arm_token",
        "expires_at_utc",
        "arm_confirmed_at_utc",
        "arm_confirmed",
        "alpaca_symbol_claim_token",
    ):
        snap.pop(key, None)
    return snap


def _find_duplicate_active_session(
    db: Session,
    *,
    user_id: int,
    sess: TradingAutomationSession,
) -> TradingAutomationSession | None:
    q = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.user_id == user_id,
            TradingAutomationSession.symbol == sess.symbol,
            TradingAutomationSession.variant_id == sess.variant_id,
            TradingAutomationSession.mode == sess.mode,
            TradingAutomationSession.id != int(sess.id),
            TradingAutomationSession.state != STATE_ARCHIVED,
        )
        .all()
    )
    for row in q:
        if row.state not in TERMINAL_STATES:
            return row
    return None


def _serialize_binding(binding: TradingAutomationSessionBinding | None, *, sess: TradingAutomationSession, quote_source: str | None = None, blocked_reason: str | None = None) -> dict[str, Any]:
    if binding is None:
        return default_session_binding(
            venue=sess.venue,
            mode=sess.mode,
            execution_family=sess.execution_family,
            quote_source=quote_source,
            gating_reason=blocked_reason,
        )
    meta_json = binding.meta_json if isinstance(binding.meta_json, dict) else {}
    return {
        "discovery_provider": binding.discovery_provider,
        "chart_provider": binding.chart_provider,
        "signal_provider": binding.signal_provider,
        "source_of_truth_provider": binding.source_of_truth_provider,
        "source_of_truth_exchange": binding.source_of_truth_exchange,
        "bar_builder": binding.bar_builder,
        "latency_class": binding.latency_class,
        "simulation_fidelity": binding.simulation_fidelity,
        "gating_reason": blocked_reason or binding.gating_reason,
        "meta_json": meta_json,
    }


def _focus_priority(sess: TradingAutomationSession) -> tuple[int, float]:
    if sess.state == STATE_ARCHIVED:
        return (2, 0.0)
    if sess.mode == "live" and sess.state not in _LIVE_TERMINAL_FOR_FOCUS:
        ts = (sess.updated_at or sess.started_at or datetime.utcnow()).timestamp()
        return (0, -ts)
    if sess.mode == "paper" and sess.state not in _PAPER_TERMINAL_FOR_FOCUS:
        ts = (sess.updated_at or sess.started_at or datetime.utcnow()).timestamp()
        return (1, -ts)
    ts = (sess.updated_at or sess.started_at or datetime.utcnow()).timestamp()
    return (2, -ts)


def _compute_lane_status(db: Session, *, user_id: int) -> dict[str, Any]:
    """Daily-loss circuit-breaker status for the momentum LIVE lane.

    Mirrors auto_arm Guard 4 / risk_evaluator's daily_loss_cap check so the
    Monitor card can render an explicit HALTED banner instead of the misleading
    "waiting for a setup" empty copy when the equity-relative daily-loss breaker
    has tripped (the breaker blocks new arms until the daily window rolls over).
    Read-only and fail-open: a compute error never reports halted.

    ``resets_at_utc`` is the next local-midnight boundary — the SAME ``date.today()``
    window ``_daily_realized_pnl`` sums over — so the shown reset is exactly when
    today's realized losses roll out and the breaker clears. Production containers
    run UTC, so this is 00:00 UTC.
    docs/DESIGN/MOMENTUM_LANE.md; see [[project_momentum_lane]].
    """
    status: dict[str, Any] = {
        "halted": False,
        "halt_reason": None,
        "daily_pnl_usd": None,
        "max_daily_loss_usd": None,
        "peak_pnl_usd": None,
        "giveback_fraction": None,
        "resets_at_utc": None,
    }
    try:
        from .risk_evaluator import _daily_realized_pnl, evaluate_profit_giveback_halt
        from .risk_policy import equity_relative_daily_loss_cap
        from .auto_arm import _lane_execution_family

        # Banner basis = the LANE's actual execution family (equity-only -> robinhood_spot),
        # NOT a hardcoded Coinbase basis (2026-06-17 fix). With crypto disabled the banner was
        # capping the equity lane's loss against the tiny Coinbase equity -> a FALSE "HALTED"
        # display even though the real per-broker/global breaker was nowhere near tripped.
        _lane_fam = _lane_execution_family()
        max_dl = equity_relative_daily_loss_cap(
            float(getattr(settings, "chili_momentum_risk_max_daily_loss_usd", 250.0)),
            _lane_fam,
        )
        daily_pnl = _daily_realized_pnl(db, int(user_id))
        resets_at = datetime.combine(date.today() + timedelta(days=1), datetime.min.time())
        status["daily_pnl_usd"] = round(float(daily_pnl), 2)
        status["max_daily_loss_usd"] = round(float(max_dl), 2)
        status["resets_at_utc"] = resets_at.isoformat()
        status["halted"] = bool(daily_pnl <= -max_dl)
        if status["halted"]:
            status["halt_reason"] = "daily_loss_cap"
        else:
            # Upside round-trip guard (Ross 50%-giveback rule): the lane ALSO halts
            # when a meaningful green day has given back >= the giveback fraction of
            # its peak. Daily-loss cap takes precedence (checked first, more severe).
            gb = evaluate_profit_giveback_halt(
                db, user_id=int(user_id), execution_family=_lane_fam
            )
            status["peak_pnl_usd"] = gb.get("peak_pnl_usd")
            status["giveback_fraction"] = gb.get("giveback_fraction")
            if gb.get("halted"):
                status["halted"] = True
                status["halt_reason"] = "profit_giveback"
    except Exception:
        pass
    return status


def list_automation_sessions(
    db: Session,
    *,
    user_id: int,
    state: Optional[str] = None,
    mode: Optional[str] = None,
    symbol: Optional[str] = None,
    include_archived: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    if not _tables_present(db):
        end_read_only_transaction(db, context="automation_sessions_tables_missing")
        return {
            "sessions": [],
            "neural": neural_config_strip(),
            "governance": governance_strip(),
            "risk_policy_summary": effective_policy_summary(),
            "lane_status": _compute_lane_status(db, user_id=user_id),
            "limitations_note": LIMITATIONS_NOTE,
            "paper_runner_queued": 0,
            "paper_runner_active": 0,
            "live_runner_queued": 0,
            "live_runner_active": 0,
            "operator_readiness": build_momentum_operator_readiness(execution_family="coinbase_spot"),
        }

    expire_stale_live_arm_sessions(db, user_id=user_id)

    q = (
        db.query(TradingAutomationSession, MomentumStrategyVariant)
        .join(MomentumStrategyVariant, MomentumStrategyVariant.id == TradingAutomationSession.variant_id)
        .filter(TradingAutomationSession.user_id == user_id)
        .order_by(TradingAutomationSession.updated_at.desc())
    )
    if not include_archived:
        q = q.filter(TradingAutomationSession.state != STATE_ARCHIVED)
    if state:
        q = q.filter(TradingAutomationSession.state == state.strip())
    if mode and mode.lower() in ("paper", "live"):
        q = q.filter(TradingAutomationSession.mode == mode.lower())
    if symbol:
        q = q.filter(TradingAutomationSession.symbol == symbol.strip().upper())

    rows = q.limit(min(max(limit, 1), 500)).all()
    ids = [int(s[0].id) for s in rows]
    counts: dict[int, int] = {}
    fill_counts: dict[int, int] = {}
    fills_present = _table_exists(db, "trading_automation_simulated_fills")
    runtime_present = _table_exists(db, "trading_automation_runtime_snapshots")
    binding_present = _table_exists(db, "trading_automation_session_bindings")
    if ids:
        for sid, cnt in (
            db.query(TradingAutomationEvent.session_id, func.count(TradingAutomationEvent.id))
            .filter(TradingAutomationEvent.session_id.in_(ids))
            .group_by(TradingAutomationEvent.session_id)
            .all()
        ):
            counts[int(sid)] = int(cnt)
        if fills_present:
            for sid, cnt in (
                db.query(TradingAutomationSimulatedFill.session_id, func.count(TradingAutomationSimulatedFill.id))
                .filter(TradingAutomationSimulatedFill.session_id.in_(ids))
                .group_by(TradingAutomationSimulatedFill.session_id)
                .all()
            ):
                fill_counts[int(sid)] = int(cnt)

    runtime_map: dict[int, TradingAutomationRuntimeSnapshot] = {}
    binding_map: dict[int, TradingAutomationSessionBinding] = {}
    if ids:
        if runtime_present:
            for row in (
                db.query(TradingAutomationRuntimeSnapshot)
                .filter(TradingAutomationRuntimeSnapshot.session_id.in_(ids))
                .all()
            ):
                runtime_map[int(row.session_id)] = row
        if binding_present:
            for row in (
                db.query(TradingAutomationSessionBinding)
                .filter(TradingAutomationSessionBinding.session_id.in_(ids))
                .all()
            ):
                binding_map[int(row.session_id)] = row

    symbols = [str(s[0].symbol) for s in rows]
    variant_ids = [int(s[0].variant_id) for s in rows]
    viability_map: dict[tuple[str, int], MomentumSymbolViability] = {}
    if symbols and variant_ids:
        for via in (
            db.query(MomentumSymbolViability)
            .filter(
                MomentumSymbolViability.symbol.in_(symbols),
                MomentumSymbolViability.variant_id.in_(variant_ids),
            )
            .all()
        ):
            viability_map[(str(via.symbol), int(via.variant_id))] = via
    detach_loaded_instances(
        db,
        rows,
        runtime_map.values(),
        binding_map.values(),
        viability_map.values(),
    )
    end_read_only_transaction(db, context="automation_sessions_bulk_reads")

    sessions_out: list[dict[str, Any]] = []
    for sess, var in rows:
        ef = (sess.execution_family or "coinbase_spot").strip().lower()
        rd = build_momentum_operator_readiness(execution_family=ef, symbol=sess.symbol)
        rd = merge_repeatable_edge_robustness_into_readiness(
            rd, db, scan_pattern_id=getattr(var, "scan_pattern_id", None)
        )
        end_read_only_transaction(db, context="automation_sessions_repeatable_edge")
        op_fields = operator_fields_for_session(sess, rd)
        paused = is_operator_paused(sess.risk_snapshot_json)
        pause_info = operator_pause_info(sess.risk_snapshot_json)
        runner_health = _runner_health_for_mode(db, mode=sess.mode, sess=sess)
        end_read_only_transaction(db, context="automation_sessions_runner_health")
        via = viability_map.get((str(sess.symbol), int(sess.variant_id)))
        runtime_values = build_runtime_snapshot_values(
            sess,
            variant=var,
            viability=via,
            trade_count=fill_counts.get(int(sess.id), 0),
            execution_readiness={
                "operator_readiness": rd,
                "blocked_reason": op_fields.get("blocked_reason"),
            },
        )
        runtime_row = runtime_map.get(int(sess.id))
        binding_payload = _serialize_binding(
            binding_map.get(int(sess.id)),
            sess=sess,
            quote_source=(runtime_row.metrics_json if runtime_row and isinstance(runtime_row.metrics_json, dict) else {}).get("paper_execution", {}).get("last_quote_source"),
            blocked_reason=op_fields.get("blocked_reason"),
        )
        data_fidelity = {
            "lane": runtime_values.get("lane"),
            "simulation_fidelity": binding_payload.get("simulation_fidelity"),
            "latency_class": binding_payload.get("latency_class"),
            "source_of_truth_provider": binding_payload.get("source_of_truth_provider"),
            "source_of_truth_exchange": binding_payload.get("source_of_truth_exchange"),
        }
        runtime_payload = {
            "seconds": runtime_values.get("runtime_seconds"),
            "started_at": sess.started_at.isoformat() if sess.started_at else None,
        }
        row = {
            "id": sess.id,
            "symbol": sess.symbol,
            "variant_id": sess.variant_id,
            "variant": _variant_brief(var),
            "strategy_family": var.family,
            "mode": sess.mode,
            "venue": sess.venue,
            "execution_family": sess.execution_family,
            "state": sess.state,
            "asset_class": asset_class_for_symbol(sess.symbol),
            "market_open_now": market_open_now(sess.symbol),
            "created_at": sess.created_at.isoformat() if sess.created_at else None,
            "updated_at": sess.updated_at.isoformat() if sess.updated_at else None,
            "started_at": sess.started_at.isoformat() if sess.started_at else None,
            "ended_at": sess.ended_at.isoformat() if sess.ended_at else None,
            "correlation_id": sess.correlation_id,
            "source_node_id": sess.source_node_id,
            "source_paper_session_id": getattr(sess, "source_paper_session_id", None),
            "event_count": counts.get(sess.id, 0),
            "status_summary": _status_summary(sess.state),
            "warnings": _session_warnings(sess),
            "risk_status": summarize_risk_from_snapshot(sess.risk_snapshot_json),
            "paper_execution": summarize_paper_execution(sess.risk_snapshot_json),
            "live_execution": summarize_live_execution(sess.risk_snapshot_json),
            "is_paused": paused,
            "pause_info": pause_info,
            "runner_health": runner_health,
            "lane": runtime_values.get("lane"),
            "runtime": runtime_payload,
            "thesis": runtime_values.get("thesis"),
            "confidence": runtime_values.get("confidence"),
            "conviction": runtime_values.get("conviction"),
            "current_position_state": runtime_values.get("current_position_state"),
            "last_action": runtime_values.get("last_action"),
            "execution_readiness": runtime_values.get("execution_readiness_json"),
            "data_binding": binding_payload,
            "data_fidelity": data_fidelity,
            "simulated_pnl": runtime_values.get("simulated_pnl_usd"),
            "pnl": _pnl_summary(sess, runtime_values),
            "trade_count": runtime_values.get("trade_count"),
            "chart_levels": runtime_values.get("latest_levels_json"),
            "strategy_params_summary": summarize_strategy_params(var.params_json),
            "refinement_info": _variant_refinement_info(var),
            "controls": _controls_for_session(sess, paused=paused, runner_health=runner_health),
            "repeatable_edge_readiness": {
                "execution_robustness": rd.get("repeatable_edge_execution_robustness"),
                "execution_robustness_v2": rd.get("repeatable_edge_execution_robustness_v2"),
                "allocation_state": rd.get("repeatable_edge_allocation_state"),
                "live_not_recommended": rd.get("repeatable_edge_live_not_recommended"),
                "live_not_recommended_reason": rd.get("repeatable_edge_live_not_recommended_reason"),
            },
            "allocation": sess.allocation_decision_json if isinstance(getattr(sess, "allocation_decision_json", None), dict) else {},
        }
        row.update(op_fields)
        sessions_out.append(row)

    lane_status = _compute_lane_status(db, user_id=user_id)
    end_read_only_transaction(db, context="automation_sessions_lane_status")

    return {
        "sessions": sessions_out,
        "neural": neural_config_strip(),
        "governance": governance_strip(),
        "risk_policy_summary": effective_policy_summary(),
        "lane_status": lane_status,
        "limitations_note": LIMITATIONS_NOTE,
        "operator_readiness": build_momentum_operator_readiness(execution_family="coinbase_spot"),
    }


def get_automation_session_detail(db: Session, *, user_id: int, session_id: int) -> Optional[dict[str, Any]]:
    if not _tables_present(db):
        end_read_only_transaction(db, context="automation_session_detail_tables_missing")
        return None

    expire_stale_live_arm_sessions(db, user_id=user_id)

    row = (
        db.query(TradingAutomationSession, MomentumStrategyVariant)
        .join(MomentumStrategyVariant, MomentumStrategyVariant.id == TradingAutomationSession.variant_id)
        .filter(
            TradingAutomationSession.id == int(session_id),
            TradingAutomationSession.user_id == user_id,
        )
        .one_or_none()
    )
    if not row:
        return None

    sess, var = row
    events = (
        db.query(TradingAutomationEvent)
        .filter(TradingAutomationEvent.session_id == sess.id)
        .order_by(TradingAutomationEvent.ts.desc())
        .limit(80)
        .all()
    )

    via = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == sess.symbol,
            MomentumSymbolViability.variant_id == sess.variant_id,
        )
        .one_or_none()
    )
    viability_brief: Optional[dict[str, Any]] = None
    if via:
        viability_brief = {
            "viability_score": via.viability_score,
            "paper_eligible": via.paper_eligible,
            "live_eligible": via.live_eligible,
            "freshness_ts": via.freshness_ts.isoformat() if via.freshness_ts else None,
        }

    risk = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    risk_summary = {k: risk[k] for k in list(risk.keys())[:24]}
    via_full = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == sess.symbol,
            MomentumSymbolViability.variant_id == sess.variant_id,
        )
        .one_or_none()
    )

    momentum_feedback = None
    try:
        from .feedback_query import get_session_feedback_row

        momentum_feedback = get_session_feedback_row(db, session_id=sess.id)
    except Exception:
        momentum_feedback = None

    ef = (sess.execution_family or "coinbase_spot").strip().lower()
    readiness = build_momentum_operator_readiness(execution_family=ef, symbol=sess.symbol)
    readiness = merge_repeatable_edge_robustness_into_readiness(
        readiness, db, scan_pattern_id=getattr(var, "scan_pattern_id", None)
    )
    op_fields = operator_fields_for_session(sess, readiness)
    paused = is_operator_paused(sess.risk_snapshot_json)
    pause_info = operator_pause_info(sess.risk_snapshot_json)
    runner_health = _runner_health_for_mode(db, mode=sess.mode, sess=sess)
    fill_rows = []
    if _table_exists(db, "trading_automation_simulated_fills"):
        fill_rows = (
            db.query(TradingAutomationSimulatedFill)
            .filter(TradingAutomationSimulatedFill.session_id == sess.id)
            .order_by(TradingAutomationSimulatedFill.ts.desc())
            .limit(40)
            .all()
        )
    binding_row = None
    if _table_exists(db, "trading_automation_session_bindings"):
        binding_row = (
            db.query(TradingAutomationSessionBinding)
            .filter(TradingAutomationSessionBinding.session_id == sess.id)
            .one_or_none()
        )
    runtime_values = build_runtime_snapshot_values(
        sess,
        variant=var,
        viability=via_full,
        trade_count=len(fill_rows),
        execution_readiness={"operator_readiness": readiness, "blocked_reason": op_fields.get("blocked_reason")},
    )
    binding_payload = _serialize_binding(
        binding_row,
        sess=sess,
        quote_source=(runtime_values.get("metrics_json") or {}).get("paper_execution", {}).get("last_quote_source"),
        blocked_reason=op_fields.get("blocked_reason"),
    )

    src_id = getattr(sess, "source_paper_session_id", None)
    source_paper_brief = None
    if src_id:
        src = (
            db.query(TradingAutomationSession)
            .filter(TradingAutomationSession.id == int(src_id), TradingAutomationSession.user_id == user_id)
            .one_or_none()
        )
        if src:
            source_paper_brief = {
                "id": src.id,
                "symbol": src.symbol,
                "mode": src.mode,
                "state": src.state,
                "updated_at": src.updated_at.isoformat() if src.updated_at else None,
            }

    detach_loaded_instances(
        db,
        sess,
        var,
        events,
        via,
        via_full,
        fill_rows,
        binding_row,
    )
    end_read_only_transaction(db, context="automation_session_detail_reads")

    session_dict = {
        "id": sess.id,
        "symbol": sess.symbol,
        "variant_id": sess.variant_id,
        "variant": _variant_brief(var),
        "strategy_family": var.family,
        "mode": sess.mode,
        "venue": sess.venue,
        "execution_family": sess.execution_family,
        "state": sess.state,
        "asset_class": asset_class_for_symbol(sess.symbol),
        "market_open_now": market_open_now(sess.symbol),
        "created_at": sess.created_at.isoformat() if sess.created_at else None,
        "updated_at": sess.updated_at.isoformat() if sess.updated_at else None,
        "started_at": sess.started_at.isoformat() if sess.started_at else None,
        "ended_at": sess.ended_at.isoformat() if sess.ended_at else None,
        "correlation_id": sess.correlation_id,
        "source_node_id": sess.source_node_id,
        "source_paper_session_id": src_id,
        "source_paper_brief": source_paper_brief,
        "risk_snapshot_summary": risk_summary,
        "status_summary": _status_summary(sess.state),
        "warnings": _session_warnings(sess),
        "risk_status": summarize_risk_from_snapshot(sess.risk_snapshot_json),
        "paper_execution": summarize_paper_execution(sess.risk_snapshot_json),
        "live_execution": summarize_live_execution(sess.risk_snapshot_json),
        "is_paused": paused,
        "pause_info": pause_info,
        "runner_health": runner_health,
        "momentum_feedback": momentum_feedback,
        "lane": runtime_values.get("lane"),
        "runtime": {
            "seconds": runtime_values.get("runtime_seconds"),
            "started_at": sess.started_at.isoformat() if sess.started_at else None,
        },
        "thesis": runtime_values.get("thesis"),
        "confidence": runtime_values.get("confidence"),
        "conviction": runtime_values.get("conviction"),
        "current_position_state": runtime_values.get("current_position_state"),
        "last_action": runtime_values.get("last_action"),
        "execution_readiness": runtime_values.get("execution_readiness_json"),
        "data_binding": binding_payload,
        "data_fidelity": {
            "simulation_fidelity": binding_payload.get("simulation_fidelity"),
            "latency_class": binding_payload.get("latency_class"),
            "source_of_truth_provider": binding_payload.get("source_of_truth_provider"),
            "source_of_truth_exchange": binding_payload.get("source_of_truth_exchange"),
        },
        "simulated_pnl": runtime_values.get("simulated_pnl_usd"),
        "pnl": _pnl_summary(sess, runtime_values),
        "trade_count": runtime_values.get("trade_count"),
        "chart_levels": runtime_values.get("latest_levels_json"),
        "strategy_params_summary": summarize_strategy_params(var.params_json),
        "refinement_info": _variant_refinement_info(var),
        "controls": _controls_for_session(sess, paused=paused, runner_health=runner_health),
        "repeatable_edge_readiness": {
            "execution_robustness": readiness.get("repeatable_edge_execution_robustness"),
            "execution_robustness_v2": readiness.get("repeatable_edge_execution_robustness_v2"),
            "allocation_state": readiness.get("repeatable_edge_allocation_state"),
            "live_not_recommended": readiness.get("repeatable_edge_live_not_recommended"),
            "live_not_recommended_reason": readiness.get("repeatable_edge_live_not_recommended_reason"),
        },
        "allocation": sess.allocation_decision_json if isinstance(getattr(sess, "allocation_decision_json", None), dict) else {},
    }
    session_dict.update(op_fields)

    return {
        "session": session_dict,
        "events": [
            {
                "id": ev.id,
                "ts": ev.ts.isoformat() if ev.ts else None,
                "event_type": ev.event_type,
                "payload_summary": _payload_summary(ev.payload_json),
                "correlation_id": ev.correlation_id,
                "source_node_id": ev.source_node_id,
            }
            for ev in events
        ],
        "simulated_fills": [
            {
                "id": row.id,
                "ts": row.ts.isoformat() if row.ts else None,
                "lane": row.lane,
                "action": row.action,
                "fill_type": row.fill_type,
                "side": row.side,
                "quantity": row.quantity,
                "price": row.price,
                "reference_price": row.reference_price,
                "fees_usd": row.fees_usd,
                "pnl_usd": row.pnl_usd,
                "position_state_before": row.position_state_before,
                "position_state_after": row.position_state_after,
                "reason": row.reason,
                "marker_json": row.marker_json if isinstance(row.marker_json, dict) else {},
            }
            for row in fill_rows
        ],
        "viability_snapshot": viability_brief,
        "neural": neural_config_strip(),
        "governance": governance_strip(),
        "risk_policy_summary": effective_policy_summary(),
        "limitations_note": LIMITATIONS_NOTE,
        "operator_readiness": readiness,
    }


def _payload_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    keys = ("symbol", "variant_id", "reason", "note", "arm_token_prefix", "hello")
    return {k: payload[k] for k in keys if k in payload}


def get_operator_session_focus(
    db: Session,
    *,
    user_id: int,
    symbol: Optional[str] = None,
) -> dict[str, Any]:
    """Latest session the operator should care about + shared readiness (coinbase_spot default path)."""
    base_readiness = build_momentum_operator_readiness(execution_family="coinbase_spot", symbol=symbol)
    if not _tables_present(db):
        return {
            "ok": True,
            "operator_readiness": base_readiness,
            "focus_session": None,
            "events_preview": [],
            "session_lifecycle_doc": None,
        }

    expire_stale_live_arm_sessions(db, user_id=user_id)

    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.user_id == user_id,
        TradingAutomationSession.state != STATE_ARCHIVED,
    )
    if symbol:
        q = q.filter(TradingAutomationSession.symbol == symbol.strip().upper())
    rows = q.all()
    if not rows:
        return {
            "ok": True,
            "operator_readiness": base_readiness,
            "focus_session": None,
            "events_preview": [],
            "session_lifecycle_doc": None,
        }

    focus = min(rows, key=_focus_priority)
    ef = (focus.execution_family or "coinbase_spot").strip().lower()
    readiness = build_momentum_operator_readiness(execution_family=ef, symbol=focus.symbol)
    vrow = (
        db.query(MomentumStrategyVariant)
        .filter(MomentumStrategyVariant.id == int(focus.variant_id))
        .one_or_none()
    )
    readiness = merge_repeatable_edge_robustness_into_readiness(
        readiness,
        db,
        scan_pattern_id=getattr(vrow, "scan_pattern_id", None) if vrow else None,
    )

    ev_rows = (
        db.query(TradingAutomationEvent)
        .filter(TradingAutomationEvent.session_id == focus.id)
        .order_by(TradingAutomationEvent.ts.desc())
        .limit(8)
        .all()
    )
    preview = [
        {
            "id": ev.id,
            "ts": ev.ts.isoformat() if ev.ts else None,
            "event_type": ev.event_type,
            "payload_summary": _payload_summary(ev.payload_json),
        }
        for ev in ev_rows
    ]

    src_id = getattr(focus, "source_paper_session_id", None)
    source_paper_brief = None
    if src_id:
        src = (
            db.query(TradingAutomationSession)
            .filter(TradingAutomationSession.id == int(src_id), TradingAutomationSession.user_id == user_id)
            .one_or_none()
        )
        if src:
            source_paper_brief = {
                "id": src.id,
                "symbol": src.symbol,
                "mode": src.mode,
                "state": src.state,
                "updated_at": src.updated_at.isoformat() if src.updated_at else None,
            }

    snap = focus.risk_snapshot_json if isinstance(focus.risk_snapshot_json, dict) else {}
    op = operator_fields_for_session(focus, readiness)
    from .session_lifecycle import session_state_machine_doc

    focus_out = {
        "id": focus.id,
        "symbol": focus.symbol,
        "variant_id": focus.variant_id,
        "mode": focus.mode,
        "venue": focus.venue,
        "execution_family": focus.execution_family,
        "state": focus.state,
        "source_paper_session_id": src_id,
        "source_paper_brief": source_paper_brief,
        "created_at": focus.created_at.isoformat() if focus.created_at else None,
        "updated_at": focus.updated_at.isoformat() if focus.updated_at else None,
        "last_transition_at": focus.updated_at.isoformat() if focus.updated_at else None,
        "risk_status": summarize_risk_from_snapshot(focus.risk_snapshot_json),
        "paper_execution": summarize_paper_execution(snap),
        "live_execution": summarize_live_execution(snap),
        "repeatable_edge_readiness": {
            "execution_robustness": readiness.get("repeatable_edge_execution_robustness"),
            "execution_robustness_v2": readiness.get("repeatable_edge_execution_robustness_v2"),
            "allocation_state": readiness.get("repeatable_edge_allocation_state"),
            "live_not_recommended": readiness.get("repeatable_edge_live_not_recommended"),
            "live_not_recommended_reason": readiness.get("repeatable_edge_live_not_recommended_reason"),
        },
        "allocation": focus.allocation_decision_json if isinstance(getattr(focus, "allocation_decision_json", None), dict) else {},
        **op,
    }

    return {
        "ok": True,
        "operator_readiness": readiness,
        "focus_session": focus_out,
        "events_preview": preview,
        "session_lifecycle_doc": session_state_machine_doc(),
    }


def list_automation_events(
    db: Session,
    *,
    user_id: int,
    session_id: Optional[int] = None,
    event_type: Optional[str] = None,
    limit: int = 50,
) -> dict[str, Any]:
    if not _tables_present(db):
        return {"events": [], "neural": neural_config_strip()}

    q = db.query(TradingAutomationEvent).join(
        TradingAutomationSession,
        TradingAutomationSession.id == TradingAutomationEvent.session_id,
    ).filter(TradingAutomationSession.user_id == user_id)

    if session_id is not None:
        q = q.filter(TradingAutomationEvent.session_id == int(session_id))
    if event_type:
        q = q.filter(TradingAutomationEvent.event_type == event_type.strip())

    rows = q.order_by(TradingAutomationEvent.ts.desc()).limit(min(max(limit, 1), 200)).all()
    return {
        "events": [
            {
                "id": ev.id,
                "session_id": ev.session_id,
                "ts": ev.ts.isoformat() if ev.ts else None,
                "event_type": ev.event_type,
                "payload_summary": _payload_summary(ev.payload_json),
                "correlation_id": ev.correlation_id,
            }
            for ev in rows
        ],
        "neural": neural_config_strip(),
    }


def automation_summary(db: Session, *, user_id: int) -> dict[str, Any]:
    pipeline_health = get_viability_pipeline_health(db)
    if not _tables_present(db):
        out = neural_config_strip()
        out.update(
            {
                "total_sessions": 0,
                "pending_paper_drafts": 0,
                "paper_runner_queued": 0,
                "paper_runner_active": 0,
                "live_runner_queued": 0,
                "live_runner_active": 0,
                "pending_live_arms": 0,
                "armed_awaiting_runner": 0,
                "cancelled": 0,
                "archived": 0,
                "expired": 0,
                "last_event_ts": None,
                "limitations_note": LIMITATIONS_NOTE,
                "governance": governance_strip(),
                "risk_policy_summary": effective_policy_summary(),
                "operator_readiness": build_momentum_operator_readiness(execution_family="coinbase_spot"),
                "paper_runner_health": _runner_health_for_mode(db, mode="paper"),
                "live_runner_health": _runner_health_for_mode(db, mode="live"),
                "viability_pipeline": pipeline_health,
            }
        )
        return out

    expire_stale_live_arm_sessions(db, user_id=user_id)

    base = db.query(TradingAutomationSession).filter(TradingAutomationSession.user_id == user_id)
    total = base.count()
    pending_draft = base.filter(TradingAutomationSession.state == STATE_DRAFT).count()
    paper_queued = base.filter(
        TradingAutomationSession.mode == "paper",
        TradingAutomationSession.state == STATE_QUEUED,
    ).count()
    paper_active = base.filter(TradingAutomationSession.state.in_(PAPER_RUNNER_ACTIVE_STATES)).count()
    pending_arm = base.filter(TradingAutomationSession.state == STATE_LIVE_ARM_PENDING).count()
    armed = base.filter(TradingAutomationSession.state == STATE_ARMED_PENDING_RUNNER).count()
    live_queued = base.filter(
        TradingAutomationSession.mode == "live",
        TradingAutomationSession.state == STATE_QUEUED_LIVE,
    ).count()
    live_active = base.filter(TradingAutomationSession.state.in_(LIVE_RUNNER_ACTIVE_SUMMARY_STATES)).count()
    cancelled = base.filter(
        TradingAutomationSession.state.in_((STATE_CANCELLED, STATE_LIVE_CANCELLED))
    ).count()
    archived = base.filter(TradingAutomationSession.state == STATE_ARCHIVED).count()
    expired = base.filter(TradingAutomationSession.state == STATE_EXPIRED).count()

    last_ev = (
        db.query(TradingAutomationEvent.ts)
        .join(TradingAutomationSession, TradingAutomationSession.id == TradingAutomationEvent.session_id)
        .filter(TradingAutomationSession.user_id == user_id)
        .order_by(TradingAutomationEvent.ts.desc())
        .limit(1)
        .scalar()
    )

    summary = neural_config_strip()
    summary.update(
        {
            "total_sessions": total,
            "pending_paper_drafts": pending_draft,
            "paper_runner_queued": paper_queued,
            "paper_runner_active": paper_active,
            "pending_live_arms": pending_arm,
            "armed_awaiting_runner": armed,
            "live_runner_queued": live_queued,
            "live_runner_active": live_active,
            "cancelled": cancelled,
            "archived": archived,
            "expired": expired,
            "last_event_ts": last_ev.isoformat() if last_ev else None,
            "limitations_note": LIMITATIONS_NOTE,
            "governance": governance_strip(),
            "risk_policy_summary": effective_policy_summary(),
            "operator_readiness": build_momentum_operator_readiness(execution_family="coinbase_spot"),
            "paper_runner_health": _runner_health_for_mode(db, mode="paper"),
            "live_runner_health": _runner_health_for_mode(db, mode="live"),
            "viability_pipeline": pipeline_health,
            "lanes": {
                "simulation": pending_draft + paper_queued + paper_active,
                "live-armed": pending_arm + armed,
                "live": live_queued + live_active,
            },
        }
    )
    return summary


def _clone_session_for_run(
    db: Session,
    *,
    user_id: int,
    sess: TradingAutomationSession,
) -> TradingAutomationSession:
    dup = _find_duplicate_active_session(db, user_id=user_id, sess=sess)
    if dup is not None:
        return dup
    new_state = STATE_QUEUED if sess.mode == "paper" else STATE_QUEUED_LIVE
    clone = create_trading_automation_session(
        db,
        user_id=user_id,
        venue=sess.venue,
        execution_family=sess.execution_family,
        mode=sess.mode,
        symbol=sess.symbol,
        variant_id=int(sess.variant_id),
        state=new_state,
        risk_snapshot_json=_fresh_run_snapshot(sess),
        correlation_id=str(uuid.uuid4()),
        source_node_id="momentum_automation_monitor",
        source_paper_session_id=getattr(sess, "source_paper_session_id", None),
    )
    append_trading_automation_event(
        db,
        clone.id,
        "session_cloned_for_run",
        {"source_session_id": int(sess.id), "source_state": sess.state},
        correlation_id=clone.correlation_id,
        source_node_id="momentum_automation_monitor",
    )
    return clone


def run_automation_session(db: Session, *, user_id: int, session_id: int) -> dict[str, Any]:
    if not _tables_present(db):
        return {"ok": False, "error": "tables_missing"}
    sess = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == int(session_id), TradingAutomationSession.user_id == user_id)
        .one_or_none()
    )
    if not sess:
        return {"ok": False, "error": "not_found"}

    # A terminal LIVE row is historical broker identity, not a reusable template.
    # Re-arming must mint a fresh token/claim/session through the live-arm flow.
    if sess.mode == "live" and (
        sess.state in TERMINAL_STATES or sess.state == STATE_ARCHIVED
    ):
        return {
            "ok": False,
            "error": "live_rearm_required",
            "state": sess.state,
            "message": "Terminal live sessions require a fresh live-arm confirmation.",
        }

    runner_health = _runner_health_for_mode(db, mode=sess.mode, sess=sess)
    if not runner_health.get("enabled"):
        return {"ok": False, "error": "runner_disabled", "runner_health": runner_health}

    target = sess
    paused = is_operator_paused(sess.risk_snapshot_json)
    if paused:
        sess.risk_snapshot_json = clear_operator_pause(sess.risk_snapshot_json)
        sess.updated_at = datetime.utcnow()
        append_trading_automation_event(
            db,
            sess.id,
            "session_resumed",
            {"by": "operator_run"},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_automation_monitor",
        )
    elif sess.state in TERMINAL_STATES or sess.state == STATE_ARCHIVED:
        target = _clone_session_for_run(db, user_id=user_id, sess=sess)
    elif sess.mode == "paper" and sess.state in (STATE_DRAFT, STATE_IDLE):
        prev = sess.state
        sess.state = STATE_QUEUED
        sess.updated_at = datetime.utcnow()
        append_trading_automation_event(
            db,
            sess.id,
            "session_run_requested",
            {"previous_state": prev, "mode": "paper"},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_automation_monitor",
        )
    elif sess.mode == "live" and sess.state == STATE_ARMED_PENDING_RUNNER:
        sess.state = STATE_QUEUED_LIVE
        sess.updated_at = datetime.utcnow()
        append_trading_automation_event(
            db,
            sess.id,
            "session_run_requested",
            {"previous_state": STATE_ARMED_PENDING_RUNNER, "mode": "live"},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_automation_monitor",
        )
    elif sess.mode == "paper" and sess.state not in PAPER_RUNNER_RUNNABLE_STATES:
        return {"ok": False, "error": "not_runnable", "state": sess.state}
    elif sess.mode == "live" and sess.state not in LIVE_RUNNER_RUNNABLE_STATES:
        return {"ok": False, "error": "not_runnable", "state": sess.state}

    if target.id != sess.id:
        # The clone and its fresh broker-ownership identity are not durable until
        # the API transaction commits. Never let a newly cloned live row cross a
        # broker boundary while it is invisible to independent claim transactions.
        db.flush()
        tick_result = {"ok": True, "skipped": "cloned_session_awaiting_commit"}
    elif target.mode == "paper":
        tick_result = tick_paper_session(db, int(target.id))
    else:
        tick_result = tick_live_session(db, int(target.id))
    return {
        "ok": True,
        "session_id": int(target.id),
        "cloned_from_session_id": int(sess.id) if target.id != sess.id else None,
        "state": target.state,
        "tick_result": tick_result,
    }


def request_flatten_session(db: Session, *, user_id: int, session_id: int) -> dict[str, Any]:
    """Operator FLATTEN: request a system-mediated market exit of a held LIVE
    position. Sets a flag the runner honors on its next tick (<=15s) so the exit
    flows through the ONE chokepoint chain — cancel scale-out, clamp to broker
    qty, place, confirm, reconcile — instead of a manual broker-app sell racing
    the system's own resting orders (2026-06-11 CPSH/SNDG lesson)."""
    sess = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.id == int(session_id),
            TradingAutomationSession.user_id == int(user_id),
        )
        .one_or_none()
    )
    if not sess:
        return {"ok": False, "error": "not_found"}
    if sess.mode != "live" or sess.state not in (
        "live_entered", "live_scaling_out", "live_trailing", "live_bailout",
    ):
        return {"ok": False, "error": "not_flattenable", "state": sess.state}
    if normalize_execution_family(sess.execution_family) in {
        "alpaca_spot",
        "alpaca_short",
    }:
        quarantine_reason = _persisted_alpaca_execution_quarantine_reason(sess)
        if quarantine_reason is not None:
            return _quarantine_operator_stop_execution(
                db,
                sess,
                reason=quarantine_reason,
            )
    snap = dict(sess.risk_snapshot_json or {})
    le = dict(snap.get("momentum_live_execution") or {})
    le["operator_flatten_requested_utc"] = datetime.utcnow().isoformat()
    snap["momentum_live_execution"] = le
    sess.risk_snapshot_json = snap
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(sess, "risk_snapshot_json")
    except Exception:
        pass
    sess.updated_at = datetime.utcnow()
    from .persistence import append_trading_automation_event

    append_trading_automation_event(
        db,
        sess.id,
        "operator_flatten_requested",
        {"by": "operator", "state": sess.state},
        correlation_id=sess.correlation_id,
        source_node_id="momentum_automation_monitor",
    )
    return {"ok": True, "session_id": int(sess.id), "state": sess.state,
            "message": "Flatten requested — the runner exits through the system on its next tick."}


def pause_automation_session(db: Session, *, user_id: int, session_id: int) -> dict[str, Any]:
    if not _tables_present(db):
        return {"ok": False, "error": "tables_missing"}
    sess = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == int(session_id), TradingAutomationSession.user_id == user_id)
        .one_or_none()
    )
    if not sess:
        return {"ok": False, "error": "not_found"}
    if sess.state not in PAUSABLE_STATES:
        return {"ok": False, "error": "not_pausable", "state": sess.state}
    if is_operator_paused(sess.risk_snapshot_json):
        return {"ok": False, "error": "already_paused"}
    sess.risk_snapshot_json = apply_operator_pause(sess.risk_snapshot_json, state=sess.state)
    sess.updated_at = datetime.utcnow()
    append_trading_automation_event(
        db,
        sess.id,
        "session_paused",
        {"state": sess.state},
        correlation_id=sess.correlation_id,
        source_node_id="momentum_automation_monitor",
    )
    return {"ok": True, "session_id": int(sess.id), "state": sess.state, "pause_info": operator_pause_info(sess.risk_snapshot_json)}


def resume_automation_session(db: Session, *, user_id: int, session_id: int) -> dict[str, Any]:
    if not _tables_present(db):
        return {"ok": False, "error": "tables_missing"}
    sess = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == int(session_id), TradingAutomationSession.user_id == user_id)
        .one_or_none()
    )
    if not sess:
        return {"ok": False, "error": "not_found"}
    if not is_operator_paused(sess.risk_snapshot_json):
        return {"ok": False, "error": "not_paused"}
    return run_automation_session(db, user_id=user_id, session_id=int(sess.id))


def _quarantine_operator_stop_execution(
    db: Session,
    sess: TradingAutomationSession,
    *,
    reason: str,
) -> dict[str, Any]:
    """Durably pause an uncertified Alpaca row without touching the broker.

    The HTTP Stop/Cancel routes commit this deferred result.  The quarantine
    annotation alone is not an execution fence: a later config correction could
    make the still-runnable row eligible again.  Persist the operator pause on
    every call so no new entry work can resume without a new explicit Run action.
    """
    snap = dict(sess.risk_snapshot_json or {})
    le = dict(snap.get(KEY_LIVE_EXEC) or {})
    prior = le.get("operator_stop_execution_quarantine")
    changed = not isinstance(prior, dict) or prior.get("reason") != reason
    if changed:
        quarantine = {
            "reason": str(reason),
            "execution_family": normalize_execution_family(sess.execution_family),
            "symbol": str(sess.symbol or "").upper(),
            "quarantined_at_utc": datetime.utcnow().isoformat(),
        }
        le["operator_stop_execution_quarantine"] = quarantine
    else:
        quarantine = dict(prior)
    snap[KEY_LIVE_EXEC] = le
    sess.risk_snapshot_json = apply_operator_pause(snap, state=sess.state)
    sess.updated_at = datetime.utcnow()
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(sess, "risk_snapshot_json")
    except Exception:
        pass
    if changed:
        append_trading_automation_event(
            db,
            int(sess.id),
            "operator_stop_execution_quarantined",
            quarantine,
            correlation_id=sess.correlation_id,
            source_node_id="momentum_automation_monitor",
        )
    return {
        "ok": True,
        "pending": "execution_quarantine",
        "terminalization_deferred": True,
        "session_id": int(sess.id),
        "state": sess.state,
        "quarantine_reason": str(reason),
        "message": "No broker execution was attempted for this uncertified Alpaca execution shape.",
    }


def _flatten_live_session_for_stop(
    db: Session,
    sess: TradingAutomationSession,
    *,
    request_kind: str = "stop",
) -> dict[str, Any]:
    """Route operator STOP through the crash-safe emergency exit state machine.

    This helper deliberately performs no direct cancel/sell.  It first persists the
    same request consumed by ``tick_live_session``; the runner then owns exact-CID
    recovery, signed broker quantity, close intent, fill accounting, and the final
    broker-zero proof.  A live session with no local exposure may terminalize only
    after an independent successful broker-flat read.
    """
    quarantine_reason = _persisted_alpaca_execution_quarantine_reason(sess)
    if quarantine_reason is not None:
        quarantined = _quarantine_operator_stop_execution(
            db,
            sess,
            reason=quarantine_reason,
        )
        return {
            **quarantined,
            "action": "execution_quarantined",
            "terminalization_deferred": True,
        }
    snap = dict(sess.risk_snapshot_json or {})
    le = snap.get(KEY_LIVE_EXEC)
    le = dict(le) if isinstance(le, dict) else {}
    pos = le.get("position")
    entry_order_id = str(le.get("entry_order_id") or "").strip()
    entry_client_order_id = str(le.get("entry_client_order_id") or "").strip()
    reconcile_client_order_id = str(
        le.get("entry_reconcile_pending_client_order_id") or ""
    ).strip()
    resolved_entry_orders = le.get("entry_orders_resolved")
    resolved_entry_orders = (
        resolved_entry_orders if isinstance(resolved_entry_orders, dict) else {}
    )
    entry_order_ids_all = [
        str(value or "").strip()
        for value in (le.get("entry_order_ids_all") or [])
        if str(value or "").strip()
        and str(value or "").strip() not in resolved_entry_orders
    ]
    unresolved_entry_order_id = bool(
        entry_order_id and entry_order_id not in resolved_entry_orders
    )
    pre_entry_state = sess.state in {
        STATE_ARMED_PENDING_RUNNER,
        STATE_QUEUED_LIVE,
        STATE_WATCHING_LIVE,
        STATE_LIVE_ENTRY_CANDIDATE,
        STATE_LIVE_PENDING_ENTRY,
    }
    emergency_service_state = sess.state in {
        STATE_LIVE_PENDING_ENTRY,
        STATE_LIVE_ENTERED,
        STATE_LIVE_SCALING_OUT,
        STATE_LIVE_TRAILING,
        STATE_LIVE_BAILOUT,
    }
    unresolved_entry_identity = bool(
        unresolved_entry_order_id
        or entry_order_ids_all
        or (
            pre_entry_state
            and (
                entry_client_order_id
                or reconcile_client_order_id
                or le.get("entry_submitted")
            )
        )
    )
    local_exposure_possible = bool(unresolved_entry_identity or isinstance(pos, dict))

    if emergency_service_state or local_exposure_possible:
        request_kind = "cancel" if str(request_kind).lower() == "cancel" else "stop"
        marker_key = f"operator_{request_kind}_reconcile_requested_utc"
        newly_requested = not bool(le.get(marker_key))
        if newly_requested:
            le[marker_key] = datetime.utcnow().isoformat()
        le.setdefault("operator_flatten_requested_utc", datetime.utcnow().isoformat())
        le[f"operator_{request_kind}_requested"] = True
        snap[KEY_LIVE_EXEC] = le
        # Freeze every pre-entry route while the exact legacy CID/OID is unknown.
        # Held/pending-entry sessions remain exit-serviceable because the runner's
        # paused-session gate explicitly permits durable emergency authority.
        sess.risk_snapshot_json = apply_operator_pause(snap, state=sess.state)
        try:
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(sess, "risk_snapshot_json")
        except Exception:
            pass
        if newly_requested:
            sess.updated_at = datetime.utcnow()
            append_trading_automation_event(
                db,
                int(sess.id),
                f"operator_{request_kind}_emergency_requested",
                {
                    "state": sess.state,
                    "entry_order_id": entry_order_id or None,
                    "entry_client_order_id": entry_client_order_id or None,
                    "entry_reconcile_pending_client_order_id": (
                        reconcile_client_order_id or None
                    ),
                    "entry_order_ids_all": entry_order_ids_all,
                    "local_position_present": isinstance(pos, dict),
                },
                correlation_id=sess.correlation_id,
                source_node_id="momentum_automation_monitor",
            )
        db.flush()
        if not emergency_service_state:
            return {
                "ok": True,
                "action": "entry_order_reconcile_requested",
                "pending": "entry_order_truth_reconcile",
                "terminalization_deferred": True,
                "request_created": newly_requested,
                "service_result": {
                    "ok": True,
                    "skipped": "paused_preentry_identity_requires_exact_reconcile",
                },
                "state": sess.state,
            }
        try:
            service_result = tick_live_session(db, int(sess.id))
        except Exception as exc:
            logger.warning(
                "[automation_query] operator-stop emergency service failed session=%s",
                sess.id,
                exc_info=True,
            )
            service_result = {"ok": False, "error": type(exc).__name__}
        return {
            "ok": True,
            "action": "emergency_exit_requested",
            "pending": "broker_flat_confirmation",
            "terminalization_deferred": True,
            "request_created": newly_requested,
            "service_result": service_result,
            "state": sess.state,
        }

    if normalize_execution_family(sess.execution_family) not in {
        "alpaca_spot",
        "alpaca_short",
    }:
        terminal_truth = _non_alpaca_live_terminalization_proof(
            db,
            sess,
            context=f"operator_{str(request_kind).lower()}_pre_terminal",
            cancelled_by="operator",
        )
        if terminal_truth.get("state") == "safe":
            return {
                "ok": True,
                "action": "strict_broker_terminal_truth",
                "broker_flat_confirmed": True,
                "terminalization_truth": terminal_truth.get("proof"),
            }
        if terminal_truth.get("state") == "managed":
            return {
                "ok": True,
                "action": "filled_entry_adopted",
                "pending": "filled_entry_management",
                "terminalization_deferred": True,
                "management_result": terminal_truth.get("result"),
            }
        return {
            **terminal_truth,
            "action": "strict_broker_terminal_truth_pending",
            "terminalization_deferred": True,
        }

    flat, detail = _reaper_broker_position_truth(sess)
    if flat is True:
        return {
            "ok": True,
            "action": "no_live_orders",
            "broker_flat_confirmed": True,
        }
    if flat is None and "mismatch" in str(detail.get("reason") or ""):
        _quarantine_reaper_direction_mismatch(db, sess, detail)
    return {
        "ok": True,
        "action": "broker_flat_unconfirmed",
        "pending": "broker_flat_confirmation",
        "terminalization_deferred": True,
        "broker_truth": detail,
    }


def _owned_unresolved_alpaca_entry_claim(
    db: Session,
    sess: TradingAutomationSession,
) -> tuple[bool, dict[str, Any] | None]:
    """Read crash-surviving entry ownership independently of session JSON."""
    if normalize_execution_family(sess.execution_family) not in {
        "alpaca_spot", "alpaca_short",
    }:
        return True, None
    from .alpaca_orphan_claims import read_action_claim

    scope = _frozen_alpaca_account_scope(sess)
    if scope != "alpaca:paper":
        return False, None
    readable, claim = read_action_claim(
        db,
        symbol=sess.symbol,
        account_scope=scope,
    )
    if not readable:
        return False, None
    if not claim or claim.get("phase") == "resolved" or claim.get("action") != "entry":
        return True, None
    if claim.get("owner_session_id") != int(sess.id):
        return True, None
    return True, claim


def _pause_operator_terminalization(
    sess: TradingAutomationSession,
) -> None:
    """Durably fence new entry work while operator terminal truth is pending.

    Stop/cancel endpoints commit ``ok=True`` deferred results.  Every such result
    must therefore carry the operator pause in the same transaction; otherwise a
    still-runnable pre-entry session can reserve a fresh broker CID after the human
    already asked it to stop.
    """
    sess.risk_snapshot_json = apply_operator_pause(
        sess.risk_snapshot_json,
        state=sess.state,
    )
    sess.updated_at = datetime.utcnow()
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(sess, "risk_snapshot_json")
    except Exception:
        pass


def _exact_pre_http_operator_claim(
    sess: TradingAutomationSession,
    claim: dict[str, Any],
) -> bool:
    snapshot = (
        sess.risk_snapshot_json
        if isinstance(sess.risk_snapshot_json, dict)
        else {}
    )
    live_exec = snapshot.get(KEY_LIVE_EXEC)
    live_exec = live_exec if isinstance(live_exec, dict) else {}
    return _is_exact_pre_http_alpaca_arm_claim(
        sess,
        claim,
        le=live_exec,
    )


def stop_automation_session(db: Session, *, user_id: int, session_id: int) -> dict[str, Any]:
    if not _tables_present(db):
        return {"ok": False, "error": "tables_missing"}
    _q = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == int(session_id), TradingAutomationSession.user_id == user_id)
    )
    try:
        _q = _q.with_for_update()
    except Exception:
        pass
    try:
        _q = _q.populate_existing()
    except Exception:
        pass
    sess = _q.one_or_none()
    if not sess:
        return {"ok": False, "error": "not_found"}
    if sess.state == STATE_ARCHIVED or sess.state in TERMINAL_STATES:
        return {"ok": False, "error": "already_terminal", "state": sess.state}
    initial_state = sess.state

    live_stop = None
    if sess.mode == "live":
        _execution_quarantine = _persisted_alpaca_execution_quarantine_reason(sess)
        if _execution_quarantine is not None:
            return _quarantine_operator_stop_execution(
                db,
                sess,
                reason=_execution_quarantine,
            )
        _claim_readable, _durable_claim = _owned_unresolved_alpaca_entry_claim(db, sess)
        if not _claim_readable:
            return {
                "ok": False,
                "error": "alpaca_entry_claim_unreadable",
                "message": "Durable broker-entry ownership is unreadable; the session was not terminalized.",
            }
        if _durable_claim is not None:
            if (
                _durable_claim.get("client_order_id")
                or _durable_claim.get("broker_order_id")
            ):
                _pause_operator_terminalization(sess)
                db.flush()
                return {
                    "ok": True,
                    "pending": "durable_alpaca_entry_claim_reconcile",
                    "terminalization_deferred": True,
                    "session_id": int(sess.id),
                    "state": sess.state,
                    "message": (
                        "Broker entry truth is unresolved; new entry work is paused "
                        "and the session remains non-terminal."
                    ),
                }
            if not _exact_pre_http_operator_claim(
                sess,
                _durable_claim,
            ):
                _pause_operator_terminalization(sess)
                db.flush()
                return {
                    "ok": True,
                    "pending": "durable_alpaca_entry_claim_reconcile",
                    "terminalization_deferred": True,
                    "session_id": int(sess.id),
                    "state": sess.state,
                    "message": (
                        "The CID-less broker permit is not exact no-transport proof; "
                        "new entry work is paused pending reconciliation."
                    ),
                }
            _retire_confirmed_pre_http_alpaca_claim_before_terminal(
                db,
                sess,
                new_state=STATE_LIVE_CANCELLED,
            )
            live_stop = {
                "ok": True,
                "action": "confirmed_pre_http_no_transport",
                "terminalization_no_transport_proven": True,
            }
        else:
            live_stop = _flatten_live_session_for_stop(db, sess)
        if not live_stop.get("ok"):
            return live_stop
        if live_stop.get("terminalization_deferred"):
            _pause_operator_terminalization(sess)
            db.flush()
            return {
                "ok": True,
                "pending": live_stop.get("pending") or "broker_flat_confirmation",
                "session_id": int(sess.id),
                "state": sess.state,
                "live_stop": live_stop,
                "message": (
                    "Emergency exit is durable and remains under broker reconciliation; "
                    "the session was not terminalized early."
                ),
            }
        if not (
            live_stop.get("broker_flat_confirmed")
            or live_stop.get("terminalization_no_transport_proven")
        ):
            return {
                "ok": False,
                "error": "broker_flat_unconfirmed",
                "session_id": int(sess.id),
                "state": sess.state,
                "live_stop": live_stop,
            }

        # Flat/order truth is valid only for the still-configured frozen account.
        _execution_quarantine = _persisted_alpaca_execution_quarantine_reason(sess)
        if _execution_quarantine is not None:
            return _quarantine_operator_stop_execution(
                db,
                sess,
                reason=_execution_quarantine,
            )
        if normalize_execution_family(sess.execution_family) not in {
            "alpaca_spot",
            "alpaca_short",
        } and (
            sess.state != initial_state
            or not _non_alpaca_terminal_proof_matches_session(
                sess,
                live_stop.get("terminalization_truth"),
            )
        ):
            return _persist_non_alpaca_terminalization_quarantine(
                db,
                sess,
                reason="terminalization_session_generation_changed",
                context="stop_automation_session_pre_terminal_mutation",
                detail={
                    "expected_state": initial_state,
                    "current_state": sess.state,
                },
            )

    now = datetime.utcnow()
    prev = sess.state

    final_account_quarantine = _final_non_alpaca_terminal_account_quarantine(
        db,
        sess,
        context="stop_automation_session_final_account_fence",
    )
    if final_account_quarantine is not None:
        return {
            **final_account_quarantine,
            "live_stop": final_account_quarantine,
        }

    terminal_state = (
        STATE_LIVE_CANCELLED if sess.mode == "live" else STATE_CANCELLED
    )
    if (
        sess.mode == "live"
        and normalize_execution_family(sess.execution_family)
        in {"alpaca_spot", "alpaca_short"}
    ):
        _retire_confirmed_pre_http_alpaca_claim_before_terminal(
            db,
            sess,
            new_state=terminal_state,
        )
    sess.state = terminal_state
    sess.ended_at = now
    sess.updated_at = now
    sess.risk_snapshot_json = clear_operator_pause(sess.risk_snapshot_json)
    append_trading_automation_event(
        db,
        sess.id,
        "session_stopped",
        {"previous_state": prev, "terminal_state": sess.state, "live_stop": live_stop},
        correlation_id=sess.correlation_id,
        source_node_id="momentum_automation_monitor",
    )
    try:
        from .feedback_emit import emit_feedback_after_terminal_transition

        emit_feedback_after_terminal_transition(db, sess)
    except Exception:
        pass
    return {"ok": True, "session_id": int(sess.id), "state": sess.state, "live_stop": live_stop}


def delete_automation_session(db: Session, *, user_id: int, session_id: int) -> dict[str, Any]:
    return archive_automation_session(db, user_id=user_id, session_id=session_id)


def _try_adopt_filled_entry_on_cancel(
    db: Session,
    sess: TradingAutomationSession,
    *,
    cancelled_by: str = "automation_monitor",
    adapter_override: Any | None = None,
    strict_filled_order: Any | None = None,
) -> Optional[dict[str, Any]]:
    """If a LIVE session being cancelled has an entry order that actually FILLED at
    the broker, ADOPT it instead of orphaning the position. Mirrors
    ``live_runner._sweep_unresolved_entry_orders``: re-point the session at the real
    order, mark it resolved 'adopted', and walk the legal live FSM
    (watching -> entry_candidate -> pending_entry) so the hardened pending-entry
    fill-handler attaches the lane stop/target on the next tick.

    Returns the adopt result dict when a fill was adopted (caller returns it and does
    NOT mark the session terminal); returns ``None`` to fall through to the normal
    cancel path (no fill, indeterminate broker I/O, or nothing to adopt).

    For a known broker order id, an indeterminate ``get_order`` keeps the legacy
    cleanup/reconciler behavior. For an ack-lost submit that has only a deterministic
    client id, an indeterminate lookup defers terminal cancellation: labeling it
    pre-entry while a fill may exist would recreate the orphan. IDEMPOTENT: orders
    the runner already resolved are skipped, so a runner-vs-cancel race cannot
    double-adopt.
    """
    # Lazy imports (live_runner imports from this module transitively — keep the
    # dependency one-directional at module load, matching the file's convention).
    from .live_runner import (
        KEY_LIVE_EXEC,
        _bind_recovered_entry_order,
        _commit_le,
        _emit,
        _mark_entry_order_resolved,
        _recover_entry_order_by_client_id,
        _safe_transition,
    )

    snap = sess.risk_snapshot_json or {}
    le = snap.get(KEY_LIVE_EXEC) if isinstance(snap, dict) else None
    le = le if isinstance(le, dict) else {}

    # Candidate entry order ids = the active pointer PLUS any placed-but-unresolved
    # ids from history. Reuse the resolved-map so an order the runner already adopted
    # (resolved 'adopted') is skipped -> idempotent under a concurrent tick.
    resolved = le.get("entry_orders_resolved") or {}
    candidates: list[str] = []
    for _o in [le.get("entry_order_id")] + list(le.get("entry_order_ids_all") or []):
        _os = str(_o or "").strip()
        if _os and _os not in candidates and _os not in resolved:
            candidates.append(_os)
    from ..venue.factory import get_adapter

    adapter = adapter_override or get_adapter(sess.execution_family)
    if adapter is None:
        return None  # no adapter -> cannot confirm a fill -> normal cancel path

    def _generation_quarantine() -> Optional[dict[str, Any]]:
        """Fail closed if this stored account generation no longer owns reads."""
        family = normalize_execution_family(sess.execution_family)
        if family in {"alpaca_spot", "alpaca_short"}:
            reason = _persisted_alpaca_execution_quarantine_reason(sess)
            if reason is None:
                return None
            return _quarantine_operator_stop_execution(db, sess, reason=reason)
        truth = verify_frozen_non_alpaca_account_identity(
            sess,
            adapter=adapter,
        )
        if truth.get("ok") is True:
            return None
        return _persist_non_alpaca_terminalization_quarantine(
            db,
            sess,
            reason=str(
                truth.get("reason") or "non_alpaca_account_identity_unknown"
            ),
            context=f"{cancelled_by}_filled_entry_adoption",
            detail={"phase": "filled_entry_adoption_generation_fence"},
        )

    # This helper is also exercised directly by reconciliation tests/callers; do
    # not rely solely on cancel_automation_session's earlier account-pin gate.
    generation_quarantine = _generation_quarantine()
    if generation_quarantine is not None:
        return generation_quarantine
    if normalize_execution_family(sess.execution_family) in {
        "alpaca_spot",
        "alpaca_short",
    }:
        if not _bind_persisted_alpaca_adapter(sess, adapter):
            return _quarantine_operator_stop_execution(
                db,
                sess,
                reason="alpaca_adapter_account_generation_bind_failed",
            )
        generation_quarantine = _generation_quarantine()
        if generation_quarantine is not None:
            return generation_quarantine

    # An ack-lost Alpaca submit can have only the deterministic client id in the
    # session.  Recover the broker order before allowing cancellation to label the
    # session "pre-entry".  If broker truth is temporarily unavailable, defer the
    # terminal transition: cancelling blind can orphan a real fill.
    if not candidates:
        reconcile_cid = (
            le.get("entry_reconcile_pending_client_order_id")
            or le.get("entry_client_order_id")
        )
        generation_quarantine = _generation_quarantine()
        if generation_quarantine is not None:
            return generation_quarantine
        recovered = _recover_entry_order_by_client_id(adapter, reconcile_cid)
        # The configured account may rotate while the CID read is in flight.
        # Re-check before binding any returned identity into this generation.
        generation_quarantine = _generation_quarantine()
        if generation_quarantine is not None:
            return generation_quarantine
        if recovered is not None:
            recovered_le = dict(le)
            recovered_le["entry_orders_resolved"] = dict(
                recovered_le.get("entry_orders_resolved") or {}
            )
            recovered_le["entry_order_ids_all"] = list(
                recovered_le.get("entry_order_ids_all") or []
            )
            recovered_oid = _bind_recovered_entry_order(
                recovered_le, recovered, client_order_id=reconcile_cid
            )
            generation_quarantine = _generation_quarantine()
            if generation_quarantine is not None:
                return generation_quarantine
            _commit_le(sess, recovered_le)
            le = recovered_le
            _emit(db, sess, "entry_client_id_recovered_on_cancel", {
                "client_order_id": le.get("entry_client_order_id") or reconcile_cid,
                "order_id": recovered_oid,
                "venue_status": getattr(recovered, "status", None),
            })
            candidates.append(recovered_oid)
        elif le.get("entry_submitted") and reconcile_cid:
            pending_le = dict(le)
            pending_le["entry_reconcile_pending_client_order_id"] = str(reconcile_cid)
            generation_quarantine = _generation_quarantine()
            if generation_quarantine is not None:
                return generation_quarantine
            _commit_le(sess, pending_le)
            le = pending_le
            _emit(db, sess, "entry_cancel_deferred_client_id_reconcile", {
                "client_order_id": str(reconcile_cid),
                "by": cancelled_by,
            })
            db.flush()
            return {
                "ok": True,
                "pending": "entry_client_id_reconcile",
                "session_id": int(sess.id),
                "state": sess.state,
            }
    if not candidates:
        return None

    for oid in candidates:
        generation_quarantine = _generation_quarantine()
        if generation_quarantine is not None:
            return generation_quarantine
        strict_oid = str(
            getattr(strict_filled_order, "order_id", "") or ""
        ).strip()
        if strict_filled_order is not None and strict_oid == str(oid):
            no = strict_filled_order
        else:
            try:
                no, _ = adapter.get_order(str(oid))
            except Exception:
                generation_quarantine = _generation_quarantine()
                if generation_quarantine is not None:
                    return generation_quarantine
                # FAIL-OPEN: indeterminate -> leave unresolved, do not adopt this order.
                logger.debug(
                    "[automation_query] adopt-on-cancel get_order failed for %s",
                    oid, exc_info=True,
                )
                continue
        if no is None:
            generation_quarantine = _generation_quarantine()
            if generation_quarantine is not None:
                return generation_quarantine
            continue
        # Broker truth belongs only to the configured account generation under
        # which it was read.  Never adopt a fill after an in-flight pin rotation.
        generation_quarantine = _generation_quarantine()
        if generation_quarantine is not None:
            return generation_quarantine
        filled = float(getattr(no, "filled_size", 0) or 0)
        if filled <= 0:
            continue
        if normalize_execution_family(sess.execution_family) not in {
            "alpaca_spot",
            "alpaca_short",
        }:
            adoption_identities = _collect_non_alpaca_persisted_order_identities(
                sess
            )
            adoption_authorized, adoption_detail = (
                _exact_non_alpaca_order_authority(
                    no,
                    order_expectations=dict(
                        adoption_identities.get("order_expectations") or {}
                    ),
                    required_intent="entry",
                    expected_symbol=str(sess.symbol or ""),
                )
            )
            if not adoption_authorized:
                return _persist_non_alpaca_terminalization_quarantine(
                    db,
                    sess,
                    reason=(
                        "terminalization_filled_entry_adoption_authority_unproven"
                    ),
                    context=f"{cancelled_by}_filled_entry_adoption",
                    detail=adoption_detail,
                )
        # LATE/RACED FILL — ADOPT. Re-point + walk the legal FSM to pending-entry.
        venue_status = str(getattr(no, "status", "") or "")
        adopted_le = dict(le)
        adopted_le["entry_orders_resolved"] = dict(
            adopted_le.get("entry_orders_resolved") or {}
        )
        adopted_le["entry_order_ids_all"] = list(
            adopted_le.get("entry_order_ids_all") or []
        )
        adopted_le["entry_order_id"] = str(oid)
        adopted_le["entry_submitted"] = True
        _mark_entry_order_resolved(adopted_le, oid, "adopted")
        generation_quarantine = _generation_quarantine()
        if generation_quarantine is not None:
            return generation_quarantine
        _commit_le(sess, adopted_le)
        le = adopted_le
        _emit(
            db, sess, "entry_adopted_on_cancel",
            {
                "order_id": str(oid),
                "filled_size": filled,
                "venue_status": venue_status,
                "by": cancelled_by,
            },
        )
        # Walk the LEGAL live FSM chain (no watching->pending shortcut exists).
        # Guarded by state so an already-pending/entered session is left as-is
        # (the runner's normal handler owns it from there).
        if sess.state == STATE_WATCHING_LIVE:
            generation_quarantine = _generation_quarantine()
            if generation_quarantine is not None:
                return generation_quarantine
            _safe_transition(db, sess, STATE_LIVE_ENTRY_CANDIDATE)
        if sess.state == STATE_LIVE_ENTRY_CANDIDATE:
            generation_quarantine = _generation_quarantine()
            if generation_quarantine is not None:
                return generation_quarantine
            _safe_transition(db, sess, STATE_LIVE_PENDING_ENTRY)
        db.flush()
        return {
            "ok": True,
            "adopted": True,
            "session_id": int(sess.id),
            "state": sess.state,
        }
    return None


def cancel_automation_session(
    db: Session, *, user_id: int, session_id: int, cancelled_by: str = "automation_monitor"
) -> dict[str, Any]:
    """Terminalize a cancellable automation session.

    ``cancelled_by`` records the TRUE initiator on the ``session_cancelled`` event's
    ``by`` field: the operator HTTP endpoint passes ``"operator"``; every automated
    caller (the auto-arm watch reaper, the rank-displacement reaper, the
    confirm-block release, the stale-session reaper) leaves the default
    ``"automation_monitor"`` so a monitor-driven cancel is never mislabeled as a
    human action (BTCT sess 9871, 2026-06-29: an automated post-recycle cancel logged
    ``by=operator`` though no operator touched it).
    """
    if not _tables_present(db):
        return {"ok": False, "error": "tables_missing"}

    # momentum-orphan adopt-on-cancel (2026-06-17): SELECT ... FOR UPDATE so the
    # cancel serializes against a concurrent live_runner tick on the same session.
    # Without the row lock, the runner could be re-pointing/adopting the same late
    # fill while we sweep + cancel -> two writers racing the position. The baton is
    # a single ROW lock: whoever holds it owns the management decision. FOR UPDATE
    # is a no-op on the test session double; on a real mapped row it blocks the tick.
    _q = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == int(session_id), TradingAutomationSession.user_id == user_id)
    )
    try:
        _q = _q.with_for_update()
    except Exception:
        # SQLite / unsupported dialect in some test paths — degrade to no lock.
        pass
    try:
        _q = _q.populate_existing()
    except Exception:
        pass
    sess = _q.one_or_none()
    if not sess:
        return {"ok": False, "error": "not_found"}
    if sess.state not in CANCELLABLE_STATES:
        return {"ok": False, "error": "not_cancellable", "state": sess.state}

    prev = sess.state
    _is_alpaca_live = bool(
        sess.mode == "live"
        and normalize_execution_family(sess.execution_family)
        in {"alpaca_spot", "alpaca_short"}
    )
    if _is_alpaca_live:
        execution_quarantine = _persisted_alpaca_execution_quarantine_reason(sess)
        if execution_quarantine is not None:
            return _quarantine_operator_stop_execution(
                db,
                sess,
                reason=execution_quarantine,
            )

    _pre_http_no_transport_terminal = False

    # A committed Alpaca permit can survive a crash that rolled every session
    # pointer back. Pause and defer terminalization until the claim-aware runner
    # has cancelled/reread/adopted the exact broker identity.
    _claim_readable, _durable_claim = _owned_unresolved_alpaca_entry_claim(db, sess)
    if not _claim_readable:
        return {
            "ok": False,
            "error": "alpaca_entry_claim_unreadable",
            "message": "Durable broker-entry ownership is unreadable; the session was not terminalized.",
        }
    if _durable_claim is not None:
        if (
            _durable_claim.get("client_order_id")
            or _durable_claim.get("broker_order_id")
        ):
            snap = dict(sess.risk_snapshot_json or {})
            le = dict(snap.get("momentum_live_execution") or {})
            snap["alpaca_symbol_claim_token"] = _durable_claim.get("claim_token")
            le["entry_submitted"] = True
            if _durable_claim.get("client_order_id"):
                le["entry_client_order_id"] = _durable_claim["client_order_id"]
                le["entry_reconcile_pending_client_order_id"] = _durable_claim[
                    "client_order_id"
                ]
            if _durable_claim.get("broker_order_id"):
                le["entry_order_id"] = _durable_claim["broker_order_id"]
            snap["momentum_live_execution"] = le
            sess.risk_snapshot_json = apply_operator_pause(snap, state=sess.state)
            sess.updated_at = datetime.utcnow()
            db.flush()
            return {
                "ok": True,
                "pending": "durable_alpaca_entry_claim_reconcile",
                "session_id": int(sess.id),
                "state": sess.state,
                "message": "Broker entry truth is unresolved; the session remains non-terminal.",
            }
        if not _exact_pre_http_operator_claim(
            sess,
            _durable_claim,
        ):
            _pause_operator_terminalization(sess)
            db.flush()
            return {
                "ok": True,
                "pending": "durable_alpaca_entry_claim_reconcile",
                "terminalization_deferred": True,
                "session_id": int(sess.id),
                "state": sess.state,
                "message": (
                    "The CID-less broker permit is not exact no-transport proof; "
                    "new entry work is paused pending reconciliation."
                ),
            }
        _retire_confirmed_pre_http_alpaca_claim_before_terminal(
            db,
            sess,
            new_state=STATE_LIVE_CANCELLED,
        )
        _pre_http_no_transport_terminal = True

    if _is_alpaca_live and not _pre_http_no_transport_terminal:
        cancel_truth = _flatten_live_session_for_stop(
            db,
            sess,
            request_kind="cancel",
        )
        if cancel_truth.get("terminalization_deferred"):
            _pause_operator_terminalization(sess)
            db.flush()
            return {
                "ok": True,
                "pending": cancel_truth.get("pending") or "broker_flat_confirmation",
                "session_id": int(sess.id),
                "state": sess.state,
                "cancel_reconcile": cancel_truth,
                "message": (
                    "Exact Alpaca order/position truth remains under reconciliation; "
                    "the session was not terminalized early."
                ),
            }
        if not cancel_truth.get("broker_flat_confirmed"):
            return {
                "ok": False,
                "error": "broker_flat_unconfirmed",
                "session_id": int(sess.id),
                "state": sess.state,
                "cancel_reconcile": cancel_truth,
            }

    # ── ADOPT-ON-CANCEL-FILL (the momentum-orphan root fix) ──────────────────
    # BEFORE we mark the session terminal: if a LIVE session is being cancelled
    # (operator OR the auto-arm reaper — both land here) but its entry order has
    # actually FILLED at the broker, killing the session would ORPHAN the broker
    # position (CRVO/FTHM 2026-06-16: cancel raced the fill, the sweep only logged
    # FILLED_NEEDS_ADOPTION and never adopted -> unmanaged position). Instead we
    # ADOPT: re-point the session at the real fill and walk the LEGAL live FSM to
    # pending-entry so the hardened fill-handler attaches the lane stop/target on
    # the next tick — exactly mirroring live_runner._sweep_unresolved_entry_orders.
    # The reconciler's scope-skip (Step 2) keeps the legacy backstop off this
    # symbol while THIS (now non-terminal) session manages it -> no double-sell.
    if (
        bool(getattr(settings, "chili_momentum_adopt_on_cancel_fill_enabled", True))
        and sess.mode == "live"
        and prev in LIVE_CANCELLABLE_STATES
        and _is_alpaca_live
        and not _pre_http_no_transport_terminal
    ):
        _adopt = _try_adopt_filled_entry_on_cancel(db, sess, cancelled_by=cancelled_by)
        if _adopt is not None:
            return _adopt

    if (
        sess.mode == "live"
        and prev in LIVE_CANCELLABLE_STATES
        and not _is_alpaca_live
    ):
        terminal_truth = _non_alpaca_live_terminalization_proof(
            db,
            sess,
            context="cancel_automation_session_pre_terminal",
            cancelled_by=cancelled_by,
        )
        if terminal_truth.get("state") == "managed":
            return dict(terminal_truth.get("result") or terminal_truth)
        if terminal_truth.get("state") != "safe":
            return {
                **terminal_truth,
                "message": (
                    "Broker order/position truth remains under reconciliation; "
                    "the live session was not terminalized."
                ),
            }
        if (
            sess.state != prev
            or not _non_alpaca_terminal_proof_matches_session(
                sess,
                terminal_truth.get("proof"),
            )
        ):
            return _persist_non_alpaca_terminalization_quarantine(
                db,
                sess,
                reason="terminalization_session_generation_changed",
                context="cancel_automation_session_pre_terminal_mutation",
                detail={"expected_state": prev, "current_state": sess.state},
            )

    if _is_alpaca_live:
        # Broker reads/adoption can span a configuration reload.  Never inherit
        # cancellation or terminalization authority across account generations.
        execution_quarantine = _persisted_alpaca_execution_quarantine_reason(sess)
        if execution_quarantine is not None:
            return _quarantine_operator_stop_execution(
                db,
                sess,
                reason=execution_quarantine,
            )

    now = datetime.utcnow()
    final_account_quarantine = _final_non_alpaca_terminal_account_quarantine(
        db,
        sess,
        context="cancel_automation_session_final_account_fence",
    )
    if final_account_quarantine is not None:
        return final_account_quarantine
    terminal_state = (
        STATE_LIVE_CANCELLED if sess.mode == "live" else STATE_CANCELLED
    )
    if _is_alpaca_live:
        _retire_confirmed_pre_http_alpaca_claim_before_terminal(
            db,
            sess,
            new_state=terminal_state,
        )
    sess.state = terminal_state
    sess.ended_at = now
    sess.updated_at = now
    sess.risk_snapshot_json = clear_operator_pause(sess.risk_snapshot_json)

    from .persistence import append_trading_automation_event

    # ORDER-TRUTH (2026-06-11): a LIVE session must never die with its broker
    # orders still resting. KMRK: a dead session's GTC buy filled hours later
    # into a -21.9% dump; CPSH/SNDG: fills raced the ack-timeout cancel and fell
    # to generic wide brackets because a dead session's late-fill sweep stops
    # ticking. This is the death chokepoint (operator cancel AND the auto-arm
    # reaper land here): best-effort cancel every unresolved entry order; if one
    # already FILLED, surface it loudly so the adoption is visible.
    _order_cleanup = None
    if sess.mode == "live" and _is_alpaca_live:
        # Alpaca can reach this terminal chokepoint only after the exact-flatten
        # path above proved broker-flat with the frozen account generation.  A
        # second unbound best-effort sweep would add cross-generation authority
        # without adding safety, so make the zero-broker-call skip explicit.
        _order_cleanup = {"skipped": "alpaca_exact_flatten_completed"}
    elif sess.mode == "live":
        # Non-Alpaca live rows reached here only after the strict pre-terminal
        # proof canceled/reread every saved identity and proved symbol-open-order
        # absence.  Never perform a second best-effort sweep after death.
        _order_cleanup = {"skipped": "strict_preterminal_order_proof_completed"}

    append_trading_automation_event(
        db,
        sess.id,
        "session_cancelled",
        {
            "previous_state": prev, "by": cancelled_by, "terminal_state": sess.state,
            **({"order_cleanup": _order_cleanup} if _order_cleanup else {}),
        },
        correlation_id=sess.correlation_id,
        source_node_id="momentum_automation_monitor",
    )
    if sess.mode == "paper" and prev != STATE_CANCELLED:
        append_trading_automation_event(
            db,
            sess.id,
            "paper_cancelled",
            {"previous_state": prev},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_automation_monitor",
        )
    if sess.mode == "live":
        append_trading_automation_event(
            db,
            sess.id,
            "live_cancelled",
            {"previous_state": prev},
            correlation_id=sess.correlation_id,
            source_node_id="momentum_automation_monitor",
        )
    try:
        from .feedback_emit import emit_feedback_after_terminal_transition

        emit_feedback_after_terminal_transition(db, sess)
    except Exception:
        pass
    return {"ok": True, "session_id": sess.id, "state": sess.state}


def archive_automation_session(db: Session, *, user_id: int, session_id: int) -> dict[str, Any]:
    if not _tables_present(db):
        return {"ok": False, "error": "tables_missing"}

    sess = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == int(session_id), TradingAutomationSession.user_id == user_id)
        .one_or_none()
    )
    if not sess:
        return {"ok": False, "error": "not_found"}
    if sess.state == STATE_ARCHIVED:
        return {"ok": False, "error": "already_archived"}
    if sess.state not in ARCHIVABLE_STATES:
        return {"ok": False, "error": "not_archivable", "state": sess.state}

    prev = sess.state
    sess.state = STATE_ARCHIVED
    sess.updated_at = datetime.utcnow()
    sess.risk_snapshot_json = clear_operator_pause(sess.risk_snapshot_json)

    from .persistence import append_trading_automation_event

    append_trading_automation_event(
        db,
        sess.id,
        "session_archived",
        {"previous_state": prev},
        correlation_id=sess.correlation_id,
        source_node_id="momentum_automation_monitor",
    )
    try:
        from .feedback_emit import emit_feedback_after_terminal_transition

        emit_feedback_after_terminal_transition(db, sess)
    except Exception:
        pass
    return {"ok": True, "session_id": sess.id, "state": sess.state}


# ── P&L rollup (2026-06-12 money-first redesign) ────────────────────────────
# The operator could not answer "magkano na ang total PnL?" from the page:
# totals were summed CLIENT-side over a capped-100, archived-excluded session
# list at the bottom of the page. This computes the truth server-side in one
# DB snapshot — uncapped, archived included — per symbol × bucket.
#
# Buckets: live (real money), paper (simulator), alpaca (live-mode twin soak
# against the Alpaca PAPER endpoint — fake money, never blended into live).
#
# Realized-today sources differ by lane because only the paper runner writes
# fill rows: paper = simulated-fill pnl_usd inside the ET day (exact);
# live/alpaca = terminal outcomes today + cumulative runtime realized of
# still-active sessions (sessions are intraday in practice).


def _rollup_bucket_for(sess_mode: str, execution_family: str | None) -> str:
    # "alpaca" = LIVE-mode twin-soak sessions only. Paper-mode sessions routed
    # to alpaca_spot (the paper-equity DMA lane, #649) are still the paper
    # SIMULATOR — they belong to the paper bucket, and their money is already
    # fully counted from simulated fills (bucketing them here would also
    # double-count their cumulative runtime realized).
    if sess_mode == "live" and (execution_family or "") == "alpaca_spot":
        return "alpaca"
    return "live" if sess_mode == "live" else "paper"


def _et_day_bounds_utc() -> tuple[datetime, datetime, str]:
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    end_et = start_et + timedelta(days=1)
    utc = ZoneInfo("UTC")
    return (
        start_et.astimezone(utc).replace(tzinfo=None),
        end_et.astimezone(utc).replace(tzinfo=None),
        now_et.strftime("%H:%M:%S"),
    )


def _rollup_exec_state(sess: TradingAutomationSession) -> dict[str, Any]:
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    key = "momentum_live_execution" if sess.mode == "live" else "momentum_paper_execution"
    ex = snap.get(key)
    return ex if isinstance(ex, dict) else {}


def automation_pnl_rollup(db: Session, *, user_id: int) -> dict[str, Any]:
    start_utc, end_utc, as_of_et = _et_day_bounds_utc()
    from ....models.trading import MomentumAutomationOutcome

    week_floor_utc = datetime.utcnow() - timedelta(days=7)

    def _sym_cell() -> dict[str, Any]:
        return {
            "state": "FLAT", "qty": None, "avg_price": None, "mark": None,
            "mark_age_s": None, "floating_usd": 0.0, "realized_usd": 0.0,
            "realized_7d_usd": 0.0,
            "trades": 0, "wins": 0, "losses": 0, "last_activity_utc": None,
            "session_id": None, "open_session_ids": [], "asset_class": None,
            "broker_unconfirmed": False,
        }

    def _bucket_cell() -> dict[str, Any]:
        return {
            "floating_usd": 0.0, "realized_usd": 0.0, "realized_7d_usd": 0.0,
            "total_usd": 0.0,
            "open_count": 0, "armed_count": 0, "at_risk_usd": 0.0,
            "at_risk_unknown_stops": 0, "trades": 0, "wins": 0, "losses": 0,
            "symbols": {},
        }

    buckets: dict[str, dict[str, Any]] = {
        "live": _bucket_cell(), "paper": _bucket_cell(), "alpaca": _bucket_cell(),
    }

    def _sym(bucket: str, symbol: str) -> dict[str, Any]:
        return buckets[bucket]["symbols"].setdefault(symbol, _sym_cell())

    def _touch_activity(cell: dict[str, Any], ts) -> None:
        if ts is None:
            return
        iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        if cell["last_activity_utc"] is None or iso > cell["last_activity_utc"]:
            cell["last_activity_utc"] = iso

    # 1) PAPER realized today — exact, from fill rows inside the ET day.
    fill_rows = (
        db.query(
            TradingAutomationSimulatedFill.symbol,
            TradingAutomationSimulatedFill.pnl_usd,
            TradingAutomationSimulatedFill.ts,
            TradingAutomationSession.mode,
            TradingAutomationSession.execution_family,
        )
        .join(TradingAutomationSession, TradingAutomationSession.id == TradingAutomationSimulatedFill.session_id)
        .filter(
            TradingAutomationSession.user_id == user_id,
            TradingAutomationSimulatedFill.ts >= week_floor_utc,
            TradingAutomationSimulatedFill.ts < end_utc,
            TradingAutomationSimulatedFill.pnl_usd.isnot(None),
        )
        .all()
    )
    for symbol, pnl, fill_ts, mode, fam in fill_rows:
        bucket = _rollup_bucket_for(mode, fam)
        cell = _sym(bucket, symbol)
        p = float(pnl or 0.0)
        cell["realized_7d_usd"] += p
        if fill_ts is not None and fill_ts >= start_utc:
            cell["realized_usd"] += p
            cell["trades"] += 1
            if p > 0:
                cell["wins"] += 1
            elif p < 0:
                cell["losses"] += 1
        _touch_activity(cell, fill_ts)

    # 2) LIVE/ALPACA realized today — terminal outcomes inside the ET day.
    outcome_rows = (
        db.query(
            MomentumAutomationOutcome.realized_pnl_usd,
            MomentumAutomationOutcome.terminal_at,
            TradingAutomationSession.symbol,
            TradingAutomationSession.mode,
            TradingAutomationSession.execution_family,
        )
        .join(TradingAutomationSession, TradingAutomationSession.id == MomentumAutomationOutcome.session_id)
        .filter(
            MomentumAutomationOutcome.user_id == user_id,
            MomentumAutomationOutcome.terminal_at >= week_floor_utc,
            MomentumAutomationOutcome.terminal_at < end_utc,
            # NULL pnl = never entered (cancelled_pre_entry, risk_block, ...)
            # — hundreds exist; they are not trades and must not create
            # phantom $0 ledger rows.
            MomentumAutomationOutcome.realized_pnl_usd.isnot(None),
            TradingAutomationSession.mode == "live",
        )
        .all()
    )
    for pnl, terminal_at, symbol, mode, fam in outcome_rows:
        bucket = _rollup_bucket_for(mode, fam)
        cell = _sym(bucket, symbol)
        p = float(pnl or 0.0)
        cell["realized_7d_usd"] += p
        if terminal_at is not None and terminal_at >= start_utc:
            cell["realized_usd"] += p
            cell["trades"] += 1
            if p > 0:
                cell["wins"] += 1
            elif p < 0:
                cell["losses"] += 1
        _touch_activity(cell, terminal_at)

    # 3) ACTIVE sessions — floating from open positions, plus (live/alpaca
    #    only) cumulative runtime realized not yet visible as an outcome row.
    inactive_states = TERMINAL_STATES | {STATE_ARCHIVED}
    active_rows = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.user_id == user_id,
            TradingAutomationSession.state.notin_(inactive_states),
        )
        .all()
    )
    now_utc = datetime.utcnow()
    # Cockpit broker-truth defense (2026-06-13): the live runner can keep
    # le['position'] populated for minutes-to-hours after a position has actually
    # left the broker (sold externally / missed exit fill / dust) — that stale
    # position read as REAL money (the TAO +$16.70 phantom that made -$3.65 look
    # like +$12). The broker-sync (every 2min) closes the Trade row once the broker
    # confirms the holding is gone (coinbase_position_sync_gone + missing-streak /
    # RH sync), so an OPEN broker-synced Trade is a DB-only broker-truth signal —
    # NO broker call in this UI path. Applied ONLY to the real-broker "live" bucket
    # (coinbase + robinhood); alpaca paper twins + paper have no real broker holding
    # and are never suppressed. Fail-open: any error leaves floating untouched.
    # Two broker-truth signals from the broker-synced Trade rows: symbols the
    # broker currently HOLDS (an open trade) and symbols the broker recently
    # EXITED (a closed/cancelled trade in the last 2 days). A live position is a
    # PHANTOM only on POSITIVE exit evidence — a recently-closed trade AND no open
    # one — never merely on "no open trade" (a brand-new real fill may not have its
    # synced Trade row yet; absence of evidence must NOT suppress real money).
    _open_tk: "set[str] | None" = None
    _exited_tk: "set[str]" = set()
    try:
        from ....models.trading import Trade

        _open_tk = {
            str(_tk).upper()
            for (_tk,) in db.query(Trade.ticker)
            .filter(Trade.user_id == user_id, Trade.status == "open")
            .distinct()
            .all()
            if _tk
        }
        _closed_floor = now_utc - timedelta(days=2)
        _exited_tk = {
            str(_tk).upper()
            for (_tk,) in db.query(Trade.ticker)
            .filter(
                Trade.user_id == user_id,
                Trade.status.in_(("closed", "cancelled")),
                Trade.exit_date.isnot(None),
                Trade.exit_date >= _closed_floor,
            )
            .distinct()
            .all()
            if _tk
        }
    except Exception:
        _open_tk = None  # fail-open: never suppress on a query error
    for sess in active_rows:
        bucket = _rollup_bucket_for(sess.mode, sess.execution_family)
        ex = _rollup_exec_state(sess)
        cell = _sym(bucket, sess.symbol)
        if cell["session_id"] is None:
            cell["session_id"] = int(sess.id)
        if cell["asset_class"] is None:
            try:
                cell["asset_class"] = asset_class_for_symbol(sess.symbol)
            except Exception:
                cell["asset_class"] = None
        _touch_activity(cell, sess.updated_at)
        if sess.mode == "live":
            # Runner identity decides the money source: only the LIVE runner
            # lacks fill rows. Day-fence by session start — a 24/7 crypto
            # session that banked before ET midnight must not put yesterday's
            # money into the "TODAY (since 00:00 ET)" hero.
            _rt = float(_float_or_none_q(ex.get("realized_pnl_usd")) or 0.0)
            if _rt:
                _sa = sess.started_at or sess.created_at
                if _sa is None or _sa >= start_utc:
                    cell["realized_usd"] += _rt
                if _sa is None or _sa >= week_floor_utc:
                    cell["realized_7d_usd"] += _rt
        pos = ex.get("position") if isinstance(ex.get("position"), dict) else None
        if not pos:
            if cell["state"] != "OPEN":
                cell["state"] = "ARMED"
            buckets[bucket]["armed_count"] += 1
            continue
        qty = _float_or_none_q(pos.get("quantity"))
        entry = _float_or_none_q(pos.get("avg_entry_price")) or _float_or_none_q(pos.get("entry_price"))
        last = _float_or_none_q(ex.get("last_mid"))
        # Multiple open sessions on one symbol: aggregate qty/weighted entry,
        # track every session id so per-row Flatten covers them all.
        if cell["state"] == "OPEN" and qty and cell["qty"]:
            prev_qty, prev_entry = cell["qty"], cell["avg_price"]
            cell["qty"] = prev_qty + qty
            if entry and prev_entry and cell["qty"] > 0:
                cell["avg_price"] = (prev_entry * prev_qty + entry * qty) / cell["qty"]
            cell["mark"] = last or cell["mark"]
        else:
            cell["qty"], cell["avg_price"], cell["mark"] = qty, entry, last
        cell["state"] = "OPEN"
        cell["open_session_ids"].append(int(sess.id))
        cell["session_id"] = int(sess.id)
        buckets[bucket]["open_count"] += 1
        tick = ex.get("last_tick_utc")
        if tick:
            try:
                tick_dt = datetime.fromisoformat(str(tick).replace("Z", "+00:00")).replace(tzinfo=None)
                cell["mark_age_s"] = max(0, int((now_utc - tick_dt).total_seconds()))
            except (TypeError, ValueError):
                pass
        # Phantom guard: a real-broker ("live" = coinbase/robinhood) holding the
        # broker does NOT confirm (no OPEN broker-synced Trade for the symbol) must
        # NOT add floating or at-risk — that is the stale-position phantom. Alpaca
        # paper twins ("alpaca" bucket) + paper are never broker-checked here.
        _sym_up = str(sess.symbol or "").upper()
        _broker_unconfirmed = (
            bucket == "live"
            and _open_tk is not None
            and _sym_up in _exited_tk
            and _sym_up not in _open_tk
        )
        if _broker_unconfirmed:
            cell["broker_unconfirmed"] = True
        if qty and entry and last and entry > 0 and not _broker_unconfirmed:
            cell["floating_usd"] += round((last - entry) * qty, 2)
        stop = _float_or_none_q(pos.get("stop_price"))
        if _broker_unconfirmed:
            pass  # phantom: no real position -> contributes no at-risk
        elif qty and entry and stop and stop > 0:
            # A stop AT or ABOVE entry (breakeven ratchet / trail in profit)
            # is a KNOWN stop with $0 risk — the safest positions must not
            # read as the scariest. "Unknown" = genuinely missing stop.
            buckets[bucket]["at_risk_usd"] += max(0.0, (entry - stop)) * qty
        else:
            buckets[bucket]["at_risk_unknown_stops"] += 1

    # Bucket totals from symbol cells.
    for b in buckets.values():
        for cell in b["symbols"].values():
            cell["realized_usd"] = round(cell["realized_usd"], 2)
            cell["realized_7d_usd"] = round(cell["realized_7d_usd"], 2)
            cell["floating_usd"] = round(cell["floating_usd"], 2)
            cell["total_usd"] = round(cell["realized_usd"] + cell["floating_usd"], 2)
            b["floating_usd"] += cell["floating_usd"]
            b["realized_usd"] += cell["realized_usd"]
            b["realized_7d_usd"] += cell["realized_7d_usd"]
            b["trades"] += cell["trades"]
            b["wins"] += cell["wins"]
            b["losses"] += cell["losses"]
        b["floating_usd"] = round(b["floating_usd"], 2)
        b["realized_usd"] = round(b["realized_usd"], 2)
        b["realized_7d_usd"] = round(b["realized_7d_usd"], 2)
        b["total_usd"] = round(b["floating_usd"] + b["realized_usd"], 2)
        b["at_risk_usd"] = round(b["at_risk_usd"], 2)
        # Ledger rows = TODAY's story (open/armed positions + today's trades).
        # Symbols whose only activity is older in the 7d window stay in the
        # bucket 7d totals but are dropped from the row list — 276 flat history
        # rows is noise, not a ledger. The count is reported, never silent.
        visible = {
            sym: cell
            for sym, cell in b["symbols"].items()
            if cell["state"] != "FLAT" or cell["trades"] > 0
            or cell["realized_usd"] != 0.0 or cell["floating_usd"] != 0.0
        }
        b["older_7d_symbols"] = len(b["symbols"]) - len(visible)
        # OPEN first by |floating|, then by |realized| — fixed sort, server-side.
        b["symbols"] = [
            dict(cell, symbol=sym)
            for sym, cell in sorted(
                visible.items(),
                key=lambda kv: (
                    0 if kv[1]["state"] == "OPEN" else 1,
                    -abs(kv[1]["floating_usd"] if kv[1]["state"] == "OPEN" else kv[1]["realized_usd"]),
                ),
            )
        ]

    # Lane-health: surface a FROZEN safety-breaker state in the cockpit's primary
    # surface (the sticky P&L band) so a silently-frozen lane is impossible to miss —
    # the 06-15 incident sat unnoticed ~8h. Read-only; fail-open (never break the P&L).
    lane_health: dict[str, Any] | None = None
    try:
        from .lane_health import evaluate_lane_health

        lane_health = evaluate_lane_health(db, user_id=user_id)
    except Exception:
        lane_health = None

    return {
        "as_of_utc": datetime.utcnow().isoformat() + "Z",
        "as_of_et": as_of_et,
        "et_day_start_utc": start_utc.isoformat() + "Z",
        "buckets": buckets,
        "lane_health": lane_health,
    }
