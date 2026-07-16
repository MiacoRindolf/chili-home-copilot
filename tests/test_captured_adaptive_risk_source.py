from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
import socket
import uuid

import pytest

from app.services.trading.momentum_neural.adaptive_risk_policy import (
    AdaptiveRiskPolicy,
    resolve_adaptive_risk,
)
from app.services.trading.momentum_neural.adaptive_risk_request_builder import (
    build_adaptive_risk_request,
)
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    ImmutableAccountRiskSnapshot,
    LockedAdaptiveRiskAdmissionSnapshot,
    RESERVATION_LEDGER_GENERATION,
)
from app.services.trading.momentum_neural.alpaca_paper_account_receipt import (
    ALPACA_PAPER_ACCOUNT_PAYLOAD_KEYS,
    ALPACA_PAPER_ACCOUNT_PAYLOAD_SCHEMA_VERSION,
    ALPACA_PAPER_ACCOUNT_PROVIDER,
    ALPACA_PAPER_ACCOUNT_QUERY_SCHEMA_VERSION,
    alpaca_paper_account_capture_query,
)
from app.services.trading.momentum_neural.alpaca_paper_identity import (
    alpaca_paper_account_identity_sha256,
)
from app.services.trading.momentum_neural.captured_adaptive_risk_source import (
    CapturedAccountRiskReceipt,
    CapturedAdaptiveRiskCoverageUnavailable,
    CapturedAdaptiveRiskDecisionBoundary,
    CapturedAdaptiveRiskDecisionIdentity,
    CapturedAdaptiveRiskEconomicInputs,
    CapturedAdaptiveRiskEvidenceSet,
    CapturedAdaptiveRiskFactProvenance,
    CapturedAdaptiveRiskPolicySpec,
    CapturedAdaptiveRiskSourceFactory,
    CapturedExactBbo,
    captured_adaptive_risk_fact_payloads,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    ALPACA_NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION,
    ALPACA_NBBO_QUOTE_QUERY_SCHEMA_VERSION,
    CaptureClocks,
    CaptureEventRef,
    CaptureReadReceipt,
    CaptureStream,
    FSMDependencyProfile,
    FSMStreamDependency,
    captured_read_result_sha256,
    sha256_json,
)
from app.services.trading.momentum_neural.replay_capture_runtime import (
    BoundedCaptureIngress,
    CaptureProducerLifecycleRuntime,
)
from tests.test_replay_capture_producer_lifecycle import (
    BASE,
    _ManualClock,
    _identity,
    _live_continuity,
    _producer,
    _resource_binding,
)


ACCOUNT_ID = "6c143be2-d40a-4a5e-a8a8-d6fc19d2cd79"


def _policy() -> AdaptiveRiskPolicy:
    return AdaptiveRiskPolicy(
        policy_version="captured-paper-shared-v1",
        policy_source="effective-config:adaptive-equity-risk-v1",
        risk_fraction_of_equity=0.01,
        daily_risk_fraction_of_equity=0.10,
        portfolio_risk_fraction_of_equity=0.05,
        cluster_risk_fraction_of_equity=0.04,
        symbol_risk_fraction_of_equity=0.03,
        daily_gap_reserve_fraction_of_equity=0.001,
        max_notional_fraction_of_equity=0.80,
        max_buying_power_fraction_for_notional=0.50,
        max_portfolio_gross_fraction_of_equity=2.0,
        quality_multiplier_floor=0.50,
        quality_multiplier_ceiling=1.50,
        volatility_reference_fraction=0.05,
        volatility_multiplier_floor=0.40,
        spread_reserve_multiple=1.0,
        per_share_gap_reserve_volatility_multiple=0.10,
        max_adv_participation=0.02,
        max_recent_volume_participation=0.10,
        max_executable_depth_participation=0.50,
        market_data_max_age_seconds=2.0,
        account_data_max_age_seconds=10.0,
        reservation_data_max_age_seconds=1.0,
        context_data_max_age_seconds=60.0,
    )


def _active_nbbo_proof(
    *,
    decision_id: str,
    provider: str = "alpaca_market_data_paper",
    stream: CaptureStream = CaptureStream.ALPACA_NBBO_QUOTE,
    account_provider: str = ALPACA_PAPER_ACCOUNT_PROVIDER,
    account_payload_overrides: dict | None = None,
    account_query_overrides: dict | None = None,
    account_snapshot_overrides: dict | None = None,
):
    identity = replace(
        _identity(),
        account_identity_sha256=alpaca_paper_account_identity_sha256(ACCOUNT_ID),
    )
    binding = _resource_binding()
    producer = _producer(
        identity,
        binding,
        streams=(
            stream,
            CaptureStream.ACCOUNT_RISK_SNAPSHOT,
            CaptureStream.FSM_DECISION,
        ),
    )
    clock = _ManualClock(BASE)
    runtime = CaptureProducerLifecycleRuntime(
        identity=identity,
        ingress=BoundedCaptureIngress.from_resource_binding(binding),
        resource_binding=binding,
        producers=(producer,),
        heartbeat_timeout_seconds=1.0,
        wall_clock=clock,
    )
    runtime.open(opened_at=BASE)
    runtime.register(
        producer.producer_id,
        recorded_at=clock.set(BASE + timedelta(milliseconds=1)),
    )
    source_at = clock.set(BASE + timedelta(milliseconds=2))
    payload = {
        "schema_version": ALPACA_NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION,
        "symbol": "VEEE",
        "bid": 4.99,
        "ask": 5.00,
        "bid_size": 1_200.0,
        "ask_size": 900.0,
        "size_unit": "shares",
        "feed": "iex",
        "provider_event_at": source_at.isoformat().replace("+00:00", "Z"),
        "received_at": source_at.isoformat().replace("+00:00", "Z"),
        "account_scope": "alpaca:paper",
    }
    query = {
        "schema_version": ALPACA_NBBO_QUOTE_QUERY_SCHEMA_VERSION,
        "operation": "get_execution_bbo",
        "symbol": "VEEE",
        "feed": "iex",
        "max_age_seconds": 2.0,
        "account_scope": "alpaca:paper",
    }
    source = runtime.submit_input(
        producer.producer_id,
        stream=stream,
        provider=provider,
        symbol="VEEE",
        clocks=CaptureClocks(
            provider_event_at=source_at,
            received_at=source_at,
            available_at=source_at,
        ),
        payload=payload,
        query=query,
        recorded_at=source_at,
    )
    bbo_read_id = str(uuid.uuid4())
    bbo_receipt = CaptureReadReceipt(
        read_id=bbo_read_id,
        decision_id=decision_id,
        identity_sha256=identity.identity_sha256,
        stream=stream,
        provider=provider,
        symbol="VEEE",
        requested_at=BASE + timedelta(milliseconds=1, microseconds=500),
        returned_at=source_at,
        query_sha256=sha256_json(query),
        source_event_sha256s=(source.event_sha256,),
        empty_result=False,
        result_sha256=captured_read_result_sha256(
            (CaptureEventRef.from_event(source),)
        ),
        content_verified=True,
        replay_network_fallback_used=False,
        query=query,
    )
    runtime.submit_read_receipt(bbo_receipt)
    if stream is CaptureStream.NBBO_QUOTE:
        runtime.submit_live_continuity_checkpoint(
            producer.producer_id,
            _live_continuity(identity, source),
            recorded_at=source_at,
        )

    account_requested_at = BASE + timedelta(milliseconds=1, microseconds=500)
    account_received_at = BASE + timedelta(milliseconds=1, microseconds=600)
    account_recorded_at = clock.set(BASE + timedelta(milliseconds=2, microseconds=100))
    account_payload = {
        "schema_version": ALPACA_PAPER_ACCOUNT_PAYLOAD_SCHEMA_VERSION,
        "account_id": ACCOUNT_ID,
        "account_identity_sha256": identity.account_identity_sha256,
        "account_scope": "alpaca:paper",
        "paper": True,
        "status": "ACTIVE",
        "equity_usd": "100000.00",
        "last_equity_usd": "101000.00",
        "buying_power_usd": "400000.00",
        "cash_usd": "100000.00",
        "account_blocked": False,
        "trading_blocked": False,
        "trade_suspended_by_user": False,
        "received_at": account_received_at.isoformat().replace("+00:00", "Z"),
    }
    account_payload.update(account_payload_overrides or {})
    account_query = alpaca_paper_account_capture_query(ACCOUNT_ID)
    account_query.update(account_query_overrides or {})
    account = ImmutableAccountRiskSnapshot(
        snapshot_id="captured-paper-account-1",
        source=ALPACA_PAPER_ACCOUNT_PROVIDER,
        provider_generation=f"{producer.producer_id}:{producer.generation}",
        account_scope="alpaca:paper",
        execution_family="alpaca_spot",
        broker_environment="paper",
        venue="alpaca",
        account_identity_sha256=identity.account_identity_sha256,
        observed_at=account_received_at,
        available_at=account_recorded_at,
        equity_usd=100_000.0,
        buying_power_usd=400_000.0,
        broker_day_change_usd=-1_000.0,
        local_realized_pnl_usd=0.0,
        pending_policy_buying_power_reflected_usd=0.0,
    )
    if account_snapshot_overrides:
        account = replace(account, **account_snapshot_overrides)
    account_source = runtime.submit_input(
        producer.producer_id,
        stream=CaptureStream.ACCOUNT_RISK_SNAPSHOT,
        provider=account_provider,
        symbol=None,
        clocks=CaptureClocks(
            received_at=account_received_at,
            available_at=account_recorded_at,
            market_reference_at=account_recorded_at,
        ),
        payload=account_payload,
        query=account_query,
        recorded_at=account_recorded_at,
    )
    account_read_id = str(uuid.uuid4())
    account_receipt = CaptureReadReceipt(
        read_id=account_read_id,
        decision_id=decision_id,
        identity_sha256=identity.identity_sha256,
        stream=CaptureStream.ACCOUNT_RISK_SNAPSHOT,
        provider=account_provider,
        symbol=None,
        requested_at=account_requested_at,
        returned_at=account_recorded_at,
        query_sha256=sha256_json(account_query),
        source_event_sha256s=(account_source.event_sha256,),
        empty_result=False,
        result_sha256=captured_read_result_sha256(
            (CaptureEventRef.from_event(account_source),)
        ),
        content_verified=True,
        replay_network_fallback_used=False,
        query=account_query,
    )
    runtime.submit_read_receipt(account_receipt)
    profile = FSMDependencyProfile(
        required_streams=frozenset(
            {stream, CaptureStream.ACCOUNT_RISK_SNAPSHOT}
        ),
        required_read_ids=(bbo_read_id, account_read_id),
        stream_dependencies=(
            FSMStreamDependency(
                stream=stream,
                exact_provider_event_at_required=True,
                market_reference_at_required=False,
                max_source_age_seconds=10.0,
                coverage_start_at=source_at,
            ),
            FSMStreamDependency(
                stream=CaptureStream.ACCOUNT_RISK_SNAPSHOT,
                exact_provider_event_at_required=False,
                market_reference_at_required=True,
                max_source_age_seconds=10.0,
                coverage_start_at=account_recorded_at,
            ),
        ),
    )
    clock.set(BASE + timedelta(milliseconds=3))
    proof = runtime.attest_predecision_input_prefix(
        decision_id=decision_id,
        dependency_profile=profile,
    )
    return (
        identity,
        source,
        bbo_read_id,
        proof,
        payload,
        account,
        account_source,
        account_read_id,
        account_payload,
    )


def _empty_ledger(
    *,
    account: ImmutableAccountRiskSnapshot,
    symbol: str,
    cluster: str,
    observed_at,
    pending_buying_power_impact_usd: float = 0.0,
) -> LockedAdaptiveRiskAdmissionSnapshot:
    aggregates = {
        "open_structural_risk_usd": 0.0,
        "pending_reserved_risk_usd": 0.0,
        "existing_same_symbol_structural_risk_usd": 0.0,
        "pending_same_symbol_structural_risk_usd": 0.0,
        "current_cluster_structural_risk_usd": 0.0,
        "pending_correlation_cluster_risk_usd": 0.0,
        "portfolio_gross_notional_usd": 0.0,
        "pending_portfolio_gross_notional_usd": 0.0,
        "open_buying_power_impact_usd": 0.0,
        "pending_buying_power_impact_usd": pending_buying_power_impact_usd,
    }
    ledger_payload = {
        "schema_version": RESERVATION_LEDGER_GENERATION,
        "account_scope": account.account_scope,
        "aggregates": aggregates,
        "active_reservations": [],
        "pending_settlements": [],
        "quarantined_exposures": [],
        "paper_position_bindings": [],
    }
    return LockedAdaptiveRiskAdmissionSnapshot.create(
        account_scope=account.account_scope,
        symbol=symbol,
        correlation_cluster=cluster,
        account_snapshot_sha256=account.snapshot_sha256,
        transaction_id="1",
        backend_pid=1,
        lock_receipt_id=str(uuid.uuid4()),
        observed_at=observed_at,
        aggregates=aggregates,
        ledger_payload=ledger_payload,
        policy_buying_power_capacity_usd=account.buying_power_usd,
    )


def _factory_and_boundary(
    *,
    execution_surface: str = "alpaca_paper",
    setup_family: str = "primary_entry",
    provider: str = "alpaca_market_data_paper",
    bbo_stream: CaptureStream = CaptureStream.ALPACA_NBBO_QUOTE,
    account_provider: str = ALPACA_PAPER_ACCOUNT_PROVIDER,
    account_payload_overrides: dict | None = None,
    account_query_overrides: dict | None = None,
    account_snapshot_overrides: dict | None = None,
    pending_buying_power_impact_usd: float = 0.0,
):
    decision_id = "chili-captured-generic-entry-1"
    (
        capture_identity,
        source,
        read_id,
        proof,
        payload,
        account,
        account_source,
        account_read_id,
        account_payload,
    ) = _active_nbbo_proof(
        decision_id=decision_id,
        provider=provider,
        stream=bbo_stream,
        account_provider=account_provider,
        account_payload_overrides=account_payload_overrides,
        account_query_overrides=account_query_overrides,
        account_snapshot_overrides=account_snapshot_overrides,
    )
    decision_at = proof.attested_available_at + timedelta(milliseconds=1)
    identity = CapturedAdaptiveRiskDecisionIdentity(
        execution_surface=execution_surface,
        run_id=capture_identity.run_id,
        generation=capture_identity.generation,
        decision_id=decision_id,
        symbol="VEEE",
        setup_family=setup_family,
        correlation_cluster="equity:momentum-a",
        account_scope="alpaca:paper",
        decision_at=decision_at,
    )
    ledger = _empty_ledger(
        account=account,
        symbol=identity.symbol,
        cluster=identity.correlation_cluster,
        observed_at=decision_at - timedelta(microseconds=100),
        pending_buying_power_impact_usd=pending_buying_power_impact_usd,
    )
    economics = CapturedAdaptiveRiskEconomicInputs(
        structural_stop=4.80,
        entry_slippage_bps=5.0,
        exit_slippage_bps=5.0,
        fees_per_share_usd=0.005,
        setup_quality=0.80,
        realized_volatility_fraction=0.05,
        average_daily_volume_shares=5_000_000.0,
        recent_volume_shares=500_000.0,
        executable_depth_shares=100_000.0,
        candidate_buying_power_impact_per_share_usd=5.00,
    )
    fact_payloads = captured_adaptive_risk_fact_payloads(identity, economics)

    def fact(name: str) -> CapturedAdaptiveRiskFactProvenance:
        return CapturedAdaptiveRiskFactProvenance.create(
            payload=fact_payloads[name],
            source=f"captured-derived:{name}",
            observed_at=source.clocks.provider_event_at,
            available_at=source.clocks.available_at,
            provider_generation="captured-derived-v1",
            source_read_ids=(read_id,),
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
    spec = CapturedAdaptiveRiskPolicySpec(
        policy=_policy(),
        code_build_sha256=capture_identity.code_build_sha256,
        effective_config_sha256=capture_identity.config_sha256,
        feature_flags_sha256=capture_identity.feature_flags_sha256,
    )
    factory = CapturedAdaptiveRiskSourceFactory(spec)
    boundary = CapturedAdaptiveRiskDecisionBoundary(
        identity=identity,
        exact_bbo=CapturedExactBbo(
            read_id=read_id,
            source_event_sha256=source.event_sha256,
            payload_json=json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
        economics=economics,
        fact_evidence=evidence,
        account_snapshot=account,
        account_receipt=CapturedAccountRiskReceipt(
            read_id=account_read_id,
            source_event_sha256=account_source.event_sha256,
            payload_json=json.dumps(
                account_payload,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
        reservation_ledger_snapshot=ledger,
        active_capture_attestation=proof,
    )
    return factory, boundary


@pytest.mark.parametrize(
    "setup_family",
    ("primary_entry", "cup_and_handle", "wick_reclaim", "momentum_pullback"),
)
def test_generic_captured_factory_reaches_normal_adaptive_request_path(
    setup_family: str,
) -> None:
    factory, boundary = _factory_and_boundary(setup_family=setup_family)

    material = factory.build(boundary)
    built = build_adaptive_risk_request(
        material.source,
        client_order_id=boundary.identity.decision_id,
        entry_limit_price=material.source.inputs.ask,
        active_capture_attestation=material.active_capture_attestation,
    )

    assert built.request.setup_family == setup_family
    assert built.request.inputs.execution_surface == "alpaca_paper"
    assert built.resolution.valid is True
    assert built.resolution.quantity_shares > 0
    assert built.trusted_capture_attestation_sha256 == (
        boundary.active_capture_attestation.attestation_sha256
    )


def test_real_flat_alpaca_paper_account_receipt_is_the_only_accepted_shape() -> None:
    factory, boundary = _factory_and_boundary()
    payload = json.loads(boundary.account_receipt.payload_json)

    material = factory.build(boundary)

    assert set(payload) == ALPACA_PAPER_ACCOUNT_PAYLOAD_KEYS
    assert payload["schema_version"] == ALPACA_PAPER_ACCOUNT_PAYLOAD_SCHEMA_VERSION
    assert payload["account_id"] == ACCOUNT_ID
    assert payload["last_equity_usd"] == "101000.00"
    assert material.source.inputs.broker_day_change_usd == -1_000.0
    assert material.source.inputs.equity_usd == 100_000.0
    assert material.source.inputs.buying_power_usd == 400_000.0


@pytest.mark.parametrize(
    ("factory_kwargs", "match"),
    (
        (
            {"account_payload_overrides": {"schema_version": "synthetic.v0"}},
            "account_payload_content_mismatch",
        ),
        (
            {"account_provider": "synthetic_account_provider"},
            "account_receipt_not_authoritative",
        ),
        (
            {
                "account_query_overrides": {
                    "schema_version": ALPACA_PAPER_ACCOUNT_QUERY_SCHEMA_VERSION
                    + ".tampered"
                }
            },
            "account_query_content_mismatch",
        ),
        (
            {
                "account_payload_overrides": {
                    "account_id": "5c143be2-d40a-4a5e-a8a8-d6fc19d2cd79"
                }
            },
            "account_payload_identity_or_posture_mismatch",
        ),
        (
            {
                "account_payload_overrides": {
                    "received_at": "2026-07-15T00:00:00Z"
                }
            },
            "account_payload_clock_mismatch",
        ),
        (
            {"account_payload_overrides": {"status": "INACTIVE"}},
            "account_payload_identity_or_posture_mismatch",
        ),
        (
            {"account_payload_overrides": {"account_blocked": True}},
            "account_payload_identity_or_posture_mismatch",
        ),
        (
            {"account_payload_overrides": {"trading_blocked": True}},
            "account_payload_identity_or_posture_mismatch",
        ),
        (
            {
                "account_payload_overrides": {
                    "trade_suspended_by_user": True
                }
            },
            "account_payload_identity_or_posture_mismatch",
        ),
        (
            {"account_snapshot_overrides": {"broker_day_change_usd": -999.99}},
            "account_snapshot_broker_fields_mismatch",
        ),
        (
            {"account_snapshot_overrides": {"source": "synthetic_account"}},
            "account_snapshot_broker_fields_mismatch",
        ),
        (
            {"account_snapshot_overrides": {"provider_generation": "stale:1"}},
            "account_snapshot_broker_fields_mismatch",
        ),
        (
            {
                "account_snapshot_overrides": {
                    "observed_at": BASE + timedelta(milliseconds=1, microseconds=700)
                }
            },
            "account_snapshot_broker_fields_mismatch",
        ),
        (
            {
                "account_snapshot_overrides": {
                    "available_at": BASE + timedelta(milliseconds=2)
                }
            },
            "account_snapshot_broker_fields_mismatch",
        ),
        (
            {
                "account_payload_overrides": {
                    "account_snapshot": {"synthetic": True}
                }
            },
            "account_payload_content_mismatch",
        ),
    ),
)
def test_account_receipt_tamper_is_decision_local_coverage_unavailable(
    factory_kwargs: dict,
    match: str,
) -> None:
    factory, boundary = _factory_and_boundary(**factory_kwargs)

    with pytest.raises(CapturedAdaptiveRiskCoverageUnavailable, match=match):
        factory.build(boundary)


def test_pending_buying_power_without_reflection_receipt_is_coverage_unavailable() -> None:
    factory, boundary = _factory_and_boundary(
        pending_buying_power_impact_usd=500.0,
    )

    with pytest.raises(
        CapturedAdaptiveRiskCoverageUnavailable,
        match="pending_policy_buying_power_reflection_unavailable",
    ):
        factory.build(boundary)


def test_replay_and_paper_share_byte_identical_economic_policy_and_resolution() -> None:
    factory, paper_boundary = _factory_and_boundary()
    paper = factory.build(paper_boundary).source
    replay_boundary = replace(
        paper_boundary,
        identity=replace(paper_boundary.identity, execution_surface="replay"),
    )
    replay = factory.build(replay_boundary).source

    paper_resolution = resolve_adaptive_risk(paper.policy, paper.inputs)
    replay_resolution = resolve_adaptive_risk(replay.policy, replay.inputs)

    assert paper.policy.policy_sha256 == replay.policy.policy_sha256
    assert paper.inputs.economic_input_sha256 == replay.inputs.economic_input_sha256
    assert (
        paper_resolution.economic_resolution_sha256
        == replay_resolution.economic_resolution_sha256
    )
    assert paper_resolution.quantity_shares == replay_resolution.quantity_shares


def test_factory_has_no_network_or_current_db_fallback(monkeypatch) -> None:
    factory, boundary = _factory_and_boundary()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("external fallback attempted")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    material = factory.build(boundary)

    assert material.source.inputs.symbol == "VEEE"
    assert material.source.inputs.evidence["bbo"].source.startswith("capture:")


def test_bbo_payload_mismatch_fails_only_the_decision_as_coverage_unavailable() -> None:
    factory, boundary = _factory_and_boundary()
    payload = json.loads(boundary.exact_bbo.payload_json)
    payload["ask"] = 5.01
    tampered = replace(
        boundary,
        exact_bbo=replace(
            boundary.exact_bbo,
            payload_json=json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )

    with pytest.raises(
        CapturedAdaptiveRiskCoverageUnavailable,
        match="bbo_payload_content_mismatch",
    ):
        factory.build(tampered)


def test_missing_derived_read_lineage_fails_before_source_or_claim_exists() -> None:
    factory, boundary = _factory_and_boundary()
    missing = replace(
        boundary.fact_evidence.structural_stop,
        source_read_ids=(str(uuid.uuid4()),),
    )
    tampered = replace(
        boundary,
        fact_evidence=replace(
            boundary.fact_evidence,
            structural_stop=missing,
        ),
    )

    with pytest.raises(
        CapturedAdaptiveRiskCoverageUnavailable,
        match="structural_stop_capture_reads_missing",
    ):
        factory.build(tampered)


def test_generic_iqfeed_nbbo_stream_cannot_be_laundered_as_alpaca_authority() -> None:
    factory, boundary = _factory_and_boundary(
        provider="iqfeed",
        bbo_stream=CaptureStream.NBBO_QUOTE,
    )

    with pytest.raises(
        CapturedAdaptiveRiskCoverageUnavailable,
        match="bbo_receipt_not_authoritative",
    ):
        factory.build(boundary)


def test_first_dip_stays_an_additive_special_tape_dependency() -> None:
    factory, boundary = _factory_and_boundary(setup_family="first_dip_reclaim")

    with pytest.raises(
        CapturedAdaptiveRiskCoverageUnavailable,
        match="first_dip_tape_dependency_mismatch",
    ):
        factory.build(boundary)


def test_policy_spec_rejects_a_paper_only_activation_variant() -> None:
    identity = _identity()
    with pytest.raises(
        CapturedAdaptiveRiskCoverageUnavailable,
        match="policy_not_shared_by_replay_and_alpaca_paper",
    ):
        CapturedAdaptiveRiskPolicySpec(
            policy=_policy(),
            code_build_sha256=identity.code_build_sha256,
            effective_config_sha256=identity.config_sha256,
            feature_flags_sha256=identity.feature_flags_sha256,
            applies_to_execution_surfaces=("alpaca_paper",),
        )


def test_decision_identity_rejects_noncanonical_nested_paper_scope() -> None:
    _factory, boundary = _factory_and_boundary()

    with pytest.raises(
        CapturedAdaptiveRiskCoverageUnavailable,
        match="account_scope_not_alpaca_paper",
    ):
        replace(
            boundary.identity,
            account_scope="alpaca:paper:paper-fixture",
        )
