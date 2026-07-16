from __future__ import annotations

import ast
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import threading
from types import SimpleNamespace
import uuid
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.models.trading import (
    AdaptiveRiskDecisionPacket,
    AdaptiveRiskOpportunityClaim,
    AdaptiveRiskReservation,
    MomentumStrategyVariant,
    TradingAutomationSession,
    TradingAutomationSimulatedFill,
)
from app.services.trading.momentum_neural import paper_runner
from app.services.trading.momentum_neural import (
    adaptive_risk_policy as risk_policy_module,
)
from app.services.trading.momentum_neural import (
    adaptive_risk_request_builder as risk_builder_module,
)
from app.services.trading.momentum_neural import (
    adaptive_risk_reservation as risk_reservation_module,
)
from app.services.trading.momentum_neural.adaptive_risk_policy import (
    AdaptiveRiskContractError,
    RiskInputEvidence,
)
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    AdaptiveRiskPendingSettlement,
    AdaptiveRiskReservationStore,
    AdaptiveRiskReservationRequest,
    LockedAdaptiveRiskAdmissionSnapshot,
    RESERVATION_LEDGER_GENERATION,
    load_adaptive_risk_reservation_request,
)
from app.services.trading.momentum_neural.adaptive_risk_request_builder import (
    AdaptiveRiskBuilderError,
    AdaptiveRiskBuilderSource,
    AdaptiveRiskDiagnosticCaptureBinding,
    DbPaperFinalAdmissionBundle,
    DbPaperFinalAdmissionMaterial,
    DbPaperFinalAdmissionObservation,
    db_paper_admission_component_sha256,
    db_paper_bbo_evidence_payload,
    db_paper_eligibility_evidence_payload,
    db_paper_entry_gate_evidence_payload,
    db_paper_execution_terms_payload,
    load_db_paper_admission_receipt,
    load_db_paper_executable_admission,
)
from tests.test_adaptive_risk_reservation import _hash, _inputs, _policy, _snapshot


ENTRY_PRICE = 10.01
REFERENCE_PRICE = 9.995
STOP_PRICE = 9.5
TARGET_PRICE = 11.0
EFFECTIVE_ATR = 0.05
FEE_RATIO = 0.08
DEFAULT_ACCOUNT_SCOPE = "db-paper:fixture-account"


def _db_paper_request(
    *,
    account_scope: str = DEFAULT_ACCOUNT_SCOPE,
    decision_id: str = "db-paper-veee-entry-1",
    account_snapshot=None,
    now: datetime | None = None,
    setup_family: str = "momentum_pullback",
) -> AdaptiveRiskReservationRequest:
    now = now or datetime.now(timezone.utc)
    account = replace(
        account_snapshot or _snapshot(account_scope=account_scope),
        observed_at=now - timedelta(milliseconds=40),
        available_at=now - timedelta(milliseconds=20),
    )
    inputs = _inputs(
        account,
        symbol="VEEE",
        decision_id=decision_id,
        cluster="equity:momentum-v",
    )
    evidence = {
        name: (
            value
            if name in {"account", "daily_pnl"}
            else replace(
                value,
                observed_at=now - timedelta(milliseconds=22),
                available_at=now - timedelta(milliseconds=20),
            )
        )
        for name, value in inputs.evidence.items()
    }
    inputs = replace(
        inputs,
        execution_surface="db_paper",
        as_of=now,
        evidence=evidence,
    )
    return AdaptiveRiskReservationRequest(
        policy=_policy(),
        inputs=inputs,
        account_snapshot=account,
        account_scope=account.account_scope,
        setup_family=setup_family,
        correlation_cluster=inputs.correlation_cluster_id,
        client_order_id=decision_id,
        entry_limit_price=ENTRY_PRICE,
        opportunity_key=None,
    )


def _automation_session(
    db,
    *,
    request: AdaptiveRiskReservationRequest | None = None,
) -> TradingAutomationSession:
    variant = MomentumStrategyVariant(
        family="adaptive_db_paper_atomic_fixture",
        variant_key=uuid.uuid4().hex,
        version=1,
        label="Adaptive DB paper atomic fixture",
        params_json={},
        execution_family="alpaca_spot",
    )
    db.add(variant)
    db.flush()
    account_scope = request.account_scope if request is not None else DEFAULT_ACCOUNT_SCOPE
    account_identity = (
        request.inputs.account_identity_sha256
        if request is not None
        else _hash("alpaca-paper-account-A")
    )
    session = TradingAutomationSession(
        venue="alpaca",
        execution_family="alpaca_spot",
        mode="paper",
        symbol="VEEE",
        variant_id=variant.id,
        state="watching",
        risk_snapshot_json={
            "db_paper_account_binding": {
                "account_scope": account_scope,
                "account_identity_sha256": account_identity,
            }
        },
        allocation_decision_json={},
    )
    db.add(session)
    db.flush()
    return session


@dataclass(frozen=True)
class _FinalBoundary:
    source: AdaptiveRiskBuilderSource
    observation: DbPaperFinalAdmissionObservation
    final_bundle: DbPaperFinalAdmissionBundle
    locked_snapshot: LockedAdaptiveRiskAdmissionSnapshot
    reference_price: float = REFERENCE_PRICE
    target_price: float = TARGET_PRICE
    effective_atr: float = EFFECTIVE_ATR
    fee_ratio: float = FEE_RATIO


def _final_source_and_observation(
    request: AdaptiveRiskReservationRequest,
    locked_snapshot: LockedAdaptiveRiskAdmissionSnapshot,
) -> _FinalBoundary:
    inputs = request.inputs
    decision_at = locked_snapshot.observed_at
    eligibility_observed = decision_at - timedelta(milliseconds=12)
    eligibility_available = decision_at - timedelta(milliseconds=8)
    eligibility_row_updated = decision_at - timedelta(milliseconds=10)
    bbo_observed = decision_at - timedelta(milliseconds=7)
    bbo_available = decision_at - timedelta(milliseconds=6)
    gate_observed = decision_at - timedelta(milliseconds=5)
    gate_available = decision_at - timedelta(milliseconds=4)
    readiness = {
        "spread_bps": 10.0,
        "slippage_estimate_bps": inputs.entry_slippage_bps,
        "fee_to_target_ratio": FEE_RATIO,
    }
    opportunity = {
        "account_scope": request.account_scope,
        "symbol": inputs.symbol,
        "trading_date": inputs.as_of.astimezone(
            ZoneInfo("America/New_York")
        ).date().isoformat(),
        "setup_family": request.setup_family,
    }
    gate_debug = {"pullback_low": inputs.structural_stop}
    bbo_source = "fixture:db-paper-final-bbo"
    bbo_generation = "fixture-final-bbo-generation"
    eligibility_source = "fixture:locked-viability-read"
    eligibility_generation = "fixture-eligibility-generation"
    gate_source = "fixture:final-entry-gate"
    gate_generation = "fixture-gate-generation"
    bbo_payload = db_paper_bbo_evidence_payload(
        symbol=inputs.symbol,
        bid=inputs.bid,
        ask=inputs.ask,
        quote_source=bbo_source,
        observed_at=bbo_observed,
        available_at=bbo_available,
        provider_generation=bbo_generation,
    )
    eligibility_payload = db_paper_eligibility_evidence_payload(
        symbol=inputs.symbol,
        viability_id=71,
        variant_id=17,
        viability_score=0.91,
        paper_eligible=True,
        observed_at=eligibility_observed,
        available_at=eligibility_available,
        row_updated_at=eligibility_row_updated,
        execution_readiness=readiness,
        source=eligibility_source,
        provider_generation=eligibility_generation,
    )
    gate_payload = db_paper_entry_gate_evidence_payload(
        symbol=inputs.symbol,
        allowed=True,
        reason="all_gates_pass",
        debug=gate_debug,
        structural_stop=inputs.structural_stop,
        setup_family=request.setup_family,
        opportunity_key=opportunity,
        observed_at=gate_observed,
        available_at=gate_available,
        source=gate_source,
        provider_generation=gate_generation,
    )
    evidence = dict(inputs.evidence)
    prior_capture = evidence["capture_prefix"]
    evidence["capture_prefix"] = replace(
        prior_capture,
        content_sha256=inputs.capture_prefix_root_sha256,
    )
    evidence["bbo"] = RiskInputEvidence(
        source=bbo_source,
        observed_at=bbo_observed,
        available_at=bbo_available,
        content_sha256=db_paper_admission_component_sha256(bbo_payload),
        provider_generation=bbo_generation,
    )
    evidence["paper_eligibility"] = RiskInputEvidence(
        source=eligibility_source,
        observed_at=eligibility_observed,
        available_at=eligibility_available,
        content_sha256=db_paper_admission_component_sha256(eligibility_payload),
        provider_generation=eligibility_generation,
    )
    evidence["paper_entry_gate"] = RiskInputEvidence(
        source=gate_source,
        observed_at=gate_observed,
        available_at=gate_available,
        content_sha256=db_paper_admission_component_sha256(gate_payload),
        provider_generation=gate_generation,
    )
    evidence["reservation_ledger"] = RiskInputEvidence(
        source="postgresql:adaptive_risk_reservations",
        observed_at=locked_snapshot.observed_at,
        available_at=locked_snapshot.observed_at,
        content_sha256=locked_snapshot.ledger_sha256,
        provider_generation=RESERVATION_LEDGER_GENERATION,
    )
    aggregates = locked_snapshot.aggregates
    inputs = replace(
        inputs,
        as_of=decision_at,
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
        policy_buying_power_capacity_usd=(
            locked_snapshot.policy_buying_power_capacity_usd
        ),
        open_buying_power_impact_usd=aggregates[
            "open_buying_power_impact_usd"
        ],
        pending_buying_power_impact_usd=aggregates[
            "pending_buying_power_impact_usd"
        ],
        evidence=evidence,
    )
    capture_evidence = inputs.evidence["capture_prefix"]
    capture = AdaptiveRiskDiagnosticCaptureBinding.create_diagnostic(
        run_id=inputs.replay_or_paper_run_id,
        generation=inputs.generation,
        decision_id=inputs.decision_id,
        input_prefix_sequence=3,
        input_prefix_root_sha256=inputs.capture_prefix_root_sha256,
        identity_sha256=_hash("db-paper-final-capture-identity"),
        observed_at=capture_evidence.observed_at,
        available_at=capture_evidence.available_at,
        verifier_generation=capture_evidence.provider_generation,
    )
    source = AdaptiveRiskBuilderSource(
        policy=request.policy,
        inputs=inputs,
        account_snapshot=request.account_snapshot,
        capture_binding=capture,
        account_scope=request.account_scope,
        setup_family=request.setup_family,
        correlation_cluster=request.correlation_cluster,
    )
    material = DbPaperFinalAdmissionMaterial.create(
        source,
        quote_source=bbo_source,
        gate_allowed=True,
        gate_reason="all_gates_pass",
        gate_debug=gate_debug,
        opportunity_key=opportunity,
        eligibility=eligibility_payload,
        execution_terms=db_paper_execution_terms_payload(
            effective_config_sha256=inputs.effective_config_sha256,
            stop_atr_mult=0.6,
            target_atr_mult=0.9,
            vol_floor_mult=0.5,
            reward_risk=2.0,
            entry_slippage_bps=inputs.entry_slippage_bps,
            exit_slippage_bps=inputs.exit_slippage_bps,
            fee_to_target_ratio=FEE_RATIO,
        ),
    )
    final_bundle = DbPaperFinalAdmissionBundle.create(
        material,
        source,
        locked_risk_snapshot=locked_snapshot,
    )
    observation = DbPaperFinalAdmissionObservation.create(
        source,
        decision_at=decision_at,
        bid=inputs.bid,
        ask=inputs.ask,
        quote_source=bbo_source,
        viability_id=71,
        variant_id=17,
        viability_score=0.91,
        paper_eligible=True,
        eligibility_observed_at=eligibility_observed,
        eligibility_available_at=eligibility_available,
        eligibility_row_updated_at=eligibility_row_updated,
        execution_readiness=readiness,
        gate_allowed=True,
        gate_reason="all_gates_pass",
        gate_debug=gate_debug,
        structural_stop=inputs.structural_stop,
        opportunity_key=opportunity,
    )
    return _FinalBoundary(
        source=source,
        observation=observation,
        final_bundle=final_bundle,
        locked_snapshot=locked_snapshot,
    )


def _final_boundary(
    db,
    request: AdaptiveRiskReservationRequest,
) -> _FinalBoundary:
    store = AdaptiveRiskReservationStore(db.get_bind())
    locked_snapshot = store.lock_admission_snapshot(
        account_scope=request.account_scope,
        symbol=request.inputs.symbol,
        correlation_cluster=request.correlation_cluster,
        account_snapshot=request.account_snapshot,
        session=db,
    )
    return _final_source_and_observation(request, locked_snapshot)


def _reserve(db, session, pe):
    raw = pe[paper_runner.KEY_ADAPTIVE_RISK_REQUEST]
    request = load_adaptive_risk_reservation_request(raw)
    boundary = _final_boundary(db, request)
    result = paper_runner._reserve_adaptive_db_paper_entry(
        db,
        session,
        pe,
        bid=boundary.source.inputs.bid,
        ask=boundary.source.inputs.ask,
        entry_price=ENTRY_PRICE,
        structural_stop=STOP_PRICE,
        setup_family=request.setup_family,
        builder_source=boundary.source,
        final_observation=boundary.observation,
        final_bundle=boundary.final_bundle,
        locked_snapshot=boundary.locked_snapshot,
        reference_price=boundary.reference_price,
        target_price=boundary.target_price,
        effective_atr=boundary.effective_atr,
        fee_ratio=boundary.fee_ratio,
    )
    if result.get("ok"):
        pe["adaptive_risk_reservation_id"] = result["reservation_id"]
        pe["adaptive_risk_decision_packet_sha256"] = result[
            "decision_packet_sha256"
        ]
        pe["adaptive_risk_request_sha256"] = result["request_sha256"]
        pe["adaptive_risk_connection_generation"] = result[
            "connection_generation"
        ]
        pe["adaptive_risk_account_scope"] = result["account_scope"]
        pe["adaptive_risk_reservation_closed"] = False
    return result


def _rehash_content_payload(payload: dict[str, object]) -> dict[str, object]:
    body = copy.deepcopy(payload)
    body.pop("content_sha256", None)
    payload["content_sha256"] = db_paper_admission_component_sha256(body)
    return payload


def test_runtime_reservation_request_round_trips_and_rejects_tampering() -> None:
    request = _db_paper_request()
    payload = request.to_payload()

    loaded = load_adaptive_risk_reservation_request(payload)
    assert loaded == request
    assert loaded.request_sha256 == payload["request_sha256"]

    tampered = request.to_payload()
    tampered["inputs"]["buying_power_usd"] *= 2
    with pytest.raises(AdaptiveRiskContractError, match="hash mismatch"):
        load_adaptive_risk_reservation_request(tampered)


def test_db_paper_reserve_uses_caller_session_without_consuming(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _db_paper_request()
    sess = _automation_session(db, request=request)
    boundary = _final_boundary(db, request)
    calls: dict[str, object] = {}
    reservation_id = uuid.UUID("00000000-0000-0000-0000-000000000137")

    class _Store:
        def __init__(self, bind):
            calls["bind"] = bind

        def reserve(
            self,
            supplied,
            *,
            session,
            locked_snapshot,
            prepared_resolution,
            prepared_decision_packet,
        ):
            calls["request"] = supplied
            calls["session"] = session
            calls["locked_snapshot"] = locked_snapshot
            calls["prepared_resolution"] = prepared_resolution
            calls["prepared_decision_packet"] = prepared_decision_packet
            return SimpleNamespace(
                admission_accepted=True,
                reservation_id=reservation_id,
                quantity_shares=prepared_resolution.quantity_shares,
                structural_risk_usd=(
                    prepared_resolution.planned_structural_risk_usd
                ),
                gross_notional_usd=prepared_resolution.planned_notional_usd,
                buying_power_impact_usd=(
                    prepared_resolution.planned_buying_power_impact_usd
                ),
                decision_packet_sha256=(
                    prepared_resolution.decision_packet_sha256
                ),
                rejection_reasons=(),
            )

    monkeypatch.setattr(paper_runner, "AdaptiveRiskReservationStore", _Store)
    pe = {paper_runner.KEY_ADAPTIVE_RISK_REQUEST: request.to_payload()}

    result = paper_runner._reserve_adaptive_db_paper_entry(
        db,
        sess,
        pe,
        bid=boundary.source.inputs.bid,
        ask=boundary.source.inputs.ask,
        entry_price=ENTRY_PRICE,
        structural_stop=STOP_PRICE,
        setup_family=request.setup_family,
        builder_source=boundary.source,
        final_observation=boundary.observation,
        final_bundle=boundary.final_bundle,
        locked_snapshot=boundary.locked_snapshot,
        reference_price=boundary.reference_price,
        target_price=boundary.target_price,
        effective_atr=boundary.effective_atr,
        fee_ratio=boundary.fee_ratio,
    )

    assert result["ok"] is True
    prepared = calls["prepared_resolution"]
    assert result["quantity_shares"] == prepared.quantity_shares
    assert result["gross_notional_usd"] == pytest.approx(
        prepared.planned_notional_usd
    )
    assert calls["request"].inputs == boundary.source.inputs
    assert calls["request"].account_scope == request.account_scope
    assert calls["session"] is db
    assert calls["locked_snapshot"] == boundary.locked_snapshot
    assert calls["prepared_decision_packet"] == prepared.to_decision_packet()
    assert result["_prepared_resolution"] is prepared
    assert "adaptive_risk_request_consumed_sha256" not in pe


def test_locked_db_paper_boundary_builds_once_and_strictly_recomputes(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _db_paper_request()
    session = _automation_session(db, request=request)
    pe = {paper_runner.KEY_ADAPTIVE_RISK_REQUEST: request.to_payload()}
    calls = {"builder": 0, "verifier": 0}
    original_builder_resolver = risk_builder_module.resolve_adaptive_risk
    original_verifier_resolver = risk_policy_module.resolve_adaptive_risk

    def count_builder_resolution(policy, inputs):
        calls["builder"] += 1
        return original_builder_resolver(policy, inputs)

    def count_strict_recomputation(policy, inputs):
        calls["verifier"] += 1
        return original_verifier_resolver(policy, inputs)

    monkeypatch.setattr(
        risk_builder_module,
        "resolve_adaptive_risk",
        count_builder_resolution,
    )
    monkeypatch.setattr(
        risk_policy_module,
        "resolve_adaptive_risk",
        count_strict_recomputation,
    )

    result = _reserve(db, session, pe)

    assert result["ok"] is True, result
    # The builder verifies its own packet once; the reservation boundary then
    # repeats that strict verification independently before trusting economics.
    assert calls == {"builder": 1, "verifier": 2}
    prepared_resolution = result["_prepared_resolution"]
    assert prepared_resolution.decision_packet_sha256 == result[
        "decision_packet_sha256"
    ]
    assert result["decision_packet_sha256"] == pe[
        "adaptive_risk_builder_audit"
    ]["decision_packet_sha256"]
    assert result["executable_admission_sha256"] == result[
        paper_runner.KEY_DB_PAPER_EXECUTABLE_ADMISSION
    ]["content_sha256"]
    assert result["final_admission_receipt_sha256"] == result[
        paper_runner.KEY_DB_PAPER_FINAL_ADMISSION_RECEIPT
    ]["content_sha256"]


def _built_locked_boundary_request(boundary: _FinalBoundary, session_id: int):
    return risk_builder_module.build_adaptive_risk_request(
        boundary.source,
        client_order_id=(
            f"db-paper-{session_id}-{boundary.source.inputs.decision_id}"
        ),
        entry_limit_price=ENTRY_PRICE,
        opportunity_key=boundary.final_bundle.opportunity_key,
    )


def test_locked_snapshot_cannot_be_reused_after_transaction_commit(db) -> None:
    request = _db_paper_request(decision_id="stale-locked-snapshot")
    session = _automation_session(db, request=request)
    boundary = _final_boundary(db, request)
    built = _built_locked_boundary_request(boundary, int(session.id))

    db.commit()
    db.connection()  # begin a distinct top-level transaction on the same Session

    with pytest.raises(
        AdaptiveRiskContractError,
        match="not issued by the current database transaction",
    ):
        AdaptiveRiskReservationStore(db.get_bind()).reserve(
            built.request,
            session=db,
            locked_snapshot=boundary.locked_snapshot,
            prepared_resolution=built.resolution,
            prepared_decision_packet=built.decision_packet,
        )


def test_manually_rehashed_locked_snapshot_is_not_transaction_authority(db) -> None:
    request = _db_paper_request(decision_id="forged-locked-snapshot")
    session = _automation_session(db, request=request)
    boundary = _final_boundary(db, request)
    locked = boundary.locked_snapshot
    forged = LockedAdaptiveRiskAdmissionSnapshot.create(
        account_scope=locked.account_scope,
        symbol=locked.symbol,
        correlation_cluster=locked.correlation_cluster,
        account_snapshot_sha256=locked.account_snapshot_sha256,
        transaction_id=locked.transaction_id,
        backend_pid=locked.backend_pid,
        lock_receipt_id="00000000-0000-0000-0000-000000000999",
        observed_at=locked.observed_at,
        aggregates=locked.aggregates,
        ledger_payload=locked.ledger_payload,
        policy_buying_power_capacity_usd=(
            locked.policy_buying_power_capacity_usd
        ),
    )
    built = _built_locked_boundary_request(boundary, int(session.id))

    with pytest.raises(
        AdaptiveRiskContractError,
        match="not issued by the current database transaction",
    ):
        AdaptiveRiskReservationStore(db.get_bind()).reserve(
            built.request,
            session=db,
            locked_snapshot=forged,
            prepared_resolution=built.resolution,
            prepared_decision_packet=built.decision_packet,
        )


@pytest.mark.parametrize("mutation_style", ["object_setattr", "instance_dict"])
def test_locked_snapshot_clock_mutation_is_rejected_at_consumption(
    db,
    mutation_style: str,
) -> None:
    request = _db_paper_request(
        decision_id=f"mutated-locked-clock-{mutation_style}"
    )
    session = _automation_session(db, request=request)
    boundary = _final_boundary(db, request)
    locked = boundary.locked_snapshot
    built = _built_locked_boundary_request(boundary, int(session.id))
    mutated_clock = locked.observed_at + timedelta(milliseconds=1)

    if mutation_style == "object_setattr":
        object.__setattr__(locked, "observed_at", mutated_clock)
    else:
        locked.__dict__["observed_at"] = mutated_clock

    with pytest.raises(
        AdaptiveRiskContractError,
        match="locked admission snapshot changed",
    ):
        AdaptiveRiskReservationStore(db.get_bind()).reserve(
            built.request,
            session=db,
            locked_snapshot=locked,
            prepared_resolution=built.resolution,
            prepared_decision_packet=built.decision_packet,
        )


def test_locked_snapshot_subclass_cannot_override_consumption_verification(db) -> None:
    request = _db_paper_request(decision_id="subclassed-locked-snapshot")
    session = _automation_session(db, request=request)
    boundary = _final_boundary(db, request)
    built = _built_locked_boundary_request(boundary, int(session.id))

    @dataclass(frozen=True)
    class BypassSnapshot(LockedAdaptiveRiskAdmissionSnapshot):
        def verify(self):
            return self

    bypass = BypassSnapshot(**boundary.locked_snapshot.__dict__)
    object.__setattr__(
        bypass,
        "observed_at",
        bypass.observed_at + timedelta(milliseconds=1),
    )

    with pytest.raises(
        AdaptiveRiskContractError,
        match="locked admission snapshot is malformed",
    ):
        AdaptiveRiskReservationStore(db.get_bind()).reserve(
            built.request,
            session=db,
            locked_snapshot=bypass,
            prepared_resolution=built.resolution,
            prepared_decision_packet=built.decision_packet,
        )


def test_forged_prepared_resolution_cannot_substitute_locked_economics(db) -> None:
    request = _db_paper_request(decision_id="forged-prepared-economics")
    session = _automation_session(db, request=request)
    boundary = _final_boundary(db, request)
    built = _built_locked_boundary_request(boundary, int(session.id))
    forged = replace(
        built.resolution,
        valid=True,
        rejection_reasons=(),
        quantity_shares=99_999_999,
        planned_structural_risk_usd=999_999_999.0,
        planned_notional_usd=999_999_999.0,
        planned_buying_power_impact_usd=999_999_999.0,
    )

    with pytest.raises(
        AdaptiveRiskContractError,
        match="failed canonical recomputation",
    ):
        AdaptiveRiskReservationStore(db.get_bind()).reserve(
            built.request,
            session=db,
            locked_snapshot=boundary.locked_snapshot,
            prepared_resolution=forged,
            prepared_decision_packet=forged.to_decision_packet(),
        )
    assert db.scalar(select(func.count(AdaptiveRiskReservation.reservation_id))) == 0
    assert db.scalar(
        select(func.count(AdaptiveRiskDecisionPacket.decision_packet_sha256))
    ) == 0


def test_locked_snapshot_binds_exact_account_snapshot_within_transaction(db) -> None:
    request = _db_paper_request(decision_id="account-snapshot-bound-lock")
    session = _automation_session(db, request=request)
    boundary = _final_boundary(db, request)
    changed_account = replace(
        request.account_snapshot,
        account_identity_sha256=_hash("different-db-paper-account"),
        equity_usd=request.account_snapshot.equity_usd * 10.0,
        broker_day_change_usd=12_500.0,
        local_realized_pnl_usd=8_000.0,
    )
    changed_request = _db_paper_request(
        decision_id=request.inputs.decision_id,
        account_snapshot=changed_account,
        now=boundary.locked_snapshot.observed_at,
    )
    changed_boundary = _final_source_and_observation(
        changed_request,
        boundary.locked_snapshot,
    )
    built = _built_locked_boundary_request(changed_boundary, int(session.id))

    assert changed_account.buying_power_usd == request.account_snapshot.buying_power_usd
    assert (
        changed_account.pending_policy_buying_power_reflected_usd
        == request.account_snapshot.pending_policy_buying_power_reflected_usd
    )
    assert changed_account.snapshot_sha256 != request.account_snapshot.snapshot_sha256
    with pytest.raises(
        AdaptiveRiskContractError,
        match="does not match request account snapshot",
    ):
        AdaptiveRiskReservationStore(db.get_bind()).reserve(
            built.request,
            session=db,
            locked_snapshot=boundary.locked_snapshot,
            prepared_resolution=built.resolution,
            prepared_decision_packet=built.decision_packet,
        )


def test_locked_snapshot_rereads_pending_settlement_before_reserve(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _db_paper_request(decision_id="fresh-pending-settlement")
    session = _automation_session(db, request=request)
    boundary = _final_boundary(db, request)
    built = _built_locked_boundary_request(boundary, int(session.id))
    store = AdaptiveRiskReservationStore(db.get_bind())
    active_ledger = store._active_ledger

    def current_ledger_with_pending(
        owned_session,
        *,
        account_scope,
        symbol,
        correlation_cluster,
    ):
        aggregates, payload = active_ledger(
            owned_session,
            account_scope=account_scope,
            symbol=symbol,
            correlation_cluster=correlation_cluster,
        )
        current = copy.deepcopy(payload)
        current["pending_settlements"] = [
            {
                "reservation_id": "00000000-0000-0000-0000-000000000332",
                "state": "flat_pending_settlement",
            }
        ]
        return aggregates, current

    monkeypatch.setattr(store, "_active_ledger", current_ledger_with_pending)

    with pytest.raises(AdaptiveRiskPendingSettlement) as exc:
        store.reserve(
            built.request,
            session=db,
            locked_snapshot=boundary.locked_snapshot,
            prepared_resolution=built.resolution,
            prepared_decision_packet=built.decision_packet,
        )
    assert exc.value.reason == "adaptive_risk_pending_cycle_settlement"
    assert exc.value.pending_settlements[0]["state"] == (
        "flat_pending_settlement"
    )


@pytest.mark.parametrize("fault_site", ["executable", "receipt"])
def test_post_reserve_admission_fault_rolls_back_pending_claims(
    db,
    monkeypatch: pytest.MonkeyPatch,
    fault_site: str,
) -> None:
    request = _db_paper_request(decision_id=f"db-paper-{fault_site}-fault")
    session = _automation_session(db, request=request)
    pe = {paper_runner.KEY_ADAPTIVE_RISK_REQUEST: request.to_payload()}

    def fail_after_reserve(_cls, *_args, **_kwargs):
        raise AdaptiveRiskBuilderError(f"forced_{fault_site}_fault")

    target = (
        paper_runner.DbPaperExecutableAdmission
        if fault_site == "executable"
        else paper_runner.DbPaperAdmissionReceipt
    )
    monkeypatch.setattr(target, "create", classmethod(fail_after_reserve))

    result = _reserve(db, session, pe)

    assert result == {"ok": False, "reason": f"forced_{fault_site}_fault"}
    assert db.scalar(select(func.count(AdaptiveRiskReservation.reservation_id))) == 0
    assert db.scalar(select(func.count(AdaptiveRiskOpportunityClaim.id))) == 0
    assert db.scalar(
        select(func.count(AdaptiveRiskDecisionPacket.decision_packet_sha256))
    ) == 0
    assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == 0


def test_executable_admission_rejects_tampered_fill_economics(db) -> None:
    request = _db_paper_request()
    session = _automation_session(db, request=request)
    pe = {paper_runner.KEY_ADAPTIVE_RISK_REQUEST: request.to_payload()}
    result = _reserve(db, session, pe)
    reservation_id = uuid.UUID(result["reservation_id"])
    baseline = {
        "price": ENTRY_PRICE,
        "reference_price": REFERENCE_PRICE,
        "fees_usd": result["fees_usd"],
        "stop_price": STOP_PRICE,
        "target_price": TARGET_PRICE,
        "effective_atr": result["effective_atr"],
        "decision_packet_id": None,
    }
    tampered = {
        "entry_price": (
            {**baseline, "price": ENTRY_PRICE + 0.01},
            "db_paper_executable_admission_mismatch: entry_price,resolved_entry_price",
        ),
        "stop_price": (
            {**baseline, "stop_price": STOP_PRICE - 0.01},
            "db_paper_executable_admission_mismatch: stop_price",
        ),
        "target_price": (
            {**baseline, "target_price": TARGET_PRICE + 0.01},
            "executable admission failed full canonical reconstruction",
        ),
        "fees_usd": (
            {**baseline, "fees_usd": result["fees_usd"] + 0.01},
            "executable admission failed full canonical reconstruction",
        ),
        "effective_atr": (
            {
                **baseline,
                "effective_atr": result["effective_atr"] + 0.01,
            },
            "executable admission failed full canonical reconstruction",
        ),
    }

    for field, (supplied, error) in tampered.items():
        with pytest.raises(
            AdaptiveRiskContractError,
            match=error,
        ):
            paper_runner._record_adaptive_db_paper_entry_fill(
                db,
                session,
                pe,
                result,
                **supplied,
            )
        db.expire_all()
        reservation = db.get(AdaptiveRiskReservation, reservation_id)
        assert reservation.state == "reserved", field
        assert reservation.cumulative_filled_quantity_shares == 0, field
        assert reservation.opportunity_claim_id is None, field
        assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == 0


@pytest.mark.parametrize(
    "tamper_kind",
    ["effective_atr", "execution_terms", "effective_config"],
)
def test_rehashed_executable_tamper_fails_full_reconstruction(
    db,
    tamper_kind: str,
) -> None:
    request = _db_paper_request(decision_id=f"db-paper-rehash-{tamper_kind}")
    session = _automation_session(db, request=request)
    pe = {paper_runner.KEY_ADAPTIVE_RISK_REQUEST: request.to_payload()}
    result = _reserve(db, session, pe)
    assert result["ok"] is True, result
    reservation_id = uuid.UUID(result["reservation_id"])

    tampered = copy.deepcopy(result)
    executable = copy.deepcopy(
        tampered[paper_runner.KEY_DB_PAPER_EXECUTABLE_ADMISSION]
    )
    if tamper_kind == "effective_atr":
        executable["effective_atr"] = float(executable["effective_atr"]) + 0.01
    elif tamper_kind == "execution_terms":
        terms = copy.deepcopy(executable["execution_terms"])
        terms["stop_atr_mult"] = float(terms["stop_atr_mult"]) + 0.1
        executable["execution_terms"] = terms
        executable["execution_terms_sha256"] = (
            db_paper_admission_component_sha256(terms)
        )
    else:
        replacement_config = _hash("tampered-effective-config")
        terms = copy.deepcopy(executable["execution_terms"])
        terms["effective_config_sha256"] = replacement_config
        executable["execution_terms"] = terms
        executable["execution_terms_sha256"] = (
            db_paper_admission_component_sha256(terms)
        )
        executable["effective_config_sha256"] = replacement_config
    _rehash_content_payload(executable)
    assert (
        load_db_paper_executable_admission(executable).content_sha256
        == executable["content_sha256"]
    )
    tampered[paper_runner.KEY_DB_PAPER_EXECUTABLE_ADMISSION] = executable
    tampered["executable_admission_sha256"] = executable["content_sha256"]
    pe[paper_runner.KEY_DB_PAPER_EXECUTABLE_ADMISSION] = copy.deepcopy(executable)

    with pytest.raises(
        AdaptiveRiskContractError,
        match="executable admission failed full canonical reconstruction",
    ):
        paper_runner._record_adaptive_db_paper_entry_fill(
            db,
            session,
            pe,
            tampered,
            price=ENTRY_PRICE,
            reference_price=REFERENCE_PRICE,
            fees_usd=result["fees_usd"],
            stop_price=STOP_PRICE,
            target_price=TARGET_PRICE,
            effective_atr=result["effective_atr"],
            decision_packet_id=None,
        )

    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, reservation_id)
    assert reservation.state == "reserved"
    assert reservation.cumulative_filled_quantity_shares == 0
    assert reservation.opportunity_claim_id is None
    assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == 0


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        (
            "venue",
            "iex",
            "final receipt failed full canonical reconstruction",
        ),
        ("broker_source", "alpaca", "db_paper_admission_receipt_invalid"),
    ],
)
def test_rehashed_receipt_identity_tamper_fails_closed(
    db,
    field: str,
    value: str,
    error: str,
) -> None:
    request = _db_paper_request(decision_id=f"db-paper-receipt-{field}")
    session = _automation_session(db, request=request)
    pe = {paper_runner.KEY_ADAPTIVE_RISK_REQUEST: request.to_payload()}
    result = _reserve(db, session, pe)
    assert result["ok"] is True, result
    reservation_id = uuid.UUID(result["reservation_id"])

    tampered = copy.deepcopy(result)
    receipt = copy.deepcopy(
        tampered[paper_runner.KEY_DB_PAPER_FINAL_ADMISSION_RECEIPT]
    )
    receipt[field] = value
    _rehash_content_payload(receipt)
    if field == "venue":
        assert (
            load_db_paper_admission_receipt(receipt).content_sha256
            == receipt["content_sha256"]
        )
    tampered[paper_runner.KEY_DB_PAPER_FINAL_ADMISSION_RECEIPT] = receipt
    tampered["final_admission_receipt_sha256"] = receipt["content_sha256"]
    pe[paper_runner.KEY_DB_PAPER_FINAL_ADMISSION_RECEIPT] = copy.deepcopy(receipt)

    with pytest.raises(AdaptiveRiskContractError, match=error):
        paper_runner._record_adaptive_db_paper_entry_fill(
            db,
            session,
            pe,
            tampered,
            price=ENTRY_PRICE,
            reference_price=REFERENCE_PRICE,
            fees_usd=result["fees_usd"],
            stop_price=STOP_PRICE,
            target_price=TARGET_PRICE,
            effective_atr=result["effective_atr"],
            decision_packet_id=None,
        )

    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, reservation_id)
    assert reservation.state == "reserved"
    assert reservation.cumulative_filled_quantity_shares == 0
    assert reservation.opportunity_claim_id is None
    assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == 0


def test_same_size_reservation_id_swap_is_rejected(db) -> None:
    first_request = _db_paper_request(
        account_scope="db-paper:swap-account-a",
        decision_id="db-paper-swap-a",
    )
    first_session = _automation_session(db, request=first_request)
    first_pe = {
        paper_runner.KEY_ADAPTIVE_RISK_REQUEST: first_request.to_payload()
    }
    first = _reserve(db, first_session, first_pe)

    second_request = _db_paper_request(
        account_scope="db-paper:swap-account-b",
        decision_id="db-paper-swap-b",
    )
    second_session = _automation_session(db, request=second_request)
    second_pe = {
        paper_runner.KEY_ADAPTIVE_RISK_REQUEST: second_request.to_payload()
    }
    second = _reserve(db, second_session, second_pe)
    assert first["ok"] is True and second["ok"] is True
    assert first["quantity_shares"] == second["quantity_shares"]

    swapped = copy.deepcopy(first)
    swapped["reservation_id"] = second["reservation_id"]
    with pytest.raises(
        AdaptiveRiskContractError,
        match="executable admission failed full canonical reconstruction",
    ):
        paper_runner._record_adaptive_db_paper_entry_fill(
            db,
            first_session,
            first_pe,
            swapped,
            price=ENTRY_PRICE,
            reference_price=REFERENCE_PRICE,
            fees_usd=first["fees_usd"],
            stop_price=STOP_PRICE,
            target_price=TARGET_PRICE,
            effective_atr=first["effective_atr"],
            decision_packet_id=None,
        )

    for admission in (first, second):
        reservation = db.get(
            AdaptiveRiskReservation, uuid.UUID(admission["reservation_id"])
        )
        assert reservation.state == "reserved"
        assert reservation.cumulative_filled_quantity_shares == 0
        assert reservation.opportunity_claim_id is None
    assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == 0


def test_max_length_cid_uses_fixed_length_paper_broker_order_id(db) -> None:
    request = replace(_db_paper_request(), client_order_id="c" * 160)
    session = _automation_session(db)
    pe = {paper_runner.KEY_ADAPTIVE_RISK_REQUEST: request.to_payload()}
    result = _reserve(db, session, pe)
    reservation_id = uuid.UUID(result["reservation_id"])

    paper_runner._record_adaptive_db_paper_entry_fill(
        db,
        session,
        pe,
        result,
        price=10.01,
        reference_price=REFERENCE_PRICE,
        fees_usd=result["fees_usd"],
        stop_price=9.5,
        target_price=11.0,
        effective_atr=result["effective_atr"],
        decision_packet_id=None,
    )

    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, reservation_id)
    assert reservation.state == "filled"
    final_request_sha = pe[paper_runner.KEY_ADAPTIVE_RISK_REQUEST][
        "request_sha256"
    ]
    assert reservation.broker_order_id == f"db-paper-order:{final_request_sha}"
    assert len(reservation.broker_order_id) < 160


def test_db_paper_boundary_fails_closed_without_binding() -> None:
    db = SimpleNamespace(get_bind=lambda: "must-not-be-read")
    sess = SimpleNamespace(
        id=1,
        symbol="VEEE",
        venue="alpaca",
        execution_family="alpaca_spot",
        risk_snapshot_json={},
    )
    result = paper_runner._reserve_adaptive_db_paper_entry(
        db,
        sess,
        {},
        bid=9.99,
        ask=10.0,
        entry_price=10.01,
        structural_stop=9.5,
        setup_family="first_dip_reclaim",
    )
    assert result["ok"] is False
    assert result["reason"] == "db_paper_final_admission_receipt_required"


def test_canonical_entry_cannot_consume_without_final_receipt(db) -> None:
    request = _db_paper_request()
    session = _automation_session(db)
    pe = {paper_runner.KEY_ADAPTIVE_RISK_REQUEST: request.to_payload()}
    result = _reserve(db, session, pe)
    reservation_id = uuid.UUID(result["reservation_id"])
    unbound = dict(result)
    unbound.pop(paper_runner.KEY_DB_PAPER_FINAL_ADMISSION_RECEIPT)

    with pytest.raises(
        AdaptiveRiskContractError,
        match="requires final admission receipt",
    ):
        paper_runner._record_adaptive_db_paper_entry_fill(
            db,
            session,
            pe,
            unbound,
            price=10.01,
            reference_price=REFERENCE_PRICE,
            fees_usd=result["fees_usd"],
            stop_price=9.5,
            target_price=11.0,
            effective_atr=result["effective_atr"],
            decision_packet_id=None,
        )

    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, reservation_id)
    assert reservation.state == "reserved"
    assert reservation.cumulative_filled_quantity_shares == 0
    assert reservation.opportunity_claim_id is None
    assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == 0


def test_atomic_entry_partial_and_flat_lifecycle_is_idempotent(db) -> None:
    request = _db_paper_request()
    session = _automation_session(db)
    pe = {paper_runner.KEY_ADAPTIVE_RISK_REQUEST: request.to_payload()}
    result = _reserve(db, session, pe)

    assert result["ok"] is True, result
    reservation_id = uuid.UUID(result["reservation_id"])
    reservation = db.get(AdaptiveRiskReservation, reservation_id)
    assert reservation.state == "reserved"
    assert reservation.cumulative_filled_quantity_shares == 0
    assert reservation.open_quantity_shares == 0
    assert reservation.opportunity_claim_id is None
    assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == 0
    assert "adaptive_risk_request_consumed_sha256" not in pe

    entry = paper_runner._record_adaptive_db_paper_entry_fill(
        db,
        session,
        pe,
        result,
        price=10.01,
        reference_price=REFERENCE_PRICE,
        fees_usd=result["fees_usd"],
        stop_price=9.5,
        target_price=11.0,
        effective_atr=result["effective_atr"],
        decision_packet_id=None,
    )
    retry = paper_runner._record_adaptive_db_paper_entry_fill(
        db,
        session,
        pe,
        result,
        price=10.01,
        reference_price=REFERENCE_PRICE,
        fees_usd=result["fees_usd"],
        stop_price=9.5,
        target_price=11.0,
        effective_atr=result["effective_atr"],
        decision_packet_id=None,
    )
    assert retry.id == entry.id
    assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == 1
    for statement in (
        "UPDATE trading_automation_simulated_fills "
        "SET reason = 'tampered' WHERE id = :fill_id",
        "DELETE FROM trading_automation_simulated_fills WHERE id = :fill_id",
    ):
        with pytest.raises(
            DBAPIError,
            match="adaptive DB-paper lifecycle fills are append-only",
        ):
            with db.begin_nested():
                db.execute(text(statement), {"fill_id": int(entry.id)})
    db.refresh(entry)
    assert entry.reason == "entry_fill"
    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, reservation_id)
    assert reservation.state == "filled"
    assert reservation.open_quantity_shares == result["quantity_shares"]
    full_open_risk = reservation.open_structural_risk_usd

    exit_quantity = max(1, int(result["quantity_shares"]) // 2)
    remaining = int(result["quantity_shares"]) - exit_quantity
    assert remaining > 0
    reservation.pending_gross_notional_usd = Decimal("1")
    db.flush()
    with pytest.raises(
        paper_runner.AdaptiveReservationError,
        match="entry dimensions remain pending",
    ):
        paper_runner._record_adaptive_db_paper_position_fill(
            db,
            session,
            pe,
            action="exit_long",
            price=10.75,
            quantity=exit_quantity,
            remaining_open_quantity=remaining,
            reference_price=10.76,
            pnl_usd=50.0,
            reason="scale_out_target",
            marker_json={"entry": 10.01, "partial": True},
            decision_packet_id=None,
        )
    assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == 1
    reservation.pending_gross_notional_usd = Decimal("0")
    db.flush()
    partial = paper_runner._record_adaptive_db_paper_position_fill(
        db,
        session,
        pe,
        action="exit_long",
        price=10.75,
        quantity=exit_quantity,
        remaining_open_quantity=remaining,
        reference_price=10.76,
        pnl_usd=50.0,
        reason="scale_out_target",
        marker_json={"entry": 10.01, "partial": True, "runner_qty": remaining},
        decision_packet_id=None,
    )
    partial_retry = paper_runner._record_adaptive_db_paper_position_fill(
        db,
        session,
        pe,
        action="exit_long",
        price=10.75,
        quantity=exit_quantity,
        remaining_open_quantity=remaining,
        reference_price=10.76,
        pnl_usd=50.0,
        reason="scale_out_target",
        marker_json={"entry": 10.01, "partial": True, "runner_qty": remaining},
        decision_packet_id=None,
    )
    assert partial_retry.id == partial.id
    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, reservation_id)
    assert reservation.state == "filled"
    assert reservation.open_quantity_shares == remaining
    assert 0 < reservation.open_structural_risk_usd < full_open_risk
    assert reservation.open_gross_notional_usd > 0
    assert reservation.open_buying_power_impact_usd > 0
    assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == 2

    reservation.pending_buying_power_impact_usd = Decimal("1")
    db.flush()
    with pytest.raises(
        paper_runner.AdaptiveReservationError,
        match="entry dimensions remain pending",
    ):
        paper_runner._record_adaptive_db_paper_position_fill(
            db,
            session,
            pe,
            action="exit_long",
            price=10.5,
            quantity=remaining,
            remaining_open_quantity=0,
            reference_price=10.51,
            pnl_usd=25.0,
            reason="trail_stop",
            marker_json={"entry": 10.01, "stop": 10.5},
            decision_packet_id=None,
        )
    assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == 2
    reservation.pending_buying_power_impact_usd = Decimal("0")
    db.flush()
    flat = paper_runner._record_adaptive_db_paper_position_fill(
        db,
        session,
        pe,
        action="exit_long",
        price=10.5,
        quantity=remaining,
        remaining_open_quantity=0,
        reference_price=10.51,
        pnl_usd=25.0,
        reason="trail_stop",
        marker_json={"entry": 10.01, "stop": 10.5},
        decision_packet_id=None,
    )
    assert flat.position_state_after == "flat"
    assert pe["adaptive_risk_reservation_closed"] is True
    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, reservation_id)
    assert reservation.state == "closed"
    assert reservation.open_quantity_shares == 0
    assert reservation.open_structural_risk_usd == 0
    assert reservation.open_gross_notional_usd == 0
    assert reservation.open_buying_power_impact_usd == 0
    assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == 3

    packet = db.get(
        AdaptiveRiskDecisionPacket, result["decision_packet_sha256"]
    )
    assert packet.execution_surface == "db_paper"
    assert packet.admission_accepted is True


def test_canonical_entry_failure_rolls_back_fill_and_consumption(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _db_paper_request()
    session = _automation_session(db)
    pe = {paper_runner.KEY_ADAPTIVE_RISK_REQUEST: request.to_payload()}
    result = _reserve(db, session, pe)
    reservation_id = uuid.UUID(result["reservation_id"])
    original_apply = paper_runner.AdaptiveRiskReservationStore.apply_cumulative_fill

    def fail_after_canonical_row(self, supplied_id, *, evidence, session):
        assert supplied_id == reservation_id
        assert session.scalar(
            select(func.count(TradingAutomationSimulatedFill.id))
        ) == 1
        state = original_apply(
            self,
            supplied_id,
            evidence=evidence,
            session=session,
        )
        assert state.state == "filled"
        assert state.cumulative_filled_quantity_shares == result["quantity_shares"]
        assert state.open_quantity_shares == result["quantity_shares"]
        assert state.opportunity_status == "not_applicable"
        raise RuntimeError("forced adaptive ledger failure")

    monkeypatch.setattr(
        paper_runner.AdaptiveRiskReservationStore,
        "apply_cumulative_fill",
        fail_after_canonical_row,
    )
    with pytest.raises(RuntimeError, match="forced adaptive ledger failure"):
        paper_runner._record_adaptive_db_paper_entry_fill(
            db,
            session,
            pe,
            result,
            price=10.01,
            reference_price=REFERENCE_PRICE,
            fees_usd=result["fees_usd"],
            stop_price=9.5,
            target_price=11.0,
            effective_atr=result["effective_atr"],
            decision_packet_id=None,
        )

    assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == 0
    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, reservation_id)
    assert reservation.state == "reserved"
    assert reservation.cumulative_filled_quantity_shares == 0
    assert reservation.open_quantity_shares == 0
    assert reservation.opportunity_claim_id is None

    db.rollback()
    assert db.get(AdaptiveRiskReservation, reservation_id) is None
    assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == 0


def test_strict_fill_ledger_uses_canonical_cumulative_and_rolls_back(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.trading import economic_ledger

    session = _automation_session(db)
    prior = TradingAutomationSimulatedFill(
        session_id=int(session.id),
        symbol=session.symbol,
        lane="simulation",
        side="long",
        action="exit_long",
        fill_type="exit",
        quantity=10.0,
        price=10.5,
        reference_price=10.51,
        pnl_usd=50.0,
        position_state_before="long",
        position_state_after="long",
        reason="prior_partial",
        marker_json={"entry": 10.0},
    )
    db.add(prior)
    db.flush()
    reconciled: dict[str, float] = {}
    monkeypatch.setattr(economic_ledger, "mode_is_active", lambda: True)
    monkeypatch.setattr(
        economic_ledger,
        "record_automation_session_exit_fill",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        economic_ledger,
        "reconcile_automation_session",
        lambda *args, **kwargs: reconciled.update(
            legacy_pnl=float(kwargs["legacy_pnl"])
        ),
    )

    row = paper_runner._record_sim_fill(
        db,
        session,
        action="exit_long",
        fill_type="exit",
        price=10.25,
        quantity=10.0,
        reference_price=10.26,
        pnl_usd=25.0,
        position_state_before="long",
        position_state_after="flat",
        reason="final_exit",
        marker_json={"entry": 10.0},
        strict=True,
    )
    assert row is not None
    assert reconciled["legacy_pnl"] == pytest.approx(75.0)

    count_before = db.scalar(select(func.count(TradingAutomationSimulatedFill.id)))

    def fail_ledger(*args, **kwargs):
        raise RuntimeError("economic-ledger-write-failed")

    monkeypatch.setattr(
        economic_ledger,
        "record_automation_session_exit_fill",
        fail_ledger,
    )
    with pytest.raises(RuntimeError, match="economic-ledger-write-failed"):
        with db.begin_nested():
            paper_runner._record_sim_fill(
                db,
                session,
                action="exit_long",
                fill_type="exit",
                price=10.1,
                quantity=1.0,
                reference_price=10.11,
                pnl_usd=1.0,
                position_state_before="long",
                position_state_after="flat",
                reason="strict_failure",
                marker_json={"entry": 10.0},
                strict=True,
            )
    assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == count_before


def test_partial_and_flat_rows_roll_back_with_their_risk_transitions(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _db_paper_request()
    session = _automation_session(db)
    pe = {paper_runner.KEY_ADAPTIVE_RISK_REQUEST: request.to_payload()}
    result = _reserve(db, session, pe)
    reservation_id = uuid.UUID(result["reservation_id"])
    paper_runner._record_adaptive_db_paper_entry_fill(
        db,
        session,
        pe,
        result,
        price=10.01,
        reference_price=REFERENCE_PRICE,
        fees_usd=result["fees_usd"],
        stop_price=9.5,
        target_price=11.0,
        effective_atr=result["effective_atr"],
        decision_packet_id=None,
    )
    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, reservation_id)
    full_quantity = int(reservation.open_quantity_shares)
    full_risk = reservation.open_structural_risk_usd
    exit_quantity = max(1, full_quantity // 2)
    remaining = full_quantity - exit_quantity
    assert remaining > 0

    original_reduce = paper_runner.AdaptiveRiskReservationStore.reduce_open_exposure

    def fail_after_reduce(self, supplied_id, *, evidence, reason, session):
        state = original_reduce(
            self,
            supplied_id,
            evidence=evidence,
            reason=reason,
            session=session,
        )
        assert state.open_quantity_shares == remaining
        assert state.open_structural_risk_usd < full_risk
        raise RuntimeError("forced partial transition failure")

    monkeypatch.setattr(
        paper_runner.AdaptiveRiskReservationStore,
        "reduce_open_exposure",
        fail_after_reduce,
    )
    with pytest.raises(RuntimeError, match="forced partial transition failure"):
        paper_runner._record_adaptive_db_paper_position_fill(
            db,
            session,
            pe,
            action="exit_long",
            price=10.75,
            quantity=exit_quantity,
            remaining_open_quantity=remaining,
            reference_price=10.76,
            pnl_usd=50.0,
            reason="scale_out_target",
            marker_json={"entry": 10.01},
            decision_packet_id=None,
        )
    assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == 1
    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, reservation_id)
    assert reservation.open_quantity_shares == full_quantity
    assert reservation.open_structural_risk_usd == full_risk

    monkeypatch.setattr(
        paper_runner.AdaptiveRiskReservationStore,
        "reduce_open_exposure",
        original_reduce,
    )
    paper_runner._record_adaptive_db_paper_position_fill(
        db,
        session,
        pe,
        action="exit_long",
        price=10.75,
        quantity=exit_quantity,
        remaining_open_quantity=remaining,
        reference_price=10.76,
        pnl_usd=50.0,
        reason="scale_out_target",
        marker_json={"entry": 10.01},
        decision_packet_id=None,
    )
    assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == 2

    original_close = paper_runner.AdaptiveRiskReservationStore.close_open_exposure

    def fail_after_close(self, supplied_id, *, evidence, reason, session):
        state = original_close(
            self,
            supplied_id,
            evidence=evidence,
            reason=reason,
            session=session,
        )
        assert state.state == "closed"
        assert state.open_quantity_shares == 0
        raise RuntimeError("forced flat transition failure")

    monkeypatch.setattr(
        paper_runner.AdaptiveRiskReservationStore,
        "close_open_exposure",
        fail_after_close,
    )
    with pytest.raises(RuntimeError, match="forced flat transition failure"):
        paper_runner._record_adaptive_db_paper_position_fill(
            db,
            session,
            pe,
            action="exit_long",
            price=10.5,
            quantity=remaining,
            remaining_open_quantity=0,
            reference_price=10.51,
            pnl_usd=25.0,
            reason="trail_stop",
            marker_json={"entry": 10.01},
            decision_packet_id=None,
        )
    assert db.scalar(select(func.count(TradingAutomationSimulatedFill.id))) == 2
    assert pe.get("adaptive_risk_reservation_closed") is not True
    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, reservation_id)
    assert reservation.state == "filled"
    assert reservation.open_quantity_shares == remaining
    assert reservation.open_structural_risk_usd > 0


def test_first_entry_fill_is_cross_session_concurrent_and_idempotent(db) -> None:
    request = _db_paper_request()
    session = _automation_session(db)
    pe = {paper_runner.KEY_ADAPTIVE_RISK_REQUEST: request.to_payload()}
    result = _reserve(db, session, pe)
    reservation_id = uuid.UUID(result["reservation_id"])
    session_id = int(session.id)
    bind = db.get_bind()
    db.commit()

    make_session = sessionmaker(
        bind=bind,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    start = threading.Barrier(2, timeout=10)

    def create_or_retry_entry() -> int:
        worker = make_session()
        try:
            worker_session = worker.get(TradingAutomationSession, session_id)
            assert worker_session is not None
            worker_pe = copy.deepcopy(pe)
            worker_result = copy.deepcopy(result)
            start.wait()
            row = paper_runner._record_adaptive_db_paper_entry_fill(
                worker,
                worker_session,
                worker_pe,
                worker_result,
                price=10.01,
                reference_price=REFERENCE_PRICE,
                fees_usd=worker_result["fees_usd"],
                stop_price=9.5,
                target_price=11.0,
                effective_atr=worker_result["effective_atr"],
                decision_packet_id=None,
            )
            worker.commit()
            return int(row.id)
        except Exception:
            worker.rollback()
            raise
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        row_ids = list(pool.map(lambda _index: create_or_retry_entry(), range(2)))

    assert len(set(row_ids)) == 1
    db.expire_all()
    reservation = db.get(AdaptiveRiskReservation, reservation_id)
    lifecycle_id = f"db-paper-entry:{reservation_id}"
    fill_count = db.scalar(
        select(func.count(TradingAutomationSimulatedFill.id)).where(
            TradingAutomationSimulatedFill.marker_json[
                "adaptive_risk_lifecycle_event_id"
            ].astext
            == lifecycle_id
        )
    )
    assert fill_count == 1
    assert reservation.state == "filled"
    assert reservation.open_quantity_shares == result["quantity_shares"]
    assert reservation.opportunity_claim_id is None


def test_reconciliation_never_closes_without_canonical_flat_fill(db) -> None:
    request = _db_paper_request()
    session = _automation_session(db)
    pe = {paper_runner.KEY_ADAPTIVE_RISK_REQUEST: request.to_payload()}
    result = _reserve(db, session, pe)
    paper_runner._record_adaptive_db_paper_entry_fill(
        db,
        session,
        pe,
        result,
        price=10.01,
        reference_price=REFERENCE_PRICE,
        fees_usd=result["fees_usd"],
        stop_price=9.5,
        target_price=11.0,
        effective_atr=result["effective_atr"],
        decision_packet_id=None,
    )

    assert paper_runner._close_adaptive_db_paper_exposure(
        db,
        session,
        pe,
        reason="focused_test_flat",
    ) is False
    assert pe["adaptive_risk_reconciliation_required"] is True
    db.expire_all()
    reservation = db.get(
        AdaptiveRiskReservation, uuid.UUID(result["reservation_id"])
    )
    assert reservation.state == "filled"
    assert reservation.open_quantity_shares == result["quantity_shares"]


def test_db_paper_runtime_has_no_activation_only_dollar_literals() -> None:
    source = Path(paper_runner.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {
        float(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
        and float(node.value) in {50.0, 250.0}
    }
    assert forbidden == set()
