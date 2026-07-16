"""Durable two-sided Alpaca account/symbol action ownership.

Entry/add workers and the orphan reconciler run in separate transactions and can
both cross the broker boundary.  A single row keyed by (account scope, symbol) is
the permit.  It is committed before HTTP and contains the deterministic client id,
so a crash/timeout can only be recovered under that exact identity.
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from ....config import settings
from .adaptive_risk_policy import (
    AdaptiveRiskContractError,
    resolve_adaptive_risk,
)
from .adaptive_risk_reservation import (
    AdaptiveRiskReservationStore,
    load_adaptive_risk_reservation_request,
)
from .adaptive_risk_runtime_contract import (
    AdaptiveRiskLedgerSnapshot,
    load_and_verify_adaptive_risk_reservation_claim,
    verify_adaptive_risk_claim_against_atomic_ledger,
)

_log = logging.getLogger(__name__)

ALPACA_EXECUTION_FAMILIES = frozenset({"alpaca_spot", "alpaca_short"})
CLAIMED = "claimed"
SUBMIT_INDETERMINATE = "submit_indeterminate"
SUBMITTED = "submitted"
RESOLVED = "resolved"
_PRE_HTTP_LEASE_SECONDS = 15 * 60
_TERMINAL_ORDER_STATUSES = frozenset({
    "filled", "done", "closed", "canceled", "cancelled", "expired", "rejected", "failed"
})
_OWNER_TRANSPORT_METADATA_KEY = "owner_transport"
_OWNER_TRANSPORT_HISTORY_KEY = "owner_transport_history"
_PROTECTIVE_TERMINAL_LEDGER_KEY = "protective_terminal_ledger"
_PROTECTIVE_ATTRIBUTION_QUARANTINE_LEDGER_KEY = (
    "protective_attribution_quarantine_ledger"
)
_REPLACEMENT_LINEAGE_CONTAINMENT_KEY = "replacement_lineage_containment"
_DEADMAN_GENERATION_HIGH_WATERMARK_KEY = "deadman_generation_high_watermark"
_DEADMAN_CLOSE_HANDOFF_METADATA_KEY = "deadman_close_handoff"
_DEADMAN_CLOSE_HANDOFF_HISTORY_KEY = "deadman_close_handoff_history"
_OWNER_TRANSPORT_LEASE_SECONDS = 30
_ORPHAN_CLOSE_TRANSPORT_HISTORY_KEY = "close_transport_history"
_ORPHAN_CLOSE_TRANSPORT_LEASE_SECONDS = 30
_ACTIVE_ALPACA_PROTECTIVE_LIFECYCLES = frozenset({
    "new",
    "partially_filled",
    "accepted",
    "pending_new",
    "accepted_for_bidding",
    "stopped",
})


def _terminal_order_status_lifecycle_compatible(
    status: str,
    lifecycle: str,
) -> bool:
    """Require normalized and raw broker truth to describe one terminal state."""
    if (
        status not in _TERMINAL_ORDER_STATUSES
        or lifecycle not in _TERMINAL_ORDER_STATUSES
    ):
        return False
    if status in {"canceled", "cancelled"}:
        return lifecycle in {"canceled", "cancelled"}
    return lifecycle == status


def _format_base_size(value: float) -> str:
    """Match the runner's literal broker-request quantity serialization."""
    rendered = f"{float(value):.12f}".rstrip("0").rstrip(".")
    return rendered or "0"


def _deadman_client_order_generation(
    client_order_id: Any,
    *,
    owner_session_id: int,
) -> int | None:
    prefix = f"chili_dm_{int(owner_session_id)}_"
    cid = str(client_order_id or "").strip()
    if not cid.startswith(prefix):
        return None
    raw = cid[len(prefix):].split("_", 1)[0]
    try:
        generation = int(raw)
    except (TypeError, ValueError):
        return None
    return generation if generation > 0 else None


def _durable_deadman_generation_high_watermark(
    metadata: dict[str, Any],
    *,
    owner_session_id: int,
) -> int:
    try:
        high = max(
            0,
            int(metadata.get(_DEADMAN_GENERATION_HIGH_WATERMARK_KEY) or 0),
        )
    except (TypeError, ValueError):
        high = 0
    rows: list[Any] = [metadata.get(_OWNER_TRANSPORT_METADATA_KEY)]
    for key in (
        _OWNER_TRANSPORT_HISTORY_KEY,
        _PROTECTIVE_TERMINAL_LEDGER_KEY,
    ):
        values = metadata.get(key)
        if isinstance(values, list):
            rows.extend(values)
    handoff = metadata.get(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY)
    if isinstance(handoff, dict):
        rows.extend([
            {"client_order_id": handoff.get("deadman_client_order_id")},
            {
                "client_order_id": handoff.get(
                    "replacement_deadman_client_order_id"
                )
            },
        ])
        lineage = handoff.get("protective_terminal_generations")
        if isinstance(lineage, list):
            rows.extend(lineage)
    for row in rows:
        if not isinstance(row, dict):
            continue
        generation = _deadman_client_order_generation(
            row.get("client_order_id"),
            owner_session_id=owner_session_id,
        )
        if generation is not None:
            high = max(high, generation)
    return high


def alpaca_symbol_is_crypto_like(value: Any) -> bool:
    """True for every crypto spelling that must not cross the equity-only seam."""
    symbol = str(value or "").strip().upper()
    return "/" in symbol or symbol.endswith("-USD")


def alpaca_asset_class_is_crypto(value: Any) -> bool:
    """Fail closed when a caller explicitly labels an instruction as crypto."""
    raw = getattr(value, "value", value)
    return "crypto" in str(raw or "").strip().lower()


def alpaca_account_scope() -> str:
    """Compatibility default; certified execution freezes ``alpaca:paper`` per row.

    Live posture is not execution-capable in this deployment. Broker-bound callers
    must pass the session/claim's durable paper scope explicitly; this ambient value
    exists only for legacy read compatibility and never certifies a live endpoint.
    """
    paper = bool(getattr(settings, "chili_alpaca_paper", True))
    environment = "paper" if paper else "live"
    return f"alpaca:{environment}"


def alpaca_account_risk_lock_key(account_scope: str | None = None) -> int:
    """Stable signed BIGINT advisory key for one Alpaca account posture."""
    scope = str(account_scope or alpaca_account_scope()).strip().lower()
    digest = hashlib.sha256(f"chili|alpaca|account-risk|{scope}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


_ENTRY_IDENTITY_METADATA_KEYS = frozenset({
    "order_role",
    "order_request",
    "reserved_risk_usd",
    "alpaca_account_id",
    # One unguessable worker generation owns the only pre-HTTP transition.  Once
    # a CID is bound this value is immutable and is never overwritten by a
    # same-owner recovery worker.
    "entry_post_bind_token",
    "adaptive_risk_decision_packet",
    "adaptive_risk_reservation_claim",
    "adaptive_risk_reservation_request",
})


def _entry_identity_metadata_matches(
    existing: dict[str, Any],
    proposed: dict[str, Any],
) -> bool:
    for key in _ENTRY_IDENTITY_METADATA_KEYS:
        if key not in proposed:
            continue
        if key not in existing or existing.get(key) != proposed.get(key):
            return False
    return True


def _entry_pre_transport_generation_rebindable(
    existing: dict[str, Any],
    proposed: dict[str, Any],
) -> bool:
    """Require one complete immutable entry identity except for its binder.

    A restarted worker may rotate only the expired pre-HTTP generation token.
    Partial/legacy metadata cannot use this recovery seam because omission must
    never turn into authority to reinterpret a CID-bound instruction.
    """

    required = set(_ENTRY_IDENTITY_METADATA_KEYS)
    if not required.issubset(existing) or not required.issubset(proposed):
        return False
    prior_binder = str(existing.get("entry_post_bind_token") or "").strip()
    next_binder = str(proposed.get("entry_post_bind_token") or "").strip()
    if not prior_binder or not next_binder or prior_binder == next_binder:
        return False
    return all(
        existing.get(key) == proposed.get(key)
        for key in required
        if key != "entry_post_bind_token"
    )


def _owner_transport_request_valid(
    request: Any,
    *,
    symbol: str,
    client_order_id: str,
    transport_kind: str,
) -> bool:
    """Narrow paper-equity long-close envelope for the owner's transport slot."""
    if not isinstance(request, dict):
        return False
    try:
        qty = float(request.get("base_size"))
    except (TypeError, ValueError):
        return False
    order_type = str(request.get("order_type") or "").strip().lower()
    tif = str(request.get("time_in_force") or "").strip().lower()
    kind = str(transport_kind or "").strip().lower()
    if kind not in {"deadman", "ordinary_exit", "emergency_exit"}:
        return False
    if order_type not in {"market", "limit", "stop"}:
        return False
    if kind == "deadman" and (
        order_type != "stop" or abs(qty - round(qty)) > 1e-9
    ):
        # Alpaca fractional equity stops cannot satisfy the GTC protection
        # contract.  A fractional remainder must rotate to a DAY close instead.
        return False
    if kind in {"ordinary_exit", "emergency_exit"} and order_type not in {
        "market",
        "limit",
    }:
        return False
    if tif not in {"day", "gtc"}:
        return False
    if order_type == "market" and tif != "day":
        return False
    if not (
        str(request.get("account_scope") or "").strip().lower() == "alpaca:paper"
        and str(request.get("alpaca_account_id") or "").strip()
        and not alpaca_symbol_is_crypto_like(symbol)
        and not alpaca_symbol_is_crypto_like(request.get("product_id"))
        and not alpaca_asset_class_is_crypto(request.get("asset_class"))
        and str(request.get("product_id") or "").strip().upper() == _symbol(symbol)
        and str(request.get("client_order_id") or "").strip() == str(client_order_id or "").strip()
        and str(request.get("side") or "").strip().lower() == "sell"
        and str(request.get("position_intent") or "").strip().lower() == "sell_to_close"
        and math.isfinite(qty)
        and qty > 0.0
    ):
        return False
    if order_type == "limit":
        try:
            price = float(request.get("limit_price"))
        except (TypeError, ValueError):
            return False
        if not math.isfinite(price) or price <= 0.0:
            return False
    elif order_type == "stop":
        try:
            price = float(request.get("stop_price"))
        except (TypeError, ValueError):
            return False
        if not math.isfinite(price) or price <= 0.0 or tif != "gtc":
            return False
    return True


def _parse_utc(value: Any) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _append_protective_terminal_generation(
    handoff: dict[str, Any],
    transport: dict[str, Any] | None,
) -> dict[str, Any]:
    """Retain ordered deadman terminal lineage across child generations."""
    current = dict(transport) if isinstance(transport, dict) else {}
    try:
        filled = float(current.get("filled_size"))
        remaining = float(current.get("remaining_quantity"))
    except (TypeError, ValueError):
        return dict(handoff)
    cid = str(current.get("client_order_id") or "").strip()
    oid = str(current.get("broker_order_id") or "").strip()
    request = current.get("order_request")
    request = request if isinstance(request, dict) else {}
    if not (
        str(current.get("transport_kind") or "").strip().lower() == "deadman"
        and str(current.get("phase") or "").strip().lower() == "resolved"
        and str(current.get("broker_order_status") or "").strip().lower()
        in _TERMINAL_ORDER_STATUSES
        and cid
        and oid
        and request
        and math.isfinite(filled)
        and filled >= 0.0
        and math.isfinite(remaining)
        and remaining >= 0.0
    ):
        return dict(handoff)
    generation = {
        "identity_contract": "alpaca_protective_terminal_generation_v1",
        "client_order_id": cid,
        "broker_order_id": oid,
        "order_request": dict(request),
        "broker_order_status": str(current.get("broker_order_status") or "").strip().lower(),
        "filled_size": filled,
        "remaining_quantity": remaining,
        "resolved_at_utc": current.get("resolved_at_utc"),
    }
    updated = dict(handoff)
    lineage = updated.get("protective_terminal_generations")
    lineage = list(lineage) if isinstance(lineage, list) else []
    duplicate = any(
        isinstance(row, dict)
        and row.get("client_order_id") == cid
        and row.get("broker_order_id") == oid
        and row.get("order_request") == request
        for row in lineage
    )
    if not duplicate:
        lineage.append(generation)
    updated["protective_terminal_generations"] = lineage
    return updated


def prepare_deadman_close_handoff(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    account_scope: str,
    alpaca_account_id: str,
    handoff_token: str,
    deadman_client_order_id: str,
    deadman_broker_order_id: str,
    deadman_order_request: dict[str, Any],
    successor_transport_kind: str,
    successor_intent: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Freeze a deadman-to-close generation in the durable owner claim.

    This short transaction does not release the caller's session-row lock.  A
    crash after it returns can recover the same successor CID/intent directly
    from the claim, while the old deadman remains the active owner transport.
    """
    scope = str(account_scope or "").strip().lower()
    sym = _symbol(symbol)
    account_id = str(alpaca_account_id or "").strip()
    token = str(handoff_token or "").strip()
    deadman_cid = str(deadman_client_order_id or "").strip()
    deadman_oid = str(deadman_broker_order_id or "").strip()
    successor_kind = str(successor_transport_kind or "").strip().lower()
    exit_reason = str(reason or "").strip()
    deadman_request = dict(deadman_order_request or {})
    successor = dict(successor_intent or {})
    successor_cid = str(successor.get("client_order_id") or "").strip()
    if not (
        scope == "alpaca:paper"
        and sym
        and account_id
        and token
        and deadman_cid
        and deadman_oid
        and exit_reason
        and successor_kind in {"ordinary_exit", "emergency_exit"}
        and _owner_transport_request_valid(
            deadman_request,
            symbol=sym,
            client_order_id=deadman_cid,
            transport_kind="deadman",
        )
        and _owner_transport_request_valid(
            successor,
            symbol=sym,
            client_order_id=successor_cid,
            transport_kind=successor_kind,
        )
    ):
        return {"ok": False, "reason": "deadman_close_handoff_not_certified"}
    readable, claim = read_action_claim(
        db, symbol=sym, account_scope=scope, for_update=True
    )
    if not readable or claim is None:
        return {"ok": False, "reason": "deadman_close_handoff_claim_unreadable"}
    metadata = dict(claim.get("metadata") or {})
    frozen_entry_request = metadata.get("order_request")
    frozen_entry_request = (
        frozen_entry_request if isinstance(frozen_entry_request, dict) else {}
    )
    current = metadata.get(_OWNER_TRANSPORT_METADATA_KEY)
    current = dict(current) if isinstance(current, dict) else {}
    if not (
        claim.get("phase") != RESOLVED
        and claim.get("action") == "entry"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("owner_session_id") == int(owner_session_id)
        and str(
            metadata.get("alpaca_account_id")
            or frozen_entry_request.get("alpaca_account_id")
            or ""
        ).strip() == account_id
        and str(current.get("transport_kind") or "").strip().lower() == "deadman"
        and str(current.get("client_order_id") or "").strip() == deadman_cid
        and str(current.get("broker_order_id") or "").strip() == deadman_oid
        and current.get("order_request") == deadman_request
        and str(current.get("phase") or "").strip().lower() != "resolved"
    ):
        return {"ok": False, "reason": "deadman_close_handoff_owner_mismatch"}
    proposed = {
        "identity_contract": "alpaca_deadman_close_handoff_v1",
        "handoff_token": token,
        "owner_session_id": int(owner_session_id),
        "alpaca_account_id": account_id,
        "symbol": sym,
        "phase": "intent_frozen",
        "deadman_client_order_id": deadman_cid,
        "deadman_broker_order_id": deadman_oid,
        "deadman_order_request": deadman_request,
        "successor_transport_kind": successor_kind,
        "successor_client_order_id": successor_cid,
        "successor_intent": successor,
        "successor_order_request": None,
        "reason": exit_reason,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    existing = metadata.get(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY)
    if isinstance(existing, dict):
        immutable_keys = (
            "identity_contract",
            "handoff_token",
            "owner_session_id",
            "alpaca_account_id",
            "symbol",
            "deadman_client_order_id",
            "deadman_broker_order_id",
            "deadman_order_request",
            "successor_transport_kind",
            "successor_client_order_id",
            "successor_intent",
            "reason",
        )
        if any(existing.get(key) != proposed.get(key) for key in immutable_keys):
            return {
                "ok": False,
                "reason": "deadman_close_handoff_generation_mismatch",
                "handoff": existing,
            }
        return {"ok": True, "handoff": existing, "reused": True}
    metadata[_DEADMAN_CLOSE_HANDOFF_METADATA_KEY] = proposed
    row = db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb),"
        " updated_at = NOW() WHERE account_scope = :scope AND symbol = :symbol"
        " AND claim_token = :claim_token AND action = 'entry' AND phase <> 'resolved'"
    ), {
        "metadata": json.dumps(metadata, separators=(",", ":"), default=str),
        "scope": scope,
        "symbol": sym,
        "claim_token": str(claim_token),
    })
    if int(row.rowcount or 0) != 1:
        return {"ok": False, "reason": "deadman_close_handoff_write_failed"}
    return {"ok": True, "handoff": proposed, "created": True}


def finalize_deadman_close_handoff_request(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    account_scope: str,
    alpaca_account_id: str,
    handoff_token: str,
    successor_order_request: dict[str, Any],
) -> dict[str, Any]:
    """CAS-freeze the one replayable successor request after terminal stop truth."""
    scope = str(account_scope or "").strip().lower()
    sym = _symbol(symbol)
    account_id = str(alpaca_account_id or "").strip()
    readable, claim = read_action_claim(
        db, symbol=sym, account_scope=scope, for_update=True
    )
    if not readable or claim is None:
        return {"ok": False, "reason": "deadman_close_handoff_claim_unreadable"}
    metadata = dict(claim.get("metadata") or {})
    handoff = metadata.get(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY)
    handoff = dict(handoff) if isinstance(handoff, dict) else {}
    current = metadata.get(_OWNER_TRANSPORT_METADATA_KEY)
    current = dict(current) if isinstance(current, dict) else {}
    request = dict(successor_order_request or {})
    kind = str(handoff.get("successor_transport_kind") or "").strip().lower()
    cid = str(handoff.get("successor_client_order_id") or "").strip()
    intent = handoff.get("successor_intent")
    intent = dict(intent) if isinstance(intent, dict) else {}
    try:
        final_qty = float(request.get("base_size"))
        intent_qty = float(intent.get("base_size"))
        deadman_remaining = float(current.get("remaining_quantity"))
    except (TypeError, ValueError):
        final_qty = intent_qty = deadman_remaining = math.nan
    immutable_request_keys = (
        "account_scope",
        "alpaca_account_id",
        "product_id",
        "side",
        "client_order_id",
        "position_intent",
        "order_type",
        "time_in_force",
        "extended_hours",
        "limit_price",
    )
    valid = bool(
        claim.get("phase") != RESOLVED
        and claim.get("action") == "entry"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("owner_session_id") == int(owner_session_id)
        and handoff.get("identity_contract") == "alpaca_deadman_close_handoff_v1"
        and str(handoff.get("handoff_token") or "") == str(handoff_token or "")
        and str(handoff.get("alpaca_account_id") or "") == account_id
        and handoff.get("phase") in {"deadman_terminal", "successor_ready"}
        and str(current.get("transport_kind") or "").strip().lower() == "deadman"
        and current.get("phase") == "resolved"
        and str(current.get("client_order_id") or "").strip()
        == str(handoff.get("deadman_client_order_id") or "").strip()
        and str(current.get("broker_order_id") or "").strip()
        == str(handoff.get("deadman_broker_order_id") or "").strip()
        and current.get("order_request") == handoff.get("deadman_order_request")
        and str(current.get("broker_order_status") or "").strip().lower()
        in _TERMINAL_ORDER_STATUSES
        and current.get("proven_no_transport") is not True
        and current.get("pre_accept_rejected") is not True
        and _owner_transport_request_valid(
            request,
            symbol=sym,
            client_order_id=cid,
            transport_kind=kind,
        )
        and math.isfinite(final_qty)
        and math.isfinite(intent_qty)
        and 0.0 < final_qty <= intent_qty + max(1e-9, abs(intent_qty) * 1e-8)
        and math.isfinite(deadman_remaining)
        and abs(deadman_remaining - final_qty)
        <= max(1e-9, abs(final_qty) * 1e-8)
        and str(request.get("alpaca_account_id") or "").strip() == account_id
        and all(request.get(key) == intent.get(key) for key in immutable_request_keys)
    )
    if not valid:
        return {"ok": False, "reason": "deadman_close_final_request_not_certified"}
    existing = handoff.get("successor_order_request")
    if isinstance(existing, dict) and existing != request:
        return {
            "ok": False,
            "reason": "deadman_close_final_request_immutable_mismatch",
            "handoff": handoff,
        }
    handoff["successor_order_request"] = request
    handoff["phase"] = "successor_ready"
    handoff["finalized_at_utc"] = str(
        handoff.get("finalized_at_utc") or datetime.now(timezone.utc).isoformat()
    )
    metadata[_DEADMAN_CLOSE_HANDOFF_METADATA_KEY] = handoff
    row = db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb),"
        " updated_at = NOW() WHERE account_scope = :scope AND symbol = :symbol"
        " AND claim_token = :claim_token AND action = 'entry' AND phase <> 'resolved'"
    ), {
        "metadata": json.dumps(metadata, separators=(",", ":"), default=str),
        "scope": scope,
        "symbol": sym,
        "claim_token": str(claim_token),
    })
    if int(row.rowcount or 0) != 1:
        return {"ok": False, "reason": "deadman_close_final_request_write_failed"}
    return {"ok": True, "handoff": handoff, "reused": isinstance(existing, dict)}


def retire_deadman_close_handoff(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    account_scope: str,
    alpaca_account_id: str,
    handoff_token: str,
    outcome: str,
) -> bool:
    """Retire one exact handoff only from a broker-inert terminal generation."""
    scope = str(account_scope or "").strip().lower()
    readable, claim = read_action_claim(
        db, symbol=symbol, account_scope=scope, for_update=True
    )
    if not readable or claim is None or not (
        claim.get("phase") != RESOLVED
        and claim.get("action") == "entry"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("owner_session_id") == int(owner_session_id)
    ):
        return False
    metadata = dict(claim.get("metadata") or {})
    handoff = metadata.get(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY)
    handoff = dict(handoff) if isinstance(handoff, dict) else {}
    current = metadata.get(_OWNER_TRANSPORT_METADATA_KEY)
    current = dict(current) if isinstance(current, dict) else {}
    account_id = str(alpaca_account_id or "").strip()
    result = str(outcome or "").strip().lower()
    successor_cid = str(handoff.get("successor_client_order_id") or "").strip()
    current_cid = str(current.get("client_order_id") or "").strip()
    current_kind = str(current.get("transport_kind") or "").strip().lower()
    current_phase = str(current.get("phase") or "").strip().lower()
    safe = False
    if result in {"successor_proven_no_transport", "successor_terminal_zero_fill"}:
        safe = bool(
            current_kind in {"ordinary_exit", "emergency_exit"}
            and current_cid == successor_cid
            and current_phase == "resolved"
            and (
                current.get("proven_no_transport") is True
                or (
                    result == "successor_terminal_zero_fill"
                    and float(current.get("filled_size") or 0.0) <= 1e-12
                )
            )
        )
    elif result in {"successor_terminal_accounted", "position_closed_by_deadman"}:
        safe = bool(
            current_phase == "resolved"
            and (
                (
                    result == "successor_terminal_accounted"
                    and current_kind in {"ordinary_exit", "emergency_exit"}
                    and current_cid == successor_cid
                )
                or (
                    result == "position_closed_by_deadman"
                    and current_kind == "deadman"
                    and current_cid
                    == str(handoff.get("deadman_client_order_id") or "").strip()
                )
            )
        )
    elif result == "successor_never_leased":
        safe = bool(
            current_phase == "resolved"
            and current_kind == "deadman"
            and current_cid
            == str(handoff.get("deadman_client_order_id") or "").strip()
            and handoff.get("phase") in {"intent_frozen", "successor_ready"}
        )
    if not (
        safe
        and handoff.get("identity_contract") == "alpaca_deadman_close_handoff_v1"
        and str(handoff.get("handoff_token") or "") == str(handoff_token or "")
        and str(handoff.get("alpaca_account_id") or "") == account_id
    ):
        return False
    retired = {
        **handoff,
        "phase": "retired",
        "retirement_outcome": result,
        "retired_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    history = metadata.get(_DEADMAN_CLOSE_HANDOFF_HISTORY_KEY)
    history = list(history) if isinstance(history, list) else []
    history.append(retired)
    metadata[_DEADMAN_CLOSE_HANDOFF_HISTORY_KEY] = history[-20:]
    metadata.pop(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY, None)
    row = db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb),"
        " updated_at = NOW() WHERE account_scope = :scope AND symbol = :symbol"
        " AND claim_token = :claim_token AND action = 'entry' AND phase <> 'resolved'"
    ), {
        "metadata": json.dumps(metadata, separators=(",", ":"), default=str),
        "scope": scope,
        "symbol": _symbol(symbol),
        "claim_token": str(claim_token),
    })
    return int(row.rowcount or 0) == 1


def retire_deadman_handoff_for_fractional_day_close(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    account_scope: str,
    alpaca_account_id: str,
    handoff_token: str,
    broker_position_quantity: float,
) -> bool:
    """Release terminal handoff authority for one fractional DAY close.

    Alpaca cannot hold the lane's GTC disaster stop for a fractional equity
    remainder.  The next safe generation is therefore an ordinary/emergency DAY
    close, but that generation must not be leased until every predecessor fill is
    durably reflected in the session row.  This CAS verifies the committed local
    quantity, the exact terminal owner transport, and all retained protective
    watermarks before archiving the handoff.  The generic owner-transport outbox
    may then durably lease the new DAY close on the next literal submit.
    """
    scope = str(account_scope or "").strip().lower()
    sym = _symbol(symbol)
    account_id = str(alpaca_account_id or "").strip()
    try:
        broker_qty = float(broker_position_quantity)
    except (TypeError, ValueError):
        broker_qty = math.nan
    if not (
        scope == "alpaca:paper"
        and sym
        and account_id
        and math.isfinite(broker_qty)
        and broker_qty > 0.0
        and abs(broker_qty - round(broker_qty)) > 1e-9
    ):
        return False

    readable, claim = read_action_claim(
        db,
        symbol=sym,
        account_scope=scope,
        for_update=True,
    )
    if not readable or claim is None or not (
        claim.get("phase") != RESOLVED
        and claim.get("action") == "entry"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("owner_session_id") == int(owner_session_id)
    ):
        return False
    metadata = dict(claim.get("metadata") or {})
    entry_request = metadata.get("order_request")
    entry_request = entry_request if isinstance(entry_request, dict) else {}
    handoff = metadata.get(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY)
    handoff = dict(handoff) if isinstance(handoff, dict) else {}
    current = metadata.get(_OWNER_TRANSPORT_METADATA_KEY)
    current = dict(current) if isinstance(current, dict) else {}
    current_request = current.get("order_request")
    current_request = current_request if isinstance(current_request, dict) else {}
    current_kind = str(current.get("transport_kind") or "").strip().lower()
    current_cid = str(current.get("client_order_id") or "").strip()
    current_oid = str(current.get("broker_order_id") or "").strip()
    current_status = str(current.get("broker_order_status") or "").strip().lower()
    current_no_transport = current.get("proven_no_transport") is True
    if not (
        str(
            metadata.get("alpaca_account_id")
            or entry_request.get("alpaca_account_id")
            or ""
        ).strip()
        == account_id
        and handoff.get("identity_contract") == "alpaca_deadman_close_handoff_v1"
        and str(handoff.get("handoff_token") or "").strip()
        == str(handoff_token or "").strip()
        and handoff.get("owner_session_id") == int(owner_session_id)
        and str(handoff.get("alpaca_account_id") or "").strip() == account_id
        and str(handoff.get("symbol") or "").strip().upper() == sym
        and str(handoff.get("phase") or "").strip().lower()
        in {
            "deadman_terminal",
            "successor_terminal",
            "successor_proven_no_transport",
            "replacement_deadman_terminal",
            "replacement_deadman_proven_no_transport",
        }
        and str(current.get("phase") or "").strip().lower() == "resolved"
        and current_kind in {"deadman", "ordinary_exit", "emergency_exit"}
        and current_cid
        and current_request
        and str(current_request.get("alpaca_account_id") or "").strip()
        == account_id
        and (
            current_no_transport
            or (current_oid and current_status in _TERMINAL_ORDER_STATUSES)
        )
    ):
        return False

    try:
        filled = float(current.get("filled_size") or 0.0)
        remaining = (
            float(current_request.get("base_size"))
            if current_no_transport
            else float(current.get("remaining_quantity"))
        )
    except (TypeError, ValueError):
        return False
    if not (
        math.isfinite(filled)
        and filled >= 0.0
        and math.isfinite(remaining)
        and remaining > 0.0
        and abs(remaining - broker_qty)
        <= max(1e-9, abs(broker_qty) * 1e-8)
    ):
        return False

    row = db.execute(text(
        "SELECT risk_snapshot_json FROM trading_automation_sessions"
        " WHERE id = :session_id"
    ), {"session_id": int(owner_session_id)}).fetchone()
    snapshot = row[0] if row is not None and isinstance(row[0], dict) else {}
    live = snapshot.get("momentum_live_execution")
    live = live if isinstance(live, dict) else {}
    position = live.get("position")
    position = position if isinstance(position, dict) else {}
    fractional_marker = live.get("alpaca_fractional_day_close_required")
    fractional_marker = (
        fractional_marker if isinstance(fractional_marker, dict) else {}
    )
    source = fractional_marker.get("source_owner_transport")
    source = source if isinstance(source, dict) else {}
    try:
        local_qty = float(position.get("quantity"))
        marker_qty = float(fractional_marker.get("broker_remainder_quantity"))
    except (TypeError, ValueError):
        return False
    if not (
        fractional_marker.get("identity_contract")
        == "alpaca_fractional_day_close_v1"
        and str(fractional_marker.get("product_id") or "").strip().upper() == sym
        and math.isfinite(local_qty)
        and abs(local_qty - broker_qty) <= max(1e-9, broker_qty * 1e-8)
        and math.isfinite(marker_qty)
        and abs(marker_qty - broker_qty) <= max(1e-9, broker_qty * 1e-8)
        and str(source.get("transport_kind") or "").strip().lower() == current_kind
        and str(source.get("client_order_id") or "").strip() == current_cid
        and str(source.get("broker_order_id") or "").strip() == current_oid
        and source.get("order_request") == current_request
        and str(source.get("phase") or "").strip().lower() == "resolved"
    ):
        return False

    def _exact_marker(
        markers: Any,
        transport: dict[str, Any],
        *,
        marker_order_key: str,
    ) -> bool:
        try:
            expected_fill = float(transport.get("filled_size") or 0.0)
            expected_remaining = float(transport.get("remaining_quantity"))
        except (TypeError, ValueError):
            return False
        if expected_fill <= 1e-12:
            return True
        exact = []
        for marker in markers if isinstance(markers, list) else []:
            if not isinstance(marker, dict):
                continue
            marker_transport = marker.get("owner_transport")
            marker_transport = (
                marker_transport if isinstance(marker_transport, dict) else {}
            )
            try:
                marker_fill = float(marker.get("applied_filled_size"))
                marker_remaining = float(marker.get("broker_remaining_quantity"))
            except (TypeError, ValueError):
                continue
            if (
                str(marker.get("client_order_id") or "").strip()
                == str(transport.get("client_order_id") or "").strip()
                and str(marker.get(marker_order_key) or "").strip()
                == str(transport.get("broker_order_id") or "").strip()
                and (
                    marker.get("order_request") == transport.get("order_request")
                    or marker_transport.get("order_request")
                    == transport.get("order_request")
                )
                and abs(marker_fill - expected_fill)
                <= max(1e-9, abs(expected_fill) * 1e-8)
                and abs(marker_remaining - expected_remaining)
                <= max(1e-9, abs(expected_remaining) * 1e-8)
            ):
                exact.append(marker)
        return len(exact) == 1

    if not current_no_transport and filled > 1e-12:
        current_markers = (
            live.get("deadman_applied_fill_watermarks")
            if current_kind == "deadman"
            else live.get("alpaca_exit_applied_fill_watermarks")
        )
        if not _exact_marker(
            current_markers,
            current,
            marker_order_key=("order_id" if current_kind == "deadman" else "broker_order_id"),
        ):
            return False

    lineage = handoff.get("protective_terminal_generations")
    lineage = lineage if isinstance(lineage, list) else []
    deadman_markers = live.get("deadman_applied_fill_watermarks")
    for generation in lineage:
        if not isinstance(generation, dict) or not _exact_marker(
            deadman_markers,
            {
                **generation,
                "phase": "resolved",
            },
            marker_order_key="order_id",
        ):
            return False

    retired = {
        **handoff,
        "phase": "retired",
        "retirement_outcome": "fractional_remainder_day_close_required",
        "fractional_remainder_quantity": broker_qty,
        "retired_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    history = metadata.get(_DEADMAN_CLOSE_HANDOFF_HISTORY_KEY)
    history = list(history) if isinstance(history, list) else []
    history.append(retired)
    metadata[_DEADMAN_CLOSE_HANDOFF_HISTORY_KEY] = history[-20:]
    metadata.pop(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY, None)
    result = db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb),"
        " updated_at = NOW() WHERE account_scope = :scope AND symbol = :symbol"
        " AND claim_token = :token AND action = 'entry' AND phase <> 'resolved'"
    ), {
        "metadata": json.dumps(metadata, separators=(",", ":"), default=str),
        "scope": scope,
        "symbol": sym,
        "token": str(claim_token),
    })
    return int(result.rowcount or 0) == 1


def lease_deadman_handoff_replacement(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    account_scope: str,
    alpaca_account_id: str,
    client_order_id: str,
    order_request: dict[str, Any],
    lease_token: str,
    broker_position_quantity: float,
    local_position_quantity: float,
    strict_cid_absent_after_expiry: bool = False,
    lease_seconds: int = _OWNER_TRANSPORT_LEASE_SECONDS,
) -> dict[str, Any]:
    """Atomically install a replacement stop while retaining the close handoff.

    The handoff is not retired here.  It remains the crash-recovery authority
    until a later exact broker read proves this replacement stop active.
    """
    scope = str(account_scope or "").strip().lower()
    sym = _symbol(symbol)
    account_id = str(alpaca_account_id or "").strip()
    cid = str(client_order_id or "").strip()
    worker_token = str(lease_token or "").strip()
    request = dict(order_request or {})
    try:
        broker_qty = float(broker_position_quantity)
        local_qty = float(local_position_quantity)
        request_qty = float(request.get("base_size"))
    except (TypeError, ValueError):
        broker_qty = local_qty = request_qty = math.nan
    if not (
        scope == "alpaca:paper"
        and account_id
        and worker_token
        and _owner_transport_request_valid(
            request,
            symbol=sym,
            client_order_id=cid,
            transport_kind="deadman",
        )
        and str(request.get("alpaca_account_id") or "").strip() == account_id
        and math.isfinite(broker_qty)
        and broker_qty > 0.0
        and math.isfinite(local_qty)
        and local_qty > 0.0
        and abs(local_qty - broker_qty)
        <= max(1e-9, abs(broker_qty) * 1e-8)
        and math.isfinite(request_qty)
        and abs(request_qty - broker_qty)
        <= max(1e-9, abs(broker_qty) * 1e-8)
    ):
        return {"ok": False, "reason": "replacement_deadman_request_not_certified"}
    readable, claim = read_action_claim(
        db,
        symbol=sym,
        account_scope=scope,
        for_update=True,
    )
    if not readable or claim is None:
        return {"ok": False, "reason": "replacement_deadman_claim_unreadable"}
    metadata = dict(claim.get("metadata") or {})
    entry_request = metadata.get("order_request")
    entry_request = entry_request if isinstance(entry_request, dict) else {}
    handoff = metadata.get(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY)
    handoff = dict(handoff) if isinstance(handoff, dict) else {}
    current = metadata.get(_OWNER_TRANSPORT_METADATA_KEY)
    current = dict(current) if isinstance(current, dict) else {}
    if not (
        claim.get("phase") != RESOLVED
        and claim.get("action") == "entry"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("owner_session_id") == int(owner_session_id)
        and str(
            metadata.get("alpaca_account_id")
            or entry_request.get("alpaca_account_id")
            or ""
        ).strip()
        == account_id
        and handoff.get("identity_contract") == "alpaca_deadman_close_handoff_v1"
        and handoff.get("owner_session_id") == int(owner_session_id)
        and str(handoff.get("alpaca_account_id") or "").strip() == account_id
        and str(handoff.get("symbol") or "").strip().upper() == sym
    ):
        return {"ok": False, "reason": "replacement_deadman_generation_mismatch"}

    phase = str(handoff.get("phase") or "").strip().lower()
    current_kind = str(current.get("transport_kind") or "").strip().lower()
    current_cid = str(current.get("client_order_id") or "").strip()
    current_phase = str(current.get("phase") or "").strip().lower()
    successor_kind = str(
        handoff.get("successor_transport_kind") or ""
    ).strip().lower()
    successor_cid = str(handoff.get("successor_client_order_id") or "").strip()
    successor_request = handoff.get("successor_order_request")
    successor_request = (
        successor_request if isinstance(successor_request, dict) else {}
    )
    prior_replacement_request = handoff.get("replacement_deadman_order_request")
    prior_replacement_request = (
        prior_replacement_request
        if isinstance(prior_replacement_request, dict)
        else {}
    )

    same_inflight_replacement = bool(
        phase in {
            "replacement_deadman_leased",
            "replacement_deadman_submitted",
            "replacement_deadman_submit_indeterminate",
        }
        and current_kind == "deadman"
        and current_cid == cid
        and current.get("order_request") == request
        and str(handoff.get("replacement_deadman_client_order_id") or "").strip()
        == cid
        and prior_replacement_request == request
        and current_phase != "resolved"
    )
    if same_inflight_replacement:
        expiry = _parse_utc(current.get("lease_expires_at_utc"))
        now = datetime.now(timezone.utc)
        expired = bool(expiry is not None and expiry <= now)
        if strict_cid_absent_after_expiry and expired:
            lease_expires = now + timedelta(seconds=max(30, int(lease_seconds)))
            current = {
                **current,
                "phase": "submitting",
                "lease_token": worker_token,
                "lease_expires_at_utc": lease_expires.isoformat(),
                "updated_at_utc": now.isoformat(),
                "same_cid_replay_count": int(
                    current.get("same_cid_replay_count") or 0
                )
                + 1,
            }
            handoff = {
                **handoff,
                "phase": "replacement_deadman_leased",
                "replacement_deadman_lease_token": worker_token,
                "replacement_deadman_lease_expires_at_utc": lease_expires.isoformat(),
                "replacement_deadman_released_at_utc": now.isoformat(),
            }
            metadata[_OWNER_TRANSPORT_METADATA_KEY] = current
            metadata[_DEADMAN_CLOSE_HANDOFF_METADATA_KEY] = handoff
            row = db.execute(text(
                "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb),"
                " updated_at = :now WHERE account_scope = :scope AND symbol = :symbol"
                " AND claim_token = :token AND action = 'entry' AND phase <> 'resolved'"
            ), {
                "metadata": json.dumps(metadata, separators=(",", ":"), default=str),
                "now": now,
                "scope": scope,
                "symbol": sym,
                "token": str(claim_token),
            })
            if int(row.rowcount or 0) != 1:
                return {"ok": False, "reason": "replacement_deadman_replay_write_failed"}
            return {
                "ok": True,
                "transport": current,
                "handoff": handoff,
                "same_cid_replay": True,
            }
        return {
            "ok": False,
            "reason": "replacement_deadman_reconcile_required",
            "transport": current,
            "handoff": handoff,
            "lease_expired": expired,
        }

    safe_inert_predecessor = False
    predecessor_outcome = None
    predecessor_remaining = math.nan
    if phase in {"deadman_terminal", "successor_ready"}:
        safe_inert_predecessor = bool(
            current_phase == "resolved"
            and current_kind == "deadman"
            and current_cid
            == str(handoff.get("deadman_client_order_id") or "").strip()
            and current.get("order_request") == handoff.get("deadman_order_request")
            and str(current.get("broker_order_status") or "").strip().lower()
            in _TERMINAL_ORDER_STATUSES
        )
        predecessor_outcome = "successor_never_leased"
        try:
            predecessor_remaining = float(current.get("remaining_quantity"))
        except (TypeError, ValueError):
            predecessor_remaining = math.nan
    elif phase == "successor_proven_no_transport":
        safe_inert_predecessor = bool(
            current_phase == "resolved"
            and current_kind == successor_kind
            and current_cid == successor_cid
            and current.get("order_request") == successor_request
            and current.get("proven_no_transport") is True
        )
        predecessor_outcome = "successor_proven_no_transport"
        try:
            predecessor_remaining = float(successor_request.get("base_size"))
        except (TypeError, ValueError):
            predecessor_remaining = math.nan
    elif phase == "successor_terminal":
        try:
            terminal_fill = float(current.get("filled_size"))
            terminal_remaining = float(current.get("remaining_quantity"))
        except (TypeError, ValueError):
            terminal_fill = terminal_remaining = math.nan
        successor_terminal_exact = bool(
            current_phase == "resolved"
            and current_kind == successor_kind
            and current_cid == successor_cid
            and current.get("order_request") == successor_request
            and str(current.get("broker_order_status") or "").strip().lower()
            in _TERMINAL_ORDER_STATUSES
            and math.isfinite(terminal_fill)
            and terminal_fill >= 0.0
            and math.isfinite(terminal_remaining)
            and terminal_remaining > 1e-12
        )
        locally_accounted = terminal_fill <= 1e-12
        if successor_terminal_exact and terminal_fill > 1e-12:
            # Read the prior committed session generation.  The caller holds its
            # current row lock in another transaction; a same-tick uncommitted
            # marker is intentionally invisible and therefore cannot authorize
            # re-protection before PnL/quantity accounting commits.
            row = db.execute(text(
                "SELECT risk_snapshot_json FROM trading_automation_sessions"
                " WHERE id = :session_id"
            ), {"session_id": int(owner_session_id)}).fetchone()
            snapshot = (
                row[0]
                if row is not None and isinstance(row[0], dict)
                else {}
            )
            live = snapshot.get("momentum_live_execution")
            live = live if isinstance(live, dict) else {}
            position = live.get("position")
            position = position if isinstance(position, dict) else {}
            markers = live.get("alpaca_exit_applied_fill_watermarks")
            markers = markers if isinstance(markers, list) else []
            try:
                local_remaining = float(position.get("quantity"))
            except (TypeError, ValueError):
                local_remaining = math.nan
            matching_markers = []
            for marker in markers:
                if not isinstance(marker, dict):
                    continue
                try:
                    marker_fill = float(marker.get("applied_filled_size"))
                    marker_remaining = float(marker.get("broker_remaining_quantity"))
                except (TypeError, ValueError):
                    continue
                if (
                    str(marker.get("client_order_id") or "").strip()
                    == successor_cid
                    and str(marker.get("broker_order_id") or "").strip()
                    == str(current.get("broker_order_id") or "").strip()
                    and marker.get("order_request") == successor_request
                    and abs(marker_fill - terminal_fill)
                    <= max(1e-9, abs(terminal_fill) * 1e-8)
                    and abs(marker_remaining - terminal_remaining)
                    <= max(1e-9, abs(terminal_remaining) * 1e-8)
                ):
                    matching_markers.append(marker)
            locally_accounted = bool(
                len(matching_markers) == 1
                and math.isfinite(local_remaining)
                and abs(local_remaining - terminal_remaining)
                <= max(1e-9, abs(terminal_remaining) * 1e-8)
            )
        safe_inert_predecessor = successor_terminal_exact and locally_accounted
        predecessor_remaining = terminal_remaining
        predecessor_outcome = (
            "successor_terminal_zero_fill"
            if terminal_fill <= 1e-12
            else "successor_terminal_partial_accounted"
        )
    elif phase in {
        "replacement_deadman_proven_no_transport",
        "replacement_deadman_terminal",
    }:
        try:
            replacement_fill = float(current.get("filled_size") or 0.0)
        except (TypeError, ValueError):
            replacement_fill = math.nan
        replacement_terminal_exact = bool(
            current_phase == "resolved"
            and current_kind == "deadman"
            and current_cid
            == str(handoff.get("replacement_deadman_client_order_id") or "").strip()
            and current.get("order_request") == prior_replacement_request
            and (
                current.get("proven_no_transport") is True
                or phase == "replacement_deadman_terminal"
            )
        )
        try:
            replacement_remaining = (
                float(prior_replacement_request.get("base_size"))
                if current.get("proven_no_transport") is True
                else float(current.get("remaining_quantity"))
            )
        except (TypeError, ValueError):
            replacement_remaining = math.nan
        # The caller must already have applied this terminal cumulative fill to
        # its in-memory session quantity and pass that exact value alongside a
        # fresh broker read.  The child may then be posted in the same pulse;
        # retained protective lineage forces replay if the outer transaction
        # rolls back before its watermark commits.
        replacement_locally_accounted = bool(
            replacement_terminal_exact
            and math.isfinite(replacement_remaining)
            and replacement_remaining > 1e-12
        )
        safe_inert_predecessor = bool(
            replacement_terminal_exact
            and math.isfinite(replacement_remaining)
            and replacement_remaining > 1e-12
            and replacement_locally_accounted
        )
        predecessor_remaining = replacement_remaining
        predecessor_outcome = "replacement_deadman_retry"
    if not safe_inert_predecessor or current_phase != "resolved":
        return {
            "ok": False,
            "reason": "replacement_deadman_predecessor_not_inert",
            "transport": current,
            "handoff": handoff,
        }
    # Exact predecessor remainder, caller's locally-accounted quantity, fresh
    # broker quantity, and replacement request must agree before claim mutation.
    # If the outer accounting transaction later rolls back, retained protective
    # lineage forces predecessor-first replay before this child is serviced.
    if not (
        math.isfinite(predecessor_remaining)
        and predecessor_remaining > 0.0
        and abs(predecessor_remaining - request_qty)
        <= max(1e-9, abs(request_qty) * 1e-8)
        and math.isfinite(local_qty)
        and abs(local_qty - request_qty)
        <= max(1e-9, abs(request_qty) * 1e-8)
        and abs(broker_qty - request_qty)
        <= max(1e-9, abs(request_qty) * 1e-8)
    ):
        return {
            "ok": False,
            "reason": "replacement_deadman_quantity_generation_mismatch",
            "transport": current,
            "handoff": handoff,
        }
    used_deadman_cids = {
        str(handoff.get("deadman_client_order_id") or "").strip(),
        successor_cid,
        str(handoff.get("replacement_deadman_client_order_id") or "").strip(),
    }
    owner_history = metadata.get(_OWNER_TRANSPORT_HISTORY_KEY)
    for candidate in owner_history if isinstance(owner_history, list) else []:
        if (
            isinstance(candidate, dict)
            and str(candidate.get("transport_kind") or "").strip().lower()
            == "deadman"
        ):
            used_deadman_cids.add(
                str(candidate.get("client_order_id") or "").strip()
            )
    protective_ledger = metadata.get(_PROTECTIVE_TERMINAL_LEDGER_KEY)
    for candidate in (
        protective_ledger if isinstance(protective_ledger, list) else []
    ):
        if isinstance(candidate, dict):
            used_deadman_cids.add(
                str(candidate.get("client_order_id") or "").strip()
            )
    lineage = handoff.get("protective_terminal_generations")
    for candidate in lineage if isinstance(lineage, list) else []:
        if isinstance(candidate, dict):
            used_deadman_cids.add(
                str(candidate.get("client_order_id") or "").strip()
            )
    if cid in used_deadman_cids:
        return {"ok": False, "reason": "replacement_deadman_cid_not_new"}

    generation = _deadman_client_order_generation(
        cid,
        owner_session_id=owner_session_id,
    )
    high_watermark = _durable_deadman_generation_high_watermark(
        metadata,
        owner_session_id=owner_session_id,
    )
    if (
        str(cid).startswith("chili_dm_") and generation is None
    ) or (
        generation is not None and generation <= high_watermark
    ):
        return {
            "ok": False,
            "reason": "replacement_deadman_generation_not_monotonic",
            "deadman_generation_high_watermark": high_watermark,
        }
    if generation is not None:
        metadata[_DEADMAN_GENERATION_HIGH_WATERMARK_KEY] = generation

    handoff = _append_protective_terminal_generation(handoff, current)

    now = datetime.now(timezone.utc)
    lease_expires = now + timedelta(seconds=max(30, int(lease_seconds)))
    replacement = {
        "identity_contract": "alpaca_owner_transport_v1",
        "transport_kind": "deadman",
        "client_order_id": cid,
        "order_request": request,
        "phase": "submitting",
        "lease_token": worker_token,
        "lease_expires_at_utc": lease_expires.isoformat(),
        "created_at_utc": now.isoformat(),
        "updated_at_utc": now.isoformat(),
    }
    handoff = {
        **handoff,
        "phase": "replacement_deadman_leased",
        "replacement_predecessor_outcome": predecessor_outcome,
        "replacement_deadman_client_order_id": cid,
        "replacement_deadman_order_request": request,
        "replacement_deadman_lease_token": worker_token,
        "replacement_deadman_lease_expires_at_utc": lease_expires.isoformat(),
        "replacement_deadman_leased_at_utc": now.isoformat(),
    }
    metadata[_OWNER_TRANSPORT_METADATA_KEY] = replacement
    metadata[_DEADMAN_CLOSE_HANDOFF_METADATA_KEY] = handoff
    row = db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb),"
        " updated_at = :now WHERE account_scope = :scope AND symbol = :symbol"
        " AND claim_token = :token AND action = 'entry' AND phase <> 'resolved'"
    ), {
        "metadata": json.dumps(metadata, separators=(",", ":"), default=str),
        "now": now,
        "scope": scope,
        "symbol": sym,
        "token": str(claim_token),
    })
    if int(row.rowcount or 0) != 1:
        return {"ok": False, "reason": "replacement_deadman_lease_write_failed"}
    return {"ok": True, "transport": replacement, "handoff": handoff}


def certify_deadman_handoff_reprotected(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    account_scope: str,
    alpaca_account_id: str,
    client_order_id: str,
    broker_order_id: str,
    broker_order_status: str,
    broker_order_lifecycle: str,
) -> bool:
    """Certify an active replacement while retaining predecessor lineage."""
    scope = str(account_scope or "").strip().lower()
    sym = _symbol(symbol)
    cid = str(client_order_id or "").strip()
    oid = str(broker_order_id or "").strip()
    status = str(broker_order_status or "").strip().lower()
    lifecycle = str(broker_order_lifecycle or "").strip().lower()
    if (
        not cid
        or not oid
        or not status
        or status in _TERMINAL_ORDER_STATUSES
        or lifecycle not in _ACTIVE_ALPACA_PROTECTIVE_LIFECYCLES
    ):
        return False
    readable, claim = read_action_claim(
        db,
        symbol=sym,
        account_scope=scope,
        for_update=True,
    )
    if not readable or claim is None or not (
        claim.get("phase") != RESOLVED
        and claim.get("action") == "entry"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("owner_session_id") == int(owner_session_id)
    ):
        return False
    metadata = dict(claim.get("metadata") or {})
    handoff = metadata.get(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY)
    handoff = dict(handoff) if isinstance(handoff, dict) else {}
    current = metadata.get(_OWNER_TRANSPORT_METADATA_KEY)
    current = dict(current) if isinstance(current, dict) else {}
    account_id = str(alpaca_account_id or "").strip()
    if not (
        handoff.get("identity_contract") == "alpaca_deadman_close_handoff_v1"
        and str(handoff.get("alpaca_account_id") or "").strip() == account_id
        and handoff.get("phase") in {
            "replacement_deadman_submitted",
            "replacement_deadman_active",
        }
        and str(handoff.get("replacement_deadman_client_order_id") or "").strip()
        == cid
        and str(handoff.get("replacement_deadman_broker_order_id") or "").strip()
        == oid
        and str(current.get("transport_kind") or "").strip().lower() == "deadman"
        and str(current.get("phase") or "").strip().lower() == "submitted"
        and str(current.get("client_order_id") or "").strip() == cid
        and str(current.get("broker_order_id") or "").strip() == oid
        and current.get("order_request")
        == handoff.get("replacement_deadman_order_request")
        and str((current.get("order_request") or {}).get("alpaca_account_id") or "").strip()
        == account_id
    ):
        return False
    active = {
        **handoff,
        "phase": "replacement_deadman_active",
        "replacement_deadman_broker_status": status,
        "replacement_deadman_broker_lifecycle": lifecycle,
        "replacement_deadman_active_certified_at_utc": str(
            handoff.get("replacement_deadman_active_certified_at_utc")
            or datetime.now(timezone.utc).isoformat()
        ),
    }
    metadata[_DEADMAN_CLOSE_HANDOFF_METADATA_KEY] = active
    row = db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb),"
        " updated_at = NOW() WHERE account_scope = :scope AND symbol = :symbol"
        " AND claim_token = :token AND action = 'entry' AND phase <> 'resolved'"
    ), {
        "metadata": json.dumps(metadata, separators=(",", ":"), default=str),
        "scope": scope,
        "symbol": sym,
        "token": str(claim_token),
    })
    return int(row.rowcount or 0) == 1


def reconcile_deadman_replacement_successor(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    account_scope: str,
    alpaca_account_id: str,
    predecessor_client_order_id: str,
    predecessor_broker_order_id: str,
    predecessor_order_request: dict[str, Any],
    predecessor_broker_lifecycle: str,
    predecessor_reported_filled_size: float,
    successor_client_order_id: str,
    successor_broker_order_id: str,
    successor_order_request: dict[str, Any],
    successor_broker_status: str,
    successor_broker_lifecycle: str,
    successor_reported_filled_size: float,
    successor_average_filled_price: float | None,
    attributable_filled_size: float,
    attributable_fill_source: str | None,
    broker_remaining_quantity: float,
    successor_active: bool,
    fill_attribution_quarantined: bool = False,
) -> dict[str, Any]:
    """CAS one exact Alpaca replacement edge into durable protective lineage.

    Alpaca reports ``replaced`` as an unresolved normalized status even though an
    exact, bidirectionally linked successor can prove that the predecessor is
    inert.  This transition deliberately preserves that raw truth; it never
    rewrites the predecessor as ``canceled``.  Active successors are adopted only
    for a zero-fill, quantity-conserved edge.  A terminal edge may carry one
    provably non-inherited cumulative fill for later idempotent local accounting.
    """
    scope = str(account_scope or "").strip().lower()
    sym = _symbol(symbol)
    account_id = str(alpaca_account_id or "").strip()
    predecessor_cid = str(predecessor_client_order_id or "").strip()
    predecessor_oid = str(predecessor_broker_order_id or "").strip()
    successor_cid = str(successor_client_order_id or "").strip()
    successor_oid = str(successor_broker_order_id or "").strip()
    predecessor_request = dict(predecessor_order_request or {})
    successor_request = dict(successor_order_request or {})
    predecessor_lifecycle = str(predecessor_broker_lifecycle or "").strip().lower()
    successor_status = str(successor_broker_status or "").strip().lower()
    successor_lifecycle = str(successor_broker_lifecycle or "").strip().lower()
    fill_source = str(attributable_fill_source or "").strip().lower() or None
    try:
        predecessor_fill = float(predecessor_reported_filled_size)
        successor_fill = float(successor_reported_filled_size)
        attributable_fill = float(attributable_filled_size)
        remaining = float(broker_remaining_quantity)
        requested = float(predecessor_request.get("base_size"))
        successor_requested = float(successor_request.get("base_size"))
        average_fill = (
            None
            if successor_average_filled_price is None
            else float(successor_average_filled_price)
        )
    except (TypeError, ValueError):
        return {"ok": False, "reason": "replacement_successor_numeric_truth_invalid"}
    tol = max(1e-9, abs(requested) * 1e-8) if math.isfinite(requested) else 1e-9
    positive_sources = [
        name
        for name, value in (
            ("predecessor", predecessor_fill),
            ("successor", successor_fill),
        )
        if value > 1e-12
    ]
    quarantine_active = bool(fill_attribution_quarantined and successor_active)
    attributable_exact = bool(
        (
            quarantine_active
            and successor_fill > 1e-12
            and remaining > 1e-12
            and attributable_fill <= 1e-12
            and fill_source is None
            and abs((requested - successor_fill) - remaining) <= tol
        )
        or (
            attributable_fill <= 1e-12
            and not positive_sources
            and fill_source is None
        )
        or (
            attributable_fill > 1e-12
            and len(positive_sources) == 1
            and fill_source == positive_sources[0]
            and abs(
                attributable_fill
                - (
                    predecessor_fill
                    if fill_source == "predecessor"
                    else successor_fill
                )
            )
            <= tol
            and average_fill is not None
            and math.isfinite(average_fill)
            and average_fill > 0.0
        )
    )
    successor_state_exact = bool(
        (
            successor_active
            and successor_status not in _TERMINAL_ORDER_STATUSES
            and successor_lifecycle in _ACTIVE_ALPACA_PROTECTIVE_LIFECYCLES
            and (attributable_fill <= 1e-12 or quarantine_active)
        )
        or (
            not successor_active
            and _terminal_order_status_lifecycle_compatible(
                successor_status,
                successor_lifecycle,
            )
        )
    )
    quantity_conservation_exact = bool(
        (
            quarantine_active
            and abs((requested - successor_fill) - remaining) <= tol
        )
        or (
            not quarantine_active
            and abs((requested - remaining) - attributable_fill) <= tol
        )
    )
    if not (
        scope == "alpaca:paper"
        and account_id
        and predecessor_cid
        and predecessor_oid
        and successor_cid
        and successor_oid
        and successor_cid != predecessor_cid
        and successor_oid != predecessor_oid
        and predecessor_lifecycle == "replaced"
        and _owner_transport_request_valid(
            predecessor_request,
            symbol=sym,
            client_order_id=predecessor_cid,
            transport_kind="deadman",
        )
        and _owner_transport_request_valid(
            successor_request,
            symbol=sym,
            client_order_id=successor_cid,
            transport_kind="deadman",
        )
        and str(predecessor_request.get("alpaca_account_id") or "").strip()
        == account_id
        and str(successor_request.get("alpaca_account_id") or "").strip()
        == account_id
        and math.isfinite(requested)
        and requested > 0.0
        and math.isfinite(successor_requested)
        and abs(successor_requested - requested) <= tol
        and all(
            math.isfinite(value) and value >= 0.0
            for value in (
                predecessor_fill,
                successor_fill,
                attributable_fill,
                remaining,
            )
        )
        and attributable_fill <= requested + tol
        and quantity_conservation_exact
        and attributable_exact
        and successor_state_exact
    ):
        return {"ok": False, "reason": "replacement_successor_conservation_unproven"}

    immutable_evidence = {
        "identity_contract": "alpaca_replacement_lineage_v1",
        "predecessor_client_order_id": predecessor_cid,
        "predecessor_broker_order_id": predecessor_oid,
        "predecessor_order_request": predecessor_request,
        "predecessor_broker_lifecycle": predecessor_lifecycle,
        "predecessor_reported_filled_size": predecessor_fill,
        "successor_client_order_id": successor_cid,
        "successor_broker_order_id": successor_oid,
        "successor_order_request": successor_request,
        "successor_replaces": predecessor_oid,
        "successor_broker_status": successor_status,
        "successor_broker_lifecycle": successor_lifecycle,
        "successor_reported_filled_size": successor_fill,
        "successor_average_filled_price": average_fill,
        "attributable_filled_size": attributable_fill,
        "attributable_fill_source": fill_source,
        "broker_remaining_quantity": remaining,
        "successor_active": bool(successor_active),
        "fill_attribution_quarantined": quarantine_active,
    }
    lineage_id = hashlib.sha256(
        json.dumps(
            immutable_evidence,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    immutable_evidence["replacement_lineage_id"] = lineage_id

    readable, claim = read_action_claim(
        db,
        symbol=sym,
        account_scope=scope,
        for_update=True,
    )
    if not readable or claim is None or not (
        claim.get("phase") != RESOLVED
        and claim.get("action") == "entry"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("owner_session_id") == int(owner_session_id)
    ):
        return {"ok": False, "reason": "replacement_successor_claim_unreadable"}
    metadata = dict(claim.get("metadata") or {})
    handoff = metadata.get(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY)
    handoff = dict(handoff) if isinstance(handoff, dict) else {}
    current = metadata.get(_OWNER_TRANSPORT_METADATA_KEY)
    current = dict(current) if isinstance(current, dict) else {}
    ledger = metadata.get(_PROTECTIVE_TERMINAL_LEDGER_KEY)
    ledger = list(ledger) if isinstance(ledger, list) else []
    quarantine_ledger = metadata.get(
        _PROTECTIVE_ATTRIBUTION_QUARANTINE_LEDGER_KEY
    )
    quarantine_ledger = (
        list(quarantine_ledger) if isinstance(quarantine_ledger, list) else []
    )
    exact_prior = [
        row
        for row in [*ledger, *quarantine_ledger]
        if isinstance(row, dict)
        and (
            (row.get("replacement_lineage_evidence") or {}).get(
                "replacement_lineage_id"
            )
            == lineage_id
            or row.get("replacement_lineage_id") == lineage_id
        )
    ]
    if len(exact_prior) > 1:
        return {"ok": False, "reason": "replacement_successor_lineage_ambiguous"}
    if exact_prior:
        expected_current_cid = successor_cid if successor_active else predecessor_cid
        expected_phase = "submitted" if successor_active else "resolved"
        if (
            str(current.get("client_order_id") or "").strip()
            == expected_current_cid
            and str(current.get("phase") or "").strip().lower() == expected_phase
        ):
            return {
                "ok": True,
                "reused": True,
                "transport": current,
                "handoff": handoff,
                "replacement_lineage": immutable_evidence,
            }
        return {"ok": False, "reason": "replacement_successor_replay_state_mismatch"}

    handoff_present = bool(handoff)
    handoff_exact = bool(
        not handoff_present
        or (
            handoff.get("identity_contract") == "alpaca_deadman_close_handoff_v1"
            and str(handoff.get("alpaca_account_id") or "").strip() == account_id
            and str(
                handoff.get("replacement_deadman_client_order_id") or ""
            ).strip()
            == predecessor_cid
            and str(
                handoff.get("replacement_deadman_broker_order_id") or ""
            ).strip()
            == predecessor_oid
            and handoff.get("replacement_deadman_order_request")
            == predecessor_request
        )
    )
    if not (
        handoff_exact
        and str(current.get("transport_kind") or "").strip().lower() == "deadman"
        and str(current.get("phase") or "").strip().lower() != "resolved"
        and str(current.get("client_order_id") or "").strip() == predecessor_cid
        and str(current.get("broker_order_id") or "").strip() == predecessor_oid
        and current.get("order_request") == predecessor_request
    ):
        return {"ok": False, "reason": "replacement_successor_owner_generation_mismatch"}

    resolved_at = datetime.now(timezone.utc).isoformat()
    resolved_predecessor = {
        **current,
        "phase": "resolved",
        # Preserve the exact Alpaca lifecycle.  `replaced` is made inert only by
        # the bidirectional successor evidence below, never by relabeling it.
        "broker_order_status": "replaced",
        "broker_order_lifecycle": "replaced",
        "filled_size": attributable_fill,
        "remaining_quantity": remaining,
        "fill_attribution_quarantined": quarantine_active,
        "replacement_lineage_evidence": immutable_evidence,
        "resolved_at_utc": resolved_at,
    }
    history = metadata.get(_OWNER_TRANSPORT_HISTORY_KEY)
    history = list(history) if isinstance(history, list) else []
    history.append(resolved_predecessor)
    if quarantine_active:
        quarantine_row = {
            **immutable_evidence,
            "identity_contract": "alpaca_replacement_attribution_quarantine_v1",
            "containment_id": f"active-adopt-{lineage_id}",
            "predecessor_order_request": predecessor_request,
            "broker_remaining_quantity": remaining,
            "quantity_delta_quarantined": max(0.0, requested - remaining),
            "successor_applied_fill_baseline": successor_fill,
            "activated_at_utc": resolved_at,
        }
        quarantine_ledger.append(quarantine_row)
        metadata[_PROTECTIVE_ATTRIBUTION_QUARANTINE_LEDGER_KEY] = (
            quarantine_ledger
        )
    else:
        ledger.append(resolved_predecessor)
    metadata[_OWNER_TRANSPORT_HISTORY_KEY] = history[-20:]
    metadata[_PROTECTIVE_TERMINAL_LEDGER_KEY] = ledger

    if successor_active:
        next_transport = {
            "identity_contract": "alpaca_owner_transport_v1",
            "transport_kind": "deadman",
            "client_order_id": successor_cid,
            "broker_order_id": successor_oid,
            "order_request": successor_request,
            "phase": "submitted",
            "replacement_lineage_parent": immutable_evidence,
            "created_at_utc": resolved_at,
            "updated_at_utc": resolved_at,
        }
        if handoff_present:
            handoff.update({
                "phase": "replacement_deadman_submitted",
                "replacement_deadman_client_order_id": successor_cid,
                "replacement_deadman_broker_order_id": successor_oid,
                "replacement_deadman_order_request": successor_request,
                "replacement_lineage_evidence": immutable_evidence,
            })
    else:
        next_transport = resolved_predecessor
        if handoff_present:
            handoff.update({
                "phase": "replacement_deadman_terminal",
                "replacement_deadman_terminal_status": successor_status,
                "replacement_deadman_terminal_filled_size": attributable_fill,
                "replacement_deadman_broker_remaining_quantity": remaining,
                "replacement_lineage_evidence": immutable_evidence,
            })
    metadata[_OWNER_TRANSPORT_METADATA_KEY] = next_transport
    if handoff_present:
        metadata[_DEADMAN_CLOSE_HANDOFF_METADATA_KEY] = handoff
    row = db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb),"
        " updated_at = NOW() WHERE account_scope = :scope AND symbol = :symbol"
        " AND claim_token = :token AND action = 'entry' AND phase <> 'resolved'"
    ), {
        "metadata": json.dumps(metadata, separators=(",", ":"), default=str),
        "scope": scope,
        "symbol": sym,
        "token": str(claim_token),
    })
    if int(row.rowcount or 0) != 1:
        return {"ok": False, "reason": "replacement_successor_write_failed"}
    return {
        "ok": True,
        "transport": next_transport,
        "handoff": handoff,
        "replacement_lineage": immutable_evidence,
    }


def advance_deadman_replacement_quarantine_baseline(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    account_scope: str,
    alpaca_account_id: str,
    containment_id: str,
    successor_client_order_id: str,
    successor_broker_order_id: str,
    successor_order_request: dict[str, Any],
    successor_reported_filled_size: float,
    broker_remaining_quantity: float,
) -> dict[str, Any]:
    """Monotonically advance one active replacement's quarantined fill baseline.

    The broker's cumulative fill remains deliberately unpriced: this transition
    records only exact quantity conservation for the same already-adopted active
    successor.  It therefore cannot invent P&L or authorize another transport.
    """
    scope = str(account_scope or "").strip().lower()
    sym = _symbol(symbol)
    account_id = str(alpaca_account_id or "").strip()
    quarantine_id = str(containment_id or "").strip()
    successor_cid = str(successor_client_order_id or "").strip()
    successor_oid = str(successor_broker_order_id or "").strip()
    successor_request = dict(successor_order_request or {})
    try:
        cumulative = float(successor_reported_filled_size)
        remaining = float(broker_remaining_quantity)
        requested = float(successor_request.get("base_size"))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "replacement_quarantine_baseline_numeric_invalid"}
    tol = max(1e-9, abs(requested) * 1e-8) if math.isfinite(requested) else 1e-9
    if not (
        scope == "alpaca:paper"
        and account_id
        and quarantine_id
        and successor_cid
        and successor_oid
        and _owner_transport_request_valid(
            successor_request,
            symbol=sym,
            client_order_id=successor_cid,
            transport_kind="deadman",
        )
        and str(successor_request.get("alpaca_account_id") or "").strip()
        == account_id
        and math.isfinite(requested)
        and requested > 0.0
        and math.isfinite(cumulative)
        and cumulative > 0.0
        and math.isfinite(remaining)
        and remaining > 0.0
        and abs((requested - cumulative) - remaining) <= tol
    ):
        return {"ok": False, "reason": "replacement_quarantine_baseline_unproven"}
    readable, claim = read_action_claim(
        db,
        symbol=sym,
        account_scope=scope,
        for_update=True,
    )
    if not readable or claim is None or not (
        claim.get("phase") != RESOLVED
        and claim.get("action") == "entry"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("owner_session_id") == int(owner_session_id)
    ):
        return {"ok": False, "reason": "replacement_quarantine_claim_unreadable"}
    metadata = dict(claim.get("metadata") or {})
    current = metadata.get(_OWNER_TRANSPORT_METADATA_KEY)
    current = dict(current) if isinstance(current, dict) else {}
    lineage = current.get("replacement_lineage_parent")
    lineage = dict(lineage) if isinstance(lineage, dict) else {}
    lineage_id = str(lineage.get("replacement_lineage_id") or "").strip()
    if not (
        lineage.get("identity_contract") == "alpaca_replacement_lineage_v1"
        and lineage.get("fill_attribution_quarantined") is True
        and quarantine_id == f"active-adopt-{lineage_id}"
        and str(current.get("transport_kind") or "").strip().lower() == "deadman"
        and str(current.get("phase") or "").strip().lower() == "submitted"
        and str(current.get("client_order_id") or "").strip() == successor_cid
        and str(current.get("broker_order_id") or "").strip() == successor_oid
        and current.get("order_request") == successor_request
    ):
        return {"ok": False, "reason": "replacement_quarantine_owner_mismatch"}
    ledger = metadata.get(_PROTECTIVE_ATTRIBUTION_QUARANTINE_LEDGER_KEY)
    ledger = list(ledger) if isinstance(ledger, list) else []
    matches = [
        (idx, dict(row))
        for idx, row in enumerate(ledger)
        if isinstance(row, dict)
        and str(row.get("containment_id") or "").strip() == quarantine_id
    ]
    if len(matches) != 1:
        return {"ok": False, "reason": "replacement_quarantine_identity_ambiguous"}
    index, row = matches[0]
    try:
        prior_cumulative = float(row.get("successor_applied_fill_baseline"))
        prior_remaining = float(row.get("broker_remaining_quantity"))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "replacement_quarantine_prior_baseline_invalid"}
    if not (
        str(row.get("successor_client_order_id") or "").strip() == successor_cid
        and str(row.get("successor_broker_order_id") or "").strip() == successor_oid
        and row.get("successor_order_request") == successor_request
        and math.isfinite(prior_cumulative)
        and prior_cumulative > 0.0
        and math.isfinite(prior_remaining)
        and prior_remaining > 0.0
        and abs((requested - prior_cumulative) - prior_remaining) <= tol
        and cumulative + tol >= prior_cumulative
        and remaining <= prior_remaining + tol
    ):
        return {"ok": False, "reason": "replacement_quarantine_baseline_regressed"}
    if (
        abs(cumulative - prior_cumulative) <= tol
        and abs(remaining - prior_remaining) <= tol
    ):
        return {"ok": True, "reused": True, "quarantine": row}
    history = row.get("successor_quarantined_fill_baselines")
    history = list(history) if isinstance(history, list) else []
    if not history:
        history.append({
            "successor_reported_filled_size": prior_cumulative,
            "broker_remaining_quantity": prior_remaining,
            "recorded_at_utc": row.get("activated_at_utc"),
        })
    updated_at = datetime.now(timezone.utc).isoformat()
    history.append({
        "successor_reported_filled_size": cumulative,
        "broker_remaining_quantity": remaining,
        "recorded_at_utc": updated_at,
    })
    row.update({
        "successor_applied_fill_baseline": cumulative,
        "broker_remaining_quantity": remaining,
        "successor_quarantined_fill_baselines": history,
        "baseline_updated_at_utc": updated_at,
    })
    ledger[index] = row
    metadata[_PROTECTIVE_ATTRIBUTION_QUARANTINE_LEDGER_KEY] = ledger
    result = db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb),"
        " updated_at = NOW() WHERE account_scope = :scope AND symbol = :symbol"
        " AND claim_token = :token AND action = 'entry' AND phase <> 'resolved'"
    ), {
        "metadata": json.dumps(metadata, separators=(",", ":"), default=str),
        "scope": scope,
        "symbol": sym,
        "token": str(claim_token),
    })
    if int(result.rowcount or 0) != 1:
        return {"ok": False, "reason": "replacement_quarantine_baseline_write_failed"}
    return {"ok": True, "quarantine": row}


def prepare_deadman_replacement_containment(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    account_scope: str,
    alpaca_account_id: str,
    predecessor_client_order_id: str,
    predecessor_broker_order_id: str,
    predecessor_order_request: dict[str, Any],
    predecessor_reported_filled_size: float,
    successor_client_order_id: str,
    successor_broker_order_id: str,
    successor_order_request: dict[str, Any],
    successor_broker_status: str,
    successor_broker_lifecycle: str,
    successor_reported_filled_size: float,
    close_intent: dict[str, Any],
) -> dict[str, Any]:
    """Precommit recovery authority before touching an ambiguous successor.

    The frozen close intent is a ceiling only.  Its final exact quantity remains
    unset until a post-cancel broker-position read, so a cancel-race fill cannot
    leave a stale close quantity in the durable outbox.
    """
    scope = str(account_scope or "").strip().lower()
    sym = _symbol(symbol)
    account_id = str(alpaca_account_id or "").strip()
    predecessor_cid = str(predecessor_client_order_id or "").strip()
    predecessor_oid = str(predecessor_broker_order_id or "").strip()
    successor_cid = str(successor_client_order_id or "").strip()
    successor_oid = str(successor_broker_order_id or "").strip()
    predecessor_request = dict(predecessor_order_request or {})
    successor_request = dict(successor_order_request or {})
    intent = dict(close_intent or {})
    intent_cid = str(intent.get("client_order_id") or "").strip()
    try:
        predecessor_fill = float(predecessor_reported_filled_size)
        successor_fill = float(successor_reported_filled_size)
        request_qty = float(predecessor_request.get("base_size"))
        successor_qty = float(successor_request.get("base_size"))
        intent_qty = float(intent.get("base_size"))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "replacement_containment_numeric_truth_invalid"}
    tol = max(1e-9, abs(request_qty) * 1e-8) if math.isfinite(request_qty) else 1e-9
    if not (
        scope == "alpaca:paper"
        and account_id
        and predecessor_cid
        and predecessor_oid
        and successor_cid
        and successor_oid
        and intent_cid
        and _owner_transport_request_valid(
            predecessor_request,
            symbol=sym,
            client_order_id=predecessor_cid,
            transport_kind="deadman",
        )
        and _owner_transport_request_valid(
            successor_request,
            symbol=sym,
            client_order_id=successor_cid,
            transport_kind="deadman",
        )
        and _owner_transport_request_valid(
            intent,
            symbol=sym,
            client_order_id=intent_cid,
            transport_kind="emergency_exit",
        )
        and str(intent.get("alpaca_account_id") or "").strip() == account_id
        and math.isfinite(request_qty)
        and request_qty > 0.0
        and math.isfinite(successor_qty)
        and abs(successor_qty - request_qty) <= tol
        and math.isfinite(intent_qty)
        and abs(intent_qty - request_qty) <= tol
        and math.isfinite(predecessor_fill)
        and predecessor_fill >= 0.0
        and math.isfinite(successor_fill)
        and successor_fill >= 0.0
    ):
        return {"ok": False, "reason": "replacement_containment_intent_not_certified"}
    evidence = {
        "identity_contract": "alpaca_replacement_containment_v1",
        "predecessor_client_order_id": predecessor_cid,
        "predecessor_broker_order_id": predecessor_oid,
        "predecessor_order_request": predecessor_request,
        "predecessor_reported_filled_size": predecessor_fill,
        "successor_client_order_id": successor_cid,
        "successor_broker_order_id": successor_oid,
        "successor_order_request": successor_request,
        "successor_replaces": predecessor_oid,
        "successor_broker_status_at_prepare": str(
            successor_broker_status or ""
        ).strip().lower(),
        "successor_broker_lifecycle_at_prepare": str(
            successor_broker_lifecycle or ""
        ).strip().lower(),
        "successor_reported_filled_size_at_prepare": successor_fill,
        "close_intent": intent,
    }
    evidence_id = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    evidence["containment_id"] = evidence_id
    readable, claim = read_action_claim(
        db, symbol=sym, account_scope=scope, for_update=True
    )
    if not readable or claim is None or not (
        claim.get("phase") != RESOLVED
        and claim.get("action") == "entry"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("owner_session_id") == int(owner_session_id)
    ):
        return {"ok": False, "reason": "replacement_containment_claim_unreadable"}
    metadata = dict(claim.get("metadata") or {})
    current = metadata.get(_OWNER_TRANSPORT_METADATA_KEY)
    current = dict(current) if isinstance(current, dict) else {}
    existing = metadata.get(_REPLACEMENT_LINEAGE_CONTAINMENT_KEY)
    existing = dict(existing) if isinstance(existing, dict) else {}
    if existing:
        if existing.get("containment_id") == evidence_id and existing.get("state") in {
            "prepared",
            "successor_ready",
        }:
            return {"ok": True, "reused": True, "containment": existing}
        return {"ok": False, "reason": "replacement_containment_generation_mismatch"}
    if not (
        str(current.get("transport_kind") or "").strip().lower() == "deadman"
        and str(current.get("phase") or "").strip().lower() != "resolved"
        and str(current.get("client_order_id") or "").strip() == predecessor_cid
        and str(current.get("broker_order_id") or "").strip() == predecessor_oid
        and current.get("order_request") == predecessor_request
    ):
        return {"ok": False, "reason": "replacement_containment_owner_mismatch"}
    prepared = {
        **evidence,
        "state": "prepared",
        "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    old_handoff = metadata.get(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY)
    old_handoff = dict(old_handoff) if isinstance(old_handoff, dict) else {}
    if old_handoff:
        handoff_history = metadata.get(_DEADMAN_CLOSE_HANDOFF_HISTORY_KEY)
        handoff_history = list(handoff_history) if isinstance(handoff_history, list) else []
        handoff_history.append(old_handoff)
        metadata[_DEADMAN_CLOSE_HANDOFF_HISTORY_KEY] = handoff_history[-20:]
    handoff = {
        "identity_contract": "alpaca_deadman_close_handoff_v1",
        "handoff_token": f"replacement-containment-{evidence_id[:24]}",
        "owner_session_id": int(owner_session_id),
        "alpaca_account_id": account_id,
        "symbol": sym,
        "phase": "replacement_lineage_containment_prepared",
        "deadman_client_order_id": predecessor_cid,
        "deadman_broker_order_id": predecessor_oid,
        "deadman_order_request": predecessor_request,
        "successor_transport_kind": "emergency_exit",
        "successor_client_order_id": intent_cid,
        "successor_intent": intent,
        "successor_order_request": None,
        "reason": "replacement_lineage_ambiguous_close_only_containment",
        "replacement_lineage_containment": prepared,
        "created_at_utc": prepared["prepared_at_utc"],
    }
    metadata[_REPLACEMENT_LINEAGE_CONTAINMENT_KEY] = prepared
    metadata[_DEADMAN_CLOSE_HANDOFF_METADATA_KEY] = handoff
    row = db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb),"
        " updated_at = NOW() WHERE account_scope = :scope AND symbol = :symbol"
        " AND claim_token = :token AND action = 'entry' AND phase <> 'resolved'"
    ), {
        "metadata": json.dumps(metadata, separators=(",", ":"), default=str),
        "scope": scope,
        "symbol": sym,
        "token": str(claim_token),
    })
    if int(row.rowcount or 0) != 1:
        return {"ok": False, "reason": "replacement_containment_prepare_write_failed"}
    return {"ok": True, "containment": prepared, "handoff": handoff}


def activate_deadman_replacement_containment(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    account_scope: str,
    alpaca_account_id: str,
    containment_id: str,
    predecessor_broker_lifecycle: str,
    successor_broker_status: str,
    successor_broker_lifecycle: str,
    predecessor_reported_filled_size: float,
    successor_reported_filled_size: float,
    broker_remaining_quantity: float,
) -> dict[str, Any]:
    """After fresh two-sided inert proof, activate the exact-B close outbox."""
    scope = str(account_scope or "").strip().lower()
    sym = _symbol(symbol)
    account_id = str(alpaca_account_id or "").strip()
    cid = str(containment_id or "").strip()
    predecessor_lifecycle = str(predecessor_broker_lifecycle or "").strip().lower()
    successor_status = str(successor_broker_status or "").strip().lower()
    successor_lifecycle = str(successor_broker_lifecycle or "").strip().lower()
    try:
        predecessor_fill = float(predecessor_reported_filled_size)
        successor_fill = float(successor_reported_filled_size)
        remaining = float(broker_remaining_quantity)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "replacement_containment_terminal_truth_invalid"}
    if not (
        scope == "alpaca:paper"
        and account_id
        and cid
        and predecessor_lifecycle == "replaced"
        and _terminal_order_status_lifecycle_compatible(
            successor_status,
            successor_lifecycle,
        )
        and math.isfinite(predecessor_fill)
        and predecessor_fill >= 0.0
        and math.isfinite(successor_fill)
        and successor_fill >= 0.0
        and math.isfinite(remaining)
        and remaining >= 0.0
    ):
        return {"ok": False, "reason": "replacement_containment_terminal_truth_invalid"}
    readable, claim = read_action_claim(
        db, symbol=sym, account_scope=scope, for_update=True
    )
    if not readable or claim is None or not (
        claim.get("phase") != RESOLVED
        and claim.get("action") == "entry"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("owner_session_id") == int(owner_session_id)
    ):
        return {"ok": False, "reason": "replacement_containment_claim_unreadable"}
    metadata = dict(claim.get("metadata") or {})
    prepared = metadata.get(_REPLACEMENT_LINEAGE_CONTAINMENT_KEY)
    prepared = dict(prepared) if isinstance(prepared, dict) else {}
    handoff = metadata.get(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY)
    handoff = dict(handoff) if isinstance(handoff, dict) else {}
    current = metadata.get(_OWNER_TRANSPORT_METADATA_KEY)
    current = dict(current) if isinstance(current, dict) else {}
    if prepared.get("containment_id") != cid:
        return {"ok": False, "reason": "replacement_containment_generation_mismatch"}
    if prepared.get("state") == "successor_ready":
        quarantines = metadata.get(
            _PROTECTIVE_ATTRIBUTION_QUARANTINE_LEDGER_KEY
        )
        quarantines = quarantines if isinstance(quarantines, list) else []
        exact_rows = [
            row
            for row in quarantines
            if isinstance(row, dict) and row.get("containment_id") == cid
        ]
        try:
            replay_exact = bool(
                len(exact_rows) == 1
                and str(
                    exact_rows[0].get("predecessor_broker_lifecycle_after")
                    or ""
                ).strip().lower()
                == predecessor_lifecycle
                and str(
                    exact_rows[0].get("successor_broker_status_after") or ""
                ).strip().lower()
                == successor_status
                and str(
                    exact_rows[0].get("successor_broker_lifecycle_after")
                    or ""
                ).strip().lower()
                == successor_lifecycle
                and abs(
                    float(
                        exact_rows[0].get(
                            "predecessor_reported_filled_size_after"
                        )
                    )
                    - predecessor_fill
                )
                <= max(1e-9, abs(predecessor_fill) * 1e-8)
                and abs(
                    float(
                        exact_rows[0].get(
                            "successor_reported_filled_size_after"
                        )
                    )
                    - successor_fill
                )
                <= max(1e-9, abs(successor_fill) * 1e-8)
                and abs(
                    float(exact_rows[0].get("broker_remaining_quantity"))
                    - remaining
                )
                <= max(1e-9, abs(remaining) * 1e-8)
            )
        except (TypeError, ValueError):
            replay_exact = False
        if not replay_exact:
            return {
                "ok": False,
                "reason": "replacement_containment_replay_truth_mismatch",
            }
        return {
            "ok": True,
            "reused": True,
            "containment": prepared,
            "handoff": handoff,
        }
    predecessor_request = prepared.get("predecessor_order_request")
    predecessor_request = predecessor_request if isinstance(predecessor_request, dict) else {}
    intent = prepared.get("close_intent")
    intent = dict(intent) if isinstance(intent, dict) else {}
    try:
        requested = float(predecessor_request.get("base_size"))
    except (TypeError, ValueError):
        requested = math.nan
    tol = max(1e-9, abs(requested) * 1e-8) if math.isfinite(requested) else 1e-9
    if not (
        prepared.get("state") == "prepared"
        and handoff.get("phase") == "replacement_lineage_containment_prepared"
        and handoff.get("replacement_lineage_containment") == prepared
        and str(current.get("client_order_id") or "").strip()
        == str(prepared.get("predecessor_client_order_id") or "").strip()
        and str(current.get("broker_order_id") or "").strip()
        == str(prepared.get("predecessor_broker_order_id") or "").strip()
        and current.get("order_request") == predecessor_request
        and abs(
            predecessor_fill
            - float(prepared.get("predecessor_reported_filled_size"))
        )
        <= tol
        and remaining <= requested + tol
    ):
        return {"ok": False, "reason": "replacement_containment_owner_mismatch"}
    quarantine = {
        **prepared,
        "identity_contract": "alpaca_replacement_attribution_quarantine_v1",
        "predecessor_broker_lifecycle_after": predecessor_lifecycle,
        "predecessor_reported_filled_size_after": predecessor_fill,
        "successor_broker_status_after": successor_status,
        "successor_broker_lifecycle_after": successor_lifecycle,
        "successor_reported_filled_size_after": successor_fill,
        "broker_remaining_quantity": remaining,
        "quantity_delta_quarantined": max(0.0, requested - remaining),
        "activated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    quarantines = metadata.get(_PROTECTIVE_ATTRIBUTION_QUARANTINE_LEDGER_KEY)
    quarantines = list(quarantines) if isinstance(quarantines, list) else []
    if not any(
        isinstance(row, dict) and row.get("containment_id") == cid
        for row in quarantines
    ):
        quarantines.append(quarantine)
    metadata[_PROTECTIVE_ATTRIBUTION_QUARANTINE_LEDGER_KEY] = quarantines
    resolved_current = {
        **current,
        "phase": "resolved",
        "broker_order_status": "replaced",
        "broker_order_lifecycle": "replaced",
        "filled_size": 0.0,
        "remaining_quantity": remaining,
        "fill_attribution_quarantined": True,
        "replacement_lineage_containment": quarantine,
        "resolved_at_utc": quarantine["activated_at_utc"],
    }
    history = metadata.get(_OWNER_TRANSPORT_HISTORY_KEY)
    history = list(history) if isinstance(history, list) else []
    history.append(resolved_current)
    metadata[_OWNER_TRANSPORT_HISTORY_KEY] = history[-20:]
    metadata[_OWNER_TRANSPORT_METADATA_KEY] = resolved_current
    prepared = {**prepared, "state": "successor_ready", "broker_remaining_quantity": remaining}
    metadata[_REPLACEMENT_LINEAGE_CONTAINMENT_KEY] = prepared
    if remaining <= 1e-9:
        handoff_history = metadata.get(_DEADMAN_CLOSE_HANDOFF_HISTORY_KEY)
        handoff_history = list(handoff_history) if isinstance(handoff_history, list) else []
        handoff_history.append({**handoff, "phase": "replacement_lineage_flat_quarantined"})
        metadata[_DEADMAN_CLOSE_HANDOFF_HISTORY_KEY] = handoff_history[-20:]
        metadata.pop(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY, None)
        activated_handoff = None
    else:
        final_request = {**intent, "base_size": _format_base_size(remaining)}
        handoff.update({
            "phase": "successor_ready",
            "successor_order_request": final_request,
            "replacement_lineage_containment": prepared,
            "deadman_terminal_status": "replaced",
            "deadman_terminal_filled_size": 0.0,
            "broker_remaining_quantity": remaining,
        })
        metadata[_DEADMAN_CLOSE_HANDOFF_METADATA_KEY] = handoff
        activated_handoff = handoff
    row = db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb),"
        " updated_at = NOW() WHERE account_scope = :scope AND symbol = :symbol"
        " AND claim_token = :token AND action = 'entry' AND phase <> 'resolved'"
    ), {
        "metadata": json.dumps(metadata, separators=(",", ":"), default=str),
        "scope": scope,
        "symbol": sym,
        "token": str(claim_token),
    })
    if int(row.rowcount or 0) != 1:
        return {"ok": False, "reason": "replacement_containment_activate_write_failed"}
    return {
        "ok": True,
        "containment": prepared,
        "quarantine": quarantine,
        "handoff": activated_handoff,
        "broker_flat": remaining <= 1e-9,
    }


def retire_deadman_handoff_reprotected(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    account_scope: str,
    alpaca_account_id: str,
    client_order_id: str,
    broker_order_id: str,
    broker_order_status: str,
    broker_order_lifecycle: str,
) -> bool:
    """Archive certified replacement lineage only after committed fill replay."""
    scope = str(account_scope or "").strip().lower()
    sym = _symbol(symbol)
    cid = str(client_order_id or "").strip()
    oid = str(broker_order_id or "").strip()
    status = str(broker_order_status or "").strip().lower()
    lifecycle = str(broker_order_lifecycle or "").strip().lower()
    if (
        not cid
        or not oid
        or status in _TERMINAL_ORDER_STATUSES
        or lifecycle not in _ACTIVE_ALPACA_PROTECTIVE_LIFECYCLES
    ):
        return False
    readable, claim = read_action_claim(
        db,
        symbol=sym,
        account_scope=scope,
        for_update=True,
    )
    if not readable or claim is None or not (
        claim.get("phase") != RESOLVED
        and claim.get("action") == "entry"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("owner_session_id") == int(owner_session_id)
    ):
        return False
    metadata = dict(claim.get("metadata") or {})
    handoff = metadata.get(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY)
    handoff = dict(handoff) if isinstance(handoff, dict) else {}
    current = metadata.get(_OWNER_TRANSPORT_METADATA_KEY)
    current = dict(current) if isinstance(current, dict) else {}
    request = current.get("order_request")
    request = request if isinstance(request, dict) else {}
    try:
        replacement_qty = float(request.get("base_size"))
    except (TypeError, ValueError):
        replacement_qty = math.nan
    if not (
        handoff.get("identity_contract") == "alpaca_deadman_close_handoff_v1"
        and handoff.get("phase") == "replacement_deadman_active"
        and str(handoff.get("alpaca_account_id") or "").strip()
        == str(alpaca_account_id or "").strip()
        and str(handoff.get("replacement_deadman_client_order_id") or "").strip()
        == cid
        and str(handoff.get("replacement_deadman_broker_order_id") or "").strip()
        == oid
        and str(current.get("transport_kind") or "").strip().lower() == "deadman"
        and str(current.get("phase") or "").strip().lower() == "submitted"
        and str(current.get("client_order_id") or "").strip() == cid
        and str(current.get("broker_order_id") or "").strip() == oid
        and request == handoff.get("replacement_deadman_order_request")
        and math.isfinite(replacement_qty)
        and replacement_qty > 0.0
    ):
        return False

    row = db.execute(text(
        "SELECT risk_snapshot_json FROM trading_automation_sessions"
        " WHERE id = :session_id"
    ), {"session_id": int(owner_session_id)}).fetchone()
    snapshot = row[0] if row is not None and isinstance(row[0], dict) else {}
    live = snapshot.get("momentum_live_execution")
    live = live if isinstance(live, dict) else {}
    position = live.get("position")
    position = position if isinstance(position, dict) else {}
    markers = live.get("deadman_applied_fill_watermarks")
    markers = markers if isinstance(markers, list) else []
    try:
        local_qty = float(position.get("quantity"))
    except (TypeError, ValueError):
        local_qty = math.nan
    if not (
        math.isfinite(local_qty)
        and abs(local_qty - replacement_qty)
        <= max(1e-9, replacement_qty * 1e-8)
    ):
        return False
    lineage = handoff.get("protective_terminal_generations")
    lineage = lineage if isinstance(lineage, list) else []
    for generation in lineage:
        if not isinstance(generation, dict):
            return False
        try:
            filled = float(generation.get("filled_size"))
            remaining = float(generation.get("remaining_quantity"))
        except (TypeError, ValueError):
            return False
        if not (
            math.isfinite(filled)
            and filled >= 0.0
            and math.isfinite(remaining)
            and remaining >= 0.0
        ):
            return False
        if filled <= 1e-12:
            continue
        exact = []
        for marker in markers:
            if not isinstance(marker, dict):
                continue
            marker_transport = marker.get("owner_transport")
            marker_transport = (
                marker_transport if isinstance(marker_transport, dict) else {}
            )
            try:
                marker_fill = float(marker.get("applied_filled_size"))
                marker_remaining = float(marker.get("broker_remaining_quantity"))
            except (TypeError, ValueError):
                continue
            if (
                str(marker.get("client_order_id") or "").strip()
                == str(generation.get("client_order_id") or "").strip()
                and str(marker.get("order_id") or "").strip()
                == str(generation.get("broker_order_id") or "").strip()
                and marker_transport.get("order_request")
                == generation.get("order_request")
                and abs(marker_fill - filled) <= max(1e-9, abs(filled) * 1e-8)
                and abs(marker_remaining - remaining)
                <= max(1e-9, abs(remaining) * 1e-8)
            ):
                exact.append(marker)
        if len(exact) != 1:
            return False

    retired = {
        **handoff,
        "phase": "retired",
        "retirement_outcome": "replacement_deadman_active_lineage_replayed",
        "retired_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    history = metadata.get(_DEADMAN_CLOSE_HANDOFF_HISTORY_KEY)
    history = list(history) if isinstance(history, list) else []
    history.append(retired)
    metadata[_DEADMAN_CLOSE_HANDOFF_HISTORY_KEY] = history[-20:]
    metadata.pop(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY, None)
    result = db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb),"
        " updated_at = NOW() WHERE account_scope = :scope AND symbol = :symbol"
        " AND claim_token = :token AND action = 'entry' AND phase <> 'resolved'"
    ), {
        "metadata": json.dumps(metadata, separators=(",", ":"), default=str),
        "scope": scope,
        "symbol": sym,
        "token": str(claim_token),
    })
    return int(result.rowcount or 0) == 1


def lease_owner_transport(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    transport_kind: str,
    client_order_id: str,
    order_request: dict[str, Any],
    lease_token: str,
    account_scope: str,
    alpaca_account_id: str,
    strict_cid_absent_after_expiry: bool = False,
    lease_seconds: int = _OWNER_TRANSPORT_LEASE_SECONDS,
) -> dict[str, Any]:
    """Atomically freeze one owner close/protection POST on the retained entry claim.

    A second process never receives permission for a different CID while a transport
    is unresolved.  Once the lease expires, an explicit strict-CID 404 may only renew
    the *same* immutable request/CID.  Thus a paused original worker and its recovery
    worker can at worst replay one Alpaca-idempotent instruction, never two orders.
    """
    scope = str(account_scope or "").strip().lower()
    sym = _symbol(symbol)
    token = str(claim_token or "").strip()
    cid = str(client_order_id or "").strip()
    worker_token = str(lease_token or "").strip()
    kind = str(transport_kind or "").strip().lower()
    request = dict(order_request or {})
    account_id = str(alpaca_account_id or "").strip()
    if (
        scope != "alpaca:paper"
        or not bool(getattr(settings, "chili_alpaca_paper", True))
        or not token
        or not account_id
        or not worker_token
        or not _owner_transport_request_valid(
            request,
            symbol=sym,
            client_order_id=cid,
            transport_kind=kind,
        )
    ):
        return {"ok": False, "reason": "owner_transport_request_not_certified"}
    now = datetime.now(timezone.utc)
    lease_expires = now + timedelta(seconds=max(30, int(lease_seconds)))
    readable, claim = read_action_claim(
        db,
        symbol=sym,
        account_scope=scope,
        for_update=True,
    )
    if not readable or claim is None:
        return {"ok": False, "reason": "owner_transport_claim_unreadable"}
    if not (
        claim.get("phase") != RESOLVED
        and claim.get("action") == "entry"
        and claim.get("claim_token") == token
        and claim.get("owner_session_id") == int(owner_session_id)
    ):
        return {"ok": False, "reason": "owner_transport_entry_owner_mismatch"}
    metadata = dict(claim.get("metadata") or {})
    frozen_entry_request = metadata.get("order_request")
    frozen_entry_request = (
        frozen_entry_request if isinstance(frozen_entry_request, dict) else {}
    )
    frozen_claim_account_id = str(
        metadata.get("alpaca_account_id")
        or frozen_entry_request.get("alpaca_account_id")
        or ""
    ).strip()
    if (
        frozen_claim_account_id != account_id
        or str(request.get("alpaca_account_id") or "").strip() != account_id
    ):
        return {"ok": False, "reason": "alpaca_account_generation_mismatch"}
    current = metadata.get(_OWNER_TRANSPORT_METADATA_KEY)
    current = dict(current) if isinstance(current, dict) else None
    handoff = metadata.get(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY)
    handoff = dict(handoff) if isinstance(handoff, dict) else None
    if kind == "deadman" and (
        current is None or str(current.get("phase") or "").strip().lower() == "resolved"
    ):
        # A resolved Alpaca CID is immutable broker history, never a reusable
        # generation.  The runner's local counter may roll back after this claim
        # commits; reject that stale ordinal at the durable permit as a second
        # line of defense.
        used_deadman_cids: set[str] = set()
        owner_rows: list[Any] = [current]
        owner_history = metadata.get(_OWNER_TRANSPORT_HISTORY_KEY)
        if isinstance(owner_history, list):
            owner_rows.extend(owner_history)
        protective_ledger = metadata.get(_PROTECTIVE_TERMINAL_LEDGER_KEY)
        if isinstance(protective_ledger, list):
            owner_rows.extend(protective_ledger)
        for candidate in owner_rows:
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("transport_kind") or "").strip().lower() != "deadman":
                continue
            historical_cid = str(
                candidate.get("client_order_id") or ""
            ).strip()
            if historical_cid:
                used_deadman_cids.add(historical_cid)
        if cid in used_deadman_cids:
            return {
                "ok": False,
                "reason": "owner_transport_client_order_id_reused",
                "transport": current,
            }
    if handoff is not None:
        if kind == "deadman":
            return {
                "ok": False,
                "reason": "deadman_close_handoff_requires_resolution",
                "handoff": handoff,
                "transport": current,
            }
        if not (
            handoff.get("identity_contract") == "alpaca_deadman_close_handoff_v1"
            and handoff.get("phase") in {
                "successor_ready",
                "successor_leased",
                "successor_submitted",
                "successor_submit_indeterminate",
            }
            and str(handoff.get("successor_transport_kind") or "").strip().lower()
            == kind
            and str(handoff.get("successor_client_order_id") or "").strip() == cid
            and handoff.get("successor_order_request") == request
        ):
            return {
                "ok": False,
                "reason": "deadman_close_handoff_successor_mismatch",
                "handoff": handoff,
                "transport": current,
            }
        # Before the resolved old stop is replaced by its close successor, keep
        # its exact cumulative-fill lineage in the handoff.  A child POST may be
        # independently committed while local PnL rolls back; restart must replay
        # this predecessor before it services the child.
        handoff = _append_protective_terminal_generation(handoff, current)
    if current and str(current.get("phase") or "") != "resolved":
        same_identity = bool(
            str(current.get("transport_kind") or "").strip().lower() == kind
            and str(current.get("client_order_id") or "").strip() == cid
            and current.get("order_request") == request
        )
        expiry = _parse_utc(current.get("lease_expires_at_utc"))
        expired = bool(expiry is not None and expiry <= now)
        if not (
            same_identity
            and strict_cid_absent_after_expiry
            and expired
        ):
            return {
                "ok": False,
                "reason": (
                    "owner_transport_reconcile_required"
                    if same_identity or expired
                    else "owner_transport_leased"
                ),
                "transport": current,
                "lease_expired": expired,
            }
        # The strict lookup was an explicit 404 after the old lease expired. Keep
        # the exact CID/request and only fence a same-CID replay under a new token.
        current.update({
            "phase": "submitting",
            "lease_token": worker_token,
            "lease_expires_at_utc": lease_expires.isoformat(),
            "updated_at_utc": now.isoformat(),
            "same_cid_replay_count": int(current.get("same_cid_replay_count") or 0) + 1,
        })
        metadata[_OWNER_TRANSPORT_METADATA_KEY] = current
        replay = True
    else:
        if kind == "deadman":
            generation = _deadman_client_order_generation(
                cid,
                owner_session_id=owner_session_id,
            )
            high_watermark = _durable_deadman_generation_high_watermark(
                metadata,
                owner_session_id=owner_session_id,
            )
            if (
                str(cid).startswith("chili_dm_")
                and generation is None
            ) or (
                generation is not None and generation <= high_watermark
            ):
                return {
                    "ok": False,
                    "reason": "deadman_generation_not_monotonic",
                    "deadman_generation_high_watermark": high_watermark,
                }
            if generation is not None:
                metadata[_DEADMAN_GENERATION_HIGH_WATERMARK_KEY] = generation
        current = {
            "identity_contract": "alpaca_owner_transport_v1",
            "transport_kind": kind,
            "client_order_id": cid,
            "order_request": request,
            "phase": "submitting",
            "lease_token": worker_token,
            "lease_expires_at_utc": lease_expires.isoformat(),
            "created_at_utc": now.isoformat(),
            "updated_at_utc": now.isoformat(),
        }
        metadata[_OWNER_TRANSPORT_METADATA_KEY] = current
        replay = False
    if handoff is not None:
        handoff.update({
            "phase": "successor_leased",
            "successor_lease_token": worker_token,
            "successor_lease_expires_at_utc": lease_expires.isoformat(),
            "successor_leased_at_utc": str(
                handoff.get("successor_leased_at_utc") or now.isoformat()
            ),
        })
        metadata[_DEADMAN_CLOSE_HANDOFF_METADATA_KEY] = handoff
    row = db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb), "
        " updated_at = :now WHERE account_scope = :scope AND symbol = :symbol "
        " AND claim_token = :token AND action = 'entry' AND phase <> 'resolved'"
    ), {
        "metadata": json.dumps(metadata, separators=(",", ":"), default=str),
        "now": now,
        "scope": scope,
        "symbol": sym,
        "token": token,
    })
    if int(row.rowcount or 0) != 1:
        return {"ok": False, "reason": "owner_transport_lease_write_failed"}
    return {"ok": True, "transport": current, "same_cid_replay": replay}


def advance_owner_transport(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    client_order_id: str,
    lease_token: str,
    phase: str,
    broker_order_id: str | None,
    metadata: dict[str, Any] | None = None,
    account_scope: str,
    alpaca_account_id: str,
) -> bool:
    """Advance only the exact fenced transport worker after the adapter seam."""
    if phase not in {"submitted", "submit_indeterminate"}:
        return False
    scope = str(account_scope or "").strip().lower()
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope=scope,
        for_update=True,
    )
    if not readable or claim is None or not (
        claim.get("phase") != RESOLVED
        and claim.get("action") == "entry"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("owner_session_id") == int(owner_session_id)
    ):
        return False
    claim_meta = dict(claim.get("metadata") or {})
    current = claim_meta.get(_OWNER_TRANSPORT_METADATA_KEY)
    current_phase = str((current or {}).get("phase") or "").strip().lower()
    phase_transition_ok = bool(
        current_phase == "submitting"
        or (
            current_phase == "submit_indeterminate"
            and phase == "submitted"
            and bool(str(broker_order_id or "").strip())
        )
    )
    if not isinstance(current, dict) or not (
        str(current.get("client_order_id") or "") == str(client_order_id)
        and str(current.get("lease_token") or "") == str(lease_token)
        and phase_transition_ok
        and str((current.get("order_request") or {}).get("alpaca_account_id") or "").strip()
        == str(alpaca_account_id or "").strip()
    ):
        return False
    current = {
        **dict(current),
        "phase": phase,
        "broker_order_id": str(broker_order_id or "") or None,
        "transport_result": dict(metadata or {}),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    claim_meta[_OWNER_TRANSPORT_METADATA_KEY] = current
    handoff = claim_meta.get(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY)
    if isinstance(handoff, dict) and (
        str(handoff.get("successor_client_order_id") or "").strip()
        == str(client_order_id or "").strip()
        and str(handoff.get("successor_transport_kind") or "").strip().lower()
        == str(current.get("transport_kind") or "").strip().lower()
        and handoff.get("successor_order_request") == current.get("order_request")
    ):
        handoff = {
            **dict(handoff),
            "phase": (
                "successor_submitted"
                if phase == "submitted"
                else "successor_submit_indeterminate"
            ),
            "successor_broker_order_id": str(broker_order_id or "") or None,
            "successor_advanced_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        claim_meta[_DEADMAN_CLOSE_HANDOFF_METADATA_KEY] = handoff
    elif isinstance(handoff, dict) and (
        str(handoff.get("replacement_deadman_client_order_id") or "").strip()
        == str(client_order_id or "").strip()
        and handoff.get("replacement_deadman_order_request")
        == current.get("order_request")
        and str(current.get("transport_kind") or "").strip().lower() == "deadman"
    ):
        handoff = {
            **dict(handoff),
            "phase": (
                "replacement_deadman_submitted"
                if phase == "submitted"
                else "replacement_deadman_submit_indeterminate"
            ),
            "replacement_deadman_broker_order_id": str(broker_order_id or "") or None,
            "replacement_deadman_advanced_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        claim_meta[_DEADMAN_CLOSE_HANDOFF_METADATA_KEY] = handoff
    result = db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb), "
        " updated_at = NOW() WHERE account_scope = :scope AND symbol = :symbol "
        " AND claim_token = :token AND action = 'entry' AND phase <> 'resolved'"
    ), {
        "metadata": json.dumps(claim_meta, separators=(",", ":"), default=str),
        "scope": scope,
        "symbol": _symbol(symbol),
        "token": str(claim_token),
    })
    return int(result.rowcount or 0) == 1


def resolve_owner_transport_terminal(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    client_order_id: str,
    broker_order_id: str,
    broker_order_status: str,
    filled_size: float,
    account_scope: str,
    alpaca_account_id: str,
    remaining_quantity: float | None = None,
    pre_accept_rejected: bool = False,
    lease_token: str | None = None,
) -> bool:
    """Release only an exact terminal transport; never resolves entry ownership."""
    status = str(broker_order_status or "").strip().lower()
    cid = str(client_order_id or "").strip()
    oid = str(broker_order_id or "").strip()
    try:
        filled = float(filled_size)
        remaining = None if remaining_quantity is None else float(remaining_quantity)
    except (TypeError, ValueError):
        return False
    if (
        status not in _TERMINAL_ORDER_STATUSES
        or not cid
        or (not oid and not (pre_accept_rejected and status in {"rejected", "failed"}))
        or not math.isfinite(filled)
        or filled < 0.0
        or (
            filled > 1e-12
            and (remaining is None or not math.isfinite(remaining) or remaining < 0.0)
        )
    ):
        return False
    scope = str(account_scope or "").strip().lower()
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope=scope,
        for_update=True,
    )
    if not readable or claim is None or not (
        claim.get("phase") != RESOLVED
        and claim.get("action") == "entry"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("owner_session_id") == int(owner_session_id)
    ):
        return False
    claim_meta = dict(claim.get("metadata") or {})
    current = claim_meta.get(_OWNER_TRANSPORT_METADATA_KEY)
    try:
        replay_count = int((current or {}).get("same_cid_replay_count") or 0)
    except (TypeError, ValueError):
        return False
    if not isinstance(current, dict) or not (
        str(current.get("client_order_id") or "").strip() == cid
        and str((current.get("order_request") or {}).get("alpaca_account_id") or "").strip()
        == str(alpaca_account_id or "").strip()
        and (
            (oid and str(current.get("broker_order_id") or oid).strip() == oid)
            or (
                not oid
                and pre_accept_rejected
                and not str(current.get("broker_order_id") or "").strip()
                and replay_count == 0
            )
        )
        and (
            not pre_accept_rejected
            or (
                current.get("phase") == "submitting"
                and str(current.get("lease_token") or "").strip()
                == str(lease_token or "").strip()
                and bool(str(lease_token or "").strip())
            )
        )
    ):
        return False
    resolved = {
        **dict(current),
        "phase": "resolved",
        "broker_order_id": oid,
        "broker_order_status": status,
        "filled_size": filled,
        "remaining_quantity": remaining,
        "pre_accept_rejected": bool(pre_accept_rejected),
        "resolved_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    history = claim_meta.get(_OWNER_TRANSPORT_HISTORY_KEY)
    history = list(history) if isinstance(history, list) else []
    history.append(resolved)
    claim_meta[_OWNER_TRANSPORT_HISTORY_KEY] = history[-20:]
    if (
        str(current.get("transport_kind") or "").strip().lower() == "deadman"
        and oid
    ):
        # Non-evictable predecessor ledger.  The rolling audit history may stay
        # bounded, but an outer local transaction can roll back after arbitrarily
        # many terminal children; no positive-fill generation may disappear
        # before its exact local watermark is replayed.
        protective_ledger = claim_meta.get(_PROTECTIVE_TERMINAL_LEDGER_KEY)
        protective_ledger = (
            list(protective_ledger)
            if isinstance(protective_ledger, list)
            else []
        )
        same_identity = [
            row
            for row in protective_ledger
            if isinstance(row, dict)
            and str(row.get("client_order_id") or "").strip() == cid
            and str(row.get("broker_order_id") or "").strip() == oid
        ]
        if same_identity and not all(row == resolved for row in same_identity):
            return False
        if not same_identity:
            protective_ledger.append(dict(resolved))
        claim_meta[_PROTECTIVE_TERMINAL_LEDGER_KEY] = protective_ledger
    claim_meta[_OWNER_TRANSPORT_METADATA_KEY] = resolved
    handoff = claim_meta.get(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY)
    if isinstance(handoff, dict):
        if (
            str(current.get("transport_kind") or "").strip().lower() == "deadman"
            and str(handoff.get("deadman_client_order_id") or "").strip() == cid
            and handoff.get("deadman_order_request") == current.get("order_request")
        ):
            handoff = {
                **dict(handoff),
                "phase": "deadman_terminal",
                "deadman_terminal_status": status,
                "deadman_terminal_filled_size": filled,
                "deadman_broker_remaining_quantity": remaining,
                "deadman_terminal_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            claim_meta[_DEADMAN_CLOSE_HANDOFF_METADATA_KEY] = handoff
        elif (
            str(handoff.get("successor_client_order_id") or "").strip() == cid
            and handoff.get("successor_order_request") == current.get("order_request")
        ):
            handoff = {
                **dict(handoff),
                "phase": (
                    "successor_proven_no_transport"
                    if pre_accept_rejected
                    else "successor_terminal"
                ),
                "successor_terminal_status": status,
                "successor_terminal_filled_size": filled,
                "successor_broker_remaining_quantity": remaining,
                "successor_terminal_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            if pre_accept_rejected:
                handoff["successor_no_transport_reason"] = "pre_accept_rejected"
                handoff["successor_no_transport_at_utc"] = datetime.now(
                    timezone.utc
                ).isoformat()
            claim_meta[_DEADMAN_CLOSE_HANDOFF_METADATA_KEY] = handoff
        elif (
            str(handoff.get("replacement_deadman_client_order_id") or "").strip()
            == cid
            and handoff.get("replacement_deadman_order_request")
            == current.get("order_request")
            and str(current.get("transport_kind") or "").strip().lower() == "deadman"
        ):
            handoff = {
                **dict(handoff),
                "phase": (
                    "replacement_deadman_proven_no_transport"
                    if pre_accept_rejected
                    else "replacement_deadman_terminal"
                ),
                "replacement_deadman_terminal_status": status,
                "replacement_deadman_terminal_filled_size": filled,
                "replacement_deadman_broker_remaining_quantity": remaining,
                "replacement_deadman_terminal_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            claim_meta[_DEADMAN_CLOSE_HANDOFF_METADATA_KEY] = handoff
    result = db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb), "
        " updated_at = NOW() WHERE account_scope = :scope AND symbol = :symbol "
        " AND claim_token = :token AND action = 'entry' AND phase <> 'resolved'"
    ), {
        "metadata": json.dumps(claim_meta, separators=(",", ":"), default=str),
        "scope": scope,
        "symbol": _symbol(symbol),
        "token": str(claim_token),
    })
    return int(result.rowcount or 0) == 1


def release_owner_transport_pre_post(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    client_order_id: str,
    lease_token: str,
    account_scope: str,
    alpaca_account_id: str,
    reason: str,
) -> bool:
    """Release this worker's exact lease only when caller proves no POST occurred."""
    scope = str(account_scope or "").strip().lower()
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope=scope,
        for_update=True,
    )
    if not readable or claim is None or not (
        claim.get("phase") != RESOLVED
        and claim.get("action") == "entry"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("owner_session_id") == int(owner_session_id)
    ):
        return False
    claim_meta = dict(claim.get("metadata") or {})
    current = claim_meta.get(_OWNER_TRANSPORT_METADATA_KEY)
    try:
        replay_count = int((current or {}).get("same_cid_replay_count") or 0)
    except (TypeError, ValueError):
        return False
    if not isinstance(current, dict) or not (
        current.get("phase") == "submitting"
        and not str(current.get("broker_order_id") or "").strip()
        and replay_count == 0
        and str(current.get("client_order_id") or "").strip()
        == str(client_order_id or "").strip()
        and str(current.get("lease_token") or "").strip()
        == str(lease_token or "").strip()
        and str((current.get("order_request") or {}).get("alpaca_account_id") or "").strip()
        == str(alpaca_account_id or "").strip()
    ):
        return False
    resolved = {
        **dict(current),
        "phase": "resolved",
        "proven_no_transport": True,
        "no_transport_reason": str(reason or "pre_post_block"),
        "resolved_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    history = claim_meta.get(_OWNER_TRANSPORT_HISTORY_KEY)
    history = list(history) if isinstance(history, list) else []
    history.append(resolved)
    claim_meta[_OWNER_TRANSPORT_HISTORY_KEY] = history[-20:]
    claim_meta[_OWNER_TRANSPORT_METADATA_KEY] = resolved
    handoff = claim_meta.get(_DEADMAN_CLOSE_HANDOFF_METADATA_KEY)
    if isinstance(handoff, dict) and (
        str(handoff.get("successor_client_order_id") or "").strip()
        == str(client_order_id or "").strip()
        and handoff.get("successor_order_request") == current.get("order_request")
    ):
        handoff = {
            **dict(handoff),
            "phase": "successor_proven_no_transport",
            "successor_no_transport_reason": str(reason or "pre_post_block"),
            "successor_no_transport_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        claim_meta[_DEADMAN_CLOSE_HANDOFF_METADATA_KEY] = handoff
    elif isinstance(handoff, dict) and (
        str(handoff.get("replacement_deadman_client_order_id") or "").strip()
        == str(client_order_id or "").strip()
        and handoff.get("replacement_deadman_order_request")
        == current.get("order_request")
        and str(current.get("transport_kind") or "").strip().lower() == "deadman"
    ):
        handoff = {
            **dict(handoff),
            "phase": "replacement_deadman_proven_no_transport",
            "replacement_deadman_no_transport_reason": str(
                reason or "pre_post_block"
            ),
            "replacement_deadman_no_transport_at_utc": datetime.now(
                timezone.utc
            ).isoformat(),
        }
        claim_meta[_DEADMAN_CLOSE_HANDOFF_METADATA_KEY] = handoff
    result = db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb), "
        " updated_at = NOW() WHERE account_scope = :scope AND symbol = :symbol "
        " AND claim_token = :token AND action = 'entry' AND phase <> 'resolved'"
    ), {
        "metadata": json.dumps(claim_meta, separators=(",", ":"), default=str),
        "scope": scope,
        "symbol": _symbol(symbol),
        "token": str(claim_token),
    })
    return int(result.rowcount or 0) == 1


def _symbol(value: str) -> str:
    return str(value or "").strip().upper()


def _row_to_claim(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "account_scope": str(row[0]),
        "symbol": str(row[1]),
        "claim_token": str(row[2]),
        "action": str(row[3]),
        "phase": str(row[4]),
        "owner_session_id": int(row[5]) if row[5] is not None else None,
        "client_order_id": str(row[6]) if row[6] else None,
        "broker_order_id": str(row[7]) if row[7] else None,
        "metadata": dict(row[8]) if isinstance(row[8], dict) else {},
        "claimed_at": row[9],
        "updated_at": row[10],
        "lease_expires_at": row[11],
        "resolved_at": row[12],
    }


_CLAIM_COLUMNS = (
    "account_scope, symbol, claim_token, action, phase, owner_session_id, "
    "client_order_id, broker_order_id, metadata_json, claimed_at, updated_at, "
    "lease_expires_at, resolved_at"
)


def read_action_claim(
    db: Session,
    *,
    symbol: str,
    account_scope: str | None = None,
    for_update: bool = False,
) -> tuple[bool, dict[str, Any] | None]:
    """Read the one account/symbol permit; DB uncertainty is explicit."""
    scope = account_scope or alpaca_account_scope()
    try:
        suffix = " FOR UPDATE" if for_update else ""
        row = db.execute(text(
            f"SELECT {_CLAIM_COLUMNS} FROM broker_symbol_action_claims "
            "WHERE account_scope = :scope AND symbol = :symbol" + suffix
        ), {"scope": scope, "symbol": _symbol(symbol)}).fetchone()
        return True, _row_to_claim(row)
    except Exception:
        _log.warning(
            "[alpaca_claim] claim read failed scope=%s symbol=%s",
            scope,
            symbol,
            exc_info=True,
        )
        return False, None


def list_unresolved_action_claims(
    db: Session,
    *,
    action: str | None = None,
    account_scope: str | None = None,
) -> tuple[bool, list[dict[str, Any]]]:
    """Detached unresolved permits for recovery sweeps; uncertainty is explicit."""
    scope = account_scope or alpaca_account_scope()
    try:
        rows = db.execute(text(
            f"SELECT {_CLAIM_COLUMNS} FROM broker_symbol_action_claims "
            "WHERE account_scope = :scope AND phase <> 'resolved' "
            "  AND (:action IS NULL OR action = :action) "
            "ORDER BY updated_at ASC"
        ), {"scope": scope, "action": action}).fetchall()
        return True, [claim for row in rows if (claim := _row_to_claim(row)) is not None]
    except Exception:
        _log.warning("[alpaca_claim] unresolved claim list failed scope=%s", scope, exc_info=True)
        return False, []


def acquire_action_claim(
    db: Session,
    *,
    symbol: str,
    action: str,
    claim_token: str,
    owner_session_id: int | None,
    client_order_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    account_scope: str | None = None,
    lease_seconds: int = _PRE_HTTP_LEASE_SECONDS,
) -> dict[str, Any]:
    """Acquire/reuse the permit inside the caller's transaction.

    Only ``resolved`` rows or expired *pre-HTTP* claims with no client/order id may
    be replaced. Submitted or indeterminate ownership never expires by time.
    """
    scope = account_scope or alpaca_account_scope()
    sym = _symbol(symbol)
    token = str(claim_token or "").strip()
    act = str(action or "").strip()
    cid = str(client_order_id or "").strip() or None
    if not sym or not token or not act:
        return {"ok": False, "reason": "invalid_claim_identity"}
    now = datetime.now(timezone.utc)
    lease_expires = now + timedelta(seconds=max(30, int(lease_seconds)))
    payload = json.dumps(dict(metadata or {}), separators=(",", ":"), default=str)
    try:
        inserted = db.execute(text(
            "INSERT INTO broker_symbol_action_claims ("
            " account_scope, symbol, claim_token, action, phase, owner_session_id,"
            " client_order_id, metadata_json, claimed_at, updated_at, lease_expires_at"
            ") VALUES ("
            " :scope, :symbol, :token, :action, 'claimed', :owner_session_id,"
            " :client_order_id, CAST(:metadata AS jsonb), :now, :now, :lease_expires"
            ") ON CONFLICT (account_scope, symbol) DO NOTHING "
            f"RETURNING {_CLAIM_COLUMNS}"
        ), {
            "scope": scope,
            "symbol": sym,
            "token": token,
            "action": act,
            "owner_session_id": owner_session_id,
            "client_order_id": cid,
            "metadata": payload,
            "now": now,
            "lease_expires": lease_expires,
        }).fetchone()
        if inserted is not None:
            return {"ok": True, "claim": _row_to_claim(inserted), "created": True}

        readable, existing = read_action_claim(
            db, symbol=sym, account_scope=scope, for_update=True
        )
        if not readable or existing is None:
            return {"ok": False, "reason": "claim_unreadable"}

        same_owner = bool(
            existing["action"] == act
            and (
                existing["claim_token"] == token
                or (
                    owner_session_id is not None
                    and existing.get("owner_session_id") == int(owner_session_id)
                )
            )
        )
        cid_compatible = bool(
            cid is None
            or existing.get("client_order_id") in (None, cid)
        )
        expired_pre_http = bool(
            existing["phase"] == CLAIMED
            and existing.get("client_order_id") is None
            and existing.get("broker_order_id") is None
            and existing.get("lease_expires_at") is not None
            and existing["lease_expires_at"] <= now
        )
        replaceable = existing["phase"] == RESOLVED or expired_pre_http

        proposed_metadata = dict(metadata or {})
        existing_metadata = dict(existing.get("metadata") or {})
        expired_bound_entry_generation = bool(
            act == "entry"
            and existing.get("action") == "entry"
            and existing.get("claim_token") == token
            and owner_session_id is not None
            and existing.get("owner_session_id") == int(owner_session_id)
            and cid is not None
            and existing.get("client_order_id") == cid
            and existing.get("phase") == CLAIMED
            and existing.get("broker_order_id") is None
            and existing.get("lease_expires_at") is not None
            and existing["lease_expires_at"] <= now
            and "entry_transport_started" not in existing_metadata
            and "owner_transport" not in existing_metadata
            and _entry_pre_transport_generation_rebindable(
                existing_metadata,
                proposed_metadata,
            )
        )
        if expired_bound_entry_generation:
            prior_binder = str(
                existing_metadata.get("entry_post_bind_token") or ""
            ).strip()
            next_binder = str(
                proposed_metadata.get("entry_post_bind_token") or ""
            ).strip()
            rebind_audit = {
                "pre_transport_generation_rebound": {
                    "schema_version": (
                        "chili.alpaca-entry-pre-transport-generation.v1"
                    ),
                    "client_order_id": cid,
                    "prior_binder_sha256": hashlib.sha256(
                        prior_binder.encode("utf-8")
                    ).hexdigest(),
                    "next_binder_sha256": hashlib.sha256(
                        next_binder.encode("utf-8")
                    ).hexdigest(),
                    "prior_lease_expires_at_utc": (
                        existing["lease_expires_at"].isoformat()
                    ),
                    "rebound_at_utc": now.isoformat(),
                    "reason": "expired_claim_only_pre_transport_recovery",
                }
            }
            row = db.execute(
                text(
                    "UPDATE broker_symbol_action_claims SET "
                    " metadata_json = metadata_json || CAST(:metadata AS jsonb)"
                    "   || CAST(:rebind_audit AS jsonb),"
                    " updated_at = :now, lease_expires_at = :lease_expires "
                    "WHERE account_scope = :scope AND symbol = :symbol "
                    " AND claim_token = :token AND action = 'entry'"
                    " AND owner_session_id = :owner_session_id"
                    " AND phase = 'claimed' AND broker_order_id IS NULL"
                    " AND client_order_id = :client_order_id"
                    " AND lease_expires_at <= :now"
                    " AND COALESCE(metadata_json->>'entry_post_bind_token', '')"
                    "     = :prior_binder"
                    " AND NOT (metadata_json ? 'entry_transport_started')"
                    " AND NOT (metadata_json ? 'owner_transport') "
                    f"RETURNING {_CLAIM_COLUMNS}"
                ),
                {
                    "scope": scope,
                    "symbol": sym,
                    "token": token,
                    "owner_session_id": int(owner_session_id),
                    "client_order_id": cid,
                    "prior_binder": prior_binder,
                    "metadata": json.dumps(
                        {"entry_post_bind_token": next_binder},
                        separators=(",", ":"),
                        default=str,
                    ),
                    "rebind_audit": json.dumps(
                        rebind_audit,
                        separators=(",", ":"),
                        default=str,
                    ),
                    "now": now,
                    "lease_expires": lease_expires,
                },
            ).fetchone()
            if row is None:
                return {
                    "ok": False,
                    "reason": "entry_pre_transport_generation_rebind_lost",
                    "claim": existing,
                }
            return {
                "ok": True,
                "claim": _row_to_claim(row),
                "pre_transport_generation_rebound": True,
                "prior_lease_expires_at": existing.get("lease_expires_at"),
            }

        if same_owner and cid_compatible and existing["phase"] != RESOLVED:
            if (
                act == "entry"
                and existing.get("client_order_id") is not None
                and not _entry_identity_metadata_matches(
                    existing_metadata,
                    proposed_metadata,
                )
            ):
                return {
                    "ok": False,
                    "reason": "entry_claim_identity_mismatch",
                    "claim": existing,
                }
            # Identity-bearing entry fields are immutable once the deterministic
            # CID is bound. Operational recovery metadata may still be appended.
            if act == "entry" and existing.get("client_order_id") is not None:
                proposed_metadata = {
                    key: value
                    for key, value in proposed_metadata.items()
                    if key not in _ENTRY_IDENTITY_METADATA_KEYS
                }
            safe_payload = json.dumps(
                proposed_metadata,
                separators=(",", ":"),
                default=str,
            )
            # Same authority may bind its deterministic CID and renew its pre-HTTP
            # lease, but it may never swap to a different CID.
            row = db.execute(text(
                "UPDATE broker_symbol_action_claims SET "
                " owner_session_id = COALESCE(owner_session_id, :owner_session_id),"
                " client_order_id = COALESCE(client_order_id, :client_order_id),"
                " metadata_json = metadata_json || CAST(:metadata AS jsonb),"
                " updated_at = :now,"
                " lease_expires_at = CASE "
                "   WHEN phase = 'claimed' AND client_order_id IS NULL "
                "   THEN :lease_expires ELSE lease_expires_at END "
                "WHERE account_scope = :scope AND symbol = :symbol "
                f"RETURNING {_CLAIM_COLUMNS}"
            ), {
                "scope": scope,
                "symbol": sym,
                "owner_session_id": owner_session_id,
                "client_order_id": cid,
                "metadata": safe_payload,
                "now": now,
                "lease_expires": lease_expires,
            }).fetchone()
            return {
                "ok": True,
                "claim": _row_to_claim(row),
                "reused": True,
                "prior_phase": existing.get("phase"),
                "prior_lease_expires_at": existing.get("lease_expires_at"),
                "client_order_id_bound": bool(
                    existing.get("client_order_id") is None and cid is not None
                ),
            }

        if replaceable:
            row = db.execute(text(
                "UPDATE broker_symbol_action_claims SET "
                " claim_token = :token, action = :action, phase = 'claimed',"
                " owner_session_id = :owner_session_id, client_order_id = :client_order_id,"
                " broker_order_id = NULL, metadata_json = CAST(:metadata AS jsonb),"
                " claimed_at = :now, updated_at = :now, lease_expires_at = :lease_expires,"
                " resolved_at = NULL "
                "WHERE account_scope = :scope AND symbol = :symbol "
                f"RETURNING {_CLAIM_COLUMNS}"
            ), {
                "scope": scope,
                "symbol": sym,
                "token": token,
                "action": act,
                "owner_session_id": owner_session_id,
                "client_order_id": cid,
                "metadata": payload,
                "now": now,
                "lease_expires": lease_expires,
            }).fetchone()
            return {"ok": True, "claim": _row_to_claim(row), "replaced": True}

        return {"ok": False, "reason": "symbol_action_claimed", "claim": existing}
    except Exception:
        _log.warning(
            "[alpaca_claim] claim acquire failed scope=%s symbol=%s action=%s",
            scope,
            sym,
            act,
            exc_info=True,
        )
        return {"ok": False, "reason": "claim_write_failed"}


def update_action_claim_phase(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    phase: str,
    client_order_id: str | None,
    broker_order_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    account_scope: str | None = None,
    retry_lease_seconds: int = _PRE_HTTP_LEASE_SECONDS,
) -> bool:
    """Advance the exact permit after submit; identity mismatch fails closed."""
    if phase not in {CLAIMED, SUBMIT_INDETERMINATE, SUBMITTED}:
        return False
    scope = account_scope or alpaca_account_scope()
    try:
        result = db.execute(text(
            "UPDATE broker_symbol_action_claims SET "
            " phase = :phase,"
            " client_order_id = COALESCE(client_order_id, :client_order_id),"
            " broker_order_id = COALESCE(broker_order_id, :broker_order_id),"
            " metadata_json = metadata_json || CAST(:metadata AS jsonb),"
            " updated_at = NOW(),"
            " lease_expires_at = CASE "
            "   WHEN :phase = 'submit_indeterminate' "
            "   THEN NOW() + (:retry_lease_seconds * interval '1 second') "
            "   WHEN :phase = 'submitted' THEN NULL "
            "   ELSE lease_expires_at END "
            "WHERE account_scope = :scope AND symbol = :symbol "
            "  AND claim_token = :token "
            "  AND phase <> 'resolved' "
            "  AND ("
            "    (phase = 'claimed'"
            "     AND :phase IN ('claimed', 'submit_indeterminate', 'submitted'))"
            "    OR (phase = 'submit_indeterminate'"
            "        AND :phase IN ('submit_indeterminate', 'submitted'))"
            "    OR (phase = 'submitted' AND :phase = 'submitted')"
            "  ) "
            "  AND (client_order_id IS NULL OR client_order_id = :client_order_id)"
            "  AND (broker_order_id IS NULL OR :broker_order_id IS NULL"
            "       OR broker_order_id = :broker_order_id)"
        ), {
            "scope": scope,
            "symbol": _symbol(symbol),
            "token": str(claim_token),
            "phase": phase,
            "client_order_id": str(client_order_id or "") or None,
            "broker_order_id": str(broker_order_id or "") or None,
            "retry_lease_seconds": max(30, int(retry_lease_seconds)),
            "metadata": json.dumps(dict(metadata or {}), separators=(",", ":"), default=str),
        })
        return int(result.rowcount or 0) == 1
    except Exception:
        _log.warning("[alpaca_claim] phase update failed for %s", symbol, exc_info=True)
        return False


def persist_orphan_close_request(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    client_order_id: str,
    close_request: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    account_scope: str | None = None,
) -> bool:
    """Freeze one exact close instruction before its first broker POST.

    Competing workers may calculate different extended-hours limits from adjacent
    quotes.  Only the first durable request wins; a later caller can proceed only
    when its byte-equivalent JSON request already owns the claim.
    """
    scope = account_scope or alpaca_account_scope()
    cid = str(client_order_id or "").strip()
    request = dict(close_request or {})
    if not cid or str(request.get("client_order_id") or "").strip() != cid:
        return False
    payload = {**dict(metadata or {}), "close_request": request}
    try:
        result = db.execute(text(
            "UPDATE broker_symbol_action_claims SET "
            " metadata_json = metadata_json || CAST(:metadata AS jsonb),"
            " updated_at = NOW() "
            "WHERE account_scope = :scope AND symbol = :symbol "
            "  AND claim_token = :token AND action = 'orphan_flatten' "
            "  AND phase IN ('claimed', 'submit_indeterminate') "
            "  AND client_order_id = :client_order_id "
            "  AND (metadata_json->'close_request' IS NULL "
            "       OR metadata_json->'close_request' = CAST(:request AS jsonb))"
        ), {
            "scope": scope,
            "symbol": _symbol(symbol),
            "token": str(claim_token),
            "client_order_id": cid,
            "request": json.dumps(request, separators=(",", ":"), default=str),
            "metadata": json.dumps(payload, separators=(",", ":"), default=str),
        })
        return int(result.rowcount or 0) == 1
    except Exception:
        _log.warning("[alpaca_claim] close request persist failed for %s", symbol, exc_info=True)
        return False


def bind_orphan_close_request(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    client_order_id: str,
    close_request: dict[str, Any],
    post_bind_token: str,
    metadata: dict[str, Any] | None = None,
    account_scope: str = "alpaca:paper",
    strict_cid_absent_after_expiry: bool = False,
    lease_seconds: int = _ORPHAN_CLOSE_TRANSPORT_LEASE_SECONDS,
) -> dict[str, Any]:
    """Lease one immutable close-only CID/request before broker transport.

    A proven pre-POST block leaves this authority recyclable instead of resolving
    the account/symbol claim while its session marker is still live.  Once a
    generation has crossed the transport-start fence, only an expired lease plus
    a caller-proven strict CID absence can lease the *same* CID/request under a
    new token.  No recovery path can mint a replacement order identity here.
    """
    scope = str(account_scope or "").strip().lower()
    sym = _symbol(symbol)
    cid = str(client_order_id or "").strip()
    proposed_token = str(post_bind_token or "").strip()
    request = dict(close_request or {})
    account_id = str(request.get("alpaca_account_id") or "").strip()
    if not (
        scope == "alpaca:paper"
        and bool(getattr(settings, "chili_alpaca_paper", True))
        and cid
        and proposed_token
        and account_id
        and _owner_transport_request_valid(
            request,
            symbol=sym,
            client_order_id=cid,
            transport_kind="emergency_exit",
        )
    ):
        return {"ok": False, "reason": "orphan_close_bind_request_invalid"}
    now = datetime.now(timezone.utc)
    lease_expires = now + timedelta(seconds=max(30, int(lease_seconds)))
    readable, claim = read_action_claim(
        db,
        symbol=sym,
        account_scope=scope,
        for_update=True,
    )
    if not readable or claim is None or not (
        claim.get("action") == "orphan_flatten"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("client_order_id") == cid
        and claim.get("phase") in {CLAIMED, SUBMIT_INDETERMINATE}
    ):
        return {"ok": False, "reason": "orphan_close_bind_claim_mismatch"}
    claim_meta = dict(claim.get("metadata") or {})
    existing_request = claim_meta.get("close_request")
    existing_token = str(claim_meta.get("close_post_bind_token") or "").strip()
    claim_account_id = str(
        claim_meta.get("close_alpaca_account_id")
        or claim_meta.get("alpaca_account_id")
        or account_id
    ).strip()
    if claim_account_id != account_id:
        return {"ok": False, "reason": "orphan_close_account_generation_mismatch"}

    reserved_keys = {
        "close_request",
        "close_post_bind_token",
        "close_transport_generation",
        "close_transport_state",
        "close_transport_started",
        "close_transport_lease_expires_at_utc",
        "close_alpaca_account_id",
    }
    proposed_metadata = dict(metadata or {})
    if any(
        key in proposed_metadata
        and key in claim_meta
        and proposed_metadata.get(key) != claim_meta.get(key)
        for key in reserved_keys
    ):
        return {"ok": False, "reason": "orphan_close_bind_metadata_mismatch"}
    proposed_metadata = {
        key: value for key, value in proposed_metadata.items() if key not in reserved_keys
    }

    try:
        generation = int(claim_meta.get("close_transport_generation") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "orphan_close_bind_generation_invalid"}
    state = str(claim_meta.get("close_transport_state") or "").strip().lower()
    if not state and existing_token:
        state = (
            "started"
            if claim_meta.get("close_transport_started") is True
            else "leased"
        )
    transport_started = state in {
        "started",
        "submit_indeterminate",
        "submitted",
    }
    expiry = _parse_utc(claim_meta.get("close_transport_lease_expires_at_utc"))
    expired = bool(expiry is not None and expiry <= now)
    same_cid_replay = False
    recycled_no_transport = False

    if isinstance(existing_request, dict):
        if existing_request != request or not existing_token:
            return {"ok": False, "reason": "orphan_close_bind_generation_mismatch"}
        if proposed_token == existing_token:
            if state != "leased" or expired:
                return {
                    "ok": False,
                    "reason": "orphan_close_new_generation_token_required",
                    "post_bind_token": existing_token,
                    "transport_started": transport_started,
                    "transport_state": state,
                    "transport_generation": generation,
                    "lease_expires_at_utc": (
                        expiry.isoformat() if expiry is not None else None
                    ),
                    "lease_expired": expired,
                    "claim_phase": claim.get("phase"),
                }
            return {
                "ok": True,
                "post_bind_token": existing_token,
                "transport_started": transport_started,
                "transport_state": state,
                "transport_generation": generation,
                "lease_expires_at_utc": (
                    expiry.isoformat() if expiry is not None else None
                ),
                "lease_expired": expired,
                "claim_phase": claim.get("phase"),
                "alpaca_account_id": account_id,
                "reused": True,
            }
        if state == "recyclable_no_transport":
            if claim.get("phase") != CLAIMED:
                return {
                    "ok": False,
                    "reason": "orphan_close_recyclable_phase_mismatch",
                }
            recycled_no_transport = True
        elif state == "leased" and expired:
            # The prior token never crossed the durable transport-start fence.
            # Rotating it fences a paused creator without needing broker proof.
            pass
        elif state in {"started", "submit_indeterminate"}:
            if not (
                expired
                and strict_cid_absent_after_expiry
                and not str(claim.get("broker_order_id") or "").strip()
            ):
                return {
                    "ok": False,
                    "reason": "orphan_close_transport_reconcile_required",
                    "transport_started": True,
                    "transport_state": state,
                    "transport_generation": generation,
                    "lease_expires_at_utc": (
                        expiry.isoformat() if expiry is not None else None
                    ),
                    "lease_expired": expired,
                }
            same_cid_replay = True
        else:
            return {
                "ok": False,
                "reason": "orphan_close_transport_reconcile_required",
                "transport_started": transport_started,
                "transport_state": state or None,
                "transport_generation": generation,
                "lease_expires_at_utc": (
                    expiry.isoformat() if expiry is not None else None
                ),
                "lease_expired": expired,
            }
        history = claim_meta.get(_ORPHAN_CLOSE_TRANSPORT_HISTORY_KEY)
        history = list(history) if isinstance(history, list) else []
        history.append({
            "transport_generation": generation,
            "post_bind_token": existing_token,
            "transport_state": state,
            "lease_expires_at_utc": (
                expiry.isoformat() if expiry is not None else None
            ),
            "recycled_at_utc": now.isoformat(),
            "recycle_proof": (
                "strict_cid_absent_after_expiry"
                if same_cid_replay
                else "proven_no_transport"
                if recycled_no_transport
                else "unstarted_lease_expired"
            ),
        })
        claim_meta[_ORPHAN_CLOSE_TRANSPORT_HISTORY_KEY] = history[-20:]
    elif claim.get("phase") != CLAIMED or existing_token or transport_started:
        return {"ok": False, "reason": "orphan_close_bind_not_creator_safe"}

    generation += 1
    claim_meta.update(proposed_metadata)
    claim_meta.update({
        "close_request": request,
        "close_alpaca_account_id": account_id,
        "close_post_bind_token": proposed_token,
        "close_transport_generation": generation,
        "close_transport_state": "leased",
        "close_transport_started": False,
        "close_transport_lease_expires_at_utc": lease_expires.isoformat(),
        "close_request_bound_at_utc": str(
            claim_meta.get("close_request_bound_at_utc") or now.isoformat()
        ),
        "close_transport_leased_at_utc": now.isoformat(),
    })
    claim_meta.pop("close_transport_started_at_utc", None)
    result = db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb),"
        " updated_at = NOW() WHERE account_scope = :scope AND symbol = :symbol"
        " AND claim_token = :token AND action = 'orphan_flatten'"
        " AND phase = :expected_phase AND phase <> 'resolved'"
        " AND client_order_id = :cid"
    ), {
        "metadata": json.dumps(claim_meta, separators=(",", ":"), default=str),
        "scope": scope,
        "symbol": sym,
        "token": str(claim_token),
        "cid": cid,
        "expected_phase": str(claim.get("phase") or ""),
    })
    if int(result.rowcount or 0) != 1:
        return {"ok": False, "reason": "orphan_close_bind_write_failed"}
    return {
        "ok": True,
        "post_bind_token": proposed_token,
        "transport_started": False,
        "transport_state": "leased",
        "transport_generation": generation,
        "lease_expires_at_utc": lease_expires.isoformat(),
        "claim_phase": claim.get("phase"),
        "alpaca_account_id": account_id,
        "same_cid_replay": same_cid_replay,
        "recycled_no_transport": recycled_no_transport,
        "created": not isinstance(existing_request, dict),
    }


def mark_orphan_close_transport_started(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    client_order_id: str,
    close_request: dict[str, Any],
    post_bind_token: str,
    transport_generation: int,
    expected_claim_phase: str,
    account_scope: str = "alpaca:paper",
    lease_seconds: int = _ORPHAN_CLOSE_TRANSPORT_LEASE_SECONDS,
) -> bool:
    """Consume the sole orphan-close POST permission immediately before HTTP."""
    scope = str(account_scope or "").strip().lower()
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope=scope,
        for_update=True,
    )
    if not readable or claim is None or not (
        claim.get("action") == "orphan_flatten"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("client_order_id") == str(client_order_id)
        and claim.get("phase") == str(expected_claim_phase or "")
        and claim.get("phase") in {CLAIMED, SUBMIT_INDETERMINATE}
        and claim.get("phase") != RESOLVED
    ):
        return False
    metadata = dict(claim.get("metadata") or {})
    try:
        current_generation = int(metadata.get("close_transport_generation"))
        expected_generation = int(transport_generation)
    except (TypeError, ValueError):
        return False
    now = datetime.now(timezone.utc)
    creator_expiry = _parse_utc(
        metadata.get("close_transport_lease_expires_at_utc")
    )
    if not (
        metadata.get("close_request") == dict(close_request or {})
        and str(metadata.get("close_post_bind_token") or "").strip()
        == str(post_bind_token or "").strip()
        and bool(str(post_bind_token or "").strip())
        and current_generation == expected_generation
        and metadata.get("close_transport_state") == "leased"
        and metadata.get("close_transport_started") is False
        and creator_expiry is not None
        and creator_expiry > now
    ):
        return False
    lease_expires = now + timedelta(seconds=max(30, int(lease_seconds)))
    metadata.update({
        "close_transport_state": "started",
        "close_transport_started": True,
        "close_transport_started_at_utc": now.isoformat(),
        "close_transport_lease_expires_at_utc": lease_expires.isoformat(),
    })
    result = db.execute(text(
        "UPDATE broker_symbol_action_claims SET metadata_json = CAST(:metadata AS jsonb),"
        " updated_at = NOW() WHERE account_scope = :scope AND symbol = :symbol"
        " AND claim_token = :token AND action = 'orphan_flatten'"
        " AND phase = :expected_phase AND phase <> 'resolved'"
        " AND client_order_id = :cid"
        " AND metadata_json ->> 'close_post_bind_token' = :post_bind_token"
        " AND metadata_json ->> 'close_transport_generation' = :generation"
        " AND metadata_json ->> 'close_transport_state' = 'leased'"
        " AND metadata_json -> 'close_request' = CAST(:request AS jsonb)"
    ), {
        "metadata": json.dumps(metadata, separators=(",", ":"), default=str),
        "scope": scope,
        "symbol": _symbol(symbol),
        "token": str(claim_token),
        "cid": str(client_order_id),
        "expected_phase": str(expected_claim_phase or ""),
        "post_bind_token": str(post_bind_token or "").strip(),
        "generation": str(int(transport_generation)),
        "request": json.dumps(dict(close_request or {}), separators=(",", ":"), default=str),
    })
    return int(result.rowcount or 0) == 1


def release_orphan_close_pre_post(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    client_order_id: str,
    close_request: dict[str, Any],
    post_bind_token: str,
    transport_generation: int,
    expected_claim_phase: str,
    reason: str,
    account_scope: str = "alpaca:paper",
) -> bool:
    """Recycle an exact unstarted generation without releasing close authority."""
    scope = str(account_scope or "").strip().lower()
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope=scope,
        for_update=True,
    )
    if not readable or claim is None or not (
        claim.get("action") == "orphan_flatten"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("client_order_id") == str(client_order_id)
        and claim.get("phase") == str(expected_claim_phase or "")
        and claim.get("phase") in {CLAIMED, SUBMIT_INDETERMINATE}
        and claim.get("phase") != RESOLVED
    ):
        return False
    metadata = dict(claim.get("metadata") or {})
    try:
        current_generation = int(metadata.get("close_transport_generation"))
        expected_generation = int(transport_generation)
    except (TypeError, ValueError):
        return False
    if not (
        metadata.get("close_request") == dict(close_request or {})
        and str(metadata.get("close_post_bind_token") or "").strip()
        == str(post_bind_token or "").strip()
        and bool(str(post_bind_token or "").strip())
        and current_generation == expected_generation
        and metadata.get("close_transport_state") == "leased"
        and metadata.get("close_transport_started") is False
    ):
        return False
    now = datetime.now(timezone.utc)
    history = metadata.get(_ORPHAN_CLOSE_TRANSPORT_HISTORY_KEY)
    history = list(history) if isinstance(history, list) else []
    history.append({
        "transport_generation": int(transport_generation),
        "post_bind_token": str(post_bind_token or "").strip(),
        "transport_state": "recyclable_no_transport",
        "no_transport_reason": str(reason or "pre_post_block"),
        "released_at_utc": now.isoformat(),
    })
    metadata[_ORPHAN_CLOSE_TRANSPORT_HISTORY_KEY] = history[-20:]
    metadata.update({
        "close_transport_state": "recyclable_no_transport",
        "close_transport_started": False,
        "close_transport_proven_no_transport": True,
        "close_transport_no_transport_reason": str(reason or "pre_post_block"),
        "close_request_released_at_utc": now.isoformat(),
    })
    result = db.execute(text(
        "UPDATE broker_symbol_action_claims SET"
        " metadata_json = CAST(:metadata AS jsonb),"
        " updated_at = NOW() WHERE account_scope = :scope AND symbol = :symbol"
        " AND claim_token = :token AND action = 'orphan_flatten'"
        " AND phase = :expected_phase AND phase <> 'resolved'"
        " AND client_order_id = :cid"
        " AND metadata_json ->> 'close_post_bind_token' = :post_bind_token"
        " AND metadata_json ->> 'close_transport_generation' = :generation"
        " AND metadata_json ->> 'close_transport_state' = 'leased'"
        " AND metadata_json -> 'close_request' = CAST(:request AS jsonb)"
    ), {
        "metadata": json.dumps(metadata, separators=(",", ":"), default=str),
        "scope": scope,
        "symbol": _symbol(symbol),
        "token": str(claim_token),
        "cid": str(client_order_id),
        "expected_phase": str(expected_claim_phase or ""),
        "post_bind_token": str(post_bind_token or "").strip(),
        "generation": str(int(transport_generation)),
        "request": json.dumps(dict(close_request or {}), separators=(",", ":"), default=str),
    })
    return int(result.rowcount or 0) == 1


def advance_orphan_close_claim_phase(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    client_order_id: str,
    close_request: dict[str, Any],
    post_bind_token: str,
    transport_generation: int,
    expected_claim_phase: str,
    phase: str,
    broker_order_id: str | None,
    metadata: dict[str, Any] | None = None,
    account_scope: str = "alpaca:paper",
    retry_lease_seconds: int = _PRE_HTTP_LEASE_SECONDS,
) -> bool:
    """CAS-advance only the exact close-only worker that crossed start fence.

    Unlike the generic phase helper, this transition binds action, prior claim
    phase, request, CID, generation, and lease token.  A paused worker therefore
    cannot advance a recycled generation or resurrect a resolved claim.
    """
    target_phase = str(phase or "").strip().lower()
    expected_phase = str(expected_claim_phase or "").strip().lower()
    scope = str(account_scope or "").strip().lower()
    sym = _symbol(symbol)
    cid = str(client_order_id or "").strip()
    token = str(post_bind_token or "").strip()
    oid = str(broker_order_id or "").strip() or None
    request = dict(close_request or {})
    try:
        generation = int(transport_generation)
    except (TypeError, ValueError):
        return False
    if not (
        scope == "alpaca:paper"
        and target_phase in {SUBMITTED, SUBMIT_INDETERMINATE}
        and expected_phase in {CLAIMED, SUBMIT_INDETERMINATE}
        and cid
        and token
        and generation > 0
        and (target_phase != SUBMITTED or oid is not None)
        and _owner_transport_request_valid(
            request,
            symbol=sym,
            client_order_id=cid,
            transport_kind="emergency_exit",
        )
    ):
        return False
    readable, claim = read_action_claim(
        db,
        symbol=sym,
        account_scope=scope,
        for_update=True,
    )
    if not readable or claim is None or not (
        claim.get("action") == "orphan_flatten"
        and claim.get("claim_token") == str(claim_token)
        and claim.get("client_order_id") == cid
        and claim.get("phase") == expected_phase
        and claim.get("phase") != RESOLVED
        and claim.get("broker_order_id") in (None, oid)
    ):
        return False
    claim_meta = dict(claim.get("metadata") or {})
    try:
        current_generation = int(claim_meta.get("close_transport_generation"))
    except (TypeError, ValueError):
        return False
    if not (
        current_generation == generation
        and claim_meta.get("close_request") == request
        and str(claim_meta.get("close_post_bind_token") or "").strip() == token
        and claim_meta.get("close_transport_state") == "started"
        and claim_meta.get("close_transport_started") is True
        and str(claim_meta.get("close_alpaca_account_id") or "").strip()
        == str(request.get("alpaca_account_id") or "").strip()
    ):
        return False
    now = datetime.now(timezone.utc)
    next_expiry = (
        now + timedelta(seconds=max(30, int(retry_lease_seconds)))
        if target_phase == SUBMIT_INDETERMINATE
        else None
    )
    operational = dict(metadata or {})
    for key in (
        "close_request",
        "close_post_bind_token",
        "close_transport_generation",
        "close_transport_state",
        "close_alpaca_account_id",
    ):
        operational.pop(key, None)
    claim_meta.update(operational)
    claim_meta.update({
        "close_transport_state": target_phase,
        "close_transport_result_at_utc": now.isoformat(),
        "close_transport_broker_order_id": oid,
        "close_transport_lease_expires_at_utc": (
            next_expiry.isoformat() if next_expiry is not None else None
        ),
    })
    result = db.execute(text(
        "UPDATE broker_symbol_action_claims SET"
        " phase = :phase,"
        " broker_order_id = COALESCE(broker_order_id, :broker_order_id),"
        " metadata_json = CAST(:metadata AS jsonb),"
        " lease_expires_at = :lease_expires,"
        " updated_at = :now"
        " WHERE account_scope = :scope AND symbol = :symbol"
        " AND claim_token = :claim_token AND action = 'orphan_flatten'"
        " AND phase = :expected_phase AND phase <> 'resolved'"
        " AND client_order_id = :cid"
        " AND (broker_order_id IS NULL OR broker_order_id = :broker_order_id)"
        " AND metadata_json ->> 'close_post_bind_token' = :post_bind_token"
        " AND metadata_json ->> 'close_transport_generation' = :generation"
        " AND metadata_json ->> 'close_transport_state' = 'started'"
        " AND metadata_json -> 'close_request' = CAST(:request AS jsonb)"
    ), {
        "phase": target_phase,
        "broker_order_id": oid,
        "metadata": json.dumps(claim_meta, separators=(",", ":"), default=str),
        "lease_expires": next_expiry,
        "now": now,
        "scope": scope,
        "symbol": sym,
        "claim_token": str(claim_token),
        "expected_phase": expected_phase,
        "cid": cid,
        "post_bind_token": token,
        "generation": str(generation),
        "request": json.dumps(request, separators=(",", ":"), default=str),
    })
    return int(result.rowcount or 0) == 1


def release_entry_claim_pre_post(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    client_order_id: str,
    post_bind_token: str,
    account_scope: str,
    alpaca_account_id: str,
    reason: str,
) -> bool:
    """CAS-release the one bound entry worker that has proven it never reached HTTP.

    Binding a CID alone cannot be released from a local ``did not POST`` assertion:
    another worker might already be paused at transport.  The immutable random
    ``post_bind_token`` makes exactly one worker the creator, while
    ``mark_entry_transport_started`` consumes that permission before the HTTP call.
    Therefore this release can win only while transport is globally still impossible.
    """
    scope = str(account_scope or "").strip().lower()
    sym = _symbol(symbol)
    token = str(claim_token or "").strip()
    cid = str(client_order_id or "").strip()
    binder = str(post_bind_token or "").strip()
    account_id = str(alpaca_account_id or "").strip()
    why = str(reason or "").strip()
    if not (
        scope == "alpaca:paper"
        and sym
        and token
        and cid
        and binder
        and account_id
        and why
    ):
        return False
    marker = json.dumps(
        {
            "pre_post_release": {
                "proven_no_transport": True,
                "reason": why,
                "client_order_id": cid,
                "post_bind_token": binder,
                "released_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        },
        separators=(",", ":"),
    )
    try:
        row = db.execute(
            text(
                "UPDATE broker_symbol_action_claims SET "
                " phase = 'resolved', resolved_at = NOW(), updated_at = NOW(),"
                " metadata_json = metadata_json || CAST(:marker AS jsonb) "
                "WHERE account_scope = :scope AND symbol = :symbol "
                " AND claim_token = :claim_token AND action = 'entry'"
                " AND owner_session_id = :owner_session_id"
                " AND phase = 'claimed' AND broker_order_id IS NULL"
                " AND client_order_id = :client_order_id"
                " AND COALESCE(metadata_json->>'alpaca_account_id', '') = :account_id"
                " AND COALESCE(metadata_json->>'entry_post_bind_token', '') = :binder"
                " AND NOT (metadata_json ? 'entry_transport_started')"
                " AND NOT (metadata_json ? 'owner_transport')"
            ),
            {
                "scope": scope,
                "symbol": sym,
                "claim_token": token,
                "owner_session_id": int(owner_session_id),
                "client_order_id": cid,
                "account_id": account_id,
                "binder": binder,
                "marker": marker,
            },
        )
        return int(row.rowcount or 0) == 1
    except Exception:
        _log.warning(
            "[alpaca_claim] pre-post entry release failed symbol=%s cid=%s",
            sym,
            cid,
            exc_info=True,
        )
        return False


class _CoordinatedPrePostReleaseBlocked(RuntimeError):
    """The exact claim/reservation generation cannot be jointly released."""

    def __init__(self, blocker: str) -> None:
        super().__init__(blocker)
        self.blocker = blocker


def release_entry_and_adaptive_reservation_pre_post(
    db: Session,
    *,
    reservation_id: str,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    client_order_id: str,
    post_bind_token: str,
    account_scope: str,
    alpaca_account_id: str,
    reason: str,
) -> dict[str, Any]:
    """Release one proven-pre-HTTP claim and reservation in one transaction.

    The adaptive reservation row is locked first, then this callback locks and
    CAS-resolves the exact action claim, and only then may the first-dip
    opportunity row be released.  Any mismatch or later validation failure
    raises through the caller transaction so neither ledger can commit alone.
    """

    scope = str(account_scope or "").strip().lower()
    sym = _symbol(symbol)
    token = str(claim_token or "").strip()
    cid = str(client_order_id or "").strip()
    binder = str(post_bind_token or "").strip()
    account_id = str(alpaca_account_id or "").strip()
    why = str(reason or "").strip()
    try:
        rid = uuid.UUID(str(reservation_id))
        owner_id = int(owner_session_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise _CoordinatedPrePostReleaseBlocked(
            "coordinated_release_identity_invalid"
        ) from exc
    if not (
        scope == "alpaca:paper"
        and sym
        and token
        and cid
        and binder
        and account_id
        and why
        and owner_id > 0
    ):
        raise _CoordinatedPrePostReleaseBlocked(
            "coordinated_release_identity_invalid"
        )

    bind = db.get_bind()
    engine = getattr(bind, "engine", bind)
    store = AdaptiveRiskReservationStore(engine)
    if not db.in_transaction():
        # ``_with_short_session`` commits the outer transaction after this
        # function returns.  Start it explicitly so the reservation store can
        # join the exact same transaction as the action-claim CAS.
        db.begin()

    def _release_exact_claim(
        session: Session,
        reservation: Any,
    ) -> bool:
        if (
            str(getattr(reservation, "account_scope", "") or "").strip().lower()
            != scope
            or str(getattr(reservation, "symbol", "") or "").strip().upper()
            != sym
        ):
            raise _CoordinatedPrePostReleaseBlocked(
                "reservation_identity_mismatch"
            )
        readable, claim = read_action_claim(
            session,
            symbol=sym,
            account_scope=scope,
            for_update=True,
        )
        if not readable:
            raise _CoordinatedPrePostReleaseBlocked("action_claim_unreadable")
        if claim is None:
            raise _CoordinatedPrePostReleaseBlocked("action_claim_missing")
        metadata = claim.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        try:
            exact_identity = bool(
                claim.get("action") == "entry"
                and claim.get("claim_token") == token
                and int(claim.get("owner_session_id")) == owner_id
                and claim.get("client_order_id") == cid
                and claim.get("broker_order_id") is None
                and str(metadata.get("alpaca_account_id") or "").strip()
                == account_id
                and str(metadata.get("entry_post_bind_token") or "").strip()
                == binder
            )
        except (TypeError, ValueError):
            exact_identity = False
        if not exact_identity:
            raise _CoordinatedPrePostReleaseBlocked(
                "action_claim_identity_mismatch"
            )

        packet = metadata.get("adaptive_risk_decision_packet")
        claim_payload = metadata.get("adaptive_risk_reservation_claim")
        request_payload = metadata.get("adaptive_risk_reservation_request")
        if not (
            isinstance(packet, dict)
            and isinstance(claim_payload, dict)
            and isinstance(request_payload, dict)
        ):
            raise _CoordinatedPrePostReleaseBlocked(
                "adaptive_claim_binding_missing"
            )
        try:
            adaptive_claim = load_and_verify_adaptive_risk_reservation_claim(
                packet,
                claim_payload,
            )
            adaptive_request = load_adaptive_risk_reservation_request(
                request_payload
            )
            packet_row = session.execute(
                text(
                    "SELECT reservation_request_sha256, account_scope, symbol, "
                    "client_order_id, setup_family, correlation_cluster "
                    "FROM adaptive_risk_decision_packets "
                    "WHERE decision_packet_sha256 = :decision_packet_sha256"
                ),
                {
                    "decision_packet_sha256": str(
                        getattr(reservation, "decision_packet_sha256", "") or ""
                    )
                },
            ).fetchone()
            binding_matches = bool(
                packet_row is not None
                and adaptive_claim.decision_packet_sha256
                == str(getattr(reservation, "decision_packet_sha256", "") or "")
                and str(packet_row[0]) == adaptive_request.request_sha256
                and str(packet_row[1]) == scope
                and str(packet_row[2]).strip().upper() == sym
                and str(packet_row[3]) == cid
                and str(packet_row[4])
                == str(getattr(reservation, "setup_family", "") or "")
                and str(packet_row[5])
                == str(getattr(reservation, "correlation_cluster", "") or "")
                and adaptive_claim.claim_id == cid
                and adaptive_claim.symbol == sym
                and adaptive_claim.execution_surface == "alpaca_paper"
                and adaptive_claim.execution_family == "alpaca_spot"
                and adaptive_claim.venue == "alpaca"
                and adaptive_claim.broker_environment == "paper"
                and adaptive_request.client_order_id == cid
                and adaptive_request.account_scope == scope
                and adaptive_request.inputs.symbol == sym
                and adaptive_request.setup_family
                == str(getattr(reservation, "setup_family", "") or "")
                and adaptive_request.correlation_cluster
                == str(getattr(reservation, "correlation_cluster", "") or "")
            )
        except (AdaptiveRiskContractError, KeyError, TypeError, ValueError):
            binding_matches = False
        if not binding_matches:
            raise _CoordinatedPrePostReleaseBlocked(
                "adaptive_claim_binding_mismatch"
            )

        phase = str(claim.get("phase") or "").strip().lower()
        if phase == RESOLVED:
            proof = metadata.get("pre_post_release")
            proof = proof if isinstance(proof, dict) else {}
            if not (
                proof.get("proven_no_transport") is True
                and proof.get("client_order_id") == cid
                and proof.get("post_bind_token") == binder
                and proof.get("reason") == why
                and "entry_transport_started" not in metadata
                and "owner_transport" not in metadata
            ):
                raise _CoordinatedPrePostReleaseBlocked(
                    "resolved_claim_lacks_exact_pre_post_proof"
                )
            return True
        if (
            phase != CLAIMED
            or "entry_transport_started" in metadata
            or "owner_transport" in metadata
        ):
            raise _CoordinatedPrePostReleaseBlocked(
                "action_claim_transport_state_indeterminate"
            )
        if not release_entry_claim_pre_post(
            session,
            symbol=sym,
            claim_token=token,
            owner_session_id=owner_id,
            client_order_id=cid,
            post_bind_token=binder,
            account_scope=scope,
            alpaca_account_id=account_id,
            reason=why,
        ):
            raise _CoordinatedPrePostReleaseBlocked(
                "action_claim_release_cas_failed"
            )
        return True

    state = store.release_zero_fill(
        rid,
        reason="pre_post_release",
        session=db,
        pre_post_claim_fence=_release_exact_claim,
    )
    if state.state != "released":
        raise _CoordinatedPrePostReleaseBlocked(
            "adaptive_reservation_release_unconfirmed"
        )
    return {
        "ok": True,
        "confirmed": True,
        "adaptive_released": True,
        "legacy_released": True,
        "reason": why,
        "reservation_id": str(state.reservation_id),
        "reservation_state": state.state,
        "opportunity_status": state.opportunity_status,
        "state": state,
    }


def mark_entry_transport_started(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    client_order_id: str,
    post_bind_token: str,
    account_scope: str,
    alpaca_account_id: str,
) -> bool:
    """Consume the creator's post permission immediately before broker HTTP.

    The durable phase becomes indeterminate *before* transport.  A crash in the
    following call can only reconcile the same CID; it can never be treated as a
    proven pre-HTTP release.
    """
    scope = str(account_scope or "").strip().lower()
    sym = _symbol(symbol)
    token = str(claim_token or "").strip()
    cid = str(client_order_id or "").strip()
    binder = str(post_bind_token or "").strip()
    account_id = str(alpaca_account_id or "").strip()
    if not (
        scope == "alpaca:paper"
        and sym
        and token
        and cid
        and binder
        and account_id
    ):
        return False
    marker = json.dumps(
        {
            "entry_transport_started": {
                "client_order_id": cid,
                "post_bind_token": binder,
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        },
        separators=(",", ":"),
    )
    try:
        row = db.execute(
            text(
                "UPDATE broker_symbol_action_claims SET "
                " phase = 'submit_indeterminate', updated_at = NOW(),"
                " metadata_json = metadata_json || CAST(:marker AS jsonb) "
                "WHERE account_scope = :scope AND symbol = :symbol "
                " AND claim_token = :claim_token AND action = 'entry'"
                " AND owner_session_id = :owner_session_id"
                " AND phase = 'claimed' AND broker_order_id IS NULL"
                " AND client_order_id = :client_order_id"
                " AND COALESCE(metadata_json->>'alpaca_account_id', '') = :account_id"
                " AND COALESCE(metadata_json->>'entry_post_bind_token', '') = :binder"
                " AND NOT (metadata_json ? 'entry_transport_started')"
                " AND NOT (metadata_json ? 'owner_transport')"
            ),
            {
                "scope": scope,
                "symbol": sym,
                "claim_token": token,
                "owner_session_id": int(owner_session_id),
                "client_order_id": cid,
                "account_id": account_id,
                "binder": binder,
                "marker": marker,
            },
        )
        return int(row.rowcount or 0) == 1
    except Exception:
        _log.warning(
            "[alpaca_claim] entry transport-start CAS failed symbol=%s cid=%s",
            sym,
            cid,
            exc_info=True,
        )
        return False


def resolve_action_claim(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    client_order_id: str | None,
    broker_order_id: str | None,
    broker_order_status: str | None,
    broker_position_zero: bool = False,
    attributable_position_zero: bool = False,
    broker_position_quantity: float | None = None,
    durable_entry_adopted: bool = False,
    zero_fill_terminal: bool = False,
    terminal_owner_broker_flat: bool = False,
    orphan_handoff_broker_flat: bool = False,
    proven_no_transport: bool = False,
    broker_cid_absent_after_grace: bool = False,
    expected_claim_updated_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    account_scope: str | None = None,
) -> bool:
    """Resolve an exact permit using action-specific broker proof.

    Entry ownership is released only after either durable fill adoption or an
    exact zero-fill terminal result.  A flat position alone is insufficient while
    a buy might still be open.  Orphan-flatten ownership is stricter: its exact
    sell must be filled *and* the broker must independently report no position.
    Runner close-only proof is equally strict: staged local, exact-entry, and
    broker quantities must have matched (zero protected floor), and the fresh
    post-fill broker quantity must be zero. A nonzero manual-share floor is not
    resolution proof.
    """
    scope = account_scope or alpaca_account_scope()
    status = str(broker_order_status or "").strip().lower()
    cid = str(client_order_id or "").strip() or None
    oid = str(broker_order_id or "").strip() or None
    try:
        readable, claim = read_action_claim(
            db, symbol=symbol, account_scope=scope, for_update=True
        )
        if not readable or claim is None or claim["claim_token"] != str(claim_token):
            return False
        if expected_claim_updated_at is not None and (
            not isinstance(expected_claim_updated_at, datetime)
            or claim.get("updated_at") != expected_claim_updated_at
        ):
            # A broker observation made against an older claim/binder generation
            # cannot mutate the replacement generation. The caller must reread
            # broker truth against the new durable claim snapshot.
            return False
        exact_cid = bool(
            claim.get("client_order_id")
            and cid
            and claim.get("client_order_id") == cid
        )
        exact_oid = bool(
            oid
            and claim.get("broker_order_id") in (None, oid)
        )
        pre_http_release = bool(
            claim["phase"] == CLAIMED
            and claim.get("broker_order_id") is None
            and status == "not_submitted"
            and proven_no_transport
            # Once a CID is bound, one local worker's "I did not POST" is not
            # global proof: a reused same-CID worker may already be paused at HTTP.
            and claim.get("client_order_id") is None
        )
        if claim.get("action") == "entry":
            claim_metadata = claim.get("metadata")
            claim_metadata = claim_metadata if isinstance(claim_metadata, dict) else {}
            owner_transport = claim_metadata.get(_OWNER_TRANSPORT_METADATA_KEY)
            if (
                isinstance(owner_transport, dict)
                and str(owner_transport.get("phase") or "").strip().lower()
                != RESOLVED
            ):
                # The entry row is also the cross-process outbox for every
                # protective/close sell.  Releasing ownership while that outbox
                # is unresolved would allow a fresh entry CID beside a paused
                # sell worker.
                return False
            entry_fill_adopted = bool(
                exact_cid
                and exact_oid
                and status in _TERMINAL_ORDER_STATUSES
                and durable_entry_adopted
            )
            entry_zero_fill_terminal = bool(
                exact_cid
                and status in {"canceled", "cancelled", "expired", "rejected", "failed"}
                and zero_fill_terminal
                and exact_oid
                and oid is not None
            )
            # A terminal/missing owner cannot durably adopt a historical fill.
            # Exact terminal entry identity plus an independently flat broker
            # position proves there is no remaining exposure to manage, so the
            # detached recovery sweep may retire the permit without fabricating a
            # session position or an exit fill.
            terminal_owner_exposure_gone = bool(
                exact_cid
                and exact_oid
                and status in _TERMINAL_ORDER_STATUSES
                and terminal_owner_broker_flat
                and broker_position_zero
            )
            proven = (
                pre_http_release
                or entry_fill_adopted
                or entry_zero_fill_terminal
                or terminal_owner_exposure_gone
            )
        elif claim.get("action") == "orphan_flatten":
            metadata_now = claim.get("metadata")
            metadata_now = metadata_now if isinstance(metadata_now, dict) else {}
            close_request = metadata_now.get("close_request")
            close_request = close_request if isinstance(close_request, dict) else {}
            try:
                immutable_max = float(metadata_now.get("max_close_qty"))
                protected_floor = float(
                    metadata_now.get("broker_unattributed_quantity_floor")
                )
                recert_broker_qty = float(
                    metadata_now.get("broker_position_qty_at_recertification")
                )
                broker_total = float(broker_position_quantity)
                frozen_qty = float(close_request.get("base_size"))
            except (TypeError, ValueError):
                immutable_max = protected_floor = recert_broker_qty = broker_total = frozen_qty = math.nan
            attributable_flat_proof = bool(
                attributable_position_zero
                and metadata_now.get("runner_emergency_close_only")
                and math.isfinite(immutable_max)
                and immutable_max > 0.0
                and math.isfinite(protected_floor)
                and abs(protected_floor) <= 1e-9
                and math.isfinite(recert_broker_qty)
                and abs(recert_broker_qty - immutable_max)
                <= max(1e-9, immutable_max * 1e-8)
                and math.isfinite(broker_total)
                and 0.0 <= broker_total <= 1e-9
                and math.isfinite(frozen_qty)
                and 0.0 < frozen_qty <= immutable_max + 1e-9
                and str(close_request.get("client_order_id") or "").strip()
                == str(claim.get("client_order_id") or "").strip()
                and str(close_request.get("product_id") or "").strip().upper()
                == str(claim.get("symbol") or "").strip().upper()
                and str(close_request.get("side") or "").strip().lower() == "sell"
                and str(close_request.get("position_intent") or "").strip().lower()
                == "sell_to_close"
            )
            exact_close_filled = bool(
                exact_cid
                and exact_oid
                and status == "filled"
                and (broker_position_zero or attributable_flat_proof)
            )
            handoff_flat_without_submit = bool(
                orphan_handoff_broker_flat
                and broker_position_zero
                and metadata_now.get("terminal_entry_handoff")
                and isinstance(metadata_now.get("entry_handoff_proof"), dict)
                and (
                    claim.get("broker_order_id") is None
                    or (
                        exact_cid
                        and exact_oid
                        and status in _TERMINAL_ORDER_STATUSES
                    )
                )
            )
            proven = (
                pre_http_release
                or exact_close_filled
                or handoff_flat_without_submit
            )
        else:
            proven = False
        if not proven:
            return False
        result = db.execute(text(
            "UPDATE broker_symbol_action_claims SET "
            " phase = 'resolved', resolved_at = NOW(), updated_at = NOW(),"
            " broker_order_id = COALESCE(broker_order_id, :broker_order_id),"
            " metadata_json = metadata_json || CAST(:metadata AS jsonb) "
            "WHERE account_scope = :scope AND symbol = :symbol AND claim_token = :token"
            " AND phase <> 'resolved'"
        ), {
            "scope": scope,
            "symbol": _symbol(symbol),
            "token": str(claim_token),
            "broker_order_id": oid,
            "metadata": json.dumps({
                **dict(metadata or {}),
                "resolved_order_status": status or None,
                "broker_position_zero": bool(broker_position_zero),
                "attributable_position_zero": bool(attributable_position_zero),
                "broker_position_quantity": broker_position_quantity,
                "durable_entry_adopted": bool(durable_entry_adopted),
                "zero_fill_terminal": bool(zero_fill_terminal),
                "terminal_owner_broker_flat": bool(terminal_owner_broker_flat),
                "orphan_handoff_broker_flat": bool(orphan_handoff_broker_flat),
                "proven_no_transport": bool(proven_no_transport),
                "broker_cid_absent_after_grace": bool(
                    broker_cid_absent_after_grace
                ),
            }, separators=(",", ":"), default=str),
        })
        return int(result.rowcount or 0) == 1
    except Exception:
        _log.warning("[alpaca_claim] resolve failed for %s", symbol, exc_info=True)
        return False


def _certified_long_execution_envelope(value: Any) -> bool:
    """Reject any explicit or contradictory short/unknown direction evidence."""
    if not isinstance(value, dict):
        return False
    position_raw = value.get("position")
    position = position_raw if isinstance(position_raw, dict) else {}
    short_evidence = False
    try:
        for marker in (value.get("side_long"), position.get("side_long")):
            if marker is False:
                short_evidence = True
            elif marker not in (None, True):
                return False
        for container in (value, position):
            if "side" in container and container.get("side") is not None:
                side = str(container.get("side") or "").strip().lower()
                if side in {"short", "sell"}:
                    short_evidence = True
                elif side not in {"long", "buy"}:
                    return False
            for key in ("position_intent", "intent"):
                if key not in container or container.get(key) is None:
                    continue
                intent = str(container.get(key) or "").strip().lower()
                if intent in {"sell_to_open", "buy_to_close"}:
                    short_evidence = True
                elif intent not in {"buy_to_open", "sell_to_close"}:
                    return False
    except Exception:
        return False
    return not short_evidence


def _certified_frozen_entry_request(
    request: Any,
    *,
    symbol: str,
    client_order_id: str,
) -> bool:
    if not isinstance(request, dict):
        return False
    try:
        qty = float(request.get("base_size"))
        order_type = str(request.get("order_type") or "").strip().lower()
        limit_price = (
            None
            if request.get("limit_price") is None
            else float(request.get("limit_price"))
        )
    except (TypeError, ValueError):
        return False
    return bool(
        not alpaca_symbol_is_crypto_like(symbol)
        and not alpaca_symbol_is_crypto_like(request.get("product_id"))
        and not alpaca_asset_class_is_crypto(request.get("asset_class"))
        and str(request.get("product_id") or "").strip().upper() == _symbol(symbol)
        and str(request.get("client_order_id") or "").strip()
        == str(client_order_id or "").strip()
        and str(request.get("side") or "").strip().lower() == "buy"
        and str(request.get("position_intent") or "").strip().lower()
        == "buy_to_open"
        and str(request.get("time_in_force") or "").strip().lower() == "day"
        and order_type in {"market", "limit"}
        and isinstance(request.get("extended_hours"), bool)
        and math.isfinite(qty)
        and qty > 0.0
        and (
            order_type == "market"
            or (
                limit_price is not None
                and math.isfinite(limit_price)
                and limit_price > 0.0
            )
        )
    )


def _adaptive_reservation_from_container(
    container: Any,
) -> tuple[dict[str, Any], dict[str, Any], Any] | None:
    """Load one strict packet+claim pair from session/claim metadata."""

    if not isinstance(container, dict):
        return None
    packet = container.get("adaptive_risk_decision_packet")
    claim_payload = container.get("adaptive_risk_reservation_claim")
    if packet is None and claim_payload is None:
        return None
    if not isinstance(packet, dict) or not isinstance(claim_payload, dict):
        raise AdaptiveRiskContractError("adaptive risk packet/claim pair is incomplete")
    claim = load_and_verify_adaptive_risk_reservation_claim(packet, claim_payload)
    return dict(packet), dict(claim_payload), claim


def _adaptive_atomic_ledger_from_rows(
    *,
    session_rows: list[Any],
    claim_rows: list[Any],
    account_id: str,
    account_identity_sha256: str,
    candidate_symbol: str,
    candidate_cluster: str,
    current_owner_session_id: int,
    current_claim_token: str,
    current_client_order_id: str,
) -> AdaptiveRiskLedgerSnapshot:
    """Project all owned open+pending dimensions under the account advisory lock.

    Legacy rows cannot be guessed into the adaptive ledger.  An open position or
    risk-bearing pending instruction without its strict packet+claim pair makes
    the ledger unreadable and therefore rejects the new candidate.
    """

    open_totals = {"risk": 0.0, "gross": 0.0, "bp": 0.0}
    pending_totals = {"risk": 0.0, "gross": 0.0, "bp": 0.0}
    open_symbol = pending_symbol = 0.0
    open_cluster = pending_cluster = 0.0
    opened_by_decision: dict[str, float] = {}

    for sid, row_symbol, family, _state, snapshot in session_rows:
        snap = snapshot if isinstance(snapshot, dict) else {}
        live = snap.get("momentum_live_execution")
        live = live if isinstance(live, dict) else {}
        position = live.get("position")
        if position is None:
            continue
        if not isinstance(position, dict):
            raise AdaptiveRiskContractError("adaptive open position is unreadable")
        if (
            str(family or "").strip().lower() != "alpaca_spot"
            or str(snap.get("alpaca_account_scope") or "").strip().lower()
            != "alpaca:paper"
            or str(snap.get("alpaca_account_id") or "").strip() != account_id
        ):
            raise AdaptiveRiskContractError("adaptive open position account mismatch")
        reservation = _adaptive_reservation_from_container(live)
        if reservation is None:
            raise AdaptiveRiskContractError(
                "adaptive open position lacks a strict reservation claim"
            )
        _packet, _payload, claim = reservation
        try:
            qty = abs(float(position.get("quantity")))
        except (TypeError, ValueError):
            qty = math.nan
        if (
            not math.isfinite(qty)
            or qty <= 0.0
            or claim.quantity_shares <= 0
            or qty > float(claim.quantity_shares) + 1e-9
            or claim.account_identity_sha256 != account_identity_sha256
            or claim.symbol != str(row_symbol or "").strip().upper()
        ):
            raise AdaptiveRiskContractError("adaptive open position claim mismatch")
        ratio = qty / float(claim.quantity_shares)
        dims = {
            "risk": float(claim.structural_risk_usd) * ratio,
            "gross": float(claim.gross_notional_usd) * ratio,
            "bp": float(claim.buying_power_impact_usd) * ratio,
        }
        for name, value in dims.items():
            open_totals[name] += value
        if claim.symbol == candidate_symbol:
            open_symbol += dims["risk"]
        if claim.correlation_cluster_id == candidate_cluster:
            open_cluster += dims["risk"]
        opened_by_decision[claim.decision_packet_sha256] = (
            opened_by_decision.get(claim.decision_packet_sha256, 0.0) + qty
        )

    for (
        row_symbol,
        row_action,
        owner_id,
        row_cid,
        row_token,
        row_phase,
        _row_oid,
        metadata,
    ) in claim_rows:
        if str(row_action or "") != "entry":
            continue
        meta = metadata if isinstance(metadata, dict) else {}
        is_current = bool(
            str(row_symbol or "").strip().upper() == candidate_symbol
            and str(row_token or "") == current_claim_token
            and int(owner_id) == int(current_owner_session_id)
            and (row_cid is None or str(row_cid) == current_client_order_id)
        )
        if is_current:
            continue
        # A watch/arm symbol claim has no broker instruction and no economics.
        if (
            str(row_phase) == CLAIMED
            and row_cid is None
            and not meta.get("order_request")
            and meta.get("reserved_risk_usd") is None
        ):
            continue
        reservation = _adaptive_reservation_from_container(meta)
        if reservation is None:
            raise AdaptiveRiskContractError(
                "adaptive pending instruction lacks a strict reservation claim"
            )
        _packet, _payload, claim = reservation
        if claim.account_identity_sha256 != account_identity_sha256:
            raise AdaptiveRiskContractError("adaptive pending claim account mismatch")
        filled_qty = opened_by_decision.get(claim.decision_packet_sha256, 0.0)
        remaining_ratio = max(
            0.0,
            (float(claim.quantity_shares) - filled_qty)
            / float(claim.quantity_shares),
        )
        dims = {
            "risk": float(claim.structural_risk_usd) * remaining_ratio,
            "gross": float(claim.gross_notional_usd) * remaining_ratio,
            "bp": float(claim.buying_power_impact_usd) * remaining_ratio,
        }
        for name, value in dims.items():
            pending_totals[name] += value
        if claim.symbol == candidate_symbol:
            pending_symbol += dims["risk"]
        if claim.correlation_cluster_id == candidate_cluster:
            pending_cluster += dims["risk"]

    return AdaptiveRiskLedgerSnapshot.from_dimensions(
        open_structural_risk_usd=open_totals["risk"],
        pending_reserved_risk_usd=pending_totals["risk"],
        existing_same_symbol_structural_risk_usd=open_symbol,
        pending_same_symbol_structural_risk_usd=pending_symbol,
        current_cluster_structural_risk_usd=open_cluster,
        pending_correlation_cluster_risk_usd=pending_cluster,
        portfolio_gross_notional_usd=open_totals["gross"],
        pending_portfolio_gross_notional_usd=pending_totals["gross"],
        open_buying_power_impact_usd=open_totals["bp"],
        pending_buying_power_impact_usd=pending_totals["bp"],
    )


def _reserve_alpaca_entry_risk(
    db: Session,
    *,
    symbol: str,
    claim_token: str,
    owner_session_id: int,
    client_order_id: str,
    order_request: dict[str, Any],
    order_role: str,
    reserved_risk_usd: float,
    account_equity_usd: float,
    post_bind_token: str,
    role_metadata: dict[str, Any] | None = None,
    account_scope: str | None = None,
    budget_fraction: float | None = None,
    per_symbol_cap_usd: float | None = None,
) -> dict[str, Any]:
    """Account-lock, admit, and freeze one entry request in one short tx."""
    scope = str(account_scope or "").strip().lower()
    if scope != "alpaca:paper":
        return {"ok": False, "reason": "alpaca_account_scope_not_certified"}
    if not bool(getattr(settings, "chili_alpaca_paper", True)):
        return {"ok": False, "reason": "alpaca_live_posture_not_certified"}
    sym = _symbol(symbol)
    request = dict(order_request or {})
    role_meta = dict(role_metadata or {})
    adaptive_packet = role_meta.get("adaptive_risk_decision_packet")
    adaptive_claim_payload = role_meta.get("adaptive_risk_reservation_claim")
    adaptive_request_payload = role_meta.get("adaptive_risk_reservation_request")
    adaptive_pair_present = (
        adaptive_packet is not None
        or adaptive_claim_payload is not None
        or adaptive_request_payload is not None
    )
    if not adaptive_pair_present:
        return {"ok": False, "reason": "adaptive_risk_request_packet_claim_required"}
    if not (
        isinstance(adaptive_packet, dict)
        and isinstance(adaptive_claim_payload, dict)
        and isinstance(adaptive_request_payload, dict)
    ):
        return {"ok": False, "reason": "adaptive_risk_request_packet_claim_incomplete"}
    try:
        adaptive_claim = load_and_verify_adaptive_risk_reservation_claim(
            adaptive_packet,
            adaptive_claim_payload,
        )
        adaptive_request = load_adaptive_risk_reservation_request(
            adaptive_request_payload
        )
        request_resolution = resolve_adaptive_risk(
            adaptive_request.policy,
            adaptive_request.inputs,
        )
    except AdaptiveRiskContractError:
        return {"ok": False, "reason": "adaptive_risk_request_packet_claim_invalid"}
    if (
        not sym
        or alpaca_symbol_is_crypto_like(sym)
        or alpaca_asset_class_is_crypto(request.get("asset_class"))
        or alpaca_asset_class_is_crypto(role_meta.get("asset_class"))
    ):
        return {"ok": False, "reason": "alpaca_equity_long_only"}
    token = str(claim_token or "").strip()
    cid = str(client_order_id or "").strip()
    role = str(order_role or "").strip().lower()
    try:
        candidate = float(reserved_risk_usd)
    except (TypeError, ValueError):
        candidate = float("nan")
    if not math.isfinite(candidate) or candidate <= 0.0:
        return {"ok": False, "reason": "invalid_candidate_risk"}
    try:
        equity = float(account_equity_usd)
    except (TypeError, ValueError):
        equity = float("nan")
    if not math.isfinite(equity) or equity <= 0.0:
        return {"ok": False, "reason": "equity_unavailable"}
    # The strict adaptive resolver owns symbol, cluster, daily and portfolio
    # budgets.  Missing adaptive economics is rejected above; there is no
    # activation-only dollar fallback on this committed entry boundary.
    symbol_cap = float("inf")
    if budget_fraction is None:
        try:
            budget_fraction = float(
                getattr(
                    settings,
                    "chili_momentum_max_aggregate_risk_pct_of_equity",
                    0.03,
                )
                or 0.0
            )
        except (TypeError, ValueError):
            budget_fraction = float("nan")
    try:
        budget_frac = float(budget_fraction)
    except (TypeError, ValueError):
        budget_frac = float("nan")
    if not math.isfinite(budget_frac):
        return {"ok": False, "reason": "invalid_account_budget_fraction"}
    account_budget = float("inf") if budget_frac <= 0.0 else equity * budget_frac

    request_symbol = str(request.get("product_id") or "").strip().upper()
    request_account_id = str(request.get("alpaca_account_id") or "").strip()
    binder_token = str(post_bind_token or "").strip()
    request_cid = str(request.get("client_order_id") or "").strip()
    request_side = str(request.get("side") or "").strip().lower()
    request_type = str(request.get("order_type") or "").strip().lower()
    request_tif = str(request.get("time_in_force") or "").strip().lower()
    request_intent = str(request.get("position_intent") or "").strip().lower()
    try:
        request_qty = float(request.get("base_size"))
    except (TypeError, ValueError):
        request_qty = float("nan")
    try:
        request_limit = (
            None
            if request.get("limit_price") is None
            else float(request.get("limit_price"))
        )
    except (TypeError, ValueError):
        request_limit = float("nan")
    request_ok = bool(
        sym
        and token
        and cid
        and role
        and request_account_id
        and binder_token
        and request_symbol == sym
        and request_cid == cid
        and request_side == "buy"
        and request_type in {"market", "limit"}
        and request_tif == "day"
        and isinstance(request.get("extended_hours"), bool)
        and request_intent == "buy_to_open"
        and math.isfinite(request_qty)
        and request_qty > 0.0
        and abs(request_qty - round(request_qty)) <= 1e-9
        and (
            request_type == "market"
            or (
                request_limit is not None
                and math.isfinite(request_limit)
                and request_limit > 0.0
            )
        )
    )
    if not request_ok:
        return {"ok": False, "reason": "invalid_order_request"}
    try:
        adaptive_request_ok = bool(
            adaptive_claim.execution_surface == "alpaca_paper"
            and adaptive_claim.execution_family == "alpaca_spot"
            and adaptive_claim.venue == "alpaca"
            and adaptive_claim.broker_environment == "paper"
            and adaptive_claim.symbol == sym
            and adaptive_claim.side == "long"
            and adaptive_claim.claim_id == cid
            and adaptive_claim.quantity_shares == int(request_qty)
            and abs(request_qty - float(adaptive_claim.quantity_shares)) <= 1e-9
            and adaptive_claim.structural_risk_usd > 0.0
            and adaptive_claim.gross_notional_usd > 0.0
            and adaptive_claim.buying_power_impact_usd > 0.0
            and adaptive_request.client_order_id == cid
            and adaptive_request.account_scope == scope
            and adaptive_request.inputs.symbol == sym
            and adaptive_request.inputs.execution_surface == "alpaca_paper"
            and adaptive_request.inputs.execution_family == "alpaca_spot"
            and adaptive_request.inputs.venue == "alpaca"
            and adaptive_request.inputs.broker_environment == "paper"
            and request_resolution.valid
            and request_resolution.decision_packet_sha256
            == adaptive_claim.decision_packet_sha256
            and (
                request_type != "limit"
                or math.isclose(
                    adaptive_request.entry_limit_price,
                    float(request_limit),
                    rel_tol=1e-12,
                    abs_tol=1e-9,
                )
            )
        )
    except (TypeError, ValueError):
        adaptive_request_ok = False
    if not adaptive_request_ok:
        return {"ok": False, "reason": "adaptive_risk_order_request_mismatch"}
    candidate = float(adaptive_claim.structural_risk_usd)

    db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": alpaca_account_risk_lock_key(scope)},
    )
    readable, existing = read_action_claim(
        db,
        symbol=sym,
        account_scope=scope,
        for_update=True,
    )
    if not readable:
        return {"ok": False, "reason": "risk_ledger_unreadable"}
    compatible_existing = bool(
        existing is not None
        and existing.get("action") == "entry"
        and existing.get("claim_token") == token
        and existing.get("owner_session_id") == int(owner_session_id)
        and existing.get("client_order_id") in (None, cid)
    )
    exact_existing = bool(
        compatible_existing and existing.get("client_order_id") == cid
    )
    if existing is not None and existing.get("phase") != RESOLVED:
        if not compatible_existing:
            return {
                "ok": False,
                "reason": "symbol_action_claimed",
                "claim": existing,
            }
        if exact_existing:
            existing_meta = dict(existing.get("metadata") or {})
            if (
                existing_meta.get("order_role") != role
                or existing_meta.get("order_request") != request
                or (
                    existing_meta.get("adaptive_risk_decision_packet")
                    != adaptive_packet
                    or existing_meta.get("adaptive_risk_reservation_claim")
                    != adaptive_claim_payload
                    or existing_meta.get("adaptive_risk_reservation_request")
                    != adaptive_request_payload
                )
            ):
                return {
                    "ok": False,
                    "reason": "entry_claim_identity_mismatch",
                    "claim": existing,
                }
            try:
                existing_risk = float(existing_meta.get("reserved_risk_usd"))
            except (TypeError, ValueError):
                existing_risk = float("nan")
            if (
                not math.isfinite(existing_risk)
                or abs(existing_risk - candidate) > max(1e-9, candidate * 1e-9)
            ):
                return {
                    "ok": False,
                    "reason": "entry_claim_identity_mismatch",
                    "claim": existing,
                }

    # Read every local paper-Alpaca row, regardless of FSM state.  A terminal/error
    # state is not proof of flatness; persisted position evidence must win.
    session_rows = db.execute(text(
        "SELECT id, upper(symbol), execution_family, state, risk_snapshot_json "
        "FROM trading_automation_sessions "
        "WHERE mode = 'live' AND execution_family IN ('alpaca_spot', 'alpaca_short') "
    )).fetchall()
    claim_rows = db.execute(text(
        "SELECT upper(symbol), action, owner_session_id, client_order_id, claim_token,"
        "       phase, broker_order_id, metadata_json "
        "FROM broker_symbol_action_claims "
        "WHERE account_scope = :scope AND phase <> 'resolved'"
    ), {"scope": scope}).fetchall()

    rows_by_id = {int(row[0]): row for row in session_rows}
    owner_rows = [row for row in session_rows if int(row[0]) == int(owner_session_id)]
    if len(owner_rows) != 1:
        return {"ok": False, "reason": "owner_session_not_certified"}
    _, owner_symbol, owner_family, _owner_state, owner_snapshot = owner_rows[0]
    owner_snap = owner_snapshot if isinstance(owner_snapshot, dict) else {}
    owner_live = owner_snap.get("momentum_live_execution")
    owner_live = owner_live if isinstance(owner_live, dict) else {}
    owner_position = owner_live.get("position")
    owner_position = owner_position if isinstance(owner_position, dict) else {}
    if not (
        str(owner_family or "").strip().lower() == "alpaca_spot"
        and str(owner_symbol or "").strip().upper() == sym
        and str(owner_snap.get("alpaca_account_scope") or "").strip().lower()
        == "alpaca:paper"
        and str(owner_snap.get("alpaca_account_id") or "").strip()
        == request_account_id
        and _certified_long_execution_envelope(owner_live)
    ):
        return {"ok": False, "reason": "owner_session_not_certified"}

    # Account scope alone is not an identity.  A paper credential can be swapped
    # without changing that scope, so every unresolved permit in the account-wide
    # risk ledger must belong to this same stable Alpaca account UUID.
    for claim_row in claim_rows:
        claim_meta = claim_row[7] if isinstance(claim_row[7], dict) else {}
        claim_request = claim_meta.get("order_request")
        claim_request = claim_request if isinstance(claim_request, dict) else {}
        frozen_claim_account_id = str(
            claim_meta.get("alpaca_account_id")
            or claim_request.get("alpaca_account_id")
            or ""
        ).strip()
        if frozen_claim_account_id != request_account_id:
            return {"ok": False, "reason": "alpaca_account_generation_mismatch"}

    adaptive_ledger = None
    if adaptive_claim is not None:
        try:
            adaptive_ledger = _adaptive_atomic_ledger_from_rows(
                session_rows=list(session_rows),
                claim_rows=list(claim_rows),
                account_id=request_account_id,
                account_identity_sha256=adaptive_claim.account_identity_sha256,
                candidate_symbol=sym,
                candidate_cluster=adaptive_claim.correlation_cluster_id,
                current_owner_session_id=int(owner_session_id),
                current_claim_token=token,
                current_client_order_id=cid,
            )
            verify_adaptive_risk_claim_against_atomic_ledger(
                adaptive_packet,
                adaptive_claim_payload,
                adaptive_ledger,
            )
            packet_inputs = adaptive_packet.get("input_snapshot")
            packet_inputs = packet_inputs if isinstance(packet_inputs, dict) else {}
            packet_equity = float(packet_inputs.get("equity_usd"))
            if abs(packet_equity - equity) > max(
                1e-9,
                max(abs(packet_equity), abs(equity)) * 1e-12,
            ):
                raise AdaptiveRiskContractError(
                    "adaptive account equity differs at reservation"
                )
        except (AdaptiveRiskContractError, TypeError, ValueError):
            return {"ok": False, "reason": "adaptive_risk_atomic_ledger_mismatch"}

    # Serial recertification posture: no add/pyramid may reserve while *any*
    # persisted position exists.  Classify position evidence before state so a
    # pending-entry row with a fill cannot fall through the legacy-pending path.
    pending_sessions: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
    try:
        for sid, row_symbol, family, state, snapshot in session_rows:
            snap = snapshot if isinstance(snapshot, dict) else {}
            live = snap.get("momentum_live_execution")
            live = live if isinstance(live, dict) else {}
            if "position" in live and live.get("position") is not None:
                pos = live.get("position")
                if not isinstance(pos, dict):
                    raise ValueError("persisted_position_unreadable")
                qty = abs(float(pos.get("quantity")))
                if not math.isfinite(qty) or qty <= 0.0:
                    raise ValueError("persisted_position_quantity_unreadable")
                if (
                    str(family or "").strip().lower() != "alpaca_spot"
                    or str(snap.get("alpaca_account_scope") or "").strip().lower()
                    != "alpaca:paper"
                    or str(snap.get("alpaca_account_id") or "").strip()
                    != request_account_id
                    or not _certified_long_execution_envelope(live)
                ):
                    raise ValueError("persisted_position_direction_not_certified")
                if adaptive_claim is None:
                    return {
                        "ok": False,
                        "reason": "account_position_exposure_present",
                        "position_session_id": int(sid),
                        "position_symbol": str(row_symbol),
                        "position_state": str(state),
                    }
                continue
            if str(state) == "live_pending_entry" and live.get("entry_submitted"):
                if (
                    str(family or "").strip().lower() != "alpaca_spot"
                    or str(snap.get("alpaca_account_scope") or "").strip().lower()
                    != "alpaca:paper"
                    or str(snap.get("alpaca_account_id") or "").strip()
                    != request_account_id
                    or not _certified_long_execution_envelope(live)
                ):
                    raise ValueError("legacy_pending_direction_not_certified")
                pending_sessions.append((int(sid), str(row_symbol), live, snap))
    except Exception:
        return {"ok": False, "reason": "risk_ledger_unreadable"}

    claim_owner_cids: set[tuple[int, str]] = set()
    try:
        for (
            row_symbol,
            row_action,
            owner_id,
            row_cid,
            row_token,
            row_phase,
            row_oid,
            metadata,
        ) in claim_rows:
            meta = metadata if isinstance(metadata, dict) else {}
            is_current = bool(
                compatible_existing
                and str(row_action) == "entry"
                and str(row_symbol) == sym
                and str(row_token) == token
                and int(owner_id) == int(owner_session_id)
                and (row_cid is None or str(row_cid) == cid)
            )
            if owner_id is not None and row_cid:
                claim_owner_cids.add((int(owner_id), str(row_cid)))
            if is_current:
                continue
            if (
                str(row_action) == "entry"
                and
                str(row_phase) == CLAIMED
                and row_cid is None
                and row_oid is None
                and not meta.get("order_request")
                and meta.get("reserved_risk_usd") is None
            ):
                # A pure arm/watch symbol claim has no frozen instruction or dollars.
                continue
            if str(row_action) != "entry":
                return {
                    "ok": False,
                    "reason": "account_unresolved_non_entry_claim",
                    "blocking_claim_action": str(row_action),
                    "blocking_claim_symbol": str(row_symbol),
                }
            owner_row = rows_by_id.get(int(owner_id)) if owner_id is not None else None
            if owner_row is None:
                raise ValueError("claim_owner_missing")
            _, claim_owner_symbol, claim_family, _claim_state, claim_snapshot = owner_row
            claim_snap = claim_snapshot if isinstance(claim_snapshot, dict) else {}
            claim_live = claim_snap.get("momentum_live_execution")
            claim_live = claim_live if isinstance(claim_live, dict) else {}
            if (
                str(claim_family or "").strip().lower() != "alpaca_spot"
                or str(claim_snap.get("alpaca_account_scope") or "").strip().lower()
                != "alpaca:paper"
                or str(claim_owner_symbol or "").strip().upper()
                != str(row_symbol or "").strip().upper()
                or not _certified_long_execution_envelope(claim_live)
                or not _certified_frozen_entry_request(
                    meta.get("order_request"),
                    symbol=str(row_symbol),
                    client_order_id=str(row_cid or ""),
                )
            ):
                raise ValueError("claim_instruction_not_certified")
            risk = float(meta.get("reserved_risk_usd"))
            if not math.isfinite(risk) or risk <= 0.0:
                raise ValueError("claim_reservation_invalid")
            if adaptive_claim is not None:
                continue
            return {
                "ok": False,
                "reason": "account_entry_claim_present",
                "blocking_claim_symbol": str(row_symbol),
                "blocking_claim_client_order_id": str(row_cid or ""),
            }
    except Exception:
        return {"ok": False, "reason": "risk_ledger_unreadable"}

    try:
        for sid, row_symbol, live, _snap in pending_sessions:
            legacy_cid = str(live.get("entry_client_order_id") or "").strip()
            if legacy_cid and (sid, legacy_cid) in claim_owner_cids:
                continue
            if adaptive_claim is not None:
                raise ValueError("legacy_pending_not_in_adaptive_ledger")
            risk = float(live.get("entry_inflight_risk_usd"))
            if (
                not math.isfinite(risk)
                or risk <= 0.0
                or not _certified_frozen_entry_request(
                    live.get("entry_order_request"),
                    symbol=row_symbol,
                    client_order_id=legacy_cid,
                )
            ):
                raise ValueError("legacy_reservation_invalid")
            return {
                "ok": False,
                "reason": "account_legacy_entry_present",
                "blocking_session_id": sid,
                "blocking_symbol": row_symbol,
            }
    except Exception:
        return {"ok": False, "reason": "risk_ledger_unreadable"}

    if adaptive_ledger is not None:
        packet_risk_caps = adaptive_packet.get("risk_budget_caps_usd")
        packet_risk_caps = (
            packet_risk_caps if isinstance(packet_risk_caps, dict) else {}
        )
        open_account_risk = float(adaptive_ledger.open_structural_risk_usd)
        pending_account_risk = float(adaptive_ledger.pending_reserved_risk_usd)
        open_symbol_risk = float(
            adaptive_ledger.existing_same_symbol_structural_risk_usd
        )
        pending_symbol_risk = float(
            adaptive_ledger.pending_same_symbol_structural_risk_usd
        )
        projected_account = open_account_risk + pending_account_risk + candidate
        projected_symbol = open_symbol_risk + pending_symbol_risk + candidate
        try:
            account_budget = (
                open_account_risk
                + pending_account_risk
                + float(
                    packet_risk_caps[
                        "portfolio_remaining_after_open_and_pending"
                    ]
                )
            )
            symbol_cap = (
                open_symbol_risk
                + pending_symbol_risk
                + float(
                    packet_risk_caps[
                        "symbol_remaining_after_existing_and_pending"
                    ]
                )
            )
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "reason": "adaptive_risk_budget_caps_unreadable"}
    else:
        open_account_risk = pending_account_risk = 0.0
        open_symbol_risk = pending_symbol_risk = 0.0
        projected_account = candidate
        projected_symbol = candidate
    detail = {
        "reserved_risk_usd": candidate,
        "account_open_risk_usd": open_account_risk,
        "active_claim_risk_usd": pending_account_risk,
        "projected_account_risk_usd": projected_account,
        "account_budget_usd": account_budget,
        "symbol_open_risk_usd": open_symbol_risk,
        "symbol_active_claim_risk_usd": pending_symbol_risk,
        "projected_symbol_risk_usd": projected_symbol,
        "symbol_cap_usd": symbol_cap,
    }
    if projected_symbol > symbol_cap + 1e-9:
        return {"ok": False, "reason": "symbol_risk_cap_exceeded", **detail}
    if projected_account > account_budget + 1e-9:
        return {"ok": False, "reason": "account_risk_budget_exceeded", **detail}

    acquired = acquire_action_claim(
        db,
        symbol=sym,
        action="entry",
        claim_token=token,
        owner_session_id=int(owner_session_id),
        client_order_id=cid,
        metadata={
            "stage": "pre_broker_place",
            "order_role": role,
            "order_request": request,
            "alpaca_account_id": request_account_id,
            "entry_post_bind_token": binder_token,
            "reserved_risk_usd": candidate,
            **(
                {
                    "adaptive_risk_decision_packet": adaptive_packet,
                    "adaptive_risk_reservation_claim": adaptive_claim_payload,
                    "adaptive_risk_reservation_request": (
                        adaptive_request_payload
                    ),
                    "reserved_gross_notional_usd": float(
                        adaptive_claim.gross_notional_usd
                    ),
                    "reserved_buying_power_impact_usd": float(
                        adaptive_claim.buying_power_impact_usd
                    ),
                    "correlation_cluster_id": adaptive_claim.correlation_cluster_id,
                }
                if adaptive_claim is not None
                else {}
            ),
            "role_metadata": role_meta,
            "account_risk_reservation": detail,
        },
        account_scope=scope,
    )
    if not acquired.get("ok"):
        return {
            **acquired,
            "reason": acquired.get("reason") or "reservation_commit_failed",
            **detail,
        }
    return {**acquired, **detail}


def _with_short_session(fn: Callable[[Session], Any]) -> Any:
    from ....db import SessionLocal

    db = SessionLocal()
    try:
        result = fn(db)
        db.commit()
        return result
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        db.close()


def acquire_action_claim_committed(**kwargs: Any) -> dict[str, Any]:
    """Acquire and commit in an independent short transaction before broker HTTP."""
    scope = str(kwargs.get("account_scope") or "").strip().lower()
    if scope.startswith("alpaca:") and scope != "alpaca:paper":
        return {"ok": False, "reason": "alpaca_account_scope_not_certified"}
    if scope == "alpaca:paper" and not bool(
        getattr(settings, "chili_alpaca_paper", True)
    ):
        return {"ok": False, "reason": "alpaca_live_posture_not_certified"}
    try:
        return _with_short_session(lambda db: acquire_action_claim(db, **kwargs))
    except Exception:
        return {"ok": False, "reason": "claim_commit_failed"}


def read_action_claim_committed(**kwargs: Any) -> tuple[bool, dict[str, Any] | None]:
    """Read one durable claim in an independent short transaction."""
    try:
        readable, claim = _with_short_session(
            lambda db: read_action_claim(db, **kwargs)
        )
        return bool(readable), claim
    except Exception:
        return False, None


def prepare_deadman_close_handoff_committed(**kwargs: Any) -> dict[str, Any]:
    try:
        return dict(
            _with_short_session(
                lambda db: prepare_deadman_close_handoff(db, **kwargs)
            )
        )
    except Exception:
        _log.warning("[alpaca_claim] deadman close handoff prepare failed", exc_info=True)
        return {"ok": False, "reason": "deadman_close_handoff_prepare_commit_failed"}


def finalize_deadman_close_handoff_request_committed(
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        return dict(
            _with_short_session(
                lambda db: finalize_deadman_close_handoff_request(db, **kwargs)
            )
        )
    except Exception:
        _log.warning("[alpaca_claim] deadman close request finalize failed", exc_info=True)
        return {"ok": False, "reason": "deadman_close_request_finalize_commit_failed"}


def retire_deadman_close_handoff_committed(**kwargs: Any) -> bool:
    try:
        return bool(
            _with_short_session(
                lambda db: retire_deadman_close_handoff(db, **kwargs)
            )
        )
    except Exception:
        _log.warning("[alpaca_claim] deadman close handoff retire failed", exc_info=True)
        return False


def retire_deadman_handoff_for_fractional_day_close_committed(
    **kwargs: Any,
) -> bool:
    try:
        return bool(
            _with_short_session(
                lambda db: retire_deadman_handoff_for_fractional_day_close(
                    db,
                    **kwargs,
                )
            )
        )
    except Exception:
        _log.warning(
            "[alpaca_claim] fractional DAY-close handoff retire failed",
            exc_info=True,
        )
        return False


def lease_deadman_handoff_replacement_committed(
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        return dict(
            _with_short_session(
                lambda db: lease_deadman_handoff_replacement(db, **kwargs)
            )
        )
    except Exception:
        _log.warning("[alpaca_claim] replacement deadman lease failed", exc_info=True)
        return {"ok": False, "reason": "replacement_deadman_lease_commit_failed"}


def certify_deadman_handoff_reprotected_committed(**kwargs: Any) -> bool:
    try:
        return bool(
            _with_short_session(
                lambda db: certify_deadman_handoff_reprotected(db, **kwargs)
            )
        )
    except Exception:
        _log.warning(
            "[alpaca_claim] replacement deadman certification failed",
            exc_info=True,
        )
        return False


def reconcile_deadman_replacement_successor_committed(
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        return dict(
            _with_short_session(
                lambda db: reconcile_deadman_replacement_successor(db, **kwargs)
            )
        )
    except Exception:
        _log.warning(
            "[alpaca_claim] replacement successor reconcile failed",
            exc_info=True,
        )
        return {
            "ok": False,
            "reason": "replacement_successor_reconcile_commit_failed",
        }


def advance_deadman_replacement_quarantine_baseline_committed(
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        return dict(
            _with_short_session(
                lambda db: advance_deadman_replacement_quarantine_baseline(
                    db,
                    **kwargs,
                )
            )
        )
    except Exception:
        _log.warning(
            "[alpaca_claim] replacement quarantine baseline advance failed",
            exc_info=True,
        )
        return {
            "ok": False,
            "reason": "replacement_quarantine_baseline_commit_failed",
        }


def prepare_deadman_replacement_containment_committed(
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        return dict(
            _with_short_session(
                lambda db: prepare_deadman_replacement_containment(db, **kwargs)
            )
        )
    except Exception:
        _log.warning(
            "[alpaca_claim] replacement containment prepare failed",
            exc_info=True,
        )
        return {"ok": False, "reason": "replacement_containment_prepare_commit_failed"}


def activate_deadman_replacement_containment_committed(
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        return dict(
            _with_short_session(
                lambda db: activate_deadman_replacement_containment(db, **kwargs)
            )
        )
    except Exception:
        _log.warning(
            "[alpaca_claim] replacement containment activate failed",
            exc_info=True,
        )
        return {"ok": False, "reason": "replacement_containment_activate_commit_failed"}


def retire_deadman_handoff_reprotected_committed(**kwargs: Any) -> bool:
    try:
        return bool(
            _with_short_session(
                lambda db: retire_deadman_handoff_reprotected(db, **kwargs)
            )
        )
    except Exception:
        _log.warning(
            "[alpaca_claim] replacement deadman lineage retirement failed",
            exc_info=True,
        )
        return False


def reserve_alpaca_entry_risk_committed(**kwargs: Any) -> dict[str, Any]:
    """Commit an account-wide risk reservation; broker HTTP happens afterward."""
    if str(kwargs.get("account_scope") or "").strip().lower() != "alpaca:paper":
        return {"ok": False, "reason": "alpaca_account_scope_not_certified"}
    if not bool(getattr(settings, "chili_alpaca_paper", True)):
        return {"ok": False, "reason": "alpaca_live_posture_not_certified"}
    request = kwargs.get("order_request")
    request = request if isinstance(request, dict) else {}
    if (
        str(request.get("side") or "").strip().lower() != "buy"
        or str(request.get("position_intent") or "").strip().lower()
        != "buy_to_open"
        or str(request.get("time_in_force") or "").strip().lower() != "day"
        or alpaca_symbol_is_crypto_like(kwargs.get("symbol"))
        or alpaca_asset_class_is_crypto(request.get("asset_class"))
        or alpaca_asset_class_is_crypto(
            (kwargs.get("role_metadata") or {}).get("asset_class")
            if isinstance(kwargs.get("role_metadata"), dict)
            else None
        )
    ):
        return {"ok": False, "reason": "alpaca_equity_long_only"}
    try:
        return dict(_with_short_session(lambda db: _reserve_alpaca_entry_risk(db, **kwargs)))
    except Exception:
        _log.warning("[alpaca_claim] account risk reservation failed", exc_info=True)
        return {"ok": False, "reason": "reservation_commit_failed"}


def _certify_alpaca_owned_entry_posture(
    db: Session,
    *,
    broker_positions: list[dict[str, Any]],
    broker_orders: list[Any],
    account_scope: str,
    alpaca_account_id: str,
) -> dict[str, Any]:
    """Allow concurrent entries only when every broker exposure is CHILI-owned."""

    scope = str(account_scope or "").strip().lower()
    account_id = str(alpaca_account_id or "").strip()
    if scope != "alpaca:paper" or not account_id:
        return {"ok": False, "reason": "alpaca_account_identity_unfrozen"}
    rows = db.execute(text(
        "SELECT id, upper(symbol), execution_family, state, risk_snapshot_json "
        "FROM trading_automation_sessions "
        "WHERE mode = 'live' AND execution_family IN ('alpaca_spot', 'alpaca_short')"
    )).fetchall()
    claims = db.execute(text(
        "SELECT upper(symbol), owner_session_id, client_order_id, broker_order_id, "
        "metadata_json FROM broker_symbol_action_claims "
        "WHERE account_scope = :scope AND phase <> 'resolved'"
    ), {"scope": scope}).fetchall()

    expected_positions: dict[str, float] = {}
    allowed_order_ids: set[str] = set()
    allowed_client_ids: set[str] = set()
    active_order_keys = (
        "entry_order_id",
        "pyramid_order_id",
        "micropullback_reentry_order_id",
        "pullback_add_order_id",
        "flag_breakout_add_order_id",
        "scale_out_order_id",
    )
    for _sid, row_symbol, family, _state, snapshot in rows:
        snap = snapshot if isinstance(snapshot, dict) else {}
        live = snap.get("momentum_live_execution")
        live = live if isinstance(live, dict) else {}
        if (
            str(snap.get("alpaca_account_scope") or "").strip().lower() != scope
            or str(snap.get("alpaca_account_id") or "").strip() != account_id
            or str(family or "").strip().lower() != "alpaca_spot"
        ):
            # Rows with no economic footprint are harmless; exposed rows from a
            # different generation are an account-identity quarantine.
            if live.get("position") is not None or live.get("entry_submitted"):
                return {
                    "ok": False,
                    "reason": "alpaca_account_generation_mismatch",
                }
            continue
        position = live.get("position")
        if position is not None:
            if not isinstance(position, dict):
                return {"ok": False, "reason": "owned_position_unreadable"}
            try:
                qty = abs(float(position.get("quantity")))
            except (TypeError, ValueError):
                qty = math.nan
            if not math.isfinite(qty) or qty <= 0.0:
                return {"ok": False, "reason": "owned_position_unreadable"}
            sym = str(row_symbol or "").strip().upper()
            expected_positions[sym] = expected_positions.get(sym, 0.0) + qty
        for key in active_order_keys:
            oid = str(live.get(key) or "").strip()
            if oid:
                allowed_order_ids.add(oid)
        deadman = live.get("deadman_stop")
        if isinstance(deadman, dict):
            oid = str(deadman.get("order_id") or "").strip()
            cid = str(deadman.get("client_order_id") or "").strip()
            if oid:
                allowed_order_ids.add(oid)
            if cid:
                allowed_client_ids.add(cid)

    for _sym, _owner, cid, oid, metadata in claims:
        if cid:
            allowed_client_ids.add(str(cid))
        if oid:
            allowed_order_ids.add(str(oid))
        meta = metadata if isinstance(metadata, dict) else {}
        transport = meta.get("owner_transport")
        if isinstance(transport, dict):
            request = transport.get("order_request")
            request = request if isinstance(request, dict) else {}
            tcid = str(request.get("client_order_id") or "").strip()
            toid = str(transport.get("broker_order_id") or "").strip()
            if tcid:
                allowed_client_ids.add(tcid)
            if toid:
                allowed_order_ids.add(toid)

    observed_positions: dict[str, float] = {}
    try:
        for position in broker_positions:
            if not isinstance(position, dict):
                raise ValueError("broker position shape")
            sym = str(position.get("product_id") or "").strip().upper()
            qty = abs(float(position.get("qty")))
            if not sym or not math.isfinite(qty) or qty <= 0.0:
                raise ValueError("broker position value")
            observed_positions[sym] = observed_positions.get(sym, 0.0) + qty
    except (TypeError, ValueError):
        return {"ok": False, "reason": "alpaca_account_posture_unreadable"}
    all_symbols = set(expected_positions) | set(observed_positions)
    for sym in all_symbols:
        expected = expected_positions.get(sym, 0.0)
        observed = observed_positions.get(sym, 0.0)
        if abs(expected - observed) > max(1e-9, max(expected, observed) * 1e-9):
            return {
                "ok": False,
                "reason": "alpaca_broker_local_position_mismatch",
                "symbol": sym,
                "expected_quantity": expected,
                "observed_quantity": observed,
            }

    for order in broker_orders:
        if isinstance(order, dict):
            oid = str(order.get("order_id") or order.get("id") or "").strip()
            cid = str(order.get("client_order_id") or "").strip()
        else:
            oid = str(getattr(order, "order_id", "") or "").strip()
            cid = str(getattr(order, "client_order_id", "") or "").strip()
        if not ((oid and oid in allowed_order_ids) or (cid and cid in allowed_client_ids)):
            return {
                "ok": False,
                "reason": "alpaca_unowned_open_order_present",
                "broker_order_id": oid or None,
                "client_order_id": cid or None,
            }
    return {
        "ok": True,
        "reason": "broker_exposure_fully_owned",
        "position_count": len(broker_positions),
        "open_order_count": len(broker_orders),
        "owned_position_symbols": sorted(expected_positions),
    }


def certify_alpaca_owned_entry_posture_committed(**kwargs: Any) -> dict[str, Any]:
    """Read-only account-generation/ownership check for a fresh broker snapshot."""

    try:
        return dict(
            _with_short_session(
                lambda db: _certify_alpaca_owned_entry_posture(db, **kwargs)
            )
        )
    except Exception:
        _log.warning("[alpaca_claim] owned posture certification failed", exc_info=True)
        return {"ok": False, "reason": "alpaca_account_posture_unreadable"}


def release_entry_claim_pre_post_committed(**kwargs: Any) -> bool:
    """Commit the creator-generation, proven-no-HTTP entry release."""
    try:
        return bool(
            _with_short_session(lambda db: release_entry_claim_pre_post(db, **kwargs))
        )
    except Exception:
        _log.warning("[alpaca_claim] committed pre-post release failed", exc_info=True)
        return False


def release_entry_and_adaptive_reservation_pre_post_committed(
    **kwargs: Any,
) -> dict[str, Any]:
    """Commit both pre-HTTP releases or retain both for same-CID recovery."""

    reason = str(kwargs.get("reason") or "").strip()
    reservation_id = str(kwargs.get("reservation_id") or "").strip()
    try:
        return dict(
            _with_short_session(
                lambda db: release_entry_and_adaptive_reservation_pre_post(
                    db,
                    **kwargs,
                )
            )
        )
    except _CoordinatedPrePostReleaseBlocked as exc:
        _log.warning(
            "[alpaca_claim] coordinated pre-post release retained cid=%s blocker=%s",
            str(kwargs.get("client_order_id") or ""),
            exc.blocker,
        )
        return {
            "ok": False,
            "confirmed": False,
            "adaptive_released": False,
            "legacy_released": False,
            "reason": reason,
            "reservation_id": reservation_id or None,
            "release_blocker": exc.blocker,
        }
    except Exception:
        _log.warning(
            "[alpaca_claim] coordinated pre-post release transaction failed cid=%s",
            str(kwargs.get("client_order_id") or ""),
            exc_info=True,
        )
        return {
            "ok": False,
            "confirmed": False,
            "adaptive_released": False,
            "legacy_released": False,
            "reason": reason,
            "reservation_id": reservation_id or None,
            "release_blocker": "coordinated_release_transaction_failed",
        }


def mark_entry_transport_started_committed(**kwargs: Any) -> bool:
    """Commit the one-way transport-start fence immediately before HTTP."""
    try:
        return bool(
            _with_short_session(lambda db: mark_entry_transport_started(db, **kwargs))
        )
    except Exception:
        _log.warning("[alpaca_claim] committed transport-start failed", exc_info=True)
        return False


def update_action_claim_phase_committed(**kwargs: Any) -> bool:
    try:
        return bool(_with_short_session(lambda db: update_action_claim_phase(db, **kwargs)))
    except Exception:
        return False


def lease_owner_transport_committed(**kwargs: Any) -> dict[str, Any]:
    """Commit one retained-owner transport lease before broker HTTP."""
    try:
        return dict(_with_short_session(lambda db: lease_owner_transport(db, **kwargs)))
    except Exception:
        _log.warning("[alpaca_claim] owner transport lease failed", exc_info=True)
        return {"ok": False, "reason": "owner_transport_lease_commit_failed"}


def advance_owner_transport_committed(**kwargs: Any) -> bool:
    try:
        return bool(_with_short_session(lambda db: advance_owner_transport(db, **kwargs)))
    except Exception:
        _log.warning("[alpaca_claim] owner transport advance failed", exc_info=True)
        return False


def resolve_owner_transport_terminal_committed(**kwargs: Any) -> bool:
    try:
        return bool(
            _with_short_session(lambda db: resolve_owner_transport_terminal(db, **kwargs))
        )
    except Exception:
        _log.warning("[alpaca_claim] owner transport resolution failed", exc_info=True)
        return False


def release_owner_transport_pre_post_committed(**kwargs: Any) -> bool:
    try:
        return bool(
            _with_short_session(lambda db: release_owner_transport_pre_post(db, **kwargs))
        )
    except Exception:
        _log.warning("[alpaca_claim] owner transport pre-post release failed", exc_info=True)
        return False


def persist_orphan_close_request_committed(**kwargs: Any) -> bool:
    try:
        return bool(
            _with_short_session(lambda db: persist_orphan_close_request(db, **kwargs))
        )
    except Exception:
        return False


def bind_orphan_close_request_committed(**kwargs: Any) -> dict[str, Any]:
    try:
        return dict(
            _with_short_session(lambda db: bind_orphan_close_request(db, **kwargs))
        )
    except Exception:
        _log.warning("[alpaca_claim] orphan close bind failed", exc_info=True)
        return {"ok": False, "reason": "orphan_close_bind_commit_failed"}


def mark_orphan_close_transport_started_committed(**kwargs: Any) -> bool:
    try:
        return bool(
            _with_short_session(
                lambda db: mark_orphan_close_transport_started(db, **kwargs)
            )
        )
    except Exception:
        _log.warning("[alpaca_claim] orphan close start mark failed", exc_info=True)
        return False


def release_orphan_close_pre_post_committed(**kwargs: Any) -> bool:
    try:
        return bool(
            _with_short_session(
                lambda db: release_orphan_close_pre_post(db, **kwargs)
            )
        )
    except Exception:
        _log.warning("[alpaca_claim] orphan close pre-post release failed", exc_info=True)
        return False


def advance_orphan_close_claim_phase_committed(**kwargs: Any) -> bool:
    """Commit the exact close-only generation CAS after broker transport."""
    try:
        return bool(
            _with_short_session(
                lambda db: advance_orphan_close_claim_phase(db, **kwargs)
            )
        )
    except Exception:
        _log.warning("[alpaca_claim] orphan close phase CAS failed", exc_info=True)
        return False


def resolve_action_claim_committed(**kwargs: Any) -> bool:
    try:
        return bool(_with_short_session(lambda db: resolve_action_claim(db, **kwargs)))
    except Exception:
        return False


def guard_alpaca_entry_ownership(
    db: Session,
    *,
    symbol: str,
    execution_family: str,
    owner_session_id: int | None = None,
    account_scope: str | None = None,
) -> tuple[bool, dict[str, Any] | None, str | None]:
    """Cheap arm-time gate; the authoritative boundary uses a committed permit."""
    if str(execution_family or "").strip().lower() not in ALPACA_EXECUTION_FAMILIES:
        return True, None, None
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope=account_scope,
    )
    if not readable:
        return False, None, "symbol_action_claim_unreadable"
    if (
        claim is not None
        and claim["phase"] != RESOLVED
        and not (
            owner_session_id is not None
            and claim.get("owner_session_id") == int(owner_session_id)
            and claim.get("action") == "entry"
        )
    ):
        return False, claim, "symbol_action_claimed"
    return True, claim, None
