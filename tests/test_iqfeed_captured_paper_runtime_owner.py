from __future__ import annotations

from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import engine
from app.services.trading.momentum_neural.adaptive_risk_account_lock import (
    AdaptiveRiskAccountLockIdentity,
)
from app.services.trading.momentum_neural import live_runner
from app.services.trading.momentum_neural.captured_paper_entry_intent import (
    CapturedPaperPostCommitRequest,
)
from app.services.trading.momentum_neural.captured_paper_selection import (
    CapturedPaperSelectionContext,
    captured_paper_candidate_generation_sha256,
    require_captured_paper_selection,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureContractError,
)
from scripts import iqfeed_capture_host as host_module
from tests.test_captured_paper_admission import (
    _inputs as _admission_inputs,
    _pre_reservation_authority,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 15, 14, 31, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _stub_phase_one_ledger(monkeypatch):
    """Owner unit tests isolate RAM handoff mechanics from PostgreSQL."""

    monkeypatch.setattr(
        host_module,
        "record_captured_paper_phase_one_handoff",
        lambda *_args, **_kwargs: SimpleNamespace(state="pending"),
    )
    monkeypatch.setattr(
        host_module,
        "acknowledge_captured_paper_phase_one_handoff",
        lambda *_args, **_kwargs: SimpleNamespace(state="outbox_committed"),
    )

    def activate_owner(
        db,
        *,
        request,
        account_lock_identity,
        assert_service_fence_held,
    ):
        assert getattr(db, "tick_completed", False) is False
        assert account_lock_identity == AdaptiveRiskAccountLockIdentity.for_scope(
            request.account_scope
        )
        assert_service_fence_held()
        db.owner_activation_calls.append((request, account_lock_identity))
        db.call_order.append("owner_activation")
        return {"content_sha256": request.provenance_sha256}

    monkeypatch.setattr(
        host_module,
        "activate_captured_paper_session_owner_before_tick",
        activate_owner,
    )


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class _RuntimeDb:
    def __init__(self) -> None:
        self.transaction_active = False
        self.lock_sql = []
        self.tick_completed = False
        self.owner_activation_calls = []
        self.call_order = []

    def begin(self):
        self.transaction_active = True
        return object()

    def in_transaction(self):
        return self.transaction_active

    def execute(self, statement, parameters=None):
        self.lock_sql.append((str(statement), dict(parameters or {})))
        return None


def _selection_marker(arm):
    return {
        "version": 1,
        "session_id": arm.session_id,
        "arm_token": arm.arm_token,
        "expires_at_utc": arm.expires_at.isoformat(),
        "alpaca_symbol_claim_token": arm.symbol_claim_token,
        "alpaca_account_scope": arm.account_scope,
        "alpaca_account_id": arm.expected_account_id,
        "confirmed_at_utc": arm.confirmed_at.isoformat(),
    }


def _material() -> host_module.IqfeedCapturedPaperDecisionMaterial:
    original = _admission_inputs(
        now=NOW,
        setup_family="primary_entry",
        first_dip_policy_mode="candidate",
    )
    proof = original.active_input_attestation
    authority = original.broker_account_facts.capture_authority
    bbo_payload = json.loads(original.exact_bbo.payload_json)
    bbo_receipt_sha = next(
        row.receipt_sha256
        for row in proof.read_evidence
        if row.receipt.read_id == original.exact_bbo.read_id
    )
    arm = original.post_commit_request.intent.confirmed_arm_generation
    decision_at = original.post_commit_request.intent.decision_at
    marker = _selection_marker(arm)
    trigger_debug = {"pullback_low": original.economics.structural_stop}
    candidate_generation_sha256 = captured_paper_candidate_generation_sha256(
        session_id=original.dispatch_request.session_id,
        symbol=original.dispatch_request.symbol,
        execution_family=original.dispatch_request.execution_family,
        entry_place_count=1,
        client_order_id=proof.decision_id,
        setup_family="primary_entry",
        structural_stop_price=original.economics.structural_stop,
        trigger_reason="pullback_break",
        trigger_debug=trigger_debug,
        confirmed_arm_marker=marker,
        viability_updated_at=decision_at - timedelta(seconds=1),
        viability_score=original.economics.setup_quality,
        viability_payload_sha256=_digest("runtime-viability-payload"),
        execution_readiness_sha256=_digest("runtime-execution-readiness"),
    )
    context = CapturedPaperSelectionContext.create(
        dispatch_request=original.dispatch_request,
        confirmed_arm_generation=arm,
        confirmed_arm_marker=marker,
        entry_place_count=1,
        client_order_id=proof.decision_id,
        setup_family="primary_entry",
        decision_at=decision_at,
        evidence_available_at=proof.attested_available_at,
        evidence_expires_at=proof.expires_at,
        bid=bbo_payload["bid"],
        ask=bbo_payload["ask"],
        structural_stop_price=original.economics.structural_stop,
        entry_limit_ceiling_price=bbo_payload["ask"],
        trigger_reason="pullback_break",
        trigger_debug=trigger_debug,
        candidate_generation_sha256=candidate_generation_sha256,
        viability_updated_at=decision_at - timedelta(seconds=1),
        viability_score=original.economics.setup_quality,
        viability_payload_sha256=_digest("runtime-viability-payload"),
        execution_readiness_sha256=_digest("runtime-execution-readiness"),
        account_receipt_sha256=authority.account_read_receipt_sha256,
        bbo_receipt_sha256=bbo_receipt_sha,
        setup_evidence_sha256=(
            original.post_commit_request.intent.setup_evidence_sha256
        ),
        policy_sha256=original.policy_spec.policy.policy_sha256,
        feature_flags_sha256=proof.feature_flags_sha256,
        opportunity_key=None,
    )
    admission = replace(
        original,
        dispatch_request=context.dispatch_request,
        post_commit_request=context.draft,
    )
    return host_module.IqfeedCapturedPaperDecisionMaterial(
        selection_context=context,
        admission_inputs=admission,
        predecision_captured_reads=admission.predecision_captured_reads,
        predecision_executed_read_inventory=(
            admission.executed_read_inventory
        ),
    )


class _FakeHost(host_module.IqfeedCaptureHost):
    def __init__(self, result):
        self.result = result
        self.tick_calls = []
        self.post_commit_scopes = 0

    def tick_captured_alpaca_paper_session(
        self,
        db,
        *,
        dispatch_request,
        decision_material,
        adapter_factory,
    ):
        self.tick_calls.append(
            (db, dispatch_request, decision_material, adapter_factory)
        )
        db.call_order.append("fsm_tick")
        result = host_module.IqfeedCapturedPaperTickResult(
            decision_at=decision_material.selection_context.draft.intent.decision_at,
            fsm_result=(
                decision_material.selection_context.draft
                if self.result == "typed"
                else {"ok": True, "deferred": True, "reason": self.result}
            ),
            first_dip_final_capture_frontier=None,
            scanner_snapshot_read_ids=(),
            ohlcv_read_ids=(),
            microstructure_read_ids=(),
            captured_reads=decision_material.predecision_captured_reads,
            executed_read_inventory=(
                decision_material.predecision_executed_read_inventory
            ),
        )
        db.tick_completed = True
        return result

    @contextmanager
    def captured_paper_post_commit_scope(self, material):
        material.verify()
        self.post_commit_scopes += 1
        yield None


class _FinancialBreakerIssuer:
    def __init__(self, material):
        authority = _pre_reservation_authority(
            material.admission_inputs,
            now=material.selection_context.draft.intent.decision_at
            + timedelta(milliseconds=1),
        )
        self.receipt = authority["financial_breaker_receipt"]
        self.verification_at = authority["financial_breaker_verification_at"]
        self.calls = []

    def issue_for_request(self, request, *, phase):
        self.calls.append((request.completion_sha256, phase))
        assert phase == "pre_reservation"
        return self.receipt


def _owner(
    fake_host,
    material,
    *,
    clock=lambda: 10.0,
    assert_service_fence_held=lambda: None,
):
    route = material.selection_context.dispatch_request
    financial_issuer = _FinancialBreakerIssuer(material)
    return host_module.IqfeedCapturedPaperRuntimeOwner(
        host=fake_host,
        adapter_factory=lambda: object(),
        admission_bind=engine,
        expected_account_id=route.expected_account_id,
        code_build_sha256=route.code_build_sha256,
        config_sha256=route.config_sha256,
        capture_receipt_sha256=route.capture_receipt_sha256,
        runtime_generation=route.runtime_generation,
        first_dip_policy_mode=route.first_dip_policy_mode,
        decision_max_entries=2,
        decision_ttl_seconds=5.0,
        admission_max_entries=2,
        admission_ttl_seconds=5.0,
        allow_manual_staging=True,
        financial_breaker_issuer=financial_issuer,
        financial_breaker_clock=lambda: financial_issuer.verification_at,
        assert_service_fence_held=assert_service_fence_held,
        monotonic_clock=clock,
    )


def test_material_binds_route_arm_decision_bbo_stop_receipts_and_policy() -> None:
    material = _material()
    material.verify()

    intent = material.selection_context.draft.intent
    assert material.material_sha256
    assert intent.confirmed_arm_generation.confirmed_arm_generation_sha256
    assert intent.bbo_receipt_sha256
    assert intent.account_receipt_sha256
    assert intent.policy_sha256 == material.admission_inputs.policy_spec.policy.policy_sha256
    assert intent.feature_flags_sha256 == (
        material.admission_inputs.active_input_attestation.feature_flags_sha256
    )


def test_material_rejects_any_prelocked_adaptive_source_contract() -> None:
    material = _material()
    field_names = {field.name for field in fields(type(material))}

    assert "adaptive_runtime_material" not in field_names
    assert "adaptive_source" not in field_names
    assert "locked_alpaca_paper_admission_bundle" not in field_names
    assert "adaptive_source_sha256" not in material._verified_body()
    with pytest.raises(TypeError, match="adaptive_runtime_material"):
        host_module.IqfeedCapturedPaperDecisionMaterial(
            selection_context=material.selection_context,
            admission_inputs=material.admission_inputs,
            adaptive_runtime_material=object(),
        )


def test_expired_initial_recovery_short_circuits_before_material_or_fsm(
    monkeypatch,
) -> None:
    material = _material()
    fake_host = _FakeHost("must_not_tick")
    owner = _owner(fake_host, material)
    request = material.selection_context.dispatch_request
    recovery_result = {
        "ok": True,
        "released": True,
        "reason": "captured_paper_initial_authority_expired_released",
        "refresh_session_inventory": True,
        "opportunity_consumed": False,
        "risk_reserved": False,
        "outbox_created": False,
        "order_posted": False,
        "broker_order_post_calls": 0,
    }
    monkeypatch.setattr(
        owner,
        "_recover_pending_initial_generation_before_material",
        lambda _db, selected: recovery_result if selected is request else None,
    )
    monkeypatch.setattr(
        owner._admissions,
        "any_match",
        lambda _predicate: pytest.fail("admission/material path ran after release"),
    )

    assert owner(SimpleNamespace(), request) is recovery_result
    assert fake_host.tick_calls == []


def test_pending_initial_recovery_uses_exact_request_and_ends_reader_transaction(
    monkeypatch,
) -> None:
    material = _material()
    owner = _owner(_FakeHost("must_not_tick"), material)
    # Exercise the production-only seam without constructing a full provider;
    # the method under test never calls the factory itself.
    owner._production_material_factory = object()
    request = material.selection_context.dispatch_request
    row = SimpleNamespace(
        risk_snapshot_json={
            "captured_paper_session_pending_owner": {"typed": True}
        }
    )

    class _Query:
        def populate_existing(self):
            return self

        def filter(self, *_criteria):
            return self

        def one_or_none(self):
            return row

    class _Db:
        def __init__(self):
            self.rollbacks = 0

        def query(self, _model):
            return _Query()

        def rollback(self):
            self.rollbacks += 1

    db = _Db()
    calls = []
    receipt = SimpleNamespace(
        disposition="expired_released",
        session_id=request.session_id,
        receipt_sha256=_digest("initial-recovery-receipt"),
    )

    def _recover(bind, **kwargs):
        calls.append((bind, kwargs))
        return receipt

    monkeypatch.setattr(
        host_module,
        "recover_captured_paper_initial_preowner",
        _recover,
    )

    result = owner._recover_pending_initial_generation_before_material(db, request)

    assert db.rollbacks == 1
    assert calls == [
        (
            engine,
            {
                "session_id": request.session_id,
                "expected_account_id": request.expected_account_id,
                "expected_runtime_generation": request.runtime_generation,
                "expected_code_build_sha256": request.code_build_sha256,
                "expected_config_sha256": request.config_sha256,
                "expected_capture_receipt_sha256": request.capture_receipt_sha256,
                "assert_service_fence_held": owner._assert_service_fence_held,
            },
        )
    ]
    assert result["reason"] == "captured_paper_initial_authority_expired_released"
    assert result["refresh_session_inventory"] is True
    assert result["broker_order_post_calls"] == 0


def test_runtime_writer_lock_waits_for_competing_dispatch_session_lock() -> None:
    """The post-material writer seam shares the transport's real PG identity."""

    request = _material().selection_context.dispatch_request
    identity = AdaptiveRiskAccountLockIdentity.for_scope(request.account_scope)
    acquired = threading.Event()
    release_worker = threading.Event()
    failures = []

    def writer() -> None:
        db = Session(bind=engine)
        try:
            host_module.IqfeedCapturedPaperRuntimeOwner._join_dispatch_linearization_after_material_read(
                db,
                request,
            )
            acquired.set()
            assert db.in_transaction()
            assert release_worker.wait(timeout=5.0)
            db.rollback()
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)
            acquired.set()
        finally:
            db.close()

    holder = engine.connect()
    action_locked = False
    adaptive_locked = False
    thread = None
    try:
        with holder.begin():
            holder.execute(
                text("SELECT pg_advisory_lock(:key)"),
                {"key": identity.action_advisory_key},
            )
            action_locked = True
            holder.execute(
                text(
                    "SELECT pg_advisory_lock("
                    ":namespace, hashtext(:account_scope))"
                ),
                {
                    "namespace": identity.adaptive_advisory_namespace,
                    "account_scope": identity.account_scope,
                },
            )
            adaptive_locked = True

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        assert acquired.wait(timeout=0.25) is False

        with holder.begin():
            assert holder.execute(
                text(
                    "SELECT pg_advisory_unlock("
                    ":namespace, hashtext(:account_scope))"
                ),
                {
                    "namespace": identity.adaptive_advisory_namespace,
                    "account_scope": identity.account_scope,
                },
            ).scalar_one() is True
            adaptive_locked = False
            assert holder.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": identity.action_advisory_key},
            ).scalar_one() is True
            action_locked = False

        assert acquired.wait(timeout=5.0) is True
        assert failures == []
    finally:
        release_worker.set()
        if thread is not None:
            thread.join(timeout=5.0)
        if holder.in_transaction():
            holder.rollback()
        with holder.begin():
            if adaptive_locked:
                holder.execute(
                    text(
                        "SELECT pg_advisory_unlock("
                        ":namespace, hashtext(:account_scope))"
                    ),
                    {
                        "namespace": identity.adaptive_advisory_namespace,
                        "account_scope": identity.account_scope,
                    },
                )
            if action_locked:
                holder.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": identity.action_advisory_key},
                )
        holder.close()
    assert thread is not None and not thread.is_alive()


def test_runtime_owner_returns_exact_request_then_single_consumes_admission(
    monkeypatch,
) -> None:
    material = _material()
    fake_host = _FakeHost("typed")
    owner = _owner(fake_host, material)
    calls = []

    committed = object.__new__(host_module.CommittedCapturedPaperAdmission)
    object.__setattr__(
        committed,
        "post_commit_request",
        material.selection_context.draft,
    )

    def commit(
        bind,
        *,
        inputs,
        phase_one_material_sha256,
        executed_read_inventory,
        executed_captured_reads,
        financial_breaker_receipt,
        financial_breaker_verification_at,
        final_executed_read_provider,
    ):
        calls.append(
            (
                bind,
                inputs,
                phase_one_material_sha256,
                executed_read_inventory,
                executed_captured_reads,
                financial_breaker_receipt,
                financial_breaker_verification_at,
                final_executed_read_provider,
            )
        )
        return committed

    monkeypatch.setattr(host_module, "commit_captured_paper_admission", commit)
    monkeypatch.setattr(
        host_module,
        "read_committed_captured_paper_admission",
        lambda *_args, **_kwargs: None,
    )
    assert owner.stage_decision(material) == material.material_sha256
    runtime_db = _RuntimeDb()
    result = owner(runtime_db, material.selection_context.dispatch_request)

    assert type(result) is CapturedPaperPostCommitRequest
    assert runtime_db.transaction_active is True
    assert len(runtime_db.lock_sql) == 2
    assert result is material.selection_context.draft
    assert len(runtime_db.owner_activation_calls) == 1
    assert runtime_db.call_order == ["owner_activation", "fsm_tick"]
    assert owner.post_commit(result) is committed
    assert calls == [
        (
            engine,
            material.admission_inputs,
            material.material_sha256,
            material.predecision_executed_read_inventory,
            material.predecision_captured_reads,
            _pre_reservation_authority(
                material.admission_inputs,
                now=material.selection_context.draft.intent.decision_at
                + timedelta(milliseconds=1),
            )["financial_breaker_receipt"],
            material.selection_context.draft.intent.decision_at
            + timedelta(milliseconds=1),
            None,
        )
    ]
    assert fake_host.post_commit_scopes == 1
    assert owner.health()["retained_sqlalchemy_sessions"] == 0
    with pytest.raises(CaptureContractError, match="material unavailable"):
        owner.post_commit(result)


def test_runtime_owner_lost_service_fence_runs_zero_fsm_ticks() -> None:
    material = _material()
    fake_host = _FakeHost("typed")

    def lost_fence() -> None:
        raise RuntimeError("service fence lost")

    owner = _owner(
        fake_host,
        material,
        assert_service_fence_held=lost_fence,
    )
    owner.stage_decision(material)
    runtime_db = _RuntimeDb()

    with pytest.raises(RuntimeError, match="service fence lost"):
        owner(runtime_db, material.selection_context.dispatch_request)

    assert fake_host.tick_calls == []
    assert runtime_db.tick_completed is False
    assert runtime_db.owner_activation_calls == []
    assert runtime_db.call_order == []


def test_post_commit_failure_retains_exact_material_for_worker_retry(
    monkeypatch,
) -> None:
    material = _material()
    fake_host = _FakeHost("typed")
    owner = _owner(fake_host, material)
    committed = object.__new__(host_module.CommittedCapturedPaperAdmission)
    object.__setattr__(
        committed,
        "post_commit_request",
        material.selection_context.draft,
    )
    attempts = []

    def commit(
        _bind,
        *,
        inputs,
        phase_one_material_sha256,
        executed_read_inventory,
        executed_captured_reads,
        financial_breaker_receipt,
        financial_breaker_verification_at,
        final_executed_read_provider,
    ):
        assert phase_one_material_sha256 == material.material_sha256
        assert (
            executed_read_inventory
            == material.predecision_executed_read_inventory
        )
        assert executed_captured_reads == material.predecision_captured_reads
        financial_breaker_receipt.verify_for_request(
            material.selection_context.draft,
            phase="pre_reservation",
            now=financial_breaker_verification_at,
            require_allowed=True,
        )
        assert final_executed_read_provider is None
        attempts.append(inputs)
        if len(attempts) == 1:
            raise RuntimeError("transient admission failure")
        return committed

    monkeypatch.setattr(host_module, "commit_captured_paper_admission", commit)
    monkeypatch.setattr(
        host_module,
        "read_committed_captured_paper_admission",
        lambda *_args, **_kwargs: None,
    )
    owner.stage_decision(material)
    request = owner(_RuntimeDb(), material.selection_context.dispatch_request)

    with pytest.raises(RuntimeError, match="transient admission failure"):
        owner.post_commit(request)
    assert owner.health()["post_commit_handoffs"] == {
        "pending": 1,
        "in_flight": 0,
        "max_entries": 2,
        "ttl_seconds": 5.0,
    }
    deferred = owner(_RuntimeDb(), material.selection_context.dispatch_request)
    assert deferred["reason"] == "captured_paper_post_commit_retry_pending"
    assert deferred["opportunity_consumed"] is False
    assert deferred["risk_reserved"] is False
    assert deferred["order_posted"] is False

    retried = owner.retry_pending_post_commits(limit=1)
    assert dict(retried) == {
        "attempted": 1,
        "completed": 1,
        "failed": 0,
        "failure_reasons": (),
        "remaining": 0,
    }
    assert attempts == [material.admission_inputs, material.admission_inputs]


def test_lost_commit_ack_is_resolved_by_exact_durable_readback(
    monkeypatch,
) -> None:
    material = _material()
    fake_host = _FakeHost("typed")
    owner = _owner(fake_host, material)
    committed = object.__new__(host_module.CommittedCapturedPaperAdmission)
    object.__setattr__(
        committed,
        "post_commit_request",
        material.selection_context.draft,
    )
    reads = iter((None, committed))

    monkeypatch.setattr(
        host_module,
        "read_committed_captured_paper_admission",
        lambda *_args, **_kwargs: next(reads),
    )

    def ack_lost(_bind, *, inputs):
        assert inputs is material.admission_inputs
        raise ConnectionError("commit acknowledgement lost")

    monkeypatch.setattr(
        host_module,
        "commit_captured_paper_admission",
        ack_lost,
    )
    owner.stage_decision(material)
    request = owner(_RuntimeDb(), material.selection_context.dispatch_request)

    assert owner.post_commit(request) is committed
    assert owner.health()["post_commit_handoffs"]["pending"] == 0


def test_deferred_mapping_never_creates_a_post_commit_handoff() -> None:
    material = _material()
    fake_host = _FakeHost("decision_local_coverage_unavailable")
    owner = _owner(fake_host, material)
    owner.stage_decision(material)

    runtime_db = _RuntimeDb()
    result = owner(runtime_db, material.selection_context.dispatch_request)

    assert dict(result) == {
        "ok": True,
        "deferred": True,
        "reason": "decision_local_coverage_unavailable",
    }
    assert len(runtime_db.owner_activation_calls) == 1
    assert runtime_db.call_order == ["owner_activation", "fsm_tick"]
    with pytest.raises(CaptureContractError, match="material unavailable"):
        owner.post_commit(material.selection_context.draft)


def test_one_shot_store_is_ttl_and_capacity_bounded() -> None:
    now = [100.0]
    store = host_module._BoundedOneShotStore[str](
        max_entries=1,
        ttl_seconds=2.0,
        monotonic_clock=lambda: now[0],
    )
    key_a = "a" * 64
    key_b = "b" * 64
    store.stage(key_a, "one")
    with pytest.raises(CaptureContractError, match="capacity unavailable"):
        store.stage(key_b, "two")
    now[0] = 102.1
    with pytest.raises(CaptureContractError, match="expired"):
        store.consume(key_a)
    store.stage(key_b, "two")
    assert store.consume(key_b) == "two"
    with pytest.raises(CaptureContractError, match="material unavailable"):
        store.consume(key_b)


def test_one_shot_store_lease_requires_exact_ack_or_release() -> None:
    store = host_module._BoundedOneShotStore[str](
        max_entries=1,
        ttl_seconds=2.0,
        monotonic_clock=lambda: 100.0,
    )
    key = "a" * 64
    store.stage(key, "one")
    token, value = store.lease(key)
    assert value == "one"
    assert store.health()["in_flight"] == 1
    with pytest.raises(CaptureContractError, match="already in flight"):
        store.lease(key)
    with pytest.raises(CaptureContractError, match="lease mismatch"):
        store.release(key, str("b" * 36))
    store.release(key, token)
    retry_token, retry_value = store.lease(key)
    assert retry_value == "one"
    assert store.ack(key, retry_token) == "one"
    assert store.health()["pending"] == 0


@pytest.mark.parametrize(
    "role",
    ("primary", "repeg", "anticipation", "pyramid", "micro", "pullback", "flag"),
)
def test_captured_runtime_blocks_every_legacy_exposure_increase_before_place(role):
    material = _material()
    calls = []
    sess = SimpleNamespace(
        id=material.selection_context.dispatch_request.session_id,
        symbol=material.selection_context.dispatch_request.symbol,
        execution_family="alpaca_spot",
        risk_snapshot_json={},
    )
    with require_captured_paper_selection(
        material.selection_context.dispatch_request
    ):
        result = live_runner._governed_place(
            object(),
            lambda **kwargs: calls.append(kwargs),
            sess=sess,
            alpaca_order_role=role,
            product_id=sess.symbol,
            side="buy",
            base_size="100",
            limit_price="3.00",
            client_order_id=f"captured-{role}",
            time_in_force="day",
            extended_hours=False,
            position_intent="buy_to_open",
        )

    assert calls == []
    assert result["coverage_status"] == "COVERAGE_UNAVAILABLE"
    assert result["pre_place_blocked"] is True
    assert result["opportunity_consumed"] is False
    assert result["risk_reserved"] is False
    assert result["order_posted"] is False


def test_captured_runtime_fence_does_not_block_exposure_decreasing_exit(
    monkeypatch,
) -> None:
    material = _material()
    calls = []
    sess = SimpleNamespace(
        id=material.selection_context.dispatch_request.session_id,
        symbol=material.selection_context.dispatch_request.symbol,
        execution_family="alpaca_spot",
        risk_snapshot_json={},
    )
    rail = SimpleNamespace(acquired=True, waited_s=0.0, refill_rps=10.0)
    from app.services.trading.momentum_neural import rail_governor

    monkeypatch.setattr(rail_governor, "acquire_rail", lambda *_a, **_k: rail)
    monkeypatch.setattr(rail_governor, "note_rail_outcome", lambda *_a, **_k: None)
    # This regression isolates the captured-runtime legacy-entry fence.  Account
    # identity/quarantine behavior is independently covered by the Alpaca safety
    # suite and must not mask whether the fence itself permits an exit.
    monkeypatch.setattr(
        live_runner,
        "_alpaca_execution_quarantine_reason",
        lambda *_args: None,
    )

    with require_captured_paper_selection(
        material.selection_context.dispatch_request
    ):
        result = live_runner._governed_place(
            object(),
            lambda **kwargs: calls.append(kwargs) or {"ok": True},
            sess=sess,
            product_id=sess.symbol,
            side="sell",
            base_size="100",
            client_order_id="captured-exit",
            position_intent="sell_to_close",
        )

    assert result["ok"] is True
    assert calls == [
        {
            "product_id": sess.symbol,
            "side": "sell",
            "base_size": "100",
            "client_order_id": "captured-exit",
            "position_intent": "sell_to_close",
        }
    ]
