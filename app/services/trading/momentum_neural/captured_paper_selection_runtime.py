"""Inert lifecycle owner for the captured Alpaca PAPER selection frontier.

The constructor deliberately performs no database, provider, broker, capture-store,
or thread work.  ``start`` is the only activation boundary and must be invoked while
the process-wide captured-PAPER service fence is held.  The worker publishes and
durably consumes one complete initial selection snapshot before exposing the
initial-candidate reader.

This module does not authorize an Alpaca order lane.  It owns only the selection
capture/clone lifecycle and its exact post-quiesce rollback receipt.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Mapping
import uuid

from .captured_paper_initial_candidate_reader import (
    CapturedPaperInitialCandidateReaderUnavailable,
    SqlAlchemyCapturedPaperInitialCandidateReader,
)
from .captured_paper_initial_provider import (
    CapturedPaperInitialCandidateRead,
    CapturedPaperInitialCandidateReadPort,
)
from .captured_paper_selection_producer import (
    CapturedPaperSelectionAuthority,
)
from .captured_paper_selection_source import (
    CapturedPaperSelectionSourceUnavailable,
)
from .captured_paper_variant_binding import (
    CapturedPaperVariantBindingApplication,
)
from .replay_capture_contract import CaptureRunIdentity, sha256_json
from .replay_capture_runtime import SharedCaptureStoreRuntime


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ROLLBACK_SCHEMA_VERSION = "chili.captured-paper-variant-binding-rollback.v2"
_RUNTIME_ROLLBACK_SCHEMA_VERSION = (
    "chili.captured-paper-selection-runtime-rollback.v2"
)
_HEALTH_SCHEMA_VERSION = "chili.captured-paper-selection-runtime-health.v1"
_ACCOUNT_IDENTITY_SCHEMA_VERSION = "chili.captured-paper-selection-account.v1"


class CapturedPaperSelectionRuntimeError(RuntimeError):
    """Typed fail-closed lifecycle error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code or "selection_runtime_failed")
        super().__init__(f"{self.code}: {message}")


class CapturedPaperSelectionApplicationNotApplied(
    CapturedPaperSelectionRuntimeError
):
    """Typed, hash-bound proof that the clone transaction did not commit."""

    def __init__(self, proof: Mapping[str, Any]) -> None:
        self.proof = copy.deepcopy(dict(proof)) if isinstance(proof, Mapping) else {}
        super().__init__(
            "APPLICATION_NOT_APPLIED",
            "durable receipt and current-generation clone census are absent",
        )


class CapturedPaperSelectionApplicationOutcomeAmbiguous(
    CapturedPaperSelectionRuntimeError
):
    """Carries the exact non-runnable application solely for reconciliation."""

    def __init__(self, setup: Any) -> None:
        self.setup = setup
        super().__init__(
            "APPLICATION_OUTCOME_AMBIGUOUS",
            "clone transaction outcome requires exact reconciliation",
        )


def _reject(code: str, message: str) -> None:
    raise CapturedPaperSelectionRuntimeError(code, message)


def _sha(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _reject("CONTRACT_INVALID", f"{field_name} is not a canonical SHA-256")
    return value


def _canonical_uuid_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (AttributeError, TypeError, ValueError):
        return False


def _canonical_utc_text(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return (
        parsed.tzinfo is not None
        and parsed.utcoffset() is not None
        and parsed.utcoffset().total_seconds() == 0
        and parsed.isoformat().replace("+00:00", "Z") == value
    )


def _positive_seconds(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject("CONTRACT_INVALID", f"{field_name} must be finite and positive")
    resolved = float(value)
    if not math.isfinite(resolved) or resolved <= 0.0:
        _reject("CONTRACT_INVALID", f"{field_name} must be finite and positive")
    return resolved


def _nonnegative_seconds(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject("CONTRACT_INVALID", f"{field_name} must be finite and non-negative")
    resolved = float(value)
    if not math.isfinite(resolved) or resolved < 0.0:
        _reject("CONTRACT_INVALID", f"{field_name} must be finite and non-negative")
    return resolved


# Re-check cadence while warming up the initial selection snapshot.  read_snapshot
# is a database read, so this is deliberately coarse (a documented floor, not a
# busy-wait like the durable/producer frontier loops).
_INITIAL_SNAPSHOT_WARMUP_POLL_SECONDS = 2.0


def _health_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    converter = getattr(value, "to_mapping", None)
    if not callable(converter):
        converter = getattr(value, "to_dict", None)
    if callable(converter):
        converted = converter()
        if isinstance(converted, Mapping):
            return dict(converted)
    _reject("HEALTH_INVALID", f"{field_name} health is not a mapping")


def _same_authority(
    left: Any,
    right: CapturedPaperSelectionAuthority,
) -> bool:
    return (
        type(left) is CapturedPaperSelectionAuthority
        and left.to_dict() == right.to_dict()
    )


class CapturedPaperSelectionStartupCleanup:
    """Reverse-order cleanup registry for lazy component-factory side effects."""

    def __init__(self) -> None:
        self._callbacks: list[tuple[str, Callable[[], None]]] = []
        self._disarmed = False
        self._lock = threading.RLock()

    @property
    def registration_count(self) -> int:
        with self._lock:
            return len(self._callbacks)

    def register(self, name: str, callback: Callable[[], None]) -> None:
        normalized = str(name or "").strip()
        if not normalized or not callable(callback):
            _reject("COMPONENT_CLEANUP_INVALID", "partial cleanup is invalid")
        with self._lock:
            if self._disarmed:
                _reject(
                    "COMPONENT_CLEANUP_INVALID",
                    "partial cleanup registry is already disarmed",
                )
            self._callbacks.append((normalized, callback))

    def disarm(self) -> None:
        with self._lock:
            self._disarmed = True
            self._callbacks.clear()

    def cleanup(self) -> tuple[str, ...]:
        with self._lock:
            if self._disarmed:
                return ()
            callbacks = tuple(reversed(self._callbacks))
            self._callbacks.clear()
            self._disarmed = True
        errors: list[str] = []
        for name, callback in callbacks:
            try:
                callback()
            except BaseException as exc:
                errors.append(f"{name}:{type(exc).__name__}:{exc}")
        return tuple(errors)


class DeferredCapturedPaperInitialCandidateReader(
    CapturedPaperInitialCandidateReadPort
):
    """Atomic, one-shot reader handoff which is dark until durable prime.

    A delegated read carries a private installation epoch.  Revocation is
    immediate and non-blocking; an already-running read is discarded as typed
    coverage-unavailable if its epoch was revoked before it could return.
    """

    def __init__(
        self,
        *,
        expected_reader_type: type = SqlAlchemyCapturedPaperInitialCandidateReader,
    ) -> None:
        if not isinstance(expected_reader_type, type):
            _reject("CONTRACT_INVALID", "expected candidate-reader type is invalid")
        self._expected_reader_type = expected_reader_type
        self._reader: CapturedPaperInitialCandidateReadPort | None = None
        self._authority_sha256: str | None = None
        self._suspended = False
        self._suspend_reason: str | None = None
        self._revoked = False
        self._revoke_reason: str | None = None
        self._epoch = 0
        self._lock = threading.RLock()

    @property
    def network_fallback_allowed(self) -> bool:
        return False

    @property
    def mutation_allowed(self) -> bool:
        return False

    def _validate_installable(
        self,
        reader: CapturedPaperInitialCandidateReadPort,
        *,
        authority: CapturedPaperSelectionAuthority,
    ) -> None:
        if type(reader) is not self._expected_reader_type:
            _reject(
                "INITIAL_READER_INVALID",
                "candidate reader is not the exact pinned implementation",
            )
        try:
            unsafe = (
                not isinstance(reader, CapturedPaperInitialCandidateReadPort)
                or reader.network_fallback_allowed is not False
                or reader.mutation_allowed is not False
            )
        except Exception:
            unsafe = True
        if unsafe:
            _reject(
                "INITIAL_READER_INVALID",
                "candidate reader exposes an unsafe capability",
            )
        bound_authority = getattr(reader, "_authority", None)
        if not _same_authority(bound_authority, authority):
            _reject(
                "INITIAL_READER_INVALID",
                "candidate reader differs from the exact selection authority",
            )

    def validate_installable(
        self,
        reader: CapturedPaperInitialCandidateReadPort,
        *,
        authority: CapturedPaperSelectionAuthority,
    ) -> None:
        """Validate without publishing the reader capability."""

        self._validate_installable(reader, authority=authority)

    def install(
        self,
        reader: CapturedPaperInitialCandidateReadPort,
        *,
        authority: CapturedPaperSelectionAuthority,
    ) -> None:
        self._validate_installable(reader, authority=authority)
        with self._lock:
            if self._revoked:
                _reject(
                    "INITIAL_READER_REVOKED",
                    "a revoked candidate reader cannot be reinstalled",
                )
            if self._reader is not None:
                if (
                    self._reader is reader
                    and self._authority_sha256 == authority.authority_sha256
                ):
                    return
                _reject(
                    "INITIAL_READER_ALREADY_INSTALLED",
                    "candidate reader installation is one-shot",
                )
            self._reader = reader
            self._authority_sha256 = authority.authority_sha256
            self._suspended = False
            self._suspend_reason = None
            self._epoch += 1

    def suspend(self, reason: str = "selection_source_unavailable") -> None:
        """Temporarily hide an installed reader without discarding its binding."""

        normalized = str(reason or "selection_source_unavailable")
        with self._lock:
            if self._revoked or self._reader is None:
                return
            if self._suspended and self._suspend_reason == normalized:
                return
            self._suspended = True
            self._suspend_reason = normalized
            self._epoch += 1

    def resume(
        self,
        reader: CapturedPaperInitialCandidateReadPort,
        *,
        authority: CapturedPaperSelectionAuthority,
    ) -> None:
        """Restore only the same exact reader after a newly durable frontier."""

        self._validate_installable(reader, authority=authority)
        with self._lock:
            if self._revoked:
                _reject(
                    "INITIAL_READER_REVOKED",
                    "a revoked candidate reader cannot be resumed",
                )
            if not (
                self._reader is reader
                and self._authority_sha256 == authority.authority_sha256
            ):
                _reject(
                    "INITIAL_READER_BINDING_DRIFT",
                    "candidate reader resume differs from its installation",
                )
            if self._suspended:
                self._suspended = False
                self._suspend_reason = None
                self._epoch += 1

    def revoke(self, reason: str = "selection_runtime_closed") -> None:
        normalized = str(reason or "selection_runtime_closed")
        with self._lock:
            self._reader = None
            self._authority_sha256 = None
            self._suspended = False
            self._suspend_reason = None
            self._revoked = True
            self._epoch += 1
            if self._revoke_reason is None:
                self._revoke_reason = normalized

    def read_candidates(
        self,
        *,
        user_id: int,
        symbol: str,
        decision_at: datetime,
    ) -> CapturedPaperInitialCandidateRead:
        with self._lock:
            reader = self._reader
            if reader is None or self._revoked or self._suspended:
                reason = (
                    self._revoke_reason
                    or self._suspend_reason
                    or "selection_frontier_not_durable"
                )
                raise CapturedPaperInitialCandidateReaderUnavailable(
                    f"initial_candidate_selection_coverage_unavailable:{reason}"
                )
            epoch = self._epoch
        try:
            result = reader.read_candidates(
                user_id=user_id, symbol=symbol, decision_at=decision_at
            )
        except BaseException as exc:
            with self._lock:
                revoked = (
                    self._revoked
                    or self._suspended
                    or self._reader is not reader
                    or self._epoch != epoch
                )
                reason = self._revoke_reason or "selection_frontier_revoked"
            if revoked:
                raise CapturedPaperInitialCandidateReaderUnavailable(
                    f"initial_candidate_selection_coverage_unavailable:{reason}"
                ) from exc
            raise
        with self._lock:
            if (
                self._revoked
                or self._suspended
                or self._reader is not reader
                or self._epoch != epoch
            ):
                reason = self._revoke_reason or "selection_frontier_revoked"
                raise CapturedPaperInitialCandidateReaderUnavailable(
                    f"initial_candidate_selection_coverage_unavailable:{reason}"
                )
            return result

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "installed": (
                    self._reader is not None
                    and not self._revoked
                    and not self._suspended
                ),
                "suspended": self._suspended,
                "suspend_reason": self._suspend_reason,
                "revoked": self._revoked,
                "revoke_reason": self._revoke_reason,
                "authority_sha256": self._authority_sha256,
                "network_fallback_allowed": False,
                "mutation_allowed": False,
            }


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionApplicationSetup:
    """Exact clone application retained before runtime component assembly."""

    application: CapturedPaperVariantBindingApplication
    authority: CapturedPaperSelectionAuthority

    def __post_init__(self) -> None:
        if type(self.application) is not CapturedPaperVariantBindingApplication:
            _reject("APPLICATION_INVALID", "variant application type is invalid")
        if type(self.authority) is not CapturedPaperSelectionAuthority:
            _reject("APPLICATION_INVALID", "selection authority type is invalid")
        application = self.application
        if (
            sha256_json(application.plan.body())
            != application.plan.plan_sha256
            or sha256_json(application.body()) != application.application_sha256
        ):
            _reject("APPLICATION_INVALID", "variant application hash is invalid")
        binding = application.plan.authority
        authority = self.authority
        if not (
            binding.account_scope == authority.account_scope
            and binding.execution_family == authority.execution_family
            and binding.expected_account_id == authority.expected_account_id
            and binding.activation_generation == authority.activation_generation
            and binding.policy_sha256 == authority.policy_sha256
            and binding.settings_projection_sha256
            == authority.settings_projection_sha256
            and binding.code_build_sha256 == authority.code_build_sha256
        ):
            _reject(
                "APPLICATION_INVALID",
                "variant application and selection authority differ",
            )
        application_routes = {
            (
                item.target_variant_id,
                item.family,
                item.version,
                item.target_variant_key,
                item.target_after_sha256,
            )
            for item in application.items
        }
        authority_routes = {
            (
                item.variant_id,
                item.family,
                item.version,
                item.variant_key,
                item.target_after_sha256,
            )
            for item in authority.variant_bindings
        }
        if (
            not application_routes
            or len(application_routes) != len(application.items)
            or application_routes != authority_routes
        ):
            _reject(
                "APPLICATION_INVALID",
                "variant application routes differ from selection authority",
            )


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionWriterSlotAccounting:
    """Measured resource accounting; this is not a strategy/exposure limit."""

    max_writer_threads: int
    permanent_selection_writer_slots: int
    remaining_capture_writer_slots: int
    derived_hot_symbol_capacity: int
    resource_binding_sha256: str


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionRuntimeComponents:
    """Components constructed lazily by the fenced ``start`` boundary."""

    source: Any
    publisher: Any
    writer: Any
    input_port: Any
    producer: Any
    initial_reader: CapturedPaperInitialCandidateReadPort
    close_source: Callable[[], None]

    def __post_init__(self) -> None:
        if not callable(self.close_source):
            _reject("COMPONENT_INVALID", "source close callback is invalid")


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionRollbackReceipt:
    application_sha256: str
    variant_rollback_sha256: str
    target_variant_ids: tuple[int, ...]
    application_outcome: str
    strategy_variants_deactivated: bool
    receipt: Mapping[str, Any]
    runtime_rollback_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.receipt))


class CapturedPaperSelectionLifecycleWorker:
    """Managed worker which owns one captured selection activation generation."""

    def __init__(
        self,
        *,
        shared_capture_runtime: SharedCaptureStoreRuntime,
        deferred_reader: DeferredCapturedPaperInitialCandidateReader,
        assert_service_fence_held: Callable[[], None],
        application_setup_factory: Callable[
            [], CapturedPaperSelectionApplicationSetup
        ],
        component_factory: Callable[
            [
                CapturedPaperSelectionApplicationSetup,
                CapturedPaperSelectionWriterSlotAccounting,
                CapturedPaperSelectionStartupCleanup,
            ],
            CapturedPaperSelectionRuntimeComponents,
        ],
        rollback_application: Callable[
            [CapturedPaperVariantBindingApplication], Mapping[str, Any]
        ],
        poll_interval_seconds: float = 0.25,
        durable_timeout_seconds: float = 15.0,
        producer_timeout_seconds: float = 15.0,
        initial_snapshot_warmup_seconds: float = 0.0,
        monotonic_clock: Callable[[], float] = time.monotonic,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        # Validation is intentionally local-only.  No injected callback, store
        # method, provider, database, or thread is touched here.
        if not isinstance(deferred_reader, DeferredCapturedPaperInitialCandidateReader):
            _reject("CONTRACT_INVALID", "deferred candidate reader is invalid")
        for name, callback in (
            ("assert_service_fence_held", assert_service_fence_held),
            ("application_setup_factory", application_setup_factory),
            ("component_factory", component_factory),
            ("rollback_application", rollback_application),
            ("monotonic_clock", monotonic_clock),
            ("thread_factory", thread_factory),
        ):
            if not callable(callback):
                _reject("CONTRACT_INVALID", f"{name} is not callable")
        self.shared_capture_runtime = shared_capture_runtime
        self.deferred_reader = deferred_reader
        self.assert_service_fence_held = assert_service_fence_held
        self.application_setup_factory = application_setup_factory
        self.component_factory = component_factory
        self.rollback_application = rollback_application
        self.poll_interval_seconds = _positive_seconds(
            poll_interval_seconds, "poll_interval_seconds"
        )
        self.durable_timeout_seconds = _positive_seconds(
            durable_timeout_seconds, "durable_timeout_seconds"
        )
        self.producer_timeout_seconds = _positive_seconds(
            producer_timeout_seconds, "producer_timeout_seconds"
        )
        # Bounded warmup for the fenced-start's FIRST selection cycle.  During a
        # host cutover the legacy capture lanes are stopped and the candidate
        # lanes need a few seconds to re-establish watches and refill the derived
        # viability, so the very first read can transiently observe an empty
        # source.  A positive value tolerates ONLY that transient (an empty
        # source) for this long before failing closed; 0.0 preserves the strict
        # fail-on-first-empty behaviour (used by unit tests).
        self.initial_snapshot_warmup_seconds = _nonnegative_seconds(
            initial_snapshot_warmup_seconds, "initial_snapshot_warmup_seconds"
        )
        self.monotonic_clock = monotonic_clock
        self.thread_factory = thread_factory

        self._lifecycle_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "prepared"
        self._ever_started = False
        self._fatal = False
        self._fatal_reason: str | None = None
        self._application_setup: CapturedPaperSelectionApplicationSetup | None = None
        self._application_outcome = "not_attempted"
        self._not_applied_proof: Mapping[str, Any] | None = None
        self._components: CapturedPaperSelectionRuntimeComponents | None = None
        self._slot_accounting: CapturedPaperSelectionWriterSlotAccounting | None = None
        self._writer_started = False
        self._quiesced = False
        self._rollback_receipt: CapturedPaperSelectionRollbackReceipt | None = None
        self._cycles_completed = 0
        self._source_unavailable_cycles = 0
        self._initial_warmup_retries = 0
        self._snapshot_batches_read = 0
        self._occurrences_published = 0
        self._producer_batches_applied = 0
        self._producer_idle_cycles = 0
        self._last_frontier_sequence = 0
        self._last_frontier_status: str | None = None

    @property
    def application(self) -> CapturedPaperVariantBindingApplication | None:
        setup = self._application_setup
        return setup.application if setup is not None else None

    def _assert_fence(self) -> None:
        self.assert_service_fence_held()

    def _resource_accounting(self) -> CapturedPaperSelectionWriterSlotAccounting:
        runtime = self.shared_capture_runtime
        try:
            max_writers = int(runtime.max_writer_threads)
            binding = runtime.resource_binding
            hot_capacity = int(binding.budget.derived_hot_symbol_capacity)
            binding_sha = str(binding.binding_sha256)
        except Exception as exc:
            raise CapturedPaperSelectionRuntimeError(
                "RESOURCE_BINDING_INVALID",
                "shared capture runtime lacks exact measured resource accounting",
            ) from exc
        remaining = max_writers - 1
        if max_writers <= 0 or remaining <= 0:
            _reject(
                "SELECTION_WRITER_CAPACITY_UNAVAILABLE",
                "the permanent selection writer would leave zero measured writer slots",
            )
        _sha(binding_sha, "resource_binding_sha256")
        return CapturedPaperSelectionWriterSlotAccounting(
            max_writer_threads=max_writers,
            permanent_selection_writer_slots=1,
            remaining_capture_writer_slots=remaining,
            derived_hot_symbol_capacity=hot_capacity,
            resource_binding_sha256=binding_sha,
        )

    @staticmethod
    def _assert_component_capabilities(components: CapturedPaperSelectionRuntimeComponents) -> None:
        input_port = components.input_port
        try:
            unsafe_input = any(
                getattr(input_port, name, None) is not False
                for name in (
                    "network_fallback_allowed",
                    "broker_access_allowed",
                    "mutation_allowed",
                )
            )
        except Exception:
            unsafe_input = True
        if unsafe_input:
            _reject("COMPONENT_INVALID", "selection input port is unsafe")

        source = components.source
        try:
            unsafe_source = (
                source.network_fallback_allowed is not False
                or source.broker_access_allowed is not False
                or source.mutation_allowed is not False
            )
        except Exception:
            unsafe_source = True
        if unsafe_source:
            _reject("COMPONENT_INVALID", "selection source is unsafe")

    def _validate_components(
        self,
        setup: CapturedPaperSelectionApplicationSetup,
        components: CapturedPaperSelectionRuntimeComponents,
    ) -> None:
        if type(components) is not CapturedPaperSelectionRuntimeComponents:
            _reject("COMPONENT_INVALID", "runtime component assembly is invalid")
        authority = setup.authority
        source = components.source
        publisher = components.publisher
        writer = components.writer
        input_port = components.input_port
        producer = components.producer
        self._assert_component_capabilities(components)

        source_identity = getattr(source, "capture_identity", None)
        queue_identity = getattr(publisher, "identity", None)
        expected_account_identity_sha256 = sha256_json(
            {
                "schema_version": _ACCOUNT_IDENTITY_SCHEMA_VERSION,
                "account_scope": "alpaca:paper",
                "expected_account_id": authority.expected_account_id,
                "broker": "alpaca",
                "broker_environment": "paper",
            }
        )
        try:
            source_config_sha256 = sha256_json(
                source.settings_projection.to_dict()
            )
        except Exception as exc:
            raise CapturedPaperSelectionRuntimeError(
                "COMPONENT_IDENTITY_INVALID",
                "source viability settings projection is unavailable",
            ) from exc
        if not (
            isinstance(source_identity, CaptureRunIdentity)
            and isinstance(queue_identity, CaptureRunIdentity)
            and source_identity.run_id == authority.activation_generation
            and source_identity.generation == 2
            and queue_identity.run_id == authority.activation_generation
            and queue_identity.generation == 1
            and source_identity.identity_sha256 != queue_identity.identity_sha256
            and source_identity.code_build_sha256 == authority.code_build_sha256
            # The source identity binds the exact narrow viability projection
            # consumed by the scorer.  The queue identity separately binds the
            # full activation settings projection.  Equating them would make
            # the real source impossible to compose and would erase the
            # intended scorer-vs-activation provenance distinction.
            and source_identity.config_sha256 == source_config_sha256
            and source_identity.feature_flags_sha256 == authority.policy_sha256
            and queue_identity.code_build_sha256 == authority.code_build_sha256
            and queue_identity.config_sha256
            == authority.settings_projection_sha256
            and queue_identity.feature_flags_sha256 == authority.policy_sha256
            and source_identity.account_identity_sha256
            == queue_identity.account_identity_sha256
            == expected_account_identity_sha256
            and queue_identity.broker.strip().lower() == "alpaca"
            and queue_identity.broker_environment.strip().lower() == "paper"
        ):
            _reject(
                "COMPONENT_IDENTITY_INVALID",
                "source generation 2 and queue generation 1 are not exactly bound",
            )
        if not (
            _same_authority(getattr(source, "selection_authority", None), authority)
            and getattr(source, "variant_application", None) is setup.application
            and _same_authority(
                getattr(publisher, "selection_authority", None), authority
            )
            and getattr(writer, "publisher", None) is publisher
            and getattr(input_port, "queue_identity", None) == queue_identity
            and _same_authority(
                getattr(input_port, "selection_authority", None), authority
            )
            and getattr(input_port, "durable_gate", None)
            is getattr(publisher, "durable_gate", None)
            and _same_authority(getattr(producer, "authority", None), authority)
            and getattr(producer, "input_port", None) is input_port
        ):
            _reject(
                "COMPONENT_BINDING_INVALID",
                "selection components do not share one exact authority/queue graph",
            )

        lease = getattr(publisher, "writer_lease", None)
        ingress = getattr(publisher, "ingress", None)
        runtime = self.shared_capture_runtime
        try:
            shared_binding_ok = (
                getattr(lease, "_runtime", None) is runtime
                and lease.store is runtime.store
                and ingress.resource_binding == runtime.resource_binding
                and ingress.shared_admission_budget
                is runtime.shared_admission_budget
                and Path(input_port.root).resolve()
                == Path(runtime.store.root).resolve()
            )
        except Exception:
            shared_binding_ok = False
        if not shared_binding_ok:
            _reject(
                "COMPONENT_RESOURCE_BINDING_INVALID",
                "selection queue is not bound to the exact shared measured store",
            )
        self.deferred_reader.validate_installable(
            components.initial_reader,
            authority=authority,
        )

    def _publisher_health(self) -> dict[str, Any]:
        components = self._components
        if components is None:
            return {}
        return _health_mapping(components.publisher.health(), "queue publisher")

    def _writer_health(self) -> dict[str, Any]:
        components = self._components
        if components is None:
            return {}
        return _health_mapping(components.writer.health(), "queue writer")

    @staticmethod
    def _queue_health_is_fatal(health: Mapping[str, Any]) -> bool:
        ingress = health.get("ingress")
        return bool(
            health.get("poisoned")
            or health.get("poison_reason")
            or (
                isinstance(ingress, Mapping)
                and (
                    ingress.get("writer_failure_count")
                    or ingress.get("dropped")
                    or ingress.get("post_close_submissions")
                )
            )
        )

    @staticmethod
    def _writer_health_is_fatal(health: Mapping[str, Any]) -> bool:
        writer = health.get("writer")
        queue = health.get("queue")
        if isinstance(writer, Mapping):
            if writer.get("last_error"):
                return True
            ingress = writer.get("ingress")
            if isinstance(ingress, Mapping) and (
                ingress.get("writer_failure_count")
                or ingress.get("dropped")
                or ingress.get("post_close_submissions")
            ):
                return True
        return isinstance(queue, Mapping) and bool(
            queue.get("poisoned") or queue.get("poison_reason")
        )

    def _assert_runtime_health(self) -> None:
        queue_health = self._publisher_health()
        writer_health = self._writer_health()
        if self._queue_health_is_fatal(queue_health):
            _reject("QUEUE_POISONED", "selection queue is poisoned or overflowed")
        if self._writer_health_is_fatal(writer_health):
            _reject("WRITER_FATAL", "selection writer reported a fatal condition")

    def _poison(self, reason: str) -> None:
        components = self._components
        if components is None or not self._writer_started:
            return
        try:
            components.publisher.poison(reason)
        except Exception:
            # The original failure remains authoritative; health/close will retain
            # this ambiguity rather than pretending a poison receipt was durable.
            pass

    def _capture_source_once(self, *, require_snapshot: bool) -> int:
        components = self._components
        if components is None:
            _reject("LIFECYCLE_INVALID", "selection components are unavailable")
        source = components.source
        publisher = components.publisher
        self._assert_fence()
        try:
            snapshots = source.read_snapshot()
        except CapturedPaperSelectionSourceUnavailable:
            with self._state_lock:
                self._source_unavailable_cycles += 1
            if not require_snapshot:
                self.deferred_reader.suspend(
                    "selection_source_coverage_unavailable"
                )
            raise
        self._assert_fence()
        try:
            snapshots = tuple(snapshots)
        except TypeError as exc:
            raise CapturedPaperSelectionRuntimeError(
                "SOURCE_CONTRACT_INVALID", "source snapshot result is not iterable"
            ) from exc
        if require_snapshot and not snapshots:
            _reject(
                "SOURCE_PRIME_EMPTY",
                "initial source snapshot did not establish a selection frontier",
            )
        if snapshots:
            with self._state_lock:
                self._snapshot_batches_read += 1

        published = 0
        watermark: datetime | None = None
        try:
            for snapshot in snapshots:
                if self._stop_event.is_set():
                    _reject(
                        "SOURCE_BATCH_INTERRUPTED",
                        "shutdown interrupted an owned source snapshot batch",
                    )
                self._assert_fence()
                sequence = publisher.reserve_sequence()
                occurrence = source.build_occurrence(
                    snapshot,
                    source_sequence=sequence,
                )
                self._assert_fence()
                receipt = publisher.publish_bundle(
                    bundle=occurrence.bundle,
                    scoring_authority=occurrence.scoring_authority,
                    evaluation_at=occurrence.bundle.read_at,
                    source_events=occurrence.source_events,
                )
                if getattr(receipt, "accepted", None) is not True:
                    _reject(
                        "QUEUE_INGRESS_REJECTED",
                        "selection occurrence was rejected by bounded ingress",
                    )
                published += 1
                event_at = occurrence.bundle.event_at
                watermark = event_at if watermark is None else max(watermark, event_at)
            if watermark is not None:
                publisher.heartbeat(watermark_at=watermark)
        except Exception:
            # read_snapshot has already advanced its generation marker.  Once a
            # nonempty tuple is returned, partial ownership can never be retried.
            if snapshots:
                self._poison("selection_source_batch_incomplete")
            raise
        with self._state_lock:
            self._occurrences_published += published
        return published

    def _wait_for_durable_frontier(self) -> int:
        deadline = float(self.monotonic_clock()) + self.durable_timeout_seconds
        while True:
            self._assert_fence()
            self._assert_runtime_health()
            health = self._publisher_health()
            accepted = int(health.get("accepted_through", 0) or 0)
            durable = int(health.get("durable_through", 0) or 0)
            reserved = health.get("reserved_sequence")
            if durable == accepted and reserved is None:
                return durable
            now = float(self.monotonic_clock())
            if not math.isfinite(now) or now >= deadline:
                _reject(
                    "DURABLE_FRONTIER_TIMEOUT",
                    "selection queue did not reach its fsync acknowledgement frontier",
                )
            self._stop_event.wait(min(0.01, max(0.0, deadline - now)))

    def _drain_producer_to(self, target_sequence: int) -> Any:
        components = self._components
        if components is None:
            _reject("LIFECYCLE_INVALID", "selection producer is unavailable")
        deadline = float(self.monotonic_clock()) + self.producer_timeout_seconds
        while True:
            self._assert_fence()
            result = components.producer.tick()
            self._assert_fence()
            frontier = getattr(result, "frontier", None)
            status = getattr(result, "status", None)
            if frontier is None:
                _reject("PRODUCER_INVALID", "selection producer returned no frontier")
            gap_count = int(getattr(frontier, "gap_count", -1))
            frontier_status = str(getattr(frontier, "status", ""))
            sequence = int(getattr(frontier, "last_source_sequence", -1))
            if status == "gap" or gap_count != 0 or frontier_status == "gap":
                _reject(
                    "PRODUCER_GAP",
                    "selection producer recorded a non-reusable coverage gap",
                )
            with self._state_lock:
                self._last_frontier_sequence = sequence
                self._last_frontier_status = frontier_status
                if status == "applied":
                    self._producer_batches_applied += 1
                elif status == "idle":
                    self._producer_idle_cycles += 1
            if sequence == target_sequence and frontier_status == "ready":
                return frontier
            if sequence > target_sequence:
                _reject(
                    "PRODUCER_FRONTIER_INVALID",
                    "selection producer advanced beyond the durable target",
                )
            now = float(self.monotonic_clock())
            if not math.isfinite(now) or now >= deadline:
                _reject(
                    "PRODUCER_FRONTIER_TIMEOUT",
                    "selection producer did not reach a ready gap-free frontier",
                )
            self._stop_event.wait(min(0.01, max(0.0, deadline - now)))

    def _run_initial_cycle_with_warmup(self) -> None:
        """Run the fenced-start's first cycle, tolerating a transiently empty source.

        The only retryable condition is ``CapturedPaperSelectionSourceUnavailable``
        raised by ``read_snapshot`` — that escapes ``_capture_source_once`` BEFORE
        any occurrence is published (the batch loop runs strictly after the read),
        so re-running the whole cycle cannot double-publish.  Every other failure
        (fence violation, contract error, an empty durable frontier AFTER a
        non-empty read, shutdown) stays fail-closed and is raised immediately.
        The wait is interruptible by the stop event and bounded by
        ``initial_snapshot_warmup_seconds`` (0.0 => strict, no retry).
        """
        if self.initial_snapshot_warmup_seconds <= 0.0:
            self._run_cycle(initial=True)
            return
        deadline = (
            float(self.monotonic_clock()) + self.initial_snapshot_warmup_seconds
        )
        while True:
            try:
                self._run_cycle(initial=True)
                return
            except CapturedPaperSelectionSourceUnavailable:
                now = float(self.monotonic_clock())
                if (
                    self._stop_event.is_set()
                    or not math.isfinite(now)
                    or now >= deadline
                ):
                    raise
                with self._state_lock:
                    self._initial_warmup_retries += 1
                self._stop_event.wait(
                    min(
                        _INITIAL_SNAPSHOT_WARMUP_POLL_SECONDS,
                        max(0.0, deadline - now),
                    )
                )

    def _run_cycle(self, *, initial: bool) -> None:
        self._assert_fence()
        published = self._capture_source_once(require_snapshot=initial)
        target = self._wait_for_durable_frontier()
        if initial and target <= 0:
            _reject(
                "DURABLE_FRONTIER_EMPTY",
                "initial selection frontier has no durable occurrence",
            )
        self._drain_producer_to(target)
        self._assert_runtime_health()
        reader_health = self.deferred_reader.health()
        if not initial and reader_health["suspended"] and published > 0:
            components = self._components
            setup = self._application_setup
            if components is None or setup is None:
                _reject(
                    "LIFECYCLE_INVALID",
                    "selection reader cannot resume without exact components",
                )
            self.deferred_reader.resume(
                components.initial_reader,
                authority=setup.authority,
            )
        with self._state_lock:
            self._cycles_completed += 1

    def _record_fatal(self, exc: BaseException) -> None:
        reason = f"{type(exc).__name__}: {exc}"
        with self._state_lock:
            self._fatal = True
            if self._fatal_reason is None:
                self._fatal_reason = reason
            if self._state not in {
                "quiesced",
                "quiesce_ambiguous",
                "rollback_complete",
                "rollback_ambiguous",
            }:
                self._state = "failed"
        self.deferred_reader.revoke("selection_runtime_fatal")
        self._stop_event.set()

    def _poll(self) -> None:
        try:
            while not self._stop_event.wait(self.poll_interval_seconds):
                try:
                    self._run_cycle(initial=False)
                except CapturedPaperSelectionSourceUnavailable:
                    # No snapshot was returned, hence no source generation was
                    # consumed and no sequence was reserved.  Hide the prior
                    # frontier until a fresh snapshot is durably applied.  The
                    # capture boundary suspended it before propagating here.
                    continue
        except BaseException as exc:
            self._poison("selection_runtime_poll_fatal")
            self._record_fatal(exc)

    def _quiesce(self, *, join_timeout_seconds: float) -> tuple[str, ...]:
        errors: list[str] = []
        self.deferred_reader.revoke("selection_runtime_quiescing")
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=join_timeout_seconds)
            if thread.is_alive():
                errors.append("selection_poll_thread_join_timeout")
                return tuple(errors)

        components = self._components
        if components is not None:
            try:
                components.close_source()
            except BaseException as exc:
                errors.append(f"source_close:{type(exc).__name__}:{exc}")
            try:
                closed = components.writer.close(
                    timeout_seconds=join_timeout_seconds
                )
                if closed is not True:
                    errors.append("selection_writer_close_unconfirmed")
            except BaseException as exc:
                errors.append(f"writer_close:{type(exc).__name__}:{exc}")
            try:
                lease_health = _health_mapping(
                    components.publisher.writer_lease.health(),
                    "selection writer lease",
                )
                if lease_health.get("released") is not True:
                    errors.append("selection_writer_lease_not_released")
            except BaseException as exc:
                errors.append(f"lease_health:{type(exc).__name__}:{exc}")
        if not errors:
            with self._state_lock:
                self._quiesced = True
        return tuple(errors)

    def start(self) -> None:
        with self._lifecycle_lock:
            with self._state_lock:
                if self._state != "prepared":
                    _reject("LIFECYCLE_INVALID", "selection worker is one-shot")
                self._state = "starting"
                self._ever_started = True
            try:
                self._assert_fence()
                accounting = self._resource_accounting()
                self._slot_accounting = accounting
                try:
                    setup = self.application_setup_factory()
                except CapturedPaperSelectionApplicationNotApplied as exc:
                    proof = copy.deepcopy(dict(exc.proof))
                    supplied = proof.pop("not_applied_sha256", None)
                    if (
                        set(proof)
                        != {
                            "schema_version",
                            "account_scope",
                            "expected_account_id",
                            "activation_generation",
                            "activation_manifest_sha256",
                            "authority_sha256",
                            "checked_at",
                            "durable_application_receipt_present",
                            "generation_bound_clone_count",
                            "paper_order_submission_authorized",
                            "live_cash_authorized",
                            "real_money_authorized",
                        }
                        or proof.get("schema_version")
                        != "chili.captured-paper-variant-application-not-applied.v1"
                        or proof.get("account_scope") != "alpaca:paper"
                        or not _canonical_uuid_text(
                            proof.get("expected_account_id")
                        )
                        or not _canonical_uuid_text(
                            proof.get("activation_generation")
                        )
                        or not isinstance(
                            proof.get("activation_manifest_sha256"), str
                        )
                        or _SHA256_RE.fullmatch(
                            proof.get("activation_manifest_sha256")
                        ) is None
                        or not isinstance(proof.get("authority_sha256"), str)
                        or _SHA256_RE.fullmatch(
                            proof.get("authority_sha256")
                        ) is None
                        or not _canonical_utc_text(proof.get("checked_at"))
                        or proof.get("durable_application_receipt_present") is not False
                        or proof.get("generation_bound_clone_count") != 0
                        or proof.get("paper_order_submission_authorized") is not False
                        or proof.get("live_cash_authorized") is not False
                        or proof.get("real_money_authorized") is not False
                        or not isinstance(supplied, str)
                        or _SHA256_RE.fullmatch(supplied) is None
                        or sha256_json(proof) != supplied
                    ):
                        _reject(
                            "APPLICATION_NOT_APPLIED_PROOF_INVALID",
                            "clone transaction negative proof is invalid",
                        )
                    self._not_applied_proof = {**proof, "not_applied_sha256": supplied}
                    self._application_outcome = "not_applied"
                    raise
                except CapturedPaperSelectionApplicationOutcomeAmbiguous as exc:
                    if type(exc.setup) is not CapturedPaperSelectionApplicationSetup:
                        _reject(
                            "APPLICATION_AMBIGUOUS_PROOF_INVALID",
                            "ambiguous clone transaction lacks the exact application",
                        )
                    self._application_setup = exc.setup
                    self._application_outcome = "ambiguous"
                    raise
                if type(setup) is not CapturedPaperSelectionApplicationSetup:
                    _reject(
                        "APPLICATION_INVALID",
                        "application setup factory returned an invalid receipt",
                    )
                # Retain the exact application immediately.  Any later failure
                # requires the post-quiesce rollback hook to use these same bytes.
                self._application_setup = setup
                self._application_outcome = "applied"
                self._assert_fence()
                startup_cleanup = CapturedPaperSelectionStartupCleanup()
                try:
                    components = self.component_factory(
                        setup,
                        accounting,
                        startup_cleanup,
                    )
                    if startup_cleanup.registration_count <= 0:
                        _reject(
                            "COMPONENT_CLEANUP_UNREGISTERED",
                            "component factory did not register its acquired writer cleanup",
                        )
                    # Validate before publishing the graph on ``self`` or
                    # disarming the reverse-order cleanup registry.  A factory
                    # can return an object with the wrong type or a subtly
                    # mismatched identity just as easily as it can raise while
                    # constructing it; both cases must release every acquired
                    # lease/writer without relying on generic quiesce logic to
                    # understand an untrusted partial object.
                    self._validate_components(setup, components)
                except BaseException as factory_exc:
                    cleanup_errors = startup_cleanup.cleanup()
                    if cleanup_errors:
                        raise CapturedPaperSelectionRuntimeError(
                            "COMPONENT_FACTORY_CLEANUP_AMBIGUOUS",
                            "; ".join(cleanup_errors),
                        ) from factory_exc
                    raise
                self._components = components
                startup_cleanup.disarm()
                self._assert_fence()
                components.writer.start()
                self._writer_started = True
                self._run_initial_cycle_with_warmup()
                self._assert_fence()
                self.deferred_reader.install(
                    components.initial_reader,
                    authority=setup.authority,
                )
                thread = self.thread_factory(
                    target=self._poll,
                    name="captured-paper-selection-lifecycle",
                    daemon=False,
                )
                self._thread = thread
                with self._state_lock:
                    self._state = "running"
                thread.start()
                if not thread.is_alive():
                    _reject(
                        "THREAD_START_FAILED",
                        "selection lifecycle thread did not become live",
                    )
                self._assert_runtime_health()
            except BaseException as exc:
                self._poison("selection_runtime_start_failed")
                self._record_fatal(exc)
                cleanup = self._quiesce(
                    join_timeout_seconds=self.durable_timeout_seconds
                )
                if cleanup:
                    with self._state_lock:
                        self._fatal_reason = (
                            f"{self._fatal_reason}; cleanup={','.join(cleanup)}"
                        )
                        self._quiesced = False
                else:
                    with self._state_lock:
                        self._state = "quiesced"
                if isinstance(exc, CapturedPaperSelectionRuntimeError):
                    raise
                raise CapturedPaperSelectionRuntimeError(
                    "START_FAILED", "selection lifecycle failed during fenced start"
                ) from exc

    def close(self, *, join_timeout_seconds: float) -> None:
        timeout = _positive_seconds(join_timeout_seconds, "join_timeout_seconds")
        with self._lifecycle_lock:
            with self._state_lock:
                if self._state == "rollback_complete":
                    return
                if self._state == "quiesced" and self._quiesced:
                    return
                if self._state == "prepared":
                    self.deferred_reader.revoke("selection_runtime_closed_before_start")
                    self._state = "quiesced"
                    self._quiesced = True
                    return
                self._state = "quiescing"
            errors = self._quiesce(join_timeout_seconds=timeout)
            if errors:
                exc = CapturedPaperSelectionRuntimeError(
                    "QUIESCE_AMBIGUOUS", "; ".join(errors)
                )
                self._record_fatal(exc)
                with self._state_lock:
                    self._state = "quiesce_ambiguous"
                    self._quiesced = False
                raise exc
            with self._state_lock:
                self._state = "quiesced"

    def _validate_rollback(
        self,
        raw: Mapping[str, Any],
    ) -> CapturedPaperSelectionRollbackReceipt:
        setup = self._application_setup
        if setup is None:
            _reject("ROLLBACK_INVALID", "no exact variant application is retained")
        if not isinstance(raw, Mapping):
            _reject("ROLLBACK_INVALID", "variant rollback receipt is not a mapping")
        receipt = copy.deepcopy(dict(raw))
        rollback_sha = receipt.pop("rollback_sha256", None)
        application_outcome = receipt.get("application_outcome")
        if (
            receipt.get("schema_version") != _ROLLBACK_SCHEMA_VERSION
            or application_outcome not in {"rolled_back", "not_applied"}
            or receipt.get("application_sha256")
            != setup.application.application_sha256
            or receipt.get("account_scope") != "alpaca:paper"
            or receipt.get("expected_account_id")
            != setup.authority.expected_account_id
            or receipt.get("activation_generation")
            != setup.authority.activation_generation
            or receipt.get("paper_order_submission_authorized") is not False
            or receipt.get("live_cash_authorized") is not False
            or receipt.get("real_money_authorized") is not False
            or not isinstance(receipt.get("items"), list)
        ):
            _reject("ROLLBACK_INVALID", "variant rollback receipt binding is invalid")
        rollback_sha = _sha(rollback_sha, "rollback_sha256")
        if sha256_json(receipt) != rollback_sha:
            _reject("ROLLBACK_INVALID", "variant rollback receipt hash is invalid")
        expected_items = {
            item.target_variant_id: item for item in setup.application.items
        }
        observed_ids: list[int] = []
        for row in receipt["items"]:
            if not isinstance(row, Mapping):
                _reject("ROLLBACK_INVALID", "variant rollback item is invalid")
            target_id = row.get("target_variant_id")
            expected = expected_items.get(target_id)
            if not (
                expected is not None
                and row.get("target_variant_key") == expected.target_variant_key
                and row.get("target_before_sha256")
                == expected.target_after_sha256
                and row.get("deactivated") is True
                and _SHA256_RE.fullmatch(str(row.get("target_after_sha256") or ""))
            ):
                _reject(
                    "ROLLBACK_INVALID",
                    "variant rollback item differs from the exact application",
                )
            observed_ids.append(int(target_id))
        expected_target_ids = tuple(sorted(expected_items))
        target_ids = (
            expected_target_ids if application_outcome == "rolled_back" else ()
        )
        if (
            (application_outcome == "rolled_back"
             and tuple(sorted(observed_ids)) != expected_target_ids)
            or (application_outcome == "not_applied" and observed_ids)
        ):
            _reject(
                "ROLLBACK_INVALID",
                "variant rollback receipt does not cover the exact clone set",
            )
        body = {
            "schema_version": _RUNTIME_ROLLBACK_SCHEMA_VERSION,
            "account_scope": "alpaca:paper",
            "expected_account_id": setup.authority.expected_account_id,
            "activation_generation": setup.authority.activation_generation,
            "variant_application_sha256": setup.application.application_sha256,
            "variant_rollback_sha256": rollback_sha,
            "target_variant_ids": list(target_ids),
            "application_outcome": application_outcome,
            "strategy_variants_deactivated": application_outcome == "rolled_back",
            "paper_order_submission_authorized": False,
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }
        runtime_sha = sha256_json(body)
        full = {**body, "runtime_rollback_sha256": runtime_sha}
        return CapturedPaperSelectionRollbackReceipt(
            application_sha256=setup.application.application_sha256,
            variant_rollback_sha256=rollback_sha,
            target_variant_ids=target_ids,
            application_outcome=str(application_outcome),
            strategy_variants_deactivated=application_outcome == "rolled_back",
            receipt=full,
            runtime_rollback_sha256=runtime_sha,
        )

    def rollback_after_quiesce(self) -> Mapping[str, Any]:
        """Rollback exact clone bytes after all PAPER workers are quiesced.

        An exception from the callback is ambiguous: the database transaction may
        have committed before its acknowledgement was lost.  The application is
        retained and automatic retry is forbidden.
        """

        with self._lifecycle_lock:
            if self._rollback_receipt is not None:
                return self._rollback_receipt.to_dict()
            with self._state_lock:
                if self._state == "rollback_ambiguous":
                    _reject(
                        "ROLLBACK_AMBIGUOUS",
                        "automatic retry is forbidden after ambiguous rollback",
                    )
                if not self._quiesced or self._state not in {"quiesced", "failed"}:
                    _reject(
                        "ROLLBACK_NOT_QUIESCED",
                        "selection runtime is not proven quiesced",
                    )
            setup = self._application_setup
            if setup is None:
                proof = self._not_applied_proof
                if self._application_outcome != "not_applied" or proof is None:
                    _reject("ROLLBACK_INVALID", "no applied clone set is retained")
                proof_sha = _sha(
                    proof.get("not_applied_sha256"), "not_applied_sha256"
                )
                rollback_body = {
                    "schema_version": (
                        "chili.captured-paper-selection-not-applied-rollback.v1"
                    ),
                    "not_applied_sha256": proof_sha,
                    "account_scope": proof.get("account_scope"),
                    "expected_account_id": proof.get("expected_account_id"),
                    "activation_generation": proof.get("activation_generation"),
                    "paper_order_submission_authorized": False,
                    "live_cash_authorized": False,
                    "real_money_authorized": False,
                }
                rollback_sha = sha256_json(rollback_body)
                body = {
                    "schema_version": _RUNTIME_ROLLBACK_SCHEMA_VERSION,
                    "account_scope": "alpaca:paper",
                    "expected_account_id": proof.get("expected_account_id"),
                    "activation_generation": proof.get("activation_generation"),
                    "variant_application_sha256": proof_sha,
                    "variant_rollback_sha256": rollback_sha,
                    "target_variant_ids": [],
                    "application_outcome": "not_applied",
                    "strategy_variants_deactivated": False,
                    "paper_order_submission_authorized": False,
                    "live_cash_authorized": False,
                    "real_money_authorized": False,
                }
                runtime_sha = sha256_json(body)
                full = {**body, "runtime_rollback_sha256": runtime_sha}
                receipt = CapturedPaperSelectionRollbackReceipt(
                    application_sha256=proof_sha,
                    variant_rollback_sha256=rollback_sha,
                    target_variant_ids=(),
                    application_outcome="not_applied",
                    strategy_variants_deactivated=False,
                    receipt=full,
                    runtime_rollback_sha256=runtime_sha,
                )
                self._rollback_receipt = receipt
                with self._state_lock:
                    self._state = "rollback_complete"
                return receipt.to_dict()
            self._assert_fence()
            try:
                raw = self.rollback_application(setup.application)
                self._assert_fence()
                receipt = self._validate_rollback(raw)
            except BaseException as exc:
                with self._state_lock:
                    self._fatal = True
                    self._fatal_reason = f"rollback_ambiguous:{type(exc).__name__}:{exc}"
                    self._state = "rollback_ambiguous"
                if isinstance(exc, CapturedPaperSelectionRuntimeError):
                    raise
                raise CapturedPaperSelectionRuntimeError(
                    "ROLLBACK_AMBIGUOUS",
                    "exact variant rollback outcome is not proven",
                ) from exc
            self._rollback_receipt = receipt
            self._application_outcome = receipt.application_outcome
            with self._state_lock:
                self._state = "rollback_complete"
            return receipt.to_dict()

    def health(self) -> dict[str, Any]:
        queue: dict[str, Any] = {}
        writer: dict[str, Any] = {}
        input_port: dict[str, Any] = {}
        dynamic_error: str | None = None
        try:
            queue = self._publisher_health()
            writer = self._writer_health()
            components = self._components
            if components is not None and callable(
                getattr(components.input_port, "health", None)
            ):
                input_port = _health_mapping(
                    components.input_port.health(), "selection input port"
                )
            if self._queue_health_is_fatal(queue) or self._writer_health_is_fatal(
                writer
            ):
                dynamic_error = "selection_queue_or_writer_fatal"
            if input_port.get("poisoned") or input_port.get("poison_reason"):
                dynamic_error = "selection_input_port_fatal"
        except BaseException as exc:
            dynamic_error = f"health_unavailable:{type(exc).__name__}:{exc}"
        if dynamic_error is not None:
            self._record_fatal(
                CapturedPaperSelectionRuntimeError("HEALTH_FATAL", dynamic_error)
            )

        with self._state_lock:
            setup = self._application_setup
            accounting = self._slot_accounting
            thread = self._thread
            running = bool(
                self._state == "running"
                and thread is not None
                and thread.is_alive()
                and not self._fatal
            )
            return {
                "schema_version": _HEALTH_SCHEMA_VERSION,
                "state": self._state,
                "ever_started": self._ever_started,
                "running": running,
                "stop_requested": self._stop_event.is_set(),
                "fatal": self._fatal,
                "fatal_reason": self._fatal_reason,
                "ready": bool(
                    running
                    and self.deferred_reader.health()["installed"]
                    and self._last_frontier_status == "ready"
                    and self._last_frontier_sequence > 0
                ),
                "quiesced": self._quiesced,
                "thread_alive": bool(thread is not None and thread.is_alive()),
                "cycles_completed": self._cycles_completed,
                "source_unavailable_cycles": self._source_unavailable_cycles,
                "initial_warmup_retries": self._initial_warmup_retries,
                "snapshot_batches_read": self._snapshot_batches_read,
                "occurrences_published": self._occurrences_published,
                "producer_batches_applied": self._producer_batches_applied,
                "producer_idle_cycles": self._producer_idle_cycles,
                "last_frontier_sequence": self._last_frontier_sequence,
                "last_frontier_status": self._last_frontier_status,
                "application_sha256": (
                    setup.application.application_sha256 if setup is not None else None
                ),
                "application_outcome": self._application_outcome,
                "not_applied_sha256": (
                    self._not_applied_proof.get("not_applied_sha256")
                    if self._not_applied_proof is not None
                    else None
                ),
                "authority_sha256": (
                    setup.authority.authority_sha256 if setup is not None else None
                ),
                "rollback_runtime_sha256": (
                    self._rollback_receipt.runtime_rollback_sha256
                    if self._rollback_receipt is not None
                    else None
                ),
                "writer_slot_accounting": (
                    {
                        "max_writer_threads": accounting.max_writer_threads,
                        "permanent_selection_writer_slots": (
                            accounting.permanent_selection_writer_slots
                        ),
                        "remaining_capture_writer_slots": (
                            accounting.remaining_capture_writer_slots
                        ),
                        "derived_hot_symbol_capacity": (
                            accounting.derived_hot_symbol_capacity
                        ),
                        "resource_binding_sha256": (
                            accounting.resource_binding_sha256
                        ),
                        "strategy_or_exposure_cap": False,
                    }
                    if accounting is not None
                    else None
                ),
                "candidate_reader": self.deferred_reader.health(),
                "queue": queue,
                "writer": writer,
                "input_port": input_port,
                "paper_order_submission_authorized": False,
                "live_cash_authorized": False,
                "real_money_authorized": False,
            }


__all__ = [
    "CapturedPaperSelectionApplicationSetup",
    "CapturedPaperSelectionApplicationNotApplied",
    "CapturedPaperSelectionApplicationOutcomeAmbiguous",
    "CapturedPaperSelectionLifecycleWorker",
    "CapturedPaperSelectionRollbackReceipt",
    "CapturedPaperSelectionRuntimeComponents",
    "CapturedPaperSelectionRuntimeError",
    "CapturedPaperSelectionStartupCleanup",
    "CapturedPaperSelectionWriterSlotAccounting",
    "DeferredCapturedPaperInitialCandidateReader",
]
