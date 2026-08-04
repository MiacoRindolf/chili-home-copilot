from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app import migrations
from app.db import engine


EXPECTED_COLUMNS = {
    "attempt_id",
    "plan_scope",
    "month_start",
    "bundle_sha256",
    "bundle_index",
    "owner_token",
    "request_sha256",
    "provider",
    "endpoint",
    "symbol",
    "status",
    "monthly_limit",
    "quota_used_after",
    "reserved_at",
    "lease_expires_at",
    "transport_started_at",
    "completed_at",
    "refunded_at",
    "provider_outcome",
    "http_status",
    "backoff_until",
    "transient_streak",
    "raw_response_body_b64",
    "response_sha256",
    "provider_event_at",
    "effective_at",
    "received_at",
    "available_at",
    "created_at",
    "updated_at",
}


def _fingerprint(conn) -> tuple:
    rows = conn.execute(
        text(
            """
            SELECT column_name, data_type, is_nullable, column_default
              FROM information_schema.columns
             WHERE table_schema = ANY(current_schemas(false))
               AND table_name = 'momentum_ortex_request_attempts'
             ORDER BY ordinal_position
            """
        )
    ).tuples().all()
    indexes = conn.execute(
        text(
            """
            SELECT indexname, indexdef
              FROM pg_indexes
             WHERE schemaname = ANY(current_schemas(false))
               AND tablename = 'momentum_ortex_request_attempts'
             ORDER BY indexname
            """
        )
    ).tuples().all()
    constraints = conn.execute(
        text(
            """
            SELECT conname, pg_get_constraintdef(oid, true)
              FROM pg_constraint
             WHERE conrelid = 'momentum_ortex_request_attempts'::regclass
             ORDER BY conname
            """
        )
    ).tuples().all()
    return tuple(rows), tuple(indexes), tuple(constraints)


def _assert_physical_completion_guards(conn) -> None:
    base = {
        "attempt_id": uuid.uuid4(),
        "bundle_sha256": "a" * 64,
        "request_sha256": "b" * 64,
    }
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(
            text(
                """
                INSERT INTO momentum_ortex_request_attempts (
                    attempt_id, plan_scope, month_start, bundle_sha256,
                    bundle_index, owner_token, request_sha256, provider,
                    endpoint, symbol, status, monthly_limit, quota_used_after,
                    reserved_at, lease_expires_at, transport_started_at,
                    completed_at, provider_outcome, created_at, updated_at
                ) VALUES (
                    :attempt_id, 'ortex:trader-1000:v1', DATE '2026-07-01',
                    :bundle_sha256, 0, 'guard-test', :request_sha256, 'ortex',
                    '/api/v1/stock/nasdaq/TEST/short_interest', 'TEST',
                    'completed', 1000, 1,
                    TIMESTAMPTZ '2026-07-08 12:00:00+00',
                    TIMESTAMPTZ '2026-07-08 12:01:00+00',
                    TIMESTAMPTZ '2026-07-08 12:00:01+00',
                    TIMESTAMPTZ '2026-07-08 12:00:02+00',
                    'success',
                    TIMESTAMPTZ '2026-07-08 12:00:00+00',
                    TIMESTAMPTZ '2026-07-08 12:00:00+00'
                )
                """
            ),
            base,
        )
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(
            text(
                """
                INSERT INTO momentum_ortex_request_attempts (
                    attempt_id, plan_scope, month_start, bundle_sha256,
                    bundle_index, owner_token, request_sha256, provider,
                    endpoint, symbol, status, monthly_limit, quota_used_after,
                    reserved_at, lease_expires_at, transport_started_at,
                    completed_at, provider_outcome, backoff_until,
                    created_at, updated_at
                ) VALUES (
                    :attempt_id, 'ortex:trader-1000:v1', DATE '2026-07-01',
                    :bundle_sha256, 0, 'guard-test', :request_sha256, 'ortex',
                    '/api/v1/stock/nasdaq/TEST/short_interest', 'TEST',
                    'completed', 1000, 1,
                    TIMESTAMPTZ '2026-07-08 12:00:00+00',
                    TIMESTAMPTZ '2026-07-08 12:01:00+00',
                    TIMESTAMPTZ '2026-07-08 12:00:02+00',
                    TIMESTAMPTZ '2026-07-08 12:00:01+00',
                    'permanent_error',
                    TIMESTAMPTZ '2026-07-08 12:05:00+00',
                    TIMESTAMPTZ '2026-07-08 12:00:00+00',
                    TIMESTAMPTZ '2026-07-08 12:00:00+00'
                )
                """
            ),
            {**base, "attempt_id": uuid.uuid4()},
        )


def test_migration_355_handles_old_partial_current_and_reapply(db):
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS momentum_ortex_request_attempts"))
        migrations._migration_355_ortex_monthly_request_authority(conn)
        migrations._verify_migration_355_physical_contract(conn)
        assert {c["name"] for c in inspect(conn).get_columns(
            "momentum_ortex_request_attempts"
        )} == EXPECTED_COLUMNS
        old_fingerprint = _fingerprint(conn)

        migrations._migration_355_ortex_monthly_request_authority(conn)
        migrations._verify_migration_355_physical_contract(conn)
        assert _fingerprint(conn) == old_fingerprint

        conn.execute(text("DROP TABLE momentum_ortex_request_attempts"))
        conn.execute(
            text(
                """
                CREATE TABLE momentum_ortex_request_attempts (
                    attempt_id UUID PRIMARY KEY
                )
                """
            )
        )
        migrations._migration_355_ortex_monthly_request_authority(conn)
        migrations._verify_migration_355_physical_contract(conn)
        partial_fingerprint = _fingerprint(conn)

        migrations._migration_355_ortex_monthly_request_authority(conn)
        migrations._verify_migration_355_physical_contract(conn)
        _assert_physical_completion_guards(conn)
        assert _fingerprint(conn) == partial_fingerprint == old_fingerprint


def test_migration_355_is_registered_once_after_354():
    ids = [version_id for version_id, _ in migrations.MIGRATIONS]
    current = "355_ortex_monthly_request_authority"
    assert ids.count(current) == 1
    index = ids.index(current)
    assert ids[index - 1] == (
        "354_alpaca_exit_owner_and_post_settlement_exit_v2"
    )
    assert ids.count("355_ortex_monthly_request_authority") == 1
    migrations._assert_migration_ids_unique()


def test_migration_355_verifier_rejects_same_named_wrong_guards(db):
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS momentum_ortex_request_attempts"))
        migrations._migration_355_ortex_monthly_request_authority(conn)

        conn.execute(
            text(
                "ALTER TABLE momentum_ortex_request_attempts "
                "DROP CONSTRAINT ck_ortex_attempt_completion_evidence"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE momentum_ortex_request_attempts "
                "ADD CONSTRAINT ck_ortex_attempt_completion_evidence "
                "CHECK (true)"
            )
        )
        with pytest.raises(
            RuntimeError,
            match="constraint definition differs",
        ):
            migrations._verify_migration_355_physical_contract(conn)

        conn.execute(text("DROP TABLE momentum_ortex_request_attempts"))
        migrations._migration_355_ortex_monthly_request_authority(conn)
        conn.execute(text("DROP INDEX ix_ortex_attempt_backoff"))
        conn.execute(
            text(
                "CREATE INDEX ix_ortex_attempt_backoff "
                "ON momentum_ortex_request_attempts (symbol)"
            )
        )
        with pytest.raises(
            RuntimeError,
            match="index contract differs",
        ):
            migrations._verify_migration_355_physical_contract(conn)

        conn.execute(text("DROP TABLE momentum_ortex_request_attempts"))
        migrations._migration_355_ortex_monthly_request_authority(conn)
