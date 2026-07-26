"""Typed, quota-bound Ortex short mechanics for the squeeze-fuel selection edge.

Every read has a closed outcome. A cold resolved-symbol read atomically reserves
short-interest and cost-to-borrow quota, commits each transport marker before HTTP,
performs no transient retry, and preserves exact raw bytes and causal clocks.
Successful or authoritative-empty daily rows use a 24-hour process and restart-safe
cache; failures and incomplete endpoint pairs never become economic data.

The fixed HTTPS origin refuses redirects. Capture dictionaries omit credentials,
are content-addressed, and re-parse raw rows during strict loading. Captured PAPER
and ReplayV3 can bind an exact provider whose network fallback is forbidden.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from types import MappingProxyType, SimpleNamespace
from typing import Iterator, Mapping, Protocol, Sequence, runtime_checkable
from uuid import uuid4

from ....config import settings

# Ortex API origin is immutable.  The request opener refuses redirects and the response
# URL is checked byte-for-byte before any provider payload can influence selection.
_ORTEX_SCHEME = "https"
_ORTEX_HOST = "api.ortex.com"
_ORTEX_API_PREFIX = "/api/v1"
_ORTEX_BASE = f"{_ORTEX_SCHEME}://{_ORTEX_HOST}{_ORTEX_API_PREFIX}"
_ORTEX_KEY_HEADER = "Ortex-Api-Key"

_CAPTURE_SCHEMA = "chili.ortex.short-mechanics.v2"
_CAPTURE_VERSION = 2
_DISCOVERY_EXCHANGES = ("nasdaq", "nyse", "amex")
_ENDPOINT_DATASETS = ("short_interest", "cost_to_borrow")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,15}$")
_NON_EQUITY_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,15}-USD$")
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
_UTC = timezone.utc

# Daily datasets.  At 12 candidates x 2 endpoints x about 21 sessions, one successful
# refresh per 24 hours uses roughly 504 credits/month and leaves headroom under the
# operator's 1,000-credit plan for typed exchange discovery and failures.
_DEFAULT_SUCCESS_CACHE_TTL_SECONDS = 24 * 60 * 60
_DEFAULT_MONTHLY_LIMIT = 1000
_REQUEST_INTERVAL_SECONDS = 1.05
_QUOTA_LEASE_SECONDS = 30
_BACKOFF_BASE_SECONDS = 2.0
_BACKOFF_MAX_SECONDS = 300.0
# One rejected credential may consume one credit, but this durable latch makes
# it mathematically impossible for an unchanged runtime to spend all 1,000
# monthly credits on authentication failures (31 days / 1 hour < 1,000).
_AUTH_REJECTION_BACKOFF_SECONDS = 60.0 * 60.0
_MAX_RESPONSE_BYTES = 1024 * 1024
_REQ_TIMEOUT_SECONDS = 8.0


def _utcnow() -> datetime:
    return datetime.now(_UTC)


def _monotonic() -> float:
    return time.monotonic()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(_canonical_json_bytes(value))


def _setting_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = getattr(settings, name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be in [{minimum}, {maximum}]")
    return value


def _setting_float(
    name: str, default: float, *, minimum: float, maximum: float
) -> float:
    raw = getattr(settings, name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be finite and in [{minimum}, {maximum}]")
    return value


def _policy_capture() -> dict[str, object]:
    """Public, credential-free policy values sealed beside every outcome."""

    quota_lease_seconds = _setting_float(
        "chili_ortex_reservation_lease_seconds",
        float(_QUOTA_LEASE_SECONDS),
        minimum=1.0,
        maximum=300.0,
    )
    return {
        "schema": _CAPTURE_SCHEMA,
        "version": _CAPTURE_VERSION,
        "monthly_limit": _setting_int(
            "chili_ortex_monthly_request_limit",
            _DEFAULT_MONTHLY_LIMIT,
            minimum=1,
            maximum=_DEFAULT_MONTHLY_LIMIT,
        ),
        "success_cache_ttl_seconds": _setting_int(
            "chili_ortex_success_cache_ttl_seconds",
            _DEFAULT_SUCCESS_CACHE_TTL_SECONDS,
            minimum=60,
            maximum=7 * 24 * 60 * 60,
        ),
        "request_interval_seconds": _setting_float(
            "chili_ortex_request_interval_seconds",
            _REQUEST_INTERVAL_SECONDS,
            minimum=1.0,
            maximum=10.0,
        ),
        "quota_lease_seconds": quota_lease_seconds,
        "backoff_base_seconds": _setting_float(
            "chili_ortex_transient_backoff_base_seconds",
            _BACKOFF_BASE_SECONDS,
            minimum=1.0,
            maximum=60.0,
        ),
        "backoff_max_seconds": _setting_float(
            "chili_ortex_transient_backoff_max_seconds",
            _BACKOFF_MAX_SECONDS,
            minimum=1.0,
            maximum=3600.0,
        ),
        "auth_rejection_backoff_seconds": (
            _AUTH_REJECTION_BACKOFF_SECONDS
        ),
        "max_response_bytes": _setting_int(
            "chili_ortex_response_max_bytes",
            _MAX_RESPONSE_BYTES,
            minimum=1024,
            maximum=10 * 1024 * 1024,
        ),
        "request_timeout_seconds": _REQ_TIMEOUT_SECONDS,
        "month_boundary_guard_seconds": max(
            _REQ_TIMEOUT_SECONDS,
            quota_lease_seconds,
        ),
        "origin": f"{_ORTEX_SCHEME}://{_ORTEX_HOST}",
        "api_prefix": _ORTEX_API_PREFIX,
        "discovery_exchanges": list(_DISCOVERY_EXCHANGES),
        "endpoint_datasets": list(_ENDPOINT_DATASETS),
        "easy_to_borrow_ctb_pct": 1.0,
    }


def _policy_sha256() -> str:
    return _sha256_json(_policy_capture())


def _validate_public_policy(value: Mapping[str, object]) -> None:
    expected_keys = {
        "schema",
        "version",
        "monthly_limit",
        "success_cache_ttl_seconds",
        "request_interval_seconds",
        "quota_lease_seconds",
        "backoff_base_seconds",
        "backoff_max_seconds",
        "auth_rejection_backoff_seconds",
        "max_response_bytes",
        "request_timeout_seconds",
        "month_boundary_guard_seconds",
        "origin",
        "api_prefix",
        "discovery_exchanges",
        "endpoint_datasets",
        "easy_to_borrow_ctb_pct",
    }
    if set(value) != expected_keys:
        raise ValueError("public Ortex policy fields do not match the v2 contract")
    if value["schema"] != _CAPTURE_SCHEMA or value["version"] != _CAPTURE_VERSION:
        raise ValueError("public Ortex policy schema/version mismatch")
    integer_ranges = {
        "monthly_limit": (1, _DEFAULT_MONTHLY_LIMIT),
        "success_cache_ttl_seconds": (60, 7 * 24 * 60 * 60),
        "max_response_bytes": (1024, 10 * 1024 * 1024),
    }
    for field, (minimum, maximum) in integer_ranges.items():
        item = value[field]
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"public policy {field} must be an integer")
        if not minimum <= item <= maximum:
            raise ValueError(f"public policy {field} is outside its domain")
    float_ranges = {
        "request_interval_seconds": (1.0, 10.0),
        "quota_lease_seconds": (1.0, 300.0),
        "backoff_base_seconds": (1.0, 60.0),
        "backoff_max_seconds": (1.0, 3600.0),
        "auth_rejection_backoff_seconds": (
            _AUTH_REJECTION_BACKOFF_SECONDS,
            _AUTH_REJECTION_BACKOFF_SECONDS,
        ),
        "request_timeout_seconds": (0.1, 60.0),
        "month_boundary_guard_seconds": (0.1, 300.0),
        "easy_to_borrow_ctb_pct": (0.0, 1000.0),
    }
    for field, (minimum, maximum) in float_ranges.items():
        item = value[field]
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"public policy {field} must be numeric")
        parsed = float(item)
        if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
            raise ValueError(f"public policy {field} is outside its domain")
    if value["origin"] != f"{_ORTEX_SCHEME}://{_ORTEX_HOST}":
        raise ValueError("public policy origin mismatch")
    if float(value["month_boundary_guard_seconds"]) < float(
        value["request_timeout_seconds"]
    ):
        raise ValueError(
            "month boundary guard is shorter than request timeout"
        )
    if float(value["month_boundary_guard_seconds"]) < float(
        value["quota_lease_seconds"]
    ):
        raise ValueError(
            "month boundary guard is shorter than quota authority lease"
        )
    if value["api_prefix"] != _ORTEX_API_PREFIX:
        raise ValueError("public policy API prefix mismatch")
    if tuple(value["discovery_exchanges"]) != _DISCOVERY_EXCHANGES:  # type: ignore[arg-type]
        raise ValueError("public policy exchange discovery order mismatch")
    if tuple(value["endpoint_datasets"]) != _ENDPOINT_DATASETS:  # type: ignore[arg-type]
        raise ValueError("public policy endpoint set mismatch")


def ortex_public_policy() -> dict[str, object]:
    """Return the exact credential-free runtime policy bound into capture."""

    return json.loads(_canonical_json_bytes(_policy_capture()).decode("utf-8"))


def ortex_public_policy_sha256() -> str:
    return _sha256_json(ortex_public_policy())


class OrtexOutcomeKind(str, Enum):
    SUCCESS = "SUCCESS"
    AUTHORITATIVE_EMPTY = "AUTHORITATIVE_EMPTY"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    DISABLED = "DISABLED"
    CREDENTIAL_UNAVAILABLE = "CREDENTIAL_UNAVAILABLE"
    UNSUPPORTED_SYMBOL_OR_EXCHANGE = "UNSUPPORTED_SYMBOL_OR_EXCHANGE"
    AUTH_REJECTED = "AUTH_REJECTED"
    RATE_LIMITED = "RATE_LIMITED"
    TRANSIENT_UNAVAILABLE = "TRANSIENT_UNAVAILABLE"
    PERMANENT_REJECTED = "PERMANENT_REJECTED"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    MONTHLY_QUOTA_EXHAUSTED = "MONTHLY_QUOTA_EXHAUSTED"
    BACKOFF_ACTIVE = "BACKOFF_ACTIVE"
    QUOTA_AUTHORITY_UNAVAILABLE = "QUOTA_AUTHORITY_UNAVAILABLE"
    CONTRACT_MISMATCH = "CONTRACT_MISMATCH"


def _enum_name(value: object) -> str:
    if isinstance(value, Enum):
        return value.name.upper()
    return str(value or "").strip().upper()


def _checked_sha256(value: object, *, field: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    text = str(value or "")
    if not _SHA256_RE.fullmatch(text):
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return text


def _checked_code(value: object, *, field: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    text = str(value or "")
    if not _SAFE_CODE_RE.fullmatch(text):
        raise ValueError(f"{field} is not a safe canonical code")
    return text


def _as_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(_UTC)


def _dt_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value, field="datetime").isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_dt(value: object, *, field: str, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a UTC timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid timestamp") from exc
    return _as_utc(parsed, field=field)


def _finite_nonnegative(value: object, *, field: str, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return parsed


@dataclass(frozen=True, slots=True)
class OrtexQuotaProvenance:
    attempt_id: str
    bundle_sha256: str
    bundle_index: int
    owner_token_sha256: str
    request_sha256: str
    month_start: datetime
    quota_used_after: int
    monthly_limit: int
    reserved_at: datetime
    lease_expires_at: datetime
    transport_committed: bool
    completion_kind: str
    backoff_until: datetime | None = None

    def __post_init__(self) -> None:
        if not self.attempt_id:
            raise ValueError("attempt_id is required")
        _checked_sha256(self.bundle_sha256, field="bundle_sha256")
        _checked_sha256(self.owner_token_sha256, field="owner_token_sha256")
        _checked_sha256(self.request_sha256, field="request_sha256")
        if self.bundle_index < 0:
            raise ValueError("bundle_index must be non-negative")
        if self.quota_used_after < 1:
            raise ValueError("quota_used_after must be positive")
        if not 1 <= self.monthly_limit <= _DEFAULT_MONTHLY_LIMIT:
            raise ValueError("monthly_limit is outside the configured plan")
        month = _as_utc(self.month_start, field="month_start")
        if month != datetime(month.year, month.month, 1, tzinfo=_UTC):
            raise ValueError("month_start must be UTC calendar-month start")
        reserved = _as_utc(self.reserved_at, field="reserved_at")
        lease = _as_utc(self.lease_expires_at, field="lease_expires_at")
        if lease < reserved:
            raise ValueError("lease_expires_at precedes reserved_at")
        if not self.transport_committed:
            raise ValueError("captured attempts must be transport-committed")
        _checked_code(self.completion_kind, field="completion_kind")
        if self.backoff_until is not None:
            _as_utc(self.backoff_until, field="backoff_until")

    def to_capture_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "bundle_sha256": self.bundle_sha256,
            "bundle_index": self.bundle_index,
            "owner_token_sha256": self.owner_token_sha256,
            "request_sha256": self.request_sha256,
            "month_start": _dt_text(self.month_start),
            "quota_used_after": self.quota_used_after,
            "monthly_limit": self.monthly_limit,
            "reserved_at": _dt_text(self.reserved_at),
            "lease_expires_at": _dt_text(self.lease_expires_at),
            "transport_committed": self.transport_committed,
            "completion_kind": self.completion_kind,
            "backoff_until": _dt_text(self.backoff_until),
        }

    @classmethod
    def from_capture_dict(cls, value: object) -> "OrtexQuotaProvenance":
        if not isinstance(value, Mapping):
            raise ValueError("quota provenance must be an object")
        expected = {
            "attempt_id",
            "bundle_sha256",
            "bundle_index",
            "owner_token_sha256",
            "request_sha256",
            "month_start",
            "quota_used_after",
            "monthly_limit",
            "reserved_at",
            "lease_expires_at",
            "transport_committed",
            "completion_kind",
            "backoff_until",
        }
        if set(value) != expected:
            raise ValueError("quota provenance fields do not match the v2 contract")
        for field in (
            "bundle_index",
            "quota_used_after",
            "monthly_limit",
        ):
            if isinstance(value[field], bool) or not isinstance(value[field], int):
                raise ValueError(f"quota provenance {field} must be an integer")
        if not isinstance(value["transport_committed"], bool):
            raise ValueError("transport_committed must be a Boolean")
        return cls(
            attempt_id=str(value["attempt_id"]),
            bundle_sha256=str(value["bundle_sha256"]),
            bundle_index=int(value["bundle_index"]),
            owner_token_sha256=str(value["owner_token_sha256"]),
            request_sha256=str(value["request_sha256"]),
            month_start=_parse_dt(value["month_start"], field="month_start"),  # type: ignore[arg-type]
            quota_used_after=int(value["quota_used_after"]),
            monthly_limit=int(value["monthly_limit"]),
            reserved_at=_parse_dt(value["reserved_at"], field="reserved_at"),  # type: ignore[arg-type]
            lease_expires_at=_parse_dt(
                value["lease_expires_at"], field="lease_expires_at"
            ),  # type: ignore[arg-type]
            transport_committed=value["transport_committed"],
            completion_kind=str(value["completion_kind"]),
            backoff_until=_parse_dt(
                value["backoff_until"], field="backoff_until", optional=True
            ),
        )


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in sorted(value.items())}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite public policy value")
        return value
    raise ValueError("public policy contains a non-JSON value")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _endpoint_path(exchange: str, symbol: str, dataset: str) -> str:
    suffix = "short_interest" if dataset == "short_interest" else "ctb/all"
    return f"/stock/{exchange}/{symbol}/{suffix}"


@dataclass(frozen=True, slots=True)
class OrtexEndpointOutcome:
    kind: OrtexOutcomeKind
    dataset: str
    symbol: str
    exchange: str
    request_path: str
    request_sha256: str
    query_sha256: str
    quota_policy_sha256: str
    http_status: int | None
    provider_event_at: datetime | None
    effective_at: datetime | None
    received_at: datetime
    available_at: datetime
    raw_response_b64: str | None
    raw_response_sha256: str | None
    selected_row_sha256: str | None
    value: float | None
    detail_code: str
    quota: OrtexQuotaProvenance

    def __post_init__(self) -> None:
        if not isinstance(self.kind, OrtexOutcomeKind):
            raise ValueError("endpoint kind is not typed")
        if self.dataset not in _ENDPOINT_DATASETS:
            raise ValueError("unsupported Ortex endpoint dataset")
        if not _SYMBOL_RE.fullmatch(self.symbol):
            raise ValueError("endpoint symbol is not canonical")
        if self.exchange not in _DISCOVERY_EXCHANGES:
            raise ValueError("endpoint exchange is not canonical")
        if self.request_path != _endpoint_path(
            self.exchange, self.symbol, self.dataset
        ):
            raise ValueError("request_path does not bind dataset/symbol/exchange")
        _checked_sha256(self.request_sha256, field="request_sha256")
        _checked_sha256(self.query_sha256, field="query_sha256")
        _checked_sha256(
            self.quota_policy_sha256, field="quota_policy_sha256"
        )
        if self.quota.request_sha256 != self.request_sha256:
            raise ValueError("quota request hash does not bind endpoint request")
        if self.http_status is not None and not 100 <= self.http_status <= 599:
            raise ValueError("http_status is outside the HTTP domain")
        received = _as_utc(self.received_at, field="received_at")
        available = _as_utc(self.available_at, field="available_at")
        if available < received:
            raise ValueError("available_at precedes received_at")
        if self.provider_event_at is not None:
            provider_event = _as_utc(
                self.provider_event_at, field="provider_event_at"
            )
            if provider_event > received:
                raise ValueError("provider_event_at is later than received_at")
        if self.effective_at is not None:
            effective = _as_utc(self.effective_at, field="effective_at")
            if effective > received:
                raise ValueError("effective_at is later than received_at")
        _checked_code(self.detail_code, field="detail_code")
        if (self.raw_response_b64 is None) != (
            self.raw_response_sha256 is None
        ):
            raise ValueError("raw response bytes/hash must be present together")
        if self.raw_response_b64 is not None:
            try:
                raw = base64.b64decode(
                    self.raw_response_b64.encode("ascii"), validate=True
                )
            except Exception as exc:
                raise ValueError("raw response is not strict base64") from exc
            if len(raw) > 10 * 1024 * 1024:
                raise ValueError("raw response exceeds the sealed policy maximum")
            if _sha256_bytes(raw) != self.raw_response_sha256:
                raise ValueError("raw response hash mismatch")
        if self.selected_row_sha256 is not None:
            _checked_sha256(
                self.selected_row_sha256, field="selected_row_sha256"
            )
        parsed_value = _finite_nonnegative(
            self.value, field="value", optional=True
        )
        object.__setattr__(self, "value", parsed_value)
        if self.kind is OrtexOutcomeKind.SUCCESS and self.value is None:
            raise ValueError("successful endpoint has no typed value")
        if (
            self.kind is OrtexOutcomeKind.AUTHORITATIVE_EMPTY
            and self.value is not None
        ):
            raise ValueError("authoritative-empty endpoint carries a value")

    def _capture_without_hash(self) -> dict[str, object]:
        return {
            "schema": _CAPTURE_SCHEMA,
            "version": _CAPTURE_VERSION,
            "kind": self.kind.value,
            "dataset": self.dataset,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "request_path": self.request_path,
            "request_sha256": self.request_sha256,
            "query_sha256": self.query_sha256,
            "quota_policy_sha256": self.quota_policy_sha256,
            "http_status": self.http_status,
            "provider_event_at": _dt_text(self.provider_event_at),
            "effective_at": _dt_text(self.effective_at),
            "received_at": _dt_text(self.received_at),
            "available_at": _dt_text(self.available_at),
            "raw_response_b64": self.raw_response_b64,
            "raw_response_sha256": self.raw_response_sha256,
            "selected_row_sha256": self.selected_row_sha256,
            "value": self.value,
            "detail_code": self.detail_code,
            "quota": self.quota.to_capture_dict(),
        }

    @property
    def capture_sha256(self) -> str:
        return _sha256_json(self._capture_without_hash())

    def to_capture_dict(self) -> dict[str, object]:
        value = self._capture_without_hash()
        value["capture_sha256"] = self.capture_sha256
        return value

    @classmethod
    def from_capture_dict(cls, value: object) -> "OrtexEndpointOutcome":
        if not isinstance(value, Mapping):
            raise ValueError("endpoint capture must be an object")
        expected = {
            "schema",
            "version",
            "kind",
            "dataset",
            "symbol",
            "exchange",
            "request_path",
            "request_sha256",
            "query_sha256",
            "quota_policy_sha256",
            "http_status",
            "provider_event_at",
            "effective_at",
            "received_at",
            "available_at",
            "raw_response_b64",
            "raw_response_sha256",
            "selected_row_sha256",
            "value",
            "detail_code",
            "quota",
            "capture_sha256",
        }
        if set(value) != expected:
            raise ValueError("endpoint capture fields do not match the v2 contract")
        if value["schema"] != _CAPTURE_SCHEMA or value["version"] != _CAPTURE_VERSION:
            raise ValueError("endpoint capture schema/version mismatch")
        if value["http_status"] is not None and (
            isinstance(value["http_status"], bool)
            or not isinstance(value["http_status"], int)
        ):
            raise ValueError("endpoint http_status must be an integer or null")
        if value["value"] is not None and (
            isinstance(value["value"], bool)
            or not isinstance(value["value"], (int, float))
        ):
            raise ValueError("endpoint value must be numeric or null")
        outcome = cls(
            kind=OrtexOutcomeKind(str(value["kind"])),
            dataset=str(value["dataset"]),
            symbol=str(value["symbol"]),
            exchange=str(value["exchange"]),
            request_path=str(value["request_path"]),
            request_sha256=str(value["request_sha256"]),
            query_sha256=str(value["query_sha256"]),
            quota_policy_sha256=str(value["quota_policy_sha256"]),
            http_status=(
                None if value["http_status"] is None else int(value["http_status"])
            ),
            provider_event_at=_parse_dt(
                value["provider_event_at"],
                field="provider_event_at",
                optional=True,
            ),
            effective_at=_parse_dt(
                value["effective_at"], field="effective_at", optional=True
            ),
            received_at=_parse_dt(
                value["received_at"], field="received_at"
            ),  # type: ignore[arg-type]
            available_at=_parse_dt(
                value["available_at"], field="available_at"
            ),  # type: ignore[arg-type]
            raw_response_b64=(
                None
                if value["raw_response_b64"] is None
                else str(value["raw_response_b64"])
            ),
            raw_response_sha256=(
                None
                if value["raw_response_sha256"] is None
                else str(value["raw_response_sha256"])
            ),
            selected_row_sha256=(
                None
                if value["selected_row_sha256"] is None
                else str(value["selected_row_sha256"])
            ),
            value=(
                None
                if value["value"] is None
                else _finite_nonnegative(value["value"], field="value")
            ),
            detail_code=str(value["detail_code"]),
            quota=OrtexQuotaProvenance.from_capture_dict(value["quota"]),
        )
        if value["capture_sha256"] != outcome.capture_sha256:
            raise ValueError("endpoint capture hash mismatch")
        return outcome


@dataclass(frozen=True, slots=True)
class OrtexShortMechanicsOutcome:
    kind: OrtexOutcomeKind
    symbol: str
    requested_exchange: str | None
    resolved_exchange: str | None
    endpoints: tuple[OrtexEndpointOutcome, ...]
    short_interest_pct: float | None
    cost_to_borrow: float | None
    utilization: float | None
    is_easy_to_borrow: bool | None
    provider_event_at: datetime | None
    effective_at: datetime | None
    received_at: datetime
    available_at: datetime
    detail_code: str
    policy: Mapping[str, object]
    quota_policy_sha256: str
    cache_origin_sha256: str | None = None
    cache_origin_received_at: datetime | None = None
    cache_origin_available_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, OrtexOutcomeKind):
            raise ValueError("aggregate kind is not typed")
        if not _SYMBOL_RE.fullmatch(self.symbol) and not (
            self.kind is OrtexOutcomeKind.NOT_APPLICABLE
            and self.detail_code == "non_equity"
            and _NON_EQUITY_SYMBOL_RE.fullmatch(self.symbol)
        ):
            raise ValueError("aggregate symbol is not canonical")
        if (
            self.requested_exchange is not None
            and self.requested_exchange not in _DISCOVERY_EXCHANGES
        ):
            raise ValueError("requested_exchange is not supported")
        if (
            self.resolved_exchange is not None
            and self.resolved_exchange not in _DISCOVERY_EXCHANGES
        ):
            raise ValueError("resolved_exchange is not supported")
        frozen_policy = _freeze_json(self.policy)
        if not isinstance(frozen_policy, Mapping):
            raise ValueError("public policy must be an object")
        object.__setattr__(self, "policy", frozen_policy)
        policy_dict = _thaw_json(frozen_policy)
        if not isinstance(policy_dict, dict):
            raise ValueError("public policy is malformed")
        _validate_public_policy(policy_dict)
        _checked_sha256(
            self.quota_policy_sha256, field="quota_policy_sha256"
        )
        if _sha256_json(policy_dict) != self.quota_policy_sha256:
            raise ValueError("quota policy hash mismatch")
        _checked_code(self.detail_code, field="detail_code")
        received = _as_utc(self.received_at, field="received_at")
        available = _as_utc(self.available_at, field="available_at")
        if available < received:
            raise ValueError("aggregate available_at precedes received_at")
        if self.provider_event_at is not None:
            _as_utc(self.provider_event_at, field="provider_event_at")
        if self.effective_at is not None:
            _as_utc(self.effective_at, field="effective_at")
        si = _finite_nonnegative(
            self.short_interest_pct,
            field="short_interest_pct",
            optional=True,
        )
        ctb = _finite_nonnegative(
            self.cost_to_borrow, field="cost_to_borrow", optional=True
        )
        util = _finite_nonnegative(
            self.utilization, field="utilization", optional=True
        )
        object.__setattr__(self, "short_interest_pct", si)
        object.__setattr__(self, "cost_to_borrow", ctb)
        object.__setattr__(self, "utilization", util)
        if self.is_easy_to_borrow is not None and not isinstance(
            self.is_easy_to_borrow, bool
        ):
            raise ValueError("is_easy_to_borrow must be a Boolean or null")
        if self.kind is OrtexOutcomeKind.SUCCESS and si is None and ctb is None:
            raise ValueError("successful aggregate has no usable mechanics")
        if self.kind is not OrtexOutcomeKind.SUCCESS and any(
            item is not None
            for item in (si, ctb, util, self.is_easy_to_borrow)
        ):
            raise ValueError("neutral aggregate carries mechanics values")
        endpoint_hashes: set[str] = set()
        for endpoint in self.endpoints:
            if endpoint.symbol != self.symbol:
                raise ValueError("endpoint symbol differs from aggregate symbol")
            if endpoint.quota_policy_sha256 != self.quota_policy_sha256:
                raise ValueError("endpoint policy hash differs from aggregate")
            if endpoint.capture_sha256 in endpoint_hashes:
                raise ValueError("duplicate endpoint receipt")
            endpoint_hashes.add(endpoint.capture_sha256)
        if self.kind in {
            OrtexOutcomeKind.SUCCESS,
            OrtexOutcomeKind.AUTHORITATIVE_EMPTY,
        }:
            if self.resolved_exchange is None:
                raise ValueError("authoritative aggregate has no resolved exchange")
            resolved = [
                endpoint
                for endpoint in self.endpoints
                if endpoint.exchange == self.resolved_exchange
            ]
            if (
                len(resolved) != 2
                or {endpoint.dataset for endpoint in resolved}
                != set(_ENDPOINT_DATASETS)
                or any(
                    endpoint.kind
                    not in {
                        OrtexOutcomeKind.SUCCESS,
                        OrtexOutcomeKind.AUTHORITATIVE_EMPTY,
                    }
                    for endpoint in resolved
                )
            ):
                raise ValueError(
                    "authoritative aggregate lacks the exact SI+CTB receipt pair"
                )
            resolved_values = {
                endpoint.dataset: (
                    endpoint.value
                    if endpoint.kind is OrtexOutcomeKind.SUCCESS
                    else None
                )
                for endpoint in resolved
            }
            if (
                resolved_values["short_interest"] != si
                or resolved_values["cost_to_borrow"] != ctb
            ):
                raise ValueError("aggregate mechanics differ from endpoint receipts")
        cache_values = (
            self.cache_origin_sha256,
            self.cache_origin_received_at,
            self.cache_origin_available_at,
        )
        if any(item is not None for item in cache_values) and not all(
            item is not None for item in cache_values
        ):
            raise ValueError("cache origin provenance is incomplete")
        if self.cache_origin_sha256 is not None:
            _checked_sha256(
                self.cache_origin_sha256, field="cache_origin_sha256"
            )
            cache_received = _as_utc(
                self.cache_origin_received_at,  # type: ignore[arg-type]
                field="cache_origin_received_at",
            )
            cache_available = _as_utc(
                self.cache_origin_available_at,  # type: ignore[arg-type]
                field="cache_origin_available_at",
            )
            if cache_available < cache_received:
                raise ValueError("cache origin clocks are reversed")

    @classmethod
    def not_applicable(
        cls,
        *,
        symbol: str,
        reason: str,
        observed_at: datetime,
        policy_sha256: str,
        policy: Mapping[str, object] | None = None,
    ) -> "OrtexShortMechanicsOutcome":
        if reason not in {"outside_top_n", "non_equity"}:
            raise ValueError("unsupported not-applicable reason")
        canonical_symbol = str(symbol or "").strip().upper()
        valid_symbol = bool(_SYMBOL_RE.fullmatch(canonical_symbol))
        if reason == "non_equity":
            valid_symbol = bool(_NON_EQUITY_SYMBOL_RE.fullmatch(canonical_symbol))
        if not valid_symbol:
            raise ValueError("not-applicable symbol is not canonical")
        policy_value = dict(policy or _policy_capture())
        if _sha256_json(policy_value) != policy_sha256:
            raise ValueError("not-applicable policy hash mismatch")
        observed = _as_utc(observed_at, field="observed_at")
        return cls(
            kind=OrtexOutcomeKind.NOT_APPLICABLE,
            symbol=canonical_symbol,
            requested_exchange=None,
            resolved_exchange=None,
            endpoints=(),
            short_interest_pct=None,
            cost_to_borrow=None,
            utilization=None,
            is_easy_to_borrow=None,
            provider_event_at=None,
            effective_at=None,
            received_at=observed,
            available_at=observed,
            detail_code=reason,
            policy=policy_value,
            quota_policy_sha256=policy_sha256,
        )

    def _capture_without_hash(self) -> dict[str, object]:
        return {
            "schema": _CAPTURE_SCHEMA,
            "version": _CAPTURE_VERSION,
            "kind": self.kind.value,
            "symbol": self.symbol,
            "requested_exchange": self.requested_exchange,
            "resolved_exchange": self.resolved_exchange,
            "endpoints": [endpoint.to_capture_dict() for endpoint in self.endpoints],
            "short_interest_pct": self.short_interest_pct,
            "cost_to_borrow": self.cost_to_borrow,
            "utilization": self.utilization,
            "is_easy_to_borrow": self.is_easy_to_borrow,
            "provider_event_at": _dt_text(self.provider_event_at),
            "effective_at": _dt_text(self.effective_at),
            "received_at": _dt_text(self.received_at),
            "available_at": _dt_text(self.available_at),
            "detail_code": self.detail_code,
            "policy": _thaw_json(self.policy),
            "quota_policy_sha256": self.quota_policy_sha256,
            "cache_origin_sha256": self.cache_origin_sha256,
            "cache_origin_received_at": _dt_text(
                self.cache_origin_received_at
            ),
            "cache_origin_available_at": _dt_text(
                self.cache_origin_available_at
            ),
        }

    @property
    def capture_sha256(self) -> str:
        return _sha256_json(self._capture_without_hash())

    def to_capture_dict(self) -> dict[str, object]:
        value = self._capture_without_hash()
        value["capture_sha256"] = self.capture_sha256
        return value

    def to_selection_reference_dict(self) -> dict[str, object]:
        """Compact viability-row reference; full raw bytes stay in the sealed event."""

        endpoint_refs = [
            {
                "dataset": endpoint.dataset,
                "kind": endpoint.kind.value,
                "exchange": endpoint.exchange,
                "attempt_id": endpoint.quota.attempt_id,
                "request_sha256": endpoint.request_sha256,
                "raw_response_sha256": endpoint.raw_response_sha256,
                "endpoint_capture_sha256": endpoint.capture_sha256,
                "selected_row_sha256": endpoint.selected_row_sha256,
                "http_status": endpoint.http_status,
                "provider_event_at": _dt_text(endpoint.provider_event_at),
                "effective_at": _dt_text(endpoint.effective_at),
                "received_at": _dt_text(endpoint.received_at),
                "available_at": _dt_text(endpoint.available_at),
                "value": endpoint.value,
            }
            for endpoint in self.endpoints
        ]
        value: dict[str, object] = {
            "schema": "chili.ortex.selection-reference.v1",
            "version": 1,
            "outcome_capture_sha256": self.capture_sha256,
            "kind": self.kind.value,
            "symbol": self.symbol,
            "requested_exchange": self.requested_exchange,
            "resolved_exchange": self.resolved_exchange,
            "short_interest_pct": self.short_interest_pct,
            "cost_to_borrow": self.cost_to_borrow,
            "utilization": self.utilization,
            "is_easy_to_borrow": self.is_easy_to_borrow,
            "provider_event_at": _dt_text(self.provider_event_at),
            "effective_at": _dt_text(self.effective_at),
            "received_at": _dt_text(self.received_at),
            "available_at": _dt_text(self.available_at),
            "detail_code": self.detail_code,
            "quota_policy_sha256": self.quota_policy_sha256,
            "cache_origin_sha256": self.cache_origin_sha256,
            "cache_origin_received_at": _dt_text(
                self.cache_origin_received_at
            ),
            "cache_origin_available_at": _dt_text(
                self.cache_origin_available_at
            ),
            "endpoint_refs": endpoint_refs,
        }
        value["selection_reference_sha256"] = _sha256_json(value)
        return value

    @classmethod
    def from_capture_dict(
        cls, value: object
    ) -> "OrtexShortMechanicsOutcome":
        if not isinstance(value, Mapping):
            raise ValueError("aggregate capture must be an object")
        expected = {
            "schema",
            "version",
            "kind",
            "symbol",
            "requested_exchange",
            "resolved_exchange",
            "endpoints",
            "short_interest_pct",
            "cost_to_borrow",
            "utilization",
            "is_easy_to_borrow",
            "provider_event_at",
            "effective_at",
            "received_at",
            "available_at",
            "detail_code",
            "policy",
            "quota_policy_sha256",
            "cache_origin_sha256",
            "cache_origin_received_at",
            "cache_origin_available_at",
            "capture_sha256",
        }
        if set(value) != expected:
            raise ValueError("aggregate capture fields do not match the v2 contract")
        if value["schema"] != _CAPTURE_SCHEMA or value["version"] != _CAPTURE_VERSION:
            raise ValueError("aggregate capture schema/version mismatch")
        endpoint_values = value["endpoints"]
        if not isinstance(endpoint_values, list):
            raise ValueError("aggregate endpoints must be a list")
        policy = value["policy"]
        if not isinstance(policy, Mapping):
            raise ValueError("aggregate policy must be an object")
        for field in (
            "short_interest_pct",
            "cost_to_borrow",
            "utilization",
        ):
            if value[field] is not None and (
                isinstance(value[field], bool)
                or not isinstance(value[field], (int, float))
            ):
                raise ValueError(f"aggregate {field} must be numeric or null")
        if value["is_easy_to_borrow"] is not None and not isinstance(
            value["is_easy_to_borrow"], bool
        ):
            raise ValueError(
                "aggregate is_easy_to_borrow must be a Boolean or null"
            )
        outcome = cls(
            kind=OrtexOutcomeKind(str(value["kind"])),
            symbol=str(value["symbol"]),
            requested_exchange=(
                None
                if value["requested_exchange"] is None
                else str(value["requested_exchange"])
            ),
            resolved_exchange=(
                None
                if value["resolved_exchange"] is None
                else str(value["resolved_exchange"])
            ),
            endpoints=tuple(
                OrtexEndpointOutcome.from_capture_dict(item)
                for item in endpoint_values
            ),
            short_interest_pct=(
                None
                if value["short_interest_pct"] is None
                else _finite_nonnegative(
                    value["short_interest_pct"], field="short_interest_pct"
                )
            ),
            cost_to_borrow=(
                None
                if value["cost_to_borrow"] is None
                else _finite_nonnegative(
                    value["cost_to_borrow"], field="cost_to_borrow"
                )
            ),
            utilization=(
                None
                if value["utilization"] is None
                else _finite_nonnegative(
                    value["utilization"], field="utilization"
                )
            ),
            is_easy_to_borrow=(
                None
                if value["is_easy_to_borrow"] is None
                else value["is_easy_to_borrow"]
            ),
            provider_event_at=_parse_dt(
                value["provider_event_at"],
                field="provider_event_at",
                optional=True,
            ),
            effective_at=_parse_dt(
                value["effective_at"], field="effective_at", optional=True
            ),
            received_at=_parse_dt(
                value["received_at"], field="received_at"
            ),  # type: ignore[arg-type]
            available_at=_parse_dt(
                value["available_at"], field="available_at"
            ),  # type: ignore[arg-type]
            detail_code=str(value["detail_code"]),
            policy=dict(policy),
            quota_policy_sha256=str(value["quota_policy_sha256"]),
            cache_origin_sha256=(
                None
                if value["cache_origin_sha256"] is None
                else str(value["cache_origin_sha256"])
            ),
            cache_origin_received_at=_parse_dt(
                value["cache_origin_received_at"],
                field="cache_origin_received_at",
                optional=True,
            ),
            cache_origin_available_at=_parse_dt(
                value["cache_origin_available_at"],
                field="cache_origin_available_at",
                optional=True,
            ),
        )
        maximum = int(dict(policy)["max_response_bytes"])
        for endpoint in outcome.endpoints:
            if endpoint.kind not in {
                OrtexOutcomeKind.SUCCESS,
                OrtexOutcomeKind.AUTHORITATIVE_EMPTY,
            }:
                continue
            if endpoint.http_status != 200 or endpoint.raw_response_b64 is None:
                raise ValueError("authoritative endpoint lacks a 200 raw response")
            raw = base64.b64decode(
                endpoint.raw_response_b64.encode("ascii"), validate=True
            )
            parsed = parse_ortex_response_body(
                endpoint.dataset,
                raw,
                max_response_bytes=maximum,
            )
            if (
                parsed.kind is not endpoint.kind
                or parsed.value != endpoint.value
                or parsed.provider_event_at != endpoint.provider_event_at
                or parsed.effective_at != endpoint.effective_at
                or parsed.selected_row_sha256
                != endpoint.selected_row_sha256
                or parsed.detail_code != endpoint.detail_code
            ):
                raise ValueError("endpoint body semantics mismatch")
        if value["capture_sha256"] != outcome.capture_sha256:
            raise ValueError("aggregate capture hash mismatch")
        return outcome

# Ortex v2 provider implementation.


@runtime_checkable
class OrtexOutcomeProvider(Protocol):
    @property
    def network_fallback_allowed(self) -> bool: ...

    def get_short_mechanics_outcome(
        self, *, symbol: str, exchange_hint: str | None
    ) -> OrtexShortMechanicsOutcome: ...


@runtime_checkable
class OrtexQuotaAuthorityProtocol(Protocol):
    def reserve_bundle(
        self,
        *,
        bundle_sha256: str,
        owner_token: str,
        requests: Sequence[object],
        lease_seconds: int,
    ) -> object: ...

    def begin_transport(self, permit: object) -> object: ...

    def complete_attempt(self, permit: object, **kwargs: object) -> object: ...

    def refund_unstarted(self, permit: object) -> object: ...

    def read_recent_completed(
        self, request_sha256: str, max_age_seconds: int
    ) -> object: ...


_ORTEX_OUTCOME_PROVIDER: ContextVar[OrtexOutcomeProvider | None] = ContextVar(
    "chili_ortex_outcome_provider", default=None
)
_ORTEX_QUOTA_AUTHORITY: ContextVar[OrtexQuotaAuthorityProtocol | None] = ContextVar(
    "chili_ortex_quota_authority", default=None
)


@contextmanager
def ortex_outcome_provider(
    provider: OrtexOutcomeProvider | None,
) -> Iterator[OrtexOutcomeProvider | None]:
    if provider is not None:
        if bool(getattr(provider, "network_fallback_allowed", True)):
            raise ValueError("Ortex outcome provider permits network fallback")
        if not callable(getattr(provider, "get_short_mechanics_outcome", None)):
            raise ValueError("Ortex outcome provider is malformed")
    token = _ORTEX_OUTCOME_PROVIDER.set(provider)
    try:
        yield provider
    finally:
        _ORTEX_OUTCOME_PROVIDER.reset(token)


@contextmanager
def ortex_quota_authority(
    authority: OrtexQuotaAuthorityProtocol | None,
) -> Iterator[OrtexQuotaAuthorityProtocol | None]:
    if authority is not None:
        required = (
            "reserve_bundle",
            "begin_transport",
            "complete_attempt",
            "refund_unstarted",
        )
        if any(not callable(getattr(authority, name, None)) for name in required):
            raise ValueError("Ortex quota authority is malformed")
    token = _ORTEX_QUOTA_AUTHORITY.set(authority)
    try:
        yield authority
    finally:
        _ORTEX_QUOTA_AUTHORITY.reset(token)


EASY_TO_BORROW_CTB_PCT = 1.0
_MAX_CACHE = 512
_cache = {}
_cache_lock = threading.Lock()
_credential_auth_latches: dict[tuple[str, str], float] = {}


def _clear_cache_for_tests() -> None:
    with _cache_lock:
        _cache.clear()
        _credential_auth_latches.clear()


def _credential_auth_latch_key(
    api_key: str, policy_sha256: str
) -> tuple[str, str]:
    # Process-private only: never persisted, captured, logged, or returned.
    # A rotated key or changed sealed policy therefore bypasses an old latch.
    return (_credential_auth_scope(api_key, policy_sha256), policy_sha256)


def _credential_auth_scope(api_key: str, policy_sha256: str) -> str:
    return hmac.new(
        api_key.encode("utf-8"),
        (
            "chili-ortex-auth-scope-v1:"
            f"{policy_sha256}"
        ).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()[:32]


def _credential_auth_latch_active(
    api_key: str, policy_sha256: str
) -> bool:
    key = _credential_auth_latch_key(api_key, policy_sha256)
    with _cache_lock:
        expires_at = _credential_auth_latches.get(key)
        if expires_at is None:
            return False
        if _monotonic() >= expires_at:
            _credential_auth_latches.pop(key, None)
            return False
        return True


def _install_credential_auth_latch(
    api_key: str,
    policy_sha256: str,
    *,
    ttl_seconds: float,
) -> None:
    key = _credential_auth_latch_key(api_key, policy_sha256)
    with _cache_lock:
        _credential_auth_latches.clear()
        _credential_auth_latches[key] = _monotonic() + float(ttl_seconds)


def _cache_key(
    symbol: str, exchange_hint: str | None, policy_sha256: str
) -> tuple[str, str, str]:
    return (symbol, exchange_hint or "*", policy_sha256)


def _cache_get_v2(
    symbol: str,
    exchange_hint: str | None,
    *,
    policy_sha256: str,
    observed_at: datetime,
) -> OrtexShortMechanicsOutcome | None:
    key = _cache_key(symbol, exchange_hint, policy_sha256)
    with _cache_lock:
        item = _cache.get(key)
        if item is None:
            return None
        expires_at, original = item
        if _monotonic() >= expires_at:
            _cache.pop(key, None)
            return None
    return replace(
        original,
        received_at=observed_at,
        available_at=observed_at,
        detail_code="process_cache",
        cache_origin_sha256=(
            original.cache_origin_sha256 or original.capture_sha256
        ),
        cache_origin_received_at=(
            original.cache_origin_received_at or original.received_at
        ),
        cache_origin_available_at=(
            original.cache_origin_available_at or original.available_at
        ),
    )


def _cache_set_v2(
    symbol: str,
    exchange_hint: str | None,
    *,
    policy_sha256: str,
    outcome: OrtexShortMechanicsOutcome,
    ttl_seconds: int,
) -> None:
    if outcome.kind not in {
        OrtexOutcomeKind.SUCCESS,
        OrtexOutcomeKind.AUTHORITATIVE_EMPTY,
        OrtexOutcomeKind.UNSUPPORTED_SYMBOL_OR_EXCHANGE,
    }:
        return
    if not outcome.endpoints or any(
        endpoint.kind
        not in {
            OrtexOutcomeKind.SUCCESS,
            OrtexOutcomeKind.AUTHORITATIVE_EMPTY,
            OrtexOutcomeKind.UNSUPPORTED_SYMBOL_OR_EXCHANGE,
        }
        for endpoint in outcome.endpoints
    ):
        return
    origin_available_at = (
        outcome.cache_origin_available_at or outcome.available_at
    )
    remaining_ttl = float(ttl_seconds) - max(
        0.0,
        (
            _utcnow()
            - _as_utc(
                origin_available_at,
                field="cache_origin_available_at",
            )
        ).total_seconds(),
    )
    if remaining_ttl <= 0.0:
        return
    key = _cache_key(symbol, exchange_hint, policy_sha256)
    with _cache_lock:
        if len(_cache) >= _MAX_CACHE:
            now = _monotonic()
            for existing_key, (expiry, _value) in tuple(_cache.items()):
                if expiry <= now:
                    _cache.pop(existing_key, None)
            if len(_cache) >= _MAX_CACHE:
                oldest = min(_cache, key=lambda item: _cache[item][0])
                _cache.pop(oldest, None)
        _cache[key] = (_monotonic() + remaining_ttl, outcome)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _open_no_redirect(
    request: urllib.request.Request, timeout: float
) -> object:
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


@dataclass(frozen=True, slots=True)
class _RequestPlan:
    dataset: str
    symbol: str
    exchange: str
    path: str
    url: str
    query_sha256: str
    request_sha256: str


@dataclass(frozen=True, slots=True)
class OrtexParsedResponse:
    kind: OrtexOutcomeKind
    value: float | None
    provider_event_at: datetime | None
    effective_at: datetime | None
    selected_row_sha256: str | None
    detail_code: str


_PROVIDER_CLOCK_KEYS = (
    "updatedAt",
    "updated_at",
    "providerEventAt",
    "provider_event_at",
    "timestamp",
    "asOf",
    "as_of",
)
_EFFECTIVE_CLOCK_KEYS = (
    "effectiveDate",
    "effective_date",
    "tradingDate",
    "trading_date",
    "settlementDate",
    "settlement_date",
    "date",
)
_DATASET_VALUE_FIELDS = {
    "short_interest": "shortInterestPcFreeFloat",
    "cost_to_borrow": "costToBorrowAll",
}
_SECRET_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "credential",
    "password",
    "access_token",
)


def _json_no_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant {value}")


def _reject_secret_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in _SECRET_KEY_PARTS):
                raise ValueError("provider response contains a credential-like field")
            _reject_secret_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_fields(item)


def _parse_provider_clock(value: object, *, field: str) -> datetime:
    if isinstance(value, bool):
        raise ValueError(f"{field} is not a clock")
    if isinstance(value, (int, float)):
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError(f"{field} is non-finite")
        if abs(numeric) > 10_000_000_000:
            numeric /= 1000.0
        return datetime.fromtimestamp(numeric, tz=_UTC)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is not a clock")
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed_date = date.fromisoformat(text)
        return datetime(
            parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=_UTC
        )
    return _parse_dt(text, field=field)  # type: ignore[return-value]


def _first_row_clock(
    row: Mapping[str, object], keys: Sequence[str], *, field: str
) -> datetime | None:
    for key in keys:
        if key in row and row[key] is not None:
            return _parse_provider_clock(row[key], field=f"{field}.{key}")
    return None


def parse_ortex_response_body(
    dataset: str,
    raw_bytes: bytes,
    *,
    max_response_bytes: int | None = None,
) -> OrtexParsedResponse:
    """Pure authoritative Ortex-row parser shared by live, capture, and replay."""

    if dataset not in _ENDPOINT_DATASETS:
        raise ValueError("unsupported Ortex dataset")
    if not isinstance(raw_bytes, bytes):
        raise ValueError("raw Ortex response must be bytes")
    maximum = (
        int(max_response_bytes)
        if max_response_bytes is not None
        else int(_policy_capture()["max_response_bytes"])
    )
    if not 1024 <= maximum <= 10 * 1024 * 1024:
        raise ValueError("invalid Ortex response-size policy")
    if len(raw_bytes) > maximum:
        raise ValueError("raw Ortex response exceeds policy maximum")
    try:
        payload = json.loads(
            raw_bytes.decode("utf-8", "strict"),
            parse_constant=_json_no_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("malformed Ortex JSON response") from exc
    _reject_secret_fields(payload)
    if not isinstance(payload, dict):
        raise ValueError("Ortex response root is not an object")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Ortex response rows is not a list")
    if not rows:
        return OrtexParsedResponse(
            kind=OrtexOutcomeKind.AUTHORITATIVE_EMPTY,
            value=None,
            provider_event_at=None,
            effective_at=None,
            selected_row_sha256=None,
            detail_code="provider_empty_rows",
        )
    candidates = []
    floor = datetime.min.replace(tzinfo=_UTC)
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("Ortex row is not an object")
        row_dict = dict(row)
        row_sha256 = _sha256_json(row_dict)
        provider_event_at = _first_row_clock(
            row_dict, _PROVIDER_CLOCK_KEYS, field="provider_event_at"
        )
        effective_at = _first_row_clock(
            row_dict, _EFFECTIVE_CLOCK_KEYS, field="effective_at"
        )
        if provider_event_at is None and effective_at is None:
            raise ValueError("Ortex row has no authoritative dataset clock")
        candidates.append(
            (
                effective_at or provider_event_at or floor,
                provider_event_at or effective_at or floor,
                row_sha256,
                row_dict,
                provider_event_at,
                effective_at,
            )
        )
    (
        _sort_effective,
        _sort_provider,
        row_sha256,
        selected,
        provider_event_at,
        effective_at,
    ) = max(candidates, key=lambda item: (item[0], item[1], item[2]))
    field = _DATASET_VALUE_FIELDS[dataset]
    if field not in selected:
        raise ValueError("latest Ortex row lacks the dataset value field")
    if selected[field] is None:
        return OrtexParsedResponse(
            kind=OrtexOutcomeKind.AUTHORITATIVE_EMPTY,
            value=None,
            provider_event_at=provider_event_at,
            effective_at=effective_at,
            selected_row_sha256=row_sha256,
            detail_code="provider_null_value",
        )
    parsed = _finite_nonnegative(selected[field], field=field)
    return OrtexParsedResponse(
        kind=OrtexOutcomeKind.SUCCESS,
        value=parsed,
        provider_event_at=provider_event_at,
        effective_at=effective_at,
        selected_row_sha256=row_sha256,
        detail_code="provider_row",
    )


def _request_plan(
    *,
    dataset: str,
    symbol: str,
    exchange: str,
    policy_sha256: str,
) -> _RequestPlan:
    path = _endpoint_path(exchange, symbol, dataset)
    query = {
        "provider": "ortex",
        "dataset": dataset,
        "symbol": symbol,
        "exchange": exchange,
        "path": path,
    }
    query_sha256 = _sha256_json(query)
    request_sha256 = _sha256_json(
        {
            "method": "GET",
            "origin": f"{_ORTEX_SCHEME}://{_ORTEX_HOST}",
            "api_prefix": _ORTEX_API_PREFIX,
            "query_sha256": query_sha256,
            "quota_policy_sha256": policy_sha256,
        }
    )
    return _RequestPlan(
        dataset=dataset,
        symbol=symbol,
        exchange=exchange,
        path=path,
        url=f"{_ORTEX_BASE}{path}",
        query_sha256=query_sha256,
        request_sha256=request_sha256,
    )


def _quota_request_spec(plan: _RequestPlan) -> object:
    from .ortex_quota import OrtexRequestSpec

    return OrtexRequestSpec(
        request_sha256=plan.request_sha256,
        endpoint=f"{_ORTEX_API_PREFIX}{plan.path}",
        symbol=plan.symbol,
    )


def _quota_provider_outcome(kind: str) -> object:
    from .ortex_quota import OrtexProviderOutcome

    return OrtexProviderOutcome[kind]


def _month_start_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        result = _as_utc(value, field="month_start")
    elif isinstance(value, date):
        result = datetime(value.year, value.month, value.day, tzinfo=_UTC)
    else:
        raise ValueError("quota month_start is malformed")
    return datetime(result.year, result.month, 1, tzinfo=_UTC)


def _quota_provenance(
    *,
    permit_or_record: object,
    completion: object | None,
    policy: Mapping[str, object],
) -> OrtexQuotaProvenance:
    record = getattr(completion, "attempt", None) if completion is not None else None
    source = record or permit_or_record
    owner_token = str(getattr(source, "owner_token"))
    completion_kind = (
        _enum_name(getattr(completion, "kind", "COMPLETED"))
        if completion is not None
        else "COMPLETED"
    )
    backoff_until = getattr(source, "backoff_until", None)
    if backoff_until is None and completion is not None:
        backoff_until = getattr(completion, "retry_at", None)
    return OrtexQuotaProvenance(
        attempt_id=str(getattr(source, "attempt_id")),
        bundle_sha256=str(getattr(source, "bundle_sha256")),
        bundle_index=int(getattr(source, "bundle_index", 0)),
        owner_token_sha256=_sha256_bytes(owner_token.encode("utf-8")),
        request_sha256=str(getattr(source, "request_sha256")),
        month_start=_month_start_datetime(getattr(source, "month_start")),
        quota_used_after=int(getattr(source, "quota_used_after")),
        monthly_limit=int(
            getattr(
                source,
                "monthly_limit",
                policy["monthly_limit"],
            )
        ),
        reserved_at=_as_utc(
            getattr(source, "reserved_at"), field="reserved_at"
        ),
        lease_expires_at=_as_utc(
            getattr(source, "lease_expires_at"), field="lease_expires_at"
        ),
        transport_committed=True,
        completion_kind=completion_kind,
        backoff_until=(
            None
            if backoff_until is None
            else _as_utc(backoff_until, field="backoff_until")
        ),
    )


@dataclass(frozen=True, slots=True)
class _TransportResult:
    kind: OrtexOutcomeKind
    quota_outcome_name: str
    http_status: int | None
    raw: bytes | None
    provider_event_at: datetime | None
    effective_at: datetime | None
    received_at: datetime
    available_at: datetime
    selected_row_sha256: str | None
    value: float | None
    detail_code: str
    backoff_until: datetime | None


def _retry_after(
    headers: object, *, observed_at: datetime, maximum_seconds: float
) -> datetime | None:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    raw = getter("Retry-After")
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        seconds = float(text)
        if not math.isfinite(seconds) or seconds < 0:
            return None
        return observed_at + timedelta(seconds=min(seconds, maximum_seconds))
    except ValueError:
        try:
            parsed = _parse_dt(text, field="Retry-After")
        except ValueError:
            try:
                parsed = parsedate_to_datetime(text)
            except (TypeError, ValueError, OverflowError):
                return None
            if parsed.tzinfo is None:
                return None
            parsed = parsed.astimezone(_UTC)
        upper = observed_at + timedelta(seconds=maximum_seconds)
        return max(observed_at, min(parsed, upper))  # type: ignore[arg-type]


def _http_kind(status: int) -> tuple[OrtexOutcomeKind, str, str]:
    if status in {401, 403}:
        return OrtexOutcomeKind.AUTH_REJECTED, "AUTH_ERROR", "http_auth_rejected"
    if status == 404:
        return (
            OrtexOutcomeKind.UNSUPPORTED_SYMBOL_OR_EXCHANGE,
            "NOT_FOUND",
            "http_not_found",
        )
    if status == 429:
        return OrtexOutcomeKind.RATE_LIMITED, "RATE_LIMITED", "http_rate_limited"
    if status in {408, 425} or 500 <= status <= 599:
        return (
            OrtexOutcomeKind.TRANSIENT_UNAVAILABLE,
            "TRANSIENT_HTTP",
            "http_transient",
        )
    if 300 <= status <= 399:
        return (
            OrtexOutcomeKind.CONTRACT_MISMATCH,
            "PERMANENT_ERROR",
            "redirect_rejected",
        )
    return (
        OrtexOutcomeKind.PERMANENT_REJECTED,
        "PERMANENT_ERROR",
        "http_permanent_rejected",
    )


def _bounded_read(response: object, *, maximum: int) -> tuple[bytes | None, bool]:
    reader = getattr(response, "read", None)
    if not callable(reader):
        raise ValueError("HTTP response has no byte reader")
    raw = reader(maximum + 1)
    if not isinstance(raw, bytes):
        raise ValueError("HTTP response body is not bytes")
    if len(raw) > maximum:
        return None, True
    return raw, False


def _perform_http(
    plan: _RequestPlan,
    *,
    api_key: str,
    policy: Mapping[str, object],
) -> _TransportResult:
    request = urllib.request.Request(
        plan.url,
        headers={_ORTEX_KEY_HEADER: api_key},
        method="GET",
    )
    parsed_url = urllib.parse.urlsplit(request.full_url)
    if (
        parsed_url.scheme != _ORTEX_SCHEME
        or parsed_url.netloc != _ORTEX_HOST
        or parsed_url.path != f"{_ORTEX_API_PREFIX}{plan.path}"
        or parsed_url.query
        or parsed_url.fragment
    ):
        now = _utcnow()
        return _TransportResult(
            kind=OrtexOutcomeKind.CONTRACT_MISMATCH,
            quota_outcome_name="PERMANENT_ERROR",
            http_status=None,
            raw=None,
            provider_event_at=None,
            effective_at=None,
            received_at=now,
            available_at=now,
            selected_row_sha256=None,
            value=None,
            detail_code="request_origin_contract_mismatch",
            backoff_until=None,
        )


    maximum = int(policy["max_response_bytes"])
    timeout = float(policy["request_timeout_seconds"])
    try:
        with _open_no_redirect(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            final_url = str(getattr(response, "geturl")())
            raw, oversized = _bounded_read(response, maximum=maximum)
            received = _utcnow()
            headers = getattr(response, "headers", None)
            if final_url != request.full_url:
                return _TransportResult(
                    kind=OrtexOutcomeKind.CONTRACT_MISMATCH,
                    quota_outcome_name="PERMANENT_ERROR",
                    http_status=status,
                    raw=raw,
                    provider_event_at=None,
                    effective_at=None,
                    received_at=received,
                    available_at=received,
                    selected_row_sha256=None,
                    value=None,
                    detail_code="response_origin_contract_mismatch",
                    backoff_until=None,
                )
            if oversized:
                return _TransportResult(
                    kind=OrtexOutcomeKind.MALFORMED_RESPONSE,
                    quota_outcome_name="MALFORMED",
                    http_status=status,
                    raw=None,
                    provider_event_at=None,
                    effective_at=None,
                    received_at=received,
                    available_at=received,
                    selected_row_sha256=None,
                    value=None,
                    detail_code="response_too_large",
                    backoff_until=None,
                )
            if raw is not None and api_key.encode("utf-8") in raw:
                return _TransportResult(
                    kind=OrtexOutcomeKind.MALFORMED_RESPONSE,
                    quota_outcome_name="MALFORMED",
                    http_status=status,
                    raw=None,
                    provider_event_at=None,
                    effective_at=None,
                    received_at=received,
                    available_at=received,
                    selected_row_sha256=None,
                    value=None,
                    detail_code="response_contains_credential_material",
                    backoff_until=None,
                )
            if status != 200:
                kind, quota_kind, detail = _http_kind(status)
                return _TransportResult(
                    kind=kind,
                    quota_outcome_name=quota_kind,
                    http_status=status,
                    raw=raw,
                    provider_event_at=None,
                    effective_at=None,
                    received_at=received,
                    available_at=received,
                    selected_row_sha256=None,
                    value=None,
                    detail_code=detail,
                    backoff_until=_retry_after(
                        headers,
                        observed_at=received,
                        maximum_seconds=float(policy["backoff_max_seconds"]),
                    ),
                )
            try:
                parsed = parse_ortex_response_body(
                    plan.dataset,
                    raw or b"",
                    max_response_bytes=maximum,
                )
            except ValueError:
                return _TransportResult(
                    kind=OrtexOutcomeKind.MALFORMED_RESPONSE,
                    quota_outcome_name="MALFORMED",
                    http_status=status,
                    raw=raw,
                    provider_event_at=None,
                    effective_at=None,
                    received_at=received,
                    available_at=received,
                    selected_row_sha256=None,
                    value=None,
                    detail_code="malformed_response",
                    backoff_until=None,
                )
            if any(
                value is not None and value > received
                for value in (
                    parsed.provider_event_at,
                    parsed.effective_at,
                )
            ):
                return _TransportResult(
                    kind=OrtexOutcomeKind.MALFORMED_RESPONSE,
                    quota_outcome_name="MALFORMED",
                    http_status=status,
                    raw=None,
                    provider_event_at=None,
                    effective_at=None,
                    received_at=received,
                    available_at=received,
                    selected_row_sha256=None,
                    value=None,
                    detail_code="response_clock_from_future",
                    backoff_until=None,
                )
            return _TransportResult(
                kind=parsed.kind,
                quota_outcome_name=(
                    "SUCCESS"
                    if parsed.kind is OrtexOutcomeKind.SUCCESS
                    else "AUTHORITATIVE_EMPTY"
                ),
                http_status=status,
                raw=raw,
                provider_event_at=parsed.provider_event_at,
                effective_at=parsed.effective_at,
                received_at=received,
                available_at=received,
                selected_row_sha256=parsed.selected_row_sha256,
                value=parsed.value,
                detail_code=parsed.detail_code,
                backoff_until=None,
            )
    except urllib.error.HTTPError as exc:
        received = _utcnow()
        try:
            raw, oversized = _bounded_read(exc, maximum=maximum)
        except Exception:
            raw, oversized = None, False
        status = int(exc.code)
        if str(exc.geturl()) != request.full_url:
            kind, quota_kind, detail = (
                OrtexOutcomeKind.CONTRACT_MISMATCH,
                "PERMANENT_ERROR",
                "response_origin_contract_mismatch",
            )
        else:
            kind, quota_kind, detail = _http_kind(status)
        if oversized:
            kind, quota_kind, detail, raw = (
                OrtexOutcomeKind.MALFORMED_RESPONSE,
                "MALFORMED",
                "response_too_large",
                None,
            )
        return _TransportResult(
            kind=kind,
            quota_outcome_name=quota_kind,
            http_status=status,
            raw=raw,
            provider_event_at=None,
            effective_at=None,
            received_at=received,
            available_at=received,
            selected_row_sha256=None,
            value=None,
            detail_code=detail,
            backoff_until=_retry_after(
                getattr(exc, "headers", None),
                observed_at=received,
                maximum_seconds=float(policy["backoff_max_seconds"]),
            ),
        )
    except (TimeoutError, urllib.error.URLError, OSError):
        received = _utcnow()
        return _TransportResult(
            kind=OrtexOutcomeKind.TRANSIENT_UNAVAILABLE,
            quota_outcome_name="TIMEOUT",
            http_status=None,
            raw=None,
            provider_event_at=None,
            effective_at=None,
            received_at=received,
            available_at=received,
            selected_row_sha256=None,
            value=None,
            detail_code="transport_transient",
            backoff_until=None,
        )
    except Exception:
        received = _utcnow()
        return _TransportResult(
            kind=OrtexOutcomeKind.CONTRACT_MISMATCH,
            quota_outcome_name="PERMANENT_ERROR",
            http_status=None,
            raw=None,
            provider_event_at=None,
            effective_at=None,
            received_at=received,
            available_at=received,
            selected_row_sha256=None,
            value=None,
            detail_code="transport_contract_mismatch",
            backoff_until=None,
        )


def _aggregate_from_endpoints(
    *,
    symbol: str,
    requested_exchange: str | None,
    resolved_exchange: str | None,
    endpoints: Sequence[OrtexEndpointOutcome],
    policy: Mapping[str, object],
    observed_at: datetime | None = None,
    forced_kind: OrtexOutcomeKind | None = None,
    detail_code: str | None = None,
    cache_origin_sha256: str | None = None,
    cache_origin_received_at: datetime | None = None,
    cache_origin_available_at: datetime | None = None,
) -> OrtexShortMechanicsOutcome:
    relevant = [
        endpoint
        for endpoint in endpoints
        if resolved_exchange is None or endpoint.exchange == resolved_exchange
    ]
    latest_by_dataset = {
        dataset: next(
            (
                endpoint
                for endpoint in reversed(relevant)
                if endpoint.dataset == dataset
            ),
            None,
        )
        for dataset in _ENDPOINT_DATASETS
    }
    complete_authoritative = (
        all(latest_by_dataset.values())
        and all(
            endpoint.kind
            in {
                OrtexOutcomeKind.SUCCESS,
                OrtexOutcomeKind.AUTHORITATIVE_EMPTY,
            }
            for endpoint in latest_by_dataset.values()
            if endpoint is not None
        )
    )
    si_endpoint = latest_by_dataset["short_interest"]
    ctb_endpoint = latest_by_dataset["cost_to_borrow"]
    required_endpoint_not_found = (
        not complete_authoritative
        and any(
            endpoint.kind
            is OrtexOutcomeKind.UNSUPPORTED_SYMBOL_OR_EXCHANGE
            for endpoint in relevant
        )
        and any(
            endpoint.kind
            is not OrtexOutcomeKind.UNSUPPORTED_SYMBOL_OR_EXCHANGE
            for endpoint in relevant
        )
    )
    si = (
        si_endpoint.value
        if complete_authoritative
        and si_endpoint is not None
        and si_endpoint.kind is OrtexOutcomeKind.SUCCESS
        else None
    )
    ctb = (
        ctb_endpoint.value
        if complete_authoritative
        and ctb_endpoint is not None
        and ctb_endpoint.kind is OrtexOutcomeKind.SUCCESS
        else None
    )
    if forced_kind is not None:
        kind = forced_kind
    elif required_endpoint_not_found:
        kind = OrtexOutcomeKind.CONTRACT_MISMATCH
    elif not complete_authoritative:
        kind = next(
            (
                endpoint.kind
                for endpoint in relevant
                if endpoint.kind
                not in {
                    OrtexOutcomeKind.SUCCESS,
                    OrtexOutcomeKind.AUTHORITATIVE_EMPTY,
                }
            ),
            OrtexOutcomeKind.CONTRACT_MISMATCH,
        )
    elif si is not None or ctb is not None:
        kind = OrtexOutcomeKind.SUCCESS
    else:
        kind = OrtexOutcomeKind.AUTHORITATIVE_EMPTY
    if kind is not OrtexOutcomeKind.SUCCESS:
        si = None
        ctb = None
    if detail_code is None:
        if required_endpoint_not_found:
            detail_code = "required_endpoint_not_found"
        elif kind is OrtexOutcomeKind.SUCCESS:
            detail_code = (
                "provider_success"
                if all(
                    endpoint.kind
                    in {
                        OrtexOutcomeKind.SUCCESS,
                        OrtexOutcomeKind.AUTHORITATIVE_EMPTY,
                    }
                    for endpoint in relevant
                )
                else "partial_provider_success"
            )
        elif kind is OrtexOutcomeKind.AUTHORITATIVE_EMPTY:
            detail_code = "provider_authoritative_empty"
        else:
            detail_code = next(
                (
                    endpoint.detail_code
                    for endpoint in relevant
                    if endpoint.kind is kind
                ),
                "aggregate_contract_mismatch",
            )
    provider_clocks = [
        endpoint.provider_event_at
        for endpoint in relevant
        if endpoint.provider_event_at is not None
    ]
    effective_clocks = [
        endpoint.effective_at
        for endpoint in relevant
        if endpoint.effective_at is not None
    ]
    source_received = (
        max(endpoint.received_at for endpoint in endpoints)
        if endpoints
        else observed_at or _utcnow()
    )
    source_available = (
        max(endpoint.available_at for endpoint in endpoints)
        if endpoints
        else observed_at or source_received
    )
    return OrtexShortMechanicsOutcome(
        kind=kind,
        symbol=symbol,
        requested_exchange=requested_exchange,
        resolved_exchange=resolved_exchange,
        endpoints=tuple(endpoints),
        short_interest_pct=si,
        cost_to_borrow=ctb,
        utilization=None,
        is_easy_to_borrow=(
            None if ctb is None else bool(ctb <= EASY_TO_BORROW_CTB_PCT)
        ),
        provider_event_at=max(provider_clocks) if provider_clocks else None,
        effective_at=max(effective_clocks) if effective_clocks else None,
        received_at=observed_at or source_received,
        available_at=observed_at or source_available,
        detail_code=detail_code,
        policy=policy,
        quota_policy_sha256=_sha256_json(policy),
        cache_origin_sha256=cache_origin_sha256,
        cache_origin_received_at=cache_origin_received_at,
        cache_origin_available_at=cache_origin_available_at,
    )


def _simple_outcome(
    *,
    kind: OrtexOutcomeKind,
    symbol: str,
    requested_exchange: str | None,
    detail_code: str,
    policy: Mapping[str, object],
    endpoints: Sequence[OrtexEndpointOutcome] = (),
) -> OrtexShortMechanicsOutcome:
    observed = _utcnow()
    return _aggregate_from_endpoints(
        symbol=symbol,
        requested_exchange=requested_exchange,
        resolved_exchange=None,
        endpoints=endpoints,
        policy=policy,
        observed_at=observed,
        forced_kind=kind,
        detail_code=detail_code,
    )


_RECORD_OUTCOME_MAP = {
    "SUCCESS": OrtexOutcomeKind.SUCCESS,
    "AUTHORITATIVE_EMPTY": OrtexOutcomeKind.AUTHORITATIVE_EMPTY,
    "NOT_FOUND": OrtexOutcomeKind.UNSUPPORTED_SYMBOL_OR_EXCHANGE,
    "AUTH_ERROR": OrtexOutcomeKind.AUTH_REJECTED,
    "RATE_LIMITED": OrtexOutcomeKind.RATE_LIMITED,
    "TRANSIENT_HTTP": OrtexOutcomeKind.TRANSIENT_UNAVAILABLE,
    "TIMEOUT": OrtexOutcomeKind.TRANSIENT_UNAVAILABLE,
    "MALFORMED": OrtexOutcomeKind.MALFORMED_RESPONSE,
    "PERMANENT_ERROR": OrtexOutcomeKind.PERMANENT_REJECTED,
}


def _plan_from_completed_record(
    record: object, *, policy_sha256: str
) -> _RequestPlan:
    endpoint = str(getattr(record, "endpoint"))
    if not endpoint.startswith(f"{_ORTEX_API_PREFIX}/stock/"):
        raise ValueError("completed attempt endpoint is not canonical")
    path = endpoint[len(_ORTEX_API_PREFIX) :]
    segments = path.strip("/").split("/")
    if len(segments) < 4 or segments[0] != "stock":
        raise ValueError("completed attempt endpoint shape is invalid")
    exchange = segments[1]
    symbol = segments[2]
    suffix = "/".join(segments[3:])
    if suffix == "short_interest":
        dataset = "short_interest"
    elif suffix == "ctb/all":
        dataset = "cost_to_borrow"
    else:
        raise ValueError("completed attempt dataset is invalid")
    plan = _request_plan(
        dataset=dataset,
        symbol=symbol,
        exchange=exchange,
        policy_sha256=policy_sha256,
    )
    if str(getattr(record, "request_sha256")) != plan.request_sha256:
        raise ValueError("completed attempt request hash mismatch")
    if str(getattr(record, "symbol")) != symbol:
        raise ValueError("completed attempt symbol mismatch")
    return plan


def _endpoint_from_completed_record(
    record: object,
    *,
    policy: Mapping[str, object],
) -> OrtexEndpointOutcome:
    policy_sha256 = _sha256_json(policy)
    if str(getattr(record, "status")) != "completed":
        raise ValueError("attempt is not durably completed")
    plan = _plan_from_completed_record(record, policy_sha256=policy_sha256)
    outcome_name = _enum_name(getattr(record, "provider_outcome"))
    kind = _RECORD_OUTCOME_MAP.get(outcome_name)
    if kind is None:
        raise ValueError("completed attempt provider outcome is unsupported")
    raw_b64 = getattr(record, "raw_response_body_b64", None)
    raw_sha256 = getattr(record, "response_sha256", None)
    raw: bytes | None = None
    if raw_b64 is not None:
        try:
            raw = base64.b64decode(str(raw_b64).encode("ascii"), validate=True)
        except Exception as exc:
            raise ValueError("completed attempt raw response is invalid") from exc
        if len(raw) > int(policy["max_response_bytes"]):
            raise ValueError("completed attempt raw response exceeds policy")
        if _sha256_bytes(raw) != str(raw_sha256):
            raise ValueError("completed attempt raw response hash mismatch")
    elif raw_sha256 is not None:
        raise ValueError("completed attempt raw response/hash mismatch")
    parsed: OrtexParsedResponse | None = None
    if kind in {
        OrtexOutcomeKind.SUCCESS,
        OrtexOutcomeKind.AUTHORITATIVE_EMPTY,
    }:
        if raw is None:
            raise ValueError("cacheable attempt lacks raw response")
        parsed = parse_ortex_response_body(
            plan.dataset,
            raw,
            max_response_bytes=int(policy["max_response_bytes"]),
        )
        if parsed.kind is not kind:
            raise ValueError("completed attempt typed outcome disagrees with body")
    elif kind is OrtexOutcomeKind.UNSUPPORTED_SYMBOL_OR_EXCHANGE:
        if (
            raw is not None
            or getattr(record, "http_status", None) != 404
            or getattr(record, "provider_event_at", None) is not None
            or getattr(record, "effective_at", None) is not None
        ):
            raise ValueError("completed not-found attempt is not canonical")
    received_at = _as_utc(
        getattr(record, "received_at"), field="received_at"
    )
    available_at = _as_utc(
        getattr(record, "available_at"), field="available_at"
    )
    stored_provider_at = getattr(record, "provider_event_at", None)
    stored_effective_at = getattr(record, "effective_at", None)
    provider_event_at = (
        None
        if stored_provider_at is None
        else _as_utc(stored_provider_at, field="provider_event_at")
    )
    effective_at = (
        None
        if stored_effective_at is None
        else _as_utc(stored_effective_at, field="effective_at")
    )
    if parsed is not None and (
        parsed.provider_event_at != provider_event_at
        or parsed.effective_at != effective_at
    ):
        raise ValueError("completed attempt dataset clocks disagree with body")
    detail = (
        parsed.detail_code
        if parsed is not None
        else {
            OrtexOutcomeKind.UNSUPPORTED_SYMBOL_OR_EXCHANGE: "http_not_found",
            OrtexOutcomeKind.AUTH_REJECTED: "http_auth_rejected",
            OrtexOutcomeKind.RATE_LIMITED: "http_rate_limited",
            OrtexOutcomeKind.TRANSIENT_UNAVAILABLE: "http_transient",
            OrtexOutcomeKind.MALFORMED_RESPONSE: "malformed_response",
            OrtexOutcomeKind.PERMANENT_REJECTED: "http_permanent_rejected",
        }[kind]
    )
    return OrtexEndpointOutcome(
        kind=kind,
        dataset=plan.dataset,
        symbol=plan.symbol,
        exchange=plan.exchange,
        request_path=plan.path,
        request_sha256=plan.request_sha256,
        query_sha256=plan.query_sha256,
        quota_policy_sha256=policy_sha256,
        http_status=(
            None
            if getattr(record, "http_status", None) is None
            else int(getattr(record, "http_status"))
        ),
        provider_event_at=provider_event_at,
        effective_at=effective_at,
        received_at=received_at,
        available_at=available_at,
        raw_response_b64=(None if raw_b64 is None else str(raw_b64)),
        raw_response_sha256=(
            None if raw_sha256 is None else str(raw_sha256)
        ),
        selected_row_sha256=(
            None if parsed is None else parsed.selected_row_sha256
        ),
        value=(None if parsed is None else parsed.value),
        detail_code=detail,
        quota=_quota_provenance(
            permit_or_record=record,
            completion=None,
            policy=policy,
        ),
    )


def ortex_outcome_from_completed_attempts(
    *,
    symbol: str,
    requested_exchange: str | None,
    records: Sequence[object],
    policy: Mapping[str, object],
    observed_at: datetime,
) -> OrtexShortMechanicsOutcome:
    """Pure DB-row reconstruction; performs no provider, broker, or DB I/O."""

    canonical_symbol = str(symbol or "").strip().upper()
    if not _SYMBOL_RE.fullmatch(canonical_symbol):
        raise ValueError("reconstructed symbol is not canonical")
    policy_dict = json.loads(_canonical_json_bytes(policy).decode("utf-8"))
    endpoints = tuple(
        _endpoint_from_completed_record(record, policy=policy_dict)
        for record in records
    )
    if not endpoints or any(
        endpoint.symbol != canonical_symbol for endpoint in endpoints
    ):
        raise ValueError("completed attempts do not bind the requested symbol")
    usable_exchanges = {
        endpoint.exchange
        for endpoint in endpoints
        if endpoint.kind
        in {
            OrtexOutcomeKind.SUCCESS,
            OrtexOutcomeKind.AUTHORITATIVE_EMPTY,
        }
    }
    if len(usable_exchanges) > 1:
        raise ValueError("completed attempts bind multiple resolved exchanges")
    resolved_exchange = next(iter(usable_exchanges), None)
    if (
        requested_exchange is not None
        and resolved_exchange is not None
        and requested_exchange != resolved_exchange
    ):
        raise ValueError("completed attempts violate requested exchange")
    observed = _as_utc(observed_at, field="observed_at")
    if observed < max(endpoint.available_at for endpoint in endpoints):
        raise ValueError("observed_at predates completed attempt availability")
    return _aggregate_from_endpoints(
        symbol=canonical_symbol,
        requested_exchange=requested_exchange,
        resolved_exchange=resolved_exchange,
        endpoints=endpoints,
        policy=policy_dict,
    )


def _complete_endpoint(
    *,
    authority: OrtexQuotaAuthorityProtocol,
    permit: object,
    plan: _RequestPlan,
    result: _TransportResult,
    policy: Mapping[str, object],
) -> OrtexEndpointOutcome:
    authoritative_result = result.kind in {
        OrtexOutcomeKind.SUCCESS,
        OrtexOutcomeKind.AUTHORITATIVE_EMPTY,
    }
    retained_raw = result.raw if authoritative_result else None
    raw_b64 = (
        None
        if retained_raw is None
        else base64.b64encode(retained_raw).decode("ascii")
    )
    raw_sha256 = (
        None if retained_raw is None else _sha256_bytes(retained_raw)
    )
    try:
        completion = authority.complete_attempt(
            permit,
            outcome=_quota_provider_outcome(result.quota_outcome_name),
            http_status=result.http_status,
            backoff_until=result.backoff_until,
            raw_response_body_b64=raw_b64,
            response_sha256=raw_sha256,
            provider_event_at=result.provider_event_at,
            effective_at=result.effective_at,
            received_at=result.received_at,
            available_at=result.available_at,
        )
    except Exception:
        completion = SimpleNamespace(
            kind="DB_UNAVAILABLE",
            attempt=None,
            retry_at=result.backoff_until,
        )
    completion_kind = _enum_name(getattr(completion, "kind", "DB_UNAVAILABLE"))
    if completion_kind != "COMPLETED":
        endpoint_kind = OrtexOutcomeKind.QUOTA_AUTHORITY_UNAVAILABLE
        detail_code = "quota_completion_unavailable"
    else:
        endpoint_kind = result.kind
        detail_code = result.detail_code
    endpoint_raw_b64 = (
        raw_b64
        if endpoint_kind
        in {
            OrtexOutcomeKind.SUCCESS,
            OrtexOutcomeKind.AUTHORITATIVE_EMPTY,
        }
        else None
    )
    endpoint_raw_sha256 = (
        raw_sha256 if endpoint_raw_b64 is not None else None
    )
    return OrtexEndpointOutcome(
        kind=endpoint_kind,
        dataset=plan.dataset,
        symbol=plan.symbol,
        exchange=plan.exchange,
        request_path=plan.path,
        request_sha256=plan.request_sha256,
        query_sha256=plan.query_sha256,
        quota_policy_sha256=_sha256_json(policy),
        http_status=result.http_status,
        provider_event_at=result.provider_event_at,
        effective_at=result.effective_at,
        received_at=result.received_at,
        available_at=result.available_at,
        raw_response_b64=endpoint_raw_b64,
        raw_response_sha256=endpoint_raw_sha256,
        selected_row_sha256=result.selected_row_sha256,
        value=(result.value if endpoint_kind is OrtexOutcomeKind.SUCCESS else None),
        detail_code=detail_code,
        quota=_quota_provenance(
            permit_or_record=permit,
            completion=completion,
            policy=policy,
        ),
    )


@dataclass(frozen=True, slots=True)
class _FetchPlansResult:
    endpoints: tuple[OrtexEndpointOutcome, ...]
    blocked_kind: OrtexOutcomeKind | None = None
    blocked_detail: str | None = None
    all_from_durable_cache: bool = False


def _quota_block(kind: str) -> tuple[OrtexOutcomeKind, str]:
    if kind == "QUOTA_EXHAUSTED":
        return (
            OrtexOutcomeKind.MONTHLY_QUOTA_EXHAUSTED,
            "monthly_quota_exhausted",
        )
    if kind in {
        "BACKOFF_ACTIVE",
        "PACING_ACTIVE",
        "MONTH_BOUNDARY_GUARD",
        "INDETERMINATE_ACTIVE",
    }:
        return OrtexOutcomeKind.BACKOFF_ACTIVE, kind.lower()
    return OrtexOutcomeKind.QUOTA_AUTHORITY_UNAVAILABLE, kind.lower()


def _read_recent_endpoint(
    authority: OrtexQuotaAuthorityProtocol,
    *,
    plan: _RequestPlan,
    policy: Mapping[str, object],
) -> tuple[OrtexEndpointOutcome | None, tuple[OrtexOutcomeKind, str] | None]:
    reader = getattr(authority, "read_recent_completed", None)
    if not callable(reader):
        return None, None
    try:
        decision = reader(
            plan.request_sha256,
            max_age_seconds=int(policy["success_cache_ttl_seconds"]),
        )
    except Exception:
        return None, (
            OrtexOutcomeKind.QUOTA_AUTHORITY_UNAVAILABLE,
            "durable_cache_authority_unavailable",
        )
    kind = _enum_name(getattr(decision, "kind", "DB_UNAVAILABLE"))
    if kind == "NOT_FOUND":
        return None, None
    if kind != "COMPLETED" or getattr(decision, "attempt", None) is None:
        mapped, detail = _quota_block(kind)
        return None, (mapped, f"durable_cache_{detail}")
    try:
        endpoint = _endpoint_from_completed_record(
            getattr(decision, "attempt"),
            policy=policy,
        )
    except Exception:
        return None, (
            OrtexOutcomeKind.CONTRACT_MISMATCH,
            "durable_cache_contract_mismatch",
        )
    if endpoint.request_sha256 != plan.request_sha256:
        return None, (
            OrtexOutcomeKind.CONTRACT_MISMATCH,
            "durable_cache_request_mismatch",
        )
    return endpoint, None


def _fetch_plans(
    authority: OrtexQuotaAuthorityProtocol,
    *,
    plans: Sequence[_RequestPlan],
    api_key: str,
    policy: Mapping[str, object],
) -> _FetchPlansResult:
    endpoints: list[OrtexEndpointOutcome] = []
    missing: list[_RequestPlan] = []
    for plan in plans:
        endpoint, block = _read_recent_endpoint(
            authority, plan=plan, policy=policy
        )
        if block is not None:
            return _FetchPlansResult(
                endpoints=tuple(endpoints),
                blocked_kind=block[0],
                blocked_detail=block[1],
            )
        if endpoint is None:
            missing.append(plan)
        else:
            endpoints.append(endpoint)
    if not missing:
        ordered = tuple(
            next(
                endpoint
                for endpoint in endpoints
                if endpoint.request_sha256 == plan.request_sha256
            )
            for plan in plans
        )
        return _FetchPlansResult(
            endpoints=ordered,
            all_from_durable_cache=True,
        )
    owner_token = (
        "provider-auth-"
        f"{_credential_auth_scope(api_key, _sha256_json(policy))}-"
        f"{uuid4().hex}"
    )
    bundle_sha256 = _sha256_json(
        {
            "provider": "ortex",
            "quota_policy_sha256": _sha256_json(policy),
            "owner_token_sha256": _sha256_bytes(
                owner_token.encode("utf-8")
            ),
            "request_sha256s": [plan.request_sha256 for plan in missing],
        }
    )
    try:
        decision = authority.reserve_bundle(
            bundle_sha256=bundle_sha256,
            owner_token=owner_token,
            requests=tuple(_quota_request_spec(plan) for plan in missing),
            lease_seconds=float(policy["quota_lease_seconds"]),
        )
    except Exception:
        return _FetchPlansResult(
            endpoints=tuple(endpoints),
            blocked_kind=OrtexOutcomeKind.QUOTA_AUTHORITY_UNAVAILABLE,
            blocked_detail="quota_reservation_unavailable",
        )
    decision_kind = _enum_name(getattr(decision, "kind", "DB_UNAVAILABLE"))
    if decision_kind != "GRANTED":
        mapped, detail = _quota_block(decision_kind)
        return _FetchPlansResult(
            endpoints=tuple(endpoints),
            blocked_kind=mapped,
            blocked_detail=detail,
        )
    permits = tuple(getattr(decision, "permits", ()))
    if len(permits) != len(missing):
        return _FetchPlansResult(
            endpoints=tuple(endpoints),
            blocked_kind=OrtexOutcomeKind.CONTRACT_MISMATCH,
            blocked_detail="quota_permit_count_mismatch",
        )
    permit_by_request = {
        str(getattr(permit, "request_sha256")): permit for permit in permits
    }
    if set(permit_by_request) != {
        plan.request_sha256 for plan in missing
    }:
        return _FetchPlansResult(
            endpoints=tuple(endpoints),
            blocked_kind=OrtexOutcomeKind.CONTRACT_MISMATCH,
            blocked_detail="quota_permit_request_mismatch",
        )
    for index, plan in enumerate(missing):
        if index:
            _sleep(float(policy["request_interval_seconds"]))
        permit = permit_by_request[plan.request_sha256]
        try:
            begin = authority.begin_transport(permit)
        except Exception:
            begin = SimpleNamespace(
                kind="DB_UNAVAILABLE", may_call_transport=False
            )
        begin_kind = _enum_name(getattr(begin, "kind", "DB_UNAVAILABLE"))
        if begin_kind != "GRANTED" or not bool(
            getattr(begin, "may_call_transport", False)
        ):
            try:
                authority.refund_unstarted(permit)
            except Exception:
                pass
            mapped, detail = _quota_block(begin_kind)
            return _FetchPlansResult(
                endpoints=tuple(endpoints),
                blocked_kind=mapped,
                blocked_detail=f"transport_{detail}",
            )
        result = _perform_http(plan, api_key=api_key, policy=policy)
        completed_endpoint = _complete_endpoint(
            authority=authority,
            permit=permit,
            plan=plan,
            result=result,
            policy=policy,
        )
        endpoints.append(completed_endpoint)
        if completed_endpoint.kind is OrtexOutcomeKind.AUTH_REJECTED:
            _install_credential_auth_latch(
                api_key,
                _sha256_json(policy),
                ttl_seconds=float(
                    policy["auth_rejection_backoff_seconds"]
                ),
            )
            for unstarted_plan in missing[index + 1 :]:
                try:
                    authority.refund_unstarted(
                        permit_by_request[unstarted_plan.request_sha256]
                    )
                except Exception:
                    pass
            return _FetchPlansResult(
                endpoints=tuple(endpoints),
            )
    ordered = tuple(
        next(
            endpoint
            for endpoint in endpoints
            if endpoint.request_sha256 == plan.request_sha256
        )
        for plan in plans
    )
    return _FetchPlansResult(endpoints=ordered)


def _durable_cache_provenance(
    outcome: OrtexShortMechanicsOutcome,
    *,
    observed_at: datetime,
) -> OrtexShortMechanicsOutcome:
    return replace(
        outcome,
        received_at=observed_at,
        available_at=observed_at,
        detail_code="durable_cache",
        cache_origin_sha256=_sha256_json(
            {
                "endpoint_capture_sha256s": [
                    endpoint.capture_sha256 for endpoint in outcome.endpoints
                ]
            }
        ),
        cache_origin_received_at=max(
            endpoint.received_at for endpoint in outcome.endpoints
        ),
        cache_origin_available_at=max(
            endpoint.available_at for endpoint in outcome.endpoints
        ),
    )


def _validate_bound_outcome(
    outcome: object,
    *,
    symbol: str,
    exchange_hint: str | None,
    policy_sha256: str,
) -> OrtexShortMechanicsOutcome:
    if not isinstance(outcome, OrtexShortMechanicsOutcome):
        raise ValueError("bound Ortex provider returned an untyped outcome")
    strict = OrtexShortMechanicsOutcome.from_capture_dict(
        outcome.to_capture_dict()
    )
    if strict.symbol != symbol:
        raise ValueError("bound Ortex provider symbol mismatch")
    if strict.requested_exchange != exchange_hint:
        raise ValueError("bound Ortex provider exchange mismatch")
    if strict.quota_policy_sha256 != policy_sha256:
        raise ValueError("bound Ortex provider policy mismatch")
    return strict


def _quota_authority_matches_policy(
    authority: object, policy: Mapping[str, object]
) -> bool:
    bindings = {
        "monthly_limit": "monthly_limit",
        "request_interval_seconds": "request_interval_seconds",
        "reservation_lease_seconds": "quota_lease_seconds",
        "transient_backoff_base_seconds": "backoff_base_seconds",
        "transient_backoff_max_seconds": "backoff_max_seconds",
        "auth_rejection_backoff_seconds": (
            "auth_rejection_backoff_seconds"
        ),
        "month_boundary_guard_seconds": (
            "month_boundary_guard_seconds"
        ),
        "response_max_bytes": "max_response_bytes",
    }
    for attribute, policy_key in bindings.items():
        if not hasattr(authority, attribute):
            continue
        left = getattr(authority, attribute)
        right = policy[policy_key]
        if isinstance(right, float):
            if float(left) != right:
                return False
        elif int(left) != int(right):
            return False
    return True


def get_short_mechanics_outcome(
    symbol: str,
    exchange_hint: str | None = None,
) -> OrtexShortMechanicsOutcome:
    """Typed Ortex v2 outcome; no transient retry and no unproven fallback."""

    policy = ortex_public_policy()
    policy_sha256 = _sha256_json(policy)
    canonical_symbol = str(symbol or "").strip().upper()
    requested_exchange = (
        None
        if exchange_hint is None
        else str(exchange_hint).strip().lower()
    )
    capture_symbol = (
        canonical_symbol
        if _SYMBOL_RE.fullmatch(canonical_symbol)
        else "UNKNOWN"
    )
    if not bool(
        getattr(settings, "chili_momentum_squeeze_fuel_tilt_enabled", True)
    ):
        return _simple_outcome(
            kind=OrtexOutcomeKind.DISABLED,
            symbol=capture_symbol,
            requested_exchange=(
                requested_exchange
                if requested_exchange in _DISCOVERY_EXCHANGES
                else None
            ),
            detail_code="feature_disabled",
            policy=policy,
        )
    if not _SYMBOL_RE.fullmatch(canonical_symbol) or any(
        marker in canonical_symbol for marker in ("-", "/")
    ):
        return _simple_outcome(
            kind=OrtexOutcomeKind.UNSUPPORTED_SYMBOL_OR_EXCHANGE,
            symbol=capture_symbol,
            requested_exchange=None,
            detail_code="unsupported_symbol",
            policy=policy,
        )
    if (
        requested_exchange is not None
        and requested_exchange not in _DISCOVERY_EXCHANGES
    ):
        return _simple_outcome(
            kind=OrtexOutcomeKind.UNSUPPORTED_SYMBOL_OR_EXCHANGE,
            symbol=canonical_symbol,
            requested_exchange=None,
            detail_code="unsupported_exchange",
            policy=policy,
        )
    provider = _ORTEX_OUTCOME_PROVIDER.get()
    if provider is not None:
        try:
            provided = provider.get_short_mechanics_outcome(
                symbol=canonical_symbol,
                exchange_hint=requested_exchange,
            )
            return _validate_bound_outcome(
                provided,
                symbol=canonical_symbol,
                exchange_hint=requested_exchange,
                policy_sha256=policy_sha256,
            )
        except Exception:
            return _simple_outcome(
                kind=OrtexOutcomeKind.CONTRACT_MISMATCH,
                symbol=canonical_symbol,
                requested_exchange=requested_exchange,
                detail_code="bound_provider_contract_mismatch",
                policy=policy,
            )
    observed = _utcnow()
    cached = _cache_get_v2(
        canonical_symbol,
        requested_exchange,
        policy_sha256=policy_sha256,
        observed_at=observed,
    )
    if cached is not None:
        return cached
    api_key = str(getattr(settings, "chili_ortex_api_key", "") or "").strip()
    if not api_key:
        return _simple_outcome(
            kind=OrtexOutcomeKind.CREDENTIAL_UNAVAILABLE,
            symbol=canonical_symbol,
            requested_exchange=requested_exchange,
            detail_code="credential_unavailable",
            policy=policy,
        )
    if _credential_auth_latch_active(api_key, policy_sha256):
        return _simple_outcome(
            kind=OrtexOutcomeKind.BACKOFF_ACTIVE,
            symbol=canonical_symbol,
            requested_exchange=requested_exchange,
            detail_code="credential_auth_latch_active",
            policy=policy,
        )
    authority = _ORTEX_QUOTA_AUTHORITY.get()
    if authority is None:
        return _simple_outcome(
            kind=OrtexOutcomeKind.QUOTA_AUTHORITY_UNAVAILABLE,
            symbol=canonical_symbol,
            requested_exchange=requested_exchange,
            detail_code="quota_authority_unavailable",
            policy=policy,
        )
    if not _quota_authority_matches_policy(authority, policy):
        return _simple_outcome(
            kind=OrtexOutcomeKind.CONTRACT_MISMATCH,
            symbol=canonical_symbol,
            requested_exchange=requested_exchange,
            detail_code="quota_policy_contract_mismatch",
            policy=policy,
        )
    ttl_seconds = int(policy["success_cache_ttl_seconds"])
    if requested_exchange is not None:
        plans = tuple(
            _request_plan(
                dataset=dataset,
                symbol=canonical_symbol,
                exchange=requested_exchange,
                policy_sha256=policy_sha256,
            )
            for dataset in _ENDPOINT_DATASETS
        )
        fetched = _fetch_plans(
            authority,
            plans=plans,
            api_key=api_key,
            policy=policy,
        )
        outcome = _aggregate_from_endpoints(
            symbol=canonical_symbol,
            requested_exchange=requested_exchange,
            resolved_exchange=requested_exchange,
            endpoints=fetched.endpoints,
            policy=policy,
            forced_kind=fetched.blocked_kind,
            detail_code=(
                fetched.blocked_detail
                if fetched.blocked_kind is not None
                else None
            ),
        )
        if fetched.all_from_durable_cache:
            outcome = _durable_cache_provenance(
                outcome, observed_at=observed
            )
        _cache_set_v2(
            canonical_symbol,
            requested_exchange,
            policy_sha256=policy_sha256,
            outcome=outcome,
            ttl_seconds=ttl_seconds,
        )
        return outcome

    all_endpoints: list[OrtexEndpointOutcome] = []
    all_durable = True
    for exchange_index, exchange in enumerate(_DISCOVERY_EXCHANGES):
        # Exchange discovery is proved by short-interest first.  Reserving CTB
        # before that proof burned a second credit on every wrong exchange.
        datasets = ("short_interest",)
        plans = tuple(
            _request_plan(
                dataset=dataset,
                symbol=canonical_symbol,
                exchange=exchange,
                policy_sha256=policy_sha256,
            )
            for dataset in datasets
        )
        fetched = _fetch_plans(
            authority,
            plans=plans,
            api_key=api_key,
            policy=policy,
        )
        all_endpoints.extend(fetched.endpoints)
        all_durable = all_durable and fetched.all_from_durable_cache
        si_endpoint = next(
            (
                endpoint
                for endpoint in fetched.endpoints
                if endpoint.dataset == "short_interest"
            ),
            None,
        )
        if fetched.blocked_kind is not None and si_endpoint is None:
            return _simple_outcome(
                kind=fetched.blocked_kind,
                symbol=canonical_symbol,
                requested_exchange=None,
                detail_code=fetched.blocked_detail or "quota_blocked",
                policy=policy,
                endpoints=all_endpoints,
            )
        if (
            si_endpoint is not None
            and si_endpoint.kind
            is OrtexOutcomeKind.UNSUPPORTED_SYMBOL_OR_EXCHANGE
        ):
            ctb_endpoints = tuple(
                endpoint
                for endpoint in fetched.endpoints
                if endpoint.dataset == "cost_to_borrow"
            )
            if any(
                endpoint.kind
                is not OrtexOutcomeKind.UNSUPPORTED_SYMBOL_OR_EXCHANGE
                for endpoint in ctb_endpoints
            ):
                return _simple_outcome(
                    kind=OrtexOutcomeKind.CONTRACT_MISMATCH,
                    symbol=canonical_symbol,
                    requested_exchange=None,
                    detail_code="exchange_discovery_endpoint_conflict",
                    policy=policy,
                    endpoints=all_endpoints,
                )
            if exchange_index + 1 < len(_DISCOVERY_EXCHANGES):
                _sleep(float(policy["request_interval_seconds"]))
            continue
        if si_endpoint is None or si_endpoint.kind not in {
            OrtexOutcomeKind.SUCCESS,
            OrtexOutcomeKind.AUTHORITATIVE_EMPTY,
        }:
            return _aggregate_from_endpoints(
                symbol=canonical_symbol,
                requested_exchange=None,
                resolved_exchange=exchange,
                endpoints=all_endpoints,
                policy=policy,
                forced_kind=(
                    fetched.blocked_kind
                    if fetched.blocked_kind is not None
                    else (si_endpoint.kind if si_endpoint is not None else None)
                ),
                detail_code=(
                    fetched.blocked_detail
                    if fetched.blocked_detail is not None
                    else (
                        si_endpoint.detail_code
                        if si_endpoint is not None
                        else "exchange_discovery_unavailable"
                    )
                ),
            )
        ctb_plan = _request_plan(
            dataset="cost_to_borrow",
            symbol=canonical_symbol,
            exchange=exchange,
            policy_sha256=policy_sha256,
        )
        _sleep(float(policy["request_interval_seconds"]))
        ctb = _fetch_plans(
            authority,
            plans=(ctb_plan,),
            api_key=api_key,
            policy=policy,
        )
        all_endpoints.extend(ctb.endpoints)
        all_durable = all_durable and ctb.all_from_durable_cache
        fetched = _FetchPlansResult(
            endpoints=tuple(fetched.endpoints + ctb.endpoints),
            blocked_kind=ctb.blocked_kind,
            blocked_detail=ctb.blocked_detail,
            all_from_durable_cache=(
                fetched.all_from_durable_cache
                and ctb.all_from_durable_cache
            ),
        )
        outcome = _aggregate_from_endpoints(
            symbol=canonical_symbol,
            requested_exchange=None,
            resolved_exchange=exchange,
            endpoints=all_endpoints,
            policy=policy,
            forced_kind=fetched.blocked_kind,
            detail_code=(
                fetched.blocked_detail
                if fetched.blocked_kind is not None
                else None
            ),
        )
        if all_durable:
            outcome = _durable_cache_provenance(
                outcome, observed_at=observed
            )
        _cache_set_v2(
            canonical_symbol,
            None,
            policy_sha256=policy_sha256,
            outcome=outcome,
            ttl_seconds=ttl_seconds,
        )
        return outcome
    outcome = _simple_outcome(
        kind=OrtexOutcomeKind.UNSUPPORTED_SYMBOL_OR_EXCHANGE,
        symbol=canonical_symbol,
        requested_exchange=None,
        detail_code="symbol_not_found_on_supported_exchanges",
        policy=policy,
        endpoints=all_endpoints,
    )
    if all_durable:
        outcome = _durable_cache_provenance(
            outcome, observed_at=observed
        )
    _cache_set_v2(
        canonical_symbol,
        None,
        policy_sha256=policy_sha256,
        outcome=outcome,
        ttl_seconds=ttl_seconds,
    )
    return outcome


def get_short_mechanics(
    symbol: str,
    exchange_hint: str | None = None,
) -> dict[str, object] | None:
    """Compatibility wrapper for callers not yet consuming typed provenance."""

    try:
        outcome = get_short_mechanics_outcome(
            symbol, exchange_hint=exchange_hint
        )
    except Exception:
        return None
    if outcome.kind is not OrtexOutcomeKind.SUCCESS:
        return None
    return {
        "short_interest_pct": outcome.short_interest_pct,
        "cost_to_borrow": outcome.cost_to_borrow,
        "utilization": outcome.utilization,
        "is_easy_to_borrow": outcome.is_easy_to_borrow,
    }
