from __future__ import annotations

from types import SimpleNamespace
import hashlib
import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.trading.momentum_neural import operator_actions
from app.services.trading.momentum_neural.captured_paper_dispatcher import (
    CapturedPaperRuntime,
)
from app.services.trading.momentum_neural.captured_paper_service_supervisor import (
    CapturedPaperServiceSupervisor,
)
from app.services.trading.momentum_neural.captured_paper_service_fence import (
    CAPTURED_PAPER_SERVICE_FENCE_CLASS_ID,
    CAPTURED_PAPER_SERVICE_FENCE_OBJECT_ID,
    CapturedPaperServiceFence,
    CapturedPaperServiceFenceError,
    read_captured_paper_prestart_admission_inventory,
    try_acquire_generic_alpaca_arm_fence,
)


class _StaticQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self._rows)

    def one_or_none(self):
        return self._rows[0] if self._rows else None


class _QueryWrappedSession:
    """Route advisory SQL to a real Session while supplying bounded rows."""

    def __init__(self, session: Session, rows):
        self._session = session
        self._rows = list(rows)

    def get_bind(self):
        return self._session.get_bind()

    def execute(self, *args, **kwargs):
        return self._session.execute(*args, **kwargs)

    def query(self, *_args, **_kwargs):
        return _StaticQuery(self._rows)


def _assert_postgresql(engine) -> None:
    if getattr(getattr(engine, "dialect", None), "name", "") != "postgresql":
        pytest.skip("captured service fence requires PostgreSQL")


@pytest.fixture()
def fence_engine():
    # These tests touch advisory-lock state only; they intentionally avoid the
    # heavyweight table-resetting ``db`` fixture.  Still prove the configured
    # engine is the dedicated test database before opening a lock session.
    from app.db import engine

    _assert_postgresql(engine)
    with engine.connect() as connection:
        database_name = str(
            connection.execute(text("SELECT current_database()"))
            .scalar_one()
        )
    if not database_name.lower().endswith("_test"):
        raise RuntimeError(
            "captured service fence tests require a *_test database"
        )
    return engine


def test_service_fence_and_generic_transaction_lock_are_mutually_exclusive(
    fence_engine,
):
    engine = fence_engine
    service = CapturedPaperServiceFence(engine)
    acquired = service.acquire()
    assert acquired["held"] is True
    assert acquired["account_scope"] == "alpaca:paper"
    service.assert_held()

    generic = Session(bind=engine)
    try:
        assert try_acquire_generic_alpaca_arm_fence(
            generic,
            account_scope="alpaca:paper",
        ) is False
        generic.rollback()

        released = service.release()
        assert released["held"] is False

        assert try_acquire_generic_alpaca_arm_fence(
            generic,
            account_scope="alpaca:paper",
        ) is True
        competing_service = CapturedPaperServiceFence(engine)
        with pytest.raises(
            CapturedPaperServiceFenceError,
            match="service_fence_busy",
        ):
            competing_service.acquire()
        generic.rollback()

        reacquired = competing_service.acquire()
        assert reacquired["held"] is True
        competing_service.release()
    finally:
        generic.rollback()
        generic.close()
        if service.health().get("held") is True:
            service.release()


def test_lost_session_lock_is_detected_and_does_not_block_generic_path(
    fence_engine,
):
    engine = fence_engine
    service = CapturedPaperServiceFence(engine)
    service.acquire()

    # Simulate an external/session-level lock loss on the same dedicated
    # backend.  assert_held must detect it and invalidate that DB session.
    connection = service._connection
    assert connection is not None
    assert connection.execute(
        text("SELECT pg_advisory_unlock(:class_id, :object_id)"),
        {
            "class_id": CAPTURED_PAPER_SERVICE_FENCE_CLASS_ID,
            "object_id": CAPTURED_PAPER_SERVICE_FENCE_OBJECT_ID,
        },
    ).scalar_one() is True
    connection.commit()

    with pytest.raises(CapturedPaperServiceFenceError, match="fence_lost"):
        service.assert_held()
    assert service.health()["held"] is False

    generic = Session(bind=engine)
    try:
        assert try_acquire_generic_alpaca_arm_fence(
            generic,
            account_scope="alpaca:paper",
        ) is True
    finally:
        generic.rollback()
        generic.close()


def test_committed_generic_claim_between_baseline_and_service_lock_is_visible(
    fence_engine,
):
    """Reproduce the exact historical pre-first-owner startup race.

    The generic transaction commits after the old restart baseline but before
    the service gets its session lock.  The mandatory fenced prestart snapshot
    must observe that new claim, so production rejects before provider startup.
    """

    engine = fence_engine
    symbol = f"F{uuid.uuid4().hex[:10]}".upper()
    claim_token = f"arm-{uuid.uuid4()}"
    baseline = read_captured_paper_prestart_admission_inventory(engine)

    generic = Session(bind=engine)
    service = None
    try:
        assert try_acquire_generic_alpaca_arm_fence(
            generic,
            account_scope="alpaca:paper",
        ) is True
        generic.execute(
            text(
                """
                INSERT INTO broker_symbol_action_claims (
                    account_scope, symbol, claim_token, action, phase,
                    owner_session_id, metadata_json, claimed_at, updated_at,
                    lease_expires_at, resolved_at
                ) VALUES (
                    'alpaca:paper', :symbol, :claim_token, 'entry', 'claimed',
                    NULL, '{}'::jsonb, clock_timestamp(), clock_timestamp(),
                    NULL, NULL
                )
                """
            ),
            {"symbol": symbol, "claim_token": claim_token},
        )
        generic.commit()

        service = CapturedPaperServiceFence(engine)
        service.acquire()
        observed = read_captured_paper_prestart_admission_inventory(engine)
        assert observed["active_action_claims"] == (
            baseline["active_action_claims"] + 1
        )
        assert observed["active_total"] == baseline["active_total"] + 1
        assert observed["empty"] is False
        service.release()
    finally:
        if service is not None and service.health().get("held") is True:
            service.release()
        generic.rollback()
        generic.close()
        with engine.begin() as cleanup:
            cleanup.execute(
                text(
                    "DELETE FROM broker_symbol_action_claims "
                    "WHERE account_scope = 'alpaca:paper' AND symbol = :symbol"
                ),
                {"symbol": symbol},
            )


def test_real_service_lock_is_continuous_through_callback_provider_and_runtime(
    fence_engine,
):
    engine = fence_engine
    events: list[str] = []
    running = {"provider": False}

    def assert_generic_blocked(label: str) -> None:
        contender = Session(bind=engine)
        try:
            assert try_acquire_generic_alpaca_arm_fence(
                contender,
                account_scope="alpaca:paper",
            ) is False
            events.append(label)
        finally:
            contender.rollback()
            contender.close()

    class _Host:
        def start_provider_loops(self, **_kwargs):
            assert_generic_blocked("provider_start")
            running["provider"] = True
            return {"binding_receipt_sha256": "b" * 64}

        def health(self):
            return {
                "provider_loop_supervisor": {
                    "state": "running" if running["provider"] else "stopped",
                    "all_ready": running["provider"],
                    "provider_sockets_started": running["provider"],
                    "failures": {},
                }
            }

        def close(self):
            events.append("provider_close")
            running["provider"] = False
            return self.health()

    class _Handle:
        def close(self):
            events.append("runtime_close")

    def revalidate():
        assert_generic_blocked("fenced_prestart")
        body = {
            "schema_version": "chili.captured-paper-fenced-prestart.v1",
            "verdict": "CAPTURED_ALPACA_PAPER_FENCED_PRESTART_REVALIDATED",
            "account_scope": "alpaca:paper",
            "expected_account_id": "3e0776af-76cd-4afd-8fe1-f2ee8dc6242f",
            "runtime_generation": "df0d0942-bbc0-4dc7-8218-ef387a8761db",
            "baseline_restart_gate_receipt_sha256": "c" * 64,
            "restart_gate_receipt_sha256": "d" * 64,
            "admission_inventory_sha256": "e" * 64,
            "durable_admission_drift": False,
            "broker_inventory_flat": True,
            "paper_execution_only": True,
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }
        encoded = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return {**body, "receipt_sha256": hashlib.sha256(encoded).hexdigest()}

    def register(_runtime):
        assert_generic_blocked("runtime_register")
        return _Handle()

    runtime = CapturedPaperRuntime(
        handler=lambda *_args: None,
        expected_account_id="3e0776af-76cd-4afd-8fe1-f2ee8dc6242f",
        code_build_sha256="a" * 64,
        config_sha256="b" * 64,
        capture_receipt_sha256="c" * 64,
        runtime_generation="df0d0942-bbc0-4dc7-8218-ef387a8761db",
        first_dip_policy_mode="candidate",
    )
    supervisor = CapturedPaperServiceSupervisor(
        host=_Host(),
        runtime=runtime,
        service_fence=CapturedPaperServiceFence(engine),
        fenced_prestart_revalidate=revalidate,
        managed_workers=(),
        live_loop_start=lambda: pytest.fail("no-order smoke cannot start loop"),
        live_loop_stop=lambda: True,
        live_loop_health=lambda: False,
        runtime_registrar=register,
    )

    health = supervisor.start_no_order_smoke()
    assert events == [
        "fenced_prestart",
        "provider_start",
        "runtime_register",
    ]
    assert health["service_fence"]["held"] is True
    supervisor.close(join_timeout_seconds=1.0, quiesce_timeout_seconds=1.0)
    assert events[-3:] == [
        "runtime_register",
        "runtime_close",
        "provider_close",
    ]

    generic = Session(bind=engine)
    try:
        assert try_acquire_generic_alpaca_arm_fence(
            generic,
            account_scope="alpaca:paper",
        ) is True
    finally:
        generic.rollback()
        generic.close()


def test_operator_begin_is_fenced_before_symbol_claim_or_external_work(
    fence_engine,
    monkeypatch,
):
    engine = fence_engine
    service = CapturedPaperServiceFence(engine)
    service.acquire()
    monkeypatch.setattr(
        operator_actions,
        "_alpaca_execution_quarantine_reason",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        operator_actions,
        "_live_symbol_arm_lock_acquired",
        lambda *_args, **_kwargs: pytest.fail(
            "symbol lock must be unreachable while service owns the fence"
        ),
    )

    arm_db = Session(bind=engine)
    try:
        result = operator_actions.begin_live_arm(
            arm_db,
            user_id=31,
            symbol="FENC",
            variant_id=7,
            execution_family="alpaca_spot",
        )
        assert result["error"] == "captured_paper_service_owns_alpaca_arm_path"
        assert not arm_db.new
        assert not arm_db.dirty
        assert not arm_db.deleted
    finally:
        arm_db.rollback()
        arm_db.close()
        service.release()


def test_operator_confirm_and_promote_are_fenced_before_mutation_or_identity_read(
    fence_engine,
    monkeypatch,
):
    engine = fence_engine
    service = CapturedPaperServiceFence(engine)
    service.acquire()
    monkeypatch.setattr(
        operator_actions,
        "_persisted_alpaca_execution_quarantine_reason",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        operator_actions,
        "_alpaca_execution_quarantine_reason",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        operator_actions,
        "is_momentum_automation_implemented",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        operator_actions,
        "_paper_promotion_gate",
        lambda *_args, **_kwargs: (True, None),
    )
    monkeypatch.setattr(
        operator_actions,
        "_certified_alpaca_account_id",
        lambda *_args, **_kwargs: pytest.fail(
            "broker account identity must be unreachable while fenced"
        ),
    )

    pending = SimpleNamespace(
        id=101,
        user_id=31,
        symbol="FENC",
        variant_id=7,
        state=operator_actions.STATE_LIVE_ARM_PENDING,
        execution_family="alpaca_spot",
        mode="live",
        risk_snapshot_json={
            "arm_token": "pending-token",
            "expires_at_utc": "2099-01-01T00:00:00",
            "alpaca_symbol_claim_token": "arm-pending-token",
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": "0cbec7c8-f364-4b5e-8b60-4e8fd6ab36cb",
        },
    )
    confirm_session = Session(bind=engine)
    try:
        result = operator_actions.confirm_live_arm(
            _QueryWrappedSession(confirm_session, [pending]),
            user_id=31,
            arm_token="pending-token",
            confirm=True,
        )
        assert result["error"] == "captured_paper_service_owns_alpaca_arm_path"
    finally:
        confirm_session.rollback()
        confirm_session.close()

    paper = SimpleNamespace(
        id=202,
        user_id=31,
        symbol="FENC",
        variant_id=7,
        execution_family="alpaca_spot",
        state=operator_actions.STATE_FINISHED,
    )
    promote_session = Session(bind=engine)
    try:
        result = operator_actions.promote_paper_session_to_live_arm(
            _QueryWrappedSession(promote_session, [paper]),
            user_id=31,
            paper_session_id=paper.id,
            execution_family="alpaca_spot",
        )
        assert result["error"] == "captured_paper_service_owns_alpaca_arm_path"
    finally:
        promote_session.rollback()
        promote_session.close()
        service.release()


def test_non_alpaca_operator_family_does_not_touch_the_captured_service_fence():
    class _NoDatabaseAccess:
        def get_bind(self):
            raise AssertionError("non-Alpaca family must not query process fence")

        def execute(self, *_args, **_kwargs):
            raise AssertionError("non-Alpaca family must not query process fence")

    assert operator_actions._generic_alpaca_arm_process_fence_acquired(
        _NoDatabaseAccess(),
        execution_family="coinbase_spot",
    ) is True


def test_generic_fence_fails_closed_for_wrong_scope_or_database_failure():
    class _BrokenDatabase:
        def get_bind(self):
            raise RuntimeError("database unavailable")

    assert try_acquire_generic_alpaca_arm_fence(
        _BrokenDatabase(),
        account_scope="alpaca:paper",
    ) is False
    assert try_acquire_generic_alpaca_arm_fence(
        _BrokenDatabase(),
        account_scope="alpaca:live",
    ) is False
