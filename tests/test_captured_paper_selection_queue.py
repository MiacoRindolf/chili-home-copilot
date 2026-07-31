from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import threading
import time
import uuid

import pytest

from app.services.trading.momentum_neural import (
    captured_paper_selection_queue as queue_module,
)
from app.services.trading.momentum_neural import (
    replay_capture_contract as capture_contract,
)
from app.services.trading.momentum_neural.captured_paper_selection_producer import (
    FRONTIER_SCHEMA_VERSION,
    CapturedPaperSelectionAuthority,
    CapturedPaperSelectionFrontierReceipt,
    CapturedPaperSelectionQueueUnavailable,
    CapturedPaperSelectionVariantBinding,
)
from app.services.trading.momentum_neural.captured_paper_selection_queue import (
    CapturedPaperSelectionQueueError,
    CapturedPaperSelectionQueueInputPort,
    CapturedPaperSelectionQueuePublisher,
    CapturedPaperSelectionQueueReadTimeout,
    CapturedPaperSelectionQueueWriter,
)
from app.services.trading.momentum_neural.captured_viability_adapter import (
    REQUIRED_COMPONENTS,
    CapturedViabilityDependencyBinding,
    CapturedViabilityDependencyInventory,
    CapturedViabilityInputBundle,
    CapturedViabilityScoringAuthority,
    captured_viability_component_sha256s,
    captured_viability_read_receipt_sha256,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureClocks,
    CaptureEvent,
    CaptureEventRef,
    CaptureRunIdentity,
    CaptureStream,
    CoverageGap,
    ProviderWatermark,
    StreamCoverage,
    canonical_json_bytes,
    captured_read_result_sha256,
    sha256_json,
)
from app.services.trading.momentum_neural.replay_capture_runtime import (
    BoundedCaptureIngress,
    CaptureBudgetPolicy,
    CaptureResourceBinding,
    CaptureResourceMeasurement,
    SharedCaptureAdmissionBudget,
    SharedCaptureStoreRuntime,
)
from tests.test_captured_viability_adapter import _fixture as _adapter_fixture


UTC = timezone.utc
BASE = datetime(2026, 7, 18, 16, 0, tzinfo=UTC)
ACCOUNT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
GENERATION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _resource_binding(*, max_queue_events: int = 100) -> CaptureResourceBinding:
    measurement = CaptureResourceMeasurement(
        measured_at=BASE,
        sample_seconds=5,
        total_memory_bytes=256_000_000,
        available_memory_bytes=192_000_000,
        disk_free_bytes=2_000_000_000,
        average_cpu_percent=20,
        sustained_append_bytes_per_second=20_000_000,
        fsync_p95_milliseconds=5,
        logical_cpu_count=8,
        host_fingerprint_sha256=_digest("queue-host"),
    )
    policy = CaptureBudgetPolicy(
        memory_reserve_bytes=32_000_000,
        disk_reserve_bytes=100_000_000,
        capture_fraction_of_memory_headroom=0.50,
        ring_fraction_of_capture_memory=0.25,
        queue_fraction_of_capture_memory=0.25,
        capture_fraction_of_disk_headroom=0.50,
        capture_fraction_of_measured_write_bandwidth=0.25,
        max_average_cpu_percent=80,
        capture_fraction_of_cpu_headroom=0.90,
        calibrated_hot_symbol_bytes=100_000,
        max_queue_events=max_queue_events,
        max_ring_events=200,
        max_gap_keys=64,
        raw_retention_days=3,
        derived_retention_days=90,
        pressure_cpu_enter_percent=75,
        pressure_cpu_exit_percent=60,
        pressure_memory_enter_margin_bytes=1_000_000,
        pressure_memory_exit_margin_bytes=2_000_000,
        pressure_disk_enter_margin_bytes=1_000_000,
        pressure_disk_exit_margin_bytes=2_000_000,
        pressure_write_latency_enter_milliseconds=100,
        pressure_write_latency_exit_milliseconds=25,
        pressure_enter_samples=3,
        pressure_recovery_samples=3,
        pressure_sample_max_age_seconds=5,
        store_owner_lease_seconds=60,
        store_owner_heartbeat_seconds=10,
    )
    return CaptureResourceBinding.resolve(measurement, policy)


def _source_bundle(
    *, source_sequence: int, code_payload: dict | None = None
) -> tuple[
    CapturedViabilityInputBundle,
    CapturedViabilityScoringAuthority,
    tuple[CaptureEvent, ...],
    CapturedPaperSelectionAuthority,
    CaptureRunIdentity,
]:
    base, base_scoring, _evaluation_at = _adapter_fixture()
    payloads = {
        CaptureStream.CONFIG_SNAPSHOT: {"fixture": "config", "revision": 1},
        CaptureStream.FEATURE_FLAG_SNAPSHOT: {
            "fixture": "intended_strategy_policy",
            "paper_only_strategy_override": False,
        },
        CaptureStream.CODE_BUILD: (
            code_payload
            if code_payload is not None
            else {"fixture": "code", "build": "queue-test"}
        ),
        CaptureStream.PROVIDER_OHLCV: {"bars": [[10.0, 11.0, 9.5, 10.5, 1000]]},
        CaptureStream.IQFEED_PRINT: {
            "price": 10.55,
            "size": 100,
            "tick_id": "101",
        },
    }
    config_sha256 = sha256_json(payloads[CaptureStream.CONFIG_SNAPSHOT])
    policy_sha256 = sha256_json(payloads[CaptureStream.FEATURE_FLAG_SNAPSHOT])
    code_sha256 = sha256_json(payloads[CaptureStream.CODE_BUILD])
    source_identity = CaptureRunIdentity(
        run_id=GENERATION,
        generation=2,
        code_build_sha256=code_sha256,
        config_sha256=config_sha256,
        feature_flags_sha256=policy_sha256,
        account_identity_sha256=_digest("source-account"),
        broker="iqfeed",
        broker_environment="recorded",
    )
    query = base.read_receipts[0].query
    events: list[CaptureEvent] = []
    for original in base.source_refs:
        events.append(
            CaptureEvent(
                identity=source_identity,
                sequence=original.sequence,
                stream=original.stream,
                clocks=CaptureClocks(
                    received_at=original.received_at,
                    available_at=original.available_at,
                    provider_event_at=original.provider_event_at,
                    market_reference_at=original.market_reference_at,
                ),
                payload=payloads[original.stream],
                provider=original.provider,
                symbol=original.symbol,
                query=(query if original.stream is CaptureStream.PROVIDER_OHLCV else None),
            )
        )
    refs = tuple(CaptureEventRef.from_event(event) for event in events)
    old_to_new = {
        old.event_sha256: new.event_sha256
        for old, new in zip(base.source_refs, refs, strict=True)
    }
    ohlcv_ref = next(
        ref for ref in refs if ref.stream is CaptureStream.PROVIDER_OHLCV
    )
    receipt = replace(
        base.read_receipts[0],
        identity_sha256=source_identity.identity_sha256,
        source_event_sha256s=(ohlcv_ref.event_sha256,),
        result_sha256=captured_read_result_sha256((ohlcv_ref,)),
    )
    receipt_sha256 = captured_viability_read_receipt_sha256(receipt)

    coverages: list[StreamCoverage] = []
    for coverage in base.stream_coverages:
        watermark = coverage.watermark
        if watermark is not None:
            watermark = replace(
                watermark,
                identity_sha256=source_identity.identity_sha256,
            )
        coverages.append(
            replace(
                coverage,
                identity_sha256=source_identity.identity_sha256,
                watermark=watermark,
            )
        )

    roots = captured_viability_component_sha256s(
        symbol=base.symbol,
        variant_id=base.variant_id,
        family=base.family,
        context=base.context,
        features=base.features,
        settings=base.settings,
        external=base.external,
        post_score_adjustment=base.post_score_adjustment,
        event_at=base.event_at,
        available_at=base.available_at,
        read_at=base.read_at,
        capture_identity_sha256=source_identity.identity_sha256,
        policy_sha256=policy_sha256,
        config_sha256=config_sha256,
        code_sha256=code_sha256,
    )
    bindings = tuple(
        CapturedViabilityDependencyBinding(
            component=row.component,
            component_sha256=roots[row.component],
            source_event_sha256s=tuple(
                old_to_new[value] for value in row.source_event_sha256s
            ),
            read_receipt_sha256s=(
                (receipt_sha256,) if row.read_receipt_sha256s else ()
            ),
        )
        for row in base.dependency_inventory.bindings
    )
    assert {row.component for row in bindings} == set(REQUIRED_COMPONENTS)
    inventory = CapturedViabilityDependencyInventory(
        dependency_profile=base.dependency_inventory.dependency_profile,
        bindings=bindings,
    )
    bundle = replace(
        base,
        source_sequence=source_sequence,
        capture_identity_sha256=source_identity.identity_sha256,
        policy_sha256=policy_sha256,
        config_sha256=config_sha256,
        code_sha256=code_sha256,
        dependency_inventory=inventory,
        source_refs=refs,
        read_receipts=(receipt,),
        stream_coverages=tuple(coverages),
        correlation_id=f"captured-queue-{source_sequence}",
    )
    family = bundle.family.family_id
    activation_policy_sha256 = _digest("activation-policy")
    activation_settings_sha256 = _digest("activation-settings")
    activation_code_sha256 = _digest("activation-code-build")
    selection = CapturedPaperSelectionAuthority(
        expected_account_id=ACCOUNT_ID,
        activation_generation=GENERATION,
        policy_sha256=activation_policy_sha256,
        settings_projection_sha256=activation_settings_sha256,
        code_build_sha256=activation_code_sha256,
        variant_bindings=(
            CapturedPaperSelectionVariantBinding(
                variant_id=bundle.variant_id,
                family=family,
                # Deployment/clone revision is a different authority domain
                # from the scorer taxonomy's family-version contract.
                version=bundle.family.version + 2,
                variant_key=f"captured_paper:{family}",
                target_after_sha256=_digest("bound-paper-variant"),
            ),
        ),
    )
    scoring = replace(
        base_scoring,
        capture_identity_sha256=source_identity.identity_sha256,
        policy_sha256=policy_sha256,
        config_sha256=config_sha256,
        code_sha256=code_sha256,
        settings_projection_sha256=bundle.settings_projection_sha256,
        family_sha256=bundle.component_roots["family"],
        dependency_profile_sha256=inventory.dependency_profile.profile_sha256,
        activation_policy_sha256=selection.policy_sha256,
        activation_settings_projection_sha256=(
            selection.settings_projection_sha256
        ),
        activation_code_build_sha256=selection.code_build_sha256,
        selection_authority_sha256=selection.authority_sha256,
    )
    queue_identity = CaptureRunIdentity(
        run_id=GENERATION,
        generation=1,
        code_build_sha256=selection.code_build_sha256,
        config_sha256=selection.settings_projection_sha256,
        feature_flags_sha256=selection.policy_sha256,
        account_identity_sha256=_digest("alpaca-paper-account-receipt"),
        broker="alpaca",
        broker_environment="paper",
    )
    return bundle, scoring, tuple(events), selection, queue_identity


def _frontier(
    authority: CapturedPaperSelectionAuthority,
    *,
    last_source_sequence: int = 0,
    last_batch_sha256: str | None = None,
) -> CapturedPaperSelectionFrontierReceipt:
    values = {
        "schema_version": FRONTIER_SCHEMA_VERSION,
        "account_scope": authority.account_scope,
        "expected_account_id": authority.expected_account_id,
        "activation_generation": authority.activation_generation,
        "execution_family": authority.execution_family,
        "authority_sha256": authority.authority_sha256,
        "policy_sha256": authority.policy_sha256,
        "settings_projection_sha256": authority.settings_projection_sha256,
        "code_build_sha256": authority.code_build_sha256,
        "variant_set_sha256": authority.variant_set_sha256,
        "last_source_sequence": last_source_sequence,
        "last_source_event_at": None,
        "last_source_available_at": None,
        "last_batch_sha256": last_batch_sha256,
        "status": "ready",
        "gap_count": 0,
        "version": last_source_sequence + 1,
        "event_sequence": last_source_sequence,
        "last_event_sha256": (
            _digest(f"frontier-event-{last_source_sequence}")
            if last_source_sequence
            else None
        ),
    }
    body = dict(values)
    body.pop("schema_version")
    body["schema_version"] = FRONTIER_SCHEMA_VERSION
    receipt = CapturedPaperSelectionFrontierReceipt(
        frontier_id=1,
        **{key: value for key, value in values.items() if key != "schema_version"},
        frontier_sha256=sha256_json(body),
    )
    receipt.verify()
    return receipt


@dataclass
class _Harness:
    manager: SharedCaptureStoreRuntime
    publisher: CapturedPaperSelectionQueuePublisher
    writer: CapturedPaperSelectionQueueWriter
    bundle: CapturedViabilityInputBundle
    scoring: CapturedViabilityScoringAuthority
    source_events: tuple[CaptureEvent, ...]
    selection: CapturedPaperSelectionAuthority
    queue_identity: CaptureRunIdentity
    now: datetime


def _harness(
    tmp_path: Path,
    *,
    max_queue_events: int = 100,
    monotonic_clock=time.monotonic,
    code_payload: dict | None = None,
) -> _Harness:
    bundle, scoring, events, selection, queue_identity = _source_bundle(
        source_sequence=1,
        code_payload=code_payload,
    )
    binding = _resource_binding(max_queue_events=max_queue_events)
    shared = SharedCaptureAdmissionBudget.from_resource_binding(
        binding,
        monotonic_clock=monotonic_clock,
    )
    manager = SharedCaptureStoreRuntime.create(
        tmp_path / "captured-selection-queue",
        resource_binding=binding,
        shared_admission_budget=shared,
        compression_codec="zlib",
    )
    ingress = BoundedCaptureIngress.from_resource_binding(
        binding,
        shared_admission_budget=shared,
        monotonic_clock=monotonic_clock,
    )
    lease = manager.acquire(queue_identity)
    now = bundle.read_at + timedelta(seconds=1)
    publisher = CapturedPaperSelectionQueuePublisher(
        writer_lease=lease,
        ingress=ingress,
        selection_authority=selection,
        wall_clock=lambda: now,
        monotonic_clock=monotonic_clock,
    )
    writer = CapturedPaperSelectionQueueWriter(
        publisher=publisher,
        batch_events=10,
        batch_bytes=binding.budget.async_queue_bytes,
        poll_seconds=0.001,
        flush_interval_seconds=0.001,
    )
    return _Harness(
        manager=manager,
        publisher=publisher,
        writer=writer,
        bundle=bundle,
        scoring=scoring,
        source_events=events,
        selection=selection,
        queue_identity=queue_identity,
        now=now,
    )


def _input_port(
    harness: _Harness,
    **limits,
) -> CapturedPaperSelectionQueueInputPort:
    return CapturedPaperSelectionQueueInputPort(
        root=harness.manager.store.root,
        queue_identity=harness.queue_identity,
        selection_authority=harness.selection,
        durable_gate=harness.publisher.durable_gate,
        max_batch_events=limits.get("max_batch_events", 10),
        max_batch_bytes=limits.get("max_batch_bytes", 5_000_000),
        max_read_seconds=limits.get("max_read_seconds", 5.0),
        max_commit_files=limits.get("max_commit_files", 100_000),
        wall_clock=lambda: harness.now + timedelta(seconds=1),
    )


def _publish(harness: _Harness, bundle=None):
    selected = harness.bundle if bundle is None else bundle
    assert harness.publisher.reserve_sequence() == selected.source_sequence
    return harness.publisher.publish_bundle(
        bundle=selected,
        scoring_authority=harness.scoring,
        evaluation_at=selected.read_at,
        source_events=harness.source_events,
    )


def _publish_one_commit_at_a_time(harness: _Harness, count: int) -> None:
    harness.writer.start()
    for source_sequence in range(1, count + 1):
        bundle = replace(
            harness.bundle,
            source_sequence=source_sequence,
            correlation_id=f"captured-queue-commit-{source_sequence}",
        )
        assert _publish(harness, bundle).accepted is True
        deadline = time.monotonic() + 5.0
        while harness.publisher.health().durable_through < source_sequence:
            assert time.monotonic() < deadline
            time.sleep(0.005)


def test_before_ingress_admission_runs_after_event_cost_and_before_submit(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    assert harness.publisher.reserve_sequence() == harness.bundle.source_sequence
    observations: list[tuple[bool, bool, int, int, int | None]] = []

    def before_ingress_admission(
        event: CaptureEvent,
        size: int,
        rejection_reason: str | None,
    ) -> None:
        assert rejection_reason is None
        ingress = harness.publisher.ingress.health()
        queue = harness.publisher.health()
        observations.append(
            (
                "canonical_size_bytes" in vars(event),
                "event_sha256" in vars(event),
                size,
                int(ingress["submitted"]),
                queue.reserved_sequence,
            )
        )

    receipt = harness.publisher.publish_bundle(
        bundle=harness.bundle,
        scoring_authority=harness.scoring,
        evaluation_at=harness.bundle.read_at,
        source_events=harness.source_events,
        before_ingress_admission=before_ingress_admission,
    )

    assert receipt.accepted is True
    assert len(observations) == 1
    cached_size, cached_sha, size, submitted, reserved = observations[0]
    assert cached_size is True
    assert cached_sha is True
    assert size > 0
    assert submitted == 0
    assert reserved == 1
    _close_idle_harness(harness)


def test_queue_event_compacts_activation_static_source_payloads(
    tmp_path: Path,
) -> None:
    code_payload = {
        "fixture": "code",
        "artifacts": [
            {
                "path": f"module_{index:03d}.py",
                "sha256": _digest(f"module-{index:03d}"),
            }
            for index in range(1_045)
        ],
    }
    harness = _harness(tmp_path, code_payload=code_payload)
    captured: list[CaptureEvent] = []

    def capture_before_ingress(
        event: CaptureEvent,
        _size: int,
        rejection_reason: str | None,
    ) -> None:
        assert rejection_reason is None
        captured.append(event)

    assert harness.publisher.reserve_sequence() == harness.bundle.source_sequence
    receipt = harness.publisher.publish_bundle(
        bundle=harness.bundle,
        scoring_authority=harness.scoring,
        evaluation_at=harness.bundle.read_at,
        source_events=harness.source_events,
        before_ingress_admission=capture_before_ingress,
    )
    assert receipt.accepted is True
    harness.writer.start()
    assert harness.writer.close(timeout_seconds=5) is True

    assert len(captured) == 1
    payload = captured[0].payload
    assert payload["schema_version"] == queue_module.QUEUE_EVENT_SCHEMA_VERSION
    assert payload["source_event_refs"] == [
        CaptureEventRef.from_event(event).to_dict()
        for event in harness.source_events
    ]
    embedded_events = tuple(
        CaptureEvent.from_record(row["event"])
        for row in payload["source_events"]
    )
    assert {event.stream for event in embedded_events} == {
        CaptureStream.PROVIDER_OHLCV,
        CaptureStream.IQFEED_PRINT,
    }
    assert b"module_1044.py" not in canonical_json_bytes(payload)
    assert captured[0].canonical_size_bytes < 64 * 1_024

    legacy_payload = dict(payload)
    legacy_payload.pop("source_event_refs")
    legacy_source_events = [
        queue_module._event_envelope(event) for event in harness.source_events
    ]
    legacy_payload["source_events"] = legacy_source_events
    legacy_payload["source_event_inventory_sha256"] = sha256_json(
        legacy_source_events
    )
    assert len(canonical_json_bytes(payload)) < (
        len(canonical_json_bytes(legacy_payload)) * 0.65
    )

    port = _input_port(harness)
    batch = port.read_batch(
        frontier=_frontier(harness.selection),
        authority=harness.selection,
    )
    assert batch is not None
    assert [row.source_sequence for row in batch.observations] == [1]
    harness.manager.close()


@pytest.mark.parametrize(
    ("mutate_ref", "error"),
    [
        (
            lambda ref: replace(ref, provider="tampered-static-provider"),
            "content address mismatch",
        ),
        (
            lambda ref: replace(ref, query_sha256=_digest("unexpected-query")),
            "unexpectedly has a query",
        ),
    ],
)
def test_compact_static_source_ref_must_reconstruct_exact_event_hash(
    tmp_path: Path,
    mutate_ref: Callable[[CaptureEventRef], CaptureEventRef],
    error: str,
) -> None:
    harness = _harness(tmp_path)
    refs = list(harness.bundle.source_refs)
    index = next(
        index
        for index, ref in enumerate(refs)
        if ref.stream is CaptureStream.CODE_BUILD
    )
    refs[index] = mutate_ref(refs[index])
    tampered_bundle = replace(harness.bundle, source_refs=tuple(refs))
    retained_events = [
        queue_module._event_envelope(event)
        for event in harness.source_events
        if event.stream not in queue_module._ACTIVATION_STATIC_SOURCE_STREAMS
    ]

    with pytest.raises(CapturedPaperSelectionQueueError, match=error):
        queue_module._validate_compact_source_evidence(
            tampered_bundle,
            raw_refs=[ref.to_dict() for ref in refs],
            raw_events=retained_events,
        )
    _close_idle_harness(harness)


def test_compacted_240_occurrence_backlog_is_bounded_and_verifies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mib = 1_024 * 1_024
    code_payload = {
        "schema_version": "chili.captured-paper-code-build.v1",
        "artifacts": [
            {
                "role": (
                    "dependency:app.services.trading.momentum_neural."
                    f"fixture_module_{index:04d}"
                ),
                "path": (
                    "D:/sealed/chili/app/services/trading/momentum_neural/"
                    f"fixture_module_{index:04d}.py"
                ),
                "sha256": _digest(f"fixture-module-{index:04d}"),
            }
            for index in range(1_045)
        ],
    }
    harness = _harness(
        tmp_path,
        max_queue_events=300,
        code_payload=code_payload,
    )

    worker = harness.writer.worker
    worker.batch_events = 256
    worker.batch_bytes = mib
    worker.poll_seconds = 0.05
    worker.flush_interval_seconds = 0.5

    original_canonical = capture_contract.canonical_json_bytes
    canonical_work_bytes = 0

    def counted_canonical(value: object) -> bytes:
        nonlocal canonical_work_bytes
        raw = original_canonical(value)
        canonical_work_bytes += len(raw)
        return raw

    monkeypatch.setattr(
        capture_contract,
        "canonical_json_bytes",
        counted_canonical,
    )

    started_cpu = time.process_time()
    for sequence in range(1, 241):
        bundle, scoring, events, selection, identity = _source_bundle(
            source_sequence=sequence,
            code_payload=code_payload,
        )
        assert selection == harness.selection
        assert identity == harness.queue_identity
        assert harness.publisher.reserve_sequence() == sequence
        assert harness.publisher.publish_bundle(
            bundle=bundle,
            scoring_authority=scoring,
            evaluation_at=bundle.read_at,
            source_events=events,
        ).accepted is True

    build_cpu_seconds = time.process_time() - started_cpu
    queued = harness.publisher.health()
    total_canonical_bytes = int(queued.ingress["queued_bytes"])
    assert total_canonical_bytes <= 16 * mib
    assert canonical_work_bytes <= 384 * mib
    assert build_cpu_seconds < 30.0

    harness.writer.start()
    assert harness.writer.close(timeout_seconds=30.0) is True

    durable = harness.publisher.health()
    assert durable.accepted_through == 240
    assert durable.durable_through == 240
    assert durable.poisoned is False
    assert durable.commit_count <= 16
    assert harness.writer.health()["writer"]["events_written"] == 240

    chain = queue_module._load_commit_chain(
        harness.manager.store.root,
        identity=harness.queue_identity,
        selection_authority=harness.selection,
    )
    materialized = tuple(
        event
        for loaded in chain
        for event in queue_module._materialize_commit_events(
            harness.manager.store.root,
            loaded,
        )
    )
    assert [event.sequence for event in materialized] == list(range(1, 241))
    assert len({event.event_sha256 for event in materialized}) == 240
    assert sum(len(row.commit.event_refs) for row in chain) == 240
    for event in materialized:
        bundle, result = queue_module._verify_queue_event(
            event,
            queue_identity=harness.queue_identity,
            selection_authority=harness.selection,
        )
        assert bundle.source_sequence == event.sequence
        assert result.to_dict() == event.payload["score_result"]
    harness.manager.close()


def test_ingress_remains_final_fail_closed_authority_after_callback(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    assert harness.publisher.reserve_sequence() == harness.bundle.source_sequence

    def close_before_admission(
        _event: CaptureEvent,
        _size: int,
        rejection_reason: str | None,
    ) -> None:
        assert rejection_reason is None
        harness.publisher.ingress.close()

    receipt = harness.publisher.publish_bundle(
        bundle=harness.bundle,
        scoring_authority=harness.scoring,
        evaluation_at=harness.bundle.read_at,
        source_events=harness.source_events,
        before_ingress_admission=close_before_admission,
    )

    assert receipt.accepted is False
    ingress = harness.publisher.ingress.health()
    assert ingress["dropped"] == 1
    assert ingress["pending_gap_keys"] == 1
    drained = harness.publisher.ingress.pop_batch(
        max_events=1,
        max_bytes=1,
        timeout_seconds=0,
    )
    assert len(drained.gaps) == 1
    harness.publisher.writer_lease.release()
    harness.manager.close()


def test_initial_publish_retries_same_event_after_shared_budget_recovers(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    harness = _harness(tmp_path, monotonic_clock=clock)
    assert harness.publisher.reserve_sequence() == harness.bundle.source_sequence
    shared = harness.publisher.ingress.shared_admission_budget
    assert shared is not None
    filler: list[CaptureEvent] = []
    observations: list[tuple[str, int, str | None]] = []

    def before_ingress_admission(
        event: CaptureEvent,
        size: int,
        rejection_reason: str | None,
    ) -> None:
        observations.append((event.event_sha256, size, rejection_reason))
        if rejection_reason is None:
            other = _event_for_shared_admission(event)
            filler.append(other)
            assert shared.try_admit(
                other,
                shared.max_bytes - size + 1,
            ) is None
            return
        assert (
            rejection_reason
            == "shared_capture_write_bandwidth_budget_exceeded"
        )
        shared.complete(tuple(filler))
        clock.now += 1.0

    receipt = harness.publisher.publish_bundle(
        bundle=harness.bundle,
        scoring_authority=harness.scoring,
        evaluation_at=harness.bundle.read_at,
        source_events=harness.source_events,
        before_ingress_admission=before_ingress_admission,
    )

    assert receipt.accepted is True
    assert len(observations) == 2
    assert observations[0][0:2] == observations[1][0:2]
    assert observations[0][2] is None
    assert (
        observations[1][2]
        == "shared_capture_write_bandwidth_budget_exceeded"
    )
    ingress = harness.publisher.ingress.health()
    assert ingress["submitted"] == 1
    assert ingress["accepted"] == 1
    assert ingress["dropped"] == 0
    assert ingress["pending_gap_keys"] == 0
    _close_idle_harness(harness)


def test_retained_publish_wait_releases_publisher_lock_for_real_writer(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, max_queue_events=1)
    assert _publish(harness).accepted is True
    second, scoring, events, _selection, _identity = _source_bundle(
        source_sequence=2
    )
    assert harness.publisher.reserve_sequence() == 2
    observations: list[tuple[str, int, str | None]] = []
    writer_started = False

    def before_ingress_admission(
        event: CaptureEvent,
        size: int,
        rejection_reason: str | None,
    ) -> None:
        nonlocal writer_started
        observations.append((event.event_sha256, size, rejection_reason))
        if rejection_reason is None:
            return
        assert rejection_reason == "capture_queue_overflow"
        assert writer_started is False
        writer_started = True
        harness.writer.start()
        deadline = time.monotonic() + 2.0
        while harness.publisher.health().durable_through < 1:
            assert time.monotonic() < deadline
            time.sleep(0.001)

    receipt = harness.publisher.publish_bundle(
        bundle=second,
        scoring_authority=scoring,
        evaluation_at=second.read_at,
        source_events=events,
        before_ingress_admission=before_ingress_admission,
    )

    assert receipt.accepted is True
    assert len(observations) == 2
    assert observations[0][0:2] == observations[1][0:2]
    assert observations[0][2] is None
    assert observations[1][2] == "capture_queue_overflow"
    assert harness.writer.close(timeout_seconds=5) is True
    queue = harness.publisher.health()
    assert queue.accepted_through == 2
    assert queue.durable_through == 2
    assert queue.ingress is not None
    assert queue.ingress["submitted"] == 2
    assert queue.ingress["accepted"] == 2
    assert queue.ingress["dropped"] == 0
    harness.manager.close()


def test_retained_publish_timeout_records_one_exact_terminal_gap(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    harness = _harness(tmp_path, monotonic_clock=clock)
    assert harness.publisher.reserve_sequence() == harness.bundle.source_sequence
    shared = harness.publisher.ingress.shared_admission_budget
    assert shared is not None
    filler: list[CaptureEvent] = []

    def before_ingress_admission(
        event: CaptureEvent,
        size: int,
        rejection_reason: str | None,
    ) -> None:
        if rejection_reason is None:
            other = _event_for_shared_admission(event)
            filler.append(other)
            assert shared.try_admit(
                other,
                shared.max_bytes - size + 1,
            ) is None
            return
        assert (
            rejection_reason
            == "shared_capture_write_bandwidth_budget_exceeded"
        )
        raise RuntimeError("startup deadline expired")

    with pytest.raises(RuntimeError, match="startup deadline expired"):
        harness.publisher.publish_bundle(
            bundle=harness.bundle,
            scoring_authority=harness.scoring,
            evaluation_at=harness.bundle.read_at,
            source_events=harness.source_events,
            before_ingress_admission=before_ingress_admission,
        )

    queue = harness.publisher.health()
    assert queue.poisoned is True
    assert queue.poison_reason == (
        "queue_ingress_rejected:"
        "shared_capture_write_bandwidth_budget_exceeded"
    )
    assert queue.reserved_sequence is None
    assert queue.ingress is not None
    assert queue.ingress["submitted"] == 1
    assert queue.ingress["accepted"] == 0
    assert queue.ingress["dropped"] == 1
    assert queue.ingress["pending_gap_keys"] == 1
    assert queue.ingress["pending_retained_admissions"] == 0
    shared.complete(tuple(filler))
    harness.writer.start()
    assert harness.writer.close(timeout_seconds=5) is True
    assert harness.publisher.health().ingress["gap_lost_emitted"] == 1
    harness.manager.close()


def _event_for_shared_admission(event: CaptureEvent) -> CaptureEvent:
    return CaptureEvent(
        identity=CaptureRunIdentity(
            run_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            generation=1,
            code_build_sha256=_digest("shared-filler-code"),
            config_sha256=_digest("shared-filler-config"),
            feature_flags_sha256=_digest("shared-filler-flags"),
            account_identity_sha256=_digest("shared-filler-account"),
            broker="fixture",
            broker_environment="recorded",
        ),
        sequence=1,
        stream=CaptureStream.NBBO_QUOTE,
        clocks=event.clocks,
        payload={"fixture": "shared-admission-filler"},
        provider="fixture",
        symbol=event.symbol,
    )


def _close_idle_harness(harness: _Harness) -> None:
    # A rejected publish intentionally retains its sequence reservation until
    # shutdown emits and durably drains the generation poison marker.
    harness.writer.start()
    harness.writer.close(timeout_seconds=5)
    harness.manager.close()


def test_scorer_specific_hashes_are_distinct_from_activation_authority(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)

    assert harness.scoring.policy_sha256 != harness.selection.policy_sha256
    assert (
        harness.scoring.settings_projection_sha256
        != harness.selection.settings_projection_sha256
    )
    assert harness.scoring.code_sha256 != harness.selection.code_build_sha256
    assert (
        harness.scoring.family_version
        != harness.selection.variant_bindings[0].version
    )
    assert _input_port(harness).network_fallback_allowed is False
    _close_idle_harness(harness)


@pytest.mark.parametrize(
    ("activation_field", "wrong_value_field"),
    (
        ("activation_policy_sha256", "policy_sha256"),
        (
            "activation_settings_projection_sha256",
            "settings_projection_sha256",
        ),
        ("activation_code_build_sha256", "code_sha256"),
    ),
)
def test_activation_hash_cannot_be_replaced_by_bundle_specific_hash(
    tmp_path,
    activation_field: str,
    wrong_value_field: str,
) -> None:
    harness = _harness(tmp_path)
    wrong = replace(
        harness.scoring,
        **{
            activation_field: getattr(harness.scoring, wrong_value_field),
        },
    )

    assert harness.publisher.reserve_sequence() == harness.bundle.source_sequence
    with pytest.raises(
        CapturedPaperSelectionQueueError,
        match="scoring authority differs",
    ):
        harness.publisher.publish_bundle(
            bundle=harness.bundle,
            scoring_authority=wrong,
            evaluation_at=harness.bundle.read_at,
            source_events=harness.source_events,
        )
    _close_idle_harness(harness)


def test_scoring_authority_requires_exact_selection_authority_hash(tmp_path) -> None:
    harness = _harness(tmp_path)
    wrong = replace(
        harness.scoring,
        selection_authority_sha256=_digest("different-selection-authority"),
    )

    assert harness.publisher.reserve_sequence() == harness.bundle.source_sequence
    with pytest.raises(
        CapturedPaperSelectionQueueError,
        match="scoring authority differs",
    ):
        harness.publisher.publish_bundle(
            bundle=harness.bundle,
            scoring_authority=wrong,
            evaluation_at=harness.bundle.read_at,
            source_events=harness.source_events,
        )
    _close_idle_harness(harness)


def test_scoring_authority_family_route_must_match_bound_variant(tmp_path) -> None:
    harness = _harness(tmp_path)
    wrong = replace(harness.scoring, family_id="foreign_family")

    assert harness.publisher.reserve_sequence() == harness.bundle.source_sequence
    with pytest.raises(
        CapturedPaperSelectionQueueError,
        match="scoring authority differs",
    ):
        harness.publisher.publish_bundle(
            bundle=harness.bundle,
            scoring_authority=wrong,
            evaluation_at=harness.bundle.read_at,
            source_events=harness.source_events,
        )
    _close_idle_harness(harness)


def test_durable_round_trip_fsync_order_and_orphan_chunk_is_ignored(
    tmp_path, monkeypatch
) -> None:
    harness = _harness(tmp_path)
    calls: list[str] = []
    store = harness.manager.store
    original_write = store.write_events
    original_sync = store.sync
    original_derived = store.put_derived_artifact

    def write_events(events):
        calls.append("write_objects")
        return original_write(events)

    def sync():
        calls.append("fsync")
        return original_sync()

    def put_derived_artifact(**kwargs):
        calls.append("publish_commit")
        return original_derived(**kwargs)

    monkeypatch.setattr(store, "write_events", write_events)
    monkeypatch.setattr(store, "sync", sync)
    monkeypatch.setattr(store, "put_derived_artifact", put_derived_artifact)

    receipt = _publish(harness)
    assert receipt.accepted is True and receipt.durable is False
    harness.writer.start()
    assert harness.writer.close(timeout_seconds=5) is True
    assert calls.index("write_objects") < calls.index("fsync")
    first_sync = calls.index("fsync")
    commit = calls.index("publish_commit")
    second_sync = calls.index("fsync", first_sync + 1)
    assert first_sync < commit < second_sync
    assert harness.publisher.health().durable_through == 1

    orphan = CaptureEvent(
        identity=harness.queue_identity,
        sequence=999,
        stream=CaptureStream.CAPTURED_VIABILITY_INPUT,
        clocks=CaptureClocks(
            received_at=harness.now,
            available_at=harness.now,
            market_reference_at=harness.bundle.event_at,
        ),
        payload={"orphan": True},
        provider="captured_viability_adapter",
        symbol="VEEE",
    )
    original_write((orphan,))
    original_sync()

    port = _input_port(harness)
    assert port.network_fallback_allowed is False
    assert port.broker_access_allowed is False
    assert port.mutation_allowed is False
    batch = port.read_batch(frontier=_frontier(harness.selection), authority=harness.selection)
    assert batch is not None
    assert batch.source_sequence_from == 0
    assert batch.source_sequence_through == 1
    assert [row.source_sequence for row in batch.observations] == [1]
    assert port.health(consumer_frontier_sequence=0).lag_events == 1
    harness.manager.close()


def test_visible_commit_is_ignored_until_post_fsync_gate_acknowledges_it(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    assert _publish(harness).accepted is True
    batch = harness.publisher.ingress.pop_batch(
        max_events=10,
        max_bytes=harness.manager.resource_binding.budget.async_queue_bytes,
        timeout_seconds=0,
    )
    event_chunks = harness.manager.store.write_events(batch.events)
    gap_chunks = harness.manager.store.write_gaps(batch.gaps)
    harness.manager.store.sync()
    prepared = harness.publisher._prepare_commit(
        store=harness.manager.store,
        batch=batch,
        event_chunks=event_chunks,
        gap_chunks=gap_chunks,
    )
    assert tuple((harness.manager.store.root / "derived").rglob("*.json"))

    port = _input_port(harness)
    assert port.read_batch(
        frontier=_frontier(harness.selection), authority=harness.selection
    ) is None

    harness.manager.store.sync()
    harness.publisher._acknowledge_commit(prepared)
    durable = port.read_batch(
        frontier=_frontier(harness.selection), authority=harness.selection
    )
    assert durable is not None and durable.source_sequence_through == 1
    harness.publisher.ingress.complete_shared_admission(batch.events)
    assert harness.writer.close(timeout_seconds=5) is False
    harness.manager.close()


def test_writer_failure_before_third_commit_is_quiescent_but_never_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    original_prepare = harness.publisher._prepare_commit
    injected_reason = "injected third commit durable publication failure"

    def fail_third_commit(**kwargs):
        if harness.publisher.health().commit_count == 2:
            raise OSError(injected_reason)
        return original_prepare(**kwargs)

    monkeypatch.setattr(
        harness.publisher,
        "_prepare_commit",
        fail_third_commit,
    )
    harness.writer.start()

    def publish_sequence(sequence: int, *, retained: bool = False):
        bundle, scoring, events, selection, identity = _source_bundle(
            source_sequence=sequence
        )
        assert selection == harness.selection
        assert identity == harness.queue_identity
        assert harness.publisher.reserve_sequence() == sequence
        kwargs = {}
        if retained:
            kwargs["before_ingress_admission"] = (
                lambda _event, _size, _reason: None
            )
        return harness.publisher.publish_bundle(
            bundle=bundle,
            scoring_authority=scoring,
            evaluation_at=bundle.read_at,
            source_events=events,
            **kwargs,
        )

    for sequence in (1, 2):
        assert publish_sequence(sequence).accepted is True
        deadline = time.monotonic() + 5.0
        while harness.publisher.health().durable_through < sequence:
            assert time.monotonic() < deadline
            time.sleep(0.001)

    assert publish_sequence(3).accepted is True
    deadline = time.monotonic() + 5.0
    while harness.writer.health()["writer"]["last_error"] is None:
        assert time.monotonic() < deadline
        time.sleep(0.001)

    rejected = publish_sequence(4, retained=True)
    assert rejected.accepted is False
    failed = harness.writer.health()
    ingress = failed["writer"]["ingress"]
    assert injected_reason in str(failed["writer"]["last_error"])
    assert injected_reason in str(ingress["writer_failure_reason"])
    assert ingress["writer_failure_count"] == 1
    assert ingress["pending_gap_keys"] == 0
    assert ingress["pending_retained_admissions"] == 0
    assert harness.publisher.ingress.drained is True

    # Physical shutdown and lease release permit strategy rollback, but the
    # capture remains permanently dirty and can never claim a clean seal.
    assert harness.writer.close(timeout_seconds=5) is True
    assert harness.publisher.writer_lease.health()["released"] is True
    terminal = failed["writer"]["ingress"]
    assert terminal["clean_close_eligible"] is False
    assert failed["writer"]["stopped_cleanly"] is False
    with pytest.raises(Exception, match="clean, error-free shutdown"):
        harness.writer.worker.seal_run(harness.queue_identity)
    harness.manager.close()


def test_exact_source_event_mismatch_poison_is_durable_and_fail_closed(tmp_path) -> None:
    harness = _harness(tmp_path)
    assert harness.publisher.reserve_sequence() == 1
    with pytest.raises(Exception, match="source event inventory"):
        harness.publisher.publish_bundle(
            bundle=harness.bundle,
            scoring_authority=harness.scoring,
            evaluation_at=harness.bundle.read_at,
            source_events=harness.source_events[:-1],
        )
    assert harness.publisher.health().reserved_sequence == 1
    poison = harness.publisher.poison("source_event_inventory_mismatch")
    assert poison.source_sequence == 1
    harness.writer.start()
    assert harness.writer.close(timeout_seconds=5) is True

    with pytest.raises(CapturedPaperSelectionQueueUnavailable, match="poisoned"):
        _input_port(harness).read_batch(
            frontier=_frontier(harness.selection), authority=harness.selection
        )
    harness.manager.close()


def test_ingress_overflow_commits_gap_and_poison_without_advancing_consumer(
    tmp_path,
) -> None:
    harness = _harness(tmp_path, max_queue_events=1)
    first = _publish(harness)
    assert first.accepted is True
    second, _scoring, _events, _selection, _identity = _source_bundle(
        source_sequence=2
    )
    assert harness.publisher.reserve_sequence() == 2
    rejected = harness.publisher.publish_bundle(
        bundle=second,
        scoring_authority=harness.scoring,
        evaluation_at=second.read_at,
        source_events=harness.source_events,
    )
    assert rejected.accepted is False
    assert harness.publisher.health().poisoned is True
    harness.writer.start()
    assert harness.writer.close(timeout_seconds=5) is True
    with pytest.raises(CapturedPaperSelectionQueueUnavailable, match="poisoned"):
        _input_port(harness).read_batch(
            frontier=_frontier(harness.selection), authority=harness.selection
        )
    harness.manager.close()


def test_duplicate_symbol_variant_route_splits_bounded_batches(tmp_path) -> None:
    harness = _harness(tmp_path)
    assert _publish(harness).accepted is True
    second, _scoring, _events, _selection, _identity = _source_bundle(
        source_sequence=2
    )
    assert harness.publisher.reserve_sequence() == 2
    assert harness.publisher.publish_bundle(
        bundle=second,
        scoring_authority=harness.scoring,
        evaluation_at=second.read_at,
        source_events=harness.source_events,
    ).accepted is True
    harness.writer.start()
    assert harness.writer.close(timeout_seconds=5) is True

    port = _input_port(harness, max_batch_events=10)
    first = port.read_batch(
        frontier=_frontier(harness.selection), authority=harness.selection
    )
    assert first is not None and first.source_sequence_through == 1
    assert len(first.observations) == 1
    second_frontier = _frontier(
        harness.selection,
        last_source_sequence=1,
        last_batch_sha256=first.batch_sha256,
    )
    following = port.read_batch(
        frontier=second_frontier, authority=harness.selection
    )
    assert following is not None and following.source_sequence_through == 2
    assert len(following.observations) == 1
    harness.manager.close()


def test_partial_commit_materializes_each_payload_once(
    tmp_path,
    monkeypatch,
) -> None:
    harness = _harness(tmp_path)
    assert _publish(harness).accepted is True
    second, _scoring, _events, _selection, _identity = _source_bundle(
        source_sequence=2
    )
    assert _publish(harness, second).accepted is True
    harness.writer.start()
    assert harness.writer.close(timeout_seconds=5) is True

    chain = queue_module._load_commit_chain(
        harness.manager.store.root,
        identity=harness.queue_identity,
        selection_authority=harness.selection,
    )
    assert len(chain) == 1
    loaded = chain[0]
    rows = tuple(
        row
        for chunk in loaded.commit.event_chunks
        for row in queue_module.ContentAddressedCaptureStore.read_chunk_ref(
            harness.manager.store.root,
            chunk,
        )
    )
    assert len(rows) == 2
    payload_refs = {str(row["payload_ref"]) for row in rows}
    assert len(payload_refs) == 1
    shared_pack_ref = next(iter(payload_refs))
    assert shared_pack_ref.startswith("blobs/packs/sha256/")

    root = harness.manager.store.root
    pack_path = (root / shared_pack_ref).resolve()
    event_chunk_paths = {
        (root / chunk.relative_path).resolve()
        for chunk in loaded.commit.event_chunks
    }
    reads = {"event_chunk": 0, "payload_pack": 0}
    original_read_bytes = Path.read_bytes

    def counted_read_bytes(path):
        resolved = path.resolve()
        if resolved in event_chunk_paths:
            reads["event_chunk"] += 1
        if resolved == pack_path:
            reads["payload_pack"] += 1
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)

    port = _input_port(harness, max_batch_events=10)
    first = port.read_batch(
        frontier=_frontier(harness.selection),
        authority=harness.selection,
    )
    assert first is not None and first.source_sequence_through == 1
    following = port.read_batch(
        frontier=_frontier(
            harness.selection,
            last_source_sequence=1,
            last_batch_sha256=first.batch_sha256,
        ),
        authority=harness.selection,
    )
    assert following is not None and following.source_sequence_through == 2

    observed = dict(reads)
    harness.manager.close()
    assert observed == {"event_chunk": 1, "payload_pack": 2}


def test_coverage_unavailable_event_emits_route_tombstone_not_empty_advance(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    unavailable = replace(
        harness.bundle,
        coverage_gaps=(
            CoverageGap(
                stream=CaptureStream.IQFEED_PRINT,
                reason="fundamentals_receipt_unavailable",
                first_available_at=harness.bundle.event_at,
                last_available_at=harness.bundle.available_at,
                lost_count=1,
                symbol=harness.bundle.symbol,
            ),
        ),
    )
    receipt = _publish(harness, bundle=unavailable)
    assert receipt.score_result.status == "COVERAGE_UNAVAILABLE"
    assert receipt.score_result.opportunity_consumed is False
    assert receipt.score_result.risk_reserved is False
    assert receipt.score_result.order_posted is False
    harness.writer.start()
    assert harness.writer.close(timeout_seconds=5) is True

    batch = _input_port(harness).read_batch(
        frontier=_frontier(harness.selection),
        authority=harness.selection,
    )
    assert batch is not None
    assert batch.source_sequence_through == 1
    assert batch.observations == ()
    assert len(batch.route_state_updates) == 1
    tombstone = batch.route_state_updates[0]
    assert tombstone.symbol == unavailable.symbol
    assert tombstone.variant_id == unavailable.variant_id
    assert tombstone.state == "coverage_unavailable"
    assert tombstone.source_sequence == 1
    assert tombstone.reason_codes
    assert tombstone.evidence_sha256 == tombstone.score_result_sha256
    harness.manager.close()


def test_next_snapshot_may_rotate_only_causal_profile_authority(tmp_path) -> None:
    """A later capture advances coverage clocks without changing activation.

    The primed route template must continue to pin every stable scorer domain,
    while the profile hash is re-derived from each exact committed bundle.
    """

    harness = _harness(tmp_path)
    assert _publish(harness).accepted is True
    second, second_scoring, second_events, _selection, _identity = _source_bundle(
        source_sequence=2
    )
    old_profile = second.dependency_inventory.dependency_profile
    rotated_profile = replace(
        old_profile,
        stream_dependencies=tuple(
            replace(
                dependency,
                coverage_start_at=(
                    dependency.coverage_start_at + timedelta(microseconds=1)
                ),
            )
            for dependency in old_profile.stream_dependencies
        ),
    )
    rotated_inventory = CapturedViabilityDependencyInventory(
        dependency_profile=rotated_profile,
        bindings=second.dependency_inventory.bindings,
    )
    second = replace(second, dependency_inventory=rotated_inventory)
    second_scoring = replace(
        second_scoring,
        dependency_profile_sha256=rotated_profile.profile_sha256,
    )
    assert second_scoring.dependency_profile_sha256 != (
        harness.scoring.dependency_profile_sha256
    )
    assert harness.publisher.reserve_sequence() == 2
    assert harness.publisher.publish_bundle(
        bundle=second,
        scoring_authority=second_scoring,
        evaluation_at=second.read_at,
        source_events=second_events,
    ).accepted is True
    harness.writer.start()
    assert harness.writer.close(timeout_seconds=5) is True

    # The port was primed with the first authority.  It must derive and verify
    # the second authority rather than comparing the rotating profile byte-for-byte.
    port = _input_port(harness)
    first = port.read_batch(
        frontier=_frontier(harness.selection),
        authority=harness.selection,
    )
    assert first is not None and first.source_sequence_through == 1
    following = port.read_batch(
        frontier=_frontier(
            harness.selection,
            last_source_sequence=1,
            last_batch_sha256=first.batch_sha256,
        ),
        authority=harness.selection,
    )
    assert following is not None and following.source_sequence_through == 2
    harness.manager.close()


def test_restart_recovers_durable_allocator_frontier(tmp_path) -> None:
    harness = _harness(tmp_path)
    assert _publish(harness).accepted is True
    harness.writer.start()
    assert harness.writer.close(timeout_seconds=5) is True

    binding = harness.manager.resource_binding
    ingress = BoundedCaptureIngress.from_resource_binding(
        binding,
        shared_admission_budget=harness.manager.shared_admission_budget,
    )
    lease = harness.manager.acquire(harness.queue_identity)
    publisher = CapturedPaperSelectionQueuePublisher(
        writer_lease=lease,
        ingress=ingress,
        selection_authority=harness.selection,
        wall_clock=lambda: harness.now,
    )
    assert publisher.reserve_sequence() == 2
    publisher.poison("restart_outstanding_reservation")
    writer = CapturedPaperSelectionQueueWriter(
        publisher=publisher,
        batch_events=10,
        batch_bytes=binding.budget.async_queue_bytes,
        poll_seconds=0.001,
        flush_interval_seconds=0.001,
    )
    writer.start()
    assert writer.close(timeout_seconds=5) is True
    harness.manager.close()


def _assert_hash_verified_route_backlog_drains(
    tmp_path: Path,
    *,
    backlog_events: int,
) -> None:
    restart_after = backlog_events // 2
    harness = _harness(tmp_path, max_queue_events=backlog_events + 1)
    for source_sequence in range(1, backlog_events + 1):
        bundle = replace(
            harness.bundle,
            source_sequence=source_sequence,
            correlation_id=f"captured-queue-soak-{source_sequence}",
        )
        assert harness.publisher.reserve_sequence() == source_sequence
        receipt = harness.publisher.publish_bundle(
            bundle=bundle,
            scoring_authority=harness.scoring,
            evaluation_at=bundle.read_at,
            source_events=harness.source_events,
        )
        assert receipt.accepted is True

    harness.writer.start()
    assert harness.writer.close(timeout_seconds=10) is True
    assert harness.publisher.health().durable_through == backlog_events

    frontier = _frontier(harness.selection)
    port = _input_port(harness, max_batch_events=10, max_read_seconds=5.0)
    assert port.network_fallback_allowed is False
    assert port.broker_access_allowed is False
    assert port.mutation_allowed is False

    observed_sequences: list[int] = []
    for expected_sequence in range(1, backlog_events + 1):
        batch = port.read_batch(
            frontier=frontier,
            authority=harness.selection,
        )
        assert batch is not None
        assert batch.source_sequence_from == expected_sequence - 1
        assert batch.source_sequence_through == expected_sequence
        assert [row.source_sequence for row in batch.observations] == [
            expected_sequence
        ]
        observed_sequences.append(expected_sequence)
        frontier = _frontier(
            harness.selection,
            last_source_sequence=expected_sequence,
            last_batch_sha256=batch.batch_sha256,
        )
        if expected_sequence == restart_after:
            port = _input_port(
                harness,
                max_batch_events=10,
                max_read_seconds=5.0,
            )

    assert observed_sequences == list(range(1, backlog_events + 1))
    assert port.read_batch(
        frontier=frontier,
        authority=harness.selection,
    ) is None
    harness.manager.close()


def test_hash_verified_route_backlog_drains_across_reader_restart(tmp_path) -> None:
    """The fast suite covers the same route/restart contract at bounded cost."""

    _assert_hash_verified_route_backlog_drains(tmp_path, backlog_events=8)


def test_reader_uses_gate_deltas_and_publisher_restart_reverifies(
    tmp_path,
    monkeypatch,
) -> None:
    """Hot readers consume exact gate deltas; a process restart verifies disk."""

    harness = _harness(tmp_path)

    original_load = queue_module._load_commit_chain
    calls: list[int] = []

    def counted_load(*args, **kwargs):
        calls.append(1)
        return original_load(*args, **kwargs)

    monkeypatch.setattr(queue_module, "_load_commit_chain", counted_load)
    assert _publish(harness).accepted is True
    harness.writer.start()
    deadline = time.monotonic() + 5.0
    while harness.publisher.health().durable_through < 1:
        assert time.monotonic() < deadline
        time.sleep(0.005)

    port = _input_port(harness)
    first = port.read_batch(
        frontier=_frontier(harness.selection),
        authority=harness.selection,
    )
    assert first is not None and first.source_sequence_through == 1
    assert calls == []

    next_bundle = replace(
        harness.bundle,
        source_sequence=2,
        correlation_id="captured-queue-gate-delta-2",
    )
    assert _publish(harness, next_bundle).accepted is True
    deadline = time.monotonic() + 5.0
    while harness.publisher.health().durable_through < 2:
        assert time.monotonic() < deadline
        time.sleep(0.005)

    bounded = _input_port(harness, max_commit_files=1)
    with pytest.raises(
        CapturedPaperSelectionQueueUnavailable,
        match="verification failed",
    ):
        bounded.read_batch(
            frontier=_frontier(harness.selection),
            authority=harness.selection,
        )
    assert bounded.health().poisoned is True

    advanced = _frontier(
        harness.selection,
        last_source_sequence=1,
        last_batch_sha256=first.batch_sha256,
    )
    second = port.read_batch(
        frontier=advanced,
        authority=harness.selection,
    )
    assert second is not None and second.source_sequence_through == 2
    assert calls == []
    consumed = _frontier(
        harness.selection,
        last_source_sequence=2,
        last_batch_sha256=second.batch_sha256,
    )
    for _ in range(25):
        assert port.read_batch(
            frontier=consumed,
            authority=harness.selection,
        ) is None
    assert calls == []

    restarted = _input_port(harness)
    assert restarted.read_batch(
        frontier=consumed,
        authority=harness.selection,
    ) is None
    assert calls == []

    assert harness.writer.close(timeout_seconds=5) is True
    binding = harness.manager.resource_binding
    ingress = BoundedCaptureIngress.from_resource_binding(
        binding,
        shared_admission_budget=harness.manager.shared_admission_budget,
    )
    lease = harness.manager.acquire(harness.queue_identity)
    restarted_publisher = CapturedPaperSelectionQueuePublisher(
        writer_lease=lease,
        ingress=ingress,
        selection_authority=harness.selection,
        wall_clock=lambda: harness.now,
    )
    assert len(calls) == 1
    restarted_publisher.writer_lease.release()
    harness.manager.close()


def test_tampered_commit_fails_closed_without_network_fallback(tmp_path) -> None:
    harness = _harness(tmp_path)
    assert _publish(harness).accepted is True
    harness.writer.start()
    assert harness.writer.close(timeout_seconds=5) is True
    paths = tuple((harness.manager.store.root / "derived").rglob("*.json"))
    assert len(paths) == 1
    paths[0].write_bytes(paths[0].read_bytes() + b" ")

    port = _input_port(harness)
    with pytest.raises(
        CapturedPaperSelectionQueueUnavailable,
        match="verification failed",
    ):
        port.read_batch(
            frontier=_frontier(harness.selection), authority=harness.selection
        )
    assert port.network_fallback_allowed is False
    harness.manager.close()


def test_read_budget_timeout_is_transient_and_does_not_poison_health(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    assert _publish(harness).accepted is True
    harness.writer.start()
    assert harness.writer.close(timeout_seconds=5) is True
    ticks = iter((0.0, 1.0))
    port = CapturedPaperSelectionQueueInputPort(
        root=harness.manager.store.root,
        queue_identity=harness.queue_identity,
        selection_authority=harness.selection,
        durable_gate=harness.publisher.durable_gate,
        max_batch_events=10,
        max_batch_bytes=5_000_000,
        max_read_seconds=0.5,
        wall_clock=lambda: harness.now + timedelta(seconds=1),
        monotonic_clock=lambda: next(ticks),
    )

    with pytest.raises(
        CapturedPaperSelectionQueueReadTimeout,
        match="exceeded bounded time",
    ):
        port.read_batch(
            frontier=_frontier(harness.selection),
            authority=harness.selection,
        )

    health = port.health()
    assert health.poisoned is False
    assert health.poison_reason is None
    harness.manager.close()


def test_timed_delta_checkpoint_resumes_without_prefix_livelock(tmp_path) -> None:
    harness = _harness(tmp_path)
    commit_count = 8
    _publish_one_commit_at_a_time(harness, commit_count)
    ticks = iter(index * 0.2 for index in range(1, 10_000))
    port = CapturedPaperSelectionQueueInputPort(
        root=harness.manager.store.root,
        queue_identity=harness.queue_identity,
        selection_authority=harness.selection,
        durable_gate=harness.publisher.durable_gate,
        max_batch_events=10,
        max_batch_bytes=5_000_000,
        max_read_seconds=0.5,
        max_commit_files=100,
        wall_clock=lambda: harness.now + timedelta(seconds=1),
        monotonic_clock=lambda: next(ticks),
    )

    verified_counts: list[int] = []
    timeout_count = 0
    for _attempt in range(commit_count + 1):
        try:
            port.read_batch(
                frontier=_frontier(harness.selection),
                authority=harness.selection,
            )
        except CapturedPaperSelectionQueueReadTimeout:
            timeout_count += 1
        verified_counts.append(len(port._verified_chain))
        if verified_counts[-1] == commit_count:
            break

    assert verified_counts[-1] == commit_count
    assert timeout_count >= 1
    assert 0 < verified_counts[0] < commit_count
    assert len(verified_counts) > 1
    assert verified_counts == sorted(verified_counts)
    assert len(set(verified_counts)) == len(verified_counts)
    port.monotonic_clock = lambda: 100.0
    batch = port.read_batch(
        frontier=_frontier(harness.selection),
        authority=harness.selection,
    )
    assert batch is not None and batch.source_sequence_through == 1
    assert port.health().poisoned is False
    assert harness.writer.close(timeout_seconds=5) is True
    harness.manager.close()


def test_caught_up_reader_bisects_without_walking_consumed_prefix(
    tmp_path,
    monkeypatch,
) -> None:
    harness = _harness(tmp_path)
    commit_count = 32
    _publish_one_commit_at_a_time(harness, commit_count)
    port = _input_port(harness)
    consumed = _frontier(
        harness.selection,
        last_source_sequence=commit_count,
        last_batch_sha256=_digest("already-consumed-batch"),
    )
    assert port.read_batch(
        frontier=consumed,
        authority=harness.selection,
    ) is None

    class CountingList(list):
        def __init__(self, values):
            super().__init__(values)
            self.reads = 0
            self.iterated = 0

        def __getitem__(self, index):
            self.reads += 1
            return super().__getitem__(index)

        def __iter__(self):
            for value in super().__iter__():
                self.iterated += 1
                yield value

    counted = CountingList(port._verified_chain)
    port._verified_chain = counted

    def unexpected_materialization(*_args, **_kwargs):
        raise AssertionError("caught-up reader materialized a consumed commit")

    monkeypatch.setattr(
        queue_module,
        "_materialize_commit_events",
        unexpected_materialization,
    )
    assert port.read_batch(
        frontier=consumed,
        authority=harness.selection,
    ) is None
    assert counted.reads + counted.iterated < 15
    assert harness.writer.close(timeout_seconds=5) is True
    harness.manager.close()


def test_same_port_concurrent_delta_extension_is_serialized(
    tmp_path,
    monkeypatch,
) -> None:
    harness = _harness(tmp_path)
    _publish_one_commit_at_a_time(harness, 2)
    port = _input_port(harness)
    original_snapshot = harness.publisher.durable_gate._snapshot_since
    active = 0
    maximum_active = 0
    counter_lock = threading.Lock()

    def delayed_snapshot(*args, **kwargs):
        nonlocal active, maximum_active
        with counter_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.05)
            return original_snapshot(*args, **kwargs)
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(
        harness.publisher.durable_gate,
        "_snapshot_since",
        delayed_snapshot,
    )
    start = threading.Barrier(3)
    results: list[object] = []
    failures: list[BaseException] = []

    def reader() -> None:
        start.wait()
        try:
            results.append(
                port.read_batch(
                    frontier=_frontier(harness.selection),
                    authority=harness.selection,
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion payload
            failures.append(exc)

    workers = [threading.Thread(target=reader) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(timeout=5)

    assert not failures
    assert all(not worker.is_alive() for worker in workers)
    assert len(results) == 2
    assert maximum_active == 1
    assert port.health().poisoned is False
    assert harness.writer.close(timeout_seconds=5) is True
    harness.manager.close()


def test_durable_gate_rejects_cumulative_discontinuity_in_initial_chain(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    _publish_one_commit_at_a_time(harness, 2)
    assert harness.writer.close(timeout_seconds=5) is True

    durable, rows = harness.publisher.durable_gate._snapshot_since(
        0,
        max_commit_files=100,
    )
    forged_first = queue_module._LoadedCommit(
        object_ref=rows[0].object_ref,
        commit=replace(
            rows[0].commit,
            cumulative_sha256=_digest("forged-initial-cumulative"),
        ),
    )
    try:
        with pytest.raises(
            CapturedPaperSelectionQueueError,
            match="breaks the verified chain",
        ):
            queue_module.CapturedPaperSelectionQueueDurableGate(
                queue_identity_sha256=harness.queue_identity.identity_sha256,
                selection_authority_sha256=harness.selection.authority_sha256,
                expected_account_id=harness.selection.expected_account_id,
                activation_generation=harness.selection.activation_generation,
                commit_count=durable.commit_count,
                last_commit_sha256=durable.last_commit_sha256,
                durable_through=durable.durable_through,
                poisoned=durable.poisoned,
                poison_reason=durable.poison_reason,
                initial_chain=(forged_first, rows[1]),
            )
    finally:
        harness.manager.close()


@pytest.mark.parametrize(
    ("binding_field", "foreign_value"),
    [
        (
            "expected_account_id",
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        ),
        (
            "activation_generation",
            "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        ),
    ],
)
def test_durable_gate_rejects_foreign_account_generation_binding(
    tmp_path,
    binding_field,
    foreign_value,
) -> None:
    harness = _harness(tmp_path)
    _publish_one_commit_at_a_time(harness, 1)
    durable, rows = harness.publisher.durable_gate._snapshot_since(
        0,
        max_commit_files=100,
    )
    assert durable.commit_count == 1
    last = rows[-1]
    empty_gate = queue_module.CapturedPaperSelectionQueueDurableGate(
        queue_identity_sha256=harness.queue_identity.identity_sha256,
        selection_authority_sha256=harness.selection.authority_sha256,
        expected_account_id=harness.selection.expected_account_id,
        activation_generation=harness.selection.activation_generation,
        commit_count=0,
        last_commit_sha256=None,
        durable_through=0,
        poisoned=False,
        poison_reason=None,
        initial_chain=(),
    )
    empty_snapshot = empty_gate.snapshot()
    forged = queue_module._LoadedCommit(
        object_ref=last.object_ref,
        commit=replace(
            last.commit,
            **{binding_field: foreign_value},
        ),
    )
    with pytest.raises(
        CapturedPaperSelectionQueueError,
        match="stale or foreign",
    ):
        empty_gate._advance(forged)
    assert empty_gate.snapshot() == empty_snapshot
    assert harness.writer.close(timeout_seconds=5) is True
    harness.manager.close()


@pytest.mark.parametrize("invalid_tick", [float("nan"), float("inf"), 0.5])
def test_invalid_or_regressed_read_clock_remains_terminal(
    tmp_path,
    invalid_tick,
) -> None:
    harness = _harness(tmp_path)
    ticks = iter((1.0, invalid_tick))
    port = CapturedPaperSelectionQueueInputPort(
        root=harness.manager.store.root,
        queue_identity=harness.queue_identity,
        selection_authority=harness.selection,
        durable_gate=harness.publisher.durable_gate,
        max_batch_events=10,
        max_batch_bytes=5_000_000,
        max_read_seconds=0.5,
        wall_clock=lambda: harness.now + timedelta(seconds=1),
        monotonic_clock=lambda: next(ticks),
    )

    with pytest.raises(
        CapturedPaperSelectionQueueUnavailable,
        match="verification failed",
    ):
        port.read_batch(
            frontier=_frontier(harness.selection),
            authority=harness.selection,
        )

    health = port.health()
    assert health.poisoned is True
    assert "monotonic clock" in str(health.poison_reason)
    harness.publisher.writer_lease.release()
    harness.manager.close()
