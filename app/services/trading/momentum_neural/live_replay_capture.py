"""Inert production seam for a resource-bounded live ReplayV3 capture.

This module deliberately does not install itself in a runner and never calls a
provider.  Live producers hand already-observed values to the coordinator.  It
then assigns one contiguous durable sequence, routes cold-symbol prehistory
through the bounded ring, and submits without waiting for disk I/O.  Any
admission failure becomes coverage-gap evidence through the runtime ingress.

The values returned here are *capture receipts*, not replay certification.
Certification still requires a clean final seal, an independently supplied
expected seal SHA, the official verified loader, complete coverage grading, and
an OS-enforced no-egress replay process.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import ExitStack, contextmanager
import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import queue
import threading
from typing import (
    Any,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)
import uuid
from zoneinfo import ZoneInfo

import pandas as pd

from .first_dip_tape_policy import FirstDipTapeEvaluation, FirstDipTapePolicy
from .replay_capture_contract import (
    ActiveCaptureContinuityEvidence,
    ActiveCaptureInputPrefixAttestation,
    ActiveCapturePrefixAttestation,
    CaptureClocks,
    CaptureBrokerOrderLifecycle,
    CaptureContractError,
    CaptureDecisionCheckpoint,
    CaptureDecisionOutput,
    CaptureEvent,
    CaptureEventRef,
    CaptureIqfeedPrint,
    CaptureMicrostructureOperation,
    CaptureMicrostructureReadQuery,
    CaptureProviderRegistrationEvidence,
    FirstDipTapeReceiptEvidence,
    FIRST_DIP_FINAL_CAPTURE_FRONTIER_SCHEMA_VERSION,
    FSMDependencyProfile,
    CaptureProducerSpec,
    CaptureReadReceipt,
    CaptureRunIdentity,
    CaptureScannerProfile,
    CaptureScannerSnapshotQuery,
    CaptureStream,
    CoverageGap,
    CoverageMode,
    ProviderWatermark,
    StreamCoverage,
    STREAM_POLICIES,
    PROVIDER_OHLCV_PAYLOAD_SCHEMA_VERSION,
    PROVIDER_OHLCV_QUERY_SCHEMA_VERSION,
    SCANNER_SNAPSHOT_PROVIDER,
    build_scanner_snapshot_payload,
    build_provider_registration_evidence_from_source_event,
    canonical_json_bytes,
    captured_read_result_sha256,
    sha256_json,
    scanner_snapshot_market_reference_at,
    verify_active_capture_input_attestation,
    verify_active_capture_prefix_attestation,
)
from .replay_capture_runtime import (
    BoundedCaptureIngress,
    BoundedHotSymbolLeases,
    BoundedPreTriggerRing,
    CaptureAdaptivePressureController,
    FirstDipTapeCoverageUnavailable,
    MicrostructureCoverageUnavailable,
    CapturePressureSample,
    CaptureProducerLifecycleRuntime,
    CaptureResourceBinding,
    CaptureRunSeal,
    CaptureWriterPool,
    CaptureWriterWorker,
    ContentAddressedCaptureStore,
    HotSymbolLease,
    PreTriggerRetainResult,
    PromotionBatch,
    PromotionTransfer,
    SharedCaptureAdmissionBudget,
    SharedCaptureStoreRuntime,
    SharedCaptureWriterLease,
)
from .replay_errors import (
    ReplayDecisionLocalMicrostructureCoverageUnavailableError,
    ReplayMicrostructureInputUnavailableError,
)


UTC = timezone.utc
_MASSIVE_MARKET_TZ = ZoneInfo("America/New_York")
_CONTROL_PROVIDER = "chili_live_capture"
_COORDINATOR_STARTUP_STREAMS = frozenset(
    {
        CaptureStream.CODE_BUILD,
        CaptureStream.CONFIG_SNAPSHOT,
        CaptureStream.FEATURE_FLAG_SNAPSHOT,
        CaptureStream.ACCOUNT_RISK_SNAPSHOT,
    }
)
_BROAD_STREAMS = frozenset(
    {
        CaptureStream.IQFEED_PRINT,
        CaptureStream.NBBO_QUOTE,
        CaptureStream.SCANNER_SNAPSHOT,
    }
)
_CERTIFICATION_SYMBOL_STREAMS = frozenset(
    {
        CaptureStream.IQFEED_PRINT,
        CaptureStream.PROVIDER_TRADE_PRINT,
        CaptureStream.NBBO_QUOTE,
        CaptureStream.ALPACA_NBBO_QUOTE,
        CaptureStream.L2_DEPTH_DELTA,
        CaptureStream.L2_DEPTH_CHECKPOINT,
        CaptureStream.PROVIDER_OHLCV,
        CaptureStream.ORTEX_SNAPSHOT,
        CaptureStream.SCANNER_SNAPSHOT,
        CaptureStream.CATALYST_NEWS,
        CaptureStream.ADMISSION_ELIGIBILITY,
        CaptureStream.HALT_LULD_STATE,
        CaptureStream.SSR_STATE,
        CaptureStream.BROKER_ORDER_LIFECYCLE,
        CaptureStream.FSM_DECISION,
    }
)
_CURRENT_STATE_INVENTORY_MODES = frozenset(
    {
        CoverageMode.QUERY_RECEIPT,
        CoverageMode.CHANGE_LOG,
        CoverageMode.IDENTITY,
        CoverageMode.DERIVED,
    }
)


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CaptureContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value, "capture timestamp").isoformat().replace("+00:00", "Z")


def _normalized_symbol(value: str | None, *, required: bool = False) -> str | None:
    symbol = str(value or "").strip().upper() or None
    if required and symbol is None:
        raise CaptureContractError("capture symbol is required")
    return symbol


class CaptureSessionState(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    STOPPING = "stopping"
    SEALED = "sealed"
    ABORTED = "aborted"


@dataclass(frozen=True)
class CaptureIdentityEvidence:
    """Exact immutable startup evidence bound by ``CaptureRunIdentity``."""

    code_build: Mapping[str, Any]
    config: Mapping[str, Any]
    feature_flags: Mapping[str, Any]
    account_identity: Mapping[str, Any]
    account_risk_snapshot: Mapping[str, Any]
    account_query: Mapping[str, Any]
    account_provider: str

    def validate_for(
        self, identity: CaptureRunIdentity, *, certification_symbol: str
    ) -> None:
        if sha256_json(self.code_build) != identity.code_build_sha256:
            raise CaptureContractError("code-build evidence does not match run identity")
        if sha256_json(self.config) != identity.config_sha256:
            raise CaptureContractError("config evidence does not match run identity")
        if sha256_json(self.feature_flags) != identity.feature_flags_sha256:
            raise CaptureContractError("feature flags do not match run identity")
        if sha256_json(self.account_identity) != identity.account_identity_sha256:
            raise CaptureContractError("account identity evidence does not match run identity")
        if not isinstance(self.account_risk_snapshot, Mapping):
            raise CaptureContractError("account risk snapshot must be a mapping")
        if not isinstance(self.account_query, Mapping) or not self.account_query:
            raise CaptureContractError("account snapshot requires exact query parameters")
        if not str(self.account_provider or "").strip():
            raise CaptureContractError("account snapshot provider is required")
        if (
            str(self.config.get("capture_certification_symbol") or "")
            .strip()
            .upper()
            != certification_symbol
        ):
            raise CaptureContractError(
                "effective config does not bind the capture certification symbol"
            )


@dataclass(frozen=True)
class ObservedCaptureInput:
    """One already-observed provider result; this type has no fetch callback."""

    payload: Mapping[str, Any]
    clocks: CaptureClocks

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise CaptureContractError("observed capture payload must be a mapping")
        if not isinstance(self.clocks, CaptureClocks):
            raise CaptureContractError("observed capture clocks are malformed")


@dataclass(frozen=True)
class CaptureSubmission:
    accepted: bool
    event: CaptureEvent | None
    coverage_gap_recorded: bool
    disposition: str
    explicit_decision_rejection: bool = False

    def __post_init__(self) -> None:
        if self.accepted != (self.event is not None):
            raise CaptureContractError("accepted submission must retain its exact event")
        if self.accepted and (
            self.coverage_gap_recorded or self.explicit_decision_rejection
        ):
            raise CaptureContractError(
                "accepted capture cannot also be rejected or gapped"
            )
        if not self.accepted and not (
            self.coverage_gap_recorded or self.explicit_decision_rejection
        ):
            raise CaptureContractError("capture rejection cannot be silent")


@dataclass(frozen=True)
class HotPromotionResult:
    symbol: str
    lease: HotSymbolLease | None
    promoted_submissions: tuple[CaptureSubmission, ...]
    reported_gaps: tuple[CoverageGap, ...]

    @property
    def hot(self) -> bool:
        return self.lease is not None


@dataclass(frozen=True)
class CapturedReadResult:
    receipt: CaptureReadReceipt | None
    source_events: tuple[CaptureEvent, ...]
    receipt_submission: CaptureSubmission | None
    coverage_gap_recorded: bool
    first_dip_tape_evidence: FirstDipTapeReceiptEvidence | None = None

    @property
    def durable(self) -> bool:
        return bool(
            self.receipt is not None
            and self.receipt_submission is not None
            and self.receipt_submission.accepted
            and not self.coverage_gap_recorded
        )


_EXECUTED_CAPTURE_SOURCE_SCHEMA_VERSION = (
    "chili.captured-paper-executed-source-event.v1"
)
_EXECUTED_CAPTURE_READ_SCHEMA_VERSION = (
    "chili.captured-paper-executed-read.v1"
)
_EXECUTED_CAPTURE_INVENTORY_SCHEMA_VERSION = (
    "chili.captured-paper-executed-read-inventory.v1"
)


@dataclass(frozen=True)
class ExecutedCaptureSourceEventEvidence:
    """Canonical source-event identity consumed by one executed provider read."""

    source_index: int
    sequence: int
    event_sha256: str
    payload_sha256: str
    query_sha256: str | None
    stream: str
    provider: str
    symbol: str | None
    provider_event_at: datetime | None
    market_reference_at: datetime | None
    received_at: datetime
    available_at: datetime
    schema_version: str = _EXECUTED_CAPTURE_SOURCE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_index": self.source_index,
            "sequence": self.sequence,
            "event_sha256": self.event_sha256,
            "payload_sha256": self.payload_sha256,
            "query_sha256": self.query_sha256,
            "stream": self.stream,
            "provider": self.provider,
            "symbol": self.symbol,
            "clocks": {
                "provider_event_at": (
                    None
                    if self.provider_event_at is None
                    else self.provider_event_at.isoformat().replace("+00:00", "Z")
                ),
                "market_reference_at": (
                    None
                    if self.market_reference_at is None
                    else self.market_reference_at.isoformat().replace("+00:00", "Z")
                ),
                "received_at": self.received_at.isoformat().replace("+00:00", "Z"),
                "available_at": self.available_at.isoformat().replace("+00:00", "Z"),
            },
        }


@dataclass(frozen=True)
class ExecutedCaptureReadEvidence:
    """Full durable read/receipt identity that affected one real FSM tick."""

    run_id: str
    generation: int
    identity_sha256: str
    decision_id: str
    stream: str
    provider: str
    symbol: str | None
    read_id: str
    receipt_canonical_json: str
    receipt_sha256: str
    receipt_event_sha256: str
    receipt_event_sequence: int
    receipt_committed_available_at: datetime
    result_sha256: str
    replay_network_fallback_used: bool
    source_events: tuple[ExecutedCaptureSourceEventEvidence, ...]
    schema_version: str = _EXECUTED_CAPTURE_READ_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "generation": self.generation,
            "identity_sha256": self.identity_sha256,
            "decision_id": self.decision_id,
            "stream": self.stream,
            "provider": self.provider,
            "symbol": self.symbol,
            "read_id": self.read_id,
            "receipt_canonical_json": self.receipt_canonical_json,
            "receipt_sha256": self.receipt_sha256,
            "receipt_event_sha256": self.receipt_event_sha256,
            "receipt_event_sequence": self.receipt_event_sequence,
            "receipt_committed_available_at": (
                self.receipt_committed_available_at.isoformat().replace(
                    "+00:00", "Z"
                )
            ),
            "result_sha256": self.result_sha256,
            "replay_network_fallback_used": self.replay_network_fallback_used,
            "source_events": [row.to_dict() for row in self.source_events],
        }

    @property
    def evidence_sha256(self) -> str:
        return sha256_json(self.to_dict())


@dataclass(frozen=True)
class ExecutedCaptureReadInventory:
    """Hash-bound execution-time provider reads for one exact FSM decision."""

    run_id: str
    generation: int
    identity_sha256: str
    decision_id: str
    reads: tuple[ExecutedCaptureReadEvidence, ...]
    schema_version: str = _EXECUTED_CAPTURE_INVENTORY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "generation": self.generation,
            "identity_sha256": self.identity_sha256,
            "decision_id": self.decision_id,
            "reads": [row.to_dict() for row in self.reads],
        }

    @property
    def inventory_sha256(self) -> str:
        return sha256_json(self.to_dict())


def executed_capture_read_evidence(
    captured: CapturedReadResult,
) -> ExecutedCaptureReadEvidence:
    """Export one durable read without reconstructing any receipt or event."""

    if type(captured) is not CapturedReadResult or not captured.durable:
        raise CaptureContractError("executed capture read is not durable")
    receipt = captured.receipt
    receipt_submission = captured.receipt_submission
    assert receipt is not None and receipt_submission is not None
    receipt_event = receipt_submission.event
    if (
        receipt_event is None
        or receipt_event.stream is not CaptureStream.READ_RECEIPT
        or receipt_event.payload != receipt.to_dict()
        or receipt.identity_sha256 != receipt_event.identity.identity_sha256
        or receipt.replay_network_fallback_used is not False
        or receipt.content_verified is not True
        or captured.coverage_gap_recorded
    ):
        raise CaptureContractError(
            "executed capture receipt identity is unavailable"
        )
    sources = tuple(captured.source_events)
    if (
        tuple(event.event_sha256 for event in sources)
        != receipt.source_event_sha256s
        or receipt.result_sha256
        != captured_read_result_sha256(
            tuple(CaptureEventRef.from_event(event) for event in sources)
        )
        or any(
            event.identity != receipt_event.identity
            or event.stream is not receipt.stream
            or event.provider != receipt.provider
            or event.symbol != receipt.symbol
            or event.sequence >= receipt_event.sequence
            for event in sources
        )
        or tuple(event.sequence for event in sources)
        != tuple(sorted(event.sequence for event in sources))
        or len({event.sequence for event in sources}) != len(sources)
    ):
        raise CaptureContractError(
            "executed capture source inventory differs from its receipt"
        )
    receipt_json = canonical_json_bytes(receipt.to_dict()).decode("utf-8")
    source_evidence = tuple(
        ExecutedCaptureSourceEventEvidence(
            source_index=index,
            sequence=event.sequence,
            event_sha256=event.event_sha256,
            payload_sha256=event.payload_sha256,
            query_sha256=event.query_sha256,
            stream=event.stream.value,
            provider=event.provider,
            symbol=event.symbol,
            provider_event_at=event.clocks.provider_event_at,
            market_reference_at=event.clocks.market_reference_at,
            received_at=event.clocks.received_at,
            available_at=event.clocks.available_at,
        )
        for index, event in enumerate(sources)
    )
    return ExecutedCaptureReadEvidence(
        run_id=receipt_event.identity.run_id,
        generation=receipt_event.identity.generation,
        identity_sha256=receipt_event.identity.identity_sha256,
        decision_id=receipt.decision_id,
        stream=receipt.stream.value,
        provider=receipt.provider,
        symbol=receipt.symbol,
        read_id=receipt.read_id,
        receipt_canonical_json=receipt_json,
        receipt_sha256=sha256_json(receipt.to_dict()),
        receipt_event_sha256=receipt_event.event_sha256,
        receipt_event_sequence=receipt_event.sequence,
        receipt_committed_available_at=receipt_event.clocks.available_at,
        result_sha256=receipt.result_sha256,
        replay_network_fallback_used=False,
        source_events=source_evidence,
    )


def build_executed_capture_read_inventory(
    *,
    identity: CaptureRunIdentity,
    decision_id: str,
    captured_reads: Sequence[CapturedReadResult],
) -> ExecutedCaptureReadInventory:
    """Build the canonical phase-one handoff for actual in-tick provider reads."""

    if type(identity) is not CaptureRunIdentity:
        raise CaptureContractError("executed read inventory identity is malformed")
    normalized_decision = str(decision_id or "").strip()
    if not normalized_decision:
        raise CaptureContractError("executed read inventory decision id is missing")
    reads = tuple(executed_capture_read_evidence(row) for row in captured_reads)
    if not reads:
        raise CaptureContractError("executed read inventory is empty")
    if any(
        row.run_id != identity.run_id
        or row.generation != identity.generation
        or row.identity_sha256 != identity.identity_sha256
        or row.decision_id != normalized_decision
        for row in reads
    ):
        raise CaptureContractError(
            "executed read inventory escaped decision or run identity"
        )
    ordered = tuple(
        sorted(reads, key=lambda row: (row.receipt_event_sequence, row.read_id))
    )
    if (
        len({row.read_id for row in ordered}) != len(ordered)
        or len({row.receipt_event_sequence for row in ordered}) != len(ordered)
    ):
        raise CaptureContractError("executed read inventory is duplicated")
    return ExecutedCaptureReadInventory(
        run_id=identity.run_id,
        generation=identity.generation,
        identity_sha256=identity.identity_sha256,
        decision_id=normalized_decision,
        reads=ordered,
    )


@dataclass(frozen=True)
class ChangeCaptureResult:
    changed: bool
    submission: CaptureSubmission | None
    coverage_gap_recorded: bool = False
    current_event: CaptureEvent | None = field(default=None, compare=False)


def _pretrigger_change_capture_result(
    result: PreTriggerRetainResult,
) -> ChangeCaptureResult:
    """Translate bounded-ring change admission without treating dedup as a gap."""

    if result.unchanged:
        return ChangeCaptureResult(changed=False, submission=None)
    submission = CaptureSubmission(
        accepted=result.event is not None,
        event=result.event,
        coverage_gap_recorded=result.coverage_gap_recorded,
        disposition=result.disposition,
    )
    return ChangeCaptureResult(
        changed=True,
        submission=submission,
        coverage_gap_recorded=result.coverage_gap_recorded,
        current_event=submission.event,
    )


@dataclass(frozen=True)
class UnverifiedDecisionPrefix:
    """Decision-time prefix receipt which is intentionally noncertifying."""

    checkpoint: CaptureDecisionCheckpoint
    decision_event: CaptureEvent
    capture_root: Path
    independently_verified: bool = False
    certification_eligible: bool = False
    replay_network_fallback_allowed: bool = False

    def as_certifying_adaptive_evidence(self) -> None:
        raise CaptureContractError(
            "a live decision prefix is not certifying adaptive evidence; require "
            "clean final seal, independent expected SHA, verified loader, complete "
            "coverage, and isolated no-egress replay"
        )


@dataclass(frozen=True)
class UnsealedCaptureClose:
    identity: CaptureRunIdentity
    capture_root: Path
    reason: str
    writer_stopped: bool
    independently_verified: bool = False
    certification_eligible: bool = False

    def as_certifying_adaptive_evidence(self) -> None:
        raise CaptureContractError("an unsealed capture can never certify adaptive evidence")


@dataclass(frozen=True)
class SealedCaptureHandoff:
    """Clean local seal output awaiting an independent pin and verified load."""

    identity: CaptureRunIdentity
    capture_root: Path
    final_seal_sha256: str
    event_count: int
    gap_count: int
    gap_lost_count: int
    resource_hashes: Mapping[str, str]
    sequence_min: int | None
    sequence_max: int | None
    producer_lifecycle_candidate: bool
    independently_verified: bool = False
    certification_eligible: bool = False
    independent_expected_seal_required: bool = True

    def as_certifying_adaptive_evidence(self) -> None:
        raise CaptureContractError(
            "a locally produced seal is only a handoff; certification requires an "
            "independently supplied expected seal SHA and the official verified loader"
        )


@runtime_checkable
class LiveReplayCapturePort(Protocol):
    """Provider-facing capture-only seam; implementations must never fetch data."""

    @property
    def network_fallback_allowed(self) -> bool: ...

    def submit_exact_input(
        self,
        *,
        stream: CaptureStream,
        provider: str,
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
        symbol: str | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> CaptureSubmission: ...

    def capture_query_result(
        self,
        *,
        decision_id: str,
        stream: CaptureStream,
        provider: str,
        query: Mapping[str, Any],
        requested_at: datetime,
        returned_at: datetime,
        results: Sequence[ObservedCaptureInput],
        symbol: str | None = None,
        read_id: str | None = None,
    ) -> CapturedReadResult: ...

    def capture_durable_read(
        self,
        *,
        decision_id: str,
        stream: CaptureStream,
        provider: str,
        query: Mapping[str, Any],
        requested_at: datetime,
        returned_at: datetime,
        source_events: Sequence[CaptureEvent],
        symbol: str | None = None,
        read_id: str | None = None,
        first_dip_tape: bool = False,
    ) -> CapturedReadResult: ...

    def capture_complete_microstructure_window(
        self,
        *,
        decision_id: str,
        operation: CaptureMicrostructureOperation,
        stream: CaptureStream,
        provider: str,
        symbol: str,
        requested_at: datetime,
        returned_at: datetime,
        event_start_exclusive: datetime,
        event_end_inclusive: datetime,
        parameters: Mapping[str, Any],
        read_id: str | None = None,
    ) -> CapturedReadResult: ...


class LiveReplayCaptureCoordinator:
    """Thread-safe orchestration around the bounded capture runtime.

    The shared producer lifecycle owns sequence assignment.  The coordinator is
    the only supported producer-facing path into that lifecycle and keeps an
    independently checked incremental hash of the contiguous decision prefix
    rather than retaining every payload or event reference in RAM.
    """

    def __init__(
        self,
        *,
        identity: CaptureRunIdentity,
        certification_symbol: str,
        resource_binding: CaptureResourceBinding,
        pressure_controller: CaptureAdaptivePressureController,
        ingress: BoundedCaptureIngress,
        pretrigger_ring: BoundedPreTriggerRing,
        hot_symbol_leases: BoundedHotSymbolLeases,
        store: ContentAddressedCaptureStore,
        writer: CaptureWriterWorker | CaptureWriterPool,
        producer_lifecycle: CaptureProducerLifecycleRuntime,
        wall_clock: Callable[[], datetime],
        max_change_keys: int,
        max_read_sources: int,
        shared_writer_lease: SharedCaptureWriterLease | None = None,
    ) -> None:
        if not isinstance(identity, CaptureRunIdentity):
            raise CaptureContractError("capture coordinator identity is malformed")
        normalized_certification_symbol = _normalized_symbol(
            certification_symbol, required=True
        )
        assert normalized_certification_symbol is not None
        if not isinstance(resource_binding, CaptureResourceBinding):
            raise CaptureContractError("capture coordinator resource binding is malformed")
        if pressure_controller.binding != resource_binding:
            raise CaptureContractError("capture coordinator pressure binding mismatch")
        if ingress.resource_binding != resource_binding:
            raise CaptureContractError("capture coordinator ingress binding mismatch")
        if pretrigger_ring.resource_binding != resource_binding:
            raise CaptureContractError("capture coordinator ring binding mismatch")
        if hot_symbol_leases.identity != identity:
            raise CaptureContractError("capture coordinator hot-symbol identity mismatch")
        if hot_symbol_leases.resource_binding != resource_binding:
            raise CaptureContractError("capture coordinator hot-symbol binding mismatch")
        if store.resource_binding != resource_binding:
            raise CaptureContractError("capture coordinator store binding mismatch")
        if writer.ingress is not ingress or writer.store is not store:
            raise CaptureContractError("capture coordinator writer ownership mismatch")
        if shared_writer_lease is not None:
            if not isinstance(shared_writer_lease, SharedCaptureWriterLease):
                raise CaptureContractError("capture coordinator shared lease is malformed")
            if (
                shared_writer_lease.identity != identity
                or shared_writer_lease.store is not store
                or shared_writer_lease.writer is not writer
            ):
                raise CaptureContractError(
                    "capture coordinator shared writer lease boundary mismatch"
                )
        if producer_lifecycle.identity != identity:
            raise CaptureContractError("capture coordinator producer identity mismatch")
        if producer_lifecycle.ingress is not ingress:
            raise CaptureContractError("capture coordinator producer ingress mismatch")
        if producer_lifecycle.resource_binding != resource_binding:
            raise CaptureContractError("capture coordinator producer binding mismatch")
        if not callable(wall_clock):
            raise CaptureContractError("capture coordinator wall clock is malformed")
        if min(int(max_change_keys), int(max_read_sources)) <= 0:
            raise CaptureContractError("capture coordinator finite bounds must be positive")
        if int(max_change_keys) > resource_binding.budget.max_ring_events:
            raise CaptureContractError("change-key bound exceeds measured ring budget")
        if int(max_read_sources) > resource_binding.budget.max_ring_events:
            raise CaptureContractError("read-source bound exceeds measured ring budget")

        self.identity = identity
        self.certification_symbol = normalized_certification_symbol
        self.resource_binding = resource_binding
        self.pressure_controller = pressure_controller
        self.ingress = ingress
        self.pretrigger_ring = pretrigger_ring
        self.hot_symbol_leases = hot_symbol_leases
        self.store = store
        self.writer = writer
        self._shared_writer_lease = shared_writer_lease
        self._producer_lifecycle = producer_lifecycle
        self._wall_clock = wall_clock
        self.max_change_keys = int(max_change_keys)
        self.max_read_sources = int(max_read_sources)
        self.capture_root = store.root

        self._state = CaptureSessionState.CREATED
        self._lock = threading.RLock()
        self._hot_by_symbol: dict[str, HotSymbolLease] = {}
        self._change_hashes: OrderedDict[tuple[str, str, str, str], str] = OrderedDict()
        self._change_events: dict[
            tuple[str, str, str, str], CaptureEvent
        ] = {}
        # Bounded current-state inventory for the capture-backed PAPER host.
        # Keys can only be one of the finite CaptureStream values and either
        # the certification symbol or the global ``None`` scope.  Query-backed
        # entries are installed only after their complete result receipt is
        # durable, never while an individual result row is being appended.
        self._latest_state_sources: dict[
            tuple[CaptureStream, str | None], tuple[CaptureEvent, ...]
        ] = {}
        self._accepted_count = 0
        self._rejected_count = 0
        self._prefix_hasher = hashlib.sha256()
        self._prefix_hasher.update(b'{"events":[')
        self._prefix_rows = 0
        self._owner_by_stream: dict[CaptureStream, str] = {
            stream: producer.producer_id
            for producer in producer_lifecycle.producers.values()
            for stream in producer.streams
        }
        startup_owners = {
            self._owner_by_stream.get(stream)
            for stream in _COORDINATOR_STARTUP_STREAMS
        }
        if None in startup_owners or len(startup_owners) != 1:
            raise CaptureContractError(
                "one coordinator producer must own every startup identity stream"
            )
        self._coordinator_producer_id = startup_owners.pop()
        assert self._coordinator_producer_id is not None
        coordinator_spec = producer_lifecycle.producers[self._coordinator_producer_id]
        if coordinator_spec.generation != identity.generation:
            raise CaptureContractError(
                "coordinator producer generation differs from run generation"
            )
        # External producer ingress is capability-bound.  The coordinator's
        # identity/account producer is local and is the only producer that may
        # use the general coordinator methods without an endpoint token.
        self._external_producer_tokens: dict[str, object] = {}
        self._external_registration_evidence: dict[
            str, CaptureProviderRegistrationEvidence
        ] = {}
        self._pending_watermarks: dict[CaptureStream, ProviderWatermark] = {}
        self._finalized_coverages: set[CaptureStream] = set()
        self._first_dip_final_frontier_by_decision: dict[
            str, FirstDipFinalCaptureFrontier
        ] = {}
        self._first_dip_final_request_json_by_decision: dict[str, str] = {}
        self._checkpointed_first_dip_final_frontiers: set[str] = set()
        self._stream_stats: dict[CaptureStream, dict[str, Any]] = {}
        # A gap may be reported before the first durable input/empty receipt for
        # its stream.  Keep that fact independently so later lazy stats
        # creation cannot accidentally restore ``continuity_complete=True``.
        self._gapped_streams: set[CaptureStream] = set()
        # Repeated retries can rediscover the same already-recorded loss.  Keep
        # a resource-bounded content-addressed inventory so one physical gap
        # produces one durable fact instead of unbounded duplicate control rows.
        self._reported_gap_hashes: OrderedDict[str, None] = OrderedDict()
        self._max_reported_gap_hashes = int(
            self.resource_binding.budget.max_gap_keys
        )
        self._supervisor_token: object | None = None
        self._supervised_hot_tokens: set[str] = set()
        self._supervised_external_endpoints: dict[
            str, BoundLiveCaptureProducer
        ] = {}
        self._supervisor_hot_health: Mapping[str, Any] | None = None

    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        identity: CaptureRunIdentity,
        certification_symbol: str,
        resource_binding: CaptureResourceBinding,
        pressure_controller: CaptureAdaptivePressureController,
        producers: Sequence[CaptureProducerSpec],
        heartbeat_timeout_seconds: float,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        pretrigger_horizon: timedelta,
        per_symbol_pretrigger_events: int,
        writer_workers: int = 1,
        writer_batch_events: int = 512,
        writer_batch_bytes: int | None = None,
        writer_poll_seconds: float = 0.05,
        writer_flush_interval_seconds: float = 1.0,
        max_change_keys: int | None = None,
        max_read_sources: int | None = None,
        shared_admission_budget: SharedCaptureAdmissionBudget | None = None,
        compression_codec: str = "zstd",
        compression_level: int = 3,
    ) -> "LiveReplayCaptureCoordinator":
        """Construct exact-binding runtime components without starting a thread."""

        if pressure_controller.binding != resource_binding:
            raise CaptureContractError("capture coordinator pressure binding mismatch")
        ingress = BoundedCaptureIngress.from_resource_binding(
            resource_binding,
            pressure_controller=pressure_controller,
            shared_admission_budget=shared_admission_budget,
        )
        ring = BoundedPreTriggerRing.from_resource_binding(
            resource_binding,
            horizon=pretrigger_horizon,
            per_symbol_max_events=per_symbol_pretrigger_events,
            pressure_controller=pressure_controller,
        )
        hot = BoundedHotSymbolLeases(
            identity=identity,
            resource_binding=resource_binding,
            pressure_controller=pressure_controller,
        )
        store = ContentAddressedCaptureStore(
            root,
            compression_codec=compression_codec,
            compression_level=compression_level,
            resource_binding=resource_binding,
            wall_clock=wall_clock,
        )
        producer_lifecycle = CaptureProducerLifecycleRuntime(
            identity=identity,
            ingress=ingress,
            resource_binding=resource_binding,
            producers=producers,
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
            wall_clock=wall_clock,
        )
        batch_bytes = int(
            writer_batch_bytes or min(
                resource_binding.budget.async_queue_bytes,
                8 * 1024 * 1024,
            )
        )
        if int(writer_workers) == 1:
            writer: CaptureWriterWorker | CaptureWriterPool = CaptureWriterWorker(
                ingress=ingress,
                store=store,
                batch_events=writer_batch_events,
                batch_bytes=batch_bytes,
                poll_seconds=writer_poll_seconds,
                flush_interval_seconds=writer_flush_interval_seconds,
            )
        else:
            writer = CaptureWriterPool(
                ingress=ingress,
                store=store,
                workers=writer_workers,
                batch_events=writer_batch_events,
                batch_bytes=batch_bytes,
                poll_seconds=writer_poll_seconds,
                flush_interval_seconds=writer_flush_interval_seconds,
            )
        return cls(
            identity=identity,
            certification_symbol=certification_symbol,
            resource_binding=resource_binding,
            pressure_controller=pressure_controller,
            ingress=ingress,
            pretrigger_ring=ring,
            hot_symbol_leases=hot,
            store=store,
            writer=writer,
            producer_lifecycle=producer_lifecycle,
            wall_clock=wall_clock,
            max_change_keys=(
                int(max_change_keys)
                if max_change_keys is not None
                else min(
                    resource_binding.budget.max_ring_events,
                    resource_binding.budget.max_gap_keys * 8,
                )
            ),
            max_read_sources=(
                int(max_read_sources)
                if max_read_sources is not None
                else resource_binding.budget.max_ring_events
            ),
        )

    @classmethod
    def create_with_shared_store(
        cls,
        *,
        identity: CaptureRunIdentity,
        certification_symbol: str,
        resource_binding: CaptureResourceBinding,
        pressure_controller: CaptureAdaptivePressureController,
        shared_store_runtime: SharedCaptureStoreRuntime,
        producers: Sequence[CaptureProducerSpec],
        heartbeat_timeout_seconds: float,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        pretrigger_horizon: timedelta,
        per_symbol_pretrigger_events: int,
        writer_batch_events: int = 512,
        writer_batch_bytes: int | None = None,
        writer_poll_seconds: float = 0.05,
        writer_flush_interval_seconds: float = 1.0,
        max_change_keys: int | None = None,
        max_read_sources: int | None = None,
    ) -> "LiveReplayCaptureCoordinator":
        """Build one run on the process-wide quota/store and one writer lease."""

        if not isinstance(shared_store_runtime, SharedCaptureStoreRuntime):
            raise CaptureContractError("shared capture store runtime is malformed")
        if pressure_controller.binding != resource_binding:
            raise CaptureContractError("capture coordinator pressure binding mismatch")
        if shared_store_runtime.resource_binding != resource_binding:
            raise CaptureContractError(
                "capture coordinator shared store binding mismatch"
            )
        shared_admission = shared_store_runtime.shared_admission_budget
        if shared_admission.pressure_controller is not pressure_controller:
            raise CaptureContractError(
                "capture coordinator shared pressure controller mismatch"
            )
        ingress = BoundedCaptureIngress.from_resource_binding(
            resource_binding,
            pressure_controller=pressure_controller,
            shared_admission_budget=shared_admission,
        )
        ring = BoundedPreTriggerRing.from_resource_binding(
            resource_binding,
            horizon=pretrigger_horizon,
            per_symbol_max_events=per_symbol_pretrigger_events,
            pressure_controller=pressure_controller,
        )
        hot = BoundedHotSymbolLeases(
            identity=identity,
            resource_binding=resource_binding,
            pressure_controller=pressure_controller,
        )
        producer_lifecycle = CaptureProducerLifecycleRuntime(
            identity=identity,
            ingress=ingress,
            resource_binding=resource_binding,
            producers=producers,
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
            wall_clock=wall_clock,
        )
        batch_bytes = int(
            writer_batch_bytes
            or min(
                resource_binding.budget.async_queue_bytes,
                8 * 1024 * 1024,
            )
        )
        lease = shared_store_runtime.acquire(identity)
        try:
            writer = lease.build_writer(
                ingress=ingress,
                batch_events=writer_batch_events,
                batch_bytes=batch_bytes,
                poll_seconds=writer_poll_seconds,
                flush_interval_seconds=writer_flush_interval_seconds,
            )
            return cls(
                identity=identity,
                certification_symbol=certification_symbol,
                resource_binding=resource_binding,
                pressure_controller=pressure_controller,
                ingress=ingress,
                pretrigger_ring=ring,
                hot_symbol_leases=hot,
                store=shared_store_runtime.store,
                writer=writer,
                producer_lifecycle=producer_lifecycle,
                wall_clock=wall_clock,
                max_change_keys=(
                    int(max_change_keys)
                    if max_change_keys is not None
                    else min(
                        resource_binding.budget.max_ring_events,
                        resource_binding.budget.max_gap_keys * 8,
                    )
                ),
                max_read_sources=(
                    int(max_read_sources)
                    if max_read_sources is not None
                    else resource_binding.budget.max_ring_events
                ),
                shared_writer_lease=lease,
            )
        except BaseException:
            lease.release()
            raise

    def _release_storage(self) -> None:
        lease = self._shared_writer_lease
        if lease is None:
            self.store.close()
        else:
            lease.release()

    def discard_unstarted(self, *, reason: str) -> UnsealedCaptureClose:
        """Release an inert CREATED run which never emitted lifecycle evidence."""

        normalized_reason = str(reason or "unstarted_capture_discarded").strip()
        if not normalized_reason:
            raise CaptureContractError("unstarted capture discard reason is required")
        with self._lock:
            if self._state is not CaptureSessionState.CREATED:
                raise CaptureContractError("only an unstarted capture can be discarded")
            self._release_storage()
            self._state = CaptureSessionState.ABORTED
            return UnsealedCaptureClose(
                identity=self.identity,
                capture_root=self.capture_root,
                reason=normalized_reason,
                writer_stopped=True,
            )

    @property
    def state(self) -> CaptureSessionState:
        with self._lock:
            return self._state

    @property
    def network_fallback_allowed(self) -> bool:
        return False

    def _require_running(self) -> None:
        if self._state is not CaptureSessionState.RUNNING:
            raise CaptureContractError(
                f"capture coordinator is not running: {self._state.value}"
            )

    def _bind_supervisor(
        self,
        token: object,
        *,
        shared_admission_budget: SharedCaptureAdmissionBudget,
        hot_health: Mapping[str, Any],
    ) -> None:
        with self._lock:
            if self._state is not CaptureSessionState.CREATED:
                raise CaptureContractError(
                    "capture supervisor must bind before coordinator start"
                )
            if self._supervisor_token is not None:
                raise CaptureContractError("capture coordinator already has a supervisor")
            if self.ingress.shared_admission_budget is not shared_admission_budget:
                raise CaptureContractError(
                    "supervised coordinator lacks the exact shared admission budget"
                )
            external_specs = tuple(
                producer
                for producer in self._producer_lifecycle.producers.values()
                if producer.producer_id != self._coordinator_producer_id
            )
            if self._external_producer_tokens:
                raise CaptureContractError(
                    "supervised coordinator cannot adopt pre-bound external producers"
                )
            endpoints: dict[str, BoundLiveCaptureProducer] = {}
            for spec in sorted(external_specs, key=lambda row: row.producer_id):
                capability = object()
                self._external_producer_tokens[spec.producer_id] = capability
                endpoints[spec.producer_id] = BoundLiveCaptureProducer(
                    coordinator=self,
                    producer_id=spec.producer_id,
                    token=capability,
                    spec=spec,
                )
            self._supervisor_token = token
            self._supervised_external_endpoints = endpoints
            self._supervisor_hot_health = dict(hot_health)

    def _require_supervisor_token(self, token: object) -> None:
        if self._supervisor_token is None or token is not self._supervisor_token:
            raise CaptureContractError("capture supervisor ownership token mismatch")

    def _update_supervisor_hot_health(
        self, token: object, health: Mapping[str, Any]
    ) -> None:
        with self._lock:
            self._require_supervisor_token(token)
            self._supervisor_hot_health = dict(health)

    def _update_prefix(self, event: CaptureEvent) -> None:
        if event.sequence != self._prefix_rows + 1:
            raise CaptureContractError("durable capture sequence is not contiguous")
        if self._prefix_rows:
            self._prefix_hasher.update(b",")
        self._prefix_hasher.update(
            canonical_json_bytes(
                {
                    "sequence": event.sequence,
                    "event_sha256": event.event_sha256,
                    "available_at": event.clocks.to_dict()["available_at"],
                }
            )
        )
        self._prefix_rows += 1

    def _current_prefix_root(self) -> str:
        if self._prefix_rows <= 0:
            raise CaptureContractError("decision prefix has no durable events")
        digest = self._prefix_hasher.copy()
        digest.update(b'],"identity_sha256":')
        digest.update(canonical_json_bytes(self.identity.identity_sha256))
        digest.update(b',"through_sequence":')
        digest.update(str(self._prefix_rows).encode("ascii"))
        digest.update(b"}")
        return digest.hexdigest()

    def _observed_now(self) -> datetime:
        return _utc(self._wall_clock(), "capture coordinator wall clock")

    def _producer_for_stream(
        self, stream: CaptureStream, producer_id: str | None = None
    ) -> str:
        owner = self._owner_by_stream.get(stream)
        if owner is None:
            raise CaptureContractError(f"no declared producer owns {stream.value}")
        requested = str(producer_id or owner).strip().lower()
        if requested != owner:
            raise CaptureContractError(
                f"producer {requested} does not own {stream.value}; owner is {owner}"
            )
        return owner

    def _require_certification_symbol(
        self, stream: CaptureStream, symbol: str | None
    ) -> str | None:
        normalized = _normalized_symbol(symbol)
        if stream is CaptureStream.PROVIDER_OHLCV:
            if normalized is None:
                raise CaptureContractError(
                    "provider_ohlcv requires its exact queried symbol"
                )
            return normalized
        if stream not in _CERTIFICATION_SYMBOL_STREAMS:
            return normalized
        if normalized != self.certification_symbol:
            raise CaptureContractError(
                f"{stream.value} belongs to certifying symbol "
                f"{self.certification_symbol}, not {normalized or 'NONE'}"
            )
        return normalized

    def _observe_stream_event(self, event: CaptureEvent) -> None:
        if STREAM_POLICIES[event.stream].coverage_mode is CoverageMode.CONTROL:
            return
        stats = self._stream_stats.setdefault(
            event.stream,
            {
                "event_count": 0,
                "first_available_at": event.clocks.available_at,
                "last_available_at": event.clocks.available_at,
                "providers": set(),
                "symbols": set(),
                "exact_event_clock_complete": True,
                "query_receipt_count": 0,
                "gapped": event.stream in self._gapped_streams,
            },
        )
        stats["event_count"] += 1
        stats["first_available_at"] = min(
            stats["first_available_at"], event.clocks.available_at
        )
        stats["last_available_at"] = max(
            stats["last_available_at"], event.clocks.available_at
        )
        stats["providers"].add(event.provider)
        stats["symbols"].add(event.symbol)
        if STREAM_POLICIES[event.stream].exact_provider_event_clock_required:
            stats["exact_event_clock_complete"] = bool(
                stats["exact_event_clock_complete"]
                and event.clocks.provider_event_at is not None
            )

    def _observe_durable_event(self, event: CaptureEvent) -> None:
        self._accepted_count += 1
        self._update_prefix(event)
        self._observe_stream_event(event)
        policy = STREAM_POLICIES[event.stream]
        if (
            policy.coverage_mode in _CURRENT_STATE_INVENTORY_MODES
            and policy.coverage_mode is not CoverageMode.QUERY_RECEIPT
            and event.symbol in {None, self.certification_symbol}
        ):
            self._latest_state_sources[(event.stream, event.symbol)] = (event,)

    def _observe_durable_pointer(
        self,
        *,
        sequence: int,
        event_sha256: str,
        available_at: datetime,
    ) -> None:
        """Advance the local prefix for runtime-issued typed control evidence."""

        if sequence != self._prefix_rows + 1:
            raise CaptureContractError(
                "runtime control evidence is not contiguous with coordinator prefix"
            )
        if self._prefix_rows:
            self._prefix_hasher.update(b",")
        self._prefix_hasher.update(
            canonical_json_bytes(
                {
                    "sequence": sequence,
                    "event_sha256": event_sha256,
                    "available_at": _utc(
                        available_at, "control evidence available_at"
                    ).isoformat().replace("+00:00", "Z"),
                }
            )
        )
        self._prefix_rows += 1
        self._accepted_count += 1

    def _submit_event_locked(
        self,
        *,
        stream: CaptureStream,
        provider: str,
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
        symbol: str | None = None,
        query: Mapping[str, Any] | None = None,
        producer_id: str | None = None,
        producer_token: object | None = None,
        promotion_id: str | None = None,
        promoted_at: datetime | None = None,
        promotion_source_identity_sha256: str | None = None,
        promotion_resource_binding_sha256: str | None = None,
        promotion_inventory_sha256: str | None = None,
        promotion_admission_handoff_sha256: str | None = None,
        promotion_transfer: PromotionTransfer | None = None,
        predecision_attestation: ActiveCaptureInputPrefixAttestation | None = None,
    ) -> CaptureSubmission:
        symbol = self._require_certification_symbol(stream, symbol)
        owner = self._producer_for_stream(stream, producer_id)
        if owner != self._coordinator_producer_id and (
            producer_token is None
            or self._external_producer_tokens.get(owner) is not producer_token
        ):
            raise CaptureContractError(
                f"external producer {owner} requires its bound capture endpoint"
            )
        try:
            event = self._producer_lifecycle.submit_input(
                owner,
                stream=stream,
                provider=provider,
                symbol=symbol,
                query=query,
                clocks=clocks,
                payload=payload,
                promotion_id=promotion_id,
                promoted_at=promoted_at,
                promotion_source_identity_sha256=promotion_source_identity_sha256,
                promotion_resource_binding_sha256=promotion_resource_binding_sha256,
                promotion_inventory_sha256=promotion_inventory_sha256,
                promotion_admission_handoff_sha256=(
                    promotion_admission_handoff_sha256
                ),
                promotion_transfer=promotion_transfer,
                predecision_attestation=predecision_attestation,
            )
        except CaptureContractError:
            if self._producer_lifecycle.health().get("submission_failure") is None:
                raise
            self._rejected_count += 1
            return CaptureSubmission(
                accepted=False,
                event=None,
                coverage_gap_recorded=True,
                disposition="ingress_rejected_gap_recorded",
            )
        self._observe_durable_event(event)
        return CaptureSubmission(
            accepted=True,
            event=event,
            coverage_gap_recorded=False,
            disposition="durable_ingress_accepted",
        )

    def _submit_gap_locked(self, gap: CoverageGap) -> bool:
        gap_sha256 = sha256_json(gap.to_dict())
        if gap_sha256 in self._reported_gap_hashes:
            self._reported_gap_hashes.move_to_end(gap_sha256)
            return True
        self._rejected_count += gap.lost_count
        self._gapped_streams.add(gap.stream)
        stats = self._stream_stats.get(gap.stream)
        if stats is not None:
            stats["gapped"] = True
        owner = self._owner_by_stream.get(gap.stream)
        if owner is None or (
            owner != self._coordinator_producer_id
            and owner not in self._external_registration_evidence
        ):
            # A provider can lose a frame before its first exact registration
            # candidate arrives.  Persist that loss directly in the bounded gap
            # ledger; inventing a REGISTERED fact merely to write a GAP would
            # convert absence of provider authority into false authority.
            accepted = self.ingress.submit_gap(self.identity, gap)
            if not accepted:
                return False
        else:
            event = self._producer_lifecycle.report_gap(
                owner,
                stream=gap.stream,
                reason=gap.reason,
                first_available_at=gap.first_available_at,
                last_available_at=gap.last_available_at,
                lost_count=gap.lost_count,
                symbol=gap.symbol,
            )
            self._observe_durable_event(event)
        self._reported_gap_hashes[gap_sha256] = None
        while len(self._reported_gap_hashes) > self._max_reported_gap_hashes:
            self._reported_gap_hashes.popitem(last=False)
        return True

    def record_coverage_gap(self, gap: CoverageGap) -> bool:
        """Persist one explicit in-process capture loss without provider fallback."""

        if not isinstance(gap, CoverageGap):
            raise CaptureContractError("capture coverage gap is malformed")
        with self._lock:
            self._require_running()
            self._require_certification_symbol(gap.stream, gap.symbol)
            return self._submit_gap_locked(gap)

    def observe_pressure(self, sample: CapturePressureSample) -> Mapping[str, Any]:
        with self._lock:
            if self._state in {CaptureSessionState.SEALED, CaptureSessionState.ABORTED}:
                raise CaptureContractError("closed capture cannot accept pressure samples")
            return self.pressure_controller.observe(sample)

    def start(
        self,
        evidence: CaptureIdentityEvidence,
        *,
        started_at: datetime | None = None,
    ) -> tuple[CaptureEvent, ...]:
        """Start writer and durably emit all run/account identity inputs."""

        evidence.validate_for(
            self.identity, certification_symbol=self.certification_symbol
        )
        with self._lock:
            if self._state is not CaptureSessionState.CREATED:
                raise CaptureContractError("capture coordinator is one-shot")
            if not self.pressure_controller.required_full_fidelity_admissible:
                raise CaptureContractError(
                    "capture cannot start without a fresh admissible resource sample"
                )
            try:
                self.writer.start()
            except BaseException:
                self._release_storage()
                self._state = CaptureSessionState.ABORTED
                raise
            self._state = CaptureSessionState.RUNNING
            try:
                lifecycle_events: list[CaptureEvent] = []
                opened = self._producer_lifecycle.open(opened_at=started_at)
                lifecycle_events.append(opened)
                self._observe_durable_event(opened)
                # This process can truthfully register only the producer it
                # owns: immutable run/config/account identity.  Provider
                # producers remain unregistered until their capability-bound
                # endpoint receives an exact provider frame for the declared
                # instance and generation.  Decision attestations already fail
                # closed while any RUN_OPEN producer is absent.
                registered = self._producer_lifecycle.register(
                    self._coordinator_producer_id, recorded_at=started_at
                )
                lifecycle_events.append(registered)
                self._observe_durable_event(registered)
                at = self._observed_now()
                clocks = CaptureClocks(received_at=at, available_at=at)
                rows = [
                    self._submit_event_locked(
                        stream=CaptureStream.CODE_BUILD,
                        provider=_CONTROL_PROVIDER,
                        payload=evidence.code_build,
                        clocks=clocks,
                    ),
                    self._submit_event_locked(
                        stream=CaptureStream.CONFIG_SNAPSHOT,
                        provider=_CONTROL_PROVIDER,
                        payload=evidence.config,
                        clocks=clocks,
                    ),
                    self._submit_event_locked(
                        stream=CaptureStream.FEATURE_FLAG_SNAPSHOT,
                        provider=_CONTROL_PROVIDER,
                        payload=evidence.feature_flags,
                        clocks=clocks,
                    ),
                    self._submit_event_locked(
                        stream=CaptureStream.ACCOUNT_RISK_SNAPSHOT,
                        provider=str(evidence.account_provider).strip(),
                        payload={
                            "account_identity": dict(evidence.account_identity),
                            "risk_snapshot": dict(evidence.account_risk_snapshot),
                        },
                        query=evidence.account_query,
                        clocks=CaptureClocks(
                            received_at=at,
                            available_at=at,
                            market_reference_at=at,
                        ),
                    ),
                ]
                account_event = rows[-1].event
                if account_event is None:
                    self._abort_locked(
                        reason="startup_account_admission_failed", at=at
                    )
                    raise CaptureContractError(
                        "startup account snapshot was not durably admitted"
                    )
                account_ref = CaptureEventRef.from_event(account_event)
                account_returned_at = self._observed_now()
                account_receipt = CaptureReadReceipt(
                    read_id=str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"chili-capture-account:{self.identity.identity_sha256}",
                        )
                    ),
                    decision_id=f"capture-startup-{self.identity.run_id}",
                    identity_sha256=self.identity.identity_sha256,
                    stream=CaptureStream.ACCOUNT_RISK_SNAPSHOT,
                    provider=str(evidence.account_provider).strip(),
                    symbol=None,
                    requested_at=opened.clocks.available_at,
                    returned_at=account_returned_at,
                    query_sha256=sha256_json(evidence.account_query),
                    source_event_sha256s=(account_event.event_sha256,),
                    empty_result=False,
                    result_sha256=captured_read_result_sha256((account_ref,)),
                    content_verified=True,
                    replay_network_fallback_used=False,
                    query=evidence.account_query,
                )
                account_receipt_submission, _ = self._commit_read_receipt_locked(
                    account_receipt,
                    first_dip_tape=False,
                )
                rows.append(account_receipt_submission)
                rows.append(self._emit_capture_health_locked(at=at, phase="started"))
                if any(not row.accepted for row in rows):
                    self._abort_locked(
                        reason="startup_identity_admission_failed", at=at
                    )
                    raise CaptureContractError(
                        "capture startup identity evidence was not durably admitted"
                    )
                lifecycle_events.extend(
                    row.event for row in rows if row.event is not None
                )
                return tuple(lifecycle_events)
            except BaseException:
                if self._state is CaptureSessionState.RUNNING:
                    self._abort_locked(
                        reason="startup_exception", at=self._observed_now()
                    )
                raise

    def submit_exact_input(
        self,
        *,
        stream: CaptureStream,
        provider: str,
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
        symbol: str | None = None,
        query: Mapping[str, Any] | None = None,
        producer_id: str | None = None,
    ) -> CaptureSubmission:
        with self._lock:
            self._require_running()
            return self._submit_event_locked(
                stream=stream,
                provider=provider,
                payload=payload,
                clocks=clocks,
                symbol=symbol,
                query=query,
                producer_id=producer_id,
            )

    def record_broad_input(
        self,
        *,
        stream: CaptureStream,
        provider: str,
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
        symbol: str,
        query: Mapping[str, Any] | None = None,
    ) -> CaptureSubmission:
        """Ring a cold-symbol event or submit immediately after hot promotion."""

        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        if stream not in _BROAD_STREAMS:
            raise CaptureContractError(
                f"{stream.value} is not an allowed broad-universe ring stream"
            )
        with self._lock:
            self._require_running()
            if self._supervisor_token is not None:
                raise CaptureContractError(
                    "broad input for a supervised coordinator must use its supervisor"
                )
            if clocks.available_at > self._observed_now():
                raise CaptureContractError(
                    "pretrigger input availability is later than trusted wall clock"
                )
            if normalized in self._hot_by_symbol:
                return self._submit_event_locked(
                    stream=stream,
                    provider=provider,
                    payload=payload,
                    clocks=clocks,
                    symbol=normalized,
                    query=query,
                )

            # Ring sequences are provisional and never enter a final seal.  A
            # promoted event is re-enveloped with the next contiguous durable
            # sequence, so expiry of an irrelevant cold symbol cannot punch a
            # false hole in the certifying sequence inventory.
            retained, ring_event = self.pretrigger_ring.retain_observation(
                identity=self.identity,
                stream=stream,
                provider=provider,
                payload=payload,
                clocks=clocks,
                symbol=normalized,
                query=query,
            )
            if retained:
                return CaptureSubmission(
                    accepted=True,
                    event=ring_event,
                    coverage_gap_recorded=False,
                    disposition="bounded_pretrigger_retained_provisional",
                )
            return CaptureSubmission(
                accepted=False,
                event=None,
                coverage_gap_recorded=True,
                disposition="pretrigger_capacity_gap_pending_promotion",
            )

    def promote_hot_symbol(
        self,
        symbol: str,
        *,
        promoted_at: datetime | None = None,
        required_stream: CaptureStream = CaptureStream.L2_DEPTH_DELTA,
    ) -> HotPromotionResult:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        with self._lock:
            self._require_running()
            if self._supervisor_token is not None:
                raise CaptureContractError(
                    "supervised hot promotion must use the shared supervisor"
                )
            if normalized != self.certification_symbol:
                raise CaptureContractError(
                    "a certifying capture run may promote only its bound symbol; "
                    "use a separate coordinator/run for another hot symbol"
                )
            at = self._observed_now()
            if promoted_at is not None and _utc(promoted_at, "promoted_at") != at:
                raise CaptureContractError(
                    "promotion time differs from the trusted capture wall clock"
                )
            existing = self._hot_by_symbol.get(normalized)
            if existing is not None:
                return HotPromotionResult(normalized, existing, (), ())
            admission = self.hot_symbol_leases.acquire(
                normalized,
                requested_at=at,
                required_stream=required_stream,
            )
            if admission.gap is not None:
                self._submit_gap_locked(admission.gap)
                return HotPromotionResult(
                    symbol=normalized,
                    lease=None,
                    promoted_submissions=(),
                    reported_gaps=(admission.gap,),
                )
            assert admission.lease is not None
            transfer = None
            try:
                transfer = self.pretrigger_ring.begin_promotion(
                    normalized,
                    promoted_at=at,
                    source_identity=self.identity,
                )
                batch = PromotionBatch(
                    symbol=transfer.symbol,
                    promoted_at=transfer.promoted_at,
                    events=transfer.events,
                    gaps=transfer.gaps,
                    promotion_id=transfer.promotion_id,
                    source_identity_sha256=transfer.source_identity_sha256,
                    resource_binding_sha256=transfer.resource_binding_sha256,
                    inventory_sha256=transfer.inventory_sha256,
                    admission_handoff_sha256=transfer.admission_handoff_sha256,
                )
                result = self._admit_promotion_batch_locked(
                    normalized,
                    lease=admission.lease,
                    batch=batch,
                    transfer=transfer,
                )
                if result.hot:
                    committed = self.pretrigger_ring.commit_promotion(transfer)
                    if committed != batch:
                        raise CaptureContractError(
                            "committed promotion differs from admitted inventory"
                        )
                else:
                    self.pretrigger_ring.abort_promotion(transfer)
            except BaseException:
                if transfer is not None:
                    self.pretrigger_ring.abort_promotion(transfer)
                self._hot_by_symbol.pop(normalized, None)
                self.hot_symbol_leases.release(admission.lease)
                self._submit_gap_locked(
                    CoverageGap(
                        stream=required_stream,
                        symbol=normalized,
                        reason="promotion_not_atomically_committed",
                        first_available_at=at,
                        last_available_at=at,
                        lost_count=1,
                    )
                )
                raise
            if not result.hot:
                self.hot_symbol_leases.release(admission.lease)
            return result

    def _admit_promotion_batch_locked(
        self,
        symbol: str,
        *,
        lease: HotSymbolLease,
        batch: PromotionBatch,
        transfer: PromotionTransfer,
    ) -> HotPromotionResult:
        """Re-envelope one immutable pretrigger batch into this run."""

        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        expected_batch = PromotionBatch(
            symbol=transfer.symbol,
            promoted_at=transfer.promoted_at,
            events=transfer.events,
            gaps=transfer.gaps,
            promotion_id=transfer.promotion_id,
            source_identity_sha256=transfer.source_identity_sha256,
            resource_binding_sha256=transfer.resource_binding_sha256,
            inventory_sha256=transfer.inventory_sha256,
            admission_handoff_sha256=transfer.admission_handoff_sha256,
        )
        if batch != expected_batch:
            raise CaptureContractError(
                "promotion batch differs from its opaque pre-trigger transfer"
            )
        if batch.symbol != normalized:
            raise CaptureContractError("promotion batch symbol mismatch")
        at = _utc(batch.promoted_at, "promotion batch promoted_at")
        promotion_id = str(batch.promotion_id or "").strip() or sha256_json(
            {
                "identity_sha256": self.identity.identity_sha256,
                "source_identity_sha256": (
                    batch.source_identity_sha256
                    or self.identity.identity_sha256
                ),
                "symbol": normalized,
                "promoted_at": at.isoformat().replace("+00:00", "Z"),
                "provisional_event_sha256s": [
                    event.event_sha256 for event in batch.events
                ],
            }
        )
        source_identity_sha256 = (
            batch.source_identity_sha256
            or (
                batch.events[0].identity.identity_sha256
                if batch.events
                else self.identity.identity_sha256
            )
        )
        inventory_sha256 = batch.inventory_sha256 or sha256_json(
            {
                "promotion_id": promotion_id,
                "source_identity_sha256": source_identity_sha256,
                "symbol": normalized,
                "promoted_at": at.isoformat().replace("+00:00", "Z"),
                "events": [
                    {
                        "sequence": event.sequence,
                        "event_sha256": event.event_sha256,
                    }
                    for event in batch.events
                ],
                "gaps": [gap.to_dict() for gap in batch.gaps],
            }
        )
        submissions: list[CaptureSubmission] = []
        derived_gaps: list[CoverageGap] = []
        handled_events = 0
        for promotion_order, event in enumerate(batch.events, start=1):
            if any(
                key in event.payload
                for key in ("_capture_promotion", "_capture_release")
            ):
                raise CaptureContractError(
                    "provider payload collides with capture runtime provenance"
                )
            if event.symbol != normalized:
                raise CaptureContractError("promotion batch mixes symbols")
            if event.clocks.received_at > at:
                raise CaptureContractError(
                    "pretrigger input was received after its promotion boundary"
                )
            promoted_payload = {
                **dict(event.payload),
                "_capture_promotion": {
                    "promotion_id": promotion_id,
                    "promoted_at": at.isoformat().replace("+00:00", "Z"),
                    "promotion_order": promotion_order,
                    "original_provisional_available_at": (
                        event.clocks.available_at.isoformat().replace(
                            "+00:00", "Z"
                        )
                    ),
                    "provisional_event_sha256": event.event_sha256,
                    "source_identity_sha256": source_identity_sha256,
                    "inventory_sha256": inventory_sha256,
                },
            }
            promotion_kwargs = {
                "promotion_id": promotion_id,
                "promoted_at": at,
                "promotion_source_identity_sha256": source_identity_sha256,
                "promotion_resource_binding_sha256": (
                    batch.resource_binding_sha256
                    or self.resource_binding.binding_sha256
                ),
                "promotion_inventory_sha256": inventory_sha256,
                "promotion_admission_handoff_sha256": (
                    batch.admission_handoff_sha256
                ),
                "promotion_transfer": transfer,
            }
            owner = self._producer_for_stream(event.stream)
            if owner == self._coordinator_producer_id:
                submission = self._submit_event_locked(
                    stream=event.stream,
                    provider=event.provider,
                    payload=promoted_payload,
                    clocks=event.clocks,
                    symbol=event.symbol,
                    query=event.query,
                    **promotion_kwargs,
                )
            else:
                endpoint = self._supervised_external_endpoints.get(owner)
                if endpoint is None:
                    raise CaptureContractError(
                        "promoted external producer endpoint is unavailable"
                    )
                if not endpoint.registered:
                    try:
                        registration = (
                            build_provider_registration_evidence_from_source_event(
                                event,
                                producer_id=owner,
                            )
                        )
                    except CaptureContractError:
                        gap = CoverageGap(
                            stream=event.stream,
                            symbol=event.symbol,
                            reason=(
                                "pretrigger_external_registration_evidence_unavailable"
                            ),
                            first_available_at=event.clocks.available_at,
                            last_available_at=event.clocks.available_at,
                            lost_count=1,
                        )
                        if not self._submit_gap_locked(gap):
                            raise CaptureContractError(
                                "pretrigger registration gap could not be persisted"
                            )
                        derived_gaps.append(gap)
                        handled_events += 1
                        continue
                    submission = endpoint.register_and_submit_first(
                        evidence=registration,
                        stream=event.stream,
                        provider=event.provider,
                        payload=promoted_payload,
                        clocks=event.clocks,
                        symbol=event.symbol,
                        query=event.query,
                        registration_source_payload=event.payload,
                        registration_source_clocks=event.clocks,
                        **promotion_kwargs,
                    )
                else:
                    submission = self._submit_event_locked(
                        stream=event.stream,
                        provider=event.provider,
                        payload=promoted_payload,
                        clocks=event.clocks,
                        symbol=event.symbol,
                        query=event.query,
                        producer_id=owner,
                        producer_token=self._external_producer_tokens[owner],
                        **promotion_kwargs,
                    )
            submissions.append(submission)
            handled_events += 1
            if not submission.accepted:
                break

        # Lifecycle GAP facts are appended only after promoted rows so their
        # trusted control clock cannot make released prehistory look backdated.
        gaps_persisted = all(self._submit_gap_locked(gap) for gap in batch.gaps)
        all_accepted = (
            handled_events == len(batch.events)
            and all(row.accepted for row in submissions)
            and gaps_persisted
        )
        if all_accepted:
            self._hot_by_symbol[normalized] = lease
        return HotPromotionResult(
            symbol=normalized,
            lease=(lease if all_accepted else None),
            promoted_submissions=tuple(submissions),
            reported_gaps=tuple((*batch.gaps, *derived_gaps)),
        )

    def release_hot_symbol(self, symbol: str) -> bool:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        with self._lock:
            self._require_running()
            lease = self._hot_by_symbol.get(normalized)
            if lease is not None and lease.lease_token in self._supervised_hot_tokens:
                raise CaptureContractError(
                    "supervised hot lease must be released by its shared supervisor"
                )
            if lease is not None:
                self._hot_by_symbol.pop(normalized, None)
            return False if lease is None else self.hot_symbol_leases.release(lease)

    def _submit_supervised_hot_input(
        self,
        token: object,
        *,
        stream: CaptureStream,
        provider: str,
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
        symbol: str,
        query: Mapping[str, Any] | None,
    ) -> CaptureSubmission:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        with self._lock:
            self._require_supervisor_token(token)
            self._require_running()
            lease = self._hot_by_symbol.get(normalized)
            if lease is None or lease.lease_token not in self._supervised_hot_tokens:
                raise CaptureContractError("symbol has no supervised hot lease")
            owner = self._producer_for_stream(stream)
            if owner != self._coordinator_producer_id:
                endpoint = self._supervised_external_endpoints.get(owner)
                if endpoint is None:
                    raise CaptureContractError(
                        "supervised external producer endpoint is unavailable"
                    )
                if not endpoint.registered:
                    candidate = CaptureEvent(
                        identity=self.identity,
                        sequence=1,
                        stream=stream,
                        provider=provider,
                        payload=payload,
                        clocks=clocks,
                        symbol=normalized,
                        query=query,
                    )
                    try:
                        evidence = (
                            build_provider_registration_evidence_from_source_event(
                                candidate,
                                producer_id=owner,
                            )
                        )
                    except CaptureContractError:
                        gap = CoverageGap(
                            stream=stream,
                            symbol=normalized,
                            reason=(
                                "external_producer_registration_evidence_unavailable"
                            ),
                            first_available_at=clocks.available_at,
                            last_available_at=clocks.available_at,
                            lost_count=1,
                        )
                        if not self._submit_gap_locked(gap):
                            raise CaptureContractError(
                                "unregistered external input gap could not be persisted"
                            )
                        return CaptureSubmission(
                            accepted=False,
                            event=None,
                            coverage_gap_recorded=True,
                            disposition=(
                                "external_registration_evidence_unavailable_gap_recorded"
                            ),
                        )
                    return endpoint.register_and_submit_first(
                        evidence=evidence,
                        stream=stream,
                        provider=provider,
                        payload=payload,
                        clocks=clocks,
                        symbol=normalized,
                        query=query,
                    )
                return endpoint.submit_exact_input(
                    stream=stream,
                    provider=provider,
                    payload=payload,
                    clocks=clocks,
                    symbol=normalized,
                    query=query,
                )
            return self._submit_event_locked(
                stream=stream,
                provider=provider,
                payload=payload,
                clocks=clocks,
                symbol=normalized,
                query=query,
            )

    def _admit_supervised_promotion(
        self,
        token: object,
        *,
        lease: HotSymbolLease,
        batch: PromotionBatch,
        transfer: PromotionTransfer,
    ) -> HotPromotionResult:
        with self._lock:
            self._require_supervisor_token(token)
            self._require_running()
            if batch.symbol != self.certification_symbol:
                raise CaptureContractError(
                    "supervised promotion differs from certification symbol"
                )
            if (
                batch.resource_binding_sha256
                != self.resource_binding.binding_sha256
                or lease.resource_binding_sha256
                != self.resource_binding.binding_sha256
            ):
                raise CaptureContractError(
                    "supervised promotion resource binding mismatch"
                )
            if batch.promoted_at != self._observed_now():
                raise CaptureContractError(
                    "supervised promotion differs from trusted coordinator clock"
                )
            result = self._admit_promotion_batch_locked(
                batch.symbol,
                lease=lease,
                batch=batch,
                transfer=transfer,
            )
            if result.hot:
                self._supervised_hot_tokens.add(lease.lease_token)
            return result

    def _report_supervised_gap(
        self, token: object, gap: CoverageGap
    ) -> bool:
        with self._lock:
            self._require_supervisor_token(token)
            self._require_running()
            return self._submit_gap_locked(gap)

    def _release_supervised_hot_symbol(
        self, token: object, lease: HotSymbolLease
    ) -> bool:
        with self._lock:
            self._require_supervisor_token(token)
            self._validate_supervised_hot_symbol_locked(lease)
            self._hot_by_symbol.pop(lease.symbol, None)
            self._supervised_hot_tokens.discard(lease.lease_token)
            return True

    def _validate_supervised_hot_symbol_locked(self, lease: HotSymbolLease) -> None:
        current = self._hot_by_symbol.get(lease.symbol)
        if current != lease or lease.lease_token not in self._supervised_hot_tokens:
            raise CaptureContractError(
                "supervised coordinator hot-lease invariant mismatch"
            )

    def _validate_supervised_hot_symbol(
        self, token: object, lease: HotSymbolLease
    ) -> None:
        with self._lock:
            self._require_supervisor_token(token)
            self._validate_supervised_hot_symbol_locked(lease)

    def _commit_read_receipt_locked(
        self,
        receipt: CaptureReadReceipt,
        *,
        first_dip_tape: bool,
    ) -> tuple[CaptureSubmission, FirstDipTapeReceiptEvidence | None]:
        """Commit one typed receipt and mirror only bounded coverage counters."""

        try:
            if first_dip_tape:
                receipt_event, tape_evidence = (
                    self._producer_lifecycle.submit_first_dip_tape_receipt(receipt)
                )
            else:
                receipt_event = self._producer_lifecycle.submit_read_receipt(receipt)
                tape_evidence = None
        except FirstDipTapeCoverageUnavailable as exc:
            if exc.coverage_gap_required:
                self._submit_gap_locked(
                    CoverageGap(
                        stream=CaptureStream.IQFEED_PRINT,
                        symbol=exc.symbol,
                        reason=exc.reason,
                        first_available_at=exc.first_available_at,
                        last_available_at=exc.last_available_at,
                        lost_count=exc.lost_count,
                    )
                )
            return (
                CaptureSubmission(
                    accepted=False,
                    event=None,
                    coverage_gap_recorded=exc.coverage_gap_required,
                    disposition=(
                        "first_dip_tape_coverage_unavailable_gap_recorded"
                        if exc.coverage_gap_required
                        else "first_dip_tape_decision_rejected_without_gap"
                    ),
                    explicit_decision_rejection=(
                        not exc.coverage_gap_required
                    ),
                ),
                None,
            )
        except CaptureContractError:
            if self._producer_lifecycle.health().get("submission_failure") is None:
                raise
            self._rejected_count += 1
            return (
                CaptureSubmission(
                    accepted=False,
                    event=None,
                    coverage_gap_recorded=True,
                    disposition="receipt_ingress_rejected_gap_recorded",
                ),
                None,
            )

        self._observe_durable_event(receipt_event)
        stats = self._stream_stats.get(receipt.stream)
        if stats is None:
            if not receipt.empty_result:
                raise CaptureContractError(
                    "non-empty read receipt has no observed source stream"
                )
            stats = {
                "event_count": 0,
                "first_available_at": receipt.returned_at,
                "last_available_at": receipt.returned_at,
                "providers": {receipt.provider},
                "symbols": {receipt.symbol},
                "exact_event_clock_complete": True,
                "query_receipt_count": 0,
                "gapped": receipt.stream in self._gapped_streams,
            }
            self._stream_stats[receipt.stream] = stats
        stats["query_receipt_count"] += 1
        return (
            CaptureSubmission(
                accepted=True,
                event=receipt_event,
                coverage_gap_recorded=False,
                disposition=(
                    "durable_first_dip_tape_receipt_accepted"
                    if first_dip_tape
                    else "durable_receipt_accepted"
                ),
            ),
            tape_evidence,
        )

    def capture_query_result(
        self,
        *,
        decision_id: str,
        stream: CaptureStream,
        provider: str,
        query: Mapping[str, Any],
        requested_at: datetime,
        returned_at: datetime,
        results: Sequence[ObservedCaptureInput],
        symbol: str | None = None,
        read_id: str | None = None,
    ) -> CapturedReadResult:
        """Persist exact query results and an explicit empty/non-empty receipt."""

        requested = _utc(requested_at, "requested_at")
        returned = _utc(returned_at, "returned_at")
        if returned < requested:
            raise CaptureContractError("query returned before it was requested")
        if STREAM_POLICIES[stream].coverage_mode is not CoverageMode.QUERY_RECEIPT:
            raise CaptureContractError(f"{stream.value} is not query-receipt backed")
        if not isinstance(query, Mapping) or not query:
            raise CaptureContractError("captured query parameters cannot be empty")
        items = tuple(results)
        normalized = _normalized_symbol(symbol)
        self._require_certification_symbol(stream, normalized)
        with self._lock:
            self._require_running()
            if len(items) > self.max_read_sources:
                gap = CoverageGap(
                    stream=stream,
                    symbol=normalized,
                    reason="query_result_exceeds_measured_source_bound",
                    first_available_at=returned,
                    last_available_at=returned,
                    lost_count=len(items),
                )
                self._submit_gap_locked(gap)
                return CapturedReadResult(None, (), None, True)

            source_events: list[CaptureEvent] = []
            for item in items:
                if item.clocks.available_at > returned:
                    raise CaptureContractError(
                        "query result cannot become available after returned_at"
                    )
                submission = self._submit_event_locked(
                    stream=stream,
                    provider=provider,
                    payload=item.payload,
                    clocks=item.clocks,
                    symbol=normalized,
                    query=query,
                )
                if not submission.accepted or submission.event is None:
                    gap = CoverageGap(
                        stream=CaptureStream.READ_RECEIPT,
                        symbol=normalized,
                        reason="query_source_not_durable_receipt_withheld",
                        first_available_at=returned,
                        last_available_at=returned,
                        lost_count=1,
                    )
                    self._submit_gap_locked(gap)
                    return CapturedReadResult(None, tuple(source_events), None, True)
                source_events.append(submission.event)

            source_refs = tuple(CaptureEventRef.from_event(row) for row in source_events)
            committed_at = self._observed_now()
            if returned > committed_at:
                raise CaptureContractError(
                    "provider returned_at is later than the trusted capture wall clock"
                )
            receipt = CaptureReadReceipt(
                read_id=str(read_id or uuid.uuid4()),
                decision_id=str(decision_id or "").strip(),
                identity_sha256=self.identity.identity_sha256,
                stream=stream,
                provider=str(provider or "").strip(),
                symbol=normalized,
                requested_at=requested,
                returned_at=returned,
                query_sha256=sha256_json(query),
                source_event_sha256s=tuple(row.event_sha256 for row in source_refs),
                empty_result=not source_refs,
                result_sha256=captured_read_result_sha256(source_refs),
                content_verified=True,
                replay_network_fallback_used=False,
                query=query,
            )
            receipt_submission, _tape_evidence = self._commit_read_receipt_locked(
                receipt,
                first_dip_tape=False,
            )
            if receipt_submission.accepted:
                self._latest_state_sources[(stream, normalized)] = tuple(
                    source_events
                )
            return CapturedReadResult(
                receipt=(receipt if receipt_submission.accepted else None),
                source_events=tuple(source_events),
                receipt_submission=receipt_submission,
                coverage_gap_recorded=not receipt_submission.accepted,
            )

    def capture_latest_durable_state_read(
        self,
        *,
        decision_id: str,
        stream: CaptureStream,
        returned_at: datetime,
        max_source_age_seconds: float,
        symbol: str | None = None,
        read_id: str | None = None,
    ) -> CapturedReadResult:
        """Receipt the exact latest already-durable state for one decision.

        This is an inventory read, not a fetch seam.  It cannot call a
        provider, database, or application callback and it never manufactures
        a default state.  Missing, future, stale, or clock-incomplete evidence
        raises ``CaptureContractError`` so the caller can grade only the
        current decision ``COVERAGE_UNAVAILABLE`` before opportunity/risk/order.
        """

        if type(stream) is not CaptureStream:
            raise CaptureContractError("current-state stream is malformed")
        policy = STREAM_POLICIES[stream]
        if policy.coverage_mode not in _CURRENT_STATE_INVENTORY_MODES:
            raise CaptureContractError(
                f"{stream.value} is not a current-state inventory stream"
            )
        try:
            max_age = float(max_source_age_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CaptureContractError(
                "current-state freshness bound is malformed"
            ) from exc
        if not math.isfinite(max_age) or max_age <= 0.0:
            raise CaptureContractError(
                "current-state freshness bound is malformed"
            )
        returned = _utc(returned_at, "current-state returned_at")
        normalized = _normalized_symbol(symbol)
        self._require_certification_symbol(stream, normalized)
        with self._lock:
            self._require_running()
            sources = self._latest_state_sources.get((stream, normalized))
            if not sources:
                raise CaptureContractError(
                    f"current_state_{stream.value}_unavailable"
                )
            provider = sources[0].provider
            query = sources[0].query
            if any(
                event.identity != self.identity
                or event.stream is not stream
                or event.symbol != normalized
                or event.provider != provider
                or event.query != query
                or event.sequence > self._prefix_rows
                for event in sources
            ):
                raise CaptureContractError(
                    f"current_state_{stream.value}_identity_mismatch"
                )
            if policy.query_parameters_required and not query:
                raise CaptureContractError(
                    f"current_state_{stream.value}_query_unavailable"
                )
            anchors: list[datetime] = []
            for event in sources:
                clocks = event.clocks
                if clocks.available_at > returned or clocks.received_at > returned:
                    raise CaptureContractError(
                        f"current_state_{stream.value}_future"
                    )
                if (
                    policy.exact_provider_event_clock_required
                    and clocks.provider_event_at is None
                ):
                    raise CaptureContractError(
                        f"current_state_{stream.value}_exact_clock_unavailable"
                    )
                if policy.market_reference_clock_required and (
                    clocks.market_reference_at is None
                    and clocks.provider_event_at is None
                ):
                    raise CaptureContractError(
                        f"current_state_{stream.value}_market_clock_unavailable"
                    )
                anchors.extend((clocks.received_at, clocks.available_at))
                if clocks.provider_event_at is not None:
                    anchors.append(clocks.provider_event_at)
                if clocks.market_reference_at is not None:
                    anchors.append(clocks.market_reference_at)
            if any(age < -1e-6 for age in (
                (returned - anchor).total_seconds() for anchor in anchors
            )):
                raise CaptureContractError(
                    f"current_state_{stream.value}_future"
                )
            if (
                policy.coverage_mode is not CoverageMode.IDENTITY
                and any(
                    age > max_age
                    for age in (
                        (returned - anchor).total_seconds()
                        for anchor in anchors
                    )
                )
            ):
                raise CaptureContractError(
                    f"current_state_{stream.value}_stale"
                )
            exact_query: Mapping[str, Any]
            if query is not None:
                exact_query = query
            else:
                exact_query = {
                    "schema_version": "chili.current-state-inventory-read.v1",
                    "stream": stream.value,
                    "symbol": normalized,
                    "source_event_sha256s": [
                        event.event_sha256 for event in sources
                    ],
                }
            requested = max(event.clocks.available_at for event in sources)
            # RLock deliberately keeps the selected latest generation stable
            # through receipt commit; capture_durable_read re-enters this lock.
            return self.capture_durable_read(
                decision_id=decision_id,
                stream=stream,
                provider=provider,
                query=exact_query,
                requested_at=requested,
                returned_at=returned,
                source_events=sources,
                symbol=normalized,
                read_id=read_id,
            )

    def capture_durable_read(
        self,
        *,
        decision_id: str,
        stream: CaptureStream,
        provider: str,
        query: Mapping[str, Any],
        requested_at: datetime,
        returned_at: datetime,
        source_events: Sequence[CaptureEvent],
        symbol: str | None = None,
        read_id: str | None = None,
        first_dip_tape: bool = False,
    ) -> CapturedReadResult:
        """Receipt an exact read over already-durable live input events.

        Continuous tape is captured once as provider input, then a decision-time
        read names the exact bounded subset it consumed.  This method never
        fetches, replays, or reconstructs missing rows.
        """

        requested = _utc(requested_at, "requested_at")
        returned = _utc(returned_at, "returned_at")
        if returned < requested:
            raise CaptureContractError("read returned before it was requested")
        if STREAM_POLICIES[stream].coverage_mode is CoverageMode.CONTROL:
            raise CaptureContractError("control streams cannot be decision read sources")
        if not isinstance(query, Mapping) or not query:
            raise CaptureContractError("captured read parameters cannot be empty")
        normalized = _normalized_symbol(symbol)
        self._require_certification_symbol(stream, normalized)
        rows = tuple(source_events)
        if len(rows) > self.max_read_sources:
            with self._lock:
                self._require_running()
                gap = CoverageGap(
                    stream=stream,
                    symbol=normalized,
                    reason="durable_read_exceeds_measured_source_bound",
                    first_available_at=returned,
                    last_available_at=returned,
                    lost_count=len(rows),
                )
                self._submit_gap_locked(gap)
            return CapturedReadResult(None, (), None, True)
        if first_dip_tape and stream is not CaptureStream.IQFEED_PRINT:
            raise CaptureContractError(
                "first-dip tape read must reference IQFeed prints"
            )
        with self._lock:
            self._require_running()
            committed_at = self._observed_now()
            if returned > committed_at:
                raise CaptureContractError(
                    "read returned_at is later than the trusted capture wall clock"
                )
            seen_hashes: set[str] = set()
            for event in rows:
                if not isinstance(event, CaptureEvent):
                    raise CaptureContractError("captured read source is malformed")
                if event.event_sha256 in seen_hashes:
                    raise CaptureContractError("captured read repeats a source event")
                seen_hashes.add(event.event_sha256)
                if (
                    event.identity != self.identity
                    or event.stream is not stream
                    or event.provider != str(provider or "").strip()
                    or event.symbol != normalized
                    or event.sequence > self._prefix_rows
                    or event.clocks.available_at > returned
                ):
                    raise CaptureContractError(
                        "captured read source is outside its exact durable boundary"
                    )
            source_refs = tuple(CaptureEventRef.from_event(event) for event in rows)
            receipt = CaptureReadReceipt(
                read_id=str(read_id or uuid.uuid4()),
                decision_id=str(decision_id or "").strip(),
                identity_sha256=self.identity.identity_sha256,
                stream=stream,
                provider=str(provider or "").strip(),
                symbol=normalized,
                requested_at=requested,
                returned_at=returned,
                query_sha256=sha256_json(query),
                source_event_sha256s=tuple(
                    event.event_sha256 for event in source_refs
                ),
                empty_result=not source_refs,
                result_sha256=captured_read_result_sha256(source_refs),
                content_verified=True,
                replay_network_fallback_used=False,
                query=query,
            )
            receipt_submission, tape_evidence = self._commit_read_receipt_locked(
                receipt,
                first_dip_tape=first_dip_tape,
            )
            return CapturedReadResult(
                receipt=(receipt if receipt_submission.accepted else None),
                source_events=rows,
                receipt_submission=receipt_submission,
                coverage_gap_recorded=receipt_submission.coverage_gap_recorded,
                first_dip_tape_evidence=tape_evidence,
            )

    def capture_complete_microstructure_window(
        self,
        *,
        decision_id: str,
        operation: CaptureMicrostructureOperation,
        stream: CaptureStream,
        provider: str,
        symbol: str,
        requested_at: datetime,
        returned_at: datetime,
        event_start_exclusive: datetime,
        event_end_inclusive: datetime,
        parameters: Mapping[str, Any],
        read_id: str | None = None,
    ) -> CapturedReadResult:
        """Receipt the lifecycle-owned complete source window for one read.

        Unlike :meth:`capture_durable_read`, the caller cannot name source
        events.  The producer lifecycle inventories the bounded durable index
        and commits the receipt under one append lock, preventing a racing
        provider append or caller-selected subset.
        """

        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        self._require_certification_symbol(stream, normalized)
        returned = _utc(returned_at, "microstructure returned_at")
        with self._lock:
            self._require_running()
            try:
                receipt_event, receipt, source_events = (
                    self._producer_lifecycle
                    .submit_microstructure_window_receipt(
                        decision_id=decision_id,
                        operation=operation,
                        stream=stream,
                        provider=provider,
                        symbol=normalized,
                        requested_at=requested_at,
                        returned_at=returned,
                        event_start_exclusive=event_start_exclusive,
                        event_end_inclusive=event_end_inclusive,
                        parameters=parameters,
                        read_id=read_id,
                    )
                )
            except MicrostructureCoverageUnavailable as exc:
                self._submit_gap_locked(
                    CoverageGap(
                        stream=exc.stream,
                        symbol=exc.symbol,
                        reason=exc.reason,
                        first_available_at=exc.first_available_at,
                        last_available_at=exc.last_available_at,
                        lost_count=exc.lost_count,
                    )
                )
                return CapturedReadResult(None, (), None, True)
            self._observe_durable_event(receipt_event)
            stats = self._stream_stats.get(receipt.stream)
            if stats is None:
                if not receipt.empty_result:
                    raise CaptureContractError(
                        "microstructure receipt has no observed source stream"
                    )
                stats = {
                    "event_count": 0,
                    "first_available_at": receipt.returned_at,
                    "last_available_at": receipt.returned_at,
                    "providers": {receipt.provider},
                    "symbols": {receipt.symbol},
                    "exact_event_clock_complete": True,
                    "query_receipt_count": 0,
                    "gapped": receipt.stream in self._gapped_streams,
                }
                self._stream_stats[receipt.stream] = stats
            stats["query_receipt_count"] += 1
            submission = CaptureSubmission(
                accepted=True,
                event=receipt_event,
                coverage_gap_recorded=False,
                disposition="durable_microstructure_receipt_accepted",
            )
            return CapturedReadResult(
                receipt=receipt,
                source_events=source_events,
                receipt_submission=submission,
                coverage_gap_recorded=False,
            )

    def record_change(
        self,
        *,
        stream: CaptureStream,
        provider: str,
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
        symbol: str | None = None,
        query: Mapping[str, Any] | None = None,
        broad: bool = False,
    ) -> ChangeCaptureResult:
        """Persist an initial/change value; identical observations are deduped."""

        if STREAM_POLICIES[stream].coverage_mode is not CoverageMode.CHANGE_LOG:
            raise CaptureContractError(f"{stream.value} is not change-log backed")
        normalized = _normalized_symbol(symbol)
        query_key = sha256_json(query or {})
        key = (stream.value, str(provider or "").strip(), normalized or "*", query_key)
        payload_hash = sha256_json(payload)
        with self._lock:
            self._require_running()
            previous = self._change_hashes.get(key)
            if previous == payload_hash:
                self._change_hashes.move_to_end(key)
                current_event = self._change_events.get(key)
                if current_event is None:
                    raise CaptureContractError(
                        "deduplicated change has no durable source event"
                    )
                return ChangeCaptureResult(
                    changed=False,
                    submission=None,
                    current_event=current_event,
                )
            if previous is None and len(self._change_hashes) >= self.max_change_keys:
                gap = CoverageGap(
                    stream=stream,
                    symbol=normalized,
                    reason="change_key_measured_capacity_exhausted",
                    first_available_at=clocks.available_at,
                    last_available_at=clocks.available_at,
                    lost_count=1,
                )
                self._submit_gap_locked(gap)
                return ChangeCaptureResult(
                    changed=True,
                    submission=None,
                    coverage_gap_recorded=True,
                )
            if broad:
                if normalized is None:
                    raise CaptureContractError("broad change capture requires a symbol")
                retain_result = self.pretrigger_ring.retain_change_observation(
                    identity=self.identity,
                    stream=stream,
                    provider=provider,
                    payload=payload,
                    clocks=clocks,
                    symbol=normalized,
                    change_key=sha256_json(
                        {
                            "stream": stream.value,
                            "provider": str(provider or "").strip(),
                            "symbol": normalized,
                            "query_sha256": query_key,
                        }
                    ),
                    query=query,
                )
                translated = _pretrigger_change_capture_result(retain_result)
                submission = translated.submission
                if translated.changed is False:
                    return translated
            else:
                submission = self._submit_event_locked(
                    stream=stream,
                    provider=provider,
                    payload=payload,
                    clocks=clocks,
                    symbol=normalized,
                    query=query,
                )
            if submission.accepted:
                self._change_hashes[key] = payload_hash
                self._change_hashes.move_to_end(key)
                assert submission.event is not None
                self._change_events[key] = submission.event
            return ChangeCaptureResult(
                changed=True,
                submission=submission,
                coverage_gap_recorded=not submission.accepted,
                current_event=submission.event,
            )

    def emit_provider_watermark(
        self,
        *,
        stream: CaptureStream,
        provider: str,
        event_watermark_at: datetime,
        emitted_available_at: datetime,
        bounded_lateness_seconds: float,
        max_observed_lateness_seconds: float,
        generation: int,
        symbol: str | None = None,
    ) -> ProviderWatermark:
        watermark = ProviderWatermark(
            stream=stream,
            provider=provider,
            identity_sha256=self.identity.identity_sha256,
            event_watermark_at=event_watermark_at,
            emitted_available_at=emitted_available_at,
            bounded_lateness_seconds=bounded_lateness_seconds,
            max_observed_lateness_seconds=max_observed_lateness_seconds,
            generation=generation,
            symbol=symbol,
        )
        with self._lock:
            self._require_running()
            self._require_certification_symbol(stream, symbol)
            if STREAM_POLICIES[stream].coverage_mode not in {
                CoverageMode.CONTINUOUS,
                CoverageMode.CHANGE_LOG,
            }:
                raise CaptureContractError(
                    f"{stream.value} does not accept a continuity watermark"
                )
            existing = self._pending_watermarks.get(stream)
            if stream in self._finalized_coverages and existing != watermark:
                raise CaptureContractError("provider watermark changed after finalization")
            if existing is not None and (
                watermark.provider != existing.provider
                or watermark.symbol != existing.symbol
                or watermark.identity_sha256 != existing.identity_sha256
                or watermark.generation != existing.generation
                or watermark.bounded_lateness_seconds
                != existing.bounded_lateness_seconds
                or watermark.event_watermark_at < existing.event_watermark_at
                or watermark.emitted_available_at < existing.emitted_available_at
                or watermark.max_observed_lateness_seconds
                < existing.max_observed_lateness_seconds
            ):
                raise CaptureContractError(
                    "provider watermark identity or frontier moved backwards"
                )
            self._pending_watermarks[stream] = watermark
            return watermark

    def checkpoint_live_continuity(
        self, stream: CaptureStream
    ) -> ActiveCaptureContinuityEvidence:
        """Commit a cumulative watermark/coverage checkpoint before decision compute."""

        with self._lock:
            self._require_running()
            owner = self._producer_for_stream(stream)
            coverage = self.build_stream_coverage(stream)
            evidence = self._producer_lifecycle.submit_live_continuity_checkpoint(
                owner, coverage
            )
            self._observe_durable_pointer(
                sequence=evidence.watermark_event_sequence,
                event_sha256=evidence.watermark_event_sha256,
                available_at=evidence.watermark_committed_available_at,
            )
            self._observe_durable_pointer(
                sequence=evidence.coverage_event_sequence,
                event_sha256=evidence.coverage_event_sha256,
                available_at=evidence.coverage_committed_available_at,
            )
            return evidence

    def attest_predecision_inputs(
        self,
        *,
        decision_id: str,
        dependency_profile: FSMDependencyProfile,
        captured_reads: Sequence[CapturedReadResult],
        first_dip_tape_read_id: str | None = None,
    ) -> ActiveCaptureInputPrefixAttestation:
        """Issue the one-time typed input proof consumed by the decision event."""

        if not isinstance(dependency_profile, FSMDependencyProfile):
            raise CaptureContractError(
                "predecision capture requires a typed FSM dependency profile"
            )
        reads = tuple(captured_reads)
        with self._lock:
            self._require_running()
            by_id: dict[str, CapturedReadResult] = {}
            for captured in reads:
                if not isinstance(captured, CapturedReadResult) or not captured.durable:
                    raise CaptureContractError(
                        "predecision capture requires durable typed reads"
                    )
                assert captured.receipt is not None
                receipt = captured.receipt
                if (
                    receipt.decision_id != str(decision_id or "").strip()
                    or receipt.read_id in by_id
                ):
                    raise CaptureContractError(
                        "predecision read is duplicated or belongs to another decision"
                    )
                by_id[receipt.read_id] = captured
            if tuple(sorted(by_id)) != dependency_profile.required_read_ids:
                raise CaptureContractError(
                    "predecision typed reads differ from dependency profile"
                )
            first_dip_id = str(first_dip_tape_read_id or "").strip() or None
            if first_dip_id is not None:
                first_dip = by_id.get(first_dip_id)
                if first_dip is None or first_dip.first_dip_tape_evidence is None:
                    raise CaptureContractError(
                        "predecision proof lacks typed first-dip tape evidence"
                    )
            proof = self._producer_lifecycle.attest_predecision_input_prefix(
                decision_id=decision_id,
                dependency_profile=dependency_profile,
                first_dip_tape_read_id=first_dip_id,
            )
            return verify_active_capture_input_attestation(proof)

    def prepare_captured_first_dip_tape_authority(
        self,
        *,
        attestation: ActiveCaptureInputPrefixAttestation,
        policy: FirstDipTapePolicy,
        purpose: str,
        final_boundary_available_at: datetime | None = None,
    ) -> object:
        """Prepare one evidence-only authority owned by this live coordinator.

        Callers cannot supply an evaluation or provenance hashes.  The private
        lifecycle recomputes both from the exact durable IQFeed read and
        continuity checkpoint named by ``attestation``.
        """

        if not isinstance(policy, FirstDipTapePolicy):
            raise CaptureContractError(
                "captured first-dip authority requires a typed policy"
            )
        proof = verify_active_capture_input_attestation(attestation)
        with self._lock:
            self._require_running()
            if (
                proof.run_id != self.identity.run_id
                or proof.generation != self.identity.generation
                or proof.identity_sha256 != self.identity.identity_sha256
                or proof.account_identity_sha256
                != self.identity.account_identity_sha256
                or proof.code_build_sha256 != self.identity.code_build_sha256
                or proof.config_sha256 != self.identity.config_sha256
                or proof.feature_flags_sha256
                != self.identity.feature_flags_sha256
                or proof.resource_binding_sha256
                != self.resource_binding.binding_sha256
            ):
                raise CaptureContractError(
                    "captured first-dip authority belongs to another coordinator"
                )
            authority = (
                self._producer_lifecycle.prepare_captured_first_dip_tape_authority(
                    attestation=proof,
                    policy=policy,
                    purpose=purpose,
                )
            )
            normalized_purpose = str(purpose or "").strip().lower()
            if normalized_purpose == "pre_reservation":
                from .first_dip_tape_decision import (  # noqa: PLC0415
                    FirstDipTapeDecisionReceipt,
                )

                if final_boundary_available_at is None:
                    raise CaptureContractError(
                        "captured first-dip final authority lacks its decision clock"
                    )
                final_boundary = _utc(
                    final_boundary_available_at,
                    "first-dip final boundary_available_at",
                )

                receipt = getattr(authority, "receipt", None)
                if type(receipt) is not FirstDipTapeDecisionReceipt:
                    raise CaptureContractError(
                        "captured first-dip final authority lacks a typed receipt"
                    )
                adaptive_request_json = (
                    self._first_dip_final_request_json_by_decision.get(
                        proof.decision_id
                    )
                )
                prior_reference = receipt.prior_detector_reference
                if adaptive_request_json is None or prior_reference is None:
                    raise CaptureContractError(
                        "captured first-dip final frontier lacks request lineage"
                    )
                frontier = FirstDipFinalCaptureFrontier.from_active_attestation(
                    proof,
                    policy=policy,
                    evaluation=receipt.evaluation,
                    decision_receipt_binding_sha256=receipt.binding_sha256,
                    adaptive_request_payload=json.loads(adaptive_request_json),
                    prior_detector_reference_payload=(
                        prior_reference.to_dict()
                    ),
                    final_boundary_available_at=final_boundary,
                )
                existing = self._first_dip_final_frontier_by_decision.get(
                    proof.decision_id
                )
                if existing is not None and existing != frontier:
                    raise CaptureContractError(
                        "runtime first-dip final frontier changed after issuance"
                    )
                self._first_dip_final_frontier_by_decision[
                    proof.decision_id
                ] = frontier
                self._first_dip_final_request_json_by_decision.pop(
                    proof.decision_id,
                    None,
                )
            elif final_boundary_available_at is not None:
                raise CaptureContractError(
                    "captured first-dip detector authority received a final clock"
                )
            return authority

    def retain_accepted_first_dip_detector(
        self,
        *,
        resolution: object,
        opportunity_key_sha256: str,
    ) -> str:
        """Retain one positively consumed detector receipt inside this run."""

        with self._lock:
            self._require_running()
            return self._producer_lifecycle.retain_accepted_first_dip_detector(
                resolution=resolution,
                opportunity_key_sha256=opportunity_key_sha256,
            )

    def attest_first_dip_pre_reservation_inputs(
        self,
        *,
        adaptive_request: object,
        dependency_profile: FSMDependencyProfile,
        captured_reads: Sequence[CapturedReadResult],
        first_dip_tape_read_id: str,
    ) -> ActiveCaptureInputPrefixAttestation:
        """Bind a fresh durable tape read to one exact adaptive request.

        This is the final evidence checkpoint before reservation.  It grants
        neither reservation nor order authority and performs no provider/DB
        lookup.  Missing, stale, foreign, or mismatched evidence fails only the
        current decision closed.
        """

        from .adaptive_risk_reservation import (  # noqa: PLC0415
            AdaptiveRiskReservationRequest,
            load_adaptive_risk_reservation_request,
        )

        if type(adaptive_request) is not AdaptiveRiskReservationRequest:
            raise CaptureContractError(
                "first-dip final capture requires a typed adaptive request"
            )
        try:
            request = load_adaptive_risk_reservation_request(
                adaptive_request.to_payload()
            )
        except Exception as exc:
            raise CaptureContractError(
                "first-dip final adaptive request is invalid"
            ) from exc
        if (
            request.setup_family != "first_dip_reclaim"
            or request.opportunity_key is None
        ):
            raise CaptureContractError(
                "first-dip final capture lacks its typed opportunity"
            )
        if not isinstance(dependency_profile, FSMDependencyProfile):
            raise CaptureContractError(
                "first-dip final capture requires a typed dependency profile"
            )
        reads = tuple(captured_reads)
        first_dip_id = str(first_dip_tape_read_id or "").strip()
        if not first_dip_id:
            raise CaptureContractError(
                "first-dip final capture read id is missing"
            )

        with self._lock:
            self._require_running()
            by_id: dict[str, CapturedReadResult] = {}
            for captured in reads:
                if not isinstance(captured, CapturedReadResult) or not captured.durable:
                    raise CaptureContractError(
                        "first-dip final capture requires durable typed reads"
                    )
                receipt = captured.receipt
                submission = captured.receipt_submission
                assert receipt is not None and submission is not None
                assert submission.event is not None
                if (
                    receipt.identity_sha256 != self.identity.identity_sha256
                    or receipt.read_id in by_id
                    or submission.event.identity != self.identity
                    or submission.event.payload != receipt.to_dict()
                    or tuple(event.event_sha256 for event in captured.source_events)
                    != receipt.source_event_sha256s
                ):
                    raise CaptureContractError(
                        "first-dip final read is duplicated or foreign"
                    )
                by_id[receipt.read_id] = captured
            if tuple(sorted(by_id)) != dependency_profile.required_read_ids:
                raise CaptureContractError(
                    "first-dip final reads differ from dependency profile"
                )
            first_dip = by_id.get(first_dip_id)
            if first_dip is None or first_dip.first_dip_tape_evidence is None:
                raise CaptureContractError(
                    "first-dip final proof lacks typed tape evidence"
                )
            assert first_dip.receipt is not None
            if (
                first_dip.receipt.symbol != request.inputs.symbol
                or first_dip.receipt.returned_at < request.inputs.as_of
                or request.inputs.replay_or_paper_run_id != self.identity.run_id
                or request.inputs.generation != self.identity.generation
                or request.inputs.account_identity_sha256
                != self.identity.account_identity_sha256
                or request.inputs.code_build_sha256
                != self.identity.code_build_sha256
                or request.inputs.effective_config_sha256
                != self.identity.config_sha256
                or request.inputs.feature_flags_sha256
                != self.identity.feature_flags_sha256
            ):
                raise CaptureContractError(
                    "first-dip final read/request escaped capture identity or time"
                )

            proof = (
                self._producer_lifecycle
                .attest_first_dip_pre_reservation_input_prefix(
                    adaptive_request=request,
                    dependency_profile=dependency_profile,
                    first_dip_tape_read_id=first_dip_id,
                )
            )
            verified = verify_active_capture_input_attestation(proof)
            if (
                verified.run_id != self.identity.run_id
                or verified.generation != self.identity.generation
                or verified.identity_sha256 != self.identity.identity_sha256
                or verified.decision_id != first_dip.receipt.decision_id
                or verified.required_read_ids != dependency_profile.required_read_ids
                or verified.first_dip_tape_read_id != first_dip_id
                or verified.first_dip_adaptive_request_sha256
                != request.request_sha256
                or verified.first_dip_opportunity_key_sha256
                != request.opportunity_key.key_sha256
                or verified.first_dip_prior_detector_reference_sha256 is None
            ):
                raise CaptureContractError(
                    "runtime first-dip final proof differs from coordinator inputs"
                )
            request_json = canonical_json_bytes(request.to_payload()).decode(
                "utf-8"
            )
            existing_request = self._first_dip_final_request_json_by_decision.get(
                verified.decision_id
            )
            if existing_request is not None and existing_request != request_json:
                raise CaptureContractError(
                    "runtime first-dip final request changed after attestation"
                )
            self._first_dip_final_request_json_by_decision[
                verified.decision_id
            ] = request_json
            return verified

    def first_dip_final_capture_frontier(
        self,
        attestation: ActiveCaptureInputPrefixAttestation,
    ) -> FirstDipFinalCaptureFrontier:
        """Return only the exact frontier issued by this running coordinator."""

        proof = verify_active_capture_input_attestation(attestation)
        with self._lock:
            self._require_running()
            owned = self._first_dip_final_frontier_by_decision.get(
                proof.decision_id
            )
            if owned is None or (
                owned.run_id != proof.run_id
                or owned.generation != proof.generation
                or owned.identity_sha256 != proof.identity_sha256
                or owned.decision_id != proof.decision_id
                or owned.input_prefix_sequence != proof.input_prefix_sequence
                or owned.input_prefix_root_sha256
                != proof.input_prefix_root_sha256
                or owned.attested_available_at != proof.attested_available_at
                or owned.expires_at != proof.expires_at
                or owned.dependency_profile_sha256
                != proof.dependency_profile.profile_sha256
                or owned.dependency_profile_canonical_json
                != canonical_json_bytes(
                    proof.dependency_profile.to_dict()
                ).decode("utf-8")
                or owned.required_read_ids != proof.required_read_ids
                or owned.read_evidence_inventory_sha256
                != proof.read_evidence_inventory_sha256
                or owned.continuity_evidence_inventory_sha256
                != proof.continuity_evidence_inventory_sha256
                or owned.first_dip_tape_read_id
                != proof.first_dip_tape_read_id
                or owned.prior_detector_reference_sha256
                != proof.first_dip_prior_detector_reference_sha256
                or owned.adaptive_request_sha256
                != proof.first_dip_adaptive_request_sha256
                or owned.opportunity_key_sha256
                != proof.first_dip_opportunity_key_sha256
            ):
                raise CaptureContractError(
                    "first-dip final capture frontier is not runtime-owned"
                )
            return owned

    def checkpoint_decision(
        self,
        *,
        decision_id: str,
        symbol: str,
        decision_at: datetime,
        received_at: datetime,
        available_at: datetime,
        required_read_ids: Iterable[str],
        decision_output: CaptureDecisionOutput,
        decision_details: Mapping[str, Any],
        predecision_attestation: ActiveCaptureInputPrefixAttestation,
        first_dip_final_capture_frontier: (
            FirstDipFinalCaptureFrontier | None
        ) = None,
    ) -> UnverifiedDecisionPrefix:
        """Persist exact FSM inputs and the canonical action/no-action output."""

        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        decision = _utc(decision_at, "decision_at")
        received = _utc(received_at, "received_at")
        available = _utc(available_at, "available_at")
        if not isinstance(decision_output, CaptureDecisionOutput):
            raise CaptureContractError(
                "checkpoint requires a canonical FSM decision output"
            )
        normalized_decision_id = str(decision_id or "").strip()
        if (
            decision_output.decision_id != normalized_decision_id
            or decision_output.symbol != normalized
        ):
            raise CaptureContractError(
                "FSM decision output differs from checkpoint envelope"
            )
        read_ids = tuple(sorted(str(value) for value in required_read_ids))
        if len(read_ids) != len(set(read_ids)):
            raise CaptureContractError("decision contains duplicate read ids")
        reserved = {
            "decision_id",
            "symbol",
            "decision_at",
            "input_prefix_sequence",
            "input_prefix_root_sha256",
            "required_read_ids",
            "decision_output",
            "decision_output_sha256",
            "fsm_dependency_profile",
            "predecision_attestation_sha256",
            "predecision_read_evidence_inventory_sha256",
            "predecision_continuity_evidence_inventory_sha256",
            "predecision_admission_handoff_sha256",
            "first_dip_tape_read_id",
            "first_dip_final_capture_frontier",
            "first_dip_final_capture_frontier_sha256",
        }
        if any(key in decision_details for key in reserved):
            raise CaptureContractError("decision details contain coordinator-owned fields")
        proof = verify_active_capture_input_attestation(predecision_attestation)
        if (
            proof.decision_id != normalized_decision_id
            or proof.required_read_ids != read_ids
            or proof.identity_sha256 != self.identity.identity_sha256
        ):
            raise CaptureContractError(
                "decision predecision proof is unrelated to this run/read set"
            )
        with self._lock:
            self._require_running()
            final_frontier = first_dip_final_capture_frontier
            if final_frontier is not None:
                if not isinstance(
                    final_frontier, FirstDipFinalCaptureFrontier
                ):
                    raise CaptureContractError(
                        "decision first-dip final frontier is malformed"
                    )
                owned_frontier = self._first_dip_final_frontier_by_decision.get(
                    final_frontier.decision_id
                )
                if (
                    owned_frontier is not final_frontier
                    or final_frontier.decision_id
                    in self._checkpointed_first_dip_final_frontiers
                    or final_frontier.run_id != self.identity.run_id
                    or final_frontier.generation != self.identity.generation
                    or final_frontier.identity_sha256
                    != self.identity.identity_sha256
                    or final_frontier.input_prefix_sequence
                    <= proof.input_prefix_sequence
                    or proof.first_dip_tape_read_id is None
                    or decision_output.setup_role != "first_dip_reclaim"
                ):
                    raise CaptureContractError(
                        "decision first-dip final frontier is foreign or mismatched"
                    )
            elif (
                decision_output.setup_role == "first_dip_reclaim"
                and decision_output.action.value == "order_intent"
            ):
                raise CaptureContractError(
                    "first-dip order decision lacks its final capture frontier"
                )
            prefix_sequence = proof.input_prefix_sequence
            prefix_root = proof.input_prefix_root_sha256
            supplied_profile = decision_details.get("fsm_dependency_profile")
            if supplied_profile is not None and supplied_profile != proof.dependency_profile.to_dict():
                raise CaptureContractError(
                    "decision dependency profile differs from predecision proof"
                )
            payload = {
                **dict(decision_details),
                "fsm_dependency_profile": proof.dependency_profile.to_dict(),
                "predecision_attestation_sha256": proof.attestation_sha256,
                "predecision_read_evidence_inventory_sha256": (
                    proof.read_evidence_inventory_sha256
                ),
                "predecision_continuity_evidence_inventory_sha256": (
                    proof.continuity_evidence_inventory_sha256
                ),
                "predecision_admission_handoff_sha256": (
                    proof.admission_handoff_sha256
                ),
                "first_dip_tape_read_id": proof.first_dip_tape_read_id,
                "first_dip_final_capture_frontier": (
                    None
                    if final_frontier is None
                    else final_frontier.to_dict()
                ),
                "first_dip_final_capture_frontier_sha256": (
                    None
                    if final_frontier is None
                    else final_frontier.frontier_sha256
                ),
                "decision_id": normalized_decision_id,
                "symbol": normalized,
                "decision_at": decision.isoformat().replace("+00:00", "Z"),
                "input_prefix_sequence": prefix_sequence,
                "input_prefix_root_sha256": prefix_root,
                "required_read_ids": list(read_ids),
                "decision_output": decision_output.to_dict(),
                "decision_output_sha256": (
                    decision_output.decision_output_sha256
                ),
            }
            submission = self._submit_event_locked(
                stream=CaptureStream.FSM_DECISION,
                provider=_CONTROL_PROVIDER,
                symbol=normalized,
                payload=payload,
                clocks=CaptureClocks(
                    received_at=received,
                    available_at=available,
                    market_reference_at=decision,
                ),
                predecision_attestation=proof,
            )
            if not submission.accepted or submission.event is None:
                raise CaptureContractError(
                    "FSM decision was not durably captured; coverage is unavailable"
                )
            if final_frontier is not None:
                self._checkpointed_first_dip_final_frontiers.add(
                    final_frontier.decision_id
                )
            checkpoint = CaptureDecisionCheckpoint(
                identity_sha256=self.identity.identity_sha256,
                decision_id=normalized_decision_id,
                symbol=normalized,
                decision_at=decision,
                available_at=submission.event.clocks.available_at,
                decision_event_sha256=submission.event.event_sha256,
                input_prefix_sequence=prefix_sequence,
                input_prefix_root_sha256=prefix_root,
                required_read_ids=read_ids,
                decision_payload=payload,
            )
            return UnverifiedDecisionPrefix(
                checkpoint=checkpoint,
                decision_event=submission.event,
                capture_root=self.capture_root,
            )

    def active_decision_capture_proof(
        self,
        *,
        decision_prefix: UnverifiedDecisionPrefix,
        captured_reads: Sequence[CapturedReadResult],
        first_dip_tape_read_id: str | None = None,
    ) -> ActiveCapturePrefixAttestation:
        """Issue the runtime's opaque live-only proof for one exact snapshot.

        The coordinator lock is held only through local cross-checks and the
        lifecycle attestation call.  The returned immutable snapshot remains
        valid when unrelated later capture events arrive; callers must still
        enforce age and current economic fingerprints before broker I/O.
        """

        if not isinstance(decision_prefix, UnverifiedDecisionPrefix):
            raise CaptureContractError("active decision prefix is malformed")
        reads = tuple(captured_reads)
        with self._lock:
            self._require_running()
            checkpoint = decision_prefix.checkpoint
            decision_event = decision_prefix.decision_event
            if (
                decision_prefix.capture_root != self.capture_root
                or checkpoint.identity_sha256 != self.identity.identity_sha256
                or decision_event.identity != self.identity
                or decision_event.event_sha256
                != checkpoint.decision_event_sha256
                or decision_event.sequence <= checkpoint.input_prefix_sequence
                or checkpoint.input_prefix_sequence > self._prefix_rows
            ):
                raise CaptureContractError(
                    "active decision prefix belongs to another capture frontier"
                )

            by_id: dict[str, CapturedReadResult] = {}
            for captured in reads:
                if not isinstance(captured, CapturedReadResult) or not captured.durable:
                    raise CaptureContractError(
                        "active decision proof requires durable typed reads"
                    )
                receipt = captured.receipt
                submission = captured.receipt_submission
                assert receipt is not None and submission is not None
                assert submission.event is not None
                if (
                    receipt.decision_id != checkpoint.decision_id
                    or receipt.identity_sha256 != self.identity.identity_sha256
                    or submission.event.sequence > checkpoint.input_prefix_sequence
                    or submission.event.payload != receipt.to_dict()
                    or tuple(
                        event.event_sha256 for event in captured.source_events
                    )
                    != receipt.source_event_sha256s
                ):
                    raise CaptureContractError(
                        "active decision read differs from its durable receipt"
                    )
                if receipt.read_id in by_id:
                    raise CaptureContractError(
                        "active decision proof repeats a read receipt"
                    )
                by_id[receipt.read_id] = captured
            required_ids = tuple(sorted(by_id))
            if required_ids != checkpoint.required_read_ids:
                raise CaptureContractError(
                    "active decision proof read set differs from checkpoint"
                )
            first_dip_id = str(first_dip_tape_read_id or "").strip() or None
            if first_dip_id is not None:
                first_dip = by_id.get(first_dip_id)
                if (
                    first_dip is None
                    or first_dip.first_dip_tape_evidence is None
                ):
                    raise CaptureContractError(
                        "active decision proof lacks typed first-dip tape evidence"
                    )

            attestation = self._producer_lifecycle.attest_active_prefix(
                decision_id=checkpoint.decision_id,
                required_read_ids=required_ids,
                first_dip_tape_read_id=first_dip_id,
            )
            verified = verify_active_capture_prefix_attestation(attestation)
            if (
                verified.run_id != self.identity.run_id
                or verified.generation != self.identity.generation
                or verified.decision_id != checkpoint.decision_id
                or verified.decision_event_sha256
                != checkpoint.decision_event_sha256
                or verified.input_prefix_sequence
                != checkpoint.input_prefix_sequence
                or verified.input_prefix_root_sha256
                != checkpoint.input_prefix_root_sha256
                or verified.attestation_frontier_sequence != self._prefix_rows
                or verified.attestation_frontier_root_sha256
                != self._current_prefix_root()
                or verified.identity_sha256 != self.identity.identity_sha256
                or verified.account_identity_sha256
                != self.identity.account_identity_sha256
                or verified.code_build_sha256 != self.identity.code_build_sha256
                or verified.config_sha256 != self.identity.config_sha256
                or verified.feature_flags_sha256
                != self.identity.feature_flags_sha256
                or verified.resource_binding_sha256
                != self.resource_binding.binding_sha256
                or verified.required_read_ids != required_ids
                or verified.first_dip_tape_read_id != first_dip_id
            ):
                raise CaptureContractError(
                    "runtime active attestation differs from coordinator snapshot"
                )
            evidence_by_id = {
                row.receipt.read_id: row for row in verified.read_evidence
            }
            for read_id, captured in by_id.items():
                row = evidence_by_id.get(read_id)
                assert captured.receipt is not None
                assert captured.receipt_submission is not None
                assert captured.receipt_submission.event is not None
                if (
                    row is None
                    or row.receipt != captured.receipt
                    or row.receipt_event_sha256
                    != captured.receipt_submission.event.event_sha256
                    or row.receipt_event_sequence
                    != captured.receipt_submission.event.sequence
                    or row.source_event_refs
                    != tuple(
                        CaptureEventRef.from_event(event)
                        for event in captured.source_events
                    )
                ):
                    raise CaptureContractError(
                        "runtime active attestation read evidence mismatch"
                    )
            return verified

    def record_broker_order_lifecycle(
        self,
        *,
        lifecycle: CaptureBrokerOrderLifecycle,
        provider: str,
        symbol: str,
        clocks: CaptureClocks,
    ) -> CaptureSubmission:
        """Commit one typed, cumulative broker transition for a captured intent."""

        if not isinstance(lifecycle, CaptureBrokerOrderLifecycle):
            raise CaptureContractError("broker lifecycle transition is malformed")
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        with self._lock:
            self._require_running()
            self._require_certification_symbol(
                CaptureStream.BROKER_ORDER_LIFECYCLE,
                normalized,
            )
            owner = self._producer_for_stream(
                CaptureStream.BROKER_ORDER_LIFECYCLE
            )
            try:
                event = self._producer_lifecycle.submit_broker_order_lifecycle(
                    owner,
                    lifecycle,
                    provider=provider,
                    symbol=normalized,
                    clocks=clocks,
                )
            except CaptureContractError:
                if self._producer_lifecycle.health().get("submission_failure") is None:
                    raise
                self._rejected_count += 1
                return CaptureSubmission(
                    accepted=False,
                    event=None,
                    coverage_gap_recorded=True,
                    disposition="broker_lifecycle_ingress_rejected_gap_recorded",
                )
            self._observe_durable_event(event)
            return CaptureSubmission(
                accepted=True,
                event=event,
                coverage_gap_recorded=False,
                disposition="durable_broker_lifecycle_accepted",
            )

    def _health_payload_locked(self, *, phase: str) -> dict[str, Any]:
        return {
            "phase": str(phase),
            "state": self._state.value,
            "identity_sha256": self.identity.identity_sha256,
            "resource_binding": self.resource_binding.to_record(),
            "resource_hashes": self.resource_binding.hashes,
            "network_fallback_allowed": False,
            "durable_sequence_next": int(
                self._producer_lifecycle.health().get("last_sequence") or 0
            )
            + 1,
            "accepted_count": self._accepted_count,
            "rejected_or_reported_lost_count": self._rejected_count,
            "change_key_count": len(self._change_hashes),
            "max_change_keys": self.max_change_keys,
            "max_read_sources": self.max_read_sources,
            "hot_symbols": tuple(sorted(self._hot_by_symbol)),
            "ingress": self.ingress.health(),
            "pretrigger": self.pretrigger_ring.health(),
            "hot_leases": (
                dict(self._supervisor_hot_health)
                if self._supervisor_hot_health is not None
                else self.hot_symbol_leases.health()
            ),
            "pressure": self.pressure_controller.health(),
            "writer": self.writer.health(),
        }

    def _emit_capture_health_locked(
        self, *, at: datetime, phase: str
    ) -> CaptureSubmission:
        _utc(at, "capture health observed_at")
        try:
            event = self._producer_lifecycle.submit_capture_health(
                self._health_payload_locked(phase=phase)
            )
        except CaptureContractError:
            if self._producer_lifecycle.health().get("submission_failure") is None:
                raise
            self._rejected_count += 1
            return CaptureSubmission(
                accepted=False,
                event=None,
                coverage_gap_recorded=True,
                disposition="health_ingress_rejected_gap_recorded",
            )
        self._observe_durable_event(event)
        return CaptureSubmission(
            accepted=True,
            event=event,
            coverage_gap_recorded=False,
            disposition="durable_capture_health_accepted",
        )

    def emit_capture_health(
        self, *, observed_at: datetime | None = None, phase: str = "periodic"
    ) -> CaptureSubmission:
        with self._lock:
            self._require_running()
            at = self._observed_now()
            if observed_at is not None and _utc(observed_at, "observed_at") != at:
                raise CaptureContractError(
                    "capture health time differs from trusted capture wall clock"
                )
            return self._emit_capture_health_locked(at=at, phase=phase)

    def build_stream_coverage(self, stream: CaptureStream) -> StreamCoverage:
        """Resolve bounded live counters; the sealed loader re-verifies them."""

        with self._lock:
            stats = self._stream_stats.get(stream)
            if stats is None:
                raise CaptureContractError(
                    f"stream {stream.value} has no observed input or empty receipt"
                )
            providers = set(stats["providers"])
            symbols = set(stats["symbols"])
            query_scoped = (
                STREAM_POLICIES[stream].coverage_mode
                is CoverageMode.QUERY_RECEIPT
            )
            if not providers or not symbols:
                raise CaptureContractError(
                    f"stream {stream.value} has empty provider/symbol scope"
                )
            if query_scoped:
                provider = (
                    next(iter(providers))
                    if len(providers) == 1
                    else "mixed_query_receipt"
                )
                symbol = next(iter(symbols)) if len(symbols) == 1 else None
            else:
                if len(providers) != 1:
                    raise CaptureContractError(
                        f"stream {stream.value} has ambiguous provider scope"
                    )
                if len(symbols) != 1:
                    raise CaptureContractError(
                        f"stream {stream.value} has ambiguous symbol scope"
                    )
                provider = next(iter(providers))
                symbol = next(iter(symbols))
            watermark = self._pending_watermarks.get(stream)
            if watermark is not None and (
                watermark.provider != provider or watermark.symbol != symbol
            ):
                raise CaptureContractError(
                    f"stream {stream.value} watermark scope does not match inputs"
                )
            lifecycle_health = self._producer_lifecycle.health()
            content_verified = bool(
                lifecycle_health.get("submission_failure") is None
                and not stats["gapped"]
            )
            return StreamCoverage(
                stream=stream,
                identity_sha256=self.identity.identity_sha256,
                provider=provider,
                symbol=symbol,
                first_available_at=stats["first_available_at"],
                last_available_at=stats["last_available_at"],
                event_count=int(stats["event_count"]),
                exact_event_clock_complete=bool(
                    stats["exact_event_clock_complete"]
                ),
                content_verified=content_verified,
                continuity_complete=bool(not stats["gapped"]),
                watermark=watermark,
                query_receipt_count=int(stats["query_receipt_count"]),
            )

    def finalize_stream_coverage(self, stream: CaptureStream) -> StreamCoverage:
        with self._lock:
            self._require_running()
            if stream in self._finalized_coverages:
                raise CaptureContractError(
                    f"stream {stream.value} coverage was already finalized"
                )
            coverage = self.build_stream_coverage(stream)
            owner = self._producer_for_stream(stream)
            events = self._producer_lifecycle.submit_stream_coverage(owner, coverage)
            for event in events:
                self._observe_durable_event(event)
            self._finalized_coverages.add(stream)
            return coverage

    def heartbeat(self, producer_id: str) -> CaptureEvent:
        with self._lock:
            self._require_running()
            if producer_id != self._coordinator_producer_id:
                raise CaptureContractError(
                    "external producer heartbeat requires its bound endpoint"
                )
            event = self._producer_lifecycle.heartbeat(producer_id)
            self._observe_durable_event(event)
            return event

    def bind_external_producer(self, producer_id: str) -> "BoundLiveCaptureProducer":
        """Issue one opaque, one-process ingress capability for a rostered producer."""

        normalized = str(producer_id or "").strip().lower()
        with self._lock:
            if normalized == self._coordinator_producer_id:
                raise CaptureContractError(
                    "the coordinator producer does not use an external endpoint"
                )
            spec = self._producer_lifecycle.producers.get(normalized)
            if spec is None:
                raise CaptureContractError(
                    f"producer is not in RUN_OPEN roster: {normalized}"
                )
            if normalized in self._external_producer_tokens:
                raise CaptureContractError(
                    f"external producer endpoint already exists: {normalized}"
                )
            if self._state not in {CaptureSessionState.CREATED, CaptureSessionState.RUNNING}:
                raise CaptureContractError("closed capture cannot bind a producer")
            token = object()
            self._external_producer_tokens[normalized] = token
            return BoundLiveCaptureProducer(
                coordinator=self,
                producer_id=normalized,
                token=token,
                spec=spec,
            )

    def _require_external_token(self, producer_id: str, token: object) -> None:
        if (
            producer_id == self._coordinator_producer_id
            or self._external_producer_tokens.get(producer_id) is not token
        ):
            raise CaptureContractError("external producer capability is invalid")

    def _register_and_submit_external_first(
        self,
        *,
        producer_id: str,
        token: object,
        evidence: CaptureProviderRegistrationEvidence,
        stream: CaptureStream,
        provider: str,
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
        symbol: str | None,
        query: Mapping[str, Any] | None,
        registration_source_payload: Mapping[str, Any] | None = None,
        registration_source_clocks: CaptureClocks | None = None,
        promotion_id: str | None = None,
        promoted_at: datetime | None = None,
        promotion_source_identity_sha256: str | None = None,
        promotion_resource_binding_sha256: str | None = None,
        promotion_inventory_sha256: str | None = None,
        promotion_admission_handoff_sha256: str | None = None,
        promotion_transfer: PromotionTransfer | None = None,
    ) -> CaptureSubmission:
        """Durably bind one registration record, fact, and proving frame."""

        with self._lock:
            self._require_running()
            self._require_external_token(producer_id, token)
            spec = self._producer_lifecycle.producers[producer_id]
            if evidence.producer_id != producer_id:
                raise CaptureContractError("provider registration producer mismatch")
            if evidence.provider_instance_id != spec.instance_id:
                raise CaptureContractError(
                    "provider registration instance differs from RUN_OPEN"
                )
            if evidence.provider_generation != spec.generation:
                raise CaptureContractError(
                    "provider registration generation differs from RUN_OPEN"
                )
            if evidence.provider != str(provider or "").strip().lower():
                raise CaptureContractError("provider registration source mismatch")
            source_payload = (
                payload
                if registration_source_payload is None
                else registration_source_payload
            )
            source_clocks = (
                clocks
                if registration_source_clocks is None
                else registration_source_clocks
            )
            if not isinstance(source_payload, Mapping) or not isinstance(
                source_clocks, CaptureClocks
            ):
                raise CaptureContractError(
                    "provider registration source envelope is malformed"
                )
            if evidence.source_payload_sha256 != sha256_json(source_payload):
                raise CaptureContractError("provider registration payload hash mismatch")
            if (
                source_clocks.provider_event_at != evidence.provider_event_at
                or source_clocks.received_at != evidence.received_at
            ):
                raise CaptureContractError("provider registration clocks mismatch")
            if promotion_transfer is None:
                if (
                    registration_source_payload is not None
                    and dict(source_payload) != dict(payload)
                ) or (
                    registration_source_clocks is not None and source_clocks != clocks
                ):
                    raise CaptureContractError(
                        "unpromoted provider registration source differs from input"
                    )
            else:
                promotion_metadata = payload.get("_capture_promotion")
                provider_payload = {
                    key: value
                    for key, value in payload.items()
                    if key != "_capture_promotion"
                }
                if (
                    not isinstance(promotion_metadata, Mapping)
                    or dict(provider_payload) != dict(source_payload)
                    or source_clocks != clocks
                ):
                    raise CaptureContractError(
                        "promoted provider registration source differs from transfer input"
                    )
            if producer_id in self._external_registration_evidence:
                raise CaptureContractError(
                    "external producer registration evidence was already consumed"
                )
            # Validate the source envelope before making registration durable.
            # The runtime re-envelopes it with the real next sequence and trusted
            # availability instant after the registration fact is accepted.
            CaptureEvent(
                identity=self.identity,
                sequence=1,
                stream=stream,
                provider=provider,
                symbol=symbol,
                query=query,
                clocks=source_clocks,
                payload=source_payload,
            )
            registration_record = (
                self._producer_lifecycle.record_provider_registration_evidence(
                    producer_id,
                    evidence=evidence,
                    stream=stream,
                    provider=provider,
                    payload=source_payload,
                    clocks=source_clocks,
                    symbol=symbol,
                    query=query,
                )
            )
            self._observe_durable_event(registration_record)
            registered = self._producer_lifecycle.register_from_provider_evidence(
                producer_id,
                registration_record,
            )
            self._observe_durable_event(registered)
            try:
                submission = self._submit_event_locked(
                    stream=stream,
                    provider=provider,
                    payload=payload,
                    clocks=clocks,
                    symbol=symbol,
                    query=query,
                    producer_id=producer_id,
                    producer_token=token,
                    promotion_id=promotion_id,
                    promoted_at=promoted_at,
                    promotion_source_identity_sha256=(
                        promotion_source_identity_sha256
                    ),
                    promotion_resource_binding_sha256=(
                        promotion_resource_binding_sha256
                    ),
                    promotion_inventory_sha256=promotion_inventory_sha256,
                    promotion_admission_handoff_sha256=(
                        promotion_admission_handoff_sha256
                    ),
                    promotion_transfer=promotion_transfer,
                )
            except BaseException:
                # Registration is already an append-only fact.  Never pretend
                # the producer became usable if its proving frame could not be
                # captured; persist a gap when the lifecycle remains writable.
                if self._producer_lifecycle.health().get("submission_failure") is None:
                    self._submit_gap_locked(
                        CoverageGap(
                            stream=stream,
                            symbol=symbol,
                            reason="provider_first_frame_rejected",
                            first_available_at=clocks.available_at,
                            last_available_at=clocks.available_at,
                            lost_count=1,
                        )
                    )
                raise
            if not submission.accepted:
                raise CaptureContractError(
                    "provider first frame was not durably admitted"
                )
            self._external_registration_evidence[producer_id] = evidence
            return submission

    def _submit_bound_external(
        self,
        *,
        producer_id: str,
        token: object,
        stream: CaptureStream,
        provider: str,
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
        symbol: str | None,
        query: Mapping[str, Any] | None,
    ) -> CaptureSubmission:
        with self._lock:
            self._require_running()
            self._require_external_token(producer_id, token)
            if producer_id not in self._external_registration_evidence:
                raise CaptureContractError(
                    "external producer lacks first-frame registration evidence"
                )
            return self._submit_event_locked(
                stream=stream,
                provider=provider,
                payload=payload,
                clocks=clocks,
                symbol=symbol,
                query=query,
                producer_id=producer_id,
                producer_token=token,
            )

    def _heartbeat_bound_external(
        self, *, producer_id: str, token: object
    ) -> CaptureEvent:
        with self._lock:
            self._require_running()
            self._require_external_token(producer_id, token)
            if producer_id not in self._external_registration_evidence:
                raise CaptureContractError("unregistered producer cannot heartbeat")
            event = self._producer_lifecycle.heartbeat(producer_id)
            self._observe_durable_event(event)
            return event

    def _report_bound_external_gap(
        self,
        *,
        producer_id: str,
        token: object,
        stream: CaptureStream,
        reason: str,
        first_available_at: datetime,
        last_available_at: datetime,
        lost_count: int,
        symbol: str | None,
    ) -> CaptureEvent:
        with self._lock:
            self._require_running()
            self._require_external_token(producer_id, token)
            if producer_id not in self._external_registration_evidence:
                raise CaptureContractError(
                    "an unregistered producer cannot publish a lifecycle gap"
                )
            event = self._producer_lifecycle.report_gap(
                producer_id,
                stream=stream,
                reason=reason,
                first_available_at=first_available_at,
                last_available_at=last_available_at,
                lost_count=lost_count,
                symbol=symbol,
            )
            self._gapped_streams.add(stream)
            stats = self._stream_stats.get(stream)
            if stats is not None:
                stats["gapped"] = True
            self._observe_durable_event(event)
            return event

    def _close_bound_external(
        self, *, producer_id: str, token: object
    ) -> tuple[CaptureEvent, CaptureEvent]:
        with self._lock:
            self._require_running()
            self._require_external_token(producer_id, token)
            if producer_id not in self._external_registration_evidence:
                raise CaptureContractError("unregistered producer cannot close")
            spec = self._producer_lifecycle.producers[producer_id]
            for stream in spec.streams:
                if stream not in self._finalized_coverages:
                    self.finalize_stream_coverage(stream)
            quiescent = self._producer_lifecycle.quiesce(producer_id)
            self._observe_durable_event(quiescent)
            closed = self._producer_lifecycle.close_producer(producer_id)
            self._observe_durable_event(closed)
            return quiescent, closed

    def health(self) -> Mapping[str, Any]:
        with self._lock:
            return {
                **self._health_payload_locked(phase="inspection"),
                "producer_lifecycle": self._producer_lifecycle.health(),
                "certification_symbol": self.certification_symbol,
            }

    def stop_and_seal(
        self,
        *,
        stopped_at: datetime | None = None,
        timeout_seconds: float = 30.0,
    ) -> SealedCaptureHandoff:
        with self._lock:
            self._require_running()
            if self._hot_by_symbol or self._supervised_hot_tokens:
                raise CaptureContractError(
                    "release every hot-symbol lease before sealing capture"
                )
            lifecycle_health = self._producer_lifecycle.health()
            externally_required = set(self._producer_lifecycle.producers) - {
                self._coordinator_producer_id
            }
            externally_closed = set(lifecycle_health.get("closed_producers") or ())
            missing_external = sorted(externally_required - externally_closed)
            if missing_external:
                raise CaptureContractError(
                    "external producers must quiesce and close themselves before RUN_CLOSED: "
                    + ",".join(missing_external)
                )
            at = self._observed_now()
            if stopped_at is not None and _utc(stopped_at, "stopped_at") != at:
                raise CaptureContractError(
                    "stop time differs from trusted capture wall clock"
                )
            final_health = self._emit_capture_health_locked(at=at, phase="stopping")
            if not final_health.accepted:
                self._abort_locked(reason="final_health_admission_failed", at=at)
                raise CaptureContractError("final capture health was not admitted")
            coordinator_streams = self._producer_lifecycle.producers[
                self._coordinator_producer_id
            ].streams
            for stream in sorted(coordinator_streams, key=lambda row: row.value):
                if stream not in self._finalized_coverages:
                    self.finalize_stream_coverage(stream)
            event = self._producer_lifecycle.quiesce(
                self._coordinator_producer_id
            )
            self._observe_durable_event(event)
            event = self._producer_lifecycle.close_producer(
                self._coordinator_producer_id
            )
            self._observe_durable_event(event)
            run_closed = self._producer_lifecycle.close_run()
            self._observe_durable_event(run_closed)
            self._state = CaptureSessionState.STOPPING
            stopped = self.writer.stop(timeout_seconds=timeout_seconds)
            if not stopped:
                self._release_storage()
                self._state = CaptureSessionState.ABORTED
                raise CaptureContractError("capture writer did not stop cleanly; run unsealed")
            try:
                seal: CaptureRunSeal = self._producer_lifecycle.seal_run(self.writer)
                lifecycle_candidate = bool(
                    self._producer_lifecycle.health().get(
                        "certifying_seal_eligible", False
                    )
                )
                handoff = SealedCaptureHandoff(
                    identity=self.identity,
                    capture_root=self.capture_root,
                    final_seal_sha256=seal.seal_sha256,
                    event_count=seal.event_count,
                    gap_count=seal.gap_count,
                    gap_lost_count=seal.gap_lost_count,
                    resource_hashes=dict(self.resource_binding.hashes),
                    sequence_min=seal.sequence_min,
                    sequence_max=seal.sequence_max,
                    producer_lifecycle_candidate=lifecycle_candidate,
                )
                self._state = CaptureSessionState.SEALED
                return handoff
            except BaseException:
                # The writer and lifecycle are already closed at this point;
                # this run is permanently unsealed and cannot be resumed or
                # mistaken for an in-progress session.
                self._state = CaptureSessionState.ABORTED
                raise
            finally:
                self._release_storage()

    def _abort_locked(self, *, reason: str, at: datetime) -> UnsealedCaptureClose:
        if self._state not in {CaptureSessionState.RUNNING, CaptureSessionState.STOPPING}:
            raise CaptureContractError("only a running capture can be aborted")
        gap = CoverageGap(
            stream=CaptureStream.CAPTURE_HEALTH,
            symbol=None,
            reason=f"capture_session_aborted:{str(reason or 'unspecified').strip()}",
            first_available_at=at,
            last_available_at=at,
            lost_count=1,
        )
        self._submit_gap_locked(gap)
        writer_stopped = self.writer.stop(timeout_seconds=10.0)
        self._release_storage()
        self._state = CaptureSessionState.ABORTED
        return UnsealedCaptureClose(
            identity=self.identity,
            capture_root=self.capture_root,
            reason=gap.reason,
            writer_stopped=writer_stopped,
        )

    def abort(
        self,
        *,
        reason: str,
        aborted_at: datetime | None = None,
    ) -> UnsealedCaptureClose:
        with self._lock:
            self._require_running()
            at = self._observed_now()
            if aborted_at is not None and _utc(aborted_at, "aborted_at") != at:
                raise CaptureContractError(
                    "abort time differs from trusted capture wall clock"
                )
            return self._abort_locked(reason=reason, at=at)

    def __enter__(self) -> "LiveReplayCaptureCoordinator":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        with self._lock:
            if self._state is CaptureSessionState.RUNNING:
                self._abort_locked(
                    reason=("context_exception" if exc_type is not None else "unsealed_exit"),
                    at=self._observed_now(),
                )
        return False


class BoundLiveCaptureProducer:
    """Opaque ingress capability owned by one concrete external producer."""

    def __init__(
        self,
        *,
        coordinator: LiveReplayCaptureCoordinator,
        producer_id: str,
        token: object,
        spec: CaptureProducerSpec,
    ) -> None:
        self._coordinator = coordinator
        self.producer_id = producer_id
        self._token = token
        self.spec = spec

    @property
    def registered(self) -> bool:
        return self.producer_id in self._coordinator._external_registration_evidence

    def register_and_submit_first(
        self,
        *,
        evidence: CaptureProviderRegistrationEvidence,
        stream: CaptureStream,
        provider: str,
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
        symbol: str | None = None,
        query: Mapping[str, Any] | None = None,
        registration_source_payload: Mapping[str, Any] | None = None,
        registration_source_clocks: CaptureClocks | None = None,
        promotion_id: str | None = None,
        promoted_at: datetime | None = None,
        promotion_source_identity_sha256: str | None = None,
        promotion_resource_binding_sha256: str | None = None,
        promotion_inventory_sha256: str | None = None,
        promotion_admission_handoff_sha256: str | None = None,
        promotion_transfer: PromotionTransfer | None = None,
    ) -> CaptureSubmission:
        return self._coordinator._register_and_submit_external_first(
            producer_id=self.producer_id,
            token=self._token,
            evidence=evidence,
            stream=stream,
            provider=provider,
            payload=payload,
            clocks=clocks,
            symbol=symbol,
            query=query,
            registration_source_payload=registration_source_payload,
            registration_source_clocks=registration_source_clocks,
            promotion_id=promotion_id,
            promoted_at=promoted_at,
            promotion_source_identity_sha256=promotion_source_identity_sha256,
            promotion_resource_binding_sha256=(
                promotion_resource_binding_sha256
            ),
            promotion_inventory_sha256=promotion_inventory_sha256,
            promotion_admission_handoff_sha256=(
                promotion_admission_handoff_sha256
            ),
            promotion_transfer=promotion_transfer,
        )

    def submit_exact_input(
        self,
        *,
        stream: CaptureStream,
        provider: str,
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
        symbol: str | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> CaptureSubmission:
        return self._coordinator._submit_bound_external(
            producer_id=self.producer_id,
            token=self._token,
            stream=stream,
            provider=provider,
            payload=payload,
            clocks=clocks,
            symbol=symbol,
            query=query,
        )

    def heartbeat(self) -> CaptureEvent:
        return self._coordinator._heartbeat_bound_external(
            producer_id=self.producer_id, token=self._token
        )

    def report_gap(
        self,
        *,
        stream: CaptureStream,
        reason: str,
        first_available_at: datetime,
        last_available_at: datetime,
        lost_count: int,
        symbol: str | None = None,
    ) -> CaptureEvent:
        return self._coordinator._report_bound_external_gap(
            producer_id=self.producer_id,
            token=self._token,
            stream=stream,
            reason=reason,
            first_available_at=first_available_at,
            last_available_at=last_available_at,
            lost_count=lost_count,
            symbol=symbol,
        )

    def quiesce_and_close(self) -> tuple[CaptureEvent, CaptureEvent]:
        return self._coordinator._close_bound_external(
            producer_id=self.producer_id, token=self._token
        )


class MassiveWsLiveCaptureProducer:
    """Bind one exact Massive Q *or* T channel to a RUN_OPEN producer.

    Parser callbacks perform only a bounded in-memory admission before the
    operational fan-out.  Slow lifecycle validation and append-only submission
    run on this producer's bounded worker.  Overflow becomes explicit loss
    evidence instead of blocking the market-data parser.

    Massive documents ``q`` as increasing and unique, but not sequential, and
    exposes no authoritative provider watermark or durable per-symbol ACK.  A
    first exact frame proves registration; it does not manufacture continuity.
    """

    _ALLOWED_STREAMS = frozenset(
        {CaptureStream.NBBO_QUOTE, CaptureStream.PROVIDER_TRADE_PRINT}
    )
    _CHANNEL_BY_STREAM = {
        CaptureStream.NBBO_QUOTE: "Q",
        CaptureStream.PROVIDER_TRADE_PRINT: "T",
    }
    _WORKER_POLL_SECONDS = 0.05

    def __init__(
        self,
        *,
        endpoint: BoundLiveCaptureProducer,
        massive_client: Any,
        symbol: str,
        bounded_lateness_seconds: float,
        heartbeat_interval_seconds: float,
        max_pending_events: int | None = None,
    ) -> None:
        normalized_symbol = _normalized_symbol(symbol, required=True)
        assert normalized_symbol is not None
        streams = frozenset(endpoint.spec.streams)
        if len(streams) != 1 or not streams.issubset(self._ALLOWED_STREAMS):
            raise CaptureContractError(
                "Massive producer must own exactly one Q or T input stream"
            )
        if normalized_symbol != endpoint._coordinator.certification_symbol:
            raise CaptureContractError(
                "Massive producer symbol differs from capture certification symbol"
            )
        bounded_lateness = float(bounded_lateness_seconds)
        heartbeat_interval = float(heartbeat_interval_seconds)
        if not (bounded_lateness > 0 and heartbeat_interval > 0):
            raise CaptureContractError(
                "Massive lateness and heartbeat bounds must be positive"
            )
        if (
            heartbeat_interval
            >= endpoint._coordinator._producer_lifecycle.heartbeat_timeout_seconds
        ):
            raise CaptureContractError(
                "Massive heartbeat interval must precede lifecycle timeout"
            )
        for method in (
            "attach_capture_sink_for_symbols",
            "detach_capture_sink",
        ):
            if not callable(getattr(massive_client, method, None)):
                raise CaptureContractError("Massive client lacks capture sink support")
        self.endpoint = endpoint
        self.massive_client = massive_client
        self.symbol = normalized_symbol
        self.stream = next(iter(streams))
        self.channel = self._CHANNEL_BY_STREAM[self.stream]
        source_identity = massive_client.capture_source_identity
        if (
            source_identity.get("provider") != "massive_ws"
            or source_identity.get("instance_id") != endpoint.spec.instance_id
        ):
            raise CaptureContractError(
                "Massive source instance differs from RUN_OPEN producer"
            )
        provider_generation = source_identity.get("connection_generation")
        if isinstance(provider_generation, bool) or not isinstance(
            provider_generation, int
        ) or provider_generation <= 0:
            raise CaptureContractError(
                "Massive provider connection generation is unavailable"
            )
        if provider_generation != endpoint.spec.generation:
            raise CaptureContractError(
                "Massive source generation differs from RUN_OPEN producer"
            )
        self._provider_connection_generation = provider_generation
        self.bounded_lateness_seconds = bounded_lateness
        self.heartbeat_interval_seconds = heartbeat_interval
        measured_queue_limit = int(
            endpoint._coordinator.resource_binding.budget.max_queue_events
        )
        if max_pending_events is None:
            pending_limit = measured_queue_limit
        else:
            if isinstance(max_pending_events, bool):
                raise CaptureContractError(
                    "Massive capture queue capacity is malformed"
                )
            pending_limit = int(max_pending_events)
        if pending_limit <= 0 or pending_limit > measured_queue_limit:
            raise CaptureContractError(
                "Massive capture queue exceeds the measured resource binding"
            )
        self.max_pending_events = pending_limit
        self._capture_queue: queue.Queue[tuple[Any, ...]] = queue.Queue(
            maxsize=pending_limit
        )
        self._condition = threading.Condition(threading.RLock())
        self._started = False
        self._stop_attempted = False
        self._closed = False
        self._accepting = False
        self._worker_inflight = 0
        self._subscription_request_sha256: str | None = None
        self._last_provider_at: dict[CaptureStream, datetime] = {}
        self._max_lateness: dict[CaptureStream, float] = {}
        self._last_sequence: dict[CaptureStream, int] = {}
        # Parser-ingress order is tracked separately from durable worker order
        # so a regressing/reset frame can never reach the FSM while the worker
        # is still draining earlier admissions.
        self._ingress_last_sequence: dict[CaptureStream, int] = {}
        self._ingress_session_date: str | None = None
        self._event_count: dict[CaptureStream, int] = {}
        self._provider_session_date: str | None = None
        self._pending_gap_count = 0
        self._pending_gap_first_at: datetime | None = None
        self._pending_gap_last_at: datetime | None = None
        self._pending_gap_reported = False
        self._gap_reasons: OrderedDict[str, int] = OrderedDict()
        self._total_gap_lost_count = 0
        self._pending_overflow_count = 0
        self._pending_overflow_first_at: datetime | None = None
        self._pending_overflow_last_at: datetime | None = None
        self._overflow_lost_count = 0
        self._overflow_incidents = 0
        self._pending_provider_gaps: OrderedDict[
            str, tuple[int, datetime, datetime]
        ] = OrderedDict()
        self._coverage_failed = False
        self._terminal_fenced = False
        self._gap_persistence_failed = False
        self._provider_continuity_blocker_recorded = False
        self._terminal_error: BaseException | None = None
        self._capture_worker_stop = threading.Event()
        self._capture_worker_thread: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    @staticmethod
    def _timestamp(value: Any, field_name: str) -> datetime:
        if isinstance(value, bool) or value is None:
            raise CaptureContractError(f"Massive {field_name} is missing")
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CaptureContractError(f"Massive {field_name} is malformed") from exc
        if not math.isfinite(parsed) or parsed <= 0:
            raise CaptureContractError(f"Massive {field_name} is malformed")
        return datetime.fromtimestamp(parsed, tz=UTC)

    def on_massive_ws_subscription(self, evidence: dict[str, Any]) -> None:
        identity = self.massive_client.capture_source_identity
        expected_request = {
            "action": "subscribe",
            "params": f"{self.channel}.{self.symbol}",
        }
        if (
            not isinstance(evidence, Mapping)
            or evidence.get("provider") != "massive_ws"
            or evidence.get("instance_id") != self.endpoint.spec.instance_id
            or evidence.get("connection_generation")
            != self._provider_connection_generation
            or evidence.get("symbols") != [self.symbol]
            or evidence.get("channels") != [self.channel]
            or evidence.get("request") != expected_request
            or not identity.get("authenticated")
        ):
            raise CaptureContractError(
                "Massive subscription evidence differs from RUN_OPEN"
            )
        self._subscription_request_sha256 = sha256_json(evidence)

    def start(self) -> Mapping[str, Any]:
        with self._condition:
            if self._started:
                raise CaptureContractError("Massive capture producer is one-shot")
            identity = self.massive_client.capture_source_identity
            if (
                identity.get("provider") != "massive_ws"
                or identity.get("instance_id") != self.endpoint.spec.instance_id
                or identity.get("connection_generation")
                != self._provider_connection_generation
                or not identity.get("authenticated")
            ):
                raise CaptureContractError(
                    "Massive authenticated source differs from RUN_OPEN producer"
                )
            self._started = True
            self._accepting = True
        capture_thread = threading.Thread(
            target=self._capture_worker_loop,
            daemon=True,
            name=f"capture-massive-{self.channel.lower()}-{self.symbol.lower()}",
        )
        self._capture_worker_thread = capture_thread
        capture_thread.start()
        try:
            evidence = self.massive_client.attach_capture_sink_for_symbols(
                self, [self.symbol], channels=(self.channel,)
            )
        except BaseException:
            with self._condition:
                self._accepting = False
            self._capture_worker_stop.set()
            capture_thread.join(timeout=1.0)
            raise
        if self._subscription_request_sha256 != sha256_json(evidence):
            self.massive_client.detach_capture_sink(self)
            with self._condition:
                self._accepting = False
            self._capture_worker_stop.set()
            capture_thread.join(timeout=1.0)
            raise CaptureContractError("Massive subscription handoff changed in flight")
        thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name=f"capture-massive-{self.symbol.lower()}",
        )
        self._heartbeat_thread = thread
        thread.start()
        return dict(evidence)

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self.heartbeat_interval_seconds):
            with self._condition:
                if self._stop_attempted:
                    return
                registered = self.endpoint.registered
            if not registered:
                continue
            try:
                self.endpoint.heartbeat()
            except BaseException as exc:
                with self._condition:
                    if self._terminal_error is None:
                        self._terminal_error = exc
                    self._terminal_fenced = True
                    self._accepting = False
                    self._gap_persistence_failed = True
                return

    def _begin_worker(self) -> None:
        with self._condition:
            self._worker_inflight += 1

    def _end_worker(self) -> None:
        with self._condition:
            self._worker_inflight -= 1
            self._condition.notify_all()

    @staticmethod
    def _stream_for_snapshot(snapshot: Any) -> CaptureStream:
        return (
            CaptureStream.NBBO_QUOTE
            if hasattr(snapshot, "bid")
            else CaptureStream.PROVIDER_TRADE_PRINT
        )

    def _note_gap_reason_locked(self, reason: str, lost_count: int) -> None:
        normalized = str(reason or "massive_ws_provider_gap").strip().lower()
        capacity = int(
            self.endpoint._coordinator.resource_binding.budget.max_gap_keys
        )
        if normalized not in self._gap_reasons and len(self._gap_reasons) >= capacity:
            normalized = "massive_ws_additional_gap_reasons"
        self._gap_reasons[normalized] = (
            self._gap_reasons.get(normalized, 0) + lost_count
        )
        self._total_gap_lost_count += lost_count
        self._coverage_failed = True

    def _record_gap(
        self, *, reason: str, at: datetime, lost_count: int
    ) -> None:
        normalized = str(reason or "massive_ws_provider_gap").strip().lower()
        lost = max(1, int(lost_count))
        with self._condition:
            self._note_gap_reason_locked(normalized, lost)
        if not self.endpoint.registered:
            with self._condition:
                self._pending_gap_count += lost
                self._pending_gap_first_at = min(
                    at, self._pending_gap_first_at or at
                )
                self._pending_gap_last_at = max(
                    at, self._pending_gap_last_at or at
                )
            return
        self.endpoint.report_gap(
            stream=self.stream,
            reason=normalized,
            first_available_at=at,
            last_available_at=at,
            lost_count=lost,
            symbol=self.symbol,
        )

    def _fence_terminal(self, error: BaseException) -> None:
        with self._condition:
            if self._terminal_error is None:
                self._terminal_error = error
            self._terminal_fenced = True
            self._accepting = False
            self._coverage_failed = True
            self._condition.notify_all()

    def _latch_queue_overflow(self, *, at: datetime, lost_count: int) -> None:
        lost = max(1, int(lost_count))
        # Called on the single websocket parser thread.  Keep this a bounded
        # in-memory latch; the worker polls and persists it asynchronously.
        self._pending_overflow_count += lost
        self._pending_overflow_first_at = min(
            at, self._pending_overflow_first_at or at
        )
        self._pending_overflow_last_at = max(
            at, self._pending_overflow_last_at or at
        )
        self._overflow_lost_count += lost
        self._overflow_incidents += 1
        self._coverage_failed = True

    def _take_pending_overflow(
        self,
    ) -> tuple[int, datetime, datetime] | None:
        with self._condition:
            count = self._pending_overflow_count
            first = self._pending_overflow_first_at
            last = self._pending_overflow_last_at
            if count <= 0 or first is None or last is None:
                return None
            self._pending_overflow_count = 0
            self._pending_overflow_first_at = None
            self._pending_overflow_last_at = None
            return count, first, last

    def _flush_pending_overflow(self) -> None:
        pending = self._take_pending_overflow()
        if pending is None:
            return
        count, first, last = pending
        with self._condition:
            self._note_gap_reason_locked(
                "massive_ws_capture_queue_overflow", count
            )
        if not self.endpoint.registered:
            with self._condition:
                self._pending_gap_count += count
                self._pending_gap_first_at = min(
                    first, self._pending_gap_first_at or first
                )
                self._pending_gap_last_at = max(
                    last, self._pending_gap_last_at or last
                )
            return
        self.endpoint.report_gap(
            stream=self.stream,
            reason="massive_ws_capture_queue_overflow",
            first_available_at=first,
            last_available_at=last,
            lost_count=count,
            symbol=self.symbol,
        )

    def _latch_pending_provider_gap(
        self, *, reason: str, at: datetime, lost_count: int
    ) -> None:
        """Bounded in-memory side latch when the parser queue is full."""

        normalized = str(reason or "massive_ws_provider_gap").strip().lower()
        lost = max(1, int(lost_count))
        capacity = int(
            self.endpoint._coordinator.resource_binding.budget.max_gap_keys
        )
        if (
            normalized not in self._pending_provider_gaps
            and len(self._pending_provider_gaps) >= capacity
        ):
            normalized = "massive_ws_additional_gap_reasons"
        prior = self._pending_provider_gaps.get(normalized)
        if prior is None:
            self._pending_provider_gaps[normalized] = (lost, at, at)
        else:
            self._pending_provider_gaps[normalized] = (
                prior[0] + lost,
                min(prior[1], at),
                max(prior[2], at),
            )
        self._coverage_failed = True

    def _take_pending_provider_gaps(
        self,
    ) -> tuple[tuple[str, int, datetime, datetime], ...]:
        with self._condition:
            if not self._pending_provider_gaps:
                return ()
            pending = tuple(
                (reason, count, first, last)
                for reason, (count, first, last) in self._pending_provider_gaps.items()
            )
            self._pending_provider_gaps.clear()
            return pending

    def _flush_pending_provider_gaps(self) -> None:
        for reason, count, first, last in self._take_pending_provider_gaps():
            with self._condition:
                self._note_gap_reason_locked(reason, count)
            if not self.endpoint.registered:
                with self._condition:
                    self._pending_gap_count += count
                    self._pending_gap_first_at = min(
                        first, self._pending_gap_first_at or first
                    )
                    self._pending_gap_last_at = max(
                        last, self._pending_gap_last_at or last
                    )
            else:
                self.endpoint.report_gap(
                    stream=self.stream,
                    reason=reason,
                    first_available_at=first,
                    last_available_at=last,
                    lost_count=count,
                    symbol=self.symbol,
                )
            if reason in {
                "massive_ws_connection_closed",
                "massive_ws_generation_changed",
                "massive_ws_session_boundary_crossed",
            }:
                self._fence_terminal(
                    CaptureContractError(
                        f"Massive provider continuity ended: {reason}"
                    )
                )

    def _worker_failure_at(self, item: tuple[Any, ...]) -> datetime:
        try:
            if item[0] == "frame":
                return self._timestamp(
                    getattr(item[2], "available_at", None),
                    "worker failure available_at",
                )
            return self._timestamp(item[3], "worker failure received_at")
        except CaptureContractError:
            return self.endpoint._coordinator._observed_now()

    def _capture_worker_loop(self) -> None:
        while True:
            try:
                self._flush_pending_overflow()
            except BaseException as exc:
                with self._condition:
                    self._gap_persistence_failed = True
                self._fence_terminal(exc)
            if self._capture_queue.empty():
                try:
                    self._flush_pending_provider_gaps()
                except BaseException as exc:
                    with self._condition:
                        self._gap_persistence_failed = True
                    self._fence_terminal(exc)
            if self._capture_worker_stop.is_set() and self._capture_queue.empty():
                with self._condition:
                    if (
                        self._pending_overflow_count <= 0
                        and not self._pending_provider_gaps
                    ):
                        return
            try:
                item = self._capture_queue.get(timeout=self._WORKER_POLL_SECONDS)
            except queue.Empty:
                continue
            try:
                if item[0] == "frame":
                    self._process_massive_ws_frame(item[1], item[2])
                else:
                    self._process_massive_ws_gap(
                        reason=item[1],
                        symbol=item[2],
                        received_at=item[3],
                        lost_count=item[4],
                    )
            except BaseException as exc:
                at = self._worker_failure_at(item)
                try:
                    self._record_gap(
                        reason="massive_ws_capture_worker_failed",
                        at=at,
                        lost_count=1,
                    )
                except BaseException:
                    with self._condition:
                        self._gap_persistence_failed = True
                self._fence_terminal(exc)
            finally:
                self._capture_queue.task_done()
                with self._condition:
                    self._condition.notify_all()

    def _frame_payload(self, snapshot: Any, stream: CaptureStream) -> dict[str, Any]:
        common = {
            "event_kind": (
                "quote" if stream is CaptureStream.NBBO_QUOTE else "trade"
            ),
            "price": snapshot.price,
            "provider_timestamp_ms": snapshot.provider_timestamp_ms,
            "provider_sequence": snapshot.sequence,
            "provider_run_id": snapshot.bridge_run_id,
            "provider_connection_generation": snapshot.connection_generation,
        }
        if stream is CaptureStream.NBBO_QUOTE:
            return {
                **common,
                "bid": snapshot.bid,
                "ask": snapshot.ask,
                "bid_size": snapshot.bid_size,
                "ask_size": snapshot.ask_size,
                "bid_exchange": snapshot.bid_exchange,
                "ask_exchange": snapshot.ask_exchange,
                "condition": snapshot.condition,
                "indicators": list(snapshot.indicators),
                "tape": snapshot.tape,
            }
        return {
            **common,
            "size": snapshot.size,
            "participant_timestamp_ms": snapshot.participant_timestamp_ms,
            "trf_timestamp_ms": snapshot.trf_timestamp_ms,
            "exchange": snapshot.exchange,
            "trade_id": snapshot.trade_id,
            "tape": snapshot.tape,
            "conditions": list(snapshot.conditions),
            "trf_id": snapshot.trf_id,
            "fractional_size": snapshot.fractional_size,
        }

    def on_massive_ws_frame(self, symbol: str, snapshot: Any) -> bool:
        """Admit one parser frame without touching coordinator or disk locks."""

        if str(symbol or "").strip().upper() != self.symbol:
            return False
        stream = self._stream_for_snapshot(snapshot)
        if stream is not self.stream:
            return False
        available_at = self._timestamp(
            getattr(snapshot, "available_at", None), "available_at"
        )
        received_at = self._timestamp(
            getattr(snapshot, "received_at", None), "received_at"
        )
        provider_event_at = self._timestamp(
            getattr(snapshot, "provider_event_at", None), "provider_event_at"
        )
        if available_at < received_at:
            raise CaptureContractError(
                "Massive available_at precedes socket received_at"
            )
        provider_timestamp_ms = getattr(snapshot, "provider_timestamp_ms", None)
        if (
            isinstance(provider_timestamp_ms, bool)
            or not isinstance(provider_timestamp_ms, int)
            or provider_timestamp_ms <= 0
            or abs(
                provider_event_at.timestamp()
                - (provider_timestamp_ms / 1000.0)
            )
            > 0.000_001
        ):
            raise CaptureContractError(
                "Massive provider event clock differs from exact Unix-ms payload"
            )
        sequence = getattr(snapshot, "sequence", None)
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise CaptureContractError("Massive provider sequence is malformed")
        generation_changed = (
            getattr(snapshot, "bridge_run_id", None)
            != self.endpoint.spec.instance_id
            or getattr(snapshot, "connection_generation", None)
            != self._provider_connection_generation
        )
        session_date = provider_event_at.astimezone(
            _MASSIVE_MARKET_TZ
        ).date().isoformat()
        gap_reason: str | None = None
        if not self._accepting or self._terminal_fenced:
            return False
        if generation_changed:
            gap_reason = "massive_ws_generation_changed"
        elif (
            self._ingress_session_date is not None
            and session_date != self._ingress_session_date
        ):
            gap_reason = "massive_ws_session_boundary_crossed"
        else:
            prior_sequence = self._ingress_last_sequence.get(stream)
            if prior_sequence is not None and sequence <= prior_sequence:
                gap_reason = "massive_ws_sequence_nonmonotonic"
            else:
                self._ingress_session_date = session_date
                self._ingress_last_sequence[stream] = sequence
        if gap_reason is not None:
            self.on_massive_ws_gap(
                reason=gap_reason,
                symbol=self.symbol,
                received_at=available_at.timestamp(),
                lost_count=1,
            )
            return False
        snapshot_copy = copy.copy(snapshot)
        try:
            self._capture_queue.put_nowait(("frame", self.symbol, snapshot_copy))
        except queue.Full:
            self._latch_queue_overflow(at=available_at, lost_count=1)
            return False
        return True

    def _process_massive_ws_frame(self, symbol: str, snapshot: Any) -> None:
        del symbol
        self._begin_worker()
        try:
            with self._condition:
                if self._terminal_fenced:
                    return
            stream = self._stream_for_snapshot(snapshot)
            if stream is not self.stream:
                return
            provider_event_at = self._timestamp(
                getattr(snapshot, "provider_event_at", None), "provider_event_at"
            )
            received_at = self._timestamp(
                getattr(snapshot, "received_at", None), "received_at"
            )
            available_at = self._timestamp(
                getattr(snapshot, "available_at", None), "available_at"
            )
            sequence = getattr(snapshot, "sequence", None)
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                raise CaptureContractError("Massive provider sequence is malformed")
            if (
                getattr(snapshot, "bridge_run_id", None) != self.endpoint.spec.instance_id
                or getattr(snapshot, "connection_generation", None)
                != self._provider_connection_generation
            ):
                self._record_gap(
                    reason="massive_ws_generation_changed",
                    at=available_at,
                    lost_count=1,
                )
                self._fence_terminal(
                    CaptureContractError(
                        "Massive connection generation changed during capture"
                    )
                )
                return
            session_date = provider_event_at.astimezone(
                _MASSIVE_MARKET_TZ
            ).date().isoformat()
            prior_session_date = self._provider_session_date
            if prior_session_date is None:
                self._provider_session_date = session_date
            elif session_date != prior_session_date:
                self._record_gap(
                    reason="massive_ws_session_boundary_crossed",
                    at=available_at,
                    lost_count=1,
                )
                self._fence_terminal(
                    CaptureContractError(
                        "Massive provider session changed during one capture run"
                    )
                )
                return
            prior_sequence = self._last_sequence.get(stream)
            if prior_sequence is not None and sequence <= prior_sequence:
                self._record_gap(
                    reason="massive_ws_sequence_nonmonotonic",
                    at=available_at,
                    lost_count=1,
                )
                return
            payload = self._frame_payload(snapshot, stream)
            clocks = CaptureClocks(
                provider_event_at=provider_event_at,
                received_at=received_at,
                available_at=available_at,
            )
            if not self.endpoint.registered:
                if self._subscription_request_sha256 is None:
                    raise CaptureContractError(
                        "Massive first frame preceded subscription evidence"
                    )
                registration = CaptureProviderRegistrationEvidence(
                    producer_id=self.endpoint.producer_id,
                    provider="massive_ws",
                    provider_instance_id=snapshot.bridge_run_id,
                    provider_generation=snapshot.connection_generation,
                    evidence_kind="first_provider_frame",
                    source_payload_sha256=sha256_json(payload),
                    provider_event_at=provider_event_at,
                    received_at=received_at,
                    provider_sequence=sequence,
                    subscription_request_sha256=self._subscription_request_sha256,
                )
                self.endpoint.register_and_submit_first(
                    evidence=registration,
                    stream=stream,
                    provider="massive_ws",
                    payload=payload,
                    clocks=clocks,
                    symbol=self.symbol,
                )
                with self._condition:
                    pending_gap_count = self._pending_gap_count
                    pending_gap_first_at = self._pending_gap_first_at
                    pending_gap_last_at = self._pending_gap_last_at
                    already_reported = self._pending_gap_reported
                    self._pending_gap_reported = pending_gap_count > 0
                if pending_gap_count and not already_reported:
                    assert pending_gap_first_at is not None
                    assert pending_gap_last_at is not None
                    self.endpoint.report_gap(
                        stream=self.stream,
                        reason="massive_ws_pre_registration_gap",
                        first_available_at=pending_gap_first_at,
                        last_available_at=pending_gap_last_at,
                        lost_count=pending_gap_count,
                        symbol=self.symbol,
                    )
            else:
                self.endpoint.submit_exact_input(
                    stream=stream,
                    provider="massive_ws",
                    payload=payload,
                    clocks=clocks,
                    symbol=self.symbol,
                )
            lateness = max(0.0, (received_at - provider_event_at).total_seconds())
            self._last_sequence[stream] = sequence
            self._last_provider_at[stream] = max(
                provider_event_at,
                self._last_provider_at.get(stream, provider_event_at),
            )
            self._max_lateness[stream] = max(
                lateness, self._max_lateness.get(stream, 0.0)
            )
            self._event_count[stream] = self._event_count.get(stream, 0) + 1
        finally:
            self._end_worker()

    def on_massive_ws_gap(
        self,
        *,
        reason: str,
        symbol: str | None,
        received_at: float,
        lost_count: int,
    ) -> None:
        if symbol is not None and str(symbol).strip().upper() != self.symbol:
            return
        normalized_reason = str(
            reason or "massive_ws_provider_gap"
        ).strip().lower()
        terminal_reason = normalized_reason in {
            "massive_ws_connection_closed",
            "massive_ws_generation_changed",
            "massive_ws_session_boundary_crossed",
        }
        if not self._accepting or self._terminal_fenced:
            return
        try:
            normalized_lost_count = max(1, int(lost_count))
        except (TypeError, ValueError, OverflowError):
            normalized_lost_count = 1
        try:
            self._capture_queue.put_nowait(
                (
                    "gap",
                    normalized_reason,
                    symbol,
                    received_at,
                    normalized_lost_count,
                )
            )
        except queue.Full:
            at = self._timestamp(received_at, "queue overflow received_at")
            self._latch_pending_provider_gap(
                reason=normalized_reason,
                at=at,
                lost_count=normalized_lost_count,
            )
        if terminal_reason:
            # Stop new parser admissions immediately, while leaving the
            # terminal flag unset until the FIFO worker persists every earlier
            # frame and then this gap.
            self._accepting = False

    def _process_massive_ws_gap(
        self,
        *,
        reason: str,
        symbol: str | None,
        received_at: float,
        lost_count: int,
    ) -> None:
        del symbol
        self._begin_worker()
        try:
            at = self._timestamp(received_at, "gap received_at")
            normalized_reason = str(
                reason or "massive_ws_provider_gap"
            ).strip().lower()
            self._record_gap(
                reason=normalized_reason,
                at=at,
                lost_count=max(1, int(lost_count)),
            )
            if normalized_reason in {
                "massive_ws_connection_closed",
                "massive_ws_generation_changed",
                "massive_ws_session_boundary_crossed",
            }:
                self._fence_terminal(
                    CaptureContractError(
                        f"Massive provider continuity ended: {normalized_reason}"
                    )
                )
        finally:
            self._end_worker()

    def health(self) -> Mapping[str, Any]:
        with self._condition:
            worker = self._capture_worker_thread
            heartbeat = self._heartbeat_thread
            return {
                "producer_id": self.endpoint.producer_id,
                "symbol": self.symbol,
                "owned_stream": self.stream.value,
                "subscribed_channel": self.channel,
                "provider_instance_id": self.endpoint.spec.instance_id,
                "capture_generation": self.endpoint.spec.generation,
                "provider_connection_generation": (
                    self._provider_connection_generation
                ),
                "provider_session_date_et": self._provider_session_date,
                "subscription_request_sha256": (
                    self._subscription_request_sha256
                ),
                "registered_from_first_frame": self.endpoint.registered,
                "accepting": self._accepting,
                "worker_inflight": self._worker_inflight,
                "capture_worker_alive": bool(
                    worker is not None and worker.is_alive()
                ),
                "heartbeat_alive": bool(
                    heartbeat is not None and heartbeat.is_alive()
                ),
                "queue_capacity": self.max_pending_events,
                "queue_depth": self._capture_queue.qsize(),
                "queue_unfinished_tasks": self._capture_queue.unfinished_tasks,
                "overflow_incidents": self._overflow_incidents,
                "overflow_lost_count": self._overflow_lost_count,
                "pending_overflow_count": self._pending_overflow_count,
                "pending_provider_gap_count": sum(
                    row[0] for row in self._pending_provider_gaps.values()
                ),
                "event_count": {
                    stream.value: count
                    for stream, count in sorted(
                        self._event_count.items(), key=lambda row: row[0].value
                    )
                },
                "last_provider_sequence": {
                    stream.value: sequence
                    for stream, sequence in self._last_sequence.items()
                },
                "bounded_lateness_seconds": self.bounded_lateness_seconds,
                "max_observed_lateness_seconds": {
                    stream.value: seconds
                    for stream, seconds in self._max_lateness.items()
                },
                "provider_watermark_available": False,
                "provider_continuity_provable": False,
                "provider_continuity_blocker_recorded": (
                    self._provider_continuity_blocker_recorded
                ),
                "coverage_failed": self._coverage_failed,
                "gap_lost_count": self._total_gap_lost_count,
                "gap_reasons": dict(self._gap_reasons),
                "pending_gap_count": self._pending_gap_count,
                "terminal_fenced": self._terminal_fenced,
                "terminal_error": (
                    None if self._terminal_error is None else str(self._terminal_error)
                ),
                "gap_persistence_failed": self._gap_persistence_failed,
                "closed": self._closed,
            }

    def _wait_for_queue_drain(self, deadline: float) -> bool:
        import time as _time

        while _time.monotonic() < deadline:
            with self._capture_queue.all_tasks_done:
                unfinished = self._capture_queue.unfinished_tasks
            with self._condition:
                pending_overflow = self._pending_overflow_count
                pending_provider_gaps = bool(self._pending_provider_gaps)
                if (
                    unfinished <= 0
                    and pending_overflow <= 0
                    and not pending_provider_gaps
                ):
                    return True
                self._condition.wait(
                    timeout=min(
                        self._WORKER_POLL_SECONDS,
                        max(0.0, deadline - _time.monotonic()),
                    )
                )
        return False

    def stop(self, *, timeout_seconds: float = 10.0) -> tuple[CaptureEvent, CaptureEvent]:
        """Detach, drain the bounded worker, grade honestly, and close once."""

        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise CaptureContractError("Massive producer stop timeout must be positive")
        with self._condition:
            if not self._started or self._stop_attempted:
                raise CaptureContractError("Massive capture producer is not running")
            self._stop_attempted = True

        # The concrete client's detach is a callback barrier.  Keep admission
        # open until it returns so a callback reserved just before detach cannot
        # disappear between the parser and this bounded queue.
        self.massive_client.detach_capture_sink(self)
        with self._condition:
            self._accepting = False

        import time as _time

        deadline = _time.monotonic() + timeout
        if not self._wait_for_queue_drain(deadline):
            self._capture_worker_stop.set()
            self._heartbeat_stop.set()
            self._fence_terminal(
                CaptureContractError(
                    "Massive capture queue did not drain before close"
                )
            )
            raise CaptureContractError(
                "Massive capture queue did not drain before close"
            )

        self._capture_worker_stop.set()
        capture_thread = self._capture_worker_thread
        if capture_thread is not None:
            capture_thread.join(timeout=max(0.0, deadline - _time.monotonic()))
            if capture_thread.is_alive():
                self._fence_terminal(
                    CaptureContractError(
                        "Massive capture worker did not quiesce before close"
                    )
                )
                raise CaptureContractError(
                    "Massive capture worker did not quiesce before close"
                )

        self._heartbeat_stop.set()
        heartbeat_thread = self._heartbeat_thread
        if heartbeat_thread is not None:
            heartbeat_thread.join(
                timeout=max(0.0, deadline - _time.monotonic())
            )
            if heartbeat_thread.is_alive():
                raise CaptureContractError(
                    "Massive producer heartbeat did not quiesce before close"
                )
        self._capture_worker_thread = None
        self._heartbeat_thread = None

        with self._condition:
            if self._worker_inflight:
                raise CaptureContractError(
                    "Massive capture worker remained in flight before close"
                )
            persistence_failed = self._gap_persistence_failed
        if persistence_failed:
            raise CaptureContractError(
                "Massive producer could not persist terminal coverage evidence"
            )
        if not self.endpoint.registered:
            raise CaptureContractError(
                "Massive producer never received a first-frame acknowledgement"
            )
        if self._event_count.get(self.stream, 0) <= 0:
            raise CaptureContractError(
                f"Massive producer lacks source evidence for: {self.stream.value}"
            )

        emitted_at = self.endpoint._coordinator._observed_now()
        max_lateness = self._max_lateness[self.stream]
        if max_lateness > self.bounded_lateness_seconds:
            self._record_gap(
                reason="massive_ws_lateness_bound_exceeded",
                at=emitted_at,
                lost_count=1,
            )

        # Q/T sequence values are not contiguous and the socket exposes no
        # authoritative provider watermark.  The last locally observed frame
        # is not a bounded-lateness proof, so final coverage must fail closed.
        self._record_gap(
            reason="massive_ws_provider_continuity_unprovable",
            at=emitted_at,
            lost_count=1,
        )
        with self._condition:
            self._provider_continuity_blocker_recorded = True

        lifecycle = self.endpoint.quiesce_and_close()
        with self._condition:
            self._closed = True
        return lifecycle


class LiveReplayCaptureSupervisor:
    """Always-on broad ring with one host-wide hot/queue admission budget.

    A supervisor identity is provisional only: its ring rows are never treated
    as target-run events.  Promotion reserves a non-destructive snapshot,
    re-envelopes every row through the target run's real lifecycle, and consumes
    the ring inventory only after all target submissions were accepted.
    """

    def __init__(
        self,
        *,
        identity: CaptureRunIdentity,
        resource_binding: CaptureResourceBinding,
        pressure_controller: CaptureAdaptivePressureController,
        pretrigger_ring: BoundedPreTriggerRing,
        hot_symbol_leases: BoundedHotSymbolLeases,
        shared_admission_budget: SharedCaptureAdmissionBudget,
        wall_clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(identity, CaptureRunIdentity):
            raise CaptureContractError("capture supervisor identity is malformed")
        if pressure_controller.binding != resource_binding:
            raise CaptureContractError("capture supervisor pressure binding mismatch")
        if pretrigger_ring.resource_binding != resource_binding:
            raise CaptureContractError("capture supervisor ring binding mismatch")
        if hot_symbol_leases.identity != identity:
            raise CaptureContractError("capture supervisor hot identity mismatch")
        if hot_symbol_leases.resource_binding != resource_binding:
            raise CaptureContractError("capture supervisor hot binding mismatch")
        if shared_admission_budget.resource_binding != resource_binding:
            raise CaptureContractError("capture supervisor shared budget mismatch")
        if not callable(wall_clock):
            raise CaptureContractError("capture supervisor wall clock is malformed")
        self.identity = identity
        self.resource_binding = resource_binding
        self.pressure_controller = pressure_controller
        self.pretrigger_ring = pretrigger_ring
        self.hot_symbol_leases = hot_symbol_leases
        self.shared_admission_budget = shared_admission_budget
        self._wall_clock = wall_clock
        self._lock = threading.RLock()
        self._ownership_token = object()
        self._coordinator_by_symbol: dict[str, LiveReplayCaptureCoordinator] = {}
        self._active_by_symbol: dict[
            str, tuple[LiveReplayCaptureCoordinator, HotSymbolLease]
        ] = {}

    @classmethod
    def create(
        cls,
        *,
        identity: CaptureRunIdentity,
        resource_binding: CaptureResourceBinding,
        pressure_controller: CaptureAdaptivePressureController,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        pretrigger_horizon: timedelta,
        per_symbol_pretrigger_events: int,
        shared_admission_budget: SharedCaptureAdmissionBudget | None = None,
    ) -> "LiveReplayCaptureSupervisor":
        shared = shared_admission_budget or (
            SharedCaptureAdmissionBudget.from_resource_binding(
                resource_binding,
                pressure_controller=pressure_controller,
            )
        )
        if shared.resource_binding != resource_binding:
            raise CaptureContractError(
                "capture supervisor shared admission binding mismatch"
            )
        if shared.pressure_controller is not pressure_controller:
            raise CaptureContractError(
                "capture supervisor shared pressure controller mismatch"
            )
        ring = BoundedPreTriggerRing.from_resource_binding(
            resource_binding,
            horizon=pretrigger_horizon,
            per_symbol_max_events=per_symbol_pretrigger_events,
            pressure_controller=pressure_controller,
        )
        hot = BoundedHotSymbolLeases(
            identity=identity,
            resource_binding=resource_binding,
            pressure_controller=pressure_controller,
        )
        return cls(
            identity=identity,
            resource_binding=resource_binding,
            pressure_controller=pressure_controller,
            pretrigger_ring=ring,
            hot_symbol_leases=hot,
            shared_admission_budget=shared,
            wall_clock=wall_clock,
        )

    @property
    def network_fallback_allowed(self) -> bool:
        return False

    def _observed_now(self) -> datetime:
        return _utc(self._wall_clock(), "capture supervisor wall clock")

    def _hot_health_locked(self) -> dict[str, Any]:
        return {
            "scope": "global_live_replay_capture_supervisor",
            "resource_hashes": self.resource_binding.hashes,
            "leases": self.hot_symbol_leases.health(),
            "shared_admission": self.shared_admission_budget.health(),
        }

    def _publish_hot_health_locked(self) -> None:
        health = self._hot_health_locked()
        for coordinator in self._coordinator_by_symbol.values():
            coordinator._update_supervisor_hot_health(
                self._ownership_token,
                health,
            )

    def attach(self, coordinator: LiveReplayCaptureCoordinator) -> None:
        """Bind a new one-symbol run before it starts."""

        if not isinstance(coordinator, LiveReplayCaptureCoordinator):
            raise CaptureContractError("supervised capture coordinator is malformed")
        with self._lock:
            symbol = coordinator.certification_symbol
            if symbol in self._coordinator_by_symbol:
                raise CaptureContractError(
                    f"capture supervisor already owns symbol {symbol}"
                )
            if coordinator.resource_binding != self.resource_binding:
                raise CaptureContractError(
                    "supervised coordinator resource binding mismatch"
                )
            if coordinator.pressure_controller is not self.pressure_controller:
                raise CaptureContractError(
                    "supervised coordinator must share the pressure controller"
                )
            if coordinator._wall_clock is not self._wall_clock:
                raise CaptureContractError(
                    "supervised coordinator must share the trusted wall clock"
                )
            coordinator._bind_supervisor(
                self._ownership_token,
                shared_admission_budget=self.shared_admission_budget,
                hot_health=self._hot_health_locked(),
            )
            self._coordinator_by_symbol[symbol] = coordinator

    def detach(self, coordinator: LiveReplayCaptureCoordinator) -> bool:
        """Forget one inactive, closed/unused one-shot target run."""

        if not isinstance(coordinator, LiveReplayCaptureCoordinator):
            raise CaptureContractError("supervised capture coordinator is malformed")
        with self._lock:
            symbol = coordinator.certification_symbol
            current = self._coordinator_by_symbol.get(symbol)
            if current is None:
                return False
            if current is not coordinator:
                raise CaptureContractError(
                    "capture supervisor coordinator ownership mismatch"
                )
            if symbol in self._active_by_symbol:
                raise CaptureContractError("cannot detach an active hot coordinator")
            if coordinator.state is CaptureSessionState.RUNNING:
                raise CaptureContractError("cannot detach a running coordinator")
            self._coordinator_by_symbol.pop(symbol, None)
            self._publish_hot_health_locked()
            return True

    def record_broad_input(
        self,
        *,
        stream: CaptureStream,
        provider: str,
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
        symbol: str,
        query: Mapping[str, Any] | None = None,
    ) -> CaptureSubmission:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        if stream not in _BROAD_STREAMS:
            raise CaptureContractError(
                f"{stream.value} is not an allowed supervisor broad stream"
            )
        with self._lock:
            active = self._active_by_symbol.get(normalized)
            if active is not None:
                coordinator, _lease = active
                return coordinator._submit_supervised_hot_input(
                    self._ownership_token,
                    stream=stream,
                    provider=provider,
                    payload=payload,
                    clocks=clocks,
                    symbol=normalized,
                    query=query,
                )
            if clocks.available_at > self._observed_now():
                raise CaptureContractError(
                    "shared pretrigger availability is later than trusted wall clock"
                )
            retained, provisional = self.pretrigger_ring.retain_observation(
                identity=self.identity,
                stream=stream,
                provider=provider,
                payload=payload,
                clocks=clocks,
                symbol=normalized,
                query=query,
            )
            if retained:
                return CaptureSubmission(
                    accepted=True,
                    event=provisional,
                    coverage_gap_recorded=False,
                    disposition="shared_pretrigger_retained_provisional",
                )
            return CaptureSubmission(
                accepted=False,
                event=None,
                coverage_gap_recorded=True,
                disposition="shared_pretrigger_capacity_gap_pending_promotion",
            )

    def record_broad_gap(self, gap: CoverageGap) -> tuple[str, ...]:
        """Route one upstream loss to cold promotion history and active runs.

        This is the loss-side companion to :meth:`record_broad_input`.  A gap
        can occur before an event is constructed (for example a bounded IPC
        queue overflow), so it must enter the pre-trigger ledger directly.  A
        global gap is also copied into every currently active one-symbol run.
        """

        if not isinstance(gap, CoverageGap):
            raise CaptureContractError("supervisor broad gap is malformed")
        if gap.stream not in _BROAD_STREAMS:
            raise CaptureContractError(
                f"{gap.stream.value} is not an allowed supervisor broad gap stream"
            )
        with self._lock:
            self.pretrigger_ring.record_gap(self.identity, gap)
            if gap.symbol is None:
                active = tuple(sorted(self._active_by_symbol.items()))
            else:
                row = self._active_by_symbol.get(gap.symbol)
                active = () if row is None else ((gap.symbol, row),)
            reported: list[str] = []
            for symbol, (coordinator, _lease) in active:
                scoped = gap
                if gap.symbol is None:
                    scoped = CoverageGap(
                        stream=gap.stream,
                        symbol=symbol,
                        reason=gap.reason,
                        first_available_at=gap.first_available_at,
                        last_available_at=gap.last_available_at,
                        lost_count=gap.lost_count,
                    )
                coordinator._report_supervised_gap(self._ownership_token, scoped)
                reported.append(symbol)
            return tuple(reported)

    def record_hot_gap(self, gap: CoverageGap) -> bool:
        """Persist loss for one already-admitted hot-only producer stream.

        L2 deltas/checkpoints are intentionally not broad-ring inputs.  Their
        bounded producer queues therefore need a public supervisor-owned gap
        path which cannot forge a cold/pretrigger loss or reach a foreign run.
        """

        if not isinstance(gap, CoverageGap):
            raise CaptureContractError("supervisor hot gap is malformed")
        normalized = _normalized_symbol(gap.symbol, required=True)
        assert normalized is not None
        if gap.stream in _BROAD_STREAMS:
            raise CaptureContractError(
                "broad-stream loss must use the broad gap ledger"
            )
        with self._lock:
            active = self._active_by_symbol.get(normalized)
            if active is None:
                raise CaptureContractError("hot gap symbol is not globally hot")
            coordinator, _lease = active
            return coordinator._report_supervised_gap(self._ownership_token, gap)

    def record_broad_change(
        self,
        *,
        stream: CaptureStream,
        provider: str,
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
        symbol: str,
        change_key: str,
        query: Mapping[str, Any] | None = None,
    ) -> ChangeCaptureResult:
        """Deduplicate a broad change-log fact before it can evict tape history."""

        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        policy = STREAM_POLICIES[stream]
        if (
            stream not in _BROAD_STREAMS
            or policy.coverage_mode is not CoverageMode.CHANGE_LOG
            or not policy.content_dedup_allowed
        ):
            raise CaptureContractError(
                f"{stream.value} is not a deduplicable broad change stream"
            )
        with self._lock:
            active = self._active_by_symbol.get(normalized)
            if active is not None:
                coordinator, _lease = active
                return coordinator.record_change(
                    stream=stream,
                    provider=provider,
                    payload=payload,
                    clocks=clocks,
                    symbol=normalized,
                    query=query,
                    broad=False,
                )
            if clocks.available_at > self._observed_now():
                raise CaptureContractError(
                    "shared pretrigger availability is later than trusted wall clock"
                )
            result = self.pretrigger_ring.retain_change_observation(
                identity=self.identity,
                stream=stream,
                provider=provider,
                payload=payload,
                clocks=clocks,
                symbol=normalized,
                change_key=change_key,
                query=query,
            )
            return _pretrigger_change_capture_result(result)

    def submit_hot_input(
        self,
        *,
        stream: CaptureStream,
        provider: str,
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
        symbol: str,
        query: Mapping[str, Any] | None = None,
    ) -> CaptureSubmission:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        with self._lock:
            active = self._active_by_symbol.get(normalized)
            if active is None:
                raise CaptureContractError("symbol is not globally hot")
            coordinator, _lease = active
            return coordinator._submit_supervised_hot_input(
                self._ownership_token,
                stream=stream,
                provider=provider,
                payload=payload,
                clocks=clocks,
                symbol=normalized,
                query=query,
            )

    @staticmethod
    def _batch_from_transfer(transfer: Any) -> PromotionBatch:
        return PromotionBatch(
            symbol=transfer.symbol,
            promoted_at=transfer.promoted_at,
            events=transfer.events,
            gaps=transfer.gaps,
            promotion_id=transfer.promotion_id,
            source_identity_sha256=transfer.source_identity_sha256,
            resource_binding_sha256=transfer.resource_binding_sha256,
            inventory_sha256=transfer.inventory_sha256,
            admission_handoff_sha256=transfer.admission_handoff_sha256,
        )

    def promote_hot_symbol(
        self,
        symbol: str,
        *,
        promoted_at: datetime | None = None,
        required_stream: CaptureStream = CaptureStream.L2_DEPTH_DELTA,
    ) -> HotPromotionResult:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        with self._lock:
            coordinator = self._coordinator_by_symbol.get(normalized)
            if coordinator is None:
                raise CaptureContractError(
                    "hot promotion requires an attached one-symbol coordinator"
                )
            existing = self._active_by_symbol.get(normalized)
            if existing is not None:
                return HotPromotionResult(normalized, existing[1], (), ())
            at = self._observed_now()
            if promoted_at is not None and _utc(promoted_at, "promoted_at") != at:
                raise CaptureContractError(
                    "supervisor promotion differs from trusted wall clock"
                )
            admission = self.hot_symbol_leases.acquire(
                normalized,
                requested_at=at,
                required_stream=required_stream,
            )
            if admission.gap is not None:
                coordinator._report_supervised_gap(
                    self._ownership_token,
                    admission.gap,
                )
                self._publish_hot_health_locked()
                return HotPromotionResult(
                    normalized,
                    None,
                    (),
                    (admission.gap,),
                )
            assert admission.lease is not None
            transfer = None
            try:
                transfer = self.pretrigger_ring.begin_promotion(
                    normalized,
                    promoted_at=at,
                    source_identity=self.identity,
                )
                batch = self._batch_from_transfer(transfer)
                result = coordinator._admit_supervised_promotion(
                    self._ownership_token,
                    lease=admission.lease,
                    batch=batch,
                    transfer=transfer,
                )
                if not result.hot:
                    self.pretrigger_ring.abort_promotion(transfer)
                    self.hot_symbol_leases.release(admission.lease)
                    self._publish_hot_health_locked()
                    return result
                committed = self.pretrigger_ring.commit_promotion(transfer)
                if committed != batch:
                    raise CaptureContractError(
                        "committed promotion differs from target-admitted inventory"
                    )
            except BaseException:
                if transfer is not None:
                    self.pretrigger_ring.abort_promotion(transfer)
                coordinator._release_supervised_hot_symbol(
                    self._ownership_token,
                    admission.lease,
                )
                self.hot_symbol_leases.release(admission.lease)
                coordinator._report_supervised_gap(
                    self._ownership_token,
                    CoverageGap(
                        stream=required_stream,
                        symbol=normalized,
                        reason="supervised_promotion_not_atomically_committed",
                        first_available_at=at,
                        last_available_at=at,
                        lost_count=1,
                    ),
                )
                self._publish_hot_health_locked()
                raise
            self._active_by_symbol[normalized] = (coordinator, admission.lease)
            self._publish_hot_health_locked()
            return result

    def release_hot_symbol(self, symbol: str) -> bool:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        with self._lock:
            active = self._active_by_symbol.get(normalized)
            if active is None:
                return False
            coordinator, lease = active
            # Validate both ownership views before freeing scarce global
            # capacity.  All supervised mutation is serialized by this lock,
            # so the following removal cannot race a second supervisor action.
            coordinator._validate_supervised_hot_symbol(
                self._ownership_token,
                lease,
            )
            released = self.hot_symbol_leases.release(lease)
            if not released:
                return False
            coordinator._release_supervised_hot_symbol(
                self._ownership_token,
                lease,
            )
            self._active_by_symbol.pop(normalized, None)
            self._publish_hot_health_locked()
            return True

    def health(self) -> Mapping[str, Any]:
        with self._lock:
            return {
                "network_fallback_allowed": False,
                "identity_sha256": self.identity.identity_sha256,
                "resource_hashes": self.resource_binding.hashes,
                "attached_symbols": tuple(sorted(self._coordinator_by_symbol)),
                "active_symbols": tuple(sorted(self._active_by_symbol)),
                "pretrigger": self.pretrigger_ring.health(),
                "hot": self.hot_symbol_leases.health(),
                "shared_admission": self.shared_admission_budget.health(),
            }


@dataclass(frozen=True)
class FirstDipFinalCaptureRead:
    """Exact already-durable read inventory for one final checkpoint."""

    dependency_profile: FSMDependencyProfile
    captured_reads: tuple[CapturedReadResult, ...]
    first_dip_tape_read_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.dependency_profile, FSMDependencyProfile):
            raise CaptureContractError(
                "first-dip final read dependency profile is malformed"
            )
        reads = tuple(self.captured_reads)
        if not reads or any(
            not isinstance(row, CapturedReadResult) or not row.durable
            for row in reads
        ):
            raise CaptureContractError(
                "first-dip final read inventory is not durable"
            )
        read_id = str(self.first_dip_tape_read_id or "").strip()
        receipts = tuple(row.receipt for row in reads)
        if (
            not read_id
            or any(receipt is None for receipt in receipts)
            or tuple(sorted(receipt.read_id for receipt in receipts if receipt))
            != self.dependency_profile.required_read_ids
        ):
            raise CaptureContractError(
                "first-dip final read inventory differs from its profile"
            )
        matching = tuple(
            row
            for row in reads
            if row.receipt is not None and row.receipt.read_id == read_id
        )
        if (
            len(matching) != 1
            or matching[0].first_dip_tape_evidence is None
        ):
            raise CaptureContractError(
                "first-dip final read lacks typed IQFeed tape evidence"
            )
        object.__setattr__(self, "captured_reads", reads)
        object.__setattr__(self, "first_dip_tape_read_id", read_id)


@dataclass(frozen=True)
class FirstDipFinalCaptureFrontier:
    """Persistable, non-authorizing digest of the final input frontier.

    The private runtime HMAC/token is intentionally absent.  This record can
    become replay evidence only after a sealed loader independently recomputes
    the exact prefix/read/continuity inventories from the content-addressed
    manifest.  A hash match alone never grants reservation or order authority.
    """

    run_id: str
    generation: int
    identity_sha256: str
    decision_id: str
    input_prefix_sequence: int
    input_prefix_root_sha256: str
    attested_available_at: datetime
    final_boundary_available_at: datetime
    expires_at: datetime
    dependency_profile_sha256: str
    dependency_profile_canonical_json: str
    required_read_ids: tuple[str, ...]
    read_evidence_inventory_sha256: str
    continuity_evidence_inventory_sha256: str
    first_dip_tape_read_id: str
    policy_sha256: str
    policy_canonical_json: str
    evaluation_sha256: str
    evaluation_canonical_json: str
    decision_receipt_binding_sha256: str
    prior_detector_reference_sha256: str
    prior_detector_reference_canonical_json: str
    adaptive_request_sha256: str
    adaptive_request_canonical_json: str
    opportunity_key_sha256: str
    schema_version: str = FIRST_DIP_FINAL_CAPTURE_FRONTIER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FIRST_DIP_FINAL_CAPTURE_FRONTIER_SCHEMA_VERSION:
            raise CaptureContractError(
                "first-dip final capture frontier schema is invalid"
            )
        run_id = str(self.run_id or "").strip()
        decision_id = str(self.decision_id or "").strip()
        if not run_id or not decision_id:
            raise CaptureContractError(
                "first-dip final capture frontier identity is missing"
            )
        if isinstance(self.generation, bool) or int(self.generation) <= 0:
            raise CaptureContractError(
                "first-dip final capture frontier generation is invalid"
            )
        if (
            isinstance(self.input_prefix_sequence, bool)
            or int(self.input_prefix_sequence) <= 0
        ):
            raise CaptureContractError(
                "first-dip final capture frontier sequence is invalid"
            )
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "decision_id", decision_id)
        object.__setattr__(self, "generation", int(self.generation))
        object.__setattr__(
            self, "input_prefix_sequence", int(self.input_prefix_sequence)
        )
        for name in (
            "identity_sha256",
            "input_prefix_root_sha256",
            "dependency_profile_sha256",
            "read_evidence_inventory_sha256",
            "continuity_evidence_inventory_sha256",
            "policy_sha256",
            "evaluation_sha256",
            "decision_receipt_binding_sha256",
            "prior_detector_reference_sha256",
            "adaptive_request_sha256",
            "opportunity_key_sha256",
        ):
            value = str(getattr(self, name) or "").strip().lower()
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise CaptureContractError(
                    f"first-dip final capture frontier {name} is invalid"
                )
            object.__setattr__(self, name, value)
        attested = _utc(
            self.attested_available_at,
            "first-dip final frontier attested_available_at",
        )
        final_boundary = _utc(
            self.final_boundary_available_at,
            "first-dip final frontier final_boundary_available_at",
        )
        expires = _utc(
            self.expires_at,
            "first-dip final frontier expires_at",
        )
        if expires <= attested or not (attested <= final_boundary <= expires):
            raise CaptureContractError(
                "first-dip final capture frontier decision clock is invalid"
            )
        object.__setattr__(self, "attested_available_at", attested)
        object.__setattr__(self, "final_boundary_available_at", final_boundary)
        object.__setattr__(self, "expires_at", expires)
        try:
            raw_profile = json.loads(self.dependency_profile_canonical_json)
            if not isinstance(raw_profile, Mapping):
                raise TypeError("dependency profile must be an object")
            profile = FSMDependencyProfile.from_dict(raw_profile)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CaptureContractError(
                "first-dip final capture frontier dependency profile is invalid"
            ) from exc
        canonical_profile = canonical_json_bytes(profile.to_dict()).decode("utf-8")
        if (
            canonical_profile != self.dependency_profile_canonical_json
            or profile.profile_sha256 != self.dependency_profile_sha256
        ):
            raise CaptureContractError(
                "first-dip final capture frontier dependency profile changed"
            )
        try:
            raw_policy = json.loads(self.policy_canonical_json)
            raw_evaluation = json.loads(self.evaluation_canonical_json)
            if not isinstance(raw_policy, Mapping) or not isinstance(
                raw_evaluation, Mapping
            ):
                raise TypeError("policy/evaluation must be objects")
            policy = FirstDipTapePolicy.from_dict(raw_policy)
            evaluation = FirstDipTapeEvaluation.from_dict(raw_evaluation)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CaptureContractError(
                "first-dip final capture frontier evaluation is invalid"
            ) from exc
        if (
            canonical_json_bytes(policy.to_dict()).decode("utf-8")
            != self.policy_canonical_json
            or canonical_json_bytes(evaluation.to_dict()).decode("utf-8")
            != self.evaluation_canonical_json
            or policy.policy_sha256 != self.policy_sha256
            or evaluation.policy_sha256 != policy.policy_sha256
            or evaluation.evaluation_sha256 != self.evaluation_sha256
            or evaluation.read_id != self.first_dip_tape_read_id
        ):
            raise CaptureContractError(
                "first-dip final capture frontier evaluation changed"
            )
        try:
            from .adaptive_risk_reservation import (  # noqa: PLC0415
                load_adaptive_risk_reservation_request,
            )
            from .first_dip_tape_decision import (  # noqa: PLC0415
                _FirstDipPriorDetectorReference,
            )

            raw_request = json.loads(self.adaptive_request_canonical_json)
            raw_prior = json.loads(
                self.prior_detector_reference_canonical_json
            )
            if not isinstance(raw_request, Mapping) or not isinstance(
                raw_prior, Mapping
            ):
                raise TypeError("request/prior reference must be objects")
            adaptive_request = load_adaptive_risk_reservation_request(
                raw_request
            )
            prior_reference = _FirstDipPriorDetectorReference.from_dict(
                raw_prior
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CaptureContractError(
                "first-dip final capture frontier request lineage is invalid"
            ) from exc
        if (
            canonical_json_bytes(adaptive_request.to_payload()).decode("utf-8")
            != self.adaptive_request_canonical_json
            or adaptive_request.request_sha256 != self.adaptive_request_sha256
            or adaptive_request.opportunity_key is None
            or adaptive_request.opportunity_key.key_sha256
            != self.opportunity_key_sha256
            or canonical_json_bytes(prior_reference.to_dict()).decode("utf-8")
            != self.prior_detector_reference_canonical_json
            or sha256_json(prior_reference.to_dict())
            != self.prior_detector_reference_sha256
            or prior_reference.opportunity_key_sha256
            != self.opportunity_key_sha256
            or prior_reference.policy_sha256 != self.policy_sha256
        ):
            raise CaptureContractError(
                "first-dip final capture frontier request lineage changed"
            )
        read_ids = tuple(sorted(str(value or "").strip() for value in self.required_read_ids))
        tape_read_id = str(self.first_dip_tape_read_id or "").strip()
        if (
            not read_ids
            or any(not value for value in read_ids)
            or len(read_ids) != len(set(read_ids))
            or tape_read_id not in read_ids
            or profile.required_read_ids != read_ids
        ):
            raise CaptureContractError(
                "first-dip final capture frontier read inventory is invalid"
            )
        object.__setattr__(self, "required_read_ids", read_ids)
        object.__setattr__(self, "first_dip_tape_read_id", tape_read_id)

    @classmethod
    def from_active_attestation(
        cls,
        attestation: ActiveCaptureInputPrefixAttestation,
        *,
        policy: FirstDipTapePolicy,
        evaluation: FirstDipTapeEvaluation,
        decision_receipt_binding_sha256: str,
        adaptive_request_payload: Mapping[str, Any],
        prior_detector_reference_payload: Mapping[str, Any],
        final_boundary_available_at: datetime,
    ) -> "FirstDipFinalCaptureFrontier":
        proof = verify_active_capture_input_attestation(attestation)
        final_boundary = _utc(
            final_boundary_available_at,
            "first-dip final frontier final_boundary_available_at",
        )
        if (
            proof.first_dip_tape_read_id is None
            or proof.first_dip_prior_detector_reference_sha256 is None
            or proof.first_dip_adaptive_request_sha256 is None
            or proof.first_dip_opportunity_key_sha256 is None
        ):
            raise CaptureContractError(
                "first-dip final capture frontier lacks complete lineage"
            )
        if (
            not isinstance(policy, FirstDipTapePolicy)
            or not isinstance(evaluation, FirstDipTapeEvaluation)
            or evaluation.policy_sha256 != policy.policy_sha256
            or evaluation.read_id != proof.first_dip_tape_read_id
            or not (
                proof.attested_available_at
                <= final_boundary
                <= proof.expires_at
            )
        ):
            raise CaptureContractError(
                "first-dip final capture frontier policy/evaluation mismatch"
            )
        try:
            from .adaptive_risk_reservation import (  # noqa: PLC0415
                load_adaptive_risk_reservation_request,
            )
            from .first_dip_tape_decision import (  # noqa: PLC0415
                _FirstDipPriorDetectorReference,
            )

            adaptive_request = load_adaptive_risk_reservation_request(
                adaptive_request_payload
            )
            prior_reference = _FirstDipPriorDetectorReference.from_dict(
                prior_detector_reference_payload
            )
        except (TypeError, ValueError) as exc:
            raise CaptureContractError(
                "first-dip final capture frontier request lineage is malformed"
            ) from exc
        if (
            adaptive_request.request_sha256
            != proof.first_dip_adaptive_request_sha256
            or adaptive_request.opportunity_key is None
            or adaptive_request.opportunity_key.key_sha256
            != proof.first_dip_opportunity_key_sha256
            or sha256_json(prior_reference.to_dict())
            != proof.first_dip_prior_detector_reference_sha256
            or prior_reference.opportunity_key_sha256
            != proof.first_dip_opportunity_key_sha256
            or prior_reference.policy_sha256 != policy.policy_sha256
        ):
            raise CaptureContractError(
                "first-dip final capture frontier request lineage mismatch"
            )
        return cls(
            run_id=proof.run_id,
            generation=proof.generation,
            identity_sha256=proof.identity_sha256,
            decision_id=proof.decision_id,
            input_prefix_sequence=proof.input_prefix_sequence,
            input_prefix_root_sha256=proof.input_prefix_root_sha256,
            attested_available_at=proof.attested_available_at,
            final_boundary_available_at=final_boundary,
            expires_at=proof.expires_at,
            dependency_profile_sha256=proof.dependency_profile.profile_sha256,
            dependency_profile_canonical_json=canonical_json_bytes(
                proof.dependency_profile.to_dict()
            ).decode("utf-8"),
            required_read_ids=proof.required_read_ids,
            read_evidence_inventory_sha256=(
                proof.read_evidence_inventory_sha256
            ),
            continuity_evidence_inventory_sha256=(
                proof.continuity_evidence_inventory_sha256
            ),
            first_dip_tape_read_id=proof.first_dip_tape_read_id,
            policy_sha256=policy.policy_sha256,
            policy_canonical_json=canonical_json_bytes(
                policy.to_dict()
            ).decode("utf-8"),
            evaluation_sha256=evaluation.evaluation_sha256,
            evaluation_canonical_json=canonical_json_bytes(
                evaluation.to_dict()
            ).decode("utf-8"),
            decision_receipt_binding_sha256=(
                decision_receipt_binding_sha256
            ),
            prior_detector_reference_sha256=(
                proof.first_dip_prior_detector_reference_sha256
            ),
            prior_detector_reference_canonical_json=canonical_json_bytes(
                prior_reference.to_dict()
            ).decode("utf-8"),
            adaptive_request_sha256=proof.first_dip_adaptive_request_sha256,
            adaptive_request_canonical_json=canonical_json_bytes(
                adaptive_request.to_payload()
            ).decode("utf-8"),
            opportunity_key_sha256=proof.first_dip_opportunity_key_sha256,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "generation": self.generation,
            "identity_sha256": self.identity_sha256,
            "decision_id": self.decision_id,
            "input_prefix_sequence": self.input_prefix_sequence,
            "input_prefix_root_sha256": self.input_prefix_root_sha256,
            "attested_available_at": self.attested_available_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "final_boundary_available_at": (
                self.final_boundary_available_at.isoformat().replace("+00:00", "Z")
            ),
            "expires_at": self.expires_at.isoformat().replace("+00:00", "Z"),
            "dependency_profile_sha256": self.dependency_profile_sha256,
            "dependency_profile_canonical_json": (
                self.dependency_profile_canonical_json
            ),
            "required_read_ids": list(self.required_read_ids),
            "read_evidence_inventory_sha256": (
                self.read_evidence_inventory_sha256
            ),
            "continuity_evidence_inventory_sha256": (
                self.continuity_evidence_inventory_sha256
            ),
            "first_dip_tape_read_id": self.first_dip_tape_read_id,
            "policy_sha256": self.policy_sha256,
            "policy_canonical_json": self.policy_canonical_json,
            "evaluation_sha256": self.evaluation_sha256,
            "evaluation_canonical_json": self.evaluation_canonical_json,
            "decision_receipt_binding_sha256": (
                self.decision_receipt_binding_sha256
            ),
            "prior_detector_reference_sha256": (
                self.prior_detector_reference_sha256
            ),
            "prior_detector_reference_canonical_json": (
                self.prior_detector_reference_canonical_json
            ),
            "adaptive_request_sha256": self.adaptive_request_sha256,
            "adaptive_request_canonical_json": (
                self.adaptive_request_canonical_json
            ),
            "opportunity_key_sha256": self.opportunity_key_sha256,
        }

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any],
    ) -> "FirstDipFinalCaptureFrontier":
        expected = {
            "schema_version",
            "run_id",
            "generation",
            "identity_sha256",
            "decision_id",
            "input_prefix_sequence",
            "input_prefix_root_sha256",
            "attested_available_at",
            "final_boundary_available_at",
            "expires_at",
            "dependency_profile_sha256",
            "dependency_profile_canonical_json",
            "required_read_ids",
            "read_evidence_inventory_sha256",
            "continuity_evidence_inventory_sha256",
            "first_dip_tape_read_id",
            "policy_sha256",
            "policy_canonical_json",
            "evaluation_sha256",
            "evaluation_canonical_json",
            "decision_receipt_binding_sha256",
            "prior_detector_reference_sha256",
            "prior_detector_reference_canonical_json",
            "adaptive_request_sha256",
            "adaptive_request_canonical_json",
            "opportunity_key_sha256",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise CaptureContractError(
                "first-dip final capture frontier fields are invalid"
            )
        try:
            if not isinstance(raw["required_read_ids"], (list, tuple)):
                raise TypeError("required_read_ids must be an array")
            attested = datetime.fromisoformat(
                str(raw["attested_available_at"]).replace("Z", "+00:00")
            )
            final_boundary = datetime.fromisoformat(
                str(raw["final_boundary_available_at"]).replace("Z", "+00:00")
            )
            expires = datetime.fromisoformat(
                str(raw["expires_at"]).replace("Z", "+00:00")
            )
            read_ids = tuple(str(value) for value in raw["required_read_ids"])
        except (TypeError, ValueError) as exc:
            raise CaptureContractError(
                "first-dip final capture frontier encoding is invalid"
            ) from exc
        return cls(
            schema_version=str(raw["schema_version"]),
            run_id=str(raw["run_id"]),
            generation=raw["generation"],
            identity_sha256=str(raw["identity_sha256"]),
            decision_id=str(raw["decision_id"]),
            input_prefix_sequence=raw["input_prefix_sequence"],
            input_prefix_root_sha256=str(raw["input_prefix_root_sha256"]),
            attested_available_at=attested,
            final_boundary_available_at=final_boundary,
            expires_at=expires,
            dependency_profile_sha256=str(raw["dependency_profile_sha256"]),
            dependency_profile_canonical_json=str(
                raw["dependency_profile_canonical_json"]
            ),
            required_read_ids=read_ids,
            read_evidence_inventory_sha256=str(
                raw["read_evidence_inventory_sha256"]
            ),
            continuity_evidence_inventory_sha256=str(
                raw["continuity_evidence_inventory_sha256"]
            ),
            first_dip_tape_read_id=str(raw["first_dip_tape_read_id"]),
            policy_sha256=str(raw["policy_sha256"]),
            policy_canonical_json=str(raw["policy_canonical_json"]),
            evaluation_sha256=str(raw["evaluation_sha256"]),
            evaluation_canonical_json=str(raw["evaluation_canonical_json"]),
            decision_receipt_binding_sha256=str(
                raw["decision_receipt_binding_sha256"]
            ),
            prior_detector_reference_sha256=str(
                raw["prior_detector_reference_sha256"]
            ),
            prior_detector_reference_canonical_json=str(
                raw["prior_detector_reference_canonical_json"]
            ),
            adaptive_request_sha256=str(raw["adaptive_request_sha256"]),
            adaptive_request_canonical_json=str(
                raw["adaptive_request_canonical_json"]
            ),
            opportunity_key_sha256=str(raw["opportunity_key_sha256"]),
        )

    @property
    def frontier_sha256(self) -> str:
        return sha256_json(self.to_dict())


@runtime_checkable
class FirstDipFinalReadProvider(Protocol):
    """Local producer of a fresh durable read; it is not a data fetch seam."""

    def __call__(
        self,
        *,
        adaptive_request: object,
        detector_policy: FirstDipTapePolicy,
        final_boundary_available_at: datetime,
    ) -> FirstDipFinalCaptureRead: ...


class LiveMicrostructureCaptureBridge:
    """Capture-native provider for the pipeline's exact IQFeed read windows.

    The bridge never selects source hashes and never reads a provider, ring, or
    database.  The capture lifecycle inventories its own durable source index,
    commits the receipt atomically, and returns immutable events for the same
    pure computation used by the live FSM.  L2 operations remain fail-closed
    until provider snapshot-completion semantics are proven; exact IQFeed print
    operations are fully supported here.
    """

    _PRINT_OPERATIONS = frozenset(
        {
            CaptureMicrostructureOperation.TRADE_FLOW,
            CaptureMicrostructureOperation.REALIZED_VOL,
            CaptureMicrostructureOperation.FLOW_SLOPE,
        }
    )

    def __init__(
        self,
        *,
        coordinator: LiveReplayCaptureCoordinator,
        decision_id: str,
    ) -> None:
        if (
            not isinstance(coordinator, LiveReplayCaptureCoordinator)
            or coordinator.state is not CaptureSessionState.RUNNING
        ):
            raise CaptureContractError(
                "microstructure bridge requires a running coordinator"
            )
        normalized_decision = str(decision_id or "").strip()
        if not normalized_decision:
            raise CaptureContractError(
                "microstructure bridge decision id is required"
            )
        self.coordinator = coordinator
        self.decision_id = normalized_decision
        self.symbol = coordinator.certification_symbol
        self._lock = threading.RLock()
        self._install_started = False
        self._install_finished = False
        self._captured_reads: list[CapturedReadResult] = []
        self._capture_failure_reason: str | None = None

    @property
    def network_fallback_allowed(self) -> bool:
        return False

    @property
    def captured_reads(self) -> tuple[CapturedReadResult, ...]:
        with self._lock:
            return tuple(self._captured_reads)

    @property
    def capture_failure_reason(self) -> str | None:
        """Return the first durable-capture failure observed in this scope.

        Legacy feature callers may deliberately treat a missing microstructure
        input as a non-signal.  The capture owner still needs a sticky record so
        the enclosing PAPER capability can reject the whole decision before it
        is dispatched rather than silently accepting an unrecorded read.
        """

        with self._lock:
            return self._capture_failure_reason

    def _assert_installed(self) -> None:
        with self._lock:
            if not self._install_started or self._install_finished:
                raise CaptureContractError(
                    "microstructure callback is outside its installed runner scope"
                )

    def _gap(self, reason: str, at: datetime, *, stream: CaptureStream) -> None:
        normalized_reason = str(reason or "microstructure_capture_unavailable")
        with self._lock:
            if self._capture_failure_reason is None:
                self._capture_failure_reason = normalized_reason
        self.coordinator.record_coverage_gap(
            CoverageGap(
                stream=stream,
                symbol=self.symbol,
                reason=normalized_reason,
                first_available_at=at,
                last_available_at=at,
                lost_count=1,
            )
        )

    @staticmethod
    def _print_rows(
        source_events: Sequence[CaptureEvent],
    ) -> tuple[tuple[float, float, float | None, float | None, datetime], ...]:
        rows = []
        for event in source_events:
            parsed = CaptureIqfeedPrint.from_event(event)
            provider_at = event.clocks.provider_event_at
            if provider_at is None:
                raise CaptureContractError(
                    "microstructure print lacks an exact provider clock"
                )
            rows.append(
                (
                    parsed.price,
                    parsed.size,
                    parsed.bid,
                    parsed.ask,
                    provider_at,
                )
            )
        return tuple(rows)

    @staticmethod
    def _compute_print_result(
        operation: CaptureMicrostructureOperation,
        rows: Sequence[tuple[float, float, float | None, float | None, datetime]],
        *,
        decision_at: datetime,
        parameters: Mapping[str, Any],
    ) -> Any:
        from . import pipeline as _pipeline  # noqa: PLC0415
        from .paper_execution import (  # noqa: PLC0415
            denoised_rv_ewma,
            ofi_level_and_slope,
            roll_effective_spread_pct,
        )

        if operation is CaptureMicrostructureOperation.TRADE_FLOW:
            return _pipeline._aggressor_imbalance(
                tuple(row[:4] for row in rows)
            )

        grid_seconds = float(parameters["grid_seconds"])
        if operation is CaptureMicrostructureOperation.REALIZED_VOL:
            returns, tick_rate, debug = _pipeline._event_grid_log_returns(
                tuple((row[0], row[4]) for row in rows),
                grid_secs=grid_seconds,
            )
            if len(returns) < 2:
                return None
            window_seconds = float(parameters["window_seconds"])
            half_life = max(
                2.0,
                (window_seconds / max(grid_seconds, 1e-9)) / 2.0,
            )
            rv_step = denoised_rv_ewma(returns, half_life=half_life)
            if rv_step is None:
                return None
            return {
                "rv_step": float(rv_step),
                "tick_rate": float(tick_rate),
                "eff_spread_pct": roll_effective_spread_pct(returns),
                "grid_secs": grid_seconds,
                "n_ticks": int(debug.get("n_ticks", 0)),
                "n_grid": int(debug.get("n_grid", 0)),
            }

        if operation is CaptureMicrostructureOperation.FLOW_SLOPE:
            if rows and (
                decision_at - rows[-1][4]
            ).total_seconds() > grid_seconds:
                return None
            levels, tick_rate, debug = _pipeline._event_grid_aggressor_flow(
                rows,
                grid_secs=grid_seconds,
            )
            if len(levels) < 2:
                return None
            level, slope = ofi_level_and_slope(
                levels,
                half_life=float(parameters["half_life_steps"]),
            )
            if level is None:
                return None
            last_price = rows[-1][0] if rows else None
            bid = rows[-1][2] if rows else None
            ask = rows[-1][3] if rows else None
            mid = (
                (float(bid) + float(ask)) / 2.0
                if bid is not None
                and ask is not None
                and float(ask) > float(bid) > 0.0
                else None
            )
            return {
                "ofi_level": float(level),
                "ofi_slope": float(slope) if slope is not None else None,
                "tick_rate": float(tick_rate),
                "last_price": float(last_price) if last_price is not None else None,
                "mid": mid,
                "grid_secs": grid_seconds,
                "n_ticks": int(debug.get("n_ticks", 0)),
                "n_grid": int(debug.get("n_grid", 0)),
            }
        raise CaptureContractError(
            "microstructure print operation is unsupported"
        )

    def read_microstructure(
        self,
        *,
        operation: CaptureMicrostructureOperation,
        symbol: str,
        decision_at: datetime,
        parameters: Mapping[str, Any],
    ) -> Any:
        self._assert_installed()
        normalized_symbol = _normalized_symbol(symbol, required=True)
        decision = _utc(decision_at, "microstructure decision_at")
        capture_available = self.coordinator._observed_now()
        if normalized_symbol != self.symbol or capture_available < decision:
            self._gap(
                "microstructure_identity_or_clock_mismatch",
                capture_available,
                stream=(
                    CaptureStream.IQFEED_PRINT
                    if operation in self._PRINT_OPERATIONS
                    else CaptureStream.L2_DEPTH_CHECKPOINT
                ),
            )
            raise ReplayMicrostructureInputUnavailableError(
                "microstructure identity/clock mismatch"
            )
        if operation not in self._PRINT_OPERATIONS:
            self._gap(
                "iqfeed_l2_provider_snapshot_completion_unavailable",
                capture_available,
                stream=CaptureStream.L2_DEPTH_CHECKPOINT,
            )
            raise ReplayDecisionLocalMicrostructureCoverageUnavailableError(
                "IQFeed L2 provider snapshot completion is unavailable"
            )
        window_seconds = float(parameters.get("window_seconds") or 0.0)
        if not math.isfinite(window_seconds) or window_seconds <= 0.0:
            self._gap(
                "microstructure_window_parameters_invalid",
                capture_available,
                stream=CaptureStream.IQFEED_PRINT,
            )
            raise ReplayMicrostructureInputUnavailableError(
                "microstructure window parameters are invalid"
            )
        captured = self.coordinator.capture_complete_microstructure_window(
            decision_id=self.decision_id,
            operation=operation,
            stream=CaptureStream.IQFEED_PRINT,
            provider="iqfeed",
            symbol=self.symbol,
            requested_at=decision,
            returned_at=capture_available,
            event_start_exclusive=decision - timedelta(seconds=window_seconds),
            event_end_inclusive=decision,
            parameters=parameters,
        )
        if not captured.durable or captured.receipt is None:
            raise ReplayMicrostructureInputUnavailableError(
                "exact IQFeed print window is unavailable"
            )
        try:
            query = CaptureMicrostructureReadQuery.from_dict(
                captured.receipt.query or {}
            )
            if (
                query.operation is not operation
                or query.symbol != self.symbol
                or dict(query.parameters) != dict(parameters)
            ):
                raise CaptureContractError(
                    "microstructure receipt differs from provider request"
                )
            rows = self._print_rows(captured.source_events)
            result = self._compute_print_result(
                operation,
                rows,
                decision_at=decision,
                parameters=query.parameters,
            )
        except (CaptureContractError, KeyError, TypeError, ValueError) as exc:
            self._gap(
                "microstructure_typed_result_rejected",
                capture_available,
                stream=CaptureStream.IQFEED_PRINT,
            )
            raise ReplayMicrostructureInputUnavailableError(
                "microstructure typed result could not be reproduced"
            ) from exc
        with self._lock:
            self._captured_reads.append(captured)
        return result

    @contextmanager
    def install(self) -> Iterator["LiveMicrostructureCaptureBridge"]:
        from .pipeline import microstructure_read_provider  # noqa: PLC0415

        with self._lock:
            if self._install_started or self._install_finished:
                raise CaptureContractError(
                    "microstructure bridge install is one-shot"
                )
            self._install_started = True
        try:
            with microstructure_read_provider(self):
                yield self
        finally:
            with self._lock:
                self._install_finished = True


class LiveOhlcvCaptureBridge:
    """Receipt every OHLCV frame already returned to one captured FSM tick.

    The bridge has no provider callback and never fetches.  It binds the exact
    runner call, final provider/frame provenance, dual clocks, and immutable
    rows to one decision receipt.  Missing, ambiguous, stale-cache-provenance,
    or non-durable results create an explicit coverage gap and reject the read.
    """

    def __init__(
        self,
        *,
        coordinator: LiveReplayCaptureCoordinator,
        decision_id: str,
        macro_cache: dict,
    ) -> None:
        if (
            not isinstance(coordinator, LiveReplayCaptureCoordinator)
            or coordinator.state is not CaptureSessionState.RUNNING
        ):
            raise CaptureContractError(
                "OHLCV capture bridge requires a running coordinator"
            )
        normalized_decision = str(decision_id or "").strip()
        if not normalized_decision:
            raise CaptureContractError(
                "OHLCV capture bridge decision id is required"
            )
        if not isinstance(macro_cache, dict):
            raise CaptureContractError(
                "OHLCV capture bridge macro cache is malformed"
            )
        self.coordinator = coordinator
        self.decision_id = normalized_decision
        self.macro_cache = macro_cache
        self._lock = threading.Lock()
        self._install_started = False
        self._install_finished = False
        self._captured_reads: list[CapturedReadResult] = []
        self._capture_failure_reason: str | None = None

    @property
    def network_fallback_allowed(self) -> bool:
        return False

    @property
    def captured_reads(self) -> tuple[CapturedReadResult, ...]:
        with self._lock:
            return tuple(self._captured_reads)

    @property
    def capture_failure_reason(self) -> str | None:
        """Return the first OHLCV capture failure, even if a caller swallowed it."""

        with self._lock:
            return self._capture_failure_reason

    def _assert_installed(self) -> None:
        with self._lock:
            if not self._install_started or self._install_finished:
                raise CaptureContractError(
                    "OHLCV capture callback is outside its installed runner scope"
                )

    def _gap(self, reason: str, at: datetime, *, symbol: str) -> bool:
        normalized_reason = str(reason or "provider_ohlcv_capture_unavailable")
        with self._lock:
            if self._capture_failure_reason is None:
                self._capture_failure_reason = normalized_reason
        return self.coordinator.record_coverage_gap(
            CoverageGap(
                stream=CaptureStream.PROVIDER_OHLCV,
                symbol=_normalized_symbol(symbol, required=True),
                reason=normalized_reason,
                first_available_at=_utc(at, "OHLCV gap time"),
                last_available_at=_utc(at, "OHLCV gap time"),
                lost_count=1,
            )
        )

    @staticmethod
    def _finite(value: Any, field_name: str, *, nonnegative: bool = False) -> float:
        if isinstance(value, bool):
            raise CaptureContractError(f"{field_name} is not a numeric market value")
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise CaptureContractError(f"{field_name} is not numeric") from exc
        if not math.isfinite(parsed) or (nonnegative and parsed < 0.0):
            raise CaptureContractError(f"{field_name} is outside its finite domain")
        return parsed

    @staticmethod
    def _source_received_at(frame: Any) -> datetime:
        attrs = getattr(frame, "attrs", None)
        raw = attrs.get("fetched_at_utc") if isinstance(attrs, Mapping) else None
        if not isinstance(raw, str) or not raw.strip():
            raise CaptureContractError(
                "OHLCV frame source fetched_at provenance is missing"
            )
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise CaptureContractError(
                "OHLCV frame source fetched_at provenance is malformed"
            ) from exc
        return _utc(parsed, "OHLCV source fetched_at")

    def on_ohlcv_failure(
        self,
        *,
        ticker: str,
        interval: str,
        period: str,
        requested_at: datetime,
        failed_at: datetime,
        allow_provider_fallback: bool,
        error: BaseException,
    ) -> bool:
        """Record the missing exact query result before the runner re-raises."""

        self._assert_installed()
        del interval, period, requested_at, allow_provider_fallback, error
        return self._gap(
            "provider_ohlcv_fetch_failed",
            failed_at,
            symbol=ticker,
        )

    def on_ohlcv_result(
        self,
        *,
        ticker: str,
        interval: str,
        period: str,
        requested_at: datetime,
        returned_at: datetime,
        allow_provider_fallback: bool,
        frame: Any,
    ) -> bool:
        """Persist one exact provider frame and the read that consumed it."""

        self._assert_installed()
        symbol = _normalized_symbol(ticker, required=True)
        assert symbol is not None
        requested = _utc(requested_at, "OHLCV requested_at")
        provider_returned = _utc(returned_at, "OHLCV returned_at")
        if provider_returned < requested:
            self._gap(
                "provider_ohlcv_query_clock_reversed",
                provider_returned,
                symbol=symbol,
            )
            return False
        capture_available = self.coordinator._observed_now()
        if capture_available < provider_returned:
            self._gap(
                "provider_ohlcv_capture_clock_precedes_return",
                provider_returned,
                symbol=symbol,
            )
            return False
        try:
            if type(allow_provider_fallback) is not bool:
                raise CaptureContractError(
                    "OHLCV fallback policy provenance is malformed"
                )
            if frame is None or not hasattr(frame, "iterrows") or len(frame) <= 0:
                raise CaptureContractError("OHLCV result frame is empty")
            attrs = getattr(frame, "attrs", None)
            if not isinstance(attrs, Mapping):
                raise CaptureContractError("OHLCV result provenance is missing")
            provider = str(attrs.get("provider") or "").strip().lower()
            if not provider:
                raise CaptureContractError("OHLCV resolved provider is missing")
            if str(attrs.get("ticker") or "").strip().upper() != symbol:
                raise CaptureContractError("OHLCV frame ticker provenance mismatch")
            normalized_interval = str(interval or "").strip()
            normalized_period = str(period or "").strip()
            if (
                not normalized_interval
                or not normalized_period
                or str(attrs.get("interval") or "").strip() != normalized_interval
            ):
                raise CaptureContractError("OHLCV frame call provenance mismatch")
            if attrs.get("integrity_ok") is not True:
                raise CaptureContractError("OHLCV integrity provenance is not clean")
            cache_hit = attrs.get("cache_hit")
            if type(cache_hit) is not bool:
                raise CaptureContractError("OHLCV cache-hit provenance is missing")
            cache_age = self._finite(
                attrs.get("cache_age_seconds"),
                "OHLCV cache age",
                nonnegative=True,
            )
            source_received_at = self._source_received_at(frame)
            if source_received_at > provider_returned:
                raise CaptureContractError(
                    "OHLCV source fetch clock exceeds provider return"
                )
            required_columns = {"Open", "High", "Low", "Close", "Volume"}
            if not required_columns.issubset(set(getattr(frame, "columns", ()))):
                raise CaptureContractError("OHLCV frame columns are incomplete")
            rows: list[dict[str, Any]] = []
            previous_at: datetime | None = None
            for index_value, row in frame.iterrows():
                timestamp = pd.Timestamp(index_value)
                if timestamp.tzinfo is None:
                    raise CaptureContractError(
                        "OHLCV market reference timezone is ambiguous"
                    )
                market_at = timestamp.to_pydatetime().astimezone(UTC)
                if previous_at is not None and market_at <= previous_at:
                    raise CaptureContractError(
                        "OHLCV market references are not strictly increasing"
                    )
                open_px = self._finite(row["Open"], "OHLCV open")
                high_px = self._finite(row["High"], "OHLCV high")
                low_px = self._finite(row["Low"], "OHLCV low")
                close_px = self._finite(row["Close"], "OHLCV close")
                volume = self._finite(
                    row["Volume"], "OHLCV volume", nonnegative=True
                )
                if (
                    min(open_px, high_px, low_px, close_px) <= 0.0
                    or high_px < max(open_px, close_px, low_px)
                    or low_px > min(open_px, close_px, high_px)
                ):
                    raise CaptureContractError("OHLCV price bounds are invalid")
                rows.append(
                    {
                        "market_reference_at": _iso(market_at),
                        "open": open_px,
                        "high": high_px,
                        "low": low_px,
                        "close": close_px,
                        "volume": volume,
                    }
                )
                previous_at = market_at
            if not rows or previous_at is None:
                raise CaptureContractError("OHLCV result rows are missing")
            query = {
                "schema_version": PROVIDER_OHLCV_QUERY_SCHEMA_VERSION,
                "call": {
                    "symbol": symbol,
                    "interval": normalized_interval,
                    "period": normalized_period,
                },
                "provider_parameters": {
                    "allow_provider_fallback": allow_provider_fallback,
                    "resolved_provider": provider,
                    "cache_hit": cache_hit,
                    "cache_age_seconds": cache_age,
                    "source_fetched_at_utc": _iso(source_received_at),
                    "integrity_ok": True,
                },
            }
            payload = {
                "schema_version": PROVIDER_OHLCV_PAYLOAD_SCHEMA_VERSION,
                "query_sha256": sha256_json(query),
                "rows": rows,
            }
            captured = self.coordinator.capture_query_result(
                decision_id=self.decision_id,
                stream=CaptureStream.PROVIDER_OHLCV,
                provider=provider,
                query=query,
                requested_at=requested,
                returned_at=capture_available,
                results=(
                    ObservedCaptureInput(
                        payload=payload,
                        clocks=CaptureClocks(
                            market_reference_at=previous_at,
                            received_at=source_received_at,
                            available_at=capture_available,
                        ),
                    ),
                ),
                symbol=symbol,
            )
        except (CaptureContractError, KeyError, TypeError, ValueError, OverflowError):
            self._gap(
                "provider_ohlcv_typed_capture_rejected",
                capture_available,
                symbol=symbol,
            )
            return False
        if not captured.durable:
            return False
        with self._lock:
            self._captured_reads.append(captured)
        return True

    @contextmanager
    def install(self) -> Iterator["LiveOhlcvCaptureBridge"]:
        """Install capture plus a run-local macro cache for one FSM invocation."""

        from . import live_runner as _live_runner  # noqa: PLC0415
        from .entry_features import macro_feature_cache  # noqa: PLC0415

        with self._lock:
            if self._install_started or self._install_finished:
                raise CaptureContractError(
                    "OHLCV capture bridge install is one-shot"
                )
            self._install_started = True
        try:
            with _live_runner.live_ohlcv_capture_sink(self), macro_feature_cache(
                self.macro_cache
            ):
                yield self
        finally:
            with self._lock:
                self._install_finished = True


class LiveScannerSnapshotCaptureBridge:
    """Capture the exact Massive snapshot projection consumed by one FSM tick.

    The bridge observes the result already returned by ``massive_client``; it
    never fetches.  A capture failure makes that provider read fail closed and
    writes an explicit coverage gap, so current data cannot silently influence
    a supposedly replayable decision.
    """

    def __init__(
        self,
        *,
        coordinator: LiveReplayCaptureCoordinator,
        decision_id: str,
        profile: CaptureScannerProfile,
        include_otc: bool = False,
    ) -> None:
        if (
            not isinstance(coordinator, LiveReplayCaptureCoordinator)
            or coordinator.state is not CaptureSessionState.RUNNING
        ):
            raise CaptureContractError(
                "scanner capture bridge requires a running coordinator"
            )
        normalized_decision = str(decision_id or "").strip()
        if not normalized_decision:
            raise CaptureContractError(
                "scanner capture bridge decision id is required"
            )
        if not isinstance(profile, CaptureScannerProfile):
            raise CaptureContractError(
                "scanner capture bridge profile is malformed"
            )
        if type(include_otc) is not bool:
            raise CaptureContractError(
                "scanner capture bridge include_otc must be boolean"
            )
        if profile.asset_class != "equity":
            raise CaptureContractError(
                "scanner capture bridge requires an equity profile"
            )
        self.coordinator = coordinator
        self.decision_id = normalized_decision
        self.profile = profile
        self.include_otc = include_otc
        self.symbol = coordinator.certification_symbol
        self._lock = threading.Lock()
        self._install_started = False
        self._install_finished = False
        self._captured_reads: list[CapturedReadResult] = []
        self._capture_failure_reason: str | None = None

    @property
    def network_fallback_allowed(self) -> bool:
        return False

    @property
    def captured_reads(self) -> tuple[CapturedReadResult, ...]:
        with self._lock:
            return tuple(self._captured_reads)

    @property
    def capture_failure_reason(self) -> str | None:
        """Return the first scanner capture failure in this runner scope."""

        with self._lock:
            return self._capture_failure_reason

    def _gap(self, reason: str, at: datetime) -> bool:
        normalized_reason = str(reason or "scanner_snapshot_capture_unavailable")
        with self._lock:
            if self._capture_failure_reason is None:
                self._capture_failure_reason = normalized_reason
        return self.coordinator.record_coverage_gap(
            CoverageGap(
                stream=CaptureStream.SCANNER_SNAPSHOT,
                symbol=self.symbol,
                reason=normalized_reason,
                first_available_at=at,
                last_available_at=at,
                lost_count=1,
            )
        )

    def on_massive_full_snapshot(
        self,
        *,
        include_otc: bool,
        max_age_seconds: float | None,
        provider_cache_ttl_seconds: float,
        requested_at: datetime,
        returned_at: datetime,
        cache_hit: bool,
        cache_age_seconds: float | None,
        rows: list[dict[str, Any]],
    ) -> bool:
        """Persist one symbol projection and its exact decision read receipt."""

        with self._lock:
            if not self._install_started or self._install_finished:
                raise CaptureContractError(
                    "scanner capture callback is outside its installed runner scope"
                )
        del cache_hit, cache_age_seconds  # query TTL/result bytes are authoritative
        requested = _utc(requested_at, "scanner requested_at")
        provider_returned = _utc(returned_at, "scanner returned_at")
        if provider_returned < requested:
            self._gap("scanner_snapshot_query_clock_reversed", provider_returned)
            return False
        capture_available = self.coordinator._observed_now()
        if capture_available < provider_returned:
            self._gap(
                "scanner_snapshot_capture_clock_precedes_provider_return",
                provider_returned,
            )
            return False
        if (
            type(include_otc) is not bool
            or include_otc != self.include_otc
            or max_age_seconds != self.profile.snapshot_max_age_seconds
        ):
            self._gap(
                "scanner_snapshot_query_parameters_changed",
                capture_available,
            )
            return False
        try:
            query = CaptureScannerSnapshotQuery(
                symbol=self.symbol,
                include_otc=include_otc,
                max_age_seconds=float(max_age_seconds),
                provider_cache_ttl_seconds=provider_cache_ttl_seconds,
                profile=self.profile,
                profile_sha256=self.profile.profile_sha256,
                config_sha256=self.coordinator.identity.config_sha256,
            )
        except (CaptureContractError, TypeError, ValueError):
            self._gap(
                "scanner_snapshot_query_contract_mismatch",
                capture_available,
            )
            return False
        matches = tuple(
            row
            for row in rows
            if isinstance(row, Mapping)
            and str(row.get("ticker") or "").strip().upper() == self.symbol
        )
        if len(matches) != 1:
            self._gap(
                "scanner_snapshot_symbol_projection_unavailable",
                capture_available,
            )
            return False
        raw = matches[0]
        last_trade = raw.get("lastTrade")
        day = raw.get("day")
        minute = raw.get("min")
        source_projection = {
            "ticker": self.symbol,
            "todaysChangePerc": raw.get("todaysChangePerc"),
            "updated": raw.get("updated"),
            "lastTrade": {
                "p": (
                    last_trade.get("p")
                    if isinstance(last_trade, Mapping)
                    else None
                ),
                "t": (
                    last_trade.get("t")
                    if isinstance(last_trade, Mapping)
                    else None
                ),
            },
            "day": {
                key: day.get(key) if isinstance(day, Mapping) else None
                for key in ("c", "vw", "v")
            },
            "min": {
                key: minute.get(key) if isinstance(minute, Mapping) else None
                for key in ("c", "av")
            },
        }
        try:
            market_reference_at = scanner_snapshot_market_reference_at(
                source_projection,
                symbol=self.symbol,
            )
            payload = build_scanner_snapshot_payload(
                query,
                market_reference_at=market_reference_at,
                source_projection=source_projection,
            )
            change = self.coordinator.record_change(
                stream=CaptureStream.SCANNER_SNAPSHOT,
                provider=SCANNER_SNAPSHOT_PROVIDER,
                payload=payload,
                clocks=CaptureClocks(
                    market_reference_at=market_reference_at,
                    received_at=provider_returned,
                    available_at=capture_available,
                ),
                symbol=self.symbol,
                query=query.to_dict(),
                broad=False,
            )
            source_event = change.current_event
            if change.coverage_gap_recorded or source_event is None:
                return False
            captured = self.coordinator.capture_durable_read(
                decision_id=self.decision_id,
                stream=CaptureStream.SCANNER_SNAPSHOT,
                provider=SCANNER_SNAPSHOT_PROVIDER,
                query=query.to_dict(),
                requested_at=requested,
                returned_at=capture_available,
                source_events=(source_event,),
                symbol=self.symbol,
            )
        except (CaptureContractError, TypeError, ValueError):
            self._gap(
                "scanner_snapshot_typed_capture_rejected",
                capture_available,
            )
            return False
        if not captured.durable:
            return False
        with self._lock:
            self._captured_reads.append(captured)
        return True

    @contextmanager
    def install(self) -> Iterator["LiveScannerSnapshotCaptureBridge"]:
        """Install this capture observer for exactly one runner invocation."""

        from ...massive_client import (  # noqa: PLC0415
            massive_full_snapshot_capture_sink,
        )

        with self._lock:
            if self._install_started or self._install_finished:
                raise CaptureContractError(
                    "scanner capture bridge install is one-shot"
                )
            self._install_started = True
        try:
            with massive_full_snapshot_capture_sink(self):
                yield self
        finally:
            with self._lock:
                self._install_finished = True


class LiveFirstDipAdaptiveCaptureBridge:
    """One-shot detector→risk request→fresh-final capture scope.

    This bridge installs only process-local capabilities.  It does not start a
    coordinator, query a provider/DB, reserve risk, or call a broker.  The
    caller supplies one source built from the detector prefix and a callback
    that receipts an already-durable final tape inventory.  Every step is then
    independently rebound by the coordinator before the runner can reach its
    normal atomic risk reservation boundary.
    """

    def __init__(
        self,
        *,
        coordinator: LiveReplayCaptureCoordinator,
        detector_attestation: ActiveCaptureInputPrefixAttestation,
        detector_policy: FirstDipTapePolicy,
        adaptive_source: object,
        final_read_provider: FirstDipFinalReadProvider,
    ) -> None:
        from .adaptive_risk_request_builder import (  # noqa: PLC0415
            AdaptiveRiskBuilderSource,
            adaptive_risk_capture_binding_from_active_attestation,
        )

        if (
            not isinstance(coordinator, LiveReplayCaptureCoordinator)
            or coordinator.state is not CaptureSessionState.RUNNING
        ):
            raise CaptureContractError(
                "first-dip adaptive bridge requires a running coordinator"
            )
        proof = verify_active_capture_input_attestation(
            detector_attestation
        )
        if not isinstance(detector_policy, FirstDipTapePolicy):
            raise CaptureContractError(
                "first-dip adaptive bridge policy is malformed"
            )
        if not isinstance(adaptive_source, AdaptiveRiskBuilderSource):
            raise CaptureContractError(
                "first-dip adaptive bridge source is not typed"
            )
        if not isinstance(final_read_provider, FirstDipFinalReadProvider):
            raise CaptureContractError(
                "first-dip adaptive bridge final-read provider is malformed"
            )
        source = adaptive_source
        inputs = source.inputs
        observed_now = coordinator._observed_now()
        if (
            source.setup_family != "first_dip_reclaim"
            or inputs.execution_surface != "alpaca_paper"
            or inputs.broker_environment != "paper"
            or inputs.decision_id != proof.decision_id
            or inputs.replay_or_paper_run_id != proof.run_id
            or inputs.generation != proof.generation
            or inputs.capture_prefix_root_sha256
            != proof.input_prefix_root_sha256
            or proof.first_dip_tape_read_id is None
            or observed_now < proof.attested_available_at
            or observed_now > proof.expires_at
            or source.capture_binding
            != adaptive_risk_capture_binding_from_active_attestation(proof)
        ):
            raise CaptureContractError(
                "first-dip adaptive bridge source escaped detector capture"
            )
        # Preparing the authority cross-checks the complete coordinator/run,
        # account, build, config, feature, resource, read, and continuity
        # identity before this bridge can be installed.
        detector_authority = coordinator.prepare_captured_first_dip_tape_authority(
            attestation=proof,
            policy=detector_policy,
            purpose="detector",
        )
        self.coordinator = coordinator
        self.detector_attestation = proof
        self.detector_policy = detector_policy
        self.adaptive_source = source
        self.final_read_provider = final_read_provider
        self._detector_authority = detector_authority
        self._install_lock = threading.Lock()
        self._install_started = False
        self._install_finished = False
        self._final_capture_frontier: FirstDipFinalCaptureFrontier | None = None

    @property
    def network_fallback_allowed(self) -> bool:
        return False

    @property
    def final_capture_frontier(self) -> FirstDipFinalCaptureFrontier | None:
        with self._install_lock:
            return self._final_capture_frontier

    def _retain_detector(
        self,
        resolution: object,
        opportunity_key: Mapping[str, object],
    ) -> str:
        from .adaptive_risk_reservation import (  # noqa: PLC0415
            AdaptiveRiskOpportunityKey,
        )

        payload = dict(opportunity_key)
        supplied_scope = str(payload.get("account_scope") or "").strip()
        if supplied_scope and supplied_scope != self.adaptive_source.account_scope:
            raise CaptureContractError(
                "first-dip detector opportunity account scope mismatch"
            )
        payload["account_scope"] = self.adaptive_source.account_scope
        opportunity = AdaptiveRiskOpportunityKey.from_payload(payload)
        expected_date = self.adaptive_source.inputs.as_of.astimezone(
            _MASSIVE_MARKET_TZ
        ).date()
        if (
            opportunity.symbol != self.adaptive_source.inputs.symbol
            or opportunity.setup_family != self.adaptive_source.setup_family
            or opportunity.trading_date != expected_date
        ):
            raise CaptureContractError(
                "first-dip detector opportunity differs from adaptive source"
            )
        return self.coordinator.retain_accepted_first_dip_detector(
            resolution=resolution,
            opportunity_key_sha256=opportunity.key_sha256,
        )

    def _runtime_material(self, **_boundary: Any) -> object:
        from .adaptive_risk_request_builder import (  # noqa: PLC0415
            AdaptiveRiskRuntimeCaptureMaterial,
        )

        return AdaptiveRiskRuntimeCaptureMaterial(
            source=self.adaptive_source,
            active_capture_attestation=self.detector_attestation,
        )

    def _final_authority(
        self,
        *,
        adaptive_request: object,
        detector_policy: FirstDipTapePolicy,
        final_boundary_available_at: datetime,
    ) -> object:
        from .adaptive_risk_reservation import (  # noqa: PLC0415
            AdaptiveRiskReservationRequest,
            load_adaptive_risk_reservation_request,
        )

        if type(adaptive_request) is not AdaptiveRiskReservationRequest:
            raise CaptureContractError(
                "first-dip adaptive bridge final request is not typed"
            )
        request = load_adaptive_risk_reservation_request(
            adaptive_request.to_payload()
        )
        boundary = _utc(
            final_boundary_available_at,
            "first-dip final boundary_available_at",
        )
        if (
            detector_policy.to_dict() != self.detector_policy.to_dict()
            or detector_policy.policy_sha256 != self.detector_policy.policy_sha256
            or request.inputs.decision_id
            != self.adaptive_source.inputs.decision_id
            or request.inputs.replay_or_paper_run_id
            != self.adaptive_source.inputs.replay_or_paper_run_id
            or request.inputs.generation != self.adaptive_source.inputs.generation
            or request.account_scope != self.adaptive_source.account_scope
            or request.setup_family != "first_dip_reclaim"
            or request.inputs.as_of > boundary
        ):
            raise CaptureContractError(
                "first-dip adaptive bridge final request/policy mismatch"
            )
        captured = self.final_read_provider(
            adaptive_request=request,
            detector_policy=detector_policy,
            final_boundary_available_at=boundary,
        )
        if not isinstance(captured, FirstDipFinalCaptureRead):
            raise CaptureContractError(
                "first-dip adaptive bridge final read is untyped"
            )
        matching = next(
            row
            for row in captured.captured_reads
            if row.receipt is not None
            and row.receipt.read_id == captured.first_dip_tape_read_id
        )
        assert matching.receipt is not None
        if matching.receipt.returned_at > boundary:
            raise CaptureContractError(
                "first-dip adaptive bridge final read is from the future"
            )
        proof = self.coordinator.attest_first_dip_pre_reservation_inputs(
            adaptive_request=request,
            dependency_profile=captured.dependency_profile,
            captured_reads=captured.captured_reads,
            first_dip_tape_read_id=captured.first_dip_tape_read_id,
        )
        # The caller sampled ``boundary`` before this provider durably committed
        # the final read/continuity proof.  Sample again after attestation and
        # bind that exact clock into both the private handoff and sealed record.
        resolved_boundary = self.coordinator._observed_now()
        if resolved_boundary < boundary:
            raise CaptureContractError(
                "first-dip adaptive bridge final clock moved backwards"
            )
        authority = self.coordinator.prepare_captured_first_dip_tape_authority(
            attestation=proof,
            policy=detector_policy,
            purpose="pre_reservation",
            final_boundary_available_at=resolved_boundary,
        )
        frontier = self.coordinator.first_dip_final_capture_frontier(proof)
        with self._install_lock:
            if self._final_capture_frontier is not None:
                raise CaptureContractError(
                    "first-dip adaptive bridge final frontier already exists"
                )
            self._final_capture_frontier = frontier
        from .first_dip_tape_decision import (  # noqa: PLC0415
            _issue_first_dip_final_authority_handoff,
        )

        return _issue_first_dip_final_authority_handoff(
            authority=authority,
            final_boundary_available_at=resolved_boundary,
            source="captured_db_paper",
        )

    @contextmanager
    def install(self) -> Iterator["LiveFirstDipAdaptiveCaptureBridge"]:
        """Install all four exact capabilities for one runner invocation."""

        from .adaptive_risk_request_builder import (  # noqa: PLC0415
            adaptive_risk_source_provider,
        )
        from .first_dip_tape_decision import (  # noqa: PLC0415
            _installed_captured_db_paper_first_dip_tape_decision_authority,
            _installed_captured_first_dip_detector_retention_provider,
            _installed_captured_first_dip_final_authority_provider,
        )

        with self._install_lock:
            if self._install_started or self._install_finished:
                raise CaptureContractError(
                    "first-dip adaptive bridge install is one-shot"
                )
            self._install_started = True
        try:
            with ExitStack() as stack:
                stack.enter_context(
                    _installed_captured_first_dip_detector_retention_provider(
                        self._retain_detector
                    )
                )
                stack.enter_context(
                    _installed_captured_first_dip_final_authority_provider(
                        self._final_authority
                    )
                )
                stack.enter_context(
                    adaptive_risk_source_provider(
                        self._runtime_material,
                        one_shot=True,
                    )
                )
                stack.enter_context(
                    _installed_captured_db_paper_first_dip_tape_decision_authority(
                        self._detector_authority
                    )
                )
                yield self
        finally:
            with self._install_lock:
                self._install_finished = True


LIVE_CAPTURE_RUN_CONFIGURATION_SCHEMA_VERSION = (
    "chili.live-replay-capture-run-configuration.v1"
)


@dataclass(frozen=True)
class LiveCaptureRunConfiguration:
    """Exact non-strategy runtime knobs which every run config must hash-bind."""

    heartbeat_timeout_seconds: float
    pretrigger_horizon_seconds: float
    per_symbol_pretrigger_events: int
    writer_batch_events: int
    writer_batch_bytes: int
    writer_poll_seconds: float
    writer_flush_interval_seconds: float
    max_change_keys: int
    max_read_sources: int
    schema_version: str = LIVE_CAPTURE_RUN_CONFIGURATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != LIVE_CAPTURE_RUN_CONFIGURATION_SCHEMA_VERSION:
            raise CaptureContractError("live capture run configuration is unsupported")
        for name in (
            "heartbeat_timeout_seconds",
            "pretrigger_horizon_seconds",
            "writer_poll_seconds",
            "writer_flush_interval_seconds",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise CaptureContractError(
                    f"live capture run configuration {name} must be positive"
                )
            object.__setattr__(self, name, value)
        for name in (
            "per_symbol_pretrigger_events",
            "writer_batch_events",
            "writer_batch_bytes",
            "max_change_keys",
            "max_read_sources",
        ):
            raw = getattr(self, name)
            if isinstance(raw, bool) or int(raw) <= 0:
                raise CaptureContractError(
                    f"live capture run configuration {name} must be positive"
                )
            object.__setattr__(self, name, int(raw))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "heartbeat_timeout_seconds": self.heartbeat_timeout_seconds,
            "pretrigger_horizon_seconds": self.pretrigger_horizon_seconds,
            "per_symbol_pretrigger_events": self.per_symbol_pretrigger_events,
            "writer_batch_events": self.writer_batch_events,
            "writer_batch_bytes": self.writer_batch_bytes,
            "writer_poll_seconds": self.writer_poll_seconds,
            "writer_flush_interval_seconds": self.writer_flush_interval_seconds,
            "max_change_keys": self.max_change_keys,
            "max_read_sources": self.max_read_sources,
        }

    @property
    def configuration_sha256(self) -> str:
        return sha256_json(self.to_dict())


@dataclass(frozen=True)
class LiveCaptureRunInputs:
    """Already-observed immutable startup inputs for one exact run identity."""

    identity: CaptureRunIdentity
    evidence: CaptureIdentityEvidence
    producers: tuple[CaptureProducerSpec, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.identity, CaptureRunIdentity):
            raise CaptureContractError("live capture run identity is malformed")
        if not isinstance(self.evidence, CaptureIdentityEvidence):
            raise CaptureContractError("live capture startup evidence is malformed")
        producers = tuple(self.producers)
        if not producers or any(
            not isinstance(row, CaptureProducerSpec) for row in producers
        ):
            raise CaptureContractError("live capture producer roster is malformed")
        object.__setattr__(self, "producers", producers)


class LiveCaptureStartupInputProvider(Protocol):
    """Packages already-observed identity/account facts; it must never fetch."""

    def __call__(
        self,
        symbol: str,
        *,
        resource_binding: CaptureResourceBinding,
        run_configuration: LiveCaptureRunConfiguration,
        capture_store_root: Path,
    ) -> LiveCaptureRunInputs: ...


@runtime_checkable
class LiveCaptureRunFactory(Protocol):
    """Build one inert one-symbol run from already-observed startup evidence."""

    def __call__(
        self,
        symbol: str,
        *,
        resource_binding: CaptureResourceBinding,
        pressure_controller: CaptureAdaptivePressureController,
        shared_admission_budget: SharedCaptureAdmissionBudget,
        wall_clock: Callable[[], datetime],
    ) -> tuple[LiveReplayCaptureCoordinator, CaptureIdentityEvidence]: ...


@dataclass(frozen=True)
class SharedStoreLiveCaptureRunFactory:
    """Concrete no-fetch factory using one aggregate store/quota runtime."""

    shared_store_runtime: SharedCaptureStoreRuntime
    run_configuration: LiveCaptureRunConfiguration
    startup_input_provider: LiveCaptureStartupInputProvider

    def __post_init__(self) -> None:
        if not isinstance(self.shared_store_runtime, SharedCaptureStoreRuntime):
            raise CaptureContractError("live capture shared store runtime is malformed")
        if not isinstance(self.run_configuration, LiveCaptureRunConfiguration):
            raise CaptureContractError("live capture run configuration is malformed")
        if not callable(self.startup_input_provider):
            raise CaptureContractError("live capture startup input provider is malformed")

    def __call__(
        self,
        symbol: str,
        *,
        resource_binding: CaptureResourceBinding,
        pressure_controller: CaptureAdaptivePressureController,
        shared_admission_budget: SharedCaptureAdmissionBudget,
        wall_clock: Callable[[], datetime],
    ) -> tuple[LiveReplayCaptureCoordinator, CaptureIdentityEvidence]:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        manager = self.shared_store_runtime
        if manager.resource_binding != resource_binding:
            raise CaptureContractError("live capture factory resource binding mismatch")
        if manager.shared_admission_budget is not shared_admission_budget:
            raise CaptureContractError("live capture factory shared admission mismatch")
        if shared_admission_budget.pressure_controller is not pressure_controller:
            raise CaptureContractError("live capture factory pressure controller mismatch")
        if not callable(wall_clock):
            raise CaptureContractError("live capture factory wall clock is malformed")
        inputs = self.startup_input_provider(
            normalized,
            resource_binding=resource_binding,
            run_configuration=self.run_configuration,
            capture_store_root=manager.store.root,
        )
        if not isinstance(inputs, LiveCaptureRunInputs):
            raise CaptureContractError("live capture startup provider returned malformed inputs")
        identity = inputs.identity
        evidence = inputs.evidence
        evidence.validate_for(identity, certification_symbol=normalized)
        config = evidence.config
        recorded_configuration = config.get("live_capture_run_configuration")
        if (
            not isinstance(recorded_configuration, Mapping)
            or dict(recorded_configuration) != self.run_configuration.to_dict()
        ):
            raise CaptureContractError(
                "live capture startup config lacks its full run configuration"
            )
        if (
            str(config.get("live_capture_run_configuration_sha256") or "")
            .strip()
            .lower()
            != self.run_configuration.configuration_sha256
        ):
            raise CaptureContractError(
                "live capture startup config lacks its exact run configuration"
            )
        if (
            str(config.get("capture_resource_binding_sha256") or "")
            .strip()
            .lower()
            != resource_binding.binding_sha256
        ):
            raise CaptureContractError(
                "live capture startup config lacks its exact resource binding"
            )
        store_root_raw = str(config.get("capture_store_root") or "").strip()
        if (
            not store_root_raw
            or not Path(store_root_raw).is_absolute()
            or Path(store_root_raw).resolve() != manager.store.root.resolve()
        ):
            raise CaptureContractError(
                "live capture startup config has a different shared store root"
            )
        for producer in inputs.producers:
            if (
                producer.code_build_sha256 != identity.code_build_sha256
                or producer.config_sha256 != identity.config_sha256
                or producer.feature_flags_sha256 != identity.feature_flags_sha256
                or producer.resource_binding_sha256
                != resource_binding.binding_sha256
            ):
                raise CaptureContractError(
                    "live capture producer roster escaped its run/resource identity"
                )
        configuration = self.run_configuration
        coordinator = LiveReplayCaptureCoordinator.create_with_shared_store(
            identity=identity,
            certification_symbol=normalized,
            resource_binding=resource_binding,
            pressure_controller=pressure_controller,
            shared_store_runtime=manager,
            producers=inputs.producers,
            heartbeat_timeout_seconds=configuration.heartbeat_timeout_seconds,
            wall_clock=wall_clock,
            pretrigger_horizon=timedelta(
                seconds=configuration.pretrigger_horizon_seconds
            ),
            per_symbol_pretrigger_events=(
                configuration.per_symbol_pretrigger_events
            ),
            writer_batch_events=configuration.writer_batch_events,
            writer_batch_bytes=configuration.writer_batch_bytes,
            writer_poll_seconds=configuration.writer_poll_seconds,
            writer_flush_interval_seconds=(
                configuration.writer_flush_interval_seconds
            ),
            max_change_keys=configuration.max_change_keys,
            max_read_sources=configuration.max_read_sources,
        )
        return coordinator, evidence


@dataclass(frozen=True)
class LiveCaptureHotRunAdmission:
    symbol: str
    coordinator: LiveReplayCaptureCoordinator
    promotion: HotPromotionResult
    startup_events: tuple[CaptureEvent, ...]
    rejected_close: UnsealedCaptureClose | None = None

    @property
    def capture_ready(self) -> bool:
        return bool(
            self.promotion.hot
            and self.rejected_close is None
            and self.coordinator.state is CaptureSessionState.RUNNING
        )


class LiveReplayCaptureProcessService:
    """Fail-closed live-loop integration surface; performs no provider fetches.

    Intended hook order is explicit: broad observations may arrive immediately
    after process start; candidate admission builds/starts a one-symbol run and
    atomically promotes its ring history; hot inputs and exact reads feed that
    run; the FSM may perform only a provisional, side-effect-free computation,
    commits its canonical checkpoint/output, then requests the private final
    proof.  Atomic ledger recomputation/reservation and broker mutation belong
    after that proof; this service rejects broker callbacks without it.  The
    loop finally releases the hot lease and seals or aborts.
    """

    def __init__(
        self,
        *,
        supervisor: LiveReplayCaptureSupervisor,
        run_factory: LiveCaptureRunFactory,
    ) -> None:
        if not isinstance(supervisor, LiveReplayCaptureSupervisor):
            raise CaptureContractError("capture process supervisor is malformed")
        if not callable(run_factory):
            raise CaptureContractError("capture run factory is malformed")
        self.supervisor = supervisor
        self.run_factory = run_factory
        self._lock = threading.RLock()
        self._pending_symbols: set[str] = set()
        self._run_by_symbol: dict[str, LiveReplayCaptureCoordinator] = {}
        self._config_json_by_symbol: dict[str, str] = {}
        self._identity_evidence_json_by_symbol: dict[str, str] = {}
        self._final_proof_by_decision: dict[
            tuple[str, str], ActiveCapturePrefixAttestation
        ] = {}

    @property
    def network_fallback_allowed(self) -> bool:
        return False

    @property
    def capture_resource_binding_sha256(self) -> str:
        return self.supervisor.resource_binding.binding_sha256

    @property
    def capture_queue_event_limit(self) -> int:
        return int(self.supervisor.resource_binding.budget.max_queue_events)

    @property
    def capture_queue_byte_limit(self) -> int:
        return int(self.supervisor.resource_binding.budget.async_queue_bytes)

    @property
    def capture_gap_key_limit(self) -> int:
        return int(self.supervisor.resource_binding.budget.max_gap_keys)

    def record_broad_input(self, **observation: Any) -> CaptureSubmission:
        return self.supervisor.record_broad_input(**observation)

    def record_broad_gap(self, gap: CoverageGap) -> tuple[str, ...]:
        return self.supervisor.record_broad_gap(gap)

    def record_hot_gap(self, gap: CoverageGap) -> bool:
        return self.supervisor.record_hot_gap(gap)

    def record_broad_change(self, **observation: Any) -> ChangeCaptureResult:
        return self.supervisor.record_broad_change(**observation)

    def admit_hot_symbol(
        self,
        symbol: str,
        *,
        required_stream: CaptureStream = CaptureStream.L2_DEPTH_DELTA,
    ) -> LiveCaptureHotRunAdmission:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        with self._lock:
            if normalized in self._run_by_symbol or normalized in self._pending_symbols:
                raise CaptureContractError(
                    "capture process already owns or is building this symbol"
                )
            self._pending_symbols.add(normalized)
        coordinator: LiveReplayCaptureCoordinator | None = None
        startup: tuple[CaptureEvent, ...] = ()
        try:
            coordinator, evidence = self.run_factory(
                normalized,
                resource_binding=self.supervisor.resource_binding,
                pressure_controller=self.supervisor.pressure_controller,
                shared_admission_budget=self.supervisor.shared_admission_budget,
                wall_clock=self.supervisor._wall_clock,
            )
            if (
                not isinstance(coordinator, LiveReplayCaptureCoordinator)
                or not isinstance(evidence, CaptureIdentityEvidence)
                or coordinator.certification_symbol != normalized
            ):
                raise CaptureContractError(
                    "capture run factory returned a mismatched one-symbol run"
                )
            self.supervisor.attach(coordinator)
            startup = coordinator.start(evidence)
            promotion = self.supervisor.promote_hot_symbol(
                normalized,
                required_stream=required_stream,
            )
            if not promotion.hot:
                closed = coordinator.abort(
                    reason="hot_symbol_admission_rejected"
                )
                self.supervisor.detach(coordinator)
                return LiveCaptureHotRunAdmission(
                    symbol=normalized,
                    coordinator=coordinator,
                    promotion=promotion,
                    startup_events=startup,
                    rejected_close=closed,
                )
            with self._lock:
                self._run_by_symbol[normalized] = coordinator
                self._config_json_by_symbol[normalized] = (
                    canonical_json_bytes(dict(evidence.config)).decode("utf-8")
                )
                self._identity_evidence_json_by_symbol[normalized] = (
                    canonical_json_bytes(
                        {
                            "code_build": dict(evidence.code_build),
                            "config": dict(evidence.config),
                            "feature_flags": dict(evidence.feature_flags),
                            "account_identity": dict(evidence.account_identity),
                            "account_risk_snapshot": dict(
                                evidence.account_risk_snapshot
                            ),
                            "account_query": dict(evidence.account_query),
                            "account_provider": evidence.account_provider,
                        }
                    ).decode("utf-8")
                )
            return LiveCaptureHotRunAdmission(
                symbol=normalized,
                coordinator=coordinator,
                promotion=promotion,
                startup_events=startup,
            )
        except BaseException:
            if coordinator is not None:
                if coordinator.state is CaptureSessionState.RUNNING:
                    active = normalized in self.supervisor.health()["active_symbols"]
                    if active:
                        self.supervisor.release_hot_symbol(normalized)
                    coordinator.abort(reason="hot_symbol_admission_exception")
                elif coordinator.state is CaptureSessionState.CREATED:
                    coordinator.discard_unstarted(
                        reason="hot_symbol_admission_exception_before_start"
                    )
                if coordinator.state in {
                    CaptureSessionState.CREATED,
                    CaptureSessionState.ABORTED,
                    CaptureSessionState.SEALED,
                }:
                    self.supervisor.detach(coordinator)
            raise
        finally:
            with self._lock:
                self._pending_symbols.discard(normalized)

    def coordinator_for(self, symbol: str) -> LiveReplayCaptureCoordinator:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        with self._lock:
            coordinator = self._run_by_symbol.get(normalized)
            if coordinator is None or coordinator.state is not CaptureSessionState.RUNNING:
                raise CaptureContractError("symbol has no running capture coordinator")
            return coordinator

    def config_evidence_for(self, symbol: str) -> Mapping[str, Any]:
        """Return an isolated copy of the exact active run config evidence."""

        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        coordinator = self.coordinator_for(normalized)
        with self._lock:
            canonical = self._config_json_by_symbol.get(normalized)
        if canonical is None:
            raise CaptureContractError(
                "symbol has no active capture config evidence"
            )
        try:
            config = json.loads(canonical)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CaptureContractError(
                "active capture config evidence is malformed"
            ) from exc
        if not isinstance(config, dict) or sha256_json(config) != coordinator.identity.config_sha256:
            raise CaptureContractError(
                "active capture config evidence escaped run identity"
            )
        return config

    def config_sha256_for(self, symbol: str) -> str:
        """Resolve the actual per-symbol capture identity config digest."""

        config = self.config_evidence_for(symbol)
        return sha256_json(config)

    def identity_evidence_for(self, symbol: str) -> CaptureIdentityEvidence:
        """Return a detached copy of the exact startup evidence for one hot run."""

        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        coordinator = self.coordinator_for(normalized)
        with self._lock:
            canonical = self._identity_evidence_json_by_symbol.get(normalized)
        if canonical is None:
            raise CaptureContractError(
                "symbol has no active capture identity evidence"
            )
        try:
            raw = json.loads(canonical)
            evidence = CaptureIdentityEvidence(
                code_build=raw["code_build"],
                config=raw["config"],
                feature_flags=raw["feature_flags"],
                account_identity=raw["account_identity"],
                account_risk_snapshot=raw["account_risk_snapshot"],
                account_query=raw["account_query"],
                account_provider=raw["account_provider"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CaptureContractError(
                "active capture identity evidence is malformed"
            ) from exc
        evidence.validate_for(
            coordinator.identity,
            certification_symbol=normalized,
        )
        return evidence

    def submit_hot_input(self, **observation: Any) -> CaptureSubmission:
        return self.supervisor.submit_hot_input(**observation)

    def submit_exact_input(self, symbol: str, **observation: Any) -> CaptureSubmission:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        supplied = _normalized_symbol(observation.pop("symbol", normalized))
        if supplied != normalized:
            raise CaptureContractError("exact input symbol boundary mismatch")
        return self.coordinator_for(normalized).submit_exact_input(
            symbol=normalized,
            **observation,
        )

    def record_change(self, symbol: str, **change: Any) -> ChangeCaptureResult:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        supplied = _normalized_symbol(change.pop("symbol", normalized))
        if supplied != normalized:
            raise CaptureContractError("change input symbol boundary mismatch")
        if change.get("broad"):
            raise CaptureContractError(
                "supervised broad observations must use record_broad_input"
            )
        return self.coordinator_for(normalized).record_change(
            symbol=normalized,
            **change,
        )

    def capture_query_result(self, symbol: str, **read: Any) -> CapturedReadResult:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        supplied = _normalized_symbol(read.pop("symbol", normalized))
        if supplied != normalized:
            raise CaptureContractError("capture query symbol boundary mismatch")
        return self.coordinator_for(normalized).capture_query_result(
            symbol=normalized,
            **read,
        )

    def capture_durable_read(self, symbol: str, **read: Any) -> CapturedReadResult:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        supplied = _normalized_symbol(read.pop("symbol", normalized))
        if supplied != normalized:
            raise CaptureContractError("capture read symbol boundary mismatch")
        return self.coordinator_for(normalized).capture_durable_read(
            symbol=normalized,
            **read,
        )

    def capture_complete_microstructure_window(
        self, symbol: str, **read: Any
    ) -> CapturedReadResult:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        supplied = _normalized_symbol(read.pop("symbol", normalized))
        if supplied != normalized:
            raise CaptureContractError(
                "microstructure read symbol boundary mismatch"
            )
        return self.coordinator_for(
            normalized
        ).capture_complete_microstructure_window(
            symbol=normalized,
            **read,
        )

    def checkpoint_decision(
        self, symbol: str, **decision: Any
    ) -> UnverifiedDecisionPrefix:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        supplied = _normalized_symbol(decision.pop("symbol", normalized))
        if supplied != normalized:
            raise CaptureContractError("capture decision symbol boundary mismatch")
        return self.coordinator_for(normalized).checkpoint_decision(
            symbol=normalized,
            **decision,
        )

    def active_decision_capture_proof(
        self, symbol: str, **proof: Any
    ) -> ActiveCapturePrefixAttestation:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        attestation = self.coordinator_for(
            normalized
        ).active_decision_capture_proof(**proof)
        with self._lock:
            key = (normalized, attestation.decision_id)
            if key in self._final_proof_by_decision:
                raise CaptureContractError(
                    "capture process decision proof was already issued"
                )
            self._final_proof_by_decision[key] = attestation
        return attestation

    def final_proof_for(
        self, symbol: str, decision_id: str
    ) -> ActiveCapturePrefixAttestation:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        key = (normalized, str(decision_id or "").strip())
        with self._lock:
            proof = self._final_proof_by_decision.get(key)
        if proof is None:
            raise CaptureContractError(
                "no private final capture proof exists for this decision"
            )
        return verify_active_capture_prefix_attestation(proof)

    def emit_provider_watermark(
        self, symbol: str, **watermark: Any
    ) -> ProviderWatermark:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        supplied = _normalized_symbol(watermark.pop("symbol", normalized))
        if supplied != normalized:
            raise CaptureContractError("provider watermark symbol boundary mismatch")
        return self.coordinator_for(normalized).emit_provider_watermark(
            symbol=normalized,
            **watermark,
        )

    def emit_capture_health(
        self, symbol: str, **health: Any
    ) -> CaptureSubmission:
        return self.coordinator_for(symbol).emit_capture_health(**health)

    def heartbeat(self, symbol: str, producer_id: str) -> CaptureEvent:
        return self.coordinator_for(symbol).heartbeat(producer_id)

    def record_broker_order_lifecycle(
        self, symbol: str, **transition: Any
    ) -> CaptureSubmission:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        supplied = _normalized_symbol(transition.pop("symbol", normalized))
        if supplied != normalized:
            raise CaptureContractError("broker lifecycle symbol boundary mismatch")
        lifecycle = transition.get("lifecycle")
        if not isinstance(lifecycle, CaptureBrokerOrderLifecycle):
            raise CaptureContractError("broker lifecycle transition is malformed")
        self.final_proof_for(normalized, lifecycle.decision_id)
        return self.coordinator_for(normalized).record_broker_order_lifecycle(
            symbol=normalized,
            **transition,
        )

    def release_and_seal(
        self,
        symbol: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> SealedCaptureHandoff:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        coordinator = self.coordinator_for(normalized)
        if not self.supervisor.release_hot_symbol(normalized):
            raise CaptureContractError("capture process hot lease was not active")
        try:
            handoff = coordinator.stop_and_seal(timeout_seconds=timeout_seconds)
        except BaseException:
            if coordinator.state is CaptureSessionState.RUNNING:
                coordinator.abort(reason="seal_failed")
            if coordinator.state is not CaptureSessionState.RUNNING:
                self.supervisor.detach(coordinator)
            with self._lock:
                self._run_by_symbol.pop(normalized, None)
                self._config_json_by_symbol.pop(normalized, None)
                self._identity_evidence_json_by_symbol.pop(normalized, None)
                for key in tuple(self._final_proof_by_decision):
                    if key[0] == normalized:
                        self._final_proof_by_decision.pop(key, None)
            raise
        self.supervisor.detach(coordinator)
        with self._lock:
            self._run_by_symbol.pop(normalized, None)
            self._config_json_by_symbol.pop(normalized, None)
            self._identity_evidence_json_by_symbol.pop(normalized, None)
            for key in tuple(self._final_proof_by_decision):
                if key[0] == normalized:
                    self._final_proof_by_decision.pop(key, None)
        return handoff

    def abort_symbol(self, symbol: str, *, reason: str) -> UnsealedCaptureClose:
        normalized = _normalized_symbol(symbol, required=True)
        assert normalized is not None
        coordinator = self.coordinator_for(normalized)
        if normalized in self.supervisor.health()["active_symbols"]:
            self.supervisor.release_hot_symbol(normalized)
        closed = coordinator.abort(reason=reason)
        self.supervisor.detach(coordinator)
        with self._lock:
            self._run_by_symbol.pop(normalized, None)
            self._config_json_by_symbol.pop(normalized, None)
            self._identity_evidence_json_by_symbol.pop(normalized, None)
            for key in tuple(self._final_proof_by_decision):
                if key[0] == normalized:
                    self._final_proof_by_decision.pop(key, None)
        return closed

    def health(self) -> Mapping[str, Any]:
        with self._lock:
            runs = {
                symbol: coordinator.health()
                for symbol, coordinator in sorted(self._run_by_symbol.items())
            }
            pending = tuple(sorted(self._pending_symbols))
        return {
            "network_fallback_allowed": False,
            "pending_symbols": pending,
            "running_symbols": tuple(sorted(runs)),
            "supervisor": self.supervisor.health(),
            "runs": runs,
        }


__all__ = [
    "CaptureIdentityEvidence",
    "CaptureSessionState",
    "CaptureSubmission",
    "CapturedReadResult",
    "ChangeCaptureResult",
    "ExecutedCaptureReadEvidence",
    "ExecutedCaptureReadInventory",
    "ExecutedCaptureSourceEventEvidence",
    "FirstDipFinalCaptureRead",
    "FirstDipFinalCaptureFrontier",
    "FirstDipFinalReadProvider",
    "HotPromotionResult",
    "LiveCaptureHotRunAdmission",
    "LiveCaptureRunConfiguration",
    "LiveCaptureRunFactory",
    "LiveCaptureRunInputs",
    "LiveCaptureStartupInputProvider",
    "LiveFirstDipAdaptiveCaptureBridge",
    "LiveMicrostructureCaptureBridge",
    "LiveOhlcvCaptureBridge",
    "LiveReplayCaptureCoordinator",
    "LiveReplayCapturePort",
    "LiveReplayCaptureProcessService",
    "LiveReplayCaptureSupervisor",
    "LiveScannerSnapshotCaptureBridge",
    "ObservedCaptureInput",
    "SealedCaptureHandoff",
    "SharedStoreLiveCaptureRunFactory",
    "UnsealedCaptureClose",
    "UnverifiedDecisionPrefix",
    "build_executed_capture_read_inventory",
    "executed_capture_read_evidence",
]
