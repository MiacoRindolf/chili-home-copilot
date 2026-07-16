from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import uuid

import pytest

from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureClocks,
    CaptureContractError,
    CaptureEvent,
    ReplayCoverageRequest,
    CaptureRunIdentity,
    CaptureStream,
    VerifiedReplayCapture,
)
from app.services.trading.momentum_neural.replay_capture_runtime import (
    BoundedCaptureIngress,
    BoundedHotSymbolLeases,
    BoundedPreTriggerRing,
    CaptureAdaptivePressureController,
    CaptureBudgetPolicy,
    CaptureColdArchiveReceipt,
    CapturePressureSample,
    CaptureResourceBinding,
    CaptureResourceMeasurement,
    CaptureRetentionPin,
    CaptureWriterWorker,
    ContentAddressedCaptureStore,
    ReadOnlyV4CaptureStore,
    ReplayNetworkGuard,
    load_verified_replay_capture_v4,
)


UTC = timezone.utc
BASE = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def _identity(index: int = 1) -> CaptureRunIdentity:
    return CaptureRunIdentity(
        run_id=str(uuid.UUID(int=index)),
        generation=1,
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
    identity: CaptureRunIdentity | None = None,
    at: datetime = BASE,
) -> CaptureEvent:
    available_at = at + timedelta(milliseconds=sequence)
    return CaptureEvent(
        identity=identity or _identity(),
        sequence=sequence,
        stream=CaptureStream.NBBO_QUOTE,
        symbol="PLSM",
        provider="fixture",
        clocks=CaptureClocks(
            provider_event_at=available_at - timedelta(milliseconds=2),
            received_at=available_at - timedelta(milliseconds=1),
            available_at=available_at,
        ),
        payload={"bid": 5.10, "ask": 5.12, "sequence": sequence},
    )


def _coverage_request(identity: CaptureRunIdentity) -> ReplayCoverageRequest:
    return ReplayCoverageRequest(
        warmup_start_at=BASE,
        decision_at=BASE + timedelta(milliseconds=1),
        exit_end_at=BASE + timedelta(milliseconds=2),
        required_streams=frozenset({CaptureStream.NBBO_QUOTE}),
        decision_id="readonly-v4-fixture",
        decision_checkpoint_sha256="f" * 64,
        symbol="PLSM",
        expected_identity_sha256=identity.identity_sha256,
    )


def _tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    rows: list[tuple[object, ...]] = []
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        rows.append(
            (
                path.relative_to(root).as_posix(),
                path.is_dir(),
                stat.st_size,
                stat.st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file()
                else None,
            )
        )
    return tuple(rows)


def _binding(
    *,
    available_memory_bytes: int = 50_000_000,
    memory_reserve_bytes: int = 10_000_000,
    disk_free_bytes: int = 1_000_000_000,
    disk_reserve_bytes: int = 100_000_000,
    disk_fraction: float = 0.50,
    write_bytes_per_second: float = 4_000_000,
    calibrated_hot_symbol_bytes: int = 1_000,
    average_cpu_percent: float = 20,
) -> CaptureResourceBinding:
    measurement = CaptureResourceMeasurement(
        measured_at=BASE,
        sample_seconds=5,
        total_memory_bytes=max(available_memory_bytes, 100_000_000),
        available_memory_bytes=available_memory_bytes,
        disk_free_bytes=disk_free_bytes,
        average_cpu_percent=average_cpu_percent,
        sustained_append_bytes_per_second=write_bytes_per_second,
        fsync_p95_milliseconds=5,
        logical_cpu_count=8,
        host_fingerprint_sha256="e" * 64,
    )
    policy = CaptureBudgetPolicy(
        memory_reserve_bytes=memory_reserve_bytes,
        disk_reserve_bytes=disk_reserve_bytes,
        capture_fraction_of_memory_headroom=0.50,
        ring_fraction_of_capture_memory=0.25,
        queue_fraction_of_capture_memory=0.25,
        capture_fraction_of_disk_headroom=disk_fraction,
        capture_fraction_of_measured_write_bandwidth=0.25,
        max_average_cpu_percent=80,
        capture_fraction_of_cpu_headroom=0.90,
        calibrated_hot_symbol_bytes=calibrated_hot_symbol_bytes,
        max_queue_events=100,
        max_ring_events=200,
        max_gap_keys=16,
        raw_retention_days=3,
        derived_retention_days=90,
        pressure_cpu_enter_percent=75,
        pressure_cpu_exit_percent=60,
        pressure_memory_enter_margin_bytes=1_000,
        pressure_memory_exit_margin_bytes=2_000,
        pressure_disk_enter_margin_bytes=1_000,
        pressure_disk_exit_margin_bytes=2_000,
        pressure_write_latency_enter_milliseconds=100,
        pressure_write_latency_exit_milliseconds=25,
        pressure_enter_samples=3,
        pressure_recovery_samples=3,
        pressure_sample_max_age_seconds=5,
        store_owner_lease_seconds=60,
        store_owner_heartbeat_seconds=10,
    )
    return CaptureResourceBinding.resolve(measurement, policy)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_resource_health_cannot_double_count_an_inflight_immutable_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture",
        compression_codec="zlib",
        resource_binding=_binding(),
        wall_clock=lambda: BASE,
    )
    original_link = os.link
    immutable_link_created = threading.Event()
    allow_publisher_to_finish = threading.Event()
    publisher_done = threading.Event()
    health_done = threading.Event()
    failures: list[BaseException] = []

    def paused_link(source: object, target: object, *args: object, **kwargs: object) -> None:
        original_link(source, target, *args, **kwargs)
        if "blobs" in Path(target).parts:
            immutable_link_created.set()
            if not allow_publisher_to_finish.wait(timeout=5):
                raise AssertionError("fixture did not release immutable publisher")

    monkeypatch.setattr(os, "link", paused_link)

    def publish() -> None:
        try:
            store.put_payload({"fixture": "concurrent-health"})
        except BaseException as exc:  # surfaced on the owning test thread below
            failures.append(exc)
        finally:
            publisher_done.set()

    def inspect_health() -> None:
        try:
            store.resource_health()
        except BaseException as exc:  # surfaced on the owning test thread below
            failures.append(exc)
        finally:
            health_done.set()

    publisher = threading.Thread(target=publish, daemon=True)
    health = threading.Thread(target=inspect_health, daemon=True)
    publisher.start()
    assert immutable_link_created.wait(timeout=5)
    health.start()
    health_finished_during_publish = health_done.wait(timeout=0.1)
    allow_publisher_to_finish.set()
    assert publisher_done.wait(timeout=5)
    assert health_done.wait(timeout=5)
    publisher.join(timeout=1)
    health.join(timeout=1)

    assert health_finished_during_publish is False
    assert failures == []
    final_health = store.resource_health()
    assert final_health["resource_failure_reasons"] == ()
    assert final_health["actual_root_bytes"] == final_health["root_bytes"]
    store.close()


def _pressure_sample(
    binding: CaptureResourceBinding,
    index: int,
    *,
    cpu_percent: float = 20,
    available_memory_bytes: int = 20_000_000,
    disk_free_bytes: int = 200_000_000,
    write_latency_milliseconds: float = 5,
) -> CapturePressureSample:
    return CapturePressureSample(
        observed_at=BASE + timedelta(seconds=index),
        resource_binding_sha256=binding.binding_sha256,
        cpu_percent=cpu_percent,
        available_memory_bytes=available_memory_bytes,
        disk_free_bytes=disk_free_bytes,
        write_latency_milliseconds=write_latency_milliseconds,
    )


def test_binding_factories_enforce_exact_finite_ring_queue_and_hashes() -> None:
    binding = _binding()
    clock = _Clock()
    ring = BoundedPreTriggerRing.from_resource_binding(
        binding, horizon=timedelta(minutes=3), per_symbol_max_events=50
    )
    ingress = BoundedCaptureIngress.from_resource_binding(
        binding, monotonic_clock=clock
    )

    assert ring.max_events == binding.budget.max_ring_events
    assert ring.max_bytes == binding.budget.pretrigger_ring_bytes
    assert ingress.max_events == binding.budget.max_queue_events
    assert ingress.max_bytes == binding.budget.async_queue_bytes
    assert (
        ingress.sustained_write_budget_bytes_per_second
        == binding.budget.sustained_write_budget_bytes_per_second
    )
    assert ring.health()["resource_hashes"] == binding.hashes
    assert ingress.health()["resource_hashes"] == binding.hashes

    with pytest.raises(CaptureContractError, match="exceeds resolved"):
        BoundedCaptureIngress(
            max_events=binding.budget.max_queue_events + 1,
            max_bytes=binding.budget.async_queue_bytes,
            max_gap_keys=binding.budget.max_gap_keys,
            sustained_write_budget_bytes_per_second=(
                binding.budget.sustained_write_budget_bytes_per_second
            ),
            resource_binding=binding,
        )


def test_measured_cpu_headroom_derates_budget_and_saturation_refuses_start() -> None:
    roomy = _binding(average_cpu_percent=20)
    constrained = _binding(average_cpu_percent=74)

    assert constrained.budget.cpu_headroom_to_ceiling_percent == pytest.approx(6)
    assert constrained.budget.cpu_limited_resource_fraction < (
        roomy.budget.cpu_limited_resource_fraction
    )
    assert constrained.budget.capture_memory_bytes < roomy.budget.capture_memory_bytes
    assert (
        constrained.budget.sustained_write_budget_bytes_per_second
        < roomy.budget.sustained_write_budget_bytes_per_second
    )
    with pytest.raises(CaptureContractError, match="CPU leaves no policy headroom"):
        _binding(average_cpu_percent=80)


def test_adaptive_pressure_is_hysteretic_emits_gap_and_recovers() -> None:
    binding = _binding()
    controller = CaptureAdaptivePressureController(binding)
    clock = _Clock()
    ingress = BoundedCaptureIngress.from_resource_binding(
        binding,
        pressure_controller=controller,
        monotonic_clock=clock,
    )

    for index in range(1, binding.policy.pressure_enter_samples + 1):
        health = controller.observe(
            _pressure_sample(
                binding,
                index,
                cpu_percent=90,
                available_memory_bytes=(
                    binding.policy.memory_reserve_bytes
                    + binding.policy.pressure_memory_enter_margin_bytes
                ),
                disk_free_bytes=(
                    binding.policy.disk_reserve_bytes
                    + binding.policy.pressure_disk_enter_margin_bytes
                ),
                write_latency_milliseconds=(
                    binding.policy.pressure_write_latency_enter_milliseconds
                ),
            )
        )
    assert health["pressure_state"] == "failed_closed"
    assert health["active_reasons"] == (
        "cpu",
        "memory",
        "disk",
        "write_latency",
    )
    assert ingress.submit(_event(1)) is False
    batch = ingress.pop_batch(
        max_events=10, max_bytes=1_000_000, timeout_seconds=0
    )
    assert batch.events == ()
    assert len(batch.gaps) == 1
    assert batch.gaps[0][1].reason.startswith("capture_resource_pressure_")
    assert ingress.health()["resource_pressure_dropped"] == 1

    first_recovery_index = binding.policy.pressure_enter_samples + 1
    for offset in range(binding.policy.pressure_recovery_samples - 1):
        health = controller.observe(
            _pressure_sample(
                binding,
                first_recovery_index + offset,
                cpu_percent=binding.policy.pressure_cpu_exit_percent,
                available_memory_bytes=(
                    binding.policy.memory_reserve_bytes
                    + binding.policy.pressure_memory_exit_margin_bytes
                ),
                disk_free_bytes=(
                    binding.policy.disk_reserve_bytes
                    + binding.policy.pressure_disk_exit_margin_bytes
                ),
                write_latency_milliseconds=(
                    binding.policy.pressure_write_latency_exit_milliseconds
                ),
            )
        )
        assert health["pressure_state"] == "failed_closed"
    health = controller.observe(
        _pressure_sample(
            binding,
            first_recovery_index + binding.policy.pressure_recovery_samples - 1,
            cpu_percent=binding.policy.pressure_cpu_exit_percent,
            available_memory_bytes=(
                binding.policy.memory_reserve_bytes
                + binding.policy.pressure_memory_exit_margin_bytes
            ),
            disk_free_bytes=(
                binding.policy.disk_reserve_bytes
                + binding.policy.pressure_disk_exit_margin_bytes
            ),
            write_latency_milliseconds=(
                binding.policy.pressure_write_latency_exit_milliseconds
            ),
        )
    )
    assert health["pressure_state"] == "normal"
    assert health["transition_count"] == 2
    assert health["last_sample_sha256"] is not None
    assert ingress.submit(_event(2)) is True


def test_pressure_sample_absence_and_staleness_are_fail_closed() -> None:
    binding = _binding()
    clock = _Clock()
    controller = CaptureAdaptivePressureController(
        binding, monotonic_clock=clock
    )

    assert controller.rejection_reason == (
        "capture_resource_pressure_sample_unavailable"
    )
    assert controller.health()["pressure_state"] == "unobserved_fail_closed"
    controller.observe(_pressure_sample(binding, 1))
    assert controller.required_full_fidelity_admissible is True
    clock.now += binding.policy.pressure_sample_max_age_seconds + 0.001
    assert controller.rejection_reason == "capture_resource_pressure_sample_stale"
    assert controller.health()["pressure_state"] == "stale_fail_closed"
    controller.observe(_pressure_sample(binding, 2))
    assert controller.required_full_fidelity_admissible is True


def test_hot_symbol_capacity_is_measured_rejects_with_gap_and_releases_exactly() -> None:
    binding = _binding(calibrated_hot_symbol_bytes=4_000_000)
    assert binding.budget.derived_hot_symbol_capacity == 2
    leases = BoundedHotSymbolLeases(
        identity=_identity(),
        resource_binding=binding,
    )
    first = leases.acquire("plsm", requested_at=BASE)
    second = leases.acquire("veee", requested_at=BASE + timedelta(milliseconds=1))
    rejected = leases.acquire("sobr", requested_at=BASE + timedelta(milliseconds=2))

    assert first.lease is not None and second.lease is not None
    assert rejected.lease is None
    assert rejected.gap is not None
    assert rejected.gap.reason == "hot_symbol_measured_capacity_exhausted"
    assert leases.acquire("PLSM", requested_at=BASE).lease == first.lease
    assert leases.release(first.lease) is True
    assert leases.release(first.lease) is False
    admitted = leases.acquire("SOBR", requested_at=BASE + timedelta(milliseconds=3))
    assert admitted.lease is not None
    assert leases.health()["capacity"] == 2


def test_hot_symbol_pressure_rejection_is_explicit_and_does_not_consume_slot() -> None:
    binding = _binding(calibrated_hot_symbol_bytes=4_000_000)
    controller = CaptureAdaptivePressureController(binding)
    for index in range(1, binding.policy.pressure_enter_samples + 1):
        controller.observe(_pressure_sample(binding, index, cpu_percent=90))
    leases = BoundedHotSymbolLeases(
        identity=_identity(),
        resource_binding=binding,
        pressure_controller=controller,
    )

    rejected = leases.acquire("PLSM", requested_at=BASE + timedelta(seconds=10))

    assert rejected.lease is None
    assert rejected.gap is not None
    assert rejected.gap.reason == "hot_symbol_capture_resource_pressure_cpu"
    assert leases.health()["active"] == 0


def test_pretrigger_promotion_preserves_buffered_events_and_marks_pressure_gap() -> None:
    binding = _binding()
    controller = CaptureAdaptivePressureController(binding)
    ring = BoundedPreTriggerRing.from_resource_binding(
        binding,
        horizon=timedelta(minutes=3),
        per_symbol_max_events=20,
        pressure_controller=controller,
    )
    assert ring.add(_event(1))
    for index in range(1, binding.policy.pressure_enter_samples + 1):
        controller.observe(_pressure_sample(binding, index, cpu_percent=90))

    promotion = ring.promote("PLSM", promoted_at=BASE + timedelta(seconds=10))

    assert len(promotion.events) == 1
    assert promotion.events[0].symbol == "PLSM"
    assert len(promotion.gaps) == 1
    assert promotion.gaps[0].reason == (
        "pretrigger_promotion_capture_resource_pressure_cpu"
    )
    assert ring.event_count == 0


def test_sustained_write_budget_rejects_without_blocking_and_emits_gap() -> None:
    # Resolve a small token bucket while leaving room for several minimal events.
    binding = _binding(
        available_memory_bytes=30_000,
        memory_reserve_bytes=10_000,
        disk_free_bytes=1_000_000,
        disk_reserve_bytes=100_000,
        write_bytes_per_second=4_000,
    )
    clock = _Clock()
    ingress = BoundedCaptureIngress.from_resource_binding(
        binding, monotonic_clock=clock
    )

    accepted = 0
    rejected_sequence = None
    for sequence in range(1, 30):
        if ingress.submit(_event(sequence)):
            accepted += 1
        else:
            rejected_sequence = sequence
            break

    assert accepted > 0
    assert rejected_sequence is not None
    batch = ingress.pop_batch(
        max_events=100, max_bytes=1_000_000, timeout_seconds=0
    )
    assert len(batch.events) == accepted
    assert len(batch.gaps) == 1
    assert batch.gaps[0][1].reason == "capture_write_bandwidth_budget_exceeded"
    assert batch.gaps[0][1].lost_count == 1
    assert ingress.health()["write_bandwidth_dropped"] == 1
    assert ingress.health()["backpressure_state"] == "failed_closed"

    clock.now += (
        binding.budget.async_queue_bytes
        / binding.budget.sustained_write_budget_bytes_per_second
        + 1
    )
    assert ingress.submit(_event(rejected_sequence + 1)) is True


def test_store_logs_resource_hashes_and_fails_closed_on_disk_reserve(tmp_path) -> None:
    binding = _binding()
    free = [binding.policy.disk_reserve_bytes + 1_000_000]
    store = ContentAddressedCaptureStore(
        tmp_path / "capture",
        compression_codec="zlib",
        resource_binding=binding,
        disk_usage_provider=lambda _path: SimpleNamespace(free=free[0]),
    )
    audit_path = (
        store.root / "resource_audits" / f"{binding.binding_sha256}.json"
    )
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["measurement_sha256"] == binding.measurement.measurement_sha256
    assert audit["policy_sha256"] == binding.policy.policy_sha256
    assert audit["budget_sha256"] == binding.budget.budget_sha256

    free[0] = binding.policy.disk_reserve_bytes + 1
    with pytest.raises(CaptureContractError, match="disk reserve"):
        store.write_events((_event(1),))
    health = store.resource_health()
    assert health["fail_closed"] is True
    assert health["resource_failure_reasons"] == (
        "capture_disk_reserve_breached",
    )
    with pytest.raises(CaptureContractError, match="resource gate is dirty"):
        store.write_events((_event(2),))


def test_store_fails_closed_before_crossing_disk_quota(tmp_path) -> None:
    binding = _binding(
        disk_free_bytes=130_000,
        disk_reserve_bytes=100_000,
        disk_fraction=0.50,
    )
    store = ContentAddressedCaptureStore(
        tmp_path / "capture",
        compression_codec="zlib",
        resource_binding=binding,
        disk_usage_provider=lambda _path: SimpleNamespace(free=1_000_000_000),
    )
    with pytest.raises(CaptureContractError, match="disk quota"):
        store.put_derived_artifact(
            identity=_identity(),
            kind="bars",
            window_start=BASE,
            window_end=BASE + timedelta(minutes=1),
            payload={"uncompressed": "0123456789abcdef" * 2_000},
        )
    assert store.resource_health()["resource_failure_reasons"] == (
        "capture_disk_quota_exceeded",
    )


def test_store_exclusive_owner_blocks_a_real_second_process_and_reopens_cleanly(
    tmp_path,
) -> None:
    binding = _binding()
    root = tmp_path / "capture"
    store = ContentAddressedCaptureStore(
        root,
        compression_codec="zlib",
        resource_binding=binding,
        disk_usage_provider=lambda _path: SimpleNamespace(free=1_000_000_000),
    )
    child = """
import sys
from app.services.trading.momentum_neural.replay_capture_contract import CaptureContractError
from app.services.trading.momentum_neural.replay_capture_runtime import ContentAddressedCaptureStore
try:
    ContentAddressedCaptureStore(sys.argv[1], compression_codec='zlib')
except CaptureContractError as exc:
    print(str(exc))
    raise SystemExit(0)
raise SystemExit(9)
"""
    completed = subprocess.run(
        [sys.executable, "-c", child, str(root)],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "another writer" in completed.stdout
    ownership = store.resource_health()["exclusive_ownership"]
    assert ownership["fail_closed"] is False
    assert ownership["record_sha256"]

    store.close()
    reopened = ContentAddressedCaptureStore(
        root,
        compression_codec="zlib",
        resource_binding=binding,
        disk_usage_provider=lambda _path: SimpleNamespace(free=1_000_000_000),
    )
    assert reopened.resource_health()["exclusive_ownership"]["state"] == "active"
    reopened.close()


def test_expired_store_owner_lease_fails_closed_before_publish(tmp_path) -> None:
    binding = _binding()
    now = [BASE]
    store = ContentAddressedCaptureStore(
        tmp_path / "capture",
        compression_codec="zlib",
        resource_binding=binding,
        disk_usage_provider=lambda _path: SimpleNamespace(free=1_000_000_000),
        wall_clock=lambda: now[0],
    )
    now[0] += timedelta(seconds=binding.policy.store_owner_lease_seconds + 1)

    with pytest.raises(CaptureContractError, match="ownership lease expired"):
        store.write_events((_event(1),))
    assert store.resource_health()["exclusive_ownership"]["fail_closed"] is True
    store.close()


def test_store_owner_heartbeat_renews_with_append_only_receipt(tmp_path) -> None:
    binding = _binding()
    now = [BASE]
    store = ContentAddressedCaptureStore(
        tmp_path / "capture",
        compression_codec="zlib",
        resource_binding=binding,
        disk_usage_provider=lambda _path: SimpleNamespace(free=1_000_000_000),
        wall_clock=lambda: now[0],
    )
    before = store.resource_health()["exclusive_ownership"]
    now[0] += timedelta(
        seconds=(
            binding.policy.store_owner_lease_seconds
            - binding.policy.store_owner_heartbeat_seconds
            + 1
        )
    )

    store.write_events((_event(1),))

    after = store.resource_health()["exclusive_ownership"]
    assert after["record_sha256"] != before["record_sha256"]
    receipts = tuple((store.root / "ownership" / "receipts").glob("*.json"))
    assert len(receipts) >= 2


def test_unexpired_foreign_owner_record_remains_fail_closed_after_lock_release(
    tmp_path,
) -> None:
    binding = _binding()
    root = tmp_path / "capture"
    store = ContentAddressedCaptureStore(
        root,
        compression_codec="zlib",
        resource_binding=binding,
        disk_usage_provider=lambda _path: SimpleNamespace(free=1_000_000_000),
    )
    active_receipt = next(
        path.read_bytes()
        for path in (root / "ownership" / "receipts").glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["state"] == "active"
    )
    store.close()
    (root / ".chili-capture-store-owner.lock").write_bytes(active_receipt)

    with pytest.raises(CaptureContractError, match="unexpired foreign ownership"):
        ContentAddressedCaptureStore(
            root,
            compression_codec="zlib",
            resource_binding=binding,
            disk_usage_provider=lambda _path: SimpleNamespace(free=1_000_000_000),
        )


def test_writer_requires_same_resource_binding_and_disk_failure_cannot_seal(
    tmp_path,
) -> None:
    binding = _binding()
    other = _binding(write_bytes_per_second=8_000_000)
    free = [1_000_000_000]
    store = ContentAddressedCaptureStore(
        tmp_path / "capture",
        compression_codec="zlib",
        resource_binding=binding,
        disk_usage_provider=lambda _path: SimpleNamespace(free=free[0]),
    )
    with pytest.raises(CaptureContractError, match="bindings do not match"):
        CaptureWriterWorker(
            ingress=BoundedCaptureIngress.from_resource_binding(other),
            store=store,
            batch_events=10,
            batch_bytes=other.budget.async_queue_bytes,
        )

    ingress = BoundedCaptureIngress.from_resource_binding(binding)
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=10,
        batch_bytes=binding.budget.async_queue_bytes,
        poll_seconds=0.001,
        flush_interval_seconds=0.001,
    )
    assert ingress.submit(_event(1))
    free[0] = binding.policy.disk_reserve_bytes
    worker.start()
    assert worker.stop(timeout_seconds=5) is False
    assert worker.health()["resource"]["fail_closed"] is True
    with pytest.raises(CaptureContractError, match="clean, error-free shutdown"):
        worker.seal_run(_identity())


def _closed_writer(
    store: ContentAddressedCaptureStore,
    binding: CaptureResourceBinding,
    event: CaptureEvent,
) -> CaptureWriterWorker:
    ingress = BoundedCaptureIngress.from_resource_binding(binding)
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=20,
        batch_bytes=binding.budget.async_queue_bytes,
        poll_seconds=0.001,
        flush_interval_seconds=0.01,
    )
    assert ingress.submit(event)
    worker.start()
    assert worker.stop(timeout_seconds=5)
    return worker


def test_bound_seal_commits_all_resource_hashes_and_mismatch_cannot_load(
    tmp_path,
) -> None:
    binding = _binding()
    root = tmp_path / "capture"
    store = ContentAddressedCaptureStore(
        root,
        compression_codec="zlib",
        resource_binding=binding,
        disk_usage_provider=lambda _path: SimpleNamespace(free=1_000_000_000),
    )
    identity = _identity(31)
    worker = _closed_writer(store, binding, _event(1, identity=identity))
    seal = worker.seal_run(identity)

    assert seal.schema_version == "chili-replay-capture-run-seal-v4"
    assert seal.resource_hashes == binding.hashes
    persisted = json.loads(
        next((store.root / "seals").rglob("*.json")).read_text(encoding="utf-8")
    )
    assert persisted["resource_hashes"] == binding.hashes
    assert store.load_sealed_run(
        identity, expected_seal_sha256=seal.seal_sha256
    ).seal == seal
    store.close()

    mismatched = _binding(write_bytes_per_second=8_000_000)
    other = ContentAddressedCaptureStore(
        root,
        compression_codec="zlib",
        resource_binding=mismatched,
        disk_usage_provider=lambda _path: SimpleNamespace(free=1_000_000_000),
    )
    with pytest.raises(CaptureContractError, match="resource binding does not match"):
        other.load_sealed_run(
            identity, expected_seal_sha256=seal.seal_sha256
        )
    other.close()


def test_read_only_v4_loader_has_no_writer_api_and_never_mutates_store(
    tmp_path,
    monkeypatch,
) -> None:
    binding = _binding()
    root = tmp_path / "capture"
    identity = _identity(131)
    mutable = ContentAddressedCaptureStore(
        root,
        compression_codec="zlib",
        resource_binding=binding,
        disk_usage_provider=lambda _path: SimpleNamespace(free=1_000_000_000),
    )
    worker = _closed_writer(mutable, binding, _event(1, identity=identity))
    seal = worker.seal_run(identity)
    mutable.close()
    before = _tree_snapshot(root)

    for writer_api in (
        "close",
        "put_payload",
        "retention_sweep",
        "seal_run",
        "sync",
        "write_events",
        "write_gaps",
    ):
        assert not hasattr(ReadOnlyV4CaptureStore, writer_api)

    def forbid_mutation(*_args, **_kwargs):
        raise AssertionError("read-only loader attempted filesystem mutation")

    guard = ReplayNetworkGuard()
    with guard:
        with monkeypatch.context() as patcher:
            patcher.setattr(Path, "mkdir", forbid_mutation)
            patcher.setattr(Path, "unlink", forbid_mutation)
            patcher.setattr(Path, "write_bytes", forbid_mutation)
            patcher.setattr(Path, "write_text", forbid_mutation)
            patcher.setattr(os, "replace", forbid_mutation)
            patcher.setattr(os, "fsync", forbid_mutation)
            verified = load_verified_replay_capture_v4(
                root,
                identity,
                expected_final_seal_sha256=seal.seal_sha256,
                expected_resource_binding=binding,
                coverage_request=_coverage_request(identity),
            )

    assert isinstance(verified, VerifiedReplayCapture)
    assert verified.final_seal_sha256 == seal.seal_sha256
    assert verified.resource_hashes == binding.hashes
    assert guard.attempt_count == 0
    assert _tree_snapshot(root) == before


def test_read_only_v4_loader_requires_external_identity_seal_and_resource_binding(
    tmp_path,
) -> None:
    binding = _binding()
    root = tmp_path / "capture"
    identity = _identity(132)
    mutable = ContentAddressedCaptureStore(
        root,
        compression_codec="zlib",
        resource_binding=binding,
        disk_usage_provider=lambda _path: SimpleNamespace(free=1_000_000_000),
    )
    worker = _closed_writer(mutable, binding, _event(1, identity=identity))
    seal = worker.seal_run(identity)
    mutable.close()

    with pytest.raises(CaptureContractError, match="seal does not match expected SHA"):
        load_verified_replay_capture_v4(
            root,
            identity,
            expected_final_seal_sha256="0" * 64,
            expected_resource_binding=binding,
            coverage_request=_coverage_request(identity),
        )

    with pytest.raises(CaptureContractError, match="resource binding does not match"):
        load_verified_replay_capture_v4(
            root,
            identity,
            expected_final_seal_sha256=seal.seal_sha256,
            expected_resource_binding=_binding(write_bytes_per_second=8_000_000),
            coverage_request=_coverage_request(identity),
        )

    unpinned = replace(
        _coverage_request(identity), expected_identity_sha256=None
    )
    with pytest.raises(CaptureContractError, match="must pin expected_identity_sha256"):
        load_verified_replay_capture_v4(
            root,
            identity,
            expected_final_seal_sha256=seal.seal_sha256,
            expected_resource_binding=binding,
            coverage_request=unpinned,
        )


def test_read_only_v4_loader_rehashes_objects_and_preserves_private_attestation(
    tmp_path,
) -> None:
    binding = _binding()
    root = tmp_path / "capture"
    identity = _identity(133)
    mutable = ContentAddressedCaptureStore(
        root,
        compression_codec="zlib",
        resource_binding=binding,
        disk_usage_provider=lambda _path: SimpleNamespace(free=1_000_000_000),
    )
    worker = _closed_writer(mutable, binding, _event(1, identity=identity))
    seal = worker.seal_run(identity)
    mutable.close()
    request = _coverage_request(identity)

    verified = load_verified_replay_capture_v4(
        root,
        identity,
        expected_final_seal_sha256=seal.seal_sha256,
        expected_resource_binding=binding,
        coverage_request=request,
    )
    with pytest.raises(CaptureContractError, match="attestation mismatch"):
        replace(verified, resource_binding_sha256="0" * 64)

    event_chunk = next(row for row in seal.objects if row.kind == "event_chunk")
    object_path = root / event_chunk.relative_path
    object_path.write_bytes(object_path.read_bytes() + b"tamper")
    with pytest.raises(
        CaptureContractError,
        match="corrupt|content mismatch|hash mismatch|object set/counts/content root",
    ):
        load_verified_replay_capture_v4(
            root,
            identity,
            expected_final_seal_sha256=seal.seal_sha256,
            expected_resource_binding=binding,
            coverage_request=request,
        )


def test_unbound_legacy_seal_cannot_assert_resource_certification(tmp_path) -> None:
    store = ContentAddressedCaptureStore(
        tmp_path / "capture", compression_codec="zlib"
    )
    identity = _identity(32)
    ingress = BoundedCaptureIngress(
        max_events=10, max_bytes=500_000, max_gap_keys=8
    )
    assert ingress.submit(_event(1, identity=identity))
    worker = CaptureWriterWorker(
        ingress=ingress,
        store=store,
        batch_events=10,
        batch_bytes=500_000,
        poll_seconds=0.001,
    )
    worker.start()
    assert worker.stop(timeout_seconds=5)
    seal = worker.seal_run(identity)

    assert seal.schema_version == "chili-replay-capture-run-seal-v3"
    assert seal.resource_hashes is None
    assert "resource_hashes" not in seal.to_record()


def test_retention_tombstones_old_seal_but_preserves_pins_and_longer_derived_tier(
    tmp_path,
) -> None:
    binding = _binding()
    store = ContentAddressedCaptureStore(
        tmp_path / "capture",
        compression_codec="zlib",
        resource_binding=binding,
        disk_usage_provider=lambda _path: SimpleNamespace(free=1_000_000_000),
    )
    old_unpinned = _identity(11)
    old_pinned = _identity(12)
    old_sealed = _identity(13)
    young = _identity(14)
    fresh_backfill = _identity(15)
    tiered = _identity(16)
    planned_at = BASE + timedelta(days=100)

    old_unpinned_refs = store.write_events((_event(1, identity=old_unpinned),))
    store.write_events((_event(1, identity=old_pinned),))
    store.write_events((_event(1, identity=fresh_backfill),))
    pinned_derived = store.put_derived_artifact(
        identity=old_pinned,
        kind="bars",
        window_start=BASE,
        window_end=BASE + timedelta(minutes=1),
        payload={"bar": [1, 2, 3]},
    )
    old_derived = store.put_derived_artifact(
        identity=old_unpinned,
        kind="bars",
        window_start=BASE,
        window_end=BASE + timedelta(minutes=1),
        payload={"bar": [4, 5, 6]},
    )
    tiered_at = planned_at - timedelta(days=10)
    tiered_event_refs = store.write_events(
        (_event(1, identity=tiered, at=tiered_at),)
    )
    tiered_derived = store.put_derived_artifact(
        identity=tiered,
        kind="bars",
        window_start=tiered_at,
        window_end=tiered_at + timedelta(minutes=1),
        payload={"bar": [10, 11, 12]},
    )
    # Retention requires both old event time and old durable object age; a
    # newly imported/backfilled or still-dirty historical run is preserved.
    old_mtime = (BASE + timedelta(days=1)).timestamp()
    old_unpinned_event_path = store.root / old_unpinned_refs[0].relative_path
    store.sync()
    os.utime(old_unpinned_event_path, (old_mtime, old_mtime))
    os.utime(store.root / old_derived.relative_path, (old_mtime, old_mtime))
    tiered_mtime = tiered_at.timestamp()
    os.utime(
        store.root / tiered_event_refs[0].relative_path,
        (tiered_mtime, tiered_mtime),
    )
    os.utime(
        store.root / tiered_derived.relative_path,
        (tiered_mtime, tiered_mtime),
    )
    young_derived = store.put_derived_artifact(
        identity=young,
        kind="bars",
        window_start=planned_at - timedelta(days=1),
        window_end=planned_at - timedelta(hours=1),
        payload={"bar": [7, 8, 9]},
    )
    sealed_worker = _closed_writer(
        store, binding, _event(1, identity=old_sealed)
    )
    sealed = sealed_worker.seal_run(old_sealed)
    sealed_material: dict[Path, bytes] = {}
    for object_ref in sealed.objects:
        path = store.root / object_ref.relative_path
        sealed_material[path] = path.read_bytes()
        os.utime(path, (old_mtime, old_mtime))
    pin = CaptureRetentionPin(
        identity=old_pinned,
        reason="ross_labeled_window",
        window_start=BASE - timedelta(minutes=1),
        window_end=BASE + timedelta(minutes=2),
        evidence_sha256="f" * 64,
        created_at=planned_at - timedelta(days=1),
    )
    store.put_retention_pin(pin)
    unmanaged = store.root / "operator-notes" / "keep.txt"
    unmanaged.parent.mkdir(parents=True)
    unmanaged.write_text("not capture-owned", encoding="utf-8")

    result = store.retention_sweep(planned_at=planned_at)

    assert result["deleted_objects"] == 4
    assert result["retired_seals"] == 1
    assert not any(
        f"run={old_unpinned.run_id}" in path.as_posix()
        for path in (store.root / "events").rglob("*.jsonl.zlib")
    )
    assert not (store.root / old_derived.relative_path).exists()
    assert any(
        f"run={old_pinned.run_id}" in path.as_posix()
        for path in (store.root / "events").rglob("*.jsonl.zlib")
    )
    assert any(
        f"run={fresh_backfill.run_id}" in path.as_posix()
        for path in (store.root / "events").rglob("*.jsonl.zlib")
    )
    assert (store.root / pinned_derived.relative_path).exists()
    assert (store.root / young_derived.relative_path).exists()
    assert (store.root / tiered_derived.relative_path).exists()
    assert not (store.root / tiered_event_refs[0].relative_path).exists()
    disposition = store.load_retention_dispositions()
    assert len(disposition) == 1
    assert disposition[0].seal_sha256 == sealed.seal_sha256
    assert disposition[0].disposition == "raw_deleted"
    assert next((store.root / "seals").rglob(f"{sealed.seal_sha256}.json")).is_file()
    with pytest.raises(CaptureContractError, match="irreversibly retired"):
        store.load_sealed_run(
            old_sealed, expected_seal_sha256=sealed.seal_sha256
        )
    # Reintroducing exact old bytes cannot erase the append-only tombstone.
    for path, content in sealed_material.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    with pytest.raises(CaptureContractError, match="irreversibly retired"):
        store.load_sealed_run(
            old_sealed, expected_seal_sha256=sealed.seal_sha256
        )
    with pytest.raises(CaptureContractError, match="cannot be resurrected"):
        store.put_retention_pin(
            CaptureRetentionPin(
                identity=old_sealed,
                reason="traded_window",
                window_start=BASE,
                window_end=BASE + timedelta(minutes=1),
                evidence_sha256="1" * 64,
                created_at=planned_at,
            )
        )
    assert unmanaged.read_text(encoding="utf-8") == "not capture-owned"
    plan = json.loads((store.root / result["plan_path"]).read_text(encoding="utf-8"))
    assert set(plan["tiers"]) == {"raw", "derived"}
    assert plan["resource_hashes"] == binding.hashes
    assert plan["pin_sha256s"] == [pin.pin_sha256]
    assert len(plan["seal_dispositions"]) == 1


def test_corrupt_pin_fails_retention_before_any_delete(tmp_path) -> None:
    binding = _binding()
    store = ContentAddressedCaptureStore(
        tmp_path / "capture",
        compression_codec="zlib",
        resource_binding=binding,
        disk_usage_provider=lambda _path: SimpleNamespace(free=1_000_000_000),
    )
    identity = _identity(21)
    refs = store.write_events((_event(1, identity=identity),))
    candidate = store.root / refs[0].relative_path
    bad = store.root / "retention" / "pins" / f"{'0' * 64}.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{}", encoding="utf-8")

    with pytest.raises(CaptureContractError, match="pin content address"):
        store.retention_sweep(planned_at=BASE + timedelta(days=10))
    assert candidate.exists()


def test_cold_archive_receipt_is_exactly_bound_and_content_addressed(tmp_path) -> None:
    binding = _binding()
    store = ContentAddressedCaptureStore(
        tmp_path / "capture",
        compression_codec="zlib",
        resource_binding=binding,
        disk_usage_provider=lambda _path: SimpleNamespace(free=1_000_000_000),
    )
    identity = _identity(41)
    worker = _closed_writer(store, binding, _event(1, identity=identity))
    seal = worker.seal_run(identity)
    receipt = CaptureColdArchiveReceipt(
        identity=identity,
        seal_sha256=seal.seal_sha256,
        archive_provider="offline_fixture_vault",
        archive_object_sha256="2" * 64,
        retrieval_evidence_sha256="3" * 64,
        archived_at=BASE + timedelta(days=1),
        resource_binding_sha256=binding.binding_sha256,
    )

    relative = store.put_cold_archive_receipt(receipt)

    assert relative.endswith(f"{receipt.receipt_sha256}.json")
    assert store.load_cold_archive_receipts() == (receipt,)


def test_corrupt_disposition_fails_retention_before_any_delete(tmp_path) -> None:
    binding = _binding()
    store = ContentAddressedCaptureStore(
        tmp_path / "capture",
        compression_codec="zlib",
        resource_binding=binding,
        disk_usage_provider=lambda _path: SimpleNamespace(free=1_000_000_000),
    )
    identity = _identity(42)
    refs = store.write_events((_event(1, identity=identity),))
    candidate = store.root / refs[0].relative_path
    store.sync()
    old_mtime = (BASE + timedelta(days=1)).timestamp()
    os.utime(candidate, (old_mtime, old_mtime))
    bad = (
        store.root
        / "retention"
        / "dispositions"
        / f"run={identity.run_id}"
        / f"generation={identity.generation}"
        / f"{'0' * 64}.json"
    )
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{}", encoding="utf-8")

    with pytest.raises(CaptureContractError, match="disposition content address"):
        store.retention_sweep(planned_at=BASE + timedelta(days=10))
    assert candidate.exists()


def test_python_network_guard_doc_is_explicitly_noncertifying() -> None:
    doc = ReplayNetworkGuard.__doc__ or ""
    assert "noncertifying" in doc
    assert "native libraries" in doc
    assert "subprocesses" in doc
