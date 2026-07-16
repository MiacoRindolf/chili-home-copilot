from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import uuid

import pytest

from app.services.trading.momentum_neural.replay_capture_contract import (
    CAPTURE_PRODUCER_ROSTER_SCHEMA_VERSION,
    CaptureClocks,
    CaptureContractError,
    CaptureCoverageManifest,
    CaptureDecisionAction,
    CaptureDecisionCheckpoint,
    CaptureDecisionOutput,
    CaptureEvent,
    CaptureEventRef,
    CaptureReadReceipt,
    CaptureRunIdentity,
    CaptureStream,
    CoverageGap,
    DeterministicDualClockLoader,
    FSM_DEPENDENCY_PROFILE_SCHEMA_VERSION,
    ProviderWatermark,
    ReplayCoverageRequest,
    StreamCoverage,
    VerifiedReplayCapture,
    capture_prefix_root_sha256,
    captured_read_result_sha256,
    grade_replay_coverage,
    resolve_capture_source_payload,
    sha256_json,
)
from app.services.trading.momentum_neural.replay_capture_runtime import (
    BoundedCaptureIngress,
    CaptureBudgetPolicy,
    CaptureResourceBinding,
    CaptureResourceMeasurement,
    CaptureWriterWorker,
    ContentAddressedCaptureStore,
)


UTC = timezone.utc
BASE = datetime(2026, 7, 13, 12, 50, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


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


def _identity() -> CaptureRunIdentity:
    return CaptureRunIdentity(
        run_id=str(uuid.UUID("00000000-0000-0000-0000-000000000013")),
        generation=3,
        code_build_sha256=SHA_A,
        config_sha256=SHA_B,
        feature_flags_sha256=SHA_C,
        account_identity_sha256=SHA_D,
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


def _event(
    stream: CaptureStream,
    *,
    sequence: int,
    event_at: datetime | None,
    available_at: datetime,
    market_reference_at: datetime | None = None,
    query: dict | None = None,
    symbol: str | None = "VEEE",
    provider: str = "fixture",
    payload: dict | None = None,
    identity: CaptureRunIdentity | None = None,
) -> CaptureEvent:
    return CaptureEvent(
        identity=identity or _identity(),
        sequence=sequence,
        stream=stream,
        symbol=symbol,
        provider=provider,
        clocks=CaptureClocks(
            provider_event_at=event_at,
            market_reference_at=market_reference_at,
            received_at=available_at - timedelta(milliseconds=2),
            available_at=available_at,
        ),
        query=query,
        payload=payload or {"sequence": sequence, "stream": stream.value},
    )


def test_exact_quote_event_clock_preserves_missing_truth_without_proxy() -> None:
    event = _event(
        CaptureStream.NBBO_QUOTE,
        sequence=1,
        event_at=None,
        market_reference_at=BASE,
        available_at=BASE + timedelta(milliseconds=10),
    )

    # Ingestion preserves the provider's honest absence.  It must neither
    # substitute the market/trade reference clock nor discard the observation;
    # exact-clock coverage is graded unavailable later.
    assert event.clocks.provider_event_at is None
    assert event.clocks.market_reference_at == BASE
    record = event.to_record(include_payload=True)
    restored = CaptureEvent.from_record(record)
    assert restored.clocks.provider_event_at is None
    assert restored.clocks.market_reference_at == BASE
    assert restored.to_record(include_payload=True) == record
    assert restored.event_sha256 == event.event_sha256


def _promoted_source_event(
    *,
    payload_updates: dict | None = None,
    remove_release: bool = False,
) -> tuple[CaptureEvent, dict]:
    original_available_at = BASE + timedelta(milliseconds=10)
    promoted_at = BASE + timedelta(milliseconds=20)
    promotion_id = "4c74656f-4ec5-4c40-b37d-95063c262b17"
    source_payload = {
        "schema_version": "fixture.source.v1",
        "symbol": "VEEE",
        "value": 7,
    }
    payload = {
        **source_payload,
        "_capture_promotion": {
            "promotion_id": promotion_id,
            "promoted_at": promoted_at.isoformat().replace("+00:00", "Z"),
            "promotion_order": 1,
            "original_provisional_available_at": (
                original_available_at.isoformat().replace("+00:00", "Z")
            ),
            "provisional_event_sha256": "1" * 64,
            "source_identity_sha256": "2" * 64,
            "inventory_sha256": "3" * 64,
        },
        "_capture_release": {
            "original_available_at": (
                original_available_at.isoformat().replace("+00:00", "Z")
            ),
            "released_available_at": promoted_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "release_kind": "hot_symbol_promotion",
            "promotion_id": promotion_id,
            "promoted_at": promoted_at.isoformat().replace("+00:00", "Z"),
            "source_identity_sha256": "2" * 64,
            "resource_binding_sha256": "4" * 64,
            "inventory_sha256": "3" * 64,
            "admission_handoff_sha256": "5" * 64,
        },
    }
    if payload_updates:
        for section, values in payload_updates.items():
            if section in {"_capture_release", "_capture_promotion"}:
                payload[section].update(values)
            else:
                payload[section] = values
    if remove_release:
        payload.pop("_capture_release")
    event = CaptureEvent(
        identity=_identity(),
        sequence=1,
        stream=CaptureStream.NBBO_QUOTE,
        symbol="VEEE",
        provider="fixture",
        clocks=CaptureClocks(
            provider_event_at=BASE,
            received_at=BASE + timedelta(milliseconds=5),
            available_at=promoted_at,
        ),
        payload=payload,
    )
    return event, source_payload


def test_capture_source_payload_unwraps_only_hash_bound_promotion_metadata() -> None:
    event, source_payload = _promoted_source_event()

    view = resolve_capture_source_payload(event)

    assert dict(view.payload) == source_payload
    assert view.original_available_at == BASE + timedelta(milliseconds=10)
    assert view.release_kind == "hot_symbol_promotion"
    assert view.promotion_id == "4c74656f-4ec5-4c40-b37d-95063c262b17"
    assert view.promotion_order == 1
    assert view.provisional_event_sha256 == "1" * 64
    assert view.source_identity_sha256 == "2" * 64
    assert view.resource_binding_sha256 == "4" * 64
    assert view.inventory_sha256 == "3" * 64
    assert view.admission_handoff_sha256 == "5" * 64
    with pytest.raises(TypeError, match="immutable"):
        view.payload["value"] = 8


@pytest.mark.parametrize(
    ("payload_updates", "remove_release", "error"),
    (
        (
            {"_capture_release": {"released_available_at": "2026-07-13T12:50:01Z"}},
            False,
            "release clocks",
        ),
        (
            {"_capture_release": {"source_identity_sha256": "6" * 64}},
            False,
            "provenance mismatch",
        ),
        (
            {"_capture_promotion": {"inventory_sha256": "7" * 64}},
            False,
            "provenance mismatch",
        ),
        (
            {"_capture_promotion": {"promotion_order": True}},
            False,
            "promotion order",
        ),
        (
            {"_capture_release": {"unknown_field": "forged"}},
            False,
            "fields do not match schema",
        ),
        ({}, True, "release metadata"),
    ),
)
def test_capture_source_payload_rejects_forged_runtime_metadata(
    payload_updates: dict,
    remove_release: bool,
    error: str,
) -> None:
    event, _source_payload = _promoted_source_event(
        payload_updates=payload_updates,
        remove_release=remove_release,
    )

    with pytest.raises(CaptureContractError, match=error):
        resolve_capture_source_payload(event)


def test_queried_ohlcv_requires_exact_query_and_dual_clocks() -> None:
    with pytest.raises(CaptureContractError, match="query parameters"):
        _event(
            CaptureStream.PROVIDER_OHLCV,
            sequence=1,
            event_at=None,
            market_reference_at=BASE,
            available_at=BASE + timedelta(seconds=1),
        )

    event = _event(
        CaptureStream.PROVIDER_OHLCV,
        sequence=2,
        event_at=None,
        market_reference_at=BASE,
        available_at=BASE + timedelta(seconds=1),
        query={"symbol": "VEEE", "interval": "1m", "from": "2026-07-13"},
    )
    assert event.query_sha256 == sha256_json(event.query)
    assert event.clocks.market_reference_at == BASE


def test_scanner_snapshot_requires_exact_query_and_market_reference() -> None:
    with pytest.raises(CaptureContractError, match="query parameters"):
        _event(
            CaptureStream.SCANNER_SNAPSHOT,
            sequence=3,
            event_at=None,
            market_reference_at=BASE,
            available_at=BASE + timedelta(milliseconds=1),
        )

    event = _event(
        CaptureStream.SCANNER_SNAPSHOT,
        sequence=4,
        event_at=None,
        market_reference_at=BASE,
        available_at=BASE + timedelta(milliseconds=1),
        query={"include_otc": False, "max_age_seconds": 300.0},
    )

    assert event.query_sha256 == sha256_json(event.query)
    assert event.clocks.market_reference_at == BASE


def test_capture_event_deep_freezes_hashed_payload_and_requires_addresses() -> None:
    payload = {"nested": {"values": [1, 2]}}
    event = CaptureEvent(
        identity=_identity(),
        sequence=9,
        stream=CaptureStream.NBBO_QUOTE,
        symbol="VEEE",
        provider="fixture",
        clocks=CaptureClocks(
            provider_event_at=BASE,
            received_at=BASE + timedelta(milliseconds=1),
            available_at=BASE + timedelta(milliseconds=2),
        ),
        payload=payload,
    )
    digest = event.event_sha256

    payload["nested"]["values"].append(3)
    assert event.event_sha256 == digest
    assert event.payload["nested"]["values"] == [1, 2]
    with pytest.raises(TypeError, match="immutable"):
        event.payload["nested"]["values"].append(4)

    record = event.to_record(include_payload=True)
    record.pop("payload_sha256")
    with pytest.raises(CaptureContractError, match="payload_sha256"):
        CaptureEvent.from_record(record)


def test_dual_clock_loader_releases_by_availability_not_market_time() -> None:
    early_market_late_arrival = _event(
        CaptureStream.NBBO_QUOTE,
        sequence=1,
        event_at=BASE,
        available_at=BASE + timedelta(seconds=5),
    )
    late_market_early_arrival = _event(
        CaptureStream.NBBO_QUOTE,
        sequence=2,
        event_at=BASE + timedelta(seconds=2),
        available_at=BASE + timedelta(seconds=3),
    )
    loader = DeterministicDualClockLoader(
        [early_market_late_arrival, late_market_early_arrival]
    )

    assert loader.advance_to(BASE + timedelta(seconds=4)) == (
        late_market_early_arrival,
    )
    assert loader.advance_to(BASE + timedelta(seconds=5)) == (
        early_market_late_arrival,
    )
    with pytest.raises(CaptureContractError, match="move backwards"):
        loader.advance_to(BASE)


def _watermark(
    stream: CaptureStream,
    exit_at: datetime,
    *,
    identity: CaptureRunIdentity | None = None,
) -> ProviderWatermark:
    capture_identity = identity or _identity()
    return ProviderWatermark(
        stream=stream,
        provider="fixture",
        identity_sha256=capture_identity.identity_sha256,
        event_watermark_at=exit_at + timedelta(seconds=1),
        emitted_available_at=exit_at + timedelta(seconds=2),
        bounded_lateness_seconds=2.0,
        max_observed_lateness_seconds=0.25,
        generation=3,
        symbol="VEEE",
    )


def _coverage(
    stream: CaptureStream,
    *,
    warmup: datetime,
    exit_at: datetime,
    continuous: bool = False,
    query_receipts: int = 0,
    event_count: int = 1,
    symbol: str | None = None,
    identity: CaptureRunIdentity | None = None,
) -> StreamCoverage:
    capture_identity = identity or _identity()
    return StreamCoverage(
        stream=stream,
        identity_sha256=capture_identity.identity_sha256,
        provider="fixture",
        first_available_at=warmup - timedelta(seconds=1),
        last_available_at=exit_at + timedelta(seconds=1),
        event_count=event_count,
        exact_event_clock_complete=stream is CaptureStream.NBBO_QUOTE,
        content_verified=True,
        continuity_complete=True,
        watermark=(
            replace(
                _watermark(stream, exit_at, identity=capture_identity),
                symbol=symbol,
            )
            if continuous
            else None
        ),
        query_receipt_count=query_receipts,
        symbol=symbol,
    )


def _receipt(
    stream: CaptureStream,
    *,
    read_id: str,
    source: CaptureEventRef,
    requested_at: datetime,
    returned_at: datetime,
) -> CaptureReadReceipt:
    return CaptureReadReceipt(
        read_id=read_id,
        decision_id="veee-entry-1",
        identity_sha256=source.identity_sha256,
        stream=stream,
        provider=source.provider,
        symbol=source.symbol,
        requested_at=requested_at,
        returned_at=returned_at,
        query_sha256=(
            str(source.query_sha256)
            if source.query_sha256 is not None
            else sha256_json({"stream": stream.value, "read_id": read_id})
        ),
        source_event_sha256s=(source.event_sha256,),
        empty_result=False,
        result_sha256=captured_read_result_sha256((source,)),
    )


def _passing_manifest(
    *,
    include_events: bool = False,
    scanner_watermark_frontier: str | None = None,
) -> (
    tuple[ReplayCoverageRequest, CaptureCoverageManifest]
    | tuple[ReplayCoverageRequest, CaptureCoverageManifest, tuple[CaptureEvent, ...]]
):
    warmup = BASE
    decision = BASE + timedelta(minutes=10)
    exit_at = BASE + timedelta(minutes=20)
    nbbo_read = str(uuid.UUID("00000000-0000-0000-0000-000000000100"))
    ohlcv_read = str(uuid.UUID("00000000-0000-0000-0000-000000000101"))
    account_read = str(uuid.UUID("00000000-0000-0000-0000-000000000102"))
    config_payload = {"effective_config": "fixture"}
    flags_payload = {"feature_flags": "fixture"}
    code_payload = {"code_build": "fixture"}
    identity = replace(
        _identity(),
        config_sha256=sha256_json(config_payload),
        feature_flags_sha256=sha256_json(flags_payload),
        code_build_sha256=sha256_json(code_payload),
    )
    sequence_offset = 1 if scanner_watermark_frontier is not None else 0
    events = (
        [
            _event(
                CaptureStream.SCANNER_SNAPSHOT,
                sequence=1,
                event_at=None,
                market_reference_at=warmup - timedelta(seconds=2),
                available_at=warmup - timedelta(seconds=1),
                query={"include_otc": False, "max_age_seconds": 300.0},
                identity=identity,
            )
        ]
        if scanner_watermark_frontier is not None
        else []
    ) + [
        _event(
            CaptureStream.NBBO_QUOTE,
            sequence=1 + sequence_offset,
            event_at=warmup - timedelta(seconds=1),
            available_at=warmup - timedelta(seconds=1),
            identity=identity,
        ),
        _event(
            CaptureStream.CONFIG_SNAPSHOT,
            sequence=2 + sequence_offset,
            event_at=None,
            available_at=warmup - timedelta(milliseconds=900),
            symbol=None,
            payload=config_payload,
            identity=identity,
        ),
        _event(
            CaptureStream.FEATURE_FLAG_SNAPSHOT,
            sequence=3 + sequence_offset,
            event_at=None,
            available_at=warmup - timedelta(milliseconds=800),
            symbol=None,
            payload=flags_payload,
            identity=identity,
        ),
        _event(
            CaptureStream.CODE_BUILD,
            sequence=4 + sequence_offset,
            event_at=None,
            available_at=warmup - timedelta(milliseconds=700),
            symbol=None,
            payload=code_payload,
            identity=identity,
        ),
        _event(
            CaptureStream.PROVIDER_OHLCV,
            sequence=5 + sequence_offset,
            event_at=None,
            market_reference_at=decision - timedelta(minutes=1),
            available_at=decision - timedelta(milliseconds=10),
            query={"symbol": "VEEE", "interval": "1m"},
            identity=identity,
        ),
        _event(
            CaptureStream.ACCOUNT_RISK_SNAPSHOT,
            sequence=6 + sequence_offset,
            event_at=None,
            market_reference_at=decision - timedelta(milliseconds=9),
            available_at=decision - timedelta(milliseconds=8),
            query={"account": "paper", "fields": ["equity", "buying_power"]},
            symbol=None,
            identity=identity,
        ),
    ]
    source_refs = tuple(CaptureEventRef.from_event(event) for event in events)
    source_ref_by_stream = {ref.stream: ref for ref in source_refs}
    receipts = (
        _receipt(
            CaptureStream.NBBO_QUOTE,
            read_id=nbbo_read,
            source=source_ref_by_stream[CaptureStream.NBBO_QUOTE],
            requested_at=warmup,
            returned_at=warmup + timedelta(milliseconds=1),
        ),
        _receipt(
            CaptureStream.PROVIDER_OHLCV,
            read_id=ohlcv_read,
            source=source_ref_by_stream[CaptureStream.PROVIDER_OHLCV],
            requested_at=decision - timedelta(milliseconds=12),
            returned_at=decision - timedelta(milliseconds=9),
        ),
        _receipt(
            CaptureStream.ACCOUNT_RISK_SNAPSHOT,
            read_id=account_read,
            source=source_ref_by_stream[CaptureStream.ACCOUNT_RISK_SNAPSHOT],
            requested_at=decision - timedelta(milliseconds=9),
            returned_at=decision - timedelta(milliseconds=7),
        ),
    )
    for sequence, (receipt, available_at) in enumerate(
        (
            (receipts[0], decision - timedelta(microseconds=7_900)),
            (receipts[1], decision - timedelta(microseconds=7_500)),
            (receipts[2], decision - timedelta(milliseconds=7)),
        ),
        start=7 + sequence_offset,
    ):
        events.append(
            CaptureEvent(
                identity=identity,
                sequence=sequence,
                stream=CaptureStream.READ_RECEIPT,
                provider="chili_capture",
                symbol=None,
                clocks=CaptureClocks(
                    received_at=available_at - timedelta(microseconds=1),
                    available_at=available_at,
                ),
                payload=receipt.to_dict(),
            )
        )
    prefix_refs = tuple(CaptureEventRef.from_event(event) for event in events)
    prefix_root = capture_prefix_root_sha256(
        prefix_refs,
        identity_sha256=identity.identity_sha256,
        through_sequence=9 + sequence_offset,
    )
    decision_output = _abstain_decision_output("veee-entry-1", "VEEE")
    decision_payload = {
        "decision_id": "veee-entry-1",
        "symbol": "VEEE",
        "decision_at": decision.isoformat().replace("+00:00", "Z"),
        "input_prefix_sequence": 9 + sequence_offset,
        "input_prefix_root_sha256": prefix_root,
        "required_read_ids": sorted([nbbo_read, ohlcv_read, account_read]),
        "fsm_dependency_profile": {
            "schema_version": FSM_DEPENDENCY_PROFILE_SCHEMA_VERSION,
            "required_streams": sorted(
                [
                    CaptureStream.ACCOUNT_RISK_SNAPSHOT.value,
                    CaptureStream.NBBO_QUOTE.value,
                    CaptureStream.PROVIDER_OHLCV.value,
                ]
            ),
            "required_read_ids": sorted([nbbo_read, ohlcv_read, account_read]),
            "stream_dependencies": [
                {
                    "stream": CaptureStream.ACCOUNT_RISK_SNAPSHOT.value,
                    "exact_provider_event_at_required": False,
                    "market_reference_at_required": True,
                    "max_source_age_seconds": 1.0,
                    "coverage_start_at": warmup.isoformat().replace("+00:00", "Z"),
                },
                {
                    "stream": CaptureStream.NBBO_QUOTE.value,
                    "exact_provider_event_at_required": True,
                    "market_reference_at_required": False,
                    "max_source_age_seconds": 1_000.0,
                    "coverage_start_at": warmup.isoformat().replace("+00:00", "Z"),
                },
                {
                    "stream": CaptureStream.PROVIDER_OHLCV.value,
                    "exact_provider_event_at_required": False,
                    "market_reference_at_required": True,
                    "max_source_age_seconds": 120.0,
                    "coverage_start_at": warmup.isoformat().replace("+00:00", "Z"),
                },
            ],
        },
        "decision_output": decision_output.to_dict(),
        "decision_output_sha256": decision_output.decision_output_sha256,
    }
    decision_event = _event(
        CaptureStream.FSM_DECISION,
        sequence=10 + sequence_offset,
        event_at=None,
        market_reference_at=decision,
        available_at=decision + timedelta(milliseconds=1),
        payload=decision_payload,
        identity=identity,
    )
    exit_quote = _event(
        CaptureStream.NBBO_QUOTE,
        sequence=11 + sequence_offset,
        event_at=exit_at,
        available_at=exit_at + timedelta(seconds=1),
        identity=identity,
    )
    events.extend((decision_event, exit_quote))
    event_refs = tuple(CaptureEventRef.from_event(event) for event in events)
    event_index = {ref.event_sha256: ref for ref in event_refs}
    checkpoint = CaptureDecisionCheckpoint(
        identity_sha256=identity.identity_sha256,
        decision_id="veee-entry-1",
        symbol="VEEE",
        decision_at=decision,
        available_at=decision_event.clocks.available_at,
        decision_event_sha256=decision_event.event_sha256,
        input_prefix_sequence=9 + sequence_offset,
        input_prefix_root_sha256=prefix_root,
        required_read_ids=(nbbo_read, ohlcv_read, account_read),
        decision_payload=decision_payload,
    )
    request = ReplayCoverageRequest(
        warmup_start_at=warmup,
        decision_at=decision,
        exit_end_at=exit_at,
        required_streams=frozenset(
            {
                CaptureStream.ACCOUNT_RISK_SNAPSHOT,
                CaptureStream.NBBO_QUOTE,
                CaptureStream.PROVIDER_OHLCV,
            }
            | (
                {CaptureStream.SCANNER_SNAPSHOT}
                if scanner_watermark_frontier is not None
                else set()
            )
        ),
        decision_id="veee-entry-1",
        decision_checkpoint_sha256=checkpoint.checkpoint_sha256,
        required_read_ids=frozenset({nbbo_read, ohlcv_read, account_read}),
        symbol="VEEE",
        expected_identity_sha256=identity.identity_sha256,
    )
    coverages = {
        CaptureStream.NBBO_QUOTE: _coverage(
            CaptureStream.NBBO_QUOTE,
            warmup=warmup,
            exit_at=exit_at,
            continuous=True,
            event_count=2,
            symbol="VEEE",
            identity=identity,
        ),
        CaptureStream.PROVIDER_OHLCV: _coverage(
            CaptureStream.PROVIDER_OHLCV,
            warmup=warmup,
            exit_at=exit_at,
            query_receipts=1,
            symbol="VEEE",
            identity=identity,
        ),
        CaptureStream.ACCOUNT_RISK_SNAPSHOT: _coverage(
            CaptureStream.ACCOUNT_RISK_SNAPSHOT,
            warmup=warmup,
            exit_at=exit_at,
            query_receipts=1,
            identity=identity,
        ),
        CaptureStream.CONFIG_SNAPSHOT: _coverage(
            CaptureStream.CONFIG_SNAPSHOT,
            warmup=warmup,
            exit_at=exit_at,
            identity=identity,
        ),
        CaptureStream.FEATURE_FLAG_SNAPSHOT: _coverage(
            CaptureStream.FEATURE_FLAG_SNAPSHOT,
            warmup=warmup,
            exit_at=exit_at,
            identity=identity,
        ),
        CaptureStream.CODE_BUILD: _coverage(
            CaptureStream.CODE_BUILD,
            warmup=warmup,
            exit_at=exit_at,
            identity=identity,
        ),
    }
    if scanner_watermark_frontier is not None:
        scanner_source = next(
            event
            for event in events
            if event.stream is CaptureStream.SCANNER_SNAPSHOT
        )
        scanner_watermark = None
        if scanner_watermark_frontier != "missing":
            scanner_watermark = _watermark(
                CaptureStream.SCANNER_SNAPSHOT,
                exit_at,
                identity=identity,
            )
            if scanner_watermark_frontier == "stale":
                scanner_watermark = replace(
                    scanner_watermark,
                    event_watermark_at=exit_at - timedelta(microseconds=1),
                )
        coverages[CaptureStream.SCANNER_SNAPSHOT] = StreamCoverage(
            stream=CaptureStream.SCANNER_SNAPSHOT,
            identity_sha256=identity.identity_sha256,
            provider="fixture",
            symbol="VEEE",
            first_available_at=scanner_source.clocks.available_at,
            last_available_at=scanner_source.clocks.available_at,
            event_count=1,
            exact_event_clock_complete=False,
            content_verified=True,
            continuity_complete=True,
            watermark=scanner_watermark,
        )
    manifest = CaptureCoverageManifest(
        identity=identity,
        event_index=event_index,
        decision_checkpoints=(checkpoint,),
        stream_coverage=coverages,
        read_receipts=receipts,
        gaps=(),
        closed_cleanly=True,
        content_root_verified=True,
        replay_network_fallback_count=0,
        required_streams_full_fidelity=True,
        created_at=exit_at + timedelta(minutes=1),
    )
    if include_events:
        return request, manifest, tuple(events)
    return request, manifest


def _scanner_change_log_fixture(
    *,
    watermark_frontier: str,
) -> tuple[ReplayCoverageRequest, CaptureCoverageManifest, str]:
    request, manifest = _passing_manifest(
        scanner_watermark_frontier=watermark_frontier
    )
    source_ref = next(
        ref
        for ref in manifest.event_index.values()
        if ref.stream is CaptureStream.SCANNER_SNAPSHOT
    )
    return request, manifest, source_ref.event_sha256


def _sealed_passing_manifest(
    tmp_path,
    *,
    include_producer_roster: bool = False,
    resource_bound: bool = False,
) -> tuple[
    ReplayCoverageRequest,
    CaptureCoverageManifest,
    VerifiedReplayCapture,
]:
    request, logical, source_events = _passing_manifest(include_events=True)
    events = list(source_events)
    available_at = request.exit_end_at + timedelta(seconds=3)

    def append_control(stream: CaptureStream, payload: dict) -> None:
        nonlocal available_at
        available_at += timedelta(microseconds=1)
        events.append(
            CaptureEvent(
                identity=logical.identity,
                sequence=len(events) + 1,
                stream=stream,
                provider="chili_capture",
                symbol=None,
                clocks=CaptureClocks(
                    received_at=available_at - timedelta(microseconds=1),
                    available_at=available_at,
                ),
                payload=payload,
            )
        )

    for coverage in logical.stream_coverage.values():
        if coverage.watermark is not None:
            append_control(
                CaptureStream.PROVIDER_WATERMARK,
                coverage.watermark.to_dict(),
            )
        append_control(CaptureStream.CAPTURE_HEALTH, coverage.to_dict())
    if include_producer_roster:
        append_control(
            CaptureStream.CAPTURE_HEALTH,
            {
                "schema_version": CAPTURE_PRODUCER_ROSTER_SCHEMA_VERSION,
                "run_state": "RUN_CLOSING",
                "producer_roster_complete": True,
                "expected_producers": ["fixture_capture"],
                "closed_producers": ["fixture_capture"],
                "required_streams": sorted(
                    stream.value for stream in logical.stream_coverage
                ),
                "required_streams_full_fidelity": True,
            },
        )

    binding = _resource_binding() if resource_bound else None
    store = ContentAddressedCaptureStore(
        tmp_path / "sealed-capture",
        compression_codec="zlib",
        resource_binding=binding,
    )
    ingress = (
        BoundedCaptureIngress.from_resource_binding(binding)
        if binding is not None
        else BoundedCaptureIngress(
            max_events=len(events), max_bytes=5_000_000, max_gap_keys=64
        )
    )
    for event in events:
        assert ingress.submit(event)
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=100,
        batch_bytes=(
            binding.budget.async_queue_bytes
            if binding is not None
            else 5_000_000
        ),
        poll_seconds=0.001,
        flush_interval_seconds=0.01,
    )
    worker.start()
    assert worker.stop(timeout_seconds=5)
    seal = worker.seal_run(logical.identity)
    verified = VerifiedReplayCapture.load_sealed_run(
        store,
        logical.identity,
        expected_final_seal_sha256=seal.seal_sha256,
    )
    manifest = CaptureCoverageManifest.from_verified_capture(
        verified,
        decision_checkpoints=logical.decision_checkpoints,
        stream_coverage=logical.stream_coverage,
        read_receipts=logical.read_receipts,
    )
    return request, manifest, verified


def test_hand_built_complete_manifest_is_explicitly_diagnostic_only() -> None:
    request, manifest = _passing_manifest()
    grade = grade_replay_coverage(request, manifest)

    assert grade.replayable is False
    assert grade.grade == "coverage_unavailable"
    assert grade.reasons == ("sealed_capture_binding_missing",)
    assert len(grade.manifest_sha256) == 64


def test_exact_sealed_manifest_binds_but_stays_diagnostic_without_run_open(
    tmp_path,
) -> None:
    request, manifest, _verified = _sealed_passing_manifest(tmp_path)

    grade = grade_replay_coverage(request, manifest)

    assert grade.replayable is False
    assert grade.grade == "coverage_unavailable"
    assert "capture_run_open_or_producer_roster_unverified" in grade.reasons
    assert "capture_resource_binding_unverified" in grade.reasons
    assert "required_stream_capture_not_full_fidelity" in grade.reasons
    assert manifest.seal_binding is not None
    assert (
        manifest.seal_binding.expected_final_seal_sha256
        == manifest.seal_binding.final_seal_sha256
    )


def test_closing_only_producer_roster_assertion_cannot_certify(
    tmp_path,
) -> None:
    request, manifest, _verified = _sealed_passing_manifest(
        tmp_path, include_producer_roster=True
    )

    grade = grade_replay_coverage(request, manifest)

    assert grade.replayable is False
    assert "capture_run_open_or_producer_roster_unverified" in grade.reasons
    assert "capture_resource_binding_unverified" in grade.reasons
    assert "required_stream_capture_not_full_fidelity" in grade.reasons

    with pytest.raises(CaptureContractError, match="derived full-fidelity"):
        replace(
            manifest,
            certification_blockers=(),
            required_streams_full_fidelity=True,
        )


def test_v4_exact_resource_hashes_remove_only_the_resource_blocker(
    tmp_path,
) -> None:
    request, manifest, verified = _sealed_passing_manifest(
        tmp_path, resource_bound=True
    )

    grade = grade_replay_coverage(request, manifest)

    assert verified.resource_hashes == _resource_binding().hashes
    assert manifest.seal_binding is not None
    assert manifest.seal_binding.resource_hashes == verified.resource_hashes
    assert "capture_resource_binding_unverified" not in manifest.certification_blockers
    assert "capture_resource_binding_unverified" not in grade.reasons
    assert "capture_run_open_or_producer_roster_unverified" in grade.reasons
    assert "required_stream_capture_not_full_fidelity" in grade.reasons

    with pytest.raises(CaptureContractError, match="attestation"):
        replace(
            manifest.seal_binding,
            resource_policy_sha256="f" * 64,
        )


def test_verified_resource_hashes_cannot_be_rebased_after_store_load(tmp_path) -> None:
    _request, _manifest, verified = _sealed_passing_manifest(
        tmp_path, resource_bound=True
    )

    with pytest.raises(CaptureContractError, match="attestation"):
        replace(verified, resource_binding_sha256="f" * 64)


def test_legacy_binding_cannot_drop_resource_blocker_by_caller_assertion(
    tmp_path,
) -> None:
    _request, manifest, _verified = _sealed_passing_manifest(tmp_path)

    with pytest.raises(CaptureContractError, match="derived certification blockers"):
        replace(
            manifest,
            certification_blockers=(
                "capture_run_open_or_producer_roster_unverified",
            ),
        )


def test_verified_load_requires_the_exact_expected_final_seal_sha(tmp_path) -> None:
    _request, manifest, verified = _sealed_passing_manifest(tmp_path)
    assert manifest.seal_binding is not None
    store = ContentAddressedCaptureStore(
        tmp_path / "sealed-capture", compression_codec="zlib"
    )

    with pytest.raises(CaptureContractError, match="expected SHA"):
        VerifiedReplayCapture.load_sealed_run(
            store,
            verified.identity,
            expected_final_seal_sha256="f" * 64,
        )


def test_duck_typed_loader_cannot_self_attest_a_verified_capture() -> None:
    with pytest.raises(CaptureContractError, match="store-verified"):
        VerifiedReplayCapture.load_sealed_run(
            object(),
            _identity(),
            expected_final_seal_sha256="f" * 64,
        )


def test_verified_capture_cannot_drop_a_sealed_event_after_load(tmp_path) -> None:
    _request, _manifest, verified = _sealed_passing_manifest(tmp_path)

    with pytest.raises(CaptureContractError, match="inventory"):
        replace(verified, events=verified.events[:-1])


def test_verified_binding_cannot_be_rebased_after_factory_return(tmp_path) -> None:
    _request, manifest, _verified = _sealed_passing_manifest(tmp_path)
    assert manifest.seal_binding is not None

    with pytest.raises(CaptureContractError, match="attestation"):
        replace(
            manifest.seal_binding,
            control_evidence_sha256="e" * 64,
        )


def test_verified_capture_cannot_omit_a_sealed_gap_or_rebase_counts(
    tmp_path,
) -> None:
    identity = _identity()
    first = _event(
        CaptureStream.NBBO_QUOTE,
        sequence=1,
        event_at=BASE,
        available_at=BASE,
        identity=identity,
    )
    dropped = _event(
        CaptureStream.NBBO_QUOTE,
        sequence=2,
        event_at=BASE + timedelta(milliseconds=1),
        available_at=BASE + timedelta(milliseconds=1),
        identity=identity,
    )
    store = ContentAddressedCaptureStore(
        tmp_path / "gap-sealed-capture", compression_codec="zlib"
    )
    ingress = BoundedCaptureIngress(max_events=1, max_bytes=500_000, max_gap_keys=8)
    assert ingress.submit(first)
    assert not ingress.submit(dropped)
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=10,
        batch_bytes=500_000,
        poll_seconds=0.001,
        flush_interval_seconds=0.01,
    )
    worker.start()
    assert worker.stop(timeout_seconds=5)
    seal = worker.seal_run(identity)
    verified = VerifiedReplayCapture.load_sealed_run(
        store,
        identity,
        expected_final_seal_sha256=seal.seal_sha256,
    )
    assert len(verified.gaps) == 1

    with pytest.raises(CaptureContractError, match="inventory count"):
        replace(verified, gaps=())
    with pytest.raises(CaptureContractError, match="inventory count"):
        replace(verified, event_count=verified.event_count + 1)


def test_capture_prefix_rejects_a_missing_generation_sequence() -> None:
    identity = _identity()
    first = CaptureEventRef.from_event(
        _event(
            CaptureStream.NBBO_QUOTE,
            sequence=1,
            event_at=BASE,
            available_at=BASE,
            identity=identity,
        )
    )
    third = CaptureEventRef.from_event(
        _event(
            CaptureStream.NBBO_QUOTE,
            sequence=3,
            event_at=BASE + timedelta(milliseconds=2),
            available_at=BASE + timedelta(milliseconds=2),
            identity=identity,
        )
    )

    with pytest.raises(CaptureContractError, match="not contiguous"):
        capture_prefix_root_sha256(
            (first, third),
            identity_sha256=identity.identity_sha256,
            through_sequence=3,
        )


@pytest.mark.parametrize("control_kind", ["checkpoint", "receipt", "watermark"])
def test_unsealed_control_view_cannot_enter_a_verified_manifest(
    tmp_path, control_kind: str
) -> None:
    _request, manifest, verified = _sealed_passing_manifest(tmp_path)
    checkpoints = manifest.decision_checkpoints
    receipts = manifest.read_receipts
    coverage = dict(manifest.stream_coverage)
    if control_kind == "checkpoint":
        checkpoint = checkpoints[0]
        payload = dict(checkpoint.decision_payload)
        payload["unsealed_mutation"] = True
        checkpoints = (replace(checkpoint, decision_payload=payload),)
    elif control_kind == "receipt":
        receipts = (
            replace(receipts[0], result_sha256="f" * 64),
            *receipts[1:],
        )
    else:
        quote_coverage = coverage[CaptureStream.NBBO_QUOTE]
        assert quote_coverage.watermark is not None
        coverage[CaptureStream.NBBO_QUOTE] = replace(
            quote_coverage,
            watermark=replace(
                quote_coverage.watermark,
                max_observed_lateness_seconds=0.2,
            ),
        )

    with pytest.raises(CaptureContractError, match="not committed"):
        CaptureCoverageManifest.from_verified_capture(
            verified,
            decision_checkpoints=checkpoints,
            stream_coverage=coverage,
            read_receipts=receipts,
        )


def test_read_receipt_recorded_after_decision_cannot_be_backdated_into_prefix(
    tmp_path,
) -> None:
    request, logical, source_events = _passing_manifest(include_events=True)
    late_events = list(source_events)
    for index in (6, 7):
        event = late_events[index]
        available_at = request.decision_at + timedelta(seconds=index - 5)
        late_events[index] = replace(
            event,
            clocks=CaptureClocks(
                received_at=available_at - timedelta(microseconds=1),
                available_at=available_at,
            ),
        )

    store = ContentAddressedCaptureStore(
        tmp_path / "late-receipt-capture", compression_codec="zlib"
    )
    ingress = BoundedCaptureIngress(
        max_events=len(late_events), max_bytes=5_000_000, max_gap_keys=64
    )
    for event in late_events:
        assert ingress.submit(event)
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=100,
        batch_bytes=5_000_000,
        poll_seconds=0.001,
        flush_interval_seconds=0.01,
    )
    worker.start()
    assert worker.stop(timeout_seconds=5)
    seal = worker.seal_run(logical.identity)
    verified = VerifiedReplayCapture.load_sealed_run(
        store,
        logical.identity,
        expected_final_seal_sha256=seal.seal_sha256,
    )

    with pytest.raises(CaptureContractError, match="read receipt .*not committed"):
        CaptureCoverageManifest.from_verified_capture(
            verified,
            decision_checkpoints=logical.decision_checkpoints,
            stream_coverage=logical.stream_coverage,
            read_receipts=logical.read_receipts,
        )


def test_unrelated_query_receipt_cannot_satisfy_checkpoint_read(tmp_path) -> None:
    request, manifest, _verified = _sealed_passing_manifest(tmp_path)
    missing_required = next(
        receipt
        for receipt in manifest.read_receipts
        if receipt.stream is CaptureStream.PROVIDER_OHLCV
    )
    unrelated = replace(
        missing_required,
        read_id=str(uuid.UUID(int=998)),
    )
    # Use a diagnostic copy so grading, rather than the immutable seal binding,
    # can expose the decision-local receipt semantics directly.
    diagnostic = CaptureCoverageManifest(
        identity=manifest.identity,
        event_index=manifest.event_index,
        decision_checkpoints=manifest.decision_checkpoints,
        stream_coverage=manifest.stream_coverage,
        read_receipts=tuple(
            unrelated if receipt.read_id == missing_required.read_id else receipt
            for receipt in manifest.read_receipts
        ),
        gaps=manifest.gaps,
        closed_cleanly=True,
        content_root_verified=True,
        replay_network_fallback_count=0,
        required_streams_full_fidelity=True,
        created_at=manifest.created_at,
    )

    grade = grade_replay_coverage(request, diagnostic)

    assert f"read_receipt_missing:{missing_required.read_id}" in grade.reasons
    assert "query_receipt_missing:provider_ohlcv" in grade.reasons


def test_gap_or_network_fallback_makes_coverage_unavailable() -> None:
    request, manifest = _passing_manifest()
    gap = CoverageGap(
        stream=CaptureStream.NBBO_QUOTE,
        symbol="VEEE",
        reason="capture_queue_overflow",
        first_available_at=request.decision_at,
        last_available_at=request.decision_at + timedelta(milliseconds=1),
        lost_count=2,
    )
    failed = replace(
        manifest,
        gaps=(gap,),
        replay_network_fallback_count=1,
        required_streams_full_fidelity=False,
    )
    grade = grade_replay_coverage(request, failed)

    assert grade.replayable is False
    assert grade.grade == "coverage_unavailable"
    assert "replay_network_fallback_used" in grade.reasons
    assert "required_stream_capture_not_full_fidelity" in grade.reasons
    assert any(reason.startswith("coverage_gap:nbbo_quote") for reason in grade.reasons)


def test_generic_gap_ledger_overflow_blocks_every_symbol_and_stream() -> None:
    request, manifest = _passing_manifest()
    generic = CoverageGap(
        stream=CaptureStream.COVERAGE_GAP,
        symbol="OTHER",
        reason="gap_ledger_key_budget_overflow",
        first_available_at=request.warmup_start_at,
        last_available_at=request.exit_end_at,
        lost_count=9,
    )
    grade = grade_replay_coverage(request, replace(manifest, gaps=(generic,)))
    assert grade.replayable is False
    assert any(reason.startswith("coverage_gap:coverage_gap") for reason in grade.reasons)


def test_required_receipts_are_unique_and_bound_to_the_requested_decision() -> None:
    request, manifest = _passing_manifest()
    duplicate = next(
        receipt
        for receipt in manifest.read_receipts
        if receipt.stream is CaptureStream.PROVIDER_OHLCV
    )
    with pytest.raises(CaptureContractError, match="duplicate read receipt"):
        replace(manifest, read_receipts=(duplicate, duplicate))

    wrong_decision = replace(duplicate, decision_id="another-decision")
    failed = replace(
        manifest,
        read_receipts=tuple(
            wrong_decision if receipt.read_id == duplicate.read_id else receipt
            for receipt in manifest.read_receipts
        ),
    )
    grade = grade_replay_coverage(request, failed)

    assert grade.replayable is False
    assert (
        f"read_receipt_decision_mismatch:{wrong_decision.read_id}" in grade.reasons
    )
    assert "query_receipt_missing:provider_ohlcv" in grade.reasons


def test_checkpoint_blocks_symbol_substitution() -> None:
    request, manifest = _passing_manifest()
    grade = grade_replay_coverage(replace(request, symbol="PLSM"), manifest)

    assert grade.replayable is False
    assert "decision_checkpoint_symbol_mismatch" in grade.reasons
    assert "stream_symbol_mismatch:nbbo_quote" in grade.reasons


def test_decision_receipt_returned_after_decision_fails_closed() -> None:
    request, manifest = _passing_manifest()
    original = manifest.read_receipts[0]
    late = replace(
        original,
        requested_at=request.decision_at,
        returned_at=request.decision_at + timedelta(milliseconds=1),
    )
    failed = replace(
        manifest,
        read_receipts=tuple(
            late if receipt.read_id == original.read_id else receipt
            for receipt in manifest.read_receipts
        ),
    )
    grade = grade_replay_coverage(request, failed)

    assert grade.replayable is False
    assert f"read_receipt_outside_window:{late.read_id}" in grade.reasons


def test_recent_availability_cannot_launder_stale_market_reference_clock() -> None:
    request, manifest = _passing_manifest()
    receipt = next(
        row
        for row in manifest.read_receipts
        if row.stream is CaptureStream.ACCOUNT_RISK_SNAPSHOT
    )
    source_hash = receipt.source_event_sha256s[0]
    source_ref = manifest.event_index[source_hash]
    stale_ref = replace(
        source_ref,
        market_reference_at=request.decision_at - timedelta(seconds=10),
        # Keep release time recent: this is the laundering shape under test.
        available_at=request.decision_at - timedelta(milliseconds=8),
    )
    failed = replace(
        manifest,
        event_index={**manifest.event_index, source_hash: stale_ref},
    )

    grade = grade_replay_coverage(request, failed)

    assert f"read_receipt_source_stale:{receipt.read_id}" in grade.reasons


def test_future_market_reference_clock_fails_even_when_already_available() -> None:
    request, manifest = _passing_manifest()
    receipt = next(
        row
        for row in manifest.read_receipts
        if row.stream is CaptureStream.ACCOUNT_RISK_SNAPSHOT
    )
    source_hash = receipt.source_event_sha256s[0]
    source_ref = manifest.event_index[source_hash]
    future_ref = replace(
        source_ref,
        market_reference_at=request.decision_at + timedelta(milliseconds=1),
        available_at=request.decision_at - timedelta(milliseconds=8),
    )
    failed = replace(
        manifest,
        event_index={**manifest.event_index, source_hash: future_ref},
    )

    grade = grade_replay_coverage(request, failed)

    assert (
        f"read_receipt_source_clock_from_future:{receipt.read_id}"
        in grade.reasons
    )


def test_change_log_watermark_proves_unchanged_state_through_exit() -> None:
    request, manifest, _source_hash = _scanner_change_log_fixture(
        watermark_frontier="complete"
    )

    grade = grade_replay_coverage(request, manifest)

    assert grade.reasons == ("sealed_capture_binding_missing",)
    assert "exit_coverage_missing:scanner_snapshot" not in grade.reasons


@pytest.mark.parametrize(
    ("frontier", "expected_reason"),
    (
        ("missing", "provider_watermark_missing:scanner_snapshot"),
        ("stale", "provider_watermark_before_exit:scanner_snapshot"),
    ),
)
def test_change_log_requires_watermark_through_exit(
    frontier: str,
    expected_reason: str,
) -> None:
    request, manifest, _source_hash = _scanner_change_log_fixture(
        watermark_frontier=frontier
    )

    grade = grade_replay_coverage(request, manifest)

    assert expected_reason in grade.reasons


def test_change_log_rejects_forged_missing_source_clock() -> None:
    request, manifest, source_hash = _scanner_change_log_fixture(
        watermark_frontier="complete"
    )
    source_ref = manifest.event_index[source_hash]
    failed = replace(
        manifest,
        event_index={
            **manifest.event_index,
            source_hash: replace(
                source_ref,
                provider_event_at=None,
                market_reference_at=None,
            ),
        },
    )

    grade = grade_replay_coverage(request, failed)

    assert "change_log_source_clock_missing:scanner_snapshot" in grade.reasons


def test_change_log_rejects_future_source_as_missing_warmup_state() -> None:
    request, manifest, source_hash = _scanner_change_log_fixture(
        watermark_frontier="complete"
    )
    source_ref = manifest.event_index[source_hash]
    failed = replace(
        manifest,
        event_index={
            **manifest.event_index,
            source_hash: replace(
                source_ref,
                market_reference_at=request.warmup_start_at
                + timedelta(microseconds=1),
            ),
        },
    )

    grade = grade_replay_coverage(request, failed)

    assert "change_log_source_clock_from_future:scanner_snapshot" in grade.reasons
    assert "change_log_warmup_source_missing:scanner_snapshot" in grade.reasons


def test_change_log_coverage_bounds_must_match_sealed_event_inventory() -> None:
    request, manifest, _source_hash = _scanner_change_log_fixture(
        watermark_frontier="complete"
    )
    coverage = manifest.stream_coverage[CaptureStream.SCANNER_SNAPSHOT]
    failed = replace(
        manifest,
        stream_coverage={
            **manifest.stream_coverage,
            CaptureStream.SCANNER_SNAPSHOT: replace(
                coverage,
                last_available_at=request.exit_end_at,
            ),
        },
    )

    grade = grade_replay_coverage(request, failed)

    assert "stream_available_bounds_mismatch:scanner_snapshot" in grade.reasons


def test_nonexistent_receipt_source_hash_fails_closed() -> None:
    request, manifest = _passing_manifest()
    missing = "9" * 64
    original = manifest.read_receipts[0]
    forged = replace(
        original,
        source_event_sha256s=(missing,),
    )
    failed = replace(
        manifest,
        read_receipts=tuple(
            forged if receipt.read_id == original.read_id else receipt
            for receipt in manifest.read_receipts
        ),
    )
    grade = grade_replay_coverage(request, failed)

    assert grade.replayable is False
    assert (
        f"read_receipt_source_event_missing:{forged.read_id}:{missing}"
        in grade.reasons
    )
    assert f"read_receipt_result_mismatch:{forged.read_id}" in grade.reasons
