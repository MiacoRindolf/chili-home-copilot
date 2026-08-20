"""Bounded exact-manifest handoff history for Ortex viability batches.

The scheduler computes one full Ortex field before persisting viability rows in
separate transactions.  Each row carries only a content-addressed compact
reference.  A newer field may therefore become the hub's current manifest
before captured PAPER has consumed a row from the preceding field.  This
module retains those displaced manifests for the complete strict captured
context window without changing either the manifest or compact-reference
schemas.

Publication, lookup, inspection, and preservation helpers are pure.  The
pipeline owns the hub-row lock and transaction, while readers keep using the
existing manifest validator and exact reference binder after lookup.  Separate
fixed-cardinality counters provide process-local observability without writes.
"""

from __future__ import annotations

import copy
import hashlib
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from .replay_capture_contract import canonical_json_bytes


ORTEX_BATCH_STATUS_KEY = "ortex_squeeze_fuel_batch"
ORTEX_HANDOFF_HISTORY_KEY = "ortex_squeeze_fuel_batch_handoff.v1"
ORTEX_HANDOFF_HISTORY_SCHEMA_VERSION = (
    "chili.ortex-squeeze-fuel-batch-handoff.v1"
)

# Captured PAPER rejects generic derived context older than 60 seconds.  Keep
# twice that strict window so a reference at the boundary is not lost to
# scheduler/read timing jitter.  The two independent hard caps bound JSONB row
# growth; an unexpired exact manifest is never silently evicted to make room.
ORTEX_HANDOFF_STRICT_CONTEXT_MAX_AGE_SECONDS = 60
ORTEX_HANDOFF_RETENTION_SECONDS = 120
ORTEX_HANDOFF_MAX_ENTRIES = 32
ORTEX_HANDOFF_MAX_CANONICAL_BYTES = 16 * 1024 * 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HISTORY_FIELDS = {
    "schema_version",
    "retention_seconds",
    "max_entries",
    "max_canonical_bytes",
    "entries",
    "metrics",
}
_ENTRY_FIELDS = {"manifest", "displaced_at"}
_METRIC_FIELDS = {
    "entry_count",
    "canonical_bytes",
    "entries_sha256",
    "pruned_expired_total",
}


class OrtexHandoffReason(str, Enum):
    """Finite internal outcomes for lookup/publication observability."""

    CURRENT_HIT = "current_hit"
    HISTORY_HIT = "history_hit"
    HISTORY_EXPIRED = "history_expired"
    MISSING = "missing"
    REFERENCE_INVALID = "reference_invalid"
    HISTORY_INVALID = "history_invalid"
    CURRENT_MANIFEST_INVALID = "current_manifest_invalid"
    CURRENT_MANIFEST_CONFLICT = "current_manifest_conflict"
    COUNT_CAP_EXCEEDED = "count_cap_exceeded"
    CANONICAL_BYTE_CAP_EXCEEDED = "canonical_byte_cap_exceeded"
    VIABILITY_PUBLICATION_EMPTY = "viability_publication_empty"


class OrtexHandoffHistoryError(ValueError):
    """Typed fail-closed history publication/inspection failure."""

    def __init__(
        self,
        reason: OrtexHandoffReason,
        *,
        metrics: "OrtexHandoffHistoryMetrics | None" = None,
    ) -> None:
        self.reason = reason
        self.metrics = metrics
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class OrtexHandoffHistoryMetrics:
    retention_seconds: int
    max_entries: int
    max_canonical_bytes: int
    entry_count: int
    canonical_bytes: int
    entries_sha256: str
    pruned_expired_total: int


@dataclass(frozen=True, slots=True)
class OrtexHandoffLookup:
    manifest: dict[str, Any] | None
    reason: OrtexHandoffReason
    metrics: OrtexHandoffHistoryMetrics | None
    current_present: bool


@dataclass(frozen=True, slots=True)
class OrtexHandoffPublication:
    local_state: dict[str, Any]
    metrics: OrtexHandoffHistoryMetrics
    changed: bool


@dataclass(frozen=True, slots=True)
class OrtexHandoffRuntimeMetrics:
    """Fixed-cardinality in-process counters; reads never write the database."""

    current_hits: int
    history_hits: int
    misses: int
    invalid_history: int
    cap_rejects: int
    count_cap_rejects: int
    canonical_byte_cap_rejects: int


class _OrtexHandoffRuntimeCounters:
    __slots__ = (
        "_lock",
        "current_hits",
        "history_hits",
        "misses",
        "invalid_history",
        "cap_rejects",
        "count_cap_rejects",
        "canonical_byte_cap_rejects",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current_hits = 0
        self.history_hits = 0
        self.misses = 0
        self.invalid_history = 0
        self.cap_rejects = 0
        self.count_cap_rejects = 0
        self.canonical_byte_cap_rejects = 0

    def note_lookup(self, reason: OrtexHandoffReason) -> None:
        with self._lock:
            if reason is OrtexHandoffReason.CURRENT_HIT:
                self.current_hits += 1
            elif reason is OrtexHandoffReason.HISTORY_HIT:
                self.history_hits += 1
            elif reason is OrtexHandoffReason.HISTORY_INVALID:
                self.invalid_history += 1
            else:
                self.misses += 1

    def note_cap_reject(self, reason: OrtexHandoffReason) -> None:
        if reason not in {
            OrtexHandoffReason.COUNT_CAP_EXCEEDED,
            OrtexHandoffReason.CANONICAL_BYTE_CAP_EXCEEDED,
        }:
            return
        with self._lock:
            self.cap_rejects += 1
            if reason is OrtexHandoffReason.COUNT_CAP_EXCEEDED:
                self.count_cap_rejects += 1
            else:
                self.canonical_byte_cap_rejects += 1

    def snapshot(self) -> OrtexHandoffRuntimeMetrics:
        with self._lock:
            return OrtexHandoffRuntimeMetrics(
                current_hits=self.current_hits,
                history_hits=self.history_hits,
                misses=self.misses,
                invalid_history=self.invalid_history,
                cap_rejects=self.cap_rejects,
                count_cap_rejects=self.count_cap_rejects,
                canonical_byte_cap_rejects=(
                    self.canonical_byte_cap_rejects
                ),
            )


_RUNTIME_COUNTERS = _OrtexHandoffRuntimeCounters()


def note_ortex_handoff_lookup(reason: OrtexHandoffReason) -> None:
    """Record one fixed-label lookup outcome outside the pure lookup helper."""

    _RUNTIME_COUNTERS.note_lookup(reason)


def note_ortex_handoff_cap_reject(reason: OrtexHandoffReason) -> None:
    """Record one fail-closed capacity rejection outside the pure stager."""

    _RUNTIME_COUNTERS.note_cap_reject(reason)


def ortex_handoff_runtime_metrics() -> OrtexHandoffRuntimeMetrics:
    """Expose a typed preactivation/operator snapshot without database writes."""

    return _RUNTIME_COUNTERS.snapshot()


def _exact_positive_int(value: object, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
    if maximum is not None and value > maximum:
        raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
    return value


def _exact_nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
    if value.tzinfo is None or value.utcoffset() is None:
        raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
    return value.astimezone(timezone.utc)


def _parse_displaced_at(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise OrtexHandoffHistoryError(
            OrtexHandoffReason.HISTORY_INVALID
        ) from exc
    return _utc(parsed)


def _manifest_batch_sha256(manifest: object) -> str:
    if not isinstance(manifest, Mapping):
        raise OrtexHandoffHistoryError(
            OrtexHandoffReason.CURRENT_MANIFEST_INVALID
        )
    digest = manifest.get("batch_sha256")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise OrtexHandoffHistoryError(
            OrtexHandoffReason.CURRENT_MANIFEST_INVALID
        )
    return digest


def _entries_canonical_bytes(entries: Mapping[str, Any]) -> int:
    try:
        return len(canonical_json_bytes(entries))
    except Exception as exc:
        raise OrtexHandoffHistoryError(
            OrtexHandoffReason.HISTORY_INVALID
        ) from exc


def _entries_sha256(entries: Mapping[str, Any]) -> str:
    try:
        return hashlib.sha256(canonical_json_bytes(entries)).hexdigest()
    except Exception as exc:
        raise OrtexHandoffHistoryError(
            OrtexHandoffReason.HISTORY_INVALID
        ) from exc


def _parse_history(
    raw: object,
    *,
    observed_at: datetime | None = None,
    authority_retention_seconds: int = ORTEX_HANDOFF_RETENTION_SECONDS,
    authority_max_entries: int = ORTEX_HANDOFF_MAX_ENTRIES,
    authority_max_canonical_bytes: int = ORTEX_HANDOFF_MAX_CANONICAL_BYTES,
) -> tuple[dict[str, dict[str, Any]], OrtexHandoffHistoryMetrics]:
    retained_for = _exact_positive_int(
        authority_retention_seconds,
        maximum=ORTEX_HANDOFF_RETENTION_SECONDS,
    )
    if retained_for < ORTEX_HANDOFF_STRICT_CONTEXT_MAX_AGE_SECONDS:
        raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
    entry_cap = _exact_positive_int(
        authority_max_entries,
        maximum=ORTEX_HANDOFF_MAX_ENTRIES,
    )
    byte_cap = _exact_positive_int(
        authority_max_canonical_bytes,
        maximum=ORTEX_HANDOFF_MAX_CANONICAL_BYTES,
    )
    trusted_observed_at = (
        None if observed_at is None else _utc(observed_at)
    )
    if raw is None:
        metrics = OrtexHandoffHistoryMetrics(
            retention_seconds=retained_for,
            max_entries=entry_cap,
            max_canonical_bytes=byte_cap,
            entry_count=0,
            canonical_bytes=_entries_canonical_bytes({}),
            entries_sha256=_entries_sha256({}),
            pruned_expired_total=0,
        )
        return {}, metrics
    if not isinstance(raw, Mapping) or set(raw) != _HISTORY_FIELDS:
        raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
    if raw.get("schema_version") != ORTEX_HANDOFF_HISTORY_SCHEMA_VERSION:
        raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)

    retention_seconds = _exact_positive_int(
        raw.get("retention_seconds"),
        maximum=ORTEX_HANDOFF_RETENTION_SECONDS,
    )
    if retention_seconds != retained_for:
        raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
    max_entries = _exact_positive_int(
        raw.get("max_entries"),
        maximum=ORTEX_HANDOFF_MAX_ENTRIES,
    )
    max_canonical_bytes = _exact_positive_int(
        raw.get("max_canonical_bytes"),
        maximum=ORTEX_HANDOFF_MAX_CANONICAL_BYTES,
    )
    if max_entries != entry_cap or max_canonical_bytes != byte_cap:
        raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
    raw_entries = raw.get("entries")
    raw_metrics = raw.get("metrics")
    if not isinstance(raw_entries, Mapping) or not isinstance(
        raw_metrics, Mapping
    ):
        raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
    if set(raw_metrics) != _METRIC_FIELDS:
        raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)

    entries: dict[str, dict[str, Any]] = {}
    for digest, raw_entry in raw_entries.items():
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
        if not isinstance(raw_entry, Mapping) or set(raw_entry) != _ENTRY_FIELDS:
            raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
        manifest = raw_entry.get("manifest")
        if not isinstance(manifest, Mapping) or manifest.get(
            "batch_sha256"
        ) != digest:
            raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
        displaced_at = raw_entry.get("displaced_at")
        parsed_displaced_at = _parse_displaced_at(displaced_at)
        if (
            trusted_observed_at is not None
            and parsed_displaced_at > trusted_observed_at
        ):
            raise OrtexHandoffHistoryError(
                OrtexHandoffReason.HISTORY_INVALID
            )
        entries[digest] = {
            "manifest": copy.deepcopy(dict(manifest)),
            "displaced_at": displaced_at,
        }

    canonical_bytes = _entries_canonical_bytes(entries)
    entry_count = len(entries)
    claimed_count = _exact_nonnegative_int(raw_metrics.get("entry_count"))
    claimed_bytes = _exact_nonnegative_int(raw_metrics.get("canonical_bytes"))
    claimed_sha256 = raw_metrics.get("entries_sha256")
    if (
        not isinstance(claimed_sha256, str)
        or _SHA256_RE.fullmatch(claimed_sha256) is None
    ):
        raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
    pruned_total = _exact_nonnegative_int(
        raw_metrics.get("pruned_expired_total")
    )
    entries_sha256 = _entries_sha256(entries)
    if (
        claimed_count != entry_count
        or claimed_bytes != canonical_bytes
        or claimed_sha256 != entries_sha256
    ):
        raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
    if entry_count > max_entries or canonical_bytes > max_canonical_bytes:
        raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
    return entries, OrtexHandoffHistoryMetrics(
        retention_seconds=retention_seconds,
        max_entries=max_entries,
        max_canonical_bytes=max_canonical_bytes,
        entry_count=entry_count,
        canonical_bytes=canonical_bytes,
        entries_sha256=entries_sha256,
        pruned_expired_total=pruned_total,
    )


def inspect_ortex_handoff_history(
    local_state: Mapping[str, Any] | None,
    *,
    observed_at: datetime | None = None,
) -> OrtexHandoffHistoryMetrics:
    """Return verified, typed row-growth metrics for preactivation checks."""

    state = local_state if isinstance(local_state, Mapping) else {}
    _, metrics = _parse_history(
        state.get(ORTEX_HANDOFF_HISTORY_KEY),
        observed_at=observed_at,
    )
    return metrics


def lookup_ortex_handoff_manifest(
    local_state: Mapping[str, Any] | None,
    *,
    batch_sha256: object,
    observed_at: datetime,
) -> OrtexHandoffLookup:
    """Deep-copy the exact current or retained manifest selected by its hash."""

    state = local_state if isinstance(local_state, Mapping) else {}
    current = state.get(ORTEX_BATCH_STATUS_KEY)
    current_present = isinstance(current, Mapping)
    if (
        not isinstance(batch_sha256, str)
        or _SHA256_RE.fullmatch(batch_sha256) is None
    ):
        return OrtexHandoffLookup(
            manifest=None,
            reason=OrtexHandoffReason.REFERENCE_INVALID,
            metrics=None,
            current_present=current_present,
        )
    try:
        trusted_observed_at = _utc(observed_at)
    except OrtexHandoffHistoryError:
        return OrtexHandoffLookup(
            manifest=None,
            reason=OrtexHandoffReason.HISTORY_INVALID,
            metrics=None,
            current_present=current_present,
        )
    if current_present and current.get("batch_sha256") == batch_sha256:
        return OrtexHandoffLookup(
            manifest=copy.deepcopy(dict(current)),
            reason=OrtexHandoffReason.CURRENT_HIT,
            metrics=None,
            current_present=True,
        )
    try:
        entries, metrics = _parse_history(
            state.get(ORTEX_HANDOFF_HISTORY_KEY),
            observed_at=trusted_observed_at,
        )
    except OrtexHandoffHistoryError:
        return OrtexHandoffLookup(
            manifest=None,
            reason=OrtexHandoffReason.HISTORY_INVALID,
            metrics=None,
            current_present=current_present,
        )
    entry = entries.get(batch_sha256)
    if entry is None:
        return OrtexHandoffLookup(
            manifest=None,
            reason=OrtexHandoffReason.MISSING,
            metrics=metrics,
            current_present=current_present,
        )
    retained_at = _parse_displaced_at(entry["displaced_at"])
    if (
        trusted_observed_at - retained_at
    ).total_seconds() > ORTEX_HANDOFF_RETENTION_SECONDS:
        return OrtexHandoffLookup(
            manifest=None,
            reason=OrtexHandoffReason.HISTORY_EXPIRED,
            metrics=metrics,
            current_present=current_present,
        )
    return OrtexHandoffLookup(
        manifest=copy.deepcopy(entry["manifest"]),
        reason=OrtexHandoffReason.HISTORY_HIT,
        metrics=metrics,
        current_present=current_present,
    )


def _history_document(
    entries: Mapping[str, Any],
    *,
    retention_seconds: int,
    max_entries: int,
    max_canonical_bytes: int,
    pruned_expired_total: int,
) -> tuple[dict[str, Any], OrtexHandoffHistoryMetrics]:
    canonical_bytes = _entries_canonical_bytes(entries)
    metrics = OrtexHandoffHistoryMetrics(
        retention_seconds=retention_seconds,
        max_entries=max_entries,
        max_canonical_bytes=max_canonical_bytes,
        entry_count=len(entries),
        canonical_bytes=canonical_bytes,
        entries_sha256=_entries_sha256(entries),
        pruned_expired_total=pruned_expired_total,
    )
    if metrics.entry_count > max_entries:
        raise OrtexHandoffHistoryError(
            OrtexHandoffReason.COUNT_CAP_EXCEEDED,
            metrics=metrics,
        )
    if metrics.canonical_bytes > max_canonical_bytes:
        raise OrtexHandoffHistoryError(
            OrtexHandoffReason.CANONICAL_BYTE_CAP_EXCEEDED,
            metrics=metrics,
        )
    document = {
        "schema_version": ORTEX_HANDOFF_HISTORY_SCHEMA_VERSION,
        "retention_seconds": retention_seconds,
        "max_entries": max_entries,
        "max_canonical_bytes": max_canonical_bytes,
        "entries": copy.deepcopy(dict(entries)),
        "metrics": {
            "entry_count": metrics.entry_count,
            "canonical_bytes": metrics.canonical_bytes,
            "entries_sha256": metrics.entries_sha256,
            "pruned_expired_total": metrics.pruned_expired_total,
        },
    }
    return document, metrics


def stage_ortex_handoff_publication(
    local_state: Mapping[str, Any] | None,
    *,
    manifest: Mapping[str, Any],
    displaced_at: datetime,
    retention_seconds: int = ORTEX_HANDOFF_RETENTION_SECONDS,
    max_entries: int = ORTEX_HANDOFF_MAX_ENTRIES,
    max_canonical_bytes: int = ORTEX_HANDOFF_MAX_CANONICAL_BYTES,
) -> OrtexHandoffPublication:
    """Stage one atomic current-manifest rotation under the caller's row lock.

    Capacity failures raise before returning a replacement local-state object;
    callers must roll back the surrounding hub/viability transaction.
    """

    retained_for = _exact_positive_int(
        retention_seconds,
        maximum=ORTEX_HANDOFF_RETENTION_SECONDS,
    )
    if retained_for < ORTEX_HANDOFF_STRICT_CONTEXT_MAX_AGE_SECONDS:
        raise OrtexHandoffHistoryError(OrtexHandoffReason.HISTORY_INVALID)
    entry_cap = _exact_positive_int(
        max_entries,
        maximum=ORTEX_HANDOFF_MAX_ENTRIES,
    )
    byte_cap = _exact_positive_int(
        max_canonical_bytes,
        maximum=ORTEX_HANDOFF_MAX_CANONICAL_BYTES,
    )
    rotation_at = _utc(displaced_at)
    state = copy.deepcopy(
        dict(local_state) if isinstance(local_state, Mapping) else {}
    )
    new_manifest = copy.deepcopy(dict(manifest))
    new_digest = _manifest_batch_sha256(new_manifest)
    entries, prior_metrics = _parse_history(
        state.get(ORTEX_HANDOFF_HISTORY_KEY),
        observed_at=rotation_at,
        authority_retention_seconds=retained_for,
        authority_max_entries=entry_cap,
        authority_max_canonical_bytes=byte_cap,
    )
    current = state.get(ORTEX_BATCH_STATUS_KEY)
    if isinstance(current, Mapping):
        current_digest = _manifest_batch_sha256(current)
        if current_digest == new_digest:
            try:
                same_canonical_manifest = canonical_json_bytes(
                    current
                ) == canonical_json_bytes(new_manifest)
            except Exception as exc:
                raise OrtexHandoffHistoryError(
                    OrtexHandoffReason.CURRENT_MANIFEST_INVALID
                ) from exc
            if not same_canonical_manifest:
                raise OrtexHandoffHistoryError(
                    OrtexHandoffReason.CURRENT_MANIFEST_CONFLICT
                )
            return OrtexHandoffPublication(
                local_state=state,
                metrics=prior_metrics,
                changed=False,
            )
    elif current is not None:
        raise OrtexHandoffHistoryError(
            OrtexHandoffReason.CURRENT_MANIFEST_INVALID
        )

    entries.pop(new_digest, None)
    if isinstance(current, Mapping):
        current_digest = _manifest_batch_sha256(current)
        entries[current_digest] = {
            "manifest": copy.deepcopy(dict(current)),
            # This clock records loss of current-hub authority.  Manifest
            # decision_at is deliberately never a retention/pruning clock.
            "displaced_at": rotation_at.isoformat(),
        }

    expired: list[str] = []
    for digest, entry in entries.items():
        retained_at = _parse_displaced_at(entry.get("displaced_at"))
        if (rotation_at - retained_at).total_seconds() > retained_for:
            expired.append(digest)
    for digest in expired:
        entries.pop(digest, None)

    history, metrics = _history_document(
        entries,
        retention_seconds=retained_for,
        max_entries=entry_cap,
        max_canonical_bytes=byte_cap,
        pruned_expired_total=(
            prior_metrics.pruned_expired_total + len(expired)
        ),
    )
    state[ORTEX_BATCH_STATUS_KEY] = new_manifest
    state[ORTEX_HANDOFF_HISTORY_KEY] = history
    return OrtexHandoffPublication(
        local_state=state,
        metrics=metrics,
        changed=True,
    )


def preserve_ortex_handoff_state(
    source_local_state: Mapping[str, Any] | None,
    target_local_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Carry current/history keys through a tick with no Ortex publication."""

    source = source_local_state if isinstance(source_local_state, Mapping) else {}
    target = copy.deepcopy(dict(target_local_state))
    for key in (ORTEX_BATCH_STATUS_KEY, ORTEX_HANDOFF_HISTORY_KEY):
        if key in source:
            target[key] = copy.deepcopy(source[key])
    return target
