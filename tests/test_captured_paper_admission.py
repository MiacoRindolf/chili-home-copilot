from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import inspect
import json
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, text

from app.db import engine
from app.services.trading.momentum_neural import captured_paper_admission as admission
from app.services.trading.momentum_neural import (
    captured_paper_phase_one_handoff as phase_one,
)
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    AlpacaPaperBrokerAccountFacts,
)
from app.services.trading.momentum_neural.captured_adaptive_risk_source import (
    CapturedAccountRiskReceipt,
    CapturedAdaptiveRiskDecisionIdentity,
    CapturedAdaptiveRiskEconomicInputs,
    CapturedAdaptiveRiskEvidenceSet,
    CapturedAdaptiveRiskFactProvenance,
    CapturedAdaptiveRiskPolicySpec,
    CapturedExactBbo,
    captured_adaptive_risk_fact_payloads,
)
from app.services.trading.momentum_neural.captured_paper_dispatcher import (
    CapturedPaperDispatchRequest,
)
from app.services.trading.momentum_neural.captured_paper_entry_intent import (
    CapturedPaperConfirmedArmGeneration,
    CapturedPaperEntryIntent,
    CapturedPaperOpportunityKey,
    CapturedPaperPostCommitRequest,
)
from app.services.trading.momentum_neural.captured_paper_financial_breaker import (
    CapturedPaperFinancialBreakerError,
    CapturedPaperFinancialBreakerReceipt,
    SqlAlchemyCapturedPaperFinancialBreakerIssuer,
)
from app.services.trading.momentum_neural.persistence import (
    ensure_momentum_strategy_variants,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    ActiveCaptureReadEvidence,
    CaptureEventRef,
    CaptureStream,
    FSMDependencyProfile,
    FSMStreamDependency,
    _issue_active_capture_input_attestation,
    sha256_json,
)
from app.services.trading.momentum_neural.live_replay_capture import (
    build_executed_capture_read_inventory,
)
from tests.test_captured_adaptive_risk_source import _policy as shared_policy
from tests.test_captured_alpaca_paper_adapter import (
    ACCOUNT_ID,
    _Clock,
    _wrapper,
)


ET = ZoneInfo("America/New_York")
SESSION_ID = 41
SYMBOL = "ACTU"
DECISION_ID = "chili_ml_ACTU_41_atomic_1"
RUNTIME_GENERATION = "f6ef5ba0-5b91-49bf-a2f5-e71e8e270eb3"
ARM_TOKEN = "d2b8f7d8-6ad5-4cd0-a94e-8a9ca146d3ab"
BINDER_ID = "122158cc-18ae-4cef-bc52-f1c5b689b352"
INTENT_GENERATION = "39f55a65-e6f2-4ccc-bd02-f50dc9c27c69"
COMPLETION_GENERATION = "73dbcf92-94ea-436e-978c-b0e31ce7252d"
OTHER_ACCOUNT_ID = "7c143be2-d40a-4a5e-a8a8-d6fc19d2cd79"
PHASE_ONE_MATERIAL_SHA256 = "a" * 64


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _canonical_json(value: dict) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _combined_account_bbo_proof(coordinator, *, decision_id: str, expires_at):
    evidence: list[ActiveCaptureReadEvidence] = []
    dependencies: list[FSMStreamDependency] = []
    for result in coordinator.results:
        assert result.receipt is not None
        assert result.receipt_submission is not None
        assert result.receipt_submission.event is not None
        assert len(result.source_events) == 1
        receipt_event = result.receipt_submission.event
        source = result.source_events[0]
        evidence.append(
            ActiveCaptureReadEvidence(
                receipt=result.receipt,
                receipt_sha256=sha256_json(result.receipt.to_dict()),
                receipt_event_sha256=receipt_event.event_sha256,
                receipt_event_sequence=receipt_event.sequence,
                receipt_committed_available_at=(
                    receipt_event.clocks.available_at
                ),
                producer_id=coordinator._coordinator_producer_id,
                producer_generation=coordinator.identity.generation,
                source_event_refs=(CaptureEventRef.from_event(source),),
            )
        )
        if result.receipt.stream is CaptureStream.ACCOUNT_RISK_SNAPSHOT:
            dependencies.append(
                FSMStreamDependency(
                    stream=result.receipt.stream,
                    exact_provider_event_at_required=False,
                    market_reference_at_required=True,
                    max_source_age_seconds=60.0,
                    coverage_start_at=source.clocks.received_at,
                )
            )
        else:
            assert result.receipt.stream is CaptureStream.ALPACA_NBBO_QUOTE
            dependencies.append(
                FSMStreamDependency(
                    stream=result.receipt.stream,
                    exact_provider_event_at_required=True,
                    market_reference_at_required=False,
                    max_source_age_seconds=60.0,
                    coverage_start_at=source.clocks.provider_event_at,
                )
            )
    assert {row.receipt.stream for row in evidence} == {
        CaptureStream.ACCOUNT_RISK_SNAPSHOT,
        CaptureStream.ALPACA_NBBO_QUOTE,
    }
    committed_at = max(row.receipt_committed_available_at for row in evidence)
    profile = FSMDependencyProfile(
        required_streams=frozenset(row.receipt.stream for row in evidence),
        required_read_ids=tuple(row.receipt.read_id for row in evidence),
        stream_dependencies=tuple(dependencies),
    )
    receipt_events = tuple(
        sorted(row.receipt_event_sha256 for row in evidence)
    )
    return _issue_active_capture_input_attestation(
        run_id=coordinator.identity.run_id,
        generation=coordinator.identity.generation,
        decision_id=decision_id,
        input_prefix_sequence=max(
            row.receipt_event_sequence for row in evidence
        ),
        input_prefix_root_sha256=sha256_json(
            {"receipt_event_sha256s": receipt_events}
        ),
        attested_available_at=committed_at,
        expires_at=expires_at,
        dependency_profile=profile,
        identity_sha256=coordinator.identity.identity_sha256,
        account_identity_sha256=(
            coordinator.identity.account_identity_sha256
        ),
        code_build_sha256=coordinator.identity.code_build_sha256,
        config_sha256=coordinator.identity.config_sha256,
        feature_flags_sha256=coordinator.identity.feature_flags_sha256,
        resource_binding_sha256=_digest("atomic-paper-resource-binding"),
        producer_generations={
            coordinator._coordinator_producer_id: (
                coordinator.identity.generation
            )
        },
        required_read_ids=tuple(row.receipt.read_id for row in evidence),
        read_evidence=tuple(evidence),
        continuity_evidence=(),
    )


def _captured_material(
    *,
    now: datetime,
    decision_id: str = DECISION_ID,
    candidate_buying_power_impact_per_share_usd: float = 3.00,
):
    clock = _Clock()
    clock.now = now
    wrapper, clock, _adapter, coordinator = _wrapper(
        clock=clock,
        account_max_age_seconds=60.0,
    )
    with wrapper.decision_scope(decision_id):
        wrapper.get_execution_bbo(SYMBOL, max_age_seconds=30.0)
        proof = _combined_account_bbo_proof(
            coordinator,
            decision_id=decision_id,
            expires_at=clock.now + timedelta(seconds=60),
        )
        account_authority = wrapper.issue_account_authority(proof)

    account_result = next(
        row
        for row in coordinator.results
        if row.receipt.stream is CaptureStream.ACCOUNT_RISK_SNAPSHOT
    )
    bbo_result = next(
        row
        for row in coordinator.results
        if row.receipt.stream is CaptureStream.ALPACA_NBBO_QUOTE
    )
    account_source = account_result.source_events[0]
    bbo_source = bbo_result.source_events[0]
    exact_bbo = CapturedExactBbo(
        read_id=bbo_result.receipt.read_id,
        source_event_sha256=bbo_source.event_sha256,
        payload_json=_canonical_json(dict(bbo_source.payload)),
    )
    account_receipt = CapturedAccountRiskReceipt(
        read_id=account_result.receipt.read_id,
        source_event_sha256=account_source.event_sha256,
        payload_json=_canonical_json(dict(account_source.payload)),
    )
    economics = CapturedAdaptiveRiskEconomicInputs(
        structural_stop=2.80,
        entry_slippage_bps=5.0,
        exit_slippage_bps=5.0,
        fees_per_share_usd=0.005,
        setup_quality=0.80,
        realized_volatility_fraction=0.05,
        average_daily_volume_shares=5_000_000.0,
        recent_volume_shares=500_000.0,
        executable_depth_shares=100_000.0,
        candidate_buying_power_impact_per_share_usd=(
            candidate_buying_power_impact_per_share_usd
        ),
    )
    return {
        "clock": clock,
        "proof": proof,
        "account_authority": account_authority,
        "broker_account_facts": (
            AlpacaPaperBrokerAccountFacts.from_capture_authority(
                account_authority
            )
        ),
        "exact_bbo": exact_bbo,
        "account_receipt": account_receipt,
        "economics": economics,
        "bbo_result": bbo_result,
        "bbo_source": bbo_source,
        "captured_reads": tuple(coordinator.results),
        "executed_read_inventory": build_executed_capture_read_inventory(
            identity=coordinator.identity,
            decision_id=decision_id,
            captured_reads=tuple(coordinator.results),
        ),
    }


def _inputs(
    *,
    now: datetime,
    setup_family: str = "primary_entry",
    first_dip_policy_mode: str = "candidate",
    expected_account_id: str = ACCOUNT_ID,
    candidate_buying_power_impact_per_share_usd: float = 3.00,
    decision_id: str = DECISION_ID,
    binder_id: str = BINDER_ID,
    intent_generation: str = INTENT_GENERATION,
    completion_generation: str = COMPLETION_GENERATION,
):
    captured = _captured_material(
        now=now,
        candidate_buying_power_impact_per_share_usd=(
            candidate_buying_power_impact_per_share_usd
        ),
        decision_id=decision_id,
    )
    proof = captured["proof"]
    dispatch = CapturedPaperDispatchRequest(
        session_id=SESSION_ID,
        symbol=SYMBOL,
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
        expected_account_id=expected_account_id,
        code_build_sha256=proof.code_build_sha256,
        config_sha256=proof.config_sha256,
        capture_receipt_sha256=_digest("runtime-capture-binding-receipt"),
        runtime_generation=RUNTIME_GENERATION,
        first_dip_policy_mode=first_dip_policy_mode,
    )
    route = dispatch.route_token
    arm = CapturedPaperConfirmedArmGeneration(
        session_id=route.session_id,
        arm_token=ARM_TOKEN,
        expires_at=now + timedelta(seconds=60),
        symbol_claim_token=f"arm-{ARM_TOKEN}",
        account_scope=route.account_scope,
        expected_account_id=route.expected_account_id,
        confirmed_at=now - timedelta(seconds=5),
    )
    opportunity = (
        CapturedPaperOpportunityKey(
            account_scope=route.account_scope,
            symbol=route.symbol,
            trading_date=now.astimezone(ET).date(),
            setup_family=setup_family,
        )
        if setup_family == "first_dip_reclaim"
        else None
    )
    policy = replace(
        shared_policy(),
        market_data_max_age_seconds=60.0,
        account_data_max_age_seconds=60.0,
        context_data_max_age_seconds=60.0,
    )
    intent = CapturedPaperEntryIntent(
        route_token=route,
        confirmed_arm_generation=arm,
        symbol_claim_token=arm.symbol_claim_token,
        binder_id=binder_id,
        opportunity_key=opportunity,
        intent_generation=intent_generation,
        decision_id=decision_id,
        client_order_id=decision_id,
        setup_family=setup_family,
        decision_at=captured["clock"].now,
        structural_stop_price="2.80",
        entry_limit_ceiling_price="3.00",
        account_receipt_sha256=(
            captured["account_authority"].account_read_receipt_sha256
        ),
        bbo_receipt_sha256=next(
            row.receipt_sha256
            for row in proof.read_evidence
            if row.receipt.read_id == captured["exact_bbo"].read_id
        ),
        setup_evidence_sha256=_digest("captured-setup-evidence"),
        policy_sha256=policy.policy_sha256,
        feature_flags_sha256=proof.feature_flags_sha256,
    )
    post_commit_request = CapturedPaperPostCommitRequest(
        intent=intent,
        completion_generation=completion_generation,
    )
    pre_identity = CapturedAdaptiveRiskDecisionIdentity(
        execution_surface="alpaca_paper",
        run_id=proof.run_id,
        generation=proof.generation,
        decision_id=intent.decision_id,
        symbol=route.symbol,
        setup_family=setup_family,
        correlation_cluster="equity:momentum-a",
        account_scope=route.account_scope,
        decision_at=intent.decision_at,
    )
    payloads = captured_adaptive_risk_fact_payloads(
        pre_identity,
        captured["economics"],
    )
    bbo_source = captured["bbo_source"]
    bbo_read_id = captured["exact_bbo"].read_id

    def fact(name: str) -> CapturedAdaptiveRiskFactProvenance:
        return CapturedAdaptiveRiskFactProvenance.create(
            payload=payloads[name],
            source=f"captured-derived:{name}",
            observed_at=bbo_source.clocks.provider_event_at,
            available_at=bbo_source.clocks.available_at,
            provider_generation="captured-derived:1",
            source_read_ids=(bbo_read_id,),
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
    return admission.CapturedPaperAdmissionInputs(
        dispatch_request=dispatch,
        post_commit_request=post_commit_request,
        broker_account_facts=captured["broker_account_facts"],
        policy_spec=CapturedAdaptiveRiskPolicySpec(
            policy=policy,
            code_build_sha256=proof.code_build_sha256,
            effective_config_sha256=proof.config_sha256,
            feature_flags_sha256=proof.feature_flags_sha256,
        ),
        active_input_attestation=proof,
        predecision_captured_reads=captured["captured_reads"],
        executed_read_inventory=captured["executed_read_inventory"],
        exact_bbo=captured["exact_bbo"],
        account_receipt=captured["account_receipt"],
        economics=captured["economics"],
        fact_evidence=evidence,
        correlation_cluster="equity:momentum-a",
        operational_policy=admission.CapturedPaperOperationalPolicy(
            action_claim_lease_seconds=30,
            outbox_max_attempts=3,
            outbox_max_reconciliation_attempts=2,
            reconciliation_retry_delay_seconds=5,
            reconciliation_health_escalation_delay_seconds=60,
            time_in_force="day",
            extended_hours=True,
            config_provenance_sha256=proof.config_sha256,
        ),
    )


def _seed_session(db, request: CapturedPaperPostCommitRequest, *, account_id=None):
    intent = request.intent
    route = intent.route_token
    arm = intent.confirmed_arm_generation
    ensure_momentum_strategy_variants(db)
    db.flush()
    variant_id = db.execute(
        text("SELECT id FROM momentum_strategy_variants ORDER BY id LIMIT 1")
    ).scalar_one()
    risk_snapshot = {
        "alpaca_account_scope": route.account_scope,
        "alpaca_account_id": account_id or route.expected_account_id,
        "alpaca_symbol_claim_token": arm.symbol_claim_token,
        "confirmed_arm_generation": {
            "version": 1,
            "session_id": route.session_id,
            "arm_token": arm.arm_token,
            "expires_at_utc": arm.expires_at.isoformat(),
            "alpaca_symbol_claim_token": arm.symbol_claim_token,
            "alpaca_account_scope": route.account_scope,
            "alpaca_account_id": route.expected_account_id,
            "confirmed_at_utc": arm.confirmed_at.isoformat(),
        },
        "momentum_live_execution": {"position": None},
    }
    db.execute(
        text(
            """
            INSERT INTO trading_automation_sessions (
                id, venue, execution_family, mode, symbol, variant_id,
                state, risk_snapshot_json, allocation_decision_json,
                started_at, created_at, updated_at
            ) VALUES (
                :session_id, 'alpaca', 'alpaca_spot', 'live', :symbol,
                :variant_id, 'watching_live', CAST(:snapshot AS jsonb),
                '{}'::jsonb, clock_timestamp(), clock_timestamp(),
                clock_timestamp()
            )
            """
        ),
        {
            "session_id": route.session_id,
            "symbol": route.symbol,
            "variant_id": variant_id,
            "snapshot": _canonical_json(risk_snapshot),
        },
    )
    db.commit()


def _table_count(db, table: str) -> int:
    allowed = {
        "adaptive_risk_decision_packets",
        "adaptive_risk_opportunity_claims",
        "adaptive_risk_reservations",
        "alpaca_paper_account_settlement_heads",
        "broker_symbol_action_claims",
        "captured_paper_post_commit_outbox",
    }
    assert table in allowed
    return int(db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def _record_phase_one(db, inputs, *, material_sha256=PHASE_ONE_MATERIAL_SHA256):
    receipt = phase_one.record_captured_paper_phase_one_handoff(
        db,
        request=inputs.post_commit_request,
        material_sha256=material_sha256,
        executed_read_inventory=inputs.executed_read_inventory,
        captured_reads=inputs.predecision_captured_reads,
        active_input_attestation=inputs.active_input_attestation,
        candidate_sha256=_digest("phase-one-test-candidate"),
        bound_input_scope_sha256=_digest("phase-one-test-input-scope"),
    )
    db.commit()
    return receipt


def _pre_reservation_authority(
    inputs,
    *,
    now: datetime | None = None,
    allowed: bool = True,
    validity: timedelta = timedelta(seconds=5),
    verification_at: datetime | None = None,
):
    request = inputs.post_commit_request
    route = request.route_token
    intent = request.intent
    checked_at = now or (intent.decision_at + timedelta(milliseconds=1))
    blocker = None if allowed else "governance_kill_switch"
    reason = None if allowed else "governance_kill_switch"
    evidence = {
        "schema_version": "chili.alpaca-final-breaker-admission.v1",
        "phase": "pre_reservation",
        "execution_family": route.execution_family,
        "checked_at_utc": checked_at.isoformat(),
        "checks": [{"id": "test_financial_breaker", "ok": allowed}],
        "allowed": allowed,
        "breaker": blocker,
        "reason": reason,
    }
    receipt = CapturedPaperFinancialBreakerReceipt(
        phase="pre_reservation",
        completion_sha256=request.completion_sha256,
        route_token_sha256=route.route_token_sha256,
        intent_sha256=intent.intent_sha256,
        session_id=route.session_id,
        symbol=route.symbol,
        execution_family=route.execution_family,
        account_scope=route.account_scope,
        expected_account_id=route.expected_account_id,
        code_build_sha256=route.code_build_sha256,
        config_sha256=route.config_sha256,
        feature_flags_sha256=intent.feature_flags_sha256,
        policy_sha256=intent.policy_sha256,
        runtime_generation=route.runtime_generation,
        intent_generation=intent.intent_generation,
        completion_generation=request.completion_generation,
        decision_id=intent.decision_id,
        capture_receipt_sha256=route.capture_receipt_sha256,
        checked_at=checked_at,
        issued_at=checked_at,
        valid_until=min(
            checked_at + validity,
            intent.confirmed_arm_generation.expires_at,
        ),
        allowed=allowed,
        blocker=blocker,
        reason=reason,
        breaker_evidence=evidence,
    )
    return {
        "executed_captured_reads": inputs.predecision_captured_reads,
        "financial_breaker_receipt": receipt,
        "financial_breaker_verification_at": verification_at or checked_at,
    }


def test_atomic_zero_pending_admission_commits_before_typed_handoff(db):
    now = db.execute(
        text("SELECT clock_timestamp() - interval '200 ms'")
    ).scalar_one()
    inputs = _inputs(now=now)
    _seed_session(db, inputs.post_commit_request)
    _record_phase_one(db, inputs)
    assert admission.read_committed_captured_paper_admission(
        engine,
        request=inputs.post_commit_request,
    ) is None
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", record_sql)
    pre_reservation = _pre_reservation_authority(inputs)
    try:
        committed = admission.commit_captured_paper_admission(
            engine,
            inputs=inputs,
            phase_one_material_sha256=PHASE_ONE_MATERIAL_SHA256,
            executed_read_inventory=inputs.executed_read_inventory,
            **pre_reservation,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)

    assert type(committed) is admission.CommittedCapturedPaperAdmission
    assert committed.post_commit_request is inputs.post_commit_request
    assert committed.quantity_shares > 0
    assert committed.order_request["position_intent"] == "buy_to_open"
    assert committed.order_request["qty"] == str(committed.quantity_shares)
    assert committed.order_request["limit_price"] == "3.00"
    assert committed.committed_at.tzinfo is not None
    replayed = admission.read_committed_captured_paper_admission(
        engine,
        request=inputs.post_commit_request,
    )
    assert replayed == committed

    action = db.execute(
        text(
            "SELECT phase, metadata_json FROM broker_symbol_action_claims "
            "WHERE account_scope='alpaca:paper' AND symbol=:symbol"
        ),
        {"symbol": SYMBOL},
    ).mappings().one()
    assert action["phase"] == "claimed"
    assert action["metadata_json"]["order_request"]["position_intent"] == (
        "buy_to_open"
    )
    assert "entry_transport_started" not in action["metadata_json"]
    assert _table_count(db, "adaptive_risk_decision_packets") == 1
    assert _table_count(db, "adaptive_risk_reservations") == 1
    assert _table_count(db, "captured_paper_post_commit_outbox") == 1
    outbox_status = db.execute(
        text(
            "SELECT status FROM captured_paper_post_commit_outbox "
            "WHERE completion_sha256=:completion"
        ),
        {"completion": inputs.post_commit_request.completion_sha256},
    ).scalar_one()
    assert outbox_status == "pending"
    admission_record = json.loads(
        db.execute(
            text(
                "SELECT admission_record_canonical_json "
                "FROM captured_paper_post_commit_outbox "
                "WHERE completion_sha256=:completion"
            ),
            {"completion": inputs.post_commit_request.completion_sha256},
        ).scalar_one()
    )
    financial_receipt = pre_reservation["financial_breaker_receipt"]
    assert admission_record[
        "pre_reservation_financial_breaker_receipt_sha256"
    ] == financial_receipt.receipt_sha256
    assert admission_record[
        "pre_reservation_financial_breaker_evidence_sha256"
    ] == financial_receipt.breaker_evidence_sha256
    assert admission_record[
        "pre_reservation_financial_breaker_evaluator_id"
    ] == financial_receipt.evaluator_id
    assert admission_record[
        "pre_reservation_financial_breaker_receipt"
    ] == financial_receipt.to_payload()
    assert admission_record["executed_read_inventory_sha256"] == (
        inputs.executed_read_inventory.inventory_sha256
    )
    phase_one_state = db.execute(
        text(
            "SELECT state, event_sequence FROM captured_paper_phase_one_handoffs "
            "WHERE completion_sha256=:completion"
        ),
        {"completion": inputs.post_commit_request.completion_sha256},
    ).mappings().one()
    assert phase_one_state == {
        "state": phase_one.STATE_OUTBOX_COMMITTED,
        "event_sequence": 2,
    }

    def sql_index(*needles: str) -> int:
        return next(
            index
            for index, statement in enumerate(statements)
            if all(needle in statement for needle in needles)
        )

    advisory = [
        index
        for index, statement in enumerate(statements)
        if "pg_advisory_xact_lock" in statement
    ]
    assert len(advisory) >= 2
    lock_walk = (
        sql_index("from captured_paper_phase_one_handoffs", "for update"),
        advisory[0],
        advisory[1],
        sql_index("from alpaca_paper_account_settlement_heads", "for update"),
        sql_index("from adaptive_risk_reservations", "for update"),
        sql_index("from alpaca_paper_fill_activities", "for update"),
        sql_index("from alpaca_paper_cycle_settlements", "for update"),
        sql_index("from broker_symbol_action_claims", "for update"),
        sql_index("from trading_automation_sessions", "for update"),
        sql_index("insert into captured_paper_post_commit_outbox"),
    )
    assert lock_walk == tuple(sorted(lock_walk))


def test_rolled_back_phase_one_can_never_create_admission_authority(db):
    now = db.execute(
        text("SELECT clock_timestamp() - interval '200 ms'")
    ).scalar_one()
    inputs = _inputs(now=now)
    _seed_session(db, inputs.post_commit_request)
    phase_one.record_captured_paper_phase_one_handoff(
        db,
        request=inputs.post_commit_request,
        material_sha256=PHASE_ONE_MATERIAL_SHA256,
        executed_read_inventory=inputs.executed_read_inventory,
        captured_reads=inputs.predecision_captured_reads,
        active_input_attestation=inputs.active_input_attestation,
        candidate_sha256=_digest("rolled-back-candidate"),
        bound_input_scope_sha256=_digest("rolled-back-scope"),
    )
    db.rollback()

    with pytest.raises(
        admission.CapturedPaperAdmissionRejected,
        match="captured_paper_phase_one_handoff_missing",
    ):
        admission.commit_captured_paper_admission(
            engine,
            inputs=inputs,
            phase_one_material_sha256=PHASE_ONE_MATERIAL_SHA256,
            executed_read_inventory=inputs.executed_read_inventory,
            **_pre_reservation_authority(inputs),
        )

    for table in (
        "adaptive_risk_opportunity_claims",
        "adaptive_risk_decision_packets",
        "adaptive_risk_reservations",
        "broker_symbol_action_claims",
        "captured_paper_post_commit_outbox",
    ):
        assert _table_count(db, table) == 0


def test_phase_one_transition_failure_rolls_back_risk_claim_and_outbox(
    db,
    monkeypatch,
):
    now = db.execute(
        text("SELECT clock_timestamp() - interval '200 ms'")
    ).scalar_one()
    inputs = _inputs(now=now)
    _seed_session(db, inputs.post_commit_request)
    _record_phase_one(db, inputs)
    original = admission.commit_captured_paper_phase_one_outbox_in_transaction

    def fail_after_transition(*args, **kwargs):
        original(*args, **kwargs)
        raise phase_one.CapturedPaperPhaseOneHandoffError(
            "synthetic_phase_one_commit_ack_loss"
        )

    monkeypatch.setattr(
        admission,
        "commit_captured_paper_phase_one_outbox_in_transaction",
        fail_after_transition,
    )
    with pytest.raises(
        admission.CapturedPaperAdmissionRejected,
        match="synthetic_phase_one_commit_ack_loss",
    ):
        admission.commit_captured_paper_admission(
            engine,
            inputs=inputs,
            phase_one_material_sha256=PHASE_ONE_MATERIAL_SHA256,
            executed_read_inventory=inputs.executed_read_inventory,
            **_pre_reservation_authority(inputs),
        )

    phase_state = db.execute(
        text(
            "SELECT state, event_sequence FROM captured_paper_phase_one_handoffs "
            "WHERE completion_sha256=:completion"
        ),
        {"completion": inputs.post_commit_request.completion_sha256},
    ).mappings().one()
    assert phase_state == {"state": phase_one.STATE_PENDING, "event_sequence": 1}
    for table in (
        "adaptive_risk_opportunity_claims",
        "adaptive_risk_decision_packets",
        "adaptive_risk_reservations",
        "broker_symbol_action_claims",
        "captured_paper_post_commit_outbox",
    ):
        assert _table_count(db, table) == 0


def test_post_lock_session_drift_rolls_back_every_admission_row(db):
    now = db.execute(
        text("SELECT clock_timestamp() - interval '200 ms'")
    ).scalar_one()
    inputs = _inputs(now=now)
    _seed_session(
        db,
        inputs.post_commit_request,
        account_id=OTHER_ACCOUNT_ID,
    )
    _record_phase_one(db, inputs)

    with pytest.raises(
        admission.CapturedPaperAdmissionRejected,
        match="captured_paper_route_account_id_drift",
    ):
        admission.commit_captured_paper_admission(
            engine,
            inputs=inputs,
            phase_one_material_sha256=PHASE_ONE_MATERIAL_SHA256,
            executed_read_inventory=inputs.executed_read_inventory,
            **_pre_reservation_authority(inputs),
        )

    for table in (
        "alpaca_paper_account_settlement_heads",
        "adaptive_risk_decision_packets",
        "adaptive_risk_reservations",
        "broker_symbol_action_claims",
        "captured_paper_post_commit_outbox",
    ):
        assert _table_count(db, table) == 0


def test_missing_first_dip_receipt_keeps_daily_opportunity_reusable(db):
    now = db.execute(
        text("SELECT clock_timestamp() - interval '200 ms'")
    ).scalar_one()
    inputs = _inputs(now=now, setup_family="first_dip_reclaim")
    _record_phase_one(db, inputs)

    with pytest.raises(
        admission.CapturedPaperAdmissionRejected,
        match="captured_paper_first_dip_detector_audit_missing",
    ):
        admission.commit_captured_paper_admission(
            engine,
            inputs=inputs,
            phase_one_material_sha256=PHASE_ONE_MATERIAL_SHA256,
            executed_read_inventory=inputs.executed_read_inventory,
            **_pre_reservation_authority(inputs),
        )

    for table in (
        "adaptive_risk_opportunity_claims",
        "adaptive_risk_decision_packets",
        "adaptive_risk_reservations",
        "broker_symbol_action_claims",
        "captured_paper_post_commit_outbox",
    ):
        assert _table_count(db, table) == 0


@pytest.mark.parametrize(
    ("input_kwargs", "reason"),
    (
        (
            {"first_dip_policy_mode": "baseline"},
            "captured_paper_candidate_policy_not_active",
        ),
        (
            {"expected_account_id": OTHER_ACCOUNT_ID},
            "captured_paper_pure_binding_mismatch:account_id",
        ),
    ),
)
def test_policy_or_account_tamper_fails_before_any_authority_row(
    db,
    input_kwargs,
    reason,
):
    now = db.execute(
        text("SELECT clock_timestamp() - interval '200 ms'")
    ).scalar_one()
    inputs = _inputs(now=now, **input_kwargs)
    _record_phase_one(db, inputs)

    with pytest.raises(admission.CapturedPaperAdmissionRejected, match=reason):
        admission.commit_captured_paper_admission(
            engine,
            inputs=inputs,
            phase_one_material_sha256=PHASE_ONE_MATERIAL_SHA256,
            executed_read_inventory=inputs.executed_read_inventory,
            **_pre_reservation_authority(inputs),
        )

    assert _table_count(db, "broker_symbol_action_claims") == 0
    assert _table_count(db, "adaptive_risk_reservations") == 0
    assert _table_count(db, "captured_paper_post_commit_outbox") == 0


def test_buy_to_open_is_part_of_the_exact_broker_request_contract(db):
    now = db.execute(
        text("SELECT clock_timestamp() - interval '200 ms'")
    ).scalar_one()
    inputs = _inputs(now=now)
    request = inputs.post_commit_request
    complete = {
        "asset_class": "us_equity",
        "client_order_id": request.intent.client_order_id,
        "extended_hours": True,
        "limit_price": "3.00",
        "position_intent": "buy_to_open",
        "qty": "100",
        "side": "buy",
        "symbol": SYMBOL,
        "time_in_force": "day",
        "type": "limit",
    }
    assert admission._verify_order_request(
        complete,
        request=request,
        quantity_shares=100,
    )["position_intent"] == "buy_to_open"

    missing = dict(complete)
    missing.pop("position_intent")
    with pytest.raises(
        admission.CapturedPaperAdmissionRejected,
        match="captured_paper_order_request_fields_invalid",
    ):
        admission._verify_order_request(
            missing,
            request=request,
            quantity_shares=100,
        )
    changed = {**complete, "position_intent": "buy"}
    with pytest.raises(
        admission.CapturedPaperAdmissionRejected,
        match="captured_paper_order_request_binding_mismatch",
    ):
        admission._verify_order_request(
            changed,
            request=request,
            quantity_shares=100,
        )


@pytest.mark.parametrize(
    "defect",
    ("missing_read", "coverage_gap", "reordered", "foreign_generation"),
)
def test_executed_read_defects_fail_before_any_admission_sql_or_side_effect(
    db,
    defect,
):
    now = db.execute(
        text("SELECT clock_timestamp() - interval '200 ms'")
    ).scalar_one()
    inputs = _inputs(now=now)
    _record_phase_one(db, inputs)
    inventory = inputs.executed_read_inventory
    reads = inputs.predecision_captured_reads
    if defect == "missing_read":
        executed_reads = reads[:-1]
    elif defect == "coverage_gap":
        executed_reads = (
            replace(reads[0], coverage_gap_recorded=True),
            *reads[1:],
        )
    elif defect == "reordered":
        executed_reads = reads
        inventory = replace(inventory, reads=tuple(reversed(inventory.reads)))
    else:
        executed_reads = reads
        inventory = replace(inventory, generation=inventory.generation + 1)
    authority = _pre_reservation_authority(inputs)
    authority["executed_captured_reads"] = tuple(executed_reads)
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        with pytest.raises(
            admission.CapturedPaperAdmissionRejected,
            match="captured_paper_executed_read_inventory_rejected",
        ):
            admission.commit_captured_paper_admission(
                engine,
                inputs=inputs,
                phase_one_material_sha256=PHASE_ONE_MATERIAL_SHA256,
                executed_read_inventory=inventory,
                **authority,
            )
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)

    assert statements == []
    assert _table_count(db, "broker_symbol_action_claims") == 0
    assert _table_count(db, "adaptive_risk_reservations") == 0
    assert _table_count(db, "adaptive_risk_opportunity_claims") == 0
    assert _table_count(db, "captured_paper_post_commit_outbox") == 0


@pytest.mark.parametrize(
    "defect",
    ("missing", "denied", "stale", "foreign_request"),
)
def test_pre_reservation_financial_breaker_defects_fail_before_session(
    db,
    defect,
):
    now = db.execute(
        text("SELECT clock_timestamp() - interval '200 ms'")
    ).scalar_one()
    inputs = _inputs(now=now)
    _record_phase_one(db, inputs)
    if defect == "denied":
        authority = _pre_reservation_authority(inputs, allowed=False)
    elif defect == "stale":
        checked_at = inputs.post_commit_request.intent.decision_at + timedelta(
            milliseconds=1
        )
        authority = _pre_reservation_authority(
            inputs,
            now=checked_at,
            validity=timedelta(milliseconds=1),
            verification_at=checked_at + timedelta(milliseconds=2),
        )
    elif defect == "foreign_request":
        foreign = _inputs(
            now=now,
            decision_id="chili_ml_ACTU_41_financial_foreign",
            binder_id="166969db-e938-4342-af03-06b04a501f5d",
            intent_generation="601c3897-c2f1-4b2d-ab24-4be2b39a3c62",
            completion_generation="6a8c9369-b51b-4d02-85af-fd621e4c73c2",
        )
        authority = _pre_reservation_authority(inputs)
        authority["financial_breaker_receipt"] = _pre_reservation_authority(
            foreign
        )["financial_breaker_receipt"]
    else:
        authority = _pre_reservation_authority(inputs)
        authority["financial_breaker_receipt"] = None
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        with pytest.raises(
            admission.CapturedPaperAdmissionRejected,
            match="captured_paper_pre_reservation_financial_breaker",
        ):
            admission.commit_captured_paper_admission(
                engine,
                inputs=inputs,
                phase_one_material_sha256=PHASE_ONE_MATERIAL_SHA256,
                executed_read_inventory=inputs.executed_read_inventory,
                **authority,
            )
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)

    assert statements == []
    assert _table_count(db, "broker_symbol_action_claims") == 0
    assert _table_count(db, "adaptive_risk_reservations") == 0
    assert _table_count(db, "adaptive_risk_opportunity_claims") == 0
    assert _table_count(db, "captured_paper_post_commit_outbox") == 0


def test_pre_reservation_breaker_expiring_during_lock_walk_rolls_back_all(
    db,
    monkeypatch,
):
    """The receipt is rechecked at the last mutation-free reserve seam."""

    now = db.execute(
        text("SELECT clock_timestamp() - interval '200 ms'")
    ).scalar_one()
    inputs = _inputs(now=now)
    _seed_session(db, inputs.post_commit_request)
    _record_phase_one(db, inputs)
    authority = _pre_reservation_authority(
        inputs,
        validity=timedelta(seconds=2),
    )
    calls = 0

    def advancing_database_clock(session):
        nonlocal calls
        calls += 1
        observed = session.execute(
            text("SELECT clock_timestamp()")
        ).scalar_one()
        # The final call immediately before reserve crosses the receipt's
        # validity frontier; earlier lock/session checks still see real time.
        return observed if calls < 2 else observed + timedelta(seconds=10)

    monkeypatch.setattr(admission, "_db_now", advancing_database_clock)
    with pytest.raises(
        admission.CapturedPaperAdmissionRejected,
        match=(
            "captured_paper_pre_reservation_financial_breaker_"
            "rejected_at_reserve"
        ),
    ):
        admission.commit_captured_paper_admission(
            engine,
            inputs=inputs,
            phase_one_material_sha256=PHASE_ONE_MATERIAL_SHA256,
            executed_read_inventory=inputs.executed_read_inventory,
            **authority,
        )

    assert calls == 2
    phase_state = db.execute(
        text(
            "SELECT state, event_sequence "
            "FROM captured_paper_phase_one_handoffs "
            "WHERE completion_sha256=:completion"
        ),
        {"completion": inputs.post_commit_request.completion_sha256},
    ).mappings().one()
    assert phase_state == {"state": phase_one.STATE_PENDING, "event_sequence": 1}
    for table in (
        "adaptive_risk_opportunity_claims",
        "adaptive_risk_decision_packets",
        "adaptive_risk_reservations",
        "broker_symbol_action_claims",
        "captured_paper_post_commit_outbox",
    ):
        assert _table_count(db, table) == 0


def test_financial_breaker_slow_observation_cannot_be_reissued_as_fresh(
    db,
    monkeypatch,
):
    checked_at = db.execute(text("SELECT clock_timestamp()" )).scalar_one()
    inputs = _inputs(now=checked_at - timedelta(milliseconds=200))
    request = inputs.post_commit_request
    issuer = SqlAlchemyCapturedPaperFinancialBreakerIssuer(
        engine,
        observation_clock=lambda: checked_at + timedelta(seconds=6),
        validity_seconds=5,
    )
    evidence = {
        "schema_version": "chili.alpaca-final-breaker-admission.v1",
        "phase": "pre_reservation",
        "execution_family": request.route_token.execution_family,
        "checked_at_utc": checked_at.isoformat(),
        "checks": [{"id": "test_slow_observation", "ok": True}],
        "allowed": True,
        "breaker": None,
        "reason": None,
    }
    monkeypatch.setattr(
        issuer,
        "_evaluate",
        lambda _request, *, phase: (True, evidence),
    )

    with pytest.raises(
        CapturedPaperFinancialBreakerError,
        match="financial_breaker_observation_stale_or_future",
    ):
        issuer.issue_for_request(request, phase="pre_reservation")


def test_module_has_no_broker_network_wall_clock_or_magic_cap_fallback():
    source = inspect.getsource(admission)
    lowered = source.lower()
    for prohibited in (
        "alpacaspotadapter",
        "requests.",
        "httpx.",
        "datetime.now",
        "_reserve_alpaca_entry_risk",
        "entry_transport_started",
        "$50",
        "$250",
        "one_symbol",
    ):
        assert prohibited not in lowered
    assert admission.CAPTURED_PAPER_CANONICAL_LOCK_ORDER == (
        "alpaca_account_advisory",
        "adaptive_account_advisory",
        "account_settlement_head",
        "adaptive_risk_reservation",
        "fill_activity_or_cycle_settlement",
        "broker_symbol_action_claim",
        "trading_automation_session",
        "adaptive_risk_opportunity_claim",
        "captured_paper_post_commit_outbox",
    )
    fields = admission.CapturedPaperOperationalPolicy.__dataclass_fields__
    assert not any(
        fragment in name
        for name in fields
        for fragment in ("dollar", "daily_loss", "symbol_count", "position_count")
    )


def test_extended_hours_policy_requires_day_without_disabling_premarket():
    policy = admission.CapturedPaperOperationalPolicy(
        action_claim_lease_seconds=30,
        outbox_max_attempts=3,
        outbox_max_reconciliation_attempts=2,
        reconciliation_retry_delay_seconds=5,
        reconciliation_health_escalation_delay_seconds=60,
        time_in_force="day",
        extended_hours=True,
        config_provenance_sha256=_digest("config"),
    )
    assert policy.extended_hours is True
    assert policy.time_in_force == "day"

    with pytest.raises(
        ValueError,
        match="extended-hours limit entries require DAY tif",
    ):
        admission.CapturedPaperOperationalPolicy(
            action_claim_lease_seconds=30,
            outbox_max_attempts=3,
            outbox_max_reconciliation_attempts=2,
            reconciliation_retry_delay_seconds=5,
            reconciliation_health_escalation_delay_seconds=60,
            time_in_force="gtc",
            extended_hours=True,
            config_provenance_sha256=_digest("config"),
        )
