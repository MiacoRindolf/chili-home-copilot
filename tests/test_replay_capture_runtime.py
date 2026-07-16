from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import socket
import time
import uuid

import pytest

from app.services.trading.momentum_neural import replay_capture_runtime as capture_runtime

from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureClocks,
    CaptureContractError,
    CaptureEvent,
    CaptureRunIdentity,
    CaptureStream,
    CoverageGap,
)
from app.services.trading.momentum_neural.replay_capture_runtime import (
    BoundedCaptureIngress,
    BoundedPreTriggerRing,
    CaptureBudgetPolicy,
    CaptureResourceMeasurement,
    CaptureWriterWorker,
    CaptureWriterPool,
    ContentAddressedCaptureStore,
    ReplayNetworkAccessError,
    ReplayNetworkGuard,
    resolve_capture_budget,
)


UTC = timezone.utc
BASE = datetime(2026, 7, 13, 12, 50, tzinfo=UTC)


def _identity(
    *,
    run_id: str = "00000000-0000-0000-0000-000000000013",
) -> CaptureRunIdentity:
    return CaptureRunIdentity(
        run_id=str(uuid.UUID(run_id)),
        generation=3,
        code_build_sha256="a" * 64,
        config_sha256="b" * 64,
        feature_flags_sha256="c" * 64,
        account_identity_sha256="d" * 64,
        broker="alpaca",
        broker_environment="paper",
    )


def _event(
    sequence: int,
    *,
    stream: CaptureStream = CaptureStream.NBBO_QUOTE,
    symbol: str = "VEEE",
    payload_bytes: int = 0,
    identity: CaptureRunIdentity | None = None,
) -> CaptureEvent:
    at = BASE + timedelta(milliseconds=sequence)
    return CaptureEvent(
        identity=identity or _identity(),
        sequence=sequence,
        stream=stream,
        symbol=symbol,
        provider="fixture",
        clocks=CaptureClocks(
            provider_event_at=at - timedelta(milliseconds=3),
            received_at=at - timedelta(milliseconds=1),
            available_at=at,
        ),
        payload={"sequence": sequence, "padding": "x" * payload_bytes},
    )


def _external_payload_event(
    sequence: int,
    *,
    identity: CaptureRunIdentity | None = None,
) -> CaptureEvent:
    return CaptureEvent(
        identity=identity or _identity(),
        sequence=sequence,
        stream=CaptureStream.PROVIDER_OHLCV,
        symbol="VEEE",
        provider="massive",
        clocks=CaptureClocks(
            market_reference_at=BASE,
            received_at=BASE + timedelta(seconds=1),
            available_at=BASE + timedelta(seconds=1, milliseconds=1),
        ),
        query={"symbol": "VEEE", "interval": "1m", "from": "2026-07-13"},
        payload={"bars": [["13:00", 8.0, 9.0, 7.8, 8.8, 100_000]]},
    )


def _gap(
    identity: CaptureRunIdentity | None = None,
) -> tuple[CaptureRunIdentity, CoverageGap]:
    exact_identity = identity or _identity()
    return (
        exact_identity,
        CoverageGap(
            stream=CaptureStream.NBBO_QUOTE,
            symbol="VEEE",
            reason="fixture_drop",
            first_available_at=BASE + timedelta(milliseconds=5),
            last_available_at=BASE + timedelta(milliseconds=7),
            lost_count=3,
        ),
    )


def _closed_writer(
    store: ContentAddressedCaptureStore,
    events: tuple[CaptureEvent, ...],
    *,
    dropped_events: tuple[CaptureEvent, ...] = (),
) -> CaptureWriterWorker:
    assert events or dropped_events
    ingress = BoundedCaptureIngress(
        max_events=max(1, len(events)),
        max_bytes=5_000_000,
        max_gap_keys=32,
    )
    for event in events:
        assert ingress.submit(event)
    for event in dropped_events:
        assert not ingress.submit(event)
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
    return worker


def test_resource_budget_is_finite_and_derived_from_measured_headroom() -> None:
    measurement = CaptureResourceMeasurement(
        measured_at=BASE,
        sample_seconds=5,
        total_memory_bytes=64 * 1024**3,
        available_memory_bytes=19 * 1024**3,
        disk_free_bytes=240 * 1024**3,
        average_cpu_percent=32,
        sustained_append_bytes_per_second=29 * 1024**2,
        fsync_p95_milliseconds=583,
        logical_cpu_count=32,
        host_fingerprint_sha256="e" * 64,
    )
    policy = CaptureBudgetPolicy(
        memory_reserve_bytes=12 * 1024**3,
        disk_reserve_bytes=80 * 1024**3,
        capture_fraction_of_memory_headroom=0.25,
        ring_fraction_of_capture_memory=0.30,
        queue_fraction_of_capture_memory=0.15,
        capture_fraction_of_disk_headroom=0.50,
        capture_fraction_of_measured_write_bandwidth=0.25,
        max_average_cpu_percent=80,
        capture_fraction_of_cpu_headroom=0.90,
        calibrated_hot_symbol_bytes=64 * 1024**2,
        max_queue_events=250_000,
        max_ring_events=1_000_000,
        max_gap_keys=4_096,
        raw_retention_days=3,
        derived_retention_days=90,
        pressure_cpu_enter_percent=75,
        pressure_cpu_exit_percent=60,
        pressure_memory_enter_margin_bytes=512 * 1024**2,
        pressure_memory_exit_margin_bytes=1024 * 1024**2,
        pressure_disk_enter_margin_bytes=1024 * 1024**2,
        pressure_disk_exit_margin_bytes=2 * 1024**3,
        pressure_write_latency_enter_milliseconds=100,
        pressure_write_latency_exit_milliseconds=25,
        pressure_enter_samples=3,
        pressure_recovery_samples=3,
        pressure_sample_max_age_seconds=5,
        store_owner_lease_seconds=60,
        store_owner_heartbeat_seconds=10,
    )
    resolved = resolve_capture_budget(measurement, policy)

    assert resolved.capture_memory_bytes < 7 * 1024**3
    assert resolved.derived_hot_symbol_capacity > 1
    assert resolved.disk_quota_bytes == 80 * 1024**3
    assert resolved.sustained_write_budget_bytes_per_second == int(29 * 1024**2 * 0.25)
    assert len(resolved.budget_sha256) == 64


def test_pretrigger_promotion_flushes_preceding_events_and_reports_capacity_gap() -> None:
    ring = BoundedPreTriggerRing(
        horizon=timedelta(minutes=3),
        max_events=2,
        max_bytes=1_000_000,
        per_symbol_max_events=2,
    )
    assert ring.add(_event(1))
    assert ring.add(_event(2))
    assert ring.add(_event(3))

    promoted = ring.promote("veee", promoted_at=BASE + timedelta(seconds=1))
    assert [event.sequence for event in promoted.events] == [2, 3]
    assert len(promoted.gaps) == 1
    assert promoted.gaps[0].reason == "pretrigger_per_symbol_capacity"
    assert promoted.gaps[0].lost_count == 1
    assert ring.event_count == 0


def test_pretrigger_expiry_is_correct_for_out_of_order_arrivals() -> None:
    ring = BoundedPreTriggerRing(
        horizon=timedelta(milliseconds=50),
        max_events=10,
        max_bytes=1_000_000,
        per_symbol_max_events=10,
    )
    assert ring.add(_event(100))
    assert ring.add(_event(1))
    assert ring.add(_event(200))

    promoted = ring.promote(
        "VEEE", promoted_at=BASE + timedelta(milliseconds=200)
    )
    assert [event.sequence for event in promoted.events] == [200]
    assert promoted.gaps == ()


def test_pretrigger_gap_ledger_overflow_is_bounded_and_globally_visible() -> None:
    ring = BoundedPreTriggerRing(
        horizon=timedelta(minutes=3),
        max_events=20,
        max_bytes=1_000_000,
        per_symbol_max_events=1,
        max_gap_keys=1,
    )
    assert ring.add(_event(1, symbol="AAAA"))
    assert ring.add(_event(2, symbol="AAAA"))
    assert ring.add(_event(3, symbol="BBBB"))
    assert ring.add(_event(4, symbol="BBBB"))

    promoted = ring.promote("BBBB", promoted_at=BASE + timedelta(seconds=1))
    assert any(
        gap.stream is CaptureStream.COVERAGE_GAP
        and gap.reason == "pretrigger_gap_ledger_key_budget_overflow"
        and gap.symbol is None
        and gap.lost_count == 1
        for gap in promoted.gaps
    )


def test_nonblocking_ingress_aggregates_every_overflow_as_explicit_gap() -> None:
    ingress = BoundedCaptureIngress(max_events=1, max_bytes=50_000, max_gap_keys=8)
    first = _event(1)
    second = _event(2)
    assert ingress.submit(first) is True
    assert ingress.submit(second) is False

    batch = ingress.pop_batch(max_events=10, max_bytes=100_000, timeout_seconds=0)
    assert batch.events == (first,)
    assert len(batch.gaps) == 1
    identity, gap = batch.gaps[0]
    assert identity == _identity()
    assert gap.reason == "capture_queue_overflow"
    assert gap.lost_count == 1
    assert ingress.health()["dropped"] == 1


def test_gap_ledger_key_budget_stays_bounded_and_fails_all_streams() -> None:
    ingress = BoundedCaptureIngress(max_events=1, max_bytes=50_000, max_gap_keys=1)
    assert ingress.submit(_event(1))
    assert not ingress.submit(_event(2, stream=CaptureStream.NBBO_QUOTE))
    assert not ingress.submit(_event(3, stream=CaptureStream.IQFEED_PRINT))

    batch = ingress.pop_batch(max_events=10, max_bytes=100_000, timeout_seconds=0)
    assert len(batch.gaps) == 1
    assert batch.gaps[0][1].stream is CaptureStream.COVERAGE_GAP
    assert batch.gaps[0][1].lost_count == 2
    assert ingress.health()["pending_gap_keys"] == 0


def test_content_addressed_store_round_trips_and_deduplicates(tmp_path) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    ohlcv_query = {"symbol": "VEEE", "interval": "1m", "from": "2026-07-13"}
    ohlcv_payload = {"bars": [["13:00", 8.0, 9.0, 7.8, 8.8, 100_000]]}
    ohlcv = CaptureEvent(
        identity=_identity(),
        sequence=2,
        stream=CaptureStream.PROVIDER_OHLCV,
        symbol="VEEE",
        provider="massive",
        clocks=CaptureClocks(
            market_reference_at=BASE,
            received_at=BASE + timedelta(seconds=1),
            available_at=BASE + timedelta(seconds=1, milliseconds=1),
        ),
        query=ohlcv_query,
        payload=ohlcv_payload,
    )
    events = (_event(1), ohlcv)
    first_refs = store.write_events(events)
    second_refs = store.write_events(events)

    assert first_refs == second_refs
    assert store.load_events() == events
    blob_files = list((tmp_path / "capture" / "blobs").rglob("*.zlib"))
    # High-rate NBBO stays inline in a compressed chunk; queried OHLCV is the
    # only externalized, content-deduplicated payload.
    assert len(blob_files) == 1


def test_payload_pack_deduplicates_logical_payload_across_distinct_events(
    tmp_path,
) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    first = _external_payload_event(1)
    second = replace(
        first,
        sequence=2,
        clocks=replace(
            first.clocks,
            received_at=first.clocks.received_at + timedelta(milliseconds=1),
            available_at=first.clocks.available_at + timedelta(milliseconds=1),
        ),
        query={**dict(first.query or {}), "request_id": "second"},
    )
    worker = _closed_writer(store, (first, second))

    packs = tuple((store.root / "blobs" / "packs").rglob("*.json.zlib"))
    assert len(packs) == 1
    seal = worker.seal_run(_identity())
    packed = next(row for row in seal.objects if row.kind == "payload_pack")
    assert packed.record_count == 1
    assert packed.reference_count == 2
    assert store.load_sealed_run(
        _identity(), expected_seal_sha256=seal.seal_sha256
    ).events == (first, second)


def test_payload_pack_lookup_fails_when_valid_pack_lacks_requested_digest(
    tmp_path,
) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture",
        compression_codec="zlib",
        payload_pack_max_records=1,
    )
    first = _external_payload_event(1)
    second = replace(
        _external_payload_event(2),
        payload={"bars": [["13:01", 9.0, 10.0, 8.8, 9.8, 200_000]]},
    )
    store.write_events((first, second))
    packs = tuple((store.root / "blobs" / "packs").rglob("*.json.zlib"))
    assert len(packs) == 2
    wrong_pack = next(
        path
        for path in packs
        if first.payload_sha256 not in store._load_payload_pack(path)[0]
    )

    with pytest.raises(CaptureContractError, match="unavailable in packed object"):
        store.get_payload(
            first.payload_sha256,
            relative_path=wrong_pack.relative_to(store.root).as_posix(),
        )


def test_sealed_load_rejects_corrupt_payload_pack(tmp_path) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    identity = _identity()
    worker = _closed_writer(store, (_external_payload_event(1),))
    seal = worker.seal_run(identity)
    packed = next(row for row in seal.objects if row.kind == "payload_pack")
    path = store.root / packed.relative_path
    content = path.read_bytes()
    path.write_bytes(content[:-1] + bytes([content[-1] ^ 0xFF]))

    with pytest.raises(CaptureContractError, match="corrupt zlib|hash mismatch"):
        store.load_sealed_run(
            identity, expected_seal_sha256=seal.seal_sha256
        )


def test_payload_packing_bounds_physical_files_and_exact_seal_inventory(
    tmp_path,
) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture",
        compression_codec="zlib",
        payload_pack_max_records=16,
        payload_pack_target_raw_bytes=1_000_000,
    )
    events = tuple(
        replace(
            _external_payload_event(sequence),
            payload={"bars": [["13:00", sequence, sequence + 1]]},
            query={"symbol": "VEEE", "request_id": sequence},
            clocks=CaptureClocks(
                market_reference_at=BASE,
                received_at=BASE + timedelta(milliseconds=sequence),
                available_at=BASE + timedelta(milliseconds=sequence + 1),
            ),
        )
        for sequence in range(1, 101)
    )
    worker = _closed_writer(store, events)
    seal = worker.seal_run(_identity())
    packed = tuple(row for row in seal.objects if row.kind == "payload_pack")

    assert len(packed) == 7
    assert sum(row.record_count for row in packed) == 100
    assert sum(row.reference_count for row in packed) == 100
    assert len(tuple((store.root / "blobs" / "packs").rglob("*.json.zlib"))) == 7
    assert store.load_sealed_run(
        _identity(), expected_seal_sha256=seal.seal_sha256
    ).events == events


def test_clean_close_syncs_each_physical_object_once_and_seal_last(
    tmp_path, monkeypatch
) -> None:
    fsync_calls: list[int] = []
    monkeypatch.setattr(
        capture_runtime.os, "fsync", lambda descriptor: fsync_calls.append(descriptor)
    )
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    calls_after_ownership_acquire = len(fsync_calls)
    worker = _closed_writer(store, (_external_payload_event(1),))
    calls_after_close = len(fsync_calls)
    synced_after_close = store.resource_health()["sync"]["objects"]

    seal = worker.seal_run(_identity())

    # Storage-policy audit, event chunk, and payload pack were flushed by the
    # clean writer close. Sealing does not flush them twice; it flushes only
    # the newly published final seal.
    assert calls_after_close - calls_after_ownership_acquire == 3
    assert synced_after_close == 3
    assert len(fsync_calls) == calls_after_close + 1
    assert store.resource_health()["sync"]["objects"] == synced_after_close + 1
    assert store.load_sealed_run(
        _identity(), expected_seal_sha256=seal.seal_sha256
    ).seal == seal


def test_fsync_failure_revokes_clean_close_and_records_resource_failure(
    tmp_path, monkeypatch
) -> None:
    def fail_fsync(_descriptor: int) -> None:
        raise OSError("fixture flush failure")

    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    # Ownership acquisition is independently durable; fail the capture-object
    # flush path after the exclusive store lease has been established.
    monkeypatch.setattr(capture_runtime.os, "fsync", fail_fsync)
    identity = _identity()
    ingress = BoundedCaptureIngress(
        max_events=10, max_bytes=500_000, max_gap_keys=8
    )
    assert ingress.submit(_external_payload_event(1))
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=10,
        batch_bytes=500_000,
        poll_seconds=0.001,
    )
    worker.start()

    assert worker.stop(timeout_seconds=5) is False
    assert store.resource_health()["resource_failure_reasons"] == (
        "capture_fsync_failed",
    )
    with pytest.raises(CaptureContractError, match="clean, error-free shutdown"):
        worker.seal_run(identity)


def test_exact_run_seal_round_trips_chunks_blob_counts_and_bounds(tmp_path) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    identity = _identity()
    events = (_event(1, identity=identity), _external_payload_event(2, identity=identity))
    dropped = tuple(_event(sequence, identity=identity) for sequence in range(3, 6))
    worker = _closed_writer(store, events, dropped_events=dropped)

    seal = worker.seal_run(identity)
    loaded = store.load_sealed_run(
        identity, expected_seal_sha256=seal.seal_sha256
    )

    assert loaded.seal == seal
    assert loaded.events == events
    assert len(loaded.gaps) == 1
    assert loaded.gaps[0][0] == identity
    assert loaded.gaps[0][1].reason == "capture_queue_overflow"
    assert loaded.gaps[0][1].lost_count == 3
    assert seal.event_count == 2
    assert seal.gap_count == 1
    assert seal.gap_lost_count == 3
    assert (seal.sequence_min, seal.sequence_max) == (1, 2)
    assert seal.close_proof.ingress_submitted == 5
    assert seal.close_proof.events_written == 2
    assert seal.close_proof.lost_events_recorded == 3
    assert seal.close_proof.submission_sequence_max == 5
    assert seal.close_proof_sha256 == seal.close_proof.proof_sha256
    assert len(seal.content_root_sha256) == 64
    assert [row.kind for row in seal.objects] == [
        "event_chunk",
        "gap_chunk",
        "payload_pack",
    ]
    payload_ref = next(row for row in seal.objects if row.kind == "payload_pack")
    assert payload_ref.record_count == 1
    assert payload_ref.reference_count == 1
    assert worker.seal_run(identity) == seal
    with pytest.raises(CaptureContractError, match="already sealed"):
        store.write_events((_event(3, identity=identity),))


@pytest.mark.parametrize("missing_kind", ["event_chunk", "gap_chunk"])
def test_exact_run_sealed_load_fails_when_whole_chunk_is_deleted(
    tmp_path, missing_kind: str
) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    identity = _identity()
    worker = _closed_writer(
        store,
        (_event(1, identity=identity),),
        dropped_events=(_event(2, identity=identity),),
    )
    seal = worker.seal_run(identity)
    missing = next(row for row in seal.objects if row.kind == missing_kind)
    (store.root / missing.relative_path).unlink()

    with pytest.raises(
        CaptureContractError,
        match="close-proof|object set|content root",
    ):
        store.load_sealed_run(
            identity, expected_seal_sha256=seal.seal_sha256
        )


def test_exact_run_sealed_load_fails_when_referenced_blob_is_deleted(tmp_path) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    identity = _identity()
    worker = _closed_writer(
        store, (_external_payload_event(1, identity=identity),)
    )
    seal = worker.seal_run(identity)
    missing = next(row for row in seal.objects if row.kind == "payload_pack")
    (store.root / missing.relative_path).unlink()

    with pytest.raises(CaptureContractError, match="payload blob unavailable"):
        store.load_sealed_run(
            identity, expected_seal_sha256=seal.seal_sha256
        )


def test_exact_run_sealed_load_rejects_wrong_full_identity(tmp_path) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    identity = _identity()
    worker = _closed_writer(store, (_event(1, identity=identity),))
    seal = worker.seal_run(identity)
    wrong_identity = replace(identity, config_sha256="f" * 64)

    with pytest.raises(CaptureContractError, match="seal identity mismatch"):
        store.load_sealed_run(
            wrong_identity, expected_seal_sha256=seal.seal_sha256
        )


def test_exact_run_sealed_load_rejects_conflicting_seals(tmp_path) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    identity = _identity()
    worker = _closed_writer(store, (_event(1, identity=identity),))
    seal = worker.seal_run(identity)
    conflict_path = (
        store.root
        / "seals"
        / f"run={identity.run_id}"
        / f"generation={identity.generation}"
        / f"{'0' * 64}.json"
    )
    conflict_path.write_bytes(b"{}")

    with pytest.raises(CaptureContractError, match="multiple conflicting seals"):
        store.load_sealed_run(
            identity, expected_seal_sha256=seal.seal_sha256
        )


def test_exact_run_sealed_loader_selects_only_requested_run(tmp_path) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    first = _identity()
    second = _identity(run_id="00000000-0000-0000-0000-000000000014")
    first_event = _event(1, identity=first)
    second_event = _event(1, identity=second)
    first_writer = _closed_writer(store, (first_event,))
    second_writer = _closed_writer(store, (second_event,))
    first_seal = first_writer.seal_run(first)

    with pytest.raises(CaptureContractError, match="exact capture run is unsealed"):
        store.load_sealed_run(second, expected_seal_sha256="0" * 64)
    with pytest.raises(CaptureContractError, match="one exact CaptureRunIdentity"):
        store.load_sealed_run(  # type: ignore[arg-type]
            None, expected_seal_sha256="0" * 64
        )

    second_seal = second_writer.seal_run(second)
    assert store.load_sealed_run(
        first, expected_seal_sha256=first_seal.seal_sha256
    ).events == (first_event,)
    assert store.load_sealed_run(
        second, expected_seal_sha256=second_seal.seal_sha256
    ).events == (second_event,)
    assert len(store.load_events()) == 2


def test_certifying_load_requires_exact_expected_seal_sha_and_diagnostic_is_named(
    tmp_path,
) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    identity = _identity()
    worker = _closed_writer(store, (_event(1, identity=identity),))
    seal = worker.seal_run(identity)

    with pytest.raises(TypeError, match="expected_seal_sha256"):
        store.load_sealed_run(identity)  # type: ignore[call-arg]
    with pytest.raises(CaptureContractError, match="does not match expected SHA"):
        store.load_sealed_run(identity, expected_seal_sha256="0" * 64)

    diagnostic = store.load_sealed_run_diagnostic(identity)
    assert diagnostic.seal == seal
    assert diagnostic.events == (_event(1, identity=identity),)


def test_direct_object_inventory_cannot_fabricate_clean_close_seal(tmp_path) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    identity = _identity()
    store.write_events((_event(1, identity=identity),))

    with pytest.raises(CaptureContractError, match="writer lifecycle"):
        store.seal_run(identity)


def test_seal_rejects_queued_unstarted_and_running_unclosed_writer(tmp_path) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    identity = _identity()
    ingress = BoundedCaptureIngress(
        max_events=10, max_bytes=500_000, max_gap_keys=8
    )
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=20,
        batch_bytes=500_000,
        poll_seconds=0.001,
    )
    assert ingress.submit(_event(1, identity=identity))

    with pytest.raises(CaptureContractError, match="clean, error-free shutdown"):
        store.seal_run(identity, lifecycle=worker)
    worker.start()
    with pytest.raises(CaptureContractError, match="clean, error-free shutdown"):
        store.seal_run(identity, lifecycle=worker)
    assert worker.stop(timeout_seconds=5)


def test_seal_rejects_direct_import_outside_runtime_counters(tmp_path) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    identity = _identity()
    ingress = BoundedCaptureIngress(
        max_events=2, max_bytes=500_000, max_gap_keys=8
    )
    assert ingress.submit(_event(1, identity=identity))
    removed = ingress.pop_batch(
        max_events=10, max_bytes=500_000, timeout_seconds=0
    )
    # The bytes exist, but the writer lifecycle did not write them.  A direct
    # import therefore cannot be mistaken for runtime-complete capture.
    store.write_events(removed.events)
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=20,
        batch_bytes=500_000,
        poll_seconds=0.001,
    )
    worker.start()
    assert worker.stop(timeout_seconds=5)

    with pytest.raises(CaptureContractError, match="accepted capture events"):
        worker.seal_run(identity)


def test_noncontiguous_global_sequences_are_exactly_bound_by_accumulator(tmp_path) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    identity = _identity()
    ingress = BoundedCaptureIngress(
        max_events=10, max_bytes=500_000, max_gap_keys=8
    )
    assert ingress.submit(_event(1, identity=identity))
    assert ingress.submit(_event(3, identity=identity))
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=20,
        batch_bytes=500_000,
        poll_seconds=0.001,
    )
    worker.start()
    assert worker.stop(timeout_seconds=5)
    seal = worker.seal_run(identity)

    assert seal.event_count == 2
    assert (seal.sequence_min, seal.sequence_max) == (1, 3)
    assert (
        seal.event_accumulator_sha256
        == seal.close_proof.accepted_event_accumulator_sha256
    )
    with pytest.raises(CaptureContractError, match="not contiguous from sequence 1"):
        store.load_sealed_run(
            identity, expected_seal_sha256=seal.seal_sha256
        )
    assert store.load_sealed_run_diagnostic(identity).seal == seal


def test_upstream_pretrigger_gap_is_accounted_separately_from_ingress_drop(
    tmp_path,
) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    identity = _identity()
    ingress = BoundedCaptureIngress(
        max_events=10, max_bytes=500_000, max_gap_keys=8
    )
    assert ingress.submit(_event(40, identity=identity))
    assert ingress.submit_gap(
        identity,
        CoverageGap(
            stream=CaptureStream.NBBO_QUOTE,
            symbol="VEEE",
            reason="pretrigger_per_symbol_capacity",
            first_available_at=BASE + timedelta(milliseconds=10),
            last_available_at=BASE + timedelta(milliseconds=20),
            lost_count=3,
        ),
    )
    assert ingress.submit(_event(45, identity=identity))
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=20,
        batch_bytes=500_000,
        poll_seconds=0.001,
    )
    worker.start()
    assert worker.stop(timeout_seconds=5)
    seal = worker.seal_run(identity)

    assert seal.close_proof.ingress_dropped == 0
    assert seal.close_proof.reported_gap_lost == 3
    assert seal.gap_lost_count == 3
    assert (seal.sequence_min, seal.sequence_max) == (40, 45)


def test_seal_rejects_whole_chunk_missing_before_seal(tmp_path) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    identity = _identity()
    worker = _closed_writer(store, (_event(1, identity=identity),))
    next((store.root / "events").rglob("*.jsonl.zlib")).unlink()

    with pytest.raises(CaptureContractError, match="written event count"):
        worker.seal_run(identity)


def test_store_rejects_same_count_event_replacement_after_external_removal(
    tmp_path,
) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    identity = _identity()
    worker = _closed_writer(store, (_event(1, identity=identity),))
    next((store.root / "events").rglob("*.jsonl.zlib")).unlink()
    with pytest.raises(
        CaptureContractError, match="capture_owned_object_removed_outside_store"
    ):
        store.write_events((_event(1, identity=identity, symbol="FAKE"),))
    assert store.resource_health()["fail_closed"] is True


def test_store_rejects_same_count_gap_replacement_after_external_removal(
    tmp_path,
) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    identity = _identity()
    worker = _closed_writer(
        store,
        (_event(1, identity=identity),),
        dropped_events=(_event(2, identity=identity),),
    )
    next((store.root / "gaps").rglob("*.jsonl.zlib")).unlink()
    forged_gap = (
        identity,
        CoverageGap(
            stream=CaptureStream.NBBO_QUOTE,
            symbol="FAKE",
            reason="forged_gap",
            first_available_at=BASE + timedelta(milliseconds=2),
            last_available_at=BASE + timedelta(milliseconds=2),
            lost_count=1,
        ),
    )
    with pytest.raises(
        CaptureContractError, match="capture_owned_object_removed_outside_store"
    ):
        store.write_gaps((forged_gap,))
    assert store.resource_health()["fail_closed"] is True


def test_valid_seal_finalizes_ingress_and_rejects_late_submission(tmp_path) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    identity = _identity()
    ingress = BoundedCaptureIngress(
        max_events=10, max_bytes=500_000, max_gap_keys=8
    )
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=20,
        batch_bytes=500_000,
        poll_seconds=0.001,
    )
    assert ingress.submit(_event(1, identity=identity))
    worker.start()
    assert worker.stop(timeout_seconds=5)
    seal = worker.seal_run(identity)

    assert seal.close_proof.ingress_finalized is True
    with pytest.raises(CaptureContractError, match="durably finalized"):
        ingress.submit(_event(2, identity=identity))
    assert store.load_sealed_run(
        identity, expected_seal_sha256=seal.seal_sha256
    ).seal == seal


def test_async_writer_flushes_events_and_overflow_gaps(tmp_path) -> None:
    ingress = BoundedCaptureIngress(max_events=1, max_bytes=50_000, max_gap_keys=8)
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=20,
        batch_bytes=100_000,
        poll_seconds=0.01,
    )
    assert ingress.submit(_event(1))
    assert not ingress.submit(_event(2))
    worker.start()
    assert worker.stop(timeout_seconds=5) is True

    assert [event.sequence for event in store.load_events()] == [1]
    gaps = store.load_gaps()
    assert len(gaps) == 1
    assert gaps[0][1].lost_count == 1
    assert worker.health()["last_error"] is None
    assert worker.health()["stopped_cleanly"] is True


def test_submission_after_clean_stop_revokes_clean_close_status(tmp_path) -> None:
    ingress = BoundedCaptureIngress(max_events=2, max_bytes=50_000, max_gap_keys=8)
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=20,
        batch_bytes=100_000,
        poll_seconds=0.01,
    )
    assert ingress.submit(_event(1))
    worker.start()
    assert worker.stop(timeout_seconds=5) is True

    assert ingress.submit(_event(2)) is False
    assert worker.health()["stopped_cleanly"] is False
    assert ingress.health()["post_close_submissions"] == 1
    assert ingress.health()["clean_close_eligible"] is False
    with pytest.raises(CaptureContractError, match="post-close submissions"):
        worker.seal_run(_identity())
    with pytest.raises(CaptureContractError, match="one-shot"):
        worker.start()


@pytest.mark.parametrize(
    ("sequence_min", "sequence_max"),
    ((None, 1), (1, None), (2, 1)),
)
def test_clean_close_rejects_corrupt_sequence_bounds_before_finalizing(
    sequence_min: int | None,
    sequence_max: int | None,
) -> None:
    ingress = BoundedCaptureIngress(
        max_events=2,
        max_bytes=50_000,
        max_gap_keys=8,
    )
    identity = _identity()
    assert ingress.submit(_event(1, identity=identity))
    batch = ingress.pop_batch(
        max_events=2,
        max_bytes=50_000,
        timeout_seconds=0,
    )
    assert [event.sequence for event in batch.events] == [1]
    assert batch.gaps == ()
    ingress.close()
    ingress._sequence_min = sequence_min
    ingress._sequence_max = sequence_max

    with pytest.raises(
        CaptureContractError,
        match="sequence bounds do not reconcile",
    ):
        ingress.finalize_clean_close(identity)

    assert ingress.health()["finalized"] is False


def test_parallel_writer_pool_preserves_every_sequence(tmp_path) -> None:
    ingress = BoundedCaptureIngress(
        max_events=200, max_bytes=5_000_000, max_gap_keys=32
    )
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib", compression_level=1
    )
    pool = CaptureWriterPool(
        ingress=ingress,
        store=store,
        workers=3,
        batch_events=10,
        batch_bytes=500_000,
        poll_seconds=0.001,
        flush_interval_seconds=0.01,
    )
    pool.start()
    for sequence in range(1, 101):
        assert ingress.submit(_event(sequence))
    assert pool.stop(timeout_seconds=10) is True

    loaded = store.load_events()
    assert sorted(event.sequence for event in loaded) == list(range(1, 101))
    assert pool.health()["events_written"] == 100
    assert pool.health()["last_errors"] == []
    seal = pool.seal_run(_identity())
    assert seal.event_count == 100
    assert seal.close_proof.writer_count == 3
    assert store.load_sealed_run(
        _identity(), expected_seal_sha256=seal.seal_sha256
    ).seal == seal


def test_network_guard_proves_attempt_and_restores_socket() -> None:
    original = socket.create_connection
    original_connect_ex = socket.socket.connect_ex
    guard = ReplayNetworkGuard()
    with guard:
        with pytest.raises(ReplayNetworkAccessError, match="forbidden"):
            socket.create_connection(("127.0.0.1", 9), timeout=0.01)
        sock = socket.socket()
        try:
            with pytest.raises(ReplayNetworkAccessError, match="forbidden"):
                sock.connect_ex(("127.0.0.1", 9))
        finally:
            sock.close()
    assert guard.attempt_count == 2
    assert socket.create_connection is original
    assert socket.socket.connect_ex is original_connect_ex
