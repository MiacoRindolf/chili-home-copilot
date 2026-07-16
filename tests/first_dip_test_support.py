"""Focused helpers for typed captured-DB-paper first-dip mechanics tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import uuid

from app.services.trading.momentum_neural import first_dip_tape_decision as decision
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    AdaptiveRiskReservationRequest,
)
from app.services.trading.momentum_neural.first_dip_tape_policy import (
    FirstDipTapePolicy,
    FirstDipTapeReadQuery,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureClocks,
    CaptureEventRef,
    CaptureProducerSpec,
    CaptureReadReceipt,
    CaptureRunIdentity,
    CaptureStream,
    FSMDependencyProfile,
    FSMStreamDependency,
    IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
    ProviderWatermark,
    StreamCoverage,
    captured_read_result_sha256,
    sha256_json,
)
from app.services.trading.momentum_neural.replay_capture_runtime import (
    BoundedCaptureIngress,
    CaptureBudgetPolicy,
    CaptureProducerLifecycleRuntime,
    CaptureResourceBinding,
    CaptureResourceMeasurement,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


UTC = timezone.utc


class _ManualClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def set(self, value: datetime) -> datetime:
        self.value = value
        return value


@dataclass(frozen=True)
class CapturedFirstDipRuntimeFixture:
    """Exact two-checkpoint captured-paper proof for one adaptive request."""

    runtime: CaptureProducerLifecycleRuntime
    request: AdaptiveRiskReservationRequest
    policy: FirstDipTapePolicy
    detector_request: decision.FirstDipTapeDecisionRequest
    detector_proof: object
    detector_authority: object
    detector_resolution: decision.FirstDipTapeDecisionResolution
    prior_detector_reference_sha256: str
    final_proof: object
    final_authority: object


def captured_first_dip_detector_authority(
    request: decision.FirstDipTapeDecisionRequest,
) -> object:
    """Issue one detector authority from an exact active-capture prefix."""

    if type(request) is not decision.FirstDipTapeDecisionRequest:
        raise TypeError("detector request must be exact")
    warmup_at = (
        request.decision_at
        - timedelta(seconds=request.policy.window_seconds)
        - timedelta(milliseconds=10)
    )
    opened_at = warmup_at - timedelta(milliseconds=20)
    clock = _ManualClock(opened_at)
    identity = CaptureRunIdentity(
        run_id=str(uuid.uuid5(uuid.NAMESPACE_URL, request.symbol + ":detector")),
        generation=1,
        code_build_sha256=_digest("first-dip-detector-build"),
        config_sha256=_digest("first-dip-detector-config"),
        feature_flags_sha256=_digest("first-dip-detector-flags"),
        account_identity_sha256=_digest("first-dip-detector-account"),
        broker="alpaca",
        broker_environment="paper",
    )
    binding = _resource_binding(opened_at)
    producer = CaptureProducerSpec(
        producer_id="iqfeed_first_dip_detector",
        instance_id=str(uuid.uuid5(uuid.NAMESPACE_URL, request.symbol + ":producer")),
        generation=identity.generation,
        streams=(CaptureStream.IQFEED_PRINT,),
        code_build_sha256=identity.code_build_sha256,
        config_sha256=identity.config_sha256,
        feature_flags_sha256=identity.feature_flags_sha256,
        resource_binding_sha256=binding.binding_sha256,
    )
    runtime = CaptureProducerLifecycleRuntime(
        identity=identity,
        ingress=BoundedCaptureIngress.from_resource_binding(binding),
        resource_binding=binding,
        producers=(producer,),
        heartbeat_timeout_seconds=max(1.0, request.policy.window_seconds + 1.0),
        wall_clock=clock,
    )
    runtime.open(opened_at=opened_at)
    runtime.register(
        producer.producer_id,
        recorded_at=clock.set(opened_at + timedelta(milliseconds=1)),
    )
    warmup = runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol=request.symbol,
        clocks=CaptureClocks(
            provider_event_at=warmup_at,
            received_at=warmup_at,
            available_at=warmup_at,
        ),
        payload={
            "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
            "symbol": request.symbol,
            "price": 8.50,
            "size": 100,
            "conditions": ["fixture-warmup"],
        },
        recorded_at=clock.set(warmup_at),
    )
    runtime.heartbeat(
        producer.producer_id,
        recorded_at=clock.set(
            request.decision_at - timedelta(milliseconds=50)
        ),
    )
    returned_at = request.decision_at
    clock.set(returned_at)
    sources = tuple(
        runtime.submit_input(
            producer.producer_id,
            stream=CaptureStream.IQFEED_PRINT,
            provider="iqfeed",
            symbol=request.symbol,
            clocks=CaptureClocks(
                provider_event_at=provider_at,
                received_at=returned_at,
                available_at=returned_at,
            ),
            payload={
                "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
                "symbol": request.symbol,
                "price": price,
                "size": size,
                "bid": price - 0.01,
                "ask": price,
                "conditions": ["fixture-captured"],
            },
            recorded_at=returned_at,
        )
        for provider_at, price, size in (
            (returned_at - timedelta(milliseconds=30), 8.50, 100),
            (returned_at - timedelta(milliseconds=20), 8.61, 200),
            (returned_at - timedelta(milliseconds=10), 8.62, 300),
        )
    )
    query = FirstDipTapeReadQuery(
        symbol=request.symbol,
        provider="iqfeed",
        event_start_exclusive=returned_at
        - timedelta(seconds=request.policy.window_seconds),
        event_end_inclusive=returned_at,
        decision_at=returned_at,
        available_at_most=returned_at,
        source_frontier_sequence=sources[-1].sequence,
        policy_sha256=request.policy.policy_sha256,
    )
    read_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            request.symbol + request.decision_at.isoformat() + ":read",
        )
    )
    receipt = CaptureReadReceipt(
        read_id=read_id,
        decision_id=request.symbol + ":first-dip-detector",
        identity_sha256=identity.identity_sha256,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol=request.symbol,
        requested_at=returned_at - timedelta(milliseconds=40),
        returned_at=returned_at,
        query_sha256=sha256_json(query.to_dict()),
        source_event_sha256s=tuple(row.event_sha256 for row in sources),
        empty_result=False,
        result_sha256=captured_read_result_sha256(
            tuple(CaptureEventRef.from_event(row) for row in sources)
        ),
        query=query.to_dict(),
    )
    runtime.submit_first_dip_tape_receipt(receipt)
    continuity_at = clock.set(returned_at + timedelta(milliseconds=2))
    runtime.submit_live_continuity_checkpoint(
        producer.producer_id,
        StreamCoverage(
            stream=CaptureStream.IQFEED_PRINT,
            identity_sha256=identity.identity_sha256,
            provider="iqfeed",
            symbol=request.symbol,
            first_available_at=warmup.clocks.available_at,
            last_available_at=returned_at,
            event_count=4,
            exact_event_clock_complete=True,
            content_verified=True,
            continuity_complete=True,
            watermark=ProviderWatermark(
                stream=CaptureStream.IQFEED_PRINT,
                provider="iqfeed",
                identity_sha256=identity.identity_sha256,
                event_watermark_at=returned_at,
                emitted_available_at=continuity_at,
                bounded_lateness_seconds=1,
                max_observed_lateness_seconds=0.05,
                generation=identity.generation,
                symbol=request.symbol,
            ),
        ),
        recorded_at=continuity_at,
    )
    profile = FSMDependencyProfile(
        required_streams=frozenset({CaptureStream.IQFEED_PRINT}),
        required_read_ids=(read_id,),
        stream_dependencies=(
            FSMStreamDependency(
                stream=CaptureStream.IQFEED_PRINT,
                exact_provider_event_at_required=True,
                market_reference_at_required=False,
                max_source_age_seconds=request.policy.max_source_age_seconds,
                coverage_start_at=query.event_start_exclusive,
            ),
        ),
    )
    clock.set(returned_at + timedelta(milliseconds=3))
    proof = runtime.attest_predecision_input_prefix(
        decision_id=receipt.decision_id,
        dependency_profile=profile,
        first_dip_tape_read_id=read_id,
    )
    return runtime.prepare_captured_first_dip_tape_authority(
        attestation=proof,
        policy=request.policy,
        purpose=decision.FIRST_DIP_TAPE_PURPOSE_DETECTOR,
    )


def _resource_binding(measured_at: datetime) -> CaptureResourceBinding:
    measurement = CaptureResourceMeasurement(
        measured_at=measured_at,
        sample_seconds=5,
        total_memory_bytes=256_000_000,
        available_memory_bytes=192_000_000,
        disk_free_bytes=2_000_000_000,
        average_cpu_percent=20,
        sustained_append_bytes_per_second=20_000_000,
        fsync_p95_milliseconds=5,
        logical_cpu_count=8,
        host_fingerprint_sha256=_digest("first-dip-runtime-fixture-host"),
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
        max_queue_events=100,
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


def captured_first_dip_runtime_for_adaptive_request(
    adaptive_request: AdaptiveRiskReservationRequest,
) -> CapturedFirstDipRuntimeFixture:
    """Produce detector and final authority through the real capture runtime.

    The returned request is a strict copy whose capture-prefix evidence is bound
    to the detector checkpoint actually produced below.  No arbitrary binding,
    receipt, lineage, callback, or provenance hash enters the issuer.
    """

    if type(adaptive_request) is not AdaptiveRiskReservationRequest:
        raise TypeError("adaptive request must be exact")
    if (
        adaptive_request.setup_family != "first_dip_reclaim"
        or adaptive_request.opportunity_key is None
    ):
        raise ValueError("fixture requires a first-dip opportunity request")

    inputs = adaptive_request.inputs
    base = inputs.as_of - timedelta(milliseconds=500)
    clock = _ManualClock(base)
    identity = CaptureRunIdentity(
        run_id=inputs.replay_or_paper_run_id,
        generation=inputs.generation,
        code_build_sha256=inputs.code_build_sha256,
        config_sha256=inputs.effective_config_sha256,
        feature_flags_sha256=inputs.feature_flags_sha256,
        account_identity_sha256=inputs.account_identity_sha256,
        broker="alpaca",
        broker_environment="paper",
    )
    binding = _resource_binding(base)
    producer = CaptureProducerSpec(
        producer_id="iqfeed_first_dip_fixture",
        instance_id=str(uuid.uuid5(uuid.NAMESPACE_URL, inputs.decision_id)),
        generation=identity.generation,
        streams=(CaptureStream.IQFEED_PRINT,),
        code_build_sha256=identity.code_build_sha256,
        config_sha256=identity.config_sha256,
        feature_flags_sha256=identity.feature_flags_sha256,
        resource_binding_sha256=binding.binding_sha256,
    )
    runtime = CaptureProducerLifecycleRuntime(
        identity=identity,
        ingress=BoundedCaptureIngress.from_resource_binding(binding),
        resource_binding=binding,
        producers=(producer,),
        heartbeat_timeout_seconds=1,
        wall_clock=clock,
    )
    runtime.open(opened_at=base)
    runtime.register(
        producer.producer_id,
        recorded_at=clock.set(base + timedelta(milliseconds=10)),
    )
    warmup_at = clock.set(base + timedelta(milliseconds=20))
    warmup = runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol=inputs.symbol,
        clocks=CaptureClocks(
            provider_event_at=warmup_at,
            received_at=warmup_at,
            available_at=warmup_at,
        ),
        payload={
            "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
            "symbol": inputs.symbol,
            "price": 9.90,
            "size": 100,
            "conditions": ["fixture-warmup"],
        },
        recorded_at=warmup_at,
    )
    policy = FirstDipTapePolicy(
        window_seconds=0.05,
        max_source_age_seconds=1.0,
        tick_rate_floor_pctile=0.0,
        minimum_prints=3,
    )
    cumulative_event_count = 1

    def stage_read(
        *,
        decision_id: str,
        read_id: str,
        returned_at: datetime,
        prices: tuple[float, float, float],
    ):
        nonlocal cumulative_event_count
        clock.set(returned_at)
        source_rows = tuple(
            runtime.submit_input(
                producer.producer_id,
                stream=CaptureStream.IQFEED_PRINT,
                provider="iqfeed",
                symbol=inputs.symbol,
                clocks=CaptureClocks(
                    provider_event_at=provider_at,
                    received_at=returned_at,
                    available_at=returned_at,
                ),
                payload={
                    "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
                    "symbol": inputs.symbol,
                    "price": price,
                    "size": size,
                    "bid": price - 0.01,
                    "ask": price,
                    "conditions": ["fixture-captured"],
                },
                recorded_at=returned_at,
            )
            for provider_at, price, size in (
                (returned_at - timedelta(milliseconds=30), prices[0], 100),
                (returned_at - timedelta(milliseconds=20), prices[1], 200),
                (returned_at - timedelta(milliseconds=10), prices[2], 300),
            )
        )
        cumulative_event_count += len(source_rows)
        query = FirstDipTapeReadQuery(
            symbol=inputs.symbol,
            provider="iqfeed",
            event_start_exclusive=returned_at
            - timedelta(seconds=policy.window_seconds),
            event_end_inclusive=returned_at,
            decision_at=returned_at,
            available_at_most=returned_at,
            source_frontier_sequence=source_rows[-1].sequence,
            policy_sha256=policy.policy_sha256,
        )
        receipt = CaptureReadReceipt(
            read_id=read_id,
            decision_id=decision_id,
            identity_sha256=identity.identity_sha256,
            stream=CaptureStream.IQFEED_PRINT,
            provider="iqfeed",
            symbol=inputs.symbol,
            requested_at=returned_at - timedelta(milliseconds=40),
            returned_at=returned_at,
            query_sha256=sha256_json(query.to_dict()),
            source_event_sha256s=tuple(
                row.event_sha256 for row in source_rows
            ),
            empty_result=False,
            result_sha256=captured_read_result_sha256(
                tuple(CaptureEventRef.from_event(row) for row in source_rows)
            ),
            query=query.to_dict(),
        )
        runtime.submit_first_dip_tape_receipt(receipt)
        continuity_at = clock.set(returned_at + timedelta(milliseconds=2))
        runtime.submit_live_continuity_checkpoint(
            producer.producer_id,
            StreamCoverage(
                stream=CaptureStream.IQFEED_PRINT,
                identity_sha256=identity.identity_sha256,
                provider="iqfeed",
                symbol=inputs.symbol,
                first_available_at=warmup.clocks.available_at,
                last_available_at=returned_at,
                event_count=cumulative_event_count,
                exact_event_clock_complete=True,
                content_verified=True,
                continuity_complete=True,
                watermark=ProviderWatermark(
                    stream=CaptureStream.IQFEED_PRINT,
                    provider="iqfeed",
                    identity_sha256=identity.identity_sha256,
                    event_watermark_at=returned_at,
                    emitted_available_at=continuity_at,
                    bounded_lateness_seconds=1,
                    max_observed_lateness_seconds=0.05,
                    generation=identity.generation,
                    symbol=inputs.symbol,
                ),
            ),
            recorded_at=continuity_at,
        )
        profile = FSMDependencyProfile(
            required_streams=frozenset({CaptureStream.IQFEED_PRINT}),
            required_read_ids=(read_id,),
            stream_dependencies=(
                FSMStreamDependency(
                    stream=CaptureStream.IQFEED_PRINT,
                    exact_provider_event_at_required=True,
                    market_reference_at_required=False,
                    max_source_age_seconds=1.0,
                    coverage_start_at=query.event_start_exclusive,
                ),
            ),
        )
        clock.set(returned_at + timedelta(milliseconds=3))
        return receipt, profile

    detector_at = base + timedelta(milliseconds=100)
    detector_read_id = str(uuid.uuid5(uuid.NAMESPACE_URL, inputs.decision_id + ":detector"))
    detector_receipt, detector_profile = stage_read(
        # Production binds the adaptive source and detector attestation to the
        # same deterministic entry decision id.  The later final tape read has
        # its own checkpoint id and cannot alias this prefix.
        decision_id=inputs.decision_id,
        read_id=detector_read_id,
        returned_at=detector_at,
        prices=(9.90, 10.01, 10.02),
    )
    detector_proof = runtime.attest_predecision_input_prefix(
        decision_id=detector_receipt.decision_id,
        dependency_profile=detector_profile,
        first_dip_tape_read_id=detector_read_id,
    )
    detector_authority = runtime.prepare_captured_first_dip_tape_authority(
        attestation=detector_proof,
        policy=policy,
        purpose=decision.FIRST_DIP_TAPE_PURPOSE_DETECTOR,
    )
    detector_request = decision.FirstDipTapeDecisionRequest(
        symbol=inputs.symbol,
        decision_at=detector_at,
        policy=policy,
        purpose=decision.FIRST_DIP_TAPE_PURPOSE_DETECTOR,
    )
    with decision._installed_captured_db_paper_first_dip_tape_decision_authority(
        detector_authority
    ):
        detector_resolution = decision.resolve_first_dip_tape_decision(
            symbol=detector_request.symbol,
            decision_at=detector_request.decision_at,
            policy=detector_request.policy,
            purpose=detector_request.purpose,
        )
    if (
        not detector_resolution.run_bound
        or detector_resolution.evaluation.status != "valid_positive"
        or detector_resolution.receipt is None
    ):
        raise RuntimeError(
            "captured detector fixture did not resolve positive: "
            f"status={detector_resolution.evaluation.status},"
            f"reason={detector_resolution.evaluation.reason},"
            f"confirmed={detector_resolution.evaluation.confirmed},"
            f"features={detector_resolution.evaluation.features},"
            f"sources={detector_resolution.evaluation.source_event_sha256s}"
        )

    capture_evidence = inputs.evidence["capture_prefix"]
    evidence = dict(inputs.evidence)
    evidence["capture_prefix"] = replace(
        capture_evidence,
        observed_at=detector_at,
        available_at=detector_proof.attested_available_at,
        content_sha256=detector_proof.input_prefix_root_sha256,
    )
    rebound_request = replace(
        adaptive_request,
        inputs=replace(
            inputs,
            capture_prefix_root_sha256=detector_proof.input_prefix_root_sha256,
            evidence=evidence,
        ),
    )
    opportunity_sha256 = rebound_request.opportunity_key.key_sha256
    prior_sha256 = runtime.retain_accepted_first_dip_detector(
        resolution=detector_resolution,
        opportunity_key_sha256=opportunity_sha256,
    )

    final_at = inputs.as_of + timedelta(milliseconds=100)
    final_read_id = str(uuid.uuid5(uuid.NAMESPACE_URL, inputs.decision_id + ":final"))
    final_receipt, final_profile = stage_read(
        decision_id=inputs.decision_id + ":first-dip-final",
        read_id=final_read_id,
        returned_at=final_at,
        prices=(10.03, 10.10, 10.12),
    )
    final_proof = runtime.attest_first_dip_pre_reservation_input_prefix(
        adaptive_request=rebound_request,
        dependency_profile=final_profile,
        first_dip_tape_read_id=final_receipt.read_id,
    )
    final_authority = runtime.prepare_captured_first_dip_tape_authority(
        attestation=final_proof,
        policy=policy,
        purpose=decision.FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION,
    )
    return CapturedFirstDipRuntimeFixture(
        runtime=runtime,
        request=rebound_request,
        policy=policy,
        detector_request=detector_request,
        detector_proof=detector_proof,
        detector_authority=detector_authority,
        detector_resolution=detector_resolution,
        prior_detector_reference_sha256=prior_sha256,
        final_proof=final_proof,
        final_authority=final_authority,
    )
