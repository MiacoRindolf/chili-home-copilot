from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import inspect
import json
import threading
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app import migrations
from app.db import engine
from app.services.trading.momentum_neural.alpaca_cycle_settlement import (
    new_zero_settlement_head,
)
from app.services.trading.momentum_neural import alpaca_fill_activity
from app.services.trading.momentum_neural import captured_paper_entry_intent as contract
from app.services.trading.momentum_neural import captured_paper_outbox as outbox
from app.services.trading.momentum_neural import (
    captured_paper_financial_breaker as financial_breaker,
)
from app.services.trading.momentum_neural import (
    captured_paper_fill_watch as fill_watch,
)
from app.services.trading.momentum_neural import (
    captured_paper_positive_acceptance as positive_acceptance,
)
from app.services.trading.momentum_neural import (
    captured_paper_transport_coordinator as transport,
)
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    AdaptiveReservationStateConflict,
    AdaptiveRiskReservationStore,
)
from app.services.trading.momentum_neural.persistence import (
    ensure_momentum_strategy_variants,
)


UTC = timezone.utc
ACCOUNT_ID = "d7cc580c-2b8f-432f-b771-1cecfb3fe87a"
RUNTIME_GENERATION = "f6ef5ba0-5b91-49bf-a2f5-e71e8e270eb3"
ARM_TOKEN = "d2b8f7d8-6ad5-4cd0-a94e-8a9ca146d3ab"
BINDER_ID = "122158cc-18ae-4cef-bc52-f1c5b689b352"
INTENT_GENERATION = "39f55a65-e6f2-4ccc-bd02-f50dc9c27c69"
COMPLETION_GENERATION = "73dbcf92-94ea-436e-978c-b0e31ce7252d"
OWNER_A = "2ed29ed9-79dd-4f75-ae44-2e5a33b8e77e"
OWNER_B = "53fb486d-f420-4dab-a202-c4de1346b9eb"
RESERVATION_ID = "da45acc8-6b95-4d20-8579-8da28e203511"
ACCOUNT_IDENTITY_SHA256 = "3" * 64
DECISION_PACKET_SHA256 = "4" * 64
RESERVATION_REQUEST_SHA256 = "5" * 64
ADMISSION_EVIDENCE_SHA256 = "6" * 64
BROKER_ORDER_EVIDENCE_SHA256 = "7" * 64


def _request(
    *,
    client_order_id: str = "chili_ml_ACTU_41_1",
    binder_id: str = BINDER_ID,
    intent_generation: str = INTENT_GENERATION,
    completion_generation: str = COMPLETION_GENERATION,
    setup_family: str = "first_dip_reclaim",
    arm_confirmed_at: datetime | None = None,
    arm_expires_at: datetime | None = None,
    decision_at: datetime | None = None,
) -> contract.CapturedPaperPostCommitRequest:
    confirmed_at = arm_confirmed_at or datetime(
        2036, 7, 15, 16, 0, tzinfo=UTC
    )
    expires_at = arm_expires_at or datetime(
        2036, 7, 15, 17, 0, tzinfo=UTC
    )
    resolved_decision_at = decision_at or datetime(
        2036, 7, 15, 16, 30, tzinfo=UTC
    )
    route = contract.CapturedPaperRouteToken(
        session_id=41,
        symbol="ACTU",
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
        expected_account_id=ACCOUNT_ID,
        code_build_sha256="a" * 64,
        config_sha256="b" * 64,
        capture_receipt_sha256="c" * 64,
        runtime_generation=RUNTIME_GENERATION,
        first_dip_policy_mode="candidate",
    )
    arm = contract.CapturedPaperConfirmedArmGeneration(
        session_id=route.session_id,
        arm_token=ARM_TOKEN,
        expires_at=expires_at,
        symbol_claim_token=f"arm-{ARM_TOKEN}",
        account_scope=route.account_scope,
        expected_account_id=route.expected_account_id,
        confirmed_at=confirmed_at,
    )
    opportunity_key = (
        contract.CapturedPaperOpportunityKey(
            account_scope=route.account_scope,
            symbol=route.symbol,
            trading_date=resolved_decision_at.date(),
            setup_family="first_dip_reclaim",
        )
        if setup_family == "first_dip_reclaim"
        else None
    )
    intent = contract.CapturedPaperEntryIntent(
        route_token=route,
        confirmed_arm_generation=arm,
        symbol_claim_token=arm.symbol_claim_token,
        binder_id=binder_id,
        opportunity_key=opportunity_key,
        intent_generation=intent_generation,
        decision_id=client_order_id,
        client_order_id=client_order_id,
        setup_family=setup_family,
        decision_at=resolved_decision_at,
        structural_stop_price="2.50",
        entry_limit_ceiling_price="3.00",
        account_receipt_sha256="d" * 64,
        bbo_receipt_sha256="e" * 64,
        setup_evidence_sha256="f" * 64,
        policy_sha256="1" * 64,
        feature_flags_sha256="2" * 64,
    )
    return contract.CapturedPaperPostCommitRequest(
        intent=intent,
        completion_generation=completion_generation,
    )


def _persist(db, request=None, *, attempts=3, reconciliation_attempts=2):
    resolved = request or _request()
    values = _durable_values(resolved)
    return outbox.persist_captured_paper_post_commit_request(
        db,
        request=resolved,
        **values,
        max_attempts=attempts,
        max_reconciliation_attempts=reconciliation_attempts,
    )


def _request_active_at_database_now(db):
    """Build an arm whose authority interval contains the real DB clock.

    Most contract tests intentionally use a frozen 2036 decision epoch.  The
    literal pre-I/O authority walk, however, compares its durable marker with
    ``clock_timestamp()`` and must not be weakened to accommodate that future
    fixture.  Tests that cross the invocation seam therefore derive only their
    causal times from the database clock.
    """

    now = db.execute(text("SELECT clock_timestamp()" )).scalar_one()
    return _request(
        arm_confirmed_at=now - timedelta(minutes=5),
        arm_expires_at=now + timedelta(hours=1),
        decision_at=now - timedelta(minutes=1),
    )


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _order_request(request):
    intent = request.intent
    return {
        "asset_class": "us_equity",
        "client_order_id": intent.client_order_id,
        "extended_hours": False,
        "limit_price": "3.00",
        "position_intent": "buy_to_open",
        "qty": "100",
        "side": "buy",
        "symbol": intent.route_token.symbol,
        "time_in_force": "day",
        "type": "limit",
    }


def _transport_authority(request):
    intent = request.intent
    route = intent.route_token
    order_request = _order_request(request)
    return outbox.CapturedPaperTransportAuthority(
        completion_sha256=request.completion_sha256,
        account_scope=route.account_scope,
        expected_account_id=route.expected_account_id,
        account_identity_sha256=ACCOUNT_IDENTITY_SHA256,
        session_id=route.session_id,
        symbol=route.symbol,
        client_order_id=intent.client_order_id,
        binder_id=intent.binder_id,
        action_claim_token=intent.symbol_claim_token,
        reservation_id=RESERVATION_ID,
        decision_packet_sha256=DECISION_PACKET_SHA256,
        reservation_request_sha256=RESERVATION_REQUEST_SHA256,
        admission_evidence_sha256=ADMISSION_EVIDENCE_SHA256,
        broker_request_sha256=hashlib.sha256(
            _canonical_json(order_request).encode("utf-8")
        ).hexdigest(),
        opportunity_key_sha256=(
            intent.opportunity_key.opportunity_key_sha256
        ),
    )


def _financial_breaker_receipt(
    request,
    *,
    invocation_authority,
    transport_instruction_sha256,
    now,
    allowed=True,
):
    blocker = None if allowed else "governance_kill_switch"
    reason = None if allowed else "governance_kill_switch"
    evidence = {
        "schema_version": "chili.alpaca-final-breaker-admission.v1",
        "phase": "pre_post",
        "execution_family": request.route_token.execution_family,
        "checked_at_utc": now.isoformat(),
        "checks": [
            {"id": "governance_kill_switch", "ok": allowed}
        ],
        "allowed": allowed,
        "breaker": blocker,
        "reason": reason,
    }
    route = request.route_token
    intent = request.intent
    return financial_breaker.CapturedPaperFinancialBreakerReceipt(
        phase="pre_post",
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
        checked_at=now,
        issued_at=now,
        valid_until=min(
            now + timedelta(seconds=5),
            invocation_authority.valid_until,
            intent.confirmed_arm_generation.expires_at,
        ),
        allowed=allowed,
        blocker=blocker,
        reason=reason,
        breaker_evidence=evidence,
        transport_instruction_sha256=transport_instruction_sha256,
        transport_invocation_authority_sha256=(
            invocation_authority.invocation_authority_sha256
        ),
    )


def _pre_dispatch_evidence(
    request,
    *,
    authority,
    invocation_authority,
    financial_breaker_receipt,
    transport_instruction_sha256,
    now,
):
    return outbox.CapturedPaperTransportPreDispatchEvidence(
        completion_sha256=request.completion_sha256,
        transport_authority_sha256=authority.authority_sha256,
        transport_instruction_sha256=transport_instruction_sha256,
        invocation_authority_sha256=(
            invocation_authority.invocation_authority_sha256
        ),
        connection_receipt_sha256="a" * 64,
        account_scope=authority.account_scope,
        expected_account_id=authority.expected_account_id,
        broker_connection_generation="alpaca-paper-generation-1",
        adapter_build_sha256="b" * 64,
        connection_available_at=now,
        prepared_at=now,
        valid_until=min(
            invocation_authority.valid_until,
            financial_breaker_receipt.valid_until,
        ),
    )


def _durable_values(request):
    authority = _transport_authority(request)
    order = _order_request(request)
    order_hash = authority.broker_request_sha256
    policy_sha256 = "8" * 64
    lock_order = (
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
    admission_record = {
        "schema_version": "chili.captured-paper-atomic-admission.v1",
        "completion_sha256": request.completion_sha256,
        "route_token_sha256": request.intent.route_token.route_token_sha256,
        "intent_sha256": request.intent.intent_sha256,
        "reservation_id": RESERVATION_ID,
        "decision_packet_sha256": DECISION_PACKET_SHA256,
        "reservation_request_sha256": RESERVATION_REQUEST_SHA256,
        "adaptive_input_evidence_sha256": ADMISSION_EVIDENCE_SHA256,
        "account_identity_sha256": ACCOUNT_IDENTITY_SHA256,
        "quantity_shares": 100,
        "order_request_sha256": order_hash,
        "operational_policy_sha256": policy_sha256,
        "lock_order": list(lock_order),
    }
    return {
        "authority": authority,
        "order_request": order,
        "order_request_sha256": order_hash,
        "admission_record": admission_record,
        "admission_record_sha256": hashlib.sha256(
            _canonical_json(admission_record).encode("utf-8")
        ).hexdigest(),
        "quantity_shares": 100,
        "structural_risk_usd": "50",
        "gross_notional_usd": "300",
        "buying_power_impact_usd": "300",
        "operational_policy_sha256": policy_sha256,
        "committed_at": request.intent.decision_at,
        "lock_order": lock_order,
        "reconciliation_retry_delay_seconds": 1,
        "reconciliation_health_escalation_delay_seconds": 60,
    }


def _active_fill_handoff_proof(*, request, authority, suffix="a"):
    source_sha256 = suffix * 64
    observation_sha256 = "b" * 64
    return alpaca_fill_activity.AlpacaPaperEntryFillHandoffProof(
        schema_version=(
            alpaca_fill_activity.ALPACA_PAPER_ENTRY_FILL_HANDOFF_SCHEMA_VERSION
        ),
        publication_kind="active_cycle_fill",
        reservation_id=RESERVATION_ID,
        decision_packet_sha256=DECISION_PACKET_SHA256,
        account_scope=request.intent.route_token.account_scope,
        account_identity_sha256=ACCOUNT_IDENTITY_SHA256,
        client_order_id=request.intent.client_order_id,
        broker_order_id="alpaca-order-ACTU-1",
        broker_connection_generation="alpaca-paper-generation-1",
        observation_sha256=observation_sha256,
        durability_kind="committed_alpaca_paper_fill",
        source_record_table="alpaca_paper_fill_activities",
        source_record_id=source_sha256,
        terminal_evidence_sha256=source_sha256,
        immutable_fill_identity_sha256="c" * 64,
        cumulative_filled_quantity_shares=1,
        lifecycle_provider_event_id=(
            f"alpaca-fill:{source_sha256}:observation:{observation_sha256}"
        ),
        lifecycle_event_sha256="d" * 64,
        lifecycle_event_sequence=2,
        resulting_reservation_state="partially_filled",
        observed_at=datetime(2036, 7, 15, 16, 31, tzinfo=UTC),
        available_at=datetime(2036, 7, 15, 16, 31, 1, tzinfo=UTC),
    )
def _seed_transport_binding(db, request):
    """Seed only the durable rows phase two must already have created."""

    intent = request.intent
    route = intent.route_token
    arm = intent.confirmed_arm_generation
    order_request = _order_request(request)
    ensure_momentum_strategy_variants(db)
    db.flush()
    variant_id = db.execute(
        text("SELECT id FROM momentum_strategy_variants ORDER BY id LIMIT 1")
    ).scalar_one()
    risk_snapshot = {
        "alpaca_account_scope": route.account_scope,
        "alpaca_account_id": route.expected_account_id,
        "alpaca_symbol_claim_token": arm.symbol_claim_token,
        "arm_token": arm.arm_token,
        "expires_at_utc": arm.expires_at.isoformat(),
        "arm_confirmed_at_utc": arm.confirmed_at.isoformat(),
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
        "captured_paper_admission": {
            **_durable_values(request)["admission_record"],
            "admission_record_sha256": _durable_values(request)[
                "admission_record_sha256"
            ],
            "status": "admitted_pending_transport",
        },
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
                :variant_id, 'live_pending_entry', CAST(:risk_snapshot AS JSONB),
                '{}'::jsonb, clock_timestamp(), clock_timestamp(),
                clock_timestamp()
            )
            """
        ),
        {
            "session_id": route.session_id,
            "symbol": route.symbol,
            "variant_id": variant_id,
            "risk_snapshot": _canonical_json(risk_snapshot),
        },
    )

    db.add(
        new_zero_settlement_head(
            account_identity_sha256=ACCOUNT_IDENTITY_SHA256
        )
    )
    db.flush()
    db.execute(
        text(
            """
            INSERT INTO adaptive_risk_decision_packets (
                decision_packet_sha256, reservation_request_sha256,
                decision_id, account_scope, symbol, trading_date,
                setup_family, correlation_cluster, client_order_id,
                execution_surface, execution_family, broker_environment,
                account_identity_sha256, account_snapshot_sha256,
                account_snapshot_generation, policy_sha256, input_sha256,
                economic_input_sha256, economic_resolution_sha256,
                effective_config_sha256, code_build_sha256,
                feature_flags_sha256, capture_prefix_root_sha256,
                evidence_sha256, reservation_ledger_sha256,
                resolved_quantity_shares, structural_stop,
                entry_limit_price, resolver_valid, admission_accepted,
                rejection_reasons_json, account_snapshot_json,
                decision_packet_json
            ) VALUES (
                :packet, :request, :cid, :scope, :symbol, :trading_date,
                :setup, 'hot-momentum', :cid, 'alpaca_paper',
                'alpaca_spot', 'paper', :identity, :account_snapshot,
                'account-generation-1', :policy, :input, :economic_input,
                :economic_resolution, :effective_config, :code_build,
                :feature_flags, :capture_root, :evidence, :ledger,
                100, 2.5, 3.0, TRUE, TRUE, '[]'::jsonb,
                CAST(:account_json AS JSONB), CAST(:packet_json AS JSONB)
            )
            """
        ),
        {
            "packet": DECISION_PACKET_SHA256,
            "request": RESERVATION_REQUEST_SHA256,
            "cid": intent.client_order_id,
            "scope": route.account_scope,
            "symbol": route.symbol,
            "trading_date": intent.opportunity_key.trading_date,
            "setup": intent.setup_family,
            "identity": ACCOUNT_IDENTITY_SHA256,
            "account_snapshot": "8" * 64,
            "policy": intent.policy_sha256,
            "input": "a" * 64,
            "economic_input": "b" * 64,
            "economic_resolution": "c" * 64,
            "effective_config": route.config_sha256,
            "code_build": route.code_build_sha256,
            "feature_flags": intent.feature_flags_sha256,
            "capture_root": "1" * 64,
            "evidence": ADMISSION_EVIDENCE_SHA256,
            "ledger": "2" * 64,
            "account_json": _canonical_json(
                {"account_id": ACCOUNT_ID, "venue": "alpaca"}
            ),
            "packet_json": _canonical_json(
                {"decision_packet_sha256": DECISION_PACKET_SHA256}
            ),
        },
    )
    opportunity_id = db.execute(
        text(
            """
            INSERT INTO adaptive_risk_opportunity_claims (
                account_scope, symbol, trading_date, setup_family,
                status, reservation_id, event_sequence, version
            ) VALUES (
                :scope, :symbol, :trading_date, :setup, 'reserved',
                CAST(:reservation_id AS UUID), 0, 1
            ) RETURNING id
            """
        ),
        {
            "scope": route.account_scope,
            "symbol": route.symbol,
            "trading_date": intent.opportunity_key.trading_date,
            "setup": intent.setup_family,
            "reservation_id": RESERVATION_ID,
        },
    ).scalar_one()
    db.execute(
        text(
            """
            INSERT INTO adaptive_risk_reservations (
                reservation_id, decision_packet_sha256,
                opportunity_claim_id, account_scope, symbol, trading_date,
                setup_family, correlation_cluster, state,
                planned_quantity_shares, cumulative_filled_quantity_shares,
                open_quantity_shares, planned_structural_risk_usd,
                planned_gross_notional_usd,
                planned_buying_power_impact_usd,
                pending_structural_risk_usd, pending_gross_notional_usd,
                pending_buying_power_impact_usd, open_structural_risk_usd,
                open_gross_notional_usd, open_buying_power_impact_usd,
                event_sequence, version
            ) VALUES (
                CAST(:reservation_id AS UUID), :packet, :opportunity_id,
                :scope, :symbol, :trading_date, :setup, 'hot-momentum',
                'reserved', 100, 0, 0, 50, 300, 300, 50, 300, 300,
                0, 0, 0, 0, 1
            )
            """
        ),
        {
            "reservation_id": RESERVATION_ID,
            "packet": DECISION_PACKET_SHA256,
            "opportunity_id": opportunity_id,
            "scope": route.account_scope,
            "symbol": route.symbol,
            "trading_date": intent.opportunity_key.trading_date,
            "setup": intent.setup_family,
        },
    )
    action_metadata = {
        "alpaca_account_id": route.expected_account_id,
        "entry_post_bind_token": intent.binder_id,
        "order_request": order_request,
        "adaptive_risk_decision_packet": {
            "decision_packet_sha256": DECISION_PACKET_SHA256,
        },
        "adaptive_risk_reservation_claim": {
            "claim_id": intent.client_order_id,
            "decision_packet_sha256": DECISION_PACKET_SHA256,
        },
        "adaptive_risk_reservation_request": {
            "request_sha256": RESERVATION_REQUEST_SHA256,
        },
        "captured_paper_completion_sha256": request.completion_sha256,
        "captured_paper_admission_record_sha256": _durable_values(request)[
            "admission_record_sha256"
        ],
        "adaptive_input_evidence_sha256": ADMISSION_EVIDENCE_SHA256,
    }
    db.execute(
        text(
            """
            INSERT INTO broker_symbol_action_claims (
                account_scope, symbol, claim_token, action, phase,
                owner_session_id, client_order_id, metadata_json,
                claimed_at, updated_at, lease_expires_at
            ) VALUES (
                :scope, :symbol, :claim_token, 'entry',
                'claimed', :session_id, :cid,
                CAST(:metadata AS JSONB), clock_timestamp(), clock_timestamp(),
                clock_timestamp() + interval '1 day'
            )
            """
        ),
        {
            "scope": route.account_scope,
            "symbol": route.symbol,
            "claim_token": arm.symbol_claim_token,
            "session_id": route.session_id,
            "cid": intent.client_order_id,
            "metadata": _canonical_json(action_metadata),
        },
    )
    return _transport_authority(request)


def _transport_instruction(request, authority):
    order = _order_request(request)
    return transport.CapturedPaperTransportInstruction(
        request=request,
        authority=authority,
        order_request=order,
        order_request_sha256=authority.broker_request_sha256,
    )


def _positive_observation(
    db,
    instruction,
    *,
    order_id="alpaca-order-ACTU-1",
    status="accepted",
    evidence_sha256=BROKER_ORDER_EVIDENCE_SHA256,
):
    observed_at = db.execute(text("SELECT clock_timestamp()" )).scalar_one()
    exact = transport.CapturedPaperExactBrokerOrderObservation(
        account_scope=instruction.account_scope,
        expected_account_id=instruction.expected_account_id,
        verified_adapter_account_id=instruction.expected_account_id,
        account_binding_source=(
            transport.EXACT_PAPER_ACCOUNT_BINDING_SOURCE
        ),
        broker_account_id=instruction.expected_account_id,
        client_order_id=instruction.client_order_id,
        broker_order_id=order_id,
        symbol=instruction.symbol,
        side="buy",
        order_type="limit",
        asset_class="us_equity",
        quantity_shares=instruction.quantity_shares,
        broker_quantity_echo=str(instruction.quantity_shares),
        broker_filled_quantity_echo="0",
        cumulative_filled_quantity_shares=0,
        limit_price=instruction.limit_price,
        broker_limit_price_echo=instruction.limit_price,
        time_in_force=instruction.time_in_force,
        extended_hours=instruction.extended_hours,
        position_intent_echo=None,
        broker_order_status=status,
        broker_order_status_echo=status,
        broker_connection_generation="alpaca-paper-generation-1",
        broker_order_evidence_sha256=evidence_sha256,
        observed_at=observed_at,
        available_at=observed_at,
    )
    return transport.CapturedPaperPositiveOrderObservation(order=exact)


def _start_transport_for_positive_adoption(db, request=None):
    resolved_request = request or _request_active_at_database_now(db)
    record = _persist(db, resolved_request)
    authority = _seed_transport_binding(db, resolved_request)
    db.commit()
    lease = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert lease is not None
    db.commit()
    outbox.mark_captured_paper_transport_started(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=lease.lease_token,
        lease_owner_id=lease.lease_owner_id,
        authority=authority,
    )
    db.commit()
    _record_allowed_financial_breaker(
        db,
        request=resolved_request,
        record=record,
        authority=authority,
        lease=lease,
    )
    return (
        record,
        authority,
        lease,
        _transport_instruction(resolved_request, authority),
    )


def _record_allowed_financial_breaker(
    db,
    *,
    request,
    record,
    authority,
    lease,
):
    invocation = outbox.authorize_captured_paper_transport_invocation(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=lease.lease_token,
        lease_owner_id=lease.lease_owner_id,
        authority=authority,
    )
    db.commit()
    now = db.execute(text("SELECT clock_timestamp()" )).scalar_one()
    receipt = _financial_breaker_receipt(
        request,
        invocation_authority=invocation,
        transport_instruction_sha256=(
            record.durable_transport.transport_instruction_sha256
        ),
        now=now,
    )
    outbox.record_captured_paper_transport_financial_breaker(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=lease.lease_token,
        lease_owner_id=lease.lease_owner_id,
        authority=authority,
        invocation_authority=invocation,
        receipt=receipt,
    )
    db.commit()
    prepared_at = db.execute(text("SELECT clock_timestamp()" )).scalar_one()
    pre_dispatch = _pre_dispatch_evidence(
        request,
        authority=authority,
        invocation_authority=invocation,
        financial_breaker_receipt=receipt,
        transport_instruction_sha256=(
            record.durable_transport.transport_instruction_sha256
        ),
        now=prepared_at,
    )
    dispatch = outbox.consume_captured_paper_transport_dispatch_authority(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=lease.lease_token,
        lease_owner_id=lease.lease_owner_id,
        authority=authority,
        invocation_authority=invocation,
        financial_breaker_receipt=receipt,
        pre_dispatch_evidence=pre_dispatch,
    )
    db.commit()
    return invocation, receipt, pre_dispatch, dispatch


def _consumed_transport_context(db):
    request = _request_active_at_database_now(db)
    record = _persist(db, request)
    authority = _seed_transport_binding(db, request)
    db.commit()
    raw_lease = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert raw_lease is not None
    db.commit()
    started_record = outbox.mark_captured_paper_transport_started(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=raw_lease.lease_token,
        lease_owner_id=raw_lease.lease_owner_id,
        authority=authority,
    )
    db.commit()
    invocation, receipt, pre_dispatch, dispatch = (
        _record_allowed_financial_breaker(
            db,
            request=request,
            record=record,
            authority=authority,
            lease=raw_lease,
        )
    )
    instruction = _transport_instruction(request, authority)
    committed_lease = transport.CapturedPaperCommittedLease(
        completion_sha256=record.completion_sha256,
        lease_token=raw_lease.lease_token,
        lease_owner_id=raw_lease.lease_owner_id,
        lease_expires_at=raw_lease.lease_expires_at,
        reconciliation_only=False,
    )
    start = transport.CapturedPaperTransportStart(
        lease=committed_lease,
        instruction_sha256=instruction.instruction_sha256,
        transport_authority_sha256=authority.authority_sha256,
        started_at=started_record.transport_started_at,
    )
    bound = db.get_bind()
    bind = getattr(bound, "engine", bound)
    return {
        "request": request,
        "record": record,
        "authority": authority,
        "instruction": instruction,
        "start": start,
        "invocation": invocation,
        "receipt": receipt,
        "pre_dispatch": pre_dispatch,
        "dispatch": dispatch,
        "store": transport.SqlAlchemyCapturedPaperTransportStore(bind),
    }


def _mark_canonical_broker_accepted(db, request, authority, *, kind):
    observed_at = datetime(2036, 7, 15, 16, 31, tzinfo=UTC)
    available_at = datetime(2036, 7, 15, 16, 31, 1, tzinfo=UTC)
    broker_order_id = "alpaca-order-ACTU-1"
    db.execute(
        text(
            """
            UPDATE adaptive_risk_reservations
               SET state = 'submitted', broker_order_id = :broker_order_id,
                   broker_source = 'alpaca',
                   broker_connection_generation = 'alpaca-paper-generation-1',
                   last_broker_observed_at = :observed_at,
                   last_broker_available_at = :available_at,
                   last_source_event_content_sha256 = :evidence,
                   submitted_at = :available_at, updated_at = clock_timestamp(),
                   version = version + 1
             WHERE reservation_id = CAST(:reservation_id AS UUID)
            """
        ),
        {
            "broker_order_id": broker_order_id,
            "observed_at": observed_at,
            "available_at": available_at,
            "evidence": BROKER_ORDER_EVIDENCE_SHA256,
            "reservation_id": authority.reservation_id,
        },
    )
    db.execute(
        text(
            """
            UPDATE broker_symbol_action_claims
               SET phase = 'submitted', broker_order_id = :broker_order_id,
                   updated_at = clock_timestamp()
             WHERE account_scope = :account_scope AND symbol = :symbol
            """
        ),
        {
            "broker_order_id": broker_order_id,
            "account_scope": authority.account_scope,
            "symbol": authority.symbol,
        },
    )
    return outbox.CapturedPaperBrokerAcceptanceProof(
        acceptance_kind=kind,
        completion_sha256=request.completion_sha256,
        account_scope=authority.account_scope,
        expected_account_id=authority.expected_account_id,
        client_order_id=authority.client_order_id,
        broker_order_id=broker_order_id,
        reservation_id=authority.reservation_id,
        action_claim_token=authority.action_claim_token,
        binder_id=authority.binder_id,
        broker_order_evidence_sha256=BROKER_ORDER_EVIDENCE_SHA256,
        observed_at=observed_at,
        available_at=available_at,
    )


def test_v2_contract_roundtrip_binds_arm_claim_opportunity_binder_and_cid():
    request = _request()
    restored = contract.CapturedPaperPostCommitRequest.from_canonical_json(
        request.to_canonical_json()
    )

    assert restored == request
    assert restored.intent.decision_id == restored.intent.client_order_id
    assert restored.intent.symbol_claim_token == f"arm-{ARM_TOKEN}"
    assert restored.intent.binder_id == BINDER_ID
    assert restored.intent.opportunity_key is not None
    assert len(restored.intent.opportunity_key.opportunity_key_sha256) == 64
    assert (
        restored.intent.confirmed_arm_generation.confirmed_arm_generation_sha256
        == request.intent.confirmed_arm_generation.confirmed_arm_generation_sha256
    )
    assert request.to_canonical_json() == json.dumps(
        request.to_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def test_v2_contract_rejects_unequal_cid_missing_first_dip_key_and_mutation():
    request = _request()
    kwargs = {
        key: getattr(request.intent, key)
        for key in (
            "route_token",
            "confirmed_arm_generation",
            "symbol_claim_token",
            "binder_id",
            "opportunity_key",
            "intent_generation",
            "decision_id",
            "client_order_id",
            "setup_family",
            "decision_at",
            "structural_stop_price",
            "entry_limit_ceiling_price",
            "account_receipt_sha256",
            "bbo_receipt_sha256",
            "setup_evidence_sha256",
            "policy_sha256",
            "feature_flags_sha256",
        )
    }
    with pytest.raises(
        contract.CapturedPaperIntentContractError,
        match="decision_client_order_id_mismatch",
    ):
        contract.CapturedPaperEntryIntent(
            **{**kwargs, "decision_id": "different-decision-id"}
        )
    with pytest.raises(
        contract.CapturedPaperIntentContractError,
        match="first_dip_opportunity_key_missing",
    ):
        contract.CapturedPaperEntryIntent(
            **{**kwargs, "opportunity_key": None}
        )

    payload = request.to_payload()
    payload["intent"]["confirmed_arm_generation"][
        "symbol_claim_token"
    ] = f"arm-00000000-0000-4000-8000-000000000000"
    tampered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with pytest.raises(
        contract.CapturedPaperIntentContractError,
        match="confirmed_arm_symbol_claim_token_invalid",
    ):
        contract.CapturedPaperPostCommitRequest.from_canonical_json(tampered)


def test_persist_is_atomic_content_addressed_idempotent_and_side_effect_free(db):
    request = _request()
    protected_tables = (
        "adaptive_risk_reservations",
        "adaptive_risk_opportunity_claims",
        "broker_symbol_action_claims",
    )
    before = {
        name: db.execute(text(f"SELECT count(*) FROM {name}")).scalar_one()
        for name in protected_tables
    }
    first = _persist(db, request)
    second = _persist(db, request)
    after = {
        name: db.execute(text(f"SELECT count(*) FROM {name}")).scalar_one()
        for name in protected_tables
    }

    assert first.status == outbox.OUTBOX_STATUS_PENDING
    assert second.payload_sha256 == first.payload_sha256
    assert second.events == first.events
    assert [event.event_type for event in first.events] == ["enqueued"]
    assert before == after
    assert first.request.to_canonical_json() == request.to_canonical_json()


def test_durable_transport_loader_rehashes_and_rebinds_exact_phase_one_bytes(db):
    request = _request()
    record = _persist(db, request)
    authority = _seed_transport_binding(db, request)
    db.commit()

    bundle = outbox.load_captured_paper_durable_transport_bundle(
        db,
        completion_sha256=record.completion_sha256,
    )
    instruction = transport.CapturedPaperTransportInstruction.from_durable_bundle(
        bundle
    )

    assert bundle.request == request
    assert bundle.authority == authority
    assert bundle.order_request == _order_request(request)
    assert bundle.order_request_sha256 == authority.broker_request_sha256
    assert instruction.instruction_sha256 == (
        bundle.transport_instruction_sha256
    )
    assert bundle.committed_admission["quantity_shares"] == 100
    assert bundle.committed_admission["structural_risk_usd"] == "50"
    assert bundle.reconciliation_retry_delay_seconds == 1
    assert bundle.reconciliation_health_escalation_delay_seconds == 60


def test_durable_transport_private_loader_rejects_corrupt_canonical_bytes(db):
    record = _persist(db)
    row = dict(
        db.execute(
            text(
                f"SELECT {outbox._ROW_COLUMNS} "
                "FROM captured_paper_post_commit_outbox "
                "WHERE completion_sha256 = :completion_sha256"
            ),
            {"completion_sha256": record.completion_sha256},
        ).mappings().one()
    )
    changed_order = json.loads(row["order_request_canonical_json"])
    changed_order["qty"] = "101"
    row["order_request_canonical_json"] = _canonical_json(changed_order)

    with pytest.raises(
        outbox.CapturedPaperOutboxCorruptionError,
        match="outbox_order_request_sha256_mismatch",
    ):
        outbox._durable_bundle_from_row(row)


def test_durable_transport_loader_rejects_related_session_metadata_drift(db):
    request = _request()
    record = _persist(db, request)
    _seed_transport_binding(db, request)
    db.execute(
        text(
            "UPDATE trading_automation_sessions "
            "SET risk_snapshot_json = jsonb_set(" 
            "risk_snapshot_json, "
            "'{captured_paper_admission,admission_record_sha256}', "
            "to_jsonb(CAST(:wrong_hash AS TEXT)), false) "
            "WHERE id = :session_id"
        ),
        {"wrong_hash": "0" * 64, "session_id": request.intent.route_token.session_id},
    )

    with pytest.raises(
        outbox.CapturedPaperOutboxCorruptionError,
        match="outbox_durable_transport_related_metadata_mismatch",
    ):
        outbox.load_captured_paper_durable_transport_bundle(
            db,
            completion_sha256=record.completion_sha256,
        )


def test_same_cid_with_different_canonical_bytes_is_a_hard_conflict(db):
    first = _request()
    _persist(db, first)
    conflict = _request(
        binder_id="39289bbc-23cb-4688-9c67-17b13561bf67",
        intent_generation="e10cc008-60ea-4f5f-b6e2-9cbcbf43c299",
        completion_generation="1b96515c-3c5b-41c9-91eb-eb04aa89b034",
    )
    with pytest.raises(
        outbox.CapturedPaperOutboxConflictError,
        match="outbox_same_id_different_bytes",
    ):
        _persist(db, conflict)


def test_phase_one_rollback_removes_request_and_initial_event(db):
    request = _request()
    nested = db.begin_nested()
    _persist(db, request)
    nested.rollback()

    with pytest.raises(
        outbox.CapturedPaperOutboxNotFoundError,
        match="captured_paper_outbox_not_found",
    ):
        outbox.load_captured_paper_outbox(
            db, completion_sha256=request.completion_sha256
        )
    event_count = db.execute(
        text(
            "SELECT count(*) FROM captured_paper_post_commit_outbox_events "
            "WHERE completion_sha256 = :completion_sha256"
        ),
        {"completion_sha256": request.completion_sha256},
    ).scalar_one()
    assert event_count == 0


def test_pretransport_retry_is_bounded_and_never_creates_transport_marker(db):
    record = _persist(db, attempts=2)
    first = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert first is not None
    retry = outbox.mark_captured_paper_retryable(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=first.lease_token,
        lease_owner_id=first.lease_owner_id,
        failure_sha256="4" * 64,
        retry_delay_seconds=0,
    )
    assert retry.status == outbox.OUTBOX_STATUS_RETRY_WAIT

    second = outbox.lease_next_captured_paper_completion(
        db, lease_owner_id=OWNER_B, lease_seconds=30
    )
    assert second is not None
    assert second.record.request.intent.client_order_id == (
        first.record.request.intent.client_order_id
    )
    exhausted = outbox.mark_captured_paper_retryable(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=second.lease_token,
        lease_owner_id=second.lease_owner_id,
        failure_sha256="5" * 64,
        retry_delay_seconds=0,
    )
    assert exhausted.status == outbox.OUTBOX_STATUS_RETRY_EXHAUSTED
    assert exhausted.transport_started_at is None
    assert (
        outbox.lease_next_captured_paper_completion(
            db, lease_owner_id=OWNER_A, lease_seconds=30
        )
        is None
    )


def test_transport_invocation_authority_reinventories_and_is_one_shot(db):
    now = db.execute(text("SELECT clock_timestamp()" )).scalar_one()
    request = _request(
        arm_confirmed_at=now - timedelta(minutes=1),
        arm_expires_at=now + timedelta(minutes=10),
        decision_at=now,
    )
    record = _persist(db, request)
    db.commit()
    authority = _seed_transport_binding(db, request)
    db.commit()
    leased = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert leased is not None
    db.commit()
    started = outbox.mark_captured_paper_transport_started(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=leased.lease_token,
        lease_owner_id=leased.lease_owner_id,
        authority=authority,
    )
    db.commit()

    receipt = outbox.authorize_captured_paper_transport_invocation(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=leased.lease_token,
        lease_owner_id=leased.lease_owner_id,
        authority=authority,
    )
    assert receipt.transport_started_at == started.transport_started_at
    assert receipt.transport_authority_sha256 == authority.authority_sha256
    assert receipt.lease_token == leased.lease_token
    assert receipt.lease_owner_id == leased.lease_owner_id
    assert receipt.verified_at < receipt.valid_until
    receipt.verify_for(
        authority,
        transport_instruction_sha256=(
            record.durable_transport.transport_instruction_sha256
        ),
        lease_token=leased.lease_token,
        lease_owner_id=leased.lease_owner_id,
    )
    db.commit()

    current = outbox.load_captured_paper_outbox(
        db, completion_sha256=record.completion_sha256
    )
    assert current.status == outbox.OUTBOX_STATUS_TRANSPORT_STARTED
    assert current.events[-1].event_type == "transport_invocation_authorized"
    assert current.events[-1].event_payload == receipt.to_payload()

    with pytest.raises(
        outbox.CapturedPaperOutboxConflictError,
        match="transport_invocation_already_authorized",
    ):
        outbox.authorize_captured_paper_transport_invocation(
            db,
            completion_sha256=record.completion_sha256,
            lease_token=leased.lease_token,
            lease_owner_id=leased.lease_owner_id,
            authority=authority,
        )


def test_transport_financial_breaker_receipt_is_durable_before_io_and_one_shot(
    db,
):
    now = db.execute(text("SELECT clock_timestamp()" )).scalar_one()
    request = _request(
        arm_confirmed_at=now - timedelta(minutes=1),
        arm_expires_at=now + timedelta(minutes=10),
        decision_at=now,
    )
    record = _persist(db, request)
    authority = _seed_transport_binding(db, request)
    db.commit()
    lease = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert lease is not None
    db.commit()
    started = outbox.mark_captured_paper_transport_started(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=lease.lease_token,
        lease_owner_id=lease.lease_owner_id,
        authority=authority,
    )
    db.commit()
    invocation = outbox.authorize_captured_paper_transport_invocation(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=lease.lease_token,
        lease_owner_id=lease.lease_owner_id,
        authority=authority,
    )
    db.commit()
    issued_at = db.execute(text("SELECT clock_timestamp()" )).scalar_one()
    receipt = _financial_breaker_receipt(
        request,
        invocation_authority=invocation,
        transport_instruction_sha256=(
            record.durable_transport.transport_instruction_sha256
        ),
        now=issued_at,
    )

    committed = outbox.record_captured_paper_transport_financial_breaker(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=lease.lease_token,
        lease_owner_id=lease.lease_owner_id,
        authority=authority,
        invocation_authority=invocation,
        receipt=receipt,
    )
    assert committed.receipt_sha256 == receipt.receipt_sha256
    db.commit()

    current = outbox.load_captured_paper_outbox(
        db,
        completion_sha256=record.completion_sha256,
    )
    assert current.status == outbox.OUTBOX_STATUS_TRANSPORT_STARTED
    assert current.transport_started_at == started.transport_started_at
    assert current.events[-1].event_type == (
        "transport_financial_breaker_recorded"
    )
    assert current.events[-1].event_payload == receipt.to_payload()
    assert current.events[-2].event_type == "transport_invocation_authorized"

    with pytest.raises(
        outbox.CapturedPaperOutboxConflictError,
        match="transport_financial_breaker_already_recorded",
    ):
        outbox.record_captured_paper_transport_financial_breaker(
            db,
            completion_sha256=record.completion_sha256,
            lease_token=lease.lease_token,
            lease_owner_id=lease.lease_owner_id,
            authority=authority,
            invocation_authority=invocation,
            receipt=receipt,
        )


def test_transport_invocation_rejects_post_fence_admission_invalidation(db):
    now = db.execute(text("SELECT clock_timestamp()" )).scalar_one()
    request = _request(
        arm_confirmed_at=now - timedelta(minutes=1),
        arm_expires_at=now + timedelta(minutes=10),
        decision_at=now,
    )
    record = _persist(db, request)
    db.commit()
    authority = _seed_transport_binding(db, request)
    db.commit()
    leased = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert leased is not None
    db.commit()
    outbox.mark_captured_paper_transport_started(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=leased.lease_token,
        lease_owner_id=leased.lease_owner_id,
        authority=authority,
    )
    db.commit()

    # Simulate a committed authority revocation after the durable fence but
    # before broker invocation.  The second lock walk must see it.
    db.execute(
        text(
            "UPDATE trading_automation_sessions "
            "SET ended_at = clock_timestamp() WHERE id = :session_id"
        ),
        {"session_id": request.intent.route_token.session_id},
    )
    db.commit()

    with pytest.raises(
        outbox.CapturedPaperOutboxError,
        match="transport_authority_automation_mismatch",
    ):
        outbox.authorize_captured_paper_transport_invocation(
            db,
            completion_sha256=record.completion_sha256,
            lease_token=leased.lease_token,
            lease_owner_id=leased.lease_owner_id,
            authority=authority,
        )

    current = outbox.load_captured_paper_outbox(
        db, completion_sha256=record.completion_sha256
    )
    assert current.status == outbox.OUTBOX_STATUS_TRANSPORT_STARTED
    assert "transport_invocation_authorized" not in {
        event.event_type for event in current.events
    }


def test_final_dispatch_fence_rejects_authority_revoked_after_broker_read(db):
    """PT-C3: the post-read canonical lock walk rejects drift with zero POST."""

    now = db.execute(text("SELECT clock_timestamp()" )).scalar_one()
    request = _request(
        arm_confirmed_at=now - timedelta(minutes=1),
        arm_expires_at=now + timedelta(minutes=10),
        decision_at=now,
    )
    record = _persist(db, request)
    authority = _seed_transport_binding(db, request)
    db.commit()
    lease = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert lease is not None
    db.commit()
    outbox.mark_captured_paper_transport_started(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=lease.lease_token,
        lease_owner_id=lease.lease_owner_id,
        authority=authority,
    )
    db.commit()
    invocation = outbox.authorize_captured_paper_transport_invocation(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=lease.lease_token,
        lease_owner_id=lease.lease_owner_id,
        authority=authority,
    )
    db.commit()
    issued_at = db.execute(text("SELECT clock_timestamp()" )).scalar_one()
    receipt = _financial_breaker_receipt(
        request,
        invocation_authority=invocation,
        transport_instruction_sha256=(
            record.durable_transport.transport_instruction_sha256
        ),
        now=issued_at,
    )
    outbox.record_captured_paper_transport_financial_breaker(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=lease.lease_token,
        lease_owner_id=lease.lease_owner_id,
        authority=authority,
        invocation_authority=invocation,
        receipt=receipt,
    )
    db.commit()
    prepared_at = db.execute(text("SELECT clock_timestamp()" )).scalar_one()
    pre_dispatch = _pre_dispatch_evidence(
        request,
        authority=authority,
        invocation_authority=invocation,
        financial_breaker_receipt=receipt,
        transport_instruction_sha256=(
            record.durable_transport.transport_instruction_sha256
        ),
        now=prepared_at,
    )

    # External broker/account preparation has completed.  Revoke canonical
    # admission authority before the irreversible dispatch event is consumed.
    db.execute(
        text(
            "UPDATE trading_automation_sessions "
            "SET ended_at = clock_timestamp() WHERE id = :session_id"
        ),
        {"session_id": request.intent.route_token.session_id},
    )
    db.commit()

    with pytest.raises(
        outbox.CapturedPaperOutboxError,
        match="transport_authority_automation_mismatch",
    ):
        outbox.consume_captured_paper_transport_dispatch_authority(
            db,
            completion_sha256=record.completion_sha256,
            lease_token=lease.lease_token,
            lease_owner_id=lease.lease_owner_id,
            authority=authority,
            invocation_authority=invocation,
            financial_breaker_receipt=receipt,
            pre_dispatch_evidence=pre_dispatch,
        )

    current = outbox.load_captured_paper_outbox(
        db, completion_sha256=record.completion_sha256
    )
    event_types = [event.event_type for event in current.events]
    assert current.status == outbox.OUTBOX_STATUS_TRANSPORT_STARTED
    assert event_types[-2:] == [
        "transport_invocation_authorized",
        "transport_financial_breaker_recorded",
    ]
    assert "transport_invocation_consumed" not in event_types


def test_post_consume_linearization_rejects_committed_reservation_invalidation(
    db,
):
    """C3: authority invalidated after the fence still authorizes zero POSTs."""

    context = _consumed_transport_context(db)
    db.execute(
        text(
            "UPDATE adaptive_risk_reservations "
            "SET state = 'released', version = version + 1 "
            "WHERE reservation_id = CAST(:reservation_id AS UUID)"
        ),
        {"reservation_id": context["authority"].reservation_id},
    )
    db.commit()
    fake_post_calls = []

    with pytest.raises(
        outbox.CapturedPaperOutboxError,
        match="transport_authority_pre_invocation_state_mismatch",
    ):
        with context["store"].acquire_dispatch_linearization(
            context["instruction"],
            context["start"],
            context["invocation"],
            context["receipt"],
            context["pre_dispatch"],
            context["dispatch"],
        ):
            fake_post_calls.append(context["instruction"].client_order_id)

    assert fake_post_calls == []


def test_alpaca_paper_pre_post_release_requires_exact_claim_fence(db):
    """Local did-not-POST knowledge cannot release Alpaca PAPER risk."""

    request = _request_active_at_database_now(db)
    _seed_transport_binding(db, request)
    db.commit()
    bound = db.get_bind()
    bind = getattr(bound, "engine", bound)
    store = AdaptiveRiskReservationStore(bind)
    reservation_id = uuid.UUID(RESERVATION_ID)

    with pytest.raises(
        AdaptiveReservationStateConflict,
        match="Alpaca PAPER pre-POST release requires exact action-claim fence",
    ):
        store.release_zero_fill(
            reservation_id,
            reason="pre_post_release",
        )

    state = store.read_state(reservation_id)
    assert state.state == "reserved"
    assert state.opportunity_status == "reserved"


def test_alpaca_paper_pre_claim_release_remains_reusable(db):
    """The safety fence does not create a permanent pre-claim dark throttle."""

    request = _request_active_at_database_now(db)
    _seed_transport_binding(db, request)
    db.execute(
        text(
            "DELETE FROM broker_symbol_action_claims "
            "WHERE account_scope = :account_scope AND symbol = :symbol"
        ),
        {
            "account_scope": request.intent.route_token.account_scope,
            "symbol": request.intent.route_token.symbol,
        },
    )
    db.commit()
    bound = db.get_bind()
    bind = getattr(bound, "engine", bound)
    store = AdaptiveRiskReservationStore(bind)
    reservation_id = uuid.UUID(RESERVATION_ID)

    state = store.release_zero_fill(
        reservation_id,
        reason="pre_post_release",
    )

    assert state.state == "released"
    assert state.opportunity_status == "available"


def test_post_consume_linearization_rejects_committed_admission_invalidation(
    db,
):
    """C3: capture/admission revocation after the fence means zero POSTs."""

    context = _consumed_transport_context(db)
    db.execute(
        text(
            "UPDATE trading_automation_sessions "
            "SET risk_snapshot_json = jsonb_set("
            "risk_snapshot_json, '{captured_paper_admission,status}', "
            "'\"revoked\"'::jsonb, FALSE), updated_at = clock_timestamp() "
            "WHERE id = :session_id"
        ),
        {
            "session_id": (
                context["instruction"].request.intent.route_token.session_id
            )
        },
    )
    db.commit()
    fake_post_calls = []

    with pytest.raises(
        outbox.CapturedPaperOutboxCorruptionError,
        match="outbox_durable_transport_related_metadata_mismatch",
    ):
        with context["store"].acquire_dispatch_linearization(
            context["instruction"],
            context["start"],
            context["invocation"],
            context["receipt"],
            context["pre_dispatch"],
            context["dispatch"],
        ):
            fake_post_calls.append(context["instruction"].client_order_id)

    assert fake_post_calls == []


def test_post_consume_linearization_rejects_committed_operator_revocation(db):
    """An operator pause/reconcile projection after the fence means zero POSTs."""

    context = _consumed_transport_context(db)
    db.execute(
        text(
            "UPDATE trading_automation_sessions "
            "SET risk_snapshot_json = jsonb_set(jsonb_set("
            "risk_snapshot_json, '{operator_pause}', "
            "jsonb_build_object('active', TRUE), TRUE), "
            "'{momentum_live_execution,entry_submitted}', "
            "'true'::jsonb, TRUE), updated_at = clock_timestamp() "
            "WHERE id = :session_id"
        ),
        {
            "session_id": (
                context["instruction"].request.intent.route_token.session_id
            )
        },
    )
    db.commit()
    fake_post_calls = []

    with pytest.raises(
        outbox.CapturedPaperOutboxError,
        match="transport_authority_pre_invocation_state_mismatch",
    ):
        with context["store"].acquire_dispatch_linearization(
            context["instruction"],
            context["start"],
            context["invocation"],
            context["receipt"],
            context["pre_dispatch"],
            context["dispatch"],
        ):
            fake_post_calls.append(context["instruction"].client_order_id)

    assert fake_post_calls == []


def test_post_consume_linearization_rejects_committed_opportunity_invalidation(
    db,
):
    """C3: consumed/released opportunity authority cannot reach POST."""

    context = _consumed_transport_context(db)
    db.execute(
        text(
            "UPDATE adaptive_risk_opportunity_claims "
            "SET status = 'available', reservation_id = NULL, "
            "version = version + 1, updated_at = clock_timestamp() "
            "WHERE reservation_id = CAST(:reservation_id AS UUID)"
        ),
        {"reservation_id": context["authority"].reservation_id},
    )
    db.commit()
    fake_post_calls = []

    with pytest.raises(
        outbox.CapturedPaperOutboxError,
        match="transport_authority_opportunity_mismatch",
    ):
        with context["store"].acquire_dispatch_linearization(
            context["instruction"],
            context["start"],
            context["invocation"],
            context["receipt"],
            context["pre_dispatch"],
            context["dispatch"],
        ):
            fake_post_calls.append(context["instruction"].client_order_id)

    assert fake_post_calls == []


def test_post_consume_linearization_rejects_stale_outbox_version_with_zero_post(
    db,
):
    context = _consumed_transport_context(db)
    db.execute(
        text(
            "UPDATE captured_paper_post_commit_outbox "
            "SET version = version + 1 "
            "WHERE completion_sha256 = :completion_sha256"
        ),
        {"completion_sha256": context["record"].completion_sha256},
    )
    db.commit()
    fake_post_calls = []

    with pytest.raises(
        outbox.CapturedPaperOutboxCorruptionError,
        match="transport_dispatch_revalidation_event_mismatch",
    ):
        with context["store"].acquire_dispatch_linearization(
            context["instruction"],
            context["start"],
            context["invocation"],
            context["receipt"],
            context["pre_dispatch"],
            context["dispatch"],
        ):
            fake_post_calls.append(context["instruction"].client_order_id)

    assert fake_post_calls == []


def test_post_consume_linearization_lock_busy_is_bounded_zero_post(db):
    context = _consumed_transport_context(db)
    identity = transport.AdaptiveRiskAccountLockIdentity.for_scope(
        context["instruction"].account_scope
    )
    bound = db.get_bind()
    bind = getattr(bound, "engine", bound)
    holder = bind.connect()
    fake_post_calls = []
    try:
        with holder.begin():
            holder.execute(
                text("SELECT pg_advisory_lock(:key)"),
                {"key": identity.action_advisory_key},
            )
        started = datetime.now(UTC)
        with pytest.raises(
            transport.CapturedPaperTransportUnavailable,
            match="transport_dispatch_action_lock_unavailable",
        ):
            with context["store"].acquire_dispatch_linearization(
                context["instruction"],
                context["start"],
                context["invocation"],
                context["receipt"],
                context["pre_dispatch"],
                context["dispatch"],
            ):
                fake_post_calls.append(
                    context["instruction"].client_order_id
                )
        assert datetime.now(UTC) - started < timedelta(seconds=2)
    finally:
        if holder.in_transaction():
            holder.rollback()
        with holder.begin():
            assert holder.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": identity.action_advisory_key},
            ).scalar_one() is True
        holder.close()

    assert fake_post_calls == []


def test_post_consume_linearization_adaptive_lock_busy_cleans_partial_lock(
    db,
):
    """A concurrent risk writer wins; dispatch fails now and leaks no lock."""

    context = _consumed_transport_context(db)
    identity = transport.AdaptiveRiskAccountLockIdentity.for_scope(
        context["instruction"].account_scope
    )
    bound = db.get_bind()
    bind = getattr(bound, "engine", bound)
    holder = bind.connect()
    fake_post_calls = []
    try:
        with holder.begin():
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
        started = datetime.now(UTC)
        with pytest.raises(
            transport.CapturedPaperTransportUnavailable,
            match="transport_dispatch_adaptive_lock_unavailable",
        ):
            with context["store"].acquire_dispatch_linearization(
                context["instruction"],
                context["start"],
                context["invocation"],
                context["receipt"],
                context["pre_dispatch"],
                context["dispatch"],
            ):
                fake_post_calls.append(
                    context["instruction"].client_order_id
                )
        assert datetime.now(UTC) - started < timedelta(seconds=2)

        # The context acquired the action lock before discovering the adaptive
        # contention.  Prove that this partially acquired lock was released.
        with bind.connect() as probe:
            with probe.begin():
                assert probe.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": identity.action_advisory_key},
                ).scalar_one() is True
                assert probe.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": identity.action_advisory_key},
                ).scalar_one() is True
    finally:
        if holder.in_transaction():
            holder.rollback()
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
        holder.close()

    assert fake_post_calls == []


def test_post_consume_linearization_holds_no_open_transaction_during_io(db):
    context = _consumed_transport_context(db)
    identity = transport.AdaptiveRiskAccountLockIdentity.for_scope(
        context["instruction"].account_scope
    )
    bound = db.get_bind()
    bind = getattr(bound, "engine", bound)

    with context["store"].acquire_dispatch_linearization(
        context["instruction"],
        context["start"],
        context["invocation"],
        context["receipt"],
        context["pre_dispatch"],
        context["dispatch"],
    ):
        with bind.connect() as observer:
            rows = observer.execute(
                text(
                    """
                    SELECT a.state, a.xact_start
                      FROM pg_locks l
                      JOIN pg_stat_activity a ON a.pid = l.pid
                     WHERE l.locktype = 'advisory'
                       AND l.granted
                       AND l.objsubid = 2
                       AND l.classid::bigint = :namespace
                       AND l.objid::bigint =
                           (hashtext(:account_scope)::bigint & 4294967295)
                    """
                ),
                {
                    "namespace": identity.adaptive_advisory_namespace,
                    "account_scope": identity.account_scope,
                },
            ).all()
        assert rows == [("idle", None)]


def test_transport_indeterminate_is_reconciliation_only_and_never_terminalized(db):
    transport_calls = []

    def fake_post_then_lose_response(client_order_id):
        transport_calls.append(client_order_id)
        raise TimeoutError("accepted response was lost")

    request = _request()
    record = _persist(db, request, reconciliation_attempts=2)
    db.commit()
    authority = _seed_transport_binding(db, request)
    db.commit()
    leased = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert leased is not None
    db.commit()
    started = outbox.mark_captured_paper_transport_started(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=leased.lease_token,
        lease_owner_id=leased.lease_owner_id,
        authority=authority,
    )
    assert started.status == outbox.OUTBOX_STATUS_TRANSPORT_STARTED
    action = db.execute(
        text(
            "SELECT phase, metadata_json FROM broker_symbol_action_claims "
            "WHERE account_scope = :scope AND symbol = :symbol"
        ),
        {
            "scope": request.intent.route_token.account_scope,
            "symbol": request.intent.route_token.symbol,
        },
    ).mappings().one()
    marker = action["metadata_json"]["entry_transport_started"]
    assert action["phase"] == "submit_indeterminate"
    assert marker["client_order_id"] == request.intent.client_order_id
    assert marker["transport_authority_sha256"] == authority.authority_sha256
    assert datetime.fromisoformat(marker["started_at_utc"]) == (
        started.transport_started_at
    )
    db.commit()
    with pytest.raises(TimeoutError, match="accepted response was lost"):
        fake_post_then_lose_response(request.intent.client_order_id)
    indeterminate = outbox.mark_captured_paper_transport_indeterminate(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=leased.lease_token,
        lease_owner_id=leased.lease_owner_id,
        indeterminate_evidence_sha256="7" * 64,
    )
    assert indeterminate.status == outbox.OUTBOX_STATUS_TRANSPORT_INDETERMINATE
    db.commit()
    assert (
        outbox.lease_captured_paper_completion(
            db,
            completion_sha256=record.completion_sha256,
            lease_owner_id=OWNER_B,
            lease_seconds=30,
        )
        is None
    )

    for index in range(2):
        reconciliation = outbox.lease_captured_paper_indeterminate_reconciliation(
            db,
            completion_sha256=record.completion_sha256,
            lease_owner_id=OWNER_B,
            lease_seconds=30,
        )
        assert reconciliation is not None
        assert reconciliation.reconciliation_only is True
        db.commit()
        pending = outbox.mark_captured_paper_reconciliation_pending(
            db,
            completion_sha256=record.completion_sha256,
            lease_token=reconciliation.lease_token,
            lease_owner_id=reconciliation.lease_owner_id,
            reconciliation_evidence_sha256=("8" if index == 0 else "9") * 64,
        )
        assert pending.status == outbox.OUTBOX_STATUS_TRANSPORT_INDETERMINATE
        db.commit()
        if index == 0:
            db.execute(
                text(
                    "UPDATE captured_paper_post_commit_outbox "
                    "SET reconciliation_next_attempt_at = "
                    "clock_timestamp() - interval '1 second' "
                    "WHERE completion_sha256 = :completion_sha256"
                ),
                {"completion_sha256": record.completion_sha256},
            )
            db.commit()
    assert (
        outbox.lease_captured_paper_indeterminate_reconciliation(
            db,
            completion_sha256=record.completion_sha256,
            lease_owner_id=OWNER_A,
            lease_seconds=30,
        )
        is None
    )
    final = outbox.load_captured_paper_outbox(
        db, completion_sha256=record.completion_sha256
    )
    assert final.status == outbox.OUTBOX_STATUS_TRANSPORT_INDETERMINATE
    assert transport_calls == [request.intent.client_order_id]
    assert final.completed_at is None
    assert final.completion_proof_sha256 is None
    assert final.reconciliation_health_state == "escalated"
    assert final.reconciliation_escalation_count == 1
    assert final.reconciliation_total_attempt_count == 2
    assert final.reconciliation_attempt_count == 0
    assert final.reconciliation_next_attempt_at is not None
    assert final.events[-1].event_type == "reconciliation_health_escalated"


def test_expired_transport_marker_recovers_only_to_indeterminate(db):
    request = _request()
    record = _persist(db, request)
    db.commit()
    authority = _seed_transport_binding(db, request)
    db.commit()
    leased = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert leased is not None
    db.commit()
    outbox.mark_captured_paper_transport_started(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=leased.lease_token,
        lease_owner_id=leased.lease_owner_id,
        authority=authority,
    )
    db.commit()
    db.execute(
        text(
            "UPDATE captured_paper_post_commit_outbox "
            "SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE completion_sha256 = :completion_sha256"
        ),
        {"completion_sha256": record.completion_sha256},
    )
    assert outbox.recover_expired_captured_paper_leases(db, limit=10) == (
        record.completion_sha256,
    )
    recovered = outbox.load_captured_paper_outbox(
        db, completion_sha256=record.completion_sha256
    )
    assert recovered.status == outbox.OUTBOX_STATUS_TRANSPORT_INDETERMINATE
    assert recovered.indeterminate_evidence_sha256 is not None
    assert recovered.request.intent.client_order_id == (
        record.request.intent.client_order_id
    )
    assert (
        outbox.lease_next_captured_paper_completion(
            db, lease_owner_id=OWNER_B, lease_seconds=30
        )
        is None
    )


def test_restart_scanners_never_return_transport_marked_work_to_post_lane(db):
    request = _request()
    record = _persist(db, request)
    authority = _seed_transport_binding(db, request)
    db.commit()
    assert outbox.find_next_due_captured_paper_completion(db) == (
        record.completion_sha256
    )
    assert outbox.find_next_due_captured_paper_reconciliation(db) is None

    leased = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert leased is not None
    db.commit()
    outbox.mark_captured_paper_transport_started(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=leased.lease_token,
        lease_owner_id=leased.lease_owner_id,
        authority=authority,
    )
    db.commit()
    assert outbox.find_next_due_captured_paper_completion(db) is None

    db.execute(
        text(
            "UPDATE captured_paper_post_commit_outbox "
            "SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE completion_sha256 = :completion_sha256"
        ),
        {"completion_sha256": record.completion_sha256},
    )
    assert outbox.recover_expired_captured_paper_leases(db, limit=10) == (
        record.completion_sha256,
    )
    assert outbox.find_next_due_captured_paper_completion(db) is None
    assert outbox.find_next_due_captured_paper_reconciliation(db) == (
        record.completion_sha256
    )


def test_positive_fill_handoff_is_terminal_idempotent_and_never_reposted(
    db, monkeypatch
):
    request = _request()
    record = _persist(db, request)
    authority = _seed_transport_binding(db, request)
    db.commit()
    leased = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert leased is not None
    db.commit()
    outbox.mark_captured_paper_transport_started(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=leased.lease_token,
        lease_owner_id=leased.lease_owner_id,
        authority=authority,
    )
    db.commit()
    outbox.mark_captured_paper_transport_indeterminate(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=leased.lease_token,
        lease_owner_id=leased.lease_owner_id,
        indeterminate_evidence_sha256="0" * 64,
    )
    db.commit()

    proof = _active_fill_handoff_proof(
        request=request,
        authority=authority,
    )
    monkeypatch.setattr(
        alpaca_fill_activity,
        "verify_alpaca_paper_entry_fill_handoff",
        lambda session, supplied: supplied,
    )
    handed = outbox.commit_captured_paper_fill_handoff(
        db,
        completion_sha256=record.completion_sha256,
        authority=authority,
        proof=proof,
    )
    assert handed.status == outbox.OUTBOX_STATUS_FILL_HANDOFF_COMMITTED
    assert handed.fill_handoff_proof_sha256 == proof.proof_sha256
    assert handed.fill_handoff_receipt is not None
    assert handed.fill_handoff_receipt["broker_order_id"] == (
        proof.broker_order_id
    )
    assert handed.fill_handoff_receipt["terminal_evidence_sha256"] == (
        proof.terminal_evidence_sha256
    )
    assert handed.events[-1].event_type == "fill_handoff_committed"
    assert outbox.find_next_due_captured_paper_completion(db) is None
    assert outbox.find_next_due_captured_paper_reconciliation(db) is None
    with pytest.raises(
        outbox.CapturedPaperOutboxError,
        match="durable_transport_fill_handoff_work_not_loadable",
    ):
        outbox.load_captured_paper_durable_transport_bundle(
            db,
            completion_sha256=record.completion_sha256,
        )

    replayed = outbox.commit_captured_paper_fill_handoff(
        db,
        completion_sha256=record.completion_sha256,
        authority=authority,
        proof=proof,
    )
    assert replayed.version == handed.version
    assert replayed.event_sequence == handed.event_sequence

    conflicting = _active_fill_handoff_proof(
        request=request,
        authority=authority,
        suffix="e",
    )
    with pytest.raises(
        outbox.CapturedPaperOutboxConflictError,
        match="fill_handoff_idempotency_mismatch",
    ):
        outbox.commit_captured_paper_fill_handoff(
            db,
            completion_sha256=record.completion_sha256,
            authority=authority,
            proof=conflicting,
        )


def test_success_requires_typed_durable_authority_and_exact_lease(db):
    request = _request_active_at_database_now(db)
    record = _persist(db, request)
    db.commit()
    authority = _seed_transport_binding(db, request)
    db.commit()
    leased = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert leased is not None
    db.commit()
    outbox.mark_captured_paper_transport_started(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=leased.lease_token,
        lease_owner_id=leased.lease_owner_id,
        authority=authority,
    )
    db.commit()
    _record_allowed_financial_breaker(
        db,
        request=request,
        record=record,
        authority=authority,
        lease=leased,
    )
    acceptance = _mark_canonical_broker_accepted(
        db, request, authority, kind="post_response"
    )
    db.commit()
    completed = outbox.mark_captured_paper_completion_accepted(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=leased.lease_token,
        lease_owner_id=leased.lease_owner_id,
        authority=authority,
        acceptance=acceptance,
    )
    assert completed.status == outbox.OUTBOX_STATUS_COMPLETED
    assert completed.completion_proof_sha256 == acceptance.acceptance_sha256
    assert completed.lease_token is None


def test_completed_acceptance_hands_later_exact_fill_to_terminal_ownership(
    db,
    monkeypatch,
):
    """A later fill cannot reopen POST/reconciliation after direct acceptance."""

    request = _request_active_at_database_now(db)
    record = _persist(db, request)
    authority = _seed_transport_binding(db, request)
    db.commit()
    leased = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert leased is not None
    db.commit()
    outbox.mark_captured_paper_transport_started(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=leased.lease_token,
        lease_owner_id=leased.lease_owner_id,
        authority=authority,
    )
    db.commit()
    _record_allowed_financial_breaker(
        db,
        request=request,
        record=record,
        authority=authority,
        lease=leased,
    )
    acceptance = _mark_canonical_broker_accepted(
        db,
        request,
        authority,
        kind="post_response",
    )
    db.commit()
    completed = outbox.mark_captured_paper_completion_accepted(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=leased.lease_token,
        lease_owner_id=leased.lease_owner_id,
        authority=authority,
        acceptance=acceptance,
    )
    db.commit()
    assert completed.status == outbox.OUTBOX_STATUS_COMPLETED

    proof = _active_fill_handoff_proof(
        request=request,
        authority=authority,
    )
    monkeypatch.setattr(
        alpaca_fill_activity,
        "verify_alpaca_paper_entry_fill_handoff",
        lambda session, supplied: supplied,
    )
    with pytest.raises(
        outbox.CapturedPaperOutboxConflictError,
        match="fill_handoff_prior_completion_binding_mismatch",
    ):
        outbox.commit_captured_paper_fill_handoff(
            db,
            completion_sha256=record.completion_sha256,
            authority=authority,
            proof=replace(proof, broker_order_id="different-order"),
        )
    unchanged = outbox.load_captured_paper_outbox(
        db,
        completion_sha256=record.completion_sha256,
    )
    assert unchanged.status == outbox.OUTBOX_STATUS_COMPLETED

    handed = outbox.commit_captured_paper_fill_handoff(
        db,
        completion_sha256=record.completion_sha256,
        authority=authority,
        proof=proof,
    )
    assert handed.status == outbox.OUTBOX_STATUS_FILL_HANDOFF_COMMITTED
    assert handed.completion_proof_sha256 == acceptance.acceptance_sha256
    assert handed.completed_at == completed.completed_at
    assert handed.fill_handoff_receipt is not None
    assert handed.fill_handoff_receipt[
        "prior_completion_proof_sha256"
    ] == acceptance.acceptance_sha256
    assert handed.events[-2].event_type == "completion_accepted"
    assert handed.events[-1].event_type == "fill_handoff_committed"
    assert handed.events[-1].event_payload[
        "prior_completion_proof_sha256"
    ] == acceptance.acceptance_sha256
    assert outbox.find_next_due_captured_paper_completion(db) is None
    assert outbox.find_next_due_captured_paper_reconciliation(db) is None
    with pytest.raises(
        outbox.CapturedPaperOutboxError,
        match="durable_transport_fill_handoff_work_not_loadable",
    ):
        outbox.load_captured_paper_durable_transport_bundle(
            db,
            completion_sha256=record.completion_sha256,
        )

    replayed = outbox.commit_captured_paper_fill_handoff(
        db,
        completion_sha256=record.completion_sha256,
        authority=authority,
        proof=proof,
    )
    assert replayed.version == handed.version
    assert replayed.event_sequence == handed.event_sequence


def test_completed_fill_watch_leases_recovers_and_positive_handoff_excludes(
    db,
    monkeypatch,
):
    """Expired readers cannot clobber a successor or reopen a handed row."""

    request = _request_active_at_database_now(db)
    record = _persist(db, request)
    authority = _seed_transport_binding(db, request)
    db.commit()
    transport_lease = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert transport_lease is not None
    db.commit()
    outbox.mark_captured_paper_transport_started(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=transport_lease.lease_token,
        lease_owner_id=transport_lease.lease_owner_id,
        authority=authority,
    )
    db.commit()
    _record_allowed_financial_breaker(
        db,
        request=request,
        record=record,
        authority=authority,
        lease=transport_lease,
    )
    acceptance = _mark_canonical_broker_accepted(
        db,
        request,
        authority,
        kind="post_response",
    )
    db.commit()
    completed = outbox.mark_captured_paper_completion_accepted(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=transport_lease.lease_token,
        lease_owner_id=transport_lease.lease_owner_id,
        authority=authority,
        acceptance=acceptance,
    )
    db.commit()
    assert completed.status == outbox.OUTBOX_STATUS_COMPLETED
    watch = db.execute(
        text(
            """
            SELECT state, broker_order_id, broker_connection_generation,
                   completion_proof_sha256, event_sequence
              FROM captured_paper_completed_fill_watch
             WHERE completion_sha256 = :completion_sha256
            """
        ),
        {"completion_sha256": record.completion_sha256},
    ).mappings().one()
    assert watch["state"] == outbox.FILL_WATCH_STATE_PENDING
    assert watch["broker_order_id"] == acceptance.broker_order_id
    assert watch["completion_proof_sha256"] == acceptance.acceptance_sha256
    assert int(watch["event_sequence"]) == 1

    first = outbox.lease_next_captured_paper_completed_fill_watch(
        db,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert first is not None
    db.commit()
    bound = outbox.load_captured_paper_completed_fill_watch_bundle(
        db,
        lease=first,
    )
    assert bound.broker_order_id == acceptance.broker_order_id
    assert bound.completion_proof_sha256 == acceptance.acceptance_sha256
    db.commit()
    assert (
        outbox.lease_next_captured_paper_completed_fill_watch(
            db,
            lease_owner_id=OWNER_B,
            lease_seconds=30,
        )
        is None
    )

    # Simulate a crash after the exact read.  Only the expired successor lease
    # can mutate queue state; the stale owner is fenced by token+expiry.
    db.execute(
        text(
            """
            UPDATE captured_paper_completed_fill_watch
               SET lease_expires_at = clock_timestamp() - interval '1 second'
             WHERE completion_sha256 = :completion_sha256
            """
        ),
        {"completion_sha256": record.completion_sha256},
    )
    db.commit()
    second = outbox.lease_next_captured_paper_completed_fill_watch(
        db,
        lease_owner_id=OWNER_B,
        lease_seconds=30,
    )
    assert second is not None and second.recovered is True
    db.commit()
    with pytest.raises(
        outbox.CapturedPaperOutboxLeaseError,
        match="fill_watch_lease_mismatch",
    ):
        outbox.reschedule_captured_paper_completed_fill_watch(
            db,
            lease=first,
            observation_sha256="8" * 64,
            retry_delay_seconds=1,
            reason="stale_owner",
        )
    db.rollback()

    handoff_source = db.execute(
        text(
            """
            SELECT status, fill_handoff_proof_canonical_json,
                   fill_handoff_proof_sha256,
                   fill_handoff_receipt_canonical_json,
                   fill_handoff_receipt_sha256, fill_handoff_committed_at
              FROM captured_paper_post_commit_outbox
             WHERE completion_sha256 = :completion_sha256
            """
        ),
        {"completion_sha256": record.completion_sha256},
    ).mappings().one()
    assert handoff_source["status"] == outbox.OUTBOX_STATUS_COMPLETED
    assert all(
        handoff_source[name] is None
        for name in (
            "fill_handoff_proof_canonical_json",
            "fill_handoff_proof_sha256",
            "fill_handoff_receipt_canonical_json",
            "fill_handoff_receipt_sha256",
            "fill_handoff_committed_at",
        )
    )

    proof = _active_fill_handoff_proof(
        request=request,
        authority=authority,
    )
    monkeypatch.setattr(
        alpaca_fill_activity,
        "verify_alpaca_paper_entry_fill_handoff",
        lambda session, supplied: supplied,
    )
    handed = outbox.commit_captured_paper_fill_handoff(
        db,
        completion_sha256=record.completion_sha256,
        authority=authority,
        proof=proof,
    )
    db.commit()
    assert handed.status == outbox.OUTBOX_STATUS_FILL_HANDOFF_COMMITTED
    terminal = db.execute(
        text(
            """
            SELECT state, lease_token, lease_owner_id, lease_expires_at,
                   last_observation_sha256, terminal_receipt_sha256,
                   terminal_at
              FROM captured_paper_completed_fill_watch
             WHERE completion_sha256 = :completion_sha256
            """
        ),
        {"completion_sha256": record.completion_sha256},
    ).mappings().one()
    assert terminal["state"] == outbox.FILL_WATCH_STATE_HANDOFF_COMMITTED
    assert terminal["lease_token"] is None
    assert terminal["lease_owner_id"] is None
    assert terminal["lease_expires_at"] is None
    assert terminal["last_observation_sha256"] == proof.observation_sha256
    assert terminal["terminal_receipt_sha256"] == (
        handed.fill_handoff_receipt_sha256
    )
    assert terminal["terminal_at"] is not None
    assert (
        outbox.lease_next_captured_paper_completed_fill_watch(
            db,
            lease_owner_id=OWNER_A,
            lease_seconds=30,
        )
        is None
    )


def test_terminal_zero_fill_atomically_releases_risk_claim_and_watch(
    db,
    monkeypatch,
):
    """A claim-resolution failure rolls back the preceding risk release."""

    request = _request_active_at_database_now(db)
    record = _persist(db, request)
    authority = _seed_transport_binding(db, request)
    db.commit()
    transport_lease = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=300,
    )
    assert transport_lease is not None
    db.commit()
    outbox.mark_captured_paper_transport_started(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=transport_lease.lease_token,
        lease_owner_id=transport_lease.lease_owner_id,
        authority=authority,
    )
    db.commit()
    _record_allowed_financial_breaker(
        db,
        request=request,
        record=record,
        authority=authority,
        lease=transport_lease,
    )
    acceptance = _mark_canonical_broker_accepted(
        db, request, authority, kind="post_response"
    )
    db.commit()
    outbox.mark_captured_paper_completion_accepted(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=transport_lease.lease_token,
        lease_owner_id=transport_lease.lease_owner_id,
        authority=authority,
        acceptance=acceptance,
    )
    db.commit()

    store = fill_watch.SqlAlchemyCapturedPaperCompletedFillWatchStore(engine)
    lease = store.lease_next(
        lease_owner_id=OWNER_A,
        lease_seconds=300,
    )
    assert lease is not None
    instruction = store.load_instruction(lease)

    observed_at = datetime(2036, 7, 15, 16, 32, tzinfo=UTC)
    order_available_at = datetime(2036, 7, 15, 16, 32, 1, tzinfo=UTC)
    query_after = datetime(2036, 7, 15, 15, 30, tzinfo=UTC)
    query_until = datetime(2036, 7, 15, 16, 32, 1, tzinfo=UTC)
    received_at = datetime(2036, 7, 15, 16, 32, 2, tzinfo=UTC)
    available_at = datetime(2036, 7, 15, 16, 32, 3, tzinfo=UTC)
    expires_at = datetime(2036, 7, 15, 16, 32, 13, tzinfo=UTC)
    provider_order = {
        "id": instruction.broker_order_id,
        "client_order_id": authority.client_order_id,
        "account_id": ACCOUNT_ID,
        "symbol": authority.symbol,
        "side": "buy",
        "status": "canceled",
        "qty": "100.0000000000",
        "filled_qty": "0.0000000000",
        "asset_class": "us_equity",
    }
    provider_order_json = _canonical_json(provider_order)
    provider_order_sha256 = hashlib.sha256(
        provider_order_json.encode("utf-8")
    ).hexdigest()
    read_binding = {
        "schema_version": "chili.alpaca-paper-fill-read-binding.v1",
        "reservation_id": authority.reservation_id,
        "provider_order_id": instruction.broker_order_id,
        "expected_client_order_id": authority.client_order_id,
        "order_role": "entry",
    }
    read_binding_json = _canonical_json(read_binding)
    read_binding_sha256 = hashlib.sha256(
        read_binding_json.encode("utf-8")
    ).hexdigest()
    request_json = _canonical_json(
        {"method": "GET", "path": "/account/activities"}
    )
    response_json = "[]"
    request_sha256 = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
    response_sha256 = hashlib.sha256(response_json.encode("utf-8")).hexdigest()
    page_object_body = {
        "page_schema_version": "chili.alpaca-paper-fill-page-object.v1",
        "request_sha256": request_sha256,
        "response_sha256": response_sha256,
        "response_count": 0,
    }
    page_object_sha256 = hashlib.sha256(
        _canonical_json(page_object_body).encode("utf-8")
    ).hexdigest()
    page = {
        "page_index": 0,
        "request_page_token": None,
        "next_page_token": None,
        "requested_at": query_until.isoformat(),
        "received_at": received_at.isoformat(),
        "available_at": available_at.isoformat(),
        "terminal": True,
        "request_canonical_json": request_json,
        "request_sha256": request_sha256,
        "response_canonical_json": response_json,
        "response_sha256": response_sha256,
        "response_count": 0,
    }
    query_receipt = {
        "schema_version": "chili.alpaca-paper-fill-query-receipt.v1",
        "broker_environment": "paper",
        "asset_class": "us_equity",
        "provider_account_id": ACCOUNT_ID,
        "provider_order_id": instruction.broker_order_id,
        "provider_order_payload_sha256": provider_order_sha256,
        "read_binding_sha256": read_binding_sha256,
        "adapter_connection_generation": (
            instruction.broker_connection_generation
        ),
        "adapter_build_sha256": "a" * 64,
        "method": "GET",
        "path": "/account/activities",
        "api_version": "v2",
        "query_after": query_after.isoformat(),
        "query_until": query_until.isoformat(),
        "direction": "asc",
        "page_size": 100,
        "max_pages": 100,
        "pages": [page],
    }
    query_receipt_json = _canonical_json(query_receipt)
    query_receipt_sha256 = hashlib.sha256(
        query_receipt_json.encode("utf-8")
    ).hexdigest()
    observation_content = {
        "schema_version": "chili.test-empty-fill-observation.v1",
        "provider_order_payload_sha256": provider_order_sha256,
        "read_binding_sha256": read_binding_sha256,
        "query_receipt_sha256": query_receipt_sha256,
        "activities": [],
    }
    observation_json = _canonical_json(observation_content)
    observation_sha256 = hashlib.sha256(
        observation_json.encode("utf-8")
    ).hexdigest()
    mapping_body = {
        "observation_sha256": observation_sha256,
        "page_index": 0,
        "page_object_sha256": page_object_sha256,
        "request_page_token": None,
        "next_page_token": None,
        "requested_at": query_until.isoformat(),
        "received_at": received_at.isoformat(),
        "available_at": available_at.isoformat(),
        "terminal": True,
    }
    mapping_sha256 = hashlib.sha256(
        _canonical_json(mapping_body).encode("utf-8")
    ).hexdigest()
    db.execute(
        text(
            """
            INSERT INTO alpaca_paper_fill_query_observations (
                observation_sha256, observation_schema_version,
                observation_authority_status, reservation_id,
                decision_packet_sha256, account_scope,
                account_identity_sha256, provider_account_id_sha256,
                broker_environment, asset_class, execution_family,
                position_direction, symbol, provider_order_id,
                expected_client_order_id, order_role,
                account_snapshot_generation,
                cycle_broker_connection_generation,
                adapter_connection_generation, adapter_build_sha256,
                provider_order_payload_canonical_json,
                provider_order_payload_sha256,
                read_binding_canonical_json, read_binding_sha256,
                query_receipt_canonical_json, query_receipt_sha256,
                observation_content_canonical_json,
                observation_content_sha256, query_after, query_until,
                received_at, available_at, expires_at,
                exact_activity_count, page_count, pagination_complete,
                pagination_scope
            ) VALUES (
                :observation_sha256,
                'chili.alpaca-paper-fill-query-observation.v1', 'verified',
                CAST(:reservation_id AS UUID), :decision_packet_sha256,
                'alpaca:paper', :identity, :identity, 'paper',
                'us_equity', 'alpaca_spot', 'long', :symbol,
                :provider_order_id, :expected_client_order_id, 'entry',
                'account-generation-1', :connection_generation,
                :connection_generation, :adapter_build_sha256,
                :provider_order_json, :provider_order_sha256,
                :read_binding_json, :read_binding_sha256,
                :query_receipt_json, :query_receipt_sha256,
                :observation_json, :observation_sha256,
                :query_after, :query_until, :received_at, :available_at,
                :expires_at, 0, 1, TRUE,
                'pagination_only_not_fill_absence_or_economic_completeness'
            )
            """
        ),
        {
            "observation_sha256": observation_sha256,
            "reservation_id": authority.reservation_id,
            "decision_packet_sha256": authority.decision_packet_sha256,
            "identity": authority.account_identity_sha256,
            "symbol": authority.symbol,
            "provider_order_id": instruction.broker_order_id,
            "expected_client_order_id": authority.client_order_id,
            "connection_generation": instruction.broker_connection_generation,
            "adapter_build_sha256": "a" * 64,
            "provider_order_json": provider_order_json,
            "provider_order_sha256": provider_order_sha256,
            "read_binding_json": read_binding_json,
            "read_binding_sha256": read_binding_sha256,
            "query_receipt_json": query_receipt_json,
            "query_receipt_sha256": query_receipt_sha256,
            "observation_json": observation_json,
            "query_after": query_after,
            "query_until": query_until,
            "received_at": received_at,
            "available_at": available_at,
            "expires_at": expires_at,
        },
    )
    db.execute(
        text(
            """
            INSERT INTO alpaca_paper_fill_page_objects (
                page_object_sha256, page_schema_version,
                request_canonical_json, request_sha256,
                response_canonical_json, response_sha256, response_count
            ) VALUES (
                :page_object_sha256,
                'chili.alpaca-paper-fill-page-object.v1',
                :request_json, :request_sha256,
                :response_json, :response_sha256, 0
            )
            """
        ),
        {
            "page_object_sha256": page_object_sha256,
            "request_json": request_json,
            "request_sha256": request_sha256,
            "response_json": response_json,
            "response_sha256": response_sha256,
        },
    )
    db.execute(
        text(
            """
            INSERT INTO alpaca_paper_fill_observation_pages (
                observation_sha256, page_index, page_object_sha256,
                request_page_token, next_page_token, requested_at,
                received_at, available_at, terminal, mapping_sha256
            ) VALUES (
                :observation_sha256, 0, :page_object_sha256,
                NULL, NULL, :requested_at, :received_at, :available_at,
                TRUE, :mapping_sha256
            )
            """
        ),
        {
            "observation_sha256": observation_sha256,
            "page_object_sha256": page_object_sha256,
            "requested_at": query_until,
            "received_at": received_at,
            "available_at": available_at,
            "mapping_sha256": mapping_sha256,
        },
    )
    db.commit()

    exact = transport.CapturedPaperExactBrokerOrderObservation(
        account_scope=instruction.transport_instruction.account_scope,
        expected_account_id=instruction.transport_instruction.expected_account_id,
        verified_adapter_account_id=(
            instruction.transport_instruction.expected_account_id
        ),
        account_binding_source=(
            transport.EXACT_PAPER_ACCOUNT_BINDING_SOURCE
        ),
        broker_account_id=instruction.transport_instruction.expected_account_id,
        client_order_id=instruction.transport_instruction.client_order_id,
        broker_order_id=instruction.broker_order_id,
        symbol=instruction.transport_instruction.symbol,
        side="buy",
        order_type="limit",
        asset_class="us_equity",
        quantity_shares=instruction.transport_instruction.quantity_shares,
        broker_quantity_echo=str(
            instruction.transport_instruction.quantity_shares
        ),
        broker_filled_quantity_echo="0",
        cumulative_filled_quantity_shares=0,
        limit_price=instruction.transport_instruction.limit_price,
        broker_limit_price_echo=instruction.transport_instruction.limit_price,
        time_in_force=instruction.transport_instruction.time_in_force,
        extended_hours=instruction.transport_instruction.extended_hours,
        position_intent_echo="buy_to_open",
        broker_order_status="canceled",
        broker_order_status_echo="canceled",
        broker_connection_generation=instruction.broker_connection_generation,
        broker_order_evidence_sha256="d" * 64,
        observed_at=observed_at,
        available_at=order_available_at,
    )
    terminal = transport.CapturedPaperTerminalZeroFillObservation(order=exact)
    read = transport.CapturedPaperFillReadAuthority(
        account_scope=instruction.transport_instruction.account_scope,
        expected_account_id=instruction.transport_instruction.expected_account_id,
        reservation_id=authority.reservation_id,
        client_order_id=authority.client_order_id,
        broker_order_id=instruction.broker_order_id,
        query_receipt_sha256=query_receipt_sha256,
        observation_sha256=observation_sha256,
        exact_activity_count=0,
        positive_fill_observed=False,
        pagination_complete=True,
        available_at=available_at,
    )
    append_receipt = transport.CapturedPaperFillAppendReceipt(
        observation_sha256=observation_sha256,
        durable_receipt_sha256=observation_sha256,
        committed_at=available_at,
    )
    monkeypatch.setattr(
        fill_watch.AdaptiveRiskReservationStore,
        "_clock",
        staticmethod(lambda _session: available_at),
    )

    # An unresolved protective-owner marker must retain every resource.  The
    # release performed earlier in the transaction is rolled back too.
    db.execute(
        text(
            """
            UPDATE broker_symbol_action_claims
               SET metadata_json = metadata_json ||
                   '{"owner_transport":{"phase":"pending"}}'::jsonb
             WHERE account_scope = 'alpaca:paper' AND symbol = 'ACTU'
            """
        )
    )
    db.commit()
    with pytest.raises(
        fill_watch.CapturedPaperCompletedFillWatchError,
        match="terminal_action_claim_unresolved",
    ):
        store.complete_terminal_zero_fill(
            instruction,
            observation=terminal,
            read=read,
            append_receipt=append_receipt,
        )
    retained = db.execute(
        text(
            """
            SELECT r.state AS reservation_state, c.phase AS claim_phase,
                   w.state AS watch_state
              FROM adaptive_risk_reservations r
              JOIN broker_symbol_action_claims c
                ON c.account_scope = r.account_scope
               AND upper(c.symbol) = upper(r.symbol)
              JOIN captured_paper_completed_fill_watch w
                ON w.completion_sha256 = :completion_sha256
             WHERE r.reservation_id = CAST(:reservation_id AS UUID)
            """
        ),
        {
            "completion_sha256": record.completion_sha256,
            "reservation_id": authority.reservation_id,
        },
    ).mappings().one()
    assert retained == {
        "reservation_state": "submitted",
        "claim_phase": "submitted",
        "watch_state": "leased",
    }
    db.execute(
        text(
            """
            UPDATE broker_symbol_action_claims
               SET metadata_json = metadata_json - 'owner_transport'
             WHERE account_scope = 'alpaca:paper' AND symbol = 'ACTU'
            """
        )
    )
    db.commit()

    terminal_receipt = store.complete_terminal_zero_fill(
        instruction,
        observation=terminal,
        read=read,
        append_receipt=append_receipt,
    )
    settled = db.execute(
        text(
            """
            SELECT r.state AS reservation_state, r.release_reason,
                   c.phase AS claim_phase, w.state AS watch_state,
                   w.terminal_receipt_sha256, o.status AS opportunity_status
              FROM adaptive_risk_reservations r
              JOIN broker_symbol_action_claims c
                ON c.account_scope = r.account_scope
               AND upper(c.symbol) = upper(r.symbol)
              JOIN captured_paper_completed_fill_watch w
                ON w.completion_sha256 = :completion_sha256
              JOIN adaptive_risk_opportunity_claims o
                ON o.id = r.opportunity_claim_id
             WHERE r.reservation_id = CAST(:reservation_id AS UUID)
            """
        ),
        {
            "completion_sha256": record.completion_sha256,
            "reservation_id": authority.reservation_id,
        },
    ).mappings().one()
    assert settled["reservation_state"] == "released"
    assert settled["release_reason"] == "broker_canceled"
    assert settled["claim_phase"] == "resolved"
    assert (
        settled["watch_state"]
        == outbox.FILL_WATCH_STATE_TERMINAL_ZERO_FILL
    )
    assert settled["terminal_receipt_sha256"] == terminal_receipt
    assert settled["opportunity_status"] == "available"
    db.rollback()

def test_transport_start_fails_closed_without_phase_two_durable_rows(db):
    request = _request()
    record = _persist(db, request)
    db.commit()
    leased = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert leased is not None
    db.commit()

    with pytest.raises(
        outbox.CapturedPaperOutboxError,
        match="transport_authority_account_head_mismatch",
    ):
        outbox.mark_captured_paper_transport_started(
            db,
            completion_sha256=record.completion_sha256,
            lease_token=leased.lease_token,
            lease_owner_id=leased.lease_owner_id,
            authority=_transport_authority(request),
        )

    unchanged = outbox.load_captured_paper_outbox(
        db, completion_sha256=record.completion_sha256
    )
    assert unchanged.status == outbox.OUTBOX_STATUS_LEASED
    assert unchanged.transport_started_at is None
    assert [event.event_type for event in unchanged.events] == [
        "enqueued",
        "leased",
    ]


def test_transport_start_rejects_expired_lease_at_consumption_time(db):
    request = _request()
    record = _persist(db, request)
    db.commit()
    authority = _seed_transport_binding(db, request)
    db.commit()
    leased = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert leased is not None
    db.execute(
        text(
            "UPDATE captured_paper_post_commit_outbox "
            "SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE completion_sha256 = :completion_sha256"
        ),
        {"completion_sha256": record.completion_sha256},
    )
    db.commit()

    with pytest.raises(
        outbox.CapturedPaperOutboxLeaseError,
        match="outbox_transport_start_lease_expired",
    ):
        outbox.mark_captured_paper_transport_started(
            db,
            completion_sha256=record.completion_sha256,
            lease_token=leased.lease_token,
            lease_owner_id=leased.lease_owner_id,
            authority=authority,
        )

    unchanged = outbox.load_captured_paper_outbox(
        db, completion_sha256=record.completion_sha256
    )
    assert unchanged.status == outbox.OUTBOX_STATUS_LEASED
    assert unchanged.transport_started_at is None


def test_transport_start_lease_mismatch_rolls_back_action_claim_and_marker(db):
    request = _request()
    record = _persist(db, request)
    db.commit()
    authority = _seed_transport_binding(db, request)
    db.commit()
    leased = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert leased is not None
    db.commit()

    with pytest.raises(
        outbox.CapturedPaperOutboxLeaseError,
        match="outbox_lease_owner_mismatch",
    ):
        outbox.mark_captured_paper_transport_started(
            db,
            completion_sha256=record.completion_sha256,
            lease_token=leased.lease_token,
            lease_owner_id=OWNER_B,
            authority=authority,
        )

    action = db.execute(
        text(
            "SELECT phase, metadata_json FROM broker_symbol_action_claims "
            "WHERE account_scope = :scope AND symbol = :symbol"
        ),
        {
            "scope": request.intent.route_token.account_scope,
            "symbol": request.intent.route_token.symbol,
        },
    ).mappings().one()
    unchanged = outbox.load_captured_paper_outbox(
        db, completion_sha256=record.completion_sha256
    )
    assert action["phase"] == "claimed"
    assert "entry_transport_started" not in action["metadata_json"]
    assert unchanged.status == outbox.OUTBOX_STATUS_LEASED
    assert unchanged.transport_started_at is None


def test_positive_adoption_uses_started_fence_after_arm_expiry_and_owner_end(
    db, monkeypatch
):
    confirmed_at = datetime(2020, 7, 15, 16, 0, tzinfo=UTC)
    expires_at = datetime(2020, 7, 15, 17, 0, tzinfo=UTC)
    started_at = datetime(2020, 7, 15, 16, 59, 59, tzinfo=UTC)
    request = _request(
        arm_confirmed_at=confirmed_at,
        arm_expires_at=expires_at,
        decision_at=datetime(2020, 7, 15, 16, 30, tzinfo=UTC),
    )
    record = _persist(db, request)
    authority = _seed_transport_binding(db, request)
    db.commit()
    leased = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert leased is not None
    db.commit()

    monkeypatch.setattr(outbox, "_db_now", lambda unused_db: started_at)
    outbox.mark_captured_paper_transport_started(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=leased.lease_token,
        lease_owner_id=leased.lease_owner_id,
        authority=authority,
    )
    db.execute(
        text(
            "UPDATE trading_automation_sessions "
            "SET ended_at = clock_timestamp() WHERE id = :session_id"
        ),
        {"session_id": request.intent.route_token.session_id},
    )
    db.commit()

    receipt = outbox.lock_captured_paper_positive_adoption(
        db,
        completion_sha256=record.completion_sha256,
        authority=authority,
        acceptance_kind="post_response",
    )

    assert receipt.binding_state == "pending_unbound"
    assert receipt.transport_started_at == started_at
    assert receipt.transport_started_at <= expires_at
    assert receipt.session_ended is True
    assert receipt.outbox_status == outbox.OUTBOX_STATUS_TRANSPORT_STARTED


def test_positive_same_cid_reconciliation_completes_and_retains_lineage(db):
    request = _request()
    record = _persist(db, request)
    db.commit()
    authority = _seed_transport_binding(db, request)
    db.commit()
    leased = outbox.lease_captured_paper_completion(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    assert leased is not None
    db.commit()
    outbox.mark_captured_paper_transport_started(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=leased.lease_token,
        lease_owner_id=leased.lease_owner_id,
        authority=authority,
    )
    db.commit()
    indeterminate = outbox.mark_captured_paper_transport_indeterminate(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=leased.lease_token,
        lease_owner_id=leased.lease_owner_id,
        indeterminate_evidence_sha256="0" * 64,
    )
    db.commit()
    reconciliation = outbox.lease_captured_paper_indeterminate_reconciliation(
        db,
        completion_sha256=record.completion_sha256,
        lease_owner_id=OWNER_B,
        lease_seconds=30,
    )
    assert reconciliation is not None
    db.commit()
    acceptance = _mark_canonical_broker_accepted(
        db,
        request,
        authority,
        kind="same_cid_reconciliation",
    )
    db.commit()
    completed = outbox.mark_captured_paper_reconciliation_accepted(
        db,
        completion_sha256=record.completion_sha256,
        lease_token=reconciliation.lease_token,
        lease_owner_id=reconciliation.lease_owner_id,
        authority=authority,
        acceptance=acceptance,
    )

    assert completed.status == outbox.OUTBOX_STATUS_COMPLETED
    assert completed.transport_indeterminate_at == (
        indeterminate.transport_indeterminate_at
    )
    assert completed.indeterminate_evidence_sha256 == "0" * 64
    assert completed.completion_proof_sha256 == acceptance.acceptance_sha256
    assert completed.last_reconciliation_evidence_sha256 == (
        acceptance.acceptance_sha256
    )
    assert completed.events[-1].event_type == "reconciliation_accepted"


def test_content_and_event_rows_are_database_immutable(db):
    record = _persist(db)
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            db.execute(
                text(
                    "UPDATE captured_paper_post_commit_outbox "
                    "SET payload_canonical_json = '{}' "
                    "WHERE completion_sha256 = :completion_sha256"
                ),
                {"completion_sha256": record.completion_sha256},
            )
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            db.execute(
                text(
                    "UPDATE captured_paper_post_commit_outbox "
                    "SET order_request_canonical_json = '{}' "
                    "WHERE completion_sha256 = :completion_sha256"
                ),
                {"completion_sha256": record.completion_sha256},
            )
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            db.execute(
                text(
                    "DELETE FROM captured_paper_post_commit_outbox_events "
                    "WHERE completion_sha256 = :completion_sha256"
                ),
                {"completion_sha256": record.completion_sha256},
            )
    reloaded = outbox.load_captured_paper_outbox(
        db, completion_sha256=record.completion_sha256
    )
    assert reloaded.payload_sha256 == record.payload_sha256
    assert len(reloaded.events) == 1


def test_concurrent_workers_cannot_both_lease_one_completion(db):
    record = _persist(db)
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    barrier = threading.Barrier(2)

    def worker(owner_id):
        session = factory()
        try:
            barrier.wait(timeout=5)
            lease = outbox.lease_captured_paper_completion(
                session,
                completion_sha256=record.completion_sha256,
                lease_owner_id=owner_id,
                lease_seconds=30,
            )
            session.commit()
            return lease.lease_owner_id if lease is not None else None
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, (OWNER_A, OWNER_B)))
    assert sorted(result for result in results if result is not None) in (
        [OWNER_A],
        [OWNER_B],
    )

    check = factory()
    try:
        reloaded = outbox.load_captured_paper_outbox(
            check, completion_sha256=record.completion_sha256
        )
        assert reloaded.status == outbox.OUTBOX_STATUS_LEASED
        assert reloaded.attempt_count == 1
        assert [event.event_type for event in reloaded.events] == [
            "enqueued",
            "leased",
        ]
    finally:
        check.close()


def test_migration_337_is_registered_idempotent_and_installs_guards(db):
    frozen_337 = (
        "337_captured_paper_post_commit_outbox",
        migrations._migration_337_captured_paper_post_commit_outbox,
    )
    authority_338 = (
        "338_captured_paper_outbox_authority_hardening",
        migrations._migration_338_captured_paper_outbox_authority_hardening,
    )
    assert frozen_337 in migrations.MIGRATIONS
    assert authority_338 in migrations.MIGRATIONS
    assert migrations.MIGRATIONS.index(authority_338) == (
        migrations.MIGRATIONS.index(frozen_337) + 1
    )
    durable_342 = (
        "342_captured_paper_durable_transport_instruction",
        migrations._migration_342_captured_paper_durable_transport_instruction,
    )
    assert durable_342 in migrations.MIGRATIONS
    fill_handoff_344 = (
        "344_captured_paper_positive_fill_handoff",
        migrations._migration_344_captured_paper_positive_fill_handoff,
    )
    assert fill_handoff_344 in migrations.MIGRATIONS
    assert migrations.MIGRATIONS.index(fill_handoff_344) > (
        migrations.MIGRATIONS.index(durable_342)
    )
    fill_watch_345 = (
        "345_captured_paper_completed_fill_watch",
        migrations._migration_345_captured_paper_completed_fill_watch,
    )
    guard_repair_346 = (
        "346_captured_paper_fill_watch_guard_repair",
        migrations._migration_346_captured_paper_fill_watch_guard_repair,
    )
    assert fill_watch_345 in migrations.MIGRATIONS
    assert guard_repair_346 in migrations.MIGRATIONS
    assert migrations.MIGRATIONS.index(fill_watch_345) == (
        migrations.MIGRATIONS.index(fill_handoff_344) + 1
    )
    assert migrations.MIGRATIONS.index(guard_repair_346) == (
        migrations.MIGRATIONS.index(fill_watch_345) + 1
    )
    migrations._migration_337_captured_paper_post_commit_outbox(db.connection())
    migrations._migration_337_captured_paper_post_commit_outbox(db.connection())
    migrations._migration_338_captured_paper_outbox_authority_hardening(
        db.connection()
    )
    migrations._migration_342_captured_paper_durable_transport_instruction(
        db.connection()
    )
    migrations._migration_344_captured_paper_positive_fill_handoff(
        db.connection()
    )
    migrations._migration_345_captured_paper_completed_fill_watch(
        db.connection()
    )
    migrations._migration_345_captured_paper_completed_fill_watch(
        db.connection()
    )
    migrations._migration_346_captured_paper_fill_watch_guard_repair(
        db.connection()
    )
    migrations._migration_346_captured_paper_fill_watch_guard_repair(
        db.connection()
    )
    migrations._migration_344_captured_paper_positive_fill_handoff(
        db.connection()
    )
    migrations._migration_342_captured_paper_durable_transport_instruction(
        db.connection()
    )
    migrations._migration_338_captured_paper_outbox_authority_hardening(
        db.connection()
    )
    columns = {
        row[0]
        for row in db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'captured_paper_post_commit_outbox'"
            )
        ).all()
    }
    assert {
        "payload_canonical_json",
        "binder_id",
        "symbol_claim_token",
        "confirmed_arm_generation_sha256",
        "opportunity_key_sha256",
        "transport_started_at",
        "transport_indeterminate_at",
        "order_request_canonical_json",
        "transport_authority_canonical_json",
        "committed_admission_canonical_json",
        "transport_instruction_canonical_json",
        "reconciliation_total_attempt_count",
        "reconciliation_health_state",
        "fill_handoff_proof_canonical_json",
        "fill_handoff_proof_sha256",
        "fill_handoff_receipt_canonical_json",
        "fill_handoff_receipt_sha256",
        "fill_handoff_committed_at",
    } <= columns
    tables = {
        row[0]
        for row in db.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() "
                "AND table_name IN ("
                "'captured_paper_completed_fill_watch',"
                "'captured_paper_completed_fill_watch_events')"
            )
        ).all()
    }
    assert tables == {
        "captured_paper_completed_fill_watch",
        "captured_paper_completed_fill_watch_events",
    }
    triggers = {
        row[0]
        for row in db.execute(
            text(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgrelid IN ("
                "  'captured_paper_post_commit_outbox'::regclass,"
                "  'captured_paper_post_commit_outbox_events'::regclass,"
                "  'captured_paper_completed_fill_watch'::regclass,"
                "  'captured_paper_completed_fill_watch_events'::regclass"
                ") AND NOT tgisinternal"
            )
        ).all()
    }
    assert {
        "trg_captured_paper_outbox_content_immutable",
        "trg_captured_paper_outbox_event_append",
        "trg_captured_paper_fill_handoff_transition",
        "trg_captured_paper_completed_fill_watch",
        "trg_captured_paper_fill_watch_event_append_only",
    } <= triggers
    marker_constraint = db.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = "
            "'captured_paper_post_commit_outbox'::regclass "
            "AND conname = 'ck_captured_paper_outbox_indeterminate_marker'"
        )
    ).scalar_one()
    assert "completed" in marker_constraint
    completion_constraint = db.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = "
            "'captured_paper_post_commit_outbox'::regclass "
            "AND conname = 'ck_captured_paper_outbox_completion_marker'"
        )
    ).scalar_one()
    assert "fill_handoff_committed" in completion_constraint
    watch_guard = db.execute(
        text(
            "SELECT pg_get_functiondef("
            "'guard_captured_paper_completed_fill_watch()'::regprocedure)"
        )
    ).scalar_one()
    assert "NEW IS DISTINCT FROM OLD" in watch_guard


def test_clean_bootstrap_migration_327_defines_guard_before_trigger():
    source = inspect.getsource(
        migrations._migration_327_alpaca_paper_fill_activity_capture
    )
    function_marker = (
        "CREATE OR REPLACE FUNCTION "
        "chili_guard_alpaca_fill_activity_insert()"
    )
    trigger_marker = (
        "CREATE TRIGGER trg_alpaca_paper_fill_activity_cycle_guard"
    )

    assert function_marker in source
    assert trigger_marker in source
    assert source.index(function_marker) < source.index(trigger_marker)
    for required_binding in (
        "reservation_row.state = 'closed'",
        "packet_row.account_identity_sha256",
        "reservation_row.broker_connection_generation",
        "packet_row.client_order_id",
        "first fill activity must start a contiguous chain",
    ):
        assert required_binding in source


def test_outbox_module_has_no_broker_provider_or_admission_capability():
    tree = ast.parse(inspect.getsource(outbox))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported.isdisjoint(
        {
            "AlpacaSpotAdapter",
            "AdaptiveRiskReservationStore",
            "BrokerSymbolActionClaim",
            "AdaptiveRiskOpportunityClaim",
            "requests",
            "httpx",
        }
    )
    source = inspect.getsource(outbox)
    assert "clock_timestamp()" in source
    assert "datetime.now(" not in source
    assert ".commit(" not in source
    assert "transport_evidence_sha256" not in inspect.signature(
        outbox.mark_captured_paper_transport_started
    ).parameters
    assert "completion_proof_sha256" not in inspect.signature(
        outbox.mark_captured_paper_completion_accepted
    ).parameters
    assert "authority" in inspect.signature(
        outbox.mark_captured_paper_transport_started
    ).parameters
    assert "acceptance" in inspect.signature(
        outbox.mark_captured_paper_reconciliation_accepted
    ).parameters
