from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import inspect
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.db import engine
from app.models.trading import TradingAutomationEvent, TradingAutomationSession
from app.services.trading.momentum_neural import (
    captured_paper_initial_admission as initial,
)
from app.services.trading.momentum_neural import (
    captured_paper_initial_recovery as recovery,
)
from app.services.trading.momentum_neural import (
    captured_paper_pending_owner as pending_owner,
)
from app.services.trading.momentum_neural import (
    captured_paper_preowner_promotion as promotion,
)
from app.services.trading.momentum_neural.adaptive_risk_account_lock import (
    AdaptiveRiskAccountLockIdentity,
)
from tests.test_captured_paper_initial_admission import _pure_material
from tests.test_captured_paper_initial_admission import _commit, _seed_authority
from tests.test_captured_paper_preowner_promotion import (
    _dispatch,
    _preowner_receipt,
)


class _Result:
    def __init__(
        self,
        *,
        first=None,
        scalar=None,
        mapping=None,
        rows=None,
    ):
        self._first = first
        self._scalar = scalar
        self._mapping = mapping
        self._rows = [] if rows is None else rows

    def first(self):
        return self._first

    def scalar_one(self):
        return self._scalar

    def mappings(self):
        return self

    def one_or_none(self):
        return self._mapping

    def fetchall(self):
        return self._rows


class _ReleaseDb:
    def __init__(self, *, now, present_sql: str | None = None):
        self.now = now
        self.present_sql = present_sql
        self.statements: list[str] = []
        self.params: list[dict] = []
        self.events = []

    def in_transaction(self):
        return True

    def execute(self, statement, params=None):
        sql = str(statement)
        values = dict(params or {})
        self.statements.append(sql)
        self.params.append(values)
        if sql == "SELECT clock_timestamp()":
            return _Result(scalar=self.now)
        if self.present_sql is not None and self.present_sql in sql:
            return _Result(first=("present",))
        if sql.startswith("SELECT"):
            return _Result(first=None)
        if "UPDATE broker_symbol_action_claims" in sql:
            metadata = values["released_metadata"]
            import json

            return _Result(
                mapping={
                    "phase": "resolved",
                    "owner_session_id": values["session_id"],
                    "client_order_id": None,
                    "broker_order_id": None,
                    "metadata_json": json.loads(metadata),
                    "resolved_at": values["released_at"],
                }
            )
        if "UPDATE trading_automation_sessions" in sql:
            return _Result(
                mapping={
                    "id": values["session_id"],
                    "state": recovery.CAPTURED_PAPER_CANCELLED_STATE,
                    "ended_at": values["released_at"],
                }
            )
        raise AssertionError(sql)

    def add(self, value):
        self.events.append(value)

    def flush(self):
        return None


def _preowner_session(material, *, session_id=41):
    marker = initial._preowner_marker(
        material,
        session_id=session_id,
        claim_token=material.material_sha256,
    )
    return SimpleNamespace(
        id=session_id,
        user_id=material.user_id,
        venue="alpaca",
        execution_family=material.execution_family,
        mode="live",
        symbol=material.symbol,
        variant_id=material.variant_id,
        state=initial.CAPTURED_PAPER_PREOWNER_STATE,
        risk_snapshot_json=initial._risk_snapshot(material, marker),
        allocation_decision_json={},
        correlation_id=material.material_sha256,
        source_node_id="captured_paper_initial_admission",
        ended_at=None,
    )


def _pending_session(material, *, session_id=41):
    preowner = _preowner_receipt(material, session_id=session_id)
    request = _dispatch(material, session_id=session_id)
    projection = promotion.build_captured_paper_pending_owner_projection(
        material=material,
        preowner_receipt=preowner,
        dispatch_request=request,
        arm_token="7ddc5883-c493-4de4-a4e5-e3f959461bfd",
        confirmed_at=material.decision_at + timedelta(milliseconds=1),
    )
    session = SimpleNamespace(
        id=session_id,
        user_id=material.user_id,
        venue="alpaca",
        execution_family=material.execution_family,
        mode="live",
        symbol=material.symbol,
        variant_id=material.variant_id,
        state=promotion.CAPTURED_PAPER_PENDING_OWNER_STATE,
        risk_snapshot_json=promotion._canonical_value(
            projection.risk_snapshot
        ),
        allocation_decision_json={},
        correlation_id=material.material_sha256,
        source_node_id="captured_paper_preowner_promotion",
        ended_at=None,
    )
    validated = pending_owner.validate_captured_paper_pending_owner_inventory(
        session,
        expected_account_id=material.expected_account_id,
        expected_runtime_generation=material.runtime_generation,
        expected_execution_family=material.execution_family,
    )
    claim = {
        "account_scope": material.account_scope,
        "symbol": material.symbol,
        "claim_token": projection.arm.symbol_claim_token,
        "action": "entry",
        "phase": "claimed",
        "owner_session_id": session_id,
        "client_order_id": None,
        "broker_order_id": None,
        "metadata": promotion._canonical_value(
            projection.action_claim_metadata
        ),
        "lease_expires_at": material.expires_at,
        "resolved_at": None,
    }
    return session, validated, claim


def test_preowner_snapshot_persists_exact_typed_material_and_zero_economics():
    material = _pure_material()
    session = _preowner_session(material)
    marker = session.risk_snapshot_json[initial._PREOWNER_KEY]

    reconstructed = recovery._stored_material(session.risk_snapshot_json)

    assert reconstructed.to_dict() == material.to_dict()
    assert marker[initial.INITIAL_PREOWNER_MATERIAL_KEY] == material.to_dict()
    assert marker["opportunity_consumed"] is False
    assert marker["risk_reserved"] is False
    assert marker["outbox_created"] is False
    assert marker["order_posted"] is False
    assert marker["broker_order_post_calls"] == 0
    assert marker["content_sha256"] == promotion._sha256_json(
        {key: value for key, value in marker.items() if key != "content_sha256"}
    )


def test_stored_material_rejects_hash_or_shape_mutation():
    material = _pure_material()
    session = _preowner_session(material)
    snapshot = promotion._canonical_value(session.risk_snapshot_json)
    snapshot[initial._PREOWNER_KEY][initial.INITIAL_PREOWNER_MATERIAL_KEY][
        "config_sha256"
    ] = "f" * 64

    with pytest.raises(
        recovery.CapturedPaperInitialRecoveryError,
        match="initial_recovery_material_invalid",
    ):
        recovery._stored_material(snapshot)


def test_expired_preowner_release_is_one_atomic_zero_economic_transition():
    material = _pure_material()
    session = _preowner_session(material)
    receipt = _preowner_receipt(material, session_id=session.id)
    expected_metadata = promotion._expected_preowner_claim_metadata(
        material,
        preowner_marker_sha256=receipt.preowner_marker["content_sha256"],
    )
    db = _ReleaseDb(now=material.expires_at + timedelta(microseconds=1))

    result = recovery._release_exact_locked(
        db,
        session=session,
        claim={},
        material=material,
        prior_stage=initial.CAPTURED_PAPER_PREOWNER_STATE,
        prior_state=initial.CAPTURED_PAPER_PREOWNER_STATE,
        prior_source_node="captured_paper_initial_admission",
        prior_claim_token=material.material_sha256,
        expected_claim_metadata=expected_metadata,
        now=db.now,
        assert_service_fence_held=lambda: None,
    )

    assert result.disposition == "expired_released"
    assert result.created is True
    assert len(db.events) == 1
    assert db.events[0].event_type == (
        "captured_paper_initial_generation_expired_released"
    )
    assert db.events[0].payload_json["opportunity_consumed"] is False
    assert db.events[0].payload_json["risk_reserved"] is False
    claim_update = next(
        index
        for index, sql in enumerate(db.statements)
        if "UPDATE broker_symbol_action_claims" in sql
    )
    session_update = next(
        index
        for index, sql in enumerate(db.statements)
        if "UPDATE trading_automation_sessions" in sql
    )
    assert claim_update < session_update
    assert "clock_timestamp() >= :authority_expires_at" in db.statements[
        claim_update
    ]
    assert "client_order_id IS NULL" in db.statements[claim_update]
    assert "broker_order_id IS NULL" in db.statements[claim_update]
    assert "entry_transport_started" in db.statements[claim_update]
    assert "owner_transport" in db.statements[claim_update]


def test_committed_release_ack_loss_reconstructs_same_marker_without_mutation():
    material = _pure_material()
    session = _preowner_session(material)
    preowner = _preowner_receipt(material, session_id=session.id)
    expected_metadata = promotion._expected_preowner_claim_metadata(
        material,
        preowner_marker_sha256=preowner.preowner_marker["content_sha256"],
    )
    released_at = material.expires_at + timedelta(microseconds=1)
    first_db = _ReleaseDb(now=released_at)
    first = recovery._release_exact_locked(
        first_db,
        session=session,
        claim={},
        material=material,
        prior_stage=initial.CAPTURED_PAPER_PREOWNER_STATE,
        prior_state=initial.CAPTURED_PAPER_PREOWNER_STATE,
        prior_source_node="captured_paper_initial_admission",
        prior_claim_token=material.material_sha256,
        expected_claim_metadata=expected_metadata,
        now=released_at,
        assert_service_fence_held=lambda: None,
    )
    claim_params = next(
        params
        for sql, params in zip(first_db.statements, first_db.params)
        if "UPDATE broker_symbol_action_claims" in sql
    )
    import json

    terminal_session = _preowner_session(material)
    terminal_session.state = recovery.CAPTURED_PAPER_CANCELLED_STATE
    terminal_session.ended_at = released_at.replace(tzinfo=None)
    terminal_session.source_node_id = "captured_paper_initial_recovery"
    terminal_claim = {
        "account_scope": material.account_scope,
        "symbol": material.symbol,
        "claim_token": material.material_sha256,
        "action": "entry",
        "phase": "resolved",
        "owner_session_id": terminal_session.id,
        "client_order_id": None,
        "broker_order_id": None,
        "metadata": json.loads(claim_params["released_metadata"]),
        "lease_expires_at": None,
        "resolved_at": released_at,
    }

    class _AckDb(_ReleaseDb):
        def execute(self, statement, params=None):
            sql = str(statement)
            if "SELECT payload_json FROM trading_automation_events" in sql:
                self.statements.append(sql)
                self.params.append(dict(params or {}))
                return _Result(rows=[(first_db.events[0].payload_json,)])
            return super().execute(statement, params)

    retry_db = _AckDb(now=released_at + timedelta(seconds=1))
    retry = recovery._validate_existing_release_locked(
        retry_db,
        session=terminal_session,
        claim=terminal_claim,
        material=material,
    )

    assert retry.created is False
    assert retry.release_marker == first.release_marker
    assert not any(sql.startswith("UPDATE") for sql in retry_db.statements)
    assert retry_db.events == []


@pytest.mark.parametrize(
    ("present_sql", "reason"),
    (
        (
            "captured_paper_post_commit_outbox",
            "initial_recovery_outbox_present",
        ),
        (
            "adaptive_risk_reservations",
            "initial_recovery_active_reservation_present",
        ),
        (
            "adaptive_risk_opportunity_claims",
            "initial_recovery_opportunity_side_effect_present",
        ),
    ),
)
def test_release_fails_before_mutation_when_economic_evidence_exists(
    present_sql,
    reason,
):
    material = _pure_material()
    session = _preowner_session(material)
    preowner = _preowner_receipt(material, session_id=session.id)
    db = _ReleaseDb(
        now=material.expires_at + timedelta(seconds=1),
        present_sql=present_sql,
    )

    with pytest.raises(recovery.CapturedPaperInitialRecoveryError, match=reason):
        recovery._release_exact_locked(
            db,
            session=session,
            claim={},
            material=material,
            prior_stage=initial.CAPTURED_PAPER_PREOWNER_STATE,
            prior_state=initial.CAPTURED_PAPER_PREOWNER_STATE,
            prior_source_node="captured_paper_initial_admission",
            prior_claim_token=material.material_sha256,
            expected_claim_metadata=(
                promotion._expected_preowner_claim_metadata(
                    material,
                    preowner_marker_sha256=(
                        preowner.preowner_marker["content_sha256"]
                    ),
                )
            ),
            now=db.now,
            assert_service_fence_held=lambda: None,
        )

    assert not any(sql.startswith("UPDATE") for sql in db.statements)
    assert db.events == []


def test_expired_pending_release_uses_same_fail_closed_cas():
    material = _pure_material()
    session, validated, claim = _pending_session(material)
    db = _ReleaseDb(now=material.expires_at + timedelta(microseconds=1))

    receipt = recovery.release_expired_captured_paper_pending_owner_locked(
        db,
        session=session,
        claim=claim,
        validated_pending=validated,
        account_lock_identity=AdaptiveRiskAccountLockIdentity.for_scope(
            initial.ALPACA_PAPER_ACCOUNT_SCOPE
        ),
        assert_service_fence_held=lambda: None,
    )

    assert receipt.disposition == "expired_released"
    assert receipt.prior_claim_token == validated.projection.arm.symbol_claim_token
    assert receipt.release_marker["prior_stage"] == (
        promotion.CAPTURED_PAPER_PENDING_OWNER_STAGE
    )
    assert len(db.events) == 1


def test_pending_release_before_expiry_is_zero_mutation():
    material = _pure_material()
    session, validated, claim = _pending_session(material)
    db = _ReleaseDb(now=material.expires_at - timedelta(microseconds=1))

    with pytest.raises(
        recovery.CapturedPaperInitialRecoveryError,
        match="initial_recovery_generation_not_expired",
    ):
        recovery.release_expired_captured_paper_pending_owner_locked(
            db,
            session=session,
            claim=claim,
            validated_pending=validated,
            account_lock_identity=AdaptiveRiskAccountLockIdentity.for_scope(
                initial.ALPACA_PAPER_ACCOUNT_SCOPE
            ),
            assert_service_fence_held=lambda: None,
        )

    assert not any(sql.startswith("UPDATE") for sql in db.statements)
    assert db.events == []


def test_preowner_recovery_calls_existing_promotion_after_lock_transaction(
    monkeypatch,
):
    material = _pure_material()
    session = _preowner_session(material)
    preowner = _preowner_receipt(material, session_id=session.id)
    claim = {
        "account_scope": material.account_scope,
        "symbol": material.symbol,
        "claim_token": material.material_sha256,
        "action": "entry",
        "phase": "claimed",
        "owner_session_id": session.id,
        "client_order_id": None,
        "broker_order_id": None,
        "metadata": promotion._expected_preowner_claim_metadata(
            material,
            preowner_marker_sha256=preowner.preowner_marker["content_sha256"],
        ),
        "lease_expires_at": material.expires_at,
        "resolved_at": None,
    }
    state = {"transaction_active": False, "promoted": 0}

    class _Query:
        def populate_existing(self):
            return self

        def filter(self, *_args):
            return self

        def with_for_update(self):
            return self

        def one_or_none(self):
            return session

    class _Db(_ReleaseDb):
        def __init__(self):
            super().__init__(
                now=material.decision_at + timedelta(milliseconds=2)
            )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @contextmanager
        def begin(self):
            state["transaction_active"] = True
            try:
                yield self
            finally:
                state["transaction_active"] = False

        def query(self, *_args):
            return _Query()

    monkeypatch.setattr(recovery, "Session", lambda **_kwargs: _Db())
    monkeypatch.setattr(
        recovery,
        "acquire_adaptive_risk_account_locks",
        lambda *_args, **_kwargs: AdaptiveRiskAccountLockIdentity.for_scope(
            initial.ALPACA_PAPER_ACCOUNT_SCOPE
        ),
    )
    monkeypatch.setattr(
        recovery,
        "read_action_claim",
        lambda *_args, **_kwargs: (True, claim),
    )

    def _promote(*_args, **kwargs):
        assert state["transaction_active"] is False
        assert kwargs["material"].material_sha256 == material.material_sha256
        state["promoted"] += 1
        return SimpleNamespace(
            session_id=session.id,
            receipt_sha256="a" * 64,
            created=True,
        )

    monkeypatch.setattr(promotion, "promote_captured_paper_preowner", _promote)

    receipt = recovery.recover_captured_paper_initial_preowner(
        engine,
        session_id=session.id,
        expected_account_id=material.expected_account_id,
        expected_runtime_generation=material.runtime_generation,
        expected_code_build_sha256=material.code_build_sha256,
        expected_config_sha256=material.config_sha256,
        expected_capture_receipt_sha256=material.capture_receipt_sha256,
        assert_service_fence_held=lambda: None,
    )

    assert state["promoted"] == 1
    assert receipt.disposition == "pending_owner_recovered"
    assert receipt.pending_owner_receipt_sha256 == "a" * 64


def test_symbol_recovery_zero_rows_returns_none_without_delegate(monkeypatch):
    state = {"transaction_active": False, "delegated": 0}

    class _Query:
        def populate_existing(self):
            return self

        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def with_for_update(self):
            return self

        def all(self):
            return []

    class _Db:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @contextmanager
        def begin(self):
            state["transaction_active"] = True
            try:
                yield self
            finally:
                state["transaction_active"] = False

        def query(self, *_args):
            return _Query()

    monkeypatch.setattr(recovery, "Session", lambda **_kwargs: _Db())
    monkeypatch.setattr(
        recovery, "acquire_adaptive_risk_account_locks", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        recovery,
        "recover_captured_paper_initial_preowner",
        lambda *_a, **_k: state.__setitem__("delegated", 1),
    )
    material = _pure_material()

    result = recovery.recover_captured_paper_initial_symbol(
        engine,
        symbol=material.symbol,
        expected_account_id=material.expected_account_id,
        expected_runtime_generation=material.runtime_generation,
        expected_code_build_sha256=material.code_build_sha256,
        expected_config_sha256=material.config_sha256,
        expected_capture_receipt_sha256=material.capture_receipt_sha256,
        assert_service_fence_held=lambda: None,
    )

    assert result is None
    assert state == {"transaction_active": False, "delegated": 0}


def test_symbol_recovery_delegates_exact_row_after_inventory_transaction(
    monkeypatch,
):
    material = _pure_material()
    session = _preowner_session(material)
    state = {"transaction_active": False, "delegated": 0}

    class _Query:
        def populate_existing(self):
            return self

        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def with_for_update(self):
            return self

        def all(self):
            return [session]

    class _Db:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @contextmanager
        def begin(self):
            state["transaction_active"] = True
            try:
                yield self
            finally:
                state["transaction_active"] = False

        def query(self, *_args):
            return _Query()

    monkeypatch.setattr(recovery, "Session", lambda **_kwargs: _Db())
    monkeypatch.setattr(
        recovery, "acquire_adaptive_risk_account_locks", lambda *_a, **_k: None
    )
    expected = SimpleNamespace(receipt_sha256="b" * 64)

    def _delegate(*_args, **kwargs):
        assert state["transaction_active"] is False
        assert kwargs["session_id"] == session.id
        assert kwargs["expected_account_id"] == material.expected_account_id
        state["delegated"] += 1
        return expected

    monkeypatch.setattr(
        recovery, "recover_captured_paper_initial_preowner", _delegate
    )

    result = recovery.recover_captured_paper_initial_symbol(
        engine,
        symbol=material.symbol,
        expected_account_id=material.expected_account_id,
        expected_runtime_generation=material.runtime_generation,
        expected_code_build_sha256=material.code_build_sha256,
        expected_config_sha256=material.config_sha256,
        expected_capture_receipt_sha256=material.capture_receipt_sha256,
        assert_service_fence_held=lambda: None,
    )

    assert result is expected
    assert state == {"transaction_active": False, "delegated": 1}


@pytest.mark.parametrize("failure", ("ambiguous", "foreign", "identity"))
def test_symbol_recovery_foreign_ambiguous_or_identity_drift_fails_closed(
    monkeypatch,
    failure,
):
    material = _pure_material()
    first = _preowner_session(material)
    second = _preowner_session(material, session_id=42)
    rows = [first, second] if failure == "ambiguous" else [first]
    if failure == "foreign":
        first.venue = "robinhood"

    class _Query:
        def populate_existing(self):
            return self

        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def with_for_update(self):
            return self

        def all(self):
            return rows

    class _Db:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @contextmanager
        def begin(self):
            yield self

        def query(self, *_args):
            return _Query()

    monkeypatch.setattr(recovery, "Session", lambda **_kwargs: _Db())
    monkeypatch.setattr(
        recovery, "acquire_adaptive_risk_account_locks", lambda *_a, **_k: None
    )
    delegated = []
    monkeypatch.setattr(
        recovery,
        "recover_captured_paper_initial_preowner",
        lambda *_a, **_k: delegated.append(True),
    )
    expected_generation = (
        "00000000-0000-0000-0000-000000000001"
        if failure == "identity"
        else material.runtime_generation
    )

    with pytest.raises(recovery.CapturedPaperInitialRecoveryError):
        recovery.recover_captured_paper_initial_symbol(
            engine,
            symbol=material.symbol,
            expected_account_id=material.expected_account_id,
            expected_runtime_generation=expected_generation,
            expected_code_build_sha256=material.code_build_sha256,
            expected_config_sha256=material.config_sha256,
            expected_capture_receipt_sha256=(
                material.capture_receipt_sha256
            ),
            assert_service_fence_held=lambda: None,
        )

    assert delegated == []


def test_recovery_module_has_no_provider_adapter_reservation_outbox_or_post_capability():
    source = inspect.getsource(recovery)
    forbidden = (
        "requests.",
        "httpx.",
        "AlpacaSpotAdapter(",
        "post_limit_buy(",
        "post_limit_sell(",
        "reserve_adaptive_risk(",
        "claim_adaptive_risk_opportunity(",
        "commit_captured_paper_outbox(",
    )
    assert all(value not in source for value in forbidden)


def _cleanup_real_recovery_symbol(symbol: str) -> None:
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT id, user_id, variant_id FROM trading_automation_sessions"
                " WHERE symbol = :symbol FOR UPDATE"
            ),
            {"symbol": symbol},
        ).mappings().all()
        session_ids = [int(row["id"]) for row in rows]
        user_ids = {
            int(row["user_id"])
            for row in rows
            if row["user_id"] is not None
        }
        variant_ids = {int(row["variant_id"]) for row in rows}
        for session_id in session_ids:
            for table_name in (
                "trading_automation_events",
                "trading_automation_runtime_snapshots",
                "trading_automation_session_bindings",
                "trading_automation_simulated_fills",
                "momentum_automation_outcomes",
            ):
                connection.execute(
                    text(f"DELETE FROM {table_name} WHERE session_id = :session_id"),
                    {"session_id": session_id},
                )
            connection.execute(
                text(
                    "DELETE FROM trading_decision_packets"
                    " WHERE automation_session_id = :session_id"
                ),
                {"session_id": session_id},
            )
        connection.execute(
            text(
                "DELETE FROM broker_symbol_action_claims"
                " WHERE account_scope = 'alpaca:paper' AND symbol = :symbol"
            ),
            {"symbol": symbol},
        )
        connection.execute(
            text("DELETE FROM momentum_fill_outcomes WHERE symbol = :symbol"),
            {"symbol": symbol},
        )
        connection.execute(
            text("DELETE FROM momentum_symbol_viability WHERE symbol = :symbol"),
            {"symbol": symbol},
        )
        connection.execute(
            text("DELETE FROM trading_automation_sessions WHERE symbol = :symbol"),
            {"symbol": symbol},
        )
        for variant_id in variant_ids:
            connection.execute(
                text("DELETE FROM momentum_strategy_variants WHERE id = :id"),
                {"id": variant_id},
            )
        for user_id in user_ids:
            connection.execute(
                text(
                    "DELETE FROM users WHERE id = :id"
                    " AND name LIKE 'captured-preowner-%'"
                ),
                {"id": user_id},
            )


def test_real_db_expired_preowner_release_is_atomic_and_ack_idempotent(
    db,
    request,
):
    request.addfinalizer(lambda: _cleanup_real_recovery_symbol("RCVR"))
    material, _ = _seed_authority(db, symbol="RCVR")
    material = replace(
        material,
        expires_at=material.decision_at + timedelta(milliseconds=5),
    )
    preowner = _commit(material)

    first = recovery.recover_captured_paper_initial_preowner(
        engine,
        session_id=preowner.session_id,
        expected_account_id=material.expected_account_id,
        expected_runtime_generation=material.runtime_generation,
        expected_code_build_sha256=material.code_build_sha256,
        expected_config_sha256=material.config_sha256,
        expected_capture_receipt_sha256=material.capture_receipt_sha256,
        assert_service_fence_held=lambda: None,
    )
    second = recovery.recover_captured_paper_initial_preowner(
        engine,
        session_id=preowner.session_id,
        expected_account_id=material.expected_account_id,
        expected_runtime_generation=material.runtime_generation,
        expected_code_build_sha256=material.code_build_sha256,
        expected_config_sha256=material.config_sha256,
        expected_capture_receipt_sha256=material.capture_receipt_sha256,
        assert_service_fence_held=lambda: None,
    )

    db.rollback()
    session = db.get(TradingAutomationSession, preowner.session_id)
    claim = db.execute(
        text(
            "SELECT phase, client_order_id, broker_order_id, metadata_json,"
            " lease_expires_at FROM broker_symbol_action_claims"
            " WHERE account_scope = :scope AND symbol = :symbol"
        ),
        {"scope": material.account_scope, "symbol": material.symbol},
    ).mappings().one()
    events = (
        db.query(TradingAutomationEvent)
        .filter(
            TradingAutomationEvent.session_id == preowner.session_id,
            TradingAutomationEvent.event_type
            == "captured_paper_initial_generation_expired_released",
        )
        .all()
    )

    assert first.disposition == "expired_released"
    assert first.created is True
    assert second.created is False
    assert second.release_marker == first.release_marker
    assert session is not None
    assert session.state == recovery.CAPTURED_PAPER_CANCELLED_STATE
    assert session.ended_at is not None
    assert claim["phase"] == "resolved"
    assert claim["client_order_id"] is None
    assert claim["broker_order_id"] is None
    assert claim["lease_expires_at"] is None
    assert claim["metadata_json"][recovery.INITIAL_RELEASE_METADATA_KEY][
        "content_sha256"
    ] == first.release_marker["content_sha256"]
    assert len(events) == 1
    assert db.execute(
        text(
            "SELECT count(*) FROM captured_paper_post_commit_outbox"
            " WHERE session_id = :session_id"
        ),
        {"session_id": preowner.session_id},
    ).scalar_one() == 0
    assert db.execute(
        text(
            "SELECT count(*) FROM adaptive_risk_reservations"
            " WHERE account_scope = :scope AND symbol = :symbol"
        ),
        {"scope": material.account_scope, "symbol": material.symbol},
    ).scalar_one() == 0
    db.rollback()
