"""Resource-bounded runtime primitives for production replay capture.

The hot path never waits for disk.  Producers submit to a bounded in-memory
queue; a background worker writes immutable compressed chunks.  Any rejected
event is aggregated into an explicit coverage gap.  If the writer crashes
before those gaps can be persisted, the run cannot acquire a clean-close
marker and coverage therefore still fails closed.

This module is intentionally provider-agnostic.  Live FSM call-site hooks are
added separately after their exact read surfaces have been audited.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict, deque
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import heapq
import json
import math
import os
from pathlib import Path
import shutil
import secrets
import socket
import ssl
import threading
import time
from typing import Any, Callable, Iterable, Mapping
import uuid
import zlib

try:
    import zstandard as zstd
except ImportError:  # exercised by deployment preflight when requirements drift
    zstd = None  # type: ignore[assignment]

from .replay_capture_contract import (
    CAPTURE_PRODUCER_LIFECYCLE_PROVIDER,
    CAPTURE_SCHEMA_VERSION,
    ActiveCaptureInputPrefixAttestation,
    ActiveCaptureContinuityEvidence,
    ActiveCapturePrefixAttestation,
    ActiveCaptureReadEvidence,
    CaptureBrokerOrderLifecycle,
    CaptureBrokerTransition,
    CaptureClocks,
    CaptureContractError,
    CaptureDecisionAction,
    CaptureDecisionOutput,
    CaptureEvent,
    CaptureEventRef,
    CaptureIqfeedPrint,
    CaptureMicrostructureOperation,
    CaptureMicrostructureReadQuery,
    CaptureOrderIntent,
    CaptureOrderIntentRole,
    CaptureProviderRegistrationEvidence,
    CaptureProviderRegistrationRecord,
    CaptureReadReceipt,
    FirstDipTapeReceiptEvidence,
    FSMDependencyProfile,
    CaptureProducerLifecycleFact,
    CaptureProducerLifecycleKind,
    CaptureProducerSpec,
    CaptureRunOpen,
    ReplayCoverageRequest,
    ProviderWatermark,
    CaptureRunIdentity,
    CaptureStream,
    CoverageGap,
    CoverageMode,
    STREAM_POLICIES,
    StreamCoverage,
    VerifiedReplayCapture,
    canonical_json_bytes,
    captured_read_result_sha256,
    capture_adaptive_order_artifacts_from_payload,
    capture_decision_output_from_payload,
    _issue_active_capture_input_attestation,
    _issue_active_capture_prefix_attestation,
    sha256_json,
    verify_active_capture_input_attestation,
    _coverage_source_clock,
)
from .replay_errors import ReplayInputContractError
from .first_dip_tape_policy import (
    FirstDipTapePolicy,
    FirstDipTapePolicyError,
    FirstDipTapeReadQuery,
    evaluate_first_dip_tape,
    first_dip_tape_window_from_capture,
)


UTC = timezone.utc


class FirstDipTapeCoverageUnavailable(CaptureContractError):
    """The exact first-dip print window cannot be proven from bounded capture."""

    def __init__(
        self,
        reason: str,
        *,
        symbol: str | None,
        first_available_at: datetime,
        last_available_at: datetime,
        lost_count: int = 1,
        coverage_gap_required: bool = False,
    ) -> None:
        normalized = str(reason or "first_dip_tape_coverage_unavailable").strip().lower()
        super().__init__(normalized)
        self.reason = normalized
        self.symbol = str(symbol or "").strip().upper() or None
        self.first_available_at = _utc(
            first_available_at, "first_dip gap first_available_at"
        )
        self.last_available_at = _utc(
            last_available_at, "first_dip gap last_available_at"
        )
        if self.last_available_at < self.first_available_at:
            raise CaptureContractError("first-dip coverage gap clocks are reversed")
        if isinstance(lost_count, bool) or int(lost_count) <= 0:
            raise CaptureContractError("first-dip coverage gap count must be positive")
        self.lost_count = int(lost_count)
        if type(coverage_gap_required) is not bool:
            raise CaptureContractError(
                "first-dip coverage gap disposition must be boolean"
            )
        self.coverage_gap_required = coverage_gap_required


class MicrostructureCoverageUnavailable(CaptureContractError):
    """A complete runtime-owned microstructure source window is unavailable."""

    def __init__(
        self,
        reason: str,
        *,
        stream: CaptureStream,
        symbol: str,
        first_available_at: datetime,
        last_available_at: datetime,
        lost_count: int = 1,
    ) -> None:
        normalized = str(
            reason or "microstructure_coverage_unavailable"
        ).strip().lower()
        super().__init__(normalized)
        self.reason = normalized
        self.stream = stream
        self.symbol = str(symbol or "").strip().upper()
        self.first_available_at = _utc(
            first_available_at, "microstructure gap first_available_at"
        )
        self.last_available_at = _utc(
            last_available_at, "microstructure gap last_available_at"
        )
        if (
            not self.symbol
            or self.last_available_at < self.first_available_at
            or isinstance(lost_count, bool)
            or int(lost_count) <= 0
        ):
            raise CaptureContractError(
                "microstructure coverage-unavailable evidence is malformed"
            )
        self.lost_count = int(lost_count)


_FIRST_DIP_FINAL_INPUT_BINDING_TOKEN = object()


@dataclass(frozen=True)
class _FirstDipFinalInputBinding:
    """Runtime-only lineage carried into one final input attestation."""

    prior_detector_reference_sha256: str
    adaptive_request_sha256: str
    opportunity_key_sha256: str
    _verification_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_token is not _FIRST_DIP_FINAL_INPUT_BINDING_TOKEN:
            raise CaptureContractError(
                "first-dip final input binding was not issued by capture runtime"
            )
        for name in (
            "prior_detector_reference_sha256",
            "adaptive_request_sha256",
            "opportunity_key_sha256",
        ):
            object.__setattr__(
                self,
                name,
                _validated_sha256(getattr(self, name), f"first-dip final {name}"),
            )

_CAPTURE_RUNTIME_HEALTH_PROVIDER = "chili_capture_runtime"
_CAPTURE_RUNTIME_HEALTH_KEYS = frozenset(
    {
        "phase",
        "state",
        "identity_sha256",
        "resource_binding",
        "resource_hashes",
        "network_fallback_allowed",
        "durable_sequence_next",
        "accepted_count",
        "rejected_or_reported_lost_count",
        "change_key_count",
        "max_change_keys",
        "max_read_sources",
        "hot_symbols",
        "ingress",
        "pretrigger",
        "hot_leases",
        "pressure",
        "writer",
    }
)


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CaptureContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value, "datetime").isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise CaptureContractError(f"{field_name} must be ISO-8601 text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CaptureContractError(f"{field_name} must be ISO-8601 text") from exc
    return _utc(parsed, field_name)


@dataclass(frozen=True)
class CaptureResourceMeasurement:
    """Observed host headroom used to resolve finite capture budgets.

    ``sustained_append_bytes_per_second`` is canonical uncompressed capture
    input accepted by the measured compression/write pipeline per wall second.
    This matches ``CaptureEvent.canonical_size_bytes`` used by admission; disk
    bytes after compression are reported separately by the benchmark.
    """

    measured_at: datetime
    sample_seconds: float
    total_memory_bytes: int
    available_memory_bytes: int
    disk_free_bytes: int
    average_cpu_percent: float
    sustained_append_bytes_per_second: float
    fsync_p95_milliseconds: float
    logical_cpu_count: int
    host_fingerprint_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "measured_at", _utc(self.measured_at, "measured_at"))
        for name in (
            "sample_seconds",
            "average_cpu_percent",
            "sustained_append_bytes_per_second",
            "fsync_p95_milliseconds",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise CaptureContractError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, value)
        if self.sample_seconds <= 0:
            raise CaptureContractError("sample_seconds must be positive")
        if self.average_cpu_percent > 100:
            raise CaptureContractError("average_cpu_percent cannot exceed 100")
        for name in (
            "total_memory_bytes",
            "available_memory_bytes",
            "disk_free_bytes",
        ):
            value = int(getattr(self, name))
            if value <= 0:
                raise CaptureContractError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        if self.available_memory_bytes > self.total_memory_bytes:
            raise CaptureContractError("available memory cannot exceed total memory")
        if isinstance(self.logical_cpu_count, bool) or int(self.logical_cpu_count) <= 0:
            raise CaptureContractError("logical_cpu_count must be a positive integer")
        object.__setattr__(self, "logical_cpu_count", int(self.logical_cpu_count))
        digest = str(self.host_fingerprint_sha256 or "").lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise CaptureContractError("host_fingerprint_sha256 must be a full SHA256")
        object.__setattr__(self, "host_fingerprint_sha256", digest)

    @property
    def measurement_sha256(self) -> str:
        return sha256_json(asdict(self))


@dataclass(frozen=True)
class CaptureBudgetPolicy:
    """Explicit, logged resource allocation policy; no unlimited sentinel values."""

    memory_reserve_bytes: int
    disk_reserve_bytes: int
    capture_fraction_of_memory_headroom: float
    ring_fraction_of_capture_memory: float
    queue_fraction_of_capture_memory: float
    capture_fraction_of_disk_headroom: float
    capture_fraction_of_measured_write_bandwidth: float
    max_average_cpu_percent: float
    capture_fraction_of_cpu_headroom: float
    calibrated_hot_symbol_bytes: int
    max_queue_events: int
    max_ring_events: int
    max_gap_keys: int
    raw_retention_days: int
    derived_retention_days: int
    pressure_cpu_enter_percent: float
    pressure_cpu_exit_percent: float
    pressure_memory_enter_margin_bytes: int
    pressure_memory_exit_margin_bytes: int
    pressure_disk_enter_margin_bytes: int
    pressure_disk_exit_margin_bytes: int
    pressure_write_latency_enter_milliseconds: float
    pressure_write_latency_exit_milliseconds: float
    pressure_enter_samples: int
    pressure_recovery_samples: int
    pressure_sample_max_age_seconds: float
    store_owner_lease_seconds: float
    store_owner_heartbeat_seconds: float

    def __post_init__(self) -> None:
        for name in (
            "memory_reserve_bytes",
            "disk_reserve_bytes",
            "calibrated_hot_symbol_bytes",
            "max_queue_events",
            "max_ring_events",
            "max_gap_keys",
            "raw_retention_days",
            "derived_retention_days",
            "pressure_memory_enter_margin_bytes",
            "pressure_memory_exit_margin_bytes",
            "pressure_disk_enter_margin_bytes",
            "pressure_disk_exit_margin_bytes",
            "pressure_enter_samples",
            "pressure_recovery_samples",
        ):
            value = int(getattr(self, name))
            if value <= 0:
                raise CaptureContractError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        for name in (
            "capture_fraction_of_memory_headroom",
            "ring_fraction_of_capture_memory",
            "queue_fraction_of_capture_memory",
            "capture_fraction_of_disk_headroom",
            "capture_fraction_of_measured_write_bandwidth",
            "capture_fraction_of_cpu_headroom",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0 < value < 1:
                raise CaptureContractError(f"{name} must be between zero and one")
            object.__setattr__(self, name, value)
        for name in (
            "max_average_cpu_percent",
            "pressure_cpu_enter_percent",
            "pressure_cpu_exit_percent",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0 < value <= 100:
                raise CaptureContractError(f"{name} must be between zero and 100")
            object.__setattr__(self, name, value)
        for name in (
            "pressure_write_latency_enter_milliseconds",
            "pressure_write_latency_exit_milliseconds",
            "pressure_sample_max_age_seconds",
            "store_owner_lease_seconds",
            "store_owner_heartbeat_seconds",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise CaptureContractError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)
        if (
            self.ring_fraction_of_capture_memory
            + self.queue_fraction_of_capture_memory
            >= 1
        ):
            raise CaptureContractError(
                "ring plus queue fractions must leave memory for hot-symbol state"
            )
        if self.derived_retention_days < self.raw_retention_days:
            raise CaptureContractError("derived retention must not be shorter than raw")
        if self.pressure_cpu_exit_percent >= self.pressure_cpu_enter_percent:
            raise CaptureContractError("CPU pressure recovery must be below entry")
        if self.pressure_cpu_enter_percent > self.max_average_cpu_percent:
            raise CaptureContractError(
                "CPU pressure entry cannot exceed the measured-start ceiling"
            )
        if (
            self.pressure_memory_exit_margin_bytes
            <= self.pressure_memory_enter_margin_bytes
        ):
            raise CaptureContractError("memory pressure recovery margin must exceed entry")
        if self.pressure_disk_exit_margin_bytes <= self.pressure_disk_enter_margin_bytes:
            raise CaptureContractError("disk pressure recovery margin must exceed entry")
        if (
            self.pressure_write_latency_exit_milliseconds
            >= self.pressure_write_latency_enter_milliseconds
        ):
            raise CaptureContractError("write-latency recovery must be below entry")
        if self.store_owner_heartbeat_seconds >= self.store_owner_lease_seconds:
            raise CaptureContractError("store owner heartbeat must be shorter than lease")

    @property
    def policy_sha256(self) -> str:
        return sha256_json(asdict(self))


@dataclass(frozen=True)
class ResolvedCaptureBudget:
    measurement_sha256: str
    policy_sha256: str
    measured_average_cpu_percent: float
    cpu_headroom_to_ceiling_percent: float
    capture_cpu_budget_percent: float
    cpu_limited_resource_fraction: float
    capture_memory_bytes: int
    pretrigger_ring_bytes: int
    async_queue_bytes: int
    hot_symbol_state_bytes: int
    derived_hot_symbol_capacity: int
    max_writer_threads: int
    disk_quota_bytes: int
    sustained_write_budget_bytes_per_second: int
    max_queue_events: int
    max_ring_events: int
    max_gap_keys: int
    raw_retention_days: int
    derived_retention_days: int

    @property
    def budget_sha256(self) -> str:
        return sha256_json(asdict(self))


CAPTURE_RESOURCE_BINDING_SCHEMA_VERSION = "chili-replay-capture-resource-binding-v1"


@dataclass(frozen=True)
class CaptureResourceBinding:
    """Exact benchmark measurement, policy, and resolved finite limits.

    The binding is constructed from the measured host record and policy rather
    than accepting caller-supplied hashes.  Runtime objects retain this exact
    value so their health output and the append-only store audit name the same
    resource provenance.
    """

    measurement: CaptureResourceMeasurement
    policy: CaptureBudgetPolicy
    budget: ResolvedCaptureBudget
    schema_version: str = CAPTURE_RESOURCE_BINDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURE_RESOURCE_BINDING_SCHEMA_VERSION:
            raise CaptureContractError("unsupported capture resource-binding schema")
        if not isinstance(self.measurement, CaptureResourceMeasurement):
            raise CaptureContractError("resource binding measurement is malformed")
        if not isinstance(self.policy, CaptureBudgetPolicy):
            raise CaptureContractError("resource binding policy is malformed")
        if not isinstance(self.budget, ResolvedCaptureBudget):
            raise CaptureContractError("resource binding budget is malformed")
        expected = resolve_capture_budget(self.measurement, self.policy)
        if self.budget != expected:
            raise CaptureContractError(
                "resolved capture budget does not match measurement and policy"
            )

    @classmethod
    def resolve(
        cls,
        measurement: CaptureResourceMeasurement,
        policy: CaptureBudgetPolicy,
    ) -> "CaptureResourceBinding":
        return cls(
            measurement=measurement,
            policy=policy,
            budget=resolve_capture_budget(measurement, policy),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "measurement": asdict(self.measurement),
            "measurement_sha256": self.measurement.measurement_sha256,
            "policy": asdict(self.policy),
            "policy_sha256": self.policy.policy_sha256,
            "budget": asdict(self.budget),
            "budget_sha256": self.budget.budget_sha256,
        }

    @property
    def binding_sha256(self) -> str:
        return sha256_json(self.to_record())

    @property
    def hashes(self) -> dict[str, str]:
        return {
            "measurement_sha256": self.measurement.measurement_sha256,
            "policy_sha256": self.policy.policy_sha256,
            "budget_sha256": self.budget.budget_sha256,
            "binding_sha256": self.binding_sha256,
        }


def resolve_capture_budget(
    measurement: CaptureResourceMeasurement,
    policy: CaptureBudgetPolicy,
) -> ResolvedCaptureBudget:
    """Resolve finite budgets from measured headroom or refuse to start."""

    memory_headroom = measurement.available_memory_bytes - policy.memory_reserve_bytes
    disk_headroom = measurement.disk_free_bytes - policy.disk_reserve_bytes
    if memory_headroom <= 0:
        raise CaptureContractError("capture unavailable: memory reserve consumes headroom")
    if disk_headroom <= 0:
        raise CaptureContractError("capture unavailable: disk reserve consumes headroom")
    if measurement.sustained_append_bytes_per_second <= 0:
        raise CaptureContractError("capture unavailable: write throughput is unmeasured")

    cpu_headroom = policy.max_average_cpu_percent - measurement.average_cpu_percent
    if cpu_headroom <= 0:
        raise CaptureContractError(
            "capture unavailable: measured CPU leaves no policy headroom"
        )
    if measurement.average_cpu_percent >= policy.pressure_cpu_enter_percent:
        raise CaptureContractError(
            "capture unavailable: measured CPU is already inside pressure state"
        )
    total_idle_cpu = 100.0 - measurement.average_cpu_percent
    if total_idle_cpu <= 0:
        raise CaptureContractError("capture unavailable: measured CPU is saturated")
    capture_cpu_budget = cpu_headroom * policy.capture_fraction_of_cpu_headroom
    cpu_limited_fraction = min(1.0, capture_cpu_budget / total_idle_cpu)
    if cpu_limited_fraction <= 0:
        raise CaptureContractError("capture unavailable: resolved CPU budget is empty")

    capture_memory = int(
        memory_headroom
        * min(
            policy.capture_fraction_of_memory_headroom,
            cpu_limited_fraction,
        )
    )
    ring_bytes = int(capture_memory * policy.ring_fraction_of_capture_memory)
    queue_bytes = int(capture_memory * policy.queue_fraction_of_capture_memory)
    hot_bytes = capture_memory - ring_bytes - queue_bytes
    hot_capacity = hot_bytes // policy.calibrated_hot_symbol_bytes
    if min(capture_memory, ring_bytes, queue_bytes, hot_bytes) <= 0:
        raise CaptureContractError("capture unavailable: resolved memory budget is empty")
    if hot_capacity <= 0:
        raise CaptureContractError(
            "capture unavailable: measured headroom cannot hold one calibrated hot symbol"
        )

    disk_quota = int(disk_headroom * policy.capture_fraction_of_disk_headroom)
    write_budget = int(
        measurement.sustained_append_bytes_per_second
        * min(
            policy.capture_fraction_of_measured_write_bandwidth,
            cpu_limited_fraction,
        )
    )
    if disk_quota <= 0 or write_budget <= 0:
        raise CaptureContractError("capture unavailable: resolved disk budget is empty")
    measured_writer_threads = max(
        1,
        int(
            math.floor(
                measurement.logical_cpu_count * capture_cpu_budget / 100.0
            )
        ),
    )
    max_writer_threads = min(
        measurement.logical_cpu_count,
        int(hot_capacity),
        measured_writer_threads,
    )
    if max_writer_threads <= 0:
        raise CaptureContractError(
            "capture unavailable: measured headroom cannot support a writer"
        )
    return ResolvedCaptureBudget(
        measurement_sha256=measurement.measurement_sha256,
        policy_sha256=policy.policy_sha256,
        measured_average_cpu_percent=measurement.average_cpu_percent,
        cpu_headroom_to_ceiling_percent=cpu_headroom,
        capture_cpu_budget_percent=capture_cpu_budget,
        cpu_limited_resource_fraction=cpu_limited_fraction,
        capture_memory_bytes=capture_memory,
        pretrigger_ring_bytes=ring_bytes,
        async_queue_bytes=queue_bytes,
        hot_symbol_state_bytes=hot_bytes,
        derived_hot_symbol_capacity=int(hot_capacity),
        max_writer_threads=int(max_writer_threads),
        disk_quota_bytes=disk_quota,
        sustained_write_budget_bytes_per_second=write_budget,
        max_queue_events=policy.max_queue_events,
        max_ring_events=policy.max_ring_events,
        max_gap_keys=policy.max_gap_keys,
        raw_retention_days=policy.raw_retention_days,
        derived_retention_days=policy.derived_retention_days,
    )


CAPTURE_PRESSURE_SAMPLE_SCHEMA_VERSION = "chili-replay-capture-pressure-sample-v1"


@dataclass(frozen=True)
class CapturePressureSample:
    """One explicit host-pressure observation tied to an exact resource binding."""

    observed_at: datetime
    resource_binding_sha256: str
    cpu_percent: float
    available_memory_bytes: int
    disk_free_bytes: int
    write_latency_milliseconds: float
    schema_version: str = CAPTURE_PRESSURE_SAMPLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURE_PRESSURE_SAMPLE_SCHEMA_VERSION:
            raise CaptureContractError("unsupported capture pressure-sample schema")
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        object.__setattr__(
            self,
            "resource_binding_sha256",
            _validated_sha256(
                self.resource_binding_sha256,
                "pressure sample resource_binding_sha256",
            ),
        )
        for name in ("cpu_percent", "write_latency_milliseconds"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise CaptureContractError(f"pressure sample {name} is invalid")
            object.__setattr__(self, name, value)
        if self.cpu_percent > 100:
            raise CaptureContractError("pressure sample CPU cannot exceed 100")
        for name in ("available_memory_bytes", "disk_free_bytes"):
            value = int(getattr(self, name))
            if value < 0:
                raise CaptureContractError(f"pressure sample {name} is invalid")
            object.__setattr__(self, name, value)

    def to_record(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "observed_at": _iso(self.observed_at),
        }

    @property
    def sample_sha256(self) -> str:
        return sha256_json(self.to_record())


class CaptureAdaptivePressureController:
    """Bounded hysteretic admission gate for required full-fidelity capture.

    Pressure never selects a lower-fidelity mode.  Once the configured entry
    evidence is observed, callers must reject required inputs and emit an
    explicit coverage gap until every resource has remained inside the stricter
    recovery envelope for the configured recovery evidence count.
    """

    def __init__(
        self,
        binding: CaptureResourceBinding,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(binding, CaptureResourceBinding):
            raise CaptureContractError("pressure controller binding is malformed")
        if not callable(monotonic_clock):
            raise CaptureContractError("pressure controller clock is malformed")
        self.binding = binding
        self._monotonic_clock = monotonic_clock
        self._lock = threading.RLock()
        self._pressured = False
        self._enter_streak = 0
        self._enter_reasons: set[str] = set()
        self._recovery_streak = 0
        self._active_reasons: tuple[str, ...] = ()
        self._last_sample: CapturePressureSample | None = None
        self._last_sample_monotonic: float | None = None
        self._sample_count = 0
        self._transition_count = 0

    def _entry_reasons(self, sample: CapturePressureSample) -> tuple[str, ...]:
        policy = self.binding.policy
        reasons: list[str] = []
        if sample.cpu_percent >= policy.pressure_cpu_enter_percent:
            reasons.append("cpu")
        if sample.available_memory_bytes <= (
            policy.memory_reserve_bytes + policy.pressure_memory_enter_margin_bytes
        ):
            reasons.append("memory")
        if sample.disk_free_bytes <= (
            policy.disk_reserve_bytes + policy.pressure_disk_enter_margin_bytes
        ):
            reasons.append("disk")
        if (
            sample.write_latency_milliseconds
            >= policy.pressure_write_latency_enter_milliseconds
        ):
            reasons.append("write_latency")
        return tuple(reasons)

    def _inside_recovery_envelope(self, sample: CapturePressureSample) -> bool:
        policy = self.binding.policy
        return (
            sample.cpu_percent <= policy.pressure_cpu_exit_percent
            and sample.available_memory_bytes
            >= policy.memory_reserve_bytes
            + policy.pressure_memory_exit_margin_bytes
            and sample.disk_free_bytes
            >= policy.disk_reserve_bytes + policy.pressure_disk_exit_margin_bytes
            and sample.write_latency_milliseconds
            <= policy.pressure_write_latency_exit_milliseconds
        )

    def observe(self, sample: CapturePressureSample) -> dict[str, Any]:
        if not isinstance(sample, CapturePressureSample):
            raise CaptureContractError("pressure observation is malformed")
        if sample.resource_binding_sha256 != self.binding.binding_sha256:
            raise CaptureContractError("pressure observation binding mismatch")
        with self._lock:
            observed_monotonic = float(self._monotonic_clock())
            if not math.isfinite(observed_monotonic):
                raise CaptureContractError("pressure controller clock is non-finite")
            if (
                self._last_sample_monotonic is not None
                and observed_monotonic < self._last_sample_monotonic
            ):
                raise CaptureContractError("pressure controller clock moved backwards")
            if self._last_sample is not None and sample.observed_at <= self._last_sample.observed_at:
                raise CaptureContractError(
                    "pressure observations must have strictly increasing clocks"
                )
            self._last_sample = sample
            self._last_sample_monotonic = observed_monotonic
            self._sample_count += 1
            reasons = self._entry_reasons(sample)
            if not self._pressured:
                self._recovery_streak = 0
                if reasons:
                    self._enter_streak += 1
                    self._enter_reasons.update(reasons)
                else:
                    self._enter_streak = 0
                    self._enter_reasons.clear()
                if self._enter_streak >= self.binding.policy.pressure_enter_samples:
                    self._pressured = True
                    self._active_reasons = tuple(
                        reason
                        for reason in ("cpu", "memory", "disk", "write_latency")
                        if reason in self._enter_reasons
                    )
                    self._enter_streak = 0
                    self._enter_reasons.clear()
                    self._transition_count += 1
            else:
                if reasons:
                    combined = set(self._active_reasons).union(reasons)
                    self._active_reasons = tuple(
                        reason
                        for reason in ("cpu", "memory", "disk", "write_latency")
                        if reason in combined
                    )
                if self._inside_recovery_envelope(sample):
                    self._recovery_streak += 1
                else:
                    self._recovery_streak = 0
                if (
                    self._recovery_streak
                    >= self.binding.policy.pressure_recovery_samples
                ):
                    self._pressured = False
                    self._active_reasons = ()
                    self._recovery_streak = 0
                    self._transition_count += 1
            return self.health()

    @property
    def required_full_fidelity_admissible(self) -> bool:
        with self._lock:
            return self._current_rejection_reason() is None

    @property
    def rejection_reason(self) -> str | None:
        with self._lock:
            return self._current_rejection_reason()

    def _current_rejection_reason(self) -> str | None:
        if self._last_sample is None or self._last_sample_monotonic is None:
            return "capture_resource_pressure_sample_unavailable"
        now = float(self._monotonic_clock())
        if not math.isfinite(now) or now < self._last_sample_monotonic:
            return "capture_resource_pressure_sample_clock_invalid"
        if (
            now - self._last_sample_monotonic
            > self.binding.policy.pressure_sample_max_age_seconds
        ):
            return "capture_resource_pressure_sample_stale"
        if not self._pressured:
            return None
        else:
            suffix = "_".join(self._active_reasons) or "unknown"
            return f"capture_resource_pressure_{suffix}"

    def health(self) -> dict[str, Any]:
        with self._lock:
            sample = self._last_sample
            rejection = self._current_rejection_reason()
            if rejection == "capture_resource_pressure_sample_unavailable":
                pressure_state = "unobserved_fail_closed"
            elif rejection == "capture_resource_pressure_sample_stale":
                pressure_state = "stale_fail_closed"
            elif rejection is not None:
                pressure_state = "failed_closed"
            else:
                pressure_state = "normal"
            return {
                "resource_hashes": self.binding.hashes,
                "required_full_fidelity_admissible": rejection is None,
                "pressure_state": pressure_state,
                "rejection_reason": rejection,
                "active_reasons": self._active_reasons,
                "entry_streak": self._enter_streak,
                "recovery_streak": self._recovery_streak,
                "sample_count": self._sample_count,
                "transition_count": self._transition_count,
                "last_sample_sha256": sample.sample_sha256 if sample else None,
                "last_observed_at": _iso(sample.observed_at) if sample else None,
                "sample_age_seconds": (
                    max(0.0, float(self._monotonic_clock()) - self._last_sample_monotonic)
                    if self._last_sample_monotonic is not None
                    else None
                ),
                "thresholds": {
                    "cpu_enter_percent": self.binding.policy.pressure_cpu_enter_percent,
                    "cpu_exit_percent": self.binding.policy.pressure_cpu_exit_percent,
                    "memory_enter_bytes": self.binding.policy.memory_reserve_bytes
                    + self.binding.policy.pressure_memory_enter_margin_bytes,
                    "memory_exit_bytes": self.binding.policy.memory_reserve_bytes
                    + self.binding.policy.pressure_memory_exit_margin_bytes,
                    "disk_enter_bytes": self.binding.policy.disk_reserve_bytes
                    + self.binding.policy.pressure_disk_enter_margin_bytes,
                    "disk_exit_bytes": self.binding.policy.disk_reserve_bytes
                    + self.binding.policy.pressure_disk_exit_margin_bytes,
                    "write_latency_enter_milliseconds": (
                        self.binding.policy.pressure_write_latency_enter_milliseconds
                    ),
                    "write_latency_exit_milliseconds": (
                        self.binding.policy.pressure_write_latency_exit_milliseconds
                    ),
                },
            }


@dataclass(frozen=True)
class HotSymbolLease:
    identity_sha256: str
    symbol: str
    lease_token: str
    acquired_at: datetime
    resource_binding_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "identity_sha256",
            _validated_sha256(self.identity_sha256, "hot-symbol identity_sha256"),
        )
        symbol = str(self.symbol or "").strip().upper()
        if not symbol:
            raise CaptureContractError("hot-symbol lease symbol is required")
        object.__setattr__(self, "symbol", symbol)
        try:
            token = str(uuid.UUID(str(self.lease_token)))
        except ValueError as exc:
            raise CaptureContractError("hot-symbol lease token is malformed") from exc
        object.__setattr__(self, "lease_token", token)
        object.__setattr__(self, "acquired_at", _utc(self.acquired_at, "acquired_at"))
        object.__setattr__(
            self,
            "resource_binding_sha256",
            _validated_sha256(
                self.resource_binding_sha256, "hot-symbol resource_binding_sha256"
            ),
        )


@dataclass(frozen=True)
class HotSymbolAdmission:
    lease: HotSymbolLease | None
    gap: CoverageGap | None

    def __post_init__(self) -> None:
        if (self.lease is None) == (self.gap is None):
            raise CaptureContractError(
                "hot-symbol admission requires exactly one lease or coverage gap"
            )


class BoundedHotSymbolLeases:
    """Exact-binding hot-symbol capacity with explicit fail-closed rejection."""

    def __init__(
        self,
        *,
        identity: CaptureRunIdentity,
        resource_binding: CaptureResourceBinding,
        pressure_controller: CaptureAdaptivePressureController | None = None,
    ) -> None:
        if not isinstance(identity, CaptureRunIdentity):
            raise CaptureContractError("hot-symbol run identity is malformed")
        if not isinstance(resource_binding, CaptureResourceBinding):
            raise CaptureContractError("hot-symbol resource binding is malformed")
        if pressure_controller is not None and pressure_controller.binding != resource_binding:
            raise CaptureContractError("hot-symbol pressure binding mismatch")
        self.identity = identity
        self.resource_binding = resource_binding
        self.pressure_controller = pressure_controller
        self.capacity = resource_binding.budget.derived_hot_symbol_capacity
        self._lock = threading.RLock()
        self._active_by_symbol: dict[str, HotSymbolLease] = {}
        self._released_token_order: deque[str] = deque()
        self._released_tokens: set[str] = set()
        self._rejections: dict[str, int] = defaultdict(int)

    @staticmethod
    def _gap(
        *, symbol: str, requested_at: datetime, reason: str, stream: CaptureStream
    ) -> CoverageGap:
        return CoverageGap(
            stream=stream,
            symbol=symbol,
            reason=reason,
            first_available_at=requested_at,
            last_available_at=requested_at,
            lost_count=1,
        )

    def acquire(
        self,
        symbol: str,
        *,
        requested_at: datetime,
        required_stream: CaptureStream = CaptureStream.L2_DEPTH_DELTA,
    ) -> HotSymbolAdmission:
        normalized = str(symbol or "").strip().upper()
        at = _utc(requested_at, "requested_at")
        if not normalized:
            raise CaptureContractError("hot-symbol admission requires a symbol")
        if not isinstance(required_stream, CaptureStream):
            raise CaptureContractError("hot-symbol required stream is malformed")
        with self._lock:
            existing = self._active_by_symbol.get(normalized)
            if existing is not None:
                return HotSymbolAdmission(lease=existing, gap=None)
            rejection = (
                self.pressure_controller.rejection_reason
                if self.pressure_controller is not None
                else None
            )
            if rejection is not None:
                reason = f"hot_symbol_{rejection}"
                self._rejections[reason] += 1
                return HotSymbolAdmission(
                    lease=None,
                    gap=self._gap(
                        symbol=normalized,
                        requested_at=at,
                        reason=reason,
                        stream=required_stream,
                    ),
                )
            if len(self._active_by_symbol) >= self.capacity:
                reason = "hot_symbol_measured_capacity_exhausted"
                self._rejections[reason] += 1
                return HotSymbolAdmission(
                    lease=None,
                    gap=self._gap(
                        symbol=normalized,
                        requested_at=at,
                        reason=reason,
                        stream=required_stream,
                    ),
                )
            lease = HotSymbolLease(
                identity_sha256=self.identity.identity_sha256,
                symbol=normalized,
                lease_token=str(uuid.uuid4()),
                acquired_at=at,
                resource_binding_sha256=self.resource_binding.binding_sha256,
            )
            self._active_by_symbol[normalized] = lease
            return HotSymbolAdmission(lease=lease, gap=None)

    def release(self, lease: HotSymbolLease) -> bool:
        if not isinstance(lease, HotSymbolLease):
            raise CaptureContractError("hot-symbol release lease is malformed")
        with self._lock:
            if lease.lease_token in self._released_tokens:
                return False
            if (
                lease.identity_sha256 != self.identity.identity_sha256
                or lease.resource_binding_sha256
                != self.resource_binding.binding_sha256
            ):
                raise CaptureContractError("hot-symbol release binding mismatch")
            active = self._active_by_symbol.get(lease.symbol)
            if active != lease:
                raise CaptureContractError("hot-symbol release token is not active")
            del self._active_by_symbol[lease.symbol]
            self._released_tokens.add(lease.lease_token)
            self._released_token_order.append(lease.lease_token)
            while len(self._released_token_order) > self.capacity:
                expired = self._released_token_order.popleft()
                self._released_tokens.discard(expired)
            return True

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "resource_hashes": self.resource_binding.hashes,
                "capacity": self.capacity,
                "active": len(self._active_by_symbol),
                "active_symbols": tuple(sorted(self._active_by_symbol)),
                "rejections": dict(sorted(self._rejections.items())),
                "pressure": (
                    self.pressure_controller.health()
                    if self.pressure_controller is not None
                    else None
                ),
            }


def approximate_event_bytes(event: CaptureEvent) -> int:
    return event.canonical_size_bytes


_EMPTY_ACCUMULATOR_SHA256 = "0" * 64


def _accumulate_sha256(current: str, payload: Mapping[str, Any]) -> str:
    """Bounded, order-independent accumulator for unique capture records."""

    prior = int(current, 16)
    item = int.from_bytes(hashlib.sha256(canonical_json_bytes(payload)).digest(), "big")
    return f"{(prior + item) % (1 << 256):064x}"


def _merge_sha256_accumulators(values: Iterable[str]) -> str:
    merged = _EMPTY_ACCUMULATOR_SHA256
    for value in values:
        raw = int(_validated_sha256(value, "capture accumulator"), 16)
        merged = f"{(int(merged, 16) + raw) % (1 << 256):064x}"
    return merged


def _event_accumulator_add(current: str, event: CaptureEvent) -> str:
    return _accumulate_sha256(
        current,
        {"sequence": event.sequence, "event_sha256": event.event_sha256},
    )


def _gap_accumulator_add(
    current: str,
    identity: CaptureRunIdentity,
    gap: CoverageGap,
) -> str:
    return _accumulate_sha256(
        current,
        {"identity": identity.to_dict(), "gap": gap.to_dict()},
    )


@dataclass(frozen=True)
class PromotionBatch:
    symbol: str
    promoted_at: datetime
    events: tuple[CaptureEvent, ...]
    gaps: tuple[CoverageGap, ...]
    promotion_id: str | None = None
    source_identity_sha256: str | None = None
    resource_binding_sha256: str | None = None
    inventory_sha256: str | None = None
    admission_handoff_sha256: str | None = None


@dataclass(frozen=True)
class PreTriggerRetainResult:
    retained: bool
    event: CaptureEvent | None
    unchanged: bool
    coverage_gap_recorded: bool
    disposition: str

    def __post_init__(self) -> None:
        if self.retained != (self.event is not None):
            raise CaptureContractError(
                "pre-trigger retain result event/retained state is inconsistent"
            )
        if self.unchanged and (self.retained or self.coverage_gap_recorded):
            raise CaptureContractError(
                "unchanged pre-trigger result cannot retain or report a loss"
            )
        if not str(self.disposition or "").strip():
            raise CaptureContractError("pre-trigger retain disposition is required")


_PRETRIGGER_ADMISSION_HANDOFF_TOKEN = object()
_PROMOTION_TRANSFER_TOKEN = object()
_PRETRIGGER_ADMISSION_HANDOFF_KEY = secrets.token_bytes(32)
_PROMOTION_TRANSFER_KEY = secrets.token_bytes(32)


@dataclass(frozen=True)
class PreTriggerAdmissionHandoff:
    """Exact causal admission input retained before a target run exists."""

    handoff_id: str
    source_identity_sha256: str
    resource_binding_sha256: str | None
    symbol: str
    admission_event_sequence: int
    admission_event_sha256: str
    admission_available_at: datetime
    inventory_sha256: str
    _verification_token: object = field(default=None, repr=False, compare=False)
    _verification_sha256: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_token is not _PRETRIGGER_ADMISSION_HANDOFF_TOKEN:
            raise CaptureContractError(
                "admission handoff was not issued by the bounded pre-trigger ring"
            )
        try:
            object.__setattr__(self, "handoff_id", str(uuid.UUID(self.handoff_id)))
        except ValueError as exc:
            raise CaptureContractError("admission handoff id is malformed") from exc
        object.__setattr__(
            self,
            "source_identity_sha256",
            _validated_sha256(
                self.source_identity_sha256, "admission handoff source identity"
            ),
        )
        if self.resource_binding_sha256 is not None:
            object.__setattr__(
                self,
                "resource_binding_sha256",
                _validated_sha256(
                    self.resource_binding_sha256,
                    "admission handoff resource binding",
                ),
            )
        symbol = str(self.symbol or "").strip().upper()
        if not symbol:
            raise CaptureContractError("admission handoff symbol is required")
        object.__setattr__(self, "symbol", symbol)
        if (
            isinstance(self.admission_event_sequence, bool)
            or int(self.admission_event_sequence) <= 0
        ):
            raise CaptureContractError(
                "admission handoff event sequence must be positive"
            )
        object.__setattr__(
            self, "admission_event_sequence", int(self.admission_event_sequence)
        )
        object.__setattr__(
            self,
            "admission_event_sha256",
            _validated_sha256(
                self.admission_event_sha256, "admission handoff event"
            ),
        )
        object.__setattr__(
            self,
            "admission_available_at",
            _utc(self.admission_available_at, "admission handoff available_at"),
        )
        object.__setattr__(
            self,
            "inventory_sha256",
            _validated_sha256(self.inventory_sha256, "admission handoff inventory"),
        )
        if self.inventory_sha256 != sha256_json(self.inventory_record()):
            raise CaptureContractError("admission handoff inventory hash mismatch")
        expected_verification = hmac.new(
            _PRETRIGGER_ADMISSION_HANDOFF_KEY,
            canonical_json_bytes(
                {
                    "kind": "pretrigger_admission_handoff",
                    "inventory_sha256": self.inventory_sha256,
                }
            ),
            hashlib.sha256,
        ).hexdigest()
        if not self._verification_sha256 or not hmac.compare_digest(
            self._verification_sha256, expected_verification
        ):
            raise CaptureContractError("admission handoff capability is invalid")

    def inventory_record(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id,
            "source_identity_sha256": self.source_identity_sha256,
            "resource_binding_sha256": self.resource_binding_sha256,
            "symbol": self.symbol,
            "admission_event_sequence": self.admission_event_sequence,
            "admission_event_sha256": self.admission_event_sha256,
            "admission_available_at": _iso(self.admission_available_at),
        }

    @property
    def handoff_sha256(self) -> str:
        return sha256_json(
            {
                **self.inventory_record(),
                "inventory_sha256": self.inventory_sha256,
            }
        )


@dataclass(frozen=True)
class PromotionTransfer:
    """Non-destructive symbol snapshot awaiting explicit commit or abort."""

    promotion_id: str
    source_identity_sha256: str
    resource_binding_sha256: str | None
    symbol: str
    promoted_at: datetime
    events: tuple[CaptureEvent, ...]
    gaps: tuple[CoverageGap, ...]
    inventory_sha256: str
    admission_handoff_sha256: str | None = None
    _verification_token: object = field(default=None, repr=False, compare=False)
    _verification_sha256: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_token is not _PROMOTION_TRANSFER_TOKEN:
            raise CaptureContractError(
                "promotion transfer was not issued by the bounded pre-trigger ring"
            )
        try:
            object.__setattr__(self, "promotion_id", str(uuid.UUID(self.promotion_id)))
        except ValueError as exc:
            raise CaptureContractError("promotion transfer id is malformed") from exc
        object.__setattr__(
            self,
            "source_identity_sha256",
            _validated_sha256(
                self.source_identity_sha256, "promotion source identity"
            ),
        )
        if self.resource_binding_sha256 is not None:
            object.__setattr__(
                self,
                "resource_binding_sha256",
                _validated_sha256(
                    self.resource_binding_sha256, "promotion resource binding"
                ),
            )
        symbol = str(self.symbol or "").strip().upper()
        if not symbol:
            raise CaptureContractError("promotion transfer symbol is required")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(
            self, "promoted_at", _utc(self.promoted_at, "promotion.promoted_at")
        )
        events = tuple(self.events)
        gaps = tuple(self.gaps)
        if any(
            not isinstance(event, CaptureEvent)
            or event.identity.identity_sha256 != self.source_identity_sha256
            or event.symbol != symbol
            or event.clocks.available_at > self.promoted_at
            for event in events
        ):
            raise CaptureContractError("promotion transfer event inventory is invalid")
        if any(not isinstance(gap, CoverageGap) for gap in gaps):
            raise CaptureContractError("promotion transfer gap inventory is invalid")
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "gaps", gaps)
        object.__setattr__(
            self,
            "inventory_sha256",
            _validated_sha256(self.inventory_sha256, "promotion inventory"),
        )
        if self.admission_handoff_sha256 is not None:
            object.__setattr__(
                self,
                "admission_handoff_sha256",
                _validated_sha256(
                    self.admission_handoff_sha256,
                    "promotion admission handoff",
                ),
            )
        expected = sha256_json(
            {
                "promotion_id": self.promotion_id,
                "source_identity_sha256": self.source_identity_sha256,
                "resource_binding_sha256": self.resource_binding_sha256,
                "symbol": self.symbol,
                "promoted_at": _iso(self.promoted_at),
                "events": [
                    {
                        "sequence": event.sequence,
                        "event_sha256": event.event_sha256,
                    }
                    for event in events
                ],
                "gaps": [gap.to_dict() for gap in gaps],
                "admission_handoff_sha256": self.admission_handoff_sha256,
            }
        )
        if self.inventory_sha256 != expected:
            raise CaptureContractError("promotion transfer inventory hash mismatch")
        expected_verification = hmac.new(
            _PROMOTION_TRANSFER_KEY,
            canonical_json_bytes(
                {
                    "kind": "pretrigger_promotion_transfer",
                    "inventory_sha256": self.inventory_sha256,
                }
            ),
            hashlib.sha256,
        ).hexdigest()
        if not self._verification_sha256 or not hmac.compare_digest(
            self._verification_sha256, expected_verification
        ):
            raise CaptureContractError("promotion transfer capability is invalid")


@dataclass
class _EvictionAccumulator:
    stream: CaptureStream
    symbol: str | None
    reason: str
    first_available_at: datetime
    last_available_at: datetime
    lost_count: int = 1

    def add(self, available_at: datetime) -> None:
        self.add_range(available_at, available_at, lost_count=1)

    def add_range(
        self,
        first_available_at: datetime,
        last_available_at: datetime,
        *,
        lost_count: int,
    ) -> None:
        self.first_available_at = min(self.first_available_at, first_available_at)
        self.last_available_at = max(self.last_available_at, last_available_at)
        self.lost_count += int(lost_count)

    def freeze(self) -> CoverageGap:
        return CoverageGap(
            stream=self.stream,
            symbol=self.symbol,
            reason=self.reason,
            first_available_at=self.first_available_at,
            last_available_at=self.last_available_at,
            lost_count=self.lost_count,
        )


class BoundedPreTriggerRing:
    """Broad-universe ring with atomic hot-symbol prehistory promotion."""

    def __init__(
        self,
        *,
        horizon: timedelta,
        max_events: int,
        max_bytes: int,
        per_symbol_max_events: int,
        max_gap_keys: int = 4_096,
        max_change_keys: int | None = None,
        resource_binding: CaptureResourceBinding | None = None,
        pressure_controller: CaptureAdaptivePressureController | None = None,
    ) -> None:
        if horizon.total_seconds() <= 0:
            raise CaptureContractError("pre-trigger horizon must be positive")
        change_key_limit = int(max_change_keys or max_gap_keys)
        if min(
            max_events,
            max_bytes,
            per_symbol_max_events,
            max_gap_keys,
            change_key_limit,
        ) <= 0:
            raise CaptureContractError("pre-trigger limits must be positive")
        if resource_binding is not None:
            if not isinstance(resource_binding, CaptureResourceBinding):
                raise CaptureContractError("pre-trigger resource binding is malformed")
            if max_events > resource_binding.budget.max_ring_events:
                raise CaptureContractError(
                    "pre-trigger event limit exceeds resolved resource budget"
                )
            if max_bytes > resource_binding.budget.pretrigger_ring_bytes:
                raise CaptureContractError(
                    "pre-trigger byte limit exceeds resolved resource budget"
                )
        if pressure_controller is not None:
            if not isinstance(pressure_controller, CaptureAdaptivePressureController):
                raise CaptureContractError(
                    "pre-trigger pressure controller is malformed"
                )
            if resource_binding is None or pressure_controller.binding != resource_binding:
                raise CaptureContractError("pre-trigger pressure binding mismatch")
        self.horizon = horizon
        self.max_events = int(max_events)
        self.max_bytes = int(max_bytes)
        self.per_symbol_max_events = int(per_symbol_max_events)
        self.max_gap_keys = int(max_gap_keys)
        self.max_change_keys = change_key_limit
        self.resource_binding = resource_binding
        self.pressure_controller = pressure_controller
        self._by_symbol: dict[str, deque[tuple[CaptureEvent, int]]] = defaultdict(deque)
        self._global: list[tuple[datetime, int, str]] = []
        self._active: dict[int, tuple[str, CaptureEvent, int]] = {}
        self._evictions: dict[
            tuple[str | None, CaptureStream, str], _EvictionAccumulator
        ] = {}
        self._eviction_overflow: _EvictionAccumulator | None = None
        self._bytes = 0
        self._identity_sha256: str | None = None
        self._next_provisional_sequence = 1
        self._change_state_by_key: OrderedDict[str, tuple[str, int]] = OrderedDict()
        self._change_keys_by_sequence: dict[int, set[str]] = defaultdict(set)
        self._unchanged_observations = 0
        self._change_key_capacity_rejections = 0
        self._admission_handoffs: dict[str, PreTriggerAdmissionHandoff] = {}
        self._admission_handoff_by_symbol: dict[str, str] = {}
        self._pending_transfers: dict[str, PromotionTransfer] = {}
        self._transfer_by_symbol: dict[str, str] = {}
        self._pending_transfer_sequences: set[int] = set()
        self._invalidated_transfers: set[str] = set()
        self._max_pending_transfers = (
            resource_binding.budget.derived_hot_symbol_capacity
            if resource_binding is not None
            else max(1, min(max_events, 64))
        )
        self._lock = threading.RLock()

    @classmethod
    def from_resource_binding(
        cls,
        binding: CaptureResourceBinding,
        *,
        horizon: timedelta,
        per_symbol_max_events: int,
        pressure_controller: CaptureAdaptivePressureController | None = None,
    ) -> "BoundedPreTriggerRing":
        if not isinstance(binding, CaptureResourceBinding):
            raise CaptureContractError("pre-trigger resource binding is malformed")
        return cls(
            horizon=horizon,
            max_events=binding.budget.max_ring_events,
            max_bytes=binding.budget.pretrigger_ring_bytes,
            per_symbol_max_events=min(
                int(per_symbol_max_events), binding.budget.max_ring_events
            ),
            max_gap_keys=binding.budget.max_gap_keys,
            max_change_keys=binding.budget.max_gap_keys,
            resource_binding=binding,
            pressure_controller=pressure_controller,
        )

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self._active)

    @property
    def approximate_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "event_count": len(self._active),
                "approximate_bytes": self._bytes,
                "max_events": self.max_events,
                "max_bytes": self.max_bytes,
                "per_symbol_max_events": self.per_symbol_max_events,
                "pending_gap_keys": len(self._evictions),
                "change_key_count": len(self._change_state_by_key),
                "max_change_keys": self.max_change_keys,
                "unchanged_observations_deduplicated": self._unchanged_observations,
                "change_key_capacity_rejections": (
                    self._change_key_capacity_rejections
                ),
                "admission_handoff_count": len(self._admission_handoffs),
                "gap_ledger_overflow": self._eviction_overflow is not None,
                "next_provisional_sequence": self._next_provisional_sequence,
                "pending_transfers": len(self._pending_transfers),
                "reserved_symbols": tuple(sorted(self._transfer_by_symbol)),
                "resource_hashes": (
                    self.resource_binding.hashes
                    if self.resource_binding is not None
                    else None
                ),
                "adaptive_pressure": (
                    self.pressure_controller.health()
                    if self.pressure_controller is not None
                    else None
                ),
            }

    def _remove(self, sequence: int, *, record_gap_reason: str | None) -> None:
        active = self._active.pop(sequence, None)
        if active is None:
            return
        symbol, event, size = active
        self._bytes -= size
        rows = self._by_symbol.get(symbol)
        if rows:
            if rows[0][0].sequence == sequence:
                rows.popleft()
            else:
                self._by_symbol[symbol] = deque(
                    row for row in rows if row[0].sequence != sequence
                )
            if not self._by_symbol[symbol]:
                self._by_symbol.pop(symbol, None)
        if record_gap_reason and sequence not in self._pending_transfer_sequences:
            self._record_eviction(event, symbol=symbol, reason=record_gap_reason)
        for key in self._change_keys_by_sequence.pop(sequence, set()):
            current = self._change_state_by_key.get(key)
            if current is not None and current[1] == sequence:
                self._change_state_by_key.pop(key, None)
        handoff_id = self._admission_handoff_by_symbol.get(symbol)
        if handoff_id is not None:
            handoff = self._admission_handoffs.get(handoff_id)
            if (
                handoff is not None
                and handoff.admission_event_sequence == sequence
            ):
                self._admission_handoffs.pop(handoff_id, None)
                self._admission_handoff_by_symbol.pop(symbol, None)

    def _record_eviction(
        self,
        event: CaptureEvent,
        *,
        symbol: str,
        reason: str,
    ) -> None:
        key = (symbol, event.stream, reason)
        existing = self._evictions.get(key)
        if existing is not None:
            existing.add(event.clocks.available_at)
            return
        if len(self._evictions) < self.max_gap_keys:
            self._evictions[key] = _EvictionAccumulator(
                stream=event.stream,
                symbol=symbol,
                reason=reason,
                first_available_at=event.clocks.available_at,
                last_available_at=event.clocks.available_at,
            )
            return

        # The specific-key ledger is itself bounded.  Once exhausted, preserve
        # every additional loss in a global ambiguity gap; never silently drop
        # fidelity merely to protect memory.
        if self._eviction_overflow is None:
            self._eviction_overflow = _EvictionAccumulator(
                stream=CaptureStream.COVERAGE_GAP,
                symbol=None,
                reason="pretrigger_gap_ledger_key_budget_overflow",
                first_available_at=event.clocks.available_at,
                last_available_at=event.clocks.available_at,
            )
        else:
            self._eviction_overflow.add(event.clocks.available_at)

    def record_gap(
        self,
        identity: CaptureRunIdentity,
        gap: CoverageGap,
    ) -> None:
        """Retain an upstream loss for the same atomic hot-promotion ledger.

        A provider/IPC queue can lose an input before a provisional event exists,
        so eviction-only accounting is insufficient.  Symbol-scoped gaps follow
        that symbol into promotion; a global gap (``symbol is None``) follows every
        promotion whose time window intersects it.  The ledger remains bounded,
        with overflow represented by the existing global ambiguity accumulator.
        """

        if not isinstance(identity, CaptureRunIdentity) or not isinstance(
            gap, CoverageGap
        ):
            raise CaptureContractError(
                "pre-trigger gap requires exact identity and CoverageGap values"
            )
        with self._lock:
            identity_sha256 = identity.identity_sha256
            if self._identity_sha256 not in (None, identity_sha256):
                raise CaptureContractError("pre-trigger gap identity mismatch")
            self._identity_sha256 = identity_sha256
            self._purge_expected_expiry(gap.last_available_at)
            key = (gap.symbol, gap.stream, gap.reason)
            existing = self._evictions.get(key)
            if existing is not None:
                existing.add_range(
                    gap.first_available_at,
                    gap.last_available_at,
                    lost_count=gap.lost_count,
                )
                return
            if len(self._evictions) < self.max_gap_keys:
                self._evictions[key] = _EvictionAccumulator(
                    stream=gap.stream,
                    symbol=gap.symbol,
                    reason=gap.reason,
                    first_available_at=gap.first_available_at,
                    last_available_at=gap.last_available_at,
                    lost_count=gap.lost_count,
                )
                return
            if self._eviction_overflow is None:
                self._eviction_overflow = _EvictionAccumulator(
                    stream=CaptureStream.COVERAGE_GAP,
                    symbol=None,
                    reason="pretrigger_gap_ledger_key_budget_overflow",
                    first_available_at=gap.first_available_at,
                    last_available_at=gap.last_available_at,
                    lost_count=gap.lost_count,
                )
            else:
                self._eviction_overflow.add_range(
                    gap.first_available_at,
                    gap.last_available_at,
                    lost_count=gap.lost_count,
                )

    def _compact_global_heap(self) -> None:
        threshold = max(self.max_events * 2, self.max_events + 1_024)
        if len(self._global) <= threshold:
            return
        self._global = [
            (event.clocks.available_at, sequence, event.event_sha256)
            for sequence, (_symbol, event, _size) in self._active.items()
        ]
        heapq.heapify(self._global)

    def _purge_expected_expiry(self, now: datetime) -> None:
        cutoff = now - self.horizon
        while self._global:
            available_at, sequence, event_sha256 = self._global[0]
            active = self._active.get(sequence)
            if active is None or active[1].event_sha256 != event_sha256:
                heapq.heappop(self._global)
                continue
            if available_at >= cutoff:
                break
            heapq.heappop(self._global)
            self._remove(sequence, record_gap_reason=None)
        for key, eviction in tuple(self._evictions.items()):
            if eviction.last_available_at < cutoff:
                self._evictions.pop(key, None)
        if (
            self._eviction_overflow is not None
            and self._eviction_overflow.last_available_at < cutoff
        ):
            self._eviction_overflow = None

    def add(self, event: CaptureEvent) -> bool:
        """Add without blocking on I/O; false means capacity loss was recorded."""

        symbol = str(event.symbol or "").strip().upper()
        if not symbol:
            raise CaptureContractError("pre-trigger events require a symbol")
        size = approximate_event_bytes(event)
        with self._lock:
            identity_sha256 = event.identity.identity_sha256
            if self._identity_sha256 not in (None, identity_sha256):
                raise CaptureContractError(
                    "pre-trigger ring cannot mix capture identities"
                )
            self._identity_sha256 = identity_sha256
            self._purge_expected_expiry(event.clocks.available_at)
            if event.sequence in self._active:
                raise CaptureContractError("duplicate sequence in pre-trigger ring")
            self._next_provisional_sequence = max(
                self._next_provisional_sequence, event.sequence + 1
            )
            if symbol in self._transfer_by_symbol:
                self._invalidated_transfers.add(self._transfer_by_symbol[symbol])
                self._record_eviction(
                    event,
                    symbol=symbol,
                    reason="pretrigger_symbol_promotion_in_progress",
                )
                return False
            if size > self.max_bytes:
                self._record_eviction(
                    event,
                    symbol=symbol,
                    reason="pretrigger_event_exceeds_byte_budget",
                )
                return False
            self._by_symbol[symbol].append((event, size))
            heapq.heappush(
                self._global,
                (event.clocks.available_at, event.sequence, event.event_sha256),
            )
            self._active[event.sequence] = (symbol, event, size)
            self._bytes += size

            while len(self._by_symbol.get(symbol, ())) > self.per_symbol_max_events:
                oldest, _ = min(
                    self._by_symbol[symbol],
                    key=lambda row: (
                        row[0].clocks.available_at,
                        row[0].sequence,
                        row[0].event_sha256,
                    ),
                )
                self._remove(
                    oldest.sequence,
                    record_gap_reason="pretrigger_per_symbol_capacity",
                )
            while len(self._active) > self.max_events or self._bytes > self.max_bytes:
                while self._global:
                    _, sequence, event_sha256 = heapq.heappop(self._global)
                    active = self._active.get(sequence)
                    if active is not None and active[1].event_sha256 == event_sha256:
                        self._remove(
                            sequence,
                            record_gap_reason="pretrigger_global_capacity",
                        )
                        break
            self._compact_global_heap()
            return event.sequence in self._active

    def retain_observation(
        self,
        *,
        identity: CaptureRunIdentity,
        stream: CaptureStream,
        provider: str,
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
        symbol: str,
        query: Mapping[str, Any] | None = None,
    ) -> tuple[bool, CaptureEvent]:
        """Construct and retain one globally sequenced provisional observation."""

        policy = STREAM_POLICIES[stream]
        if (
            policy.coverage_mode is CoverageMode.CHANGE_LOG
            and policy.content_dedup_allowed
        ):
            raise CaptureContractError(
                "broad change-log inputs require retain_change_observation and an exact change key"
            )

        with self._lock:
            event = CaptureEvent(
                identity=identity,
                sequence=self._next_provisional_sequence,
                stream=stream,
                provider=provider,
                payload=payload,
                clocks=clocks,
                symbol=symbol,
                query=query,
            )
            retained = self.add(event)
            return retained, event

    @staticmethod
    def _change_state_sha256(event: CaptureEvent) -> str:
        # A CHANGE_LOG records state transitions, not polling heartbeats.
        # Provider/source progress is proven independently by a watermark, so
        # advancing clocks alone must not consume the bounded broad ring.
        return sha256_json(
            {
                "stream": event.stream.value,
                "provider": event.provider,
                "symbol": event.symbol,
                "payload_sha256": event.payload_sha256,
                "query_sha256": event.query_sha256,
            }
        )

    def retain_change_observation(
        self,
        *,
        identity: CaptureRunIdentity,
        stream: CaptureStream,
        provider: str,
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
        symbol: str,
        change_key: str,
        query: Mapping[str, Any] | None = None,
    ) -> PreTriggerRetainResult:
        """Retain changed broad state without letting identical scans evict tape."""

        policy = STREAM_POLICIES[stream]
        if (
            policy.coverage_mode is not CoverageMode.CHANGE_LOG
            or not policy.content_dedup_allowed
        ):
            raise CaptureContractError(
                "retain_change_observation requires a deduplicable change-log stream"
            )
        normalized_symbol = str(symbol or "").strip().upper()
        raw_change_key = str(change_key or "").strip()
        if not normalized_symbol or not raw_change_key or len(raw_change_key) > 512:
            raise CaptureContractError(
                "pre-trigger change key and symbol are required and bounded"
            )
        with self._lock:
            if self._identity_sha256 not in (None, identity.identity_sha256):
                raise CaptureContractError(
                    "pre-trigger ring cannot mix capture identities"
                )
            self._identity_sha256 = identity.identity_sha256
            event = CaptureEvent(
                identity=identity,
                sequence=self._next_provisional_sequence,
                stream=stream,
                provider=provider,
                payload=payload,
                clocks=clocks,
                symbol=normalized_symbol,
                query=query,
            )
            bounded_key = sha256_json(
                {
                    "stream": stream.value,
                    "provider": event.provider,
                    "symbol": normalized_symbol,
                    "query_sha256": event.query_sha256,
                    "change_key": raw_change_key,
                }
            )
            state_sha256 = self._change_state_sha256(event)
            existing = self._change_state_by_key.get(bounded_key)
            if existing is not None and existing[0] == state_sha256:
                self._change_state_by_key.move_to_end(bounded_key)
                self._unchanged_observations += 1
                return PreTriggerRetainResult(
                    retained=False,
                    event=None,
                    unchanged=True,
                    coverage_gap_recorded=False,
                    disposition="unchanged_change_log_deduplicated",
                )
            if existing is None and len(self._change_state_by_key) >= self.max_change_keys:
                self._record_eviction(
                    event,
                    symbol=normalized_symbol,
                    reason="pretrigger_change_key_capacity_overflow",
                )
                self._change_key_capacity_rejections += 1
                return PreTriggerRetainResult(
                    retained=False,
                    event=None,
                    unchanged=False,
                    coverage_gap_recorded=True,
                    disposition="change_key_capacity_gap_pending_promotion",
                )
            retained = self.add(event)
            if not retained:
                return PreTriggerRetainResult(
                    retained=False,
                    event=None,
                    unchanged=False,
                    coverage_gap_recorded=True,
                    disposition="pretrigger_capacity_gap_pending_promotion",
                )
            if existing is not None:
                self._change_keys_by_sequence[existing[1]].discard(bounded_key)
            self._change_state_by_key[bounded_key] = (
                state_sha256,
                event.sequence,
            )
            self._change_state_by_key.move_to_end(bounded_key)
            self._change_keys_by_sequence[event.sequence].add(bounded_key)
            return PreTriggerRetainResult(
                retained=True,
                event=event,
                unchanged=False,
                coverage_gap_recorded=False,
                disposition="changed_state_retained_provisional",
            )

    def create_admission_handoff(
        self,
        event: CaptureEvent,
    ) -> PreTriggerAdmissionHandoff:
        """Bind an already-retained admission decision before run construction."""

        if not isinstance(event, CaptureEvent):
            raise CaptureContractError("admission handoff event is malformed")
        if event.stream is not CaptureStream.ADMISSION_ELIGIBILITY:
            raise CaptureContractError(
                "admission handoff requires an admission_eligibility event"
            )
        if event.symbol is None:
            raise CaptureContractError("admission handoff event requires a symbol")
        with self._lock:
            active = self._active.get(event.sequence)
            if active is None or active[1].event_sha256 != event.event_sha256:
                self._record_eviction(
                    event,
                    symbol=event.symbol,
                    reason="pretrigger_admission_handoff_source_unavailable",
                )
                raise CaptureContractError(
                    "admission handoff source is not retained in the broad ring"
                )
            prior_id = self._admission_handoff_by_symbol.get(event.symbol)
            if prior_id is None and len(self._admission_handoffs) >= self._max_pending_transfers:
                self._record_eviction(
                    event,
                    symbol=event.symbol,
                    reason="pretrigger_admission_handoff_capacity_overflow",
                )
                raise CaptureContractError("admission handoff capacity is exhausted")
            handoff_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "chili-replay-admission-handoff:"
                    + sha256_json(
                        {
                            "identity_sha256": event.identity.identity_sha256,
                            "event_sha256": event.event_sha256,
                        }
                    ),
                )
            )
            resource_sha256 = (
                self.resource_binding.binding_sha256
                if self.resource_binding is not None
                else None
            )
            record = {
                "handoff_id": handoff_id,
                "source_identity_sha256": event.identity.identity_sha256,
                "resource_binding_sha256": resource_sha256,
                "symbol": event.symbol,
                "admission_event_sequence": event.sequence,
                "admission_event_sha256": event.event_sha256,
                "admission_available_at": _iso(event.clocks.available_at),
            }
            inventory_sha256 = sha256_json(record)
            verification_sha256 = hmac.new(
                _PRETRIGGER_ADMISSION_HANDOFF_KEY,
                canonical_json_bytes(
                    {
                        "kind": "pretrigger_admission_handoff",
                        "inventory_sha256": inventory_sha256,
                    }
                ),
                hashlib.sha256,
            ).hexdigest()
            handoff = PreTriggerAdmissionHandoff(
                handoff_id=handoff_id,
                source_identity_sha256=event.identity.identity_sha256,
                resource_binding_sha256=resource_sha256,
                symbol=event.symbol,
                admission_event_sequence=event.sequence,
                admission_event_sha256=event.event_sha256,
                admission_available_at=event.clocks.available_at,
                inventory_sha256=inventory_sha256,
                _verification_token=_PRETRIGGER_ADMISSION_HANDOFF_TOKEN,
                _verification_sha256=verification_sha256,
            )
            if prior_id is not None and prior_id != handoff_id:
                self._admission_handoffs.pop(prior_id, None)
            self._admission_handoffs[handoff_id] = handoff
            self._admission_handoff_by_symbol[event.symbol] = handoff_id
            return handoff

    @staticmethod
    def _promotion_inventory_sha256(
        *,
        promotion_id: str,
        source_identity_sha256: str,
        resource_binding_sha256: str | None,
        symbol: str,
        promoted_at: datetime,
        events: tuple[CaptureEvent, ...],
        gaps: tuple[CoverageGap, ...],
        admission_handoff_sha256: str | None,
    ) -> str:
        return sha256_json(
            {
                "promotion_id": promotion_id,
                "source_identity_sha256": source_identity_sha256,
                "resource_binding_sha256": resource_binding_sha256,
                "symbol": symbol,
                "promoted_at": _iso(promoted_at),
                "events": [
                    {
                        "sequence": event.sequence,
                        "event_sha256": event.event_sha256,
                    }
                    for event in events
                ],
                "gaps": [gap.to_dict() for gap in gaps],
                "admission_handoff_sha256": admission_handoff_sha256,
            }
        )

    def begin_promotion(
        self,
        symbol: str,
        *,
        promoted_at: datetime,
        source_identity: CaptureRunIdentity | None = None,
        admission_handoff: PreTriggerAdmissionHandoff | None = None,
    ) -> PromotionTransfer:
        """Reserve one non-destructive symbol snapshot for target re-enveloping."""

        normalized = str(symbol or "").strip().upper()
        if not normalized:
            raise CaptureContractError("promotion symbol is required")
        boundary = _utc(promoted_at, "promoted_at")
        start = boundary - self.horizon
        with self._lock:
            if normalized in self._transfer_by_symbol:
                raise CaptureContractError("symbol already has a pending promotion")
            if len(self._pending_transfers) >= self._max_pending_transfers:
                raise CaptureContractError("pending promotion capacity is exhausted")
            if source_identity is not None:
                if not isinstance(source_identity, CaptureRunIdentity):
                    raise CaptureContractError("promotion source identity is malformed")
                supplied = source_identity.identity_sha256
                if self._identity_sha256 not in (None, supplied):
                    raise CaptureContractError("promotion source identity mismatch")
                self._identity_sha256 = supplied
            if self._identity_sha256 is None:
                raise CaptureContractError("promotion ring has no bound source identity")
            self._purge_expected_expiry(boundary)
            pressure_rejection = (
                self.pressure_controller.rejection_reason
                if self.pressure_controller is not None
                else None
            )
            rows = list(self._by_symbol.get(normalized, ()))
            events = tuple(
                sorted(
                    (
                        event
                        for event, _size in rows
                        if start <= event.clocks.available_at <= boundary
                    ),
                    key=lambda event: (event.clocks.available_at, event.sequence),
                )
            )
            admission_handoff_sha256: str | None = None
            if admission_handoff is not None:
                if not isinstance(admission_handoff, PreTriggerAdmissionHandoff):
                    raise CaptureContractError("promotion admission handoff is malformed")
                stored_handoff = self._admission_handoffs.get(
                    admission_handoff.handoff_id
                )
                if stored_handoff != admission_handoff:
                    raise CaptureContractError(
                        "promotion admission handoff is missing, stale, or foreign"
                    )
                if (
                    admission_handoff.symbol != normalized
                    or admission_handoff.source_identity_sha256
                    != self._identity_sha256
                    or admission_handoff.resource_binding_sha256
                    != (
                        self.resource_binding.binding_sha256
                        if self.resource_binding is not None
                        else None
                    )
                    or admission_handoff.admission_available_at > boundary
                    or not any(
                        event.sequence == admission_handoff.admission_event_sequence
                        and event.event_sha256
                        == admission_handoff.admission_event_sha256
                        for event in events
                    )
                ):
                    raise CaptureContractError(
                        "promotion does not contain the exact causal admission input"
                    )
                admission_handoff_sha256 = admission_handoff.handoff_sha256
            gaps: list[CoverageGap] = []
            for (key_symbol, _stream, _reason), accumulator in self._evictions.items():
                if key_symbol not in (None, normalized):
                    continue
                frozen = accumulator.freeze()
                if frozen.intersects(start, boundary):
                    gaps.append(frozen)
            if self._eviction_overflow is not None:
                overflow = self._eviction_overflow.freeze()
                if overflow.intersects(start, boundary):
                    gaps.append(overflow)
            if pressure_rejection is not None:
                gaps.append(
                    CoverageGap(
                        stream=CaptureStream.COVERAGE_GAP,
                        symbol=normalized,
                        reason=f"pretrigger_promotion_{pressure_rejection}",
                        first_available_at=boundary,
                        last_available_at=boundary,
                        lost_count=1,
                    )
                )
            ordered_gaps = tuple(
                sorted(
                    gaps,
                    key=lambda gap: (
                        gap.first_available_at,
                        gap.last_available_at,
                        gap.stream.value,
                        gap.symbol or "",
                        gap.reason,
                    ),
                )
            )
            promotion_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "chili-replay-promotion:"
                    + sha256_json(
                        {
                            "source_identity_sha256": self._identity_sha256,
                            "symbol": normalized,
                            "promoted_at": _iso(boundary),
                            "event_sha256s": [
                                event.event_sha256 for event in events
                            ],
                            "gaps": [gap.to_dict() for gap in ordered_gaps],
                            "admission_handoff_sha256": (
                                admission_handoff_sha256
                            ),
                        }
                    ),
                )
            )
            resource_sha = (
                self.resource_binding.binding_sha256
                if self.resource_binding is not None
                else None
            )
            inventory = self._promotion_inventory_sha256(
                promotion_id=promotion_id,
                source_identity_sha256=self._identity_sha256,
                resource_binding_sha256=resource_sha,
                symbol=normalized,
                promoted_at=boundary,
                events=events,
                gaps=ordered_gaps,
                admission_handoff_sha256=admission_handoff_sha256,
            )
            verification_sha256 = hmac.new(
                _PROMOTION_TRANSFER_KEY,
                canonical_json_bytes(
                    {
                        "kind": "pretrigger_promotion_transfer",
                        "inventory_sha256": inventory,
                    }
                ),
                hashlib.sha256,
            ).hexdigest()
            transfer = PromotionTransfer(
                promotion_id=promotion_id,
                source_identity_sha256=self._identity_sha256,
                resource_binding_sha256=resource_sha,
                symbol=normalized,
                promoted_at=boundary,
                events=events,
                gaps=ordered_gaps,
                inventory_sha256=inventory,
                admission_handoff_sha256=admission_handoff_sha256,
                _verification_token=_PROMOTION_TRANSFER_TOKEN,
                _verification_sha256=verification_sha256,
            )
            self._pending_transfers[promotion_id] = transfer
            self._transfer_by_symbol[normalized] = promotion_id
            self._pending_transfer_sequences.update(
                event.sequence for event in events
            )
            return transfer

    def commit_promotion(self, transfer: PromotionTransfer) -> PromotionBatch:
        """Consume the exact ring rows only after target durable admission succeeds."""

        if not isinstance(transfer, PromotionTransfer):
            raise CaptureContractError("promotion commit transfer is malformed")
        with self._lock:
            pending = self._pending_transfers.get(transfer.promotion_id)
            if pending != transfer:
                raise CaptureContractError("promotion commit does not match pending snapshot")
            if transfer.promotion_id in self._invalidated_transfers:
                raise CaptureContractError(
                    "promotion snapshot was invalidated by a concurrent observation"
                )
            for event in transfer.events:
                active = self._active.get(event.sequence)
                if active is not None and active[1].event_sha256 == event.event_sha256:
                    self._remove(event.sequence, record_gap_reason=None)
            for key in tuple(self._evictions):
                if key[0] == transfer.symbol:
                    self._evictions.pop(key, None)
            self._pending_transfer_sequences.difference_update(
                event.sequence for event in transfer.events
            )
            self._pending_transfers.pop(transfer.promotion_id, None)
            self._invalidated_transfers.discard(transfer.promotion_id)
            self._transfer_by_symbol.pop(transfer.symbol, None)
            if transfer.admission_handoff_sha256 is not None:
                handoff_id = self._admission_handoff_by_symbol.pop(
                    transfer.symbol, None
                )
                if handoff_id is not None:
                    self._admission_handoffs.pop(handoff_id, None)
            self._compact_global_heap()
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

    def abort_promotion(self, transfer: PromotionTransfer) -> bool:
        """Release a snapshot reservation without consuming its ring rows."""

        if not isinstance(transfer, PromotionTransfer):
            raise CaptureContractError("promotion abort transfer is malformed")
        with self._lock:
            pending = self._pending_transfers.get(transfer.promotion_id)
            if pending is None:
                return False
            if pending != transfer:
                raise CaptureContractError("promotion abort does not match pending snapshot")
            for event in transfer.events:
                active = self._active.get(event.sequence)
                if active is None or active[1].event_sha256 != event.event_sha256:
                    self._record_eviction(
                        event,
                        symbol=transfer.symbol,
                        reason="pretrigger_pending_transfer_lost_before_abort",
                    )
            self._pending_transfer_sequences.difference_update(
                event.sequence for event in transfer.events
            )
            self._pending_transfers.pop(transfer.promotion_id, None)
            self._invalidated_transfers.discard(transfer.promotion_id)
            self._transfer_by_symbol.pop(transfer.symbol, None)
            return True

    def promote(self, symbol: str, *, promoted_at: datetime) -> PromotionBatch:
        """Atomically detach the preceding horizon for a newly hot symbol."""
        return self.commit_promotion(
            self.begin_promotion(symbol, promoted_at=promoted_at)
        )


@dataclass
class _PendingGap:
    identity: CaptureRunIdentity
    stream: CaptureStream
    symbol: str | None
    reason: str
    first_available_at: datetime
    last_available_at: datetime
    lost_count: int = 1

    def add(self, available_at: datetime) -> None:
        self.first_available_at = min(self.first_available_at, available_at)
        self.last_available_at = max(self.last_available_at, available_at)
        self.lost_count += 1

    def merge(self, other: "_PendingGap") -> None:
        self.first_available_at = min(
            self.first_available_at, other.first_available_at
        )
        self.last_available_at = max(self.last_available_at, other.last_available_at)
        self.lost_count += other.lost_count

    def freeze(self) -> tuple[CaptureRunIdentity, CoverageGap]:
        return self.identity, CoverageGap(
            stream=self.stream,
            symbol=self.symbol,
            reason=self.reason,
            first_available_at=self.first_available_at,
            last_available_at=self.last_available_at,
            lost_count=self.lost_count,
        )


@dataclass(frozen=True)
class IngressBatch:
    events: tuple[CaptureEvent, ...]
    gaps: tuple[tuple[CaptureRunIdentity, CoverageGap], ...]


class SharedCaptureAdmissionBudget:
    """One measured aggregate queue/write budget shared across run identities.

    Reservations cover both queued and writer-inflight events and are released
    only after the writer reports success or an explicit failed-write outcome.
    This prevents N one-symbol coordinators from each claiming the full host
    queue and sustained write budget.
    """

    def __init__(
        self,
        *,
        resource_binding: CaptureResourceBinding,
        max_events: int,
        max_bytes: int,
        sustained_write_budget_bytes_per_second: int,
        pressure_controller: CaptureAdaptivePressureController | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(resource_binding, CaptureResourceBinding):
            raise CaptureContractError("shared admission resource binding is malformed")
        if min(
            int(max_events),
            int(max_bytes),
            int(sustained_write_budget_bytes_per_second),
        ) <= 0:
            raise CaptureContractError("shared admission limits must be positive")
        if max_events > resource_binding.budget.max_queue_events:
            raise CaptureContractError("shared event budget exceeds measured binding")
        if max_bytes > resource_binding.budget.async_queue_bytes:
            raise CaptureContractError("shared byte budget exceeds measured binding")
        if (
            sustained_write_budget_bytes_per_second
            > resource_binding.budget.sustained_write_budget_bytes_per_second
        ):
            raise CaptureContractError("shared write rate exceeds measured binding")
        if pressure_controller is not None and (
            pressure_controller.binding != resource_binding
        ):
            raise CaptureContractError("shared admission pressure binding mismatch")
        if not callable(monotonic_clock):
            raise CaptureContractError("shared admission monotonic clock is invalid")
        self.resource_binding = resource_binding
        self.max_events = int(max_events)
        self.max_bytes = int(max_bytes)
        self.sustained_write_budget_bytes_per_second = int(
            sustained_write_budget_bytes_per_second
        )
        self.pressure_controller = pressure_controller
        self._monotonic_clock = monotonic_clock
        self._lock = threading.RLock()
        self._reservations: dict[str, tuple[str, int]] = {}
        self._events_by_identity: dict[str, int] = defaultdict(int)
        self._bytes_by_identity: dict[str, int] = defaultdict(int)
        self._outstanding_bytes = 0
        self._write_tokens = float(self.max_bytes)
        self._token_updated_at = float(self._monotonic_clock())
        if not math.isfinite(self._token_updated_at):
            raise CaptureContractError(
                "shared admission monotonic clock returned a non-finite value"
            )
        self._admitted = 0
        self._completed = 0
        self._failed = 0
        self._rejections: dict[str, int] = defaultdict(int)
        self._failures_by_identity: dict[str, int] = defaultdict(int)

    @classmethod
    def from_resource_binding(
        cls,
        binding: CaptureResourceBinding,
        *,
        pressure_controller: CaptureAdaptivePressureController | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> "SharedCaptureAdmissionBudget":
        return cls(
            resource_binding=binding,
            max_events=binding.budget.max_queue_events,
            max_bytes=binding.budget.async_queue_bytes,
            sustained_write_budget_bytes_per_second=(
                binding.budget.sustained_write_budget_bytes_per_second
            ),
            pressure_controller=pressure_controller,
            monotonic_clock=monotonic_clock,
        )

    def _refresh_tokens(self) -> None:
        now = float(self._monotonic_clock())
        if not math.isfinite(now):
            raise CaptureContractError(
                "shared admission monotonic clock returned a non-finite value"
            )
        elapsed = max(0.0, now - self._token_updated_at)
        self._token_updated_at = now
        self._write_tokens = min(
            float(self.max_bytes),
            self._write_tokens
            + elapsed * self.sustained_write_budget_bytes_per_second,
        )

    def try_admit(self, event: CaptureEvent, size: int) -> str | None:
        if not isinstance(event, CaptureEvent) or int(size) <= 0:
            raise CaptureContractError("shared admission event is malformed")
        identity = event.identity.identity_sha256
        with self._lock:
            if event.event_sha256 in self._reservations:
                raise CaptureContractError("shared admission event is duplicated")
            rejection = (
                self.pressure_controller.rejection_reason
                if self.pressure_controller is not None
                else None
            )
            if rejection is not None:
                reason = f"shared_{rejection}"
            elif size > self.max_bytes:
                reason = "shared_capture_event_exceeds_byte_budget"
            else:
                self._refresh_tokens()
                if size > self._write_tokens:
                    reason = "shared_capture_write_bandwidth_budget_exceeded"
                elif (
                    len(self._reservations) >= self.max_events
                    or self._outstanding_bytes + size > self.max_bytes
                ):
                    reason = "shared_capture_queue_overflow"
                else:
                    self._reservations[event.event_sha256] = (identity, int(size))
                    self._events_by_identity[identity] += 1
                    self._bytes_by_identity[identity] += int(size)
                    self._outstanding_bytes += int(size)
                    self._write_tokens -= int(size)
                    self._admitted += 1
                    return None
            self._rejections[reason] += 1
            return reason

    def _release(self, events: Iterable[CaptureEvent], *, failed: bool) -> None:
        with self._lock:
            for event in events:
                reservation = self._reservations.pop(event.event_sha256, None)
                if reservation is None:
                    raise CaptureContractError(
                        "shared admission completion has no exact reservation"
                    )
                identity, size = reservation
                if identity != event.identity.identity_sha256:
                    raise CaptureContractError(
                        "shared admission completion identity mismatch"
                    )
                self._events_by_identity[identity] -= 1
                self._bytes_by_identity[identity] -= size
                self._outstanding_bytes -= size
                if self._events_by_identity[identity] == 0:
                    self._events_by_identity.pop(identity, None)
                if self._bytes_by_identity[identity] == 0:
                    self._bytes_by_identity.pop(identity, None)
                if failed:
                    self._failed += 1
                    self._failures_by_identity[identity] += 1
                else:
                    self._completed += 1

    def complete(self, events: Iterable[CaptureEvent]) -> None:
        self._release(tuple(events), failed=False)

    def fail(self, events: Iterable[CaptureEvent]) -> None:
        self._release(tuple(events), failed=True)

    def outstanding_for(self, identity_sha256: str) -> int:
        identity = _validated_sha256(identity_sha256, "shared admission identity")
        with self._lock:
            return self._events_by_identity.get(identity, 0)

    def health(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_tokens()
            return {
                "resource_hashes": self.resource_binding.hashes,
                "max_events": self.max_events,
                "max_bytes": self.max_bytes,
                "sustained_write_budget_bytes_per_second": (
                    self.sustained_write_budget_bytes_per_second
                ),
                "outstanding_events": len(self._reservations),
                "outstanding_bytes": self._outstanding_bytes,
                "events_by_identity": dict(sorted(self._events_by_identity.items())),
                "bytes_by_identity": dict(sorted(self._bytes_by_identity.items())),
                "admitted": self._admitted,
                "completed": self._completed,
                "failed": self._failed,
                "failures_by_identity": dict(
                    sorted(self._failures_by_identity.items())
                ),
                "rejections": dict(sorted(self._rejections.items())),
                "write_admission_tokens": self._write_tokens,
                "pressure": (
                    self.pressure_controller.health()
                    if self.pressure_controller is not None
                    else None
                ),
            }


class BoundedCaptureIngress:
    """Non-blocking producer queue with bounded, aggregated overflow evidence."""

    def __init__(
        self,
        *,
        max_events: int,
        max_bytes: int,
        max_gap_keys: int,
        sustained_write_budget_bytes_per_second: int | None = None,
        resource_binding: CaptureResourceBinding | None = None,
        pressure_controller: CaptureAdaptivePressureController | None = None,
        shared_admission_budget: SharedCaptureAdmissionBudget | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min(max_events, max_bytes, max_gap_keys) <= 0:
            raise CaptureContractError("ingress limits must be positive")
        if sustained_write_budget_bytes_per_second is not None:
            if int(sustained_write_budget_bytes_per_second) <= 0:
                raise CaptureContractError(
                    "sustained ingress write budget must be positive"
                )
        if resource_binding is not None:
            if not isinstance(resource_binding, CaptureResourceBinding):
                raise CaptureContractError("ingress resource binding is malformed")
            if max_events > resource_binding.budget.max_queue_events:
                raise CaptureContractError(
                    "ingress event limit exceeds resolved resource budget"
                )
            if max_bytes > resource_binding.budget.async_queue_bytes:
                raise CaptureContractError(
                    "ingress byte limit exceeds resolved resource budget"
                )
            if (
                sustained_write_budget_bytes_per_second is None
                or int(sustained_write_budget_bytes_per_second)
                > resource_binding.budget.sustained_write_budget_bytes_per_second
            ):
                raise CaptureContractError(
                    "ingress write rate is absent or exceeds resolved resource budget"
                )
        if pressure_controller is not None:
            if not isinstance(pressure_controller, CaptureAdaptivePressureController):
                raise CaptureContractError("ingress pressure controller is malformed")
            if resource_binding is None or pressure_controller.binding != resource_binding:
                raise CaptureContractError("ingress pressure binding mismatch")
        if shared_admission_budget is not None:
            if not isinstance(shared_admission_budget, SharedCaptureAdmissionBudget):
                raise CaptureContractError("shared ingress admission is malformed")
            if (
                resource_binding is None
                or shared_admission_budget.resource_binding != resource_binding
            ):
                raise CaptureContractError("shared ingress admission binding mismatch")
        if not callable(monotonic_clock):
            raise CaptureContractError("ingress monotonic clock must be callable")
        self.max_events = int(max_events)
        self.max_bytes = int(max_bytes)
        self.max_gap_keys = int(max_gap_keys)
        self.sustained_write_budget_bytes_per_second = (
            int(sustained_write_budget_bytes_per_second)
            if sustained_write_budget_bytes_per_second is not None
            else None
        )
        self.resource_binding = resource_binding
        self.pressure_controller = pressure_controller
        self.shared_admission_budget = shared_admission_budget
        self._monotonic_clock = monotonic_clock
        self._write_tokens = float(self.max_bytes)
        self._write_token_updated_at = float(self._monotonic_clock())
        if not math.isfinite(self._write_token_updated_at):
            raise CaptureContractError("ingress monotonic clock returned a non-finite value")
        self._queue: deque[tuple[CaptureEvent, int]] = deque()
        self._queued_bytes = 0
        self._gaps: dict[tuple[str, str, str, str], _PendingGap] = {}
        self._submitted = 0
        self._accepted = 0
        self._dropped = 0
        self._write_bandwidth_dropped = 0
        self._resource_pressure_dropped = 0
        self._reported_gap_lost = 0
        self._accepted_event_accumulator_sha256 = _EMPTY_ACCUMULATOR_SHA256
        self._gap_records_emitted = 0
        self._gap_lost_emitted = 0
        self._gap_accumulator_sha256 = _EMPTY_ACCUMULATOR_SHA256
        self._closed = False
        self._finalized = False
        self._post_close_submissions = 0
        self._writer_failure_count = 0
        self._writer_failed_event_count = 0
        self._writer_failure_reason: str | None = None
        self._identity_sha256: str | None = None
        self._sequence_min: int | None = None
        self._sequence_max: int | None = None
        self._condition = threading.Condition(threading.RLock())

    @classmethod
    def from_resource_binding(
        cls,
        binding: CaptureResourceBinding,
        *,
        pressure_controller: CaptureAdaptivePressureController | None = None,
        shared_admission_budget: SharedCaptureAdmissionBudget | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> "BoundedCaptureIngress":
        if not isinstance(binding, CaptureResourceBinding):
            raise CaptureContractError("ingress resource binding is malformed")
        return cls(
            max_events=binding.budget.max_queue_events,
            max_bytes=binding.budget.async_queue_bytes,
            max_gap_keys=binding.budget.max_gap_keys,
            sustained_write_budget_bytes_per_second=(
                binding.budget.sustained_write_budget_bytes_per_second
            ),
            resource_binding=binding,
            pressure_controller=pressure_controller,
            shared_admission_budget=shared_admission_budget,
            monotonic_clock=monotonic_clock,
        )

    def _refresh_write_tokens(self) -> None:
        if self.sustained_write_budget_bytes_per_second is None:
            return
        now = float(self._monotonic_clock())
        if not math.isfinite(now):
            raise CaptureContractError("ingress monotonic clock returned a non-finite value")
        elapsed = max(0.0, now - self._write_token_updated_at)
        self._write_token_updated_at = now
        self._write_tokens = min(
            float(self.max_bytes),
            self._write_tokens
            + elapsed * self.sustained_write_budget_bytes_per_second,
        )

    def _record_submission_sequence(self, sequence: int) -> None:
        self._sequence_min = (
            sequence if self._sequence_min is None else min(self._sequence_min, sequence)
        )
        self._sequence_max = (
            sequence if self._sequence_max is None else max(self._sequence_max, sequence)
        )

    def _bind_run_identity(self, identity: CaptureRunIdentity) -> None:
        """Fail-closed lease binding before a shared writer can start.

        Ordinary isolated ingresses still bind on their first event.  A
        process-wide shared-store lease cannot wait until that point: two
        leases could otherwise attach to the same empty ingress and let the
        first submitted identity silently choose which lease actually owns it.
        """

        if not isinstance(identity, CaptureRunIdentity):
            raise CaptureContractError("capture ingress lease identity is malformed")
        identity_sha256 = identity.identity_sha256
        with self._condition:
            if self._identity_sha256 not in (None, identity_sha256):
                raise CaptureContractError(
                    "capture ingress lease identity conflicts with queued run"
                )
            self._identity_sha256 = identity_sha256

    def _record_pending_gap(self, incoming: _PendingGap) -> None:
        symbol = incoming.symbol or "*"
        reason = incoming.reason
        key = (
            incoming.identity.identity_sha256,
            incoming.stream.value,
            symbol,
            reason,
        )
        if key not in self._gaps and len(self._gaps) >= self.max_gap_keys:
            symbol = "*"
            reason = "gap_key_budget_overflow"
            key = (
                incoming.identity.identity_sha256,
                incoming.stream.value,
                symbol,
                reason,
            )
        existing = self._gaps.get(key)
        if existing is None and len(self._gaps) >= self.max_gap_keys:
            merge_key = next(
                (
                    candidate
                    for candidate in self._gaps
                    if candidate[0] == incoming.identity.identity_sha256
                    and candidate[1] == incoming.stream.value
                ),
                None,
            )
            if merge_key is not None:
                displaced = self._gaps.pop(merge_key)
                existing = _PendingGap(
                    identity=incoming.identity,
                    stream=incoming.stream,
                    symbol=None,
                    reason="gap_key_budget_overflow",
                    first_available_at=displaced.first_available_at,
                    last_available_at=displaced.last_available_at,
                    lost_count=displaced.lost_count,
                )
                self._gaps[key] = existing
            else:
                overflow_key = (
                    incoming.identity.identity_sha256,
                    CaptureStream.COVERAGE_GAP.value,
                    "*",
                    "gap_ledger_key_budget_overflow",
                )
                overflow = self._gaps.get(overflow_key)
                if overflow is None:
                    displaced_key = next(iter(self._gaps))
                    displaced = self._gaps.pop(displaced_key)
                    overflow = _PendingGap(
                        identity=incoming.identity,
                        stream=CaptureStream.COVERAGE_GAP,
                        symbol=None,
                        reason="gap_ledger_key_budget_overflow",
                        first_available_at=displaced.first_available_at,
                        last_available_at=displaced.last_available_at,
                        lost_count=displaced.lost_count,
                    )
                    self._gaps[overflow_key] = overflow
                overflow_incoming = _PendingGap(
                    identity=incoming.identity,
                    stream=CaptureStream.COVERAGE_GAP,
                    symbol=None,
                    reason="gap_ledger_key_budget_overflow",
                    first_available_at=incoming.first_available_at,
                    last_available_at=incoming.last_available_at,
                    lost_count=incoming.lost_count,
                )
                overflow.merge(overflow_incoming)
                return
        if existing is None:
            self._gaps[key] = _PendingGap(
                identity=incoming.identity,
                stream=incoming.stream,
                symbol=None if symbol == "*" else symbol,
                reason=reason,
                first_available_at=incoming.first_available_at,
                last_available_at=incoming.last_available_at,
                lost_count=incoming.lost_count,
            )
        else:
            existing.merge(incoming)

    def _record_gap(self, event: CaptureEvent, reason: str) -> None:
        self._record_pending_gap(
            _PendingGap(
                identity=event.identity,
                stream=event.stream,
                symbol=event.symbol,
                reason=reason,
                first_available_at=event.clocks.available_at,
                last_available_at=event.clocks.available_at,
            )
        )
        self._dropped += 1

    def submit(self, event: CaptureEvent) -> bool:
        """Return immediately; false always creates pending gap evidence."""

        size = approximate_event_bytes(event)
        with self._condition:
            identity_sha256 = event.identity.identity_sha256
            if self._identity_sha256 not in (None, identity_sha256):
                raise CaptureContractError("capture ingress cannot mix run identities")
            self._identity_sha256 = identity_sha256
            if self._finalized:
                raise CaptureContractError(
                    "capture ingress is durably finalized; late submissions are forbidden"
                )
            if self._closed:
                self._post_close_submissions += 1
                self._record_gap(event, "capture_ingress_closed")
                self._condition.notify()
                return False
            self._submitted += 1
            self._record_submission_sequence(event.sequence)
            pressure_rejection = (
                self.pressure_controller.rejection_reason
                if self.pressure_controller is not None
                else None
            )
            if pressure_rejection is not None:
                self._resource_pressure_dropped += 1
                self._record_gap(event, pressure_rejection)
                self._condition.notify()
                return False
            if size > self.max_bytes:
                self._record_gap(event, "capture_event_exceeds_queue_byte_budget")
                self._condition.notify()
                return False
            self._refresh_write_tokens()
            if (
                self.shared_admission_budget is None
                and
                self.sustained_write_budget_bytes_per_second is not None
                and size > self._write_tokens
            ):
                self._write_bandwidth_dropped += 1
                self._record_gap(event, "capture_write_bandwidth_budget_exceeded")
                self._condition.notify()
                return False
            if (
                len(self._queue) >= self.max_events
                or self._queued_bytes + size > self.max_bytes
            ):
                self._record_gap(event, "capture_queue_overflow")
                self._condition.notify()
                return False
            if self.shared_admission_budget is not None:
                shared_rejection = self.shared_admission_budget.try_admit(event, size)
                if shared_rejection is not None:
                    if "write_bandwidth" in shared_rejection:
                        self._write_bandwidth_dropped += 1
                    if "pressure" in shared_rejection:
                        self._resource_pressure_dropped += 1
                    self._record_gap(event, shared_rejection)
                    self._condition.notify()
                    return False
            self._queue.append((event, size))
            self._queued_bytes += size
            self._accepted += 1
            if (
                self.shared_admission_budget is None
                and self.sustained_write_budget_bytes_per_second is not None
            ):
                self._write_tokens -= size
            self._accepted_event_accumulator_sha256 = _event_accumulator_add(
                self._accepted_event_accumulator_sha256, event
            )
            self._condition.notify()
            return True

    def submit_gap(
        self,
        identity: CaptureRunIdentity,
        gap: CoverageGap,
    ) -> bool:
        """Submit an upstream/pre-trigger loss without pretending it was queued.

        Promotion-ring evictions and provider-side losses are already gaps, not
        capture events rejected by this ingress. They share the same bounded
        aggregation ledger but retain a separate lost-count provenance in the
        durable close proof.
        """

        if not isinstance(identity, CaptureRunIdentity) or not isinstance(
            gap, CoverageGap
        ):
            raise CaptureContractError(
                "submit_gap requires exact CaptureRunIdentity/CoverageGap values"
            )
        with self._condition:
            identity_sha256 = identity.identity_sha256
            if self._identity_sha256 not in (None, identity_sha256):
                raise CaptureContractError("capture ingress cannot mix run identities")
            self._identity_sha256 = identity_sha256
            if self._finalized:
                raise CaptureContractError(
                    "capture ingress is durably finalized; late gaps are forbidden"
                )
            incoming = _PendingGap(
                identity=identity,
                stream=gap.stream,
                symbol=gap.symbol,
                reason=gap.reason,
                first_available_at=gap.first_available_at,
                last_available_at=gap.last_available_at,
                lost_count=gap.lost_count,
            )
            self._record_pending_gap(incoming)
            self._reported_gap_lost += gap.lost_count
            if self._closed:
                self._post_close_submissions += 1
                self._condition.notify()
                return False
            self._condition.notify()
            return True

    def pop_batch(
        self,
        *,
        max_events: int,
        max_bytes: int,
        timeout_seconds: float,
    ) -> IngressBatch:
        if min(max_events, max_bytes) <= 0 or timeout_seconds < 0:
            raise CaptureContractError("invalid ingress batch limits")
        with self._condition:
            if not self._queue and not self._gaps and not self._closed:
                self._condition.wait(timeout_seconds)
            gaps = tuple(
                sorted(
                    (gap.freeze() for gap in self._gaps.values()),
                    key=lambda row: (
                        row[0].identity_sha256,
                        row[1].first_available_at,
                        row[1].last_available_at,
                        row[1].stream.value,
                        row[1].symbol or "",
                        row[1].reason,
                    ),
                )
            )
            for identity, gap in gaps:
                self._gap_records_emitted += 1
                self._gap_lost_emitted += gap.lost_count
                self._gap_accumulator_sha256 = _gap_accumulator_add(
                    self._gap_accumulator_sha256, identity, gap
                )
            self._gaps.clear()
            events: list[CaptureEvent] = []
            used = 0
            while self._queue and len(events) < max_events:
                event, size = self._queue[0]
                if events and used + size > max_bytes:
                    break
                self._queue.popleft()
                self._queued_bytes -= size
                used += size
                events.append(event)
            return IngressBatch(events=tuple(events), gaps=gaps)

    def complete_shared_admission(self, events: Iterable[CaptureEvent]) -> None:
        rows = tuple(events)
        if self.shared_admission_budget is not None and rows:
            self.shared_admission_budget.complete(rows)

    def fail_shared_admission(self, events: Iterable[CaptureEvent]) -> None:
        rows = tuple(events)
        if self.shared_admission_budget is not None and rows:
            self.shared_admission_budget.fail(rows)

    def fail_writer(
        self,
        inflight_events: Iterable[CaptureEvent],
        *,
        reason: str,
    ) -> None:
        """Fence a failed writer and release every queued/inflight reservation.

        The run remains permanently ineligible for a clean seal.  Clearing the
        in-memory queue is resource cleanup, not a claim that its contents were
        durable; the writer error and counters remain explicit in health.
        """

        failure_reason = str(reason or "").strip()
        if not failure_reason:
            raise CaptureContractError("capture writer failure reason is required")
        with self._condition:
            rows_by_sha256: dict[str, CaptureEvent] = {
                event.event_sha256: event for event in inflight_events
            }
            for event, _size in self._queue:
                rows_by_sha256.setdefault(event.event_sha256, event)
            rows = tuple(rows_by_sha256.values())
            self._queue.clear()
            self._queued_bytes = 0
            self._gaps.clear()
            self._writer_failure_count += 1
            self._writer_failed_event_count += len(rows)
            self._writer_failure_reason = failure_reason
            self._closed = True
            self._condition.notify_all()
        self.fail_shared_admission(rows)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    @property
    def drained(self) -> bool:
        with self._condition:
            return not self._queue and not self._gaps

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def clean_close_eligible(self) -> bool:
        with self._condition:
            return bool(
                self._closed
                and self._writer_failure_count == 0
                and self._post_close_submissions == 0
                and not self._queue
                and not self._gaps
                and (
                    self.shared_admission_budget is None
                    or self._identity_sha256 is None
                    or self.shared_admission_budget.outstanding_for(
                        self._identity_sha256
                    )
                    == 0
                )
            )

    def finalize_clean_close(self, identity: CaptureRunIdentity) -> dict[str, Any]:
        """Atomically freeze a drained ingress and return runtime-derived counters.

        After this transition, a late producer call raises instead of mutating the
        already-proved run.  Before this transition, any post-close submission
        permanently makes the run ineligible for a durable clean-close seal.
        """

        if not isinstance(identity, CaptureRunIdentity):
            raise CaptureContractError(
                "clean-close finalization requires one exact CaptureRunIdentity"
            )
        with self._condition:
            if self._identity_sha256 != identity.identity_sha256:
                raise CaptureContractError(
                    "clean-close identity does not match ingress runtime identity"
                )
            if not self._closed:
                raise CaptureContractError("capture ingress is not closed")
            if self._post_close_submissions:
                raise CaptureContractError(
                    "post-close submissions make capture lifecycle dirty"
                )
            if self._writer_failure_count:
                raise CaptureContractError(
                    "capture writer failure makes lifecycle permanently dirty"
                )
            if self._queue or self._gaps or self._queued_bytes:
                raise CaptureContractError(
                    "capture ingress queues or coverage gaps are not drained"
                )
            if (
                self.shared_admission_budget is not None
                and self.shared_admission_budget.outstanding_for(
                    identity.identity_sha256
                )
                != 0
            ):
                raise CaptureContractError(
                    "capture ingress still owns shared writer reservations"
                )
            if self._submitted != self._accepted + self._dropped:
                raise CaptureContractError(
                    "capture ingress submission counters do not reconcile"
                )
            if self._gap_lost_emitted != self._dropped + self._reported_gap_lost:
                raise CaptureContractError(
                    "capture ingress dropped-event gaps were not all emitted"
                )
            total_lost = self._dropped + self._reported_gap_lost
            if total_lost and not (1 <= self._gap_records_emitted <= total_lost):
                raise CaptureContractError(
                    "capture ingress coverage-gap record counts do not reconcile"
                )
            if not total_lost and self._gap_records_emitted:
                raise CaptureContractError(
                    "capture ingress emitted gaps without dropped events"
                )
            if self._submitted <= 0:
                raise CaptureContractError(
                    "capture ingress observed no events for the exact run"
                )
            if (
                self._sequence_min is None
                or self._sequence_max is None
                or self._sequence_min > self._sequence_max
            ):
                raise CaptureContractError(
                    "capture ingress sequence bounds do not reconcile"
                )
            self._finalized = True
            return {
                "identity_sha256": self._identity_sha256,
                "submitted": self._submitted,
                "accepted": self._accepted,
                "dropped": self._dropped,
                "reported_gap_lost": self._reported_gap_lost,
                "accepted_event_accumulator_sha256": (
                    self._accepted_event_accumulator_sha256
                ),
                "gap_records_emitted": self._gap_records_emitted,
                "gap_lost_emitted": self._gap_lost_emitted,
                "gap_accumulator_sha256": self._gap_accumulator_sha256,
                "post_close_submissions": self._post_close_submissions,
                "writer_failure_count": self._writer_failure_count,
                "writer_failed_event_count": self._writer_failed_event_count,
                "writer_failure_reason": self._writer_failure_reason,
                "queued_events": len(self._queue),
                "queued_bytes": self._queued_bytes,
                "pending_gap_keys": len(self._gaps),
                "closed": self._closed,
                "finalized": self._finalized,
                "sequence_min": self._sequence_min,
                "sequence_max": self._sequence_max,
            }

    def health(self) -> dict[str, Any]:
        with self._condition:
            self._refresh_write_tokens()
            event_utilization = len(self._queue) / self.max_events
            byte_utilization = self._queued_bytes / self.max_bytes
            token_fraction = (
                self._write_tokens / self.max_bytes
                if self.sustained_write_budget_bytes_per_second is not None
                else None
            )
            if self._dropped or self._post_close_submissions or self._writer_failure_count:
                backpressure_state = "failed_closed"
            elif event_utilization >= 1.0 or byte_utilization >= 1.0 or token_fraction == 0:
                backpressure_state = "at_capacity"
            elif self._queue:
                backpressure_state = "buffered"
            else:
                backpressure_state = "idle"
            return {
                "identity_sha256": self._identity_sha256,
                "submitted": self._submitted,
                "accepted": self._accepted,
                "dropped": self._dropped,
                "write_bandwidth_dropped": self._write_bandwidth_dropped,
                "resource_pressure_dropped": self._resource_pressure_dropped,
                "reported_gap_lost": self._reported_gap_lost,
                "accepted_event_accumulator_sha256": (
                    self._accepted_event_accumulator_sha256
                ),
                "gap_records_emitted": self._gap_records_emitted,
                "gap_lost_emitted": self._gap_lost_emitted,
                "gap_accumulator_sha256": self._gap_accumulator_sha256,
                "queued_events": len(self._queue),
                "queued_bytes": self._queued_bytes,
                "pending_gap_keys": len(self._gaps),
                "closed": self._closed,
                "finalized": self._finalized,
                "post_close_submissions": self._post_close_submissions,
                "writer_failure_count": self._writer_failure_count,
                "writer_failed_event_count": self._writer_failed_event_count,
                "writer_failure_reason": self._writer_failure_reason,
                "sequence_min": self._sequence_min,
                "sequence_max": self._sequence_max,
                "clean_close_eligible": bool(
                    self._closed
                    and self._writer_failure_count == 0
                    and self._post_close_submissions == 0
                    and not self._queue
                    and not self._gaps
                    and (
                        self.shared_admission_budget is None
                        or self._identity_sha256 is None
                        or self.shared_admission_budget.outstanding_for(
                            self._identity_sha256
                        )
                        == 0
                    )
                ),
                "max_events": self.max_events,
                "max_bytes": self.max_bytes,
                "max_gap_keys": self.max_gap_keys,
                "sustained_write_budget_bytes_per_second": (
                    self.sustained_write_budget_bytes_per_second
                ),
                "write_admission_tokens": self._write_tokens,
                "queue_event_utilization": event_utilization,
                "queue_byte_utilization": byte_utilization,
                "write_token_fraction": token_fraction,
                "backpressure_state": backpressure_state,
                "resource_hashes": (
                    self.resource_binding.hashes
                    if self.resource_binding is not None
                    else None
                ),
                "adaptive_pressure": (
                    self.pressure_controller.health()
                    if self.pressure_controller is not None
                    else None
                ),
                "shared_admission": (
                    self.shared_admission_budget.health()
                    if self.shared_admission_budget is not None
                    else None
                ),
            }


@dataclass
class _BoundedSourceEvictionFrontier:
    """Conservative per-stream summary for events evicted from the read index."""

    count: int
    max_sequence: int
    max_available_at: datetime
    max_provider_event_at: datetime | None
    exact_provider_clock_unknown: bool
    max_market_reference_at: datetime | None
    market_reference_clock_unknown: bool

    @classmethod
    def from_event(cls, event: CaptureEvent) -> "_BoundedSourceEvictionFrontier":
        return cls(
            count=1,
            max_sequence=event.sequence,
            max_available_at=event.clocks.available_at,
            max_provider_event_at=event.clocks.provider_event_at,
            exact_provider_clock_unknown=event.clocks.provider_event_at is None,
            max_market_reference_at=event.clocks.market_reference_at,
            market_reference_clock_unknown=(
                event.clocks.market_reference_at is None
            ),
        )

    def observe(self, event: CaptureEvent) -> None:
        self.count += 1
        self.max_sequence = max(self.max_sequence, event.sequence)
        self.max_available_at = max(
            self.max_available_at, event.clocks.available_at
        )
        provider_at = event.clocks.provider_event_at
        if provider_at is None:
            self.exact_provider_clock_unknown = True
        elif self.max_provider_event_at is None:
            self.max_provider_event_at = provider_at
        else:
            self.max_provider_event_at = max(
                self.max_provider_event_at, provider_at
            )
        market_at = event.clocks.market_reference_at
        if market_at is None:
            self.market_reference_clock_unknown = True
        elif self.max_market_reference_at is None:
            self.max_market_reference_at = market_at
        else:
            self.max_market_reference_at = max(
                self.max_market_reference_at, market_at
            )


class CaptureProducerLifecycleRuntime:
    """Single sequencing boundary for producer inputs and close certification.

    The runtime owns sequence allocation so RUN_OPEN is always sequence one and
    lifecycle/data availability never moves backwards.  Producers must publish
    through :meth:`submit_input`; a dropped event, overdue heartbeat, missing
    watermark, or unclosed producer makes the certifying ``seal_run`` path
    unavailable.  Direct store sealing remains a diagnostic compatibility path
    and the contract grader keeps it explicitly blocked.
    """

    def __init__(
        self,
        *,
        identity: CaptureRunIdentity,
        ingress: BoundedCaptureIngress,
        resource_binding: CaptureResourceBinding,
        producers: Iterable[CaptureProducerSpec],
        heartbeat_timeout_seconds: float,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(identity, CaptureRunIdentity):
            raise CaptureContractError("producer lifecycle identity is malformed")
        if not isinstance(ingress, BoundedCaptureIngress):
            raise CaptureContractError("producer lifecycle ingress is malformed")
        if not isinstance(resource_binding, CaptureResourceBinding):
            raise CaptureContractError(
                "producer lifecycle requires an exact resource binding"
            )
        if ingress.resource_binding != resource_binding:
            raise CaptureContractError(
                "producer lifecycle ingress/resource bindings do not match"
            )
        timeout = float(heartbeat_timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise CaptureContractError("producer heartbeat timeout must be positive")
        if not callable(wall_clock):
            raise CaptureContractError("producer lifecycle wall clock must be callable")
        roster = tuple(producers)
        run_open_probe = CaptureRunOpen(
            identity_sha256=identity.identity_sha256,
            run_id=identity.run_id,
            generation=identity.generation,
            opened_at=datetime(1970, 1, 1, tzinfo=UTC),
            heartbeat_timeout_seconds=timeout,
            resource_binding_sha256=resource_binding.binding_sha256,
            producers=roster,
        )
        for producer in run_open_probe.producers:
            if (
                producer.code_build_sha256 != identity.code_build_sha256
                or producer.config_sha256 != identity.config_sha256
                or producer.feature_flags_sha256 != identity.feature_flags_sha256
                or producer.resource_binding_sha256 != resource_binding.binding_sha256
            ):
                raise CaptureContractError(
                    f"producer {producer.producer_id} identity/resource binding mismatch"
                )
        owners: dict[CaptureStream, str] = {}
        for producer in run_open_probe.producers:
            for stream in producer.streams:
                if stream in owners:
                    raise CaptureContractError(
                        f"capture stream {stream.value} has conflicting producers"
                    )
                owners[stream] = producer.producer_id
        self.identity = identity
        self.ingress = ingress
        self.resource_binding = resource_binding
        self.producers = {
            producer.producer_id: producer for producer in run_open_probe.producers
        }
        self._owners = dict(owners)
        self.heartbeat_timeout_seconds = timeout
        self._wall_clock = wall_clock
        self._lock = threading.RLock()
        self._sequence = 0
        self._last_available_at: datetime | None = None
        self._run_open: CaptureRunOpen | None = None
        self._run_open_event: CaptureEvent | None = None
        self._registered: set[str] = set()
        self._pending_provider_registrations: dict[
            str, tuple[CaptureProviderRegistrationRecord, CaptureEvent]
        ] = {}
        self._quiescent: set[str] = set()
        self._closed: set[str] = set()
        self._last_lifecycle_event: dict[str, CaptureEvent] = {}
        self._last_input_sequence: dict[str, int] = {}
        self._coverage: dict[tuple[str, CaptureStream], StreamCoverage] = {}
        self._live_continuity: dict[
            tuple[str, CaptureStream], ActiveCaptureContinuityEvidence
        ] = {}
        self._evidence_events: dict[
            tuple[str, CaptureStream], tuple[CaptureEvent, ...]
        ] = {}
        self._close_events: dict[str, CaptureEvent] = {}
        self._run_close_event: CaptureEvent | None = None
        self._submission_failure: str | None = None
        self._producer_gaps: list[str] = []
        self._reported_gaps: list[tuple[str, CoverageGap]] = []
        self._recent_source_events: OrderedDict[str, CaptureEvent] = OrderedDict()
        self._recent_source_event_limit = resource_binding.budget.max_ring_events
        self._bounded_source_evictions: dict[
            tuple[CaptureStream, str, str | None],
            _BoundedSourceEvictionFrontier,
        ] = {}
        self._prefix_hasher = hashlib.sha256()
        self._prefix_hasher.update(b'{"events":[')
        self._prefix_rows = 0
        self._stream_stats: dict[CaptureStream, dict[str, Any]] = {}
        self._receipt_count_by_stream: dict[CaptureStream, int] = defaultdict(int)
        self._receipt_by_id: dict[str, CaptureReadReceipt] = {}
        self._receipt_event_owner: dict[str, str] = {}
        self._read_evidence_by_id: dict[str, ActiveCaptureReadEvidence] = {}
        self._first_dip_tape_by_id: dict[str, FirstDipTapeReceiptEvidence] = {}
        self._accepted_first_dip_detector_refs: dict[str, object] = {}
        self._decision_ids: set[str] = set()
        self._decision_events: dict[str, CaptureEvent] = {}
        self._decision_outputs: dict[str, CaptureDecisionOutput] = {}
        self._predecision_attestations: dict[
            str, ActiveCaptureInputPrefixAttestation
        ] = {}
        self._consumed_predecision_attestations: set[str] = set()
        self._final_decision_attestations: dict[
            str, ActiveCapturePrefixAttestation
        ] = {}
        self._order_intents_by_sha: dict[
            str, tuple[str, CaptureOrderIntent]
        ] = {}
        self._order_intent_sha_by_client_id: dict[str, str] = {}
        self._broker_state_by_intent: dict[
            str, tuple[CaptureEvent, CaptureBrokerOrderLifecycle]
        ] = {}

    def _latch_failure(self, reason: str) -> None:
        normalized = str(reason or "capture_lifecycle_failure").strip().lower()
        if self._submission_failure is None:
            self._submission_failure = normalized

    def _trusted_recorded_at(self, requested: datetime | None) -> datetime:
        """Source append time from the runtime clock, never a producer assertion."""

        recorded = _utc(self._wall_clock(), "producer lifecycle wall clock")
        if self._last_available_at is not None and recorded < self._last_available_at:
            self._latch_failure("trusted_wall_clock_rollback")
            raise CaptureContractError(
                "producer lifecycle wall clock moved behind the durable frontier"
            )
        if requested is not None and _utc(
            requested, "lifecycle.recorded_at"
        ) != recorded:
            self._latch_failure("caller_recorded_at_mismatch")
            raise CaptureContractError(
                "producer lifecycle recorded_at differs from the runtime wall clock"
            )
        return recorded

    def _proposed_sequence(self, available_at: datetime) -> int:
        available = _utc(available_at, "capture available_at")
        if self._last_available_at is not None and available < self._last_available_at:
            raise CaptureContractError(
                "capture sequence cannot move backwards on available_at"
            )
        return self._sequence + 1

    def _submit(self, event: CaptureEvent) -> CaptureEvent:
        if self._submission_failure is not None:
            raise CaptureContractError(
                f"capture lifecycle is already noncertifiable: {self._submission_failure}"
            )
        if event.identity != self.identity:
            raise CaptureContractError("capture event identity escaped lifecycle boundary")
        if event.sequence != self._sequence + 1:
            raise CaptureContractError("capture event sequence escaped lifecycle boundary")
        if (
            self._last_available_at is not None
            and event.clocks.available_at < self._last_available_at
        ):
            raise CaptureContractError("capture event availability moved backwards")
        if not self.ingress.submit(event):
            self._latch_failure(f"ingress_rejected_sequence_{event.sequence}")
            raise CaptureContractError(
                "capture ingress rejected an event; lifecycle cannot certify"
            )
        # Commit lifecycle state only after the bounded ingress accepted the
        # fully constructed, fully validated immutable event.  Validation
        # failures therefore cannot burn a durable sequence number.
        self._sequence = event.sequence
        self._last_available_at = event.clocks.available_at
        if self._prefix_rows:
            self._prefix_hasher.update(b",")
        self._prefix_hasher.update(
            canonical_json_bytes(
                {
                    "sequence": event.sequence,
                    "event_sha256": event.event_sha256,
                    "available_at": _iso(event.clocks.available_at),
                }
            )
        )
        self._prefix_rows += 1
        if STREAM_POLICIES[event.stream].coverage_mode is not CoverageMode.CONTROL:
            self._recent_source_events[event.event_sha256] = event
            self._recent_source_events.move_to_end(event.event_sha256)
            while len(self._recent_source_events) > self._recent_source_event_limit:
                _evicted_sha256, evicted = self._recent_source_events.popitem(
                    last=False
                )
                key = (evicted.stream, evicted.provider, evicted.symbol)
                frontier = self._bounded_source_evictions.get(key)
                if frontier is None:
                    self._bounded_source_evictions[key] = (
                        _BoundedSourceEvictionFrontier.from_event(evicted)
                    )
                else:
                    frontier.observe(evicted)
        return event

    def _current_prefix_root(self) -> str:
        if self._prefix_rows != self._sequence or self._sequence <= 0:
            raise CaptureContractError("capture prefix frontier is inconsistent")
        digest = self._prefix_hasher.copy()
        digest.update(b'],"identity_sha256":')
        digest.update(canonical_json_bytes(self.identity.identity_sha256))
        digest.update(b',"through_sequence":')
        digest.update(str(self._sequence).encode("ascii"))
        digest.update(b"}")
        return digest.hexdigest()

    def _observe_source_event(self, event: CaptureEvent) -> None:
        policy = STREAM_POLICIES[event.stream]
        source_clock = (
            event.clocks.provider_event_at
            if policy.exact_provider_event_clock_required
            else (
                event.clocks.market_reference_at
                or event.clocks.provider_event_at
                if policy.market_reference_clock_required
                else event.clocks.available_at
            )
        )
        stats = self._stream_stats.setdefault(
            event.stream,
            {
                "event_count": 0,
                "first_available_at": event.clocks.available_at,
                "last_available_at": event.clocks.available_at,
                "providers": set(),
                "symbols": set(),
                "exact_event_clock_complete": True,
                "source_sequence_min": event.sequence,
                "source_sequence_max": event.sequence,
                "provider_event_at_max": event.clocks.provider_event_at,
                "source_clock_max": source_clock,
                "max_observed_lateness_seconds": (
                    event.clocks.observed_lateness_seconds or 0.0
                ),
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
        stats["source_sequence_min"] = min(
            stats["source_sequence_min"], event.sequence
        )
        stats["source_sequence_max"] = max(
            stats["source_sequence_max"], event.sequence
        )
        stats["max_observed_lateness_seconds"] = max(
            float(stats["max_observed_lateness_seconds"]),
            float(event.clocks.observed_lateness_seconds or 0.0),
        )
        if event.clocks.provider_event_at is not None:
            current_provider_max = stats["provider_event_at_max"]
            stats["provider_event_at_max"] = (
                event.clocks.provider_event_at
                if current_provider_max is None
                else max(current_provider_max, event.clocks.provider_event_at)
            )
        if source_clock is not None:
            current_source_max = stats["source_clock_max"]
            stats["source_clock_max"] = (
                source_clock
                if current_source_max is None
                else max(current_source_max, source_clock)
            )
        if STREAM_POLICIES[event.stream].exact_provider_event_clock_required:
            stats["exact_event_clock_complete"] = bool(
                stats["exact_event_clock_complete"]
                and event.clocks.provider_event_at is not None
            )

    def _lifecycle_event(
        self,
        payload: Mapping[str, Any],
        *,
        recorded_at: datetime,
    ) -> CaptureEvent:
        recorded = _utc(recorded_at, "lifecycle.recorded_at")
        event = CaptureEvent(
            identity=self.identity,
            sequence=self._proposed_sequence(recorded),
            stream=CaptureStream.CAPTURE_HEALTH,
            provider=CAPTURE_PRODUCER_LIFECYCLE_PROVIDER,
            symbol=None,
            clocks=CaptureClocks(received_at=recorded, available_at=recorded),
            payload=payload,
        )
        return self._submit(event)

    def open(self, *, opened_at: datetime | None = None) -> CaptureEvent:
        with self._lock:
            if self._run_open is not None or self._sequence:
                raise CaptureContractError("capture run may be opened only once")
            recorded = self._trusted_recorded_at(opened_at)
            run_open = CaptureRunOpen(
                identity_sha256=self.identity.identity_sha256,
                run_id=self.identity.run_id,
                generation=self.identity.generation,
                opened_at=recorded,
                heartbeat_timeout_seconds=self.heartbeat_timeout_seconds,
                resource_binding_sha256=self.resource_binding.binding_sha256,
                producers=tuple(self.producers.values()),
            )
            event = self._lifecycle_event(run_open.to_dict(), recorded_at=recorded)
            self._run_open = run_open
            self._run_open_event = event
            return event

    def _producer(self, producer_id: str) -> CaptureProducerSpec:
        normalized = str(producer_id or "").strip().lower()
        producer = self.producers.get(normalized)
        if producer is None:
            raise CaptureContractError(f"producer is not in RUN_OPEN roster: {normalized}")
        return producer

    def _require_open_producer(self, producer_id: str) -> CaptureProducerSpec:
        producer = self._producer(producer_id)
        if self._run_open is None or self._run_close_event is not None:
            raise CaptureContractError("capture run is not open")
        if producer.producer_id not in self._registered:
            raise CaptureContractError("capture producer is not registered")
        if producer.producer_id in self._quiescent:
            raise CaptureContractError("capture producer is already quiescent")
        return producer

    def _check_heartbeat_deadline(self, producer_id: str, recorded_at: datetime) -> None:
        prior = self._last_lifecycle_event.get(producer_id)
        if prior is None:
            assert self._run_open_event is not None
            prior = self._run_open_event
        elapsed = (
            _utc(recorded_at, "lifecycle.recorded_at") - prior.clocks.available_at
        ).total_seconds()
        if elapsed > self.heartbeat_timeout_seconds:
            self._latch_failure(
                f"producer_heartbeat_deadline_exceeded:{producer_id}"
            )
            raise CaptureContractError(
                f"capture producer heartbeat deadline exceeded: {producer_id}"
            )

    def _producer_fact(
        self,
        producer: CaptureProducerSpec,
        *,
        kind: CaptureProducerLifecycleKind,
        recorded_at: datetime,
        frontier_sequence: int,
        evidence_event_sha256s: Iterable[str] = (),
        gap_reason: str | None = None,
    ) -> CaptureEvent:
        assert self._run_open is not None and self._run_open_event is not None
        prior = self._last_lifecycle_event.get(
            producer.producer_id, self._run_open_event
        )
        fact = CaptureProducerLifecycleFact(
            kind=kind,
            identity_sha256=self.identity.identity_sha256,
            producer_roster_sha256=self._run_open.producer_roster_sha256,
            recorded_at=recorded_at,
            frontier_sequence=frontier_sequence,
            prior_lifecycle_event_sha256=prior.event_sha256,
            producer_id=producer.producer_id,
            producer_instance_id=producer.instance_id,
            producer_generation=producer.generation,
            producer_spec_sha256=producer.spec_sha256,
            evidence_event_sha256s=tuple(evidence_event_sha256s),
            gap_reason=gap_reason,
        )
        event = self._lifecycle_event(fact.to_dict(), recorded_at=recorded_at)
        self._last_lifecycle_event[producer.producer_id] = event
        return event

    def register(
        self, producer_id: str, *, recorded_at: datetime | None = None
    ) -> CaptureEvent:
        with self._lock:
            recorded = self._trusted_recorded_at(recorded_at)
            producer = self._producer(producer_id)
            if self._run_open is None or self._run_close_event is not None:
                raise CaptureContractError("capture run is not open")
            if producer.producer_id in self._registered:
                raise CaptureContractError("capture producer registered more than once")
            self._check_heartbeat_deadline(producer.producer_id, recorded)
            assert self._run_open_event is not None
            event = self._producer_fact(
                producer,
                kind=CaptureProducerLifecycleKind.REGISTERED,
                recorded_at=recorded,
                frontier_sequence=self._run_open_event.sequence,
            )
            self._registered.add(producer.producer_id)
            self._last_input_sequence[producer.producer_id] = self._run_open_event.sequence
            return event

    def record_provider_registration_evidence(
        self,
        producer_id: str,
        *,
        evidence: CaptureProviderRegistrationEvidence,
        stream: CaptureStream,
        provider: str,
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
        symbol: str | None = None,
        query: Mapping[str, Any] | None = None,
        recorded_at: datetime | None = None,
    ) -> CaptureEvent:
        """Persist the exact first-frame proof before registering a producer."""

        with self._lock:
            recorded = self._trusted_recorded_at(recorded_at)
            producer = self._producer(producer_id)
            if self._run_open is None or self._run_close_event is not None:
                raise CaptureContractError("capture run is not open")
            if producer.producer_id in self._registered:
                raise CaptureContractError("capture producer is already registered")
            if producer.producer_id in self._pending_provider_registrations:
                raise CaptureContractError(
                    "capture producer registration evidence already exists"
                )
            self._check_heartbeat_deadline(producer.producer_id, recorded)
            if not isinstance(evidence, CaptureProviderRegistrationEvidence):
                raise CaptureContractError("provider registration evidence is malformed")
            if (
                evidence.producer_id != producer.producer_id
                or evidence.provider_instance_id != producer.instance_id
                or evidence.provider_generation != producer.generation
            ):
                raise CaptureContractError(
                    "provider registration evidence differs from RUN_OPEN"
                )
            normalized_stream = (
                stream if isinstance(stream, CaptureStream) else CaptureStream(str(stream))
            )
            normalized_provider = str(provider or "").strip().lower()
            if normalized_stream not in producer.streams:
                raise CaptureContractError(
                    "provider registration stream is not owned by producer"
                )
            if evidence.provider != normalized_provider:
                raise CaptureContractError("provider registration source mismatch")
            if evidence.source_payload_sha256 != sha256_json(payload):
                raise CaptureContractError("provider registration payload hash mismatch")
            if not isinstance(clocks, CaptureClocks):
                raise CaptureContractError("provider registration clocks are malformed")
            if (
                clocks.provider_event_at != evidence.provider_event_at
                or clocks.received_at != evidence.received_at
            ):
                raise CaptureContractError("provider registration clocks mismatch")
            if clocks.available_at > recorded or clocks.received_at > recorded:
                raise CaptureContractError(
                    "provider registration source frame is later than trusted clock"
                )
            record = CaptureProviderRegistrationRecord(
                identity_sha256=self.identity.identity_sha256,
                evidence=evidence,
                source_stream=normalized_stream,
                source_symbol=symbol,
                source_query_sha256=(None if query is None else sha256_json(query)),
                source_available_at=clocks.available_at,
                source_market_reference_at=clocks.market_reference_at,
            )
            event = self._lifecycle_event(record.to_dict(), recorded_at=recorded)
            self._pending_provider_registrations[producer.producer_id] = (
                record,
                event,
            )
            return event

    def register_from_provider_evidence(
        self,
        producer_id: str,
        evidence_event: CaptureEvent,
        *,
        recorded_at: datetime | None = None,
    ) -> CaptureEvent:
        """Consume the immediately preceding durable first-frame control proof."""

        with self._lock:
            recorded = self._trusted_recorded_at(recorded_at)
            producer = self._producer(producer_id)
            pending = self._pending_provider_registrations.get(producer.producer_id)
            if pending is None or pending[1] != evidence_event:
                raise CaptureContractError(
                    "provider registration evidence is missing, stale, or foreign"
                )
            if self._sequence != evidence_event.sequence:
                raise CaptureContractError(
                    "provider registration evidence is not the durable frontier"
                )
            if producer.producer_id in self._registered:
                raise CaptureContractError("capture producer registered more than once")
            self._check_heartbeat_deadline(producer.producer_id, recorded)
            event = self._producer_fact(
                producer,
                kind=CaptureProducerLifecycleKind.REGISTERED,
                recorded_at=recorded,
                frontier_sequence=evidence_event.sequence,
                evidence_event_sha256s=(evidence_event.event_sha256,),
            )
            self._registered.add(producer.producer_id)
            self._last_input_sequence[producer.producer_id] = evidence_event.sequence
            self._pending_provider_registrations.pop(producer.producer_id, None)
            return event

    def _validate_broker_lifecycle_transition(
        self,
        lifecycle: CaptureBrokerOrderLifecycle,
        *,
        symbol: str | None,
    ) -> None:
        bound = self._order_intents_by_sha.get(lifecycle.order_intent_sha256)
        if bound is None:
            raise CaptureContractError(
                "broker lifecycle references an unknown durable order intent"
            )
        decision_id, intent = bound
        final_attestation = self._final_decision_attestations.get(decision_id)
        normalized_symbol = str(symbol or "").strip().upper()
        if (
            lifecycle.decision_id != decision_id
            or lifecycle.client_order_id != intent.client_order_id
            or lifecycle.client_order_id_sha256 != intent.client_order_id_sha256
            or lifecycle.order_quantity != intent.quantity
            or normalized_symbol != intent.symbol
        ):
            raise CaptureContractError(
                "broker lifecycle does not match its exact decision/order intent"
            )
        if (
            final_attestation is None
            or lifecycle.final_decision_attestation_sha256
            != final_attestation.durable_authority_sha256
        ):
            raise CaptureContractError(
                "broker lifecycle lacks its durable final decision authority"
            )
        if intent.risk_increasing:
            if (
                lifecycle.reservation_claim_sha256 is None
                or lifecycle.reservation_claim_sha256
                != intent.reservation_claim_sha256
            ):
                raise CaptureContractError(
                    "risk-increasing broker lifecycle lacks exact reservation claim"
                )
        elif lifecycle.reservation_claim_sha256 is not None:
            raise CaptureContractError(
                "non-risk-increasing broker lifecycle carries a reservation claim"
            )
        if lifecycle.transition not in {
            CaptureBrokerTransition.SUBMITTED,
            CaptureBrokerTransition.REJECTED,
            CaptureBrokerTransition.FAILED,
        } and lifecycle.broker_order_id is None:
            raise CaptureContractError(
                "broker lifecycle transition lacks its broker order id"
            )
        if intent.intent_role is CaptureOrderIntentRole.REPLACEMENT:
            predecessor_sha256 = intent.replaces_order_intent_sha256
            assert predecessor_sha256 is not None
            predecessor = self._order_intents_by_sha.get(predecessor_sha256)
            predecessor_state = self._broker_state_by_intent.get(
                predecessor_sha256
            )
            if predecessor is None or predecessor_state is None:
                raise CaptureContractError(
                    "replacement lifecycle lacks its durable predecessor"
                )
            predecessor_lifecycle = predecessor_state[1]
            if (
                predecessor_lifecycle.broker_order_id is None
                or lifecycle.replaces_broker_order_id
                != predecessor_lifecycle.broker_order_id
            ):
                raise CaptureContractError(
                    "replacement lifecycle broker predecessor is inconsistent"
                )
            if (
                predecessor_lifecycle.transition
                is CaptureBrokerTransition.REPLACED
                and lifecycle.broker_order_id is not None
                and predecessor_lifecycle.replaced_by_broker_order_id
                != lifecycle.broker_order_id
            ):
                raise CaptureContractError(
                    "replacement successor broker order id is inconsistent"
                )
        elif lifecycle.replaces_broker_order_id is not None:
            raise CaptureContractError(
                "non-replacement lifecycle carries broker predecessor lineage"
            )
        if lifecycle.replacement_order_intent_sha256 is not None:
            successor = self._order_intents_by_sha.get(
                lifecycle.replacement_order_intent_sha256
            )
            if (
                successor is None
                or successor[1].intent_role
                is not CaptureOrderIntentRole.REPLACEMENT
                or successor[1].replaces_order_intent_sha256
                != lifecycle.order_intent_sha256
            ):
                raise CaptureContractError(
                    "broker replacement transition has invalid successor intent lineage"
                )
            successor_state = self._broker_state_by_intent.get(
                lifecycle.replacement_order_intent_sha256
            )
            if (
                lifecycle.replaced_by_broker_order_id is not None
                and successor_state is not None
                and successor_state[1].broker_order_id is not None
                and lifecycle.replaced_by_broker_order_id
                != successor_state[1].broker_order_id
            ):
                raise CaptureContractError(
                    "broker replacement transition has invalid successor broker id"
                )

        previous = self._broker_state_by_intent.get(
            lifecycle.order_intent_sha256
        )
        if previous is None:
            if (
                lifecycle.transition is not CaptureBrokerTransition.SUBMITTED
                or lifecycle.prior_transition_event_sha256 is not None
                or lifecycle.cumulative_filled_quantity != 0
            ):
                raise CaptureContractError(
                    "broker lifecycle must begin with an unfilled submitted transition"
                )
            return

        previous_event, previous_lifecycle = previous
        if previous_lifecycle.terminal:
            raise CaptureContractError(
                "broker lifecycle cannot continue after a terminal transition"
            )
        if (
            lifecycle.prior_transition_event_sha256
            != previous_event.event_sha256
        ):
            raise CaptureContractError("broker lifecycle hash chain is broken")
        if lifecycle.transition is CaptureBrokerTransition.SUBMITTED:
            raise CaptureContractError("broker lifecycle transition order is invalid")
        if (
            lifecycle.transition is CaptureBrokerTransition.REPLACED
            and previous_lifecycle.transition
            is not CaptureBrokerTransition.PENDING_REPLACE
        ):
            raise CaptureContractError(
                "replaced lifecycle did not follow pending_replace"
            )
        fill_delta = (
            lifecycle.cumulative_filled_quantity
            - previous_lifecycle.cumulative_filled_quantity
        )
        if fill_delta < 0 or fill_delta != lifecycle.last_fill_quantity:
            raise CaptureContractError(
                "broker lifecycle cumulative fill progression is invalid"
            )
        if (
            previous_lifecycle.broker_order_id is not None
            and lifecycle.broker_order_id
            != previous_lifecycle.broker_order_id
        ):
            raise CaptureContractError(
                "broker lifecycle changed or dropped its broker order id"
            )
        if (
            lifecycle.replaces_broker_order_id
            != previous_lifecycle.replaces_broker_order_id
        ):
            raise CaptureContractError(
                "broker lifecycle changed its replacement predecessor lineage"
            )
        if (
            lifecycle.final_decision_attestation_sha256
            != previous_lifecycle.final_decision_attestation_sha256
            or lifecycle.reservation_claim_sha256
            != previous_lifecycle.reservation_claim_sha256
        ):
            raise CaptureContractError(
                "broker lifecycle changed its decision/reservation authority"
            )

    def retain_accepted_first_dip_detector(
        self,
        *,
        resolution: object,
        opportunity_key_sha256: str,
    ) -> str:
        """Retain one private, positively consumed detector receipt for reuse.

        Serialized detector debug is never accepted here.  The receipt must be
        the exact private object issued from this runtime's earlier active input
        attestation and its committed IQFeed read inventory.
        """

        try:
            from .first_dip_tape_decision import (  # noqa: PLC0415
                FirstDipTapeDecisionProviderError,
                _prior_detector_reference_from_resolution,
            )

            opportunity = _validated_sha256(
                opportunity_key_sha256,
                "first-dip detector opportunity",
            )
            reference = _prior_detector_reference_from_resolution(
                resolution,
                opportunity_key_sha256=opportunity,
            )
        except CaptureContractError:
            raise
        except (FirstDipTapeDecisionProviderError, TypeError, ValueError) as exc:
            raise CaptureContractError(
                "accepted first-dip detector receipt is invalid"
            ) from exc

        with self._lock:
            if (
                reference.authority_source != "captured_db_paper"
                or reference.run_id != self.identity.run_id
                or reference.generation != self.identity.generation
                or reference.opportunity_key_sha256 != opportunity
            ):
                raise CaptureContractError(
                    "accepted first-dip detector escaped the active capture run"
                )
            predecision = self._predecision_attestations.get(
                reference.decision_id
            )
            if predecision is None:
                raise CaptureContractError(
                    "accepted first-dip detector lacks its runtime input attestation"
                )
            predecision = verify_active_capture_input_attestation(predecision)
            if (
                predecision.attestation_sha256
                != reference.active_input_attestation_sha256
                or predecision.run_id != reference.run_id
                or predecision.generation != reference.generation
                or predecision.input_prefix_root_sha256
                != reference.input_prefix_root_sha256
                or predecision.first_dip_tape_read_id is None
                or predecision.first_dip_prior_detector_reference_sha256 is not None
                or predecision.first_dip_adaptive_request_sha256 is not None
                or predecision.first_dip_opportunity_key_sha256 is not None
            ):
                raise CaptureContractError(
                    "accepted first-dip detector input attestation is mismatched"
                )
            matching_reads = tuple(
                row
                for row in predecision.read_evidence
                if row.receipt.read_id == predecision.first_dip_tape_read_id
            )
            if len(matching_reads) != 1:
                raise CaptureContractError(
                    "accepted first-dip detector read evidence is ambiguous"
                )
            read = matching_reads[0]
            inventory_sha256 = sha256_json(
                {
                    "read_id": read.receipt.read_id,
                    "source_event_sha256s": list(
                        read.receipt.source_event_sha256s
                    ),
                }
            )
            if (
                read.receipt.symbol != reference.symbol
                or read.receipt_sha256 != reference.read_receipt_sha256
                or read.receipt_event_sha256 != reference.receipt_event_sha256
                or inventory_sha256 != reference.source_event_inventory_sha256
            ):
                raise CaptureContractError(
                    "accepted first-dip detector receipt inventory is mismatched"
                )
            existing = self._accepted_first_dip_detector_refs.get(opportunity)
            if existing is not None and sha256_json(existing.to_dict()) != sha256_json(
                reference.to_dict()
            ):
                raise CaptureContractError(
                    "first-dip opportunity already has a different detector receipt"
                )
            self._accepted_first_dip_detector_refs[opportunity] = reference
            return sha256_json(reference.to_dict())

    def attest_first_dip_pre_reservation_input_prefix(
        self,
        *,
        adaptive_request: object,
        dependency_profile: FSMDependencyProfile,
        first_dip_tape_read_id: str,
    ) -> ActiveCaptureInputPrefixAttestation:
        """Bind a fresh final tape read to one retained detector and request."""

        try:
            from .adaptive_risk_reservation import (  # noqa: PLC0415
                AdaptiveRiskReservationRequest,
                load_adaptive_risk_reservation_request,
            )

            if type(adaptive_request) is not AdaptiveRiskReservationRequest:
                raise CaptureContractError(
                    "first-dip final boundary requires a typed adaptive request"
                )
            request = load_adaptive_risk_reservation_request(
                adaptive_request.to_payload()
            )
        except CaptureContractError:
            raise
        except Exception as exc:
            raise CaptureContractError(
                "first-dip final adaptive request is invalid"
            ) from exc
        opportunity_key = request.opportunity_key
        if request.setup_family != "first_dip_reclaim" or opportunity_key is None:
            raise CaptureContractError(
                "first-dip final adaptive request lacks its opportunity"
            )
        opportunity = opportunity_key.key_sha256
        with self._lock:
            reference = self._accepted_first_dip_detector_refs.get(opportunity)
            read = self._read_evidence_by_id.get(
                str(first_dip_tape_read_id or "").strip()
            )
            detector_input = self._predecision_attestations.get(
                reference.decision_id
            ) if reference is not None else None
            if reference is None:
                raise CaptureContractError(
                    "first-dip final boundary lacks an accepted detector receipt"
                )
            if read is None:
                raise CaptureContractError(
                    "first-dip final boundary lacks its fresh tape receipt"
                )
            if (
                request.inputs.execution_surface
                not in {"alpaca_paper", "db_paper", "captured_db_paper"}
                or request.inputs.broker_environment != "paper"
                or request.inputs.replay_or_paper_run_id != self.identity.run_id
                or request.inputs.generation != self.identity.generation
                or request.inputs.symbol != reference.symbol
                or request.inputs.account_identity_sha256
                != self.identity.account_identity_sha256
                or request.inputs.code_build_sha256
                != self.identity.code_build_sha256
                or request.inputs.effective_config_sha256
                != self.identity.config_sha256
                or request.inputs.feature_flags_sha256
                != self.identity.feature_flags_sha256
                or detector_input is None
                or detector_input.input_prefix_root_sha256
                != request.inputs.capture_prefix_root_sha256
                or reference.opportunity_key_sha256 != opportunity
                or request.inputs.as_of < reference.decision_at
                or read.receipt.decision_id == reference.decision_id
                or read.receipt.symbol != reference.symbol
                or read.receipt.returned_at < request.inputs.as_of
            ):
                raise CaptureContractError(
                    "first-dip final boundary escaped detector/request capture identity"
                )
            final_binding = _FirstDipFinalInputBinding(
                prior_detector_reference_sha256=sha256_json(reference.to_dict()),
                adaptive_request_sha256=request.request_sha256,
                opportunity_key_sha256=opportunity,
                _verification_token=_FIRST_DIP_FINAL_INPUT_BINDING_TOKEN,
            )
            return self.attest_predecision_input_prefix(
                decision_id=read.receipt.decision_id,
                dependency_profile=dependency_profile,
                first_dip_tape_read_id=read.receipt.read_id,
                _first_dip_final_binding=final_binding,
            )

    def prepare_captured_first_dip_tape_authority(
        self,
        *,
        attestation: ActiveCaptureInputPrefixAttestation,
        policy: FirstDipTapePolicy,
        purpose: str,
    ) -> object:
        """Mint one evidence-only authority from runtime-owned source events."""

        proof = verify_active_capture_input_attestation(attestation)
        with self._lock:
            if self._predecision_attestations.get(proof.decision_id) is not proof:
                raise CaptureContractError(
                    "captured first-dip authority uses a foreign input attestation"
                )
            prior_reference = None
            if str(purpose or "").strip().lower() == "pre_reservation":
                opportunity = proof.first_dip_opportunity_key_sha256
                prior_reference = self._accepted_first_dip_detector_refs.get(
                    str(opportunity or "")
                )
                if (
                    prior_reference is None
                    or proof.first_dip_prior_detector_reference_sha256
                    != sha256_json(prior_reference.to_dict())
                ):
                    raise CaptureContractError(
                        "captured first-dip final proof lacks retained detector lineage"
                    )
            source_events: list[CaptureEvent] = []
            for row in proof.read_evidence:
                if row.receipt.read_id != proof.first_dip_tape_read_id:
                    continue
                for ref in row.source_event_refs:
                    event = self._recent_source_events.get(ref.event_sha256)
                    if event is None or event.event_sha256 != ref.event_sha256:
                        raise CaptureContractError(
                            "captured first-dip source event is no longer runtime-owned"
                        )
                    source_events.append(event)
            from .first_dip_tape_decision import (  # noqa: PLC0415
                _FIRST_DIP_TAPE_DECISION_AUTHORITY_ISSUER,
            )

            return (
                _FIRST_DIP_TAPE_DECISION_AUTHORITY_ISSUER
                .issue_captured_db_paper_from_active_input(
                    attestation=proof,
                    source_events=tuple(source_events),
                    policy=policy,
                    purpose=purpose,
                    prior_detector_reference=prior_reference,
                )
            )

    def attest_predecision_input_prefix(
        self,
        *,
        decision_id: str,
        dependency_profile: FSMDependencyProfile,
        first_dip_tape_read_id: str | None = None,
        _first_dip_final_binding: _FirstDipFinalInputBinding | None = None,
    ) -> ActiveCaptureInputPrefixAttestation:
        """Issue a one-time, non-authorizing proof for provisional compute."""

        with self._lock:
            final_binding = _first_dip_final_binding
            if final_binding is not None and (
                type(final_binding) is not _FirstDipFinalInputBinding
                or final_binding._verification_token
                is not _FIRST_DIP_FINAL_INPUT_BINDING_TOKEN
                or first_dip_tape_read_id is None
            ):
                raise CaptureContractError(
                    "predecision first-dip final binding is not runtime-issued"
                )
            attested_at = self._trusted_recorded_at(None)
            normalized_decision_id = str(decision_id or "").strip()
            if (
                not normalized_decision_id
                or normalized_decision_id in self._decision_ids
                or normalized_decision_id in self._predecision_attestations
            ):
                raise CaptureContractError(
                    "predecision input attestation decision id is invalid or reused"
                )
            if not isinstance(dependency_profile, FSMDependencyProfile):
                raise CaptureContractError(
                    "predecision input attestation requires a typed dependency profile"
                )
            if (
                self._run_open is None
                or self._run_close_event is not None
                or set(self.producers) != self._registered
                or self._quiescent
                or self._closed
                or self._submission_failure is not None
            ):
                raise CaptureContractError(
                    "predecision input attestation requires a healthy RUNNING lifecycle"
                )
            for producer_id in sorted(self.producers):
                self._check_heartbeat_deadline(producer_id, attested_at)
            read_ids = dependency_profile.required_read_ids
            evidence: list[ActiveCaptureReadEvidence] = []
            freshness_deadlines: list[datetime] = []
            for read_id in read_ids:
                row = self._read_evidence_by_id.get(read_id)
                if (
                    row is None
                    or row.receipt.decision_id != normalized_decision_id
                    or row.receipt_event_sequence > self._sequence
                ):
                    raise CaptureContractError(
                        "predecision input attestation receipt evidence is incomplete"
                    )
                dependency = dependency_profile.dependency_for(
                    row.receipt.stream
                )
                typed_empty_first_dip = (
                    row.receipt.stream is CaptureStream.IQFEED_PRINT
                    and row.receipt.read_id
                    == str(first_dip_tape_read_id or "").strip()
                    and row.receipt.empty_result
                    and not row.source_event_refs
                )
                if dependency.exact_provider_event_at_required and (
                    (not row.source_event_refs and not typed_empty_first_dip)
                    or any(
                        ref.provider_event_at is None
                        for ref in row.source_event_refs
                    )
                ):
                    raise CaptureContractError(
                        "predecision input attestation lacks exact provider event time"
                    )
                if dependency.market_reference_at_required and (
                    (not row.source_event_refs and not typed_empty_first_dip)
                    or any(
                        ref.market_reference_at is None
                        and ref.provider_event_at is None
                        for ref in row.source_event_refs
                    )
                ):
                    raise CaptureContractError(
                        "predecision input attestation lacks market reference time"
                    )
                source_clocks = tuple(
                    _coverage_source_clock(
                        row.receipt.stream,
                        ref,
                        exact_required=(
                            dependency.exact_provider_event_at_required
                        ),
                        reference_required=(
                            dependency.market_reference_at_required
                        ),
                    )
                    for ref in row.source_event_refs
                )
                freshest_source_at = max(
                    (clock for clock in source_clocks if clock is not None),
                    default=row.receipt.returned_at,
                )
                if freshest_source_at > attested_at:
                    raise CaptureContractError(
                        "predecision input attestation source clock is from the future"
                    )
                freshness_deadlines.append(
                    freshest_source_at
                    + timedelta(seconds=dependency.max_source_age_seconds)
                )
                evidence.append(row)
            if frozenset(
                row.receipt.stream for row in evidence
            ) != dependency_profile.required_streams:
                raise CaptureContractError(
                    "predecision reads do not exactly cover dependency streams"
                )
            first_dip_id = str(first_dip_tape_read_id or "").strip() or None
            first_dip_receipt: CaptureReadReceipt | None = None
            first_dip_query: FirstDipTapeReadQuery | None = None
            if first_dip_id is not None and (
                first_dip_id not in read_ids
                or first_dip_id not in self._first_dip_tape_by_id
            ):
                raise CaptureContractError(
                    "predecision input attestation lacks typed first-dip tape"
                )
            if first_dip_id is not None:
                first_dip_evidence = self._read_evidence_by_id[first_dip_id]
                first_dip_receipt = first_dip_evidence.receipt
                if first_dip_receipt.query is None:
                    raise CaptureContractError(
                        "predecision first-dip tape lacks its typed query"
                    )
                try:
                    first_dip_query = FirstDipTapeReadQuery.from_dict(
                        first_dip_receipt.query
                    )
                except FirstDipTapePolicyError as exc:
                    raise CaptureContractError(
                        "predecision first-dip tape query is invalid"
                    ) from exc
                first_dip_stream_rows = tuple(
                    row
                    for row in evidence
                    if row.receipt.stream is CaptureStream.IQFEED_PRINT
                )
                if (
                    len(first_dip_stream_rows) != 1
                    or first_dip_stream_rows[0].receipt.read_id != first_dip_id
                ):
                    raise CaptureContractError(
                        "predecision first-dip tape read is ambiguous"
                    )
            continuity_evidence: list[ActiveCaptureContinuityEvidence] = []
            evidence_by_stream: dict[
                CaptureStream, list[ActiveCaptureReadEvidence]
            ] = defaultdict(list)
            for row in evidence:
                evidence_by_stream[row.receipt.stream].append(row)
            for stream in sorted(
                dependency_profile.required_streams,
                key=lambda value: value.value,
            ):
                dependency = dependency_profile.dependency_for(stream)
                if dependency.coverage_start_at > attested_at:
                    raise CaptureContractError(
                        "predecision dependency coverage begins after attestation"
                    )
                for _gap_producer_id, gap in self._reported_gaps:
                    if (
                        gap.stream not in {stream, CaptureStream.COVERAGE_GAP}
                        or (
                            gap.stream is not CaptureStream.COVERAGE_GAP
                            and
                            gap.symbol is not None
                            and not any(
                                row.receipt.symbol == gap.symbol
                                for row in evidence_by_stream[stream]
                            )
                        )
                    ):
                        continue
                    if gap.intersects(dependency.coverage_start_at, attested_at):
                        raise CaptureContractError(
                            "predecision dependency intersects an unresolved coverage gap"
                        )
                if STREAM_POLICIES[stream].coverage_mode not in {
                    CoverageMode.CONTINUOUS,
                    CoverageMode.CHANGE_LOG,
                }:
                    continue
                owner_id = self._owners.get(stream)
                active_continuity = (
                    self._live_continuity.get((owner_id, stream))
                    if owner_id is not None
                    else None
                )
                if active_continuity is None:
                    raise CaptureContractError(
                        "predecision continuity-backed dependency lacks a live checkpoint"
                    )
                stream_rows = evidence_by_stream[stream]
                source_refs = tuple(
                    ref for row in stream_rows for ref in row.source_event_refs
                )
                coverage = active_continuity.coverage
                watermark = coverage.watermark
                if watermark is None:
                    raise CaptureContractError(
                        "predecision continuity-backed dependency lacks a watermark"
                    )
                current_stats = self._stream_stats.get(stream)
                first_dip_frontier_bound = (
                    first_dip_query.source_frontier_sequence
                    if (
                        stream is CaptureStream.IQFEED_PRINT
                        and first_dip_query is not None
                        and len(stream_rows) == 1
                        and stream_rows[0].receipt.read_id == first_dip_id
                    )
                    else None
                )
                empty_first_dip_window = (
                    first_dip_frontier_bound is not None
                    and len(stream_rows) == 1
                    and stream_rows[0].receipt.empty_result
                    and not source_refs
                )
                if not source_refs and not empty_first_dip_window:
                    raise CaptureContractError(
                        "predecision continuity-backed dependency has no source frontier"
                    )
                latest_source_sequence = max(
                    (ref.sequence for ref in source_refs),
                    default=first_dip_frontier_bound or 0,
                )
                if (
                    active_continuity.coverage_event_sequence > self._sequence
                    or current_stats is None
                    or active_continuity.source_frontier_sequence
                    != int(current_stats["source_sequence_max"])
                    or (
                        first_dip_frontier_bound is None
                        and latest_source_sequence
                        != active_continuity.source_frontier_sequence
                    )
                    or (
                        first_dip_frontier_bound is not None
                        and (
                            active_continuity.source_frontier_sequence
                            < first_dip_frontier_bound
                            or any(
                                ref.sequence > first_dip_frontier_bound
                                for ref in source_refs
                            )
                        )
                    )
                    or coverage.first_available_at > dependency.coverage_start_at
                    or coverage.last_available_at
                    < max(
                        (ref.available_at for ref in source_refs),
                        default=coverage.first_available_at,
                    )
                    or (
                        attested_at
                        - active_continuity.coverage_committed_available_at
                    ).total_seconds()
                    > self.heartbeat_timeout_seconds
                    or (
                        attested_at - watermark.emitted_available_at
                    ).total_seconds()
                    > self.heartbeat_timeout_seconds
                    or watermark.event_watermark_at
                    < attested_at
                    - timedelta(seconds=watermark.bounded_lateness_seconds)
                    or any(
                        row.receipt.provider != coverage.provider
                        or row.receipt.symbol != coverage.symbol
                        for row in stream_rows
                    )
                    or any(
                        source_clock is not None
                        and watermark.event_watermark_at < source_clock
                        for source_clock in (
                            _coverage_source_clock(
                                stream,
                                ref,
                                exact_required=(
                                    dependency.exact_provider_event_at_required
                                ),
                                reference_required=(
                                    dependency.market_reference_at_required
                                ),
                            )
                            for ref in source_refs
                        )
                    )
                ):
                    raise CaptureContractError(
                        "predecision continuity does not cover the dependency window/frontier"
                    )
                freshness_deadlines.extend(
                    (
                        active_continuity.coverage_committed_available_at
                        + timedelta(seconds=self.heartbeat_timeout_seconds),
                        watermark.emitted_available_at
                        + timedelta(seconds=self.heartbeat_timeout_seconds),
                        watermark.event_watermark_at
                        + timedelta(seconds=watermark.bounded_lateness_seconds),
                    )
                )
                continuity_evidence.append(active_continuity)
            expires_at = min(freshness_deadlines)
            if expires_at <= attested_at:
                raise CaptureContractError(
                    "predecision inputs are already stale under dependency policy"
                )
            if first_dip_id is not None:
                if first_dip_receipt is None or first_dip_query is None:
                    raise CaptureContractError(
                        "predecision first-dip receipt/query binding is missing"
                    )
                first_dip_dependency = dependency_profile.dependency_for(
                    CaptureStream.IQFEED_PRINT
                )
                first_dip_continuity = next(
                    (
                        row
                        for row in continuity_evidence
                        if row.coverage.stream is CaptureStream.IQFEED_PRINT
                    ),
                    None,
                )
                first_dip_watermark = (
                    first_dip_continuity.coverage.watermark
                    if first_dip_continuity is not None
                    else None
                )
                if (
                    first_dip_query.symbol != first_dip_receipt.symbol
                    or first_dip_query.provider != first_dip_receipt.provider
                    or first_dip_query.decision_at
                    != first_dip_receipt.returned_at
                    or first_dip_query.source_frontier_sequence
                    >= first_dip_evidence.receipt_event_sequence
                    or first_dip_dependency.coverage_start_at
                    > first_dip_query.event_start_exclusive
                    or first_dip_continuity is None
                    or first_dip_watermark is None
                    or first_dip_continuity.coverage.first_available_at
                    > first_dip_query.event_start_exclusive
                    or first_dip_watermark.event_watermark_at
                    < first_dip_query.event_end_inclusive
                ):
                    raise CaptureContractError(
                        "predecision first-dip continuity does not cover the exact query"
                    )
            admission_handoff_sha256: str | None = None
            if CaptureStream.ADMISSION_ELIGIBILITY in evidence_by_stream:
                handoff_hashes: set[str] = set()
                for row in evidence_by_stream[CaptureStream.ADMISSION_ELIGIBILITY]:
                    for ref in row.source_event_refs:
                        source_event = self._recent_source_events.get(
                            ref.event_sha256
                        )
                        release = (
                            source_event.payload.get("_capture_release")
                            if source_event is not None
                            else None
                        )
                        if not isinstance(release, Mapping):
                            raise CaptureContractError(
                                "predecision admission dependency lacks promoted causal provenance"
                            )
                        handoff_hashes.add(
                            _validated_sha256(
                                release.get("admission_handoff_sha256"),
                                "promoted admission handoff",
                            )
                        )
                if len(handoff_hashes) != 1:
                    raise CaptureContractError(
                        "predecision admission dependency mixes causal handoffs"
                    )
                admission_handoff_sha256 = next(iter(handoff_hashes))
            proof = _issue_active_capture_input_attestation(
                run_id=self.identity.run_id,
                generation=self.identity.generation,
                decision_id=normalized_decision_id,
                input_prefix_sequence=self._sequence,
                input_prefix_root_sha256=self._current_prefix_root(),
                attested_available_at=attested_at,
                expires_at=expires_at,
                dependency_profile=dependency_profile,
                identity_sha256=self.identity.identity_sha256,
                account_identity_sha256=self.identity.account_identity_sha256,
                code_build_sha256=self.identity.code_build_sha256,
                config_sha256=self.identity.config_sha256,
                feature_flags_sha256=self.identity.feature_flags_sha256,
                resource_binding_sha256=self.resource_binding.binding_sha256,
                producer_generations={
                    producer_id: producer.generation
                    for producer_id, producer in self.producers.items()
                },
                required_read_ids=read_ids,
                read_evidence=tuple(evidence),
                continuity_evidence=tuple(continuity_evidence),
                admission_handoff_sha256=admission_handoff_sha256,
                first_dip_tape_read_id=first_dip_id,
                first_dip_prior_detector_reference_sha256=(
                    None
                    if final_binding is None
                    else final_binding.prior_detector_reference_sha256
                ),
                first_dip_adaptive_request_sha256=(
                    None
                    if final_binding is None
                    else final_binding.adaptive_request_sha256
                ),
                first_dip_opportunity_key_sha256=(
                    None
                    if final_binding is None
                    else final_binding.opportunity_key_sha256
                ),
            )
            self._predecision_attestations[normalized_decision_id] = proof
            return proof

    def submit_input(
        self,
        producer_id: str,
        *,
        stream: CaptureStream,
        clocks: CaptureClocks,
        payload: Mapping[str, Any],
        provider: str,
        symbol: str | None = None,
        query: Mapping[str, Any] | None = None,
        recorded_at: datetime | None = None,
        promotion_id: str | None = None,
        promoted_at: datetime | None = None,
        promotion_source_identity_sha256: str | None = None,
        promotion_resource_binding_sha256: str | None = None,
        promotion_inventory_sha256: str | None = None,
        promotion_admission_handoff_sha256: str | None = None,
        promotion_transfer: PromotionTransfer | None = None,
        predecision_attestation: ActiveCaptureInputPrefixAttestation | None = None,
    ) -> CaptureEvent:
        with self._lock:
            recorded = self._trusted_recorded_at(recorded_at)
            producer = self._require_open_producer(producer_id)
            self._check_heartbeat_deadline(producer.producer_id, recorded)
            normalized_stream = (
                stream if isinstance(stream, CaptureStream) else CaptureStream(str(stream))
            )
            if normalized_stream not in producer.streams:
                raise CaptureContractError(
                    f"producer {producer.producer_id} does not own {normalized_stream.value}"
            )
            if not isinstance(clocks, CaptureClocks):
                raise CaptureContractError("capture input clocks are malformed")
            if recorded < clocks.received_at:
                self._latch_failure("trusted_capture_clock_precedes_received_at")
                raise CaptureContractError(
                    "trusted capture clock cannot precede input received_at"
                )
            original_available_at = clocks.available_at
            if original_available_at > recorded:
                self._latch_failure("producer_asserted_future_available_at")
                raise CaptureContractError(
                    "producer input available_at is later than the trusted wall clock"
                )
            normalized_promotion_id = str(promotion_id or "").strip() or None
            if normalized_promotion_id is not None:
                try:
                    normalized_promotion_id = str(
                        uuid.UUID(normalized_promotion_id)
                    )
                except ValueError as exc:
                    raise CaptureContractError("promotion id is malformed") from exc
            promotion_provenance = (
                promotion_source_identity_sha256,
                promotion_resource_binding_sha256,
                promotion_inventory_sha256,
            )
            if (normalized_promotion_id is None) != (promoted_at is None) or (
                normalized_promotion_id is None
                and (
                    any(value is not None for value in promotion_provenance)
                    or promotion_admission_handoff_sha256 is not None
                )
            ):
                raise CaptureContractError(
                    "promotion id, boundary, and provenance must be supplied together"
                )
            if normalized_promotion_id is not None and any(
                value is None for value in promotion_provenance
            ):
                raise CaptureContractError("promotion provenance is incomplete")
            if (normalized_promotion_id is None) != (promotion_transfer is None):
                raise CaptureContractError(
                    "promotion requires its exact opaque pre-trigger transfer"
                )
            if promotion_transfer is not None:
                if not isinstance(promotion_transfer, PromotionTransfer):
                    raise CaptureContractError("promotion transfer is malformed")
                promotion_metadata = (
                    payload.get("_capture_promotion")
                    if isinstance(payload, Mapping)
                    else None
                )
                provisional_sha256 = (
                    promotion_metadata.get("provisional_event_sha256")
                    if isinstance(promotion_metadata, Mapping)
                    else None
                )
                source_rows = tuple(
                    row
                    for row in promotion_transfer.events
                    if row.event_sha256 == provisional_sha256
                )
                raw_payload = (
                    {
                        key: value
                        for key, value in payload.items()
                        if key != "_capture_promotion"
                    }
                    if isinstance(payload, Mapping)
                    else None
                )
                if (
                    len(source_rows) != 1
                    or normalized_promotion_id
                    != promotion_transfer.promotion_id
                    or _utc(promoted_at, "promoted_at")
                    != promotion_transfer.promoted_at
                    or promotion_source_identity_sha256
                    != promotion_transfer.source_identity_sha256
                    or promotion_resource_binding_sha256
                    != promotion_transfer.resource_binding_sha256
                    or promotion_inventory_sha256
                    != promotion_transfer.inventory_sha256
                    or promotion_admission_handoff_sha256
                    != promotion_transfer.admission_handoff_sha256
                ):
                    raise CaptureContractError(
                        "promotion provenance differs from its opaque transfer"
                    )
                source_row = source_rows[0]
                if (
                    source_row.stream is not normalized_stream
                    or source_row.provider != str(provider or "").strip()
                    or source_row.symbol != str(symbol or "").strip().upper()
                    or source_row.clocks != clocks
                    or source_row.query != query
                    or source_row.payload != raw_payload
                ):
                    raise CaptureContractError(
                        "promoted input differs from its retained source event"
                    )
            if promoted_at is not None and _utc(
                promoted_at, "promoted_at"
            ) != recorded:
                self._latch_failure("promotion_boundary_clock_mismatch")
                raise CaptureContractError(
                    "promotion boundary differs from the trusted wall clock"
                )
            captured_payload: Mapping[str, Any] = payload
            if original_available_at != recorded or normalized_promotion_id is not None:
                if not isinstance(payload, Mapping):
                    raise CaptureContractError("capture input payload is malformed")
                if "_capture_release" in payload:
                    raise CaptureContractError(
                        "producer payload uses reserved _capture_release metadata"
                    )
                release: dict[str, Any] = {
                    "original_available_at": _iso(original_available_at),
                    "released_available_at": _iso(recorded),
                    "release_kind": (
                        "hot_symbol_promotion"
                        if normalized_promotion_id is not None
                        else "observed_ingress"
                    ),
                }
                if normalized_promotion_id is not None:
                    release["promotion_id"] = normalized_promotion_id
                    release["promoted_at"] = _iso(recorded)
                    release["source_identity_sha256"] = _validated_sha256(
                        promotion_source_identity_sha256 or "",
                        "promotion source identity",
                    )
                    release["resource_binding_sha256"] = _validated_sha256(
                        promotion_resource_binding_sha256 or "",
                        "promotion resource binding",
                    )
                    release["inventory_sha256"] = _validated_sha256(
                        promotion_inventory_sha256 or "", "promotion inventory"
                    )
                    if promotion_admission_handoff_sha256 is not None:
                        release["admission_handoff_sha256"] = _validated_sha256(
                            promotion_admission_handoff_sha256,
                            "promotion admission handoff",
                        )
                    if (
                        release["resource_binding_sha256"]
                        != self.resource_binding.binding_sha256
                    ):
                        raise CaptureContractError(
                            "promotion resource binding differs from target lifecycle"
                        )
                captured_payload = {**dict(payload), "_capture_release": release}
            durable_clocks = CaptureClocks(
                provider_event_at=clocks.provider_event_at,
                market_reference_at=clocks.market_reference_at,
                received_at=clocks.received_at,
                available_at=recorded,
            )
            decision_id: str | None = None
            decision_output: CaptureDecisionOutput | None = None
            consumed_predecision: ActiveCaptureInputPrefixAttestation | None = None
            broker_lifecycle: CaptureBrokerOrderLifecycle | None = None
            if normalized_stream is CaptureStream.FSM_DECISION:
                decision_id = str(captured_payload.get("decision_id") or "").strip()
                if not decision_id or decision_id in self._decision_ids:
                    raise CaptureContractError("FSM decision id is missing or duplicated")
                required_read_ids = tuple(
                    sorted(
                        str(value).strip()
                        for value in captured_payload.get("required_read_ids", ())
                    )
                )
                if any(not value for value in required_read_ids) or len(
                    required_read_ids
                ) != len(set(required_read_ids)):
                    raise CaptureContractError("FSM decision read ids are malformed")
                if any(
                    read_id not in self._receipt_by_id
                    or self._receipt_by_id[read_id].decision_id != decision_id
                    for read_id in required_read_ids
                ):
                    raise CaptureContractError(
                        "FSM decision references a missing or unrelated read receipt"
                    )
                decision_output = capture_decision_output_from_payload(
                    captured_payload
                )
                adaptive_artifacts = capture_adaptive_order_artifacts_from_payload(
                    captured_payload,
                    decision_output,
                    identity=self.identity,
                )
                try:
                    prefix_sequence = int(
                        captured_payload.get("input_prefix_sequence") or 0
                    )
                except (TypeError, ValueError) as exc:
                    raise CaptureContractError(
                        "FSM decision prefix sequence is malformed"
                    ) from exc
                if predecision_attestation is None:
                    if decision_output.action is CaptureDecisionAction.ORDER_INTENT:
                        raise CaptureContractError(
                            "order decision requires an opaque predecision input attestation"
                        )
                    if any(
                        key in captured_payload
                        for key in (
                            "predecision_attestation_sha256",
                            "predecision_read_evidence_inventory_sha256",
                            "predecision_continuity_evidence_inventory_sha256",
                            "predecision_admission_handoff_sha256",
                        )
                    ):
                        raise CaptureContractError(
                            "decision cannot substitute serialized predecision hashes"
                        )
                    expected_prefix_sequence = self._sequence
                    expected_prefix = self._current_prefix_root()
                else:
                    proof = verify_active_capture_input_attestation(
                        predecision_attestation
                    )
                    stored = self._predecision_attestations.get(decision_id)
                    if (
                        stored is not proof
                        or decision_id in self._consumed_predecision_attestations
                        or proof.decision_id != decision_id
                        or proof.identity_sha256 != self.identity.identity_sha256
                        or proof.resource_binding_sha256
                        != self.resource_binding.binding_sha256
                        or recorded > proof.expires_at
                        or required_read_ids != proof.required_read_ids
                    ):
                        raise CaptureContractError(
                            "predecision input attestation is expired, reused, or unrelated"
                        )
                    if (
                        captured_payload.get("predecision_attestation_sha256")
                        != proof.attestation_sha256
                        or captured_payload.get(
                            "predecision_read_evidence_inventory_sha256"
                        )
                        != proof.read_evidence_inventory_sha256
                        or captured_payload.get(
                            "predecision_continuity_evidence_inventory_sha256"
                        )
                        != proof.continuity_evidence_inventory_sha256
                    ):
                        raise CaptureContractError(
                            "decision payload does not bind its opaque predecision proof"
                        )
                    payload_admission_handoff = captured_payload.get(
                        "predecision_admission_handoff_sha256"
                    )
                    if payload_admission_handoff != proof.admission_handoff_sha256:
                        raise CaptureContractError(
                            "decision payload does not bind its causal admission handoff"
                        )
                    if (
                        captured_payload.get("first_dip_tape_read_id")
                        != proof.first_dip_tape_read_id
                    ):
                        raise CaptureContractError(
                            "decision payload does not bind its typed first-dip read"
                        )
                    raw_dependency_profile = captured_payload.get(
                        "fsm_dependency_profile"
                    )
                    if not isinstance(raw_dependency_profile, Mapping):
                        raise CaptureContractError(
                            "decision payload lacks typed FSM dependency profile"
                        )
                    decision_dependency_profile = FSMDependencyProfile.from_dict(
                        raw_dependency_profile
                    )
                    if decision_dependency_profile != proof.dependency_profile:
                        raise CaptureContractError(
                            "decision dependency profile differs from predecision proof"
                        )
                    if (
                        decision_output.setup_role == "first_dip_reclaim"
                        and decision_output.action
                        is CaptureDecisionAction.ORDER_INTENT
                        and proof.first_dip_tape_read_id is None
                    ):
                        raise CaptureContractError(
                            "first-dip order decision lacks typed tape evidence"
                        )
                    if proof.first_dip_tape_read_id is not None:
                        if decision_output.setup_role != "first_dip_reclaim":
                            raise CaptureContractError(
                                "typed first-dip evidence is bound to another setup"
                            )
                        first_dip_receipt = self._receipt_by_id.get(
                            proof.first_dip_tape_read_id
                        )
                        raw_policy = captured_payload.get("first_dip_tape_policy")
                        raw_evaluation = captured_payload.get(
                            "first_dip_tape_evaluation"
                        )
                        if (
                            first_dip_receipt is None
                            or first_dip_receipt.query is None
                            or not isinstance(raw_policy, Mapping)
                            or not isinstance(raw_evaluation, Mapping)
                            or clocks.market_reference_at is None
                        ):
                            raise CaptureContractError(
                                "decision lacks typed first-dip policy/evaluation provenance"
                            )
                        source_events = tuple(
                            self._recent_source_events.get(source_sha256)
                            for source_sha256 in first_dip_receipt.source_event_sha256s
                        )
                        if any(source is None for source in source_events):
                            raise CaptureContractError(
                                "decision first-dip source events are unavailable"
                            )
                        try:
                            first_dip_policy = FirstDipTapePolicy.from_dict(
                                raw_policy
                            )
                            first_dip_query = FirstDipTapeReadQuery.from_dict(
                                first_dip_receipt.query
                            )
                            first_dip_query.validate_for_policy(
                                first_dip_policy
                            )
                            first_dip_window = first_dip_tape_window_from_capture(
                                first_dip_receipt,
                                tuple(
                                    source
                                    for source in source_events
                                    if source is not None
                                ),
                            )
                            first_dip_evaluation = evaluate_first_dip_tape(
                                first_dip_window,
                                policy=first_dip_policy,
                                decision_at=clocks.market_reference_at,
                                symbol=str(symbol or ""),
                            )
                        except (FirstDipTapePolicyError, CaptureContractError) as exc:
                            raise CaptureContractError(
                                "decision first-dip policy/evidence is invalid"
                            ) from exc
                        first_dip_dependency = (
                            decision_dependency_profile.dependency_for(
                                CaptureStream.IQFEED_PRINT
                            )
                        )
                        first_dip_continuity = next(
                            (
                                row
                                for row in proof.continuity_evidence
                                if row.coverage.stream
                                is CaptureStream.IQFEED_PRINT
                            ),
                            None,
                        )
                        first_dip_watermark = (
                            first_dip_continuity.coverage.watermark
                            if first_dip_continuity is not None
                            else None
                        )
                        if (
                            first_dip_query.decision_at
                            != clocks.market_reference_at
                            or first_dip_query.available_at_most
                            != first_dip_receipt.returned_at
                            or first_dip_dependency.coverage_start_at
                            > first_dip_query.event_start_exclusive
                            or not math.isclose(
                                first_dip_dependency.max_source_age_seconds,
                                first_dip_policy.max_source_age_seconds,
                                rel_tol=0.0,
                                abs_tol=1e-12,
                            )
                            or first_dip_continuity is None
                            or first_dip_watermark is None
                            or first_dip_watermark.event_watermark_at
                            < first_dip_query.event_end_inclusive
                            or captured_payload.get(
                                "first_dip_tape_policy_sha256"
                            )
                            != first_dip_policy.policy_sha256
                            or dict(raw_evaluation)
                            != first_dip_evaluation.to_dict()
                            or captured_payload.get(
                                "first_dip_tape_evaluation_sha256"
                            )
                            != first_dip_evaluation.evaluation_sha256
                        ):
                            raise CaptureContractError(
                                "decision first-dip evaluation differs from exact prints"
                            )
                        if (
                            decision_output.action
                            is CaptureDecisionAction.ORDER_INTENT
                            and (
                                first_dip_evaluation.status != "valid_positive"
                                or not first_dip_evaluation.confirmed
                                or first_dip_evaluation.reason
                                != "first_dip_tape_confirmed"
                            )
                        ):
                            raise CaptureContractError(
                                "first-dip order decision lacks a positive tape verdict"
                            )
                        if (
                            decision_output.action
                            is CaptureDecisionAction.ORDER_INTENT
                        ):
                            # CaptureIqfeedPrint v1 proves deterministic mechanics,
                            # not provider-documented trade occurrence/correction/
                            # condition completeness.  It may drive sealed offline
                            # experiments, but it cannot authorize a broker mutation.
                            raise CaptureContractError(
                                "first-dip IQFeed v1 evidence is mechanics-only"
                            )
                    if (
                        decision_output.action
                        is CaptureDecisionAction.ORDER_INTENT
                        and any(
                            intent.risk_increasing
                            for intent in decision_output.order_intents
                        )
                        and any(
                            self._receipt_by_id[read_id].stream
                            is CaptureStream.IQFEED_PRINT
                            for read_id in required_read_ids
                        )
                    ):
                        # IQFEED_PRINT receipts currently carry only the v1
                        # CaptureIqfeedPrint capability.  That contract proves
                        # deterministic mechanics, not provider-documented
                        # occurrence/correction/condition completeness.  Bind
                        # this prohibition to the canonical read inventory and
                        # risk direction so a setup label or omitted typed
                        # first-dip id cannot promote the evidence, while exits
                        # remain available.  A future authoritative schema must
                        # introduce an explicit receipt capability before this
                        # guard can distinguish and admit it.
                        raise CaptureContractError(
                            "risk-increasing order cannot use IQFeed print v1 "
                            "mechanics-only evidence"
                        )
                    if any(
                        intent.decision_provenance_sha256
                        != proof.attestation_sha256
                        for intent in decision_output.order_intents
                    ):
                        raise CaptureContractError(
                            "order intent does not bind its predecision proof"
                        )
                    if any(
                        str(
                            artifact.decision_packet.get("input_snapshot", {}).get(
                                "capture_prefix_root_sha256"
                            )
                            or ""
                        ).lower()
                        != proof.input_prefix_root_sha256
                        for artifact in adaptive_artifacts
                    ):
                        raise CaptureContractError(
                            "adaptive artifacts use a different captured input prefix"
                        )
                    expected_prefix_sequence = proof.input_prefix_sequence
                    expected_prefix = proof.input_prefix_root_sha256
                    consumed_predecision = proof
                if prefix_sequence != expected_prefix_sequence:
                    raise CaptureContractError(
                        "FSM decision prefix sequence differs from its trusted input proof"
                    )
                if str(
                    captured_payload.get("input_prefix_root_sha256") or ""
                ).strip().lower() != expected_prefix:
                    raise CaptureContractError("FSM decision prefix root is incorrect")
                if decision_output.symbol != str(symbol or "").strip().upper():
                    raise CaptureContractError(
                        "FSM decision output differs from its event symbol"
                    )
                if (
                    decision_output.action is CaptureDecisionAction.ORDER_INTENT
                    and CaptureStream.BROKER_ORDER_LIFECYCLE not in self._owners
                ):
                    raise CaptureContractError(
                        "order decision lacks a broker lifecycle producer"
                    )
                for intent in decision_output.order_intents:
                    if intent.order_intent_sha256 in self._order_intents_by_sha:
                        raise CaptureContractError(
                            "FSM decision repeats a durable order intent"
                        )
                    prior_intent_sha256 = self._order_intent_sha_by_client_id.get(
                        intent.client_order_id
                    )
                    if prior_intent_sha256 is not None:
                        raise CaptureContractError(
                            "FSM decision repeats a durable client order id"
                        )
                    if intent.intent_role is CaptureOrderIntentRole.REPLACEMENT:
                        predecessor_sha256 = intent.replaces_order_intent_sha256
                        assert predecessor_sha256 is not None
                        predecessor = self._order_intents_by_sha.get(
                            predecessor_sha256
                        )
                        predecessor_state = self._broker_state_by_intent.get(
                            predecessor_sha256
                        )
                        if (
                            predecessor is None
                            or predecessor_state is None
                            or predecessor_state[1].terminal
                            or predecessor_state[1].broker_order_id is None
                            or predecessor[1].symbol != intent.symbol
                        ):
                            raise CaptureContractError(
                                "replacement intent lacks an active durable predecessor"
                            )
            elif normalized_stream is CaptureStream.BROKER_ORDER_LIFECYCLE:
                if original_available_at != recorded:
                    raise CaptureContractError(
                        "broker lifecycle must be committed at its trusted availability"
                    )
                broker_lifecycle = CaptureBrokerOrderLifecycle.from_dict(
                    captured_payload
                )
                self._validate_broker_lifecycle_transition(
                    broker_lifecycle,
                    symbol=symbol,
                )
            event = CaptureEvent(
                identity=self.identity,
                sequence=self._proposed_sequence(recorded),
                stream=normalized_stream,
                clocks=durable_clocks,
                payload=captured_payload,
                provider=provider,
                symbol=symbol,
                query=query,
            )
            event = self._submit(event)
            self._observe_source_event(event)
            self._last_input_sequence[producer.producer_id] = event.sequence
            if decision_id is not None:
                assert decision_output is not None
                self._decision_ids.add(decision_id)
                self._decision_events[decision_id] = event
                self._decision_outputs[decision_id] = decision_output
                if consumed_predecision is not None:
                    self._consumed_predecision_attestations.add(decision_id)
                for intent in decision_output.order_intents:
                    self._order_intents_by_sha[intent.order_intent_sha256] = (
                        decision_id,
                        intent,
                    )
                    self._order_intent_sha_by_client_id[
                        intent.client_order_id
                    ] = intent.order_intent_sha256
            if broker_lifecycle is not None:
                self._broker_state_by_intent[
                    broker_lifecycle.order_intent_sha256
                ] = (event, broker_lifecycle)
            return event

    def submit_broker_order_lifecycle(
        self,
        producer_id: str,
        lifecycle: CaptureBrokerOrderLifecycle,
        *,
        provider: str,
        symbol: str,
        clocks: CaptureClocks,
        recorded_at: datetime | None = None,
    ) -> CaptureEvent:
        """Commit one typed broker transition bound to a durable order intent."""

        if not isinstance(lifecycle, CaptureBrokerOrderLifecycle):
            raise CaptureContractError("broker order lifecycle is malformed")
        return self.submit_input(
            producer_id,
            stream=CaptureStream.BROKER_ORDER_LIFECYCLE,
            provider=provider,
            symbol=symbol,
            clocks=clocks,
            payload=lifecycle.to_dict(),
            recorded_at=recorded_at,
        )

    def submit_read_receipt(self, receipt: CaptureReadReceipt) -> CaptureEvent:
        """Commit one exact decision read through the lifecycle sequence boundary."""

        with self._lock:
            recorded = self._trusted_recorded_at(None)
            if not isinstance(receipt, CaptureReadReceipt):
                raise CaptureContractError("capture read receipt is malformed")
            owner_id = self._owners.get(receipt.stream)
            if owner_id is None:
                raise CaptureContractError("read receipt source stream has no owner")
            producer = self._require_open_producer(owner_id)
            self._check_heartbeat_deadline(producer.producer_id, recorded)
            if receipt.identity_sha256 != self.identity.identity_sha256:
                raise CaptureContractError("read receipt identity does not match capture run")
            if receipt.read_id in self._receipt_by_id:
                raise CaptureContractError("read receipt id was already committed")
            if receipt.decision_id in self._decision_ids:
                raise CaptureContractError("read receipt cannot be committed after its decision")
            assert self._run_open is not None
            if (
                receipt.requested_at < self._run_open.opened_at
                or receipt.returned_at > recorded
            ):
                raise CaptureContractError(
                    "read receipt clocks are outside the trusted capture interval"
                )
            if not receipt.content_verified or receipt.replay_network_fallback_used:
                self._latch_failure("read_receipt_unverified_or_network_fallback")
                raise CaptureContractError(
                    "read receipt must be content-verified without network fallback"
                )
            if len(receipt.source_event_sha256s) != len(
                set(receipt.source_event_sha256s)
            ):
                raise CaptureContractError("read receipt repeats a source event")
            source_refs: list[CaptureEventRef] = []
            for source_sha256 in receipt.source_event_sha256s:
                source = self._recent_source_events.get(source_sha256)
                if source is None:
                    self._latch_failure("read_receipt_source_outside_bounded_index")
                    raise CaptureContractError(
                        "read receipt references an event outside this durable prefix"
                    )
                if (
                    source.stream is not receipt.stream
                    or source.provider != receipt.provider
                    or source.symbol != receipt.symbol
                    or source.clocks.available_at > receipt.returned_at
                    or (
                        STREAM_POLICIES[receipt.stream].query_parameters_required
                        and source.query_sha256 != receipt.query_sha256
                    )
                ):
                    raise CaptureContractError(
                        "read receipt source event does not match its exact read"
                    )
                source_refs.append(CaptureEventRef.from_event(source))
            if captured_read_result_sha256(source_refs) != receipt.result_sha256:
                raise CaptureContractError("read receipt result hash is incorrect")
            existing_empty_stats = self._stream_stats.get(receipt.stream)
            if (
                receipt.empty_result
                and STREAM_POLICIES[receipt.stream].coverage_mode
                is CoverageMode.QUERY_RECEIPT
                and existing_empty_stats is not None
                and (
                    existing_empty_stats["providers"] != {receipt.provider}
                    or existing_empty_stats["symbols"] != {receipt.symbol}
                )
            ):
                raise CaptureContractError(
                    "empty read receipt conflicts with its stream envelope"
                )
            event = CaptureEvent(
                identity=self.identity,
                sequence=self._proposed_sequence(recorded),
                stream=CaptureStream.READ_RECEIPT,
                provider=receipt.provider,
                symbol=receipt.symbol,
                clocks=CaptureClocks(received_at=recorded, available_at=recorded),
                payload=receipt.to_dict(),
            )
            event = self._submit(event)
            read_evidence = ActiveCaptureReadEvidence(
                receipt=receipt,
                receipt_sha256=sha256_json(receipt.to_dict()),
                receipt_event_sha256=event.event_sha256,
                receipt_event_sequence=event.sequence,
                receipt_committed_available_at=event.clocks.available_at,
                producer_id=producer.producer_id,
                producer_generation=producer.generation,
                source_event_refs=tuple(source_refs),
            )
            self._receipt_by_id[receipt.read_id] = receipt
            self._read_evidence_by_id[receipt.read_id] = read_evidence
            self._receipt_count_by_stream[receipt.stream] += 1
            if (
                receipt.empty_result
                and STREAM_POLICIES[receipt.stream].coverage_mode
                is CoverageMode.QUERY_RECEIPT
            ):
                stats = self._stream_stats.get(receipt.stream)
                if stats is None:
                    self._stream_stats[receipt.stream] = {
                        "event_count": 0,
                        "first_available_at": receipt.returned_at,
                        "last_available_at": receipt.returned_at,
                        "providers": {receipt.provider},
                        "symbols": {receipt.symbol},
                        "exact_event_clock_complete": True,
                    }
                elif stats["event_count"] == 0:
                    stats["first_available_at"] = min(
                        stats["first_available_at"], receipt.returned_at
                    )
                    stats["last_available_at"] = max(
                        stats["last_available_at"], receipt.returned_at
                    )
            self._receipt_event_owner[event.event_sha256] = producer.producer_id
            self._last_input_sequence[producer.producer_id] = event.sequence
            return event

    def submit_microstructure_window_receipt(
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
    ) -> tuple[CaptureEvent, CaptureReadReceipt, tuple[CaptureEvent, ...]]:
        """Atomically inventory and receipt one complete provider source window.

        The lifecycle owns the bounded source index and holds its append lock
        from inventory through receipt commit.  Callers provide only the
        economic operation and window; they cannot choose source hashes or the
        global source frontier represented by the receipt.
        """

        requested = _utc(requested_at, "microstructure requested_at")
        returned = _utc(returned_at, "microstructure returned_at")
        start = _utc(
            event_start_exclusive,
            "microstructure event_start_exclusive",
        )
        end = _utc(
            event_end_inclusive,
            "microstructure event_end_inclusive",
        )
        normalized_provider = str(provider or "").strip().lower()
        normalized_symbol = str(symbol or "").strip().upper()
        if (
            returned < requested
            or not start < end
            or end > returned
            or normalized_provider != "iqfeed"
            or not normalized_symbol
        ):
            raise CaptureContractError(
                "microstructure source-window request is malformed"
            )
        try:
            resolved_operation = (
                operation
                if isinstance(operation, CaptureMicrostructureOperation)
                else CaptureMicrostructureOperation(str(operation))
            )
            resolved_stream = (
                stream
                if isinstance(stream, CaptureStream)
                else CaptureStream(str(stream))
            )
        except ValueError as exc:
            raise CaptureContractError(
                "microstructure source-window operation is unknown"
            ) from exc
        if resolved_stream not in {
            CaptureStream.IQFEED_PRINT,
            CaptureStream.L2_DEPTH_CHECKPOINT,
        }:
            raise CaptureContractError(
                "microstructure source-window stream is unsupported"
            )

        with self._lock:
            key = (resolved_stream, normalized_provider, normalized_symbol)
            evicted = self._bounded_source_evictions.get(key)
            if resolved_stream is CaptureStream.IQFEED_PRINT:
                source_clock_name = "provider_event_at"

                def source_clock(event: CaptureEvent) -> datetime | None:
                    return event.clocks.provider_event_at

                evicted_clock = (
                    None if evicted is None else evicted.max_provider_event_at
                )
                evicted_clock_unknown = bool(
                    evicted is not None
                    and evicted.exact_provider_clock_unknown
                )
            else:
                source_clock_name = "market_reference_at"

                def source_clock(event: CaptureEvent) -> datetime | None:
                    return event.clocks.market_reference_at

                evicted_clock = (
                    None if evicted is None else evicted.max_market_reference_at
                )
                evicted_clock_unknown = bool(
                    evicted is not None
                    and evicted.market_reference_clock_unknown
                )

            evicted_frontier_sequence = 0
            if evicted is not None:
                if (
                    evicted.max_available_at > returned
                    or evicted_clock_unknown
                    or evicted_clock is None
                    or evicted_clock > start
                ):
                    raise MicrostructureCoverageUnavailable(
                        "microstructure_bounded_source_index_coverage_gap",
                        stream=resolved_stream,
                        symbol=normalized_symbol,
                        first_available_at=start,
                        last_available_at=returned,
                        lost_count=evicted.count,
                    )
                evicted_frontier_sequence = evicted.max_sequence

            visible_sources = tuple(
                event
                for event in self._recent_source_events.values()
                if (
                    event.stream is resolved_stream
                    and event.provider == normalized_provider
                    and event.symbol == normalized_symbol
                    and event.clocks.available_at <= returned
                )
            )
            source_clocks = tuple(source_clock(event) for event in visible_sources)
            if any(value is None for value in source_clocks):
                raise MicrostructureCoverageUnavailable(
                    "microstructure_exact_source_clock_missing",
                    stream=resolved_stream,
                    symbol=normalized_symbol,
                    first_available_at=start,
                    last_available_at=returned,
                )
            if any(
                value is not None and value > returned
                for value in source_clocks
            ):
                raise MicrostructureCoverageUnavailable(
                    "microstructure_source_clock_from_future",
                    stream=resolved_stream,
                    symbol=normalized_symbol,
                    first_available_at=start,
                    last_available_at=returned,
                )
            source_frontier_sequence = max(
                (
                    evicted_frontier_sequence,
                    *(event.sequence for event in visible_sources),
                )
            )
            expected_sources = tuple(
                sorted(
                    (
                        event
                        for event in visible_sources
                        if (
                            source_clock(event) is not None
                            and source_clock(event) > start
                            and source_clock(event) <= end
                        )
                    ),
                    key=lambda event: (
                        source_clock(event),
                        event.sequence,
                    ),
                )
            )
            query = CaptureMicrostructureReadQuery(
                operation=resolved_operation,
                stream=resolved_stream,
                symbol=normalized_symbol,
                provider=normalized_provider,
                event_start_exclusive=start,
                event_end_inclusive=end,
                decision_at=end,
                available_at_most=returned,
                source_frontier_sequence=source_frontier_sequence,
                source_clock_basis=source_clock_name,
                parameters=parameters,
            )
            source_refs = tuple(
                CaptureEventRef.from_event(event) for event in expected_sources
            )
            receipt = CaptureReadReceipt(
                read_id=str(read_id or uuid.uuid4()),
                decision_id=str(decision_id or "").strip(),
                identity_sha256=self.identity.identity_sha256,
                stream=resolved_stream,
                provider=normalized_provider,
                symbol=normalized_symbol,
                requested_at=requested,
                returned_at=returned,
                query_sha256=sha256_json(query.to_dict()),
                source_event_sha256s=tuple(
                    event.event_sha256 for event in expected_sources
                ),
                empty_result=not expected_sources,
                result_sha256=captured_read_result_sha256(source_refs),
                content_verified=True,
                replay_network_fallback_used=False,
                query=query.to_dict(),
            )
            receipt_event = self.submit_read_receipt(receipt)
            return receipt_event, receipt, expected_sources

    def submit_first_dip_tape_receipt(
        self, receipt: CaptureReadReceipt
    ) -> tuple[CaptureEvent, FirstDipTapeReceiptEvidence]:
        """Commit the complete typed IQFeed window, never a caller-picked subset."""

        if not isinstance(receipt, CaptureReadReceipt):
            raise CaptureContractError("first-dip tape receipt is malformed")
        if receipt.stream is not CaptureStream.IQFEED_PRINT:
            raise CaptureContractError("first-dip tape receipt must read IQFeed prints")

        def _unavailable(
            reason: str,
            *,
            start: datetime | None = None,
            end: datetime | None = None,
            lost_count: int = 1,
            coverage_gap_required: bool = False,
        ) -> None:
            raise FirstDipTapeCoverageUnavailable(
                reason,
                symbol=receipt.symbol,
                first_available_at=start or receipt.requested_at,
                last_available_at=end or receipt.returned_at,
                lost_count=lost_count,
                coverage_gap_required=coverage_gap_required,
            )

        if receipt.query is None:
            _unavailable("first_dip_tape_typed_query_missing")
        try:
            read_query = FirstDipTapeReadQuery.from_dict(receipt.query)
        except FirstDipTapePolicyError:
            _unavailable("first_dip_tape_typed_query_invalid")
        if (
            read_query.provider != receipt.provider
            or read_query.symbol != receipt.symbol
            or receipt.requested_at > read_query.decision_at
            or read_query.available_at_most != receipt.returned_at
        ):
            _unavailable(
                "first_dip_tape_query_receipt_boundary_mismatch",
                start=read_query.event_start_exclusive,
                end=read_query.event_end_inclusive,
            )

        with self._lock:
            key = (
                CaptureStream.IQFEED_PRINT,
                read_query.provider,
                read_query.symbol,
            )
            evicted = self._bounded_source_evictions.get(key)
            evicted_frontier_sequence = 0
            if evicted is not None:
                if (
                    evicted.max_available_at > read_query.available_at_most
                    or evicted.exact_provider_clock_unknown
                    or evicted.max_provider_event_at is None
                    or evicted.max_provider_event_at
                    > read_query.event_start_exclusive
                ):
                    _unavailable(
                        "first_dip_tape_bounded_index_coverage_gap",
                        start=read_query.event_start_exclusive,
                        end=read_query.event_end_inclusive,
                        lost_count=evicted.count,
                        coverage_gap_required=True,
                    )
                # Every evicted print is proven outside the left-open window and
                # causally available by the query boundary.  Its sequence still
                # participates in the exact provider/symbol source frontier.
                evicted_frontier_sequence = evicted.max_sequence

            visible_sources = tuple(
                event
                for event in self._recent_source_events.values()
                if (
                    event.stream is CaptureStream.IQFEED_PRINT
                    and event.provider == read_query.provider
                    and event.symbol == read_query.symbol
                    and event.clocks.available_at
                    <= read_query.available_at_most
                )
            )
            if any(
                source.clocks.provider_event_at is None
                for source in visible_sources
            ):
                _unavailable(
                    "first_dip_tape_exact_event_clock_missing",
                    start=read_query.event_start_exclusive,
                    end=read_query.event_end_inclusive,
                    coverage_gap_required=True,
                )
            if any(
                source.clocks.provider_event_at is not None
                and source.clocks.provider_event_at
                > read_query.available_at_most
                for source in visible_sources
            ):
                _unavailable(
                    "first_dip_tape_source_clock_from_future",
                    start=read_query.event_start_exclusive,
                    end=read_query.event_end_inclusive,
                    coverage_gap_required=True,
                )

            actual_frontier_sequence = max(
                (
                    evicted_frontier_sequence,
                    *(source.sequence for source in visible_sources),
                )
            )
            if (
                actual_frontier_sequence <= 0
                or read_query.source_frontier_sequence
                != actual_frontier_sequence
            ):
                _unavailable(
                    "first_dip_tape_source_frontier_mismatch",
                    start=read_query.event_start_exclusive,
                    end=read_query.event_end_inclusive,
                )

            expected_sources = tuple(
                sorted(
                    (
                        source
                        for source in visible_sources
                        if (
                            source.clocks.provider_event_at
                            > read_query.event_start_exclusive
                            and source.clocks.provider_event_at
                            <= read_query.event_end_inclusive
                        )
                    ),
                    key=lambda source: (
                        source.clocks.provider_event_at,
                        source.sequence,
                    ),
                )
            )
            expected_hashes = tuple(
                source.event_sha256 for source in expected_sources
            )
            if receipt.empty_result != (not expected_sources):
                _unavailable(
                    "first_dip_tape_complete_window_empty_result_mismatch",
                    start=read_query.event_start_exclusive,
                    end=read_query.event_end_inclusive,
                )
            if receipt.source_event_sha256s != expected_hashes:
                _unavailable(
                    "first_dip_tape_complete_window_inventory_mismatch",
                    start=read_query.event_start_exclusive,
                    end=read_query.event_end_inclusive,
                    lost_count=max(
                        1,
                        len(set(expected_hashes).symmetric_difference(
                            receipt.source_event_sha256s
                        )),
                    ),
                )
            try:
                tuple(
                    CaptureIqfeedPrint.from_event(source)
                    for source in expected_sources
                )
            except CaptureContractError:
                _unavailable(
                    "first_dip_tape_print_payload_semantics_unavailable",
                    start=read_query.event_start_exclusive,
                    end=read_query.event_end_inclusive,
                    coverage_gap_required=True,
                )

            # Keep the same re-entrant lifecycle lock through validation and
            # receipt commit so a newly durable print cannot race the inventory.
            event = self.submit_read_receipt(receipt)
            evidence = FirstDipTapeReceiptEvidence(
                self._read_evidence_by_id[receipt.read_id]
            )
            self._first_dip_tape_by_id[receipt.read_id] = evidence
            return event, evidence

    def attest_active_prefix(
        self,
        *,
        decision_id: str,
        required_read_ids: Iterable[str],
        first_dip_tape_read_id: str | None = None,
    ) -> ActiveCapturePrefixAttestation:
        """Privately attest one exact durable decision/read snapshot.

        The lock is held only while the current frontier is sampled and the
        HMAC-backed object is issued.  Later unrelated feed events do not
        invalidate the immutable snapshot, and this lock must never be held
        across broker HTTP.
        """

        with self._lock:
            attested_at = self._trusted_recorded_at(None)
            normalized_decision_id = str(decision_id or "").strip()
            decision_event = self._decision_events.get(normalized_decision_id)
            decision_output = self._decision_outputs.get(normalized_decision_id)
            predecision = self._predecision_attestations.get(
                normalized_decision_id
            )
            if decision_event is None:
                raise CaptureContractError(
                    "active prefix attestation requires a durable FSM decision"
                )
            if decision_output is None:
                raise CaptureContractError(
                    "active prefix attestation lacks its canonical decision output"
                )
            if (
                predecision is None
                or normalized_decision_id
                not in self._consumed_predecision_attestations
                or normalized_decision_id in self._final_decision_attestations
                or attested_at >= predecision.expires_at
            ):
                raise CaptureContractError(
                    "active prefix attestation lacks a fresh consumed predecision proof"
                )
            if (
                self._run_open is None
                or self._run_close_event is not None
                or set(self.producers) != self._registered
                or self._quiescent
                or self._closed
                or self._submission_failure is not None
            ):
                raise CaptureContractError(
                    "active prefix attestation requires a healthy RUNNING lifecycle"
                )
            for producer_id in sorted(self.producers):
                self._check_heartbeat_deadline(producer_id, attested_at)
            read_ids = tuple(sorted(str(value).strip() for value in required_read_ids))
            if (
                not read_ids
                or any(not value for value in read_ids)
                or len(read_ids) != len(set(read_ids))
            ):
                raise CaptureContractError(
                    "active prefix attestation read ids are malformed"
                )
            decision_read_ids = tuple(
                sorted(
                    str(value).strip()
                    for value in decision_event.payload.get("required_read_ids", ())
                )
            )
            if read_ids != decision_read_ids:
                raise CaptureContractError(
                    "active prefix attestation read set differs from the decision"
                )
            if read_ids != predecision.required_read_ids:
                raise CaptureContractError(
                    "active prefix attestation read set differs from predecision proof"
                )
            for stream in predecision.dependency_profile.required_streams:
                dependency = predecision.dependency_profile.dependency_for(stream)
                for _gap_producer_id, gap in self._reported_gaps:
                    if gap.stream not in {stream, CaptureStream.COVERAGE_GAP}:
                        continue
                    stream_symbols = {
                        row.receipt.symbol
                        for row in predecision.read_evidence
                        if row.receipt.stream is stream
                    }
                    if (
                        gap.stream is not CaptureStream.COVERAGE_GAP
                        and gap.symbol is not None
                        and gap.symbol not in stream_symbols
                    ):
                        continue
                    if gap.intersects(dependency.coverage_start_at, attested_at):
                        raise CaptureContractError(
                            "active prefix dependency acquired an unresolved coverage gap"
                        )
            try:
                prefix_sequence = int(
                    decision_event.payload.get("input_prefix_sequence") or 0
                )
            except (TypeError, ValueError) as exc:
                raise CaptureContractError(
                    "active prefix decision sequence is malformed"
                ) from exc
            prefix_root = str(
                decision_event.payload.get("input_prefix_root_sha256") or ""
            ).strip().lower()
            if (
                decision_event.sequence <= prefix_sequence
                or prefix_sequence != predecision.input_prefix_sequence
                or prefix_root != predecision.input_prefix_root_sha256
                or decision_event.payload.get("predecision_attestation_sha256")
                != predecision.attestation_sha256
                or decision_event.payload.get(
                    "predecision_read_evidence_inventory_sha256"
                )
                != predecision.read_evidence_inventory_sha256
                or decision_event.payload.get(
                    "predecision_continuity_evidence_inventory_sha256"
                )
                != predecision.continuity_evidence_inventory_sha256
                or decision_event.payload.get(
                    "predecision_admission_handoff_sha256"
                )
                != predecision.admission_handoff_sha256
            ):
                raise CaptureContractError(
                    "active prefix decision/predecision chain is inconsistent"
                )
            evidence = list(predecision.read_evidence)
            first_dip_id = str(first_dip_tape_read_id or "").strip() or None
            if first_dip_id != predecision.first_dip_tape_read_id:
                raise CaptureContractError(
                    "active prefix first-dip receipt differs from predecision proof"
                )
            proof = _issue_active_capture_prefix_attestation(
                run_id=self.identity.run_id,
                generation=self.identity.generation,
                decision_id=normalized_decision_id,
                decision_event_sha256=decision_event.event_sha256,
                predecision_attestation_sha256=(
                    predecision.attestation_sha256
                ),
                predecision_read_evidence_inventory_sha256=(
                    predecision.read_evidence_inventory_sha256
                ),
                predecision_continuity_evidence_inventory_sha256=(
                    predecision.continuity_evidence_inventory_sha256
                ),
                decision_output_sha256=decision_output.decision_output_sha256,
                order_intent_sha256s=tuple(
                    intent.order_intent_sha256
                    for intent in decision_output.order_intents
                ),
                input_prefix_sequence=prefix_sequence,
                input_prefix_root_sha256=prefix_root,
                attestation_frontier_sequence=self._sequence,
                attestation_frontier_root_sha256=self._current_prefix_root(),
                decision_available_at=decision_event.clocks.available_at,
                attested_available_at=attested_at,
                expires_at=predecision.expires_at,
                identity_sha256=self.identity.identity_sha256,
                account_identity_sha256=self.identity.account_identity_sha256,
                code_build_sha256=self.identity.code_build_sha256,
                config_sha256=self.identity.config_sha256,
                feature_flags_sha256=self.identity.feature_flags_sha256,
                resource_binding_sha256=self.resource_binding.binding_sha256,
                producer_generations={
                    producer_id: producer.generation
                    for producer_id, producer in self.producers.items()
                },
                required_read_ids=read_ids,
                read_evidence=tuple(evidence),
                continuity_evidence=predecision.continuity_evidence,
                first_dip_tape_read_id=first_dip_id,
            )
            self._final_decision_attestations[normalized_decision_id] = proof
            return proof

    def submit_capture_health(self, payload: Mapping[str, Any]) -> CaptureEvent:
        """Commit periodic runtime health using a strict, non-spoofable envelope."""

        with self._lock:
            recorded = self._trusted_recorded_at(None)
            if self._run_open is None or self._run_close_event is not None:
                raise CaptureContractError("capture run is not open")
            if not isinstance(payload, Mapping) or set(payload) != _CAPTURE_RUNTIME_HEALTH_KEYS:
                raise CaptureContractError("capture health fields do not match allowlist")
            if payload.get("identity_sha256") != self.identity.identity_sha256:
                raise CaptureContractError("capture health identity mismatch")
            if payload.get("resource_hashes") != self.resource_binding.hashes:
                raise CaptureContractError("capture health resource hashes mismatch")
            if payload.get("resource_binding") != self.resource_binding.to_record():
                raise CaptureContractError("capture health resource binding mismatch")
            if payload.get("network_fallback_allowed") is not False:
                raise CaptureContractError("capture health cannot permit network fallback")
            if payload.get("durable_sequence_next") != self._sequence + 1:
                raise CaptureContractError("capture health durable frontier mismatch")
            for key in (
                "accepted_count",
                "rejected_or_reported_lost_count",
                "change_key_count",
                "max_change_keys",
                "max_read_sources",
            ):
                value = payload.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise CaptureContractError(f"capture health {key} is malformed")
            event = CaptureEvent(
                identity=self.identity,
                sequence=self._proposed_sequence(recorded),
                stream=CaptureStream.CAPTURE_HEALTH,
                provider=_CAPTURE_RUNTIME_HEALTH_PROVIDER,
                symbol=None,
                clocks=CaptureClocks(received_at=recorded, available_at=recorded),
                payload=payload,
            )
            return self._submit(event)

    def heartbeat(
        self, producer_id: str, *, recorded_at: datetime | None = None
    ) -> CaptureEvent:
        with self._lock:
            recorded = self._trusted_recorded_at(recorded_at)
            producer = self._require_open_producer(producer_id)
            self._check_heartbeat_deadline(producer.producer_id, recorded)
            return self._producer_fact(
                producer,
                kind=CaptureProducerLifecycleKind.HEARTBEAT,
                recorded_at=recorded,
                frontier_sequence=self._last_input_sequence[producer.producer_id],
            )

    def submit_live_continuity_checkpoint(
        self,
        producer_id: str,
        coverage: StreamCoverage,
        *,
        recorded_at: datetime | None = None,
    ) -> ActiveCaptureContinuityEvidence:
        """Durably checkpoint live continuity before a risk decision.

        This is cumulative, may advance repeatedly, and is separate from the
        terminal stream-coverage record used by post-run grading.
        """

        with self._lock:
            recorded = self._trusted_recorded_at(recorded_at)
            producer = self._require_open_producer(producer_id)
            self._check_heartbeat_deadline(producer.producer_id, recorded)
            if not isinstance(coverage, StreamCoverage):
                raise CaptureContractError("live continuity coverage is malformed")
            policy = STREAM_POLICIES[coverage.stream]
            if (
                coverage.stream not in producer.streams
                or policy.coverage_mode
                not in {CoverageMode.CONTINUOUS, CoverageMode.CHANGE_LOG}
            ):
                raise CaptureContractError(
                    "live continuity stream is not continuity-backed or owned by producer"
                )
            if (
                coverage.identity_sha256 != self.identity.identity_sha256
                or coverage.watermark is None
                or not coverage.content_verified
                or not coverage.continuity_complete
                or (
                    policy.exact_provider_event_clock_required
                    and not coverage.exact_event_clock_complete
                )
            ):
                raise CaptureContractError(
                    "live continuity coverage is incomplete or unverified"
                )
            stats = self._stream_stats.get(coverage.stream)
            if stats is None:
                raise CaptureContractError(
                    "live continuity coverage has no durable source events"
                )
            if (
                coverage.event_count != stats["event_count"]
                or coverage.first_available_at != stats["first_available_at"]
                or coverage.last_available_at != stats["last_available_at"]
                or stats["providers"] != {coverage.provider}
                or stats["symbols"] != {coverage.symbol}
                or coverage.exact_event_clock_complete
                != stats["exact_event_clock_complete"]
                or coverage.last_available_at > recorded
            ):
                raise CaptureContractError(
                    "live continuity coverage differs from durable stream frontier"
                )
            watermark = coverage.watermark
            if (
                watermark.stream is not coverage.stream
                or watermark.provider != coverage.provider
                or watermark.symbol != coverage.symbol
                or watermark.identity_sha256 != self.identity.identity_sha256
                or watermark.generation != producer.generation
                or watermark.generation != self.identity.generation
                or watermark.emitted_available_at > recorded
                or (
                    stats["source_clock_max"] is not None
                    and watermark.event_watermark_at
                    < stats["source_clock_max"]
                )
                or watermark.max_observed_lateness_seconds
                < float(stats["max_observed_lateness_seconds"])
            ):
                raise CaptureContractError(
                    "live continuity watermark does not cover the durable source frontier"
                )
            key = (producer.producer_id, coverage.stream)
            prior = self._live_continuity.get(key)
            if prior is not None and (
                coverage.event_count < prior.coverage.event_count
                or coverage.last_available_at < prior.coverage.last_available_at
                or watermark.event_watermark_at
                < prior.coverage.watermark.event_watermark_at
            ):
                raise CaptureContractError(
                    "live continuity checkpoint moved backwards"
                )
            watermark_event = self._submit(
                CaptureEvent(
                    identity=self.identity,
                    sequence=self._proposed_sequence(recorded),
                    stream=CaptureStream.PROVIDER_WATERMARK,
                    provider=watermark.provider,
                    symbol=watermark.symbol,
                    clocks=CaptureClocks(
                        received_at=recorded,
                        available_at=recorded,
                        market_reference_at=watermark.event_watermark_at,
                    ),
                    payload=watermark.to_dict(),
                )
            )
            coverage_event = self._submit(
                CaptureEvent(
                    identity=self.identity,
                    sequence=self._proposed_sequence(recorded),
                    stream=CaptureStream.CAPTURE_HEALTH,
                    provider=coverage.provider,
                    symbol=coverage.symbol,
                    clocks=CaptureClocks(
                        received_at=recorded,
                        available_at=recorded,
                    ),
                    payload={
                        "live_continuity_checkpoint": True,
                        "coverage": coverage.to_dict(),
                    },
                )
            )
            evidence = ActiveCaptureContinuityEvidence(
                coverage=coverage,
                producer_id=producer.producer_id,
                producer_generation=producer.generation,
                source_frontier_sequence=int(stats["source_sequence_max"]),
                watermark_event_sha256=watermark_event.event_sha256,
                watermark_event_sequence=watermark_event.sequence,
                watermark_committed_available_at=watermark_event.clocks.available_at,
                coverage_event_sha256=coverage_event.event_sha256,
                coverage_event_sequence=coverage_event.sequence,
                coverage_committed_available_at=coverage_event.clocks.available_at,
            )
            self._live_continuity[key] = evidence
            return evidence

    def submit_stream_coverage(
        self,
        producer_id: str,
        coverage: StreamCoverage,
        *,
        recorded_at: datetime | None = None,
    ) -> tuple[CaptureEvent, ...]:
        with self._lock:
            recorded = self._trusted_recorded_at(recorded_at)
            producer = self._require_open_producer(producer_id)
            self._check_heartbeat_deadline(producer.producer_id, recorded)
            if not isinstance(coverage, StreamCoverage):
                raise CaptureContractError("producer stream coverage is malformed")
            if coverage.stream not in producer.streams:
                raise CaptureContractError("coverage stream is owned by another producer")
            if coverage.identity_sha256 != self.identity.identity_sha256:
                raise CaptureContractError("coverage identity does not match capture run")
            policy = STREAM_POLICIES[coverage.stream]
            # Persist honest incomplete coverage.  The lifecycle/ReplayV3
            # graders fail closed; rejecting this evidence here would hide the
            # precise reason the window is unscorable.
            if coverage.last_available_at > recorded:
                raise CaptureContractError("coverage health cannot be backdated")
            stats = self._stream_stats.get(coverage.stream)
            if stats is None:
                raise CaptureContractError("producer coverage has no durable source events")
            if (
                coverage.event_count != stats["event_count"]
                or coverage.first_available_at != stats["first_available_at"]
                or coverage.last_available_at != stats["last_available_at"]
                or stats["providers"] != {coverage.provider}
                or stats["symbols"] != {coverage.symbol}
                or coverage.exact_event_clock_complete
                != stats["exact_event_clock_complete"]
            ):
                raise CaptureContractError(
                    "producer coverage does not match the durable stream frontier"
                )
            expected_receipts = self._receipt_count_by_stream.get(coverage.stream, 0)
            if coverage.query_receipt_count != expected_receipts:
                raise CaptureContractError(
                    "producer coverage query receipt count does not match durable receipts"
                )
            if (
                policy.coverage_mode is CoverageMode.QUERY_RECEIPT
                and expected_receipts <= 0
            ):
                raise CaptureContractError(
                    "query-backed producer coverage requires a durable read receipt"
                )
            key = (producer.producer_id, coverage.stream)
            if key in self._coverage:
                raise CaptureContractError("producer coverage was already finalized")
            evidence: list[CaptureEvent] = []
            watermark = coverage.watermark
            if watermark is not None:
                if watermark.generation != self.identity.generation:
                    raise CaptureContractError(
                        "provider watermark generation does not match capture run"
                    )
                if watermark.emitted_available_at > recorded:
                    raise CaptureContractError("provider watermark cannot be backdated")
                watermark_event = CaptureEvent(
                    identity=self.identity,
                    sequence=self._proposed_sequence(recorded),
                    stream=CaptureStream.PROVIDER_WATERMARK,
                    provider=watermark.provider,
                    symbol=watermark.symbol,
                    clocks=CaptureClocks(
                        received_at=recorded,
                        available_at=recorded,
                        market_reference_at=watermark.event_watermark_at,
                    ),
                    payload=watermark.to_dict(),
                )
                evidence.append(self._submit(watermark_event))
            health_event = CaptureEvent(
                identity=self.identity,
                sequence=self._proposed_sequence(recorded),
                stream=CaptureStream.CAPTURE_HEALTH,
                provider=coverage.provider,
                symbol=coverage.symbol,
                clocks=CaptureClocks(received_at=recorded, available_at=recorded),
                payload=coverage.to_dict(),
            )
            evidence.append(self._submit(health_event))
            self._coverage[key] = coverage
            self._evidence_events[key] = tuple(evidence)
            return tuple(evidence)

    def report_gap(
        self,
        producer_id: str,
        *,
        stream: CaptureStream,
        reason: str,
        first_available_at: datetime,
        last_available_at: datetime,
        lost_count: int,
        recorded_at: datetime | None = None,
        symbol: str | None = None,
    ) -> CaptureEvent:
        with self._lock:
            recorded = self._trusted_recorded_at(recorded_at)
            producer = self._require_open_producer(producer_id)
            normalized_stream = (
                stream if isinstance(stream, CaptureStream) else CaptureStream(str(stream))
            )
            if (
                normalized_stream not in producer.streams
                and normalized_stream is not CaptureStream.COVERAGE_GAP
            ):
                raise CaptureContractError("producer gap stream is not owned")
            self._check_heartbeat_deadline(producer.producer_id, recorded)
            event = self._producer_fact(
                producer,
                kind=CaptureProducerLifecycleKind.GAP,
                recorded_at=recorded,
                frontier_sequence=self._last_input_sequence[producer.producer_id],
                gap_reason=reason,
            )
            gap = CoverageGap(
                stream=normalized_stream,
                reason=reason,
                first_available_at=first_available_at,
                last_available_at=last_available_at,
                lost_count=lost_count,
                symbol=symbol,
            )
            if not self.ingress.submit_gap(self.identity, gap):
                self._latch_failure("producer_gap_rejected")
                raise CaptureContractError("capture producer gap could not be persisted")
            self._producer_gaps.append(f"{producer.producer_id}:{reason}")
            self._reported_gaps.append((producer.producer_id, gap))
            return event

    def quiesce(
        self, producer_id: str, *, recorded_at: datetime | None = None
    ) -> CaptureEvent:
        with self._lock:
            recorded = self._trusted_recorded_at(recorded_at)
            producer = self._require_open_producer(producer_id)
            self._check_heartbeat_deadline(producer.producer_id, recorded)
            if CaptureStream.BROKER_ORDER_LIFECYCLE in producer.streams:
                incomplete_client_order_ids = sorted(
                    intent.client_order_id
                    for intent_sha256, (_, intent) in self._order_intents_by_sha.items()
                    if (
                        intent_sha256 not in self._broker_state_by_intent
                        or not self._broker_state_by_intent[intent_sha256][1].terminal
                    )
                )
                if incomplete_client_order_ids:
                    raise CaptureContractError(
                        "broker producer cannot quiesce with incomplete order lifecycles: "
                        + ",".join(incomplete_client_order_ids)
                    )
            evidence_events = tuple(
                event
                for stream in producer.streams
                for event in self._evidence_events.get(
                    (producer.producer_id, stream), ()
                )
            )
            frontier = max(
                (
                    self._last_input_sequence[producer.producer_id],
                    *(event.sequence for event in evidence_events),
                )
            )
            event = self._producer_fact(
                producer,
                kind=CaptureProducerLifecycleKind.QUIESCENT,
                recorded_at=recorded,
                frontier_sequence=frontier,
                evidence_event_sha256s=(
                    event.event_sha256 for event in evidence_events
                ),
            )
            self._quiescent.add(producer.producer_id)
            self._last_input_sequence[producer.producer_id] = frontier
            return event

    def close_producer(
        self, producer_id: str, *, recorded_at: datetime | None = None
    ) -> CaptureEvent:
        with self._lock:
            recorded = self._trusted_recorded_at(recorded_at)
            producer = self._producer(producer_id)
            if producer.producer_id not in self._quiescent:
                raise CaptureContractError("producer must be quiescent before close")
            if producer.producer_id in self._closed:
                raise CaptureContractError("capture producer closed more than once")
            self._check_heartbeat_deadline(producer.producer_id, recorded)
            event = self._producer_fact(
                producer,
                kind=CaptureProducerLifecycleKind.CLOSED,
                recorded_at=recorded,
                frontier_sequence=self._last_input_sequence[producer.producer_id],
            )
            self._closed.add(producer.producer_id)
            self._close_events[producer.producer_id] = event
            return event

    def close_run(self, *, recorded_at: datetime | None = None) -> CaptureEvent:
        with self._lock:
            recorded = self._trusted_recorded_at(recorded_at)
            if self._run_open is None or self._run_open_event is None:
                raise CaptureContractError("capture run was never opened")
            if self._run_close_event is not None:
                raise CaptureContractError("capture run closed more than once")
            missing = sorted(set(self.producers) - self._closed)
            if missing:
                raise CaptureContractError(
                    "capture run has open producers: " + ",".join(missing)
                )
            close_hashes = {
                producer_id: event.event_sha256
                for producer_id, event in self._close_events.items()
            }
            fact = CaptureProducerLifecycleFact(
                kind=CaptureProducerLifecycleKind.RUN_CLOSED,
                identity_sha256=self.identity.identity_sha256,
                producer_roster_sha256=self._run_open.producer_roster_sha256,
                recorded_at=recorded,
                frontier_sequence=max(
                    event.sequence for event in self._close_events.values()
                ),
                prior_lifecycle_event_sha256=None,
                producer_close_event_sha256s=close_hashes,
            )
            event = self._lifecycle_event(fact.to_dict(), recorded_at=recorded)
            self._run_close_event = event
            return event

    def seal_run(self, writer: Any) -> "CaptureRunSeal":
        """Cryptographically close exact inventory; grading remains separate."""

        with self._lock:
            if self._run_close_event is None:
                raise CaptureContractError("capture run lacks a sealed RUN_CLOSED fact")
            if self._submission_failure is not None:
                raise CaptureContractError(
                    "capture lifecycle lost an unrepresented event and cannot seal"
                )
            if type(writer) not in (CaptureWriterWorker, CaptureWriterPool):
                raise CaptureContractError(
                    "certifying seal requires an exact stopped capture writer"
                )
            if writer.ingress is not self.ingress:
                raise CaptureContractError("capture writer uses another ingress")
            writer_health = writer.health()
            if (
                not writer_health.get("stopped_cleanly")
                or writer_health.get("writer_alive")
                or writer_health.get("last_error")
                or writer_health.get("last_errors")
                or not self.ingress.drained
            ):
                raise CaptureContractError(
                    "capture writer must be stopped, drained, and error-free before seal"
                )
            ingress_health = self.ingress.health()
            if (
                ingress_health["submitted"] != self._sequence
                or ingress_health["accepted"] != self._sequence
                or ingress_health["dropped"]
                or ingress_health["sequence_min"] != 1
                or ingress_health["sequence_max"] != self._sequence
            ):
                raise CaptureContractError(
                    "capture ingress escaped the exact producer lifecycle boundary"
                )
            return writer.seal_run(self.identity)

    def health(self) -> dict[str, Any]:
        with self._lock:
            coverage_certification_ready = all(
                (
                    coverage := self._coverage.get((producer_id, stream))
                )
                is not None
                and coverage.content_verified
                and coverage.continuity_complete
                and (
                    not STREAM_POLICIES[stream].exact_provider_event_clock_required
                    or coverage.exact_event_clock_complete
                )
                and (
                    STREAM_POLICIES[stream].coverage_mode
                    is not CoverageMode.CONTINUOUS
                    or coverage.watermark is not None
                )
                for producer_id, producer in self.producers.items()
                for stream in producer.streams
            )
            return {
                "identity_sha256": self.identity.identity_sha256,
                "producer_roster_sha256": (
                    self._run_open.producer_roster_sha256
                    if self._run_open is not None
                    else None
                ),
                "opened": self._run_open is not None,
                "registered_producers": sorted(self._registered),
                "pending_provider_registrations": sorted(
                    self._pending_provider_registrations
                ),
                "quiescent_producers": sorted(self._quiescent),
                "closed_producers": sorted(self._closed),
                "run_closed": self._run_close_event is not None,
                "last_sequence": self._sequence,
                "last_available_at": (
                    _iso(self._last_available_at)
                    if self._last_available_at is not None
                    else None
                ),
                "submission_failure": self._submission_failure,
                "producer_gaps": list(self._producer_gaps),
                "read_receipt_count": len(self._receipt_by_id),
                "decision_count": len(self._decision_ids),
                "order_intent_count": len(self._order_intents_by_sha),
                "broker_lifecycle_count": len(self._broker_state_by_intent),
                "terminal_broker_lifecycle_count": sum(
                    1
                    for _, lifecycle in self._broker_state_by_intent.values()
                    if lifecycle.terminal
                ),
                "coverage_certification_ready": coverage_certification_ready,
                "certifying_seal_eligible": bool(
                    self._run_close_event is not None
                    and self._submission_failure is None
                    and not self._pending_provider_registrations
                    and not self._producer_gaps
                    and not self._reported_gaps
                    and coverage_certification_ready
                    and all(
                        intent_sha256 in self._broker_state_by_intent
                        and self._broker_state_by_intent[intent_sha256][1].terminal
                        for intent_sha256 in self._order_intents_by_sha
                    )
                ),
                "cryptographic_seal_eligible": bool(
                    self._run_close_event is not None
                    and self._submission_failure is None
                    and all(
                        intent_sha256 in self._broker_state_by_intent
                        and self._broker_state_by_intent[intent_sha256][1].terminal
                        for intent_sha256 in self._order_intents_by_sha
                    )
                ),
            }


@dataclass(frozen=True)
class BlobRef:
    sha256: str
    raw_bytes: int
    compressed_bytes: int
    relative_path: str


@dataclass(frozen=True)
class ChunkRef:
    sha256: str
    row_count: int
    raw_bytes: int
    compressed_bytes: int
    relative_path: str


CAPTURE_STORAGE_POLICY_SCHEMA_VERSION = "chili-replay-capture-storage-policy-v1"
PAYLOAD_PACK_SCHEMA_VERSION = "chili-replay-payload-pack-v1"


@dataclass(frozen=True)
class CaptureStoragePolicy:
    """Exact physical-storage policy used by one capture store.

    Payloads which are safe to content-deduplicate remain addressed by their
    own canonical payload SHA.  The bytes are physically grouped into bounded,
    immutable, content-addressed packs so clean close has a bounded number of
    objects to flush.  A single payload larger than ``pack_target_raw_bytes``
    is stored alone; the target is never used to truncate or downgrade it.
    """

    compression_codec: str
    compression_level: int
    payload_layout: str = "content_addressed_pack_v1"
    pack_max_records: int = 2_048
    pack_target_raw_bytes: int = 8 * 1024 * 1024
    pack_read_cache_entries: int = 4
    clean_close_sync: str = "each_immutable_object_once"
    schema_version: str = CAPTURE_STORAGE_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        codec = str(self.compression_codec or "").strip().lower()
        if codec not in {"zstd", "zlib"}:
            raise CaptureContractError("storage policy compression is unsupported")
        level = int(self.compression_level)
        if codec == "zlib" and not 0 <= level <= 9:
            raise CaptureContractError("storage policy zlib level must be 0..9")
        if codec == "zstd" and not 1 <= level <= 22:
            raise CaptureContractError("storage policy zstd level must be 1..22")
        if int(self.pack_max_records) <= 0:
            raise CaptureContractError("payload pack record bound must be positive")
        if int(self.pack_target_raw_bytes) <= 0:
            raise CaptureContractError("payload pack byte target must be positive")
        if int(self.pack_read_cache_entries) <= 0:
            raise CaptureContractError("payload pack read-cache bound must be positive")
        if self.payload_layout != "content_addressed_pack_v1":
            raise CaptureContractError("payload storage layout is unsupported")
        if self.clean_close_sync != "each_immutable_object_once":
            raise CaptureContractError("capture sync policy is unsupported")
        if self.schema_version != CAPTURE_STORAGE_POLICY_SCHEMA_VERSION:
            raise CaptureContractError("capture storage-policy schema is unsupported")
        object.__setattr__(self, "compression_codec", codec)
        object.__setattr__(self, "compression_level", level)
        object.__setattr__(self, "pack_max_records", int(self.pack_max_records))
        object.__setattr__(
            self, "pack_target_raw_bytes", int(self.pack_target_raw_bytes)
        )
        object.__setattr__(
            self, "pack_read_cache_entries", int(self.pack_read_cache_entries)
        )

    def to_record(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def policy_sha256(self) -> str:
        return sha256_json(self.to_record())


CAPTURE_RUN_SEAL_SCHEMA_VERSION = "chili-replay-capture-run-seal-v4"
CAPTURE_CLOSE_PROOF_SCHEMA_VERSION = "chili-replay-capture-close-proof-v1"
_SEALED_OBJECT_KINDS = frozenset(
    {"event_chunk", "gap_chunk", "payload_blob", "payload_pack"}
)


def _validated_sha256(value: Any, field_name: str) -> str:
    digest = str(value or "").strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise CaptureContractError(f"{field_name} must be a full SHA256")
    return digest


def _validated_relative_path(value: Any, field_name: str) -> str:
    raw = str(value or "").strip()
    candidate = Path(raw)
    if (
        not raw
        or candidate.is_absolute()
        or ".." in candidate.parts
        or "\\" in raw
        or candidate.as_posix() != raw
    ):
        raise CaptureContractError(f"{field_name} must be a normalized relative path")
    return raw


def _validated_integer(value: Any, field_name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise CaptureContractError(f"{field_name} must be a {qualifier} integer")
    return value


@dataclass(frozen=True)
class CaptureLifecycleCloseProof:
    """Runtime-derived proof that one exact ingress/writer lifecycle drained.

    This is created only by a stopped :class:`CaptureWriterWorker` or
    :class:`CaptureWriterPool`.  Its counters are reconciled again against the
    immutable exact-run object inventory while sealing and while loading.
    """

    identity_sha256: str
    writer_count: int
    writers_started: int
    writers_stopped_cleanly: int
    writer_errors: tuple[str, ...]
    ingress_submitted: int
    ingress_accepted: int
    ingress_dropped: int
    reported_gap_lost: int
    accepted_event_accumulator_sha256: str
    gap_records_emitted: int
    gap_lost_emitted: int
    emitted_gap_accumulator_sha256: str
    ingress_closed: bool
    ingress_finalized: bool
    post_close_submissions: int
    queued_events: int
    queued_bytes: int
    pending_gap_keys: int
    submission_sequence_min: int
    submission_sequence_max: int
    events_written: int
    written_event_accumulator_sha256: str
    gap_records_written: int
    lost_events_recorded: int
    written_gap_accumulator_sha256: str
    event_chunks_written: int
    gap_chunks_written: int
    schema_version: str = CAPTURE_CLOSE_PROOF_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURE_CLOSE_PROOF_SCHEMA_VERSION:
            raise CaptureContractError("unsupported capture close-proof schema")
        object.__setattr__(
            self,
            "identity_sha256",
            _validated_sha256(self.identity_sha256, "close proof identity_sha256"),
        )
        for name in (
            "accepted_event_accumulator_sha256",
            "emitted_gap_accumulator_sha256",
            "written_event_accumulator_sha256",
            "written_gap_accumulator_sha256",
        ):
            object.__setattr__(
                self,
                name,
                _validated_sha256(getattr(self, name), f"close proof {name}"),
            )
        for name in ("writer_count", "writers_started", "writers_stopped_cleanly"):
            object.__setattr__(
                self,
                name,
                _validated_integer(
                    getattr(self, name), f"close proof {name}", minimum=1
                ),
            )
        errors = tuple(self.writer_errors)
        if any(not isinstance(row, str) or not row.strip() for row in errors):
            raise CaptureContractError("close proof writer errors are malformed")
        object.__setattr__(self, "writer_errors", errors)
        for name in (
            "ingress_submitted",
            "ingress_accepted",
            "ingress_dropped",
            "reported_gap_lost",
            "gap_records_emitted",
            "gap_lost_emitted",
            "post_close_submissions",
            "queued_events",
            "queued_bytes",
            "pending_gap_keys",
            "events_written",
            "gap_records_written",
            "lost_events_recorded",
            "event_chunks_written",
            "gap_chunks_written",
        ):
            object.__setattr__(
                self,
                name,
                _validated_integer(
                    getattr(self, name), f"close proof {name}", minimum=0
                ),
            )
        for name in ("submission_sequence_min", "submission_sequence_max"):
            object.__setattr__(
                self,
                name,
                _validated_integer(
                    getattr(self, name), f"close proof {name}", minimum=1
                ),
            )
        if not isinstance(self.ingress_closed, bool) or not isinstance(
            self.ingress_finalized, bool
        ):
            raise CaptureContractError("close proof lifecycle flags must be booleans")
        if self.writer_count != self.writers_started:
            raise CaptureContractError("not every capture writer started")
        if self.writer_count != self.writers_stopped_cleanly or errors:
            raise CaptureContractError("not every capture writer stopped cleanly")
        if not self.ingress_closed or not self.ingress_finalized:
            raise CaptureContractError("capture ingress was not closed and finalized")
        if (
            self.post_close_submissions
            or self.queued_events
            or self.queued_bytes
            or self.pending_gap_keys
        ):
            raise CaptureContractError("capture close proof contains undrained or dirty state")
        if self.ingress_submitted <= 0:
            raise CaptureContractError("capture close proof cannot certify an empty ingress")
        if self.ingress_submitted != self.ingress_accepted + self.ingress_dropped:
            raise CaptureContractError("capture close proof submission counts do not reconcile")
        if self.events_written != self.ingress_accepted:
            raise CaptureContractError("accepted capture events were not all written")
        if (
            self.accepted_event_accumulator_sha256
            != self.written_event_accumulator_sha256
        ):
            raise CaptureContractError("written capture event identities do not reconcile")
        total_lost = self.ingress_dropped + self.reported_gap_lost
        if self.lost_events_recorded != total_lost:
            raise CaptureContractError("dropped capture events were not all recorded")
        if self.gap_lost_emitted != total_lost:
            raise CaptureContractError("dropped capture events were not all emitted as gaps")
        if self.gap_records_emitted != self.gap_records_written:
            raise CaptureContractError("emitted capture gaps were not all written")
        if self.emitted_gap_accumulator_sha256 != self.written_gap_accumulator_sha256:
            raise CaptureContractError("written capture gap identities do not reconcile")
        if self.submission_sequence_max < self.submission_sequence_min:
            raise CaptureContractError("capture close proof sequence bounds are reversed")
        if self.events_written and self.event_chunks_written <= 0:
            raise CaptureContractError("written events have no capture chunks")
        if not self.events_written and self.event_chunks_written:
            raise CaptureContractError("capture event chunks exist without written events")
        if not self.events_written and (
            self.accepted_event_accumulator_sha256 != _EMPTY_ACCUMULATOR_SHA256
            or self.written_event_accumulator_sha256 != _EMPTY_ACCUMULATOR_SHA256
        ):
            raise CaptureContractError("empty capture event set has a non-empty identity")
        if total_lost:
            if not (1 <= self.gap_records_written <= total_lost):
                raise CaptureContractError(
                    "dropped events lack reconciled coverage-gap records"
                )
            if self.gap_chunks_written <= 0:
                raise CaptureContractError("coverage-gap records have no capture chunks")
        elif (
            self.gap_records_written
            or self.lost_events_recorded
            or self.gap_chunks_written
            or self.emitted_gap_accumulator_sha256 != _EMPTY_ACCUMULATOR_SHA256
            or self.written_gap_accumulator_sha256 != _EMPTY_ACCUMULATOR_SHA256
        ):
            raise CaptureContractError("coverage-gap counters exist without dropped events")

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "writer_errors": list(self.writer_errors),
        }

    @property
    def proof_sha256(self) -> str:
        return sha256_json(self.to_dict())

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CaptureLifecycleCloseProof":
        expected = {
            "identity_sha256",
            "writer_count",
            "writers_started",
            "writers_stopped_cleanly",
            "writer_errors",
            "ingress_submitted",
            "ingress_accepted",
            "ingress_dropped",
            "reported_gap_lost",
            "accepted_event_accumulator_sha256",
            "gap_records_emitted",
            "gap_lost_emitted",
            "emitted_gap_accumulator_sha256",
            "ingress_closed",
            "ingress_finalized",
            "post_close_submissions",
            "queued_events",
            "queued_bytes",
            "pending_gap_keys",
            "submission_sequence_min",
            "submission_sequence_max",
            "events_written",
            "written_event_accumulator_sha256",
            "gap_records_written",
            "lost_events_recorded",
            "written_gap_accumulator_sha256",
            "event_chunks_written",
            "gap_chunks_written",
            "schema_version",
        }
        if set(raw) != expected:
            raise CaptureContractError("capture close-proof fields do not match schema")
        errors = raw.get("writer_errors")
        if not isinstance(errors, list):
            raise CaptureContractError("capture close-proof writer_errors is malformed")
        values = dict(raw)
        values["writer_errors"] = tuple(errors)
        return cls(**values)


@dataclass(frozen=True)
class SealedCaptureObjectRef:
    """One immutable object named by an exact-run seal.

    ``record_count`` is the number of JSONL rows for a chunk and one for a
    canonical standalone payload blob.  For a payload pack it is the number
    of independently content-addressed payload records in that physical pack.
    ``reference_count`` is one for a chunk and the exact number of event rows
    in this run referencing the standalone blob or any record in the pack.
    """

    kind: str
    relative_path: str
    sha256: str
    record_count: int
    reference_count: int
    raw_bytes: int
    compressed_bytes: int
    sequence_min: int | None = None
    sequence_max: int | None = None

    def __post_init__(self) -> None:
        kind = str(self.kind or "").strip()
        if kind not in _SEALED_OBJECT_KINDS:
            raise CaptureContractError(f"unsupported sealed object kind: {kind}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self,
            "relative_path",
            _validated_relative_path(self.relative_path, "sealed object relative_path"),
        )
        object.__setattr__(
            self, "sha256", _validated_sha256(self.sha256, "sealed object sha256")
        )
        for name in ("record_count", "reference_count", "raw_bytes", "compressed_bytes"):
            object.__setattr__(
                self,
                name,
                _validated_integer(
                    getattr(self, name), f"sealed object {name}", minimum=1
                ),
            )
        expected_prefix = {
            "event_chunk": "events",
            "gap_chunk": "gaps",
            "payload_blob": "blobs",
            "payload_pack": "blobs",
        }[kind]
        path = Path(self.relative_path)
        if path.parts[0] != expected_prefix or path.name.split(".", 1)[0] != self.sha256:
            raise CaptureContractError(
                "sealed object path does not match its kind/content address"
            )
        if kind in {"event_chunk", "gap_chunk"} and self.reference_count != 1:
            raise CaptureContractError("sealed chunks require reference_count=1")
        if kind == "payload_blob" and self.record_count != 1:
            raise CaptureContractError("sealed payload blobs require record_count=1")
        if kind == "event_chunk":
            if self.sequence_min is None or self.sequence_max is None:
                raise CaptureContractError(
                    "sealed event chunk requires valid sequence bounds"
                )
            sequence_min = _validated_integer(
                self.sequence_min, "sealed object sequence_min", minimum=1
            )
            sequence_max = _validated_integer(
                self.sequence_max, "sealed object sequence_max", minimum=1
            )
            if sequence_max < sequence_min:
                raise CaptureContractError(
                    "sealed event chunk requires valid sequence bounds"
                )
            if self.record_count > sequence_max - sequence_min + 1:
                raise CaptureContractError(
                    "sealed event chunk count exceeds its sequence bounds"
                )
            object.__setattr__(self, "sequence_min", sequence_min)
            object.__setattr__(self, "sequence_max", sequence_max)
        elif self.sequence_min is not None or self.sequence_max is not None:
            raise CaptureContractError(
                "only sealed event chunks may carry sequence bounds"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SealedCaptureObjectRef":
        expected = {
            "kind",
            "relative_path",
            "sha256",
            "record_count",
            "reference_count",
            "raw_bytes",
            "compressed_bytes",
            "sequence_min",
            "sequence_max",
        }
        if set(raw) != expected:
            raise CaptureContractError("sealed object fields do not match schema")
        return cls(**dict(raw))


def _run_content_root_payload(
    *,
    identity: CaptureRunIdentity,
    close_proof: CaptureLifecycleCloseProof,
    close_proof_sha256: str,
    objects: Iterable[SealedCaptureObjectRef],
    event_count: int,
    gap_count: int,
    gap_lost_count: int,
    event_accumulator_sha256: str,
    gap_accumulator_sha256: str,
    sequence_min: int | None,
    sequence_max: int | None,
    resource_hashes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    payload = {
        "identity": identity.to_dict(),
        "close_proof": close_proof.to_dict(),
        "close_proof_sha256": close_proof_sha256,
        "objects": [row.to_dict() for row in objects],
        "event_count": event_count,
        "gap_count": gap_count,
        "gap_lost_count": gap_lost_count,
        "event_accumulator_sha256": event_accumulator_sha256,
        "gap_accumulator_sha256": gap_accumulator_sha256,
        "sequence_min": sequence_min,
        "sequence_max": sequence_max,
    }
    if resource_hashes is not None:
        payload["resource_hashes"] = dict(resource_hashes)
    return payload


@dataclass(frozen=True)
class CaptureRunSeal:
    """Content-addressed, deterministic inventory for one exact run identity."""

    identity: CaptureRunIdentity
    close_proof: CaptureLifecycleCloseProof
    close_proof_sha256: str
    objects: tuple[SealedCaptureObjectRef, ...]
    event_count: int
    gap_count: int
    gap_lost_count: int
    event_accumulator_sha256: str
    gap_accumulator_sha256: str
    sequence_min: int | None
    sequence_max: int | None
    resource_measurement_sha256: str | None
    resource_policy_sha256: str | None
    resource_budget_sha256: str | None
    resource_binding_sha256: str | None
    content_root_sha256: str
    schema_version: str = CAPTURE_RUN_SEAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in {
            "chili-replay-capture-run-seal-v2",
            "chili-replay-capture-run-seal-v3",
            CAPTURE_RUN_SEAL_SCHEMA_VERSION,
        }:
            raise CaptureContractError("unsupported capture run seal schema")
        if not isinstance(self.identity, CaptureRunIdentity):
            raise CaptureContractError("seal identity must be a CaptureRunIdentity")
        if not isinstance(self.close_proof, CaptureLifecycleCloseProof):
            raise CaptureContractError(
                "seal close_proof must be a CaptureLifecycleCloseProof"
            )
        object.__setattr__(
            self,
            "close_proof_sha256",
            _validated_sha256(self.close_proof_sha256, "close_proof_sha256"),
        )
        if self.close_proof_sha256 != self.close_proof.proof_sha256:
            raise CaptureContractError("capture close-proof content hash mismatch")
        if self.close_proof.identity_sha256 != self.identity.identity_sha256:
            raise CaptureContractError("capture close-proof identity mismatch")
        objects = tuple(self.objects)
        if any(not isinstance(row, SealedCaptureObjectRef) for row in objects):
            raise CaptureContractError("seal objects must be SealedCaptureObjectRef values")
        ordered = tuple(sorted(objects, key=lambda row: (row.kind, row.relative_path)))
        if objects != ordered:
            raise CaptureContractError("seal objects must be deterministically ordered")
        paths = [row.relative_path for row in objects]
        if len(paths) != len(set(paths)):
            raise CaptureContractError("seal cannot list an object more than once")
        object.__setattr__(self, "objects", objects)
        for name in ("event_count", "gap_count", "gap_lost_count"):
            object.__setattr__(
                self,
                name,
                _validated_integer(
                    getattr(self, name), f"seal {name}", minimum=0
                ),
            )
        for name in ("event_accumulator_sha256", "gap_accumulator_sha256"):
            object.__setattr__(
                self,
                name,
                _validated_sha256(getattr(self, name), f"seal {name}"),
            )
        event_objects = tuple(row for row in objects if row.kind == "event_chunk")
        gap_objects = tuple(row for row in objects if row.kind == "gap_chunk")
        blob_objects = tuple(row for row in objects if row.kind == "payload_blob")
        if sum(row.record_count for row in event_objects) != self.event_count:
            raise CaptureContractError("seal event count does not match event chunks")
        if sum(row.record_count for row in gap_objects) != self.gap_count:
            raise CaptureContractError("seal gap count does not match gap chunks")
        if sum(row.reference_count for row in blob_objects) > self.event_count:
            raise CaptureContractError("seal blob reference count exceeds event count")
        if (
            (self.gap_count == 0 and self.gap_lost_count != 0)
            or (self.gap_count > 0 and self.gap_lost_count < self.gap_count)
        ):
            raise CaptureContractError("seal lost-gap count is inconsistent")
        if self.close_proof.events_written != self.event_count:
            raise CaptureContractError(
                "close-proof written event count does not match sealed inventory"
            )
        if self.close_proof.gap_records_written != self.gap_count:
            raise CaptureContractError(
                "close-proof gap record count does not match sealed inventory"
            )
        if self.close_proof.lost_events_recorded != self.gap_lost_count:
            raise CaptureContractError(
                "close-proof lost event count does not match sealed inventory"
            )
        if self.close_proof.event_chunks_written != len(event_objects):
            raise CaptureContractError(
                "close-proof event chunk count does not match sealed inventory"
            )
        if self.close_proof.gap_chunks_written != len(gap_objects):
            raise CaptureContractError(
                "close-proof gap chunk count does not match sealed inventory"
            )
        if (
            self.close_proof.written_event_accumulator_sha256
            != self.event_accumulator_sha256
        ):
            raise CaptureContractError(
                "close-proof event identities do not match sealed inventory"
            )
        if (
            self.close_proof.written_gap_accumulator_sha256
            != self.gap_accumulator_sha256
        ):
            raise CaptureContractError(
                "close-proof gap identities do not match sealed inventory"
            )
        if self.event_count == 0:
            if self.sequence_min is not None or self.sequence_max is not None:
                raise CaptureContractError("empty seal cannot carry sequence bounds")
        else:
            if self.sequence_min is None or self.sequence_max is None:
                raise CaptureContractError(
                    "non-empty seal requires valid sequence bounds"
                )
            sequence_min = _validated_integer(
                self.sequence_min, "seal sequence_min", minimum=1
            )
            sequence_max = _validated_integer(
                self.sequence_max, "seal sequence_max", minimum=1
            )
            if sequence_max < sequence_min:
                raise CaptureContractError(
                    "non-empty seal requires valid sequence bounds"
                )
            if (
                min(row.sequence_min for row in event_objects) != sequence_min
                or max(row.sequence_max for row in event_objects) != sequence_max
            ):
                raise CaptureContractError(
                    "seal sequence bounds do not match event chunks"
                )
            object.__setattr__(self, "sequence_min", sequence_min)
            object.__setattr__(self, "sequence_max", sequence_max)
            if (
                sequence_min < self.close_proof.submission_sequence_min
                or sequence_max > self.close_proof.submission_sequence_max
            ):
                raise CaptureContractError(
                    "sealed event sequences escape close-proof submission bounds"
                )
        object.__setattr__(
            self,
            "content_root_sha256",
            _validated_sha256(self.content_root_sha256, "content_root_sha256"),
        )
        resource_names = (
            "resource_measurement_sha256",
            "resource_policy_sha256",
            "resource_budget_sha256",
            "resource_binding_sha256",
        )
        present = tuple(getattr(self, name) is not None for name in resource_names)
        if any(present) and not all(present):
            raise CaptureContractError("capture seal resource hashes are incomplete")
        if self.schema_version == CAPTURE_RUN_SEAL_SCHEMA_VERSION and not all(present):
            raise CaptureContractError("v4 capture seal requires exact resource hashes")
        if self.schema_version != CAPTURE_RUN_SEAL_SCHEMA_VERSION and any(present):
            raise CaptureContractError("legacy capture seal cannot assert resource hashes")
        if all(present):
            for name in resource_names:
                object.__setattr__(
                    self,
                    name,
                    _validated_sha256(getattr(self, name), f"seal {name}"),
                )
        expected_root = sha256_json(
            _run_content_root_payload(
                identity=self.identity,
                close_proof=self.close_proof,
                close_proof_sha256=self.close_proof_sha256,
                objects=self.objects,
                event_count=self.event_count,
                gap_count=self.gap_count,
                gap_lost_count=self.gap_lost_count,
                event_accumulator_sha256=self.event_accumulator_sha256,
                gap_accumulator_sha256=self.gap_accumulator_sha256,
                sequence_min=self.sequence_min,
                sequence_max=self.sequence_max,
                resource_hashes=self.resource_hashes,
            )
        )
        if self.content_root_sha256 != expected_root:
            raise CaptureContractError("capture run seal content root mismatch")

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            **_run_content_root_payload(
                identity=self.identity,
                close_proof=self.close_proof,
                close_proof_sha256=self.close_proof_sha256,
                objects=self.objects,
                event_count=self.event_count,
                gap_count=self.gap_count,
                gap_lost_count=self.gap_lost_count,
                event_accumulator_sha256=self.event_accumulator_sha256,
                gap_accumulator_sha256=self.gap_accumulator_sha256,
                sequence_min=self.sequence_min,
                sequence_max=self.sequence_max,
                resource_hashes=self.resource_hashes,
            ),
            "content_root_sha256": self.content_root_sha256,
        }

    @property
    def seal_sha256(self) -> str:
        return sha256_json(self.to_record())

    @property
    def resource_hashes(self) -> dict[str, str] | None:
        if self.resource_binding_sha256 is None:
            return None
        assert self.resource_measurement_sha256 is not None
        assert self.resource_policy_sha256 is not None
        assert self.resource_budget_sha256 is not None
        return {
            "measurement_sha256": self.resource_measurement_sha256,
            "policy_sha256": self.resource_policy_sha256,
            "budget_sha256": self.resource_budget_sha256,
            "binding_sha256": self.resource_binding_sha256,
        }

    @classmethod
    def from_record(cls, raw: Mapping[str, Any]) -> "CaptureRunSeal":
        schema_version = str(raw.get("schema_version") or "")
        expected = {
            "schema_version",
            "identity",
            "close_proof",
            "close_proof_sha256",
            "objects",
            "event_count",
            "gap_count",
            "gap_lost_count",
            "event_accumulator_sha256",
            "gap_accumulator_sha256",
            "sequence_min",
            "sequence_max",
            "content_root_sha256",
        }
        if schema_version == CAPTURE_RUN_SEAL_SCHEMA_VERSION:
            expected.add("resource_hashes")
        if set(raw) != expected:
            raise CaptureContractError("capture run seal fields do not match schema")
        identity_raw = raw.get("identity")
        close_proof_raw = raw.get("close_proof")
        objects_raw = raw.get("objects")
        if (
            not isinstance(identity_raw, Mapping)
            or not isinstance(close_proof_raw, Mapping)
            or not isinstance(objects_raw, list)
        ):
            raise CaptureContractError(
                "capture run seal identity/proof/objects are malformed"
            )
        objects: list[SealedCaptureObjectRef] = []
        for row in objects_raw:
            if not isinstance(row, Mapping):
                raise CaptureContractError("capture run seal object is malformed")
            objects.append(SealedCaptureObjectRef.from_dict(row))
        resource_raw = raw.get("resource_hashes")
        if schema_version == CAPTURE_RUN_SEAL_SCHEMA_VERSION:
            if not isinstance(resource_raw, Mapping) or set(resource_raw) != {
                "measurement_sha256",
                "policy_sha256",
                "budget_sha256",
                "binding_sha256",
            }:
                raise CaptureContractError("capture run seal resource hashes are malformed")
        elif resource_raw is not None:
            raise CaptureContractError("legacy capture run seal has resource hashes")
        return cls(
            schema_version=schema_version,
            identity=CaptureRunIdentity.from_dict(identity_raw),
            close_proof=CaptureLifecycleCloseProof.from_dict(close_proof_raw),
            close_proof_sha256=str(raw.get("close_proof_sha256") or ""),
            objects=tuple(objects),
            event_count=raw.get("event_count"),
            gap_count=raw.get("gap_count"),
            gap_lost_count=raw.get("gap_lost_count"),
            event_accumulator_sha256=str(
                raw.get("event_accumulator_sha256") or ""
            ),
            gap_accumulator_sha256=str(raw.get("gap_accumulator_sha256") or ""),
            sequence_min=raw.get("sequence_min"),
            sequence_max=raw.get("sequence_max"),
            resource_measurement_sha256=(
                str(resource_raw.get("measurement_sha256") or "")
                if isinstance(resource_raw, Mapping)
                else None
            ),
            resource_policy_sha256=(
                str(resource_raw.get("policy_sha256") or "")
                if isinstance(resource_raw, Mapping)
                else None
            ),
            resource_budget_sha256=(
                str(resource_raw.get("budget_sha256") or "")
                if isinstance(resource_raw, Mapping)
                else None
            ),
            resource_binding_sha256=(
                str(resource_raw.get("binding_sha256") or "")
                if isinstance(resource_raw, Mapping)
                else None
            ),
            content_root_sha256=str(raw.get("content_root_sha256") or ""),
        )


@dataclass(frozen=True)
class SealedCaptureRun:
    seal: CaptureRunSeal
    events: tuple[CaptureEvent, ...]
    gaps: tuple[tuple[CaptureRunIdentity, CoverageGap], ...]


CAPTURE_RETENTION_PIN_SCHEMA_VERSION = "chili-replay-capture-retention-pin-v1"
CAPTURE_RETENTION_PLAN_SCHEMA_VERSION = "chili-replay-capture-retention-plan-v2"
CAPTURE_DERIVED_ARTIFACT_SCHEMA_VERSION = "chili-replay-capture-derived-v1"
CAPTURE_COLD_ARCHIVE_RECEIPT_SCHEMA_VERSION = (
    "chili-replay-capture-cold-archive-receipt-v1"
)
CAPTURE_RETENTION_DISPOSITION_SCHEMA_VERSION = (
    "chili-replay-capture-retention-disposition-v1"
)
_RETENTION_PIN_REASONS = frozenset({"ross_labeled_window", "traded_window"})


@dataclass(frozen=True)
class CaptureRetentionPin:
    """Immutable evidence pin for an exact Ross-labeled or traded window."""

    identity: CaptureRunIdentity
    reason: str
    window_start: datetime
    window_end: datetime
    evidence_sha256: str
    created_at: datetime
    schema_version: str = CAPTURE_RETENTION_PIN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURE_RETENTION_PIN_SCHEMA_VERSION:
            raise CaptureContractError("unsupported capture retention-pin schema")
        if not isinstance(self.identity, CaptureRunIdentity):
            raise CaptureContractError("retention pin identity is malformed")
        reason = str(self.reason or "").strip()
        if reason not in _RETENTION_PIN_REASONS:
            raise CaptureContractError("retention pin reason is not auditable")
        object.__setattr__(self, "reason", reason)
        start = _utc(self.window_start, "retention pin window_start")
        end = _utc(self.window_end, "retention pin window_end")
        if end < start:
            raise CaptureContractError("retention pin window is reversed")
        object.__setattr__(self, "window_start", start)
        object.__setattr__(self, "window_end", end)
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        object.__setattr__(
            self,
            "evidence_sha256",
            _validated_sha256(self.evidence_sha256, "retention pin evidence_sha256"),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity": self.identity.to_dict(),
            "reason": self.reason,
            "window_start": _iso(self.window_start),
            "window_end": _iso(self.window_end),
            "evidence_sha256": self.evidence_sha256,
            "created_at": _iso(self.created_at),
        }

    @property
    def pin_sha256(self) -> str:
        return sha256_json(self.to_record())

    @classmethod
    def from_record(cls, raw: Mapping[str, Any]) -> "CaptureRetentionPin":
        expected = {
            "schema_version",
            "identity",
            "reason",
            "window_start",
            "window_end",
            "evidence_sha256",
            "created_at",
        }
        if set(raw) != expected or not isinstance(raw.get("identity"), Mapping):
            raise CaptureContractError("capture retention-pin fields do not match schema")
        return cls(
            schema_version=str(raw.get("schema_version") or ""),
            identity=CaptureRunIdentity.from_dict(raw["identity"]),
            reason=str(raw.get("reason") or ""),
            window_start=_parse_utc(raw.get("window_start"), "pin.window_start"),
            window_end=_parse_utc(raw.get("window_end"), "pin.window_end"),
            evidence_sha256=str(raw.get("evidence_sha256") or ""),
            created_at=_parse_utc(raw.get("created_at"), "pin.created_at"),
        )


@dataclass(frozen=True)
class RetentionObjectRef:
    tier: str
    relative_path: str
    sha256: str
    bytes: int

    def __post_init__(self) -> None:
        if self.tier not in {"raw", "derived"}:
            raise CaptureContractError("retention object tier is unknown")
        object.__setattr__(
            self,
            "relative_path",
            _validated_relative_path(self.relative_path, "retention relative_path"),
        )
        object.__setattr__(
            self, "sha256", _validated_sha256(self.sha256, "retention object sha256")
        )
        object.__setattr__(
            self,
            "bytes",
            _validated_integer(self.bytes, "retention object bytes", minimum=1),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CaptureColdArchiveReceipt:
    """Content-addressed evidence for an externally preserved exact raw seal."""

    identity: CaptureRunIdentity
    seal_sha256: str
    archive_provider: str
    archive_object_sha256: str
    retrieval_evidence_sha256: str
    archived_at: datetime
    resource_binding_sha256: str
    schema_version: str = CAPTURE_COLD_ARCHIVE_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURE_COLD_ARCHIVE_RECEIPT_SCHEMA_VERSION:
            raise CaptureContractError("unsupported cold-archive receipt schema")
        if not isinstance(self.identity, CaptureRunIdentity):
            raise CaptureContractError("cold-archive identity is malformed")
        provider = str(self.archive_provider or "").strip()
        if not provider:
            raise CaptureContractError("cold-archive provider is required")
        object.__setattr__(self, "archive_provider", provider)
        for name in (
            "seal_sha256",
            "archive_object_sha256",
            "retrieval_evidence_sha256",
            "resource_binding_sha256",
        ):
            object.__setattr__(
                self,
                name,
                _validated_sha256(getattr(self, name), f"cold archive {name}"),
            )
        object.__setattr__(self, "archived_at", _utc(self.archived_at, "archived_at"))

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity": self.identity.to_dict(),
            "seal_sha256": self.seal_sha256,
            "archive_provider": self.archive_provider,
            "archive_object_sha256": self.archive_object_sha256,
            "retrieval_evidence_sha256": self.retrieval_evidence_sha256,
            "archived_at": _iso(self.archived_at),
            "resource_binding_sha256": self.resource_binding_sha256,
        }

    @property
    def receipt_sha256(self) -> str:
        return sha256_json(self.to_record())

    @classmethod
    def from_record(cls, raw: Mapping[str, Any]) -> "CaptureColdArchiveReceipt":
        expected = {
            "schema_version",
            "identity",
            "seal_sha256",
            "archive_provider",
            "archive_object_sha256",
            "retrieval_evidence_sha256",
            "archived_at",
            "resource_binding_sha256",
        }
        if set(raw) != expected or not isinstance(raw.get("identity"), Mapping):
            raise CaptureContractError("cold-archive receipt fields do not match schema")
        return cls(
            schema_version=str(raw.get("schema_version") or ""),
            identity=CaptureRunIdentity.from_dict(raw["identity"]),
            seal_sha256=str(raw.get("seal_sha256") or ""),
            archive_provider=str(raw.get("archive_provider") or ""),
            archive_object_sha256=str(raw.get("archive_object_sha256") or ""),
            retrieval_evidence_sha256=str(
                raw.get("retrieval_evidence_sha256") or ""
            ),
            archived_at=_parse_utc(raw.get("archived_at"), "archive.archived_at"),
            resource_binding_sha256=str(
                raw.get("resource_binding_sha256") or ""
            ),
        )


@dataclass(frozen=True)
class CaptureRetentionDisposition:
    """Irreversible local-certification tombstone for one exact final seal."""

    identity: CaptureRunIdentity
    seal_sha256: str
    disposition: str
    disposed_at: datetime
    deleted_objects: tuple[RetentionObjectRef, ...]
    resource_binding_sha256: str
    pin_sha256s_at_disposition: tuple[str, ...] = ()
    cold_archive_receipt_sha256: str | None = None
    schema_version: str = CAPTURE_RETENTION_DISPOSITION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURE_RETENTION_DISPOSITION_SCHEMA_VERSION:
            raise CaptureContractError("unsupported retention disposition schema")
        if not isinstance(self.identity, CaptureRunIdentity):
            raise CaptureContractError("retention disposition identity is malformed")
        if self.disposition not in {"raw_deleted", "raw_cold_archived"}:
            raise CaptureContractError("retention disposition is unknown")
        object.__setattr__(
            self, "seal_sha256", _validated_sha256(self.seal_sha256, "seal_sha256")
        )
        object.__setattr__(
            self,
            "resource_binding_sha256",
            _validated_sha256(
                self.resource_binding_sha256,
                "retention disposition resource_binding_sha256",
            ),
        )
        object.__setattr__(self, "disposed_at", _utc(self.disposed_at, "disposed_at"))
        objects = tuple(
            sorted(self.deleted_objects, key=lambda row: row.relative_path)
        )
        if not objects or any(
            not isinstance(row, RetentionObjectRef) or row.tier != "raw"
            for row in objects
        ):
            raise CaptureContractError(
                "retention disposition requires exact raw deleted objects"
            )
        if len({row.relative_path for row in objects}) != len(objects):
            raise CaptureContractError("retention disposition repeats an object")
        object.__setattr__(self, "deleted_objects", objects)
        pins = tuple(
            sorted(
                _validated_sha256(value, "retention disposition pin")
                for value in self.pin_sha256s_at_disposition
            )
        )
        if pins:
            raise CaptureContractError("pinned capture material cannot be retired")
        object.__setattr__(self, "pin_sha256s_at_disposition", pins)
        receipt = self.cold_archive_receipt_sha256
        if self.disposition == "raw_cold_archived":
            if receipt is None:
                raise CaptureContractError(
                    "cold-archived disposition requires an archive receipt"
                )
            object.__setattr__(
                self,
                "cold_archive_receipt_sha256",
                _validated_sha256(receipt, "cold archive receipt SHA"),
            )
        elif receipt is not None:
            raise CaptureContractError(
                "raw-deleted disposition cannot claim a cold archive receipt"
            )

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity": self.identity.to_dict(),
            "seal_sha256": self.seal_sha256,
            "disposition": self.disposition,
            "disposed_at": _iso(self.disposed_at),
            "deleted_objects": [row.to_dict() for row in self.deleted_objects],
            "resource_binding_sha256": self.resource_binding_sha256,
            "pin_sha256s_at_disposition": list(self.pin_sha256s_at_disposition),
            "cold_archive_receipt_sha256": self.cold_archive_receipt_sha256,
        }

    @property
    def disposition_sha256(self) -> str:
        return sha256_json(self.to_record())

    @classmethod
    def from_record(cls, raw: Mapping[str, Any]) -> "CaptureRetentionDisposition":
        expected = {
            "schema_version",
            "identity",
            "seal_sha256",
            "disposition",
            "disposed_at",
            "deleted_objects",
            "resource_binding_sha256",
            "pin_sha256s_at_disposition",
            "cold_archive_receipt_sha256",
        }
        object_rows = raw.get("deleted_objects")
        pins = raw.get("pin_sha256s_at_disposition")
        if (
            set(raw) != expected
            or not isinstance(raw.get("identity"), Mapping)
            or not isinstance(object_rows, list)
            or not isinstance(pins, list)
        ):
            raise CaptureContractError(
                "retention disposition fields do not match schema"
            )
        objects: list[RetentionObjectRef] = []
        for row in object_rows:
            if not isinstance(row, Mapping):
                raise CaptureContractError("retention disposition object is malformed")
            objects.append(RetentionObjectRef(**dict(row)))
        return cls(
            schema_version=str(raw.get("schema_version") or ""),
            identity=CaptureRunIdentity.from_dict(raw["identity"]),
            seal_sha256=str(raw.get("seal_sha256") or ""),
            disposition=str(raw.get("disposition") or ""),
            disposed_at=_parse_utc(raw.get("disposed_at"), "disposition.disposed_at"),
            deleted_objects=tuple(objects),
            resource_binding_sha256=str(
                raw.get("resource_binding_sha256") or ""
            ),
            pin_sha256s_at_disposition=tuple(str(value) for value in pins),
            cold_archive_receipt_sha256=(
                str(raw["cold_archive_receipt_sha256"])
                if raw.get("cold_archive_receipt_sha256") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class CaptureRetentionPlan:
    """Append-only authorization for a conservative, verified cleanup pass."""

    planned_at: datetime
    measurement_sha256: str
    policy_sha256: str
    budget_sha256: str
    resource_binding_sha256: str
    raw_cutoff: datetime
    derived_cutoff: datetime
    pin_sha256s: tuple[str, ...]
    delete_objects: tuple[RetentionObjectRef, ...]
    seal_dispositions: tuple[CaptureRetentionDisposition, ...]
    preserved_sealed_objects: int
    preserved_pinned_objects: int
    preserved_young_objects: int
    schema_version: str = CAPTURE_RETENTION_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURE_RETENTION_PLAN_SCHEMA_VERSION:
            raise CaptureContractError("unsupported capture retention-plan schema")
        object.__setattr__(self, "planned_at", _utc(self.planned_at, "planned_at"))
        object.__setattr__(self, "raw_cutoff", _utc(self.raw_cutoff, "raw_cutoff"))
        object.__setattr__(
            self, "derived_cutoff", _utc(self.derived_cutoff, "derived_cutoff")
        )
        for name in (
            "measurement_sha256",
            "policy_sha256",
            "budget_sha256",
            "resource_binding_sha256",
        ):
            object.__setattr__(
                self,
                name,
                _validated_sha256(getattr(self, name), f"retention {name}"),
            )
        pins = tuple(
            sorted(
                _validated_sha256(value, "retention pin_sha256")
                for value in self.pin_sha256s
            )
        )
        if len(pins) != len(set(pins)):
            raise CaptureContractError("retention plan repeats a pin")
        object.__setattr__(self, "pin_sha256s", pins)
        objects = tuple(
            sorted(self.delete_objects, key=lambda row: row.relative_path)
        )
        if any(not isinstance(row, RetentionObjectRef) for row in objects):
            raise CaptureContractError("retention plan object is malformed")
        if len({row.relative_path for row in objects}) != len(objects):
            raise CaptureContractError("retention plan repeats an object")
        object.__setattr__(self, "delete_objects", objects)
        dispositions = tuple(
            sorted(
                self.seal_dispositions,
                key=lambda row: (row.identity.identity_sha256, row.seal_sha256),
            )
        )
        if any(not isinstance(row, CaptureRetentionDisposition) for row in dispositions):
            raise CaptureContractError("retention plan disposition is malformed")
        if len({row.seal_sha256 for row in dispositions}) != len(dispositions):
            raise CaptureContractError("retention plan repeats a seal disposition")
        deleted_paths = {row.relative_path for row in objects}
        if any(
            not {item.relative_path for item in row.deleted_objects}.issubset(
                deleted_paths
            )
            for row in dispositions
        ):
            raise CaptureContractError(
                "retention disposition escapes the plan delete inventory"
            )
        object.__setattr__(self, "seal_dispositions", dispositions)
        for name in (
            "preserved_sealed_objects",
            "preserved_pinned_objects",
            "preserved_young_objects",
        ):
            object.__setattr__(
                self,
                name,
                _validated_integer(getattr(self, name), name, minimum=0),
            )

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "planned_at": _iso(self.planned_at),
            "resource_hashes": {
                "measurement_sha256": self.measurement_sha256,
                "policy_sha256": self.policy_sha256,
                "budget_sha256": self.budget_sha256,
                "binding_sha256": self.resource_binding_sha256,
            },
            "tiers": {
                "raw": {"cutoff": _iso(self.raw_cutoff)},
                "derived": {"cutoff": _iso(self.derived_cutoff)},
            },
            "pin_sha256s": list(self.pin_sha256s),
            "delete_objects": [row.to_dict() for row in self.delete_objects],
            "seal_dispositions": [
                row.to_record() for row in self.seal_dispositions
            ],
            "preserved": {
                "sealed_objects": self.preserved_sealed_objects,
                "pinned_objects": self.preserved_pinned_objects,
                "young_objects": self.preserved_young_objects,
            },
        }

    @property
    def plan_sha256(self) -> str:
        return sha256_json(self.to_record())


CAPTURE_STORE_OWNERSHIP_SCHEMA_VERSION = "chili-replay-capture-store-owner-v1"
_STORE_OWNER_LOCK_NAME = ".chili-capture-store-owner.lock"


class _ExclusiveCaptureStoreOwnership:
    """Cross-process writer lease backed by an OS lock and immutable receipts."""

    def __init__(
        self,
        root: Path,
        *,
        resource_binding_sha256: str | None,
        host_fingerprint_sha256: str | None,
        lease_seconds: float,
        heartbeat_seconds: float,
        wall_clock: Callable[[], datetime],
    ) -> None:
        if not callable(wall_clock):
            raise CaptureContractError("capture ownership wall clock must be callable")
        self.root = root.resolve()
        self.lock_path = self.root / _STORE_OWNER_LOCK_NAME
        self.receipt_root = self.root / "ownership" / "receipts"
        self.resource_binding_sha256 = (
            _validated_sha256(
                resource_binding_sha256, "owner resource_binding_sha256"
            )
            if resource_binding_sha256 is not None
            else None
        )
        self.host_fingerprint_sha256 = (
            _validated_sha256(
                host_fingerprint_sha256, "owner host_fingerprint_sha256"
            )
            if host_fingerprint_sha256 is not None
            else "0" * 64
        )
        self.lease_seconds = float(lease_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)
        if (
            not math.isfinite(self.lease_seconds)
            or not math.isfinite(self.heartbeat_seconds)
            or self.lease_seconds <= 0
            or self.heartbeat_seconds <= 0
            or self.heartbeat_seconds >= self.lease_seconds
        ):
            raise CaptureContractError("capture ownership lease timing is invalid")
        self._wall_clock = wall_clock
        self.owner_token = str(uuid.uuid4())
        self._handle: Any | None = None
        self._record: dict[str, Any] | None = None
        self._closed = False
        self._thread_lock = threading.RLock()
        self._acquire()

    @staticmethod
    def _lock_handle(handle: Any) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise CaptureContractError(
                    "capture store has another writer or foreign ownership ambiguity"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise CaptureContractError(
                    "capture store has another writer or foreign ownership ambiguity"
                ) from exc

    @staticmethod
    def _unlock_handle(handle: Any) -> None:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass

    def _now(self) -> datetime:
        value = self._wall_clock()
        return _utc(value, "capture ownership wall clock")

    @staticmethod
    def _parse_record(raw: bytes) -> dict[str, Any] | None:
        if not raw or raw == b"\0":
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CaptureContractError(
                "capture store ownership record is corrupt or ambiguous"
            ) from exc
        expected = {
            "schema_version",
            "state",
            "owner_token",
            "pid",
            "host_fingerprint_sha256",
            "resource_binding_sha256",
            "acquired_at",
            "heartbeat_at",
            "lease_expires_at",
            "previous_receipt_sha256",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != expected
            or canonical_json_bytes(value) != raw
            or value.get("schema_version") != CAPTURE_STORE_OWNERSHIP_SCHEMA_VERSION
            or value.get("state") not in {"active", "released"}
        ):
            raise CaptureContractError(
                "capture store ownership record is corrupt or ambiguous"
            )
        try:
            str(uuid.UUID(str(value.get("owner_token"))))
        except ValueError as exc:
            raise CaptureContractError("capture store owner token is malformed") from exc
        _validated_integer(value.get("pid"), "capture store owner pid", minimum=1)
        _validated_sha256(
            value.get("host_fingerprint_sha256"),
            "capture store owner host fingerprint",
        )
        binding_sha = value.get("resource_binding_sha256")
        if binding_sha is not None:
            _validated_sha256(binding_sha, "capture store owner resource binding")
        previous = value.get("previous_receipt_sha256")
        if previous is not None:
            _validated_sha256(previous, "capture store owner previous receipt")
        acquired = _parse_utc(value.get("acquired_at"), "owner.acquired_at")
        heartbeat = _parse_utc(value.get("heartbeat_at"), "owner.heartbeat_at")
        expires = _parse_utc(value.get("lease_expires_at"), "owner.lease_expires_at")
        if heartbeat < acquired or expires < heartbeat:
            raise CaptureContractError("capture store ownership clocks are malformed")
        return dict(value)

    def _read_record(self) -> dict[str, Any] | None:
        assert self._handle is not None
        self._handle.seek(0)
        return self._parse_record(self._handle.read())

    def _publish_receipt(self, record: Mapping[str, Any]) -> str:
        raw = canonical_json_bytes(record)
        digest = hashlib.sha256(raw).hexdigest()
        self.receipt_root.mkdir(parents=True, exist_ok=True)
        target = self.receipt_root / f"{digest}.json"
        if target.exists():
            if target.read_bytes() != raw:
                raise CaptureContractError("capture ownership receipt collision")
            return digest
        temp = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            with temp.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp, target)
            except FileExistsError:
                if target.read_bytes() != raw:
                    raise CaptureContractError("capture ownership receipt collision")
            except OSError as exc:
                raise CaptureContractError(
                    "append-only capture ownership receipt publish unavailable"
                ) from exc
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
        return digest

    def _verify_receipt(self, record: Mapping[str, Any]) -> str:
        raw = canonical_json_bytes(record)
        digest = hashlib.sha256(raw).hexdigest()
        path = self.receipt_root / f"{digest}.json"
        try:
            persisted = path.read_bytes()
        except FileNotFoundError as exc:
            raise CaptureContractError(
                "capture store ownership receipt is missing or ambiguous"
            ) from exc
        if persisted != raw:
            raise CaptureContractError(
                "capture store ownership receipt is corrupt or ambiguous"
            )
        return digest

    def _write_record(self, record: dict[str, Any]) -> str:
        assert self._handle is not None
        raw = canonical_json_bytes(record)
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(raw)
        self._handle.flush()
        os.fsync(self._handle.fileno())
        receipt = self._publish_receipt(record)
        self._record = dict(record)
        return receipt

    def _record_for(
        self,
        *,
        state: str,
        now: datetime,
        acquired_at: datetime,
        previous_receipt_sha256: str | None,
    ) -> dict[str, Any]:
        expires = now + (
            timedelta(seconds=self.lease_seconds)
            if state == "active"
            else timedelta(0)
        )
        return {
            "schema_version": CAPTURE_STORE_OWNERSHIP_SCHEMA_VERSION,
            "state": state,
            "owner_token": self.owner_token,
            "pid": os.getpid(),
            "host_fingerprint_sha256": (
                self.host_fingerprint_sha256
            ),
            "resource_binding_sha256": self.resource_binding_sha256,
            "acquired_at": _iso(acquired_at),
            "heartbeat_at": _iso(now),
            "lease_expires_at": _iso(expires),
            "previous_receipt_sha256": previous_receipt_sha256,
        }

    def _acquire(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(exist_ok=True)
        handle = self.lock_path.open("r+b", buffering=0)
        if self.lock_path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            self._lock_handle(handle)
            self._handle = handle
            prior = self._read_record()
            now = self._now()
            previous_receipt: str | None = None
            if prior is not None:
                previous_receipt = self._verify_receipt(prior)
                prior_expires = _parse_utc(
                    prior["lease_expires_at"], "owner.lease_expires_at"
                )
                if prior["state"] == "active" and prior_expires > now:
                    raise CaptureContractError(
                        "capture store has an unexpired foreign ownership record"
                    )
            record = self._record_for(
                state="active",
                now=now,
                acquired_at=now,
                previous_receipt_sha256=previous_receipt,
            )
            self._write_record(record)
        except Exception:
            self._unlock_handle(handle)
            handle.close()
            self._handle = None
            raise

    def assert_valid(self, *, renew_if_due: bool = True) -> None:
        with self._thread_lock:
            if self._closed or self._handle is None or self._record is None:
                raise CaptureContractError("capture store ownership is closed")
            on_disk = self._read_record()
            if on_disk != self._record:
                raise CaptureContractError(
                    "capture store ownership changed or is ambiguous"
                )
            if on_disk["state"] != "active" or on_disk["owner_token"] != self.owner_token:
                raise CaptureContractError("capture store ownership is foreign")
            self._verify_receipt(on_disk)
            now = self._now()
            heartbeat = _parse_utc(on_disk["heartbeat_at"], "owner.heartbeat_at")
            expires = _parse_utc(on_disk["lease_expires_at"], "owner.lease_expires_at")
            if now < heartbeat:
                raise CaptureContractError("capture store ownership clock moved backwards")
            if now >= expires:
                raise CaptureContractError("capture store ownership lease expired")
            if renew_if_due and (expires - now).total_seconds() <= self.heartbeat_seconds:
                self.renew()

    def renew(self) -> str:
        with self._thread_lock:
            self.assert_valid(renew_if_due=False)
            assert self._record is not None
            now = self._now()
            acquired_at = _parse_utc(self._record["acquired_at"], "owner.acquired_at")
            previous = hashlib.sha256(
                canonical_json_bytes(self._record)
            ).hexdigest()
            record = self._record_for(
                state="active",
                now=now,
                acquired_at=acquired_at,
                previous_receipt_sha256=previous,
            )
            return self._write_record(record)

    def health(self) -> dict[str, Any]:
        with self._thread_lock:
            failure: str | None = None
            try:
                self.assert_valid()
            except CaptureContractError as exc:
                failure = str(exc)
            record = self._record
            return {
                "enforced": True,
                "owner_token": self.owner_token,
                "pid": os.getpid(),
                "resource_binding_sha256": self.resource_binding_sha256,
                "host_fingerprint_sha256": self.host_fingerprint_sha256,
                "lease_seconds": self.lease_seconds,
                "heartbeat_seconds": self.heartbeat_seconds,
                "state": record.get("state") if record else "closed",
                "heartbeat_at": record.get("heartbeat_at") if record else None,
                "lease_expires_at": record.get("lease_expires_at") if record else None,
                "record_sha256": (
                    hashlib.sha256(canonical_json_bytes(record)).hexdigest()
                    if record
                    else None
                ),
                "failure": failure,
                "fail_closed": failure is not None,
            }

    def close(self) -> None:
        with self._thread_lock:
            if self._closed:
                return
            handle = self._handle
            if handle is None:
                self._closed = True
                return
            try:
                if self._record is not None:
                    now = self._now()
                    acquired_at = _parse_utc(
                        self._record["acquired_at"], "owner.acquired_at"
                    )
                    previous = hashlib.sha256(
                        canonical_json_bytes(self._record)
                    ).hexdigest()
                    self._write_record(
                        self._record_for(
                            state="released",
                            now=now,
                            acquired_at=acquired_at,
                            previous_receipt_sha256=previous,
                        )
                    )
            finally:
                self._unlock_handle(handle)
                handle.close()
                self._handle = None
                self._closed = True


class ContentAddressedCaptureStore:
    """Immutable compressed payload blobs and partitioned event chunks."""

    def __init__(
        self,
        root: str | Path,
        *,
        compression_codec: str = "zstd",
        compression_level: int = 3,
        payload_pack_max_records: int = 2_048,
        payload_pack_target_raw_bytes: int = 8 * 1024 * 1024,
        payload_pack_read_cache_entries: int = 4,
        resource_binding: CaptureResourceBinding | None = None,
        disk_usage_provider: Callable[[str | os.PathLike[str]], Any] = shutil.disk_usage,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        ownership_lease_seconds: float | None = None,
        ownership_heartbeat_seconds: float | None = None,
    ) -> None:
        codec = str(compression_codec or "").strip().lower()
        if codec not in {"zstd", "zlib"}:
            raise CaptureContractError("compression codec must be zstd or zlib")
        level = int(compression_level)
        if codec == "zlib" and not 0 <= level <= 9:
            raise CaptureContractError("zlib compression level must be 0..9")
        if codec == "zstd" and not 1 <= level <= 22:
            raise CaptureContractError("zstd compression level must be 1..22")
        if codec == "zstd" and zstd is None:
            raise CaptureContractError(
                "zstd capture selected but the zstandard dependency is unavailable"
            )
        self.root = Path(root).resolve()
        self.compression_codec = codec
        self.compression_level = level
        self.storage_policy = CaptureStoragePolicy(
            compression_codec=codec,
            compression_level=level,
            pack_max_records=payload_pack_max_records,
            pack_target_raw_bytes=payload_pack_target_raw_bytes,
            pack_read_cache_entries=payload_pack_read_cache_entries,
        )
        if resource_binding is not None and not isinstance(
            resource_binding, CaptureResourceBinding
        ):
            raise CaptureContractError("capture store resource binding is malformed")
        if not callable(disk_usage_provider) or not callable(monotonic_clock):
            raise CaptureContractError("capture store resource probes must be callable")
        self.resource_binding = resource_binding
        if resource_binding is not None:
            policy_lease = resource_binding.policy.store_owner_lease_seconds
            policy_heartbeat = resource_binding.policy.store_owner_heartbeat_seconds
            if ownership_lease_seconds is not None and float(ownership_lease_seconds) != policy_lease:
                raise CaptureContractError("store owner lease differs from resource policy")
            if ownership_heartbeat_seconds is not None and float(ownership_heartbeat_seconds) != policy_heartbeat:
                raise CaptureContractError("store owner heartbeat differs from resource policy")
            owner_lease = policy_lease
            owner_heartbeat = policy_heartbeat
        else:
            owner_lease = float(ownership_lease_seconds or 30.0)
            owner_heartbeat = float(ownership_heartbeat_seconds or 10.0)
        self._disk_usage_provider = disk_usage_provider
        self._monotonic_clock = monotonic_clock
        self._publish_state_lock = threading.RLock()
        self._payload_pack_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._seal_lock = threading.Lock()
        self._retention_lock = threading.Lock()
        self._dirty_paths: set[Path] = set()
        self._durable_paths: set[Path] = set()
        self._payload_locations: dict[str, str] = {}
        self._payload_locations_loaded = False
        self._payload_duplicate_locations = 0
        self._resource_failure_reasons: list[str] = []
        self._reserved_publish_bytes = 0
        self._published_bytes = 0
        self._publish_seconds = 0.0
        self._publish_count = 0
        self._sync_calls = 0
        self._synced_objects = 0
        self._sync_seconds = 0.0
        self._sync_failures = 0
        self.root.mkdir(parents=True, exist_ok=True)
        self._ownership = _ExclusiveCaptureStoreOwnership(
            self.root,
            resource_binding_sha256=(
                resource_binding.binding_sha256
                if resource_binding is not None
                else None
            ),
            host_fingerprint_sha256=(
                resource_binding.measurement.host_fingerprint_sha256
                if resource_binding is not None
                else None
            ),
            lease_seconds=owner_lease,
            heartbeat_seconds=owner_heartbeat,
            wall_clock=wall_clock,
        )
        self._tracked_root_bytes = self._root_file_bytes()
        try:
            if self.resource_binding is not None:
                record = canonical_json_bytes(self.resource_binding.to_record())
                audit_path = (
                    self.root
                    / "resource_audits"
                    / f"{self.resource_binding.binding_sha256}.json"
                )
                self._publish(audit_path, record)
            storage_record = canonical_json_bytes(self.storage_policy.to_record())
            storage_audit_path = (
                self.root
                / "storage_audits"
                / f"{self.storage_policy.policy_sha256}.json"
            )
            self._publish(storage_audit_path, storage_record)
        except Exception:
            self._ownership.close()
            raise

    @classmethod
    def from_measurement(
        cls,
        root: str | Path,
        *,
        measurement: CaptureResourceMeasurement,
        policy: CaptureBudgetPolicy,
        compression_codec: str = "zstd",
        compression_level: int = 3,
        payload_pack_max_records: int = 2_048,
        payload_pack_target_raw_bytes: int = 8 * 1024 * 1024,
        payload_pack_read_cache_entries: int = 4,
        disk_usage_provider: Callable[[str | os.PathLike[str]], Any] = shutil.disk_usage,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> "ContentAddressedCaptureStore":
        return cls(
            root,
            compression_codec=compression_codec,
            compression_level=compression_level,
            payload_pack_max_records=payload_pack_max_records,
            payload_pack_target_raw_bytes=payload_pack_target_raw_bytes,
            payload_pack_read_cache_entries=payload_pack_read_cache_entries,
            resource_binding=CaptureResourceBinding.resolve(measurement, policy),
            disk_usage_provider=disk_usage_provider,
            monotonic_clock=monotonic_clock,
            wall_clock=wall_clock,
        )

    def _root_file_bytes(self) -> int:
        total = 0
        for path in self.root.rglob("*"):
            name = path.name
            temporary_token = (
                name[1:-4].rsplit(".", 1)[-1]
                if name.startswith(".") and name.endswith(".tmp")
                else ""
            )
            store_owned_temporary = (
                len(temporary_token) == 32
                and all(char in "0123456789abcdef" for char in temporary_token)
            )
            if (
                path.is_file()
                and name != _STORE_OWNER_LOCK_NAME
                and not store_owned_temporary
            ):
                total += path.stat().st_size
        return total

    def close(self) -> None:
        self._ownership.close()

    def __enter__(self) -> "ContentAddressedCaptureStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.close()
        return False

    def __del__(self) -> None:
        ownership = getattr(self, "_ownership", None)
        if ownership is not None:
            try:
                ownership.close()
            except Exception:
                pass

    def renew_ownership(self) -> str:
        return self._ownership.renew()

    def _disk_free_bytes(self) -> int:
        usage = self._disk_usage_provider(self.root)
        try:
            free = int(usage.free)
        except (AttributeError, TypeError, ValueError) as exc:
            raise CaptureContractError("capture disk-usage probe is malformed") from exc
        if free < 0:
            raise CaptureContractError("capture disk-usage probe returned negative free space")
        return free

    def _record_resource_failure(self, reason: str) -> None:
        if reason not in self._resource_failure_reasons:
            self._resource_failure_reasons.append(reason)

    def _assert_publish_capacity(self, additional_bytes: int) -> None:
        self._ownership.assert_valid()
        binding = self.resource_binding
        actual_root_bytes = self._root_file_bytes()
        if actual_root_bytes < self._tracked_root_bytes:
            self._record_resource_failure("capture_owned_object_removed_outside_store")
        elif actual_root_bytes > self._tracked_root_bytes:
            # Cross-process ownership receipts are part of this store's quota
            # even though they bypass the content-object publisher.
            self._tracked_root_bytes = actual_root_bytes
        if self._resource_failure_reasons:
            raise CaptureContractError(
                "capture resource gate is dirty: "
                + ", ".join(self._resource_failure_reasons)
            )
        if binding is None:
            return
        projected = (
            self._tracked_root_bytes
            + self._reserved_publish_bytes
            + int(additional_bytes)
        )
        if projected > binding.budget.disk_quota_bytes:
            self._record_resource_failure("capture_disk_quota_exceeded")
            raise CaptureContractError("capture disk quota would be exceeded")
        free = self._disk_free_bytes()
        if (
            free - self._reserved_publish_bytes - int(additional_bytes)
            < binding.policy.disk_reserve_bytes
        ):
            self._record_resource_failure("capture_disk_reserve_breached")
            raise CaptureContractError("capture disk reserve would be breached")

    def resource_health(
        self, ingress: BoundedCaptureIngress | None = None
    ) -> dict[str, Any]:
        binding = self.resource_binding
        ownership_health = self._ownership.health()
        # A publish creates the immutable hard link before it advances the
        # tracked byte count.  Keep the filesystem scan in the same critical
        # section as that transition; otherwise a concurrent health probe can
        # observe the new link, advance the counter itself, and then make the
        # publisher count the same object a second time.  The next probe would
        # falsely report an externally removed object and poison the run.
        with self._publish_state_lock:
            actual_root_bytes = self._root_file_bytes()
            if actual_root_bytes < self._tracked_root_bytes:
                self._record_resource_failure(
                    "capture_owned_object_removed_outside_store"
                )
            elif actual_root_bytes > self._tracked_root_bytes:
                # Ownership receipts bypass the content-object publisher but
                # remain part of this store's measured quota.
                self._tracked_root_bytes = actual_root_bytes
            root_bytes = max(self._tracked_root_bytes, actual_root_bytes)
            published_bytes = self._published_bytes
            publish_seconds = self._publish_seconds
            publish_count = self._publish_count
            payload_locations_indexed = len(self._payload_locations)
            payload_duplicate_locations = self._payload_duplicate_locations
            sync_calls = self._sync_calls
            synced_objects = self._synced_objects
            sync_seconds = self._sync_seconds
            sync_failures = self._sync_failures
            dirty_objects = len(self._dirty_paths)
            durable_objects = len(self._durable_paths)
            resource_failure_reasons = tuple(self._resource_failure_reasons)
        free = self._disk_free_bytes()
        ingress_health = ingress.health() if ingress is not None else None
        ingress_failed_closed = bool(
            ingress_health is not None
            and ingress_health.get("backpressure_state") == "failed_closed"
        )
        observed_bps = (
            published_bytes / publish_seconds
            if publish_seconds > 0
            else 0.0
        )
        return {
            "enforced": binding is not None,
            "resource_hashes": binding.hashes if binding is not None else None,
            "root_bytes": root_bytes,
            "actual_root_bytes": actual_root_bytes,
            "untracked_root_bytes": max(0, actual_root_bytes - root_bytes),
            "disk_free_bytes": free,
            "disk_quota_bytes": (
                binding.budget.disk_quota_bytes if binding is not None else None
            ),
            "disk_reserve_bytes": (
                binding.policy.disk_reserve_bytes if binding is not None else None
            ),
            "quota_remaining_bytes": (
                binding.budget.disk_quota_bytes - root_bytes
                if binding is not None
                else None
            ),
            "reserve_margin_bytes": (
                free - binding.policy.disk_reserve_bytes
                if binding is not None
                else None
            ),
            "sustained_write_budget_bytes_per_second": (
                binding.budget.sustained_write_budget_bytes_per_second
                if binding is not None
                else None
            ),
            "observed_publish_bytes_per_second": observed_bps,
            "published_bytes": published_bytes,
            "publish_count": publish_count,
            "storage_policy": self.storage_policy.to_record(),
            "storage_policy_sha256": self.storage_policy.policy_sha256,
            "payload_locations_indexed": payload_locations_indexed,
            "payload_duplicate_locations": payload_duplicate_locations,
            "sync": {
                "calls": sync_calls,
                "objects": synced_objects,
                "seconds": sync_seconds,
                "failures": sync_failures,
                "dirty_objects": dirty_objects,
                "durable_objects_this_process": durable_objects,
            },
            "resource_failure_reasons": resource_failure_reasons,
            "fail_closed": bool(resource_failure_reasons)
            or bool(ownership_health["fail_closed"])
            or ingress_failed_closed,
            "ingress_backpressure": ingress_health,
            "exclusive_ownership": ownership_health,
        }

    @property
    def _extension(self) -> str:
        return "zst" if self.compression_codec == "zstd" else "zlib"

    def _compress(self, raw: bytes) -> bytes:
        if self.compression_codec == "zstd":
            assert zstd is not None
            return zstd.ZstdCompressor(level=self.compression_level).compress(raw)
        return zlib.compress(raw, level=self.compression_level)

    @staticmethod
    def _decompress(path: Path, compressed: bytes) -> bytes:
        if path.suffix == ".zst":
            if zstd is None:
                raise CaptureContractError(
                    "zstd capture exists but the zstandard dependency is unavailable"
                )
            try:
                return zstd.ZstdDecompressor().decompress(compressed)
            except zstd.ZstdError as exc:
                raise CaptureContractError(f"corrupt zstd capture object: {path}") from exc
        if path.suffix == ".zlib":
            try:
                return zlib.decompress(compressed)
            except zlib.error as exc:
                raise CaptureContractError(f"corrupt zlib capture object: {path}") from exc
        raise CaptureContractError(f"unknown capture compression suffix: {path}")

    def _publish(self, target: Path, content: bytes) -> None:
        """Publish without overwriting an existing content-addressed object."""

        self._ownership.assert_valid()
        target.parent.mkdir(parents=True, exist_ok=True)
        publish_started = float(self._monotonic_clock())
        if not math.isfinite(publish_started):
            self._record_resource_failure("capture_publish_clock_non_finite")
            raise CaptureContractError(
                "capture publish clock returned a non-finite value"
            )
        with self._publish_state_lock:
            if target.exists():
                if target.read_bytes() != content:
                    raise CaptureContractError(
                        f"content-address collision or corruption: {target}"
                    )
                return
            self._assert_publish_capacity(len(content))
            self._reserved_publish_bytes += len(content)
        temp = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        published_new = False
        try:
            with temp.open("xb") as handle:
                handle.write(content)
                handle.flush()
            with self._publish_state_lock:
                if target.exists():
                    if target.read_bytes() != content:
                        raise CaptureContractError(
                            f"content-address collision or corruption: {target}"
                        )
                    return
                self._reject_new_chunk_for_sealed_run(target)
                try:
                    os.link(temp, target)
                    published_new = True
                except FileExistsError:
                    if target.read_bytes() != content:
                        raise CaptureContractError(
                            f"content-address collision or corruption: {target}"
                        )
                except OSError as exc:
                    # Do not fall back to overwrite-capable rename semantics.  If
                    # this volume cannot atomically create a hard link, capture is
                    # unavailable and the run must fail closed.
                    raise CaptureContractError(
                        f"append-only publish unavailable for {target}: {exc}"
                    ) from exc
                if published_new:
                    self._dirty_paths.add(target)
                    self._tracked_root_bytes += len(content)
                    publish_finished = float(self._monotonic_clock())
                    if not math.isfinite(publish_started) or not math.isfinite(
                        publish_finished
                    ):
                        self._record_resource_failure(
                            "capture_publish_clock_non_finite"
                        )
                        raise CaptureContractError(
                            "capture publish clock returned a non-finite value"
                        )
                    self._published_bytes += len(content)
                    self._publish_seconds += max(
                        0.0, publish_finished - publish_started
                    )
                    self._publish_count += 1
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
            with self._publish_state_lock:
                self._reserved_publish_bytes -= len(content)

    def _reject_new_chunk_for_sealed_run(self, target: Path) -> None:
        """Fence late chunks once this store has published an exact-run seal."""

        try:
            parts = target.resolve().relative_to(self.root).parts
        except ValueError as exc:
            raise CaptureContractError("capture object escaped capture root") from exc
        if (
            len(parts) != 5
            or parts[0] not in {"events", "gaps"}
            or not parts[2].startswith("run=")
            or not parts[3].startswith("generation=")
        ):
            return
        run_id = parts[2].removeprefix("run=")
        generation = parts[3].removeprefix("generation=")
        seal_dir = self.root / "seals" / f"run={run_id}" / f"generation={generation}"
        if any(seal_dir.glob("*.json")):
            raise CaptureContractError(
                f"capture run {run_id} generation {generation} is already sealed"
            )

    def sync(self) -> int:
        """Durably flush newly published objects before a clean-close claim.

        Capture writes stay batched and asynchronous; this is called by a
        writer only after its ingress has been fenced and drained.  A failure
        propagates and keeps the run from being marked clean.
        """

        self._ownership.assert_valid()
        synced = 0
        sync_started = float(self._monotonic_clock())
        if not math.isfinite(sync_started):
            self._record_resource_failure("capture_sync_clock_non_finite")
            raise CaptureContractError(
                "capture sync clock returned a non-finite value"
            )
        with self._sync_lock:
            self._sync_calls += 1
            while True:
                with self._publish_state_lock:
                    paths = tuple(sorted(self._dirty_paths))
                if not paths:
                    sync_finished = float(self._monotonic_clock())
                    if not math.isfinite(sync_finished):
                        self._record_resource_failure(
                            "capture_sync_clock_non_finite"
                        )
                        raise CaptureContractError(
                            "capture sync clock returned a non-finite value"
                        )
                    self._sync_seconds += max(0.0, sync_finished - sync_started)
                    return synced
                for path in paths:
                    try:
                        # Windows requires a writable file descriptor for
                        # FlushFileBuffers (which backs ``os.fsync``).
                        with path.open("rb+") as handle:
                            os.fsync(handle.fileno())
                    except FileNotFoundError as exc:
                        self._sync_failures += 1
                        self._record_resource_failure(
                            "capture_object_missing_before_sync"
                        )
                        raise CaptureContractError(
                            f"published capture object disappeared before sync: {path}"
                        ) from exc
                    except OSError as exc:
                        self._sync_failures += 1
                        self._record_resource_failure("capture_fsync_failed")
                        raise CaptureContractError(
                            f"capture object fsync failed: {path}: {exc}"
                        ) from exc
                    with self._publish_state_lock:
                        self._dirty_paths.discard(path)
                        self._durable_paths.add(path.resolve())
                    self._synced_objects += 1
                    synced += 1

    def _payload_reference_path(
        self, relative_path: str, *, expected_payload_sha256: str
    ) -> tuple[str, Path, str]:
        """Validate one standalone or packed payload reference.

        Returns ``(layout, absolute_path, physical_object_sha256)``.  The
        logical payload digest remains independent from a pack's physical
        object digest.
        """

        relative = _validated_relative_path(relative_path, "event payload_ref")
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise CaptureContractError("payload ref escaped capture root") from exc
        suffix = path.suffix
        if suffix not in {".zst", ".zlib"}:
            raise CaptureContractError("payload ref compression is unknown")
        parts = Path(relative).parts
        filename_digest = _validated_sha256(
            path.name.split(".", 1)[0], "payload object filename SHA256"
        )
        if (
            len(parts) == 4
            and parts[0] == "blobs"
            and parts[1] == "sha256"
            and parts[2] == expected_payload_sha256[:2]
            and filename_digest == expected_payload_sha256
        ):
            return "standalone", path, filename_digest
        if (
            len(parts) == 5
            and parts[0] == "blobs"
            and parts[1] == "packs"
            and parts[2] == "sha256"
            and parts[3] == filename_digest[:2]
        ):
            return "pack", path, filename_digest
        raise CaptureContractError("payload ref/content address mismatch")

    def _load_payload_pack(
        self, path: Path
    ) -> tuple[dict[str, Mapping[str, Any]], bytes, bytes, str]:
        raw, compressed = self._object_material(path)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CaptureContractError("payload pack is invalid JSON") from exc
        if not isinstance(value, Mapping) or canonical_json_bytes(value) != raw:
            raise CaptureContractError("payload pack is not canonical JSON")
        if set(value) != {
            "payloads",
            "schema_version",
            "storage_policy_sha256",
        }:
            raise CaptureContractError("payload pack fields do not match schema")
        if value.get("schema_version") != PAYLOAD_PACK_SCHEMA_VERSION:
            raise CaptureContractError("payload pack schema is unsupported")
        storage_policy_sha256 = _validated_sha256(
            value.get("storage_policy_sha256"),
            "payload pack storage_policy_sha256",
        )
        rows = value.get("payloads")
        if not isinstance(rows, list) or not rows:
            raise CaptureContractError("payload pack must contain payload records")
        payloads: dict[str, Mapping[str, Any]] = {}
        ordered_digests: list[str] = []
        for row in rows:
            if not isinstance(row, Mapping) or set(row) != {
                "payload",
                "payload_sha256",
            }:
                raise CaptureContractError("payload pack record is malformed")
            digest = _validated_sha256(
                row.get("payload_sha256"), "packed payload SHA256"
            )
            payload = row.get("payload")
            if not isinstance(payload, Mapping):
                raise CaptureContractError("packed payload must be a mapping")
            if hashlib.sha256(canonical_json_bytes(payload)).hexdigest() != digest:
                raise CaptureContractError("packed payload content mismatch")
            if digest in payloads:
                raise CaptureContractError("payload pack contains duplicate digest")
            payloads[digest] = payload
            ordered_digests.append(digest)
        if ordered_digests != sorted(ordered_digests):
            raise CaptureContractError("payload pack records are not deterministic")
        return payloads, raw, compressed, storage_policy_sha256

    def _discover_payload_locations(self) -> None:
        """Build a verified in-process dedupe index for existing pack objects."""

        if self._payload_locations_loaded:
            return
        root = self.root / "blobs" / "packs" / "sha256"
        paths = tuple(root.rglob("*.json.zst")) + tuple(
            root.rglob("*.json.zlib")
        )
        for path in sorted(path.resolve() for path in paths):
            payloads, _raw, _compressed, _policy_sha256 = self._load_payload_pack(
                path
            )
            relative = path.relative_to(self.root).as_posix()
            for digest in payloads:
                prior = self._payload_locations.get(digest)
                if prior is None:
                    self._payload_locations[digest] = relative
                elif prior != relative:
                    # Duplicate physical records are valid evidence but not a
                    # reason to create another copy. Pick deterministically.
                    self._payload_duplicate_locations += 1
                    self._payload_locations[digest] = min(prior, relative)
        self._payload_locations_loaded = True

    def _payload_pack_groups(
        self,
        rows: list[tuple[str, Mapping[str, Any]]],
    ) -> tuple[tuple[tuple[str, Mapping[str, Any]], ...], ...]:
        """Split sorted unique logical payloads by the explicit pack policy."""

        if not rows:
            return ()
        empty_record = {
            "payloads": [],
            "schema_version": PAYLOAD_PACK_SCHEMA_VERSION,
            "storage_policy_sha256": self.storage_policy.policy_sha256,
        }
        wrapper_bytes = len(canonical_json_bytes(empty_record))
        groups: list[tuple[tuple[str, Mapping[str, Any]], ...]] = []
        pending: list[tuple[str, Mapping[str, Any]]] = []
        pending_bytes = wrapper_bytes
        for digest, payload in rows:
            entry_bytes = len(
                canonical_json_bytes(
                    {"payload": payload, "payload_sha256": digest}
                )
            )
            projected = pending_bytes + entry_bytes + (1 if pending else 0)
            if pending and (
                len(pending) >= self.storage_policy.pack_max_records
                or projected > self.storage_policy.pack_target_raw_bytes
            ):
                groups.append(tuple(pending))
                pending = []
                pending_bytes = wrapper_bytes
            pending.append((digest, payload))
            pending_bytes += entry_bytes + (1 if len(pending) > 1 else 0)
        if pending:
            groups.append(tuple(pending))
        return tuple(groups)

    def _put_packed_payloads(
        self, payloads: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, BlobRef]:
        """Persist new logical payloads in bounded immutable physical packs."""

        if not payloads:
            return {}
        canonical: dict[str, tuple[Mapping[str, Any], bytes]] = {}
        for requested_digest, payload in payloads.items():
            if not isinstance(payload, Mapping):
                raise CaptureContractError("payload pack input must be a mapping")
            raw = canonical_json_bytes(payload)
            digest = hashlib.sha256(raw).hexdigest()
            if digest != requested_digest:
                raise CaptureContractError("payload pack input digest mismatch")
            canonical[digest] = (payload, raw)

        with self._payload_pack_lock:
            self._discover_payload_locations()
            pending: list[tuple[str, Mapping[str, Any]]] = []
            for digest, (payload, _raw) in sorted(canonical.items()):
                if digest in self._payload_locations:
                    continue
                # Reuse a legacy standalone object if one already exists.
                standalone_paths = tuple(
                    self.root
                    / "blobs"
                    / "sha256"
                    / digest[:2]
                    / f"{digest}.json.{suffix}"
                    for suffix in ("zst", "zlib")
                )
                existing = next(
                    (path for path in standalone_paths if path.is_file()), None
                )
                if existing is not None:
                    loaded = self.get_payload(
                        digest,
                        relative_path=existing.relative_to(self.root).as_posix(),
                    )
                    if canonical_json_bytes(loaded) != canonical[digest][1]:
                        raise CaptureContractError(
                            "standalone payload content address mismatch"
                        )
                    self._payload_locations[digest] = existing.relative_to(
                        self.root
                    ).as_posix()
                    continue
                pending.append((digest, payload))

            for group in self._payload_pack_groups(pending):
                record = {
                    "payloads": [
                        {"payload": payload, "payload_sha256": digest}
                        for digest, payload in group
                    ],
                    "schema_version": PAYLOAD_PACK_SCHEMA_VERSION,
                    "storage_policy_sha256": self.storage_policy.policy_sha256,
                }
                raw = canonical_json_bytes(record)
                pack_digest = hashlib.sha256(raw).hexdigest()
                compressed = self._compress(raw)
                relative = (
                    Path("blobs")
                    / "packs"
                    / "sha256"
                    / pack_digest[:2]
                    / f"{pack_digest}.json.{self._extension}"
                )
                self._publish(self.root / relative, compressed)
                relative_text = relative.as_posix()
                for digest, _payload in group:
                    self._payload_locations[digest] = relative_text

            refs: dict[str, BlobRef] = {}
            for digest, (_payload, raw) in canonical.items():
                relative = self._payload_locations.get(digest)
                if relative is None:
                    raise CaptureContractError(
                        "payload pack publish did not create a logical location"
                    )
                path = self.root / relative
                try:
                    compressed_bytes = path.stat().st_size
                except FileNotFoundError as exc:
                    raise CaptureContractError(
                        f"payload blob unavailable after publish: {digest}"
                    ) from exc
                refs[digest] = BlobRef(
                    sha256=digest,
                    raw_bytes=len(raw),
                    compressed_bytes=compressed_bytes,
                    relative_path=relative,
                )
            return refs

    def put_payload(self, payload: Mapping[str, Any]) -> BlobRef:
        raw = canonical_json_bytes(payload)
        digest = hashlib.sha256(raw).hexdigest()
        compressed = self._compress(raw)
        relative = (
            Path("blobs")
            / "sha256"
            / digest[:2]
            / f"{digest}.json.{self._extension}"
        )
        self._publish(self.root / relative, compressed)
        return BlobRef(
            sha256=digest,
            raw_bytes=len(raw),
            compressed_bytes=len(compressed),
            relative_path=relative.as_posix(),
        )

    def get_payload(
        self, sha256: str, *, relative_path: str | None = None
    ) -> Mapping[str, Any]:
        digest = str(sha256 or "").lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise CaptureContractError("invalid payload SHA256")
        if relative_path is None:
            relative_path = (
                self.root
                / "blobs"
                / "sha256"
                / digest[:2]
                / f"{digest}.json.{self._extension}"
            ).relative_to(self.root).as_posix()
        layout, path, _physical_digest = self._payload_reference_path(
            relative_path, expected_payload_sha256=digest
        )
        try:
            if layout == "pack":
                payloads, _raw, _compressed, _policy_sha256 = (
                    self._load_payload_pack(path)
                )
                payload = payloads.get(digest)
                if payload is None:
                    raise CaptureContractError(
                        f"payload blob unavailable in packed object: {digest}"
                    )
                return payload
            raw = self._decompress(path, path.read_bytes())
        except FileNotFoundError as exc:
            raise CaptureContractError(f"payload blob unavailable: {digest}") from exc
        if hashlib.sha256(raw).hexdigest() != digest:
            raise CaptureContractError(f"payload blob content mismatch: {digest}")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CaptureContractError(
                f"payload blob is not canonical JSON: {digest}"
            ) from exc
        if not isinstance(value, Mapping):
            raise CaptureContractError("payload blob must decode to a mapping")
        return value

    def _write_chunk(
        self,
        *,
        base: Path,
        rows: Iterable[Mapping[str, Any]],
    ) -> ChunkRef | None:
        encoded_rows = [canonical_json_bytes(row) for row in rows]
        if not encoded_rows:
            return None
        raw = b"\n".join(encoded_rows) + b"\n"
        digest = hashlib.sha256(raw).hexdigest()
        compressed = self._compress(raw)
        relative = base / f"{digest}.jsonl.{self._extension}"
        self._publish(self.root / relative, compressed)
        return ChunkRef(
            sha256=digest,
            row_count=len(encoded_rows),
            raw_bytes=len(raw),
            compressed_bytes=len(compressed),
            relative_path=relative.as_posix(),
        )

    def write_events(self, events: Iterable[CaptureEvent]) -> tuple[ChunkRef, ...]:
        event_rows: list[tuple[CaptureEvent, dict[str, Any], str | None]] = []
        external_payloads: dict[str, Mapping[str, Any]] = {}
        for event in events:
            policy = STREAM_POLICIES[event.stream]
            externalize_payload = policy.content_dedup_allowed
            record = event.to_record(include_payload=not externalize_payload)
            payload_digest: str | None = None
            if externalize_payload:
                payload_digest = hashlib.sha256(
                    canonical_json_bytes(event.payload)
                ).hexdigest()
                external_payloads[payload_digest] = event.payload
            event_rows.append((event, record, payload_digest))

        payload_refs = self._put_packed_payloads(external_payloads)
        grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
        for event, record, payload_digest in event_rows:
            if payload_digest is not None:
                record["payload_ref"] = payload_refs[payload_digest].relative_path
            record["event_sha256"] = event.event_sha256
            day = event.clocks.available_at.date().isoformat()
            key = (day, event.identity.run_id, event.identity.generation)
            grouped[key].append(record)
        refs: list[ChunkRef] = []
        for key in sorted(grouped):
            day, run_id, generation = key
            rows = sorted(
                grouped[key],
                key=lambda row: (int(row["sequence"]), str(row["event_sha256"])),
            )
            ref = self._write_chunk(
                base=(
                    Path("events")
                    / f"date={day}"
                    / f"run={run_id}"
                    / f"generation={generation}"
                ),
                rows=rows,
            )
            if ref:
                refs.append(ref)
        return tuple(refs)

    def write_gaps(
        self,
        gaps: Iterable[tuple[CaptureRunIdentity, CoverageGap]],
    ) -> tuple[ChunkRef, ...]:
        grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
        for identity, gap in gaps:
            day = gap.first_available_at.date().isoformat()
            key = (day, identity.run_id, identity.generation)
            grouped[key].append(
                {
                    "schema_version": CAPTURE_SCHEMA_VERSION,
                    "identity": identity.to_dict(),
                    "gap": gap.to_dict(),
                }
            )
        refs: list[ChunkRef] = []
        for key in sorted(grouped):
            day, run_id, generation = key
            ref = self._write_chunk(
                base=(
                    Path("gaps")
                    / f"date={day}"
                    / f"run={run_id}"
                    / f"generation={generation}"
                ),
                rows=sorted(grouped[key], key=canonical_json_bytes),
            )
            if ref:
                refs.append(ref)
        return tuple(refs)

    def put_retention_pin(self, pin: CaptureRetentionPin) -> str:
        """Append one immutable Ross/trade evidence pin and return its path."""

        if not isinstance(pin, CaptureRetentionPin):
            raise CaptureContractError("retention pin must be CaptureRetentionPin")
        if any(
            row.identity == pin.identity
            for row in self.load_retention_dispositions()
        ):
            raise CaptureContractError(
                "retired capture identity cannot be resurrected by a retention pin"
            )
        relative = Path("retention") / "pins" / f"{pin.pin_sha256}.json"
        self._publish(self.root / relative, canonical_json_bytes(pin.to_record()))
        return relative.as_posix()

    def load_retention_pins(self) -> tuple[CaptureRetentionPin, ...]:
        pins: list[CaptureRetentionPin] = []
        directory = self.root / "retention" / "pins"
        if not directory.exists():
            return ()
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.suffix != ".json":
                raise CaptureContractError(
                    f"unexpected object in retention pin ledger: {path}"
                )
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != path.stem:
                raise CaptureContractError("retention pin content address mismatch")
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CaptureContractError("retention pin is invalid JSON") from exc
            if not isinstance(value, Mapping) or canonical_json_bytes(value) != raw:
                raise CaptureContractError("retention pin is not canonical JSON")
            pin = CaptureRetentionPin.from_record(value)
            if pin.pin_sha256 != path.stem:
                raise CaptureContractError("retention pin hash mismatch")
            pins.append(pin)
        return tuple(pins)

    def put_cold_archive_receipt(
        self, receipt: CaptureColdArchiveReceipt
    ) -> str:
        if not isinstance(receipt, CaptureColdArchiveReceipt):
            raise CaptureContractError(
                "cold archive receipt must be CaptureColdArchiveReceipt"
            )
        binding = self.resource_binding
        if binding is None or receipt.resource_binding_sha256 != binding.binding_sha256:
            raise CaptureContractError("cold archive receipt resource binding mismatch")
        seal = self._read_exact_seal(receipt.identity)
        if seal.seal_sha256 != receipt.seal_sha256:
            raise CaptureContractError("cold archive receipt seal mismatch")
        relative = (
            Path("retention")
            / "cold_archive_receipts"
            / f"{receipt.receipt_sha256}.json"
        )
        self._publish(self.root / relative, canonical_json_bytes(receipt.to_record()))
        return relative.as_posix()

    def load_cold_archive_receipts(self) -> tuple[CaptureColdArchiveReceipt, ...]:
        directory = self.root / "retention" / "cold_archive_receipts"
        if not directory.exists():
            return ()
        receipts: list[CaptureColdArchiveReceipt] = []
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.suffix != ".json":
                raise CaptureContractError(
                    f"unexpected object in cold archive receipt ledger: {path}"
                )
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != path.stem:
                raise CaptureContractError("cold archive receipt content address mismatch")
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CaptureContractError("cold archive receipt is invalid JSON") from exc
            if not isinstance(value, Mapping) or canonical_json_bytes(value) != raw:
                raise CaptureContractError("cold archive receipt is not canonical JSON")
            receipt = CaptureColdArchiveReceipt.from_record(value)
            if receipt.receipt_sha256 != path.stem:
                raise CaptureContractError("cold archive receipt hash mismatch")
            receipts.append(receipt)
        return tuple(receipts)

    def _put_retention_disposition(
        self, disposition: CaptureRetentionDisposition
    ) -> str:
        if not isinstance(disposition, CaptureRetentionDisposition):
            raise CaptureContractError(
                "retention disposition must be CaptureRetentionDisposition"
            )
        binding = self.resource_binding
        if (
            binding is None
            or disposition.resource_binding_sha256 != binding.binding_sha256
        ):
            raise CaptureContractError("retention disposition resource binding mismatch")
        if any(pin.identity == disposition.identity for pin in self.load_retention_pins()):
            raise CaptureContractError("pinned capture identity cannot be retired")
        seal = self._read_exact_seal(disposition.identity)
        if seal.seal_sha256 != disposition.seal_sha256:
            raise CaptureContractError("retention disposition exact seal mismatch")
        sealed_paths = {row.relative_path for row in seal.objects}
        if not {
            row.relative_path for row in disposition.deleted_objects
        }.issubset(sealed_paths):
            raise CaptureContractError(
                "retention disposition object is outside its exact seal"
            )
        if disposition.disposition == "raw_cold_archived":
            receipts = {
                row.receipt_sha256: row for row in self.load_cold_archive_receipts()
            }
            receipt = receipts.get(disposition.cold_archive_receipt_sha256 or "")
            if (
                receipt is None
                or receipt.identity != disposition.identity
                or receipt.seal_sha256 != disposition.seal_sha256
            ):
                raise CaptureContractError(
                    "retention disposition cold archive receipt mismatch"
                )
        existing = tuple(
            row
            for row in self.load_retention_dispositions()
            if row.seal_sha256 == disposition.seal_sha256
        )
        if existing and existing != (disposition,):
            raise CaptureContractError("conflicting retention disposition exists")
        relative = (
            Path("retention")
            / "dispositions"
            / f"run={disposition.identity.run_id}"
            / f"generation={disposition.identity.generation}"
            / f"{disposition.disposition_sha256}.json"
        )
        self._publish(
            self.root / relative,
            canonical_json_bytes(disposition.to_record()),
        )
        return relative.as_posix()

    def load_retention_dispositions(
        self,
    ) -> tuple[CaptureRetentionDisposition, ...]:
        directory = self.root / "retention" / "dispositions"
        if not directory.exists():
            return ()
        dispositions: list[CaptureRetentionDisposition] = []
        seen_seals: set[str] = set()
        for candidate in directory.rglob("*"):
            relative_parts = candidate.relative_to(directory).parts
            if candidate.is_symlink() or (
                candidate.is_file() and candidate.suffix != ".json"
            ):
                raise CaptureContractError(
                    f"unexpected object in retention disposition ledger: {candidate}"
                )
            if candidate.is_dir() and (
                len(relative_parts) > 2
                or not relative_parts[0].startswith("run=")
                or (
                    len(relative_parts) == 2
                    and not relative_parts[1].startswith("generation=")
                )
            ):
                raise CaptureContractError(
                    f"unexpected directory in retention disposition ledger: {candidate}"
                )
        for path in sorted(directory.rglob("*.json")):
            try:
                parts = path.relative_to(directory).parts
            except ValueError as exc:
                raise CaptureContractError("retention disposition escaped ledger") from exc
            if (
                len(parts) != 3
                or not parts[0].startswith("run=")
                or not parts[1].startswith("generation=")
                or not path.is_file()
            ):
                raise CaptureContractError(
                    f"unexpected object in retention disposition ledger: {path}"
                )
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != path.stem:
                raise CaptureContractError(
                    "retention disposition content address mismatch"
                )
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CaptureContractError(
                    "retention disposition is invalid JSON"
                ) from exc
            if not isinstance(value, Mapping) or canonical_json_bytes(value) != raw:
                raise CaptureContractError(
                    "retention disposition is not canonical JSON"
                )
            disposition = CaptureRetentionDisposition.from_record(value)
            if (
                disposition.disposition_sha256 != path.stem
                or disposition.identity.run_id != parts[0].removeprefix("run=")
                or str(disposition.identity.generation)
                != parts[1].removeprefix("generation=")
            ):
                raise CaptureContractError("retention disposition partition mismatch")
            if disposition.seal_sha256 in seen_seals:
                raise CaptureContractError(
                    "multiple retention dispositions exist for one exact seal"
                )
            seen_seals.add(disposition.seal_sha256)
            dispositions.append(disposition)
        return tuple(dispositions)

    def put_derived_artifact(
        self,
        *,
        identity: CaptureRunIdentity,
        kind: str,
        window_start: datetime,
        window_end: datetime,
        payload: Mapping[str, Any],
    ) -> RetentionObjectRef:
        """Persist a generic content-addressed derived bar/feature artifact."""

        if not isinstance(identity, CaptureRunIdentity):
            raise CaptureContractError("derived artifact identity is malformed")
        normalized_kind = str(kind or "").strip()
        if not normalized_kind or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
            for char in normalized_kind
        ):
            raise CaptureContractError("derived artifact kind is malformed")
        start = _utc(window_start, "derived window_start")
        end = _utc(window_end, "derived window_end")
        if end < start:
            raise CaptureContractError("derived artifact window is reversed")
        if not isinstance(payload, Mapping):
            raise CaptureContractError("derived artifact payload must be a mapping")
        record = {
            "schema_version": CAPTURE_DERIVED_ARTIFACT_SCHEMA_VERSION,
            "identity": identity.to_dict(),
            "kind": normalized_kind,
            "window_start": _iso(start),
            "window_end": _iso(end),
            "payload": payload,
            "payload_sha256": sha256_json(payload),
        }
        raw = canonical_json_bytes(record)
        digest = hashlib.sha256(raw).hexdigest()
        relative = (
            Path("derived")
            / f"date={end.date().isoformat()}"
            / f"run={identity.run_id}"
            / f"generation={identity.generation}"
            / f"{digest}.json"
        )
        self._publish(self.root / relative, raw)
        return RetentionObjectRef(
            tier="derived",
            relative_path=relative.as_posix(),
            sha256=digest,
            bytes=len(raw),
        )

    @staticmethod
    def _pin_intersects(
        pins: Iterable[CaptureRetentionPin],
        *,
        identity: CaptureRunIdentity,
        window_start: datetime,
        window_end: datetime,
    ) -> bool:
        return any(
            pin.identity == identity
            and pin.window_start <= window_end
            and pin.window_end >= window_start
            for pin in pins
        )

    def _load_derived_retention_metadata(
        self, path: Path
    ) -> tuple[CaptureRunIdentity, datetime, datetime, RetentionObjectRef]:
        path = path.resolve()
        try:
            parts = path.relative_to(self.root).parts
        except ValueError as exc:
            raise CaptureContractError("derived artifact escaped capture root") from exc
        if (
            len(parts) != 5
            or parts[0] != "derived"
            or not parts[1].startswith("date=")
            or not parts[2].startswith("run=")
            or not parts[3].startswith("generation=")
            or path.suffix != ".json"
        ):
            raise CaptureContractError(f"invalid derived artifact partition: {path}")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != path.stem:
            raise CaptureContractError("derived artifact content address mismatch")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CaptureContractError("derived artifact is invalid JSON") from exc
        expected = {
            "schema_version",
            "identity",
            "kind",
            "window_start",
            "window_end",
            "payload",
            "payload_sha256",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != expected
            or canonical_json_bytes(value) != raw
            or value.get("schema_version") != CAPTURE_DERIVED_ARTIFACT_SCHEMA_VERSION
            or not isinstance(value.get("identity"), Mapping)
            or not isinstance(value.get("payload"), Mapping)
        ):
            raise CaptureContractError("derived artifact fields do not match schema")
        identity = CaptureRunIdentity.from_dict(value["identity"])
        start = _parse_utc(value.get("window_start"), "derived.window_start")
        end = _parse_utc(value.get("window_end"), "derived.window_end")
        if end < start or sha256_json(value["payload"]) != value.get("payload_sha256"):
            raise CaptureContractError("derived artifact payload/window is invalid")
        try:
            generation = int(parts[3].removeprefix("generation="))
        except ValueError as exc:
            raise CaptureContractError("derived generation partition is invalid") from exc
        if (
            end.date().isoformat() != parts[1].removeprefix("date=")
            or identity.run_id != parts[2].removeprefix("run=")
            or identity.generation != generation
        ):
            raise CaptureContractError("derived artifact row/partition mismatch")
        return (
            identity,
            start,
            end,
            RetentionObjectRef(
                tier="derived",
                relative_path=path.relative_to(self.root).as_posix(),
                sha256=digest,
                bytes=len(raw),
            ),
        )

    def _raw_retention_metadata(
        self, path: Path, *, kind: str
    ) -> tuple[
        CaptureRunIdentity,
        datetime,
        datetime,
        set[str],
        RetentionObjectRef,
    ]:
        partition_day, partition_run, partition_generation = self._partition_identity(
            path, kind=kind
        )
        raw, _compressed = self._object_material(path)
        rows = self._read_chunk_rows(path)
        if not rows:
            raise CaptureContractError("retention cannot own an empty capture chunk")
        identities: set[CaptureRunIdentity] = set()
        starts: list[datetime] = []
        ends: list[datetime] = []
        blob_references: dict[str, dict[str, int]] = {}
        payload_cache: dict[str, dict[str, Mapping[str, Any]]] = {}
        if kind == "events":
            for row in rows:
                payload = self._payload_from_record(
                    row,
                    blob_references=blob_references,
                    payload_cache=payload_cache,
                )
                event = CaptureEvent.from_record(row, payload=payload)
                if event.event_sha256 != str(row.get("event_sha256") or ""):
                    raise CaptureContractError("retention event content address mismatch")
                identities.add(event.identity)
                starts.append(event.clocks.available_at)
                ends.append(event.clocks.available_at)
        elif kind == "gaps":
            for row in rows:
                identity, gap = self._gap_from_row(row)
                identities.add(identity)
                starts.append(gap.first_available_at)
                ends.append(gap.last_available_at)
        else:
            raise CaptureContractError("retention raw kind is unknown")
        if len(identities) != 1:
            raise CaptureContractError("capture chunk mixes run identities")
        identity = next(iter(identities))
        if (
            identity.run_id != partition_run
            or identity.generation != partition_generation
            or any(value.date().isoformat() != partition_day for value in starts)
        ):
            raise CaptureContractError("retention chunk row/partition mismatch")
        relative = path.resolve().relative_to(self.root).as_posix()
        return (
            identity,
            min(starts),
            max(ends),
            set(blob_references),
            RetentionObjectRef(
                tier="raw",
                relative_path=relative,
                sha256=hashlib.sha256(raw).hexdigest(),
                bytes=path.stat().st_size,
            ),
        )

    def _verified_active_seals(self) -> dict[str, CaptureRunSeal]:
        dispositions = {
            row.seal_sha256: row for row in self.load_retention_dispositions()
        }
        seals: dict[str, CaptureRunSeal] = {}
        observed_seals: set[str] = set()
        seal_root = self.root / "seals"
        if not seal_root.exists():
            if dispositions:
                raise CaptureContractError(
                    "retention disposition exists without its immutable seal"
                )
            return seals
        for path in sorted(seal_root.rglob("*.json")):
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != path.stem:
                raise CaptureContractError("retention found a corrupt capture seal")
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CaptureContractError("retention found an invalid capture seal") from exc
            if not isinstance(value, Mapping) or canonical_json_bytes(value) != raw:
                raise CaptureContractError("retention found a non-canonical capture seal")
            seal = CaptureRunSeal.from_record(value)
            if seal.seal_sha256 != path.stem:
                raise CaptureContractError("retention found a mismatched capture seal")
            observed_seals.add(seal.seal_sha256)
            disposition = dispositions.get(seal.seal_sha256)
            if disposition is not None:
                if disposition.identity != seal.identity:
                    raise CaptureContractError(
                        "retention disposition identity does not match its seal"
                    )
                continue
            self.load_sealed_run(
                seal.identity, expected_seal_sha256=seal.seal_sha256
            )
            if seal.identity.identity_sha256 in seals:
                raise CaptureContractError("multiple active seals share one identity")
            seals[seal.identity.identity_sha256] = seal
        if set(dispositions) - observed_seals:
            raise CaptureContractError(
                "retention disposition exists without its immutable seal"
            )
        return seals

    def _build_retention_plan(self, *, planned_at: datetime) -> CaptureRetentionPlan:
        binding = self.resource_binding
        if binding is None:
            raise CaptureContractError(
                "retention requires an exact measured resource binding"
            )
        now = _utc(planned_at, "retention planned_at")
        raw_cutoff = now - timedelta(days=binding.budget.raw_retention_days)
        derived_cutoff = now - timedelta(
            days=binding.budget.derived_retention_days
        )
        pins = self.load_retention_pins()
        active_seals = self._verified_active_seals()
        pinned_identity_sha256s = {pin.identity.identity_sha256 for pin in pins}
        delete_objects: list[RetentionObjectRef] = []
        retained_blob_refs: set[str] = set()
        pinned_blob_refs: set[str] = set()
        young_blob_refs: set[str] = set()
        preserved_sealed = 0
        preserved_pinned = 0
        preserved_young = 0

        raw_by_identity: dict[
            str,
            list[
                tuple[
                    Path,
                    CaptureRunIdentity,
                    datetime,
                    datetime,
                    set[str],
                    RetentionObjectRef,
                ]
            ],
        ] = defaultdict(list)
        for kind in ("events", "gaps"):
            paths = tuple((self.root / kind).rglob("*.jsonl.zst")) + tuple(
                (self.root / kind).rglob("*.jsonl.zlib")
            )
            for path in sorted(paths):
                identity, start, end, blob_refs, object_ref = (
                    self._raw_retention_metadata(path, kind=kind)
                )
                raw_by_identity[identity.identity_sha256].append(
                    (path.resolve(), identity, start, end, blob_refs, object_ref)
                )

        for identity_sha256, rows in sorted(raw_by_identity.items()):
            identity = rows[0][1]
            if any(row[1] != identity for row in rows):
                raise CaptureContractError("retention identity hash collision")
            if identity_sha256 in pinned_identity_sha256s:
                for _path, _identity, _start, _end, blob_refs, _object_ref in rows:
                    preserved_pinned += 1
                    pinned_blob_refs.update(blob_refs)
                    retained_blob_refs.update(blob_refs)
                continue
            active_seal = active_seals.get(identity_sha256)
            has_young = any(
                end >= raw_cutoff
                or path in self._dirty_paths
                or datetime.fromtimestamp(path.stat().st_mtime, UTC) >= raw_cutoff
                for path, _identity, _start, end, _blob_refs, _object_ref in rows
            )
            if active_seal is not None and has_young:
                for _path, _identity, _start, _end, blob_refs, _object_ref in rows:
                    preserved_sealed += 1
                    young_blob_refs.update(blob_refs)
                    retained_blob_refs.update(blob_refs)
                continue
            for path, _identity, _start, end, blob_refs, object_ref in rows:
                if (
                    end >= raw_cutoff
                    or path in self._dirty_paths
                    or datetime.fromtimestamp(path.stat().st_mtime, UTC) >= raw_cutoff
                ):
                    preserved_young += 1
                    young_blob_refs.update(blob_refs)
                    retained_blob_refs.update(blob_refs)
                else:
                    delete_objects.append(object_ref)

        derived_root = self.root / "derived"
        if derived_root.exists():
            for path in sorted(derived_root.rglob("*.json")):
                identity, start, end, object_ref = (
                    self._load_derived_retention_metadata(path)
                )
                if self._pin_intersects(
                    pins,
                    identity=identity,
                    window_start=start,
                    window_end=end,
                ):
                    preserved_pinned += 1
                elif (
                    end >= derived_cutoff
                    or path.resolve() in self._dirty_paths
                    or datetime.fromtimestamp(path.stat().st_mtime, UTC)
                    >= derived_cutoff
                ):
                    preserved_young += 1
                else:
                    delete_objects.append(object_ref)

        blob_root = self.root / "blobs"
        if blob_root.exists():
            blob_paths = tuple(blob_root.rglob("*.json.zst")) + tuple(
                blob_root.rglob("*.json.zlib")
            )
            for path in sorted(blob_paths):
                raw, _compressed = self._object_material(path)
                relative = path.resolve().relative_to(self.root).as_posix()
                digest = path.name.split(".", 1)[0]
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CaptureContractError("retention found invalid payload blob") from exc
                if not isinstance(value, Mapping) or canonical_json_bytes(value) != raw:
                    raise CaptureContractError("retention found non-canonical payload blob")
                if relative in retained_blob_refs:
                    if relative in pinned_blob_refs:
                        preserved_pinned += 1
                    else:
                        preserved_young += 1
                    continue
                modified_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
                if modified_at >= raw_cutoff:
                    preserved_young += 1
                    continue
                delete_objects.append(
                    RetentionObjectRef(
                        tier="raw",
                        relative_path=relative,
                        sha256=digest,
                        bytes=path.stat().st_size,
                    )
                )

        delete_by_path = {row.relative_path: row for row in delete_objects}
        dispositions: list[CaptureRetentionDisposition] = []
        for identity_sha256, seal in sorted(active_seals.items()):
            retired_objects = tuple(
                delete_by_path[row.relative_path]
                for row in seal.objects
                if row.relative_path in delete_by_path
            )
            if not retired_objects:
                continue
            if identity_sha256 in pinned_identity_sha256s:
                raise CaptureContractError(
                    "retention attempted to retire a pinned capture seal"
                )
            dispositions.append(
                CaptureRetentionDisposition(
                    identity=seal.identity,
                    seal_sha256=seal.seal_sha256,
                    disposition="raw_deleted",
                    disposed_at=now,
                    deleted_objects=retired_objects,
                    resource_binding_sha256=binding.binding_sha256,
                    pin_sha256s_at_disposition=(),
                )
            )

        return CaptureRetentionPlan(
            planned_at=now,
            measurement_sha256=binding.measurement.measurement_sha256,
            policy_sha256=binding.policy.policy_sha256,
            budget_sha256=binding.budget.budget_sha256,
            resource_binding_sha256=binding.binding_sha256,
            raw_cutoff=raw_cutoff,
            derived_cutoff=derived_cutoff,
            pin_sha256s=tuple(pin.pin_sha256 for pin in pins),
            delete_objects=tuple(delete_objects),
            seal_dispositions=tuple(dispositions),
            preserved_sealed_objects=preserved_sealed,
            preserved_pinned_objects=preserved_pinned,
            preserved_young_objects=preserved_young,
        )

    def retention_sweep(self, *, planned_at: datetime) -> dict[str, Any]:
        """Retire old verified raw seals and delete old unpinned owned objects.

        An append-only content-addressed plan is published before deletion.  A
        disposition tombstone is published for every affected exact seal before
        its first object is removed. A corrupt seal, pin, disposition, chunk,
        blob, or derived artifact aborts the pass before deletion.
        """

        self._ownership.assert_valid()
        with self._retention_lock, self._publish_state_lock:
            plan = self._build_retention_plan(planned_at=planned_at)
            plan_bytes = canonical_json_bytes(plan.to_record())
            plan_relative = Path("retention") / "plans" / f"{plan.plan_sha256}.json"
            self._publish(self.root / plan_relative, plan_bytes)

            # Reverify every byte before the first unlink.  The publish lock
            # fences this store's writers for the full verification/delete pass.
            paths: list[Path] = []
            for object_ref in plan.delete_objects:
                path = (self.root / object_ref.relative_path).resolve()
                try:
                    path.relative_to(self.root)
                    content = path.read_bytes()
                except (ValueError, FileNotFoundError) as exc:
                    raise CaptureContractError(
                        "retention candidate changed before deletion"
                    ) from exc
                if path.suffix in {".zst", ".zlib"}:
                    raw = self._decompress(path, content)
                    actual_sha256 = hashlib.sha256(raw).hexdigest()
                else:
                    actual_sha256 = hashlib.sha256(content).hexdigest()
                if (
                    actual_sha256 != object_ref.sha256
                    or len(content) != object_ref.bytes
                ):
                    raise CaptureContractError(
                        "retention candidate content changed before deletion"
                    )
                paths.append(path)

            disposition_paths: list[str] = []
            for disposition in plan.seal_dispositions:
                current = self._read_exact_seal(disposition.identity)
                if current.seal_sha256 != disposition.seal_sha256:
                    raise CaptureContractError(
                        "retention disposition seal changed before deletion"
                    )
                disposition_paths.append(
                    self._put_retention_disposition(disposition)
                )

            deleted_bytes = 0
            for path, object_ref in zip(paths, plan.delete_objects):
                self._ownership.assert_valid()
                path.unlink()
                self._dirty_paths.discard(path)
                self._tracked_root_bytes -= object_ref.bytes
                deleted_bytes += object_ref.bytes
            return {
                "plan_sha256": plan.plan_sha256,
                "plan_path": plan_relative.as_posix(),
                "resource_hashes": self.resource_binding.hashes,
                "raw_cutoff": _iso(plan.raw_cutoff),
                "derived_cutoff": _iso(plan.derived_cutoff),
                "pin_sha256s": plan.pin_sha256s,
                "deleted_objects": len(paths),
                "deleted_bytes": deleted_bytes,
                "retired_seals": len(plan.seal_dispositions),
                "retention_disposition_paths": tuple(disposition_paths),
                "preserved_sealed_objects": plan.preserved_sealed_objects,
                "preserved_pinned_objects": plan.preserved_pinned_objects,
                "preserved_young_objects": plan.preserved_young_objects,
            }

    def _read_chunk_rows(self, path: Path) -> list[Mapping[str, Any]]:
        path = path.resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise CaptureContractError("capture chunk escaped capture root") from exc
        try:
            raw = self._decompress(path, path.read_bytes())
        except FileNotFoundError as exc:
            raise CaptureContractError(f"capture chunk disappeared: {path}") from exc
        digest = path.name.split(".", 1)[0]
        if hashlib.sha256(raw).hexdigest() != digest:
            raise CaptureContractError(f"capture chunk content mismatch: {path}")
        rows: list[Mapping[str, Any]] = []
        for line in raw.splitlines():
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CaptureContractError(
                    f"capture chunk contains invalid JSON: {path}"
                ) from exc
            if not isinstance(value, Mapping):
                raise CaptureContractError(f"capture chunk row is not a mapping: {path}")
            rows.append(value)
        return rows

    def _partition_identity(
        self, path: Path, *, kind: str
    ) -> tuple[str, str, int]:
        try:
            parts = path.resolve().relative_to(self.root).parts
        except ValueError as exc:
            raise CaptureContractError("capture partition escaped capture root") from exc
        if (
            len(parts) != 5
            or parts[0] != kind
            or not parts[1].startswith("date=")
            or not parts[2].startswith("run=")
            or not parts[3].startswith("generation=")
        ):
            raise CaptureContractError(f"invalid {kind} capture partition: {path}")
        day = parts[1].removeprefix("date=")
        run_id = parts[2].removeprefix("run=")
        try:
            generation = int(parts[3].removeprefix("generation="))
        except ValueError as exc:
            raise CaptureContractError(f"invalid capture generation partition: {path}") from exc
        return day, run_id, generation

    def load_events(self) -> tuple[CaptureEvent, ...]:
        """Diagnostic broad scan; never a certifying replay input loader."""

        events: list[CaptureEvent] = []
        seen_sequences: dict[tuple[str, int], str] = {}
        payload_cache: dict[str, dict[str, Mapping[str, Any]]] = {}
        payload_references: dict[str, dict[str, int]] = {}
        paths = tuple((self.root / "events").rglob("*.jsonl.zlib")) + tuple(
            (self.root / "events").rglob("*.jsonl.zst")
        )
        for path in sorted(paths):
            partition_day, partition_run, partition_generation = (
                self._partition_identity(path, kind="events")
            )
            for row in self._read_chunk_rows(path):
                payload_ref = row.get("payload_ref")
                if payload_ref is not None:
                    payload = self._payload_from_record(
                        row,
                        blob_references=payload_references,
                        payload_cache=payload_cache,
                    )
                else:
                    payload = row.get("payload")
                    if not isinstance(payload, Mapping):
                        raise CaptureContractError(
                            "event has neither an inline payload nor a payload ref"
                        )
                event = CaptureEvent.from_record(row, payload=payload)
                if event.event_sha256 != str(row.get("event_sha256") or ""):
                    raise CaptureContractError("event content address mismatch")
                if (
                    event.clocks.available_at.date().isoformat() != partition_day
                    or event.identity.run_id != partition_run
                    or event.identity.generation != partition_generation
                ):
                    raise CaptureContractError("event row/partition identity mismatch")
                sequence_key = (event.identity.identity_sha256, event.sequence)
                prior = seen_sequences.get(sequence_key)
                if prior is not None:
                    raise CaptureContractError(
                        "duplicate capture sequence across immutable chunks"
                    )
                seen_sequences[sequence_key] = event.event_sha256
                events.append(event)
        return tuple(
            sorted(
                events,
                key=lambda event: (
                    event.clocks.available_at,
                    event.sequence,
                    event.event_sha256,
                ),
            )
        )

    def load_gaps(self) -> tuple[tuple[CaptureRunIdentity, CoverageGap], ...]:
        """Diagnostic broad scan; never a certifying replay input loader."""

        gaps: list[tuple[CaptureRunIdentity, CoverageGap]] = []
        paths = tuple((self.root / "gaps").rglob("*.jsonl.zlib")) + tuple(
            (self.root / "gaps").rglob("*.jsonl.zst")
        )
        for path in sorted(paths):
            partition_day, partition_run, partition_generation = (
                self._partition_identity(path, kind="gaps")
            )
            for row in self._read_chunk_rows(path):
                if row.get("schema_version") != CAPTURE_SCHEMA_VERSION:
                    raise CaptureContractError("unsupported gap schema version")
                identity = CaptureRunIdentity.from_dict(
                    row.get("identity") if isinstance(row.get("identity"), Mapping) else {}
                )
                raw = row.get("gap")
                if not isinstance(raw, Mapping):
                    raise CaptureContractError("gap row is missing payload")
                gap = CoverageGap(
                    stream=CaptureStream(str(raw.get("stream") or "")),
                    reason=str(raw.get("reason") or ""),
                    first_available_at=_parse_utc(
                        raw.get("first_available_at"), "gap.first_available_at"
                    ),
                    last_available_at=_parse_utc(
                        raw.get("last_available_at"), "gap.last_available_at"
                    ),
                    lost_count=int(raw.get("lost_count") or 0),
                    symbol=raw.get("symbol"),
                )
                if (
                    gap.first_available_at.date().isoformat() != partition_day
                    or identity.run_id != partition_run
                    or identity.generation != partition_generation
                ):
                    raise CaptureContractError("gap row/partition identity mismatch")
                gaps.append((identity, gap))
        return tuple(
            sorted(
                gaps,
                key=lambda row: (
                    row[0].identity_sha256,
                    row[1].first_available_at,
                    row[1].last_available_at,
                    row[1].stream.value,
                    row[1].symbol or "",
                    row[1].reason,
                ),
            )
        )

    def _exact_chunk_paths(
        self, identity: CaptureRunIdentity, *, kind: str
    ) -> tuple[Path, ...]:
        if kind not in {"events", "gaps"}:
            raise CaptureContractError("sealed chunk kind must be events or gaps")
        pattern = (
            f"date=*/run={identity.run_id}/generation={identity.generation}/*"
        )
        paths: list[Path] = []
        for path in (self.root / kind).glob(pattern):
            if not path.is_file() or not (
                path.name.endswith(".jsonl.zst")
                or path.name.endswith(".jsonl.zlib")
            ):
                raise CaptureContractError(
                    f"unexpected object in exact {kind} run partition: {path}"
                )
            paths.append(path.resolve())
        return tuple(sorted(paths))

    def _object_material(self, path: Path) -> tuple[bytes, bytes]:
        path = path.resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise CaptureContractError("sealed object escaped capture root") from exc
        try:
            compressed = path.read_bytes()
        except FileNotFoundError as exc:
            raise CaptureContractError(f"sealed capture object is missing: {path}") from exc
        raw = self._decompress(path, compressed)
        expected = path.name.split(".", 1)[0]
        if hashlib.sha256(raw).hexdigest() != expected:
            raise CaptureContractError(f"sealed capture object hash mismatch: {path}")
        return raw, compressed

    def _payload_from_record(
        self,
        row: Mapping[str, Any],
        *,
        blob_references: dict[str, dict[str, int]],
        payload_cache: dict[str, dict[str, Mapping[str, Any]]],
    ) -> Mapping[str, Any]:
        expected_payload = _validated_sha256(
            row.get("payload_sha256"), "event payload_sha256"
        )
        payload_ref = row.get("payload_ref")
        if payload_ref is None:
            payload = row.get("payload")
            if not isinstance(payload, Mapping):
                raise CaptureContractError(
                    "event has neither an inline payload nor a payload ref"
                )
            return payload

        relative = _validated_relative_path(payload_ref, "event payload_ref")
        layout, path, _physical_digest = self._payload_reference_path(
            relative, expected_payload_sha256=expected_payload
        )
        cached = payload_cache.get(relative)
        if cached is None:
            if layout == "pack":
                if not path.is_file():
                    raise CaptureContractError(
                        f"payload blob unavailable: {expected_payload}"
                    )
                cached, _raw, _compressed, _policy_sha256 = (
                    self._load_payload_pack(path)
                )
            else:
                payload = self.get_payload(
                    expected_payload, relative_path=relative
                )
                cached = {expected_payload: payload}
            payload_cache[relative] = cached
            while (
                len(payload_cache)
                > self.storage_policy.pack_read_cache_entries
            ):
                oldest = next(iter(payload_cache))
                if oldest == relative and len(payload_cache) == 1:
                    break
                payload_cache.pop(oldest)
        payload = cached.get(expected_payload)
        if payload is None:
            raise CaptureContractError(
                f"payload blob unavailable in packed object: {expected_payload}"
            )
        references = blob_references.setdefault(relative, {})
        references[expected_payload] = references.get(expected_payload, 0) + 1
        return payload

    @staticmethod
    def _gap_from_row(
        row: Mapping[str, Any],
    ) -> tuple[CaptureRunIdentity, CoverageGap]:
        if row.get("schema_version") != CAPTURE_SCHEMA_VERSION:
            raise CaptureContractError("unsupported gap schema version")
        identity_raw = row.get("identity")
        gap_raw = row.get("gap")
        if not isinstance(identity_raw, Mapping) or not isinstance(gap_raw, Mapping):
            raise CaptureContractError("gap row identity/payload is malformed")
        identity = CaptureRunIdentity.from_dict(identity_raw)
        try:
            stream = CaptureStream(str(gap_raw.get("stream") or ""))
        except ValueError as exc:
            raise CaptureContractError("gap row stream is unknown") from exc
        gap = CoverageGap(
            stream=stream,
            reason=str(gap_raw.get("reason") or ""),
            first_available_at=_parse_utc(
                gap_raw.get("first_available_at"), "gap.first_available_at"
            ),
            last_available_at=_parse_utc(
                gap_raw.get("last_available_at"), "gap.last_available_at"
            ),
            lost_count=int(gap_raw.get("lost_count") or 0),
            symbol=gap_raw.get("symbol"),
        )
        return identity, gap

    def _inventory_exact_run(
        self,
        identity: CaptureRunIdentity,
        *,
        close_proof: CaptureLifecycleCloseProof,
    ) -> SealedCaptureRun:
        if not isinstance(identity, CaptureRunIdentity):
            raise CaptureContractError(
                "certifying capture load requires one exact CaptureRunIdentity"
            )

        objects: list[SealedCaptureObjectRef] = []
        events: list[CaptureEvent] = []
        gaps: list[tuple[CaptureRunIdentity, CoverageGap]] = []
        blob_references: dict[str, dict[str, int]] = {}
        payload_cache: dict[str, dict[str, Mapping[str, Any]]] = {}
        seen_sequences: dict[int, str] = {}
        event_accumulator_sha256 = _EMPTY_ACCUMULATOR_SHA256
        gap_accumulator_sha256 = _EMPTY_ACCUMULATOR_SHA256

        for path in self._exact_chunk_paths(identity, kind="events"):
            partition_day, partition_run, partition_generation = (
                self._partition_identity(path, kind="events")
            )
            if (
                partition_run != identity.run_id
                or partition_generation != identity.generation
            ):
                raise CaptureContractError("event seal selected the wrong run partition")
            raw, compressed = self._object_material(path)
            rows = self._read_chunk_rows(path)
            chunk_sequences: list[int] = []
            for row in rows:
                payload = self._payload_from_record(
                    row,
                    blob_references=blob_references,
                    payload_cache=payload_cache,
                )
                event = CaptureEvent.from_record(row, payload=payload)
                if event.event_sha256 != str(row.get("event_sha256") or ""):
                    raise CaptureContractError("event content address mismatch")
                if event.identity != identity:
                    raise CaptureContractError("event identity does not match exact run seal")
                if event.clocks.available_at.date().isoformat() != partition_day:
                    raise CaptureContractError("event row/partition date mismatch")
                prior = seen_sequences.get(event.sequence)
                if prior is not None:
                    raise CaptureContractError(
                        "duplicate capture sequence across sealed chunks"
                    )
                seen_sequences[event.sequence] = event.event_sha256
                event_accumulator_sha256 = _event_accumulator_add(
                    event_accumulator_sha256, event
                )
                chunk_sequences.append(event.sequence)
                events.append(event)
            if not chunk_sequences:
                raise CaptureContractError("sealed event chunk cannot be empty")
            objects.append(
                SealedCaptureObjectRef(
                    kind="event_chunk",
                    relative_path=path.relative_to(self.root).as_posix(),
                    sha256=hashlib.sha256(raw).hexdigest(),
                    record_count=len(rows),
                    reference_count=1,
                    raw_bytes=len(raw),
                    compressed_bytes=len(compressed),
                    sequence_min=min(chunk_sequences),
                    sequence_max=max(chunk_sequences),
                )
            )

        for path in self._exact_chunk_paths(identity, kind="gaps"):
            partition_day, partition_run, partition_generation = (
                self._partition_identity(path, kind="gaps")
            )
            if (
                partition_run != identity.run_id
                or partition_generation != identity.generation
            ):
                raise CaptureContractError("gap seal selected the wrong run partition")
            raw, compressed = self._object_material(path)
            rows = self._read_chunk_rows(path)
            for row in rows:
                row_identity, gap = self._gap_from_row(row)
                if row_identity != identity:
                    raise CaptureContractError("gap identity does not match exact run seal")
                if gap.first_available_at.date().isoformat() != partition_day:
                    raise CaptureContractError("gap row/partition date mismatch")
                gap_accumulator_sha256 = _gap_accumulator_add(
                    gap_accumulator_sha256, row_identity, gap
                )
                gaps.append((row_identity, gap))
            if not rows:
                raise CaptureContractError("sealed gap chunk cannot be empty")
            objects.append(
                SealedCaptureObjectRef(
                    kind="gap_chunk",
                    relative_path=path.relative_to(self.root).as_posix(),
                    sha256=hashlib.sha256(raw).hexdigest(),
                    record_count=len(rows),
                    reference_count=1,
                    raw_bytes=len(raw),
                    compressed_bytes=len(compressed),
                )
            )

        for relative, digest_counts in sorted(blob_references.items()):
            path = (self.root / relative).resolve()
            try:
                path.relative_to(self.root)
            except ValueError as exc:
                raise CaptureContractError("payload ref escaped capture root") from exc
            any_digest = next(iter(digest_counts))
            layout, validated_path, physical_digest = self._payload_reference_path(
                relative, expected_payload_sha256=any_digest
            )
            if validated_path != path:
                raise CaptureContractError("payload path resolution changed")
            if layout == "pack":
                payloads, raw, compressed, _policy_sha256 = (
                    self._load_payload_pack(path)
                )
                if any(digest not in payloads for digest in digest_counts):
                    raise CaptureContractError(
                        "sealed payload pack is missing a referenced payload"
                    )
                kind = "payload_pack"
                digest = physical_digest
                record_count = len(payloads)
            else:
                if len(digest_counts) != 1:
                    raise CaptureContractError(
                        "standalone payload path aliases multiple content hashes"
                    )
                digest = any_digest
                raw, compressed = self._object_material(path)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CaptureContractError(
                        "sealed payload blob is invalid JSON"
                    ) from exc
                if not isinstance(payload, Mapping):
                    raise CaptureContractError(
                        "sealed payload blob must be a mapping"
                    )
                if canonical_json_bytes(payload) != raw:
                    raise CaptureContractError(
                        "sealed payload blob is not canonical JSON"
                    )
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise CaptureContractError(
                        "sealed payload blob content mismatch"
                    )
                kind = "payload_blob"
                record_count = 1
            objects.append(
                SealedCaptureObjectRef(
                    kind=kind,
                    relative_path=relative,
                    sha256=digest,
                    record_count=record_count,
                    reference_count=sum(digest_counts.values()),
                    raw_bytes=len(raw),
                    compressed_bytes=len(compressed),
                )
            )

        ordered_objects = tuple(
            sorted(objects, key=lambda row: (row.kind, row.relative_path))
        )
        sequences = tuple(seen_sequences)
        sequence_min = min(sequences) if sequences else None
        sequence_max = max(sequences) if sequences else None
        gap_lost_count = sum(gap.lost_count for _row_identity, gap in gaps)
        resource_hashes = (
            self.resource_binding.hashes
            if self.resource_binding is not None
            else None
        )
        content_root = sha256_json(
            _run_content_root_payload(
                identity=identity,
                close_proof=close_proof,
                close_proof_sha256=close_proof.proof_sha256,
                objects=ordered_objects,
                event_count=len(events),
                gap_count=len(gaps),
                gap_lost_count=gap_lost_count,
                event_accumulator_sha256=event_accumulator_sha256,
                gap_accumulator_sha256=gap_accumulator_sha256,
                sequence_min=sequence_min,
                sequence_max=sequence_max,
                resource_hashes=resource_hashes,
            )
        )
        seal = CaptureRunSeal(
            identity=identity,
            close_proof=close_proof,
            close_proof_sha256=close_proof.proof_sha256,
            objects=ordered_objects,
            event_count=len(events),
            gap_count=len(gaps),
            gap_lost_count=gap_lost_count,
            event_accumulator_sha256=event_accumulator_sha256,
            gap_accumulator_sha256=gap_accumulator_sha256,
            sequence_min=sequence_min,
            sequence_max=sequence_max,
            resource_measurement_sha256=(
                resource_hashes["measurement_sha256"]
                if resource_hashes is not None
                else None
            ),
            resource_policy_sha256=(
                resource_hashes["policy_sha256"]
                if resource_hashes is not None
                else None
            ),
            resource_budget_sha256=(
                resource_hashes["budget_sha256"]
                if resource_hashes is not None
                else None
            ),
            resource_binding_sha256=(
                resource_hashes["binding_sha256"]
                if resource_hashes is not None
                else None
            ),
            content_root_sha256=content_root,
            schema_version=(
                CAPTURE_RUN_SEAL_SCHEMA_VERSION
                if resource_hashes is not None
                else "chili-replay-capture-run-seal-v3"
            ),
        )
        return SealedCaptureRun(
            seal=seal,
            events=tuple(
                sorted(
                    events,
                    key=lambda event: (
                        event.clocks.available_at,
                        event.sequence,
                        event.event_sha256,
                    ),
                )
            ),
            gaps=tuple(
                sorted(
                    gaps,
                    key=lambda row: (
                        row[1].first_available_at,
                        row[1].last_available_at,
                        row[1].stream.value,
                        row[1].symbol or "",
                        row[1].reason,
                    ),
                )
            ),
        )

    def _seal_directory(self, identity: CaptureRunIdentity) -> Path:
        return (
            self.root
            / "seals"
            / f"run={identity.run_id}"
            / f"generation={identity.generation}"
        )

    def _seal_paths(self, identity: CaptureRunIdentity) -> tuple[Path, ...]:
        paths = tuple(self._seal_directory(identity).glob("*.json"))
        if any(not path.is_file() for path in paths):
            raise CaptureContractError("exact capture seal path is not a file")
        return tuple(
            sorted(path.resolve() for path in paths)
        )

    def _sync_sealed_objects(self, seal: CaptureRunSeal) -> int:
        """Fsync every not-yet-durable listed physical object.

        Objects published and flushed by this store's stopped writer are
        already durable and are not flushed a second time. Objects reused from
        an earlier process are not in the in-memory durable set and therefore
        still receive an explicit fsync before the seal can be published.
        """

        self._ownership.assert_valid()
        synced = 0
        started = float(self._monotonic_clock())
        with self._sync_lock:
            self._sync_calls += 1
            for object_ref in seal.objects:
                path = (self.root / object_ref.relative_path).resolve()
                try:
                    path.relative_to(self.root)
                    if path in self._durable_paths:
                        continue
                    with path.open("rb+") as handle:
                        os.fsync(handle.fileno())
                except FileNotFoundError as exc:
                    self._sync_failures += 1
                    self._record_resource_failure(
                        "capture_object_missing_before_sync"
                    )
                    raise CaptureContractError(
                        f"sealed capture object disappeared before sync: {path}"
                    ) from exc
                except OSError as exc:
                    self._sync_failures += 1
                    self._record_resource_failure("capture_fsync_failed")
                    raise CaptureContractError(
                        f"sealed capture object fsync failed: {path}: {exc}"
                    ) from exc
                self._durable_paths.add(path)
                self._synced_objects += 1
                synced += 1
            finished = float(self._monotonic_clock())
            if not math.isfinite(started) or not math.isfinite(finished):
                self._record_resource_failure("capture_sync_clock_non_finite")
                raise CaptureContractError(
                    "capture sync clock returned a non-finite value"
                )
            self._sync_seconds += max(0.0, finished - started)
        return synced

    def _read_exact_seal(self, identity: CaptureRunIdentity) -> CaptureRunSeal:
        paths = self._seal_paths(identity)
        if not paths:
            raise CaptureContractError(
                "exact capture run is unsealed; broad diagnostic scans cannot certify replay"
            )
        if len(paths) != 1:
            raise CaptureContractError("multiple conflicting seals exist for exact run")
        path = paths[0]
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise CaptureContractError("exact capture run seal disappeared") from exc
        if hashlib.sha256(raw).hexdigest() != path.stem:
            raise CaptureContractError("capture run seal content address mismatch")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CaptureContractError("capture run seal is invalid JSON") from exc
        if not isinstance(value, Mapping) or canonical_json_bytes(value) != raw:
            raise CaptureContractError("capture run seal is not canonical JSON")
        seal = CaptureRunSeal.from_record(value)
        if seal.seal_sha256 != path.stem:
            raise CaptureContractError("capture run seal hash mismatch")
        if seal.identity != identity:
            raise CaptureContractError("capture run seal identity mismatch")
        if seal.resource_hashes is not None:
            if self.resource_binding is None:
                raise CaptureContractError(
                    "resource-bound capture seal requires its exact runtime binding"
                )
            if seal.resource_hashes != self.resource_binding.hashes:
                raise CaptureContractError(
                    "capture run seal resource binding does not match this store"
                )
        return seal

    def seal_run(
        self,
        identity: CaptureRunIdentity,
        *,
        lifecycle: "CaptureWriterWorker | CaptureWriterPool | None" = None,
    ) -> CaptureRunSeal:
        """Verify, fsync, and append one deterministic seal for an exact run.

        Object files are fsynced before the seal file and the seal file is then
        fsynced separately. This deliberately makes no parent-directory fsync
        portability claim. A caller cannot supply a close-proof record: the
        proof must be derived from the stopped worker lifecycle bound to this
        store, and is reconciled against the exact object inventory.
        """

        if not isinstance(identity, CaptureRunIdentity):
            raise CaptureContractError("seal_run requires one exact CaptureRunIdentity")
        self._ownership.assert_valid()
        if type(lifecycle) not in (CaptureWriterWorker, CaptureWriterPool):
            raise CaptureContractError(
                "seal_run requires a stopped capture writer lifecycle; "
                "direct inventory sealing cannot certify clean close"
            )
        if lifecycle.store is not self:
            raise CaptureContractError(
                "capture writer lifecycle belongs to a different capture store"
            )
        with self._seal_lock:
            close_proof = lifecycle._finalize_close_proof(identity)
            before = self._inventory_exact_run(identity, close_proof=close_proof)
            self.sync()
            self._sync_sealed_objects(before.seal)
            with self._publish_state_lock:
                after = self._inventory_exact_run(
                    identity, close_proof=close_proof
                )
                if before.seal != after.seal:
                    raise CaptureContractError(
                        "capture run changed while sealing; drain writers and retry"
                    )
                # Avoid a publish-state/sync lock inversion. If a publisher
                # completed after the preceding sync, require a clean retry;
                # the caller should already have drained capture writers.
                if self._dirty_paths:
                    raise CaptureContractError(
                        "capture objects became dirty while sealing; drain writers and retry"
                    )
                existing = self._seal_paths(identity)
                if existing:
                    sealed = self._read_exact_seal(identity)
                    if sealed != after.seal:
                        raise CaptureContractError(
                            "conflicting immutable seal already exists for exact run"
                        )
                    return sealed
                seal_bytes = canonical_json_bytes(after.seal.to_record())
                seal_path = (
                    self._seal_directory(identity)
                    / f"{after.seal.seal_sha256}.json"
                )
                self._publish(seal_path, seal_bytes)
            # The seal is synced only after all listed objects have completed
            # their own sync. Directory durability is platform/filesystem-specific.
            self.sync()
            persisted = self._read_exact_seal(identity)
            if persisted != after.seal:
                raise CaptureContractError("persisted capture run seal changed")
            return persisted

    def _load_exact_sealed_run(self, identity: CaptureRunIdentity) -> SealedCaptureRun:
        """Internal exact inventory load shared by certifying and diagnostic APIs."""

        self._ownership.assert_valid()
        if not isinstance(identity, CaptureRunIdentity):
            raise CaptureContractError(
                "certifying capture load requires one exact CaptureRunIdentity"
            )
        sealed = self._read_exact_seal(identity)
        disposition = next(
            (
                row
                for row in self.load_retention_dispositions()
                if row.seal_sha256 == sealed.seal_sha256
            ),
            None,
        )
        if disposition is not None:
            raise CaptureContractError(
                "capture certification material is irreversibly retired by "
                f"{disposition.disposition_sha256}"
            )
        inventory = self._inventory_exact_run(
            identity, close_proof=sealed.close_proof
        )
        if inventory.seal != sealed:
            raise CaptureContractError(
                "sealed capture object set/counts/content root do not match"
            )
        return SealedCaptureRun(
            seal=sealed,
            events=inventory.events,
            gaps=inventory.gaps,
        )

    def load_sealed_run(
        self,
        identity: CaptureRunIdentity,
        *,
        expected_seal_sha256: str,
    ) -> SealedCaptureRun:
        """Certifying load pinned to one caller-supplied exact final seal SHA.

        The expected digest must come from outside the store scan being loaded;
        selecting whichever seal happens to be present is diagnostic behavior
        and cannot establish certification provenance.
        """

        expected = _validated_sha256(
            expected_seal_sha256, "expected_seal_sha256"
        )
        loaded = self._load_exact_sealed_run(identity)
        if loaded.seal.seal_sha256 != expected:
            raise CaptureContractError(
                "loaded capture seal does not match expected SHA"
            )
        sequences = sorted(event.sequence for event in loaded.events)
        if not sequences or sequences[0] != 1 or sequences != list(
            range(1, sequences[-1] + 1)
        ):
            raise CaptureContractError(
                "certifying sealed inventory is not contiguous from sequence 1"
            )
        return loaded

    def load_sealed_run_diagnostic(
        self, identity: CaptureRunIdentity
    ) -> SealedCaptureRun:
        """Exact but noncertifying load without an independently pinned seal SHA."""

        return self._load_exact_sealed_run(identity)


@dataclass(frozen=True)
class _ReadOnlyPayloadCachePolicy:
    """Only the bounded cache setting needed while verifying packed payloads."""

    pack_read_cache_entries: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.pack_read_cache_entries, bool)
            or int(self.pack_read_cache_entries) <= 0
        ):
            raise CaptureContractError(
                "read-only payload pack cache entries must be positive"
            )
        object.__setattr__(
            self, "pack_read_cache_entries", int(self.pack_read_cache_entries)
        )


class ReadOnlyV4CaptureStore:
    """Non-mutating exact-run verifier for one externally pinned v4 capture.

    Construction deliberately does not create directories, acquire store
    ownership, publish receipts/audits, repair content, or initialize any
    writer state.  The only public load is pre-bound to an exact identity,
    caller-supplied final seal digest, measured resource binding, and replay
    coverage request.  Coverage completeness is still graded separately from
    the resulting :class:`VerifiedReplayCapture`; merely loading durable bytes
    never turns missing evidence into a pass.
    """

    # Reuse the mature byte-level verification routines without inheriting the
    # mutable store API.  These methods only read paths under ``root``.
    _decompress = staticmethod(ContentAddressedCaptureStore._decompress)
    _payload_reference_path = ContentAddressedCaptureStore._payload_reference_path
    _load_payload_pack = ContentAddressedCaptureStore._load_payload_pack
    _read_chunk_rows = ContentAddressedCaptureStore._read_chunk_rows
    _partition_identity = ContentAddressedCaptureStore._partition_identity
    _exact_chunk_paths = ContentAddressedCaptureStore._exact_chunk_paths
    _object_material = ContentAddressedCaptureStore._object_material
    _payload_from_record = ContentAddressedCaptureStore._payload_from_record
    _gap_from_row = staticmethod(ContentAddressedCaptureStore._gap_from_row)
    _inventory_exact_run = ContentAddressedCaptureStore._inventory_exact_run
    _seal_directory = ContentAddressedCaptureStore._seal_directory
    _seal_paths = ContentAddressedCaptureStore._seal_paths
    _read_exact_seal = ContentAddressedCaptureStore._read_exact_seal
    load_retention_dispositions = (
        ContentAddressedCaptureStore.load_retention_dispositions
    )

    def __init__(
        self,
        root: str | Path,
        *,
        identity: CaptureRunIdentity,
        expected_seal_sha256: str,
        expected_resource_binding: CaptureResourceBinding,
        coverage_request: ReplayCoverageRequest,
        payload_pack_read_cache_entries: int = 4,
    ) -> None:
        if not isinstance(identity, CaptureRunIdentity):
            raise CaptureContractError(
                "read-only certifying load requires one exact CaptureRunIdentity"
            )
        if not isinstance(expected_resource_binding, CaptureResourceBinding):
            raise CaptureContractError(
                "read-only certifying load requires an exact resource binding"
            )
        if not isinstance(coverage_request, ReplayCoverageRequest):
            raise CaptureContractError(
                "read-only certifying load requires a replay coverage request"
            )
        if coverage_request.expected_identity_sha256 is None:
            raise CaptureContractError(
                "read-only coverage request must pin expected_identity_sha256"
            )
        if coverage_request.expected_identity_sha256 != identity.identity_sha256:
            raise CaptureContractError(
                "read-only coverage request identity does not match exact run"
            )
        resolved_root = Path(root).resolve()
        if not resolved_root.is_dir():
            raise CaptureContractError(
                "read-only capture root must already exist as a directory"
            )
        self.root = resolved_root
        self.identity = identity
        self.expected_seal_sha256 = _validated_sha256(
            expected_seal_sha256, "expected_seal_sha256"
        )
        self.resource_binding = expected_resource_binding
        self.coverage_request = coverage_request
        self.storage_policy = _ReadOnlyPayloadCachePolicy(
            payload_pack_read_cache_entries
        )

    def get_payload(
        self, sha256: str, *, relative_path: str | None = None
    ) -> Mapping[str, Any]:
        # A certifying read must follow the exact relative object reference
        # committed by an event row; it must never discover a convenient blob.
        if relative_path is None:
            raise CaptureContractError(
                "read-only certifying payload load requires an exact relative path"
            )
        return ContentAddressedCaptureStore.get_payload(
            self, sha256, relative_path=relative_path
        )

    def _retention_disposition_for(
        self, sealed: CaptureRunSeal
    ) -> CaptureRetentionDisposition | None:
        return next(
            (
                row
                for row in self.load_retention_dispositions()
                if row.seal_sha256 == sealed.seal_sha256
            ),
            None,
        )

    def _load_exact_sealed_run(self) -> SealedCaptureRun:
        sealed = self._read_exact_seal(self.identity)
        if sealed.schema_version != CAPTURE_RUN_SEAL_SCHEMA_VERSION:
            raise CaptureContractError(
                "read-only certification requires a resource-bound v4 capture seal"
            )
        resource_hashes = sealed.resource_hashes
        if resource_hashes is None:
            raise CaptureContractError(
                "read-only certification requires all four resource hashes"
            )
        for name, expected in self.resource_binding.hashes.items():
            if resource_hashes.get(name) != expected:
                raise CaptureContractError(
                    f"capture run seal {name} does not match expected resource binding"
                )
        disposition = self._retention_disposition_for(sealed)
        if disposition is not None:
            raise CaptureContractError(
                "capture certification material is irreversibly retired by "
                f"{disposition.disposition_sha256}"
            )
        inventory = self._inventory_exact_run(
            self.identity, close_proof=sealed.close_proof
        )
        if inventory.seal != sealed:
            raise CaptureContractError(
                "sealed capture object set/counts/content root do not match"
            )
        # Re-read the exact seal and retirement ledger after the object walk so
        # a concurrent replacement/retirement cannot be accepted mid-load.
        if self._read_exact_seal(self.identity) != sealed:
            raise CaptureContractError("capture run seal changed during read-only load")
        disposition = self._retention_disposition_for(sealed)
        if disposition is not None:
            raise CaptureContractError(
                "capture certification material is irreversibly retired by "
                f"{disposition.disposition_sha256}"
            )
        return SealedCaptureRun(
            seal=sealed,
            events=inventory.events,
            gaps=inventory.gaps,
        )

    def load_sealed_run(
        self,
        identity: CaptureRunIdentity,
        *,
        expected_seal_sha256: str,
    ) -> SealedCaptureRun:
        """Load only the identity and final digest pinned at construction."""

        if identity != self.identity:
            raise CaptureContractError(
                "read-only certifying load identity differs from pinned identity"
            )
        expected = _validated_sha256(
            expected_seal_sha256, "expected_seal_sha256"
        )
        if expected != self.expected_seal_sha256:
            raise CaptureContractError(
                "read-only certifying load seal differs from pinned seal"
            )
        loaded = self._load_exact_sealed_run()
        if loaded.seal.seal_sha256 != expected:
            raise CaptureContractError(
                "loaded capture seal does not match expected SHA"
            )
        sequences = sorted(event.sequence for event in loaded.events)
        if not sequences or sequences[0] != 1 or sequences != list(
            range(1, sequences[-1] + 1)
        ):
            raise CaptureContractError(
                "certifying sealed inventory is not contiguous from sequence 1"
            )
        return loaded


def load_verified_replay_capture_v4(
    root: str | Path,
    identity: CaptureRunIdentity,
    *,
    expected_final_seal_sha256: str,
    expected_resource_binding: CaptureResourceBinding,
    coverage_request: ReplayCoverageRequest,
    payload_pack_read_cache_entries: int = 4,
) -> VerifiedReplayCapture:
    """Certify one local v4 capture without opening a mutable capture store."""

    store = ReadOnlyV4CaptureStore(
        root,
        identity=identity,
        expected_seal_sha256=expected_final_seal_sha256,
        expected_resource_binding=expected_resource_binding,
        coverage_request=coverage_request,
        payload_pack_read_cache_entries=payload_pack_read_cache_entries,
    )
    verified = VerifiedReplayCapture.load_sealed_run(
        store,
        identity,
        expected_final_seal_sha256=expected_final_seal_sha256,
    )
    if verified.resource_hashes != expected_resource_binding.hashes:
        raise CaptureContractError(
            "verified replay capture resource hashes changed after exact store load"
        )
    return verified


class CaptureWriterWorker:
    """Single batched writer; producers remain non-blocking and bounded."""

    def __init__(
        self,
        *,
        ingress: BoundedCaptureIngress,
        store: ContentAddressedCaptureStore,
        batch_events: int,
        batch_bytes: int,
        poll_seconds: float = 0.1,
        flush_interval_seconds: float = 5.0,
    ) -> None:
        if (
            min(batch_events, batch_bytes) <= 0
            or poll_seconds <= 0
            or flush_interval_seconds <= 0
        ):
            raise CaptureContractError("writer batch settings must be positive")
        if ingress.resource_binding != store.resource_binding:
            raise CaptureContractError(
                "capture writer ingress/store resource bindings do not match"
            )
        if (
            ingress.resource_binding is not None
            and int(batch_bytes) > ingress.resource_binding.budget.async_queue_bytes
        ):
            raise CaptureContractError(
                "capture writer batch exceeds resolved async queue byte budget"
            )
        self.ingress = ingress
        self.store = store
        self.batch_events = int(batch_events)
        self.batch_bytes = int(batch_bytes)
        self.poll_seconds = float(poll_seconds)
        self.flush_interval_seconds = float(flush_interval_seconds)
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._has_started = False
        self._stop = threading.Event()
        self._last_error: str | None = None
        self._event_chunks = 0
        self._gap_chunks = 0
        self._events_written = 0
        self._event_accumulator_sha256 = _EMPTY_ACCUMULATOR_SHA256
        self._gap_records_written = 0
        self._gaps_written = 0
        self._gap_accumulator_sha256 = _EMPTY_ACCUMULATOR_SHA256
        self._started_at: float | None = None
        self._stopped_cleanly = False

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._has_started:
                raise CaptureContractError("capture writer is one-shot")
            if self.ingress.closed:
                raise CaptureContractError("cannot start writer with closed ingress")
            self._has_started = True
            self._stop.clear()
            self._started_at = time.monotonic()
            self._thread = threading.Thread(
                target=self._run,
                name="replay-capture-writer",
                daemon=True,
            )
            self._thread.start()

    def _write(self, batch: IngressBatch) -> None:
        event_refs = self.store.write_events(batch.events)
        gap_refs = self.store.write_gaps(batch.gaps)
        self._event_chunks += len(event_refs)
        self._gap_chunks += len(gap_refs)
        self._events_written += len(batch.events)
        for event in batch.events:
            self._event_accumulator_sha256 = _event_accumulator_add(
                self._event_accumulator_sha256, event
            )
        self._gap_records_written += len(batch.gaps)
        for identity, gap in batch.gaps:
            self._gaps_written += gap.lost_count
            self._gap_accumulator_sha256 = _gap_accumulator_add(
                self._gap_accumulator_sha256, identity, gap
            )
        self.ingress.complete_shared_admission(batch.events)

    def _run(self) -> None:
        try:
            pending_events: list[CaptureEvent] = []
            pending_gaps: list[tuple[CaptureRunIdentity, CoverageGap]] = []
            pending_bytes = 0
            last_flush = time.monotonic()
            while True:
                remaining_events = max(1, self.batch_events - len(pending_events))
                remaining_bytes = max(1, self.batch_bytes - pending_bytes)
                batch = self.ingress.pop_batch(
                    max_events=remaining_events,
                    max_bytes=remaining_bytes,
                    timeout_seconds=self.poll_seconds,
                )
                pending_events.extend(batch.events)
                pending_gaps.extend(batch.gaps)
                pending_bytes += sum(approximate_event_bytes(row) for row in batch.events)
                elapsed = time.monotonic() - last_flush
                stopping = self._stop.is_set() or self.ingress.closed
                should_flush = bool(pending_events or pending_gaps) and (
                    len(pending_events) >= self.batch_events
                    or pending_bytes >= self.batch_bytes
                    or elapsed >= self.flush_interval_seconds
                    or (stopping and self.ingress.drained)
                )
                if should_flush:
                    self._write(
                        IngressBatch(
                            events=tuple(pending_events), gaps=tuple(pending_gaps)
                        )
                    )
                    pending_events.clear()
                    pending_gaps.clear()
                    pending_bytes = 0
                    last_flush = time.monotonic()
                if (
                    stopping
                    and self.ingress.drained
                    and not pending_events
                    and not pending_gaps
                ):
                    self.store.sync()
                    self._stopped_cleanly = self.ingress.clean_close_eligible
                    return
        except Exception as exc:  # fail-closed health is inspected by the owner
            self._last_error = f"{type(exc).__name__}: {exc}"
            try:
                self.ingress.fail_writer(
                    tuple(pending_events),
                    reason=self._last_error,
                )
            except Exception as release_exc:
                self._last_error += (
                    f"; shared admission release failed: "
                    f"{type(release_exc).__name__}: {release_exc}"
                )
            self._stopped_cleanly = False
            self.ingress.close()

    def request_stop(self, *, close_ingress: bool = True) -> None:
        if close_ingress:
            self.ingress.close()
        self._stop.set()
        self.ingress.wake()

    def join(self, *, timeout_seconds: float = 10.0) -> bool:
        thread = self._thread
        if thread is None:
            return False
        thread.join(timeout=max(0.0, timeout_seconds))
        return bool(
            self._stopped_cleanly
            and self.ingress.clean_close_eligible
            and not thread.is_alive()
        )

    def stop(self, *, timeout_seconds: float = 10.0) -> bool:
        self.request_stop(close_ingress=True)
        return self.join(timeout_seconds=timeout_seconds)

    def _finalize_close_proof(
        self, identity: CaptureRunIdentity
    ) -> CaptureLifecycleCloseProof:
        thread = self._thread
        if (
            not self._has_started
            or thread is None
            or thread.is_alive()
            or not self._stopped_cleanly
            or self._last_error is not None
        ):
            raise CaptureContractError(
                "capture writer has not completed a clean, error-free shutdown"
            )
        ingress = self.ingress.finalize_clean_close(identity)
        return CaptureLifecycleCloseProof(
            identity_sha256=str(ingress["identity_sha256"]),
            writer_count=1,
            writers_started=1,
            writers_stopped_cleanly=1,
            writer_errors=(),
            ingress_submitted=int(ingress["submitted"]),
            ingress_accepted=int(ingress["accepted"]),
            ingress_dropped=int(ingress["dropped"]),
            reported_gap_lost=int(ingress["reported_gap_lost"]),
            accepted_event_accumulator_sha256=str(
                ingress["accepted_event_accumulator_sha256"]
            ),
            gap_records_emitted=int(ingress["gap_records_emitted"]),
            gap_lost_emitted=int(ingress["gap_lost_emitted"]),
            emitted_gap_accumulator_sha256=str(
                ingress["gap_accumulator_sha256"]
            ),
            ingress_closed=bool(ingress["closed"]),
            ingress_finalized=bool(ingress["finalized"]),
            post_close_submissions=int(ingress["post_close_submissions"]),
            queued_events=int(ingress["queued_events"]),
            queued_bytes=int(ingress["queued_bytes"]),
            pending_gap_keys=int(ingress["pending_gap_keys"]),
            submission_sequence_min=int(ingress["sequence_min"]),
            submission_sequence_max=int(ingress["sequence_max"]),
            events_written=self._events_written,
            written_event_accumulator_sha256=self._event_accumulator_sha256,
            gap_records_written=self._gap_records_written,
            lost_events_recorded=self._gaps_written,
            written_gap_accumulator_sha256=self._gap_accumulator_sha256,
            event_chunks_written=self._event_chunks,
            gap_chunks_written=self._gap_chunks,
        )

    def seal_run(self, identity: CaptureRunIdentity) -> CaptureRunSeal:
        return self.store.seal_run(identity, lifecycle=self)

    def health(self) -> dict[str, Any]:
        thread = self._thread
        elapsed = (
            max(0.0, time.monotonic() - self._started_at)
            if self._started_at is not None
            else 0.0
        )
        return {
            "writer_alive": bool(thread and thread.is_alive()),
            "stopped_cleanly": bool(
                self._stopped_cleanly and self.ingress.clean_close_eligible
            ),
            "last_error": self._last_error,
            "event_chunks": self._event_chunks,
            "gap_chunks": self._gap_chunks,
            "events_written": self._events_written,
            "event_accumulator_sha256": self._event_accumulator_sha256,
            "gap_records_written": self._gap_records_written,
            "lost_events_recorded": self._gaps_written,
            "gap_accumulator_sha256": self._gap_accumulator_sha256,
            "elapsed_seconds": elapsed,
            "ingress": self.ingress.health(),
            "resource": self.store.resource_health(self.ingress),
        }


class CaptureWriterPool:
    """Finite parallel writers derived from measured CPU/disk headroom."""

    def __init__(
        self,
        *,
        ingress: BoundedCaptureIngress,
        store: ContentAddressedCaptureStore,
        workers: int,
        batch_events: int,
        batch_bytes: int,
        poll_seconds: float = 0.1,
        flush_interval_seconds: float = 5.0,
    ) -> None:
        if int(workers) <= 0:
            raise CaptureContractError("writer pool size must be positive")
        if ingress.resource_binding != store.resource_binding:
            raise CaptureContractError(
                "capture writer pool ingress/store resource bindings do not match"
            )
        self.ingress = ingress
        self.store = store
        self._lifecycle_lock = threading.Lock()
        self._has_started = False
        self.workers = tuple(
            CaptureWriterWorker(
                ingress=ingress,
                store=store,
                batch_events=batch_events,
                batch_bytes=batch_bytes,
                poll_seconds=poll_seconds,
                flush_interval_seconds=flush_interval_seconds,
            )
            for _ in range(int(workers))
        )

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._has_started:
                raise CaptureContractError("capture writer pool is one-shot")
            if self.ingress.closed:
                raise CaptureContractError("cannot start writer pool with closed ingress")
            self._has_started = True
            for worker in self.workers:
                worker.start()

    def stop(self, *, timeout_seconds: float = 30.0) -> bool:
        self.ingress.close()
        for worker in self.workers:
            worker.request_stop(close_ingress=False)
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        results = []
        for worker in self.workers:
            results.append(
                worker.join(
                    timeout_seconds=max(0.0, deadline - time.monotonic())
                )
            )
        return all(results)

    def _finalize_close_proof(
        self, identity: CaptureRunIdentity
    ) -> CaptureLifecycleCloseProof:
        rows = tuple(worker.health() for worker in self.workers)
        if (
            not self._has_started
            or any(row["writer_alive"] for row in rows)
            or any(not row["stopped_cleanly"] for row in rows)
            or any(row["last_error"] for row in rows)
        ):
            raise CaptureContractError(
                "capture writer pool has not completed a clean, error-free shutdown"
            )
        ingress = self.ingress.finalize_clean_close(identity)
        return CaptureLifecycleCloseProof(
            identity_sha256=str(ingress["identity_sha256"]),
            writer_count=len(rows),
            writers_started=sum(1 for worker in self.workers if worker._has_started),
            writers_stopped_cleanly=sum(
                1 for row in rows if row["stopped_cleanly"]
            ),
            writer_errors=tuple(
                str(row["last_error"]) for row in rows if row["last_error"]
            ),
            ingress_submitted=int(ingress["submitted"]),
            ingress_accepted=int(ingress["accepted"]),
            ingress_dropped=int(ingress["dropped"]),
            reported_gap_lost=int(ingress["reported_gap_lost"]),
            accepted_event_accumulator_sha256=str(
                ingress["accepted_event_accumulator_sha256"]
            ),
            gap_records_emitted=int(ingress["gap_records_emitted"]),
            gap_lost_emitted=int(ingress["gap_lost_emitted"]),
            emitted_gap_accumulator_sha256=str(
                ingress["gap_accumulator_sha256"]
            ),
            ingress_closed=bool(ingress["closed"]),
            ingress_finalized=bool(ingress["finalized"]),
            post_close_submissions=int(ingress["post_close_submissions"]),
            queued_events=int(ingress["queued_events"]),
            queued_bytes=int(ingress["queued_bytes"]),
            pending_gap_keys=int(ingress["pending_gap_keys"]),
            submission_sequence_min=int(ingress["sequence_min"]),
            submission_sequence_max=int(ingress["sequence_max"]),
            events_written=sum(int(row["events_written"]) for row in rows),
            written_event_accumulator_sha256=_merge_sha256_accumulators(
                str(row["event_accumulator_sha256"]) for row in rows
            ),
            gap_records_written=sum(
                int(row["gap_records_written"]) for row in rows
            ),
            lost_events_recorded=sum(
                int(row["lost_events_recorded"]) for row in rows
            ),
            written_gap_accumulator_sha256=_merge_sha256_accumulators(
                str(row["gap_accumulator_sha256"]) for row in rows
            ),
            event_chunks_written=sum(int(row["event_chunks"]) for row in rows),
            gap_chunks_written=sum(int(row["gap_chunks"]) for row in rows),
        )

    def seal_run(self, identity: CaptureRunIdentity) -> CaptureRunSeal:
        return self.store.seal_run(identity, lifecycle=self)

    def health(self) -> dict[str, Any]:
        rows = tuple(worker.health() for worker in self.workers)
        return {
            "worker_count": len(rows),
            "stopped_cleanly": all(row["stopped_cleanly"] for row in rows),
            "last_errors": [row["last_error"] for row in rows if row["last_error"]],
            "event_chunks": sum(int(row["event_chunks"]) for row in rows),
            "gap_chunks": sum(int(row["gap_chunks"]) for row in rows),
            "events_written": sum(int(row["events_written"]) for row in rows),
            "gap_records_written": sum(
                int(row["gap_records_written"]) for row in rows
            ),
            "lost_events_recorded": sum(
                int(row["lost_events_recorded"]) for row in rows
            ),
            "ingress": self.ingress.health(),
            "resource": self.store.resource_health(self.ingress),
            "workers": rows,
        }


class SharedCaptureWriterLease:
    """Opaque one-run lease on the host-wide store and one writer slot."""

    def __init__(
        self,
        *,
        runtime: "SharedCaptureStoreRuntime",
        lease_token: str,
        identity: CaptureRunIdentity,
    ) -> None:
        self._runtime = runtime
        self._lease_token = str(lease_token)
        self.identity = identity
        self._writer: CaptureWriterWorker | None = None
        self._released = False
        self._lock = threading.RLock()

    @property
    def store(self) -> ContentAddressedCaptureStore:
        with self._lock:
            if self._released:
                raise CaptureContractError("shared capture writer lease is released")
            return self._runtime.store

    @property
    def writer(self) -> CaptureWriterWorker | None:
        with self._lock:
            return self._writer

    def build_writer(
        self,
        *,
        ingress: BoundedCaptureIngress,
        batch_events: int,
        batch_bytes: int,
        poll_seconds: float = 0.1,
        flush_interval_seconds: float = 5.0,
    ) -> CaptureWriterWorker:
        """Build exactly one writer; aggregate concurrency is manager-bounded."""

        with self._lock:
            if self._released:
                raise CaptureContractError("shared capture writer lease is released")
            if self._writer is not None:
                raise CaptureContractError("shared capture writer lease is one-shot")
            self._runtime._validate_writer_build(self, ingress)
            writer = CaptureWriterWorker(
                ingress=ingress,
                store=self._runtime.store,
                batch_events=batch_events,
                batch_bytes=batch_bytes,
                poll_seconds=poll_seconds,
                flush_interval_seconds=flush_interval_seconds,
            )
            self._writer = writer
            return writer

    def release(self) -> None:
        """Release only after no queued/inflight work can outlive the lease."""

        with self._lock:
            if self._released:
                return
            writer = self._writer
            if writer is not None:
                writer_health = writer.health()
                outstanding = self._runtime.shared_admission_budget.outstanding_for(
                    self.identity.identity_sha256
                )
                if (
                    writer_health["writer_alive"]
                    or not writer.ingress.drained
                    or outstanding != 0
                    or (writer._has_started and not writer.ingress.closed)
                ):
                    raise CaptureContractError(
                        "cannot release shared capture store with active or reserved writer work"
                    )
            self._runtime._release(self)
            self._released = True

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "identity_sha256": self.identity.identity_sha256,
                "released": self._released,
                "writer_built": self._writer is not None,
                "writer": self._writer.health() if self._writer is not None else None,
            }


class SharedCaptureStoreRuntime:
    """One process-wide durable store, disk quota, and measured writer budget.

    Individual hot-symbol runs receive opaque leases.  A run may stop, seal,
    and release without closing the common store or invalidating another run.
    The owner closes the store explicitly only after every lease is released.
    """

    def __init__(
        self,
        *,
        store: ContentAddressedCaptureStore,
        shared_admission_budget: SharedCaptureAdmissionBudget,
    ) -> None:
        if not isinstance(store, ContentAddressedCaptureStore):
            raise CaptureContractError("shared capture store is malformed")
        binding = store.resource_binding
        if binding is None:
            raise CaptureContractError(
                "shared capture store requires a measured resource binding"
            )
        if not isinstance(shared_admission_budget, SharedCaptureAdmissionBudget):
            raise CaptureContractError("shared capture admission budget is malformed")
        if shared_admission_budget.resource_binding != binding:
            raise CaptureContractError(
                "shared capture store/admission resource bindings do not match"
            )
        self.store = store
        self.shared_admission_budget = shared_admission_budget
        self.resource_binding = binding
        self.max_writer_threads = int(binding.budget.max_writer_threads)
        if self.max_writer_threads <= 0:
            raise CaptureContractError("shared capture writer budget is empty")
        self._leases: dict[str, SharedCaptureWriterLease] = {}
        self._lease_by_identity: dict[str, str] = {}
        self._writer_ingress_owners: dict[BoundedCaptureIngress, str] = {}
        self._closed = False
        self._lock = threading.RLock()

    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        resource_binding: CaptureResourceBinding,
        shared_admission_budget: SharedCaptureAdmissionBudget,
        compression_codec: str = "zstd",
        compression_level: int = 3,
        payload_pack_max_records: int = 2_048,
        payload_pack_target_raw_bytes: int = 8 * 1024 * 1024,
        payload_pack_read_cache_entries: int = 4,
        disk_usage_provider: Callable[[str | os.PathLike[str]], Any] = shutil.disk_usage,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> "SharedCaptureStoreRuntime":
        if shared_admission_budget.resource_binding != resource_binding:
            raise CaptureContractError(
                "shared capture store factory admission binding mismatch"
            )
        store = ContentAddressedCaptureStore(
            root,
            compression_codec=compression_codec,
            compression_level=compression_level,
            payload_pack_max_records=payload_pack_max_records,
            payload_pack_target_raw_bytes=payload_pack_target_raw_bytes,
            payload_pack_read_cache_entries=payload_pack_read_cache_entries,
            resource_binding=resource_binding,
            disk_usage_provider=disk_usage_provider,
            monotonic_clock=monotonic_clock,
            wall_clock=wall_clock,
        )
        try:
            return cls(
                store=store,
                shared_admission_budget=shared_admission_budget,
            )
        except Exception:
            store.close()
            raise

    def acquire(self, identity: CaptureRunIdentity) -> SharedCaptureWriterLease:
        if not isinstance(identity, CaptureRunIdentity):
            raise CaptureContractError("shared capture lease identity is malformed")
        identity_sha256 = identity.identity_sha256
        with self._lock:
            if self._closed:
                raise CaptureContractError("shared capture store runtime is closed")
            if identity_sha256 in self._lease_by_identity:
                raise CaptureContractError(
                    "capture identity already owns a shared writer lease"
                )
            if len(self._leases) >= self.max_writer_threads:
                raise CaptureContractError(
                    "measured aggregate capture writer capacity is exhausted"
                )
            token = str(uuid.uuid4())
            lease = SharedCaptureWriterLease(
                runtime=self,
                lease_token=token,
                identity=identity,
            )
            self._leases[token] = lease
            self._lease_by_identity[identity_sha256] = token
            return lease

    def _validate_writer_build(
        self,
        lease: SharedCaptureWriterLease,
        ingress: BoundedCaptureIngress,
    ) -> None:
        if not isinstance(ingress, BoundedCaptureIngress):
            raise CaptureContractError("shared capture writer ingress is malformed")
        with self._lock:
            active = self._leases.get(lease._lease_token)
            if active is not lease or self._closed:
                raise CaptureContractError("shared capture writer lease is not active")
            if ingress.resource_binding != self.resource_binding:
                raise CaptureContractError(
                    "shared capture writer ingress binding mismatch"
                )
            if ingress.shared_admission_budget is not self.shared_admission_budget:
                raise CaptureContractError(
                    "shared capture writer must use the exact host admission budget"
                )
            existing_owner = self._writer_ingress_owners.get(ingress)
            if existing_owner is not None:
                raise CaptureContractError(
                    "shared capture ingress already belongs to another writer lease"
                )
            ingress._bind_run_identity(lease.identity)
            self._writer_ingress_owners[ingress] = lease._lease_token

    def _release(self, lease: SharedCaptureWriterLease) -> None:
        with self._lock:
            active = self._leases.get(lease._lease_token)
            if active is not lease:
                raise CaptureContractError("shared capture writer lease is foreign")
            identity_sha256 = lease.identity.identity_sha256
            if self._lease_by_identity.get(identity_sha256) != lease._lease_token:
                raise CaptureContractError(
                    "shared capture writer identity ownership is inconsistent"
                )
            self._leases.pop(lease._lease_token)
            self._lease_by_identity.pop(identity_sha256)
            writer = lease.writer
            if writer is not None:
                owner = self._writer_ingress_owners.get(writer.ingress)
                if owner != lease._lease_token:
                    raise CaptureContractError(
                        "shared capture writer ingress ownership is inconsistent"
                    )
                self._writer_ingress_owners.pop(writer.ingress)

    def health(self) -> dict[str, Any]:
        with self._lock:
            active = tuple(
                sorted(
                    lease.identity.identity_sha256
                    for lease in self._leases.values()
                )
            )
            return {
                "closed": self._closed,
                "root": str(self.store.root),
                "resource_hashes": self.resource_binding.hashes,
                "max_writer_threads": self.max_writer_threads,
                "writer_threads_in_use": len(self._leases),
                "writer_threads_available": self.max_writer_threads - len(self._leases),
                "lease_count": len(self._leases),
                "claimed_writer_ingresses": len(self._writer_ingress_owners),
                "active_identity_sha256s": active,
                "shared_admission": self.shared_admission_budget.health(),
                "store": self.store.resource_health(),
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._leases:
                raise CaptureContractError(
                    "cannot close shared capture store with active writer leases"
                )
            shared_health = self.shared_admission_budget.health()
            if int(shared_health["outstanding_events"]) != 0:
                raise CaptureContractError(
                    "cannot close shared capture store with outstanding reservations"
                )
            if self._writer_ingress_owners:
                raise CaptureContractError(
                    "cannot close shared capture store with claimed writer ingresses"
                )
            self.store.sync()
            self.store.close()
            self._closed = True

    def __enter__(self) -> "SharedCaptureStoreRuntime":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.close()
        return False


class ReplayNetworkAccessError(ReplayInputContractError):
    pass


class ReplayNetworkGuard(AbstractContextManager["ReplayNetworkGuard"]):
    """Python-level fail-fast instrumentation for a ReplayV3 run/test.

    This guard is not installed in a shared live process.  It is used by the
    offline ReplayV3 worker so Python socket/SSL/requests fallbacks raise and
    increment an auditable attempt count.  It is deliberately noncertifying:
    it cannot prove OS-level zero egress and does not fence native libraries,
    libpq/curl transports, or subprocesses.  Certification therefore requires
    a separately isolated, OS-enforced zero-egress replay process.
    """

    _patch_lock = threading.Lock()

    def __init__(
        self,
        *,
        allowed_endpoints: Iterable[tuple[str, int]] = (),
    ) -> None:
        self.attempt_count = 0
        normalized: set[tuple[str, int]] = set()
        for host, port in allowed_endpoints:
            endpoint = (str(host or "").strip().lower(), int(port))
            if not endpoint[0] or not 0 < endpoint[1] <= 65_535:
                raise CaptureContractError("invalid replay network allowlist endpoint")
            normalized.add(endpoint)
        self.allowed_endpoints = frozenset(normalized)
        self._attempt_lock = threading.Lock()
        self._patches: list[tuple[Any, str, Any]] = []
        self._active = False

    def _blocked(self, *_args: Any, **_kwargs: Any) -> Any:
        with self._attempt_lock:
            self.attempt_count += 1
        raise ReplayNetworkAccessError("ReplayV3 network fallback is forbidden")

    @staticmethod
    def _address_endpoint(address: Any) -> tuple[str, int] | None:
        if not isinstance(address, tuple) or len(address) < 2:
            return None
        try:
            return str(address[0]).strip().lower(), int(address[1])
        except (TypeError, ValueError):
            return None

    def _address_allowed(self, address: Any) -> bool:
        endpoint = self._address_endpoint(address)
        return endpoint is not None and endpoint in self.allowed_endpoints

    def _socket_allowed(self, sock: socket.socket) -> bool:
        if sock.family not in {socket.AF_INET, socket.AF_INET6}:
            return True
        try:
            return self._address_allowed(sock.getpeername())
        except OSError:
            return False

    def _patch(self, owner: Any, name: str, replacement: Any) -> None:
        original = getattr(owner, name)
        self._patches.append((owner, name, original))
        setattr(owner, name, replacement)

    def _restore(self) -> None:
        for owner, name, original in reversed(self._patches):
            setattr(owner, name, original)
        self._patches.clear()

    def __enter__(self) -> "ReplayNetworkGuard":
        if self._active:
            raise ReplayNetworkAccessError("ReplayNetworkGuard cannot be re-entered")
        if not self._patch_lock.acquire(blocking=False):
            raise ReplayNetworkAccessError("another ReplayNetworkGuard is active")
        try:
            original_create_connection = socket.create_connection
            original_connect = socket.socket.connect
            original_connect_ex = socket.socket.connect_ex

            def guarded_create_connection(
                address: Any, *args: Any, **kwargs: Any
            ) -> Any:
                if self._address_allowed(address):
                    return original_create_connection(address, *args, **kwargs)
                return self._blocked()

            def guarded_connect(sock: socket.socket, address: Any) -> Any:
                if self._address_allowed(address):
                    return original_connect(sock, address)
                return self._blocked()

            def guarded_connect_ex(sock: socket.socket, address: Any) -> Any:
                if self._address_allowed(address):
                    return original_connect_ex(sock, address)
                return self._blocked()

            self._patch(socket, "create_connection", guarded_create_connection)
            self._patch(socket.socket, "connect", guarded_connect)
            self._patch(socket.socket, "connect_ex", guarded_connect_ex)

            # A client created before the guard could otherwise reuse an open
            # provider connection without calling connect again.  Fence all
            # Internet-socket I/O unless its exact endpoint was explicitly
            # allowlisted (for example, a pinned local replay database).
            for method_name in ("send", "sendall", "recv", "recv_into"):
                original = getattr(socket.socket, method_name)

                def guarded_io(
                    sock: socket.socket,
                    *args: Any,
                    _original: Any = original,
                    **kwargs: Any,
                ) -> Any:
                    if self._socket_allowed(sock):
                        return _original(sock, *args, **kwargs)
                    return self._blocked()

                self._patch(socket.socket, method_name, guarded_io)

            original_sendto = socket.socket.sendto

            def guarded_sendto(sock: socket.socket, *args: Any, **kwargs: Any) -> Any:
                address = args[-1] if args else kwargs.get("address")
                if self._address_allowed(address):
                    return original_sendto(sock, *args, **kwargs)
                return self._blocked()

            self._patch(socket.socket, "sendto", guarded_sendto)

            original_ssl_connect = ssl.SSLSocket.connect
            original_ssl_connect_ex = ssl.SSLSocket.connect_ex

            def guarded_ssl_connect(sock: ssl.SSLSocket, address: Any) -> Any:
                if self._address_allowed(address):
                    return original_ssl_connect(sock, address)
                return self._blocked()

            def guarded_ssl_connect_ex(sock: ssl.SSLSocket, address: Any) -> Any:
                if self._address_allowed(address):
                    return original_ssl_connect_ex(sock, address)
                return self._blocked()

            self._patch(ssl.SSLSocket, "connect", guarded_ssl_connect)
            self._patch(ssl.SSLSocket, "connect_ex", guarded_ssl_connect_ex)
            for method_name in (
                "send",
                "sendall",
                "recv",
                "recv_into",
                "write",
                "read",
                "do_handshake",
            ):
                original = getattr(ssl.SSLSocket, method_name)

                def guarded_ssl_io(
                    sock: ssl.SSLSocket,
                    *args: Any,
                    _original: Any = original,
                    **kwargs: Any,
                ) -> Any:
                    if self._socket_allowed(sock):
                        return _original(sock, *args, **kwargs)
                    return self._blocked()

                self._patch(ssl.SSLSocket, method_name, guarded_ssl_io)

            try:
                import requests
            except ImportError:
                pass
            else:
                def guarded_request(*_args: Any, **_kwargs: Any) -> Any:
                    return self._blocked()

                self._patch(requests.sessions.Session, "request", guarded_request)
        except BaseException:
            self._restore()
            self._patch_lock.release()
            raise
        self._active = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if not self._active:
            raise ReplayNetworkAccessError("ReplayNetworkGuard is not active")
        try:
            self._restore()
        finally:
            self._active = False
            self._patch_lock.release()
        return False
