"""Alpaca PAPER position/order reconciler — flatten ORPHANS no session manages.

WHY (2026-07-09 audit): the sub-penny reject storm (fixed by ``_equity_limit_price``)
made EXIT submissions bounce while their sessions terminalized via live_error /
live_cancelled — stranding SIX positions (~$65k MV; RKTO 20,815 sh, −$1,249 unrealized)
plus a stale resting buy on the paper account with NO managing session. The broker-sync
loop covers RH/Coinbase only, so the orphans persisted silently and consumed buying
power ($399k → $66k), starving new entries. Same failure class as the RH dup-Reference
orphan (#854 reconcile-not-terminalize) — this is the Alpaca-side guard.

WHAT (each scheduler pass, ~120s): compare the ACTUAL Alpaca paper account against the
DB's alpaca-family sessions:
  * ORPHAN POSITION = a LONG EQUITY position with no active/recent owner *and* an
    exact broker-verified filled CHILI entry. Unknown/manual exposure is quarantined;
    it is never sold merely because no session currently owns the ticker.
  * ORPHAN OPEN ORDER = an old resting order with an exact unresolved CHILI paper
    claim matching both CID and OID. Unknown/manual orders are reported, never canceled.

SAFETY:
  * PAPER-ONLY BY CONSTRUCTION — hard-gated on ``chili_alpaca_paper`` (this never runs
    against a real-money account; extending to live requires a deliberate code change).
  * FLATTEN-ONLY — sells an existing long / cancels a resting order; never opens,
    adds, or shorts. Crypto + short positions are OUT OF SCOPE (skipped).
  * FAIL-CLOSED / NO MUTATION — any unreadable account, order, claim, position, or
    session truth blocks broker changes for the affected pass or identity.
  * Grace window (one documented knob) absorbs races with just-created sessions and
    just-terminalized exits still settling.
  * Per-pass action cap; idempotent per-minute client_order_id.
  * Default-ON with a kill-switch (no dark flags). CHILI automation places the
    orders — the same authority as every other lane order.
(ALPACA_PAPER_ENABLE_PLAN.md)
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import (
    AdaptiveRiskDecisionPacket,
    AdaptiveRiskReservationEvent,
    MomentumAutomationOutcome,
    TradingAutomationEvent,
    TradingAutomationSession,
)
from .outcome_labels import OUTCOME_CANCELLED_PRE_ENTRY, OUTCOME_GOVERNANCE_EXIT
from .alpaca_orphan_claims import (
    CLAIMED,
    RESOLVED,
    SUBMIT_INDETERMINATE,
    SUBMITTED,
    consume_orphan_handoff_close_post_permission_committed,
    list_unresolved_action_claims,
    persist_orphan_close_request_committed,
    read_action_claim,
    resolve_action_claim,
    resolve_action_claim_committed,
    update_action_claim_phase,
    update_action_claim_phase_committed,
)
from .adaptive_risk_policy import AdaptiveRiskContractError
from .adaptive_risk_reservation import (
    AdaptiveReservationError,
    AdaptiveRiskReservationStore,
    DurableOrderLifecycleEvidence,
    load_adaptive_risk_reservation_request,
)
from .adaptive_risk_runtime_contract import (
    load_and_verify_adaptive_risk_reservation_claim,
)
from .alpaca_cycle_settlement import settle_flat_alpaca_paper_cycle
from .alpaca_fill_activity import capture_verified_alpaca_paper_order_fills
from .alpaca_paper_identity import alpaca_paper_account_identity_sha256
from .operator_actions import (
    _TERMINAL_OPERATOR_STATES,
    _alpaca_execution_quarantine_reason,
)
from .market_profile import market_session_now

logger = logging.getLogger(__name__)

_ALPACA_FAMILIES = ("alpaca_spot", "alpaca_short")
_ORPHAN_EXIT_REASON = "alpaca_orphan_reconcile"
_ORPHAN_SETTLE_LOOKBACK_DAYS = 7
_ENTRY_TERMINAL_STATUSES = frozenset({
    "filled", "done", "closed", "canceled", "cancelled", "expired",
    "rejected", "failed",
})
# Retry authority is never exhausted while broker exposure remains.  Only the
# retained audit history is bounded; the per-sweep broker-action cap below limits
# request pressure without silently abandoning a residual after N failures.
_HANDOFF_CLOSE_HISTORY_MAX = 8
_ADAPTIVE_REQUEST_KEY = "adaptive_risk_reservation_request"
_ADAPTIVE_PACKET_KEY = "adaptive_risk_decision_packet"
_ADAPTIVE_CLAIM_KEY = "adaptive_risk_reservation_claim"
_ADAPTIVE_LIFECYCLE_KEY = "adaptive_risk_lifecycle_binding"
_ADAPTIVE_LIFECYCLE_SCHEMA = "chili.adaptive-risk-alpaca-lifecycle.v1"
_ADAPTIVE_MARKER_KEYS = frozenset(
    {
        _ADAPTIVE_REQUEST_KEY,
        _ADAPTIVE_PACKET_KEY,
        _ADAPTIVE_CLAIM_KEY,
        _ADAPTIVE_LIFECYCLE_KEY,
    }
)

# Per-symbol attempt memory (process-local): a stuck symbol (halt, reject loop) is
# re-attempted at most once per grace window, not every 120s pass — bounds repeat-fire
# over TIME (adversarial review lens 2) on top of the per-pass action cap.
_LAST_ATTEMPT: dict[str, datetime] = {}


def _alpaca_reconcile_shape_quarantine_reason(
    *,
    symbol: str | None,
    execution_family: str | None = "alpaca_spot",
    metadata: dict[str, Any] | None = None,
) -> str | None:
    """Paper/equity/long-only boundary for every persisted reconcile shape."""
    meta = metadata if isinstance(metadata, dict) else {}
    request = meta.get("order_request")
    request = request if isinstance(request, dict) else {}
    handoff = meta.get("entry_handoff_proof")
    handoff = handoff if isinstance(handoff, dict) else {}
    role_meta = meta.get("role_metadata")
    role_meta = role_meta if isinstance(role_meta, dict) else {}
    explicit_asset_class = (
        request.get("asset_class")
        or handoff.get("asset_class")
        or role_meta.get("asset_class")
        or meta.get("asset_class")
    )
    reason = _alpaca_execution_quarantine_reason(
        execution_family,
        symbol,
        asset_class=explicit_asset_class,
    )
    if reason is not None:
        return reason
    if (
        str(request.get("side") or "").strip().lower() == "sell"
        or str(meta.get("close_side") or "").strip().lower() == "buy"
        or str(meta.get("position_intent") or "").strip().lower() == "buy_to_close"
        or str(handoff.get("entry_side") or "").strip().lower() == "sell"
    ):
        return "alpaca_short_execution_not_certified"
    return None


def _persisted_reconcile_quarantine_reason(
    db: Session,
) -> dict[str, int] | str | None:
    """Report unsupported stored identities without starving certified work.

    Crypto/short/unfrozen rows are quarantined again at their exact session/claim
    boundary.  They must not suppress recovery for an unrelated certified paper
    equity claim in the same batch.  Only an unreadable persistence view is a
    pass-wide fail-closed condition.
    """
    reasons: dict[str, int] = {}
    expected_account_id = str(
        getattr(settings, "chili_alpaca_expected_account_id", "") or ""
    ).strip()
    if not expected_account_id:
        return "alpaca_expected_account_id_unconfigured"

    def _record(reason: str | None) -> None:
        if reason:
            reasons[str(reason)] = int(reasons.get(str(reason)) or 0) + 1

    try:
        sessions = db.execute(text(
            "SELECT execution_family, upper(symbol), "
            "       lower(coalesce(risk_snapshot_json->>'alpaca_account_scope', '')), "
            "       coalesce(risk_snapshot_json->>'alpaca_account_id', '') "
            "FROM trading_automation_sessions "
            "WHERE mode = 'live' AND execution_family = ANY(:families) "
            "  AND (NOT (state = ANY(:terminal_states)) "
            "       OR risk_snapshot_json->'momentum_live_execution'->'position' "
            "          IS NOT NULL "
            "       OR coalesce(risk_snapshot_json->'momentum_live_execution'"
            "          ->>'entry_order_id', '') <> '' "
            "       OR coalesce(risk_snapshot_json->'momentum_live_execution'"
            "          ->>'exit_order_id', '') <> '' "
            "       OR coalesce(risk_snapshot_json->'momentum_live_execution'"
            "          ->>'pending_exit_reason', '') <> '') "
            "ORDER BY id ASC"
        ), {
            "families": list(_ALPACA_FAMILIES),
            "terminal_states": list(_TERMINAL_OPERATOR_STATES),
        }).fetchall()
        for family, symbol, account_scope, account_id in sessions:
            reason = _alpaca_reconcile_shape_quarantine_reason(
                symbol=str(symbol or ""),
                execution_family=str(family or ""),
            )
            if reason is None and str(account_scope or "").strip().lower() != "alpaca:paper":
                reason = "alpaca_account_scope_unfrozen_or_mismatched"
            if reason is None and str(account_id or "").strip() != expected_account_id:
                reason = "alpaca_account_generation_mismatch"
            _record(reason)

        claims = db.execute(text(
            "SELECT account_scope, symbol, metadata_json FROM broker_symbol_action_claims "
            "WHERE phase <> 'resolved' AND account_scope LIKE 'alpaca:%' "
            "ORDER BY updated_at ASC"
        )).fetchall()
        for account_scope, symbol, metadata in claims:
            claim_meta = metadata if isinstance(metadata, dict) else {}
            reason = _alpaca_reconcile_shape_quarantine_reason(
                symbol=str(symbol or ""),
                metadata=claim_meta,
            )
            if reason is None and str(account_scope or "").strip().lower() != "alpaca:paper":
                reason = "alpaca_account_scope_unfrozen_or_mismatched"
            claim_request = claim_meta.get("order_request")
            claim_request = claim_request if isinstance(claim_request, dict) else {}
            claim_account_id = str(
                claim_meta.get("alpaca_account_id")
                or claim_request.get("alpaca_account_id")
                or ""
            ).strip()
            if reason is None and claim_account_id != expected_account_id:
                reason = "alpaca_account_generation_mismatch"
            _record(reason)
        return reasons or None
    except Exception:
        logger.warning(
            "[alpaca_reconcile] persisted execution identity unreadable; broker dark",
            exc_info=True,
        )
        return "alpaca_persisted_execution_identity_unreadable"


def _settlement_source_quarantine_reason(
    db: Session,
    *,
    session_id: int,
    payload: dict[str, Any],
) -> str | None:
    """Validate a stored settlement source before its exact broker-order read."""
    try:
        row = db.execute(text(
            "SELECT execution_family, upper(symbol), "
            "       lower(coalesce(risk_snapshot_json->>'alpaca_account_scope', '')) "
            "FROM trading_automation_sessions WHERE id = :sid"
        ), {"sid": int(session_id)}).fetchone()
    except Exception:
        return "alpaca_settlement_session_identity_unreadable"
    if row is None:
        return "alpaca_settlement_session_missing"
    family, symbol, account_scope = row
    payload_symbol = str(payload.get("symbol") or symbol or "").strip().upper()
    if str(symbol or "").strip().upper() != payload_symbol:
        return "alpaca_settlement_symbol_identity_mismatch"
    if str(account_scope or "").strip().lower() != "alpaca:paper":
        return "alpaca_account_scope_unfrozen_or_mismatched"
    return _alpaca_reconcile_shape_quarantine_reason(
        symbol=payload_symbol,
        execution_family=str(family or ""),
        metadata=payload,
    )


def _grace_minutes() -> float:
    try:
        return max(1.0, float(getattr(settings, "chili_momentum_alpaca_orphan_grace_minutes", 15.0) or 15.0))
    except (TypeError, ValueError):
        return 15.0


def _managed_and_recent_symbols(db: Session) -> tuple[set[str], set[str]] | None:
    """(active_symbols, recent_symbols) for the alpaca families — or None on a read
    error (callers fail closed: no reconcile action without a trustworthy DB view).

    active = any NON-terminal LIVE-mode session: the symbol is owned; hands off.
    (2026-07-10 GMM incident: two month-old PAPER `watching` rows counted as
    ownership and the reconciler stood hands-off while an orphaned $54k live
    position bled −$18k. A paper watcher can never exit a live position — it
    must never own one.)
    recent = any session CREATED inside the grace window OR any outcome TERMINALIZED
    inside it: a race-guard for fills/exits still settling."""
    try:
        grace = _grace_minutes()
        rows = db.execute(text(
            "SELECT upper(symbol) AS s, state, mode, "
            "       (created_at > (now() at time zone 'utc') - (:g * interval '1 minute')) AS is_recent "
            "FROM trading_automation_sessions "
            "WHERE execution_family = ANY(:fams)"
        ), {"fams": list(_ALPACA_FAMILIES), "g": grace}).fetchall()
        active: set[str] = set()
        recent: set[str] = set()
        for s, state, mode, is_recent in rows:
            if str(state or "") not in _TERMINAL_OPERATOR_STATES and str(mode or "") == "live":
                active.add(s)
            if bool(is_recent):
                recent.add(s)
        orows = db.execute(text(
            "SELECT upper(symbol) FROM momentum_automation_outcomes "
            "WHERE execution_family = ANY(:fams) "
            "  AND terminal_at > (now() at time zone 'utc') - (:g * interval '1 minute')"
        ), {"fams": list(_ALPACA_FAMILIES), "g": grace}).fetchall()
        recent.update(r[0] for r in orows)
        return active, recent
    except Exception:
        logger.warning("[alpaca_reconcile] session/outcome read failed — fail-closed (no action)", exc_info=True)
        return None


def _latest_session_id(db: Session, symbol: str) -> int | None:
    """Most recent alpaca-family session for the symbol (audit-event anchor)."""
    try:
        row = db.execute(text(
            "SELECT id FROM trading_automation_sessions "
            "WHERE upper(symbol) = :s AND execution_family = ANY(:fams) "
            "ORDER BY created_at DESC LIMIT 1"
        ), {"s": symbol, "fams": list(_ALPACA_FAMILIES)}).fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


def _audit(
    db: Session,
    symbol: str,
    payload: dict[str, Any],
    *,
    session_id: int | None = None,
) -> int | None:
    """Idempotent audit row on the symbol's latest session (skipped when none)."""
    sid = int(session_id) if session_id is not None else _latest_session_id(db, symbol)
    if sid is None:
        return None
    try:
        import json

        claim_token = str(payload.get("claim_token") or "").strip()
        if claim_token:
            existing = db.execute(text(
                "SELECT id FROM trading_automation_events "
                "WHERE session_id = :sid AND event_type = 'alpaca_orphan_reconcile' "
                "  AND payload_json->>'claim_token' = :claim_token "
                "  AND COALESCE(payload_json->>'action', '') = :action "
                "ORDER BY id DESC LIMIT 1"
            ), {
                "sid": sid,
                "claim_token": claim_token,
                "action": str(payload.get("action") or ""),
            }).fetchone()
            if existing is not None:
                return int(existing[0])
        row = db.execute(text(
            "INSERT INTO trading_automation_events (session_id, ts, event_type, payload_json) "
            "VALUES (:sid, (now() at time zone 'utc'), 'alpaca_orphan_reconcile', CAST(:p AS jsonb)) "
            "RETURNING id"
        ), {"sid": sid, "p": json.dumps(payload)}).fetchone()
        return int(row[0]) if row is not None else None
    except Exception:
        logger.debug("[alpaca_reconcile] audit insert failed", exc_info=True)
        return None


def _open_order_has_exact_chili_claim(
    db: Session,
    order: Any,
) -> tuple[bool, str]:
    """Only a durable exact paper claim authorizes canceling a broker order."""
    raw = order if isinstance(order, dict) else {}
    symbol = str(getattr(order, "product_id", None) or raw.get("product_id") or "").strip().upper()
    order_id = str(getattr(order, "order_id", None) or raw.get("order_id") or "").strip()
    client_id = str(
        getattr(order, "client_order_id", None) or raw.get("client_order_id") or ""
    ).strip()
    if not (
        symbol
        and order_id
        and client_id
        and client_id.startswith(("chili_ml_", "orphrec-", "chili-lco-"))
    ):
        return False, "open_order_provenance_missing"
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    if not readable:
        return False, "open_order_claim_unreadable"
    if not (
        claim is not None
        and claim.get("phase") != RESOLVED
        and claim.get("action") in {"entry", "orphan_flatten"}
        and claim.get("client_order_id") == client_id
        and claim.get("broker_order_id") == order_id
    ):
        return False, "open_order_not_owned_by_exact_claim"
    if claim.get("action") == "orphan_flatten":
        authority_ok, authority_reason = _strict_terminal_handoff_claim_authority(
            claim
        )
        if not authority_ok:
            return False, authority_reason
    return True, "exact_chili_claim"


def _claim_lease_expired(claim: dict[str, Any]) -> bool:
    lease = claim.get("lease_expires_at")
    if lease is None:
        return False
    if getattr(lease, "tzinfo", None) is None:
        lease = lease.replace(tzinfo=timezone.utc)
    return bool(lease <= datetime.now(timezone.utc))


def _detached_entry_owner_state(
    db: Session,
    claim: dict[str, Any],
    *,
    for_update: bool = False,
) -> str:
    """Return active/terminal/missing/unknown for the claim's exact owner."""
    owner_id = claim.get("owner_session_id")
    if owner_id is None:
        return "missing"
    try:
        suffix = " FOR UPDATE" if for_update else ""
        row = db.execute(text(
            "SELECT state, mode, execution_family, upper(symbol) "
            "FROM trading_automation_sessions WHERE id = :sid" + suffix
        ), {"sid": int(owner_id)}).fetchone()
    except Exception:
        logger.warning(
            "[alpaca_reconcile] owner-state read failed claim=%s",
            claim.get("claim_token"),
            exc_info=True,
        )
        return "unknown"
    if row is None:
        return "missing"
    state, mode, family, symbol = row
    identity_ok = bool(
        str(mode or "") == "live"
        and str(family or "") in _ALPACA_FAMILIES
        and str(symbol or "").upper() == str(claim.get("symbol") or "").upper()
    )
    if not identity_ok:
        return "unknown"
    return "terminal" if str(state or "") in _TERMINAL_OPERATOR_STATES else "active"


def _entry_claim_order_matches(order: Any, claim: dict[str, Any]) -> bool:
    """Validate the broker object against the exact frozen entry instruction."""
    metadata = claim.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    request = metadata.get("order_request")
    request = request if isinstance(request, dict) else {}
    cid = str(claim.get("client_order_id") or "").strip()
    oid = str(claim.get("broker_order_id") or "").strip()
    expected_symbol = str(request.get("product_id") or "").strip().upper()
    expected_side = str(request.get("side") or "").strip().lower()
    try:
        expected_qty = float(request.get("base_size"))
    except (TypeError, ValueError):
        return False
    raw = getattr(order, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    try:
        broker_qty = float(raw.get("qty"))
        filled = float(getattr(order, "filled_size", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    return bool(
        cid
        and str(getattr(order, "client_order_id", "") or "").strip() == cid
        and (not oid or str(getattr(order, "order_id", "") or "").strip() == oid)
        and str(getattr(order, "order_id", "") or "").strip()
        and expected_symbol == str(claim.get("symbol") or "").strip().upper()
        and str(getattr(order, "product_id", "") or "").strip().upper() == expected_symbol
        and expected_side in {"buy", "sell"}
        and str(getattr(order, "side", "") or "").strip().lower() == expected_side
        and expected_qty > 0.0
        and broker_qty > 0.0
        and abs(broker_qty - expected_qty) <= max(1e-9, expected_qty * 1e-8)
        and 0.0 <= filled <= broker_qty + 1e-9
    )


def _strict_detached_entry_claim_order(
    adapter: Any,
    claim: dict[str, Any],
) -> tuple[str, Any | None]:
    """OID-first broker lookup; only strict CID 404 is authoritative absence."""
    oid = str(claim.get("broker_order_id") or "").strip()
    cid = str(claim.get("client_order_id") or "").strip()
    if not cid:
        return "unknown", None
    if oid:
        try:
            order, _ = adapter.get_order(oid)
        except Exception:
            order = None
        if order is not None:
            return (
                ("found", order)
                if _entry_claim_order_matches(order, claim)
                else ("identity_mismatch", order)
            )
    if not hasattr(adapter, "get_order_by_client_order_id_truth"):
        return "unknown", None
    try:
        truth = adapter.get_order_by_client_order_id_truth(cid)
    except Exception:
        return "unknown", None
    if not isinstance(truth, dict) or not truth.get("readable"):
        return "unknown", None
    order = truth.get("order")
    if not truth.get("found") or order is None:
        return "absent", None
    return (
        ("found", order)
        if _entry_claim_order_matches(order, claim)
        else ("identity_mismatch", order)
    )


def _alpaca_unresolved_order_lineage(order: Any) -> str | None:
    """Return the raw Alpaca state that cannot authorize cancel/terminal truth."""

    raw = getattr(order, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    raw_status = str(raw.get("alpaca_status") or "").strip().lower()
    normalized = str(getattr(order, "status", "") or "").strip().lower()
    replaced_by = str(raw.get("replaced_by") or "").strip()
    if replaced_by or raw_status in {"pending_replace", "replaced"}:
        return "replacement_lineage_unresolved"
    if raw_status == "pending_cancel":
        return "cancel_pending"
    if raw_status in {
        "held",
        "calculated",
        "suspended",
        "done_for_day",
    }:
        return f"alpaca_{raw_status}_unresolved"
    if normalized == "pending":
        return "alpaca_pending_state_unresolved"
    return None


def _signed_broker_position(adapter: Any, symbol: str) -> float | None:
    if not hasattr(adapter, "get_position_quantity"):
        return None
    try:
        qty = adapter.get_position_quantity(symbol)
        return None if qty is None else float(qty)
    except Exception:
        return None


def _detached_entry_position_authority(
    adapter: Any,
    *,
    claim: dict[str, Any],
    order: Any,
) -> tuple[dict[str, Any] | None, str]:
    """Prove a single uncontested broker lot before entry-to-close handoff."""
    symbol = str(claim.get("symbol") or "").strip().upper()
    entry_side = str(getattr(order, "side", "") or "").strip().lower()
    filled_qty = _positive_finite(getattr(order, "filled_size", None))
    entry_avg = _positive_finite(getattr(order, "average_filled_price", None))
    signed_qty = _signed_broker_position(adapter, symbol)
    if not symbol or entry_side not in {"buy", "sell"} or filled_qty is None:
        return None, "entry_fill_authority_missing"
    if signed_qty is None or abs(signed_qty) <= 1e-9:
        return None, "broker_position_unreadable_or_flat"
    if (entry_side == "buy" and signed_qty <= 0.0) or (
        entry_side == "sell" and signed_qty >= 0.0
    ):
        return None, "broker_position_direction_mismatch"
    tolerance = max(1e-6, filled_qty * 1e-6)
    if abs(abs(float(signed_qty)) - filled_qty) > tolerance:
        return None, "broker_position_not_exact_entry_fill"

    if not hasattr(adapter, "list_open_orders"):
        return None, "open_order_truth_unavailable"
    try:
        open_orders, _ = adapter.list_open_orders(
            product_id=symbol,
            limit=100,
            strict=True,
        )
    except Exception:
        return None, "open_order_truth_unreadable"
    if open_orders is None:
        return None, "open_order_truth_unreadable"
    if list(open_orders):
        return None, "competing_open_order_present"

    if not hasattr(adapter, "list_positions"):
        return None, "position_detail_truth_unavailable"
    try:
        positions, _ = adapter.list_positions()
    except Exception:
        return None, "position_detail_unreadable"
    if positions is None:
        return None, "position_detail_unreadable"
    matches = [
        row
        for row in positions
        if isinstance(row, dict)
        and str(row.get("product_id") or "").strip().upper() == symbol
    ]
    if len(matches) != 1:
        return None, "position_detail_identity_mismatch"
    try:
        detailed_qty = float(matches[0].get("qty"))
    except (TypeError, ValueError):
        return None, "position_detail_quantity_invalid"
    if abs(detailed_qty - float(signed_qty)) > tolerance:
        return None, "position_detail_quantity_mismatch"
    position_avg = _positive_finite(matches[0].get("avg_entry_price"))
    if entry_avg is not None:
        if position_avg is None:
            return None, "position_average_unreadable"
        price_tolerance = max(0.0001, entry_avg * 0.0001)
        if abs(position_avg - entry_avg) > price_tolerance:
            return None, "position_average_not_exact_entry_fill"

    return {
        "proof_version": "durable_entry_claim_handoff_v1",
        "entry_claim_token": str(claim.get("claim_token") or ""),
        "entry_client_order_id": str(claim.get("client_order_id") or ""),
        "entry_broker_order_id": str(getattr(order, "order_id", "") or ""),
        "entry_order_status": str(getattr(order, "status", "") or ""),
        "entry_filled_size": filled_qty,
        "entry_average_filled_price": entry_avg,
        "entry_side": entry_side,
        "broker_position_qty": float(signed_qty),
        "broker_position_avg_entry_price": position_avg,
        "no_competing_open_orders": True,
        "entry_account_scope": str(claim.get("account_scope") or ""),
    }, "strict_durable_entry_claim_handoff"


def _adaptive_marker_present(metadata: Any) -> bool:
    """Any adaptive marker upgrades the exact claim to strict semantics."""

    meta = metadata if isinstance(metadata, dict) else {}
    role_meta = meta.get("role_metadata")
    role_meta = role_meta if isinstance(role_meta, dict) else {}
    return any(key in meta or key in role_meta for key in _ADAPTIVE_MARKER_KEYS)


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_aware_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    normalized = _aware_utc(parsed)
    if normalized is None:
        raise AdaptiveRiskContractError("adaptive lifecycle clock is missing")
    return normalized


def _adaptive_owner_connection_generation(
    db: Session,
    claim: dict[str, Any],
) -> str | None:
    """Recompute the live runner's frozen arm generation when its row survives."""

    metadata = claim.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    owner_id = claim.get("owner_session_id") or metadata.get("session_id")
    try:
        owner_id = int(owner_id)
    except (TypeError, ValueError):
        return None
    owner = db.get(TradingAutomationSession, owner_id)
    if owner is None:
        return None
    snapshot = owner.risk_snapshot_json
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    marker = snapshot.get("confirmed_arm_generation")
    account_scope = str(snapshot.get("alpaca_account_scope") or "").strip().lower()
    account_id = str(snapshot.get("alpaca_account_id") or "").strip()
    if not (
        isinstance(marker, dict)
        and account_scope == "alpaca:paper"
        and account_id
        and str(owner.symbol or "").strip().upper()
        == str(claim.get("symbol") or "").strip().upper()
    ):
        return None
    body = {
        "account_scope": account_scope,
        "account_id": account_id,
        "session_id": owner_id,
        "marker": marker,
    }
    encoded = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "alpaca-arm:" + hashlib.sha256(encoded).hexdigest()


def _adaptive_claim_context(
    db: Session,
    claim: dict[str, Any],
) -> dict[str, Any] | None:
    """Verify one complete adaptive claim without inferring missing state.

    ``None`` means a pure legacy exact claim.  Once any adaptive marker exists,
    every immutable request/packet/claim field and the durable lifecycle binding
    is mandatory.  A partial marker therefore raises before any broker mutation.
    """

    metadata = claim.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    if not _adaptive_marker_present(metadata):
        return None
    request_payload = metadata.get(_ADAPTIVE_REQUEST_KEY)
    packet_payload = metadata.get(_ADAPTIVE_PACKET_KEY)
    reservation_claim_payload = metadata.get(_ADAPTIVE_CLAIM_KEY)
    binding = metadata.get(_ADAPTIVE_LIFECYCLE_KEY)
    if not all(
        isinstance(value, dict)
        for value in (
            request_payload,
            packet_payload,
            reservation_claim_payload,
            binding,
        )
    ):
        raise AdaptiveRiskContractError(
            "adaptive claim request/packet/claim/binding is incomplete"
        )
    request = load_adaptive_risk_reservation_request(request_payload)
    reservation_claim = load_and_verify_adaptive_risk_reservation_claim(
        packet_payload,
        reservation_claim_payload,
    )
    if binding.get("schema_version") != _ADAPTIVE_LIFECYCLE_SCHEMA:
        raise AdaptiveRiskContractError("adaptive lifecycle schema mismatch")
    try:
        reservation_id = uuid.UUID(str(binding.get("reservation_id") or ""))
    except (TypeError, ValueError) as exc:
        raise AdaptiveRiskContractError(
            "adaptive lifecycle reservation id is invalid"
        ) from exc
    connection_generation = str(
        binding.get("connection_generation") or ""
    ).strip()
    expected_account_id = str(
        getattr(settings, "chili_alpaca_expected_account_id", "") or ""
    ).strip()
    try:
        expected_identity = alpaca_paper_account_identity_sha256(
            expected_account_id
        )
    except (TypeError, ValueError):
        expected_identity = ""
    entry_proof = metadata.get("entry_handoff_proof")
    entry_proof = entry_proof if isinstance(entry_proof, dict) else {}
    entry_cid = (
        str(claim.get("client_order_id") or "").strip()
        if claim.get("action") == "entry"
        else str(entry_proof.get("entry_client_order_id") or "").strip()
    )
    entry_oid = (
        str(claim.get("broker_order_id") or "").strip()
        if claim.get("action") == "entry"
        else str(entry_proof.get("entry_broker_order_id") or "").strip()
    )
    order_request = metadata.get("order_request")
    order_request = order_request if isinstance(order_request, dict) else {}
    try:
        frozen_quantity = float(order_request.get("base_size"))
        frozen_limit = float(order_request.get("limit_price"))
    except (TypeError, ValueError):
        frozen_quantity = frozen_limit = math.nan
    immutable_ok = bool(
        expected_account_id
        and connection_generation
        and request.inputs.execution_surface == "alpaca_paper"
        and request.inputs.execution_family == "alpaca_spot"
        and request.inputs.venue == "alpaca"
        and request.inputs.broker_environment == "paper"
        and request.account_scope == "alpaca:paper"
        and request.account_snapshot.account_scope == "alpaca:paper"
        and request.inputs.symbol == str(claim.get("symbol") or "").strip().upper()
        and request.inputs.account_identity_sha256 == expected_identity
        and request.account_snapshot.account_identity_sha256 == expected_identity
        and reservation_claim.account_identity_sha256 == expected_identity
        and reservation_claim.symbol == request.inputs.symbol
        and reservation_claim.claim_id == request.client_order_id
        and reservation_claim.quantity_shares > 0
        and str(order_request.get("product_id") or "").strip().upper()
        == request.inputs.symbol
        and str(order_request.get("side") or "").strip().lower() == "buy"
        and str(order_request.get("position_intent") or "").strip().lower()
        == "buy_to_open"
        and str(order_request.get("client_order_id") or "").strip()
        == request.client_order_id
        and str(order_request.get("alpaca_account_id") or "").strip()
        == expected_account_id
        and math.isfinite(frozen_quantity)
        and int(frozen_quantity) == reservation_claim.quantity_shares
        and abs(frozen_quantity - reservation_claim.quantity_shares) <= 1e-9
        and math.isfinite(frozen_limit)
        and math.isclose(
            frozen_limit,
            request.entry_limit_price,
            rel_tol=1e-12,
            abs_tol=1e-9,
        )
        and entry_cid == request.client_order_id
        and entry_oid
        and str(binding.get("request_sha256") or "") == request.request_sha256
        and str(binding.get("decision_packet_sha256") or "")
        == reservation_claim.decision_packet_sha256
        and str(binding.get("account_scope") or "") == request.account_scope
        and str(binding.get("account_identity_sha256") or "")
        == request.inputs.account_identity_sha256
        and str(binding.get("client_order_id") or "") == request.client_order_id
    )
    if not immutable_ok:
        raise AdaptiveRiskContractError("adaptive lifecycle identity mismatch")

    packet_row = db.get(
        AdaptiveRiskDecisionPacket,
        reservation_claim.decision_packet_sha256,
    )
    if not (
        packet_row is not None
        and packet_row.reservation_request_sha256 == request.request_sha256
        and packet_row.account_scope == request.account_scope
        and packet_row.symbol == request.inputs.symbol
        and packet_row.client_order_id == request.client_order_id
        and packet_row.account_identity_sha256
        == request.inputs.account_identity_sha256
        and int(packet_row.resolved_quantity_shares)
        == int(reservation_claim.quantity_shares)
    ):
        raise AdaptiveRiskContractError(
            "adaptive immutable decision packet does not match the claim"
        )
    bind = db.get_bind()
    engine = getattr(bind, "engine", bind)
    store = AdaptiveRiskReservationStore(engine)
    state = store.read_state(reservation_id, session=db)
    if not (
        state.decision_packet_sha256 == reservation_claim.decision_packet_sha256
        and state.account_scope == request.account_scope
        and state.symbol == request.inputs.symbol
        and int(state.planned_quantity_shares)
        == int(reservation_claim.quantity_shares)
    ):
        raise AdaptiveRiskContractError("adaptive reservation projection mismatch")
    if state.broker_order_id is not None and state.broker_order_id != entry_oid:
        raise AdaptiveRiskContractError("adaptive entry broker order id mismatch")
    if state.broker_connection_generation is not None:
        if state.broker_connection_generation != connection_generation:
            raise AdaptiveRiskContractError(
                "adaptive broker connection generation mismatch"
            )
    else:
        owner_generation = _adaptive_owner_connection_generation(db, claim)
        if owner_generation != connection_generation:
            raise AdaptiveRiskContractError(
                "adaptive unbound connection generation is unverifiable"
            )
    return {
        "request": request,
        "reservation_claim": reservation_claim,
        "reservation_id": reservation_id,
        "connection_generation": connection_generation,
        "entry_broker_order_id": entry_oid,
        "binding": dict(binding),
        "store": store,
        "state": state,
    }


def _adaptive_claim_preflight(
    db: Session,
    claim: dict[str, Any],
) -> tuple[str, str | None]:
    """Classify a claim before any broker read/cancel/submit boundary."""

    metadata = claim.get("metadata")
    if not _adaptive_marker_present(metadata):
        return "legacy", None
    try:
        readable, locked = read_action_claim(
            db,
            symbol=claim["symbol"],
            account_scope=claim["account_scope"],
            for_update=True,
        )
        if not (
            readable
            and locked is not None
            and locked.get("claim_token") == claim.get("claim_token")
            and locked.get("action") == claim.get("action")
        ):
            raise AdaptiveRiskContractError("adaptive claim changed before preflight")
        _adaptive_claim_context(db, locked)
        db.rollback()
        return "adaptive", None
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        return "invalid", type(exc).__name__


def _load_persisted_lifecycle_evidence(
    db: Session,
    *,
    reservation_id: uuid.UUID,
    provider_event_id: str,
) -> DurableOrderLifecycleEvidence | None:
    event = db.scalar(
        select(AdaptiveRiskReservationEvent)
        .where(AdaptiveRiskReservationEvent.reservation_id == reservation_id)
        .where(AdaptiveRiskReservationEvent.broker_event_id == provider_event_id)
    )
    if event is None:
        return None
    details = dict((event.payload_json or {}).get("details") or {})
    raw = details.get("lifecycle_evidence")
    if not isinstance(raw, dict):
        raise AdaptiveRiskContractError(
            "persisted adaptive lifecycle evidence is missing"
        )
    values = dict(raw)
    expected_sha = str(values.pop("evidence_sha256", "") or "")
    values["observed_at"] = _parse_aware_utc(values.get("observed_at"))
    values["available_at"] = _parse_aware_utc(values.get("available_at"))
    evidence = DurableOrderLifecycleEvidence(**values)
    if evidence.evidence_sha256 != expected_sha:
        raise AdaptiveRiskContractError(
            "persisted adaptive lifecycle evidence hash mismatch"
        )
    return evidence


def _adaptive_lifecycle_evidence(
    db: Session,
    *,
    context: dict[str, Any],
    body: dict[str, Any],
    event_kind: str,
    order_status: str,
    cumulative_fill: int,
    broker_order_id: str,
    source_table: str,
    provider_prefix: str,
    remaining_open_quantity: int | None = None,
) -> DurableOrderLifecycleEvidence:
    content = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    content_sha = hashlib.sha256(content).hexdigest()
    provider_event_id = f"{provider_prefix}:{event_kind}:{content_sha}"
    existing = _load_persisted_lifecycle_evidence(
        db,
        reservation_id=context["reservation_id"],
        provider_event_id=provider_event_id,
    )
    if existing is not None:
        return existing
    state = context["state"]
    now = datetime.now(timezone.utc)
    floors = [
        value
        for value in (
            _aware_utc(state.last_broker_observed_at),
            _aware_utc(state.last_broker_available_at),
        )
        if value is not None
    ]
    clock = max([now, *floors])
    request = context["request"]
    return DurableOrderLifecycleEvidence(
        event_kind=event_kind,
        durability_kind="authoritative_broker_event",
        provider_event_id=provider_event_id,
        broker_source="alpaca",
        connection_generation=context["connection_generation"],
        account_scope=request.account_scope,
        execution_family="alpaca_spot",
        broker_environment="paper",
        account_identity_sha256=request.inputs.account_identity_sha256,
        client_order_id=request.client_order_id,
        broker_order_id=broker_order_id,
        observed_at=clock,
        available_at=clock,
        event_content_sha256=content_sha,
        cumulative_filled_quantity=int(cumulative_fill),
        remaining_open_quantity=remaining_open_quantity,
        source_record_table=source_table,
        source_record_id=f"{broker_order_id}:{content_sha}",
        order_status=order_status,
    )


def _adaptive_integer_quantity(value: Any, field: str) -> int:
    quantity = float(value or 0.0)
    rounded = int(round(quantity))
    if (
        not math.isfinite(quantity)
        or quantity < 0.0
        or abs(quantity - rounded) > 1e-8
    ):
        raise AdaptiveRiskContractError(
            f"adaptive Alpaca {field} must be a non-negative integer"
        )
    return rounded


def _adaptive_order_evidence(
    db: Session,
    *,
    context: dict[str, Any],
    order: Any,
    event_kind: str,
    order_status: str,
    cumulative_fill: int,
) -> DurableOrderLifecycleEvidence:
    raw = getattr(order, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    oid = str(getattr(order, "order_id", "") or "").strip()
    body = {
        "schema_version": "chili.alpaca-rest-order-observation.v2",
        "adaptive_reservation_id": str(context["reservation_id"]),
        "adaptive_request_sha256": context["request"].request_sha256,
        "event_kind": event_kind,
        "order_id": oid,
        "client_order_id": str(getattr(order, "client_order_id", "") or ""),
        "symbol": str(getattr(order, "product_id", "") or "").strip().upper(),
        "side": str(getattr(order, "side", "") or "").strip().lower(),
        "status": str(getattr(order, "status", "") or "").strip().lower(),
        "alpaca_status": str(raw.get("alpaca_status") or "").strip().lower(),
        "filled_size": int(cumulative_fill),
        "average_filled_price": getattr(order, "average_filled_price", None),
        "qty": raw.get("qty"),
        "created_time": getattr(order, "created_time", None),
        "submitted_at": raw.get("submitted_at"),
        "filled_at": raw.get("filled_at"),
    }
    return _adaptive_lifecycle_evidence(
        db,
        context=context,
        body=body,
        event_kind=event_kind,
        order_status=order_status,
        cumulative_fill=cumulative_fill,
        broker_order_id=oid,
        source_table="alpaca_rest_order_observations",
        provider_prefix="alpaca-rest-reconcile",
    )


def _adaptive_position_evidence(
    db: Session,
    *,
    context: dict[str, Any],
    close_order: Any,
    remaining: int,
) -> DurableOrderLifecycleEvidence:
    state = context["state"]
    entry_oid = str(state.broker_order_id or context["entry_broker_order_id"])
    body = {
        "schema_version": "chili.alpaca-rest-position-observation.v2",
        "adaptive_reservation_id": str(context["reservation_id"]),
        "adaptive_request_sha256": context["request"].request_sha256,
        "symbol": context["request"].inputs.symbol,
        "entry_broker_order_id": entry_oid,
        "entry_cumulative_filled_quantity": int(
            state.cumulative_filled_quantity_shares
        ),
        "remaining_quantity": int(remaining),
        "exact_close_order_id": str(
            getattr(close_order, "order_id", "") or ""
        ),
        "exact_close_client_order_id": str(
            getattr(close_order, "client_order_id", "") or ""
        ),
        "exact_close_status": str(
            getattr(close_order, "status", "") or ""
        ).strip().lower(),
        "exact_close_filled_size": getattr(close_order, "filled_size", None),
    }
    return _adaptive_lifecycle_evidence(
        db,
        context=context,
        body=body,
        event_kind="position_flat" if remaining == 0 else "position_reduced",
        order_status="flat" if remaining == 0 else "partially_exited",
        cumulative_fill=int(state.cumulative_filled_quantity_shares),
        remaining_open_quantity=int(remaining),
        broker_order_id=entry_oid,
        source_table="alpaca_rest_position_observations",
        provider_prefix="alpaca-position-reconcile",
    )


def _capture_adaptive_reconcile_order_fills(
    db: Session,
    adapter: Any,
    *,
    context: dict[str, Any],
    provider_order_id: str,
    expected_exit_client_order_id: str | None = None,
) -> None:
    """Append an exact PAPER fill batch in the reconciler's transaction."""

    captured = capture_verified_alpaca_paper_order_fills(
        db,
        adapter=adapter,
        reservation_id=context["reservation_id"],
        provider_order_id=str(provider_order_id or "").strip(),
        expected_exit_client_order_id=expected_exit_client_order_id,
    )
    if captured.observed_count <= 0:
        raise AdaptiveRiskContractError(
            "adaptive reconcile order has no exact fill activity"
        )


def _apply_adaptive_entry_order_lifecycle(
    db: Session,
    *,
    context: dict[str, Any],
    order: Any,
) -> Any:
    """Apply exact cumulative fill and terminal remainder in the caller tx."""

    request = context["request"]
    oid = str(getattr(order, "order_id", "") or "").strip()
    cid = str(getattr(order, "client_order_id", "") or "").strip()
    symbol = str(getattr(order, "product_id", "") or "").strip().upper()
    side = str(getattr(order, "side", "") or "").strip().lower()
    if not (
        oid == context["entry_broker_order_id"]
        and cid == request.client_order_id
        and symbol == request.inputs.symbol
        and side == "buy"
    ):
        raise AdaptiveRiskContractError(
            "adaptive detached entry order identity mismatch"
        )
    state = context["state"]
    cumulative = _adaptive_integer_quantity(
        getattr(order, "filled_size", 0.0),
        "cumulative fill",
    )
    status = str(getattr(order, "status", "") or "").strip().lower()
    raw = getattr(order, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    raw_status = str(raw.get("alpaca_status") or status).strip().lower()
    if cumulative < int(state.cumulative_filled_quantity_shares):
        raise AdaptiveRiskContractError(
            "adaptive Alpaca cumulative fill regressed below durable truth"
        )
    if cumulative > int(state.cumulative_filled_quantity_shares):
        state = context["store"].apply_cumulative_fill(
            context["reservation_id"],
            evidence=_adaptive_order_evidence(
                db,
                context={**context, "state": state},
                order=order,
                event_kind="cumulative_fill",
                order_status=(
                    "filled"
                    if status == "filled" or raw_status == "filled"
                    else "partially_filled"
                ),
                cumulative_fill=cumulative,
            ),
            session=db,
        )
    terminal_partial = cumulative > 0 and status in {
        "canceled",
        "cancelled",
        "expired",
    }
    terminal_zero = cumulative == 0 and status in {
        "rejected",
        "canceled",
        "cancelled",
        "expired",
    }
    if terminal_partial and float(state.pending_structural_risk_usd) > 0.0:
        state = context["store"].finalize_filled_entry_remainder(
            context["reservation_id"],
            evidence=_adaptive_order_evidence(
                db,
                context={**context, "state": state},
                order=order,
                event_kind="filled_entry_terminal",
                order_status="canceled" if status == "cancelled" else status,
                cumulative_fill=cumulative,
            ),
            session=db,
        )
    elif terminal_zero and state.state != "released":
        release_reason = {
            "rejected": "broker_rejected",
            "canceled": "broker_canceled",
            "cancelled": "broker_canceled",
            "expired": "broker_expired",
        }[status]
        state = context["store"].release_zero_fill(
            context["reservation_id"],
            reason=release_reason,
            evidence=_adaptive_order_evidence(
                db,
                context={**context, "state": state},
                order=order,
                event_kind="terminal_zero_fill",
                order_status="canceled" if status == "cancelled" else status,
                cumulative_fill=0,
            ),
            session=db,
        )
    elif status == "filled" or raw_status == "filled":
        if cumulative != int(state.planned_quantity_shares):
            raise AdaptiveRiskContractError(
                "terminal filled order differs from adaptive planned quantity"
            )
    elif status not in {"canceled", "cancelled", "expired", "rejected"}:
        raise AdaptiveRiskContractError(
            "adaptive detached entry terminal status is unsupported"
        )
    context["state"] = state
    return state


def _refreshed_adaptive_binding(context: dict[str, Any], state: Any) -> dict[str, Any]:
    binding = dict(context["binding"])
    binding.update(
        {
            "schema_version": _ADAPTIVE_LIFECYCLE_SCHEMA,
            "state": state.state,
            "planned_quantity_shares": int(state.planned_quantity_shares),
            "cumulative_filled_quantity_shares": int(
                state.cumulative_filled_quantity_shares
            ),
            "open_quantity_shares": int(state.open_quantity_shares),
            "broker_order_id": state.broker_order_id,
            "opportunity_status": state.opportunity_status,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    )
    return binding


def _handoff_detached_entry_claim(
    db: Session,
    *,
    claim: dict[str, Any],
    order: Any,
    broker_position_qty: float,
    authority_proof: dict[str, Any],
) -> dict[str, Any]:
    """Atomically rewrite the same permit from entry proof to close authority."""
    # Lock in the same order as live ticks/terminalization: session first, then
    # symbol claim. Reversing these locks can deadlock a reconciler against a live
    # owner that already holds its session row and is committing claim progress.
    owner_state = _detached_entry_owner_state(db, claim, for_update=True)
    if owner_state not in {"terminal", "missing"}:
        db.rollback()
        return {"ok": False, "reason": f"owner_{owner_state}"}
    readable, locked = read_action_claim(
        db,
        symbol=claim["symbol"],
        account_scope=claim["account_scope"],
        for_update=True,
    )
    if (
        not readable
        or locked is None
        or locked.get("claim_token") != claim.get("claim_token")
        or locked.get("action") != "entry"
        or locked.get("phase") == RESOLVED
        or locked.get("owner_session_id") != claim.get("owner_session_id")
        or locked.get("updated_at") != claim.get("updated_at")
    ):
        db.rollback()
        return {"ok": False, "reason": "entry_claim_changed"}
    if not _entry_claim_order_matches(order, locked):
        db.rollback()
        return {"ok": False, "reason": "entry_order_identity_mismatch"}
    locked_metadata = locked.get("metadata")
    locked_metadata = locked_metadata if isinstance(locked_metadata, dict) else {}
    owner_transport = locked_metadata.get("owner_transport")
    if (
        isinstance(owner_transport, dict)
        and str(owner_transport.get("phase") or "").strip().lower() != RESOLVED
    ):
        db.rollback()
        return {"ok": False, "reason": "entry_owner_transport_unresolved"}

    proof = dict(authority_proof or {})
    if not (
        proof.get("proof_version") == "durable_entry_claim_handoff_v1"
        and proof.get("entry_claim_token") == locked.get("claim_token")
        and proof.get("entry_client_order_id") == locked.get("client_order_id")
        and proof.get("entry_broker_order_id")
        == str(getattr(order, "order_id", "") or "")
        and proof.get("entry_account_scope") == "alpaca:paper"
        and proof.get("no_competing_open_orders") is True
    ):
        db.rollback()
        return {"ok": False, "reason": "entry_handoff_authority_invalid"}

    entry_side = str(getattr(order, "side", "") or "").strip().lower()
    close_side = "sell" if broker_position_qty > 0.0 else "buy"
    if (entry_side == "buy" and broker_position_qty <= 0.0) or (
        entry_side == "sell" and broker_position_qty >= 0.0
    ):
        db.rollback()
        return {"ok": False, "reason": "broker_position_direction_mismatch"}
    close_qty = abs(float(broker_position_qty))
    filled_qty = _positive_finite(getattr(order, "filled_size", None))
    proof_filled_qty = _positive_finite(proof.get("entry_filled_size"))
    equality_tolerance = max(1e-6, (filled_qty or 0.0) * 1e-6)
    if (
        filled_qty is None
        or proof_filled_qty is None
        or abs(proof_filled_qty - filled_qty) > equality_tolerance
        or abs(close_qty - filled_qty) > equality_tolerance
    ):
        db.rollback()
        return {"ok": False, "reason": "entry_fill_position_quantity_mismatch"}
    if close_qty <= 1e-9:
        db.rollback()
        return {"ok": False, "reason": "broker_position_zero"}

    adaptive_binding = None
    try:
        adaptive_context = _adaptive_claim_context(db, locked)
        if adaptive_context is not None:
            adaptive_state = _apply_adaptive_entry_order_lifecycle(
                db,
                context=adaptive_context,
                order=order,
            )
            close_quantity = _adaptive_integer_quantity(
                close_qty,
                "handoff position quantity",
            )
            if not (
                adaptive_state.state == "filled"
                and float(adaptive_state.pending_structural_risk_usd) == 0.0
                and int(adaptive_state.open_quantity_shares) == close_quantity
            ):
                raise AdaptiveRiskContractError(
                    "adaptive handoff does not own the exact open quantity"
                )
            adaptive_binding = _refreshed_adaptive_binding(
                adaptive_context,
                adaptive_state,
            )
    except (AdaptiveRiskContractError, AdaptiveReservationError, TypeError, ValueError):
        db.rollback()
        return {"ok": False, "reason": "adaptive_entry_lifecycle_conflict"}

    old_cid = str(locked.get("client_order_id") or "")
    entry_oid = str(getattr(order, "order_id", "") or "")
    digest = hashlib.sha256(
        (
            f"{locked['account_scope']}|{locked['symbol']}|"
            f"{locked['claim_token']}|{old_cid}|{entry_oid}|orphan-handoff"
        ).encode("utf-8")
    ).hexdigest()
    new_token = f"orphan-handoff-{digest[:32]}"
    new_cid = f"orphrec-{locked['symbol']}-{digest[:20]}"[:48]
    old_metadata = dict(locked.get("metadata") or {})
    new_metadata = {
        **old_metadata,
        "session_id": locked.get("owner_session_id"),
        "qty": close_qty,
        "symbol": locked["symbol"],
        "close_side": close_side,
        "position_intent": (
            "sell_to_close" if close_side == "sell" else "buy_to_close"
        ),
        "close_attempt_no": 1,
        "close_attempt_history": [],
        "terminal_entry_handoff": True,
        "entry_handoff_proof": {
            **proof,
            "owner_state": owner_state,
            "handed_off_at_utc": datetime.now(timezone.utc).isoformat(),
        },
    }
    if adaptive_binding is not None:
        new_metadata[_ADAPTIVE_LIFECYCLE_KEY] = adaptive_binding
    now = datetime.now(timezone.utc)
    updated = db.execute(text(
        "UPDATE broker_symbol_action_claims SET "
        " claim_token = :new_token, action = 'orphan_flatten', phase = 'claimed',"
        " owner_session_id = NULL, client_order_id = :new_cid, broker_order_id = NULL,"
        " metadata_json = CAST(:metadata AS jsonb), claimed_at = :now, updated_at = :now,"
        " lease_expires_at = :now, resolved_at = NULL "
        "WHERE account_scope = :scope AND symbol = :symbol "
        "  AND claim_token = :old_token AND action = 'entry' AND phase <> 'resolved'"
    ), {
        "new_token": new_token,
        "new_cid": new_cid,
        "metadata": json.dumps(new_metadata, separators=(",", ":"), default=str),
        "now": now,
        "scope": locked["account_scope"],
        "symbol": locked["symbol"],
        "old_token": locked["claim_token"],
    })
    if int(updated.rowcount or 0) != 1:
        db.rollback()
        return {"ok": False, "reason": "entry_claim_handoff_raced"}
    reread_ok, handed = read_action_claim(
        db,
        symbol=locked["symbol"],
        account_scope=locked["account_scope"],
        for_update=True,
    )
    if not reread_ok or handed is None or handed.get("claim_token") != new_token:
        db.rollback()
        return {"ok": False, "reason": "entry_claim_handoff_unreadable"}
    try:
        db.commit()
    except Exception:
        db.rollback()
        return {"ok": False, "reason": "entry_claim_handoff_commit_failed"}
    return {"ok": True, "claim": handed, "owner_state": owner_state}


def _resolve_detached_adaptive_entry_flat(
    db: Session,
    *,
    adapter: Any,
    claim: dict[str, Any],
    order: Any,
    owner_state: str,
) -> dict[str, Any]:
    """Atomically map exact entry+flat truth into both durable ledgers."""

    try:
        locked_owner_state = _detached_entry_owner_state(
            db,
            claim,
            for_update=True,
        )
        if locked_owner_state not in {"terminal", "missing"}:
            raise AdaptiveRiskContractError(
                "adaptive detached entry owner is no longer terminal"
            )
        readable, locked = read_action_claim(
            db,
            symbol=claim["symbol"],
            account_scope=claim["account_scope"],
            for_update=True,
        )
        if not (
            readable
            and locked is not None
            and locked.get("claim_token") == claim.get("claim_token")
            and locked.get("action") == "entry"
            and locked.get("phase") != RESOLVED
            and locked.get("updated_at") == claim.get("updated_at")
            and locked.get("owner_session_id") == claim.get("owner_session_id")
            and _entry_claim_order_matches(order, locked)
        ):
            raise AdaptiveRiskContractError(
                "adaptive detached entry claim changed"
            )
        metadata = locked.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        owner_transport = metadata.get("owner_transport")
        if (
            isinstance(owner_transport, dict)
            and str(owner_transport.get("phase") or "").strip().lower()
            != RESOLVED
        ):
            raise AdaptiveRiskContractError(
                "adaptive detached owner transport remains unresolved"
            )
        context = _adaptive_claim_context(db, locked)
        if context is None:
            raise AdaptiveRiskContractError(
                "adaptive detached resolver received a legacy claim"
            )
        state = _apply_adaptive_entry_order_lifecycle(
            db,
            context=context,
            order=order,
        )
        if state.state == "exposure_quarantined":
            if not update_action_claim_phase(
                db,
                symbol=locked["symbol"],
                claim_token=locked["claim_token"],
                phase=locked["phase"],
                client_order_id=locked.get("client_order_id"),
                broker_order_id=str(getattr(order, "order_id", "") or ""),
                metadata={
                    "adaptive_late_fill_contradiction": True,
                    "adaptive_reconciliation_required": True,
                    _ADAPTIVE_LIFECYCLE_KEY: _refreshed_adaptive_binding(
                        context,
                        state,
                    ),
                },
                account_scope=locked["account_scope"],
            ):
                raise AdaptiveRiskContractError(
                    "adaptive detached quarantine audit failed"
                )
            db.commit()
            return {
                "ok": False,
                "reason": "adaptive_late_fill_exposure_quarantined",
                "quarantined": True,
                "state": state,
            }
        filled = _adaptive_integer_quantity(
            getattr(order, "filled_size", 0.0),
            "cumulative fill",
        )
        if filled > 0:
            _capture_adaptive_reconcile_order_fills(
                db,
                adapter,
                context=context,
                provider_order_id=context["entry_broker_order_id"],
            )
        if filled > 0 and state.state != "flat_pending_settlement":
            if not (
                state.state == "filled"
                and float(state.pending_structural_risk_usd) == 0.0
                and int(state.open_quantity_shares) > 0
            ):
                raise AdaptiveRiskContractError(
                    "adaptive entry cannot close before terminal remainder"
                )
            context["state"] = state
            state = context["store"].close_open_exposure(
                context["reservation_id"],
                evidence=_adaptive_position_evidence(
                    db,
                    context=context,
                    close_order=order,
                    remaining=0,
                ),
                reason="detached_terminal_owner_broker_flat",
                session=db,
            )
        if filled == 0 and state.state != "released":
            raise AdaptiveRiskContractError(
                "adaptive zero-fill entry was not safely released"
            )
        if filled > 0 and state.state != "flat_pending_settlement":
            raise AdaptiveRiskContractError(
                "adaptive filled entry did not retain flat settlement debt"
            )
        status = str(getattr(order, "status", "") or "").strip().lower()
        resolved = resolve_action_claim(
            db,
            symbol=locked["symbol"],
            claim_token=locked["claim_token"],
            client_order_id=locked.get("client_order_id"),
            broker_order_id=str(getattr(order, "order_id", "") or ""),
            broker_order_status=status,
            broker_position_zero=True,
            zero_fill_terminal=bool(filled == 0),
            terminal_owner_broker_flat=bool(filled > 0),
            metadata={
                "reason": "detached_terminal_entry_broker_flat",
                "owner_state": owner_state,
                "filled_size": filled,
                "cycle_settlement_pending": bool(filled > 0),
                "cycle_settlement_pending_reason": (
                    "owned_exit_fill_activity_unavailable"
                    if filled > 0
                    else None
                ),
                _ADAPTIVE_LIFECYCLE_KEY: _refreshed_adaptive_binding(
                    context,
                    state,
                ),
            },
            account_scope=locked["account_scope"],
        )
        if not resolved:
            raise AdaptiveRiskContractError(
                "adaptive detached exact claim resolution failed"
            )
        db.commit()
        return {"ok": True, "state": state}
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        return {
            "ok": False,
            "reason": "adaptive_detached_flat_lifecycle_conflict",
            "error_type": type(exc).__name__,
        }


def _rotate_orphan_claim_for_residual(
    db: Session,
    *,
    claim: dict[str, Any],
    order: Any,
    broker_position_qty: float,
) -> dict[str, Any]:
    """Atomically mint one deterministic successor after exact terminal proof."""
    readable, locked = read_action_claim(
        db,
        symbol=claim["symbol"],
        account_scope=claim["account_scope"],
        for_update=True,
    )
    if (
        not readable
        or locked is None
        or locked.get("claim_token") != claim.get("claim_token")
        or locked.get("action") != "orphan_flatten"
        or locked.get("phase") == RESOLVED
    ):
        db.rollback()
        return {"ok": False, "reason": "orphan_claim_changed"}
    if not _orphan_claim_order_matches(order, locked):
        db.rollback()
        return {"ok": False, "reason": "terminal_close_identity_mismatch"}
    status = str(getattr(order, "status", "") or "").strip().lower()
    if status not in _ENTRY_TERMINAL_STATUSES:
        db.rollback()
        return {"ok": False, "reason": "close_not_terminal"}
    metadata = dict(locked.get("metadata") or {})
    authority_ok, authority_reason = _strict_terminal_handoff_claim_authority(locked)
    if not authority_ok:
        db.rollback()
        return {"ok": False, "reason": authority_reason}
    if metadata.get("runner_emergency_close_only") is True:
        db.rollback()
        return {"ok": False, "reason": "runner_close_only_residual_quarantined"}
    close_side = str(metadata.get("close_side") or "sell").strip().lower()
    if (close_side == "sell" and broker_position_qty <= 0.0) or (
        close_side == "buy" and broker_position_qty >= 0.0
    ):
        db.rollback()
        return {"ok": False, "reason": "residual_direction_mismatch"}
    prior_qty = _positive_finite(metadata.get("qty"))
    try:
        terminal_filled = max(
            0.0,
            float(getattr(order, "filled_size", 0.0) or 0.0),
        )
    except (TypeError, ValueError):
        terminal_filled = -1.0
    expected_remaining = (
        max(0.0, prior_qty - terminal_filled)
        if prior_qty is not None and terminal_filled >= 0.0
        else None
    )
    if (
        expected_remaining is None
        or expected_remaining <= 1e-9
        or abs(abs(float(broker_position_qty)) - expected_remaining)
        > max(1e-6, expected_remaining * 1e-6)
    ):
        db.rollback()
        return {"ok": False, "reason": "residual_not_exact_proven_remainder"}
    attempt_no = max(1, int(metadata.get("close_attempt_no") or 1))
    next_attempt = attempt_no + 1
    root = str(
        (metadata.get("entry_handoff_proof") or {}).get("entry_claim_token")
        if isinstance(metadata.get("entry_handoff_proof"), dict)
        else ""
    ) or str(metadata.get("orphan_root_token") or locked["claim_token"])
    digest = hashlib.sha256(
        f"{locked['account_scope']}|{locked['symbol']}|{root}|close-attempt|{next_attempt}".encode(
            "utf-8"
        )
    ).hexdigest()
    new_token = f"orphan-residual-{digest[:32]}"
    new_cid = f"orphrec-{locked['symbol']}-{digest[:20]}"[:48]
    history = list(metadata.get("close_attempt_history") or [])
    history.append({
        "attempt_no": attempt_no,
        "claim_token": locked["claim_token"],
        "client_order_id": locked.get("client_order_id"),
        "broker_order_id": str(getattr(order, "order_id", "") or ""),
        "status": status,
        "filled_size": float(getattr(order, "filled_size", 0.0) or 0.0),
        "broker_position_qty_after": float(broker_position_qty),
        "terminal_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    metadata.update({
        "orphan_root_token": root,
        "close_attempt_no": next_attempt,
        "close_attempt_history": history[-_HANDOFF_CLOSE_HISTORY_MAX:],
        "qty": abs(float(broker_position_qty)),
        "residual_retry": True,
        "residual_retry_authority_exhausted": False,
    })
    # A successor CID is a new exact request.  Never let its broker identity be
    # validated against the terminal predecessor's frozen quantity/type/price.
    metadata.pop("close_request", None)
    now = datetime.now(timezone.utc)
    updated = db.execute(text(
        "UPDATE broker_symbol_action_claims SET "
        " claim_token = :new_token, phase = 'claimed', client_order_id = :new_cid,"
        " broker_order_id = NULL, metadata_json = CAST(:metadata AS jsonb),"
        " claimed_at = :now, updated_at = :now, lease_expires_at = :now, resolved_at = NULL "
        "WHERE account_scope = :scope AND symbol = :symbol "
        "  AND claim_token = :old_token AND action = 'orphan_flatten' "
        "  AND phase <> 'resolved'"
    ), {
        "new_token": new_token,
        "new_cid": new_cid,
        "metadata": json.dumps(metadata, separators=(",", ":"), default=str),
        "now": now,
        "scope": locked["account_scope"],
        "symbol": locked["symbol"],
        "old_token": locked["claim_token"],
    })
    if int(updated.rowcount or 0) != 1:
        db.rollback()
        return {"ok": False, "reason": "residual_rotation_raced"}
    ok, rotated = read_action_claim(
        db,
        symbol=locked["symbol"],
        account_scope=locked["account_scope"],
        for_update=True,
    )
    if not ok or rotated is None or rotated.get("claim_token") != new_token:
        db.rollback()
        return {"ok": False, "reason": "residual_rotation_unreadable"}
    try:
        db.commit()
    except Exception:
        db.rollback()
        return {"ok": False, "reason": "residual_rotation_commit_failed"}
    return {"ok": True, "claim": rotated}


def _close_request_identity(
    request: Any,
    *,
    claim: dict[str, Any],
    close_side: str,
) -> tuple[bool, float | None]:
    """Validate the durable request shape and return its frozen quantity."""
    if not isinstance(request, dict):
        return False, None
    cid = str(claim.get("client_order_id") or "").strip()
    symbol = str(claim.get("symbol") or "").strip().upper()
    side = str(request.get("side") or "").strip().lower()
    intent = str(request.get("position_intent") or "").strip().lower()
    expected_intent = "sell_to_close" if close_side == "sell" else "buy_to_close"
    qty = _positive_finite(request.get("base_size"))
    order_type = str(request.get("order_type") or "").strip().lower()
    tif = str(request.get("time_in_force") or "").strip().lower()
    extended = request.get("extended_hours")
    limit_price = request.get("limit_price")
    valid_shape = bool(
        cid
        and symbol
        and str(request.get("client_order_id") or "").strip() == cid
        and str(request.get("product_id") or "").strip().upper() == symbol
        and side == close_side
        and intent == expected_intent
        and qty is not None
        and order_type in {"market", "limit"}
        and tif == "day"
        and isinstance(extended, bool)
        and (
            (order_type == "market" and extended is False and limit_price is None)
            or (
                order_type == "limit"
                and extended is True
                and _positive_finite(limit_price) is not None
            )
        )
    )
    return valid_shape, qty if valid_shape else None


def _strict_terminal_handoff_claim_authority(
    claim: dict[str, Any],
) -> tuple[bool, str]:
    """Accept only claims derived from a strict durable paper-entry handoff."""
    metadata = claim.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    proof = metadata.get("entry_handoff_proof")
    proof = proof if isinstance(proof, dict) else {}
    token = str(claim.get("claim_token") or "").strip()
    cid = str(claim.get("client_order_id") or "").strip()
    close_side = str(metadata.get("close_side") or "").strip().lower()
    entry_side = str(proof.get("entry_side") or "").strip().lower()
    filled_qty = _positive_finite(proof.get("entry_filled_size"))
    original_position_qty = proof.get("broker_position_qty")
    remaining_qty = _positive_finite(metadata.get("qty"))
    try:
        original_position_qty = float(original_position_qty)
    except (TypeError, ValueError):
        original_position_qty = None
    if not (
        claim.get("account_scope") == "alpaca:paper"
        and claim.get("action") == "orphan_flatten"
        and token
        and cid.startswith("orphrec-")
        and metadata.get("terminal_entry_handoff") is True
        and metadata.get("runner_emergency_close_only") is not True
        and proof.get("proof_version") == "durable_entry_claim_handoff_v1"
        and str(proof.get("entry_claim_token") or "").strip()
        and str(proof.get("entry_client_order_id") or "").strip()
        and str(proof.get("entry_broker_order_id") or "").strip()
        and proof.get("entry_account_scope") == "alpaca:paper"
        and proof.get("no_competing_open_orders") is True
        and entry_side in {"buy", "sell"}
        and close_side == ("sell" if entry_side == "buy" else "buy")
        and filled_qty is not None
        and remaining_qty is not None
        and remaining_qty <= filled_qty + max(1e-6, filled_qty * 1e-6)
        and original_position_qty is not None
        and abs(abs(original_position_qty) - filled_qty)
        <= max(1e-6, filled_qty * 1e-6)
        and ((entry_side == "buy" and original_position_qty > 0.0)
             or (entry_side == "sell" and original_position_qty < 0.0))
    ):
        return False, "strict_terminal_entry_handoff_proof_missing"

    entry_avg = _positive_finite(proof.get("entry_average_filled_price"))
    position_avg = _positive_finite(proof.get("broker_position_avg_entry_price"))
    if entry_avg is not None and (
        position_avg is None
        or abs(position_avg - entry_avg) > max(0.0001, entry_avg * 0.0001)
    ):
        return False, "strict_terminal_entry_handoff_average_mismatch"
    request = metadata.get("close_request")
    if request is not None:
        request_ok, request_qty = _close_request_identity(
            request,
            claim=claim,
            close_side=close_side,
        )
        if (
            not request_ok
            or request_qty is None
            or abs(request_qty - remaining_qty) > max(1e-6, remaining_qty * 1e-6)
        ):
            return False, "strict_terminal_entry_handoff_request_mismatch"
    return True, "strict_terminal_entry_handoff"


def _orphan_claim_order_matches(order: Any, claim: dict[str, Any]) -> bool:
    cid = str(claim.get("client_order_id") or "").strip()
    if not cid or order is None:
        return False
    if str(getattr(order, "client_order_id", "") or "").strip() != cid:
        return False
    expected_oid = str(claim.get("broker_order_id") or "").strip()
    if expected_oid and str(getattr(order, "order_id", "") or "").strip() != expected_oid:
        return False
    if (
        str(getattr(order, "product_id", "") or "").strip().upper()
        != str(claim.get("symbol") or "").strip().upper()
    ):
        return False
    metadata = claim.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    expected_side = str(metadata.get("close_side") or "sell").strip().lower()
    if expected_side not in {"sell", "buy"}:
        return False
    if str(getattr(order, "side", "") or "").strip().lower() != expected_side:
        return False
    request = metadata.get("close_request")
    request_ok, expected_qty = _close_request_identity(
        request,
        claim=claim,
        close_side=expected_side,
    )
    if not request_ok or expected_qty is None:
        return False
    raw = getattr(order, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    # Broker qty is mandatory identity evidence.  A normalized order with no raw
    # qty is indeterminate, never permission to adopt/resolve/rotate a claim.
    broker_qty = _positive_finite(raw.get("qty"))
    if broker_qty is None or abs(broker_qty - expected_qty) > max(1e-9, expected_qty * 1e-8):
        return False
    expected_type = str(request.get("order_type") or "").strip().lower()
    if str(getattr(order, "order_type", "") or "").strip().lower() != expected_type:
        return False

    # These Alpaca fields are preserved by the adapter when exposed by the SDK.
    # Missing optional echoes remain indeterminate metadata; contradictory echoes
    # are conclusive identity mismatches.
    expected_limit = _positive_finite(request.get("limit_price"))
    broker_limit = _positive_finite(raw.get("limit_price"))
    if broker_limit is not None and (
        expected_limit is None
        or abs(broker_limit - expected_limit) > max(1e-9, expected_limit * 1e-8)
    ):
        return False
    broker_tif = str(raw.get("time_in_force") or "").strip().lower()
    if broker_tif and broker_tif != str(request.get("time_in_force") or "").strip().lower():
        return False
    broker_extended = raw.get("extended_hours")
    if broker_extended is not None and bool(broker_extended) is not bool(
        request.get("extended_hours")
    ):
        return False
    broker_intent = str(raw.get("position_intent") or "").strip().lower()
    if broker_intent and broker_intent != str(request.get("position_intent") or "").strip().lower():
        return False
    return bool(str(getattr(order, "order_id", "") or "").strip())


def _exact_claim_order(adapter: Any, claim: dict[str, Any]) -> Any | None:
    """Return only the close order identified by this claim's exact CID."""
    cid = str(claim.get("client_order_id") or "").strip()
    if not cid:
        return None
    try:
        order, _ = adapter.get_order_by_client_order_id(cid)
    except Exception:
        return None
    return order if _orphan_claim_order_matches(order, claim) else None


def _strict_orphan_claim_order_state(
    adapter: Any,
    claim: dict[str, Any],
) -> tuple[str, Any | None]:
    """OID-first truth; only an explicit CID 404 is authoritative absence."""
    oid = str(claim.get("broker_order_id") or "").strip()
    cid = str(claim.get("client_order_id") or "").strip()
    if not cid:
        return "unknown", None
    if oid:
        try:
            order, _ = adapter.get_order(oid)
        except Exception:
            order = None
        if order is not None:
            return (
                ("found", order)
                if _orphan_claim_order_matches(order, claim)
                else ("identity_mismatch", order)
            )
        # A durable broker OID proves the order once existed.  Its temporary
        # absence is never permission to mint another close.
        return "unknown", None
    if not hasattr(adapter, "get_order_by_client_order_id_truth"):
        return "unknown", None
    try:
        truth = adapter.get_order_by_client_order_id_truth(cid)
    except Exception:
        return "unknown", None
    if not isinstance(truth, dict) or not truth.get("readable"):
        return "unknown", None
    order = truth.get("order")
    if truth.get("found") and order is not None:
        return (
            ("found", order)
            if _orphan_claim_order_matches(order, claim)
            else ("identity_mismatch", order)
        )
    if truth.get("found") is False:
        return "absent", None
    return "unknown", None


def _rotate_absent_orphan_claim(
    db: Session,
    *,
    claim: dict[str, Any],
    broker_position_qty: float,
) -> dict[str, Any]:
    """Absence/time can never authorize a successor close identity."""
    return {
        "ok": False,
        "reason": "absent_close_requires_same_cid_terminal_reconciliation",
    }

    # Retained below only as migration documentation for old persisted rows; the
    # recertified entry point above makes it unreachable.
    readable, locked = read_action_claim(
        db,
        symbol=claim["symbol"],
        account_scope=claim["account_scope"],
        for_update=True,
    )
    if (
        not readable
        or locked is None
        or locked.get("claim_token") != claim.get("claim_token")
        or locked.get("action") != "orphan_flatten"
        or locked.get("phase") not in {CLAIMED, SUBMIT_INDETERMINATE}
        or locked.get("broker_order_id") is not None
        or not _claim_lease_expired(locked)
    ):
        db.rollback()
        return {"ok": False, "reason": "absent_close_claim_changed_or_ineligible"}
    metadata = dict(locked.get("metadata") or {})
    authority_ok, authority_reason = _strict_terminal_handoff_claim_authority(locked)
    if not authority_ok:
        db.rollback()
        return {"ok": False, "reason": authority_reason}
    if metadata.get("runner_emergency_close_only") is True:
        db.rollback()
        return {"ok": False, "reason": "runner_close_only_absent_rotation_quarantined"}
    close_side = str(metadata.get("close_side") or "sell").strip().lower()
    request_ok, _ = _close_request_identity(
        metadata.get("close_request"),
        claim=locked,
        close_side=close_side,
    )
    if not request_ok:
        db.rollback()
        return {"ok": False, "reason": "absent_close_request_identity_missing"}
    if (close_side == "sell" and broker_position_qty <= 0.0) or (
        close_side == "buy" and broker_position_qty >= 0.0
    ):
        db.rollback()
        return {"ok": False, "reason": "absent_close_position_direction_mismatch"}
    prior_qty = _positive_finite(metadata.get("qty"))
    if prior_qty is None or abs(
        abs(float(broker_position_qty)) - prior_qty
    ) > max(1e-6, prior_qty * 1e-6):
        db.rollback()
        return {"ok": False, "reason": "absent_close_position_not_exact_proven_qty"}

    attempt_no = max(1, int(metadata.get("close_attempt_no") or 1))
    next_attempt = attempt_no + 1
    root = str(
        (metadata.get("entry_handoff_proof") or {}).get("entry_claim_token")
        if isinstance(metadata.get("entry_handoff_proof"), dict)
        else ""
    ) or str(metadata.get("orphan_root_token") or locked["claim_token"])
    digest = hashlib.sha256(
        f"{locked['account_scope']}|{locked['symbol']}|{root}|close-attempt|{next_attempt}".encode(
            "utf-8"
        )
    ).hexdigest()
    new_token = f"orphan-residual-{digest[:32]}"
    new_cid = f"orphrec-{locked['symbol']}-{digest[:20]}"[:48]
    history = list(metadata.get("close_attempt_history") or [])
    history.append({
        "attempt_no": attempt_no,
        "claim_token": locked["claim_token"],
        "client_order_id": locked.get("client_order_id"),
        "broker_order_id": None,
        "status": "strict_cid_absent_after_grace",
        "broker_position_qty_after": float(broker_position_qty),
        "terminal_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    metadata.update({
        "orphan_root_token": root,
        "close_attempt_no": next_attempt,
        "close_attempt_history": history[-_HANDOFF_CLOSE_HISTORY_MAX:],
        "qty": abs(float(broker_position_qty)),
        "residual_retry": True,
        "strict_prior_cid_absent_after_grace": True,
        "residual_retry_authority_exhausted": False,
    })
    metadata.pop("close_request", None)
    now = datetime.now(timezone.utc)
    updated = db.execute(text(
        "UPDATE broker_symbol_action_claims SET "
        " claim_token = :new_token, phase = 'claimed', client_order_id = :new_cid,"
        " broker_order_id = NULL, metadata_json = CAST(:metadata AS jsonb),"
        " claimed_at = :now, updated_at = :now, lease_expires_at = :now, resolved_at = NULL "
        "WHERE account_scope = :scope AND symbol = :symbol "
        "  AND claim_token = :old_token AND action = 'orphan_flatten' "
        "  AND phase IN ('claimed', 'submit_indeterminate') "
        "  AND broker_order_id IS NULL AND lease_expires_at <= :now"
    ), {
        "new_token": new_token,
        "new_cid": new_cid,
        "metadata": json.dumps(metadata, separators=(",", ":"), default=str),
        "now": now,
        "scope": locked["account_scope"],
        "symbol": locked["symbol"],
        "old_token": locked["claim_token"],
    })
    if int(updated.rowcount or 0) != 1:
        db.rollback()
        return {"ok": False, "reason": "absent_close_rotation_raced"}
    ok, rotated = read_action_claim(
        db,
        symbol=locked["symbol"],
        account_scope=locked["account_scope"],
        for_update=True,
    )
    if not ok or rotated is None or rotated.get("claim_token") != new_token:
        db.rollback()
        return {"ok": False, "reason": "absent_close_rotation_unreadable"}
    try:
        db.commit()
    except Exception:
        db.rollback()
        return {"ok": False, "reason": "absent_close_rotation_commit_failed"}
    return {"ok": True, "claim": rotated}


def _broker_position_is_zero(adapter: Any, symbol: str) -> bool:
    try:
        qty = adapter.get_position_quantity(symbol)
    except Exception:
        return False
    if qty is None:
        return False
    try:
        return abs(float(qty)) <= 1e-9
    except (TypeError, ValueError):
        return False


def _advance_adaptive_orphan_claim_from_order(
    db: Session,
    adapter: Any,
    claim: dict[str, Any],
    order: Any,
    *,
    signed_position_qty: float | None = None,
) -> dict[str, Any]:
    """Atomically bind an exact close and update adaptive open exposure."""

    if signed_position_qty is None:
        signed_position_qty = _signed_broker_position(adapter, claim["symbol"])
    if signed_position_qty is None:
        return {"ok": False, "reason": "broker_position_unreadable"}
    try:
        remaining = _adaptive_integer_quantity(
            abs(float(signed_position_qty)),
            "remaining broker position",
        )
        readable, locked = read_action_claim(
            db,
            symbol=claim["symbol"],
            account_scope=claim["account_scope"],
            for_update=True,
        )
        if not (
            readable
            and locked is not None
            and locked.get("claim_token") == claim.get("claim_token")
            and locked.get("action") == "orphan_flatten"
            and locked.get("phase") != RESOLVED
            and _orphan_claim_order_matches(order, locked)
        ):
            raise AdaptiveRiskContractError(
                "adaptive orphan exact close claim changed"
            )
        context = _adaptive_claim_context(db, locked)
        if context is None:
            raise AdaptiveRiskContractError(
                "adaptive orphan updater received a legacy claim"
            )
        try:
            entry_order, _entry_meta = adapter.get_order(
                context["entry_broker_order_id"]
            )
        except Exception as exc:
            raise AdaptiveRiskContractError(
                "adaptive orphan entry order is unreadable"
            ) from exc
        if entry_order is None:
            raise AdaptiveRiskContractError(
                "adaptive orphan exact entry order is missing"
            )
        state = _apply_adaptive_entry_order_lifecycle(
            db,
            context=context,
            order=entry_order,
        )
        context["state"] = state
        if state.state == "exposure_quarantined":
            if not update_action_claim_phase(
                db,
                symbol=locked["symbol"],
                claim_token=locked["claim_token"],
                phase=SUBMITTED,
                client_order_id=str(
                    getattr(order, "client_order_id", "") or ""
                ),
                broker_order_id=str(getattr(order, "order_id", "") or ""),
                metadata={
                    "adaptive_late_fill_contradiction": True,
                    "adaptive_reconciliation_required": True,
                    _ADAPTIVE_LIFECYCLE_KEY: _refreshed_adaptive_binding(
                        context,
                        state,
                    ),
                },
                account_scope=locked["account_scope"],
            ):
                raise AdaptiveRiskContractError(
                    "adaptive quarantine claim audit failed"
                )
            db.commit()
            return {
                "ok": False,
                "reason": "adaptive_late_fill_exposure_quarantined",
                "quarantined": True,
                "state": state,
            }
        if float(state.pending_structural_risk_usd) != 0.0:
            raise AdaptiveRiskContractError(
                "adaptive orphan entry remainder is not terminal"
            )
        if state.state not in {"filled", "flat_pending_settlement", "closed"}:
            raise AdaptiveRiskContractError(
                "adaptive orphan reservation is not filled"
            )
        if remaining > int(state.open_quantity_shares):
            raise AdaptiveRiskContractError(
                "broker position exceeds adaptive owned open quantity"
            )
        oid = str(getattr(order, "order_id", "") or "").strip()
        cid = str(getattr(order, "client_order_id", "") or "").strip()
        status = str(getattr(order, "status", "") or "").strip().lower()
        if not update_action_claim_phase(
            db,
            symbol=locked["symbol"],
            claim_token=locked["claim_token"],
            phase=SUBMITTED,
            client_order_id=cid,
            broker_order_id=oid,
            metadata={
                "reconciled_order_status": status,
                _ADAPTIVE_LIFECYCLE_KEY: _refreshed_adaptive_binding(
                    context,
                    state,
                ),
            },
            account_scope=locked["account_scope"],
        ):
            raise AdaptiveRiskContractError(
                "adaptive orphan exact close binding failed"
            )
        if remaining < int(state.open_quantity_shares):
            context["state"] = state
            evidence = _adaptive_position_evidence(
                db,
                context=context,
                close_order=order,
                remaining=remaining,
            )
            if remaining == 0:
                state = context["store"].close_open_exposure(
                    context["reservation_id"],
                    evidence=evidence,
                    reason="exact_orphan_close_broker_flat",
                    session=db,
                )
            else:
                state = context["store"].reduce_open_exposure(
                    context["reservation_id"],
                    evidence=evidence,
                    reason="exact_orphan_close_partial_fill",
                    session=db,
                )
        close_filled = _adaptive_integer_quantity(
            getattr(order, "filled_size", 0.0),
            "orphan close cumulative fill",
        )
        if int(state.cumulative_filled_quantity_shares) > 0:
            _capture_adaptive_reconcile_order_fills(
                db,
                adapter,
                context=context,
                provider_order_id=context["entry_broker_order_id"],
            )
        if close_filled > 0:
            _capture_adaptive_reconcile_order_fills(
                db,
                adapter,
                context=context,
                provider_order_id=oid,
                expected_exit_client_order_id=cid,
            )
        if not update_action_claim_phase(
            db,
            symbol=locked["symbol"],
            claim_token=locked["claim_token"],
            phase=SUBMITTED,
            client_order_id=cid,
            broker_order_id=oid,
            metadata={
                "reconciled_order_status": status,
                _ADAPTIVE_LIFECYCLE_KEY: _refreshed_adaptive_binding(
                    context,
                    state,
                ),
            },
            account_scope=locked["account_scope"],
        ):
            raise AdaptiveRiskContractError(
                "adaptive orphan lifecycle refresh failed"
            )
        resolved = False
        terminal = status in _ENTRY_TERMINAL_STATUSES
        if remaining == 0 and terminal:
            settled = settle_flat_alpaca_paper_cycle(
                db,
                reservation_id=context["reservation_id"],
            )
            state = context["store"].read_state(
                context["reservation_id"],
                session=db,
            )
            if state.state != "closed":
                raise AdaptiveRiskContractError(
                    "adaptive orphan exact settlement did not close the cycle"
                )
            resolved = resolve_action_claim(
                db,
                symbol=locked["symbol"],
                claim_token=locked["claim_token"],
                client_order_id=cid,
                broker_order_id=oid,
                broker_order_status=status,
                broker_position_zero=True,
                orphan_handoff_broker_flat=(status != "filled"),
                metadata={
                    "reason": "exact_adaptive_orphan_close_broker_flat",
                    "terminal_close_filled_size": float(
                        getattr(order, "filled_size", 0.0) or 0.0
                    ),
                    "cycle_settlement_sha256": settled.row.settlement_sha256,
                    _ADAPTIVE_LIFECYCLE_KEY: _refreshed_adaptive_binding(
                        context,
                        state,
                    ),
                },
                account_scope=locked["account_scope"],
            )
            if not resolved:
                raise AdaptiveRiskContractError(
                    "adaptive orphan exact claim resolution failed"
                )
        db.commit()
        return {
            "ok": True,
            "resolved": resolved,
            "remaining_quantity": remaining,
            "state": state,
        }
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        return {
            "ok": False,
            "reason": "adaptive_orphan_lifecycle_conflict",
            "error_type": type(exc).__name__,
        }


def _advance_orphan_claim_from_order(
    adapter: Any,
    claim: dict[str, Any],
    order: Any,
    *,
    db: Session | None = None,
    signed_position_qty: float | None = None,
) -> bool:
    """Bind an exact broker order and resolve only on filled+broker-flat proof."""
    metadata = claim.get("metadata")
    if db is not None and _adaptive_marker_present(metadata):
        return bool(
            _advance_adaptive_orphan_claim_from_order(
                db,
                adapter,
                claim,
                order,
                signed_position_qty=signed_position_qty,
            ).get("ok")
        )
    cid = str(claim.get("client_order_id") or "")
    oid = str(getattr(order, "order_id", "") or "").strip()
    status = str(getattr(order, "status", "") or "").strip().lower()
    if not oid:
        return False
    bound = update_action_claim_phase_committed(
        symbol=claim["symbol"],
        claim_token=claim["claim_token"],
        phase=SUBMITTED,
        client_order_id=cid,
        broker_order_id=oid,
        metadata={"reconciled_order_status": status},
        account_scope=claim["account_scope"],
    )
    if not bound:
        return False
    if status != "filled" or not _broker_position_is_zero(adapter, claim["symbol"]):
        return True
    resolve_action_claim_committed(
        symbol=claim["symbol"],
        claim_token=claim["claim_token"],
        client_order_id=cid,
        broker_order_id=oid,
        broker_order_status=status,
        broker_position_zero=True,
        metadata={"reason": "exact_orphan_sell_filled_and_broker_flat"},
        account_scope=claim["account_scope"],
    )
    return True


def _place_alpaca_equity_close(
    adapter: Any,
    *,
    symbol: str,
    close_side: str,
    quantity: float,
    client_order_id: str,
    before_submit: Any | None = None,
    frozen_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Hours-aware, close-only Alpaca equity order boundary.

    Regular hours use a DAY market close. Extended hours require a fresh (<=2s),
    uncrossed execution BBO and a marketable DAY limit with
    ``extended_hours=True``. Closed/no-quote paths perform zero broker POSTs.
    """
    side = str(close_side or "").strip().lower()
    sym = str(symbol or "").strip().upper()
    cid = str(client_order_id or "").strip()
    quarantine_reason = _alpaca_reconcile_shape_quarantine_reason(
        symbol=sym,
        metadata={
            "close_side": side,
            "position_intent": (
                "sell_to_close"
                if side == "sell"
                else "buy_to_close"
                if side == "buy"
                else None
            ),
        },
    )
    if quarantine_reason is not None:
        return {
            "ok": False,
            "pre_place_blocked": True,
            "error": quarantine_reason,
            "execution_quarantined": True,
            "transport_attempted": False,
        }
    if side not in {"sell", "buy"} or quantity <= 0.0 or not cid or not sym:
        return {"ok": False, "pre_place_blocked": True, "error": "invalid_close_instruction"}
    try:
        session = market_session_now(symbol)
    except Exception:
        session = None
    if session not in {"regular", "premarket", "afterhours"}:
        return {
            "ok": False,
            "pre_place_blocked": True,
            "deferred": True,
            "error": "alpaca_equity_exit_session_closed_or_unknown",
            "market_session": session,
            "transport_attempted": False,
        }
    position_intent = "sell_to_close" if side == "sell" else "buy_to_close"
    request: dict[str, Any]
    if frozen_request is not None:
        request = dict(frozen_request)
        request_ok, frozen_qty = _close_request_identity(
            request,
            claim={"symbol": sym, "client_order_id": cid},
            close_side=side,
        )
        if (
            not request_ok
            or frozen_qty is None
            or abs(frozen_qty - float(quantity)) > max(1e-9, frozen_qty * 1e-8)
        ):
            return {
                "ok": False,
                "pre_place_blocked": True,
                "error": "frozen_close_request_identity_mismatch",
                "transport_attempted": False,
            }
    elif session == "regular":
        request = {
            "product_id": sym,
            "side": side,
            "base_size": str(float(quantity)),
            "client_order_id": cid,
            "position_intent": position_intent,
            "order_type": "market",
            "time_in_force": "day",
            "extended_hours": False,
            "limit_price": None,
            "market_session": session,
        }
    else:
        request = {}

    def _durable_before_post(instruction: dict[str, Any]) -> bool:
        if before_submit is None:
            return True
        try:
            return bool(before_submit(dict(instruction)))
        except Exception:
            return False

    order_type = str(request.get("order_type") or "").strip().lower()
    if order_type == "market":
        if session != "regular":
            return {
                "ok": False,
                "pre_place_blocked": True,
                "deferred": True,
                "error": "frozen_market_close_outside_regular_session",
                "market_session": session,
                "transport_attempted": False,
            }
        if not _durable_before_post(request):
            return {
                "ok": False,
                "pre_place_blocked": True,
                "error": "orphan_close_request_not_durable",
                "market_session": session,
                "transport_attempted": False,
            }
        common = {
            key: request[key]
            for key in (
                "product_id", "side", "base_size", "client_order_id",
                "position_intent", "time_in_force", "extended_hours",
            )
        }
        try:
            result = adapter.place_market_order(**common) or {}
        except Exception as exc:
            result = {"ok": False, "error": type(exc).__name__}
        return {
            **dict(result),
            "order_type": "market",
            "market_session": session,
            "transport_attempted": True,
            "close_request": request,
        }

    if frozen_request is not None and order_type != "limit":
        return {
            "ok": False,
            "pre_place_blocked": True,
            "error": "frozen_close_order_type_invalid",
            "transport_attempted": False,
        }

    tick = None
    freshness = None
    try:
        if not hasattr(adapter, "get_execution_bbo"):
            raise RuntimeError("execution_bbo_capability_missing")
        tick, returned_freshness = adapter.get_execution_bbo(
            sym,
            max_age_seconds=2.0,
        )
        freshness = getattr(tick, "freshness", None) or returned_freshness
        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        mid = float(getattr(tick, "mid", 0.0) or 0.0)
        age = float(freshness.age_seconds())
        valid = bool(
            tick is not None
            and str(getattr(tick, "product_id", "") or "").strip().upper()
            == sym
            and all(math.isfinite(v) and v > 0.0 for v in (bid, ask, mid))
            and ask >= bid
            and math.isfinite(age)
            and age <= 2.0
        )
    except Exception:
        valid = False
        bid = ask = mid = 0.0
        age = None
    if not valid or not hasattr(adapter, "place_limit_order_gtc"):
        return {
            "ok": False,
            "pre_place_blocked": True,
            "deferred": True,
            "error": "alpaca_extended_exit_bbo_unavailable",
            "market_session": session,
            "execution_bbo_age_seconds": age,
            "transport_attempted": False,
        }
    try:
        raw_bps = getattr(settings, "chili_momentum_order_notional_guard_bps", 25.0)
        guard = max(0.0, float(25.0 if raw_bps is None else raw_bps)) / 10_000.0 * 8.0
    except (TypeError, ValueError):
        guard = 0.02
    if frozen_request is None:
        raw_limit = bid * (1.0 - guard) if side == "sell" else ask * (1.0 + guard)
        tick_size = Decimal("0.01") if Decimal(str(raw_limit)) >= Decimal("1") else Decimal("0.0001")
        rounding = ROUND_CEILING if side == "buy" else ROUND_FLOOR
        limit_price = float(Decimal(str(raw_limit)).quantize(tick_size, rounding=rounding))
        request = {
            "product_id": sym,
            "side": side,
            "base_size": str(float(quantity)),
            "client_order_id": cid,
            "position_intent": position_intent,
            "order_type": "limit",
            "time_in_force": "day",
            "extended_hours": True,
            "limit_price": limit_price,
            "market_session": session,
            "execution_bbo": {"bid": bid, "ask": ask, "mid": mid},
        }
    else:
        limit_price = float(request["limit_price"])
        # A retry with the same CID must preserve the byte-equivalent frozen
        # instruction, but it still needs to be marketable against a fresh BBO.
        marketable = (
            limit_price <= bid + max(1e-9, bid * 1e-10)
            if side == "sell"
            else limit_price >= ask - max(1e-9, ask * 1e-10)
        )
        if not marketable:
            return {
                "ok": False,
                "pre_place_blocked": True,
                "deferred": True,
                "error": "frozen_extended_close_no_longer_marketable",
                "market_session": session,
                "transport_attempted": False,
            }
    # Literal last-instruction freshness check: the same execution snapshot must
    # still be <=2s at the call boundary. Metadata alone can never authorize it.
    try:
        last_age = float(freshness.age_seconds())
    except Exception:
        last_age = float("inf")
    if not math.isfinite(last_age) or last_age > 2.0:
        return {
            "ok": False,
            "pre_place_blocked": True,
            "deferred": True,
            "error": "alpaca_extended_exit_bbo_stale_at_place",
            "market_session": session,
            "execution_bbo_age_seconds": last_age,
            "transport_attempted": False,
        }
    if not _durable_before_post(request):
        return {
            "ok": False,
            "pre_place_blocked": True,
            "error": "orphan_close_request_not_durable",
            "market_session": session,
            "execution_bbo_age_seconds": last_age,
            "transport_attempted": False,
        }
    common = {
        key: request[key]
        for key in (
            "product_id", "side", "base_size", "client_order_id",
            "position_intent", "time_in_force",
        )
    }
    try:
        result = adapter.place_limit_order_gtc(
            **common,
            limit_price=limit_price,
            extended_hours=True,
        ) or {}
    except Exception as exc:
        result = {"ok": False, "error": type(exc).__name__}
    return {
        **dict(result),
        "order_type": "limit",
        "limit_price": limit_price,
        "extended_hours": True,
        "market_session": session,
        "execution_bbo_age_seconds": last_age,
        "transport_attempted": True,
        "close_request": request,
    }


def _submit_handoff_close(
    adapter: Any,
    claim: dict[str, Any],
    *,
    db: Session | None = None,
) -> dict[str, Any]:
    """Recover or submit one deterministic close for a handed-off entry claim."""
    authority_ok, authority_reason = _strict_terminal_handoff_claim_authority(claim)
    if not authority_ok:
        return {
            "ok": False,
            "reason": authority_reason,
            "execution_quarantined": True,
            "transport_attempted": False,
        }
    metadata = claim.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    quarantine_reason = _alpaca_reconcile_shape_quarantine_reason(
        symbol=claim.get("symbol"),
        metadata=metadata,
    )
    if quarantine_reason is not None:
        return {
            "ok": False,
            "reason": quarantine_reason,
            "execution_quarantined": True,
            "transport_attempted": False,
        }
    existing = _exact_claim_order(adapter, claim)
    if existing is not None:
        advanced = _advance_orphan_claim_from_order(
            adapter,
            claim,
            existing,
            db=db,
        )
        return {
            "ok": bool(advanced),
            "recovered": bool(advanced),
            "order_id": str(getattr(existing, "order_id", "") or ""),
            "status": str(getattr(existing, "status", "") or ""),
        }
    close_side = str(metadata.get("close_side") or "").strip().lower()
    if close_side not in {"sell", "buy"}:
        return {"ok": False, "reason": "handoff_close_side_missing"}
    signed_qty = _signed_broker_position(adapter, claim["symbol"])
    if signed_qty is None:
        return {"ok": False, "reason": "broker_position_unreadable"}
    if abs(signed_qty) <= 1e-9:
        # Flatness cannot prove that a worker which durably froze this close CID
        # never paused immediately before POST. Retain the same authority until
        # exact terminal broker identity exists.
        return {
            "ok": False,
            "reason": "handoff_flat_without_exact_close_terminal_truth",
            "transport_attempted": False,
        }
    if (close_side == "sell" and signed_qty <= 0.0) or (
        close_side == "buy" and signed_qty >= 0.0
    ):
        return {"ok": False, "reason": "handoff_position_direction_mismatch"}
    proven_remaining_qty = _positive_finite(metadata.get("qty"))
    if proven_remaining_qty is None or abs(
        abs(float(signed_qty)) - proven_remaining_qty
    ) > max(1e-6, proven_remaining_qty * 1e-6):
        return {
            "ok": False,
            "reason": "handoff_position_exceeds_or_differs_from_proven_quantity",
            "transport_attempted": False,
        }
    qty = proven_remaining_qty
    cid = str(claim.get("client_order_id") or "").strip()
    if not cid:
        return {"ok": False, "reason": "handoff_close_cid_missing"}
    frozen_request = metadata.get("close_request")
    if frozen_request is not None:
        request_ok, frozen_qty = _close_request_identity(
            frozen_request,
            claim=claim,
            close_side=close_side,
        )
        if (
            not request_ok
            or frozen_qty is None
            or abs(frozen_qty - qty) > max(1e-9, qty * 1e-8)
        ):
            return {"ok": False, "reason": "frozen_close_request_position_mismatch"}

    effective_claim = dict(claim)

    def _persist_request(request: dict[str, Any]) -> bool:
        nonlocal effective_claim, frozen_request
        existing = metadata.get("close_request")
        if existing is not None and existing != request:
            return False
        persisted = persist_orphan_close_request_committed(
            symbol=claim["symbol"],
            claim_token=claim["claim_token"],
            client_order_id=cid,
            close_request=request,
            metadata={
                "submitted_qty": qty,
                "close_side": close_side,
                "close_request_persisted_at_utc": datetime.now(timezone.utc).isoformat(),
            },
            account_scope=claim["account_scope"],
        )
        if persisted:
            metadata["close_request"] = dict(request)
            effective_claim["metadata"] = dict(metadata)
            frozen_request = dict(request)
        return bool(persisted)

    def _consume_post_permission(request: dict[str, Any]) -> bool:
        # Livehead-audit gap G2: the byte-equal freeze alone lets two competing
        # sweep workers both POST the same CID, delegating duplicate-close
        # safety to broker-side dedup.  Consume the single client-side POST
        # permission immediately before HTTP; a CAS loser performs zero POSTs
        # and recovers only through the same-CID reconciliation path.
        if frozen_request is None and not _persist_request(request):
            return False
        return consume_orphan_handoff_close_post_permission_committed(
            symbol=claim["symbol"],
            claim_token=claim["claim_token"],
            client_order_id=cid,
            close_request=request,
            account_scope=claim["account_scope"],
        )

    result = _place_alpaca_equity_close(
        adapter,
        symbol=claim["symbol"],
        close_side=close_side,
        quantity=qty,
        client_order_id=cid,
        before_submit=_consume_post_permission,
        frozen_request=(dict(frozen_request) if isinstance(frozen_request, dict) else None),
    )
    if result.get("pre_place_blocked"):
        return result
    oid = str(result.get("order_id") or "").strip() if isinstance(result, dict) else ""
    if bool(isinstance(result, dict) and result.get("ok")) and oid:
        bound = update_action_claim_phase_committed(
            symbol=claim["symbol"],
            claim_token=claim["claim_token"],
            phase=SUBMITTED,
            client_order_id=cid,
            broker_order_id=oid,
            metadata={
                "submit_status": result.get("status"),
                "submitted_qty": qty,
                "close_side": close_side,
            },
            account_scope=claim["account_scope"],
        )
        return {
            "ok": bool(bound),
            "submitted": bool(bound),
            "order_id": oid,
            "status": result.get("status"),
            "transport_attempted": bool(result.get("transport_attempted")),
        }

    # The POST response is not negative truth. Recover the same deterministic CID
    # before marking the boundary indeterminate; never generate a second close ID.
    recovered = _exact_claim_order(adapter, effective_claim)
    if recovered is not None:
        advanced = _advance_orphan_claim_from_order(
            adapter,
            claim,
            recovered,
            db=db,
        )
        return {
            "ok": bool(advanced),
            "recovered": bool(advanced),
            "order_id": str(getattr(recovered, "order_id", "") or ""),
            "status": str(getattr(recovered, "status", "") or ""),
        }
    update_action_claim_phase_committed(
        symbol=claim["symbol"],
        claim_token=claim["claim_token"],
        phase=SUBMIT_INDETERMINATE,
        client_order_id=cid,
        broker_order_id=None,
        metadata={
            "submit_error": str(
                (result or {}).get("error") if isinstance(result, dict) else "unknown"
            )[:160],
            "submitted_qty": qty,
            "close_side": close_side,
        },
        account_scope=claim["account_scope"],
    )
    return {
        "ok": False,
        "indeterminate": True,
        "reason": "close_submit_indeterminate",
        "transport_attempted": bool(result.get("transport_attempted")),
    }


def _sweep_detached_entry_claims(db: Session, adapter: Any) -> dict[str, int]:
    """Recover entry permits whose owner is terminal or missing."""
    readable, claims = list_unresolved_action_claims(db, action="entry")
    try:
        db.rollback()
    except Exception:
        return {"detached_entry_claims_unreadable": 1}
    if not readable:
        return {"detached_entry_claims_unreadable": 1}
    result = {
        "detached_entry_claims_active": 0,
        "detached_entry_claims_pending": 0,
        "detached_entry_claims_resolved": 0,
        "detached_entry_claims_handed_off": 0,
        "detached_entry_closes_submitted": 0,
    }
    broker_actions = 0
    for claim in claims:
        claim_quarantine = _alpaca_reconcile_shape_quarantine_reason(
            symbol=claim.get("symbol"),
            metadata=(claim.get("metadata") if isinstance(claim.get("metadata"), dict) else {}),
        )
        if claim_quarantine is not None:
            result["detached_entry_claims_quarantined"] = int(
                result.get("detached_entry_claims_quarantined") or 0
            ) + 1
            continue
        owner_state = _detached_entry_owner_state(db, claim)
        try:
            db.rollback()
        except Exception:
            owner_state = "unknown"
        if owner_state == "active":
            result["detached_entry_claims_active"] += 1
            continue
        if owner_state not in {"terminal", "missing"}:
            result["detached_entry_claims_pending"] += 1
            continue

        adaptive_mode, adaptive_reason = _adaptive_claim_preflight(db, claim)
        if adaptive_mode == "invalid":
            result["detached_entry_claims_quarantined"] = int(
                result.get("detached_entry_claims_quarantined") or 0
            ) + 1
            result["detached_adaptive_lifecycle_quarantined"] = int(
                result.get("detached_adaptive_lifecycle_quarantined") or 0
            ) + 1
            if adaptive_reason:
                result[f"detached_adaptive_{adaptive_reason}"] = int(
                    result.get(f"detached_adaptive_{adaptive_reason}") or 0
                ) + 1
            continue

        lookup_state, order = _strict_detached_entry_claim_order(adapter, claim)
        if lookup_state != "found" or order is None:
            result["detached_entry_claims_pending"] += 1
            continue

        unresolved_lineage = _alpaca_unresolved_order_lineage(order)
        if unresolved_lineage is not None:
            result["detached_entry_claims_pending"] += 1
            result[f"detached_{unresolved_lineage}"] = int(
                result.get(f"detached_{unresolved_lineage}") or 0
            ) + 1
            continue

        # Cancel an exact open remainder and re-read exact truth. A cancel ack is
        # never terminal proof and an unreadable reread remains blocking.
        if str(getattr(order, "status", "") or "").strip().lower() not in _ENTRY_TERMINAL_STATUSES:
            if broker_actions >= 8:
                result["detached_entry_claims_pending"] += 1
                continue
            try:
                adapter.cancel_order(str(getattr(order, "order_id", "") or ""))
            except Exception:
                pass
            broker_actions += 1
            lookup_state, order = _strict_detached_entry_claim_order(adapter, claim)
            if lookup_state != "found" or order is None:
                result["detached_entry_claims_pending"] += 1
                continue
            unresolved_lineage = _alpaca_unresolved_order_lineage(order)
            if unresolved_lineage is not None:
                result["detached_entry_claims_pending"] += 1
                result[f"detached_{unresolved_lineage}"] = int(
                    result.get(f"detached_{unresolved_lineage}") or 0
                ) + 1
                continue
        status = str(getattr(order, "status", "") or "").strip().lower()
        if status not in _ENTRY_TERMINAL_STATUSES:
            result["detached_entry_claims_pending"] += 1
            continue
        try:
            filled = max(0.0, float(getattr(order, "filled_size", 0.0) or 0.0))
        except (TypeError, ValueError):
            result["detached_entry_claims_pending"] += 1
            continue
        signed_qty = _signed_broker_position(adapter, claim["symbol"])
        if signed_qty is None:
            result["detached_entry_claims_pending"] += 1
            continue
        if abs(signed_qty) <= 1e-9:
            if adaptive_mode == "adaptive":
                adaptive_resolved = _resolve_detached_adaptive_entry_flat(
                    db,
                    adapter=adapter,
                    claim=claim,
                    order=order,
                    owner_state=owner_state,
                )
                resolved = bool(adaptive_resolved.get("ok"))
            else:
                resolved = resolve_action_claim_committed(
                    symbol=claim["symbol"],
                    claim_token=claim["claim_token"],
                    client_order_id=claim.get("client_order_id"),
                    broker_order_id=str(getattr(order, "order_id", "") or ""),
                    broker_order_status=status,
                    broker_position_zero=True,
                    zero_fill_terminal=bool(filled <= 1e-12),
                    terminal_owner_broker_flat=bool(filled > 1e-12),
                    expected_claim_updated_at=claim.get("updated_at"),
                    metadata={
                        "reason": "detached_terminal_entry_broker_flat",
                        "owner_state": owner_state,
                        "filled_size": filled,
                    },
                    account_scope=claim["account_scope"],
                )
            if resolved:
                result["detached_entry_claims_resolved"] += 1
            else:
                result["detached_entry_claims_pending"] += 1
            continue
        if filled <= 1e-12:
            # The exact entry created no exposure. A non-zero position has a
            # different provenance and must go through the normal orphan grace
            # policy; do not attribute/flatten it under this claim.
            result["detached_entry_claims_pending"] += 1
            continue
        authority_proof, authority_reason = _detached_entry_position_authority(
            adapter,
            claim=claim,
            order=order,
        )
        if authority_proof is None:
            result["detached_entry_claims_quarantined"] = int(
                result.get("detached_entry_claims_quarantined") or 0
            ) + 1
            result[f"detached_quarantine_{authority_reason}"] = int(
                result.get(f"detached_quarantine_{authority_reason}") or 0
            ) + 1
            continue
        signed_qty = float(authority_proof["broker_position_qty"])
        handed = _handoff_detached_entry_claim(
            db,
            claim=claim,
            order=order,
            broker_position_qty=signed_qty,
            authority_proof=authority_proof,
        )
        if not handed.get("ok"):
            result["detached_entry_claims_pending"] += 1
            continue
        result["detached_entry_claims_handed_off"] += 1
        if broker_actions >= 8:
            result["detached_entry_claims_pending"] += 1
            continue
        close_result = _submit_handoff_close(
            adapter,
            handed["claim"],
            db=db,
        )
        broker_actions += 1 if close_result.get("transport_attempted") else 0
        if close_result.get("submitted") or close_result.get("recovered"):
            result["detached_entry_closes_submitted"] += 1
        elif not close_result.get("resolved_flat"):
            result["detached_entry_claims_pending"] += 1
    return result


def _sweep_active_orphan_claims(db: Session, adapter: Any) -> dict[str, int]:
    """Recover permits even after a filled sell removes the broker position.

    A no-session orphan has no event anchor, and a filled sell disappears from the
    positions view.  The claim table therefore has its own exact-CID recovery sweep.
    """
    readable, claims = list_unresolved_action_claims(db, action="orphan_flatten")
    try:
        db.rollback()
    except Exception:
        return {"claim_recovery_unreadable": 1}
    if not readable:
        return {"claim_recovery_unreadable": 1}
    result = {
        "claims_recovered": 0,
        "claims_still_pending": 0,
        "claims_residual_rotated": 0,
    }
    broker_actions = 0
    for claim in claims:
        claim_metadata = claim.get("metadata")
        claim_metadata = claim_metadata if isinstance(claim_metadata, dict) else {}
        if claim_metadata.get("runner_emergency_close_only") is True:
            # This claim is capped by one session's attributable quantity/floor.
            # Generic residual rotation would mint a new CID and expand to the
            # aggregate broker position, potentially liquidating manual shares.
            result["runner_emergency_close_only_claims_skipped"] = int(
                result.get("runner_emergency_close_only_claims_skipped") or 0
            ) + 1
            continue
        adaptive_mode, adaptive_reason = _adaptive_claim_preflight(db, claim)
        if adaptive_mode == "invalid":
            result["claims_quarantined"] = int(
                result.get("claims_quarantined") or 0
            ) + 1
            result["adaptive_lifecycle_claims_quarantined"] = int(
                result.get("adaptive_lifecycle_claims_quarantined") or 0
            ) + 1
            if adaptive_reason:
                result[f"adaptive_quarantine_{adaptive_reason}"] = int(
                    result.get(f"adaptive_quarantine_{adaptive_reason}") or 0
                ) + 1
            continue
        authority_ok, _authority_reason = _strict_terminal_handoff_claim_authority(
            claim
        )
        if not authority_ok:
            # Old generic orphan-* claims were minted from symbol/position
            # inference and have no continuous ownership proof. Do not even look
            # up their CID: lookup/rotation/submit/cancel authority is absent.
            result["unsafe_unclaimed_orphan_claims_quarantined"] = int(
                result.get("unsafe_unclaimed_orphan_claims_quarantined") or 0
            ) + 1
            continue
        claim_quarantine = _alpaca_reconcile_shape_quarantine_reason(
            symbol=claim.get("symbol"),
            metadata=(claim.get("metadata") if isinstance(claim.get("metadata"), dict) else {}),
        )
        if claim_quarantine is not None:
            result["claims_quarantined"] = int(result.get("claims_quarantined") or 0) + 1
            continue
        order = _exact_claim_order(adapter, claim)
        if order is None:
            metadata = claim.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            eligible_handoff = bool(
                metadata.get("terminal_entry_handoff")
                and claim.get("phase") in {CLAIMED, SUBMIT_INDETERMINATE}
                and _claim_lease_expired(claim)
            )
            if eligible_handoff:
                submit_claim = claim
                if isinstance(metadata.get("close_request"), dict):
                    strict_state, strict_order = _strict_orphan_claim_order_state(
                        adapter, claim
                    )
                    if strict_state == "found" and strict_order is not None:
                        advanced = _advance_orphan_claim_from_order(
                            adapter,
                            claim,
                            strict_order,
                            db=db,
                        )
                        if advanced:
                            result["claims_recovered"] += 1
                        else:
                            result["claims_still_pending"] += 1
                        continue
                    if strict_state != "absent":
                        # Timeout/read failure/identity contradiction is not
                        # negative truth and authorizes zero broker POSTs.
                        result["claims_still_pending"] += 1
                        continue
                    signed_qty = _signed_broker_position(adapter, claim["symbol"])
                    if signed_qty is None:
                        result["claims_still_pending"] += 1
                        continue
                    if abs(signed_qty) > 1e-9:
                        rotated = _rotate_absent_orphan_claim(
                            db,
                            claim=claim,
                            broker_position_qty=signed_qty,
                        )
                        if not rotated.get("ok"):
                            result["claims_still_pending"] += 1
                            continue
                        result["claims_residual_rotated"] += 1
                        submit_claim = rotated["claim"]
                if broker_actions < 8:
                    submit = _submit_handoff_close(
                        adapter,
                        submit_claim,
                        db=db,
                    )
                    broker_actions += 1 if submit.get("transport_attempted") else 0
                    if submit.get("submitted") or submit.get("recovered"):
                        result["claims_recovered"] += 1
                        continue
                    if submit.get("resolved_flat"):
                        result["claims_recovered"] += 1
                        continue
            result["claims_still_pending"] += 1
            continue
        status = str(getattr(order, "status", "") or "").strip().lower()
        signed_qty = _signed_broker_position(adapter, claim["symbol"])
        metadata = dict(claim.get("metadata") or {})
        if signed_qty is None:
            result["claims_still_pending"] += 1
            continue
        advanced = _advance_orphan_claim_from_order(
            adapter,
            claim,
            order,
            db=db,
            signed_position_qty=signed_qty,
        )
        if not advanced:
            result["claims_still_pending"] += 1
            continue
        result["claims_recovered"] += 1
        if abs(signed_qty) <= 1e-9:
            if (
                adaptive_mode == "legacy"
                and status != "filled"
                and metadata.get("terminal_entry_handoff")
            ):
                resolved = resolve_action_claim_committed(
                    symbol=claim["symbol"],
                    claim_token=claim["claim_token"],
                    client_order_id=claim.get("client_order_id"),
                    broker_order_id=str(getattr(order, "order_id", "") or ""),
                    broker_order_status=status,
                    broker_position_zero=True,
                    orphan_handoff_broker_flat=True,
                    metadata={
                        "reason": "terminal_handoff_close_broker_flat",
                        "terminal_close_filled_size": float(
                            getattr(order, "filled_size", 0.0) or 0.0
                        ),
                    },
                    account_scope=claim["account_scope"],
                )
                if not resolved:
                    result["claims_still_pending"] += 1
        elif status in _ENTRY_TERMINAL_STATUSES:
            if broker_actions >= 8:
                result["claims_still_pending"] += 1
            else:
                rotated = _rotate_orphan_claim_for_residual(
                    db,
                    claim=claim,
                    order=order,
                    broker_position_qty=signed_qty,
                )
                if not rotated.get("ok"):
                    result["claims_still_pending"] += 1
                else:
                    result["claims_residual_rotated"] += 1
                    retry = _submit_handoff_close(
                        adapter,
                        rotated["claim"],
                        db=db,
                    )
                    broker_actions += 1 if retry.get("transport_attempted") else 0
                    if not (
                        retry.get("submitted")
                        or retry.get("recovered")
                        or retry.get("resolved_flat")
                    ):
                        result["claims_still_pending"] += 1
        else:
            result["claims_still_pending"] += 1
        _audit(db, claim["symbol"], {
            "action": "flatten_orphan_position",
            "ok": True,
            **metadata,
            "claim_token": claim["claim_token"],
            "account_scope": claim["account_scope"],
            "order_id": str(getattr(order, "order_id", "") or ""),
            "client_order_id": claim.get("client_order_id"),
            "order_status": str(getattr(order, "status", "") or ""),
            "recovered_by_claim_sweep": True,
            "error": None,
        }, session_id=metadata.get("session_id"))
        try:
            db.commit()
        except Exception:
            db.rollback()
    return result


def _pending_orphan_flatten_events(
    db: Session,
    *,
    limit: int = 50,
    source_event_id: int | None = None,
) -> list[dict[str, Any]]:
    """Submitted orphan flattens that do not yet have a durable settlement marker.

    The initial sweep can only prove that Alpaca accepted the sell.  A later sweep
    must read the terminal broker order before it changes any accounting.  The
    source-event marker makes that second pass durable and idempotent across worker
    restarts.
    """
    try:
        with db.begin_nested():
            rows = db.execute(text(
                "SELECT e.id, e.session_id, e.payload_json "
                "FROM trading_automation_events e "
                "WHERE e.event_type = 'alpaca_orphan_reconcile' "
                "  AND e.ts > (now() at time zone 'utc') - (:days * interval '1 day') "
                "  AND COALESCE(e.payload_json->>'action', '') = 'flatten_orphan_position' "
                "  AND COALESCE(e.payload_json->>'ok', 'false') = 'true' "
                "  AND NULLIF(e.payload_json->>'order_id', '') IS NOT NULL "
                "  AND (:source_event_id IS NULL OR e.id = :source_event_id) "
                "  AND NOT EXISTS ("
                "      SELECT 1 FROM trading_automation_events s "
                "      WHERE s.session_id = e.session_id "
                "        AND s.event_type = 'alpaca_orphan_reconcile' "
                "        AND COALESCE(s.payload_json->>'action', '') = 'settle_orphan_position' "
                "        AND COALESCE(s.payload_json->>'source_event_id', '') = e.id::text"
                "  ) "
                "ORDER BY e.ts ASC LIMIT :limit"
            ), {
                "days": _ORPHAN_SETTLE_LOOKBACK_DAYS,
                "limit": max(1, min(int(limit), 200)),
                "source_event_id": (
                    int(source_event_id) if source_event_id is not None else None
                ),
            }).fetchall()
        pending: list[dict[str, Any]] = []
        for event_id, session_id, payload in rows:
            if not isinstance(payload, dict):
                continue
            pending.append({
                "event_id": int(event_id),
                "session_id": int(session_id),
                "payload": dict(payload),
            })
        return pending
    except Exception:
        logger.warning("[alpaca_reconcile] pending-flatten read failed; settlement deferred", exc_info=True)
        return []


def _load_session_outcome_for_update(
    db: Session,
    session_id: int,
) -> tuple[TradingAutomationSession | None, MomentumAutomationOutcome | None]:
    sess = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.id == int(session_id))
        .with_for_update()
        .one_or_none()
    )
    if sess is None:
        return None, None
    outcome = (
        db.query(MomentumAutomationOutcome)
        .filter(MomentumAutomationOutcome.session_id == int(session_id))
        .with_for_update()
        .one_or_none()
    )
    return sess, outcome


def _orphan_settlement_marker_exists(
    db: Session,
    *,
    session_id: int,
    source_event_id: int,
) -> bool | None:
    """Recheck the exact durable marker after the session/outcome row locks.

    ``_pending_orphan_flatten_events`` is intentionally an unlocked discovery
    query. Two scheduler workers can therefore discover the same source event.
    The session row lock serializes their mutation; this second read makes the
    later worker observe the first worker's committed marker before it rewrites
    accounting or appends a duplicate settlement event. ``None`` is fail-closed.
    """
    try:
        row = db.execute(text(
            "SELECT 1 FROM trading_automation_events "
            "WHERE session_id = :sid "
            "  AND event_type = 'alpaca_orphan_reconcile' "
            "  AND COALESCE(payload_json->>'action', '') = 'settle_orphan_position' "
            "  AND COALESCE(payload_json->>'source_event_id', '') = :source_event_id "
            "LIMIT 1"
        ), {
            "sid": int(session_id),
            "source_event_id": str(int(source_event_id)),
        }).fetchone()
        return row is not None
    except Exception:
        logger.warning(
            "[alpaca_reconcile] settlement-marker recheck failed session=%s source_event=%s",
            session_id,
            source_event_id,
            exc_info=True,
        )
        return None


def _positive_finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0.0 and out == out and out not in (float("inf"), float("-inf")) else None


def _parse_broker_timestamp(value: Any) -> datetime | None:
    """Parse an explicit aware broker instant to naive UTC without guessing."""
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError):
            return None
    return None


def _order_fill_time(order: Any) -> datetime | None:
    """Return Alpaca's exact aware fill instant as naive UTC, or ``None``.

    Accounting repair must never substitute repair-time for missing broker
    history. Naive timestamps are also rejected because their timezone is
    ambiguous.
    """
    raw = getattr(order, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    return _parse_broker_timestamp(raw.get("filled_at") or raw.get("fill_time"))


def _utc_iso(value: datetime) -> str:
    """Canonical UTC text that preserves broker microseconds."""
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return aware.isoformat().replace("+00:00", "Z")


def _apply_cancelled_pre_entry_orphan_truth(
    sess: TradingAutomationSession,
    outcome: MomentumAutomationOutcome,
    *,
    source_event_id: int,
    entry_order_id: str,
    entry_client_order_id: str,
    entry_filled_at: datetime,
    order_id: str,
    exit_client_order_id: str | None,
    quantity: float,
    entry_price: float,
    exit_price: float,
    filled_at: datetime,
) -> dict[str, Any]:
    """Repair only the narrow ACTU failure class, without inventing missing facts.

    Preconditions are deliberately strict: a LIVE Alpaca session, a still-false
    ``cancelled_pre_entry`` outcome, a broker-position average entry captured before
    flatten, and a fully-filled broker sell.  Other outcome classes are left alone.
    """
    if str(getattr(sess, "mode", "") or "").lower() != "live":
        return {"ok": False, "reason": "session_not_live"}
    if str(getattr(sess, "execution_family", "") or "") not in _ALPACA_FAMILIES:
        return {"ok": False, "reason": "session_not_alpaca"}
    if str(getattr(outcome, "outcome_class", "") or "") != OUTCOME_CANCELLED_PRE_ENTRY:
        return {"ok": False, "reason": "outcome_not_cancelled_pre_entry"}
    summary = dict(getattr(outcome, "extracted_summary_json", None) or {})
    if bool(summary.get("entry_occurred")):
        return {"ok": False, "reason": "outcome_already_entered"}
    if str(getattr(outcome, "broker_recon_status", "") or "") == "reconciled":
        return {"ok": False, "reason": "outcome_already_broker_reconciled"}

    snap = dict(getattr(sess, "risk_snapshot_json", None) or {})
    le = snap.get("momentum_live_execution")
    le = dict(le) if isinstance(le, dict) else {}
    prior_lane_pnl = le.get("realized_pnl_usd")
    prior_outcome_pnl = getattr(outcome, "realized_pnl_usd", None)
    try:
        if prior_lane_pnl is not None and abs(float(prior_lane_pnl)) > 1e-9:
            return {"ok": False, "reason": "lane_pnl_already_nonzero"}
        if prior_outcome_pnl is not None and abs(float(prior_outcome_pnl)) > 1e-9:
            return {"ok": False, "reason": "outcome_pnl_already_nonzero"}
    except (TypeError, ValueError):
        return {"ok": False, "reason": "existing_pnl_unreadable"}

    notional = abs(float(entry_price) * float(quantity))
    pnl = (float(exit_price) - float(entry_price)) * float(quantity)
    return_bps = (pnl / notional) * 10_000.0
    hold_seconds = max(0, int((filled_at - entry_filled_at).total_seconds()))
    truth = {
        "source": "alpaca_entry_order_plus_broker_exit_fill",
        "source_event_id": int(source_event_id),
        "entry_order_id": str(entry_order_id),
        "entry_client_order_id": str(entry_client_order_id),
        "entry_filled_at_utc": _utc_iso(entry_filled_at),
        "exit_order_id": str(order_id),
        "exit_client_order_id": str(exit_client_order_id or "") or None,
        "quantity": float(quantity),
        "entry_price": float(entry_price),
        "exit_price": float(exit_price),
        "notional_basis_usd": notional,
        "realized_pnl_usd": pnl,
        "fees_status": "unconfirmed",
        "filled_at_utc": _utc_iso(filled_at),
        "hold_seconds": hold_seconds,
    }

    le["realized_pnl_usd"] = pnl
    le["last_exit_price"] = float(exit_price)
    le["last_exit_entry_price"] = float(entry_price)
    le["last_exit_quantity"] = float(quantity)
    le["last_exit_notional_basis_usd"] = notional
    le["last_exit_return_bps"] = return_bps
    le["last_exit_reason"] = _ORPHAN_EXIT_REASON
    le["entry_order_id"] = str(entry_order_id)
    le["entry_client_order_id"] = str(entry_client_order_id)
    le["entry_filled_at_utc"] = _utc_iso(entry_filled_at)
    le["position"] = None
    le["orphan_reconcile_truth"] = truth
    snap["momentum_live_execution"] = le
    sess.risk_snapshot_json = snap
    if getattr(sess, "ended_at", None) is None or sess.ended_at < filled_at:
        sess.ended_at = filled_at

    old_legacy_pnl = prior_outcome_pnl
    outcome.terminal_at = filled_at
    outcome.hold_seconds = hold_seconds
    outcome.outcome_class = OUTCOME_GOVERNANCE_EXIT
    outcome.realized_pnl_usd = pnl
    outcome.return_bps = return_bps
    outcome.exit_reason = _ORPHAN_EXIT_REASON
    outcome.contributes_to_evolution = False

    summary["entry_occurred"] = True
    summary["notional_basis_usd"] = notional
    summary["orphan_reconcile_truth"] = truth
    credit = dict(summary.get("evolution_credit") or {})
    reasons = [
        str(r) for r in (credit.get("reason_codes") or [])
        if str(r) not in {
            "no_entry",
            "missing_economic_result",
            f"non_strategy_outcome_{OUTCOME_CANCELLED_PRE_ENTRY}",
        }
    ]
    governance_reason = f"non_strategy_outcome_{OUTCOME_GOVERNANCE_EXIT}"
    if governance_reason not in reasons:
        reasons.append(governance_reason)
    credit.update({
        "contributes_to_evolution": False,
        "reason_codes": reasons,
        "outcome_class": OUTCOME_GOVERNANCE_EXIT,
    })
    summary["evolution_credit"] = credit
    outcome.extracted_summary_json = summary

    # Entry and exit order identities/times are broker-verified, but fees remain
    # unconfirmed. Keep this accurate-but-excluded until fee truth is settled.
    outcome.broker_recon_status = "fee_unconfirmed"
    outcome.broker_realized_pnl_usd = pnl
    outcome.broker_return_bps = return_bps
    outcome.broker_notional_basis_usd = notional
    outcome.broker_win = None
    outcome.broker_divergence_usd = (
        None
        if prior_outcome_pnl is None
        else pnl - float(prior_outcome_pnl)
    )
    outcome.broker_reconciled_at = datetime.now(timezone.utc).replace(tzinfo=None)
    outcome.broker_recon_detail_json = {
        **truth,
        "status": "fee_unconfirmed",
        "legacy_pnl_before_repair": old_legacy_pnl,
    }
    return {"ok": True, "pnl_usd": pnl, "return_bps": return_bps, "truth": truth}


def _add_orphan_settlement_event(
    db: Session,
    *,
    session_id: int,
    source_event_id: int,
    order_id: str,
    accounting_repaired: bool,
    detail: dict[str, Any],
    sess: TradingAutomationSession | None = None,
) -> None:
    db.add(TradingAutomationEvent(
        session_id=int(session_id),
        event_type="alpaca_orphan_reconcile",
        payload_json={
            "action": "settle_orphan_position",
            "source_event_id": int(source_event_id),
            "order_id": str(order_id),
            "accounting_repaired": bool(accounting_repaired),
            **detail,
        },
        correlation_id=getattr(sess, "correlation_id", None) if sess is not None else None,
        source_node_id=getattr(sess, "source_node_id", None) if sess is not None else None,
    ))


def _read_exact_order_truth(adapter: Any, order_id: str) -> Any | None:
    """Prefer explicit readable/found truth and reject any OID contradiction."""
    oid = str(order_id or "").strip()
    if not oid:
        return None
    if hasattr(adapter, "get_order_truth"):
        try:
            truth = adapter.get_order_truth(oid)
        except Exception:
            return None
        order = truth.get("order") if isinstance(truth, dict) else None
        if not (
            isinstance(truth, dict)
            and truth.get("readable") is True
            and truth.get("found") is True
            and order is not None
        ):
            return None
    else:
        try:
            order, _ = adapter.get_order(oid)
        except Exception:
            return None
    if str(getattr(order, "order_id", "") or "").strip() != oid:
        return None
    return order


def _settle_submitted_orphan_flattens(
    db: Session,
    adapter: Any,
    *,
    source_event_id: int | None = None,
) -> dict[str, int]:
    """Poll certified orphan-flatten orders and repair outcomes exactly once."""
    result = {"orphan_fills_settled": 0, "outcomes_repaired": 0, "settlement_pending": 0}
    pending_events = (
        _pending_orphan_flatten_events(db)
        if source_event_id is None
        else _pending_orphan_flatten_events(db, source_event_id=int(source_event_id))
    )
    requested_source_event_id = int(source_event_id) if source_event_id is not None else None
    for pending in pending_events:
        event_id = int(pending["event_id"])
        # Defense in depth: even a faulty/mocked pending-event reader cannot
        # broaden an explicitly scoped settlement.
        if requested_source_event_id is not None and event_id != requested_source_event_id:
            continue
        session_id = int(pending["session_id"])
        payload = pending["payload"]
        order_id = str(payload.get("order_id") or "")
        quarantine_reason = _settlement_source_quarantine_reason(
            db,
            session_id=session_id,
            payload=payload,
        )
        try:
            db.rollback()
        except Exception:
            pass
        if quarantine_reason is not None:
            result["settlement_pending"] += 1
            result["settlement_quarantined"] = int(
                result.get("settlement_quarantined") or 0
            ) + 1
            continue
        order = _read_exact_order_truth(adapter, order_id)
        if order is None:
            result["settlement_pending"] += 1
            continue
        status = str(getattr(order, "status", "") or "").lower()
        if status in {"open", "pending", "unknown", ""}:
            result["settlement_pending"] += 1
            continue
        broker_filled_at = _order_fill_time(order)

        reason: str | None = None
        repair_eligible = payload.get("repair_eligible") is True
        entry_order_id = str(payload.get("entry_order_id") or "")
        entry_client_order_id = str(payload.get("entry_client_order_id") or "")
        entry_filled_at = _parse_broker_timestamp(payload.get("entry_filled_at_utc"))
        expected_exit_client_id = str(payload.get("client_order_id") or "").strip()
        entry_price = _positive_finite(payload.get("entry_price"))
        expected_qty = _positive_finite(payload.get("qty"))
        filled_qty = _positive_finite(getattr(order, "filled_size", None))
        exit_price = _positive_finite(getattr(order, "average_filled_price", None))
        actual_order_id = str(getattr(order, "order_id", "") or "").strip()
        actual_exit_client_id = str(
            getattr(order, "client_order_id", "") or ""
        ).strip()
        expected_symbol = str(payload.get("symbol") or "").strip().upper()
        actual_symbol = str(getattr(order, "product_id", "") or "").strip().upper()
        if reason is not None:
            pass
        elif actual_order_id != order_id:
            reason = "flatten_order_id_mismatch"
        elif expected_exit_client_id and actual_exit_client_id != expected_exit_client_id:
            reason = "flatten_client_order_id_mismatch"
        elif status != "filled":
            reason = f"flatten_order_terminal_{status}"
        elif not repair_eligible or not entry_order_id or not entry_client_order_id:
            reason = "broker_verified_entry_anchor_missing"
        elif entry_filled_at is None:
            reason = "entry_fill_time_missing"
        elif str(getattr(order, "side", "") or "").lower() != "sell":
            reason = "flatten_order_not_sell"
        elif expected_symbol and actual_symbol != expected_symbol:
            reason = "flatten_symbol_mismatch"
        elif entry_price is None:
            reason = "captured_entry_price_missing"
        elif expected_qty is None or filled_qty is None:
            reason = "filled_qty_missing"
        elif abs(filled_qty - expected_qty) > max(1e-6, expected_qty * 1e-6):
            reason = "filled_qty_mismatch"
        elif exit_price is None:
            reason = "exit_fill_price_missing"

        if reason in {
            "flatten_order_id_mismatch",
            "flatten_client_order_id_mismatch",
        }:
            # A broker lookup returning a different identity is not evidence about
            # this source event. Retry without a durable marker or any mutation.
            result["settlement_pending"] += 1
            continue

        # A filled exit without an explicit broker fill time may become readable
        # on a later poll. Do not lock rows, mutate accounting, or mint a durable
        # settlement marker until that chronology is authoritative.
        if reason is None and broker_filled_at is None:
            result["settlement_pending"] += 1
            continue
        if (
            reason is None
            and entry_filled_at is not None
            and broker_filled_at is not None
            and entry_filled_at > broker_filled_at
        ):
            # Impossible lifecycle chronology: leave the source event unmarked so
            # a corrected broker read can be retried, but invent no outcome history.
            result["settlement_pending"] += 1
            continue

        try:
            with db.begin_nested():
                sess, outcome = _load_session_outcome_for_update(db, session_id)
                if sess is None:
                    # The source event has an FK to the session, so this should only
                    # happen during a concurrent delete.  Retry instead of minting a
                    # marker that would permanently suppress repair.
                    result["settlement_pending"] += 1
                    continue
                if outcome is None:
                    # Feedback emission can lag the broker fill.  Leave unmarked so
                    # the next sweep retries after the one-per-session row appears.
                    result["settlement_pending"] += 1
                    continue
                marker_exists = _orphan_settlement_marker_exists(
                    db,
                    session_id=session_id,
                    source_event_id=event_id,
                )
                if marker_exists is None:
                    result["settlement_pending"] += 1
                    continue
                if marker_exists:
                    # Another worker discovered the same unlocked source event,
                    # won the session/outcome lock, and committed first.
                    continue
                if reason is not None:
                    _add_orphan_settlement_event(
                        db,
                        session_id=session_id,
                        source_event_id=event_id,
                        order_id=order_id,
                        accounting_repaired=False,
                        detail={
                            "reason": reason,
                            "order_status": status,
                            "filled_at_utc": (
                                _utc_iso(broker_filled_at)
                                if broker_filled_at is not None
                                else None
                            ),
                        },
                        sess=sess,
                    )
                    if status == "filled":
                        result["orphan_fills_settled"] += 1
                    continue

                repair = _apply_cancelled_pre_entry_orphan_truth(
                    sess,
                    outcome,
                    source_event_id=event_id,
                    entry_order_id=entry_order_id,
                    entry_client_order_id=entry_client_order_id,
                    entry_filled_at=entry_filled_at,
                    order_id=order_id,
                    exit_client_order_id=(actual_exit_client_id or expected_exit_client_id),
                    quantity=float(filled_qty),
                    entry_price=float(entry_price),
                    exit_price=float(exit_price),
                    filled_at=broker_filled_at,
                )
                _add_orphan_settlement_event(
                    db,
                    session_id=session_id,
                    source_event_id=event_id,
                    order_id=order_id,
                    accounting_repaired=bool(repair.get("ok")),
                    detail={
                        "reason": repair.get("reason"),
                        "order_status": status,
                        "filled_qty": filled_qty,
                        "fill_price": exit_price,
                        "filled_at_utc": _utc_iso(broker_filled_at),
                        "entry_filled_at_utc": _utc_iso(entry_filled_at),
                        "exit_client_order_id": (
                            actual_exit_client_id or expected_exit_client_id or None
                        ),
                        "realized_pnl_usd": repair.get("pnl_usd"),
                    },
                    sess=sess,
                )
                if repair.get("ok"):
                    db.add(TradingAutomationEvent(
                        session_id=session_id,
                        ts=broker_filled_at,
                        event_type="live_exit_filled",
                        payload_json={
                            "reason": _ORPHAN_EXIT_REASON,
                            "pnl_usd": repair.get("pnl_usd"),
                            "fill_price": exit_price,
                            "quantity": filled_qty,
                            "order_id": order_id,
                            "client_order_id": (
                                actual_exit_client_id or expected_exit_client_id or None
                            ),
                            "source_event_id": event_id,
                            "entry_filled_at_utc": _utc_iso(entry_filled_at),
                            "filled_at_utc": _utc_iso(broker_filled_at),
                        },
                        correlation_id=getattr(sess, "correlation_id", None),
                        source_node_id=getattr(sess, "source_node_id", None),
                    ))
                    result["outcomes_repaired"] += 1
                result["orphan_fills_settled"] += 1
        except Exception:
            logger.warning(
                "[alpaca_reconcile] orphan-fill settlement failed session=%s order=%s",
                session_id,
                order_id,
                exc_info=True,
            )
            result["settlement_pending"] += 1
    return result


def _verify_exact_paper_account_for_reconcile(
    adapter: Any,
) -> tuple[bool, dict[str, Any]]:
    """Bind one reconciler instance to the configured paper-account generation.

    The exact-claim sweep is allowed to reduce exposure, but it still must not
    mutate an account selected only by credentials.  Freeze the adapter to the
    configured UUID and require a fresh broker account snapshot whose native
    operational flags are explicitly readable and false.  No account number or
    credential material is returned in the audit evidence.
    """

    expected_account_id = str(
        getattr(settings, "chili_alpaca_expected_account_id", "") or ""
    ).strip()
    if not expected_account_id:
        return False, {
            "reason": "alpaca_expected_account_id_unconfigured",
            "account_snapshot_read": False,
        }
    bind_account_id = getattr(adapter, "bind_account_id", None)
    get_account_snapshot = getattr(adapter, "get_account_snapshot", None)
    if not callable(bind_account_id) or not callable(get_account_snapshot):
        return False, {
            "reason": "alpaca_reconcile_account_capability_missing",
            "account_snapshot_read": False,
        }
    try:
        if bind_account_id(expected_account_id) is not True:
            return False, {
                "reason": "alpaca_reconcile_account_bind_failed",
                "account_snapshot_read": False,
            }
        snapshot = get_account_snapshot()
    except Exception as exc:
        return False, {
            "reason": "alpaca_reconcile_account_snapshot_unreadable",
            "error_type": type(exc).__name__,
            "account_snapshot_read": False,
        }
    if not isinstance(snapshot, dict) or snapshot.get("ok") is not True:
        return False, {
            "reason": "alpaca_reconcile_account_snapshot_unreadable",
            "account_snapshot_read": False,
        }
    observed_account_id = str(snapshot.get("account_id") or "").strip()
    if observed_account_id != expected_account_id:
        return False, {
            "reason": "alpaca_reconcile_account_generation_mismatch",
            "account_snapshot_read": True,
        }
    if snapshot.get("paper") is not True:
        return False, {
            "reason": "alpaca_reconcile_non_paper_account_blocked",
            "account_snapshot_read": True,
        }
    status = str(snapshot.get("status") or "").strip().lower()
    if status != "active":
        return False, {
            "reason": "alpaca_reconcile_account_status_blocked",
            "status": status or None,
            "account_snapshot_read": True,
        }
    blocking_flags = (
        "account_blocked",
        "trading_blocked",
        "trade_suspended_by_user",
    )
    if any(snapshot.get(field) is not False for field in blocking_flags):
        return False, {
            "reason": "alpaca_reconcile_account_operational_flags_blocked",
            "account_blocked": snapshot.get("account_blocked"),
            "trading_blocked": snapshot.get("trading_blocked"),
            "trade_suspended_by_user": snapshot.get("trade_suspended_by_user"),
            "account_snapshot_read": True,
        }
    return True, {
        "reason": "exact_paper_account_verified",
        "account_identity_sha256": alpaca_paper_account_identity_sha256(
            observed_account_id
        ),
        "paper": True,
        "status": status,
        "account_snapshot_read": True,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def run_alpaca_orphan_reconcile(db: Session) -> dict[str, Any]:
    """One reconcile pass. Returns a summary dict (logged by the scheduler job)."""
    out: dict[str, Any] = {
        "flattened": 0,
        "cancelled": 0,
        "skipped_active": 0,
        "skipped_recent": 0,
        "skipped_recheck_unreadable": 0,
    }
    if not bool(getattr(settings, "chili_momentum_alpaca_orphan_reconcile_enabled", True)):
        out["skipped"] = "flag_off"
        return out
    if not (
        bool(getattr(settings, "chili_momentum_live_runner_enabled", False))
        or bool(
            getattr(
                settings,
                "chili_momentum_alpaca_orphan_reconcile_standalone_enabled",
                False,
            )
        )
    ):
        out["skipped"] = "live_runner_disabled_without_standalone_authority"
        return out
    # PAPER-ONLY hard gate: never reconcile-flatten a real-money account from here.
    if not (
        bool(getattr(settings, "chili_alpaca_enabled", False))
        and bool(getattr(settings, "chili_alpaca_paper", True))
        and str(getattr(settings, "chili_alpaca_api_key", "") or "")
    ):
        out["skipped"] = "alpaca_not_paper_ready"
        return out

    persisted_quarantine = _persisted_reconcile_quarantine_reason(db)
    try:
        db.rollback()
    except Exception:
        pass
    if isinstance(persisted_quarantine, dict):
        out["persisted_execution_quarantines"] = dict(persisted_quarantine)
        out["skipped"] = "alpaca_execution_quarantined"
        out["broker_calls"] = 0
        return out
    elif persisted_quarantine is not None:
        out["skipped"] = "alpaca_execution_quarantined"
        out["quarantine_reason"] = str(persisted_quarantine)
        out["broker_calls"] = 0
        return out

    try:
        from ..venue.alpaca_spot import AlpacaSpotAdapter

        adapter = AlpacaSpotAdapter()
        if not adapter.is_enabled():
            out["skipped"] = "adapter_disabled"
            return out
    except Exception:
        out["skipped"] = "adapter_import_failed"
        return out

    account_ok, account_evidence = _verify_exact_paper_account_for_reconcile(adapter)
    out["account_verification"] = dict(account_evidence)
    if not account_ok:
        out["skipped"] = account_evidence.get("reason") or (
            "alpaca_reconcile_account_verification_failed"
        )
        return out
    try:
        # End persistence reads before any exact broker-order/position lookup.
        db.rollback()
    except Exception:
        out["skipped"] = "reconcile_pre_broker_rollback_failed"
        return out

    # A successful submit is not a fill.  Poll prior passes' broker orders and
    # repair only broker-confirmed, full-fill ACTU-class outcomes before looking
    # for new orphans.  This is accounting-only; it never submits an extra order.
    out.update(_settle_submitted_orphan_flattens(db, adapter))
    # Settlement locks the exact session/outcome rows while rewriting broker
    # truth. End that transaction before any subsequent Alpaca HTTP reads or
    # place/cancel calls; otherwise a slow broker can hold the live runner and
    # feedback writers behind FOR UPDATE locks for the whole reconcile pass.
    try:
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        out["skipped"] = "settlement_commit_failed"
        return out

    # Entry ownership can outlive its session when the submit HTTP committed but
    # the outer session transaction crashed. Recover that exact order first; a
    # terminal/missing owner with real exposure is atomically handed to the same
    # symbol permit's close authority before any new orphan decision.
    out.update(_sweep_detached_entry_claims(db, adapter))
    out.update(_sweep_active_orphan_claims(db, adapter))

    # Certification scope is deliberately exact-claim-only. No broad account
    # inventory shape may mint close/cancel authority in this reconciler.
    out["reconcile_scope"] = "exact_claims_only"
    out["generic_inventory_mutation_enabled"] = False
    return out
