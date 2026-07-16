from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import uuid
from zoneinfo import ZoneInfo

import pytest

from app.services.trading.momentum_neural.adaptive_risk_policy import (
    AdaptiveRiskInputs,
    AdaptiveRiskPolicy,
    RiskInputEvidence,
    resolve_adaptive_risk,
)
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    AdaptiveRiskOpportunityKey,
    AdaptiveRiskReservationRequest,
    ImmutableAccountRiskSnapshot,
    load_adaptive_risk_reservation_request,
)
from app.services.trading.momentum_neural.adaptive_risk_runtime_contract import (
    build_adaptive_risk_reservation_claim,
)
from app.services.trading.momentum_neural.first_dip_tape_policy import (
    FirstDipTapePolicy,
    FirstDipTapeReadQuery,
    evaluate_first_dip_tape,
    first_dip_tape_window_from_capture,
)
from app.services.trading.momentum_neural import (
    first_dip_tape_decision as first_dip_decision,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CAPTURE_PRODUCER_LIFECYCLE_SCHEMA_VERSION,
    ActiveCaptureInputPrefixAttestation,
    ActiveCapturePrefixAttestation,
    CaptureAdaptiveOrderArtifacts,
    CaptureBrokerOrderLifecycle,
    CaptureBrokerTransition,
    CaptureClocks,
    CaptureContractError,
    CaptureCoverageManifest,
    CaptureDecisionAction,
    CaptureDecisionOutput,
    CaptureEvent,
    CaptureEventRef,
    CaptureOrderIntent,
    CaptureOrderIntentRole,
    CaptureProviderRegistrationEvidence,
    CaptureProviderRegistrationRecord,
    CaptureProducerLifecycleFact,
    CaptureReadReceipt,
    CaptureProducerSpec,
    CaptureRunIdentity,
    CaptureRunOpen,
    CaptureStream,
    FSMDependencyProfile,
    FSMStreamDependency,
    IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
    ProviderWatermark,
    StreamCoverage,
    VerifiedReplayCapture,
    capture_prefix_root_sha256,
    captured_read_result_sha256,
    grade_capture_decision_order_path,
    grade_capture_producer_lifecycle,
    sha256_json,
    verify_active_capture_input_attestation,
    verify_active_capture_prefix_attestation,
)
from app.services.trading.momentum_neural.replay_capture_runtime import (
    BoundedCaptureIngress,
    BoundedPreTriggerRing,
    CaptureBudgetPolicy,
    CaptureProducerLifecycleRuntime,
    CaptureResourceBinding,
    CaptureResourceMeasurement,
    CaptureWriterWorker,
    ContentAddressedCaptureStore,
    FirstDipTapeCoverageUnavailable,
    SharedCaptureAdmissionBudget,
    SharedCaptureStoreRuntime,
)


UTC = timezone.utc
BASE = datetime(2026, 7, 13, 12, 50, tzinfo=UTC)


def _abstain_decision_output(
    decision_id: str,
    symbol: str,
) -> CaptureDecisionOutput:
    return CaptureDecisionOutput(
        decision_id=decision_id,
        symbol=symbol,
        action=CaptureDecisionAction.ABSTAIN,
        fsm_state="fixture_evaluation",
        setup_role="fixture",
        order_intents=(),
        reason_code="fixture_no_order",
    )


class _ManualClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def set(self, value: datetime) -> datetime:
        self.value = value
        return value


def _identity() -> CaptureRunIdentity:
    return CaptureRunIdentity(
        run_id=str(uuid.UUID("00000000-0000-0000-0000-000000000513")),
        generation=3,
        code_build_sha256="a" * 64,
        config_sha256="b" * 64,
        feature_flags_sha256="c" * 64,
        account_identity_sha256="d" * 64,
        broker="alpaca",
        broker_environment="paper",
    )


def _resource_binding() -> CaptureResourceBinding:
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
        host_fingerprint_sha256="e" * 64,
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


def _producer(
    identity: CaptureRunIdentity,
    binding: CaptureResourceBinding,
    *,
    suffix: int = 1,
    streams: tuple[CaptureStream, ...] = (CaptureStream.NBBO_QUOTE,),
) -> CaptureProducerSpec:
    return CaptureProducerSpec(
        producer_id=f"iqfeed_nbbo_{suffix}",
        instance_id=str(uuid.UUID(int=500 + suffix)),
        generation=identity.generation,
        streams=streams,
        code_build_sha256=identity.code_build_sha256,
        config_sha256=identity.config_sha256,
        feature_flags_sha256=identity.feature_flags_sha256,
        resource_binding_sha256=binding.binding_sha256,
    )


def _coverage(identity: CaptureRunIdentity, *, watermark: bool = True) -> StreamCoverage:
    provider_watermark = (
        ProviderWatermark(
            stream=CaptureStream.NBBO_QUOTE,
            provider="iqfeed",
            identity_sha256=identity.identity_sha256,
            event_watermark_at=BASE + timedelta(milliseconds=2),
            emitted_available_at=BASE + timedelta(milliseconds=3),
            bounded_lateness_seconds=1,
            max_observed_lateness_seconds=0.01,
            generation=identity.generation,
            symbol="VEEE",
        )
        if watermark
        else None
    )
    return StreamCoverage(
        stream=CaptureStream.NBBO_QUOTE,
        identity_sha256=identity.identity_sha256,
        provider="iqfeed",
        symbol="VEEE",
        first_available_at=BASE + timedelta(milliseconds=2),
        last_available_at=BASE + timedelta(milliseconds=2),
        event_count=1,
        exact_event_clock_complete=True,
        content_verified=True,
        continuity_complete=True,
        watermark=provider_watermark,
    )


def _dependency_profile(
    *,
    stream: CaptureStream,
    read_id: str,
    coverage_start_at: datetime,
    max_source_age_seconds: float = 1.0,
) -> FSMDependencyProfile:
    policy_exact = stream in {
        CaptureStream.IQFEED_PRINT,
        CaptureStream.PROVIDER_TRADE_PRINT,
        CaptureStream.NBBO_QUOTE,
        CaptureStream.L2_DEPTH_DELTA,
        CaptureStream.L2_DEPTH_CHECKPOINT,
    }
    return FSMDependencyProfile(
        required_streams=frozenset({stream}),
        required_read_ids=(read_id,),
        stream_dependencies=(
            FSMStreamDependency(
                stream=stream,
                exact_provider_event_at_required=policy_exact,
                market_reference_at_required=False,
                max_source_age_seconds=max_source_age_seconds,
                coverage_start_at=coverage_start_at,
            ),
        ),
    )


def _live_continuity(
    identity: CaptureRunIdentity,
    source: CaptureEvent,
) -> StreamCoverage:
    assert source.clocks.provider_event_at is not None
    return StreamCoverage(
        stream=source.stream,
        identity_sha256=identity.identity_sha256,
        provider=source.provider,
        symbol=source.symbol,
        first_available_at=source.clocks.available_at,
        last_available_at=source.clocks.available_at,
        event_count=1,
        exact_event_clock_complete=True,
        content_verified=True,
        continuity_complete=True,
        watermark=ProviderWatermark(
            stream=source.stream,
            provider=source.provider,
            identity_sha256=identity.identity_sha256,
            event_watermark_at=source.clocks.provider_event_at,
            emitted_available_at=source.clocks.available_at,
            bounded_lateness_seconds=1.0,
            max_observed_lateness_seconds=0.0,
            generation=identity.generation,
            symbol=source.symbol,
        ),
    )


def _valid_adaptive_entry_for_predecision(
    *,
    identity: CaptureRunIdentity,
    predecision: ActiveCaptureInputPrefixAttestation,
    decision_id: str,
    symbol: str,
    as_of: datetime,
    setup_family: str = "generic_breakout",
) -> tuple[CaptureOrderIntent, CaptureAdaptiveOrderArtifacts]:
    """Build fully recomputable artifacts so capability is the only rejection."""

    policy = AdaptiveRiskPolicy(
        policy_version="iqfeed-v1-capability-fixture-v1",
        policy_source="recorded_fixture",
        risk_fraction_of_equity=0.01,
        daily_risk_fraction_of_equity=0.10,
        portfolio_risk_fraction_of_equity=0.05,
        cluster_risk_fraction_of_equity=0.04,
        symbol_risk_fraction_of_equity=0.03,
        daily_gap_reserve_fraction_of_equity=0.001,
        max_notional_fraction_of_equity=0.80,
        max_buying_power_fraction_for_notional=0.50,
        max_portfolio_gross_fraction_of_equity=2.0,
        quality_multiplier_floor=0.50,
        quality_multiplier_ceiling=1.50,
        volatility_reference_fraction=0.05,
        volatility_multiplier_floor=0.40,
        spread_reserve_multiple=1.0,
        per_share_gap_reserve_volatility_multiple=0.10,
        max_adv_participation=0.02,
        max_recent_volume_participation=0.10,
        max_executable_depth_participation=0.50,
        market_data_max_age_seconds=2.0,
        account_data_max_age_seconds=10.0,
        reservation_data_max_age_seconds=0.25,
        context_data_max_age_seconds=60.0,
    )
    account_snapshot = ImmutableAccountRiskSnapshot(
        snapshot_id="iqfeed-v1-capability-account-fixture",
        source="recorded_fixture",
        provider_generation="fixture-generation-3",
        account_scope="alpaca:paper:paper-fixture",
        execution_family="alpaca_spot",
        broker_environment="paper",
        venue="alpaca",
        account_identity_sha256=identity.account_identity_sha256,
        observed_at=as_of - timedelta(microseconds=200),
        available_at=as_of - timedelta(microseconds=100),
        equity_usd=100_000.0,
        buying_power_usd=400_000.0,
        broker_day_change_usd=0.0,
        local_realized_pnl_usd=0.0,
        pending_policy_buying_power_reflected_usd=0.0,
    )

    def evidence(
        content_sha256: str,
        *,
        source: str = "recorded_fixture",
        provider_generation: str = "fixture-generation-3",
        observed_at: datetime | None = None,
        available_at: datetime | None = None,
    ) -> RiskInputEvidence:
        return RiskInputEvidence(
            source=source,
            provider_generation=provider_generation,
            observed_at=observed_at or as_of - timedelta(microseconds=200),
            available_at=available_at or as_of - timedelta(microseconds=100),
            content_sha256=content_sha256,
        )

    account_evidence = evidence(
        account_snapshot.snapshot_sha256,
        observed_at=account_snapshot.observed_at,
        available_at=account_snapshot.available_at,
    )
    reservation_ledger_sha256 = sha256_json(
        {"fixture": "empty_atomic_reservation_ledger"}
    )
    input_evidence = {
        "account": account_evidence,
        "daily_pnl": account_evidence,
        "bbo": evidence(sha256_json({"bid": 5.00, "ask": 5.01})),
        "structural_stop": evidence(sha256_json({"stop": 4.80})),
        "setup_quality": evidence(sha256_json({"quality": 0.80})),
        "volatility": evidence(sha256_json({"fraction": 0.05})),
        "liquidity": evidence(sha256_json({"adv": 5_000_000})),
        "portfolio_heat": evidence(sha256_json({"open_risk": 0.0})),
        "correlation": evidence(sha256_json({"cluster": "equity:v"})),
        "candidate_buying_power_estimate": evidence(
            sha256_json({"per_share": 5.01})
        ),
        "reservation_ledger": evidence(reservation_ledger_sha256),
        "code_build": evidence(identity.code_build_sha256),
        "effective_config": evidence(identity.config_sha256),
        "feature_flags": evidence(identity.feature_flags_sha256),
        "capture_prefix": evidence(
            predecision.input_prefix_root_sha256,
            source="capture_hash_chain",
        ),
    }
    inputs = AdaptiveRiskInputs(
        decision_id=decision_id,
        replay_or_paper_run_id=identity.run_id,
        generation=identity.generation,
        execution_surface="alpaca_paper",
        execution_family="alpaca_spot",
        venue="alpaca",
        broker_environment=identity.broker_environment,
        symbol=symbol,
        side="long",
        as_of=as_of,
        account_identity_sha256=identity.account_identity_sha256,
        code_build_sha256=identity.code_build_sha256,
        effective_config_sha256=identity.config_sha256,
        feature_flags_sha256=identity.feature_flags_sha256,
        capture_prefix_root_sha256=predecision.input_prefix_root_sha256,
        equity_usd=100_000.0,
        buying_power_usd=400_000.0,
        broker_day_change_usd=0.0,
        local_realized_pnl_usd=0.0,
        open_structural_risk_usd=0.0,
        pending_reserved_risk_usd=0.0,
        existing_same_symbol_structural_risk_usd=0.0,
        pending_same_symbol_structural_risk_usd=0.0,
        current_cluster_structural_risk_usd=0.0,
        pending_correlation_cluster_risk_usd=0.0,
        portfolio_gross_notional_usd=0.0,
        pending_portfolio_gross_notional_usd=0.0,
        policy_buying_power_capacity_usd=400_000.0,
        open_buying_power_impact_usd=0.0,
        pending_buying_power_impact_usd=0.0,
        candidate_buying_power_impact_per_share_usd=5.01,
        bid=5.00,
        ask=5.01,
        structural_stop=4.80,
        entry_slippage_bps=10.0,
        exit_slippage_bps=20.0,
        fees_per_share_usd=0.005,
        setup_quality=0.80,
        realized_volatility_fraction=0.05,
        average_daily_volume_shares=5_000_000.0,
        recent_volume_shares=500_000.0,
        executable_depth_shares=100_000.0,
        correlation_cluster_id="equity:v",
        evidence=input_evidence,
    )
    resolution = resolve_adaptive_risk(policy, inputs)
    assert resolution.valid
    client_order_id = "chili-paper-veee-generic-v1-bypass-attempt"
    request = AdaptiveRiskReservationRequest(
        policy=policy,
        inputs=inputs,
        account_snapshot=account_snapshot,
        account_scope=account_snapshot.account_scope,
        setup_family=setup_family,
        correlation_cluster=inputs.correlation_cluster_id,
        client_order_id=client_order_id,
        entry_limit_price=float(resolution.effective_entry_price),
        opportunity_key=(
            AdaptiveRiskOpportunityKey(
                account_scope=account_snapshot.account_scope,
                symbol=symbol,
                trading_date=as_of.astimezone(
                    ZoneInfo("America/New_York")
                ).date(),
                setup_family=setup_family,
            )
            if setup_family == "first_dip_reclaim"
            else None
        ),
    )
    claim = build_adaptive_risk_reservation_claim(
        resolution.to_decision_packet(),
        claim_id=client_order_id,
    )
    intent = CaptureOrderIntent(
        intent_id=str(uuid.UUID(int=9130)),
        client_order_id=client_order_id,
        client_order_id_sha256=sha256_json(
            {"client_order_id": client_order_id}
        ),
        symbol=symbol,
        side="buy",
        order_type="limit",
        quantity=int(resolution.quantity_shares),
        time_in_force="day",
        extended_hours=False,
        intent_role=CaptureOrderIntentRole.ENTRY,
        risk_increasing=True,
        decision_provenance_sha256=predecision.attestation_sha256,
        adaptive_request_sha256=request.request_sha256,
        adaptive_decision_sha256=resolution.decision_packet_sha256,
        adaptive_resolution_sha256=resolution.economic_resolution_sha256,
        reservation_claim_sha256=claim.claim_sha256,
        limit_price=float(resolution.effective_entry_price),
    )
    artifact = CaptureAdaptiveOrderArtifacts(
        order_intent_sha256=intent.order_intent_sha256,
        reservation_request=request.to_payload(),
        decision_packet=resolution.to_decision_packet(),
        reservation_claim=claim.to_payload(),
    )
    return intent, artifact


def _runtime_fixture(
    *,
    heartbeat_timeout_seconds: float = 1,
) -> tuple[
    CaptureProducerLifecycleRuntime,
    BoundedCaptureIngress,
    CaptureRunIdentity,
    CaptureResourceBinding,
    CaptureProducerSpec,
    _ManualClock,
]:
    identity = _identity()
    binding = _resource_binding()
    producer = _producer(identity, binding)
    ingress = BoundedCaptureIngress.from_resource_binding(binding)
    clock = _ManualClock(BASE)
    runtime = CaptureProducerLifecycleRuntime(
        identity=identity,
        ingress=ingress,
        resource_binding=binding,
        producers=(producer,),
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        wall_clock=clock,
    )
    return runtime, ingress, identity, binding, producer, clock


def _stage1_fixture(
    *,
    decision_id: str,
    heartbeat_timeout_seconds: float = 1.0,
) -> tuple[
    CaptureProducerLifecycleRuntime,
    CaptureRunIdentity,
    CaptureResourceBinding,
    CaptureProducerSpec,
    _ManualClock,
    CaptureEvent,
    str,
    FSMDependencyProfile,
]:
    identity = _identity()
    binding = _resource_binding()
    producer = _producer(
        identity,
        binding,
        streams=(CaptureStream.NBBO_QUOTE, CaptureStream.FSM_DECISION),
    )
    ingress = BoundedCaptureIngress.from_resource_binding(binding)
    clock = _ManualClock(BASE)
    runtime = CaptureProducerLifecycleRuntime(
        identity=identity,
        ingress=ingress,
        resource_binding=binding,
        producers=(producer,),
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        wall_clock=clock,
    )
    runtime.open(opened_at=BASE)
    runtime.register(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=1)),
    )
    source_at = clock.set(BASE + timedelta(milliseconds=2))
    source = runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.NBBO_QUOTE,
        provider="iqfeed",
        symbol="VEEE",
        clocks=CaptureClocks(
            provider_event_at=source_at,
            received_at=source_at,
            available_at=source_at,
        ),
        payload={"bid": 4.99, "ask": 5.00},
        recorded_at=source_at,
    )
    read_id = str(uuid.uuid4())
    receipt = CaptureReadReceipt(
        read_id=read_id,
        decision_id=decision_id,
        identity_sha256=identity.identity_sha256,
        stream=CaptureStream.NBBO_QUOTE,
        provider="iqfeed",
        symbol="VEEE",
        requested_at=BASE + timedelta(milliseconds=1, microseconds=500),
        returned_at=source_at,
        query_sha256=sha256_json({"surface": "latest_nbbo"}),
        source_event_sha256s=(source.event_sha256,),
        empty_result=False,
        result_sha256=captured_read_result_sha256(
            (CaptureEventRef.from_event(source),)
        ),
    )
    runtime.submit_read_receipt(receipt)
    runtime.submit_live_continuity_checkpoint(
        producer.producer_id,
        _live_continuity(identity, source),
        recorded_at=source_at,
    )
    profile = _dependency_profile(
        stream=CaptureStream.NBBO_QUOTE,
        read_id=read_id,
        coverage_start_at=source_at,
        max_source_age_seconds=10.0,
    )
    return runtime, identity, binding, producer, clock, source, read_id, profile


def _change_log_stage1_fixture(
    *,
    decision_id: str,
    market_reference_at: datetime,
    available_at: datetime,
    max_source_age_seconds: float,
) -> tuple[
    CaptureProducerLifecycleRuntime,
    CaptureProducerSpec,
    _ManualClock,
    FSMDependencyProfile,
    StreamCoverage,
]:
    identity = _identity()
    binding = _resource_binding()
    producer = _producer(
        identity,
        binding,
        streams=(CaptureStream.SSR_STATE,),
    )
    ingress = BoundedCaptureIngress.from_resource_binding(binding)
    clock = _ManualClock(BASE)
    runtime = CaptureProducerLifecycleRuntime(
        identity=identity,
        ingress=ingress,
        resource_binding=binding,
        producers=(producer,),
        heartbeat_timeout_seconds=2.0,
        wall_clock=clock,
    )
    runtime.open(opened_at=BASE)
    runtime.register(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=1)),
    )
    source = runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.SSR_STATE,
        provider="alpaca",
        symbol="VEEE",
        clocks=CaptureClocks(
            market_reference_at=market_reference_at,
            received_at=available_at,
            available_at=available_at,
        ),
        payload={"ssr": False},
        recorded_at=clock.set(available_at),
    )
    read_id = str(uuid.uuid4())
    receipt = CaptureReadReceipt(
        read_id=read_id,
        decision_id=decision_id,
        identity_sha256=identity.identity_sha256,
        stream=CaptureStream.SSR_STATE,
        provider="alpaca",
        symbol="VEEE",
        requested_at=BASE + timedelta(milliseconds=1),
        returned_at=available_at,
        query_sha256=sha256_json({"surface": "current_ssr_state"}),
        source_event_sha256s=(source.event_sha256,),
        empty_result=False,
        result_sha256=captured_read_result_sha256(
            (CaptureEventRef.from_event(source),)
        ),
    )
    runtime.submit_read_receipt(receipt)
    profile = FSMDependencyProfile(
        required_streams=frozenset({CaptureStream.SSR_STATE}),
        required_read_ids=(read_id,),
        stream_dependencies=(
            FSMStreamDependency(
                stream=CaptureStream.SSR_STATE,
                exact_provider_event_at_required=False,
                market_reference_at_required=True,
                max_source_age_seconds=max_source_age_seconds,
                coverage_start_at=available_at,
            ),
        ),
    )
    checkpoint_at = available_at + timedelta(milliseconds=1)
    coverage = StreamCoverage(
        stream=CaptureStream.SSR_STATE,
        identity_sha256=identity.identity_sha256,
        provider="alpaca",
        symbol="VEEE",
        first_available_at=available_at,
        last_available_at=available_at,
        event_count=1,
        exact_event_clock_complete=True,
        content_verified=True,
        continuity_complete=True,
        watermark=ProviderWatermark(
            stream=CaptureStream.SSR_STATE,
            provider="alpaca",
            identity_sha256=identity.identity_sha256,
            event_watermark_at=checkpoint_at,
            emitted_available_at=checkpoint_at,
            bounded_lateness_seconds=2.0,
            max_observed_lateness_seconds=0.0,
            generation=identity.generation,
            symbol="VEEE",
        ),
    )
    return runtime, producer, clock, profile, coverage


def _submit_abstain_with_stage1(
    runtime: CaptureProducerLifecycleRuntime,
    producer: CaptureProducerSpec,
    clock: _ManualClock,
    *,
    decision_id: str,
    read_id: str,
    profile: FSMDependencyProfile,
    predecision: ActiveCaptureInputPrefixAttestation,
    decided_at: datetime,
) -> CaptureEvent:
    output = _abstain_decision_output(decision_id, "VEEE")
    at = clock.set(decided_at)
    return runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.FSM_DECISION,
        provider="chili_fsm",
        symbol="VEEE",
        clocks=CaptureClocks(
            market_reference_at=at,
            received_at=at,
            available_at=at,
        ),
        payload={
            "decision_id": decision_id,
            "symbol": "VEEE",
            "decision_at": at.isoformat().replace("+00:00", "Z"),
            "input_prefix_sequence": predecision.input_prefix_sequence,
            "input_prefix_root_sha256": predecision.input_prefix_root_sha256,
            "required_read_ids": [read_id],
            "fsm_dependency_profile": profile.to_dict(),
            "predecision_attestation_sha256": predecision.attestation_sha256,
            "predecision_read_evidence_inventory_sha256": (
                predecision.read_evidence_inventory_sha256
            ),
            "predecision_continuity_evidence_inventory_sha256": (
                predecision.continuity_evidence_inventory_sha256
            ),
            "predecision_admission_handoff_sha256": (
                predecision.admission_handoff_sha256
            ),
            "decision_output": output.to_dict(),
            "decision_output_sha256": output.decision_output_sha256,
        },
        recorded_at=at,
        predecision_attestation=predecision,
    )


def _complete_runtime(
    *, include_gap: bool = False
) -> tuple[
    CaptureProducerLifecycleRuntime,
    BoundedCaptureIngress,
    CaptureRunIdentity,
    CaptureResourceBinding,
    StreamCoverage,
    tuple,
]:
    runtime, ingress, identity, binding, producer, clock = _runtime_fixture()
    runtime.open(opened_at=BASE)
    source_payload = {"bid": 4.99, "ask": 5.00}
    source_clocks = CaptureClocks(
        provider_event_at=BASE + timedelta(milliseconds=1),
        received_at=BASE + timedelta(milliseconds=2),
        available_at=BASE + timedelta(milliseconds=2),
    )
    fixture_provider = "sealed_fixture_feed"
    registration_evidence = CaptureProviderRegistrationEvidence(
        producer_id=producer.producer_id,
        provider=fixture_provider,
        provider_instance_id=producer.instance_id,
        provider_generation=producer.generation,
        evidence_kind="first_provider_frame",
        source_payload_sha256=sha256_json(source_payload),
        provider_event_at=source_clocks.provider_event_at,
        received_at=source_clocks.received_at,
        provider_sequence=1,
    )
    registration_record = runtime.record_provider_registration_evidence(
        producer.producer_id,
        evidence=registration_evidence,
        stream=CaptureStream.NBBO_QUOTE,
        provider=fixture_provider,
        payload=source_payload,
        clocks=source_clocks,
        symbol="VEEE",
        recorded_at=clock.set(BASE + timedelta(milliseconds=2)),
    )
    runtime.register_from_provider_evidence(
        producer.producer_id,
        registration_record,
        recorded_at=clock.set(BASE + timedelta(milliseconds=2)),
    )
    runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.NBBO_QUOTE,
        provider=fixture_provider,
        symbol="VEEE",
        clocks=source_clocks,
        payload=source_payload,
        recorded_at=clock.set(BASE + timedelta(milliseconds=2)),
    )
    if include_gap:
        runtime.report_gap(
            producer.producer_id,
            stream=CaptureStream.NBBO_QUOTE,
            reason="provider_sequence_gap",
            first_available_at=BASE + timedelta(milliseconds=2),
            last_available_at=BASE + timedelta(milliseconds=2),
            lost_count=1,
            recorded_at=clock.set(
                BASE + timedelta(milliseconds=2, microseconds=500)
            ),
            symbol="VEEE",
        )
    coverage = _coverage(identity)
    assert coverage.watermark is not None
    coverage = replace(
        coverage,
        provider=fixture_provider,
        watermark=replace(coverage.watermark, provider=fixture_provider),
    )
    evidence = runtime.submit_stream_coverage(
        producer.producer_id,
        coverage,
        recorded_at=clock.set(BASE + timedelta(milliseconds=4)),
    )
    runtime.quiesce(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=5)),
    )
    runtime.close_producer(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=6)),
    )
    runtime.close_run(recorded_at=clock.set(BASE + timedelta(milliseconds=7)))
    return runtime, ingress, identity, binding, coverage, evidence


def _pure_grade(*, include_gap: bool = False):
    runtime, ingress, identity, binding, coverage, evidence = _complete_runtime(
        include_gap=include_gap
    )
    batch = ingress.pop_batch(
        max_events=100,
        max_bytes=binding.budget.async_queue_bytes,
        timeout_seconds=0,
    )
    health_event = next(
        event
        for event in evidence
        if event.stream is CaptureStream.CAPTURE_HEALTH
    )
    watermark_event = next(
        event
        for event in evidence
        if event.stream is CaptureStream.PROVIDER_WATERMARK
    )
    grade = grade_capture_producer_lifecycle(
        identity=identity,
        events=batch.events,
        stream_coverage={CaptureStream.NBBO_QUOTE: coverage},
        coverage_health_events={CaptureStream.NBBO_QUOTE: health_event},
        watermark_events={CaptureStream.NBBO_QUOTE: watermark_event},
        resource_binding_sha256=binding.binding_sha256,
    )
    return grade, batch.events, identity, binding, coverage, health_event, watermark_event


def test_exact_producer_lifecycle_certifies_after_quiescent_close() -> None:
    grade, *_rest = _pure_grade()

    assert grade.certified is True
    assert grade.reasons == ()
    assert grade.producer_roster_sha256 is not None
    assert grade.run_close_event_sha256 is not None


def test_provider_registration_record_cannot_self_attest_a_forged_first_frame() -> None:
    _grade, events, identity, binding, coverage, health, watermark = _pure_grade()
    record_event = next(
        event
        for event in events
        if event.payload.get("kind") == "PROVIDER_REGISTRATION_EVIDENCE"
    )
    record = CaptureProviderRegistrationRecord.from_dict(record_event.payload)
    forged_record = replace(
        record,
        evidence=replace(record.evidence, source_payload_sha256="f" * 64),
    )
    forged_record_event = replace(record_event, payload=forged_record.to_dict())
    registration_event = next(
        event for event in events if event.payload.get("kind") == "PRODUCER_REGISTERED"
    )
    registration = CaptureProducerLifecycleFact.from_dict(registration_event.payload)
    forged_registration_event = replace(
        registration_event,
        payload=replace(
            registration,
            evidence_event_sha256s=(forged_record_event.event_sha256,),
        ).to_dict(),
    )
    mutated = tuple(
        forged_record_event
        if event is record_event
        else forged_registration_event
        if event is registration_event
        else event
        for event in events
    )

    grade = grade_capture_producer_lifecycle(
        identity=identity,
        events=mutated,
        stream_coverage={CaptureStream.NBBO_QUOTE: coverage},
        coverage_health_events={CaptureStream.NBBO_QUOTE: health},
        watermark_events={CaptureStream.NBBO_QUOTE: watermark},
        resource_binding_sha256=binding.binding_sha256,
    )

    assert grade.certified is False
    assert (
        "capture_provider_registration_first_event_mismatch:iqfeed_nbbo_1"
        in grade.reasons
    )


def test_missing_close_and_open_producer_are_explicit_blockers() -> None:
    _grade, events, identity, binding, coverage, health, watermark = _pure_grade()
    open_events = tuple(
        event
        for event in events
        if event.payload.get("kind")
        not in {"PRODUCER_CLOSED", "RUN_CLOSED"}
    )

    grade = grade_capture_producer_lifecycle(
        identity=identity,
        events=open_events,
        stream_coverage={CaptureStream.NBBO_QUOTE: coverage},
        coverage_health_events={CaptureStream.NBBO_QUOTE: health},
        watermark_events={CaptureStream.NBBO_QUOTE: watermark},
        resource_binding_sha256=binding.binding_sha256,
    )

    assert grade.certified is False
    assert "capture_producer_open:iqfeed_nbbo_1" in grade.reasons
    assert "capture_run_close_missing" in grade.reasons


def test_duplicate_registration_and_late_fact_are_explicit_blockers() -> None:
    _grade, events, identity, binding, coverage, health, watermark = _pure_grade()
    registration = next(
        event for event in events if event.payload.get("kind") == "PRODUCER_REGISTERED"
    )
    duplicate = replace(
        registration,
        sequence=max(event.sequence for event in events) + 1,
        clocks=CaptureClocks(
            received_at=BASE + timedelta(milliseconds=8),
            available_at=BASE + timedelta(milliseconds=8),
        ),
    )
    close = next(
        event for event in events if event.payload.get("kind") == "PRODUCER_CLOSED"
    )
    backdated_close = replace(
        close,
        payload={
            **dict(close.payload),
            "recorded_at": (BASE + timedelta(milliseconds=5)).isoformat().replace(
                "+00:00", "Z"
            ),
        },
    )
    mutated = tuple(
        backdated_close if event is close else event for event in events
    ) + (duplicate,)

    grade = grade_capture_producer_lifecycle(
        identity=identity,
        events=mutated,
        stream_coverage={CaptureStream.NBBO_QUOTE: coverage},
        coverage_health_events={CaptureStream.NBBO_QUOTE: health},
        watermark_events={CaptureStream.NBBO_QUOTE: watermark},
        resource_binding_sha256=binding.binding_sha256,
    )

    assert "capture_producer_registration_duplicate:iqfeed_nbbo_1" in grade.reasons
    assert "capture_producer_lifecycle_backdated_or_late" in grade.reasons


def test_conflicting_roster_and_missing_watermark_are_explicit_blockers() -> None:
    _grade, events, identity, binding, coverage, health, _watermark = _pure_grade()
    run_open_event = events[0]
    run_open = CaptureRunOpen.from_dict(run_open_event.payload)
    conflicting = CaptureRunOpen(
        identity_sha256=run_open.identity_sha256,
        run_id=run_open.run_id,
        generation=run_open.generation,
        opened_at=run_open.opened_at,
        heartbeat_timeout_seconds=run_open.heartbeat_timeout_seconds,
        resource_binding_sha256=run_open.resource_binding_sha256,
        producers=(*run_open.producers, _producer(identity, binding, suffix=2)),
    )
    mutated = (replace(run_open_event, payload=conflicting.to_dict()), *events[1:])

    grade = grade_capture_producer_lifecycle(
        identity=identity,
        events=mutated,
        stream_coverage={CaptureStream.NBBO_QUOTE: coverage},
        coverage_health_events={CaptureStream.NBBO_QUOTE: health},
        watermark_events={},
        resource_binding_sha256=binding.binding_sha256,
    )

    assert "capture_producer_stream_conflict:nbbo_quote" in grade.reasons
    assert (
        "capture_producer_watermark_evidence_missing:iqfeed_nbbo_1:nbbo_quote"
        in grade.reasons
    )


def test_producer_gap_is_append_only_and_blocks_certification() -> None:
    grade, events, *_rest = _pure_grade(include_gap=True)

    assert any(
        event.payload.get("schema_version")
        == CAPTURE_PRODUCER_LIFECYCLE_SCHEMA_VERSION
        and event.payload.get("kind") == "PRODUCER_GAP"
        for event in events
    )
    assert (
        "capture_producer_gap:iqfeed_nbbo_1:provider_sequence_gap" in grade.reasons
    )


def test_runtime_preserves_missing_watermark_then_refuses_stale_heartbeat_and_open_run() -> None:
    runtime, _ingress, identity, _binding, producer, clock = _runtime_fixture(
        heartbeat_timeout_seconds=0.01
    )
    runtime.open(opened_at=BASE)
    runtime.register(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=1)),
    )

    source_at = clock.set(BASE + timedelta(milliseconds=2))
    runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.NBBO_QUOTE,
        provider="iqfeed",
        symbol="VEEE",
        clocks=CaptureClocks(
            provider_event_at=source_at,
            received_at=source_at,
            available_at=source_at,
        ),
        payload={"bid": 4.99, "ask": 5.00},
        recorded_at=source_at,
    )
    evidence = runtime.submit_stream_coverage(
        producer.producer_id,
        _coverage(identity, watermark=False),
        recorded_at=clock.set(BASE + timedelta(milliseconds=4)),
    )
    assert len(evidence) == 1
    assert evidence[0].stream is CaptureStream.CAPTURE_HEALTH
    with pytest.raises(CaptureContractError, match="heartbeat deadline"):
        runtime.heartbeat(
            producer.producer_id,
            recorded_at=clock.set(BASE + timedelta(seconds=1)),
        )
    with pytest.raises(CaptureContractError, match="open producers"):
        runtime.close_run(recorded_at=clock.set(BASE + timedelta(seconds=1)))
    with pytest.raises(CaptureContractError, match="RUN_CLOSED"):
        runtime.seal_run(object())


def test_runtime_clock_rejects_a_backdated_lifecycle_assertion() -> None:
    runtime, _ingress, _identity_row, _binding, _producer_row, _clock = (
        _runtime_fixture()
    )

    with pytest.raises(CaptureContractError, match="differs.*runtime wall clock"):
        runtime.open(opened_at=BASE - timedelta(seconds=1))


def test_runtime_certifying_seal_round_trips_into_manifest(tmp_path) -> None:
    runtime, ingress, identity, binding, coverage, _evidence = _complete_runtime()
    store = ContentAddressedCaptureStore(
        tmp_path / "producer-lifecycle",
        compression_codec="zlib",
        resource_binding=binding,
    )
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=100,
        batch_bytes=binding.budget.async_queue_bytes,
        poll_seconds=0.001,
        flush_interval_seconds=0.01,
    )
    worker.start()
    assert worker.stop(timeout_seconds=5)
    seal = runtime.seal_run(worker)
    verified = VerifiedReplayCapture.load_sealed_run(
        store,
        identity,
        expected_final_seal_sha256=seal.seal_sha256,
    )

    manifest = CaptureCoverageManifest.from_verified_capture(
        verified,
        decision_checkpoints=(),
        stream_coverage={CaptureStream.NBBO_QUOTE: coverage},
        read_receipts=(),
    )

    assert manifest.certification_blockers == ()
    assert manifest.required_streams_full_fidelity is True
    assert manifest.seal_binding is not None
    assert manifest.seal_binding.derived_certification_blockers == ()


def test_gap_run_seals_cryptographically_but_grades_coverage_unavailable(
    tmp_path,
) -> None:
    runtime, ingress, identity, binding, coverage, _evidence = _complete_runtime(
        include_gap=True
    )
    assert runtime.health()["cryptographic_seal_eligible"] is True
    assert runtime.health()["certifying_seal_eligible"] is False
    store = ContentAddressedCaptureStore(
        tmp_path / "honest-gap-lifecycle",
        compression_codec="zlib",
        resource_binding=binding,
    )
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=100,
        batch_bytes=binding.budget.async_queue_bytes,
        poll_seconds=0.001,
        flush_interval_seconds=0.01,
    )
    worker.start()
    assert worker.stop(timeout_seconds=5)
    seal = runtime.seal_run(worker)
    verified = VerifiedReplayCapture.load_sealed_run(
        store,
        identity,
        expected_final_seal_sha256=seal.seal_sha256,
    )
    manifest = CaptureCoverageManifest.from_verified_capture(
        verified,
        decision_checkpoints=(),
        stream_coverage={CaptureStream.NBBO_QUOTE: coverage},
        read_receipts=(),
    )
    assert manifest.seal_binding is not None
    assert manifest.certification_blockers
    assert any("gap" in reason for reason in manifest.certification_blockers)


def test_runtime_certifying_seal_rejects_direct_ingress_bypass(tmp_path) -> None:
    runtime, ingress, identity, binding, _coverage_row, _evidence = _complete_runtime()
    bypass_at = BASE + timedelta(milliseconds=8)
    assert ingress.submit(
        CaptureEvent(
            identity=identity,
            sequence=runtime.health()["last_sequence"] + 1,
            stream=CaptureStream.NBBO_QUOTE,
            provider="iqfeed",
            symbol="VEEE",
            clocks=CaptureClocks(
                provider_event_at=bypass_at,
                received_at=bypass_at,
                available_at=bypass_at,
            ),
            payload={"direct_ingress_bypass": True},
        )
    )
    store = ContentAddressedCaptureStore(
        tmp_path / "bypassed-lifecycle",
        compression_codec="zlib",
        resource_binding=binding,
    )
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=100,
        batch_bytes=binding.budget.async_queue_bytes,
        poll_seconds=0.001,
        flush_interval_seconds=0.01,
    )
    worker.start()
    assert worker.stop(timeout_seconds=5)

    with pytest.raises(CaptureContractError, match="escaped.*lifecycle boundary"):
        runtime.seal_run(worker)


def test_missing_exact_clock_is_preserved_and_latches_incomplete_coverage() -> None:
    runtime, _ingress, _identity_row, _binding, producer, clock = _runtime_fixture()
    runtime.open(opened_at=BASE)
    runtime.register(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=1)),
    )
    at = clock.set(BASE + timedelta(milliseconds=2))

    missing_clock = runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.NBBO_QUOTE,
        provider="iqfeed",
        symbol="VEEE",
        clocks=CaptureClocks(received_at=at, available_at=at),
        payload={"bid": 4.98, "ask": 5.00},
        recorded_at=at,
    )
    assert missing_clock.clocks.provider_event_at is None

    accepted = runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.NBBO_QUOTE,
        provider="iqfeed",
        symbol="VEEE",
        clocks=CaptureClocks(
            provider_event_at=at,
            received_at=at,
            available_at=at,
        ),
        payload={"bid": 4.99, "ask": 5.00},
        recorded_at=at,
    )

    assert accepted.sequence == 4
    assert runtime.health()["last_sequence"] == 4
    assert runtime._stream_stats[CaptureStream.NBBO_QUOTE][
        "exact_event_clock_complete"
    ] is False
    assert runtime.health()["submission_failure"] is None


def test_ingress_rejection_is_latched_without_committing_sequence() -> None:
    runtime, ingress, _identity_row, _binding, producer, clock = _runtime_fixture()
    runtime.open(opened_at=BASE)
    runtime.register(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=1)),
    )
    ingress.close()
    at = clock.set(BASE + timedelta(milliseconds=2))

    with pytest.raises(CaptureContractError, match="ingress rejected"):
        runtime.submit_input(
            producer.producer_id,
            stream=CaptureStream.NBBO_QUOTE,
            provider="iqfeed",
            symbol="VEEE",
            clocks=CaptureClocks(
                provider_event_at=at,
                received_at=at,
                available_at=at,
            ),
            payload={"bid": 4.99, "ask": 5.00},
            recorded_at=at,
        )

    assert runtime.health()["last_sequence"] == 2
    assert runtime.health()["submission_failure"] == "ingress_rejected_sequence_3"


def test_trusted_clock_rollback_and_overdue_heartbeat_stay_latched() -> None:
    runtime, _ingress, _identity_row, _binding, producer, clock = _runtime_fixture(
        heartbeat_timeout_seconds=0.01
    )
    runtime.open(opened_at=BASE)
    runtime.register(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=1)),
    )

    with pytest.raises(CaptureContractError, match="heartbeat deadline"):
        runtime.heartbeat(
            producer.producer_id,
            recorded_at=clock.set(BASE + timedelta(seconds=1)),
        )
    assert "heartbeat_deadline_exceeded" in runtime.health()["submission_failure"]

    with pytest.raises(CaptureContractError, match="already noncertifiable"):
        runtime.heartbeat(
            producer.producer_id,
            recorded_at=clock.set(BASE + timedelta(milliseconds=2)),
        )
    assert "heartbeat_deadline_exceeded" in runtime.health()["submission_failure"]
    assert runtime.health()["certifying_seal_eligible"] is False

    rollback, _ingress, _identity, _binding, rollback_producer, rollback_clock = (
        _runtime_fixture()
    )
    rollback.open(opened_at=BASE)
    rollback.register(
        rollback_producer.producer_id,
        recorded_at=rollback_clock.set(BASE + timedelta(milliseconds=2)),
    )
    with pytest.raises(CaptureContractError, match="moved behind"):
        rollback.heartbeat(
            rollback_producer.producer_id,
            recorded_at=rollback_clock.set(BASE + timedelta(milliseconds=1)),
        )
    assert rollback.health()["submission_failure"] == "trusted_wall_clock_rollback"


def test_same_promotion_batch_uses_one_release_clock_and_sequence_tiebreak() -> None:
    runtime, _ingress, identity, binding, producer, clock = _runtime_fixture()
    runtime.open(opened_at=BASE)
    runtime.register(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=1)),
    )
    promotion_at = clock.set(BASE + timedelta(milliseconds=5))
    ring = BoundedPreTriggerRing.from_resource_binding(
        binding,
        horizon=timedelta(minutes=3),
        per_symbol_max_events=16,
    )
    provisional = []
    for offset in (2, 3):
        source_at = BASE + timedelta(milliseconds=offset)
        retained, source = ring.retain_observation(
            identity=identity,
            stream=CaptureStream.NBBO_QUOTE,
            provider="iqfeed",
            symbol="VEEE",
            clocks=CaptureClocks(
                provider_event_at=source_at,
                received_at=source_at,
                available_at=source_at,
            ),
            payload={"offset": offset},
        )
        assert retained is True
        provisional.append(source)
    transfer = ring.begin_promotion(
        "VEEE",
        promoted_at=promotion_at,
        source_identity=identity,
    )
    forged_handoff = "f" * 64
    forged_inventory = ring._promotion_inventory_sha256(
        promotion_id=transfer.promotion_id,
        source_identity_sha256=transfer.source_identity_sha256,
        resource_binding_sha256=transfer.resource_binding_sha256,
        symbol=transfer.symbol,
        promoted_at=transfer.promoted_at,
        events=transfer.events,
        gaps=transfer.gaps,
        admission_handoff_sha256=forged_handoff,
    )
    with pytest.raises(CaptureContractError, match="capability is invalid"):
        replace(
            transfer,
            admission_handoff_sha256=forged_handoff,
            inventory_sha256=forged_inventory,
        )

    first = provisional[0]
    with pytest.raises(CaptureContractError, match="opaque pre-trigger transfer"):
        runtime.submit_input(
            producer.producer_id,
            stream=first.stream,
            provider=first.provider,
            symbol=first.symbol,
            clocks=first.clocks,
            payload={
                **dict(first.payload),
                "_capture_promotion": {
                    "provisional_event_sha256": first.event_sha256
                },
            },
            recorded_at=promotion_at,
            promotion_id=transfer.promotion_id,
            promoted_at=transfer.promoted_at,
            promotion_source_identity_sha256=transfer.source_identity_sha256,
            promotion_resource_binding_sha256=transfer.resource_binding_sha256,
            promotion_inventory_sha256=transfer.inventory_sha256,
        )

    rows = []
    for source in provisional:
        rows.append(
            runtime.submit_input(
                producer.producer_id,
                stream=source.stream,
                provider=source.provider,
                symbol=source.symbol,
                clocks=source.clocks,
                payload={
                    **dict(source.payload),
                    "_capture_promotion": {
                        "provisional_event_sha256": source.event_sha256
                    },
                },
                recorded_at=promotion_at,
                promotion_id=transfer.promotion_id,
                promoted_at=transfer.promoted_at,
                promotion_source_identity_sha256=transfer.source_identity_sha256,
                promotion_resource_binding_sha256=transfer.resource_binding_sha256,
                promotion_inventory_sha256=transfer.inventory_sha256,
                promotion_transfer=transfer,
            )
        )

    assert [row.sequence for row in rows] == [3, 4]
    assert {row.clocks.available_at for row in rows} == {promotion_at}
    assert {
        row.payload["_capture_release"]["promotion_id"] for row in rows
    } == {transfer.promotion_id}
    assert all(
        row.payload["_capture_release"]["promoted_at"]
        == promotion_at.isoformat().replace("+00:00", "Z")
        for row in rows
    )


def test_stage1_refuses_stale_continuity_and_symbol_scoped_global_gap() -> None:
    (
        stale_runtime,
        _identity_row,
        _binding,
        stale_producer,
        stale_clock,
        _source,
        stale_read_id,
        stale_profile,
    ) = _stage1_fixture(decision_id="stale-continuity")
    stale_runtime.heartbeat(
        stale_producer.producer_id,
        recorded_at=stale_clock.set(BASE + timedelta(milliseconds=900)),
    )
    stale_clock.set(BASE + timedelta(milliseconds=1_100))
    with pytest.raises(CaptureContractError, match="continuity does not cover"):
        stale_runtime.attest_predecision_input_prefix(
            decision_id="stale-continuity",
            dependency_profile=stale_profile,
        )

    (
        gap_runtime,
        _identity_row,
        _binding,
        gap_producer,
        gap_clock,
        source,
        _gap_read_id,
        gap_profile,
    ) = _stage1_fixture(decision_id="global-gap")
    gap_at = gap_clock.set(BASE + timedelta(milliseconds=3))
    gap_runtime.report_gap(
        gap_producer.producer_id,
        stream=CaptureStream.COVERAGE_GAP,
        reason="gap_ledger_key_budget_overflow",
        first_available_at=source.clocks.available_at,
        last_available_at=gap_at,
        lost_count=1,
        recorded_at=gap_at,
        # A global ledger overflow remains global even if its last displaced
        # row happened to carry another symbol.
        symbol="OTHER",
    )
    gap_clock.set(BASE + timedelta(milliseconds=4))
    with pytest.raises(CaptureContractError, match="unresolved coverage gap"):
        gap_runtime.attest_predecision_input_prefix(
            decision_id="global-gap",
            dependency_profile=gap_profile,
        )


def test_change_log_stage1_requires_then_accepts_watermark_checkpoint() -> None:
    decision_id = "change-log-continuity"
    available_at = BASE + timedelta(milliseconds=2)
    runtime, producer, clock, profile, coverage = _change_log_stage1_fixture(
        decision_id=decision_id,
        market_reference_at=available_at,
        available_at=available_at,
        max_source_age_seconds=1.0,
    )
    clock.set(available_at + timedelta(microseconds=500))
    with pytest.raises(CaptureContractError, match="lacks a live checkpoint"):
        runtime.attest_predecision_input_prefix(
            decision_id=decision_id,
            dependency_profile=profile,
        )

    checkpoint_at = clock.set(available_at + timedelta(milliseconds=1))
    continuity = runtime.submit_live_continuity_checkpoint(
        producer.producer_id,
        coverage,
        recorded_at=checkpoint_at,
    )
    clock.set(available_at + timedelta(milliseconds=2))
    predecision = runtime.attest_predecision_input_prefix(
        decision_id=decision_id,
        dependency_profile=profile,
    )

    assert verify_active_capture_input_attestation(predecision) is predecision
    assert predecision.continuity_evidence == (continuity,)


def test_change_log_stage1_uses_source_clock_not_recent_availability() -> None:
    decision_id = "change-log-stale-source"
    available_at = BASE + timedelta(milliseconds=900)
    runtime, producer, clock, profile, coverage = _change_log_stage1_fixture(
        decision_id=decision_id,
        market_reference_at=BASE + timedelta(milliseconds=2),
        available_at=available_at,
        max_source_age_seconds=0.5,
    )
    checkpoint_at = clock.set(available_at + timedelta(milliseconds=1))
    runtime.submit_live_continuity_checkpoint(
        producer.producer_id,
        coverage,
        recorded_at=checkpoint_at,
    )
    clock.set(available_at + timedelta(milliseconds=2))

    with pytest.raises(CaptureContractError, match="inputs are already stale"):
        runtime.attest_predecision_input_prefix(
            decision_id=decision_id,
            dependency_profile=profile,
        )


def test_final_stage_refuses_stage1_after_continuity_deadline() -> None:
    decision_id = "continuity-expiry"
    (
        runtime,
        _identity_row,
        _binding,
        producer,
        clock,
        _source,
        read_id,
        profile,
    ) = _stage1_fixture(decision_id=decision_id)
    clock.set(BASE + timedelta(milliseconds=3))
    predecision = runtime.attest_predecision_input_prefix(
        decision_id=decision_id,
        dependency_profile=profile,
    )
    _submit_abstain_with_stage1(
        runtime,
        producer,
        clock,
        decision_id=decision_id,
        read_id=read_id,
        profile=profile,
        predecision=predecision,
        decided_at=BASE + timedelta(milliseconds=4),
    )
    runtime.heartbeat(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=900)),
    )
    clock.set(BASE + timedelta(milliseconds=1_100))
    with pytest.raises(CaptureContractError, match="fresh consumed predecision"):
        runtime.attest_active_prefix(
            decision_id=decision_id,
            required_read_ids=(read_id,),
        )


def test_typed_receipt_precedes_decision_and_is_owned_by_source_producer() -> None:
    identity = _identity()
    binding = _resource_binding()
    producer = _producer(
        identity,
        binding,
        streams=(CaptureStream.NBBO_QUOTE, CaptureStream.FSM_DECISION),
    )
    ingress = BoundedCaptureIngress.from_resource_binding(binding)
    clock = _ManualClock(BASE)
    runtime = CaptureProducerLifecycleRuntime(
        identity=identity,
        ingress=ingress,
        resource_binding=binding,
        producers=(producer,),
        heartbeat_timeout_seconds=1,
        wall_clock=clock,
    )
    opened = runtime.open(opened_at=BASE)
    registered = runtime.register(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=1)),
    )
    returned = clock.set(BASE + timedelta(milliseconds=2))
    source = runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.NBBO_QUOTE,
        provider="iqfeed",
        symbol="VEEE",
        clocks=CaptureClocks(
            provider_event_at=returned,
            received_at=returned,
            available_at=returned,
        ),
        payload={"bid": 4.99, "ask": 5.00},
        recorded_at=returned,
    )
    source_ref = CaptureEventRef.from_event(source)
    read_id = str(uuid.UUID(int=9001))
    receipt = CaptureReadReceipt(
        read_id=read_id,
        decision_id="decision-veee-1",
        identity_sha256=identity.identity_sha256,
        stream=CaptureStream.NBBO_QUOTE,
        provider="iqfeed",
        symbol="VEEE",
        requested_at=BASE + timedelta(milliseconds=1, microseconds=500),
        returned_at=returned,
        query_sha256=sha256_json({"surface": "latest_nbbo"}),
        source_event_sha256s=(source.event_sha256,),
        empty_result=False,
        result_sha256=captured_read_result_sha256((source_ref,)),
    )
    receipt_event = runtime.submit_read_receipt(receipt)
    assert receipt_event.stream is CaptureStream.READ_RECEIPT
    assert receipt_event.provider == "iqfeed"
    assert receipt_event.symbol == "VEEE"

    prefix_events = (opened, registered, source, receipt_event)
    prefix_root = capture_prefix_root_sha256(
        tuple(CaptureEventRef.from_event(row) for row in prefix_events),
        identity_sha256=identity.identity_sha256,
        through_sequence=receipt_event.sequence,
    )
    decision_at = clock.set(BASE + timedelta(milliseconds=3))
    decision_output = _abstain_decision_output("decision-veee-1", "VEEE")
    before_invalid_decision = runtime.health()["last_sequence"]
    with pytest.raises(CaptureContractError, match="decision output is missing"):
        runtime.submit_input(
            producer.producer_id,
            stream=CaptureStream.FSM_DECISION,
            provider="chili_fsm",
            symbol="VEEE",
            clocks=CaptureClocks(
                market_reference_at=decision_at,
                received_at=decision_at,
                available_at=decision_at,
            ),
            payload={
                "decision_id": "decision-veee-1",
                "symbol": "VEEE",
                "decision_at": decision_at.isoformat().replace("+00:00", "Z"),
                "input_prefix_sequence": receipt_event.sequence,
                "input_prefix_root_sha256": prefix_root,
                "required_read_ids": [read_id],
            },
            recorded_at=decision_at,
        )
    assert runtime.health()["last_sequence"] == before_invalid_decision
    decision = runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.FSM_DECISION,
        provider="chili_fsm",
        symbol="VEEE",
        clocks=CaptureClocks(
            market_reference_at=decision_at,
            received_at=decision_at,
            available_at=decision_at,
        ),
        payload={
            "decision_id": "decision-veee-1",
            "symbol": "VEEE",
            "decision_at": decision_at.isoformat().replace("+00:00", "Z"),
            "input_prefix_sequence": receipt_event.sequence,
            "input_prefix_root_sha256": prefix_root,
            "required_read_ids": [read_id],
            "decision_output": decision_output.to_dict(),
            "decision_output_sha256": decision_output.decision_output_sha256,
        },
        recorded_at=decision_at,
    )
    assert decision.sequence == receipt_event.sequence + 1

    late = replace(
        receipt,
        read_id=str(uuid.UUID(int=9002)),
        returned_at=decision_at,
    )
    with pytest.raises(CaptureContractError, match="after its decision"):
        runtime.submit_read_receipt(late)


def test_order_decision_binds_exact_typed_broker_lifecycle() -> None:
    identity = _identity()
    binding = _resource_binding()
    decision_producer = _producer(
        identity,
        binding,
        suffix=1,
        streams=(CaptureStream.NBBO_QUOTE, CaptureStream.FSM_DECISION),
    )
    broker_producer = _producer(
        identity,
        binding,
        suffix=2,
        streams=(CaptureStream.BROKER_ORDER_LIFECYCLE,),
    )
    ingress = BoundedCaptureIngress.from_resource_binding(binding)
    clock = _ManualClock(BASE)
    runtime = CaptureProducerLifecycleRuntime(
        identity=identity,
        ingress=ingress,
        resource_binding=binding,
        producers=(decision_producer, broker_producer),
        heartbeat_timeout_seconds=1,
        wall_clock=clock,
    )
    events = [runtime.open(opened_at=BASE)]
    events.append(
        runtime.register(
            decision_producer.producer_id,
            recorded_at=clock.set(BASE + timedelta(milliseconds=1)),
        )
    )
    events.append(
        runtime.register(
            broker_producer.producer_id,
            recorded_at=clock.set(BASE + timedelta(milliseconds=2)),
        )
    )
    source_at = clock.set(BASE + timedelta(milliseconds=3))
    source = runtime.submit_input(
        decision_producer.producer_id,
        stream=CaptureStream.NBBO_QUOTE,
        provider="iqfeed",
        symbol="VEEE",
        clocks=CaptureClocks(
            provider_event_at=source_at,
            received_at=source_at,
            available_at=source_at,
        ),
        payload={"bid": 5.00, "ask": 5.01},
        recorded_at=source_at,
    )
    events.append(source)
    read_id = str(uuid.UUID(int=9100))
    decision_id = "decision-veee-order-1"
    receipt = CaptureReadReceipt(
        read_id=read_id,
        decision_id=decision_id,
        identity_sha256=identity.identity_sha256,
        stream=CaptureStream.NBBO_QUOTE,
        provider="iqfeed",
        symbol="VEEE",
        requested_at=BASE + timedelta(milliseconds=2, microseconds=500),
        returned_at=source_at,
        query_sha256=sha256_json({"surface": "boundary_nbbo"}),
        source_event_sha256s=(source.event_sha256,),
        empty_result=False,
        result_sha256=captured_read_result_sha256(
            (CaptureEventRef.from_event(source),)
        ),
    )
    events.append(runtime.submit_read_receipt(receipt))
    continuity = runtime.submit_live_continuity_checkpoint(
        decision_producer.producer_id,
        _live_continuity(identity, source),
        recorded_at=source_at,
    )
    profile = _dependency_profile(
        stream=CaptureStream.NBBO_QUOTE,
        read_id=read_id,
        coverage_start_at=source_at,
    )
    clock.set(BASE + timedelta(milliseconds=3, microseconds=500))
    predecision = runtime.attest_predecision_input_prefix(
        decision_id=decision_id,
        dependency_profile=profile,
    )
    assert verify_active_capture_input_attestation(predecision) is predecision
    assert predecision.continuity_evidence == (continuity,)
    client_order_id = "chili-paper-veee-entry-0001"
    intent = CaptureOrderIntent(
        intent_id=str(uuid.UUID(int=9101)),
        client_order_id=client_order_id,
        client_order_id_sha256=sha256_json(
            {"client_order_id": client_order_id}
        ),
        symbol="VEEE",
        side="sell",
        order_type="limit",
        quantity=10,
        time_in_force="day",
        extended_hours=True,
        intent_role=CaptureOrderIntentRole.EXIT,
        risk_increasing=False,
        decision_provenance_sha256=predecision.attestation_sha256,
        limit_price=5.01,
    )
    output = CaptureDecisionOutput(
        decision_id=decision_id,
        symbol="VEEE",
        action=CaptureDecisionAction.ORDER_INTENT,
        fsm_state="risk_exit",
        setup_role="exit",
        order_intents=(intent,),
    )
    decision_at = clock.set(BASE + timedelta(milliseconds=4))
    decision_event = runtime.submit_input(
            decision_producer.producer_id,
            stream=CaptureStream.FSM_DECISION,
            provider="chili_fsm",
            symbol="VEEE",
            clocks=CaptureClocks(
                market_reference_at=decision_at,
                received_at=decision_at,
                available_at=decision_at,
            ),
            payload={
                "decision_id": output.decision_id,
                "symbol": "VEEE",
                "decision_at": decision_at.isoformat().replace("+00:00", "Z"),
                "input_prefix_sequence": predecision.input_prefix_sequence,
                "input_prefix_root_sha256": predecision.input_prefix_root_sha256,
                "required_read_ids": [read_id],
                "fsm_dependency_profile": profile.to_dict(),
                "predecision_attestation_sha256": predecision.attestation_sha256,
                "predecision_read_evidence_inventory_sha256": (
                    predecision.read_evidence_inventory_sha256
                ),
                "predecision_continuity_evidence_inventory_sha256": (
                    predecision.continuity_evidence_inventory_sha256
                ),
                "decision_output": output.to_dict(),
                "decision_output_sha256": output.decision_output_sha256,
            },
            recorded_at=decision_at,
            predecision_attestation=predecision,
    )
    events.append(decision_event)
    clock.set(BASE + timedelta(milliseconds=4, microseconds=100))
    final_proof = runtime.attest_active_prefix(
        decision_id=decision_id,
        required_read_ids=(read_id,),
    )

    submitted_at = clock.set(BASE + timedelta(milliseconds=5))
    submitted = CaptureBrokerOrderLifecycle(
        decision_id=output.decision_id,
        order_intent_sha256=intent.order_intent_sha256,
        client_order_id=client_order_id,
        client_order_id_sha256=intent.client_order_id_sha256,
        transition=CaptureBrokerTransition.SUBMITTED,
        order_quantity=10,
        cumulative_filled_quantity=0,
        last_fill_quantity=0,
        prior_transition_event_sha256=None,
        final_decision_attestation_sha256=final_proof.durable_authority_sha256,
    )
    submitted_event = runtime.submit_broker_order_lifecycle(
        broker_producer.producer_id,
        submitted,
        provider="alpaca_paper",
        symbol="VEEE",
        clocks=CaptureClocks(
            provider_event_at=submitted_at,
            received_at=submitted_at,
            available_at=submitted_at,
        ),
        recorded_at=submitted_at,
    )
    events.append(submitted_event)

    accepted_at = clock.set(BASE + timedelta(milliseconds=6))
    wrong_chain = CaptureBrokerOrderLifecycle(
        decision_id=output.decision_id,
        order_intent_sha256=intent.order_intent_sha256,
        client_order_id=client_order_id,
        client_order_id_sha256=intent.client_order_id_sha256,
        broker_order_id="alpaca-order-1",
        raw_provider_event_sha256="4" * 64,
        transition=CaptureBrokerTransition.ACCEPTED,
        order_quantity=10,
        cumulative_filled_quantity=0,
        last_fill_quantity=0,
        prior_transition_event_sha256="f" * 64,
        final_decision_attestation_sha256=final_proof.durable_authority_sha256,
    )
    before_invalid_transition = runtime.health()["last_sequence"]
    with pytest.raises(CaptureContractError, match="hash chain is broken"):
        runtime.submit_broker_order_lifecycle(
            broker_producer.producer_id,
            wrong_chain,
            provider="alpaca_paper",
            symbol="VEEE",
            clocks=CaptureClocks(
                provider_event_at=accepted_at,
                received_at=accepted_at,
                available_at=accepted_at,
            ),
            recorded_at=accepted_at,
        )
    assert runtime.health()["last_sequence"] == before_invalid_transition

    accepted = replace(
        wrong_chain,
        prior_transition_event_sha256=submitted_event.event_sha256,
    )
    accepted_event = runtime.submit_broker_order_lifecycle(
        broker_producer.producer_id,
        accepted,
        provider="alpaca_paper",
        symbol="VEEE",
        clocks=CaptureClocks(
            provider_event_at=accepted_at,
            received_at=accepted_at,
            available_at=accepted_at,
        ),
        recorded_at=accepted_at,
    )
    events.append(accepted_event)

    partial_at = clock.set(BASE + timedelta(milliseconds=7))
    partial = CaptureBrokerOrderLifecycle(
        decision_id=output.decision_id,
        order_intent_sha256=intent.order_intent_sha256,
        client_order_id=client_order_id,
        client_order_id_sha256=intent.client_order_id_sha256,
        broker_order_id="alpaca-order-1",
        raw_provider_event_sha256="5" * 64,
        transition=CaptureBrokerTransition.PARTIALLY_FILLED,
        order_quantity=10,
        cumulative_filled_quantity=4,
        last_fill_quantity=4,
        last_fill_price=5.00,
        prior_transition_event_sha256=accepted_event.event_sha256,
        final_decision_attestation_sha256=final_proof.durable_authority_sha256,
    )
    partial_event = runtime.submit_broker_order_lifecycle(
        broker_producer.producer_id,
        partial,
        provider="alpaca_paper",
        symbol="VEEE",
        clocks=CaptureClocks(
            provider_event_at=partial_at,
            received_at=partial_at,
            available_at=partial_at,
        ),
        recorded_at=partial_at,
    )
    events.append(partial_event)

    filled_at = clock.set(BASE + timedelta(milliseconds=8))
    filled = CaptureBrokerOrderLifecycle(
        decision_id=output.decision_id,
        order_intent_sha256=intent.order_intent_sha256,
        client_order_id=client_order_id,
        client_order_id_sha256=intent.client_order_id_sha256,
        broker_order_id="alpaca-order-1",
        raw_provider_event_sha256="6" * 64,
        transition=CaptureBrokerTransition.FILLED,
        order_quantity=10,
        cumulative_filled_quantity=10,
        last_fill_quantity=6,
        last_fill_price=5.01,
        prior_transition_event_sha256=partial_event.event_sha256,
        final_decision_attestation_sha256=final_proof.durable_authority_sha256,
    )
    events.append(
        runtime.submit_broker_order_lifecycle(
            broker_producer.producer_id,
            filled,
            provider="alpaca_paper",
            symbol="VEEE",
            clocks=CaptureClocks(
                provider_event_at=filled_at,
                received_at=filled_at,
                available_at=filled_at,
            ),
            recorded_at=filled_at,
        )
    )

    assert grade_capture_decision_order_path(tuple(events)) == ()
    assert runtime.health()["order_intent_count"] == 1
    assert runtime.health()["terminal_broker_lifecycle_count"] == 1


def test_private_active_prefix_attests_exact_current_first_dip_tape() -> None:
    identity = _identity()
    binding = _resource_binding()
    producer = _producer(
        identity,
        binding,
        streams=(CaptureStream.IQFEED_PRINT, CaptureStream.FSM_DECISION),
    )
    ingress = BoundedCaptureIngress.from_resource_binding(binding)
    clock = _ManualClock(BASE)
    runtime = CaptureProducerLifecycleRuntime(
        identity=identity,
        ingress=ingress,
        resource_binding=binding,
        producers=(producer,),
        heartbeat_timeout_seconds=1,
        wall_clock=clock,
    )
    opened = runtime.open(opened_at=BASE)
    registered = runtime.register(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=1)),
    )
    warmup_at = clock.set(BASE + timedelta(milliseconds=1, microseconds=200))
    warmup_source = runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        clocks=CaptureClocks(
            provider_event_at=warmup_at,
            received_at=warmup_at,
            available_at=warmup_at,
        ),
        payload={
            "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
            "symbol": "VEEE",
            "price": 4.90,
            "size": 100,
            "conditions": ["fixture-warmup"],
        },
        recorded_at=warmup_at,
    )
    returned = clock.set(BASE + timedelta(milliseconds=2))
    sources = tuple(
        runtime.submit_input(
            producer.producer_id,
            stream=CaptureStream.IQFEED_PRINT,
            provider="iqfeed",
            symbol="VEEE",
            clocks=CaptureClocks(
                provider_event_at=provider_event_at,
                received_at=returned,
                available_at=returned,
            ),
            payload={
                "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
                "symbol": "VEEE",
                "price": price,
                "size": size,
                "bid": bid,
                "ask": ask,
                "conditions": ["fixture-only"],
            },
            recorded_at=returned,
        )
        for provider_event_at, price, size, bid, ask in (
            (
                BASE + timedelta(milliseconds=1, microseconds=600),
                4.90,
                100,
                4.90,
                4.91,
            ),
            (
                BASE + timedelta(milliseconds=1, microseconds=800),
                5.01,
                200,
                5.00,
                5.01,
            ),
            (
                BASE + timedelta(milliseconds=1, microseconds=900),
                5.02,
                300,
                5.01,
                5.02,
            ),
        )
    )
    read_id = str(uuid.UUID(int=9020))
    tape_policy = FirstDipTapePolicy(
        window_seconds=0.0005,
        max_source_age_seconds=1.0,
        tick_rate_floor_pctile=0.0,
    )
    tape_query = FirstDipTapeReadQuery(
        symbol="VEEE",
        provider="iqfeed",
        event_start_exclusive=returned - timedelta(
            seconds=tape_policy.window_seconds
        ),
        event_end_inclusive=returned,
        decision_at=returned,
        available_at_most=returned,
        source_frontier_sequence=sources[-1].sequence,
        policy_sha256=tape_policy.policy_sha256,
    )
    receipt = CaptureReadReceipt(
        read_id=read_id,
        decision_id="decision-first-dip-attested",
        identity_sha256=identity.identity_sha256,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        requested_at=BASE + timedelta(milliseconds=1),
        returned_at=returned,
        query_sha256=sha256_json(tape_query.to_dict()),
        source_event_sha256s=tuple(source.event_sha256 for source in sources),
        empty_result=False,
        result_sha256=captured_read_result_sha256(
            tuple(CaptureEventRef.from_event(source) for source in sources)
        ),
        query=tape_query.to_dict(),
    )
    receipt_event, typed = runtime.submit_first_dip_tape_receipt(receipt)
    assert typed.read_evidence.producer_generation == identity.generation
    assert typed.read_evidence.source_event_refs[0].received_at == returned
    # A provider callback can win the lock immediately after the read commit
    # while the trusted wall clock is unchanged.  It is causally later by
    # sequence, so it belongs to the continuity checkpoint but not to the
    # already-committed query-frontier inventory.
    post_query_source = runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        clocks=CaptureClocks(
            provider_event_at=returned,
            received_at=returned,
            available_at=returned,
        ),
        payload={
            "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
            "symbol": "VEEE",
            "price": 5.03,
            "size": 50,
            "bid": 5.02,
            "ask": 5.03,
            "conditions": ["fixture-post-query"],
        },
        recorded_at=returned,
    )
    assert post_query_source.sequence > receipt_event.sequence
    assert post_query_source.sequence > tape_query.source_frontier_sequence
    continuity_at = clock.set(BASE + timedelta(milliseconds=2, microseconds=100))
    coverage = StreamCoverage(
        stream=CaptureStream.IQFEED_PRINT,
        identity_sha256=identity.identity_sha256,
        provider="iqfeed",
        symbol="VEEE",
        first_available_at=warmup_source.clocks.available_at,
        last_available_at=returned,
        event_count=5,
        exact_event_clock_complete=True,
        content_verified=True,
        continuity_complete=True,
        watermark=ProviderWatermark(
            stream=CaptureStream.IQFEED_PRINT,
            provider="iqfeed",
            identity_sha256=identity.identity_sha256,
            event_watermark_at=returned,
            emitted_available_at=continuity_at,
            bounded_lateness_seconds=1,
            max_observed_lateness_seconds=0.001,
            generation=identity.generation,
            symbol="VEEE",
        ),
    )
    runtime.submit_live_continuity_checkpoint(
        producer.producer_id,
        coverage,
        recorded_at=continuity_at,
    )
    dependency_profile = FSMDependencyProfile(
        required_streams=frozenset({CaptureStream.IQFEED_PRINT}),
        required_read_ids=(read_id,),
        stream_dependencies=(
            FSMStreamDependency(
                stream=CaptureStream.IQFEED_PRINT,
                exact_provider_event_at_required=True,
                market_reference_at_required=False,
                max_source_age_seconds=1.0,
                coverage_start_at=tape_query.event_start_exclusive,
            ),
        ),
    )
    clock.set(BASE + timedelta(milliseconds=2, microseconds=200))
    predecision = runtime.attest_predecision_input_prefix(
        decision_id=receipt.decision_id,
        dependency_profile=dependency_profile,
        first_dip_tape_read_id=read_id,
    )
    decision_at = returned
    tape_evaluation = evaluate_first_dip_tape(
        first_dip_tape_window_from_capture(receipt, sources),
        policy=tape_policy,
        decision_at=decision_at,
        symbol="VEEE",
    )
    assert tape_evaluation.status == "valid_positive"
    assert tape_evaluation.confirmed is True
    _entry_intent, adaptive_artifact = _valid_adaptive_entry_for_predecision(
        identity=identity,
        predecision=predecision,
        decision_id=receipt.decision_id,
        symbol="VEEE",
        as_of=decision_at,
        setup_family="first_dip_reclaim",
    )
    adaptive_request = load_adaptive_risk_reservation_request(
        adaptive_artifact.reservation_request
    )
    detector_authority = runtime.prepare_captured_first_dip_tape_authority(
        attestation=predecision,
        policy=tape_policy,
        purpose="detector",
    )
    with (
        first_dip_decision
        ._installed_captured_db_paper_first_dip_tape_decision_authority(
            detector_authority
        )
    ):
        detector_resolution = first_dip_decision.resolve_first_dip_tape_decision(
            symbol="VEEE",
            decision_at=decision_at,
            policy=tape_policy,
            purpose="detector",
        )
    assert detector_resolution.run_bound
    assert detector_resolution.evaluation.status == "valid_positive"
    assert detector_resolution.receipt is not None
    assert adaptive_request.opportunity_key is not None
    opportunity_sha256 = adaptive_request.opportunity_key.key_sha256
    prior_reference_sha256 = runtime.retain_accepted_first_dip_detector(
        resolution=detector_resolution,
        opportunity_key_sha256=opportunity_sha256,
    )
    client_order_id = "chili-paper-veee-first-dip-v1-blocked"
    blocked_intent = CaptureOrderIntent(
        intent_id=str(uuid.UUID(int=9021)),
        client_order_id=client_order_id,
        client_order_id_sha256=sha256_json(
            {"client_order_id": client_order_id}
        ),
        symbol="VEEE",
        side="sell",
        order_type="limit",
        quantity=1,
        time_in_force="day",
        extended_hours=True,
        intent_role=CaptureOrderIntentRole.EXIT,
        risk_increasing=False,
        decision_provenance_sha256=predecision.attestation_sha256,
        limit_price=5.01,
    )
    blocked_output = CaptureDecisionOutput(
        decision_id=receipt.decision_id,
        symbol="VEEE",
        action=CaptureDecisionAction.ORDER_INTENT,
        fsm_state="entry_ready",
        setup_role="first_dip_reclaim",
        order_intents=(blocked_intent,),
    )
    blocked_recorded_at = clock.set(BASE + timedelta(milliseconds=2, microseconds=500))
    common_decision_payload = {
        "decision_id": receipt.decision_id,
        "symbol": "VEEE",
        "decision_at": decision_at.isoformat().replace("+00:00", "Z"),
        "input_prefix_sequence": predecision.input_prefix_sequence,
        "input_prefix_root_sha256": predecision.input_prefix_root_sha256,
        "required_read_ids": [read_id],
        "fsm_dependency_profile": dependency_profile.to_dict(),
        "first_dip_tape_read_id": read_id,
        "first_dip_tape_policy": tape_policy.to_dict(),
        "first_dip_tape_policy_sha256": tape_policy.policy_sha256,
        "first_dip_tape_evaluation": tape_evaluation.to_dict(),
        "first_dip_tape_evaluation_sha256": tape_evaluation.evaluation_sha256,
        "predecision_attestation_sha256": predecision.attestation_sha256,
        "predecision_read_evidence_inventory_sha256": (
            predecision.read_evidence_inventory_sha256
        ),
        "predecision_continuity_evidence_inventory_sha256": (
            predecision.continuity_evidence_inventory_sha256
        ),
    }
    with pytest.raises(
        CaptureContractError,
        match="first-dip IQFeed v1 evidence is mechanics-only",
    ):
        runtime.submit_input(
            producer.producer_id,
            stream=CaptureStream.FSM_DECISION,
            provider="chili_fsm",
            symbol="VEEE",
            clocks=CaptureClocks(
                market_reference_at=decision_at,
                received_at=blocked_recorded_at,
                available_at=blocked_recorded_at,
            ),
            payload={
                **common_decision_payload,
                "decision_output": blocked_output.to_dict(),
                "decision_output_sha256": blocked_output.decision_output_sha256,
            },
            recorded_at=blocked_recorded_at,
            predecision_attestation=predecision,
        )
    decision_output = replace(
        _abstain_decision_output(
            receipt.decision_id,
            "VEEE",
        ),
        setup_role="first_dip_reclaim",
    )
    decision_recorded_at = clock.set(BASE + timedelta(milliseconds=3))
    decision = runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.FSM_DECISION,
        provider="chili_fsm",
        symbol="VEEE",
        clocks=CaptureClocks(
            market_reference_at=decision_at,
            received_at=decision_recorded_at,
            available_at=decision_recorded_at,
        ),
        payload={
            **common_decision_payload,
            "decision_output": decision_output.to_dict(),
            "decision_output_sha256": decision_output.decision_output_sha256,
        },
        recorded_at=decision_recorded_at,
        predecision_attestation=predecision,
    )
    attestation = runtime.attest_active_prefix(
        decision_id=receipt.decision_id,
        required_read_ids=(read_id,),
        first_dip_tape_read_id=read_id,
    )

    assert verify_active_capture_prefix_attestation(attestation) is attestation
    assert attestation.decision_event_sha256 == decision.event_sha256
    assert (
        attestation.decision_output_sha256
        == decision_output.decision_output_sha256
    )
    assert attestation.order_intent_sha256s == ()
    assert attestation.input_prefix_sequence == predecision.input_prefix_sequence
    assert (
        attestation.input_prefix_root_sha256
        == predecision.input_prefix_root_sha256
    )
    assert attestation.attestation_frontier_sequence == decision.sequence
    assert attestation.first_dip_tape_read_id == read_id
    assert attestation.expires_at == predecision.expires_at
    assert attestation.continuity_evidence == predecision.continuity_evidence
    assert (
        predecision.continuity_evidence[0].source_frontier_sequence
        == post_query_source.sequence
    )
    assert (
        predecision.continuity_evidence[0].source_frontier_sequence
        > tape_query.source_frontier_sequence
    )
    assert attestation.account_identity_sha256 == identity.account_identity_sha256
    assert attestation.resource_binding_sha256 == binding.binding_sha256
    assert attestation.producer_generations == {producer.producer_id: 3}
    evidence = attestation.read_evidence[0]
    assert evidence.receipt_event_sha256 == receipt_event.event_sha256
    assert len(evidence.source_event_refs) == 3
    assert all(
        source_ref.provider_event_at is not None
        and source_ref.received_at == returned
        and source_ref.available_at == returned
        for source_ref in evidence.source_event_refs
    )

    # An unrelated later heartbeat does not invalidate the immutable snapshot.
    runtime.heartbeat(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=4)),
    )
    assert verify_active_capture_prefix_attestation(attestation) is attestation

    # The final pre-reservation check is a distinct FSM tick with a new read,
    # watermark, and runtime-HMAC-bound link to the accepted detector receipt.
    final_returned = clock.set(BASE + timedelta(milliseconds=4, microseconds=100))
    final_sources = tuple(
        runtime.submit_input(
            producer.producer_id,
            stream=CaptureStream.IQFEED_PRINT,
            provider="iqfeed",
            symbol="VEEE",
            clocks=CaptureClocks(
                provider_event_at=provider_event_at,
                received_at=final_returned,
                available_at=final_returned,
            ),
            payload={
                "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
                "symbol": "VEEE",
                "price": price,
                "size": size,
                "bid": bid,
                "ask": ask,
                "conditions": ["fixture-final-checkpoint"],
            },
            recorded_at=final_returned,
        )
        for provider_event_at, price, size, bid, ask in (
            (
                BASE + timedelta(milliseconds=3, microseconds=700),
                5.03,
                100,
                5.02,
                5.03,
            ),
            (
                BASE + timedelta(milliseconds=3, microseconds=900),
                5.10,
                200,
                5.09,
                5.10,
            ),
            (
                BASE + timedelta(milliseconds=4),
                5.12,
                300,
                5.11,
                5.12,
            ),
        )
    )
    final_decision_id = "decision-first-dip-pre-reservation"
    final_read_id = str(uuid.UUID(int=9022))
    final_query = FirstDipTapeReadQuery(
        symbol="VEEE",
        provider="iqfeed",
        event_start_exclusive=final_returned
        - timedelta(seconds=tape_policy.window_seconds),
        event_end_inclusive=final_returned,
        decision_at=final_returned,
        available_at_most=final_returned,
        source_frontier_sequence=final_sources[-1].sequence,
        policy_sha256=tape_policy.policy_sha256,
    )
    final_receipt = CaptureReadReceipt(
        read_id=final_read_id,
        decision_id=final_decision_id,
        identity_sha256=identity.identity_sha256,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        requested_at=BASE + timedelta(milliseconds=4, microseconds=50),
        returned_at=final_returned,
        query_sha256=sha256_json(final_query.to_dict()),
        source_event_sha256s=tuple(
            source.event_sha256 for source in final_sources
        ),
        empty_result=False,
        result_sha256=captured_read_result_sha256(
            tuple(CaptureEventRef.from_event(source) for source in final_sources)
        ),
        query=final_query.to_dict(),
    )
    runtime.submit_first_dip_tape_receipt(final_receipt)
    final_continuity_at = clock.set(
        BASE + timedelta(milliseconds=4, microseconds=200)
    )
    runtime.submit_live_continuity_checkpoint(
        producer.producer_id,
        StreamCoverage(
            stream=CaptureStream.IQFEED_PRINT,
            identity_sha256=identity.identity_sha256,
            provider="iqfeed",
            symbol="VEEE",
            first_available_at=warmup_source.clocks.available_at,
            last_available_at=final_returned,
            event_count=8,
            exact_event_clock_complete=True,
            content_verified=True,
            continuity_complete=True,
            watermark=ProviderWatermark(
                stream=CaptureStream.IQFEED_PRINT,
                provider="iqfeed",
                identity_sha256=identity.identity_sha256,
                event_watermark_at=final_returned,
                emitted_available_at=final_continuity_at,
                bounded_lateness_seconds=1,
                max_observed_lateness_seconds=0.001,
                generation=identity.generation,
                symbol="VEEE",
            ),
        ),
        recorded_at=final_continuity_at,
    )
    final_dependency_profile = FSMDependencyProfile(
        required_streams=frozenset({CaptureStream.IQFEED_PRINT}),
        required_read_ids=(final_read_id,),
        stream_dependencies=(
            FSMStreamDependency(
                stream=CaptureStream.IQFEED_PRINT,
                exact_provider_event_at_required=True,
                market_reference_at_required=False,
                max_source_age_seconds=1.0,
                coverage_start_at=final_query.event_start_exclusive,
            ),
        ),
    )
    clock.set(BASE + timedelta(milliseconds=4, microseconds=300))
    final_proof = runtime.attest_first_dip_pre_reservation_input_prefix(
        adaptive_request=adaptive_request,
        dependency_profile=final_dependency_profile,
        first_dip_tape_read_id=final_read_id,
    )
    assert (
        final_proof.first_dip_prior_detector_reference_sha256
        == prior_reference_sha256
    )
    assert (
        final_proof.first_dip_adaptive_request_sha256
        == adaptive_request.request_sha256
    )
    assert final_proof.first_dip_opportunity_key_sha256 == opportunity_sha256

    def resolve_final(*, detector_binding_sha256: str):
        authority = runtime.prepare_captured_first_dip_tape_authority(
            attestation=final_proof,
            policy=tape_policy,
            purpose="pre_reservation",
        )
        with (
            first_dip_decision
            ._installed_captured_db_paper_first_dip_tape_decision_authority(
                authority
            )
        ):
            return first_dip_decision._resolve_installed_first_dip_final_admission(
                symbol=adaptive_request.inputs.symbol,
                adaptive_decision_at=adaptive_request.inputs.as_of,
                run_id=adaptive_request.inputs.replay_or_paper_run_id,
                generation=adaptive_request.inputs.generation,
                adaptive_decision_id=adaptive_request.inputs.decision_id,
                adaptive_input_prefix_root_sha256=(
                    adaptive_request.inputs.capture_prefix_root_sha256
                ),
                adaptive_request_sha256=adaptive_request.request_sha256,
                opportunity_key_sha256=opportunity_sha256,
                final_boundary_available_at=final_proof.attested_available_at,
                expected_execution_surface="captured_db_paper",
                detector_policy_sha256=tape_policy.policy_sha256,
                detector_authority_source="captured_db_paper",
                detector_receipt_binding_sha256=detector_binding_sha256,
                detector_opportunity_key_sha256=opportunity_sha256,
            )

    order_count_before = runtime.health()["order_intent_count"]
    mismatch = resolve_final(detector_binding_sha256="f" * 64)
    assert not mismatch.admitted
    assert mismatch.reason == "first_dip_final_admission_detector_audit_mismatch"
    assert runtime.health()["order_intent_count"] == order_count_before

    admitted = resolve_final(
        detector_binding_sha256=detector_resolution.receipt.binding_sha256
    )
    assert admitted.admitted
    assert admitted.reason == "first_dip_final_admission_typed_receipt_verified"
    assert admitted.reservation_authority is False
    assert admitted.order_authority is False
    assert runtime.health()["order_intent_count"] == order_count_before

    with pytest.raises(CaptureContractError, match="attestation changed"):
        replace(attestation, input_prefix_root_sha256="f" * 64)
    with pytest.raises(
        CaptureContractError,
        match="expiry must follow final proof issuance",
    ):
        replace(attestation, expires_at=attestation.attested_available_at)
    public_values = {
        key: value
        for key, value in attestation.__dict__.items()
        if not key.startswith("_")
    }
    with pytest.raises(CaptureContractError, match="not issued by the runtime"):
        ActiveCapturePrefixAttestation(**public_values)


def test_generic_iqfeed_v1_read_cannot_authorize_risk_but_exit_remains_available() -> None:
    identity = _identity()
    binding = _resource_binding()
    producer = _producer(
        identity,
        binding,
        streams=(
            CaptureStream.IQFEED_PRINT,
            CaptureStream.FSM_DECISION,
            CaptureStream.BROKER_ORDER_LIFECYCLE,
        ),
    )
    ingress = BoundedCaptureIngress.from_resource_binding(binding)
    clock = _ManualClock(BASE)
    runtime = CaptureProducerLifecycleRuntime(
        identity=identity,
        ingress=ingress,
        resource_binding=binding,
        producers=(producer,),
        heartbeat_timeout_seconds=1,
        wall_clock=clock,
    )
    runtime.open(opened_at=BASE)
    runtime.register(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=1)),
    )
    source_at = clock.set(BASE + timedelta(milliseconds=2))
    source = runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        clocks=CaptureClocks(
            provider_event_at=source_at,
            received_at=source_at,
            available_at=source_at,
        ),
        payload={
            "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
            "symbol": "VEEE",
            "price": 5.01,
            "size": 100,
            "bid": 5.00,
            "ask": 5.01,
            "conditions": ["fixture-only"],
        },
        recorded_at=source_at,
    )
    decision_id = "decision-generic-iqfeed-v1-bypass"
    read_id = str(uuid.UUID(int=9129))
    generic_query = {"surface": "latest_iqfeed_print"}
    receipt = CaptureReadReceipt(
        read_id=read_id,
        decision_id=decision_id,
        identity_sha256=identity.identity_sha256,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        requested_at=BASE + timedelta(milliseconds=1, microseconds=500),
        returned_at=source_at,
        query_sha256=sha256_json(generic_query),
        source_event_sha256s=(source.event_sha256,),
        empty_result=False,
        result_sha256=captured_read_result_sha256(
            (CaptureEventRef.from_event(source),)
        ),
        query=generic_query,
    )
    runtime.submit_read_receipt(receipt)
    runtime.submit_live_continuity_checkpoint(
        producer.producer_id,
        _live_continuity(identity, source),
        recorded_at=clock.set(BASE + timedelta(milliseconds=2, microseconds=100)),
    )
    dependency_profile = _dependency_profile(
        stream=CaptureStream.IQFEED_PRINT,
        read_id=read_id,
        coverage_start_at=source_at,
    )
    clock.set(BASE + timedelta(milliseconds=2, microseconds=200))
    predecision = runtime.attest_predecision_input_prefix(
        decision_id=decision_id,
        dependency_profile=dependency_profile,
    )
    assert predecision.first_dip_tape_read_id is None
    entry_intent, artifact = _valid_adaptive_entry_for_predecision(
        identity=identity,
        predecision=predecision,
        decision_id=decision_id,
        symbol="VEEE",
        as_of=source_at,
    )
    disguised_entry = CaptureDecisionOutput(
        decision_id=decision_id,
        symbol="VEEE",
        action=CaptureDecisionAction.ORDER_INTENT,
        fsm_state="entry_ready",
        setup_role="generic_breakout_not_first_dip",
        order_intents=(entry_intent,),
    )
    common_payload = {
        "decision_id": decision_id,
        "symbol": "VEEE",
        "decision_at": source_at.isoformat().replace("+00:00", "Z"),
        "input_prefix_sequence": predecision.input_prefix_sequence,
        "input_prefix_root_sha256": predecision.input_prefix_root_sha256,
        "required_read_ids": [read_id],
        "fsm_dependency_profile": dependency_profile.to_dict(),
        "predecision_attestation_sha256": predecision.attestation_sha256,
        "predecision_read_evidence_inventory_sha256": (
            predecision.read_evidence_inventory_sha256
        ),
        "predecision_continuity_evidence_inventory_sha256": (
            predecision.continuity_evidence_inventory_sha256
        ),
    }
    assert "first_dip_tape_read_id" not in common_payload
    sequence_before_attempt = runtime.health()["last_sequence"]
    rejected_at = clock.set(BASE + timedelta(milliseconds=2, microseconds=500))
    with pytest.raises(
        CaptureContractError,
        match=(
            "risk-increasing order cannot use IQFeed print v1 "
            "mechanics-only evidence"
        ),
    ):
        runtime.submit_input(
            producer.producer_id,
            stream=CaptureStream.FSM_DECISION,
            provider="chili_fsm",
            symbol="VEEE",
            clocks=CaptureClocks(
                market_reference_at=source_at,
                received_at=rejected_at,
                available_at=rejected_at,
            ),
            payload={
                **common_payload,
                "decision_output": disguised_entry.to_dict(),
                "decision_output_sha256": disguised_entry.decision_output_sha256,
                "adaptive_order_artifacts": [artifact.to_dict()],
            },
            recorded_at=rejected_at,
            predecision_attestation=predecision,
        )
    assert runtime.health()["last_sequence"] == sequence_before_attempt

    exit_client_order_id = "chili-paper-veee-generic-v1-exit"
    exit_intent = CaptureOrderIntent(
        intent_id=str(uuid.UUID(int=9131)),
        client_order_id=exit_client_order_id,
        client_order_id_sha256=sha256_json(
            {"client_order_id": exit_client_order_id}
        ),
        symbol="VEEE",
        side="sell",
        order_type="limit",
        quantity=1,
        time_in_force="day",
        extended_hours=False,
        intent_role=CaptureOrderIntentRole.EXIT,
        risk_increasing=False,
        decision_provenance_sha256=predecision.attestation_sha256,
        limit_price=5.00,
    )
    exit_output = CaptureDecisionOutput(
        decision_id=decision_id,
        symbol="VEEE",
        action=CaptureDecisionAction.ORDER_INTENT,
        fsm_state="risk_exit",
        setup_role="risk_exit",
        order_intents=(exit_intent,),
    )
    exit_recorded_at = clock.set(BASE + timedelta(milliseconds=3))
    exit_event = runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.FSM_DECISION,
        provider="chili_fsm",
        symbol="VEEE",
        clocks=CaptureClocks(
            market_reference_at=source_at,
            received_at=exit_recorded_at,
            available_at=exit_recorded_at,
        ),
        payload={
            **common_payload,
            "decision_output": exit_output.to_dict(),
            "decision_output_sha256": exit_output.decision_output_sha256,
            "adaptive_order_artifacts": [],
        },
        recorded_at=exit_recorded_at,
        predecision_attestation=predecision,
    )
    assert exit_event.sequence == sequence_before_attempt + 1
    assert runtime.health()["order_intent_count"] == 1


def test_first_dip_adaptive_request_cannot_hide_label_or_omit_typed_tape() -> None:
    decision_id = "decision-first-dip-request-without-tape"
    (
        runtime,
        identity,
        _binding,
        producer,
        clock,
        source,
        read_id,
        profile,
    ) = _stage1_fixture(decision_id=decision_id)
    clock.set(BASE + timedelta(milliseconds=3))
    predecision = runtime.attest_predecision_input_prefix(
        decision_id=decision_id,
        dependency_profile=profile,
    )
    assert predecision.first_dip_tape_read_id is None
    assert source.clocks.provider_event_at is not None
    intent, artifact = _valid_adaptive_entry_for_predecision(
        identity=identity,
        predecision=predecision,
        decision_id=decision_id,
        symbol="VEEE",
        as_of=source.clocks.provider_event_at,
        setup_family="first_dip_reclaim",
    )
    common_payload = {
        "decision_id": decision_id,
        "symbol": "VEEE",
        "decision_at": source.clocks.provider_event_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "input_prefix_sequence": predecision.input_prefix_sequence,
        "input_prefix_root_sha256": predecision.input_prefix_root_sha256,
        "required_read_ids": [read_id],
        "fsm_dependency_profile": profile.to_dict(),
        "predecision_attestation_sha256": predecision.attestation_sha256,
        "predecision_read_evidence_inventory_sha256": (
            predecision.read_evidence_inventory_sha256
        ),
        "predecision_continuity_evidence_inventory_sha256": (
            predecision.continuity_evidence_inventory_sha256
        ),
        "predecision_admission_handoff_sha256": (
            predecision.admission_handoff_sha256
        ),
        "adaptive_order_artifacts": [artifact.to_dict()],
    }
    sequence_before = runtime.health()["last_sequence"]

    disguised = CaptureDecisionOutput(
        decision_id=decision_id,
        symbol="VEEE",
        action=CaptureDecisionAction.ORDER_INTENT,
        fsm_state="entry_ready",
        setup_role="adaptive_entry",
        order_intents=(intent,),
    )
    disguised_at = clock.set(BASE + timedelta(milliseconds=4))
    with pytest.raises(
        CaptureContractError,
        match="adaptive first-dip setup family/output role mismatch",
    ):
        runtime.submit_input(
            producer.producer_id,
            stream=CaptureStream.FSM_DECISION,
            provider="chili_fsm",
            symbol="VEEE",
            clocks=CaptureClocks(
                market_reference_at=source.clocks.provider_event_at,
                received_at=disguised_at,
                available_at=disguised_at,
            ),
            payload={
                **common_payload,
                "decision_output": disguised.to_dict(),
                "decision_output_sha256": disguised.decision_output_sha256,
            },
            recorded_at=disguised_at,
            predecision_attestation=predecision,
        )

    correctly_labeled = replace(
        disguised,
        setup_role="first_dip_reclaim",
    )
    labeled_at = clock.set(BASE + timedelta(milliseconds=5))
    with pytest.raises(
        CaptureContractError,
        match="first-dip order decision lacks typed tape evidence",
    ):
        runtime.submit_input(
            producer.producer_id,
            stream=CaptureStream.FSM_DECISION,
            provider="chili_fsm",
            symbol="VEEE",
            clocks=CaptureClocks(
                market_reference_at=source.clocks.provider_event_at,
                received_at=labeled_at,
                available_at=labeled_at,
            ),
            payload={
                **common_payload,
                "decision_output": correctly_labeled.to_dict(),
                "decision_output_sha256": (
                    correctly_labeled.decision_output_sha256
                ),
            },
            recorded_at=labeled_at,
            predecision_attestation=predecision,
        )

    assert runtime.health()["last_sequence"] == sequence_before
    assert runtime.health()["order_intent_count"] == 0


def test_first_dip_tape_receipt_preserves_earlier_source_for_external_age_gate() -> None:
    identity = _identity()
    binding = _resource_binding()
    producer = _producer(
        identity, binding, streams=(CaptureStream.IQFEED_PRINT,)
    )
    ingress = BoundedCaptureIngress.from_resource_binding(binding)
    clock = _ManualClock(BASE)
    runtime = CaptureProducerLifecycleRuntime(
        identity=identity,
        ingress=ingress,
        resource_binding=binding,
        producers=(producer,),
        heartbeat_timeout_seconds=1,
        wall_clock=clock,
    )
    runtime.open(opened_at=BASE)
    runtime.register(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=1)),
    )
    source_at = clock.set(BASE + timedelta(milliseconds=2))
    source = runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        clocks=CaptureClocks(
            provider_event_at=source_at,
            received_at=source_at,
            available_at=source_at,
        ),
        payload={
            "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
            "symbol": "VEEE",
            "price": 5.00,
            "size": 100,
            "conditions": ["fixture-only"],
        },
        recorded_at=source_at,
    )
    returned = clock.set(BASE + timedelta(milliseconds=3))
    tape_policy = FirstDipTapePolicy(
        window_seconds=2.0,
        max_source_age_seconds=1.0,
        tick_rate_floor_pctile=0.0,
    )
    tape_query = FirstDipTapeReadQuery(
        symbol="VEEE",
        provider="iqfeed",
        event_start_exclusive=returned - timedelta(
            seconds=tape_policy.window_seconds
        ),
        event_end_inclusive=returned,
        decision_at=returned,
        available_at_most=returned,
        source_frontier_sequence=source.sequence,
        policy_sha256=tape_policy.policy_sha256,
    )
    receipt = CaptureReadReceipt(
        read_id=str(uuid.UUID(int=9021)),
        decision_id="decision-stale-tape",
        identity_sha256=identity.identity_sha256,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        requested_at=BASE + timedelta(milliseconds=1),
        returned_at=returned,
        query_sha256=sha256_json(tape_query.to_dict()),
        source_event_sha256s=(source.event_sha256,),
        empty_result=False,
        result_sha256=captured_read_result_sha256(
            (CaptureEventRef.from_event(source),)
        ),
        query=tape_query.to_dict(),
    )
    event, evidence = runtime.submit_first_dip_tape_receipt(receipt)
    assert event.clocks.available_at == returned
    assert evidence.read_evidence.source_event_refs[0].available_at == source_at
    assert evidence.read_evidence.receipt.returned_at == returned


def test_live_first_dip_receipt_rejects_visible_future_provider_clock() -> None:
    identity = _identity()
    binding = _resource_binding()
    producer = _producer(
        identity, binding, streams=(CaptureStream.IQFEED_PRINT,)
    )
    ingress = BoundedCaptureIngress.from_resource_binding(binding)
    clock = _ManualClock(BASE)
    runtime = CaptureProducerLifecycleRuntime(
        identity=identity,
        ingress=ingress,
        resource_binding=binding,
        producers=(producer,),
        heartbeat_timeout_seconds=1,
        wall_clock=clock,
    )
    runtime.open(opened_at=BASE)
    runtime.register(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=1)),
    )
    returned = BASE + timedelta(milliseconds=3)
    clock.set(returned)
    future_clock_source = runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        clocks=CaptureClocks(
            provider_event_at=returned + timedelta(milliseconds=1),
            received_at=returned,
            available_at=returned,
        ),
        payload={
            "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
            "symbol": "VEEE",
            "price": 5.00,
            "size": 100,
            "conditions": ["fixture-future-provider-clock"],
        },
        recorded_at=returned,
    )
    policy = FirstDipTapePolicy(
        window_seconds=1.0,
        max_source_age_seconds=1.0,
        tick_rate_floor_pctile=0.0,
    )
    query = FirstDipTapeReadQuery(
        symbol="VEEE",
        provider="iqfeed",
        event_start_exclusive=returned - timedelta(seconds=1),
        event_end_inclusive=returned,
        decision_at=returned,
        available_at_most=returned,
        source_frontier_sequence=future_clock_source.sequence,
        policy_sha256=policy.policy_sha256,
    )
    receipt = CaptureReadReceipt(
        read_id=str(uuid.UUID(int=9022)),
        decision_id="decision-future-clock-tape",
        identity_sha256=identity.identity_sha256,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        requested_at=returned - timedelta(milliseconds=1),
        returned_at=returned,
        query_sha256=sha256_json(query.to_dict()),
        source_event_sha256s=(),
        empty_result=True,
        result_sha256=captured_read_result_sha256(()),
        query=query.to_dict(),
    )

    with pytest.raises(
        FirstDipTapeCoverageUnavailable,
        match="first_dip_tape_source_clock_from_future",
    ) as exc_info:
        runtime.submit_first_dip_tape_receipt(receipt)

    assert exc_info.value.coverage_gap_required is True
    assert runtime.health()["read_receipt_count"] == 0


def _capture_health_payload(
    runtime: CaptureProducerLifecycleRuntime,
    binding: CaptureResourceBinding,
) -> dict:
    return {
        "phase": "periodic",
        "state": "running",
        "identity_sha256": runtime.identity.identity_sha256,
        "resource_binding": binding.to_record(),
        "resource_hashes": binding.hashes,
        "network_fallback_allowed": False,
        "durable_sequence_next": runtime.health()["last_sequence"] + 1,
        "accepted_count": runtime.health()["last_sequence"],
        "rejected_or_reported_lost_count": 0,
        "change_key_count": 0,
        "max_change_keys": 64,
        "max_read_sources": 64,
        "hot_symbols": (),
        "ingress": {},
        "pretrigger": {},
        "hot_leases": {},
        "pressure": {},
        "writer": {},
    }


def test_periodic_health_uses_strict_typed_control_boundary() -> None:
    runtime, _ingress, _identity_row, binding, producer, clock = _runtime_fixture()
    runtime.open(opened_at=BASE)
    runtime.register(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=1)),
    )
    at = clock.set(BASE + timedelta(milliseconds=2))
    event = runtime.submit_capture_health(_capture_health_payload(runtime, binding))

    assert event.sequence == 3
    assert event.clocks.available_at == at
    assert event.provider == "chili_capture_runtime"

    invalid = _capture_health_payload(runtime, binding)
    invalid["unexpected"] = True
    with pytest.raises(CaptureContractError, match="allowlist"):
        runtime.submit_capture_health(invalid)


def test_query_and_identity_modes_do_not_require_fake_continuity_or_watermark() -> None:
    for stream in (CaptureStream.PROVIDER_OHLCV, CaptureStream.CONFIG_SNAPSHOT):
        identity = _identity()
        binding = _resource_binding()
        producer = _producer(identity, binding, streams=(stream,))
        ingress = BoundedCaptureIngress.from_resource_binding(binding)
        clock = _ManualClock(BASE)
        runtime = CaptureProducerLifecycleRuntime(
            identity=identity,
            ingress=ingress,
            resource_binding=binding,
            producers=(producer,),
            heartbeat_timeout_seconds=1,
            wall_clock=clock,
        )
        runtime.open(opened_at=BASE)
        runtime.register(
            producer.producer_id,
            recorded_at=clock.set(BASE + timedelta(milliseconds=1)),
        )
        observed = clock.set(BASE + timedelta(milliseconds=2))
        query = {"ticker": "VEEE", "interval": "1m"}
        source = runtime.submit_input(
            producer.producer_id,
            stream=stream,
            provider="massive" if stream is CaptureStream.PROVIDER_OHLCV else "chili",
            symbol="VEEE" if stream is CaptureStream.PROVIDER_OHLCV else None,
            query=query if stream is CaptureStream.PROVIDER_OHLCV else None,
            clocks=CaptureClocks(
                market_reference_at=(
                    observed if stream is CaptureStream.PROVIDER_OHLCV else None
                ),
                received_at=observed,
                available_at=observed,
            ),
            payload={"value": 1},
            recorded_at=observed,
        )
        receipt_count = 0
        if stream is CaptureStream.PROVIDER_OHLCV:
            receipt = CaptureReadReceipt(
                read_id=str(uuid.uuid4()),
                decision_id="decision-query-mode",
                identity_sha256=identity.identity_sha256,
                stream=stream,
                provider="massive",
                symbol="VEEE",
                requested_at=BASE + timedelta(milliseconds=1),
                returned_at=observed,
                query_sha256=sha256_json(query),
                source_event_sha256s=(source.event_sha256,),
                empty_result=False,
                result_sha256=captured_read_result_sha256(
                    (CaptureEventRef.from_event(source),)
                ),
            )
            runtime.submit_read_receipt(receipt)
            receipt_count = 1
        coverage = StreamCoverage(
            stream=stream,
            identity_sha256=identity.identity_sha256,
            provider=("massive" if stream is CaptureStream.PROVIDER_OHLCV else "chili"),
            symbol="VEEE" if stream is CaptureStream.PROVIDER_OHLCV else None,
            first_available_at=observed,
            last_available_at=observed,
            event_count=1,
            exact_event_clock_complete=True,
            content_verified=True,
            continuity_complete=False,
            watermark=None,
            query_receipt_count=receipt_count,
        )
        evidence = runtime.submit_stream_coverage(
            producer.producer_id,
            coverage,
            recorded_at=clock.set(BASE + timedelta(milliseconds=3)),
        )
        assert len(evidence) == 1


def test_empty_query_receipt_creates_scorable_zero_event_coverage() -> None:
    identity = _identity()
    binding = _resource_binding()
    producer = _producer(
        identity, binding, streams=(CaptureStream.ORTEX_SNAPSHOT,)
    )
    ingress = BoundedCaptureIngress.from_resource_binding(binding)
    clock = _ManualClock(BASE)
    runtime = CaptureProducerLifecycleRuntime(
        identity=identity,
        ingress=ingress,
        resource_binding=binding,
        producers=(producer,),
        heartbeat_timeout_seconds=1,
        wall_clock=clock,
    )
    runtime.open(opened_at=BASE)
    runtime.register(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=1)),
    )
    returned = clock.set(BASE + timedelta(milliseconds=2))
    query = {"symbol": "VEEE", "fields": ["short_interest"]}
    receipt = CaptureReadReceipt(
        read_id=str(uuid.UUID(int=9010)),
        decision_id="decision-empty-ortex",
        identity_sha256=identity.identity_sha256,
        stream=CaptureStream.ORTEX_SNAPSHOT,
        provider="ortex",
        symbol="VEEE",
        requested_at=BASE + timedelta(milliseconds=1),
        returned_at=returned,
        query_sha256=sha256_json(query),
        source_event_sha256s=(),
        empty_result=True,
        result_sha256=captured_read_result_sha256(()),
    )
    event = runtime.submit_read_receipt(receipt)
    coverage = StreamCoverage(
        stream=CaptureStream.ORTEX_SNAPSHOT,
        identity_sha256=identity.identity_sha256,
        provider="ortex",
        symbol="VEEE",
        first_available_at=returned,
        last_available_at=returned,
        event_count=0,
        exact_event_clock_complete=True,
        content_verified=True,
        continuity_complete=False,
        watermark=None,
        query_receipt_count=1,
    )
    evidence = runtime.submit_stream_coverage(
        producer.producer_id,
        coverage,
        recorded_at=clock.set(BASE + timedelta(milliseconds=3)),
    )

    assert event.stream is CaptureStream.READ_RECEIPT
    assert len(evidence) == 1
    assert evidence[0].provider == "ortex"
    assert evidence[0].symbol == "VEEE"


def test_evidence_provider_and_symbol_envelopes_are_graded_exactly() -> None:
    _grade, events, identity, binding, coverage, health, watermark = _pure_grade()
    wrong_health = replace(health, provider="wrong-provider")
    wrong_watermark = replace(watermark, symbol=None)

    grade = grade_capture_producer_lifecycle(
        identity=identity,
        events=tuple(
            wrong_health
            if event is health
            else wrong_watermark
            if event is watermark
            else event
            for event in events
        ),
        stream_coverage={CaptureStream.NBBO_QUOTE: coverage},
        coverage_health_events={CaptureStream.NBBO_QUOTE: wrong_health},
        watermark_events={CaptureStream.NBBO_QUOTE: wrong_watermark},
        resource_binding_sha256=binding.binding_sha256,
    )

    assert (
        "capture_producer_health_evidence_envelope_invalid:iqfeed_nbbo_1:nbbo_quote"
        in grade.reasons
    )
    assert (
        "capture_producer_watermark_evidence_envelope_invalid:iqfeed_nbbo_1:nbbo_quote"
        in grade.reasons
    )


def test_certifying_seal_refuses_a_writer_that_is_not_stopped_and_drained(
    tmp_path,
) -> None:
    runtime, ingress, identity, binding, _coverage_row, _evidence = _complete_runtime()
    store = ContentAddressedCaptureStore(
        tmp_path / "active-writer",
        compression_codec="zlib",
        resource_binding=binding,
    )
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=100,
        batch_bytes=binding.budget.async_queue_bytes,
        poll_seconds=0.001,
        flush_interval_seconds=0.01,
    )
    worker.start()
    try:
        with pytest.raises(CaptureContractError, match="stopped, drained"):
            runtime.seal_run(worker)
    finally:
        worker.stop(timeout_seconds=5)


def test_shared_ring_owns_sequences_and_transfers_symbol_non_destructively() -> None:
    identity = _identity()
    binding = _resource_binding()
    ring = BoundedPreTriggerRing.from_resource_binding(
        binding,
        horizon=timedelta(minutes=3),
        per_symbol_max_events=16,
    )
    retained = []
    for offset, symbol in ((1, "PLSM"), (2, "VEEE")):
        at = BASE + timedelta(milliseconds=offset)
        result = ring.retain_change_observation(
            identity=identity,
            stream=CaptureStream.SCANNER_SNAPSHOT,
            provider="chili_scanner",
            symbol=symbol,
            change_key=f"scanner-rank:{symbol}",
            query={"include_otc": False, "max_age_seconds": 300.0},
            clocks=CaptureClocks(
                market_reference_at=at,
                received_at=at,
                available_at=at,
            ),
            payload={"rank": offset},
        )
        assert result.retained is True
        assert result.event is not None
        retained.append(result.event)
    assert [event.sequence for event in retained] == [1, 2]

    promoted_at = BASE + timedelta(milliseconds=3)
    transfer = ring.begin_promotion("VEEE", promoted_at=promoted_at)
    assert ring.event_count == 2
    assert transfer.events == (retained[1],)
    assert transfer.source_identity_sha256 == identity.identity_sha256
    assert transfer.resource_binding_sha256 == binding.binding_sha256
    assert ring.health()["reserved_symbols"] == ("VEEE",)
    with pytest.raises(CaptureContractError, match="pending promotion"):
        ring.begin_promotion("VEEE", promoted_at=promoted_at)

    assert ring.abort_promotion(transfer) is True
    assert ring.event_count == 2
    retry = ring.begin_promotion("VEEE", promoted_at=promoted_at)
    assert retry.promotion_id == transfer.promotion_id
    batch = ring.commit_promotion(retry)
    assert ring.event_count == 1
    assert batch.events == (retained[1],)
    assert batch.promotion_id == retry.promotion_id
    assert batch.inventory_sha256 == retry.inventory_sha256
    assert ring.abort_promotion(retry) is False


def test_shared_multi_identity_admission_enforces_one_aggregate_budget() -> None:
    binding = _resource_binding()
    shared = SharedCaptureAdmissionBudget(
        resource_binding=binding,
        max_events=2,
        max_bytes=binding.budget.async_queue_bytes,
        sustained_write_budget_bytes_per_second=(
            binding.budget.sustained_write_budget_bytes_per_second
        ),
    )
    ingress_a = BoundedCaptureIngress.from_resource_binding(
        binding, shared_admission_budget=shared
    )
    ingress_b = BoundedCaptureIngress.from_resource_binding(
        binding, shared_admission_budget=shared
    )
    identity_a = _identity()
    identity_b = replace(
        identity_a,
        run_id=str(uuid.UUID(int=9040)),
        generation=identity_a.generation + 1,
    )

    def event(identity: CaptureRunIdentity, sequence: int, symbol: str) -> CaptureEvent:
        at = BASE + timedelta(milliseconds=sequence)
        return CaptureEvent(
            identity=identity,
            sequence=sequence,
            stream=CaptureStream.CONFIG_SNAPSHOT,
            provider="chili",
            symbol=symbol,
            clocks=CaptureClocks(received_at=at, available_at=at),
            payload={"sequence": sequence, "symbol": symbol},
        )

    first = event(identity_a, 1, "PLSM")
    second = event(identity_b, 1, "VEEE")
    rejected = event(identity_a, 2, "PLSM")
    assert ingress_a.shared_admission_budget is shared
    assert ingress_b.shared_admission_budget is shared
    assert ingress_a.submit(first) is True
    assert ingress_b.submit(second) is True
    assert ingress_a.submit(rejected) is False
    health = shared.health()
    assert health["outstanding_events"] == 2
    assert set(health["events_by_identity"]) == {
        identity_a.identity_sha256,
        identity_b.identity_sha256,
    }

    batch_a = ingress_a.pop_batch(
        max_events=10,
        max_bytes=binding.budget.async_queue_bytes,
        timeout_seconds=0,
    )
    batch_b = ingress_b.pop_batch(
        max_events=10,
        max_bytes=binding.budget.async_queue_bytes,
        timeout_seconds=0,
    )
    assert any(
        gap.reason == "shared_capture_queue_overflow"
        for _identity, gap in batch_a.gaps
    )
    # Popping for a writer does not free aggregate in-flight capacity.
    assert shared.health()["outstanding_events"] == 2
    ingress_a.complete_shared_admission(batch_a.events)
    ingress_b.complete_shared_admission(batch_b.events)
    assert shared.health()["outstanding_events"] == 0
    assert shared.health()["completed"] == 2


def test_writer_completion_releases_shared_admission_reservation(tmp_path) -> None:
    identity = _identity()
    binding = _resource_binding()
    shared = SharedCaptureAdmissionBudget.from_resource_binding(binding)
    ingress = BoundedCaptureIngress.from_resource_binding(
        binding, shared_admission_budget=shared
    )
    event_at = BASE + timedelta(milliseconds=1)
    assert ingress.submit(
        CaptureEvent(
            identity=identity,
            sequence=1,
            stream=CaptureStream.CONFIG_SNAPSHOT,
            provider="chili",
            clocks=CaptureClocks(received_at=event_at, available_at=event_at),
            payload={"config": "exact"},
        )
    )
    store = ContentAddressedCaptureStore(
        tmp_path / "shared-writer",
        compression_codec="zlib",
        resource_binding=binding,
    )
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=10,
        batch_bytes=binding.budget.async_queue_bytes,
        poll_seconds=0.001,
        flush_interval_seconds=0.01,
    )
    worker.start()
    assert worker.stop(timeout_seconds=5)
    assert shared.health()["outstanding_events"] == 0
    assert shared.health()["completed"] == 1


def test_shared_store_two_leases_share_quota_and_release_independently(
    tmp_path,
) -> None:
    binding = _resource_binding()
    shared = SharedCaptureAdmissionBudget.from_resource_binding(binding)
    manager = SharedCaptureStoreRuntime.create(
        tmp_path / "shared-manager",
        resource_binding=binding,
        shared_admission_budget=shared,
        compression_codec="zlib",
    )
    identity_a = _identity()
    identity_b = replace(
        identity_a,
        run_id=str(uuid.UUID(int=9501)),
        generation=identity_a.generation + 1,
    )
    ingress_a = BoundedCaptureIngress.from_resource_binding(
        binding, shared_admission_budget=shared
    )
    ingress_b = BoundedCaptureIngress.from_resource_binding(
        binding, shared_admission_budget=shared
    )
    lease_a = manager.acquire(identity_a)
    lease_b = manager.acquire(identity_b)
    writer_a = lease_a.build_writer(
        ingress=ingress_a,
        batch_events=10,
        batch_bytes=binding.budget.async_queue_bytes,
        poll_seconds=0.001,
        flush_interval_seconds=0.01,
    )
    # Exercise cross-run ingress ownership while lease_b is still unused.  A
    # second build after lease_b already owns writer_b would only prove the
    # lease's one-shot guard and would never reach the ownership boundary.
    with pytest.raises(CaptureContractError, match="already belongs"):
        lease_b.build_writer(
            ingress=ingress_a,
            batch_events=10,
            batch_bytes=binding.budget.async_queue_bytes,
        )
    writer_b = lease_b.build_writer(
        ingress=ingress_b,
        batch_events=10,
        batch_bytes=binding.budget.async_queue_bytes,
        poll_seconds=0.001,
        flush_interval_seconds=0.01,
    )

    def submit_one(
        ingress: BoundedCaptureIngress,
        identity: CaptureRunIdentity,
        symbol: str,
    ) -> None:
        at = BASE + timedelta(milliseconds=1)
        assert ingress.submit(
            CaptureEvent(
                identity=identity,
                sequence=1,
                stream=CaptureStream.CONFIG_SNAPSHOT,
                provider="chili",
                symbol=symbol,
                clocks=CaptureClocks(received_at=at, available_at=at),
                payload={"symbol": symbol},
            )
        )

    submit_one(ingress_a, identity_a, "PLSM")
    submit_one(ingress_b, identity_b, "VEEE")
    with pytest.raises(CaptureContractError, match="active or reserved writer work"):
        lease_a.release()
    writer_a.start()
    writer_b.start()
    assert writer_a.stop(timeout_seconds=5)
    assert writer_b.stop(timeout_seconds=5)
    assert shared.health()["completed"] == 2
    assert shared.health()["outstanding_events"] == 0
    lease_a.release()
    assert lease_b.store is manager.store
    assert manager.health()["lease_count"] == 1
    lease_b.release()
    assert manager.health()["claimed_writer_ingresses"] == 0
    manager.close()


def test_writer_failure_releases_shared_reservations_and_other_run_survives(
    tmp_path,
    monkeypatch,
) -> None:
    binding = _resource_binding()
    shared = SharedCaptureAdmissionBudget.from_resource_binding(binding)
    manager = SharedCaptureStoreRuntime.create(
        tmp_path / "shared-failure-manager",
        resource_binding=binding,
        shared_admission_budget=shared,
        compression_codec="zlib",
    )
    identity_a = _identity()
    identity_b = replace(
        identity_a,
        run_id=str(uuid.UUID(int=9502)),
        generation=identity_a.generation + 1,
    )
    ingress_a = BoundedCaptureIngress.from_resource_binding(
        binding, shared_admission_budget=shared
    )
    lease_a = manager.acquire(identity_a)
    writer_a = lease_a.build_writer(
        ingress=ingress_a,
        batch_events=10,
        batch_bytes=binding.budget.async_queue_bytes,
        poll_seconds=0.001,
        flush_interval_seconds=0.001,
    )
    event_at = BASE + timedelta(milliseconds=1)
    assert ingress_a.submit(
        CaptureEvent(
            identity=identity_a,
            sequence=1,
            stream=CaptureStream.CONFIG_SNAPSHOT,
            provider="chili",
            clocks=CaptureClocks(received_at=event_at, available_at=event_at),
            payload={"run": "failure"},
        )
    )
    original_write_events = manager.store.write_events

    def fail_write(_events):
        raise OSError("fixture durable write failure")

    monkeypatch.setattr(manager.store, "write_events", fail_write)
    writer_a.start()
    assert writer_a.stop(timeout_seconds=5) is False
    assert "fixture durable write failure" in str(writer_a.health()["last_error"])
    assert shared.health()["outstanding_events"] == 0
    assert shared.health()["failed"] == 1
    lease_a.release()

    monkeypatch.setattr(manager.store, "write_events", original_write_events)
    ingress_b = BoundedCaptureIngress.from_resource_binding(
        binding, shared_admission_budget=shared
    )
    lease_b = manager.acquire(identity_b)
    writer_b = lease_b.build_writer(
        ingress=ingress_b,
        batch_events=10,
        batch_bytes=binding.budget.async_queue_bytes,
        poll_seconds=0.001,
        flush_interval_seconds=0.001,
    )
    event_b_at = BASE + timedelta(milliseconds=2)
    assert ingress_b.submit(
        CaptureEvent(
            identity=identity_b,
            sequence=1,
            stream=CaptureStream.CONFIG_SNAPSHOT,
            provider="chili",
            clocks=CaptureClocks(
                received_at=event_b_at,
                available_at=event_b_at,
            ),
            payload={"run": "survivor"},
        )
    )
    writer_b.start()
    assert writer_b.stop(timeout_seconds=5)
    assert shared.health()["completed"] == 1
    assert shared.health()["outstanding_events"] == 0
    lease_b.release()
    manager.close()
