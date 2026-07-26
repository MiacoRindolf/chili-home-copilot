from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import threading
import uuid
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.db import engine
from app.models.trading import (
    AdaptiveRiskDecisionPacket,
    AdaptiveRiskOpportunityClaim,
    AdaptiveRiskOpportunityEvent,
    AdaptiveRiskReservation,
    AdaptiveRiskReservationEvent,
    AlpacaPaperAccountSettlementHead,
    MomentumStrategyVariant,
    TradingAutomationSession,
    TradingAutomationSimulatedFill,
)
from app.services.trading.momentum_neural.adaptive_risk_policy import (
    AdaptiveRiskContractError,
    AdaptiveRiskInputs,
    AdaptiveRiskPolicy,
    RiskInputEvidence,
)
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    AdaptiveExitOwnerTransportBinding,
    AdaptiveReservationIdempotencyConflict,
    AdaptiveReservationStateConflict,
    AdaptiveRiskExposureQuarantined,
    AdaptiveRiskReservationRequest,
    AdaptiveRiskReservationStore,
    canonical_db_paper_fill_content_sha256,
    DurableOrderLifecycleEvidence,
    DurableSubmitAttemptEvidence,
    ImmutableAccountRiskSnapshot,
    load_adaptive_risk_reservation_request,
)
from app.services.trading.momentum_neural.alpaca_cycle_settlement import (
    new_zero_settlement_head,
)
from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    acquire_action_claim,
    advance_owner_transport,
    lease_owner_transport,
    read_action_claim,
)


UTC = timezone.utc
ET = ZoneInfo("America/New_York")
def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _policy(**overrides) -> AdaptiveRiskPolicy:
    values = {
        "policy_version": "adaptive-reservation-test-v1",
        "policy_source": "focused-db-fixture",
        "risk_fraction_of_equity": 0.01,
        "daily_risk_fraction_of_equity": 0.10,
        "portfolio_risk_fraction_of_equity": 0.05,
        "cluster_risk_fraction_of_equity": 0.04,
        "symbol_risk_fraction_of_equity": 0.03,
        "daily_gap_reserve_fraction_of_equity": 0.001,
        "max_notional_fraction_of_equity": 0.80,
        "max_buying_power_fraction_for_notional": 0.50,
        "max_portfolio_gross_fraction_of_equity": 2.0,
        "quality_multiplier_floor": 0.50,
        "quality_multiplier_ceiling": 1.50,
        "volatility_reference_fraction": 0.05,
        "volatility_multiplier_floor": 0.40,
        "spread_reserve_multiple": 1.0,
        "per_share_gap_reserve_volatility_multiple": 0.10,
        "max_adv_participation": 0.02,
        "max_recent_volume_participation": 0.10,
        "max_executable_depth_participation": 0.50,
        "market_data_max_age_seconds": 30.0,
        "account_data_max_age_seconds": 30.0,
        # The focused suite executes real PostgreSQL locks/migrations and can
        # spend more than 250 ms between fixture capture and the DB decision
        # clock on a loaded workstation.  Keep the freshness gate meaningful
        # without making valid-path tests scheduler-sensitive; dedicated stale
        # cases below still prove fail-closed behavior.
        "reservation_data_max_age_seconds": 30.0,
        "context_data_max_age_seconds": 60.0,
    }
    values.update(overrides)
    return AdaptiveRiskPolicy(**values)


def _snapshot(
    *,
    family: str = "alpaca_spot",
    environment: str = "paper",
    account_scope: str = "alpaca:paper:acct-A",
    pending_reflected: float = 0.0,
) -> ImmutableAccountRiskSnapshot:
    now = datetime.now(UTC)
    return ImmutableAccountRiskSnapshot(
        snapshot_id="alpaca-account-clock-101",
        source="alpaca:account-v2",
        provider_generation="alpaca-paper-generation-101",
        account_scope=account_scope,
        execution_family=family,
        broker_environment=environment,
        venue="alpaca",
        account_identity_sha256=_hash("alpaca-paper-account-A"),
        observed_at=now - timedelta(milliseconds=40),
        available_at=now - timedelta(milliseconds=20),
        equity_usd=100_000.0,
        buying_power_usd=400_000.0,
        broker_day_change_usd=0.0,
        local_realized_pnl_usd=0.0,
        pending_policy_buying_power_reflected_usd=pending_reflected,
    )


def _evidence(name: str, *, available_at: datetime | None = None) -> RiskInputEvidence:
    available = available_at or (datetime.now(UTC) - timedelta(milliseconds=20))
    return RiskInputEvidence(
        source=f"fixture:{name}",
        observed_at=available - timedelta(milliseconds=2),
        available_at=available,
        content_sha256=_hash(f"fixture:{name}"),
        provider_generation="fixture-generation-101",
    )


def _inputs(
    snapshot: ImmutableAccountRiskSnapshot,
    *,
    symbol: str,
    decision_id: str,
    cluster: str,
    surface: str = "alpaca_paper",
    evidence_overrides: dict[str, RiskInputEvidence] | None = None,
) -> AdaptiveRiskInputs:
    decision_at = datetime.now(UTC)
    evidence = {
        name: _evidence(name)
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
            "reservation_ledger",
        )
    }
    account_evidence = RiskInputEvidence(
        source=snapshot.source,
        observed_at=snapshot.observed_at,
        available_at=snapshot.available_at,
        content_sha256=snapshot.snapshot_sha256,
        provider_generation=snapshot.provider_generation,
    )
    evidence["account"] = account_evidence
    evidence["daily_pnl"] = account_evidence
    evidence.update(evidence_overrides or {})
    return AdaptiveRiskInputs(
        decision_id=decision_id,
        replay_or_paper_run_id=str(uuid.uuid5(uuid.NAMESPACE_DNS, decision_id)),
        generation=101,
        execution_surface=surface,
        execution_family=snapshot.execution_family,
        venue=snapshot.venue,
        broker_environment=snapshot.broker_environment,
        symbol=symbol,
        side="long",
        as_of=decision_at,
        account_identity_sha256=snapshot.account_identity_sha256,
        code_build_sha256=_hash("build-101"),
        effective_config_sha256=_hash("config-101"),
        feature_flags_sha256=_hash("flags-101"),
        capture_prefix_root_sha256=_hash("capture-prefix-101"),
        equity_usd=snapshot.equity_usd,
        buying_power_usd=snapshot.buying_power_usd,
        broker_day_change_usd=snapshot.broker_day_change_usd,
        local_realized_pnl_usd=snapshot.local_realized_pnl_usd,
        open_structural_risk_usd=999_999.0,  # ignored; DB ledger is authoritative
        pending_reserved_risk_usd=999_999.0,
        existing_same_symbol_structural_risk_usd=999_999.0,
        pending_same_symbol_structural_risk_usd=999_999.0,
        current_cluster_structural_risk_usd=999_999.0,
        pending_correlation_cluster_risk_usd=999_999.0,
        portfolio_gross_notional_usd=999_999.0,
        pending_portfolio_gross_notional_usd=999_999.0,
        policy_buying_power_capacity_usd=1.0,
        open_buying_power_impact_usd=999_999.0,
        pending_buying_power_impact_usd=999_999.0,
        candidate_buying_power_impact_per_share_usd=10.0,
        bid=9.99,
        ask=10.00,
        structural_stop=9.50,
        entry_slippage_bps=10.0,
        exit_slippage_bps=20.0,
        fees_per_share_usd=0.005,
        setup_quality=0.80,
        realized_volatility_fraction=0.05,
        average_daily_volume_shares=5_000_000.0,
        recent_volume_shares=500_000.0,
        executable_depth_shares=100_000.0,
        correlation_cluster_id=cluster,
        evidence=evidence,
    )


def _request(
    *,
    symbol: str = "VEEE",
    decision_id: str = "veee-entry-1",
    client_order_id: str = "chili-veee-entry-1",
    setup_family: str = "first_dip_reclaim",
    cluster: str = "equity:momentum-a",
    snapshot: ImmutableAccountRiskSnapshot | None = None,
    inputs: AdaptiveRiskInputs | None = None,
) -> AdaptiveRiskReservationRequest:
    account = snapshot or _snapshot()
    risk_inputs = inputs or _inputs(
        account,
        symbol=symbol,
        decision_id=decision_id,
        cluster=cluster,
    )
    return AdaptiveRiskReservationRequest(
        policy=_policy(),
        inputs=risk_inputs,
        account_snapshot=account,
        account_scope=account.account_scope,
        setup_family=setup_family,
        correlation_cluster=cluster,
        client_order_id=client_order_id,
        entry_limit_price=10.00,
        opportunity_key=(
            {
                "account_scope": account.account_scope,
                "symbol": risk_inputs.symbol,
                "trading_date": risk_inputs.as_of.astimezone(ET).date().isoformat(),
                "setup_family": setup_family,
            }
            if setup_family == "first_dip_reclaim"
            else None
        ),
    )


def _exit_owner_binding(
    request: AdaptiveRiskReservationRequest,
    *,
    reservation_id: uuid.UUID,
    decision_packet_sha256: str,
    exit_client_order_id: str | None = None,
    transport_lease_id: str | None = None,
    owner_generation: int = 1,
    owner_session_id: int = 7001,
    owner_kind: str = "ordinary_exit",
) -> AdaptiveExitOwnerTransportBinding:
    exit_cid = exit_client_order_id or (
        f"exit-{request.client_order_id}-{owner_generation}"
    )
    account_id = "3e0776af-76cd-4afd-8fe1-f2ee8dc6242f"
    return AdaptiveExitOwnerTransportBinding(
        reservation_id=reservation_id,
        expected_account_id=account_id,
        account_identity_sha256=request.inputs.account_identity_sha256,
        decision_packet_sha256=decision_packet_sha256,
        symbol=request.inputs.symbol,
        entry_client_order_id=request.client_order_id,
        exit_client_order_id=exit_cid,
        order_request={
            "account_scope": "alpaca:paper",
            "alpaca_account_id": account_id,
            "asset_class": "us_equity",
            "base_size": 1.0,
            "client_order_id": exit_cid,
            "order_type": "market",
            "position_intent": "sell_to_close",
            "product_id": request.inputs.symbol,
            "side": "sell",
            "time_in_force": "day",
        },
        transport_claim_token=f"claim-{reservation_id}",
        transport_owner_session_id=owner_session_id,
        transport_owner_generation=owner_generation,
        transport_owner_kind=owner_kind,
        transport_lease_id=(
            transport_lease_id or f"lease-{reservation_id}-{owner_generation}"
        ),
        transport_runtime_generation="paper-runtime-generation-501",
        transport_connection_generation="alpaca-paper-connection-501",
    )


def _reserve_filled_exit_owner_fixture(
    db,
    *,
    label: str,
    account_identity_sha256: str | None = None,
    surface: str = "db_paper",
):
    snapshot = _snapshot(account_scope="alpaca:paper")
    if account_identity_sha256 is not None:
        snapshot = replace(
            snapshot,
            account_identity_sha256=account_identity_sha256,
        )
    inputs = _inputs(
        snapshot,
        symbol=label[:4].upper(),
        decision_id=f"{label}-decision",
        cluster=f"equity:{label}",
        surface=surface,
    )
    request = _request(
        symbol=label[:4].upper(),
        decision_id=f"{label}-decision",
        client_order_id=f"{label}-entry-cid",
        setup_family="gap_and_go",
        cluster=f"equity:{label}",
        snapshot=snapshot,
        inputs=inputs,
    )
    if db.get(
        AlpacaPaperAccountSettlementHead,
        ("alpaca:paper", request.inputs.account_identity_sha256),
    ) is None:
        db.add(
            new_zero_settlement_head(
                account_identity_sha256=request.inputs.account_identity_sha256
            )
        )
    db.commit()
    store = AdaptiveRiskReservationStore(engine)
    decision = store.reserve(request)
    variant = MomentumStrategyVariant(
        family="db_paper_exit_owner_fixture",
        variant_key=uuid.uuid4().hex,
        version=1,
        label="DB paper exit-owner fixture",
        params_json={},
        execution_family="alpaca_spot",
    )
    db.add(variant)
    db.flush()
    automation_session = TradingAutomationSession(
        venue="alpaca",
        execution_family="alpaca_spot",
        mode="paper",
        symbol=request.inputs.symbol,
        variant_id=variant.id,
        state="entered",
        risk_snapshot_json={},
        allocation_decision_json={},
    )
    db.add(automation_session)
    db.flush()
    lifecycle_event_id = f"db-paper-owner-fill:{decision.reservation_id}"
    connection_generation = f"db-paper-owner-generation:{label}"
    fill = TradingAutomationSimulatedFill(
        session_id=automation_session.id,
        symbol=request.inputs.symbol,
        lane="simulation",
        side="long",
        action="enter_long",
        fill_type="entry",
        quantity=float(decision.quantity_shares),
        price=10.0,
        position_state_before="flat",
        position_state_after="long",
        reason="entry_fill",
        marker_json={
            "adaptive_risk_reservation_id": str(decision.reservation_id),
            "adaptive_risk_decision_packet_sha256": (
                decision.decision_packet_sha256
            ),
            "adaptive_risk_client_order_id": request.client_order_id,
            "adaptive_risk_account_scope": request.account_scope,
            "adaptive_risk_cumulative_fill_quantity": (
                decision.quantity_shares
            ),
            "adaptive_risk_lifecycle_event_id": lifecycle_event_id,
            "adaptive_risk_connection_generation": connection_generation,
        },
    )
    db.add(fill)
    db.commit()
    db.refresh(fill)
    filled = store.apply_cumulative_fill(
        decision.reservation_id,
        evidence=_lifecycle_evidence(
            request,
            event_kind="cumulative_fill",
            provider_event_id=lifecycle_event_id,
            cumulative=decision.quantity_shares,
            order_status="filled",
            connection_generation=connection_generation,
            durability_kind="committed_db_paper_fill",
            broker_source="db_paper",
            source_record_table="trading_automation_simulated_fills",
            source_record_id=str(fill.id),
            event_content_sha256=canonical_db_paper_fill_content_sha256(fill),
        ),
    )
    assert filled.state == "filled"
    return store, request, decision, _exit_owner_binding(
        request,
        reservation_id=decision.reservation_id,
        decision_packet_sha256=decision.decision_packet_sha256,
        owner_session_id=int(automation_session.id),
    )


def _acquire_exit_owner_claim(db, binding: AdaptiveExitOwnerTransportBinding):
    acquired = acquire_action_claim(
        db,
        symbol=binding.symbol,
        action="entry",
        claim_token=binding.transport_claim_token,
        owner_session_id=binding.transport_owner_session_id,
        client_order_id=binding.entry_client_order_id,
        metadata={
            "alpaca_account_id": binding.expected_account_id,
            "order_request": {
                "alpaca_account_id": binding.expected_account_id,
                "client_order_id": binding.entry_client_order_id,
                "product_id": binding.symbol,
                "side": "buy",
            },
        },
        account_scope=binding.account_scope,
    )
    assert acquired["ok"] is True
    db.commit()
    return {
        "symbol": binding.symbol,
        "claim_token": binding.transport_claim_token,
        "owner_session_id": binding.transport_owner_session_id,
        "account_scope": binding.account_scope,
        "alpaca_account_id": binding.expected_account_id,
    }


def _lease_exit_owner(
    db,
    store: AdaptiveRiskReservationStore,
    binding: AdaptiveExitOwnerTransportBinding,
    context: dict,
    *,
    event_at: datetime,
):
    return lease_owner_transport(
        db,
        **context,
        transport_kind=binding.transport_owner_kind,
        client_order_id=binding.exit_client_order_id,
        order_request=dict(binding.order_request),
        lease_token=binding.transport_lease_id,
        exit_owner_store=store,
        exit_owner_binding=binding,
        exit_owner_effective_at=event_at,
        exit_owner_available_at=event_at,
    )


def _lifecycle_evidence(
    request: AdaptiveRiskReservationRequest,
    *,
    event_kind: str,
    provider_event_id: str,
    cumulative: int,
    order_status: str,
    connection_generation: str = "alpaca-order-stream-101",
    observed_at: datetime | None = None,
    available_at: datetime | None = None,
    durability_kind: str = "authoritative_broker_event",
    broker_source: str | None = None,
    source_record_table: str = "alpaca_order_updates",
    source_record_id: str | None = None,
    event_content_sha256: str | None = None,
    remaining_open_quantity: int | None = None,
) -> DurableOrderLifecycleEvidence:
    available = available_at or datetime.now(UTC)
    observed = observed_at or (available - timedelta(milliseconds=1))
    return DurableOrderLifecycleEvidence(
        event_kind=event_kind,
        durability_kind=durability_kind,
        provider_event_id=provider_event_id,
        broker_source=(
            broker_source
            if broker_source is not None
            else request.account_snapshot.venue
        ),
        connection_generation=connection_generation,
        account_scope=request.account_scope,
        execution_family=request.inputs.execution_family,
        broker_environment=request.inputs.broker_environment,
        account_identity_sha256=request.inputs.account_identity_sha256,
        client_order_id=request.client_order_id,
        broker_order_id=f"broker-order:{request.client_order_id}",
        observed_at=observed,
        available_at=available,
        event_content_sha256=(
            event_content_sha256 or _hash(f"source-event:{provider_event_id}")
        ),
        cumulative_filled_quantity=cumulative,
        source_record_table=source_record_table,
        source_record_id=(source_record_id or provider_event_id),
        order_status=order_status,
        remaining_open_quantity=(
            0
            if event_kind == "position_flat" and remaining_open_quantity is None
            else remaining_open_quantity
        ),
    )


def _submit_attempt_evidence(
    request: AdaptiveRiskReservationRequest,
    *,
    attempt_event_id: str,
    broker_order_id: str | None = None,
    connection_generation: str = "alpaca-order-stream-101",
    observed_at: datetime | None = None,
    available_at: datetime | None = None,
) -> DurableSubmitAttemptEvidence:
    available = available_at or datetime.now(UTC)
    observed = observed_at or (available - timedelta(milliseconds=1))
    return DurableSubmitAttemptEvidence(
        attempt_event_id=attempt_event_id,
        broker_source=request.account_snapshot.venue,
        connection_generation=connection_generation,
        account_scope=request.account_scope,
        execution_family=request.inputs.execution_family,
        broker_environment=request.inputs.broker_environment,
        account_identity_sha256=request.inputs.account_identity_sha256,
        client_order_id=request.client_order_id,
        broker_order_id=broker_order_id,
        observed_at=observed,
        available_at=available,
        event_content_sha256=_hash(f"submit-attempt:{attempt_event_id}"),
        source_record_table="broker_transport_attempts",
        source_record_id=attempt_event_id,
    )
def test_atomic_reservation_persists_exact_packet_without_consuming_opportunity(db) -> None:
    store = AdaptiveRiskReservationStore(engine)
    request = _request()
    assert (
        load_adaptive_risk_reservation_request(request.to_payload()).request_sha256
        == request.request_sha256
    )

    first = store.reserve(request)
    retry = store.reserve(request)

    assert first.admission_accepted is True
    assert first.quantity_shares > 0
    assert retry.idempotent_retry is True
    assert retry.reservation_id == first.reservation_id
    db.expire_all()
    packet = db.get(AdaptiveRiskDecisionPacket, first.decision_packet_sha256)
    reservation = db.get(AdaptiveRiskReservation, first.reservation_id)
    opportunity = db.get(
        AdaptiveRiskOpportunityClaim, reservation.opportunity_claim_id
    )
    assert packet.account_snapshot_sha256 == request.account_snapshot.snapshot_sha256
    assert packet.reservation_request_sha256 == request.request_sha256
    assert packet.account_snapshot_json["execution_family"] == "alpaca_spot"
    assert packet.account_snapshot_json["broker_environment"] == "paper"
    assert packet.input_sha256 == packet.decision_packet_json["input_sha256"]
    assert packet.reservation_ledger_sha256 == (
        packet.decision_packet_json["input_snapshot"]["evidence"][
            "reservation_ledger"
        ]["content_sha256"]
    )
    assert (
        packet.decision_packet_json["input_snapshot"]["evidence"][
            "reservation_ledger"
        ]["available_at"]
        == packet.decision_packet_json["input_snapshot"]["as_of"]
    )
    assert reservation.pending_structural_risk_usd > 0
    assert reservation.open_structural_risk_usd == 0
    assert opportunity.status == "reserved"
    assert opportunity.consumed_by_reservation_id is None
    assert db.scalar(select(func.count(AdaptiveRiskReservationEvent.id))) == 1
    assert db.scalar(select(func.count(AdaptiveRiskOpportunityEvent.id))) == 1

    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(
            text(
                "UPDATE adaptive_risk_decision_packets "
                "SET admission_accepted = FALSE "
                "WHERE decision_packet_sha256 = :packet"
            ),
            {"packet": first.decision_packet_sha256},
        )
        db.commit()
    db.rollback()


def test_request_loader_rejects_intrinsic_account_evidence_snapshot_mismatch() -> None:
    request = _request()
    mismatched = replace(
        request,
        account_snapshot=replace(
            request.account_snapshot,
            provider_generation="different-account-generation",
        ),
    )

    with pytest.raises(
        AdaptiveRiskContractError,
        match="account_snapshot_evidence_mismatch:account:generation",
    ):
        load_adaptive_risk_reservation_request(mismatched.to_payload())


def test_replay_surface_cannot_enter_wall_clock_reservation_store(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay uses its sealed ledger/pure resolver, never current PostgreSQL time."""

    snapshot = _snapshot(account_scope="replay:sealed:run-1")
    inputs = _inputs(
        snapshot,
        symbol="VEEE",
        decision_id="sealed-replay-wall-clock-guard",
        cluster="equity:momentum-v",
        surface="replay",
    )
    request = _request(
        symbol="VEEE",
        decision_id=inputs.decision_id,
        client_order_id="sealed-replay-wall-clock-guard-cid",
        setup_family="momentum_pullback",
        cluster=inputs.correlation_cluster_id,
        snapshot=snapshot,
        inputs=inputs,
    )
    touched: list[str] = []

    def _forbidden(*_args, **_kwargs):
        touched.append("mutable_store")
        raise AssertionError("replay reached mutable reservation authority")

    monkeypatch.setattr(
        AdaptiveRiskReservationStore,
        "_lock_account",
        staticmethod(_forbidden),
    )
    monkeypatch.setattr(
        AdaptiveRiskReservationStore,
        "_clock",
        staticmethod(_forbidden),
    )

    with pytest.raises(
        AdaptiveRiskContractError,
        match="does not accept execution surface: replay",
    ):
        AdaptiveRiskReservationStore(engine).reserve(request)

    assert touched == []
    assert (
        db.scalar(
            select(func.count(AdaptiveRiskDecisionPacket.decision_packet_sha256))
        )
        == 0
    )
    assert db.scalar(select(func.count(AdaptiveRiskReservation.reservation_id))) == 0


def test_first_dip_opportunity_key_is_canonical_hash_bound_and_required() -> None:
    request = _request()
    payload = request.to_payload()

    assert payload["opportunity_key"] == request.opportunity_key.to_payload()
    assert payload["opportunity_key_sha256"] == request.opportunity_key.key_sha256
    loaded = load_adaptive_risk_reservation_request(payload)
    assert loaded.opportunity_key == request.opportunity_key
    assert loaded.request_sha256 == request.request_sha256

    tampered = {
        **payload,
        "opportunity_key": {
            **payload["opportunity_key"],
            "symbol": "PLSM",
        },
    }
    with pytest.raises(AdaptiveRiskContractError, match="opportunity_key hash mismatch"):
        load_adaptive_risk_reservation_request(tampered)

    with pytest.raises(
        AdaptiveRiskContractError,
        match="first_dip_reclaim requires a captured opportunity_key",
    ):
        replace(request, opportunity_key=None)

    wrong_date = request.opportunity_key.trading_date + timedelta(days=1)
    with pytest.raises(
        AdaptiveRiskContractError,
        match="opportunity_key does not match captured decision: trading_date",
    ):
        replace(
            request,
            opportunity_key={
                **request.opportunity_key.to_payload(),
                "trading_date": wrong_date.isoformat(),
            },
        )


def test_first_dip_reservation_uses_captured_et_date_across_db_boundary_and_reuses(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_at = datetime(2026, 7, 14, 3, 59, 59, 500000, tzinfo=UTC)
    database_at = datetime(2026, 7, 14, 4, 0, 0, 500000, tzinfo=UTC)
    assert captured_at.astimezone(ET).date().isoformat() == "2026-07-13"
    assert database_at.astimezone(ET).date().isoformat() == "2026-07-14"

    account = replace(
        _snapshot(),
        observed_at=captured_at - timedelta(milliseconds=40),
        available_at=captured_at - timedelta(milliseconds=20),
    )
    inputs = _inputs(
        account,
        symbol="VEEE",
        decision_id="captured-et-boundary-1",
        cluster="equity:momentum-v",
    )
    account_evidence = RiskInputEvidence(
        source=account.source,
        observed_at=account.observed_at,
        available_at=account.available_at,
        content_sha256=account.snapshot_sha256,
        provider_generation=account.provider_generation,
    )
    evidence = {
        name: (
            account_evidence
            if name in {"account", "daily_pnl"}
            else replace(
                value,
                observed_at=captured_at - timedelta(milliseconds=12),
                available_at=captured_at - timedelta(milliseconds=10),
            )
        )
        for name, value in inputs.evidence.items()
    }
    inputs = replace(inputs, as_of=captured_at, evidence=evidence)
    request = _request(
        decision_id=inputs.decision_id,
        client_order_id="captured-et-boundary-cid-1",
        cluster=inputs.correlation_cluster_id,
        snapshot=account,
        inputs=inputs,
    )
    assert request.opportunity_key.trading_date.isoformat() == "2026-07-13"

    monkeypatch.setattr(
        AdaptiveRiskReservationStore,
        "_clock",
        staticmethod(lambda _session: database_at),
    )
    store = AdaptiveRiskReservationStore(engine)
    first = store.reserve(request)
    assert first.admission_accepted is True, first.rejection_reasons
    assert first.trading_date.isoformat() == "2026-07-13"

    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, first.reservation_id)
    opportunity = db.get(
        AdaptiveRiskOpportunityClaim, reservation.opportunity_claim_id
    )
    event = db.scalar(
        select(AdaptiveRiskOpportunityEvent).where(
            AdaptiveRiskOpportunityEvent.opportunity_claim_id == opportunity.id
        )
    )
    assert reservation.trading_date.isoformat() == "2026-07-13"
    assert opportunity.trading_date.isoformat() == "2026-07-13"
    assert event.payload_json["opportunity_key"]["trading_date"] == "2026-07-13"

    released = store.release_zero_fill(
        first.reservation_id,
        reason="pre_post_release",
    )
    assert released.opportunity_status == "available"
    second_inputs = replace(
        inputs,
        decision_id="captured-et-boundary-2",
        replay_or_paper_run_id=str(
            uuid.uuid5(uuid.NAMESPACE_DNS, "captured-et-boundary-2")
        ),
    )
    second_request = _request(
        decision_id=second_inputs.decision_id,
        client_order_id="captured-et-boundary-cid-2",
        cluster=second_inputs.correlation_cluster_id,
        snapshot=account,
        inputs=second_inputs,
    )
    second = store.reserve(second_request)
    assert second.admission_accepted is True, second.rejection_reasons
    assert second.reservation_id != first.reservation_id
    assert second.trading_date.isoformat() == "2026-07-13"


def test_invalid_first_dip_opportunity_key_never_creates_or_consumes_claim(db) -> None:
    request = _request(
        decision_id="invalid-opportunity-key",
        client_order_id="invalid-opportunity-key-cid",
    )

    with pytest.raises(AdaptiveRiskContractError, match="captured decision: symbol"):
        replace(
            request,
            opportunity_key={
                **request.opportunity_key.to_payload(),
                "symbol": "PLSM",
            },
        )

    assert db.scalar(select(func.count(AdaptiveRiskReservation.reservation_id))) == 0
    assert db.scalar(select(func.count(AdaptiveRiskOpportunityClaim.id))) == 0
    assert db.scalar(select(func.count(AdaptiveRiskOpportunityEvent.id))) == 0


def test_first_dip_decision_from_future_fails_before_claim(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(
        decision_id="future-opportunity-key",
        client_order_id="future-opportunity-key-cid",
    )
    database_at = request.inputs.as_of - timedelta(milliseconds=1)
    monkeypatch.setattr(
        AdaptiveRiskReservationStore,
        "_clock",
        staticmethod(lambda _session: database_at),
    )

    with pytest.raises(
        AdaptiveRiskContractError,
        match="captured decision is after the database transaction observation",
    ):
        AdaptiveRiskReservationStore(engine).reserve(request)

    assert db.scalar(select(func.count(AdaptiveRiskReservation.reservation_id))) == 0
    assert db.scalar(select(func.count(AdaptiveRiskOpportunityClaim.id))) == 0
    assert db.scalar(select(func.count(AdaptiveRiskOpportunityEvent.id))) == 0


def test_first_positive_cumulative_fill_atomically_splits_and_consumes(db) -> None:
    store = AdaptiveRiskReservationStore(engine)
    request = _request()
    decision = store.reserve(request)

    zero = store.apply_cumulative_fill(
        decision.reservation_id,
        evidence=_lifecycle_evidence(
            request,
            event_kind="cumulative_fill",
            provider_event_id="alpaca-fill-clock-0",
            cumulative=0,
            order_status="working",
        ),
    )
    assert zero.opportunity_status == "reserved"
    assert zero.cumulative_filled_quantity_shares == 0
    partial_qty = max(1, decision.quantity_shares // 2)
    partial = store.apply_cumulative_fill(
        decision.reservation_id,
        evidence=_lifecycle_evidence(
            request,
            event_kind="cumulative_fill",
            provider_event_id="alpaca-fill-clock-1",
            cumulative=partial_qty,
            order_status="partially_filled",
        ),
    )
    partial_evidence = _lifecycle_evidence(
        request,
        event_kind="cumulative_fill",
        provider_event_id="alpaca-fill-clock-replayed",
        cumulative=partial_qty,
        order_status="partially_filled",
    )
    replayed = store.apply_cumulative_fill(
        decision.reservation_id,
        evidence=partial_evidence,
    )
    replayed_again = store.apply_cumulative_fill(
        decision.reservation_id,
        evidence=partial_evidence,
    )

    assert partial.state == "partially_filled"
    assert partial.opportunity_status == "consumed"
    assert partial.pending_structural_risk_usd > 0
    assert partial.open_structural_risk_usd > 0
    assert replayed.cumulative_filled_quantity_shares == partial_qty
    assert replayed_again == replayed
    with pytest.raises(AdaptiveReservationStateConflict, match="incompatible status"):
        store.apply_cumulative_fill(
            decision.reservation_id,
            evidence=_lifecycle_evidence(
                request,
                event_kind="cumulative_fill",
                provider_event_id="alpaca-fill-status-conflict",
                cumulative=partial_qty + 1,
                order_status="rejected",
            ),
        )
    with pytest.raises(AdaptiveReservationStateConflict, match="regressed"):
        store.apply_cumulative_fill(
            decision.reservation_id,
            evidence=_lifecycle_evidence(
                request,
                event_kind="cumulative_fill",
                provider_event_id="alpaca-fill-regression",
                cumulative=max(0, partial_qty - 1),
                order_status="partially_filled",
            ),
        )
    with pytest.raises(AdaptiveReservationStateConflict, match="generation changed"):
        store.apply_cumulative_fill(
            decision.reservation_id,
            evidence=_lifecycle_evidence(
                request,
                event_kind="cumulative_fill",
                provider_event_id="alpaca-fill-new-generation",
                cumulative=partial_qty + 1,
                order_status="partially_filled",
                connection_generation="alpaca-reconnected-generation-102",
            ),
        )
    stale_clock = request.inputs.as_of - timedelta(seconds=1)
    with pytest.raises(AdaptiveReservationStateConflict, match="out-of-order"):
        store.apply_cumulative_fill(
            decision.reservation_id,
            evidence=_lifecycle_evidence(
                request,
                event_kind="cumulative_fill",
                provider_event_id="alpaca-fill-out-of-order",
                cumulative=partial_qty + 1,
                order_status="partially_filled",
                observed_at=stale_clock,
                available_at=stale_clock,
            ),
        )
    with pytest.raises(AdaptiveReservationStateConflict, match="non-zero"):
        store.release_zero_fill(
            decision.reservation_id,
            reason="broker_canceled",
            evidence=_lifecycle_evidence(
                request,
                event_kind="terminal_zero_fill",
                provider_event_id="alpaca-terminal-cancel-after-partial",
                cumulative=0,
                order_status="canceled",
            ),
        )
    terminal = store.finalize_filled_entry_remainder(
        decision.reservation_id,
        evidence=_lifecycle_evidence(
            request,
            event_kind="filled_entry_terminal",
            provider_event_id="alpaca-terminal-partial-cancel",
            cumulative=partial_qty,
            order_status="canceled",
        ),
    )
    assert terminal.state == "filled"
    assert terminal.pending_structural_risk_usd == 0
    assert terminal.open_structural_risk_usd > 0
    assert terminal.opportunity_status == "consumed"
    closed = store.close_open_exposure(
        decision.reservation_id,
        evidence=_lifecycle_evidence(
            request,
            event_kind="position_flat",
            provider_event_id="alpaca-position-flat",
            cumulative=partial_qty,
            order_status="flat",
        ),
    )
    assert closed.state == "closed"
    assert closed.open_structural_risk_usd == 0
    assert closed.opportunity_status == "consumed"


def test_late_fill_after_terminal_truth_is_durable_risk_quarantine(db) -> None:
    store = AdaptiveRiskReservationStore(engine)

    released_request = _request(
        symbol="LATE",
        decision_id=f"late-released-{uuid.uuid4().hex}",
        client_order_id=f"late-released-cid-{uuid.uuid4().hex}",
        snapshot=_snapshot(account_scope="alpaca:paper"),
    )
    released_decision = store.reserve(released_request)
    released = store.release_zero_fill(
        released_decision.reservation_id,
        reason="pre_post_release",
    )
    assert released.state == "released"
    late_quantity = max(1, released_decision.quantity_shares // 2)
    late_evidence = _lifecycle_evidence(
        released_request,
        event_kind="cumulative_fill",
        provider_event_id=f"late-released-fill-{uuid.uuid4().hex}",
        cumulative=late_quantity,
        order_status=(
            "filled"
            if late_quantity >= released_decision.quantity_shares
            else "partially_filled"
        ),
    )
    quarantined = store.apply_cumulative_fill(
        released_decision.reservation_id,
        evidence=late_evidence,
    )
    replayed = store.apply_cumulative_fill(
        released_decision.reservation_id,
        evidence=late_evidence,
    )

    assert replayed == quarantined
    assert quarantined.state == "exposure_quarantined"
    assert quarantined.lifecycle_contradiction_source_state == "released"
    assert quarantined.lifecycle_contradiction_at is not None
    assert (
        quarantined.lifecycle_contradiction_evidence_sha256
        == late_evidence.event_content_sha256
    )
    assert quarantined.cumulative_filled_quantity_shares == late_quantity
    assert quarantined.open_quantity_shares == late_quantity
    assert quarantined.open_structural_risk_usd > 0
    assert quarantined.opportunity_status == "consumed"
    db.expire_all()
    released_events = list(
        db.scalars(
            select(AdaptiveRiskReservationEvent)
            .where(
                AdaptiveRiskReservationEvent.reservation_id
                == released_decision.reservation_id
            )
            .order_by(AdaptiveRiskReservationEvent.sequence)
        )
    )
    assert [event.event_type for event in released_events].count(
        "late_cumulative_fill_quarantined"
    ) == 1

    blocked_request = _request(
        symbol="BLOCK",
        decision_id=f"late-blocked-{uuid.uuid4().hex}",
        client_order_id=f"late-blocked-cid-{uuid.uuid4().hex}",
        setup_family="gap_and_go",
        cluster="equity:late-blocked",
        snapshot=_snapshot(account_scope="alpaca:paper"),
    )
    with pytest.raises(AdaptiveRiskExposureQuarantined) as blocked:
        store.reserve(blocked_request)
    assert blocked.value.reason == "adaptive_risk_exposure_quarantined"
    assert blocked.value.quarantined_exposures[0]["symbol"] == "LATE"

    settlement_scope = f"alpaca:paper:settlement-{uuid.uuid4().hex[:8]}"
    settlement_request = _request(
        symbol="LSET",
        decision_id=f"late-settlement-{uuid.uuid4().hex}",
        client_order_id=f"late-settlement-cid-{uuid.uuid4().hex}",
        setup_family="gap_and_go",
        cluster="equity:late-settlement",
        snapshot=_snapshot(account_scope=settlement_scope),
    )
    settlement_decision = store.reserve(settlement_request)
    fully_filled = store.apply_cumulative_fill(
        settlement_decision.reservation_id,
        evidence=_lifecycle_evidence(
            settlement_request,
            event_kind="cumulative_fill",
            provider_event_id=f"settlement-entry-fill-{uuid.uuid4().hex}",
            cumulative=settlement_decision.quantity_shares,
            order_status="filled",
        ),
    )
    assert fully_filled.state == "filled"
    closed = store.close_open_exposure(
        settlement_decision.reservation_id,
        evidence=_lifecycle_evidence(
            settlement_request,
            event_kind="position_flat",
            provider_event_id=f"settlement-flat-{uuid.uuid4().hex}",
            cumulative=settlement_decision.quantity_shares,
            order_status="flat",
        ),
    )
    assert closed.state == "closed"
    closed_late = store.apply_cumulative_fill(
        settlement_decision.reservation_id,
        evidence=_lifecycle_evidence(
            settlement_request,
            event_kind="cumulative_fill",
            provider_event_id=f"settlement-late-fill-{uuid.uuid4().hex}",
            cumulative=settlement_decision.quantity_shares + 1,
            order_status="filled",
        ),
    )
    assert closed_late.state == "exposure_quarantined"
    assert closed_late.lifecycle_contradiction_source_state == "closed"
    assert closed_late.open_quantity_shares == 1
    assert closed_late.open_structural_risk_usd > 0


def test_late_fill_after_alpaca_flat_pending_settlement_stays_quarantined(db) -> None:
    store = AdaptiveRiskReservationStore(engine)
    request = _request(
        symbol="LAFP",
        decision_id=f"late-flat-pending-{uuid.uuid4().hex}",
        client_order_id=f"late-flat-pending-cid-{uuid.uuid4().hex}",
        setup_family="gap_and_go",
        cluster="equity:late-flat-pending",
        snapshot=_snapshot(account_scope="alpaca:paper"),
    )
    decision = store.reserve(request)
    filled = store.apply_cumulative_fill(
        decision.reservation_id,
        evidence=_lifecycle_evidence(
            request,
            event_kind="cumulative_fill",
            provider_event_id=f"late-flat-entry-{uuid.uuid4().hex}",
            cumulative=decision.quantity_shares,
            order_status="filled",
        ),
    )
    assert filled.state == "filled"
    flat = store.close_open_exposure(
        decision.reservation_id,
        evidence=_lifecycle_evidence(
            request,
            event_kind="position_flat",
            provider_event_id=f"late-flat-proof-{uuid.uuid4().hex}",
            cumulative=decision.quantity_shares,
            order_status="flat",
        ),
    )
    assert flat.state == "flat_pending_settlement"

    first_late_evidence = _lifecycle_evidence(
        request,
        event_kind="cumulative_fill",
        provider_event_id=f"late-flat-fill-1-{uuid.uuid4().hex}",
        cumulative=decision.quantity_shares + 1,
        order_status="filled",
    )
    first_late = store.apply_cumulative_fill(
        decision.reservation_id,
        evidence=first_late_evidence,
    )
    assert first_late.state == "exposure_quarantined"
    assert (
        first_late.lifecycle_contradiction_source_state
        == "flat_pending_settlement"
    )
    assert first_late.open_quantity_shares == 1
    assert first_late.lifecycle_contradiction_evidence_sha256 == (
        first_late_evidence.event_content_sha256
    )

    later = store.apply_cumulative_fill(
        decision.reservation_id,
        evidence=_lifecycle_evidence(
            request,
            event_kind="cumulative_fill",
            provider_event_id=f"late-flat-fill-2-{uuid.uuid4().hex}",
            cumulative=decision.quantity_shares + 2,
            order_status="filled",
        ),
    )
    assert later.state == "exposure_quarantined"
    assert later.open_quantity_shares == 2
    assert later.lifecycle_contradiction_source_state == (
        "flat_pending_settlement"
    )
    assert later.lifecycle_contradiction_evidence_sha256 == (
        first_late_evidence.event_content_sha256
    )
    db.expire_all()
    event_types = list(
        db.scalars(
            select(AdaptiveRiskReservationEvent.event_type)
            .where(
                AdaptiveRiskReservationEvent.reservation_id
                == decision.reservation_id
            )
            .order_by(AdaptiveRiskReservationEvent.sequence)
        )
    )
    assert "late_cumulative_fill_quarantined" in event_types
    assert "quarantined_cumulative_fill_advanced" in event_types


def test_quarantined_partial_remainder_and_flat_proof_never_restore_admission(
    db,
) -> None:
    store = AdaptiveRiskReservationStore(engine)
    request = _request(
        symbol="LQRM",
        decision_id=f"late-quarantine-remainder-{uuid.uuid4().hex}",
        client_order_id=f"late-quarantine-remainder-cid-{uuid.uuid4().hex}",
        snapshot=_snapshot(account_scope="alpaca:paper"),
    )
    decision = store.reserve(request)
    assert decision.quantity_shares > 1
    store.release_zero_fill(
        decision.reservation_id,
        reason="pre_post_release",
    )
    late_quantity = max(1, decision.quantity_shares // 2)
    assert late_quantity < decision.quantity_shares
    quarantined = store.apply_cumulative_fill(
        decision.reservation_id,
        evidence=_lifecycle_evidence(
            request,
            event_kind="cumulative_fill",
            provider_event_id=f"late-quarantine-partial-{uuid.uuid4().hex}",
            cumulative=late_quantity,
            order_status="partially_filled",
        ),
    )
    assert quarantined.state == "exposure_quarantined"
    assert float(quarantined.pending_structural_risk_usd) > 0.0

    terminal = store.finalize_filled_entry_remainder(
        decision.reservation_id,
        evidence=_lifecycle_evidence(
            request,
            event_kind="filled_entry_terminal",
            provider_event_id=f"late-quarantine-cancel-{uuid.uuid4().hex}",
            cumulative=late_quantity,
            order_status="canceled",
        ),
    )
    assert terminal.state == "exposure_quarantined"
    assert float(terminal.pending_structural_risk_usd) == 0.0
    assert terminal.open_quantity_shares == late_quantity

    flat = store.close_open_exposure(
        decision.reservation_id,
        evidence=_lifecycle_evidence(
            request,
            event_kind="position_flat",
            provider_event_id=f"late-quarantine-flat-{uuid.uuid4().hex}",
            cumulative=late_quantity,
            order_status="flat",
        ),
    )
    assert flat.state == "exposure_quarantined"
    assert flat.open_quantity_shares == 0
    assert float(flat.open_structural_risk_usd) == 0.0
    assert flat.lifecycle_contradiction_evidence_sha256 == (
        quarantined.lifecycle_contradiction_evidence_sha256
    )

    blocked_request = _request(
        symbol="QBLK",
        decision_id=f"late-quarantine-blocked-{uuid.uuid4().hex}",
        client_order_id=f"late-quarantine-blocked-cid-{uuid.uuid4().hex}",
        setup_family="gap_and_go",
        cluster="equity:late-quarantine-blocked",
        snapshot=_snapshot(account_scope="alpaca:paper"),
    )
    with pytest.raises(AdaptiveRiskExposureQuarantined):
        store.reserve(blocked_request)

    db.expire_all()
    event_types = list(
        db.scalars(
            select(AdaptiveRiskReservationEvent.event_type)
            .where(
                AdaptiveRiskReservationEvent.reservation_id
                == decision.reservation_id
            )
            .order_by(AdaptiveRiskReservationEvent.sequence)
        )
    )
    assert "quarantined_entry_remainder_released" in event_types
    assert "quarantined_exposure_flat_observed" in event_types


def test_fill_over_planned_quantity_is_quarantined_and_risk_accounted(db) -> None:
    store = AdaptiveRiskReservationStore(engine)
    request = _request(
        symbol="OVFL",
        decision_id=f"overfill-{uuid.uuid4().hex}",
        client_order_id=f"overfill-cid-{uuid.uuid4().hex}",
        setup_family="gap_and_go",
        cluster="equity:overfill",
        snapshot=_snapshot(account_scope="alpaca:paper"),
    )
    decision = store.reserve(request)
    filled = store.apply_cumulative_fill(
        decision.reservation_id,
        evidence=_lifecycle_evidence(
            request,
            event_kind="cumulative_fill",
            provider_event_id=f"planned-fill-{uuid.uuid4().hex}",
            cumulative=decision.quantity_shares,
            order_status="filled",
        ),
    )
    assert filled.state == "filled"
    overfill = store.apply_cumulative_fill(
        decision.reservation_id,
        evidence=_lifecycle_evidence(
            request,
            event_kind="cumulative_fill",
            provider_event_id=f"overfill-{uuid.uuid4().hex}",
            cumulative=decision.quantity_shares + 1,
            order_status="filled",
        ),
    )
    assert overfill.state == "exposure_quarantined"
    assert overfill.lifecycle_contradiction_source_state == "filled"
    assert overfill.open_quantity_shares == decision.quantity_shares + 1
    assert float(overfill.pending_structural_risk_usd) == 0.0
    assert float(overfill.open_structural_risk_usd) > float(
        filled.open_structural_risk_usd
    )


def test_submit_indeterminate_retains_claim_but_safe_zero_release_reopens_it(db) -> None:
    store = AdaptiveRiskReservationStore(engine)
    first_request = _request()
    first = store.reserve(first_request)
    submitted = store.mark_submitted(
        first.reservation_id,
        evidence=_lifecycle_evidence(
            first_request,
            event_kind="order_accepted",
            provider_event_id="alpaca-order-accepted-1",
            cumulative=0,
            order_status="accepted",
        ),
    )
    assert submitted.state == "submitted"
    assert submitted.opportunity_status == "reserved"
    timeout_evidence = _submit_attempt_evidence(
        first_request,
        attempt_event_id="post-timeout-1",
        broker_order_id=f"broker-order:{first_request.client_order_id}",
    )
    indeterminate = store.mark_submit_indeterminate(
        first.reservation_id,
        evidence=timeout_evidence,
        reason="transport_timeout_after_post_started",
    )
    assert indeterminate.state == "submit_indeterminate"
    assert indeterminate.opportunity_status == "reserved"

    with pytest.raises(
        AdaptiveReservationIdempotencyConflict,
        match="changed its retained reason",
    ):
        store.mark_submit_indeterminate(
            first.reservation_id,
            evidence=timeout_evidence,
            reason="different_reason",
        )
    with pytest.raises(AdaptiveRiskContractError, match="requires durable"):
        store.release_zero_fill(
            first.reservation_id,
            reason="confirmed_zero_fill",
        )
    with pytest.raises(AdaptiveReservationStateConflict, match="generation changed"):
        store.release_zero_fill(
            first.reservation_id,
            reason="broker_canceled",
            evidence=_lifecycle_evidence(
                first_request,
                event_kind="terminal_zero_fill",
                provider_event_id="reconnect-cancel-not-reconciled",
                cumulative=0,
                order_status="canceled",
                connection_generation="alpaca-reconnect-generation-102",
            ),
        )

    # A distinct pre-POST reservation can be released and the same opportunity
    # can then be considered again under a new immutable decision/CID.
    other = store.reserve(
        _request(
            symbol="PLSM",
            decision_id="plsm-prepost-1",
            client_order_id="chili-plsm-prepost-1",
            cluster="equity:momentum-b",
        )
    )
    released = store.release_zero_fill(
        other.reservation_id,
        reason="pre_post_release",
    )
    assert released.state == "released"
    assert released.opportunity_status == "available"
    again = store.reserve(
        _request(
            symbol="PLSM",
            decision_id="plsm-prepost-2",
            client_order_id="chili-plsm-prepost-2",
            cluster="equity:momentum-b",
        )
    )
    assert again.admission_accepted is True
    assert again.reservation_id != other.reservation_id


def test_lifecycle_evidence_cannot_be_backfilled_from_a_future_clock(db) -> None:
    store = AdaptiveRiskReservationStore(engine)
    request = _request(
        decision_id="future-lifecycle-clock",
        client_order_id="future-lifecycle-clock-cid",
    )
    decision = store.reserve(request)
    future = datetime.now(UTC) + timedelta(minutes=5)

    with pytest.raises(AdaptiveReservationStateConflict, match="not yet available"):
        store.apply_cumulative_fill(
            decision.reservation_id,
            evidence=_lifecycle_evidence(
                request,
                event_kind="cumulative_fill",
                provider_event_id="future-fill-event",
                cumulative=1,
                order_status="partially_filled",
                observed_at=future,
                available_at=future,
            ),
        )

    state = store.mark_submit_indeterminate(
        decision.reservation_id,
        evidence=_submit_attempt_evidence(
            request,
            attempt_event_id="present-submit-timeout",
        ),
        reason="transport_timeout_after_post_started",
    )
    assert state.state == "submit_indeterminate"
    with pytest.raises(
        AdaptiveReservationStateConflict,
        match="retained submit attempt: connection_generation",
    ):
        store.mark_submitted(
            decision.reservation_id,
            evidence=_lifecycle_evidence(
                request,
                event_kind="order_accepted",
                provider_event_id="cross-generation-accept",
                cumulative=0,
                order_status="accepted",
                connection_generation="alpaca-order-stream-102",
            ),
        )


def test_cross_broker_or_mixed_generation_account_facts_fail_closed(db) -> None:
    store = AdaptiveRiskReservationStore(engine)
    alpaca = _snapshot()
    valid_inputs = _inputs(
        alpaca,
        symbol="PLSM",
        decision_id="mixed-account-facts",
        cluster="equity:momentum-a",
    )
    mixed_inputs = replace(
        valid_inputs,
        execution_family="robinhood_spot",
        local_realized_pnl_usd=-300.0,
    )
    request = _request(
        symbol="PLSM",
        decision_id="mixed-account-facts",
        client_order_id="mixed-account-facts-cid",
        snapshot=alpaca,
        inputs=mixed_inputs,
    )

    result = store.reserve(request)

    assert result.admission_accepted is False
    assert result.quantity_shares == 0
    assert "account_snapshot_mismatch:execution_family" in result.rejection_reasons
    assert "account_snapshot_mismatch:local_realized_pnl_usd" in result.rejection_reasons
    assert db.scalar(select(func.count(AdaptiveRiskReservation.reservation_id))) == 0
    packet = db.get(AdaptiveRiskDecisionPacket, result.decision_packet_sha256)
    assert packet.execution_family == "robinhood_spot"
    assert packet.account_snapshot_json["execution_family"] == "alpaca_spot"
    assert packet.admission_accepted is False

    mixed_daily = replace(
        valid_inputs.evidence["daily_pnl"],
        provider_generation="robinhood-generation-incorrect",
    )
    mixed_generation_inputs = replace(
        _inputs(
            alpaca,
            symbol="VEEE",
            decision_id="mixed-account-generation",
            cluster="equity:momentum-v",
        ),
        evidence={
            **_inputs(
                alpaca,
                symbol="VEEE",
                decision_id="mixed-account-generation",
                cluster="equity:momentum-v",
            ).evidence,
            "daily_pnl": mixed_daily,
        },
    )
    mixed_generation = store.reserve(
        _request(
            symbol="VEEE",
            decision_id="mixed-account-generation",
            client_order_id="mixed-account-generation-cid",
            cluster="equity:momentum-v",
            snapshot=alpaca,
            inputs=mixed_generation_inputs,
        )
    )
    assert mixed_generation.admission_accepted is False
    assert (
        "account_snapshot_evidence_mismatch:daily_pnl:generation"
        in mixed_generation.rejection_reasons
    )
    assert db.scalar(select(func.count(AdaptiveRiskReservation.reservation_id))) == 0


def test_missing_or_stale_required_evidence_yields_zero_and_no_reservation(db) -> None:
    store = AdaptiveRiskReservationStore(engine)
    snapshot = _snapshot()
    stale_bbo = _evidence(
        "bbo", available_at=datetime.now(UTC) - timedelta(seconds=120)
    )
    inputs = _inputs(
        snapshot,
        symbol="QTTB",
        decision_id="stale-bbo-decision",
        cluster="equity:momentum-q",
        evidence_overrides={"bbo": stale_bbo},
    )

    result = store.reserve(
        _request(
            symbol="QTTB",
            decision_id="stale-bbo-decision",
            client_order_id="stale-bbo-cid",
            cluster="equity:momentum-q",
            snapshot=snapshot,
            inputs=inputs,
        )
    )

    assert result.admission_accepted is False
    assert result.quantity_shares == 0
    assert "evidence_stale:bbo" in result.rejection_reasons
    assert db.scalar(select(func.count(AdaptiveRiskReservation.reservation_id))) == 0
    assert db.scalar(select(func.count(AdaptiveRiskOpportunityClaim.id))) == 0


def test_db_paper_cannot_consume_before_canonical_fill_is_durable(db) -> None:
    """Regression: the old paper helper applied a fill before its outer write."""

    store = AdaptiveRiskReservationStore(engine)
    snapshot = _snapshot(account_scope="db-paper:session-13103")
    inputs = _inputs(
        snapshot,
        symbol="PLSM",
        decision_id="db-paper-precanonical-misuse",
        cluster="equity:momentum-p",
        surface="db_paper",
    )
    request = _request(
        symbol="PLSM",
        decision_id="db-paper-precanonical-misuse",
        client_order_id="db-paper-precanonical-cid",
        cluster="equity:momentum-p",
        snapshot=snapshot,
        inputs=inputs,
    )
    decision = store.reserve(request)
    asserted_but_not_committed = _lifecycle_evidence(
        request,
        event_kind="cumulative_fill",
        provider_event_id="db-paper-local-assertion-before-write",
        cumulative=decision.quantity_shares,
        order_status="filled",
        connection_generation="db-paper-transaction-13103",
        durability_kind="committed_db_paper_fill",
        broker_source="db_paper",
        source_record_table="trading_automation_simulated_fills",
        source_record_id="9223372036854775807",
    )

    with pytest.raises(
        AdaptiveReservationStateConflict,
        match="missing from the caller transaction",
    ):
        store.apply_cumulative_fill(
            decision.reservation_id,
            evidence=asserted_but_not_committed,
        )

    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, decision.reservation_id)
    opportunity = db.get(
        AdaptiveRiskOpportunityClaim, reservation.opportunity_claim_id
    )
    assert reservation.state == "reserved"
    assert reservation.cumulative_filled_quantity_shares == 0
    assert reservation.open_structural_risk_usd == 0
    assert reservation.pending_structural_risk_usd > 0
    assert opportunity.status == "reserved"

    variant = MomentumStrategyVariant(
        family="db_paper_canonical_fixture",
        variant_key=uuid.uuid4().hex,
        version=1,
        label="DB paper canonical fixture",
        params_json={},
        execution_family="alpaca_spot",
    )
    db.add(variant)
    db.flush()
    automation_session = TradingAutomationSession(
        venue="alpaca",
        execution_family="alpaca_spot",
        mode="paper",
        symbol="PLSM",
        variant_id=variant.id,
        state="entered",
        risk_snapshot_json={},
        allocation_decision_json={},
    )
    db.add(automation_session)
    db.flush()
    lifecycle_event_id = f"db-paper-fill:{decision.reservation_id}"
    connection_generation = "db-paper-transaction-13103"
    marker_base = {
        "adaptive_risk_reservation_id": str(decision.reservation_id),
        "adaptive_risk_decision_packet_sha256": (
            decision.decision_packet_sha256
        ),
        "adaptive_risk_client_order_id": request.client_order_id,
        "adaptive_risk_account_scope": request.account_scope,
        "adaptive_risk_cumulative_fill_quantity": decision.quantity_shares,
    }
    wrong_event_id = f"{lifecycle_event_id}:wrong-generation"
    wrong_fill = TradingAutomationSimulatedFill(
        session_id=automation_session.id,
        symbol="PLSM",
        lane="simulation",
        side="long",
        action="enter_long",
        fill_type="entry",
        quantity=float(decision.quantity_shares),
        price=10.0,
        position_state_before="flat",
        position_state_after="long",
        reason="entry_fill",
        marker_json={
            **marker_base,
            "adaptive_risk_lifecycle_event_id": wrong_event_id,
            # Deliberately wrong first: every provenance marker must be checked
            # before its canonical row hash can advance economic truth.
            "adaptive_risk_connection_generation": "wrong-generation",
        },
    )
    db.add(wrong_fill)
    db.commit()
    db.refresh(wrong_fill)
    wrong_evidence = _lifecycle_evidence(
        request,
        event_kind="cumulative_fill",
        provider_event_id=wrong_event_id,
        cumulative=decision.quantity_shares,
        order_status="filled",
        connection_generation=connection_generation,
        durability_kind="committed_db_paper_fill",
        broker_source="db_paper",
        source_record_table="trading_automation_simulated_fills",
        source_record_id=str(wrong_fill.id),
        event_content_sha256=canonical_db_paper_fill_content_sha256(
            wrong_fill
        ),
    )
    with pytest.raises(
        AdaptiveReservationStateConflict,
        match="not bound to this reservation",
    ):
        store.apply_cumulative_fill(
            decision.reservation_id,
            evidence=wrong_evidence,
        )

    canonical_fill = TradingAutomationSimulatedFill(
        session_id=automation_session.id,
        symbol="PLSM",
        lane="simulation",
        side="long",
        action="enter_long",
        fill_type="entry",
        quantity=float(decision.quantity_shares),
        price=10.0,
        position_state_before="flat",
        position_state_after="long",
        reason="entry_fill",
        marker_json={
            **marker_base,
            "adaptive_risk_lifecycle_event_id": lifecycle_event_id,
            "adaptive_risk_connection_generation": connection_generation,
        },
    )
    db.add(canonical_fill)
    db.commit()
    db.refresh(canonical_fill)
    committed_evidence = _lifecycle_evidence(
        request,
        event_kind="cumulative_fill",
        provider_event_id=lifecycle_event_id,
        cumulative=decision.quantity_shares,
        order_status="filled",
        connection_generation=connection_generation,
        durability_kind="committed_db_paper_fill",
        broker_source="db_paper",
        source_record_table="trading_automation_simulated_fills",
        source_record_id=str(canonical_fill.id),
        event_content_sha256=canonical_db_paper_fill_content_sha256(
            canonical_fill
        ),
    )
    filled = store.apply_cumulative_fill(
        decision.reservation_id,
        evidence=committed_evidence,
    )
    assert filled.state == "filled"
    assert filled.pending_structural_risk_usd == 0
    assert filled.open_structural_risk_usd > 0
    assert filled.opportunity_status == "consumed"


def test_same_opportunity_concurrency_has_one_reservation_across_two_connections(db) -> None:
    store = AdaptiveRiskReservationStore(engine)
    barrier = threading.Barrier(2)
    requests = [
        _request(
            decision_id=f"concurrent-same-opportunity-{index}",
            client_order_id=f"concurrent-same-cid-{index}",
        )
        for index in range(2)
    ]

    def reserve(request):
        barrier.wait(timeout=10)
        return store.reserve(request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, requests))

    assert sum(result.admission_accepted for result in results) == 1
    assert sorted(
        reason
        for result in results
        for reason in result.rejection_reasons
        if reason == "opportunity_already_reserved"
    ) == ["opportunity_already_reserved"]
    assert db.scalar(select(func.count(AdaptiveRiskReservation.reservation_id))) == 1
    assert db.scalar(select(func.count(AdaptiveRiskDecisionPacket.decision_packet_sha256))) == 2
    assert db.scalar(select(func.count(AdaptiveRiskOpportunityClaim.id))) == 1
    assert db.scalar(select(func.count(AdaptiveRiskOpportunityEvent.id))) == 1


def test_non_first_same_setup_can_reserve_concurrently_without_opportunity_rows(db) -> None:
    store = AdaptiveRiskReservationStore(engine)
    barrier = threading.Barrier(2)
    requests = [
        _request(
            symbol="VEEE",
            decision_id=f"concurrent-non-first-{index}",
            client_order_id=f"concurrent-non-first-cid-{index}",
            setup_family="micro_pullback",
            cluster="equity:momentum-v",
        )
        for index in range(2)
    ]

    def reserve(request):
        barrier.wait(timeout=10)
        return store.reserve(request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(reserve, requests))

    assert all(decision.admission_accepted for decision in decisions)
    assert len({decision.reservation_id for decision in decisions}) == 2
    db.expire_all()
    reservations = [
        db.get(AdaptiveRiskReservation, decision.reservation_id)
        for decision in decisions
    ]
    assert all(row.opportunity_claim_id is None for row in reservations)
    assert db.scalar(select(func.count(AdaptiveRiskOpportunityClaim.id))) == 0
    assert db.scalar(select(func.count(AdaptiveRiskOpportunityEvent.id))) == 0

    submitted = store.mark_submitted(
        decisions[0].reservation_id,
        evidence=_lifecycle_evidence(
            requests[0],
            event_kind="order_accepted",
            provider_event_id="non-first-order-accepted",
            cumulative=0,
            order_status="accepted",
        ),
    )
    assert submitted.state == "submitted"
    assert submitted.opportunity_status == "not_applicable"

    filled = store.apply_cumulative_fill(
        decisions[0].reservation_id,
        evidence=_lifecycle_evidence(
            requests[0],
            event_kind="cumulative_fill",
            provider_event_id="non-first-entry-filled",
            cumulative=decisions[0].quantity_shares,
            order_status="filled",
        ),
    )
    assert filled.state == "filled"
    assert filled.opportunity_status == "not_applicable"

    closed = store.close_open_exposure(
        decisions[0].reservation_id,
        evidence=_lifecycle_evidence(
            requests[0],
            event_kind="position_flat",
            provider_event_id="non-first-position-flat",
            cumulative=decisions[0].quantity_shares,
            order_status="flat",
        ),
    )
    released = store.release_zero_fill(
        decisions[1].reservation_id,
        reason="pre_post_release",
    )
    assert closed.state == "closed"
    assert closed.opportunity_status == "not_applicable"
    assert released.state == "released"
    assert released.opportunity_status == "not_applicable"

    reservation_events = db.scalars(
        select(AdaptiveRiskReservationEvent).where(
            AdaptiveRiskReservationEvent.reservation_id.in_(
                [decision.reservation_id for decision in decisions]
            )
        )
    ).all()
    assert reservation_events
    assert all(
        event.payload_json["details"]["opportunity_status"] == "not_applicable"
        for event in reservation_events
    )
    assert db.scalar(select(func.count(AdaptiveRiskOpportunityClaim.id))) == 0
    assert db.scalar(select(func.count(AdaptiveRiskOpportunityEvent.id))) == 0


def test_account_lock_allows_multiple_symbols_when_aggregate_budgets_permit(db) -> None:
    store = AdaptiveRiskReservationStore(engine)
    barrier = threading.Barrier(2)
    requests = (
        _request(
            symbol="VEEE",
            decision_id="parallel-veee",
            client_order_id="parallel-veee-cid",
            cluster="equity:momentum-v",
        ),
        _request(
            symbol="PLSM",
            decision_id="parallel-plsm",
            client_order_id="parallel-plsm-cid",
            cluster="equity:momentum-p",
        ),
    )

    def reserve(request):
        barrier.wait(timeout=10)
        return store.reserve(request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, requests))

    assert all(result.admission_accepted for result in results)
    assert {result.symbol for result in results} == {"VEEE", "PLSM"}
    assert db.scalar(select(func.count(AdaptiveRiskReservation.reservation_id))) == 2


def test_cid_retry_cannot_resize_or_recompute(db) -> None:
    store = AdaptiveRiskReservationStore(engine)
    request = _request()
    first = store.reserve(request)
    assert first.admission_accepted is True, first.rejection_reasons
    changed = replace(request, entry_limit_price=9.99)

    with pytest.raises(
        AdaptiveReservationIdempotencyConflict,
        match="entry_limit_price",
    ):
        store.reserve(changed)

    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, first.reservation_id)
    assert reservation.planned_quantity_shares == first.quantity_shares
    assert db.scalar(select(func.count(AdaptiveRiskReservation.reservation_id))) == 1


def test_foundation_source_has_no_activation_only_dollar_or_symbol_caps() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "trading"
        / "momentum_neural"
        / "adaptive_risk_reservation.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "max_trade_dollars",
        "daily_loss_usd_failsafe",
        "one_symbol",
        "fixed_symbol_concurrency",
        "$50",
        "$250",
    )
    assert not [token for token in forbidden if token in source]


def test_exit_owner_transport_started_receipt_is_atomic_hash_bound_and_idempotent(
    db,
) -> None:
    store, _request_value, decision, binding = (
        _reserve_filled_exit_owner_fixture(db, label="OWNR")
    )
    context = _acquire_exit_owner_claim(db, binding)
    event_at = datetime.now(UTC)
    db.expire_all()
    before = db.get(AdaptiveRiskReservation, decision.reservation_id)
    before_sequence = int(before.event_sequence)
    before_version = int(before.version)
    db.commit()

    oversized_binding = replace(
        binding,
        order_request={
            **dict(binding.order_request),
            "base_size": str(decision.quantity_shares + 1),
        },
    )
    with pytest.raises(
        AdaptiveReservationStateConflict,
        match="exceeds locked positive long exposure",
    ):
        _lease_exit_owner(
            db,
            store,
            oversized_binding,
            context,
            event_at=event_at,
        )
    db.rollback()
    assert db.scalar(
        select(func.count(AdaptiveRiskReservationEvent.id)).where(
            AdaptiveRiskReservationEvent.reservation_id
            == decision.reservation_id,
            AdaptiveRiskReservationEvent.event_type
            == "alpaca_exit_owner_transport_started",
        )
    ) == 0
    db.commit()

    with pytest.raises(RuntimeError, match="force owner receipt rollback"):
        with db.begin():
            leased = _lease_exit_owner(
                db,
                store,
                binding,
                context,
                event_at=event_at,
            )
            assert leased["ok"] is True
            raise RuntimeError("force owner receipt rollback")

    db.expire_all()
    assert db.scalar(
        select(func.count(AdaptiveRiskReservationEvent.id)).where(
            AdaptiveRiskReservationEvent.reservation_id
            == decision.reservation_id,
            AdaptiveRiskReservationEvent.event_type
            == "alpaca_exit_owner_transport_started",
        )
    ) == 0
    rolled_back = db.get(AdaptiveRiskReservation, decision.reservation_id)
    assert rolled_back.event_sequence == before_sequence
    assert rolled_back.version == before_version
    readable, claim = read_action_claim(
        db,
        symbol=binding.symbol,
        account_scope=binding.account_scope,
    )
    assert readable and claim is not None
    assert "owner_transport" not in claim["metadata"]
    db.commit()

    leased = _lease_exit_owner(
        db,
        store,
        binding,
        context,
        event_at=event_at,
    )
    assert leased["ok"] is True
    receipt_sha = leased["exit_owner_transport_started_event_sha256"]
    db.commit()
    with pytest.raises(RuntimeError, match="owner_transport_reconcile_required"):
        _lease_exit_owner(
            db,
            store,
            binding,
            context,
            event_at=event_at,
        )
    db.rollback()
    assert len(receipt_sha) == 64

    db.expire_all()
    receipt = db.scalar(
        select(AdaptiveRiskReservationEvent).where(
            AdaptiveRiskReservationEvent.event_sha256 == receipt_sha
        )
    )
    reservation = db.get(AdaptiveRiskReservation, decision.reservation_id)
    assert receipt.event_type == "alpaca_exit_owner_transport_started"
    assert receipt.payload_json["details"]["request_sha256"] == (
        binding.request_sha256
    )
    assert receipt.payload_json["details"]["entry_client_order_id"] == (
        binding.entry_client_order_id
    )
    assert receipt.payload_json["details"]["exit_client_order_id"] == (
        binding.exit_client_order_id
    )
    readable, claim = read_action_claim(
        db,
        symbol=binding.symbol,
        account_scope=binding.account_scope,
    )
    assert readable and claim is not None
    assert claim["metadata"]["owner_transport"][
        "exit_owner_transport_started_event_sha256"
    ] == receipt_sha
    assert reservation.event_sequence == before_sequence + 1
    assert reservation.last_event_sha256 == receipt_sha
    assert reservation.version == before_version + 1
    assert db.scalar(
        select(func.count(AdaptiveRiskReservationEvent.id)).where(
            AdaptiveRiskReservationEvent.event_sha256 == receipt_sha
        )
    ) == 1


def test_exit_owner_oid_receipt_requires_exact_transport_started_ancestor(
    db,
) -> None:
    store, _request_value, decision, binding = (
        _reserve_filled_exit_owner_fixture(db, label="OIDR")
    )
    context = _acquire_exit_owner_claim(db, binding)
    started_at = datetime.now(UTC)
    leased = _lease_exit_owner(
        db,
        store,
        binding,
        context,
        event_at=started_at,
    )
    assert leased["ok"] is True
    started_sha = leased["exit_owner_transport_started_event_sha256"]
    db.commit()
    outcome_at = started_at + timedelta(milliseconds=10)
    assert advance_owner_transport(
        db,
        **context,
        client_order_id=binding.exit_client_order_id,
        lease_token=binding.transport_lease_id,
        phase="submitted",
        broker_order_id="alpaca-exit-order-oidr",
        metadata={"provider_status": "accepted"},
        exit_owner_store=store,
        exit_owner_effective_at=outcome_at,
        exit_owner_available_at=outcome_at,
        provider_status="accepted",
        provider_cumulative_quantity=0,
        observer_claim_token=binding.transport_claim_token,
        observer_session_id=binding.transport_owner_session_id,
        observer_generation=binding.transport_owner_generation,
        observer_runtime_generation=binding.transport_runtime_generation,
        observer_connection_generation=(
            binding.transport_connection_generation
        ),
    )
    db.commit()
    readable, claim = read_action_claim(
        db,
        symbol=binding.symbol,
        account_scope=binding.account_scope,
    )
    assert readable and claim is not None
    submitted_sha = claim["metadata"]["owner_transport"][
        "exit_owner_last_event_sha256"
    ]
    with pytest.raises(RuntimeError, match="exit_owner_marker_changed_before_advance"):
        advance_owner_transport(
            db,
            **context,
            client_order_id=binding.exit_client_order_id,
            lease_token=binding.transport_lease_id,
            phase="submitted",
            broker_order_id="alpaca-exit-order-oidr",
            metadata={"provider_status": "accepted"},
            exit_owner_store=store,
            exit_owner_effective_at=outcome_at,
            exit_owner_available_at=outcome_at,
            provider_status="accepted",
            provider_cumulative_quantity=0,
            observer_claim_token=binding.transport_claim_token,
            observer_session_id=binding.transport_owner_session_id,
            observer_generation=binding.transport_owner_generation,
            observer_runtime_generation=binding.transport_runtime_generation,
            observer_connection_generation=(
                binding.transport_connection_generation
            ),
        )
    db.rollback()

    db.expire_all()
    submitted = db.scalar(
        select(AdaptiveRiskReservationEvent).where(
            AdaptiveRiskReservationEvent.event_sha256 == submitted_sha
        )
    )
    assert submitted.payload_json["details"][
        "transport_started_event_sha256"
    ] == started_sha
    assert submitted.payload_json["details"]["provider_client_order_id"] == (
        binding.exit_client_order_id
    )
    assert submitted.payload_json["details"]["provider_order_id"] == (
        "alpaca-exit-order-oidr"
    )
    db.commit()

    other_store, _other_request, _other_decision, other_binding = (
        _reserve_filled_exit_owner_fixture(db, label="OIDX")
    )
    other_context = _acquire_exit_owner_claim(db, other_binding)
    other_at = outcome_at + timedelta(milliseconds=10)
    other_leased = _lease_exit_owner(
        db,
        other_store,
        other_binding,
        other_context,
        event_at=other_at,
    )
    assert other_leased["ok"] is True
    other_started_sha = other_leased[
        "exit_owner_transport_started_event_sha256"
    ]
    db.commit()
    with pytest.raises(
        AdaptiveReservationStateConflict,
        match="transport-started receipt is missing",
    ):
        store.append_exit_owner_transport_outcome(
            binding,
            event_type="alpaca_exit_owner_reconciled",
            transport_started_event_sha256=other_started_sha,
            effective_at=other_at + timedelta(milliseconds=10),
            available_at=other_at + timedelta(milliseconds=10),
            observer_claim_token="restart-observer-claim",
            observer_session_id=8002,
            observer_generation=2,
            observer_runtime_generation="paper-runtime-generation-502",
            observer_connection_generation="alpaca-paper-connection-502",
            provider_order_id="alpaca-exit-order-oidr",
            provider_status="filled",
            provider_cumulative_quantity=1,
        )


def test_exit_owner_event_guard_rejects_headless_mutation_delete_and_truncate(
    db,
) -> None:
    store, _request_value, decision, binding = (
        _reserve_filled_exit_owner_fixture(db, label="GARD")
    )
    context = _acquire_exit_owner_claim(db, binding)
    event_at = datetime.now(UTC)
    leased = _lease_exit_owner(
        db,
        store,
        binding,
        context,
        event_at=event_at,
    )
    assert leased["ok"] is True
    started_sha = leased["exit_owner_transport_started_event_sha256"]
    db.commit()
    db.expire_all()
    started = db.scalar(
        select(AdaptiveRiskReservationEvent).where(
            AdaptiveRiskReservationEvent.event_sha256 == started_sha
        )
    )
    forged_payload = json.loads(json.dumps(started.payload_json))
    next_effective = event_at + timedelta(milliseconds=10)
    forged_payload.update(
        {
            "event_type": "alpaca_exit_owner_submit_indeterminate",
            "sequence": int(started.sequence) + 1,
            "effective_at": next_effective.isoformat().replace(
                "+00:00", "Z"
            ),
            "previous_event_sha256": started_sha,
            "broker_event_id": "headless-owner-event",
        }
    )
    forged_payload["details"].update(
        {
            "available_at": next_effective.isoformat().replace(
                "+00:00", "Z"
            ),
            "transport_started_event_sha256": started_sha,
            "observer_claim_token": binding.transport_claim_token,
            "observer_session_id": binding.transport_owner_session_id,
            "observer_generation": binding.transport_owner_generation,
            "observer_runtime_generation": (
                binding.transport_runtime_generation
            ),
            "observer_connection_generation": (
                binding.transport_connection_generation
            ),
        }
    )
    db.commit()

    guarded_statements = (
        (
            "UPDATE adaptive_risk_reservation_events "
            "SET payload_json = payload_json || CAST(:tamper AS jsonb) "
            "WHERE event_sha256 = :sha",
            {"sha": started_sha, "tamper": '{"tampered":true}'},
        ),
        (
            "DELETE FROM adaptive_risk_reservation_events "
            "WHERE event_sha256 = :sha",
            {"sha": started_sha},
        ),
        (
            "TRUNCATE adaptive_risk_reservation_events CASCADE",
            {},
        ),
    )
    for statement, params in guarded_statements:
        with pytest.raises(DBAPIError):
            with db.begin():
                db.execute(text(statement), params)

    with pytest.raises(DBAPIError):
        with db.begin():
            db.execute(
                text(
                    "UPDATE adaptive_risk_reservations SET "
                    "event_sequence = event_sequence + 1, "
                    "last_event_sha256 = :sha, version = version + 1, "
                    "updated_at = :updated_at "
                    "WHERE reservation_id = :reservation_id"
                ),
                {
                    "sha": _hash("head-without-event"),
                    "updated_at": next_effective,
                    "reservation_id": decision.reservation_id,
                },
            )
            db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    with pytest.raises(DBAPIError):
        with db.begin():
            db.execute(
                text(
                    "INSERT INTO adaptive_risk_reservation_events ("
                    "reservation_id, sequence, event_type, "
                    "previous_event_sha256, event_sha256, broker_event_id, "
                    "payload_json, effective_at) VALUES ("
                    ":reservation_id, :sequence, :event_type, :previous_sha, "
                    ":event_sha, :broker_event_id, CAST(:payload AS jsonb), "
                    ":effective_at)"
                ),
                {
                    "reservation_id": decision.reservation_id,
                    "sequence": int(started.sequence) + 1,
                    "event_type": "alpaca_exit_owner_submit_indeterminate",
                    "previous_sha": started_sha,
                    "event_sha": _hash("headless-event"),
                    "broker_event_id": "headless-owner-event",
                    "payload": json.dumps(
                        forged_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "effective_at": next_effective,
                },
            )
            db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
