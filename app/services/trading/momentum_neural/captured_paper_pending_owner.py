"""Fail-closed activation of one captured Alpaca PAPER pending owner.

The initial admission path deliberately persists a non-runnable PREOWNER and
then promotes it to a ``queued_live`` PENDING_OWNER without creating risk,
opportunity, outbox, or transport authority.  This module is the only bridge
from that exact durable generation to the ordinary captured-session owner.

The bridge is intentionally narrow:

* reconstruct every typed initial-material and dispatch byte from the row;
* recompute the full pending projection instead of trusting its self-claims;
* hold the canonical account locks, action-claim row lock, and session row
  lock while rechecking expiry and process-lifetime service ownership; and
* install the final owner inside the caller's transaction before the FSM tick.

No provider, broker, reservation, opportunity, outbox, or order operation is
available from this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from sqlalchemy import text

from ....models.trading import TradingAutomationSession
from .adaptive_risk_account_lock import AdaptiveRiskAccountLockIdentity
from .alpaca_orphan_claims import read_action_claim
from .captured_paper_dispatcher import (
    CapturedPaperDispatchRequest,
    bind_captured_paper_session_owner,
)
from . import captured_paper_initial_admission as initial
from . import captured_paper_preowner_promotion as promotion


class CapturedPaperPendingOwnerError(RuntimeError):
    """One pending generation could not be proven safe to activate."""

    def __init__(self, reason: str):
        self.reason = str(reason or "captured_paper_pending_owner_unavailable")
        super().__init__(self.reason)


def _reject(reason: str) -> None:
    raise CapturedPaperPendingOwnerError(reason)


def _strict_utc_text(value: Any, reason: str) -> datetime:
    if not isinstance(value, str) or not value:
        _reject(reason)
    if not value.endswith("Z"):
        _reject(reason)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except (TypeError, ValueError) as exc:
        raise CapturedPaperPendingOwnerError(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _reject(reason)
    normalized = parsed.astimezone(timezone.utc)
    if normalized.isoformat().replace("+00:00", "Z") != value:
        _reject(reason)
    return normalized


def _aware_utc(value: Any, reason: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _reject(reason)
    try:
        if value.utcoffset() is None:
            _reject(reason)
    except Exception as exc:  # pragma: no cover - defensive tzinfo boundary
        raise CapturedPaperPendingOwnerError(reason) from exc
    return value.astimezone(timezone.utc)


def _assert_service_fence(assertion: Callable[[], None] | None) -> None:
    if not callable(assertion):
        _reject("pending_owner_activation_service_fence_unavailable")
    try:
        result = assertion()
    except Exception as exc:
        raise CapturedPaperPendingOwnerError(
            "pending_owner_activation_service_fence_lost"
        ) from exc
    if result is not None:
        _reject("pending_owner_activation_service_fence_invalid")


def _initial_material(raw: Any) -> initial.CapturedPaperInitialSessionMaterial:
    if type(raw) is not dict:
        _reject("pending_owner_initial_material_invalid")
    expected_keys = {
        "schema_version",
        "symbol",
        "user_id",
        "variant_id",
        "account_scope",
        "expected_account_id",
        "runtime_generation",
        "execution_family",
        "code_build_sha256",
        "config_sha256",
        "capture_receipt_sha256",
        "policy_sha256",
        "adaptive_policy_settings_projection",
        "settings_projection_sha256",
        "feature_flags_sha256",
        "adaptive_policy_provenance_sha256",
        "runner_risk_template",
        "runner_risk_template_sha256",
        "trigger_read_receipt_sha256",
        "captured_input_attestation_sha256",
        "captured_read_ids",
        "captured_read_inventory_sha256",
        "selection_receipt_sha256",
        "strategy_variant_sha256",
        "viability_snapshot_sha256",
        "decision_at",
        "expires_at",
        "material_sha256",
    }
    if set(raw) != expected_keys:
        _reject("pending_owner_initial_material_invalid")
    risk = raw.get("runner_risk_template")
    if type(risk) is not dict or set(risk) != {
        "schema_version",
        "payload",
        "payload_sha256",
        "source_receipt_sha256s",
        "template_sha256",
    }:
        _reject("pending_owner_initial_risk_template_invalid")
    try:
        template = initial.CapturedPaperInitialRunnerRiskTemplate(
            payload=risk["payload"],
            payload_sha256=risk["payload_sha256"],
            source_receipt_sha256s=risk["source_receipt_sha256s"],
            schema_version=risk["schema_version"],
        )
        if template.template_sha256 != risk["template_sha256"]:
            _reject("pending_owner_initial_risk_template_mismatch")
        if template.template_sha256 != raw["runner_risk_template_sha256"]:
            _reject("pending_owner_initial_risk_template_mismatch")
        material = initial.CapturedPaperInitialSessionMaterial(
            symbol=raw["symbol"],
            user_id=raw["user_id"],
            variant_id=raw["variant_id"],
            account_scope=raw["account_scope"],
            expected_account_id=raw["expected_account_id"],
            runtime_generation=raw["runtime_generation"],
            execution_family=raw["execution_family"],
            code_build_sha256=raw["code_build_sha256"],
            config_sha256=raw["config_sha256"],
            capture_receipt_sha256=raw["capture_receipt_sha256"],
            policy_sha256=raw["policy_sha256"],
            adaptive_policy_settings_projection=(
                raw["adaptive_policy_settings_projection"]
            ),
            settings_projection_sha256=raw["settings_projection_sha256"],
            feature_flags_sha256=raw["feature_flags_sha256"],
            adaptive_policy_provenance_sha256=(
                raw["adaptive_policy_provenance_sha256"]
            ),
            runner_risk_template=template,
            trigger_read_receipt_sha256=raw["trigger_read_receipt_sha256"],
            captured_input_attestation_sha256=(
                raw["captured_input_attestation_sha256"]
            ),
            captured_read_ids=tuple(raw["captured_read_ids"]),
            captured_read_inventory_sha256=(
                raw["captured_read_inventory_sha256"]
            ),
            selection_receipt_sha256=raw["selection_receipt_sha256"],
            strategy_variant_sha256=raw["strategy_variant_sha256"],
            viability_snapshot_sha256=raw["viability_snapshot_sha256"],
            decision_at=_strict_utc_text(
                raw["decision_at"], "pending_owner_initial_decision_clock_invalid"
            ),
            expires_at=_strict_utc_text(
                raw["expires_at"], "pending_owner_initial_expiry_clock_invalid"
            ),
            schema_version=raw["schema_version"],
        )
        material.verify()
    except CapturedPaperPendingOwnerError:
        raise
    except Exception as exc:
        raise CapturedPaperPendingOwnerError(
            "pending_owner_initial_material_invalid"
        ) from exc
    if (
        promotion._sha256_json(material.to_dict())
        != promotion._sha256_json(raw)
        or material.material_sha256 != raw["material_sha256"]
    ):
        _reject("pending_owner_initial_material_mismatch")
    return material


def _dispatch_request(raw: Any) -> CapturedPaperDispatchRequest:
    if type(raw) is not dict:
        _reject("pending_owner_dispatch_request_invalid")
    expected_keys = {
        "schema_version",
        "session_id",
        "symbol",
        "execution_family",
        "account_scope",
        "expected_account_id",
        "code_build_sha256",
        "config_sha256",
        "capture_receipt_sha256",
        "runtime_generation",
        "first_dip_policy_mode",
        "route_token",
        "provenance_sha256",
    }
    if set(raw) != expected_keys:
        _reject("pending_owner_dispatch_request_invalid")
    try:
        request = CapturedPaperDispatchRequest(
            session_id=raw["session_id"],
            symbol=raw["symbol"],
            execution_family=raw["execution_family"],
            account_scope=raw["account_scope"],
            expected_account_id=raw["expected_account_id"],
            code_build_sha256=raw["code_build_sha256"],
            config_sha256=raw["config_sha256"],
            capture_receipt_sha256=raw["capture_receipt_sha256"],
            runtime_generation=raw["runtime_generation"],
            first_dip_policy_mode=raw["first_dip_policy_mode"],
        )
        request.verify()
    except Exception as exc:
        raise CapturedPaperPendingOwnerError(
            "pending_owner_dispatch_request_invalid"
        ) from exc
    if (
        promotion._sha256_json(promotion._dispatch_payload(request))
        != promotion._sha256_json(raw)
    ):
        _reject("pending_owner_dispatch_request_mismatch")
    return request


@dataclass(frozen=True, slots=True)
class ValidatedCapturedPaperPendingOwner:
    """Reconstructed exact authority for one not-yet-final owner row."""

    session_id: int
    material: initial.CapturedPaperInitialSessionMaterial
    request: CapturedPaperDispatchRequest
    projection: promotion.CapturedPaperPendingOwnerProjection


def validate_captured_paper_pending_owner_inventory(
    session: Any,
    *,
    expected_account_id: str,
    expected_runtime_generation: str,
    expected_execution_family: str = initial.ALPACA_SPOT_EXECUTION_FAMILY,
) -> ValidatedCapturedPaperPendingOwner:
    """Purely validate one pending row; never read DB/provider/broker state."""

    snapshot = getattr(session, "risk_snapshot_json", None)
    if type(snapshot) is not dict:
        _reject("pending_owner_snapshot_invalid")
    if snapshot.get("captured_paper_session_owner") is not None:
        _reject("pending_owner_already_final")
    raw_material = snapshot.get(promotion.CAPTURED_PAPER_INITIAL_MATERIAL_KEY)
    raw_marker = snapshot.get(promotion.CAPTURED_PAPER_PENDING_OWNER_KEY)
    if type(raw_marker) is not dict:
        _reject("pending_owner_marker_invalid")
    material = _initial_material(raw_material)
    request = _dispatch_request(raw_marker.get("dispatch_request"))
    session_id = getattr(session, "id", None)
    variant_id = getattr(session, "variant_id", None)
    user_id = getattr(session, "user_id", None)
    if any(
        type(value) is not int or value <= 0
        for value in (session_id, variant_id, user_id)
    ):
        _reject("pending_owner_session_invalid")
    try:
        promotion._verify_dispatch_material_route(
            request,
            material=material,
            session_id=session_id,
        )
        arm = promotion._arm_from_legacy_marker(
            snapshot.get("confirmed_arm_generation")
        )
        preowner_marker = initial._preowner_marker(
            material,
            session_id=session_id,
            claim_token=material.material_sha256,
        )
        preowner = initial.CommittedCapturedPaperInitialPreowner(
            session_id=session_id,
            initial_material_sha256=material.material_sha256,
            preowner_marker=preowner_marker,
            claim_token=material.material_sha256,
            account_lock_identity=AdaptiveRiskAccountLockIdentity.for_scope(
                initial.ALPACA_PAPER_ACCOUNT_SCOPE
            ),
            created=True,
        )
        expected_projection = promotion.build_captured_paper_pending_owner_projection(
            material=material,
            preowner_receipt=preowner,
            dispatch_request=request,
            arm_token=arm.arm_token,
            confirmed_at=arm.confirmed_at,
        )
    except CapturedPaperPendingOwnerError:
        raise
    except Exception as exc:
        raise CapturedPaperPendingOwnerError(
            "pending_owner_projection_invalid"
        ) from exc
    canonical_snapshot = promotion._canonical_value(snapshot)
    if (
        promotion._sha256_json(canonical_snapshot)
        != promotion._sha256_json(
            promotion._canonical_value(expected_projection.risk_snapshot)
        )
        or promotion._sha256_json(raw_marker)
        != promotion._sha256_json(
            promotion._canonical_value(expected_projection.pending_owner_marker)
        )
    ):
        _reject("pending_owner_projection_mismatch")
    exact_row = {
        "id": session_id,
        "mode": getattr(session, "mode", None),
        "venue": getattr(session, "venue", None),
        "state": getattr(session, "state", None),
        "symbol": getattr(session, "symbol", None),
        "variant_id": variant_id,
        "user_id": user_id,
        "execution_family": getattr(session, "execution_family", None),
        "correlation_id": getattr(session, "correlation_id", None),
        "source_node_id": getattr(session, "source_node_id", None),
    }
    if (
        exact_row["mode"] != "live"
        or exact_row["venue"] != "alpaca"
        or exact_row["state"] != promotion.CAPTURED_PAPER_PENDING_OWNER_STATE
        or exact_row["symbol"] != material.symbol
        or exact_row["variant_id"] != material.variant_id
        or exact_row["user_id"] != material.user_id
        or exact_row["execution_family"] != material.execution_family
        or exact_row["correlation_id"] != material.material_sha256
        or exact_row["source_node_id"] != "captured_paper_preowner_promotion"
        or getattr(session, "ended_at", None) is not None
        or getattr(session, "allocation_decision_json", None) != {}
        or expected_account_id != material.expected_account_id
        or expected_runtime_generation != material.runtime_generation
        or expected_execution_family != material.execution_family
    ):
        _reject("pending_owner_inventory_scope_mismatch")
    return ValidatedCapturedPaperPendingOwner(
        session_id=session_id,
        material=material,
        request=request,
        projection=expected_projection,
    )


@dataclass(frozen=True, slots=True)
class ActivatedCapturedPaperSessionOwner:
    session_id: int
    request_provenance_sha256: str
    initial_material_sha256: str | None
    owner_marker: Mapping[str, Any]
    created_from_pending: bool


def activate_captured_paper_session_owner_before_tick(
    db: Any,
    *,
    request: CapturedPaperDispatchRequest,
    account_lock_identity: AdaptiveRiskAccountLockIdentity,
    assert_service_fence_held: Callable[[], None] | None,
) -> ActivatedCapturedPaperSessionOwner:
    """Install/revalidate the final owner under locks before the FSM tick."""

    if type(request) is not CapturedPaperDispatchRequest:
        _reject("pending_owner_activation_request_invalid")
    request.verify()
    expected_lock = AdaptiveRiskAccountLockIdentity.for_scope(
        initial.ALPACA_PAPER_ACCOUNT_SCOPE
    )
    if account_lock_identity != expected_lock:
        _reject("pending_owner_activation_account_lock_missing")
    in_transaction = getattr(db, "in_transaction", None)
    if not callable(in_transaction) or not in_transaction():
        _reject("pending_owner_activation_transaction_missing")
    _assert_service_fence(assert_service_fence_held)

    preliminary = (
        db.query(TradingAutomationSession)
        .populate_existing()
        .filter(TradingAutomationSession.id == request.session_id)
        .one_or_none()
    )
    if preliminary is None:
        _reject("pending_owner_activation_session_missing")
    preliminary_snapshot = getattr(preliminary, "risk_snapshot_json", None)
    preliminary_snapshot = (
        preliminary_snapshot if type(preliminary_snapshot) is dict else {}
    )
    if preliminary_snapshot.get("captured_paper_session_owner") is not None:
        _assert_service_fence(assert_service_fence_held)
        owner = bind_captured_paper_session_owner(
            db,
            request=request,
            account_lock_identity=account_lock_identity,
        )
        return ActivatedCapturedPaperSessionOwner(
            session_id=request.session_id,
            request_provenance_sha256=request.provenance_sha256,
            initial_material_sha256=None,
            owner_marker=owner,
            created_from_pending=False,
        )

    readable, claim = read_action_claim(
        db,
        symbol=request.symbol,
        account_scope=request.account_scope,
        for_update=True,
    )
    if not readable or claim is None:
        _reject("pending_owner_activation_action_claim_unavailable")
    locked = (
        db.query(TradingAutomationSession)
        .populate_existing()
        .filter(TradingAutomationSession.id == request.session_id)
        .with_for_update()
        .one_or_none()
    )
    if locked is None:
        _reject("pending_owner_activation_session_missing")
    locked_snapshot = getattr(locked, "risk_snapshot_json", None)
    locked_snapshot = locked_snapshot if type(locked_snapshot) is dict else {}
    if locked_snapshot.get("captured_paper_session_owner") is not None:
        _assert_service_fence(assert_service_fence_held)
        owner = bind_captured_paper_session_owner(
            db,
            request=request,
            account_lock_identity=account_lock_identity,
        )
        return ActivatedCapturedPaperSessionOwner(
            session_id=request.session_id,
            request_provenance_sha256=request.provenance_sha256,
            initial_material_sha256=None,
            owner_marker=owner,
            created_from_pending=False,
        )
    pending = validate_captured_paper_pending_owner_inventory(
        locked,
        expected_account_id=request.expected_account_id,
        expected_runtime_generation=request.runtime_generation,
        expected_execution_family=request.execution_family,
    )
    if pending.request.provenance_sha256 != request.provenance_sha256:
        _reject("pending_owner_activation_request_mismatch")
    arm = pending.projection.arm
    metadata = promotion._canonical_value(
        pending.projection.action_claim_metadata
    )
    lease = claim.get("lease_expires_at")
    try:
        lease_utc = _aware_utc(
            lease, "pending_owner_activation_claim_lease_invalid"
        )
    except CapturedPaperPendingOwnerError:
        raise
    if (
        claim.get("account_scope") != request.account_scope
        or claim.get("symbol") != request.symbol
        or claim.get("claim_token") != arm.symbol_claim_token
        or claim.get("action") != "entry"
        or claim.get("phase") != "claimed"
        or claim.get("owner_session_id") != request.session_id
        or claim.get("client_order_id") is not None
        or claim.get("broker_order_id") is not None
        or claim.get("resolved_at") is not None
        or promotion._sha256_json(
            promotion._canonical_value(dict(claim.get("metadata") or {}))
        )
        != promotion._sha256_json(metadata)
        or lease_utc < arm.expires_at
    ):
        _reject("pending_owner_activation_action_claim_mismatch")
    try:
        now = _aware_utc(
            db.execute(text("SELECT clock_timestamp()")).scalar_one(),
            "pending_owner_activation_clock_unavailable",
        )
    except CapturedPaperPendingOwnerError:
        raise
    except Exception as exc:
        raise CapturedPaperPendingOwnerError(
            "pending_owner_activation_clock_unavailable"
        ) from exc
    if not (
        pending.material.decision_at <= now < pending.material.expires_at
        and arm.confirmed_at <= now < arm.expires_at
        and now < lease_utc
    ):
        _reject("pending_owner_activation_authority_expired")
    _assert_service_fence(assert_service_fence_held)
    owner = bind_captured_paper_session_owner(
        db,
        request=request,
        account_lock_identity=account_lock_identity,
    )
    return ActivatedCapturedPaperSessionOwner(
        session_id=request.session_id,
        request_provenance_sha256=request.provenance_sha256,
        initial_material_sha256=pending.material.material_sha256,
        owner_marker=owner,
        created_from_pending=True,
    )


__all__ = [
    "ActivatedCapturedPaperSessionOwner",
    "CapturedPaperPendingOwnerError",
    "ValidatedCapturedPaperPendingOwner",
    "activate_captured_paper_session_owner_before_tick",
    "validate_captured_paper_pending_owner_inventory",
]
