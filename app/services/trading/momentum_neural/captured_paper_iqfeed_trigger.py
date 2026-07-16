"""Capture-only IQFeed Q-notify to exact-print trigger authority.

The database ``NOTIFY`` is only a wake-up hint.  It is never market evidence and
the Most-Recent-Trade timestamp in that quote envelope is never treated as an
exact quote clock.  This module accepts the complete immutable Q envelope, then
uses the public capture-window read seam to wait a finite amount of time for the
matching, already-durable ``IQFEED_PRINT`` event.  It has no provider, database,
current-state, or network fallback.

Failure is event-local ``COVERAGE_UNAVAILABLE``.  In particular, this boundary
does not create a session, consume an opportunity, reserve risk, create an
outbox row, or submit an order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import math
import re
from typing import Any, Callable, Mapping, Protocol, runtime_checkable
import uuid

from .live_replay_capture import CapturedReadResult
from .replay_capture_contract import (
    CaptureContractError,
    CaptureEvent,
    CaptureEventRef,
    CaptureIqfeedPrint,
    CaptureMicrostructureOperation,
    CaptureMicrostructureReadQuery,
    CaptureStream,
    IQFEED_EXACT_PRINT_SOURCE_PROVENANCE_SCHEMA_VERSION,
    IQFEED_L1_SOURCE_PROVENANCE_FIELD,
    captured_read_result_sha256,
    resolve_capture_source_payload,
    sha256_json,
    validate_iqfeed_exact_print_source_provenance,
)


IQFEED_TRIGGER_RECEIPT_SCHEMA_VERSION = (
    "chili.captured-paper-iqfeed-trigger-receipt.v1"
)
IQFEED_Q_NOTIFY_SCHEMA_VERSION = "chili.iqfeed-q-notify-authority.v1"

_IQFEED_Q_TIMESTAMP_BASIS = "iqfeed_q_receive_trade_reference_fenced"
_IQFEED_EXACT_PRINT_TIMESTAMP_BASIS = (
    "iqfeed_selected_trade_date_timems_exact"
)
_BRIDGE_BUILD_RE = re.compile(
    r"iqfeed-l1-exact-print-provenance-v3\+sha256:[0-9a-f]{16}"
)
_EQUITY_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.]{0,15}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_NOTIFY_FIELDS = frozenset(
    {
        "symbol",
        "observed_at",
        "bid",
        "ask",
        "received_at",
        "provider_event_at",
        "provider_trade_reference_at",
        "timestamp_basis",
        "source",
        "bridge_version",
        "message_type",
        "bridge_run_id",
        "connection_generation",
        "source_frame_sequence",
        "source_frame_sha256",
        "available_at",
    }
)
_READ_NAMESPACE = uuid.UUID("bd82012b-8470-42bf-98ad-ce8b0742df56")


class IqfeedTriggerStatus(str, Enum):
    READY = "ready"
    COVERAGE_UNAVAILABLE = "coverage_unavailable"


class _NotifyRejected(ValueError):
    def __init__(self, reason: str):
        self.reason = str(reason)
        super().__init__(self.reason)


def _reject(reason: str) -> None:
    raise _NotifyRejected(reason)


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _reject(f"{field_name}_invalid")
    return value.astimezone(timezone.utc)


def _parse_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        _reject(f"{field_name}_invalid")
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        _reject(f"{field_name}_invalid")
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _reject(f"{field_name}_invalid")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _positive_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject(f"{field_name}_invalid")
    resolved = float(value)
    if not math.isfinite(resolved) or resolved <= 0.0:
        _reject(f"{field_name}_invalid")
    return resolved


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _reject(f"{field_name}_invalid")
    return value


def _sha(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _reject(f"{field_name}_invalid")
    return value


def _canonical_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        _reject(f"{field_name}_invalid")
    try:
        resolved = str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        _reject(f"{field_name}_invalid")
    if value != resolved:
        _reject(f"{field_name}_invalid")
    return resolved


def _symbol(value: Any) -> str:
    if (
        not isinstance(value, str)
        or _EQUITY_SYMBOL_RE.fullmatch(value) is None
        or value.endswith(".")
        or ".." in value
    ):
        _reject("iqfeed_notify_symbol_invalid")
    return value


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _reject("iqfeed_notify_duplicate_json_key")
        value[key] = item
    return value


@dataclass(frozen=True, slots=True)
class IqfeedQNotify:
    """Strict normalized wake-up envelope emitted by the IQFeed bridge."""

    symbol: str
    observed_at: datetime
    bid: float
    ask: float
    received_at: datetime
    provider_trade_reference_at: datetime
    bridge_version: str
    bridge_run_id: str
    connection_generation: int
    source_frame_sequence: int
    source_frame_sha256: str
    available_at: datetime
    source: str = "iqfeed_l1"
    message_type: str = "Q"
    timestamp_basis: str = _IQFEED_Q_TIMESTAMP_BASIS
    provider_event_at: None = None
    schema_version: str = IQFEED_Q_NOTIFY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "observed_at": _iso(self.observed_at),
            "bid": self.bid,
            "ask": self.ask,
            "received_at": _iso(self.received_at),
            "provider_event_at": None,
            "provider_trade_reference_at": _iso(
                self.provider_trade_reference_at
            ),
            "timestamp_basis": self.timestamp_basis,
            "source": self.source,
            "bridge_version": self.bridge_version,
            "message_type": self.message_type,
            "bridge_run_id": self.bridge_run_id,
            "connection_generation": self.connection_generation,
            "source_frame_sequence": self.source_frame_sequence,
            "source_frame_sha256": self.source_frame_sha256,
            "available_at": _iso(self.available_at),
        }

    @property
    def content_sha256(self) -> str:
        return sha256_json(self.to_dict())


@dataclass(frozen=True, slots=True)
class CapturedPaperIqfeedTriggerReceipt:
    """Content-addressed authority linking one notify to one durable print/read."""

    decision_id: str
    notify_sha256: str
    symbol: str
    bridge_version: str
    bridge_run_id: str
    connection_generation: int
    source_frame_sequence: int
    source_frame_sha256: str
    provider_trade_reference_at: datetime
    notify_received_at: datetime
    notify_available_at: datetime
    capture_identity_sha256: str
    captured_read_id: str
    captured_read_receipt_sha256: str
    captured_read_receipt_event_sha256: str
    captured_read_receipt_event_sequence: int
    captured_read_result_sha256: str
    captured_read_query_sha256: str
    source_event_sha256: str
    source_event_sequence: int
    source_payload_sha256: str
    source_provenance_sha256: str
    source_provider_event_at: datetime
    source_received_at: datetime
    source_available_at: datetime
    schema_version: str = IQFEED_TRIGGER_RECEIPT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "notify_sha256": self.notify_sha256,
            "symbol": self.symbol,
            "bridge_version": self.bridge_version,
            "bridge_run_id": self.bridge_run_id,
            "connection_generation": self.connection_generation,
            "source_frame_sequence": self.source_frame_sequence,
            "source_frame_sha256": self.source_frame_sha256,
            "provider_trade_reference_at": _iso(
                self.provider_trade_reference_at
            ),
            "notify_received_at": _iso(self.notify_received_at),
            "notify_available_at": _iso(self.notify_available_at),
            "capture_identity_sha256": self.capture_identity_sha256,
            "captured_read_id": self.captured_read_id,
            "captured_read_receipt_sha256": (
                self.captured_read_receipt_sha256
            ),
            "captured_read_receipt_event_sha256": (
                self.captured_read_receipt_event_sha256
            ),
            "captured_read_receipt_event_sequence": (
                self.captured_read_receipt_event_sequence
            ),
            "captured_read_result_sha256": self.captured_read_result_sha256,
            "captured_read_query_sha256": self.captured_read_query_sha256,
            "source_event_sha256": self.source_event_sha256,
            "source_event_sequence": self.source_event_sequence,
            "source_payload_sha256": self.source_payload_sha256,
            "source_provenance_sha256": self.source_provenance_sha256,
            "source_provider_event_at": _iso(self.source_provider_event_at),
            "source_received_at": _iso(self.source_received_at),
            "source_available_at": _iso(self.source_available_at),
        }

    @property
    def content_sha256(self) -> str:
        return sha256_json(self.to_dict())


@dataclass(frozen=True, slots=True)
class IqfeedTriggerResolution:
    status: IqfeedTriggerStatus
    reason: str
    attempts: int
    notify_sha256: str | None = None
    receipt: CapturedPaperIqfeedTriggerReceipt | None = None
    # Process-private typed evidence for the next capture attestation.  It is
    # deliberately excluded from equality/repr and from the content-addressed
    # receipt: callers cannot serialize this object and later mint live input
    # authority from hashes alone.
    captured_read: CapturedReadResult | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int):
            raise CaptureContractError("IQFeed trigger attempt count is malformed")
        if self.attempts < 0:
            raise CaptureContractError("IQFeed trigger attempt count is malformed")
        if self.status is IqfeedTriggerStatus.COVERAGE_UNAVAILABLE:
            if self.receipt is not None or self.captured_read is not None:
                raise CaptureContractError(
                    "unavailable IQFeed trigger cannot carry live read authority"
                )
            return
        if self.status is not IqfeedTriggerStatus.READY:
            raise CaptureContractError("IQFeed trigger status is malformed")
        receipt = self.receipt
        captured = self.captured_read
        if (
            not isinstance(receipt, CapturedPaperIqfeedTriggerReceipt)
            or not isinstance(captured, CapturedReadResult)
            or not captured.durable
            or captured.receipt is None
            or captured.receipt_submission is None
            or captured.receipt_submission.event is None
            or captured.receipt.read_id != receipt.captured_read_id
            or captured.receipt.identity_sha256 != receipt.capture_identity_sha256
            or captured.receipt.result_sha256
            != receipt.captured_read_result_sha256
            or captured.receipt_submission.event.event_sha256
            != receipt.captured_read_receipt_event_sha256
            or captured.receipt_submission.event.sequence
            != receipt.captured_read_receipt_event_sequence
        ):
            raise CaptureContractError(
                "ready IQFeed trigger lacks its exact process-private durable read"
            )

    @property
    def ready(self) -> bool:
        return self.status is IqfeedTriggerStatus.READY

    @property
    def coverage_unavailable(self) -> bool:
        return self.status is IqfeedTriggerStatus.COVERAGE_UNAVAILABLE


@runtime_checkable
class CapturedIqfeedReadPort(Protocol):
    """The sole allowed read capability; this seam performs no fetch."""

    @property
    def network_fallback_allowed(self) -> bool: ...

    def capture_complete_microstructure_window(
        self,
        *,
        decision_id: str,
        operation: CaptureMicrostructureOperation,
        stream: CaptureStream,
        provider: str,
        symbol: str,
        requested_at: datetime,
        returned_at: datetime,
        event_start_exclusive: datetime,
        event_end_inclusive: datetime,
        parameters: Mapping[str, Any],
        read_id: str | None = None,
    ) -> CapturedReadResult: ...


def _unavailable(
    reason: str,
    *,
    attempts: int,
    notify_sha256: str | None = None,
) -> IqfeedTriggerResolution:
    return IqfeedTriggerResolution(
        status=IqfeedTriggerStatus.COVERAGE_UNAVAILABLE,
        reason=str(reason),
        attempts=int(attempts),
        notify_sha256=notify_sha256,
    )


def _parse_notify(
    payload: str | Mapping[str, Any],
    *,
    expected_bridge_version: str,
) -> IqfeedQNotify:
    if isinstance(payload, str):
        try:
            raw = json.loads(
                payload,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=lambda _value: _reject(
                    "iqfeed_notify_nonfinite_json"
                ),
            )
        except _NotifyRejected:
            raise
        except (TypeError, ValueError):
            _reject("iqfeed_notify_json_invalid")
    elif isinstance(payload, Mapping):
        raw = dict(payload)
    else:
        _reject("iqfeed_notify_json_invalid")
    if not isinstance(raw, Mapping) or set(raw) != _NOTIFY_FIELDS:
        _reject("iqfeed_notify_fields_mismatch")
    symbol = _symbol(raw.get("symbol"))
    expected_build = str(expected_bridge_version or "")
    bridge_version = raw.get("bridge_version")
    if (
        _BRIDGE_BUILD_RE.fullmatch(expected_build) is None
        or bridge_version != expected_build
    ):
        _reject("iqfeed_notify_bridge_version_mismatch")
    if (
        raw.get("source") != "iqfeed_l1"
        or raw.get("message_type") != "Q"
        or raw.get("timestamp_basis") != _IQFEED_Q_TIMESTAMP_BASIS
        or raw.get("provider_event_at", object()) is not None
    ):
        _reject("iqfeed_notify_authority_class_invalid")
    observed = _parse_utc(raw.get("observed_at"), "iqfeed_notify_observed_at")
    reference = _parse_utc(
        raw.get("provider_trade_reference_at"),
        "iqfeed_notify_provider_trade_reference_at",
    )
    received = _parse_utc(
        raw.get("received_at"), "iqfeed_notify_received_at"
    )
    available = _parse_utc(
        raw.get("available_at"), "iqfeed_notify_available_at"
    )
    if observed != reference or available < received:
        _reject("iqfeed_notify_clock_relation_invalid")
    bid = _positive_number(raw.get("bid"), "iqfeed_notify_bid")
    ask = _positive_number(raw.get("ask"), "iqfeed_notify_ask")
    if ask < bid:
        _reject("iqfeed_notify_quote_invalid")
    return IqfeedQNotify(
        symbol=symbol,
        observed_at=observed,
        bid=bid,
        ask=ask,
        received_at=received,
        provider_trade_reference_at=reference,
        bridge_version=expected_build,
        bridge_run_id=_canonical_uuid(
            raw.get("bridge_run_id"), "iqfeed_notify_bridge_run_id"
        ),
        connection_generation=_positive_int(
            raw.get("connection_generation"),
            "iqfeed_notify_connection_generation",
        ),
        source_frame_sequence=_positive_int(
            raw.get("source_frame_sequence"),
            "iqfeed_notify_source_frame_sequence",
        ),
        source_frame_sha256=_sha(
            raw.get("source_frame_sha256"),
            "iqfeed_notify_source_frame_sha256",
        ),
        available_at=available,
    )


def parse_captured_paper_iqfeed_q_notify(
    payload: str | Mapping[str, Any],
    *,
    expected_bridge_version: str,
) -> IqfeedQNotify:
    """Return only a byte-schema-valid Q wake-up before hot-run allocation.

    Freshness and exact-print authority are deliberately rechecked by
    :meth:`CapturedPaperIqfeedTriggerResolver.resolve` after the bounded hot
    capture run exists.  This public pre-parser only prevents malformed or
    foreign bridge envelopes from allocating those resources in the first
    place.
    """

    try:
        return _parse_notify(
            payload,
            expected_bridge_version=expected_bridge_version,
        )
    except _NotifyRejected as exc:
        raise CaptureContractError(exc.reason) from exc


def _freshness_reason(
    notify: IqfeedQNotify,
    *,
    now: datetime,
    max_notify_age_seconds: float,
    future_tolerance_seconds: float,
) -> str | None:
    anchors = (
        notify.provider_trade_reference_at,
        notify.received_at,
        notify.available_at,
    )
    if notify.provider_trade_reference_at > (
        notify.received_at + timedelta(seconds=future_tolerance_seconds)
    ):
        return "iqfeed_notify_reference_from_future"
    ages = tuple((now - anchor).total_seconds() for anchor in anchors)
    if any(age < -future_tolerance_seconds for age in ages):
        return "iqfeed_notify_from_future"
    if any(age > max_notify_age_seconds for age in ages):
        return "iqfeed_notify_stale"
    return None


def _read_id(
    *, decision_id: str, notify_sha256: str, attempt: int
) -> str:
    return str(
        uuid.uuid5(
            _READ_NAMESPACE,
            f"{decision_id}:{notify_sha256}:{attempt}",
        )
    )


def _receipt_from_result(
    result: CapturedReadResult,
    *,
    notify: IqfeedQNotify,
    notify_sha256: str,
    decision_id: str,
    expected_read_id: str,
    returned_at: datetime,
    window_seconds: float,
) -> tuple[CapturedPaperIqfeedTriggerReceipt | None, str]:
    if not isinstance(result, CapturedReadResult) or not result.durable:
        return None, "iqfeed_exact_print_capture_read_unavailable"
    receipt = result.receipt
    submission = result.receipt_submission
    assert receipt is not None and submission is not None
    receipt_event = submission.event
    if (
        receipt_event is None
        or receipt.read_id != expected_read_id
        or receipt.decision_id != decision_id
        or receipt.stream is not CaptureStream.IQFEED_PRINT
        or receipt.provider != "iqfeed"
        or receipt.symbol != notify.symbol
        or receipt.requested_at != notify.available_at
        or receipt.returned_at != returned_at
        or not receipt.content_verified
        or receipt.replay_network_fallback_used
        or receipt.empty_result
        or receipt_event.stream is not CaptureStream.READ_RECEIPT
        or receipt_event.identity.identity_sha256 != receipt.identity_sha256
        or receipt_event.provider != "iqfeed"
        or receipt_event.symbol != notify.symbol
        or receipt_event.clocks.received_at < receipt.returned_at
        or receipt_event.clocks.available_at < receipt.returned_at
        or receipt_event.payload != receipt.to_dict()
    ):
        return None, "iqfeed_exact_print_capture_read_mismatch"
    if len(result.source_events) != 1 or len(receipt.source_event_sha256s) != 1:
        return None, "iqfeed_exact_print_capture_read_ambiguous"
    source = result.source_events[0]
    if (
        not isinstance(source, CaptureEvent)
        or receipt.source_event_sha256s != (source.event_sha256,)
        or source.identity.identity_sha256 != receipt.identity_sha256
        or source.stream is not CaptureStream.IQFEED_PRINT
        or source.provider != "iqfeed"
        or source.symbol != notify.symbol
        or source.clocks.provider_event_at
        != notify.provider_trade_reference_at
        or source.clocks.market_reference_at is not None
        or source.clocks.received_at != notify.received_at
        or source.clocks.available_at != notify.available_at
        or source.clocks.available_at > returned_at
        or receipt_event.sequence <= source.sequence
    ):
        return None, "iqfeed_exact_print_source_identity_mismatch"
    ref = CaptureEventRef.from_event(source)
    if captured_read_result_sha256((ref,)) != receipt.result_sha256:
        return None, "iqfeed_exact_print_result_hash_mismatch"
    try:
        query = CaptureMicrostructureReadQuery.from_dict(receipt.query or {})
    except (CaptureContractError, ValueError):
        return None, "iqfeed_exact_print_query_mismatch"
    if (
        query.operation is not CaptureMicrostructureOperation.TRADE_FLOW
        or query.stream is not CaptureStream.IQFEED_PRINT
        or query.symbol != notify.symbol
        or query.provider != "iqfeed"
        or query.event_end_inclusive
        != notify.provider_trade_reference_at
        or query.available_at_most != returned_at
        or query.parameters != {"window_seconds": window_seconds}
    ):
        return None, "iqfeed_exact_print_query_mismatch"
    try:
        exact_print = CaptureIqfeedPrint.from_event(source)
        view = resolve_capture_source_payload(source)
        provenance = view.payload.get(IQFEED_L1_SOURCE_PROVENANCE_FIELD)
        validated = validate_iqfeed_exact_print_source_provenance(
            provenance,
            symbol=notify.symbol,
            clocks=source.clocks,
        )
    except CaptureContractError:
        return None, "iqfeed_exact_print_provenance_invalid"
    if (
        validated.get("schema_version")
        != IQFEED_EXACT_PRINT_SOURCE_PROVENANCE_SCHEMA_VERSION
        or validated.get("bridge_run_id") != notify.bridge_run_id
        or validated.get("connection_generation")
        != notify.connection_generation
        or validated.get("bridge_version") != notify.bridge_version
        or validated.get("message_type") != "Q"
        or validated.get("timestamp_basis")
        != _IQFEED_EXACT_PRINT_TIMESTAMP_BASIS
        or validated.get("source_frame_sequence")
        != notify.source_frame_sequence
        or validated.get("source_frame_sha256")
        != notify.source_frame_sha256
        or exact_print.bid != notify.bid
        or exact_print.ask != notify.ask
    ):
        return None, "iqfeed_exact_print_provenance_mismatch"
    return (
        CapturedPaperIqfeedTriggerReceipt(
            decision_id=decision_id,
            notify_sha256=notify_sha256,
            symbol=notify.symbol,
            bridge_version=notify.bridge_version,
            bridge_run_id=notify.bridge_run_id,
            connection_generation=notify.connection_generation,
            source_frame_sequence=notify.source_frame_sequence,
            source_frame_sha256=notify.source_frame_sha256,
            provider_trade_reference_at=(
                notify.provider_trade_reference_at
            ),
            notify_received_at=notify.received_at,
            notify_available_at=notify.available_at,
            capture_identity_sha256=receipt.identity_sha256,
            captured_read_id=receipt.read_id,
            captured_read_receipt_sha256=sha256_json(receipt.to_dict()),
            captured_read_receipt_event_sha256=receipt_event.event_sha256,
            captured_read_receipt_event_sequence=receipt_event.sequence,
            captured_read_result_sha256=receipt.result_sha256,
            captured_read_query_sha256=receipt.query_sha256,
            source_event_sha256=source.event_sha256,
            source_event_sequence=source.sequence,
            source_payload_sha256=source.payload_sha256,
            source_provenance_sha256=sha256_json(validated),
            source_provider_event_at=notify.provider_trade_reference_at,
            source_received_at=source.clocks.received_at,
            source_available_at=source.clocks.available_at,
        ),
        "iqfeed_exact_print_trigger_ready",
    )


class CapturedPaperIqfeedTriggerResolver:
    """Finite capture-only resolver for one initial-admission wake-up."""

    def __init__(
        self,
        *,
        capture: CapturedIqfeedReadPort,
        expected_bridge_version: str,
        wall_clock: Callable[[], datetime],
        wait: Callable[[float], None],
        max_attempts: int,
        retry_delay_seconds: float,
        max_notify_age_seconds: float,
        future_tolerance_seconds: float,
        exact_print_window_seconds: float = 0.001,
    ) -> None:
        if not isinstance(capture, CapturedIqfeedReadPort):
            raise CaptureContractError("IQFeed trigger capture port is malformed")
        if not callable(wall_clock) or not callable(wait):
            raise CaptureContractError("IQFeed trigger clocks are malformed")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 64
        ):
            raise CaptureContractError("IQFeed trigger retry bound is malformed")
        numeric = (
            retry_delay_seconds,
            max_notify_age_seconds,
            future_tolerance_seconds,
            exact_print_window_seconds,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in numeric
        ):
            raise CaptureContractError("IQFeed trigger timing is malformed")
        if (
            float(retry_delay_seconds) < 0.0
            or float(retry_delay_seconds) > 1.0
            or float(max_notify_age_seconds) <= 0.0
            or float(future_tolerance_seconds) < 0.0
            or float(exact_print_window_seconds) <= 0.0
            or float(exact_print_window_seconds) > 1.0
        ):
            raise CaptureContractError("IQFeed trigger timing is outside bounds")
        expected = str(expected_bridge_version or "")
        if _BRIDGE_BUILD_RE.fullmatch(expected) is None:
            raise CaptureContractError(
                "IQFeed trigger bridge version is malformed"
            )
        self.capture = capture
        self.expected_bridge_version = expected
        self.wall_clock = wall_clock
        self.wait = wait
        self.max_attempts = max_attempts
        self.retry_delay_seconds = float(retry_delay_seconds)
        self.max_notify_age_seconds = float(max_notify_age_seconds)
        self.future_tolerance_seconds = float(future_tolerance_seconds)
        self.exact_print_window_seconds = float(exact_print_window_seconds)

    def resolve(
        self,
        payload: str | Mapping[str, Any],
        *,
        decision_id: str,
    ) -> IqfeedTriggerResolution:
        decision = str(decision_id or "").strip()
        if not decision:
            return _unavailable("iqfeed_trigger_decision_id_invalid", attempts=0)
        try:
            notify = _parse_notify(
                payload,
                expected_bridge_version=self.expected_bridge_version,
            )
        except _NotifyRejected as exc:
            return _unavailable(exc.reason, attempts=0)
        notify_sha256 = notify.content_sha256
        try:
            fallback_allowed = self.capture.network_fallback_allowed
        except Exception:
            return _unavailable(
                "iqfeed_capture_fallback_posture_unavailable",
                attempts=0,
                notify_sha256=notify_sha256,
            )
        if fallback_allowed is not False:
            return _unavailable(
                "iqfeed_capture_network_fallback_forbidden",
                attempts=0,
                notify_sha256=notify_sha256,
            )

        last_reason = "iqfeed_exact_print_capture_read_unavailable"
        for attempt in range(1, self.max_attempts + 1):
            try:
                now = _utc(self.wall_clock(), "iqfeed_trigger_wall_clock")
            except _NotifyRejected as exc:
                return _unavailable(
                    exc.reason,
                    attempts=attempt - 1,
                    notify_sha256=notify_sha256,
                )
            freshness = _freshness_reason(
                notify,
                now=now,
                max_notify_age_seconds=self.max_notify_age_seconds,
                future_tolerance_seconds=self.future_tolerance_seconds,
            )
            if freshness is not None:
                return _unavailable(
                    freshness,
                    attempts=attempt - 1,
                    notify_sha256=notify_sha256,
                )
            read_id = _read_id(
                decision_id=decision,
                notify_sha256=notify_sha256,
                attempt=attempt,
            )
            try:
                captured = self.capture.capture_complete_microstructure_window(
                    decision_id=decision,
                    operation=CaptureMicrostructureOperation.TRADE_FLOW,
                    stream=CaptureStream.IQFEED_PRINT,
                    provider="iqfeed",
                    symbol=notify.symbol,
                    requested_at=notify.available_at,
                    returned_at=now,
                    event_start_exclusive=(
                        notify.provider_trade_reference_at
                        - timedelta(seconds=self.exact_print_window_seconds)
                    ),
                    event_end_inclusive=notify.provider_trade_reference_at,
                    parameters={
                        "window_seconds": self.exact_print_window_seconds
                    },
                    read_id=read_id,
                )
            except CaptureContractError:
                captured = None
            if captured is not None:
                receipt, last_reason = _receipt_from_result(
                    captured,
                    notify=notify,
                    notify_sha256=notify_sha256,
                    decision_id=decision,
                    expected_read_id=read_id,
                    returned_at=now,
                    window_seconds=self.exact_print_window_seconds,
                )
                if receipt is not None:
                    return IqfeedTriggerResolution(
                        status=IqfeedTriggerStatus.READY,
                        reason=last_reason,
                        attempts=attempt,
                        notify_sha256=notify_sha256,
                        receipt=receipt,
                        captured_read=captured,
                    )
            if attempt < self.max_attempts:
                self.wait(self.retry_delay_seconds)
        return _unavailable(
            last_reason,
            attempts=self.max_attempts,
            notify_sha256=notify_sha256,
        )


__all__ = [
    "CapturedIqfeedReadPort",
    "CapturedPaperIqfeedTriggerReceipt",
    "CapturedPaperIqfeedTriggerResolver",
    "IQFEED_Q_NOTIFY_SCHEMA_VERSION",
    "IQFEED_TRIGGER_RECEIPT_SCHEMA_VERSION",
    "IqfeedQNotify",
    "IqfeedTriggerResolution",
    "IqfeedTriggerStatus",
    "parse_captured_paper_iqfeed_q_notify",
]
