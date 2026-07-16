from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading
import time

import pytest

from app.services.trading.momentum_neural.iqfeed_l2_capture import (
    BoundedIqfeedL2CaptureHandoff,
    IqfeedL2CaptureEnvelope,
    IqfeedL2ProcessCaptureSink,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureContractError,
    CaptureEvent,
    CaptureIqfeedL2Checkpoint,
    CaptureIqfeedL2Delta,
    CaptureRunIdentity,
    CaptureStream,
    CoverageGap,
    IQFEED_L2_SOURCE_PROVENANCE_FIELD,
    sha256_json,
)


UTC = timezone.utc
BASE = datetime(2026, 7, 15, 15, 30, tzinfo=UTC)
BRIDGE_RUN_ID = "8294b035-49e0-4e17-924b-88bc0cfcd02b"
BRIDGE_SOURCE_SHA256 = "a" * 64
RESOURCE_BINDING_SHA256 = "b" * 64
BRIDGE_CONFIGURATION = {
    "schema_version": "chili.iqfeed-depth-bridge.capture-config.v1",
    "protocol": "6.2",
    "provider_timezone": "America/New_York",
}
BRIDGE_CONFIGURATION_SHA256 = sha256_json(BRIDGE_CONFIGURATION)


def _delta(
    *,
    symbol: str = "VEEE",
    generation: int = 7,
    sequence: int = 11,
    provider_at: datetime | None = None,
) -> dict:
    provider = provider_at or BASE + timedelta(milliseconds=800)
    return {
        "sym": symbol,
        "venue": "ARCX",
        "side": "A",
        "px": 4.12,
        "sz": 100.0,
        "condition_code": "4",
        "provider_at": provider,
        "received_at": BASE + timedelta(seconds=1),
        "bridge": "iqfeed-depth-bridge-test",
        "bridge_run_id": BRIDGE_RUN_ID,
        "connection_generation": generation,
        "source_frame_sequence": sequence,
        "source_frame_sha256": f"{sequence:064x}",
    }


def _checkpoint(
    *,
    symbol: str = "VEEE",
    generation: int = 7,
    covered: int = 10,
) -> dict:
    return {
        "sym": symbol,
        "received_at": BASE + timedelta(seconds=1),
        "bridge": "iqfeed-depth-bridge-test",
        "bridge_run_id": BRIDGE_RUN_ID,
        "connection_generation": generation,
        "covered_through_source_frame_sequence": covered,
        "covered_through_source_frame_sha256": f"{covered:064x}",
        "initial_snapshot_complete": False,
        "completion_basis": "provider_snapshot_completion_boundary_unavailable",
        "levels": [
            {
                "venue": "ARCX",
                "side": "A",
                "px": 4.12,
                "sz": 100.0,
                "provider_at": BASE + timedelta(milliseconds=800),
                "connection_generation": generation,
                "source_frame_sequence": covered - 1,
                "source_frame_sha256": f"{covered - 1:064x}",
                "condition_code": "4",
            },
            {
                "venue": "BATS",
                "side": "B",
                "px": 4.11,
                "sz": 200.0,
                "provider_at": BASE + timedelta(milliseconds=700),
                "connection_generation": generation,
                "source_frame_sequence": covered - 2,
                "source_frame_sha256": f"{covered - 2:064x}",
                "condition_code": "4",
            },
        ],
    }


class _Sink:
    network_fallback_allowed = False
    capture_resource_binding_sha256 = RESOURCE_BINDING_SHA256
    capture_queue_event_limit = 32
    capture_queue_byte_limit = 2_000_000
    capture_gap_key_limit = 32

    def __init__(self) -> None:
        self.envelopes: list[IqfeedL2CaptureEnvelope] = []
        self.gaps: list[CoverageGap] = []

    def submit_envelope(self, envelope: IqfeedL2CaptureEnvelope) -> None:
        self.envelopes.append(envelope)

    def report_gap(self, gap: CoverageGap) -> None:
        self.gaps.append(gap)


class _BlockingSink(_Sink):
    def __init__(self, *, fail_checkpoint: bool = False) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail_checkpoint = fail_checkpoint

    def submit_envelope(self, envelope: IqfeedL2CaptureEnvelope) -> None:
        if envelope.stream is CaptureStream.L2_DEPTH_CHECKPOINT:
            self.entered.set()
            assert self.release.wait(timeout=2)
            if self.fail_checkpoint:
                raise RuntimeError("fixture checkpoint sink failure")
        super().submit_envelope(envelope)


def _handoff(
    sink,
    *,
    max_pending_events: int = 8,
    max_pending_bytes: int = 1_000_000,
    max_gap_keys: int = 8,
) -> BoundedIqfeedL2CaptureHandoff:
    return BoundedIqfeedL2CaptureHandoff(
        sink=sink,
        max_pending_events=max_pending_events,
        max_pending_bytes=max_pending_bytes,
        max_gap_keys=max_gap_keys,
        bridge_source_sha256=BRIDGE_SOURCE_SHA256,
        bridge_configuration=BRIDGE_CONFIGURATION,
        bridge_configuration_sha256=BRIDGE_CONFIGURATION_SHA256,
    )


def _delta_envelope(
    handoff: BoundedIqfeedL2CaptureHandoff,
    *,
    sequence: int = 11,
) -> IqfeedL2CaptureEnvelope:
    return IqfeedL2CaptureEnvelope.from_delta_row(
        _delta(sequence=sequence),
        available_at=BASE + timedelta(seconds=2),
        bridge_source_sha256=BRIDGE_SOURCE_SHA256,
        bridge_configuration_sha256=BRIDGE_CONFIGURATION_SHA256,
        capture_resource_binding_sha256=RESOURCE_BINDING_SHA256,
        handoff_configuration_sha256=handoff.handoff_configuration_sha256,
    )


def _checkpoint_envelope(
    handoff: BoundedIqfeedL2CaptureHandoff,
) -> IqfeedL2CaptureEnvelope:
    return IqfeedL2CaptureEnvelope.from_checkpoint_row(
        _checkpoint(),
        available_at=BASE + timedelta(seconds=2),
        bridge_source_sha256=BRIDGE_SOURCE_SHA256,
        bridge_configuration_sha256=BRIDGE_CONFIGURATION_SHA256,
        capture_resource_binding_sha256=RESOURCE_BINDING_SHA256,
        handoff_configuration_sha256=handoff.handoff_configuration_sha256,
    )


def _event(envelope: IqfeedL2CaptureEnvelope, *, sequence: int) -> CaptureEvent:
    return CaptureEvent(
        identity=CaptureRunIdentity(
            run_id="64cc0ab7-fef5-4fc5-bb73-a798bf0b7374",
            generation=1,
            code_build_sha256="1" * 64,
            config_sha256="2" * 64,
            feature_flags_sha256="3" * 64,
            account_identity_sha256="4" * 64,
            broker="alpaca",
            broker_environment="paper",
        ),
        sequence=sequence,
        stream=envelope.stream,
        provider="iqfeed",
        symbol=envelope.symbol,
        clocks=envelope.clocks,
        payload=envelope.payload,
    )


def test_l2_connection_boundary_exposes_only_current_hash_bound_generation():
    sink = _Sink()
    handoff = _handoff(sink)
    handoff.start()

    symbols = handoff.record_connection_boundary(
        at=BASE,
        bridge_run_id=BRIDGE_RUN_ID,
        connection_generation=7,
        active=True,
    )

    assert symbols == ()
    evidence = handoff.active_producer_generation()
    assert evidence is not None
    assert evidence.producer_id == "iqfeed_l2"
    assert evidence.provider_instance_id == BRIDGE_RUN_ID
    assert evidence.provider_generation == 7
    assert evidence.streams == (
        CaptureStream.L2_DEPTH_CHECKPOINT,
        CaptureStream.L2_DEPTH_DELTA,
    )
    assert handoff.health()["active_producer_generation_sha256"] == (
        evidence.evidence_sha256
    )
    with pytest.raises(CaptureContractError, match="does not match"):
        handoff.record_connection_boundary(
            at=BASE + timedelta(seconds=1),
            bridge_run_id=BRIDGE_RUN_ID,
            connection_generation=8,
            active=False,
        )

    assert handoff.record_connection_boundary(
        at=BASE + timedelta(seconds=2),
        bridge_run_id=BRIDGE_RUN_ID,
        connection_generation=7,
        active=False,
    ) == ()
    assert handoff.active_producer_generation() is None
    assert handoff.close()["active_producer_generation"] is None


def test_delta_preserves_exact_provider_clock_and_immutable_payload() -> None:
    handoff = _handoff(_Sink())
    envelope = _delta_envelope(handoff)

    assert envelope.clocks.provider_event_at == BASE + timedelta(milliseconds=800)
    assert envelope.clocks.market_reference_at is None
    assert envelope.payload[IQFEED_L2_SOURCE_PROVENANCE_FIELD][
        "timestamp_basis"
    ] == "iqfeed_l2_frame_date_time_et"
    first_hash = envelope.envelope_sha256
    returned = envelope.payload
    returned["symbol"] = "FORGED"
    returned[IQFEED_L2_SOURCE_PROVENANCE_FIELD]["source_frame_sequence"] = 999
    assert envelope.payload["symbol"] == "VEEE"
    assert envelope.envelope_sha256 == first_hash


def test_checkpoint_is_local_incomplete_and_generation_bound() -> None:
    handoff = _handoff(_Sink())
    envelope = _checkpoint_envelope(handoff)

    assert envelope.clocks.provider_event_at is None
    assert envelope.clocks.market_reference_at == BASE + timedelta(milliseconds=800)
    assert envelope.payload["initial_snapshot_complete"] is False
    assert envelope.payload["exact_level_event_clock_complete"] is True
    assert all(
        level["connection_generation"] == 7
        for level in envelope.payload["levels"]
    )

    forged = _checkpoint()
    forged["levels"][0]["connection_generation"] = 6
    with pytest.raises(CaptureContractError, match="foreign-generation"):
        IqfeedL2CaptureEnvelope.from_checkpoint_row(
            forged,
            available_at=BASE + timedelta(seconds=2),
            bridge_source_sha256=BRIDGE_SOURCE_SHA256,
            bridge_configuration_sha256=BRIDGE_CONFIGURATION_SHA256,
            capture_resource_binding_sha256=RESOURCE_BINDING_SHA256,
            handoff_configuration_sha256=handoff.handoff_configuration_sha256,
        )


def test_typed_replay_parsers_accept_exact_delta_but_never_promote_checkpoint() -> None:
    handoff = _handoff(_Sink())
    delta = CaptureIqfeedL2Delta.from_event(_event(_delta_envelope(handoff), sequence=1))
    checkpoint = CaptureIqfeedL2Checkpoint.from_event(
        _event(_checkpoint_envelope(handoff), sequence=2)
    )

    assert delta.side == "A"
    assert delta.event.clocks.provider_event_at is not None
    assert checkpoint.initial_snapshot_complete is False
    assert (
        checkpoint.completion_basis
        == "provider_snapshot_completion_boundary_unavailable"
    )


def test_process_sink_treats_gapped_rejection_as_failure_and_checks_gap_ack() -> None:
    class _Service:
        network_fallback_allowed = False
        capture_resource_binding_sha256 = RESOURCE_BINDING_SHA256
        capture_queue_event_limit = 8
        capture_queue_byte_limit = 2_000_000
        capture_gap_key_limit = 8

        def submit_hot_input(self, **_value):
            return SimpleNamespace(
                accepted=False,
                coverage_gap_recorded=True,
                disposition="fixture_store_rejected",
            )

        def record_hot_gap(self, _gap):
            return False

    sink = IqfeedL2ProcessCaptureSink(_Service())
    envelope = _delta_envelope(_handoff(_Sink()))
    with pytest.raises(CaptureContractError, match="fixture_store_rejected"):
        sink.submit_envelope(envelope)
    with pytest.raises(CaptureContractError, match="was not persisted"):
        sink.report_gap(
            CoverageGap(
                stream=CaptureStream.L2_DEPTH_DELTA,
                symbol="VEEE",
                reason="fixture",
                first_available_at=BASE,
                last_available_at=BASE,
                lost_count=1,
            )
        )


def test_cold_symbol_is_ignored_without_claiming_or_gapping_coverage() -> None:
    sink = _Sink()
    handoff = _handoff(sink)
    handoff.start()

    assert handoff.offer_delta_rows(
        [_delta(symbol="COLD")], available_at=BASE + timedelta(seconds=2)
    ) == (0, 0, 1)
    assert handoff.wait_until_idle(1)
    health = handoff.close()
    assert health["ignored_cold"] == 1
    assert sink.envelopes == []
    assert sink.gaps == []


def test_checkpoint_is_pending_until_durable_and_orders_before_delta() -> None:
    sink = _BlockingSink()
    handoff = _handoff(sink)
    handoff.start()

    assert handoff.activate_hot_symbol(
        _checkpoint(), available_at=BASE + timedelta(seconds=2)
    )
    assert sink.entered.wait(timeout=1)
    assert handoff.offer_delta_rows(
        [_delta()], available_at=BASE + timedelta(seconds=3)
    ) == (1, 0, 0)
    health = handoff.health()
    assert health["pending_checkpoint_generations"] == {"VEEE": 7}
    assert health["active_generations"] == {}

    sink.release.set()
    assert handoff.wait_until_idle(1)
    health = handoff.close()
    assert [row.stream for row in sink.envelopes] == [
        CaptureStream.L2_DEPTH_CHECKPOINT,
        CaptureStream.L2_DEPTH_DELTA,
    ]
    assert health["active_generations"] == {"VEEE": 7}


def test_checkpoint_sink_failure_fences_already_queued_deltas() -> None:
    sink = _BlockingSink(fail_checkpoint=True)
    handoff = _handoff(sink)
    handoff.start()

    assert handoff.activate_hot_symbol(
        _checkpoint(), available_at=BASE + timedelta(seconds=2)
    )
    assert sink.entered.wait(timeout=1)
    assert handoff.offer_delta_rows(
        [_delta()], available_at=BASE + timedelta(seconds=3)
    ) == (1, 0, 0)
    sink.release.set()
    assert handoff.wait_until_idle(1)
    health = handoff.close()

    assert sink.envelopes == []
    assert health["active_generations"] == {}
    assert health["submit_failures"] == 1
    assert {gap.reason for gap in sink.gaps} == {
        "iqfeed_l2_capture_sink_submit_failed",
        "iqfeed_l2_capture_checkpoint_not_durable",
    }


def test_malformed_hot_delta_is_gapped_without_throwing_and_requires_recheckpoint() -> None:
    sink = _Sink()
    handoff = _handoff(sink)
    handoff.start()
    assert handoff.activate_hot_symbol(
        _checkpoint(), available_at=BASE + timedelta(seconds=2)
    )
    assert handoff.wait_until_idle(1)

    malformed = _delta()
    malformed["source_frame_sequence"] = "not-an-int"
    assert handoff.offer_delta_rows(
        [malformed], available_at=BASE + timedelta(seconds=3)
    ) == (0, 1, 0)
    assert handoff.offer_delta_rows(
        [_delta(sequence=12)], available_at=BASE + timedelta(seconds=4)
    ) == (0, 1, 0)
    assert handoff.wait_until_idle(1)
    health = handoff.close()
    assert health["active_generations"] == {}
    assert {gap.reason for gap in sink.gaps} == {
        "iqfeed_l2_capture_delta_invalid",
        "iqfeed_l2_capture_checkpoint_required",
    }


def test_missing_symbol_gaps_and_fences_every_requested_hot_symbol() -> None:
    sink = _Sink()
    handoff = _handoff(sink)
    handoff.start()
    for symbol in ("VEEE", "SDOT"):
        assert handoff.activate_hot_symbol(
            _checkpoint(symbol=symbol), available_at=BASE + timedelta(seconds=2)
        )
    assert handoff.wait_until_idle(1)

    assert handoff.offer_delta_rows(
        [{"connection_generation": 7}],
        available_at=BASE + timedelta(seconds=3),
    ) == (0, 2, 0)
    assert handoff.wait_until_idle(1)
    health = handoff.close()
    assert health["active_generations"] == {}
    assert {(gap.symbol, gap.reason) for gap in sink.gaps} == {
        ("SDOT", "iqfeed_l2_capture_delta_unattributed_invalid"),
        ("VEEE", "iqfeed_l2_capture_delta_unattributed_invalid"),
    }


def test_generation_boundary_and_nonmonotonic_sequence_force_new_checkpoint() -> None:
    sink = _Sink()
    handoff = _handoff(sink)
    handoff.start()
    assert handoff.activate_hot_symbol(
        _checkpoint(), available_at=BASE + timedelta(seconds=2)
    )
    assert handoff.wait_until_idle(1)

    assert handoff.offer_delta_rows(
        [_delta(generation=8)], available_at=BASE + timedelta(seconds=3)
    ) == (0, 1, 0)
    assert handoff.activate_hot_symbol(
        _checkpoint(generation=8, covered=20),
        available_at=BASE + timedelta(seconds=4),
    )
    assert handoff.wait_until_idle(1)
    assert handoff.offer_delta_rows(
        [_delta(generation=8, sequence=20)],
        available_at=BASE + timedelta(seconds=5),
    ) == (0, 1, 0)
    assert handoff.wait_until_idle(1)
    health = handoff.close()
    assert health["active_generations"] == {}
    assert {gap.reason for gap in sink.gaps} == {
        "iqfeed_l2_capture_generation_changed",
        "iqfeed_l2_capture_sequence_not_monotonic",
    }


def test_queue_overflow_is_nonblocking_gapped_and_fences_pending_generation() -> None:
    sink = _BlockingSink()
    handoff = _handoff(sink, max_pending_events=1)
    handoff.start()
    assert handoff.activate_hot_symbol(
        _checkpoint(), available_at=BASE + timedelta(seconds=2)
    )
    assert sink.entered.wait(timeout=1)
    assert handoff.offer_delta_rows(
        [_delta(sequence=11)], available_at=BASE + timedelta(seconds=3)
    ) == (1, 0, 0)

    started = time.perf_counter()
    assert handoff.offer_delta_rows(
        [_delta(sequence=12)], available_at=BASE + timedelta(seconds=4)
    ) == (0, 1, 0)
    assert time.perf_counter() - started < 0.1
    sink.release.set()
    assert handoff.wait_until_idle(1)
    health = handoff.close()
    assert health["queue_overflow_lost"] == 1
    assert health["active_generations"] == {}
    assert {gap.reason for gap in sink.gaps} == {
        "iqfeed_l2_capture_queue_overflow",
        "iqfeed_l2_capture_checkpoint_not_durable",
    }


def test_single_checkpoint_over_byte_budget_is_nonblocking_and_gapped() -> None:
    sink = _Sink()
    handoff = _handoff(sink, max_pending_bytes=1)
    checkpoint = _checkpoint_envelope(handoff)
    assert checkpoint.canonical_size_bytes > handoff.max_pending_bytes
    handoff.start()

    started = time.perf_counter()
    assert not handoff.activate_hot_symbol(
        _checkpoint(), available_at=BASE + timedelta(seconds=2)
    )
    assert time.perf_counter() - started < 0.1

    assert handoff.wait_until_idle(1)
    health = handoff.close()
    assert health["pending_bytes"] == 0
    assert health["oversized_envelope_lost"] == 1
    assert health["byte_overflow_lost"] == 1
    assert health["active_generations"] == {}
    assert [(gap.reason, gap.lost_count) for gap in sink.gaps] == [
        ("iqfeed_l2_capture_event_exceeds_byte_budget", 1)
    ]


def test_l2_byte_budget_counts_checkpoint_writer_inflight() -> None:
    probe = _handoff(_Sink())
    cap = max(
        _checkpoint_envelope(probe).canonical_size_bytes,
        _delta_envelope(probe).canonical_size_bytes,
    ) + 128
    sink = _BlockingSink()
    handoff = _handoff(sink, max_pending_bytes=cap)
    checkpoint = _checkpoint_envelope(handoff)
    delta = _delta_envelope(handoff)
    assert checkpoint.canonical_size_bytes <= cap
    assert delta.canonical_size_bytes <= cap
    assert checkpoint.canonical_size_bytes + delta.canonical_size_bytes > cap
    handoff.start()
    assert handoff.activate_hot_symbol(
        _checkpoint(), available_at=BASE + timedelta(seconds=2)
    )
    assert sink.entered.wait(timeout=1)

    started = time.perf_counter()
    assert handoff.offer_delta_rows(
        [_delta()], available_at=BASE + timedelta(seconds=3)
    ) == (0, 1, 0)
    assert time.perf_counter() - started < 0.1
    assert handoff.health()["pending_bytes"] == checkpoint.canonical_size_bytes
    sink.release.set()

    assert handoff.wait_until_idle(1)
    health = handoff.close()
    assert health["pending_bytes"] == 0
    assert health["peak_pending_bytes"] == checkpoint.canonical_size_bytes
    assert health["byte_overflow_lost"] == 1
    assert health["oversized_envelope_lost"] == 0
    assert health["active_generations"] == {}
    assert [(gap.reason, gap.lost_count) for gap in sink.gaps] == [
        ("iqfeed_l2_capture_queue_byte_overflow", 1)
    ]


def test_gap_key_exhaustion_never_coalesces_loss_into_a_foreign_symbol() -> None:
    handoff = _handoff(_Sink(), max_gap_keys=1)

    # Before start, each offered checkpoint is explicitly rejected.  The second
    # distinct symbol cannot fit the exact bounded ledger and must make the run
    # terminal instead of being attributed to VEEE.
    assert not handoff.activate_hot_symbol(
        _checkpoint(symbol="VEEE"), available_at=BASE + timedelta(seconds=2)
    )
    assert not handoff.activate_hot_symbol(
        _checkpoint(symbol="SDOT"), available_at=BASE + timedelta(seconds=2)
    )
    health = handoff.health()
    assert health["gap_ledger_overflow"] is True
    assert health["unpersisted_gap_count"] == 1
    assert health["terminal_error"] == "CaptureContractError"


def test_resource_binding_caps_are_hard_and_network_fallback_is_forbidden() -> None:
    sink = _Sink()
    sink.capture_queue_event_limit = 1
    with pytest.raises(CaptureContractError, match="measured resource binding"):
        _handoff(sink, max_pending_events=2)

    sink = _Sink()
    sink.capture_queue_byte_limit = 1
    with pytest.raises(CaptureContractError, match="measured resource binding"):
        _handoff(sink, max_pending_bytes=2)

    sink = _Sink()
    sink.network_fallback_allowed = True
    with pytest.raises(CaptureContractError, match="network fallback"):
        _handoff(sink)
