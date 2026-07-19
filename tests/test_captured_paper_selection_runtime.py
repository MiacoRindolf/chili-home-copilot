from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

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
        self.dropped = 0

    def health(self) -> dict[str, object]:
        return {
            "writer_failure_count": 0,
            "dropped": self.dropped,
            "post_close_submissions": 0,
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
            budget=SimpleNamespace(derived_hot_symbol_capacity=9),
        )
        self.shared_admission_budget = object()
        self.store = SimpleNamespace(root=root)


class _FakePublisher:
    def __init__(
        self,
        runtime: _FakeSharedRuntime,
        authority: CapturedPaperSelectionAuthority,
        *,
        auto_durable: bool,
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

    def reserve_sequence(self) -> int:
        assert self.reserved_sequence is None
        self.reserved_sequence = self.accepted_through + 1
        return self.reserved_sequence

    def publish_bundle(self, **kwargs: object) -> SimpleNamespace:
        bundle = kwargs["bundle"]
        assert getattr(bundle, "source_sequence") == self.reserved_sequence
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
                "last_error": None,
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

    def read_snapshot(self) -> tuple[object, ...]:
        self.log.append("source_read")
        self.read_count += 1
        if self.read_count > 1 and self.raise_unavailable_after_prime:
            raise CapturedPaperSelectionSourceUnavailable("provider_unavailable")
        if self.read_count == 1 or self.recovery_snapshot_pending:
            self.recovery_snapshot_pending = False
            return (object(),)
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
        self.fence_calls = 0
        self.setup_calls = 0
        self.component_calls = 0
        self.rollback_calls = 0
        self.publisher: _FakePublisher | None = None
        self.source: _FakeSource | None = None
        self.writer: _FakeWriter | None = None
        self.auto_durable = auto_durable
        self.source_generation = source_generation

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
            monotonic_clock=monotonic_clock,
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
