from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import hashlib
import inspect
import json
import uuid

import pytest

from app.models.trading import (
    AlpacaPaperCycleSettlement,
    AlpacaPaperFillActivity,
    AlpacaPaperFillObservationActivity,
    AlpacaPaperFillQueryObservation,
)
from app.services.trading.momentum_neural.adaptive_risk_policy import (
    AdaptiveRiskContractError,
    AdaptiveRiskInputs,
    AdaptiveRiskPolicy,
    RiskInputEvidence,
    resolve_adaptive_risk,
)
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    AlpacaPaperBrokerAccountFacts,
    AdaptiveRiskReservationRequest,
    AdaptiveRiskReservationStore,
    ImmutableAccountRiskSnapshot,
    _derive_verified_alpaca_paper_daily_pnl_evidence,
)
from app.services.trading.momentum_neural.alpaca_cycle_settlement import (
    AlpacaCycleSettlementIntegrityError,
    SETTLEMENT_HASH_DOMAIN,
    SETTLEMENT_SCHEMA_VERSION,
    AlpacaPaperSettledDailyPnlEvidence,
    cycle_settlement_content_payload,
    new_zero_settlement_head,
    settlement_head_content_sha256,
)
from app.services.trading.momentum_neural.alpaca_fill_activity import (
    AlpacaPaperFillCycleBinding,
    prepare_alpaca_paper_terminal_fill_observation_receipt,
    prepare_verified_alpaca_paper_fill_activity,
)
from app.services.trading.momentum_neural.alpaca_paper_identity import (
    alpaca_paper_account_identity_sha256,
)
from tests.test_captured_alpaca_paper_adapter import (
    ACCOUNT_ID,
    _Clock,
    _account_input_attestation,
    _wrapper,
)


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _broker_facts(*, decision_id: str) -> AlpacaPaperBrokerAccountFacts:
    clock = _Clock()
    clock.now = datetime.now(tz=UTC) - timedelta(seconds=1)
    wrapper, _clock, _adapter, coordinator = _wrapper(clock=clock)
    with wrapper.decision_scope(decision_id):
        wrapper.capture_account_snapshot()
        proof = _account_input_attestation(
            coordinator,
            decision_id=decision_id,
            expires_at=clock.now + timedelta(minutes=1),
        )
        authority = wrapper.issue_account_authority(proof)
    return AlpacaPaperBrokerAccountFacts.from_capture_authority(authority)


def _policy() -> AdaptiveRiskPolicy:
    return AdaptiveRiskPolicy(
        policy_version="locked-paper-authority-v1",
        policy_source="focused-test",
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
        market_data_max_age_seconds=30.0,
        account_data_max_age_seconds=30.0,
        reservation_data_max_age_seconds=30.0,
        context_data_max_age_seconds=60.0,
    )


def _request_for_bundle(bundle, *, decision_id: str) -> AdaptiveRiskReservationRequest:
    at = bundle.decision_as_of
    ledger = bundle.locked_risk_snapshot
    evidence = {
        name: RiskInputEvidence(
            source=f"focused:{name}",
            observed_at=at,
            available_at=at,
            content_sha256=_hash(f"focused:{name}"),
            provider_generation="focused:1",
        )
        for name in (
            "bbo",
            "structural_stop",
            "setup_quality",
            "volatility",
            "liquidity",
            "portfolio_heat",
            "correlation",
            "code_build",
            "effective_config",
            "feature_flags",
            "capture_prefix",
            "candidate_buying_power_estimate",
        )
    }
    evidence["account"] = bundle.account_evidence
    evidence["daily_pnl"] = bundle.daily_pnl_evidence
    evidence["reservation_ledger"] = RiskInputEvidence(
        source="postgresql:adaptive_risk_reservations",
        observed_at=at,
        available_at=at,
        content_sha256=ledger.ledger_sha256,
        provider_generation="adaptive-risk-reservation-ledger.v1",
    )
    aggregates = ledger.aggregates
    account = bundle.account_snapshot
    inputs = AdaptiveRiskInputs(
        decision_id=decision_id,
        replay_or_paper_run_id=bundle.attestation.run_id,
        generation=bundle.attestation.generation,
        execution_surface="alpaca_paper",
        execution_family="alpaca_spot",
        venue="alpaca",
        broker_environment="paper",
        symbol="VEEE",
        side="long",
        as_of=at,
        account_identity_sha256=account.account_identity_sha256,
        code_build_sha256=_hash("build"),
        effective_config_sha256=_hash("config"),
        feature_flags_sha256=_hash("flags"),
        capture_prefix_root_sha256=_hash("capture"),
        equity_usd=account.equity_usd,
        buying_power_usd=account.buying_power_usd,
        broker_day_change_usd=account.broker_day_change_usd,
        local_realized_pnl_usd=account.local_realized_pnl_usd,
        open_structural_risk_usd=aggregates["open_structural_risk_usd"],
        pending_reserved_risk_usd=aggregates["pending_reserved_risk_usd"],
        existing_same_symbol_structural_risk_usd=aggregates[
            "existing_same_symbol_structural_risk_usd"
        ],
        pending_same_symbol_structural_risk_usd=aggregates[
            "pending_same_symbol_structural_risk_usd"
        ],
        current_cluster_structural_risk_usd=aggregates[
            "current_cluster_structural_risk_usd"
        ],
        pending_correlation_cluster_risk_usd=aggregates[
            "pending_correlation_cluster_risk_usd"
        ],
        portfolio_gross_notional_usd=aggregates[
            "portfolio_gross_notional_usd"
        ],
        pending_portfolio_gross_notional_usd=aggregates[
            "pending_portfolio_gross_notional_usd"
        ],
        policy_buying_power_capacity_usd=ledger.policy_buying_power_capacity_usd,
        open_buying_power_impact_usd=aggregates[
            "open_buying_power_impact_usd"
        ],
        pending_buying_power_impact_usd=aggregates[
            "pending_buying_power_impact_usd"
        ],
        candidate_buying_power_impact_per_share_usd=5.0,
        bid=4.99,
        ask=5.0,
        structural_stop=4.8,
        entry_slippage_bps=5.0,
        exit_slippage_bps=5.0,
        fees_per_share_usd=0.005,
        setup_quality=0.8,
        realized_volatility_fraction=0.05,
        average_daily_volume_shares=5_000_000,
        recent_volume_shares=500_000,
        executable_depth_shares=100_000,
        correlation_cluster_id="equity:momentum",
        evidence=evidence,
    )
    return AdaptiveRiskReservationRequest(
        policy=_policy(),
        inputs=inputs,
        account_snapshot=account,
        account_scope="alpaca:paper",
        setup_family="primary_entry",
        correlation_cluster="equity:momentum",
        client_order_id=decision_id,
        entry_limit_price=5.0,
        broker_account_evidence=bundle.account_evidence,
        settled_daily_pnl_evidence=bundle.daily_pnl_evidence,
    )


def _settlement(
    *,
    sequence: int,
    previous: str | None,
    available_at: datetime,
    net: Decimal,
    identity: str,
) -> AlpacaPaperCycleSettlement:
    fee = Decimal("0.02")
    gross = net + fee
    row = AlpacaPaperCycleSettlement(
        settlement_sha256="0" * 64,
        settlement_schema_version=SETTLEMENT_SCHEMA_VERSION,
        settlement_authority_status="sealed_verified",
        reservation_id=uuid.UUID(int=sequence),
        decision_packet_sha256=_hash(f"packet:{sequence}"),
        reservation_request_sha256=_hash(f"request:{sequence}"),
        account_scope="alpaca:paper",
        account_identity_sha256=identity,
        account_snapshot_sha256=_hash(f"snapshot:{sequence}"),
        broker_connection_generation="alpaca-paper-connection:7",
        execution_family="alpaca_spot",
        broker_environment="paper",
        position_direction="long",
        symbol="VEEE",
        trading_date=available_at.date(),
        setup_family="momentum_breakout",
        terminal_sequence=sequence,
        previous_account_settlement_sha256=previous,
        source_fill_count=2,
        terminal_fill_sequence=2,
        terminal_fill_event_sha256=_hash(f"fill:{sequence}"),
        fill_chain_root_sha256=_hash(f"fill-root:{sequence}"),
        flat_evidence_sha256=_hash(f"flat:{sequence}"),
        capture_authority_status="verified",
        capture_authority_receipt_sha256=_hash(f"capture:{sequence}"),
        provider_event_clock_status="authoritative",
        provider_client_order_id_status="authoritative",
        exit_order_ownership_status="authoritative",
        fee_status="authoritative",
        fee_evidence_root_sha256=_hash(f"fee:{sequence}"),
        entry_quantity=Decimal("10"),
        exit_quantity=Decimal("10"),
        entry_cost_usd=Decimal("25"),
        exit_proceeds_usd=Decimal("25") + gross,
        gross_realized_pnl_usd=gross,
        fee_usd=fee,
        net_realized_pnl_usd=net,
        settlement_policy_sha256=_hash("settlement-policy"),
        effective_config_sha256=_hash("config"),
        code_build_sha256=_hash("build"),
        feature_flags_sha256=_hash("flags"),
        settlement_content_canonical_json="{}",
        settlement_content_sha256="0" * 64,
        closed_observed_at=available_at,
        closed_available_at=available_at,
    )
    canonical = json.dumps(
        cycle_settlement_content_payload(row),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    row.settlement_content_canonical_json = canonical
    row.settlement_content_sha256 = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    row.settlement_sha256 = hashlib.sha256(
        (
            f"{SETTLEMENT_HASH_DOMAIN}|{previous or 'genesis'}|"
            f"{row.settlement_content_sha256}"
        ).encode("utf-8")
    ).hexdigest()
    return row


def _rehash_settlement(
    row: AlpacaPaperCycleSettlement,
    *,
    previous: str | None,
) -> None:
    row.previous_account_settlement_sha256 = previous
    canonical = json.dumps(
        cycle_settlement_content_payload(row),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    row.settlement_content_canonical_json = canonical
    row.settlement_content_sha256 = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    row.settlement_sha256 = hashlib.sha256(
        (
            f"{SETTLEMENT_HASH_DOMAIN}|{previous or 'genesis'}|"
            f"{row.settlement_content_sha256}"
        ).encode("utf-8")
    ).hexdigest()


def _terminal_fill(
    settlement: AlpacaPaperCycleSettlement,
    *,
    provider_execution_at: datetime,
    fill_available_at: datetime,
) -> AlpacaPaperFillActivity:
    cycle = AlpacaPaperFillCycleBinding(
        reservation_id=settlement.reservation_id,
        decision_packet_sha256=settlement.decision_packet_sha256,
        reservation_request_sha256=settlement.reservation_request_sha256,
        account_scope="alpaca:paper",
        account_identity_sha256=settlement.account_identity_sha256,
        account_snapshot_sha256=settlement.account_snapshot_sha256,
        account_snapshot_generation=f"account-generation:{settlement.terminal_sequence}",
        broker_connection_generation=settlement.broker_connection_generation,
        execution_family="alpaca_spot",
        position_direction="long",
        cycle_client_order_id=f"entry-cid:{settlement.terminal_sequence}",
        entry_provider_order_id=f"entry-order:{settlement.terminal_sequence}",
        symbol=settlement.symbol,
    )
    exit_order_id = f"exit-order:{settlement.terminal_sequence}"
    exit_cid = f"exit-cid:{settlement.terminal_sequence}"
    activity = {
        "id": f"exit-fill:{settlement.terminal_sequence}",
        "account_id": ACCOUNT_ID,
        "activity_type": "FILL",
        "transaction_time": provider_execution_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "type": "fill",
        "price": "2.5000000000",
        "qty": "10.0000000000",
        "side": "sell",
        "symbol": settlement.symbol,
        "leaves_qty": "0.0000000000",
        "order_id": exit_order_id,
        "cum_qty": "10.0000000000",
        "order_status": "filled",
    }
    provider_order = {
        "id": exit_order_id,
        "client_order_id": exit_cid,
        "account_id": ACCOUNT_ID,
        "asset_class": "us_equity",
        "symbol": settlement.symbol,
        "side": "sell",
        "status": "filled",
    }
    fee_evidence = {
        "schema_version": "chili.alpaca-paper-equity-fee-contract.v1",
        "provider_activity_id": activity["id"],
        "provider_order_id": exit_order_id,
        "fee_usd": "0.0000000000",
        "currency": "USD",
        "broker_environment": "paper",
        "asset_class": "us_equity",
        "basis": "alpaca_paper_does_not_account_for_regulatory_fees",
        "source": "https://docs.alpaca.markets/us/docs/paper-trading",
    }
    prepared = prepare_verified_alpaca_paper_fill_activity(
        cycle,
        provider_activity=activity,
        provider_order=provider_order,
        received_at=fill_available_at - timedelta(microseconds=1),
        available_at=fill_available_at,
        expected_exit_client_order_id=exit_cid,
        fee_usd="0.0000000000",
        fee_evidence=fee_evidence,
    )
    return AlpacaPaperFillActivity(
        **prepared.model_kwargs(
            sequence=int(settlement.terminal_fill_sequence),
            previous_event_sha256=_hash(
                f"entry-fill:{settlement.terminal_sequence}"
            ),
        )
    )


def _terminal_observation_authority(
    settlement: AlpacaPaperCycleSettlement,
    terminal_fill: AlpacaPaperFillActivity,
):
    observation_sha256 = _hash(
        f"terminal-observation:{settlement.terminal_sequence}"
    )
    observation = AlpacaPaperFillQueryObservation(
        observation_sha256=observation_sha256,
        reservation_id=settlement.reservation_id,
        query_receipt_sha256=_hash(
            f"terminal-query:{settlement.terminal_sequence}"
        ),
        read_binding_sha256=_hash(
            f"terminal-binding:{settlement.terminal_sequence}"
        ),
        adapter_connection_generation=settlement.broker_connection_generation,
        adapter_build_sha256=_hash("alpaca-adapter-build"),
        available_at=terminal_fill.available_at,
    )
    mapping = AlpacaPaperFillObservationActivity(
        observation_sha256=observation_sha256,
        activity_ordinal=0,
        fill_event_sha256=terminal_fill.event_sha256,
        immutable_fill_identity_sha256=(
            terminal_fill.immutable_fill_identity_sha256
        ),
        provider_activity_id=terminal_fill.provider_activity_id,
        provider_payload_sha256=terminal_fill.provider_payload_sha256,
        mapping_sha256=_hash(
            f"terminal-mapping:{settlement.terminal_sequence}"
        ),
    )
    receipt = prepare_alpaca_paper_terminal_fill_observation_receipt(
        settlement=settlement,
        observation=observation,
        terminal_fill=terminal_fill,
        mapping=mapping,
    )
    return receipt, observation, mapping


def test_et_day_uses_terminal_execution_not_delayed_poll_or_packet_date() -> None:
    identity = alpaca_paper_account_identity_sha256(ACCOUNT_ID)
    # The first exit executes on the prior ET day but is not available until
    # after midnight.  A delayed poll must not move its P&L into the new day.
    prior = _settlement(
        sequence=1,
        previous=None,
        available_at=datetime(2026, 7, 15, 4, 2, tzinfo=UTC),
        net=Decimal("7.50"),
        identity=identity,
    )
    current = _settlement(
        sequence=2,
        previous=prior.settlement_sha256,
        available_at=datetime(2026, 7, 15, 4, 3, tzinfo=UTC),
        net=Decimal("-2.25"),
        identity=identity,
    )
    prior_fill = _terminal_fill(
        prior,
        provider_execution_at=datetime(2026, 7, 15, 3, 59, tzinfo=UTC),
        fill_available_at=datetime(2026, 7, 15, 4, 1, tzinfo=UTC),
    )
    current_fill = _terminal_fill(
        current,
        provider_execution_at=datetime(2026, 7, 15, 4, 1, tzinfo=UTC),
        fill_available_at=datetime(2026, 7, 15, 4, 2, tzinfo=UTC),
    )
    prior.terminal_fill_event_sha256 = prior_fill.event_sha256
    prior.fill_chain_root_sha256 = prior_fill.event_sha256
    current.terminal_fill_event_sha256 = current_fill.event_sha256
    current.fill_chain_root_sha256 = current_fill.event_sha256
    # Deliberately contaminate packet-era trading dates; daily P&L must ignore it.
    prior.trading_date = date(2030, 1, 1)
    current.trading_date = date(2000, 1, 1)
    _rehash_settlement(prior, previous=None)
    _rehash_settlement(current, previous=prior.settlement_sha256)

    head = new_zero_settlement_head(account_identity_sha256=identity)
    head.settled_cycle_sequence = 2
    head.last_settlement_sha256 = current.settlement_sha256
    head.cumulative_gross_realized_pnl_usd = Decimal("5.29")
    head.cumulative_fee_usd = Decimal("0.04")
    head.cumulative_net_realized_pnl_usd = Decimal("5.25")
    head.version = 3
    head.last_settled_at = current.closed_available_at
    head.head_content_sha256 = settlement_head_content_sha256(head)
    prior_receipt, prior_observation, prior_mapping = (
        _terminal_observation_authority(prior, prior_fill)
    )
    current_receipt, current_observation, current_mapping = (
        _terminal_observation_authority(current, current_fill)
    )

    evidence, terminal_authority_sha256 = (
        _derive_verified_alpaca_paper_daily_pnl_evidence(
        head=head,
        settlements=[prior, current],
        terminal_fills={
            prior_fill.event_sha256: prior_fill,
            current_fill.event_sha256: current_fill,
        },
        terminal_observation_receipts={
            prior.settlement_sha256: prior_receipt,
            current.settlement_sha256: current_receipt,
        },
        fill_observations={
            prior_observation.observation_sha256: prior_observation,
            current_observation.observation_sha256: current_observation,
        },
        fill_observation_mappings={
            (prior_mapping.observation_sha256, prior_mapping.fill_event_sha256): (
                prior_mapping
            ),
            (
                current_mapping.observation_sha256,
                current_mapping.fill_event_sha256,
            ): current_mapping,
        },
        decision_as_of=datetime(2026, 7, 15, 15, 0, tzinfo=UTC),
    )
    )
    assert evidence.risk_date_et == date(2026, 7, 15)
    assert evidence.local_realized_pnl_usd == Decimal("-2.2500000000")
    assert evidence.included_day_settlement_sha256s == (
        current.settlement_sha256,
    )
    assert len(terminal_authority_sha256) == 64


def test_settled_daily_evidence_is_exact_and_content_addressed() -> None:
    evidence = AlpacaPaperSettledDailyPnlEvidence.create(
        account_identity_sha256=_hash("account:evidence"),
        risk_date_et=date(2026, 7, 15),
        decision_as_of=datetime(2026, 7, 15, 15, 0, tzinfo=UTC),
        local_realized_pnl_usd=Decimal("-1711.2200000000"),
        settlement_head_content_sha256=_hash("head"),
        settlement_head_sequence=0,
        settlement_head_tail_sha256=None,
        included_day_settlement_sha256s=(),
    )
    assert isinstance(evidence.local_realized_pnl_usd, Decimal)
    with pytest.raises(
        AlpacaCycleSettlementIntegrityError,
        match="content hash changed",
    ):
        replace(evidence, local_realized_pnl_usd=Decimal("999"))


def test_fall_dst_repeated_hour_is_same_et_day_and_derivation_is_replayable() -> None:
    identity = alpaca_paper_account_identity_sha256(ACCOUNT_ID)
    first = _settlement(
        sequence=1,
        previous=None,
        available_at=datetime(2026, 11, 1, 6, 0, tzinfo=UTC),
        net=Decimal("3.00"),
        identity=identity,
    )
    second = _settlement(
        sequence=2,
        previous=first.settlement_sha256,
        available_at=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        net=Decimal("4.00"),
        identity=identity,
    )
    first_fill = _terminal_fill(
        first,
        provider_execution_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
        fill_available_at=datetime(2026, 11, 1, 5, 31, tzinfo=UTC),
    )
    second_fill = _terminal_fill(
        second,
        provider_execution_at=datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
        fill_available_at=datetime(2026, 11, 1, 6, 31, tzinfo=UTC),
    )
    first.terminal_fill_event_sha256 = first_fill.event_sha256
    first.fill_chain_root_sha256 = first_fill.event_sha256
    second.terminal_fill_event_sha256 = second_fill.event_sha256
    second.fill_chain_root_sha256 = second_fill.event_sha256
    _rehash_settlement(first, previous=None)
    _rehash_settlement(second, previous=first.settlement_sha256)
    head = new_zero_settlement_head(account_identity_sha256=identity)
    head.settled_cycle_sequence = 2
    head.last_settlement_sha256 = second.settlement_sha256
    head.cumulative_gross_realized_pnl_usd = Decimal("7.04")
    head.cumulative_fee_usd = Decimal("0.04")
    head.cumulative_net_realized_pnl_usd = Decimal("7.00")
    head.version = 3
    head.last_settled_at = second.closed_available_at
    head.head_content_sha256 = settlement_head_content_sha256(head)
    first_receipt, first_observation, first_mapping = (
        _terminal_observation_authority(first, first_fill)
    )
    second_receipt, second_observation, second_mapping = (
        _terminal_observation_authority(second, second_fill)
    )
    kwargs = {
        "head": head,
        "settlements": [first, second],
        "terminal_fills": {
            first_fill.event_sha256: first_fill,
            second_fill.event_sha256: second_fill,
        },
        "terminal_observation_receipts": {
            first.settlement_sha256: first_receipt,
            second.settlement_sha256: second_receipt,
        },
        "fill_observations": {
            first_observation.observation_sha256: first_observation,
            second_observation.observation_sha256: second_observation,
        },
        "fill_observation_mappings": {
            (first_mapping.observation_sha256, first_mapping.fill_event_sha256): (
                first_mapping
            ),
            (
                second_mapping.observation_sha256,
                second_mapping.fill_event_sha256,
            ): second_mapping,
        },
        "decision_as_of": datetime(2026, 11, 1, 15, 0, tzinfo=UTC),
    }

    live = _derive_verified_alpaca_paper_daily_pnl_evidence(**kwargs)
    replay = _derive_verified_alpaca_paper_daily_pnl_evidence(**kwargs)

    assert live == replay
    assert live[0].risk_date_et == date(2026, 11, 1)
    assert live[0].local_realized_pnl_usd == Decimal("7.0000000000")


def test_locked_bundle_derives_zero_daily_pnl_and_issues_db_clock(db) -> None:
    store = AdaptiveRiskReservationStore(db.get_bind())
    decision_id = "decision-locked-zero"

    with db.begin():
        bundle = store.lock_alpaca_paper_admission_bundle(
            broker_account_facts=_broker_facts(decision_id=decision_id),
            symbol="VEEE",
            correlation_cluster="equity:momentum",
            session=db,
        )

    assert bundle.account_snapshot.local_realized_pnl_usd == 0.0
    assert bundle.decision_as_of > bundle.account_snapshot.available_at
    assert bundle.decision_as_of <= bundle.attestation.expires_at
    assert bundle.daily_pnl_evidence != bundle.account_evidence
    assert bundle.daily_pnl_evidence.content_sha256 != (
        bundle.settled_daily_pnl_evidence.evidence_sha256
    )
    assert len(bundle.daily_terminal_fill_authority_sha256) == 64
    assert bundle.account_snapshot.pending_policy_buying_power_reflected_usd == 0
    assert bundle.locked_risk_snapshot.aggregates[
        "pending_buying_power_impact_usd"
    ] == 0
    assert bundle.locked_risk_snapshot.policy_buying_power_capacity_usd == (
        bundle.account_snapshot.buying_power_usd
        + bundle.locked_risk_snapshot.aggregates[
            "open_buying_power_impact_usd"
        ]
    )
    assert bundle.locked_risk_snapshot.observed_at == bundle.decision_as_of
    assert bundle.attestation.bundle_sha256 == bundle.bundle_sha256

    with pytest.raises(AdaptiveRiskContractError, match="attestation changed"):
        replace(bundle.attestation, decision_id="forged")


def test_locked_bundle_is_required_and_consumed_in_same_transaction(db) -> None:
    store = AdaptiveRiskReservationStore(db.get_bind())
    decision_id = "decision-locked-reserve"
    with db.begin():
        bundle = store.lock_alpaca_paper_admission_bundle(
            broker_account_facts=_broker_facts(decision_id=decision_id),
            symbol="VEEE",
            correlation_cluster="equity:momentum",
            session=db,
        )
        request = _request_for_bundle(bundle, decision_id=decision_id)
        resolution = resolve_adaptive_risk(request.policy, request.inputs)
        result = store.reserve(
            request,
            session=db,
            locked_alpaca_paper_bundle=bundle,
            prepared_resolution=resolution,
            prepared_decision_packet=resolution.to_decision_packet(),
        )
        assert result.admission_accepted is True
        assert result.reservation_id is not None

    # A process-valid object is not a transaction capability after commit.
    with db.begin():
        with pytest.raises(
            AdaptiveRiskContractError,
            match="current database transaction",
        ):
            store.reserve(
                request,
                session=db,
                locked_alpaca_paper_bundle=bundle,
                prepared_resolution=resolution,
                prepared_decision_packet=resolution.to_decision_packet(),
            )


def test_locked_bundle_expiry_is_checked_when_reservation_consumes_it(
    db,
    monkeypatch,
) -> None:
    store = AdaptiveRiskReservationStore(db.get_bind())
    decision_id = "decision-locked-expiry"
    with db.begin():
        bundle = store.lock_alpaca_paper_admission_bundle(
            broker_account_facts=_broker_facts(decision_id=decision_id),
            symbol="VEEE",
            correlation_cluster="equity:momentum",
            session=db,
        )
        request = _request_for_bundle(bundle, decision_id=decision_id)
        resolution = resolve_adaptive_risk(request.policy, request.inputs)
        monkeypatch.setattr(
            store,
            "_clock",
            lambda _session: bundle.attestation.expires_at
            + timedelta(microseconds=1),
        )
        with pytest.raises(
            AdaptiveRiskContractError,
            match="expired before reservation",
        ):
            store.reserve(
                request,
                session=db,
                locked_alpaca_paper_bundle=bundle,
                prepared_resolution=resolution,
                prepared_decision_packet=resolution.to_decision_packet(),
            )


def test_paper_raw_snapshot_path_is_closed_but_db_paper_remains_available(db) -> None:
    store = AdaptiveRiskReservationStore(db.get_bind())
    observed = datetime.now(tz=UTC) - timedelta(seconds=1)
    paper_snapshot = ImmutableAccountRiskSnapshot(
        snapshot_id="caller-supplied-paper",
        source="forged",
        provider_generation="forged:1",
        account_scope="alpaca:paper",
        execution_family="alpaca_spot",
        broker_environment="paper",
        venue="alpaca",
        account_identity_sha256=_hash("caller-paper"),
        observed_at=observed,
        available_at=observed,
        equity_usd=100_000,
        buying_power_usd=400_000,
        broker_day_change_usd=0,
        local_realized_pnl_usd=999_999,
        pending_policy_buying_power_reflected_usd=0,
    )
    with db.begin():
        with pytest.raises(
            AdaptiveRiskContractError,
            match="lock_alpaca_paper_admission_bundle",
        ):
            store.lock_admission_snapshot(
                account_scope="alpaca:paper",
                symbol="VEEE",
                correlation_cluster="equity:momentum",
                account_snapshot=paper_snapshot,
                session=db,
            )

    db_snapshot = replace(
        paper_snapshot,
        snapshot_id="db-paper-account",
        source="postgresql:test",
        provider_generation="db-paper:test:1",
        account_scope="db-paper:test-authority",
        execution_family="db_paper",
        broker_environment="paper",
        venue="internal",
        local_realized_pnl_usd=12.5,
    )
    with db.begin():
        locked = store.lock_admission_snapshot(
            account_scope="db-paper:test-authority",
            symbol="VEEE",
            correlation_cluster="equity:momentum",
            account_snapshot=db_snapshot,
            session=db,
        )
    assert locked.account_scope == "db-paper:test-authority"


def test_broker_account_facts_have_no_caller_daily_pnl_field() -> None:
    signature = inspect.signature(AlpacaPaperBrokerAccountFacts)
    assert "local_realized_pnl_usd" not in signature.parameters
    assert "pending_policy_buying_power_reflected_usd" not in signature.parameters
    assert "account_evidence" not in signature.parameters
    assert "capture_authority" in signature.parameters


def test_plain_or_fabricated_broker_facts_cannot_reach_locked_bundle(db) -> None:
    with pytest.raises(
        (TypeError, AdaptiveRiskContractError),
        match="capture_authority|capture-issued authority",
    ):
        AlpacaPaperBrokerAccountFacts(
            snapshot_id="fabricated",
            source="alpaca_trading_paper",
            provider_generation="fabricated:1",
            account_identity_sha256=_hash("fabricated-account"),
            observed_at=datetime.now(tz=UTC),
            available_at=datetime.now(tz=UTC),
            equity_usd=100_000,
            buying_power_usd=400_000,
            broker_day_change_usd=0,
        )

    facts = _broker_facts(decision_id="decision-no-inflation")
    assert facts.account_evidence.content_sha256 == (
        facts.capture_authority.account_payload_sha256
    )
    with pytest.raises(AdaptiveRiskContractError, match="differ from capture"):
        replace(facts, buying_power_usd=facts.buying_power_usd + 1_000_000)
