from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

from app.services.trading.momentum_neural.captured_paper_initial_candidate_reader import (
    CapturedPaperInitialCandidateReaderUnavailable,
)
from app.services.trading.momentum_neural.captured_paper_initial_provider import (
    CapturedPaperInitialCandidateRead,
)
from app.services.trading.momentum_neural.captured_paper_selection_producer import (
    CapturedPaperSelectionAuthority,
    CapturedPaperSelectionVariantBinding,
)
from app.services.trading.momentum_neural.captured_paper_selection_runtime import (
    CapturedPaperSelectionApplicationNotApplied,
    CapturedPaperSelectionApplicationOutcomeAmbiguous,
    CapturedPaperSelectionApplicationSetup,
    CapturedPaperSelectionLifecycleWorker,
    CapturedPaperSelectionRuntimeComponents,
    CapturedPaperSelectionRuntimeError,
    CapturedPaperSelectionStartupCleanup,
    DeferredCapturedPaperInitialCandidateReader,
)
from app.services.trading.momentum_neural.captured_paper_selection_source import (
    CapturedPaperSelectionSourceUnavailable,
)
from app.services.trading.momentum_neural.captured_paper_variant_binding import (
    CapturedPaperVariantBindingApplication,
    CapturedPaperVariantBindingApplicationItem,
    CapturedPaperVariantBindingAuthority,
    CapturedPaperVariantBindingPlan,
    CapturedPaperVariantBindingPlanItem,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureRunIdentity,
    sha256_json,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 18, 16, 0, tzinfo=UTC)
ACCOUNT_ID = "10000000-0000-4000-8000-000000000001"
ACTIVATION_ID = "20000000-0000-4000-8000-000000000002"
POLICY_SHA = "1" * 64
SETTINGS_SHA = "2" * 64
CODE_SHA = "3" * 64
ACCOUNT_SHA = sha256_json(
    {
        "schema_version": "chili.captured-paper-selection-account.v1",
        "account_scope": "alpaca:paper",
        "expected_account_id": ACCOUNT_ID,
        "broker": "alpaca",
        "broker_environment": "paper",
    }
)
SOURCE_SHA = "5" * 64
TARGET_SHA = "6" * 64
PROJECTION_SHA = "7" * 64
RESOURCE_SHA = "8" * 64
SOURCE_SETTINGS = {"viability_setting": 0.75}
SOURCE_SETTINGS_SHA = sha256_json(SOURCE_SETTINGS)


class _RetainedDeadlineClock:
    def __init__(self, *, step: float) -> None:
        self.now = 0.0
        self.step = step
        self.armed = False

    def __call__(self) -> float:
        if self.armed:
            self.now += self.step
        return self.now

    def arm(self, _reason: str) -> None:
        self.armed = True


def _not_applied_proof() -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": (
            "chili.captured-paper-variant-application-not-applied.v1"
        ),
        "account_scope": "alpaca:paper",
        "expected_account_id": ACCOUNT_ID,
        "activation_generation": ACTIVATION_ID,
        "activation_manifest_sha256": "a" * 64,
        "authority_sha256": "b" * 64,
        "checked_at": NOW.isoformat().replace("+00:00", "Z"),
        "durable_application_receipt_present": False,
        "generation_bound_clone_count": 0,
        "paper_order_submission_authorized": False,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    return {**body, "not_applied_sha256": sha256_json(body)}


def _application_setup() -> CapturedPaperSelectionApplicationSetup:
    binding_authority = CapturedPaperVariantBindingAuthority(
        expected_account_id=ACCOUNT_ID,
        activation_generation=ACTIVATION_ID,
        policy_sha256=POLICY_SHA,
        settings_projection_sha256=SETTINGS_SHA,
        code_build_sha256=CODE_SHA,
        bound_at=NOW,
    )
    plan_item = CapturedPaperVariantBindingPlanItem(
        family="momentum_breakout",
        version=3,
        source_variant_id=11,
        source_variant_sha256=SOURCE_SHA,
        source_parent_variant_id=None,
        target_variant_key="captured_paper:momentum_breakout",
        target_variant_id=21,
        target_state="update_required",
        target_before_sha256=None,
        target_projection_sha256=PROJECTION_SHA,
    )
    provisional_plan = CapturedPaperVariantBindingPlan(
        authority=binding_authority,
        items=(plan_item,),
        plan_sha256="0" * 64,
    )
    plan = CapturedPaperVariantBindingPlan(
        authority=binding_authority,
        items=(plan_item,),
        plan_sha256=sha256_json(provisional_plan.body()),
    )
    application_item = CapturedPaperVariantBindingApplicationItem(
        family="momentum_breakout",
        version=3,
        source_variant_id=11,
        source_variant_sha256=SOURCE_SHA,
        target_variant_key="captured_paper:momentum_breakout",
        target_variant_id=21,
        target_before_sha256=None,
        target_after_sha256=TARGET_SHA,
        action="updated",
    )
    provisional_application = CapturedPaperVariantBindingApplication(
        plan=plan,
        items=(application_item,),
        application_sha256="0" * 64,
    )
    application = CapturedPaperVariantBindingApplication(
        plan=plan,
        items=(application_item,),
        application_sha256=sha256_json(provisional_application.body()),
    )
    authority = CapturedPaperSelectionAuthority(
        expected_account_id=ACCOUNT_ID,
        activation_generation=ACTIVATION_ID,
        policy_sha256=POLICY_SHA,
        settings_projection_sha256=SETTINGS_SHA,
        code_build_sha256=CODE_SHA,
        variant_bindings=(
            CapturedPaperSelectionVariantBinding(
                variant_id=21,
                family="momentum_breakout",
                version=3,
                variant_key="captured_paper:momentum_breakout",
                target_after_sha256=TARGET_SHA,
            ),
        ),
    )
    return CapturedPaperSelectionApplicationSetup(
        application=application,
        authority=authority,
    )


class _FakeInitialReader:
    network_fallback_allowed = False
    mutation_allowed = False

    def __init__(
        self,
        authority: CapturedPaperSelectionAuthority,
        *,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self._authority = authority
        self.entered = entered
        self.release = release

    def read_candidates(
        self,
        *,
        user_id: int,
        symbol: str,
        decision_at: datetime,
    ) -> CapturedPaperInitialCandidateRead:
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(timeout=2.0)
        return CapturedPaperInitialCandidateRead(
            user_id=user_id,
            symbol=symbol,
            read_at=decision_at,
            rows=(),
        )


class _FakeIngress:
    def __init__(self, runtime: "_FakeSharedRuntime") -> None:
        self.resource_binding = runtime.resource_binding
        self.shared_admission_budget = runtime.shared_admission_budget
        self.pressure_controller = (
            runtime.ingress_pressure_controller_override
            if runtime.ingress_pressure_controller_override is not None
            else runtime.shared_admission_budget.pressure_controller
        )
        self.dropped = 0
        self.writer_failure_count = 0
        self.writer_failure_reason: str | None = None
        self.post_close_submissions = 0

    def health(self) -> dict[str, object]:
        return {
            "writer_failure_count": self.writer_failure_count,
            "writer_failure_reason": self.writer_failure_reason,
            "dropped": self.dropped,
            "post_close_submissions": self.post_close_submissions,
        }


class _FakeLease:
    def __init__(self, runtime: "_FakeSharedRuntime") -> None:
        self._runtime = runtime
        self.store = runtime.store
        self.released = False

    def health(self) -> dict[str, object]:
        return {"released": self.released}


class _FakeSharedRuntime:
    def __init__(self, root: Path, *, max_writer_threads: int) -> None:
        self.max_writer_threads = max_writer_threads
        self.resource_binding = SimpleNamespace(
            binding_sha256=RESOURCE_SHA,
            hashes={"resource_binding_sha256": RESOURCE_SHA},
            policy=SimpleNamespace(pressure_sample_max_age_seconds=0.05),
            budget=SimpleNamespace(derived_hot_symbol_capacity=9),
        )
        self.shared_admission_budget = SimpleNamespace(
            resource_binding=self.resource_binding,
            pressure_controller=None,
        )
        self.ingress_pressure_controller_override = None
        self.store = SimpleNamespace(root=root)


class _FakePressureController:
    def __init__(
        self,
        binding: object,
        *,
        health_sequence: list[dict[str, object]],
        log: list[str],
    ) -> None:
        assert health_sequence
        self.binding = binding
        self._health_sequence = [dict(item) for item in health_sequence]
        self._log = log
        self.health_calls = 0

    def health(self) -> dict[str, object]:
        self._log.append("pressure_health")
        index = min(self.health_calls, len(self._health_sequence) - 1)
        self.health_calls += 1
        return dict(self._health_sequence[index])


def _pressure_health(
    *,
    clean: bool,
    resource_hashes: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "resource_hashes": resource_hashes
        or {"resource_binding_sha256": RESOURCE_SHA},
        "required_full_fidelity_admissible": clean,
        "pressure_state": "normal" if clean else "failed_closed",
        "rejection_reason": (
            None if clean else "capture_resource_pressure_cpu"
        ),
        "active_reasons": () if clean else ("cpu",),
        "entry_streak": 0,
        "recovery_streak": 0,
        "sample_count": 4,
        "transition_count": 1,
        "last_sample_sha256": "a" * 64,
        "last_observed_at": NOW.isoformat().replace("+00:00", "Z"),
        "sample_age_seconds": 0.001,
    }


class _FakePublisher:
    def __init__(
        self,
        runtime: _FakeSharedRuntime,
        authority: CapturedPaperSelectionAuthority,
        *,
        auto_durable: bool,
        retained_rejection_reasons: list[str] | None = None,
        retained_rejection_hook=None,
    ) -> None:
        self.selection_authority = authority
        self.identity = CaptureRunIdentity(
            run_id=authority.activation_generation,
            generation=1,
            code_build_sha256=authority.code_build_sha256,
            config_sha256=authority.settings_projection_sha256,
            feature_flags_sha256=authority.policy_sha256,
            account_identity_sha256=ACCOUNT_SHA,
            broker="alpaca",
            broker_environment="paper",
        )
        self.writer_lease = _FakeLease(runtime)
        self.ingress = _FakeIngress(runtime)
        self.durable_gate = object()
        self.auto_durable = auto_durable
        self.accepted_through = 0
        self.durable_through = 0
        self.reserved_sequence: int | None = None
        self.poisoned = False
        self.poison_reason: str | None = None
        self.retained_rejection_reasons = list(
            retained_rejection_reasons or ()
        )
        self.admission_callback_reasons: list[str | None] = []
        self.retained_rejection_hook = retained_rejection_hook

    def reserve_sequence(self) -> int:
        assert self.reserved_sequence is None
        self.reserved_sequence = self.accepted_through + 1
        return self.reserved_sequence

    def publish_bundle(self, **kwargs: object) -> SimpleNamespace:
        bundle = kwargs["bundle"]
        assert getattr(bundle, "source_sequence") == self.reserved_sequence
        before_ingress_admission = kwargs.get("before_ingress_admission")
        if (
            before_ingress_admission is None
            and self.retained_rejection_reasons
        ):
            reason = self.retained_rejection_reasons.pop(0)
            self.reserved_sequence = None
            self.poisoned = True
            self.poison_reason = f"queue_ingress_rejected:{reason}"
            return SimpleNamespace(accepted=False)
        if before_ingress_admission is not None:
            assert callable(before_ingress_admission)
            self.admission_callback_reasons.append(None)
            before_ingress_admission(None, 0, None)
            for reason in self.retained_rejection_reasons:
                self.admission_callback_reasons.append(reason)
                if self.retained_rejection_hook is not None:
                    self.retained_rejection_hook(reason)
                before_ingress_admission(None, 0, reason)
        self.accepted_through = int(self.reserved_sequence or 0)
        self.reserved_sequence = None
        if self.auto_durable:
            self.durable_through = self.accepted_through
        return SimpleNamespace(accepted=True)

    def heartbeat(self, *, watermark_at: datetime) -> dict[str, object]:
        assert watermark_at == NOW
        return self.health()

    def poison(self, reason: str) -> SimpleNamespace:
        self.reserved_sequence = None
        self.poisoned = True
        self.poison_reason = reason
        return SimpleNamespace(reason=reason)

    def health(self) -> dict[str, object]:
        return {
            "poisoned": self.poisoned,
            "poison_reason": self.poison_reason,
            "reserved_sequence": self.reserved_sequence,
            "accepted_through": self.accepted_through,
            "durable_through": self.durable_through,
            "ingress": self.ingress.health(),
        }


class _FakeWriter:
    def __init__(self, publisher: _FakePublisher, log: list[str]) -> None:
        self.publisher = publisher
        self.log = log
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.log.append("writer_start")
        self.started = True

    def close(self, *, timeout_seconds: float) -> bool:
        assert timeout_seconds > 0.0
        self.log.append("writer_close")
        self.closed = True
        self.publisher.writer_lease.released = True
        return True

    def health(self) -> dict[str, object]:
        return {
            "queue": self.publisher.health(),
            "writer": {
                "last_error": self.publisher.ingress.writer_failure_reason,
                "writer_alive": self.started and not self.closed,
                "ingress": self.publisher.ingress.health(),
            },
        }


class _FakeInputPort:
    network_fallback_allowed = False
    broker_access_allowed = False
    mutation_allowed = False

    def __init__(
        self,
        runtime: _FakeSharedRuntime,
        publisher: _FakePublisher,
        authority: CapturedPaperSelectionAuthority,
    ) -> None:
        self.root = runtime.store.root
        self.queue_identity = publisher.identity
        self.selection_authority = authority
        self.durable_gate = publisher.durable_gate

    def health(self) -> dict[str, object]:
        return {"poisoned": False, "poison_reason": None}


class _FakeSource:
    network_fallback_allowed = False
    broker_access_allowed = False
    mutation_allowed = False

    def __init__(
        self,
        setup: CapturedPaperSelectionApplicationSetup,
        *,
        generation: int,
        log: list[str],
    ) -> None:
        self.selection_authority = setup.authority
        self.variant_application = setup.application
        self.settings_projection = SimpleNamespace(
            to_dict=lambda: dict(SOURCE_SETTINGS)
        )
        self.capture_identity = CaptureRunIdentity(
            run_id=setup.authority.activation_generation,
            generation=generation,
            code_build_sha256=setup.authority.code_build_sha256,
            config_sha256=SOURCE_SETTINGS_SHA,
            feature_flags_sha256=setup.authority.policy_sha256,
            account_identity_sha256=ACCOUNT_SHA,
            broker="alpaca",
            broker_environment="paper",
        )
        self.log = log
        self.read_count = 0
        self.raise_unavailable_after_prime = False
        self.recovery_snapshot_pending = False
        # First N reads raise a transiently-empty source (the fenced-start warmup
        # case); the (N+1)th read primes.  Default 0 => prime on the first read.
        self.unavailable_before_prime_reads = 0
        self.prime_snapshot_count = 1

    def read_snapshot(self) -> tuple[object, ...]:
        self.log.append("source_read")
        self.read_count += 1
        if self.read_count <= self.unavailable_before_prime_reads:
            raise CapturedPaperSelectionSourceUnavailable(
                "derived_source_current_snapshot_empty"
            )
        prime_read = self.unavailable_before_prime_reads + 1
        if self.read_count > prime_read and self.raise_unavailable_after_prime:
            raise CapturedPaperSelectionSourceUnavailable("provider_unavailable")
        if self.read_count == prime_read or self.recovery_snapshot_pending:
            self.recovery_snapshot_pending = False
            return tuple(object() for _ in range(self.prime_snapshot_count))
        return ()

    def build_occurrence(self, snapshot: object, *, source_sequence: int) -> object:
        assert snapshot is not None
        self.log.append("source_build")
        bundle = SimpleNamespace(
            source_sequence=source_sequence,
            read_at=NOW,
            event_at=NOW,
        )
        return SimpleNamespace(
            bundle=bundle,
            scoring_authority=object(),
            source_events=(object(),),
        )


class _FakeProducer:
    def __init__(
        self,
        authority: CapturedPaperSelectionAuthority,
        input_port: _FakeInputPort,
        publisher: _FakePublisher,
        log: list[str],
    ) -> None:
        self.authority = authority
        self.input_port = input_port
        self.publisher = publisher
        self.log = log
        self.last_sequence = 0

    def tick(self) -> SimpleNamespace:
        self.log.append("producer_tick")
        sequence = self.publisher.durable_through
        status = "applied" if sequence > self.last_sequence else "idle"
        self.last_sequence = sequence
        return SimpleNamespace(
            status=status,
            frontier=SimpleNamespace(
                last_source_sequence=sequence,
                status="ready" if sequence > 0 else "initializing",
                gap_count=0,
            ),
        )


def _ready_producer_result(sequence: int) -> SimpleNamespace:
    return SimpleNamespace(
        status="applied",
        frontier=SimpleNamespace(
            last_source_sequence=sequence,
            status="ready",
            gap_count=0,
        ),
    )


class _Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        max_writer_threads: int = 3,
        auto_durable: bool = True,
        source_generation: int = 2,
        monotonic_clock=time.monotonic,
        poll_interval_seconds: float = 60.0,
        initial_snapshot_warmup_seconds: float = 0.0,
        source_unavailable_before_prime_reads: int = 0,
        wait_for_initial_pressure: bool = False,
        pressure_health_sequence: list[dict[str, object]] | None = None,
        retained_rejection_reasons: list[str] | None = None,
        retained_rejection_hook=None,
    ) -> None:
        self.setup = _application_setup()
        self.runtime = _FakeSharedRuntime(
            tmp_path,
            max_writer_threads=max_writer_threads,
        )
        self.reader = DeferredCapturedPaperInitialCandidateReader(
            expected_reader_type=_FakeInitialReader
        )
        self.log: list[str] = []
        self.pressure_controller: _FakePressureController | None = None
        if pressure_health_sequence is not None:
            self.pressure_controller = _FakePressureController(
                self.runtime.resource_binding,
                health_sequence=pressure_health_sequence,
                log=self.log,
            )
            self.runtime.shared_admission_budget.pressure_controller = (
                self.pressure_controller
            )
        self.fence_calls = 0
        self.setup_calls = 0
        self.component_calls = 0
        self.rollback_calls = 0
        self.publisher: _FakePublisher | None = None
        self.source: _FakeSource | None = None
        self.writer: _FakeWriter | None = None
        self.auto_durable = auto_durable
        self.source_generation = source_generation
        self.source_unavailable_before_prime_reads = (
            source_unavailable_before_prime_reads
        )

        def assert_fence() -> None:
            self.fence_calls += 1
            self.log.append("fence")

        def setup_factory() -> CapturedPaperSelectionApplicationSetup:
            self.setup_calls += 1
            self.log.append("application_setup")
            return self.setup

        def component_factory(
            setup: CapturedPaperSelectionApplicationSetup,
            accounting: object,
            startup_cleanup: CapturedPaperSelectionStartupCleanup,
        ) -> CapturedPaperSelectionRuntimeComponents:
            self.component_calls += 1
            self.log.append("component_factory")
            assert getattr(accounting, "remaining_capture_writer_slots") == (
                max_writer_threads - 1
            )
            publisher = _FakePublisher(
                self.runtime,
                setup.authority,
                auto_durable=self.auto_durable,
                retained_rejection_reasons=retained_rejection_reasons,
                retained_rejection_hook=retained_rejection_hook,
            )
            startup_cleanup.register(
                "writer_lease",
                lambda: setattr(publisher.writer_lease, "released", True),
            )
            writer = _FakeWriter(publisher, self.log)
            startup_cleanup.register(
                "selection_writer",
                lambda: writer.close(timeout_seconds=0.1),
            )
            source = _FakeSource(
                setup,
                generation=self.source_generation,
                log=self.log,
            )
            source.unavailable_before_prime_reads = (
                self.source_unavailable_before_prime_reads
            )
            input_port = _FakeInputPort(self.runtime, publisher, setup.authority)
            producer = _FakeProducer(
                setup.authority,
                input_port,
                publisher,
                self.log,
            )
            initial_reader = _FakeInitialReader(setup.authority)
            self.publisher = publisher
            self.source = source
            self.writer = writer
            return CapturedPaperSelectionRuntimeComponents(
                source=source,
                publisher=publisher,
                writer=writer,
                input_port=input_port,
                producer=producer,
                initial_reader=initial_reader,
                close_source=lambda: self.log.append("source_close"),
            )

        def rollback(
            application: CapturedPaperVariantBindingApplication,
        ) -> dict[str, object]:
            self.rollback_calls += 1
            self.log.append("rollback")
            assert application is self.setup.application
            body: dict[str, object] = {
                "schema_version": (
                    "chili.captured-paper-variant-binding-rollback.v2"
                ),
                "application_sha256": application.application_sha256,
                "application_outcome": "rolled_back",
                "account_scope": "alpaca:paper",
                "expected_account_id": ACCOUNT_ID,
                "activation_generation": ACTIVATION_ID,
                "rolled_back_at": NOW.isoformat().replace("+00:00", "Z"),
                "items": [
                    {
                        "target_variant_id": 21,
                        "target_variant_key": (
                            "captured_paper:momentum_breakout"
                        ),
                        "target_before_sha256": TARGET_SHA,
                        "target_after_sha256": "9" * 64,
                        "deactivated": True,
                    }
                ],
                "paper_order_submission_authorized": False,
                "live_cash_authorized": False,
                "real_money_authorized": False,
            }
            return {**body, "rollback_sha256": sha256_json(body)}

        worker_kwargs: dict[str, object] = {}
        if wait_for_initial_pressure:
            worker_kwargs["wait_for_initial_pressure"] = True
        self.worker = CapturedPaperSelectionLifecycleWorker(
            shared_capture_runtime=self.runtime,  # type: ignore[arg-type]
            deferred_reader=self.reader,
            assert_service_fence_held=assert_fence,
            application_setup_factory=setup_factory,
            component_factory=component_factory,
            rollback_application=rollback,
            poll_interval_seconds=poll_interval_seconds,
            durable_timeout_seconds=0.1,
            producer_timeout_seconds=0.1,
            initial_snapshot_warmup_seconds=initial_snapshot_warmup_seconds,
            monotonic_clock=monotonic_clock,
            **worker_kwargs,
        )


def test_deferred_reader_is_typed_unavailable_before_install_and_after_revoke() -> None:
    setup = _application_setup()
    reader = DeferredCapturedPaperInitialCandidateReader(
        expected_reader_type=_FakeInitialReader
    )
    with pytest.raises(CapturedPaperInitialCandidateReaderUnavailable) as before:
        reader.read_candidates(user_id=1, symbol="AAPL", decision_at=NOW)
    assert "coverage_unavailable" in before.value.reason

    concrete = _FakeInitialReader(setup.authority)
    reader.install(concrete, authority=setup.authority)
    assert reader.read_candidates(
        user_id=1,
        symbol="AAPL",
        decision_at=NOW,
    ).rows == ()
    reader.revoke("test_close")
    with pytest.raises(CapturedPaperInitialCandidateReaderUnavailable) as after:
        reader.read_candidates(user_id=1, symbol="AAPL", decision_at=NOW)
    assert "test_close" in after.value.reason
    with pytest.raises(CapturedPaperSelectionRuntimeError):
        reader.install(concrete, authority=setup.authority)


def test_deferred_reader_revoke_is_nonblocking_and_discards_inflight_read() -> None:
    setup = _application_setup()
    entered = threading.Event()
    release = threading.Event()
    reader = DeferredCapturedPaperInitialCandidateReader(
        expected_reader_type=_FakeInitialReader
    )
    reader.install(
        _FakeInitialReader(setup.authority, entered=entered, release=release),
        authority=setup.authority,
    )
    result: list[CapturedPaperInitialCandidateRead] = []
    errors: list[BaseException] = []

    def read() -> None:
        try:
            result.append(
                reader.read_candidates(user_id=1, symbol="AAPL", decision_at=NOW)
            )
        except BaseException as exc:
            errors.append(exc)

    read_thread = threading.Thread(
        target=read
    )
    read_thread.start()
    assert entered.wait(timeout=1.0)
    revoke_thread = threading.Thread(target=lambda: reader.revoke("atomic_revoke"))
    revoke_thread.start()
    revoke_thread.join(timeout=1.0)
    assert not revoke_thread.is_alive()
    assert read_thread.is_alive()
    release.set()
    read_thread.join(timeout=1.0)
    assert result == []
    assert len(errors) == 1
    assert isinstance(errors[0], CapturedPaperInitialCandidateReaderUnavailable)
    with pytest.raises(CapturedPaperInitialCandidateReaderUnavailable):
        reader.read_candidates(user_id=1, symbol="AAPL", decision_at=NOW)


def test_deferred_reader_pressure_guard_discards_inflight_read() -> None:
    setup = _application_setup()
    entered = threading.Event()
    release = threading.Event()
    pressure = {"clean": True}
    reader = DeferredCapturedPaperInitialCandidateReader(
        expected_reader_type=_FakeInitialReader
    )
    reader.bind_admission_guard(lambda: pressure["clean"])
    reader.install(
        _FakeInitialReader(setup.authority, entered=entered, release=release),
        authority=setup.authority,
    )
    result: list[CapturedPaperInitialCandidateRead] = []
    errors: list[BaseException] = []

    def read() -> None:
        try:
            result.append(
                reader.read_candidates(
                    user_id=1, symbol="AAPL", decision_at=NOW
                )
            )
        except BaseException as exc:
            errors.append(exc)

    read_thread = threading.Thread(target=read)
    read_thread.start()
    assert entered.wait(timeout=1.0)
    pressure["clean"] = False
    release.set()
    read_thread.join(timeout=1.0)

    assert read_thread.is_alive() is False
    assert result == []
    assert len(errors) == 1
    assert isinstance(errors[0], CapturedPaperInitialCandidateReaderUnavailable)
    health = reader.health()
    assert health["admission_guard_bound"] is True
    assert health["suspended"] is True
    assert health["suspend_reason"] == (
        "selection_capture_pressure_unavailable"
    )
    with pytest.raises(CapturedPaperInitialCandidateReaderUnavailable):
        reader.read_candidates(user_id=1, symbol="AAPL", decision_at=NOW)


def test_deferred_reader_suspends_until_same_exact_binding_is_resumed() -> None:
    setup = _application_setup()
    reader = DeferredCapturedPaperInitialCandidateReader(
        expected_reader_type=_FakeInitialReader
    )
    concrete = _FakeInitialReader(setup.authority)
    reader.install(concrete, authority=setup.authority)
    reader.suspend("provider_coverage_unavailable")
    with pytest.raises(CapturedPaperInitialCandidateReaderUnavailable):
        reader.read_candidates(user_id=1, symbol="AAPL", decision_at=NOW)
    assert reader.health()["suspended"] is True
    reader.resume(concrete, authority=setup.authority)
    assert reader.read_candidates(
        user_id=1, symbol="AAPL", decision_at=NOW
    ).rows == ()
    assert reader.health()["installed"] is True


def test_constructor_is_fully_inert_then_prime_precedes_reader_install(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    assert harness.fence_calls == 0
    assert harness.setup_calls == 0
    assert harness.component_calls == 0
    assert harness.reader.health()["installed"] is False

    harness.worker.start()
    health = harness.worker.health()
    assert health["ever_started"] is True
    assert health["running"] is True
    assert health["fatal"] is False
    assert health["ready"] is True
    assert health["last_frontier_sequence"] == 1
    assert health["writer_slot_accounting"] == {
        "max_writer_threads": 3,
        "permanent_selection_writer_slots": 1,
        "remaining_capture_writer_slots": 2,
        "derived_hot_symbol_capacity": 9,
        "resource_binding_sha256": RESOURCE_SHA,
        "strategy_or_exposure_cap": False,
    }
    assert harness.log.index("application_setup") < harness.log.index(
        "component_factory"
    )
    assert harness.log.index("writer_start") < harness.log.index("source_read")
    assert harness.log.index("source_build") < harness.log.index("producer_tick")
    assert harness.reader.health()["installed"] is True
    harness.worker.close(join_timeout_seconds=1.0)


_WARMUP_POLL_TARGET = (
    "app.services.trading.momentum_neural.captured_paper_selection_runtime."
    "_INITIAL_SNAPSHOT_WARMUP_POLL_SECONDS"
)


def _await_worker_health(
    worker: CapturedPaperSelectionLifecycleWorker,
    predicate,
    *,
    timeout_seconds: float = 2.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    health = worker.health()
    while not predicate(health) and time.monotonic() < deadline:
        time.sleep(0.005)
        health = worker.health()
    assert predicate(health), health
    return health


def test_initial_snapshot_warmup_retries_transient_empty_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A host cutover leaves the FIRST fenced-start read transiently empty while
    # the candidate capture lanes re-establish watches and the derived viability
    # refills.  With a warmup window the start tolerates that transient and
    # succeeds once the source primes -- without double-publishing the frontier.
    monkeypatch.setattr(_WARMUP_POLL_TARGET, 0.01)
    harness = _Harness(
        tmp_path,
        initial_snapshot_warmup_seconds=30.0,
        source_unavailable_before_prime_reads=2,
    )
    harness.worker.start()
    health = harness.worker.health()
    assert health["running"] is True
    assert health["ready"] is True
    assert health["fatal"] is False
    assert health["initial_warmup_retries"] == 2
    # exactly one prime publish despite the two retried empty reads (the read
    # raises before any occurrence is built, so re-running cannot double-publish)
    assert harness.log.count("source_build") == 1
    harness.worker.close(join_timeout_seconds=1.0)


def test_initial_snapshot_warmup_fails_closed_after_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Warmup tolerates a transient, it does NOT mask a persistently empty source:
    # once the bounded deadline elapses the fenced start still fails closed.
    monkeypatch.setattr(_WARMUP_POLL_TARGET, 0.01)
    harness = _Harness(
        tmp_path,
        initial_snapshot_warmup_seconds=0.05,
        source_unavailable_before_prime_reads=10**9,
    )
    with pytest.raises(CapturedPaperSelectionRuntimeError) as failure:
        harness.worker.start()
    assert failure.value.code == "START_FAILED"
    health = harness.worker.health()
    assert health["fatal"] is True
    assert health["running"] is False
    assert health["initial_warmup_retries"] >= 1
    # an empty source never publishes an occurrence
    assert "source_build" not in harness.log


def test_initial_snapshot_warmup_zero_is_strict_no_retry(tmp_path: Path) -> None:
    # warmup=0.0 preserves the strict fail-on-first-empty fenced-start contract
    # (the default for callers/tests that must not tolerate any empty read).
    harness = _Harness(
        tmp_path,
        initial_snapshot_warmup_seconds=0.0,
        source_unavailable_before_prime_reads=1,
    )
    with pytest.raises(CapturedPaperSelectionRuntimeError):
        harness.worker.start()
    assert harness.worker.health()["initial_warmup_retries"] == 0


def test_initial_pressure_wait_precedes_writer_and_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_WARMUP_POLL_TARGET, 0.001)
    entering = _pressure_health(clean=True)
    entering["entry_streak"] = 1
    harness = _Harness(
        tmp_path,
        initial_snapshot_warmup_seconds=1.0,
        wait_for_initial_pressure=True,
        pressure_health_sequence=[
            entering,
            _pressure_health(clean=False),
            _pressure_health(clean=True),
            _pressure_health(clean=True),
            _pressure_health(clean=True),
        ],
    )

    harness.worker.start()
    _await_worker_health(harness.worker, lambda health: health["ready"] is True)

    assert harness.pressure_controller is not None
    assert harness.pressure_controller.health_calls >= 3
    assert harness.log.index("pressure_health") < harness.log.index(
        "writer_start"
    )
    assert harness.log.index("writer_start") < harness.log.index("source_read")
    assert harness.source is not None
    assert harness.source.read_count == 1
    assert harness.publisher is not None
    assert harness.publisher.accepted_through == 1
    harness.worker.close(join_timeout_seconds=1.0)


def test_initial_pressure_suspends_then_recovers_without_order_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_WARMUP_POLL_TARGET, 0.001)
    harness = _Harness(
        tmp_path,
        initial_snapshot_warmup_seconds=0.01,
        wait_for_initial_pressure=True,
        pressure_health_sequence=[_pressure_health(clean=False)],
    )

    harness.worker.start()
    try:
        suspended = harness.worker.health()
        assert suspended["running"] is True
        assert suspended["fatal"] is False
        assert suspended["ready"] is False
        assert suspended["admission_suspended"] is True
        assert suspended["candidate_reader"]["installed"] is False
        assert harness.source is not None
        assert harness.source.read_count == 0
        assert harness.writer is not None
        assert harness.writer.started is True
        assert harness.publisher is not None
        assert harness.publisher.reserved_sequence is None
        assert harness.publisher.accepted_through == 0
        assert harness.publisher.poisoned is False

        assert harness.pressure_controller is not None
        harness.pressure_controller._health_sequence = [
            _pressure_health(clean=True)
        ]
        harness.pressure_controller.health_calls = 0
        recovered = _await_worker_health(
            harness.worker, lambda health: health["ready"] is True
        )
        assert recovered["fatal"] is False
        assert recovered["admission_suspended"] is False
        assert harness.source.read_count == 1
        assert harness.publisher.accepted_through == 1
        assert harness.publisher.poisoned is False
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_close_racing_async_initial_reader_install_is_orderly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(
        tmp_path,
        initial_snapshot_warmup_seconds=1.0,
        wait_for_initial_pressure=True,
        pressure_health_sequence=[_pressure_health(clean=True)],
    )
    entered = threading.Event()
    release = threading.Event()
    original_install = harness.reader.install

    def blocked_install(reader, *, authority) -> None:
        entered.set()
        assert release.wait(timeout=2.0)
        original_install(reader, authority=authority)

    monkeypatch.setattr(harness.reader, "install", blocked_install)
    harness.worker.start()
    assert entered.wait(timeout=2.0)

    close_errors: list[BaseException] = []

    def close_worker() -> None:
        try:
            harness.worker.close(join_timeout_seconds=1.0)
        except BaseException as exc:  # pragma: no cover - assertion captures it
            close_errors.append(exc)

    closer = threading.Thread(target=close_worker, daemon=False)
    closer.start()
    deadline = time.monotonic() + 2.0
    while (
        not harness.reader.health()["revoked"]
        and time.monotonic() < deadline
    ):
        time.sleep(0.005)
    assert harness.reader.health()["revoked"] is True
    release.set()
    closer.join(timeout=2.0)

    assert closer.is_alive() is False
    assert close_errors == []
    health = harness.worker.health()
    assert health["state"] == "quiesced"
    assert health["quiesced"] is True
    assert health["fatal"] is False
    assert harness.publisher is not None
    assert harness.publisher.poisoned is False


def test_initial_pressure_binding_mismatch_rejects_before_writer(
    tmp_path: Path,
) -> None:
    harness = _Harness(
        tmp_path,
        initial_snapshot_warmup_seconds=1.0,
        wait_for_initial_pressure=True,
        pressure_health_sequence=[_pressure_health(clean=True)],
    )
    foreign = _FakePressureController(
        harness.runtime.resource_binding,
        health_sequence=[_pressure_health(clean=True)],
        log=harness.log,
    )
    harness.runtime.ingress_pressure_controller_override = (
        harness.pressure_controller
    )
    harness.runtime.shared_admission_budget.pressure_controller = foreign

    with pytest.raises(CapturedPaperSelectionRuntimeError) as failure:
        harness.worker.start()

    assert failure.value.code == "INITIAL_INGRESS_PRESSURE_BINDING_INVALID"
    assert harness.source is not None
    assert harness.source.read_count == 0
    assert harness.writer is not None
    assert harness.writer.started is False


def test_pressure_return_after_source_read_retains_then_recovers(
    tmp_path: Path,
) -> None:
    harness = _Harness(
        tmp_path,
        initial_snapshot_warmup_seconds=1.0,
        wait_for_initial_pressure=True,
        pressure_health_sequence=[
            _pressure_health(clean=True),
            _pressure_health(clean=True),
            _pressure_health(clean=True),
            _pressure_health(clean=False),
        ],
    )

    harness.worker.start()
    try:
        assert harness.source is not None
        assert harness.publisher is not None
        _await_worker_health(
            harness.worker,
            lambda health: (
                health["running"] is True
                and health["ready"] is False
                and harness.source.read_count == 1
            ),
        )
        assert harness.publisher.accepted_through == 0
        assert harness.publisher.poisoned is False

        assert harness.pressure_controller is not None
        harness.pressure_controller._health_sequence = [
            _pressure_health(clean=True)
        ]
        harness.pressure_controller.health_calls = 0
        recovered = _await_worker_health(
            harness.worker, lambda health: health["ready"] is True
        )
        assert recovered["fatal"] is False
        assert harness.source.read_count == 1
        assert harness.publisher.accepted_through == 1
        assert harness.publisher.poisoned is False
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_pressure_return_inside_publish_waits_before_ingress_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_WARMUP_POLL_TARGET, 0.001)
    harness = _Harness(
        tmp_path,
        initial_snapshot_warmup_seconds=1.0,
        wait_for_initial_pressure=True,
        pressure_health_sequence=[
            _pressure_health(clean=True),
            _pressure_health(clean=True),
            _pressure_health(clean=True),
            _pressure_health(clean=True),
            _pressure_health(clean=True),
            # The existing runtime check has passed.  This transition models
            # pressure returning while publish_bundle performs scoring and
            # hashes the immutable envelope, before its ingress.submit call.
            _pressure_health(clean=False),
            _pressure_health(clean=True),
        ],
    )

    harness.worker.start()
    _await_worker_health(harness.worker, lambda health: health["ready"] is True)
    assert harness.pressure_controller is not None
    health_calls = harness.pressure_controller.health_calls
    assert harness.publisher is not None
    accepted_through = harness.publisher.accepted_through
    poisoned = harness.publisher.poisoned
    harness.worker.close(join_timeout_seconds=1.0)

    assert health_calls >= 7
    assert accepted_through == 1
    assert poisoned is False


def test_retained_initial_ingress_retries_without_rereading_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_WARMUP_POLL_TARGET, 0.001)
    reason = "shared_capture_write_bandwidth_budget_exceeded"
    harness = _Harness(
        tmp_path,
        initial_snapshot_warmup_seconds=1.0,
        wait_for_initial_pressure=True,
        pressure_health_sequence=[_pressure_health(clean=True)],
        retained_rejection_reasons=[reason],
    )

    harness.worker.start()
    _await_worker_health(harness.worker, lambda health: health["ready"] is True)

    assert harness.source is not None
    assert harness.source.read_count == 1
    assert harness.log.count("source_build") == 1
    assert harness.publisher is not None
    assert harness.publisher.admission_callback_reasons == [None, reason]
    assert harness.publisher.accepted_through == 1
    assert harness.publisher.poisoned is False
    harness.worker.close(join_timeout_seconds=1.0)


def test_periodic_pressure_retries_retained_occurrence_without_poison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_WARMUP_POLL_TARGET, 0.001)
    reason = "capture_resource_pressure_write_latency"
    harness = _Harness(
        tmp_path,
        initial_snapshot_warmup_seconds=1.0,
        wait_for_initial_pressure=True,
        pressure_health_sequence=[_pressure_health(clean=True)],
    )
    harness.worker.start()
    _await_worker_health(harness.worker, lambda health: health["ready"] is True)
    assert harness.publisher is not None
    assert harness.source is not None
    assert harness.pressure_controller is not None

    suspensions: list[tuple[str, bool]] = []
    suspend = harness.reader.suspend

    def record_suspend(reason: str) -> None:
        suspend(reason)
        suspensions.append((reason, harness.reader.health()["suspended"]))

    monkeypatch.setattr(harness.reader, "suspend", record_suspend)
    harness.publisher.retained_rejection_reasons = [reason]
    harness.publisher.admission_callback_reasons = []
    harness.source.recovery_snapshot_pending = True
    harness.pressure_controller._health_sequence = [_pressure_health(clean=True)]
    harness.pressure_controller.health_calls = 0

    harness.worker._run_cycle(initial=False)

    assert harness.source.read_count == 2
    assert harness.log.count("source_build") == 2
    assert harness.publisher.admission_callback_reasons == [None, reason]
    assert suspensions == [
        ("selection_producer_frontier_pending", True),
        ("selection_capture_pressure_unavailable", True),
    ]
    assert harness.publisher.accepted_through == 2
    assert harness.publisher.poisoned is False
    assert harness.reader.health()["installed"] is True
    assert harness.worker.health()["fatal"] is False
    harness.worker.close(join_timeout_seconds=1.0)


def test_periodic_pressure_retains_one_event_across_multiple_retry_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_WARMUP_POLL_TARGET, 0.001)
    reason = "capture_resource_pressure_write_latency"
    clock = _RetainedDeadlineClock(step=0.02)
    harness = _Harness(
        tmp_path,
        monotonic_clock=clock,
        initial_snapshot_warmup_seconds=1.0,
        wait_for_initial_pressure=True,
        pressure_health_sequence=[_pressure_health(clean=True)],
    )
    harness.worker.start()
    _await_worker_health(harness.worker, lambda health: health["ready"] is True)
    assert harness.publisher is not None
    assert harness.source is not None
    assert harness.pressure_controller is not None

    harness.publisher.retained_rejection_reasons = [reason]
    harness.publisher.retained_rejection_hook = clock.arm
    harness.publisher.admission_callback_reasons = []
    harness.source.recovery_snapshot_pending = True
    harness.pressure_controller._health_sequence = [
        *[_pressure_health(clean=False) for _ in range(10)],
        _pressure_health(clean=True),
    ]
    harness.pressure_controller.health_calls = 0

    harness.worker._run_cycle(initial=False)

    health = harness.worker.health()
    assert health["periodic_ingress_retry_windows_expired"] >= 2
    assert health["fatal"] is False
    assert health["ready"] is True
    assert harness.source.read_count == 2
    assert harness.log.count("source_build") == 2
    assert harness.publisher.admission_callback_reasons == [None, reason]
    assert harness.publisher.accepted_through == 2
    assert harness.publisher.poisoned is False
    assert harness.reader.health()["installed"] is True
    harness.worker.close(join_timeout_seconds=1.0)


def test_periodic_pressure_suspends_before_source_and_recovers_on_new_frontier(
    tmp_path: Path,
) -> None:
    harness = _Harness(
        tmp_path,
        poll_interval_seconds=0.01,
        initial_snapshot_warmup_seconds=1.0,
        wait_for_initial_pressure=True,
        pressure_health_sequence=[_pressure_health(clean=True)],
    )
    harness.worker.start()
    try:
        _await_worker_health(
            harness.worker, lambda health: health["ready"] is True
        )
        assert harness.publisher is not None
        assert harness.source is not None
        assert harness.pressure_controller is not None
        initial_reads = harness.source.read_count
        initial_accepted = harness.publisher.accepted_through

        harness.source.recovery_snapshot_pending = True
        harness.pressure_controller._health_sequence = [
            _pressure_health(clean=False)
        ]
        harness.pressure_controller.health_calls = 0
        _await_worker_health(
            harness.worker,
            lambda health: (
                health["candidate_reader"]["suspended"] is True
            ),
        )
        assert harness.source.read_count == initial_reads
        assert harness.publisher.accepted_through == initial_accepted
        assert harness.publisher.poisoned is False

        harness.pressure_controller._health_sequence = [
            _pressure_health(clean=True)
        ]
        harness.pressure_controller.health_calls = 0
        recovered = _await_worker_health(
            harness.worker,
            lambda health: (
                health["ready"] is True
                and harness.publisher.accepted_through
                == initial_accepted + 1
            ),
        )
        assert recovered["fatal"] is False
        assert harness.source.read_count == initial_reads + 1
        assert harness.publisher.poisoned is False
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_periodic_pressure_suspension_closes_without_runtime_fatal(
    tmp_path: Path,
) -> None:
    reason = "capture_resource_pressure_write_latency"
    harness = _Harness(
        tmp_path,
        poll_interval_seconds=0.01,
        initial_snapshot_warmup_seconds=1.0,
        wait_for_initial_pressure=True,
        pressure_health_sequence=[_pressure_health(clean=True)],
    )
    harness.worker.start()
    _await_worker_health(harness.worker, lambda health: health["ready"] is True)
    assert harness.publisher is not None
    assert harness.source is not None
    assert harness.pressure_controller is not None

    harness.publisher.retained_rejection_reasons = [reason]
    harness.source.recovery_snapshot_pending = True
    harness.pressure_controller._health_sequence = [
        _pressure_health(clean=False)
    ]
    harness.pressure_controller.health_calls = 0

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if harness.reader.health()["suspended"]:
            break
        time.sleep(0.005)
    assert harness.reader.health()["suspended"] is True
    assert harness.publisher.reserved_sequence is None
    assert harness.source.read_count == 1
    assert harness.log.count("source_build") == 1

    harness.worker.close(join_timeout_seconds=1.0)

    health = harness.worker.health()
    assert health["state"] == "quiesced"
    assert health["quiesced"] is True
    assert health["thread_alive"] is False
    assert health["fatal"] is False
    assert health["stop_requested"] is True
    assert harness.publisher.accepted_through == 1
    assert harness.publisher.poisoned is False
    assert harness.publisher.poison_reason is None
    assert harness.reader.health()["revoked"] is True


def test_quiesced_retained_abandonment_does_not_hide_writer_failure(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    harness.worker.start()
    assert harness.publisher is not None
    harness.worker.close(join_timeout_seconds=1.0)
    harness.publisher.poisoned = True
    harness.publisher.poison_reason = "selection_source_batch_incomplete"
    harness.publisher.ingress.writer_failure_count = 1
    harness.publisher.ingress.writer_failure_reason = "injected_writer_failure"

    health = harness.worker.health()

    assert health["state"] == "quiesced"
    assert health["fatal"] is True
    assert "selection_queue_or_writer_fatal" in str(health["fatal_reason"])


def test_retained_initial_ingress_pressure_recovers_without_reread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_WARMUP_POLL_TARGET, 0.001)
    reason = "shared_capture_write_bandwidth_budget_exceeded"
    clock = _RetainedDeadlineClock(step=0.01)
    harness = _Harness(
        tmp_path,
        monotonic_clock=clock,
        initial_snapshot_warmup_seconds=1.0,
        wait_for_initial_pressure=True,
        pressure_health_sequence=[_pressure_health(clean=True)],
        retained_rejection_reasons=[reason] * 3,
        retained_rejection_hook=clock.arm,
    )

    harness.worker.start()
    try:
        recovered = _await_worker_health(
            harness.worker, lambda health: health["ready"] is True
        )
        assert recovered["fatal"] is False
        assert harness.source is not None
        assert harness.source.read_count == 1
        assert harness.log.count("source_build") == 1
        assert harness.publisher is not None
        assert harness.publisher.admission_callback_reasons[0] is None
        assert set(harness.publisher.admission_callback_reasons[1:]) == {
            reason
        }
        assert harness.publisher.accepted_through == 1
        assert harness.publisher.poisoned is False
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_initial_multi_snapshot_batch_waits_again_before_each_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_WARMUP_POLL_TARGET, 0.001)
    harness = _Harness(
        tmp_path,
        initial_snapshot_warmup_seconds=1.0,
        wait_for_initial_pressure=True,
        pressure_health_sequence=[
            _pressure_health(clean=True),
            _pressure_health(clean=True),
            _pressure_health(clean=True),
            _pressure_health(clean=True),
            _pressure_health(clean=True),
            _pressure_health(clean=False),
            _pressure_health(clean=True),
            _pressure_health(clean=True),
        ],
    )
    assert harness.source is None

    original_factory = harness.worker.component_factory

    def two_snapshot_factory(*args: object) -> object:
        components = original_factory(*args)
        assert harness.source is not None
        harness.source.prime_snapshot_count = 2
        return components

    harness.worker.component_factory = two_snapshot_factory
    harness.worker.start()
    _await_worker_health(harness.worker, lambda health: health["ready"] is True)

    assert harness.pressure_controller is not None
    assert harness.pressure_controller.health_calls >= 8
    assert harness.publisher is not None
    assert harness.publisher.accepted_through == 2
    assert harness.publisher.poisoned is False
    harness.worker.close(join_timeout_seconds=1.0)


def test_hash_bound_not_applied_outcome_never_builds_runtime_or_calls_rollback(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)

    def not_applied() -> CapturedPaperSelectionApplicationSetup:
        raise CapturedPaperSelectionApplicationNotApplied(_not_applied_proof())

    harness.worker.application_setup_factory = not_applied
    with pytest.raises(CapturedPaperSelectionApplicationNotApplied):
        harness.worker.start()

    assert harness.component_calls == 0
    assert harness.reader.health()["installed"] is False
    health = harness.worker.health()
    assert health["application_outcome"] == "not_applied"
    assert health["not_applied_sha256"] == _not_applied_proof()[
        "not_applied_sha256"
    ]
    receipt = harness.worker.rollback_after_quiesce()
    assert receipt["application_outcome"] == "not_applied"
    assert receipt["target_variant_ids"] == []
    assert receipt["strategy_variants_deactivated"] is False
    assert harness.rollback_calls == 0


def test_ambiguous_application_outcome_retains_exact_setup_without_starting(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)

    def ambiguous() -> CapturedPaperSelectionApplicationSetup:
        raise CapturedPaperSelectionApplicationOutcomeAmbiguous(harness.setup)

    harness.worker.application_setup_factory = ambiguous
    with pytest.raises(CapturedPaperSelectionApplicationOutcomeAmbiguous):
        harness.worker.start()

    assert harness.component_calls == 0
    assert harness.reader.health()["installed"] is False
    assert harness.worker.application is harness.setup.application
    assert harness.worker.health()["application_outcome"] == "ambiguous"
    receipt = harness.worker.rollback_after_quiesce()
    assert receipt["application_outcome"] == "rolled_back"
    assert receipt["strategy_variants_deactivated"] is True
    assert harness.rollback_calls == 1


def test_invalid_not_applied_proof_is_rejected_without_runtime_or_rollback(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    forged = _not_applied_proof()
    forged["expected_account_id"] = "not-a-uuid"
    forged.pop("not_applied_sha256")
    forged["not_applied_sha256"] = sha256_json(forged)

    def invalid() -> CapturedPaperSelectionApplicationSetup:
        raise CapturedPaperSelectionApplicationNotApplied(forged)

    harness.worker.application_setup_factory = invalid
    with pytest.raises(CapturedPaperSelectionRuntimeError) as rejected:
        harness.worker.start()

    assert rejected.value.code == "APPLICATION_NOT_APPLIED_PROOF_INVALID"
    assert harness.component_calls == 0
    assert harness.rollback_calls == 0
    assert harness.reader.health()["installed"] is False


def test_close_quiesces_releases_and_exact_hash_bound_rollback_is_one_shot(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    harness.worker.start()
    harness.worker.close(join_timeout_seconds=1.0)
    assert harness.reader.health()["revoked"] is True
    assert harness.publisher is not None
    assert harness.publisher.writer_lease.released is True
    assert harness.log.index("source_close") < harness.log.index("writer_close")

    receipt = harness.worker.rollback_after_quiesce()
    assert receipt["variant_application_sha256"] == (
        harness.setup.application.application_sha256
    )
    assert receipt["variant_rollback_sha256"]
    assert receipt["target_variant_ids"] == [21]
    assert receipt["strategy_variants_deactivated"] is True
    assert receipt["paper_order_submission_authorized"] is False
    assert receipt["live_cash_authorized"] is False
    assert harness.rollback_calls == 1
    assert harness.worker.rollback_after_quiesce() == receipt
    assert harness.rollback_calls == 1


def test_measured_writer_accounting_fails_before_clone_setup_when_residual_is_zero(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, max_writer_threads=1)
    with pytest.raises(CapturedPaperSelectionRuntimeError) as raised:
        harness.worker.start()
    assert raised.value.code == "SELECTION_WRITER_CAPACITY_UNAVAILABLE"
    assert harness.setup_calls == 0
    assert harness.component_calls == 0
    assert harness.reader.health()["revoked"] is True


def test_source_generation_mismatch_fails_before_writer_start_and_retains_application(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, source_generation=1)
    with pytest.raises(CapturedPaperSelectionRuntimeError) as raised:
        harness.worker.start()
    assert raised.value.code == "COMPONENT_IDENTITY_INVALID"
    assert harness.writer is not None
    assert harness.writer.started is False
    assert harness.writer.closed is True
    assert harness.worker.application is harness.setup.application
    assert harness.reader.health()["installed"] is False


def test_component_factory_partial_failure_runs_registered_cleanup_and_retains_application(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    cleaned: list[str] = []

    def fail_after_acquire(
        _setup: CapturedPaperSelectionApplicationSetup,
        _accounting: object,
        startup_cleanup: CapturedPaperSelectionStartupCleanup,
    ) -> CapturedPaperSelectionRuntimeComponents:
        startup_cleanup.register("partial_lease", lambda: cleaned.append("lease"))
        raise OSError("component construction failed")

    harness.worker.component_factory = fail_after_acquire
    with pytest.raises(CapturedPaperSelectionRuntimeError) as raised:
        harness.worker.start()
    assert raised.value.code == "START_FAILED"
    assert cleaned == ["lease"]
    assert harness.worker.application is harness.setup.application
    assert harness.worker.health()["quiesced"] is True


def test_missing_durable_ack_never_installs_reader_and_application_remains_rollbackable(
    tmp_path: Path,
) -> None:
    ticks = iter((0.0, 1.0, 2.0, 3.0, 4.0, 5.0))
    harness = _Harness(
        tmp_path,
        auto_durable=False,
        monotonic_clock=lambda: next(ticks, 99.0),
    )
    with pytest.raises(CapturedPaperSelectionRuntimeError):
        harness.worker.start()
    assert harness.reader.health()["installed"] is False
    assert harness.writer is not None and harness.writer.closed is True
    assert harness.worker.application is harness.setup.application
    assert harness.worker.health()["quiesced"] is True
    receipt = harness.worker.rollback_after_quiesce()
    assert receipt["variant_application_sha256"] == (
        harness.setup.application.application_sha256
    )


def test_durable_wait_allows_bounded_stall_timeout_to_reset_on_forward_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)
    harness.worker.start()
    assert harness.publisher is not None
    publisher = harness.publisher
    publisher.accepted_through = 4
    publisher.durable_through = 1

    class _ProgressClock:
        now = 0.0

        def __call__(self) -> float:
            self.now += 0.06
            return self.now

    clock = _ProgressClock()
    harness.worker.monotonic_clock = clock
    original_health = publisher.health

    def progressive_health() -> dict[str, object]:
        if publisher.durable_through < publisher.accepted_through:
            publisher.durable_through += 1
        return original_health()

    monkeypatch.setattr(publisher, "health", progressive_health)
    monkeypatch.setattr(harness.worker, "_assert_runtime_health", lambda: None)

    # Total elapsed time is greater than the 0.1-second configured timeout, but
    # every observation advances the exact durable frontier.  This models a
    # large immutable snapshot whose writer remains healthy while fsyncing
    # multiple bounded batches.
    assert harness.worker._wait_for_durable_frontier() == 4
    harness.worker.close(join_timeout_seconds=1.0)


def test_durable_wait_rejects_a_backward_frontier_before_equal_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)
    harness.worker.start()
    assert harness.publisher is not None
    observations = iter(
        (
            {
                "accepted_through": 4,
                "durable_through": 2,
                "reserved_sequence": None,
            },
            {
                "accepted_through": 1,
                "durable_through": 1,
                "reserved_sequence": None,
            },
        )
    )
    monkeypatch.setattr(
        harness.worker,
        "_publisher_health",
        lambda: next(observations),
    )
    monkeypatch.setattr(harness.worker, "_assert_runtime_health", lambda: None)
    monkeypatch.setattr(harness.worker, "monotonic_clock", lambda: 0.0)

    with pytest.raises(CapturedPaperSelectionRuntimeError) as failure:
        harness.worker._wait_for_durable_frontier()
    assert failure.value.code == "DURABLE_FRONTIER_INVALID"
    harness.worker.close(join_timeout_seconds=1.0)


def test_durable_wait_rejects_a_backward_frontier_across_retry_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)
    harness.worker.start()
    assert harness.publisher is not None
    publisher = harness.publisher
    publisher.accepted_through = 4
    publisher.durable_through = 2
    observations = iter((0.0, 0.2))
    monkeypatch.setattr(
        harness.worker,
        "monotonic_clock",
        lambda: next(observations),
    )
    monkeypatch.setattr(harness.worker, "_assert_runtime_health", lambda: None)

    try:
        with pytest.raises(CapturedPaperSelectionRuntimeError) as timeout:
            harness.worker._wait_for_durable_frontier()
        assert timeout.value.code == "DURABLE_FRONTIER_TIMEOUT"

        publisher.durable_through = 1
        monkeypatch.setattr(harness.worker, "monotonic_clock", lambda: 0.0)
        with pytest.raises(CapturedPaperSelectionRuntimeError) as backward:
            harness.worker._wait_for_durable_frontier()
        assert backward.value.code == "DURABLE_FRONTIER_INVALID"
    finally:
        publisher.durable_through = publisher.accepted_through
        harness.worker.close(join_timeout_seconds=1.0)


def test_durable_wait_rejects_nonfinite_clock_as_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)
    harness.worker.start()
    assert harness.publisher is not None
    publisher = harness.publisher
    publisher.accepted_through = 2
    publisher.durable_through = 1
    observations = iter((float("nan"), 0.0))
    monkeypatch.setattr(
        harness.worker,
        "monotonic_clock",
        lambda: next(observations),
    )
    monkeypatch.setattr(harness.worker, "_assert_runtime_health", lambda: None)

    try:
        with pytest.raises(CapturedPaperSelectionRuntimeError) as failure:
            harness.worker._wait_for_durable_frontier()
        assert failure.value.code == "DURABLE_CLOCK_INVALID"
    finally:
        publisher.durable_through = publisher.accepted_through
        harness.worker.close(join_timeout_seconds=1.0)


def test_durable_progress_never_extends_the_absolute_initial_cycle_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)
    harness.worker.start()
    assert harness.publisher is not None
    publisher = harness.publisher
    publisher.accepted_through = 4
    publisher.durable_through = 0

    class _InitialDeadlineClock:
        now = -0.06

        def __call__(self) -> float:
            self.now += 0.06
            return self.now

    clock = _InitialDeadlineClock()
    harness.worker.monotonic_clock = clock
    original_health = publisher.health

    def progressive_health() -> dict[str, object]:
        if publisher.durable_through < publisher.accepted_through:
            publisher.durable_through += 1
        return original_health()

    monkeypatch.setattr(publisher, "health", progressive_health)
    monkeypatch.setattr(harness.worker, "_assert_runtime_health", lambda: None)

    with pytest.raises(CapturedPaperSelectionRuntimeError) as failure:
        harness.worker._wait_for_durable_frontier(
            initial_cycle_deadline=0.15,
        )
    assert failure.value.code == "DURABLE_FRONTIER_TIMEOUT"
    harness.worker.close(join_timeout_seconds=1.0)


def test_producer_wait_renews_stall_timeout_on_forward_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)
    harness.worker.start()
    components = harness.worker._components
    assert components is not None
    sequences = iter((1, 2, 3, 4))
    observations = iter((0.04, 0.08, 0.12, 0.16, 0.20))
    monkeypatch.setattr(
        components.producer,
        "tick",
        lambda: _ready_producer_result(next(sequences)),
    )
    harness.worker.monotonic_clock = lambda: next(observations)

    # Total elapsed time exceeds the 0.1-second producer timeout, but every
    # observation advances the exact gap-free frontier.
    try:
        frontier = harness.worker._drain_producer_to(4)
        assert frontier.last_source_sequence == 4
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_producer_progress_never_extends_absolute_initial_cycle_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)
    harness.worker.start()
    components = harness.worker._components
    assert components is not None
    sequences = iter((1, 2, 3, 4))
    observations = iter((0.0, 0.04, 0.08, 0.12, 0.16))
    monkeypatch.setattr(
        components.producer,
        "tick",
        lambda: _ready_producer_result(next(sequences)),
    )
    harness.worker.monotonic_clock = lambda: next(observations)

    try:
        with pytest.raises(CapturedPaperSelectionRuntimeError) as failure:
            harness.worker._drain_producer_to(
                4,
                initial_cycle_deadline=0.15,
            )
        assert failure.value.code == "PRODUCER_FRONTIER_TIMEOUT"
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_producer_target_completed_after_stall_deadline_is_not_false_timed_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)
    harness.worker.start()
    components = harness.worker._components
    assert components is not None
    observations = iter((0.0, 0.11))
    harness.worker.monotonic_clock = lambda: next(observations)
    monkeypatch.setattr(
        components.producer,
        "tick",
        lambda: _ready_producer_result(4),
    )

    # A producer tick can spend longer than the stall window inside one
    # authoritative DB transaction and still return the exact ready target.
    # That is completed work, not a stalled frontier.  A separate finite
    # initial-cycle deadline remains an absolute startup bound.
    try:
        frontier = harness.worker._drain_producer_to(4)
        assert frontier.last_source_sequence == 4
        assert frontier.status == "ready"
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_producer_wait_rejects_a_backward_frontier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)
    harness.worker.start()
    components = harness.worker._components
    assert components is not None
    sequences = iter((2, 1))
    monkeypatch.setattr(
        components.producer,
        "tick",
        lambda: _ready_producer_result(next(sequences)),
    )
    harness.worker.monotonic_clock = lambda: 0.0

    try:
        with pytest.raises(CapturedPaperSelectionRuntimeError) as failure:
            harness.worker._drain_producer_to(4)
        assert failure.value.code == "PRODUCER_FRONTIER_INVALID"
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_producer_target_after_absolute_initial_deadline_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)
    harness.worker.start()
    components = harness.worker._components
    assert components is not None
    observations = iter((0.0, 0.2))
    harness.worker.producer_timeout_seconds = 1.0
    harness.worker.monotonic_clock = lambda: next(observations)
    monkeypatch.setattr(
        components.producer,
        "tick",
        lambda: _ready_producer_result(4),
    )

    try:
        with pytest.raises(CapturedPaperSelectionRuntimeError) as failure:
            harness.worker._drain_producer_to(
                4,
                initial_cycle_deadline=0.15,
            )
        assert failure.value.code == "PRODUCER_FRONTIER_TIMEOUT"
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_producer_late_progress_cannot_resurrect_expired_stall_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)
    harness.worker.start()
    components = harness.worker._components
    assert components is not None
    sequences = iter((1, 2, 3))
    observations = iter((0.0, 0.05, 0.11, 0.12))
    monkeypatch.setattr(
        components.producer,
        "tick",
        lambda: _ready_producer_result(next(sequences)),
    )
    harness.worker.monotonic_clock = lambda: next(observations)

    try:
        with pytest.raises(CapturedPaperSelectionRuntimeError) as failure:
            harness.worker._drain_producer_to(3)
        assert failure.value.code == "PRODUCER_FRONTIER_TIMEOUT"
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_producer_applied_status_without_sequence_progress_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)
    harness.worker.start()
    components = harness.worker._components
    assert components is not None
    observations = iter((0.0, 0.05, 0.11))
    monkeypatch.setattr(
        components.producer,
        "tick",
        lambda: _ready_producer_result(1),
    )
    harness.worker.monotonic_clock = lambda: next(observations)

    try:
        with pytest.raises(CapturedPaperSelectionRuntimeError) as failure:
            harness.worker._drain_producer_to(3)
        assert failure.value.code == "PRODUCER_FRONTIER_TIMEOUT"
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_periodic_producer_timeout_suspends_then_recovers_same_frontier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=0.2)
    harness.worker.producer_timeout_seconds = 0.05
    harness.worker.start()
    assert harness.source is not None
    assert harness.publisher is not None
    components = harness.worker._components
    assert components is not None

    release_frontier = threading.Event()

    def stalled_then_ready() -> SimpleNamespace:
        if release_frontier.is_set():
            return _ready_producer_result(harness.publisher.durable_through)
        return _ready_producer_result(1)

    monkeypatch.setattr(components.producer, "tick", stalled_then_ready)
    harness.source.recovery_snapshot_pending = True

    try:
        suspended = _await_worker_health(
            harness.worker,
            lambda health: (
                health.get("producer_frontier_timeout_cycles", 0) >= 2
            ),
        )
        assert suspended["running"] is True
        assert suspended["fatal"] is False
        assert suspended["ready"] is False
        assert suspended["candidate_reader"]["suspended"] is True
        assert suspended["candidate_reader"]["suspend_reason"] == (
            "selection_producer_frontier_pending"
        )
        assert harness.source.read_count == 2
        assert harness.publisher.accepted_through == 2
        assert suspended["occurrences_published"] == 2
        assert harness.publisher.poisoned is False
        with pytest.raises(CapturedPaperInitialCandidateReaderUnavailable):
            harness.reader.read_candidates(
                user_id=1,
                symbol="AAPL",
                decision_at=NOW,
            )

        release_frontier.set()
        recovered = _await_worker_health(
            harness.worker,
            lambda health: (
                health["ready"] is True
                and health["last_frontier_sequence"] == 2
            ),
        )
        assert recovered["running"] is True
        assert recovered["fatal"] is False
        assert recovered["producer_frontier_timeout_cycles"] >= 1
        assert recovered["candidate_reader"]["installed"] is True
        assert harness.source.read_count == 2
        assert harness.publisher.accepted_through == 2
        assert recovered["occurrences_published"] == 2
        assert harness.publisher.poisoned is False
    finally:
        release_frontier.set()
        harness.worker.close(join_timeout_seconds=1.0)


def test_periodic_transient_producer_database_failure_suspends_then_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)
    harness.worker.start()
    assert harness.publisher is not None
    components = harness.worker._components
    assert components is not None
    original_tick = components.producer.tick
    attempts = 0

    def transient_then_ready() -> SimpleNamespace:
        nonlocal attempts
        attempts += 1
        if attempts in {1, 3}:
            raise OperationalError(
                "SELECT captured_paper_selection_frontier",
                {},
                TimeoutError("statement timeout"),
            )
        if attempts == 2:
            reader_health = harness.reader.health()
            assert reader_health["suspended"] is True
            assert reader_health["suspend_reason"] == (
                "selection_producer_frontier_pending"
            )
        if attempts == 4:
            reader_health = harness.reader.health()
            assert reader_health["suspended"] is True
            assert reader_health["suspend_reason"] == (
                "selection_source_coverage_unavailable"
            )
        return original_tick()

    monkeypatch.setattr(components.producer, "tick", transient_then_ready)

    try:
        harness.worker._run_cycle(initial=False)
        recovered = harness.worker.health()
        assert attempts >= 2
        assert recovered["running"] is True
        assert recovered["fatal"] is False
        assert recovered["ready"] is True
        assert recovered["producer_database_unavailable_cycles"] == 1
        assert recovered["producer_frontier_recovery_pending"] is False
        assert recovered["candidate_reader"]["installed"] is True
        assert recovered["candidate_reader"]["suspended"] is False
        assert harness.publisher.poisoned is False
        assert recovered["occurrences_published"] == 1
        assert harness.publisher.durable_through == 1
        assert recovered["last_frontier_sequence"] == 1

        harness.reader.suspend("selection_source_coverage_unavailable")
        harness.worker._run_cycle(initial=False)
        source_suspended = harness.worker.health()
        assert attempts >= 4
        assert source_suspended["running"] is True
        assert source_suspended["fatal"] is False
        assert source_suspended["ready"] is False
        assert source_suspended["producer_database_unavailable_cycles"] == 2
        assert source_suspended["producer_frontier_recovery_pending"] is False
        assert source_suspended["candidate_reader"]["suspended"] is True
        assert source_suspended["candidate_reader"]["suspend_reason"] == (
            "selection_source_coverage_unavailable"
        )
        assert harness.publisher.poisoned is False
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_initial_transient_producer_database_failure_leaves_no_recovery_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_tick = _FakeProducer.tick
    attempts = 0

    def transient_then_ready(producer: _FakeProducer) -> SimpleNamespace:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError(
                "SELECT captured_paper_selection_frontier",
                {},
                TimeoutError("statement timeout"),
            )
        return original_tick(producer)

    monkeypatch.setattr(_FakeProducer, "tick", transient_then_ready)
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)

    try:
        harness.worker.start()
        health = harness.worker.health()
        assert attempts >= 2
        assert health["running"] is True
        assert health["fatal"] is False
        assert health["ready"] is True
        assert health["producer_database_unavailable_cycles"] == 1
        assert health["producer_frontier_recovery_pending"] is False
        assert health["candidate_reader"]["installed"] is True
        assert health["candidate_reader"]["suspended"] is False
        assert health["last_frontier_sequence"] == 1
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_persistent_transient_producer_database_failure_uses_existing_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)
    harness.worker.start()
    assert harness.publisher is not None
    components = harness.worker._components
    assert components is not None
    observations = iter((0.0, 0.11))
    harness.worker.producer_timeout_seconds = 0.1
    harness.worker.monotonic_clock = lambda: next(observations)

    def unavailable() -> SimpleNamespace:
        raise SqlAlchemyTimeoutError("selection pool exhausted")

    monkeypatch.setattr(components.producer, "tick", unavailable)

    try:
        with pytest.raises(CapturedPaperSelectionRuntimeError) as failure:
            harness.worker._drain_producer_to(1)
        assert failure.value.code == "PRODUCER_FRONTIER_TIMEOUT"
        health = harness.worker.health()
        assert health["running"] is True
        assert health["fatal"] is False
        assert health["producer_database_unavailable_cycles"] == 1
        assert health["candidate_reader"]["suspended"] is True
        assert health["candidate_reader"]["suspend_reason"] == (
            "selection_producer_frontier_pending"
        )
        assert harness.publisher.poisoned is False
        assert harness.publisher.durable_through == 1
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_initial_transient_producer_database_failure_during_close_is_not_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocked_then_unavailable(_producer: _FakeProducer) -> SimpleNamespace:
        entered.set()
        assert release.wait(timeout=2.0)
        raise SqlAlchemyTimeoutError("selection pool exhausted")

    monkeypatch.setattr(_FakeProducer, "tick", blocked_then_unavailable)
    harness = _Harness(
        tmp_path,
        poll_interval_seconds=60.0,
        wait_for_initial_pressure=True,
        pressure_health_sequence=[_pressure_health(clean=True)],
    )
    harness.worker.producer_timeout_seconds = 0.01
    close_errors: list[BaseException] = []

    def close_worker() -> None:
        try:
            harness.worker.close(join_timeout_seconds=1.0)
        except BaseException as exc:
            close_errors.append(exc)

    harness.worker.start()
    assert entered.wait(timeout=1.0)
    time.sleep(0.02)
    close_thread = threading.Thread(target=close_worker)
    close_thread.start()
    assert harness.worker._stop_event.wait(timeout=1.0)
    release.set()
    close_thread.join(timeout=1.0)

    assert not close_thread.is_alive()
    assert close_errors == []
    health = harness.worker.health()
    assert health["state"] == "quiesced"
    assert health["fatal"] is False
    assert health["running"] is False
    assert health["stop_requested"] is True
    assert harness.publisher is not None
    assert harness.publisher.poisoned is False


def test_periodic_durable_timeout_suspends_then_recovers_same_accepted_frontier(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=0.02)
    harness.worker.durable_timeout_seconds = 0.05
    harness.worker.start()
    assert harness.source is not None
    assert harness.publisher is not None
    publisher = harness.publisher
    publisher.auto_durable = False
    harness.source.recovery_snapshot_pending = True

    try:
        suspended = _await_worker_health(
            harness.worker,
            lambda health: (
                health.get("durable_frontier_timeout_cycles", 0) >= 2
            ),
        )
        assert suspended["running"] is True
        assert suspended["fatal"] is False
        assert suspended["ready"] is False
        assert suspended["candidate_reader"]["suspended"] is True
        assert suspended["candidate_reader"]["suspend_reason"] == (
            "selection_producer_frontier_pending"
        )
        assert harness.source.read_count == 2
        assert publisher.accepted_through == 2
        assert publisher.durable_through == 1
        assert publisher.poisoned is False
        with pytest.raises(CapturedPaperInitialCandidateReaderUnavailable):
            harness.reader.read_candidates(
                user_id=1,
                symbol="AAPL",
                decision_at=NOW,
            )

        publisher.durable_through = publisher.accepted_through
        recovered = _await_worker_health(
            harness.worker,
            lambda health: (
                health["ready"] is True
                and health["last_frontier_sequence"] == 2
            ),
        )
        assert recovered["running"] is True
        assert recovered["fatal"] is False
        assert recovered["durable_frontier_timeout_cycles"] >= 2
        assert recovered["candidate_reader"]["installed"] is True
        assert harness.source.read_count == 2
        assert publisher.accepted_through == 2
        assert publisher.durable_through == 2
        assert publisher.poisoned is False
    finally:
        publisher.durable_through = publisher.accepted_through
        harness.worker.close(join_timeout_seconds=1.0)


def test_periodic_batch_suspends_after_first_accept_before_later_publish_returns(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=0.02)
    harness.worker.start()
    assert harness.source is not None
    assert harness.publisher is not None
    publisher = harness.publisher
    harness.source.prime_snapshot_count = 2

    first_publish_accepted = threading.Event()
    release_first_publish = threading.Event()
    original_publish = publisher.publish_bundle

    def block_after_first_periodic_accept(**kwargs: object) -> SimpleNamespace:
        sequence = int(getattr(kwargs["bundle"], "source_sequence"))
        receipt = original_publish(**kwargs)
        if sequence == 2:
            first_publish_accepted.set()
            assert release_first_publish.wait(timeout=2.0)
        return receipt

    publisher.publish_bundle = block_after_first_periodic_accept
    harness.source.recovery_snapshot_pending = True

    try:
        assert first_publish_accepted.wait(timeout=2.0)
        health = harness.worker.health()
        assert health["running"] is True
        assert health["fatal"] is False
        assert health["candidate_reader"]["suspended"] is True
        assert health["candidate_reader"]["suspend_reason"] == (
            "selection_producer_frontier_pending"
        )
        assert publisher.accepted_through == 2
        with pytest.raises(CapturedPaperInitialCandidateReaderUnavailable):
            harness.reader.read_candidates(
                user_id=1,
                symbol="AAPL",
                decision_at=NOW,
            )

        release_first_publish.set()
        recovered = _await_worker_health(
            harness.worker,
            lambda observed: (
                observed["ready"] is True
                and observed["last_frontier_sequence"] == 3
            ),
        )
        assert recovered["fatal"] is False
        assert harness.source.read_count == 2
        assert publisher.accepted_through == 3
        assert publisher.poisoned is False
    finally:
        release_first_publish.set()
        harness.worker.close(join_timeout_seconds=1.0)


def test_periodic_durable_wait_is_promptly_interruptible_on_close(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=0.02)
    harness.worker.durable_timeout_seconds = 5.0
    harness.worker.start()
    assert harness.source is not None
    assert harness.publisher is not None
    publisher = harness.publisher
    publisher.auto_durable = False
    harness.source.recovery_snapshot_pending = True

    try:
        _await_worker_health(
            harness.worker,
            lambda health: (
                health["candidate_reader"]["suspended"] is True
                and publisher.accepted_through == 2
                and publisher.durable_through == 1
            ),
        )

        started_at = time.monotonic()
        harness.worker.close(join_timeout_seconds=1.0)
        elapsed = time.monotonic() - started_at
        health = harness.worker.health()

        assert elapsed < 1.0
        assert health["state"] == "quiesced"
        assert health["fatal"] is False
        assert health["running"] is False
        assert publisher.poisoned is False
    finally:
        publisher.durable_through = publisher.accepted_through
        if harness.worker.health()["running"]:
            harness.worker.close(join_timeout_seconds=1.0)


def test_periodic_producer_drain_is_promptly_interruptible_on_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=0.05)
    harness.worker.producer_timeout_seconds = 5.0
    harness.worker.start()
    assert harness.source is not None
    assert harness.publisher is not None
    components = harness.worker._components
    assert components is not None

    monkeypatch.setattr(
        components.producer,
        "tick",
        lambda: _ready_producer_result(1),
    )
    harness.source.recovery_snapshot_pending = True
    _await_worker_health(
        harness.worker,
        lambda health: health["candidate_reader"]["suspended"] is True,
    )

    started_at = time.monotonic()
    harness.worker.close(join_timeout_seconds=1.0)
    elapsed = time.monotonic() - started_at
    health = harness.worker.health()

    assert elapsed < 1.0
    assert health["state"] == "quiesced"
    assert health["fatal"] is False
    assert health["running"] is False
    assert harness.publisher.poisoned is False


def test_non_finite_producer_clock_is_integrity_fatal_not_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)
    harness.worker.start()
    components = harness.worker._components
    assert components is not None
    monkeypatch.setattr(
        components.producer,
        "tick",
        lambda: _ready_producer_result(1),
    )
    harness.worker.monotonic_clock = lambda: float("nan")

    try:
        with pytest.raises(CapturedPaperSelectionRuntimeError) as failure:
            harness.worker._drain_producer_to(2)
        assert failure.value.code == "PRODUCER_CLOCK_INVALID"
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_producer_catchup_does_not_clear_source_coverage_suspension(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)
    harness.worker.start()
    assert harness.publisher is not None
    assert harness.source is not None
    harness.reader.suspend("selection_source_coverage_unavailable")
    harness.publisher.accepted_through = 2
    harness.publisher.durable_through = 2

    try:
        harness.worker._run_cycle(initial=False)
        health = harness.worker.health()
        assert health["last_frontier_sequence"] == 2
        assert health["producer_frontier_recovery_pending"] is False
        assert health["candidate_reader"]["suspended"] is True
        assert health["candidate_reader"]["suspend_reason"] == (
            "selection_source_coverage_unavailable"
        )
        assert harness.source.read_count == 2
        assert harness.publisher.poisoned is False
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_producer_recovery_resumes_after_pressure_clears_without_new_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=60.0)
    harness.worker.start()
    assert harness.source is not None
    assert harness.publisher is not None
    harness.source.recovery_snapshot_pending = True
    harness.worker.wait_for_initial_pressure = True
    pressure_clean = iter((False, True))
    monkeypatch.setattr(
        harness.worker,
        "_initial_pressure_status",
        lambda _components: (next(pressure_clean), {}, 1.0),
    )

    try:
        harness.worker._run_cycle(initial=False)
        suspended = harness.worker.health()
        assert suspended["producer_frontier_recovery_pending"] is True
        assert suspended["candidate_reader"]["suspended"] is True
        assert harness.source.read_count == 2

        harness.worker._run_cycle(initial=False)
        recovered = harness.worker.health()
        assert recovered["ready"] is True
        assert recovered["producer_frontier_recovery_pending"] is False
        assert recovered["candidate_reader"]["installed"] is True
        assert harness.source.read_count == 3
        assert harness.publisher.accepted_through == 2
        assert harness.publisher.poisoned is False
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_writer_failure_reason_survives_startup_and_quiesced_rollback(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    original_factory = harness.worker.component_factory
    injected_reason = "OSError: injected commit publication failure"

    def failing_factory(*args: object, **kwargs: object):
        components = original_factory(*args, **kwargs)
        assert harness.publisher is not None

        def reject_after_writer_failure(**_publish_kwargs: object):
            publisher = harness.publisher
            assert publisher is not None
            publisher.ingress.writer_failure_count = 1
            publisher.ingress.writer_failure_reason = injected_reason
            publisher.ingress.post_close_submissions = 1
            publisher.reserved_sequence = None
            publisher.poisoned = True
            publisher.poison_reason = (
                "queue_ingress_rejected:capture_ingress_closed"
            )
            return SimpleNamespace(accepted=False)

        harness.publisher.publish_bundle = reject_after_writer_failure
        return components

    harness.worker.component_factory = failing_factory
    with pytest.raises(CapturedPaperSelectionRuntimeError) as failure:
        harness.worker.start()

    assert failure.value.code == "QUEUE_WRITER_FAILED"
    assert injected_reason in str(failure.value)
    assert harness.reader.health()["installed"] is False
    assert harness.writer is not None and harness.writer.closed is True
    health = harness.worker.health()
    assert health["fatal"] is True
    assert health["quiesced"] is True
    receipt = harness.worker.rollback_after_quiesce()
    assert receipt["application_outcome"] == "rolled_back"
    assert receipt["paper_order_submission_authorized"] is False
    assert receipt["live_cash_authorized"] is False
    assert harness.rollback_calls == 1


def test_writer_failure_after_last_accept_survives_durability_wait(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, auto_durable=False)
    original_factory = harness.worker.component_factory
    injected_reason = "OSError: injected post-accept fsync failure"

    def failing_factory(*args: object, **kwargs: object):
        components = original_factory(*args, **kwargs)
        assert harness.publisher is not None
        original_publish = harness.publisher.publish_bundle

        def accept_then_fail(**publish_kwargs: object):
            receipt = original_publish(**publish_kwargs)
            publisher = harness.publisher
            assert publisher is not None
            publisher.ingress.writer_failure_count = 1
            publisher.ingress.writer_failure_reason = injected_reason
            return receipt

        harness.publisher.publish_bundle = accept_then_fail
        return components

    harness.worker.component_factory = failing_factory
    with pytest.raises(CapturedPaperSelectionRuntimeError) as failure:
        harness.worker.start()

    assert failure.value.code == "QUEUE_WRITER_FAILED"
    assert injected_reason in str(failure.value)
    assert harness.reader.health()["installed"] is False
    assert harness.worker.health()["quiesced"] is True
    receipt = harness.worker.rollback_after_quiesce()
    assert receipt["application_outcome"] == "rolled_back"
    assert receipt["paper_order_submission_authorized"] is False
    assert receipt["live_cash_authorized"] is False


def test_queue_overflow_health_is_terminal_and_revokes_reader(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.worker.start()
    assert harness.publisher is not None
    harness.publisher.ingress.dropped = 1
    health = harness.worker.health()
    assert health["fatal"] is True
    assert health["running"] is False
    assert harness.reader.health()["revoked"] is True
    harness.worker.close(join_timeout_seconds=1.0)


def test_source_unavailable_suspends_decisions_until_new_durable_frontier(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=0.01)
    harness.worker.start()
    assert harness.source is not None
    harness.source.raise_unavailable_after_prime = True
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        health = harness.worker.health()
        if health["source_unavailable_cycles"] > 0:
            break
        time.sleep(0.01)
    assert health["source_unavailable_cycles"] > 0
    assert health["fatal"] is False
    assert health["running"] is True
    assert health["ready"] is False
    assert harness.reader.health()["suspended"] is True
    with pytest.raises(CapturedPaperInitialCandidateReaderUnavailable):
        harness.reader.read_candidates(user_id=1, symbol="AAPL", decision_at=NOW)

    harness.source.raise_unavailable_after_prime = False
    harness.source.recovery_snapshot_pending = True
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        health = harness.worker.health()
        if health["ready"] and health["occurrences_published"] == 2:
            break
        time.sleep(0.01)
    assert health["ready"] is True
    assert health["occurrences_published"] == 2
    assert harness.reader.health()["installed"] is True
    harness.worker.close(join_timeout_seconds=1.0)


@pytest.mark.parametrize(
    "database_error",
    [
        OperationalError(
            "SELECT captured_paper_selection_source",
            {},
            TimeoutError("statement timeout"),
        ),
        SqlAlchemyTimeoutError("selection pool exhausted"),
        DBAPIError(
            "SELECT captured_paper_selection_source",
            {},
            ConnectionError("connection lost"),
            connection_invalidated=True,
        ),
    ],
    ids=("operational", "pool_timeout", "connection_invalidated"),
)
def test_transient_source_database_failure_suspends_then_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_error: BaseException,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=0.2)
    harness.worker.start()
    assert harness.source is not None
    assert harness.publisher is not None
    original_read_snapshot = harness.source.read_snapshot
    attempts = 0

    def transient_then_snapshot() -> tuple[object, ...]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise database_error
        return original_read_snapshot()

    monkeypatch.setattr(harness.source, "read_snapshot", transient_then_snapshot)
    harness.source.recovery_snapshot_pending = True

    try:
        recovered = _await_worker_health(
            harness.worker,
            lambda health: (
                health["ready"] is True
                and health["occurrences_published"] == 2
                and health["source_unavailable_cycles"] == 1
                and health["source_database_unavailable_cycles"] == 1
            ),
        )
        assert attempts >= 2
        assert recovered["running"] is True
        assert recovered["fatal"] is False
        assert recovered["candidate_reader"]["installed"] is True
        assert harness.publisher.poisoned is False
        assert harness.publisher.durable_through == 2
        assert recovered["last_frontier_sequence"] == 2
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_nontransient_source_database_failure_remains_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, poll_interval_seconds=0.2)
    harness.worker.start()
    assert harness.source is not None
    assert harness.publisher is not None

    def invalid_query() -> tuple[object, ...]:
        raise DBAPIError(
            "SELECT captured_paper_selection_source",
            {},
            ValueError("invalid query contract"),
            connection_invalidated=False,
        )

    monkeypatch.setattr(harness.source, "read_snapshot", invalid_query)

    try:
        failed = _await_worker_health(
            harness.worker,
            lambda health: health["fatal"] is True,
        )
        assert failed["running"] is False
        assert failed["source_database_unavailable_cycles"] == 0
        assert failed["candidate_reader"]["revoked"] is True
        assert harness.publisher.poisoned is True
    finally:
        harness.worker.close(join_timeout_seconds=1.0)


def test_ambiguous_rollback_is_retained_and_never_retried(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.worker.start()
    harness.worker.close(join_timeout_seconds=1.0)
    calls = 0

    def ambiguous(_application: CapturedPaperVariantBindingApplication) -> object:
        nonlocal calls
        calls += 1
        raise TimeoutError("commit acknowledgement lost")

    harness.worker.rollback_application = ambiguous  # type: ignore[assignment]
    with pytest.raises(CapturedPaperSelectionRuntimeError) as first:
        harness.worker.rollback_after_quiesce()
    assert first.value.code == "ROLLBACK_AMBIGUOUS"
    assert harness.worker.application is harness.setup.application
    with pytest.raises(CapturedPaperSelectionRuntimeError) as second:
        harness.worker.rollback_after_quiesce()
    assert second.value.code == "ROLLBACK_AMBIGUOUS"
    assert calls == 1
