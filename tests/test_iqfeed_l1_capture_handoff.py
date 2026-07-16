from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading
import time

import pytest

import scripts.iqfeed_trade_bridge as bridge
from app.services.trading.momentum_neural.iqfeed_l1_capture import (
    BoundedIqfeedL1CaptureHandoff,
    IqfeedL1CaptureEnvelope,
    IqfeedL1ProcessCaptureSink,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureContractError,
    CaptureEvent,
    CaptureIqfeedPrint,
    CaptureRunIdentity,
    CaptureStream,
    CoverageGap,
    IQFEED_L1_SOURCE_PROVENANCE_FIELD,
    sha256_json,
)


UTC = timezone.utc
BASE = datetime(2026, 7, 15, 15, 30, tzinfo=UTC)
RESOURCE_BINDING_SHA256 = "b" * 64
HANDOFF_CONFIGURATION = {
    "schema_version": "chili.iqfeed-l1-capture-handoff-config.v2",
    "max_pending_events": 8,
    "max_pending_bytes": 1_000_000,
    "max_gap_keys": 8,
    "capture_resource_binding_sha256": RESOURCE_BINDING_SHA256,
}
HANDOFF_CONFIGURATION_SHA256 = sha256_json(HANDOFF_CONFIGURATION)


def _row(
    *,
    symbol: str = "VEEE",
    source_frame_sequence: int = 1,
    connection_generation: int = 7,
    received_at: datetime | None = None,
) -> dict:
    received = received_at or BASE + timedelta(milliseconds=10)
    reference = received - timedelta(milliseconds=100)
    return {
        "sym": symbol,
        "px": 4.12,
        "sz": 100.0,
        "bid": 4.11,
        "ask": 4.12,
        "provider_at": None,
        "provider_trade_reference_at": reference,
        "received_at": received,
        "basis": bridge.AUTHORITATIVE_TIMESTAMP_BASIS,
        "bridge": bridge.BRIDGE_BUILD,
        "message_type": "Q",
        "bridge_run_id": bridge.BRIDGE_RUN_ID,
        "connection_generation": connection_generation,
        "source_frame_sequence": source_frame_sequence,
        "source_frame_sha256": f"{source_frame_sequence:064x}",
    }


def _envelope(
    stream: CaptureStream = CaptureStream.NBBO_QUOTE,
    *,
    sequence: int = 1,
    symbol: str = "VEEE",
    handoff: BoundedIqfeedL1CaptureHandoff | None = None,
) -> IqfeedL1CaptureEnvelope:
    handoff_configuration = (
        HANDOFF_CONFIGURATION
        if handoff is None
        else handoff.handoff_configuration
    )
    handoff_configuration_sha256 = (
        HANDOFF_CONFIGURATION_SHA256
        if handoff is None
        else handoff.handoff_configuration_sha256
    )
    return IqfeedL1CaptureEnvelope.from_released_row(
        _row(symbol=symbol, source_frame_sequence=sequence),
        stream=stream,
        available_at=BASE + timedelta(seconds=1, milliseconds=sequence),
        bridge_source_sha256=bridge.BRIDGE_SOURCE_SHA256,
        bridge_configuration=bridge.BRIDGE_CAPTURE_CONFIGURATION,
        bridge_configuration_sha256=bridge.BRIDGE_CAPTURE_CONFIGURATION_SHA256,
        capture_resource_binding_sha256=(
            RESOURCE_BINDING_SHA256
            if handoff is None
            else handoff.capture_resource_binding_sha256
        ),
        handoff_configuration=handoff_configuration,
        handoff_configuration_sha256=handoff_configuration_sha256,
    )


class _Sink:
    network_fallback_allowed = False
    capture_resource_binding_sha256 = RESOURCE_BINDING_SHA256
    capture_queue_event_limit = 32
    capture_queue_byte_limit = 2_000_000
    capture_gap_key_limit = 32

    def __init__(self) -> None:
        self.envelopes: list[IqfeedL1CaptureEnvelope] = []
        self.gaps: list[CoverageGap] = []

    def submit_envelope(self, envelope: IqfeedL1CaptureEnvelope) -> None:
        self.envelopes.append(envelope)

    def report_gap(self, gap: CoverageGap) -> None:
        self.gaps.append(gap)


def _handoff(
    sink,
    *,
    max_pending_events: int = 8,
    max_pending_bytes: int = 1_000_000,
    max_gap_keys: int = 8,
) -> BoundedIqfeedL1CaptureHandoff:
    return BoundedIqfeedL1CaptureHandoff(
        sink=sink,
        max_pending_events=max_pending_events,
        max_pending_bytes=max_pending_bytes,
        max_gap_keys=max_gap_keys,
        bridge_source_sha256=bridge.BRIDGE_SOURCE_SHA256,
        bridge_configuration=bridge.BRIDGE_CAPTURE_CONFIGURATION,
        bridge_configuration_sha256=bridge.BRIDGE_CAPTURE_CONFIGURATION_SHA256,
    )


@pytest.mark.parametrize(
    "stream",
    [CaptureStream.NBBO_QUOTE, CaptureStream.IQFEED_PRINT],
)
def test_released_envelope_preserves_proxy_as_reference_not_event_clock(stream):
    envelope = _envelope(stream)

    assert envelope.clocks.provider_event_at is None
    assert envelope.clocks.market_reference_at == _row()[
        "provider_trade_reference_at"
    ]
    payload = envelope.payload
    provenance = payload[IQFEED_L1_SOURCE_PROVENANCE_FIELD]
    assert provenance["provider_event_at"] is None
    assert provenance["source_frame_sequence"] == 1
    assert provenance["bridge_source_sha256"] == bridge.BRIDGE_SOURCE_SHA256
    assert (
        provenance["bridge_configuration_sha256"]
        == bridge.BRIDGE_CAPTURE_CONFIGURATION_SHA256
    )
    assert len(envelope.envelope_sha256) == 64

    # The queued bytes remain stable even if a caller mutates its returned copy.
    payload["symbol"] = "FORGED"
    assert envelope.payload["symbol"] == "VEEE"


def test_released_envelope_refuses_trade_reference_laundering():
    row = _row()
    row["provider_at"] = row["provider_trade_reference_at"]

    with pytest.raises(CaptureContractError, match="cannot claim an exact"):
        IqfeedL1CaptureEnvelope.from_released_row(
            row,
            stream=CaptureStream.NBBO_QUOTE,
            available_at=BASE + timedelta(seconds=1),
            bridge_source_sha256=bridge.BRIDGE_SOURCE_SHA256,
            bridge_configuration=bridge.BRIDGE_CAPTURE_CONFIGURATION,
            bridge_configuration_sha256=(
                bridge.BRIDGE_CAPTURE_CONFIGURATION_SHA256
            ),
            capture_resource_binding_sha256=RESOURCE_BINDING_SHA256,
            handoff_configuration=HANDOFF_CONFIGURATION,
            handoff_configuration_sha256=HANDOFF_CONFIGURATION_SHA256,
        )


def test_q_frame_trade_proxy_cannot_be_promoted_to_replay_print_authority():
    envelope = _envelope(CaptureStream.IQFEED_PRINT)
    identity = CaptureRunIdentity(
        run_id="64cc0ab7-fef5-4fc5-bb73-a798bf0b7374",
        generation=1,
        code_build_sha256="1" * 64,
        config_sha256="2" * 64,
        feature_flags_sha256="3" * 64,
        account_identity_sha256="4" * 64,
        broker="alpaca",
        broker_environment="paper",
    )
    event = CaptureEvent(
        identity=identity,
        sequence=1,
        stream=envelope.stream,
        provider=envelope.provider,
        symbol=envelope.symbol,
        clocks=envelope.clocks,
        payload=envelope.payload,
    )

    with pytest.raises(CaptureContractError, match="exact provider event clock"):
        CaptureIqfeedPrint.from_event(event)


def test_process_sink_routes_only_already_observed_values_and_explicit_gaps():
    class _Service:
        network_fallback_allowed = False
        capture_resource_binding_sha256 = RESOURCE_BINDING_SHA256
        capture_queue_event_limit = 32
        capture_queue_byte_limit = 2_000_000
        capture_gap_key_limit = 32

        def __init__(self):
            self.inputs = []
            self.gaps = []

        def record_broad_input(self, **value):
            self.inputs.append(value)
            return SimpleNamespace(accepted=True, coverage_gap_recorded=False)

        def record_broad_gap(self, gap):
            self.gaps.append(gap)
            return ()

    service = _Service()
    sink = IqfeedL1ProcessCaptureSink(service)
    envelope = _envelope()
    gap = CoverageGap(
        stream=CaptureStream.NBBO_QUOTE,
        symbol="VEEE",
        reason="fixture_loss",
        first_available_at=envelope.clocks.available_at,
        last_available_at=envelope.clocks.available_at,
        lost_count=1,
    )

    sink.submit_envelope(envelope)
    sink.report_gap(gap)

    assert sink.network_fallback_allowed is False
    assert service.inputs == [
        {
            "stream": CaptureStream.NBBO_QUOTE,
            "provider": "iqfeed",
            "payload": envelope.payload,
            "clocks": envelope.clocks,
            "symbol": "VEEE",
        }
    ]
    assert service.gaps == [gap]


def test_process_sink_rejection_already_gapped_is_not_counted_or_double_gapped():
    class _RejectingService:
        network_fallback_allowed = False
        capture_resource_binding_sha256 = RESOURCE_BINDING_SHA256
        capture_queue_event_limit = 32
        capture_queue_byte_limit = 2_000_000
        capture_gap_key_limit = 32

        def __init__(self):
            self.gaps = []

        def record_broad_input(self, **_value):
            return SimpleNamespace(
                accepted=False,
                coverage_gap_recorded=True,
                disposition="coverage_unavailable",
            )

        def record_broad_gap(self, gap):
            self.gaps.append(gap)

    service = _RejectingService()
    handoff = _handoff(IqfeedL1ProcessCaptureSink(service))
    handoff.start()
    assert handoff.offer(_envelope(handoff=handoff))

    assert handoff.wait_until_idle(2.0)
    health = handoff.close()
    assert health["submit_failures"] == 1
    assert health["submitted"] == 0
    assert health["reported_gap_count"] == 0
    assert service.gaps == []


def test_bounded_worker_preserves_source_frame_order_across_trade_quote_queues():
    sink = _Sink()
    handoff = _handoff(sink)
    handoff.start()

    trade_rows = [_row(source_frame_sequence=1)]
    quote_rows = [
        _row(source_frame_sequence=2),
        _row(source_frame_sequence=1),
    ]
    accepted, rejected = handoff.offer_released_rows(
        trade_rows=trade_rows,
        quote_rows=quote_rows,
        available_at=BASE + timedelta(seconds=1),
    )

    assert (accepted, rejected) == (3, 0)
    assert handoff.wait_until_idle(2.0)
    health = handoff.close()
    assert [
        (event.source_frame_sequence, event.stream)
        for event in sink.envelopes
    ] == [
        (1, CaptureStream.IQFEED_PRINT),
        (1, CaptureStream.NBBO_QUOTE),
        (2, CaptureStream.NBBO_QUOTE),
    ]
    assert health["submitted"] == 3
    assert health["reported_gap_count"] == 0


def test_connection_boundary_exposes_hash_bound_generation_and_invalidates_on_close():
    sink = _Sink()
    handoff = _handoff(sink)
    handoff.start()
    bridge_run_id = "284f4454-fb2f-4b49-bcff-9ce3a5dfdd69"

    evidence = handoff.record_connection_boundary(
        at=BASE,
        bridge_run_id=bridge_run_id,
        connection_generation=7,
        active=True,
    )

    assert evidence is not None
    assert evidence.producer_id == "iqfeed_l1"
    assert evidence.provider_instance_id == bridge_run_id
    assert evidence.provider_generation == 7
    assert evidence.capture_resource_binding_sha256 == RESOURCE_BINDING_SHA256
    assert handoff.health()["active_producer_generation_sha256"] == (
        evidence.evidence_sha256
    )
    with pytest.raises(CaptureContractError, match="does not match"):
        handoff.record_connection_boundary(
            at=BASE + timedelta(seconds=1),
            bridge_run_id=bridge_run_id,
            connection_generation=8,
            active=False,
        )

    assert (
        handoff.record_connection_boundary(
            at=BASE + timedelta(seconds=2),
            bridge_run_id=bridge_run_id,
            connection_generation=7,
            active=False,
        )
        is None
    )
    assert handoff.active_producer_generation() is None
    assert handoff.wait_until_idle(2.0)
    health = handoff.close()
    assert health["active_producer_generation"] is None
    assert {(gap.stream, gap.reason) for gap in sink.gaps} == {
        (CaptureStream.IQFEED_PRINT, "iqfeed_l1_connection_boundary"),
        (CaptureStream.NBBO_QUOTE, "iqfeed_l1_connection_boundary"),
    }


def test_queue_overflow_returns_immediately_and_persists_coverage_gap():
    class _BlockingSink(_Sink):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def submit_envelope(self, envelope):
            self.entered.set()
            assert self.release.wait(timeout=2.0)
            super().submit_envelope(envelope)

    sink = _BlockingSink()
    handoff = _handoff(sink, max_pending_events=1)
    handoff.start()
    assert handoff.offer(_envelope(sequence=1, handoff=handoff))
    assert sink.entered.wait(timeout=1.0)
    assert handoff.offer(_envelope(sequence=2, handoff=handoff))

    started = time.monotonic()
    assert handoff.offer(_envelope(sequence=3, handoff=handoff)) is False
    assert time.monotonic() - started < 0.1
    sink.release.set()

    assert handoff.wait_until_idle(2.0)
    health = handoff.close()
    assert health["queue_overflow_lost"] == 1
    assert health["queue_overflow_incidents"] == 1
    assert health["reported_gap_count"] == 1
    assert [(gap.reason, gap.lost_count) for gap in sink.gaps] == [
        ("iqfeed_l1_capture_queue_overflow", 1)
    ]


def test_single_envelope_over_byte_budget_is_nonblocking_and_explicitly_gapped():
    sink = _Sink()
    handoff = _handoff(sink, max_pending_bytes=1)
    envelope = _envelope(handoff=handoff)
    assert envelope.canonical_size_bytes > handoff.max_pending_bytes
    handoff.start()

    started = time.monotonic()
    assert handoff.offer(envelope) is False
    assert time.monotonic() - started < 0.1

    assert handoff.wait_until_idle(2.0)
    health = handoff.close()
    assert health["oversized_envelope_lost"] == 1
    assert health["byte_overflow_lost"] == 1
    assert health["pending_bytes"] == 0
    assert [(gap.reason, gap.lost_count) for gap in sink.gaps] == [
        ("iqfeed_l1_capture_event_exceeds_byte_budget", 1)
    ]


def test_byte_budget_counts_writer_inflight_and_rejects_aggregate_overflow():
    class _BlockingSink(_Sink):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def submit_envelope(self, envelope):
            self.entered.set()
            assert self.release.wait(timeout=2.0)
            super().submit_envelope(envelope)

    probe = _handoff(_Sink())
    cap = _envelope(handoff=probe).canonical_size_bytes + 128
    sink = _BlockingSink()
    handoff = _handoff(sink, max_pending_bytes=cap)
    first = _envelope(sequence=1, handoff=handoff)
    second = _envelope(sequence=2, handoff=handoff)
    assert first.canonical_size_bytes <= cap
    assert second.canonical_size_bytes <= cap
    assert first.canonical_size_bytes + second.canonical_size_bytes > cap
    handoff.start()
    assert handoff.offer(first)
    assert sink.entered.wait(timeout=1.0)

    started = time.monotonic()
    assert handoff.offer(second) is False
    assert time.monotonic() - started < 0.1
    assert handoff.health()["pending_bytes"] == first.canonical_size_bytes
    sink.release.set()

    assert handoff.wait_until_idle(2.0)
    health = handoff.close()
    assert health["pending_bytes"] == 0
    assert health["peak_pending_bytes"] == first.canonical_size_bytes
    assert health["byte_overflow_lost"] == 1
    assert health["oversized_envelope_lost"] == 0
    assert [(gap.reason, gap.lost_count) for gap in sink.gaps] == [
        ("iqfeed_l1_capture_queue_byte_overflow", 1)
    ]


def test_sink_submit_failure_is_not_silent_and_does_not_kill_later_capture():
    class _FailOnceSink(_Sink):
        def __init__(self):
            super().__init__()
            self.failed = False

        def submit_envelope(self, envelope):
            if not self.failed:
                self.failed = True
                raise RuntimeError("fixture sink failure")
            super().submit_envelope(envelope)

    sink = _FailOnceSink()
    handoff = _handoff(sink)
    handoff.start()
    assert handoff.offer(_envelope(sequence=1))
    assert handoff.offer(_envelope(sequence=2))

    assert handoff.wait_until_idle(2.0)
    health = handoff.close()
    assert health["submit_failures"] == 1
    assert health["submitted"] == 1
    assert [(gap.reason, gap.lost_count) for gap in sink.gaps] == [
        ("iqfeed_l1_capture_sink_submit_failed", 1)
    ]


def test_network_permitting_sink_is_rejected_before_worker_start():
    sink = _Sink()
    sink.network_fallback_allowed = True
    with pytest.raises(CaptureContractError, match="permits network fallback"):
        _handoff(sink)


def test_handoff_limits_cannot_exceed_measured_sink_resource_binding():
    sink = _Sink()
    with pytest.raises(CaptureContractError, match="queue exceeds"):
        _handoff(sink, max_pending_events=33)
    with pytest.raises(CaptureContractError, match="gap ledger exceeds"):
        _handoff(sink, max_gap_keys=33)
    with pytest.raises(CaptureContractError, match="queue bytes exceed"):
        _handoff(sink, max_pending_bytes=2_000_001)
    with pytest.raises(CaptureContractError, match="positive integer"):
        _handoff(sink, max_pending_bytes=1000.0)


def test_foreign_handoff_configuration_is_rejected_with_explicit_gap():
    sink = _Sink()
    handoff = _handoff(sink, max_pending_events=1)
    handoff.start()

    # Default fixture envelope is bound to the 8-event handoff, not this one.
    assert handoff.offer(_envelope(sequence=1)) is False
    assert handoff.wait_until_idle(2.0)
    health = handoff.close()

    assert health["submitted"] == 0
    assert [(gap.reason, gap.lost_count) for gap in sink.gaps] == [
        ("iqfeed_l1_capture_envelope_binding_mismatch", 1)
    ]


def test_malformed_release_row_becomes_gap_instead_of_escaping_writer_path():
    sink = _Sink()
    handoff = _handoff(sink)
    handoff.start()
    malformed = _row()
    malformed["source_frame_sequence"] = "not-an-integer"

    accepted, rejected = handoff.offer_released_rows(
        trade_rows=[],
        quote_rows=[malformed],
        available_at=BASE + timedelta(seconds=1),
    )

    assert (accepted, rejected) == (0, 1)
    assert handoff.wait_until_idle(2.0)
    health = handoff.close()
    assert health["submitted"] == 0
    assert [(gap.reason, gap.lost_count) for gap in sink.gaps] == [
        ("iqfeed_l1_capture_envelope_invalid", 1)
    ]
