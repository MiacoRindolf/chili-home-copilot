"""Hot-only, bounded IQFeed L2 delta/checkpoint capture handoff.

The operational IQFeed depth bridge runs on the Windows host.  This module is
its no-fetch capture boundary: the bridge may offer already-decoded type-6
frames and local book checkpoints, but this code never opens a provider socket,
queries a database, or enables an order path.

Unlike IQFeed L1 Q frames, the observed equity type-6 depth format includes an
ET date and microsecond time.  Deltas preserve that exact provider event clock.
Locally assembled checkpoints deliberately carry no single provider event
clock; each level keeps its own event clock and the latest one is only a market
reference.  The legacy stream exposes no proven initial-snapshot completion or
provider watermark, so every v1 checkpoint is explicitly incomplete and cannot
certify replay coverage.
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
    CaptureClocks,
    CaptureContractError,
    CaptureExternalProducerGeneration,
    CaptureStream,
    CoverageGap,
    IQFEED_L2_CHECKPOINT_PAYLOAD_SCHEMA_VERSION,
    IQFEED_L2_DELTA_PAYLOAD_SCHEMA_VERSION,
    IQFEED_L2_SOURCE_PROVENANCE_FIELD,
    IQFEED_L2_SOURCE_PROVENANCE_SCHEMA_VERSION,
    canonical_json_bytes,
    sha256_json,
    validate_iqfeed_l2_source_provenance,
)


UTC = timezone.utc
IQFEED_L2_CAPTURE_ENVELOPE_SCHEMA_VERSION = (
    "chili.capture-handoff.iqfeed-l2.v1"
)
_ALLOWED_STREAMS = frozenset(
    {CaptureStream.L2_DEPTH_DELTA, CaptureStream.L2_DEPTH_CHECKPOINT}
)
_CHECKPOINT_COMPLETION_BASIS = (
    "provider_snapshot_completion_boundary_unavailable"
)


def _utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CaptureContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value, "datetime").isoformat().replace("+00:00", "Z")


def _require_sha256(value: Any, field_name: str) -> str:
    resolved = str(value or "").strip().lower()
    if len(resolved) != 64 or any(ch not in "0123456789abcdef" for ch in resolved):
        raise CaptureContractError(f"{field_name} is malformed")
    return resolved


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CaptureContractError(f"{field_name} must be a positive integer")
    return int(value)


def _number(value: Any, field_name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CaptureContractError(f"{field_name} is malformed")
    parsed = float(value)
    if not math.isfinite(parsed) or (parsed < 0 if allow_zero else parsed <= 0):
        raise CaptureContractError(f"{field_name} is malformed")
    return parsed


def _canonical_copy(value: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        return json.loads(canonical_json_bytes(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CaptureContractError("IQFeed L2 payload is not canonical JSON") from exc


def _normalized_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol:
        raise CaptureContractError("IQFeed L2 symbol is required")
    return symbol


def _base_provenance(
    *,
    symbol: str,
    bridge_run_id: Any,
    connection_generation: Any,
    bridge_version: Any,
    bridge_source_sha256: str,
    bridge_configuration_sha256: str,
    capture_resource_binding_sha256: str,
    handoff_configuration_sha256: str,
    source_frame_sequence: Any,
    source_frame_sha256: Any,
    message_type: str,
    timestamp_basis: str,
    provider_event_at: datetime | None,
    received_at: datetime,
) -> dict[str, Any]:
    generation = _positive_int(connection_generation, "IQFeed L2 connection generation")
    sequence = _positive_int(source_frame_sequence, "IQFeed L2 source frame sequence")
    try:
        import uuid

        bridge_run = str(uuid.UUID(str(bridge_run_id or "")))
    except ValueError as exc:
        raise CaptureContractError("IQFeed L2 bridge run id is malformed") from exc
    version = str(bridge_version or "").strip()
    if not version:
        raise CaptureContractError("IQFeed L2 bridge version is missing")
    received = _utc(received_at, "IQFeed L2 received_at")
    provider = (
        None
        if provider_event_at is None
        else _utc(provider_event_at, "IQFeed L2 provider_event_at")
    )
    return {
        "schema_version": IQFEED_L2_SOURCE_PROVENANCE_SCHEMA_VERSION,
        "symbol": symbol,
        "bridge_run_id": bridge_run,
        "connection_generation": generation,
        "bridge_version": version,
        "bridge_source_sha256": _require_sha256(
            bridge_source_sha256, "IQFeed L2 bridge source hash"
        ),
        "bridge_configuration_sha256": _require_sha256(
            bridge_configuration_sha256, "IQFeed L2 bridge configuration hash"
        ),
        "capture_resource_binding_sha256": _require_sha256(
            capture_resource_binding_sha256,
            "IQFeed L2 capture resource binding hash",
        ),
        "handoff_configuration_sha256": _require_sha256(
            handoff_configuration_sha256,
            "IQFeed L2 handoff configuration hash",
        ),
        "source_frame_sequence": sequence,
        "source_frame_sha256": _require_sha256(
            source_frame_sha256, "IQFeed L2 source frame hash"
        ),
        "message_type": message_type,
        "timestamp_basis": timestamp_basis,
        "provider_event_at": None if provider is None else _iso(provider),
        "received_at": _iso(received),
    }


@dataclass(frozen=True)
class IqfeedL2CaptureEnvelope:
    stream: CaptureStream
    symbol: str
    clocks: CaptureClocks
    # The constructor accepts one mapping, but ``__post_init__`` replaces it
    # with canonical bytes.  A frozen dataclass alone would not make nested
    # dict/list values immutable and producer code must not be able to mutate
    # an already-enqueued event behind its content hash.
    _payload: Any
    schema_version: str = IQFEED_L2_CAPTURE_ENVELOPE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != IQFEED_L2_CAPTURE_ENVELOPE_SCHEMA_VERSION:
            raise CaptureContractError("IQFeed L2 capture envelope schema is unsupported")
        if self.stream not in _ALLOWED_STREAMS:
            raise CaptureContractError("IQFeed L2 capture envelope stream is invalid")
        symbol = _normalized_symbol(self.symbol)
        if not isinstance(self.clocks, CaptureClocks):
            raise CaptureContractError("IQFeed L2 capture clocks are malformed")
        if not isinstance(self._payload, Mapping):
            raise CaptureContractError("IQFeed L2 capture payload is malformed")
        payload = _canonical_copy(self._payload)
        if payload.get("symbol") != symbol:
            raise CaptureContractError("IQFeed L2 envelope payload/symbol mismatch")
        validate_iqfeed_l2_source_provenance(
            payload.get(IQFEED_L2_SOURCE_PROVENANCE_FIELD),
            symbol=symbol,
            clocks=self.clocks,
            stream=self.stream,
        )
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "_payload", canonical_json_bytes(payload))

    @property
    def payload(self) -> Mapping[str, Any]:
        try:
            payload = json.loads(self._payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:  # pragma: no cover
            raise CaptureContractError(
                "IQFeed L2 immutable payload bytes are malformed"
            ) from exc
        if not isinstance(payload, Mapping):  # pragma: no cover - constructor invariant
            raise CaptureContractError("IQFeed L2 immutable payload is malformed")
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "stream": self.stream.value,
            "symbol": self.symbol,
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
    def from_delta_row(
        cls,
        row: Mapping[str, Any],
        *,
        available_at: datetime,
        bridge_source_sha256: str,
        bridge_configuration_sha256: str,
        capture_resource_binding_sha256: str,
        handoff_configuration_sha256: str,
    ) -> "IqfeedL2CaptureEnvelope":
        if not isinstance(row, Mapping):
            raise CaptureContractError("IQFeed L2 delta row is malformed")
        symbol = _normalized_symbol(row.get("sym"))
        received = _utc(row.get("received_at"), "IQFeed L2 delta received_at")
        available = _utc(available_at, "IQFeed L2 delta available_at")
        provider = _utc(row.get("provider_at"), "IQFeed L2 delta provider_at")
        clocks = CaptureClocks(
            provider_event_at=provider,
            market_reference_at=None,
            received_at=received,
            available_at=available,
        )
        venue = str(row.get("venue") or "").strip().upper()
        side = str(row.get("side") or "").strip().upper()
        condition = str(row.get("condition_code") or "").strip()
        if not venue or len(venue) > 16 or side not in {"A", "B"} or not condition:
            raise CaptureContractError("IQFeed L2 delta identity is malformed")
        provenance = _base_provenance(
            symbol=symbol,
            bridge_run_id=row.get("bridge_run_id"),
            connection_generation=row.get("connection_generation"),
            bridge_version=row.get("bridge"),
            bridge_source_sha256=bridge_source_sha256,
            bridge_configuration_sha256=bridge_configuration_sha256,
            capture_resource_binding_sha256=capture_resource_binding_sha256,
            handoff_configuration_sha256=handoff_configuration_sha256,
            source_frame_sequence=row.get("source_frame_sequence"),
            source_frame_sha256=row.get("source_frame_sha256"),
            message_type="6",
            timestamp_basis="iqfeed_l2_frame_date_time_et",
            provider_event_at=provider,
            received_at=received,
        )
        return cls(
            stream=CaptureStream.L2_DEPTH_DELTA,
            symbol=symbol,
            clocks=clocks,
            _payload={
                "schema_version": IQFEED_L2_DELTA_PAYLOAD_SCHEMA_VERSION,
                "symbol": symbol,
                "venue": venue,
                "side": side,
                "price": _number(row.get("px"), "IQFeed L2 delta price"),
                "size": _number(
                    row.get("sz"), "IQFeed L2 delta size", allow_zero=True
                ),
                "condition_code": condition,
                IQFEED_L2_SOURCE_PROVENANCE_FIELD: provenance,
            },
        )

    @classmethod
    def from_checkpoint_row(
        cls,
        row: Mapping[str, Any],
        *,
        available_at: datetime,
        bridge_source_sha256: str,
        bridge_configuration_sha256: str,
        capture_resource_binding_sha256: str,
        handoff_configuration_sha256: str,
    ) -> "IqfeedL2CaptureEnvelope":
        if not isinstance(row, Mapping):
            raise CaptureContractError("IQFeed L2 checkpoint row is malformed")
        symbol = _normalized_symbol(row.get("sym"))
        received = _utc(row.get("received_at"), "IQFeed L2 checkpoint received_at")
        available = _utc(available_at, "IQFeed L2 checkpoint available_at")
        checkpoint_generation = _positive_int(
            row.get("connection_generation"),
            "IQFeed L2 checkpoint connection generation",
        )
        raw_levels = row.get("levels")
        if not isinstance(raw_levels, Sequence) or isinstance(
            raw_levels, (str, bytes, bytearray)
        ) or not raw_levels:
            raise CaptureContractError("IQFeed L2 checkpoint levels are malformed")
        levels: list[dict[str, Any]] = []
        identities: set[tuple[str, str]] = set()
        event_times: list[datetime] = []
        exact_complete = True
        for raw_level in raw_levels:
            if not isinstance(raw_level, Mapping):
                raise CaptureContractError("IQFeed L2 checkpoint level is malformed")
            venue = str(raw_level.get("venue") or "").strip().upper()
            side = str(raw_level.get("side") or "").strip().upper()
            condition = str(raw_level.get("condition_code") or "").strip()
            identity = (side, venue)
            if (
                not venue
                or len(venue) > 16
                or side not in {"A", "B"}
                or not condition
                or identity in identities
            ):
                raise CaptureContractError(
                    "IQFeed L2 checkpoint level identity is malformed"
                )
            identities.add(identity)
            level_generation = _positive_int(
                raw_level.get("connection_generation"),
                "IQFeed L2 checkpoint level connection generation",
            )
            if level_generation != checkpoint_generation:
                raise CaptureContractError(
                    "IQFeed L2 checkpoint contains a foreign-generation level"
                )
            provider_raw = raw_level.get("provider_at")
            provider = (
                None
                if provider_raw is None
                else _utc(provider_raw, "IQFeed L2 checkpoint level provider_at")
            )
            if provider is None:
                exact_complete = False
            else:
                event_times.append(provider)
            levels.append(
                {
                    "venue": venue,
                    "side": side,
                    "price": _number(
                        raw_level.get("px"), "IQFeed L2 checkpoint price"
                    ),
                    "size": _number(
                        raw_level.get("sz"),
                        "IQFeed L2 checkpoint size",
                        allow_zero=True,
                    ),
                    "provider_event_at": None if provider is None else _iso(provider),
                    "connection_generation": level_generation,
                    "source_frame_sequence": _positive_int(
                        raw_level.get("source_frame_sequence"),
                        "IQFeed L2 checkpoint level source sequence",
                    ),
                    "source_frame_sha256": _require_sha256(
                        raw_level.get("source_frame_sha256"),
                        "IQFeed L2 checkpoint level frame hash",
                    ),
                    "condition_code": condition,
                }
            )
        levels.sort(key=lambda value: (value["side"], value["venue"]))
        covered = _positive_int(
            row.get("covered_through_source_frame_sequence"),
            "IQFeed L2 checkpoint covered sequence",
        )
        if any(level["source_frame_sequence"] > covered for level in levels):
            raise CaptureContractError(
                "IQFeed L2 checkpoint level exceeds its covered sequence"
            )
        if row.get("initial_snapshot_complete") is not False or row.get(
            "completion_basis"
        ) != _CHECKPOINT_COMPLETION_BASIS:
            raise CaptureContractError(
                "IQFeed L2 checkpoint cannot self-attest provider snapshot completion"
            )
        reference = max(event_times) if event_times else None
        clocks = CaptureClocks(
            provider_event_at=None,
            market_reference_at=reference,
            received_at=received,
            available_at=available,
        )
        provenance = _base_provenance(
            symbol=symbol,
            bridge_run_id=row.get("bridge_run_id"),
            connection_generation=checkpoint_generation,
            bridge_version=row.get("bridge"),
            bridge_source_sha256=bridge_source_sha256,
            bridge_configuration_sha256=bridge_configuration_sha256,
            capture_resource_binding_sha256=capture_resource_binding_sha256,
            handoff_configuration_sha256=handoff_configuration_sha256,
            source_frame_sequence=covered,
            source_frame_sha256=row.get("covered_through_source_frame_sha256"),
            message_type="LOCAL_DEPTH_CHECKPOINT",
            timestamp_basis="iqfeed_l2_local_checkpoint_market_reference",
            provider_event_at=None,
            received_at=received,
        )
        return cls(
            stream=CaptureStream.L2_DEPTH_CHECKPOINT,
            symbol=symbol,
            clocks=clocks,
            _payload={
                "schema_version": IQFEED_L2_CHECKPOINT_PAYLOAD_SCHEMA_VERSION,
                "symbol": symbol,
                "levels": levels,
                "covered_through_source_frame_sequence": covered,
                "exact_level_event_clock_complete": exact_complete,
                "initial_snapshot_complete": False,
                "completion_basis": _CHECKPOINT_COMPLETION_BASIS,
                IQFEED_L2_SOURCE_PROVENANCE_FIELD: provenance,
            },
        )


@runtime_checkable
class IqfeedL2CaptureSink(Protocol):
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

    def submit_envelope(self, envelope: IqfeedL2CaptureEnvelope) -> None: ...

    def report_gap(self, gap: CoverageGap) -> None: ...


class _CaptureSubmissionRejected(CaptureContractError):
    """A sink rejected one event and says whether it already persisted loss."""

    def __init__(self, *, coverage_gap_recorded: bool, disposition: str) -> None:
        super().__init__(
            "IQFeed L2 capture service rejected input: "
            + (disposition or "unknown_disposition")
        )
        self.coverage_gap_recorded = bool(coverage_gap_recorded)
        self.disposition = disposition or "unknown_disposition"


class IqfeedL2ProcessCaptureSink:
    """No-fetch adapter into one already-created live capture process service."""

    def __init__(self, service: Any) -> None:
        required = (
            "record_hot_gap",
            "submit_hot_input",
            "capture_resource_binding_sha256",
            "capture_queue_event_limit",
            "capture_queue_byte_limit",
            "capture_gap_key_limit",
            "network_fallback_allowed",
        )
        if any(not hasattr(service, name) for name in required):
            raise CaptureContractError("IQFeed L2 capture service is malformed")
        if bool(service.network_fallback_allowed):
            raise CaptureContractError("IQFeed L2 capture service permits network fallback")
        self.service = service

    @property
    def network_fallback_allowed(self) -> bool:
        return False

    @property
    def capture_resource_binding_sha256(self) -> str:
        return _require_sha256(
            self.service.capture_resource_binding_sha256,
            "IQFeed L2 capture resource binding hash",
        )

    @property
    def capture_queue_event_limit(self) -> int:
        return _positive_int(
            self.service.capture_queue_event_limit,
            "IQFeed L2 capture queue limit",
        )

    @property
    def capture_queue_byte_limit(self) -> int:
        return _positive_int(
            self.service.capture_queue_byte_limit,
            "IQFeed L2 capture queue byte limit",
        )

    @property
    def capture_gap_key_limit(self) -> int:
        return _positive_int(
            self.service.capture_gap_key_limit,
            "IQFeed L2 capture gap-key limit",
        )

    def submit_envelope(self, envelope: IqfeedL2CaptureEnvelope) -> None:
        if not isinstance(envelope, IqfeedL2CaptureEnvelope):
            raise CaptureContractError("IQFeed L2 capture envelope is malformed")
        result = self.service.submit_hot_input(
            stream=envelope.stream,
            provider="iqfeed",
            payload=envelope.payload,
            clocks=envelope.clocks,
            symbol=envelope.symbol,
            query=None,
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
            raise CaptureContractError("IQFeed L2 capture gap is malformed")
        if gap.symbol is None:
            raise CaptureContractError("IQFeed L2 hot gap requires a symbol")
        if self.service.record_hot_gap(gap) is not True:
            raise CaptureContractError("IQFeed L2 capture gap was not persisted")


@dataclass
class _GapAccumulator:
    stream: CaptureStream
    symbol: str
    reason: str
    first_available_at: datetime
    last_available_at: datetime
    lost_count: int

    def add(self, gap: CoverageGap) -> None:
        self.first_available_at = min(self.first_available_at, gap.first_available_at)
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


class BoundedIqfeedL2CaptureHandoff:
    """Hot-only nonblocking L2 ingress with explicit gap accounting."""

    _WORKER_POLL_SECONDS = 0.02

    def __init__(
        self,
        *,
        sink: IqfeedL2CaptureSink,
        max_pending_events: int,
        max_pending_bytes: int,
        max_gap_keys: int,
        bridge_source_sha256: str,
        bridge_configuration: Mapping[str, Any],
        bridge_configuration_sha256: str,
    ) -> None:
        if not isinstance(sink, IqfeedL2CaptureSink):
            raise CaptureContractError("IQFeed L2 capture sink is malformed")
        if sink.network_fallback_allowed:
            raise CaptureContractError("IQFeed L2 capture sink permits network fallback")
        pending = _positive_int(max_pending_events, "IQFeed L2 queue limit")
        pending_bytes = _positive_int(
            max_pending_bytes, "IQFeed L2 queue byte limit"
        )
        gaps = _positive_int(max_gap_keys, "IQFeed L2 gap-key limit")
        if pending > sink.capture_queue_event_limit:
            raise CaptureContractError(
                "IQFeed L2 capture queue exceeds the measured resource binding"
            )
        if pending_bytes > sink.capture_queue_byte_limit:
            raise CaptureContractError(
                "IQFeed L2 capture queue bytes exceed the measured resource binding"
            )
        if gaps > sink.capture_gap_key_limit:
            raise CaptureContractError(
                "IQFeed L2 capture gap ledger exceeds the measured resource binding"
            )
        configuration = dict(bridge_configuration)
        if not configuration or sha256_json(configuration) != str(
            bridge_configuration_sha256 or ""
        ).strip().lower():
            raise CaptureContractError("IQFeed L2 bridge configuration hash mismatch")
        self.sink = sink
        self.max_pending_events = pending
        self.max_pending_bytes = pending_bytes
        self.max_gap_keys = gaps
        self.bridge_source_sha256 = _require_sha256(
            bridge_source_sha256, "IQFeed L2 bridge source hash"
        )
        self.bridge_configuration = configuration
        self.bridge_configuration_sha256 = _require_sha256(
            bridge_configuration_sha256, "IQFeed L2 bridge configuration hash"
        )
        self.capture_resource_binding_sha256 = _require_sha256(
            sink.capture_resource_binding_sha256,
            "IQFeed L2 capture resource binding hash",
        )
        self.handoff_configuration = {
            "schema_version": "chili.iqfeed-l2-capture-handoff-config.v2",
            "max_pending_events": pending,
            "max_pending_bytes": pending_bytes,
            "max_gap_keys": gaps,
            "capture_resource_binding_sha256": (
                self.capture_resource_binding_sha256
            ),
        }
        self.handoff_configuration_sha256 = sha256_json(
            self.handoff_configuration
        )
        self._queue: queue.Queue[tuple[IqfeedL2CaptureEnvelope, int]] = queue.Queue(
            maxsize=pending
        )
        self._condition = threading.Condition(threading.RLock())
        self._pending_gaps: OrderedDict[
            tuple[CaptureStream, str, str], _GapAccumulator
        ] = OrderedDict()
        self._gap_ledger_overflow = False
        self._requested_hot: set[str] = set()
        # ``pending`` means the checkpoint is ordered ahead of subsequent
        # deltas in the bounded queue but is not durable yet.  Only the worker
        # promotes it into ``active`` after the sink accepts that checkpoint.
        self._pending_checkpoint_generation: dict[str, int] = {}
        self._active_generation: dict[str, int] = {}
        self._last_enqueued_sequence: dict[tuple[str, int], int] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._accepting = False
        self._terminal_error: BaseException | None = None
        self._unpersisted_gap_count = 0
        self._offered_hot = 0
        self._ignored_cold = 0
        self._submitted = 0
        self._queue_overflow_lost = 0
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
                raise CaptureContractError("IQFeed L2 capture handoff is one-shot")
            self._started = True
            self._accepting = True
        thread = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="capture-iqfeed-l2-handoff",
        )
        self._thread = thread
        thread.start()

    def _latch_gap(self, gap: CoverageGap) -> None:
        symbol = _normalized_symbol(gap.symbol)
        key = (gap.stream, symbol, gap.reason)
        with self._condition:
            existing = self._pending_gaps.get(key)
            if existing is not None:
                existing.add(gap)
            elif len(self._pending_gaps) < self.max_gap_keys:
                self._pending_gaps[key] = _GapAccumulator(
                    stream=gap.stream,
                    symbol=symbol,
                    reason=gap.reason,
                    first_available_at=gap.first_available_at,
                    last_available_at=gap.last_available_at,
                    lost_count=gap.lost_count,
                )
            else:
                # Coalescing this loss into some other symbol would falsify
                # coverage.  Stop accepting and expose an unpersisted terminal
                # loss instead; the run can no longer seal or certify.
                self._gap_ledger_overflow = True
                self._accepting = False
                self._pending_checkpoint_generation.clear()
                self._active_generation.clear()
                self._last_enqueued_sequence.clear()
                self._unpersisted_gap_count += gap.lost_count
                if self._terminal_error is None:
                    self._terminal_error = CaptureContractError(
                        "IQFeed L2 gap ledger exhausted exact symbol keys"
                    )
                self._condition.notify_all()
                return
            self._condition.notify_all()

    def _offer_envelope(self, envelope: IqfeedL2CaptureEnvelope) -> bool:
        envelope_size = envelope.canonical_size_bytes
        provenance = envelope.payload.get(IQFEED_L2_SOURCE_PROVENANCE_FIELD)
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
        # Keep state validation and nonblocking enqueue under the same reentrant
        # lock.  This is what guarantees a checkpoint cannot be overtaken by a
        # delta that observes its pending generation on another reader thread.
        with self._condition:
            accepting = self._accepting and self._terminal_error is None
            self._offered_hot += 1
            if not binding_matches:
                self._latch_gap(
                    CoverageGap(
                        stream=envelope.stream,
                        symbol=envelope.symbol,
                        reason="iqfeed_l2_capture_envelope_binding_mismatch",
                        first_available_at=envelope.clocks.available_at,
                        last_available_at=envelope.clocks.available_at,
                        lost_count=1,
                    )
                )
                return False
            if not accepting:
                self._latch_gap(
                    CoverageGap(
                        stream=envelope.stream,
                        symbol=envelope.symbol,
                        reason="iqfeed_l2_capture_handoff_not_accepting",
                        first_available_at=envelope.clocks.available_at,
                        last_available_at=envelope.clocks.available_at,
                        lost_count=1,
                    )
                )
                return False
            if envelope_size > self.max_pending_bytes:
                self._byte_overflow_lost += 1
                self._byte_overflow_incidents += 1
                self._oversized_envelope_lost += 1
                reason = "iqfeed_l2_capture_event_exceeds_byte_budget"
            elif self._pending_bytes + envelope_size > self.max_pending_bytes:
                self._byte_overflow_lost += 1
                self._byte_overflow_incidents += 1
                reason = "iqfeed_l2_capture_queue_byte_overflow"
            else:
                try:
                    self._queue.put_nowait((envelope, envelope_size))
                except queue.Full:
                    self._queue_overflow_lost += 1
                    reason = "iqfeed_l2_capture_queue_overflow"
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

    def _fence_generation(self, symbol: str, generation: int | None) -> None:
        """Prevent any later delta from continuing across known L2 loss."""

        with self._condition:
            if generation is None or self._pending_checkpoint_generation.get(
                symbol
            ) == generation:
                self._pending_checkpoint_generation.pop(symbol, None)
            if generation is None or self._active_generation.get(symbol) == generation:
                self._active_generation.pop(symbol, None)
            if generation is None:
                stale_keys = [key for key in self._last_enqueued_sequence if key[0] == symbol]
            else:
                stale_keys = [(symbol, generation)]
            for key in stale_keys:
                self._last_enqueued_sequence.pop(key, None)
            self._condition.notify_all()

    def _latch_unattributed_hot_loss(
        self,
        *,
        stream: CaptureStream,
        reason: str,
        at: datetime,
    ) -> int:
        """Conservatively gap every requested hot symbol for an unscoped frame."""

        with self._condition:
            symbols = tuple(sorted(self._requested_hot))
        for symbol in symbols:
            self._fence_generation(symbol, None)
            self._latch_gap(
                CoverageGap(
                    stream=stream,
                    symbol=symbol,
                    reason=reason,
                    first_available_at=at,
                    last_available_at=at,
                    lost_count=1,
                )
            )
        return len(symbols)

    def activate_hot_symbol(
        self,
        checkpoint_row: Mapping[str, Any],
        *,
        available_at: datetime,
    ) -> bool:
        if not isinstance(checkpoint_row, Mapping):
            at = _utc(available_at, "IQFeed L2 checkpoint activation time")
            self._latch_unattributed_hot_loss(
                stream=CaptureStream.L2_DEPTH_CHECKPOINT,
                reason="iqfeed_l2_capture_checkpoint_unattributed_invalid",
                at=at,
            )
            return False
        try:
            symbol = _normalized_symbol(checkpoint_row.get("sym"))
        except CaptureContractError:
            at = _utc(available_at, "IQFeed L2 checkpoint activation time")
            self._latch_unattributed_hot_loss(
                stream=CaptureStream.L2_DEPTH_CHECKPOINT,
                reason="iqfeed_l2_capture_checkpoint_unattributed_invalid",
                at=at,
            )
            return False
        with self._condition:
            self._requested_hot.add(symbol)
            self._pending_checkpoint_generation.pop(symbol, None)
            self._active_generation.pop(symbol, None)
            for key in tuple(self._last_enqueued_sequence):
                if key[0] == symbol:
                    self._last_enqueued_sequence.pop(key, None)
        try:
            envelope = IqfeedL2CaptureEnvelope.from_checkpoint_row(
                checkpoint_row,
                available_at=available_at,
                bridge_source_sha256=self.bridge_source_sha256,
                bridge_configuration_sha256=self.bridge_configuration_sha256,
                capture_resource_binding_sha256=(
                    self.capture_resource_binding_sha256
                ),
                handoff_configuration_sha256=(
                    self.handoff_configuration_sha256
                ),
            )
        except Exception:
            at = _utc(available_at, "IQFeed L2 checkpoint activation time")
            self._latch_gap(
                CoverageGap(
                    stream=CaptureStream.L2_DEPTH_CHECKPOINT,
                    symbol=symbol,
                    reason="iqfeed_l2_capture_checkpoint_invalid",
                    first_available_at=at,
                    last_available_at=at,
                    lost_count=1,
                )
            )
            return False
        generation = envelope.payload[IQFEED_L2_SOURCE_PROVENANCE_FIELD][
            "connection_generation"
        ]
        covered = int(
            envelope.payload["covered_through_source_frame_sequence"]
        )
        with self._condition:
            # Publish pending state and enqueue the checkpoint atomically.  The
            # worker, not the producer, owns the later durable activation.
            self._pending_checkpoint_generation[symbol] = int(generation)
            self._last_enqueued_sequence[(symbol, int(generation))] = covered
            if not self._offer_envelope(envelope):
                if self._pending_checkpoint_generation.get(symbol) == generation:
                    self._pending_checkpoint_generation.pop(symbol, None)
                self._last_enqueued_sequence.pop((symbol, int(generation)), None)
                return False
        return True

    def offer_delta_rows(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        available_at: datetime,
    ) -> tuple[int, int, int]:
        at = _utc(available_at, "IQFeed L2 delta release boundary")
        if not isinstance(rows, Sequence) or isinstance(
            rows, (str, bytes, bytearray)
        ):
            lost = self._latch_unattributed_hot_loss(
                stream=CaptureStream.L2_DEPTH_DELTA,
                reason="iqfeed_l2_capture_delta_batch_unattributed_invalid",
                at=at,
            )
            return (0, max(1, lost), 0)
        accepted = 0
        rejected = 0
        ignored = 0
        # Preserve the exact reader release order.  Sorting source frames here
        # can create a history that never existed and used to throw before a
        # malformed row could be represented as coverage loss.
        for row in rows:
            if not isinstance(row, Mapping):
                rejected += max(
                    1,
                    self._latch_unattributed_hot_loss(
                        stream=CaptureStream.L2_DEPTH_DELTA,
                        reason="iqfeed_l2_capture_delta_unattributed_invalid",
                        at=at,
                    ),
                )
                continue
            try:
                symbol = _normalized_symbol(row.get("sym"))
            except CaptureContractError:
                rejected += max(
                    1,
                    self._latch_unattributed_hot_loss(
                        stream=CaptureStream.L2_DEPTH_DELTA,
                        reason="iqfeed_l2_capture_delta_unattributed_invalid",
                        at=at,
                    ),
                )
                continue
            with self._condition:
                requested = symbol in self._requested_hot
            if not requested:
                with self._condition:
                    self._ignored_cold += 1
                ignored += 1
                continue
            generation_raw = row.get("connection_generation")
            generation = (
                generation_raw
                if isinstance(generation_raw, int)
                and not isinstance(generation_raw, bool)
                else None
            )
            try:
                envelope = IqfeedL2CaptureEnvelope.from_delta_row(
                    row,
                    available_at=at,
                    bridge_source_sha256=self.bridge_source_sha256,
                    bridge_configuration_sha256=self.bridge_configuration_sha256,
                    capture_resource_binding_sha256=(
                        self.capture_resource_binding_sha256
                    ),
                    handoff_configuration_sha256=(
                        self.handoff_configuration_sha256
                    ),
                )
            except Exception:
                self._fence_generation(symbol, generation)
                rejected += 1
                self._latch_gap(
                    CoverageGap(
                        stream=CaptureStream.L2_DEPTH_DELTA,
                        symbol=symbol,
                        reason="iqfeed_l2_capture_delta_invalid",
                        first_available_at=at,
                        last_available_at=at,
                        lost_count=1,
                    )
                )
                continue
            sequence = envelope.payload[IQFEED_L2_SOURCE_PROVENANCE_FIELD][
                "source_frame_sequence"
            ]
            with self._condition:
                active_generation = self._active_generation.get(symbol)
                pending_generation = self._pending_checkpoint_generation.get(symbol)
                expected_generation = (
                    active_generation
                    if active_generation is not None
                    else pending_generation
                )
                if expected_generation is None:
                    rejected += 1
                    self._latch_gap(
                        CoverageGap(
                            stream=CaptureStream.L2_DEPTH_DELTA,
                            symbol=symbol,
                            reason="iqfeed_l2_capture_checkpoint_required",
                            first_available_at=at,
                            last_available_at=at,
                            lost_count=1,
                        )
                    )
                    continue
                if generation != expected_generation:
                    # A newer/unknown generation invalidates the local book.
                    # A stale frame must not tear down an already-newer book.
                    if generation is None or generation > expected_generation:
                        self._fence_generation(symbol, expected_generation)
                    rejected += 1
                    self._latch_gap(
                        CoverageGap(
                            stream=CaptureStream.L2_DEPTH_DELTA,
                            symbol=symbol,
                            reason="iqfeed_l2_capture_generation_changed",
                            first_available_at=at,
                            last_available_at=at,
                            lost_count=1,
                        )
                    )
                    continue
                prior_sequence = self._last_enqueued_sequence.get(
                    (symbol, expected_generation)
                )
                if prior_sequence is None or int(sequence) <= prior_sequence:
                    self._fence_generation(symbol, expected_generation)
                    rejected += 1
                    self._latch_gap(
                        CoverageGap(
                            stream=CaptureStream.L2_DEPTH_DELTA,
                            symbol=symbol,
                            reason="iqfeed_l2_capture_sequence_not_monotonic",
                            first_available_at=at,
                            last_available_at=at,
                            lost_count=1,
                        )
                    )
                    continue
                if self._offer_envelope(envelope):
                    self._last_enqueued_sequence[
                        (symbol, expected_generation)
                    ] = int(sequence)
                    accepted += 1
                else:
                    # Any lost delta breaks the book until a fresh checkpoint.
                    self._fence_generation(symbol, expected_generation)
                    rejected += 1
        return accepted, rejected, ignored

    def deactivate_hot_symbol(self, symbol: str) -> bool:
        normalized = _normalized_symbol(symbol)
        with self._condition:
            existed = normalized in self._requested_hot
            self._requested_hot.discard(normalized)
            self._pending_checkpoint_generation.pop(normalized, None)
            self._active_generation.pop(normalized, None)
            for key in tuple(self._last_enqueued_sequence):
                if key[0] == normalized:
                    self._last_enqueued_sequence.pop(key, None)
            return existed

    def record_connection_boundary(
        self,
        *,
        at: datetime,
        bridge_run_id: str,
        connection_generation: int,
        active: bool,
    ) -> tuple[str, ...]:
        boundary = _utc(at, "IQFeed L2 connection boundary")
        generation = _positive_int(
            connection_generation, "IQFeed L2 connection generation"
        )
        if not isinstance(active, bool):
            raise CaptureContractError("IQFeed L2 connection state is malformed")
        evidence = (
            CaptureExternalProducerGeneration(
                producer_id="iqfeed_l2",
                provider="iqfeed",
                provider_instance_id=bridge_run_id,
                provider_generation=generation,
                streams=(
                    CaptureStream.L2_DEPTH_DELTA,
                    CaptureStream.L2_DEPTH_CHECKPOINT,
                ),
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
                        "IQFeed L2 producer generation changed without close"
                    )
                self._active_producer_generation = evidence
            else:
                if (
                    current is None
                    or current.provider_instance_id
                    != str(bridge_run_id).strip().lower()
                    or current.provider_generation != generation
                ):
                    raise CaptureContractError(
                        "IQFeed L2 connection close does not match active generation"
                    )
                self._active_producer_generation = None
            symbols = tuple(sorted(self._requested_hot))
            self._pending_checkpoint_generation.clear()
            self._active_generation.clear()
            self._last_enqueued_sequence.clear()
        for symbol in symbols:
            self._latch_gap(
                CoverageGap(
                    stream=CaptureStream.L2_DEPTH_DELTA,
                    symbol=symbol,
                    reason="iqfeed_l2_connection_boundary_requires_checkpoint",
                    first_available_at=boundary,
                    last_available_at=boundary,
                    lost_count=1,
                )
            )
            self._latch_gap(
                CoverageGap(
                    stream=CaptureStream.L2_DEPTH_CHECKPOINT,
                    symbol=symbol,
                    reason=(
                        "iqfeed_l2_provider_snapshot_completion_boundary_unavailable"
                    ),
                    first_available_at=boundary,
                    last_available_at=boundary,
                    lost_count=1,
                )
            )
        return symbols

    def active_producer_generation(
        self,
    ) -> CaptureExternalProducerGeneration | None:
        with self._condition:
            return self._active_producer_generation

    def record_release_failure(
        self,
        *,
        rows: Sequence[Mapping[str, Any]],
        available_at: datetime,
    ) -> int:
        at = _utc(available_at, "IQFeed L2 failed handoff boundary")
        lost = 0
        for row in rows:
            if not isinstance(row, Mapping):
                lost += self._latch_unattributed_hot_loss(
                    stream=CaptureStream.L2_DEPTH_DELTA,
                    reason="iqfeed_l2_capture_release_handoff_unattributed_failed",
                    at=at,
                )
                continue
            symbol = str(row.get("sym") or "").strip().upper()
            if not symbol:
                lost += self._latch_unattributed_hot_loss(
                    stream=CaptureStream.L2_DEPTH_DELTA,
                    reason="iqfeed_l2_capture_release_handoff_unattributed_failed",
                    at=at,
                )
                continue
            with self._condition:
                requested = symbol in self._requested_hot
            if not requested:
                continue
            generation_raw = row.get("connection_generation")
            generation = (
                generation_raw
                if isinstance(generation_raw, int)
                and not isinstance(generation_raw, bool)
                and generation_raw > 0
                else None
            )
            self._fence_generation(symbol, generation)
            lost += 1
            self._latch_gap(
                CoverageGap(
                    stream=CaptureStream.L2_DEPTH_DELTA,
                    symbol=symbol,
                    reason="iqfeed_l2_capture_release_handoff_failed",
                    first_available_at=at,
                    last_available_at=at,
                    lost_count=1,
                )
            )
        return lost

    def _take_gaps(self) -> tuple[CoverageGap, ...]:
        with self._condition:
            gaps = tuple(row.freeze() for row in self._pending_gaps.values())
            self._pending_gaps.clear()
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
                self._flush_gaps()
                return
            try:
                envelope, envelope_size = self._queue.get(
                    timeout=self._WORKER_POLL_SECONDS
                )
            except queue.Empty:
                continue
            try:
                provenance = envelope.payload.get(
                    IQFEED_L2_SOURCE_PROVENANCE_FIELD
                )
                generation = (
                    provenance.get("connection_generation")
                    if isinstance(provenance, Mapping)
                    else None
                )
                if not isinstance(generation, int) or isinstance(generation, bool):
                    generation = None
                if envelope.stream is CaptureStream.L2_DEPTH_DELTA:
                    with self._condition:
                        durable_checkpoint = (
                            generation is not None
                            and self._active_generation.get(envelope.symbol)
                            == generation
                        )
                    if not durable_checkpoint:
                        self._latch_gap(
                            CoverageGap(
                                stream=envelope.stream,
                                symbol=envelope.symbol,
                                reason=(
                                    "iqfeed_l2_capture_checkpoint_not_durable"
                                ),
                                first_available_at=envelope.clocks.available_at,
                                last_available_at=envelope.clocks.available_at,
                                lost_count=1,
                            )
                        )
                        continue
                try:
                    self.sink.submit_envelope(envelope)
                except _CaptureSubmissionRejected as exc:
                    with self._condition:
                        self._submit_failures += 1
                    self._fence_generation(envelope.symbol, generation)
                    if not exc.coverage_gap_recorded:
                        self._latch_gap(
                            CoverageGap(
                                stream=envelope.stream,
                                symbol=envelope.symbol,
                                reason="iqfeed_l2_capture_sink_submit_failed",
                                first_available_at=envelope.clocks.available_at,
                                last_available_at=envelope.clocks.available_at,
                                lost_count=1,
                            )
                        )
                except Exception:
                    with self._condition:
                        self._submit_failures += 1
                    self._fence_generation(envelope.symbol, generation)
                    self._latch_gap(
                        CoverageGap(
                            stream=envelope.stream,
                            symbol=envelope.symbol,
                            reason="iqfeed_l2_capture_sink_submit_failed",
                            first_available_at=envelope.clocks.available_at,
                            last_available_at=envelope.clocks.available_at,
                            lost_count=1,
                        )
                    )
                else:
                    with self._condition:
                        self._submitted += 1
                        if envelope.stream is CaptureStream.L2_DEPTH_CHECKPOINT:
                            if (
                                generation is not None
                                and self._pending_checkpoint_generation.get(
                                    envelope.symbol
                                )
                                == generation
                            ):
                                self._pending_checkpoint_generation.pop(
                                    envelope.symbol, None
                                )
                                self._active_generation[envelope.symbol] = generation
                        self._condition.notify_all()
            finally:
                self._queue.task_done()
                with self._condition:
                    self._pending_bytes -= envelope_size
                    if self._pending_bytes < 0:  # pragma: no cover - lock invariant
                        self._pending_bytes = 0
                        self._terminal_error = CaptureContractError(
                            "IQFeed L2 capture byte reservation underflow"
                        )
                        self._accepting = False
                    self._condition.notify_all()

    def wait_until_idle(self, timeout_seconds: float) -> bool:
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise CaptureContractError("IQFeed L2 idle timeout must be positive")
        deadline = time.monotonic() + timeout
        with self._condition:
            while time.monotonic() < deadline:
                if (
                    self._queue.unfinished_tasks == 0
                    and not self._pending_gaps
                ):
                    return True
                self._condition.wait(timeout=min(0.02, deadline - time.monotonic()))
            return False

    def close(self, timeout_seconds: float = 5.0) -> Mapping[str, Any]:
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise CaptureContractError("IQFeed L2 close timeout must be positive")
        with self._condition:
            if not self._started:
                raise CaptureContractError("IQFeed L2 capture handoff was never started")
            self._accepting = False
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise CaptureContractError(
                    "IQFeed L2 capture handoff did not drain before close"
                )
        health = self.health()
        if health["terminal_error"] is not None or health["unpersisted_gap_count"]:
            raise CaptureContractError(
                "IQFeed L2 capture handoff closed with unpersisted coverage loss"
            )
        return health

    def health(self) -> Mapping[str, Any]:
        with self._condition:
            return {
                "network_fallback_allowed": False,
                "started": self._started,
                "accepting": self._accepting,
                "requested_hot_symbols": tuple(sorted(self._requested_hot)),
                "pending_checkpoint_generations": dict(
                    sorted(self._pending_checkpoint_generation.items())
                ),
                "active_generations": dict(sorted(self._active_generation.items())),
                "last_enqueued_sequences": {
                    f"{symbol}:{generation}": sequence
                    for (symbol, generation), sequence in sorted(
                        self._last_enqueued_sequence.items()
                    )
                },
                "queue_depth": self._queue.qsize(),
                "unfinished_tasks": self._queue.unfinished_tasks,
                "max_pending_events": self.max_pending_events,
                "max_pending_bytes": self.max_pending_bytes,
                "pending_bytes": self._pending_bytes,
                "peak_pending_bytes": self._peak_pending_bytes,
                "max_gap_keys": self.max_gap_keys,
                "offered_hot": self._offered_hot,
                "ignored_cold": self._ignored_cold,
                "submitted": self._submitted,
                "queue_overflow_lost": self._queue_overflow_lost,
                "byte_overflow_lost": self._byte_overflow_lost,
                "byte_overflow_incidents": self._byte_overflow_incidents,
                "oversized_envelope_lost": self._oversized_envelope_lost,
                "reported_gap_count": self._reported_gap_count,
                "submit_failures": self._submit_failures,
                "pending_gap_keys": len(self._pending_gaps),
                "gap_ledger_overflow": self._gap_ledger_overflow,
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
    "BoundedIqfeedL2CaptureHandoff",
    "IQFEED_L2_CAPTURE_ENVELOPE_SCHEMA_VERSION",
    "IqfeedL2CaptureEnvelope",
    "IqfeedL2CaptureSink",
    "IqfeedL2ProcessCaptureSink",
]
