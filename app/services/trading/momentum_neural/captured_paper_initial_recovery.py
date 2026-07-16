"""Crash recovery for pre-execution captured Alpaca PAPER generations.

Only two transitions are allowed here:

* an exact, still-current PREOWNER is handed to the existing idempotent
  PREOWNER -> PENDING_OWNER promotion after this module releases its read/lock
  transaction; or
* an exact expired PREOWNER/PENDING_OWNER with proof that transport and every
  economic side effect are still impossible is atomically resolved and ended.

The module has no provider, adapter, opportunity, reservation, outbox writer,
or order transport capability.  Stored content-addressed material is the sole
input.  Stale authority is never renewed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ....models.trading import TradingAutomationEvent, TradingAutomationSession
from .adaptive_risk_account_lock import (
    AdaptiveRiskAccountLockIdentity,
    acquire_adaptive_risk_account_locks,
)
from .alpaca_orphan_claims import read_action_claim
from .captured_paper_dispatcher import CapturedPaperDispatchRequest
from . import captured_paper_initial_admission as initial
from . import captured_paper_pending_owner as pending_owner
from . import captured_paper_preowner_promotion as promotion


INITIAL_RECOVERY_SCHEMA_VERSION = "chili.captured-paper-initial-recovery.v1"
INITIAL_RELEASE_SCHEMA_VERSION = (
    "chili.captured-paper-initial-generation-release.v1"
)
INITIAL_RELEASE_METADATA_KEY = "captured_paper_initial_generation_release"
CAPTURED_PAPER_CANCELLED_STATE = "live_cancelled"

_SOURCE_NODE = "captured_paper_initial_recovery"
_NEW_YORK = ZoneInfo("America/New_York")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ZERO_ECONOMIC_FIELDS = (
    "opportunity_consumed",
    "risk_reserved",
    "outbox_created",
    "order_posted",
)


class CapturedPaperInitialRecoveryError(RuntimeError):
    """An initial durable generation could not be recovered exactly."""

    def __init__(self, reason: str):
        self.reason = str(reason or "captured_paper_initial_recovery_rejected")
        super().__init__(self.reason)


def _reject(reason: str) -> None:
    raise CapturedPaperInitialRecoveryError(reason)


def _positive_int(value: Any, reason: str) -> int:
    if type(value) is not int or value <= 0:
        _reject(reason)
    return value


def _sha(value: Any, reason: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _reject(reason)
    return value


def _canonical_uuid(value: Any, reason: str) -> str:
    try:
        return initial._canonical_uuid(value, reason)
    except initial.CapturedPaperInitialAdmissionError as exc:
        raise CapturedPaperInitialRecoveryError(reason) from exc


def _aware_utc(value: Any, reason: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _reject(reason)
    try:
        if value.utcoffset() is None:
            _reject(reason)
    except Exception as exc:  # pragma: no cover - defensive tzinfo boundary
        raise CapturedPaperInitialRecoveryError(reason) from exc
    return value.astimezone(timezone.utc)


def _assert_service_fence(assertion: Callable[[], None] | None) -> None:
    if not callable(assertion):
        _reject("initial_recovery_service_fence_unavailable")
    try:
        result = assertion()
    except Exception as exc:
        raise CapturedPaperInitialRecoveryError(
            "initial_recovery_service_fence_lost"
        ) from exc
    if result is not None:
        _reject("initial_recovery_service_fence_invalid")


def _locked_database_clock(db: Any) -> datetime:
    try:
        value = db.execute(text("SELECT clock_timestamp()")).scalar_one()
    except Exception as exc:
        raise CapturedPaperInitialRecoveryError(
            "initial_recovery_database_clock_unavailable"
        ) from exc
    return _aware_utc(value, "initial_recovery_database_clock_invalid")


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        promotion._canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _stored_material(snapshot: Any) -> initial.CapturedPaperInitialSessionMaterial:
    if type(snapshot) is not dict:
        _reject("initial_recovery_snapshot_invalid")
    marker = snapshot.get(initial._PREOWNER_KEY)
    if type(marker) is dict:
        raw = marker.get(initial.INITIAL_PREOWNER_MATERIAL_KEY)
    else:
        # Promotion intentionally removes the PREOWNER marker, but retains the
        # same exact material at a stable top-level key.  Accepting that shape
        # makes an acknowledgement-lost promotion retry idempotent.
        raw = snapshot.get(promotion.CAPTURED_PAPER_INITIAL_MATERIAL_KEY)
    try:
        material = pending_owner._initial_material(raw)
    except Exception as exc:
        raise CapturedPaperInitialRecoveryError(
            "initial_recovery_material_invalid"
        ) from exc
    if type(material) is not initial.CapturedPaperInitialSessionMaterial:
        _reject("initial_recovery_material_invalid")
    material.verify()
    return material


def _verify_expected_authority(
    material: initial.CapturedPaperInitialSessionMaterial,
    *,
    expected_account_id: str,
    expected_runtime_generation: str,
    expected_code_build_sha256: str,
    expected_config_sha256: str,
    expected_capture_receipt_sha256: str,
) -> None:
    account_id = _canonical_uuid(
        expected_account_id, "initial_recovery_expected_account_id_invalid"
    )
    generation = _canonical_uuid(
        expected_runtime_generation,
        "initial_recovery_expected_runtime_generation_invalid",
    )
    expected = {
        "expected_account_id": account_id,
        "runtime_generation": generation,
        "code_build_sha256": _sha(
            expected_code_build_sha256,
            "initial_recovery_expected_code_build_invalid",
        ),
        "config_sha256": _sha(
            expected_config_sha256,
            "initial_recovery_expected_config_invalid",
        ),
        "capture_receipt_sha256": _sha(
            expected_capture_receipt_sha256,
            "initial_recovery_expected_capture_receipt_invalid",
        ),
    }
    if any(getattr(material, name) != value for name, value in expected.items()):
        _reject("initial_recovery_authority_mismatch")
    if (
        material.account_scope != initial.ALPACA_PAPER_ACCOUNT_SCOPE
        or material.execution_family != initial.ALPACA_SPOT_EXECUTION_FAMILY
    ):
        _reject("initial_recovery_route_mismatch")


def _verify_zero_economic_marker(marker: Any) -> None:
    if type(marker) is not dict:
        _reject("initial_recovery_zero_economic_marker_invalid")
    if (
        any(marker.get(name) is not False for name in _ZERO_ECONOMIC_FIELDS)
        or marker.get("broker_order_post_calls") != 0
    ):
        _reject("initial_recovery_economic_effect_present")


def _verify_no_execution_rows(
    db: Any,
    *,
    session_id: int,
    material: initial.CapturedPaperInitialSessionMaterial,
) -> None:
    """Lock/read all session-addressable execution evidence before release."""

    statements = (
        (
            "SELECT completion_sha256 FROM captured_paper_post_commit_outbox "
            "WHERE session_id = :session_id FOR UPDATE",
            "initial_recovery_outbox_present",
        ),
        (
            "SELECT id FROM trading_automation_simulated_fills "
            "WHERE session_id = :session_id FOR UPDATE",
            "initial_recovery_fill_present",
        ),
        (
            "SELECT id FROM momentum_fill_outcomes "
            "WHERE session_id = :session_id FOR UPDATE",
            "initial_recovery_fill_present",
        ),
    )
    try:
        for sql, reason in statements:
            if db.execute(text(sql), {"session_id": session_id}).first() is not None:
                _reject(reason)
        if db.execute(
            text(
                "SELECT reservation_id FROM adaptive_risk_reservations"
                " WHERE account_scope = :account_scope AND symbol = :symbol"
                " AND state NOT IN ('released', 'closed') FOR UPDATE"
            ),
            {
                "account_scope": material.account_scope,
                "symbol": material.symbol,
            },
        ).first() is not None:
            _reject("initial_recovery_active_reservation_present")
        if db.execute(
            text(
                "SELECT id FROM adaptive_risk_opportunity_claims"
                " WHERE account_scope = :account_scope AND symbol = :symbol"
                " AND trading_date = :trading_date"
                # Consumed is historical economic truth and has no generation
                # identifier in this table.  Only an actively reserved row can
                # still belong to the never-executed initial generation.
                " AND status = 'reserved' FOR UPDATE"
            ),
            {
                "account_scope": material.account_scope,
                "symbol": material.symbol,
                "trading_date": material.decision_at.astimezone(_NEW_YORK).date(),
            },
        ).first() is not None:
            _reject("initial_recovery_opportunity_side_effect_present")
    except CapturedPaperInitialRecoveryError:
        raise
    except Exception as exc:
        raise CapturedPaperInitialRecoveryError(
            "initial_recovery_execution_inventory_unavailable"
        ) from exc


def _release_marker(
    *,
    session_id: int,
    material: initial.CapturedPaperInitialSessionMaterial,
    prior_stage: str,
    prior_claim_token: str,
    released_at: datetime,
) -> dict[str, Any]:
    body = {
        "schema_version": INITIAL_RELEASE_SCHEMA_VERSION,
        "session_id": session_id,
        "symbol": material.symbol,
        "account_scope": material.account_scope,
        "expected_account_id": material.expected_account_id,
        "runtime_generation": material.runtime_generation,
        "initial_material_sha256": material.material_sha256,
        "prior_stage": prior_stage,
        "prior_claim_token": prior_claim_token,
        "authority_expires_at": initial._iso_utc(material.expires_at),
        "released_at": initial._iso_utc(released_at),
        "reason": "initial_generation_expired_before_transport",
        "opportunity_consumed": False,
        "risk_reserved": False,
        "outbox_created": False,
        "order_posted": False,
        "broker_order_post_calls": 0,
    }
    return {**body, "content_sha256": promotion._sha256_json(body)}


@dataclass(frozen=True, slots=True)
class CapturedPaperInitialRecoveryReceipt:
    """Content-addressed result of one recovery/release attempt."""

    session_id: int
    symbol: str
    initial_material_sha256: str
    disposition: str
    prior_claim_token: str
    pending_owner_receipt_sha256: str | None
    release_marker: Mapping[str, Any] | None
    created: bool
    receipt_sha256: str = field(init=False)
    schema_version: str = INITIAL_RECOVERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _positive_int(self.session_id, "initial_recovery_receipt_session_invalid")
        initial._symbol(self.symbol)
        _sha(
            self.initial_material_sha256,
            "initial_recovery_receipt_material_invalid",
        )
        if self.disposition not in {"pending_owner_recovered", "expired_released"}:
            _reject("initial_recovery_receipt_disposition_invalid")
        if not isinstance(self.prior_claim_token, str) or not self.prior_claim_token:
            _reject("initial_recovery_receipt_claim_token_invalid")
        if type(self.created) is not bool:
            _reject("initial_recovery_receipt_created_invalid")
        marker = None
        if self.disposition == "pending_owner_recovered":
            _sha(
                self.pending_owner_receipt_sha256,
                "initial_recovery_pending_receipt_invalid",
            )
            if self.release_marker is not None:
                _reject("initial_recovery_receipt_shape_invalid")
        else:
            if self.pending_owner_receipt_sha256 is not None:
                _reject("initial_recovery_receipt_shape_invalid")
            if type(self.release_marker) is not dict:
                _reject("initial_recovery_release_marker_invalid")
            marker = promotion._canonical_value(self.release_marker)
            if marker.get("content_sha256") != promotion._sha256_json(
                {key: value for key, value in marker.items() if key != "content_sha256"}
            ):
                _reject("initial_recovery_release_marker_invalid")
        object.__setattr__(
            self,
            "release_marker",
            None if marker is None else MappingProxyType(marker),
        )
        object.__setattr__(self, "receipt_sha256", promotion._sha256_json(self.to_body()))

    def to_body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "initial_material_sha256": self.initial_material_sha256,
            "disposition": self.disposition,
            "prior_claim_token": self.prior_claim_token,
            "pending_owner_receipt_sha256": self.pending_owner_receipt_sha256,
            "release_marker": (
                None
                if self.release_marker is None
                else promotion._canonical_value(self.release_marker)
            ),
            "created": self.created,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.to_body(), "receipt_sha256": self.receipt_sha256}


def _dispatch_request(
    material: initial.CapturedPaperInitialSessionMaterial,
    *,
    session_id: int,
) -> CapturedPaperDispatchRequest:
    request = CapturedPaperDispatchRequest(
        session_id=session_id,
        symbol=material.symbol,
        execution_family=material.execution_family,
        account_scope=material.account_scope,
        expected_account_id=material.expected_account_id,
        code_build_sha256=material.code_build_sha256,
        config_sha256=material.config_sha256,
        capture_receipt_sha256=material.capture_receipt_sha256,
        runtime_generation=material.runtime_generation,
        first_dip_policy_mode=promotion._INTENDED_FIRST_DIP_POLICY_MODE,
    )
    request.verify()
    return request


def _preowner_receipt(
    material: initial.CapturedPaperInitialSessionMaterial,
    *,
    session_id: int,
    lock_identity: AdaptiveRiskAccountLockIdentity,
) -> initial.CommittedCapturedPaperInitialPreowner:
    marker = initial._preowner_marker(
        material,
        session_id=session_id,
        claim_token=material.material_sha256,
    )
    return initial.CommittedCapturedPaperInitialPreowner(
        session_id=session_id,
        initial_material_sha256=material.material_sha256,
        preowner_marker=marker,
        claim_token=material.material_sha256,
        account_lock_identity=lock_identity,
        created=False,
    )


def _validate_preowner_locked(
    db: Any,
    *,
    session: TradingAutomationSession,
    claim: Mapping[str, Any],
    material: initial.CapturedPaperInitialSessionMaterial,
    lock_identity: AdaptiveRiskAccountLockIdentity,
) -> initial.CommittedCapturedPaperInitialPreowner:
    session_id = _positive_int(
        getattr(session, "id", None), "initial_recovery_session_invalid"
    )
    receipt = _preowner_receipt(
        material,
        session_id=session_id,
        lock_identity=lock_identity,
    )
    expected_marker = dict(receipt.preowner_marker)
    expected_snapshot = initial._risk_snapshot(material, expected_marker)
    expected_claim_metadata = promotion._expected_preowner_claim_metadata(
        material,
        preowner_marker_sha256=expected_marker["content_sha256"],
    )
    marker = expected_snapshot[initial._PREOWNER_KEY]
    _verify_zero_economic_marker(marker)
    metadata = dict(claim.get("metadata") or {})
    if (
        session.mode != "live"
        or session.venue != "alpaca"
        or session.execution_family != initial.ALPACA_SPOT_EXECUTION_FAMILY
        or session.state != initial.CAPTURED_PAPER_PREOWNER_STATE
        or session.symbol != material.symbol
        or type(session.variant_id) is not int
        or session.variant_id != material.variant_id
        or type(session.user_id) is not int
        or session.user_id != material.user_id
        or session.ended_at is not None
        or session.risk_snapshot_json != expected_snapshot
        or session.allocation_decision_json != {}
        or session.correlation_id != material.material_sha256
        or session.source_node_id != "captured_paper_initial_admission"
        or claim.get("account_scope") != material.account_scope
        or claim.get("symbol") != material.symbol
        or claim.get("claim_token") != material.material_sha256
        or claim.get("action") != "entry"
        or claim.get("phase") != "claimed"
        or claim.get("owner_session_id") != session_id
        or claim.get("client_order_id") is not None
        or claim.get("broker_order_id") is not None
        or claim.get("resolved_at") is not None
        or metadata != expected_claim_metadata
        or "entry_transport_started" in metadata
        or "owner_transport" in metadata
        or "captured_paper_session_owner" in expected_snapshot
        or "momentum_live_execution" in expected_snapshot
    ):
        _reject("initial_recovery_preowner_generation_mismatch")
    _verify_no_execution_rows(
        db, session_id=session_id, material=material
    )
    return receipt


def _validate_pending_claim(
    *,
    claim: Mapping[str, Any],
    pending: pending_owner.ValidatedCapturedPaperPendingOwner,
) -> None:
    arm = pending.projection.arm
    metadata = promotion._canonical_value(
        pending.projection.action_claim_metadata
    )
    claim_metadata = dict(claim.get("metadata") or {})
    lease = _aware_utc(
        claim.get("lease_expires_at"),
        "initial_recovery_pending_claim_lease_invalid",
    )
    if (
        claim.get("account_scope") != pending.material.account_scope
        or claim.get("symbol") != pending.material.symbol
        or claim.get("claim_token") != arm.symbol_claim_token
        or claim.get("action") != "entry"
        or claim.get("phase") != "claimed"
        or claim.get("owner_session_id") != pending.session_id
        or claim.get("client_order_id") is not None
        or claim.get("broker_order_id") is not None
        or claim.get("resolved_at") is not None
        or claim_metadata != metadata
        or "entry_transport_started" in claim_metadata
        or "owner_transport" in claim_metadata
        or lease < arm.expires_at
    ):
        _reject("initial_recovery_pending_claim_mismatch")


def _validate_existing_release_locked(
    db: Any,
    *,
    session: TradingAutomationSession,
    claim: Mapping[str, Any],
    material: initial.CapturedPaperInitialSessionMaterial,
) -> CapturedPaperInitialRecoveryReceipt:
    session_id = _positive_int(
        getattr(session, "id", None), "initial_recovery_session_invalid"
    )
    metadata = dict(claim.get("metadata") or {})
    raw_release = metadata.pop(INITIAL_RELEASE_METADATA_KEY, None)
    if type(raw_release) is not dict:
        _reject("initial_recovery_release_marker_invalid")
    marker = promotion._canonical_value(raw_release)
    unsigned = {key: value for key, value in marker.items() if key != "content_sha256"}
    _verify_zero_economic_marker(marker)
    prior_stage = marker.get("prior_stage")
    if prior_stage == initial.CAPTURED_PAPER_PREOWNER_STATE:
        expected_preowner_marker = initial._preowner_marker(
            material,
            session_id=session_id,
            claim_token=material.material_sha256,
        )
        expected_snapshot = initial._risk_snapshot(
            material, expected_preowner_marker
        )
        base_metadata = promotion._expected_preowner_claim_metadata(
            material,
            preowner_marker_sha256=(
                expected_preowner_marker["content_sha256"]
            ),
        )
        prior_claim_token = material.material_sha256
    elif prior_stage == promotion.CAPTURED_PAPER_PENDING_OWNER_STAGE:
        snapshot = getattr(session, "risk_snapshot_json", None)
        if type(snapshot) is not dict:
            _reject("initial_recovery_existing_release_mismatch")
        raw_pending_marker = snapshot.get(
            promotion.CAPTURED_PAPER_PENDING_OWNER_KEY
        )
        if type(raw_pending_marker) is not dict:
            _reject("initial_recovery_existing_release_mismatch")
        try:
            request = pending_owner._dispatch_request(
                raw_pending_marker.get("dispatch_request")
            )
            arm = promotion._arm_from_legacy_marker(
                snapshot.get("confirmed_arm_generation")
            )
            preowner = _preowner_receipt(
                material,
                session_id=session_id,
                lock_identity=AdaptiveRiskAccountLockIdentity.for_scope(
                    initial.ALPACA_PAPER_ACCOUNT_SCOPE
                ),
            )
            projection = promotion.build_captured_paper_pending_owner_projection(
                material=material,
                preowner_receipt=preowner,
                dispatch_request=request,
                arm_token=arm.arm_token,
                confirmed_at=arm.confirmed_at,
            )
        except Exception as exc:
            raise CapturedPaperInitialRecoveryError(
                "initial_recovery_existing_release_mismatch"
            ) from exc
        expected_snapshot = promotion._canonical_value(
            projection.risk_snapshot
        )
        base_metadata = promotion._canonical_value(
            projection.action_claim_metadata
        )
        prior_claim_token = arm.symbol_claim_token
    else:
        _reject("initial_recovery_existing_release_mismatch")
    try:
        released_at = pending_owner._strict_utc_text(
            marker.get("released_at"), "initial_recovery_release_clock_invalid"
        )
    except Exception as exc:
        raise CapturedPaperInitialRecoveryError(
            "initial_recovery_release_clock_invalid"
        ) from exc
    ended_at = getattr(session, "ended_at", None)
    if isinstance(ended_at, datetime) and ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=timezone.utc)
    if (
        metadata != base_metadata
        or marker.get("content_sha256") != promotion._sha256_json(unsigned)
        or marker.get("schema_version") != INITIAL_RELEASE_SCHEMA_VERSION
        or marker.get("session_id") != session_id
        or marker.get("symbol") != material.symbol
        or marker.get("account_scope") != material.account_scope
        or marker.get("expected_account_id") != material.expected_account_id
        or marker.get("runtime_generation") != material.runtime_generation
        or marker.get("initial_material_sha256") != material.material_sha256
        or marker.get("prior_claim_token") != prior_claim_token
        or marker.get("authority_expires_at")
        != initial._iso_utc(material.expires_at)
        or marker.get("reason")
        != "initial_generation_expired_before_transport"
        or session.mode != "live"
        or session.venue != "alpaca"
        or session.execution_family != material.execution_family
        or session.state != CAPTURED_PAPER_CANCELLED_STATE
        or session.symbol != material.symbol
        or session.variant_id != material.variant_id
        or session.user_id != material.user_id
        or ended_at is None
        or _aware_utc(ended_at, "initial_recovery_release_clock_invalid")
        != released_at
        or session.risk_snapshot_json != expected_snapshot
        or session.allocation_decision_json != {}
        or session.correlation_id != material.material_sha256
        or session.source_node_id != _SOURCE_NODE
        or claim.get("account_scope") != material.account_scope
        or claim.get("symbol") != material.symbol
        or claim.get("claim_token") != prior_claim_token
        or claim.get("action") != "entry"
        or claim.get("phase") != "resolved"
        or claim.get("owner_session_id") != session_id
        or claim.get("client_order_id") is not None
        or claim.get("broker_order_id") is not None
        or claim.get("resolved_at") is None
        or claim.get("lease_expires_at") is not None
        or "entry_transport_started" in metadata
        or "owner_transport" in metadata
    ):
        _reject("initial_recovery_existing_release_mismatch")
    _verify_no_execution_rows(
        db, session_id=session_id, material=material
    )
    try:
        events = db.execute(
            text(
                "SELECT payload_json FROM trading_automation_events"
                " WHERE session_id = :session_id"
                " AND event_type = :event_type ORDER BY id FOR UPDATE"
            ),
            {
                "session_id": session_id,
                "event_type": (
                    "captured_paper_initial_generation_expired_released"
                ),
            },
        ).fetchall()
    except Exception as exc:
        raise CapturedPaperInitialRecoveryError(
            "initial_recovery_release_event_unavailable"
        ) from exc
    if (
        len(events) != 1
        or type(events[0][0]) is not dict
        or events[0][0].get("release_marker_sha256")
        != marker["content_sha256"]
    ):
        _reject("initial_recovery_release_event_mismatch")
    return CapturedPaperInitialRecoveryReceipt(
        session_id=session_id,
        symbol=material.symbol,
        initial_material_sha256=material.material_sha256,
        disposition="expired_released",
        prior_claim_token=prior_claim_token,
        pending_owner_receipt_sha256=None,
        release_marker=marker,
        created=False,
    )


def _release_exact_locked(
    db: Any,
    *,
    session: TradingAutomationSession,
    claim: Mapping[str, Any],
    material: initial.CapturedPaperInitialSessionMaterial,
    prior_stage: str,
    prior_state: str,
    prior_source_node: str,
    prior_claim_token: str,
    expected_claim_metadata: Mapping[str, Any],
    now: datetime,
    assert_service_fence_held: Callable[[], None] | None,
) -> CapturedPaperInitialRecoveryReceipt:
    session_id = _positive_int(
        getattr(session, "id", None), "initial_recovery_session_invalid"
    )
    if now < material.expires_at:
        _reject("initial_recovery_generation_not_expired")
    _verify_no_execution_rows(
        db, session_id=session_id, material=material
    )
    _assert_service_fence(assert_service_fence_held)
    marker = _release_marker(
        session_id=session_id,
        material=material,
        prior_stage=prior_stage,
        prior_claim_token=prior_claim_token,
        released_at=now,
    )
    released_metadata = {
        **promotion._canonical_value(expected_claim_metadata),
        INITIAL_RELEASE_METADATA_KEY: marker,
    }
    try:
        released_claim = db.execute(
            text(
                "UPDATE broker_symbol_action_claims SET"
                " phase = 'resolved', resolved_at = :released_at,"
                " updated_at = :released_at, lease_expires_at = NULL,"
                " metadata_json = CAST(:released_metadata AS jsonb)"
                " WHERE account_scope = :account_scope AND symbol = :symbol"
                " AND claim_token = :claim_token AND action = 'entry'"
                " AND phase = 'claimed' AND owner_session_id = :session_id"
                " AND client_order_id IS NULL AND broker_order_id IS NULL"
                " AND resolved_at IS NULL"
                " AND metadata_json = CAST(:expected_metadata AS jsonb)"
                " AND NOT (metadata_json ? 'entry_transport_started')"
                " AND NOT (metadata_json ? 'owner_transport')"
                " AND clock_timestamp() >= :authority_expires_at"
                " RETURNING phase, owner_session_id, client_order_id,"
                " broker_order_id, metadata_json, resolved_at"
            ),
            {
                "released_at": now,
                "released_metadata": _canonical_json(released_metadata),
                "account_scope": material.account_scope,
                "symbol": material.symbol,
                "claim_token": prior_claim_token,
                "session_id": session_id,
                "expected_metadata": _canonical_json(expected_claim_metadata),
                "authority_expires_at": material.expires_at,
            },
        ).mappings().one_or_none()
    except Exception as exc:
        raise CapturedPaperInitialRecoveryError(
            "initial_recovery_claim_release_unavailable"
        ) from exc
    if (
        released_claim is None
        or released_claim["phase"] != "resolved"
        or released_claim["owner_session_id"] != session_id
        or released_claim["client_order_id"] is not None
        or released_claim["broker_order_id"] is not None
        or dict(released_claim["metadata_json"] or {})
        != promotion._canonical_value(released_metadata)
        or released_claim["resolved_at"] is None
    ):
        _reject("initial_recovery_claim_release_lost")

    # If the process-lifetime singleton was lost after the claim CAS, aborting
    # here rolls that CAS back with the session/event changes below.
    _assert_service_fence(assert_service_fence_held)
    expected_snapshot = promotion._canonical_value(session.risk_snapshot_json)
    try:
        released_session = db.execute(
            text(
                "UPDATE trading_automation_sessions SET"
                " state = :cancelled_state, ended_at = :released_at,"
                " updated_at = :released_at, source_node_id = :source_node"
                " WHERE id = :session_id AND mode = 'live' AND venue = 'alpaca'"
                " AND execution_family = :execution_family"
                " AND symbol = :symbol AND variant_id = :variant_id"
                " AND user_id = :user_id AND state = :prior_state"
                " AND ended_at IS NULL"
                " AND risk_snapshot_json = CAST(:risk_snapshot AS jsonb)"
                " AND allocation_decision_json = '{}'::jsonb"
                " AND correlation_id = :material_sha256"
                " AND source_node_id = :prior_source_node"
                " RETURNING id, state, ended_at"
            ),
            {
                "cancelled_state": CAPTURED_PAPER_CANCELLED_STATE,
                "released_at": now.replace(tzinfo=None),
                "source_node": _SOURCE_NODE,
                "session_id": session_id,
                "execution_family": material.execution_family,
                "symbol": material.symbol,
                "variant_id": material.variant_id,
                "user_id": material.user_id,
                "prior_state": prior_state,
                "risk_snapshot": _canonical_json(expected_snapshot),
                "material_sha256": material.material_sha256,
                "prior_source_node": prior_source_node,
            },
        ).mappings().one_or_none()
    except Exception as exc:
        raise CapturedPaperInitialRecoveryError(
            "initial_recovery_session_release_unavailable"
        ) from exc
    if (
        released_session is None
        or released_session["id"] != session_id
        or released_session["state"] != CAPTURED_PAPER_CANCELLED_STATE
        or released_session["ended_at"] is None
    ):
        _reject("initial_recovery_session_release_lost")

    _assert_service_fence(assert_service_fence_held)
    db.add(
        TradingAutomationEvent(
            session_id=session_id,
            ts=now.replace(tzinfo=None),
            event_type="captured_paper_initial_generation_expired_released",
            payload_json={
                **marker,
                "release_marker_sha256": marker["content_sha256"],
            },
            correlation_id=material.material_sha256,
            source_node_id=_SOURCE_NODE,
        )
    )
    db.flush()
    return CapturedPaperInitialRecoveryReceipt(
        session_id=session_id,
        symbol=material.symbol,
        initial_material_sha256=material.material_sha256,
        disposition="expired_released",
        prior_claim_token=prior_claim_token,
        pending_owner_receipt_sha256=None,
        release_marker=marker,
        created=True,
    )


def recover_captured_paper_initial_preowner(
    bind: Engine,
    *,
    session_id: int,
    expected_account_id: str,
    expected_runtime_generation: str,
    expected_code_build_sha256: str,
    expected_config_sha256: str,
    expected_capture_receipt_sha256: str,
    assert_service_fence_held: Callable[[], None] | None,
) -> CapturedPaperInitialRecoveryReceipt:
    """Recover one exact durable PREOWNER without nesting promotion locks."""

    if not isinstance(bind, Engine):
        _reject("initial_recovery_engine_invalid")
    target_session_id = _positive_int(
        session_id, "initial_recovery_session_id_invalid"
    )
    _assert_service_fence(assert_service_fence_held)
    promotion_inputs: tuple[
        initial.CapturedPaperInitialSessionMaterial,
        initial.CommittedCapturedPaperInitialPreowner,
        CapturedPaperDispatchRequest,
        datetime,
    ] | None = None
    released: CapturedPaperInitialRecoveryReceipt | None = None

    with Session(bind=bind, expire_on_commit=False) as db:
        with db.begin():
            lock_identity = acquire_adaptive_risk_account_locks(
                db, account_scope=initial.ALPACA_PAPER_ACCOUNT_SCOPE
            )
            preliminary = (
                db.query(TradingAutomationSession)
                .populate_existing()
                .filter(TradingAutomationSession.id == target_session_id)
                .one_or_none()
            )
            if preliminary is None:
                _reject("initial_recovery_session_missing")
            preliminary_material = _stored_material(
                preliminary.risk_snapshot_json
            )
            _verify_expected_authority(
                preliminary_material,
                expected_account_id=expected_account_id,
                expected_runtime_generation=expected_runtime_generation,
                expected_code_build_sha256=expected_code_build_sha256,
                expected_config_sha256=expected_config_sha256,
                expected_capture_receipt_sha256=(
                    expected_capture_receipt_sha256
                ),
            )
            readable, claim = read_action_claim(
                db,
                symbol=preliminary_material.symbol,
                account_scope=initial.ALPACA_PAPER_ACCOUNT_SCOPE,
                for_update=True,
            )
            if not readable or claim is None:
                _reject("initial_recovery_action_claim_unavailable")
            locked = (
                db.query(TradingAutomationSession)
                .populate_existing()
                .filter(TradingAutomationSession.id == target_session_id)
                .with_for_update()
                .one_or_none()
            )
            if locked is None:
                _reject("initial_recovery_session_missing")
            material = _stored_material(locked.risk_snapshot_json)
            if material.material_sha256 != preliminary_material.material_sha256:
                _reject("initial_recovery_session_changed_while_locking")
            if (
                locked.state == CAPTURED_PAPER_CANCELLED_STATE
                and claim.get("phase") == "resolved"
            ):
                released = _validate_existing_release_locked(
                    db,
                    session=locked,
                    claim=claim,
                    material=material,
                )
            elif locked.state == promotion.CAPTURED_PAPER_PENDING_OWNER_STATE:
                validated = (
                    pending_owner.validate_captured_paper_pending_owner_inventory(
                        locked,
                        expected_account_id=material.expected_account_id,
                        expected_runtime_generation=material.runtime_generation,
                        expected_execution_family=material.execution_family,
                    )
                )
                _validate_pending_claim(claim=claim, pending=validated)
                _verify_no_execution_rows(
                    db,
                    session_id=target_session_id,
                    material=material,
                )
                now = _locked_database_clock(db)
                _assert_service_fence(assert_service_fence_held)
                if now >= material.expires_at:
                    released = (
                        release_expired_captured_paper_pending_owner_locked(
                            db,
                            session=locked,
                            claim=claim,
                            validated_pending=validated,
                            account_lock_identity=lock_identity,
                            assert_service_fence_held=(
                                assert_service_fence_held
                            ),
                        )
                    )
                else:
                    promoted = promotion._receipt(
                        validated.projection,
                        material=material,
                        request=validated.request,
                        lock_identity=lock_identity,
                        created=False,
                    )
                    released = CapturedPaperInitialRecoveryReceipt(
                        session_id=target_session_id,
                        symbol=material.symbol,
                        initial_material_sha256=material.material_sha256,
                        disposition="pending_owner_recovered",
                        prior_claim_token=material.material_sha256,
                        pending_owner_receipt_sha256=(
                            promoted.receipt_sha256
                        ),
                        release_marker=None,
                        created=False,
                    )
            else:
                receipt = _validate_preowner_locked(
                    db,
                    session=locked,
                    claim=claim,
                    material=material,
                    lock_identity=lock_identity,
                )
                now = _locked_database_clock(db)
                _assert_service_fence(assert_service_fence_held)
                if now >= material.expires_at:
                    expected_claim_metadata = (
                        promotion._expected_preowner_claim_metadata(
                            material,
                            preowner_marker_sha256=(
                                receipt.preowner_marker["content_sha256"]
                            ),
                        )
                    )
                    released = _release_exact_locked(
                        db,
                        session=locked,
                        claim=claim,
                        material=material,
                        prior_stage=initial.CAPTURED_PAPER_PREOWNER_STATE,
                        prior_state=initial.CAPTURED_PAPER_PREOWNER_STATE,
                        prior_source_node="captured_paper_initial_admission",
                        prior_claim_token=material.material_sha256,
                        expected_claim_metadata=expected_claim_metadata,
                        now=now,
                        assert_service_fence_held=assert_service_fence_held,
                    )
                else:
                    promotion_inputs = (
                        material,
                        receipt,
                        _dispatch_request(
                            material, session_id=target_session_id
                        ),
                        now,
                    )

    if released is not None:
        return released
    if promotion_inputs is None:
        _reject("initial_recovery_result_unavailable")
    material, preowner_receipt, request, observed_at = promotion_inputs
    # Deliberately outside the transaction above.  The promotion function owns
    # the canonical account/claim/session locks and is idempotent if another
    # worker won after our read transaction committed.
    try:
        promoted = promotion.promote_captured_paper_preowner(
            bind,
            material=material,
            preowner_receipt=preowner_receipt,
            dispatch_request=request,
            verification_at=observed_at,
            assert_service_fence_held=assert_service_fence_held,
        )
    except Exception as exc:
        raise CapturedPaperInitialRecoveryError(
            "initial_recovery_promotion_unavailable"
        ) from exc
    return CapturedPaperInitialRecoveryReceipt(
        session_id=promoted.session_id,
        symbol=material.symbol,
        initial_material_sha256=material.material_sha256,
        disposition="pending_owner_recovered",
        prior_claim_token=material.material_sha256,
        pending_owner_receipt_sha256=promoted.receipt_sha256,
        release_marker=None,
        created=promoted.created,
    )


def recover_captured_paper_initial_symbol(
    bind: Engine,
    *,
    symbol: str,
    expected_account_id: str,
    expected_runtime_generation: str,
    expected_code_build_sha256: str,
    expected_config_sha256: str,
    expected_capture_receipt_sha256: str,
    assert_service_fence_held: Callable[[], None] | None,
) -> CapturedPaperInitialRecoveryReceipt | None:
    """Resume/release the sole exact initial generation for ``symbol``.

    The inventory transaction closes before delegating to the session-id
    recovery.  This preserves canonical action-claim -> session lock ordering
    and prevents nested account-lock transactions.  A foreign or ambiguous
    active session is never treated as an empty inventory.
    """

    if not isinstance(bind, Engine):
        _reject("initial_recovery_engine_invalid")
    try:
        exact_symbol = initial._symbol(symbol)
    except initial.CapturedPaperInitialAdmissionError as exc:
        raise CapturedPaperInitialRecoveryError(
            "initial_recovery_symbol_invalid"
        ) from exc
    _assert_service_fence(assert_service_fence_held)
    target_session_id: int | None = None
    with Session(bind=bind, expire_on_commit=False) as db:
        with db.begin():
            acquire_adaptive_risk_account_locks(
                db, account_scope=initial.ALPACA_PAPER_ACCOUNT_SCOPE
            )
            rows = (
                db.query(TradingAutomationSession)
                .populate_existing()
                .filter(
                    TradingAutomationSession.mode == "live",
                    TradingAutomationSession.symbol == exact_symbol,
                    TradingAutomationSession.ended_at.is_(None),
                )
                .order_by(TradingAutomationSession.id.asc())
                .with_for_update()
                .all()
            )
            if not rows:
                _assert_service_fence(assert_service_fence_held)
                return None
            if len(rows) != 1:
                _reject("initial_recovery_symbol_inventory_ambiguous")
            row = rows[0]
            if (
                row.venue != "alpaca"
                or row.execution_family != initial.ALPACA_SPOT_EXECUTION_FAMILY
                or row.state
                not in {
                    initial.CAPTURED_PAPER_PREOWNER_STATE,
                    promotion.CAPTURED_PAPER_PENDING_OWNER_STATE,
                }
                or getattr(row, "ended_at", None) is not None
            ):
                _reject("initial_recovery_foreign_active_session")
            material = _stored_material(row.risk_snapshot_json)
            _verify_expected_authority(
                material,
                expected_account_id=expected_account_id,
                expected_runtime_generation=expected_runtime_generation,
                expected_code_build_sha256=expected_code_build_sha256,
                expected_config_sha256=expected_config_sha256,
                expected_capture_receipt_sha256=(
                    expected_capture_receipt_sha256
                ),
            )
            if material.symbol != exact_symbol:
                _reject("initial_recovery_symbol_material_mismatch")
            snapshot = getattr(row, "risk_snapshot_json", None)
            if (
                type(snapshot) is not dict
                or snapshot.get("captured_paper_session_owner") is not None
                or snapshot.get("momentum_live_execution") is not None
            ):
                _reject("initial_recovery_foreign_active_session")
            target_session_id = _positive_int(
                getattr(row, "id", None),
                "initial_recovery_session_id_invalid",
            )
            _assert_service_fence(assert_service_fence_held)

    if target_session_id is None:  # pragma: no cover - structural guard
        _reject("initial_recovery_symbol_inventory_unavailable")
    return recover_captured_paper_initial_preowner(
        bind,
        session_id=target_session_id,
        expected_account_id=expected_account_id,
        expected_runtime_generation=expected_runtime_generation,
        expected_code_build_sha256=expected_code_build_sha256,
        expected_config_sha256=expected_config_sha256,
        expected_capture_receipt_sha256=expected_capture_receipt_sha256,
        assert_service_fence_held=assert_service_fence_held,
    )


def release_expired_captured_paper_pending_owner_locked(
    db: Any,
    *,
    session: TradingAutomationSession,
    claim: Mapping[str, Any],
    validated_pending: pending_owner.ValidatedCapturedPaperPendingOwner,
    account_lock_identity: AdaptiveRiskAccountLockIdentity,
    assert_service_fence_held: Callable[[], None] | None,
) -> CapturedPaperInitialRecoveryReceipt:
    """Release an expired PENDING_OWNER inside the caller's lock transaction.

    The caller must already hold the canonical account advisory locks, the exact
    action-claim row lock, and the session row lock.  This helper never commits.
    """

    if type(validated_pending) is not pending_owner.ValidatedCapturedPaperPendingOwner:
        _reject("initial_recovery_pending_validation_invalid")
    expected_lock = AdaptiveRiskAccountLockIdentity.for_scope(
        initial.ALPACA_PAPER_ACCOUNT_SCOPE
    )
    if account_lock_identity != expected_lock:
        _reject("initial_recovery_pending_account_lock_missing")
    in_transaction = getattr(db, "in_transaction", None)
    if not callable(in_transaction) or not in_transaction():
        _reject("initial_recovery_pending_transaction_missing")
    _assert_service_fence(assert_service_fence_held)
    pending = pending_owner.validate_captured_paper_pending_owner_inventory(
        session,
        expected_account_id=validated_pending.material.expected_account_id,
        expected_runtime_generation=(
            validated_pending.material.runtime_generation
        ),
        expected_execution_family=(
            validated_pending.material.execution_family
        ),
    )
    if (
        pending.session_id != validated_pending.session_id
        or pending.material.material_sha256
        != validated_pending.material.material_sha256
        or pending.request.provenance_sha256
        != validated_pending.request.provenance_sha256
        or pending.projection.projection_sha256
        != validated_pending.projection.projection_sha256
    ):
        _reject("initial_recovery_pending_generation_mismatch")
    marker = promotion._canonical_value(
        pending.projection.pending_owner_marker
    )
    _verify_zero_economic_marker(marker)
    metadata = promotion._canonical_value(
        pending.projection.action_claim_metadata
    )
    arm = pending.projection.arm
    _validate_pending_claim(claim=claim, pending=pending)
    now = _locked_database_clock(db)
    if now < pending.material.expires_at or now < arm.expires_at:
        _reject("initial_recovery_generation_not_expired")
    return _release_exact_locked(
        db,
        session=session,
        claim=claim,
        material=pending.material,
        prior_stage=promotion.CAPTURED_PAPER_PENDING_OWNER_STAGE,
        prior_state=promotion.CAPTURED_PAPER_PENDING_OWNER_STATE,
        prior_source_node="captured_paper_preowner_promotion",
        prior_claim_token=arm.symbol_claim_token,
        expected_claim_metadata=metadata,
        now=now,
        assert_service_fence_held=assert_service_fence_held,
    )


__all__ = [
    "CAPTURED_PAPER_CANCELLED_STATE",
    "INITIAL_RELEASE_METADATA_KEY",
    "CapturedPaperInitialRecoveryError",
    "CapturedPaperInitialRecoveryReceipt",
    "recover_captured_paper_initial_preowner",
    "recover_captured_paper_initial_symbol",
    "release_expired_captured_paper_pending_owner_locked",
]
