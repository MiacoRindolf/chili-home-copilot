"""Durable local queue for captured viability inputs used by Alpaca PAPER.

The queue is deliberately broker- and provider-incapable.  Producers reserve a
single service-wide sequence before constructing the hash-bound viability
bundle, submit through the host's bounded capture ingress, and return
immediately.  A background capture writer fsyncs immutable chunks/payload packs,
publishes one hash-chained content-addressed commit receipt, fsyncs that receipt,
and only then acknowledges a durable frontier.

Readers follow only that committed chain.  Unsealed/orphan chunks are ignored;
any committed gap or fork poisons the exact activation generation.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from .captured_paper_selection_producer import (
    CapturedPaperSelectionAuthority,
    CapturedPaperSelectionBatch,
    CapturedPaperSelectionFrontierReceipt,
    CapturedPaperSelectionObservation,
    CapturedPaperSelectionQueueReadTimeout,
    CapturedPaperSelectionQueueUnavailable,
    CapturedPaperSelectionRouteStateUpdate,
    ROUTE_COVERAGE_UNAVAILABLE,
    ROUTE_ELIGIBLE,
)
from .captured_viability_adapter import (
    COVERAGE_UNAVAILABLE,
    SCORED,
    CapturedViabilityInputBundle,
    CapturedViabilityScoreResult,
    CapturedViabilityScoringAuthority,
    score_captured_viability,
)
from .replay_capture_contract import (
    CAPTURE_SCHEMA_VERSION,
    CaptureOrtexSelectionSnapshot,
    CaptureClocks,
    CaptureContractError,
    CaptureEvent,
    CaptureEventRef,
    CaptureRunIdentity,
    CaptureStream,
    CoverageGap,
    canonical_json_bytes,
    sha256_json,
)
from .replay_capture_runtime import (
    CAPTURE_DERIVED_ARTIFACT_SCHEMA_VERSION,
    BoundedCaptureIngress,
    CaptureDurableBatchCommitter,
    CaptureWriterWorker,
    ChunkRef,
    ContentAddressedCaptureStore,
    IngressBatch,
    RetentionObjectRef,
    SharedCaptureWriterLease,
)


UTC = timezone.utc

QUEUE_EVENT_SCHEMA_VERSION = "chili.captured-paper-selection-queue-event.v2"
QUEUE_COMMIT_SCHEMA_VERSION = "chili.captured-paper-selection-queue-commit.v2"
QUEUE_RECEIPT_SCHEMA_VERSION = "chili.captured-paper-selection-queue-receipt.v1"
QUEUE_POISON_SCHEMA_VERSION = "chili.captured-paper-selection-queue-poison.v1"
QUEUE_DERIVED_KIND = "captured_paper_selection_queue_commit"
QUEUE_ORTEX_MANIFEST_DERIVED_KIND = "captured_paper_ortex_selection_batch"
QUEUE_ORTEX_MANIFEST_REF_SCHEMA_VERSION = (
    "chili.captured-paper-ortex-manifest-ref.v1"
)
QUEUE_PROVIDER = "captured_viability_adapter"
QUEUE_SOURCE_NAME = "captured_viability_queue"

_ACTIVATION_STATIC_SOURCE_STREAMS = frozenset(
    {
        CaptureStream.CONFIG_SNAPSHOT,
        CaptureStream.FEATURE_FLAG_SNAPSHOT,
        CaptureStream.CODE_BUILD,
    }
)

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")


class CapturedPaperSelectionQueueError(CaptureContractError):
    """The local queue contract or durable chain is invalid."""


def _fail(message: str) -> None:
    raise CapturedPaperSelectionQueueError(message)


def _utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _fail(f"{field_name} must be timezone-aware")
    try:
        offset = value.utcoffset()
    except Exception as exc:
        raise CapturedPaperSelectionQueueError(
            f"{field_name} clock is invalid"
        ) from exc
    if offset is None:
        _fail(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        _fail(f"{field_name} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapturedPaperSelectionQueueError(
            f"{field_name} is not ISO-8601"
        ) from exc
    return _utc(parsed, field_name)


def _iso(value: datetime) -> str:
    return _utc(value, "datetime").isoformat().replace("+00:00", "Z")


def _sha(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized != value or _SHA_RE.fullmatch(normalized) is None:
        _fail(f"{field_name} must be a lowercase SHA-256")
    return normalized


def _positive_int(value: Any, field_name: str, *, allow_zero: bool = False) -> int:
    if type(value) is not int or value < (0 if allow_zero else 1):
        _fail(f"{field_name} is invalid")
    return value


def _reason(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized != value or _REASON_RE.fullmatch(normalized) is None:
        _fail("queue poison reason is invalid")
    return normalized


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{field_name} must be an object")
    return value


def _exact_fields(
    raw: Mapping[str, Any], expected: set[str], field_name: str
) -> None:
    if set(raw) != expected:
        _fail(f"{field_name} fields do not match schema")


def _chunk_dict(ref: ChunkRef) -> dict[str, Any]:
    return {
        "sha256": ref.sha256,
        "row_count": ref.row_count,
        "raw_bytes": ref.raw_bytes,
        "compressed_bytes": ref.compressed_bytes,
        "relative_path": ref.relative_path,
    }


def _chunk_from_dict(raw: Mapping[str, Any]) -> ChunkRef:
    _exact_fields(
        raw,
        {
            "sha256",
            "row_count",
            "raw_bytes",
            "compressed_bytes",
            "relative_path",
        },
        "queue chunk ref",
    )
    return ChunkRef(
        sha256=_sha(raw.get("sha256"), "chunk sha256"),
        row_count=_positive_int(raw.get("row_count"), "chunk row_count"),
        raw_bytes=_positive_int(raw.get("raw_bytes"), "chunk raw_bytes"),
        compressed_bytes=_positive_int(
            raw.get("compressed_bytes"), "chunk compressed_bytes"
        ),
        relative_path=str(raw.get("relative_path") or ""),
    )


@dataclass(frozen=True, slots=True)
class _PendingOrtexManifest:
    source_sequence: int
    batch_sha256: str
    window_start: datetime
    window_end: datetime
    payload: Mapping[str, Any]
    payload_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _positive_int(self.source_sequence, "Ortex manifest source sequence")
        object.__setattr__(
            self,
            "batch_sha256",
            _sha(self.batch_sha256, "Ortex manifest batch SHA256"),
        )
        start = _utc(self.window_start, "Ortex manifest window_start")
        end = _utc(self.window_end, "Ortex manifest window_end")
        if end < start:
            _fail("Ortex manifest window is reversed")
        if not isinstance(self.payload, Mapping):
            _fail("Ortex manifest payload is malformed")
        raw_payload = canonical_json_bytes(self.payload)
        if hashlib.sha256(raw_payload).hexdigest() != self.batch_sha256:
            _fail("Ortex manifest payload hash mismatch")
        object.__setattr__(self, "window_start", start)
        object.__setattr__(self, "window_end", end)
        object.__setattr__(self, "payload", json.loads(raw_payload))
        object.__setattr__(self, "payload_bytes", len(raw_payload))


def _ortex_manifest_pointer(
    event: CaptureEvent,
    *,
    queue_source_sequence: int,
) -> tuple[dict[str, Any], _PendingOrtexManifest]:
    if event.stream is not CaptureStream.ORTEX_SNAPSHOT:
        _fail("Ortex manifest pointer received a non-Ortex event")
    try:
        CaptureOrtexSelectionSnapshot.from_event(event)
    except (CaptureContractError, TypeError, ValueError) as exc:
        raise CapturedPaperSelectionQueueError(
            "Ortex source event failed strict batch validation"
        ) from exc
    batch_sha256 = event.payload_sha256
    pointer = {
        "schema_version": QUEUE_ORTEX_MANIFEST_REF_SCHEMA_VERSION,
        "kind": QUEUE_ORTEX_MANIFEST_DERIVED_KIND,
        "batch_sha256": batch_sha256,
        "window_start": _iso(event.clocks.received_at),
        "window_end": _iso(event.clocks.available_at),
    }
    return pointer, _PendingOrtexManifest(
        source_sequence=queue_source_sequence,
        batch_sha256=batch_sha256,
        window_start=event.clocks.received_at,
        window_end=event.clocks.available_at,
        payload=event.payload,
    )


def _event_envelope(
    event: CaptureEvent,
    *,
    queue_source_sequence: int,
) -> tuple[dict[str, Any], _PendingOrtexManifest | None]:
    if event.stream is CaptureStream.ORTEX_SNAPSHOT:
        pointer, pending = _ortex_manifest_pointer(
            event,
            queue_source_sequence=queue_source_sequence,
        )
        return (
            {
                "event": event.to_record(include_payload=False),
                "event_sha256": event.event_sha256,
                "ortex_manifest_ref": pointer,
            },
            pending,
        )
    return {
        "event": event.to_record(include_payload=True),
        "event_sha256": event.event_sha256,
    }, None


def _retention_ref_from_dict(raw: Mapping[str, Any]) -> RetentionObjectRef:
    _exact_fields(raw, {"tier", "relative_path", "sha256", "bytes"}, "retention ref")
    return RetentionObjectRef(
        tier=str(raw.get("tier") or ""),
        relative_path=str(raw.get("relative_path") or ""),
        sha256=str(raw.get("sha256") or ""),
        bytes=raw.get("bytes"),
    )


def _event_from_envelope(
    raw: Mapping[str, Any],
    *,
    root: Path,
    queue_identity: CaptureRunIdentity,
    ortex_manifest_refs: Sequence[RetentionObjectRef],
    manifest_cache: dict[
        str, tuple[Mapping[str, Any], RetentionObjectRef]
    ],
    budget_check: Callable[[], None],
) -> CaptureEvent:
    budget_check()
    if set(raw) == {"event", "event_sha256"}:
        event = CaptureEvent.from_record(_mapping(raw.get("event"), "source event"))
        if event.stream is CaptureStream.ORTEX_SNAPSHOT:
            _fail("Ortex source event lacks a durable manifest ref")
    else:
        _exact_fields(
            raw,
            {"event", "event_sha256", "ortex_manifest_ref"},
            "source event envelope",
        )
        event_record = _mapping(raw.get("event"), "source event")
        pointer = _mapping(raw.get("ortex_manifest_ref"), "Ortex manifest ref")
        _exact_fields(
            pointer,
            {
                "schema_version",
                "kind",
                "batch_sha256",
                "window_start",
                "window_end",
            },
            "Ortex manifest ref",
        )
        if (
            pointer.get("schema_version")
            != QUEUE_ORTEX_MANIFEST_REF_SCHEMA_VERSION
            or pointer.get("kind") != QUEUE_ORTEX_MANIFEST_DERIVED_KIND
        ):
            _fail("Ortex manifest ref schema/kind mismatch")
        batch_sha256 = _sha(
            pointer.get("batch_sha256"), "Ortex manifest ref batch SHA256"
        )
        if event_record.get("payload_sha256") != batch_sha256:
            _fail("Ortex manifest ref differs from source event payload hash")
        window_start = _parse_utc(
            pointer.get("window_start"), "Ortex manifest ref window_start"
        )
        window_end = _parse_utc(
            pointer.get("window_end"), "Ortex manifest ref window_end"
        )
        if window_end < window_start:
            _fail("Ortex manifest ref window is reversed")

        cached = manifest_cache.get(batch_sha256)
        manifest = None
        if cached is not None and any(
            ref == cached[1] for ref in ortex_manifest_refs
        ):
            manifest = cached[0]
        if manifest is None:
            matches: list[Mapping[str, Any]] = []
            matching_ref_sha256: str | None = None
            for object_ref in ortex_manifest_refs:
                budget_check()
                record = ContentAddressedCaptureStore.read_derived_ref(
                    root, object_ref
                )
                budget_check()
                expected_record_fields = {
                    "schema_version",
                    "identity",
                    "kind",
                    "window_start",
                    "window_end",
                    "payload",
                    "payload_sha256",
                }
                if set(record) != expected_record_fields:
                    _fail("Ortex derived artifact fields do not match schema")
                if (
                    record.get("schema_version")
                    != CAPTURE_DERIVED_ARTIFACT_SCHEMA_VERSION
                    or record.get("identity") != queue_identity.to_dict()
                    or record.get("kind")
                    != QUEUE_ORTEX_MANIFEST_DERIVED_KIND
                    or record.get("payload_sha256") != batch_sha256
                    or not isinstance(record.get("payload"), Mapping)
                    or sha256_json(record["payload"]) != batch_sha256
                ):
                    continue
                matches.append(record)
                matching_ref_sha256 = object_ref.sha256
            if len(matches) != 1:
                _fail("Ortex committed manifest is missing or duplicated")
            manifest = matches[0]
            assert matching_ref_sha256 is not None
            manifest_cache[batch_sha256] = (
                manifest,
                next(
                    ref
                    for ref in ortex_manifest_refs
                    if ref.sha256 == matching_ref_sha256
                ),
            )
        if (
            manifest.get("window_start") != _iso(window_start)
            or manifest.get("window_end") != _iso(window_end)
        ):
            _fail("Ortex manifest ref window differs from durable artifact")
        payload = _mapping(manifest.get("payload"), "Ortex manifest payload")
        event = CaptureEvent.from_record(event_record, payload=payload)
        if (
            event.stream is not CaptureStream.ORTEX_SNAPSHOT
            or event.clocks.received_at != window_start
            or event.clocks.available_at != window_end
        ):
            _fail("Ortex manifest ref differs from source event clocks")
        try:
            CaptureOrtexSelectionSnapshot.from_event(event)
        except (CaptureContractError, TypeError, ValueError) as exc:
            raise CapturedPaperSelectionQueueError(
                "durable Ortex manifest failed strict batch validation"
            ) from exc
        budget_check()
    if event.event_sha256 != _sha(raw.get("event_sha256"), "source event SHA256"):
        _fail("source event content address mismatch")
    return event


def _validate_source_events(
    bundle: CapturedViabilityInputBundle,
    source_events: Sequence[CaptureEvent],
) -> tuple[CaptureEvent, ...]:
    events = tuple(source_events)
    if not events or any(type(event) is not CaptureEvent for event in events):
        _fail("source events must be non-empty exact CaptureEvent values")
    by_hash = {event.event_sha256: event for event in events}
    if len(by_hash) != len(events):
        _fail("source events contain duplicate content addresses")
    refs = {ref.event_sha256: ref for ref in bundle.source_refs}
    if set(by_hash) != set(refs):
        _fail("source event inventory differs from bundle refs")
    for digest, event in by_hash.items():
        if CaptureEventRef.from_event(event) != refs[digest]:
            _fail("source event bytes do not reconstruct their bundle ref")
    return tuple(sorted(events, key=lambda event: (event.sequence, event.event_sha256)))


def _source_event_refs(events: Sequence[CaptureEvent]) -> list[dict[str, Any]]:
    return [CaptureEventRef.from_event(event).to_dict() for event in events]


def _retained_source_event_envelopes(
    events: Sequence[CaptureEvent],
    *,
    queue_source_sequence: int,
) -> tuple[list[dict[str, Any]], list[_PendingOrtexManifest]]:
    """Retain raw decision-local evidence without duplicating sealed build bytes.

    Config, policy, and code-build payloads are activation-static and already
    hash-bound by both the selection authority and the scored bundle.  Their
    exact event refs remain in every queue event, while their large raw payloads
    stay in the sealed activation evidence instead of being copied once per
    symbol/variant occurrence.  Dynamic market/provider evidence remains fully
    embedded and independently reconstructable.
    """

    envelopes: list[dict[str, Any]] = []
    pending_ortex: list[_PendingOrtexManifest] = []
    for event in events:
        if event.stream in _ACTIVATION_STATIC_SOURCE_STREAMS:
            continue
        envelope, pending = _event_envelope(
            event,
            queue_source_sequence=queue_source_sequence,
        )
        envelopes.append(envelope)
        if pending is not None:
            pending_ortex.append(pending)
    return envelopes, pending_ortex


def _recomputed_static_event_sha256(
    ref: CaptureEventRef,
    *,
    identity: CaptureRunIdentity,
) -> str:
    """Reconstruct the content address of one payload-elided static event."""

    return sha256_json(
        {
            "schema_version": CAPTURE_SCHEMA_VERSION,
            "identity": identity.to_dict(),
            "sequence": ref.sequence,
            "stream": ref.stream.value,
            "symbol": ref.symbol,
            "provider": ref.provider,
            "clocks": CaptureClocks(
                provider_event_at=ref.provider_event_at,
                market_reference_at=ref.market_reference_at,
                received_at=ref.received_at,
                available_at=ref.available_at,
            ).to_dict(),
            "query": None,
            "query_sha256": None,
            "payload_sha256": ref.payload_sha256,
        }
    )


def _validate_compact_source_evidence(
    bundle: CapturedViabilityInputBundle,
    *,
    raw_refs: Any,
    raw_events: Any,
    root: Path,
    queue_identity: CaptureRunIdentity,
    loaded_commit: _LoadedCommit,
    manifest_cache: dict[
        str, tuple[Mapping[str, Any], RetentionObjectRef]
    ],
    budget_check: Callable[[], None],
) -> tuple[tuple[CaptureEventRef, ...], tuple[CaptureEvent, ...]]:
    if not isinstance(raw_refs, list) or not isinstance(raw_events, list):
        _fail("queue source evidence inventory is malformed")
    try:
        refs = tuple(
            CaptureEventRef.from_dict(_mapping(value, "source event ref"))
            for value in raw_refs
        )
    except (CaptureContractError, TypeError, ValueError) as exc:
        raise CapturedPaperSelectionQueueError(
            "queue source event reference is malformed"
        ) from exc
    if refs != bundle.source_refs:
        _fail("queue source event refs differ from bundle refs")

    events = tuple(
        _event_from_envelope(
            _mapping(value, "source event envelope"),
            root=root,
            queue_identity=queue_identity,
            ortex_manifest_refs=loaded_commit.commit.ortex_manifest_refs,
            manifest_cache=manifest_cache,
            budget_check=budget_check,
        )
        for value in raw_events
    )
    retained_refs = tuple(CaptureEventRef.from_event(event) for event in events)
    expected_retained_refs = tuple(
        ref
        for ref in refs
        if ref.stream not in _ACTIVATION_STATIC_SOURCE_STREAMS
    )
    if retained_refs != expected_retained_refs:
        _fail("queue retained source payloads differ from dynamic bundle refs")
    if not events:
        _fail("queue lacks retained decision-local source evidence")
    source_identity = events[0].identity
    if any(event.identity != source_identity for event in events):
        _fail("queue retained source payloads span capture identities")

    expected_static_hashes = {
        CaptureStream.CONFIG_SNAPSHOT: bundle.config_sha256,
        CaptureStream.FEATURE_FLAG_SNAPSHOT: bundle.policy_sha256,
        CaptureStream.CODE_BUILD: bundle.code_sha256,
    }
    for stream, expected_payload_sha256 in expected_static_hashes.items():
        matches = tuple(ref for ref in refs if ref.stream is stream)
        if (
            len(matches) != 1
            or matches[0].payload_sha256 != expected_payload_sha256
        ):
            _fail("queue activation-static source ref binding mismatch")
        static_ref = matches[0]
        if static_ref.query_sha256 is not None:
            _fail("queue activation-static source ref unexpectedly has a query")
        if (
            static_ref.identity_sha256 != source_identity.identity_sha256
            or static_ref.event_sha256
            != _recomputed_static_event_sha256(
                static_ref,
                identity=source_identity,
            )
        ):
            _fail("queue activation-static source ref content address mismatch")
    return refs, events


def _authority_matches_selection(
    scoring: CapturedViabilityScoringAuthority,
    selection: CapturedPaperSelectionAuthority,
) -> bool:
    binding = next(
        (
            row
            for row in selection.variant_bindings
            if row.variant_id == scoring.variant_id
        ),
        None,
    )
    return bool(
        binding is not None
        and scoring.family_id == binding.family
        and scoring.activation_policy_sha256 == selection.policy_sha256
        and scoring.activation_settings_projection_sha256
        == selection.settings_projection_sha256
        and scoring.activation_code_build_sha256
        == selection.code_build_sha256
        and scoring.selection_authority_sha256 == selection.authority_sha256
        and not scoring.paper_only_strategy_override
        and not scoring.live_cash_authorized
        and not scoring.real_money_authorized
    )


def _expected_scoring_authority(
    bundle: CapturedViabilityInputBundle,
    selection: CapturedPaperSelectionAuthority,
) -> CapturedViabilityScoringAuthority:
    """Derive the exact per-occurrence authority from sealed bundle bytes.

    ``dependency_profile_sha256`` legitimately changes when the next captured
    snapshot advances its causal coverage clock.  It is therefore not an
    activation-constant field and cannot be pinned to the first occurrence.
    Every other scorer field remains hash-derived from this bundle or from the
    immutable selection authority.
    """

    return CapturedViabilityScoringAuthority(
        capture_identity_sha256=bundle.capture_identity_sha256,
        policy_sha256=bundle.policy_sha256,
        config_sha256=bundle.config_sha256,
        code_sha256=bundle.code_sha256,
        settings_projection_sha256=bundle.settings_projection_sha256,
        family_sha256=bundle.component_roots["family"],
        dependency_profile_sha256=(
            bundle.dependency_inventory.dependency_profile.profile_sha256
        ),
        variant_id=bundle.variant_id,
        family_id=bundle.family.family_id,
        family_version=bundle.family.version,
        activation_policy_sha256=selection.policy_sha256,
        activation_settings_projection_sha256=(
            selection.settings_projection_sha256
        ),
        activation_code_build_sha256=selection.code_build_sha256,
        selection_authority_sha256=selection.authority_sha256,
    )


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionQueuePublishReceipt:
    source_sequence: int
    bundle_sha256: str
    event_sha256: str
    score_result: CapturedViabilityScoreResult
    accepted: bool
    durable: bool = False
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _positive_int(self.source_sequence, "publish source_sequence")
        _sha(self.bundle_sha256, "publish bundle_sha256")
        _sha(self.event_sha256, "publish event_sha256")
        if type(self.score_result) is not CapturedViabilityScoreResult:
            _fail("publish score result is malformed")
        if type(self.accepted) is not bool or type(self.durable) is not bool:
            _fail("publish receipt flags must be boolean")
        if self.durable:
            _fail("hot-path publish receipt cannot claim asynchronous durability")
        object.__setattr__(self, "receipt_sha256", sha256_json(self.body()))

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": QUEUE_RECEIPT_SCHEMA_VERSION,
            "source_sequence": self.source_sequence,
            "bundle_sha256": self.bundle_sha256,
            "event_sha256": self.event_sha256,
            "score_result": self.score_result.to_dict(),
            "accepted": self.accepted,
            "durable": self.durable,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "receipt_sha256": self.receipt_sha256}


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionQueuePoisonReceipt:
    reason: str
    observed_at: datetime
    source_sequence: int | None
    accepted_by_gap_ledger: bool
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", _reason(self.reason))
        object.__setattr__(
            self, "observed_at", _utc(self.observed_at, "poison observed_at")
        )
        if self.source_sequence is not None:
            _positive_int(self.source_sequence, "poison source_sequence")
        if type(self.accepted_by_gap_ledger) is not bool:
            _fail("poison gap-ledger flag must be boolean")
        object.__setattr__(self, "receipt_sha256", sha256_json(self.body()))

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": QUEUE_POISON_SCHEMA_VERSION,
            "reason": self.reason,
            "observed_at": _iso(self.observed_at),
            "source_sequence": self.source_sequence,
            "accepted_by_gap_ledger": self.accepted_by_gap_ledger,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "receipt_sha256": self.receipt_sha256}


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionQueueHealth:
    poisoned: bool
    poison_reason: str | None
    reserved_sequence: int | None
    accepted_through: int
    durable_through: int
    commit_count: int
    last_commit_sha256: str | None
    watermark_at: datetime | None
    lag_events: int
    lag_seconds: float | None
    ingress: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "poisoned": self.poisoned,
            "poison_reason": self.poison_reason,
            "reserved_sequence": self.reserved_sequence,
            "accepted_through": self.accepted_through,
            "durable_through": self.durable_through,
            "commit_count": self.commit_count,
            "last_commit_sha256": self.last_commit_sha256,
            "watermark_at": _iso(self.watermark_at) if self.watermark_at else None,
            "lag_events": self.lag_events,
            "lag_seconds": self.lag_seconds,
            "ingress": dict(self.ingress) if self.ingress is not None else None,
        }


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionQueueDurableFrontier:
    queue_identity_sha256: str
    selection_authority_sha256: str
    commit_count: int
    last_commit_sha256: str | None
    durable_through: int
    poisoned: bool
    poison_reason: str | None


class CapturedPaperSelectionQueueDurableGate:
    """Read-only-to-consumers acknowledgement gate advanced after commit fsync."""

    def __init__(
        self,
        *,
        queue_identity_sha256: str,
        selection_authority_sha256: str,
        expected_account_id: str,
        activation_generation: str,
        commit_count: int,
        last_commit_sha256: str | None,
        durable_through: int,
        poisoned: bool,
        poison_reason: str | None,
        initial_chain: Sequence["_LoadedCommit"],
    ) -> None:
        self._queue_identity_sha256 = _sha(
            queue_identity_sha256, "durable gate queue identity"
        )
        self._selection_authority_sha256 = _sha(
            selection_authority_sha256, "durable gate selection authority"
        )
        if (
            not isinstance(expected_account_id, str)
            or not expected_account_id.strip()
            or expected_account_id != expected_account_id.strip()
            or not isinstance(activation_generation, str)
            or not activation_generation.strip()
            or activation_generation != activation_generation.strip()
        ):
            _fail("durable gate account/generation binding is malformed")
        self._expected_account_id = expected_account_id
        self._activation_generation = activation_generation
        self._lock = threading.RLock()
        claimed_commit_count = _positive_int(
            commit_count, "durable gate commit count", allow_zero=True
        )
        claimed_last_commit_sha256 = (
            _sha(last_commit_sha256, "durable gate last commit")
            if last_commit_sha256 is not None
            else None
        )
        claimed_durable_through = _positive_int(
            durable_through, "durable gate source frontier", allow_zero=True
        )
        if type(poisoned) is not bool:
            _fail("durable gate poison flag must be boolean")
        claimed_poisoned = poisoned
        claimed_poison_reason = (
            _reason(poison_reason) if poison_reason is not None else None
        )
        if bool(claimed_commit_count) != bool(claimed_last_commit_sha256):
            _fail("durable gate commit count/hash are inconsistent")
        if claimed_poisoned != bool(claimed_poison_reason):
            _fail("durable gate poison state is inconsistent")
        chain = tuple(initial_chain)
        if any(type(row) is not _LoadedCommit for row in chain):
            _fail("durable gate initial chain is malformed")
        if len(chain) != claimed_commit_count:
            _fail("durable gate initial chain count is inconsistent")
        self._commit_count = 0
        self._last_commit_sha256: str | None = None
        self._durable_through = 0
        self._poisoned = False
        self._poison_reason: str | None = None
        self._chain: list[_LoadedCommit] = []
        for row in chain:
            self._advance(row)
        if (
            self._commit_count != claimed_commit_count
            or self._last_commit_sha256 != claimed_last_commit_sha256
            or self._durable_through != claimed_durable_through
            or self._poisoned != claimed_poisoned
            or self._poison_reason != claimed_poison_reason
        ):
            _fail("durable gate initial chain frontier is inconsistent")

    def snapshot(self) -> CapturedPaperSelectionQueueDurableFrontier:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> CapturedPaperSelectionQueueDurableFrontier:
        return CapturedPaperSelectionQueueDurableFrontier(
            queue_identity_sha256=self._queue_identity_sha256,
            selection_authority_sha256=self._selection_authority_sha256,
            commit_count=self._commit_count,
            last_commit_sha256=self._last_commit_sha256,
            durable_through=self._durable_through,
            poisoned=self._poisoned,
            poison_reason=self._poison_reason,
        )

    def _snapshot_since(
        self,
        commit_count: int,
        *,
        max_commit_files: int,
    ) -> tuple[
        CapturedPaperSelectionQueueDurableFrontier,
        tuple["_LoadedCommit", ...],
    ]:
        with self._lock:
            count = _positive_int(
                commit_count,
                "durable gate reader commit count",
                allow_zero=True,
            )
            maximum = _positive_int(
                max_commit_files,
                "durable gate reader maximum commit files",
            )
            if count > self._commit_count:
                _fail("durable gate reader commit count is ahead")
            if self._commit_count > maximum:
                _fail("queue commit inventory exceeds bounded scan limit")
            return self._snapshot_locked(), tuple(self._chain[count:])

    def _advance(self, loaded: "_LoadedCommit") -> None:
        if type(loaded) is not _LoadedCommit:
            _fail("durable gate acknowledgement is malformed")
        commit = loaded.commit
        with self._lock:
            if (
                commit.queue_identity_sha256 != self._queue_identity_sha256
                or commit.selection_authority_sha256
                != self._selection_authority_sha256
                or commit.expected_account_id
                != self._expected_account_id
                or commit.activation_generation
                != self._activation_generation
                or commit.commit_index != self._commit_count + 1
                or commit.event_sequence_from_exclusive != self._durable_through
                or commit.previous_commit_sha256 != self._last_commit_sha256
            ):
                _fail("durable gate acknowledgement is stale or foreign")
            prior = self._chain[-1] if self._chain else None
            if (
                commit.cumulative_sha256
                != _expected_cumulative(
                    (
                        prior.commit.cumulative_sha256
                        if prior is not None
                        else None
                    ),
                    commit_index=commit.commit_index,
                    event_refs=commit.event_refs,
                    gaps=commit.gaps,
                    ortex_manifest_refs=commit.ortex_manifest_refs,
                )
                or (
                    prior is not None
                    and (
                        commit.resource_binding_sha256
                        != prior.commit.resource_binding_sha256
                        or commit.storage_policy_sha256
                        != prior.commit.storage_policy_sha256
                    )
                )
                or (
                    self._poisoned
                    and (commit.event_refs or not commit.poisoned)
                )
            ):
                _fail("durable gate acknowledgement breaks the verified chain")
            self._chain.append(loaded)
            self._commit_count = commit.commit_index
            self._last_commit_sha256 = loaded.object_ref.sha256
            self._durable_through = commit.event_sequence_through
            if commit.poisoned:
                self._poisoned = True
                self._poison_reason = commit.poison_reason


@dataclass(frozen=True, slots=True)
class CapturedPaperSelectionQueueCommit:
    queue_identity_sha256: str
    selection_authority_sha256: str
    expected_account_id: str
    activation_generation: str
    commit_index: int
    previous_commit_sha256: str | None
    event_sequence_from_exclusive: int
    event_sequence_through: int
    event_refs: tuple[CaptureEventRef, ...]
    event_chunks: tuple[ChunkRef, ...]
    gaps: tuple[Mapping[str, Any], ...]
    gap_chunks: tuple[ChunkRef, ...]
    ortex_manifest_refs: tuple[RetentionObjectRef, ...]
    poisoned: bool
    poison_reason: str | None
    watermark_at: datetime
    committed_at: datetime
    cumulative_sha256: str
    resource_binding_sha256: str
    storage_policy_sha256: str
    schema_version: str = QUEUE_COMMIT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != QUEUE_COMMIT_SCHEMA_VERSION:
            _fail("queue commit schema is unsupported")
        if type(self.poisoned) is not bool:
            _fail("queue commit poisoned flag must be boolean")
        if (
            not isinstance(self.expected_account_id, str)
            or not self.expected_account_id.strip()
            or self.expected_account_id != self.expected_account_id.strip()
            or not isinstance(self.activation_generation, str)
            or not self.activation_generation.strip()
            or self.activation_generation != self.activation_generation.strip()
        ):
            _fail("queue commit account/generation binding is invalid")
        for name in (
            "queue_identity_sha256",
            "selection_authority_sha256",
            "cumulative_sha256",
            "resource_binding_sha256",
            "storage_policy_sha256",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        if self.previous_commit_sha256 is not None:
            object.__setattr__(
                self,
                "previous_commit_sha256",
                _sha(self.previous_commit_sha256, "previous_commit_sha256"),
            )
        _positive_int(self.commit_index, "commit_index")
        start = _positive_int(
            self.event_sequence_from_exclusive,
            "event_sequence_from_exclusive",
            allow_zero=True,
        )
        through = _positive_int(
            self.event_sequence_through,
            "event_sequence_through",
            allow_zero=True,
        )
        refs = tuple(sorted(self.event_refs, key=lambda ref: ref.sequence))
        chunks = tuple(sorted(self.event_chunks, key=lambda ref: ref.relative_path))
        gap_chunks = tuple(sorted(self.gap_chunks, key=lambda ref: ref.relative_path))
        ortex_manifest_refs = tuple(
            sorted(self.ortex_manifest_refs, key=lambda ref: ref.sha256)
        )
        gaps = tuple(sorted((dict(row) for row in self.gaps), key=canonical_json_bytes))
        if any(type(ref) is not CaptureEventRef for ref in refs):
            _fail("queue commit event refs are malformed")
        if refs:
            sequences = [ref.sequence for ref in refs]
            if sequences != list(range(start + 1, through + 1)):
                _fail("queue commit event range is not contiguous")
        elif through != start:
            _fail("empty queue commit cannot advance event frontier")
        if len(chunks) == 0 and refs:
            _fail("queue commit lacks event chunks")
        if chunks and not refs:
            _fail("queue commit has event chunks without event refs")
        if bool(gaps) != bool(gap_chunks):
            _fail("queue commit gap rows/chunks do not agree")
        if (
            any(
                type(ref) is not RetentionObjectRef or ref.tier != "derived"
                for ref in ortex_manifest_refs
            )
            or len({ref.sha256 for ref in ortex_manifest_refs})
            != len(ortex_manifest_refs)
        ):
            _fail("queue commit Ortex manifest refs are malformed or duplicated")
        if self.poisoned != bool(gaps):
            _fail("queue commit poison state does not match durable gaps")
        if self.poisoned:
            if self.poison_reason is None:
                _fail("poisoned queue commit lacks a reason")
            object.__setattr__(self, "poison_reason", _reason(self.poison_reason))
        elif self.poison_reason is not None:
            _fail("clean queue commit carries a poison reason")
        watermark = _utc(self.watermark_at, "commit watermark_at")
        committed = _utc(self.committed_at, "commit committed_at")
        if watermark > committed:
            _fail("queue commit watermark is in the future")
        object.__setattr__(self, "watermark_at", watermark)
        object.__setattr__(self, "committed_at", committed)
        object.__setattr__(self, "event_refs", refs)
        object.__setattr__(self, "event_chunks", chunks)
        object.__setattr__(self, "gap_chunks", gap_chunks)
        object.__setattr__(self, "ortex_manifest_refs", ortex_manifest_refs)
        object.__setattr__(self, "gaps", gaps)

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "queue_identity_sha256": self.queue_identity_sha256,
            "selection_authority_sha256": self.selection_authority_sha256,
            "expected_account_id": self.expected_account_id,
            "activation_generation": self.activation_generation,
            "commit_index": self.commit_index,
            "previous_commit_sha256": self.previous_commit_sha256,
            "event_sequence_from_exclusive": self.event_sequence_from_exclusive,
            "event_sequence_through": self.event_sequence_through,
            "event_refs": [ref.to_dict() for ref in self.event_refs],
            "event_chunks": [_chunk_dict(ref) for ref in self.event_chunks],
            "gaps": [dict(row) for row in self.gaps],
            "gap_chunks": [_chunk_dict(ref) for ref in self.gap_chunks],
            "ortex_manifest_refs": [
                ref.to_dict() for ref in self.ortex_manifest_refs
            ],
            "poisoned": self.poisoned,
            "poison_reason": self.poison_reason,
            "watermark_at": _iso(self.watermark_at),
            "committed_at": _iso(self.committed_at),
            "cumulative_sha256": self.cumulative_sha256,
            "resource_binding_sha256": self.resource_binding_sha256,
            "storage_policy_sha256": self.storage_policy_sha256,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CapturedPaperSelectionQueueCommit":
        expected = {
            "schema_version",
            "queue_identity_sha256",
            "selection_authority_sha256",
            "expected_account_id",
            "activation_generation",
            "commit_index",
            "previous_commit_sha256",
            "event_sequence_from_exclusive",
            "event_sequence_through",
            "event_refs",
            "event_chunks",
            "gaps",
            "gap_chunks",
            "ortex_manifest_refs",
            "poisoned",
            "poison_reason",
            "watermark_at",
            "committed_at",
            "cumulative_sha256",
            "resource_binding_sha256",
            "storage_policy_sha256",
        }
        _exact_fields(raw, expected, "queue commit")
        raw_refs = raw.get("event_refs")
        raw_chunks = raw.get("event_chunks")
        raw_gaps = raw.get("gaps")
        raw_gap_chunks = raw.get("gap_chunks")
        raw_ortex_manifest_refs = raw.get("ortex_manifest_refs")
        if not all(
            isinstance(value, list)
            for value in (
                raw_refs,
                raw_chunks,
                raw_gaps,
                raw_gap_chunks,
                raw_ortex_manifest_refs,
            )
        ):
            _fail("queue commit arrays are malformed")
        return cls(
            schema_version=str(raw.get("schema_version") or ""),
            queue_identity_sha256=str(raw.get("queue_identity_sha256") or ""),
            selection_authority_sha256=str(
                raw.get("selection_authority_sha256") or ""
            ),
            expected_account_id=str(raw.get("expected_account_id") or ""),
            activation_generation=str(raw.get("activation_generation") or ""),
            commit_index=raw.get("commit_index"),
            previous_commit_sha256=raw.get("previous_commit_sha256"),
            event_sequence_from_exclusive=raw.get(
                "event_sequence_from_exclusive"
            ),
            event_sequence_through=raw.get("event_sequence_through"),
            event_refs=tuple(
                CaptureEventRef.from_dict(_mapping(value, "queue event ref"))
                for value in raw_refs
            ),
            event_chunks=tuple(
                _chunk_from_dict(_mapping(value, "queue event chunk"))
                for value in raw_chunks
            ),
            gaps=tuple(_mapping(value, "queue gap") for value in raw_gaps),
            gap_chunks=tuple(
                _chunk_from_dict(_mapping(value, "queue gap chunk"))
                for value in raw_gap_chunks
            ),
            ortex_manifest_refs=tuple(
                _retention_ref_from_dict(
                    _mapping(value, "queue Ortex manifest ref")
                )
                for value in raw_ortex_manifest_refs
            ),
            poisoned=raw.get("poisoned"),
            poison_reason=raw.get("poison_reason"),
            watermark_at=_parse_utc(raw.get("watermark_at"), "commit watermark_at"),
            committed_at=_parse_utc(raw.get("committed_at"), "commit committed_at"),
            cumulative_sha256=str(raw.get("cumulative_sha256") or ""),
            resource_binding_sha256=str(
                raw.get("resource_binding_sha256") or ""
            ),
            storage_policy_sha256=str(
                raw.get("storage_policy_sha256") or ""
            ),
        )


@dataclass(frozen=True, slots=True)
class _LoadedCommit:
    object_ref: RetentionObjectRef
    commit: CapturedPaperSelectionQueueCommit


@dataclass(frozen=True, slots=True)
class _PreparedCommit:
    loaded: _LoadedCommit


def _expected_cumulative(
    previous_cumulative_sha256: str | None,
    *,
    commit_index: int,
    event_refs: Sequence[CaptureEventRef],
    gaps: Sequence[Mapping[str, Any]],
    ortex_manifest_refs: Sequence[RetentionObjectRef],
) -> str:
    return sha256_json(
        {
            "previous_cumulative_sha256": previous_cumulative_sha256,
            "commit_index": commit_index,
            "event_refs": [ref.to_dict() for ref in event_refs],
            "gaps": [dict(row) for row in gaps],
            "ortex_manifest_refs": [
                ref.to_dict() for ref in ortex_manifest_refs
            ],
        }
    )


def _commit_paths(
    root: Path,
    identity: CaptureRunIdentity,
    *,
    max_commit_files: int,
    budget_check: Callable[[], None] | None = None,
) -> tuple[Path, ...]:
    pattern = (
        f"date=*/run={identity.run_id}/generation={identity.generation}/*.json"
    )
    paths: list[Path] = []
    for path in (root / "derived").glob(pattern):
        if budget_check is not None:
            budget_check()
        paths.append(path)
        if len(paths) > max_commit_files:
            _fail("queue commit inventory exceeds bounded scan limit")
    if budget_check is not None:
        budget_check()
    return tuple(sorted(paths))


def _load_commit_chain(
    root: str | Path,
    *,
    identity: CaptureRunIdentity,
    selection_authority: CapturedPaperSelectionAuthority,
    max_commit_files: int = 100_000,
    budget_check: Callable[[], None] | None = None,
) -> tuple[_LoadedCommit, ...]:
    resolved = Path(root).resolve()
    paths = _commit_paths(
        resolved,
        identity,
        max_commit_files=max_commit_files,
        budget_check=budget_check,
    )
    loaded: list[_LoadedCommit] = []
    for path in paths:
        if budget_check is not None:
            budget_check()
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise CapturedPaperSelectionQueueError(
                "queue commit inventory changed during scan"
            ) from exc
        digest = hashlib.sha256(raw).hexdigest()
        if path.stem != digest:
            _fail("derived queue object filename hash mismatch")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CapturedPaperSelectionQueueError(
                "derived queue object is invalid JSON"
            ) from exc
        if not isinstance(value, Mapping) or canonical_json_bytes(value) != raw:
            _fail("derived queue object is not canonical JSON")
        if value.get("kind") != QUEUE_DERIVED_KIND:
            continue
        ref = RetentionObjectRef(
            tier="derived",
            relative_path=path.relative_to(resolved).as_posix(),
            sha256=digest,
            bytes=len(raw),
        )
        verified = ContentAddressedCaptureStore.read_derived_ref(resolved, ref)
        if (
            verified.get("schema_version")
            != CAPTURE_DERIVED_ARTIFACT_SCHEMA_VERSION
            or verified.get("identity") != identity.to_dict()
            or verified.get("kind") != QUEUE_DERIVED_KIND
            or sha256_json(_mapping(verified.get("payload"), "queue commit payload"))
            != verified.get("payload_sha256")
        ):
            _fail("derived queue commit wrapper is invalid")
        commit = CapturedPaperSelectionQueueCommit.from_dict(
            _mapping(verified.get("payload"), "queue commit payload")
        )
        if (
            commit.queue_identity_sha256 != identity.identity_sha256
            or commit.selection_authority_sha256
            != selection_authority.authority_sha256
            or commit.expected_account_id != selection_authority.expected_account_id
            or commit.activation_generation
            != selection_authority.activation_generation
        ):
            _fail("queue commit authority/identity binding mismatch")
        loaded.append(_LoadedCommit(object_ref=ref, commit=commit))

    by_index: dict[int, _LoadedCommit] = {}
    for row in loaded:
        if row.commit.commit_index in by_index:
            _fail("queue commit chain forks at one commit index")
        by_index[row.commit.commit_index] = row
    if not by_index:
        return ()
    if sorted(by_index) != list(range(1, len(by_index) + 1)):
        _fail("queue commit chain has a missing index")
    chain = tuple(by_index[index] for index in range(1, len(by_index) + 1))
    prior_object: str | None = None
    prior_cumulative: str | None = None
    through = 0
    poisoned = False
    resource_binding: str | None = None
    storage_policy: str | None = None
    for row in chain:
        if budget_check is not None:
            budget_check()
        commit = row.commit
        if commit.previous_commit_sha256 != prior_object:
            _fail("queue commit previous-object chain is broken")
        if commit.event_sequence_from_exclusive != through:
            _fail("queue commit source frontier is not contiguous")
        if poisoned and (commit.event_refs or not commit.poisoned):
            _fail("queue commit advances after generation poison")
        expected_cumulative = _expected_cumulative(
            prior_cumulative,
            commit_index=commit.commit_index,
            event_refs=commit.event_refs,
            gaps=commit.gaps,
            ortex_manifest_refs=commit.ortex_manifest_refs,
        )
        if commit.cumulative_sha256 != expected_cumulative:
            _fail("queue commit cumulative hash chain is invalid")
        if resource_binding not in (None, commit.resource_binding_sha256):
            _fail("queue commit resource binding changed within generation")
        if storage_policy not in (None, commit.storage_policy_sha256):
            _fail("queue commit storage policy changed within generation")
        resource_binding = commit.resource_binding_sha256
        storage_policy = commit.storage_policy_sha256
        prior_object = row.object_ref.sha256
        prior_cumulative = commit.cumulative_sha256
        through = commit.event_sequence_through
        poisoned = poisoned or commit.poisoned
    return chain


class CapturedPaperSelectionQueuePublisher:
    """Single-reservation, non-blocking source-worker queue capability."""

    def __init__(
        self,
        *,
        writer_lease: SharedCaptureWriterLease,
        ingress: BoundedCaptureIngress,
        selection_authority: CapturedPaperSelectionAuthority,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(writer_lease, SharedCaptureWriterLease):
            _fail("queue publisher requires an exact shared-store writer lease")
        if not isinstance(ingress, BoundedCaptureIngress):
            _fail("queue publisher ingress is malformed")
        if type(selection_authority) is not CapturedPaperSelectionAuthority:
            _fail("queue selection authority is malformed")
        if not callable(wall_clock) or not callable(monotonic_clock):
            _fail("queue clocks must be callable")
        identity = writer_lease.identity
        if (
            identity.run_id != selection_authority.activation_generation
            # The activation UUID is already the service-wide queue namespace.
            # A second capture generation would create a second allocator and
            # could reuse source sequence one after restart, so this dedicated
            # queue has one canonical physical generation.
            or identity.generation != 1
            or identity.code_build_sha256 != selection_authority.code_build_sha256
            or identity.config_sha256
            != selection_authority.settings_projection_sha256
            or identity.feature_flags_sha256 != selection_authority.policy_sha256
            or identity.broker.strip().lower() != "alpaca"
            or identity.broker_environment.strip().lower() != "paper"
        ):
            _fail("queue identity is not bound to the exact Alpaca PAPER activation")
        store = writer_lease.store
        if (
            ingress.resource_binding != store.resource_binding
            or ingress.shared_admission_budget is None
        ):
            _fail("queue ingress is not bound to shared measured capture resources")
        chain = _load_commit_chain(
            store.root,
            identity=identity,
            selection_authority=selection_authority,
        )
        self.writer_lease = writer_lease
        self.identity = identity
        self.ingress = ingress
        self.store = store
        self.selection_authority = selection_authority
        self.wall_clock = wall_clock
        self.monotonic_clock = monotonic_clock
        self._lock = threading.RLock()
        self._reserved_sequence: int | None = None
        self._active_publish_event_sha256: str | None = None
        self._accepted_through = (
            chain[-1].commit.event_sequence_through if chain else 0
        )
        self._durable_through = self._accepted_through
        self._commit_count = len(chain)
        self._last_commit_sha256 = chain[-1].object_ref.sha256 if chain else None
        self._last_cumulative_sha256 = (
            chain[-1].commit.cumulative_sha256 if chain else None
        )
        self._watermark_at = chain[-1].commit.watermark_at if chain else None
        self._last_durable_monotonic = float(monotonic_clock()) if chain else None
        self._pending_since_monotonic: float | None = None
        self._poisoned = bool(chain and chain[-1].commit.poisoned)
        self._poison_reason = (
            chain[-1].commit.poison_reason if self._poisoned else None
        )
        self._poison_receipt: CapturedPaperSelectionQueuePoisonReceipt | None = None
        self._pending_ortex_manifests_by_batch: dict[
            str, _PendingOrtexManifest
        ] = {}
        self._pending_ortex_batch_refcounts: dict[str, int] = {}
        self._pending_ortex_batches_by_sequence: dict[int, tuple[str, ...]] = {}
        # A manifest may outlive the sequence that first reserved its retained
        # bytes when a later same-batch publish is still pending.  Track the
        # reservation by content hash rather than by sequence so ACK/rollback
        # races release it exactly once.
        self._pending_ortex_retained_batches: set[str] = set()
        self._durable_gate = CapturedPaperSelectionQueueDurableGate(
            queue_identity_sha256=self.identity.identity_sha256,
            selection_authority_sha256=self.selection_authority.authority_sha256,
            expected_account_id=self.selection_authority.expected_account_id,
            activation_generation=self.selection_authority.activation_generation,
            commit_count=self._commit_count,
            last_commit_sha256=self._last_commit_sha256,
            durable_through=self._durable_through,
            poisoned=self._poisoned,
            poison_reason=self._poison_reason,
            initial_chain=chain,
        )

    @property
    def durable_gate(self) -> CapturedPaperSelectionQueueDurableGate:
        return self._durable_gate

    @property
    def has_outstanding_reservation(self) -> bool:
        """Inspect shutdown ownership without invoking runtime health probes."""

        with self._lock:
            return self._reserved_sequence is not None

    def reserve_sequence(self) -> int:
        """Reserve exactly one sequence before the caller hashes its bundle."""

        with self._lock:
            if self._poisoned:
                _fail("queue generation is poisoned")
            if self._reserved_sequence is not None:
                _fail("queue already has an outstanding sequence reservation")
            self._reserved_sequence = self._accepted_through + 1
            return self._reserved_sequence

    def _finish_publish(
        self,
        *,
        bundle: CapturedViabilityInputBundle,
        event_sha256: str,
        score_result: CapturedViabilityScoreResult,
        accepted: bool,
        rejection_reason: str | None = None,
    ) -> CapturedPaperSelectionQueuePublishReceipt:
        with self._lock:
            if (
                self._reserved_sequence != bundle.source_sequence
                or self._accepted_through + 1 != bundle.source_sequence
                or self._active_publish_event_sha256
                not in (None, event_sha256)
            ):
                _fail("queue publish reservation changed before receipt")
            receipt = CapturedPaperSelectionQueuePublishReceipt(
                source_sequence=bundle.source_sequence,
                bundle_sha256=bundle.bundle_sha256,
                event_sha256=event_sha256,
                score_result=score_result,
                accepted=accepted,
            )
            self._reserved_sequence = None
            self._active_publish_event_sha256 = None
            if not accepted:
                self._rollback_pending_ortex_sequence(bundle.source_sequence)
                self._poisoned = True
                self._poison_reason = (
                    f"queue_ingress_rejected:{rejection_reason}"
                    if rejection_reason is not None
                    else "queue_ingress_rejected"
                )
                return receipt
            if self._accepted_through == self._durable_through:
                pending_at = float(self.monotonic_clock())
                if not math.isfinite(pending_at):
                    _fail("queue monotonic clock returned a non-finite value")
                self._pending_since_monotonic = pending_at
            self._accepted_through = bundle.source_sequence
            return receipt

    def _stage_pending_ortex_sequence(
        self,
        *,
        source_sequence: int,
        pending_ortex: Sequence[_PendingOrtexManifest],
    ) -> tuple[str | None, int]:
        """Bind one queue sequence to bounded, deduplicated Ortex RAM."""

        if source_sequence in self._pending_ortex_batches_by_sequence:
            _fail("queue sequence already owns pending Ortex material")
        retained_key: str | None = None
        retained_bytes = 0
        pending_batch_hashes = tuple(row.batch_sha256 for row in pending_ortex)
        for row in pending_ortex:
            prior = self._pending_ortex_manifests_by_batch.get(row.batch_sha256)
            if prior is None:
                self._pending_ortex_manifests_by_batch[row.batch_sha256] = row
                self._pending_ortex_batch_refcounts[row.batch_sha256] = 1
                retained_key = row.batch_sha256
                retained_bytes = row.payload_bytes
            else:
                if (
                    prior.payload != row.payload
                    or prior.window_start != row.window_start
                    or prior.window_end != row.window_end
                ):
                    _fail("same Ortex batch hash carries different material")
                refcount = self._pending_ortex_batch_refcounts.get(
                    row.batch_sha256
                )
                if refcount is None or refcount <= 0:
                    _fail("pending Ortex manifest refcount is malformed")
                self._pending_ortex_batch_refcounts[row.batch_sha256] = (
                    refcount + 1
                )
        self._pending_ortex_batches_by_sequence[source_sequence] = (
            pending_batch_hashes
        )
        return retained_key, retained_bytes

    def publish_bundle(
        self,
        *,
        bundle: CapturedViabilityInputBundle,
        scoring_authority: CapturedViabilityScoringAuthority,
        evaluation_at: datetime,
        source_events: Sequence[CaptureEvent],
        before_ingress_admission: (
            Callable[[CaptureEvent, int, str | None], None] | None
        ) = None,
    ) -> CapturedPaperSelectionQueuePublishReceipt:
        """Validate, score, and enqueue one complete immutable input envelope."""

        if before_ingress_admission is not None and not callable(
            before_ingress_admission
        ):
            _fail("queue pre-admission callback is not callable")
        with self._lock:
            if self._poisoned:
                _fail("queue generation is poisoned")
            if self._active_publish_event_sha256 is not None:
                _fail("queue already has an active retained publish")
            if type(bundle) is not CapturedViabilityInputBundle:
                _fail("queue bundle is not the exact typed contract")
            if type(scoring_authority) is not CapturedViabilityScoringAuthority:
                _fail("queue scoring authority is not the exact typed contract")
            if self._reserved_sequence is None:
                _fail("queue bundle was built without a sequence reservation")
            if bundle.source_sequence != self._reserved_sequence:
                _fail("queue bundle source sequence differs from reservation")
            if not _authority_matches_selection(
                scoring_authority, self.selection_authority
            ):
                _fail("queue scoring authority differs from selection authority")
            expected_scoring = _expected_scoring_authority(
                bundle,
                self.selection_authority,
            )
            if scoring_authority.to_dict() != expected_scoring.to_dict():
                _fail("queue scoring authority differs from exact bundle authority")
            events = _validate_source_events(bundle, source_events)
            source_identity = events[0].identity
            if (
                any(event.identity != source_identity for event in events)
                or source_identity.identity_sha256
                != bundle.capture_identity_sha256
                or source_identity.run_id
                != self.selection_authority.activation_generation
                or source_identity.generation == self.identity.generation
            ):
                _fail("queue source identity is not a distinct activation generation")
            evaluation = _utc(evaluation_at, "queue evaluation_at")
            now = _utc(self.wall_clock(), "queue wall clock")
            if evaluation > now or bundle.read_at > now:
                _fail("queue bundle/evaluation clock is in the future")
            score_result = score_captured_viability(
                bundle,
                authority=scoring_authority,
                evaluation_at=evaluation,
            )
            source_event_refs = _source_event_refs(events)
            retained_source_events, pending_ortex = (
                _retained_source_event_envelopes(
                    events,
                    queue_source_sequence=bundle.source_sequence,
                )
            )
            if len(pending_ortex) > 1:
                _fail("queue route carries multiple Ortex batch manifests")
            if bundle.source_sequence in self._pending_ortex_batches_by_sequence:
                _fail("queue sequence already owns pending Ortex material")
            pending_batch_hashes = tuple(
                row.batch_sha256 for row in pending_ortex
            )
            for row in pending_ortex:
                prior = self._pending_ortex_manifests_by_batch.get(
                    row.batch_sha256
                )
                if prior is not None and (
                    prior.payload != row.payload
                    or prior.window_start != row.window_start
                    or prior.window_end != row.window_end
                ):
                    _fail("same Ortex batch hash carries different material")
            envelope = {
                "schema_version": QUEUE_EVENT_SCHEMA_VERSION,
                "queue_identity_sha256": self.identity.identity_sha256,
                "selection_authority_sha256": (
                    self.selection_authority.authority_sha256
                ),
                "source_sequence": bundle.source_sequence,
                "bundle": bundle.to_dict(),
                "scoring_authority": scoring_authority.to_dict(),
                "evaluation_at": _iso(evaluation),
                "score_result": score_result.to_dict(),
                "source_event_refs": source_event_refs,
                "source_events": retained_source_events,
                "source_event_inventory_sha256": sha256_json(source_event_refs),
            }
            queue_event = CaptureEvent(
                identity=self.identity,
                sequence=bundle.source_sequence,
                stream=CaptureStream.CAPTURED_VIABILITY_INPUT,
                clocks=CaptureClocks(
                    received_at=bundle.read_at,
                    available_at=now,
                    market_reference_at=bundle.event_at,
                ),
                payload=envelope,
                provider=QUEUE_PROVIDER,
                symbol=bundle.symbol,
            )
            retained_key, retained_bytes = self._stage_pending_ortex_sequence(
                source_sequence=bundle.source_sequence,
                pending_ortex=pending_ortex,
            )
            # Admission owns the final fail-closed pressure/capacity check, but
            # canonical payload sizing and event hashing can take seconds for a
            # full captured selection envelope.  Give the initial-start caller
            # one last bounded pressure wait only after both expensive values
            # are cached, immediately before the non-blocking ingress boundary.
            # Normal hot-path callers supply no callback and remain non-blocking.
            event_size = queue_event.canonical_size_bytes
            event_sha256 = queue_event.event_sha256
            if before_ingress_admission is None:
                try:
                    accepted = self.ingress.submit(
                        queue_event,
                        retained_key=retained_key,
                        retained_bytes=retained_bytes,
                    )
                except Exception:
                    self._rollback_pending_ortex_sequence(
                        bundle.source_sequence
                    )
                    raise
                if accepted and retained_key is not None:
                    self._pending_ortex_retained_batches.add(retained_key)
                return self._finish_publish(
                    bundle=bundle,
                    event_sha256=event_sha256,
                    score_result=score_result,
                    accepted=accepted,
                )
            self._active_publish_event_sha256 = event_sha256

        rejection_reason: str | None = None
        last_attempt = None
        while True:
            try:
                before_ingress_admission(
                    queue_event,
                    event_size,
                    rejection_reason,
                )
            except BaseException:
                with self._lock:
                    if last_attempt is not None:
                        self.ingress.finalize_retained_rejection(
                            queue_event,
                            last_attempt,
                        )
                        self._reserved_sequence = None
                        self._poisoned = True
                        self._poison_reason = (
                            "queue_ingress_rejected:"
                            f"{last_attempt.rejection_reason}"
                        )
                    self._rollback_pending_ortex_sequence(
                        bundle.source_sequence
                    )
                    self._active_publish_event_sha256 = None
                raise
            with self._lock:
                if (
                    self._poisoned
                    or self._active_publish_event_sha256 != event_sha256
                    or self._reserved_sequence != bundle.source_sequence
                    or self._accepted_through + 1 != bundle.source_sequence
                ):
                    if last_attempt is not None:
                        self.ingress.finalize_retained_rejection(
                            queue_event,
                            last_attempt,
                        )
                        self._poisoned = True
                        self._poison_reason = (
                            "queue_ingress_rejected:"
                            f"{last_attempt.rejection_reason}"
                        )
                    self._rollback_pending_ortex_sequence(
                        bundle.source_sequence
                    )
                    self._active_publish_event_sha256 = None
                    _fail("queue publish reservation changed during admission")
                try:
                    attempt = self.ingress.try_submit_retained(
                        queue_event,
                        retained_key=retained_key,
                        retained_bytes=retained_bytes,
                    )
                except Exception:
                    self._rollback_pending_ortex_sequence(
                        bundle.source_sequence
                    )
                    self._active_publish_event_sha256 = None
                    raise
                if attempt.accepted:
                    if retained_key is not None:
                        self._pending_ortex_retained_batches.add(retained_key)
                    return self._finish_publish(
                        bundle=bundle,
                        event_sha256=event_sha256,
                        score_result=score_result,
                        accepted=True,
                    )
                last_attempt = attempt
                if not attempt.retryable:
                    self.ingress.finalize_retained_rejection(
                        queue_event,
                        attempt,
                    )
                    return self._finish_publish(
                        bundle=bundle,
                        event_sha256=event_sha256,
                        score_result=score_result,
                        accepted=False,
                        rejection_reason=attempt.rejection_reason,
                    )
                rejection_reason = attempt.rejection_reason

    def _rollback_pending_ortex_sequence(self, source_sequence: int) -> None:
        batch_hashes = self._pending_ortex_batches_by_sequence.pop(
            source_sequence, ()
        )
        for batch_sha256 in batch_hashes:
            refcount = self._pending_ortex_batch_refcounts.get(batch_sha256)
            if refcount is None or refcount <= 0:
                _fail("pending Ortex manifest refcount is malformed")
            if refcount == 1:
                self._pending_ortex_batch_refcounts.pop(batch_sha256)
                pending = self._pending_ortex_manifests_by_batch.pop(
                    batch_sha256, None
                )
                if pending is None:
                    _fail("pending Ortex manifest material is missing")
                if batch_sha256 in self._pending_ortex_retained_batches:
                    self.ingress.release_retained(
                        identity_sha256=self.identity.identity_sha256,
                        retained_key=batch_sha256,
                        expected_bytes=pending.payload_bytes,
                    )
                    self._pending_ortex_retained_batches.remove(batch_sha256)
            else:
                self._pending_ortex_batch_refcounts[batch_sha256] = refcount - 1

    def _release_pending_ortex_after_writer_failure(self) -> None:
        """Release bounded RAM only after the writer is terminally fenced.

        Failed writer events are never retried in this publisher instance.
        Their orphaned content-addressed files remain uncommitted and invisible;
        a fresh publisher may safely reuse those exact bytes on restart.
        """

        with self._lock:
            pending = tuple(
                row
                for row in self._pending_ortex_manifests_by_batch.values()
                if row.batch_sha256 in self._pending_ortex_retained_batches
            )
            for row in pending:
                self.ingress.release_retained(
                    identity_sha256=self.identity.identity_sha256,
                    retained_key=row.batch_sha256,
                    expected_bytes=row.payload_bytes,
                )
            self._pending_ortex_manifests_by_batch.clear()
            self._pending_ortex_batch_refcounts.clear()
            self._pending_ortex_batches_by_sequence.clear()
            self._pending_ortex_retained_batches.clear()

    def heartbeat(self, *, watermark_at: datetime) -> CapturedPaperSelectionQueueHealth:
        with self._lock:
            watermark = _utc(watermark_at, "queue heartbeat watermark")
            now = _utc(self.wall_clock(), "queue heartbeat wall clock")
            if watermark > now:
                _fail("queue heartbeat watermark is in the future")
            # Source cohorts are sequenced by durable ingestion, not by their
            # market-reference clocks.  A later cohort can therefore carry a
            # slightly older valid event time.  The queue watermark is the
            # observed high-water mark: retain it instead of poisoning an
            # otherwise complete batch for normal event-time reordering.
            if self._watermark_at is None or watermark > self._watermark_at:
                self._watermark_at = watermark
            return self.health()

    def poison(self, reason: str) -> CapturedPaperSelectionQueuePoisonReceipt:
        normalized = _reason(reason)
        with self._lock:
            if self._active_publish_event_sha256 is not None:
                _fail("queue poison cannot race an active retained publish")
            if self._poison_receipt is not None:
                if self._poison_receipt.reason != normalized:
                    _fail("queue poison reason changed after terminalization")
                return self._poison_receipt
            now = _utc(self.wall_clock(), "queue poison wall clock")
            reserved = self._reserved_sequence
            self._reserved_sequence = None
            self._active_publish_event_sha256 = None
            self._poisoned = True
            self._poison_reason = normalized
            accepted = self.ingress.submit_gap(
                self.identity,
                CoverageGap(
                    stream=CaptureStream.CAPTURED_VIABILITY_INPUT,
                    reason=normalized,
                    first_available_at=now,
                    last_available_at=now,
                    lost_count=1,
                ),
            )
            receipt = CapturedPaperSelectionQueuePoisonReceipt(
                reason=normalized,
                observed_at=now,
                source_sequence=reserved,
                accepted_by_gap_ledger=accepted,
            )
            self._poison_receipt = receipt
            return receipt

    def _prepare_commit(
        self,
        *,
        store: ContentAddressedCaptureStore,
        batch: IngressBatch,
        event_chunks: tuple[ChunkRef, ...],
        gap_chunks: tuple[ChunkRef, ...],
    ) -> _PreparedCommit:
        with self._lock:
            if store is not self.store:
                _fail("queue committer received a foreign store")
            events = tuple(sorted(batch.events, key=lambda event: event.sequence))
            if any(
                event.identity != self.identity
                or event.stream is not CaptureStream.CAPTURED_VIABILITY_INPUT
                for event in events
            ):
                _fail("queue writer batch escaped identity/stream boundary")
            expected = self._durable_through + 1
            if events and [event.sequence for event in events] != list(
                range(expected, expected + len(events))
            ):
                _fail("queue writer batch is not contiguous with durable frontier")
            gap_rows = tuple(
                sorted(
                    (
                        {
                            "schema_version": CAPTURE_SCHEMA_VERSION,
                            "identity": identity.to_dict(),
                            "gap": gap.to_dict(),
                        }
                        for identity, gap in batch.gaps
                    ),
                    key=canonical_json_bytes,
                )
            )
            if any(identity != self.identity for identity, _gap in batch.gaps):
                _fail("queue writer gap escaped identity boundary")
            if not events and not gap_rows:
                _fail("queue committer received an empty batch")
            now = _utc(self.wall_clock(), "queue commit wall clock")
            candidates = [event.clocks.available_at for event in events]
            candidates.extend(
                gap.last_available_at for _identity, gap in batch.gaps
            )
            window_start_candidates = [event.clocks.received_at for event in events]
            window_start_candidates.extend(
                gap.first_available_at for _identity, gap in batch.gaps
            )
            watermark_candidates = [
                event.clocks.market_reference_at or event.clocks.available_at
                for event in events
            ]
            if self._watermark_at is not None:
                watermark_candidates.append(self._watermark_at)
            watermark = max(watermark_candidates or candidates or [now])
            if watermark > now:
                _fail("queue commit watermark is in the future")
            refs = tuple(CaptureEventRef.from_event(event) for event in events)
            pending_by_batch: dict[str, _PendingOrtexManifest] = {}
            for event in events:
                raw_event = _mapping(event.payload, "queue event payload")
                raw_sources = raw_event.get("source_events")
                if not isinstance(raw_sources, list):
                    _fail("queue source event inventory is malformed")
                compact_batch_hashes = []
                for raw_source in raw_sources:
                    source_envelope = _mapping(
                        raw_source, "source event envelope"
                    )
                    pointer = source_envelope.get("ortex_manifest_ref")
                    if pointer is None:
                        continue
                    pointer = _mapping(pointer, "Ortex manifest ref")
                    _exact_fields(
                        pointer,
                        {
                            "schema_version",
                            "kind",
                            "batch_sha256",
                            "window_start",
                            "window_end",
                        },
                        "Ortex manifest ref",
                    )
                    if (
                        pointer.get("schema_version")
                        != QUEUE_ORTEX_MANIFEST_REF_SCHEMA_VERSION
                        or pointer.get("kind")
                        != QUEUE_ORTEX_MANIFEST_DERIVED_KIND
                    ):
                        _fail("Ortex manifest ref schema/kind mismatch")
                    compact_batch_hashes.append(
                        _sha(
                            pointer.get("batch_sha256"),
                            "Ortex manifest ref batch SHA256",
                        )
                    )
                pending_hashes = self._pending_ortex_batches_by_sequence.get(
                    event.sequence
                )
                if pending_hashes is None:
                    _fail("queue writer lost pending Ortex manifest ownership")
                if compact_batch_hashes != list(pending_hashes):
                    _fail("queue compact/full Ortex manifest inventory mismatch")
                for batch_sha256 in pending_hashes:
                    row = self._pending_ortex_manifests_by_batch.get(
                        batch_sha256
                    )
                    if row is None:
                        _fail("queue writer lost content-addressed Ortex material")
                    prior = pending_by_batch.get(batch_sha256)
                    if prior is not None and (
                        prior.payload != row.payload
                        or prior.window_start != row.window_start
                        or prior.window_end != row.window_end
                    ):
                        _fail("same Ortex batch hash carries different material")
                    pending_by_batch[batch_sha256] = row

            ortex_manifest_refs: list[RetentionObjectRef] = []
            for batch_sha256 in sorted(pending_by_batch):
                pending = pending_by_batch[batch_sha256]
                manifest_ref = store.put_derived_artifact(
                    identity=self.identity,
                    kind=QUEUE_ORTEX_MANIFEST_DERIVED_KIND,
                    window_start=pending.window_start,
                    window_end=pending.window_end,
                    payload=pending.payload,
                )
                verified_manifest = ContentAddressedCaptureStore.read_derived_ref(
                    store.root,
                    manifest_ref,
                )
                if (
                    verified_manifest.get("identity") != self.identity.to_dict()
                    or verified_manifest.get("kind")
                    != QUEUE_ORTEX_MANIFEST_DERIVED_KIND
                    or verified_manifest.get("window_start")
                    != _iso(pending.window_start)
                    or verified_manifest.get("window_end")
                    != _iso(pending.window_end)
                    or verified_manifest.get("payload_sha256") != batch_sha256
                    or not isinstance(verified_manifest.get("payload"), Mapping)
                    or sha256_json(verified_manifest["payload"]) != batch_sha256
                ):
                    _fail("persisted Ortex manifest wrapper is invalid")
                # The queue commit must never become durable before the
                # immutable manifest it pins.  This exact-ref fsync also
                # handles an idempotent restart which found an orphan created
                # before the prior process could flush it.
                store.sync_derived_ref(manifest_ref)
                ortex_manifest_refs.append(manifest_ref)
            immutable_ortex_refs = tuple(
                sorted(ortex_manifest_refs, key=lambda ref: ref.sha256)
            )
            index = self._commit_count + 1
            cumulative = _expected_cumulative(
                self._last_cumulative_sha256,
                commit_index=index,
                event_refs=refs,
                gaps=gap_rows,
                ortex_manifest_refs=immutable_ortex_refs,
            )
            poison_reason = None
            if gap_rows:
                reasons = sorted(
                    {
                        str(_mapping(row.get("gap"), "queue gap").get("reason") or "")
                        for row in gap_rows
                    }
                )
                poison_reason = (
                    reasons[0] if len(reasons) == 1 else "multiple_capture_gaps"
                )
                poison_reason = _reason(poison_reason)
            commit = CapturedPaperSelectionQueueCommit(
                queue_identity_sha256=self.identity.identity_sha256,
                selection_authority_sha256=(
                    self.selection_authority.authority_sha256
                ),
                expected_account_id=self.selection_authority.expected_account_id,
                activation_generation=self.selection_authority.activation_generation,
                commit_index=index,
                previous_commit_sha256=self._last_commit_sha256,
                event_sequence_from_exclusive=self._durable_through,
                event_sequence_through=(
                    events[-1].sequence if events else self._durable_through
                ),
                event_refs=refs,
                event_chunks=event_chunks,
                gaps=gap_rows,
                gap_chunks=gap_chunks,
                ortex_manifest_refs=immutable_ortex_refs,
                poisoned=bool(gap_rows),
                poison_reason=poison_reason,
                watermark_at=watermark,
                committed_at=now,
                cumulative_sha256=cumulative,
                resource_binding_sha256=(
                    store.resource_binding.binding_sha256
                    if store.resource_binding is not None
                    else ""
                ),
                storage_policy_sha256=store.storage_policy.policy_sha256,
            )
            window_start = min(window_start_candidates or [now])
            window_end = max(candidates or [now])
            ref = store.put_derived_artifact(
                identity=self.identity,
                kind=QUEUE_DERIVED_KIND,
                window_start=window_start,
                window_end=max(window_start, window_end),
                payload=commit.body(),
            )
            return _PreparedCommit(loaded=_LoadedCommit(ref, commit))

    def _acknowledge_commit(self, token: _PreparedCommit) -> None:
        if type(token) is not _PreparedCommit:
            _fail("queue durable commit acknowledgement token is malformed")
        with self._lock:
            loaded = token.loaded
            commit = loaded.commit
            if (
                commit.commit_index != self._commit_count + 1
                or commit.previous_commit_sha256 != self._last_commit_sha256
                or commit.event_sequence_from_exclusive != self._durable_through
            ):
                _fail("queue durable commit acknowledgement is stale")
            self._durable_gate._advance(loaded)
            self._commit_count = commit.commit_index
            self._durable_through = commit.event_sequence_through
            self._last_commit_sha256 = loaded.object_ref.sha256
            self._last_cumulative_sha256 = commit.cumulative_sha256
            self._watermark_at = commit.watermark_at
            release_manifests: list[_PendingOrtexManifest] = []
            for source_sequence in range(
                commit.event_sequence_from_exclusive + 1,
                commit.event_sequence_through + 1,
            ):
                batch_hashes = self._pending_ortex_batches_by_sequence.pop(
                    source_sequence, None
                )
                if batch_hashes is None:
                    _fail("durable commit lost pending Ortex sequence ownership")
                for batch_sha256 in batch_hashes:
                    refcount = self._pending_ortex_batch_refcounts.get(
                        batch_sha256
                    )
                    if refcount is None or refcount <= 0:
                        _fail("pending Ortex manifest refcount is malformed")
                    if refcount == 1:
                        self._pending_ortex_batch_refcounts.pop(batch_sha256)
                        pending = self._pending_ortex_manifests_by_batch.pop(
                            batch_sha256, None
                        )
                        if pending is None:
                            _fail(
                                "durable commit lost content-addressed Ortex material"
                            )
                        release_manifests.append(pending)
                    else:
                        self._pending_ortex_batch_refcounts[batch_sha256] = (
                            refcount - 1
                        )
            for pending in release_manifests:
                if pending.batch_sha256 not in self._pending_ortex_retained_batches:
                    _fail("durable commit lost retained Ortex ownership")
                self.ingress.release_retained(
                    identity_sha256=self.identity.identity_sha256,
                    retained_key=pending.batch_sha256,
                    expected_bytes=pending.payload_bytes,
                )
                self._pending_ortex_retained_batches.remove(
                    pending.batch_sha256
                )
            durable_at = float(self.monotonic_clock())
            if not math.isfinite(durable_at):
                _fail("queue monotonic clock returned a non-finite value")
            self._last_durable_monotonic = durable_at
            if self._durable_through == self._accepted_through:
                self._pending_since_monotonic = None
            if commit.poisoned:
                self._poisoned = True
                self._poison_reason = commit.poison_reason

    def health(self) -> CapturedPaperSelectionQueueHealth:
        with self._lock:
            ingress_health = self.ingress.health()
            failed = bool(
                ingress_health.get("writer_failure_count")
                or ingress_health.get("dropped")
                or ingress_health.get("post_close_submissions")
            )
            poison_reason = self._poison_reason
            if failed and poison_reason is None:
                poison_reason = "capture_runtime_failed_closed"
            lag = max(0, self._accepted_through - self._durable_through)
            lag_seconds = None
            if lag:
                current = float(self.monotonic_clock())
                if not math.isfinite(current):
                    failed = True
                    poison_reason = poison_reason or "queue_monotonic_clock_non_finite"
                elif self._pending_since_monotonic is not None:
                    lag_seconds = max(0.0, current - self._pending_since_monotonic)
            return CapturedPaperSelectionQueueHealth(
                poisoned=self._poisoned or failed,
                poison_reason=poison_reason,
                reserved_sequence=self._reserved_sequence,
                accepted_through=self._accepted_through,
                durable_through=self._durable_through,
                commit_count=self._commit_count,
                last_commit_sha256=self._last_commit_sha256,
                watermark_at=self._watermark_at,
                lag_events=lag,
                lag_seconds=lag_seconds,
                ingress=ingress_health,
            )


class _QueueDurableCommitter(CaptureDurableBatchCommitter):
    def __init__(self, publisher: CapturedPaperSelectionQueuePublisher) -> None:
        self.publisher = publisher

    def prepare_batch(
        self,
        *,
        store: ContentAddressedCaptureStore,
        batch: IngressBatch,
        event_chunks: tuple[ChunkRef, ...],
        gap_chunks: tuple[ChunkRef, ...],
    ) -> _PreparedCommit:
        return self.publisher._prepare_commit(
            store=store,
            batch=batch,
            event_chunks=event_chunks,
            gap_chunks=gap_chunks,
        )

    def acknowledge_batch(self, token: Any) -> None:
        self.publisher._acknowledge_commit(token)


class CapturedPaperSelectionQueueWriter:
    """Shared-resource-counted writer for one queue activation generation."""

    def __init__(
        self,
        *,
        publisher: CapturedPaperSelectionQueuePublisher,
        batch_events: int,
        batch_bytes: int,
        poll_seconds: float = 0.05,
        flush_interval_seconds: float = 0.25,
    ) -> None:
        if type(publisher) is not CapturedPaperSelectionQueuePublisher:
            _fail("queue writer publisher is malformed")
        self.publisher = publisher
        self._worker = publisher.writer_lease.build_writer(
            ingress=publisher.ingress,
            batch_events=batch_events,
            batch_bytes=batch_bytes,
            poll_seconds=poll_seconds,
            flush_interval_seconds=flush_interval_seconds,
            durable_batch_committer=_QueueDurableCommitter(publisher),
        )

    @property
    def worker(self) -> CaptureWriterWorker:
        return self._worker

    def start(self) -> None:
        self._worker.start()

    def stop(self, *, timeout_seconds: float = 10.0) -> bool:
        return self._worker.stop(timeout_seconds=timeout_seconds)

    def close(self, *, timeout_seconds: float = 10.0) -> bool:
        if self.publisher.has_outstanding_reservation:
            self.publisher.poison("queue_shutdown_with_outstanding_reservation")
        stopped = self.stop(timeout_seconds=timeout_seconds)
        worker_health = self._worker.lifecycle_health()
        physically_quiesced_after_failure = bool(
            worker_health.get("has_started")
            and not worker_health["writer_alive"]
            and worker_health.get("last_error")
            and self._worker.ingress.drained
        )
        if (
            not worker_health["writer_alive"]
            and worker_health.get("last_error") is not None
        ):
            self.publisher._release_pending_ortex_after_writer_failure()
        if (
            not worker_health["writer_alive"]
            and self._worker.ingress.drained
        ):
            self.publisher.writer_lease.release()
        return bool(stopped or physically_quiesced_after_failure)

    def health(self) -> dict[str, Any]:
        return {
            "queue": self.publisher.health().to_dict(),
            "writer": self._worker.health(),
        }

    def progress_health(self) -> dict[str, Any]:
        """Bounded health used only while awaiting the fsync frontier."""

        return {
            "queue": self.publisher.health().to_dict(),
            "writer": self._worker.progress_health(),
        }


def _materialize_commit_events(
    root: Path,
    loaded: _LoadedCommit,
) -> tuple[CaptureEvent, ...]:
    commit = loaded.commit
    rows = tuple(
        row
        for chunk in commit.event_chunks
        for row in ContentAddressedCaptureStore.read_chunk_ref(root, chunk)
    )
    events: list[CaptureEvent] = []
    for row in rows:
        payload = row.get("payload")
        payload_ref = row.get("payload_ref")
        if payload is None:
            if not isinstance(payload_ref, str):
                _fail("queue event row lacks an exact payload reference")
            payload = ContentAddressedCaptureStore.read_payload_ref(
                root,
                payload_sha256=str(row.get("payload_sha256") or ""),
                relative_path=payload_ref,
                expected_storage_policy_sha256=commit.storage_policy_sha256,
            )
        event = CaptureEvent.from_record(row, payload=_mapping(payload, "queue payload"))
        if event.event_sha256 != row.get("event_sha256"):
            _fail("queue event row content address mismatch")
        events.append(event)
    events = sorted(events, key=lambda event: event.sequence)
    refs = tuple(CaptureEventRef.from_event(event) for event in events)
    if refs != commit.event_refs:
        _fail("queue committed event chunks differ from commit refs")
    return tuple(events)


def _verify_commit_gaps(root: Path, loaded: _LoadedCommit) -> None:
    commit = loaded.commit
    rows = tuple(
        row
        for chunk in commit.gap_chunks
        for row in ContentAddressedCaptureStore.read_chunk_ref(root, chunk)
    )
    if tuple(sorted((dict(row) for row in rows), key=canonical_json_bytes)) != commit.gaps:
        _fail("queue committed gap chunks differ from commit receipt")


def _verify_queue_event(
    event: CaptureEvent,
    *,
    root: Path,
    loaded_commit: _LoadedCommit,
    queue_identity: CaptureRunIdentity,
    selection_authority: CapturedPaperSelectionAuthority,
    manifest_cache: dict[
        str, tuple[Mapping[str, Any], RetentionObjectRef]
    ],
    budget_check: Callable[[], None],
) -> tuple[CapturedViabilityInputBundle, CapturedViabilityScoreResult]:
    if (
        event.identity != queue_identity
        or event.stream is not CaptureStream.CAPTURED_VIABILITY_INPUT
        or event.provider != QUEUE_PROVIDER
    ):
        _fail("queue event escaped identity/provider/stream boundary")
    raw = _mapping(event.payload, "queue event payload")
    expected = {
        "schema_version",
        "queue_identity_sha256",
        "selection_authority_sha256",
        "source_sequence",
        "bundle",
        "scoring_authority",
        "evaluation_at",
        "score_result",
        "source_event_refs",
        "source_events",
        "source_event_inventory_sha256",
    }
    _exact_fields(raw, expected, "queue event envelope")
    if (
        raw.get("schema_version") != QUEUE_EVENT_SCHEMA_VERSION
        or raw.get("queue_identity_sha256") != queue_identity.identity_sha256
        or raw.get("selection_authority_sha256")
        != selection_authority.authority_sha256
        or raw.get("source_sequence") != event.sequence
    ):
        _fail("queue event envelope binding mismatch")
    bundle = CapturedViabilityInputBundle.from_dict(
        _mapping(raw.get("bundle"), "queue bundle")
    )
    scoring = CapturedViabilityScoringAuthority.from_dict(
        _mapping(raw.get("scoring_authority"), "queue scoring authority")
    )
    expected_scoring = _expected_scoring_authority(
        bundle,
        selection_authority,
    )
    if (
        bundle.source_sequence != event.sequence
        or event.symbol != bundle.symbol
        or event.clocks.market_reference_at != bundle.event_at
        or scoring.to_dict() != expected_scoring.to_dict()
        or not _authority_matches_selection(scoring, selection_authority)
    ):
        _fail("queue bundle/scoring authority binding mismatch")
    raw_refs = raw.get("source_event_refs")
    raw_sources = raw.get("source_events")
    if not isinstance(raw_refs, list):
        _fail("queue source event ref inventory is malformed")
    if raw.get("source_event_inventory_sha256") != sha256_json(raw_refs):
        _fail("queue source event inventory hash mismatch")
    source_refs, source_events = _validate_compact_source_evidence(
        bundle,
        raw_refs=raw_refs,
        raw_events=raw_sources,
        root=root,
        queue_identity=queue_identity,
        loaded_commit=loaded_commit,
        manifest_cache=manifest_cache,
        budget_check=budget_check,
    )
    source_identity = source_events[0].identity
    if (
        any(event.identity != source_identity for event in source_events)
        or any(
            ref.identity_sha256 != source_identity.identity_sha256
            for ref in source_refs
        )
        or source_identity.identity_sha256 != bundle.capture_identity_sha256
        or source_identity.run_id != selection_authority.activation_generation
        or source_identity.generation == queue_identity.generation
    ):
        _fail("queue source identity is not a distinct activation generation")
    evaluation = _parse_utc(raw.get("evaluation_at"), "queue evaluation_at")
    recomputed = score_captured_viability(
        bundle,
        authority=scoring,
        evaluation_at=evaluation,
    )
    if recomputed.to_dict() != _mapping(raw.get("score_result"), "queue score result"):
        _fail("queue score result does not reproduce from committed inputs")
    return bundle, recomputed


class CapturedPaperSelectionQueueInputPort:
    """Read-only committed-chain adapter for CapturedPaperSelectionProducer."""

    network_fallback_allowed = False
    broker_access_allowed = False
    mutation_allowed = False

    def __init__(
        self,
        *,
        root: str | Path,
        queue_identity: CaptureRunIdentity,
        selection_authority: CapturedPaperSelectionAuthority,
        durable_gate: CapturedPaperSelectionQueueDurableGate,
        max_batch_events: int,
        max_batch_bytes: int,
        max_read_seconds: float,
        max_commit_files: int = 100_000,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        resolved = Path(root).resolve()
        if not resolved.is_dir():
            _fail("queue read root must already exist")
        if not isinstance(queue_identity, CaptureRunIdentity):
            _fail("queue read identity is malformed")
        if type(selection_authority) is not CapturedPaperSelectionAuthority:
            _fail("queue read selection authority is malformed")
        if type(durable_gate) is not CapturedPaperSelectionQueueDurableGate:
            _fail("queue reader durable gate is malformed")
        if (
            queue_identity.run_id != selection_authority.activation_generation
            or queue_identity.generation != 1
            or queue_identity.code_build_sha256
            != selection_authority.code_build_sha256
            or queue_identity.config_sha256
            != selection_authority.settings_projection_sha256
            or queue_identity.feature_flags_sha256
            != selection_authority.policy_sha256
            or queue_identity.broker.strip().lower() != "alpaca"
            or queue_identity.broker_environment.strip().lower() != "paper"
        ):
            _fail("queue reader is not bound to exact Alpaca PAPER identity")
        if (
            _positive_int(max_batch_events, "max_batch_events") <= 0
            or _positive_int(max_batch_bytes, "max_batch_bytes") <= 0
            or not math.isfinite(float(max_read_seconds))
            or float(max_read_seconds) <= 0
            or _positive_int(max_commit_files, "max_commit_files") <= 0
        ):
            _fail("queue read limits are invalid")
        if not callable(wall_clock) or not callable(monotonic_clock):
            _fail("queue reader clocks must be callable")
        self.root = resolved
        self.queue_identity = queue_identity
        self.selection_authority = selection_authority
        self.durable_gate = durable_gate
        self.max_batch_events = int(max_batch_events)
        self.max_batch_bytes = int(max_batch_bytes)
        self.max_read_seconds = float(max_read_seconds)
        self.max_commit_files = int(max_commit_files)
        self.wall_clock = wall_clock
        self.monotonic_clock = monotonic_clock
        self._lock = threading.RLock()
        self._last_consumed_sequence = 0
        self._last_committed_sequence = 0
        self._commit_count = 0
        self._last_commit_sha256: str | None = None
        self._durable_watermark_at: datetime | None = None
        self._last_read_at: datetime | None = None
        self._last_error: str | None = None
        # The publisher verifies the sealed chain once at process startup and
        # the durable gate appends each exact post-fsync commit token.  Readers
        # reuse that immutable prefix and independently verify each commit
        # object only when its events first cross the consumer frontier.
        self._verified_chain: list[_LoadedCommit] = []
        self._verified_commit_objects: set[str] = set()
        self._materialized_commit_sha256: str | None = None
        self._materialized_commit_events: tuple[CaptureEvent, ...] = ()

    def _materialize_commit_events_once(
        self,
        loaded: _LoadedCommit,
    ) -> tuple[CaptureEvent, ...]:
        digest = loaded.object_ref.sha256
        with self._lock:
            if self._materialized_commit_sha256 == digest:
                return self._materialized_commit_events
        events = _materialize_commit_events(self.root, loaded)
        with self._lock:
            if self._materialized_commit_sha256 == digest:
                return self._materialized_commit_events
            self._materialized_commit_sha256 = digest
            self._materialized_commit_events = events
            return events

    def _acknowledged_chain(
        self,
        *,
        durable: CapturedPaperSelectionQueueDurableFrontier,
        gate_delta: tuple[_LoadedCommit, ...],
        budget_check: Callable[[], None],
    ) -> list[_LoadedCommit]:
        with self._lock:
            cached = self._verified_chain
            cached_count = len(cached)
            if durable.commit_count < cached_count:
                _fail("queue durable acknowledgement moved backwards")
            if durable.commit_count > self.max_commit_files:
                _fail("queue commit inventory exceeds bounded scan limit")
            if len(gate_delta) != durable.commit_count - cached_count:
                _fail("queue durable acknowledgement delta is inconsistent")
            if durable.commit_count == cached_count:
                cached_hash = cached[-1].object_ref.sha256 if cached else None
                cached_through = (
                    cached[-1].commit.event_sequence_through if cached else 0
                )
                if (
                    gate_delta
                    or cached_hash != durable.last_commit_sha256
                    or cached_through != durable.durable_through
                ):
                    _fail("cached queue prefix differs from durable acknowledgement")
                return cached

            prior_object = cached[-1].object_ref.sha256 if cached else None
            prior_through = (
                cached[-1].commit.event_sequence_through if cached else 0
            )
            for offset, row in enumerate(gate_delta, start=1):
                budget_check()
                commit = row.commit
                if (
                    type(row) is not _LoadedCommit
                    or commit.commit_index != cached_count + offset
                    or commit.previous_commit_sha256 != prior_object
                    or commit.event_sequence_from_exclusive != prior_through
                    or commit.queue_identity_sha256
                    != self.queue_identity.identity_sha256
                    or commit.selection_authority_sha256
                    != self.selection_authority.authority_sha256
                    or commit.expected_account_id
                    != self.selection_authority.expected_account_id
                    or commit.activation_generation
                    != self.selection_authority.activation_generation
                ):
                    _fail("queue durable acknowledgement delta is invalid")
                prior_object = row.object_ref.sha256
                prior_through = commit.event_sequence_through
                # Checkpoint each fully validated immutable token so a bounded
                # timeout resumes at the next delta instead of rescanning the
                # same long startup prefix forever.
                cached.append(row)
            if (
                prior_object != durable.last_commit_sha256
                or prior_through != durable.durable_through
            ):
                _fail("queue durable acknowledgement hash/frontier mismatch")
            return cached

    def _verify_commit_object_once(
        self,
        loaded: _LoadedCommit,
        *,
        budget_check: Callable[[], None],
    ) -> None:
        digest = loaded.object_ref.sha256
        with self._lock:
            if digest in self._verified_commit_objects:
                return
        budget_check()
        verified = ContentAddressedCaptureStore.read_derived_ref(
            self.root,
            loaded.object_ref,
        )
        if (
            verified.get("schema_version")
            != CAPTURE_DERIVED_ARTIFACT_SCHEMA_VERSION
            or verified.get("identity") != self.queue_identity.to_dict()
            or verified.get("kind") != QUEUE_DERIVED_KIND
            or _mapping(verified.get("payload"), "queue commit payload")
            != loaded.commit.body()
            or sha256_json(_mapping(verified.get("payload"), "queue commit payload"))
            != verified.get("payload_sha256")
        ):
            _fail("derived queue commit differs from acknowledged content")
        with self._lock:
            self._verified_commit_objects.add(digest)

    def read_batch(
        self,
        *,
        frontier: CapturedPaperSelectionFrontierReceipt,
        authority: CapturedPaperSelectionAuthority,
    ) -> CapturedPaperSelectionBatch | None:
        if type(frontier) is not CapturedPaperSelectionFrontierReceipt:
            raise CapturedPaperSelectionQueueUnavailable("frontier contract invalid")
        try:
            frontier.verify()
        except Exception as exc:
            raise CapturedPaperSelectionQueueUnavailable(
                "frontier integrity verification failed"
            ) from exc
        if (
            type(authority) is not CapturedPaperSelectionAuthority
            or authority.to_dict() != self.selection_authority.to_dict()
        ):
            raise CapturedPaperSelectionQueueUnavailable(
                "selection authority differs from queue binding"
            )
        if not (
            frontier.account_scope == authority.account_scope
            and frontier.expected_account_id == authority.expected_account_id
            and frontier.activation_generation == authority.activation_generation
            and frontier.execution_family == authority.execution_family
            and frontier.authority_sha256 == authority.authority_sha256
            and frontier.policy_sha256 == authority.policy_sha256
            and frontier.settings_projection_sha256
            == authority.settings_projection_sha256
            and frontier.code_build_sha256 == authority.code_build_sha256
            and frontier.variant_set_sha256 == authority.variant_set_sha256
        ):
            raise CapturedPaperSelectionQueueUnavailable(
                "frontier differs from queue activation authority"
            )
        try:
            started = float(self.monotonic_clock())
            if not math.isfinite(started):
                _fail("queue reader monotonic clock returned a non-finite value")

            def read_budget_exceeded() -> bool:
                current = float(self.monotonic_clock())
                if not math.isfinite(current) or current < started:
                    _fail("queue reader monotonic clock regressed or is non-finite")
                return current - started > self.max_read_seconds

            def budget_check() -> None:
                if read_budget_exceeded():
                    raise CapturedPaperSelectionQueueReadTimeout(
                        "queue committed-chain read exceeded bounded time"
                    )

            # Snapshot and extend the per-port verified prefix atomically.
            # Concurrent reads may share the immutable list after this point,
            # but cannot request overlapping stale deltas and poison the port.
            with self._lock:
                verified_commit_count = len(self._verified_chain)
                durable, gate_delta = self.durable_gate._snapshot_since(
                    verified_commit_count,
                    max_commit_files=self.max_commit_files,
                )
                if (
                    durable.queue_identity_sha256
                    != self.queue_identity.identity_sha256
                    or durable.selection_authority_sha256
                    != self.selection_authority.authority_sha256
                ):
                    _fail("queue durable acknowledgement gate is inconsistent")
                if not durable.commit_count and (
                    durable.last_commit_sha256 is not None
                    or durable.durable_through
                ):
                    _fail("empty queue durable acknowledgement is inconsistent")
                budget_check()
                chain = self._acknowledged_chain(
                    durable=durable,
                    gate_delta=gate_delta,
                    budget_check=budget_check,
                )
            chain_count = durable.commit_count
            if durable.poisoned:
                if (
                    not chain_count
                    or not chain[chain_count - 1].commit.poisoned
                ):
                    _fail("queue durable poison acknowledgement is inconsistent")
                self._verify_commit_object_once(
                    chain[chain_count - 1],
                    budget_check=budget_check,
                )
                _verify_commit_gaps(self.root, chain[chain_count - 1])
                raise CapturedPaperSelectionQueueUnavailable(
                    f"queue generation poisoned: {durable.poison_reason}"
                )
            committed_through = (
                chain[chain_count - 1].commit.event_sequence_through
                if chain_count
                else 0
            )
            if frontier.last_source_sequence > committed_through:
                _fail("consumer frontier is ahead of durable queue frontier")
            selected: list[
                tuple[CaptureEvent, _LoadedCommit, CapturedViabilityInputBundle, CapturedViabilityScoreResult]
            ] = []
            observations: list[CapturedPaperSelectionObservation] = []
            route_state_updates: list[CapturedPaperSelectionRouteStateUpdate] = []
            routes: set[tuple[str, int]] = set()
            used_bytes = 0
            bounded_stop = False
            ortex_manifest_cache: dict[
                str, tuple[Mapping[str, Any], RetentionObjectRef]
            ] = {}
            charged_ortex_refs: set[tuple[str, str, int]] = set()
            first_unread_commit = bisect_right(
                chain,
                frontier.last_source_sequence,
                hi=chain_count,
                key=lambda row: row.commit.event_sequence_through,
            )
            for commit_offset in range(first_unread_commit, chain_count):
                loaded = chain[commit_offset]
                self._verify_commit_object_once(
                    loaded,
                    budget_check=budget_check,
                )
                _verify_commit_gaps(self.root, loaded)
                for event in self._materialize_commit_events_once(loaded):
                    if event.sequence <= frontier.last_source_sequence:
                        continue
                    event_bytes = event.canonical_size_bytes
                    current_ortex_ref_keys = {
                        (ref.sha256, ref.relative_path, ref.bytes)
                        for ref in loaded.commit.ortex_manifest_refs
                    }
                    new_ortex_bytes = sum(
                        ref.bytes
                        for ref in loaded.commit.ortex_manifest_refs
                        if (
                            ref.sha256,
                            ref.relative_path,
                            ref.bytes,
                        )
                        not in charged_ortex_refs
                    )
                    prospective_bytes = (
                        used_bytes + event_bytes + new_ortex_bytes
                    )
                    if not selected and prospective_bytes > self.max_batch_bytes:
                        raise CapturedPaperSelectionQueueUnavailable(
                            "next committed queue event and Ortex manifest "
                            "exceed batch byte limit"
                        )
                    if selected and (
                        len(selected) >= self.max_batch_events
                        or prospective_bytes > self.max_batch_bytes
                    ):
                        bounded_stop = True
                        break
                    bundle, result = _verify_queue_event(
                        event,
                        root=self.root,
                        loaded_commit=loaded,
                        queue_identity=self.queue_identity,
                        selection_authority=self.selection_authority,
                        manifest_cache=ortex_manifest_cache,
                        budget_check=budget_check,
                    )
                    observation = (
                        result.observation if result.status == SCORED else None
                    )
                    route = (bundle.symbol, bundle.variant_id)
                    if selected and route in routes:
                        bounded_stop = True
                        break
                    selected.append((event, loaded, bundle, result))
                    used_bytes = prospective_bytes
                    charged_ortex_refs.update(current_ortex_ref_keys)
                    routes.add(route)
                    if observation is not None:
                        observations.append(observation)
                    result_sha256 = sha256_json(result.to_dict())
                    if result.status not in {SCORED, COVERAGE_UNAVAILABLE}:
                        _fail("queue score result status is unsupported")
                    route_state_updates.append(
                        CapturedPaperSelectionRouteStateUpdate(
                            source_sequence=bundle.source_sequence,
                            source_event_at=bundle.event_at,
                            source_available_at=bundle.available_at,
                            symbol=bundle.symbol,
                            variant_id=bundle.variant_id,
                            state=(
                                ROUTE_ELIGIBLE
                                if observation is not None
                                else ROUTE_COVERAGE_UNAVAILABLE
                            ),
                            evidence_sha256=(
                                observation.observation_sha256
                                if observation is not None
                                else result_sha256
                            ),
                            bundle_sha256=bundle.bundle_sha256,
                            scoring_authority_sha256=str(
                                result.authority_sha256 or ""
                            ),
                            score_result_sha256=result_sha256,
                            reason_codes=result.reasons,
                        )
                    )
                    if read_budget_exceeded():
                        bounded_stop = True
                        break
                if bounded_stop:
                    break
                if read_budget_exceeded():
                    if not selected:
                        raise CapturedPaperSelectionQueueReadTimeout(
                            "queue committed-chain read exceeded bounded time"
                        )
                    break
            with self._lock:
                self._last_committed_sequence = committed_through
                self._commit_count = chain_count
                self._last_commit_sha256 = (
                    chain[chain_count - 1].object_ref.sha256
                    if chain_count
                    else None
                )
                self._durable_watermark_at = (
                    chain[chain_count - 1].commit.watermark_at
                    if chain_count
                    else None
                )
            if not selected:
                with self._lock:
                    self._last_error = None
                return None
            read_at = _utc(self.wall_clock(), "queue read wall clock")
            if any(event.clocks.available_at > read_at for event, *_rest in selected):
                _fail("queue read clock precedes committed availability")
            selected_refs = [
                CaptureEventRef.from_event(event) for event, *_rest in selected
            ]
            commit_hashes = list(
                dict.fromkeys(loaded.object_ref.sha256 for _event, loaded, *_rest in selected)
            )
            through = selected[-1][0].sequence
            queue_receipt_sha256 = sha256_json(
                {
                    "schema_version": "chili.captured-paper-selection-queue-read.v1",
                    "queue_identity_sha256": self.queue_identity.identity_sha256,
                    "selection_authority_sha256": authority.authority_sha256,
                    "source_sequence_from": frontier.last_source_sequence,
                    "source_sequence_through": through,
                    "event_refs": [ref.to_dict() for ref in selected_refs],
                    "commit_sha256s": commit_hashes,
                }
            )
            coverage_receipt_sha256 = sha256_json(
                {
                    "schema_version": "chili.captured-paper-selection-queue-coverage.v1",
                    "queue_receipt_sha256": queue_receipt_sha256,
                    "gap_count": 0,
                    "poisoned": False,
                    "commit_cumulative_sha256": selected[-1][1].commit.cumulative_sha256,
                }
            )
            watermark = max(bundle.event_at for _event, _loaded, bundle, _result in selected)
            batch = CapturedPaperSelectionBatch(
                authority_sha256=authority.authority_sha256,
                expected_frontier=frontier,
                source_name=QUEUE_SOURCE_NAME,
                source_generation=self.queue_identity.run_id,
                queue_receipt_sha256=queue_receipt_sha256,
                coverage_receipt_sha256=coverage_receipt_sha256,
                source_sequence_from=frontier.last_source_sequence,
                source_sequence_through=through,
                watermark_at=watermark,
                read_at=read_at,
                observations=tuple(observations),
                route_state_updates=tuple(route_state_updates),
            )
            with self._lock:
                self._last_consumed_sequence = through
                self._last_read_at = read_at
                self._last_error = None
            return batch
        except CapturedPaperSelectionQueueReadTimeout:
            raise
        except CapturedPaperSelectionQueueUnavailable:
            with self._lock:
                self._last_error = "queue_unavailable"
            raise
        except Exception as exc:
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
            raise CapturedPaperSelectionQueueUnavailable(
                "committed local queue verification failed"
            ) from exc

    def health(
        self, *, consumer_frontier_sequence: int | None = None
    ) -> CapturedPaperSelectionQueueHealth:
        with self._lock:
            durable = self.durable_gate.snapshot()
            consumed = (
                self._last_consumed_sequence
                if consumer_frontier_sequence is None
                else _positive_int(
                    consumer_frontier_sequence,
                    "consumer_frontier_sequence",
                    allow_zero=True,
                )
            )
            committed = max(self._last_committed_sequence, durable.durable_through)
            lag = max(0, committed - consumed)
            lag_seconds = None
            if lag and self._last_read_at is not None:
                now = _utc(self.wall_clock(), "queue health wall clock")
                lag_seconds = max(0.0, (now - self._last_read_at).total_seconds())
            return CapturedPaperSelectionQueueHealth(
                poisoned=durable.poisoned or self._last_error is not None,
                poison_reason=durable.poison_reason or self._last_error,
                reserved_sequence=None,
                accepted_through=committed,
                durable_through=committed,
                commit_count=max(self._commit_count, durable.commit_count),
                last_commit_sha256=(
                    durable.last_commit_sha256 or self._last_commit_sha256
                ),
                watermark_at=self._durable_watermark_at,
                lag_events=lag,
                lag_seconds=lag_seconds,
                ingress=None,
            )


__all__ = [
    "CapturedPaperSelectionQueueCommit",
    "CapturedPaperSelectionQueueDurableFrontier",
    "CapturedPaperSelectionQueueDurableGate",
    "CapturedPaperSelectionQueueError",
    "CapturedPaperSelectionQueueHealth",
    "CapturedPaperSelectionQueueInputPort",
    "CapturedPaperSelectionQueuePoisonReceipt",
    "CapturedPaperSelectionQueuePublishReceipt",
    "CapturedPaperSelectionQueuePublisher",
    "CapturedPaperSelectionQueueReadTimeout",
    "CapturedPaperSelectionQueueWriter",
    "QUEUE_DERIVED_KIND",
    "QUEUE_EVENT_SCHEMA_VERSION",
]
