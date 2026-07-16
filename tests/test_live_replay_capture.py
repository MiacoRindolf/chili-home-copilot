from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import uuid

import pandas as pd
import pytest

from app.services.trading.momentum_neural import first_dip_tape_decision
from app.services.trading.momentum_neural.adaptive_risk_policy import (
    RiskInputEvidence,
)
from app.services.trading.momentum_neural.adaptive_risk_request_builder import (
    AdaptiveRiskBuilderSource,
    adaptive_risk_capture_binding_from_active_attestation,
    build_adaptive_risk_request,
    runtime_adaptive_risk_capture_material,
)
from app.services.trading.momentum_neural.first_dip_tape_policy import (
    FirstDipTapePolicy,
    FirstDipTapeReadQuery,
    evaluate_first_dip_tape,
    first_dip_tape_window_from_capture,
)
from app.services.trading.momentum_neural.live_replay_capture import (
    ChangeCaptureResult,
    CaptureIdentityEvidence,
    CaptureSessionState,
    CapturedReadResult,
    FirstDipFinalCaptureRead,
    LiveFirstDipAdaptiveCaptureBridge,
    LiveMicrostructureCaptureBridge,
    LiveOhlcvCaptureBridge,
    LiveCaptureRunConfiguration,
    LiveCaptureRunInputs,
    LiveReplayCaptureCoordinator,
    LiveReplayCaptureProcessService,
    LiveReplayCaptureSupervisor,
    LiveScannerSnapshotCaptureBridge,
    ObservedCaptureInput,
    SharedStoreLiveCaptureRunFactory,
    build_executed_capture_read_inventory,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureClocks,
    CaptureBrokerOrderLifecycle,
    CaptureBrokerTransition,
    CaptureContractError,
    CaptureDecisionAction,
    CaptureDecisionOutput,
    CaptureEventRef,
    CaptureMicrostructureOperation,
    CaptureMicrostructureReadQuery,
    CaptureOrderIntent,
    CaptureOrderIntentRole,
    CaptureProducerSpec,
    CaptureRunIdentity,
    CaptureScannerProfile,
    CaptureScannerSnapshot,
    CaptureStream,
    CoverageGap,
    FSMDependencyProfile,
    FSMStreamDependency,
    IQFEED_EXACT_PRINT_SOURCE_PROVENANCE_SCHEMA_VERSION,
    IQFEED_L1_SOURCE_PROVENANCE_FIELD,
    IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
    NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION,
    capture_prefix_root_sha256,
    sha256_json,
    verify_active_capture_prefix_attestation,
)
from app.services.trading.momentum_neural.replay_capture_runtime import (
    CaptureAdaptivePressureController,
    CaptureBudgetPolicy,
    CapturePressureSample,
    CaptureResourceBinding,
    CaptureResourceMeasurement,
    SharedCaptureAdmissionBudget,
    SharedCaptureStoreRuntime,
)
from app.services.trading.momentum_neural.replay_errors import (
    ReplayMicrostructureInputUnavailableError,
)
from tests.test_adaptive_risk_reservation import _inputs, _request, _snapshot


UTC = timezone.utc
BASE = datetime(2026, 7, 14, 20, 0, tzinfo=UTC)


class _WallClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, value: datetime) -> None:
        self.now = value


def _binding(
    *,
    calibrated_hot_symbol_bytes: int = 250_000,
    max_ring_events: int = 64,
) -> CaptureResourceBinding:
    measurement = CaptureResourceMeasurement(
        measured_at=BASE,
        sample_seconds=10,
        total_memory_bytes=100_000_000,
        available_memory_bytes=60_000_000,
        disk_free_bytes=1_000_000_000,
        average_cpu_percent=20,
        sustained_append_bytes_per_second=4_000_000,
        fsync_p95_milliseconds=4,
        logical_cpu_count=8,
        host_fingerprint_sha256="e" * 64,
    )
    policy = CaptureBudgetPolicy(
        memory_reserve_bytes=10_000_000,
        disk_reserve_bytes=100_000_000,
        capture_fraction_of_memory_headroom=0.50,
        ring_fraction_of_capture_memory=0.25,
        queue_fraction_of_capture_memory=0.25,
        capture_fraction_of_disk_headroom=0.10,
        capture_fraction_of_measured_write_bandwidth=0.50,
        max_average_cpu_percent=80,
        capture_fraction_of_cpu_headroom=0.90,
        calibrated_hot_symbol_bytes=calibrated_hot_symbol_bytes,
        max_queue_events=128,
        max_ring_events=max_ring_events,
        max_gap_keys=16,
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
        pressure_enter_samples=2,
        pressure_recovery_samples=2,
        pressure_sample_max_age_seconds=120,
        store_owner_lease_seconds=60,
        store_owner_heartbeat_seconds=10,
    )
    return CaptureResourceBinding.resolve(measurement, policy)


def _identity_and_evidence(
    certification_symbol: str = "VEEE",
) -> tuple[CaptureRunIdentity, CaptureIdentityEvidence]:
    code = {"git_commit": "repair-fixture", "dirty": True}
    config = {
        "risk_budget_equity_fraction": 0.01,
        "paper_execution": False,
        "capture_certification_symbol": certification_symbol,
    }
    features = {"first_dip_reclaim": False, "replay_capture": True}
    account_identity = {
        "broker": "alpaca",
        "environment": "paper",
        "account_id": "fixture-paper-account",
    }
    identity = CaptureRunIdentity(
        run_id=str(uuid.uuid4()),
        generation=1,
        code_build_sha256=sha256_json(code),
        config_sha256=sha256_json(config),
        feature_flags_sha256=sha256_json(features),
        account_identity_sha256=sha256_json(account_identity),
        broker="alpaca",
        broker_environment="paper",
    )
    evidence = CaptureIdentityEvidence(
        code_build=code,
        config=config,
        feature_flags=features,
        account_identity=account_identity,
        account_risk_snapshot={
            "equity": "71868.33",
            "buying_power": "287473.32",
            "portfolio_heat_r": "0",
        },
        account_query={"operation": "get_account", "environment": "paper"},
        account_provider="alpaca",
    )
    return identity, evidence


def _coordinator(
    root: Path,
    *,
    per_symbol_pretrigger_events: int = 8,
    extra_streams: tuple[CaptureStream, ...] = (),
    certification_symbol: str = "VEEE",
    binding: CaptureResourceBinding | None = None,
    controller: CaptureAdaptivePressureController | None = None,
    wall_clock: _WallClock | None = None,
    shared_admission_budget=None,
    start: bool = True,
) -> tuple[LiveReplayCaptureCoordinator, tuple, _WallClock]:
    binding = binding or _binding()
    if controller is None:
        controller = CaptureAdaptivePressureController(binding)
        controller.observe(
            CapturePressureSample(
                observed_at=BASE + timedelta(seconds=1),
                resource_binding_sha256=binding.binding_sha256,
                cpu_percent=20,
                available_memory_bytes=50_000_000,
                disk_free_bytes=900_000_000,
                write_latency_milliseconds=5,
            )
        )
    identity, evidence = _identity_and_evidence(certification_symbol)
    producer = CaptureProducerSpec(
        producer_id="live_fsm",
        instance_id=str(uuid.uuid4()),
        generation=identity.generation,
        streams=tuple(
            sorted(
                {
                    CaptureStream.CODE_BUILD,
                    CaptureStream.CONFIG_SNAPSHOT,
                    CaptureStream.FEATURE_FLAG_SNAPSHOT,
                    CaptureStream.ACCOUNT_RISK_SNAPSHOT,
                    *extra_streams,
                },
                key=lambda stream: stream.value,
            )
        ),
        code_build_sha256=identity.code_build_sha256,
        config_sha256=identity.config_sha256,
        feature_flags_sha256=identity.feature_flags_sha256,
        resource_binding_sha256=binding.binding_sha256,
    )
    wall_clock = wall_clock or _WallClock(BASE + timedelta(seconds=2))
    coordinator = LiveReplayCaptureCoordinator.create(
        root,
        identity=identity,
        certification_symbol=certification_symbol,
        resource_binding=binding,
        pressure_controller=controller,
        producers=(producer,),
        heartbeat_timeout_seconds=300,
        wall_clock=wall_clock,
        pretrigger_horizon=timedelta(minutes=3),
        per_symbol_pretrigger_events=per_symbol_pretrigger_events,
        writer_batch_events=16,
        writer_batch_bytes=128 * 1024,
        writer_poll_seconds=0.01,
        writer_flush_interval_seconds=0.02,
        shared_admission_budget=shared_admission_budget,
        compression_codec="zlib",
        compression_level=3,
    )
    startup = coordinator.start(evidence) if start else ()
    return coordinator, startup, wall_clock


def _quote_clocks(index: int, *, available: datetime | None = None) -> CaptureClocks:
    available = available or BASE + timedelta(seconds=3, milliseconds=index)
    return CaptureClocks(
        provider_event_at=available - timedelta(milliseconds=2),
        received_at=available - timedelta(milliseconds=1),
        available_at=available,
    )


def _epoch_ns(value: datetime) -> int:
    utc = value.astimezone(UTC)
    return int(utc.timestamp()) * 1_000_000_000 + utc.microsecond * 1_000


def _iqfeed_exact_print_observation(
    *,
    binding: CaptureResourceBinding,
    bridge_run_id: str,
    generation: int,
    frame_sequence: int,
    available_at: datetime,
) -> tuple[CaptureClocks, dict]:
    provider_at = available_at - timedelta(milliseconds=3)
    received_at = available_at - timedelta(milliseconds=1)
    clocks = CaptureClocks(
        provider_event_at=provider_at,
        received_at=received_at,
        available_at=available_at,
    )
    selected_fields = [
        "Symbol",
        "Most Recent Trade",
        "Most Recent Trade Size",
        "Most Recent Trade Time",
        "Most Recent Trade Date",
        "Most Recent Trade Market Center",
        "Most Recent Trade Conditions",
        "TickID",
        "Bid",
        "Ask",
        "Message Contents",
    ]
    bridge_configuration = {"selected_fields": selected_fields}
    handoff_configuration = {"max_pending_events": 32}
    provenance = {
        "schema_version": IQFEED_EXACT_PRINT_SOURCE_PROVENANCE_SCHEMA_VERSION,
        "symbol": "VEEE",
        "bridge_run_id": bridge_run_id,
        "connection_generation": generation,
        "bridge_version": "iqfeed-test-bridge-v1",
        "bridge_source_sha256": "a" * 64,
        "bridge_configuration": bridge_configuration,
        "bridge_configuration_sha256": sha256_json(bridge_configuration),
        "capture_resource_binding_sha256": binding.binding_sha256,
        "handoff_configuration": handoff_configuration,
        "handoff_configuration_sha256": sha256_json(handoff_configuration),
        "message_type": "Q",
        "timestamp_basis": "iqfeed_selected_trade_date_timems_exact",
        "provider_event_at": provider_at.isoformat().replace("+00:00", "Z"),
        "received_at": received_at.isoformat().replace("+00:00", "Z"),
        "provider_trade_date": "2026-07-14",
        "provider_trade_time": "16:00:03.097",
        "provider_tick_id": str(10_000 + frame_sequence),
        "trade_market_center": "N",
        "trade_conditions": ["@"],
        "message_contents": "Cba",
        "selected_update_fields": selected_fields,
        "selected_update_fields_sha256": sha256_json(selected_fields),
        "selected_update_fields_ack_sha256": "b" * 64,
        "source_frame_sequence": frame_sequence,
        "source_frame_sha256": "c" * 64,
    }
    return clocks, {
        "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
        "symbol": "VEEE",
        "price": 4.12,
        "size": 100.0,
        "bid": 4.11,
        "ask": 4.13,
        "conditions": ["@"],
        IQFEED_L1_SOURCE_PROVENANCE_FIELD: provenance,
    }


def _dependency_profile(
    *,
    stream: CaptureStream,
    read_id: str,
    coverage_start_at: datetime,
    max_source_age_seconds: float = 5.0,
) -> FSMDependencyProfile:
    policy = {
        CaptureStream.IQFEED_PRINT: (True, False),
        CaptureStream.NBBO_QUOTE: (True, False),
        CaptureStream.ORTEX_SNAPSHOT: (False, True),
    }
    exact, reference = policy[stream]
    return FSMDependencyProfile(
        required_streams=frozenset({stream}),
        required_read_ids=(read_id,),
        stream_dependencies=(
            FSMStreamDependency(
                stream=stream,
                exact_provider_event_at_required=exact,
                market_reference_at_required=reference,
                max_source_age_seconds=max_source_age_seconds,
                coverage_start_at=coverage_start_at,
            ),
        ),
    )


def test_current_state_inventory_re_receipts_exact_startup_identity_without_fetch(
    tmp_path: Path,
) -> None:
    coordinator, startup, wall_clock = _coordinator(
        tmp_path / "current-state-identity"
    )
    try:
        source = next(
            event for event in startup if event.stream is CaptureStream.CONFIG_SNAPSHOT
        )
        wall_clock.set(BASE + timedelta(seconds=20))
        read = coordinator.capture_latest_durable_state_read(
            decision_id="decision-current-config",
            stream=CaptureStream.CONFIG_SNAPSHOT,
            symbol=None,
            returned_at=wall_clock.now,
            max_source_age_seconds=1,
        )

        assert read.durable and read.receipt is not None
        assert read.receipt.decision_id == "decision-current-config"
        assert read.receipt.stream is CaptureStream.CONFIG_SNAPSHOT
        assert read.source_events == (source,)
        assert read.receipt.replay_network_fallback_used is False
    finally:
        coordinator.abort(reason="current_state_identity_test_complete")


def test_missing_current_state_is_fail_closed_without_fabricated_event(
    tmp_path: Path,
) -> None:
    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / "current-state-missing",
        extra_streams=(CaptureStream.HALT_LULD_STATE,),
    )
    try:
        before = coordinator._prefix_rows
        with pytest.raises(
            CaptureContractError,
            match="current_state_halt_luld_state_unavailable",
        ):
            coordinator.capture_latest_durable_state_read(
                decision_id="decision-missing-halt",
                stream=CaptureStream.HALT_LULD_STATE,
                symbol="VEEE",
                returned_at=wall_clock.now,
                max_source_age_seconds=2,
            )
        assert coordinator._prefix_rows == before
    finally:
        coordinator.abort(reason="current_state_missing_test_complete")


def test_stale_current_scanner_state_is_decision_local_unavailable(
    tmp_path: Path,
) -> None:
    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / "current-state-stale-scanner",
        extra_streams=(CaptureStream.SCANNER_SNAPSHOT,),
    )
    observed_at = BASE + timedelta(seconds=2)
    try:
        captured = coordinator.record_change(
            stream=CaptureStream.SCANNER_SNAPSHOT,
            provider="massive_rest_scanner",
            symbol="VEEE",
            payload={"price": 4.2, "change_pct": 31.0},
            query={"include_otc": False, "max_age_seconds": 300.0},
            clocks=CaptureClocks(
                market_reference_at=observed_at,
                received_at=observed_at,
                available_at=observed_at,
            ),
            broad=False,
        )
        assert captured.changed and captured.current_event is not None
        before = coordinator._prefix_rows
        wall_clock.set(BASE + timedelta(seconds=20))

        with pytest.raises(
            CaptureContractError,
            match="current_state_scanner_snapshot_stale",
        ):
            coordinator.capture_latest_durable_state_read(
                decision_id="decision-stale-scanner",
                stream=CaptureStream.SCANNER_SNAPSHOT,
                symbol="VEEE",
                returned_at=wall_clock.now,
                max_source_age_seconds=1,
            )
        assert coordinator._prefix_rows == before
    finally:
        coordinator.abort(reason="current_state_stale_test_complete")


def test_microstructure_window_receipt_inventory_is_runtime_owned(
    tmp_path: Path,
) -> None:
    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / "microstructure-runtime-owned",
        extra_streams=(CaptureStream.IQFEED_PRINT,),
    )
    binding = coordinator.resource_binding
    bridge_run_id = str(uuid.uuid4())
    submissions = []
    for index, available_at in enumerate(
        (
            BASE + timedelta(seconds=3),
            BASE + timedelta(seconds=5),
            BASE + timedelta(seconds=6),
        ),
        start=1,
    ):
        wall_clock.set(available_at)
        clocks, payload = _iqfeed_exact_print_observation(
            binding=binding,
            bridge_run_id=bridge_run_id,
            generation=1,
            frame_sequence=index,
            available_at=available_at,
        )
        submissions.append(
            coordinator.submit_exact_input(
                stream=CaptureStream.IQFEED_PRINT,
                provider="iqfeed",
                symbol="VEEE",
                clocks=clocks,
                payload=payload,
            )
        )
    assert all(row.accepted for row in submissions)

    decision_at = BASE + timedelta(seconds=7)
    wall_clock.set(decision_at)
    captured = coordinator.capture_complete_microstructure_window(
        decision_id="microstructure-decision-1",
        operation=CaptureMicrostructureOperation.TRADE_FLOW,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        requested_at=decision_at,
        returned_at=decision_at,
        event_start_exclusive=BASE + timedelta(seconds=4),
        event_end_inclusive=decision_at,
        parameters={"window_seconds": 3.0},
    )

    assert captured.durable is True
    assert captured.receipt is not None
    assert tuple(row.event_sha256 for row in captured.source_events) == (
        submissions[1].event.event_sha256,
        submissions[2].event.event_sha256,
    )
    query = CaptureMicrostructureReadQuery.from_dict(captured.receipt.query)
    assert query.operation is CaptureMicrostructureOperation.TRADE_FLOW
    assert query.source_frontier_sequence == submissions[-1].event.sequence
    assert captured.receipt.source_event_sha256s == tuple(
        row.event_sha256 for row in captured.source_events
    )


def test_microstructure_window_fails_closed_when_bounded_index_evicted_overlap(
    tmp_path: Path,
) -> None:
    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / "microstructure-eviction",
        extra_streams=(CaptureStream.IQFEED_PRINT,),
        binding=_binding(max_ring_events=4),
    )
    binding = coordinator.resource_binding
    bridge_run_id = str(uuid.uuid4())
    for index in range(1, 7):
        available_at = BASE + timedelta(seconds=2 + index)
        wall_clock.set(available_at)
        clocks, payload = _iqfeed_exact_print_observation(
            binding=binding,
            bridge_run_id=bridge_run_id,
            generation=1,
            frame_sequence=index,
            available_at=available_at,
        )
        assert coordinator.submit_exact_input(
            stream=CaptureStream.IQFEED_PRINT,
            provider="iqfeed",
            symbol="VEEE",
            clocks=clocks,
            payload=payload,
        ).accepted

    decision_at = BASE + timedelta(seconds=9)
    wall_clock.set(decision_at)
    captured = coordinator.capture_complete_microstructure_window(
        decision_id="microstructure-decision-evicted",
        operation=CaptureMicrostructureOperation.TRADE_FLOW,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        requested_at=decision_at,
        returned_at=decision_at,
        event_start_exclusive=BASE + timedelta(seconds=2),
        event_end_inclusive=decision_at,
        parameters={"window_seconds": 7.0},
    )

    assert captured.receipt is None
    assert captured.coverage_gap_recorded is True
    assert CaptureStream.IQFEED_PRINT in coordinator._gapped_streams


def test_live_microstructure_bridge_computes_from_exact_receipted_prints(
    tmp_path: Path,
) -> None:
    from app.services.trading.momentum_neural import pipeline

    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / "microstructure-bridge-print",
        extra_streams=(CaptureStream.IQFEED_PRINT,),
    )
    binding = coordinator.resource_binding
    bridge_run_id = str(uuid.uuid4())
    for index in range(1, 4):
        available_at = BASE + timedelta(seconds=2 + index)
        wall_clock.set(available_at)
        clocks, payload = _iqfeed_exact_print_observation(
            binding=binding,
            bridge_run_id=bridge_run_id,
            generation=1,
            frame_sequence=index,
            available_at=available_at,
        )
        assert coordinator.submit_exact_input(
            stream=CaptureStream.IQFEED_PRINT,
            provider="iqfeed",
            symbol="VEEE",
            clocks=clocks,
            payload=payload,
        ).accepted

    decision_at = BASE + timedelta(seconds=6)
    wall_clock.set(decision_at)
    bridge = LiveMicrostructureCaptureBridge(
        coordinator=coordinator,
        decision_id="microstructure-bridge-decision",
    )
    with bridge.install():
        result = pipeline._live_trade_flow(
            "VEEE",
            db=object(),
            as_of=decision_at,
        )

    assert result == pytest.approx(0.0)
    assert len(bridge.captured_reads) == 1
    receipt = bridge.captured_reads[0].receipt
    assert receipt is not None
    query = CaptureMicrostructureReadQuery.from_dict(receipt.query)
    assert query.operation is CaptureMicrostructureOperation.TRADE_FLOW
    assert len(receipt.source_event_sha256s) == 3


def test_live_microstructure_bridge_grades_unproven_l2_decision_locally(
    tmp_path: Path,
) -> None:
    from app.services.trading.momentum_neural import pipeline
    from app.services.trading.momentum_neural import live_runner

    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / "microstructure-bridge-l2",
        extra_streams=(CaptureStream.L2_DEPTH_CHECKPOINT,),
    )
    decision_at = BASE + timedelta(seconds=3)
    wall_clock.set(decision_at)
    bridge = LiveMicrostructureCaptureBridge(
        coordinator=coordinator,
        decision_id="microstructure-l2-unavailable",
    )
    with bridge.install(), live_runner.replay_clock(decision_at):
        assert pipeline._live_book_imbalance("VEEE", db=object()) is None
        ladder = pipeline.read_ladder_distribution(
            "VEEE",
            db=object(),
            as_of=decision_at,
        )

    assert ladder.n_snaps == 0
    assert ladder.depth_imbal is None

    assert CaptureStream.L2_DEPTH_CHECKPOINT in coordinator._gapped_streams


def test_microstructure_decision_scope_rethrows_identity_clock_corruption(
    tmp_path: Path,
) -> None:
    from app.services.trading.momentum_neural import live_runner, pipeline

    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / "microstructure-swallowed-rejection",
        extra_streams=(CaptureStream.L2_DEPTH_CHECKPOINT,),
    )
    decision_at = BASE + timedelta(seconds=3)
    wall_clock.set(decision_at)
    bridge = LiveMicrostructureCaptureBridge(
        coordinator=coordinator,
        decision_id="microstructure-swallowed-rejection",
    )

    with pytest.raises(
        ReplayMicrostructureInputUnavailableError,
        match="identity/clock mismatch",
    ):
        with bridge.install(), live_runner.replay_clock(decision_at):
            try:
                pipeline._live_book_imbalance("OTHER", db=object())
            except ReplayMicrostructureInputUnavailableError:
                # Mirrors legacy optional-feed callers that fail open.  The
                # decision scope must still reject genuine causal corruption.
                pass

    assert CaptureStream.L2_DEPTH_CHECKPOINT in coordinator._gapped_streams


def test_thread_safe_sequence_preserves_missing_exact_clock_for_coverage_grading(
    tmp_path: Path,
) -> None:
    coordinator, startup, wall_clock = _coordinator(
        tmp_path / "sequence", extra_streams=(CaptureStream.NBBO_QUOTE,)
    )
    assert [event.sequence for event in startup] == list(range(1, 9))

    invalid_at = BASE + timedelta(seconds=3)
    wall_clock.set(invalid_at)
    missing_clock = coordinator.submit_exact_input(
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="VEEE",
        clocks=CaptureClocks(received_at=invalid_at, available_at=invalid_at),
        payload={"bid": 4.10, "ask": 4.12},
    )
    assert missing_clock.accepted is True
    assert missing_clock.coverage_gap_recorded is False
    assert missing_clock.event is not None
    assert missing_clock.event.clocks.provider_event_at is None

    release_at = BASE + timedelta(seconds=4)
    wall_clock.set(release_at)

    def submit(index: int):
        return coordinator.submit_exact_input(
            stream=CaptureStream.NBBO_QUOTE,
            provider="massive",
            symbol="VEEE",
            clocks=_quote_clocks(index, available=release_at),
            payload={"bid": 4.10 + index / 1000, "ask": 4.12 + index / 1000},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = tuple(pool.map(submit, range(16)))
    sequences = sorted(row.event.sequence for row in rows if row.event)
    assert sequences == list(range(10, 26))
    assert all(row.accepted and not row.coverage_gap_recorded for row in rows)

    wall_clock.set(BASE + timedelta(seconds=9))
    coordinator.emit_provider_watermark(
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="VEEE",
        event_watermark_at=release_at,
        emitted_available_at=BASE + timedelta(seconds=9),
        bounded_lateness_seconds=6,
        max_observed_lateness_seconds=5,
        generation=1,
    )
    coverage = coordinator.build_stream_coverage(CaptureStream.NBBO_QUOTE)
    assert coverage.event_count == 17
    assert coverage.exact_event_clock_complete is False
    assert coverage.continuity_complete is True
    wall_clock.set(BASE + timedelta(seconds=10))
    handoff = coordinator.stop_and_seal()
    assert coordinator.state is CaptureSessionState.SEALED
    assert handoff.sequence_min == 1
    assert handoff.sequence_max == handoff.event_count
    assert handoff.gap_count == 0
    assert handoff.producer_lifecycle_candidate is False
    assert handoff.certification_eligible is False


def test_hot_promotion_releases_without_hindsight_and_other_symbol_cannot_contaminate(
    tmp_path: Path,
) -> None:
    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / "promotion",
        per_symbol_pretrigger_events=8,
        extra_streams=(CaptureStream.NBBO_QUOTE,),
    )
    wall_clock.set(BASE + timedelta(seconds=3, milliseconds=10))
    coordinator.record_broad_input(
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="PLSM",
        clocks=_quote_clocks(0),
        payload={"bid": 5.00, "ask": 5.02, "provider_sequence": 0},
    )
    first = coordinator.record_broad_input(
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="VEEE",
        clocks=_quote_clocks(1),
        payload={"bid": 5.10, "ask": 5.12, "provider_sequence": 1},
    )
    second = coordinator.record_broad_input(
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="VEEE",
        clocks=_quote_clocks(2),
        payload={"bid": 5.11, "ask": 5.13, "provider_sequence": 2},
    )
    assert first.disposition == "bounded_pretrigger_retained_provisional"
    assert second.disposition == "bounded_pretrigger_retained_provisional"

    wall_clock.set(BASE + timedelta(seconds=4))
    with pytest.raises(CaptureContractError, match="only its bound symbol"):
        coordinator.promote_hot_symbol("PLSM")
    promotion = coordinator.promote_hot_symbol("VEEE")
    assert promotion.hot
    assert len(promotion.promoted_submissions) == 2
    assert all(row.accepted for row in promotion.promoted_submissions)
    assert promotion.reported_gaps == ()
    assert {
        row.event.clocks.available_at
        for row in promotion.promoted_submissions
        if row.event is not None
    } == {BASE + timedelta(seconds=4)}
    assert all(
        row.event.payload["_capture_promotion"]["promoted_at"]
        == "2026-07-14T20:00:04Z"
        for row in promotion.promoted_submissions
        if row.event is not None
    )
    coverage = coordinator.build_stream_coverage(CaptureStream.NBBO_QUOTE)
    assert coverage.symbol == "VEEE"
    assert coverage.first_available_at == BASE + timedelta(seconds=4)

    wall_clock.set(BASE + timedelta(seconds=5))
    hot = coordinator.record_broad_input(
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="VEEE",
        clocks=_quote_clocks(3, available=BASE + timedelta(seconds=5)),
        payload={"bid": 5.12, "ask": 5.14, "provider_sequence": 3},
    )
    assert hot.accepted
    assert hot.disposition == "durable_ingress_accepted"
    assert coordinator.release_hot_symbol("VEEE") is True

    wall_clock.set(BASE + timedelta(seconds=6))
    watermark = coordinator.emit_provider_watermark(
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="VEEE",
        event_watermark_at=BASE + timedelta(seconds=5),
        emitted_available_at=BASE + timedelta(seconds=6),
        bounded_lateness_seconds=2,
        max_observed_lateness_seconds=1,
        generation=1,
    )
    assert watermark.symbol == "VEEE"
    wall_clock.set(BASE + timedelta(seconds=10))
    handoff = coordinator.stop_and_seal()
    assert handoff.gap_count == 0


def test_supervisor_atomically_transfers_two_symbols_under_one_global_budget(
    tmp_path: Path,
) -> None:
    binding = _binding(calibrated_hot_symbol_bytes=7_000_000)
    assert binding.budget.derived_hot_symbol_capacity == 1
    controller = CaptureAdaptivePressureController(binding)
    controller.observe(
        CapturePressureSample(
            observed_at=BASE + timedelta(seconds=1),
            resource_binding_sha256=binding.binding_sha256,
            cpu_percent=20,
            available_memory_bytes=50_000_000,
            disk_free_bytes=900_000_000,
            write_latency_milliseconds=5,
        )
    )
    clock = _WallClock(BASE + timedelta(seconds=2))
    supervisor_identity, _ = _identity_and_evidence("SUPERVISOR")
    supervisor = LiveReplayCaptureSupervisor.create(
        identity=supervisor_identity,
        resource_binding=binding,
        pressure_controller=controller,
        wall_clock=clock,
        pretrigger_horizon=timedelta(minutes=3),
        per_symbol_pretrigger_events=1,
    )

    clock.set(BASE + timedelta(seconds=3, milliseconds=10))
    for index, symbol in enumerate(("PLSM", "VEEE"), start=1):
        retained = supervisor.record_broad_input(
            stream=CaptureStream.NBBO_QUOTE,
            provider="massive",
            symbol=symbol,
            payload={
                "bid": 4.0 + index / 10,
                "ask": 4.02 + index / 10,
                "provider_sequence": index,
            },
            clocks=_quote_clocks(index),
        )
        assert retained.accepted
        assert retained.disposition == "shared_pretrigger_retained_provisional"
    replacement = supervisor.record_broad_input(
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="VEEE",
        payload={"bid": 4.3, "ask": 4.32, "provider_sequence": 3},
        clocks=_quote_clocks(3),
    )
    assert replacement.accepted

    plsm, _startup, _ = _coordinator(
        tmp_path / "supervisor-plsm",
        certification_symbol="PLSM",
        binding=binding,
        controller=controller,
        wall_clock=clock,
        shared_admission_budget=supervisor.shared_admission_budget,
        extra_streams=(CaptureStream.NBBO_QUOTE,),
        start=False,
    )
    veee, _startup, _ = _coordinator(
        tmp_path / "supervisor-veee",
        certification_symbol="VEEE",
        binding=binding,
        controller=controller,
        wall_clock=clock,
        shared_admission_budget=supervisor.shared_admission_budget,
        extra_streams=(CaptureStream.NBBO_QUOTE,),
        start=False,
    )
    supervisor.attach(plsm)
    supervisor.attach(veee)
    plsm.start(_identity_and_evidence("PLSM")[1])
    veee.start(_identity_and_evidence("VEEE")[1])

    clock.set(BASE + timedelta(seconds=4))
    plsm_promotion = supervisor.promote_hot_symbol(
        "PLSM", required_stream=CaptureStream.NBBO_QUOTE
    )
    assert plsm_promotion.hot
    assert len(plsm_promotion.promoted_submissions) == 1
    promoted_event = plsm_promotion.promoted_submissions[0].event
    assert promoted_event is not None
    assert promoted_event.clocks.available_at == BASE + timedelta(seconds=4)
    assert promoted_event.clocks.provider_event_at == BASE + timedelta(
        seconds=3, milliseconds=-1
    )
    promotion_provenance = promoted_event.payload["_capture_promotion"]
    assert promotion_provenance["source_identity_sha256"] == (
        supervisor.identity.identity_sha256
    )
    assert promotion_provenance["inventory_sha256"]

    veee_rejected = supervisor.promote_hot_symbol(
        "VEEE", required_stream=CaptureStream.NBBO_QUOTE
    )
    assert not veee_rejected.hot
    assert [gap.reason for gap in veee_rejected.reported_gaps] == [
        "hot_symbol_measured_capacity_exhausted"
    ]
    health = supervisor.health()
    assert health["active_symbols"] == ("PLSM",)
    assert health["pretrigger"]["event_count"] == 1
    assert health["shared_admission"]["resource_hashes"] == binding.hashes

    # Corrupt one side deliberately: release must validate before exposing the
    # scarce global capacity.  Restore the fixture only after proving the
    # supervisor lease remained active.
    assert plsm_promotion.lease is not None
    saved_lease = plsm._hot_by_symbol.pop("PLSM")
    with pytest.raises(CaptureContractError, match="invariant mismatch"):
        supervisor.release_hot_symbol("PLSM")
    assert supervisor.health()["active_symbols"] == ("PLSM",)
    assert supervisor.health()["hot"]["active"] == 1
    plsm._hot_by_symbol["PLSM"] = saved_lease

    assert supervisor.release_hot_symbol("PLSM") is True
    clock.set(BASE + timedelta(seconds=5))
    veee_promotion = supervisor.promote_hot_symbol(
        "VEEE", required_stream=CaptureStream.NBBO_QUOTE
    )
    assert veee_promotion.hot
    assert len(veee_promotion.promoted_submissions) == 1
    assert [gap.reason for gap in veee_promotion.reported_gaps] == [
        "pretrigger_per_symbol_capacity"
    ]
    assert veee_promotion.promoted_submissions[0].event is not None
    assert (
        veee_promotion.promoted_submissions[0].event.clocks.available_at
        == BASE + timedelta(seconds=5)
    )
    assert supervisor.release_hot_symbol("VEEE") is True

    clock.set(BASE + timedelta(seconds=9))
    plsm.emit_provider_watermark(
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="PLSM",
        event_watermark_at=BASE + timedelta(seconds=4),
        emitted_available_at=BASE + timedelta(seconds=9),
        bounded_lateness_seconds=6,
        max_observed_lateness_seconds=5,
        generation=1,
    )
    clock.set(BASE + timedelta(seconds=10))
    plsm_handoff = plsm.stop_and_seal()
    assert plsm_handoff.gap_count == 0
    veee_closed = veee.abort(reason="global_hot_budget_gap")
    assert veee_closed.certification_eligible is False


def test_supervisor_scanner_change_log_deduplicates_without_a_gap() -> None:
    binding = _binding()
    controller = CaptureAdaptivePressureController(binding)
    controller.observe(
        CapturePressureSample(
            observed_at=BASE + timedelta(seconds=1),
            resource_binding_sha256=binding.binding_sha256,
            cpu_percent=20,
            available_memory_bytes=50_000_000,
            disk_free_bytes=900_000_000,
            write_latency_milliseconds=5,
        )
    )
    clock = _WallClock(BASE + timedelta(seconds=3))
    supervisor = LiveReplayCaptureSupervisor.create(
        identity=_identity_and_evidence("SUPERVISOR")[0],
        resource_binding=binding,
        pressure_controller=controller,
        wall_clock=clock,
        pretrigger_horizon=timedelta(minutes=3),
        per_symbol_pretrigger_events=8,
    )
    market_reference = BASE + timedelta(seconds=2)

    def scanner_clocks(
        received_offset_ms: int,
        *,
        reference_offset_ms: int = 0,
    ) -> CaptureClocks:
        received = BASE + timedelta(seconds=2, milliseconds=received_offset_ms)
        return CaptureClocks(
            market_reference_at=(
                market_reference + timedelta(milliseconds=reference_offset_ms)
            ),
            received_at=received,
            available_at=received + timedelta(milliseconds=1),
        )

    first = supervisor.record_broad_change(
        stream=CaptureStream.SCANNER_SNAPSHOT,
        provider="massive_rest_scanner",
        symbol="VEEE",
        change_key="ross-profile:VEEE",
        query={"include_otc": False, "max_age_seconds": 300.0},
        clocks=scanner_clocks(1),
        payload={"price": 4.0, "change_pct": 25.0, "dollar_volume": 2_000_000},
    )
    duplicate = supervisor.record_broad_change(
        stream=CaptureStream.SCANNER_SNAPSHOT,
        provider="massive_rest_scanner",
        symbol="VEEE",
        change_key="ross-profile:VEEE",
        query={"include_otc": False, "max_age_seconds": 300.0},
        clocks=scanner_clocks(2, reference_offset_ms=1),
        payload={"price": 4.0, "change_pct": 25.0, "dollar_volume": 2_000_000},
    )
    changed = supervisor.record_broad_change(
        stream=CaptureStream.SCANNER_SNAPSHOT,
        provider="massive_rest_scanner",
        symbol="VEEE",
        change_key="ross-profile:VEEE",
        query={"include_otc": False, "max_age_seconds": 300.0},
        clocks=scanner_clocks(3),
        payload={"price": 4.2, "change_pct": 31.0, "dollar_volume": 2_500_000},
    )

    assert first.changed is True and first.submission is not None
    assert first.submission.accepted is True
    assert duplicate == ChangeCaptureResult(changed=False, submission=None)
    assert changed.changed is True and changed.submission is not None
    assert changed.submission.accepted is True
    assert supervisor.pretrigger_ring.health()["event_count"] == 2


def test_unsupervised_coordinator_broad_scanner_change_uses_change_ring(
    tmp_path: Path,
) -> None:
    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / "broad-scanner-change",
        extra_streams=(CaptureStream.SCANNER_SNAPSHOT,),
    )
    observed_at = BASE + timedelta(seconds=3)
    wall_clock.set(observed_at + timedelta(milliseconds=1))
    clocks = CaptureClocks(
        market_reference_at=observed_at,
        received_at=observed_at,
        available_at=observed_at,
    )
    try:
        first = coordinator.record_change(
            stream=CaptureStream.SCANNER_SNAPSHOT,
            provider="massive_rest_scanner",
            symbol="VEEE",
            payload={"price": 4.0, "change_pct": 25.0},
            query={"include_otc": False, "max_age_seconds": 300.0},
            clocks=clocks,
            broad=True,
        )
        duplicate = coordinator.record_change(
            stream=CaptureStream.SCANNER_SNAPSHOT,
            provider="massive_rest_scanner",
            symbol="VEEE",
            payload={"price": 4.0, "change_pct": 25.0},
            query={"include_otc": False, "max_age_seconds": 300.0},
            clocks=clocks,
            broad=True,
        )

        assert first.changed is True and first.submission is not None
        assert first.submission.accepted is True
        assert duplicate == ChangeCaptureResult(changed=False, submission=None)
    finally:
        coordinator.abort(reason="test_complete")


def test_process_service_exposes_fail_closed_live_loop_hook_sequence(
    tmp_path: Path,
) -> None:
    binding = _binding()
    controller = CaptureAdaptivePressureController(binding)
    controller.observe(
        CapturePressureSample(
            observed_at=BASE + timedelta(seconds=1),
            resource_binding_sha256=binding.binding_sha256,
            cpu_percent=20,
            available_memory_bytes=50_000_000,
            disk_free_bytes=900_000_000,
            write_latency_milliseconds=5,
        )
    )
    clock = _WallClock(BASE + timedelta(seconds=2))
    supervisor = LiveReplayCaptureSupervisor.create(
        identity=_identity_and_evidence("SUPERVISOR")[0],
        resource_binding=binding,
        pressure_controller=controller,
        wall_clock=clock,
        pretrigger_horizon=timedelta(minutes=3),
        per_symbol_pretrigger_events=8,
    )

    def run_factory(
        symbol: str,
        *,
        resource_binding,
        pressure_controller,
        shared_admission_budget,
        wall_clock,
    ):
        assert resource_binding is binding
        assert pressure_controller is controller
        assert shared_admission_budget is supervisor.shared_admission_budget
        assert wall_clock is clock
        coordinator, _startup, _clock = _coordinator(
            tmp_path / f"process-{symbol.lower()}",
            certification_symbol=symbol,
            binding=binding,
            controller=controller,
            wall_clock=clock,
            shared_admission_budget=shared_admission_budget,
            extra_streams=(CaptureStream.NBBO_QUOTE,),
            start=False,
        )
        return coordinator, _identity_and_evidence(symbol)[1]

    service = LiveReplayCaptureProcessService(
        supervisor=supervisor,
        run_factory=run_factory,
    )
    assert service.network_fallback_allowed is False
    assert service.capture_resource_binding_sha256 == binding.binding_sha256
    assert service.capture_queue_event_limit == binding.budget.max_queue_events
    assert service.capture_queue_byte_limit == binding.budget.async_queue_bytes
    assert service.capture_gap_key_limit == binding.budget.max_gap_keys
    clock.set(BASE + timedelta(seconds=3, milliseconds=10))
    service.record_broad_input(
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="VEEE",
        payload={"bid": 4.10, "ask": 4.12},
        clocks=_quote_clocks(1),
    )
    clock.set(BASE + timedelta(seconds=4))
    admission = service.admit_hot_symbol(
        "VEEE", required_stream=CaptureStream.NBBO_QUOTE
    )
    assert admission.capture_ready
    assert len(admission.startup_events) == 8
    assert len(admission.promotion.promoted_submissions) == 1
    active_config = service.config_evidence_for("VEEE")
    assert active_config == dict(_identity_and_evidence("VEEE")[1].config)
    assert service.config_sha256_for("VEEE") == admission.coordinator.identity.config_sha256

    clock.set(BASE + timedelta(seconds=5))
    hot = service.submit_hot_input(
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="VEEE",
        payload={"bid": 4.11, "ask": 4.13},
        clocks=_quote_clocks(2, available=BASE + timedelta(seconds=5)),
    )
    assert hot.accepted
    clock.set(BASE + timedelta(seconds=9))
    service.emit_provider_watermark(
        "VEEE",
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        event_watermark_at=BASE + timedelta(seconds=5),
        emitted_available_at=BASE + timedelta(seconds=9),
        bounded_lateness_seconds=5,
        max_observed_lateness_seconds=4,
        generation=1,
    )
    clock.set(BASE + timedelta(seconds=10))
    handoff = service.release_and_seal("VEEE")
    assert handoff.gap_count == 0
    assert service.health()["running_symbols"] == ()
    with pytest.raises(CaptureContractError, match="no running capture coordinator"):
        service.config_evidence_for("VEEE")


def test_supervised_iqfeed_promotion_registers_from_exact_print_and_gaps_prior_quote(
    tmp_path: Path,
) -> None:
    binding = _binding()
    controller = CaptureAdaptivePressureController(binding)
    controller.observe(
        CapturePressureSample(
            observed_at=BASE + timedelta(seconds=1),
            resource_binding_sha256=binding.binding_sha256,
            cpu_percent=20,
            available_memory_bytes=50_000_000,
            disk_free_bytes=900_000_000,
            write_latency_milliseconds=5,
        )
    )
    clock = _WallClock(BASE + timedelta(seconds=2))
    supervisor = LiveReplayCaptureSupervisor.create(
        identity=_identity_and_evidence("SUPERVISOR")[0],
        resource_binding=binding,
        pressure_controller=controller,
        wall_clock=clock,
        pretrigger_horizon=timedelta(minutes=3),
        per_symbol_pretrigger_events=8,
    )
    bridge_run_id = "bbfa580c-a467-4ab9-9803-8cf5c2303658"
    bridge_generation = 7

    def run_factory(
        symbol: str,
        *,
        resource_binding,
        pressure_controller,
        shared_admission_budget,
        wall_clock,
    ):
        identity, evidence = _identity_and_evidence(symbol)
        coordinator_producer = CaptureProducerSpec(
            producer_id="live_fsm",
            instance_id=str(uuid.uuid4()),
            generation=identity.generation,
            streams=(
                CaptureStream.CODE_BUILD,
                CaptureStream.CONFIG_SNAPSHOT,
                CaptureStream.FEATURE_FLAG_SNAPSHOT,
                CaptureStream.ACCOUNT_RISK_SNAPSHOT,
            ),
            code_build_sha256=identity.code_build_sha256,
            config_sha256=identity.config_sha256,
            feature_flags_sha256=identity.feature_flags_sha256,
            resource_binding_sha256=resource_binding.binding_sha256,
        )
        iqfeed_producer = CaptureProducerSpec(
            producer_id="iqfeed_l1",
            instance_id=bridge_run_id,
            generation=bridge_generation,
            streams=(CaptureStream.IQFEED_PRINT, CaptureStream.NBBO_QUOTE),
            code_build_sha256=identity.code_build_sha256,
            config_sha256=identity.config_sha256,
            feature_flags_sha256=identity.feature_flags_sha256,
            resource_binding_sha256=resource_binding.binding_sha256,
        )
        coordinator = LiveReplayCaptureCoordinator.create(
            tmp_path / "supervised-iqfeed",
            identity=identity,
            certification_symbol=symbol,
            resource_binding=resource_binding,
            pressure_controller=pressure_controller,
            producers=(coordinator_producer, iqfeed_producer),
            heartbeat_timeout_seconds=300,
            wall_clock=wall_clock,
            pretrigger_horizon=timedelta(minutes=3),
            per_symbol_pretrigger_events=8,
            writer_batch_events=16,
            writer_batch_bytes=128 * 1024,
            writer_poll_seconds=0.01,
            writer_flush_interval_seconds=0.02,
            shared_admission_budget=shared_admission_budget,
            compression_codec="zlib",
            compression_level=3,
        )
        return coordinator, evidence

    service = LiveReplayCaptureProcessService(
        supervisor=supervisor,
        run_factory=run_factory,
    )
    quote_at = BASE + timedelta(seconds=3)
    clock.set(quote_at)
    quote = service.record_broad_input(
        stream=CaptureStream.NBBO_QUOTE,
        provider="iqfeed",
        symbol="VEEE",
        payload={
            "schema_version": NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION,
            "symbol": "VEEE",
            "bid": 4.10,
            "ask": 4.12,
        },
        clocks=CaptureClocks(
            market_reference_at=quote_at - timedelta(milliseconds=2),
            received_at=quote_at - timedelta(milliseconds=1),
            available_at=quote_at,
        ),
    )
    assert quote.accepted
    print_at = BASE + timedelta(seconds=3, milliseconds=100)
    print_clocks, print_payload = _iqfeed_exact_print_observation(
        binding=binding,
        bridge_run_id=bridge_run_id,
        generation=bridge_generation,
        frame_sequence=2,
        available_at=print_at,
    )
    clock.set(print_at)
    exact_print = service.record_broad_input(
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        payload=print_payload,
        clocks=print_clocks,
    )
    assert exact_print.accepted

    clock.set(BASE + timedelta(seconds=4))
    admission = service.admit_hot_symbol(
        "VEEE", required_stream=CaptureStream.IQFEED_PRINT
    )
    assert admission.capture_ready
    assert len(admission.promotion.promoted_submissions) == 1
    assert admission.promotion.promoted_submissions[0].accepted
    assert [gap.reason for gap in admission.promotion.reported_gaps] == [
        "pretrigger_external_registration_evidence_unavailable"
    ]
    lifecycle = admission.coordinator.health()["producer_lifecycle"]
    assert "iqfeed_l1" in lifecycle["registered_producers"]

    next_quote_at = BASE + timedelta(seconds=5)
    clock.set(next_quote_at)
    next_quote = service.record_broad_input(
        stream=CaptureStream.NBBO_QUOTE,
        provider="iqfeed",
        symbol="VEEE",
        payload={
            "schema_version": NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION,
            "symbol": "VEEE",
            "bid": 4.11,
            "ask": 4.13,
        },
        clocks=CaptureClocks(
            market_reference_at=next_quote_at - timedelta(milliseconds=2),
            received_at=next_quote_at - timedelta(milliseconds=1),
            available_at=next_quote_at,
        ),
    )
    assert next_quote.accepted
    assert next_quote.event is not None
    assert next_quote.event.provider == "iqfeed"

    service.abort_symbol("VEEE", reason="external_promotion_test_complete")


def test_process_service_routes_upstream_queue_gap_into_hot_promotion(
    tmp_path: Path,
) -> None:
    binding = _binding()
    controller = CaptureAdaptivePressureController(binding)
    controller.observe(
        CapturePressureSample(
            observed_at=BASE + timedelta(seconds=1),
            resource_binding_sha256=binding.binding_sha256,
            cpu_percent=20,
            available_memory_bytes=50_000_000,
            disk_free_bytes=900_000_000,
            write_latency_milliseconds=5,
        )
    )
    clock = _WallClock(BASE + timedelta(seconds=2))
    supervisor = LiveReplayCaptureSupervisor.create(
        identity=_identity_and_evidence("SUPERVISOR")[0],
        resource_binding=binding,
        pressure_controller=controller,
        wall_clock=clock,
        pretrigger_horizon=timedelta(minutes=3),
        per_symbol_pretrigger_events=8,
    )

    def run_factory(
        symbol: str,
        *,
        resource_binding,
        pressure_controller,
        shared_admission_budget,
        wall_clock,
    ):
        coordinator, _startup, _clock = _coordinator(
            tmp_path / f"gap-{symbol.lower()}",
            certification_symbol=symbol,
            binding=resource_binding,
            controller=pressure_controller,
            wall_clock=wall_clock,
            shared_admission_budget=shared_admission_budget,
            extra_streams=(
                CaptureStream.NBBO_QUOTE,
                CaptureStream.L2_DEPTH_DELTA,
            ),
            start=False,
        )
        return coordinator, _identity_and_evidence(symbol)[1]

    service = LiveReplayCaptureProcessService(
        supervisor=supervisor,
        run_factory=run_factory,
    )
    lost_at = BASE + timedelta(seconds=3)
    reported_active = service.record_broad_gap(
        CoverageGap(
            stream=CaptureStream.NBBO_QUOTE,
            symbol="VEEE",
            reason="iqfeed_l1_capture_queue_overflow",
            first_available_at=lost_at,
            last_available_at=lost_at,
            lost_count=2,
        )
    )
    assert reported_active == ()
    with pytest.raises(CaptureContractError, match="not globally hot"):
        service.record_hot_gap(
            CoverageGap(
                stream=CaptureStream.L2_DEPTH_DELTA,
                symbol="VEEE",
                reason="iqfeed_l2_capture_queue_overflow",
                first_available_at=lost_at,
                last_available_at=lost_at,
                lost_count=1,
            )
        )

    clock.set(BASE + timedelta(seconds=4))
    admission = service.admit_hot_symbol(
        "VEEE", required_stream=CaptureStream.NBBO_QUOTE
    )
    assert admission.capture_ready
    assert [
        (gap.reason, gap.lost_count)
        for gap in admission.promotion.reported_gaps
    ] == [("iqfeed_l1_capture_queue_overflow", 2)]
    assert service.record_hot_gap(
        CoverageGap(
            stream=CaptureStream.L2_DEPTH_DELTA,
            symbol="VEEE",
            reason="iqfeed_l2_capture_queue_overflow",
            first_available_at=BASE + timedelta(seconds=4),
            last_available_at=BASE + timedelta(seconds=4),
            lost_count=3,
        )
    )
    assert (
        admission.coordinator.health()["rejected_or_reported_lost_count"] >= 5
    )
    closed = service.abort_symbol("VEEE", reason="fixture_gap_recorded")
    assert closed.certification_eligible is False


def test_process_runs_share_one_store_quota_and_seal_independently(
    tmp_path: Path,
) -> None:
    binding = _binding()
    controller = CaptureAdaptivePressureController(binding)
    controller.observe(
        CapturePressureSample(
            observed_at=BASE + timedelta(seconds=1),
            resource_binding_sha256=binding.binding_sha256,
            cpu_percent=20,
            available_memory_bytes=50_000_000,
            disk_free_bytes=900_000_000,
            write_latency_milliseconds=5,
        )
    )
    clock = _WallClock(BASE + timedelta(seconds=2))
    shared_admission = SharedCaptureAdmissionBudget.from_resource_binding(
        binding,
        pressure_controller=controller,
    )
    supervisor = LiveReplayCaptureSupervisor.create(
        identity=_identity_and_evidence("SUPERVISOR")[0],
        resource_binding=binding,
        pressure_controller=controller,
        wall_clock=clock,
        pretrigger_horizon=timedelta(minutes=3),
        per_symbol_pretrigger_events=8,
        shared_admission_budget=shared_admission,
    )
    shared_store = SharedCaptureStoreRuntime.create(
        tmp_path / "shared-live-process",
        resource_binding=binding,
        shared_admission_budget=shared_admission,
        compression_codec="zlib",
        compression_level=3,
        wall_clock=clock,
    )

    def run_factory(
        symbol: str,
        *,
        resource_binding,
        pressure_controller,
        shared_admission_budget,
        wall_clock,
    ):
        assert resource_binding is binding
        assert pressure_controller is controller
        assert shared_admission_budget is shared_admission
        assert wall_clock is clock
        identity, evidence = _identity_and_evidence(symbol)
        producer = CaptureProducerSpec(
            producer_id="live_fsm",
            instance_id=str(uuid.uuid4()),
            generation=identity.generation,
            streams=tuple(
                sorted(
                    {
                        CaptureStream.CODE_BUILD,
                        CaptureStream.CONFIG_SNAPSHOT,
                        CaptureStream.FEATURE_FLAG_SNAPSHOT,
                        CaptureStream.ACCOUNT_RISK_SNAPSHOT,
                        CaptureStream.NBBO_QUOTE,
                    },
                    key=lambda stream: stream.value,
                )
            ),
            code_build_sha256=identity.code_build_sha256,
            config_sha256=identity.config_sha256,
            feature_flags_sha256=identity.feature_flags_sha256,
            resource_binding_sha256=binding.binding_sha256,
        )
        coordinator = LiveReplayCaptureCoordinator.create_with_shared_store(
            identity=identity,
            certification_symbol=symbol,
            resource_binding=binding,
            pressure_controller=controller,
            shared_store_runtime=shared_store,
            producers=(producer,),
            heartbeat_timeout_seconds=300,
            wall_clock=clock,
            pretrigger_horizon=timedelta(minutes=3),
            per_symbol_pretrigger_events=8,
            writer_batch_events=16,
            writer_batch_bytes=128 * 1024,
            writer_poll_seconds=0.001,
            writer_flush_interval_seconds=0.01,
        )
        return coordinator, evidence

    service = LiveReplayCaptureProcessService(
        supervisor=supervisor,
        run_factory=run_factory,
    )
    clock.set(BASE + timedelta(seconds=3, milliseconds=10))
    for index, symbol in enumerate(("PLSM", "VEEE"), start=1):
        service.record_broad_input(
            stream=CaptureStream.NBBO_QUOTE,
            provider="massive",
            symbol=symbol,
            payload={"bid": 4.10 + index / 100, "ask": 4.12 + index / 100},
            clocks=_quote_clocks(index),
        )

    clock.set(BASE + timedelta(seconds=4))
    plsm = service.admit_hot_symbol(
        "PLSM", required_stream=CaptureStream.NBBO_QUOTE
    )
    veee = service.admit_hot_symbol(
        "VEEE", required_stream=CaptureStream.NBBO_QUOTE
    )
    assert plsm.capture_ready and veee.capture_ready
    assert shared_store.health()["lease_count"] == 2
    assert plsm.coordinator.store is veee.coordinator.store is shared_store.store

    clock.set(BASE + timedelta(seconds=8))
    for symbol, event_index in (("PLSM", 1), ("VEEE", 2)):
        service.emit_provider_watermark(
            symbol,
            stream=CaptureStream.NBBO_QUOTE,
            provider="massive",
            event_watermark_at=_quote_clocks(event_index).provider_event_at,
            emitted_available_at=BASE + timedelta(seconds=8),
            bounded_lateness_seconds=5,
            max_observed_lateness_seconds=4,
            generation=1,
        )

    clock.set(BASE + timedelta(seconds=9))
    first = service.release_and_seal("PLSM")
    after_first = shared_store.health()
    assert after_first["closed"] is False
    assert after_first["lease_count"] == 1
    assert first.capture_root == shared_store.store.root

    hot_at = BASE + timedelta(seconds=10)
    clock.set(hot_at)
    hot = service.submit_hot_input(
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="VEEE",
        payload={"bid": 4.20, "ask": 4.22},
        clocks=CaptureClocks(
            provider_event_at=hot_at - timedelta(milliseconds=2),
            received_at=hot_at - timedelta(milliseconds=1),
            available_at=hot_at,
        ),
    )
    assert hot.accepted is True
    clock.set(BASE + timedelta(seconds=11))
    service.emit_provider_watermark(
        "VEEE",
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        event_watermark_at=hot_at - timedelta(milliseconds=2),
        emitted_available_at=BASE + timedelta(seconds=11),
        bounded_lateness_seconds=5,
        max_observed_lateness_seconds=4,
        generation=1,
    )
    clock.set(BASE + timedelta(seconds=12))
    second = service.release_and_seal("VEEE")

    assert second.capture_root == first.capture_root
    assert second.final_seal_sha256 != first.final_seal_sha256
    assert shared_store.health()["lease_count"] == 0
    assert service.health()["running_symbols"] == ()
    shared_store.close()
    assert shared_store.health()["closed"] is True


def test_failed_shared_run_start_releases_its_writer_lease(
    tmp_path: Path,
) -> None:
    binding = _binding()
    controller = CaptureAdaptivePressureController(binding)
    controller.observe(
        CapturePressureSample(
            observed_at=BASE + timedelta(seconds=1),
            resource_binding_sha256=binding.binding_sha256,
            cpu_percent=20,
            available_memory_bytes=50_000_000,
            disk_free_bytes=900_000_000,
            write_latency_milliseconds=5,
        )
    )
    clock = _WallClock(BASE + timedelta(seconds=2))
    shared_admission = SharedCaptureAdmissionBudget.from_resource_binding(
        binding,
        pressure_controller=controller,
    )
    supervisor = LiveReplayCaptureSupervisor.create(
        identity=_identity_and_evidence("SUPERVISOR")[0],
        resource_binding=binding,
        pressure_controller=controller,
        wall_clock=clock,
        pretrigger_horizon=timedelta(minutes=3),
        per_symbol_pretrigger_events=8,
        shared_admission_budget=shared_admission,
    )
    shared_store = SharedCaptureStoreRuntime.create(
        tmp_path / "failed-shared-start",
        resource_binding=binding,
        shared_admission_budget=shared_admission,
        compression_codec="zlib",
        wall_clock=clock,
    )

    def run_factory(symbol: str, **_kwargs):
        identity, _correct_evidence = _identity_and_evidence(symbol)
        producer = CaptureProducerSpec(
            producer_id="live_fsm",
            instance_id=str(uuid.uuid4()),
            generation=identity.generation,
            streams=tuple(
                sorted(
                    {
                        CaptureStream.CODE_BUILD,
                        CaptureStream.CONFIG_SNAPSHOT,
                        CaptureStream.FEATURE_FLAG_SNAPSHOT,
                        CaptureStream.ACCOUNT_RISK_SNAPSHOT,
                    },
                    key=lambda stream: stream.value,
                )
            ),
            code_build_sha256=identity.code_build_sha256,
            config_sha256=identity.config_sha256,
            feature_flags_sha256=identity.feature_flags_sha256,
            resource_binding_sha256=binding.binding_sha256,
        )
        coordinator = LiveReplayCaptureCoordinator.create_with_shared_store(
            identity=identity,
            certification_symbol=symbol,
            resource_binding=binding,
            pressure_controller=controller,
            shared_store_runtime=shared_store,
            producers=(producer,),
            heartbeat_timeout_seconds=300,
            wall_clock=clock,
            pretrigger_horizon=timedelta(minutes=3),
            per_symbol_pretrigger_events=8,
        )
        # Deliberately bind evidence for another symbol so validation fails
        # before the writer starts or any lifecycle event can be emitted.
        wrong_evidence = _identity_and_evidence("OTHER")[1]
        return coordinator, wrong_evidence

    service = LiveReplayCaptureProcessService(
        supervisor=supervisor,
        run_factory=run_factory,
    )

    with pytest.raises(CaptureContractError, match="config evidence"):
        service.admit_hot_symbol("VEEE")

    health = shared_store.health()
    assert health["closed"] is False
    assert health["lease_count"] == 0
    assert health["writer_threads_in_use"] == 0
    assert service.health()["running_symbols"] == ()
    shared_store.close()


def test_shared_store_run_factory_hash_binds_every_runtime_knob(
    tmp_path: Path,
) -> None:
    binding = _binding()
    controller = CaptureAdaptivePressureController(binding)
    controller.observe(
        CapturePressureSample(
            observed_at=BASE + timedelta(seconds=1),
            resource_binding_sha256=binding.binding_sha256,
            cpu_percent=20,
            available_memory_bytes=50_000_000,
            disk_free_bytes=900_000_000,
            write_latency_milliseconds=5,
        )
    )
    clock = _WallClock(BASE + timedelta(seconds=2))
    shared_admission = SharedCaptureAdmissionBudget.from_resource_binding(
        binding,
        pressure_controller=controller,
    )
    shared_store = SharedCaptureStoreRuntime.create(
        tmp_path / "hash-bound-run-factory",
        resource_binding=binding,
        shared_admission_budget=shared_admission,
        compression_codec="zlib",
        wall_clock=clock,
    )
    run_configuration = LiveCaptureRunConfiguration(
        heartbeat_timeout_seconds=300,
        pretrigger_horizon_seconds=180,
        per_symbol_pretrigger_events=8,
        writer_batch_events=16,
        writer_batch_bytes=128 * 1024,
        writer_poll_seconds=0.001,
        writer_flush_interval_seconds=0.01,
        max_change_keys=8,
        max_read_sources=8,
    )

    def inputs_for(
        symbol: str,
        *,
        resource_binding,
        run_configuration,
        capture_store_root,
    ) -> LiveCaptureRunInputs:
        assert resource_binding is binding
        assert run_configuration is run_configuration_fixture
        code = {"git_commit": "shared-factory-fixture", "dirty": True}
        config = {
            "capture_certification_symbol": symbol,
            "capture_resource_binding_sha256": binding.binding_sha256,
            "capture_store_root": str(capture_store_root.resolve()),
            "live_capture_run_configuration": run_configuration.to_dict(),
            "live_capture_run_configuration_sha256": (
                run_configuration.configuration_sha256
            ),
            "paper_execution": False,
        }
        features = {"first_dip_reclaim": False, "replay_capture": True}
        account_identity = {
            "broker": "alpaca",
            "environment": "paper",
            "account_id": "factory-paper-account",
        }
        identity = CaptureRunIdentity(
            run_id=str(uuid.uuid4()),
            generation=1,
            code_build_sha256=sha256_json(code),
            config_sha256=sha256_json(config),
            feature_flags_sha256=sha256_json(features),
            account_identity_sha256=sha256_json(account_identity),
            broker="alpaca",
            broker_environment="paper",
        )
        evidence = CaptureIdentityEvidence(
            code_build=code,
            config=config,
            feature_flags=features,
            account_identity=account_identity,
            account_risk_snapshot={
                "equity": "71868.33",
                "buying_power": "287473.32",
                "portfolio_heat_r": "0",
            },
            account_query={"operation": "get_account", "environment": "paper"},
            account_provider="alpaca",
        )
        producer = CaptureProducerSpec(
            producer_id="live_fsm",
            instance_id=str(uuid.uuid4()),
            generation=identity.generation,
            streams=tuple(
                sorted(
                    {
                        CaptureStream.CODE_BUILD,
                        CaptureStream.CONFIG_SNAPSHOT,
                        CaptureStream.FEATURE_FLAG_SNAPSHOT,
                        CaptureStream.ACCOUNT_RISK_SNAPSHOT,
                    },
                    key=lambda stream: stream.value,
                )
            ),
            code_build_sha256=identity.code_build_sha256,
            config_sha256=identity.config_sha256,
            feature_flags_sha256=identity.feature_flags_sha256,
            resource_binding_sha256=binding.binding_sha256,
        )
        return LiveCaptureRunInputs(
            identity=identity,
            evidence=evidence,
            producers=(producer,),
        )

    run_configuration_fixture = run_configuration

    def inputs_without_runtime_hash(symbol: str, **kwargs) -> LiveCaptureRunInputs:
        row = inputs_for(symbol, **kwargs)
        config = dict(row.evidence.config)
        config.pop("live_capture_run_configuration_sha256")
        config_sha256 = sha256_json(config)
        return LiveCaptureRunInputs(
            identity=replace(row.identity, config_sha256=config_sha256),
            evidence=replace(row.evidence, config=config),
            producers=(
                replace(row.producers[0], config_sha256=config_sha256),
            ),
        )

    bad_factory = SharedStoreLiveCaptureRunFactory(
        shared_store_runtime=shared_store,
        run_configuration=run_configuration,
        startup_input_provider=inputs_without_runtime_hash,
    )
    with pytest.raises(
        CaptureContractError,
        match="lacks its exact run configuration",
    ):
        bad_factory(
            "VEEE",
            resource_binding=binding,
            pressure_controller=controller,
            shared_admission_budget=shared_admission,
            wall_clock=clock,
        )
    assert shared_store.health()["lease_count"] == 0

    factory = SharedStoreLiveCaptureRunFactory(
        shared_store_runtime=shared_store,
        run_configuration=run_configuration,
        startup_input_provider=inputs_for,
    )
    coordinator, evidence = factory(
        "VEEE",
        resource_binding=binding,
        pressure_controller=controller,
        shared_admission_budget=shared_admission,
        wall_clock=clock,
    )

    assert coordinator.store is shared_store.store
    assert coordinator.writer.ingress.shared_admission_budget is shared_admission
    assert (
        evidence.config["live_capture_run_configuration_sha256"]
        == run_configuration.configuration_sha256
    )
    coordinator.start(evidence)
    clock.set(BASE + timedelta(seconds=3))
    coordinator.abort(reason="factory_test_complete")
    assert shared_store.health()["lease_count"] == 0
    shared_store.close()


def test_pretrigger_eviction_is_persisted_as_gap_and_run_remains_unsealed(
    tmp_path: Path,
) -> None:
    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / "eviction",
        per_symbol_pretrigger_events=1,
        extra_streams=(CaptureStream.NBBO_QUOTE,),
    )
    wall_clock.set(BASE + timedelta(seconds=3, milliseconds=10))
    for index in (1, 2):
        coordinator.record_broad_input(
            stream=CaptureStream.NBBO_QUOTE,
            provider="massive",
            symbol="VEEE",
            clocks=_quote_clocks(index),
            payload={"bid": 5.0 + index / 100, "ask": 5.02 + index / 100},
        )
    wall_clock.set(BASE + timedelta(seconds=4))
    promotion = coordinator.promote_hot_symbol("VEEE")
    assert [gap.reason for gap in promotion.reported_gaps] == [
        "pretrigger_per_symbol_capacity"
    ]
    wall_clock.set(BASE + timedelta(seconds=5))
    closed = coordinator.abort(reason="coverage_gap")
    assert closed.certification_eligible is False
    assert coordinator.writer.health()["lost_events_recorded"] >= 1
    assert not list((tmp_path / "eviction").glob("seals/run=*/generation=*/*.json"))


def test_query_helpers_emit_explicit_empty_and_nonempty_receipts_and_change_dedupe(
    tmp_path: Path,
) -> None:
    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / "receipts",
        extra_streams=(
            CaptureStream.ORTEX_SNAPSHOT,
            CaptureStream.PROVIDER_OHLCV,
            CaptureStream.SSR_STATE,
        ),
    )

    wall_clock.set(BASE + timedelta(seconds=3, milliseconds=20))
    empty = coordinator.capture_query_result(
        decision_id="veee-entry",
        stream=CaptureStream.ORTEX_SNAPSHOT,
        provider="ortex",
        symbol="VEEE",
        query={"symbol": "VEEE", "dataset": "short_interest"},
        requested_at=BASE + timedelta(seconds=3),
        returned_at=BASE + timedelta(seconds=3, milliseconds=10),
        results=(),
        read_id=str(uuid.UUID(int=101)),
    )
    assert empty.durable
    assert empty.receipt is not None
    assert empty.receipt.empty_result is True
    assert empty.receipt.source_event_sha256s == ()
    assert empty.receipt.replay_network_fallback_used is False
    assert empty.receipt.returned_at == BASE + timedelta(
        seconds=3, milliseconds=10
    )

    source_available = BASE + timedelta(seconds=4)
    wall_clock.set(source_available + timedelta(milliseconds=1))
    nonempty = coordinator.capture_query_result(
        decision_id="veee-entry",
        stream=CaptureStream.PROVIDER_OHLCV,
        provider="massive",
        symbol="VEEE",
        query={"symbol": "VEEE", "timespan": "minute", "multiplier": 1},
        requested_at=BASE + timedelta(seconds=3, milliseconds=500),
        returned_at=source_available + timedelta(milliseconds=1),
        results=(
            ObservedCaptureInput(
                payload={"open": 4.0, "high": 4.2, "low": 3.9, "close": 4.1},
                clocks=CaptureClocks(
                    market_reference_at=BASE + timedelta(seconds=3),
                    received_at=source_available - timedelta(milliseconds=1),
                    available_at=source_available,
                ),
            ),
        ),
        read_id=str(uuid.UUID(int=102)),
    )
    assert nonempty.durable
    assert nonempty.receipt is not None
    assert nonempty.receipt.empty_result is False
    assert nonempty.receipt.source_event_sha256s == (
        nonempty.source_events[0].event_sha256,
    )

    state_clocks = CaptureClocks(
        market_reference_at=BASE + timedelta(seconds=5),
        received_at=BASE + timedelta(seconds=5),
        available_at=BASE + timedelta(seconds=5),
    )
    wall_clock.set(BASE + timedelta(seconds=5))
    changed = coordinator.record_change(
        stream=CaptureStream.SSR_STATE,
        provider="alpaca",
        symbol="VEEE",
        payload={"ssr": False},
        clocks=state_clocks,
    )
    unchanged = coordinator.record_change(
        stream=CaptureStream.SSR_STATE,
        provider="alpaca",
        symbol="VEEE",
        payload={"ssr": False},
        clocks=state_clocks,
    )
    assert changed.changed and changed.submission and changed.submission.accepted
    assert unchanged == type(unchanged)(changed=False, submission=None)
    wall_clock.set(BASE + timedelta(seconds=10))
    coordinator.emit_provider_watermark(
        stream=CaptureStream.SSR_STATE,
        provider="alpaca",
        symbol="VEEE",
        event_watermark_at=BASE + timedelta(seconds=9),
        emitted_available_at=BASE + timedelta(seconds=10),
        bounded_lateness_seconds=5,
        max_observed_lateness_seconds=0,
        generation=1,
    )
    coverage = coordinator.build_stream_coverage(CaptureStream.SSR_STATE)
    assert coverage.event_count == 1
    assert coverage.last_available_at == BASE + timedelta(seconds=5)
    assert coverage.watermark is not None
    assert coverage.watermark.event_watermark_at == BASE + timedelta(seconds=9)
    closed = coordinator.stop_and_seal()
    assert closed.producer_lifecycle_candidate is True


def test_executed_capture_inventory_binds_full_receipts_sources_and_order(
    tmp_path: Path,
) -> None:
    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / "executed-read-inventory",
        extra_streams=(CaptureStream.PROVIDER_OHLCV,),
    )
    decision_id = "paper-decision-executed-reads"

    def capture(index: int) -> CapturedReadResult:
        available = BASE + timedelta(seconds=3 + index)
        wall_clock.set(available + timedelta(milliseconds=1))
        result = coordinator.capture_query_result(
            decision_id=decision_id,
            stream=CaptureStream.PROVIDER_OHLCV,
            provider="massive",
            symbol="VEEE",
            query={"symbol": "VEEE", "interval": f"{index}m", "period": "5d"},
            requested_at=available - timedelta(milliseconds=4),
            returned_at=available + timedelta(milliseconds=1),
            results=(
                ObservedCaptureInput(
                    payload={
                        "open": 4.0,
                        "high": 4.2,
                        "low": 3.9,
                        "close": 4.1,
                        "index": index,
                    },
                    clocks=CaptureClocks(
                        market_reference_at=available - timedelta(minutes=index),
                        received_at=available - timedelta(milliseconds=1),
                        available_at=available,
                    ),
                ),
            ),
            read_id=str(uuid.UUID(int=200 + index)),
        )
        assert result.durable
        return result

    first = capture(5)
    second = capture(15)
    inventory = build_executed_capture_read_inventory(
        identity=coordinator.identity,
        decision_id=decision_id,
        captured_reads=(second, first),
    )
    assert inventory.decision_id == decision_id
    assert inventory.identity_sha256 == coordinator.identity.identity_sha256
    assert tuple(row.read_id for row in inventory.reads) == (
        first.receipt.read_id,
        second.receipt.read_id,
    )
    first_export = inventory.reads[0]
    assert json.loads(first_export.receipt_canonical_json) == first.receipt.to_dict()
    assert first_export.receipt_sha256 == sha256_json(first.receipt.to_dict())
    assert first_export.receipt_event_sha256 == (
        first.receipt_submission.event.event_sha256
    )
    assert first_export.receipt_event_sequence > first_export.source_events[0].sequence
    assert first_export.source_events[0].event_sha256 == (
        first.source_events[0].event_sha256
    )
    assert first_export.source_events[0].payload_sha256 == (
        first.source_events[0].payload_sha256
    )
    assert first_export.source_events[0].query_sha256 == (
        first.source_events[0].query_sha256
    )
    assert first_export.replay_network_fallback_used is False
    assert inventory.inventory_sha256 == sha256_json(inventory.to_dict())

    with pytest.raises(CaptureContractError, match="not durable"):
        build_executed_capture_read_inventory(
            identity=coordinator.identity,
            decision_id=decision_id,
            captured_reads=(
                replace(first, coverage_gap_recorded=True),
            ),
        )
    with pytest.raises(CaptureContractError, match="escaped decision"):
        build_executed_capture_read_inventory(
            identity=coordinator.identity,
            decision_id="another-decision",
            captured_reads=(first,),
        )
    with pytest.raises(CaptureContractError, match="inventory is empty"):
        build_executed_capture_read_inventory(
            identity=coordinator.identity,
            decision_id=decision_id,
            captured_reads=(),
        )


def test_first_dip_tape_read_receipts_exact_committed_print_clocks(
    tmp_path: Path,
) -> None:
    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / "first-dip-read",
        extra_streams=(CaptureStream.IQFEED_PRINT, CaptureStream.FSM_DECISION),
    )
    warmup_available_at = BASE + timedelta(seconds=2, milliseconds=5)
    wall_clock.set(warmup_available_at)
    warmup_submission = coordinator.submit_exact_input(
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        payload={
            "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
            "symbol": "VEEE",
            "price": 4.00,
            "size": 100,
            "conditions": ["fixture-warmup"],
        },
        clocks=CaptureClocks(
            provider_event_at=BASE + timedelta(seconds=2),
            received_at=warmup_available_at,
            available_at=warmup_available_at,
        ),
    )
    assert warmup_submission.event is not None
    wall_clock.set(BASE + timedelta(seconds=3))
    print_submission = coordinator.submit_exact_input(
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        payload={
            "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
            "symbol": "VEEE",
            "price": 4.12,
            "size": 800,
            "conditions": ["@"],
        },
        clocks=CaptureClocks(
            provider_event_at=BASE + timedelta(seconds=2, milliseconds=998),
            received_at=BASE + timedelta(seconds=2, milliseconds=999),
            available_at=BASE + timedelta(seconds=3),
        ),
    )
    assert print_submission.event is not None

    read_id = str(uuid.UUID(int=103))
    decision_at = BASE + timedelta(seconds=3, milliseconds=10)
    tape_policy = FirstDipTapePolicy(
        window_seconds=1.0,
        max_source_age_seconds=1.0,
        tick_rate_floor_pctile=0.0,
    )
    tape_query = FirstDipTapeReadQuery(
        symbol="VEEE",
        provider="iqfeed",
        event_start_exclusive=decision_at - timedelta(
            seconds=tape_policy.window_seconds
        ),
        event_end_inclusive=decision_at,
        decision_at=decision_at,
        available_at_most=decision_at,
        source_frontier_sequence=print_submission.event.sequence,
        policy_sha256=tape_policy.policy_sha256,
    )
    wall_clock.set(BASE + timedelta(seconds=3, milliseconds=10))
    captured = coordinator.capture_durable_read(
        decision_id="veee-first-dip",
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        query=tape_query.to_dict(),
        requested_at=BASE + timedelta(seconds=3, milliseconds=5),
        returned_at=BASE + timedelta(seconds=3, milliseconds=10),
        source_events=(print_submission.event,),
        read_id=read_id,
        first_dip_tape=True,
    )
    assert captured.durable
    assert captured.receipt is not None
    assert captured.receipt.returned_at == BASE + timedelta(
        seconds=3, milliseconds=10
    )
    assert captured.first_dip_tape_evidence is not None
    source_ref = captured.first_dip_tape_evidence.read_evidence.source_event_refs[0]
    assert source_ref.provider_event_at == BASE + timedelta(
        seconds=2, milliseconds=998
    )
    assert source_ref.received_at == BASE + timedelta(
        seconds=2, milliseconds=999
    )
    assert source_ref.available_at == BASE + timedelta(seconds=3)

    wall_clock.set(BASE + timedelta(seconds=3, milliseconds=11))
    coordinator.emit_provider_watermark(
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        event_watermark_at=BASE + timedelta(seconds=3, milliseconds=10),
        emitted_available_at=BASE + timedelta(seconds=3, milliseconds=11),
        bounded_lateness_seconds=1.0,
        max_observed_lateness_seconds=0.005,
        generation=coordinator.identity.generation,
    )
    wall_clock.set(BASE + timedelta(seconds=3, milliseconds=12))
    coordinator.checkpoint_live_continuity(CaptureStream.IQFEED_PRINT)
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
    predecision = coordinator.attest_predecision_inputs(
        decision_id="veee-first-dip",
        dependency_profile=dependency_profile,
        captured_reads=(captured,),
        first_dip_tape_read_id=read_id,
    )

    tape_evaluation = evaluate_first_dip_tape(
        first_dip_tape_window_from_capture(
            captured.receipt,
            (print_submission.event,),
        ),
        policy=tape_policy,
        decision_at=decision_at,
        symbol="VEEE",
    )
    wall_clock.set(BASE + timedelta(seconds=3, milliseconds=20))
    decision_prefix = coordinator.checkpoint_decision(
        decision_id="veee-first-dip",
        symbol="VEEE",
        decision_at=decision_at,
        received_at=BASE + timedelta(seconds=3, milliseconds=19),
        available_at=BASE + timedelta(seconds=3, milliseconds=20),
        required_read_ids=(read_id,),
        decision_output=CaptureDecisionOutput(
            decision_id="veee-first-dip",
            symbol="VEEE",
            action=CaptureDecisionAction.REJECT,
            fsm_state="entry_confirmation",
            setup_role="first_dip_reclaim",
            order_intents=(),
            reason_code="structural_risk_rejected",
        ),
        decision_details={
            "first_dip_tape_policy": tape_policy.to_dict(),
            "first_dip_tape_policy_sha256": tape_policy.policy_sha256,
            "first_dip_tape_evaluation": tape_evaluation.to_dict(),
            "first_dip_tape_evaluation_sha256": (
                tape_evaluation.evaluation_sha256
            ),
        },
        predecision_attestation=predecision,
    )
    assert (
        decision_prefix.checkpoint.decision_payload["first_dip_tape_read_id"]
        == read_id
    )
    wall_clock.set(BASE + timedelta(seconds=3, milliseconds=30))
    proof = coordinator.active_decision_capture_proof(
        decision_prefix=decision_prefix,
        captured_reads=(captured,),
        first_dip_tape_read_id=read_id,
    )
    assert proof.input_prefix_sequence == predecision.input_prefix_sequence
    assert (
        proof.attestation_frontier_sequence
        == decision_prefix.decision_event.sequence
    )
    assert proof.first_dip_tape_read_id == read_id
    assert proof.resource_binding_sha256 == coordinator.resource_binding.binding_sha256

    # Unrelated later control capture does not invalidate the immutable
    # decision snapshot; the broker boundary owns age/economic freshness.
    wall_clock.set(BASE + timedelta(seconds=3, milliseconds=40))
    coordinator.emit_capture_health(phase="after_active_proof")
    assert verify_active_capture_prefix_attestation(proof) is proof

    wall_clock.set(BASE + timedelta(seconds=9))
    coordinator.emit_provider_watermark(
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        event_watermark_at=BASE + timedelta(seconds=8),
        emitted_available_at=BASE + timedelta(seconds=9),
        bounded_lateness_seconds=1,
        max_observed_lateness_seconds=0.005,
        generation=1,
    )


def test_coordinator_binds_detector_and_fresh_final_read_to_adaptive_request(
    tmp_path: Path,
) -> None:
    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / "first-dip-two-checkpoint",
        extra_streams=(CaptureStream.IQFEED_PRINT, CaptureStream.FSM_DECISION),
    )
    policy = FirstDipTapePolicy(
        window_seconds=0.05,
        max_source_age_seconds=1.0,
        tick_rate_floor_pctile=0.0,
        minimum_prints=3,
    )
    warmup_at = BASE + timedelta(seconds=2, milliseconds=10)
    wall_clock.set(warmup_at)
    warmup = coordinator.submit_exact_input(
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        payload={
            "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
            "symbol": "VEEE",
            "price": 9.80,
            "size": 100,
            "conditions": ["fixture-warmup"],
        },
        clocks=CaptureClocks(
            provider_event_at=warmup_at,
            received_at=warmup_at,
            available_at=warmup_at,
        ),
    )
    assert warmup.event is not None

    def stage_tape_read(
        *,
        decision_id: str,
        decision_at: datetime,
        prices: tuple[float, float, float],
        attest: bool = True,
    ):
        wall_clock.set(decision_at)
        events = []
        for offset_ms, price, size in (
            (30, prices[0], 100),
            (20, prices[1], 200),
            (10, prices[2], 300),
        ):
            submission = coordinator.submit_exact_input(
                stream=CaptureStream.IQFEED_PRINT,
                provider="iqfeed",
                symbol="VEEE",
                payload={
                    "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
                    "symbol": "VEEE",
                    "price": price,
                    "size": size,
                    "bid": price - 0.01,
                    "ask": price,
                    "conditions": ["fixture-captured"],
                },
                clocks=CaptureClocks(
                    provider_event_at=decision_at
                    - timedelta(milliseconds=offset_ms),
                    received_at=decision_at,
                    available_at=decision_at,
                ),
            )
            assert submission.event is not None
            events.append(submission.event)
        query = FirstDipTapeReadQuery(
            symbol="VEEE",
            provider="iqfeed",
            event_start_exclusive=decision_at
            - timedelta(seconds=policy.window_seconds),
            event_end_inclusive=decision_at,
            decision_at=decision_at,
            available_at_most=decision_at,
            source_frontier_sequence=events[-1].sequence,
            policy_sha256=policy.policy_sha256,
        )
        read_id = str(uuid.uuid5(uuid.NAMESPACE_URL, decision_id + ":read"))
        captured = coordinator.capture_durable_read(
            decision_id=decision_id,
            stream=CaptureStream.IQFEED_PRINT,
            provider="iqfeed",
            symbol="VEEE",
            query=query.to_dict(),
            requested_at=decision_at - timedelta(milliseconds=1),
            returned_at=decision_at,
            source_events=tuple(events),
            read_id=read_id,
            first_dip_tape=True,
        )
        assert captured.durable
        wall_clock.set(decision_at + timedelta(milliseconds=1))
        coordinator.emit_provider_watermark(
            stream=CaptureStream.IQFEED_PRINT,
            provider="iqfeed",
            symbol="VEEE",
            event_watermark_at=decision_at,
            emitted_available_at=decision_at + timedelta(milliseconds=1),
            bounded_lateness_seconds=1.0,
            max_observed_lateness_seconds=0.05,
            generation=coordinator.identity.generation,
        )
        wall_clock.set(decision_at + timedelta(milliseconds=2))
        coordinator.checkpoint_live_continuity(CaptureStream.IQFEED_PRINT)
        profile = FSMDependencyProfile(
            required_streams=frozenset({CaptureStream.IQFEED_PRINT}),
            required_read_ids=(read_id,),
            stream_dependencies=(
                FSMStreamDependency(
                    stream=CaptureStream.IQFEED_PRINT,
                    exact_provider_event_at_required=True,
                    market_reference_at_required=False,
                    max_source_age_seconds=policy.max_source_age_seconds,
                    coverage_start_at=query.event_start_exclusive,
                ),
            ),
        )
        wall_clock.set(decision_at + timedelta(milliseconds=3))
        proof = (
            coordinator.attest_predecision_inputs(
                decision_id=decision_id,
                dependency_profile=profile,
                captured_reads=(captured,),
                first_dip_tape_read_id=read_id,
            )
            if attest
            else None
        )
        return captured, profile, proof

    detector_at = BASE + timedelta(seconds=2, milliseconds=100)
    detector_read, _detector_profile, detector_proof = stage_tape_read(
        decision_id="veee-first-dip-adaptive",
        decision_at=detector_at,
        prices=(9.90, 10.01, 10.02),
    )
    assert detector_proof is not None
    assert detector_read.receipt is not None

    adaptive_at = detector_at + timedelta(milliseconds=20)
    capture_binding = adaptive_risk_capture_binding_from_active_attestation(
        detector_proof
    )
    snapshot = replace(
        _snapshot(account_scope="alpaca:paper:first-dip-capture"),
        account_identity_sha256=coordinator.identity.account_identity_sha256,
        observed_at=adaptive_at - timedelta(milliseconds=8),
        available_at=adaptive_at - timedelta(milliseconds=7),
    )
    initial_inputs = _inputs(
        snapshot,
        symbol="VEEE",
        decision_id="veee-first-dip-adaptive",
        cluster="equity:veee",
    )
    account_evidence = RiskInputEvidence(
        source=snapshot.source,
        observed_at=snapshot.observed_at,
        available_at=snapshot.available_at,
        content_sha256=snapshot.snapshot_sha256,
        provider_generation=snapshot.provider_generation,
    )
    evidence = {
        name: RiskInputEvidence(
            source=f"fixture:{name}",
            observed_at=adaptive_at - timedelta(milliseconds=6),
            available_at=adaptive_at - timedelta(milliseconds=5),
            content_sha256=sha256_json({"fixture": name}),
            provider_generation="first-dip-capture-fixture-v1",
        )
        for name in initial_inputs.evidence
    }
    evidence["account"] = account_evidence
    evidence["daily_pnl"] = account_evidence
    evidence["code_build"] = replace(
        evidence["code_build"],
        content_sha256=coordinator.identity.code_build_sha256,
    )
    evidence["effective_config"] = replace(
        evidence["effective_config"],
        content_sha256=coordinator.identity.config_sha256,
    )
    evidence["feature_flags"] = replace(
        evidence["feature_flags"],
        content_sha256=coordinator.identity.feature_flags_sha256,
    )
    evidence["capture_prefix"] = RiskInputEvidence(
        source="live-replay-capture:first-dip-detector",
        observed_at=capture_binding.observed_at,
        available_at=capture_binding.available_at,
        content_sha256=capture_binding.input_prefix_root_sha256,
        provider_generation=capture_binding.verifier_generation,
    )
    inputs = replace(
        initial_inputs,
        replay_or_paper_run_id=coordinator.identity.run_id,
        generation=coordinator.identity.generation,
        as_of=adaptive_at,
        account_identity_sha256=coordinator.identity.account_identity_sha256,
        code_build_sha256=coordinator.identity.code_build_sha256,
        effective_config_sha256=coordinator.identity.config_sha256,
        feature_flags_sha256=coordinator.identity.feature_flags_sha256,
        capture_prefix_root_sha256=detector_proof.input_prefix_root_sha256,
        open_structural_risk_usd=0.0,
        pending_reserved_risk_usd=0.0,
        existing_same_symbol_structural_risk_usd=0.0,
        pending_same_symbol_structural_risk_usd=0.0,
        current_cluster_structural_risk_usd=0.0,
        pending_correlation_cluster_risk_usd=0.0,
        portfolio_gross_notional_usd=0.0,
        pending_portfolio_gross_notional_usd=0.0,
        policy_buying_power_capacity_usd=snapshot.buying_power_usd,
        open_buying_power_impact_usd=0.0,
        pending_buying_power_impact_usd=0.0,
        evidence=evidence,
    )
    request = _request(
        symbol="VEEE",
        decision_id=inputs.decision_id,
        client_order_id=inputs.decision_id,
        cluster="equity:veee",
        snapshot=snapshot,
        inputs=inputs,
    )
    assert request.opportunity_key is not None
    source = AdaptiveRiskBuilderSource(
        policy=request.policy,
        inputs=request.inputs,
        account_snapshot=request.account_snapshot,
        capture_binding=capture_binding,
        account_scope=request.account_scope,
        setup_family=request.setup_family,
        correlation_cluster=request.correlation_cluster,
    )

    final_at = detector_at + timedelta(milliseconds=150)
    final_read, final_profile, _ordinary_final_proof = stage_tape_read(
        decision_id="veee-first-dip-final",
        decision_at=final_at,
        prices=(10.03, 10.10, 10.12),
        attest=False,
    )
    assert final_read.receipt is not None
    stale_request = replace(
        request,
        inputs=replace(
            request.inputs,
            as_of=final_at + timedelta(milliseconds=1),
        ),
    )
    with pytest.raises(CaptureContractError, match="identity or time"):
        coordinator.attest_first_dip_pre_reservation_inputs(
            adaptive_request=stale_request,
            dependency_profile=final_profile,
            captured_reads=(final_read,),
            first_dip_tape_read_id=final_read.receipt.read_id,
        )

    final_snapshot = FirstDipFinalCaptureRead(
        dependency_profile=final_profile,
        captured_reads=(final_read,),
        first_dip_tape_read_id=final_read.receipt.read_id,
    )
    final_provider_calls = []

    def final_provider(**boundary):
        final_provider_calls.append(boundary)
        return final_snapshot

    bridge = LiveFirstDipAdaptiveCaptureBridge(
        coordinator=coordinator,
        detector_attestation=detector_proof,
        detector_policy=policy,
        adaptive_source=source,
        final_read_provider=final_provider,
    )
    final_boundary = final_at + timedelta(milliseconds=4)
    wall_clock.set(final_boundary)
    with bridge.install():
        detector_resolution = first_dip_tape_decision.resolve_first_dip_tape_decision(
            symbol="VEEE",
            decision_at=detector_at,
            policy=policy,
            purpose=(
                first_dip_tape_decision.FIRST_DIP_TAPE_PURPOSE_DETECTOR
            ),
        )
        retained = (
            first_dip_tape_decision
            ._retain_captured_first_dip_detector_for_opportunity(
                detector_resolution,
                opportunity_key={
                    "symbol": request.opportunity_key.symbol,
                    "trading_date": (
                        request.opportunity_key.trading_date.isoformat()
                    ),
                    "setup_family": request.opportunity_key.setup_family,
                },
            )
        )
        material = runtime_adaptive_risk_capture_material(
            execution_surface="alpaca_paper",
            execution_family="alpaca_spot",
            venue="alpaca",
            broker_environment="paper",
            symbol="VEEE",
            decision_id=request.inputs.decision_id,
            setup_family="first_dip_reclaim",
            correlation_cluster="equity:veee",
        )
        built = build_adaptive_risk_request(
            material.source,
            client_order_id=request.client_order_id,
            entry_limit_price=request.entry_limit_price,
            opportunity_key=request.opportunity_key.to_payload(),
            active_capture_attestation=(
                material.active_capture_attestation
            ),
        )
        final_resolution = (
            first_dip_tape_decision
            ._resolve_first_dip_final_admission_with_active_provider(
                adaptive_request=built.request,
                detector_policy=policy,
                symbol="VEEE",
                adaptive_decision_at=built.request.inputs.as_of,
                run_id=built.request.inputs.replay_or_paper_run_id,
                generation=built.request.inputs.generation,
                adaptive_decision_id=built.request.inputs.decision_id,
                adaptive_input_prefix_root_sha256=(
                    built.request.inputs.capture_prefix_root_sha256
                ),
                adaptive_request_sha256=built.request.request_sha256,
                opportunity_key_sha256=(
                    built.request.opportunity_key.key_sha256
                ),
                final_boundary_available_at=final_boundary,
                expected_execution_surface="captured_db_paper",
                detector_policy_sha256=policy.policy_sha256,
                detector_authority_source="captured_db_paper",
                detector_receipt_binding_sha256=(
                    detector_resolution.receipt.binding_sha256
                ),
                detector_opportunity_key_sha256=(
                    built.request.opportunity_key.key_sha256
                ),
            )
        )

    assert detector_resolution.run_bound
    assert detector_resolution.evaluation.status == "valid_positive"
    assert len(retained) == 64
    assert built.request.to_payload() == request.to_payload()
    assert final_resolution.admitted is True
    assert final_resolution.reason == (
        "first_dip_final_admission_typed_receipt_verified"
    )
    assert len(final_provider_calls) == 1
    assert final_provider_calls[0]["adaptive_request"].request_sha256 == (
        request.request_sha256
    )
    assert bridge.network_fallback_allowed is False
    frontier = bridge.final_capture_frontier
    assert frontier is not None
    assert frontier.run_id == coordinator.identity.run_id
    assert frontier.identity_sha256 == coordinator.identity.identity_sha256
    assert frontier.decision_id == final_read.receipt.decision_id
    assert frontier.first_dip_tape_read_id == final_read.receipt.read_id
    assert frontier.adaptive_request_sha256 == request.request_sha256
    assert frontier.opportunity_key_sha256 == request.opportunity_key.key_sha256
    assert frontier.input_prefix_sequence > detector_proof.input_prefix_sequence
    assert len(frontier.frontier_sha256) == 64
    wall_clock.set(final_boundary + timedelta(milliseconds=1))
    decision_prefix = coordinator.checkpoint_decision(
        decision_id=detector_proof.decision_id,
        symbol="VEEE",
        decision_at=detector_at,
        received_at=final_boundary,
        available_at=final_boundary + timedelta(milliseconds=1),
        required_read_ids=(detector_read.receipt.read_id,),
        decision_output=CaptureDecisionOutput(
            decision_id=detector_proof.decision_id,
            symbol="VEEE",
            action=CaptureDecisionAction.REJECT,
            fsm_state="entry_confirmation",
            setup_role="first_dip_reclaim",
            order_intents=(),
            reason_code="fixture_post_admission_breaker",
        ),
        decision_details={
            "first_dip_tape_policy": policy.to_dict(),
            "first_dip_tape_policy_sha256": policy.policy_sha256,
            "first_dip_tape_evaluation": detector_resolution.evaluation.to_dict(),
            "first_dip_tape_evaluation_sha256": (
                detector_resolution.evaluation.evaluation_sha256
            ),
        },
        predecision_attestation=detector_proof,
        first_dip_final_capture_frontier=frontier,
    )
    assert decision_prefix.checkpoint.decision_payload[
        "first_dip_final_capture_frontier"
    ] == frontier.to_dict()
    assert decision_prefix.checkpoint.decision_payload[
        "first_dip_final_capture_frontier_sha256"
    ] == frontier.frontier_sha256
    with pytest.raises(CaptureContractError, match="foreign or mismatched"):
        coordinator.checkpoint_decision(
            decision_id=detector_proof.decision_id,
            symbol="VEEE",
            decision_at=detector_at,
            received_at=final_boundary,
            available_at=final_boundary + timedelta(milliseconds=1),
            required_read_ids=(detector_read.receipt.read_id,),
            decision_output=CaptureDecisionOutput(
                decision_id=detector_proof.decision_id,
                symbol="VEEE",
                action=CaptureDecisionAction.REJECT,
                fsm_state="entry_confirmation",
                setup_role="first_dip_reclaim",
                order_intents=(),
                reason_code="duplicate_frontier_forbidden",
            ),
            decision_details={},
            predecision_attestation=detector_proof,
            first_dip_final_capture_frontier=frontier,
        )
    with pytest.raises(CaptureContractError, match="one-shot"):
        with bridge.install():
            pass


def test_complete_empty_first_dip_window_is_a_gap_free_reusable_negative(
    tmp_path: Path,
) -> None:
    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / "first-dip-empty-window",
        extra_streams=(CaptureStream.IQFEED_PRINT, CaptureStream.FSM_DECISION),
    )
    warmup_available_at = BASE + timedelta(seconds=2, milliseconds=5)
    wall_clock.set(warmup_available_at)
    warmup = coordinator.submit_exact_input(
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        payload={
            "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
            "symbol": "VEEE",
            "price": 4.00,
            "size": 100,
            "conditions": ["fixture-before-empty-window"],
        },
        clocks=CaptureClocks(
            provider_event_at=BASE + timedelta(seconds=2),
            received_at=warmup_available_at,
            available_at=warmup_available_at,
        ),
    )
    assert warmup.event is not None

    decision_at = BASE + timedelta(seconds=3, milliseconds=10)
    policy = FirstDipTapePolicy(
        window_seconds=1.0,
        max_source_age_seconds=1.0,
        tick_rate_floor_pctile=0.0,
    )
    query = FirstDipTapeReadQuery(
        symbol="VEEE",
        provider="iqfeed",
        event_start_exclusive=decision_at
        - timedelta(seconds=policy.window_seconds),
        event_end_inclusive=decision_at,
        decision_at=decision_at,
        available_at_most=decision_at,
        source_frontier_sequence=warmup.event.sequence,
        policy_sha256=policy.policy_sha256,
    )
    before = coordinator.health()
    read_id = str(uuid.UUID(int=10_303))
    wall_clock.set(decision_at)
    captured = coordinator.capture_durable_read(
        decision_id="veee-first-dip-empty",
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        query=query.to_dict(),
        requested_at=decision_at - timedelta(milliseconds=1),
        returned_at=decision_at,
        source_events=(),
        read_id=read_id,
        first_dip_tape=True,
    )

    assert captured.durable
    assert captured.receipt is not None
    assert captured.receipt.empty_result is True
    assert captured.source_events == ()
    assert captured.first_dip_tape_evidence is not None
    assert captured.first_dip_tape_evidence.read_evidence.source_event_refs == ()
    after = coordinator.health()
    assert after["producer_lifecycle"]["producer_gaps"] == []
    assert (
        after["rejected_or_reported_lost_count"]
        == before["rejected_or_reported_lost_count"]
    )

    wall_clock.set(decision_at + timedelta(milliseconds=1))
    coordinator.emit_provider_watermark(
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        event_watermark_at=decision_at,
        emitted_available_at=decision_at + timedelta(milliseconds=1),
        bounded_lateness_seconds=1.0,
        max_observed_lateness_seconds=0.005,
        generation=coordinator.identity.generation,
    )
    wall_clock.set(decision_at + timedelta(milliseconds=2))
    coordinator.checkpoint_live_continuity(CaptureStream.IQFEED_PRINT)
    dependency_profile = FSMDependencyProfile(
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
    predecision = coordinator.attest_predecision_inputs(
        decision_id="veee-first-dip-empty",
        dependency_profile=dependency_profile,
        captured_reads=(captured,),
        first_dip_tape_read_id=read_id,
    )
    evaluation = evaluate_first_dip_tape(
        first_dip_tape_window_from_capture(captured.receipt, ()),
        policy=policy,
        decision_at=decision_at,
        symbol="VEEE",
    )
    assert evaluation.status == "valid_negative"
    assert evaluation.reason == "first_dip_tape_no_prints"

    wall_clock.set(decision_at + timedelta(milliseconds=3))
    decision_prefix = coordinator.checkpoint_decision(
        decision_id="veee-first-dip-empty",
        symbol="VEEE",
        decision_at=decision_at,
        received_at=decision_at + timedelta(milliseconds=2),
        available_at=decision_at + timedelta(milliseconds=3),
        required_read_ids=(read_id,),
        decision_output=CaptureDecisionOutput(
            decision_id="veee-first-dip-empty",
            symbol="VEEE",
            action=CaptureDecisionAction.REJECT,
            fsm_state="entry_confirmation",
            setup_role="first_dip_reclaim",
            order_intents=(),
            reason_code="first_dip_tape_no_prints",
        ),
        decision_details={
            "first_dip_tape_policy": policy.to_dict(),
            "first_dip_tape_policy_sha256": policy.policy_sha256,
            "first_dip_tape_evaluation": evaluation.to_dict(),
            "first_dip_tape_evaluation_sha256": evaluation.evaluation_sha256,
        },
        predecision_attestation=predecision,
    )
    proof = coordinator.active_decision_capture_proof(
        decision_prefix=decision_prefix,
        captured_reads=(captured,),
        first_dip_tape_read_id=read_id,
    )
    assert proof.order_intent_sha256s == ()
    assert proof.first_dip_tape_read_id == read_id
    assert coordinator.health()["producer_lifecycle"]["producer_gaps"] == []


@pytest.mark.parametrize(
    "selected_indices",
    ((0, 2), (2, 1, 0)),
    ids=("omitted-middle-print", "reordered-complete-window"),
)
def test_first_dip_tape_read_rejects_caller_selected_inventory(
    tmp_path: Path,
    selected_indices: tuple[int, ...],
) -> None:
    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / f"first-dip-inventory-{len(selected_indices)}",
        extra_streams=(CaptureStream.IQFEED_PRINT,),
    )
    events = []
    for index in range(3):
        available_at = BASE + timedelta(seconds=3, milliseconds=index)
        wall_clock.set(available_at)
        submission = coordinator.submit_exact_input(
            stream=CaptureStream.IQFEED_PRINT,
            provider="iqfeed",
            symbol="VEEE",
            payload={
                "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
                "symbol": "VEEE",
                "price": 4.10 + index * 0.01,
                "size": 100 + index,
                "conditions": ["fixture-only"],
            },
            clocks=CaptureClocks(
                provider_event_at=available_at - timedelta(microseconds=2),
                received_at=available_at - timedelta(microseconds=1),
                available_at=available_at,
            ),
        )
        assert submission.event is not None
        events.append(submission.event)

    decision_at = BASE + timedelta(seconds=3, milliseconds=10)
    policy = FirstDipTapePolicy(
        window_seconds=2.0,
        max_source_age_seconds=1.0,
        tick_rate_floor_pctile=0.0,
    )
    query = FirstDipTapeReadQuery(
        symbol="VEEE",
        provider="iqfeed",
        event_start_exclusive=decision_at - timedelta(seconds=2),
        event_end_inclusive=decision_at,
        decision_at=decision_at,
        available_at_most=decision_at,
        source_frontier_sequence=events[-1].sequence,
        policy_sha256=policy.policy_sha256,
    )
    wall_clock.set(decision_at)
    captured = coordinator.capture_durable_read(
        decision_id=f"inventory-{selected_indices}",
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        query=query.to_dict(),
        requested_at=decision_at,
        returned_at=decision_at,
        source_events=tuple(events[index] for index in selected_indices),
        first_dip_tape=True,
    )

    assert not captured.durable
    assert captured.receipt is None
    assert captured.coverage_gap_recorded is False
    assert (
        captured.receipt_submission is not None
        and captured.receipt_submission.disposition
        == "first_dip_tape_decision_rejected_without_gap"
    )
    assert coordinator.health()["producer_lifecycle"]["producer_gaps"] == []


def test_first_dip_tape_bounded_index_eviction_records_explicit_gap(
    tmp_path: Path,
) -> None:
    binding = _binding(max_ring_events=8)
    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / "first-dip-index-overflow",
        extra_streams=(CaptureStream.IQFEED_PRINT,),
        binding=binding,
    )
    events = []
    for index in range(9):
        available_at = BASE + timedelta(seconds=3, milliseconds=index)
        wall_clock.set(available_at)
        submission = coordinator.submit_exact_input(
            stream=CaptureStream.IQFEED_PRINT,
            provider="iqfeed",
            symbol="VEEE",
            payload={
                "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
                "symbol": "VEEE",
                "price": 4.00 + index * 0.01,
                "size": 100,
                "conditions": ["fixture-only"],
            },
            clocks=CaptureClocks(
                provider_event_at=available_at - timedelta(microseconds=2),
                received_at=available_at - timedelta(microseconds=1),
                available_at=available_at,
            ),
        )
        assert submission.event is not None
        events.append(submission.event)

    decision_at = BASE + timedelta(seconds=3, milliseconds=20)
    policy = FirstDipTapePolicy(
        window_seconds=2.0,
        max_source_age_seconds=1.0,
        tick_rate_floor_pctile=0.0,
    )
    query = FirstDipTapeReadQuery(
        symbol="VEEE",
        provider="iqfeed",
        event_start_exclusive=decision_at - timedelta(seconds=2),
        event_end_inclusive=decision_at,
        decision_at=decision_at,
        available_at_most=decision_at,
        source_frontier_sequence=events[-1].sequence,
        policy_sha256=policy.policy_sha256,
    )
    wall_clock.set(decision_at)
    captured = coordinator.capture_durable_read(
        decision_id="bounded-index-overflow",
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        query=query.to_dict(),
        requested_at=decision_at,
        returned_at=decision_at,
        source_events=tuple(events[-8:]),
        first_dip_tape=True,
    )

    assert not captured.durable
    assert captured.coverage_gap_recorded is True
    first_health = coordinator.health()
    assert any(
        value.endswith(":first_dip_tape_bounded_index_coverage_gap")
        for value in first_health["producer_lifecycle"]["producer_gaps"]
    )
    duplicate = coordinator.capture_durable_read(
        decision_id="bounded-index-overflow-retry",
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        query=query.to_dict(),
        requested_at=decision_at,
        returned_at=decision_at,
        source_events=tuple(events[-8:]),
        first_dip_tape=True,
    )
    duplicate_health = coordinator.health()
    assert not duplicate.durable
    assert duplicate.coverage_gap_recorded is True
    assert (
        duplicate_health["producer_lifecycle"]["producer_gaps"]
        == first_health["producer_lifecycle"]["producer_gaps"]
    )
    assert (
        duplicate_health["rejected_or_reported_lost_count"]
        == first_health["rejected_or_reported_lost_count"]
    )
    wall_clock.set(BASE + timedelta(seconds=10))
    coordinator.stop_and_seal()


def test_decision_prefix_matches_contract_but_cannot_claim_certification(
    tmp_path: Path,
) -> None:
    coordinator, startup, wall_clock = _coordinator(
        tmp_path / "checkpoint",
        extra_streams=(CaptureStream.NBBO_QUOTE, CaptureStream.FSM_DECISION),
    )
    wall_clock.set(BASE + timedelta(seconds=3, milliseconds=10))
    quote = coordinator.submit_exact_input(
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="VEEE",
        clocks=_quote_clocks(1),
        payload={"bid": 4.10, "ask": 4.12},
    )
    assert quote.event is not None
    read = coordinator.capture_durable_read(
        decision_id="veee-entry",
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="VEEE",
        query={"surface": "decision_prefix_fixture"},
        requested_at=quote.event.clocks.available_at,
        returned_at=quote.event.clocks.available_at,
        source_events=(quote.event,),
    )
    assert read.durable and read.receipt is not None
    wall_clock.set(BASE + timedelta(seconds=3, milliseconds=20))
    coordinator.emit_provider_watermark(
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="VEEE",
        event_watermark_at=quote.event.clocks.provider_event_at,
        emitted_available_at=BASE + timedelta(seconds=3, milliseconds=20),
        bounded_lateness_seconds=7,
        max_observed_lateness_seconds=0.02,
        generation=1,
    )
    coordinator.checkpoint_live_continuity(CaptureStream.NBBO_QUOTE)
    dependency_profile = _dependency_profile(
        stream=CaptureStream.NBBO_QUOTE,
        read_id=read.receipt.read_id,
        coverage_start_at=quote.event.clocks.available_at,
        max_source_age_seconds=7.0,
    )
    predecision = coordinator.attest_predecision_inputs(
        decision_id="veee-entry",
        dependency_profile=dependency_profile,
        captured_reads=(read,),
    )
    expected_root = predecision.input_prefix_root_sha256
    expected_sequence = predecision.input_prefix_sequence

    wall_clock.set(BASE + timedelta(seconds=4, milliseconds=2))
    with pytest.raises(CaptureContractError, match="canonical FSM decision output"):
        coordinator.checkpoint_decision(
            decision_id="missing-output",
            symbol="VEEE",
            decision_at=BASE + timedelta(seconds=4),
            received_at=BASE + timedelta(seconds=4, milliseconds=1),
            available_at=BASE + timedelta(seconds=4, milliseconds=2),
            required_read_ids=(read.receipt.read_id,),
            decision_output=None,  # type: ignore[arg-type]
            decision_details={},
            predecision_attestation=predecision,
        )
    prefix = coordinator.checkpoint_decision(
        decision_id="veee-entry",
        symbol="VEEE",
        decision_at=BASE + timedelta(seconds=4),
        received_at=BASE + timedelta(seconds=4, milliseconds=1),
        available_at=BASE + timedelta(seconds=4, milliseconds=2),
        required_read_ids=(read.receipt.read_id,),
        decision_output=CaptureDecisionOutput(
            decision_id="veee-entry",
            symbol="VEEE",
            action=CaptureDecisionAction.REJECT,
            fsm_state="watching",
            setup_role="first_dip_reclaim",
            order_intents=(),
            reason_code="tape_not_confirmed",
        ),
        decision_details={},
        predecision_attestation=predecision,
    )
    assert prefix.checkpoint.input_prefix_sequence == expected_sequence
    assert prefix.checkpoint.input_prefix_root_sha256 == expected_root
    assert prefix.decision_event.payload["decision_output_sha256"] == sha256_json(
        prefix.decision_event.payload["decision_output"]
    )
    assert prefix.independently_verified is False
    assert prefix.replay_network_fallback_allowed is False
    with pytest.raises(CaptureContractError, match="not certifying adaptive evidence"):
        prefix.as_certifying_adaptive_evidence()

    wall_clock.set(BASE + timedelta(seconds=9))
    coordinator.emit_provider_watermark(
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="VEEE",
        event_watermark_at=BASE + timedelta(seconds=3),
        emitted_available_at=BASE + timedelta(seconds=9),
        bounded_lateness_seconds=7,
        max_observed_lateness_seconds=6,
        generation=1,
    )
    wall_clock.set(BASE + timedelta(seconds=10))
    handoff = coordinator.stop_and_seal()
    assert handoff.independent_expected_seal_required is True
    with pytest.raises(CaptureContractError, match="only a handoff"):
        handoff.as_certifying_adaptive_evidence()


def test_order_decision_binds_cid_and_ordered_cumulative_broker_transitions(
    tmp_path: Path,
) -> None:
    coordinator, _startup, wall_clock = _coordinator(
        tmp_path / "broker-lifecycle",
        extra_streams=(
            CaptureStream.NBBO_QUOTE,
            CaptureStream.FSM_DECISION,
            CaptureStream.BROKER_ORDER_LIFECYCLE,
        ),
    )
    decision_id = "veee-order-decision"
    wall_clock.set(BASE + timedelta(seconds=3, milliseconds=10))
    quote = coordinator.submit_exact_input(
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="VEEE",
        clocks=_quote_clocks(1),
        payload={"bid": 4.10, "ask": 4.12},
    )
    assert quote.event is not None
    read = coordinator.capture_durable_read(
        decision_id=decision_id,
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="VEEE",
        query={"surface": "broker_lifecycle_fixture"},
        requested_at=quote.event.clocks.available_at,
        returned_at=quote.event.clocks.available_at,
        source_events=(quote.event,),
    )
    assert read.durable and read.receipt is not None
    wall_clock.set(BASE + timedelta(seconds=3, milliseconds=20))
    coordinator.emit_provider_watermark(
        stream=CaptureStream.NBBO_QUOTE,
        provider="massive",
        symbol="VEEE",
        event_watermark_at=quote.event.clocks.provider_event_at,
        emitted_available_at=BASE + timedelta(seconds=3, milliseconds=20),
        bounded_lateness_seconds=20,
        max_observed_lateness_seconds=0.02,
        generation=1,
    )
    coordinator.checkpoint_live_continuity(CaptureStream.NBBO_QUOTE)
    dependency_profile = _dependency_profile(
        stream=CaptureStream.NBBO_QUOTE,
        read_id=read.receipt.read_id,
        coverage_start_at=quote.event.clocks.available_at,
        max_source_age_seconds=20.0,
    )
    predecision = coordinator.attest_predecision_inputs(
        decision_id=decision_id,
        dependency_profile=dependency_profile,
        captured_reads=(read,),
    )
    client_order_id = "chili-veee-order-1"
    intent = CaptureOrderIntent(
        intent_id=str(uuid.UUID(int=201)),
        client_order_id=client_order_id,
        client_order_id_sha256=sha256_json(
            {"client_order_id": client_order_id}
        ),
        symbol="VEEE",
        side="sell",
        order_type="limit",
        quantity=100,
        time_in_force="day",
        extended_hours=True,
        intent_role=CaptureOrderIntentRole.EXIT,
        risk_increasing=False,
        decision_provenance_sha256=predecision.attestation_sha256,
        limit_price=4.12,
    )
    wall_clock.set(BASE + timedelta(seconds=3, milliseconds=30))
    decision_prefix = coordinator.checkpoint_decision(
        decision_id=decision_id,
        symbol="VEEE",
        decision_at=BASE + timedelta(seconds=3),
        received_at=BASE + timedelta(seconds=3, milliseconds=30),
        available_at=BASE + timedelta(seconds=3, milliseconds=30),
        required_read_ids=(read.receipt.read_id,),
        decision_output=CaptureDecisionOutput(
            decision_id=decision_id,
            symbol="VEEE",
            action=CaptureDecisionAction.ORDER_INTENT,
            fsm_state="risk_exit",
            setup_role="exit",
            order_intents=(intent,),
        ),
        decision_details={},
        predecision_attestation=predecision,
    )
    wall_clock.set(BASE + timedelta(seconds=3, milliseconds=40))
    final_authority = coordinator.active_decision_capture_proof(
        decision_prefix=decision_prefix,
        captured_reads=(read,),
    )

    def lifecycle(
        transition: CaptureBrokerTransition,
        *,
        cumulative: int,
        last_fill: int,
        prior: str | None,
        price: float | None = None,
        broker_order_id: str | None = "alpaca-order-1",
    ) -> CaptureBrokerOrderLifecycle:
        raw_provider_event_sha256 = (
            None
            if transition is CaptureBrokerTransition.SUBMITTED
            else sha256_json(
                {
                    "provider_status": transition.value,
                    "cumulative": cumulative,
                    "last_fill": last_fill,
                    "prior": prior,
                }
            )
        )
        return CaptureBrokerOrderLifecycle(
            decision_id=decision_id,
            order_intent_sha256=intent.order_intent_sha256,
            client_order_id=client_order_id,
            client_order_id_sha256=intent.client_order_id_sha256,
            transition=transition,
            order_quantity=100,
            cumulative_filled_quantity=cumulative,
            last_fill_quantity=last_fill,
            last_fill_price=price,
            prior_transition_event_sha256=prior,
            final_decision_attestation_sha256=(
                final_authority.durable_authority_sha256
            ),
            broker_order_id=broker_order_id,
            raw_provider_event_sha256=raw_provider_event_sha256,
        )

    def broker_clocks(at: datetime) -> CaptureClocks:
        return CaptureClocks(
            provider_event_at=at,
            received_at=at,
            available_at=at,
        )

    wall_clock.set(BASE + timedelta(seconds=4))
    submitted = coordinator.record_broker_order_lifecycle(
        lifecycle=lifecycle(
            CaptureBrokerTransition.SUBMITTED,
            cumulative=0,
            last_fill=0,
            prior=None,
            broker_order_id=None,
        ),
        provider="alpaca",
        symbol="VEEE",
        clocks=broker_clocks(BASE + timedelta(seconds=4)),
    )
    assert submitted.event is not None

    wall_clock.set(BASE + timedelta(seconds=5))
    with pytest.raises(CaptureContractError, match="hash chain"):
        coordinator.record_broker_order_lifecycle(
            lifecycle=lifecycle(
                CaptureBrokerTransition.ACCEPTED,
                cumulative=0,
                last_fill=0,
                prior="f" * 64,
            ),
            provider="alpaca",
            symbol="VEEE",
            clocks=broker_clocks(BASE + timedelta(seconds=5)),
        )
    accepted = coordinator.record_broker_order_lifecycle(
        lifecycle=lifecycle(
            CaptureBrokerTransition.ACCEPTED,
            cumulative=0,
            last_fill=0,
            prior=submitted.event.event_sha256,
        ),
        provider="alpaca",
        symbol="VEEE",
        clocks=broker_clocks(BASE + timedelta(seconds=5)),
    )
    assert accepted.event is not None
    assert accepted.event.sequence == submitted.event.sequence + 1

    wall_clock.set(BASE + timedelta(seconds=6))
    partial = coordinator.record_broker_order_lifecycle(
        lifecycle=lifecycle(
            CaptureBrokerTransition.PARTIALLY_FILLED,
            cumulative=40,
            last_fill=40,
            prior=accepted.event.event_sha256,
            price=4.11,
        ),
        provider="alpaca",
        symbol="VEEE",
        clocks=broker_clocks(BASE + timedelta(seconds=6)),
    )
    assert partial.event is not None

    wall_clock.set(BASE + timedelta(seconds=7))
    filled = coordinator.record_broker_order_lifecycle(
        lifecycle=lifecycle(
            CaptureBrokerTransition.FILLED,
            cumulative=100,
            last_fill=60,
            prior=partial.event.event_sha256,
            price=4.12,
        ),
        provider="alpaca",
        symbol="VEEE",
        clocks=broker_clocks(BASE + timedelta(seconds=7)),
    )
    assert filled.event is not None
    assert filled.event.sequence == partial.event.sequence + 1

    wall_clock.set(BASE + timedelta(seconds=10))
    handoff = coordinator.stop_and_seal()
    assert handoff.gap_count == 0


def test_abort_is_explicitly_unsealed_and_cannot_be_presented_as_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "abort"
    coordinator, _startup, wall_clock = _coordinator(root)
    wall_clock.set(BASE + timedelta(seconds=5))
    closed = coordinator.abort(
        reason="provider_generation_disconnected",
    )
    assert coordinator.state is CaptureSessionState.ABORTED
    assert closed.certification_eligible is False
    assert not list(root.glob("seals/run=*/generation=*/*.json"))
    with pytest.raises(CaptureContractError, match="unsealed capture"):
        closed.as_certifying_adaptive_evidence()


def test_ohlcv_capture_bridge_receipts_cross_symbol_frame_and_query_provenance(
    tmp_path,
) -> None:
    coordinator, _startup, wall = _coordinator(
        tmp_path / "ohlcv-bridge",
        extra_streams=(CaptureStream.PROVIDER_OHLCV,),
    )
    wall.set(BASE + timedelta(seconds=10))
    macro_cache: dict = {}
    bridge = LiveOhlcvCaptureBridge(
        coordinator=coordinator,
        decision_id="ohlcv-decision-1",
        macro_cache=macro_cache,
    )
    frame = pd.DataFrame(
        {
            "Open": [500.0, 501.0],
            "High": [502.0, 503.0],
            "Low": [499.0, 500.0],
            "Close": [501.0, 502.0],
            "Volume": [1_000_000.0, 1_100_000.0],
        },
        index=pd.date_range(
            BASE - timedelta(days=1), periods=2, freq="1d", tz="UTC"
        ),
    )
    frame.attrs.update(
        {
            "provider": "massive",
            "fetched_at_utc": (BASE + timedelta(seconds=6)).isoformat(),
            "ticker": "SPY",
            "interval": "1d",
            "integrity_ok": True,
            "cache_hit": False,
            "cache_age_seconds": 0.0,
        }
    )
    second_frame = frame.copy()
    second_frame.attrs.update(frame.attrs)
    second_frame.attrs.update(
        {
            "provider": "polygon",
            "ticker": "IWM",
            "fetched_at_utc": (BASE + timedelta(seconds=7)).isoformat(),
        }
    )
    try:
        with bridge.install():
            assert bridge.on_ohlcv_result(
                ticker="SPY",
                interval="1d",
                period="3mo",
                requested_at=BASE + timedelta(seconds=7),
                returned_at=BASE + timedelta(seconds=8),
                allow_provider_fallback=True,
                frame=frame,
            )
            assert bridge.on_ohlcv_result(
                ticker="IWM",
                interval="1d",
                period="3mo",
                requested_at=BASE + timedelta(seconds=8),
                returned_at=BASE + timedelta(seconds=9),
                allow_provider_fallback=True,
                frame=second_frame,
            )
        captured = bridge.captured_reads
        assert len(captured) == 2 and all(row.durable for row in captured)
        event = captured[0].source_events[0]
        assert event.symbol == "SPY"
        assert event.provider == "massive"
        assert event.query["call"] == {
            "symbol": "SPY",
            "interval": "1d",
            "period": "3mo",
        }
        assert event.query["provider_parameters"]["cache_hit"] is False
        assert event.clocks.market_reference_at == frame.index[-1].to_pydatetime()
        assert event.payload["query_sha256"] == event.query_sha256
        assert len(event.payload["rows"]) == 2
        coverage = coordinator.build_stream_coverage(
            CaptureStream.PROVIDER_OHLCV
        )
        assert coverage.provider == "mixed_query_receipt"
        assert coverage.symbol is None
        assert coverage.event_count == 2
        assert coverage.query_receipt_count == 2
        assert bridge.network_fallback_allowed is False
        with pytest.raises(CaptureContractError, match="outside its installed"):
            bridge.on_ohlcv_result(
                ticker="SPY",
                interval="1d",
                period="3mo",
                requested_at=BASE + timedelta(seconds=7),
                returned_at=BASE + timedelta(seconds=8),
                allow_provider_fallback=True,
                frame=frame,
            )
    finally:
        if coordinator.state is CaptureSessionState.RUNNING:
            coordinator.abort(reason="ohlcv_bridge_test_complete")


def test_ohlcv_capture_bridge_records_gap_for_ambiguous_market_clock(
    tmp_path,
) -> None:
    coordinator, _startup, wall = _coordinator(
        tmp_path / "ohlcv-bridge-gap",
        extra_streams=(CaptureStream.PROVIDER_OHLCV,),
    )
    wall.set(BASE + timedelta(seconds=10))
    bridge = LiveOhlcvCaptureBridge(
        coordinator=coordinator,
        decision_id="ohlcv-decision-gap",
        macro_cache={},
    )
    frame = pd.DataFrame(
        {
            "Open": [10.0],
            "High": [10.2],
            "Low": [9.9],
            "Close": [10.1],
            "Volume": [1_000.0],
        },
        index=pd.DatetimeIndex([datetime(2026, 7, 15)]),
    )
    frame.attrs.update(
        {
            "provider": "massive",
            "fetched_at_utc": (BASE + timedelta(seconds=6)).isoformat(),
            "ticker": "SPY",
            "interval": "1d",
            "integrity_ok": True,
            "cache_hit": False,
            "cache_age_seconds": 0.0,
        }
    )
    try:
        before = coordinator.health()["rejected_or_reported_lost_count"]
        with bridge.install():
            accepted = bridge.on_ohlcv_result(
                ticker="SPY",
                interval="1d",
                period="3mo",
                requested_at=BASE + timedelta(seconds=7),
                returned_at=BASE + timedelta(seconds=8),
                allow_provider_fallback=True,
                frame=frame,
            )
        assert accepted is False
        assert bridge.captured_reads == ()
        assert coordinator.health()["rejected_or_reported_lost_count"] > before
    finally:
        if coordinator.state is CaptureSessionState.RUNNING:
            coordinator.abort(reason="ohlcv_bridge_gap_test_complete")


def test_scanner_capture_bridge_receipts_exact_projection_and_deduplicates(
    tmp_path,
) -> None:
    coordinator, _startup, wall = _coordinator(
        tmp_path / "scanner-bridge",
        extra_streams=(CaptureStream.SCANNER_SNAPSHOT,),
    )
    wall.set(BASE + timedelta(seconds=10))
    profile = CaptureScannerProfile(
        profile_id="equity_ross_smallcap",
        asset_class="equity",
        price_min=1.0,
        price_max=20.0,
        min_dollar_volume=1_000_000.0,
        min_change_pct=5.0,
        snapshot_max_age_seconds=300.0,
    )
    bridge = LiveScannerSnapshotCaptureBridge(
        coordinator=coordinator,
        decision_id="scanner-decision-1",
        profile=profile,
    )
    market_at = BASE + timedelta(seconds=5)
    rows = [
        {"ticker": "OTHER", "updated": _epoch_ns(market_at)},
        {
            "ticker": "VEEE",
            "todaysChangePerc": 31.0,
            "updated": _epoch_ns(market_at),
            "lastTrade": {"p": 4.20, "t": _epoch_ns(market_at)},
            "day": {"c": 4.05, "vw": 3.95, "v": 500_000.0},
            "min": {"c": 4.18, "av": 600_000.0},
        },
    ]
    try:
        with bridge.install():
            assert bridge.on_massive_full_snapshot(
                include_otc=False,
                max_age_seconds=300.0,
                provider_cache_ttl_seconds=300.0,
                requested_at=BASE + timedelta(seconds=6),
                returned_at=BASE + timedelta(seconds=7),
                cache_hit=False,
                cache_age_seconds=None,
                rows=rows,
            )
            assert bridge.on_massive_full_snapshot(
                include_otc=False,
                max_age_seconds=300.0,
                provider_cache_ttl_seconds=300.0,
                requested_at=BASE + timedelta(seconds=7, milliseconds=100),
                returned_at=BASE + timedelta(seconds=8),
                cache_hit=True,
                cache_age_seconds=1.0,
                rows=rows,
            )

        captured = bridge.captured_reads
        assert len(captured) == 2
        assert captured[0].durable and captured[1].durable
        assert captured[0].source_events[0].event_sha256 == (
            captured[1].source_events[0].event_sha256
        )
        typed = CaptureScannerSnapshot.from_event(
            captured[0].source_events[0]
        )
        assert typed.event.clocks.market_reference_at == market_at
        assert typed.source_projection["updated"] == _epoch_ns(market_at)
        assert typed.price == pytest.approx(4.20)
        assert typed.dollar_volume == pytest.approx(2_520_000.0)
        assert coordinator.health()["change_key_count"] == 1

        with pytest.raises(
            CaptureContractError,
            match="outside its installed runner scope",
        ):
            bridge.on_massive_full_snapshot(
                include_otc=False,
                max_age_seconds=300.0,
                provider_cache_ttl_seconds=300.0,
                requested_at=BASE + timedelta(seconds=8),
                returned_at=BASE + timedelta(seconds=9),
                cache_hit=True,
                cache_age_seconds=2.0,
                rows=rows,
            )
    finally:
        if coordinator.state is CaptureSessionState.RUNNING:
            coordinator.abort(reason="scanner_bridge_test_complete")


def test_scanner_capture_bridge_records_gap_and_rejects_unclocked_projection(
    tmp_path,
) -> None:
    coordinator, _startup, wall = _coordinator(
        tmp_path / "scanner-bridge-gap",
        extra_streams=(CaptureStream.SCANNER_SNAPSHOT,),
    )
    wall.set(BASE + timedelta(seconds=10))
    profile = CaptureScannerProfile(
        profile_id="equity_ross_smallcap",
        asset_class="equity",
        price_min=1.0,
        price_max=20.0,
        min_dollar_volume=1_000_000.0,
        min_change_pct=5.0,
        snapshot_max_age_seconds=300.0,
    )
    bridge = LiveScannerSnapshotCaptureBridge(
        coordinator=coordinator,
        decision_id="scanner-decision-gap",
        profile=profile,
    )
    try:
        before = coordinator.health()["rejected_or_reported_lost_count"]
        with bridge.install():
            accepted = bridge.on_massive_full_snapshot(
                include_otc=False,
                max_age_seconds=300.0,
                provider_cache_ttl_seconds=300.0,
                requested_at=BASE + timedelta(seconds=6),
                returned_at=BASE + timedelta(seconds=7),
                cache_hit=False,
                cache_age_seconds=None,
                rows=[
                    {
                        "ticker": "VEEE",
                        "todaysChangePerc": 31.0,
                        "updated": None,
                        "lastTrade": {"p": 4.20, "t": None},
                        "day": {"c": 4.05, "vw": 3.95, "v": 500_000.0},
                        "min": {"c": 4.18, "av": 600_000.0},
                    }
                ],
            )
        assert accepted is False
        assert bridge.captured_reads == ()
        assert (
            coordinator.health()["rejected_or_reported_lost_count"]
            > before
        )
    finally:
        if coordinator.state is CaptureSessionState.RUNNING:
            coordinator.abort(reason="scanner_bridge_gap_test_complete")
