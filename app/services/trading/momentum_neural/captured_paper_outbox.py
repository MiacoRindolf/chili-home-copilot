"""Durable two-phase outbox for captured Alpaca PAPER entry completion.

The outbox has one narrow responsibility: preserve the exact content-addressed
``CapturedPaperPostCommitRequest`` across the phase-one transaction boundary and
lease it to a later completion owner.  It never reserves risk, creates a symbol
action/opportunity claim, reads a provider, constructs a broker adapter, or
submits an order.

Every public mutation uses the database clock and an exact lease-token/version
CAS.  A transport-start marker is durable before any external POST.  Once that
marker exists, an expired or failed attempt becomes reconciliation-only and is
never fed back through the generic completion retry path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
import hashlib
import hmac
import json
import re
import secrets
import threading
import uuid
from typing import TYPE_CHECKING, Any, Mapping

from sqlalchemy import text

from .adaptive_risk_account_lock import (
    AccountRiskRowLockStage,
    CanonicalAccountRiskRowLockGuard,
    acquire_adaptive_risk_account_locks,
)
from .captured_paper_entry_intent import (
    ALPACA_PAPER_ACCOUNT_SCOPE,
    CapturedPaperIntentContractError,
    CapturedPaperPostCommitRequest,
)
from .captured_paper_financial_breaker import (
    CapturedPaperFinancialBreakerError,
    CapturedPaperFinancialBreakerReceipt,
)

if TYPE_CHECKING:
    from .alpaca_fill_activity import AlpacaPaperEntryFillHandoffProof


UTC = timezone.utc
OUTBOX_EVENT_SCHEMA_VERSION = "chili.captured-paper-outbox-event.v1"
OUTBOX_PAYLOAD_SCHEMA_VERSION = "chili.captured-paper-outbox-payload.v1"
TRANSPORT_AUTHORITY_SCHEMA_VERSION = (
    "chili.captured-paper-transport-authority.v1"
)
TRANSPORT_INVOCATION_AUTHORITY_SCHEMA_VERSION = (
    "chili.captured-paper-transport-invocation-authority.v1"
)
TRANSPORT_PRE_DISPATCH_EVIDENCE_SCHEMA_VERSION = (
    "chili.captured-paper-transport-pre-dispatch-evidence.v2"
)
TRANSPORT_DISPATCH_AUTHORITY_SCHEMA_VERSION = (
    "chili.captured-paper-transport-dispatch-authority.v2"
)

# This process-private key does not replace the durable DB event.  It prevents
# an accidentally miswired/duck-typed store from manufacturing a structurally
# valid dispatch object without having committed the one-shot consume event in
# this process.  After restart there is deliberately no way to mint/reuse an
# old dispatch authority; the durable start marker remains reconciliation-only.
_TRANSPORT_DISPATCH_ATTESTATION_KEY = secrets.token_bytes(32)
_TRANSPORT_DISPATCH_ATTESTATION_LOCK = threading.Lock()
_TRANSPORT_DISPATCH_ATTESTATIONS: dict[str, str] = {}
BROKER_ACCEPTANCE_PROOF_SCHEMA_VERSION = (
    "chili.captured-paper-broker-acceptance.v1"
)
DURABLE_COMMITTED_ADMISSION_SCHEMA_VERSION = (
    "chili.captured-paper-committed-admission.v1"
)
DURABLE_TRANSPORT_INSTRUCTION_SCHEMA_VERSION = (
    "chili.captured-paper-transport-instruction.v1"
)
CAPTURED_PAPER_FILL_HANDOFF_RECEIPT_SCHEMA_VERSION = (
    "chili.captured-paper-fill-handoff-receipt.v1"
)
CAPTURED_PAPER_COMPLETED_FILL_WATCH_SCHEMA_VERSION = (
    "chili.captured-paper-completed-fill-watch.v1"
)

OUTBOX_STATUS_PENDING = "pending"
OUTBOX_STATUS_LEASED = "leased"
OUTBOX_STATUS_RETRY_WAIT = "retry_wait"
OUTBOX_STATUS_RETRY_EXHAUSTED = "retry_exhausted"
OUTBOX_STATUS_TRANSPORT_STARTED = "transport_started"
OUTBOX_STATUS_TRANSPORT_INDETERMINATE = "transport_indeterminate"
OUTBOX_STATUS_RECONCILING = "reconciling"
OUTBOX_STATUS_FILL_HANDOFF_COMMITTED = "fill_handoff_committed"
OUTBOX_STATUS_COMPLETED = "completed"

FILL_WATCH_STATE_PENDING = "pending"
FILL_WATCH_STATE_LEASED = "leased"
FILL_WATCH_STATE_RETRY_WAIT = "retry_wait"
FILL_WATCH_STATE_TERMINAL_ZERO_FILL = "terminal_zero_fill"
FILL_WATCH_STATE_HANDOFF_COMMITTED = "fill_handoff_committed"

_OUTBOX_STATUSES = frozenset(
    {
        OUTBOX_STATUS_PENDING,
        OUTBOX_STATUS_LEASED,
        OUTBOX_STATUS_RETRY_WAIT,
        OUTBOX_STATUS_RETRY_EXHAUSTED,
        OUTBOX_STATUS_TRANSPORT_STARTED,
        OUTBOX_STATUS_TRANSPORT_INDETERMINATE,
        OUTBOX_STATUS_RECONCILING,
        OUTBOX_STATUS_FILL_HANDOFF_COMMITTED,
        OUTBOX_STATUS_COMPLETED,
    }
)
_LEASED_STATUSES = frozenset(
    {
        OUTBOX_STATUS_LEASED,
        OUTBOX_STATUS_TRANSPORT_STARTED,
        OUTBOX_STATUS_RECONCILING,
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BROKER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_MAX_ATTEMPT_BOUND = 32_767
_MAX_LEASE_SECONDS = 86_400
_MAX_RETRY_DELAY_SECONDS = 604_800
_TRANSPORT_ORDER_FIELDS = frozenset(
    {
        "asset_class",
        "client_order_id",
        "extended_hours",
        "limit_price",
        "position_intent",
        "qty",
        "side",
        "symbol",
        "time_in_force",
        "type",
    }
)


class CapturedPaperOutboxError(RuntimeError):
    """Base fail-closed outbox error with a stable reason code."""

    def __init__(self, reason: str):
        self.reason = str(reason or "captured_paper_outbox_error")
        super().__init__(self.reason)


class CapturedPaperOutboxNotFoundError(CapturedPaperOutboxError):
    pass


class CapturedPaperOutboxConflictError(CapturedPaperOutboxError):
    pass


class CapturedPaperOutboxCorruptionError(CapturedPaperOutboxError):
    pass


class CapturedPaperOutboxLeaseError(CapturedPaperOutboxError):
    pass


def _atomic_mutation(function):
    """Rollback only this outbox mutation when a caller catches its error."""

    @wraps(function)
    def wrapped(db: Any, *args: Any, **kwargs: Any):
        begin_nested = getattr(db, "begin_nested", None)
        if not callable(begin_nested):
            raise CapturedPaperOutboxError(
                "outbox_nested_transaction_unavailable"
            )
        with begin_nested():
            return function(db, *args, **kwargs)

    return wrapped


@dataclass(frozen=True, slots=True)
class CapturedPaperOutboxEvent:
    sequence: int
    event_type: str
    previous_event_sha256: str | None
    event_sha256: str
    event_payload_sha256: str
    event_payload: dict[str, Any]
    effective_at: datetime


@dataclass(frozen=True, slots=True)
class CapturedPaperOutboxRecord:
    request: CapturedPaperPostCommitRequest
    durable_transport: CapturedPaperDurableTransportBundle
    payload_sha256: str
    status: str
    binder_id: str
    attempt_count: int
    max_attempts: int
    reconciliation_attempt_count: int
    max_reconciliation_attempts: int
    reconciliation_next_attempt_at: datetime | None
    reconciliation_total_attempt_count: int
    reconciliation_health_state: str
    reconciliation_escalation_count: int
    last_reconciliation_health_escalated_at: datetime | None
    lease_token: str | None
    lease_owner_id: str | None
    lease_expires_at: datetime | None
    next_attempt_at: datetime | None
    transport_started_at: datetime | None
    transport_evidence_sha256: str | None
    transport_indeterminate_at: datetime | None
    indeterminate_evidence_sha256: str | None
    last_failure_sha256: str | None
    last_reconciliation_evidence_sha256: str | None
    completion_proof_sha256: str | None
    completed_at: datetime | None
    fill_handoff_proof: dict[str, Any] | None
    fill_handoff_proof_sha256: str | None
    fill_handoff_receipt: dict[str, Any] | None
    fill_handoff_receipt_sha256: str | None
    fill_handoff_committed_at: datetime | None
    event_sequence: int
    last_event_sha256: str | None
    version: int
    events: tuple[CapturedPaperOutboxEvent, ...]

    @property
    def completion_sha256(self) -> str:
        return self.request.completion_sha256


@dataclass(frozen=True, slots=True)
class CapturedPaperOutboxLease:
    record: CapturedPaperOutboxRecord
    lease_token: str
    lease_owner_id: str
    lease_expires_at: datetime
    recovered: bool
    reconciliation_only: bool


@dataclass(frozen=True, slots=True)
class CapturedPaperCompletedFillWatchLease:
    completion_sha256: str
    lease_token: str
    lease_owner_id: str
    lease_expires_at: datetime
    attempt_count: int
    recovered: bool


@dataclass(frozen=True, slots=True)
class CapturedPaperCompletedFillWatchBundle:
    durable_transport: CapturedPaperDurableTransportBundle
    lease: CapturedPaperCompletedFillWatchLease
    completion_proof_sha256: str
    completion_event_type: str
    broker_order_id: str
    broker_connection_generation: str
    broker_order_evidence_sha256: str
    broker_observed_at: datetime
    broker_available_at: datetime

    @property
    def completion_sha256(self) -> str:
        return self.durable_transport.completion_sha256


@dataclass(frozen=True, slots=True)
class CapturedPaperTransportAuthority:
    """Typed proof that phase two durably reserved and fenced this exact CID."""

    completion_sha256: str
    account_scope: str
    expected_account_id: str
    account_identity_sha256: str
    session_id: int
    symbol: str
    client_order_id: str
    binder_id: str
    action_claim_token: str
    reservation_id: str
    decision_packet_sha256: str
    reservation_request_sha256: str
    admission_evidence_sha256: str
    broker_request_sha256: str
    opportunity_key_sha256: str | None
    authority_sha256: str = ""

    def __post_init__(self) -> None:
        if self.account_scope != ALPACA_PAPER_ACCOUNT_SCOPE:
            raise CapturedPaperOutboxError(
                "transport_authority_account_scope_invalid"
            )
        object.__setattr__(
            self,
            "expected_account_id",
            _canonical_uuid(
                self.expected_account_id,
                field_name="transport_authority_account_id",
            ),
        )
        object.__setattr__(
            self,
            "account_identity_sha256",
            _digest(
                self.account_identity_sha256,
                field_name="transport_authority_account_identity_sha256",
            ),
        )
        if isinstance(self.session_id, bool) or int(self.session_id) <= 0:
            raise CapturedPaperOutboxError(
                "transport_authority_session_id_invalid"
            )
        object.__setattr__(self, "session_id", int(self.session_id))
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise CapturedPaperOutboxError(
                "transport_authority_symbol_invalid"
            )
        if _BROKER_ID_RE.fullmatch(str(self.client_order_id or "")) is None:
            raise CapturedPaperOutboxError(
                "transport_authority_client_order_id_invalid"
            )
        object.__setattr__(
            self,
            "binder_id",
            _canonical_uuid(self.binder_id, field_name="transport_authority_binder_id"),
        )
        if not str(self.action_claim_token).startswith("arm-"):
            raise CapturedPaperOutboxError(
                "transport_authority_action_claim_token_invalid"
            )
        _canonical_uuid(
            self.action_claim_token[4:],
            field_name="transport_authority_action_claim_generation",
        )
        object.__setattr__(
            self,
            "reservation_id",
            _canonical_uuid(
                self.reservation_id,
                field_name="transport_authority_reservation_id",
            ),
        )
        for field_name in (
            "completion_sha256",
            "decision_packet_sha256",
            "reservation_request_sha256",
            "admission_evidence_sha256",
            "broker_request_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _digest(getattr(self, field_name), field_name=field_name),
            )
        if self.opportunity_key_sha256 is not None:
            object.__setattr__(
                self,
                "opportunity_key_sha256",
                _digest(
                    self.opportunity_key_sha256,
                    field_name="opportunity_key_sha256",
                ),
            )
        expected_hash = _sha256_text(_canonical_json(self._content_payload()))
        if self.authority_sha256:
            supplied = _digest(
                self.authority_sha256, field_name="transport_authority_sha256"
            )
            if supplied != expected_hash:
                raise CapturedPaperOutboxError(
                    "transport_authority_hash_mismatch"
                )
        object.__setattr__(self, "authority_sha256", expected_hash)

    def _content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": TRANSPORT_AUTHORITY_SCHEMA_VERSION,
            "completion_sha256": self.completion_sha256,
            "account_scope": self.account_scope,
            "expected_account_id": self.expected_account_id,
            "account_identity_sha256": self.account_identity_sha256,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "client_order_id": self.client_order_id,
            "binder_id": self.binder_id,
            "action_claim_token": self.action_claim_token,
            "reservation_id": self.reservation_id,
            "decision_packet_sha256": self.decision_packet_sha256,
            "reservation_request_sha256": self.reservation_request_sha256,
            "admission_evidence_sha256": self.admission_evidence_sha256,
            "broker_request_sha256": self.broker_request_sha256,
            "opportunity_key_sha256": self.opportunity_key_sha256,
        }

    def to_payload(self) -> dict[str, Any]:
        return {**self._content_payload(), "authority_sha256": self.authority_sha256}

    def verify_for_request(self, request: CapturedPaperPostCommitRequest) -> None:
        request.verify()
        intent = request.intent
        route = intent.route_token
        opportunity_sha256 = (
            intent.opportunity_key.opportunity_key_sha256
            if intent.opportunity_key is not None
            else None
        )
        expected = {
            "completion_sha256": request.completion_sha256,
            "account_scope": route.account_scope,
            "expected_account_id": route.expected_account_id,
            "session_id": route.session_id,
            "symbol": route.symbol,
            "client_order_id": intent.client_order_id,
            "binder_id": intent.binder_id,
            "action_claim_token": intent.symbol_claim_token,
            "opportunity_key_sha256": opportunity_sha256,
        }
        if any(getattr(self, key) != value for key, value in expected.items()):
            raise CapturedPaperOutboxError(
                "transport_authority_request_binding_mismatch"
            )
        canonical = CapturedPaperTransportAuthority(
            **self._content_payload_without_schema()
        )
        if canonical.authority_sha256 != self.authority_sha256:
            raise CapturedPaperOutboxError("transport_authority_hash_mismatch")

    def _content_payload_without_schema(self) -> dict[str, Any]:
        payload = self._content_payload()
        payload.pop("schema_version")
        return payload


@dataclass(frozen=True, slots=True)
class CapturedPaperTransportInvocationAuthority:
    """One-shot durable authority checked after the start fence, before I/O."""

    completion_sha256: str
    transport_authority_sha256: str
    transport_instruction_sha256: str
    lease_token: str
    lease_owner_id: str
    transport_started_at: datetime
    verified_at: datetime
    valid_until: datetime
    outbox_version: int
    authorization_event_sequence: int
    previous_event_sha256: str | None
    invocation_authority_sha256: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "completion_sha256",
            "transport_authority_sha256",
            "transport_instruction_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _digest(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self,
            "lease_token",
            _canonical_uuid(
                self.lease_token,
                field_name="transport_invocation_lease_token",
            ),
        )
        object.__setattr__(
            self,
            "lease_owner_id",
            _canonical_uuid(
                self.lease_owner_id,
                field_name="transport_invocation_lease_owner_id",
            ),
        )
        for field_name in (
            "transport_started_at",
            "verified_at",
            "valid_until",
        ):
            object.__setattr__(
                self,
                field_name,
                _aware_utc(getattr(self, field_name), field_name=field_name),
            )
        if not (
            self.transport_started_at <= self.verified_at < self.valid_until
        ):
            raise CapturedPaperOutboxError(
                "transport_invocation_authority_clock_order_invalid"
            )
        if (
            isinstance(self.outbox_version, bool)
            or int(self.outbox_version) < 1
        ):
            raise CapturedPaperOutboxError(
                "transport_invocation_outbox_version_invalid"
            )
        object.__setattr__(self, "outbox_version", int(self.outbox_version))
        if (
            isinstance(self.authorization_event_sequence, bool)
            or int(self.authorization_event_sequence) < 1
        ):
            raise CapturedPaperOutboxError(
                "transport_invocation_event_sequence_invalid"
            )
        object.__setattr__(
            self,
            "authorization_event_sequence",
            int(self.authorization_event_sequence),
        )
        if self.previous_event_sha256 is not None:
            object.__setattr__(
                self,
                "previous_event_sha256",
                _digest(
                    self.previous_event_sha256,
                    field_name="transport_invocation_previous_event_sha256",
                ),
            )
        expected_hash = _sha256_text(_canonical_json(self._content_payload()))
        if self.invocation_authority_sha256:
            supplied = _digest(
                self.invocation_authority_sha256,
                field_name="transport_invocation_authority_sha256",
            )
            if supplied != expected_hash:
                raise CapturedPaperOutboxError(
                    "transport_invocation_authority_hash_mismatch"
                )
        object.__setattr__(
            self, "invocation_authority_sha256", expected_hash
        )

    def _content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": TRANSPORT_INVOCATION_AUTHORITY_SCHEMA_VERSION,
            "completion_sha256": self.completion_sha256,
            "transport_authority_sha256": self.transport_authority_sha256,
            "transport_instruction_sha256": self.transport_instruction_sha256,
            "lease_token": self.lease_token,
            "lease_owner_id": self.lease_owner_id,
            "transport_started_at": self.transport_started_at.isoformat(),
            "verified_at": self.verified_at.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "outbox_version": self.outbox_version,
            "authorization_event_sequence": self.authorization_event_sequence,
            "previous_event_sha256": self.previous_event_sha256,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            **self._content_payload(),
            "invocation_authority_sha256": self.invocation_authority_sha256,
        }

    def verify_for(
        self,
        authority: CapturedPaperTransportAuthority,
        *,
        transport_instruction_sha256: str,
        lease_token: str,
        lease_owner_id: str,
    ) -> None:
        if type(authority) is not CapturedPaperTransportAuthority:
            raise CapturedPaperOutboxError(
                "transport_invocation_transport_authority_invalid"
            )
        expected = {
            "completion_sha256": authority.completion_sha256,
            "transport_authority_sha256": authority.authority_sha256,
            "transport_instruction_sha256": _digest(
                transport_instruction_sha256,
                field_name="transport_invocation_instruction_sha256",
            ),
            "lease_token": _canonical_uuid(
                lease_token,
                field_name="transport_invocation_expected_lease_token",
            ),
            "lease_owner_id": _canonical_uuid(
                lease_owner_id,
                field_name="transport_invocation_expected_lease_owner_id",
            ),
        }
        if any(getattr(self, key) != value for key, value in expected.items()):
            raise CapturedPaperOutboxError(
                "transport_invocation_authority_binding_mismatch"
            )
        canonical = CapturedPaperTransportInvocationAuthority(
            completion_sha256=self.completion_sha256,
            transport_authority_sha256=self.transport_authority_sha256,
            transport_instruction_sha256=self.transport_instruction_sha256,
            lease_token=self.lease_token,
            lease_owner_id=self.lease_owner_id,
            transport_started_at=self.transport_started_at,
            verified_at=self.verified_at,
            valid_until=self.valid_until,
            outbox_version=self.outbox_version,
            authorization_event_sequence=self.authorization_event_sequence,
            previous_event_sha256=self.previous_event_sha256,
        )
        if canonical.invocation_authority_sha256 != (
            self.invocation_authority_sha256
        ):
            raise CapturedPaperOutboxError(
                "transport_invocation_authority_hash_mismatch"
            )


@dataclass(frozen=True, slots=True)
class CapturedPaperTransportPreDispatchEvidence:
    """Fresh broker/account evidence prepared before the final DB fence.

    The exact Alpaca transport mints this only after its authenticated PAPER
    account/generation read and before order I/O.  It is data only: it grants no
    POST authority until the store performs one final canonical lock walk and
    commits :class:`CapturedPaperTransportDispatchAuthority`.
    """

    completion_sha256: str
    transport_authority_sha256: str
    transport_instruction_sha256: str
    invocation_authority_sha256: str
    connection_receipt_sha256: str
    account_scope: str
    expected_account_id: str
    broker_connection_generation: str
    adapter_build_sha256: str
    connection_available_at: datetime
    prepared_at: datetime
    valid_until: datetime
    evidence_sha256: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "completion_sha256",
            "transport_authority_sha256",
            "transport_instruction_sha256",
            "invocation_authority_sha256",
            "connection_receipt_sha256",
            "adapter_build_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _digest(getattr(self, field_name), field_name=field_name),
            )
        if self.account_scope != ALPACA_PAPER_ACCOUNT_SCOPE:
            raise CapturedPaperOutboxError(
                "transport_pre_dispatch_account_scope_invalid"
            )
        object.__setattr__(
            self,
            "expected_account_id",
            _canonical_uuid(
                self.expected_account_id,
                field_name="transport_pre_dispatch_account_id",
            ),
        )
        generation = str(self.broker_connection_generation or "").strip()
        if generation != self.broker_connection_generation or (
            _BROKER_ID_RE.fullmatch(generation) is None
        ):
            raise CapturedPaperOutboxError(
                "transport_pre_dispatch_connection_generation_invalid"
            )
        for field_name in (
            "connection_available_at",
            "prepared_at",
            "valid_until",
        ):
            object.__setattr__(
                self,
                field_name,
                _aware_utc(getattr(self, field_name), field_name=field_name),
            )
        if not (
            self.connection_available_at <= self.prepared_at < self.valid_until
        ):
            raise CapturedPaperOutboxError(
                "transport_pre_dispatch_clock_order_invalid"
            )
        expected_hash = _sha256_text(_canonical_json(self._content_payload()))
        if self.evidence_sha256:
            supplied = _digest(
                self.evidence_sha256,
                field_name="transport_pre_dispatch_evidence_sha256",
            )
            if supplied != expected_hash:
                raise CapturedPaperOutboxError(
                    "transport_pre_dispatch_evidence_hash_mismatch"
                )
        object.__setattr__(self, "evidence_sha256", expected_hash)

    def _content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": TRANSPORT_PRE_DISPATCH_EVIDENCE_SCHEMA_VERSION,
            "completion_sha256": self.completion_sha256,
            "transport_authority_sha256": self.transport_authority_sha256,
            "transport_instruction_sha256": self.transport_instruction_sha256,
            "invocation_authority_sha256": self.invocation_authority_sha256,
            "connection_receipt_sha256": self.connection_receipt_sha256,
            "account_scope": self.account_scope,
            "expected_account_id": self.expected_account_id,
            "broker_connection_generation": self.broker_connection_generation,
            "adapter_build_sha256": self.adapter_build_sha256,
            "connection_available_at": self.connection_available_at.isoformat(),
            "prepared_at": self.prepared_at.isoformat(),
            "valid_until": self.valid_until.isoformat(),
        }

    def to_payload(self) -> dict[str, Any]:
        return {**self._content_payload(), "evidence_sha256": self.evidence_sha256}

    def verify_for(
        self,
        authority: CapturedPaperTransportAuthority,
        invocation_authority: CapturedPaperTransportInvocationAuthority,
        *,
        transport_instruction_sha256: str,
    ) -> None:
        if not (
            type(authority) is CapturedPaperTransportAuthority
            and type(invocation_authority)
            is CapturedPaperTransportInvocationAuthority
        ):
            raise CapturedPaperOutboxError(
                "transport_pre_dispatch_authority_type_invalid"
            )
        expected = {
            "completion_sha256": authority.completion_sha256,
            "transport_authority_sha256": authority.authority_sha256,
            "transport_instruction_sha256": _digest(
                transport_instruction_sha256,
                field_name="transport_pre_dispatch_instruction_sha256",
            ),
            "invocation_authority_sha256": (
                invocation_authority.invocation_authority_sha256
            ),
            "account_scope": authority.account_scope,
            "expected_account_id": authority.expected_account_id,
        }
        if any(getattr(self, name) != value for name, value in expected.items()):
            raise CapturedPaperOutboxError(
                "transport_pre_dispatch_authority_binding_mismatch"
            )
        if not (
            invocation_authority.verified_at <= self.prepared_at
            and self.valid_until <= invocation_authority.valid_until
        ):
            raise CapturedPaperOutboxError(
                "transport_pre_dispatch_authority_clock_mismatch"
            )
        canonical = CapturedPaperTransportPreDispatchEvidence(
            completion_sha256=self.completion_sha256,
            transport_authority_sha256=self.transport_authority_sha256,
            transport_instruction_sha256=self.transport_instruction_sha256,
            invocation_authority_sha256=self.invocation_authority_sha256,
            connection_receipt_sha256=self.connection_receipt_sha256,
            account_scope=self.account_scope,
            expected_account_id=self.expected_account_id,
            broker_connection_generation=self.broker_connection_generation,
            adapter_build_sha256=self.adapter_build_sha256,
            connection_available_at=self.connection_available_at,
            prepared_at=self.prepared_at,
            valid_until=self.valid_until,
        )
        if canonical.evidence_sha256 != self.evidence_sha256:
            raise CapturedPaperOutboxError(
                "transport_pre_dispatch_evidence_hash_mismatch"
            )


@dataclass(frozen=True, slots=True)
class CapturedPaperTransportDispatchAuthority:
    """Irreversible final DB fence consumed immediately before one POST."""

    completion_sha256: str
    transport_authority_sha256: str
    transport_instruction_sha256: str
    invocation_authority_sha256: str
    financial_breaker_receipt_sha256: str
    pre_dispatch_evidence_sha256: str
    connection_receipt_sha256: str
    lease_token: str
    lease_owner_id: str
    verified_at: datetime
    valid_until: datetime
    outbox_version: int
    dispatch_event_sequence: int
    previous_event_sha256: str
    dispatch_authority_sha256: str = ""
    process_attestation_hmac_sha256: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "completion_sha256",
            "transport_authority_sha256",
            "transport_instruction_sha256",
            "invocation_authority_sha256",
            "financial_breaker_receipt_sha256",
            "pre_dispatch_evidence_sha256",
            "connection_receipt_sha256",
            "previous_event_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _digest(getattr(self, field_name), field_name=field_name),
            )
        for field_name in ("lease_token", "lease_owner_id"):
            object.__setattr__(
                self,
                field_name,
                _canonical_uuid(getattr(self, field_name), field_name=field_name),
            )
        for field_name in ("verified_at", "valid_until"):
            object.__setattr__(
                self,
                field_name,
                _aware_utc(getattr(self, field_name), field_name=field_name),
            )
        if self.verified_at >= self.valid_until:
            raise CapturedPaperOutboxError(
                "transport_dispatch_authority_clock_order_invalid"
            )
        for field_name in ("outbox_version", "dispatch_event_sequence"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or int(value) < 1:
                raise CapturedPaperOutboxError(
                    f"transport_dispatch_{field_name}_invalid"
                )
            object.__setattr__(self, field_name, int(value))
        expected_hash = _sha256_text(_canonical_json(self._content_payload()))
        if self.dispatch_authority_sha256:
            supplied = _digest(
                self.dispatch_authority_sha256,
                field_name="transport_dispatch_authority_sha256",
            )
            if supplied != expected_hash:
                raise CapturedPaperOutboxError(
                    "transport_dispatch_authority_hash_mismatch"
                )
        object.__setattr__(self, "dispatch_authority_sha256", expected_hash)
        if self.process_attestation_hmac_sha256:
            object.__setattr__(
                self,
                "process_attestation_hmac_sha256",
                _digest(
                    self.process_attestation_hmac_sha256,
                    field_name="transport_dispatch_process_attestation_hmac_sha256",
                ),
            )

    def _content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": TRANSPORT_DISPATCH_AUTHORITY_SCHEMA_VERSION,
            "completion_sha256": self.completion_sha256,
            "transport_authority_sha256": self.transport_authority_sha256,
            "transport_instruction_sha256": self.transport_instruction_sha256,
            "invocation_authority_sha256": self.invocation_authority_sha256,
            "financial_breaker_receipt_sha256": (
                self.financial_breaker_receipt_sha256
            ),
            "pre_dispatch_evidence_sha256": self.pre_dispatch_evidence_sha256,
            "connection_receipt_sha256": self.connection_receipt_sha256,
            "lease_token": self.lease_token,
            "lease_owner_id": self.lease_owner_id,
            "verified_at": self.verified_at.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "outbox_version": self.outbox_version,
            "dispatch_event_sequence": self.dispatch_event_sequence,
            "previous_event_sha256": self.previous_event_sha256,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            **self._content_payload(),
            "dispatch_authority_sha256": self.dispatch_authority_sha256,
            "process_attestation_hmac_sha256": (
                self.process_attestation_hmac_sha256
            ),
        }

    def verify_for(
        self,
        authority: CapturedPaperTransportAuthority,
        invocation_authority: CapturedPaperTransportInvocationAuthority,
        financial_breaker_receipt: CapturedPaperFinancialBreakerReceipt,
        pre_dispatch_evidence: CapturedPaperTransportPreDispatchEvidence,
        *,
        transport_instruction_sha256: str,
    ) -> None:
        pre_dispatch_evidence.verify_for(
            authority,
            invocation_authority,
            transport_instruction_sha256=transport_instruction_sha256,
        )
        expected = {
            "completion_sha256": authority.completion_sha256,
            "transport_authority_sha256": authority.authority_sha256,
            "transport_instruction_sha256": _digest(
                transport_instruction_sha256,
                field_name="transport_dispatch_instruction_sha256",
            ),
            "invocation_authority_sha256": (
                invocation_authority.invocation_authority_sha256
            ),
            "financial_breaker_receipt_sha256": (
                financial_breaker_receipt.receipt_sha256
            ),
            "pre_dispatch_evidence_sha256": pre_dispatch_evidence.evidence_sha256,
            "connection_receipt_sha256": (
                pre_dispatch_evidence.connection_receipt_sha256
            ),
            "lease_token": invocation_authority.lease_token,
            "lease_owner_id": invocation_authority.lease_owner_id,
        }
        if any(getattr(self, name) != value for name, value in expected.items()):
            raise CapturedPaperOutboxError(
                "transport_dispatch_authority_binding_mismatch"
            )
        if not (
            pre_dispatch_evidence.prepared_at <= self.verified_at
            and self.valid_until <= pre_dispatch_evidence.valid_until
            and self.valid_until <= invocation_authority.valid_until
            and self.valid_until <= financial_breaker_receipt.valid_until
        ):
            raise CapturedPaperOutboxError(
                "transport_dispatch_authority_clock_mismatch"
            )
        canonical = CapturedPaperTransportDispatchAuthority(
            completion_sha256=self.completion_sha256,
            transport_authority_sha256=self.transport_authority_sha256,
            transport_instruction_sha256=self.transport_instruction_sha256,
            invocation_authority_sha256=self.invocation_authority_sha256,
            financial_breaker_receipt_sha256=(
                self.financial_breaker_receipt_sha256
            ),
            pre_dispatch_evidence_sha256=self.pre_dispatch_evidence_sha256,
            connection_receipt_sha256=self.connection_receipt_sha256,
            lease_token=self.lease_token,
            lease_owner_id=self.lease_owner_id,
            verified_at=self.verified_at,
            valid_until=self.valid_until,
            outbox_version=self.outbox_version,
            dispatch_event_sequence=self.dispatch_event_sequence,
            previous_event_sha256=self.previous_event_sha256,
            process_attestation_hmac_sha256=(
                self.process_attestation_hmac_sha256
            ),
        )
        if canonical.dispatch_authority_sha256 != self.dispatch_authority_sha256:
            raise CapturedPaperOutboxError(
                "transport_dispatch_authority_hash_mismatch"
            )
        _verify_transport_dispatch_process_attestation(self)


@dataclass(frozen=True, slots=True)
class CapturedPaperDurableTransportBundle:
    """Sealed phase-one material sufficient to rebuild one PAPER instruction.

    This object is returned only by the database loader after it has rehashed
    every canonical byte string and rebound the immutable artifacts to the
    reservation, action claim, automation session, opportunity and outbox
    event chain.  It deliberately contains no current config or provider
    capability from which an order could be recomputed after restart.
    """

    request: CapturedPaperPostCommitRequest
    authority: CapturedPaperTransportAuthority
    order_request: dict[str, Any]
    order_request_sha256: str
    admission_record: dict[str, Any]
    admission_record_sha256: str
    committed_admission: dict[str, Any]
    committed_admission_sha256: str
    transport_instruction: dict[str, Any]
    transport_instruction_sha256: str
    reconciliation_retry_delay_seconds: int
    reconciliation_health_escalation_delay_seconds: int

    @property
    def completion_sha256(self) -> str:
        return self.request.completion_sha256


@dataclass(frozen=True, slots=True)
class CapturedPaperBrokerAcceptanceProof:
    """Positive same-CID broker truth; absence/unreadable is never this type."""

    acceptance_kind: str
    completion_sha256: str
    account_scope: str
    expected_account_id: str
    client_order_id: str
    broker_order_id: str
    reservation_id: str
    action_claim_token: str
    binder_id: str
    broker_order_evidence_sha256: str
    observed_at: datetime
    available_at: datetime
    acceptance_sha256: str = ""

    def __post_init__(self) -> None:
        if self.acceptance_kind not in {"post_response", "same_cid_reconciliation"}:
            raise CapturedPaperOutboxError("broker_acceptance_kind_invalid")
        if self.account_scope != ALPACA_PAPER_ACCOUNT_SCOPE:
            raise CapturedPaperOutboxError("broker_acceptance_scope_invalid")
        object.__setattr__(
            self,
            "expected_account_id",
            _canonical_uuid(
                self.expected_account_id,
                field_name="broker_acceptance_account_id",
            ),
        )
        if _BROKER_ID_RE.fullmatch(str(self.client_order_id or "")) is None:
            raise CapturedPaperOutboxError("broker_acceptance_cid_invalid")
        if _BROKER_ID_RE.fullmatch(str(self.broker_order_id or "")) is None:
            raise CapturedPaperOutboxError("broker_acceptance_order_id_invalid")
        if not str(self.action_claim_token).startswith("arm-"):
            raise CapturedPaperOutboxError(
                "broker_acceptance_action_claim_token_invalid"
            )
        _canonical_uuid(
            self.action_claim_token[4:],
            field_name="broker_acceptance_action_claim_generation",
        )
        object.__setattr__(
            self,
            "reservation_id",
            _canonical_uuid(
                self.reservation_id, field_name="broker_acceptance_reservation_id"
            ),
        )
        object.__setattr__(
            self,
            "binder_id",
            _canonical_uuid(self.binder_id, field_name="broker_acceptance_binder_id"),
        )
        for field_name in ("completion_sha256", "broker_order_evidence_sha256"):
            object.__setattr__(
                self,
                field_name,
                _digest(getattr(self, field_name), field_name=field_name),
            )
        observed_at = _aware_utc(
            self.observed_at, field_name="broker_acceptance_observed_at"
        )
        available_at = _aware_utc(
            self.available_at, field_name="broker_acceptance_available_at"
        )
        if observed_at > available_at:
            raise CapturedPaperOutboxError("broker_acceptance_clock_order_invalid")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "available_at", available_at)
        expected_hash = _sha256_text(_canonical_json(self._content_payload()))
        if self.acceptance_sha256:
            supplied = _digest(
                self.acceptance_sha256, field_name="broker_acceptance_sha256"
            )
            if supplied != expected_hash:
                raise CapturedPaperOutboxError("broker_acceptance_hash_mismatch")
        object.__setattr__(self, "acceptance_sha256", expected_hash)

    def _content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": BROKER_ACCEPTANCE_PROOF_SCHEMA_VERSION,
            "acceptance_kind": self.acceptance_kind,
            "completion_sha256": self.completion_sha256,
            "account_scope": self.account_scope,
            "expected_account_id": self.expected_account_id,
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "reservation_id": self.reservation_id,
            "action_claim_token": self.action_claim_token,
            "binder_id": self.binder_id,
            "broker_order_evidence_sha256": self.broker_order_evidence_sha256,
            "observed_at": self.observed_at.isoformat(),
            "available_at": self.available_at.isoformat(),
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            **self._content_payload(),
            "acceptance_sha256": self.acceptance_sha256,
        }

    def verify_for_authority(
        self,
        authority: CapturedPaperTransportAuthority,
        *,
        expected_kind: str,
    ) -> None:
        if type(authority) is not CapturedPaperTransportAuthority:
            raise CapturedPaperOutboxError("transport_authority_type_invalid")
        if self.acceptance_kind != expected_kind:
            raise CapturedPaperOutboxError("broker_acceptance_kind_mismatch")
        expected = {
            "completion_sha256": authority.completion_sha256,
            "account_scope": authority.account_scope,
            "expected_account_id": authority.expected_account_id,
            "client_order_id": authority.client_order_id,
            "reservation_id": authority.reservation_id,
            "action_claim_token": authority.action_claim_token,
            "binder_id": authority.binder_id,
        }
        if any(getattr(self, key) != value for key, value in expected.items()):
            raise CapturedPaperOutboxError(
                "broker_acceptance_authority_binding_mismatch"
            )
        canonical = CapturedPaperBrokerAcceptanceProof(
            acceptance_kind=self.acceptance_kind,
            completion_sha256=self.completion_sha256,
            account_scope=self.account_scope,
            expected_account_id=self.expected_account_id,
            client_order_id=self.client_order_id,
            broker_order_id=self.broker_order_id,
            reservation_id=self.reservation_id,
            action_claim_token=self.action_claim_token,
            binder_id=self.binder_id,
            broker_order_evidence_sha256=self.broker_order_evidence_sha256,
            observed_at=self.observed_at,
            available_at=self.available_at,
        )
        if canonical.acceptance_sha256 != self.acceptance_sha256:
            raise CapturedPaperOutboxError("broker_acceptance_hash_mismatch")


@dataclass(frozen=True, slots=True)
class CapturedPaperPositiveAdoptionLockReceipt:
    """Immutable result of one caller-owned positive-adoption lock walk.

    The receipt is meaningful only while the caller's transaction remains
    open.  It contains no ORM row or Session capability and therefore cannot
    be used as broker truth outside that transaction.
    """

    request: CapturedPaperPostCommitRequest
    acceptance_kind: str
    outbox_status: str
    binding_state: str
    authority_sha256: str
    transport_started_at: datetime
    session_state: str
    session_ended: bool
    broker_order_id: str | None
    broker_connection_generation: str | None
    broker_order_evidence_sha256: str | None
    broker_observed_at: datetime | None
    broker_available_at: datetime | None


@dataclass(frozen=True, slots=True)
class _PositiveAdoptionBindingSnapshot:
    binding_state: str
    transport_started_at: datetime
    session_state: str
    session_ended: bool
    broker_order_id: str | None
    broker_connection_generation: str | None
    broker_order_evidence_sha256: str | None
    broker_observed_at: datetime | None
    broker_available_at: datetime | None


@dataclass(frozen=True, slots=True)
class _TransportInvocationBindingSnapshot:
    transport_started_at: datetime
    marker_lease_token: str
    marker_lease_owner_id: str
    action_lease_expires_at: datetime
    arm_expires_at: datetime


_ROW_COLUMNS = """
    completion_sha256, payload_sha256, route_token_sha256, intent_sha256,
    payload_canonical_json, account_scope, expected_account_id, session_id,
    symbol, decision_id, client_order_id, binder_id, symbol_claim_token,
    confirmed_arm_generation_sha256, opportunity_key_sha256,
    order_request_canonical_json, order_request_sha256,
    transport_authority_canonical_json, transport_authority_sha256,
    admission_record_canonical_json, admission_record_sha256,
    committed_admission_canonical_json, committed_admission_sha256,
    transport_instruction_canonical_json, transport_instruction_sha256,
    reconciliation_retry_delay_seconds,
    reconciliation_health_escalation_delay_seconds,
    reconciliation_next_attempt_at, reconciliation_total_attempt_count,
    reconciliation_health_state, reconciliation_escalation_count,
    last_reconciliation_health_escalated_at, status,
    attempt_count, max_attempts, reconciliation_attempt_count,
    max_reconciliation_attempts, lease_token, lease_owner_id,
    lease_expires_at, next_attempt_at, transport_started_at,
    transport_evidence_sha256, transport_indeterminate_at,
    indeterminate_evidence_sha256, last_failure_sha256,
    last_reconciliation_evidence_sha256, completion_proof_sha256,
    completed_at, fill_handoff_proof_canonical_json,
    fill_handoff_proof_sha256, fill_handoff_receipt_canonical_json,
    fill_handoff_receipt_sha256, fill_handoff_committed_at,
    event_sequence, last_event_sha256, version,
    created_at, updated_at
"""


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise CapturedPaperOutboxError(f"{field_name}_invalid")
    return normalized


def _transport_dispatch_process_attestation(
    dispatch_authority_sha256: str,
) -> str:
    digest = _digest(
        dispatch_authority_sha256,
        field_name="transport_dispatch_authority_sha256",
    )
    return hmac.new(
        _TRANSPORT_DISPATCH_ATTESTATION_KEY,
        digest.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _attest_transport_dispatch_authority(
    authority: CapturedPaperTransportDispatchAuthority,
) -> CapturedPaperTransportDispatchAuthority:
    if type(authority) is not CapturedPaperTransportDispatchAuthority:
        raise CapturedPaperOutboxError(
            "transport_dispatch_authority_type_invalid"
        )
    expected = _transport_dispatch_process_attestation(
        authority.dispatch_authority_sha256
    )
    supplied = authority.process_attestation_hmac_sha256
    if supplied:
        if not hmac.compare_digest(supplied, expected):
            raise CapturedPaperOutboxError(
                "transport_dispatch_process_attestation_invalid"
            )
        attested = authority
    else:
        attested = replace(
            authority,
            process_attestation_hmac_sha256=expected,
        )
    with _TRANSPORT_DISPATCH_ATTESTATION_LOCK:
        _TRANSPORT_DISPATCH_ATTESTATIONS[
            attested.dispatch_authority_sha256
        ] = expected
    return attested


def _verify_transport_dispatch_process_attestation(
    authority: CapturedPaperTransportDispatchAuthority,
) -> None:
    if type(authority) is not CapturedPaperTransportDispatchAuthority:
        raise CapturedPaperOutboxError(
            "transport_dispatch_authority_type_invalid"
        )
    supplied = authority.process_attestation_hmac_sha256
    expected = _transport_dispatch_process_attestation(
        authority.dispatch_authority_sha256
    )
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise CapturedPaperOutboxError(
            "transport_dispatch_process_attestation_invalid"
        )


def _consume_transport_dispatch_process_attestation(
    authority: CapturedPaperTransportDispatchAuthority,
) -> None:
    """Consume the process-local half of the one-shot dispatch capability."""

    _verify_transport_dispatch_process_attestation(authority)
    with _TRANSPORT_DISPATCH_ATTESTATION_LOCK:
        registered = _TRANSPORT_DISPATCH_ATTESTATIONS.pop(
            authority.dispatch_authority_sha256,
            None,
        )
    if registered is None or not hmac.compare_digest(
        registered,
        authority.process_attestation_hmac_sha256,
    ):
        raise CapturedPaperOutboxError(
            "transport_dispatch_process_attestation_not_registered"
        )


def _canonical_uuid(value: str, *, field_name: str) -> str:
    raw = str(value or "").strip().lower()
    try:
        parsed = uuid.UUID(raw)
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedPaperOutboxError(f"{field_name}_invalid") from exc
    if raw != str(parsed):
        raise CapturedPaperOutboxError(f"{field_name}_invalid")
    return raw


def _bounded_positive_int(value: int, *, field_name: str, maximum: int) -> int:
    if isinstance(value, bool):
        raise CapturedPaperOutboxError(f"{field_name}_invalid")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise CapturedPaperOutboxError(f"{field_name}_invalid") from exc
    if normalized <= 0 or normalized > maximum:
        raise CapturedPaperOutboxError(f"{field_name}_invalid")
    return normalized


def _bounded_nonnegative_int(
    value: int,
    *,
    field_name: str,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise CapturedPaperOutboxError(f"{field_name}_invalid")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise CapturedPaperOutboxError(f"{field_name}_invalid") from exc
    if normalized < 0 or normalized > maximum:
        raise CapturedPaperOutboxError(f"{field_name}_invalid")
    return normalized


def _decimal(value: Any, *, field_name: str) -> Decimal:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CapturedPaperOutboxError(f"{field_name}_invalid") from exc
    if not normalized.is_finite():
        raise CapturedPaperOutboxError(f"{field_name}_invalid")
    return normalized


def _aware_utc(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CapturedPaperOutboxCorruptionError(f"{field_name}_invalid")
    try:
        offset = value.utcoffset()
    except Exception as exc:
        raise CapturedPaperOutboxCorruptionError(f"{field_name}_invalid") from exc
    if offset is None:
        raise CapturedPaperOutboxCorruptionError(f"{field_name}_invalid")
    return value.astimezone(UTC)


def _optional_utc(value: Any, *, field_name: str) -> datetime | None:
    return None if value is None else _aware_utc(value, field_name=field_name)


def _iso_utc(value: Any, *, field_name: str) -> datetime:
    if type(value) is not str or not value:
        raise CapturedPaperOutboxError(f"{field_name}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapturedPaperOutboxError(f"{field_name}_invalid") from exc
    return _aware_utc(parsed, field_name=field_name)


def _db_now(db: Any) -> datetime:
    value = db.execute(text("SELECT clock_timestamp()")).scalar_one()
    return _aware_utc(value, field_name="outbox_db_clock")


def _strict_event_payload(value: str) -> dict[str, Any]:
    return _strict_canonical_payload(
        value,
        invalid_reason="outbox_event_payload_json_invalid",
        duplicate_reason="outbox_event_payload_duplicate_key",
        nonfinite_reason="outbox_event_payload_nonfinite",
        noncanonical_reason="outbox_event_payload_not_canonical",
    )


def _strict_canonical_payload(
    value: str,
    *,
    invalid_reason: str,
    duplicate_reason: str,
    nonfinite_reason: str,
    noncanonical_reason: str,
) -> dict[str, Any]:
    if type(value) is not str or not value:
        raise CapturedPaperOutboxCorruptionError(invalid_reason)

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise CapturedPaperOutboxCorruptionError(duplicate_reason)
            result[key] = item
        return result

    def reject_constant(_value: str) -> None:
        raise CapturedPaperOutboxCorruptionError(nonfinite_reason)

    try:
        payload = json.loads(
            value,
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except CapturedPaperOutboxCorruptionError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CapturedPaperOutboxCorruptionError(invalid_reason) from exc
    if type(payload) is not dict or _canonical_json(payload) != value:
        raise CapturedPaperOutboxCorruptionError(noncanonical_reason)
    return payload


def _event_hash(
    *,
    completion_sha256: str,
    sequence: int,
    event_type: str,
    previous_event_sha256: str | None,
    event_payload_sha256: str,
    effective_at: datetime,
) -> str:
    return _sha256_text(
        _canonical_json(
            {
                "schema_version": OUTBOX_EVENT_SCHEMA_VERSION,
                "completion_sha256": completion_sha256,
                "sequence": sequence,
                "event_type": event_type,
                "previous_event_sha256": previous_event_sha256,
                "event_payload_sha256": event_payload_sha256,
                "effective_at": effective_at.astimezone(UTC).isoformat(),
            }
        )
    )


def _load_events(
    db: Any,
    *,
    completion_sha256: str,
) -> tuple[CapturedPaperOutboxEvent, ...]:
    rows = db.execute(
        text(
            """
            SELECT sequence, event_type, previous_event_sha256, event_sha256,
                   event_payload_sha256, event_payload_canonical_json,
                   effective_at
              FROM captured_paper_post_commit_outbox_events
             WHERE completion_sha256 = :completion_sha256
             ORDER BY sequence ASC
            """
        ),
        {"completion_sha256": completion_sha256},
    ).mappings().all()
    events: list[CapturedPaperOutboxEvent] = []
    previous: str | None = None
    for expected_sequence, row in enumerate(rows, start=1):
        sequence = int(row["sequence"])
        if sequence != expected_sequence:
            raise CapturedPaperOutboxCorruptionError(
                "outbox_event_sequence_gap"
            )
        row_previous = row["previous_event_sha256"]
        if row_previous != previous:
            raise CapturedPaperOutboxCorruptionError(
                "outbox_event_previous_hash_mismatch"
            )
        payload_json = str(row["event_payload_canonical_json"])
        payload = _strict_event_payload(payload_json)
        payload_hash = _sha256_text(payload_json)
        if payload_hash != row["event_payload_sha256"]:
            raise CapturedPaperOutboxCorruptionError(
                "outbox_event_payload_hash_mismatch"
            )
        effective_at = _aware_utc(
            row["effective_at"], field_name="outbox_event_effective_at"
        )
        expected_hash = _event_hash(
            completion_sha256=completion_sha256,
            sequence=sequence,
            event_type=str(row["event_type"]),
            previous_event_sha256=previous,
            event_payload_sha256=payload_hash,
            effective_at=effective_at,
        )
        if expected_hash != row["event_sha256"]:
            raise CapturedPaperOutboxCorruptionError(
                "outbox_event_hash_mismatch"
            )
        event = CapturedPaperOutboxEvent(
            sequence=sequence,
            event_type=str(row["event_type"]),
            previous_event_sha256=previous,
            event_sha256=expected_hash,
            event_payload_sha256=payload_hash,
            event_payload=payload,
            effective_at=effective_at,
        )
        events.append(event)
        previous = expected_hash
    return tuple(events)


def _request_from_row(row: Mapping[str, Any]) -> CapturedPaperPostCommitRequest:
    canonical_json = str(row["payload_canonical_json"])
    try:
        request = CapturedPaperPostCommitRequest.from_canonical_json(
            canonical_json
        )
    except CapturedPaperIntentContractError as exc:
        raise CapturedPaperOutboxCorruptionError(
            f"outbox_request_{exc.reason}"
        ) from exc
    if _sha256_text(canonical_json) != row["payload_sha256"]:
        raise CapturedPaperOutboxCorruptionError(
            "outbox_payload_hash_mismatch"
        )
    intent = request.intent
    route = intent.route_token
    opportunity_hash = (
        intent.opportunity_key.opportunity_key_sha256
        if intent.opportunity_key is not None
        else None
    )
    expected = {
        "completion_sha256": request.completion_sha256,
        "route_token_sha256": route.route_token_sha256,
        "intent_sha256": intent.intent_sha256,
        "account_scope": route.account_scope,
        "expected_account_id": route.expected_account_id,
        "session_id": route.session_id,
        "symbol": route.symbol,
        "decision_id": intent.decision_id,
        "client_order_id": intent.client_order_id,
        "binder_id": intent.binder_id,
        "symbol_claim_token": intent.symbol_claim_token,
        "confirmed_arm_generation_sha256": (
            intent.confirmed_arm_generation.confirmed_arm_generation_sha256
        ),
        "opportunity_key_sha256": opportunity_hash,
    }
    for field_name, expected_value in expected.items():
        actual = row[field_name]
        if isinstance(actual, uuid.UUID):
            actual = str(actual)
        if actual != expected_value:
            raise CapturedPaperOutboxCorruptionError(
                f"outbox_{field_name}_mismatch"
            )
    if intent.decision_id != intent.client_order_id:
        raise CapturedPaperOutboxCorruptionError(
            "outbox_decision_client_order_id_mismatch"
        )
    return request


def _positive_decimal_text(value: Any, *, field_name: str) -> str:
    number = _decimal(value, field_name=field_name)
    if number <= 0:
        raise CapturedPaperOutboxError(f"{field_name}_invalid")
    normalized = format(number.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized


def _canonical_artifact_from_row(
    row: Mapping[str, Any],
    *,
    json_column: str,
    hash_column: str,
) -> dict[str, Any]:
    canonical_json = str(row[json_column])
    payload = _strict_canonical_payload(
        canonical_json,
        invalid_reason=f"outbox_{json_column}_invalid",
        duplicate_reason=f"outbox_{json_column}_duplicate_key",
        nonfinite_reason=f"outbox_{json_column}_nonfinite",
        noncanonical_reason=f"outbox_{json_column}_not_canonical",
    )
    if _sha256_text(canonical_json) != row[hash_column]:
        raise CapturedPaperOutboxCorruptionError(
            f"outbox_{hash_column}_mismatch"
        )
    return payload


def build_captured_paper_durable_transport_artifacts(
    *,
    request: CapturedPaperPostCommitRequest,
    authority: CapturedPaperTransportAuthority,
    order_request: Mapping[str, Any],
    order_request_sha256: str,
    admission_record: Mapping[str, Any],
    admission_record_sha256: str,
    quantity_shares: int,
    structural_risk_usd: Any,
    gross_notional_usd: Any,
    buying_power_impact_usd: Any,
    operational_policy_sha256: str,
    committed_at: datetime,
    lock_order: tuple[str, ...],
    reconciliation_retry_delay_seconds: int,
    reconciliation_health_escalation_delay_seconds: int,
) -> dict[str, Any]:
    """Build exact phase-one artifacts without consulting mutable runtime state."""

    if type(request) is not CapturedPaperPostCommitRequest:
        raise CapturedPaperOutboxError("durable_transport_request_type_invalid")
    request.verify()
    if type(authority) is not CapturedPaperTransportAuthority:
        raise CapturedPaperOutboxError("durable_transport_authority_type_invalid")
    authority.verify_for_request(request)
    order = dict(order_request)
    if frozenset(order) != _TRANSPORT_ORDER_FIELDS:
        raise CapturedPaperOutboxError("durable_transport_order_shape_invalid")
    order_json = _canonical_json(order)
    order_hash = _digest(
        order_request_sha256,
        field_name="durable_transport_order_request_sha256",
    )
    if _sha256_text(order_json) != order_hash:
        raise CapturedPaperOutboxError("durable_transport_order_hash_mismatch")
    intent = request.intent
    route = intent.route_token
    quantity = _bounded_positive_int(
        quantity_shares,
        field_name="durable_transport_quantity_shares",
        maximum=2_147_483_647,
    )
    try:
        order_quantity = int(order.get("qty"))
    except (TypeError, ValueError) as exc:
        raise CapturedPaperOutboxError(
            "durable_transport_order_quantity_invalid"
        ) from exc
    if not (
        order_quantity == quantity
        and order.get("client_order_id") == intent.client_order_id
        and order.get("symbol") == route.symbol
        and order.get("side") == "buy"
        and order.get("type") == "limit"
        and order.get("asset_class") == "us_equity"
        and order.get("position_intent") == "buy_to_open"
        and _decimal(
            order.get("limit_price"),
            field_name="durable_transport_order_limit_price",
        )
        == _decimal(
            intent.entry_limit_ceiling_price,
            field_name="durable_transport_intent_limit_price",
        )
        and order.get("time_in_force") in {"day", "gtc"}
        and type(order.get("extended_hours")) is bool
        and not (
            order.get("extended_hours") is True
            and order.get("time_in_force") != "day"
        )
        and authority.broker_request_sha256 == order_hash
    ):
        raise CapturedPaperOutboxError("durable_transport_order_binding_mismatch")

    admission = dict(admission_record)
    admission_json = _canonical_json(admission)
    admission_hash = _digest(
        admission_record_sha256,
        field_name="durable_admission_record_sha256",
    )
    if _sha256_text(admission_json) != admission_hash:
        raise CapturedPaperOutboxError("durable_admission_record_hash_mismatch")
    policy_hash = _digest(
        operational_policy_sha256,
        field_name="durable_operational_policy_sha256",
    )
    exact_admission = {
        "completion_sha256": request.completion_sha256,
        "route_token_sha256": route.route_token_sha256,
        "intent_sha256": intent.intent_sha256,
        "reservation_id": authority.reservation_id,
        "decision_packet_sha256": authority.decision_packet_sha256,
        "reservation_request_sha256": authority.reservation_request_sha256,
        "adaptive_input_evidence_sha256": authority.admission_evidence_sha256,
        "account_identity_sha256": authority.account_identity_sha256,
        "quantity_shares": quantity,
        "order_request_sha256": order_hash,
        "operational_policy_sha256": policy_hash,
        "lock_order": list(lock_order),
    }
    if any(admission.get(key) != value for key, value in exact_admission.items()):
        raise CapturedPaperOutboxError("durable_admission_record_binding_mismatch")

    instruction = {
        "schema_version": DURABLE_TRANSPORT_INSTRUCTION_SCHEMA_VERSION,
        "completion_sha256": request.completion_sha256,
        "transport_authority_sha256": authority.authority_sha256,
        "account_scope": route.account_scope,
        "expected_account_id": route.expected_account_id,
        "client_order_id": intent.client_order_id,
        "reservation_id": authority.reservation_id,
        "decision_packet_sha256": authority.decision_packet_sha256,
        "reservation_request_sha256": authority.reservation_request_sha256,
        "order_request_sha256": order_hash,
    }
    instruction_json = _canonical_json(instruction)
    instruction_hash = _sha256_text(instruction_json)
    # The stored digest is over the authority body.  Do not include the
    # self-describing ``authority_sha256`` field in the bytes protected by
    # that same digest; the loader reconstructs and checks it independently.
    authority_payload = authority._content_payload()
    authority_json = _canonical_json(authority_payload)
    if _sha256_text(_canonical_json(authority._content_payload())) != authority.authority_sha256:
        raise CapturedPaperOutboxError("durable_transport_authority_hash_mismatch")

    retry_delay = _bounded_positive_int(
        reconciliation_retry_delay_seconds,
        field_name="reconciliation_retry_delay_seconds",
        maximum=_MAX_RETRY_DELAY_SECONDS,
    )
    escalation_delay = _bounded_positive_int(
        reconciliation_health_escalation_delay_seconds,
        field_name="reconciliation_health_escalation_delay_seconds",
        maximum=_MAX_RETRY_DELAY_SECONDS,
    )
    committed_clock = _aware_utc(
        committed_at,
        field_name="durable_committed_admission_at",
    )
    committed = {
        "schema_version": DURABLE_COMMITTED_ADMISSION_SCHEMA_VERSION,
        "completion_sha256": request.completion_sha256,
        "payload_sha256": _sha256_text(request.to_canonical_json()),
        "route_token_sha256": route.route_token_sha256,
        "intent_sha256": intent.intent_sha256,
        "reservation_id": authority.reservation_id,
        "decision_packet_sha256": authority.decision_packet_sha256,
        "reservation_request_sha256": authority.reservation_request_sha256,
        "adaptive_input_evidence_sha256": authority.admission_evidence_sha256,
        "account_identity_sha256": authority.account_identity_sha256,
        "quantity_shares": quantity,
        "structural_risk_usd": _positive_decimal_text(
            structural_risk_usd,
            field_name="durable_structural_risk_usd",
        ),
        "gross_notional_usd": _positive_decimal_text(
            gross_notional_usd,
            field_name="durable_gross_notional_usd",
        ),
        "buying_power_impact_usd": _positive_decimal_text(
            buying_power_impact_usd,
            field_name="durable_buying_power_impact_usd",
        ),
        "order_request_sha256": order_hash,
        "transport_authority_sha256": authority.authority_sha256,
        "transport_instruction_sha256": instruction_hash,
        "admission_record_sha256": admission_hash,
        "operational_policy_sha256": policy_hash,
        "reconciliation_retry_delay_seconds": retry_delay,
        "reconciliation_health_escalation_delay_seconds": escalation_delay,
        "committed_at": committed_clock.isoformat(),
        "lock_order": list(lock_order),
    }
    committed_json = _canonical_json(committed)
    return {
        "order_request_canonical_json": order_json,
        "order_request_sha256": order_hash,
        "transport_authority_canonical_json": authority_json,
        "transport_authority_sha256": authority.authority_sha256,
        "admission_record_canonical_json": admission_json,
        "admission_record_sha256": admission_hash,
        "committed_admission_canonical_json": committed_json,
        "committed_admission_sha256": _sha256_text(committed_json),
        "transport_instruction_canonical_json": instruction_json,
        "transport_instruction_sha256": instruction_hash,
        "reconciliation_retry_delay_seconds": retry_delay,
        "reconciliation_health_escalation_delay_seconds": escalation_delay,
    }


def _durable_bundle_from_row(
    row: Mapping[str, Any],
) -> CapturedPaperDurableTransportBundle:
    request = _request_from_row(row)
    order = _canonical_artifact_from_row(
        row,
        json_column="order_request_canonical_json",
        hash_column="order_request_sha256",
    )
    authority_payload = _canonical_artifact_from_row(
        row,
        json_column="transport_authority_canonical_json",
        hash_column="transport_authority_sha256",
    )
    if authority_payload.pop("schema_version", None) != TRANSPORT_AUTHORITY_SCHEMA_VERSION:
        raise CapturedPaperOutboxCorruptionError(
            "outbox_transport_authority_schema_mismatch"
        )
    try:
        authority = CapturedPaperTransportAuthority(**authority_payload)
    except (TypeError, CapturedPaperOutboxError) as exc:
        raise CapturedPaperOutboxCorruptionError(
            "outbox_transport_authority_invalid"
        ) from exc
    authority.verify_for_request(request)
    if authority.authority_sha256 != row["transport_authority_sha256"]:
        raise CapturedPaperOutboxCorruptionError(
            "outbox_transport_authority_hash_mismatch"
        )
    admission = _canonical_artifact_from_row(
        row,
        json_column="admission_record_canonical_json",
        hash_column="admission_record_sha256",
    )
    committed = _canonical_artifact_from_row(
        row,
        json_column="committed_admission_canonical_json",
        hash_column="committed_admission_sha256",
    )
    instruction = _canonical_artifact_from_row(
        row,
        json_column="transport_instruction_canonical_json",
        hash_column="transport_instruction_sha256",
    )
    rebuilt = build_captured_paper_durable_transport_artifacts(
        request=request,
        authority=authority,
        order_request=order,
        order_request_sha256=str(row["order_request_sha256"]),
        admission_record=admission,
        admission_record_sha256=str(row["admission_record_sha256"]),
        quantity_shares=committed.get("quantity_shares"),
        structural_risk_usd=committed.get("structural_risk_usd"),
        gross_notional_usd=committed.get("gross_notional_usd"),
        buying_power_impact_usd=committed.get("buying_power_impact_usd"),
        operational_policy_sha256=committed.get("operational_policy_sha256"),
        committed_at=_iso_utc(
            committed.get("committed_at"),
            field_name="durable_committed_admission_at",
        ),
        lock_order=tuple(committed.get("lock_order") or ()),
        reconciliation_retry_delay_seconds=(
            row["reconciliation_retry_delay_seconds"]
        ),
        reconciliation_health_escalation_delay_seconds=(
            row["reconciliation_health_escalation_delay_seconds"]
        ),
    )
    exact_columns = (
        "order_request_canonical_json",
        "order_request_sha256",
        "transport_authority_canonical_json",
        "transport_authority_sha256",
        "admission_record_canonical_json",
        "admission_record_sha256",
        "committed_admission_canonical_json",
        "committed_admission_sha256",
        "transport_instruction_canonical_json",
        "transport_instruction_sha256",
    )
    if any(rebuilt[name] != row[name] for name in exact_columns):
        raise CapturedPaperOutboxCorruptionError(
            "outbox_durable_transport_artifact_mismatch"
        )
    return CapturedPaperDurableTransportBundle(
        request=request,
        authority=authority,
        order_request=order,
        order_request_sha256=str(row["order_request_sha256"]),
        admission_record=admission,
        admission_record_sha256=str(row["admission_record_sha256"]),
        committed_admission=committed,
        committed_admission_sha256=str(row["committed_admission_sha256"]),
        transport_instruction=instruction,
        transport_instruction_sha256=str(row["transport_instruction_sha256"]),
        reconciliation_retry_delay_seconds=int(
            row["reconciliation_retry_delay_seconds"]
        ),
        reconciliation_health_escalation_delay_seconds=int(
            row["reconciliation_health_escalation_delay_seconds"]
        ),
    )


def _validate_event_state_machine(
    events: tuple[CapturedPaperOutboxEvent, ...],
    *,
    snapshot_status: str,
) -> None:
    state: str | None = None
    invocation_authorized = False
    financial_breaker_recorded = False
    invocation_consumed = False
    for event in events:
        event_type = event.event_type
        if event_type == "enqueued" and state is None:
            state = OUTBOX_STATUS_PENDING
        elif event_type == "leased" and state in {
            OUTBOX_STATUS_PENDING,
            OUTBOX_STATUS_RETRY_WAIT,
        }:
            state = OUTBOX_STATUS_LEASED
        elif event_type == "lease_recovered" and state == OUTBOX_STATUS_LEASED:
            state = OUTBOX_STATUS_LEASED
        elif event_type == "retry_scheduled" and state == OUTBOX_STATUS_LEASED:
            state = OUTBOX_STATUS_RETRY_WAIT
        elif event_type == "retry_exhausted" and state in {
            OUTBOX_STATUS_PENDING,
            OUTBOX_STATUS_RETRY_WAIT,
            OUTBOX_STATUS_LEASED,
        }:
            state = OUTBOX_STATUS_RETRY_EXHAUSTED
        elif event_type == "transport_started" and state == OUTBOX_STATUS_LEASED:
            state = OUTBOX_STATUS_TRANSPORT_STARTED
        elif (
            event_type == "transport_invocation_authorized"
            and state == OUTBOX_STATUS_TRANSPORT_STARTED
            and not invocation_authorized
        ):
            invocation_authorized = True
            state = OUTBOX_STATUS_TRANSPORT_STARTED
        elif (
            event_type == "transport_financial_breaker_recorded"
            and state == OUTBOX_STATUS_TRANSPORT_STARTED
            and invocation_authorized
            and not financial_breaker_recorded
        ):
            financial_breaker_recorded = True
            state = OUTBOX_STATUS_TRANSPORT_STARTED
        elif (
            event_type == "transport_invocation_consumed"
            and state == OUTBOX_STATUS_TRANSPORT_STARTED
            and invocation_authorized
            and financial_breaker_recorded
            and not invocation_consumed
        ):
            invocation_consumed = True
            state = OUTBOX_STATUS_TRANSPORT_STARTED
        elif event_type in {
            "transport_indeterminate",
            "expired_transport_recovery",
        } and state == OUTBOX_STATUS_TRANSPORT_STARTED:
            state = OUTBOX_STATUS_TRANSPORT_INDETERMINATE
        elif (
            event_type == "reconciliation_leased"
            and state == OUTBOX_STATUS_TRANSPORT_INDETERMINATE
        ):
            state = OUTBOX_STATUS_RECONCILING
        elif (
            event_type == "reconciliation_health_backoff"
            and state == OUTBOX_STATUS_TRANSPORT_INDETERMINATE
        ):
            state = OUTBOX_STATUS_TRANSPORT_INDETERMINATE
        elif event_type in {
            "reconciliation_pending",
            "reconciliation_health_escalated",
            "expired_reconciliation_recovery",
        } and state == OUTBOX_STATUS_RECONCILING:
            state = OUTBOX_STATUS_TRANSPORT_INDETERMINATE
        elif (
            event_type == "completion_accepted"
            and state == OUTBOX_STATUS_TRANSPORT_STARTED
            and financial_breaker_recorded
            and invocation_consumed
        ):
            state = OUTBOX_STATUS_COMPLETED
        elif (
            event_type == "reconciliation_accepted"
            and state == OUTBOX_STATUS_RECONCILING
        ):
            state = OUTBOX_STATUS_COMPLETED
        elif (
            event_type == "fill_handoff_committed"
            and state in {
                OUTBOX_STATUS_TRANSPORT_INDETERMINATE,
                OUTBOX_STATUS_COMPLETED,
            }
        ):
            state = OUTBOX_STATUS_FILL_HANDOFF_COMMITTED
        else:
            raise CapturedPaperOutboxCorruptionError(
                "outbox_event_state_transition_invalid"
            )
    if state != snapshot_status:
        raise CapturedPaperOutboxCorruptionError(
            "outbox_event_snapshot_status_mismatch"
        )


def _record_from_row(db: Any, row: Mapping[str, Any]) -> CapturedPaperOutboxRecord:
    request = _request_from_row(row)
    durable_transport = _durable_bundle_from_row(row)
    status = str(row["status"])
    if status not in _OUTBOX_STATUSES:
        raise CapturedPaperOutboxCorruptionError("outbox_status_invalid")
    lease_token = str(row["lease_token"]) if row["lease_token"] is not None else None
    lease_owner_id = (
        str(row["lease_owner_id"])
        if row["lease_owner_id"] is not None
        else None
    )
    lease_expires_at = _optional_utc(
        row["lease_expires_at"], field_name="outbox_lease_expires_at"
    )
    lease_present = all(
        value is not None
        for value in (lease_token, lease_owner_id, lease_expires_at)
    )
    lease_absent = all(
        value is None
        for value in (lease_token, lease_owner_id, lease_expires_at)
    )
    if status in _LEASED_STATUSES and not lease_present:
        raise CapturedPaperOutboxCorruptionError(
            "outbox_lease_shape_invalid"
        )
    if status not in _LEASED_STATUSES and not lease_absent:
        raise CapturedPaperOutboxCorruptionError(
            "outbox_lease_shape_invalid"
        )
    next_attempt_at = _optional_utc(
        row["next_attempt_at"], field_name="outbox_next_attempt_at"
    )
    if (status == OUTBOX_STATUS_RETRY_WAIT) != (next_attempt_at is not None):
        raise CapturedPaperOutboxCorruptionError(
            "outbox_next_attempt_shape_invalid"
        )
    transport_started_at = _optional_utc(
        row["transport_started_at"], field_name="outbox_transport_started_at"
    )
    transport_evidence = row["transport_evidence_sha256"]
    transport_bound = transport_started_at is not None and transport_evidence is not None
    if status in {
        OUTBOX_STATUS_TRANSPORT_STARTED,
        OUTBOX_STATUS_TRANSPORT_INDETERMINATE,
        OUTBOX_STATUS_RECONCILING,
        OUTBOX_STATUS_FILL_HANDOFF_COMMITTED,
        OUTBOX_STATUS_COMPLETED,
    } and not transport_bound:
        raise CapturedPaperOutboxCorruptionError(
            "outbox_transport_marker_missing"
        )
    if status in {
        OUTBOX_STATUS_PENDING,
        OUTBOX_STATUS_LEASED,
        OUTBOX_STATUS_RETRY_WAIT,
        OUTBOX_STATUS_RETRY_EXHAUSTED,
    } and (transport_started_at is not None or transport_evidence is not None):
        raise CapturedPaperOutboxCorruptionError(
            "outbox_transport_marker_unexpected"
        )
    indeterminate_at = _optional_utc(
        row["transport_indeterminate_at"],
        field_name="outbox_transport_indeterminate_at",
    )
    indeterminate_evidence = row["indeterminate_evidence_sha256"]
    indeterminate_bound = (
        indeterminate_at is not None and indeterminate_evidence is not None
    )
    indeterminate_absent = (
        indeterminate_at is None and indeterminate_evidence is None
    )
    if not (indeterminate_bound or indeterminate_absent):
        raise CapturedPaperOutboxCorruptionError(
            "outbox_indeterminate_marker_shape_invalid"
        )
    if status in {
        OUTBOX_STATUS_TRANSPORT_INDETERMINATE,
        OUTBOX_STATUS_RECONCILING,
    } and not indeterminate_bound:
        raise CapturedPaperOutboxCorruptionError(
            "outbox_indeterminate_marker_missing"
        )
    if status not in {
        OUTBOX_STATUS_TRANSPORT_INDETERMINATE,
        OUTBOX_STATUS_RECONCILING,
        OUTBOX_STATUS_FILL_HANDOFF_COMMITTED,
        OUTBOX_STATUS_COMPLETED,
    } and not indeterminate_absent:
        raise CapturedPaperOutboxCorruptionError(
            "outbox_indeterminate_marker_unexpected"
        )
    completion_proof = row["completion_proof_sha256"]
    completed_at = _optional_utc(
        row["completed_at"], field_name="outbox_completed_at"
    )
    completion_bound = completion_proof is not None and completed_at is not None
    completion_absent = completion_proof is None and completed_at is None
    if status == OUTBOX_STATUS_COMPLETED:
        if completion_proof is None or completed_at is None:
            raise CapturedPaperOutboxCorruptionError(
                "outbox_completion_marker_missing"
            )
    elif status == OUTBOX_STATUS_FILL_HANDOFF_COMMITTED:
        if not (completion_bound or completion_absent):
            raise CapturedPaperOutboxCorruptionError(
                "outbox_completion_marker_shape_invalid"
            )
        if not (completion_bound or indeterminate_bound):
            raise CapturedPaperOutboxCorruptionError(
                "outbox_fill_handoff_predecessor_missing"
            )
    elif not completion_absent:
        raise CapturedPaperOutboxCorruptionError(
            "outbox_completion_marker_unexpected"
        )
    handoff_json = row["fill_handoff_proof_canonical_json"]
    handoff_sha256 = row["fill_handoff_proof_sha256"]
    receipt_json = row["fill_handoff_receipt_canonical_json"]
    receipt_sha256 = row["fill_handoff_receipt_sha256"]
    handoff_committed_at = _optional_utc(
        row["fill_handoff_committed_at"],
        field_name="outbox_fill_handoff_committed_at",
    )
    handoff_values = (
        handoff_json,
        handoff_sha256,
        receipt_json,
        receipt_sha256,
        handoff_committed_at,
    )
    fill_handoff_proof: dict[str, Any] | None = None
    fill_handoff_receipt: dict[str, Any] | None = None
    if status == OUTBOX_STATUS_FILL_HANDOFF_COMMITTED:
        if any(value is None for value in handoff_values):
            raise CapturedPaperOutboxCorruptionError(
                "outbox_fill_handoff_missing"
            )
        fill_handoff_proof = _canonical_artifact_from_row(
            row,
            json_column="fill_handoff_proof_canonical_json",
            hash_column="fill_handoff_proof_sha256",
        )
        from .alpaca_fill_activity import AlpacaPaperEntryFillHandoffProof

        try:
            typed_handoff = (
                AlpacaPaperEntryFillHandoffProof.from_canonical_json(
                    str(handoff_json)
                )
            )
        except Exception as exc:
            raise CapturedPaperOutboxCorruptionError(
                "outbox_fill_handoff_proof_invalid"
            ) from exc
        if not (
            typed_handoff.proof_sha256 == str(handoff_sha256)
            and typed_handoff.to_payload().get("proof_sha256")
            == str(handoff_sha256)
        ):
            raise CapturedPaperOutboxCorruptionError(
                "outbox_fill_handoff_proof_hash_mismatch"
            )
        fill_handoff_receipt = _canonical_artifact_from_row(
            row,
            json_column="fill_handoff_receipt_canonical_json",
            hash_column="fill_handoff_receipt_sha256",
        )
        expected_receipt = {
            "schema_version": CAPTURED_PAPER_FILL_HANDOFF_RECEIPT_SCHEMA_VERSION,
            "completion_sha256": request.completion_sha256,
            "transport_authority_sha256": (
                durable_transport.authority.authority_sha256
            ),
            "account_scope": request.intent.route_token.account_scope,
            "expected_account_id": (
                request.intent.route_token.expected_account_id
            ),
            "client_order_id": request.intent.client_order_id,
            "reservation_id": durable_transport.authority.reservation_id,
            "publication_kind": fill_handoff_proof.get("publication_kind"),
            "broker_order_id": fill_handoff_proof.get("broker_order_id"),
            "observation_sha256": fill_handoff_proof.get(
                "observation_sha256"
            ),
            "durability_kind": fill_handoff_proof.get("durability_kind"),
            "source_record_table": fill_handoff_proof.get(
                "source_record_table"
            ),
            "source_record_id": fill_handoff_proof.get("source_record_id"),
            "terminal_evidence_sha256": fill_handoff_proof.get(
                "terminal_evidence_sha256"
            ),
            "immutable_fill_identity_sha256": fill_handoff_proof.get(
                "immutable_fill_identity_sha256"
            ),
            "cumulative_filled_quantity_shares": fill_handoff_proof.get(
                "cumulative_filled_quantity_shares"
            ),
            "lifecycle_event_sha256": fill_handoff_proof.get(
                "lifecycle_event_sha256"
            ),
            "resulting_reservation_state": fill_handoff_proof.get(
                "resulting_reservation_state"
            ),
            "prior_completion_proof_sha256": (
                str(completion_proof) if completion_proof is not None else None
            ),
            "prior_completed_at": (
                completed_at.isoformat() if completed_at is not None else None
            ),
            "fill_handoff_proof_sha256": str(handoff_sha256),
            "committed_at": handoff_committed_at.isoformat(),
        }
        if any(
            fill_handoff_receipt.get(name) != value
            for name, value in expected_receipt.items()
        ):
            raise CapturedPaperOutboxCorruptionError(
                "outbox_fill_handoff_receipt_mismatch"
            )
    elif any(value is not None for value in handoff_values):
        raise CapturedPaperOutboxCorruptionError(
            "outbox_fill_handoff_unexpected"
        )
    events = _load_events(db, completion_sha256=request.completion_sha256)
    event_sequence = int(row["event_sequence"])
    last_event_sha256 = row["last_event_sha256"]
    expected_head = events[-1].event_sha256 if events else None
    if event_sequence != len(events) or last_event_sha256 != expected_head:
        raise CapturedPaperOutboxCorruptionError(
            "outbox_event_head_mismatch"
        )
    _validate_event_state_machine(events, snapshot_status=status)
    if not events or events[0].event_type != "enqueued":
        raise CapturedPaperOutboxCorruptionError(
            "outbox_durable_transport_enqueue_event_missing"
        )
    expected_enqueue_artifacts = {
        "order_request_sha256": row["order_request_sha256"],
        "transport_authority_sha256": row["transport_authority_sha256"],
        "admission_record_sha256": row["admission_record_sha256"],
        "committed_admission_sha256": row["committed_admission_sha256"],
        "transport_instruction_sha256": row["transport_instruction_sha256"],
    }
    if any(
        events[0].event_payload.get(name) != value
        for name, value in expected_enqueue_artifacts.items()
    ):
        raise CapturedPaperOutboxCorruptionError(
            "outbox_durable_transport_enqueue_event_mismatch"
        )
    max_attempts = int(row["max_attempts"])
    attempt_count = int(row["attempt_count"])
    max_reconciliation_attempts = int(row["max_reconciliation_attempts"])
    reconciliation_attempt_count = int(row["reconciliation_attempt_count"])
    reconciliation_total_attempt_count = int(
        row["reconciliation_total_attempt_count"]
    )
    reconciliation_escalation_count = int(row["reconciliation_escalation_count"])
    reconciliation_health_state = str(row["reconciliation_health_state"])
    reconciliation_next_attempt_at = _optional_utc(
        row["reconciliation_next_attempt_at"],
        field_name="outbox_reconciliation_next_attempt_at",
    )
    last_health_escalated_at = _optional_utc(
        row["last_reconciliation_health_escalated_at"],
        field_name="outbox_last_reconciliation_health_escalated_at",
    )
    if not (0 <= attempt_count <= max_attempts <= _MAX_ATTEMPT_BOUND):
        raise CapturedPaperOutboxCorruptionError(
            "outbox_attempt_count_invalid"
        )
    if not (
        0
        <= reconciliation_attempt_count
        <= max_reconciliation_attempts
        <= _MAX_ATTEMPT_BOUND
    ):
        raise CapturedPaperOutboxCorruptionError(
            "outbox_reconciliation_attempt_count_invalid"
        )
    if not (
        reconciliation_total_attempt_count >= reconciliation_attempt_count
        and reconciliation_total_attempt_count >= 0
        and reconciliation_escalation_count >= 0
        and reconciliation_health_state in {"normal", "escalated"}
        and (
            (
                reconciliation_health_state == "normal"
                and reconciliation_escalation_count == 0
                and last_health_escalated_at is None
            )
            or (
                reconciliation_health_state == "escalated"
                and reconciliation_escalation_count > 0
                and last_health_escalated_at is not None
            )
        )
        and (
            reconciliation_next_attempt_at is None
            or status == OUTBOX_STATUS_TRANSPORT_INDETERMINATE
        )
    ):
        raise CapturedPaperOutboxCorruptionError(
            "outbox_reconciliation_health_state_invalid"
        )
    for field_name in (
        "transport_evidence_sha256",
        "indeterminate_evidence_sha256",
        "last_failure_sha256",
        "last_reconciliation_evidence_sha256",
        "completion_proof_sha256",
        "fill_handoff_proof_sha256",
        "fill_handoff_receipt_sha256",
        "last_event_sha256",
    ):
        value = row[field_name]
        if value is not None and _SHA256_RE.fullmatch(str(value)) is None:
            raise CapturedPaperOutboxCorruptionError(
                f"outbox_{field_name}_invalid"
            )
    if int(row["version"]) <= 0:
        raise CapturedPaperOutboxCorruptionError("outbox_version_invalid")
    return CapturedPaperOutboxRecord(
        request=request,
        durable_transport=durable_transport,
        payload_sha256=str(row["payload_sha256"]),
        status=status,
        binder_id=str(row["binder_id"]),
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        reconciliation_attempt_count=reconciliation_attempt_count,
        max_reconciliation_attempts=max_reconciliation_attempts,
        reconciliation_next_attempt_at=reconciliation_next_attempt_at,
        reconciliation_total_attempt_count=reconciliation_total_attempt_count,
        reconciliation_health_state=reconciliation_health_state,
        reconciliation_escalation_count=reconciliation_escalation_count,
        last_reconciliation_health_escalated_at=last_health_escalated_at,
        lease_token=lease_token,
        lease_owner_id=lease_owner_id,
        lease_expires_at=lease_expires_at,
        next_attempt_at=next_attempt_at,
        transport_started_at=transport_started_at,
        transport_evidence_sha256=(
            str(transport_evidence) if transport_evidence is not None else None
        ),
        transport_indeterminate_at=indeterminate_at,
        indeterminate_evidence_sha256=(
            str(indeterminate_evidence)
            if indeterminate_evidence is not None
            else None
        ),
        last_failure_sha256=(
            str(row["last_failure_sha256"])
            if row["last_failure_sha256"] is not None
            else None
        ),
        last_reconciliation_evidence_sha256=(
            str(row["last_reconciliation_evidence_sha256"])
            if row["last_reconciliation_evidence_sha256"] is not None
            else None
        ),
        completion_proof_sha256=(
            str(completion_proof) if completion_proof is not None else None
        ),
        completed_at=completed_at,
        fill_handoff_proof=fill_handoff_proof,
        fill_handoff_proof_sha256=(
            str(handoff_sha256) if handoff_sha256 is not None else None
        ),
        fill_handoff_receipt=fill_handoff_receipt,
        fill_handoff_receipt_sha256=(
            str(receipt_sha256) if receipt_sha256 is not None else None
        ),
        fill_handoff_committed_at=handoff_committed_at,
        event_sequence=event_sequence,
        last_event_sha256=(
            str(last_event_sha256) if last_event_sha256 is not None else None
        ),
        version=int(row["version"]),
        events=events,
    )


def _verify_durable_transport_binding(
    db: Any,
    *,
    request: CapturedPaperPostCommitRequest,
    authority: CapturedPaperTransportAuthority,
    acceptance: CapturedPaperBrokerAcceptanceProof | None = None,
    pre_transport_start: bool = False,
    pre_transport_invocation: bool = False,
    positive_adoption_pending: bool = False,
) -> (
    _PositiveAdoptionBindingSnapshot
    | _TransportInvocationBindingSnapshot
    | None
):
    """Lock and verify phase-two authority before the outbox row is locked.

    This is intentionally read-only with respect to the canonical lifecycle
    rows, but it takes their normal writer locks.  That gives transport and
    completion exactly one ordering with account settlement, fill capture, and
    orphan reconciliation.  Callers must lock the outbox row only *after* this
    function returns.
    """

    if type(authority) is not CapturedPaperTransportAuthority:
        raise CapturedPaperOutboxError("transport_authority_type_invalid")
    authority.verify_for_request(request)
    if acceptance is not None and type(acceptance) is not CapturedPaperBrokerAcceptanceProof:
        raise CapturedPaperOutboxError("broker_acceptance_type_invalid")
    selected_modes = sum(
        (
            int(pre_transport_start),
            int(pre_transport_invocation),
            int(positive_adoption_pending),
            int(acceptance is not None),
        )
    )
    if selected_modes > 1:
        raise CapturedPaperOutboxError(
            "transport_authority_verification_mode_conflict"
        )

    intent = request.intent
    route = intent.route_token
    acquire_adaptive_risk_account_locks(db, account_scope=route.account_scope)
    row_locks = CanonicalAccountRiskRowLockGuard()

    row_locks.observe(
        AccountRiskRowLockStage.ACCOUNT_SETTLEMENT_HEAD,
        sort_key=(route.account_scope,),
    )
    head = db.execute(
        text(
            """
            SELECT account_scope, account_identity_sha256,
                   settlement_schema_version, execution_family,
                   broker_environment, head_content_sha256
              FROM alpaca_paper_account_settlement_heads
             WHERE account_scope = :account_scope
               AND account_identity_sha256 = :account_identity_sha256
             FOR UPDATE
            """
        ),
        {
            "account_scope": route.account_scope,
            "account_identity_sha256": authority.account_identity_sha256,
        },
    ).mappings().one_or_none()
    if not (
        head is not None
        and head["account_scope"] == route.account_scope
        and head["account_identity_sha256"] == authority.account_identity_sha256
        and head["settlement_schema_version"]
        == "chili.alpaca-paper-cycle-settlement.v1"
        and head["execution_family"] == "alpaca_spot"
        and head["broker_environment"] == "paper"
        and _SHA256_RE.fullmatch(str(head["head_content_sha256"] or ""))
    ):
        raise CapturedPaperOutboxError("transport_authority_account_head_mismatch")

    row_locks.observe(
        AccountRiskRowLockStage.ADAPTIVE_RESERVATION,
        sort_key=(authority.reservation_id,),
    )
    reservation = db.execute(
        text(
            """
            SELECT r.reservation_id, r.state, r.decision_packet_sha256,
                   r.account_scope, upper(r.symbol) AS symbol,
                   r.trading_date, r.setup_family, r.opportunity_claim_id,
                   r.planned_quantity_shares,
                   r.cumulative_filled_quantity_shares,
                   r.open_quantity_shares,
                   r.broker_order_id, r.broker_source,
                   r.broker_connection_generation,
                   r.last_broker_observed_at, r.last_broker_available_at,
                   r.last_source_event_content_sha256,
                   p.reservation_request_sha256, p.decision_id,
                   p.client_order_id, p.account_scope AS packet_scope,
                   upper(p.symbol) AS packet_symbol,
                   p.setup_family AS packet_setup_family,
                   p.trading_date AS packet_trading_date,
                   p.execution_surface, p.execution_family,
                   p.broker_environment, p.account_identity_sha256,
                   p.policy_sha256, p.effective_config_sha256,
                   p.code_build_sha256, p.feature_flags_sha256,
                   p.structural_stop, p.entry_limit_price,
                   p.evidence_sha256, p.resolver_valid,
                   p.admission_accepted, p.resolved_quantity_shares
              FROM adaptive_risk_reservations r
              JOIN adaptive_risk_decision_packets p
                ON p.decision_packet_sha256 = r.decision_packet_sha256
             WHERE r.reservation_id = CAST(:reservation_id AS UUID)
             FOR UPDATE OF r
            """
        ),
        {"reservation_id": authority.reservation_id},
    ).mappings().one_or_none()
    if reservation is None:
        raise CapturedPaperOutboxError("transport_authority_reservation_missing")
    if not (
        str(reservation["reservation_id"]) == authority.reservation_id
        and reservation["decision_packet_sha256"] == authority.decision_packet_sha256
        and reservation["reservation_request_sha256"]
        == authority.reservation_request_sha256
        and reservation["account_identity_sha256"]
        == authority.account_identity_sha256
        and reservation["evidence_sha256"] == authority.admission_evidence_sha256
        and reservation["account_scope"] == route.account_scope
        and reservation["packet_scope"] == route.account_scope
        and reservation["symbol"] == route.symbol
        and reservation["packet_symbol"] == route.symbol
        and reservation["setup_family"] == intent.setup_family
        and reservation["packet_setup_family"] == intent.setup_family
        and reservation["trading_date"] == reservation["packet_trading_date"]
        and reservation["decision_id"] == intent.decision_id
        and reservation["client_order_id"] == intent.client_order_id
        and reservation["execution_surface"] == "alpaca_paper"
        and reservation["execution_family"] == "alpaca_spot"
        and reservation["broker_environment"] == "paper"
        and reservation["policy_sha256"] == intent.policy_sha256
        and reservation["effective_config_sha256"] == route.config_sha256
        and reservation["code_build_sha256"] == route.code_build_sha256
        and reservation["feature_flags_sha256"] == intent.feature_flags_sha256
        and _decimal(
            reservation["structural_stop"], field_name="packet_structural_stop"
        )
        == _decimal(
            intent.structural_stop_price,
            field_name="intent_structural_stop_price",
        )
        and _decimal(
            reservation["entry_limit_price"],
            field_name="packet_entry_limit_price",
        )
        <= _decimal(
            intent.entry_limit_ceiling_price,
            field_name="intent_entry_limit_ceiling_price",
        )
        and reservation["resolver_valid"] is True
        and reservation["admission_accepted"] is True
        and int(reservation["resolved_quantity_shares"]) > 0
        and int(reservation["planned_quantity_shares"])
        == int(reservation["resolved_quantity_shares"])
    ):
        raise CapturedPaperOutboxError("transport_authority_reservation_mismatch")

    row_locks.observe(
        AccountRiskRowLockStage.FILL_ACTIVITY_OR_CYCLE_SETTLEMENT,
        sort_key=(1, 0),
    )
    fill_rows = db.execute(
        text(
            """
            SELECT sequence, capture_schema_version,
                   capture_authority_status, decision_packet_sha256,
                   reservation_request_sha256, account_scope,
                   account_identity_sha256, cycle_client_order_id,
                   entry_provider_order_id, upper(symbol) AS symbol,
                   order_role, provider_order_id,
                   provider_client_order_id_status, provider_client_order_id
              FROM alpaca_paper_fill_activities
             WHERE reservation_id = CAST(:reservation_id AS UUID)
             ORDER BY sequence
             FOR UPDATE
            """
        ),
        {"reservation_id": authority.reservation_id},
    ).mappings().all()
    for fill in fill_rows:
        if not (
            fill["capture_schema_version"]
            == "chili.alpaca-paper-fill-activity.v2"
            and fill["capture_authority_status"] == "verified"
            and fill["decision_packet_sha256"] == authority.decision_packet_sha256
            and fill["reservation_request_sha256"]
            == authority.reservation_request_sha256
            and fill["account_scope"] == route.account_scope
            and fill["account_identity_sha256"]
            == authority.account_identity_sha256
            and fill["cycle_client_order_id"] == intent.client_order_id
            and fill["symbol"] == route.symbol
            and (
                fill["order_role"] != "entry"
                or (
                    fill["provider_order_id"] == fill["entry_provider_order_id"]
                    and fill["provider_client_order_id_status"] == "authoritative"
                    and fill["provider_client_order_id"] == intent.client_order_id
                )
            )
        ):
            raise CapturedPaperOutboxError("transport_authority_fill_binding_mismatch")

    row_locks.observe(
        AccountRiskRowLockStage.ACTION_CLAIM,
        sort_key=(route.symbol,),
    )
    action = db.execute(
        text(
            """
            SELECT account_scope, upper(symbol) AS symbol, claim_token,
                   action, phase, owner_session_id, client_order_id,
                   broker_order_id, metadata_json, lease_expires_at
              FROM broker_symbol_action_claims
             WHERE account_scope = :account_scope AND symbol = :symbol
             FOR UPDATE
            """
        ),
        {"account_scope": route.account_scope, "symbol": route.symbol},
    ).mappings().one_or_none()
    if action is None:
        raise CapturedPaperOutboxError("transport_authority_action_claim_missing")
    metadata = action["metadata_json"] if type(action["metadata_json"]) is dict else {}
    order_request = metadata.get("order_request")
    order_request = order_request if type(order_request) is dict else None
    packet_payload = metadata.get("adaptive_risk_decision_packet")
    packet_payload = packet_payload if type(packet_payload) is dict else None
    claim_payload = metadata.get("adaptive_risk_reservation_claim")
    claim_payload = claim_payload if type(claim_payload) is dict else None
    request_payload = metadata.get("adaptive_risk_reservation_request")
    request_payload = request_payload if type(request_payload) is dict else None
    transport_marker = metadata.get("entry_transport_started")
    transport_marker = transport_marker if type(transport_marker) is dict else None
    owner_transport_present = "owner_transport" in metadata
    try:
        order_quantity = int(order_request.get("qty")) if order_request else 0
    except (TypeError, ValueError) as exc:
        raise CapturedPaperOutboxError(
            "transport_authority_order_quantity_invalid"
        ) from exc
    if not (
        action["account_scope"] == route.account_scope
        and action["symbol"] == route.symbol
        and action["claim_token"] == authority.action_claim_token
        and action["action"] == "entry"
        and int(action["owner_session_id"]) == route.session_id
        and action["client_order_id"] == intent.client_order_id
        and metadata.get("alpaca_account_id") == route.expected_account_id
        and metadata.get("entry_post_bind_token") == authority.binder_id
        and order_request is not None
        and order_request.get("client_order_id") == intent.client_order_id
        and order_request.get("symbol") == route.symbol
        and order_request.get("side") == "buy"
        and order_request.get("position_intent") == "buy_to_open"
        and order_quantity == int(reservation["resolved_quantity_shares"])
        and _decimal(
            order_request.get("limit_price"),
            field_name="broker_request_limit_price",
        )
        == _decimal(
            reservation["entry_limit_price"],
            field_name="packet_entry_limit_price",
        )
        and _sha256_text(_canonical_json(order_request))
        == authority.broker_request_sha256
        and (
            (
                pre_transport_start
                and transport_marker is None
                and not owner_transport_present
            )
            or (
                not pre_transport_start
                and transport_marker is not None
                and transport_marker.get("client_order_id")
                == intent.client_order_id
                and transport_marker.get("post_bind_token")
                == authority.binder_id
            )
        )
        and packet_payload is not None
        and packet_payload.get("decision_packet_sha256")
        == authority.decision_packet_sha256
        and claim_payload is not None
        and claim_payload.get("decision_packet_sha256")
        == authority.decision_packet_sha256
        and claim_payload.get("claim_id") == intent.client_order_id
        and request_payload is not None
        and request_payload.get("request_sha256")
        == authority.reservation_request_sha256
    ):
        raise CapturedPaperOutboxError("transport_authority_action_claim_mismatch")

    row_locks.observe(
        AccountRiskRowLockStage.AUTOMATION_SESSION,
        sort_key=(route.session_id,),
    )
    automation = db.execute(
        text(
            """
            SELECT id, mode, upper(symbol) AS symbol, execution_family,
                   state, risk_snapshot_json, ended_at
              FROM trading_automation_sessions
             WHERE id = :session_id
             FOR UPDATE
            """
        ),
        {"session_id": route.session_id},
    ).mappings().one_or_none()
    snapshot = (
        automation["risk_snapshot_json"]
        if automation is not None and type(automation["risk_snapshot_json"]) is dict
        else {}
    )
    marker = snapshot.get("confirmed_arm_generation")
    marker = marker if type(marker) is dict else None
    arm = intent.confirmed_arm_generation
    if not (
        automation is not None
        and int(automation["id"]) == route.session_id
        and automation["mode"] == "live"
        and automation["symbol"] == route.symbol
        and automation["execution_family"] == "alpaca_spot"
        and (
            acceptance is not None
            or positive_adoption_pending
            or automation["ended_at"] is None
        )
        and snapshot.get("alpaca_account_scope") == route.account_scope
        and snapshot.get("alpaca_account_id") == route.expected_account_id
        and snapshot.get("alpaca_symbol_claim_token") == authority.action_claim_token
        and marker is not None
        and marker.get("version") == 1
        and marker.get("session_id") == route.session_id
        and marker.get("arm_token") == arm.arm_token
        and marker.get("expires_at_utc") == arm.expires_at.isoformat()
        and marker.get("alpaca_symbol_claim_token") == arm.symbol_claim_token
        and marker.get("alpaca_account_scope") == route.account_scope
        and marker.get("alpaca_account_id") == route.expected_account_id
        and marker.get("confirmed_at_utc") == arm.confirmed_at.isoformat()
    ):
        raise CapturedPaperOutboxError("transport_authority_automation_mismatch")

    if intent.opportunity_key is None:
        if reservation["opportunity_claim_id"] is not None:
            raise CapturedPaperOutboxError("transport_authority_opportunity_unexpected")
    else:
        key = intent.opportunity_key
        opportunity_id = reservation["opportunity_claim_id"]
        if opportunity_id is None:
            raise CapturedPaperOutboxError("transport_authority_opportunity_missing")
        row_locks.observe(
            AccountRiskRowLockStage.OPPORTUNITY_CLAIM,
            sort_key=(
                key.account_scope,
                key.symbol,
                key.trading_date,
                key.setup_family,
                int(opportunity_id),
            ),
        )
        opportunity = db.execute(
            text(
                """
                SELECT id, account_scope, upper(symbol) AS symbol,
                       trading_date, setup_family, status, reservation_id,
                       consumed_by_reservation_id
                  FROM adaptive_risk_opportunity_claims
                 WHERE id = :opportunity_claim_id
                 FOR UPDATE
                """
            ),
            {"opportunity_claim_id": opportunity_id},
        ).mappings().one_or_none()
        opportunity_owner = None
        if opportunity is not None:
            opportunity_owner = (
                opportunity["reservation_id"]
                if opportunity["status"] == "reserved"
                else opportunity["consumed_by_reservation_id"]
            )
        if not (
            opportunity is not None
            and opportunity["account_scope"] == key.account_scope
            and opportunity["symbol"] == key.symbol
            and opportunity["trading_date"] == key.trading_date
            and opportunity["setup_family"] == key.setup_family
            and opportunity["status"]
            in ({"reserved"} if acceptance is None else {"reserved", "consumed"})
            and str(opportunity_owner) == authority.reservation_id
        ):
            raise CapturedPaperOutboxError("transport_authority_opportunity_mismatch")

    if pre_transport_start:
        if not (
            reservation["state"] == "reserved"
            and reservation["broker_order_id"] is None
            and reservation["broker_source"] is None
            and action["phase"] == "claimed"
            and action["broker_order_id"] is None
            and action["lease_expires_at"] is not None
            and _aware_utc(
                action["lease_expires_at"],
                field_name="transport_action_claim_lease_expires_at",
            )
            > _db_now(db)
            and transport_marker is None
            and not owner_transport_present
            and not fill_rows
            and _db_now(db) <= arm.expires_at
        ):
            raise CapturedPaperOutboxError(
                "transport_authority_pre_start_state_mismatch"
            )
        return

    if pre_transport_invocation:
        if transport_marker is None:
            raise CapturedPaperOutboxError(
                "transport_invocation_marker_missing"
            )
        live_execution = snapshot.get("momentum_live_execution")
        live_execution = (
            live_execution if type(live_execution) is dict else {}
        )
        operator_pause = snapshot.get("operator_pause")
        operator_pause = operator_pause if type(operator_pause) is dict else {}
        marker_started_at = _iso_utc(
            transport_marker.get("started_at_utc"),
            field_name="transport_invocation_started_at",
        )
        marker_lease_token = _canonical_uuid(
            transport_marker.get("outbox_lease_token"),
            field_name="transport_invocation_marker_lease_token",
        )
        marker_lease_owner_id = _canonical_uuid(
            transport_marker.get("outbox_lease_owner_id"),
            field_name="transport_invocation_marker_lease_owner_id",
        )
        action_lease_expires_at = _aware_utc(
            action["lease_expires_at"],
            field_name="transport_invocation_action_lease_expires_at",
        )
        if not (
            reservation["state"] == "reserved"
            and reservation["broker_order_id"] is None
            and reservation["broker_source"] is None
            and action["phase"] == "submit_indeterminate"
            and action["broker_order_id"] is None
            and not owner_transport_present
            and not fill_rows
            and automation["state"] == "live_pending_entry"
            and automation["ended_at"] is None
            and operator_pause.get("active") is not True
            and live_execution.get("entry_submitted") is not True
            and not live_execution.get("entry_order_id")
            and not live_execution.get("entry_order_ids_all")
            and not live_execution.get("entry_reconcile_pending_client_order_id")
            and not isinstance(live_execution.get("position"), dict)
            and transport_marker.get("client_order_id")
            == intent.client_order_id
            and transport_marker.get("post_bind_token")
            == authority.binder_id
            and transport_marker.get("completion_sha256")
            == request.completion_sha256
            and transport_marker.get("transport_authority_sha256")
            == authority.authority_sha256
            and arm.confirmed_at <= marker_started_at <= arm.expires_at
        ):
            raise CapturedPaperOutboxError(
                "transport_authority_pre_invocation_state_mismatch"
            )
        return _TransportInvocationBindingSnapshot(
            transport_started_at=marker_started_at,
            marker_lease_token=marker_lease_token,
            marker_lease_owner_id=marker_lease_owner_id,
            action_lease_expires_at=action_lease_expires_at,
            arm_expires_at=arm.expires_at,
        )

    if positive_adoption_pending:
        if fill_rows:
            raise CapturedPaperOutboxError(
                "transport_positive_adoption_fill_reconciliation_required"
            )
        if transport_marker is None:
            raise CapturedPaperOutboxError(
                "transport_positive_adoption_marker_missing"
            )
        marker_started_at = _iso_utc(
            transport_marker.get("started_at_utc"),
            field_name="transport_positive_adoption_started_at",
        )
        try:
            _canonical_uuid(
                transport_marker.get("outbox_lease_token"),
                field_name="transport_positive_adoption_original_lease_token",
            )
            _canonical_uuid(
                transport_marker.get("outbox_lease_owner_id"),
                field_name="transport_positive_adoption_original_lease_owner_id",
            )
        except CapturedPaperOutboxError:
            raise
        if not (
            transport_marker.get("client_order_id") == intent.client_order_id
            and transport_marker.get("post_bind_token") == authority.binder_id
            and transport_marker.get("completion_sha256")
            == request.completion_sha256
            and transport_marker.get("transport_authority_sha256")
            == authority.authority_sha256
            and arm.confirmed_at <= marker_started_at <= arm.expires_at
        ):
            raise CapturedPaperOutboxError(
                "transport_positive_adoption_marker_mismatch"
            )

        no_economic_fill = bool(
            int(reservation["cumulative_filled_quantity_shares"]) == 0
            and int(reservation["open_quantity_shares"]) == 0
        )
        pending_unbound = bool(
            no_economic_fill
            and reservation["state"] == "reserved"
            and reservation["broker_order_id"] is None
            and reservation["broker_source"] is None
            and reservation["broker_connection_generation"] is None
            and reservation["last_broker_observed_at"] is None
            and reservation["last_broker_available_at"] is None
            and reservation["last_source_event_content_sha256"] is None
            and action["phase"] == "submit_indeterminate"
            and action["broker_order_id"] is None
        )

        live = snapshot.get("momentum_live_execution")
        live = live if type(live) is dict else {}
        durable_order_id = reservation["broker_order_id"]
        already_bound = bool(
            no_economic_fill
            and reservation["state"] == "submitted"
            and reservation["broker_source"] == "alpaca"
            and _BROKER_ID_RE.fullmatch(str(durable_order_id or "")) is not None
            and bool(reservation["broker_connection_generation"])
            and _SHA256_RE.fullmatch(
                str(reservation["last_source_event_content_sha256"] or "")
            )
            is not None
            and reservation["last_broker_observed_at"] is not None
            and reservation["last_broker_available_at"] is not None
            and action["phase"] == "submitted"
            and action["broker_order_id"] == durable_order_id
            and automation["state"] == "live_pending_entry"
            and live.get("entry_submitted") is True
            and live.get("entry_order_id") == durable_order_id
            and live.get("entry_client_order_id") == intent.client_order_id
            and live.get("entry_order_ids_all") == [durable_order_id]
            and live.get("adaptive_risk_reservation_id")
            == authority.reservation_id
            and live.get("adaptive_risk_decision_packet_sha256")
            == authority.decision_packet_sha256
            and live.get("adaptive_risk_reservation_request_sha256")
            == authority.reservation_request_sha256
            and live.get("alpaca_account_scope") == route.account_scope
            and live.get("alpaca_account_id") == route.expected_account_id
            and live.get("entry_post_bind_token") == authority.binder_id
            and live.get("entry_symbol_claim_token")
            == authority.action_claim_token
        )
        if not (pending_unbound or already_bound):
            raise CapturedPaperOutboxError(
                "transport_positive_adoption_state_mismatch"
            )
        return _PositiveAdoptionBindingSnapshot(
            binding_state=(
                "pending_unbound" if pending_unbound else "already_bound"
            ),
            transport_started_at=marker_started_at,
            session_state=str(automation["state"]),
            session_ended=automation["ended_at"] is not None,
            broker_order_id=(
                str(durable_order_id) if durable_order_id is not None else None
            ),
            broker_connection_generation=(
                str(reservation["broker_connection_generation"])
                if reservation["broker_connection_generation"] is not None
                else None
            ),
            broker_order_evidence_sha256=(
                str(reservation["last_source_event_content_sha256"])
                if reservation["last_source_event_content_sha256"] is not None
                else None
            ),
            broker_observed_at=_optional_utc(
                reservation["last_broker_observed_at"],
                field_name="positive_adoption_existing_observed_at",
            ),
            broker_available_at=_optional_utc(
                reservation["last_broker_available_at"],
                field_name="positive_adoption_existing_available_at",
            ),
        )

    if acceptance is None:
        if not (
            reservation["state"] == "reserved"
            and reservation["broker_order_id"] is None
            and reservation["broker_source"] is None
            and action["phase"] == "submit_indeterminate"
            and action["broker_order_id"] is None
            and not fill_rows
        ):
            raise CapturedPaperOutboxError("transport_authority_pre_post_state_mismatch")
        return

    if not (
        action["phase"] in {"submitted", "resolved"}
        and action["broker_order_id"] == acceptance.broker_order_id
        and reservation["state"] in {
            "submitted",
            "partially_filled",
            "filled",
            "flat_pending_settlement",
            "exposure_quarantined",
            "closed",
        }
        and reservation["broker_order_id"] == acceptance.broker_order_id
        and reservation["broker_source"] == "alpaca"
        and bool(reservation["broker_connection_generation"])
        and reservation["last_source_event_content_sha256"]
        == acceptance.broker_order_evidence_sha256
        and _aware_utc(
            reservation["last_broker_observed_at"],
            field_name="reservation_last_broker_observed_at",
        )
        == acceptance.observed_at
        and _aware_utc(
            reservation["last_broker_available_at"],
            field_name="reservation_last_broker_available_at",
        )
        == acceptance.available_at
        and all(
            fill["entry_provider_order_id"] == acceptance.broker_order_id
            for fill in fill_rows
        )
    ):
        raise CapturedPaperOutboxError("broker_acceptance_durable_binding_mismatch")
    return _PositiveAdoptionBindingSnapshot(
        binding_state="already_bound",
        transport_started_at=_iso_utc(
            transport_marker.get("started_at_utc"),
            field_name="transport_acceptance_started_at",
        ),
        session_state=str(automation["state"]),
        session_ended=automation["ended_at"] is not None,
        broker_order_id=str(reservation["broker_order_id"]),
        broker_connection_generation=str(
            reservation["broker_connection_generation"]
        ),
        broker_order_evidence_sha256=str(
            reservation["last_source_event_content_sha256"]
        ),
        broker_observed_at=_aware_utc(
            reservation["last_broker_observed_at"],
            field_name="transport_acceptance_observed_at",
        ),
        broker_available_at=_aware_utc(
            reservation["last_broker_available_at"],
            field_name="transport_acceptance_available_at",
        ),
    )


def lock_captured_paper_positive_adoption(
    db: Any,
    *,
    completion_sha256: str,
    authority: CapturedPaperTransportAuthority,
    acceptance_kind: str,
) -> CapturedPaperPositiveAdoptionLockReceipt:
    """Lock the full lifecycle and prove a positive order may be adopted.

    This function never commits.  The caller must keep using the same database
    transaction and must compare an ``already_bound`` receipt to the exact
    broker observation before treating it as idempotent success.
    """

    digest = _digest(completion_sha256, field_name="completion_sha256")
    if acceptance_kind not in {"post_response", "same_cid_reconciliation"}:
        raise CapturedPaperOutboxError(
            "transport_positive_adoption_kind_invalid"
        )
    request = _load_request_unlocked(db, completion_sha256=digest)
    binding = _verify_durable_transport_binding(
        db,
        request=request,
        authority=authority,
        positive_adoption_pending=True,
    )
    if binding is None:
        raise CapturedPaperOutboxError(
            "transport_positive_adoption_binding_unavailable"
        )
    record = load_captured_paper_outbox(
        db, completion_sha256=digest, for_update=True
    )
    expected_status = (
        OUTBOX_STATUS_TRANSPORT_STARTED
        if acceptance_kind == "post_response"
        else OUTBOX_STATUS_RECONCILING
    )
    if not (
        record.status == expected_status
        and record.request.to_canonical_json() == request.to_canonical_json()
        and record.transport_evidence_sha256 == authority.authority_sha256
        and record.transport_started_at == binding.transport_started_at
    ):
        raise CapturedPaperOutboxError(
            "transport_positive_adoption_outbox_mismatch"
        )
    return CapturedPaperPositiveAdoptionLockReceipt(
        request=request,
        acceptance_kind=acceptance_kind,
        outbox_status=record.status,
        binding_state=binding.binding_state,
        authority_sha256=authority.authority_sha256,
        transport_started_at=binding.transport_started_at,
        session_state=binding.session_state,
        session_ended=binding.session_ended,
        broker_order_id=binding.broker_order_id,
        broker_connection_generation=binding.broker_connection_generation,
        broker_order_evidence_sha256=binding.broker_order_evidence_sha256,
        broker_observed_at=binding.broker_observed_at,
        broker_available_at=binding.broker_available_at,
    )


def _select_row(
    db: Any,
    *,
    completion_sha256: str,
    for_update: bool,
    skip_locked: bool = False,
) -> Mapping[str, Any] | None:
    suffix = ""
    if for_update:
        suffix = " FOR UPDATE" + (" SKIP LOCKED" if skip_locked else "")
    return db.execute(
        text(
            f"SELECT {_ROW_COLUMNS} "
            "FROM captured_paper_post_commit_outbox "
            "WHERE completion_sha256 = :completion_sha256"
            + suffix
        ),
        {"completion_sha256": completion_sha256},
    ).mappings().one_or_none()


def _load_request_unlocked(
    db: Any,
    *,
    completion_sha256: str,
) -> CapturedPaperPostCommitRequest:
    """Read only immutable request bytes before acquiring canonical row locks."""

    digest = _digest(completion_sha256, field_name="completion_sha256")
    row = _select_row(
        db,
        completion_sha256=digest,
        for_update=False,
    )
    if row is None:
        raise CapturedPaperOutboxNotFoundError(
            "captured_paper_outbox_not_found"
        )
    return _request_from_row(row)


def load_captured_paper_outbox(
    db: Any,
    *,
    completion_sha256: str,
    for_update: bool = False,
) -> CapturedPaperOutboxRecord:
    completion_sha256 = _digest(
        completion_sha256, field_name="completion_sha256"
    )
    row = _select_row(
        db,
        completion_sha256=completion_sha256,
        for_update=bool(for_update),
    )
    if row is None:
        raise CapturedPaperOutboxNotFoundError(
            "captured_paper_outbox_not_found"
        )
    return _record_from_row(db, row)


def _verify_durable_transport_related_metadata(
    db: Any,
    *,
    bundle: CapturedPaperDurableTransportBundle,
) -> None:
    """Rebind sealed admission bytes to their action/session projections."""

    route = bundle.request.intent.route_token
    action = db.execute(
        text(
            """
            SELECT metadata_json
              FROM broker_symbol_action_claims
             WHERE account_scope = :account_scope AND symbol = :symbol
             FOR UPDATE
            """
        ),
        {"account_scope": route.account_scope, "symbol": route.symbol},
    ).mappings().one_or_none()
    automation = db.execute(
        text(
            """
            SELECT risk_snapshot_json
              FROM trading_automation_sessions
             WHERE id = :session_id
             FOR UPDATE
            """
        ),
        {"session_id": route.session_id},
    ).mappings().one_or_none()
    action_metadata = (
        action["metadata_json"]
        if action is not None and type(action["metadata_json"]) is dict
        else {}
    )
    session_snapshot = (
        automation["risk_snapshot_json"]
        if automation is not None
        and type(automation["risk_snapshot_json"]) is dict
        else {}
    )
    expected_session_admission = {
        **bundle.admission_record,
        "admission_record_sha256": bundle.admission_record_sha256,
        "status": "admitted_pending_transport",
    }
    if not (
        action_metadata.get("order_request") == bundle.order_request
        and action_metadata.get("captured_paper_completion_sha256")
        == bundle.request.completion_sha256
        and action_metadata.get("captured_paper_admission_record_sha256")
        == bundle.admission_record_sha256
        and action_metadata.get("adaptive_input_evidence_sha256")
        == bundle.authority.admission_evidence_sha256
        and session_snapshot.get("captured_paper_admission")
        == expected_session_admission
    ):
        raise CapturedPaperOutboxCorruptionError(
            "outbox_durable_transport_related_metadata_mismatch"
        )


def load_captured_paper_durable_transport_bundle(
    db: Any,
    *,
    completion_sha256: str,
) -> CapturedPaperDurableTransportBundle:
    """Load sealed restart material and rebind it to canonical lifecycle rows.

    The outbox row is intentionally read without a lock first.  Canonical
    account/risk/action/session/opportunity locks are then acquired in their
    normal order before the outbox row is locked and re-inventoried.  A racing
    transition therefore produces either one coherent before/after state or a
    fail-closed binding error; it can never mix current config with old bytes.
    """

    digest = _digest(completion_sha256, field_name="completion_sha256")
    initial = _select_row(db, completion_sha256=digest, for_update=False)
    if initial is None:
        raise CapturedPaperOutboxNotFoundError(
            "captured_paper_outbox_not_found"
        )
    bundle = _durable_bundle_from_row(initial)
    status = str(initial["status"])
    if status == OUTBOX_STATUS_COMPLETED:
        raise CapturedPaperOutboxError(
            "durable_transport_completed_work_not_loadable"
        )
    if status == OUTBOX_STATUS_FILL_HANDOFF_COMMITTED:
        raise CapturedPaperOutboxError(
            "durable_transport_fill_handoff_work_not_loadable"
        )
    pre_transport = status in {
        OUTBOX_STATUS_PENDING,
        OUTBOX_STATUS_LEASED,
        OUTBOX_STATUS_RETRY_WAIT,
        OUTBOX_STATUS_RETRY_EXHAUSTED,
    }
    _verify_durable_transport_binding(
        db,
        request=bundle.request,
        authority=bundle.authority,
        pre_transport_start=pre_transport,
    )
    _verify_durable_transport_related_metadata(db, bundle=bundle)
    record = load_captured_paper_outbox(
        db,
        completion_sha256=digest,
        for_update=True,
    )
    if (
        record.durable_transport.committed_admission_sha256
        != bundle.committed_admission_sha256
        or record.durable_transport.transport_instruction_sha256
        != bundle.transport_instruction_sha256
    ):
        raise CapturedPaperOutboxCorruptionError(
            "outbox_durable_transport_raced"
        )
    return record.durable_transport


@_atomic_mutation
def authorize_captured_paper_transport_invocation(
    db: Any,
    *,
    completion_sha256: str,
    lease_token: str,
    lease_owner_id: str,
    authority: CapturedPaperTransportAuthority,
) -> CapturedPaperTransportInvocationAuthority:
    """Re-inventory live authority after the start fence and before POST.

    This is a one-shot durable gate.  It repeats the complete sealed admission
    and lifecycle lock walk after ``transport_started`` is committed, then
    appends a content-addressed authorization event.  It performs no broker
    I/O and never converts an invalid authority into retry permission.
    """

    digest = _digest(completion_sha256, field_name="completion_sha256")
    token = _canonical_uuid(
        lease_token, field_name="transport_invocation_lease_token"
    )
    owner = _canonical_uuid(
        lease_owner_id, field_name="transport_invocation_lease_owner_id"
    )
    initial = _select_row(db, completion_sha256=digest, for_update=False)
    if initial is None:
        raise CapturedPaperOutboxNotFoundError(
            "captured_paper_outbox_not_found"
        )
    if str(initial["status"]) != OUTBOX_STATUS_TRANSPORT_STARTED:
        raise CapturedPaperOutboxLeaseError(
            "transport_invocation_phase_invalid"
        )
    bundle = _durable_bundle_from_row(initial)
    snapshot = _verify_durable_transport_binding(
        db,
        request=bundle.request,
        authority=authority,
        pre_transport_invocation=True,
    )
    if type(snapshot) is not _TransportInvocationBindingSnapshot:
        raise CapturedPaperOutboxError(
            "transport_invocation_binding_snapshot_missing"
        )
    _verify_durable_transport_related_metadata(db, bundle=bundle)

    row = _select_row(db, completion_sha256=digest, for_update=True)
    if row is None:
        raise CapturedPaperOutboxNotFoundError(
            "captured_paper_outbox_not_found"
        )
    locked_bundle = _durable_bundle_from_row(row)
    if not (
        locked_bundle.transport_instruction_sha256
        == bundle.transport_instruction_sha256
        and locked_bundle.committed_admission_sha256
        == bundle.committed_admission_sha256
        and locked_bundle.admission_record_sha256
        == bundle.admission_record_sha256
        and locked_bundle.authority.authority_sha256
        == authority.authority_sha256
    ):
        raise CapturedPaperOutboxCorruptionError(
            "transport_invocation_durable_material_raced"
        )
    row_token = (
        str(row["lease_token"]) if row["lease_token"] is not None else None
    )
    row_owner = (
        str(row["lease_owner_id"])
        if row["lease_owner_id"] is not None
        else None
    )
    started_at = _aware_utc(
        row["transport_started_at"],
        field_name="transport_invocation_transport_started_at",
    )
    outbox_lease_expires_at = _aware_utc(
        row["lease_expires_at"],
        field_name="transport_invocation_outbox_lease_expires_at",
    )
    if not (
        row["status"] == OUTBOX_STATUS_TRANSPORT_STARTED
        and row_token == token
        and row_owner == owner
        and row["transport_evidence_sha256"] == authority.authority_sha256
        and started_at == snapshot.transport_started_at
        and snapshot.marker_lease_token == token
        and snapshot.marker_lease_owner_id == owner
    ):
        raise CapturedPaperOutboxLeaseError(
            "transport_invocation_outbox_binding_mismatch"
        )
    existing_authorization = db.execute(
        text(
            """
            SELECT 1
              FROM captured_paper_post_commit_outbox_events
             WHERE completion_sha256 = :completion_sha256
               AND event_type = 'transport_invocation_authorized'
             LIMIT 1
            """
        ),
        {"completion_sha256": digest},
    ).scalar_one_or_none()
    if existing_authorization is not None:
        raise CapturedPaperOutboxConflictError(
            "transport_invocation_already_authorized"
        )

    verified_at = _db_now(db)
    valid_until = min(
        outbox_lease_expires_at,
        snapshot.action_lease_expires_at,
        snapshot.arm_expires_at,
    )
    if not (started_at <= verified_at < valid_until):
        raise CapturedPaperOutboxLeaseError(
            "transport_invocation_authority_expired"
        )
    receipt = CapturedPaperTransportInvocationAuthority(
        completion_sha256=digest,
        transport_authority_sha256=authority.authority_sha256,
        transport_instruction_sha256=bundle.transport_instruction_sha256,
        lease_token=token,
        lease_owner_id=owner,
        transport_started_at=started_at,
        verified_at=verified_at,
        valid_until=valid_until,
        outbox_version=int(row["version"]),
        authorization_event_sequence=int(row["event_sequence"]) + 1,
        previous_event_sha256=(
            str(row["last_event_sha256"])
            if row["last_event_sha256"] is not None
            else None
        ),
    )
    _append_event(
        db,
        row=row,
        event_type="transport_invocation_authorized",
        event_payload=receipt.to_payload(),
    )
    return receipt


@_atomic_mutation
def record_captured_paper_transport_financial_breaker(
    db: Any,
    *,
    completion_sha256: str,
    lease_token: str,
    lease_owner_id: str,
    authority: CapturedPaperTransportAuthority,
    invocation_authority: CapturedPaperTransportInvocationAuthority,
    receipt: CapturedPaperFinancialBreakerReceipt,
) -> CapturedPaperFinancialBreakerReceipt:
    """Append the exact post-fence breaker receipt before any order I/O."""

    digest = _digest(completion_sha256, field_name="completion_sha256")
    token = _canonical_uuid(
        lease_token, field_name="transport_financial_breaker_lease_token"
    )
    owner = _canonical_uuid(
        lease_owner_id,
        field_name="transport_financial_breaker_lease_owner_id",
    )
    if type(receipt) is not CapturedPaperFinancialBreakerReceipt:
        raise CapturedPaperOutboxError(
            "transport_financial_breaker_receipt_type_invalid"
        )
    if type(invocation_authority) is not (
        CapturedPaperTransportInvocationAuthority
    ):
        raise CapturedPaperOutboxError(
            "transport_financial_breaker_invocation_type_invalid"
        )
    row = _select_row(db, completion_sha256=digest, for_update=True)
    if row is None:
        raise CapturedPaperOutboxNotFoundError(
            "captured_paper_outbox_not_found"
        )
    bundle = _durable_bundle_from_row(row)
    invocation_authority.verify_for(
        authority,
        transport_instruction_sha256=bundle.transport_instruction_sha256,
        lease_token=token,
        lease_owner_id=owner,
    )
    row_token = str(row["lease_token"]) if row["lease_token"] is not None else None
    row_owner = (
        str(row["lease_owner_id"])
        if row["lease_owner_id"] is not None
        else None
    )
    if not (
        row["status"] == OUTBOX_STATUS_TRANSPORT_STARTED
        and row_token == token
        and row_owner == owner
        and row["transport_evidence_sha256"] == authority.authority_sha256
        and bundle.authority.authority_sha256 == authority.authority_sha256
        and bundle.request.completion_sha256 == digest
    ):
        raise CapturedPaperOutboxLeaseError(
            "transport_financial_breaker_outbox_binding_mismatch"
        )
    invocation_event = db.execute(
        text(
            """
            SELECT event_payload_canonical_json
              FROM captured_paper_post_commit_outbox_events
             WHERE completion_sha256 = :completion_sha256
               AND event_type = 'transport_invocation_authorized'
             ORDER BY sequence DESC
             LIMIT 1
            """
        ),
        {"completion_sha256": digest},
    ).scalar_one_or_none()
    if invocation_event is None:
        raise CapturedPaperOutboxError(
            "transport_financial_breaker_invocation_event_missing"
        )
    try:
        invocation_payload = json.loads(str(invocation_event))
    except (TypeError, ValueError) as exc:
        raise CapturedPaperOutboxCorruptionError(
            "transport_invocation_event_invalid"
        ) from exc
    if invocation_payload != invocation_authority.to_payload():
        raise CapturedPaperOutboxCorruptionError(
            "transport_invocation_event_mismatch"
        )
    existing = db.execute(
        text(
            """
            SELECT 1
              FROM captured_paper_post_commit_outbox_events
             WHERE completion_sha256 = :completion_sha256
               AND event_type = 'transport_financial_breaker_recorded'
             LIMIT 1
            """
        ),
        {"completion_sha256": digest},
    ).scalar_one_or_none()
    if existing is not None:
        raise CapturedPaperOutboxConflictError(
            "transport_financial_breaker_already_recorded"
        )
    verified_at = _db_now(db)
    try:
        receipt.verify_for_request(
            bundle.request,
            phase="pre_post",
            now=verified_at,
            require_allowed=False,
            transport_instruction_sha256=bundle.transport_instruction_sha256,
            transport_invocation_authority_sha256=(
                invocation_authority.invocation_authority_sha256
            ),
        )
    except CapturedPaperFinancialBreakerError as exc:
        raise CapturedPaperOutboxError(
            "transport_financial_breaker_receipt_invalid"
        ) from exc
    if verified_at >= invocation_authority.valid_until:
        raise CapturedPaperOutboxLeaseError(
            "transport_financial_breaker_invocation_expired"
        )
    _append_event(
        db,
        row=row,
        event_type="transport_financial_breaker_recorded",
        event_payload=receipt.to_payload(),
    )
    return receipt


@_atomic_mutation
def consume_captured_paper_transport_dispatch_authority(
    db: Any,
    *,
    completion_sha256: str,
    lease_token: str,
    lease_owner_id: str,
    authority: CapturedPaperTransportAuthority,
    invocation_authority: CapturedPaperTransportInvocationAuthority,
    financial_breaker_receipt: CapturedPaperFinancialBreakerReceipt,
    pre_dispatch_evidence: CapturedPaperTransportPreDispatchEvidence,
) -> CapturedPaperTransportDispatchAuthority:
    """Repeat the full lock walk after external reads, then consume one POST.

    This is the final database boundary before order I/O.  The durable start
    marker already makes the work reconciliation-only after any crash; this
    additional append-only event ensures that stale admission/reservation truth
    observed after the first fence can never inherit permission to POST.
    """

    if not (
        type(authority) is CapturedPaperTransportAuthority
        and type(invocation_authority)
        is CapturedPaperTransportInvocationAuthority
        and type(financial_breaker_receipt)
        is CapturedPaperFinancialBreakerReceipt
        and type(pre_dispatch_evidence)
        is CapturedPaperTransportPreDispatchEvidence
    ):
        raise CapturedPaperOutboxError(
            "transport_dispatch_authority_input_type_invalid"
        )

    digest = _digest(completion_sha256, field_name="completion_sha256")
    token = _canonical_uuid(
        lease_token, field_name="transport_dispatch_lease_token"
    )
    owner = _canonical_uuid(
        lease_owner_id, field_name="transport_dispatch_lease_owner_id"
    )
    initial = _select_row(db, completion_sha256=digest, for_update=False)
    if initial is None:
        raise CapturedPaperOutboxNotFoundError(
            "captured_paper_outbox_not_found"
        )
    if str(initial["status"]) != OUTBOX_STATUS_TRANSPORT_STARTED:
        raise CapturedPaperOutboxLeaseError(
            "transport_dispatch_phase_invalid"
        )
    bundle = _durable_bundle_from_row(initial)
    invocation_authority.verify_for(
        authority,
        transport_instruction_sha256=bundle.transport_instruction_sha256,
        lease_token=token,
        lease_owner_id=owner,
    )
    pre_dispatch_evidence.verify_for(
        authority,
        invocation_authority,
        transport_instruction_sha256=bundle.transport_instruction_sha256,
    )
    snapshot = _verify_durable_transport_binding(
        db,
        request=bundle.request,
        authority=authority,
        pre_transport_invocation=True,
    )
    if type(snapshot) is not _TransportInvocationBindingSnapshot:
        raise CapturedPaperOutboxError(
            "transport_dispatch_binding_snapshot_missing"
        )
    _verify_durable_transport_related_metadata(db, bundle=bundle)

    row = _select_row(db, completion_sha256=digest, for_update=True)
    if row is None:
        raise CapturedPaperOutboxNotFoundError(
            "captured_paper_outbox_not_found"
        )
    locked_bundle = _durable_bundle_from_row(row)
    row_token = str(row["lease_token"]) if row["lease_token"] is not None else None
    row_owner = (
        str(row["lease_owner_id"])
        if row["lease_owner_id"] is not None
        else None
    )
    outbox_lease_expires_at = _aware_utc(
        row["lease_expires_at"],
        field_name="transport_dispatch_outbox_lease_expires_at",
    )
    if not (
        row["status"] == OUTBOX_STATUS_TRANSPORT_STARTED
        and row_token == token
        and row_owner == owner
        and row["transport_evidence_sha256"] == authority.authority_sha256
        and locked_bundle.transport_instruction_sha256
        == bundle.transport_instruction_sha256
        and locked_bundle.committed_admission_sha256
        == bundle.committed_admission_sha256
        and locked_bundle.admission_record_sha256
        == bundle.admission_record_sha256
        and locked_bundle.authority.authority_sha256
        == authority.authority_sha256
        and snapshot.marker_lease_token == token
        and snapshot.marker_lease_owner_id == owner
    ):
        raise CapturedPaperOutboxLeaseError(
            "transport_dispatch_outbox_binding_mismatch"
        )

    event_rows = db.execute(
        text(
            """
            SELECT event_type, event_payload_canonical_json
              FROM captured_paper_post_commit_outbox_events
             WHERE completion_sha256 = :completion_sha256
               AND event_type IN (
                    'transport_invocation_authorized',
                    'transport_financial_breaker_recorded',
                    'transport_invocation_consumed'
               )
             ORDER BY sequence
            """
        ),
        {"completion_sha256": digest},
    ).mappings().all()
    if [str(item["event_type"]) for item in event_rows] != [
        "transport_invocation_authorized",
        "transport_financial_breaker_recorded",
    ]:
        raise CapturedPaperOutboxConflictError(
            "transport_dispatch_prior_event_sequence_invalid"
        )
    try:
        invocation_payload = json.loads(
            str(event_rows[0]["event_payload_canonical_json"])
        )
        financial_payload = json.loads(
            str(event_rows[1]["event_payload_canonical_json"])
        )
    except (TypeError, ValueError) as exc:
        raise CapturedPaperOutboxCorruptionError(
            "transport_dispatch_prior_event_invalid"
        ) from exc
    if (
        invocation_payload != invocation_authority.to_payload()
        or financial_payload != financial_breaker_receipt.to_payload()
    ):
        raise CapturedPaperOutboxCorruptionError(
            "transport_dispatch_prior_event_mismatch"
        )

    verified_at = _db_now(db)
    try:
        financial_breaker_receipt.verify_for_request(
            bundle.request,
            phase="pre_post",
            now=verified_at,
            require_allowed=True,
            transport_instruction_sha256=bundle.transport_instruction_sha256,
            transport_invocation_authority_sha256=(
                invocation_authority.invocation_authority_sha256
            ),
        )
    except CapturedPaperFinancialBreakerError as exc:
        raise CapturedPaperOutboxError(
            "transport_dispatch_financial_breaker_invalid"
        ) from exc
    valid_until = min(
        outbox_lease_expires_at,
        snapshot.action_lease_expires_at,
        snapshot.arm_expires_at,
        invocation_authority.valid_until,
        financial_breaker_receipt.valid_until,
        pre_dispatch_evidence.valid_until,
    )
    if not (pre_dispatch_evidence.prepared_at <= verified_at < valid_until):
        raise CapturedPaperOutboxLeaseError(
            "transport_dispatch_authority_expired"
        )
    previous = str(row["last_event_sha256"] or "")
    receipt = _attest_transport_dispatch_authority(
        CapturedPaperTransportDispatchAuthority(
            completion_sha256=digest,
            transport_authority_sha256=authority.authority_sha256,
            transport_instruction_sha256=bundle.transport_instruction_sha256,
            invocation_authority_sha256=(
                invocation_authority.invocation_authority_sha256
            ),
            financial_breaker_receipt_sha256=(
                financial_breaker_receipt.receipt_sha256
            ),
            pre_dispatch_evidence_sha256=(
                pre_dispatch_evidence.evidence_sha256
            ),
            connection_receipt_sha256=(
                pre_dispatch_evidence.connection_receipt_sha256
            ),
            lease_token=token,
            lease_owner_id=owner,
            verified_at=verified_at,
            valid_until=valid_until,
            outbox_version=int(row["version"]),
            dispatch_event_sequence=int(row["event_sequence"]) + 1,
            previous_event_sha256=previous,
        )
    )
    _append_event(
        db,
        row=row,
        event_type="transport_invocation_consumed",
        event_payload=receipt.to_payload(),
    )
    return receipt


@_atomic_mutation
def revalidate_captured_paper_transport_dispatch_authority(
    db: Any,
    *,
    completion_sha256: str,
    lease_token: str,
    lease_owner_id: str,
    authority: CapturedPaperTransportAuthority,
    invocation_authority: CapturedPaperTransportInvocationAuthority,
    financial_breaker_receipt: CapturedPaperFinancialBreakerReceipt,
    pre_dispatch_evidence: CapturedPaperTransportPreDispatchEvidence,
    dispatch_authority: CapturedPaperTransportDispatchAuthority,
) -> CapturedPaperTransportDispatchAuthority:
    """Re-read every live authority after the dispatch event has committed.

    The caller must serialize cooperating authority writers for the duration of
    this short transaction *and* the immediately following broker invocation.
    This function itself owns no network I/O.  It proves that the exact durable
    ``transport_invocation_consumed`` event still names the supplied one-shot
    authority and that reservation, action, session, opportunity, fill, and
    account truth has not changed since that event was appended.

    A process that crashes after the consumed event cannot use this function to
    manufacture fresh POST authority: the process-private attestation is still
    required and restart work remains same-CID reconciliation only.
    """

    if not (
        type(authority) is CapturedPaperTransportAuthority
        and type(invocation_authority)
        is CapturedPaperTransportInvocationAuthority
        and type(financial_breaker_receipt)
        is CapturedPaperFinancialBreakerReceipt
        and type(pre_dispatch_evidence)
        is CapturedPaperTransportPreDispatchEvidence
        and type(dispatch_authority)
        is CapturedPaperTransportDispatchAuthority
    ):
        raise CapturedPaperOutboxError(
            "transport_dispatch_revalidation_input_type_invalid"
        )

    digest = _digest(completion_sha256, field_name="completion_sha256")
    token = _canonical_uuid(
        lease_token, field_name="transport_dispatch_revalidation_lease_token"
    )
    owner = _canonical_uuid(
        lease_owner_id,
        field_name="transport_dispatch_revalidation_lease_owner_id",
    )
    initial = _select_row(db, completion_sha256=digest, for_update=False)
    if initial is None:
        raise CapturedPaperOutboxNotFoundError(
            "captured_paper_outbox_not_found"
        )
    if str(initial["status"]) != OUTBOX_STATUS_TRANSPORT_STARTED:
        raise CapturedPaperOutboxLeaseError(
            "transport_dispatch_revalidation_phase_invalid"
        )
    bundle = _durable_bundle_from_row(initial)
    invocation_authority.verify_for(
        authority,
        transport_instruction_sha256=bundle.transport_instruction_sha256,
        lease_token=token,
        lease_owner_id=owner,
    )
    pre_dispatch_evidence.verify_for(
        authority,
        invocation_authority,
        transport_instruction_sha256=bundle.transport_instruction_sha256,
    )
    dispatch_authority.verify_for(
        authority,
        invocation_authority,
        financial_breaker_receipt,
        pre_dispatch_evidence,
        transport_instruction_sha256=bundle.transport_instruction_sha256,
    )

    snapshot = _verify_durable_transport_binding(
        db,
        request=bundle.request,
        authority=authority,
        pre_transport_invocation=True,
    )
    if type(snapshot) is not _TransportInvocationBindingSnapshot:
        raise CapturedPaperOutboxError(
            "transport_dispatch_revalidation_binding_snapshot_missing"
        )
    _verify_durable_transport_related_metadata(db, bundle=bundle)

    row = _select_row(db, completion_sha256=digest, for_update=True)
    if row is None:
        raise CapturedPaperOutboxNotFoundError(
            "captured_paper_outbox_not_found"
        )
    locked_bundle = _durable_bundle_from_row(row)
    row_token = str(row["lease_token"]) if row["lease_token"] is not None else None
    row_owner = (
        str(row["lease_owner_id"])
        if row["lease_owner_id"] is not None
        else None
    )
    outbox_lease_expires_at = _aware_utc(
        row["lease_expires_at"],
        field_name="transport_dispatch_revalidation_outbox_lease_expires_at",
    )
    if not (
        row["status"] == OUTBOX_STATUS_TRANSPORT_STARTED
        and row_token == token
        and row_owner == owner
        and row["transport_evidence_sha256"] == authority.authority_sha256
        and locked_bundle.transport_instruction_sha256
        == bundle.transport_instruction_sha256
        and locked_bundle.committed_admission_sha256
        == bundle.committed_admission_sha256
        and locked_bundle.admission_record_sha256
        == bundle.admission_record_sha256
        and locked_bundle.authority.authority_sha256
        == authority.authority_sha256
        and snapshot.marker_lease_token == token
        and snapshot.marker_lease_owner_id == owner
    ):
        raise CapturedPaperOutboxLeaseError(
            "transport_dispatch_revalidation_outbox_binding_mismatch"
        )

    event_rows = db.execute(
        text(
            """
            SELECT sequence, event_type, previous_event_sha256, event_sha256,
                   event_payload_canonical_json
              FROM captured_paper_post_commit_outbox_events
             WHERE completion_sha256 = :completion_sha256
               AND event_type IN (
                    'transport_invocation_authorized',
                    'transport_financial_breaker_recorded',
                    'transport_invocation_consumed'
               )
             ORDER BY sequence
            """
        ),
        {"completion_sha256": digest},
    ).mappings().all()
    if [str(item["event_type"]) for item in event_rows] != [
        "transport_invocation_authorized",
        "transport_financial_breaker_recorded",
        "transport_invocation_consumed",
    ]:
        raise CapturedPaperOutboxConflictError(
            "transport_dispatch_revalidation_event_sequence_invalid"
        )
    invocation_payload = _strict_event_payload(
        str(event_rows[0]["event_payload_canonical_json"])
    )
    financial_payload = _strict_event_payload(
        str(event_rows[1]["event_payload_canonical_json"])
    )
    dispatch_payload = _strict_event_payload(
        str(event_rows[2]["event_payload_canonical_json"])
    )
    if not (
        invocation_payload == invocation_authority.to_payload()
        and financial_payload == financial_breaker_receipt.to_payload()
        and dispatch_payload == dispatch_authority.to_payload()
        and int(event_rows[2]["sequence"])
        == dispatch_authority.dispatch_event_sequence
        and event_rows[2]["previous_event_sha256"]
        == dispatch_authority.previous_event_sha256
        and event_rows[1]["event_sha256"]
        == dispatch_authority.previous_event_sha256
        and int(row["event_sequence"])
        == dispatch_authority.dispatch_event_sequence
        and row["last_event_sha256"] == event_rows[2]["event_sha256"]
        and int(row["version"]) == dispatch_authority.outbox_version + 1
    ):
        raise CapturedPaperOutboxCorruptionError(
            "transport_dispatch_revalidation_event_mismatch"
        )

    verified_at = _db_now(db)
    try:
        financial_breaker_receipt.verify_for_request(
            bundle.request,
            phase="pre_post",
            now=verified_at,
            require_allowed=True,
            transport_instruction_sha256=bundle.transport_instruction_sha256,
            transport_invocation_authority_sha256=(
                invocation_authority.invocation_authority_sha256
            ),
        )
    except CapturedPaperFinancialBreakerError as exc:
        raise CapturedPaperOutboxError(
            "transport_dispatch_revalidation_financial_breaker_invalid"
        ) from exc
    valid_until = min(
        outbox_lease_expires_at,
        snapshot.action_lease_expires_at,
        snapshot.arm_expires_at,
        invocation_authority.valid_until,
        financial_breaker_receipt.valid_until,
        pre_dispatch_evidence.valid_until,
    )
    if not (
        pre_dispatch_evidence.prepared_at <= verified_at < valid_until
        and dispatch_authority.valid_until == valid_until
        and dispatch_authority.verified_at <= verified_at
    ):
        raise CapturedPaperOutboxLeaseError(
            "transport_dispatch_revalidation_authority_expired"
        )
    return dispatch_authority


def _append_event(
    db: Any,
    *,
    row: Mapping[str, Any],
    event_type: str,
    event_payload: Mapping[str, Any],
) -> None:
    completion_sha256 = str(row["completion_sha256"])
    sequence = int(row["event_sequence"]) + 1
    previous = row["last_event_sha256"]
    payload_json = _canonical_json(event_payload)
    payload_sha256 = _sha256_text(payload_json)
    effective_at = _db_now(db)
    event_sha256 = _event_hash(
        completion_sha256=completion_sha256,
        sequence=sequence,
        event_type=event_type,
        previous_event_sha256=previous,
        event_payload_sha256=payload_sha256,
        effective_at=effective_at,
    )
    db.execute(
        text(
            """
            INSERT INTO captured_paper_post_commit_outbox_events (
                completion_sha256, sequence, event_type,
                previous_event_sha256, event_sha256,
                event_payload_sha256, event_payload_canonical_json,
                effective_at
            ) VALUES (
                :completion_sha256, :sequence, :event_type,
                :previous_event_sha256, :event_sha256,
                :event_payload_sha256, :event_payload_canonical_json,
                :effective_at
            )
            """
        ),
        {
            "completion_sha256": completion_sha256,
            "sequence": sequence,
            "event_type": event_type,
            "previous_event_sha256": previous,
            "event_sha256": event_sha256,
            "event_payload_sha256": payload_sha256,
            "event_payload_canonical_json": payload_json,
            "effective_at": effective_at,
        },
    )
    result = db.execute(
        text(
            """
            UPDATE captured_paper_post_commit_outbox
               SET event_sequence = :sequence,
                   last_event_sha256 = :event_sha256,
                   updated_at = clock_timestamp(),
                   version = version + 1
             WHERE completion_sha256 = :completion_sha256
               AND event_sequence = :previous_sequence
               AND last_event_sha256 IS NOT DISTINCT FROM :previous_event_sha256
               AND version = :version
            """
        ),
        {
            "completion_sha256": completion_sha256,
            "sequence": sequence,
            "event_sha256": event_sha256,
            "previous_sequence": int(row["event_sequence"]),
            "previous_event_sha256": previous,
            "version": int(row["version"]),
        },
    )
    if result.rowcount != 1:
        raise CapturedPaperOutboxConflictError(
            "outbox_event_head_cas_failed"
        )


def _fill_watch_row(
    db: Any,
    *,
    completion_sha256: str,
    for_update: bool,
) -> Mapping[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    return db.execute(
        text(
            """
            SELECT completion_sha256, completion_proof_sha256,
                   broker_order_id, broker_connection_generation,
                   broker_order_evidence_sha256, broker_observed_at,
                   broker_available_at, state, attempt_count, lease_token,
                   lease_owner_id, lease_expires_at, next_attempt_at,
                   last_observation_sha256, terminal_receipt_sha256,
                   terminal_at, event_sequence, last_event_sha256, version,
                   created_at, updated_at
              FROM captured_paper_completed_fill_watch
             WHERE completion_sha256 = :completion_sha256
            """
            + suffix
        ),
        {"completion_sha256": completion_sha256},
    ).mappings().one_or_none()


def _append_fill_watch_event(
    db: Any,
    *,
    row: Mapping[str, Any],
    event_type: str,
    event_payload: Mapping[str, Any],
) -> None:
    completion_sha256 = _digest(
        str(row["completion_sha256"]), field_name="fill_watch_completion_sha256"
    )
    sequence = int(row["event_sequence"]) + 1
    previous = row["last_event_sha256"]
    payload_json = _canonical_json(event_payload)
    payload_sha256 = _sha256_text(payload_json)
    effective_at = _db_now(db)
    event_sha256 = _sha256_text(
        _canonical_json(
            {
                "schema_version": (
                    CAPTURED_PAPER_COMPLETED_FILL_WATCH_SCHEMA_VERSION
                ),
                "completion_sha256": completion_sha256,
                "sequence": sequence,
                "event_type": event_type,
                "previous_event_sha256": previous,
                "event_payload_sha256": payload_sha256,
                "effective_at": effective_at.isoformat(),
            }
        )
    )
    db.execute(
        text(
            """
            INSERT INTO captured_paper_completed_fill_watch_events (
                completion_sha256, sequence, event_type,
                previous_event_sha256, event_payload_canonical_json,
                event_payload_sha256, event_sha256, effective_at
            ) VALUES (
                :completion_sha256, :sequence, :event_type,
                :previous_event_sha256, :event_payload_canonical_json,
                :event_payload_sha256, :event_sha256, :effective_at
            )
            """
        ),
        {
            "completion_sha256": completion_sha256,
            "sequence": sequence,
            "event_type": event_type,
            "previous_event_sha256": previous,
            "event_payload_canonical_json": payload_json,
            "event_payload_sha256": payload_sha256,
            "event_sha256": event_sha256,
            "effective_at": effective_at,
        },
    )
    result = db.execute(
        text(
            """
            UPDATE captured_paper_completed_fill_watch
               SET event_sequence = :sequence,
                   last_event_sha256 = :event_sha256,
                   updated_at = clock_timestamp(), version = version + 1
             WHERE completion_sha256 = :completion_sha256
               AND event_sequence = :previous_sequence
               AND last_event_sha256 IS NOT DISTINCT FROM
                   :previous_event_sha256
               AND version = :version
            """
        ),
        {
            "completion_sha256": completion_sha256,
            "sequence": sequence,
            "event_sha256": event_sha256,
            "previous_sequence": int(row["event_sequence"]),
            "previous_event_sha256": previous,
            "version": int(row["version"]),
        },
    )
    if result.rowcount != 1:
        raise CapturedPaperOutboxConflictError(
            "fill_watch_event_head_cas_failed"
        )


def _ensure_completed_fill_watch(
    db: Any,
    *,
    request: CapturedPaperPostCommitRequest,
    authority: CapturedPaperTransportAuthority,
    acceptance: CapturedPaperBrokerAcceptanceProof,
    binding: _PositiveAdoptionBindingSnapshot | None,
    completion_event_type: str,
) -> None:
    if completion_event_type not in {
        "completion_accepted",
        "reconciliation_accepted",
    }:
        raise CapturedPaperOutboxError("fill_watch_completion_event_invalid")
    if binding is None or any(
        value is None
        for value in (
            binding.broker_order_id,
            binding.broker_connection_generation,
            binding.broker_order_evidence_sha256,
            binding.broker_observed_at,
            binding.broker_available_at,
        )
    ):
        raise CapturedPaperOutboxCorruptionError(
            "fill_watch_acceptance_binding_incomplete"
        )
    if not (
        binding.broker_order_id == acceptance.broker_order_id
        and binding.broker_order_evidence_sha256
        == acceptance.broker_order_evidence_sha256
        and binding.broker_observed_at == acceptance.observed_at
        and binding.broker_available_at == acceptance.available_at
    ):
        raise CapturedPaperOutboxConflictError(
            "fill_watch_acceptance_binding_mismatch"
        )
    inserted = db.execute(
        text(
            """
            INSERT INTO captured_paper_completed_fill_watch (
                completion_sha256, completion_proof_sha256,
                broker_order_id, broker_connection_generation,
                broker_order_evidence_sha256, broker_observed_at,
                broker_available_at, state
            ) VALUES (
                :completion_sha256, :completion_proof_sha256,
                :broker_order_id, :broker_connection_generation,
                :broker_order_evidence_sha256, :broker_observed_at,
                :broker_available_at, 'pending'
            )
            ON CONFLICT (completion_sha256) DO NOTHING
            """
        ),
        {
            "completion_sha256": request.completion_sha256,
            "completion_proof_sha256": acceptance.acceptance_sha256,
            "broker_order_id": acceptance.broker_order_id,
            "broker_connection_generation": (
                binding.broker_connection_generation
            ),
            "broker_order_evidence_sha256": (
                acceptance.broker_order_evidence_sha256
            ),
            "broker_observed_at": acceptance.observed_at,
            "broker_available_at": acceptance.available_at,
        },
    )
    row = _fill_watch_row(
        db,
        completion_sha256=request.completion_sha256,
        for_update=True,
    )
    if row is None:
        raise CapturedPaperOutboxCorruptionError(
            "fill_watch_enqueue_missing"
        )
    expected = {
        "completion_proof_sha256": acceptance.acceptance_sha256,
        "broker_order_id": acceptance.broker_order_id,
        "broker_connection_generation": binding.broker_connection_generation,
        "broker_order_evidence_sha256": (
            acceptance.broker_order_evidence_sha256
        ),
        "broker_observed_at": acceptance.observed_at,
        "broker_available_at": acceptance.available_at,
    }
    if any(row[name] != value for name, value in expected.items()):
        raise CapturedPaperOutboxCorruptionError(
            "fill_watch_enqueue_identity_mismatch"
        )
    if int(inserted.rowcount or 0) == 1:
        _append_fill_watch_event(
            db,
            row=row,
            event_type="watch_enqueued",
            event_payload={
                "completion_sha256": request.completion_sha256,
                "completion_event_type": completion_event_type,
                "completion_proof_sha256": acceptance.acceptance_sha256,
                "transport_authority_sha256": authority.authority_sha256,
                "broker_order_id": acceptance.broker_order_id,
                "broker_connection_generation": (
                    binding.broker_connection_generation
                ),
            },
        )


@_atomic_mutation
def lease_next_captured_paper_completed_fill_watch(
    db: Any,
    *,
    lease_owner_id: str,
    lease_seconds: int,
) -> CapturedPaperCompletedFillWatchLease | None:
    """Lease one exact accepted order without granting POST authority."""

    owner = _canonical_uuid(
        lease_owner_id, field_name="fill_watch_lease_owner_id"
    )
    seconds = _bounded_positive_int(
        lease_seconds,
        field_name="fill_watch_lease_seconds",
        maximum=_MAX_LEASE_SECONDS,
    )
    row = db.execute(
        text(
            """
            SELECT w.*
              FROM captured_paper_completed_fill_watch w
              JOIN captured_paper_post_commit_outbox o
                ON o.completion_sha256 = w.completion_sha256
             WHERE o.status = 'completed'
               AND (
                    w.state = 'pending'
                    OR (w.state = 'retry_wait'
                        AND w.next_attempt_at <= clock_timestamp())
                    OR (w.state = 'leased'
                        AND w.lease_expires_at <= clock_timestamp())
               )
             ORDER BY COALESCE(
                        w.next_attempt_at,
                        w.lease_expires_at,
                        w.updated_at
                      ), w.completion_sha256
             FOR UPDATE OF w SKIP LOCKED
             LIMIT 1
            """
        )
    ).mappings().one_or_none()
    if row is None:
        return None
    recovered = row["state"] == FILL_WATCH_STATE_LEASED
    token = str(uuid.uuid4())
    result = db.execute(
        text(
            """
            UPDATE captured_paper_completed_fill_watch
               SET state = 'leased', attempt_count = attempt_count + 1,
                   lease_token = CAST(:lease_token AS UUID),
                   lease_owner_id = CAST(:lease_owner_id AS UUID),
                   lease_expires_at = clock_timestamp()
                       + make_interval(secs => :lease_seconds),
                   next_attempt_at = NULL, updated_at = clock_timestamp(),
                   version = version + 1
             WHERE completion_sha256 = :completion_sha256
               AND version = :version
            """
        ),
        {
            "completion_sha256": row["completion_sha256"],
            "version": int(row["version"]),
            "lease_token": token,
            "lease_owner_id": owner,
            "lease_seconds": seconds,
        },
    )
    if result.rowcount != 1:
        raise CapturedPaperOutboxConflictError("fill_watch_lease_cas_failed")
    changed = _fill_watch_row(
        db,
        completion_sha256=str(row["completion_sha256"]),
        for_update=True,
    )
    assert changed is not None
    _append_fill_watch_event(
        db,
        row=changed,
        event_type="watch_lease_recovered" if recovered else "watch_leased",
        event_payload={
            "lease_token": token,
            "lease_owner_id": owner,
            "attempt_count": int(changed["attempt_count"]),
            "recovered_expired_lease": recovered,
        },
    )
    final = _fill_watch_row(
        db,
        completion_sha256=str(row["completion_sha256"]),
        for_update=True,
    )
    assert final is not None and final["lease_expires_at"] is not None
    return CapturedPaperCompletedFillWatchLease(
        completion_sha256=str(final["completion_sha256"]),
        lease_token=str(final["lease_token"]),
        lease_owner_id=str(final["lease_owner_id"]),
        lease_expires_at=_aware_utc(
            final["lease_expires_at"],
            field_name="fill_watch_lease_expires_at",
        ),
        attempt_count=int(final["attempt_count"]),
        recovered=recovered,
    )


def _exact_fill_watch_lease(
    row: Mapping[str, Any],
    lease: CapturedPaperCompletedFillWatchLease,
    *,
    now: datetime,
) -> None:
    if type(lease) is not CapturedPaperCompletedFillWatchLease:
        raise CapturedPaperOutboxLeaseError("fill_watch_lease_type_invalid")
    if not (
        row["state"] == FILL_WATCH_STATE_LEASED
        and str(row["completion_sha256"]) == lease.completion_sha256
        and str(row["lease_token"]) == lease.lease_token
        and str(row["lease_owner_id"]) == lease.lease_owner_id
        and _aware_utc(
            row["lease_expires_at"],
            field_name="fill_watch_row_lease_expires_at",
        )
        == lease.lease_expires_at
        and lease.lease_expires_at > now
    ):
        raise CapturedPaperOutboxLeaseError("fill_watch_lease_mismatch")


def load_captured_paper_completed_fill_watch_bundle(
    db: Any,
    *,
    lease: CapturedPaperCompletedFillWatchLease,
) -> CapturedPaperCompletedFillWatchBundle:
    """Rebuild immutable accepted-order read authority; never POST authority."""

    if type(lease) is not CapturedPaperCompletedFillWatchLease:
        raise CapturedPaperOutboxLeaseError("fill_watch_lease_type_invalid")
    initial = _select_row(
        db,
        completion_sha256=lease.completion_sha256,
        for_update=False,
    )
    watch = _fill_watch_row(
        db,
        completion_sha256=lease.completion_sha256,
        for_update=False,
    )
    if initial is None or watch is None:
        raise CapturedPaperOutboxNotFoundError("fill_watch_work_not_found")
    durable = _durable_bundle_from_row(initial)
    initial_record = _record_from_row(db, initial)
    if not (
        initial_record.status == OUTBOX_STATUS_COMPLETED
        and initial_record.completion_proof_sha256
        == watch["completion_proof_sha256"]
        and initial_record.events
        and initial_record.events[-1].event_type
        in {"completion_accepted", "reconciliation_accepted"}
    ):
        raise CapturedPaperOutboxCorruptionError(
            "fill_watch_completed_outbox_mismatch"
        )
    event_type = initial_record.events[-1].event_type
    acceptance = CapturedPaperBrokerAcceptanceProof(
        acceptance_kind=(
            "post_response"
            if event_type == "completion_accepted"
            else "same_cid_reconciliation"
        ),
        completion_sha256=lease.completion_sha256,
        account_scope=durable.authority.account_scope,
        expected_account_id=durable.authority.expected_account_id,
        client_order_id=durable.authority.client_order_id,
        broker_order_id=str(watch["broker_order_id"]),
        reservation_id=durable.authority.reservation_id,
        action_claim_token=durable.authority.action_claim_token,
        binder_id=durable.authority.binder_id,
        broker_order_evidence_sha256=str(
            watch["broker_order_evidence_sha256"]
        ),
        observed_at=_aware_utc(
            watch["broker_observed_at"],
            field_name="fill_watch_broker_observed_at",
        ),
        available_at=_aware_utc(
            watch["broker_available_at"],
            field_name="fill_watch_broker_available_at",
        ),
    )
    if acceptance.acceptance_sha256 != watch["completion_proof_sha256"]:
        raise CapturedPaperOutboxCorruptionError(
            "fill_watch_completion_proof_mismatch"
        )
    binding = _verify_durable_transport_binding(
        db,
        request=durable.request,
        authority=durable.authority,
        acceptance=acceptance,
    )
    locked_outbox = _select_row(
        db,
        completion_sha256=lease.completion_sha256,
        for_update=True,
    )
    locked_watch = _fill_watch_row(
        db,
        completion_sha256=lease.completion_sha256,
        for_update=True,
    )
    if locked_outbox is None or locked_watch is None:
        raise CapturedPaperOutboxNotFoundError("fill_watch_work_not_found")
    record = _record_from_row(db, locked_outbox)
    _exact_fill_watch_lease(locked_watch, lease, now=_db_now(db))
    if binding is None or not (
        record.status == OUTBOX_STATUS_COMPLETED
        and record.completion_proof_sha256 == acceptance.acceptance_sha256
        and record.durable_transport.transport_instruction_sha256
        == durable.transport_instruction_sha256
        and binding.broker_order_id == watch["broker_order_id"]
        and binding.broker_connection_generation
        == watch["broker_connection_generation"]
        and binding.broker_order_evidence_sha256
        == watch["broker_order_evidence_sha256"]
    ):
        raise CapturedPaperOutboxCorruptionError(
            "fill_watch_canonical_binding_mismatch"
        )
    return CapturedPaperCompletedFillWatchBundle(
        durable_transport=record.durable_transport,
        lease=lease,
        completion_proof_sha256=acceptance.acceptance_sha256,
        completion_event_type=event_type,
        broker_order_id=str(locked_watch["broker_order_id"]),
        broker_connection_generation=str(
            locked_watch["broker_connection_generation"]
        ),
        broker_order_evidence_sha256=str(
            locked_watch["broker_order_evidence_sha256"]
        ),
        broker_observed_at=acceptance.observed_at,
        broker_available_at=acceptance.available_at,
    )


@_atomic_mutation
def reschedule_captured_paper_completed_fill_watch(
    db: Any,
    *,
    lease: CapturedPaperCompletedFillWatchLease,
    observation_sha256: str,
    retry_delay_seconds: int,
    reason: str,
) -> None:
    observation = _digest(
        observation_sha256, field_name="fill_watch_observation_sha256"
    )
    delay = _bounded_positive_int(
        retry_delay_seconds,
        field_name="fill_watch_retry_delay_seconds",
        maximum=_MAX_RETRY_DELAY_SECONDS,
    )
    normalized_reason = str(reason or "").strip().lower()
    if _BROKER_ID_RE.fullmatch(normalized_reason) is None:
        raise CapturedPaperOutboxError("fill_watch_retry_reason_invalid")
    outbox_row = _select_row(
        db,
        completion_sha256=lease.completion_sha256,
        for_update=True,
    )
    if outbox_row is None or str(outbox_row["status"]) != OUTBOX_STATUS_COMPLETED:
        raise CapturedPaperOutboxConflictError(
            "fill_watch_retry_outbox_not_completed"
        )
    row = _fill_watch_row(
        db,
        completion_sha256=lease.completion_sha256,
        for_update=True,
    )
    if row is None:
        raise CapturedPaperOutboxNotFoundError("fill_watch_work_not_found")
    _exact_fill_watch_lease(row, lease, now=_db_now(db))
    result = db.execute(
        text(
            """
            UPDATE captured_paper_completed_fill_watch
               SET state = 'retry_wait', lease_token = NULL,
                   lease_owner_id = NULL, lease_expires_at = NULL,
                   next_attempt_at = clock_timestamp()
                       + make_interval(secs => :retry_delay_seconds),
                   last_observation_sha256 = :observation_sha256,
                   updated_at = clock_timestamp(), version = version + 1
             WHERE completion_sha256 = :completion_sha256
               AND version = :version AND state = 'leased'
               AND lease_token = CAST(:lease_token AS UUID)
               AND lease_owner_id = CAST(:lease_owner_id AS UUID)
            """
        ),
        {
            "completion_sha256": lease.completion_sha256,
            "version": int(row["version"]),
            "lease_token": lease.lease_token,
            "lease_owner_id": lease.lease_owner_id,
            "retry_delay_seconds": delay,
            "observation_sha256": observation,
        },
    )
    if result.rowcount != 1:
        raise CapturedPaperOutboxConflictError(
            "fill_watch_retry_cas_failed"
        )
    changed = _fill_watch_row(
        db,
        completion_sha256=lease.completion_sha256,
        for_update=True,
    )
    assert changed is not None
    _append_fill_watch_event(
        db,
        row=changed,
        event_type="watch_rescheduled",
        event_payload={
            "observation_sha256": observation,
            "reason": normalized_reason,
            "retry_delay_seconds": delay,
            "attempt_count": int(changed["attempt_count"]),
        },
    )


def _mark_completed_fill_watch_handoff(
    db: Any,
    *,
    completion_sha256: str,
    broker_order_id: str,
    observation_sha256: str,
    terminal_receipt_sha256: str,
) -> None:
    row = _fill_watch_row(
        db,
        completion_sha256=completion_sha256,
        for_update=True,
    )
    if row is None:
        raise CapturedPaperOutboxCorruptionError(
            "fill_watch_handoff_row_missing"
        )
    receipt = _digest(
        terminal_receipt_sha256,
        field_name="fill_watch_terminal_receipt_sha256",
    )
    observation = _digest(
        observation_sha256, field_name="fill_watch_observation_sha256"
    )
    if row["state"] == FILL_WATCH_STATE_HANDOFF_COMMITTED:
        if not (
            row["terminal_receipt_sha256"] == receipt
            and row["last_observation_sha256"] == observation
        ):
            raise CapturedPaperOutboxCorruptionError(
                "fill_watch_handoff_idempotency_mismatch"
            )
        return
    if not (
        row["state"]
        in {
            FILL_WATCH_STATE_PENDING,
            FILL_WATCH_STATE_LEASED,
            FILL_WATCH_STATE_RETRY_WAIT,
        }
        and row["broker_order_id"] == broker_order_id
    ):
        raise CapturedPaperOutboxConflictError(
            "fill_watch_handoff_state_mismatch"
        )
    _append_fill_watch_event(
        db,
        row=row,
        event_type="watch_fill_handoff_committed",
        event_payload={
            "broker_order_id": broker_order_id,
            "observation_sha256": observation,
            "terminal_receipt_sha256": receipt,
        },
    )
    current = _fill_watch_row(
        db,
        completion_sha256=completion_sha256,
        for_update=True,
    )
    assert current is not None
    result = db.execute(
        text(
            """
            UPDATE captured_paper_completed_fill_watch
               SET state = 'fill_handoff_committed', lease_token = NULL,
                   lease_owner_id = NULL, lease_expires_at = NULL,
                   next_attempt_at = NULL,
                   last_observation_sha256 = :observation_sha256,
                   terminal_receipt_sha256 = :terminal_receipt_sha256,
                   terminal_at = clock_timestamp(),
                   updated_at = clock_timestamp(), version = version + 1
             WHERE completion_sha256 = :completion_sha256
               AND version = :version
               AND state IN ('pending', 'leased', 'retry_wait')
            """
        ),
        {
            "completion_sha256": completion_sha256,
            "version": int(current["version"]),
            "observation_sha256": observation,
            "terminal_receipt_sha256": receipt,
        },
    )
    if result.rowcount != 1:
        raise CapturedPaperOutboxConflictError(
            "fill_watch_handoff_cas_failed"
        )


@_atomic_mutation
def complete_captured_paper_terminal_zero_fill_watch(
    db: Any,
    *,
    lease: CapturedPaperCompletedFillWatchLease,
    observation_sha256: str,
    terminal_receipt_sha256: str,
) -> None:
    """Retire only an exact durable zero-fill terminal observation."""

    observation = _digest(
        observation_sha256, field_name="fill_watch_observation_sha256"
    )
    receipt = _digest(
        terminal_receipt_sha256,
        field_name="fill_watch_terminal_receipt_sha256",
    )
    outbox_row = _select_row(
        db,
        completion_sha256=lease.completion_sha256,
        for_update=True,
    )
    if outbox_row is None or str(outbox_row["status"]) != OUTBOX_STATUS_COMPLETED:
        raise CapturedPaperOutboxConflictError(
            "fill_watch_terminal_outbox_not_completed"
        )
    bundle = _durable_bundle_from_row(outbox_row)
    row = _fill_watch_row(
        db,
        completion_sha256=lease.completion_sha256,
        for_update=True,
    )
    if row is None:
        raise CapturedPaperOutboxNotFoundError("fill_watch_work_not_found")
    _exact_fill_watch_lease(row, lease, now=_db_now(db))
    durable = db.execute(
        text(
            """
            SELECT r.state, r.cumulative_filled_quantity_shares,
                   r.open_quantity_shares, r.release_reason,
                   r.last_source_event_content_sha256,
                   r.setup_family, r.opportunity_claim_id,
                   p.id AS opportunity_id,
                   p.status AS opportunity_status,
                   p.reservation_id AS opportunity_reservation_id,
                   p.consumed_by_reservation_id,
                   o.observation_sha256, o.reservation_id,
                   o.provider_order_id, o.expected_client_order_id,
                   o.cycle_broker_connection_generation,
                   o.observation_authority_status, o.order_role,
                   o.exact_activity_count, o.pagination_complete,
                   c.phase AS action_claim_phase,
                   c.account_scope AS action_claim_account_scope,
                   c.symbol AS action_claim_symbol,
                   c.action AS action_claim_action,
                   c.claim_token AS action_claim_token,
                   c.client_order_id AS action_claim_client_order_id,
                   c.broker_order_id AS action_claim_broker_order_id,
                   c.metadata_json->>'entry_post_bind_token'
                       AS action_claim_binder_id
              FROM adaptive_risk_reservations r
              LEFT JOIN adaptive_risk_opportunity_claims p
                ON p.id = r.opportunity_claim_id
              JOIN alpaca_paper_fill_query_observations o
                ON o.reservation_id = r.reservation_id
              JOIN broker_symbol_action_claims c
                ON c.account_scope = r.account_scope
               AND upper(c.symbol) = upper(r.symbol)
             WHERE r.reservation_id = CAST(:reservation_id AS UUID)
               AND o.observation_sha256 = :observation_sha256
             FOR UPDATE OF r, o
            """
        ),
        {
            "reservation_id": bundle.authority.reservation_id,
            "observation_sha256": observation,
        },
    ).mappings().one_or_none()
    if durable is None or not (
        durable["state"] == "released"
        and int(durable["cumulative_filled_quantity_shares"]) == 0
        and int(durable["open_quantity_shares"]) == 0
        and durable["release_reason"]
        in {"broker_rejected", "broker_canceled", "broker_expired"}
        and durable["last_source_event_content_sha256"] == observation
        and (
            (
                durable["setup_family"] == "first_dip_reclaim"
                and durable["opportunity_claim_id"] is not None
                and durable["opportunity_id"]
                == durable["opportunity_claim_id"]
                and durable["opportunity_status"] == "available"
                and durable["opportunity_reservation_id"] is None
                and durable["consumed_by_reservation_id"] is None
            )
            or (
                durable["setup_family"] != "first_dip_reclaim"
                and durable["opportunity_claim_id"] is None
                and durable["opportunity_id"] is None
            )
        )
        and durable["reservation_id"]
        == uuid.UUID(bundle.authority.reservation_id)
        and durable["provider_order_id"] == row["broker_order_id"]
        and durable["expected_client_order_id"]
        == bundle.authority.client_order_id
        and durable["cycle_broker_connection_generation"]
        == row["broker_connection_generation"]
        and durable["observation_authority_status"] == "verified"
        and durable["order_role"] == "entry"
        and int(durable["exact_activity_count"]) == 0
        and durable["pagination_complete"] is True
        and durable["action_claim_phase"] == "resolved"
        and durable["action_claim_account_scope"]
        == bundle.authority.account_scope
        and str(durable["action_claim_symbol"]).strip().upper()
        == bundle.authority.symbol
        and durable["action_claim_action"] == "entry"
        and durable["action_claim_token"]
        == bundle.authority.action_claim_token
        and durable["action_claim_client_order_id"]
        == bundle.authority.client_order_id
        and durable["action_claim_broker_order_id"]
        == row["broker_order_id"]
        and durable["action_claim_binder_id"]
        == bundle.authority.binder_id
    ):
        raise CapturedPaperOutboxCorruptionError(
            "fill_watch_terminal_zero_durability_mismatch"
        )
    _append_fill_watch_event(
        db,
        row=row,
        event_type="watch_terminal_zero_fill",
        event_payload={
            "observation_sha256": observation,
            "terminal_receipt_sha256": receipt,
            "release_reason": durable["release_reason"],
        },
    )
    current = _fill_watch_row(
        db,
        completion_sha256=lease.completion_sha256,
        for_update=True,
    )
    assert current is not None
    result = db.execute(
        text(
            """
            UPDATE captured_paper_completed_fill_watch
               SET state = 'terminal_zero_fill', lease_token = NULL,
                   lease_owner_id = NULL, lease_expires_at = NULL,
                   next_attempt_at = NULL,
                   last_observation_sha256 = :observation_sha256,
                   terminal_receipt_sha256 = :terminal_receipt_sha256,
                   terminal_at = clock_timestamp(),
                   updated_at = clock_timestamp(), version = version + 1
             WHERE completion_sha256 = :completion_sha256
               AND version = :version AND state = 'leased'
               AND lease_token = CAST(:lease_token AS UUID)
               AND lease_owner_id = CAST(:lease_owner_id AS UUID)
            """
        ),
        {
            "completion_sha256": lease.completion_sha256,
            "version": int(current["version"]),
            "lease_token": lease.lease_token,
            "lease_owner_id": lease.lease_owner_id,
            "observation_sha256": observation,
            "terminal_receipt_sha256": receipt,
        },
    )
    if result.rowcount != 1:
        raise CapturedPaperOutboxConflictError(
            "fill_watch_terminal_zero_cas_failed"
        )


@_atomic_mutation
def commit_captured_paper_fill_handoff(
    db: Any,
    *,
    completion_sha256: str,
    authority: CapturedPaperTransportAuthority,
    proof: "AlpacaPaperEntryFillHandoffProof",
) -> CapturedPaperOutboxRecord:
    """Hand positive-fill transport ownership to immutable fill truth.

    The caller must invoke this in the same outer transaction that published
    the fill activity (or the post-settlement contradiction) and advanced the
    adaptive reservation.  The typed verifier re-reads that exact durable
    lineage; this function then seals the proof onto the outbox.  It never
    manufactures generic broker acceptance and never makes a row eligible for
    another POST or same-CID reconciliation pass.
    """

    from .alpaca_fill_activity import (
        AlpacaPaperEntryFillHandoffProof,
        verify_alpaca_paper_entry_fill_handoff,
    )

    digest = _digest(completion_sha256, field_name="completion_sha256")
    if type(authority) is not CapturedPaperTransportAuthority:
        raise CapturedPaperOutboxError("transport_authority_type_invalid")
    if type(proof) is not AlpacaPaperEntryFillHandoffProof:
        raise CapturedPaperOutboxError("fill_handoff_proof_type_invalid")

    initial = _select_row(db, completion_sha256=digest, for_update=False)
    if initial is None:
        raise CapturedPaperOutboxNotFoundError(
            "captured_paper_outbox_not_found"
        )
    initial_bundle = _durable_bundle_from_row(initial)
    request = initial_bundle.request
    authority.verify_for_request(request)
    if (
        initial_bundle.authority.authority_sha256
        != authority.authority_sha256
    ):
        raise CapturedPaperOutboxConflictError(
            "fill_handoff_transport_authority_mismatch"
        )

    verified = verify_alpaca_paper_entry_fill_handoff(db, proof)
    if verified is not proof and (
        verified.to_canonical_json() != proof.to_canonical_json()
        or verified.proof_sha256 != proof.proof_sha256
    ):
        raise CapturedPaperOutboxCorruptionError(
            "fill_handoff_verifier_changed_proof"
        )
    proof_json = proof.to_canonical_json()
    if not (
        proof_json == proof.proof_canonical_json
        and _sha256_text(proof_json) == proof.proof_sha256
        and proof.to_payload().get("proof_sha256") == proof.proof_sha256
        and str(proof.reservation_id) == authority.reservation_id
        and proof.decision_packet_sha256
        == authority.decision_packet_sha256
        and proof.account_scope == authority.account_scope
        and proof.account_identity_sha256
        == authority.account_identity_sha256
        and proof.client_order_id == authority.client_order_id
    ):
        raise CapturedPaperOutboxConflictError(
            "fill_handoff_authority_binding_mismatch"
        )

    row = _select_row(db, completion_sha256=digest, for_update=True)
    if row is None:
        raise CapturedPaperOutboxNotFoundError(
            "captured_paper_outbox_not_found"
        )
    record = _record_from_row(db, row)
    if not (
        record.request.to_canonical_json() == request.to_canonical_json()
        and record.durable_transport.authority.authority_sha256
        == authority.authority_sha256
        and record.transport_evidence_sha256 == authority.authority_sha256
        and record.transport_started_at is not None
        and proof.available_at >= record.transport_started_at
    ):
        raise CapturedPaperOutboxConflictError(
            "fill_handoff_outbox_binding_mismatch"
        )

    if record.status == OUTBOX_STATUS_FILL_HANDOFF_COMMITTED:
        if not (
            record.fill_handoff_proof_sha256 == proof.proof_sha256
            and record.fill_handoff_proof
            == json.loads(proof.proof_canonical_json)
        ):
            raise CapturedPaperOutboxConflictError(
                "fill_handoff_idempotency_mismatch"
            )
        return record
    indeterminate_source = bool(
        record.status == OUTBOX_STATUS_TRANSPORT_INDETERMINATE
        and record.lease_token is None
        and record.lease_owner_id is None
        and record.lease_expires_at is None
        and record.transport_indeterminate_at is not None
        and record.indeterminate_evidence_sha256 is not None
        and record.completion_proof_sha256 is None
        and record.completed_at is None
    )
    completed_source = bool(
        record.status == OUTBOX_STATUS_COMPLETED
        and record.lease_token is None
        and record.lease_owner_id is None
        and record.lease_expires_at is None
        and record.completion_proof_sha256 is not None
        and record.completed_at is not None
    )
    prior_completion_event_type: str | None = None
    if completed_source:
        prior_completion_event = record.events[-1] if record.events else None
        if prior_completion_event is None or prior_completion_event.event_type not in {
            "completion_accepted",
            "reconciliation_accepted",
        }:
            raise CapturedPaperOutboxCorruptionError(
                "fill_handoff_completion_event_missing"
            )
        prior_completion_event_type = prior_completion_event.event_type
        prior_payload = prior_completion_event.event_payload
        if not (
            prior_payload.get("completion_proof_sha256")
            == record.completion_proof_sha256
            and prior_payload.get("broker_order_id") == proof.broker_order_id
            and prior_payload.get("client_order_id") == proof.client_order_id
            and prior_payload.get("reservation_id") == authority.reservation_id
            and proof.available_at >= record.completed_at
        ):
            raise CapturedPaperOutboxConflictError(
                "fill_handoff_prior_completion_binding_mismatch"
            )
    if not (indeterminate_source or completed_source):
        raise CapturedPaperOutboxConflictError(
            "fill_handoff_outbox_state_mismatch"
        )

    committed_at = _db_now(db)
    receipt = {
        "schema_version": CAPTURED_PAPER_FILL_HANDOFF_RECEIPT_SCHEMA_VERSION,
        "completion_sha256": digest,
        "transport_authority_sha256": authority.authority_sha256,
        "account_scope": authority.account_scope,
        "expected_account_id": authority.expected_account_id,
        "client_order_id": authority.client_order_id,
        "reservation_id": authority.reservation_id,
        "publication_kind": proof.publication_kind,
        "broker_order_id": proof.broker_order_id,
        "observation_sha256": proof.observation_sha256,
        "durability_kind": proof.durability_kind,
        "source_record_table": proof.source_record_table,
        "source_record_id": proof.source_record_id,
        "terminal_evidence_sha256": proof.terminal_evidence_sha256,
        "immutable_fill_identity_sha256": (
            proof.immutable_fill_identity_sha256
        ),
        "cumulative_filled_quantity_shares": (
            proof.cumulative_filled_quantity_shares
        ),
        "lifecycle_event_sha256": proof.lifecycle_event_sha256,
        "resulting_reservation_state": proof.resulting_reservation_state,
        "prior_completion_proof_sha256": record.completion_proof_sha256,
        "prior_completed_at": (
            record.completed_at.isoformat()
            if record.completed_at is not None
            else None
        ),
        "fill_handoff_proof_sha256": proof.proof_sha256,
        "committed_at": committed_at.isoformat(),
    }
    receipt_json = _canonical_json(receipt)
    receipt_sha256 = _sha256_text(receipt_json)
    result = db.execute(
        text(
            """
            UPDATE captured_paper_post_commit_outbox
               SET status = 'fill_handoff_committed',
                   reconciliation_next_attempt_at = NULL,
                   fill_handoff_proof_canonical_json = :proof_json,
                   fill_handoff_proof_sha256 = :proof_sha256,
                   fill_handoff_receipt_canonical_json = :receipt_json,
                   fill_handoff_receipt_sha256 = :receipt_sha256,
                   fill_handoff_committed_at = :committed_at,
                   updated_at = clock_timestamp(), version = version + 1
             WHERE completion_sha256 = :completion_sha256
               AND version = :version
               AND status = :source_status
               AND lease_token IS NULL AND lease_owner_id IS NULL
               AND lease_expires_at IS NULL
               AND transport_evidence_sha256 = :authority_sha256
               AND (
                    (
                        :source_status = 'transport_indeterminate'
                        AND transport_indeterminate_at IS NOT NULL
                        AND indeterminate_evidence_sha256 IS NOT NULL
                        AND completion_proof_sha256 IS NULL
                        AND completed_at IS NULL
                    ) OR (
                        :source_status = 'completed'
                        AND completion_proof_sha256 = :completion_proof_sha256
                        AND completed_at = :completed_at
                    )
               )
            """
        ),
        {
            "completion_sha256": digest,
            "version": record.version,
            "authority_sha256": authority.authority_sha256,
            "source_status": record.status,
            "completion_proof_sha256": record.completion_proof_sha256,
            "completed_at": record.completed_at,
            "proof_json": proof_json,
            "proof_sha256": proof.proof_sha256,
            "receipt_json": receipt_json,
            "receipt_sha256": receipt_sha256,
            "committed_at": committed_at,
        },
    )
    if int(result.rowcount or 0) != 1:
        raise CapturedPaperOutboxConflictError(
            "fill_handoff_commit_cas_failed"
        )
    if completed_source:
        _mark_completed_fill_watch_handoff(
            db,
            completion_sha256=digest,
            broker_order_id=proof.broker_order_id,
            observation_sha256=proof.observation_sha256,
            terminal_receipt_sha256=receipt_sha256,
        )
    changed = _select_row(db, completion_sha256=digest, for_update=True)
    assert changed is not None
    _append_event(
        db,
        row=changed,
        event_type="fill_handoff_committed",
        event_payload={
            "client_order_id": authority.client_order_id,
            "broker_order_id": proof.broker_order_id,
            "reservation_id": authority.reservation_id,
            "publication_kind": proof.publication_kind,
            "terminal_evidence_sha256": proof.terminal_evidence_sha256,
            "fill_handoff_proof_sha256": proof.proof_sha256,
            "fill_handoff_receipt_sha256": receipt_sha256,
            "prior_completion_proof_sha256": (
                record.completion_proof_sha256
            ),
            "prior_completion_event_type": prior_completion_event_type,
        },
    )
    return load_captured_paper_outbox(
        db, completion_sha256=digest, for_update=True
    )


@_atomic_mutation
def persist_captured_paper_post_commit_request(
    db: Any,
    *,
    request: CapturedPaperPostCommitRequest,
    authority: CapturedPaperTransportAuthority,
    order_request: Mapping[str, Any],
    order_request_sha256: str,
    admission_record: Mapping[str, Any],
    admission_record_sha256: str,
    quantity_shares: int,
    structural_risk_usd: Any,
    gross_notional_usd: Any,
    buying_power_impact_usd: Any,
    operational_policy_sha256: str,
    committed_at: datetime,
    lock_order: tuple[str, ...],
    reconciliation_retry_delay_seconds: int,
    reconciliation_health_escalation_delay_seconds: int,
    max_attempts: int,
    max_reconciliation_attempts: int,
) -> CapturedPaperOutboxRecord:
    """Persist in the caller-owned phase-one transaction; never commits."""

    if type(request) is not CapturedPaperPostCommitRequest:
        raise CapturedPaperOutboxError("outbox_request_type_invalid")
    try:
        request.verify()
    except CapturedPaperIntentContractError as exc:
        raise CapturedPaperOutboxError(f"outbox_request_{exc.reason}") from exc
    max_attempts = _bounded_positive_int(
        max_attempts,
        field_name="outbox_max_attempts",
        maximum=_MAX_ATTEMPT_BOUND,
    )
    max_reconciliation_attempts = _bounded_positive_int(
        max_reconciliation_attempts,
        field_name="outbox_max_reconciliation_attempts",
        maximum=_MAX_ATTEMPT_BOUND,
    )
    canonical_json = request.to_canonical_json()
    payload_sha256 = _sha256_text(canonical_json)
    intent = request.intent
    route = intent.route_token
    opportunity_sha256 = (
        intent.opportunity_key.opportunity_key_sha256
        if intent.opportunity_key is not None
        else None
    )
    artifacts = build_captured_paper_durable_transport_artifacts(
        request=request,
        authority=authority,
        order_request=order_request,
        order_request_sha256=order_request_sha256,
        admission_record=admission_record,
        admission_record_sha256=admission_record_sha256,
        quantity_shares=quantity_shares,
        structural_risk_usd=structural_risk_usd,
        gross_notional_usd=gross_notional_usd,
        buying_power_impact_usd=buying_power_impact_usd,
        operational_policy_sha256=operational_policy_sha256,
        committed_at=committed_at,
        lock_order=lock_order,
        reconciliation_retry_delay_seconds=(
            reconciliation_retry_delay_seconds
        ),
        reconciliation_health_escalation_delay_seconds=(
            reconciliation_health_escalation_delay_seconds
        ),
    )
    inserted = db.execute(
        text(
            """
            INSERT INTO captured_paper_post_commit_outbox (
                completion_sha256, payload_sha256, route_token_sha256,
                intent_sha256, payload_canonical_json, account_scope,
                expected_account_id, session_id, symbol, decision_id,
                client_order_id, binder_id, symbol_claim_token,
                confirmed_arm_generation_sha256, opportunity_key_sha256,
                order_request_canonical_json, order_request_sha256,
                transport_authority_canonical_json,
                transport_authority_sha256,
                admission_record_canonical_json, admission_record_sha256,
                committed_admission_canonical_json,
                committed_admission_sha256,
                transport_instruction_canonical_json,
                transport_instruction_sha256,
                reconciliation_retry_delay_seconds,
                reconciliation_health_escalation_delay_seconds,
                status, max_attempts, max_reconciliation_attempts
            ) VALUES (
                :completion_sha256, :payload_sha256, :route_token_sha256,
                :intent_sha256, :payload_canonical_json, :account_scope,
                CAST(:expected_account_id AS UUID), :session_id, :symbol,
                :decision_id, :client_order_id, CAST(:binder_id AS UUID),
                :symbol_claim_token, :confirmed_arm_generation_sha256,
                :opportunity_key_sha256, :order_request_canonical_json,
                :order_request_sha256,
                :transport_authority_canonical_json,
                :transport_authority_sha256,
                :admission_record_canonical_json,
                :admission_record_sha256,
                :committed_admission_canonical_json,
                :committed_admission_sha256,
                :transport_instruction_canonical_json,
                :transport_instruction_sha256,
                :reconciliation_retry_delay_seconds,
                :reconciliation_health_escalation_delay_seconds,
                'pending', :max_attempts,
                :max_reconciliation_attempts
            )
            ON CONFLICT DO NOTHING
            RETURNING completion_sha256
            """
        ),
        {
            "completion_sha256": request.completion_sha256,
            "payload_sha256": payload_sha256,
            "route_token_sha256": route.route_token_sha256,
            "intent_sha256": intent.intent_sha256,
            "payload_canonical_json": canonical_json,
            "account_scope": route.account_scope,
            "expected_account_id": route.expected_account_id,
            "session_id": route.session_id,
            "symbol": route.symbol,
            "decision_id": intent.decision_id,
            "client_order_id": intent.client_order_id,
            "binder_id": intent.binder_id,
            "symbol_claim_token": intent.symbol_claim_token,
            "confirmed_arm_generation_sha256": (
                intent.confirmed_arm_generation.confirmed_arm_generation_sha256
            ),
            "opportunity_key_sha256": opportunity_sha256,
            **artifacts,
            "max_attempts": max_attempts,
            "max_reconciliation_attempts": max_reconciliation_attempts,
        },
    ).scalar_one_or_none()
    conflicts = db.execute(
        text(
            f"""
            SELECT {_ROW_COLUMNS}
              FROM captured_paper_post_commit_outbox
             WHERE completion_sha256 = :completion_sha256
                OR (account_scope = :account_scope
                    AND client_order_id = :client_order_id)
             FOR UPDATE
            """
        ),
        {
            "completion_sha256": request.completion_sha256,
            "account_scope": route.account_scope,
            "client_order_id": intent.client_order_id,
        },
    ).mappings().all()
    if len(conflicts) != 1:
        raise CapturedPaperOutboxConflictError(
            "outbox_identity_conflict"
        )
    row = conflicts[0]
    immutable_match = (
        row["completion_sha256"] == request.completion_sha256
        and row["payload_sha256"] == payload_sha256
        and row["payload_canonical_json"] == canonical_json
        and all(row[name] == value for name, value in artifacts.items())
        and int(row["max_attempts"]) == max_attempts
        and int(row["max_reconciliation_attempts"])
        == max_reconciliation_attempts
    )
    if not immutable_match:
        raise CapturedPaperOutboxConflictError(
            "outbox_same_id_different_bytes"
        )
    if inserted is not None:
        _append_event(
            db,
            row=row,
            event_type="enqueued",
            event_payload={
                "schema_version": OUTBOX_PAYLOAD_SCHEMA_VERSION,
                "completion_sha256": request.completion_sha256,
                "payload_sha256": payload_sha256,
                "route_token_sha256": route.route_token_sha256,
                "intent_sha256": intent.intent_sha256,
                "order_request_sha256": artifacts["order_request_sha256"],
                "transport_authority_sha256": artifacts[
                    "transport_authority_sha256"
                ],
                "admission_record_sha256": artifacts[
                    "admission_record_sha256"
                ],
                "committed_admission_sha256": artifacts[
                    "committed_admission_sha256"
                ],
                "transport_instruction_sha256": artifacts[
                    "transport_instruction_sha256"
                ],
            },
        )
    return load_captured_paper_outbox(
        db,
        completion_sha256=request.completion_sha256,
        for_update=True,
    )


def _exact_lease_match(
    record: CapturedPaperOutboxRecord,
    *,
    lease_token: str,
    lease_owner_id: str,
) -> tuple[str, str]:
    token = _canonical_uuid(lease_token, field_name="outbox_lease_token")
    owner = _canonical_uuid(
        lease_owner_id, field_name="outbox_lease_owner_id"
    )
    if record.lease_token != token or record.lease_owner_id != owner:
        raise CapturedPaperOutboxLeaseError("outbox_lease_owner_mismatch")
    return token, owner


@_atomic_mutation
def lease_captured_paper_completion(
    db: Any,
    *,
    completion_sha256: str,
    lease_owner_id: str,
    lease_seconds: int,
    lease_token: str | None = None,
) -> CapturedPaperOutboxLease | None:
    completion_sha256 = _digest(
        completion_sha256, field_name="completion_sha256"
    )
    owner = _canonical_uuid(
        lease_owner_id, field_name="outbox_lease_owner_id"
    )
    token = _canonical_uuid(
        lease_token or str(uuid.uuid4()), field_name="outbox_lease_token"
    )
    lease_seconds = _bounded_positive_int(
        lease_seconds,
        field_name="outbox_lease_seconds",
        maximum=_MAX_LEASE_SECONDS,
    )
    row = _select_row(
        db,
        completion_sha256=completion_sha256,
        for_update=True,
    )
    if row is None:
        raise CapturedPaperOutboxNotFoundError(
            "captured_paper_outbox_not_found"
        )
    record = _record_from_row(db, row)
    now = _db_now(db)
    due = (
        record.status == OUTBOX_STATUS_PENDING
        or (
            record.status == OUTBOX_STATUS_RETRY_WAIT
            and record.next_attempt_at is not None
            and record.next_attempt_at <= now
        )
        or (
            record.status == OUTBOX_STATUS_LEASED
            and record.lease_expires_at is not None
            and record.lease_expires_at <= now
        )
    )
    if not due:
        return None
    recovered = record.status == OUTBOX_STATUS_LEASED
    if record.attempt_count >= record.max_attempts:
        result = db.execute(
            text(
                """
                UPDATE captured_paper_post_commit_outbox
                   SET status = 'retry_exhausted', lease_token = NULL,
                       lease_owner_id = NULL, lease_expires_at = NULL,
                       next_attempt_at = NULL, updated_at = clock_timestamp(),
                       version = version + 1
                 WHERE completion_sha256 = :completion_sha256
                   AND version = :version
                   AND status IN ('pending', 'retry_wait', 'leased')
                """
            ),
            {"completion_sha256": completion_sha256, "version": record.version},
        )
        if result.rowcount != 1:
            raise CapturedPaperOutboxConflictError(
                "outbox_retry_exhaustion_cas_failed"
            )
        changed = _select_row(
            db,
            completion_sha256=completion_sha256,
            for_update=True,
        )
        assert changed is not None
        _append_event(
            db,
            row=changed,
            event_type="retry_exhausted",
            event_payload={
                "attempt_count": record.attempt_count,
                "max_attempts": record.max_attempts,
                "recovered_expired_lease": recovered,
            },
        )
        return None
    result = db.execute(
        text(
            """
            UPDATE captured_paper_post_commit_outbox
               SET status = 'leased', attempt_count = attempt_count + 1,
                   lease_token = CAST(:lease_token AS UUID),
                   lease_owner_id = CAST(:lease_owner_id AS UUID),
                   lease_expires_at = clock_timestamp()
                       + make_interval(secs => :lease_seconds),
                   next_attempt_at = NULL, updated_at = clock_timestamp(),
                   version = version + 1
             WHERE completion_sha256 = :completion_sha256
               AND version = :version
               AND status = :status
            RETURNING lease_expires_at
            """
        ),
        {
            "completion_sha256": completion_sha256,
            "version": record.version,
            "status": record.status,
            "lease_token": token,
            "lease_owner_id": owner,
            "lease_seconds": lease_seconds,
        },
    ).scalar_one_or_none()
    if result is None:
        raise CapturedPaperOutboxConflictError("outbox_lease_cas_failed")
    changed = _select_row(
        db,
        completion_sha256=completion_sha256,
        for_update=True,
    )
    assert changed is not None
    _append_event(
        db,
        row=changed,
        event_type="lease_recovered" if recovered else "leased",
        event_payload={
            "attempt_count": int(changed["attempt_count"]),
            "lease_owner_id": owner,
            "lease_token": token,
            "recovered_expired_lease": recovered,
        },
    )
    leased = load_captured_paper_outbox(
        db, completion_sha256=completion_sha256, for_update=True
    )
    assert leased.lease_expires_at is not None
    return CapturedPaperOutboxLease(
        record=leased,
        lease_token=token,
        lease_owner_id=owner,
        lease_expires_at=leased.lease_expires_at,
        recovered=recovered,
        reconciliation_only=False,
    )


@_atomic_mutation
def lease_next_captured_paper_completion(
    db: Any,
    *,
    lease_owner_id: str,
    lease_seconds: int,
) -> CapturedPaperOutboxLease | None:
    """Claim the oldest due completion with ``SKIP LOCKED`` recovery safety."""

    # Validate before taking a row lock.  The actual lease function validates
    # again and performs the version/token CAS against the selected row.
    _canonical_uuid(lease_owner_id, field_name="outbox_lease_owner_id")
    _bounded_positive_int(
        lease_seconds,
        field_name="outbox_lease_seconds",
        maximum=_MAX_LEASE_SECONDS,
    )
    completion_sha256 = db.execute(
        text(
            """
            SELECT completion_sha256
              FROM captured_paper_post_commit_outbox
             WHERE status = 'pending'
                OR (status = 'retry_wait'
                    AND next_attempt_at <= clock_timestamp())
                OR (status = 'leased'
                    AND lease_expires_at <= clock_timestamp())
             ORDER BY COALESCE(
                        next_attempt_at,
                        lease_expires_at,
                        created_at
                      ), completion_sha256
             FOR UPDATE SKIP LOCKED
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if completion_sha256 is None:
        return None
    return lease_captured_paper_completion(
        db,
        completion_sha256=str(completion_sha256),
        lease_owner_id=lease_owner_id,
        lease_seconds=lease_seconds,
    )


def find_next_due_captured_paper_completion(db: Any) -> str | None:
    """Read the oldest initial/retry candidate without acquiring ownership."""

    value = db.execute(
        text(
            """
            SELECT completion_sha256
              FROM captured_paper_post_commit_outbox
             WHERE status = 'pending'
                OR (status = 'retry_wait'
                    AND next_attempt_at <= clock_timestamp())
                OR (status = 'leased'
                    AND lease_expires_at <= clock_timestamp())
             ORDER BY COALESCE(
                        next_attempt_at,
                        lease_expires_at,
                        created_at
                      ), completion_sha256
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    return None if value is None else str(value)


@_atomic_mutation
def mark_captured_paper_transport_started(
    db: Any,
    *,
    completion_sha256: str,
    lease_token: str,
    lease_owner_id: str,
    authority: CapturedPaperTransportAuthority,
) -> CapturedPaperOutboxRecord:
    """Atomically consume the exact PAPER POST permission and outbox lease.

    The durable action claim is still ``claimed`` when this function begins.
    Canonical account/risk rows are locked before the outbox row; only after the
    exact lease has also been locked and validated do we move the action claim
    and outbox to their one-way transport-start states.  The caller owns the
    outer transaction.  The nested transaction installed by
    :func:`_atomic_mutation` guarantees that either both CAS operations and the
    append-only event survive, or neither does.

    No caller clock is accepted.  The marker and outbox transition use the same
    database-clock instant, and this function never commits or performs broker
    I/O.
    """

    completion_sha256 = _digest(
        completion_sha256, field_name="completion_sha256"
    )
    token = _canonical_uuid(lease_token, field_name="outbox_lease_token")
    owner = _canonical_uuid(lease_owner_id, field_name="outbox_lease_owner_id")
    request = _load_request_unlocked(
        db, completion_sha256=completion_sha256
    )
    _verify_durable_transport_binding(
        db,
        request=request,
        authority=authority,
        pre_transport_start=True,
    )
    record = load_captured_paper_outbox(
        db, completion_sha256=completion_sha256, for_update=True
    )
    token, owner = _exact_lease_match(
        record, lease_token=token, lease_owner_id=owner
    )
    evidence = authority.authority_sha256
    if record.status != OUTBOX_STATUS_LEASED:
        raise CapturedPaperOutboxLeaseError(
            "outbox_transport_start_phase_invalid"
        )
    started_at = _db_now(db)
    if record.lease_expires_at is None or record.lease_expires_at <= started_at:
        raise CapturedPaperOutboxLeaseError(
            "outbox_transport_start_lease_expired"
        )

    intent = request.intent
    route = intent.route_token
    marker = _canonical_json(
        {
            "entry_transport_started": {
                "client_order_id": intent.client_order_id,
                "post_bind_token": intent.binder_id,
                "started_at_utc": started_at.isoformat(),
                "completion_sha256": request.completion_sha256,
                "transport_authority_sha256": evidence,
                "outbox_lease_token": token,
                "outbox_lease_owner_id": owner,
            }
        }
    )
    action_result = db.execute(
        text(
            """
            UPDATE broker_symbol_action_claims
               SET phase = 'submit_indeterminate',
                   metadata_json = metadata_json || CAST(:marker AS jsonb),
                   updated_at = :started_at
             WHERE account_scope = :account_scope AND symbol = :symbol
               AND claim_token = :claim_token AND action = 'entry'
               AND phase = 'claimed' AND broker_order_id IS NULL
               AND owner_session_id = :owner_session_id
               AND client_order_id = :client_order_id
               AND COALESCE(metadata_json->>'alpaca_account_id', '')
                   = :expected_account_id
               AND COALESCE(metadata_json->>'entry_post_bind_token', '')
                   = :binder_id
               AND lease_expires_at > :started_at
               AND NOT (metadata_json ? 'entry_transport_started')
               AND NOT (metadata_json ? 'owner_transport')
            """
        ),
        {
            "marker": marker,
            "started_at": started_at,
            "account_scope": route.account_scope,
            "symbol": route.symbol,
            "claim_token": intent.symbol_claim_token,
            "owner_session_id": route.session_id,
            "client_order_id": intent.client_order_id,
            "expected_account_id": route.expected_account_id,
            "binder_id": intent.binder_id,
        },
    )
    if action_result.rowcount != 1:
        raise CapturedPaperOutboxConflictError(
            "transport_action_claim_start_cas_failed"
        )

    result = db.execute(
        text(
            """
            UPDATE captured_paper_post_commit_outbox
               SET status = 'transport_started',
                   transport_started_at = :started_at,
                   transport_evidence_sha256 = :transport_evidence_sha256,
                   updated_at = :started_at, version = version + 1
             WHERE completion_sha256 = :completion_sha256
               AND version = :version AND status = 'leased'
               AND lease_token = CAST(:lease_token AS UUID)
               AND lease_owner_id = CAST(:lease_owner_id AS UUID)
               AND lease_expires_at > :started_at
            """
        ),
        {
            "completion_sha256": record.completion_sha256,
            "version": record.version,
            "lease_token": token,
            "lease_owner_id": owner,
            "transport_evidence_sha256": evidence,
            "started_at": started_at,
        },
    )
    if result.rowcount != 1:
        raise CapturedPaperOutboxConflictError(
            "outbox_transport_start_cas_failed"
        )
    changed = _select_row(
        db,
        completion_sha256=record.completion_sha256,
        for_update=True,
    )
    assert changed is not None
    _append_event(
        db,
        row=changed,
        event_type="transport_started",
        event_payload={
            "client_order_id": record.request.intent.client_order_id,
            "reservation_id": authority.reservation_id,
            "transport_evidence_sha256": evidence,
        },
    )
    return load_captured_paper_outbox(
        db, completion_sha256=record.completion_sha256, for_update=True
    )


@_atomic_mutation
def mark_captured_paper_retryable(
    db: Any,
    *,
    completion_sha256: str,
    lease_token: str,
    lease_owner_id: str,
    failure_sha256: str,
    retry_delay_seconds: int,
) -> CapturedPaperOutboxRecord:
    record = load_captured_paper_outbox(
        db, completion_sha256=completion_sha256, for_update=True
    )
    token, owner = _exact_lease_match(
        record, lease_token=lease_token, lease_owner_id=lease_owner_id
    )
    if record.status != OUTBOX_STATUS_LEASED:
        raise CapturedPaperOutboxLeaseError(
            "outbox_retry_after_transport_prohibited"
        )
    if record.lease_expires_at is None or record.lease_expires_at <= _db_now(db):
        raise CapturedPaperOutboxLeaseError("outbox_retry_lease_expired")
    failure = _digest(failure_sha256, field_name="failure_sha256")
    delay = _bounded_nonnegative_int(
        retry_delay_seconds,
        field_name="retry_delay_seconds",
        maximum=_MAX_RETRY_DELAY_SECONDS,
    )
    exhausted = record.attempt_count >= record.max_attempts
    status = (
        OUTBOX_STATUS_RETRY_EXHAUSTED
        if exhausted
        else OUTBOX_STATUS_RETRY_WAIT
    )
    result = db.execute(
        text(
            """
            UPDATE captured_paper_post_commit_outbox
               SET status = :new_status, lease_token = NULL,
                   lease_owner_id = NULL, lease_expires_at = NULL,
                   next_attempt_at = CASE WHEN :new_status = 'retry_wait'
                       THEN clock_timestamp() + make_interval(secs => :delay)
                       ELSE NULL END,
                   last_failure_sha256 = :failure_sha256,
                   updated_at = clock_timestamp(), version = version + 1
             WHERE completion_sha256 = :completion_sha256
               AND version = :version AND status = 'leased'
               AND lease_token = CAST(:lease_token AS UUID)
               AND lease_owner_id = CAST(:lease_owner_id AS UUID)
               AND lease_expires_at > clock_timestamp()
            """
        ),
        {
            "new_status": status,
            "delay": delay,
            "failure_sha256": failure,
            "completion_sha256": record.completion_sha256,
            "version": record.version,
            "lease_token": token,
            "lease_owner_id": owner,
        },
    )
    if result.rowcount != 1:
        raise CapturedPaperOutboxConflictError("outbox_retry_cas_failed")
    changed = _select_row(
        db,
        completion_sha256=record.completion_sha256,
        for_update=True,
    )
    assert changed is not None
    _append_event(
        db,
        row=changed,
        event_type="retry_exhausted" if exhausted else "retry_scheduled",
        event_payload={
            "attempt_count": record.attempt_count,
            "failure_sha256": failure,
            "retry_delay_seconds": delay,
        },
    )
    return load_captured_paper_outbox(
        db, completion_sha256=record.completion_sha256, for_update=True
    )


@_atomic_mutation
def mark_captured_paper_transport_indeterminate(
    db: Any,
    *,
    completion_sha256: str,
    lease_token: str,
    lease_owner_id: str,
    indeterminate_evidence_sha256: str,
) -> CapturedPaperOutboxRecord:
    record = load_captured_paper_outbox(
        db, completion_sha256=completion_sha256, for_update=True
    )
    token, owner = _exact_lease_match(
        record, lease_token=lease_token, lease_owner_id=lease_owner_id
    )
    if record.status != OUTBOX_STATUS_TRANSPORT_STARTED:
        raise CapturedPaperOutboxLeaseError(
            "outbox_indeterminate_requires_transport_marker"
        )
    evidence = _digest(
        indeterminate_evidence_sha256,
        field_name="indeterminate_evidence_sha256",
    )
    result = db.execute(
        text(
            """
            UPDATE captured_paper_post_commit_outbox
               SET status = 'transport_indeterminate',
                   lease_token = NULL, lease_owner_id = NULL,
                   lease_expires_at = NULL,
                   transport_indeterminate_at = clock_timestamp(),
                   indeterminate_evidence_sha256 = :evidence,
                   updated_at = clock_timestamp(), version = version + 1
             WHERE completion_sha256 = :completion_sha256
               AND version = :version AND status = 'transport_started'
               AND lease_token = CAST(:lease_token AS UUID)
               AND lease_owner_id = CAST(:lease_owner_id AS UUID)
            """
        ),
        {
            "completion_sha256": record.completion_sha256,
            "version": record.version,
            "lease_token": token,
            "lease_owner_id": owner,
            "evidence": evidence,
        },
    )
    if result.rowcount != 1:
        raise CapturedPaperOutboxConflictError(
            "outbox_indeterminate_cas_failed"
        )
    changed = _select_row(
        db,
        completion_sha256=record.completion_sha256,
        for_update=True,
    )
    assert changed is not None
    _append_event(
        db,
        row=changed,
        event_type="transport_indeterminate",
        event_payload={
            "client_order_id": record.request.intent.client_order_id,
            "indeterminate_evidence_sha256": evidence,
        },
    )
    return load_captured_paper_outbox(
        db, completion_sha256=record.completion_sha256, for_update=True
    )


@_atomic_mutation
def mark_captured_paper_completion_accepted(
    db: Any,
    *,
    completion_sha256: str,
    lease_token: str,
    lease_owner_id: str,
    authority: CapturedPaperTransportAuthority,
    acceptance: CapturedPaperBrokerAcceptanceProof,
) -> CapturedPaperOutboxRecord:
    """Persist a positive direct POST result already bound in canonical rows."""

    completion_sha256 = _digest(
        completion_sha256, field_name="completion_sha256"
    )
    token = _canonical_uuid(lease_token, field_name="outbox_lease_token")
    owner = _canonical_uuid(lease_owner_id, field_name="outbox_lease_owner_id")
    request = _load_request_unlocked(
        db, completion_sha256=completion_sha256
    )
    if type(acceptance) is not CapturedPaperBrokerAcceptanceProof:
        raise CapturedPaperOutboxError("broker_acceptance_type_invalid")
    acceptance.verify_for_authority(authority, expected_kind="post_response")
    binding = _verify_durable_transport_binding(
        db,
        request=request,
        authority=authority,
        acceptance=acceptance,
    )
    record = load_captured_paper_outbox(
        db, completion_sha256=completion_sha256, for_update=True
    )
    token, owner = _exact_lease_match(
        record, lease_token=token, lease_owner_id=owner
    )
    if record.status != OUTBOX_STATUS_TRANSPORT_STARTED:
        raise CapturedPaperOutboxLeaseError(
            "outbox_completion_phase_invalid"
        )
    if record.transport_evidence_sha256 != authority.authority_sha256:
        raise CapturedPaperOutboxConflictError(
            "outbox_transport_authority_mismatch"
        )
    financial_payload_json = db.execute(
        text(
            """
            SELECT event_payload_canonical_json
              FROM captured_paper_post_commit_outbox_events
             WHERE completion_sha256 = :completion_sha256
               AND event_type = 'transport_financial_breaker_recorded'
             ORDER BY sequence DESC
             LIMIT 1
            """
        ),
        {"completion_sha256": record.completion_sha256},
    ).scalar_one_or_none()
    if financial_payload_json is None:
        raise CapturedPaperOutboxError(
            "outbox_completion_financial_breaker_missing"
        )
    try:
        financial_receipt = (
            CapturedPaperFinancialBreakerReceipt.from_payload(
                json.loads(str(financial_payload_json))
            )
        )
        financial_receipt.verify_for_request(
            request,
            phase="pre_post",
            now=financial_receipt.issued_at,
            require_allowed=True,
            transport_instruction_sha256=(
                record.durable_transport.transport_instruction_sha256
            ),
            transport_invocation_authority_sha256=(
                financial_receipt.transport_invocation_authority_sha256
            ),
        )
    except (CapturedPaperFinancialBreakerError, TypeError, ValueError) as exc:
        raise CapturedPaperOutboxCorruptionError(
            "outbox_completion_financial_breaker_invalid"
        ) from exc
    proof = acceptance.acceptance_sha256
    result = db.execute(
        text(
            """
            UPDATE captured_paper_post_commit_outbox
               SET status = 'completed', lease_token = NULL,
                   lease_owner_id = NULL, lease_expires_at = NULL,
                   completion_proof_sha256 = :proof,
                   completed_at = clock_timestamp(),
                   updated_at = clock_timestamp(), version = version + 1
             WHERE completion_sha256 = :completion_sha256
               AND version = :version AND status = 'transport_started'
               AND lease_token = CAST(:lease_token AS UUID)
               AND lease_owner_id = CAST(:lease_owner_id AS UUID)
            """
        ),
        {
            "completion_sha256": record.completion_sha256,
            "version": record.version,
            "lease_token": token,
            "lease_owner_id": owner,
            "proof": proof,
        },
    )
    if result.rowcount != 1:
        raise CapturedPaperOutboxConflictError(
            "outbox_completion_cas_failed"
        )
    changed = _select_row(
        db,
        completion_sha256=record.completion_sha256,
        for_update=True,
    )
    assert changed is not None
    _append_event(
        db,
        row=changed,
        event_type="completion_accepted",
        event_payload={
            "client_order_id": record.request.intent.client_order_id,
            "broker_order_id": acceptance.broker_order_id,
            "reservation_id": authority.reservation_id,
            "completion_proof_sha256": proof,
        },
    )
    _ensure_completed_fill_watch(
        db,
        request=request,
        authority=authority,
        acceptance=acceptance,
        binding=binding,
        completion_event_type="completion_accepted",
    )
    return load_captured_paper_outbox(
        db, completion_sha256=record.completion_sha256, for_update=True
    )


@_atomic_mutation
def lease_captured_paper_indeterminate_reconciliation(
    db: Any,
    *,
    completion_sha256: str,
    lease_owner_id: str,
    lease_seconds: int,
    lease_token: str | None = None,
) -> CapturedPaperOutboxLease | None:
    """Lease only same-CID reconciliation; never generic completion work."""

    owner = _canonical_uuid(
        lease_owner_id, field_name="outbox_lease_owner_id"
    )
    token = _canonical_uuid(
        lease_token or str(uuid.uuid4()), field_name="outbox_lease_token"
    )
    lease_seconds = _bounded_positive_int(
        lease_seconds,
        field_name="outbox_lease_seconds",
        maximum=_MAX_LEASE_SECONDS,
    )
    record = load_captured_paper_outbox(
        db, completion_sha256=completion_sha256, for_update=True
    )
    if record.status != OUTBOX_STATUS_TRANSPORT_INDETERMINATE:
        return None
    now = _db_now(db)
    if (
        record.reconciliation_next_attempt_at is not None
        and record.reconciliation_next_attempt_at > now
    ):
        return None
    if (
        record.reconciliation_attempt_count
        >= record.max_reconciliation_attempts
    ):
        escalation_delay = (
            record.durable_transport
            .reconciliation_health_escalation_delay_seconds
        )
        result = db.execute(
            text(
                """
                UPDATE captured_paper_post_commit_outbox
                   SET reconciliation_attempt_count = 0,
                       reconciliation_health_state = 'escalated',
                       reconciliation_escalation_count =
                           reconciliation_escalation_count + 1,
                       last_reconciliation_health_escalated_at = :now,
                       reconciliation_next_attempt_at = :now
                           + make_interval(secs => :delay),
                       updated_at = :now, version = version + 1
                 WHERE completion_sha256 = :completion_sha256
                   AND version = :version
                   AND status = 'transport_indeterminate'
                """
            ),
            {
                "completion_sha256": record.completion_sha256,
                "version": record.version,
                "now": now,
                "delay": escalation_delay,
            },
        )
        if result.rowcount != 1:
            raise CapturedPaperOutboxConflictError(
                "outbox_reconciliation_health_escalation_cas_failed"
            )
        changed = _select_row(
            db,
            completion_sha256=record.completion_sha256,
            for_update=True,
        )
        assert changed is not None
        _append_event(
            db,
            row=changed,
            event_type="reconciliation_health_backoff",
            event_payload={
                "client_order_id": record.request.intent.client_order_id,
                "reconciliation_total_attempt_count": (
                    record.reconciliation_total_attempt_count
                ),
                "reconciliation_escalation_count": (
                    record.reconciliation_escalation_count + 1
                ),
                "retry_delay_seconds": escalation_delay,
            },
        )
        return None
    result = db.execute(
        text(
            """
            UPDATE captured_paper_post_commit_outbox
               SET status = 'reconciling',
                   reconciliation_attempt_count =
                       reconciliation_attempt_count + 1,
                   reconciliation_total_attempt_count =
                       reconciliation_total_attempt_count + 1,
                   lease_token = CAST(:lease_token AS UUID),
                   lease_owner_id = CAST(:lease_owner_id AS UUID),
                   lease_expires_at = clock_timestamp()
                       + make_interval(secs => :lease_seconds),
                   reconciliation_next_attempt_at = NULL,
                   updated_at = clock_timestamp(), version = version + 1
             WHERE completion_sha256 = :completion_sha256
               AND version = :version
               AND status = 'transport_indeterminate'
            """
        ),
        {
            "completion_sha256": record.completion_sha256,
            "version": record.version,
            "lease_token": token,
            "lease_owner_id": owner,
            "lease_seconds": lease_seconds,
        },
    )
    if result.rowcount != 1:
        raise CapturedPaperOutboxConflictError(
            "outbox_reconciliation_lease_cas_failed"
        )
    changed = _select_row(
        db,
        completion_sha256=record.completion_sha256,
        for_update=True,
    )
    assert changed is not None
    _append_event(
        db,
        row=changed,
        event_type="reconciliation_leased",
        event_payload={
            "client_order_id": record.request.intent.client_order_id,
            "lease_owner_id": owner,
            "lease_token": token,
            "reconciliation_attempt_count": int(
                changed["reconciliation_attempt_count"]
            ),
            "reconciliation_total_attempt_count": int(
                changed["reconciliation_total_attempt_count"]
            ),
        },
    )
    leased = load_captured_paper_outbox(
        db, completion_sha256=record.completion_sha256, for_update=True
    )
    assert leased.lease_expires_at is not None
    return CapturedPaperOutboxLease(
        record=leased,
        lease_token=token,
        lease_owner_id=owner,
        lease_expires_at=leased.lease_expires_at,
        recovered=False,
        reconciliation_only=True,
    )


@_atomic_mutation
def lease_next_captured_paper_indeterminate_reconciliation(
    db: Any,
    *,
    lease_owner_id: str,
    lease_seconds: int,
) -> CapturedPaperOutboxLease | None:
    """Discover and lease the oldest due same-CID-only restart item."""

    _canonical_uuid(lease_owner_id, field_name="outbox_lease_owner_id")
    _bounded_positive_int(
        lease_seconds,
        field_name="outbox_lease_seconds",
        maximum=_MAX_LEASE_SECONDS,
    )
    completion_sha256 = db.execute(
        text(
            """
            SELECT completion_sha256
              FROM captured_paper_post_commit_outbox
             WHERE status = 'transport_indeterminate'
               AND (reconciliation_next_attempt_at IS NULL
                    OR reconciliation_next_attempt_at <= clock_timestamp())
             ORDER BY COALESCE(
                        reconciliation_next_attempt_at,
                        transport_indeterminate_at,
                        created_at
                      ), completion_sha256
             FOR UPDATE SKIP LOCKED
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if completion_sha256 is None:
        return None
    return lease_captured_paper_indeterminate_reconciliation(
        db,
        completion_sha256=str(completion_sha256),
        lease_owner_id=lease_owner_id,
        lease_seconds=lease_seconds,
    )


def find_next_due_captured_paper_reconciliation(db: Any) -> str | None:
    """Read the oldest due lookup-only item; never changes POST authority."""

    value = db.execute(
        text(
            """
            SELECT completion_sha256
              FROM captured_paper_post_commit_outbox
             WHERE status = 'transport_indeterminate'
               AND (reconciliation_next_attempt_at IS NULL
                    OR reconciliation_next_attempt_at <= clock_timestamp())
             ORDER BY COALESCE(
                        reconciliation_next_attempt_at,
                        transport_indeterminate_at,
                        created_at
                      ), completion_sha256
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    return None if value is None else str(value)


@_atomic_mutation
def mark_captured_paper_reconciliation_pending(
    db: Any,
    *,
    completion_sha256: str,
    lease_token: str,
    lease_owner_id: str,
    reconciliation_evidence_sha256: str,
) -> CapturedPaperOutboxRecord:
    """Return to nonterminal indeterminate state; never infer CID absence."""

    record = load_captured_paper_outbox(
        db, completion_sha256=completion_sha256, for_update=True
    )
    token, owner = _exact_lease_match(
        record, lease_token=lease_token, lease_owner_id=lease_owner_id
    )
    if record.status != OUTBOX_STATUS_RECONCILING:
        raise CapturedPaperOutboxLeaseError(
            "outbox_reconciliation_phase_invalid"
        )
    evidence = _digest(
        reconciliation_evidence_sha256,
        field_name="reconciliation_evidence_sha256",
    )
    exhausted_cycle = (
        record.reconciliation_attempt_count
        >= record.max_reconciliation_attempts
    )
    delay = (
        record.durable_transport
        .reconciliation_health_escalation_delay_seconds
        if exhausted_cycle
        else record.durable_transport.reconciliation_retry_delay_seconds
    )
    result = db.execute(
        text(
            """
            UPDATE captured_paper_post_commit_outbox
               SET status = 'transport_indeterminate',
                   lease_token = NULL, lease_owner_id = NULL,
                   lease_expires_at = NULL,
                   reconciliation_attempt_count = CASE
                       WHEN :exhausted_cycle THEN 0
                       ELSE reconciliation_attempt_count END,
                   reconciliation_next_attempt_at = clock_timestamp()
                       + make_interval(secs => :delay),
                   reconciliation_health_state = CASE
                       WHEN :exhausted_cycle THEN 'escalated'
                       ELSE reconciliation_health_state END,
                   reconciliation_escalation_count = CASE
                       WHEN :exhausted_cycle
                       THEN reconciliation_escalation_count + 1
                       ELSE reconciliation_escalation_count END,
                   last_reconciliation_health_escalated_at = CASE
                       WHEN :exhausted_cycle THEN clock_timestamp()
                       ELSE last_reconciliation_health_escalated_at END,
                   last_reconciliation_evidence_sha256 = :evidence,
                   updated_at = clock_timestamp(), version = version + 1
             WHERE completion_sha256 = :completion_sha256
               AND version = :version AND status = 'reconciling'
               AND lease_token = CAST(:lease_token AS UUID)
               AND lease_owner_id = CAST(:lease_owner_id AS UUID)
            """
        ),
        {
            "completion_sha256": record.completion_sha256,
            "version": record.version,
            "lease_token": token,
            "lease_owner_id": owner,
            "evidence": evidence,
            "exhausted_cycle": exhausted_cycle,
            "delay": delay,
        },
    )
    if result.rowcount != 1:
        raise CapturedPaperOutboxConflictError(
            "outbox_reconciliation_pending_cas_failed"
        )
    changed = _select_row(
        db,
        completion_sha256=record.completion_sha256,
        for_update=True,
    )
    assert changed is not None
    _append_event(
        db,
        row=changed,
        event_type=(
            "reconciliation_health_escalated"
            if exhausted_cycle
            else "reconciliation_pending"
        ),
        event_payload={
            "client_order_id": record.request.intent.client_order_id,
            "reconciliation_evidence_sha256": evidence,
            "reconciliation_total_attempt_count": (
                record.reconciliation_total_attempt_count
            ),
            "reconciliation_escalation_count": (
                record.reconciliation_escalation_count
                + int(exhausted_cycle)
            ),
            "retry_delay_seconds": delay,
        },
    )
    return load_captured_paper_outbox(
        db, completion_sha256=record.completion_sha256, for_update=True
    )


@_atomic_mutation
def mark_captured_paper_reconciliation_accepted(
    db: Any,
    *,
    completion_sha256: str,
    lease_token: str,
    lease_owner_id: str,
    authority: CapturedPaperTransportAuthority,
    acceptance: CapturedPaperBrokerAcceptanceProof,
) -> CapturedPaperOutboxRecord:
    """Complete indeterminate work only from positive same-CID durable truth."""

    completion_sha256 = _digest(
        completion_sha256, field_name="completion_sha256"
    )
    token = _canonical_uuid(lease_token, field_name="outbox_lease_token")
    owner = _canonical_uuid(lease_owner_id, field_name="outbox_lease_owner_id")
    request = _load_request_unlocked(
        db, completion_sha256=completion_sha256
    )
    if type(acceptance) is not CapturedPaperBrokerAcceptanceProof:
        raise CapturedPaperOutboxError("broker_acceptance_type_invalid")
    acceptance.verify_for_authority(
        authority,
        expected_kind="same_cid_reconciliation",
    )
    binding = _verify_durable_transport_binding(
        db,
        request=request,
        authority=authority,
        acceptance=acceptance,
    )
    record = load_captured_paper_outbox(
        db, completion_sha256=completion_sha256, for_update=True
    )
    token, owner = _exact_lease_match(
        record, lease_token=token, lease_owner_id=owner
    )
    if record.status != OUTBOX_STATUS_RECONCILING:
        raise CapturedPaperOutboxLeaseError(
            "outbox_reconciliation_phase_invalid"
        )
    if record.transport_evidence_sha256 != authority.authority_sha256:
        raise CapturedPaperOutboxConflictError(
            "outbox_transport_authority_mismatch"
        )
    proof = acceptance.acceptance_sha256
    result = db.execute(
        text(
            """
            UPDATE captured_paper_post_commit_outbox
               SET status = 'completed', lease_token = NULL,
                   lease_owner_id = NULL, lease_expires_at = NULL,
                   reconciliation_next_attempt_at = NULL,
                   last_reconciliation_evidence_sha256 = :proof,
                   completion_proof_sha256 = :proof,
                   completed_at = clock_timestamp(),
                   updated_at = clock_timestamp(), version = version + 1
             WHERE completion_sha256 = :completion_sha256
               AND version = :version AND status = 'reconciling'
               AND lease_token = CAST(:lease_token AS UUID)
               AND lease_owner_id = CAST(:lease_owner_id AS UUID)
            """
        ),
        {
            "completion_sha256": record.completion_sha256,
            "version": record.version,
            "lease_token": token,
            "lease_owner_id": owner,
            "proof": proof,
        },
    )
    if result.rowcount != 1:
        raise CapturedPaperOutboxConflictError(
            "outbox_reconciliation_acceptance_cas_failed"
        )
    changed = _select_row(
        db,
        completion_sha256=record.completion_sha256,
        for_update=True,
    )
    assert changed is not None
    _append_event(
        db,
        row=changed,
        event_type="reconciliation_accepted",
        event_payload={
            "client_order_id": request.intent.client_order_id,
            "broker_order_id": acceptance.broker_order_id,
            "reservation_id": authority.reservation_id,
            "completion_proof_sha256": proof,
        },
    )
    _ensure_completed_fill_watch(
        db,
        request=request,
        authority=authority,
        acceptance=acceptance,
        binding=binding,
        completion_event_type="reconciliation_accepted",
    )
    return load_captured_paper_outbox(
        db, completion_sha256=record.completion_sha256, for_update=True
    )


@_atomic_mutation
def recover_expired_captured_paper_leases(
    db: Any,
    *,
    limit: int,
) -> tuple[str, ...]:
    """Recover bounded expired transport/reconciliation leases fail closed."""

    limit = _bounded_positive_int(
        limit, field_name="outbox_recovery_limit", maximum=10_000
    )
    rows = db.execute(
        text(
            """
            SELECT completion_sha256
              FROM captured_paper_post_commit_outbox
             WHERE status IN ('transport_started', 'reconciling')
               AND lease_expires_at <= clock_timestamp()
             ORDER BY lease_expires_at, completion_sha256
             FOR UPDATE SKIP LOCKED
             LIMIT :limit
            """
        ),
        {"limit": limit},
    ).scalars().all()
    recovered: list[str] = []
    for raw_completion_sha256 in rows:
        completion_sha256 = str(raw_completion_sha256)
        row = _select_row(
            db,
            completion_sha256=completion_sha256,
            for_update=True,
        )
        if row is None:
            continue
        record = _record_from_row(db, row)
        now = _db_now(db)
        if record.lease_expires_at is None or record.lease_expires_at > now:
            continue
        if record.status == OUTBOX_STATUS_TRANSPORT_STARTED:
            evidence = _sha256_text(
                _canonical_json(
                    {
                        "schema_version": OUTBOX_PAYLOAD_SCHEMA_VERSION,
                        "reason": "transport_lease_expired",
                        "completion_sha256": completion_sha256,
                        "client_order_id": record.request.intent.client_order_id,
                        "transport_evidence_sha256": (
                            record.transport_evidence_sha256
                        ),
                        "expired_lease_token": record.lease_token,
                        "observed_at": now.isoformat(),
                    }
                )
            )
            result = db.execute(
                text(
                    """
                    UPDATE captured_paper_post_commit_outbox
                       SET status = 'transport_indeterminate',
                           lease_token = NULL, lease_owner_id = NULL,
                           lease_expires_at = NULL,
                           transport_indeterminate_at = :observed_at,
                           indeterminate_evidence_sha256 = :evidence,
                           updated_at = clock_timestamp(), version = version + 1
                     WHERE completion_sha256 = :completion_sha256
                       AND version = :version
                       AND status = 'transport_started'
                    """
                ),
                {
                    "completion_sha256": completion_sha256,
                    "version": record.version,
                    "observed_at": now,
                    "evidence": evidence,
                },
            )
            event_type = "expired_transport_recovery"
            event_payload = {
                "client_order_id": record.request.intent.client_order_id,
                "indeterminate_evidence_sha256": evidence,
            }
        elif record.status == OUTBOX_STATUS_RECONCILING:
            exhausted_cycle = (
                record.reconciliation_attempt_count
                >= record.max_reconciliation_attempts
            )
            delay = (
                record.durable_transport
                .reconciliation_health_escalation_delay_seconds
                if exhausted_cycle
                else record.durable_transport.reconciliation_retry_delay_seconds
            )
            evidence = _sha256_text(
                _canonical_json(
                    {
                        "schema_version": OUTBOX_PAYLOAD_SCHEMA_VERSION,
                        "reason": "reconciliation_lease_expired",
                        "completion_sha256": completion_sha256,
                        "client_order_id": (
                            record.request.intent.client_order_id
                        ),
                        "reconciliation_total_attempt_count": (
                            record.reconciliation_total_attempt_count
                        ),
                        "expired_lease_token": record.lease_token,
                        "observed_at": now.isoformat(),
                    }
                )
            )
            result = db.execute(
                text(
                    """
                    UPDATE captured_paper_post_commit_outbox
                       SET status = 'transport_indeterminate',
                           lease_token = NULL, lease_owner_id = NULL,
                           lease_expires_at = NULL,
                           reconciliation_attempt_count = CASE
                               WHEN :exhausted_cycle THEN 0
                               ELSE reconciliation_attempt_count END,
                           reconciliation_next_attempt_at = :now
                               + make_interval(secs => :delay),
                           reconciliation_health_state = CASE
                               WHEN :exhausted_cycle THEN 'escalated'
                               ELSE reconciliation_health_state END,
                           reconciliation_escalation_count = CASE
                               WHEN :exhausted_cycle
                               THEN reconciliation_escalation_count + 1
                               ELSE reconciliation_escalation_count END,
                           last_reconciliation_health_escalated_at = CASE
                               WHEN :exhausted_cycle THEN :now
                               ELSE last_reconciliation_health_escalated_at END,
                           last_reconciliation_evidence_sha256 = :evidence,
                           updated_at = clock_timestamp(), version = version + 1
                     WHERE completion_sha256 = :completion_sha256
                       AND version = :version AND status = 'reconciling'
                    """
                ),
                {
                    "completion_sha256": completion_sha256,
                    "version": record.version,
                    "exhausted_cycle": exhausted_cycle,
                    "now": now,
                    "delay": delay,
                    "evidence": evidence,
                },
            )
            event_type = (
                "reconciliation_health_escalated"
                if exhausted_cycle
                else "expired_reconciliation_recovery"
            )
            event_payload = {
                "client_order_id": record.request.intent.client_order_id,
                "reconciliation_attempt_count": (
                    record.reconciliation_attempt_count
                ),
                "reconciliation_total_attempt_count": (
                    record.reconciliation_total_attempt_count
                ),
                "reconciliation_evidence_sha256": evidence,
                "reconciliation_escalation_count": (
                    record.reconciliation_escalation_count
                    + int(exhausted_cycle)
                ),
                "retry_delay_seconds": delay,
            }
        else:
            continue
        if result.rowcount != 1:
            raise CapturedPaperOutboxConflictError(
                "outbox_expired_recovery_cas_failed"
            )
        changed = _select_row(
            db, completion_sha256=completion_sha256, for_update=True
        )
        assert changed is not None
        _append_event(
            db,
            row=changed,
            event_type=event_type,
            event_payload=event_payload,
        )
        recovered.append(completion_sha256)
    return tuple(recovered)


__all__ = (
    "CapturedPaperBrokerAcceptanceProof",
    "CapturedPaperCompletedFillWatchBundle",
    "CapturedPaperCompletedFillWatchLease",
    "CapturedPaperDurableTransportBundle",
    "CapturedPaperOutboxConflictError",
    "CapturedPaperOutboxCorruptionError",
    "CapturedPaperOutboxError",
    "CapturedPaperOutboxEvent",
    "CapturedPaperOutboxLease",
    "CapturedPaperOutboxLeaseError",
    "CapturedPaperOutboxNotFoundError",
    "CapturedPaperOutboxRecord",
    "CapturedPaperPositiveAdoptionLockReceipt",
    "CapturedPaperTransportAuthority",
    "CapturedPaperTransportDispatchAuthority",
    "CapturedPaperTransportInvocationAuthority",
    "CapturedPaperTransportPreDispatchEvidence",
    "CAPTURED_PAPER_FILL_HANDOFF_RECEIPT_SCHEMA_VERSION",
    "CAPTURED_PAPER_COMPLETED_FILL_WATCH_SCHEMA_VERSION",
    "DURABLE_COMMITTED_ADMISSION_SCHEMA_VERSION",
    "DURABLE_TRANSPORT_INSTRUCTION_SCHEMA_VERSION",
    "OUTBOX_STATUS_COMPLETED",
    "OUTBOX_STATUS_FILL_HANDOFF_COMMITTED",
    "OUTBOX_STATUS_LEASED",
    "OUTBOX_STATUS_PENDING",
    "OUTBOX_STATUS_RECONCILING",
    "OUTBOX_STATUS_RETRY_EXHAUSTED",
    "OUTBOX_STATUS_RETRY_WAIT",
    "OUTBOX_STATUS_TRANSPORT_INDETERMINATE",
    "OUTBOX_STATUS_TRANSPORT_STARTED",
    "FILL_WATCH_STATE_HANDOFF_COMMITTED",
    "FILL_WATCH_STATE_LEASED",
    "FILL_WATCH_STATE_PENDING",
    "FILL_WATCH_STATE_RETRY_WAIT",
    "FILL_WATCH_STATE_TERMINAL_ZERO_FILL",
    "build_captured_paper_durable_transport_artifacts",
    "authorize_captured_paper_transport_invocation",
    "commit_captured_paper_fill_handoff",
    "consume_captured_paper_transport_dispatch_authority",
    "complete_captured_paper_terminal_zero_fill_watch",
    "find_next_due_captured_paper_completion",
    "find_next_due_captured_paper_reconciliation",
    "lease_captured_paper_completion",
    "lease_captured_paper_indeterminate_reconciliation",
    "lease_next_captured_paper_indeterminate_reconciliation",
    "lease_next_captured_paper_completion",
    "lease_next_captured_paper_completed_fill_watch",
    "lock_captured_paper_positive_adoption",
    "load_captured_paper_durable_transport_bundle",
    "load_captured_paper_completed_fill_watch_bundle",
    "load_captured_paper_outbox",
    "mark_captured_paper_completion_accepted",
    "mark_captured_paper_reconciliation_accepted",
    "mark_captured_paper_reconciliation_pending",
    "mark_captured_paper_retryable",
    "mark_captured_paper_transport_indeterminate",
    "mark_captured_paper_transport_started",
    "persist_captured_paper_post_commit_request",
    "recover_expired_captured_paper_leases",
    "record_captured_paper_transport_financial_breaker",
    "revalidate_captured_paper_transport_dispatch_authority",
    "reschedule_captured_paper_completed_fill_watch",
)
