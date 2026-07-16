"""Atomic PREOWNER -> PENDING_OWNER promotion for captured Alpaca PAPER.

This boundary is intentionally smaller than a runner invocation.  It converts
one exact, non-runnable initial PREOWNER generation into one ``queued_live``
PENDING_OWNER generation while both canonical account advisory locks and the
action-claim/session row locks are held.  It does not read a provider, construct
an adapter, consume a setup opportunity, reserve risk, create an outbox row, or
POST an order.

The action claim retains the legacy exact ``live_arm_reserved`` metadata shape
so every existing pre-HTTP recovery/terminalization consumer can recognize it.
All captured-PAPER provenance lives in the session's PENDING_OWNER marker and
event.  A later dedicated dispatcher must validate that marker and atomically
install the final durable owner before it invokes the captured first tick; no
generic dispatcher receives a final-owner marker from this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ....models.trading import TradingAutomationEvent, TradingAutomationSession
from .adaptive_risk_account_lock import (
    AccountRiskRowLockStage,
    AdaptiveRiskAccountLockIdentity,
    CanonicalAccountRiskRowLockGuard,
    acquire_adaptive_risk_account_locks,
)
from .alpaca_orphan_claims import read_action_claim
from .captured_paper_dispatcher import CapturedPaperDispatchRequest
from .captured_paper_entry_intent import (
    CapturedPaperConfirmedArmGeneration,
    CapturedPaperIntentContractError,
)
from . import captured_paper_initial_admission as initial


PENDING_OWNER_SCHEMA_VERSION = "chili.captured-paper-pending-owner.v1"
PENDING_OWNER_BINDING_SCHEMA_VERSION = (
    "chili.captured-paper-pending-owner-binding.v1"
)
PENDING_OWNER_RISK_SNAPSHOT_SCHEMA_VERSION = (
    "chili.captured-paper-pending-owner-risk-snapshot.v1"
)
PENDING_OWNER_RECEIPT_SCHEMA_VERSION = (
    "chili.captured-paper-pending-owner-receipt.v1"
)

CAPTURED_PAPER_PENDING_OWNER_STAGE = "captured_paper_pending_owner"
CAPTURED_PAPER_PENDING_OWNER_STATE = "queued_live"
CAPTURED_PAPER_PENDING_OWNER_KEY = "captured_paper_session_pending_owner"
CAPTURED_PAPER_INITIAL_MATERIAL_KEY = "captured_paper_initial_material"
CAPTURED_PAPER_CONFIRMED_ARM_SHA256_KEY = (
    "captured_paper_confirmed_arm_generation_sha256"
)

_SOURCE_NODE = "captured_paper_preowner_promotion"
_INTENDED_FIRST_DIP_POLICY_MODE = "candidate"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PENDING_BINDING_FIELDS = (
    "session_id",
    "symbol",
    "variant_id",
    "account_scope",
    "expected_account_id",
    "runtime_generation",
    "execution_family",
    "initial_material_sha256",
    "preowner_marker_sha256",
    "preowner_claim_token",
    "arm_claim_token",
    "confirmed_arm_generation_sha256",
    "dispatch_provenance_sha256",
    "code_build_sha256",
    "config_sha256",
    "capture_receipt_sha256",
    "adaptive_policy_sha256",
    "settings_projection_sha256",
    "feature_flags_sha256",
    "adaptive_policy_provenance_sha256",
    "runner_risk_template_sha256",
    "strategy_variant_sha256",
    "viability_snapshot_sha256",
    "confirmed_at",
    "expires_at",
)


class CapturedPaperPreownerPromotionError(RuntimeError):
    """The exact PREOWNER generation could not be promoted safely."""

    def __init__(self, reason: str):
        self.reason = str(reason or "captured_paper_preowner_promotion_rejected")
        super().__init__(self.reason)


def _reject(reason: str) -> None:
    raise CapturedPaperPreownerPromotionError(reason)


def _sha(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _reject(f"{field_name}_invalid")
    return value


def _positive_int(value: Any, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        _reject(f"{field_name}_invalid")
    return value


def _canonical_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        _reject(f"{field_name}_invalid")
    try:
        canonical = str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        _reject(f"{field_name}_invalid")
    if canonical != value:
        _reject(f"{field_name}_invalid")
    return canonical


def _aware_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _reject(f"{field_name}_invalid")
    try:
        offset = value.utcoffset()
    except Exception as exc:  # pragma: no cover - defensive tzinfo boundary
        raise CapturedPaperPreownerPromotionError(
            f"{field_name}_invalid"
        ) from exc
    if offset is None:
        _reject(f"{field_name}_invalid")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _reject("pending_owner_nonfinite_json")
        return value
    if isinstance(value, datetime):
        return _iso(_aware_utc(value, "pending_owner_json_clock"))
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            _reject("pending_owner_json_key_invalid")
        return {
            key: _canonical_value(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    _reject("pending_owner_json_value_invalid")


def _sha256_json(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            _canonical_value(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze(item) for key, item in sorted(value.items())}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _dispatch_payload(request: CapturedPaperDispatchRequest) -> dict[str, Any]:
    if type(request) is not CapturedPaperDispatchRequest:
        _reject("pending_owner_dispatch_request_invalid")
    try:
        request.verify()
    except Exception as exc:
        raise CapturedPaperPreownerPromotionError(
            "pending_owner_dispatch_request_invalid"
        ) from exc
    return {
        "schema_version": "chili.captured-paper-dispatch-request.v1",
        "session_id": request.session_id,
        "symbol": request.symbol,
        "execution_family": request.execution_family,
        "account_scope": request.account_scope,
        "expected_account_id": request.expected_account_id,
        "code_build_sha256": request.code_build_sha256,
        "config_sha256": request.config_sha256,
        "capture_receipt_sha256": request.capture_receipt_sha256,
        "runtime_generation": request.runtime_generation,
        "first_dip_policy_mode": request.first_dip_policy_mode,
        "route_token": request.route_token.to_payload(),
        "provenance_sha256": request.provenance_sha256,
    }


def _verify_dispatch_material_route(
    request: CapturedPaperDispatchRequest,
    *,
    material: initial.CapturedPaperInitialSessionMaterial,
    session_id: int,
) -> dict[str, Any]:
    payload = _dispatch_payload(request)
    exact = {
        "session_id": session_id,
        "symbol": material.symbol,
        "execution_family": material.execution_family,
        "account_scope": material.account_scope,
        "expected_account_id": material.expected_account_id,
        "code_build_sha256": material.code_build_sha256,
        "config_sha256": material.config_sha256,
        "capture_receipt_sha256": material.capture_receipt_sha256,
        "runtime_generation": material.runtime_generation,
        "first_dip_policy_mode": _INTENDED_FIRST_DIP_POLICY_MODE,
    }
    if any(payload.get(name) != value for name, value in exact.items()):
        _reject("pending_owner_dispatch_material_mismatch")
    return payload


def _verify_preowner_receipt(
    receipt: initial.CommittedCapturedPaperInitialPreowner,
    *,
    material: initial.CapturedPaperInitialSessionMaterial,
) -> tuple[int, dict[str, Any]]:
    if type(receipt) is not initial.CommittedCapturedPaperInitialPreowner:
        _reject("pending_owner_preowner_receipt_invalid")
    session_id = _positive_int(receipt.session_id, "pending_owner_session_id")
    expected_lock_identity = AdaptiveRiskAccountLockIdentity.for_scope(
        initial.ALPACA_PAPER_ACCOUNT_SCOPE
    )
    if (
        receipt.schema_version != initial.INITIAL_PREOWNER_RECEIPT_SCHEMA_VERSION
        or receipt.initial_material_sha256 != material.material_sha256
        or receipt.claim_token != material.material_sha256
        or receipt.account_lock_identity != expected_lock_identity
        or receipt.receipt_sha256 != initial._sha256_json(receipt.to_body())
    ):
        _reject("pending_owner_preowner_receipt_mismatch")
    expected_marker = initial._preowner_marker(
        material,
        session_id=session_id,
        claim_token=material.material_sha256,
    )
    if dict(receipt.preowner_marker) != expected_marker:
        _reject("pending_owner_preowner_marker_mismatch")
    return session_id, expected_marker


def _verified_promotion_time(
    material: initial.CapturedPaperInitialSessionMaterial,
    verification_at: datetime,
) -> datetime:
    try:
        verified = initial._verify_material_time(material, verification_at)
    except initial.CapturedPaperInitialAdmissionError as exc:
        raise CapturedPaperPreownerPromotionError(exc.reason) from exc
    # CapturedPaperConfirmedArmGeneration requires a strictly positive remaining
    # authority interval.  Equality is acceptable for a read receipt but cannot
    # create execution authority.
    if verified >= material.expires_at:
        _reject("pending_owner_material_expired")
    return verified


def _assert_service_fence_held(
    assertion: Callable[[], None] | None,
) -> None:
    if not callable(assertion):
        _reject("pending_owner_service_fence_capability_unavailable")
    try:
        result = assertion()
    except Exception as exc:
        raise CapturedPaperPreownerPromotionError(
            "pending_owner_service_fence_not_held"
        ) from exc
    if result is not None:
        _reject("pending_owner_service_fence_assertion_invalid")


def _locked_database_clock(db: Session) -> datetime:
    """Read the live commit frontier after every potentially blocking lock."""

    try:
        value = db.execute(text("SELECT clock_timestamp()")).scalar_one()
    except Exception as exc:
        raise CapturedPaperPreownerPromotionError(
            "pending_owner_commit_clock_unavailable"
        ) from exc
    return _aware_utc(value, "pending_owner_commit_clock")


def _legacy_confirmed_arm_marker(
    arm: CapturedPaperConfirmedArmGeneration,
) -> dict[str, Any]:
    arm.verify()
    return {
        "version": 1,
        "session_id": arm.session_id,
        "arm_token": arm.arm_token,
        "expires_at_utc": arm.expires_at.isoformat(),
        "alpaca_symbol_claim_token": arm.symbol_claim_token,
        "alpaca_account_scope": arm.account_scope,
        "alpaca_account_id": arm.expected_account_id,
        "confirmed_at_utc": arm.confirmed_at.isoformat(),
    }


def _arm_from_legacy_marker(
    marker: Any,
) -> CapturedPaperConfirmedArmGeneration:
    if type(marker) is not dict:
        _reject("pending_owner_confirmed_arm_marker_invalid")
    try:
        arm = CapturedPaperConfirmedArmGeneration(
            session_id=marker["session_id"],
            arm_token=marker["arm_token"],
            expires_at=datetime.fromisoformat(marker["expires_at_utc"]),
            symbol_claim_token=marker["alpaca_symbol_claim_token"],
            account_scope=marker["alpaca_account_scope"],
            expected_account_id=marker["alpaca_account_id"],
            confirmed_at=datetime.fromisoformat(marker["confirmed_at_utc"]),
        )
    except (KeyError, TypeError, ValueError, CapturedPaperIntentContractError) as exc:
        raise CapturedPaperPreownerPromotionError(
            "pending_owner_confirmed_arm_marker_invalid"
        ) from exc
    if marker != _legacy_confirmed_arm_marker(arm):
        _reject("pending_owner_confirmed_arm_marker_invalid")
    return arm


@dataclass(frozen=True, slots=True)
class CapturedPaperPendingOwnerProjection:
    """Pure exact bytes to be committed by the promotion transaction."""

    session_id: int
    arm: CapturedPaperConfirmedArmGeneration
    dispatch_request_payload: Mapping[str, Any]
    binding_sha256: str
    action_claim_metadata: Mapping[str, Any]
    action_claim_metadata_sha256: str
    pending_owner_marker: Mapping[str, Any]
    risk_snapshot: Mapping[str, Any]
    projection_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _positive_int(self.session_id, "pending_owner_session_id")
        if type(self.arm) is not CapturedPaperConfirmedArmGeneration:
            _reject("pending_owner_arm_invalid")
        self.arm.verify()
        _sha(self.binding_sha256, "pending_owner_binding_sha256")
        _sha(
            self.action_claim_metadata_sha256,
            "pending_owner_claim_metadata_sha256",
        )
        dispatch = _canonical_value(self.dispatch_request_payload)
        claim = _canonical_value(self.action_claim_metadata)
        marker = _canonical_value(self.pending_owner_marker)
        snapshot = _canonical_value(self.risk_snapshot)
        if not all(type(value) is dict for value in (dispatch, claim, marker, snapshot)):
            _reject("pending_owner_projection_payload_invalid")
        if _sha256_json(claim) != self.action_claim_metadata_sha256:
            _reject("pending_owner_claim_metadata_hash_mismatch")
        if marker.get("content_sha256") != _sha256_json(
            {key: value for key, value in marker.items() if key != "content_sha256"}
        ):
            _reject("pending_owner_marker_hash_mismatch")
        try:
            binding_body = {
                "schema_version": PENDING_OWNER_BINDING_SCHEMA_VERSION,
                **{name: marker[name] for name in _PENDING_BINDING_FIELDS},
            }
        except KeyError as exc:
            raise CapturedPaperPreownerPromotionError(
                "pending_owner_binding_incomplete"
            ) from exc
        if (
            marker.get("schema_version") != PENDING_OWNER_SCHEMA_VERSION
            or marker.get("stage") != CAPTURED_PAPER_PENDING_OWNER_STAGE
            or marker.get("binding_sha256") != self.binding_sha256
            or _sha256_json(binding_body) != self.binding_sha256
            or marker.get("dispatch_request") != dispatch
            or marker.get("dispatch_provenance_sha256")
            != dispatch.get("provenance_sha256")
            or marker.get("action_claim_metadata_sha256")
            != self.action_claim_metadata_sha256
            or marker.get("confirmed_arm_generation")
            != self.arm.to_payload()
            or marker.get("arm_claim_token") != self.arm.symbol_claim_token
            or any(
                marker.get(name) is not False
                for name in (
                    "opportunity_consumed",
                    "risk_reserved",
                    "outbox_created",
                    "order_posted",
                )
            )
            or marker.get("broker_order_post_calls") != 0
        ):
            _reject("pending_owner_projection_binding_mismatch")
        if (
            snapshot.get(CAPTURED_PAPER_PENDING_OWNER_KEY) != marker
            or snapshot.get("confirmed_arm_generation")
            != _legacy_confirmed_arm_marker(self.arm)
            or snapshot.get(CAPTURED_PAPER_CONFIRMED_ARM_SHA256_KEY)
            != self.arm.confirmed_arm_generation_sha256
            or snapshot.get("alpaca_symbol_claim_token")
            != self.arm.symbol_claim_token
            or any(
                name in snapshot
                for name in (
                    "captured_paper_session_preowner",
                    "captured_paper_session_owner",
                    "momentum_live_execution",
                )
            )
        ):
            _reject("pending_owner_snapshot_binding_mismatch")
        object.__setattr__(self, "dispatch_request_payload", _freeze(dispatch))
        object.__setattr__(self, "action_claim_metadata", _freeze(claim))
        object.__setattr__(self, "pending_owner_marker", _freeze(marker))
        object.__setattr__(self, "risk_snapshot", _freeze(snapshot))
        object.__setattr__(
            self,
            "projection_sha256",
            _sha256_json(
                {
                    "session_id": self.session_id,
                    "arm": self.arm.to_payload(),
                    "dispatch_request": dispatch,
                    "binding_sha256": self.binding_sha256,
                    "action_claim_metadata": claim,
                    "action_claim_metadata_sha256": (
                        self.action_claim_metadata_sha256
                    ),
                    "pending_owner_marker": marker,
                    "risk_snapshot": snapshot,
                }
            ),
        )


def build_captured_paper_pending_owner_projection(
    *,
    material: initial.CapturedPaperInitialSessionMaterial,
    preowner_receipt: initial.CommittedCapturedPaperInitialPreowner,
    dispatch_request: CapturedPaperDispatchRequest,
    arm_token: str,
    confirmed_at: datetime,
) -> CapturedPaperPendingOwnerProjection:
    """Build, but do not persist, one exact PENDING_OWNER generation."""

    if type(material) is not initial.CapturedPaperInitialSessionMaterial:
        _reject("pending_owner_material_invalid")
    material.verify()
    session_id, preowner_marker = _verify_preowner_receipt(
        preowner_receipt,
        material=material,
    )
    confirmed = _verified_promotion_time(material, confirmed_at)
    dispatch = _verify_dispatch_material_route(
        dispatch_request,
        material=material,
        session_id=session_id,
    )
    canonical_arm_token = _canonical_uuid(arm_token, "pending_owner_arm_token")
    try:
        arm = CapturedPaperConfirmedArmGeneration(
            session_id=session_id,
            arm_token=canonical_arm_token,
            expires_at=material.expires_at,
            symbol_claim_token=f"arm-{canonical_arm_token}",
            account_scope=material.account_scope,
            expected_account_id=material.expected_account_id,
            confirmed_at=confirmed,
        )
    except CapturedPaperIntentContractError as exc:
        raise CapturedPaperPreownerPromotionError(
            "pending_owner_arm_invalid"
        ) from exc

    binding_body = {
        "schema_version": PENDING_OWNER_BINDING_SCHEMA_VERSION,
        "session_id": session_id,
        "symbol": material.symbol,
        "variant_id": material.variant_id,
        "account_scope": material.account_scope,
        "expected_account_id": material.expected_account_id,
        "runtime_generation": material.runtime_generation,
        "execution_family": material.execution_family,
        "initial_material_sha256": material.material_sha256,
        "preowner_marker_sha256": preowner_marker["content_sha256"],
        "preowner_claim_token": material.material_sha256,
        "arm_claim_token": arm.symbol_claim_token,
        "confirmed_arm_generation_sha256": (
            arm.confirmed_arm_generation_sha256
        ),
        "dispatch_provenance_sha256": dispatch_request.provenance_sha256,
        "code_build_sha256": material.code_build_sha256,
        "config_sha256": material.config_sha256,
        "capture_receipt_sha256": material.capture_receipt_sha256,
        "adaptive_policy_sha256": material.policy_sha256,
        "settings_projection_sha256": material.settings_projection_sha256,
        "feature_flags_sha256": material.feature_flags_sha256,
        "adaptive_policy_provenance_sha256": (
            material.adaptive_policy_provenance_sha256
        ),
        "runner_risk_template_sha256": (
            material.runner_risk_template.template_sha256
        ),
        "strategy_variant_sha256": material.strategy_variant_sha256,
        "viability_snapshot_sha256": material.viability_snapshot_sha256,
        "confirmed_at": arm.confirmed_at.isoformat(),
        "expires_at": arm.expires_at.isoformat(),
    }
    binding_sha256 = _sha256_json(binding_body)
    # Compatibility is safety here: live_runner's pre-HTTP release/recovery
    # predicates intentionally recognize this exact three-key shape.  Putting
    # captured provenance on the claim would strand an otherwise provably
    # never-submitted claim.  The full binding remains hash-bound in ``marker``.
    claim_metadata = {
        "stage": "live_arm_reserved",
        "variant_id": material.variant_id,
        "alpaca_account_id": material.expected_account_id,
    }
    claim_metadata_sha256 = _sha256_json(claim_metadata)
    marker_body = {
        **binding_body,
        "schema_version": PENDING_OWNER_SCHEMA_VERSION,
        "stage": CAPTURED_PAPER_PENDING_OWNER_STAGE,
        "binding_sha256": binding_sha256,
        "dispatch_request": dispatch,
        "action_claim_metadata_sha256": claim_metadata_sha256,
        "confirmed_arm_generation": arm.to_payload(),
        "opportunity_consumed": False,
        "risk_reserved": False,
        "outbox_created": False,
        "order_posted": False,
        "broker_order_post_calls": 0,
    }
    marker = {**marker_body, "content_sha256": _sha256_json(marker_body)}
    legacy_arm = _legacy_confirmed_arm_marker(arm)
    runner_template = material.runner_risk_template.to_dict()
    runner_payload = _canonical_value(material.runner_risk_template.payload)
    risk_snapshot = {
        "schema_version": PENDING_OWNER_RISK_SNAPSHOT_SCHEMA_VERSION,
        **runner_payload,
        "arm_token": arm.arm_token,
        "expires_at_utc": arm.expires_at.isoformat(),
        "arm_confirmed": True,
        "arm_confirmed_at_utc": arm.confirmed_at.isoformat(),
        "live_eligible_at_utc": arm.confirmed_at.isoformat(),
        "alpaca_symbol_claim_token": arm.symbol_claim_token,
        "alpaca_account_scope": material.account_scope,
        "alpaca_account_id": material.expected_account_id,
        "captured_paper_runtime_generation": material.runtime_generation,
        "captured_paper_initial_material_sha256": material.material_sha256,
        CAPTURED_PAPER_INITIAL_MATERIAL_KEY: material.to_dict(),
        "captured_paper_settings_projection_sha256": (
            material.settings_projection_sha256
        ),
        "captured_paper_feature_flags_sha256": material.feature_flags_sha256,
        "captured_paper_adaptive_policy_sha256": material.policy_sha256,
        "captured_paper_adaptive_policy_provenance_sha256": (
            material.adaptive_policy_provenance_sha256
        ),
        "captured_paper_adaptive_policy_settings_projection": (
            _canonical_value(material.adaptive_policy_settings_projection)
        ),
        "captured_paper_initial_runner_risk_template_sha256": (
            material.runner_risk_template.template_sha256
        ),
        "captured_paper_initial_runner_risk_template": runner_template,
        CAPTURED_PAPER_CONFIRMED_ARM_SHA256_KEY: (
            arm.confirmed_arm_generation_sha256
        ),
        "confirmed_arm_generation": legacy_arm,
        CAPTURED_PAPER_PENDING_OWNER_KEY: marker,
    }
    return CapturedPaperPendingOwnerProjection(
        session_id=session_id,
        arm=arm,
        dispatch_request_payload=dispatch,
        binding_sha256=binding_sha256,
        action_claim_metadata=claim_metadata,
        action_claim_metadata_sha256=claim_metadata_sha256,
        pending_owner_marker=marker,
        risk_snapshot=risk_snapshot,
    )


def _expected_preowner_claim_metadata(
    material: initial.CapturedPaperInitialSessionMaterial,
    *,
    preowner_marker_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": initial.INITIAL_PREOWNER_MARKER_SCHEMA_VERSION,
        "stage": initial.CAPTURED_PAPER_PREOWNER_STATE,
        "initial_material_sha256": material.material_sha256,
        "expected_account_id": material.expected_account_id,
        "runtime_generation": material.runtime_generation,
        "code_build_sha256": material.code_build_sha256,
        "config_sha256": material.config_sha256,
        "capture_receipt_sha256": material.capture_receipt_sha256,
        "policy_sha256": material.policy_sha256,
        "settings_projection_sha256": material.settings_projection_sha256,
        "feature_flags_sha256": material.feature_flags_sha256,
        "adaptive_policy_provenance_sha256": (
            material.adaptive_policy_provenance_sha256
        ),
        "runner_risk_template_sha256": (
            material.runner_risk_template.template_sha256
        ),
        "preowner_marker_sha256": preowner_marker_sha256,
    }


def _validate_exact_preowner_locked(
    db: Session,
    *,
    material: initial.CapturedPaperInitialSessionMaterial,
    preowner_receipt: initial.CommittedCapturedPaperInitialPreowner,
    claim: Mapping[str, Any],
    lock_identity: AdaptiveRiskAccountLockIdentity,
) -> TradingAutomationSession:
    try:
        initial._validate_existing_preowner(
            db,
            material=material,
            claim=claim,
            claim_token=material.material_sha256,
            lock_identity=lock_identity,
        )
    except initial.CapturedPaperInitialAdmissionError as exc:
        raise CapturedPaperPreownerPromotionError(exc.reason) from exc
    session = db.get(TradingAutomationSession, preowner_receipt.session_id)
    if session is None:
        _reject("pending_owner_preowner_session_unavailable")
    expected_metadata = _expected_preowner_claim_metadata(
        material,
        preowner_marker_sha256=(
            preowner_receipt.preowner_marker["content_sha256"]
        ),
    )
    if dict(claim.get("metadata") or {}) != expected_metadata:
        _reject("pending_owner_preowner_claim_metadata_mismatch")
    return session


def _validate_existing_pending_locked(
    db: Session,
    *,
    material: initial.CapturedPaperInitialSessionMaterial,
    preowner_receipt: initial.CommittedCapturedPaperInitialPreowner,
    dispatch_request: CapturedPaperDispatchRequest,
    claim: Mapping[str, Any],
) -> tuple[TradingAutomationSession, CapturedPaperPendingOwnerProjection]:
    owner_session_id = claim.get("owner_session_id")
    if owner_session_id != preowner_receipt.session_id:
        _reject("pending_owner_claim_owner_mismatch")
    active_sessions = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.execution_family
            == material.execution_family,
            TradingAutomationSession.symbol == material.symbol,
            TradingAutomationSession.ended_at.is_(None),
        )
        .order_by(TradingAutomationSession.id.asc())
        .with_for_update()
        .populate_existing()
        .all()
    )
    if (
        len(active_sessions) != 1
        or int(active_sessions[0].id) != owner_session_id
    ):
        _reject("pending_owner_session_unavailable")
    session = active_sessions[0]
    snapshot = session.risk_snapshot_json
    if type(snapshot) is not dict:
        _reject("pending_owner_snapshot_invalid")
    arm = _arm_from_legacy_marker(snapshot.get("confirmed_arm_generation"))
    projection = build_captured_paper_pending_owner_projection(
        material=material,
        preowner_receipt=preowner_receipt,
        dispatch_request=dispatch_request,
        arm_token=arm.arm_token,
        confirmed_at=arm.confirmed_at,
    )
    try:
        initial._validate_variant_and_viability(db, material)
    except initial.CapturedPaperInitialAdmissionError as exc:
        raise CapturedPaperPreownerPromotionError(exc.reason) from exc
    claim_lease = claim.get("lease_expires_at")
    if not isinstance(claim_lease, datetime) or claim_lease.tzinfo is None:
        _reject("pending_owner_claim_lease_invalid")
    claim_lease = claim_lease.astimezone(timezone.utc)
    if (
        session.mode != "live"
        or session.venue != "alpaca"
        or session.execution_family != material.execution_family
        or session.state != CAPTURED_PAPER_PENDING_OWNER_STATE
        or session.symbol != material.symbol
        or int(session.variant_id or 0) != material.variant_id
        or int(session.user_id or 0) != material.user_id
        or session.ended_at is not None
        or snapshot != _canonical_value(projection.risk_snapshot)
        or session.allocation_decision_json != {}
        or session.correlation_id != material.material_sha256
        or session.source_node_id != _SOURCE_NODE
        or claim.get("phase") != "claimed"
        or claim.get("action") != "entry"
        or claim.get("claim_token") != projection.arm.symbol_claim_token
        or claim.get("client_order_id") is not None
        or claim.get("broker_order_id") is not None
        or claim_lease < projection.arm.expires_at
        or dict(claim.get("metadata") or {})
        != _canonical_value(projection.action_claim_metadata)
    ):
        _reject("pending_owner_existing_generation_mismatch")
    return session, projection


@dataclass(frozen=True, slots=True)
class PromotedCapturedPaperPendingOwner:
    """Content-addressed acknowledgement of one atomic promotion."""

    session_id: int
    initial_material_sha256: str
    arm_token: str
    arm_claim_token: str
    confirmed_arm_generation_sha256: str
    dispatch_provenance_sha256: str
    pending_owner_marker: Mapping[str, Any]
    projection_sha256: str
    account_lock_identity: AdaptiveRiskAccountLockIdentity
    created: bool
    receipt_sha256: str = field(init=False)
    schema_version: str = PENDING_OWNER_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _positive_int(self.session_id, "pending_owner_session_id")
        for name in (
            "initial_material_sha256",
            "confirmed_arm_generation_sha256",
            "dispatch_provenance_sha256",
            "projection_sha256",
        ):
            _sha(getattr(self, name), name)
        token = _canonical_uuid(self.arm_token, "pending_owner_arm_token")
        if self.arm_claim_token != f"arm-{token}":
            _reject("pending_owner_arm_claim_token_invalid")
        if type(self.account_lock_identity) is not AdaptiveRiskAccountLockIdentity:
            _reject("pending_owner_account_lock_identity_invalid")
        if type(self.created) is not bool:
            _reject("pending_owner_created_invalid")
        marker = _canonical_value(self.pending_owner_marker)
        if type(marker) is not dict:
            _reject("pending_owner_marker_invalid")
        object.__setattr__(self, "pending_owner_marker", _freeze(marker))
        object.__setattr__(self, "receipt_sha256", _sha256_json(self.to_body()))

    def to_body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "initial_material_sha256": self.initial_material_sha256,
            "arm_token": self.arm_token,
            "arm_claim_token": self.arm_claim_token,
            "confirmed_arm_generation_sha256": (
                self.confirmed_arm_generation_sha256
            ),
            "dispatch_provenance_sha256": self.dispatch_provenance_sha256,
            "pending_owner_marker": _canonical_value(
                self.pending_owner_marker
            ),
            "projection_sha256": self.projection_sha256,
            "account_lock_identity": {
                "schema_version": self.account_lock_identity.schema_version,
                "account_scope": self.account_lock_identity.account_scope,
                "action_advisory_key": (
                    self.account_lock_identity.action_advisory_key
                ),
                "adaptive_advisory_namespace": (
                    self.account_lock_identity.adaptive_advisory_namespace
                ),
            },
            "created": self.created,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.to_body(), "receipt_sha256": self.receipt_sha256}


def _receipt(
    projection: CapturedPaperPendingOwnerProjection,
    *,
    material: initial.CapturedPaperInitialSessionMaterial,
    request: CapturedPaperDispatchRequest,
    lock_identity: AdaptiveRiskAccountLockIdentity,
    created: bool,
) -> PromotedCapturedPaperPendingOwner:
    return PromotedCapturedPaperPendingOwner(
        session_id=projection.session_id,
        initial_material_sha256=material.material_sha256,
        arm_token=projection.arm.arm_token,
        arm_claim_token=projection.arm.symbol_claim_token,
        confirmed_arm_generation_sha256=(
            projection.arm.confirmed_arm_generation_sha256
        ),
        dispatch_provenance_sha256=request.provenance_sha256,
        pending_owner_marker=projection.pending_owner_marker,
        projection_sha256=projection.projection_sha256,
        account_lock_identity=lock_identity,
        created=created,
    )


def promote_captured_paper_preowner(
    bind: Engine,
    *,
    material: initial.CapturedPaperInitialSessionMaterial,
    preowner_receipt: initial.CommittedCapturedPaperInitialPreowner,
    dispatch_request: CapturedPaperDispatchRequest,
    verification_at: datetime,
    assert_service_fence_held: Callable[[], None] | None = None,
) -> PromotedCapturedPaperPendingOwner:
    """Atomically convert exact PREOWNER authority into PENDING_OWNER.

    The action claim is converted with one compare-and-swap UPDATE.  There is
    never a committed interval in which the old PREOWNER claim is resolved but
    the new ``arm-{uuid}`` claim is absent.  Every failure raises through the
    outer transaction, preserving the original PREOWNER bytes.
    """

    if not isinstance(bind, Engine):
        _reject("pending_owner_engine_invalid")
    if type(material) is not initial.CapturedPaperInitialSessionMaterial:
        _reject("pending_owner_material_invalid")
    _assert_service_fence_held(assert_service_fence_held)
    material.verify()
    session_id, preowner_marker = _verify_preowner_receipt(
        preowner_receipt,
        material=material,
    )
    _verified_promotion_time(material, verification_at)
    _verify_dispatch_material_route(
        dispatch_request,
        material=material,
        session_id=session_id,
    )
    result: PromotedCapturedPaperPendingOwner | None = None

    with Session(bind=bind, expire_on_commit=False) as db:
        with db.begin():
            lock_identity = acquire_adaptive_risk_account_locks(
                db,
                account_scope=initial.ALPACA_PAPER_ACCOUNT_SCOPE,
            )
            row_locks = CanonicalAccountRiskRowLockGuard()
            row_locks.observe(
                AccountRiskRowLockStage.ACTION_CLAIM,
                sort_key=(material.symbol,),
            )
            readable, claim = read_action_claim(
                db,
                symbol=material.symbol,
                account_scope=initial.ALPACA_PAPER_ACCOUNT_SCOPE,
                for_update=True,
            )
            if not readable or claim is None:
                _reject("pending_owner_action_claim_unavailable")
            owner_id = claim.get("owner_session_id")
            if owner_id != session_id:
                _reject("pending_owner_action_claim_owner_mismatch")
            row_locks.observe(
                AccountRiskRowLockStage.AUTOMATION_SESSION,
                sort_key=(session_id,),
            )
            if claim.get("claim_token") == material.material_sha256:
                session = _validate_exact_preowner_locked(
                    db,
                    material=material,
                    preowner_receipt=preowner_receipt,
                    claim=claim,
                    lock_identity=lock_identity,
                )
                commit_at = _verified_promotion_time(
                    material,
                    _locked_database_clock(db),
                )
                projection = build_captured_paper_pending_owner_projection(
                    material=material,
                    preowner_receipt=preowner_receipt,
                    dispatch_request=dispatch_request,
                    arm_token=str(uuid.uuid4()),
                    confirmed_at=commit_at,
                )
                expected_preowner_metadata = _expected_preowner_claim_metadata(
                    material,
                    preowner_marker_sha256=preowner_marker["content_sha256"],
                )
                # Re-prove the process-lifetime singleton after every
                # potentially blocking account/claim/session/authority lock
                # and immediately before the first durable promotion mutation.
                # A lost session advisory lock can never be papered over by the
                # still-valid transaction locks.
                _assert_service_fence_held(assert_service_fence_held)
                converted = db.execute(
                    text(
                        "UPDATE broker_symbol_action_claims SET "
                        " claim_token = :new_claim_token, action = 'entry',"
                        " phase = 'claimed', owner_session_id = :session_id,"
                        " client_order_id = NULL, broker_order_id = NULL,"
                        " metadata_json = CAST(:new_metadata AS jsonb),"
                        " claimed_at = :confirmed_at, updated_at = :confirmed_at,"
                        " lease_expires_at = CASE"
                        "   WHEN lease_expires_at IS NULL"
                        "     OR lease_expires_at < :expires_at"
                        "   THEN :expires_at ELSE lease_expires_at END,"
                        " resolved_at = NULL "
                        "WHERE account_scope = :account_scope"
                        " AND symbol = :symbol AND claim_token = :old_claim_token"
                        " AND action = 'entry' AND phase = 'claimed'"
                        " AND owner_session_id = :session_id"
                        " AND client_order_id IS NULL AND broker_order_id IS NULL"
                        " AND clock_timestamp() >= :decision_at"
                        " AND clock_timestamp() < :expires_at"
                        " AND metadata_json = CAST(:old_metadata AS jsonb) "
                        "RETURNING claim_token, phase, owner_session_id,"
                        " client_order_id, broker_order_id, metadata_json,"
                        " lease_expires_at"
                    ),
                    {
                        "new_claim_token": projection.arm.symbol_claim_token,
                        "session_id": session_id,
                        "new_metadata": json.dumps(
                            _canonical_value(projection.action_claim_metadata),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "confirmed_at": commit_at,
                        "decision_at": material.decision_at,
                        "expires_at": material.expires_at,
                        "account_scope": initial.ALPACA_PAPER_ACCOUNT_SCOPE,
                        "symbol": material.symbol,
                        "old_claim_token": material.material_sha256,
                        "old_metadata": json.dumps(
                            expected_preowner_metadata,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                ).mappings().one_or_none()
                converted_lease = (
                    _aware_utc(
                        converted["lease_expires_at"],
                        "pending_owner_claim_lease",
                    )
                    if converted is not None
                    else None
                )
                if converted is None or not (
                    converted["claim_token"]
                    == projection.arm.symbol_claim_token
                    and converted["phase"] == "claimed"
                    and converted["owner_session_id"] == session_id
                    and converted["client_order_id"] is None
                    and converted["broker_order_id"] is None
                    and dict(converted["metadata_json"] or {})
                    == _canonical_value(projection.action_claim_metadata)
                    and converted_lease is not None
                    and converted_lease >= projection.arm.expires_at
                ):
                    _reject("pending_owner_action_claim_conversion_lost")

                session.state = CAPTURED_PAPER_PENDING_OWNER_STATE
                session.risk_snapshot_json = _canonical_value(
                    projection.risk_snapshot
                )
                session.allocation_decision_json = {}
                session.correlation_id = material.material_sha256
                session.source_node_id = _SOURCE_NODE
                session.updated_at = commit_at.replace(tzinfo=None)
                flag_modified(session, "risk_snapshot_json")
                db.add(
                    TradingAutomationEvent(
                        session_id=session_id,
                        ts=commit_at.replace(tzinfo=None),
                        event_type="captured_paper_pending_owner_committed",
                        payload_json={
                            "schema_version": PENDING_OWNER_SCHEMA_VERSION,
                            "symbol": material.symbol,
                            "account_scope": material.account_scope,
                            "expected_account_id": material.expected_account_id,
                            "runtime_generation": material.runtime_generation,
                            "initial_material_sha256": material.material_sha256,
                            "preowner_marker_sha256": (
                                preowner_marker["content_sha256"]
                            ),
                            "preowner_claim_token": material.material_sha256,
                            "arm_claim_token": (
                                projection.arm.symbol_claim_token
                            ),
                            "confirmed_arm_generation_sha256": (
                                projection.arm.confirmed_arm_generation_sha256
                            ),
                            "dispatch_provenance_sha256": (
                                dispatch_request.provenance_sha256
                            ),
                            "code_build_sha256": material.code_build_sha256,
                            "config_sha256": material.config_sha256,
                            "capture_receipt_sha256": (
                                material.capture_receipt_sha256
                            ),
                            "settings_projection_sha256": (
                                material.settings_projection_sha256
                            ),
                            "feature_flags_sha256": (
                                material.feature_flags_sha256
                            ),
                            "pending_owner_marker_sha256": (
                                projection.pending_owner_marker[
                                    "content_sha256"
                                ]
                            ),
                            "projection_sha256": projection.projection_sha256,
                            "policy_sha256": material.policy_sha256,
                            "adaptive_policy_provenance_sha256": (
                                material.adaptive_policy_provenance_sha256
                            ),
                            "runner_risk_template_sha256": (
                                material.runner_risk_template.template_sha256
                            ),
                            "strategy_variant_sha256": (
                                material.strategy_variant_sha256
                            ),
                            "viability_snapshot_sha256": (
                                material.viability_snapshot_sha256
                            ),
                            "opportunity_consumed": False,
                            "risk_reserved": False,
                            "outbox_created": False,
                            "order_posted": False,
                            "broker_order_post_calls": 0,
                        },
                        correlation_id=material.material_sha256,
                        source_node_id=_SOURCE_NODE,
                    )
                )
                db.flush()
                result = _receipt(
                    projection,
                    material=material,
                    request=dispatch_request,
                    lock_identity=lock_identity,
                    created=True,
                )
            elif str(claim.get("claim_token") or "").startswith("arm-"):
                _session, projection = _validate_existing_pending_locked(
                    db,
                    material=material,
                    preowner_receipt=preowner_receipt,
                    dispatch_request=dispatch_request,
                    claim=claim,
                )
                commit_at = _verified_promotion_time(
                    material,
                    _locked_database_clock(db),
                )
                _assert_service_fence_held(assert_service_fence_held)
                if not (
                    projection.arm.confirmed_at
                    <= commit_at
                    < projection.arm.expires_at
                ):
                    _reject("pending_owner_generation_stale_or_future")
                result = _receipt(
                    projection,
                    material=material,
                    request=dispatch_request,
                    lock_identity=lock_identity,
                    created=False,
                )
            else:
                _reject("pending_owner_symbol_owned_by_other_generation")

    if result is None:
        _reject("pending_owner_promotion_unavailable")
    return result


__all__ = [
    "CAPTURED_PAPER_CONFIRMED_ARM_SHA256_KEY",
    "CAPTURED_PAPER_INITIAL_MATERIAL_KEY",
    "CAPTURED_PAPER_PENDING_OWNER_KEY",
    "CAPTURED_PAPER_PENDING_OWNER_STAGE",
    "CAPTURED_PAPER_PENDING_OWNER_STATE",
    "CapturedPaperPendingOwnerProjection",
    "CapturedPaperPreownerPromotionError",
    "PromotedCapturedPaperPendingOwner",
    "build_captured_paper_pending_owner_projection",
    "promote_captured_paper_preowner",
]
