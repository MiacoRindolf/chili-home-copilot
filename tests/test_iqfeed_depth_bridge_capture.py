from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading

import pytest

import scripts.iqfeed_depth_bridge as bridge
from app.services.trading.momentum_neural.iqfeed_l2_capture import (
    BoundedIqfeedL2CaptureHandoff,
    IqfeedL2CaptureEnvelope,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureContractError,
    CaptureStream,
    CoverageGap,
)


UTC = timezone.utc
BASE = datetime(2026, 7, 15, 15, 30, tzinfo=UTC)
RESOURCE_BINDING_SHA256 = "b" * 64


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


class _Chunks:
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = list(chunks)

    def recv(self, _size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""


@pytest.fixture(autouse=True)
def _reset_bridge_capture_state():
    with bridge._connection_state_lock:
        bridge._active_connection_generation = 0
        bridge._frame_sequence_by_generation.clear()
    with bridge._capture_handoff_lock:
        bridge._capture_handoff = None
    with bridge.books_lock:
        bridge.books.clear()
        bridge._capture_hot_symbols.clear()
        bridge._capture_checkpointed_generation.clear()
    bridge.running = True
    yield
    with bridge._connection_state_lock:
        bridge._active_connection_generation = 0
        bridge._frame_sequence_by_generation.clear()
    with bridge._capture_handoff_lock:
        bridge._capture_handoff = None
    with bridge.books_lock:
        bridge.books.clear()
        bridge._capture_hot_symbols.clear()
        bridge._capture_checkpointed_generation.clear()
    bridge.running = True


def _handoff(sink: _Sink) -> BoundedIqfeedL2CaptureHandoff:
    return BoundedIqfeedL2CaptureHandoff(
        sink=sink,
        max_pending_events=8,
        max_pending_bytes=1_000_000,
        max_gap_keys=8,
        bridge_source_sha256=bridge.BRIDGE_SOURCE_SHA256,
        bridge_configuration=bridge.BRIDGE_CAPTURE_CONFIGURATION,
        bridge_configuration_sha256=(
            bridge.BRIDGE_CAPTURE_CONFIGURATION_SHA256
        ),
    )


def _seed_book(generation: int) -> None:
    sequence = bridge._next_source_frame_sequence(generation)
    assert bridge.books["VEEE"].update(
        "BATS",
        "B",
        4.11,
        200.0,
        provider_at=BASE + timedelta(milliseconds=700),
        received_at=BASE + timedelta(seconds=1),
        connection_generation=generation,
        source_frame_sequence=sequence,
        source_frame_sha256=f"{sequence:064x}",
        condition_code="4",
    )


def test_provider_clock_parser_uses_exact_type6_et_date_and_time() -> None:
    assert bridge._parse_l2_provider_at(
        "2026-07-15", "11:30:00.123456"
    ) == datetime(2026, 7, 15, 15, 30, 0, 123456, tzinfo=UTC)
    assert bridge._parse_l2_provider_at("bad-date", "11:30:00") is None
    assert bridge._parse_l2_provider_at("2026-07-15", "") is None


def test_book_checkpoint_retains_level_frame_provenance_but_never_completion() -> None:
    generation = bridge._begin_connection_generation()
    try:
        _seed_book(generation)
        checkpoint = bridge.books["VEEE"].capture_checkpoint(
            symbol="VEEE", received_at=BASE + timedelta(seconds=2)
        )
    finally:
        bridge._retire_connection_generation(generation)

    assert checkpoint is not None
    assert checkpoint["initial_snapshot_complete"] is False
    assert (
        checkpoint["completion_basis"]
        == "provider_snapshot_completion_boundary_unavailable"
    )
    assert checkpoint["levels"][0]["connection_generation"] == generation
    assert checkpoint["levels"][0]["provider_at"] == BASE + timedelta(
        milliseconds=700
    )


def test_reader_emits_checkpoint_then_exact_clock_delta_in_source_order() -> None:
    sink = _Sink()
    handoff = _handoff(sink)
    handoff.start()
    bridge.bind_capture_handoff(handoff)
    generation = bridge._begin_connection_generation()
    try:
        with bridge.books_lock:
            _seed_book(generation)
        assert bridge.activate_capture_symbol(
            "VEEE", available_at=BASE + timedelta(seconds=2)
        )
        line = (
            b"6,VEEE,,ARCX,A,4.1200,100,,4,11:30:00.123456,2026-07-15,\r\n"
        )
        bridge.reader(_Chunks(line), threading.Event(), generation)
        assert handoff.wait_until_idle(1)
    finally:
        bridge._retire_connection_generation(generation)
        bridge.unbind_capture_handoff(handoff)
    health = handoff.close()

    assert [envelope.stream for envelope in sink.envelopes] == [
        CaptureStream.L2_DEPTH_CHECKPOINT,
        CaptureStream.L2_DEPTH_DELTA,
    ]
    delta = sink.envelopes[1]
    assert delta.clocks.provider_event_at == datetime(
        2026, 7, 15, 15, 30, 0, 123456, tzinfo=UTC
    )
    assert delta.payload["venue"] == "ARCX"
    assert health["active_generations"] == {"VEEE": generation}


def test_bad_provider_clock_is_explicit_gap_and_fences_capture_book() -> None:
    sink = _Sink()
    handoff = _handoff(sink)
    handoff.start()
    bridge.bind_capture_handoff(handoff)
    generation = bridge._begin_connection_generation()
    try:
        with bridge.books_lock:
            _seed_book(generation)
        assert bridge.activate_capture_symbol(
            "VEEE", available_at=BASE + timedelta(seconds=2)
        )
        line = b"6,VEEE,,ARCX,A,4.1200,100,,4,bad-time,2026-07-15,\r\n"
        bridge.reader(_Chunks(line), threading.Event(), generation)
        assert handoff.wait_until_idle(1)
    finally:
        bridge._retire_connection_generation(generation)
        bridge.unbind_capture_handoff(handoff)
    health = handoff.close()

    assert [row.stream for row in sink.envelopes] == [
        CaptureStream.L2_DEPTH_CHECKPOINT
    ]
    assert {gap.reason for gap in sink.gaps} == {
        "iqfeed_l2_capture_delta_invalid"
    }
    assert health["active_generations"] == {}


def test_binding_and_unbinding_are_prohibited_mid_connection() -> None:
    sink = _Sink()
    handoff = _handoff(sink)
    handoff.start()
    generation = bridge._begin_connection_generation()
    try:
        with pytest.raises(RuntimeError, match="bind mid-connection"):
            bridge.bind_capture_handoff(handoff)
    finally:
        bridge._retire_connection_generation(generation)

    bridge.bind_capture_handoff(handoff)
    generation = bridge._begin_connection_generation()
    try:
        with pytest.raises(RuntimeError, match="unbind mid-connection"):
            bridge.unbind_capture_handoff(handoff)
    finally:
        bridge._retire_connection_generation(generation)
    bridge.unbind_capture_handoff(handoff)
    handoff.close()


def test_bind_check_and_assignment_are_atomic_with_connection_start() -> None:
    health_read = threading.Event()
    result: list[BaseException] = []

    class _StructuralHandoff:
        def health(self):
            health_read.set()
            return {"started": True, "accepting": True}

        def activate_hot_symbol(self, *_args, **_kwargs):
            return False

        def offer_delta_rows(self, *_args, **_kwargs):
            return (0, 0, 0)

        def deactivate_hot_symbol(self, *_args, **_kwargs):
            return False

        def record_connection_boundary(self, *_args, **_kwargs):
            return ()

        def record_release_failure(self, *_args, **_kwargs):
            return 0

    handoff = _StructuralHandoff()

    def bind() -> None:
        try:
            bridge.bind_capture_handoff(handoff)
        except BaseException as exc:  # captured for the assertion thread
            result.append(exc)

    bridge._connection_state_lock.acquire()
    try:
        thread = threading.Thread(target=bind)
        thread.start()
        assert health_read.wait(timeout=1)
        bridge._active_connection_generation = 99
    finally:
        bridge._connection_state_lock.release()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert len(result) == 1
    assert isinstance(result[0], RuntimeError)
    assert "bind mid-connection" in str(result[0])
    assert bridge._capture_handoff is None


def test_book_rejects_cross_generation_or_nonmonotonic_level_reuse() -> None:
    book = bridge.Book()
    assert book.update(
        "ARCX",
        "A",
        4.12,
        100.0,
        provider_at=BASE,
        received_at=BASE + timedelta(milliseconds=1),
        connection_generation=2,
        source_frame_sequence=10,
        source_frame_sha256="1" * 64,
        condition_code="4",
    )
    assert not book.update(
        "ARCX",
        "A",
        4.13,
        100.0,
        provider_at=BASE,
        received_at=BASE + timedelta(milliseconds=2),
        connection_generation=2,
        source_frame_sequence=10,
        source_frame_sha256="2" * 64,
        condition_code="4",
    )
    assert not book.update(
        "ARCX",
        "A",
        4.13,
        100.0,
        provider_at=BASE,
        received_at=BASE + timedelta(milliseconds=2),
        connection_generation=3,
        source_frame_sequence=11,
        source_frame_sha256="3" * 64,
        condition_code="4",
    )


def test_bridge_capture_configuration_is_content_addressed() -> None:
    assert len(bridge.BRIDGE_SOURCE_SHA256) == 64
    assert len(bridge.BRIDGE_CAPTURE_CONFIGURATION_SHA256) == 64
    with pytest.raises(CaptureContractError, match="configuration hash mismatch"):
        BoundedIqfeedL2CaptureHandoff(
            sink=_Sink(),
            max_pending_events=8,
            max_pending_bytes=1_000_000,
            max_gap_keys=8,
            bridge_source_sha256=bridge.BRIDGE_SOURCE_SHA256,
            bridge_configuration=bridge.BRIDGE_CAPTURE_CONFIGURATION,
            bridge_configuration_sha256="f" * 64,
        )


def test_depth_bridge_startup_schema_gate_is_read_only_and_current(db) -> None:
    bridge._verify_depth_schema()
    source = bridge.Path(bridge.__file__).read_text(encoding="utf-8")
    assert "CREATE TABLE" not in source
    assert "ALTER TABLE" not in source
    assert "DDL =" not in source


def test_depth_bridge_schema_failure_precedes_provider_connection(monkeypatch) -> None:
    called = {"provider": False}

    def _reject_schema():
        raise RuntimeError("fixture depth schema drift")

    def _provider(*_args, **_kwargs):
        called["provider"] = True
        raise AssertionError("provider connection happened before schema verification")

    monkeypatch.setattr(bridge, "_verify_depth_schema", _reject_schema)
    monkeypatch.setattr(bridge, "_run_connection", _provider)
    monkeypatch.setattr(bridge.sys, "argv", ["iqfeed_depth_bridge.py"])

    with pytest.raises(RuntimeError, match="fixture depth schema drift"):
        bridge.main()
    assert called["provider"] is False


def test_depth_bridge_unbound_capture_fails_before_provider_connection(
    monkeypatch,
) -> None:
    called = {"provider": False}
    with bridge._capture_handoff_lock:
        assert bridge._capture_handoff is None
    monkeypatch.setattr(bridge, "_verify_depth_schema", lambda: None)

    def _provider(*_args, **_kwargs):
        called["provider"] = True
        raise AssertionError("unbound depth bridge reached provider connection")

    monkeypatch.setattr(bridge, "_run_connection", _provider)
    monkeypatch.setattr(bridge.sys, "argv", ["iqfeed_depth_bridge.py"])
    with pytest.raises(RuntimeError, match="must be bound before provider connection"):
        bridge.main()
    assert called["provider"] is False


def test_depth_bridge_unbound_hot_loss_is_explicit_only_in_diagnostic_mode(
    monkeypatch, caplog
) -> None:
    at = BASE + timedelta(seconds=1)
    row = {"sym": "VEEE", "connection_generation": 3}
    with bridge._capture_handoff_lock:
        assert bridge._capture_handoff is None
    with bridge.books_lock:
        bridge._capture_hot_symbols.add("VEEE")
    try:
        monkeypatch.setattr(bridge.sys, "argv", ["iqfeed_depth_bridge.py"])
        with pytest.raises(RuntimeError, match="refusing silent delta loss"):
            bridge._publish_capture_delta_locked(
                row,
                available_at=at,
                allow_recheckpoint=False,
            )
        with pytest.raises(RuntimeError, match="refusing silent connection boundary"):
            bridge._record_capture_connection_boundary(
                at=at,
                connection_generation=3,
                active=True,
            )

        monkeypatch.setattr(
            bridge.sys,
            "argv",
            ["iqfeed_depth_bridge.py", bridge.UNCAPTURED_DIAGNOSTIC_FLAG],
        )
        assert bridge._publish_capture_delta_locked(
            row,
            available_at=at,
            allow_recheckpoint=False,
        ) == (0, 1, 0)
        bridge._record_capture_connection_boundary(
            at=at,
            connection_generation=3,
            active=True,
        )
        assert "iqfeed_l2_delta_unbound_diagnostic" in caplog.text
        assert "iqfeed_l2_connection_boundary_unbound_diagnostic" in caplog.text
    finally:
        with bridge.books_lock:
            bridge._capture_hot_symbols.discard("VEEE")
