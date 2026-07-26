from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.services.trading.momentum_neural import short_mechanics as sm
from app.services.trading.momentum_neural.ortex_quota import (
    OrtexProviderOutcome,
    OrtexQuotaAuthority,
    OrtexQuotaDecisionKind,
    OrtexRequestSpec,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clock(value: str) -> str:
    return f"TIMESTAMPTZ '{value}'"


def _authority(db, *, limit: int = 1000, now: str | None = None):
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    return OrtexQuotaAuthority(
        factory,
        monthly_limit=limit,
        _test_clock_sql=_clock(now) if now else None,
    )


def _request(index: int) -> OrtexRequestSpec:
    return OrtexRequestSpec(
        request_sha256=_sha(f"request:{index}"),
        endpoint=f"/api/v1/stock/nasdaq/T{index}/short_interest",
        symbol=f"T{index}",
    )


def test_settings_reject_ortex_backoff_base_above_max():
    with pytest.raises(
        ValidationError,
        match=(
            "CHILI_ORTEX_TRANSIENT_BACKOFF_BASE_SECONDS must be "
            "<= CHILI_ORTEX_TRANSIENT_BACKOFF_MAX_SECONDS"
        ),
    ):
        Settings(
            _env_file=None,
            database_url="postgresql://chili:chili@localhost/chili_test",
            chili_ortex_transient_backoff_base_seconds=60,
            chili_ortex_transient_backoff_max_seconds=10,
        )


def test_atomic_concurrent_bundles_stop_exactly_at_1000(db):
    concurrent_engine = create_engine(
        os.environ["TEST_DATABASE_URL"],
        pool_size=8,
        max_overflow=0,
        pool_timeout=60,
    )
    factory = sessionmaker(bind=concurrent_engine, expire_on_commit=False)
    authority = OrtexQuotaAuthority(factory)

    def reserve(index: int):
        return authority.reserve_bundle(
            bundle_sha256=_sha(f"bundle:{index}"),
            owner_token=f"worker-{index}",
            requests=(_request(index * 2), _request(index * 2 + 1)),
        )

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            decisions = list(pool.map(reserve, range(500)))
    finally:
        concurrent_engine.dispose()

    kinds = Counter(decision.kind for decision in decisions)
    reasons = Counter(decision.reason for decision in decisions)
    assert all(
        decision.kind is OrtexQuotaDecisionKind.GRANTED
        and len(decision.permits) == 2
        for decision in decisions
    ), (kinds, reasons)
    blocked = reserve(501)
    assert blocked.kind is OrtexQuotaDecisionKind.QUOTA_EXHAUSTED
    assert blocked.quota_used == 1000
    assert blocked.permits == ()
    assert db.execute(
        text(
            "SELECT count(*) FROM momentum_ortex_request_attempts "
            "WHERE status IN ('reserved', 'transport_committed', 'completed')"
        )
    ).scalar_one() == 1000


def test_two_request_bundle_at_999_is_rejected_atomically(db):
    now = datetime(2026, 7, 8, 12, tzinfo=timezone.utc)
    db.execute(
        text(
            """
            INSERT INTO momentum_ortex_request_attempts (
                attempt_id, plan_scope, month_start, bundle_sha256,
                bundle_index, owner_token, request_sha256, provider,
                endpoint, symbol, status, monthly_limit, quota_used_after,
                reserved_at, lease_expires_at, created_at, updated_at
            ) VALUES (
                :attempt_id, 'ortex:trader-1000:v1', DATE '2026-07-01',
                :bundle_sha256, 0, :owner_token, :request_sha256, 'ortex',
                :endpoint, :symbol, 'reserved', 1000, :quota_used_after,
                :reserved_at, :lease_expires_at, :reserved_at, :reserved_at
            )
            """
        ),
        [
            {
                "attempt_id": uuid.uuid4(),
                "bundle_sha256": _sha(f"seed-bundle:{index}"),
                "owner_token": f"seed-{index}",
                "request_sha256": _sha(f"seed-request:{index}"),
                "endpoint": f"/api/v1/stock/nasdaq/S{index}/short_interest",
                "symbol": f"S{index}",
                "quota_used_after": index,
                "reserved_at": now,
                "lease_expires_at": now + timedelta(hours=1),
            }
            for index in range(1, 1000)
        ],
    )
    db.commit()
    authority = _authority(db, now="2026-07-08T12:00:01+00:00")
    before = db.execute(
        text("SELECT count(*) FROM momentum_ortex_request_attempts")
    ).scalar_one()
    decision = authority.reserve_bundle(
        bundle_sha256=_sha("atomic-edge"),
        owner_token="edge-owner",
        requests=(_request(2000), _request(2001)),
    )
    assert decision.kind is OrtexQuotaDecisionKind.QUOTA_EXHAUSTED
    assert decision.quota_used == 999
    assert decision.permits == ()
    assert db.execute(
        text("SELECT count(*) FROM momentum_ortex_request_attempts")
    ).scalar_one() == before == 999


def test_reserve_and_begin_are_lost_ack_idempotent(db):
    authority = _authority(db, now="2026-07-08T12:00:00+00:00")
    kwargs = {
        "bundle_sha256": _sha("lost-ack-bundle"),
        "owner_token": "owner-a",
        "requests": (_request(1),),
    }
    first = authority.reserve_bundle(**kwargs)
    replay = authority.reserve_bundle(**kwargs)
    assert first.kind is replay.kind is OrtexQuotaDecisionKind.GRANTED
    assert first.permits[0].attempt_id == replay.permits[0].attempt_id
    assert replay.quota_used == 1

    begun = authority.begin_transport(first.permits[0])
    duplicate = authority.begin_transport(first.permits[0])
    assert begun.kind is OrtexQuotaDecisionKind.GRANTED
    assert begun.may_call_transport is True
    assert duplicate.kind is OrtexQuotaDecisionKind.ALREADY_TRANSPORT_COMMITTED
    assert duplicate.may_call_transport is False


def test_pre_marker_crash_expires_but_post_marker_never_refunds(db):
    t0 = _authority(db, limit=1, now="2026-07-08T12:00:00+00:00")
    pre = t0.reserve_bundle(
        bundle_sha256=_sha("pre-marker"),
        owner_token="owner-pre",
        requests=(_request(10),),
        lease_seconds=1,
    )
    assert pre.kind is OrtexQuotaDecisionKind.GRANTED

    t2 = _authority(db, limit=1, now="2026-07-08T12:00:02+00:00")
    replacement = t2.reserve_bundle(
        bundle_sha256=_sha("replacement"),
        owner_token="owner-replacement",
        requests=(_request(11),),
    )
    assert replacement.kind is OrtexQuotaDecisionKind.GRANTED
    assert t2.readback(pre.permits[0].attempt_id).kind is OrtexQuotaDecisionKind.REFUNDED

    assert t2.begin_transport(replacement.permits[0]).may_call_transport is True
    refused = t2.refund_unstarted(replacement.permits[0])
    assert refused.kind is OrtexQuotaDecisionKind.ALREADY_TRANSPORT_COMMITTED
    blocked = t2.reserve_bundle(
        bundle_sha256=_sha("after-marker"),
        owner_token="owner-after",
        requests=(_request(12),),
    )
    assert blocked.kind is OrtexQuotaDecisionKind.INDETERMINATE_ACTIVE
    assert blocked.retry_at == replacement.permits[0].lease_expires_at
    assert db.execute(
        text(
            "SELECT count(*) FROM momentum_ortex_request_attempts "
            "WHERE status IN ('reserved', 'transport_committed', 'completed')"
        )
    ).scalar_one() == 1

    after_hold = _authority(db, limit=1, now="2026-07-08T12:00:33+00:00")
    assert after_hold.reserve_bundle(
        bundle_sha256=_sha("after-indeterminate-hold"),
        owner_token="owner-after",
        requests=(_request(12),),
    ).kind is OrtexQuotaDecisionKind.QUOTA_EXHAUSTED


def test_late_transport_start_receives_a_full_indeterminate_hold(db):
    reserved = _authority(
        db,
        limit=2,
        now="2026-07-08T12:00:00+00:00",
    ).reserve_bundle(
        bundle_sha256=_sha("late-transport-start"),
        owner_token="late-owner",
        requests=(_request(13),),
    )
    permit = reserved.permits[0]

    late = _authority(
        db,
        limit=2,
        now="2026-07-08T12:00:29.900000+00:00",
    )
    begun = late.begin_transport(permit)
    assert begun.kind is OrtexQuotaDecisionKind.GRANTED
    assert begun.may_call_transport is True
    assert begun.attempt is not None
    assert begun.attempt.lease_expires_at == datetime(
        2026, 7, 8, 12, 0, 59, 900000, tzinfo=timezone.utc
    )

    blocked = _authority(
        db,
        limit=2,
        now="2026-07-08T12:00:31+00:00",
    ).reserve_bundle(
        bundle_sha256=_sha("during-full-indeterminate-hold"),
        owner_token="blocked-owner",
        requests=(_request(14),),
    )
    assert blocked.kind is OrtexQuotaDecisionKind.INDETERMINATE_ACTIVE
    assert blocked.retry_at == begun.attempt.lease_expires_at


def test_db_clock_month_rollover_resets_calendar_quota(db):
    january = _authority(db, limit=1, now="2026-01-31T23:59:00+00:00")
    jan = january.reserve_bundle(
        bundle_sha256=_sha("jan"),
        owner_token="january",
        requests=(_request(20),),
    )
    assert jan.kind is OrtexQuotaDecisionKind.GRANTED
    assert january.reserve_bundle(
        bundle_sha256=_sha("jan-over"),
        owner_token="january",
        requests=(_request(21),),
    ).kind is OrtexQuotaDecisionKind.QUOTA_EXHAUSTED

    february = _authority(db, limit=1, now="2026-02-01T00:00:00+00:00")
    expired = february.begin_transport(jan.permits[0])
    assert expired.kind is OrtexQuotaDecisionKind.EXPIRED
    assert expired.reason == "calendar_month_rolled_before_transport"
    feb = february.reserve_bundle(
        bundle_sha256=_sha("feb"),
        owner_token="february",
        requests=(_request(22),),
    )
    assert feb.kind is OrtexQuotaDecisionKind.GRANTED
    assert feb.month_start.isoformat() == "2026-02-01"


def test_transport_authority_is_closed_inside_utc_month_boundary_guard(db):
    reserved = _authority(
        db,
        limit=2,
        now="2026-07-31T23:59:20+00:00",
    ).reserve_bundle(
        bundle_sha256=_sha("month-boundary-blocked"),
        owner_token="boundary-owner",
        requests=(_request(23),),
        lease_seconds=30,
    )
    permit = reserved.permits[0]

    boundary = _authority(
        db,
        limit=2,
        now="2026-07-31T23:59:30+00:00",
    )
    blocked = boundary.begin_transport(permit)
    assert blocked.kind is OrtexQuotaDecisionKind.MONTH_BOUNDARY_GUARD
    assert blocked.may_call_transport is False
    assert blocked.retry_at == datetime(
        2026,
        8,
        1,
        0,
        0,
        tzinfo=timezone.utc,
    )
    assert blocked.reason == "utc_month_transport_boundary_guard"
    assert db.execute(
        text(
            "SELECT status, transport_started_at "
            "FROM momentum_ortex_request_attempts "
            "WHERE attempt_id = :attempt_id"
        ),
        {"attempt_id": permit.attempt_id},
    ).one() == ("reserved", None)
    assert boundary.refund_unstarted(
        permit
    ).kind is OrtexQuotaDecisionKind.REFUNDED

    proactive = boundary.reserve_bundle(
        bundle_sha256=_sha("month-boundary-proactive"),
        owner_token="boundary-proactive",
        requests=(_request(24),),
    )
    assert proactive.kind is OrtexQuotaDecisionKind.MONTH_BOUNDARY_GUARD
    assert proactive.permits == ()
    assert proactive.retry_at == blocked.retry_at


def test_transport_authority_grants_just_outside_month_boundary_guard(db):
    reserved = _authority(
        db,
        limit=1,
        now="2026-07-31T23:59:20+00:00",
    ).reserve_bundle(
        bundle_sha256=_sha("month-boundary-open"),
        owner_token="boundary-open-owner",
        requests=(_request(25),),
        lease_seconds=30,
    )
    permit = reserved.permits[0]

    outside = _authority(
        db,
        limit=1,
        now="2026-07-31T23:59:29.999999+00:00",
    ).begin_transport(permit)
    assert outside.kind is OrtexQuotaDecisionKind.GRANTED
    assert outside.may_call_transport is True
    assert outside.retry_at is None
    assert outside.attempt is not None
    assert outside.attempt.transport_started_at == datetime(
        2026,
        7,
        31,
        23,
        59,
        29,
        999999,
        tzinfo=timezone.utc,
    )


def test_pacing_and_transient_backoff_are_shared_by_independent_factories(
    db, request
):
    engines = [
        create_engine(
            os.environ["TEST_DATABASE_URL"],
            pool_size=1,
            max_overflow=0,
        )
        for _ in range(3)
    ]
    factories = [
        sessionmaker(bind=engine, expire_on_commit=False) for engine in engines
    ]
    for engine in engines:
        request.addfinalizer(engine.dispose)
    first = OrtexQuotaAuthority(
        factories[0],
        _test_clock_sql=_clock("2026-07-08T12:00:00+00:00"),
    )
    p1 = first.reserve_bundle(
        bundle_sha256=_sha("pace-1"),
        owner_token="host-a",
        requests=(_request(30),),
    ).permits[0]
    assert first.begin_transport(p1).may_call_transport is True
    assert first.complete_attempt(
        p1,
        outcome=OrtexProviderOutcome.PERMANENT_ERROR,
        http_status=400,
    ).kind is OrtexQuotaDecisionKind.COMPLETED

    second = OrtexQuotaAuthority(
        factories[1],
        _test_clock_sql=_clock("2026-07-08T12:00:00+00:00"),
    )
    p2 = second.reserve_bundle(
        bundle_sha256=_sha("pace-2"),
        owner_token="host-b",
        requests=(_request(31),),
    ).permits[0]
    paced = second.begin_transport(p2)
    assert paced.kind is OrtexQuotaDecisionKind.PACING_ACTIVE
    assert paced.retry_at == datetime(
        2026, 7, 8, 12, 0, 1, 50000, tzinfo=timezone.utc
    )

    later = OrtexQuotaAuthority(
        factories[1],
        _test_clock_sql=_clock("2026-07-08T12:00:02+00:00"),
    )
    assert later.begin_transport(p2).may_call_transport is True
    backoff_until = datetime(2026, 7, 8, 12, 5, tzinfo=timezone.utc)
    completed = later.complete_attempt(
        p2,
        outcome=OrtexProviderOutcome.RATE_LIMITED,
        http_status=429,
        backoff_until=backoff_until,
    )
    assert completed.kind is OrtexQuotaDecisionKind.COMPLETED
    assert completed.attempt.transient_streak == 1
    repeated = later.complete_attempt(
        p2,
        outcome=OrtexProviderOutcome.RATE_LIMITED,
        http_status=429,
        backoff_until=backoff_until,
    )
    assert repeated.kind is OrtexQuotaDecisionKind.COMPLETED
    assert repeated.attempt.transient_streak == 1

    other_process = OrtexQuotaAuthority(
        factories[2],
        _test_clock_sql=_clock("2026-07-08T12:01:00+00:00"),
    )
    blocked = other_process.reserve_bundle(
        bundle_sha256=_sha("during-backoff"),
        owner_token="host-c",
        requests=(_request(32),),
    )
    assert blocked.kind is OrtexQuotaDecisionKind.BACKOFF_ACTIVE
    assert blocked.retry_at == backoff_until
    assert other_process.begin_transport(
        p1
    ).kind is OrtexQuotaDecisionKind.COMPLETED


def test_provider_transient_refunds_unstarted_and_restart_observes_backoff(
    db,
    monkeypatch,
):
    now = datetime(2026, 7, 26, 15, 30, tzinfo=timezone.utc)
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    authority = OrtexQuotaAuthority(
        factory,
        _test_clock_sql=_clock(now.isoformat()),
    )
    transport_calls: list[str] = []

    def transient(request, timeout):
        del timeout
        transport_calls.append(request.full_url)
        raise TimeoutError("ambiguous timeout")

    monkeypatch.setattr(
        sm.settings,
        "chili_momentum_squeeze_fuel_tilt_enabled",
        True,
    )
    monkeypatch.setattr(sm.settings, "chili_ortex_api_key", "test-only-key")
    monkeypatch.setattr(sm, "_utcnow", lambda: now)
    monkeypatch.setattr(sm, "_sleep", lambda _seconds: None)
    monkeypatch.setattr(sm, "_open_no_redirect", transient)
    sm._clear_cache_for_tests()

    with sm.ortex_quota_authority(authority):
        first = sm.get_short_mechanics_outcome(
            "SILO",
            exchange_hint="nasdaq",
        )

    assert first.kind is sm.OrtexOutcomeKind.BACKOFF_ACTIVE
    assert tuple(endpoint.kind for endpoint in first.endpoints) == (
        sm.OrtexOutcomeKind.TRANSIENT_UNAVAILABLE,
    )
    assert len(transport_calls) == 1
    status_counts = dict(
        db.execute(
            text(
                "SELECT status, count(*) FROM momentum_ortex_request_attempts "
                "GROUP BY status"
            )
        ).all()
    )
    assert status_counts == {"completed": 1, "refunded": 1}

    sm._clear_cache_for_tests()
    monkeypatch.setattr(sm, "_utcnow", lambda: now + timedelta(seconds=1))
    restarted = OrtexQuotaAuthority(
        factory,
        _test_clock_sql=_clock(
            (now + timedelta(seconds=1)).isoformat()
        ),
    )
    with sm.ortex_quota_authority(restarted):
        blocked = sm.get_short_mechanics_outcome(
            "SILO",
            exchange_hint="nasdaq",
        )

    assert blocked.kind is sm.OrtexOutcomeKind.BACKOFF_ACTIVE
    assert len(transport_calls) == 1


def test_retry_after_is_bounded_by_configured_transient_backoff_max(db):
    authority = OrtexQuotaAuthority(
        sessionmaker(bind=db.get_bind(), expire_on_commit=False),
        transient_backoff_base_seconds=2,
        transient_backoff_max_seconds=300,
        _test_clock_sql=_clock("2026-07-08T12:00:00+00:00"),
    )
    permit = authority.reserve_bundle(
        bundle_sha256=_sha("bounded-retry-after"),
        owner_token="bounded-backoff-owner",
        requests=(_request(33),),
    ).permits[0]
    assert authority.begin_transport(permit).may_call_transport is True

    provider_retry_after = datetime(
        2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc
    )
    completed = authority.complete_attempt(
        permit,
        outcome=OrtexProviderOutcome.RATE_LIMITED,
        http_status=429,
        backoff_until=provider_retry_after,
    )
    assert completed.kind is OrtexQuotaDecisionKind.COMPLETED
    assert completed.retry_at == datetime(
        2026, 7, 8, 12, 5, 0, tzinfo=timezone.utc
    )
    assert authority.complete_attempt(
        permit,
        outcome=OrtexProviderOutcome.RATE_LIMITED,
        http_status=429,
        backoff_until=provider_retry_after,
    ).kind is OrtexQuotaDecisionKind.COMPLETED


def test_auth_rejection_latches_across_restart_without_quota_burn(
    db,
    monkeypatch,
):
    now = datetime(2026, 7, 26, 15, 30, tzinfo=timezone.utc)
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    transport_calls: list[str] = []

    def rejected(request, timeout):
        del timeout
        transport_calls.append(request.full_url)
        raise sm.urllib.error.HTTPError(
            request.full_url,
            401,
            "unauthorized",
            {},
            None,
        )

    monkeypatch.setattr(
        sm.settings,
        "chili_momentum_squeeze_fuel_tilt_enabled",
        True,
    )
    monkeypatch.setattr(sm.settings, "chili_ortex_api_key", "test-only-key")
    monkeypatch.setattr(sm, "_utcnow", lambda: now)
    monkeypatch.setattr(sm, "_sleep", lambda _seconds: None)
    monkeypatch.setattr(sm, "_open_no_redirect", rejected)
    sm._clear_cache_for_tests()

    authority = OrtexQuotaAuthority(
        factory,
        _test_clock_sql=_clock(now.isoformat()),
    )
    with sm.ortex_quota_authority(authority):
        first = sm.get_short_mechanics_outcome(
            "SILO",
            exchange_hint="nasdaq",
        )

    assert first.kind is sm.OrtexOutcomeKind.AUTH_REJECTED
    assert tuple(endpoint.kind for endpoint in first.endpoints) == (
        sm.OrtexOutcomeKind.AUTH_REJECTED,
    )
    assert len(transport_calls) == 1
    assert dict(
        db.execute(
            text(
                "SELECT status, count(*) "
                "FROM momentum_ortex_request_attempts GROUP BY status"
            )
        ).all()
    ) == {"completed": 1, "refunded": 1}
    auth_row = db.execute(
        text(
            "SELECT provider_outcome, backoff_until, lease_expires_at "
            "FROM momentum_ortex_request_attempts "
            "WHERE provider_outcome = 'auth_error'"
        )
    ).one()
    assert auth_row.provider_outcome == "auth_error"
    assert auth_row.backoff_until is None
    assert auth_row.lease_expires_at == now + timedelta(hours=1)

    sm._clear_cache_for_tests()
    restarted_at = now + timedelta(seconds=1)
    monkeypatch.setattr(sm, "_utcnow", lambda: restarted_at)
    restarted = OrtexQuotaAuthority(
        factory,
        _test_clock_sql=_clock(restarted_at.isoformat()),
    )
    with sm.ortex_quota_authority(restarted):
        repeated = [
            sm.get_short_mechanics_outcome(
                f"T{index}",
                exchange_hint="nasdaq",
            )
            for index in range(100)
        ]

    assert all(
        outcome.kind is sm.OrtexOutcomeKind.BACKOFF_ACTIVE
        for outcome in repeated
    )
    assert len(transport_calls) == 1
    assert db.execute(
        text(
            "SELECT count(*) FROM momentum_ortex_request_attempts "
            "WHERE status IN "
            "('reserved', 'transport_committed', 'completed')"
        )
    ).scalar_one() == 1
    rotated_scope = sm._credential_auth_scope(
        "rotated-test-only-key",
        sm.ortex_public_policy_sha256(),
    )
    rotated = restarted.reserve_bundle(
        bundle_sha256=_sha("rotated-credential-bypasses-old-auth-latch"),
        owner_token=(
            f"provider-auth-{rotated_scope}-{uuid.uuid4().hex}"
        ),
        requests=(_request(9999),),
    )
    assert rotated.kind is OrtexQuotaDecisionKind.GRANTED
    assert restarted.refund_unstarted(
        rotated.permits[0]
    ).kind is OrtexQuotaDecisionKind.REFUNDED
    assert (31 * 24 * 60 * 60) // int(
        sm.ortex_public_policy()["auth_rejection_backoff_seconds"]
    ) < 1000


@pytest.mark.parametrize(
    ("resolved_exchange", "expected_transport_count"),
    [("nyse", 3), ("amex", 4)],
)
def test_non_nasdaq_discovery_404_receipts_survive_restart_without_new_credit(
    db,
    monkeypatch,
    resolved_exchange,
    expected_transport_count,
):
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    transport_calls: list[str] = []
    event_date = datetime.now(timezone.utc).date().isoformat()

    class Response:
        status = 200
        headers: dict[str, str] = {}

        def __init__(self, url: str, payload: object) -> None:
            self._url = url
            self._raw = json.dumps(
                payload,
                separators=(",", ":"),
            ).encode("utf-8")

        def read(self, size: int = -1) -> bytes:
            return self._raw if size < 0 else self._raw[:size]

        def geturl(self) -> str:
            return self._url

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def provider(request, timeout):
        del timeout
        url = request.full_url
        transport_calls.append(url)
        exchange = next(
            item
            for item in ("nasdaq", "nyse", "amex")
            if f"/{item}/" in url
        )
        if exchange != resolved_exchange:
            assert "/short_interest" in url
            raise sm.urllib.error.HTTPError(
                url,
                404,
                "not found",
                {},
                None,
            )
        if "/short_interest" in url:
            payload = {
                "rows": [
                    {
                        "date": event_date,
                        "shortInterestPcFreeFloat": 0.31,
                    }
                ]
            }
        elif "/ctb/all" in url:
            payload = {
                "rows": [
                    {
                        "date": event_date,
                        "costToBorrowAll": 8.0,
                    }
                ]
            }
        else:
            raise AssertionError(url)
        return Response(url, payload)

    monkeypatch.setattr(
        sm.settings,
        "chili_momentum_squeeze_fuel_tilt_enabled",
        True,
    )
    monkeypatch.setattr(sm.settings, "chili_ortex_api_key", "test-only-key")
    monkeypatch.setattr(sm, "_open_no_redirect", provider)
    sm._clear_cache_for_tests()
    authority = OrtexQuotaAuthority(factory)

    with sm.ortex_quota_authority(authority):
        first = sm.get_short_mechanics_outcome("CLRO")

    assert first.kind is sm.OrtexOutcomeKind.SUCCESS
    assert first.resolved_exchange == resolved_exchange
    assert len(transport_calls) == expected_transport_count
    assert [
        endpoint.dataset for endpoint in first.endpoints
    ] == ["short_interest"] * (expected_transport_count - 1) + [
        "cost_to_borrow"
    ]
    assert all(
        endpoint.kind is sm.OrtexOutcomeKind.UNSUPPORTED_SYMBOL_OR_EXCHANGE
        for endpoint in first.endpoints[:-2]
    )
    before = db.execute(
        text(
            "SELECT count(*) FROM momentum_ortex_request_attempts "
            "WHERE status IN "
            "('reserved', 'transport_committed', 'completed')"
        )
    ).scalar_one()
    assert len(
        {endpoint.quota.attempt_id for endpoint in first.endpoints}
    ) == expected_transport_count

    sm._clear_cache_for_tests()
    monkeypatch.setattr(
        sm,
        "_open_no_redirect",
        lambda *_args, **_kwargs: pytest.fail("restart network fallback"),
    )
    restarted = OrtexQuotaAuthority(factory)
    with sm.ortex_quota_authority(restarted):
        cached = sm.get_short_mechanics_outcome("CLRO")

    assert cached.kind is sm.OrtexOutcomeKind.SUCCESS
    assert cached.resolved_exchange == resolved_exchange
    assert cached.detail_code == "durable_cache"
    assert [endpoint.capture_sha256 for endpoint in cached.endpoints] == [
        endpoint.capture_sha256 for endpoint in first.endpoints
    ]
    assert len(transport_calls) == expected_transport_count
    assert db.execute(
        text(
            "SELECT count(*) FROM momentum_ortex_request_attempts "
            "WHERE status IN "
            "('reserved', 'transport_committed', 'completed')"
        )
    ).scalar_one() == before


def test_completed_response_is_hash_bound_and_restart_cacheable(db):
    authority = _authority(db, now="2026-07-08T12:00:00+00:00")
    request = _request(40)
    permit = authority.reserve_bundle(
        bundle_sha256=_sha("cache"),
        owner_token="cache-owner",
        requests=(request,),
    ).permits[0]
    assert authority.begin_transport(permit).may_call_transport is True
    body = b'{"data":[{"shortInterest":1234}]}'
    body_b64 = base64.b64encode(body).decode("ascii")
    body_sha = hashlib.sha256(body).hexdigest()
    available_at = datetime(2026, 7, 8, 12, 0, 1, tzinfo=timezone.utc)
    done = authority.complete_attempt(
        permit,
        outcome=OrtexProviderOutcome.SUCCESS,
        http_status=200,
        raw_response_body_b64=body_b64,
        response_sha256=body_sha,
        effective_at=datetime(2026, 7, 8, 11, 59, tzinfo=timezone.utc),
        received_at=available_at,
        available_at=available_at,
    )
    assert done.kind is OrtexQuotaDecisionKind.COMPLETED

    restarted = _authority(db, now="2026-07-08T12:10:00+00:00")
    cached = restarted.read_recent_completed(
        request_sha256=request.request_sha256,
        max_age_seconds=3600,
    )
    assert cached.kind is OrtexQuotaDecisionKind.COMPLETED
    assert cached.attempt.response_sha256 == body_sha
    assert cached.attempt.raw_response_body_b64 == body_b64
    assert cached.attempt.provider_event_at is None
    assert cached.attempt.effective_at == datetime(
        2026, 7, 8, 11, 59, tzinfo=timezone.utc
    )
    assert cached.quota_used == 1

    empty_request = _request(41)
    empty_permit = restarted.reserve_bundle(
        bundle_sha256=_sha("authoritative-empty"),
        owner_token="empty-owner",
        requests=(empty_request,),
    ).permits[0]
    assert restarted.begin_transport(empty_permit).may_call_transport is True
    empty_body = b'{"data":[]}'
    empty_done = restarted.complete_attempt(
        empty_permit,
        outcome=OrtexProviderOutcome.AUTHORITATIVE_EMPTY,
        http_status=200,
        raw_response_body_b64=base64.b64encode(empty_body).decode("ascii"),
        response_sha256=hashlib.sha256(empty_body).hexdigest(),
        received_at=datetime(2026, 7, 8, 12, 10, tzinfo=timezone.utc),
        available_at=datetime(2026, 7, 8, 12, 10, tzinfo=timezone.utc),
    )
    assert empty_done.kind is OrtexQuotaDecisionKind.COMPLETED
    assert empty_done.attempt.provider_event_at is None
    assert empty_done.attempt.effective_at is None


def test_database_unavailable_is_typed_fail_closed():
    def unavailable():
        raise RuntimeError("test database is unavailable")

    authority = OrtexQuotaAuthority(unavailable)
    decision = authority.reserve_bundle(
        bundle_sha256=_sha("db-down"),
        owner_token="owner",
        requests=(_request(50),),
    )
    assert decision.kind is OrtexQuotaDecisionKind.DB_UNAVAILABLE
    assert decision.permits == ()
    assert decision.may_call_transport is False
