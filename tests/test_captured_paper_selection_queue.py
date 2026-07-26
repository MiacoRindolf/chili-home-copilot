from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from app.services.trading.momentum_neural import (
    captured_paper_selection_queue as queue_module,
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
    CaptureOrtexSelectionSnapshot,
    CaptureClocks,
    CaptureContractError,
    CaptureEvent,
    CaptureEventRef,
    CaptureReadReceipt,
    CaptureRunIdentity,
    CaptureStream,
    CoverageGap,
    FSMDependencyProfile,
    FSMStreamDependency,
    ORTEX_SNAPSHOT_PROVIDER,
    ProviderWatermark,
    StreamCoverage,
    canonical_json_bytes,
    captured_read_result_sha256,
    sha256_json,
)
from app.services.trading.momentum_neural.short_mechanics import (
    OrtexShortMechanicsOutcome,
    ortex_public_policy,
)
from app.services.trading.momentum_neural.replay_capture_runtime import (
    BoundedCaptureIngress,
    CaptureBudgetPolicy,
    CaptureResourceBinding,
    CaptureResourceMeasurement,
    RetentionObjectRef,
    SharedCaptureAdmissionBudget,
    SharedCaptureStoreRuntime,
)
from tests.test_captured_viability_adapter import _fixture as _adapter_fixture


UTC = timezone.utc
BASE = datetime(2026, 7, 18, 16, 0, tzinfo=UTC)
ACCOUNT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
GENERATION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _resource_binding(
    *,
    max_queue_events: int = 100,
    available_memory_bytes: int = 192_000_000,
) -> CaptureResourceBinding:
    measurement = CaptureResourceMeasurement(
        measured_at=BASE,
        sample_seconds=5,
        total_memory_bytes=256_000_000,
        available_memory_bytes=available_memory_bytes,
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
    *, source_sequence: int
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
        CaptureStream.CODE_BUILD: {"fixture": "code", "build": "queue-test"},
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


def _with_ortex_batch(
    bundle: CapturedViabilityInputBundle,
    scoring: CapturedViabilityScoringAuthority,
    events: tuple[CaptureEvent, ...],
    *,
    member_count: int = 96,
) -> tuple[
    CapturedViabilityInputBundle,
    CapturedViabilityScoringAuthority,
    tuple[CaptureEvent, ...],
    str,
]:
    policy = ortex_public_policy()
    policy_sha256 = sha256_json(policy)
    observed_at = bundle.event_at - timedelta(milliseconds=500)
    symbols = [bundle.symbol]
    for index in range(max(0, member_count - 1)):
        symbols.append(
            "S"
            + chr(ord("A") + (index // (26 * 26)) % 26)
            + chr(ord("A") + (index // 26) % 26)
            + chr(ord("A") + index % 26)
        )
    members = []
    for symbol in sorted(set(symbols)):
        outcome = OrtexShortMechanicsOutcome.not_applicable(
            symbol=symbol,
            reason="outside_top_n",
            observed_at=observed_at,
            policy_sha256=policy_sha256,
            policy=policy,
        )
        mechanics = outcome.to_capture_dict()
        members.append(
            {
                "symbol": symbol,
                "short_mechanics": mechanics,
                "short_mechanics_sha256": sha256_json(mechanics),
                "squeeze_fuel_pct": None,
                "rank_pct": None,
            }
        )
    payload = {
        "schema_version": "chili.ortex-selection-snapshot.v1",
        "members": members,
        "members_sha256": sha256_json(members),
        "complete": True,
    }
    typed = CaptureOrtexSelectionSnapshot.from_dict(
        payload,
        expected_symbol=bundle.symbol,
    )
    ortex_event = CaptureEvent(
        identity=events[0].identity,
        sequence=max(event.sequence for event in events) + 1,
        stream=CaptureStream.ORTEX_SNAPSHOT,
        clocks=CaptureClocks(
            received_at=typed.source_received_at,
            available_at=typed.returned_at,
            market_reference_at=typed.market_reference_at,
        ),
        payload=payload,
        provider=ORTEX_SNAPSHOT_PROVIDER,
        symbol=bundle.symbol,
        query=typed.capture_query(),
    )
    CaptureOrtexSelectionSnapshot.from_event(ortex_event)
    ortex_ref = CaptureEventRef.from_event(ortex_event)
    ortex_read_id = "22222222-2222-4222-8222-222222222222"
    ortex_receipt = CaptureReadReceipt(
        read_id=ortex_read_id,
        decision_id=f"captured-queue-ortex-{bundle.source_sequence}",
        identity_sha256=bundle.capture_identity_sha256,
        stream=CaptureStream.ORTEX_SNAPSHOT,
        provider=ORTEX_SNAPSHOT_PROVIDER,
        symbol=bundle.symbol,
        requested_at=typed.requested_at,
        returned_at=typed.returned_at,
        query_sha256=ortex_event.query_sha256 or "",
        source_event_sha256s=(ortex_ref.event_sha256,),
        empty_result=False,
        result_sha256=captured_read_result_sha256((ortex_ref,)),
        query=typed.capture_query(),
    )
    receipt_sha256 = captured_viability_read_receipt_sha256(ortex_receipt)
    profile = bundle.dependency_inventory.dependency_profile
    dependency = FSMStreamDependency(
        stream=CaptureStream.ORTEX_SNAPSHOT,
        exact_provider_event_at_required=False,
        market_reference_at_required=True,
        max_source_age_seconds=300.0,
        coverage_start_at=min(
            row.coverage_start_at for row in profile.stream_dependencies
        ),
    )
    profile = FSMDependencyProfile(
        required_streams=profile.required_streams
        | {CaptureStream.ORTEX_SNAPSHOT},
        required_read_ids=profile.required_read_ids + (ortex_read_id,),
        stream_dependencies=profile.stream_dependencies + (dependency,),
    )
    bindings = []
    for binding in bundle.dependency_inventory.bindings:
        if binding.component == "execution_readiness":
            binding = CapturedViabilityDependencyBinding(
                component=binding.component,
                component_sha256=binding.component_sha256,
                source_event_sha256s=(
                    binding.source_event_sha256s + (ortex_ref.event_sha256,)
                ),
                read_receipt_sha256s=(
                    binding.read_receipt_sha256s + (receipt_sha256,)
                ),
            )
        bindings.append(binding)
    inventory = CapturedViabilityDependencyInventory(
        dependency_profile=profile,
        bindings=tuple(bindings),
    )
    coverage = StreamCoverage(
        stream=CaptureStream.ORTEX_SNAPSHOT,
        identity_sha256=bundle.capture_identity_sha256,
        provider=ORTEX_SNAPSHOT_PROVIDER,
        symbol=bundle.symbol,
        first_available_at=dependency.coverage_start_at,
        last_available_at=typed.returned_at,
        event_count=1,
        exact_event_clock_complete=False,
        content_verified=True,
        continuity_complete=True,
        query_receipt_count=1,
    )
    bundle = replace(
        bundle,
        dependency_inventory=inventory,
        source_refs=bundle.source_refs + (ortex_ref,),
        read_receipts=bundle.read_receipts + (ortex_receipt,),
        stream_coverages=bundle.stream_coverages + (coverage,),
    )
    scoring = replace(
        scoring,
        dependency_profile_sha256=profile.profile_sha256,
    )
    return bundle, scoring, events + (ortex_event,), ortex_event.payload_sha256


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
    available_memory_bytes: int = 192_000_000,
) -> _Harness:
    bundle, scoring, events, selection, queue_identity = _source_bundle(
        source_sequence=1
    )
    binding = _resource_binding(
        max_queue_events=max_queue_events,
        available_memory_bytes=available_memory_bytes,
    )
    shared = SharedCaptureAdmissionBudget.from_resource_binding(binding)
    manager = SharedCaptureStoreRuntime.create(
        tmp_path / "captured-selection-queue",
        resource_binding=binding,
        shared_admission_budget=shared,
        compression_codec="zlib",
    )
    ingress = BoundedCaptureIngress.from_resource_binding(
        binding,
        shared_admission_budget=shared,
    )
    lease = manager.acquire(queue_identity)
    now = bundle.read_at + timedelta(seconds=1)
    publisher = CapturedPaperSelectionQueuePublisher(
        writer_lease=lease,
        ingress=ingress,
        selection_authority=selection,
        wall_clock=lambda: now,
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


def test_reader_reuses_verified_prefix_but_restart_reverifies(
    tmp_path,
    monkeypatch,
) -> None:
    """Repeated reads are bounded; a fresh process still verifies all bytes."""

    harness = _harness(tmp_path)
    assert _publish(harness).accepted is True
    harness.writer.start()
    assert harness.writer.close(timeout_seconds=5) is True

    original_load = queue_module._load_commit_chain
    calls: list[int] = []

    def counted_load(*args, **kwargs):
        calls.append(1)
        return original_load(*args, **kwargs)

    monkeypatch.setattr(queue_module, "_load_commit_chain", counted_load)
    port = _input_port(harness)
    first = port.read_batch(
        frontier=_frontier(harness.selection),
        authority=harness.selection,
    )
    assert first is not None and first.source_sequence_through == 1
    assert len(calls) == 1

    advanced = _frontier(
        harness.selection,
        last_source_sequence=1,
        last_batch_sha256=first.batch_sha256,
    )
    for _ in range(25):
        assert port.read_batch(
            frontier=advanced,
            authority=harness.selection,
        ) is None
    assert len(calls) == 1

    restarted = _input_port(harness)
    assert restarted.read_batch(
        frontier=advanced,
        authority=harness.selection,
    ) is None
    assert len(calls) == 2
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


def _ortex_artifact_paths(root: Path) -> tuple[Path, ...]:
    matches = []
    for path in (root / "derived").rglob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            value.get("kind")
            == queue_module.QUEUE_ORTEX_MANIFEST_DERIVED_KIND
        ):
            matches.append(path)
    return tuple(sorted(matches))


def test_ortex_batch_is_one_durable_object_and_routes_stay_compact(
    tmp_path,
    monkeypatch,
) -> None:
    harness = _harness(tmp_path)
    submitted: list[CaptureEvent] = []
    durable_order: list[str] = []
    original_submit = harness.publisher.ingress.submit
    original_put_derived = harness.manager.store.put_derived_artifact
    original_sync_derived = harness.manager.store.sync_derived_ref

    def submit(event, **kwargs):
        submitted.append(event)
        return original_submit(event, **kwargs)

    def put_derived_artifact(**kwargs):
        durable_order.append(f"put:{kwargs['kind']}")
        return original_put_derived(**kwargs)

    def sync_derived_ref(ref):
        durable_order.append("sync:ortex_manifest")
        return original_sync_derived(ref)

    monkeypatch.setattr(harness.publisher.ingress, "submit", submit)
    monkeypatch.setattr(
        harness.manager.store,
        "put_derived_artifact",
        put_derived_artifact,
    )
    monkeypatch.setattr(
        harness.manager.store,
        "sync_derived_ref",
        sync_derived_ref,
    )
    expected_batch_sha256 = None
    full_manifest_bytes = None
    for sequence in range(1, 4):
        bundle, scoring, events, selection, _identity = _source_bundle(
            source_sequence=sequence
        )
        assert selection.to_dict() == harness.selection.to_dict()
        bundle, scoring, events, batch_sha256 = _with_ortex_batch(
            bundle,
            scoring,
            events,
        )
        expected_batch_sha256 = expected_batch_sha256 or batch_sha256
        assert batch_sha256 == expected_batch_sha256
        ortex_event = next(
            event
            for event in events
            if event.stream is CaptureStream.ORTEX_SNAPSHOT
        )
        full_manifest_bytes = len(canonical_json_bytes(ortex_event.payload))
        assert harness.publisher.reserve_sequence() == sequence
        receipt = harness.publisher.publish_bundle(
            bundle=bundle,
            scoring_authority=scoring,
            evaluation_at=bundle.read_at,
            source_events=events,
        )
        assert receipt.accepted is True
        ingress_health = harness.publisher.ingress.health()
        shared_health = ingress_health["shared_admission"]
        assert ingress_health["retained_objects"] == 1
        assert ingress_health["retained_bytes"] == full_manifest_bytes
        assert shared_health["outstanding_retained_objects"] == 1
        assert (
            shared_health["outstanding_retained_bytes"]
            == full_manifest_bytes
        )
        expected_bounded_bytes = (
            sum(event.canonical_size_bytes for event in submitted)
            + full_manifest_bytes
        )
        assert ingress_health["bounded_memory_bytes"] == expected_bounded_bytes
        assert shared_health["outstanding_bytes"] == expected_bounded_bytes
        assert shared_health["bytes_by_identity"] == {
            harness.queue_identity.identity_sha256: expected_bounded_bytes
        }

    assert len(submitted) == 3
    assert full_manifest_bytes is not None
    for queued_event in submitted:
        raw_sources = queued_event.payload["source_events"]
        ortex_source = next(
            row
            for row in raw_sources
            if row["event"]["stream"] == CaptureStream.ORTEX_SNAPSHOT.value
        )
        assert "payload" not in ortex_source["event"]
        assert set(ortex_source) == {
            "event",
            "event_sha256",
            "ortex_manifest_ref",
        }
        assert (
            ortex_source["ortex_manifest_ref"]["batch_sha256"]
            == expected_batch_sha256
        )
        assert queued_event.canonical_size_bytes < full_manifest_bytes

    harness.writer.start()
    assert harness.writer.close(timeout_seconds=5) is True
    ingress_health = harness.publisher.ingress.health()
    assert ingress_health["retained_objects"] == 0
    assert ingress_health["retained_bytes"] == 0
    assert (
        ingress_health["shared_admission"]["outstanding_retained_objects"]
        == 0
    )
    artifact_paths = _ortex_artifact_paths(harness.manager.store.root)
    assert len(artifact_paths) == 1
    assert durable_order == [
        f"put:{queue_module.QUEUE_ORTEX_MANIFEST_DERIVED_KIND}",
        "sync:ortex_manifest",
        f"put:{queue_module.QUEUE_DERIVED_KIND}",
    ]
    artifact = json.loads(artifact_paths[0].read_text(encoding="utf-8"))
    assert artifact["payload_sha256"] == expected_batch_sha256
    assert sha256_json(artifact["payload"]) == expected_batch_sha256

    commit_rows = []
    for path in (harness.manager.store.root / "derived").rglob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("kind") == queue_module.QUEUE_DERIVED_KIND:
            commit_rows.append(value)
    assert len(commit_rows) == 1
    pinned = commit_rows[0]["payload"]["ortex_manifest_refs"]
    assert len(pinned) == 1
    assert pinned[0]["sha256"] == artifact_paths[0].stem
    durable_ref = queue_module._retention_ref_from_dict(pinned[0])
    durable_record = (
        queue_module.ContentAddressedCaptureStore.read_derived_ref(
            harness.manager.store.root,
            durable_ref,
        )
    )
    ortex_source = next(
        row
        for row in submitted[0].payload["source_events"]
        if row["event"]["stream"] == CaptureStream.ORTEX_SNAPSHOT.value
    )
    wrong_size_ref = RetentionObjectRef(
        tier=durable_ref.tier,
        relative_path=durable_ref.relative_path,
        sha256=durable_ref.sha256,
        bytes=durable_ref.bytes + 1,
    )
    with pytest.raises(CaptureContractError, match="content mismatch"):
        queue_module._event_from_envelope(
            ortex_source,
            root=harness.manager.store.root,
            queue_identity=harness.queue_identity,
            ortex_manifest_refs=(wrong_size_ref,),
            manifest_cache={
                expected_batch_sha256: (durable_record, durable_ref)
            },
            budget_check=lambda: None,
        )

    with pytest.raises(
        CapturedPaperSelectionQueueUnavailable,
        match="batch byte limit",
    ):
        _input_port(
            harness,
            max_batch_bytes=durable_ref.bytes,
        ).read_batch(
            frontier=_frontier(harness.selection),
            authority=harness.selection,
        )

    batch = _input_port(harness).read_batch(
        frontier=_frontier(harness.selection),
        authority=harness.selection,
    )
    assert batch is not None and batch.source_sequence_through == 1
    harness.manager.close()


def test_ortex_route_fanout_retains_one_bounded_payload_until_last_ack(
    tmp_path,
) -> None:
    harness = _harness(tmp_path)
    manifest_bytes = None
    for sequence in range(1, 4):
        bundle, scoring, events, selection, _identity = _source_bundle(
            source_sequence=sequence
        )
        assert selection.to_dict() == harness.selection.to_dict()
        bundle, scoring, events, _batch_sha256 = _with_ortex_batch(
            bundle,
            scoring,
            events,
        )
        ortex_event = next(
            event
            for event in events
            if event.stream is CaptureStream.ORTEX_SNAPSHOT
        )
        manifest_bytes = len(canonical_json_bytes(ortex_event.payload))
        assert harness.publisher.reserve_sequence() == sequence
        assert harness.publisher.publish_bundle(
            bundle=bundle,
            scoring_authority=scoring,
            evaluation_at=bundle.read_at,
            source_events=events,
        ).accepted

    assert manifest_bytes is not None
    for expected_through in range(1, 4):
        batch = harness.publisher.ingress.pop_batch(
            max_events=1,
            max_bytes=harness.manager.resource_binding.budget.async_queue_bytes,
            timeout_seconds=0,
        )
        assert len(batch.events) == 1
        event_chunks = harness.manager.store.write_events(batch.events)
        gap_chunks = harness.manager.store.write_gaps(batch.gaps)
        harness.manager.store.sync()
        prepared = harness.publisher._prepare_commit(
            store=harness.manager.store,
            batch=batch,
            event_chunks=event_chunks,
            gap_chunks=gap_chunks,
        )
        harness.manager.store.sync()
        harness.publisher._acknowledge_commit(prepared)
        harness.publisher.ingress.complete_shared_admission(batch.events)

        health = harness.publisher.ingress.health()
        expected_objects = 0 if expected_through == 3 else 1
        expected_bytes = 0 if expected_through == 3 else manifest_bytes
        assert health["retained_objects"] == expected_objects
        assert health["retained_bytes"] == expected_bytes
        assert (
            health["shared_admission"]["outstanding_retained_objects"]
            == expected_objects
        )
        assert (
            health["shared_admission"]["outstanding_retained_bytes"]
            == expected_bytes
        )
        assert harness.publisher.health().durable_through == expected_through

    assert len(_ortex_artifact_paths(harness.manager.store.root)) == 1
    assert harness.writer.close(timeout_seconds=5) is False
    harness.manager.close()


def test_ortex_32_route_fanout_racing_writer_never_multiplies_payload(
    tmp_path,
    monkeypatch,
) -> None:
    harness = _harness(tmp_path)
    retained_observations = []
    original_submit = harness.publisher.ingress.submit

    def observed_submit(event, **kwargs):
        accepted = original_submit(event, **kwargs)
        retained_observations.append(harness.publisher.ingress.health())
        return accepted

    monkeypatch.setattr(
        harness.publisher.ingress,
        "submit",
        observed_submit,
    )
    harness.writer.start()
    batch_sha256 = None
    manifest_bytes = None
    for sequence in range(1, 33):
        bundle, scoring, events, selection, _identity = _source_bundle(
            source_sequence=sequence
        )
        assert selection.to_dict() == harness.selection.to_dict()
        bundle, scoring, events, current_sha256 = _with_ortex_batch(
            bundle,
            scoring,
            events,
            member_count=10,
        )
        batch_sha256 = batch_sha256 or current_sha256
        assert current_sha256 == batch_sha256
        ortex_event = next(
            event
            for event in events
            if event.stream is CaptureStream.ORTEX_SNAPSHOT
        )
        manifest_bytes = len(canonical_json_bytes(ortex_event.payload))
        assert harness.publisher.reserve_sequence() == sequence
        assert harness.publisher.publish_bundle(
            bundle=bundle,
            scoring_authority=scoring,
            evaluation_at=bundle.read_at,
            source_events=events,
        ).accepted

    assert harness.writer.close(timeout_seconds=10) is True
    assert manifest_bytes is not None
    assert len(retained_observations) == 32
    assert all(row["retained_objects"] == 1 for row in retained_observations)
    assert all(
        row["retained_bytes"] == manifest_bytes
        for row in retained_observations
    )
    assert harness.publisher.health().durable_through == 32
    assert harness.writer.worker.health()["last_error"] is None
    assert len(_ortex_artifact_paths(harness.manager.store.root)) == 1
    health = harness.publisher.ingress.health()
    assert health["retained_objects"] == 0
    assert health["shared_admission"]["outstanding_retained_objects"] == 0
    harness.manager.close()


def test_ortex_manifest_over_budget_rejects_and_releases_staged_payload(
    tmp_path,
) -> None:
    harness = _harness(
        tmp_path,
        available_memory_bytes=34_000_000,
    )
    bundle, scoring, events, _selection, _identity = _source_bundle(
        source_sequence=1
    )
    bundle, scoring, events, _batch_sha256 = _with_ortex_batch(
        bundle,
        scoring,
        events,
        member_count=500,
    )
    ortex_event = next(
        event
        for event in events
        if event.stream is CaptureStream.ORTEX_SNAPSHOT
    )
    assert (
        len(canonical_json_bytes(ortex_event.payload))
        > harness.publisher.ingress.max_bytes
    )
    assert harness.publisher.reserve_sequence() == 1
    receipt = harness.publisher.publish_bundle(
        bundle=bundle,
        scoring_authority=scoring,
        evaluation_at=bundle.read_at,
        source_events=events,
    )
    assert receipt.accepted is False
    health = harness.publisher.ingress.health()
    assert health["retained_objects"] == 0
    assert health["retained_bytes"] == 0
    assert health["shared_admission"]["outstanding_retained_objects"] == 0
    assert health["shared_admission"]["outstanding_retained_bytes"] == 0
    assert health["dropped"] == 1
    assert not _ortex_artifact_paths(harness.manager.store.root)

    harness.writer.start()
    assert harness.writer.close(timeout_seconds=5) is True
    harness.manager.close()


def test_ortex_manifest_fsync_then_commit_crash_restarts_without_frontier(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "captured-selection-queue"
    first = _harness(tmp_path)
    bundle, scoring, events, _selection, _identity = _source_bundle(
        source_sequence=1
    )
    bundle, scoring, events, batch_sha256 = _with_ortex_batch(
        bundle,
        scoring,
        events,
    )
    assert first.publisher.reserve_sequence() == 1
    assert first.publisher.publish_bundle(
        bundle=bundle,
        scoring_authority=scoring,
        evaluation_at=bundle.read_at,
        source_events=events,
    ).accepted

    original_put_derived = first.manager.store.put_derived_artifact
    original_sync_derived = first.manager.store.sync_derived_ref
    manifest_was_synced = False

    def crash_before_commit(**kwargs):
        if kwargs["kind"] == queue_module.QUEUE_DERIVED_KIND:
            assert manifest_was_synced
            raise RuntimeError("crash_after_ortex_manifest_fsync")
        return original_put_derived(**kwargs)

    def observe_manifest_sync(ref):
        nonlocal manifest_was_synced
        result = original_sync_derived(ref)
        manifest_was_synced = True
        return result

    monkeypatch.setattr(
        first.manager.store,
        "put_derived_artifact",
        crash_before_commit,
    )
    monkeypatch.setattr(
        first.manager.store,
        "sync_derived_ref",
        observe_manifest_sync,
    )
    first.writer.start()
    assert first.writer.close(timeout_seconds=5) is False
    assert manifest_was_synced is True
    assert len(_ortex_artifact_paths(root)) == 1
    assert first.publisher.health().durable_through == 0
    first_port = _input_port(first)
    assert first_port.network_fallback_allowed is False
    assert first_port.read_batch(
        frontier=_frontier(first.selection),
        authority=first.selection,
    ) is None
    first_health = first.publisher.ingress.health()
    assert first_health["retained_objects"] == 0
    assert first_health["shared_admission"]["outstanding_retained_objects"] == 0
    first.manager.close()

    second = _harness(tmp_path)
    retry_bundle, retry_scoring, retry_events, _selection, _identity = (
        _source_bundle(source_sequence=1)
    )
    retry_bundle, retry_scoring, retry_events, retry_sha256 = _with_ortex_batch(
        retry_bundle,
        retry_scoring,
        retry_events,
    )
    assert retry_sha256 == batch_sha256
    assert second.publisher.reserve_sequence() == 1
    assert second.publisher.publish_bundle(
        bundle=retry_bundle,
        scoring_authority=retry_scoring,
        evaluation_at=retry_bundle.read_at,
        source_events=retry_events,
    ).accepted
    second.writer.start()
    assert second.writer.close(timeout_seconds=5) is True
    assert len(_ortex_artifact_paths(root)) == 1
    batch = _input_port(second).read_batch(
        frontier=_frontier(second.selection),
        authority=second.selection,
    )
    assert batch is not None
    assert batch.source_sequence_through == 1
    second.manager.close()


def test_ortex_manifest_restart_retry_is_content_idempotent(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "captured-selection-queue"
    first = _harness(tmp_path)
    bundle, scoring, events, _selection, _identity = _source_bundle(
        source_sequence=1
    )
    bundle, scoring, events, batch_sha256 = _with_ortex_batch(
        bundle,
        scoring,
        events,
    )
    assert first.publisher.reserve_sequence() == 1
    assert first.publisher.publish_bundle(
        bundle=bundle,
        scoring_authority=scoring,
        evaluation_at=bundle.read_at,
        source_events=events,
    ).accepted
    first.writer.start()
    assert first.writer.close(timeout_seconds=5) is True
    assert len(_ortex_artifact_paths(root)) == 1
    binding = first.manager.resource_binding
    selection = first.selection
    queue_identity = first.queue_identity
    first.manager.close()

    shared = SharedCaptureAdmissionBudget.from_resource_binding(binding)
    manager = SharedCaptureStoreRuntime.create(
        root,
        resource_binding=binding,
        shared_admission_budget=shared,
        compression_codec="zlib",
    )
    ingress = BoundedCaptureIngress.from_resource_binding(
        binding,
        shared_admission_budget=shared,
    )
    publisher = CapturedPaperSelectionQueuePublisher(
        writer_lease=manager.acquire(queue_identity),
        ingress=ingress,
        selection_authority=selection,
        wall_clock=lambda: bundle.read_at + timedelta(seconds=1),
    )
    retry_sync_refs = []
    original_sync_derived = manager.store.sync_derived_ref

    def sync_derived_ref(ref):
        retry_sync_refs.append(ref)
        return original_sync_derived(ref)

    monkeypatch.setattr(
        manager.store,
        "sync_derived_ref",
        sync_derived_ref,
    )
    writer = CapturedPaperSelectionQueueWriter(
        publisher=publisher,
        batch_events=10,
        batch_bytes=binding.budget.async_queue_bytes,
        poll_seconds=0.001,
        flush_interval_seconds=0.001,
    )
    retry_bundle, retry_scoring, retry_events, *_rest = _source_bundle(
        source_sequence=2
    )
    retry_bundle, retry_scoring, retry_events, retry_sha256 = _with_ortex_batch(
        retry_bundle,
        retry_scoring,
        retry_events,
    )
    assert retry_sha256 == batch_sha256
    assert publisher.reserve_sequence() == 2
    assert publisher.publish_bundle(
        bundle=retry_bundle,
        scoring_authority=retry_scoring,
        evaluation_at=retry_bundle.read_at,
        source_events=retry_events,
    ).accepted
    writer.start()
    assert writer.close(timeout_seconds=5) is True
    assert len(_ortex_artifact_paths(root)) == 1
    assert len(retry_sync_refs) == 1
    manager.close()


@pytest.mark.parametrize("mutation", ("missing", "tampered"))
def test_ortex_manifest_missing_or_tampered_fails_closed(
    tmp_path,
    mutation: str,
) -> None:
    harness = _harness(tmp_path)
    bundle, scoring, events, _selection, _identity = _source_bundle(
        source_sequence=1
    )
    bundle, scoring, events, _batch_sha256 = _with_ortex_batch(
        bundle,
        scoring,
        events,
    )
    assert harness.publisher.reserve_sequence() == 1
    assert harness.publisher.publish_bundle(
        bundle=bundle,
        scoring_authority=scoring,
        evaluation_at=bundle.read_at,
        source_events=events,
    ).accepted
    harness.writer.start()
    assert harness.writer.close(timeout_seconds=5) is True
    (artifact_path,) = _ortex_artifact_paths(harness.manager.store.root)
    if mutation == "missing":
        artifact_path.unlink()
    else:
        artifact_path.write_bytes(artifact_path.read_bytes() + b" ")

    with pytest.raises(
        CapturedPaperSelectionQueueUnavailable,
        match="verification failed",
    ):
        _input_port(harness).read_batch(
            frontier=_frontier(harness.selection),
            authority=harness.selection,
        )
    assert harness.publisher.health().durable_through == 1
    harness.manager.close()
