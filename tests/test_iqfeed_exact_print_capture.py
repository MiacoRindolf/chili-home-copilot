from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading

import pytest

import scripts.iqfeed_trade_bridge as bridge
from app.services.trading.momentum_neural.iqfeed_l1_capture import (
    BoundedIqfeedL1CaptureHandoff,
    IqfeedL1CaptureEnvelope,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureClocks,
    CaptureContractError,
    CaptureEvent,
    CaptureIqfeedPrint,
    CaptureRunIdentity,
    CaptureStream,
    CoverageGap,
    IQFEED_EXACT_PRINT_SOURCE_PROVENANCE_SCHEMA_VERSION,
    IQFEED_EXACT_PRINT_QUOTE_SOURCE_PROVENANCE_SCHEMA_VERSION,
    IQFEED_L1_SOURCE_PROVENANCE_SCHEMA_VERSION,
    IQFEED_L1_SOURCE_PROVENANCE_FIELD,
    build_provider_registration_evidence_from_source_event,
    validate_iqfeed_l1_source_provenance,
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
        self.envelopes: list[IqfeedL1CaptureEnvelope] = []
        self.gaps: list[CoverageGap] = []

    def submit_envelope(self, envelope: IqfeedL1CaptureEnvelope) -> None:
        self.envelopes.append(envelope)

    def report_gap(self, gap: CoverageGap) -> None:
        self.gaps.append(gap)


class _Chunks:
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = list(chunks)

    def recv(self, _size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""


@pytest.fixture(autouse=True)
def _reset_bridge_state(monkeypatch):
    with bridge._pending_lock:
        bridge._pending.clear()
        bridge._pending_nbbo.clear()
    bridge._last_trade.clear()
    bridge._last_nbbo_append_monotonic = None
    with bridge._connection_state_lock:
        bridge._active_connection_generation = 0
        bridge._frame_sequence_by_generation.clear()
        bridge._protocol_acknowledged_generations.clear()
        bridge._selected_fields_ack_sha256_by_generation.clear()
    with bridge._capture_handoff_lock:
        bridge._capture_handoff = None
    monkeypatch.setattr(
        bridge,
        "BRIDGE_RUN_ID",
        "12553525-2da8-4b22-a69f-d3034871e90c",
    )
    yield
    with bridge._capture_handoff_lock:
        bridge._capture_handoff = None
    with bridge._connection_state_lock:
        bridge._active_connection_generation = 0
        bridge._frame_sequence_by_generation.clear()
        bridge._protocol_acknowledged_generations.clear()
        bridge._selected_fields_ack_sha256_by_generation.clear()


def _frame(
    *,
    symbol: str = "VEEE",
    price: str = "4.12",
    size: str = "100",
    trade_time: str = "11:30:00.123456",
    trade_date: str = "2026-07-15",
    market_center: str = "Q",
    conditions: str = "",
    tick_id: str = "123456",
    bid: str = "4.11",
    ask: str = "4.12",
    bid_size: str = "200",
    ask_size: str = "300",
    bid_time: str = "11:30:00.123455",
    ask_time: str = "11:30:00.123456",
    delay: str = "0",
    message_contents: str = "C",
) -> str:
    values = {
        "Symbol": symbol,
        "Most Recent Trade": price,
        "Most Recent Trade Size": size,
        "Most Recent Trade Time": trade_time,
        "Most Recent Trade Date": trade_date,
        "Most Recent Trade Market Center": market_center,
        "Most Recent Trade Conditions": conditions,
        "TickID": tick_id,
        "Bid": bid,
        "Bid Size": bid_size,
        "Bid Time": bid_time,
        "Ask": ask,
        "Ask Size": ask_size,
        "Ask Time": ask_time,
        "Total Volume": "100000",
        "Delay": delay,
        "Message Contents": message_contents,
        "Decimal Precision": "4",
    }
    return "Q," + ",".join(values[field] for field in bridge.SELECTED_UPDATE_FIELDS)


def _ack_line() -> str:
    return "S,CURRENT UPDATE FIELDNAMES," + ",".join(
        bridge.SELECTED_UPDATE_FIELDS
    ) + ","


def _activate_with_ack(generation: int = 7) -> str:
    bridge._activate_connection_generation(generation)
    line = _ack_line()
    ack_sha256 = hashlib.sha256(line.encode("utf-8")).hexdigest()
    assert bridge._observe_selected_update_fields_ack(
        line,
        connection_generation=generation,
        source_frame_sha256=ack_sha256,
    )
    return ack_sha256


def _parse(
    line: str,
    *,
    generation: int = 7,
    received_at: datetime | None = None,
) -> tuple[bool, bool]:
    ack = bridge._selected_fields_ack_sha256(generation)
    assert ack is not None
    return bridge._parse_selected_l1(
        line,
        connection_generation=generation,
        selected_fields_ack_sha256=ack,
        received_at=received_at or BASE + timedelta(milliseconds=250),
        source_frame_sha256=hashlib.sha256(line.encode("utf-8")).hexdigest(),
    )


def _handoff(sink: _Sink) -> BoundedIqfeedL1CaptureHandoff:
    return BoundedIqfeedL1CaptureHandoff(
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


def test_exact_selected_print_uses_provider_date_timems_and_tick_identity() -> None:
    ack = _activate_with_ack()

    assert _parse(_frame()) == (True, True)
    assert len(bridge._pending) == 1
    assert len(bridge._pending_nbbo) == 1
    row = bridge._pending[0]
    expected = datetime(2026, 7, 15, 15, 30, 0, 123456, tzinfo=UTC)
    assert row["provider_at"] == expected
    assert row["provider_trade_reference_at"] == expected
    assert row["basis"] == bridge.EXACT_PRINT_TIMESTAMP_BASIS
    assert row["provider_tick_id"] == "123456"
    assert row["selected_update_fields_ack_sha256"] == ack
    assert row["selected_update_fields"] == list(bridge.SELECTED_UPDATE_FIELDS)


def test_exact_selected_print_accepts_provider_us_slash_date_without_inference() -> None:
    _activate_with_ack()

    assert _parse(_frame(trade_date="07/15/2026")) == (True, True)
    assert bridge._pending[0]["provider_at"] == datetime(
        2026, 7, 15, 15, 30, 0, 123456, tzinfo=UTC
    )
    assert bridge._pending[0]["provider_trade_date"] == "07/15/2026"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0), ("15", 15), (" 15 ", 15), ("000", 0),
        ("", None), (" ", None), ("bad", None), ("-1", None),
        ("15.0", None), ("NaN", None), ("Infinity", None), ("+15", None),
        ("١٥", None), ("2147483647", 2147483647),
        ("2147483648", None), ("9" * 5000, None),
    ],
    ids=["zero", "delayed", "whitespace", "leading_zero", "blank", "space",
         "malformed", "negative", "fraction", "nan", "infinity", "plus",
         "non_ascii", "integer_max", "overflow", "excessive_digits"],
)
def test_provider_delay_is_nullable_metadata_without_discarding_print(raw, expected):
    _activate_with_ack()
    assert _parse(_frame(delay=raw)) == (True, True)
    row = bridge._pending[0]
    assert row["provider_delay_minutes"] == expected
    assert row["provider_tick_id"] == "123456"
    assert "provider_delay_minutes" not in bridge._pending_nbbo[0]
    assert bridge.BRIDGE_CAPTURE_CONFIGURATION["provider_delay"] == {
        "field": "Delay", "column": "provider_delay_minutes",
        "unit": "minutes", "blank_or_invalid": "null",
    }


def test_delay_changes_do_not_duplicate_prints_or_fill_unknown_metadata():
    _activate_with_ack()
    assert _parse(_frame(delay="15")) == (True, True)
    assert _parse(_frame(delay="0")) == (True, True)
    assert len(bridge._pending) == 1
    assert bridge._pending[0]["provider_delay_minutes"] == 15
    assert _parse(_frame(tick_id="123457", delay="")) == (True, True)
    assert len(bridge._pending) == 2
    assert bridge._pending[1]["provider_delay_minutes"] is None


def test_positive_provider_delay_is_kept_with_original_delayed_trade_clock():
    _activate_with_ack()
    assert _parse(_frame(trade_time="11:15:00.000000", delay="15"))[0] is True
    row = bridge._pending[0]
    assert row["provider_delay_minutes"] == 15
    assert row["provider_at"] == BASE - timedelta(minutes=15)
    assert row["at"] == row["provider_at"].replace(tzinfo=None)


def test_truncated_selected_frame_never_treats_missing_delay_as_authority():
    _activate_with_ack()
    frame = ",".join(_frame().split(",")[:bridge._SELECTED_FIELD_INDEX["Delay"]])
    assert _parse(frame) == (False, False)
    assert not bridge._pending
    assert not bridge._pending_nbbo


def test_duplicate_tick_is_not_reemitted_but_new_tick_same_microsecond_is() -> None:
    _activate_with_ack()
    assert _parse(_frame()) == (True, True)
    assert _parse(_frame(bid="4.10")) == (True, True)
    assert len(bridge._pending) == 1
    assert len(bridge._pending_nbbo) == 2

    assert _parse(_frame(tick_id="123457")) == (True, True)
    assert len(bridge._pending) == 2


@pytest.mark.parametrize(
    "frame",
    [
        _frame(trade_date=""),
        _frame(trade_time=""),
        _frame(tick_id=""),
        _frame(market_center=""),
    ],
)
def test_missing_exact_print_authority_fails_closed(frame: str) -> None:
    _activate_with_ack()
    print_valid, _quote_captured = _parse(frame)
    assert print_valid is False
    assert not bridge._pending


def test_stale_exact_print_is_preserved_with_late_availability_but_not_nbbo() -> None:
    _activate_with_ack()
    assert _parse(
        # The quote-own-clock path legitimately captures a fresh quote beside
        # a stale trade. This fixture must make BOTH clocks stale to reject it.
        _frame(trade_time="11:29:30.000000", bid_time="11:29:30.000000",
               ask_time="11:29:30.000000"),
        received_at=BASE,
    ) == (True, False)
    assert len(bridge._pending) == 1
    assert not bridge._pending_nbbo
    assert (
        bridge._pending[0]["received_at"]
        - bridge._pending[0]["provider_at"]
    ).total_seconds() == 30


def test_provider_clock_too_far_in_future_is_rejected() -> None:
    _activate_with_ack()
    assert _parse(
        _frame(trade_time="11:30:02.000000"),
        received_at=BASE,
    ) == (False, False)
    assert not bridge._pending
    assert not bridge._pending_nbbo


def test_ack_must_match_exact_ordered_field_roster() -> None:
    bridge._activate_connection_generation(4)
    forged = "S,CURRENT UPDATE FIELDNAMES,Symbol,Most Recent Trade,"
    assert not bridge._observe_selected_update_fields_ack(
        forged,
        connection_generation=4,
        source_frame_sha256=hashlib.sha256(forged.encode()).hexdigest(),
    )
    assert bridge._selected_fields_ack_sha256(4) is None


def test_ack_with_interior_empty_field_cannot_rebind_the_roster() -> None:
    bridge._activate_connection_generation(4)
    fields = list(bridge.SELECTED_UPDATE_FIELDS)
    fields.insert(3, "")
    forged = "S,CURRENT UPDATE FIELDNAMES," + ",".join(fields) + ","

    assert not bridge._observe_selected_update_fields_ack(
        forged,
        connection_generation=4,
        source_frame_sha256=hashlib.sha256(forged.encode()).hexdigest(),
    )
    assert bridge._selected_fields_ack_sha256(4) is None


def test_exact_ack_bytes_must_match_the_declared_frame_hash() -> None:
    bridge._activate_connection_generation(4)
    line = _ack_line()

    assert not bridge._observe_selected_update_fields_ack(
        line,
        connection_generation=4,
        source_frame_sha256="0" * 64,
        source_frame_bytes=line.encode(),
    )
    assert bridge._selected_fields_ack_sha256(4) is None


@pytest.mark.parametrize("mismatch", ["forged_hash", "different_bytes"])
def test_exact_q_bytes_must_match_the_declared_frame_hash(mismatch: str) -> None:
    ack = _activate_with_ack()
    line = _frame()
    if mismatch == "forged_hash":
        source_bytes = line.encode()
        source_sha256 = "0" * 64
    else:
        source_bytes = (line + " ").encode()
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()

    assert bridge._parse_selected_l1(
        line,
        connection_generation=7,
        selected_fields_ack_sha256=ack,
        received_at=BASE + timedelta(milliseconds=250),
        source_frame_sha256=source_sha256,
        source_frame_bytes=source_bytes,
    ) == (False, False)
    assert not bridge._pending
    assert not bridge._pending_nbbo


def test_exact_print_envelope_is_hash_bound_and_replay_typed() -> None:
    _activate_with_ack()
    assert _parse(_frame()) == (True, True)
    sink = _Sink()
    handoff = _handoff(sink)
    row = bridge._pending[0]
    envelope = IqfeedL1CaptureEnvelope.from_released_row(
        row,
        stream=CaptureStream.IQFEED_PRINT,
        available_at=row["received_at"] + timedelta(milliseconds=10),
        bridge_source_sha256=bridge.BRIDGE_SOURCE_SHA256,
        bridge_configuration=bridge.BRIDGE_CAPTURE_CONFIGURATION,
        bridge_configuration_sha256=(
            bridge.BRIDGE_CAPTURE_CONFIGURATION_SHA256
        ),
        capture_resource_binding_sha256=RESOURCE_BINDING_SHA256,
        handoff_configuration=handoff.handoff_configuration,
        handoff_configuration_sha256=handoff.handoff_configuration_sha256,
    )
    provenance = envelope.payload[IQFEED_L1_SOURCE_PROVENANCE_FIELD]
    assert provenance["schema_version"] == (
        IQFEED_EXACT_PRINT_QUOTE_SOURCE_PROVENANCE_SCHEMA_VERSION
    )
    assert envelope.clocks.provider_event_at == row["provider_at"]
    assert envelope.clocks.market_reference_at is None

    event = CaptureEvent(
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
        sequence=1,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol="VEEE",
        clocks=envelope.clocks,
        payload=envelope.payload,
    )
    parsed = CaptureIqfeedPrint.from_event(event)
    assert parsed.price == 4.12
    assert parsed.event.clocks.provider_event_at == row["provider_at"]
    registration = build_provider_registration_evidence_from_source_event(
        event,
        producer_id="iqfeed_l1",
    )
    assert registration.provider_instance_id == row["bridge_run_id"]
    assert registration.provider_generation == row["connection_generation"]
    assert registration.provider_sequence == row["source_frame_sequence"]
    assert registration.source_payload_sha256 == event.payload_sha256
    assert registration.subscription_request_sha256 == (
        row["selected_update_fields_ack_sha256"]
    )

    promoted_at = envelope.clocks.available_at + timedelta(seconds=1)
    promotion_id = "9b9677a6-056b-4104-821e-aaf0a0cf25b9"
    promoted = CaptureEvent(
        identity=event.identity,
        sequence=2,
        stream=event.stream,
        provider=event.provider,
        symbol=event.symbol,
        clocks=CaptureClocks(
            provider_event_at=event.clocks.provider_event_at,
            received_at=event.clocks.received_at,
            available_at=promoted_at,
        ),
        payload={
            **dict(event.payload),
            "_capture_promotion": {
                "promotion_id": promotion_id,
                "promoted_at": promoted_at.isoformat().replace("+00:00", "Z"),
                "promotion_order": 1,
                "original_provisional_available_at": (
                    event.clocks.available_at.isoformat().replace("+00:00", "Z")
                ),
                "provisional_event_sha256": event.event_sha256,
                "source_identity_sha256": event.identity.identity_sha256,
                "inventory_sha256": "c" * 64,
            },
            "_capture_release": {
                "original_available_at": (
                    event.clocks.available_at.isoformat().replace("+00:00", "Z")
                ),
                "released_available_at": promoted_at.isoformat().replace(
                    "+00:00", "Z"
                ),
                "release_kind": "hot_symbol_promotion",
                "promotion_id": promotion_id,
                "promoted_at": promoted_at.isoformat().replace("+00:00", "Z"),
                "source_identity_sha256": event.identity.identity_sha256,
                "resource_binding_sha256": RESOURCE_BINDING_SHA256,
                "inventory_sha256": "c" * 64,
            },
        },
    )
    promoted_print = CaptureIqfeedPrint.from_event(promoted)
    assert promoted_print.price == parsed.price
    assert promoted_print.event.event_sha256 == promoted.event_sha256


def test_reader_without_ack_records_both_stream_gaps_and_no_rows() -> None:
    sink = _Sink()
    handoff = _handoff(sink)
    handoff.start()
    bridge.bind_capture_handoff(handoff)
    bridge._activate_connection_generation(9)
    try:
        bridge.reader(
            _Chunks((_frame() + "\r\n").encode()),
            threading.Event(),
            9,
        )
        assert handoff.wait_until_idle(1)
    finally:
        bridge._retire_connection_generation(9)
        bridge.unbind_capture_handoff(handoff)
    handoff.close()

    assert not bridge._pending
    assert not bridge._pending_nbbo
    assert {(gap.stream, gap.reason) for gap in sink.gaps} == {
        (CaptureStream.IQFEED_PRINT, "iqfeed_selected_fields_unconfirmed"),
        (CaptureStream.NBBO_QUOTE, "iqfeed_selected_fields_unconfirmed"),
    }


def test_reader_ack_then_q_emits_exact_rows() -> None:
    generation = 10
    bridge._activate_connection_generation(generation)
    now_et = datetime.now(UTC).astimezone(bridge._ET)
    trade_date = now_et.date().isoformat()
    trade_time = now_et.strftime("%H:%M:%S.%f")
    ack = (_ack_line() + "\r\n").encode()
    frame = (
        _frame(trade_date=trade_date, trade_time=trade_time) + "\r\n"
    ).encode()
    bridge.reader(_Chunks(ack + frame), threading.Event(), generation)

    assert len(bridge._pending) == 1
    assert bridge._pending[0]["provider_at"] == bridge._exact_trade_datetime_utc(
        trade_date, trade_time
    )
    assert len(bridge._pending_nbbo) == 1


def test_selected_field_command_is_explicit_and_content_addressed() -> None:
    # Symbol must NOT be requested: the live feed auto-prepends it and rejects
    # (silently ignores) any select that names it - verified live 2026-07-16.
    assert bridge.SELECT_UPDATE_FIELDS_COMMAND.startswith(
        "S,SELECT UPDATE FIELDS,Most Recent Trade,"
    )
    assert ",Symbol," not in bridge.SELECT_UPDATE_FIELDS_COMMAND
    assert "Most Recent Trade Date" in bridge.SELECT_UPDATE_FIELDS_COMMAND
    assert "Most Recent Trade Time" in bridge.SELECT_UPDATE_FIELDS_COMMAND
    assert "TickID" in bridge.SELECT_UPDATE_FIELDS_COMMAND
    assert len(bridge.SELECTED_UPDATE_FIELDS_SHA256) == 64


@pytest.mark.parametrize("raw", ["", " 00200 ", "0", "-1", "NaN", "bad"])
def test_raw_quote_context_is_evidence_even_when_trade_fence_passes(raw):
    _activate_with_ack()
    assert _parse(_frame(bid_size=raw, ask_size=raw, bid_time=raw, ask_time=raw)) == (True, True)
    fields = ("provider_bid_size_raw", "provider_ask_size_raw",
              "provider_bid_time_raw", "provider_ask_time_raw")
    for row in (bridge._pending[0], bridge._pending_nbbo[0]):
        assert tuple(row[f] for f in fields) == (raw,) * 4
    quote = bridge._pending_nbbo[0]
    assert quote["provider_at"] is None  # A trade fence does not date each quote side.
    assert quote["basis"] == bridge.AUTHORITATIVE_TIMESTAMP_BASIS
    notification = json.loads(bridge._notify_payload(quote))
    assert notification["source_frame_sha256"] == quote["source_frame_sha256"]
    assert all(f not in notification for f in fields)


def test_quote_only_update_retains_its_own_raw_sides_without_duplicating_trade():
    _activate_with_ack()
    assert _parse(_frame(bid_size="200")) == (True, True)
    assert _parse(_frame(bid_size="400", ask_time="11:30:00.200000")) == (True, True)
    assert len(bridge._pending) == 1
    assert len(bridge._pending_nbbo) == 2
    first, second = bridge._pending_nbbo
    assert bridge._pending[0]["provider_bid_size_raw"] == "200"
    assert second["provider_bid_size_raw"] == "400"
    assert second["provider_ask_time_raw"] == "11:30:00.200000"
    assert first["source_frame_sha256"] != second["source_frame_sha256"]
    assert first["source_frame_sequence"] < second["source_frame_sequence"]


def test_own_clock_branch_preserves_both_side_times_and_original_trade_clock():
    _activate_with_ack()
    assert _parse(_frame(trade_time="11:15:00.000000")) == (True, True)
    quote = bridge._pending_nbbo[0]
    assert quote["basis"] == bridge.QUOTE_EVENT_TIMESTAMP_BASIS
    assert quote["provider_bid_time_raw"] == "11:30:00.123455"
    assert quote["provider_ask_time_raw"] == "11:30:00.123456"
    assert quote["provider_trade_reference_at"] == BASE - timedelta(minutes=15)


@pytest.mark.parametrize("stream", [CaptureStream.IQFEED_PRINT, CaptureStream.NBBO_QUOTE])
def test_capture_preserves_raw_quote_context_without_promoting_a_side_clock(stream):
    _activate_with_ack()
    assert _parse(_frame(bid_size=" 00200 ", ask_time="")) == (True, True)
    row = (bridge._pending if stream is CaptureStream.IQFEED_PRINT else bridge._pending_nbbo)[0]
    handoff = _handoff(_Sink())
    envelope = IqfeedL1CaptureEnvelope.from_released_row(
        row, stream=stream,
        available_at=row["received_at"] + timedelta(milliseconds=10),
        bridge_source_sha256=bridge.BRIDGE_SOURCE_SHA256,
        bridge_configuration=bridge.BRIDGE_CAPTURE_CONFIGURATION,
        bridge_configuration_sha256=bridge.BRIDGE_CAPTURE_CONFIGURATION_SHA256,
        capture_resource_binding_sha256=RESOURCE_BINDING_SHA256,
        handoff_configuration=handoff.handoff_configuration,
        handoff_configuration_sha256=handoff.handoff_configuration_sha256,
    )
    provenance = envelope.payload[IQFEED_L1_SOURCE_PROVENANCE_FIELD]
    assert provenance["provider_bid_size_raw"] == " 00200 "
    assert provenance["provider_ask_time_raw"] == ""
    assert provenance["provider_bid_time_raw"] == "11:30:00.123455"
    assert envelope.clocks.provider_event_at == row["provider_at"]

    # Legacy V1 remains readable without backfilling side evidence. V2 must
    # preserve the complete typed extension and cannot smuggle it into V1.
    raw_fields = ("provider_bid_size_raw", "provider_ask_size_raw",
                  "provider_bid_time_raw", "provider_ask_time_raw")
    legacy = dict(provenance)
    for field in raw_fields:
        legacy.pop(field)
    legacy["schema_version"] = (
        IQFEED_EXACT_PRINT_SOURCE_PROVENANCE_SCHEMA_VERSION
        if stream is CaptureStream.IQFEED_PRINT else IQFEED_L1_SOURCE_PROVENANCE_SCHEMA_VERSION
    )
    assert validate_iqfeed_l1_source_provenance(
        legacy, symbol=row["sym"], clocks=envelope.clocks,
    ) == legacy
    for field in raw_fields:
        missing = dict(provenance)
        missing.pop(field)
        with pytest.raises(CaptureContractError, match="raw quote field"):
            validate_iqfeed_l1_source_provenance(missing, symbol=row["sym"], clocks=envelope.clocks)
        for invalid in (200, True, {}, []):
            malformed = {**provenance, field: invalid}
            with pytest.raises(CaptureContractError, match="raw quote field"):
                validate_iqfeed_l1_source_provenance(malformed, symbol=row["sym"], clocks=envelope.clocks)
        with pytest.raises(CaptureContractError, match="fields do not match schema"):
            validate_iqfeed_l1_source_provenance(
                {**legacy, field: "200"}, symbol=row["sym"], clocks=envelope.clocks,
            )


def test_oversized_raw_metadata_cannot_break_notify_but_remains_in_the_row():
    _activate_with_ack()
    assert _parse(_frame(bid_size="9" * 9000)) == (True, True)
    row = bridge._pending_nbbo[0]
    payload = bridge._notify_payload(row)
    assert len(payload.encode("utf-8")) < 8000
    envelope = json.loads(payload)
    assert "provider_bid_size_raw" not in envelope
    assert envelope["source_frame_sha256"] == row["source_frame_sha256"]
    assert len(row["provider_bid_size_raw"]) == 9000
