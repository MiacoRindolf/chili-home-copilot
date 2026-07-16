from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time
from typing import Callable
import uuid

import pytest

from app.services import massive_client as massive
from app.services.trading.momentum_neural.live_replay_capture import (
    CaptureIdentityEvidence,
    CaptureProviderRegistrationEvidence,
    LiveReplayCaptureCoordinator,
    MassiveWsLiveCaptureProducer,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureClocks,
    CaptureContractError,
    CaptureProducerSpec,
    CaptureRunIdentity,
    CaptureStream,
    sha256_json,
)
from app.services.trading.momentum_neural.replay_capture_runtime import (
    CaptureAdaptivePressureController,
    CaptureBudgetPolicy,
    CapturePressureSample,
    CaptureResourceBinding,
    CaptureResourceMeasurement,
)


UTC = timezone.utc
BASE = datetime(2026, 7, 14, 20, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _Socket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, value: str) -> None:
        self.sent.append(value)


def _wait_until(
    predicate: Callable[[], bool], *, timeout_seconds: float = 3.0
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.01)
    assert predicate(), "timed out waiting for bounded capture worker"


def _binding() -> CaptureResourceBinding:
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
        calibrated_hot_symbol_bytes=250_000,
        max_queue_events=128,
        max_ring_events=64,
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


def _run(
    root: Path,
    *,
    clock: _Clock,
    connection_generation: int = 7,
    heartbeat_timeout_seconds: float = 300,
    provider_stream: CaptureStream = CaptureStream.NBBO_QUOTE,
) -> tuple[LiveReplayCaptureCoordinator, massive.MassiveWSClient]:
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
    code = {"git_commit": "massive-capture-fixture", "dirty": True}
    config = {
        "capture_certification_symbol": "VEEE",
        "paper_execution": False,
        "massive_capture": {
            "instance_id": massive._MASSIVE_WS_RUN_ID,
            "connection_generation": connection_generation,
        },
    }
    flags = {"replay_capture": True}
    account = {
        "broker": "alpaca",
        "environment": "paper",
        "account_id": "fixture-paper",
    }
    identity = CaptureRunIdentity(
        run_id=str(uuid.uuid4()),
        generation=1,
        code_build_sha256=sha256_json(code),
        config_sha256=sha256_json(config),
        feature_flags_sha256=sha256_json(flags),
        account_identity_sha256=sha256_json(account),
        broker="alpaca",
        broker_environment="paper",
    )
    common = {
        "code_build_sha256": identity.code_build_sha256,
        "config_sha256": identity.config_sha256,
        "feature_flags_sha256": identity.feature_flags_sha256,
        "resource_binding_sha256": binding.binding_sha256,
    }
    local = CaptureProducerSpec(
        producer_id="live_fsm",
        instance_id=str(uuid.uuid4()),
        generation=identity.generation,
        streams=(
            CaptureStream.CODE_BUILD,
            CaptureStream.CONFIG_SNAPSHOT,
            CaptureStream.FEATURE_FLAG_SNAPSHOT,
            CaptureStream.ACCOUNT_RISK_SNAPSHOT,
        ),
        **common,
    )
    provider = CaptureProducerSpec(
        producer_id="massive_ws",
        instance_id=massive._MASSIVE_WS_RUN_ID,
        generation=connection_generation,
        streams=(provider_stream,),
        **common,
    )
    coordinator = LiveReplayCaptureCoordinator.create(
        root,
        identity=identity,
        certification_symbol="VEEE",
        resource_binding=binding,
        pressure_controller=controller,
        producers=(local, provider),
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        wall_clock=clock,
        pretrigger_horizon=timedelta(minutes=3),
        per_symbol_pretrigger_events=8,
        writer_batch_events=16,
        writer_batch_bytes=128 * 1024,
        writer_poll_seconds=0.01,
        writer_flush_interval_seconds=0.02,
        compression_codec="zlib",
        compression_level=3,
    )
    evidence = CaptureIdentityEvidence(
        code_build=code,
        config=config,
        feature_flags=flags,
        account_identity=account,
        account_risk_snapshot={
            "equity": "71868.33",
            "buying_power": "287473.32",
            "portfolio_heat_r": "0",
        },
        account_query={"operation": "get_account", "environment": "paper"},
        account_provider="alpaca",
    )
    coordinator.start(evidence)
    client = massive.MassiveWSClient()
    client._connection_generation = connection_generation
    client._authenticated_generation = connection_generation
    client._ws = _Socket()
    return coordinator, client


def _quote_frame(
    *,
    sequence: int = 101,
    provider_at: datetime | None = None,
) -> str:
    provider_at = provider_at or BASE + timedelta(seconds=3)
    return json.dumps(
        [
            {
                "ev": "Q",
                "sym": "VEEE",
                "t": int(provider_at.timestamp() * 1000),
                "q": sequence,
                "bp": 4.10,
                "ap": 4.12,
                "bs": 100,
                "as": 200,
                "bx": 11,
                "ax": 12,
                "c": 1,
                "i": [2],
                "z": 3,
            }
        ]
    )


def _trade_frame(
    *,
    sequence: int = 201,
    provider_at: datetime | None = None,
) -> str:
    provider_at = provider_at or BASE + timedelta(seconds=3)
    return json.dumps(
        [
            {
                "ev": "T",
                "sym": "VEEE",
                "t": int(provider_at.timestamp() * 1000),
                "pt": int(provider_at.timestamp() * 1000) - 2,
                "q": sequence,
                "p": 4.11,
                "s": 100,
                "x": 11,
                "i": f"trade-{sequence}",
                "z": 3,
                "c": [12],
            }
        ]
    )


def test_massive_real_parser_frame_registers_submits_and_closes_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(BASE + timedelta(seconds=2))
    coordinator, client = _run(tmp_path / "massive", clock=clock)
    endpoint = coordinator.bind_external_producer("massive_ws")
    assert coordinator.identity.generation == 1
    assert endpoint.spec.generation == 7
    producer = MassiveWsLiveCaptureProducer(
        endpoint=endpoint,
        massive_client=client,
        symbol="VEEE",
        bounded_lateness_seconds=2,
        heartbeat_interval_seconds=60,
    )
    subscription = producer.start()
    assert subscription["acknowledgement"] == "first_exact_provider_frame_required"
    assert subscription["channels"] == ["Q"]
    assert subscription["request"] == {
        "action": "subscribe",
        "params": "Q.VEEE",
    }
    assert json.loads(client._ws.sent[-1]) == subscription["request"]
    assert coordinator.health()["producer_lifecycle"]["registered_producers"] == [
        "live_fsm"
    ]
    with pytest.raises(CaptureContractError, match="bound capture endpoint"):
        coordinator.submit_exact_input(
            stream=CaptureStream.NBBO_QUOTE,
            provider="massive_ws",
            symbol="VEEE",
            clocks=CaptureClocks(
                provider_event_at=BASE + timedelta(seconds=3),
                received_at=BASE + timedelta(seconds=3, milliseconds=10),
                available_at=BASE + timedelta(seconds=3, milliseconds=20),
            ),
            payload={"bid": 4.10, "ask": 4.12},
        )

    received_at = BASE + timedelta(seconds=3, milliseconds=10)
    available_at = BASE + timedelta(seconds=3, milliseconds=20)
    clock.now = available_at
    monkeypatch.setattr(massive.time, "time", lambda: available_at.timestamp())
    client._handle_messages(_quote_frame(), received_at=received_at.timestamp())

    _wait_until(lambda: producer.health()["registered_from_first_frame"])
    health = producer.health()
    assert health["registered_from_first_frame"] is True
    assert health["event_count"] == {"nbbo_quote": 1}
    assert health["provider_connection_generation"] == 7
    assert coordinator.health()["producer_lifecycle"]["registered_producers"] == [
        "live_fsm",
        "massive_ws",
    ]

    clock.now = BASE + timedelta(seconds=4)
    producer.stop()
    producer_health = producer.health()
    assert producer_health["provider_watermark_available"] is False
    assert producer_health["provider_continuity_provable"] is False
    assert producer_health["provider_continuity_blocker_recorded"] is True
    assert producer_health["gap_reasons"] == {
        "massive_ws_provider_continuity_unprovable": 1
    }
    lifecycle = coordinator.health()["producer_lifecycle"]
    assert lifecycle["quiescent_producers"] == ["massive_ws"]
    assert lifecycle["closed_producers"] == ["massive_ws"]

    clock.now = BASE + timedelta(seconds=5)
    handoff = coordinator.stop_and_seal()
    assert handoff.producer_lifecycle_candidate is False
    assert handoff.gap_count == 1
    assert handoff.sequence_min == 1
    assert handoff.sequence_max == handoff.event_count


def test_external_registration_generation_must_match_run_open_roster(
    tmp_path: Path,
) -> None:
    clock = _Clock(BASE + timedelta(seconds=3))
    coordinator, _client = _run(tmp_path / "generation-mismatch", clock=clock)
    endpoint = coordinator.bind_external_producer("massive_ws")
    payload = {"bid": 4.10, "ask": 4.12}
    clocks = CaptureClocks(
        provider_event_at=BASE + timedelta(seconds=2),
        received_at=BASE + timedelta(seconds=2, milliseconds=10),
        available_at=BASE + timedelta(seconds=2, milliseconds=20),
    )
    evidence = CaptureProviderRegistrationEvidence(
        producer_id="massive_ws",
        provider="massive_ws",
        provider_instance_id=endpoint.spec.instance_id,
        provider_generation=endpoint.spec.generation + 1,
        evidence_kind="first_provider_frame",
        source_payload_sha256=sha256_json(payload),
        provider_event_at=clocks.provider_event_at,
        received_at=clocks.received_at,
        provider_sequence=101,
    )

    with pytest.raises(CaptureContractError, match="generation differs from RUN_OPEN"):
        endpoint.register_and_submit_first(
            evidence=evidence,
            stream=CaptureStream.NBBO_QUOTE,
            provider="massive_ws",
            payload=payload,
            clocks=clocks,
            symbol="VEEE",
        )
    assert coordinator.health()["producer_lifecycle"]["registered_producers"] == [
        "live_fsm"
    ]
    coordinator.abort(reason="generation_mismatch_fixture_complete")


def test_massive_endpoint_rejects_connection_generation_outside_run_open(
    tmp_path: Path,
) -> None:
    clock = _Clock(BASE + timedelta(seconds=2))
    coordinator, client = _run(tmp_path / "constructor-mismatch", clock=clock)
    client._connection_generation += 1
    client._authenticated_generation = client._connection_generation

    with pytest.raises(CaptureContractError, match="source generation differs"):
        MassiveWsLiveCaptureProducer(
            endpoint=coordinator.bind_external_producer("massive_ws"),
            massive_client=client,
            symbol="VEEE",
            bounded_lateness_seconds=2,
            heartbeat_interval_seconds=60,
        )
    coordinator.abort(reason="constructor_generation_mismatch_fixture_complete")


def test_massive_missing_first_frame_never_fakes_registration_or_close(
    tmp_path: Path,
) -> None:
    clock = _Clock(BASE + timedelta(seconds=2))
    coordinator, client = _run(tmp_path / "missing", clock=clock)
    endpoint = coordinator.bind_external_producer("massive_ws")
    producer = MassiveWsLiveCaptureProducer(
        endpoint=endpoint,
        massive_client=client,
        symbol="VEEE",
        bounded_lateness_seconds=2,
        heartbeat_interval_seconds=60,
    )
    producer.start()
    with pytest.raises(CaptureContractError, match="never received a first-frame"):
        producer.stop()
    lifecycle = coordinator.health()["producer_lifecycle"]
    assert lifecycle["registered_producers"] == ["live_fsm"]
    assert lifecycle["closed_producers"] == []
    with pytest.raises(CaptureContractError, match="external producers must"):
        coordinator.stop_and_seal()
    coordinator.abort(reason="missing_massive_provider_ack")


def test_massive_skipped_q_values_are_valid_but_regression_is_a_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(BASE + timedelta(seconds=2))
    coordinator, client = _run(tmp_path / "sequence", clock=clock)
    producer = MassiveWsLiveCaptureProducer(
        endpoint=coordinator.bind_external_producer("massive_ws"),
        massive_client=client,
        symbol="VEEE",
        bounded_lateness_seconds=2,
        heartbeat_interval_seconds=60,
    )
    producer.start()
    monkeypatch.setattr(massive.time, "time", lambda: clock.now.timestamp())

    clock.now = BASE + timedelta(seconds=3, milliseconds=20)
    client._handle_messages(
        _quote_frame(sequence=101),
        received_at=(BASE + timedelta(seconds=3, milliseconds=10)).timestamp(),
    )
    _wait_until(lambda: producer.health()["event_count"] == {"nbbo_quote": 1})

    clock.now = BASE + timedelta(seconds=3, milliseconds=40)
    client._handle_messages(
        _quote_frame(
            sequence=109,
            provider_at=BASE + timedelta(seconds=3, milliseconds=20),
        ),
        received_at=(BASE + timedelta(seconds=3, milliseconds=30)).timestamp(),
    )
    _wait_until(lambda: producer.health()["event_count"] == {"nbbo_quote": 2})
    assert producer.health()["gap_reasons"] == {}

    clock.now = BASE + timedelta(seconds=3, milliseconds=60)
    client._handle_messages(
        _quote_frame(
            sequence=108,
            provider_at=BASE + timedelta(seconds=3, milliseconds=40),
        ),
        received_at=(BASE + timedelta(seconds=3, milliseconds=50)).timestamp(),
    )
    _wait_until(
        lambda: producer.health()["gap_reasons"].get(
            "massive_ws_sequence_nonmonotonic"
        )
        == 1
    )
    assert producer.health()["event_count"] == {"nbbo_quote": 2}
    assert producer.health()["last_provider_sequence"] == {"nbbo_quote": 109}

    clock.now = BASE + timedelta(seconds=4)
    producer.stop()
    clock.now = BASE + timedelta(seconds=5)
    handoff = coordinator.stop_and_seal()
    assert handoff.producer_lifecycle_candidate is False
    assert handoff.gap_count == 2


def test_massive_et_session_reset_and_connection_generation_change_fence_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for case in ("session", "generation"):
        clock = _Clock(BASE + timedelta(seconds=2))
        coordinator, client = _run(
            tmp_path / case,
            clock=clock,
            heartbeat_timeout_seconds=40_000,
        )
        endpoint = coordinator.bind_external_producer("massive_ws")
        producer = MassiveWsLiveCaptureProducer(
            endpoint=endpoint,
            massive_client=client,
            symbol="VEEE",
            bounded_lateness_seconds=2,
            heartbeat_interval_seconds=60,
        )
        producer.start()
        monkeypatch.setattr(massive.time, "time", lambda: clock.now.timestamp())

        first_provider_at = (
            BASE - timedelta(hours=17)
            if case == "session"
            else BASE + timedelta(seconds=3)
        )
        clock.now = BASE + timedelta(seconds=3, milliseconds=20)
        client._handle_messages(
            _quote_frame(sequence=101, provider_at=first_provider_at),
            received_at=(BASE + timedelta(seconds=3, milliseconds=10)).timestamp(),
        )
        _wait_until(lambda: producer.health()["registered_from_first_frame"])

        if case == "session":
            next_provider_at = BASE + timedelta(seconds=3, milliseconds=30)
            clock.now = BASE + timedelta(seconds=3, milliseconds=50)
            client._handle_messages(
                _quote_frame(sequence=1, provider_at=next_provider_at),
                received_at=(
                    BASE + timedelta(seconds=3, milliseconds=40)
                ).timestamp(),
            )
            reason = "massive_ws_session_boundary_crossed"
        else:
            next_provider_at = BASE + timedelta(seconds=4)
            clock.now = next_provider_at + timedelta(milliseconds=20)
            client._connection_generation = 8
            client._authenticated_generation = 8
            client._handle_messages(
                _quote_frame(sequence=102, provider_at=next_provider_at),
                received_at=(next_provider_at + timedelta(milliseconds=10)).timestamp(),
            )
            reason = "massive_ws_generation_changed"

        _wait_until(lambda: producer.health()["terminal_fenced"])
        health = producer.health()
        assert health["gap_reasons"].get(reason) == 1
        assert health["event_count"] == {"nbbo_quote": 1}

        clock.now = max(clock.now, next_provider_at + timedelta(seconds=1))
        producer.stop()
        clock.now += timedelta(seconds=1)
        handoff = coordinator.stop_and_seal()
        assert handoff.producer_lifecycle_candidate is False
        assert handoff.gap_count >= 2


def test_massive_parser_never_waits_on_capture_lock_and_overflow_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(BASE + timedelta(seconds=3, milliseconds=20))
    coordinator, client = _run(tmp_path / "pressure", clock=clock)
    producer = MassiveWsLiveCaptureProducer(
        endpoint=coordinator.bind_external_producer("massive_ws"),
        massive_client=client,
        symbol="VEEE",
        bounded_lateness_seconds=2,
        heartbeat_interval_seconds=60,
        max_pending_events=1,
    )
    producer.start()
    monkeypatch.setattr(massive.time, "time", lambda: clock.now.timestamp())
    observed_sequences: list[int | None] = []

    def listener(_symbol: str, snapshot: object) -> None:
        observed_sequences.append(getattr(snapshot, "sequence", None))

    massive.register_tick_listener("VEEE", listener)
    try:
        # Neither capture control-plane lock may delay the parser callback.
        with coordinator._lock:
            with client._capture_sinks_lock, producer._condition:
                parser = threading.Thread(
                    target=client._handle_messages,
                    kwargs={
                        "raw": _quote_frame(sequence=101),
                        "received_at": (
                            BASE + timedelta(seconds=3, milliseconds=10)
                        ).timestamp(),
                    },
                )
                parser.start()
                parser.join(timeout=0.5)
                assert not parser.is_alive(), (
                    "parser waited on a capture control-plane lock"
                )
                assert observed_sequences == [101]
            _wait_until(lambda: producer.health()["worker_inflight"] == 1)

            client._handle_messages(
                _quote_frame(sequence=109),
                received_at=(
                    BASE + timedelta(seconds=3, milliseconds=10)
                ).timestamp(),
            )
            client._handle_messages(
                _quote_frame(sequence=110),
                received_at=(
                    BASE + timedelta(seconds=3, milliseconds=10)
                ).timestamp(),
            )
            # The queue-overflowed frame is explicitly non-admitted and cannot
            # influence either a strategy listener or a derived candle.
            assert observed_sequences == [101, 109]
            assert producer.health()["overflow_lost_count"] == 1

        _wait_until(
            lambda: producer.health()["gap_reasons"].get(
                "massive_ws_capture_queue_overflow"
            )
            == 1
        )
        _wait_until(lambda: producer.health()["event_count"] == {"nbbo_quote": 2})
        health = producer.health()
        assert health["queue_capacity"] == 1
        assert health["pending_overflow_count"] == 0
        assert health["coverage_failed"] is True

        clock.now = BASE + timedelta(seconds=4)
        producer.stop()
        clock.now = BASE + timedelta(seconds=5)
        handoff = coordinator.stop_and_seal()
        assert handoff.producer_lifecycle_candidate is False
        assert handoff.gap_count == 2
    finally:
        massive.unregister_tick_listener("VEEE", listener)


def test_massive_exact_channel_roster_survives_same_generation_resubscribe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(BASE + timedelta(seconds=2))
    coordinator, client = _run(tmp_path / "roster", clock=clock)
    producer = MassiveWsLiveCaptureProducer(
        endpoint=coordinator.bind_external_producer("massive_ws"),
        massive_client=client,
        symbol="VEEE",
        bounded_lateness_seconds=2,
        heartbeat_interval_seconds=60,
    )
    producer.start()
    assert client._subscriptions == {"VEEE": {"Q"}}

    # Register the producer from a real owned Q frame before exercising the
    # resubscribe bookkeeping.  Stopping an external producer that never
    # acknowledged any provider frame must remain fail-closed; otherwise this
    # roster-only test would accidentally weaken the lifecycle contract.
    clock.now = BASE + timedelta(seconds=2, milliseconds=20)
    monkeypatch.setattr(massive.time, "time", lambda: clock.now.timestamp())
    client._handle_messages(
        _quote_frame(sequence=100, provider_at=BASE + timedelta(seconds=2)),
        received_at=(BASE + timedelta(seconds=2, milliseconds=10)).timestamp(),
    )
    _wait_until(lambda: producer.health()["registered_from_first_frame"])

    client._ws.sent.clear()
    client._subscribe_all()
    assert [json.loads(row) for row in client._ws.sent] == [
        {"action": "subscribe", "params": "Q.VEEE"}
    ]

    # A new socket generation cannot inherit an old generation's producer
    # ownership.  No Q or T subscription is replayed until a new producer binds.
    client._connection_generation = 8
    client._authenticated_generation = 8
    client._ws.sent.clear()
    client._subscribe_all()
    assert client._ws.sent == []

    producer.stop()
    coordinator.abort(reason="exact_roster_fixture_complete")


def test_massive_unowned_trade_cannot_reach_listener_or_candle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(BASE + timedelta(seconds=2))
    coordinator, client = _run(tmp_path / "unowned-trade", clock=clock)
    producer = MassiveWsLiveCaptureProducer(
        endpoint=coordinator.bind_external_producer("massive_ws"),
        massive_client=client,
        symbol="VEEE",
        bounded_lateness_seconds=2,
        heartbeat_interval_seconds=60,
    )
    producer.start()
    monkeypatch.setattr(massive.time, "time", lambda: clock.now.timestamp())
    aggregator = massive.CandleAggregator(interval_seconds=60)
    monkeypatch.setattr(massive, "_candle_aggregators", {60: aggregator})
    observed: list[tuple[str, int | None]] = []

    def listener(symbol: str, snapshot: object) -> None:
        observed.append((symbol, getattr(snapshot, "sequence", None)))

    massive.register_tick_listener("VEEE", listener)
    try:
        clock.now = BASE + timedelta(seconds=3, milliseconds=20)
        client._handle_messages(
            _quote_frame(sequence=101),
            received_at=(BASE + timedelta(seconds=3, milliseconds=10)).timestamp(),
        )
        _wait_until(lambda: producer.health()["registered_from_first_frame"])
        assert observed == [("VEEE", 101)]

        clock.now = BASE + timedelta(seconds=3, milliseconds=40)
        client._handle_messages(
            _trade_frame(
                sequence=201,
                provider_at=BASE + timedelta(seconds=3, milliseconds=20),
            ),
            received_at=(BASE + timedelta(seconds=3, milliseconds=30)).timestamp(),
        )
        _wait_until(
            lambda: producer.health()["gap_reasons"].get(
                "massive_ws_unowned_t_frame"
            )
            == 1
        )
        assert observed == [("VEEE", 101)]
        assert "VEEE" not in aggregator._bars

        clock.now = BASE + timedelta(seconds=4)
        producer.stop()
        clock.now = BASE + timedelta(seconds=5)
        handoff = coordinator.stop_and_seal()
        assert handoff.producer_lifecycle_candidate is False
        assert handoff.gap_count == 2
    finally:
        massive.unregister_tick_listener("VEEE", listener)


def test_first_quote_does_not_acknowledge_trade_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(BASE + timedelta(seconds=2))
    coordinator, client = _run(
        tmp_path / "trade-ack",
        clock=clock,
        provider_stream=CaptureStream.PROVIDER_TRADE_PRINT,
    )
    producer = MassiveWsLiveCaptureProducer(
        endpoint=coordinator.bind_external_producer("massive_ws"),
        massive_client=client,
        symbol="VEEE",
        bounded_lateness_seconds=2,
        heartbeat_interval_seconds=60,
    )
    subscription = producer.start()
    assert subscription["channels"] == ["T"]
    assert subscription["request"]["params"] == "T.VEEE"
    monkeypatch.setattr(massive.time, "time", lambda: clock.now.timestamp())

    clock.now = BASE + timedelta(seconds=3, milliseconds=20)
    client._handle_messages(
        _quote_frame(sequence=101),
        received_at=(BASE + timedelta(seconds=3, milliseconds=10)).timestamp(),
    )
    _wait_until(
        lambda: producer.health()["gap_reasons"].get(
            "massive_ws_unowned_q_frame"
        )
        == 1
    )
    assert producer.health()["registered_from_first_frame"] is False

    clock.now = BASE + timedelta(seconds=3, milliseconds=40)
    client._handle_messages(
        _trade_frame(
            sequence=201,
            provider_at=BASE + timedelta(seconds=3, milliseconds=20),
        ),
        received_at=(BASE + timedelta(seconds=3, milliseconds=30)).timestamp(),
    )
    _wait_until(lambda: producer.health()["registered_from_first_frame"])
    assert producer.health()["event_count"] == {"provider_trade_print": 1}

    clock.now = BASE + timedelta(seconds=4)
    producer.stop()
    clock.now = BASE + timedelta(seconds=5)
    handoff = coordinator.stop_and_seal()
    assert handoff.producer_lifecycle_candidate is False
    assert handoff.gap_count == 2


def test_massive_authentication_requires_provider_ack() -> None:
    client = massive.MassiveWSClient()
    client._connection_generation = 4

    class _AuthSocket(_Socket):
        def __init__(self, response: str) -> None:
            super().__init__()
            self.response = response

        def recv(self) -> str:
            return self.response

    client._ws = _AuthSocket('[{"ev":"status","status":"auth_failed"}]')
    with pytest.raises(RuntimeError, match="not acknowledged"):
        client._authenticate()
    assert client.capture_source_identity["authenticated"] is False

    client._ws = _AuthSocket('[{"ev":"status","status":"auth_success"}]')
    client._authenticate()
    assert client.capture_source_identity["authenticated"] is True
