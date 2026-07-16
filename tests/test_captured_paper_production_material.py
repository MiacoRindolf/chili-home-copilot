from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from app.db import engine
from app.services.trading.momentum_neural.adaptive_risk_account_lock import (
    AdaptiveRiskAccountLockIdentity,
)
from app.services.trading.momentum_neural.adaptive_risk_policy import (
    AdaptiveRiskPolicy,
)
from app.services.trading.momentum_neural.captured_adaptive_risk_source import (
    CapturedAdaptiveRiskDecisionIdentity,
    CapturedAdaptiveRiskEconomicInputs,
    CapturedAdaptiveRiskEvidenceSet,
    CapturedAdaptiveRiskFactProvenance,
    CapturedAdaptiveRiskPolicySpec,
    captured_adaptive_risk_fact_payloads,
)
from app.services.trading.momentum_neural.captured_paper_admission import (
    CapturedPaperOperationalPolicy,
)
from app.services.trading.momentum_neural.captured_paper_dispatcher import (
    CapturedPaperDispatchRequest,
)
from app.services.trading.momentum_neural.captured_paper_production_material import (
    CapturedPaperBoundInputScope,
    CapturedPaperDurableCandidateSnapshot,
    CapturedPaperDurableObservationSnapshot,
    CapturedPaperObservationCapture,
    CapturedPaperProductionCapture,
    CapturedPaperProductionMaterialFactory,
    CapturedPaperProductionMaterialUnavailable,
    PreparedCapturedPaperObservation,
)
from app.services.trading.momentum_neural.captured_paper_selection import (
    CapturedPaperObservationContext,
    captured_paper_candidate_generation_sha256,
    captured_paper_observation_generation_sha256,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureContractError,
    CaptureStream,
    sha256_json,
)
from tests.test_captured_alpaca_paper_adapter import ACCOUNT_ID, _wrapper
from tests.test_captured_paper_admission import _combined_account_bbo_proof
from scripts import iqfeed_capture_host as host_module
import uuid


UTC = timezone.utc
NOW = datetime(2026, 7, 15, 19, 0, tzinfo=UTC)
RUNTIME_GENERATION = "9a2918b2-1cef-4e13-9ef8-91410cdd00fe"
ARM_TOKEN = "758bd3cc-9373-4b89-842d-819c3a2f7c02"


@pytest.fixture(autouse=True)
def _stub_runtime_owner_activation(monkeypatch):
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


def _policy() -> AdaptiveRiskPolicy:
    return AdaptiveRiskPolicy(
        policy_version="captured-paper-parity-v1",
        policy_source="sha-bound-test-manifest",
        risk_fraction_of_equity=0.005,
        daily_risk_fraction_of_equity=0.025,
        portfolio_risk_fraction_of_equity=0.02,
        cluster_risk_fraction_of_equity=0.015,
        symbol_risk_fraction_of_equity=0.01,
        daily_gap_reserve_fraction_of_equity=0.002,
        max_notional_fraction_of_equity=0.25,
        max_buying_power_fraction_for_notional=0.25,
        max_portfolio_gross_fraction_of_equity=1.0,
        quality_multiplier_floor=0.5,
        quality_multiplier_ceiling=1.5,
        volatility_reference_fraction=0.05,
        volatility_multiplier_floor=0.4,
        spread_reserve_multiple=1.0,
        per_share_gap_reserve_volatility_multiple=1.0,
        max_adv_participation=0.01,
        max_recent_volume_participation=0.05,
        max_executable_depth_participation=0.25,
        market_data_max_age_seconds=60.0,
        account_data_max_age_seconds=60.0,
        reservation_data_max_age_seconds=60.0,
        context_data_max_age_seconds=60.0,
    )


class _Db:
    def __init__(self) -> None:
        self.rolled_back = False
        self.transaction_active = False
        self.lock_sql = []
        self.transaction_events = []
        self.tick_completed = False
        self.owner_activation_calls = []
        self.call_order = []

    def rollback(self) -> None:
        self.rolled_back = True
        self.transaction_active = False
        self.lock_sql = []
        self.transaction_events.append("rollback")

    def begin(self):
        self.transaction_active = True
        self.transaction_events.append("begin")
        return object()

    def in_transaction(self):
        return self.transaction_active

    def execute(self, statement, parameters=None):
        self.lock_sql.append((str(statement), dict(parameters or {})))
        self.transaction_events.append("lock")
        return None


def _request(coordinator) -> CapturedPaperDispatchRequest:
    return CapturedPaperDispatchRequest(
        session_id=41,
        symbol="ACTU",
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
        expected_account_id=ACCOUNT_ID,
        code_build_sha256=coordinator.identity.code_build_sha256,
        config_sha256=coordinator.identity.config_sha256,
        capture_receipt_sha256=_digest("capture-host-binding"),
        runtime_generation=RUNTIME_GENERATION,
        first_dip_policy_mode="candidate",
    )


def _marker(request: CapturedPaperDispatchRequest) -> dict:
    return {
        "version": 1,
        "session_id": request.session_id,
        "arm_token": ARM_TOKEN,
        "expires_at_utc": (NOW + timedelta(minutes=5)).isoformat(),
        "alpaca_symbol_claim_token": f"arm-{ARM_TOKEN}",
        "alpaca_account_scope": request.account_scope,
        "alpaca_account_id": request.expected_account_id,
        "confirmed_at_utc": (NOW - timedelta(seconds=10)).isoformat(),
    }


def _candidate(request: CapturedPaperDispatchRequest):
    debug = {
        "pullback_low": 2.80,
        "pullback_high": 3.00,
        "nested_evidence": {"source_read_ids": ["scanner", "ohlcv"]},
    }
    marker = _marker(request)
    marker["nested_audit"] = {"tokens": ["arm", "claim"]}
    viability_payload = {
        "symbol": request.symbol,
        "variant_id": 7,
        "viability_score": 0.80,
        "execution_readiness_json": {"ross_profile": "smallcap"},
    }
    viability_payload_sha256 = sha256_json(viability_payload)
    execution_readiness_sha256 = sha256_json(
        viability_payload["execution_readiness_json"]
    )
    cid = "chili_ml_e_41_captured_a19fd31c"
    generation = captured_paper_candidate_generation_sha256(
        session_id=request.session_id,
        symbol=request.symbol,
        execution_family=request.execution_family,
        entry_place_count=1,
        client_order_id=cid,
        setup_family="momentum_pullback",
        structural_stop_price=2.80,
        trigger_reason="pullback_break",
        trigger_debug=debug,
        confirmed_arm_marker=marker,
        viability_updated_at=NOW - timedelta(milliseconds=50),
        viability_score=0.80,
        viability_payload_sha256=viability_payload_sha256,
        execution_readiness_sha256=execution_readiness_sha256,
    )
    return CapturedPaperDurableCandidateSnapshot(
        dispatch_provenance_sha256=request.provenance_sha256,
        session_id=request.session_id,
        symbol=request.symbol,
        execution_family=request.execution_family,
        state="live_pending_entry",
        correlation_id="corr-41",
        variant_id=7,
        session_updated_at=NOW - timedelta(milliseconds=50),
        viability_updated_at=NOW - timedelta(milliseconds=50),
        viability_score=0.80,
        viability_payload=viability_payload,
        viability_payload_sha256=viability_payload_sha256,
        execution_readiness_sha256=execution_readiness_sha256,
        entry_place_count=1,
        client_order_id=cid,
        setup_family="momentum_pullback",
        structural_stop_price=2.80,
        trigger_reason="pullback_break",
        trigger_debug=debug,
        confirmed_arm_marker=marker,
        session_snapshot_sha256=generation,
    )


def test_candidate_viability_payload_is_deeply_immutable_after_hashing() -> None:
    _wrapper_instance, _clock, _adapter, coordinator = _wrapper()
    candidate = _candidate(_request(coordinator))

    with pytest.raises(TypeError, match="immutable"):
        candidate.viability_payload["execution_readiness_json"][
            "ross_profile"
        ] = "fabricated"
    assert candidate.viability_payload_sha256 == sha256_json(
        candidate.viability_payload
    )

    before = candidate.candidate_sha256
    with pytest.raises(TypeError, match="immutable"):
        candidate.trigger_debug["nested_evidence"]["source_read_ids"].append(
            "fabricated"
        )
    with pytest.raises(TypeError, match="immutable"):
        candidate.confirmed_arm_marker["nested_audit"]["tokens"][0] = "changed"
    assert candidate.candidate_sha256 == before == sha256_json(candidate.to_payload())


def _economics() -> CapturedAdaptiveRiskEconomicInputs:
    return CapturedAdaptiveRiskEconomicInputs(
        structural_stop=2.80,
        entry_slippage_bps=5.0,
        exit_slippage_bps=5.0,
        fees_per_share_usd=0.005,
        setup_quality=0.80,
        realized_volatility_fraction=0.05,
        average_daily_volume_shares=5_000_000.0,
        recent_volume_shares=500_000.0,
        executable_depth_shares=900.0,
        candidate_buying_power_impact_per_share_usd=3.00,
    )


def _factory_fixture():
    wrapper, clock, _adapter, coordinator = _wrapper(
        account_max_age_seconds=60.0
    )
    clock.now = NOW
    settings_projection_sha256 = _digest("settings-projection")
    resource = {"binding": "fixture"}
    run_config = {"symbol": "ACTU", "mode": "captured_paper"}
    roster = {"iqfeed": {"generation": 1}}
    capture_config = {
        "schema_version": "chili.captured-paper-capture-runtime-config.v1",
        "captured_paper_settings_projection_sha256": (
            settings_projection_sha256
        ),
        "capture_certification_symbol": "ACTU",
        "capture_resource_binding": resource,
        "capture_resource_binding_sha256": sha256_json(resource),
        "live_capture_run_configuration": run_config,
        "live_capture_run_configuration_sha256": sha256_json(run_config),
        "capture_store_root": "D:\\captured-paper-test",
        "iqfeed_external_producer_generation_roster": roster,
        "iqfeed_external_producer_generation_roster_sha256": sha256_json(
            roster
        ),
    }
    coordinator.identity = replace(
        coordinator.identity,
        config_sha256=sha256_json(capture_config),
    )
    request = _request(coordinator)
    candidate = _candidate(request)
    db = _Db()
    captured_objects = []

    def candidate_reader(observed_db, observed_request):
        assert observed_db is db
        assert observed_request is request
        return candidate

    @contextmanager
    def capture_provider(*, request, candidate, coordinator):
        assert db.rolled_back is True
        with wrapper.decision_scope(candidate.client_order_id):
            wrapper.get_execution_bbo(request.symbol, max_age_seconds=30.0)
            account_result, bbo_result = wrapper.consume_current_captured_reads(
                symbol=request.symbol
            )
            reads = (account_result, bbo_result)
            proof = _combined_account_bbo_proof(
                coordinator,
                decision_id=candidate.client_order_id,
                expires_at=clock.now + timedelta(seconds=60),
            )
            coordinator.attest_predecision_inputs = lambda **kwargs: proof
            economics = _economics()
            identity = CapturedAdaptiveRiskDecisionIdentity(
                execution_surface="alpaca_paper",
                run_id=proof.run_id,
                generation=proof.generation,
                decision_id=candidate.client_order_id,
                symbol=request.symbol,
                setup_family=candidate.setup_family,
                correlation_cluster="equity:momentum-a",
                account_scope=request.account_scope,
                decision_at=clock.now,
            )
            payloads = captured_adaptive_risk_fact_payloads(identity, economics)
            bbo_source = bbo_result.source_events[0]
            assert bbo_result.receipt is not None

            def fact(name: str) -> CapturedAdaptiveRiskFactProvenance:
                return CapturedAdaptiveRiskFactProvenance.create(
                    payload=payloads[name],
                    source=f"captured-derived:{name}",
                    observed_at=bbo_source.clocks.provider_event_at,
                    available_at=bbo_source.clocks.available_at,
                    provider_generation="captured-derived:1",
                    source_read_ids=(bbo_result.receipt.read_id,),
                )

            evidence = CapturedAdaptiveRiskEvidenceSet(
                structural_stop=fact("structural_stop"),
                setup_quality=fact("setup_quality"),
                volatility=fact("volatility"),
                liquidity=fact("liquidity"),
                correlation=fact("correlation"),
                candidate_buying_power_estimate=fact(
                    "candidate_buying_power_estimate"
                ),
            )
            scope = CapturedPaperBoundInputScope(
                installer=lambda: nullcontext(),
                required_read_ids=proof.required_read_ids,
                scope_sha256=sha256_json(
                    {"read_ids": list(proof.required_read_ids)}
                ),
            )
            captured = CapturedPaperProductionCapture(
                decision_at=clock.now,
                adapter=wrapper,
                captured_reads=reads,
                dependency_profile=proof.dependency_profile,
                active_input_attestation=proof,
                economics=economics,
                fact_evidence=evidence,
                correlation_cluster="equity:momentum-a",
                setup_read_id=bbo_result.receipt.read_id,
                bound_input_scope=scope,
            )
            captured_objects.append(captured)
            yield captured

    factory = CapturedPaperProductionMaterialFactory(
        candidate_reader=candidate_reader,
        capture_provider=capture_provider,
        observation_capture_provider=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("candidate test invoked observation provider")
        ),
        coordinator_for=lambda symbol: coordinator,
        capture_config_for=lambda symbol: capture_config,
        settings_projection_sha256=settings_projection_sha256,
        policy_spec=CapturedAdaptiveRiskPolicySpec(
            policy=_policy(),
            code_build_sha256=request.code_build_sha256,
            effective_config_sha256=settings_projection_sha256,
            feature_flags_sha256=coordinator.identity.feature_flags_sha256,
        ),
        operational_policy=CapturedPaperOperationalPolicy(
            action_claim_lease_seconds=30,
            outbox_max_attempts=3,
            outbox_max_reconciliation_attempts=3,
            reconciliation_retry_delay_seconds=5,
            reconciliation_health_escalation_delay_seconds=30,
            time_in_force="day",
            extended_hours=True,
            config_provenance_sha256=settings_projection_sha256,
        ),
    )
    return factory, db, request, candidate, captured_objects


def test_factory_rolls_back_candidate_read_before_capture_and_binds_exact_reads():
    factory, db, request, candidate, captured_objects = _factory_fixture()

    with factory.decision_scope(db, request) as prepared:
        assert db.rolled_back is True
        assert prepared.candidate_sha256 == candidate.candidate_sha256
        assert prepared.selection_context.candidate_generation_sha256 == (
            candidate.session_snapshot_sha256
        )
        assert prepared.selection_context.draft.intent.client_order_id == (
            candidate.client_order_id
        )
        assert prepared.admission_inputs.active_input_attestation.decision_id == (
            candidate.client_order_id
        )
        assert prepared.adapter_factory() is captured_objects[0].adapter
        with prepared.bound_input_scope.install(
            prepared.admission_inputs.active_input_attestation
        ):
            pass

    with pytest.raises(CapturedPaperProductionMaterialUnavailable, match="already_used"):
        with prepared.bound_input_scope.install(
            prepared.admission_inputs.active_input_attestation
        ):
            pass


def test_factory_rejects_candidate_drift_or_missing_exact_capture_before_order_path():
    factory, db, request, candidate, _captured = _factory_fixture()
    wrong = replace(
        candidate,
        dispatch_provenance_sha256=_digest("wrong-route"),
    )
    factory._candidate_reader = lambda *_args: wrong

    with pytest.raises(
        CapturedPaperProductionMaterialUnavailable,
        match="candidate_route_mismatch",
    ):
        with factory.decision_scope(db, request):
            pass


def test_factory_rejects_setup_quality_not_bound_to_durable_viability():
    factory, db, request, candidate, _captured = _factory_fixture()
    factory._candidate_reader = lambda *_args: replace(
        candidate,
        viability_score=0.70,
    )

    with pytest.raises(
        CapturedPaperProductionMaterialUnavailable,
        match="production_setup_quality_mismatch",
    ):
        with factory.decision_scope(db, request):
            pass


def test_production_factory_has_no_manual_stage_or_network_fallback_surface():
    factory, _db, _request, _candidate, _captured = _factory_fixture()

    assert not hasattr(factory, "stage_decision")
    assert not hasattr(factory, "network_fallback")
    assert not hasattr(factory, "provider_fallback")


def test_observation_bundle_fails_closed_when_required_captured_streams_are_missing():
    wrapper, clock, _adapter, coordinator = _wrapper(
        account_max_age_seconds=60.0
    )
    decision_id = "captured-paper-observe-41-0123456789abcdef01234567"
    with wrapper.decision_scope(decision_id):
        wrapper.get_execution_bbo("ACTU", max_age_seconds=30.0)
        account_result, bbo_result = wrapper.consume_current_captured_reads(
            symbol="ACTU"
        )
        proof = _combined_account_bbo_proof(
            coordinator,
            decision_id=decision_id,
            expires_at=clock.now + timedelta(seconds=60),
        )
        scope = CapturedPaperBoundInputScope(
            installer=lambda: nullcontext(),
            required_read_ids=proof.required_read_ids,
            scope_sha256=_digest("incomplete-observation-scope"),
        )
        assert account_result.receipt is not None
        assert bbo_result.receipt is not None
        with pytest.raises(
            CapturedPaperProductionMaterialUnavailable,
            match="observation_required_stream_coverage_unavailable",
        ):
            CapturedPaperObservationCapture(
                decision_at=clock.now,
                adapter=wrapper,
                captured_reads=(account_result, bbo_result),
                dependency_profile=proof.dependency_profile,
                active_input_attestation=proof,
                bound_input_scope=scope,
                observation_snapshot_read_id=account_result.receipt.read_id,
                admission_eligibility_read_id=bbo_result.receipt.read_id,
            )


def test_runtime_owner_builds_production_material_without_manual_stage(monkeypatch):
    factory, db, request, expected_candidate, _captured = _factory_fixture()
    phase_one_calls = []
    monkeypatch.setattr(
        host_module,
        "record_captured_paper_phase_one_handoff",
        lambda observed_db, **kwargs: phase_one_calls.append(
            (observed_db, kwargs)
        ),
    )

    class Host(host_module.IqfeedCaptureHost):
        def __init__(self):
            self.calls = []

        def tick_captured_alpaca_paper_session(
            self,
            observed_db,
            *,
            dispatch_request,
            decision_material,
            adapter_factory,
        ):
            assert observed_db.in_transaction()
            assert len(observed_db.lock_sql) == 2
            assert "pg_advisory_xact_lock" in observed_db.lock_sql[0][0]
            assert "pg_advisory_xact_lock" in observed_db.lock_sql[1][0]
            assert observed_db.transaction_events[-3:] == ["begin", "lock", "lock"]
            assert "rollback" in observed_db.transaction_events[:-3]
            assert observed_db.call_order == ["owner_activation"]
            proof = decision_material.admission_inputs.active_input_attestation
            with decision_material.bound_input_scope.install(proof):
                adapter = adapter_factory()
                assert adapter.bound_account_id == ACCOUNT_ID
            self.calls.append(
                (observed_db, dispatch_request, decision_material)
            )
            result = host_module.IqfeedCapturedPaperTickResult(
                decision_at=decision_material.selection_context.draft.intent.decision_at,
                fsm_result=decision_material.selection_context.draft,
                first_dip_final_capture_frontier=None,
                scanner_snapshot_read_ids=(),
                ohlcv_read_ids=(),
                microstructure_read_ids=(),
                captured_reads=decision_material.predecision_captured_reads,
                executed_read_inventory=(
                    decision_material.predecision_executed_read_inventory
                ),
            )
            observed_db.tick_completed = True
            observed_db.call_order.append("fsm_tick")
            return result

    host = Host()
    owner = host_module.IqfeedCapturedPaperRuntimeOwner(
        host=host,
        adapter_factory=lambda: (_ for _ in ()).throw(
            AssertionError("legacy adapter factory used")
        ),
        admission_bind=engine,
        expected_account_id=request.expected_account_id,
        code_build_sha256=request.code_build_sha256,
        config_sha256=request.config_sha256,
        capture_receipt_sha256=request.capture_receipt_sha256,
        runtime_generation=request.runtime_generation,
        first_dip_policy_mode=request.first_dip_policy_mode,
        decision_max_entries=2,
        decision_ttl_seconds=5.0,
        admission_max_entries=2,
        admission_ttl_seconds=5.0,
        settings_projection_sha256=factory._settings_projection_sha256,
        config_sha256_resolver=lambda symbol: request.config_sha256,
        production_material_factory=factory,
        assert_service_fence_held=lambda: None,
    )

    result = owner(db, request)

    assert result.intent.client_order_id == expected_candidate.client_order_id
    assert len(host.calls) == 1
    assert len(db.owner_activation_calls) == 1
    assert db.call_order == ["owner_activation", "fsm_tick"]
    assert phase_one_calls[0][0] is db
    assert phase_one_calls[0][1]["request"] is result
    assert phase_one_calls[0][1]["material_sha256"] == host.calls[0][2].material_sha256
    assert owner.health()["production_material_factory_installed"] is True
    assert owner.health()["manual_staging_enabled"] is False
    with pytest.raises(CaptureContractError, match="manual staging is disabled"):
        owner.stage_decision(host.calls[0][2])


def _prepared_observation(request, *, state: str):
    hashes = {
        "risk_snapshot_sha256": _digest(f"risk:{state}"),
        "viability_payload_sha256": _digest(f"viability:{state}"),
        "variant_payload_sha256": _digest(f"variant:{state}"),
        "confirmed_arm_marker_sha256": _digest(f"arm:{state}"),
    }
    updated = NOW - timedelta(seconds=1)
    generation = captured_paper_observation_generation_sha256(
        session_id=request.session_id,
        symbol=request.symbol,
        execution_family=request.execution_family,
        state=state,
        correlation_id="corr-41",
        variant_id=7,
        session_updated_at=updated,
        **hashes,
    )
    context = CapturedPaperObservationContext(
        dispatch_request=request,
        initial_state=state,
        correlation_id="corr-41",
        variant_id=7,
        session_updated_at=updated,
        decision_at=NOW,
        evidence_available_at=NOW - timedelta(milliseconds=10),
        evidence_expires_at=NOW + timedelta(seconds=30),
        observation_decision_id=(
            f"captured-paper-observe-{request.session_id}-{generation[:24]}"
        ),
        observation_generation_sha256=generation,
        **hashes,
    )
    read_id = str(uuid.uuid4())
    scope = CapturedPaperBoundInputScope(
        installer=lambda: nullcontext(),
        required_read_ids=(read_id,),
        scope_sha256=_digest(f"scope:{state}"),
    )
    return PreparedCapturedPaperObservation(
        observation_context=context,
        active_input_attestation=object(),
        adapter_factory=lambda: object(),
        bound_input_scope=scope,
        first_dip_detector_policy=None,
        first_dip_tape_read_id=None,
        observation_snapshot_sha256=_digest(f"snapshot:{state}"),
    )


@pytest.mark.parametrize("state", ["queued_live", "watching_live"])
def test_runtime_owner_routes_watcher_through_observation_without_admission(state):
    factory, db, request, _candidate, _captured = _factory_fixture()
    prepared = _prepared_observation(request, state=state)

    @contextmanager
    def observation_scope(observed_db, observed_request):
        assert observed_db is db
        assert observed_request is request
        observed_db.rollback()
        yield prepared

    def material_kind(observed_db, observed_request):
        assert observed_db is db
        assert observed_request is request
        observed_db.rollback()
        return "observation"

    factory.material_kind = material_kind
    factory.observation_scope = observation_scope

    class Host(host_module.IqfeedCaptureHost):
        def __init__(self):
            self.calls = 0

        def tick_captured_alpaca_paper_observation_session(
            self,
            observed_db,
            *,
            dispatch_request,
            prepared,
        ):
            assert observed_db is db
            assert observed_db.in_transaction()
            assert len(observed_db.lock_sql) == 2
            assert observed_db.transaction_events[-3:] == ["begin", "lock", "lock"]
            assert "rollback" in observed_db.transaction_events[:-3]
            assert observed_db.call_order == ["owner_activation"]
            assert dispatch_request is request
            assert not hasattr(prepared, "admission_inputs")
            assert not hasattr(prepared, "post_commit_request")
            assert prepared.first_dip_tape_read_id is None
            self.calls += 1
            result = host_module.IqfeedCapturedPaperTickResult(
                decision_at=prepared.observation_context.decision_at,
                fsm_result={
                    "ok": True,
                    "state": state,
                    "opportunity_consumed": False,
                    "risk_reserved": False,
                    "order_posted": False,
                    "broker_order_post_calls": 0,
                },
                first_dip_final_capture_frontier=None,
                scanner_snapshot_read_ids=(),
                ohlcv_read_ids=(),
                microstructure_read_ids=(),
            )
            observed_db.tick_completed = True
            observed_db.call_order.append("fsm_tick")
            return result

    host = Host()
    owner = host_module.IqfeedCapturedPaperRuntimeOwner(
        host=host,
        adapter_factory=lambda: (_ for _ in ()).throw(
            AssertionError("legacy adapter factory used")
        ),
        admission_bind=engine,
        expected_account_id=request.expected_account_id,
        code_build_sha256=request.code_build_sha256,
        config_sha256=request.config_sha256,
        capture_receipt_sha256=request.capture_receipt_sha256,
        runtime_generation=request.runtime_generation,
        first_dip_policy_mode=request.first_dip_policy_mode,
        decision_max_entries=2,
        decision_ttl_seconds=5.0,
        admission_max_entries=2,
        admission_ttl_seconds=5.0,
        settings_projection_sha256=factory._settings_projection_sha256,
        config_sha256_resolver=lambda symbol: request.config_sha256,
        production_material_factory=factory,
        assert_service_fence_held=lambda: None,
    )

    result = owner(db, request)

    assert host.calls == 1
    assert len(db.owner_activation_calls) == 1
    assert db.call_order == ["owner_activation", "fsm_tick"]
    assert result["opportunity_consumed"] is False
    assert result["risk_reserved"] is False
    assert result["order_posted"] is False
    assert result["broker_order_post_calls"] == 0
