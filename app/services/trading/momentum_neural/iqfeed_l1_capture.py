"""Bounded, no-fetch IQFeed L1 handoff into production replay capture.

The host IQFeed bridge is a separate process from CHILI's live FSM.  This module
defines the immutable envelope and the bounded producer-side queue at that
process boundary.  It never opens a socket, queries a database, or fetches a
provider value.  The bridge hands it only rows already observed and released.

IQFeed's default protocol-6.2 ``Q`` update supplies a Most-Recent-Trade-Time
reference, not an exact quote-event timestamp.  The envelope preserves that
reference as ``market_reference_at`` and deliberately leaves
``provider_event_at`` null.  Such rows are useful diagnostic evidence but fail
exact-clock replay coverage until an authoritative event clock is captured.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import queue
import threading
import time
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .replay_capture_contract import (
    CaptureExternalProducerGeneration,
    CaptureClocks,
    CaptureContractError,
    CaptureStream,
    CoverageGap,
    IQFEED_EXACT_PRINT_SOURCE_PROVENANCE_SCHEMA_VERSION,
    IQFEED_L1_SOURCE_PROVENANCE_FIELD,
    IQFEED_L1_SOURCE_PROVENANCE_SCHEMA_VERSION,
    IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
    NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION,
    canonical_json_bytes,
    sha256_json,
    validate_iqfeed_l1_source_provenance,
)


UTC = timezone.utc
IQFEED_L1_CAPTURE_ENVELOPE_SCHEMA_VERSION = (
    "chili.capture-handoff.iqfeed-l1.v1"
)
_ALLOWED_STREAMS = frozenset(
    {CaptureStream.IQFEED_PRINT, CaptureStream.NBBO_QUOTE}
)


def _utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CaptureContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _positive_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise CaptureContractError(f"{field_name} is malformed")
    try:
        resolved = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CaptureContractError(f"{field_name} is malformed") from exc
    if not math.isfinite(resolved) or resolved <= 0:
        raise CaptureContractError(f"{field_name} is malformed")
    return resolved


def _optional_positive_number(value: Any, field_name: str) -> float | None:
    return None if value is None else _positive_number(value, field_name)


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CaptureContractError(f"{field_name} must be a positive integer")
    return int(value)


def _require_sha256(value: Any, field_name: str) -> str:
    resolved = str(value or "").strip().lower()
    if len(resolved) != 64 or any(ch not in "0123456789abcdef" for ch in resolved):
        raise CaptureContractError(f"{field_name} is malformed")
    return resolved


@dataclass(frozen=True)
class IqfeedL1CaptureEnvelope:
    """Immutable exact handoff for one released trade or quote observation."""

    stream: CaptureStream
    symbol: str
    clocks: CaptureClocks
    payload_json: str
    source_frame_sequence: int
    provider: str = "iqfeed"
    schema_version: str = IQFEED_L1_CAPTURE_ENVELOPE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != IQFEED_L1_CAPTURE_ENVELOPE_SCHEMA_VERSION:
            raise CaptureContractError("IQFeed L1 capture envelope schema is unsupported")
        try:
            stream = (
                self.stream
                if isinstance(self.stream, CaptureStream)
                else CaptureStream(str(self.stream))
            )
        except ValueError as exc:
            raise CaptureContractError("IQFeed L1 capture stream is unknown") from exc
        if stream not in _ALLOWED_STREAMS:
            raise CaptureContractError("IQFeed L1 capture stream is unsupported")
        object.__setattr__(self, "stream", stream)
        symbol = str(self.symbol or "").strip().upper()
        if not symbol:
            raise CaptureContractError("IQFeed L1 capture symbol is required")
        object.__setattr__(self, "symbol", symbol)
        if not isinstance(self.clocks, CaptureClocks):
            raise CaptureContractError("IQFeed L1 capture clocks are malformed")
        sequence = self.source_frame_sequence
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise CaptureContractError("IQFeed L1 source frame sequence is malformed")
        if str(self.provider or "").strip().lower() != "iqfeed":
            raise CaptureContractError("IQFeed L1 capture provider is invalid")
        object.__setattr__(self, "provider", "iqfeed")
        try:
            payload = json.loads(str(self.payload_json or ""))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CaptureContractError("IQFeed L1 capture payload is malformed") from exc
        if not isinstance(payload, Mapping):
            raise CaptureContractError("IQFeed L1 capture payload must be a mapping")
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        object.__setattr__(self, "payload_json", canonical)
        if str(payload.get("symbol") or "").strip().upper() != symbol:
            raise CaptureContractError("IQFeed L1 capture payload symbol mismatch")
        expected_schema = (
            IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION
            if stream is CaptureStream.IQFEED_PRINT
            else NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION
        )
        if payload.get("schema_version") != expected_schema:
            raise CaptureContractError("IQFeed L1 capture payload schema mismatch")
        source_provenance = payload.get(IQFEED_L1_SOURCE_PROVENANCE_FIELD)
        validate_iqfeed_l1_source_provenance(
            source_provenance,
            symbol=symbol,
            clocks=self.clocks,
        )
        assert isinstance(source_provenance, Mapping)
        if source_provenance.get("source_frame_sequence") != sequence:
            raise CaptureContractError(
                "IQFeed L1 envelope/source frame sequence mismatch"
            )

    @property
    def payload(self) -> Mapping[str, Any]:
        # Return a fresh mapping so callers cannot mutate bytes already hashed by
        # this envelope or observed later by the asynchronous worker.
        value = json.loads(self.payload_json)
        assert isinstance(value, dict)
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "stream": self.stream.value,
            "provider": self.provider,
            "symbol": self.symbol,
            "source_frame_sequence": self.source_frame_sequence,
            "clocks": self.clocks.to_dict(),
            "payload": self.payload,
        }

    @property
    def envelope_sha256(self) -> str:
        return sha256_json(self.to_dict())

    @property
    def canonical_size_bytes(self) -> int:
        """Deterministic uncompressed bytes reserved while this envelope is live."""

        return len(canonical_json_bytes(self.to_dict()))

    @classmethod
    def from_released_row(
        cls,
        row: Mapping[str, Any],
        *,
        stream: CaptureStream,
        available_at: datetime,
        bridge_source_sha256: str,
        bridge_configuration: Mapping[str, Any],
        bridge_configuration_sha256: str,
        capture_resource_binding_sha256: str,
        handoff_configuration: Mapping[str, Any],
        handoff_configuration_sha256: str,
    ) -> "IqfeedL1CaptureEnvelope":
        if not isinstance(row, Mapping):
            raise CaptureContractError("IQFeed L1 released row is malformed")
        if stream not in _ALLOWED_STREAMS:
            raise CaptureContractError("IQFeed L1 released row stream is unsupported")
        symbol = str(row.get("sym") or "").strip().upper()
        received_at = _utc(row.get("received_at"), "IQFeed L1 received_at")
        released_at = _utc(available_at, "IQFeed L1 available_at")
        exact_print = bool(
            stream is CaptureStream.IQFEED_PRINT
            and row.get("provider_at") is not None
        )
        if exact_print:
            provider_event_at = _utc(
                row.get("provider_at"), "IQFeed exact-print provider event"
            )
            reference = None
        else:
            provider_event_at = None
            reference = _utc(
                row.get("provider_trade_reference_at"),
                "IQFeed L1 provider trade reference",
            )
        # A quote frame still has no single exact quote-event clock.  Only the
        # selected-field trade row may carry the provider's exact trade date/time.
        if stream is CaptureStream.NBBO_QUOTE and row.get("provider_at") is not None:
            raise CaptureContractError(
                "IQFeed L1 Q capture cannot claim an exact provider event clock"
            )
        clocks = CaptureClocks(
            provider_event_at=provider_event_at,
            market_reference_at=reference,
            received_at=received_at,
            available_at=released_at,
        )
        generation = row.get("connection_generation")
        frame_sequence = row.get("source_frame_sequence")
        common_provenance = {
            "symbol": symbol,
            "bridge_run_id": str(row.get("bridge_run_id") or "").strip().lower(),
            "connection_generation": generation,
            "bridge_version": str(row.get("bridge") or "").strip(),
            "bridge_source_sha256": str(bridge_source_sha256 or "").strip().lower(),
            "bridge_configuration": dict(bridge_configuration),
            "bridge_configuration_sha256": str(
                bridge_configuration_sha256 or ""
            ).strip().lower(),
            "capture_resource_binding_sha256": str(
                capture_resource_binding_sha256 or ""
            ).strip().lower(),
            "handoff_configuration": dict(handoff_configuration),
            "handoff_configuration_sha256": str(
                handoff_configuration_sha256 or ""
            ).strip().lower(),
            "message_type": str(row.get("message_type") or "").strip().upper(),
            "timestamp_basis": str(row.get("basis") or "").strip(),
            "source_frame_sequence": frame_sequence,
            "source_frame_sha256": str(
                row.get("source_frame_sha256") or ""
            ).strip().lower(),
        }
        if exact_print:
            raw_fields = row.get("selected_update_fields")
            selected_fields = (
                list(raw_fields)
                if isinstance(raw_fields, (list, tuple))
                else raw_fields
            )
            raw_conditions = row.get("trade_conditions", [])
            trade_conditions = (
                list(raw_conditions)
                if isinstance(raw_conditions, (list, tuple))
                else raw_conditions
            )
            source_provenance = {
                "schema_version": (
                    IQFEED_EXACT_PRINT_SOURCE_PROVENANCE_SCHEMA_VERSION
                ),
                **common_provenance,
                "provider_event_at": provider_event_at.isoformat().replace(
                    "+00:00", "Z"
                ),
                "received_at": received_at.isoformat().replace("+00:00", "Z"),
                "provider_trade_date": str(
                    row.get("provider_trade_date") or ""
                ).strip(),
                "provider_trade_time": str(
                    row.get("provider_trade_time") or ""
                ).strip(),
                "provider_tick_id": str(row.get("provider_tick_id") or "").strip(),
                "trade_market_center": str(
                    row.get("trade_market_center") or ""
                ).strip(),
                "trade_conditions": trade_conditions,
                "message_contents": str(row.get("message_contents") or ""),
                "selected_update_fields": selected_fields,
                "selected_update_fields_sha256": str(
                    row.get("selected_update_fields_sha256") or ""
                ).strip().lower(),
                "selected_update_fields_ack_sha256": str(
                    row.get("selected_update_fields_ack_sha256") or ""
                ).strip().lower(),
            }
        else:
            assert reference is not None
            source_provenance = {
                "schema_version": IQFEED_L1_SOURCE_PROVENANCE_SCHEMA_VERSION,
                **common_provenance,
                "provider_event_at": None,
                "provider_trade_reference_at": reference.isoformat().replace(
                    "+00:00", "Z"
                ),
            }
        if stream is CaptureStream.IQFEED_PRINT:
            bid = _optional_positive_number(row.get("bid"), "IQFeed L1 trade bid")
            ask = _optional_positive_number(row.get("ask"), "IQFeed L1 trade ask")
            if (bid is None) != (ask is None) or (
                bid is not None and ask is not None and ask < bid
            ):
                raise CaptureContractError("IQFeed L1 trade quote context is invalid")
            payload: dict[str, Any] = {
                "schema_version": IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION,
                "symbol": symbol,
                "price": _positive_number(row.get("px"), "IQFeed L1 trade price"),
                "size": _positive_number(row.get("sz"), "IQFeed L1 trade size"),
                "bid": bid,
                "ask": ask,
                "conditions": trade_conditions if exact_print else [],
                IQFEED_L1_SOURCE_PROVENANCE_FIELD: source_provenance,
            }
        else:
            bid = _positive_number(row.get("bid"), "IQFeed L1 quote bid")
            ask = _positive_number(row.get("ask"), "IQFeed L1 quote ask")
            if ask < bid:
                raise CaptureContractError("IQFeed L1 quote ask cannot be below bid")
            payload = {
                "schema_version": NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION,
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                IQFEED_L1_SOURCE_PROVENANCE_FIELD: source_provenance,
            }
        return cls(
            stream=stream,
            symbol=symbol,
            clocks=clocks,
            payload_json=json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ),
            source_frame_sequence=frame_sequence,
        )


@runtime_checkable
class IqfeedL1CaptureSink(Protocol):
    """No-fetch destination used by the producer-side bounded worker."""

    @property
    def network_fallback_allowed(self) -> bool: ...

    @property
    def capture_resource_binding_sha256(self) -> str: ...

    @property
    def capture_queue_event_limit(self) -> int: ...

    @property
    def capture_queue_byte_limit(self) -> int: ...

    @property
    def capture_gap_key_limit(self) -> int: ...

    def submit_envelope(self, envelope: IqfeedL1CaptureEnvelope) -> None: ...

    def report_gap(self, gap: CoverageGap) -> None: ...


class _CaptureSubmissionRejected(CaptureContractError):
    def __init__(self, *, coverage_gap_recorded: bool, disposition: str) -> None:
        super().__init__(
            "IQFeed capture service rejected input: "
            + (disposition or "unknown_disposition")
        )
        self.coverage_gap_recorded = bool(coverage_gap_recorded)


class IqfeedL1ProcessCaptureSink:
    """Adapter from immutable bridge envelopes to the shared capture service."""

    def __init__(self, service: Any) -> None:
        if bool(getattr(service, "network_fallback_allowed", True)):
            raise CaptureContractError(
                "IQFeed capture process service permits a network fallback"
            )
        for name in ("record_broad_input", "record_broad_gap"):
            if not callable(getattr(service, name, None)):
                raise CaptureContractError(
                    f"IQFeed capture process service lacks {name}"
                )
        binding_sha256 = _require_sha256(
            getattr(service, "capture_resource_binding_sha256", None),
            "IQFeed capture service resource binding SHA-256",
        )
        queue_limit = getattr(service, "capture_queue_event_limit", None)
        byte_limit = getattr(service, "capture_queue_byte_limit", None)
        gap_limit = getattr(service, "capture_gap_key_limit", None)
        if (
            isinstance(queue_limit, bool)
            or not isinstance(queue_limit, int)
            or queue_limit <= 0
            or isinstance(byte_limit, bool)
            or not isinstance(byte_limit, int)
            or byte_limit <= 0
            or isinstance(gap_limit, bool)
            or not isinstance(gap_limit, int)
            or gap_limit <= 0
        ):
            raise CaptureContractError(
                "IQFeed capture service resource limits are malformed"
            )
        self.service = service
        self._capture_resource_binding_sha256 = binding_sha256
        self._capture_queue_event_limit = queue_limit
        self._capture_queue_byte_limit = byte_limit
        self._capture_gap_key_limit = gap_limit

    @property
    def network_fallback_allowed(self) -> bool:
        return False

    @property
    def capture_resource_binding_sha256(self) -> str:
        return self._capture_resource_binding_sha256

    @property
    def capture_queue_event_limit(self) -> int:
        return self._capture_queue_event_limit

    @property
    def capture_queue_byte_limit(self) -> int:
        return self._capture_queue_byte_limit

    @property
    def capture_gap_key_limit(self) -> int:
        return self._capture_gap_key_limit

    def submit_envelope(self, envelope: IqfeedL1CaptureEnvelope) -> None:
        if not isinstance(envelope, IqfeedL1CaptureEnvelope):
            raise CaptureContractError("IQFeed capture envelope is malformed")
        result = self.service.record_broad_input(
            stream=envelope.stream,
            provider=envelope.provider,
            payload=envelope.payload,
            clocks=envelope.clocks,
            symbol=envelope.symbol,
        )
        if not bool(getattr(result, "accepted", False)):
            raise _CaptureSubmissionRejected(
                coverage_gap_recorded=bool(
                    getattr(result, "coverage_gap_recorded", False)
                ),
                disposition=str(getattr(result, "disposition", "") or ""),
            )

    def report_gap(self, gap: CoverageGap) -> None:
        if not isinstance(gap, CoverageGap):
            raise CaptureContractError("IQFeed capture gap is malformed")
        self.service.record_broad_gap(gap)


@dataclass
class _GapAccumulator:
    stream: CaptureStream
    symbol: str | None
    reason: str
    first_available_at: datetime
    last_available_at: datetime
    lost_count: int

    def add(self, gap: CoverageGap) -> None:
        self.first_available_at = min(
            self.first_available_at, gap.first_available_at
        )
        self.last_available_at = max(self.last_available_at, gap.last_available_at)
        self.lost_count += gap.lost_count

    def freeze(self) -> CoverageGap:
        return CoverageGap(
            stream=self.stream,
            symbol=self.symbol,
            reason=self.reason,
            first_available_at=self.first_available_at,
            last_available_at=self.last_available_at,
            lost_count=self.lost_count,
        )


class BoundedIqfeedL1CaptureHandoff:
    """Nonblocking bridge ingress with asynchronous append-only sink delivery."""

    _WORKER_POLL_SECONDS = 0.02

    def __init__(
        self,
        *,
        sink: IqfeedL1CaptureSink,
        max_pending_events: int,
        max_pending_bytes: int,
        max_gap_keys: int,
        bridge_source_sha256: str,
        bridge_configuration: Mapping[str, Any],
        bridge_configuration_sha256: str,
    ) -> None:
        if not isinstance(sink, IqfeedL1CaptureSink):
            raise CaptureContractError("IQFeed capture sink is malformed")
        if sink.network_fallback_allowed:
            raise CaptureContractError("IQFeed capture sink permits network fallback")
        pending = _positive_int(max_pending_events, "IQFeed capture queue limit")
        pending_bytes = _positive_int(
            max_pending_bytes, "IQFeed capture queue byte limit"
        )
        gaps = _positive_int(max_gap_keys, "IQFeed capture gap-key limit")
        if pending > int(sink.capture_queue_event_limit):
            raise CaptureContractError(
                "IQFeed capture queue exceeds the measured resource binding"
            )
        if pending_bytes > int(sink.capture_queue_byte_limit):
            raise CaptureContractError(
                "IQFeed capture queue bytes exceed the measured resource binding"
            )
        if gaps > int(sink.capture_gap_key_limit):
            raise CaptureContractError(
                "IQFeed capture gap ledger exceeds the measured resource binding"
            )
        configuration = dict(bridge_configuration)
        if not configuration or sha256_json(configuration) != str(
            bridge_configuration_sha256 or ""
        ).strip().lower():
            raise CaptureContractError("IQFeed capture bridge configuration hash mismatch")
        source_sha256 = _require_sha256(
            bridge_source_sha256, "IQFeed capture bridge source hash"
        )
        resource_binding_sha256 = _require_sha256(
            sink.capture_resource_binding_sha256,
            "IQFeed capture resource binding hash",
        )
        self.sink = sink
        self.max_pending_events = pending
        self.max_pending_bytes = pending_bytes
        self.max_gap_keys = gaps
        self.bridge_source_sha256 = source_sha256
        self.bridge_configuration = configuration
        self.bridge_configuration_sha256 = str(
            bridge_configuration_sha256
        ).strip().lower()
        self.capture_resource_binding_sha256 = resource_binding_sha256
        self.handoff_configuration = {
            "schema_version": "chili.iqfeed-l1-capture-handoff-config.v2",
            "max_pending_events": self.max_pending_events,
            "max_pending_bytes": self.max_pending_bytes,
            "max_gap_keys": self.max_gap_keys,
            "capture_resource_binding_sha256": resource_binding_sha256,
        }
        self.handoff_configuration_sha256 = sha256_json(
            self.handoff_configuration
        )
        self._queue: queue.Queue[tuple[IqfeedL1CaptureEnvelope, int]] = queue.Queue(
            maxsize=self.max_pending_events
        )
        self._condition = threading.Condition(threading.RLock())
        self._pending_gaps: OrderedDict[
            tuple[CaptureStream, str | None, str], _GapAccumulator
        ] = OrderedDict()
        self._overflow_gap: _GapAccumulator | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._accepting = False
        self._terminal_error: BaseException | None = None
        self._unpersisted_gap_count = 0
        self._offered = 0
        self._submitted = 0
        self._queue_overflow_lost = 0
        self._queue_overflow_incidents = 0
        self._pending_bytes = 0
        self._peak_pending_bytes = 0
        self._byte_overflow_lost = 0
        self._byte_overflow_incidents = 0
        self._oversized_envelope_lost = 0
        self._reported_gap_count = 0
        self._submit_failures = 0
        self._active_producer_generation: (
            CaptureExternalProducerGeneration | None
        ) = None

    @property
    def network_fallback_allowed(self) -> bool:
        return False

    def start(self) -> None:
        with self._condition:
            if self._started:
                raise CaptureContractError("IQFeed capture handoff is one-shot")
            self._started = True
            self._accepting = True
        thread = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="capture-iqfeed-l1-handoff",
        )
        self._thread = thread
        thread.start()

    def _latch_gap(self, gap: CoverageGap) -> None:
        key = (gap.stream, gap.symbol, gap.reason)
        with self._condition:
            existing = self._pending_gaps.get(key)
            if existing is not None:
                existing.add(gap)
                self._condition.notify_all()
                return
            if len(self._pending_gaps) < self.max_gap_keys:
                self._pending_gaps[key] = _GapAccumulator(
                    stream=gap.stream,
                    symbol=gap.symbol,
                    reason=gap.reason,
                    first_available_at=gap.first_available_at,
                    last_available_at=gap.last_available_at,
                    lost_count=gap.lost_count,
                )
            elif self._overflow_gap is None:
                self._overflow_gap = _GapAccumulator(
                    stream=gap.stream,
                    symbol=None,
                    reason="iqfeed_l1_capture_gap_ledger_key_overflow",
                    first_available_at=gap.first_available_at,
                    last_available_at=gap.last_available_at,
                    lost_count=gap.lost_count,
                )
            else:
                self._overflow_gap.add(
                    CoverageGap(
                        stream=self._overflow_gap.stream,
                        symbol=None,
                        reason=self._overflow_gap.reason,
                        first_available_at=gap.first_available_at,
                        last_available_at=gap.last_available_at,
                        lost_count=gap.lost_count,
                    )
                )
            self._condition.notify_all()

    def offer(self, envelope: IqfeedL1CaptureEnvelope) -> bool:
        if not isinstance(envelope, IqfeedL1CaptureEnvelope):
            raise CaptureContractError("IQFeed capture offer is malformed")
        envelope_size = envelope.canonical_size_bytes
        provenance = envelope.payload.get(IQFEED_L1_SOURCE_PROVENANCE_FIELD)
        binding_matches = bool(
            isinstance(provenance, Mapping)
            and provenance.get("bridge_source_sha256")
            == self.bridge_source_sha256
            and provenance.get("bridge_configuration_sha256")
            == self.bridge_configuration_sha256
            and provenance.get("capture_resource_binding_sha256")
            == self.capture_resource_binding_sha256
            and provenance.get("handoff_configuration_sha256")
            == self.handoff_configuration_sha256
        )
        with self._condition:
            accepting = self._accepting and self._terminal_error is None
            self._offered += 1
            if not binding_matches:
                reason = "iqfeed_l1_capture_envelope_binding_mismatch"
            elif not accepting:
                reason = "iqfeed_l1_capture_handoff_not_accepting"
            elif envelope_size > self.max_pending_bytes:
                self._byte_overflow_lost += 1
                self._byte_overflow_incidents += 1
                self._oversized_envelope_lost += 1
                reason = "iqfeed_l1_capture_event_exceeds_byte_budget"
            elif self._pending_bytes + envelope_size > self.max_pending_bytes:
                self._byte_overflow_lost += 1
                self._byte_overflow_incidents += 1
                reason = "iqfeed_l1_capture_queue_byte_overflow"
            else:
                try:
                    self._queue.put_nowait((envelope, envelope_size))
                except queue.Full:
                    self._queue_overflow_lost += 1
                    self._queue_overflow_incidents += 1
                    reason = "iqfeed_l1_capture_queue_overflow"
                else:
                    self._pending_bytes += envelope_size
                    self._peak_pending_bytes = max(
                        self._peak_pending_bytes, self._pending_bytes
                    )
                    self._condition.notify_all()
                    return True
            self._latch_gap(
                CoverageGap(
                    stream=envelope.stream,
                    symbol=envelope.symbol,
                    reason=reason,
                    first_available_at=envelope.clocks.available_at,
                    last_available_at=envelope.clocks.available_at,
                    lost_count=1,
                )
            )
            return False

    @staticmethod
    def _row_order(
        stream: CaptureStream, row: Mapping[str, Any]
    ) -> tuple[int, int, int, str]:
        def _safe_int(value: Any) -> int:
            if isinstance(value, bool):
                return 0
            try:
                return int(value or 0)
            except (TypeError, ValueError, OverflowError):
                return 0

        generation = _safe_int(row.get("connection_generation"))
        frame_sequence = _safe_int(row.get("source_frame_sequence"))
        # A selected-fields Q frame can yield both one exact print and one
        # quote/reference row.  They share the same source-frame sequence and
        # availability boundary, so order the exact vendor-clocked row first.
        # That row can establish producer generation authority without
        # discarding the quote derived from the very same frame.
        stream_order = 0 if stream is CaptureStream.IQFEED_PRINT else 1
        return (
            generation,
            frame_sequence,
            stream_order,
            str(row.get("sym") or "").strip().upper(),
        )

    def offer_released_rows(
        self,
        *,
        trade_rows: Sequence[Mapping[str, Any]],
        quote_rows: Sequence[Mapping[str, Any]],
        available_at: datetime,
    ) -> tuple[int, int]:
        released_at = _utc(available_at, "IQFeed capture release boundary")
        rows = [
            *((CaptureStream.IQFEED_PRINT, row) for row in trade_rows),
            *((CaptureStream.NBBO_QUOTE, row) for row in quote_rows),
        ]
        rows.sort(key=lambda item: self._row_order(item[0], item[1]))
        accepted = 0
        rejected = 0
        for stream, row in rows:
            try:
                envelope = IqfeedL1CaptureEnvelope.from_released_row(
                    row,
                    stream=stream,
                    available_at=released_at,
                    bridge_source_sha256=self.bridge_source_sha256,
                    bridge_configuration=self.bridge_configuration,
                    bridge_configuration_sha256=(
                        self.bridge_configuration_sha256
                    ),
                    capture_resource_binding_sha256=(
                        self.capture_resource_binding_sha256
                    ),
                    handoff_configuration=self.handoff_configuration,
                    handoff_configuration_sha256=(
                        self.handoff_configuration_sha256
                    ),
                )
            except BaseException:
                rejected += 1
                self._latch_gap(
                    CoverageGap(
                        stream=stream,
                        symbol=str(row.get("sym") or "").strip().upper() or None,
                        reason="iqfeed_l1_capture_envelope_invalid",
                        first_available_at=released_at,
                        last_available_at=released_at,
                        lost_count=1,
                    )
                )
                continue
            if self.offer(envelope):
                accepted += 1
            else:
                rejected += 1
        return accepted, rejected

    def record_release_failure(
        self,
        *,
        trade_rows: Sequence[Mapping[str, Any]],
        quote_rows: Sequence[Mapping[str, Any]],
        available_at: datetime,
    ) -> int:
        """Persist loss when an unexpected batch-handoff defect is contained."""

        released_at = _utc(available_at, "IQFeed failed release boundary")
        lost = 0
        for stream, rows in (
            (CaptureStream.IQFEED_PRINT, trade_rows),
            (CaptureStream.NBBO_QUOTE, quote_rows),
        ):
            for row in rows:
                lost += 1
                self._latch_gap(
                    CoverageGap(
                        stream=stream,
                        symbol=str(row.get("sym") or "").strip().upper() or None,
                        reason="iqfeed_l1_capture_release_handoff_failed",
                        first_available_at=released_at,
                        last_available_at=released_at,
                        lost_count=1,
                    )
                )
        return lost

    def record_source_frame_failure(
        self,
        *,
        streams: Sequence[CaptureStream],
        symbol: str | None,
        available_at: datetime,
        reason: str,
    ) -> int:
        """Record a socket frame which could not become a typed released row."""

        at = _utc(available_at, "IQFeed failed source-frame boundary")
        normalized = str(symbol or "").strip().upper() or None
        reason_code = str(reason or "").strip()
        if not reason_code:
            raise CaptureContractError("IQFeed source-frame failure reason is missing")
        resolved_streams: list[CaptureStream] = []
        for stream in streams:
            try:
                resolved = (
                    stream
                    if isinstance(stream, CaptureStream)
                    else CaptureStream(str(stream))
                )
            except ValueError as exc:
                raise CaptureContractError(
                    "IQFeed source-frame failure stream is unknown"
                ) from exc
            if resolved not in _ALLOWED_STREAMS or resolved in resolved_streams:
                raise CaptureContractError(
                    "IQFeed source-frame failure streams are malformed"
                )
            resolved_streams.append(resolved)
        if not resolved_streams:
            raise CaptureContractError(
                "IQFeed source-frame failure requires at least one stream"
            )
        for stream in resolved_streams:
            self._latch_gap(
                CoverageGap(
                    stream=stream,
                    symbol=normalized,
                    reason=reason_code,
                    first_available_at=at,
                    last_available_at=at,
                    lost_count=1,
                )
            )
        return len(resolved_streams)

    def record_connection_boundary(
        self,
        *,
        at: datetime,
        bridge_run_id: str,
        connection_generation: int,
        active: bool,
    ) -> CaptureExternalProducerGeneration | None:
        """Open/close the exact bridge generation eligible for a future roster."""

        boundary = _utc(at, "IQFeed L1 connection boundary")
        if not isinstance(active, bool):
            raise CaptureContractError("IQFeed L1 connection state is malformed")
        evidence = (
            CaptureExternalProducerGeneration(
                producer_id="iqfeed_l1",
                provider="iqfeed",
                provider_instance_id=bridge_run_id,
                provider_generation=connection_generation,
                streams=(CaptureStream.IQFEED_PRINT, CaptureStream.NBBO_QUOTE),
                bridge_source_sha256=self.bridge_source_sha256,
                bridge_configuration_sha256=self.bridge_configuration_sha256,
                capture_resource_binding_sha256=(
                    self.capture_resource_binding_sha256
                ),
                handoff_configuration_sha256=(
                    self.handoff_configuration_sha256
                ),
                observed_at=boundary,
            )
            if active
            else None
        )
        with self._condition:
            current = self._active_producer_generation
            if active:
                if current is not None and current != evidence:
                    raise CaptureContractError(
                        "IQFeed L1 producer generation changed without close"
                    )
                self._active_producer_generation = evidence
                self._condition.notify_all()
                return evidence
            if (
                current is None
                or current.provider_instance_id != str(bridge_run_id).strip().lower()
                or current.provider_generation != int(connection_generation)
            ):
                raise CaptureContractError(
                    "IQFeed L1 connection close does not match active generation"
                )
            self._active_producer_generation = None
            self._condition.notify_all()
        for stream in (CaptureStream.IQFEED_PRINT, CaptureStream.NBBO_QUOTE):
            self._latch_gap(
                CoverageGap(
                    stream=stream,
                    symbol=None,
                    reason="iqfeed_l1_connection_boundary",
                    first_available_at=boundary,
                    last_available_at=boundary,
                    lost_count=1,
                )
            )
        return None

    def active_producer_generation(
        self,
    ) -> CaptureExternalProducerGeneration | None:
        with self._condition:
            return self._active_producer_generation

    def _take_gaps(self) -> tuple[CoverageGap, ...]:
        with self._condition:
            gaps = tuple(row.freeze() for row in self._pending_gaps.values())
            self._pending_gaps.clear()
            if self._overflow_gap is not None:
                gaps = (*gaps, self._overflow_gap.freeze())
                self._overflow_gap = None
            return gaps

    def _flush_gaps(self) -> bool:
        gaps = self._take_gaps()
        for index, gap in enumerate(gaps):
            try:
                self.sink.report_gap(gap)
            except BaseException as exc:
                remaining = sum(row.lost_count for row in gaps[index:])
                with self._condition:
                    self._terminal_error = exc
                    self._accepting = False
                    self._unpersisted_gap_count += remaining
                    self._condition.notify_all()
                return False
            with self._condition:
                self._reported_gap_count += gap.lost_count
        return True

    def _worker_loop(self) -> None:
        while True:
            if not self._flush_gaps():
                return
            if self._stop.is_set() and self._queue.empty():
                if self._flush_gaps():
                    return
                return
            try:
                envelope, envelope_size = self._queue.get(
                    timeout=self._WORKER_POLL_SECONDS
                )
            except queue.Empty:
                continue
            try:
                try:
                    self.sink.submit_envelope(envelope)
                except _CaptureSubmissionRejected as exc:
                    with self._condition:
                        self._submit_failures += 1
                    if not exc.coverage_gap_recorded:
                        self._latch_gap(
                            CoverageGap(
                                stream=envelope.stream,
                                symbol=envelope.symbol,
                                reason="iqfeed_l1_capture_sink_submit_failed",
                                first_available_at=envelope.clocks.available_at,
                                last_available_at=envelope.clocks.available_at,
                                lost_count=1,
                            )
                        )
                except Exception:
                    with self._condition:
                        self._submit_failures += 1
                    self._latch_gap(
                        CoverageGap(
                            stream=envelope.stream,
                            symbol=envelope.symbol,
                            reason="iqfeed_l1_capture_sink_submit_failed",
                            first_available_at=envelope.clocks.available_at,
                            last_available_at=envelope.clocks.available_at,
                            lost_count=1,
                        )
                    )
                else:
                    with self._condition:
                        self._submitted += 1
            finally:
                self._queue.task_done()
                with self._condition:
                    self._pending_bytes -= envelope_size
                    if self._pending_bytes < 0:  # pragma: no cover - lock invariant
                        self._pending_bytes = 0
                        self._terminal_error = CaptureContractError(
                            "IQFeed capture byte reservation underflow"
                        )
                        self._accepting = False
                    self._condition.notify_all()

    def wait_until_idle(self, timeout_seconds: float) -> bool:
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise CaptureContractError("IQFeed capture idle timeout must be positive")
        deadline = time.monotonic() + timeout
        with self._condition:
            while time.monotonic() < deadline:
                if (
                    self._queue.unfinished_tasks == 0
                    and not self._pending_gaps
                    and self._overflow_gap is None
                ):
                    return True
                self._condition.wait(timeout=min(0.02, deadline - time.monotonic()))
            return False

    def close(self, timeout_seconds: float = 5.0) -> Mapping[str, Any]:
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise CaptureContractError("IQFeed capture close timeout must be positive")
        with self._condition:
            if not self._started:
                raise CaptureContractError("IQFeed capture handoff was never started")
            self._accepting = False
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise CaptureContractError(
                    "IQFeed capture handoff did not drain before close"
                )
        health = self.health()
        if health["terminal_error"] is not None or health["unpersisted_gap_count"]:
            raise CaptureContractError(
                "IQFeed capture handoff closed with unpersisted coverage loss"
            )
        return health

    def health(self) -> Mapping[str, Any]:
        with self._condition:
            return {
                "network_fallback_allowed": False,
                "started": self._started,
                "accepting": self._accepting,
                "queue_depth": self._queue.qsize(),
                "unfinished_tasks": self._queue.unfinished_tasks,
                "max_pending_events": self.max_pending_events,
                "max_pending_bytes": self.max_pending_bytes,
                "pending_bytes": self._pending_bytes,
                "peak_pending_bytes": self._peak_pending_bytes,
                "max_gap_keys": self.max_gap_keys,
                "offered": self._offered,
                "submitted": self._submitted,
                "queue_overflow_lost": self._queue_overflow_lost,
                "queue_overflow_incidents": self._queue_overflow_incidents,
                "byte_overflow_lost": self._byte_overflow_lost,
                "byte_overflow_incidents": self._byte_overflow_incidents,
                "oversized_envelope_lost": self._oversized_envelope_lost,
                "reported_gap_count": self._reported_gap_count,
                "submit_failures": self._submit_failures,
                "pending_gap_keys": len(self._pending_gaps),
                "gap_ledger_overflow": self._overflow_gap is not None,
                "unpersisted_gap_count": self._unpersisted_gap_count,
                "terminal_error": (
                    None
                    if self._terminal_error is None
                    else type(self._terminal_error).__name__
                ),
                "bridge_source_sha256": self.bridge_source_sha256,
                "bridge_configuration_sha256": (
                    self.bridge_configuration_sha256
                ),
                "capture_resource_binding_sha256": (
                    self.capture_resource_binding_sha256
                ),
                "handoff_configuration_sha256": (
                    self.handoff_configuration_sha256
                ),
                "active_producer_generation": (
                    None
                    if self._active_producer_generation is None
                    else self._active_producer_generation.to_dict()
                ),
                "active_producer_generation_sha256": (
                    None
                    if self._active_producer_generation is None
                    else self._active_producer_generation.evidence_sha256
                ),
            }


__all__ = [
    "BoundedIqfeedL1CaptureHandoff",
    "IQFEED_L1_CAPTURE_ENVELOPE_SCHEMA_VERSION",
    "IqfeedL1CaptureEnvelope",
    "IqfeedL1CaptureSink",
    "IqfeedL1ProcessCaptureSink",
]
