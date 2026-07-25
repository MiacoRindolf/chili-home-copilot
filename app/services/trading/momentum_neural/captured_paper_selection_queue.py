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

QUEUE_EVENT_SCHEMA_VERSION = "chili.captured-paper-selection-queue-event.v1"
QUEUE_COMMIT_SCHEMA_VERSION = "chili.captured-paper-selection-queue-commit.v1"
QUEUE_RECEIPT_SCHEMA_VERSION = "chili.captured-paper-selection-queue-receipt.v1"
QUEUE_POISON_SCHEMA_VERSION = "chili.captured-paper-selection-queue-poison.v1"
QUEUE_DERIVED_KIND = "captured_paper_selection_queue_commit"
QUEUE_PROVIDER = "captured_viability_adapter"
QUEUE_SOURCE_NAME = "captured_viability_queue"

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


def _event_envelope(event: CaptureEvent) -> dict[str, Any]:
    return {
        "event": event.to_record(include_payload=True),
        "event_sha256": event.event_sha256,
    }


def _event_from_envelope(raw: Mapping[str, Any]) -> CaptureEvent:
    _exact_fields(raw, {"event", "event_sha256"}, "source event envelope")
    event = CaptureEvent.from_record(_mapping(raw.get("event"), "source event"))
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
        commit_count: int,
        last_commit_sha256: str | None,
        durable_through: int,
        poisoned: bool,
        poison_reason: str | None,
    ) -> None:
        self._queue_identity_sha256 = _sha(
            queue_identity_sha256, "durable gate queue identity"
        )
        self._selection_authority_sha256 = _sha(
            selection_authority_sha256, "durable gate selection authority"
        )
        self._lock = threading.RLock()
        self._commit_count = _positive_int(
            commit_count, "durable gate commit count", allow_zero=True
        )
        self._last_commit_sha256 = (
            _sha(last_commit_sha256, "durable gate last commit")
            if last_commit_sha256 is not None
            else None
        )
        self._durable_through = _positive_int(
            durable_through, "durable gate source frontier", allow_zero=True
        )
        if type(poisoned) is not bool:
            _fail("durable gate poison flag must be boolean")
        self._poisoned = poisoned
        self._poison_reason = (
            _reason(poison_reason) if poison_reason is not None else None
        )
        if bool(self._commit_count) != bool(self._last_commit_sha256):
            _fail("durable gate commit count/hash are inconsistent")
        if self._poisoned != bool(self._poison_reason):
            _fail("durable gate poison state is inconsistent")

    def snapshot(self) -> CapturedPaperSelectionQueueDurableFrontier:
        with self._lock:
            return CapturedPaperSelectionQueueDurableFrontier(
                queue_identity_sha256=self._queue_identity_sha256,
                selection_authority_sha256=self._selection_authority_sha256,
                commit_count=self._commit_count,
                last_commit_sha256=self._last_commit_sha256,
                durable_through=self._durable_through,
                poisoned=self._poisoned,
                poison_reason=self._poison_reason,
            )

    def _advance(self, loaded: "_LoadedCommit") -> None:
        if type(loaded) is not _LoadedCommit:
            _fail("durable gate acknowledgement is malformed")
        commit = loaded.commit
        with self._lock:
            if (
                commit.queue_identity_sha256 != self._queue_identity_sha256
                or commit.selection_authority_sha256
                != self._selection_authority_sha256
                or commit.commit_index != self._commit_count + 1
                or commit.event_sequence_from_exclusive != self._durable_through
                or commit.previous_commit_sha256 != self._last_commit_sha256
            ):
                _fail("durable gate acknowledgement is stale or foreign")
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
        if not all(
            isinstance(value, list)
            for value in (raw_refs, raw_chunks, raw_gaps, raw_gap_chunks)
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
) -> str:
    return sha256_json(
        {
            "previous_cumulative_sha256": previous_cumulative_sha256,
            "commit_index": commit_index,
            "event_refs": [ref.to_dict() for ref in event_refs],
            "gaps": [dict(row) for row in gaps],
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
        self._durable_gate = CapturedPaperSelectionQueueDurableGate(
            queue_identity_sha256=self.identity.identity_sha256,
            selection_authority_sha256=self.selection_authority.authority_sha256,
            commit_count=self._commit_count,
            last_commit_sha256=self._last_commit_sha256,
            durable_through=self._durable_through,
            poisoned=self._poisoned,
            poison_reason=self._poison_reason,
        )

    @property
    def durable_gate(self) -> CapturedPaperSelectionQueueDurableGate:
        return self._durable_gate

    def reserve_sequence(self) -> int:
        """Reserve exactly one sequence before the caller hashes its bundle."""

        with self._lock:
            if self._poisoned:
                _fail("queue generation is poisoned")
            if self._reserved_sequence is not None:
                _fail("queue already has an outstanding sequence reservation")
            self._reserved_sequence = self._accepted_through + 1
            return self._reserved_sequence

    def publish_bundle(
        self,
        *,
        bundle: CapturedViabilityInputBundle,
        scoring_authority: CapturedViabilityScoringAuthority,
        evaluation_at: datetime,
        source_events: Sequence[CaptureEvent],
    ) -> CapturedPaperSelectionQueuePublishReceipt:
        """Validate, score, and enqueue one complete immutable input envelope."""

        with self._lock:
            if self._poisoned:
                _fail("queue generation is poisoned")
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
                "source_events": [_event_envelope(event) for event in events],
                "source_event_inventory_sha256": sha256_json(
                    [_event_envelope(event) for event in events]
                ),
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
            accepted = self.ingress.submit(queue_event)
            receipt = CapturedPaperSelectionQueuePublishReceipt(
                source_sequence=bundle.source_sequence,
                bundle_sha256=bundle.bundle_sha256,
                event_sha256=queue_event.event_sha256,
                score_result=score_result,
                accepted=accepted,
            )
            self._reserved_sequence = None
            if not accepted:
                self._poisoned = True
                self._poison_reason = "queue_ingress_rejected"
                return receipt
            if self._accepted_through == self._durable_through:
                pending_at = float(self.monotonic_clock())
                if not math.isfinite(pending_at):
                    _fail("queue monotonic clock returned a non-finite value")
                self._pending_since_monotonic = pending_at
            self._accepted_through = bundle.source_sequence
            return receipt

    def heartbeat(self, *, watermark_at: datetime) -> CapturedPaperSelectionQueueHealth:
        with self._lock:
            watermark = _utc(watermark_at, "queue heartbeat watermark")
            now = _utc(self.wall_clock(), "queue heartbeat wall clock")
            if watermark > now:
                _fail("queue heartbeat watermark is in the future")
            if self._watermark_at is not None and watermark < self._watermark_at:
                _fail("queue heartbeat watermark moved backwards")
            self._watermark_at = watermark
            return self.health()

    def poison(self, reason: str) -> CapturedPaperSelectionQueuePoisonReceipt:
        normalized = _reason(reason)
        with self._lock:
            if self._poison_receipt is not None:
                if self._poison_receipt.reason != normalized:
                    _fail("queue poison reason changed after terminalization")
                return self._poison_receipt
            now = _utc(self.wall_clock(), "queue poison wall clock")
            reserved = self._reserved_sequence
            self._reserved_sequence = None
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
            index = self._commit_count + 1
            cumulative = _expected_cumulative(
                self._last_cumulative_sha256,
                commit_index=index,
                event_refs=refs,
                gaps=gap_rows,
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
        if self.publisher.health().reserved_sequence is not None:
            self.publisher.poison("queue_shutdown_with_outstanding_reservation")
        stopped = self.stop(timeout_seconds=timeout_seconds)
        worker_health = self._worker.health()
        if not worker_health["writer_alive"] and self._worker.ingress.drained:
            self.publisher.writer_lease.release()
        return stopped

    def health(self) -> dict[str, Any]:
        return {
            "queue": self.publisher.health().to_dict(),
            "writer": self._worker.health(),
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
    queue_identity: CaptureRunIdentity,
    selection_authority: CapturedPaperSelectionAuthority,
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
    raw_sources = raw.get("source_events")
    if not isinstance(raw_sources, list):
        _fail("queue source event inventory is malformed")
    if raw.get("source_event_inventory_sha256") != sha256_json(raw_sources):
        _fail("queue source event inventory hash mismatch")
    source_events = tuple(
        _event_from_envelope(_mapping(value, "source event envelope"))
        for value in raw_sources
    )
    _validate_source_events(bundle, source_events)
    source_identity = source_events[0].identity
    if (
        any(event.identity != source_identity for event in source_events)
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
        # Frontier events are immutable content-addressed objects.  Reuse an
        # exact fsync-acknowledged prefix while this reader lives; a new reader
        # (including every process restart) performs full byte verification.
        self._verified_chain: tuple[_LoadedCommit, ...] = ()

    def _acknowledged_chain(
        self,
        *,
        durable: CapturedPaperSelectionQueueDurableFrontier,
        budget_check: Callable[[], None],
    ) -> tuple[_LoadedCommit, ...]:
        with self._lock:
            cached = self._verified_chain
        if durable.commit_count < len(cached):
            _fail("queue durable acknowledgement moved backwards")
        if durable.commit_count == len(cached):
            cached_hash = cached[-1].object_ref.sha256 if cached else None
            cached_through = (
                cached[-1].commit.event_sequence_through if cached else 0
            )
            if (
                cached_hash != durable.last_commit_sha256
                or cached_through != durable.durable_through
            ):
                _fail("cached queue prefix differs from durable acknowledgement")
            return cached

        chain = _load_commit_chain(
            self.root,
            identity=self.queue_identity,
            selection_authority=self.selection_authority,
            max_commit_files=self.max_commit_files,
            budget_check=budget_check,
        )
        if durable.commit_count > len(chain):
            _fail("queue durable acknowledgement exceeds committed chain")
        acknowledged = chain[: durable.commit_count]
        if not acknowledged:
            _fail("non-empty durable acknowledgement has no committed chain")
        last = acknowledged[-1]
        if (
            last.object_ref.sha256 != durable.last_commit_sha256
            or last.commit.event_sequence_through != durable.durable_through
        ):
            _fail("queue durable acknowledgement hash/frontier mismatch")
        # The already-cached prefix must be byte-for-byte the same chain.  This
        # protects against accepting a replacement/fork when the gate advances.
        if tuple(
            row.object_ref.sha256 for row in acknowledged[: len(cached)]
        ) != tuple(row.object_ref.sha256 for row in cached):
            _fail("queue immutable verified prefix changed")
        with self._lock:
            self._verified_chain = acknowledged
        return acknowledged

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
            durable = self.durable_gate.snapshot()

            def budget_check() -> None:
                current = float(self.monotonic_clock())
                if (
                    not math.isfinite(current)
                    or current < started
                    or current - started > self.max_read_seconds
                ):
                    raise CapturedPaperSelectionQueueUnavailable(
                        "queue committed-chain read exceeded bounded time"
                    )

            if (
                durable.queue_identity_sha256
                != self.queue_identity.identity_sha256
                or durable.selection_authority_sha256
                != self.selection_authority.authority_sha256
            ):
                _fail("queue durable acknowledgement gate is inconsistent")
            if not durable.commit_count and (
                durable.last_commit_sha256 is not None or durable.durable_through
            ):
                _fail("empty queue durable acknowledgement is inconsistent")
            chain = self._acknowledged_chain(
                durable=durable,
                budget_check=budget_check,
            )
            if durable.poisoned:
                if not chain or not chain[-1].commit.poisoned:
                    _fail("queue durable poison acknowledgement is inconsistent")
                _verify_commit_gaps(self.root, chain[-1])
                raise CapturedPaperSelectionQueueUnavailable(
                    f"queue generation poisoned: {durable.poison_reason}"
                )
            committed_through = (
                chain[-1].commit.event_sequence_through if chain else 0
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
            for loaded in chain:
                _verify_commit_gaps(self.root, loaded)
                if (
                    loaded.commit.event_sequence_through
                    <= frontier.last_source_sequence
                ):
                    continue
                for event in _materialize_commit_events(self.root, loaded):
                    if event.sequence <= frontier.last_source_sequence:
                        continue
                    bundle, result = _verify_queue_event(
                        event,
                        queue_identity=self.queue_identity,
                        selection_authority=self.selection_authority,
                    )
                    event_bytes = event.canonical_size_bytes
                    if not selected and event_bytes > self.max_batch_bytes:
                        raise CapturedPaperSelectionQueueUnavailable(
                            "next committed queue event exceeds batch byte limit"
                        )
                    observation = (
                        result.observation if result.status == SCORED else None
                    )
                    route = (bundle.symbol, bundle.variant_id)
                    if selected and (
                        len(selected) >= self.max_batch_events
                        or used_bytes + event_bytes > self.max_batch_bytes
                        or route in routes
                    ):
                        bounded_stop = True
                        break
                    selected.append((event, loaded, bundle, result))
                    used_bytes += event_bytes
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
                    if float(self.monotonic_clock()) - started > self.max_read_seconds:
                        bounded_stop = True
                        break
                if bounded_stop:
                    break
                if float(self.monotonic_clock()) - started > self.max_read_seconds:
                    if not selected:
                        raise CapturedPaperSelectionQueueUnavailable(
                            "queue committed-chain read exceeded bounded time"
                        )
                    break
            with self._lock:
                self._last_committed_sequence = committed_through
                self._commit_count = len(chain)
                self._last_commit_sha256 = (
                    chain[-1].object_ref.sha256 if chain else None
                )
                self._durable_watermark_at = (
                    chain[-1].commit.watermark_at if chain else None
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
    "CapturedPaperSelectionQueueWriter",
    "QUEUE_DERIVED_KIND",
    "QUEUE_EVENT_SCHEMA_VERSION",
]
