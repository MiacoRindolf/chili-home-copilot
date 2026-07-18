"""Fail-closed contract for production live-input capture and ReplayV3.

The live momentum FSM consumes more than trades and a sampled quote.  A replay
is certifiable only when every input actually read by a decision is recorded
with both its market/event clock and the instant it became available to CHILI.

This module deliberately contains no provider or broker calls.  It defines the
portable, content-addressed envelope, read receipts, watermarks, gaps, coverage
manifest, and deterministic dual-clock ordering used by both capture and
ReplayV3.  Runtime buffering/storage lives in ``replay_capture_runtime``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import cached_property
import hashlib
import hmac
import json
import math
import re
import secrets
from typing import Any, Iterable, Mapping, Sequence
import uuid


UTC = timezone.utc
CAPTURE_SCHEMA_VERSION = "chili.replay-capture.v1"
FSM_DEPENDENCY_PROFILE_SCHEMA_VERSION = "chili.fsm-dependency-profile.v2"
CAPTURE_PRODUCER_ROSTER_SCHEMA_VERSION = "chili.capture-producer-roster.v1"
CAPTURE_PRODUCER_LIFECYCLE_SCHEMA_VERSION = "chili.capture-producer-lifecycle.v3"
CAPTURE_PRODUCER_LIFECYCLE_PROVIDER = "chili_capture_lifecycle"
FIRST_DIP_FINAL_CAPTURE_FRONTIER_SCHEMA_VERSION = (
    "chili.first-dip-final-capture-frontier.v3"
)
CAPTURE_PROVIDER_REGISTRATION_SCHEMA_VERSION = (
    "chili.capture-provider-registration.v1"
)
CAPTURE_PROVIDER_REGISTRATION_RECORD_SCHEMA_VERSION = (
    "chili.capture-provider-registration-record.v1"
)
CAPTURE_EXTERNAL_PRODUCER_GENERATION_SCHEMA_VERSION = (
    "chili.capture-external-producer-generation.v1"
)
IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION = "chili.replay-v3-input.iqfeed-print.v1"
NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION = "chili.replay-v3-input.nbbo.v1"
ALPACA_NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION = (
    "chili.replay-v3-input.alpaca-paper-nbbo.v2"
)
ALPACA_NBBO_QUOTE_QUERY_SCHEMA_VERSION = (
    "chili.replay-v3-input.alpaca-paper-nbbo-query.v1"
)
IQFEED_L1_SOURCE_PROVENANCE_SCHEMA_VERSION = (
    "chili.capture-source.iqfeed-l1.v1"
)
IQFEED_EXACT_PRINT_SOURCE_PROVENANCE_SCHEMA_VERSION = (
    "chili.capture-source.iqfeed-exact-print.v1"
)
IQFEED_L1_SOURCE_PROVENANCE_FIELD = "_iqfeed_l1_capture"
IQFEED_L2_DELTA_PAYLOAD_SCHEMA_VERSION = (
    "chili.replay-v3-input.iqfeed-l2-delta.v1"
)
IQFEED_L2_CHECKPOINT_PAYLOAD_SCHEMA_VERSION = (
    "chili.replay-v3-input.iqfeed-l2-checkpoint.v1"
)
IQFEED_L2_SOURCE_PROVENANCE_SCHEMA_VERSION = (
    "chili.capture-source.iqfeed-l2.v1"
)
IQFEED_L2_SOURCE_PROVENANCE_FIELD = "_iqfeed_l2_capture"
MICROSTRUCTURE_READ_QUERY_SCHEMA_VERSION = (
    "chili.replay-v3-input.microstructure-read-query.v1"
)
PROVIDER_OHLCV_QUERY_SCHEMA_VERSION = (
    "chili.replay-v3-input.ohlcv-query.v1"
)
PROVIDER_OHLCV_PAYLOAD_SCHEMA_VERSION = (
    "chili.replay-v3-input.ohlcv-result.v1"
)
SCANNER_SNAPSHOT_QUERY_SCHEMA_VERSION = (
    "chili.replay-v3-input.scanner-snapshot-query.v1"
)
SCANNER_SNAPSHOT_PAYLOAD_SCHEMA_VERSION = (
    "chili.replay-v3-input.scanner-snapshot.v2"
)
SCANNER_SNAPSHOT_PROVIDER = "massive_rest_scanner"
SCANNER_SNAPSHOT_PROVIDER_OPERATION = "get_full_market_snapshot"
SCANNER_SNAPSHOT_PROVIDER_MIN_CACHE_TTL_SECONDS = 60.0
SCANNER_SNAPSHOT_PROVIDER_MAX_CACHE_TTL_SECONDS = 1_800.0
_FIRST_DIP_TAPE_PURPOSE_DETECTOR = "detector"
_FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION = "pre_reservation"
_FIRST_DIP_TAPE_PURPOSES = frozenset(
    {
        _FIRST_DIP_TAPE_PURPOSE_DETECTOR,
        _FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION,
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PRODUCER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_LIFECYCLE_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


class _FrozenJsonDict(dict[str, Any]):
    """A JSON-compatible dict whose content cannot change after hashing."""

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("captured JSON is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __deepcopy__(self, _memo: dict[int, Any]) -> "_FrozenJsonDict":
        return self


class _FrozenJsonList(list[Any]):
    """A JSON-compatible list whose content cannot change after hashing."""

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("captured JSON is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable

    def __deepcopy__(self, _memo: dict[int, Any]) -> "_FrozenJsonList":
        return self


class CaptureContractError(ValueError):
    """Raised when a record could not safely be used for replay."""


class CaptureStream(str, Enum):
    """Exhaustive production-input families required by the momentum FSM."""

    IQFEED_PRINT = "iqfeed_print"
    PROVIDER_TRADE_PRINT = "provider_trade_print"
    NBBO_QUOTE = "nbbo_quote"
    # A direct, provider-timestamped Alpaca PAPER latest-quote response.  This
    # is deliberately separate from IQFeed/Massive NBBO: IQFeed Q frames do not
    # carry an exact quote-event timestamp, and producer ownership must never be
    # borrowed merely to make an execution read look complete.
    ALPACA_NBBO_QUOTE = "alpaca_nbbo_quote"
    L2_DEPTH_DELTA = "l2_depth_delta"
    L2_DEPTH_CHECKPOINT = "l2_depth_checkpoint"
    PROVIDER_OHLCV = "provider_ohlcv"
    ORTEX_SNAPSHOT = "ortex_snapshot"
    SCANNER_SNAPSHOT = "scanner_snapshot"
    CATALYST_NEWS = "catalyst_news"
    ADMISSION_ELIGIBILITY = "admission_eligibility"
    HALT_LULD_STATE = "halt_luld_state"
    SSR_STATE = "ssr_state"
    MARKET_SESSION_STATE = "market_session_state"
    ACCOUNT_RISK_SNAPSHOT = "account_risk_snapshot"
    BROKER_ORDER_LIFECYCLE = "broker_order_lifecycle"
    CONFIG_SNAPSHOT = "config_snapshot"
    FEATURE_FLAG_SNAPSHOT = "feature_flag_snapshot"
    CODE_BUILD = "code_build"
    FSM_DECISION = "fsm_decision"
    READ_RECEIPT = "read_receipt"
    PROVIDER_WATERMARK = "provider_watermark"
    COVERAGE_GAP = "coverage_gap"
    CAPTURE_HEALTH = "capture_health"


class CoverageMode(str, Enum):
    """How completeness is proven for one stream."""

    CONTINUOUS = "continuous"
    QUERY_RECEIPT = "query_receipt"
    CHANGE_LOG = "change_log"
    IDENTITY = "identity"
    DERIVED = "derived"
    CONTROL = "control"


class CaptureTier(str, Enum):
    """Resource-aware storage tier for one stream."""

    BROAD_RING = "broad_ring"
    HOT_FULL = "hot_full"
    QUERY_OR_CHANGE = "query_or_change"
    ALWAYS = "always"
    CONTROL = "control"


@dataclass(frozen=True)
class StreamPolicy:
    stream: CaptureStream
    coverage_mode: CoverageMode
    tier: CaptureTier
    exact_provider_event_clock_required: bool
    market_reference_clock_required: bool = False
    query_parameters_required: bool = False
    content_dedup_allowed: bool = False


def _policy(
    stream: CaptureStream,
    mode: CoverageMode,
    tier: CaptureTier,
    *,
    exact: bool = False,
    reference: bool = False,
    query: bool = False,
    dedup: bool = False,
) -> StreamPolicy:
    return StreamPolicy(
        stream=stream,
        coverage_mode=mode,
        tier=tier,
        exact_provider_event_clock_required=exact,
        market_reference_clock_required=reference,
        query_parameters_required=query,
        content_dedup_allowed=dedup,
    )


# Keep this registry explicit.  Adding an FSM input requires adding a stream
# here, instrumenting the read, and extending the ReplayV3 coverage request.
STREAM_POLICIES: dict[CaptureStream, StreamPolicy] = {
    CaptureStream.IQFEED_PRINT: _policy(
        CaptureStream.IQFEED_PRINT,
        CoverageMode.CONTINUOUS,
        CaptureTier.HOT_FULL,
        exact=True,
    ),
    # Massive/other provider websocket trade frames are a distinct causal
    # source.  They must never be relabeled as IQFeed prints or NBBO quotes.
    CaptureStream.PROVIDER_TRADE_PRINT: _policy(
        CaptureStream.PROVIDER_TRADE_PRINT,
        CoverageMode.CONTINUOUS,
        CaptureTier.HOT_FULL,
        exact=True,
    ),
    CaptureStream.NBBO_QUOTE: _policy(
        CaptureStream.NBBO_QUOTE,
        CoverageMode.CONTINUOUS,
        CaptureTier.HOT_FULL,
        exact=True,
    ),
    CaptureStream.ALPACA_NBBO_QUOTE: _policy(
        CaptureStream.ALPACA_NBBO_QUOTE,
        CoverageMode.QUERY_RECEIPT,
        CaptureTier.ALWAYS,
        exact=True,
        query=True,
    ),
    CaptureStream.L2_DEPTH_DELTA: _policy(
        CaptureStream.L2_DEPTH_DELTA,
        CoverageMode.CONTINUOUS,
        CaptureTier.HOT_FULL,
        exact=True,
    ),
    CaptureStream.L2_DEPTH_CHECKPOINT: _policy(
        CaptureStream.L2_DEPTH_CHECKPOINT,
        CoverageMode.CONTINUOUS,
        CaptureTier.HOT_FULL,
        exact=True,
        dedup=True,
    ),
    CaptureStream.PROVIDER_OHLCV: _policy(
        CaptureStream.PROVIDER_OHLCV,
        CoverageMode.QUERY_RECEIPT,
        CaptureTier.QUERY_OR_CHANGE,
        reference=True,
        query=True,
        dedup=True,
    ),
    CaptureStream.ORTEX_SNAPSHOT: _policy(
        CaptureStream.ORTEX_SNAPSHOT,
        CoverageMode.QUERY_RECEIPT,
        CaptureTier.QUERY_OR_CHANGE,
        reference=True,
        query=True,
        dedup=True,
    ),
    CaptureStream.SCANNER_SNAPSHOT: _policy(
        CaptureStream.SCANNER_SNAPSHOT,
        CoverageMode.CHANGE_LOG,
        CaptureTier.BROAD_RING,
        reference=True,
        query=True,
        dedup=True,
    ),
    CaptureStream.CATALYST_NEWS: _policy(
        CaptureStream.CATALYST_NEWS,
        CoverageMode.QUERY_RECEIPT,
        CaptureTier.QUERY_OR_CHANGE,
        reference=True,
        query=True,
        dedup=True,
    ),
    CaptureStream.ADMISSION_ELIGIBILITY: _policy(
        CaptureStream.ADMISSION_ELIGIBILITY,
        CoverageMode.DERIVED,
        CaptureTier.ALWAYS,
        reference=True,
        dedup=True,
    ),
    CaptureStream.HALT_LULD_STATE: _policy(
        CaptureStream.HALT_LULD_STATE,
        CoverageMode.CHANGE_LOG,
        CaptureTier.ALWAYS,
        exact=True,
        dedup=True,
    ),
    CaptureStream.SSR_STATE: _policy(
        CaptureStream.SSR_STATE,
        CoverageMode.CHANGE_LOG,
        CaptureTier.ALWAYS,
        reference=True,
        dedup=True,
    ),
    CaptureStream.MARKET_SESSION_STATE: _policy(
        CaptureStream.MARKET_SESSION_STATE,
        CoverageMode.CHANGE_LOG,
        CaptureTier.ALWAYS,
        reference=True,
        dedup=True,
    ),
    CaptureStream.ACCOUNT_RISK_SNAPSHOT: _policy(
        CaptureStream.ACCOUNT_RISK_SNAPSHOT,
        CoverageMode.QUERY_RECEIPT,
        CaptureTier.ALWAYS,
        reference=True,
        query=True,
        dedup=True,
    ),
    CaptureStream.BROKER_ORDER_LIFECYCLE: _policy(
        CaptureStream.BROKER_ORDER_LIFECYCLE,
        CoverageMode.CHANGE_LOG,
        CaptureTier.ALWAYS,
        exact=True,
    ),
    CaptureStream.CONFIG_SNAPSHOT: _policy(
        CaptureStream.CONFIG_SNAPSHOT,
        CoverageMode.IDENTITY,
        CaptureTier.ALWAYS,
        dedup=True,
    ),
    CaptureStream.FEATURE_FLAG_SNAPSHOT: _policy(
        CaptureStream.FEATURE_FLAG_SNAPSHOT,
        CoverageMode.IDENTITY,
        CaptureTier.ALWAYS,
        dedup=True,
    ),
    CaptureStream.CODE_BUILD: _policy(
        CaptureStream.CODE_BUILD,
        CoverageMode.IDENTITY,
        CaptureTier.ALWAYS,
        dedup=True,
    ),
    CaptureStream.FSM_DECISION: _policy(
        CaptureStream.FSM_DECISION,
        CoverageMode.DERIVED,
        CaptureTier.ALWAYS,
        reference=True,
    ),
    CaptureStream.READ_RECEIPT: _policy(
        CaptureStream.READ_RECEIPT,
        CoverageMode.CONTROL,
        CaptureTier.CONTROL,
    ),
    CaptureStream.PROVIDER_WATERMARK: _policy(
        CaptureStream.PROVIDER_WATERMARK,
        CoverageMode.CONTROL,
        CaptureTier.CONTROL,
    ),
    CaptureStream.COVERAGE_GAP: _policy(
        CaptureStream.COVERAGE_GAP,
        CoverageMode.CONTROL,
        CaptureTier.CONTROL,
    ),
    CaptureStream.CAPTURE_HEALTH: _policy(
        CaptureStream.CAPTURE_HEALTH,
        CoverageMode.CONTROL,
        CaptureTier.CONTROL,
        dedup=True,
    ),
}


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso_utc(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Stable JSON encoding used for every content address and manifest root."""

    try:
        encoded = json.dumps(
            value,
            default=_json_default,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return encoded.encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CaptureContractError(f"value is not canonical JSON: {exc}") from exc


def _freeze_canonical_json(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Canonicalize and deeply freeze a captured mapping.

    Frozen dataclasses do not protect nested dictionaries/lists.  Without this
    copy, a producer could mutate a payload after ``payload_sha256`` or
    ``event_sha256`` had been cached, invalidating the content address.
    """

    canonical = json.loads(canonical_json_bytes(value).decode("utf-8"))

    def freeze(node: Any) -> Any:
        if isinstance(node, dict):
            return _FrozenJsonDict({key: freeze(child) for key, child in node.items()})
        if isinstance(node, list):
            return _FrozenJsonList(freeze(child) for child in node)
        return node

    frozen = freeze(canonical)
    if not isinstance(frozen, Mapping):  # defensive; callers already require Mapping
        raise CaptureContractError("captured JSON must be a mapping")
    return frozen


def freeze_canonical_json(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a canonical, deeply immutable JSON mapping for bound consumers."""

    return _freeze_canonical_json(value)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _require_sha256(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise CaptureContractError(f"{field_name} must be a full lowercase SHA256")
    return normalized


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CaptureContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None, field_name: str) -> datetime | None:
    return None if value is None else _utc(value, field_name)


def _iso_utc(value: datetime) -> str:
    return _utc(value, "datetime").isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, field_name)
    if not isinstance(value, str) or not value.strip():
        raise CaptureContractError(f"{field_name} is missing")
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise CaptureContractError(f"{field_name} is not ISO-8601") from exc
    return _utc(parsed, field_name)


def _parse_optional_utc(value: Any, field_name: str) -> datetime | None:
    return None if value is None else _parse_utc(value, field_name)


def _uuid_text(value: str, field_name: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = uuid.UUID(raw)
    except ValueError as exc:
        raise CaptureContractError(f"{field_name} must be a UUID") from exc
    if str(parsed) != raw:
        raise CaptureContractError(f"{field_name} must be canonical lowercase UUID")
    return raw


@dataclass(frozen=True)
class FSMStreamDependency:
    stream: CaptureStream
    exact_provider_event_at_required: bool
    market_reference_at_required: bool
    max_source_age_seconds: float
    coverage_start_at: datetime

    def __post_init__(self) -> None:
        try:
            stream = (
                self.stream
                if isinstance(self.stream, CaptureStream)
                else CaptureStream(str(self.stream))
            )
        except ValueError as exc:
            raise CaptureContractError("FSM dependency stream is unknown") from exc
        object.__setattr__(self, "stream", stream)
        for name in (
            "exact_provider_event_at_required",
            "market_reference_at_required",
        ):
            if type(getattr(self, name)) is not bool:
                raise CaptureContractError(f"FSM dependency {name} must be boolean")
        max_age = float(self.max_source_age_seconds)
        if not math.isfinite(max_age) or max_age <= 0:
            raise CaptureContractError(
                "FSM dependency max source age must be finite and positive"
            )
        object.__setattr__(self, "max_source_age_seconds", max_age)
        object.__setattr__(
            self,
            "coverage_start_at",
            _utc(self.coverage_start_at, "FSM dependency coverage_start_at"),
        )
        policy = STREAM_POLICIES[stream]
        if (
            policy.exact_provider_event_clock_required
            and not self.exact_provider_event_at_required
        ):
            raise CaptureContractError(
                "FSM dependency cannot downgrade an exact-clock stream"
            )
        if (
            policy.market_reference_clock_required
            and not self.market_reference_at_required
        ):
            raise CaptureContractError(
                "FSM dependency cannot downgrade a market-reference stream"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream": self.stream.value,
            "exact_provider_event_at_required": (
                self.exact_provider_event_at_required
            ),
            "market_reference_at_required": self.market_reference_at_required,
            "max_source_age_seconds": self.max_source_age_seconds,
            "coverage_start_at": _iso_utc(self.coverage_start_at),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FSMStreamDependency":
        expected = {
            "stream",
            "exact_provider_event_at_required",
            "market_reference_at_required",
            "max_source_age_seconds",
            "coverage_start_at",
        }
        if set(raw) != expected:
            raise CaptureContractError("FSM stream dependency fields do not match schema")
        return cls(
            stream=raw.get("stream"),
            exact_provider_event_at_required=raw.get(
                "exact_provider_event_at_required"
            ),
            market_reference_at_required=raw.get("market_reference_at_required"),
            max_source_age_seconds=raw.get("max_source_age_seconds"),
            coverage_start_at=_parse_utc(
                raw.get("coverage_start_at"),
                "fsm_stream_dependency.coverage_start_at",
            ),
        )


@dataclass(frozen=True)
class FSMDependencyProfile:
    required_streams: frozenset[CaptureStream]
    required_read_ids: tuple[str, ...]
    stream_dependencies: tuple[FSMStreamDependency, ...]
    schema_version: str = FSM_DEPENDENCY_PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FSM_DEPENDENCY_PROFILE_SCHEMA_VERSION:
            raise CaptureContractError("FSM dependency profile schema is unsupported")
        try:
            streams = frozenset(
                value
                if isinstance(value, CaptureStream)
                else CaptureStream(str(value))
                for value in self.required_streams
            )
        except ValueError as exc:
            raise CaptureContractError("FSM dependency profile stream is unknown") from exc
        if not streams:
            raise CaptureContractError("FSM dependency profile stream set is empty")
        object.__setattr__(self, "required_streams", streams)
        read_ids = tuple(
            sorted(
                _uuid_text(value, "fsm_dependency_profile.required_read_id")
                for value in self.required_read_ids
            )
        )
        if not read_ids or len(read_ids) != len(set(read_ids)):
            raise CaptureContractError(
                "FSM dependency profile read set is empty or duplicated"
            )
        object.__setattr__(self, "required_read_ids", read_ids)
        dependencies = tuple(
            sorted(self.stream_dependencies, key=lambda row: row.stream.value)
        )
        if any(not isinstance(row, FSMStreamDependency) for row in dependencies):
            raise CaptureContractError("FSM stream dependencies are malformed")
        dependency_streams = tuple(row.stream for row in dependencies)
        if (
            len(dependency_streams) != len(set(dependency_streams))
            or frozenset(dependency_streams) != streams
        ):
            raise CaptureContractError(
                "FSM stream dependencies do not exactly cover required streams"
            )
        object.__setattr__(self, "stream_dependencies", dependencies)

    @cached_property
    def profile_sha256(self) -> str:
        return sha256_json(self.to_dict())

    def dependency_for(self, stream: CaptureStream) -> FSMStreamDependency:
        for dependency in self.stream_dependencies:
            if dependency.stream is stream:
                return dependency
        raise CaptureContractError(f"FSM dependency stream is undeclared: {stream.value}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "required_streams": sorted(stream.value for stream in self.required_streams),
            "required_read_ids": list(self.required_read_ids),
            "stream_dependencies": [row.to_dict() for row in self.stream_dependencies],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FSMDependencyProfile":
        expected = {
            "schema_version",
            "required_streams",
            "required_read_ids",
            "stream_dependencies",
        }
        raw_streams = raw.get("required_streams")
        raw_reads = raw.get("required_read_ids")
        raw_dependencies = raw.get("stream_dependencies")
        if (
            set(raw) != expected
            or not isinstance(raw_streams, list)
            or not isinstance(raw_reads, list)
            or not isinstance(raw_dependencies, list)
        ):
            raise CaptureContractError("FSM dependency profile fields do not match schema")
        return cls(
            schema_version=str(raw.get("schema_version") or ""),
            required_streams=frozenset(CaptureStream(str(value)) for value in raw_streams),
            required_read_ids=tuple(str(value) for value in raw_reads),
            stream_dependencies=tuple(
                FSMStreamDependency.from_dict(_mapping(value, "fsm_stream_dependency"))
                for value in raw_dependencies
            ),
        )


@dataclass(frozen=True)
class CaptureRunIdentity:
    """Pinned identity shared by all events in one capture generation."""

    run_id: str
    generation: int
    code_build_sha256: str
    config_sha256: str
    feature_flags_sha256: str
    account_identity_sha256: str
    broker: str
    broker_environment: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _uuid_text(self.run_id, "run_id"))
        if isinstance(self.generation, bool) or int(self.generation) <= 0:
            raise CaptureContractError("generation must be a positive integer")
        object.__setattr__(self, "generation", int(self.generation))
        for name in (
            "code_build_sha256",
            "config_sha256",
            "feature_flags_sha256",
            "account_identity_sha256",
        ):
            object.__setattr__(self, name, _require_sha256(getattr(self, name), name))
        if not str(self.broker or "").strip():
            raise CaptureContractError("broker is required")
        if not str(self.broker_environment or "").strip():
            raise CaptureContractError("broker_environment is required")

    @property
    def identity_sha256(self) -> str:
        return sha256_json(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CaptureRunIdentity":
        return cls(**dict(raw))


@dataclass(frozen=True)
class CaptureClocks:
    """Dual-clock evidence for one input.

    ``provider_event_at`` is the provider's exact event clock when the feed
    supplies one.  ``market_reference_at`` is a bar end, snapshot reference, or
    other market-time anchor.  It is never allowed to masquerade as an exact
    quote event clock.  Replay release ordering always uses ``available_at``.
    """

    received_at: datetime
    available_at: datetime
    provider_event_at: datetime | None = None
    market_reference_at: datetime | None = None

    def __post_init__(self) -> None:
        received = _utc(self.received_at, "received_at")
        available = _utc(self.available_at, "available_at")
        provider = _optional_utc(self.provider_event_at, "provider_event_at")
        reference = _optional_utc(self.market_reference_at, "market_reference_at")
        if available < received:
            raise CaptureContractError("available_at cannot precede received_at")
        object.__setattr__(self, "received_at", received)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "provider_event_at", provider)
        object.__setattr__(self, "market_reference_at", reference)

    @property
    def observed_lateness_seconds(self) -> float | None:
        if self.provider_event_at is None:
            return None
        return max(0.0, (self.received_at - self.provider_event_at).total_seconds())

    def to_dict(self) -> dict[str, str | None]:
        return {
            "provider_event_at": (
                _iso_utc(self.provider_event_at) if self.provider_event_at else None
            ),
            "market_reference_at": (
                _iso_utc(self.market_reference_at) if self.market_reference_at else None
            ),
            "received_at": _iso_utc(self.received_at),
            "available_at": _iso_utc(self.available_at),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CaptureClocks":
        return cls(
            provider_event_at=_parse_optional_utc(
                raw.get("provider_event_at"), "provider_event_at"
            ),
            market_reference_at=_parse_optional_utc(
                raw.get("market_reference_at"), "market_reference_at"
            ),
            received_at=_parse_utc(raw.get("received_at"), "received_at"),
            available_at=_parse_utc(raw.get("available_at"), "available_at"),
        )


def validate_iqfeed_exact_print_source_provenance(
    raw: Any,
    *,
    symbol: str,
    clocks: CaptureClocks,
) -> Mapping[str, Any]:
    """Validate a selected-field IQFeed print with an exact date/time clock."""

    if not isinstance(raw, Mapping):
        raise CaptureContractError("IQFeed exact-print provenance is malformed")
    expected = {
        "schema_version",
        "symbol",
        "bridge_run_id",
        "connection_generation",
        "bridge_version",
        "bridge_source_sha256",
        "bridge_configuration",
        "bridge_configuration_sha256",
        "capture_resource_binding_sha256",
        "handoff_configuration",
        "handoff_configuration_sha256",
        "message_type",
        "timestamp_basis",
        "provider_event_at",
        "received_at",
        "provider_trade_date",
        "provider_trade_time",
        "provider_tick_id",
        "trade_market_center",
        "trade_conditions",
        "message_contents",
        "selected_update_fields",
        "selected_update_fields_sha256",
        "selected_update_fields_ack_sha256",
        "source_frame_sequence",
        "source_frame_sha256",
    }
    if set(raw) != expected:
        raise CaptureContractError(
            "IQFeed exact-print provenance fields do not match schema"
        )
    if (
        raw.get("schema_version")
        != IQFEED_EXACT_PRINT_SOURCE_PROVENANCE_SCHEMA_VERSION
    ):
        raise CaptureContractError("IQFeed exact-print provenance schema is unsupported")
    normalized = str(symbol or "").strip().upper()
    if not normalized or str(raw.get("symbol") or "").strip().upper() != normalized:
        raise CaptureContractError("IQFeed exact-print provenance symbol mismatch")
    try:
        uuid.UUID(str(raw.get("bridge_run_id") or ""))
    except ValueError as exc:
        raise CaptureContractError("IQFeed exact-print bridge run id is malformed") from exc
    generation = raw.get("connection_generation")
    sequence = raw.get("source_frame_sequence")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence <= 0
    ):
        raise CaptureContractError(
            "IQFeed exact-print source generation/sequence is malformed"
        )
    if not str(raw.get("bridge_version") or "").strip():
        raise CaptureContractError("IQFeed exact-print bridge version is missing")
    for key, label in (
        ("bridge_source_sha256", "bridge source"),
        ("bridge_configuration_sha256", "bridge configuration"),
        ("capture_resource_binding_sha256", "capture resource binding"),
        ("handoff_configuration_sha256", "handoff configuration"),
        ("selected_update_fields_sha256", "selected fields"),
        ("selected_update_fields_ack_sha256", "selected fields acknowledgement"),
        ("source_frame_sha256", "source frame"),
    ):
        _require_sha256(str(raw.get(key) or ""), f"IQFeed exact-print {label} SHA-256")
    bridge_configuration = raw.get("bridge_configuration")
    if not isinstance(bridge_configuration, Mapping) or not bridge_configuration:
        raise CaptureContractError(
            "IQFeed exact-print bridge configuration is malformed"
        )
    if sha256_json(bridge_configuration) != raw.get("bridge_configuration_sha256"):
        raise CaptureContractError(
            "IQFeed exact-print bridge configuration hash mismatch"
        )
    handoff_configuration = raw.get("handoff_configuration")
    if not isinstance(handoff_configuration, Mapping) or not handoff_configuration:
        raise CaptureContractError(
            "IQFeed exact-print handoff configuration is malformed"
        )
    if sha256_json(handoff_configuration) != raw.get("handoff_configuration_sha256"):
        raise CaptureContractError(
            "IQFeed exact-print handoff configuration hash mismatch"
        )
    fields = raw.get("selected_update_fields")
    if (
        not isinstance(fields, (list, tuple))
        or not fields
        or any(not isinstance(value, str) or not value.strip() for value in fields)
        or len(set(fields)) != len(fields)
    ):
        raise CaptureContractError("IQFeed exact-print selected fields are malformed")
    if sha256_json(list(fields)) != raw.get("selected_update_fields_sha256"):
        raise CaptureContractError("IQFeed exact-print selected fields hash mismatch")
    required_fields = {
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
    }
    if not required_fields.issubset(set(fields)):
        raise CaptureContractError(
            "IQFeed exact-print selected fields omit required print authority"
        )
    if str(raw.get("message_type") or "").strip().upper() != "Q":
        raise CaptureContractError("IQFeed exact-print requires a Q update frame")
    if raw.get("timestamp_basis") != (
        "iqfeed_selected_trade_date_timems_exact"
    ):
        raise CaptureContractError("IQFeed exact-print timestamp basis is invalid")
    provider = _parse_utc(
        raw.get("provider_event_at"), "IQFeed exact-print provider event"
    )
    received = _parse_utc(raw.get("received_at"), "IQFeed exact-print received_at")
    if (
        clocks.provider_event_at is None
        or provider != clocks.provider_event_at
        or raw.get("provider_event_at") != _iso_utc(provider)
        or received != clocks.received_at
        or raw.get("received_at") != _iso_utc(received)
        or clocks.market_reference_at is not None
    ):
        raise CaptureContractError("IQFeed exact-print clocks do not match provenance")
    if not str(raw.get("provider_trade_date") or "").strip() or not str(
        raw.get("provider_trade_time") or ""
    ).strip():
        raise CaptureContractError("IQFeed exact-print raw date/time is missing")
    tick_id = str(raw.get("provider_tick_id") or "").strip()
    if not tick_id or not tick_id.isdigit():
        raise CaptureContractError("IQFeed exact-print tick id is malformed")
    if not str(raw.get("trade_market_center") or "").strip():
        raise CaptureContractError("IQFeed exact-print market center is missing")
    conditions = raw.get("trade_conditions")
    if not isinstance(conditions, (list, tuple)) or any(
        not isinstance(value, str) or not value.strip() for value in conditions
    ):
        raise CaptureContractError("IQFeed exact-print conditions are malformed")
    if not isinstance(raw.get("message_contents"), str):
        raise CaptureContractError("IQFeed exact-print message contents are malformed")
    return raw


def validate_iqfeed_l1_source_provenance(
    raw: Any,
    *,
    symbol: str,
    clocks: CaptureClocks,
) -> Mapping[str, Any]:
    """Validate the exact host-bridge provenance carried by an IQFeed L1 row.

    IQFeed's default L1 ``Q`` frame exposes a Most-Recent-Trade-Time reference,
    not an exact quote-event timestamp.  The provenance therefore binds that
    reference to ``market_reference_at`` and requires ``provider_event_at`` to
    remain null.  This prevents a later adapter from laundering the proxy into
    certifying event time while still preserving the observed frame/config.
    """

    if (
        isinstance(raw, Mapping)
        and raw.get("schema_version")
        == IQFEED_EXACT_PRINT_SOURCE_PROVENANCE_SCHEMA_VERSION
    ):
        return validate_iqfeed_exact_print_source_provenance(
            raw,
            symbol=symbol,
            clocks=clocks,
        )
    if not isinstance(raw, Mapping):
        raise CaptureContractError("IQFeed L1 source provenance is malformed")
    expected = {
        "schema_version",
        "symbol",
        "bridge_run_id",
        "connection_generation",
        "bridge_version",
        "bridge_source_sha256",
        "bridge_configuration",
        "bridge_configuration_sha256",
        "capture_resource_binding_sha256",
        "handoff_configuration",
        "handoff_configuration_sha256",
        "message_type",
        "timestamp_basis",
        "provider_event_at",
        "provider_trade_reference_at",
        "source_frame_sequence",
        "source_frame_sha256",
    }
    if set(raw) != expected:
        raise CaptureContractError(
            "IQFeed L1 source provenance fields do not match schema"
        )
    if raw.get("schema_version") != IQFEED_L1_SOURCE_PROVENANCE_SCHEMA_VERSION:
        raise CaptureContractError("IQFeed L1 source provenance schema is unsupported")
    normalized_symbol = str(symbol or "").strip().upper()
    if str(raw.get("symbol") or "").strip().upper() != normalized_symbol:
        raise CaptureContractError("IQFeed L1 source provenance symbol mismatch")
    run_id = str(raw.get("bridge_run_id") or "").strip().lower()
    try:
        parsed_run_id = str(uuid.UUID(run_id))
    except (ValueError, AttributeError) as exc:
        raise CaptureContractError("IQFeed L1 bridge run id is malformed") from exc
    if parsed_run_id != run_id:
        raise CaptureContractError("IQFeed L1 bridge run id is noncanonical")
    generation = raw.get("connection_generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise CaptureContractError("IQFeed L1 connection generation is malformed")
    bridge_version = str(raw.get("bridge_version") or "").strip()
    if not bridge_version or len(bridge_version) > 192:
        raise CaptureContractError("IQFeed L1 bridge version is malformed")
    _require_sha256(
        str(raw.get("bridge_source_sha256") or ""),
        "IQFeed L1 bridge source SHA-256",
    )
    configuration = raw.get("bridge_configuration")
    if not isinstance(configuration, Mapping) or not configuration:
        raise CaptureContractError("IQFeed L1 bridge configuration is malformed")
    expected_configuration_sha256 = _require_sha256(
        str(raw.get("bridge_configuration_sha256") or ""),
        "IQFeed L1 bridge configuration SHA-256",
    )
    if sha256_json(configuration) != expected_configuration_sha256:
        raise CaptureContractError("IQFeed L1 bridge configuration hash mismatch")
    _require_sha256(
        str(raw.get("capture_resource_binding_sha256") or ""),
        "IQFeed L1 capture resource binding SHA-256",
    )
    handoff_configuration = raw.get("handoff_configuration")
    if not isinstance(handoff_configuration, Mapping) or not handoff_configuration:
        raise CaptureContractError("IQFeed L1 handoff configuration is malformed")
    expected_handoff_sha256 = _require_sha256(
        str(raw.get("handoff_configuration_sha256") or ""),
        "IQFeed L1 handoff configuration SHA-256",
    )
    if sha256_json(handoff_configuration) != expected_handoff_sha256:
        raise CaptureContractError("IQFeed L1 handoff configuration hash mismatch")
    if str(raw.get("message_type") or "").strip().upper() != "Q":
        raise CaptureContractError("IQFeed L1 capture requires a Q update frame")
    frame_sequence = raw.get("source_frame_sequence")
    if (
        isinstance(frame_sequence, bool)
        or not isinstance(frame_sequence, int)
        or frame_sequence <= 0
    ):
        raise CaptureContractError("IQFeed L1 source frame sequence is malformed")
    if not str(raw.get("timestamp_basis") or "").strip():
        raise CaptureContractError("IQFeed L1 timestamp basis is missing")
    if raw.get("provider_event_at") is not None or clocks.provider_event_at is not None:
        raise CaptureContractError(
            "IQFeed L1 trade-time proxy cannot be an exact provider event clock"
        )
    reference = _parse_optional_utc(
        raw.get("provider_trade_reference_at"),
        "IQFeed L1 provider trade reference",
    )
    if reference is None or reference != clocks.market_reference_at:
        raise CaptureContractError(
            "IQFeed L1 provider trade reference does not match capture clocks"
        )
    _require_sha256(
        str(raw.get("source_frame_sha256") or ""),
        "IQFeed L1 source frame SHA-256",
    )
    return raw


def validate_iqfeed_l2_source_provenance(
    raw: Any,
    *,
    symbol: str,
    clocks: CaptureClocks,
    stream: CaptureStream,
) -> Mapping[str, Any]:
    """Validate one exact IQFeed L2 host-frame/checkpoint source binding.

    Type-6 equity depth frames carry an exchange/participant quote date and
    time; deltas therefore preserve that exact timestamp as
    ``provider_event_at``.  A locally assembled checkpoint has no single
    provider event and must leave that clock null.  Its per-level event clocks
    remain inside the typed payload and its latest level clock is only a market
    reference.  Neither form self-attests provider continuity or initial-book
    completion.
    """

    if stream not in {
        CaptureStream.L2_DEPTH_DELTA,
        CaptureStream.L2_DEPTH_CHECKPOINT,
    }:
        raise CaptureContractError("IQFeed L2 provenance uses the wrong stream")
    if not isinstance(raw, Mapping):
        raise CaptureContractError("IQFeed L2 source provenance is malformed")
    expected = {
        "schema_version",
        "symbol",
        "bridge_run_id",
        "connection_generation",
        "bridge_version",
        "bridge_source_sha256",
        "bridge_configuration_sha256",
        "capture_resource_binding_sha256",
        "handoff_configuration_sha256",
        "source_frame_sequence",
        "source_frame_sha256",
        "message_type",
        "timestamp_basis",
        "provider_event_at",
        "received_at",
    }
    if set(raw) != expected:
        raise CaptureContractError(
            "IQFeed L2 source provenance fields do not match schema"
        )
    if raw.get("schema_version") != IQFEED_L2_SOURCE_PROVENANCE_SCHEMA_VERSION:
        raise CaptureContractError("IQFeed L2 source provenance schema is unsupported")
    normalized = str(symbol or "").strip().upper()
    if not normalized or str(raw.get("symbol") or "").strip().upper() != normalized:
        raise CaptureContractError("IQFeed L2 source provenance symbol mismatch")
    try:
        uuid.UUID(str(raw.get("bridge_run_id") or ""))
    except ValueError as exc:
        raise CaptureContractError("IQFeed L2 bridge run id is malformed") from exc
    generation = raw.get("connection_generation")
    sequence = raw.get("source_frame_sequence")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence <= 0
    ):
        raise CaptureContractError("IQFeed L2 source generation/sequence is malformed")
    if not str(raw.get("bridge_version") or "").strip():
        raise CaptureContractError("IQFeed L2 bridge version is missing")
    for key, label in (
        ("bridge_source_sha256", "bridge source"),
        ("bridge_configuration_sha256", "bridge configuration"),
        ("capture_resource_binding_sha256", "capture resource binding"),
        ("handoff_configuration_sha256", "capture handoff configuration"),
        ("source_frame_sha256", "source frame"),
    ):
        _require_sha256(str(raw.get(key) or ""), f"IQFeed L2 {label} SHA-256")
    received = _parse_utc(raw.get("received_at"), "IQFeed L2 received_at")
    if received != clocks.received_at or raw.get("received_at") != _iso_utc(received):
        raise CaptureContractError("IQFeed L2 receive clock mismatch")
    provider_raw = raw.get("provider_event_at")
    provider = (
        None
        if provider_raw is None
        else _parse_utc(provider_raw, "IQFeed L2 provider_event_at")
    )
    if provider != clocks.provider_event_at or (
        provider is not None and provider_raw != _iso_utc(provider)
    ):
        raise CaptureContractError("IQFeed L2 provider event clock mismatch")
    message_type = str(raw.get("message_type") or "")
    basis = str(raw.get("timestamp_basis") or "")
    if stream is CaptureStream.L2_DEPTH_DELTA:
        if (
            message_type != "6"
            or basis != "iqfeed_l2_frame_date_time_et"
            or provider is None
        ):
            raise CaptureContractError(
                "IQFeed L2 delta lacks its exact type-6 provider event clock"
            )
    elif (
        message_type != "LOCAL_DEPTH_CHECKPOINT"
        or basis != "iqfeed_l2_local_checkpoint_market_reference"
        or provider is not None
    ):
        raise CaptureContractError(
            "IQFeed L2 checkpoint must remain a local market-reference snapshot"
        )
    return raw


@dataclass(frozen=True)
class CaptureEvent:
    """One immutable input or control event."""

    identity: CaptureRunIdentity
    sequence: int
    stream: CaptureStream
    clocks: CaptureClocks
    payload: Mapping[str, Any]
    provider: str
    symbol: str | None = None
    query: Mapping[str, Any] | None = None
    schema_version: str = CAPTURE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURE_SCHEMA_VERSION:
            raise CaptureContractError("unsupported capture schema version")
        if isinstance(self.sequence, bool) or int(self.sequence) <= 0:
            raise CaptureContractError("sequence must be a positive integer")
        object.__setattr__(self, "sequence", int(self.sequence))
        try:
            stream = (
                self.stream
                if isinstance(self.stream, CaptureStream)
                else CaptureStream(str(self.stream))
            )
        except ValueError as exc:
            raise CaptureContractError(f"unknown capture stream: {self.stream}") from exc
        object.__setattr__(self, "stream", stream)
        policy = STREAM_POLICIES[stream]
        if not isinstance(self.identity, CaptureRunIdentity):
            raise CaptureContractError("identity must be a CaptureRunIdentity")
        if not isinstance(self.clocks, CaptureClocks):
            raise CaptureContractError("clocks must be CaptureClocks")
        if not isinstance(self.payload, Mapping):
            raise CaptureContractError("payload must be a mapping")
        object.__setattr__(self, "payload", _freeze_canonical_json(self.payload))
        if self.query is not None:
            if not isinstance(self.query, Mapping):
                raise CaptureContractError("query must be a mapping")
            object.__setattr__(self, "query", _freeze_canonical_json(self.query))
        if policy.query_parameters_required and not self.query:
            raise CaptureContractError(f"{stream.value} requires exact query parameters")
        # Exact-clock requirements are certification/authorization gates, not
        # ingestion filters.  Providers such as IQFeed can legitimately omit a
        # quote event clock; preserve that ``None`` byte-for-byte so coverage is
        # graded unavailable instead of dropping the observation or laundering
        # a trade-reference proxy into quote event time.
        if policy.market_reference_clock_required and (
            self.clocks.market_reference_at is None
            and self.clocks.provider_event_at is None
        ):
            raise CaptureContractError(
                f"{stream.value} requires a market reference or provider event clock"
            )
        if policy.coverage_mode is not CoverageMode.CONTROL and not str(
            self.provider or ""
        ).strip():
            raise CaptureContractError("provider is required for non-control events")
        object.__setattr__(self, "provider", str(self.provider or "").strip())
        symbol = str(self.symbol or "").strip().upper() or None
        object.__setattr__(self, "symbol", symbol)

    @cached_property
    def payload_sha256(self) -> str:
        return sha256_json(self.payload)

    @cached_property
    def query_sha256(self) -> str | None:
        return None if self.query is None else sha256_json(self.query)

    @cached_property
    def event_sha256(self) -> str:
        # The compact record includes the full payload/query content hashes, so
        # hashing the payload bytes a second time here adds CPU but no integrity.
        return sha256_json(self.to_record(include_payload=False))

    @cached_property
    def canonical_size_bytes(self) -> int:
        return len(canonical_json_bytes(self.to_record(include_payload=True)))

    def to_record(self, *, include_payload: bool) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema_version": self.schema_version,
            "identity": self.identity.to_dict(),
            "sequence": self.sequence,
            "stream": self.stream.value,
            "symbol": self.symbol,
            "provider": self.provider,
            "clocks": self.clocks.to_dict(),
            "query": dict(self.query) if self.query is not None else None,
            "query_sha256": self.query_sha256,
            "payload_sha256": self.payload_sha256,
        }
        if include_payload:
            record["payload"] = dict(self.payload)
        return record

    @classmethod
    def from_record(
        cls,
        raw: Mapping[str, Any],
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> "CaptureEvent":
        stored_payload = raw.get("payload") if payload is None else payload
        if not isinstance(stored_payload, Mapping):
            raise CaptureContractError("capture record payload is missing")
        event = cls(
            schema_version=str(raw.get("schema_version") or ""),
            identity=CaptureRunIdentity.from_dict(
                _mapping(raw.get("identity"), "identity")
            ),
            sequence=int(raw.get("sequence") or 0),
            stream=CaptureStream(str(raw.get("stream") or "")),
            symbol=raw.get("symbol"),
            provider=str(raw.get("provider") or ""),
            clocks=CaptureClocks.from_dict(_mapping(raw.get("clocks"), "clocks")),
            query=(
                _mapping(raw.get("query"), "query")
                if raw.get("query") is not None
                else None
            ),
            payload=stored_payload,
        )
        expected_payload = _require_sha256(
            str(raw.get("payload_sha256") or ""), "payload_sha256"
        )
        if event.payload_sha256 != expected_payload:
            raise CaptureContractError("payload content address mismatch")
        expected_query = raw.get("query_sha256")
        if event.query is None and expected_query is not None:
            raise CaptureContractError("query content address present without a query")
        if event.query is not None:
            expected_query = _require_sha256(
                str(expected_query or ""), "query_sha256"
            )
        if event.query_sha256 != expected_query:
            raise CaptureContractError("query content address mismatch")
        return event


@dataclass(frozen=True)
class CaptureSourcePayloadView:
    """Validated provider payload beneath runtime release provenance.

    The durable event hash always covers the complete payload, including the
    runtime-owned release/promotion records.  Economic and provider-schema
    parsers must consume only ``payload`` from this view so those records do not
    masquerade as provider fields.  The records are removed only after their
    clocks, identifiers, and content addresses have been checked against the
    durable event envelope.
    """

    payload: Mapping[str, Any]
    original_available_at: datetime
    release_kind: str | None = None
    promotion_id: str | None = None
    promotion_order: int | None = None
    provisional_event_sha256: str | None = None
    source_identity_sha256: str | None = None
    resource_binding_sha256: str | None = None
    inventory_sha256: str | None = None
    admission_handoff_sha256: str | None = None


def resolve_capture_source_payload(event: CaptureEvent) -> CaptureSourcePayloadView:
    """Verify runtime provenance and expose the immutable provider payload.

    This function deliberately rejects unknown/partial metadata.  It is the
    sealed-load counterpart of ``CaptureProducerLifecycleRuntime.submit_input``:
    no caller may make a malformed release look like an ordinary source event
    by simply deleting underscore-prefixed keys.
    """

    if not isinstance(event, CaptureEvent):
        raise CaptureContractError("capture source event is malformed")
    raw_payload = dict(event.payload)
    if "_capture_producer_registration" in raw_payload:
        raise CaptureContractError(
            "legacy provider registration metadata is not a source payload"
        )
    release_raw = raw_payload.pop("_capture_release", None)
    promotion_raw = raw_payload.pop("_capture_promotion", None)
    if release_raw is None and promotion_raw is None:
        return CaptureSourcePayloadView(
            payload=event.payload,
            original_available_at=event.clocks.available_at,
        )
    if not isinstance(release_raw, Mapping):
        raise CaptureContractError("capture release metadata is malformed")

    release_kind = str(release_raw.get("release_kind") or "").strip().lower()
    base_release_fields = {
        "original_available_at",
        "released_available_at",
        "release_kind",
    }
    promotion_release_fields = {
        "promotion_id",
        "promoted_at",
        "source_identity_sha256",
        "resource_binding_sha256",
        "inventory_sha256",
    }
    if release_kind == "observed_ingress":
        if set(release_raw) != base_release_fields or promotion_raw is not None:
            raise CaptureContractError(
                "observed-ingress release metadata fields do not match schema"
            )
    elif release_kind == "hot_symbol_promotion":
        allowed_release_fields = (
            base_release_fields
            | promotion_release_fields
            | {"admission_handoff_sha256"}
        )
        required_release_fields = base_release_fields | promotion_release_fields
        if (
            not required_release_fields.issubset(release_raw)
            or not set(release_raw).issubset(allowed_release_fields)
            or not isinstance(promotion_raw, Mapping)
        ):
            raise CaptureContractError(
                "hot-promotion release metadata fields do not match schema"
            )
    else:
        raise CaptureContractError("capture release kind is unsupported")

    original_available_at = _parse_utc(
        release_raw.get("original_available_at"),
        "capture release original_available_at",
    )
    released_available_at = _parse_utc(
        release_raw.get("released_available_at"),
        "capture release released_available_at",
    )
    if (
        released_available_at != event.clocks.available_at
        or original_available_at < event.clocks.received_at
        or original_available_at > released_available_at
        or (
            release_kind == "observed_ingress"
            and original_available_at == released_available_at
        )
    ):
        raise CaptureContractError("capture release clocks do not match event")
    if release_kind == "observed_ingress":
        return CaptureSourcePayloadView(
            payload=_freeze_canonical_json(raw_payload),
            original_available_at=original_available_at,
            release_kind=release_kind,
        )

    assert isinstance(promotion_raw, Mapping)
    expected_promotion_fields = {
        "promotion_id",
        "promoted_at",
        "promotion_order",
        "original_provisional_available_at",
        "provisional_event_sha256",
        "source_identity_sha256",
        "inventory_sha256",
    }
    if set(promotion_raw) != expected_promotion_fields:
        raise CaptureContractError(
            "capture promotion metadata fields do not match schema"
        )
    promotion_id = _uuid_text(
        str(promotion_raw.get("promotion_id") or ""), "capture promotion id"
    )
    if str(release_raw.get("promotion_id") or "") != promotion_id:
        raise CaptureContractError("capture promotion/release id mismatch")
    promoted_at = _parse_utc(
        promotion_raw.get("promoted_at"), "capture promotion promoted_at"
    )
    release_promoted_at = _parse_utc(
        release_raw.get("promoted_at"), "capture release promoted_at"
    )
    provisional_available_at = _parse_utc(
        promotion_raw.get("original_provisional_available_at"),
        "capture promotion original_provisional_available_at",
    )
    if (
        promoted_at != event.clocks.available_at
        or release_promoted_at != promoted_at
        or provisional_available_at != original_available_at
    ):
        raise CaptureContractError("capture promotion clocks do not match release")
    promotion_order = promotion_raw.get("promotion_order")
    if (
        isinstance(promotion_order, bool)
        or not isinstance(promotion_order, int)
        or promotion_order <= 0
    ):
        raise CaptureContractError("capture promotion order is malformed")
    provisional_event_sha256 = _require_sha256(
        str(promotion_raw.get("provisional_event_sha256") or ""),
        "capture promotion provisional event",
    )
    source_identity_sha256 = _require_sha256(
        str(promotion_raw.get("source_identity_sha256") or ""),
        "capture promotion source identity",
    )
    resource_binding_sha256 = _require_sha256(
        str(release_raw.get("resource_binding_sha256") or ""),
        "capture promotion resource binding",
    )
    inventory_sha256 = _require_sha256(
        str(promotion_raw.get("inventory_sha256") or ""),
        "capture promotion inventory",
    )
    if (
        str(release_raw.get("source_identity_sha256") or "")
        != source_identity_sha256
        or str(release_raw.get("inventory_sha256") or "") != inventory_sha256
    ):
        raise CaptureContractError("capture promotion/release provenance mismatch")
    admission_handoff_sha256 = release_raw.get("admission_handoff_sha256")
    if admission_handoff_sha256 is not None:
        admission_handoff_sha256 = _require_sha256(
            str(admission_handoff_sha256),
            "capture promotion admission handoff",
        )
    return CaptureSourcePayloadView(
        payload=_freeze_canonical_json(raw_payload),
        original_available_at=original_available_at,
        release_kind=release_kind,
        promotion_id=promotion_id,
        promotion_order=promotion_order,
        provisional_event_sha256=provisional_event_sha256,
        source_identity_sha256=source_identity_sha256,
        resource_binding_sha256=resource_binding_sha256,
        inventory_sha256=inventory_sha256,
        admission_handoff_sha256=admission_handoff_sha256,
    )


@dataclass(frozen=True)
class CaptureIqfeedPrint:
    """Strict normalized payload for an exact IQFeed execution event.

    This validates capture mechanics only.  A producer still needs provider-
    documented per-print semantics, continuity, and watermark proof before the
    event can become order authority; Q-frame trade-time proxies never qualify.
    """

    event: CaptureEvent
    price: float
    size: float
    bid: float | None
    ask: float | None
    conditions: tuple[str, ...]

    @classmethod
    def from_event(cls, event: CaptureEvent) -> "CaptureIqfeedPrint":
        if not isinstance(event, CaptureEvent):
            raise CaptureContractError("IQFeed print event is malformed")
        if event.stream is not CaptureStream.IQFEED_PRINT:
            raise CaptureContractError("IQFeed print uses the wrong capture stream")
        if event.provider != "iqfeed":
            raise CaptureContractError("IQFeed print provider identity is invalid")
        if event.clocks.provider_event_at is None:
            raise CaptureContractError(
                "IQFeed print lacks an exact provider event clock"
            )
        payload = resolve_capture_source_payload(event).payload
        allowed = {
            "schema_version",
            "symbol",
            "price",
            "size",
            "bid",
            "ask",
            "conditions",
            IQFEED_L1_SOURCE_PROVENANCE_FIELD,
        }
        required = {"schema_version", "symbol", "price", "size"}
        if not required.issubset(payload) or not set(payload).issubset(allowed):
            raise CaptureContractError("IQFeed print payload fields do not match schema")
        if payload.get("schema_version") != IQFEED_PRINT_PAYLOAD_SCHEMA_VERSION:
            raise CaptureContractError("IQFeed print payload schema is unsupported")
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol or symbol != event.symbol:
            raise CaptureContractError("IQFeed print payload/event symbol mismatch")
        source_provenance = payload.get(IQFEED_L1_SOURCE_PROVENANCE_FIELD)
        if source_provenance is not None:
            validate_iqfeed_l1_source_provenance(
                source_provenance,
                symbol=symbol,
                clocks=event.clocks,
            )

        def _positive_number(value: Any, name: str) -> float:
            if isinstance(value, bool):
                raise CaptureContractError(f"IQFeed print {name} is invalid")
            try:
                resolved = float(value)
            except (TypeError, ValueError) as exc:
                raise CaptureContractError(
                    f"IQFeed print {name} is invalid"
                ) from exc
            if not math.isfinite(resolved) or resolved <= 0:
                raise CaptureContractError(f"IQFeed print {name} is invalid")
            return resolved

        price = _positive_number(payload.get("price"), "price")
        size = _positive_number(payload.get("size"), "size")
        bid_raw = payload.get("bid")
        ask_raw = payload.get("ask")
        if (bid_raw is None) != (ask_raw is None):
            raise CaptureContractError(
                "IQFeed print must carry both bid and ask or neither"
            )
        bid = None if bid_raw is None else _positive_number(bid_raw, "bid")
        ask = None if ask_raw is None else _positive_number(ask_raw, "ask")
        if bid is not None and ask is not None and ask < bid:
            raise CaptureContractError("IQFeed print ask cannot be below bid")
        raw_conditions = payload.get("conditions", ())
        if not isinstance(raw_conditions, (list, tuple)) or any(
            not isinstance(value, str) or not value.strip()
            for value in raw_conditions
        ):
            raise CaptureContractError("IQFeed print conditions are malformed")
        return cls(
            event=event,
            price=price,
            size=size,
            bid=bid,
            ask=ask,
            conditions=tuple(value.strip() for value in raw_conditions),
        )

    def tape_row(self) -> tuple[float, float, float | None, float | None, float]:
        event_at = self.event.clocks.provider_event_at
        if event_at is None:  # defensive; from_event requires it
            raise CaptureContractError(
                "IQFeed print lacks an exact provider event clock"
            )
        return (self.price, self.size, self.bid, self.ask, event_at.timestamp())


def _iqfeed_l2_number(
    value: Any,
    field_name: str,
    *,
    allow_zero: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CaptureContractError(f"IQFeed L2 {field_name} is invalid")
    parsed = float(value)
    if not math.isfinite(parsed) or (parsed < 0 if allow_zero else parsed <= 0):
        raise CaptureContractError(f"IQFeed L2 {field_name} is invalid")
    return parsed


@dataclass(frozen=True)
class CaptureIqfeedL2Delta:
    """Strict type-6 participant quote update with an exact provider clock."""

    event: CaptureEvent
    venue: str
    side: str
    price: float
    size: float
    condition_code: str

    @classmethod
    def from_event(cls, event: CaptureEvent) -> "CaptureIqfeedL2Delta":
        if not isinstance(event, CaptureEvent):
            raise CaptureContractError("IQFeed L2 delta event is malformed")
        if event.stream is not CaptureStream.L2_DEPTH_DELTA:
            raise CaptureContractError("IQFeed L2 delta uses the wrong capture stream")
        if event.provider != "iqfeed":
            raise CaptureContractError("IQFeed L2 delta provider identity is invalid")
        if event.clocks.provider_event_at is None:
            raise CaptureContractError("IQFeed L2 delta lacks an exact provider event clock")
        payload = resolve_capture_source_payload(event).payload
        expected = {
            "schema_version",
            "symbol",
            "venue",
            "side",
            "price",
            "size",
            "condition_code",
            IQFEED_L2_SOURCE_PROVENANCE_FIELD,
        }
        if set(payload) != expected:
            raise CaptureContractError("IQFeed L2 delta payload fields do not match schema")
        if payload.get("schema_version") != IQFEED_L2_DELTA_PAYLOAD_SCHEMA_VERSION:
            raise CaptureContractError("IQFeed L2 delta payload schema is unsupported")
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol or symbol != event.symbol:
            raise CaptureContractError("IQFeed L2 delta payload/event symbol mismatch")
        validate_iqfeed_l2_source_provenance(
            payload.get(IQFEED_L2_SOURCE_PROVENANCE_FIELD),
            symbol=symbol,
            clocks=event.clocks,
            stream=event.stream,
        )
        venue = str(payload.get("venue") or "").strip().upper()
        side = str(payload.get("side") or "").strip().upper()
        condition = str(payload.get("condition_code") or "").strip()
        if not venue or len(venue) > 16 or side not in {"A", "B"} or not condition:
            raise CaptureContractError("IQFeed L2 venue/side/condition is malformed")
        return cls(
            event=event,
            venue=venue,
            side=side,
            price=_iqfeed_l2_number(payload.get("price"), "delta price"),
            size=_iqfeed_l2_number(
                payload.get("size"), "delta size", allow_zero=True
            ),
            condition_code=condition,
        )


@dataclass(frozen=True)
class CaptureIqfeedL2Checkpoint:
    """Local book checkpoint which never fabricates initial-snapshot completion."""

    event: CaptureEvent
    levels: tuple[Mapping[str, Any], ...]
    covered_through_source_frame_sequence: int
    exact_level_event_clock_complete: bool
    initial_snapshot_complete: bool
    completion_basis: str

    @classmethod
    def from_event(cls, event: CaptureEvent) -> "CaptureIqfeedL2Checkpoint":
        if not isinstance(event, CaptureEvent):
            raise CaptureContractError("IQFeed L2 checkpoint event is malformed")
        if event.stream is not CaptureStream.L2_DEPTH_CHECKPOINT:
            raise CaptureContractError(
                "IQFeed L2 checkpoint uses the wrong capture stream"
            )
        if event.provider != "iqfeed":
            raise CaptureContractError(
                "IQFeed L2 checkpoint provider identity is invalid"
            )
        if event.clocks.provider_event_at is not None:
            raise CaptureContractError(
                "local IQFeed L2 checkpoint cannot claim one provider event clock"
            )
        payload = resolve_capture_source_payload(event).payload
        expected = {
            "schema_version",
            "symbol",
            "levels",
            "covered_through_source_frame_sequence",
            "exact_level_event_clock_complete",
            "initial_snapshot_complete",
            "completion_basis",
            IQFEED_L2_SOURCE_PROVENANCE_FIELD,
        }
        if set(payload) != expected:
            raise CaptureContractError(
                "IQFeed L2 checkpoint payload fields do not match schema"
            )
        if (
            payload.get("schema_version")
            != IQFEED_L2_CHECKPOINT_PAYLOAD_SCHEMA_VERSION
        ):
            raise CaptureContractError(
                "IQFeed L2 checkpoint payload schema is unsupported"
            )
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol or symbol != event.symbol:
            raise CaptureContractError(
                "IQFeed L2 checkpoint payload/event symbol mismatch"
            )
        validate_iqfeed_l2_source_provenance(
            payload.get(IQFEED_L2_SOURCE_PROVENANCE_FIELD),
            symbol=symbol,
            clocks=event.clocks,
            stream=event.stream,
        )
        covered = payload.get("covered_through_source_frame_sequence")
        if isinstance(covered, bool) or not isinstance(covered, int) or covered <= 0:
            raise CaptureContractError(
                "IQFeed L2 checkpoint covered sequence is malformed"
            )
        exact_complete = payload.get("exact_level_event_clock_complete")
        initial_complete = payload.get("initial_snapshot_complete")
        completion_basis = str(payload.get("completion_basis") or "").strip()
        if type(exact_complete) is not bool or type(initial_complete) is not bool:
            raise CaptureContractError(
                "IQFeed L2 checkpoint completeness fields must be boolean"
            )
        if initial_complete or completion_basis != (
            "provider_snapshot_completion_boundary_unavailable"
        ):
            raise CaptureContractError(
                "legacy IQFeed L2 checkpoint cannot self-attest snapshot completion"
            )
        raw_levels = payload.get("levels")
        if not isinstance(raw_levels, (list, tuple)) or not raw_levels:
            raise CaptureContractError("IQFeed L2 checkpoint levels are malformed")
        parsed_levels: list[Mapping[str, Any]] = []
        identities: set[tuple[str, str]] = set()
        exact_observed = True
        event_times: list[datetime] = []
        prior_order: tuple[str, str] | None = None
        for raw_level in raw_levels:
            if not isinstance(raw_level, Mapping) or set(raw_level) != {
                "venue",
                "side",
                "price",
                "size",
                "provider_event_at",
                "connection_generation",
                "source_frame_sequence",
                "source_frame_sha256",
                "condition_code",
            }:
                raise CaptureContractError(
                    "IQFeed L2 checkpoint level fields do not match schema"
                )
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
                or (prior_order is not None and identity <= prior_order)
            ):
                raise CaptureContractError(
                    "IQFeed L2 checkpoint level identity/order is malformed"
                )
            identities.add(identity)
            prior_order = identity
            _iqfeed_l2_number(raw_level.get("price"), "checkpoint price")
            _iqfeed_l2_number(
                raw_level.get("size"), "checkpoint size", allow_zero=True
            )
            level_generation = raw_level.get("connection_generation")
            provenance = payload.get(IQFEED_L2_SOURCE_PROVENANCE_FIELD)
            checkpoint_generation = (
                provenance.get("connection_generation")
                if isinstance(provenance, Mapping)
                else None
            )
            if (
                isinstance(level_generation, bool)
                or not isinstance(level_generation, int)
                or level_generation <= 0
                or level_generation != checkpoint_generation
            ):
                raise CaptureContractError(
                    "IQFeed L2 checkpoint level generation is malformed"
                )
            level_sequence = raw_level.get("source_frame_sequence")
            if (
                isinstance(level_sequence, bool)
                or not isinstance(level_sequence, int)
                or level_sequence <= 0
                or level_sequence > covered
            ):
                raise CaptureContractError(
                    "IQFeed L2 checkpoint level sequence is malformed"
                )
            _require_sha256(
                str(raw_level.get("source_frame_sha256") or ""),
                "IQFeed L2 checkpoint level frame SHA-256",
            )
            provider_raw = raw_level.get("provider_event_at")
            if provider_raw is None:
                exact_observed = False
            else:
                provider_at = _parse_utc(
                    provider_raw, "IQFeed L2 checkpoint level provider_event_at"
                )
                if provider_raw != _iso_utc(provider_at):
                    raise CaptureContractError(
                        "IQFeed L2 checkpoint level event clock is not canonical"
                    )
                event_times.append(provider_at)
            parsed_levels.append(raw_level)
        if exact_complete != exact_observed:
            raise CaptureContractError(
                "IQFeed L2 checkpoint exact-clock claim differs from its levels"
            )
        expected_reference = max(event_times) if event_times else None
        if event.clocks.market_reference_at != expected_reference:
            raise CaptureContractError(
                "IQFeed L2 checkpoint market reference differs from its levels"
            )
        return cls(
            event=event,
            levels=tuple(parsed_levels),
            covered_through_source_frame_sequence=covered,
            exact_level_event_clock_complete=exact_complete,
            initial_snapshot_complete=initial_complete,
            completion_basis=completion_basis,
        )


class CaptureMicrostructureOperation(str, Enum):
    """Exact P&L-affecting read surfaces exposed by ``pipeline``.

    The operation is part of the query hash.  A receipt for one operation can
    therefore never be replayed as another even when both happen to reference
    the same immutable source window.
    """

    TRADE_FLOW = "trade_flow"
    REALIZED_VOL = "realized_vol"
    FLOW_SLOPE = "flow_slope"
    BOOK_IMBALANCE = "book_imbalance"
    OFI_MICROPRICE = "ofi_microprice"
    LADDER_DISTRIBUTION = "ladder_distribution"


_MICROSTRUCTURE_OPERATION_STREAM = {
    CaptureMicrostructureOperation.TRADE_FLOW: CaptureStream.IQFEED_PRINT,
    CaptureMicrostructureOperation.REALIZED_VOL: CaptureStream.IQFEED_PRINT,
    CaptureMicrostructureOperation.FLOW_SLOPE: CaptureStream.IQFEED_PRINT,
    CaptureMicrostructureOperation.BOOK_IMBALANCE: (
        CaptureStream.L2_DEPTH_CHECKPOINT
    ),
    CaptureMicrostructureOperation.OFI_MICROPRICE: (
        CaptureStream.L2_DEPTH_CHECKPOINT
    ),
    CaptureMicrostructureOperation.LADDER_DISTRIBUTION: (
        CaptureStream.L2_DEPTH_CHECKPOINT
    ),
}

_MICROSTRUCTURE_OPERATION_PARAMETER_FIELDS = {
    CaptureMicrostructureOperation.TRADE_FLOW: frozenset({"window_seconds"}),
    CaptureMicrostructureOperation.REALIZED_VOL: frozenset(
        {"window_seconds", "grid_seconds"}
    ),
    CaptureMicrostructureOperation.FLOW_SLOPE: frozenset(
        {"window_seconds", "grid_seconds", "half_life_steps"}
    ),
    CaptureMicrostructureOperation.BOOK_IMBALANCE: frozenset(
        {"window_seconds", "maximum_snapshot_age_seconds"}
    ),
    CaptureMicrostructureOperation.OFI_MICROPRICE: frozenset(
        {"window_seconds", "multilevel_ofi_enabled"}
    ),
    CaptureMicrostructureOperation.LADDER_DISTRIBUTION: frozenset(
        {"window_seconds", "snapshot_limit", "multilevel_ofi_enabled"}
    ),
}


@dataclass(frozen=True)
class CaptureMicrostructureReadQuery:
    """Runtime-minted complete source window for one pipeline read.

    Bounds are ``(event_start_exclusive, event_end_inclusive]``.  The capture
    lifecycle, never the strategy caller, supplies ``source_frontier_sequence``
    while holding the same lock used to append provider events and the receipt.
    This prevents a caller-selected subset or a racing late append from being
    represented as the exact result consumed by the FSM.
    """

    operation: CaptureMicrostructureOperation
    stream: CaptureStream
    symbol: str
    provider: str
    event_start_exclusive: datetime
    event_end_inclusive: datetime
    decision_at: datetime
    available_at_most: datetime
    source_frontier_sequence: int
    source_clock_basis: str
    parameters: Mapping[str, Any]
    schema_version: str = MICROSTRUCTURE_READ_QUERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MICROSTRUCTURE_READ_QUERY_SCHEMA_VERSION:
            raise CaptureContractError(
                "microstructure read query schema is unsupported"
            )
        try:
            operation = (
                self.operation
                if isinstance(self.operation, CaptureMicrostructureOperation)
                else CaptureMicrostructureOperation(str(self.operation))
            )
            stream = (
                self.stream
                if isinstance(self.stream, CaptureStream)
                else CaptureStream(str(self.stream))
            )
        except ValueError as exc:
            raise CaptureContractError(
                "microstructure read operation/stream is unknown"
            ) from exc
        if _MICROSTRUCTURE_OPERATION_STREAM[operation] is not stream:
            raise CaptureContractError(
                "microstructure read operation uses the wrong source stream"
            )
        symbol = str(self.symbol or "").strip().upper()
        provider = str(self.provider or "").strip().lower()
        if not symbol or provider != "iqfeed":
            raise CaptureContractError(
                "microstructure read provider identity is invalid"
            )
        start = _utc(self.event_start_exclusive, "event_start_exclusive")
        end = _utc(self.event_end_inclusive, "event_end_inclusive")
        decision = _utc(self.decision_at, "decision_at")
        available = _utc(self.available_at_most, "available_at_most")
        if not start < end or end != decision or available < decision:
            raise CaptureContractError(
                "microstructure read clocks are inconsistent"
            )
        frontier = self.source_frontier_sequence
        if isinstance(frontier, bool) or not isinstance(frontier, int) or frontier < 0:
            raise CaptureContractError(
                "microstructure source frontier must be a nonnegative integer"
            )
        expected_clock = (
            "provider_event_at"
            if stream is CaptureStream.IQFEED_PRINT
            else "market_reference_at"
        )
        clock_basis = str(self.source_clock_basis or "").strip()
        if clock_basis != expected_clock:
            raise CaptureContractError(
                "microstructure read source clock basis is invalid"
            )
        if not isinstance(self.parameters, Mapping):
            raise CaptureContractError(
                "microstructure read parameters are malformed"
            )
        parameters = dict(self.parameters)
        expected_fields = _MICROSTRUCTURE_OPERATION_PARAMETER_FIELDS[operation]
        if set(parameters) != expected_fields:
            raise CaptureContractError(
                "microstructure read parameter fields do not match operation"
            )
        for name in expected_fields:
            value = parameters[name]
            if name == "multilevel_ofi_enabled":
                if type(value) is not bool:
                    raise CaptureContractError(
                        "microstructure multilevel OFI flag must be boolean"
                    )
                continue
            if name == "snapshot_limit":
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise CaptureContractError(
                        "microstructure snapshot limit must be positive"
                    )
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise CaptureContractError(
                    f"microstructure {name} must be a finite positive number"
                )
            parsed = float(value)
            if not math.isfinite(parsed) or parsed <= 0.0:
                raise CaptureContractError(
                    f"microstructure {name} must be a finite positive number"
                )
            parameters[name] = parsed
        window_seconds = float(parameters["window_seconds"])
        if not math.isclose(
            (end - start).total_seconds(),
            window_seconds,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise CaptureContractError(
                "microstructure read window differs from its parameters"
            )
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "stream", stream)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "event_start_exclusive", start)
        object.__setattr__(self, "event_end_inclusive", end)
        object.__setattr__(self, "decision_at", decision)
        object.__setattr__(self, "available_at_most", available)
        object.__setattr__(self, "source_frontier_sequence", frontier)
        object.__setattr__(self, "source_clock_basis", clock_basis)
        object.__setattr__(self, "parameters", _freeze_canonical_json(parameters))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation": self.operation.value,
            "stream": self.stream.value,
            "symbol": self.symbol,
            "provider": self.provider,
            "event_start_exclusive": _iso_utc(self.event_start_exclusive),
            "event_end_inclusive": _iso_utc(self.event_end_inclusive),
            "decision_at": _iso_utc(self.decision_at),
            "available_at_most": _iso_utc(self.available_at_most),
            "source_frontier_sequence": self.source_frontier_sequence,
            "source_clock_basis": self.source_clock_basis,
            "parameters": dict(self.parameters),
        }

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any]
    ) -> "CaptureMicrostructureReadQuery":
        expected = {
            "schema_version",
            "operation",
            "stream",
            "symbol",
            "provider",
            "event_start_exclusive",
            "event_end_inclusive",
            "decision_at",
            "available_at_most",
            "source_frontier_sequence",
            "source_clock_basis",
            "parameters",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise CaptureContractError(
                "microstructure read query fields do not match schema"
            )
        parameters = raw.get("parameters")
        if not isinstance(parameters, Mapping):
            raise CaptureContractError(
                "microstructure read query parameters are malformed"
            )
        return cls(
            schema_version=str(raw.get("schema_version") or ""),
            operation=CaptureMicrostructureOperation(
                str(raw.get("operation") or "")
            ),
            stream=CaptureStream(str(raw.get("stream") or "")),
            symbol=str(raw.get("symbol") or ""),
            provider=str(raw.get("provider") or ""),
            event_start_exclusive=_parse_utc(
                raw.get("event_start_exclusive"), "event_start_exclusive"
            ),
            event_end_inclusive=_parse_utc(
                raw.get("event_end_inclusive"), "event_end_inclusive"
            ),
            decision_at=_parse_utc(raw.get("decision_at"), "decision_at"),
            available_at_most=_parse_utc(
                raw.get("available_at_most"), "available_at_most"
            ),
            source_frontier_sequence=raw.get("source_frontier_sequence"),
            source_clock_basis=str(raw.get("source_clock_basis") or ""),
            parameters=parameters,
        )


def _scanner_optional_number(
    value: Any,
    field_name: str,
    *,
    nonnegative: bool = False,
) -> float | None:
    """Strict JSON number used by scanner provenance; booleans are never numbers."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CaptureContractError(f"{field_name} must be a finite number or null")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise CaptureContractError(f"{field_name} must be a finite number or null")
    if nonnegative and parsed < 0.0:
        raise CaptureContractError(f"{field_name} cannot be negative")
    return parsed


@dataclass(frozen=True)
class CaptureScannerProfile:
    """Resolved scanner profile whose exact values are query-hash provenance."""

    profile_id: str
    asset_class: str
    price_min: float | None
    price_max: float | None
    min_dollar_volume: float | None
    min_change_pct: float | None
    snapshot_max_age_seconds: float

    def __post_init__(self) -> None:
        profile_id = str(self.profile_id or "").strip().lower()
        if not profile_id or profile_id != self.profile_id:
            raise CaptureContractError("scanner profile_id must be canonical lowercase")
        asset_class = str(self.asset_class or "").strip().lower()
        if asset_class != "equity" or asset_class != self.asset_class:
            raise CaptureContractError("scanner profile asset_class must be canonical equity")
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "asset_class", asset_class)

        price_min = _scanner_optional_number(
            self.price_min, "scanner profile price_min", nonnegative=True
        )
        price_max = _scanner_optional_number(
            self.price_max, "scanner profile price_max", nonnegative=True
        )
        min_dollar_volume = _scanner_optional_number(
            self.min_dollar_volume,
            "scanner profile min_dollar_volume",
            nonnegative=True,
        )
        min_change_pct = _scanner_optional_number(
            self.min_change_pct, "scanner profile min_change_pct"
        )
        max_age = _scanner_optional_number(
            self.snapshot_max_age_seconds,
            "scanner profile snapshot_max_age_seconds",
        )
        if max_age is None or max_age <= 0.0:
            raise CaptureContractError(
                "scanner profile snapshot_max_age_seconds must be positive"
            )
        if price_min is not None and price_max is not None and price_min > price_max:
            raise CaptureContractError("scanner profile price bounds are inverted")
        object.__setattr__(self, "price_min", price_min)
        object.__setattr__(self, "price_max", price_max)
        object.__setattr__(self, "min_dollar_volume", min_dollar_volume)
        object.__setattr__(self, "min_change_pct", min_change_pct)
        object.__setattr__(self, "snapshot_max_age_seconds", max_age)

    @cached_property
    def profile_sha256(self) -> str:
        return sha256_json(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "asset_class": self.asset_class,
            "price_min": self.price_min,
            "price_max": self.price_max,
            "min_dollar_volume": self.min_dollar_volume,
            "min_change_pct": self.min_change_pct,
            "snapshot_max_age_seconds": self.snapshot_max_age_seconds,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CaptureScannerProfile":
        expected = {
            "profile_id",
            "asset_class",
            "price_min",
            "price_max",
            "min_dollar_volume",
            "min_change_pct",
            "snapshot_max_age_seconds",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise CaptureContractError("scanner profile fields do not match schema")
        return cls(**dict(raw))


@dataclass(frozen=True)
class CaptureScannerSnapshotQuery:
    """Exact Massive full-snapshot call plus resolved strategy provenance."""

    symbol: str
    include_otc: bool
    max_age_seconds: float
    provider_cache_ttl_seconds: float
    profile: CaptureScannerProfile
    profile_sha256: str
    config_sha256: str
    provider: str = SCANNER_SNAPSHOT_PROVIDER
    operation: str = SCANNER_SNAPSHOT_PROVIDER_OPERATION
    schema_version: str = SCANNER_SNAPSHOT_QUERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCANNER_SNAPSHOT_QUERY_SCHEMA_VERSION:
            raise CaptureContractError("scanner query schema version is unsupported")
        if self.provider != SCANNER_SNAPSHOT_PROVIDER:
            raise CaptureContractError("scanner query provider is unsupported")
        if self.operation != SCANNER_SNAPSHOT_PROVIDER_OPERATION:
            raise CaptureContractError("scanner query operation is unsupported")
        symbol = str(self.symbol or "").strip().upper()
        if (
            not symbol
            or symbol != self.symbol
            or "-USD" in symbol
            or "/" in symbol
        ):
            raise CaptureContractError("scanner query symbol must be canonical equity")
        if type(self.include_otc) is not bool:
            raise CaptureContractError("scanner query include_otc must be boolean")
        max_age = _scanner_optional_number(
            self.max_age_seconds, "scanner query max_age_seconds"
        )
        cache_ttl = _scanner_optional_number(
            self.provider_cache_ttl_seconds,
            "scanner query provider_cache_ttl_seconds",
        )
        if max_age is None or max_age <= 0.0:
            raise CaptureContractError("scanner query max_age_seconds must be positive")
        if cache_ttl is None or cache_ttl <= 0.0:
            raise CaptureContractError(
                "scanner query provider_cache_ttl_seconds must be positive"
            )
        expected_cache_ttl = min(
            SCANNER_SNAPSHOT_PROVIDER_MAX_CACHE_TTL_SECONDS,
            max(SCANNER_SNAPSHOT_PROVIDER_MIN_CACHE_TTL_SECONDS, max_age),
        )
        if cache_ttl != expected_cache_ttl:
            raise CaptureContractError(
                "scanner query cache TTL differs from provider max_age semantics"
            )
        if not isinstance(self.profile, CaptureScannerProfile):
            raise CaptureContractError("scanner query profile is malformed")
        if max_age != self.profile.snapshot_max_age_seconds:
            raise CaptureContractError(
                "scanner query max_age differs from its resolved profile"
            )
        profile_sha256 = _require_sha256(
            self.profile_sha256, "scanner query profile_sha256"
        )
        if profile_sha256 != self.profile.profile_sha256:
            raise CaptureContractError("scanner query profile content hash mismatch")
        object.__setattr__(
            self,
            "config_sha256",
            _require_sha256(self.config_sha256, "scanner query config_sha256"),
        )
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "max_age_seconds", max_age)
        object.__setattr__(self, "provider_cache_ttl_seconds", cache_ttl)
        object.__setattr__(self, "profile_sha256", profile_sha256)

    @cached_property
    def query_sha256(self) -> str:
        return sha256_json(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "operation": self.operation,
            "symbol": self.symbol,
            "include_otc": self.include_otc,
            "max_age_seconds": self.max_age_seconds,
            "provider_cache_ttl_seconds": self.provider_cache_ttl_seconds,
            "profile": self.profile.to_dict(),
            "profile_sha256": self.profile_sha256,
            "config_sha256": self.config_sha256,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CaptureScannerSnapshotQuery":
        expected = {
            "schema_version",
            "provider",
            "operation",
            "symbol",
            "include_otc",
            "max_age_seconds",
            "provider_cache_ttl_seconds",
            "profile",
            "profile_sha256",
            "config_sha256",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise CaptureContractError("scanner query fields do not match schema")
        raw_profile = raw.get("profile")
        if not isinstance(raw_profile, Mapping):
            raise CaptureContractError("scanner query profile is malformed")
        query = cls(
            schema_version=raw.get("schema_version"),
            provider=raw.get("provider"),
            operation=raw.get("operation"),
            symbol=raw.get("symbol"),
            include_otc=raw.get("include_otc"),
            max_age_seconds=raw.get("max_age_seconds"),
            provider_cache_ttl_seconds=raw.get("provider_cache_ttl_seconds"),
            profile=CaptureScannerProfile.from_dict(raw_profile),
            profile_sha256=raw.get("profile_sha256"),
            config_sha256=raw.get("config_sha256"),
        )
        if sha256_json(query.to_dict()) != sha256_json(raw):
            raise CaptureContractError("scanner query is not canonically serialized")
        return query


def _scanner_provider_epoch_utc(value: Any, field_name: str) -> datetime | None:
    """Decode one exact Massive integer epoch without float rounding."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CaptureContractError(f"{field_name} must be a positive integer epoch")
    if value >= 10**17:  # nanoseconds
        seconds, remainder = divmod(value, 1_000_000_000)
        microseconds = remainder // 1_000
    elif value >= 10**14:  # microseconds
        seconds, microseconds = divmod(value, 1_000_000)
    elif value >= 10**11:  # milliseconds
        seconds, milliseconds = divmod(value, 1_000)
        microseconds = milliseconds * 1_000
    else:  # seconds
        seconds = value
        microseconds = 0
    try:
        resolved = datetime.fromtimestamp(seconds, tz=UTC).replace(
            microsecond=microseconds
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise CaptureContractError(f"{field_name} is outside the UTC clock range") from exc
    if not (datetime(2000, 1, 1, tzinfo=UTC) <= resolved < datetime(2100, 1, 1, tzinfo=UTC)):
        raise CaptureContractError(f"{field_name} is outside the supported market era")
    return resolved


def _scanner_source_projection(
    raw: Mapping[str, Any], *, symbol: str
) -> dict[str, Any]:
    expected = {
        "ticker",
        "todaysChangePerc",
        "updated",
        "lastTrade",
        "day",
        "min",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise CaptureContractError("scanner source projection fields do not match schema")
    ticker = str(raw.get("ticker") or "").strip().upper()
    if ticker != raw.get("ticker") or ticker != symbol:
        raise CaptureContractError("scanner source projection symbol mismatch")
    last_trade = raw.get("lastTrade")
    day = raw.get("day")
    minute = raw.get("min")
    if not isinstance(last_trade, Mapping) or set(last_trade) != {"p", "t"}:
        raise CaptureContractError("scanner lastTrade projection fields do not match schema")
    if not isinstance(day, Mapping) or set(day) != {"c", "vw", "v"}:
        raise CaptureContractError("scanner day projection fields do not match schema")
    if not isinstance(minute, Mapping) or set(minute) != {"c", "av"}:
        raise CaptureContractError("scanner minute projection fields do not match schema")
    return {
        "ticker": ticker,
        "todaysChangePerc": _scanner_optional_number(
            raw.get("todaysChangePerc"), "scanner source todaysChangePerc"
        ),
        "updated": raw.get("updated"),
        "lastTrade": {
            "p": _scanner_optional_number(
                last_trade.get("p"), "scanner source lastTrade.p", nonnegative=True
            ),
            "t": last_trade.get("t"),
        },
        "day": {
            "c": _scanner_optional_number(
                day.get("c"), "scanner source day.c", nonnegative=True
            ),
            "vw": _scanner_optional_number(
                day.get("vw"), "scanner source day.vw", nonnegative=True
            ),
            "v": _scanner_optional_number(
                day.get("v"), "scanner source day.v", nonnegative=True
            ),
        },
        "min": {
            "c": _scanner_optional_number(
                minute.get("c"), "scanner source min.c", nonnegative=True
            ),
            "av": _scanner_optional_number(
                minute.get("av"), "scanner source min.av", nonnegative=True
            ),
        },
    }


def _scanner_market_reference_at(source: Mapping[str, Any]) -> datetime:
    clocks = tuple(
        value
        for value in (
            _scanner_provider_epoch_utc(
                source.get("updated"), "scanner source updated"
            ),
            _scanner_provider_epoch_utc(
                source["lastTrade"].get("t"),
                "scanner source lastTrade.t",
            ),
        )
        if value is not None
    )
    if not clocks:
        raise CaptureContractError(
            "scanner source has no exact Massive market timestamp"
        )
    return max(clocks)


def scanner_snapshot_market_reference_at(
    source_projection: Mapping[str, Any], *, symbol: str
) -> datetime:
    """Return the exact market clock content-bound inside a scanner projection."""

    source = _scanner_source_projection(source_projection, symbol=symbol)
    return _scanner_market_reference_at(source)


def _scanner_resolved_values(source: Mapping[str, Any]) -> dict[str, float | None]:
    last_trade = source["lastTrade"]
    day = source["day"]
    minute = source["min"]
    price = next(
        (
            value
            for value in (
                last_trade["p"],
                day["c"],
                day["vw"],
                minute["c"],
            )
            if value is not None and value > 0.0
        ),
        None,
    )
    positive_volumes = tuple(
        value
        for value in (day["v"], minute["av"])
        if value is not None and value > 0.0
    )
    share_volume = max(positive_volumes) if positive_volumes else None
    dollar_volume = (
        None if price is None or share_volume is None else price * share_volume
    )
    if dollar_volume is not None and not math.isfinite(dollar_volume):
        raise CaptureContractError("scanner resolved dollar_volume is not finite")
    return {
        "price": price,
        "change_pct": source["todaysChangePerc"],
        "share_volume": share_volume,
        "dollar_volume": dollar_volume,
    }


def _scanner_supplied_resolved_values(raw: Mapping[str, Any]) -> dict[str, float | None]:
    expected = {"price", "change_pct", "share_volume", "dollar_volume"}
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise CaptureContractError("scanner resolved fields do not match schema")
    return {
        "price": _scanner_optional_number(
            raw.get("price"), "scanner resolved price", nonnegative=True
        ),
        "change_pct": _scanner_optional_number(
            raw.get("change_pct"), "scanner resolved change_pct"
        ),
        "share_volume": _scanner_optional_number(
            raw.get("share_volume"),
            "scanner resolved share_volume",
            nonnegative=True,
        ),
        "dollar_volume": _scanner_optional_number(
            raw.get("dollar_volume"),
            "scanner resolved dollar_volume",
            nonnegative=True,
        ),
    }


def build_scanner_snapshot_payload(
    query: CaptureScannerSnapshotQuery,
    *,
    market_reference_at: datetime,
    source_projection: Mapping[str, Any],
) -> dict[str, Any]:
    """Build, rather than self-attest, the normalized scanner payload."""

    if not isinstance(query, CaptureScannerSnapshotQuery):
        raise CaptureContractError("scanner payload query is malformed")
    reference = _utc(market_reference_at, "scanner market_reference_at")
    source = _scanner_source_projection(source_projection, symbol=query.symbol)
    if reference != _scanner_market_reference_at(source):
        raise CaptureContractError(
            "scanner market_reference_at differs from exact Massive source clocks"
        )
    resolved = _scanner_resolved_values(source)
    return {
        "schema_version": SCANNER_SNAPSHOT_PAYLOAD_SCHEMA_VERSION,
        "symbol": query.symbol,
        "profile_id": query.profile.profile_id,
        "query_sha256": query.query_sha256,
        "market_reference_at": _iso_utc(reference),
        "source_projection": source,
        "source_projection_sha256": sha256_json(source),
        "resolved": resolved,
        "profile_sha256": query.profile_sha256,
        "config_sha256": query.config_sha256,
    }


@dataclass(frozen=True)
class CaptureScannerSnapshot:
    """Strict, content-bound scanner fact safe for a future ReplayV3 consumer."""

    event: CaptureEvent
    query: CaptureScannerSnapshotQuery
    source_projection: Mapping[str, Any]
    price: float | None
    change_pct: float | None
    share_volume: float | None
    dollar_volume: float | None

    @classmethod
    def from_event(cls, event: CaptureEvent) -> "CaptureScannerSnapshot":
        if not isinstance(event, CaptureEvent):
            raise CaptureContractError("scanner snapshot event is malformed")
        if event.stream is not CaptureStream.SCANNER_SNAPSHOT:
            raise CaptureContractError("scanner snapshot uses the wrong capture stream")
        if not isinstance(event.query, Mapping):
            raise CaptureContractError("scanner snapshot query is missing")
        query = CaptureScannerSnapshotQuery.from_dict(event.query)
        if query.query_sha256 != event.query_sha256:
            raise CaptureContractError("scanner snapshot query content hash mismatch")
        if event.provider != query.provider:
            raise CaptureContractError("scanner snapshot provider/query mismatch")
        if event.symbol != query.symbol:
            raise CaptureContractError("scanner snapshot event/query symbol mismatch")
        if event.identity.config_sha256 != query.config_sha256:
            raise CaptureContractError("scanner snapshot run/query config mismatch")

        payload = resolve_capture_source_payload(event).payload
        expected = {
            "schema_version",
            "symbol",
            "profile_id",
            "query_sha256",
            "market_reference_at",
            "source_projection",
            "source_projection_sha256",
            "resolved",
            "profile_sha256",
            "config_sha256",
        }
        if set(payload) != expected:
            raise CaptureContractError("scanner snapshot payload fields do not match schema")
        if payload.get("schema_version") != SCANNER_SNAPSHOT_PAYLOAD_SCHEMA_VERSION:
            raise CaptureContractError("scanner snapshot payload schema is unsupported")
        if payload.get("symbol") != query.symbol:
            raise CaptureContractError("scanner snapshot payload/query symbol mismatch")
        if payload.get("profile_id") != query.profile.profile_id:
            raise CaptureContractError("scanner snapshot payload/query profile mismatch")
        payload_query_sha256 = _require_sha256(
            payload.get("query_sha256"), "scanner snapshot query_sha256"
        )
        if payload_query_sha256 != query.query_sha256:
            raise CaptureContractError("scanner snapshot payload/query hash mismatch")
        reference = _parse_utc(
            payload.get("market_reference_at"),
            "scanner snapshot market_reference_at",
        )
        if payload.get("market_reference_at") != _iso_utc(reference):
            raise CaptureContractError(
                "scanner snapshot market_reference_at is not canonical UTC"
            )
        if (
            event.clocks.market_reference_at is None
            or reference != event.clocks.market_reference_at
        ):
            raise CaptureContractError("scanner snapshot market-reference clock mismatch")
        if (
            _require_sha256(
                payload.get("profile_sha256"), "scanner snapshot profile_sha256"
            )
            != query.profile_sha256
        ):
            raise CaptureContractError("scanner snapshot profile provenance mismatch")
        if (
            _require_sha256(
                payload.get("config_sha256"), "scanner snapshot config_sha256"
            )
            != query.config_sha256
        ):
            raise CaptureContractError("scanner snapshot config provenance mismatch")

        raw_source = payload.get("source_projection")
        if not isinstance(raw_source, Mapping):
            raise CaptureContractError("scanner source projection is malformed")
        source = _scanner_source_projection(raw_source, symbol=query.symbol)
        if reference != _scanner_market_reference_at(source):
            raise CaptureContractError(
                "scanner provider market clock differs from its source projection"
            )
        source_sha256 = _require_sha256(
            payload.get("source_projection_sha256"),
            "scanner snapshot source_projection_sha256",
        )
        if source_sha256 != sha256_json(source):
            raise CaptureContractError("scanner source projection content hash mismatch")
        raw_resolved = payload.get("resolved")
        if not isinstance(raw_resolved, Mapping):
            raise CaptureContractError("scanner resolved values are malformed")
        supplied_resolved = _scanner_supplied_resolved_values(raw_resolved)
        resolved = _scanner_resolved_values(source)
        if supplied_resolved != resolved:
            raise CaptureContractError(
                "scanner resolved values differ from their source projection"
            )
        return cls(
            event=event,
            query=query,
            source_projection=_freeze_canonical_json(source),
            price=resolved["price"],
            change_pct=resolved["change_pct"],
            share_volume=resolved["share_volume"],
            dollar_volume=resolved["dollar_volume"],
        )


@dataclass(frozen=True)
class CaptureEventRef:
    """Compact, content-addressed evidence used by coverage verification.

    The final sealed-run loader builds these from verified ``CaptureEvent``
    objects.  Coverage grading never accepts a receipt source hash unless it
    resolves through this index.
    """

    identity_sha256: str
    event_sha256: str
    sequence: int
    stream: CaptureStream
    received_at: datetime
    available_at: datetime
    payload_sha256: str
    provider: str
    symbol: str | None = None
    query_sha256: str | None = None
    provider_event_at: datetime | None = None
    market_reference_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "identity_sha256",
            _require_sha256(self.identity_sha256, "event_ref.identity_sha256"),
        )
        object.__setattr__(
            self,
            "event_sha256",
            _require_sha256(self.event_sha256, "event_ref.event_sha256"),
        )
        object.__setattr__(
            self,
            "payload_sha256",
            _require_sha256(self.payload_sha256, "event_ref.payload_sha256"),
        )
        if self.query_sha256 is not None:
            object.__setattr__(
                self,
                "query_sha256",
                _require_sha256(self.query_sha256, "event_ref.query_sha256"),
            )
        if isinstance(self.sequence, bool) or int(self.sequence) <= 0:
            raise CaptureContractError("event_ref.sequence must be positive")
        object.__setattr__(self, "sequence", int(self.sequence))
        try:
            stream = (
                self.stream
                if isinstance(self.stream, CaptureStream)
                else CaptureStream(str(self.stream))
            )
        except ValueError as exc:
            raise CaptureContractError("event_ref stream is unknown") from exc
        object.__setattr__(self, "stream", stream)
        object.__setattr__(
            self, "received_at", _utc(self.received_at, "event_ref.received_at")
        )
        object.__setattr__(
            self, "available_at", _utc(self.available_at, "event_ref.available_at")
        )
        if self.available_at < self.received_at:
            raise CaptureContractError(
                "event_ref available_at cannot precede received_at"
            )
        object.__setattr__(
            self,
            "provider_event_at",
            _optional_utc(self.provider_event_at, "event_ref.provider_event_at"),
        )
        object.__setattr__(
            self,
            "market_reference_at",
            _optional_utc(
                self.market_reference_at, "event_ref.market_reference_at"
            ),
        )
        provider = str(self.provider or "").strip()
        if STREAM_POLICIES[stream].coverage_mode is not CoverageMode.CONTROL and not provider:
            raise CaptureContractError("event_ref provider is required")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(
            self, "symbol", str(self.symbol or "").strip().upper() or None
        )

    @classmethod
    def from_event(cls, event: CaptureEvent) -> "CaptureEventRef":
        return cls(
            identity_sha256=event.identity.identity_sha256,
            event_sha256=event.event_sha256,
            sequence=event.sequence,
            stream=event.stream,
            received_at=event.clocks.received_at,
            available_at=event.clocks.available_at,
            payload_sha256=event.payload_sha256,
            query_sha256=event.query_sha256,
            provider=event.provider,
            symbol=event.symbol,
            provider_event_at=event.clocks.provider_event_at,
            market_reference_at=event.clocks.market_reference_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity_sha256": self.identity_sha256,
            "event_sha256": self.event_sha256,
            "sequence": self.sequence,
            "stream": self.stream.value,
            "received_at": _iso_utc(self.received_at),
            "available_at": _iso_utc(self.available_at),
            "payload_sha256": self.payload_sha256,
            "query_sha256": self.query_sha256,
            "provider": self.provider,
            "symbol": self.symbol,
            "provider_event_at": (
                _iso_utc(self.provider_event_at) if self.provider_event_at else None
            ),
            "market_reference_at": (
                _iso_utc(self.market_reference_at)
                if self.market_reference_at
                else None
            ),
        }


def capture_prefix_root_sha256(
    event_refs: Iterable[CaptureEventRef],
    *,
    identity_sha256: str,
    through_sequence: int,
) -> str:
    """Hash the exact logical input prefix available before one FSM decision."""

    identity = _require_sha256(identity_sha256, "prefix.identity_sha256")
    if isinstance(through_sequence, bool) or int(through_sequence) <= 0:
        raise CaptureContractError("prefix through_sequence must be positive")
    through = int(through_sequence)
    rows = []
    seen: set[int] = set()
    for ref in sorted(event_refs, key=lambda item: (item.sequence, item.event_sha256)):
        if ref.identity_sha256 != identity:
            raise CaptureContractError("capture prefix mixes run identities")
        if ref.sequence in seen:
            raise CaptureContractError("capture prefix contains duplicate sequences")
        seen.add(ref.sequence)
        if ref.sequence <= through:
            rows.append(
                {
                    "sequence": ref.sequence,
                    "event_sha256": ref.event_sha256,
                    "available_at": _iso_utc(ref.available_at),
                }
            )
    if not rows or rows[-1]["sequence"] != through:
        raise CaptureContractError("capture prefix frontier event is missing")
    sequences = [int(row["sequence"]) for row in rows]
    if sequences != list(range(1, through + 1)):
        raise CaptureContractError(
            "capture prefix is not contiguous from generation sequence 1"
        )
    return sha256_json(
        {
            "identity_sha256": identity,
            "through_sequence": through,
            "events": rows,
        }
    )


def captured_read_result_sha256(event_refs: Sequence[CaptureEventRef]) -> str:
    """Hash the exact ordered content addresses returned by a captured read."""

    return sha256_json(
        {
            "source_events": [
                {
                    "event_sha256": ref.event_sha256,
                    "payload_sha256": ref.payload_sha256,
                }
                for ref in event_refs
            ]
        }
    )


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CaptureContractError(f"{field_name} must be a mapping")
    return value


@dataclass(frozen=True)
class CoverageGap:
    stream: CaptureStream
    reason: str
    first_available_at: datetime
    last_available_at: datetime
    lost_count: int
    symbol: str | None = None

    def __post_init__(self) -> None:
        try:
            stream = (
                self.stream
                if isinstance(self.stream, CaptureStream)
                else CaptureStream(str(self.stream))
            )
        except ValueError as exc:
            raise CaptureContractError(f"unknown gap stream: {self.stream}") from exc
        object.__setattr__(self, "stream", stream)
        first = _utc(self.first_available_at, "gap.first_available_at")
        last = _utc(self.last_available_at, "gap.last_available_at")
        if last < first:
            raise CaptureContractError("gap end cannot precede gap start")
        if isinstance(self.lost_count, bool) or int(self.lost_count) <= 0:
            raise CaptureContractError("gap lost_count must be positive")
        object.__setattr__(self, "first_available_at", first)
        object.__setattr__(self, "last_available_at", last)
        object.__setattr__(self, "lost_count", int(self.lost_count))
        object.__setattr__(
            self, "symbol", str(self.symbol or "").strip().upper() or None
        )
        reason = str(self.reason or "").strip()
        if not reason:
            raise CaptureContractError("gap reason is required")
        object.__setattr__(self, "reason", reason)

    def intersects(self, start: datetime, end: datetime) -> bool:
        return self.first_available_at <= end and self.last_available_at >= start

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream": self.stream.value,
            "reason": self.reason,
            "first_available_at": _iso_utc(self.first_available_at),
            "last_available_at": _iso_utc(self.last_available_at),
            "lost_count": self.lost_count,
            "symbol": self.symbol,
        }

@dataclass(frozen=True)
class ProviderWatermark:
    stream: CaptureStream
    provider: str
    identity_sha256: str
    event_watermark_at: datetime
    emitted_available_at: datetime
    bounded_lateness_seconds: float
    max_observed_lateness_seconds: float
    generation: int
    symbol: str | None = None

    def __post_init__(self) -> None:
        try:
            stream = (
                self.stream
                if isinstance(self.stream, CaptureStream)
                else CaptureStream(str(self.stream))
            )
        except ValueError as exc:
            raise CaptureContractError(
                f"unknown watermark stream: {self.stream}"
            ) from exc
        object.__setattr__(self, "stream", stream)
        provider = str(self.provider or "").strip()
        if not provider:
            raise CaptureContractError("watermark provider is required")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(
            self,
            "identity_sha256",
            _require_sha256(self.identity_sha256, "watermark.identity_sha256"),
        )
        object.__setattr__(
            self, "symbol", str(self.symbol or "").strip().upper() or None
        )
        object.__setattr__(
            self,
            "event_watermark_at",
            _utc(self.event_watermark_at, "event_watermark_at"),
        )
        object.__setattr__(
            self,
            "emitted_available_at",
            _utc(self.emitted_available_at, "emitted_available_at"),
        )
        if self.emitted_available_at < self.event_watermark_at:
            raise CaptureContractError(
                "watermark cannot become available before its event-time frontier"
            )
        for name in ("bounded_lateness_seconds", "max_observed_lateness_seconds"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise CaptureContractError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, value)
        if self.max_observed_lateness_seconds > self.bounded_lateness_seconds:
            raise CaptureContractError("observed lateness exceeds the provider bound")
        if isinstance(self.generation, bool) or int(self.generation) <= 0:
            raise CaptureContractError("watermark generation must be positive")
        object.__setattr__(self, "generation", int(self.generation))

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream": self.stream.value,
            "provider": self.provider,
            "identity_sha256": self.identity_sha256,
            "event_watermark_at": _iso_utc(self.event_watermark_at),
            "emitted_available_at": _iso_utc(self.emitted_available_at),
            "bounded_lateness_seconds": self.bounded_lateness_seconds,
            "max_observed_lateness_seconds": self.max_observed_lateness_seconds,
            "generation": self.generation,
            "symbol": self.symbol,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProviderWatermark":
        expected = {
            "stream",
            "provider",
            "identity_sha256",
            "event_watermark_at",
            "emitted_available_at",
            "bounded_lateness_seconds",
            "max_observed_lateness_seconds",
            "generation",
            "symbol",
        }
        if set(raw) != expected:
            raise CaptureContractError("provider watermark fields do not match schema")
        return cls(
            stream=CaptureStream(str(raw.get("stream") or "")),
            provider=str(raw.get("provider") or ""),
            identity_sha256=str(raw.get("identity_sha256") or ""),
            event_watermark_at=_parse_utc(
                raw.get("event_watermark_at"), "event_watermark_at"
            ),
            emitted_available_at=_parse_utc(
                raw.get("emitted_available_at"), "emitted_available_at"
            ),
            bounded_lateness_seconds=float(raw.get("bounded_lateness_seconds")),
            max_observed_lateness_seconds=float(
                raw.get("max_observed_lateness_seconds")
            ),
            generation=raw.get("generation"),
            symbol=raw.get("symbol"),
        )


@dataclass(frozen=True)
class CaptureProviderRegistrationEvidence:
    """Exact first provider frame used to authorize one external producer."""

    producer_id: str
    provider: str
    provider_instance_id: str
    provider_generation: int
    evidence_kind: str
    source_payload_sha256: str
    provider_event_at: datetime
    received_at: datetime
    provider_sequence: int
    subscription_request_sha256: str | None = None

    def __post_init__(self) -> None:
        producer_id = str(self.producer_id or "").strip().lower()
        provider = str(self.provider or "").strip().lower()
        evidence_kind = str(self.evidence_kind or "").strip().lower()
        if _PRODUCER_ID_RE.fullmatch(producer_id) is None or not provider:
            raise CaptureContractError("provider registration identity is incomplete")
        if evidence_kind != "first_provider_frame":
            raise CaptureContractError(
                "external producer registration requires a first provider frame"
            )
        instance_id = _uuid_text(
            self.provider_instance_id, "provider_registration.provider_instance_id"
        )
        if isinstance(self.provider_generation, bool) or int(
            self.provider_generation
        ) <= 0:
            raise CaptureContractError(
                "provider registration generation must be positive"
            )
        if isinstance(self.provider_sequence, bool) or int(self.provider_sequence) < 0:
            raise CaptureContractError(
                "provider registration sequence must be nonnegative"
            )
        payload_sha = _require_sha256(
            self.source_payload_sha256,
            "provider_registration.source_payload_sha256",
        )
        subscription_sha = self.subscription_request_sha256
        if subscription_sha is not None:
            subscription_sha = _require_sha256(
                subscription_sha,
                "provider_registration.subscription_request_sha256",
            )
        provider_event_at = _utc(
            self.provider_event_at, "registration provider_event_at"
        )
        received_at = _utc(self.received_at, "registration received_at")
        if received_at < provider_event_at:
            raise CaptureContractError(
                "provider registration receive clock precedes provider event"
            )
        object.__setattr__(self, "producer_id", producer_id)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "provider_instance_id", instance_id)
        object.__setattr__(self, "provider_generation", int(self.provider_generation))
        object.__setattr__(self, "provider_sequence", int(self.provider_sequence))
        object.__setattr__(self, "evidence_kind", evidence_kind)
        object.__setattr__(self, "source_payload_sha256", payload_sha)
        object.__setattr__(self, "subscription_request_sha256", subscription_sha)
        object.__setattr__(self, "provider_event_at", provider_event_at)
        object.__setattr__(self, "received_at", received_at)

    @property
    def evidence_sha256(self) -> str:
        return sha256_json(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CAPTURE_PROVIDER_REGISTRATION_SCHEMA_VERSION,
            "producer_id": self.producer_id,
            "provider": self.provider,
            "provider_instance_id": self.provider_instance_id,
            "provider_generation": self.provider_generation,
            "evidence_kind": self.evidence_kind,
            "source_payload_sha256": self.source_payload_sha256,
            "provider_event_at": _iso_utc(self.provider_event_at),
            "received_at": _iso_utc(self.received_at),
            "provider_sequence": self.provider_sequence,
            "subscription_request_sha256": self.subscription_request_sha256,
        }

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any]
    ) -> "CaptureProviderRegistrationEvidence":
        expected = {
            "schema_version",
            "producer_id",
            "provider",
            "provider_instance_id",
            "provider_generation",
            "evidence_kind",
            "source_payload_sha256",
            "provider_event_at",
            "received_at",
            "provider_sequence",
            "subscription_request_sha256",
        }
        if set(raw) != expected or raw.get("schema_version") != (
            CAPTURE_PROVIDER_REGISTRATION_SCHEMA_VERSION
        ):
            raise CaptureContractError("provider registration fields do not match schema")
        return cls(
            producer_id=str(raw.get("producer_id") or ""),
            provider=str(raw.get("provider") or ""),
            provider_instance_id=str(raw.get("provider_instance_id") or ""),
            provider_generation=raw.get("provider_generation"),
            evidence_kind=str(raw.get("evidence_kind") or ""),
            source_payload_sha256=str(raw.get("source_payload_sha256") or ""),
            provider_event_at=_parse_utc(
                raw.get("provider_event_at"), "provider_event_at"
            ),
            received_at=_parse_utc(raw.get("received_at"), "received_at"),
            provider_sequence=raw.get("provider_sequence"),
            subscription_request_sha256=raw.get("subscription_request_sha256"),
        )


@dataclass(frozen=True)
class CaptureProviderRegistrationRecord:
    """Durable control record later matched to the exact first source event."""

    identity_sha256: str
    evidence: CaptureProviderRegistrationEvidence
    source_stream: CaptureStream
    source_symbol: str | None
    source_query_sha256: str | None
    source_available_at: datetime
    source_market_reference_at: datetime | None = None
    kind: str = "PROVIDER_REGISTRATION_EVIDENCE"
    schema_version: str = CAPTURE_PROVIDER_REGISTRATION_RECORD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            self.schema_version != CAPTURE_PROVIDER_REGISTRATION_RECORD_SCHEMA_VERSION
            or self.kind != "PROVIDER_REGISTRATION_EVIDENCE"
        ):
            raise CaptureContractError("provider registration record schema is invalid")
        object.__setattr__(
            self,
            "identity_sha256",
            _require_sha256(self.identity_sha256, "provider_registration.identity_sha256"),
        )
        if not isinstance(self.evidence, CaptureProviderRegistrationEvidence):
            raise CaptureContractError("provider registration evidence is malformed")
        try:
            stream = (
                self.source_stream
                if isinstance(self.source_stream, CaptureStream)
                else CaptureStream(str(self.source_stream))
            )
        except ValueError as exc:
            raise CaptureContractError("provider registration stream is unknown") from exc
        if STREAM_POLICIES[stream].coverage_mode is CoverageMode.CONTROL:
            raise CaptureContractError("provider registration cannot prove a control stream")
        object.__setattr__(self, "source_stream", stream)
        symbol = str(self.source_symbol or "").strip().upper() or None
        object.__setattr__(self, "source_symbol", symbol)
        query_sha = self.source_query_sha256
        if query_sha is not None:
            query_sha = _require_sha256(
                query_sha, "provider_registration.source_query_sha256"
            )
        object.__setattr__(self, "source_query_sha256", query_sha)
        object.__setattr__(
            self,
            "source_available_at",
            _utc(self.source_available_at, "provider registration source_available_at"),
        )
        if self.source_market_reference_at is not None:
            object.__setattr__(
                self,
                "source_market_reference_at",
                _utc(
                    self.source_market_reference_at,
                    "provider registration source_market_reference_at",
                ),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "identity_sha256": self.identity_sha256,
            "evidence": self.evidence.to_dict(),
            "evidence_sha256": self.evidence.evidence_sha256,
            "source_stream": self.source_stream.value,
            "source_symbol": self.source_symbol,
            "source_query_sha256": self.source_query_sha256,
            "source_available_at": _iso_utc(self.source_available_at),
            "source_market_reference_at": (
                None
                if self.source_market_reference_at is None
                else _iso_utc(self.source_market_reference_at)
            ),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CaptureProviderRegistrationRecord":
        expected = {
            "schema_version",
            "kind",
            "identity_sha256",
            "evidence",
            "evidence_sha256",
            "source_stream",
            "source_symbol",
            "source_query_sha256",
            "source_available_at",
            "source_market_reference_at",
        }
        if set(raw) != expected:
            raise CaptureContractError(
                "provider registration record fields do not match schema"
            )
        evidence = CaptureProviderRegistrationEvidence.from_dict(
            _mapping(raw.get("evidence"), "provider registration evidence")
        )
        if raw.get("evidence_sha256") != evidence.evidence_sha256:
            raise CaptureContractError("provider registration evidence hash mismatch")
        return cls(
            schema_version=str(raw.get("schema_version") or ""),
            kind=str(raw.get("kind") or ""),
            identity_sha256=str(raw.get("identity_sha256") or ""),
            evidence=evidence,
            source_stream=CaptureStream(str(raw.get("source_stream") or "")),
            source_symbol=raw.get("source_symbol"),
            source_query_sha256=raw.get("source_query_sha256"),
            source_available_at=_parse_utc(
                raw.get("source_available_at"), "source_available_at"
            ),
            source_market_reference_at=(
                None
                if raw.get("source_market_reference_at") is None
                else _parse_utc(
                    raw.get("source_market_reference_at"),
                    "source_market_reference_at",
                )
            ),
        )


@dataclass(frozen=True)
class CaptureExternalProducerGeneration:
    """Hash-bound host observation used to build a future RUN_OPEN roster.

    This is not provider registration authority.  It pins which local bridge
    process/generation may later prove itself with an exact provider frame.
    """

    producer_id: str
    provider: str
    provider_instance_id: str
    provider_generation: int
    streams: tuple[CaptureStream, ...]
    bridge_source_sha256: str
    bridge_configuration_sha256: str
    capture_resource_binding_sha256: str
    handoff_configuration_sha256: str
    observed_at: datetime
    schema_version: str = CAPTURE_EXTERNAL_PRODUCER_GENERATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURE_EXTERNAL_PRODUCER_GENERATION_SCHEMA_VERSION:
            raise CaptureContractError(
                "external producer generation schema is unsupported"
            )
        producer_id = str(self.producer_id or "").strip().lower()
        provider = str(self.provider or "").strip().lower()
        if _PRODUCER_ID_RE.fullmatch(producer_id) is None or not provider:
            raise CaptureContractError("external producer generation identity is invalid")
        object.__setattr__(self, "producer_id", producer_id)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(
            self,
            "provider_instance_id",
            _uuid_text(
                self.provider_instance_id,
                "external producer generation provider_instance_id",
            ),
        )
        if isinstance(self.provider_generation, bool) or int(
            self.provider_generation
        ) <= 0:
            raise CaptureContractError(
                "external producer generation must be positive"
            )
        object.__setattr__(
            self, "provider_generation", int(self.provider_generation)
        )
        streams = tuple(
            stream
            if isinstance(stream, CaptureStream)
            else CaptureStream(str(stream))
            for stream in self.streams
        )
        if (
            not streams
            or len(streams) != len(set(streams))
            or any(
                STREAM_POLICIES[stream].coverage_mode is CoverageMode.CONTROL
                for stream in streams
            )
        ):
            raise CaptureContractError(
                "external producer generation streams are malformed"
            )
        object.__setattr__(
            self, "streams", tuple(sorted(streams, key=lambda row: row.value))
        )
        for field_name in (
            "bridge_source_sha256",
            "bridge_configuration_sha256",
            "capture_resource_binding_sha256",
            "handoff_configuration_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_sha256(
                    str(getattr(self, field_name) or ""),
                    f"external producer generation {field_name}",
                ),
            )
        object.__setattr__(
            self,
            "observed_at",
            _utc(self.observed_at, "external producer generation observed_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "producer_id": self.producer_id,
            "provider": self.provider,
            "provider_instance_id": self.provider_instance_id,
            "provider_generation": self.provider_generation,
            "streams": [stream.value for stream in self.streams],
            "bridge_source_sha256": self.bridge_source_sha256,
            "bridge_configuration_sha256": self.bridge_configuration_sha256,
            "capture_resource_binding_sha256": (
                self.capture_resource_binding_sha256
            ),
            "handoff_configuration_sha256": self.handoff_configuration_sha256,
            "observed_at": _iso_utc(self.observed_at),
        }

    @property
    def evidence_sha256(self) -> str:
        return sha256_json(self.to_dict())

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any]
    ) -> "CaptureExternalProducerGeneration":
        expected = {
            "schema_version",
            "producer_id",
            "provider",
            "provider_instance_id",
            "provider_generation",
            "streams",
            "bridge_source_sha256",
            "bridge_configuration_sha256",
            "capture_resource_binding_sha256",
            "handoff_configuration_sha256",
            "observed_at",
        }
        if set(raw) != expected:
            raise CaptureContractError(
                "external producer generation fields do not match schema"
            )
        raw_streams = raw.get("streams")
        if not isinstance(raw_streams, (list, tuple)):
            raise CaptureContractError(
                "external producer generation streams are malformed"
            )
        return cls(
            schema_version=str(raw.get("schema_version") or ""),
            producer_id=str(raw.get("producer_id") or ""),
            provider=str(raw.get("provider") or ""),
            provider_instance_id=str(raw.get("provider_instance_id") or ""),
            provider_generation=raw.get("provider_generation"),
            streams=tuple(CaptureStream(str(stream)) for stream in raw_streams),
            bridge_source_sha256=str(raw.get("bridge_source_sha256") or ""),
            bridge_configuration_sha256=str(
                raw.get("bridge_configuration_sha256") or ""
            ),
            capture_resource_binding_sha256=str(
                raw.get("capture_resource_binding_sha256") or ""
            ),
            handoff_configuration_sha256=str(
                raw.get("handoff_configuration_sha256") or ""
            ),
            observed_at=_parse_utc(raw.get("observed_at"), "observed_at"),
        )


def build_provider_registration_evidence_from_source_event(
    event: CaptureEvent,
    *,
    producer_id: str,
) -> CaptureProviderRegistrationEvidence:
    """Derive IQFeed registration authority from one strict exact source row.

    Quote proxies and locally assembled depth checkpoints intentionally cannot
    register a producer.  They do not contain one exact vendor event clock.
    """

    view = resolve_capture_source_payload(event)
    if event.stream is CaptureStream.IQFEED_PRINT:
        CaptureIqfeedPrint.from_event(event)
        provenance = view.payload.get(IQFEED_L1_SOURCE_PROVENANCE_FIELD)
        if (
            not isinstance(provenance, Mapping)
            or provenance.get("schema_version")
            != IQFEED_EXACT_PRINT_SOURCE_PROVENANCE_SCHEMA_VERSION
        ):
            raise CaptureContractError(
                "IQFeed producer registration requires exact-print provenance"
            )
        provider_event_at = event.clocks.provider_event_at
        assert provider_event_at is not None
        return CaptureProviderRegistrationEvidence(
            producer_id=producer_id,
            provider="iqfeed",
            provider_instance_id=str(provenance.get("bridge_run_id") or ""),
            provider_generation=provenance.get("connection_generation"),
            evidence_kind="first_provider_frame",
            source_payload_sha256=sha256_json(view.payload),
            provider_event_at=provider_event_at,
            received_at=event.clocks.received_at,
            provider_sequence=provenance.get("source_frame_sequence"),
            subscription_request_sha256=str(
                provenance.get("selected_update_fields_ack_sha256") or ""
            ),
        )
    if event.stream is CaptureStream.L2_DEPTH_DELTA:
        CaptureIqfeedL2Delta.from_event(event)
        provenance = view.payload.get(IQFEED_L2_SOURCE_PROVENANCE_FIELD)
        if not isinstance(provenance, Mapping):
            raise CaptureContractError(
                "IQFeed L2 producer registration requires exact delta provenance"
            )
        provider_event_at = event.clocks.provider_event_at
        assert provider_event_at is not None
        return CaptureProviderRegistrationEvidence(
            producer_id=producer_id,
            provider="iqfeed",
            provider_instance_id=str(provenance.get("bridge_run_id") or ""),
            provider_generation=provenance.get("connection_generation"),
            evidence_kind="first_provider_frame",
            source_payload_sha256=sha256_json(view.payload),
            provider_event_at=provider_event_at,
            received_at=event.clocks.received_at,
            provider_sequence=provenance.get("source_frame_sequence"),
        )
    raise CaptureContractError(
        "source event cannot establish exact provider registration"
    )


class CaptureProducerLifecycleKind(str, Enum):
    """Append-only lifecycle facts for one declared capture producer roster."""

    REGISTERED = "PRODUCER_REGISTERED"
    HEARTBEAT = "PRODUCER_HEARTBEAT"
    GAP = "PRODUCER_GAP"
    QUIESCENT = "PRODUCER_QUIESCENT"
    CLOSED = "PRODUCER_CLOSED"
    RUN_CLOSED = "RUN_CLOSED"


@dataclass(frozen=True)
class CaptureProducerSpec:
    """Exact process generation and inputs owned by one required producer."""

    producer_id: str
    instance_id: str
    generation: int
    streams: tuple[CaptureStream, ...]
    code_build_sha256: str
    config_sha256: str
    feature_flags_sha256: str
    resource_binding_sha256: str

    def __post_init__(self) -> None:
        producer_id = str(self.producer_id or "").strip().lower()
        if _PRODUCER_ID_RE.fullmatch(producer_id) is None:
            raise CaptureContractError("producer_id is malformed")
        object.__setattr__(self, "producer_id", producer_id)
        object.__setattr__(
            self, "instance_id", _uuid_text(self.instance_id, "producer.instance_id")
        )
        if isinstance(self.generation, bool) or int(self.generation) <= 0:
            raise CaptureContractError("producer generation must be positive")
        object.__setattr__(self, "generation", int(self.generation))
        normalized_streams: list[CaptureStream] = []
        for raw_stream in self.streams:
            try:
                stream = (
                    raw_stream
                    if isinstance(raw_stream, CaptureStream)
                    else CaptureStream(str(raw_stream))
                )
            except ValueError as exc:
                raise CaptureContractError("producer owns an unknown stream") from exc
            if STREAM_POLICIES[stream].coverage_mode is CoverageMode.CONTROL:
                raise CaptureContractError(
                    "producer roster cannot claim capture control streams"
                )
            normalized_streams.append(stream)
        ordered_streams = tuple(sorted(normalized_streams, key=lambda row: row.value))
        if not ordered_streams:
            raise CaptureContractError("producer must own at least one input stream")
        if len(ordered_streams) != len(set(ordered_streams)):
            raise CaptureContractError("producer stream ownership is duplicated")
        object.__setattr__(self, "streams", ordered_streams)
        for name in (
            "code_build_sha256",
            "config_sha256",
            "feature_flags_sha256",
            "resource_binding_sha256",
        ):
            object.__setattr__(
                self, name, _require_sha256(getattr(self, name), f"producer.{name}")
            )

    @property
    def spec_sha256(self) -> str:
        return sha256_json(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "producer_id": self.producer_id,
            "instance_id": self.instance_id,
            "generation": self.generation,
            "streams": [stream.value for stream in self.streams],
            "code_build_sha256": self.code_build_sha256,
            "config_sha256": self.config_sha256,
            "feature_flags_sha256": self.feature_flags_sha256,
            "resource_binding_sha256": self.resource_binding_sha256,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CaptureProducerSpec":
        expected = {
            "producer_id",
            "instance_id",
            "generation",
            "streams",
            "code_build_sha256",
            "config_sha256",
            "feature_flags_sha256",
            "resource_binding_sha256",
        }
        if set(raw) != expected or not isinstance(raw.get("streams"), list):
            raise CaptureContractError("producer spec fields do not match schema")
        return cls(
            producer_id=str(raw.get("producer_id") or ""),
            instance_id=str(raw.get("instance_id") or ""),
            generation=raw.get("generation"),
            streams=tuple(CaptureStream(str(value)) for value in raw["streams"]),
            code_build_sha256=str(raw.get("code_build_sha256") or ""),
            config_sha256=str(raw.get("config_sha256") or ""),
            feature_flags_sha256=str(raw.get("feature_flags_sha256") or ""),
            resource_binding_sha256=str(raw.get("resource_binding_sha256") or ""),
        )


@dataclass(frozen=True)
class CaptureRunOpen:
    """Sequence-one declaration of every producer required by this run."""

    identity_sha256: str
    run_id: str
    generation: int
    opened_at: datetime
    heartbeat_timeout_seconds: float
    resource_binding_sha256: str
    producers: tuple[CaptureProducerSpec, ...]
    schema_version: str = CAPTURE_PRODUCER_LIFECYCLE_SCHEMA_VERSION
    kind: str = "RUN_OPEN"

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURE_PRODUCER_LIFECYCLE_SCHEMA_VERSION:
            raise CaptureContractError("producer lifecycle schema is unsupported")
        if self.kind != "RUN_OPEN":
            raise CaptureContractError("run-open kind is invalid")
        object.__setattr__(
            self,
            "identity_sha256",
            _require_sha256(self.identity_sha256, "run_open.identity_sha256"),
        )
        object.__setattr__(self, "run_id", _uuid_text(self.run_id, "run_open.run_id"))
        if isinstance(self.generation, bool) or int(self.generation) <= 0:
            raise CaptureContractError("run-open generation must be positive")
        object.__setattr__(self, "generation", int(self.generation))
        object.__setattr__(self, "opened_at", _utc(self.opened_at, "run_open.opened_at"))
        timeout = float(self.heartbeat_timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise CaptureContractError("producer heartbeat timeout must be positive")
        object.__setattr__(self, "heartbeat_timeout_seconds", timeout)
        object.__setattr__(
            self,
            "resource_binding_sha256",
            _require_sha256(
                self.resource_binding_sha256, "run_open.resource_binding_sha256"
            ),
        )
        producers = tuple(self.producers)
        if any(not isinstance(row, CaptureProducerSpec) for row in producers):
            raise CaptureContractError("run-open producer roster is malformed")
        ordered = tuple(sorted(producers, key=lambda row: row.producer_id))
        if not ordered:
            raise CaptureContractError("run-open producer roster cannot be empty")
        ids = [row.producer_id for row in ordered]
        instances = [row.instance_id for row in ordered]
        if len(ids) != len(set(ids)) or len(instances) != len(set(instances)):
            raise CaptureContractError("run-open producer roster contains duplicates")
        object.__setattr__(self, "producers", ordered)

    @property
    def producer_roster_sha256(self) -> str:
        return sha256_json(
            {"producers": [producer.to_dict() for producer in self.producers]}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "identity_sha256": self.identity_sha256,
            "run_id": self.run_id,
            "generation": self.generation,
            "opened_at": _iso_utc(self.opened_at),
            "heartbeat_timeout_seconds": self.heartbeat_timeout_seconds,
            "resource_binding_sha256": self.resource_binding_sha256,
            "producer_roster_sha256": self.producer_roster_sha256,
            "producers": [producer.to_dict() for producer in self.producers],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CaptureRunOpen":
        expected = {
            "schema_version",
            "kind",
            "identity_sha256",
            "run_id",
            "generation",
            "opened_at",
            "heartbeat_timeout_seconds",
            "resource_binding_sha256",
            "producer_roster_sha256",
            "producers",
        }
        producers = raw.get("producers")
        if set(raw) != expected or not isinstance(producers, list):
            raise CaptureContractError("run-open fields do not match schema")
        result = cls(
            schema_version=str(raw.get("schema_version") or ""),
            kind=str(raw.get("kind") or ""),
            identity_sha256=str(raw.get("identity_sha256") or ""),
            run_id=str(raw.get("run_id") or ""),
            generation=raw.get("generation"),
            opened_at=_parse_utc(raw.get("opened_at"), "run_open.opened_at"),
            heartbeat_timeout_seconds=float(raw.get("heartbeat_timeout_seconds")),
            resource_binding_sha256=str(raw.get("resource_binding_sha256") or ""),
            producers=tuple(
                CaptureProducerSpec.from_dict(_mapping(value, "producer"))
                for value in producers
            ),
        )
        expected_roster = _require_sha256(
            str(raw.get("producer_roster_sha256") or ""),
            "producer_roster_sha256",
        )
        if result.producer_roster_sha256 != expected_roster:
            raise CaptureContractError("producer roster content address mismatch")
        return result


@dataclass(frozen=True)
class CaptureProducerLifecycleFact:
    """One non-backdated transition chained to the preceding lifecycle fact."""

    kind: CaptureProducerLifecycleKind
    identity_sha256: str
    producer_roster_sha256: str
    recorded_at: datetime
    frontier_sequence: int
    prior_lifecycle_event_sha256: str | None
    producer_id: str | None = None
    producer_instance_id: str | None = None
    producer_generation: int | None = None
    producer_spec_sha256: str | None = None
    evidence_event_sha256s: tuple[str, ...] = ()
    gap_reason: str | None = None
    producer_close_event_sha256s: Mapping[str, str] = field(default_factory=dict)
    schema_version: str = CAPTURE_PRODUCER_LIFECYCLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURE_PRODUCER_LIFECYCLE_SCHEMA_VERSION:
            raise CaptureContractError("producer lifecycle schema is unsupported")
        try:
            kind = (
                self.kind
                if isinstance(self.kind, CaptureProducerLifecycleKind)
                else CaptureProducerLifecycleKind(str(self.kind))
            )
        except ValueError as exc:
            raise CaptureContractError("producer lifecycle kind is unknown") from exc
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self,
            "identity_sha256",
            _require_sha256(self.identity_sha256, "lifecycle.identity_sha256"),
        )
        object.__setattr__(
            self,
            "producer_roster_sha256",
            _require_sha256(
                self.producer_roster_sha256, "lifecycle.producer_roster_sha256"
            ),
        )
        object.__setattr__(
            self, "recorded_at", _utc(self.recorded_at, "lifecycle.recorded_at")
        )
        if isinstance(self.frontier_sequence, bool) or int(self.frontier_sequence) <= 0:
            raise CaptureContractError("lifecycle frontier_sequence must be positive")
        object.__setattr__(self, "frontier_sequence", int(self.frontier_sequence))
        if self.prior_lifecycle_event_sha256 is not None:
            object.__setattr__(
                self,
                "prior_lifecycle_event_sha256",
                _require_sha256(
                    self.prior_lifecycle_event_sha256,
                    "lifecycle.prior_lifecycle_event_sha256",
                ),
            )
        evidence = tuple(
            sorted(
                _require_sha256(value, "lifecycle.evidence_event_sha256")
                for value in self.evidence_event_sha256s
            )
        )
        if len(evidence) != len(set(evidence)):
            raise CaptureContractError("lifecycle evidence hashes are duplicated")
        object.__setattr__(self, "evidence_event_sha256s", evidence)
        close_hashes: dict[str, str] = {}
        for raw_id, raw_hash in self.producer_close_event_sha256s.items():
            producer_id = str(raw_id or "").strip().lower()
            if _PRODUCER_ID_RE.fullmatch(producer_id) is None:
                raise CaptureContractError("run-close producer id is malformed")
            close_hashes[producer_id] = _require_sha256(
                raw_hash, "run_close.producer_close_event_sha256"
            )
        if len(close_hashes) != len(self.producer_close_event_sha256s):
            raise CaptureContractError("run-close producer ids are duplicated")
        object.__setattr__(
            self, "producer_close_event_sha256s", _FrozenJsonDict(close_hashes)
        )

        if kind is CaptureProducerLifecycleKind.RUN_CLOSED:
            if any(
                value is not None
                for value in (
                    self.producer_id,
                    self.producer_instance_id,
                    self.producer_generation,
                    self.producer_spec_sha256,
                    self.prior_lifecycle_event_sha256,
                    self.gap_reason,
                )
            ) or evidence or not close_hashes:
                raise CaptureContractError("run-close lifecycle fields are inconsistent")
            return

        producer_id = str(self.producer_id or "").strip().lower()
        if _PRODUCER_ID_RE.fullmatch(producer_id) is None:
            raise CaptureContractError("lifecycle producer_id is malformed")
        object.__setattr__(self, "producer_id", producer_id)
        object.__setattr__(
            self,
            "producer_instance_id",
            _uuid_text(self.producer_instance_id or "", "lifecycle.producer_instance_id"),
        )
        if isinstance(self.producer_generation, bool) or int(
            self.producer_generation or 0
        ) <= 0:
            raise CaptureContractError("lifecycle producer generation must be positive")
        object.__setattr__(self, "producer_generation", int(self.producer_generation))
        object.__setattr__(
            self,
            "producer_spec_sha256",
            _require_sha256(
                self.producer_spec_sha256 or "", "lifecycle.producer_spec_sha256"
            ),
        )
        if self.prior_lifecycle_event_sha256 is None:
            raise CaptureContractError("producer lifecycle fact must chain to its predecessor")
        if close_hashes:
            raise CaptureContractError("only run-close may carry producer close hashes")
        gap_reason = str(self.gap_reason or "").strip().lower() or None
        if kind is CaptureProducerLifecycleKind.GAP:
            if gap_reason is None or _LIFECYCLE_REASON_RE.fullmatch(gap_reason) is None:
                raise CaptureContractError("producer lifecycle gap reason is malformed")
        elif gap_reason is not None:
            raise CaptureContractError("non-gap lifecycle fact carries a gap reason")
        object.__setattr__(self, "gap_reason", gap_reason)
        # REGISTERED binds the exact first-frame control record; QUIESCENT binds
        # final coverage/watermark evidence.  No other lifecycle fact may carry
        # caller-selected evidence hashes.
        if kind not in {
            CaptureProducerLifecycleKind.REGISTERED,
            CaptureProducerLifecycleKind.QUIESCENT,
        } and evidence:
            raise CaptureContractError(
                "only registered or quiescent facts may carry evidence hashes"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "identity_sha256": self.identity_sha256,
            "producer_roster_sha256": self.producer_roster_sha256,
            "recorded_at": _iso_utc(self.recorded_at),
            "frontier_sequence": self.frontier_sequence,
            "prior_lifecycle_event_sha256": self.prior_lifecycle_event_sha256,
            "producer_id": self.producer_id,
            "producer_instance_id": self.producer_instance_id,
            "producer_generation": self.producer_generation,
            "producer_spec_sha256": self.producer_spec_sha256,
            "evidence_event_sha256s": list(self.evidence_event_sha256s),
            "gap_reason": self.gap_reason,
            "producer_close_event_sha256s": dict(
                sorted(self.producer_close_event_sha256s.items())
            ),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CaptureProducerLifecycleFact":
        expected = {
            "schema_version",
            "kind",
            "identity_sha256",
            "producer_roster_sha256",
            "recorded_at",
            "frontier_sequence",
            "prior_lifecycle_event_sha256",
            "producer_id",
            "producer_instance_id",
            "producer_generation",
            "producer_spec_sha256",
            "evidence_event_sha256s",
            "gap_reason",
            "producer_close_event_sha256s",
        }
        evidence = raw.get("evidence_event_sha256s")
        close_hashes = raw.get("producer_close_event_sha256s")
        if (
            set(raw) != expected
            or not isinstance(evidence, list)
            or not isinstance(close_hashes, Mapping)
        ):
            raise CaptureContractError("producer lifecycle fields do not match schema")
        return cls(
            schema_version=str(raw.get("schema_version") or ""),
            kind=CaptureProducerLifecycleKind(str(raw.get("kind") or "")),
            identity_sha256=str(raw.get("identity_sha256") or ""),
            producer_roster_sha256=str(raw.get("producer_roster_sha256") or ""),
            recorded_at=_parse_utc(raw.get("recorded_at"), "lifecycle.recorded_at"),
            frontier_sequence=raw.get("frontier_sequence"),
            prior_lifecycle_event_sha256=raw.get("prior_lifecycle_event_sha256"),
            producer_id=raw.get("producer_id"),
            producer_instance_id=raw.get("producer_instance_id"),
            producer_generation=raw.get("producer_generation"),
            producer_spec_sha256=raw.get("producer_spec_sha256"),
            evidence_event_sha256s=tuple(str(value) for value in evidence),
            gap_reason=raw.get("gap_reason"),
            producer_close_event_sha256s={
                str(key): str(value) for key, value in close_hashes.items()
            },
        )


@dataclass(frozen=True)
class CaptureReadReceipt:
    """Proof of the exact data returned to one FSM read."""

    read_id: str
    decision_id: str
    identity_sha256: str
    stream: CaptureStream
    provider: str
    requested_at: datetime
    returned_at: datetime
    query_sha256: str
    source_event_sha256s: tuple[str, ...]
    empty_result: bool
    result_sha256: str
    symbol: str | None = None
    content_verified: bool = True
    replay_network_fallback_used: bool = False
    query: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "read_id", _uuid_text(self.read_id, "read_id"))
        decision_id = str(self.decision_id or "").strip()
        if not decision_id:
            raise CaptureContractError("decision_id is required")
        object.__setattr__(self, "decision_id", decision_id)
        object.__setattr__(
            self,
            "identity_sha256",
            _require_sha256(self.identity_sha256, "receipt.identity_sha256"),
        )
        try:
            stream = (
                self.stream
                if isinstance(self.stream, CaptureStream)
                else CaptureStream(str(self.stream))
            )
        except ValueError as exc:
            raise CaptureContractError(f"unknown receipt stream: {self.stream}") from exc
        object.__setattr__(self, "stream", stream)
        provider = str(self.provider or "").strip()
        if not provider:
            raise CaptureContractError("receipt provider is required")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(
            self, "symbol", str(self.symbol or "").strip().upper() or None
        )
        for name in (
            "empty_result",
            "content_verified",
            "replay_network_fallback_used",
        ):
            if type(getattr(self, name)) is not bool:
                raise CaptureContractError(f"{name} must be boolean")
        requested = _utc(self.requested_at, "requested_at")
        returned = _utc(self.returned_at, "returned_at")
        if returned < requested:
            raise CaptureContractError("read returned_at cannot precede requested_at")
        object.__setattr__(self, "requested_at", requested)
        object.__setattr__(self, "returned_at", returned)
        object.__setattr__(
            self, "query_sha256", _require_sha256(self.query_sha256, "query_sha256")
        )
        if self.query is not None:
            if not isinstance(self.query, Mapping):
                raise CaptureContractError("receipt query must be a mapping")
            query = _freeze_canonical_json(self.query)
            if sha256_json(query) != self.query_sha256:
                raise CaptureContractError("receipt query content hash mismatch")
            object.__setattr__(self, "query", query)
        object.__setattr__(
            self, "result_sha256", _require_sha256(self.result_sha256, "result_sha256")
        )
        hashes = tuple(
            _require_sha256(value, "source_event_sha256")
            for value in self.source_event_sha256s
        )
        object.__setattr__(self, "source_event_sha256s", hashes)
        if not self.empty_result and not hashes:
            raise CaptureContractError("a non-empty read must reference captured events")
        if self.empty_result and hashes:
            raise CaptureContractError("an empty read cannot reference captured events")

    def to_dict(self) -> dict[str, Any]:
        return {
            "read_id": self.read_id,
            "decision_id": self.decision_id,
            "identity_sha256": self.identity_sha256,
            "stream": self.stream.value,
            "provider": self.provider,
            "symbol": self.symbol,
            "requested_at": _iso_utc(self.requested_at),
            "returned_at": _iso_utc(self.returned_at),
            "query_sha256": self.query_sha256,
            "source_event_sha256s": list(self.source_event_sha256s),
            "empty_result": self.empty_result,
            "result_sha256": self.result_sha256,
            "content_verified": self.content_verified,
            "replay_network_fallback_used": self.replay_network_fallback_used,
            "query": dict(self.query) if self.query is not None else None,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CaptureReadReceipt":
        expected = {
            "read_id",
            "decision_id",
            "identity_sha256",
            "stream",
            "provider",
            "symbol",
            "requested_at",
            "returned_at",
            "query_sha256",
            "source_event_sha256s",
            "empty_result",
            "result_sha256",
            "content_verified",
            "replay_network_fallback_used",
            "query",
        }
        if set(raw) != expected:
            raise CaptureContractError("read receipt fields do not match schema")
        source_hashes = raw.get("source_event_sha256s")
        if not isinstance(source_hashes, list):
            raise CaptureContractError("read receipt source hashes are malformed")
        return cls(
            read_id=str(raw.get("read_id") or ""),
            decision_id=str(raw.get("decision_id") or ""),
            identity_sha256=str(raw.get("identity_sha256") or ""),
            stream=CaptureStream(str(raw.get("stream") or "")),
            provider=str(raw.get("provider") or ""),
            symbol=raw.get("symbol"),
            requested_at=_parse_utc(raw.get("requested_at"), "requested_at"),
            returned_at=_parse_utc(raw.get("returned_at"), "returned_at"),
            query_sha256=str(raw.get("query_sha256") or ""),
            source_event_sha256s=tuple(str(value) for value in source_hashes),
            empty_result=raw.get("empty_result"),
            result_sha256=str(raw.get("result_sha256") or ""),
            content_verified=raw.get("content_verified"),
            replay_network_fallback_used=raw.get(
                "replay_network_fallback_used"
            ),
            query=(
                _mapping(raw.get("query"), "receipt.query")
                if raw.get("query") is not None
                else None
            ),
        )


FSM_DECISION_OUTPUT_SCHEMA_VERSION = "chili.fsm-decision-output.v1"
BROKER_ORDER_LIFECYCLE_SCHEMA_VERSION = "chili.broker-order-lifecycle.v1"


class CaptureDecisionAction(str, Enum):
    ORDER_INTENT = "order_intent"
    ABSTAIN = "abstain"
    REJECT = "reject"


class CaptureOrderIntentRole(str, Enum):
    ENTRY = "entry"
    ADD = "add"
    REDUCE = "reduce"
    EXIT = "exit"
    PROTECTIVE = "protective"
    REPLACEMENT = "replacement"


class CaptureBrokerTransition(str, Enum):
    SUBMITTED = "submitted"
    PENDING_NEW = "pending_new"
    NEW = "new"
    ACCEPTED = "accepted"
    ACCEPTED_FOR_BIDDING = "accepted_for_bidding"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    PENDING_CANCEL = "pending_cancel"
    CANCELED = "canceled"
    PENDING_REPLACE = "pending_replace"
    REPLACED = "replaced"
    EXPIRED = "expired"
    DONE_FOR_DAY = "done_for_day"
    REJECTED = "rejected"
    HELD = "held"
    SUSPENDED = "suspended"
    STOPPED = "stopped"
    CALCULATED = "calculated"
    FAILED = "failed"


@dataclass(frozen=True)
class CaptureOrderIntent:
    intent_id: str
    client_order_id: str
    client_order_id_sha256: str
    symbol: str
    side: str
    order_type: str
    quantity: int
    time_in_force: str
    extended_hours: bool
    intent_role: CaptureOrderIntentRole
    risk_increasing: bool
    decision_provenance_sha256: str
    adaptive_request_sha256: str | None = None
    adaptive_decision_sha256: str | None = None
    adaptive_resolution_sha256: str | None = None
    reservation_claim_sha256: str | None = None
    replaces_order_intent_sha256: str | None = None
    limit_price: float | None = None
    stop_price: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _uuid_text(self.intent_id, "intent_id"))
        client_order_id = str(self.client_order_id or "").strip()
        if not client_order_id:
            raise CaptureContractError("order intent client_order_id is required")
        object.__setattr__(self, "client_order_id", client_order_id)
        object.__setattr__(
            self,
            "client_order_id_sha256",
            _require_sha256(
                self.client_order_id_sha256, "intent.client_order_id_sha256"
            ),
        )
        if self.client_order_id_sha256 != sha256_json(
            {"client_order_id": client_order_id}
        ):
            raise CaptureContractError("order intent client id hash mismatch")
        symbol = str(self.symbol or "").strip().upper()
        if not symbol:
            raise CaptureContractError("order intent symbol is required")
        object.__setattr__(self, "symbol", symbol)
        side = str(self.side or "").strip().lower()
        if side not in {"buy", "sell"}:
            raise CaptureContractError("order intent side is invalid")
        object.__setattr__(self, "side", side)
        order_type = str(self.order_type or "").strip().lower()
        if order_type not in {"market", "limit", "stop", "stop_limit"}:
            raise CaptureContractError("order intent type is invalid")
        object.__setattr__(self, "order_type", order_type)
        if isinstance(self.quantity, bool) or int(self.quantity) <= 0:
            raise CaptureContractError("order intent quantity must be positive")
        object.__setattr__(self, "quantity", int(self.quantity))
        tif = str(self.time_in_force or "").strip().lower()
        if tif not in {"day", "gtc", "ioc", "fok", "opg", "cls"}:
            raise CaptureContractError("order intent time_in_force is invalid")
        object.__setattr__(self, "time_in_force", tif)
        if type(self.extended_hours) is not bool:
            raise CaptureContractError("order intent extended_hours must be boolean")
        try:
            intent_role = (
                self.intent_role
                if isinstance(self.intent_role, CaptureOrderIntentRole)
                else CaptureOrderIntentRole(str(self.intent_role))
            )
        except ValueError as exc:
            raise CaptureContractError("order intent role is invalid") from exc
        object.__setattr__(self, "intent_role", intent_role)
        if type(self.risk_increasing) is not bool:
            raise CaptureContractError("order intent risk_increasing must be boolean")
        object.__setattr__(
            self,
            "decision_provenance_sha256",
            _require_sha256(
                self.decision_provenance_sha256,
                "intent.decision_provenance_sha256",
            ),
        )
        for name in (
            "adaptive_request_sha256",
            "adaptive_decision_sha256",
            "adaptive_resolution_sha256",
            "reservation_claim_sha256",
        ):
            raw = getattr(self, name)
            if raw is not None:
                object.__setattr__(
                    self, name, _require_sha256(raw, f"intent.{name}")
                )
        if self.risk_increasing and any(
            getattr(self, name) is None
            for name in (
                "adaptive_request_sha256",
                "adaptive_decision_sha256",
                "adaptive_resolution_sha256",
                "reservation_claim_sha256",
            )
        ):
            raise CaptureContractError(
                "risk-increasing order intent lacks adaptive risk provenance"
            )
        if intent_role in {
            CaptureOrderIntentRole.ENTRY,
            CaptureOrderIntentRole.ADD,
        } and not self.risk_increasing:
            raise CaptureContractError(
                "entry/add order intent must be marked risk-increasing"
            )
        if intent_role in {
            CaptureOrderIntentRole.REDUCE,
            CaptureOrderIntentRole.EXIT,
            CaptureOrderIntentRole.PROTECTIVE,
        } and self.risk_increasing:
            raise CaptureContractError(
                "reduce/exit/protective order intent cannot increase risk"
            )
        replacement_predecessor = self.replaces_order_intent_sha256
        if replacement_predecessor is not None:
            replacement_predecessor = _require_sha256(
                replacement_predecessor,
                "intent.replaces_order_intent_sha256",
            )
        if (
            intent_role is CaptureOrderIntentRole.REPLACEMENT
        ) != (replacement_predecessor is not None):
            raise CaptureContractError(
                "replacement order intent must bind exactly one predecessor"
            )
        object.__setattr__(
            self,
            "replaces_order_intent_sha256",
            replacement_predecessor,
        )
        for name in ("limit_price", "stop_price"):
            raw = getattr(self, name)
            if raw is None:
                continue
            value = float(raw)
            if not math.isfinite(value) or value <= 0:
                raise CaptureContractError(f"order intent {name} must be positive")
            object.__setattr__(self, name, value)
        if order_type in {"limit", "stop_limit"} and self.limit_price is None:
            raise CaptureContractError("limit order intent requires limit_price")
        if order_type in {"stop", "stop_limit"} and self.stop_price is None:
            raise CaptureContractError("stop order intent requires stop_price")

    @property
    def order_intent_sha256(self) -> str:
        return sha256_json(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "client_order_id": self.client_order_id,
            "client_order_id_sha256": self.client_order_id_sha256,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "quantity": self.quantity,
            "time_in_force": self.time_in_force,
            "extended_hours": self.extended_hours,
            "intent_role": self.intent_role.value,
            "risk_increasing": self.risk_increasing,
            "decision_provenance_sha256": self.decision_provenance_sha256,
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "adaptive_request_sha256": self.adaptive_request_sha256,
            "adaptive_decision_sha256": self.adaptive_decision_sha256,
            "adaptive_resolution_sha256": self.adaptive_resolution_sha256,
            "reservation_claim_sha256": self.reservation_claim_sha256,
            "replaces_order_intent_sha256": self.replaces_order_intent_sha256,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CaptureOrderIntent":
        expected = {
            "intent_id",
            "client_order_id",
            "client_order_id_sha256",
            "symbol",
            "side",
            "order_type",
            "quantity",
            "time_in_force",
            "extended_hours",
            "intent_role",
            "risk_increasing",
            "decision_provenance_sha256",
            "limit_price",
            "stop_price",
            "adaptive_request_sha256",
            "adaptive_decision_sha256",
            "adaptive_resolution_sha256",
            "reservation_claim_sha256",
            "replaces_order_intent_sha256",
        }
        if set(raw) != expected:
            raise CaptureContractError("order intent fields do not match schema")
        return cls(**dict(raw))


@dataclass(frozen=True)
class CaptureDecisionOutput:
    decision_id: str
    symbol: str
    action: CaptureDecisionAction
    fsm_state: str
    setup_role: str
    order_intents: tuple[CaptureOrderIntent, ...]
    reason_code: str | None = None
    schema_version: str = FSM_DECISION_OUTPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FSM_DECISION_OUTPUT_SCHEMA_VERSION:
            raise CaptureContractError("FSM decision output schema is invalid")
        decision_id = str(self.decision_id or "").strip()
        symbol = str(self.symbol or "").strip().upper()
        if not decision_id or not symbol:
            raise CaptureContractError("FSM decision output identity is incomplete")
        object.__setattr__(self, "decision_id", decision_id)
        object.__setattr__(self, "symbol", symbol)
        try:
            action = (
                self.action
                if isinstance(self.action, CaptureDecisionAction)
                else CaptureDecisionAction(str(self.action))
            )
        except ValueError as exc:
            raise CaptureContractError("FSM decision output action is invalid") from exc
        object.__setattr__(self, "action", action)
        for name in ("fsm_state", "setup_role"):
            value = str(getattr(self, name) or "").strip().lower()
            if not value:
                raise CaptureContractError(f"FSM decision output {name} is required")
            object.__setattr__(self, name, value)
        intents = tuple(sorted(self.order_intents, key=lambda row: row.intent_id))
        if any(not isinstance(row, CaptureOrderIntent) for row in intents):
            raise CaptureContractError("FSM decision order intents are malformed")
        if len({row.intent_id for row in intents}) != len(intents) or len(
            {row.client_order_id for row in intents}
        ) != len(intents):
            raise CaptureContractError("FSM decision order intents are duplicated")
        if any(row.symbol != symbol for row in intents):
            raise CaptureContractError("FSM decision/order intent symbol mismatch")
        reason = str(self.reason_code or "").strip().lower() or None
        if action is CaptureDecisionAction.ORDER_INTENT:
            if not intents or reason is not None:
                raise CaptureContractError(
                    "order decision requires intents and no rejection reason"
                )
        elif intents or reason is None:
            raise CaptureContractError(
                "abstain/reject decision requires explicit reason and no intent"
            )
        object.__setattr__(self, "order_intents", intents)
        object.__setattr__(self, "reason_code", reason)

    @property
    def decision_output_sha256(self) -> str:
        return sha256_json(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "symbol": self.symbol,
            "action": self.action.value,
            "fsm_state": self.fsm_state,
            "setup_role": self.setup_role,
            "order_intents": [row.to_dict() for row in self.order_intents],
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CaptureDecisionOutput":
        expected = {
            "schema_version",
            "decision_id",
            "symbol",
            "action",
            "fsm_state",
            "setup_role",
            "order_intents",
            "reason_code",
        }
        intents = raw.get("order_intents")
        if set(raw) != expected or not isinstance(intents, list):
            raise CaptureContractError("FSM decision output fields do not match schema")
        return cls(
            schema_version=str(raw.get("schema_version") or ""),
            decision_id=str(raw.get("decision_id") or ""),
            symbol=str(raw.get("symbol") or ""),
            action=str(raw.get("action") or ""),
            fsm_state=str(raw.get("fsm_state") or ""),
            setup_role=str(raw.get("setup_role") or ""),
            order_intents=tuple(
                CaptureOrderIntent.from_dict(_mapping(value, "order_intent"))
                for value in intents
            ),
            reason_code=raw.get("reason_code"),
        )


FINAL_DECISION_AUTHORITY_SCHEMA_VERSION = "chili.final-decision-authority.v1"


def _final_decision_authority_sha256(
    *,
    identity_sha256: str,
    decision_id: str,
    decision_event_sha256: str,
    decision_output_sha256: str,
    order_intent_sha256s: Iterable[str],
    input_prefix_sequence: int,
    input_prefix_root_sha256: str,
) -> str:
    normalized_decision_id = str(decision_id or "").strip()
    if not normalized_decision_id:
        raise CaptureContractError("final decision authority id is required")
    if isinstance(input_prefix_sequence, bool) or int(input_prefix_sequence) <= 0:
        raise CaptureContractError(
            "final decision authority prefix sequence is invalid"
        )
    intent_hashes = tuple(
        sorted(
            _require_sha256(value, "final authority order intent")
            for value in order_intent_sha256s
        )
    )
    if len(intent_hashes) != len(set(intent_hashes)):
        raise CaptureContractError(
            "final decision authority order intents are duplicated"
        )
    return sha256_json(
        {
            "schema_version": FINAL_DECISION_AUTHORITY_SCHEMA_VERSION,
            "identity_sha256": _require_sha256(
                identity_sha256, "final authority identity"
            ),
            "decision_id": normalized_decision_id,
            "decision_event_sha256": _require_sha256(
                decision_event_sha256, "final authority decision event"
            ),
            "decision_output_sha256": _require_sha256(
                decision_output_sha256, "final authority decision output"
            ),
            "order_intent_sha256s": list(intent_hashes),
            "input_prefix_sequence": int(input_prefix_sequence),
            "input_prefix_root_sha256": _require_sha256(
                input_prefix_root_sha256, "final authority input prefix"
            ),
        }
    )


def capture_final_decision_authority_sha256(
    event: CaptureEvent,
    output: CaptureDecisionOutput,
) -> str:
    """Rebuild the durable authority that every broker transition must bind."""

    if (
        not isinstance(event, CaptureEvent)
        or event.stream is not CaptureStream.FSM_DECISION
        or not isinstance(output, CaptureDecisionOutput)
        or str(event.payload.get("decision_id") or "").strip()
        != output.decision_id
        or str(event.payload.get("decision_output_sha256") or "").strip().lower()
        != output.decision_output_sha256
    ):
        raise CaptureContractError(
            "final decision authority event/output binding is invalid"
        )
    try:
        prefix_sequence = int(event.payload.get("input_prefix_sequence") or 0)
    except (TypeError, ValueError) as exc:
        raise CaptureContractError(
            "final decision authority prefix sequence is malformed"
        ) from exc
    return _final_decision_authority_sha256(
        identity_sha256=event.identity.identity_sha256,
        decision_id=output.decision_id,
        decision_event_sha256=event.event_sha256,
        decision_output_sha256=output.decision_output_sha256,
        order_intent_sha256s=tuple(
            intent.order_intent_sha256 for intent in output.order_intents
        ),
        input_prefix_sequence=prefix_sequence,
        input_prefix_root_sha256=str(
            event.payload.get("input_prefix_root_sha256") or ""
        ),
    )


ADAPTIVE_ORDER_ARTIFACT_SCHEMA_VERSION = "chili.adaptive-order-artifact.v1"


@dataclass(frozen=True)
class CaptureAdaptiveOrderArtifacts:
    """Canonical adaptive request, recomputed resolution, and pure claim."""

    order_intent_sha256: str
    reservation_request: Mapping[str, Any]
    decision_packet: Mapping[str, Any]
    reservation_claim: Mapping[str, Any]
    schema_version: str = ADAPTIVE_ORDER_ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ADAPTIVE_ORDER_ARTIFACT_SCHEMA_VERSION:
            raise CaptureContractError("adaptive order artifact schema is invalid")
        object.__setattr__(
            self,
            "order_intent_sha256",
            _require_sha256(
                self.order_intent_sha256,
                "adaptive_artifact.order_intent_sha256",
            ),
        )
        for name in (
            "reservation_request",
            "decision_packet",
            "reservation_claim",
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise CaptureContractError(f"adaptive artifact {name} is missing")
            object.__setattr__(self, name, _freeze_canonical_json(value))
        self._load_verified()

    def _load_verified(self) -> tuple[Any, Any, Any]:
        try:
            from .adaptive_risk_policy import (
                AdaptiveRiskContractError,
                load_and_verify_adaptive_risk_decision_packet,
            )
            from .adaptive_risk_reservation import (
                load_adaptive_risk_reservation_request,
            )
            from .adaptive_risk_runtime_contract import (
                load_and_verify_adaptive_risk_reservation_claim,
            )

            request = load_adaptive_risk_reservation_request(
                self.reservation_request
            )
            resolved = load_and_verify_adaptive_risk_decision_packet(
                self.decision_packet
            )
            claim = load_and_verify_adaptive_risk_reservation_claim(
                self.decision_packet,
                self.reservation_claim,
            )
        except (CaptureContractError, TypeError, ValueError) as exc:
            raise CaptureContractError(
                f"adaptive order artifacts are invalid: {exc}"
            ) from exc
        except AdaptiveRiskContractError as exc:
            raise CaptureContractError(
                f"adaptive order artifacts failed strict recomputation: {exc}"
            ) from exc
        request_payload = request.to_payload()
        if (
            canonical_json_bytes(request_payload.get("policy"))
            != canonical_json_bytes(resolved.policy_snapshot)
            or canonical_json_bytes(request_payload.get("inputs"))
            != canonical_json_bytes(resolved.input_snapshot)
        ):
            raise CaptureContractError(
                "adaptive request and recomputed decision snapshots differ"
            )
        return request, resolved, claim

    def verify_against(
        self,
        *,
        intent: CaptureOrderIntent,
        decision_id: str,
        identity: CaptureRunIdentity | None = None,
    ) -> None:
        request, resolved, claim = self._load_verified()
        inputs = resolved.input_snapshot
        expected_side = {"buy": {"buy", "long"}, "sell": {"sell", "short"}}
        if (
            self.order_intent_sha256 != intent.order_intent_sha256
            or intent.adaptive_request_sha256 != request.request_sha256
            or intent.adaptive_decision_sha256 != resolved.decision_packet_sha256
            or intent.adaptive_resolution_sha256
            != resolved.economic_resolution_sha256
            or intent.reservation_claim_sha256 != claim.claim_sha256
            or request.client_order_id != intent.client_order_id
            or claim.claim_id != intent.client_order_id
            or str(inputs.get("decision_id") or "") != decision_id
            or str(inputs.get("symbol") or "").strip().upper() != intent.symbol
            or str(inputs.get("side") or "").strip().lower()
            not in expected_side[intent.side]
            or int(resolved.quantity_shares) != intent.quantity
            or int(claim.quantity_shares) != intent.quantity
            or str(claim.symbol or "").strip().upper() != intent.symbol
            or str(claim.side or "").strip().lower()
            not in expected_side[intent.side]
        ):
            raise CaptureContractError(
                "adaptive artifacts do not bind the exact decision/order intent"
            )
        if intent.limit_price is not None and not math.isclose(
            float(request.entry_limit_price),
            intent.limit_price,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise CaptureContractError(
                "adaptive request entry price differs from order intent"
            )
        if identity is not None and (
            str(inputs.get("replay_or_paper_run_id") or "") != identity.run_id
            or int(inputs.get("generation") or 0) != identity.generation
            or str(inputs.get("account_identity_sha256") or "").lower()
            != identity.account_identity_sha256
            or claim.run_id != identity.run_id
            or claim.generation != identity.generation
            or claim.account_identity_sha256 != identity.account_identity_sha256
            or claim.broker_environment != identity.broker_environment
        ):
            raise CaptureContractError(
                "adaptive artifacts escaped capture run/account identity"
            )

    @property
    def reservation_setup_family(self) -> str:
        """Return the strictly reconstructed setup family, never a label claim."""

        request, _resolved, _claim = self._load_verified()
        return str(request.setup_family)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "order_intent_sha256": self.order_intent_sha256,
            "reservation_request": dict(self.reservation_request),
            "decision_packet": dict(self.decision_packet),
            "reservation_claim": dict(self.reservation_claim),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CaptureAdaptiveOrderArtifacts":
        expected = {
            "schema_version",
            "order_intent_sha256",
            "reservation_request",
            "decision_packet",
            "reservation_claim",
        }
        if set(raw) != expected:
            raise CaptureContractError(
                "adaptive order artifact fields do not match schema"
            )
        return cls(**dict(raw))


def capture_adaptive_order_artifacts_from_payload(
    payload: Mapping[str, Any],
    output: CaptureDecisionOutput,
    *,
    identity: CaptureRunIdentity | None = None,
) -> tuple[CaptureAdaptiveOrderArtifacts, ...]:
    risk_intents = {
        intent.order_intent_sha256: intent
        for intent in output.order_intents
        if intent.risk_increasing
    }
    raw_artifacts = payload.get("adaptive_order_artifacts", [])
    if not isinstance(raw_artifacts, list):
        raise CaptureContractError("adaptive order artifact inventory is malformed")
    artifacts = tuple(
        CaptureAdaptiveOrderArtifacts.from_dict(
            _mapping(value, "adaptive_order_artifact")
        )
        for value in raw_artifacts
    )
    by_intent = {row.order_intent_sha256: row for row in artifacts}
    if len(by_intent) != len(artifacts) or set(by_intent) != set(risk_intents):
        raise CaptureContractError(
            "adaptive order artifacts do not cover exact risk-increasing intents"
        )
    for intent_sha256, intent in risk_intents.items():
        by_intent[intent_sha256].verify_against(
            intent=intent,
            decision_id=output.decision_id,
            identity=identity,
        )
    return tuple(sorted(artifacts, key=lambda row: row.order_intent_sha256))


@dataclass(frozen=True)
class CaptureBrokerOrderLifecycle:
    decision_id: str
    order_intent_sha256: str
    client_order_id: str
    client_order_id_sha256: str
    transition: CaptureBrokerTransition
    order_quantity: int
    cumulative_filled_quantity: int
    last_fill_quantity: int
    prior_transition_event_sha256: str | None
    final_decision_attestation_sha256: str
    broker_order_id: str | None = None
    reservation_claim_sha256: str | None = None
    raw_provider_event_sha256: str | None = None
    replaces_broker_order_id: str | None = None
    replaced_by_broker_order_id: str | None = None
    replacement_order_intent_sha256: str | None = None
    last_fill_price: float | None = None
    reject_or_cancel_reason: str | None = None
    schema_version: str = BROKER_ORDER_LIFECYCLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BROKER_ORDER_LIFECYCLE_SCHEMA_VERSION:
            raise CaptureContractError("broker lifecycle schema is invalid")
        decision_id = str(self.decision_id or "").strip()
        client_order_id = str(self.client_order_id or "").strip()
        if not decision_id or not client_order_id:
            raise CaptureContractError("broker lifecycle decision/CID is required")
        object.__setattr__(self, "decision_id", decision_id)
        object.__setattr__(self, "client_order_id", client_order_id)
        for name in ("order_intent_sha256", "client_order_id_sha256"):
            object.__setattr__(
                self, name, _require_sha256(getattr(self, name), f"broker.{name}")
            )
        object.__setattr__(
            self,
            "final_decision_attestation_sha256",
            _require_sha256(
                self.final_decision_attestation_sha256,
                "broker.final_decision_attestation_sha256",
            ),
        )
        if self.reservation_claim_sha256 is not None:
            object.__setattr__(
                self,
                "reservation_claim_sha256",
                _require_sha256(
                    self.reservation_claim_sha256,
                    "broker.reservation_claim_sha256",
                ),
            )
        if self.client_order_id_sha256 != sha256_json(
            {"client_order_id": client_order_id}
        ):
            raise CaptureContractError("broker lifecycle CID hash mismatch")
        try:
            transition = (
                self.transition
                if isinstance(self.transition, CaptureBrokerTransition)
                else CaptureBrokerTransition(str(self.transition))
            )
        except ValueError as exc:
            raise CaptureContractError("broker lifecycle transition is invalid") from exc
        object.__setattr__(self, "transition", transition)
        for name in (
            "order_quantity",
            "cumulative_filled_quantity",
            "last_fill_quantity",
        ):
            raw = getattr(self, name)
            if isinstance(raw, bool) or int(raw) < 0:
                raise CaptureContractError(f"broker lifecycle {name} is invalid")
            object.__setattr__(self, name, int(raw))
        if self.order_quantity <= 0 or self.cumulative_filled_quantity > self.order_quantity:
            raise CaptureContractError("broker lifecycle fill quantity exceeds order")
        if self.last_fill_quantity > self.cumulative_filled_quantity:
            raise CaptureContractError("broker lifecycle last fill exceeds cumulative fill")
        if self.prior_transition_event_sha256 is not None:
            object.__setattr__(
                self,
                "prior_transition_event_sha256",
                _require_sha256(
                    self.prior_transition_event_sha256,
                    "broker.prior_transition_event_sha256",
                ),
            )
        broker_order_id = str(self.broker_order_id or "").strip() or None
        object.__setattr__(self, "broker_order_id", broker_order_id)
        raw_provider_event_sha256 = self.raw_provider_event_sha256
        if transition is CaptureBrokerTransition.SUBMITTED:
            if raw_provider_event_sha256 is not None:
                raise CaptureContractError(
                    "synthetic submitted transition cannot claim a provider event"
                )
        else:
            raw_provider_event_sha256 = _require_sha256(
                raw_provider_event_sha256,
                "broker.raw_provider_event_sha256",
            )
        object.__setattr__(
            self,
            "raw_provider_event_sha256",
            raw_provider_event_sha256,
        )
        for name in (
            "replaces_broker_order_id",
            "replaced_by_broker_order_id",
        ):
            value = str(getattr(self, name) or "").strip() or None
            object.__setattr__(self, name, value)
        replacement_intent = self.replacement_order_intent_sha256
        if replacement_intent is not None:
            replacement_intent = _require_sha256(
                replacement_intent,
                "broker.replacement_order_intent_sha256",
            )
        object.__setattr__(
            self,
            "replacement_order_intent_sha256",
            replacement_intent,
        )
        if transition in {
            CaptureBrokerTransition.PENDING_REPLACE,
            CaptureBrokerTransition.REPLACED,
        } and replacement_intent is None:
            raise CaptureContractError(
                "broker replacement transition lacks successor intent binding"
            )
        if replacement_intent is not None and transition not in {
            CaptureBrokerTransition.PENDING_REPLACE,
            CaptureBrokerTransition.REPLACED,
        }:
            raise CaptureContractError(
                "non-replacement transition carries a successor intent binding"
            )
        if transition is CaptureBrokerTransition.REPLACED and (
            self.replaced_by_broker_order_id is None
        ):
            raise CaptureContractError(
                "replaced transition lacks successor broker order id"
            )
        if self.replaced_by_broker_order_id is not None and transition not in {
            CaptureBrokerTransition.PENDING_REPLACE,
            CaptureBrokerTransition.REPLACED,
        }:
            raise CaptureContractError(
                "non-replacement transition carries replaced_by lineage"
            )
        if self.last_fill_price is not None:
            price = float(self.last_fill_price)
            if not math.isfinite(price) or price <= 0:
                raise CaptureContractError("broker lifecycle last fill price is invalid")
            object.__setattr__(self, "last_fill_price", price)
        reason = str(self.reject_or_cancel_reason or "").strip().lower() or None
        if self.last_fill_quantity > 0 and self.last_fill_price is None:
            raise CaptureContractError("broker fill delta lacks its exact fill price")
        if self.last_fill_quantity == 0 and self.last_fill_price is not None:
            raise CaptureContractError("broker lifecycle has a price without a fill delta")
        if transition is CaptureBrokerTransition.SUBMITTED and (
            self.cumulative_filled_quantity or self.last_fill_quantity
        ):
            raise CaptureContractError("submitted transition cannot already be filled")
        if transition is CaptureBrokerTransition.FILLED and self.last_fill_quantity <= 0:
            raise CaptureContractError("filled transition lacks its final fill delta")
        if transition is CaptureBrokerTransition.PARTIALLY_FILLED and not (
            0 < self.cumulative_filled_quantity < self.order_quantity
        ):
            raise CaptureContractError(
                "partially-filled transition lacks a partial cumulative fill"
            )
        if transition is CaptureBrokerTransition.FILLED and (
            self.cumulative_filled_quantity != self.order_quantity
        ):
            raise CaptureContractError("filled transition is not fully cumulative")
        object.__setattr__(self, "reject_or_cancel_reason", reason)

    @property
    def terminal(self) -> bool:
        return self.transition in {
            CaptureBrokerTransition.FILLED,
            CaptureBrokerTransition.CANCELED,
            CaptureBrokerTransition.REPLACED,
            CaptureBrokerTransition.EXPIRED,
            CaptureBrokerTransition.REJECTED,
            CaptureBrokerTransition.FAILED,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "order_intent_sha256": self.order_intent_sha256,
            "client_order_id": self.client_order_id,
            "client_order_id_sha256": self.client_order_id_sha256,
            "final_decision_attestation_sha256": (
                self.final_decision_attestation_sha256
            ),
            "broker_order_id": self.broker_order_id,
            "reservation_claim_sha256": self.reservation_claim_sha256,
            "raw_provider_event_sha256": self.raw_provider_event_sha256,
            "replaces_broker_order_id": self.replaces_broker_order_id,
            "replaced_by_broker_order_id": self.replaced_by_broker_order_id,
            "replacement_order_intent_sha256": (
                self.replacement_order_intent_sha256
            ),
            "transition": self.transition.value,
            "order_quantity": self.order_quantity,
            "cumulative_filled_quantity": self.cumulative_filled_quantity,
            "last_fill_quantity": self.last_fill_quantity,
            "last_fill_price": self.last_fill_price,
            "reject_or_cancel_reason": self.reject_or_cancel_reason,
            "prior_transition_event_sha256": self.prior_transition_event_sha256,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CaptureBrokerOrderLifecycle":
        expected = {
            "schema_version",
            "decision_id",
            "order_intent_sha256",
            "client_order_id",
            "client_order_id_sha256",
            "final_decision_attestation_sha256",
            "broker_order_id",
            "reservation_claim_sha256",
            "raw_provider_event_sha256",
            "replaces_broker_order_id",
            "replaced_by_broker_order_id",
            "replacement_order_intent_sha256",
            "transition",
            "order_quantity",
            "cumulative_filled_quantity",
            "last_fill_quantity",
            "last_fill_price",
            "reject_or_cancel_reason",
            "prior_transition_event_sha256",
        }
        if set(raw) != expected:
            raise CaptureContractError("broker lifecycle fields do not match schema")
        return cls(**dict(raw))


def capture_decision_output_from_payload(
    payload: Mapping[str, Any],
) -> CaptureDecisionOutput:
    raw_output = payload.get("decision_output")
    if not isinstance(raw_output, Mapping):
        raise CaptureContractError("FSM decision output is missing")
    output = CaptureDecisionOutput.from_dict(raw_output)
    expected = _require_sha256(
        str(payload.get("decision_output_sha256") or ""),
        "decision_output_sha256",
    )
    if output.decision_output_sha256 != expected:
        raise CaptureContractError("FSM decision output hash mismatch")
    if (
        str(payload.get("decision_id") or "").strip() != output.decision_id
        or str(payload.get("symbol") or "").strip().upper() != output.symbol
    ):
        raise CaptureContractError("FSM decision output envelope mismatch")
    adaptive_artifacts = capture_adaptive_order_artifacts_from_payload(
        payload,
        output,
    )
    if adaptive_artifacts:
        artifact_setup_families = {
            artifact.reservation_setup_family
            for artifact in adaptive_artifacts
        }
        output_is_first_dip = output.setup_role == "first_dip_reclaim"
        if (
            output_is_first_dip
            and artifact_setup_families != {"first_dip_reclaim"}
        ) or (
            not output_is_first_dip
            and "first_dip_reclaim" in artifact_setup_families
        ):
            raise CaptureContractError(
                "adaptive first-dip setup family/output role mismatch"
            )
    return output


ACTIVE_CAPTURE_PREFIX_ATTESTATION_SCHEMA_VERSION = (
    "chili.active-capture-prefix-attestation.v1"
)
_ACTIVE_CAPTURE_PREFIX_ATTESTATION_TOKEN = object()
_ACTIVE_CAPTURE_PREFIX_ATTESTATION_KEY = secrets.token_bytes(32)


@dataclass(frozen=True)
class ActiveCaptureReadEvidence:
    """Runtime-derived durable receipt and its exact source-event clocks."""

    receipt: CaptureReadReceipt
    receipt_sha256: str
    receipt_event_sha256: str
    receipt_event_sequence: int
    receipt_committed_available_at: datetime
    producer_id: str
    producer_generation: int
    source_event_refs: tuple[CaptureEventRef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, CaptureReadReceipt):
            raise CaptureContractError("active receipt evidence is malformed")
        expected_receipt = sha256_json(self.receipt.to_dict())
        object.__setattr__(
            self,
            "receipt_sha256",
            _require_sha256(self.receipt_sha256, "active_receipt.receipt_sha256"),
        )
        if self.receipt_sha256 != expected_receipt:
            raise CaptureContractError("active receipt payload hash mismatch")
        object.__setattr__(
            self,
            "receipt_event_sha256",
            _require_sha256(
                self.receipt_event_sha256,
                "active_receipt.receipt_event_sha256",
            ),
        )
        if isinstance(self.receipt_event_sequence, bool) or int(
            self.receipt_event_sequence
        ) <= 0:
            raise CaptureContractError("active receipt event sequence must be positive")
        object.__setattr__(
            self, "receipt_event_sequence", int(self.receipt_event_sequence)
        )
        committed = _utc(
            self.receipt_committed_available_at,
            "active_receipt.receipt_committed_available_at",
        )
        if committed < self.receipt.returned_at:
            raise CaptureContractError("active receipt was committed before it returned")
        object.__setattr__(self, "receipt_committed_available_at", committed)
        producer_id = str(self.producer_id or "").strip().lower()
        if _PRODUCER_ID_RE.fullmatch(producer_id) is None:
            raise CaptureContractError("active receipt producer id is malformed")
        object.__setattr__(self, "producer_id", producer_id)
        if isinstance(self.producer_generation, bool) or int(
            self.producer_generation
        ) <= 0:
            raise CaptureContractError("active receipt producer generation is invalid")
        object.__setattr__(self, "producer_generation", int(self.producer_generation))
        refs = tuple(self.source_event_refs)
        if any(not isinstance(ref, CaptureEventRef) for ref in refs):
            raise CaptureContractError("active receipt source refs are malformed")
        if tuple(ref.event_sha256 for ref in refs) != self.receipt.source_event_sha256s:
            raise CaptureContractError("active receipt source inventory mismatch")
        for ref in refs:
            if (
                ref.identity_sha256 != self.receipt.identity_sha256
                or ref.stream is not self.receipt.stream
                or ref.provider != self.receipt.provider
                or ref.symbol != self.receipt.symbol
                or ref.available_at > self.receipt.returned_at
                or (
                    STREAM_POLICIES[self.receipt.stream].query_parameters_required
                    and ref.query_sha256 != self.receipt.query_sha256
                )
            ):
                raise CaptureContractError("active receipt source ref mismatch")
        if captured_read_result_sha256(refs) != self.receipt.result_sha256:
            raise CaptureContractError("active receipt result hash mismatch")
        object.__setattr__(self, "source_event_refs", refs)

    def to_evidence_dict(self) -> dict[str, Any]:
        return {
            "receipt": self.receipt.to_dict(),
            "receipt_sha256": self.receipt_sha256,
            "receipt_event_sha256": self.receipt_event_sha256,
            "receipt_event_sequence": self.receipt_event_sequence,
            "receipt_committed_available_at": _iso_utc(
                self.receipt_committed_available_at
            ),
            "producer_id": self.producer_id,
            "producer_generation": self.producer_generation,
            "source_event_refs": [ref.to_dict() for ref in self.source_event_refs],
        }


@dataclass(frozen=True)
class ActiveCaptureContinuityEvidence:
    """Durable live continuity checkpoint used by Stage-1 authorization."""

    coverage: "StreamCoverage"
    producer_id: str
    producer_generation: int
    source_frontier_sequence: int
    watermark_event_sha256: str
    watermark_event_sequence: int
    watermark_committed_available_at: datetime
    coverage_event_sha256: str
    coverage_event_sequence: int
    coverage_committed_available_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.coverage, StreamCoverage):
            raise CaptureContractError("active continuity coverage is malformed")
        policy = STREAM_POLICIES[self.coverage.stream]
        if policy.coverage_mode not in {
            CoverageMode.CONTINUOUS,
            CoverageMode.CHANGE_LOG,
        }:
            raise CaptureContractError(
                "active continuity evidence requires a continuity-backed stream"
            )
        if (
            self.coverage.watermark is None
            or self.coverage.event_count <= 0
            or not self.coverage.content_verified
            or not self.coverage.continuity_complete
            or (
                policy.exact_provider_event_clock_required
                and not self.coverage.exact_event_clock_complete
            )
        ):
            raise CaptureContractError(
                "active continuity evidence is incomplete or unverified"
            )
        producer_id = str(self.producer_id or "").strip().lower()
        if _PRODUCER_ID_RE.fullmatch(producer_id) is None:
            raise CaptureContractError("active continuity producer id is malformed")
        object.__setattr__(self, "producer_id", producer_id)
        for name in (
            "producer_generation",
            "source_frontier_sequence",
            "watermark_event_sequence",
            "coverage_event_sequence",
        ):
            raw = getattr(self, name)
            if isinstance(raw, bool) or int(raw) <= 0:
                raise CaptureContractError(
                    f"active continuity {name} must be positive"
                )
            object.__setattr__(self, name, int(raw))
        if self.coverage.watermark.generation != self.producer_generation:
            raise CaptureContractError(
                "active continuity watermark/producer generation mismatch"
            )
        if not (
            self.source_frontier_sequence < self.watermark_event_sequence
            < self.coverage_event_sequence
        ):
            raise CaptureContractError(
                "active continuity durable sequence frontier is malformed"
            )
        for name in ("watermark_event_sha256", "coverage_event_sha256"):
            object.__setattr__(
                self,
                name,
                _require_sha256(getattr(self, name), f"active_continuity.{name}"),
            )
        watermark_committed = _utc(
            self.watermark_committed_available_at,
            "active_continuity.watermark_committed_available_at",
        )
        coverage_committed = _utc(
            self.coverage_committed_available_at,
            "active_continuity.coverage_committed_available_at",
        )
        if (
            watermark_committed < self.coverage.watermark.emitted_available_at
            or coverage_committed < watermark_committed
            or coverage_committed < self.coverage.last_available_at
        ):
            raise CaptureContractError(
                "active continuity committed clocks are inconsistent"
            )
        object.__setattr__(
            self, "watermark_committed_available_at", watermark_committed
        )
        object.__setattr__(
            self, "coverage_committed_available_at", coverage_committed
        )

    def to_evidence_dict(self) -> dict[str, Any]:
        return {
            "coverage": self.coverage.to_dict(),
            "producer_id": self.producer_id,
            "producer_generation": self.producer_generation,
            "source_frontier_sequence": self.source_frontier_sequence,
            "watermark_event_sha256": self.watermark_event_sha256,
            "watermark_event_sequence": self.watermark_event_sequence,
            "watermark_committed_available_at": _iso_utc(
                self.watermark_committed_available_at
            ),
            "coverage_event_sha256": self.coverage_event_sha256,
            "coverage_event_sequence": self.coverage_event_sequence,
            "coverage_committed_available_at": _iso_utc(
                self.coverage_committed_available_at
            ),
        }


@dataclass(frozen=True)
class FirstDipTapeReceiptEvidence:
    """Exact IQFeed print receipt used by the external first-dip age gate.

    Source and return clocks are preserved; the live order boundary separately
    enforces maximum age and re-resolves when the latest economic fingerprint
    changes.
    """

    read_evidence: ActiveCaptureReadEvidence

    def __post_init__(self) -> None:
        evidence = self.read_evidence
        if not isinstance(evidence, ActiveCaptureReadEvidence):
            raise CaptureContractError("first-dip tape receipt evidence is malformed")
        if evidence.receipt.stream is not CaptureStream.IQFEED_PRINT:
            raise CaptureContractError("first-dip tape receipt must read IQFeed prints")
        if evidence.receipt.provider != "iqfeed":
            raise CaptureContractError("first-dip tape receipt provider is not IQFeed")
        if evidence.receipt.empty_result != (not evidence.source_event_refs):
            raise CaptureContractError(
                "first-dip tape receipt empty-result binding is inconsistent"
            )
        if any(
            ref.provider_event_at is None
            or ref.received_at > ref.available_at
            for ref in evidence.source_event_refs
        ):
            raise CaptureContractError(
                "first-dip tape receipt lacks exact provider/received/available clocks"
            )
        refs = evidence.source_event_refs
        if len(refs) != len({ref.event_sha256 for ref in refs}):
            raise CaptureContractError("first-dip tape receipt repeats a source event")
        order = tuple((ref.provider_event_at, ref.sequence) for ref in refs)
        if order != tuple(sorted(order)):
            raise CaptureContractError(
                "first-dip tape receipt source order is not causal"
            )

    @property
    def read_id(self) -> str:
        return self.read_evidence.receipt.read_id


ACTIVE_CAPTURE_INPUT_ATTESTATION_SCHEMA_VERSION = (
    "chili.active-capture-input-attestation.v2"
)
_ACTIVE_CAPTURE_INPUT_ATTESTATION_TOKEN = object()
_ACTIVE_CAPTURE_INPUT_ATTESTATION_KEY = secrets.token_bytes(32)


@dataclass(frozen=True)
class ActiveCaptureInputPrefixAttestation:
    """One-time private proof of the exact reads used for provisional compute.

    This stage authorizes no reservation or broker mutation.  The producer
    lifecycle consumes the exact opaque object when it commits the canonical
    decision output; the final attestation then binds this proof to that output.
    """

    schema_version: str
    run_id: str
    generation: int
    decision_id: str
    input_prefix_sequence: int
    input_prefix_root_sha256: str
    attested_available_at: datetime
    expires_at: datetime
    dependency_profile: FSMDependencyProfile
    identity_sha256: str
    account_identity_sha256: str
    code_build_sha256: str
    config_sha256: str
    feature_flags_sha256: str
    resource_binding_sha256: str
    producer_generations: Mapping[str, int]
    required_read_ids: tuple[str, ...]
    read_evidence: tuple[ActiveCaptureReadEvidence, ...]
    continuity_evidence: tuple[ActiveCaptureContinuityEvidence, ...]
    admission_handoff_sha256: str | None = None
    first_dip_tape_read_id: str | None = None
    first_dip_prior_detector_reference_sha256: str | None = None
    first_dip_adaptive_request_sha256: str | None = None
    first_dip_opportunity_key_sha256: str | None = None
    _verification_token: object = field(default=None, repr=False, compare=False)
    _attestation_sha256: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_token is not _ACTIVE_CAPTURE_INPUT_ATTESTATION_TOKEN:
            raise CaptureContractError(
                "active capture input attestation was not issued by the runtime"
            )
        if self.schema_version != ACTIVE_CAPTURE_INPUT_ATTESTATION_SCHEMA_VERSION:
            raise CaptureContractError("active capture input attestation schema is invalid")
        object.__setattr__(self, "run_id", _uuid_text(self.run_id, "input_attestation.run_id"))
        if isinstance(self.generation, bool) or int(self.generation) <= 0:
            raise CaptureContractError("input attestation generation must be positive")
        object.__setattr__(self, "generation", int(self.generation))
        decision_id = str(self.decision_id or "").strip()
        if not decision_id:
            raise CaptureContractError("input attestation decision id is required")
        object.__setattr__(self, "decision_id", decision_id)
        if isinstance(self.input_prefix_sequence, bool) or int(
            self.input_prefix_sequence
        ) <= 0:
            raise CaptureContractError("input attestation prefix sequence must be positive")
        object.__setattr__(self, "input_prefix_sequence", int(self.input_prefix_sequence))
        for name in (
            "input_prefix_root_sha256",
            "identity_sha256",
            "account_identity_sha256",
            "code_build_sha256",
            "config_sha256",
            "feature_flags_sha256",
            "resource_binding_sha256",
        ):
            object.__setattr__(
                self,
                name,
                _require_sha256(getattr(self, name), f"input_attestation.{name}"),
            )
        if not isinstance(self.dependency_profile, FSMDependencyProfile):
            raise CaptureContractError(
                "input attestation dependency profile is malformed"
            )
        attested = _utc(
            self.attested_available_at,
            "input_attestation.attested_available_at",
        )
        expires = _utc(self.expires_at, "input_attestation.expires_at")
        if expires <= attested:
            raise CaptureContractError("input attestation expiry must follow issuance")
        object.__setattr__(self, "attested_available_at", attested)
        object.__setattr__(self, "expires_at", expires)
        generations: dict[str, int] = {}
        for raw_id, raw_generation in self.producer_generations.items():
            producer_id = str(raw_id or "").strip().lower()
            if _PRODUCER_ID_RE.fullmatch(producer_id) is None:
                raise CaptureContractError("input attestation producer id is malformed")
            if isinstance(raw_generation, bool) or int(raw_generation) <= 0:
                raise CaptureContractError("input attestation producer generation is invalid")
            generations[producer_id] = int(raw_generation)
        if not generations or len(generations) != len(self.producer_generations):
            raise CaptureContractError("input attestation producer roster is invalid")
        object.__setattr__(
            self,
            "producer_generations",
            _FrozenJsonDict(dict(sorted(generations.items()))),
        )
        read_ids = tuple(
            sorted(
                _uuid_text(value, "input_attestation.read_id")
                for value in self.required_read_ids
            )
        )
        if not read_ids or len(read_ids) != len(set(read_ids)):
            raise CaptureContractError("input attestation read ids are empty or duplicated")
        evidence = tuple(sorted(self.read_evidence, key=lambda row: row.receipt.read_id))
        if any(not isinstance(row, ActiveCaptureReadEvidence) for row in evidence):
            raise CaptureContractError("input attestation read evidence is malformed")
        if tuple(row.receipt.read_id for row in evidence) != read_ids:
            raise CaptureContractError("input attestation receipt set is incomplete")
        if (
            self.dependency_profile.required_read_ids != read_ids
            or frozenset(row.receipt.stream for row in evidence)
            != self.dependency_profile.required_streams
        ):
            raise CaptureContractError(
                "input attestation evidence does not match dependency profile"
            )
        if any(
            row.receipt.decision_id != decision_id
            or row.receipt.identity_sha256 != self.identity_sha256
            or generations.get(row.producer_id) != row.producer_generation
            or row.receipt_event_sequence > self.input_prefix_sequence
            for row in evidence
        ):
            raise CaptureContractError("input attestation receipt binding is inconsistent")
        object.__setattr__(self, "required_read_ids", read_ids)
        object.__setattr__(self, "read_evidence", evidence)
        continuity = tuple(
            sorted(
                self.continuity_evidence,
                key=lambda row: row.coverage.stream.value,
            )
        )
        if any(
            not isinstance(row, ActiveCaptureContinuityEvidence)
            for row in continuity
        ):
            raise CaptureContractError(
                "input attestation continuity evidence is malformed"
            )
        continuity_backed_streams = frozenset(
            stream
            for stream in self.dependency_profile.required_streams
            if STREAM_POLICIES[stream].coverage_mode
            in {CoverageMode.CONTINUOUS, CoverageMode.CHANGE_LOG}
        )
        if (
            len({row.coverage.stream for row in continuity}) != len(continuity)
            or frozenset(row.coverage.stream for row in continuity)
            != continuity_backed_streams
            or any(
                row.coverage.identity_sha256 != self.identity_sha256
                or generations.get(row.producer_id) != row.producer_generation
                or row.coverage_event_sequence > self.input_prefix_sequence
                for row in continuity
            )
        ):
            raise CaptureContractError(
                "input attestation continuity inventory is incomplete"
            )
        object.__setattr__(self, "continuity_evidence", continuity)
        admission_handoff = self.admission_handoff_sha256
        if admission_handoff is not None:
            admission_handoff = _require_sha256(
                admission_handoff,
                "input_attestation.admission_handoff_sha256",
            )
        object.__setattr__(self, "admission_handoff_sha256", admission_handoff)
        first_dip = str(self.first_dip_tape_read_id or "").strip() or None
        if first_dip is not None:
            first_dip = _uuid_text(first_dip, "input_attestation.first_dip_tape_read_id")
            matching = [row for row in evidence if row.receipt.read_id == first_dip]
            if len(matching) != 1:
                raise CaptureContractError("input attestation first-dip receipt is missing")
            FirstDipTapeReceiptEvidence(matching[0])
        object.__setattr__(self, "first_dip_tape_read_id", first_dip)
        final_first_dip_fields = (
            "first_dip_prior_detector_reference_sha256",
            "first_dip_adaptive_request_sha256",
            "first_dip_opportunity_key_sha256",
        )
        present_final_fields = tuple(
            name for name in final_first_dip_fields if getattr(self, name) is not None
        )
        if present_final_fields and len(present_final_fields) != len(
            final_first_dip_fields
        ):
            raise CaptureContractError(
                "input attestation first-dip final lineage is incomplete"
            )
        if present_final_fields and first_dip is None:
            raise CaptureContractError(
                "input attestation first-dip final lineage lacks a tape read"
            )
        for name in final_first_dip_fields:
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    _require_sha256(value, f"input_attestation.{name}"),
                )
        if self._attestation_sha256:
            expected = _active_capture_input_attestation_sha256(self)
            if not hmac.compare_digest(self._attestation_sha256, expected):
                raise CaptureContractError("active capture input attestation changed")

    @property
    def attestation_sha256(self) -> str:
        return self._attestation_sha256

    @cached_property
    def read_evidence_inventory_sha256(self) -> str:
        return sha256_json(
            {"read_evidence": [row.to_evidence_dict() for row in self.read_evidence]}
        )

    @cached_property
    def continuity_evidence_inventory_sha256(self) -> str:
        return sha256_json(
            {
                "continuity_evidence": [
                    row.to_evidence_dict() for row in self.continuity_evidence
                ]
            }
        )

    def _attestation_body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "generation": self.generation,
            "decision_id": self.decision_id,
            "input_prefix_sequence": self.input_prefix_sequence,
            "input_prefix_root_sha256": self.input_prefix_root_sha256,
            "attested_available_at": _iso_utc(self.attested_available_at),
            "expires_at": _iso_utc(self.expires_at),
            "dependency_profile": self.dependency_profile.to_dict(),
            "dependency_profile_sha256": self.dependency_profile.profile_sha256,
            "identity_sha256": self.identity_sha256,
            "account_identity_sha256": self.account_identity_sha256,
            "code_build_sha256": self.code_build_sha256,
            "config_sha256": self.config_sha256,
            "feature_flags_sha256": self.feature_flags_sha256,
            "resource_binding_sha256": self.resource_binding_sha256,
            "producer_generations": dict(self.producer_generations),
            "required_read_ids": list(self.required_read_ids),
            "read_evidence": [row.to_evidence_dict() for row in self.read_evidence],
            "continuity_evidence": [
                row.to_evidence_dict() for row in self.continuity_evidence
            ],
            "continuity_evidence_inventory_sha256": (
                self.continuity_evidence_inventory_sha256
            ),
            "admission_handoff_sha256": self.admission_handoff_sha256,
            "first_dip_tape_read_id": self.first_dip_tape_read_id,
            "first_dip_prior_detector_reference_sha256": (
                self.first_dip_prior_detector_reference_sha256
            ),
            "first_dip_adaptive_request_sha256": (
                self.first_dip_adaptive_request_sha256
            ),
            "first_dip_opportunity_key_sha256": (
                self.first_dip_opportunity_key_sha256
            ),
        }


def _active_capture_input_attestation_sha256(
    value: ActiveCaptureInputPrefixAttestation,
) -> str:
    return hmac.new(
        _ACTIVE_CAPTURE_INPUT_ATTESTATION_KEY,
        canonical_json_bytes(value._attestation_body()),
        hashlib.sha256,
    ).hexdigest()


def _issue_active_capture_input_attestation(
    *,
    run_id: str,
    generation: int,
    decision_id: str,
    input_prefix_sequence: int,
    input_prefix_root_sha256: str,
    attested_available_at: datetime,
    expires_at: datetime,
    dependency_profile: FSMDependencyProfile,
    identity_sha256: str,
    account_identity_sha256: str,
    code_build_sha256: str,
    config_sha256: str,
    feature_flags_sha256: str,
    resource_binding_sha256: str,
    producer_generations: Mapping[str, int],
    required_read_ids: tuple[str, ...],
    read_evidence: tuple[ActiveCaptureReadEvidence, ...],
    continuity_evidence: tuple[ActiveCaptureContinuityEvidence, ...],
    admission_handoff_sha256: str | None = None,
    first_dip_tape_read_id: str | None = None,
    first_dip_prior_detector_reference_sha256: str | None = None,
    first_dip_adaptive_request_sha256: str | None = None,
    first_dip_opportunity_key_sha256: str | None = None,
) -> ActiveCaptureInputPrefixAttestation:
    value = ActiveCaptureInputPrefixAttestation(
        schema_version=ACTIVE_CAPTURE_INPUT_ATTESTATION_SCHEMA_VERSION,
        run_id=run_id,
        generation=generation,
        decision_id=decision_id,
        input_prefix_sequence=input_prefix_sequence,
        input_prefix_root_sha256=input_prefix_root_sha256,
        attested_available_at=attested_available_at,
        expires_at=expires_at,
        dependency_profile=dependency_profile,
        identity_sha256=identity_sha256,
        account_identity_sha256=account_identity_sha256,
        code_build_sha256=code_build_sha256,
        config_sha256=config_sha256,
        feature_flags_sha256=feature_flags_sha256,
        resource_binding_sha256=resource_binding_sha256,
        producer_generations=producer_generations,
        required_read_ids=required_read_ids,
        read_evidence=read_evidence,
        continuity_evidence=continuity_evidence,
        admission_handoff_sha256=admission_handoff_sha256,
        first_dip_tape_read_id=first_dip_tape_read_id,
        first_dip_prior_detector_reference_sha256=(
            first_dip_prior_detector_reference_sha256
        ),
        first_dip_adaptive_request_sha256=first_dip_adaptive_request_sha256,
        first_dip_opportunity_key_sha256=first_dip_opportunity_key_sha256,
        _verification_token=_ACTIVE_CAPTURE_INPUT_ATTESTATION_TOKEN,
    )
    object.__setattr__(
        value,
        "_attestation_sha256",
        _active_capture_input_attestation_sha256(value),
    )
    return value


def verify_active_capture_input_attestation(
    value: ActiveCaptureInputPrefixAttestation,
) -> ActiveCaptureInputPrefixAttestation:
    if not isinstance(value, ActiveCaptureInputPrefixAttestation):
        raise CaptureContractError("active capture input attestation is malformed")
    if value._verification_token is not _ACTIVE_CAPTURE_INPUT_ATTESTATION_TOKEN:
        raise CaptureContractError("active capture input attestation token is invalid")
    expected = _active_capture_input_attestation_sha256(value)
    if not value._attestation_sha256 or not hmac.compare_digest(
        value._attestation_sha256,
        expected,
    ):
        raise CaptureContractError("active capture input attestation is invalid")
    return value


@dataclass(frozen=True)
class ActiveCapturePrefixAttestation:
    """Private-token proof of the exact live prefix at one decision boundary.

    There is deliberately no ``from_dict`` or deserialization constructor.
    Ordinary digests and caller-built dataclasses cannot mint the private token.
    """

    schema_version: str
    run_id: str
    generation: int
    decision_id: str
    decision_event_sha256: str
    predecision_attestation_sha256: str
    predecision_read_evidence_inventory_sha256: str
    predecision_continuity_evidence_inventory_sha256: str
    decision_output_sha256: str
    order_intent_sha256s: tuple[str, ...]
    input_prefix_sequence: int
    input_prefix_root_sha256: str
    attestation_frontier_sequence: int
    attestation_frontier_root_sha256: str
    decision_available_at: datetime
    attested_available_at: datetime
    expires_at: datetime
    identity_sha256: str
    account_identity_sha256: str
    code_build_sha256: str
    config_sha256: str
    feature_flags_sha256: str
    resource_binding_sha256: str
    producer_generations: Mapping[str, int]
    required_read_ids: tuple[str, ...]
    read_evidence: tuple[ActiveCaptureReadEvidence, ...]
    continuity_evidence: tuple[ActiveCaptureContinuityEvidence, ...]
    first_dip_tape_read_id: str | None = None
    _verification_token: object = field(default=None, repr=False, compare=False)
    _attestation_sha256: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_token is not _ACTIVE_CAPTURE_PREFIX_ATTESTATION_TOKEN:
            raise CaptureContractError(
                "active capture prefix attestation was not issued by the runtime"
            )
        if self.schema_version != ACTIVE_CAPTURE_PREFIX_ATTESTATION_SCHEMA_VERSION:
            raise CaptureContractError("active capture prefix attestation schema is invalid")
        object.__setattr__(self, "run_id", _uuid_text(self.run_id, "attestation.run_id"))
        if isinstance(self.generation, bool) or int(self.generation) <= 0:
            raise CaptureContractError("attestation generation must be positive")
        object.__setattr__(self, "generation", int(self.generation))
        decision_id = str(self.decision_id or "").strip()
        if not decision_id:
            raise CaptureContractError("attestation decision id is required")
        object.__setattr__(self, "decision_id", decision_id)
        for name in (
            "decision_event_sha256",
            "predecision_attestation_sha256",
            "predecision_read_evidence_inventory_sha256",
            "predecision_continuity_evidence_inventory_sha256",
            "decision_output_sha256",
            "input_prefix_root_sha256",
            "attestation_frontier_root_sha256",
            "identity_sha256",
            "account_identity_sha256",
            "code_build_sha256",
            "config_sha256",
            "feature_flags_sha256",
            "resource_binding_sha256",
        ):
            object.__setattr__(
                self, name, _require_sha256(getattr(self, name), f"attestation.{name}")
            )
        intent_hashes = tuple(
            sorted(
                _require_sha256(value, "attestation.order_intent_sha256")
                for value in self.order_intent_sha256s
            )
        )
        if len(intent_hashes) != len(set(intent_hashes)):
            raise CaptureContractError("attestation order intent hashes are duplicated")
        object.__setattr__(self, "order_intent_sha256s", intent_hashes)
        if isinstance(self.input_prefix_sequence, bool) or int(
            self.input_prefix_sequence
        ) <= 0:
            raise CaptureContractError("attestation prefix sequence must be positive")
        object.__setattr__(
            self, "input_prefix_sequence", int(self.input_prefix_sequence)
        )
        if isinstance(self.attestation_frontier_sequence, bool) or int(
            self.attestation_frontier_sequence
        ) < self.input_prefix_sequence + 1:
            raise CaptureContractError(
                "attestation frontier must include the decision boundary"
            )
        object.__setattr__(
            self,
            "attestation_frontier_sequence",
            int(self.attestation_frontier_sequence),
        )
        decision_available = _utc(
            self.decision_available_at, "attestation.decision_available_at"
        )
        attested_available = _utc(
            self.attested_available_at, "attestation.attested_available_at"
        )
        expires = _utc(self.expires_at, "attestation.expires_at")
        if attested_available < decision_available:
            raise CaptureContractError("attestation predates its decision event")
        if expires <= attested_available:
            raise CaptureContractError(
                "attestation expiry must follow final proof issuance"
            )
        object.__setattr__(self, "decision_available_at", decision_available)
        object.__setattr__(self, "attested_available_at", attested_available)
        object.__setattr__(self, "expires_at", expires)
        generations: dict[str, int] = {}
        for raw_id, raw_generation in self.producer_generations.items():
            producer_id = str(raw_id or "").strip().lower()
            if _PRODUCER_ID_RE.fullmatch(producer_id) is None:
                raise CaptureContractError("attestation producer id is malformed")
            if isinstance(raw_generation, bool) or int(raw_generation) <= 0:
                raise CaptureContractError("attestation producer generation is invalid")
            generations[producer_id] = int(raw_generation)
        if not generations or len(generations) != len(self.producer_generations):
            raise CaptureContractError("attestation producer roster is invalid")
        object.__setattr__(
            self, "producer_generations", _FrozenJsonDict(dict(sorted(generations.items())))
        )
        read_ids = tuple(sorted(_uuid_text(value, "attestation.read_id") for value in self.required_read_ids))
        if len(read_ids) != len(set(read_ids)):
            raise CaptureContractError("attestation read ids are duplicated")
        evidence = tuple(sorted(self.read_evidence, key=lambda row: row.receipt.read_id))
        if any(not isinstance(row, ActiveCaptureReadEvidence) for row in evidence):
            raise CaptureContractError("attestation read evidence is malformed")
        if tuple(row.receipt.read_id for row in evidence) != read_ids:
            raise CaptureContractError("attestation receipt set is incomplete")
        if any(
            row.receipt.decision_id != decision_id
            or row.receipt.identity_sha256 != self.identity_sha256
            or generations.get(row.producer_id) != row.producer_generation
            or row.receipt_event_sequence > self.input_prefix_sequence
            for row in evidence
        ):
            raise CaptureContractError("attestation receipt binding is inconsistent")
        object.__setattr__(self, "required_read_ids", read_ids)
        object.__setattr__(self, "read_evidence", evidence)
        if sha256_json(
            {"read_evidence": [row.to_evidence_dict() for row in evidence]}
        ) != self.predecision_read_evidence_inventory_sha256:
            raise CaptureContractError(
                "attestation read evidence differs from predecision inventory"
            )
        continuity = tuple(
            sorted(
                self.continuity_evidence,
                key=lambda row: row.coverage.stream.value,
            )
        )
        if any(
            not isinstance(row, ActiveCaptureContinuityEvidence)
            for row in continuity
        ) or len({row.coverage.stream for row in continuity}) != len(continuity):
            raise CaptureContractError(
                "attestation continuity evidence is malformed"
            )
        if any(
            row.coverage.identity_sha256 != self.identity_sha256
            or generations.get(row.producer_id) != row.producer_generation
            or row.coverage_event_sequence > self.input_prefix_sequence
            for row in continuity
        ):
            raise CaptureContractError(
                "attestation continuity evidence binding is inconsistent"
            )
        if sha256_json(
            {
                "continuity_evidence": [
                    row.to_evidence_dict() for row in continuity
                ]
            }
        ) != self.predecision_continuity_evidence_inventory_sha256:
            raise CaptureContractError(
                "attestation continuity evidence differs from predecision inventory"
            )
        object.__setattr__(self, "continuity_evidence", continuity)
        first_dip = str(self.first_dip_tape_read_id or "").strip() or None
        if first_dip is not None:
            first_dip = _uuid_text(first_dip, "attestation.first_dip_tape_read_id")
            matching = [row for row in evidence if row.receipt.read_id == first_dip]
            if len(matching) != 1:
                raise CaptureContractError("attestation first-dip tape receipt is missing")
            FirstDipTapeReceiptEvidence(matching[0])
        object.__setattr__(self, "first_dip_tape_read_id", first_dip)
        if self._attestation_sha256:
            expected = _active_capture_prefix_attestation_sha256(self)
            if not hmac.compare_digest(self._attestation_sha256, expected):
                raise CaptureContractError("active capture prefix attestation changed")

    def _attestation_body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "generation": self.generation,
            "decision_id": self.decision_id,
            "decision_event_sha256": self.decision_event_sha256,
            "predecision_attestation_sha256": (
                self.predecision_attestation_sha256
            ),
            "predecision_read_evidence_inventory_sha256": (
                self.predecision_read_evidence_inventory_sha256
            ),
            "predecision_continuity_evidence_inventory_sha256": (
                self.predecision_continuity_evidence_inventory_sha256
            ),
            "decision_output_sha256": self.decision_output_sha256,
            "order_intent_sha256s": list(self.order_intent_sha256s),
            "input_prefix_sequence": self.input_prefix_sequence,
            "input_prefix_root_sha256": self.input_prefix_root_sha256,
            "attestation_frontier_sequence": self.attestation_frontier_sequence,
            "attestation_frontier_root_sha256": (
                self.attestation_frontier_root_sha256
            ),
            "decision_available_at": _iso_utc(self.decision_available_at),
            "attested_available_at": _iso_utc(self.attested_available_at),
            "expires_at": _iso_utc(self.expires_at),
            "identity_sha256": self.identity_sha256,
            "account_identity_sha256": self.account_identity_sha256,
            "code_build_sha256": self.code_build_sha256,
            "config_sha256": self.config_sha256,
            "feature_flags_sha256": self.feature_flags_sha256,
            "resource_binding_sha256": self.resource_binding_sha256,
            "producer_generations": dict(self.producer_generations),
            "required_read_ids": list(self.required_read_ids),
            "read_evidence": [row.to_evidence_dict() for row in self.read_evidence],
            "continuity_evidence": [
                row.to_evidence_dict() for row in self.continuity_evidence
            ],
            "first_dip_tape_read_id": self.first_dip_tape_read_id,
        }

    @property
    def attestation_sha256(self) -> str:
        return self._attestation_sha256

    @property
    def durable_authority_sha256(self) -> str:
        """Public replay-verifiable binding for downstream broker events."""

        return _final_decision_authority_sha256(
            identity_sha256=self.identity_sha256,
            decision_id=self.decision_id,
            decision_event_sha256=self.decision_event_sha256,
            decision_output_sha256=self.decision_output_sha256,
            order_intent_sha256s=self.order_intent_sha256s,
            input_prefix_sequence=self.input_prefix_sequence,
            input_prefix_root_sha256=self.input_prefix_root_sha256,
        )


def _active_capture_prefix_attestation_sha256(
    value: ActiveCapturePrefixAttestation,
) -> str:
    return hmac.new(
        _ACTIVE_CAPTURE_PREFIX_ATTESTATION_KEY,
        canonical_json_bytes(value._attestation_body()),
        hashlib.sha256,
    ).hexdigest()


def _issue_active_capture_prefix_attestation(
    *,
    run_id: str,
    generation: int,
    decision_id: str,
    decision_event_sha256: str,
    predecision_attestation_sha256: str,
    predecision_read_evidence_inventory_sha256: str,
    predecision_continuity_evidence_inventory_sha256: str,
    decision_output_sha256: str,
    order_intent_sha256s: tuple[str, ...],
    input_prefix_sequence: int,
    input_prefix_root_sha256: str,
    attestation_frontier_sequence: int,
    attestation_frontier_root_sha256: str,
    decision_available_at: datetime,
    attested_available_at: datetime,
    expires_at: datetime,
    identity_sha256: str,
    account_identity_sha256: str,
    code_build_sha256: str,
    config_sha256: str,
    feature_flags_sha256: str,
    resource_binding_sha256: str,
    producer_generations: Mapping[str, int],
    required_read_ids: tuple[str, ...],
    read_evidence: tuple[ActiveCaptureReadEvidence, ...],
    continuity_evidence: tuple[ActiveCaptureContinuityEvidence, ...],
    first_dip_tape_read_id: str | None = None,
) -> ActiveCapturePrefixAttestation:
    value = ActiveCapturePrefixAttestation(
        schema_version=ACTIVE_CAPTURE_PREFIX_ATTESTATION_SCHEMA_VERSION,
        run_id=run_id,
        generation=generation,
        decision_id=decision_id,
        decision_event_sha256=decision_event_sha256,
        predecision_attestation_sha256=predecision_attestation_sha256,
        predecision_read_evidence_inventory_sha256=(
            predecision_read_evidence_inventory_sha256
        ),
        predecision_continuity_evidence_inventory_sha256=(
            predecision_continuity_evidence_inventory_sha256
        ),
        decision_output_sha256=decision_output_sha256,
        order_intent_sha256s=order_intent_sha256s,
        input_prefix_sequence=input_prefix_sequence,
        input_prefix_root_sha256=input_prefix_root_sha256,
        attestation_frontier_sequence=attestation_frontier_sequence,
        attestation_frontier_root_sha256=attestation_frontier_root_sha256,
        decision_available_at=decision_available_at,
        attested_available_at=attested_available_at,
        expires_at=expires_at,
        identity_sha256=identity_sha256,
        account_identity_sha256=account_identity_sha256,
        code_build_sha256=code_build_sha256,
        config_sha256=config_sha256,
        feature_flags_sha256=feature_flags_sha256,
        resource_binding_sha256=resource_binding_sha256,
        producer_generations=producer_generations,
        required_read_ids=required_read_ids,
        read_evidence=read_evidence,
        continuity_evidence=continuity_evidence,
        first_dip_tape_read_id=first_dip_tape_read_id,
        _verification_token=_ACTIVE_CAPTURE_PREFIX_ATTESTATION_TOKEN,
    )
    object.__setattr__(
        value, "_attestation_sha256", _active_capture_prefix_attestation_sha256(value)
    )
    return value


def verify_active_capture_prefix_attestation(
    value: ActiveCapturePrefixAttestation,
) -> ActiveCapturePrefixAttestation:
    """Verify a live-only private-token object; serialized lookalikes never pass."""

    if not isinstance(value, ActiveCapturePrefixAttestation):
        raise CaptureContractError("active capture prefix attestation is malformed")
    if value._verification_token is not _ACTIVE_CAPTURE_PREFIX_ATTESTATION_TOKEN:
        raise CaptureContractError("active capture prefix attestation token is invalid")
    expected = _active_capture_prefix_attestation_sha256(value)
    if not value._attestation_sha256 or not hmac.compare_digest(
        value._attestation_sha256, expected
    ):
        raise CaptureContractError("active capture prefix attestation is invalid")
    return value


@dataclass(frozen=True)
class CaptureDecisionCheckpoint:
    """Decision-time input prefix chained into the final post-exit manifest.

    Every causal source named by a read is available by ``decision_at``.  The
    receipt/control proof of that already-consumed read may be durably published
    afterward, but no later than this checkpoint's ``available_at``; its sequence
    remains inside ``input_prefix_root_sha256``.  The checkpoint must never claim
    that the final manifest, which includes hold/exit evidence, was available at
    entry time.
    """

    identity_sha256: str
    decision_id: str
    symbol: str
    decision_at: datetime
    available_at: datetime
    decision_event_sha256: str
    input_prefix_sequence: int
    input_prefix_root_sha256: str
    required_read_ids: tuple[str, ...]
    decision_payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "identity_sha256",
            _require_sha256(self.identity_sha256, "checkpoint.identity_sha256"),
        )
        decision_id = str(self.decision_id or "").strip()
        symbol = str(self.symbol or "").strip().upper()
        if not decision_id or not symbol:
            raise CaptureContractError("checkpoint decision_id and symbol are required")
        object.__setattr__(self, "decision_id", decision_id)
        object.__setattr__(self, "symbol", symbol)
        decision_at = _utc(self.decision_at, "checkpoint.decision_at")
        available_at = _utc(self.available_at, "checkpoint.available_at")
        if available_at < decision_at:
            raise CaptureContractError("checkpoint cannot be available before its decision")
        object.__setattr__(self, "decision_at", decision_at)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(
            self,
            "decision_event_sha256",
            _require_sha256(
                self.decision_event_sha256, "checkpoint.decision_event_sha256"
            ),
        )
        if (
            isinstance(self.input_prefix_sequence, bool)
            or int(self.input_prefix_sequence) <= 0
        ):
            raise CaptureContractError("checkpoint input prefix sequence must be positive")
        object.__setattr__(
            self, "input_prefix_sequence", int(self.input_prefix_sequence)
        )
        object.__setattr__(
            self,
            "input_prefix_root_sha256",
            _require_sha256(
                self.input_prefix_root_sha256,
                "checkpoint.input_prefix_root_sha256",
            ),
        )
        read_ids = tuple(
            sorted(_uuid_text(value, "checkpoint.required_read_id") for value in self.required_read_ids)
        )
        if len(read_ids) != len(set(read_ids)):
            raise CaptureContractError("checkpoint contains duplicate read ids")
        object.__setattr__(self, "required_read_ids", read_ids)
        if not isinstance(self.decision_payload, Mapping):
            raise CaptureContractError("checkpoint decision payload must be a mapping")
        payload = _freeze_canonical_json(self.decision_payload)
        object.__setattr__(self, "decision_payload", payload)
        decision_output = capture_decision_output_from_payload(payload)
        payload_read_ids = tuple(sorted(str(value) for value in payload.get("required_read_ids", ())))
        payload_decision_at = _parse_utc(
            payload.get("decision_at"), "checkpoint.decision_payload.decision_at"
        )
        if (
            str(payload.get("decision_id") or "").strip() != decision_id
            or str(payload.get("symbol") or "").strip().upper() != symbol
            or payload_decision_at != decision_at
            or int(payload.get("input_prefix_sequence") or 0)
            != self.input_prefix_sequence
            or str(payload.get("input_prefix_root_sha256") or "").strip().lower()
            != self.input_prefix_root_sha256
            or payload_read_ids != read_ids
        ):
            raise CaptureContractError("checkpoint decision payload does not match its envelope")
        dependency_payload = payload.get("fsm_dependency_profile")
        if dependency_payload is not None:
            if not isinstance(dependency_payload, Mapping):
                raise CaptureContractError(
                    "checkpoint FSM dependency profile must be a mapping"
                )
            dependency_profile = FSMDependencyProfile.from_dict(dependency_payload)
            if dependency_profile.required_read_ids != read_ids:
                raise CaptureContractError(
                    "checkpoint FSM dependency profile does not match its read set"
                )
            first_dip_raw = payload.get("first_dip_tape_read_id")
            first_dip_read_id = (
                None
                if first_dip_raw is None
                else _uuid_text(
                    first_dip_raw,
                    "checkpoint.decision_payload.first_dip_tape_read_id",
                )
            )
            first_dip_purpose_raw = payload.get("first_dip_tape_purpose")
            first_dip_purpose = (
                _FIRST_DIP_TAPE_PURPOSE_DETECTOR
                if first_dip_purpose_raw is None
                else str(first_dip_purpose_raw or "").strip().lower()
            )
            if (
                first_dip_read_id is not None
                and first_dip_purpose not in _FIRST_DIP_TAPE_PURPOSES
            ):
                raise CaptureContractError(
                    "checkpoint first-dip tape purpose is invalid"
                )
            if first_dip_read_id is None and first_dip_purpose_raw is not None:
                raise CaptureContractError(
                    "checkpoint first-dip tape purpose lacks its typed read"
                )
            if (
                decision_output.setup_role == "first_dip_reclaim"
                and decision_output.action is CaptureDecisionAction.ORDER_INTENT
                and (
                    CaptureStream.IQFEED_PRINT
                    not in dependency_profile.required_streams
                    or first_dip_read_id is None
                )
            ):
                raise CaptureContractError(
                    "checkpoint first-dip order lacks typed tape evidence"
                )
            if first_dip_read_id is not None:
                if (
                    CaptureStream.IQFEED_PRINT
                    not in dependency_profile.required_streams
                    or first_dip_read_id not in read_ids
                ):
                    raise CaptureContractError(
                        "checkpoint first-dip read is outside its dependency profile"
                    )
                policy_payload = payload.get("first_dip_tape_policy")
                evaluation_payload = payload.get("first_dip_tape_evaluation")
                if not isinstance(policy_payload, Mapping) or not isinstance(
                    evaluation_payload, Mapping
                ):
                    raise CaptureContractError(
                        "checkpoint IQFeed dependency lacks typed policy/evaluation"
                    )
                policy_sha256 = _require_sha256(
                    str(payload.get("first_dip_tape_policy_sha256") or ""),
                    "checkpoint.first_dip_tape_policy_sha256",
                )
                evaluation_sha256 = _require_sha256(
                    str(payload.get("first_dip_tape_evaluation_sha256") or ""),
                    "checkpoint.first_dip_tape_evaluation_sha256",
                )
                if (
                    sha256_json(policy_payload) != policy_sha256
                    or sha256_json(evaluation_payload) != evaluation_sha256
                ):
                    raise CaptureContractError(
                        "checkpoint first-dip policy/evaluation content hash mismatch"
                    )
                if decision_output.setup_role != "first_dip_reclaim":
                    raise CaptureContractError(
                        "checkpoint typed first-dip evidence is bound to another setup"
                    )
                evaluation_symbol = str(
                    evaluation_payload.get("symbol") or ""
                ).strip().upper()
                evaluation_read_id = str(
                    evaluation_payload.get("read_id") or ""
                ).strip()
                evaluation_policy_sha256 = str(
                    evaluation_payload.get("policy_sha256") or ""
                ).strip().lower()
                try:
                    evaluation_decision_at = _parse_utc(
                        evaluation_payload.get("decision_at"),
                        "checkpoint.first_dip_tape_evaluation.decision_at",
                    )
                except (CaptureContractError, TypeError, ValueError) as exc:
                    raise CaptureContractError(
                        "checkpoint first-dip evaluation identity is malformed"
                    ) from exc
                if (
                    evaluation_symbol != symbol
                    or evaluation_read_id != first_dip_read_id
                    or evaluation_policy_sha256 != policy_sha256
                    or evaluation_decision_at != decision_at
                ):
                    raise CaptureContractError(
                        "checkpoint first-dip evaluation escaped its exact decision"
                    )
                if decision_output.action is CaptureDecisionAction.ORDER_INTENT:
                    if (
                        first_dip_purpose
                        != _FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION
                    ):
                        raise CaptureContractError(
                            "checkpoint first-dip order lacks pre-reservation tape purpose"
                        )
                    if (
                        evaluation_payload.get("status") != "valid_positive"
                        or evaluation_payload.get("confirmed") is not True
                        or evaluation_payload.get("reason")
                        != "first_dip_tape_confirmed"
                    ):
                        raise CaptureContractError(
                            "checkpoint first-dip order lacks a positive tape verdict"
                        )
                    # The current IQFeed print payload is a deterministic v1
                    # mechanics contract.  It does not yet prove the provider's
                    # occurrence/correction/condition semantics, so it cannot
                    # authorize a risk-changing or broker-facing order in either
                    # live or sealed replay.  A future typed capability must
                    # replace this invariant explicitly, never by setup labels.
                    raise CaptureContractError(
                        "checkpoint first-dip IQFeed v1 evidence is mechanics-only"
                    )

    @cached_property
    def checkpoint_sha256(self) -> str:
        return sha256_json(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity_sha256": self.identity_sha256,
            "decision_id": self.decision_id,
            "symbol": self.symbol,
            "decision_at": _iso_utc(self.decision_at),
            "available_at": _iso_utc(self.available_at),
            "decision_event_sha256": self.decision_event_sha256,
            "input_prefix_sequence": self.input_prefix_sequence,
            "input_prefix_root_sha256": self.input_prefix_root_sha256,
            "required_read_ids": list(self.required_read_ids),
            "decision_payload": dict(self.decision_payload),
        }


@dataclass(frozen=True)
class StreamCoverage:
    stream: CaptureStream
    identity_sha256: str
    provider: str
    first_available_at: datetime
    last_available_at: datetime
    event_count: int
    exact_event_clock_complete: bool
    content_verified: bool
    continuity_complete: bool
    watermark: ProviderWatermark | None = None
    query_receipt_count: int = 0
    symbol: str | None = None

    def __post_init__(self) -> None:
        try:
            stream = (
                self.stream
                if isinstance(self.stream, CaptureStream)
                else CaptureStream(str(self.stream))
            )
        except ValueError as exc:
            raise CaptureContractError(
                f"unknown coverage stream: {self.stream}"
            ) from exc
        object.__setattr__(self, "stream", stream)
        object.__setattr__(
            self,
            "identity_sha256",
            _require_sha256(self.identity_sha256, "coverage.identity_sha256"),
        )
        provider = str(self.provider or "").strip()
        if not provider:
            raise CaptureContractError("coverage provider is required")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(
            self, "symbol", str(self.symbol or "").strip().upper() or None
        )
        first = _utc(self.first_available_at, "coverage.first_available_at")
        last = _utc(self.last_available_at, "coverage.last_available_at")
        if last < first:
            raise CaptureContractError("coverage end cannot precede start")
        if (
            isinstance(self.event_count, bool)
            or isinstance(self.query_receipt_count, bool)
            or int(self.event_count) < 0
            or int(self.query_receipt_count) < 0
        ):
            raise CaptureContractError("coverage counts cannot be negative")
        for name in (
            "exact_event_clock_complete",
            "content_verified",
            "continuity_complete",
        ):
            if type(getattr(self, name)) is not bool:
                raise CaptureContractError(f"{name} must be boolean")
        if self.watermark is not None:
            if not isinstance(self.watermark, ProviderWatermark):
                raise CaptureContractError("watermark must be a ProviderWatermark")
            if self.watermark.stream is not stream:
                raise CaptureContractError("coverage/watermark stream mismatch")
            if self.watermark.identity_sha256 != self.identity_sha256:
                raise CaptureContractError("coverage/watermark identity mismatch")
            if self.watermark.provider != self.provider:
                raise CaptureContractError("coverage/watermark provider mismatch")
            if self.watermark.symbol != self.symbol:
                raise CaptureContractError("coverage/watermark symbol mismatch")
        object.__setattr__(self, "first_available_at", first)
        object.__setattr__(self, "last_available_at", last)
        object.__setattr__(self, "event_count", int(self.event_count))
        object.__setattr__(self, "query_receipt_count", int(self.query_receipt_count))

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream": self.stream.value,
            "identity_sha256": self.identity_sha256,
            "provider": self.provider,
            "first_available_at": _iso_utc(self.first_available_at),
            "last_available_at": _iso_utc(self.last_available_at),
            "event_count": self.event_count,
            "exact_event_clock_complete": self.exact_event_clock_complete,
            "content_verified": self.content_verified,
            "continuity_complete": self.continuity_complete,
            "watermark": (
                self.watermark.to_dict() if self.watermark is not None else None
            ),
            "query_receipt_count": self.query_receipt_count,
            "symbol": self.symbol,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StreamCoverage":
        expected = {
            "stream",
            "identity_sha256",
            "provider",
            "first_available_at",
            "last_available_at",
            "event_count",
            "exact_event_clock_complete",
            "content_verified",
            "continuity_complete",
            "watermark",
            "query_receipt_count",
            "symbol",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise CaptureContractError("stream coverage fields do not match schema")
        raw_watermark = raw.get("watermark")
        if raw_watermark is not None and not isinstance(raw_watermark, Mapping):
            raise CaptureContractError("stream coverage watermark is malformed")
        try:
            watermark = (
                None
                if raw_watermark is None
                else ProviderWatermark.from_dict(raw_watermark)
            )
            return cls(
                stream=CaptureStream(str(raw.get("stream") or "")),
                identity_sha256=str(raw.get("identity_sha256") or ""),
                provider=str(raw.get("provider") or ""),
                first_available_at=_parse_utc(
                    raw.get("first_available_at"),
                    "coverage.first_available_at",
                ),
                last_available_at=_parse_utc(
                    raw.get("last_available_at"),
                    "coverage.last_available_at",
                ),
                event_count=raw.get("event_count"),
                exact_event_clock_complete=raw.get(
                    "exact_event_clock_complete"
                ),
                content_verified=raw.get("content_verified"),
                continuity_complete=raw.get("continuity_complete"),
                watermark=watermark,
                query_receipt_count=raw.get("query_receipt_count"),
                symbol=raw.get("symbol"),
            )
        except (TypeError, ValueError) as exc:
            raise CaptureContractError("stream coverage encoding is invalid") from exc


def grade_capture_decision_order_path(
    events: Sequence[CaptureEvent],
) -> tuple[str, ...]:
    """Grade canonical decision outputs and their exact broker transitions."""

    reasons: list[str] = []

    def reason(value: str) -> None:
        if value not in reasons:
            reasons.append(value)

    outputs: dict[str, CaptureDecisionOutput] = {}
    decision_authorities: dict[str, str] = {}
    intents: dict[str, tuple[str, CaptureOrderIntent]] = {}
    intent_decision_sequences: dict[str, int] = {}
    intent_client_ids: dict[str, str] = {}
    for event in sorted(events, key=lambda row: row.sequence):
        if event.stream is not CaptureStream.FSM_DECISION:
            continue
        try:
            output = capture_decision_output_from_payload(event.payload)
        except (CaptureContractError, TypeError, ValueError):
            reason(f"capture_fsm_decision_output_invalid:sequence_{event.sequence}")
            continue
        if output.decision_id in outputs:
            reason(f"capture_fsm_decision_output_duplicate:{output.decision_id}")
            continue
        if event.symbol != output.symbol:
            reason(f"capture_fsm_decision_output_symbol_mismatch:{output.decision_id}")
        outputs[output.decision_id] = output
        try:
            decision_authorities[output.decision_id] = (
                capture_final_decision_authority_sha256(event, output)
            )
        except (CaptureContractError, TypeError, ValueError):
            reason(
                f"capture_final_decision_authority_invalid:{output.decision_id}"
            )
        for intent in output.order_intents:
            if intent.order_intent_sha256 in intents:
                reason(f"capture_order_intent_duplicate:{intent.order_intent_sha256}")
            prior_intent_sha256 = intent_client_ids.get(intent.client_order_id)
            if (
                prior_intent_sha256 is not None
                and prior_intent_sha256 != intent.order_intent_sha256
            ):
                reason(f"capture_order_client_id_duplicate:{intent.client_order_id}")
            intents[intent.order_intent_sha256] = (output.decision_id, intent)
            intent_decision_sequences[intent.order_intent_sha256] = event.sequence
            intent_client_ids[intent.client_order_id] = intent.order_intent_sha256

    for intent_sha256, (_, intent) in intents.items():
        predecessor_sha256 = intent.replaces_order_intent_sha256
        if intent.intent_role is not CaptureOrderIntentRole.REPLACEMENT:
            continue
        predecessor = intents.get(str(predecessor_sha256 or ""))
        if (
            predecessor is None
            or predecessor[1].symbol != intent.symbol
            or intent_decision_sequences.get(str(predecessor_sha256), 0)
            >= intent_decision_sequences[intent_sha256]
        ):
            reason(
                f"capture_replacement_intent_predecessor_invalid:{intent.client_order_id}"
            )

    lifecycle_by_intent: dict[
        str, list[tuple[CaptureEvent, CaptureBrokerOrderLifecycle]]
    ] = defaultdict(list)
    for event in sorted(events, key=lambda row: row.sequence):
        if event.stream is not CaptureStream.BROKER_ORDER_LIFECYCLE:
            continue
        try:
            row = CaptureBrokerOrderLifecycle.from_dict(event.payload)
        except (CaptureContractError, TypeError, ValueError):
            reason(f"capture_broker_lifecycle_invalid:sequence_{event.sequence}")
            continue
        bound = intents.get(row.order_intent_sha256)
        if bound is None:
            reason(f"capture_broker_lifecycle_intent_missing:{row.client_order_id}")
            continue
        decision_id, intent = bound
        if (
            row.decision_id != decision_id
            or row.client_order_id != intent.client_order_id
            or row.client_order_id_sha256 != intent.client_order_id_sha256
            or row.order_quantity != intent.quantity
            or event.symbol != intent.symbol
        ):
            reason(f"capture_broker_lifecycle_binding_mismatch:{row.client_order_id}")
        if row.final_decision_attestation_sha256 != decision_authorities.get(
            decision_id
        ):
            reason(
                f"capture_broker_decision_authority_mismatch:{row.client_order_id}"
            )
        if intent.risk_increasing:
            if row.reservation_claim_sha256 != intent.reservation_claim_sha256:
                reason(
                    f"capture_broker_reservation_claim_mismatch:{row.client_order_id}"
                )
        elif row.reservation_claim_sha256 is not None:
            reason(
                f"capture_broker_unexpected_reservation_claim:{row.client_order_id}"
            )
        lifecycle_by_intent[row.order_intent_sha256].append((event, row))

    broker_ids_by_intent = {
        intent_sha256: {
            row.broker_order_id
            for _, row in rows
            if row.broker_order_id is not None
        }
        for intent_sha256, rows in lifecycle_by_intent.items()
    }
    for intent_sha256, (decision_id, intent) in intents.items():
        rows = lifecycle_by_intent.get(intent_sha256, [])
        if not rows:
            reason(f"capture_broker_lifecycle_missing:{decision_id}:{intent.client_order_id}")
            continue
        previous_event: CaptureEvent | None = None
        previous_row: CaptureBrokerOrderLifecycle | None = None
        broker_order_id: str | None = None
        replacement_predecessor_broker_id: str | None = None
        if intent.intent_role is CaptureOrderIntentRole.REPLACEMENT:
            predecessor_sha256 = intent.replaces_order_intent_sha256
            predecessor_ids = broker_ids_by_intent.get(
                str(predecessor_sha256 or ""),
                set(),
            )
            if len(predecessor_ids) != 1:
                reason(
                    f"capture_replacement_broker_predecessor_missing:{intent.client_order_id}"
                )
            else:
                replacement_predecessor_broker_id = next(iter(predecessor_ids))
        for event, row in rows:
            if row.transition not in {
                CaptureBrokerTransition.SUBMITTED,
                CaptureBrokerTransition.REJECTED,
                CaptureBrokerTransition.FAILED,
            } and row.broker_order_id is None:
                reason(
                    f"capture_broker_order_id_missing:{intent.client_order_id}"
                )
            if intent.intent_role is CaptureOrderIntentRole.REPLACEMENT:
                if (
                    row.replaces_broker_order_id
                    != replacement_predecessor_broker_id
                ):
                    reason(
                        f"capture_replacement_broker_predecessor_mismatch:{intent.client_order_id}"
                    )
            elif row.replaces_broker_order_id is not None:
                reason(
                    f"capture_nonreplacement_has_predecessor:{intent.client_order_id}"
                )
            if row.replacement_order_intent_sha256 is not None:
                successor = intents.get(row.replacement_order_intent_sha256)
                if (
                    successor is None
                    or successor[1].intent_role
                    is not CaptureOrderIntentRole.REPLACEMENT
                    or successor[1].replaces_order_intent_sha256 != intent_sha256
                ):
                    reason(
                        f"capture_replacement_successor_intent_mismatch:{intent.client_order_id}"
                    )
                elif (
                    row.replaced_by_broker_order_id is not None
                    and row.replaced_by_broker_order_id
                    not in broker_ids_by_intent.get(
                        row.replacement_order_intent_sha256,
                        set(),
                    )
                ):
                    reason(
                        f"capture_replacement_successor_broker_mismatch:{intent.client_order_id}"
                    )
            if previous_row is None:
                if (
                    row.transition is not CaptureBrokerTransition.SUBMITTED
                    or row.prior_transition_event_sha256 is not None
                    or row.cumulative_filled_quantity != 0
                ):
                    reason(
                        f"capture_broker_lifecycle_initial_invalid:{intent.client_order_id}"
                    )
            else:
                assert previous_event is not None
                if row.prior_transition_event_sha256 != previous_event.event_sha256:
                    reason(f"capture_broker_lifecycle_chain_broken:{intent.client_order_id}")
                if row.transition is CaptureBrokerTransition.SUBMITTED:
                    reason(f"capture_broker_lifecycle_order_invalid:{intent.client_order_id}")
                if (
                    row.transition is CaptureBrokerTransition.REPLACED
                    and previous_row.transition
                    is not CaptureBrokerTransition.PENDING_REPLACE
                ):
                    reason(
                        f"capture_broker_replaced_without_pending:{intent.client_order_id}"
                    )
                delta = (
                    row.cumulative_filled_quantity
                    - previous_row.cumulative_filled_quantity
                )
                if delta < 0 or delta != row.last_fill_quantity:
                    reason(
                        f"capture_broker_lifecycle_cumulative_fill_invalid:{intent.client_order_id}"
                    )
                if (
                    row.replaces_broker_order_id
                    != previous_row.replaces_broker_order_id
                ):
                    reason(
                        f"capture_replacement_predecessor_changed:{intent.client_order_id}"
                    )
                if (
                    row.final_decision_attestation_sha256
                    != previous_row.final_decision_attestation_sha256
                    or row.reservation_claim_sha256
                    != previous_row.reservation_claim_sha256
                ):
                    reason(
                        f"capture_broker_authority_changed:{intent.client_order_id}"
                    )
            if broker_order_id is None and row.broker_order_id is not None:
                broker_order_id = row.broker_order_id
            elif (
                broker_order_id is not None
                and row.broker_order_id != broker_order_id
            ):
                reason(
                    f"capture_broker_order_id_changed_or_dropped:{intent.client_order_id}"
                )
            previous_event, previous_row = event, row
        assert previous_row is not None
        if not previous_row.terminal:
            reason(f"capture_broker_lifecycle_nonterminal:{intent.client_order_id}")

    return tuple(reasons)


@dataclass(frozen=True)
class CaptureProducerLifecycleGrade:
    certified: bool
    reasons: tuple[str, ...]
    producer_roster_sha256: str | None
    run_close_event_sha256: str | None


def grade_capture_producer_lifecycle(
    *,
    identity: CaptureRunIdentity,
    events: Sequence[CaptureEvent],
    stream_coverage: Mapping[CaptureStream, StreamCoverage],
    coverage_health_events: Mapping[CaptureStream, CaptureEvent],
    watermark_events: Mapping[CaptureStream, CaptureEvent],
    resource_binding_sha256: str | None,
) -> CaptureProducerLifecycleGrade:
    """Verify the sealed RUN_OPEN -> producer quiescence -> RUN_CLOSED chain.

    Every fact is sourced from the exact sealed event inventory.  The
    ``recorded_at == available_at`` checks intentionally reject a lifecycle
    claim written later with an earlier timestamp, while the hash chain and
    sequence frontiers prevent a producer from closing before its final input
    and completeness evidence.
    """

    reasons: list[str] = []

    def reason(value: str) -> None:
        if value not in reasons:
            reasons.append(value)

    lifecycle_events = tuple(
        event
        for event in events
        if event.payload.get("schema_version")
        == CAPTURE_PRODUCER_LIFECYCLE_SCHEMA_VERSION
    )
    if not lifecycle_events:
        return CaptureProducerLifecycleGrade(
            certified=False,
            reasons=("capture_run_open_or_producer_roster_unverified",),
            producer_roster_sha256=None,
            run_close_event_sha256=None,
        )
    for event in lifecycle_events:
        if (
            event.stream is not CaptureStream.CAPTURE_HEALTH
            or event.provider != CAPTURE_PRODUCER_LIFECYCLE_PROVIDER
            or event.symbol is not None
            or event.clocks.provider_event_at is not None
            or event.clocks.market_reference_at is not None
        ):
            reason("capture_producer_lifecycle_envelope_invalid")

    run_open_events = tuple(
        event for event in lifecycle_events if event.payload.get("kind") == "RUN_OPEN"
    )
    if not run_open_events:
        reason("capture_run_open_missing")
    elif len(run_open_events) > 1:
        reason("capture_run_open_duplicate")
    if len(run_open_events) != 1:
        reason("capture_run_open_or_producer_roster_unverified")
        return CaptureProducerLifecycleGrade(
            certified=False,
            reasons=tuple(reasons),
            producer_roster_sha256=None,
            run_close_event_sha256=None,
        )
    run_open_event = run_open_events[0]
    raw_producers = run_open_event.payload.get("producers")
    if isinstance(raw_producers, list):
        raw_ids = [
            str(row.get("producer_id") or "").strip().lower()
            for row in raw_producers
            if isinstance(row, Mapping)
        ]
        raw_instances = [
            str(row.get("instance_id") or "").strip().lower()
            for row in raw_producers
            if isinstance(row, Mapping)
        ]
        if (
            len(raw_ids) != len(raw_producers)
            or len(raw_ids) != len(set(raw_ids))
            or len(raw_instances) != len(set(raw_instances))
        ):
            reason("capture_producer_roster_duplicate")
    try:
        run_open = CaptureRunOpen.from_dict(run_open_event.payload)
    except (CaptureContractError, TypeError, ValueError):
        reason("capture_run_open_invalid")
        reason("capture_run_open_or_producer_roster_unverified")
        return CaptureProducerLifecycleGrade(
            certified=False,
            reasons=tuple(reasons),
            producer_roster_sha256=None,
            run_close_event_sha256=None,
        )

    if run_open_event.sequence != 1 or min(event.sequence for event in events) != 1:
        reason("capture_run_open_not_sequence_one")
    if run_open_event.clocks.available_at != run_open.opened_at:
        reason("capture_run_open_backdated_or_late")
    if (
        run_open.identity_sha256 != identity.identity_sha256
        or run_open.run_id != identity.run_id
        or run_open.generation != identity.generation
    ):
        reason("capture_run_open_identity_mismatch")
    if resource_binding_sha256 is None:
        reason("capture_producer_resource_binding_unverified")
    elif run_open.resource_binding_sha256 != resource_binding_sha256:
        reason("capture_producer_resource_binding_mismatch")

    specs = {row.producer_id: row for row in run_open.producers}
    for spec in run_open.producers:
        if (
            spec.code_build_sha256 != identity.code_build_sha256
            or spec.config_sha256 != identity.config_sha256
            or spec.feature_flags_sha256 != identity.feature_flags_sha256
        ):
            reason(f"capture_producer_identity_binding_mismatch:{spec.producer_id}")
        if spec.resource_binding_sha256 != run_open.resource_binding_sha256:
            reason(f"capture_producer_resource_binding_mismatch:{spec.producer_id}")

    owners: dict[CaptureStream, list[str]] = {}
    for spec in run_open.producers:
        for stream in spec.streams:
            owners.setdefault(stream, []).append(spec.producer_id)
    for stream, producer_ids in sorted(owners.items(), key=lambda row: row[0].value):
        if len(producer_ids) > 1:
            reason(f"capture_producer_stream_conflict:{stream.value}")
        if stream not in stream_coverage:
            reason(f"capture_producer_stream_coverage_missing:{stream.value}")
    for stream in sorted(stream_coverage, key=lambda row: row.value):
        if stream not in owners:
            reason(f"capture_producer_stream_unowned:{stream.value}")

    # Completeness semantics are mode-specific.  Query receipts and immutable
    # identity snapshots are exact observations, not continuous subscriptions;
    # requiring a synthetic continuity claim for them would turn "unknown"
    # into a misleading pass/fail signal.  Continuous and change-log streams,
    # however, must prove that their whole declared interval is intact.
    for stream, coverage in sorted(
        stream_coverage.items(), key=lambda row: row[0].value
    ):
        policy = STREAM_POLICIES[stream]
        if coverage.identity_sha256 != identity.identity_sha256:
            reason(f"capture_producer_stream_identity_mismatch:{stream.value}")
        if not coverage.content_verified:
            reason(f"capture_producer_stream_content_unverified:{stream.value}")
        if (
            policy.coverage_mode
            in {CoverageMode.CONTINUOUS, CoverageMode.CHANGE_LOG}
            and not coverage.continuity_complete
        ):
            reason(f"capture_producer_stream_continuity_incomplete:{stream.value}")
        if (
            policy.exact_provider_event_clock_required
            and not coverage.exact_event_clock_complete
        ):
            reason(f"capture_producer_exact_clock_incomplete:{stream.value}")
        if (
            policy.coverage_mode
            in {CoverageMode.CONTINUOUS, CoverageMode.CHANGE_LOG}
            and coverage.watermark is None
        ):
            reason(f"capture_producer_watermark_missing:{stream.value}")

    fact_events: list[tuple[CaptureEvent, CaptureProducerLifecycleFact]] = []
    for event in lifecycle_events:
        if event is run_open_event:
            continue
        try:
            fact = CaptureProducerLifecycleFact.from_dict(event.payload)
        except (CaptureContractError, TypeError, ValueError):
            reason("capture_producer_lifecycle_fact_invalid")
            continue
        fact_events.append((event, fact))
        if fact.recorded_at != event.clocks.available_at:
            reason("capture_producer_lifecycle_backdated_or_late")
        if (
            fact.identity_sha256 != identity.identity_sha256
            or fact.producer_roster_sha256 != run_open.producer_roster_sha256
        ):
            reason("capture_producer_lifecycle_binding_mismatch")

    per_producer: dict[
        str, list[tuple[CaptureEvent, CaptureProducerLifecycleFact]]
    ] = {producer_id: [] for producer_id in specs}
    run_close_rows: list[tuple[CaptureEvent, CaptureProducerLifecycleFact]] = []
    for event, fact in fact_events:
        if fact.kind is CaptureProducerLifecycleKind.RUN_CLOSED:
            run_close_rows.append((event, fact))
            continue
        assert fact.producer_id is not None
        spec = specs.get(fact.producer_id)
        if spec is None:
            reason(f"capture_producer_unexpected:{fact.producer_id}")
            continue
        if (
            fact.producer_instance_id != spec.instance_id
            or fact.producer_generation != spec.generation
            or fact.producer_spec_sha256 != spec.spec_sha256
        ):
            reason(f"capture_producer_generation_conflict:{fact.producer_id}")
        per_producer[fact.producer_id].append((event, fact))

    event_by_hash = {event.event_sha256: event for event in events}
    registration_record_by_event_hash: dict[
        str, tuple[CaptureEvent, CaptureProviderRegistrationRecord]
    ] = {}
    for registration_event in (
        event
        for event in events
        if event.payload.get("schema_version")
        == CAPTURE_PROVIDER_REGISTRATION_RECORD_SCHEMA_VERSION
    ):
        try:
            registration_record = CaptureProviderRegistrationRecord.from_dict(
                registration_event.payload
            )
        except (CaptureContractError, TypeError, ValueError):
            reason("capture_provider_registration_record_invalid")
            continue
        if (
            registration_event.stream is not CaptureStream.CAPTURE_HEALTH
            or registration_event.provider != CAPTURE_PRODUCER_LIFECYCLE_PROVIDER
            or registration_event.symbol is not None
            or registration_event.clocks.provider_event_at is not None
            or registration_event.clocks.market_reference_at is not None
            or registration_record.identity_sha256 != identity.identity_sha256
        ):
            reason("capture_provider_registration_record_envelope_invalid")
        registration_record_by_event_hash[registration_event.event_sha256] = (
            registration_event,
            registration_record,
        )

    startup_streams = {
        CaptureStream.CODE_BUILD,
        CaptureStream.CONFIG_SNAPSHOT,
        CaptureStream.FEATURE_FLAG_SNAPSHOT,
        CaptureStream.ACCOUNT_RISK_SNAPSHOT,
    }
    coordinator_producers = {
        producer_id
        for producer_id, spec in specs.items()
        if startup_streams.issubset(set(spec.streams))
    }
    if len(coordinator_producers) > 1:
        reason("capture_coordinator_producer_ambiguous")

    def source_payload_and_available_at(
        event: CaptureEvent,
    ) -> tuple[Mapping[str, Any], datetime]:
        if "_capture_producer_registration" in event.payload:
            reason("capture_provider_registration_legacy_payload_metadata_present")
        try:
            view = resolve_capture_source_payload(event)
        except CaptureContractError:
            reason("capture_provider_registration_release_metadata_invalid")
            return event.payload, event.clocks.available_at
        return view.payload, view.original_available_at

    receipt_owner_by_event_hash: dict[str, str] = {}
    for receipt_event in (
        event for event in events if event.stream is CaptureStream.READ_RECEIPT
    ):
        try:
            receipt = CaptureReadReceipt.from_dict(receipt_event.payload)
        except (CaptureContractError, TypeError, ValueError):
            reason("capture_read_receipt_control_invalid")
            continue
        if (
            receipt.identity_sha256 != identity.identity_sha256
            or receipt_event.provider != receipt.provider
            or receipt_event.symbol != receipt.symbol
            or receipt_event.clocks.provider_event_at is not None
            or receipt_event.clocks.market_reference_at is not None
            or receipt_event.clocks.available_at < receipt.returned_at
        ):
            reason(f"capture_read_receipt_envelope_invalid:{receipt.read_id}")
        receipt_owners = owners.get(receipt.stream, ())
        if len(receipt_owners) != 1:
            reason(f"capture_read_receipt_owner_unverified:{receipt.read_id}")
        else:
            receipt_owner_by_event_hash[receipt_event.event_sha256] = receipt_owners[0]
        source_refs: list[CaptureEventRef] = []
        for source_sha256 in receipt.source_event_sha256s:
            source = event_by_hash.get(source_sha256)
            if source is None:
                reason(f"capture_read_receipt_source_missing:{receipt.read_id}")
                continue
            ref = CaptureEventRef.from_event(source)
            source_refs.append(ref)
            if (
                source.stream is not receipt.stream
                or source.provider != receipt.provider
                or source.symbol != receipt.symbol
                or source.clocks.available_at > receipt.returned_at
                or (
                    STREAM_POLICIES[receipt.stream].query_parameters_required
                    and source.query_sha256 != receipt.query_sha256
                )
            ):
                reason(f"capture_read_receipt_source_mismatch:{receipt.read_id}")
        if captured_read_result_sha256(source_refs) != receipt.result_sha256:
            reason(f"capture_read_receipt_result_mismatch:{receipt.read_id}")
    close_event_hashes: dict[str, str] = {}
    close_event_sequences: list[int] = []

    for producer_id, spec in specs.items():
        rows = sorted(per_producer[producer_id], key=lambda row: row[0].sequence)
        kinds = [fact.kind for _event, fact in rows]
        registrations = [
            row for row in rows if row[1].kind is CaptureProducerLifecycleKind.REGISTERED
        ]
        quiescent_rows = [
            row for row in rows if row[1].kind is CaptureProducerLifecycleKind.QUIESCENT
        ]
        closed_rows = [
            row for row in rows if row[1].kind is CaptureProducerLifecycleKind.CLOSED
        ]
        if not registrations:
            reason(f"capture_producer_registration_missing:{producer_id}")
        elif len(registrations) > 1:
            reason(f"capture_producer_registration_duplicate:{producer_id}")
        if len(quiescent_rows) > 1:
            reason(f"capture_producer_quiescence_duplicate:{producer_id}")
        if len(closed_rows) > 1:
            reason(f"capture_producer_close_duplicate:{producer_id}")
        if not closed_rows:
            reason(f"capture_producer_open:{producer_id}")
        if not rows:
            continue

        if (
            kinds[0] is not CaptureProducerLifecycleKind.REGISTERED
            or kinds[-1] is not CaptureProducerLifecycleKind.CLOSED
            or len(quiescent_rows) != 1
            or kinds[-2:] != [
                CaptureProducerLifecycleKind.QUIESCENT,
                CaptureProducerLifecycleKind.CLOSED,
            ]
            or any(
                kind in {
                    CaptureProducerLifecycleKind.REGISTERED,
                    CaptureProducerLifecycleKind.QUIESCENT,
                    CaptureProducerLifecycleKind.CLOSED,
                }
                for kind in kinds[1:-2]
            )
        ):
            reason(f"capture_producer_lifecycle_order_invalid:{producer_id}")

        previous_event = run_open_event
        previous_available_at = run_open.opened_at
        for event, fact in rows:
            if fact.prior_lifecycle_event_sha256 != previous_event.event_sha256:
                reason(f"capture_producer_lifecycle_chain_broken:{producer_id}")
            if (
                event.clocks.available_at - previous_available_at
            ).total_seconds() > run_open.heartbeat_timeout_seconds:
                reason(f"capture_producer_heartbeat_late:{producer_id}")
            previous_event = event
            previous_available_at = event.clocks.available_at
            if fact.kind is CaptureProducerLifecycleKind.GAP:
                reason(f"capture_producer_gap:{producer_id}:{fact.gap_reason}")

        owned_source_events = [
            event
            for event in events
            if event.stream in spec.streams
            or receipt_owner_by_event_hash.get(event.event_sha256) == producer_id
        ]
        if registrations and owned_source_events:
            first_owned = min(owned_source_events, key=lambda event: event.sequence)
            if registrations[0][0].sequence >= first_owned.sequence:
                reason(f"capture_producer_registered_after_inputs:{producer_id}")

        if len(registrations) == 1:
            registration_event, registration_fact = registrations[0]
            evidence_hashes = registration_fact.evidence_event_sha256s
            if producer_id in coordinator_producers:
                if evidence_hashes:
                    reason(
                        f"capture_coordinator_registration_evidence_unexpected:{producer_id}"
                    )
            elif len(evidence_hashes) != 1:
                reason(f"capture_provider_registration_evidence_missing:{producer_id}")
            else:
                record_pair = registration_record_by_event_hash.get(evidence_hashes[0])
                if record_pair is None:
                    reason(f"capture_provider_registration_evidence_missing:{producer_id}")
                else:
                    record_event, record = record_pair
                    evidence = record.evidence
                    if (
                        record_event.sequence + 1 != registration_event.sequence
                        or registration_fact.frontier_sequence != record_event.sequence
                        or evidence.producer_id != producer_id
                        or evidence.provider_instance_id != spec.instance_id
                        or evidence.provider_generation != spec.generation
                        or record.source_stream not in spec.streams
                    ):
                        reason(
                            f"capture_provider_registration_binding_mismatch:{producer_id}"
                        )
                    candidates = sorted(
                        (
                            event
                            for event in owned_source_events
                            if event.sequence > registration_event.sequence
                        ),
                        key=lambda event: event.sequence,
                    )
                    if not candidates:
                        reason(
                            f"capture_provider_registration_first_event_missing:{producer_id}"
                        )
                    else:
                        first_source = candidates[0]
                        source_payload, source_available_at = (
                            source_payload_and_available_at(first_source)
                        )
                        if (
                            first_source.sequence != registration_event.sequence + 1
                            or first_source.stream is not record.source_stream
                            or first_source.provider != evidence.provider
                            or first_source.symbol != record.source_symbol
                            or first_source.query_sha256 != record.source_query_sha256
                            or first_source.clocks.provider_event_at
                            != evidence.provider_event_at
                            or first_source.clocks.received_at != evidence.received_at
                            or first_source.clocks.market_reference_at
                            != record.source_market_reference_at
                            or source_available_at != record.source_available_at
                            or sha256_json(source_payload)
                            != evidence.source_payload_sha256
                        ):
                            reason(
                                f"capture_provider_registration_first_event_mismatch:{producer_id}"
                            )
                        if evidence.provider == "iqfeed":
                            try:
                                derived_registration = (
                                    build_provider_registration_evidence_from_source_event(
                                        first_source,
                                        producer_id=producer_id,
                                    )
                                )
                            except CaptureContractError:
                                reason(
                                    "capture_provider_registration_vendor_provenance_invalid:"
                                    f"{producer_id}"
                                )
                            else:
                                if derived_registration != evidence:
                                    reason(
                                        "capture_provider_registration_vendor_provenance_mismatch:"
                                        f"{producer_id}"
                                    )
        evidence_hashes: list[str] = []
        evidence_events: list[CaptureEvent] = []
        for stream in spec.streams:
            health_event = coverage_health_events.get(stream)
            if health_event is None:
                reason(f"capture_producer_health_evidence_missing:{producer_id}:{stream.value}")
            else:
                evidence_hashes.append(health_event.event_sha256)
                evidence_events.append(health_event)
            coverage = stream_coverage.get(stream)
            if coverage is None:
                continue
            if health_event is not None and (
                health_event.stream is not CaptureStream.CAPTURE_HEALTH
                or health_event.identity != identity
                or health_event.provider != coverage.provider
                or health_event.symbol != coverage.symbol
                or health_event.payload_sha256 != sha256_json(coverage.to_dict())
                or health_event.clocks.provider_event_at is not None
                or health_event.clocks.market_reference_at is not None
                or health_event.clocks.available_at < coverage.last_available_at
            ):
                reason(
                    f"capture_producer_health_evidence_envelope_invalid:{producer_id}:{stream.value}"
                )
            watermark_event = watermark_events.get(stream)
            if (
                STREAM_POLICIES[stream].coverage_mode
                in {CoverageMode.CONTINUOUS, CoverageMode.CHANGE_LOG}
            ):
                if coverage.watermark is None or watermark_event is None:
                    reason(
                        f"capture_producer_watermark_evidence_missing:{producer_id}:{stream.value}"
                    )
            if coverage.watermark is not None:
                if watermark_event is None:
                    reason(
                        f"capture_producer_watermark_evidence_missing:{producer_id}:{stream.value}"
                    )
                else:
                    evidence_hashes.append(watermark_event.event_sha256)
                    evidence_events.append(watermark_event)
                    if (
                        watermark_event.stream is not CaptureStream.PROVIDER_WATERMARK
                        or watermark_event.identity != identity
                        or watermark_event.provider != coverage.watermark.provider
                        or watermark_event.symbol != coverage.watermark.symbol
                        or watermark_event.payload_sha256
                        != sha256_json(coverage.watermark.to_dict())
                        or watermark_event.clocks.provider_event_at is not None
                        or watermark_event.clocks.market_reference_at
                        != coverage.watermark.event_watermark_at
                        or watermark_event.clocks.available_at
                        < coverage.watermark.emitted_available_at
                    ):
                        reason(
                            f"capture_producer_watermark_evidence_envelope_invalid:{producer_id}:{stream.value}"
                        )

        if len(quiescent_rows) == 1:
            quiescent_event, quiescent = quiescent_rows[0]
            expected_evidence = tuple(sorted(evidence_hashes))
            if quiescent.evidence_event_sha256s != expected_evidence:
                reason(f"capture_producer_quiescence_evidence_mismatch:{producer_id}")
            final_candidates = [
                event.sequence for event in (*owned_source_events, *evidence_events)
            ]
            expected_frontier = max(final_candidates, default=run_open_event.sequence)
            if quiescent.frontier_sequence != expected_frontier:
                reason(f"capture_producer_quiescence_frontier_mismatch:{producer_id}")
            if expected_frontier >= quiescent_event.sequence:
                reason(f"capture_producer_quiesced_before_final_evidence:{producer_id}")
            if any(
                event.sequence > quiescent_event.sequence
                for event in owned_source_events
            ):
                reason(f"capture_producer_input_after_quiescence:{producer_id}")
            if len(closed_rows) == 1:
                close_event, close_fact = closed_rows[0]
                if close_fact.frontier_sequence != quiescent.frontier_sequence:
                    reason(f"capture_producer_close_frontier_mismatch:{producer_id}")
                if close_fact.prior_lifecycle_event_sha256 != quiescent_event.event_sha256:
                    reason(f"capture_producer_close_chain_mismatch:{producer_id}")
                close_event_hashes[producer_id] = close_event.event_sha256
                close_event_sequences.append(close_event.sequence)

        for event, fact in rows:
            if fact.kind in {
                CaptureProducerLifecycleKind.REGISTERED,
                CaptureProducerLifecycleKind.HEARTBEAT,
                CaptureProducerLifecycleKind.GAP,
            }:
                registration_evidence_sequences = (
                    tuple(
                        event_by_hash[value].sequence
                        for value in fact.evidence_event_sha256s
                        if value in event_by_hash
                    )
                    if fact.kind is CaptureProducerLifecycleKind.REGISTERED
                    else ()
                )
                expected_frontier = max(
                    *registration_evidence_sequences,
                    *(
                        owned.sequence
                        for owned in owned_source_events
                        if owned.sequence < event.sequence
                    ),
                    run_open_event.sequence,
                )
                if fact.frontier_sequence != expected_frontier:
                    reason(f"capture_producer_runtime_frontier_mismatch:{producer_id}")
            for evidence_sha256 in fact.evidence_event_sha256s:
                if evidence_sha256 not in event_by_hash:
                    reason(f"capture_producer_evidence_event_missing:{producer_id}")

    run_close_sha256: str | None = None
    if not run_close_rows:
        reason("capture_run_close_missing")
    elif len(run_close_rows) > 1:
        reason("capture_run_close_duplicate")
    else:
        run_close_event, run_close = run_close_rows[0]
        run_close_sha256 = run_close_event.event_sha256
        if run_close_event.sequence != max(event.sequence for event in events):
            reason("capture_run_close_not_final_event")
        if run_close.producer_close_event_sha256s != close_event_hashes:
            reason("capture_run_close_roster_mismatch")
        expected_frontier = max(close_event_sequences, default=run_open_event.sequence)
        if run_close.frontier_sequence != expected_frontier:
            reason("capture_run_close_frontier_mismatch")
        if expected_frontier >= run_close_event.sequence:
            reason("capture_run_closed_before_producers")

    for decision_reason in grade_capture_decision_order_path(events):
        reason(decision_reason)
    if reasons:
        reason("capture_run_open_or_producer_roster_unverified")
    return CaptureProducerLifecycleGrade(
        certified=not reasons,
        reasons=tuple(reasons),
        producer_roster_sha256=run_open.producer_roster_sha256,
        run_close_event_sha256=run_close_sha256,
    )


def capture_event_inventory_sha256(
    event_refs: Iterable[CaptureEventRef],
) -> str:
    """Root of the complete event inventory projected from a verified seal."""

    return sha256_json(
        {
            "events": [
                ref.to_dict()
                for ref in sorted(
                    event_refs, key=lambda row: (row.sequence, row.event_sha256)
                )
            ]
        }
    )


def capture_gap_inventory_sha256(
    identity: CaptureRunIdentity,
    gaps: Iterable[CoverageGap],
) -> str:
    """Root of the complete gap inventory projected from a verified seal."""

    return sha256_json(
        {
            "identity_sha256": identity.identity_sha256,
            "gaps": [
                gap.to_dict()
                for gap in sorted(
                    gaps,
                    key=lambda row: (
                        row.first_available_at,
                        row.last_available_at,
                        row.stream.value,
                        row.symbol or "",
                        row.reason,
                        row.lost_count,
                    ),
                )
            ],
        }
    )


@dataclass(frozen=True)
class ReplayCoverageRequest:
    warmup_start_at: datetime
    decision_at: datetime
    exit_end_at: datetime
    required_streams: frozenset[CaptureStream]
    decision_id: str
    decision_checkpoint_sha256: str
    required_read_ids: frozenset[str] = frozenset()
    symbol: str | None = None
    expected_identity_sha256: str | None = None

    def __post_init__(self) -> None:
        warmup = _utc(self.warmup_start_at, "warmup_start_at")
        decision = _utc(self.decision_at, "decision_at")
        exit_end = _utc(self.exit_end_at, "exit_end_at")
        if not warmup <= decision <= exit_end:
            raise CaptureContractError("coverage window must be warmup <= decision <= exit")
        object.__setattr__(self, "warmup_start_at", warmup)
        object.__setattr__(self, "decision_at", decision)
        object.__setattr__(self, "exit_end_at", exit_end)
        try:
            streams = frozenset(
                stream
                if isinstance(stream, CaptureStream)
                else CaptureStream(str(stream))
                for stream in self.required_streams
            )
        except ValueError as exc:
            raise CaptureContractError("required_streams contains an unknown stream") from exc
        if not streams:
            raise CaptureContractError("required_streams cannot be empty")
        object.__setattr__(self, "required_streams", streams)
        decision_id = str(self.decision_id or "").strip()
        if not decision_id:
            raise CaptureContractError("decision_id is required")
        object.__setattr__(self, "decision_id", decision_id)
        object.__setattr__(
            self,
            "decision_checkpoint_sha256",
            _require_sha256(
                self.decision_checkpoint_sha256,
                "decision_checkpoint_sha256",
            ),
        )
        object.__setattr__(
            self,
            "required_read_ids",
            frozenset(
                _uuid_text(read_id, "required_read_id")
                for read_id in self.required_read_ids
            ),
        )
        object.__setattr__(
            self, "symbol", str(self.symbol or "").strip().upper() or None
        )
        if self.expected_identity_sha256 is not None:
            object.__setattr__(
                self,
                "expected_identity_sha256",
                _require_sha256(
                    self.expected_identity_sha256, "expected_identity_sha256"
                ),
            )


_VERIFIED_REPLAY_CAPTURE_TOKEN = object()
_VERIFICATION_ATTESTATION_KEY = secrets.token_bytes(32)


def _verification_attestation_sha256(payload: Mapping[str, Any]) -> str:
    return hmac.new(
        _VERIFICATION_ATTESTATION_KEY,
        canonical_json_bytes(payload),
        hashlib.sha256,
    ).hexdigest()


def _inventory_accumulator_sha256(rows: Iterable[Mapping[str, Any]]) -> str:
    """Recompute the runtime's bounded, order-independent inventory proof."""

    total = 0
    for row in rows:
        total = (total + int(sha256_json(row), 16)) % (1 << 256)
    return f"{total:064x}"


def _event_inventory_sha256(event_refs: Iterable[CaptureEventRef]) -> str:
    return capture_event_inventory_sha256(event_refs)


def _gap_inventory_sha256(
    identity: CaptureRunIdentity, gaps: Iterable[CoverageGap]
) -> str:
    return capture_gap_inventory_sha256(identity, gaps)


_RESOURCE_HASH_FIELD_NAMES = (
    "resource_measurement_sha256",
    "resource_policy_sha256",
    "resource_budget_sha256",
    "resource_binding_sha256",
)


def _validated_optional_resource_hashes(
    values: Mapping[str, Any], *, description: str
) -> dict[str, str] | None:
    """Require an all-or-none exact resource provenance tuple.

    These values are intentionally carried as four independent attested fields
    rather than as a caller-facing boolean.  The verified loader sources them
    only from the exact caller-pinned runtime seal, whose content root includes
    the same tuple and whose store has already matched it to its resource
    binding.
    """

    present = tuple(values.get(name) is not None for name in _RESOURCE_HASH_FIELD_NAMES)
    if any(present) and not all(present):
        raise CaptureContractError(f"{description} resource hashes are incomplete")
    if not any(present):
        return None
    normalized = {
        name: _require_sha256(str(values[name]), f"{description} {name}")
        for name in _RESOURCE_HASH_FIELD_NAMES
    }
    return {
        "measurement_sha256": normalized["resource_measurement_sha256"],
        "policy_sha256": normalized["resource_policy_sha256"],
        "budget_sha256": normalized["resource_budget_sha256"],
        "binding_sha256": normalized["resource_binding_sha256"],
    }


def _seal_binding_attestation_payload(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: values[key]
        for key in (
            "identity_sha256",
            "expected_final_seal_sha256",
            "final_seal_sha256",
            "seal_content_root_sha256",
            "close_proof_sha256",
            "event_accumulator_sha256",
            "gap_accumulator_sha256",
            "event_inventory_sha256",
            "gap_inventory_sha256",
            "control_evidence_sha256",
            *_RESOURCE_HASH_FIELD_NAMES,
            "event_count",
            "gap_count",
            "gap_lost_count",
            "sequence_min",
            "sequence_max",
            "sequences_contiguous",
            "derived_replay_network_fallback_count",
            "derived_required_streams_full_fidelity",
            "derived_certification_blockers",
        )
    }


@dataclass(frozen=True)
class CaptureSealBinding:
    """Exact durable seal named by a verified, exact-run store load.

    The private token intentionally prevents ordinary callers from converting a
    hand-built logical manifest into certification evidence.  A binding is only
    emitted by :meth:`VerifiedReplayCapture.load_sealed_run` and the manifest
    builder below commits every caller-facing typed view back to sealed bytes.
    """

    identity_sha256: str
    expected_final_seal_sha256: str
    final_seal_sha256: str
    seal_content_root_sha256: str
    close_proof_sha256: str
    event_accumulator_sha256: str
    gap_accumulator_sha256: str
    event_inventory_sha256: str
    gap_inventory_sha256: str
    control_evidence_sha256: str
    resource_measurement_sha256: str | None
    resource_policy_sha256: str | None
    resource_budget_sha256: str | None
    resource_binding_sha256: str | None
    event_count: int
    gap_count: int
    gap_lost_count: int
    sequence_min: int | None
    sequence_max: int | None
    sequences_contiguous: bool
    derived_replay_network_fallback_count: int
    derived_required_streams_full_fidelity: bool
    derived_certification_blockers: tuple[str, ...]
    _verification_token: object = field(repr=False, compare=False)
    _attestation_sha256: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_token is not _VERIFIED_REPLAY_CAPTURE_TOKEN:
            raise CaptureContractError(
                "capture seal binding must come from an exact verified store load"
            )
        for name in (
            "identity_sha256",
            "expected_final_seal_sha256",
            "final_seal_sha256",
            "seal_content_root_sha256",
            "close_proof_sha256",
            "event_accumulator_sha256",
            "gap_accumulator_sha256",
            "event_inventory_sha256",
            "gap_inventory_sha256",
            "control_evidence_sha256",
        ):
            object.__setattr__(
                self, name, _require_sha256(getattr(self, name), name)
            )
        if self.expected_final_seal_sha256 != self.final_seal_sha256:
            raise CaptureContractError("loaded capture seal does not match expected SHA")
        resource_hashes = _validated_optional_resource_hashes(
            self.__dict__, description="binding"
        )
        if resource_hashes is not None:
            for field_name, key in zip(
                _RESOURCE_HASH_FIELD_NAMES,
                (
                    "measurement_sha256",
                    "policy_sha256",
                    "budget_sha256",
                    "binding_sha256",
                ),
            ):
                object.__setattr__(self, field_name, resource_hashes[key])
        for name in ("event_count", "gap_count", "gap_lost_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) < 0:
                raise CaptureContractError(f"binding {name} cannot be negative")
            object.__setattr__(self, name, int(value))
        if type(self.sequences_contiguous) is not bool:
            raise CaptureContractError("binding sequences_contiguous must be boolean")
        if (
            isinstance(self.derived_replay_network_fallback_count, bool)
            or int(self.derived_replay_network_fallback_count) < 0
        ):
            raise CaptureContractError(
                "binding derived network fallback count cannot be negative"
            )
        object.__setattr__(
            self,
            "derived_replay_network_fallback_count",
            int(self.derived_replay_network_fallback_count),
        )
        if type(self.derived_required_streams_full_fidelity) is not bool:
            raise CaptureContractError(
                "binding derived full-fidelity flag must be boolean"
            )
        blockers = tuple(
            str(value or "").strip()
            for value in self.derived_certification_blockers
        )
        if any(not value for value in blockers) or len(blockers) != len(set(blockers)):
            raise CaptureContractError("binding certification blockers are invalid")
        object.__setattr__(
            self, "derived_certification_blockers", tuple(sorted(blockers))
        )
        if self.event_count:
            if self.sequence_min is None or self.sequence_max is None:
                raise CaptureContractError("non-empty binding needs sequence bounds")
            if int(self.sequence_min) <= 0 or int(self.sequence_max) < int(
                self.sequence_min
            ):
                raise CaptureContractError("binding sequence bounds are invalid")
            object.__setattr__(self, "sequence_min", int(self.sequence_min))
            object.__setattr__(self, "sequence_max", int(self.sequence_max))
        elif self.sequence_min is not None or self.sequence_max is not None:
            raise CaptureContractError("empty binding cannot have sequence bounds")
        expected_attestation = _verification_attestation_sha256(
            _seal_binding_attestation_payload(self.__dict__)
        )
        if not hmac.compare_digest(
            _require_sha256(self._attestation_sha256, "binding attestation"),
            expected_attestation,
        ):
            raise CaptureContractError("capture seal binding attestation mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity_sha256": self.identity_sha256,
            "expected_final_seal_sha256": self.expected_final_seal_sha256,
            "final_seal_sha256": self.final_seal_sha256,
            "seal_content_root_sha256": self.seal_content_root_sha256,
            "close_proof_sha256": self.close_proof_sha256,
            "event_accumulator_sha256": self.event_accumulator_sha256,
            "gap_accumulator_sha256": self.gap_accumulator_sha256,
            "event_inventory_sha256": self.event_inventory_sha256,
            "gap_inventory_sha256": self.gap_inventory_sha256,
            "control_evidence_sha256": self.control_evidence_sha256,
            "resource_hashes": self.resource_hashes,
            "event_count": self.event_count,
            "gap_count": self.gap_count,
            "gap_lost_count": self.gap_lost_count,
            "sequence_min": self.sequence_min,
            "sequence_max": self.sequence_max,
            "sequences_contiguous": self.sequences_contiguous,
            "derived_replay_network_fallback_count": (
                self.derived_replay_network_fallback_count
            ),
            "derived_required_streams_full_fidelity": (
                self.derived_required_streams_full_fidelity
            ),
            "derived_certification_blockers": list(
                self.derived_certification_blockers
            ),
        }

    @property
    def resource_hashes(self) -> dict[str, str] | None:
        return _validated_optional_resource_hashes(
            self.__dict__, description="binding"
        )


def _verified_capture_attestation_payload(values: Mapping[str, Any]) -> dict[str, Any]:
    identity = values["identity"]
    if not isinstance(identity, CaptureRunIdentity):
        raise CaptureContractError("verified capture identity is malformed")
    return {
        "identity_sha256": identity.identity_sha256,
        "expected_final_seal_sha256": values["expected_final_seal_sha256"],
        "final_seal_sha256": values["final_seal_sha256"],
        "seal_content_root_sha256": values["seal_content_root_sha256"],
        "close_proof_sha256": values["close_proof_sha256"],
        "event_accumulator_sha256": values["event_accumulator_sha256"],
        "gap_accumulator_sha256": values["gap_accumulator_sha256"],
        **{name: values[name] for name in _RESOURCE_HASH_FIELD_NAMES},
        "event_count": values["event_count"],
        "gap_count": values["gap_count"],
        "sequence_min": values["sequence_min"],
        "sequence_max": values["sequence_max"],
        "gap_lost_count": values["gap_lost_count"],
    }


@dataclass(frozen=True)
class VerifiedReplayCapture:
    """One exact durable run, loaded by identity and caller-pinned final seal SHA."""

    identity: CaptureRunIdentity
    events: tuple[CaptureEvent, ...]
    gaps: tuple[CoverageGap, ...]
    expected_final_seal_sha256: str
    final_seal_sha256: str
    seal_content_root_sha256: str
    close_proof_sha256: str
    event_accumulator_sha256: str
    gap_accumulator_sha256: str
    resource_measurement_sha256: str | None
    resource_policy_sha256: str | None
    resource_budget_sha256: str | None
    resource_binding_sha256: str | None
    event_count: int
    gap_count: int
    sequence_min: int | None
    sequence_max: int | None
    gap_lost_count: int
    _verification_token: object = field(repr=False, compare=False)
    _attestation_sha256: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_token is not _VERIFIED_REPLAY_CAPTURE_TOKEN:
            raise CaptureContractError(
                "VerifiedReplayCapture must be produced by load_sealed_run"
            )
        if not isinstance(self.identity, CaptureRunIdentity):
            raise CaptureContractError("verified capture identity is malformed")
        for name in (
            "expected_final_seal_sha256",
            "final_seal_sha256",
            "seal_content_root_sha256",
            "close_proof_sha256",
            "event_accumulator_sha256",
            "gap_accumulator_sha256",
        ):
            object.__setattr__(
                self, name, _require_sha256(getattr(self, name), name)
            )
        if self.expected_final_seal_sha256 != self.final_seal_sha256:
            raise CaptureContractError("loaded capture seal does not match expected SHA")
        resource_hashes = _validated_optional_resource_hashes(
            self.__dict__, description="verified capture"
        )
        if resource_hashes is not None:
            for field_name, key in zip(
                _RESOURCE_HASH_FIELD_NAMES,
                (
                    "measurement_sha256",
                    "policy_sha256",
                    "budget_sha256",
                    "binding_sha256",
                ),
            ):
                object.__setattr__(self, field_name, resource_hashes[key])
        for name in ("event_count", "gap_count", "gap_lost_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) < 0:
                raise CaptureContractError(f"verified capture {name} is invalid")
            object.__setattr__(self, name, int(value))
        events = tuple(self.events)
        gaps = tuple(self.gaps)
        if any(not isinstance(event, CaptureEvent) for event in events):
            raise CaptureContractError("verified capture event inventory is malformed")
        if any(not isinstance(gap, CoverageGap) for gap in gaps):
            raise CaptureContractError("verified capture gap inventory is malformed")
        if self.event_count != len(events) or self.gap_count != len(gaps):
            raise CaptureContractError("verified capture inventory count mismatch")
        if any(event.identity != self.identity for event in events):
            raise CaptureContractError("verified event escaped exact run identity")
        sequences = [event.sequence for event in events]
        if len(sequences) != len(set(sequences)):
            raise CaptureContractError("verified event inventory has duplicate sequences")
        if (
            (min(sequences) if sequences else None) != self.sequence_min
            or (max(sequences) if sequences else None) != self.sequence_max
        ):
            raise CaptureContractError("verified event inventory sequence bounds mismatch")
        if sum(gap.lost_count for gap in gaps) != self.gap_lost_count:
            raise CaptureContractError("verified gap inventory loss count mismatch")
        event_accumulator = _inventory_accumulator_sha256(
            {
                "sequence": event.sequence,
                "event_sha256": event.event_sha256,
            }
            for event in events
        )
        gap_accumulator = _inventory_accumulator_sha256(
            {"identity": self.identity.to_dict(), "gap": gap.to_dict()}
            for gap in gaps
        )
        if event_accumulator != self.event_accumulator_sha256:
            raise CaptureContractError("verified event inventory accumulator mismatch")
        if gap_accumulator != self.gap_accumulator_sha256:
            raise CaptureContractError("verified gap inventory accumulator mismatch")
        expected_attestation = _verification_attestation_sha256(
            _verified_capture_attestation_payload(self.__dict__)
        )
        if not hmac.compare_digest(
            _require_sha256(self._attestation_sha256, "capture attestation"),
            expected_attestation,
        ):
            raise CaptureContractError("verified capture attestation mismatch")
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "gaps", gaps)

    @property
    def resource_hashes(self) -> dict[str, str] | None:
        return _validated_optional_resource_hashes(
            self.__dict__, description="verified capture"
        )

    @classmethod
    def load_sealed_run(
        cls,
        store: Any,
        identity: CaptureRunIdentity,
        *,
        expected_final_seal_sha256: str,
    ) -> "VerifiedReplayCapture":
        """Load exactly one run and pin it to an out-of-band expected seal SHA.

        There is deliberately no broad scan or "latest seal" fallback here.
        The runtime loader re-hashes every listed object; this wrapper then
        reconciles the typed event/gap inventory to that exact final seal.
        """

        expected = _require_sha256(
            expected_final_seal_sha256, "expected_final_seal_sha256"
        )
        if not isinstance(identity, CaptureRunIdentity):
            raise CaptureContractError("verified load requires exact run identity")
        try:
            from .replay_capture_runtime import (
                ContentAddressedCaptureStore,
                ReadOnlyV4CaptureStore,
                SealedCaptureRun,
            )
        except ImportError as exc:  # pragma: no cover - packaging failure
            raise CaptureContractError("capture runtime loader is unavailable") from exc
        if type(store) not in (
            ContentAddressedCaptureStore,
            ReadOnlyV4CaptureStore,
        ):
            raise CaptureContractError(
                "verified load requires an exact store-verified mutable or "
                "read-only content-addressed loader"
            )
        loaded = store.load_sealed_run(
            identity, expected_seal_sha256=expected
        )
        if not isinstance(loaded, SealedCaptureRun):
            raise CaptureContractError("store did not return a SealedCaptureRun")
        seal = loaded.seal
        if seal.seal_sha256 != expected:
            raise CaptureContractError("loaded capture seal does not match expected SHA")
        if seal.identity != identity:
            raise CaptureContractError("loaded capture identity mismatch")

        events = tuple(loaded.events)
        gaps_with_identity = tuple(loaded.gaps)
        if any(event.identity != identity for event in events):
            raise CaptureContractError("sealed event escaped exact run identity")
        if any(row_identity != identity for row_identity, _gap in gaps_with_identity):
            raise CaptureContractError("sealed gap escaped exact run identity")
        gaps = tuple(gap for _row_identity, gap in gaps_with_identity)
        sequences = [event.sequence for event in events]
        if len(sequences) != len(set(sequences)):
            raise CaptureContractError("sealed event inventory has duplicate sequences")
        if len(events) != seal.event_count or len(gaps) != seal.gap_count:
            raise CaptureContractError("sealed typed inventory count mismatch")
        if sum(gap.lost_count for gap in gaps) != seal.gap_lost_count:
            raise CaptureContractError("sealed typed gap loss count mismatch")
        if (
            (min(sequences) if sequences else None) != seal.sequence_min
            or (max(sequences) if sequences else None) != seal.sequence_max
        ):
            raise CaptureContractError("sealed typed sequence bounds mismatch")
        event_accumulator = _inventory_accumulator_sha256(
            {
                "sequence": event.sequence,
                "event_sha256": event.event_sha256,
            }
            for event in events
        )
        gap_accumulator = _inventory_accumulator_sha256(
            {"identity": identity.to_dict(), "gap": gap.to_dict()} for gap in gaps
        )
        if event_accumulator != seal.event_accumulator_sha256:
            raise CaptureContractError("sealed typed event accumulator mismatch")
        if gap_accumulator != seal.gap_accumulator_sha256:
            raise CaptureContractError("sealed typed gap accumulator mismatch")
        verified_values = dict(
            identity=identity,
            events=events,
            gaps=gaps,
            expected_final_seal_sha256=expected,
            final_seal_sha256=seal.seal_sha256,
            seal_content_root_sha256=seal.content_root_sha256,
            close_proof_sha256=seal.close_proof_sha256,
            event_accumulator_sha256=seal.event_accumulator_sha256,
            gap_accumulator_sha256=seal.gap_accumulator_sha256,
            resource_measurement_sha256=seal.resource_measurement_sha256,
            resource_policy_sha256=seal.resource_policy_sha256,
            resource_budget_sha256=seal.resource_budget_sha256,
            resource_binding_sha256=seal.resource_binding_sha256,
            event_count=seal.event_count,
            gap_count=seal.gap_count,
            sequence_min=seal.sequence_min,
            sequence_max=seal.sequence_max,
            gap_lost_count=seal.gap_lost_count,
        )
        return cls(
            **verified_values,
            _verification_token=_VERIFIED_REPLAY_CAPTURE_TOKEN,
            _attestation_sha256=_verification_attestation_sha256(
                _verified_capture_attestation_payload(verified_values)
            ),
        )


def _manifest_control_evidence_sha256(
    *,
    checkpoints: Iterable[CaptureDecisionCheckpoint],
    coverage: Mapping[CaptureStream, StreamCoverage],
    receipts: Iterable[CaptureReadReceipt],
    resource_hashes: Mapping[str, str] | None,
) -> str:
    return sha256_json(
        {
            "decision_checkpoints": [
                row.to_dict()
                for row in sorted(checkpoints, key=lambda item: item.checkpoint_sha256)
            ],
            "stream_coverage": {
                stream.value: value.to_dict()
                for stream, value in sorted(
                    coverage.items(), key=lambda item: item[0].value
                )
            },
            "read_receipts": [
                row.to_dict() for row in sorted(receipts, key=lambda item: item.read_id)
            ],
            "resource_hashes": (
                dict(sorted(resource_hashes.items()))
                if resource_hashes is not None
                else None
            ),
        }
    )


@dataclass(frozen=True)
class CaptureCoverageManifest:
    identity: CaptureRunIdentity
    event_index: Mapping[str, CaptureEventRef]
    decision_checkpoints: tuple[CaptureDecisionCheckpoint, ...]
    stream_coverage: Mapping[CaptureStream, StreamCoverage]
    read_receipts: tuple[CaptureReadReceipt, ...]
    gaps: tuple[CoverageGap, ...]
    closed_cleanly: bool
    content_root_verified: bool
    replay_network_fallback_count: int
    required_streams_full_fidelity: bool
    created_at: datetime
    seal_binding: CaptureSealBinding | None = None
    certification_blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.identity, CaptureRunIdentity):
            raise CaptureContractError("manifest identity must be a CaptureRunIdentity")
        identity_sha256 = self.identity.identity_sha256
        event_index: dict[str, CaptureEventRef] = {}
        seen_sequences: set[int] = set()
        for key, ref in self.event_index.items():
            if not isinstance(ref, CaptureEventRef):
                raise CaptureContractError("event_index values must be CaptureEventRef")
            event_sha256 = _require_sha256(str(key), "event_index key")
            if ref.event_sha256 != event_sha256:
                raise CaptureContractError("event_index key/value mismatch")
            if ref.identity_sha256 != identity_sha256:
                raise CaptureContractError("event_index identity mismatch")
            if ref.sequence in seen_sequences:
                raise CaptureContractError("event_index contains duplicate sequences")
            seen_sequences.add(ref.sequence)
            event_index[event_sha256] = ref
        ordered_refs = sorted(
            event_index.values(), key=lambda row: (row.sequence, row.event_sha256)
        )
        if any(
            later.available_at < earlier.available_at
            for earlier, later in zip(ordered_refs, ordered_refs[1:])
        ):
            raise CaptureContractError(
                "event sequence moves backwards on the availability clock"
            )
        object.__setattr__(self, "event_index", _FrozenJsonDict(event_index))

        checkpoints = tuple(self.decision_checkpoints)
        checkpoint_hashes: set[str] = set()
        checkpoint_decisions: set[str] = set()
        for checkpoint in checkpoints:
            if not isinstance(checkpoint, CaptureDecisionCheckpoint):
                raise CaptureContractError(
                    "decision_checkpoints must contain CaptureDecisionCheckpoint"
                )
            if checkpoint.identity_sha256 != identity_sha256:
                raise CaptureContractError("checkpoint identity mismatch")
            if checkpoint.checkpoint_sha256 in checkpoint_hashes:
                raise CaptureContractError("duplicate decision checkpoint")
            if checkpoint.decision_id in checkpoint_decisions:
                raise CaptureContractError("duplicate checkpoint decision id")
            checkpoint_hashes.add(checkpoint.checkpoint_sha256)
            checkpoint_decisions.add(checkpoint.decision_id)
            decision_ref = event_index.get(checkpoint.decision_event_sha256)
            if decision_ref is None:
                raise CaptureContractError("checkpoint decision event is missing")
            if (
                decision_ref.stream is not CaptureStream.FSM_DECISION
                or decision_ref.symbol != checkpoint.symbol
                or decision_ref.available_at != checkpoint.available_at
                or decision_ref.payload_sha256
                != sha256_json(checkpoint.decision_payload)
                or decision_ref.sequence <= checkpoint.input_prefix_sequence
            ):
                raise CaptureContractError("checkpoint decision event does not match")
            expected_prefix = capture_prefix_root_sha256(
                event_index.values(),
                identity_sha256=identity_sha256,
                through_sequence=checkpoint.input_prefix_sequence,
            )
            if expected_prefix != checkpoint.input_prefix_root_sha256:
                raise CaptureContractError("checkpoint input prefix root mismatch")
        object.__setattr__(
            self,
            "decision_checkpoints",
            tuple(sorted(checkpoints, key=lambda row: row.checkpoint_sha256)),
        )

        normalized: dict[CaptureStream, StreamCoverage] = {}
        for key, value in self.stream_coverage.items():
            stream = key if isinstance(key, CaptureStream) else CaptureStream(str(key))
            if not isinstance(value, StreamCoverage):
                raise CaptureContractError("stream coverage values must be StreamCoverage")
            if value.stream is not stream:
                raise CaptureContractError("stream coverage key/value mismatch")
            if value.identity_sha256 != identity_sha256:
                raise CaptureContractError("stream coverage identity mismatch")
            normalized[stream] = value
        object.__setattr__(self, "stream_coverage", _FrozenJsonDict(normalized))

        receipts = tuple(self.read_receipts)
        if any(not isinstance(receipt, CaptureReadReceipt) for receipt in receipts):
            raise CaptureContractError("read_receipts must contain CaptureReadReceipt")
        read_ids = [receipt.read_id for receipt in receipts]
        if len(read_ids) != len(set(read_ids)):
            raise CaptureContractError("duplicate read receipt id")
        if any(receipt.identity_sha256 != identity_sha256 for receipt in receipts):
            raise CaptureContractError("read receipt identity mismatch")
        object.__setattr__(
            self, "read_receipts", tuple(sorted(receipts, key=lambda row: row.read_id))
        )

        gaps = tuple(self.gaps)
        if any(not isinstance(gap, CoverageGap) for gap in gaps):
            raise CaptureContractError("gaps must contain CoverageGap")
        object.__setattr__(
            self,
            "gaps",
            tuple(
                sorted(
                    gaps,
                    key=lambda gap: (
                        gap.first_available_at,
                        gap.last_available_at,
                        gap.stream.value,
                        gap.symbol or "",
                        gap.reason,
                        gap.lost_count,
                    ),
                )
            ),
        )

        for name in (
            "closed_cleanly",
            "content_root_verified",
            "required_streams_full_fidelity",
        ):
            if type(getattr(self, name)) is not bool:
                raise CaptureContractError(f"{name} must be boolean")
        created_at = _utc(self.created_at, "created_at")
        object.__setattr__(self, "created_at", created_at)
        if (
            isinstance(self.replay_network_fallback_count, bool)
            or int(self.replay_network_fallback_count) < 0
        ):
            raise CaptureContractError("network fallback count cannot be negative")
        object.__setattr__(
            self,
            "replay_network_fallback_count",
            int(self.replay_network_fallback_count),
        )
        evidence_times = [coverage.last_available_at for coverage in normalized.values()]
        evidence_times.extend(receipt.returned_at for receipt in receipts)
        evidence_times.extend(gap.last_available_at for gap in gaps)
        evidence_times.extend(
            coverage.watermark.emitted_available_at
            for coverage in normalized.values()
            if coverage.watermark is not None
        )
        if evidence_times and created_at < max(evidence_times):
            raise CaptureContractError("manifest created_at precedes captured evidence")

        blockers = tuple(str(value or "").strip() for value in self.certification_blockers)
        if any(not value for value in blockers):
            raise CaptureContractError("certification blockers cannot be empty")
        if len(blockers) != len(set(blockers)):
            raise CaptureContractError("certification blockers cannot be duplicated")
        object.__setattr__(self, "certification_blockers", tuple(sorted(blockers)))

        binding = self.seal_binding
        if binding is not None:
            if not isinstance(binding, CaptureSealBinding):
                raise CaptureContractError("seal_binding must be CaptureSealBinding")
            if binding._verification_token is not _VERIFIED_REPLAY_CAPTURE_TOKEN:
                raise CaptureContractError("manifest seal binding is not verified")
            if binding.identity_sha256 != identity_sha256:
                raise CaptureContractError("manifest/seal identity mismatch")
            if binding.event_count != len(event_index):
                raise CaptureContractError("manifest/seal event count mismatch")
            if binding.gap_count != len(gaps):
                raise CaptureContractError("manifest/seal gap count mismatch")
            if binding.gap_lost_count != sum(gap.lost_count for gap in gaps):
                raise CaptureContractError("manifest/seal gap loss mismatch")
            sequences = sorted(ref.sequence for ref in event_index.values())
            if (
                (min(sequences) if sequences else None) != binding.sequence_min
                or (max(sequences) if sequences else None) != binding.sequence_max
            ):
                raise CaptureContractError("manifest/seal sequence bounds mismatch")
            contiguous = bool(sequences) and sequences == list(
                range(sequences[0], sequences[-1] + 1)
            )
            if not sequences:
                contiguous = True
            if contiguous != binding.sequences_contiguous:
                raise CaptureContractError("manifest/seal sequence continuity mismatch")
            if _event_inventory_sha256(event_index.values()) != binding.event_inventory_sha256:
                raise CaptureContractError("manifest/seal event inventory mismatch")
            if _gap_inventory_sha256(self.identity, gaps) != binding.gap_inventory_sha256:
                raise CaptureContractError("manifest/seal gap inventory mismatch")
            control_root = _manifest_control_evidence_sha256(
                checkpoints=checkpoints,
                coverage=normalized,
                receipts=receipts,
                resource_hashes=binding.resource_hashes,
            )
            if control_root != binding.control_evidence_sha256:
                raise CaptureContractError("manifest/seal control evidence mismatch")
            if not self.closed_cleanly or not self.content_root_verified:
                raise CaptureContractError(
                    "a verified sealed manifest cannot downgrade durable integrity flags"
                )
            if (
                self.replay_network_fallback_count
                != binding.derived_replay_network_fallback_count
            ):
                raise CaptureContractError(
                    "manifest/seal derived network fallback count mismatch"
                )
            if (
                self.required_streams_full_fidelity
                != binding.derived_required_streams_full_fidelity
            ):
                raise CaptureContractError(
                    "manifest/seal derived full-fidelity flag mismatch"
                )
            if (
                self.certification_blockers
                != binding.derived_certification_blockers
            ):
                raise CaptureContractError(
                    "manifest/seal derived certification blockers mismatch"
                )

    @classmethod
    def from_verified_capture(
        cls,
        verified: VerifiedReplayCapture,
        *,
        decision_checkpoints: Iterable[CaptureDecisionCheckpoint],
        stream_coverage: Mapping[CaptureStream, StreamCoverage],
        read_receipts: Iterable[CaptureReadReceipt],
    ) -> "CaptureCoverageManifest":
        """Build certification evidence exclusively against sealed event bytes.

        The typed views remain convenient for callers, but each checkpoint,
        read receipt, provider watermark, and stream-health assertion must have
        an exact payload committed as the corresponding sealed control event.
        Events and gaps themselves are never accepted from the caller.
        """

        if not isinstance(verified, VerifiedReplayCapture) or (
            verified._verification_token is not _VERIFIED_REPLAY_CAPTURE_TOKEN
        ):
            raise CaptureContractError("manifest builder requires verified capture")
        identity = verified.identity
        events = tuple(verified.events)
        refs = tuple(CaptureEventRef.from_event(event) for event in events)
        event_index = {ref.event_sha256: ref for ref in refs}
        if len(event_index) != len(refs):
            raise CaptureContractError("sealed capture contains duplicate event hashes")

        checkpoints = tuple(decision_checkpoints)
        receipts = tuple(read_receipts)
        coverage: dict[CaptureStream, StreamCoverage] = {}
        for key, value in stream_coverage.items():
            stream = key if isinstance(key, CaptureStream) else CaptureStream(str(key))
            coverage[stream] = value

        events_by_stream: dict[CaptureStream, list[CaptureEvent]] = {}
        for event in events:
            events_by_stream.setdefault(event.stream, []).append(event)

        def require_control_payload(
            stream: CaptureStream,
            payload: Mapping[str, Any],
            *,
            available_not_before: datetime | None = None,
            available_not_after: datetime | None = None,
            sequence_at_most: int | None = None,
            description: str,
        ) -> CaptureEvent:
            payload_sha256 = sha256_json(payload)
            candidates = [
                event
                for event in events_by_stream.get(stream, ())
                if event.payload_sha256 == payload_sha256
                and (
                    available_not_before is None
                    or event.clocks.available_at >= available_not_before
                )
                and (
                    available_not_after is None
                    or event.clocks.available_at <= available_not_after
                )
                and (
                    sequence_at_most is None
                    or event.sequence <= sequence_at_most
                )
            ]
            if not candidates:
                raise CaptureContractError(
                    f"{description} is not committed by a sealed {stream.value} event"
                )
            return min(candidates, key=lambda event: event.sequence)

        checkpoints_by_decision: dict[str, CaptureDecisionCheckpoint] = {}
        for checkpoint in checkpoints:
            if checkpoint.decision_id in checkpoints_by_decision:
                raise CaptureContractError("duplicate checkpoint decision id")
            checkpoints_by_decision[checkpoint.decision_id] = checkpoint

        # The ordinary checkpoint read set is the detector-time causal prefix.
        # A first-dip order candidate may perform one separately typed final
        # read after that detector invocation but before the FSM decision event
        # is durably published.  Admit only read IDs sealed inside the v3 final
        # frontier and its canonical dependency profile; the ReplayV3 loader
        # independently reconstructs every referenced byte before minting any
        # private authority.
        supplemental_read_boundaries: dict[
            str, dict[str, tuple[datetime, int]]
        ] = {}
        for checkpoint in checkpoints:
            raw_frontier = checkpoint.decision_payload.get(
                "first_dip_final_capture_frontier"
            )
            supplied_frontier_sha = checkpoint.decision_payload.get(
                "first_dip_final_capture_frontier_sha256"
            )
            if raw_frontier is None and supplied_frontier_sha is None:
                supplemental_read_boundaries[checkpoint.decision_id] = {}
                continue
            if not isinstance(raw_frontier, Mapping) or not isinstance(
                supplied_frontier_sha, str
            ):
                raise CaptureContractError(
                    "first-dip final frontier payload/hash is incomplete"
                )
            try:
                if (
                    raw_frontier.get("schema_version")
                    != FIRST_DIP_FINAL_CAPTURE_FRONTIER_SCHEMA_VERSION
                    or _require_sha256(
                        supplied_frontier_sha,
                        "first-dip final frontier digest",
                    )
                    != sha256_json(raw_frontier)
                ):
                    raise CaptureContractError(
                        "first-dip final frontier digest is invalid"
                    )
                profile_canonical = raw_frontier.get(
                    "dependency_profile_canonical_json"
                )
                if not isinstance(profile_canonical, str):
                    raise TypeError("final dependency profile is not canonical JSON")
                profile_raw = json.loads(profile_canonical)
                if not isinstance(profile_raw, Mapping):
                    raise TypeError("final dependency profile must be an object")
                profile = FSMDependencyProfile.from_dict(profile_raw)
                if (
                    canonical_json_bytes(profile.to_dict()).decode("utf-8")
                    != profile_canonical
                    or profile.profile_sha256
                    != _require_sha256(
                        str(raw_frontier.get("dependency_profile_sha256") or ""),
                        "first-dip final dependency profile digest",
                    )
                ):
                    raise CaptureContractError(
                        "first-dip final dependency profile changed"
                    )
                raw_read_ids = raw_frontier.get("required_read_ids")
                if not isinstance(raw_read_ids, (list, tuple)):
                    raise TypeError("final required read IDs must be an array")
                read_ids = tuple(
                    sorted(
                        _uuid_text(value, "first-dip final required read ID")
                        for value in raw_read_ids
                    )
                )
                tape_read_id = _uuid_text(
                    raw_frontier.get("first_dip_tape_read_id"),
                    "first-dip final tape read ID",
                )
                attested_at = _parse_utc(
                    raw_frontier.get("attested_available_at"),
                    "first-dip final attested_available_at",
                )
                final_boundary_at = _parse_utc(
                    raw_frontier.get("final_boundary_available_at"),
                    "first-dip final boundary_available_at",
                )
                expires_at = _parse_utc(
                    raw_frontier.get("expires_at"),
                    "first-dip final expires_at",
                )
                prefix_sequence_raw = raw_frontier.get("input_prefix_sequence")
                if isinstance(prefix_sequence_raw, bool):
                    raise TypeError("final prefix sequence cannot be boolean")
                prefix_sequence = int(prefix_sequence_raw)
                prefix_root = _require_sha256(
                    str(raw_frontier.get("input_prefix_root_sha256") or ""),
                    "first-dip final prefix root",
                )
            except (
                CaptureContractError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                raise CaptureContractError(
                    "first-dip final frontier supplemental read contract is invalid"
                ) from exc

            decision_ref = event_index.get(checkpoint.decision_event_sha256)
            try:
                expected_prefix_root = capture_prefix_root_sha256(
                    refs,
                    identity_sha256=identity.identity_sha256,
                    through_sequence=prefix_sequence,
                )
            except CaptureContractError as exc:
                raise CaptureContractError(
                    "first-dip final frontier prefix is incomplete"
                ) from exc
            prefix_refs = tuple(
                ref for ref in refs if ref.sequence <= prefix_sequence
            )
            if (
                not read_ids
                or len(read_ids) != len(set(read_ids))
                or profile.required_read_ids != read_ids
                or tape_read_id not in read_ids
                or set(read_ids).intersection(checkpoint.required_read_ids)
                or str(raw_frontier.get("run_id") or "").strip()
                != identity.run_id
                or raw_frontier.get("generation") != identity.generation
                or str(raw_frontier.get("identity_sha256") or "").strip().lower()
                != identity.identity_sha256
                or str(raw_frontier.get("decision_id") or "").strip()
                != checkpoint.decision_id
                or decision_ref is None
                or not (
                    checkpoint.input_prefix_sequence
                    < prefix_sequence
                    < decision_ref.sequence
                )
                or prefix_root != expected_prefix_root
                or not prefix_refs
                or max(ref.available_at for ref in prefix_refs) > attested_at
                or not (
                    checkpoint.decision_at
                    <= attested_at
                    <= final_boundary_at
                    <= checkpoint.available_at
                    <= expires_at
                )
            ):
                raise CaptureContractError(
                    "first-dip final frontier escaped its checkpoint boundary"
                )
            supplemental_read_boundaries[checkpoint.decision_id] = {
                read_id: (attested_at, prefix_sequence) for read_id in read_ids
            }

        receipts_by_decision: dict[str, set[str]] = {}
        supplemental_receipts_by_decision: dict[str, set[str]] = {}
        for receipt in receipts:
            if not isinstance(receipt, CaptureReadReceipt):
                raise CaptureContractError(
                    "read_receipts must contain CaptureReadReceipt"
                )
            checkpoint = checkpoints_by_decision.get(receipt.decision_id)
            if checkpoint is None:
                raise CaptureContractError(
                    f"read receipt {receipt.read_id} has no decision checkpoint"
                )
            supplemental_boundary = supplemental_read_boundaries.get(
                receipt.decision_id, {}
            ).get(receipt.read_id)
            if (
                receipt.read_id not in checkpoint.required_read_ids
                and supplemental_boundary is None
            ):
                raise CaptureContractError(
                    f"read receipt {receipt.read_id} is not required by its checkpoint"
                )
            if receipt.read_id in checkpoint.required_read_ids and (
                receipt.returned_at > checkpoint.decision_at
            ):
                raise CaptureContractError(
                    f"read receipt {receipt.read_id} returned after its decision"
                )
            if supplemental_boundary is not None and (
                receipt.returned_at > supplemental_boundary[0]
            ):
                raise CaptureContractError(
                    f"supplemental read receipt {receipt.read_id} escaped its final frontier"
                )
            receipt_sets = (
                supplemental_receipts_by_decision
                if supplemental_boundary is not None
                else receipts_by_decision
            )
            decision_receipts = receipt_sets.setdefault(receipt.decision_id, set())
            if receipt.read_id in decision_receipts:
                raise CaptureContractError("duplicate read receipt id")
            decision_receipts.add(receipt.read_id)

        for checkpoint in checkpoints:
            if receipts_by_decision.get(checkpoint.decision_id, set()) != set(
                checkpoint.required_read_ids
            ):
                raise CaptureContractError(
                    f"decision checkpoint {checkpoint.decision_id} is missing its exact read receipts"
                )
            if supplemental_receipts_by_decision.get(
                checkpoint.decision_id, set()
            ) != set(supplemental_read_boundaries.get(checkpoint.decision_id, {})):
                raise CaptureContractError(
                    f"decision checkpoint {checkpoint.decision_id} is missing its exact supplemental read receipts"
                )

        for checkpoint in checkpoints:
            dependency_profile = checkpoint.decision_payload.get(
                "fsm_dependency_profile"
            )
            if not isinstance(dependency_profile, Mapping):
                raise CaptureContractError(
                    "sealed FSM decision is missing its dependency profile"
                )
            try:
                typed_profile = FSMDependencyProfile.from_dict(dependency_profile)
            except (CaptureContractError, TypeError, ValueError) as exc:
                raise CaptureContractError(
                    "sealed FSM dependency profile is malformed or unsupported"
                ) from exc
            if (
                typed_profile.required_read_ids != checkpoint.required_read_ids
            ):
                raise CaptureContractError(
                    "sealed FSM dependency profile does not match checkpoint reads"
                )
            require_control_payload(
                CaptureStream.FSM_DECISION,
                checkpoint.decision_payload,
                available_not_before=checkpoint.available_at,
                description=f"decision checkpoint {checkpoint.decision_id}",
            )
        for receipt in receipts:
            checkpoint = checkpoints_by_decision[receipt.decision_id]
            supplemental_boundary = supplemental_read_boundaries.get(
                receipt.decision_id, {}
            ).get(receipt.read_id)
            require_control_payload(
                CaptureStream.READ_RECEIPT,
                receipt.to_dict(),
                available_not_before=receipt.returned_at,
                available_not_after=(
                    checkpoint.available_at
                    if supplemental_boundary is None
                    else supplemental_boundary[0]
                ),
                sequence_at_most=(
                    checkpoint.input_prefix_sequence
                    if supplemental_boundary is None
                    else supplemental_boundary[1]
                ),
                description=f"read receipt {receipt.read_id}",
            )
        coverage_health_events: dict[CaptureStream, CaptureEvent] = {}
        watermark_events: dict[CaptureStream, CaptureEvent] = {}
        for stream, value in coverage.items():
            coverage_health_events[stream] = require_control_payload(
                CaptureStream.CAPTURE_HEALTH,
                value.to_dict(),
                available_not_before=value.last_available_at,
                description=f"stream coverage {stream.value}",
            )
            if value.watermark is not None:
                watermark_events[stream] = require_control_payload(
                    CaptureStream.PROVIDER_WATERMARK,
                    value.watermark.to_dict(),
                    available_not_before=value.watermark.emitted_available_at,
                    description=f"provider watermark {stream.value}",
                )

        lifecycle_grade = grade_capture_producer_lifecycle(
            identity=identity,
            events=events,
            stream_coverage=coverage,
            coverage_health_events=coverage_health_events,
            watermark_events=watermark_events,
            resource_binding_sha256=verified.resource_binding_sha256,
        )
        producer_lifecycle_certified = lifecycle_grade.certified

        sequences = sorted(event.sequence for event in events)
        sequences_contiguous = (
            not sequences
            or sequences == list(range(sequences[0], sequences[-1] + 1))
        )
        event_refs = tuple(event_index.values())
        gap_rows = tuple(verified.gaps)
        control_root = _manifest_control_evidence_sha256(
            checkpoints=checkpoints,
            coverage=coverage,
            receipts=receipts,
            resource_hashes=verified.resource_hashes,
        )
        network_fallback_count = sum(
            int(receipt.replay_network_fallback_used) for receipt in receipts
        )
        stream_evidence_full_fidelity = all(
            value.content_verified
            and (
                not STREAM_POLICIES[stream].exact_provider_event_clock_required
                or value.exact_event_clock_complete
            )
            and (
                STREAM_POLICIES[stream].coverage_mode
                not in {CoverageMode.CONTINUOUS, CoverageMode.CHANGE_LOG}
                or value.continuity_complete
            )
            for stream, value in coverage.items()
        )
        full_fidelity = producer_lifecycle_certified and stream_evidence_full_fidelity
        blockers = tuple(
            sorted(
                (
                    *(
                        ("capture_resource_binding_unverified",)
                        if verified.resource_hashes is None
                        else ()
                    ),
                    *lifecycle_grade.reasons,
                )
            )
        )
        binding_values = dict(
            identity_sha256=identity.identity_sha256,
            expected_final_seal_sha256=verified.expected_final_seal_sha256,
            final_seal_sha256=verified.final_seal_sha256,
            seal_content_root_sha256=verified.seal_content_root_sha256,
            close_proof_sha256=verified.close_proof_sha256,
            event_accumulator_sha256=verified.event_accumulator_sha256,
            gap_accumulator_sha256=verified.gap_accumulator_sha256,
            event_inventory_sha256=_event_inventory_sha256(event_refs),
            gap_inventory_sha256=_gap_inventory_sha256(identity, gap_rows),
            control_evidence_sha256=control_root,
            resource_measurement_sha256=verified.resource_measurement_sha256,
            resource_policy_sha256=verified.resource_policy_sha256,
            resource_budget_sha256=verified.resource_budget_sha256,
            resource_binding_sha256=verified.resource_binding_sha256,
            event_count=len(events),
            gap_count=len(gap_rows),
            gap_lost_count=sum(gap.lost_count for gap in gap_rows),
            sequence_min=(min(sequences) if sequences else None),
            sequence_max=(max(sequences) if sequences else None),
            sequences_contiguous=sequences_contiguous,
            derived_replay_network_fallback_count=network_fallback_count,
            derived_required_streams_full_fidelity=full_fidelity,
            derived_certification_blockers=blockers,
        )
        binding = CaptureSealBinding(
            **binding_values,
            _verification_token=_VERIFIED_REPLAY_CAPTURE_TOKEN,
            _attestation_sha256=_verification_attestation_sha256(
                _seal_binding_attestation_payload(binding_values)
            ),
        )
        latest_event_at = max(
            (event.clocks.available_at for event in events), default=datetime.min.replace(tzinfo=UTC)
        )
        latest_gap_at = max(
            (gap.last_available_at for gap in gap_rows),
            default=datetime.min.replace(tzinfo=UTC),
        )
        return cls(
            identity=identity,
            event_index=event_index,
            decision_checkpoints=checkpoints,
            stream_coverage=coverage,
            read_receipts=receipts,
            gaps=gap_rows,
            closed_cleanly=True,
            content_root_verified=True,
            replay_network_fallback_count=network_fallback_count,
            required_streams_full_fidelity=full_fidelity,
            created_at=max(latest_event_at, latest_gap_at),
            seal_binding=binding,
            certification_blockers=blockers,
        )

    @property
    def content_root_sha256(self) -> str:
        """Logical root chained to the durable store seal by the manifest builder."""

        return sha256_json(
            {
                "identity_sha256": self.identity.identity_sha256,
                "events": [
                    ref.to_dict()
                    for ref in sorted(
                        self.event_index.values(),
                        key=lambda row: (row.sequence, row.event_sha256),
                    )
                ],
                "decision_checkpoints": [
                    checkpoint.to_dict()
                    for checkpoint in self.decision_checkpoints
                ],
                "gaps": [gap.to_dict() for gap in self.gaps],
            }
        )

    @property
    def manifest_sha256(self) -> str:
        return sha256_json(
            {
                "identity": self.identity.to_dict(),
                "content_root_sha256": self.content_root_sha256,
                "event_index": {
                    key: value.to_dict()
                    for key, value in sorted(self.event_index.items())
                },
                "decision_checkpoints": [
                    value.to_dict() for value in self.decision_checkpoints
                ],
                "stream_coverage": {
                    key.value: asdict(value)
                    for key, value in sorted(
                        self.stream_coverage.items(), key=lambda item: item[0].value
                    )
                },
                "read_receipts": [asdict(value) for value in self.read_receipts],
                "gaps": [value.to_dict() for value in self.gaps],
                "closed_cleanly": self.closed_cleanly,
                "content_root_verified": self.content_root_verified,
                "replay_network_fallback_count": self.replay_network_fallback_count,
                "required_streams_full_fidelity": self.required_streams_full_fidelity,
                "created_at": self.created_at,
                "seal_binding": (
                    self.seal_binding.to_dict()
                    if self.seal_binding is not None
                    else None
                ),
                "certification_blockers": list(self.certification_blockers),
            }
        )


@dataclass(frozen=True)
class CaptureCoverageGrade:
    replayable: bool
    grade: str
    reasons: tuple[str, ...]
    manifest_sha256: str


_ALWAYS_REQUIRED_IDENTITY_STREAMS = frozenset(
    {
        CaptureStream.CONFIG_SNAPSHOT,
        CaptureStream.FEATURE_FLAG_SNAPSHOT,
        CaptureStream.CODE_BUILD,
        CaptureStream.ACCOUNT_RISK_SNAPSHOT,
    }
)

_SYMBOL_SCOPED_STREAMS = frozenset(
    {
        CaptureStream.IQFEED_PRINT,
        CaptureStream.PROVIDER_TRADE_PRINT,
        CaptureStream.NBBO_QUOTE,
        CaptureStream.ALPACA_NBBO_QUOTE,
        CaptureStream.L2_DEPTH_DELTA,
        CaptureStream.L2_DEPTH_CHECKPOINT,
        CaptureStream.ORTEX_SNAPSHOT,
        CaptureStream.SCANNER_SNAPSHOT,
        CaptureStream.CATALYST_NEWS,
        CaptureStream.ADMISSION_ELIGIBILITY,
        CaptureStream.HALT_LULD_STATE,
        CaptureStream.SSR_STATE,
        CaptureStream.BROKER_ORDER_LIFECYCLE,
        CaptureStream.FSM_DECISION,
    }
)


def _coverage_source_clock(
    stream: CaptureStream,
    ref: CaptureEventRef,
    *,
    exact_required: bool | None = None,
    reference_required: bool | None = None,
) -> datetime | None:
    """Return the provider/market clock that proves source freshness.

    ``available_at`` is only the causal release clock.  It may be used as the
    source clock solely for streams whose policy is explicitly clock-agnostic;
    otherwise re-observing an old cached fact must not make it look fresh.
    """

    policy = STREAM_POLICIES[stream]
    exact = (
        policy.exact_provider_event_clock_required
        if exact_required is None
        else exact_required
    )
    reference = (
        policy.market_reference_clock_required
        if reference_required is None
        else reference_required
    )
    if exact:
        return ref.provider_event_at
    if reference:
        return ref.market_reference_at or ref.provider_event_at
    return ref.available_at


class _FirstDipReceiptVerificationError(CaptureContractError):
    """Stable fail-closed code plus a human-readable sealed-replay error."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def verify_first_dip_receipt_inventory(
    *,
    receipt: CaptureReadReceipt,
    checkpoint: CaptureDecisionCheckpoint,
    manifest: CaptureCoverageManifest,
) -> tuple[CaptureEventRef, ...]:
    """Rebuild a typed first-dip print read from the sealed event index.

    The receipt may state its event-time bounds, but it may not select the
    content addresses inside them.  The durable receipt commit and the typed
    source sequence frontier bind the complete causal prefix, including a
    legitimately empty decision window.  This helper intentionally performs no
    provider, database, broker, or wall-clock read.
    """

    if (
        not isinstance(receipt, CaptureReadReceipt)
        or not isinstance(checkpoint, CaptureDecisionCheckpoint)
        or not isinstance(manifest, CaptureCoverageManifest)
        or receipt.stream is not CaptureStream.IQFEED_PRINT
    ):
        raise _FirstDipReceiptVerificationError(
            "type_mismatch",
            "sealed ReplayV3 first-dip tape receipt is not typed",
        )
    first_dip_read_id = str(
        checkpoint.decision_payload.get("first_dip_tape_read_id") or ""
    ).strip()
    if (
        not first_dip_read_id
        or receipt.read_id != first_dip_read_id
        or receipt.read_id not in checkpoint.required_read_ids
        or receipt.decision_id != checkpoint.decision_id
        or receipt.identity_sha256 != checkpoint.identity_sha256
    ):
        raise _FirstDipReceiptVerificationError(
            "decision_binding_mismatch",
            "sealed ReplayV3 first-dip tape receipt escaped its exact decision",
        )
    if receipt.query is None:
        raise _FirstDipReceiptVerificationError(
            "typed_query_missing",
            "sealed ReplayV3 first-dip tape receipt lacks its typed query",
        )
    raw_policy = checkpoint.decision_payload.get("first_dip_tape_policy")
    raw_profile = checkpoint.decision_payload.get("fsm_dependency_profile")
    if not isinstance(raw_policy, Mapping):
        raise _FirstDipReceiptVerificationError(
            "policy_missing",
            "sealed ReplayV3 first-dip tape policy is missing",
        )
    if not isinstance(raw_profile, Mapping):
        raise _FirstDipReceiptVerificationError(
            "dependency_profile_missing",
            "sealed ReplayV3 first-dip dependency profile is missing",
        )

    # Deliberately lazy: first_dip_tape_policy imports this contract module.
    # Calling after module initialization avoids a circular import while still
    # using the one canonical typed query/policy parser.
    # 2026-07-17: dual-context (tingnan ang replay_capture_runtime header).
    try:
        from .first_dip_tape_policy import (  # noqa: PLC0415
            FirstDipTapePolicy,
            FirstDipTapeReadQuery,
            evaluate_first_dip_tape,
            first_dip_tape_window_from_capture,
        )
    except ImportError:  # sealed synthetic-package exec
        from app.services.trading.momentum_neural.first_dip_tape_policy import (  # noqa: PLC0415
            FirstDipTapePolicy,
            FirstDipTapeReadQuery,
            evaluate_first_dip_tape,
            first_dip_tape_window_from_capture,
        )

    try:
        query = FirstDipTapeReadQuery.from_dict(receipt.query)
        policy = FirstDipTapePolicy.from_dict(raw_policy)
        query.validate_for_policy(policy)
        dependency_profile = FSMDependencyProfile.from_dict(raw_profile)
        dependency = dependency_profile.dependency_for(
            CaptureStream.IQFEED_PRINT
        )
    except (CaptureContractError, TypeError, ValueError) as exc:
        raise _FirstDipReceiptVerificationError(
            "typed_contract_malformed",
            "sealed ReplayV3 first-dip tape query is malformed",
        ) from exc

    committed_policy_sha256 = str(
        checkpoint.decision_payload.get("first_dip_tape_policy_sha256") or ""
    ).strip().lower()
    if (
        query.symbol != checkpoint.symbol
        or query.symbol != receipt.symbol
        or query.provider != receipt.provider.strip().lower()
        or query.decision_at != checkpoint.decision_at
        or query.available_at_most != checkpoint.decision_at
        or receipt.returned_at != query.available_at_most
        or query.policy_sha256 != committed_policy_sha256
        or query.policy_sha256 != policy.policy_sha256
    ):
        raise _FirstDipReceiptVerificationError(
            "query_decision_mismatch",
            "sealed ReplayV3 first-dip tape query escaped its exact decision",
        )
    if (
        dependency.coverage_start_at > query.event_start_exclusive
        or not math.isclose(
            dependency.max_source_age_seconds,
            policy.max_source_age_seconds,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    ):
        raise _FirstDipReceiptVerificationError(
            "dependency_policy_mismatch",
            "sealed ReplayV3 first-dip dependency does not cover its typed window",
        )
    if query.source_frontier_sequence > checkpoint.input_prefix_sequence:
        raise _FirstDipReceiptVerificationError(
            "source_frontier_unavailable",
            "sealed ReplayV3 first-dip tape source frontier is unavailable",
        )

    receipt_payload_sha256 = sha256_json(receipt.to_dict())
    receipt_commit_refs = tuple(
        ref
        for ref in manifest.event_index.values()
        if ref.stream is CaptureStream.READ_RECEIPT
        and ref.payload_sha256 == receipt_payload_sha256
        and ref.sequence <= checkpoint.input_prefix_sequence
    )
    if (
        len(receipt_commit_refs) != 1
        or receipt_commit_refs[0].available_at < receipt.returned_at
        or receipt_commit_refs[0].available_at > checkpoint.available_at
    ):
        raise _FirstDipReceiptVerificationError(
            "receipt_commit_unavailable",
            "sealed ReplayV3 first-dip receipt commit is unavailable",
        )
    receipt_commit_ref = receipt_commit_refs[0]

    matching_source_refs = tuple(
        ref
        for ref in manifest.event_index.values()
        if ref.stream is CaptureStream.IQFEED_PRINT
        and ref.provider.strip().lower() == query.provider
        and ref.symbol == query.symbol
        and ref.identity_sha256 == receipt.identity_sha256
        and ref.sequence < receipt_commit_ref.sequence
        and ref.available_at <= query.available_at_most
    )
    if (
        not matching_source_refs
        or any(ref.provider_event_at is None for ref in matching_source_refs)
        or query.source_frontier_sequence
        != max(ref.sequence for ref in matching_source_refs)
    ):
        raise _FirstDipReceiptVerificationError(
            "source_frontier_unavailable",
            "sealed ReplayV3 first-dip tape source frontier is unavailable",
        )
    if any(
        ref.provider_event_at is not None
        and ref.provider_event_at > query.available_at_most
        for ref in matching_source_refs
    ):
        raise _FirstDipReceiptVerificationError(
            "source_clock_from_future",
            "sealed ReplayV3 first-dip tape source clock is from the future",
        )

    expected = tuple(
        sorted(
            (
                ref
                for ref in matching_source_refs
                if query.event_start_exclusive < ref.provider_event_at
                <= query.event_end_inclusive
            ),
            key=lambda ref: (ref.provider_event_at, ref.sequence),
        )
    )
    if tuple(receipt.source_event_sha256s) != tuple(
        ref.event_sha256 for ref in expected
    ):
        raise _FirstDipReceiptVerificationError(
            "inventory_mismatch",
            "sealed ReplayV3 first-dip tape receipt inventory mismatch",
        )
    if receipt.empty_result != (not expected):
        raise _FirstDipReceiptVerificationError(
            "empty_result_mismatch",
            "sealed ReplayV3 first-dip tape empty-result claim is inconsistent",
        )
    if receipt.result_sha256 != captured_read_result_sha256(expected):
        raise _FirstDipReceiptVerificationError(
            "result_mismatch",
            "sealed ReplayV3 first-dip tape result digest mismatch",
        )

    raw_evaluation = checkpoint.decision_payload.get(
        "first_dip_tape_evaluation"
    )
    raw_evaluation_sources = (
        raw_evaluation.get("source_event_sha256s")
        if isinstance(raw_evaluation, Mapping)
        else None
    )
    if (
        not isinstance(raw_evaluation, Mapping)
        or not isinstance(raw_evaluation_sources, (list, tuple))
        or str(raw_evaluation.get("result_sha256") or "").strip().lower()
        != receipt.result_sha256
        or tuple(raw_evaluation_sources) != receipt.source_event_sha256s
    ):
        raise _FirstDipReceiptVerificationError(
            (
                "empty_evaluation_mismatch"
                if receipt.empty_result
                else "evaluation_inventory_mismatch"
            ),
            "sealed ReplayV3 first-dip evaluation inventory mismatch",
        )
    if not expected:
        try:
            expected_evaluation = evaluate_first_dip_tape(
                first_dip_tape_window_from_capture(receipt, ()),
                policy=policy,
                decision_at=checkpoint.decision_at,
                symbol=checkpoint.symbol,
            )
        except (CaptureContractError, TypeError, ValueError) as exc:
            raise _FirstDipReceiptVerificationError(
                "empty_evaluation_mismatch",
                "sealed ReplayV3 empty first-dip evaluation is not canonical",
            ) from exc
        committed_evaluation_sha256 = str(
            checkpoint.decision_payload.get(
                "first_dip_tape_evaluation_sha256"
            )
            or ""
        ).strip().lower()
        if (
            dict(raw_evaluation) != expected_evaluation.to_dict()
            or committed_evaluation_sha256
            != expected_evaluation.evaluation_sha256
        ):
            raise _FirstDipReceiptVerificationError(
                "empty_evaluation_mismatch",
                "sealed ReplayV3 empty first-dip evaluation is not canonical",
            )
    return expected


def grade_replay_coverage(
    request: ReplayCoverageRequest,
    manifest: CaptureCoverageManifest,
) -> CaptureCoverageGrade:
    """Grade a replay window; any ambiguity is an explicit fail-closed reason."""

    reasons: list[str] = []
    binding = manifest.seal_binding
    if binding is None:
        reasons.append("sealed_capture_binding_missing")
    else:
        if not binding.sequences_contiguous:
            reasons.append("sealed_event_sequence_not_contiguous")
        if binding.sequence_min != 1:
            reasons.append("sealed_event_sequence_does_not_start_at_one")
    reasons.extend(manifest.certification_blockers)
    if not manifest.closed_cleanly:
        reasons.append("capture_run_not_closed_cleanly")
    if not manifest.content_root_verified:
        reasons.append("content_root_unverified")
    if manifest.replay_network_fallback_count:
        reasons.append("replay_network_fallback_used")
    if not manifest.required_streams_full_fidelity:
        reasons.append("required_stream_capture_not_full_fidelity")
    if (
        request.expected_identity_sha256 is not None
        and manifest.identity.identity_sha256 != request.expected_identity_sha256
    ):
        reasons.append("capture_identity_mismatch")

    typed_profile: FSMDependencyProfile | None = None
    checkpoints_by_hash = {
        checkpoint.checkpoint_sha256: checkpoint
        for checkpoint in manifest.decision_checkpoints
    }
    checkpoint = checkpoints_by_hash.get(request.decision_checkpoint_sha256)
    if checkpoint is None:
        reasons.append("decision_checkpoint_missing")
    else:
        if checkpoint.decision_id != request.decision_id:
            reasons.append("decision_checkpoint_id_mismatch")
        if checkpoint.symbol != request.symbol:
            reasons.append("decision_checkpoint_symbol_mismatch")
        if checkpoint.decision_at != request.decision_at:
            reasons.append("decision_checkpoint_clock_mismatch")
        if frozenset(checkpoint.required_read_ids) != request.required_read_ids:
            reasons.append("decision_checkpoint_read_set_mismatch")
        dependency_profile = checkpoint.decision_payload.get(
            "fsm_dependency_profile"
        )
        if not isinstance(dependency_profile, Mapping):
            reasons.append("fsm_dependency_profile_missing")
        else:
            try:
                typed_profile = FSMDependencyProfile.from_dict(dependency_profile)
            except (CaptureContractError, TypeError, ValueError):
                typed_profile = None
                reasons.append("fsm_dependency_profile_malformed")
            if typed_profile is not None:
                if not typed_profile.required_streams.issubset(
                    request.required_streams
                ):
                    reasons.append("fsm_dependency_profile_stream_set_mismatch")
                if frozenset(typed_profile.required_read_ids) != frozenset(
                    checkpoint.required_read_ids
                ):
                    reasons.append("fsm_dependency_profile_read_set_mismatch")

    receipts_by_id = {receipt.read_id: receipt for receipt in manifest.read_receipts}
    checkpoint_streams = {
        receipts_by_id[read_id].stream
        for read_id in (checkpoint.required_read_ids if checkpoint is not None else ())
        if read_id in receipts_by_id
    }
    if (
        typed_profile is not None
        and checkpoint_streams != typed_profile.required_streams
    ):
        # Live predecision attestation requires receipt evidence to cover every
        # declared causal input stream exactly.  ``request.required_streams``
        # may additionally require postdecision/session evidence such as broker
        # lifecycle transitions; those facts cannot be fabricated as reads in
        # the decision's pre-input prefix.
        reasons.append("fsm_dependency_profile_receipt_stream_set_mismatch")
    required = (
        request.required_streams
        | _ALWAYS_REQUIRED_IDENTITY_STREAMS
        | checkpoint_streams
    )
    if checkpoint is not None and not checkpoint_streams.issubset(
        request.required_streams | _ALWAYS_REQUIRED_IDENTITY_STREAMS
    ):
        reasons.append("coverage_required_stream_set_incomplete")
    for read_id in sorted(request.required_read_ids):
        receipt = receipts_by_id.get(read_id)
        if receipt is None:
            reasons.append(f"read_receipt_missing:{read_id}")
            continue
        first_dip_read_id = (
            str(
                checkpoint.decision_payload.get("first_dip_tape_read_id")
                or ""
            ).strip()
            if checkpoint is not None
            else ""
        )
        verified_first_dip_inventory: tuple[CaptureEventRef, ...] | None = None
        if receipt.decision_id != request.decision_id:
            reasons.append(f"read_receipt_decision_mismatch:{read_id}")
        if receipt.identity_sha256 != manifest.identity.identity_sha256:
            reasons.append(f"read_receipt_identity_mismatch:{read_id}")
        if receipt.stream in _SYMBOL_SCOPED_STREAMS and receipt.symbol != request.symbol:
            reasons.append(f"read_receipt_symbol_mismatch:{read_id}")
        if not receipt.content_verified:
            reasons.append(f"read_receipt_content_unverified:{read_id}")
        if receipt.replay_network_fallback_used:
            reasons.append(f"read_receipt_network_fallback:{read_id}")
        if not (
            request.warmup_start_at <= receipt.requested_at <= request.decision_at
            and receipt.returned_at <= request.decision_at
        ):
            reasons.append(f"read_receipt_outside_window:{read_id}")
        if receipt.stream is CaptureStream.IQFEED_PRINT:
            if checkpoint is None or read_id != first_dip_read_id:
                reasons.append(
                    f"first_dip_tape_receipt_decision_binding_mismatch:{read_id}"
                )
            else:
                try:
                    verified_first_dip_inventory = (
                        verify_first_dip_receipt_inventory(
                            receipt=receipt,
                            checkpoint=checkpoint,
                            manifest=manifest,
                        )
                    )
                except _FirstDipReceiptVerificationError as exc:
                    reasons.append(
                        f"first_dip_tape_receipt_{exc.reason_code}:{read_id}"
                    )
                except CaptureContractError:
                    reasons.append(
                        f"first_dip_tape_receipt_invalid:{read_id}"
                    )
        elif read_id == first_dip_read_id:
            reasons.append(
                f"first_dip_tape_receipt_stream_mismatch:{read_id}"
            )
        source_refs: list[CaptureEventRef] = []
        for source_sha256 in receipt.source_event_sha256s:
            ref = manifest.event_index.get(source_sha256)
            if ref is None:
                reasons.append(
                    f"read_receipt_source_event_missing:{read_id}:{source_sha256}"
                )
                continue
            source_refs.append(ref)
            if ref.identity_sha256 != receipt.identity_sha256:
                reasons.append(f"read_receipt_source_identity_mismatch:{read_id}")
            if ref.stream is not receipt.stream:
                reasons.append(f"read_receipt_source_stream_mismatch:{read_id}")
            if ref.provider != receipt.provider:
                reasons.append(f"read_receipt_source_provider_mismatch:{read_id}")
            if receipt.stream in _SYMBOL_SCOPED_STREAMS and (
                ref.symbol != request.symbol or receipt.symbol != request.symbol
            ):
                reasons.append(f"read_receipt_source_symbol_mismatch:{read_id}")
            elif (
                STREAM_POLICIES[receipt.stream].coverage_mode
                is CoverageMode.QUERY_RECEIPT
                and ref.symbol != receipt.symbol
            ):
                reasons.append(f"read_receipt_source_symbol_mismatch:{read_id}")
            if (
                STREAM_POLICIES[receipt.stream].query_parameters_required
                and ref.query_sha256 != receipt.query_sha256
            ):
                reasons.append(f"read_receipt_source_query_mismatch:{read_id}")
            if ref.available_at > receipt.returned_at:
                reasons.append(f"read_receipt_source_from_future:{read_id}")
            if checkpoint is not None and ref.sequence > checkpoint.input_prefix_sequence:
                reasons.append(f"read_receipt_source_after_prefix:{read_id}")
        expected_result_sha256 = captured_read_result_sha256(source_refs)
        if expected_result_sha256 != receipt.result_sha256:
            reasons.append(f"read_receipt_result_mismatch:{read_id}")
        if typed_profile is not None:
            try:
                dependency = typed_profile.dependency_for(receipt.stream)
            except CaptureContractError:
                reasons.append(
                    f"read_receipt_stream_undeclared_by_dependency_profile:{read_id}"
                )
            else:
                verified_empty_first_dip = (
                    verified_first_dip_inventory == ()
                    and receipt.empty_result
                    and receipt.stream is CaptureStream.IQFEED_PRINT
                )
                if dependency.exact_provider_event_at_required and (
                    (not source_refs and not verified_empty_first_dip)
                    or any(ref.provider_event_at is None for ref in source_refs)
                ):
                    reasons.append(
                        f"read_receipt_exact_event_clock_missing:{read_id}"
                    )
                if dependency.market_reference_at_required and (
                    (not source_refs and not verified_empty_first_dip)
                    or any(
                        ref.market_reference_at is None
                        and ref.provider_event_at is None
                        for ref in source_refs
                    )
                ):
                    reasons.append(
                        f"read_receipt_market_reference_clock_missing:{read_id}"
                    )
                if source_refs:
                    # ``available_at`` controls when replay may release a fact;
                    # it does not prove how fresh the underlying market fact is.
                    # Prefer the dependency's required source clock so re-emitting
                    # an old cached snapshot now cannot launder it as fresh.
                    source_clocks: list[datetime] = []
                    for ref in source_refs:
                        source_clock = _coverage_source_clock(
                            receipt.stream,
                            ref,
                            exact_required=(
                                dependency.exact_provider_event_at_required
                            ),
                            reference_required=(
                                dependency.market_reference_at_required
                            ),
                        )
                        if source_clock is not None:
                            source_clocks.append(source_clock)
                    if any(
                        source_clock > request.decision_at
                        for source_clock in source_clocks
                    ):
                        reasons.append(
                            f"read_receipt_source_clock_from_future:{read_id}"
                        )
                    freshest_source_at = (
                        max(source_clocks) if source_clocks else None
                    )
                    if (
                        freshest_source_at is not None
                        and request.decision_at
                        > freshest_source_at + timedelta(
                        seconds=dependency.max_source_age_seconds
                        )
                    ):
                        reasons.append(f"read_receipt_source_stale:{read_id}")
                if dependency.coverage_start_at > request.decision_at:
                    reasons.append(
                        f"dependency_coverage_starts_after_decision:{receipt.stream.value}"
                    )

    checkpoint_required_read_ids = set(
        checkpoint.required_read_ids if checkpoint is not None else ()
    )
    eligible_receipts_by_stream: dict[CaptureStream, list[CaptureReadReceipt]] = {}
    for receipt in manifest.read_receipts:
        # Query-backed coverage is decision-local.  An unrelated successful read
        # elsewhere in the run must never satisfy this checkpoint's dependency.
        if receipt.read_id not in checkpoint_required_read_ids:
            continue
        if not (
            request.warmup_start_at <= receipt.requested_at <= request.decision_at
            and receipt.returned_at <= request.decision_at
        ):
            continue
        if receipt.decision_id != request.decision_id:
            continue
        if not receipt.content_verified or receipt.replay_network_fallback_used:
            continue
        if receipt.identity_sha256 != manifest.identity.identity_sha256:
            continue
        if receipt.stream in _SYMBOL_SCOPED_STREAMS and receipt.symbol != request.symbol:
            continue
        eligible_receipts_by_stream.setdefault(receipt.stream, []).append(receipt)

    for stream in sorted(required, key=lambda item: item.value):
        policy = STREAM_POLICIES[stream]
        coverage = manifest.stream_coverage.get(stream)
        if coverage is None:
            reasons.append(f"stream_missing:{stream.value}")
            continue
        if coverage.identity_sha256 != manifest.identity.identity_sha256:
            reasons.append(f"stream_identity_mismatch:{stream.value}")
        if stream in _SYMBOL_SCOPED_STREAMS and coverage.symbol != request.symbol:
            reasons.append(f"stream_symbol_mismatch:{stream.value}")
        if policy.coverage_mode is CoverageMode.QUERY_RECEIPT:
            matching_refs = [
                ref
                for ref in manifest.event_index.values()
                if ref.stream is stream
            ]
        else:
            matching_refs = [
                ref
                for ref in manifest.event_index.values()
                if ref.stream is stream
                and ref.provider == coverage.provider
                and ref.symbol == coverage.symbol
            ]
        if len(matching_refs) != coverage.event_count:
            reasons.append(f"stream_event_count_mismatch:{stream.value}")
        if (
            matching_refs
            and policy.coverage_mode
            in {CoverageMode.CONTINUOUS, CoverageMode.CHANGE_LOG}
            and (
                min(ref.available_at for ref in matching_refs)
                != coverage.first_available_at
                or max(ref.available_at for ref in matching_refs)
                != coverage.last_available_at
            )
        ):
            reasons.append(f"stream_available_bounds_mismatch:{stream.value}")
        if (
            coverage.event_count <= 0
            and policy.coverage_mode is not CoverageMode.QUERY_RECEIPT
        ):
            reasons.append(f"stream_empty:{stream.value}")
        if not coverage.content_verified:
            reasons.append(f"stream_content_unverified:{stream.value}")
        if (
            policy.exact_provider_event_clock_required
            and (
                not coverage.exact_event_clock_complete
                or any(ref.provider_event_at is None for ref in matching_refs)
            )
        ):
            reasons.append(f"exact_event_clock_incomplete:{stream.value}")

        if policy.coverage_mode is CoverageMode.CONTINUOUS:
            if coverage.first_available_at > request.warmup_start_at:
                reasons.append(f"warmup_coverage_missing:{stream.value}")
            if coverage.last_available_at < request.exit_end_at:
                reasons.append(f"exit_coverage_missing:{stream.value}")
            if not coverage.continuity_complete:
                reasons.append(f"continuity_unproven:{stream.value}")
            watermark = coverage.watermark
            if watermark is None:
                reasons.append(f"provider_watermark_missing:{stream.value}")
            else:
                if watermark.generation != manifest.identity.generation:
                    reasons.append(f"watermark_generation_mismatch:{stream.value}")
                if watermark.identity_sha256 != manifest.identity.identity_sha256:
                    reasons.append(f"watermark_identity_mismatch:{stream.value}")
                if watermark.provider != coverage.provider:
                    reasons.append(f"watermark_provider_mismatch:{stream.value}")
                if watermark.symbol != coverage.symbol:
                    reasons.append(f"watermark_symbol_mismatch:{stream.value}")
                if watermark.event_watermark_at < request.exit_end_at:
                    reasons.append(f"provider_watermark_before_exit:{stream.value}")

        if policy.coverage_mode is CoverageMode.QUERY_RECEIPT:
            eligible_receipts = eligible_receipts_by_stream.get(stream, [])
            expected_decision_read_ids = {
                read_id
                for read_id in checkpoint_required_read_ids
                if read_id in receipts_by_id
                and receipts_by_id[read_id].stream is stream
            }
            if not eligible_receipts:
                reasons.append(f"query_receipt_missing:{stream.value}")
            if {
                receipt.read_id for receipt in eligible_receipts
            } != expected_decision_read_ids:
                reasons.append(
                    f"query_receipt_decision_set_mismatch:{stream.value}"
                )
            inventory_receipts = [
                receipt
                for receipt in manifest.read_receipts
                if receipt.stream is stream
            ]
            if (
                coverage.query_receipt_count <= 0
                or coverage.query_receipt_count != len(inventory_receipts)
            ):
                reasons.append(f"query_receipt_count_mismatch:{stream.value}")

        if policy.coverage_mode is CoverageMode.IDENTITY:
            if coverage.first_available_at > request.decision_at:
                reasons.append(f"identity_unavailable_at_decision:{stream.value}")
            identity_payload_hash = {
                CaptureStream.CONFIG_SNAPSHOT: manifest.identity.config_sha256,
                CaptureStream.FEATURE_FLAG_SNAPSHOT: (
                    manifest.identity.feature_flags_sha256
                ),
                CaptureStream.CODE_BUILD: manifest.identity.code_build_sha256,
            }.get(stream)
            if identity_payload_hash is not None and not any(
                ref.payload_sha256 == identity_payload_hash for ref in matching_refs
            ):
                reasons.append(f"identity_content_root_mismatch:{stream.value}")

        if policy.coverage_mode is CoverageMode.CHANGE_LOG:
            if coverage.first_available_at > request.warmup_start_at:
                reasons.append(f"warmup_coverage_missing:{stream.value}")
            if not coverage.continuity_complete:
                reasons.append(f"continuity_unproven:{stream.value}")
            source_rows = tuple(
                (ref, _coverage_source_clock(stream, ref))
                for ref in matching_refs
            )
            if not source_rows or any(
                source_clock is None for _ref, source_clock in source_rows
            ):
                reasons.append(f"change_log_source_clock_missing:{stream.value}")
            if any(
                source_clock is not None and source_clock > ref.available_at
                for ref, source_clock in source_rows
            ):
                reasons.append(
                    f"change_log_source_clock_from_future:{stream.value}"
                )
            if not any(
                ref.available_at <= request.warmup_start_at
                and source_clock is not None
                and source_clock <= request.warmup_start_at
                for ref, source_clock in source_rows
            ):
                reasons.append(f"change_log_warmup_source_missing:{stream.value}")
            watermark = coverage.watermark
            if watermark is None:
                reasons.append(f"provider_watermark_missing:{stream.value}")
            else:
                if watermark.generation != manifest.identity.generation:
                    reasons.append(f"watermark_generation_mismatch:{stream.value}")
                if watermark.identity_sha256 != manifest.identity.identity_sha256:
                    reasons.append(f"watermark_identity_mismatch:{stream.value}")
                if watermark.provider != coverage.provider:
                    reasons.append(f"watermark_provider_mismatch:{stream.value}")
                if watermark.symbol != coverage.symbol:
                    reasons.append(f"watermark_symbol_mismatch:{stream.value}")
                if watermark.event_watermark_at < request.exit_end_at:
                    reasons.append(f"provider_watermark_before_exit:{stream.value}")

    for gap in manifest.gaps:
        if gap.stream not in required and gap.stream is not CaptureStream.COVERAGE_GAP:
            continue
        if (
            gap.stream is not CaptureStream.COVERAGE_GAP
            and request.symbol
            and gap.symbol not in (None, request.symbol)
        ):
            continue
        if gap.intersects(request.warmup_start_at, request.exit_end_at):
            reasons.append(
                f"coverage_gap:{gap.stream.value}:{gap.reason}:{gap.lost_count}"
            )

    unique_reasons = tuple(dict.fromkeys(reasons))
    return CaptureCoverageGrade(
        replayable=not unique_reasons,
        grade="complete" if not unique_reasons else "coverage_unavailable",
        reasons=unique_reasons,
        manifest_sha256=manifest.manifest_sha256,
    )


class DeterministicDualClockLoader:
    """Release captured inputs strictly by ``available_at``, never market time.

    Market/provider timestamps remain in each event for feature construction,
    but an event cannot be observed by ReplayV3 before its captured availability
    clock.  Ties use the monotonic generation sequence and content hash, making
    repeated loads byte-for-byte deterministic.
    """

    def __init__(self, events: Iterable[CaptureEvent]) -> None:
        rows = tuple(events)
        identities = {event.identity.identity_sha256 for event in rows}
        if len(identities) > 1:
            raise CaptureContractError("a loader cannot mix capture identities")
        seen_sequences: set[int] = set()
        for event in rows:
            if event.sequence in seen_sequences:
                raise CaptureContractError(
                    f"duplicate capture sequence: {event.sequence}"
                )
            seen_sequences.add(event.sequence)
        self._events = tuple(
            sorted(
                rows,
                key=lambda event: (
                    event.clocks.available_at,
                    event.sequence,
                    event.event_sha256,
                ),
            )
        )
        self._cursor = 0
        self._advanced_to: datetime | None = None

    @property
    def events(self) -> tuple[CaptureEvent, ...]:
        return self._events

    @property
    def remaining(self) -> int:
        return len(self._events) - self._cursor

    def reset(self) -> None:
        self._cursor = 0
        self._advanced_to = None

    def advance_to(self, available_at: datetime) -> tuple[CaptureEvent, ...]:
        boundary = _utc(available_at, "available_at")
        if self._advanced_to is not None and boundary < self._advanced_to:
            raise CaptureContractError("dual-clock replay cannot move backwards")
        released: list[CaptureEvent] = []
        while self._cursor < len(self._events):
            event = self._events[self._cursor]
            if event.clocks.available_at > boundary:
                break
            released.append(event)
            self._cursor += 1
        self._advanced_to = boundary
        return tuple(released)

    def iter_release_order(self) -> Sequence[CaptureEvent]:
        return self._events
