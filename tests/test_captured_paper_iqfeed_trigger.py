from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from typing import Any
import uuid

import pytest

from app.services.trading.momentum_neural.captured_paper_iqfeed_trigger import (
    CapturedPaperIqfeedTriggerResolver,
    IqfeedTriggerResolution,
    IqfeedTriggerStatus,
    parse_captured_paper_iqfeed_q_notify,
)
from app.services.trading.momentum_neural.live_replay_capture import (
    CapturedReadResult,
    CaptureSubmission,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureClocks,
    CaptureContractError,
    CaptureEvent,
    CaptureEventRef,
    CaptureMicrostructureReadQuery,
    CaptureReadReceipt,
    CaptureRunIdentity,
    CaptureStream,
    IQFEED_EXACT_PRINT_SOURCE_PROVENANCE_SCHEMA_VERSION,
    IQFEED_L1_SOURCE_PROVENANCE_FIELD,
    IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
    captured_read_result_sha256,
    sha256_json,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 16, 16, 30, 0, 500_000, tzinfo=UTC)
REFERENCE_AT = NOW - timedelta(milliseconds=300)
RECEIVED_AT = NOW - timedelta(milliseconds=200)
AVAILABLE_AT = NOW - timedelta(milliseconds=100)
BRIDGE_VERSION = "iqfeed-l1-exact-print-provenance-v3+sha256:0123456789abcdef"
BRIDGE_RUN_ID = "8da0a1ed-24f3-4545-8a7a-6f582ff1acc2"
FRAME_SHA256 = sha256_json({"raw_iqfeed_frame": "Q,TEST,..."})
DECISION_ID = "captured-paper-initial-admission:TEST:20260716T163000Z"
SELECTED_FIELDS = [
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


def _hash(label: str) -> str:
    return sha256_json({"fixture": label})


IDENTITY = CaptureRunIdentity(
    run_id="3ee3ebf6-1620-4af1-80d7-0418de6a9bd6",
    generation=7,
    code_build_sha256=_hash("code"),
    config_sha256=_hash("config"),
    feature_flags_sha256=_hash("flags"),
    account_identity_sha256=_hash("alpaca-paper-account"),
    broker="alpaca",
    broker_environment="paper",
)


def _notify(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "symbol": "TEST",
        "observed_at": REFERENCE_AT.isoformat(),
        "bid": 4.19,
        "ask": 4.21,
        "received_at": RECEIVED_AT.isoformat(),
        "provider_event_at": None,
        "provider_trade_reference_at": REFERENCE_AT.isoformat(),
        "timestamp_basis": "iqfeed_q_receive_trade_reference_fenced",
        "source": "iqfeed_l1",
        "bridge_version": BRIDGE_VERSION,
        "message_type": "Q",
        "bridge_run_id": BRIDGE_RUN_ID,
        "connection_generation": 3,
        "source_frame_sequence": 41,
        "source_frame_sha256": FRAME_SHA256,
        "available_at": AVAILABLE_AT.isoformat(),
    }
    payload.update(overrides)
    return payload


def _source_event(
    *,
    sequence: int = 4,
    frame_sha256: str = FRAME_SHA256,
    frame_sequence: int = 41,
    bridge_run_id: str = BRIDGE_RUN_ID,
    generation: int = 3,
    symbol: str = "TEST",
    price: float = 4.20,
) -> CaptureEvent:
    bridge_configuration = {
        "selected_fields_required": True,
        "socket": "level1",
    }
    handoff_configuration = {
        "bounded_queue": True,
        "capture_stream": "iqfeed_print",
    }
    provenance = {
        "schema_version": (
            IQFEED_EXACT_PRINT_SOURCE_PROVENANCE_SCHEMA_VERSION
        ),
        "symbol": symbol,
        "bridge_run_id": bridge_run_id,
        "connection_generation": generation,
        "bridge_version": BRIDGE_VERSION,
        "bridge_source_sha256": _hash("bridge-source"),
        "bridge_configuration": bridge_configuration,
        "bridge_configuration_sha256": sha256_json(bridge_configuration),
        "capture_resource_binding_sha256": _hash("resource-binding"),
        "handoff_configuration": handoff_configuration,
        "handoff_configuration_sha256": sha256_json(handoff_configuration),
        "message_type": "Q",
        "timestamp_basis": "iqfeed_selected_trade_date_timems_exact",
        "provider_event_at": REFERENCE_AT.isoformat().replace("+00:00", "Z"),
        "received_at": RECEIVED_AT.isoformat().replace("+00:00", "Z"),
        "provider_trade_date": "2026-07-16",
        "provider_trade_time": "16:30:00.200",
        "provider_tick_id": "901234",
        "trade_market_center": "25",
        "trade_conditions": ["@"],
        "message_contents": "Cba",
        "selected_update_fields": SELECTED_FIELDS,
        "selected_update_fields_sha256": sha256_json(SELECTED_FIELDS),
        "selected_update_fields_ack_sha256": _hash("selected-fields-ack"),
        "source_frame_sequence": frame_sequence,
        "source_frame_sha256": frame_sha256,
    }
    return CaptureEvent(
        identity=IDENTITY,
        sequence=sequence,
        stream=CaptureStream.IQFEED_PRINT,
        provider="iqfeed",
        symbol=symbol,
        clocks=CaptureClocks(
            provider_event_at=REFERENCE_AT,
            market_reference_at=None,
            received_at=RECEIVED_AT,
            available_at=AVAILABLE_AT,
        ),
        payload={
            "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
            "symbol": symbol,
            "price": price,
            "size": 100.0,
            "bid": 4.19,
            "ask": 4.21,
            "conditions": ["@"],
            IQFEED_L1_SOURCE_PROVENANCE_FIELD: provenance,
        },
    )


class _CapturePort:
    def __init__(
        self,
        outcomes: list[str],
        *,
        network_fallback_allowed: bool = False,
        tampered_frame: str | None = None,
    ) -> None:
        self._network_fallback_allowed = network_fallback_allowed
        self.outcomes = list(outcomes)
        self.tampered_frame = tampered_frame
        self.calls: list[dict[str, Any]] = []
        self.network_calls = 0
        self.database_calls = 0
        self.current_state_calls = 0

    @property
    def network_fallback_allowed(self) -> bool:
        return self._network_fallback_allowed

    def fetch_provider(self) -> None:  # pragma: no cover - forbidden fallback
        self.network_calls += 1
        raise AssertionError("provider fallback was called")

    def read_database(self) -> None:  # pragma: no cover - forbidden fallback
        self.database_calls += 1
        raise AssertionError("database fallback was called")

    def read_current_state(self) -> None:  # pragma: no cover - forbidden fallback
        self.current_state_calls += 1
        raise AssertionError("current-state fallback was called")

    def capture_complete_microstructure_window(
        self, **kwargs: Any
    ) -> CapturedReadResult:
        self.calls.append(dict(kwargs))
        outcome = self.outcomes.pop(0) if self.outcomes else "empty"
        if outcome == "gap":
            return CapturedReadResult(
                receipt=None,
                source_events=(),
                receipt_submission=None,
                coverage_gap_recorded=True,
            )
        if outcome == "valid":
            sources = (
                _source_event(
                    frame_sha256=self.tampered_frame or FRAME_SHA256
                ),
            )
        elif outcome == "duplicate":
            sources = (
                _source_event(sequence=4),
                _source_event(sequence=5, price=4.205),
            )
        elif outcome == "quote_only":
            sources = ()
        else:
            sources = ()
        query = CaptureMicrostructureReadQuery(
            operation=kwargs["operation"],
            stream=kwargs["stream"],
            symbol=kwargs["symbol"],
            provider=kwargs["provider"],
            event_start_exclusive=kwargs["event_start_exclusive"],
            event_end_inclusive=kwargs["event_end_inclusive"],
            decision_at=kwargs["event_end_inclusive"],
            available_at_most=kwargs["returned_at"],
            source_frontier_sequence=max(
                (source.sequence for source in sources), default=0
            ),
            source_clock_basis="provider_event_at",
            parameters=kwargs["parameters"],
        )
        refs = tuple(CaptureEventRef.from_event(source) for source in sources)
        receipt = CaptureReadReceipt(
            read_id=kwargs["read_id"],
            decision_id=kwargs["decision_id"],
            identity_sha256=IDENTITY.identity_sha256,
            stream=CaptureStream.IQFEED_PRINT,
            provider="iqfeed",
            symbol=kwargs["symbol"],
            requested_at=kwargs["requested_at"],
            returned_at=kwargs["returned_at"],
            query_sha256=sha256_json(query.to_dict()),
            source_event_sha256s=tuple(ref.event_sha256 for ref in refs),
            empty_result=not refs,
            result_sha256=captured_read_result_sha256(refs),
            content_verified=True,
            replay_network_fallback_used=False,
            query=query.to_dict(),
        )
        receipt_event = CaptureEvent(
            identity=IDENTITY,
            sequence=20 + len(self.calls),
            stream=CaptureStream.READ_RECEIPT,
            provider="iqfeed",
            symbol=kwargs["symbol"],
            clocks=CaptureClocks(
                received_at=kwargs["returned_at"],
                available_at=kwargs["returned_at"],
            ),
            payload=receipt.to_dict(),
        )
        submission = CaptureSubmission(
            accepted=True,
            event=receipt_event,
            coverage_gap_recorded=False,
            disposition="durable_receipt_accepted",
        )
        return CapturedReadResult(
            receipt=receipt,
            source_events=sources,
            receipt_submission=submission,
            coverage_gap_recorded=False,
        )


def _resolver(
    capture: _CapturePort,
    *,
    now: datetime = NOW,
    attempts: int = 3,
    waits: list[float] | None = None,
) -> CapturedPaperIqfeedTriggerResolver:
    observed_waits = waits if waits is not None else []
    return CapturedPaperIqfeedTriggerResolver(
        capture=capture,
        expected_bridge_version=BRIDGE_VERSION,
        wall_clock=lambda: now,
        wait=observed_waits.append,
        max_attempts=attempts,
        retry_delay_seconds=0.01,
        max_notify_age_seconds=2.0,
        future_tolerance_seconds=0.25,
    )


def test_exact_durable_print_mints_content_addressed_trigger_receipt() -> None:
    capture = _CapturePort(["valid"])
    result = _resolver(capture).resolve(_notify(), decision_id=DECISION_ID)

    assert result.status is IqfeedTriggerStatus.READY
    assert result.reason == "iqfeed_exact_print_trigger_ready"
    assert result.attempts == 1
    assert result.receipt is not None
    assert isinstance(result.captured_read, CapturedReadResult)
    assert result.captured_read.durable is True
    receipt = result.receipt
    assert result.captured_read.receipt is not None
    assert result.captured_read.receipt.read_id == receipt.captured_read_id
    assert receipt.captured_read_id == capture.calls[0]["read_id"]
    assert receipt.source_event_sha256 == _source_event().event_sha256
    assert receipt.source_frame_sequence == 41
    assert receipt.source_frame_sha256 == FRAME_SHA256
    assert receipt.provider_trade_reference_at == REFERENCE_AT
    assert len(receipt.content_sha256) == 64
    assert capture.calls[0]["stream"] is CaptureStream.IQFEED_PRINT
    assert capture.calls[0]["provider"] == "iqfeed"
    assert capture.calls[0]["event_end_inclusive"] == REFERENCE_AT
    assert capture.calls[0]["returned_at"] == NOW
    assert capture.network_calls == 0
    assert capture.database_calls == 0
    assert capture.current_state_calls == 0


def test_ready_resolution_cannot_drop_or_move_its_process_private_read() -> None:
    result = _resolver(_CapturePort(["valid"])).resolve(
        _notify(), decision_id=DECISION_ID
    )
    assert result.ready is True

    with pytest.raises(CaptureContractError, match="process-private durable read"):
        replace(result, captured_read=None)
    with pytest.raises(
        CaptureContractError,
        match="unavailable IQFeed trigger cannot carry live read authority",
    ):
        IqfeedTriggerResolution(
            status=IqfeedTriggerStatus.COVERAGE_UNAVAILABLE,
            reason="fixture",
            attempts=1,
            receipt=None,
            captured_read=result.captured_read,
        )


def test_public_preparser_rejects_foreign_envelope_before_capture_allocation() -> None:
    parsed = parse_captured_paper_iqfeed_q_notify(
        _notify(), expected_bridge_version=BRIDGE_VERSION
    )
    assert parsed.symbol == "TEST"

    with pytest.raises(CaptureContractError, match="fields_mismatch"):
        parse_captured_paper_iqfeed_q_notify(
            {**_notify(), "unexpected": True},
            expected_bridge_version=BRIDGE_VERSION,
        )


def test_identical_evidence_produces_identical_receipt_content_address() -> None:
    first = _resolver(_CapturePort(["valid"])).resolve(
        json.dumps(_notify(), sort_keys=True),
        decision_id=DECISION_ID,
    )
    second = _resolver(_CapturePort(["valid"])).resolve(
        json.dumps(_notify(), sort_keys=True),
        decision_id=DECISION_ID,
    )

    assert first.receipt is not None and second.receipt is not None
    assert first.notify_sha256 == second.notify_sha256
    assert first.receipt.to_dict() == second.receipt.to_dict()
    assert first.receipt.content_sha256 == second.receipt.content_sha256


def test_notify_precedes_async_sink_and_resolves_on_bounded_retry() -> None:
    waits: list[float] = []
    capture = _CapturePort(["quote_only", "valid"])

    result = _resolver(capture, waits=waits).resolve(
        _notify(), decision_id=DECISION_ID
    )

    assert result.ready is True
    assert result.attempts == 2
    assert len(capture.calls) == 2
    assert capture.calls[0]["read_id"] != capture.calls[1]["read_id"]
    assert waits == [0.01]


@pytest.mark.parametrize("outcome", ["empty", "quote_only", "gap"])
def test_missing_quote_only_or_gap_is_event_local_coverage_unavailable(
    outcome: str,
) -> None:
    waits: list[float] = []
    capture = _CapturePort([outcome, outcome, outcome])

    result = _resolver(capture, waits=waits).resolve(
        _notify(), decision_id=DECISION_ID
    )

    assert result.coverage_unavailable is True
    assert result.receipt is None
    assert result.attempts == 3
    assert len(capture.calls) == 3
    assert waits == [0.01, 0.01]
    assert capture.network_calls == 0
    assert capture.database_calls == 0
    assert capture.current_state_calls == 0


def test_duplicate_exact_prints_for_one_frame_are_ambiguous() -> None:
    capture = _CapturePort(["duplicate"])

    result = _resolver(capture, attempts=1).resolve(
        _notify(), decision_id=DECISION_ID
    )

    assert result.coverage_unavailable is True
    assert result.reason == "iqfeed_exact_print_capture_read_ambiguous"
    assert result.receipt is None


def test_tampered_source_frame_cannot_bind_to_notify() -> None:
    capture = _CapturePort(["valid"], tampered_frame=_hash("other-frame"))

    result = _resolver(capture, attempts=1).resolve(
        _notify(), decision_id=DECISION_ID
    )

    assert result.coverage_unavailable is True
    assert result.reason == "iqfeed_exact_print_provenance_mismatch"
    assert result.receipt is None


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda row: row.update({"unexpected": True}), "iqfeed_notify_fields_mismatch"),
        (lambda row: row.pop("source_frame_sequence"), "iqfeed_notify_fields_mismatch"),
        (
            lambda row: row.update({"bridge_version": BRIDGE_VERSION[:-1] + "0"}),
            "iqfeed_notify_bridge_version_mismatch",
        ),
        (
            lambda row: row.update({"source_frame_sha256": "A" * 64}),
            "iqfeed_notify_source_frame_sha256_invalid",
        ),
        (
            lambda row: row.update({"provider_event_at": REFERENCE_AT.isoformat()}),
            "iqfeed_notify_authority_class_invalid",
        ),
    ],
)
def test_unknown_missing_or_malformed_notify_never_reaches_capture(
    mutation: Any,
    reason: str,
) -> None:
    payload = deepcopy(_notify())
    mutation(payload)
    capture = _CapturePort(["valid"])

    result = _resolver(capture).resolve(payload, decision_id=DECISION_ID)

    assert result.coverage_unavailable is True
    assert result.reason == reason
    assert result.attempts == 0
    assert capture.calls == []


def test_duplicate_json_key_is_rejected_before_capture() -> None:
    payload = json.dumps(_notify(), separators=(",", ":"))
    payload = payload[:-1] + ',"symbol":"EVIL"}'
    capture = _CapturePort(["valid"])

    result = _resolver(capture).resolve(payload, decision_id=DECISION_ID)

    assert result.reason == "iqfeed_notify_duplicate_json_key"
    assert result.attempts == 0
    assert capture.calls == []


@pytest.mark.parametrize(
    ("clock_override", "now", "reason"),
    [
        (
            {
                "observed_at": (NOW - timedelta(seconds=3.3)).isoformat(),
                "provider_trade_reference_at": (
                    NOW - timedelta(seconds=3.3)
                ).isoformat(),
                "received_at": (NOW - timedelta(seconds=3.2)).isoformat(),
                "available_at": (NOW - timedelta(seconds=3.1)).isoformat(),
            },
            NOW,
            "iqfeed_notify_stale",
        ),
        (
            {
                "observed_at": (NOW + timedelta(seconds=1)).isoformat(),
                "provider_trade_reference_at": (
                    NOW + timedelta(seconds=1)
                ).isoformat(),
                "received_at": (NOW + timedelta(seconds=1)).isoformat(),
                "available_at": (NOW + timedelta(seconds=1)).isoformat(),
            },
            NOW,
            "iqfeed_notify_from_future",
        ),
    ],
)
def test_stale_or_future_notify_never_reaches_capture(
    clock_override: dict[str, str],
    now: datetime,
    reason: str,
) -> None:
    capture = _CapturePort(["valid"])

    result = _resolver(capture, now=now).resolve(
        _notify(**clock_override), decision_id=DECISION_ID
    )

    assert result.coverage_unavailable is True
    assert result.reason == reason
    assert result.attempts == 0
    assert capture.calls == []


def test_network_fallback_capability_is_rejected_without_read() -> None:
    capture = _CapturePort(["valid"], network_fallback_allowed=True)

    result = _resolver(capture).resolve(_notify(), decision_id=DECISION_ID)

    assert result.reason == "iqfeed_capture_network_fallback_forbidden"
    assert result.attempts == 0
    assert capture.calls == []
    assert capture.network_calls == 0


def test_retry_configuration_is_finite_and_resource_bounded() -> None:
    capture = _CapturePort(["valid"])
    with pytest.raises(CaptureContractError, match="retry bound"):
        CapturedPaperIqfeedTriggerResolver(
            capture=capture,
            expected_bridge_version=BRIDGE_VERSION,
            wall_clock=lambda: NOW,
            wait=lambda _seconds: None,
            max_attempts=65,
            retry_delay_seconds=0.01,
            max_notify_age_seconds=2.0,
            future_tolerance_seconds=0.25,
        )


def test_resolver_has_no_mutating_admission_or_order_capability() -> None:
    capture = _CapturePort(["valid"])
    resolver = _resolver(capture)

    forbidden = {
        "create_session",
        "consume_opportunity",
        "reserve_risk",
        "create_outbox",
        "post_order",
        "fetch_provider",
        "read_database",
        "read_current_state",
    }
    assert forbidden.isdisjoint(set(dir(resolver)))
    assert forbidden.isdisjoint(set(dir(type(resolver))))
