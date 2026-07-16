"""Fail-closed production assembly for one captured Alpaca PAPER decision.

The momentum FSM, not this module, chooses a setup.  A prior FSM tick first
persists a candidate snapshot.  This module then orchestrates three sharply
separated phases:

1. read and close one durable candidate transaction;
2. capture provider/account evidence with no database transaction held; and
3. build an immutable selection/admission packet which the real FSM must
   revalidate again inside its final session lock.

No method in this module has a provider, broker, or current-database fallback.
The injected capture scope must return original ``CapturedReadResult`` objects
and an exact provider scope for the later FSM tick.  Missing inputs are a local
``COVERAGE_UNAVAILABLE`` decision, never a fabricated fact or global dark flag.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
import math
from pathlib import Path
import re
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence
import uuid

from .adaptive_risk_reservation import AlpacaPaperBrokerAccountFacts
from .captured_adaptive_risk_source import (
    CapturedAccountRiskReceipt,
    CapturedAdaptiveRiskDecisionIdentity,
    CapturedAdaptiveRiskEconomicInputs,
    CapturedAdaptiveRiskEvidenceSet,
    CapturedAdaptiveRiskPolicySpec,
    CapturedExactBbo,
    captured_adaptive_risk_fact_payloads,
)
from .captured_alpaca_paper_adapter import (
    CapturedAlpacaPaperAdapter,
    CapturedAlpacaPaperObservationAdapter,
    CapturedAlpacaPaperReadError,
)
from .captured_paper_admission import (
    CapturedFirstDipDetectorAudit,
    CapturedPaperAdmissionInputs,
    CapturedPaperOperationalPolicy,
)
from .captured_paper_dispatcher import CapturedPaperDispatchRequest
from .captured_paper_entry_intent import (
    CapturedPaperConfirmedArmGeneration,
    CapturedPaperOpportunityKey,
)
from .captured_paper_selection import (
    CapturedPaperObservationContext,
    CapturedPaperSelectionContext,
    captured_paper_observation_generation_sha256,
)
from .first_dip_tape_policy import FirstDipTapePolicy
from .live_replay_capture import (
    CapturedReadResult,
    ExecutedCaptureReadInventory,
    FirstDipFinalReadProvider,
    build_executed_capture_read_inventory,
)
from .replay_capture_contract import (
    ActiveCaptureInputPrefixAttestation,
    CaptureContractError,
    CaptureStream,
    FSMDependencyProfile,
    canonical_json_bytes,
    freeze_canonical_json,
    sha256_json,
    verify_active_capture_input_attestation,
)
from ..venue.alpaca_spot import quantize_alpaca_equity_limit_price
from ..venue.protocol import FreshnessMeta, NormalizedProduct


UTC = timezone.utc
CAPTURED_PAPER_DURABLE_CANDIDATE_SCHEMA_VERSION = (
    "chili.captured-paper-durable-candidate.v4"
)
CAPTURED_PAPER_PRODUCTION_CAPTURE_SCHEMA_VERSION = (
    "chili.captured-paper-production-capture.v1"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_STATES = frozenset({"live_entry_candidate", "live_pending_entry"})
_FIRST_DIP_SETUP = "first_dip_reclaim"
_OBSERVATION_STATES = frozenset({"queued_live", "watching_live"})
_OBSERVATION_REQUIRED_STREAMS = frozenset(
    {
        CaptureStream.ACCOUNT_RISK_SNAPSHOT,
        CaptureStream.ALPACA_NBBO_QUOTE,
        CaptureStream.ADMISSION_ELIGIBILITY,
        CaptureStream.FSM_DECISION,
        CaptureStream.MARKET_SESSION_STATE,
        CaptureStream.HALT_LULD_STATE,
        CaptureStream.CONFIG_SNAPSHOT,
        CaptureStream.FEATURE_FLAG_SNAPSHOT,
        CaptureStream.CODE_BUILD,
    }
)
CAPTURED_PAPER_DURABLE_OBSERVATION_SCHEMA_VERSION = (
    "chili.captured-paper-durable-observation.v1"
)
CAPTURED_PAPER_OBSERVATION_CAPTURE_SCHEMA_VERSION = (
    "chili.captured-paper-observation-capture.v1"
)
CAPTURED_PAPER_ADMISSION_ELIGIBILITY_SCHEMA_VERSION = (
    "chili.captured-paper-admission-eligibility.v1"
)


class CapturedPaperProductionMaterialUnavailable(CaptureContractError):
    """Stable decision-local coverage/state rejection before reservation."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "captured_paper_material_coverage_unavailable")
        super().__init__(self.reason)


def _unavailable(reason: str) -> None:
    raise CapturedPaperProductionMaterialUnavailable(reason)


def _utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _unavailable(f"{field_name}_clock_unavailable")
    return value.astimezone(UTC)


def _parse_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        _unavailable(f"{field_name}_clock_unavailable")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        _unavailable(f"{field_name}_clock_unavailable")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _sha(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        _unavailable(f"{field_name}_sha256_unavailable")
    return normalized


def _finite(value: Any, field_name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        _unavailable(f"{field_name}_unavailable")
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        _unavailable(f"{field_name}_unavailable")
    if not math.isfinite(normalized) or (positive and normalized <= 0.0):
        _unavailable(f"{field_name}_unavailable")
    return normalized


def _canonical_payload(event: Any) -> str:
    return canonical_json_bytes(dict(event.payload)).decode("utf-8")


@dataclass(frozen=True, slots=True)
class CapturedPaperDurableCandidateSnapshot:
    """Read-only prior-tick candidate identity; contains no provider result."""

    dispatch_provenance_sha256: str
    session_id: int
    symbol: str
    execution_family: str
    state: str
    correlation_id: str
    variant_id: int
    session_updated_at: datetime
    viability_updated_at: datetime
    viability_score: float
    viability_payload: Mapping[str, Any]
    viability_payload_sha256: str
    execution_readiness_sha256: str
    entry_place_count: int
    client_order_id: str
    setup_family: str
    structural_stop_price: float
    trigger_reason: str
    trigger_debug: Mapping[str, Any]
    confirmed_arm_marker: Mapping[str, Any]
    session_snapshot_sha256: str
    candidate_sha256: str = field(init=False)
    schema_version: str = CAPTURED_PAPER_DURABLE_CANDIDATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURED_PAPER_DURABLE_CANDIDATE_SCHEMA_VERSION:
            _unavailable("candidate_schema_unavailable")
        object.__setattr__(
            self,
            "dispatch_provenance_sha256",
            _sha(self.dispatch_provenance_sha256, "candidate_dispatch"),
        )
        if isinstance(self.session_id, bool) or int(self.session_id) <= 0:
            _unavailable("candidate_session_unavailable")
        object.__setattr__(self, "session_id", int(self.session_id))
        symbol = str(self.symbol or "").strip().upper()
        family = str(self.execution_family or "").strip().lower()
        state = str(self.state or "").strip().lower()
        if not symbol or family != "alpaca_spot" or state not in _CANDIDATE_STATES:
            _unavailable("candidate_route_or_state_unavailable")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "execution_family", family)
        object.__setattr__(self, "state", state)
        correlation = str(self.correlation_id or "").strip()
        if not correlation:
            _unavailable("candidate_correlation_unavailable")
        object.__setattr__(self, "correlation_id", correlation)
        if isinstance(self.variant_id, bool) or int(self.variant_id) <= 0:
            _unavailable("candidate_variant_unavailable")
        object.__setattr__(self, "variant_id", int(self.variant_id))
        object.__setattr__(
            self,
            "session_updated_at",
            _utc(self.session_updated_at, "candidate_session_updated"),
        )
        object.__setattr__(
            self,
            "viability_updated_at",
            _utc(self.viability_updated_at, "candidate_viability_updated"),
        )
        quality = _finite(self.viability_score, "candidate_viability_score")
        if not 0.0 <= quality <= 1.0:
            _unavailable("candidate_viability_score_unavailable")
        object.__setattr__(self, "viability_score", quality)
        if not isinstance(self.viability_payload, Mapping) or not self.viability_payload:
            _unavailable("candidate_viability_payload_unavailable")
        viability_payload = freeze_canonical_json(dict(self.viability_payload))
        viability_payload_sha256 = _sha(
            self.viability_payload_sha256,
            "candidate_viability_payload",
        )
        if sha256_json(viability_payload) != viability_payload_sha256:
            _unavailable("candidate_viability_payload_mismatch")
        object.__setattr__(
            self,
            "viability_payload",
            viability_payload,
        )
        object.__setattr__(
            self,
            "viability_payload_sha256",
            viability_payload_sha256,
        )
        readiness = viability_payload.get("execution_readiness_json")
        if not isinstance(readiness, Mapping) or not readiness:
            _unavailable("candidate_execution_readiness_unavailable")
        readiness_sha256 = _sha(
            self.execution_readiness_sha256,
            "candidate_execution_readiness",
        )
        if sha256_json(readiness) != readiness_sha256:
            _unavailable("candidate_execution_readiness_mismatch")
        object.__setattr__(self, "execution_readiness_sha256", readiness_sha256)
        if isinstance(self.entry_place_count, bool) or int(self.entry_place_count) <= 0:
            _unavailable("candidate_place_generation_unavailable")
        object.__setattr__(self, "entry_place_count", int(self.entry_place_count))
        cid = str(self.client_order_id or "").strip()
        setup = str(self.setup_family or "").strip().lower()
        reason = str(self.trigger_reason or "").strip()
        if not cid or not setup or not reason:
            _unavailable("candidate_trigger_identity_unavailable")
        object.__setattr__(self, "client_order_id", cid)
        object.__setattr__(self, "setup_family", setup)
        object.__setattr__(self, "trigger_reason", reason)
        stop = _finite(
            self.structural_stop_price,
            "candidate_structural_stop",
            positive=True,
        )
        object.__setattr__(self, "structural_stop_price", stop)
        if not isinstance(self.trigger_debug, Mapping) or not isinstance(
            self.confirmed_arm_marker, Mapping
        ):
            _unavailable("candidate_trigger_or_arm_unavailable")
        object.__setattr__(
            self,
            "trigger_debug",
            freeze_canonical_json(dict(self.trigger_debug)),
        )
        object.__setattr__(
            self,
            "confirmed_arm_marker",
            freeze_canonical_json(dict(self.confirmed_arm_marker)),
        )
        object.__setattr__(
            self,
            "session_snapshot_sha256",
            _sha(self.session_snapshot_sha256, "candidate_session_snapshot"),
        )
        object.__setattr__(self, "candidate_sha256", sha256_json(self.to_payload()))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dispatch_provenance_sha256": self.dispatch_provenance_sha256,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "execution_family": self.execution_family,
            "state": self.state,
            "correlation_id": self.correlation_id,
            "variant_id": self.variant_id,
            "session_updated_at": self.session_updated_at.isoformat(),
            "viability_updated_at": self.viability_updated_at.isoformat(),
            "viability_score": self.viability_score,
            "viability_payload": dict(self.viability_payload),
            "viability_payload_sha256": self.viability_payload_sha256,
            "execution_readiness_sha256": self.execution_readiness_sha256,
            "entry_place_count": self.entry_place_count,
            "client_order_id": self.client_order_id,
            "setup_family": self.setup_family,
            "structural_stop_price": self.structural_stop_price,
            "trigger_reason": self.trigger_reason,
            "trigger_debug": dict(self.trigger_debug),
            "confirmed_arm_marker": dict(self.confirmed_arm_marker),
            "session_snapshot_sha256": self.session_snapshot_sha256,
        }

    def confirmed_arm(self) -> CapturedPaperConfirmedArmGeneration:
        marker = dict(self.confirmed_arm_marker)
        try:
            return CapturedPaperConfirmedArmGeneration(
                session_id=marker["session_id"],
                arm_token=marker["arm_token"],
                expires_at=_parse_utc(marker["expires_at_utc"], "candidate_arm_expiry"),
                symbol_claim_token=marker["alpaca_symbol_claim_token"],
                account_scope=marker["alpaca_account_scope"],
                expected_account_id=marker["alpaca_account_id"],
                confirmed_at=_parse_utc(
                    marker["confirmed_at_utc"], "candidate_arm_confirmation"
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CapturedPaperProductionMaterialUnavailable(
                "candidate_confirmed_arm_unavailable"
            ) from exc


@dataclass(frozen=True, slots=True)
class CapturedPaperDurableObservationSnapshot:
    """Hash-bound WATCHING/QUEUED DB decision receipt before provider I/O."""

    dispatch_provenance_sha256: str
    session_id: int
    symbol: str
    execution_family: str
    state: str
    correlation_id: str
    variant_id: int
    session_updated_at: datetime
    risk_snapshot: Mapping[str, Any]
    viability_payload: Mapping[str, Any]
    variant_payload: Mapping[str, Any]
    confirmed_arm_marker: Mapping[str, Any]
    risk_snapshot_sha256: str
    viability_payload_sha256: str
    variant_payload_sha256: str
    confirmed_arm_marker_sha256: str
    observation_generation_sha256: str
    observation_decision_id: str = field(init=False)
    observation_snapshot_sha256: str = field(init=False)
    schema_version: str = CAPTURED_PAPER_DURABLE_OBSERVATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURED_PAPER_DURABLE_OBSERVATION_SCHEMA_VERSION:
            _unavailable("observation_schema_unavailable")
        object.__setattr__(
            self,
            "dispatch_provenance_sha256",
            _sha(self.dispatch_provenance_sha256, "observation_dispatch"),
        )
        if isinstance(self.session_id, bool) or int(self.session_id) <= 0:
            _unavailable("observation_session_unavailable")
        if isinstance(self.variant_id, bool) or int(self.variant_id) <= 0:
            _unavailable("observation_variant_unavailable")
        symbol = str(self.symbol or "").strip().upper()
        family = str(self.execution_family or "").strip().lower()
        state = str(self.state or "").strip().lower()
        correlation = str(self.correlation_id or "").strip()
        if (
            not symbol
            or family != "alpaca_spot"
            or state not in _OBSERVATION_STATES
            or not correlation
        ):
            _unavailable("observation_route_or_state_unavailable")
        updated = _utc(self.session_updated_at, "observation_session_updated")
        payloads = {
            name: freeze_canonical_json(dict(getattr(self, name)))
            for name in (
                "risk_snapshot",
                "viability_payload",
                "variant_payload",
                "confirmed_arm_marker",
            )
            if isinstance(getattr(self, name), Mapping)
        }
        if len(payloads) != 4 or not payloads["confirmed_arm_marker"]:
            _unavailable("observation_db_payload_unavailable")
        hashes = {
            "risk_snapshot_sha256": _sha(
                self.risk_snapshot_sha256,
                "observation_risk_snapshot_sha256",
            ),
            "viability_payload_sha256": _sha(
                self.viability_payload_sha256,
                "observation_viability_payload_sha256",
            ),
            "variant_payload_sha256": _sha(
                self.variant_payload_sha256,
                "observation_variant_payload_sha256",
            ),
            "confirmed_arm_marker_sha256": _sha(
                self.confirmed_arm_marker_sha256,
                "observation_confirmed_arm_marker_sha256",
            ),
        }
        expected_payload_hashes = {
            "risk_snapshot_sha256": sha256_json(payloads["risk_snapshot"]),
            "viability_payload_sha256": sha256_json(
                payloads["viability_payload"]
            ),
            "variant_payload_sha256": sha256_json(payloads["variant_payload"]),
            "confirmed_arm_marker_sha256": sha256_json(
                payloads["confirmed_arm_marker"]
            ),
        }
        if hashes != expected_payload_hashes:
            _unavailable("observation_db_payload_hash_mismatch")
        generation = _sha(
            self.observation_generation_sha256,
            "observation_generation",
        )
        expected = captured_paper_observation_generation_sha256(
            session_id=int(self.session_id),
            symbol=symbol,
            execution_family=family,
            state=state,
            correlation_id=correlation,
            variant_id=int(self.variant_id),
            session_updated_at=updated,
            **hashes,
        )
        if generation != expected:
            _unavailable("observation_generation_mismatch")
        object.__setattr__(self, "session_id", int(self.session_id))
        object.__setattr__(self, "variant_id", int(self.variant_id))
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "execution_family", family)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "correlation_id", correlation)
        object.__setattr__(self, "session_updated_at", updated)
        for name, value in payloads.items():
            object.__setattr__(self, name, value)
        for name, value in hashes.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "observation_generation_sha256", generation)
        object.__setattr__(
            self,
            "observation_decision_id",
            f"captured-paper-observe-{self.session_id}-{generation[:24]}",
        )
        object.__setattr__(
            self,
            "observation_snapshot_sha256",
            sha256_json(self.to_payload()),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dispatch_provenance_sha256": self.dispatch_provenance_sha256,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "execution_family": self.execution_family,
            "state": self.state,
            "correlation_id": self.correlation_id,
            "variant_id": self.variant_id,
            "session_updated_at": self.session_updated_at.isoformat(),
            "risk_snapshot": dict(self.risk_snapshot),
            "viability_payload": dict(self.viability_payload),
            "variant_payload": dict(self.variant_payload),
            "confirmed_arm_marker": dict(self.confirmed_arm_marker),
            "risk_snapshot_sha256": self.risk_snapshot_sha256,
            "viability_payload_sha256": self.viability_payload_sha256,
            "variant_payload_sha256": self.variant_payload_sha256,
            "confirmed_arm_marker_sha256": (
                self.confirmed_arm_marker_sha256
            ),
            "observation_generation_sha256": (
                self.observation_generation_sha256
            ),
            "observation_decision_id": self.observation_decision_id,
        }


class CapturedPaperBoundInputScope:
    """Opaque one-shot installer for the exact reads in the input proof."""

    __slots__ = (
        "_installer",
        "_required_read_ids",
        "_scope_sha256",
        "_used",
    )

    def __init__(
        self,
        *,
        installer: Callable[[], AbstractContextManager[Any]],
        required_read_ids: Sequence[str],
        scope_sha256: str,
    ) -> None:
        if not callable(installer):
            _unavailable("captured_input_scope_unavailable")
        ids = tuple(sorted(str(uuid.UUID(str(value))) for value in required_read_ids))
        if not ids or len(ids) != len(set(ids)):
            _unavailable("captured_input_scope_read_inventory_unavailable")
        self._installer = installer
        self._required_read_ids = ids
        self._scope_sha256 = _sha(scope_sha256, "captured_input_scope")
        self._used = False

    @property
    def scope_sha256(self) -> str:
        return self._scope_sha256

    @property
    def required_read_ids(self) -> tuple[str, ...]:
        return self._required_read_ids

    @contextmanager
    def install(self, proof: Any) -> Iterator[None]:
        if self._used:
            _unavailable("captured_input_scope_already_used")
        proof_ids = tuple(sorted(str(value) for value in proof.required_read_ids))
        if proof_ids != self._required_read_ids:
            _unavailable("captured_input_scope_proof_mismatch")
        self._used = True
        with self._installer():
            yield

    def __reduce__(self):
        raise TypeError("captured PAPER input scopes cannot be serialized")


@dataclass(frozen=True, slots=True)
class CapturedPaperProductionCapture:
    """Original captured inputs returned by the external no-DB phase."""

    decision_at: datetime
    adapter: CapturedAlpacaPaperAdapter
    captured_reads: tuple[CapturedReadResult, ...]
    dependency_profile: FSMDependencyProfile
    active_input_attestation: ActiveCaptureInputPrefixAttestation
    economics: CapturedAdaptiveRiskEconomicInputs
    fact_evidence: CapturedAdaptiveRiskEvidenceSet
    correlation_cluster: str
    setup_read_id: str
    bound_input_scope: CapturedPaperBoundInputScope
    buying_power_double_census: Any | None = None
    first_dip_tape_read_id: str | None = None
    first_dip_detector_audit: CapturedFirstDipDetectorAudit | None = None
    first_dip_final_read_provider: FirstDipFinalReadProvider | None = None
    schema_version: str = CAPTURED_PAPER_PRODUCTION_CAPTURE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURED_PAPER_PRODUCTION_CAPTURE_SCHEMA_VERSION:
            _unavailable("production_capture_schema_unavailable")
        object.__setattr__(self, "decision_at", _utc(self.decision_at, "decision"))
        if type(self.adapter) is not CapturedAlpacaPaperAdapter:
            _unavailable("captured_alpaca_paper_adapter_unavailable")
        reads = tuple(self.captured_reads)
        if not reads or any(
            type(row) is not CapturedReadResult or not row.durable for row in reads
        ):
            _unavailable("production_captured_reads_unavailable")
        read_ids = tuple(
            sorted(row.receipt.read_id for row in reads if row.receipt is not None)
        )
        if len(read_ids) != len(reads) or len(read_ids) != len(set(read_ids)):
            _unavailable("production_captured_reads_duplicated")
        object.__setattr__(self, "captured_reads", reads)
        if type(self.dependency_profile) is not FSMDependencyProfile:
            _unavailable("production_dependency_profile_unavailable")
        if read_ids != self.dependency_profile.required_read_ids:
            _unavailable("production_dependency_profile_read_mismatch")
        try:
            proof = verify_active_capture_input_attestation(
                self.active_input_attestation
            )
        except CaptureContractError as exc:
            raise CapturedPaperProductionMaterialUnavailable(
                "production_input_attestation_unavailable"
            ) from exc
        if (
            proof.dependency_profile != self.dependency_profile
            or proof.required_read_ids != read_ids
            or not proof.attested_available_at
            <= self.decision_at
            <= proof.expires_at
        ):
            _unavailable("production_input_attestation_mismatch")
        object.__setattr__(self, "active_input_attestation", proof)
        if type(self.economics) is not CapturedAdaptiveRiskEconomicInputs or type(
            self.fact_evidence
        ) is not CapturedAdaptiveRiskEvidenceSet:
            _unavailable("production_economic_facts_unavailable")
        cluster = str(self.correlation_cluster or "").strip().lower()
        if not cluster:
            _unavailable("production_correlation_cluster_unavailable")
        object.__setattr__(self, "correlation_cluster", cluster)
        setup_id = str(self.setup_read_id or "").strip()
        if setup_id not in set(read_ids):
            _unavailable("production_setup_read_unavailable")
        object.__setattr__(self, "setup_read_id", setup_id)
        if type(self.bound_input_scope) is not CapturedPaperBoundInputScope:
            _unavailable("production_bound_input_scope_unavailable")
        first_dip_id = str(self.first_dip_tape_read_id or "").strip() or None
        if first_dip_id is not None and first_dip_id not in set(read_ids):
            _unavailable("production_first_dip_tape_read_unavailable")
        if proof.first_dip_tape_read_id != first_dip_id:
            _unavailable("production_first_dip_attestation_mismatch")
        object.__setattr__(self, "first_dip_tape_read_id", first_dip_id)


@dataclass(frozen=True, slots=True)
class CapturedPaperObservationCapture:
    """No-DB captured inputs for one read-only watcher invocation."""

    decision_at: datetime
    adapter: CapturedAlpacaPaperAdapter
    captured_reads: tuple[CapturedReadResult, ...]
    dependency_profile: FSMDependencyProfile
    active_input_attestation: ActiveCaptureInputPrefixAttestation
    bound_input_scope: CapturedPaperBoundInputScope
    observation_snapshot_read_id: str
    admission_eligibility_read_id: str
    first_dip_tape_read_id: str | None = None
    first_dip_detector_policy: FirstDipTapePolicy | None = None
    schema_version: str = CAPTURED_PAPER_OBSERVATION_CAPTURE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURED_PAPER_OBSERVATION_CAPTURE_SCHEMA_VERSION:
            _unavailable("observation_capture_schema_unavailable")
        object.__setattr__(self, "decision_at", _utc(self.decision_at, "observation"))
        if type(self.adapter) is not CapturedAlpacaPaperAdapter:
            _unavailable("observation_captured_adapter_unavailable")
        reads = tuple(self.captured_reads)
        if not reads or any(
            type(row) is not CapturedReadResult or not row.durable for row in reads
        ):
            _unavailable("observation_captured_reads_unavailable")
        read_ids = tuple(
            sorted(row.receipt.read_id for row in reads if row.receipt is not None)
        )
        if len(read_ids) != len(reads) or len(read_ids) != len(set(read_ids)):
            _unavailable("observation_captured_reads_duplicated")
        if type(self.dependency_profile) is not FSMDependencyProfile:
            _unavailable("observation_dependency_profile_unavailable")
        if read_ids != self.dependency_profile.required_read_ids:
            _unavailable("observation_dependency_profile_read_mismatch")
        try:
            proof = verify_active_capture_input_attestation(
                self.active_input_attestation
            )
        except CaptureContractError as exc:
            raise CapturedPaperProductionMaterialUnavailable(
                "observation_input_attestation_unavailable"
            ) from exc
        if (
            proof.dependency_profile != self.dependency_profile
            or proof.required_read_ids != read_ids
            or not proof.attested_available_at
            <= self.decision_at
            <= proof.expires_at
        ):
            _unavailable("observation_input_attestation_mismatch")
        object.__setattr__(self, "active_input_attestation", proof)
        streams = {
            row.receipt.stream for row in reads if row.receipt is not None
        }
        if not _OBSERVATION_REQUIRED_STREAMS.issubset(streams):
            _unavailable("observation_required_stream_coverage_unavailable")
        if type(self.bound_input_scope) is not CapturedPaperBoundInputScope:
            _unavailable("observation_bound_input_scope_unavailable")
        read_id_set = set(read_ids)
        snapshot_id = str(self.observation_snapshot_read_id or "").strip()
        eligibility_id = str(self.admission_eligibility_read_id or "").strip()
        if snapshot_id not in read_id_set or eligibility_id not in read_id_set:
            _unavailable("observation_identity_read_unavailable")
        object.__setattr__(self, "captured_reads", reads)
        object.__setattr__(self, "observation_snapshot_read_id", snapshot_id)
        object.__setattr__(self, "admission_eligibility_read_id", eligibility_id)
        first_dip_id = str(self.first_dip_tape_read_id or "").strip() or None
        if (first_dip_id is None) != (self.first_dip_detector_policy is None):
            _unavailable("observation_first_dip_exact_print_pair_unavailable")
        if proof.first_dip_tape_read_id != first_dip_id:
            _unavailable("observation_first_dip_attestation_mismatch")
        if first_dip_id is not None:
            rows = tuple(
                row
                for row in reads
                if row.receipt is not None
                and row.receipt.read_id == first_dip_id
                and row.receipt.stream is CaptureStream.IQFEED_PRINT
            )
            if len(rows) != 1 or type(self.first_dip_detector_policy) is not FirstDipTapePolicy:
                _unavailable("observation_first_dip_exact_print_unavailable")
        object.__setattr__(self, "first_dip_tape_read_id", first_dip_id)


class CapturedPaperCandidateReader(Protocol):
    def __call__(
        self,
        db: Any,
        request: CapturedPaperDispatchRequest,
    ) -> CapturedPaperDurableCandidateSnapshot | CapturedPaperDurableObservationSnapshot: ...


class CapturedPaperProductionCaptureProvider(Protocol):
    def __call__(
        self,
        *,
        request: CapturedPaperDispatchRequest,
        candidate: CapturedPaperDurableCandidateSnapshot,
        coordinator: Any,
    ) -> AbstractContextManager[CapturedPaperProductionCapture]: ...


class CapturedPaperObservationCaptureProvider(Protocol):
    def __call__(
        self,
        *,
        request: CapturedPaperDispatchRequest,
        observation: CapturedPaperDurableObservationSnapshot,
        coordinator: Any,
    ) -> AbstractContextManager[CapturedPaperObservationCapture]: ...


@dataclass(frozen=True, slots=True)
class PreparedCapturedPaperProductionDecision:
    """Process-private factory output consumed immediately by the host owner."""

    selection_context: CapturedPaperSelectionContext
    admission_inputs: CapturedPaperAdmissionInputs
    predecision_captured_reads: tuple[CapturedReadResult, ...] = field(
        repr=False,
        compare=False,
    )
    predecision_executed_read_inventory: ExecutedCaptureReadInventory = field(
        repr=False,
        compare=False,
    )
    adapter_factory: Callable[[], CapturedAlpacaPaperAdapter] = field(
        repr=False,
        compare=False,
    )
    bound_input_scope: CapturedPaperBoundInputScope = field(
        repr=False,
        compare=False,
    )
    final_read_provider: FirstDipFinalReadProvider | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    candidate_sha256: str = ""

    def __post_init__(self) -> None:
        self.selection_context.verify()
        if type(self.admission_inputs) is not CapturedPaperAdmissionInputs:
            _unavailable("prepared_admission_inputs_unavailable")
        reads = tuple(self.predecision_captured_reads)
        inventory = self.predecision_executed_read_inventory
        if (
            not reads
            or any(type(row) is not CapturedReadResult or not row.durable for row in reads)
            or type(inventory) is not ExecutedCaptureReadInventory
            or self.admission_inputs.executed_read_inventory is not inventory
        ):
            _unavailable("prepared_executed_read_inventory_unavailable")
        object.__setattr__(self, "predecision_captured_reads", reads)
        if not callable(self.adapter_factory):
            _unavailable("prepared_adapter_factory_unavailable")
        if type(self.bound_input_scope) is not CapturedPaperBoundInputScope:
            _unavailable("prepared_input_scope_unavailable")
        object.__setattr__(
            self,
            "candidate_sha256",
            _sha(self.candidate_sha256, "prepared_candidate"),
        )

    def __reduce__(self):
        raise TypeError("captured PAPER production decisions cannot be serialized")


@dataclass(frozen=True, slots=True)
class PreparedCapturedPaperObservation:
    """Read-only watcher capability; structurally carries no admission packet."""

    observation_context: CapturedPaperObservationContext
    active_input_attestation: Any
    adapter_factory: Callable[[], CapturedAlpacaPaperObservationAdapter] = field(
        repr=False,
        compare=False,
    )
    bound_input_scope: CapturedPaperBoundInputScope = field(
        repr=False,
        compare=False,
    )
    first_dip_detector_policy: FirstDipTapePolicy | None = None
    first_dip_tape_read_id: str | None = None
    observation_snapshot_sha256: str = ""

    def __post_init__(self) -> None:
        if type(self.observation_context) is not CapturedPaperObservationContext:
            _unavailable("prepared_observation_context_unavailable")
        self.observation_context.verify()
        if not callable(self.adapter_factory):
            _unavailable("prepared_observation_adapter_unavailable")
        if type(self.bound_input_scope) is not CapturedPaperBoundInputScope:
            _unavailable("prepared_observation_input_scope_unavailable")
        if (self.first_dip_tape_read_id is None) != (
            self.first_dip_detector_policy is None
        ):
            _unavailable("prepared_observation_first_dip_pair_unavailable")
        object.__setattr__(
            self,
            "observation_snapshot_sha256",
            _sha(
                self.observation_snapshot_sha256,
                "prepared_observation_snapshot",
            ),
        )

    def __reduce__(self):
        raise TypeError("captured PAPER observations cannot be serialized")


class CapturedPaperProductionMaterialFactory:
    """Concrete phase orchestrator; injected providers remain no-fallback seams."""

    def __init__(
        self,
        *,
        candidate_reader: CapturedPaperCandidateReader,
        capture_provider: CapturedPaperProductionCaptureProvider,
        observation_capture_provider: CapturedPaperObservationCaptureProvider,
        coordinator_for: Callable[[str], Any],
        capture_config_for: Callable[[str], Mapping[str, Any]],
        settings_projection_sha256: str,
        policy_spec: CapturedAdaptiveRiskPolicySpec,
        operational_policy: CapturedPaperOperationalPolicy,
    ) -> None:
        if (
            not callable(candidate_reader)
            or not callable(capture_provider)
            or not callable(observation_capture_provider)
            or not callable(capture_config_for)
        ):
            raise TypeError("captured PAPER production readers must be callable")
        if not callable(coordinator_for):
            raise TypeError("captured PAPER coordinator resolver must be callable")
        if type(policy_spec) is not CapturedAdaptiveRiskPolicySpec:
            raise TypeError("captured PAPER policy spec must be typed")
        if type(operational_policy) is not CapturedPaperOperationalPolicy:
            raise TypeError("captured PAPER operational policy must be typed")
        settings_sha = _sha(
            settings_projection_sha256,
            "production_settings_projection",
        )
        if (
            policy_spec.effective_config_sha256 != settings_sha
            or operational_policy.config_provenance_sha256 != settings_sha
        ):
            raise TypeError(
                "captured PAPER policy templates must bind the settings projection"
            )
        self._candidate_reader = candidate_reader
        self._capture_provider = capture_provider
        self._observation_capture_provider = observation_capture_provider
        self._coordinator_for = coordinator_for
        self._capture_config_for = capture_config_for
        self._settings_projection_sha256 = settings_sha
        self._policy_spec = policy_spec
        self._operational_policy = operational_policy

    def _capture_bound_policies(
        self,
        request: CapturedPaperDispatchRequest,
    ) -> tuple[CapturedAdaptiveRiskPolicySpec, CapturedPaperOperationalPolicy]:
        """Bind policy templates to the actual final per-symbol run config."""

        try:
            raw = self._capture_config_for(request.symbol)
            config = dict(raw)
        except Exception as exc:
            raise CapturedPaperProductionMaterialUnavailable(
                "production_capture_config_unavailable"
            ) from exc
        resource = config.get("capture_resource_binding")
        run_config = config.get("live_capture_run_configuration")
        roster = config.get("iqfeed_external_producer_generation_roster")
        store_root = str(config.get("capture_store_root") or "").strip()
        if (
            config.get("schema_version")
            != "chili.captured-paper-capture-runtime-config.v1"
            or config.get("captured_paper_settings_projection_sha256")
            != self._settings_projection_sha256
            or str(config.get("capture_certification_symbol") or "")
            .strip()
            .upper()
            != request.symbol
            or not isinstance(resource, Mapping)
            or config.get("capture_resource_binding_sha256")
            != sha256_json(dict(resource))
            or not isinstance(run_config, Mapping)
            or config.get("live_capture_run_configuration_sha256")
            != sha256_json(dict(run_config))
            or not isinstance(roster, Mapping)
            or config.get("iqfeed_external_producer_generation_roster_sha256")
            != sha256_json(dict(roster))
            or not store_root
            or not Path(store_root).is_absolute()
            or sha256_json(config) != request.config_sha256
        ):
            _unavailable("production_capture_config_provenance_mismatch")
        return (
            replace(
                self._policy_spec,
                effective_config_sha256=request.config_sha256,
            ),
            replace(
                self._operational_policy,
                config_provenance_sha256=request.config_sha256,
            ),
        )

    @staticmethod
    def _rollback_read_transaction(db: Any) -> None:
        rollback = getattr(db, "rollback", None)
        if not callable(rollback):
            _unavailable("candidate_read_transaction_boundary_unavailable")
        rollback()

    @staticmethod
    def _read_by_stream(
        captured: CapturedPaperProductionCapture,
        stream: CaptureStream,
    ) -> CapturedReadResult:
        rows = tuple(
            row
            for row in captured.captured_reads
            if row.receipt is not None and row.receipt.stream is stream
        )
        if len(rows) != 1:
            _unavailable(f"production_{stream.value}_read_unavailable")
        return rows[0]

    @staticmethod
    def _read_by_id(
        captured_reads: Sequence[CapturedReadResult],
        *,
        read_id: str,
        stream: CaptureStream,
        reason: str,
    ) -> CapturedReadResult:
        rows = tuple(
            row
            for row in captured_reads
            if row.receipt is not None
            and row.receipt.read_id == read_id
            and row.receipt.stream is stream
        )
        if len(rows) != 1 or rows[0].receipt is None:
            _unavailable(reason)
        return rows[0]

    @staticmethod
    def _observation_product(
        *,
        request: CapturedPaperDispatchRequest,
        observation: CapturedPaperDurableObservationSnapshot,
        captured: CapturedPaperObservationCapture,
    ) -> tuple[NormalizedProduct, FreshnessMeta, CapturedReadResult]:
        row = CapturedPaperProductionMaterialFactory._read_by_id(
            captured.captured_reads,
            read_id=captured.admission_eligibility_read_id,
            stream=CaptureStream.ADMISSION_ELIGIBILITY,
            reason="observation_admission_eligibility_unavailable",
        )
        if len(row.source_events) != 1 or row.receipt is None:
            _unavailable("observation_admission_eligibility_unavailable")
        event = row.source_events[0]
        payload = dict(event.payload)
        product_payload = payload.get("product")
        if (
            event.symbol != request.symbol
            or row.receipt.decision_id != observation.observation_decision_id
            or payload.get("schema_version")
            != CAPTURED_PAPER_ADMISSION_ELIGIBILITY_SCHEMA_VERSION
            or str(payload.get("symbol") or "").strip().upper() != request.symbol
            or str(payload.get("execution_family") or "").strip().lower()
            != request.execution_family
            or str(payload.get("account_scope") or "").strip()
            != request.account_scope
            or str(payload.get("expected_account_id") or "").strip()
            != request.expected_account_id
            or type(product_payload) is not dict
        ):
            _unavailable("observation_admission_eligibility_identity_mismatch")
        try:
            product = NormalizedProduct(**deepcopy(product_payload))
            max_age = _finite(
                payload.get("max_age_seconds"),
                "observation_admission_eligibility_max_age",
                positive=True,
            )
            freshness = FreshnessMeta(
                retrieved_at_utc=event.clocks.available_at,
                provider_time_utc=event.clocks.provider_event_at,
                max_age_seconds=max_age,
            )
        except (TypeError, ValueError) as exc:
            raise CapturedPaperProductionMaterialUnavailable(
                "observation_admission_eligibility_payload_unavailable"
            ) from exc
        if product.product_id.strip().upper() != request.symbol:
            _unavailable("observation_admission_eligibility_product_mismatch")
        return product, freshness, row

    @staticmethod
    def _receipt_sha(captured: CapturedPaperProductionCapture, read_id: str) -> str:
        rows = tuple(
            row
            for row in captured.captured_reads
            if row.receipt is not None and row.receipt.read_id == read_id
        )
        if len(rows) != 1 or rows[0].receipt is None:
            _unavailable("production_receipt_unavailable")
        return sha256_json(rows[0].receipt.to_dict())

    @staticmethod
    def _verify_economic_provenance(
        *,
        request: CapturedPaperDispatchRequest,
        candidate: CapturedPaperDurableCandidateSnapshot,
        captured: CapturedPaperProductionCapture,
        proof: Any,
    ) -> None:
        identity = CapturedAdaptiveRiskDecisionIdentity(
            execution_surface="alpaca_paper",
            run_id=proof.run_id,
            generation=proof.generation,
            decision_id=candidate.client_order_id,
            symbol=request.symbol,
            setup_family=candidate.setup_family,
            correlation_cluster=captured.correlation_cluster,
            account_scope=request.account_scope,
            decision_at=captured.decision_at,
        )
        payloads = captured_adaptive_risk_fact_payloads(
            identity,
            captured.economics,
        )
        read_ids = set(proof.required_read_ids)
        for name, fact in captured.fact_evidence.as_mapping().items():
            if (
                fact.content_sha256 != sha256_json(payloads[name])
                or not set(fact.source_read_ids).issubset(read_ids)
                or fact.available_at > captured.decision_at
                or fact.observed_at > captured.decision_at
            ):
                _unavailable(f"production_{name}_provenance_unavailable")

    @staticmethod
    def _opportunity(
        request: CapturedPaperDispatchRequest,
        candidate: CapturedPaperDurableCandidateSnapshot,
    ) -> CapturedPaperOpportunityKey | None:
        if candidate.setup_family != _FIRST_DIP_SETUP:
            return None
        raw = candidate.trigger_debug.get("opportunity_key")
        if type(raw) is not dict:
            _unavailable("production_first_dip_opportunity_unavailable")
        try:
            raw_date = str(raw.get("trading_date") or "")
            trading_date = date.fromisoformat(raw_date)
            if raw_date != trading_date.isoformat():
                raise ValueError("noncanonical trading date")
            return CapturedPaperOpportunityKey(
                account_scope=request.account_scope,
                symbol=raw.get("symbol"),
                trading_date=trading_date,
                setup_family=raw.get("setup_family"),
            )
        except (TypeError, ValueError) as exc:
            raise CapturedPaperProductionMaterialUnavailable(
                "production_first_dip_opportunity_unavailable"
            ) from exc

    def material_kind(
        self,
        db: Any,
        request: CapturedPaperDispatchRequest,
    ) -> str:
        """Classify one durable route, closing its DB transaction first."""

        try:
            snapshot = self._candidate_reader(db, request)
        finally:
            self._rollback_read_transaction(db)
        if type(snapshot) is CapturedPaperDurableObservationSnapshot:
            return "observation"
        if type(snapshot) is CapturedPaperDurableCandidateSnapshot:
            return "decision"
        _unavailable("production_durable_snapshot_unavailable")

    @contextmanager
    def observation_scope(
        self,
        db: Any,
        request: CapturedPaperDispatchRequest,
    ) -> Iterator[PreparedCapturedPaperObservation]:
        """Capture one watcher tick with no admission/risk/order capability."""

        if type(request) is not CapturedPaperDispatchRequest:
            _unavailable("observation_dispatch_request_unavailable")
        request.verify()
        try:
            observation = self._candidate_reader(db, request)
        finally:
            self._rollback_read_transaction(db)
        policy_spec, operational_policy = self._capture_bound_policies(request)
        if type(observation) is not CapturedPaperDurableObservationSnapshot:
            _unavailable("observation_durable_snapshot_unavailable")
        if (
            observation.dispatch_provenance_sha256 != request.provenance_sha256
            or observation.session_id != request.session_id
            or observation.symbol != request.symbol
            or observation.execution_family != request.execution_family
        ):
            _unavailable("observation_route_mismatch")
        coordinator = self._coordinator_for(request.symbol)
        if (
            str(getattr(coordinator, "certification_symbol", "") or "")
            .strip()
            .upper()
            != request.symbol
        ):
            _unavailable("observation_capture_coordinator_mismatch")
        with self._observation_capture_provider(
            request=request,
            observation=observation,
            coordinator=coordinator,
        ) as captured:
            if type(captured) is not CapturedPaperObservationCapture:
                _unavailable("observation_capture_bundle_unavailable")
            snapshot_row = self._read_by_id(
                captured.captured_reads,
                read_id=captured.observation_snapshot_read_id,
                stream=CaptureStream.FSM_DECISION,
                reason="observation_snapshot_receipt_unavailable",
            )
            if (
                snapshot_row.receipt is None
                or snapshot_row.receipt.decision_id
                != observation.observation_decision_id
                or len(snapshot_row.source_events) != 1
                or dict(snapshot_row.source_events[0].payload)
                != observation.to_payload()
                or sha256_json(dict(snapshot_row.source_events[0].payload))
                != observation.observation_snapshot_sha256
            ):
                _unavailable("observation_snapshot_receipt_mismatch")
            account_result = self._read_by_stream(
                captured, CaptureStream.ACCOUNT_RISK_SNAPSHOT
            )
            bbo_result = self._read_by_stream(
                captured, CaptureStream.ALPACA_NBBO_QUOTE
            )
            if account_result.receipt is None or bbo_result.receipt is None:
                _unavailable("observation_account_or_bbo_receipt_unavailable")
            if (
                account_result.receipt.decision_id
                != observation.observation_decision_id
                or bbo_result.receipt.decision_id
                != observation.observation_decision_id
                or len(account_result.source_events) != 1
                or len(bbo_result.source_events) != 1
                or account_result.source_events[0].symbol is not None
                or bbo_result.source_events[0].symbol != request.symbol
            ):
                _unavailable("observation_account_or_bbo_identity_mismatch")
            proof = verify_active_capture_input_attestation(
                captured.active_input_attestation
            )
            if proof.decision_id != observation.observation_decision_id:
                _unavailable("observation_input_attestation_identity_mismatch")
            try:
                authority = captured.adapter.issue_account_authority(proof)
            except CapturedAlpacaPaperReadError as exc:
                raise CapturedPaperProductionMaterialUnavailable(
                    "observation_account_authority_unavailable"
                ) from exc
            if (
                proof.decision_id != observation.observation_decision_id
                or proof.code_build_sha256 != request.code_build_sha256
                or proof.config_sha256 != request.config_sha256
                or proof.feature_flags_sha256
                != policy_spec.feature_flags_sha256
                or policy_spec.code_build_sha256
                != request.code_build_sha256
                or policy_spec.effective_config_sha256
                != request.config_sha256
                or operational_policy.config_provenance_sha256
                != request.config_sha256
                or proof.attested_available_at > captured.decision_at
                or proof.expires_at < captured.decision_at
                or authority.account_id != request.expected_account_id
                or request.account_scope != "alpaca:paper"
            ):
                _unavailable("observation_policy_or_clock_provenance_mismatch")
            product, freshness, eligibility_row = self._observation_product(
                request=request,
                observation=observation,
                captured=captured,
            )
            assert eligibility_row.receipt is not None
            eligibility_event = eligibility_row.source_events[0]
            context = CapturedPaperObservationContext(
                dispatch_request=request,
                initial_state=observation.state,
                correlation_id=observation.correlation_id,
                variant_id=observation.variant_id,
                session_updated_at=observation.session_updated_at,
                decision_at=captured.decision_at,
                evidence_available_at=proof.attested_available_at,
                evidence_expires_at=proof.expires_at,
                risk_snapshot_sha256=observation.risk_snapshot_sha256,
                viability_payload_sha256=observation.viability_payload_sha256,
                variant_payload_sha256=observation.variant_payload_sha256,
                confirmed_arm_marker_sha256=(
                    observation.confirmed_arm_marker_sha256
                ),
                observation_decision_id=observation.observation_decision_id,
                observation_generation_sha256=(
                    observation.observation_generation_sha256
                ),
            )
            read_only_adapter = CapturedAlpacaPaperObservationAdapter(
                captured_adapter=captured.adapter,
                product=product,
                freshness=freshness,
                eligibility_read_id=eligibility_row.receipt.read_id,
                eligibility_event_sha256=eligibility_event.event_sha256,
                observation_decision_id=observation.observation_decision_id,
            )
            yielded_adapter = False

            def same_adapter() -> CapturedAlpacaPaperObservationAdapter:
                nonlocal yielded_adapter
                if yielded_adapter:
                    _unavailable("observation_adapter_factory_reused")
                yielded_adapter = True
                return read_only_adapter

            yield PreparedCapturedPaperObservation(
                observation_context=context,
                active_input_attestation=proof,
                adapter_factory=same_adapter,
                bound_input_scope=captured.bound_input_scope,
                first_dip_detector_policy=(
                    captured.first_dip_detector_policy
                ),
                first_dip_tape_read_id=captured.first_dip_tape_read_id,
                observation_snapshot_sha256=(
                    observation.observation_snapshot_sha256
                ),
            )

    @contextmanager
    def decision_scope(
        self,
        db: Any,
        request: CapturedPaperDispatchRequest,
    ) -> Iterator[PreparedCapturedPaperProductionDecision]:
        """Build outside DB I/O, then keep exact provider/adapter scopes alive."""

        if type(request) is not CapturedPaperDispatchRequest:
            _unavailable("production_dispatch_request_unavailable")
        request.verify()
        try:
            candidate = self._candidate_reader(db, request)
        finally:
            # This rollback is deliberately before coordinator/adapter/provider use,
            # including every unavailable/exceptional reader result.
            self._rollback_read_transaction(db)
        policy_spec, operational_policy = self._capture_bound_policies(request)
        if type(candidate) is not CapturedPaperDurableCandidateSnapshot:
            _unavailable("production_candidate_snapshot_unavailable")
        if (
            candidate.dispatch_provenance_sha256 != request.provenance_sha256
            or candidate.session_id != request.session_id
            or candidate.symbol != request.symbol
            or candidate.execution_family != request.execution_family
        ):
            _unavailable("production_candidate_route_mismatch")
        coordinator = self._coordinator_for(request.symbol)
        if (
            str(getattr(coordinator, "certification_symbol", "") or "").strip().upper()
            != request.symbol
        ):
            _unavailable("production_capture_coordinator_mismatch")
        with self._capture_provider(
            request=request,
            candidate=candidate,
            coordinator=coordinator,
        ) as captured:
            if type(captured) is not CapturedPaperProductionCapture:
                _unavailable("production_capture_bundle_unavailable")
            account_result = self._read_by_stream(
                captured, CaptureStream.ACCOUNT_RISK_SNAPSHOT
            )
            bbo_result = self._read_by_stream(
                captured, CaptureStream.ALPACA_NBBO_QUOTE
            )
            if account_result.receipt is None or bbo_result.receipt is None:
                _unavailable("production_account_or_bbo_receipt_unavailable")
            account_event = account_result.source_events[0]
            bbo_event = bbo_result.source_events[0]
            if (
                account_event.symbol is not None
                or bbo_event.symbol != request.symbol
                or bbo_result.receipt.decision_id != candidate.client_order_id
                or account_result.receipt.decision_id != candidate.client_order_id
            ):
                _unavailable("production_account_or_bbo_identity_mismatch")
            first_dip_id = captured.first_dip_tape_read_id
            proof = verify_active_capture_input_attestation(
                captured.active_input_attestation
            )
            if proof.decision_id != candidate.client_order_id:
                _unavailable("production_input_attestation_identity_mismatch")
            try:
                authority = captured.adapter.issue_account_authority(proof)
            except CapturedAlpacaPaperReadError as exc:
                raise CapturedPaperProductionMaterialUnavailable(
                    "production_account_authority_unavailable"
                ) from exc
            self._verify_economic_provenance(
                request=request,
                candidate=candidate,
                captured=captured,
                proof=proof,
            )
            if (
                policy_spec.code_build_sha256 != request.code_build_sha256
                or policy_spec.effective_config_sha256
                != request.config_sha256
                or policy_spec.feature_flags_sha256
                != proof.feature_flags_sha256
                or operational_policy.config_provenance_sha256
                != request.config_sha256
            ):
                _unavailable("production_policy_provenance_mismatch")
            exact_bbo = CapturedExactBbo(
                read_id=bbo_result.receipt.read_id,
                source_event_sha256=bbo_event.event_sha256,
                payload_json=_canonical_payload(bbo_event),
            )
            account_receipt = CapturedAccountRiskReceipt(
                read_id=account_result.receipt.read_id,
                source_event_sha256=account_event.event_sha256,
                payload_json=_canonical_payload(account_event),
            )
            bbo_payload = dict(bbo_event.payload)
            bid = _finite(bbo_payload.get("bid"), "production_bid", positive=True)
            ask = _finite(bbo_payload.get("ask"), "production_ask", positive=True)
            if bid > ask or candidate.structural_stop_price >= ask:
                _unavailable("production_candidate_price_order_unavailable")
            if captured.economics.structural_stop != candidate.structural_stop_price:
                _unavailable("production_structural_stop_mismatch")
            if captured.economics.setup_quality != candidate.viability_score:
                _unavailable("production_setup_quality_mismatch")
            setup_receipt_sha = self._receipt_sha(
                captured, captured.setup_read_id
            )
            bbo_receipt_sha = self._receipt_sha(
                captured, bbo_result.receipt.read_id
            )
            opportunity = self._opportunity(request, candidate)
            setup_evidence_sha256 = setup_receipt_sha
            if candidate.setup_family == _FIRST_DIP_SETUP:
                audit = captured.first_dip_detector_audit
                if (
                    type(audit) is not CapturedFirstDipDetectorAudit
                    or not isinstance(
                        captured.first_dip_final_read_provider,
                        FirstDipFinalReadProvider,
                    )
                    or first_dip_id is None
                    or candidate.trigger_debug.get(
                        "first_dip_tape_decision_receipt_binding_sha256"
                    )
                    != audit.detector_receipt_binding_sha256
                ):
                    _unavailable("production_first_dip_capture_unavailable")
                setup_evidence_sha256 = (
                    audit.detector_receipt_binding_sha256
                )
            elif (
                captured.first_dip_detector_audit is not None
                or captured.first_dip_final_read_provider is not None
                or first_dip_id is not None
            ):
                _unavailable("production_non_first_dip_capture_contaminated")
            arm = candidate.confirmed_arm()
            context = CapturedPaperSelectionContext.create(
                dispatch_request=request,
                confirmed_arm_generation=arm,
                confirmed_arm_marker=dict(candidate.confirmed_arm_marker),
                entry_place_count=candidate.entry_place_count,
                client_order_id=candidate.client_order_id,
                setup_family=candidate.setup_family,
                decision_at=captured.decision_at,
                evidence_available_at=proof.attested_available_at,
                evidence_expires_at=proof.expires_at,
                bid=bid,
                ask=ask,
                structural_stop_price=candidate.structural_stop_price,
                entry_limit_ceiling_price=quantize_alpaca_equity_limit_price(
                    ask, "buy"
                ),
                trigger_reason=candidate.trigger_reason,
                trigger_debug=dict(candidate.trigger_debug),
                candidate_generation_sha256=(
                    candidate.session_snapshot_sha256
                ),
                viability_updated_at=candidate.viability_updated_at,
                viability_score=candidate.viability_score,
                viability_payload_sha256=(
                    candidate.viability_payload_sha256
                ),
                execution_readiness_sha256=(
                    candidate.execution_readiness_sha256
                ),
                account_receipt_sha256=(
                    authority.account_read_receipt_sha256
                ),
                bbo_receipt_sha256=bbo_receipt_sha,
                setup_evidence_sha256=setup_evidence_sha256,
                policy_sha256=policy_spec.policy.policy_sha256,
                feature_flags_sha256=proof.feature_flags_sha256,
                opportunity_key=opportunity,
            )
            admission = CapturedPaperAdmissionInputs(
                dispatch_request=request,
                post_commit_request=context.draft,
                broker_account_facts=(
                    AlpacaPaperBrokerAccountFacts.from_capture_authority(
                        authority
                    )
                ),
                policy_spec=policy_spec,
                active_input_attestation=proof,
                predecision_captured_reads=captured.captured_reads,
                executed_read_inventory=build_executed_capture_read_inventory(
                    identity=coordinator.identity,
                    decision_id=candidate.client_order_id,
                    captured_reads=captured.captured_reads,
                ),
                exact_bbo=exact_bbo,
                account_receipt=account_receipt,
                economics=captured.economics,
                fact_evidence=captured.fact_evidence,
                correlation_cluster=captured.correlation_cluster,
                operational_policy=operational_policy,
                buying_power_double_census=(
                    captured.buying_power_double_census
                ),
                first_dip_detector_audit=(
                    captured.first_dip_detector_audit
                ),
            )
            yielded_adapter = False

            def same_adapter() -> CapturedAlpacaPaperAdapter:
                nonlocal yielded_adapter
                if yielded_adapter:
                    _unavailable("production_adapter_factory_reused")
                yielded_adapter = True
                return captured.adapter

            yield PreparedCapturedPaperProductionDecision(
                selection_context=context,
                admission_inputs=admission,
                predecision_captured_reads=captured.captured_reads,
                predecision_executed_read_inventory=(
                    admission.executed_read_inventory
                ),
                adapter_factory=same_adapter,
                bound_input_scope=captured.bound_input_scope,
                final_read_provider=captured.first_dip_final_read_provider,
                candidate_sha256=candidate.candidate_sha256,
            )


__all__ = [
    "CAPTURED_PAPER_ADMISSION_ELIGIBILITY_SCHEMA_VERSION",
    "CapturedPaperBoundInputScope",
    "CapturedPaperDurableCandidateSnapshot",
    "CapturedPaperDurableObservationSnapshot",
    "CapturedPaperObservationCapture",
    "CapturedPaperProductionCapture",
    "CapturedPaperProductionMaterialFactory",
    "CapturedPaperProductionMaterialUnavailable",
    "PreparedCapturedPaperObservation",
    "PreparedCapturedPaperProductionDecision",
]
