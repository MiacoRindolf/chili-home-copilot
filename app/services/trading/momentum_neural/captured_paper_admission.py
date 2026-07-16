"""Atomic captured Alpaca PAPER admission.

This module is the only phase-one writer for the captured PAPER path.  It owns
one short PostgreSQL transaction and returns a transport handoff only after
that transaction commits.  Provider and broker I/O are deliberately absent:
all market/account inputs arrive as process-private capture authorities, while
the later outbox owner performs transport after every lock in this transaction
has been released.

The lock walk is intentionally explicit::

    phase-one handoff -> Alpaca A1 -> adaptive A2 -> settlement head
      -> reservations/fills/cycles -> action claim -> automation session
      -> first-dip opportunity -> outbox -> phase-one committed event

Missing, stale or mismatched evidence raises before commit.  Consequently a
rejected decision leaves no action claim, risk reservation, daily opportunity
reservation, session admission marker or outbox row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import uuid
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ....models.trading import (
    AdaptiveRiskDecisionPacket,
    AdaptiveRiskOpportunityClaim,
    AdaptiveRiskReservation,
    TradingAutomationSession,
)
from .adaptive_risk_account_lock import (
    AccountRiskRowLockStage,
    CanonicalAccountRiskRowLockGuard,
)
from .adaptive_risk_policy import AdaptiveRiskContractError
from .adaptive_risk_request_builder import (
    AdaptiveRiskBuilderError,
    build_adaptive_risk_request,
)
from .adaptive_risk_reservation import (
    AlpacaPaperBrokerAccountFacts,
    AdaptiveReservationError,
    AdaptiveRiskReservationStore,
)
from .captured_adaptive_risk_source import (
    CapturedAccountRiskReceipt,
    CapturedAdaptiveRiskCoverageUnavailable,
    CapturedAdaptiveRiskDecisionBoundary,
    CapturedAdaptiveRiskDecisionIdentity,
    CapturedAdaptiveRiskEconomicInputs,
    CapturedAdaptiveRiskEvidenceSet,
    CapturedAdaptiveRiskPolicySpec,
    CapturedAdaptiveRiskSourceFactory,
    CapturedExactBbo,
)
from .captured_alpaca_paper_adapter import (
    CapturedAlpacaPaperReadError,
    verify_captured_alpaca_paper_account_authority,
)
from .alpaca_buying_power_reflection import (
    PreparedAlpacaPaperBuyingPowerDoubleCensus,
)
from .captured_paper_dispatcher import (
    CapturedPaperDispatchRequest,
    CapturedPaperRuntimeUnavailableError,
)
from .captured_paper_entry_intent import (
    ALPACA_PAPER_ACCOUNT_SCOPE,
    CapturedPaperIntentContractError,
    CapturedPaperPostCommitRequest,
    CapturedPaperRouteDriftError,
    revalidate_captured_paper_route_token,
)
from .captured_paper_financial_breaker import (
    CapturedPaperFinancialBreakerError,
    CapturedPaperFinancialBreakerReceipt,
)
from .captured_paper_outbox import (
    DURABLE_COMMITTED_ADMISSION_SCHEMA_VERSION,
    CapturedPaperDurableTransportBundle,
    CapturedPaperOutboxError,
    CapturedPaperOutboxNotFoundError,
    CapturedPaperTransportAuthority,
    load_captured_paper_outbox,
    persist_captured_paper_post_commit_request,
)
from .captured_paper_phase_one_handoff import (
    CapturedPaperExecutedReadBinding,
    CapturedPaperPhaseOneHandoffError,
    commit_captured_paper_phase_one_outbox_in_transaction,
    lock_captured_paper_phase_one_for_admission,
    verify_captured_paper_executed_read_inventory,
)
from .first_dip_tape_decision import (
    FirstDipTapeDecisionProviderError,
    _resolve_first_dip_final_admission_with_active_provider,
    _verify_first_dip_final_admission_resolution,
)
from .first_dip_tape_policy import FirstDipTapePolicy
from .replay_capture_contract import (
    ActiveCaptureInputPrefixAttestation,
    CaptureContractError,
    verify_active_capture_input_attestation,
)
from .live_replay_capture import (
    CapturedReadResult,
    ExecutedCaptureReadInventory,
    FirstDipFinalCaptureFrontier,
    executed_capture_read_evidence,
)
from ..venue.alpaca_spot import quantize_alpaca_equity_limit_price


UTC = timezone.utc
ET = ZoneInfo("America/New_York")
CAPTURED_PAPER_ADMISSION_SCHEMA_VERSION = (
    "chili.captured-paper-atomic-admission.v1"
)
CAPTURED_PAPER_OPERATIONAL_POLICY_SCHEMA_VERSION = (
    "chili.captured-paper-operational-policy.v2"
)
CAPTURED_FIRST_DIP_DETECTOR_AUDIT_SCHEMA_VERSION = (
    "chili.captured-first-dip-detector-audit.v1"
)

CAPTURED_PAPER_CANONICAL_LOCK_ORDER = (
    "alpaca_account_advisory",
    "adaptive_account_advisory",
    "account_settlement_head",
    "adaptive_risk_reservation",
    "fill_activity_or_cycle_settlement",
    "broker_symbol_action_claim",
    "trading_automation_session",
    "adaptive_risk_opportunity_claim",
    "captured_paper_post_commit_outbox",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CAPTURED_PAPER_EXECUTION_SURFACE = "alpaca_paper"
_CAPTURED_FIRST_DIP_AUTHORITY_SURFACE = "captured_db_paper"
_FIRST_DIP_SETUP_FAMILY = "first_dip_reclaim"
_INTENDED_FIRST_DIP_PAPER_MODE = "candidate"


class CapturedPaperAdmissionError(RuntimeError):
    """A captured PAPER decision cannot safely acquire execution authority."""

    def __init__(self, reason: str):
        self.reason = str(reason or "captured_paper_admission_rejected")
        super().__init__(self.reason)


class CapturedPaperAdmissionRejected(CapturedPaperAdmissionError):
    """Stable fail-closed rejection; the owning transaction is rolled back."""


def _reject(reason: str) -> None:
    raise CapturedPaperAdmissionRejected(reason)


def _sha(value: Any, *, field_name: str) -> str:
    digest = str(value or "").strip()
    if _SHA256_RE.fullmatch(digest) is None:
        _reject(f"{field_name}_invalid")
    return digest


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (CaptureContractError, TypeError, ValueError) as exc:
        raise CapturedPaperAdmissionRejected(
            "captured_paper_admission_payload_not_canonical"
        ) from exc


def _sha256_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_uuid(value: Any, *, field_name: str) -> str:
    raw = str(value or "").strip().lower()
    try:
        canonical = str(uuid.UUID(raw))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedPaperAdmissionRejected(f"{field_name}_invalid") from exc
    if raw != canonical:
        _reject(f"{field_name}_invalid")
    return canonical


def _positive_int(value: Any, *, field_name: str, maximum: int) -> int:
    if isinstance(value, bool):
        _reject(f"{field_name}_invalid")
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CapturedPaperAdmissionRejected(f"{field_name}_invalid") from exc
    if normalized <= 0 or normalized > maximum:
        _reject(f"{field_name}_invalid")
    return normalized


def _decimal_text(value: Any, *, field_name: str) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CapturedPaperAdmissionRejected(f"{field_name}_invalid") from exc
    if not number.is_finite() or number <= 0:
        _reject(f"{field_name}_invalid")
    normalized = format(number.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized


def _canonical_entry_limit(value: Any) -> str:
    """Return the exact Alpaca BUY tick without widening the frozen ceiling."""

    ceiling = _decimal_text(value, field_name="intent_entry_limit_ceiling_price")
    try:
        canonical = quantize_alpaca_equity_limit_price(ceiling, "buy")
    except (TypeError, ValueError) as exc:
        raise CapturedPaperAdmissionRejected(
            "captured_paper_entry_limit_not_canonical"
        ) from exc
    if Decimal(canonical) != Decimal(ceiling):
        _reject("captured_paper_entry_limit_exceeds_frozen_ceiling")
    return canonical


def _verify_order_request(
    value: Mapping[str, Any],
    *,
    request: CapturedPaperPostCommitRequest,
    quantity_shares: int,
) -> dict[str, Any]:
    order = dict(value)
    expected_fields = {
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
    if set(order) != expected_fields:
        _reject("captured_paper_order_request_fields_invalid")
    intent = request.intent
    exact = {
        "asset_class": "us_equity",
        "client_order_id": intent.client_order_id,
        "limit_price": _canonical_entry_limit(
            intent.entry_limit_ceiling_price
        ),
        "position_intent": "buy_to_open",
        "qty": str(int(quantity_shares)),
        "side": "buy",
        "symbol": intent.route_token.symbol,
        "type": "limit",
    }
    if any(order.get(name) != required for name, required in exact.items()):
        _reject("captured_paper_order_request_binding_mismatch")
    if type(order.get("extended_hours")) is not bool:
        _reject("captured_paper_order_request_extended_hours_invalid")
    if order.get("time_in_force") not in {"day", "gtc"}:
        _reject("captured_paper_order_request_time_in_force_invalid")
    return order


def _db_now(db: Session) -> datetime:
    value = db.execute(text("SELECT clock_timestamp()")).scalar_one()
    if not isinstance(value, datetime) or value.tzinfo is None:
        _reject("captured_paper_database_clock_unavailable")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CapturedPaperOperationalPolicy:
    """Explicit non-economic transport/outbox policy with provenance.

    There are intentionally no dollar, symbol-count, position-count or daily
    loss constants here.  Adaptive sizing and aggregate exposure remain wholly
    owned by the shared replay/PAPER risk policy.
    """

    action_claim_lease_seconds: int
    outbox_max_attempts: int
    outbox_max_reconciliation_attempts: int
    reconciliation_retry_delay_seconds: int
    reconciliation_health_escalation_delay_seconds: int
    time_in_force: str
    extended_hours: bool
    config_provenance_sha256: str
    schema_version: str = CAPTURED_PAPER_OPERATIONAL_POLICY_SCHEMA_VERSION
    policy_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURED_PAPER_OPERATIONAL_POLICY_SCHEMA_VERSION:
            raise ValueError("captured PAPER operational policy schema is invalid")
        object.__setattr__(
            self,
            "action_claim_lease_seconds",
            _positive_int(
                self.action_claim_lease_seconds,
                field_name="action_claim_lease_seconds",
                maximum=86_400,
            ),
        )
        object.__setattr__(
            self,
            "outbox_max_attempts",
            _positive_int(
                self.outbox_max_attempts,
                field_name="outbox_max_attempts",
                maximum=32_767,
            ),
        )
        object.__setattr__(
            self,
            "outbox_max_reconciliation_attempts",
            _positive_int(
                self.outbox_max_reconciliation_attempts,
                field_name="outbox_max_reconciliation_attempts",
                maximum=32_767,
            ),
        )
        object.__setattr__(
            self,
            "reconciliation_retry_delay_seconds",
            _positive_int(
                self.reconciliation_retry_delay_seconds,
                field_name="reconciliation_retry_delay_seconds",
                maximum=604_800,
            ),
        )
        object.__setattr__(
            self,
            "reconciliation_health_escalation_delay_seconds",
            _positive_int(
                self.reconciliation_health_escalation_delay_seconds,
                field_name=(
                    "reconciliation_health_escalation_delay_seconds"
                ),
                maximum=604_800,
            ),
        )
        tif = str(self.time_in_force or "").strip().lower()
        if tif not in {"day", "gtc"}:
            raise ValueError("captured PAPER time_in_force is invalid")
        object.__setattr__(self, "time_in_force", tif)
        if type(self.extended_hours) is not bool:
            raise ValueError("captured PAPER extended_hours must be boolean")
        if self.extended_hours and tif != "day":
            raise ValueError(
                "captured PAPER extended-hours limit entries require DAY tif"
            )
        config_sha = _sha(
            self.config_provenance_sha256,
            field_name="operational_policy_config_provenance_sha256",
        )
        object.__setattr__(self, "config_provenance_sha256", config_sha)
        object.__setattr__(self, "policy_sha256", _sha256_json(self._body()))

    def _body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "action_claim_lease_seconds": self.action_claim_lease_seconds,
            "outbox_max_attempts": self.outbox_max_attempts,
            "outbox_max_reconciliation_attempts": (
                self.outbox_max_reconciliation_attempts
            ),
            "reconciliation_retry_delay_seconds": (
                self.reconciliation_retry_delay_seconds
            ),
            "reconciliation_health_escalation_delay_seconds": (
                self.reconciliation_health_escalation_delay_seconds
            ),
            "order_type": "limit",
            "time_in_force": self.time_in_force,
            "extended_hours": self.extended_hours,
            "config_provenance_sha256": self.config_provenance_sha256,
        }

    def to_payload(self) -> dict[str, Any]:
        return {**self._body(), "policy_sha256": self.policy_sha256}


@dataclass(frozen=True, slots=True)
class CapturedFirstDipDetectorAudit:
    """Detector-side audit values independently rebound by private authority."""

    detector_policy: FirstDipTapePolicy
    detector_authority_source: str
    detector_receipt_binding_sha256: str
    detector_opportunity_key_sha256: str
    schema_version: str = CAPTURED_FIRST_DIP_DETECTOR_AUDIT_SCHEMA_VERSION
    audit_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURED_FIRST_DIP_DETECTOR_AUDIT_SCHEMA_VERSION:
            raise ValueError("first-dip detector audit schema is invalid")
        if type(self.detector_policy) is not FirstDipTapePolicy:
            raise ValueError("first-dip detector policy is not typed")
        source = str(self.detector_authority_source or "").strip()
        if source != _CAPTURED_FIRST_DIP_AUTHORITY_SURFACE:
            raise ValueError("first-dip detector authority surface is invalid")
        object.__setattr__(self, "detector_authority_source", source)
        for name in (
            "detector_receipt_binding_sha256",
            "detector_opportunity_key_sha256",
        ):
            object.__setattr__(
                self,
                name,
                _sha(getattr(self, name), field_name=name),
            )
        object.__setattr__(self, "audit_sha256", _sha256_json(self._body()))

    def _body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "detector_policy_sha256": self.detector_policy.policy_sha256,
            "detector_authority_source": self.detector_authority_source,
            "detector_receipt_binding_sha256": (
                self.detector_receipt_binding_sha256
            ),
            "detector_opportunity_key_sha256": (
                self.detector_opportunity_key_sha256
            ),
        }

    def to_payload(self) -> dict[str, Any]:
        return {**self._body(), "audit_sha256": self.audit_sha256}


@dataclass(frozen=True, slots=True)
class CapturedPaperAdmissionInputs:
    """All already-captured material needed by the one admission transaction."""

    dispatch_request: CapturedPaperDispatchRequest
    post_commit_request: CapturedPaperPostCommitRequest
    broker_account_facts: AlpacaPaperBrokerAccountFacts
    policy_spec: CapturedAdaptiveRiskPolicySpec
    active_input_attestation: ActiveCaptureInputPrefixAttestation
    predecision_captured_reads: tuple[CapturedReadResult, ...]
    executed_read_inventory: ExecutedCaptureReadInventory
    exact_bbo: CapturedExactBbo
    account_receipt: CapturedAccountRiskReceipt
    economics: CapturedAdaptiveRiskEconomicInputs
    fact_evidence: CapturedAdaptiveRiskEvidenceSet
    correlation_cluster: str
    operational_policy: CapturedPaperOperationalPolicy
    buying_power_double_census: (
        PreparedAlpacaPaperBuyingPowerDoubleCensus | None
    ) = None
    first_dip_detector_audit: CapturedFirstDipDetectorAudit | None = None


@dataclass(frozen=True, slots=True)
class CapturedPaperFinalExecutedReadAuthority:
    """Process-private final first-dip read proof returned during admission."""

    inventory: ExecutedCaptureReadInventory
    captured_reads: tuple[CapturedReadResult, ...]
    active_input_attestation: ActiveCaptureInputPrefixAttestation
    frontier: FirstDipFinalCaptureFrontier

    def __post_init__(self) -> None:
        if (
            type(self.inventory) is not ExecutedCaptureReadInventory
            or type(self.frontier) is not FirstDipFinalCaptureFrontier
            or not self.captured_reads
            or any(type(row) is not CapturedReadResult for row in self.captured_reads)
        ):
            raise ValueError("captured PAPER final executed-read authority is invalid")
        try:
            raw_evidence = tuple(
                sorted(
                    (
                        executed_capture_read_evidence(row)
                        for row in self.captured_reads
                    ),
                    key=lambda row: (row.receipt_event_sequence, row.read_id),
                )
            )
        except CaptureContractError as exc:
            raise ValueError(
                "captured PAPER final executed-read results are invalid"
            ) from exc
        proof = verify_active_capture_input_attestation(
            self.active_input_attestation
        )
        if (
            tuple(self.inventory.reads) != raw_evidence
            or
            self.inventory.run_id != proof.run_id
            or self.inventory.generation != proof.generation
            or self.inventory.identity_sha256 != proof.identity_sha256
            or self.inventory.decision_id != proof.decision_id
            or self.frontier.run_id != proof.run_id
            or self.frontier.generation != proof.generation
            or self.frontier.identity_sha256 != proof.identity_sha256
            or self.frontier.decision_id != proof.decision_id
            or self.frontier.input_prefix_sequence
            != proof.input_prefix_sequence
            or self.frontier.input_prefix_root_sha256
            != proof.input_prefix_root_sha256
            or self.frontier.attested_available_at
            != proof.attested_available_at
            or self.frontier.expires_at != proof.expires_at
            or self.frontier.dependency_profile_sha256
            != proof.dependency_profile.profile_sha256
            or self.frontier.required_read_ids != proof.required_read_ids
            or self.frontier.read_evidence_inventory_sha256
            != proof.read_evidence_inventory_sha256
            or tuple(row.read_id for row in self.inventory.reads)
            != tuple(
                sorted(
                    proof.required_read_ids,
                    key=lambda read_id: (
                        next(
                            evidence.receipt_event_sequence
                            for evidence in proof.read_evidence
                            if evidence.receipt.read_id == read_id
                        ),
                        read_id,
                    ),
                )
            )
        ):
            raise ValueError(
                "captured PAPER final executed-read authority mismatches frontier"
            )


@dataclass(frozen=True, slots=True)
class CommittedCapturedPaperAdmission:
    """Typed handoff created only after the admission transaction committed."""

    post_commit_request: CapturedPaperPostCommitRequest
    reservation_id: str
    decision_packet_sha256: str
    reservation_request_sha256: str
    adaptive_input_evidence_sha256: str
    account_identity_sha256: str
    quantity_shares: int
    structural_risk_usd: str
    gross_notional_usd: str
    buying_power_impact_usd: str
    order_request: Mapping[str, Any]
    order_request_sha256: str
    admission_record_sha256: str
    committed_at: datetime
    lock_order: tuple[str, ...] = CAPTURED_PAPER_CANONICAL_LOCK_ORDER
    schema_version: str = CAPTURED_PAPER_ADMISSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.post_commit_request) is not CapturedPaperPostCommitRequest:
            raise ValueError("committed captured PAPER request is invalid")
        self.post_commit_request.verify()
        object.__setattr__(
            self,
            "reservation_id",
            _canonical_uuid(self.reservation_id, field_name="reservation_id"),
        )
        for name in (
            "decision_packet_sha256",
            "reservation_request_sha256",
            "adaptive_input_evidence_sha256",
            "account_identity_sha256",
            "order_request_sha256",
            "admission_record_sha256",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), field_name=name))
        if isinstance(self.quantity_shares, bool) or int(self.quantity_shares) <= 0:
            raise ValueError("committed captured PAPER quantity is invalid")
        object.__setattr__(self, "quantity_shares", int(self.quantity_shares))
        for name in (
            "structural_risk_usd",
            "gross_notional_usd",
            "buying_power_impact_usd",
        ):
            object.__setattr__(
                self, name, _decimal_text(getattr(self, name), field_name=name)
            )
        order = _verify_order_request(
            self.order_request,
            request=self.post_commit_request,
            quantity_shares=self.quantity_shares,
        )
        if _sha256_json(order) != self.order_request_sha256:
            raise ValueError("committed captured PAPER order hash is invalid")
        object.__setattr__(self, "order_request", order)
        if not isinstance(self.committed_at, datetime) or self.committed_at.tzinfo is None:
            raise ValueError("committed captured PAPER clock is invalid")
        object.__setattr__(self, "committed_at", self.committed_at.astimezone(UTC))
        if self.lock_order != CAPTURED_PAPER_CANONICAL_LOCK_ORDER:
            raise ValueError("committed captured PAPER lock order is invalid")


@dataclass(frozen=True, slots=True)
class _PendingCapturedPaperAdmission:
    values: Mapping[str, Any]


def _verify_pure_inputs(inputs: CapturedPaperAdmissionInputs) -> tuple[Any, Any]:
    if type(inputs) is not CapturedPaperAdmissionInputs:
        _reject("captured_paper_admission_inputs_invalid")
    if type(inputs.dispatch_request) is not CapturedPaperDispatchRequest:
        _reject("captured_paper_dispatch_request_invalid")
    inputs.dispatch_request.verify()
    request = inputs.post_commit_request
    if type(request) is not CapturedPaperPostCommitRequest:
        _reject("captured_paper_post_commit_request_invalid")
    request.verify()
    intent = request.intent
    route = intent.route_token
    if inputs.dispatch_request.route_token.route_token_sha256 != route.route_token_sha256:
        _reject("captured_paper_dispatch_intent_route_mismatch")
    if route.account_scope != ALPACA_PAPER_ACCOUNT_SCOPE:
        _reject("captured_paper_account_scope_invalid")
    if route.first_dip_policy_mode != _INTENDED_FIRST_DIP_PAPER_MODE:
        _reject("captured_paper_candidate_policy_not_active")
    exact_types = {
        "broker_account_facts": (
            inputs.broker_account_facts,
            AlpacaPaperBrokerAccountFacts,
        ),
        "exact_bbo": (inputs.exact_bbo, CapturedExactBbo),
        "account_receipt": (
            inputs.account_receipt,
            CapturedAccountRiskReceipt,
        ),
        "economics": (
            inputs.economics,
            CapturedAdaptiveRiskEconomicInputs,
        ),
        "fact_evidence": (
            inputs.fact_evidence,
            CapturedAdaptiveRiskEvidenceSet,
        ),
    }
    malformed = sorted(
        name
        for name, (value, required_type) in exact_types.items()
        if type(value) is not required_type
    )
    if malformed:
        _reject("captured_paper_typed_input_invalid:" + ",".join(malformed))
    predecision_reads = tuple(inputs.predecision_captured_reads)
    if (
        not predecision_reads
        or any(
            type(row) is not CapturedReadResult or not row.durable
            for row in predecision_reads
        )
        or type(inputs.executed_read_inventory)
        is not ExecutedCaptureReadInventory
    ):
        _reject("captured_paper_predecision_executed_reads_invalid")
    expected_predecision = tuple(
        sorted(
            (executed_capture_read_evidence(row) for row in predecision_reads),
            key=lambda row: (row.receipt_event_sequence, row.read_id),
        )
    )
    if tuple(inputs.executed_read_inventory.reads) != expected_predecision:
        _reject("captured_paper_predecision_executed_reads_mismatch")

    try:
        proof = verify_active_capture_input_attestation(
            inputs.active_input_attestation
        )
        authority = verify_captured_alpaca_paper_account_authority(
            inputs.broker_account_facts.capture_authority
        )
    except (CaptureContractError, CapturedAlpacaPaperReadError) as exc:
        raise CapturedPaperAdmissionRejected(
            "captured_paper_private_authority_invalid"
        ) from exc
    census = inputs.buying_power_double_census
    if census is not None and (
        type(census) is not PreparedAlpacaPaperBuyingPowerDoubleCensus
        or census.account_authority is not authority
        or census.before.decision_id != intent.decision_id
        or census.after.decision_id != intent.decision_id
    ):
        _reject("captured_paper_buying_power_census_binding_invalid")
    if type(inputs.policy_spec) is not CapturedAdaptiveRiskPolicySpec:
        _reject("captured_paper_adaptive_policy_spec_invalid")
    if type(inputs.operational_policy) is not CapturedPaperOperationalPolicy:
        _reject("captured_paper_operational_policy_invalid")
    if inputs.operational_policy.config_provenance_sha256 != route.config_sha256:
        _reject("captured_paper_operational_policy_config_mismatch")
    correlation_cluster = str(inputs.correlation_cluster or "").strip().lower()
    if not correlation_cluster or correlation_cluster != inputs.correlation_cluster:
        _reject("captured_paper_correlation_cluster_invalid")

    exact = {
        "decision_id": (proof.decision_id, intent.decision_id),
        "account_authority_decision_id": (authority.decision_id, intent.decision_id),
        "account_id": (authority.account_id, route.expected_account_id),
        "authority_run_id": (authority.run_id, proof.run_id),
        "generation": (authority.generation, proof.generation),
        "authority_account_identity": (
            authority.account_identity_sha256,
            proof.account_identity_sha256,
        ),
        "broker_facts_account_identity": (
            inputs.broker_account_facts.account_identity_sha256,
            proof.account_identity_sha256,
        ),
        "account_capture_receipt": (
            authority.active_input_attestation_sha256,
            proof.attestation_sha256,
        ),
        "account_receipt": (
            intent.account_receipt_sha256,
            authority.account_read_receipt_sha256,
        ),
        "account_read_id": (
            inputs.account_receipt.read_id,
            authority.account_read_id,
        ),
        "account_source_event": (
            inputs.account_receipt.source_event_sha256,
            authority.account_source_event_sha256,
        ),
        "code_build": (route.code_build_sha256, proof.code_build_sha256),
        "policy_code_build": (
            inputs.policy_spec.code_build_sha256,
            proof.code_build_sha256,
        ),
        "config": (route.config_sha256, proof.config_sha256),
        "policy_config": (
            inputs.policy_spec.effective_config_sha256,
            proof.config_sha256,
        ),
        "feature_flags": (
            intent.feature_flags_sha256,
            proof.feature_flags_sha256,
        ),
        "policy_feature_flags": (
            inputs.policy_spec.feature_flags_sha256,
            proof.feature_flags_sha256,
        ),
        "adaptive_policy": (
            intent.policy_sha256,
            inputs.policy_spec.policy.policy_sha256,
        ),
    }
    changed = sorted(name for name, pair in exact.items() if pair[0] != pair[1])
    if changed:
        _reject("captured_paper_pure_binding_mismatch:" + ",".join(changed))

    bbo_reads = tuple(
        row
        for row in proof.read_evidence
        if row.receipt.read_id == inputs.exact_bbo.read_id
    )
    if (
        len(bbo_reads) != 1
        or bbo_reads[0].receipt_sha256 != intent.bbo_receipt_sha256
        or inputs.exact_bbo.source_event_sha256
        not in bbo_reads[0].receipt.source_event_sha256s
    ):
        _reject("captured_paper_bbo_receipt_mismatch")
    if _decimal_text(
        inputs.economics.structural_stop,
        field_name="captured_structural_stop",
    ) != _decimal_text(
        intent.structural_stop_price,
        field_name="intent_structural_stop",
    ):
        _reject("captured_paper_structural_stop_mismatch")

    first_dip = intent.setup_family == _FIRST_DIP_SETUP_FAMILY
    if first_dip:
        audit = inputs.first_dip_detector_audit
        if type(audit) is not CapturedFirstDipDetectorAudit:
            _reject("captured_paper_first_dip_detector_audit_missing")
        if (
            intent.opportunity_key is None
            or proof.first_dip_tape_read_id is None
            or intent.setup_evidence_sha256
            != audit.detector_receipt_binding_sha256
        ):
            _reject("captured_paper_first_dip_detector_binding_mismatch")
    elif inputs.first_dip_detector_audit is not None:
        _reject("captured_paper_first_dip_detector_audit_unexpected")
    return proof, authority


def _lock_fill_and_cycle_rows(
    db: Session,
    *,
    account_identity_sha256: str,
) -> tuple[int, int]:
    fill_rows = db.execute(
        text(
            """
            SELECT reservation_id, sequence
              FROM alpaca_paper_fill_activities
             WHERE account_scope = :scope
               AND account_identity_sha256 = :account_identity_sha256
             ORDER BY reservation_id::text, sequence
             FOR UPDATE
            """
        ),
        {
            "scope": ALPACA_PAPER_ACCOUNT_SCOPE,
            "account_identity_sha256": account_identity_sha256,
        },
    ).all()
    settlement_rows = db.execute(
        text(
            """
            SELECT reservation_id, terminal_sequence
              FROM alpaca_paper_cycle_settlements
             WHERE account_scope = :scope
               AND account_identity_sha256 = :account_identity_sha256
             ORDER BY terminal_sequence
             FOR UPDATE
            """
        ),
        {
            "scope": ALPACA_PAPER_ACCOUNT_SCOPE,
            "account_identity_sha256": account_identity_sha256,
        },
    ).all()
    return len(fill_rows), len(settlement_rows)


def _action_claim_metadata(
    *,
    inputs: CapturedPaperAdmissionInputs,
    built: Any,
    order_request: Mapping[str, Any],
    first_dip_final_audit: Mapping[str, Any] | None,
) -> dict[str, Any]:
    intent = inputs.post_commit_request.intent
    route = intent.route_token
    return {
        "stage": "captured_paper_admitted_pending_transport",
        "order_role": "entry",
        "order_request": dict(order_request),
        "alpaca_account_id": route.expected_account_id,
        "entry_post_bind_token": intent.binder_id,
        "reserved_risk_usd": float(built.resolution.planned_structural_risk_usd),
        "reserved_gross_notional_usd": float(
            built.resolution.planned_notional_usd
        ),
        "reserved_buying_power_impact_usd": float(
            built.resolution.planned_buying_power_impact_usd
        ),
        "correlation_cluster_id": inputs.correlation_cluster,
        "adaptive_risk_decision_packet": dict(built.decision_packet),
        "adaptive_risk_reservation_claim": built.reservation_claim.to_payload(),
        "adaptive_risk_reservation_request": built.request.to_payload(),
        "captured_paper_completion_sha256": (
            inputs.post_commit_request.completion_sha256
        ),
        "captured_paper_runtime_generation": route.runtime_generation,
        "captured_paper_runtime_capture_receipt_sha256": (
            route.capture_receipt_sha256
        ),
        "active_input_attestation_sha256": (
            inputs.active_input_attestation.attestation_sha256
        ),
        "captured_paper_operational_policy": (
            inputs.operational_policy.to_payload()
        ),
        "first_dip_final_admission": (
            None if first_dip_final_audit is None else dict(first_dip_final_audit)
        ),
    }


def _acquire_new_action_claim(
    db: Session,
    *,
    inputs: CapturedPaperAdmissionInputs,
    metadata: Mapping[str, Any],
    claimed_at: datetime,
) -> None:
    intent = inputs.post_commit_request.intent
    route = intent.route_token
    existing = db.execute(
        text(
            """
            SELECT phase
              FROM broker_symbol_action_claims
             WHERE account_scope = :scope AND symbol = :symbol
             FOR UPDATE
            """
        ),
        {"scope": route.account_scope, "symbol": route.symbol},
    ).mappings().one_or_none()
    if existing is not None and existing["phase"] != "resolved":
        _reject("captured_paper_action_claim_already_active")
    lease_expires_at = claimed_at + timedelta(
        seconds=inputs.operational_policy.action_claim_lease_seconds
    )
    payload = _canonical_json(metadata)
    if existing is None:
        result = db.execute(
            text(
                """
                INSERT INTO broker_symbol_action_claims (
                    account_scope, symbol, claim_token, action, phase,
                    owner_session_id, client_order_id, broker_order_id,
                    metadata_json, claimed_at, updated_at, lease_expires_at,
                    resolved_at
                ) VALUES (
                    :scope, :symbol, :claim_token, 'entry', 'claimed',
                    :session_id, :client_order_id, NULL,
                    CAST(:metadata AS jsonb), :now, :now, :lease_expires_at,
                    NULL
                )
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "scope": route.account_scope,
                "symbol": route.symbol,
                "claim_token": intent.symbol_claim_token,
                "session_id": route.session_id,
                "client_order_id": intent.client_order_id,
                "metadata": payload,
                "now": claimed_at,
                "lease_expires_at": lease_expires_at,
            },
        )
    else:
        result = db.execute(
            text(
                """
                UPDATE broker_symbol_action_claims
                   SET claim_token = :claim_token,
                       action = 'entry', phase = 'claimed',
                       owner_session_id = :session_id,
                       client_order_id = :client_order_id,
                       broker_order_id = NULL,
                       metadata_json = CAST(:metadata AS jsonb),
                       claimed_at = :now, updated_at = :now,
                       lease_expires_at = :lease_expires_at,
                       resolved_at = NULL
                 WHERE account_scope = :scope AND symbol = :symbol
                   AND phase = 'resolved'
                """
            ),
            {
                "scope": route.account_scope,
                "symbol": route.symbol,
                "claim_token": intent.symbol_claim_token,
                "session_id": route.session_id,
                "client_order_id": intent.client_order_id,
                "metadata": payload,
                "now": claimed_at,
                "lease_expires_at": lease_expires_at,
            },
        )
    if int(result.rowcount or 0) != 1:
        _reject("captured_paper_action_claim_acquire_failed")


def _lock_and_verify_session(
    db: Session,
    *,
    inputs: CapturedPaperAdmissionInputs,
    decision_as_of: datetime,
) -> TradingAutomationSession:
    intent = inputs.post_commit_request.intent
    route = intent.route_token
    session_row = db.scalar(
        select(TradingAutomationSession)
        .where(TradingAutomationSession.id == route.session_id)
        .with_for_update()
    )
    if session_row is None:
        _reject("captured_paper_automation_session_missing")
    try:
        revalidate_captured_paper_route_token(
            route,
            session_row,
            inputs.dispatch_request,
        )
    except (CapturedPaperRouteDriftError, CapturedPaperRuntimeUnavailableError) as exc:
        raise CapturedPaperAdmissionRejected(str(exc)) from exc
    if session_row.ended_at is not None:
        _reject("captured_paper_automation_session_ended")
    snapshot = (
        dict(session_row.risk_snapshot_json)
        if isinstance(session_row.risk_snapshot_json, dict)
        else {}
    )
    arm = intent.confirmed_arm_generation
    marker = snapshot.get("confirmed_arm_generation")
    marker = marker if isinstance(marker, Mapping) else None
    expected_marker = {
        "version": 1,
        "session_id": route.session_id,
        "arm_token": arm.arm_token,
        "expires_at_utc": arm.expires_at.isoformat(),
        "alpaca_symbol_claim_token": arm.symbol_claim_token,
        "alpaca_account_scope": route.account_scope,
        "alpaca_account_id": route.expected_account_id,
        "confirmed_at_utc": arm.confirmed_at.isoformat(),
    }
    if (
        marker is None
        or any(marker.get(name) != value for name, value in expected_marker.items())
        or snapshot.get("alpaca_symbol_claim_token") != arm.symbol_claim_token
        or not (arm.confirmed_at <= decision_as_of <= arm.expires_at)
    ):
        _reject("captured_paper_confirmed_arm_generation_mismatch")
    live = snapshot.get("momentum_live_execution")
    live = live if isinstance(live, Mapping) else {}
    if live.get("position") is not None or live.get("entry_submitted") is True:
        _reject("captured_paper_owner_session_not_flat")
    return session_row


def _lock_opportunity(
    db: Session,
    *,
    inputs: CapturedPaperAdmissionInputs,
    decision_as_of: datetime,
) -> int:
    key = inputs.post_commit_request.intent.opportunity_key
    if key is None:
        return 0
    if key.trading_date != decision_as_of.astimezone(ET).date():
        _reject("captured_paper_opportunity_trading_date_mismatch")
    row = db.scalar(
        select(AdaptiveRiskOpportunityClaim)
        .where(AdaptiveRiskOpportunityClaim.account_scope == key.account_scope)
        .where(AdaptiveRiskOpportunityClaim.symbol == key.symbol)
        .where(AdaptiveRiskOpportunityClaim.trading_date == key.trading_date)
        .where(AdaptiveRiskOpportunityClaim.setup_family == key.setup_family)
        .with_for_update()
    )
    if row is not None and row.status != "available":
        _reject("captured_paper_opportunity_not_available")
    return 0 if row is None else int(row.id)


def _canonical_order_request(
    *,
    inputs: CapturedPaperAdmissionInputs,
    quantity_shares: int,
) -> dict[str, Any]:
    intent = inputs.post_commit_request.intent
    order = {
        "asset_class": "us_equity",
        "client_order_id": intent.client_order_id,
        "extended_hours": inputs.operational_policy.extended_hours,
        "limit_price": _canonical_entry_limit(
            intent.entry_limit_ceiling_price
        ),
        "position_intent": "buy_to_open",
        "qty": str(int(quantity_shares)),
        "side": "buy",
        "symbol": intent.route_token.symbol,
        "time_in_force": inputs.operational_policy.time_in_force,
        "type": "limit",
    }
    return _verify_order_request(
        order,
        request=inputs.post_commit_request,
        quantity_shares=quantity_shares,
    )


def _admit_in_transaction(
    db: Session,
    *,
    inputs: CapturedPaperAdmissionInputs,
    proof: ActiveCaptureInputPrefixAttestation,
    phase_one_material_sha256: str,
    executed_read_inventory: ExecutedCaptureReadInventory,
    executed_captured_reads: tuple[CapturedReadResult, ...],
    financial_breaker_receipt: CapturedPaperFinancialBreakerReceipt,
    final_executed_read_provider: (
        Callable[[], CapturedPaperFinalExecutedReadAuthority] | None
    ),
) -> _PendingCapturedPaperAdmission:
    phase_one_receipt = lock_captured_paper_phase_one_for_admission(
        db,
        request=inputs.post_commit_request,
        material_sha256=phase_one_material_sha256,
        executed_read_inventory=executed_read_inventory,
        captured_reads=executed_captured_reads,
        active_input_attestation=proof,
    )
    bound = db.get_bind()
    store = AdaptiveRiskReservationStore(getattr(bound, "engine", bound))
    intent = inputs.post_commit_request.intent
    route = intent.route_token
    row_locks = CanonicalAccountRiskRowLockGuard()

    bundle = store.lock_alpaca_paper_admission_bundle(
        broker_account_facts=inputs.broker_account_facts,
        symbol=route.symbol,
        correlation_cluster=inputs.correlation_cluster,
        session=db,
        buying_power_double_census=inputs.buying_power_double_census,
    )
    row_locks.observe(
        AccountRiskRowLockStage.ACCOUNT_SETTLEMENT_HEAD,
        sort_key=(route.account_scope,),
    )
    row_locks.observe(
        AccountRiskRowLockStage.ADAPTIVE_RESERVATION,
        sort_key=("00000000-0000-0000-0000-000000000000",),
    )

    identity = CapturedAdaptiveRiskDecisionIdentity(
        execution_surface=_CAPTURED_PAPER_EXECUTION_SURFACE,
        run_id=proof.run_id,
        generation=proof.generation,
        decision_id=intent.decision_id,
        symbol=route.symbol,
        setup_family=intent.setup_family,
        correlation_cluster=inputs.correlation_cluster,
        account_scope=route.account_scope,
        decision_at=bundle.decision_as_of,
    )
    boundary = CapturedAdaptiveRiskDecisionBoundary(
        identity=identity,
        exact_bbo=inputs.exact_bbo,
        economics=inputs.economics,
        fact_evidence=inputs.fact_evidence,
        account_snapshot=bundle.account_snapshot,
        account_receipt=inputs.account_receipt,
        reservation_ledger_snapshot=bundle.locked_risk_snapshot,
        active_capture_attestation=proof,
        locked_alpaca_paper_admission_bundle=bundle,
    )
    material = CapturedAdaptiveRiskSourceFactory(inputs.policy_spec).build(boundary)
    opportunity_payload = (
        None
        if intent.opportunity_key is None
        else {
            "account_scope": intent.opportunity_key.account_scope,
            "symbol": intent.opportunity_key.symbol,
            "trading_date": intent.opportunity_key.trading_date.isoformat(),
            "setup_family": intent.opportunity_key.setup_family,
        }
    )
    built = build_adaptive_risk_request(
        material.source,
        client_order_id=intent.client_order_id,
        entry_limit_price=float(Decimal(intent.entry_limit_ceiling_price)),
        opportunity_key=opportunity_payload,
        active_capture_attestation=material.active_capture_attestation,
    )
    if material.locked_alpaca_paper_admission_bundle is not bundle:
        _reject("captured_paper_locked_bundle_identity_changed")
    if not built.resolution.valid or int(built.resolution.quantity_shares) <= 0:
        _reject("captured_paper_adaptive_risk_rejected")

    first_dip_final_audit: Mapping[str, Any] | None = None
    final_executed_binding: CapturedPaperExecutedReadBinding | None = None
    final_executed_frontier_sha256: str | None = None
    if intent.setup_family == _FIRST_DIP_SETUP_FAMILY:
        audit = inputs.first_dip_detector_audit
        assert audit is not None and intent.opportunity_key is not None
        adaptive_opportunity = built.request.opportunity_key
        if (
            adaptive_opportunity is None
            or adaptive_opportunity.key_sha256
            != audit.detector_opportunity_key_sha256
        ):
            _reject("captured_paper_first_dip_opportunity_binding_mismatch")
        final_boundary = _db_now(db)
        final_resolution = (
            _resolve_first_dip_final_admission_with_active_provider(
                adaptive_request=built.request,
                detector_policy=audit.detector_policy,
                symbol=route.symbol,
                adaptive_decision_at=bundle.decision_as_of,
                run_id=proof.run_id,
                generation=proof.generation,
                adaptive_decision_id=intent.decision_id,
                adaptive_input_prefix_root_sha256=(
                    proof.input_prefix_root_sha256
                ),
                adaptive_request_sha256=built.request.request_sha256,
                opportunity_key_sha256=adaptive_opportunity.key_sha256,
                final_boundary_available_at=final_boundary,
                expected_execution_surface=(
                    _CAPTURED_FIRST_DIP_AUTHORITY_SURFACE
                ),
                detector_policy_sha256=audit.detector_policy.policy_sha256,
                detector_authority_source=audit.detector_authority_source,
                detector_receipt_binding_sha256=(
                    audit.detector_receipt_binding_sha256
                ),
                detector_opportunity_key_sha256=(
                    audit.detector_opportunity_key_sha256
                ),
            )
        )
        verified_final = _verify_first_dip_final_admission_resolution(
            final_resolution
        )
        if verified_final.admitted is not True:
            _reject(
                "captured_paper_first_dip_final_receipt_rejected:"
                + verified_final.reason
            )
        first_dip_final_audit = verified_final.to_audit_dict()
        if not callable(final_executed_read_provider):
            _reject("captured_paper_first_dip_final_executed_reads_unavailable")
        try:
            final_authority = final_executed_read_provider()
        except Exception as exc:
            raise CapturedPaperAdmissionRejected(
                "captured_paper_first_dip_final_executed_reads_unavailable"
            ) from exc
        if type(final_authority) is not CapturedPaperFinalExecutedReadAuthority:
            _reject("captured_paper_first_dip_final_executed_reads_unavailable")
        frontier = final_authority.frontier
        if (
            frontier.adaptive_request_sha256 != built.request.request_sha256
            or frontier.opportunity_key_sha256
            != adaptive_opportunity.key_sha256
            or frontier.policy_sha256 != audit.detector_policy.policy_sha256
        ):
            _reject("captured_paper_first_dip_final_frontier_mismatch")
        try:
            final_executed_binding = (
                verify_captured_paper_executed_read_inventory(
                    inventory=final_authority.inventory,
                    captured_reads=final_authority.captured_reads,
                    active_input_attestation=(
                        final_authority.active_input_attestation
                    ),
                    request=inputs.post_commit_request,
                    material_sha256=phase_one_material_sha256,
                    require_exact_attestation=True,
                )
            )
        except CapturedPaperPhaseOneHandoffError as exc:
            raise CapturedPaperAdmissionRejected(
                "captured_paper_first_dip_final_executed_reads_mismatch"
            ) from exc
        final_executed_frontier_sha256 = frontier.frontier_sha256
    elif final_executed_read_provider is not None:
        _reject("captured_paper_non_first_dip_final_read_provider_forbidden")

    row_locks.observe(
        AccountRiskRowLockStage.FILL_ACTIVITY_OR_CYCLE_SETTLEMENT,
        sort_key=(0, 0),
    )
    _lock_fill_and_cycle_rows(
        db,
        account_identity_sha256=bundle.account_snapshot.account_identity_sha256,
    )
    # One post-evidence database clock owns every phase-one authority mutation.
    # This is deliberately sampled after the fresh first-dip checkpoint and
    # before the action lease/session-arm checks, so slow evidence resolution
    # cannot commit an already-expired arm or backdate its claim lease.
    admission_at = _db_now(db)

    order_request = _canonical_order_request(
        inputs=inputs,
        quantity_shares=int(built.resolution.quantity_shares),
    )
    metadata = _action_claim_metadata(
        inputs=inputs,
        built=built,
        order_request=order_request,
        first_dip_final_audit=first_dip_final_audit,
    )

    row_locks.observe(
        AccountRiskRowLockStage.ACTION_CLAIM,
        sort_key=(route.symbol,),
    )
    _acquire_new_action_claim(
        db,
        inputs=inputs,
        metadata=metadata,
        claimed_at=admission_at,
    )
    row_locks.observe(
        AccountRiskRowLockStage.AUTOMATION_SESSION,
        sort_key=(route.session_id,),
    )
    session_row = _lock_and_verify_session(
        db,
        inputs=inputs,
        decision_as_of=admission_at,
    )

    opportunity_id = 0
    if intent.opportunity_key is not None:
        row_locks.observe(
            AccountRiskRowLockStage.OPPORTUNITY_CLAIM,
            sort_key=(
                intent.opportunity_key.account_scope,
                intent.opportunity_key.symbol,
                intent.opportunity_key.trading_date,
                intent.opportunity_key.setup_family,
                0,
            ),
        )
        opportunity_id = _lock_opportunity(
            db,
            inputs=inputs,
            decision_as_of=bundle.decision_as_of,
        )

    # The external breaker read intentionally occurs before this transaction,
    # but lock acquisition and final setup evaluation can consume the receipt's
    # short authority window.  Recheck it with the database clock at the last
    # mutation-free seam.  A failure raises inside the caller-owned transaction,
    # rolling back the session/action/opportunity locks before any reservation,
    # opportunity consumption, or outbox publication can survive.
    try:
        financial_breaker_receipt.verify_for_request(
            inputs.post_commit_request,
            phase="pre_reservation",
            now=_db_now(db),
            require_allowed=True,
        )
    except CapturedPaperFinancialBreakerError as exc:
        raise CapturedPaperAdmissionRejected(
            "captured_paper_pre_reservation_financial_breaker_rejected_at_reserve:"
            + exc.reason
        ) from exc

    decision = store.reserve(
        built.request,
        session=db,
        locked_snapshot=bundle.locked_risk_snapshot,
        prepared_resolution=built.resolution,
        prepared_decision_packet=built.decision_packet,
        locked_alpaca_paper_bundle=bundle,
    )
    if (
        decision.admission_accepted is not True
        or decision.reservation_id is None
        or decision.idempotent_retry
        or decision.decision_packet_sha256
        != built.resolution.decision_packet_sha256
        or int(decision.quantity_shares) != int(built.resolution.quantity_shares)
    ):
        _reject("captured_paper_adaptive_reservation_not_accepted")

    packet = db.scalar(
        select(AdaptiveRiskDecisionPacket).where(
            AdaptiveRiskDecisionPacket.decision_packet_sha256
            == decision.decision_packet_sha256
        )
    )
    reservation = db.scalar(
        select(AdaptiveRiskReservation).where(
            AdaptiveRiskReservation.reservation_id == decision.reservation_id
        )
    )
    if not (
        packet is not None
        and reservation is not None
        and packet.reservation_request_sha256 == built.request.request_sha256
        and packet.admission_accepted is True
        and packet.account_identity_sha256
        == bundle.account_snapshot.account_identity_sha256
        and packet.policy_sha256 == inputs.policy_spec.policy.policy_sha256
        and packet.effective_config_sha256 == route.config_sha256
        and packet.code_build_sha256 == route.code_build_sha256
        and packet.feature_flags_sha256 == intent.feature_flags_sha256
        and reservation.state == "reserved"
        and reservation.broker_order_id is None
        and reservation.broker_source is None
        and reservation.account_scope == route.account_scope
        and reservation.symbol == route.symbol
        and reservation.setup_family == intent.setup_family
        and int(reservation.planned_quantity_shares)
        == int(decision.quantity_shares)
    ):
        _reject("captured_paper_durable_reservation_binding_mismatch")
    if intent.opportunity_key is not None:
        opportunity = db.get(
            AdaptiveRiskOpportunityClaim,
            reservation.opportunity_claim_id,
        )
        if not (
            opportunity is not None
            and opportunity.status == "reserved"
            and opportunity.reservation_id == decision.reservation_id
            and opportunity.account_scope == intent.opportunity_key.account_scope
            and opportunity.symbol == intent.opportunity_key.symbol
            and opportunity.trading_date == intent.opportunity_key.trading_date
            and opportunity.setup_family == intent.opportunity_key.setup_family
            and (opportunity_id == 0 or opportunity.id == opportunity_id)
        ):
            _reject("captured_paper_durable_opportunity_binding_mismatch")

    order_request_sha256 = _sha256_json(order_request)
    admission_record = {
        "schema_version": CAPTURED_PAPER_ADMISSION_SCHEMA_VERSION,
        "completion_sha256": inputs.post_commit_request.completion_sha256,
        "phase_one_material_sha256": phase_one_receipt.material_sha256,
        "executed_read_inventory_sha256": (
            phase_one_receipt.executed_read_inventory_sha256
        ),
        "executed_material_sha256": (
            phase_one_receipt.executed_material_sha256
        ),
        "final_first_dip_executed_read_inventory_sha256": (
            None
            if final_executed_binding is None
            else final_executed_binding.inventory_sha256
        ),
        "final_first_dip_executed_material_sha256": (
            None
            if final_executed_binding is None
            else final_executed_binding.executed_material_sha256
        ),
        "final_first_dip_capture_frontier_sha256": (
            final_executed_frontier_sha256
        ),
        "pre_reservation_financial_breaker_receipt_sha256": (
            financial_breaker_receipt.receipt_sha256
        ),
        "pre_reservation_financial_breaker_receipt": (
            financial_breaker_receipt.to_payload()
        ),
        "pre_reservation_financial_breaker_evidence_sha256": (
            financial_breaker_receipt.breaker_evidence_sha256
        ),
        "pre_reservation_financial_breaker_checked_at": (
            financial_breaker_receipt.checked_at.isoformat()
        ),
        "pre_reservation_financial_breaker_issued_at": (
            financial_breaker_receipt.issued_at.isoformat()
        ),
        "pre_reservation_financial_breaker_valid_until": (
            financial_breaker_receipt.valid_until.isoformat()
        ),
        "pre_reservation_financial_breaker_evaluator_id": (
            financial_breaker_receipt.evaluator_id
        ),
        "route_token_sha256": route.route_token_sha256,
        "intent_sha256": intent.intent_sha256,
        "runtime_generation": route.runtime_generation,
        "runtime_capture_receipt_sha256": route.capture_receipt_sha256,
        "active_input_attestation_sha256": proof.attestation_sha256,
        "capture_run_id": proof.run_id,
        "capture_generation": proof.generation,
        "admitted_at": admission_at.isoformat(),
        "reservation_id": str(decision.reservation_id),
        "decision_packet_sha256": decision.decision_packet_sha256,
        "reservation_request_sha256": built.request.request_sha256,
        "adaptive_input_evidence_sha256": packet.evidence_sha256,
        "account_identity_sha256": packet.account_identity_sha256,
        "quantity_shares": int(decision.quantity_shares),
        "order_request_sha256": order_request_sha256,
        "operational_policy_sha256": inputs.operational_policy.policy_sha256,
        "first_dip_final_admission_sha256": (
            None
            if first_dip_final_audit is None
            else first_dip_final_audit["binding_sha256"]
        ),
        "lock_order": list(CAPTURED_PAPER_CANONICAL_LOCK_ORDER),
    }
    admission_record_sha256 = _sha256_json(admission_record)
    binding_patch = {
        "adaptive_risk_reservation_id": str(decision.reservation_id),
        "captured_paper_admission_record_sha256": admission_record_sha256,
        "adaptive_input_evidence_sha256": packet.evidence_sha256,
    }
    action_patch = db.execute(
        text(
            """
            UPDATE broker_symbol_action_claims
               SET metadata_json = metadata_json || CAST(:binding AS jsonb),
                   updated_at = :now
             WHERE account_scope = :scope AND symbol = :symbol
               AND claim_token = :claim_token AND action = 'entry'
               AND phase = 'claimed' AND owner_session_id = :session_id
               AND client_order_id = :client_order_id
            """
        ),
        {
            "binding": _canonical_json(binding_patch),
            "now": admission_at,
            "scope": route.account_scope,
            "symbol": route.symbol,
            "claim_token": intent.symbol_claim_token,
            "session_id": route.session_id,
            "client_order_id": intent.client_order_id,
        },
    )
    if int(action_patch.rowcount or 0) != 1:
        _reject("captured_paper_action_claim_binding_patch_failed")
    snapshot = dict(session_row.risk_snapshot_json or {})
    snapshot["captured_paper_admission"] = {
        **admission_record,
        "admission_record_sha256": admission_record_sha256,
        "status": "admitted_pending_transport",
    }
    session_row.risk_snapshot_json = snapshot
    db.flush([session_row])

    committed_at = _db_now(db)
    transport_authority = CapturedPaperTransportAuthority(
        completion_sha256=inputs.post_commit_request.completion_sha256,
        account_scope=route.account_scope,
        expected_account_id=route.expected_account_id,
        account_identity_sha256=packet.account_identity_sha256,
        session_id=route.session_id,
        symbol=route.symbol,
        client_order_id=intent.client_order_id,
        binder_id=intent.binder_id,
        action_claim_token=intent.symbol_claim_token,
        reservation_id=str(decision.reservation_id),
        decision_packet_sha256=decision.decision_packet_sha256,
        reservation_request_sha256=built.request.request_sha256,
        admission_evidence_sha256=packet.evidence_sha256,
        broker_request_sha256=order_request_sha256,
        opportunity_key_sha256=(
            None
            if intent.opportunity_key is None
            else intent.opportunity_key.opportunity_key_sha256
        ),
    )
    outbox_record = persist_captured_paper_post_commit_request(
        db,
        request=inputs.post_commit_request,
        authority=transport_authority,
        order_request=order_request,
        order_request_sha256=order_request_sha256,
        admission_record=admission_record,
        admission_record_sha256=admission_record_sha256,
        quantity_shares=int(decision.quantity_shares),
        structural_risk_usd=str(decision.structural_risk_usd),
        gross_notional_usd=str(decision.gross_notional_usd),
        buying_power_impact_usd=str(decision.buying_power_impact_usd),
        operational_policy_sha256=inputs.operational_policy.policy_sha256,
        committed_at=committed_at,
        lock_order=CAPTURED_PAPER_CANONICAL_LOCK_ORDER,
        reconciliation_retry_delay_seconds=(
            inputs.operational_policy.reconciliation_retry_delay_seconds
        ),
        reconciliation_health_escalation_delay_seconds=(
            inputs.operational_policy
            .reconciliation_health_escalation_delay_seconds
        ),
        max_attempts=inputs.operational_policy.outbox_max_attempts,
        max_reconciliation_attempts=(
            inputs.operational_policy.outbox_max_reconciliation_attempts
        ),
    )
    if (
        outbox_record.status != "pending"
        or outbox_record.completion_sha256
        != inputs.post_commit_request.completion_sha256
        or outbox_record.durable_transport.authority.authority_sha256
        != transport_authority.authority_sha256
        or outbox_record.durable_transport.order_request_sha256
        != order_request_sha256
    ):
        _reject("captured_paper_outbox_persistence_mismatch")
    committed_phase_one = (
        commit_captured_paper_phase_one_outbox_in_transaction(
            db,
            request=inputs.post_commit_request,
            material_sha256=phase_one_material_sha256,
            locked_receipt=phase_one_receipt,
        )
    )
    if committed_phase_one.state != "outbox_committed":
        _reject("captured_paper_phase_one_commit_mismatch")
    return _PendingCapturedPaperAdmission(
        values={
            "post_commit_request": inputs.post_commit_request,
            "reservation_id": str(decision.reservation_id),
            "decision_packet_sha256": decision.decision_packet_sha256,
            "reservation_request_sha256": built.request.request_sha256,
            "adaptive_input_evidence_sha256": packet.evidence_sha256,
            "account_identity_sha256": packet.account_identity_sha256,
            "quantity_shares": int(decision.quantity_shares),
            "structural_risk_usd": str(decision.structural_risk_usd),
            "gross_notional_usd": str(decision.gross_notional_usd),
            "buying_power_impact_usd": str(decision.buying_power_impact_usd),
            "order_request": order_request,
            "order_request_sha256": order_request_sha256,
            "admission_record_sha256": admission_record_sha256,
            "committed_at": committed_at,
        }
    )


def commit_captured_paper_admission(
    bind: Engine,
    *,
    inputs: CapturedPaperAdmissionInputs,
    phase_one_material_sha256: str,
    executed_read_inventory: ExecutedCaptureReadInventory,
    executed_captured_reads: tuple[CapturedReadResult, ...],
    financial_breaker_receipt: CapturedPaperFinancialBreakerReceipt,
    financial_breaker_verification_at: datetime,
    final_executed_read_provider: (
        Callable[[], CapturedPaperFinalExecutedReadAuthority] | None
    ) = None,
) -> CommittedCapturedPaperAdmission:
    """Commit one admission and only then expose its typed post-commit handoff.

    ``bind`` must be the PostgreSQL engine used by the durable runner.  This
    function always creates and owns a fresh Session; callers cannot smuggle an
    outer transaction or keep a row lock alive into the later broker handler.
    """

    if not isinstance(bind, Engine) or bind.dialect.name != "postgresql":
        _reject("captured_paper_postgresql_engine_required")
    try:
        proof, _authority = _verify_pure_inputs(inputs)
    except CapturedPaperAdmissionRejected:
        raise
    except (TypeError, ValueError) as exc:
        raise CapturedPaperAdmissionRejected(
            "captured_paper_pure_input_validation_failed"
        ) from exc
    try:
        verify_captured_paper_executed_read_inventory(
            inventory=inputs.executed_read_inventory,
            captured_reads=inputs.predecision_captured_reads,
            active_input_attestation=proof,
            request=inputs.post_commit_request,
            material_sha256=phase_one_material_sha256,
            require_exact_attestation=True,
        )
        verify_captured_paper_executed_read_inventory(
            inventory=executed_read_inventory,
            captured_reads=executed_captured_reads,
            active_input_attestation=proof,
            request=inputs.post_commit_request,
            material_sha256=phase_one_material_sha256,
            require_exact_attestation=False,
        )
    except CapturedPaperPhaseOneHandoffError as exc:
        raise CapturedPaperAdmissionRejected(
            "captured_paper_executed_read_inventory_rejected:"
            + exc.reason
        ) from exc
    if type(financial_breaker_receipt) is not CapturedPaperFinancialBreakerReceipt:
        _reject("captured_paper_pre_reservation_financial_breaker_unavailable")
    try:
        financial_breaker_receipt.verify_for_request(
            inputs.post_commit_request,
            phase="pre_reservation",
            now=financial_breaker_verification_at,
            require_allowed=True,
        )
    except CapturedPaperFinancialBreakerError as exc:
        raise CapturedPaperAdmissionRejected(
            "captured_paper_pre_reservation_financial_breaker_rejected:"
            + exc.reason
        ) from exc

    factory = sessionmaker(
        bind=bind,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    db = factory()
    try:
        try:
            with db.begin():
                pending = _admit_in_transaction(
                    db,
                    inputs=inputs,
                    proof=proof,
                    phase_one_material_sha256=phase_one_material_sha256,
                    executed_read_inventory=executed_read_inventory,
                    executed_captured_reads=executed_captured_reads,
                    financial_breaker_receipt=financial_breaker_receipt,
                    final_executed_read_provider=(
                        final_executed_read_provider
                    ),
                )
        except CapturedPaperAdmissionRejected:
            raise
        except (
            AdaptiveRiskBuilderError,
            AdaptiveRiskContractError,
            AdaptiveReservationError,
            CapturedAdaptiveRiskCoverageUnavailable,
            CapturedPaperIntentContractError,
            CapturedPaperOutboxError,
            CapturedPaperPhaseOneHandoffError,
            FirstDipTapeDecisionProviderError,
            CaptureContractError,
            CapturedAlpacaPaperReadError,
            ValueError,
        ) as exc:
            reason = getattr(exc, "reason", None) or str(exc) or type(exc).__name__
            raise CapturedPaperAdmissionRejected(
                "captured_paper_admission_unavailable:" + reason
            ) from exc
    finally:
        db.close()
    # The context above has committed successfully.  Constructing this public
    # type any earlier would falsely label an uncommitted transaction as ready
    # for post-commit broker work.
    return CommittedCapturedPaperAdmission(**dict(pending.values))


def _committed_admission_from_durable_bundle(
    bundle: CapturedPaperDurableTransportBundle,
    *,
    request: CapturedPaperPostCommitRequest,
) -> CommittedCapturedPaperAdmission:
    """Rebuild the public commit receipt from immutable outbox bytes only."""

    if type(bundle) is not CapturedPaperDurableTransportBundle:
        _reject("captured_paper_committed_readback_bundle_invalid")
    if type(request) is not CapturedPaperPostCommitRequest:
        _reject("captured_paper_committed_readback_request_invalid")
    request.verify()
    bundle.request.verify()
    if (
        bundle.request.completion_sha256 != request.completion_sha256
        or bundle.request.to_canonical_json() != request.to_canonical_json()
    ):
        _reject("captured_paper_committed_readback_request_mismatch")
    committed = dict(bundle.committed_admission)
    exact_fields = {
        "schema_version",
        "completion_sha256",
        "payload_sha256",
        "route_token_sha256",
        "intent_sha256",
        "reservation_id",
        "decision_packet_sha256",
        "reservation_request_sha256",
        "adaptive_input_evidence_sha256",
        "account_identity_sha256",
        "quantity_shares",
        "structural_risk_usd",
        "gross_notional_usd",
        "buying_power_impact_usd",
        "order_request_sha256",
        "transport_authority_sha256",
        "transport_instruction_sha256",
        "admission_record_sha256",
        "operational_policy_sha256",
        "reconciliation_retry_delay_seconds",
        "reconciliation_health_escalation_delay_seconds",
        "committed_at",
        "lock_order",
    }
    if set(committed) != exact_fields:
        _reject("captured_paper_committed_readback_shape_invalid")
    route = request.intent.route_token
    exact_bindings = {
        "schema_version": DURABLE_COMMITTED_ADMISSION_SCHEMA_VERSION,
        "completion_sha256": request.completion_sha256,
        "payload_sha256": hashlib.sha256(
            request.to_canonical_json().encode("utf-8")
        ).hexdigest(),
        "route_token_sha256": route.route_token_sha256,
        "intent_sha256": request.intent.intent_sha256,
        "reservation_id": bundle.authority.reservation_id,
        "decision_packet_sha256": bundle.authority.decision_packet_sha256,
        "reservation_request_sha256": (
            bundle.authority.reservation_request_sha256
        ),
        "adaptive_input_evidence_sha256": (
            bundle.authority.admission_evidence_sha256
        ),
        "account_identity_sha256": bundle.authority.account_identity_sha256,
        "order_request_sha256": bundle.order_request_sha256,
        "transport_authority_sha256": bundle.authority.authority_sha256,
        "transport_instruction_sha256": (
            bundle.transport_instruction_sha256
        ),
        "admission_record_sha256": bundle.admission_record_sha256,
        "lock_order": list(CAPTURED_PAPER_CANONICAL_LOCK_ORDER),
    }
    if any(committed.get(name) != value for name, value in exact_bindings.items()):
        _reject("captured_paper_committed_readback_binding_mismatch")
    committed_at_raw = str(committed.get("committed_at") or "")
    try:
        committed_at = datetime.fromisoformat(
            committed_at_raw.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise CapturedPaperAdmissionRejected(
            "captured_paper_committed_readback_clock_invalid"
        ) from exc
    if committed_at.tzinfo is None:
        _reject("captured_paper_committed_readback_clock_invalid")
    return CommittedCapturedPaperAdmission(
        post_commit_request=request,
        reservation_id=committed["reservation_id"],
        decision_packet_sha256=committed["decision_packet_sha256"],
        reservation_request_sha256=committed["reservation_request_sha256"],
        adaptive_input_evidence_sha256=committed[
            "adaptive_input_evidence_sha256"
        ],
        account_identity_sha256=committed["account_identity_sha256"],
        quantity_shares=committed["quantity_shares"],
        structural_risk_usd=committed["structural_risk_usd"],
        gross_notional_usd=committed["gross_notional_usd"],
        buying_power_impact_usd=committed["buying_power_impact_usd"],
        order_request=bundle.order_request,
        order_request_sha256=bundle.order_request_sha256,
        admission_record_sha256=bundle.admission_record_sha256,
        committed_at=committed_at,
        lock_order=tuple(committed["lock_order"]),
    )


def read_committed_captured_paper_admission(
    bind: Engine,
    *,
    request: CapturedPaperPostCommitRequest,
) -> CommittedCapturedPaperAdmission | None:
    """Return exact durable admission truth after a lost local acknowledgement.

    Absence is the only retryable result.  Any malformed, mismatched or
    unreadable outbox state raises fail closed; callers must never infer that a
    failed read means the admission transaction did not commit.
    """

    if not isinstance(bind, Engine) or bind.dialect.name != "postgresql":
        _reject("captured_paper_postgresql_engine_required")
    if type(request) is not CapturedPaperPostCommitRequest:
        _reject("captured_paper_committed_readback_request_invalid")
    request.verify()
    factory = sessionmaker(
        bind=bind,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    db = factory()
    try:
        with db.begin():
            try:
                record = load_captured_paper_outbox(
                    db,
                    completion_sha256=request.completion_sha256,
                    for_update=False,
                )
            except CapturedPaperOutboxNotFoundError:
                return None
            return _committed_admission_from_durable_bundle(
                record.durable_transport,
                request=request,
            )
    finally:
        db.close()


__all__ = (
    "CAPTURED_FIRST_DIP_DETECTOR_AUDIT_SCHEMA_VERSION",
    "CAPTURED_PAPER_ADMISSION_SCHEMA_VERSION",
    "CAPTURED_PAPER_CANONICAL_LOCK_ORDER",
    "CAPTURED_PAPER_OPERATIONAL_POLICY_SCHEMA_VERSION",
    "CapturedFirstDipDetectorAudit",
    "CapturedPaperAdmissionError",
    "CapturedPaperAdmissionInputs",
    "CapturedPaperAdmissionRejected",
    "CapturedPaperOperationalPolicy",
    "CommittedCapturedPaperAdmission",
    "commit_captured_paper_admission",
    "read_committed_captured_paper_admission",
)
