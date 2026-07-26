"""Durable Ortex monthly quota, pacing, and transient-backoff authority.

The authority owns only transport permission and sanitized provider-response
evidence.  It never stores API keys, request headers, or full URLs.  Every
decision uses PostgreSQL's clock under one fixed transaction advisory lock so
workers and process restarts share the same 1,000-credit calendar-month budget.
"""

from __future__ import annotations

import base64
import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Sequence

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

_PLAN_SCOPE = "ortex:trader-1000:v1"
_LOCK_CLASSID = 0x4F52  # OR
_LOCK_OBJID = 0x5458  # TX
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
_AUTH_OWNER_RE = re.compile(
    r"^provider-auth-([0-9a-f]{32})-[0-9a-f]{32}$"
)
_SYMBOL_RE = re.compile(r"^[A-Z0-9.-]{1,36}$")
_TEST_CLOCK_RE = re.compile(
    r"^TIMESTAMPTZ '[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})'$"
)
_TRANSIENT_OUTCOMES: frozenset["OrtexProviderOutcome"]
_ATTEMPT_COLUMNS = """
attempt_id, plan_scope, month_start, bundle_sha256, bundle_index,
owner_token, request_sha256, provider, endpoint, symbol, status,
monthly_limit, quota_used_after, reserved_at, lease_expires_at,
transport_started_at, completed_at, refunded_at, provider_outcome,
http_status, backoff_until, transient_streak, raw_response_body_b64,
response_sha256, provider_event_at, received_at, available_at,
effective_at, created_at, updated_at
"""


class OrtexProviderOutcome(str, Enum):
    SUCCESS = "success"
    AUTHORITATIVE_EMPTY = "authoritative_empty"
    NOT_FOUND = "not_found"
    AUTH_ERROR = "auth_error"
    RATE_LIMITED = "rate_limited"
    TRANSIENT_HTTP = "transient_http"
    TIMEOUT = "timeout"
    MALFORMED = "malformed"
    PERMANENT_ERROR = "permanent_error"


_TRANSIENT_OUTCOMES = frozenset(
    {
        OrtexProviderOutcome.RATE_LIMITED,
        OrtexProviderOutcome.TRANSIENT_HTTP,
        OrtexProviderOutcome.TIMEOUT,
    }
)


class OrtexQuotaDecisionKind(str, Enum):
    GRANTED = "granted"
    QUOTA_EXHAUSTED = "quota_exhausted"
    BACKOFF_ACTIVE = "backoff_active"
    PACING_ACTIVE = "pacing_active"
    MONTH_BOUNDARY_GUARD = "month_boundary_guard"
    INDETERMINATE_ACTIVE = "indeterminate_active"
    ALREADY_TRANSPORT_COMMITTED = "already_transport_committed"
    COMPLETED = "completed"
    REFUNDED = "refunded"
    EXPIRED = "expired"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    DB_UNAVAILABLE = "db_unavailable"


@dataclass(frozen=True)
class OrtexRequestSpec:
    request_sha256: str
    endpoint: str
    symbol: str
    provider: str = "ortex"

    def __post_init__(self) -> None:
        _require_hash(self.request_sha256, "request_sha256")
        if self.provider != "ortex":
            raise ValueError("provider must be ortex")
        if (
            not self.endpoint.startswith("/api/v1/")
            or "?" in self.endpoint
            or "#" in self.endpoint
            or "://" in self.endpoint
            or len(self.endpoint) > 256
        ):
            raise ValueError("endpoint must be a sanitized Ortex API path")
        if not _SYMBOL_RE.fullmatch(self.symbol):
            raise ValueError("symbol must be normalized and nonsecret")


@dataclass(frozen=True)
class OrtexQuotaPermit:
    attempt_id: uuid.UUID
    plan_scope: str
    month_start: date
    bundle_sha256: str
    bundle_index: int
    owner_token: str
    request_sha256: str
    provider: str
    endpoint: str
    symbol: str
    monthly_limit: int
    quota_used_after: int
    reserved_at: datetime
    lease_expires_at: datetime


@dataclass(frozen=True)
class OrtexAttemptRecord:
    attempt_id: uuid.UUID
    plan_scope: str
    month_start: date
    bundle_sha256: str
    bundle_index: int
    owner_token: str
    request_sha256: str
    provider: str
    endpoint: str
    symbol: str
    status: str
    monthly_limit: int
    quota_used_after: int
    reserved_at: datetime
    lease_expires_at: datetime
    transport_started_at: datetime | None
    completed_at: datetime | None
    refunded_at: datetime | None
    provider_outcome: OrtexProviderOutcome | None
    http_status: int | None
    backoff_until: datetime | None
    transient_streak: int
    raw_response_body_b64: str | None
    response_sha256: str | None
    provider_event_at: datetime | None
    effective_at: datetime | None
    received_at: datetime | None
    available_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class OrtexQuotaDecision:
    kind: OrtexQuotaDecisionKind
    permits: tuple[OrtexQuotaPermit, ...] = ()
    attempt: OrtexAttemptRecord | None = None
    quota_used: int = 0
    quota_limit: int = 1000
    month_start: date | None = None
    retry_at: datetime | None = None
    decision_at: datetime | None = None
    reason: str | None = None
    may_call_transport: bool = False


def _require_hash(value: str, name: str) -> None:
    if not _HASH_RE.fullmatch(str(value)):
        raise ValueError(f"{name} must be a lowercase SHA-256")


def _require_aware(value: datetime | None, name: str) -> None:
    if value is not None and (
        value.tzinfo is None or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be timezone-aware")


def _record(row) -> OrtexAttemptRecord:
    value = row if isinstance(row, dict) else dict(row)
    outcome = value.get("provider_outcome")
    return OrtexAttemptRecord(
        attempt_id=value["attempt_id"],
        plan_scope=str(value["plan_scope"]),
        month_start=value["month_start"],
        bundle_sha256=str(value["bundle_sha256"]),
        bundle_index=int(value["bundle_index"]),
        owner_token=str(value["owner_token"]),
        request_sha256=str(value["request_sha256"]),
        provider=str(value["provider"]),
        endpoint=str(value["endpoint"]),
        symbol=str(value["symbol"]),
        status=str(value["status"]),
        monthly_limit=int(value["monthly_limit"]),
        quota_used_after=int(value["quota_used_after"]),
        reserved_at=value["reserved_at"],
        lease_expires_at=value["lease_expires_at"],
        transport_started_at=value.get("transport_started_at"),
        completed_at=value.get("completed_at"),
        refunded_at=value.get("refunded_at"),
        provider_outcome=OrtexProviderOutcome(outcome) if outcome else None,
        http_status=value.get("http_status"),
        backoff_until=value.get("backoff_until"),
        transient_streak=int(value.get("transient_streak") or 0),
        raw_response_body_b64=value.get("raw_response_body_b64"),
        response_sha256=value.get("response_sha256"),
        provider_event_at=value.get("provider_event_at"),
        effective_at=value.get("effective_at"),
        received_at=value.get("received_at"),
        available_at=value.get("available_at"),
        created_at=value["created_at"],
        updated_at=value["updated_at"],
    )


def _permit(record: OrtexAttemptRecord) -> OrtexQuotaPermit:
    return OrtexQuotaPermit(
        attempt_id=record.attempt_id,
        plan_scope=record.plan_scope,
        month_start=record.month_start,
        bundle_sha256=record.bundle_sha256,
        bundle_index=record.bundle_index,
        owner_token=record.owner_token,
        request_sha256=record.request_sha256,
        provider=record.provider,
        endpoint=record.endpoint,
        symbol=record.symbol,
        monthly_limit=record.monthly_limit,
        quota_used_after=record.quota_used_after,
        reserved_at=record.reserved_at,
        lease_expires_at=record.lease_expires_at,
    )


class OrtexQuotaAuthority:
    """One fixed-plan PostgreSQL transport authority."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        monthly_limit: int = 1000,
        request_interval_seconds: float = 1.05,
        reservation_lease_seconds: float = 30.0,
        transient_backoff_base_seconds: float = 2.0,
        transient_backoff_max_seconds: float = 300.0,
        auth_rejection_backoff_seconds: float = 3600.0,
        month_boundary_guard_seconds: float = 30.0,
        response_max_bytes: int = 1_048_576,
        _test_clock_sql: str | None = None,
    ) -> None:
        if not 1 <= int(monthly_limit) <= 1000:
            raise ValueError("monthly_limit must be between 1 and 1000")
        if not 1.0 <= float(request_interval_seconds) <= 10.0:
            raise ValueError("request_interval_seconds must be between 1 and 10")
        if not 1.0 <= float(reservation_lease_seconds) <= 300.0:
            raise ValueError("reservation_lease_seconds must be between 1 and 300")
        if not 1.0 <= float(transient_backoff_base_seconds) <= 60.0:
            raise ValueError(
                "transient_backoff_base_seconds must be between 1 and 60"
            )
        if not (
            float(transient_backoff_base_seconds)
            <= float(transient_backoff_max_seconds)
            <= 3600.0
        ):
            raise ValueError(
                "transient_backoff_max_seconds must be >= base and <= 3600"
            )
        if not 2679.0 <= float(auth_rejection_backoff_seconds) <= 86400.0:
            raise ValueError(
                "auth_rejection_backoff_seconds must prevent 1,000 "
                "authentication attempts in a 31-day month"
            )
        if not 0.1 <= float(month_boundary_guard_seconds) <= 300.0:
            raise ValueError(
                "month_boundary_guard_seconds must be between 0.1 and 300"
            )
        if float(month_boundary_guard_seconds) < float(
            reservation_lease_seconds
        ):
            raise ValueError(
                "month_boundary_guard_seconds must cover the reservation lease"
            )
        if not 1024 <= int(response_max_bytes) <= 10_485_760:
            raise ValueError("response_max_bytes must be between 1024 and 10485760")
        if _test_clock_sql is not None and not _TEST_CLOCK_RE.fullmatch(
            _test_clock_sql
        ):
            raise ValueError("_test_clock_sql accepts only a fixed TIMESTAMPTZ literal")
        self._session_factory = session_factory
        self.monthly_limit = int(monthly_limit)
        self.request_interval_seconds = float(request_interval_seconds)
        self.reservation_lease_seconds = float(reservation_lease_seconds)
        self.transient_backoff_base_seconds = float(
            transient_backoff_base_seconds
        )
        self.transient_backoff_max_seconds = float(
            transient_backoff_max_seconds
        )
        self.auth_rejection_backoff_seconds = float(
            auth_rejection_backoff_seconds
        )
        self.month_boundary_guard_seconds = float(
            month_boundary_guard_seconds
        )
        self.response_max_bytes = int(response_max_bytes)
        self._clock_sql = _test_clock_sql or "clock_timestamp()"

    def _db_failure(self, exc: BaseException) -> OrtexQuotaDecision:
        return OrtexQuotaDecision(
            kind=OrtexQuotaDecisionKind.DB_UNAVAILABLE,
            quota_limit=self.monthly_limit,
            reason=f"database_unavailable:{type(exc).__name__}",
        )

    def _run(self, operation) -> OrtexQuotaDecision:
        session = None
        try:
            session = self._session_factory()
            with session.begin():
                decision = operation(session)
            return decision
        except (SQLAlchemyError, OSError, ConnectionError, RuntimeError) as exc:
            if session is not None:
                session.rollback()
            return self._db_failure(exc)
        finally:
            if session is not None:
                session.close()

    @staticmethod
    def _lock(session: Session) -> None:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:classid, :objid)"),
            {"classid": _LOCK_CLASSID, "objid": _LOCK_OBJID},
        )

    def _now(self, session: Session) -> tuple[datetime, date]:
        row = session.execute(
            text(
                f"""
                WITH decision_clock AS (
                    SELECT {self._clock_sql} AS now_utc
                )
                SELECT now_utc,
                       date_trunc(
                           'month', now_utc AT TIME ZONE 'UTC'
                       )::date AS month_start
                  FROM decision_clock
                """
            )
        ).mappings().one()
        return row["now_utc"], row["month_start"]

    def _bounded_transient_backoff(
        self,
        *,
        completed_at: datetime,
        transient_streak: int,
        requested_until: datetime | None,
    ) -> datetime:
        minimum_until = completed_at + timedelta(
            seconds=min(
                self.transient_backoff_max_seconds,
                self.transient_backoff_base_seconds
                * (2 ** min(max(0, transient_streak - 1), 20)),
            )
        )
        maximum_until = completed_at + timedelta(
            seconds=self.transient_backoff_max_seconds
        )
        return min(
            max(minimum_until, requested_until or minimum_until),
            maximum_until,
        )

    def _month_boundary_retry_at(
        self,
        *,
        now: datetime,
        month_start: date,
    ) -> datetime | None:
        if month_start.month == 12:
            next_month = datetime(
                month_start.year + 1,
                1,
                1,
                tzinfo=timezone.utc,
            )
        else:
            next_month = datetime(
                month_start.year,
                month_start.month + 1,
                1,
                tzinfo=timezone.utc,
            )
        guard_starts_at = next_month - timedelta(
            seconds=self.month_boundary_guard_seconds
        )
        return next_month if now >= guard_starts_at else None

    @staticmethod
    def _quota_used(session: Session, month_start: date) -> int:
        return int(
            session.execute(
                text(
                    """
                    SELECT count(*)
                      FROM momentum_ortex_request_attempts
                     WHERE plan_scope = :scope
                       AND month_start = :month_start
                       AND status IN
                           ('reserved', 'transport_committed', 'completed')
                    """
                ),
                {"scope": _PLAN_SCOPE, "month_start": month_start},
            ).scalar_one()
        )

    @staticmethod
    def _refund_stale_reserved(session: Session, now: datetime) -> None:
        session.execute(
            text(
                """
                UPDATE momentum_ortex_request_attempts
                   SET status = 'refunded',
                       refunded_at = :now,
                       updated_at = :now
                 WHERE plan_scope = :scope
                   AND status = 'reserved'
                   AND transport_started_at IS NULL
                   AND lease_expires_at <= :now
                """
            ),
            {"scope": _PLAN_SCOPE, "now": now},
        )

    @staticmethod
    def _auth_scope(owner_token: str) -> str | None:
        match = _AUTH_OWNER_RE.fullmatch(owner_token)
        return None if match is None else match.group(1)

    def _active_backoff(
        self,
        session: Session,
        now: datetime,
        *,
        owner_token: str,
    ) -> datetime | None:
        auth_scope = self._auth_scope(owner_token)
        return session.execute(
            text(
                """
                SELECT max(retry_at)
                  FROM (
                        SELECT backoff_until AS retry_at
                          FROM momentum_ortex_request_attempts
                         WHERE plan_scope = :scope
                           AND backoff_until > :now
                        UNION ALL
                        SELECT lease_expires_at AS retry_at
                          FROM momentum_ortex_request_attempts
                         WHERE plan_scope = :scope
                           AND status = 'completed'
                           AND provider_outcome = 'auth_error'
                           AND lease_expires_at > :now
                           AND :auth_scope IS NOT NULL
                           AND owner_token LIKE
                               'provider-auth-' || :auth_scope || '-%'
                       ) AS durable_backoffs
                """
            ),
            {
                "scope": _PLAN_SCOPE,
                "now": now,
                "auth_scope": auth_scope,
            },
        ).scalar_one()

    @staticmethod
    def _active_indeterminate(
        session: Session, now: datetime
    ) -> datetime | None:
        return session.execute(
            text(
                """
                SELECT max(lease_expires_at)
                  FROM momentum_ortex_request_attempts
                 WHERE plan_scope = :scope
                   AND status = 'transport_committed'
                   AND completed_at IS NULL
                   AND lease_expires_at > :now
                """
            ),
            {"scope": _PLAN_SCOPE, "now": now},
        ).scalar_one()

    @staticmethod
    def _attempt_by_id(
        session: Session, attempt_id: uuid.UUID, *, for_update: bool = False
    ) -> OrtexAttemptRecord | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = session.execute(
            text(
                f"""
                SELECT {_ATTEMPT_COLUMNS}
                  FROM momentum_ortex_request_attempts
                 WHERE attempt_id = :attempt_id{suffix}
                """
            ),
            {"attempt_id": attempt_id},
        ).mappings().one_or_none()
        return _record(row) if row else None

    @staticmethod
    def _permit_matches(
        permit: OrtexQuotaPermit, record: OrtexAttemptRecord
    ) -> bool:
        lease_matches = permit.lease_expires_at == record.lease_expires_at
        if record.status in {"transport_committed", "completed"}:
            # ``begin_transport`` extends the mutable post-marker safety hold.
            # The reservation permit remains the immutable pre-I/O authority;
            # only a monotonic extension under the same exact identity is
            # acceptable at completion/readback.
            lease_matches = permit.lease_expires_at <= record.lease_expires_at
        return (
            permit.attempt_id == record.attempt_id
            and permit.plan_scope == record.plan_scope == _PLAN_SCOPE
            and permit.month_start == record.month_start
            and permit.bundle_sha256 == record.bundle_sha256
            and permit.bundle_index == record.bundle_index
            and permit.owner_token == record.owner_token
            and permit.request_sha256 == record.request_sha256
            and permit.provider == record.provider == "ortex"
            and permit.endpoint == record.endpoint
            and permit.symbol == record.symbol
            and permit.monthly_limit == record.monthly_limit
            and permit.quota_used_after == record.quota_used_after
            and permit.reserved_at == record.reserved_at
            and lease_matches
        )

    def _from_record(
        self,
        record: OrtexAttemptRecord,
        *,
        now: datetime,
        month_start: date,
        quota_used: int,
    ) -> OrtexQuotaDecision:
        kind = {
            "reserved": OrtexQuotaDecisionKind.GRANTED,
            "transport_committed": (
                OrtexQuotaDecisionKind.ALREADY_TRANSPORT_COMMITTED
            ),
            "completed": OrtexQuotaDecisionKind.COMPLETED,
            "refunded": OrtexQuotaDecisionKind.REFUNDED,
        }[record.status]
        return OrtexQuotaDecision(
            kind=kind,
            permits=(_permit(record),) if record.status == "reserved" else (),
            attempt=record,
            quota_used=quota_used,
            quota_limit=self.monthly_limit,
            month_start=month_start,
            decision_at=now,
            may_call_transport=False,
        )

    def reserve_bundle(
        self,
        *,
        bundle_sha256: str,
        owner_token: str,
        requests: Sequence[OrtexRequestSpec],
        lease_seconds: float | None = None,
    ) -> OrtexQuotaDecision:
        _require_hash(bundle_sha256, "bundle_sha256")
        if not _OWNER_RE.fullmatch(owner_token):
            raise ValueError("owner_token must be normalized and nonsecret")
        specs = tuple(requests)
        if not 1 <= len(specs) <= 2:
            raise ValueError("an Ortex bundle must contain one or two requests")
        if len({item.request_sha256 for item in specs}) != len(specs):
            raise ValueError("bundle request identities must be distinct")
        lease = (
            self.reservation_lease_seconds
            if lease_seconds is None
            else float(lease_seconds)
        )
        if not 1.0 <= lease <= 300.0:
            raise ValueError("lease_seconds must be between 1 and 300")
        if lease > self.month_boundary_guard_seconds:
            raise ValueError(
                "lease_seconds exceeds the sealed month boundary guard"
            )

        def operation(session: Session) -> OrtexQuotaDecision:
            self._lock(session)
            now, month_start = self._now(session)
            self._refund_stale_reserved(session, now)
            existing_rows = session.execute(
                text(
                    f"""
                    SELECT {_ATTEMPT_COLUMNS}
                      FROM momentum_ortex_request_attempts
                     WHERE plan_scope = :scope
                       AND bundle_sha256 = :bundle_sha256
                     ORDER BY bundle_index
                     FOR UPDATE
                    """
                ),
                {"scope": _PLAN_SCOPE, "bundle_sha256": bundle_sha256},
            ).mappings().all()
            used = self._quota_used(session, month_start)
            if existing_rows:
                records = tuple(_record(row) for row in existing_rows)
                expected = tuple(
                    (
                        index,
                        spec.request_sha256,
                        spec.endpoint,
                        spec.symbol,
                        spec.provider,
                    )
                    for index, spec in enumerate(specs)
                )
                actual = tuple(
                    (
                        row.bundle_index,
                        row.request_sha256,
                        row.endpoint,
                        row.symbol,
                        row.provider,
                    )
                    for row in records
                )
                if (
                    actual != expected
                    or any(row.owner_token != owner_token for row in records)
                    or any(
                        row.monthly_limit != self.monthly_limit for row in records
                    )
                ):
                    return OrtexQuotaDecision(
                        kind=OrtexQuotaDecisionKind.CONFLICT,
                        quota_used=used,
                        quota_limit=self.monthly_limit,
                        month_start=month_start,
                        decision_at=now,
                        reason="bundle_identity_conflict",
                    )
                if all(row.status == "reserved" for row in records):
                    return OrtexQuotaDecision(
                        kind=OrtexQuotaDecisionKind.GRANTED,
                        permits=tuple(_permit(row) for row in records),
                        attempt=records[0] if len(records) == 1 else None,
                        quota_used=used,
                        quota_limit=self.monthly_limit,
                        month_start=month_start,
                        decision_at=now,
                    )
                priority = (
                    ("transport_committed", OrtexQuotaDecisionKind.ALREADY_TRANSPORT_COMMITTED),
                    ("completed", OrtexQuotaDecisionKind.COMPLETED),
                    ("refunded", OrtexQuotaDecisionKind.REFUNDED),
                )
                kind = next(
                    result
                    for status, result in priority
                    if any(row.status == status for row in records)
                )
                return OrtexQuotaDecision(
                    kind=kind,
                    attempt=records[0] if len(records) == 1 else None,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    decision_at=now,
                    reason="existing_bundle_not_fully_reserved",
                )
            boundary_retry_at = self._month_boundary_retry_at(
                now=now,
                month_start=month_start,
            )
            if boundary_retry_at is not None:
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.MONTH_BOUNDARY_GUARD,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    retry_at=boundary_retry_at,
                    decision_at=now,
                    reason="utc_month_transport_boundary_guard",
                )
            backoff = self._active_backoff(
                session, now, owner_token=owner_token
            )
            if backoff is not None:
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.BACKOFF_ACTIVE,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    retry_at=backoff,
                    decision_at=now,
                    reason="durable_provider_backoff",
                )
            indeterminate_until = self._active_indeterminate(session, now)
            if indeterminate_until is not None:
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.INDETERMINATE_ACTIVE,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    retry_at=indeterminate_until,
                    decision_at=now,
                    reason="transport_completion_indeterminate",
                )
            if used + len(specs) > self.monthly_limit:
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.QUOTA_EXHAUSTED,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    decision_at=now,
                    reason="calendar_month_quota_exhausted",
                )
            expires = now + timedelta(seconds=lease)
            records: list[OrtexAttemptRecord] = []
            for index, spec in enumerate(specs):
                row = session.execute(
                    text(
                        f"""
                        INSERT INTO momentum_ortex_request_attempts (
                            attempt_id, plan_scope, month_start, bundle_sha256,
                            bundle_index, owner_token, request_sha256, provider,
                            endpoint, symbol, status, monthly_limit,
                            quota_used_after, reserved_at, lease_expires_at,
                            created_at, updated_at
                        ) VALUES (
                            :attempt_id, :scope, :month_start, :bundle_sha256,
                            :bundle_index, :owner_token, :request_sha256, 'ortex',
                            :endpoint, :symbol, 'reserved', :monthly_limit,
                            :quota_used_after, :now, :expires, :now, :now
                        )
                        RETURNING {_ATTEMPT_COLUMNS}
                        """
                    ),
                    {
                        "attempt_id": uuid.uuid4(),
                        "scope": _PLAN_SCOPE,
                        "month_start": month_start,
                        "bundle_sha256": bundle_sha256,
                        "bundle_index": index,
                        "owner_token": owner_token,
                        "request_sha256": spec.request_sha256,
                        "endpoint": spec.endpoint,
                        "symbol": spec.symbol,
                        "monthly_limit": self.monthly_limit,
                        "quota_used_after": used + index + 1,
                        "now": now,
                        "expires": expires,
                    },
                ).mappings().one()
                records.append(_record(row))
            return OrtexQuotaDecision(
                kind=OrtexQuotaDecisionKind.GRANTED,
                permits=tuple(_permit(row) for row in records),
                attempt=records[0] if len(records) == 1 else None,
                quota_used=used + len(records),
                quota_limit=self.monthly_limit,
                month_start=month_start,
                decision_at=now,
            )

        return self._run(operation)

    def begin_transport(
        self, permit: OrtexQuotaPermit
    ) -> OrtexQuotaDecision:
        def operation(session: Session) -> OrtexQuotaDecision:
            self._lock(session)
            now, month_start = self._now(session)
            record = self._attempt_by_id(
                session, permit.attempt_id, for_update=True
            )
            used = self._quota_used(session, month_start)
            if record is None:
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.NOT_FOUND,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    decision_at=now,
                )
            if not self._permit_matches(permit, record):
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.CONFLICT,
                    attempt=record,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    decision_at=now,
                    reason="permit_identity_conflict",
                )
            if record.status != "reserved":
                return self._from_record(
                    record,
                    now=now,
                    month_start=month_start,
                    quota_used=used,
                )
            if record.monthly_limit != self.monthly_limit:
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.CONFLICT,
                    attempt=record,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    decision_at=now,
                    reason="monthly_limit_changed",
                )
            if record.month_start != month_start:
                session.execute(
                    text(
                        """
                        UPDATE momentum_ortex_request_attempts
                           SET status = 'refunded',
                               refunded_at = :now,
                               updated_at = :now
                         WHERE attempt_id = :attempt_id
                           AND status = 'reserved'
                           AND transport_started_at IS NULL
                        """
                    ),
                    {"attempt_id": record.attempt_id, "now": now},
                )
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.EXPIRED,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    decision_at=now,
                    reason="calendar_month_rolled_before_transport",
                )
            if record.lease_expires_at <= now:
                session.execute(
                    text(
                        """
                        UPDATE momentum_ortex_request_attempts
                           SET status = 'refunded',
                               refunded_at = :now,
                               updated_at = :now
                         WHERE attempt_id = :attempt_id
                           AND status = 'reserved'
                           AND transport_started_at IS NULL
                        """
                    ),
                    {"attempt_id": record.attempt_id, "now": now},
                )
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.EXPIRED,
                    quota_used=max(0, used - 1),
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    decision_at=now,
                    reason="reservation_lease_expired",
                )
            boundary_retry_at = self._month_boundary_retry_at(
                now=now,
                month_start=month_start,
            )
            if boundary_retry_at is not None:
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.MONTH_BOUNDARY_GUARD,
                    attempt=record,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    retry_at=boundary_retry_at,
                    decision_at=now,
                    reason="utc_month_transport_boundary_guard",
                    may_call_transport=False,
                )
            backoff = self._active_backoff(
                session, now, owner_token=record.owner_token
            )
            if backoff is not None:
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.BACKOFF_ACTIVE,
                    attempt=record,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    retry_at=backoff,
                    decision_at=now,
                )
            last_started = session.execute(
                text(
                    """
                    SELECT max(transport_started_at)
                      FROM momentum_ortex_request_attempts
                     WHERE plan_scope = :scope
                    """
                ),
                {"scope": _PLAN_SCOPE},
            ).scalar_one()
            if last_started is not None:
                retry_at = last_started + timedelta(
                    seconds=self.request_interval_seconds
                )
                if retry_at > now:
                    return OrtexQuotaDecision(
                        kind=OrtexQuotaDecisionKind.PACING_ACTIVE,
                        attempt=record,
                        quota_used=used,
                        quota_limit=self.monthly_limit,
                        month_start=month_start,
                        retry_at=retry_at,
                        decision_at=now,
                    )
            row = session.execute(
                text(
                    f"""
                    UPDATE momentum_ortex_request_attempts
                       SET status = 'transport_committed',
                           transport_started_at = :now,
                           lease_expires_at = GREATEST(
                               lease_expires_at, :hold_until
                           ),
                           updated_at = :now
                     WHERE attempt_id = :attempt_id
                       AND status = 'reserved'
                       AND transport_started_at IS NULL
                       AND owner_token = :owner_token
                    RETURNING {_ATTEMPT_COLUMNS}
                    """
                ),
                {
                    "attempt_id": record.attempt_id,
                    "owner_token": record.owner_token,
                    "now": now,
                    "hold_until": now
                    + timedelta(seconds=self.reservation_lease_seconds),
                },
            ).mappings().one()
            committed = _record(row)
            return OrtexQuotaDecision(
                kind=OrtexQuotaDecisionKind.GRANTED,
                attempt=committed,
                quota_used=used,
                quota_limit=self.monthly_limit,
                month_start=month_start,
                decision_at=now,
                may_call_transport=True,
            )

        return self._run(operation)

    def complete_attempt(
        self,
        permit: OrtexQuotaPermit,
        *,
        outcome: OrtexProviderOutcome,
        http_status: int | None = None,
        backoff_until: datetime | None = None,
        raw_response_body_b64: str | None = None,
        response_sha256: str | None = None,
        provider_event_at: datetime | None = None,
        effective_at: datetime | None = None,
        received_at: datetime | None = None,
        available_at: datetime | None = None,
    ) -> OrtexQuotaDecision:
        outcome = OrtexProviderOutcome(outcome)
        if http_status is not None and not 100 <= int(http_status) <= 599:
            raise ValueError("http_status must be between 100 and 599")
        for value, name in (
            (backoff_until, "backoff_until"),
            (provider_event_at, "provider_event_at"),
            (effective_at, "effective_at"),
            (received_at, "received_at"),
            (available_at, "available_at"),
        ):
            _require_aware(value, name)
        if (raw_response_body_b64 is None) != (response_sha256 is None):
            raise ValueError("response body and SHA-256 must be supplied together")
        if raw_response_body_b64 is not None:
            _require_hash(str(response_sha256), "response_sha256")
            try:
                body = base64.b64decode(raw_response_body_b64, validate=True)
            except Exception as exc:
                raise ValueError("raw_response_body_b64 is invalid") from exc
            if len(body) > self.response_max_bytes:
                raise ValueError("sanitized response exceeds response_max_bytes")
            if hashlib.sha256(body).hexdigest() != response_sha256:
                raise ValueError("response SHA-256 mismatch")
        if outcome in {
            OrtexProviderOutcome.SUCCESS,
            OrtexProviderOutcome.AUTHORITATIVE_EMPTY,
        }:
            if raw_response_body_b64 is None:
                raise ValueError("cacheable outcomes require a hash-bound body")
            if received_at is None or available_at is None:
                raise ValueError(
                    "cacheable outcomes require received and available clocks"
                )
            if (
                outcome is OrtexProviderOutcome.SUCCESS
                and provider_event_at is None
                and effective_at is None
            ):
                raise ValueError(
                    "successful outcomes require provider_event_at or effective_at"
                )
            if received_at > available_at:
                raise ValueError("received_at must not exceed available_at")
        if outcome not in _TRANSIENT_OUTCOMES and backoff_until is not None:
            raise ValueError("only transient outcomes may install backoff")

        def operation(session: Session) -> OrtexQuotaDecision:
            self._lock(session)
            now, month_start = self._now(session)
            record = self._attempt_by_id(
                session, permit.attempt_id, for_update=True
            )
            used = self._quota_used(session, month_start)
            if record is None:
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.NOT_FOUND,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    decision_at=now,
                )
            if not self._permit_matches(permit, record):
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.CONFLICT,
                    attempt=record,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    decision_at=now,
                    reason="permit_identity_conflict",
                )
            if record.status == "completed":
                if outcome in _TRANSIENT_OUTCOMES:
                    backoff_matches = (
                        record.backoff_until is not None
                        and record.completed_at is not None
                        and record.backoff_until
                        == self._bounded_transient_backoff(
                            completed_at=record.completed_at,
                            transient_streak=record.transient_streak,
                            requested_until=backoff_until,
                        )
                    )
                elif outcome is OrtexProviderOutcome.AUTH_ERROR:
                    backoff_matches = (
                        record.backoff_until is None
                        and record.completed_at is not None
                        and record.lease_expires_at
                        == record.completed_at
                        + timedelta(
                            seconds=self.auth_rejection_backoff_seconds
                        )
                    )
                else:
                    backoff_matches = record.backoff_until is None
                exact = (
                    record.provider_outcome == outcome
                    and record.http_status == http_status
                    and backoff_matches
                    and record.raw_response_body_b64 == raw_response_body_b64
                    and record.response_sha256 == response_sha256
                    and record.provider_event_at == provider_event_at
                    and record.effective_at == effective_at
                    and record.received_at == received_at
                    and record.available_at == available_at
                )
                return OrtexQuotaDecision(
                    kind=(
                        OrtexQuotaDecisionKind.COMPLETED
                        if exact
                        else OrtexQuotaDecisionKind.CONFLICT
                    ),
                    attempt=record,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    decision_at=now,
                    reason=None if exact else "completion_identity_conflict",
                )
            if record.status != "transport_committed":
                return self._from_record(
                    record,
                    now=now,
                    month_start=month_start,
                    quota_used=used,
                )
            previous = session.execute(
                text(
                    """
                    SELECT provider_outcome, transient_streak
                      FROM momentum_ortex_request_attempts
                     WHERE plan_scope = :scope
                       AND status = 'completed'
                       AND attempt_id <> :attempt_id
                     ORDER BY completed_at DESC, attempt_id DESC
                     LIMIT 1
                    """
                ),
                {"scope": _PLAN_SCOPE, "attempt_id": record.attempt_id},
            ).mappings().one_or_none()
            if outcome in _TRANSIENT_OUTCOMES:
                prior_streak = (
                    int(previous["transient_streak"])
                    if previous
                    and previous["provider_outcome"]
                    in {item.value for item in _TRANSIENT_OUTCOMES}
                    else 0
                )
                streak = prior_streak + 1
                effective_backoff = self._bounded_transient_backoff(
                    completed_at=now,
                    transient_streak=streak,
                    requested_until=backoff_until,
                )
            else:
                streak = 0
                effective_backoff = None
            auth_latch_until = (
                now
                + timedelta(
                    seconds=self.auth_rejection_backoff_seconds
                )
                if outcome is OrtexProviderOutcome.AUTH_ERROR
                else None
            )
            row = session.execute(
                text(
                    f"""
                    UPDATE momentum_ortex_request_attempts
                       SET status = 'completed',
                           completed_at = :now,
                           lease_expires_at = CASE
                               WHEN :auth_latch_until IS NOT NULL
                               THEN :auth_latch_until
                               ELSE lease_expires_at
                           END,
                           provider_outcome = :provider_outcome,
                           http_status = :http_status,
                           backoff_until = :backoff_until,
                           transient_streak = :transient_streak,
                           raw_response_body_b64 = :raw_response_body_b64,
                           response_sha256 = :response_sha256,
                           provider_event_at = :provider_event_at,
                           effective_at = :effective_at,
                           received_at = :received_at,
                           available_at = :available_at,
                           updated_at = :now
                     WHERE attempt_id = :attempt_id
                       AND status = 'transport_committed'
                       AND owner_token = :owner_token
                    RETURNING {_ATTEMPT_COLUMNS}
                    """
                ),
                {
                    "attempt_id": record.attempt_id,
                    "owner_token": record.owner_token,
                    "now": now,
                    "provider_outcome": outcome.value,
                    "http_status": http_status,
                    "backoff_until": effective_backoff,
                    "auth_latch_until": auth_latch_until,
                    "transient_streak": streak,
                    "raw_response_body_b64": raw_response_body_b64,
                    "response_sha256": response_sha256,
                    "provider_event_at": provider_event_at,
                    "effective_at": effective_at,
                    "received_at": received_at,
                    "available_at": available_at,
                },
            ).mappings().one()
            completed = _record(row)
            return OrtexQuotaDecision(
                kind=OrtexQuotaDecisionKind.COMPLETED,
                attempt=completed,
                quota_used=used,
                quota_limit=self.monthly_limit,
                month_start=month_start,
                retry_at=(
                    completed.lease_expires_at
                    if outcome is OrtexProviderOutcome.AUTH_ERROR
                    else completed.backoff_until
                ),
                decision_at=now,
            )

        return self._run(operation)

    def refund_unstarted(
        self, permit: OrtexQuotaPermit
    ) -> OrtexQuotaDecision:
        def operation(session: Session) -> OrtexQuotaDecision:
            self._lock(session)
            now, month_start = self._now(session)
            record = self._attempt_by_id(
                session, permit.attempt_id, for_update=True
            )
            used = self._quota_used(session, month_start)
            if record is None:
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.NOT_FOUND,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    decision_at=now,
                )
            if not self._permit_matches(permit, record):
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.CONFLICT,
                    attempt=record,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    decision_at=now,
                    reason="permit_identity_conflict",
                )
            if record.status != "reserved":
                return self._from_record(
                    record,
                    now=now,
                    month_start=month_start,
                    quota_used=used,
                )
            row = session.execute(
                text(
                    f"""
                    UPDATE momentum_ortex_request_attempts
                       SET status = 'refunded',
                           refunded_at = :now,
                           updated_at = :now
                     WHERE attempt_id = :attempt_id
                       AND status = 'reserved'
                       AND transport_started_at IS NULL
                       AND owner_token = :owner_token
                    RETURNING {_ATTEMPT_COLUMNS}
                    """
                ),
                {
                    "attempt_id": record.attempt_id,
                    "owner_token": record.owner_token,
                    "now": now,
                },
            ).mappings().one()
            refunded = _record(row)
            return OrtexQuotaDecision(
                kind=OrtexQuotaDecisionKind.REFUNDED,
                attempt=refunded,
                quota_used=max(0, used - 1),
                quota_limit=self.monthly_limit,
                month_start=month_start,
                decision_at=now,
            )

        return self._run(operation)

    def readback(self, attempt_id: uuid.UUID) -> OrtexQuotaDecision:
        def operation(session: Session) -> OrtexQuotaDecision:
            now, month_start = self._now(session)
            record = self._attempt_by_id(session, attempt_id)
            used = self._quota_used(session, month_start)
            if record is None:
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.NOT_FOUND,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    decision_at=now,
                )
            return self._from_record(
                record,
                now=now,
                month_start=month_start,
                quota_used=used,
            )

        return self._run(operation)

    def read_recent_completed(
        self,
        request_sha256: str,
        max_age_seconds: float,
    ) -> OrtexQuotaDecision:
        _require_hash(request_sha256, "request_sha256")
        if not 0 < float(max_age_seconds) <= 604800:
            raise ValueError("max_age_seconds must be positive and <= 604800")

        def operation(session: Session) -> OrtexQuotaDecision:
            now, month_start = self._now(session)
            row = session.execute(
                text(
                    f"""
                    SELECT {_ATTEMPT_COLUMNS}
                      FROM momentum_ortex_request_attempts
                     WHERE plan_scope = :scope
                       AND request_sha256 = :request_sha256
                       AND status = 'completed'
                       AND provider_outcome IN (
                           'success', 'authoritative_empty', 'not_found'
                       )
                       AND completed_at >=
                           :now - make_interval(secs => :max_age_seconds)
                       AND completed_at <= :now
                     ORDER BY completed_at DESC, attempt_id DESC
                     LIMIT 1
                    """
                ),
                {
                    "scope": _PLAN_SCOPE,
                    "request_sha256": request_sha256,
                    "now": now,
                    "max_age_seconds": float(max_age_seconds),
                },
            ).mappings().one_or_none()
            used = self._quota_used(session, month_start)
            if row is None:
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.NOT_FOUND,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    decision_at=now,
                    reason="no_recent_authoritative_response",
                )
            record = _record(row)
            if record.provider_outcome is OrtexProviderOutcome.NOT_FOUND:
                cache_identity_valid = (
                    record.http_status == 404
                    and record.raw_response_body_b64 is None
                    and record.response_sha256 is None
                    and record.provider_event_at is None
                    and record.effective_at is None
                    and record.received_at is not None
                    and record.available_at is not None
                    and record.received_at <= record.available_at
                )
            else:
                try:
                    body = base64.b64decode(
                        record.raw_response_body_b64 or "", validate=True
                    )
                except Exception:
                    body = b""
                cache_identity_valid = (
                    record.response_sha256 is not None
                    and len(body) <= self.response_max_bytes
                    and hashlib.sha256(body).hexdigest()
                    == record.response_sha256
                )
            if not cache_identity_valid:
                return OrtexQuotaDecision(
                    kind=OrtexQuotaDecisionKind.CONFLICT,
                    attempt=record,
                    quota_used=used,
                    quota_limit=self.monthly_limit,
                    month_start=month_start,
                    decision_at=now,
                    reason="cached_response_hash_mismatch",
                )
            return OrtexQuotaDecision(
                kind=OrtexQuotaDecisionKind.COMPLETED,
                attempt=record,
                quota_used=used,
                quota_limit=self.monthly_limit,
                month_start=month_start,
                decision_at=now,
            )

        return self._run(operation)
