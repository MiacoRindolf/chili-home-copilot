from __future__ import annotations

from contextvars import copy_context
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services.trading.momentum_neural.adaptive_risk_policy import (
    RiskInputEvidence,
)
from app.services.trading.momentum_neural.adaptive_risk_request_builder import (
    AdaptiveRiskDiagnosticCaptureBinding,
    AdaptiveRiskBuilderError,
    AdaptiveRiskBuilderSource,
    AdaptiveRiskRuntimeCaptureMaterial,
    adaptive_risk_capture_binding_from_active_attestation,
    DbPaperAdmissionReceipt,
    DbPaperExecutableAdmission,
    DbPaperFinalAdmissionBundle,
    DbPaperFinalAdmissionMaterial,
    DbPaperFinalAdmissionObservation,
    adaptive_risk_source_provider,
    build_adaptive_risk_request,
    db_paper_admission_component_sha256,
    db_paper_bbo_evidence_payload,
    db_paper_eligibility_evidence_payload,
    db_paper_entry_gate_evidence_payload,
    db_paper_execution_terms_payload,
    load_db_paper_admission_receipt,
    load_adaptive_risk_builder_source,
    runtime_adaptive_risk_source,
    runtime_adaptive_risk_capture_material,
)
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    LockedAdaptiveRiskAdmissionSnapshot,
    RESERVATION_LEDGER_GENERATION,
)
from tests.first_dip_test_support import (
    captured_first_dip_runtime_for_adaptive_request,
)
from tests.test_adaptive_risk_reservation import (
    _inputs,
    _policy,
    _request,
    _snapshot,
)


UTC = timezone.utc


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source(
    *,
    surface: str = "db_paper",
    setup_family: str = "first_dip_reclaim",
) -> AdaptiveRiskBuilderSource:
    account = _snapshot(
        account_scope=(
            "db-paper:fixture:acct-a"
            if surface == "db_paper"
            else "alpaca:paper:acct-A"
        )
    )
    inputs = _inputs(
        account,
        symbol="VEEE",
        decision_id="veee-first-dip-1",
        cluster="equity:momentum-a",
        surface=surface,
    )
    evidence = dict(inputs.evidence)
    capture = evidence["capture_prefix"]
    verifier_generation = "capture-prefix-verifier-test-v1"
    evidence["capture_prefix"] = RiskInputEvidence(
        source="capture-prefix-verifier",
        observed_at=capture.observed_at,
        available_at=capture.available_at,
        content_sha256=inputs.capture_prefix_root_sha256,
        provider_generation=verifier_generation,
    )
    inputs = replace(
        inputs,
        open_structural_risk_usd=0.0,
        pending_reserved_risk_usd=0.0,
        existing_same_symbol_structural_risk_usd=0.0,
        pending_same_symbol_structural_risk_usd=0.0,
        current_cluster_structural_risk_usd=0.0,
        pending_correlation_cluster_risk_usd=0.0,
        portfolio_gross_notional_usd=0.0,
        pending_portfolio_gross_notional_usd=0.0,
        policy_buying_power_capacity_usd=account.buying_power_usd,
        open_buying_power_impact_usd=0.0,
        pending_buying_power_impact_usd=0.0,
        evidence=evidence,
    )
    binding = AdaptiveRiskDiagnosticCaptureBinding.create_diagnostic(
        run_id=inputs.replay_or_paper_run_id,
        generation=inputs.generation,
        decision_id=inputs.decision_id,
        input_prefix_sequence=47,
        input_prefix_root_sha256=inputs.capture_prefix_root_sha256,
        identity_sha256=_hash("capture-identity"),
        observed_at=evidence["capture_prefix"].observed_at,
        available_at=evidence["capture_prefix"].available_at,
        verifier_generation=verifier_generation,
    )
    return AdaptiveRiskBuilderSource(
        policy=_policy(),
        inputs=inputs,
        account_snapshot=account,
        capture_binding=binding,
        account_scope=account.account_scope,
        setup_family=setup_family,
        correlation_cluster=inputs.correlation_cluster_id,
    )


def _trusted_alpaca_first_dip_source(*, decision_id: str):
    account = _snapshot(account_scope="alpaca:paper:trusted-capture")
    inputs = _inputs(
        account,
        symbol="VEEE",
        decision_id=decision_id,
        cluster="equity:momentum-a",
        surface="alpaca_paper",
    )
    initial = _request(
        symbol="VEEE",
        decision_id=decision_id,
        client_order_id=decision_id,
        snapshot=account,
        inputs=inputs,
        cluster="equity:momentum-a",
    )
    fixture = captured_first_dip_runtime_for_adaptive_request(initial)
    binding = adaptive_risk_capture_binding_from_active_attestation(
        fixture.detector_proof
    )
    evidence = dict(fixture.request.inputs.evidence)
    evidence["capture_prefix"] = RiskInputEvidence(
        source="live-replay-capture:active-input-prefix",
        observed_at=binding.observed_at,
        available_at=binding.available_at,
        content_sha256=binding.input_prefix_root_sha256,
        provider_generation=binding.verifier_generation,
    )
    source = AdaptiveRiskBuilderSource(
        policy=fixture.request.policy,
        inputs=replace(
            fixture.request.inputs,
            # ``_inputs`` deliberately carries impossible ledger placeholders
            # so reservation tests prove the DB lock is authoritative.  This
            # builder-only fixture has no locked ledger; give the pure resolver
            # the equivalent empty-book snapshot instead of letting those
            # sentinels obscure the capture-attestation assertion.
            open_structural_risk_usd=0.0,
            pending_reserved_risk_usd=0.0,
            existing_same_symbol_structural_risk_usd=0.0,
            pending_same_symbol_structural_risk_usd=0.0,
            current_cluster_structural_risk_usd=0.0,
            pending_correlation_cluster_risk_usd=0.0,
            portfolio_gross_notional_usd=0.0,
            pending_portfolio_gross_notional_usd=0.0,
            policy_buying_power_capacity_usd=(
                fixture.request.account_snapshot.buying_power_usd
            ),
            open_buying_power_impact_usd=0.0,
            pending_buying_power_impact_usd=0.0,
            evidence=evidence,
        ),
        account_snapshot=fixture.request.account_snapshot,
        capture_binding=binding,
        account_scope=fixture.request.account_scope,
        setup_family=fixture.request.setup_family,
        correlation_cluster=fixture.request.correlation_cluster,
    )
    assert fixture.request.opportunity_key is not None
    return fixture, source, fixture.request.opportunity_key.to_payload()


def _final_source_and_boundary(
    *,
    setup_family: str = "momentum_pullback",
):
    source = _source(setup_family=setup_family)
    decision_at = source.inputs.as_of
    eligibility_observed = decision_at - timedelta(milliseconds=8)
    eligibility_available = decision_at - timedelta(milliseconds=6)
    eligibility_row_updated = decision_at - timedelta(milliseconds=7)
    execution_readiness = {
        "spread_bps": 10.0,
        "slippage_estimate_bps": source.inputs.entry_slippage_bps,
        "fee_to_target_ratio": 0.08,
    }
    bbo_observed = decision_at - timedelta(milliseconds=5)
    bbo_available = decision_at - timedelta(milliseconds=4)
    gate_observed = decision_at - timedelta(milliseconds=3)
    gate_available = decision_at - timedelta(milliseconds=2)
    opportunity = {
        "account_scope": source.account_scope,
        "symbol": source.inputs.symbol,
        "trading_date": decision_at.astimezone(
            ZoneInfo("America/New_York")
        ).date().isoformat(),
        "setup_family": source.setup_family,
    }
    gate_debug = {"pullback_low": source.inputs.structural_stop}
    if source.setup_family == "first_dip_reclaim":
        # Public detector debug is deliberately non-authorizing.  This helper
        # never fabricates the private, request-bound final receipt.
        gate_debug.update(
            {
                "first_dip_tape_confirmed": True,
                "opportunity_key": {
                    key: value
                    for key, value in opportunity.items()
                    if key != "account_scope"
                },
            }
        )
    bbo_source = "fixture:exact-nbbo"
    bbo_generation = "fixture-nbbo-generation-7"
    eligibility_source = "fixture:viability-history"
    eligibility_generation = "fixture-viability-generation-9"
    gate_source = "fixture:entry-gate-capture"
    gate_generation = "fixture-gate-generation-11"
    bbo_payload = db_paper_bbo_evidence_payload(
        symbol=source.inputs.symbol,
        bid=source.inputs.bid,
        ask=source.inputs.ask,
        quote_source=bbo_source,
        observed_at=bbo_observed,
        available_at=bbo_available,
        provider_generation=bbo_generation,
    )
    eligibility_payload = db_paper_eligibility_evidence_payload(
        symbol=source.inputs.symbol,
        viability_id=71,
        variant_id=17,
        viability_score=0.91,
        paper_eligible=True,
        observed_at=eligibility_observed,
        available_at=eligibility_available,
        row_updated_at=eligibility_row_updated,
        execution_readiness=execution_readiness,
        source=eligibility_source,
        provider_generation=eligibility_generation,
    )
    gate_payload = db_paper_entry_gate_evidence_payload(
        symbol=source.inputs.symbol,
        allowed=True,
        reason="all_gates_pass",
        debug=gate_debug,
        structural_stop=source.inputs.structural_stop,
        setup_family=source.setup_family,
        opportunity_key=opportunity,
        observed_at=gate_observed,
        available_at=gate_available,
        source=gate_source,
        provider_generation=gate_generation,
    )
    evidence = dict(source.inputs.evidence)
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
    source = replace(source, inputs=replace(source.inputs, evidence=evidence))
    boundary = {
        "decision_at": decision_at,
        "bid": source.inputs.bid,
        "ask": source.inputs.ask,
        "quote_source": bbo_source,
        "viability_id": 71,
        "variant_id": 17,
        "viability_score": 0.91,
        "paper_eligible": True,
        "eligibility_observed_at": eligibility_observed,
        "eligibility_available_at": eligibility_available,
        "eligibility_row_updated_at": eligibility_row_updated,
        "execution_readiness": execution_readiness,
        "gate_allowed": True,
        "gate_reason": "all_gates_pass",
        "gate_debug": gate_debug,
        "structural_stop": source.inputs.structural_stop,
        "opportunity_key": opportunity,
    }
    return source, boundary


def _executable_final_source_and_boundary():
    """Exercise ordinary bundle mechanics without forging first-dip proof.

    DB-paper currently observes its gate before the exact adaptive request
    exists, so it cannot honestly create the request-bound second-checkpoint
    first-dip receipt.  Positive serialization/bundle tests therefore use an
    unrelated setup; first-dip has a separate fail-closed regression below.
    """

    return _final_source_and_boundary(setup_family="momentum_pullback")


def _opportunity_key(source: AdaptiveRiskBuilderSource) -> dict[str, str]:
    return {
        "account_scope": source.account_scope,
        "symbol": source.inputs.symbol,
        "trading_date": source.inputs.as_of.astimezone(
            ZoneInfo("America/New_York")
        ).date().isoformat(),
        "setup_family": source.setup_family,
    }


def test_builder_resolves_packet_request_and_claim_from_raw_sources() -> None:
    source = _source()
    built = build_adaptive_risk_request(
        source,
        client_order_id="chili-veee-first-dip-1",
        entry_limit_price=10.0,
        opportunity_key=_opportunity_key(source),
    )

    assert built.resolution.valid is True
    assert built.resolution.quantity_shares > 0
    assert built.request.inputs == source.inputs
    assert built.request.setup_family == "first_dip_reclaim"
    assert built.reservation_claim.quantity_shares == built.resolution.quantity_shares
    assert built.reservation_claim.claim_id == "chili-veee-first-dip-1"
    assert built.audit_payload()["source_sha256"] == source.source_sha256
    assert built.audit_payload()["opportunity_key"] == _opportunity_key(source)
    assert built.audit_payload()["opportunity_key_sha256"] == (
        built.request.opportunity_key.key_sha256
    )
    assert "50" not in built.resolution.binding_constraints
    assert "250" not in built.resolution.binding_constraints


def test_builder_source_round_trip_is_content_addressed_and_tamper_closed() -> None:
    source = _source()
    payload = source.to_payload()
    loaded = load_adaptive_risk_builder_source(payload)
    assert loaded.source_sha256 == source.source_sha256

    payload["inputs"]["buying_power_usd"] += 1.0
    with pytest.raises(
        AdaptiveRiskBuilderError, match="adaptive_risk_builder_source_hash_mismatch"
    ):
        load_adaptive_risk_builder_source(payload)


def test_missing_or_invalid_capture_binding_has_stable_blocker() -> None:
    source = _source()
    payload = source.to_payload()
    payload.pop("source_sha256")
    payload.pop("capture_binding")
    payload["source_sha256"] = _hash("irrelevant")
    with pytest.raises(AdaptiveRiskBuilderError) as exc:
        load_adaptive_risk_builder_source(payload)
    assert exc.value.reason == "builder_missing_capture_binding"

    binding = source.capture_binding
    with pytest.raises(AdaptiveRiskBuilderError) as exc:
        AdaptiveRiskDiagnosticCaptureBinding(
            **{
                **binding.__dict__,
                "verification_scope": "trusted_runtime",
                "content_sha256": _hash("not-content-addressed"),
            }
        )
    assert exc.value.reason == "builder_capture_binding_trust_scope_invalid"


def test_runtime_provider_checks_surface_symbol_setup_and_cluster() -> None:
    source = _source(surface="alpaca_paper")
    with adaptive_risk_source_provider(lambda **_kwargs: source.to_payload()):
        loaded = runtime_adaptive_risk_source(
            execution_surface="alpaca_paper",
            execution_family="alpaca_spot",
            venue="alpaca",
            broker_environment="paper",
            symbol="VEEE",
            decision_id="veee-first-dip-1",
            setup_family="first_dip_reclaim",
            correlation_cluster="equity:momentum-a",
        )
        assert loaded.source_sha256 == source.source_sha256

        with pytest.raises(AdaptiveRiskBuilderError) as exc:
            runtime_adaptive_risk_source(
                execution_surface="alpaca_paper",
                symbol="PLSM",
            )
        assert exc.value.reason == "adaptive_risk_builder_boundary_mismatch"


def test_runtime_provider_carries_private_proof_atomically_without_json_fallback() -> None:
    fixture, source, opportunity = _trusted_alpaca_first_dip_source(
        decision_id="veee-atomic-runtime-capture"
    )
    calls: list[dict[str, object]] = []

    def provider(**boundary):
        calls.append(dict(boundary))
        return AdaptiveRiskRuntimeCaptureMaterial(
            source=source,
            active_capture_attestation=fixture.detector_proof,
        )

    with adaptive_risk_source_provider(provider):
        material = runtime_adaptive_risk_capture_material(
            execution_surface="alpaca_paper",
            execution_family="alpaca_spot",
            venue="alpaca",
            broker_environment="paper",
            symbol="VEEE",
            decision_id="veee-atomic-runtime-capture",
            setup_family="first_dip_reclaim",
            correlation_cluster="equity:momentum-a",
        )
    assert len(calls) == 1
    assert material.source is source
    assert material.active_capture_attestation is fixture.detector_proof

    built = build_adaptive_risk_request(
        material.source,
        client_order_id="veee-atomic-runtime-capture",
        entry_limit_price=10.0,
        opportunity_key=opportunity,
        active_capture_attestation=material.active_capture_attestation,
    )
    assert built.trusted_capture_attestation_sha256 == (
        fixture.detector_proof.attestation_sha256
    )

    with adaptive_risk_source_provider(lambda **_boundary: source.to_payload()):
        serialized = runtime_adaptive_risk_capture_material(
            execution_surface="alpaca_paper",
            symbol="VEEE",
            decision_id="veee-atomic-runtime-capture",
        )
    assert serialized.active_capture_attestation is None
    with pytest.raises(AdaptiveRiskBuilderError) as exc:
        build_adaptive_risk_request(
            serialized.source,
            client_order_id="veee-atomic-runtime-capture",
            entry_limit_price=10.0,
            opportunity_key=opportunity,
        )
    assert exc.value.reason == "builder_trusted_capture_attestation_unavailable"


def test_runtime_private_source_lease_is_one_shot_and_revoked_across_copied_contexts() -> None:
    fixture, source, _opportunity = _trusted_alpaca_first_dip_source(
        decision_id="veee-revocable-runtime-source"
    )
    material = AdaptiveRiskRuntimeCaptureMaterial(
        source=source,
        active_capture_attestation=fixture.detector_proof,
    )
    boundary = {
        "execution_surface": "alpaca_paper",
        "symbol": "VEEE",
        "decision_id": "veee-revocable-runtime-source",
    }
    with adaptive_risk_source_provider(
        lambda **_boundary: material,
        one_shot=True,
    ):
        copied = copy_context()
        assert runtime_adaptive_risk_capture_material(**boundary) is material
        with pytest.raises(AdaptiveRiskBuilderError) as consumed:
            copied.run(
                lambda: runtime_adaptive_risk_capture_material(**boundary)
            )
        assert consumed.value.reason == (
            "builder_capture_provider_already_consumed"
        )

    with pytest.raises(AdaptiveRiskBuilderError) as revoked:
        copied.run(lambda: runtime_adaptive_risk_capture_material(**boundary))
    assert revoked.value.reason == "builder_capture_provider_scope_revoked"


def test_live_runner_primary_forwards_same_atomic_capture_proof() -> None:
    from app.services.trading.momentum_neural import live_runner

    sess = SimpleNamespace(
        id=9137,
        correlation_id="first-dip-atomic-proof",
        symbol="VEEE",
    )
    cid = live_runner._entry_client_order_id(
        session_id=sess.id,
        correlation_id=sess.correlation_id,
        trade_cycles=0,
        stopout_cycles=0,
        place_n=1,
    )
    fixture, source, opportunity = _trusted_alpaca_first_dip_source(
        decision_id=cid
    )
    le = {
        "entry_place_count": 0,
        "trade_cycles": 0,
        "stopout_cycles": 0,
        "structural_stop_price": source.inputs.structural_stop,
        "entry_trigger_reason": "first_dip_reclaim",
        "entry_trigger_debug": {
            "front_side_via": "first_dip_day_leg",
            "opportunity_key": opportunity,
        },
    }
    material = AdaptiveRiskRuntimeCaptureMaterial(
        source=source,
        active_capture_attestation=fixture.detector_proof,
    )
    with adaptive_risk_source_provider(lambda **_boundary: material):
        built, place_n, actual_cid = (
            live_runner._build_adaptive_alpaca_primary_before_legacy_sizing(
                sess,
                le,
                execution_family="alpaca_spot",
                bid=source.inputs.bid,
                ask=source.inputs.ask,
            )
        )

    assert built is not None
    assert place_n == 1
    assert actual_cid == cid
    assert built.request.inputs.decision_id == cid
    assert built.trusted_capture_attestation_sha256 == (
        fixture.detector_proof.attestation_sha256
    )


def test_runtime_without_capture_source_never_falls_back() -> None:
    with adaptive_risk_source_provider(None):
        with pytest.raises(AdaptiveRiskBuilderError) as exc:
            runtime_adaptive_risk_source(
                execution_surface="alpaca_paper", symbol="VEEE"
            )
    assert exc.value.reason == "builder_missing_capture_binding"


def test_diagnostic_capture_content_hash_binds_clocks_prefix_and_identity() -> None:
    source = _source()
    binding = source.capture_binding
    with pytest.raises(AdaptiveRiskBuilderError) as exc:
        AdaptiveRiskDiagnosticCaptureBinding(
            **{
                **binding.__dict__,
                "identity_sha256": _hash("different-identity"),
            }
        )
    assert exc.value.reason == "builder_capture_binding_content_mismatch"


def test_alpaca_request_requires_private_attested_capture_provider() -> None:
    source = _source(surface="alpaca_paper")
    with pytest.raises(AdaptiveRiskBuilderError) as exc:
        build_adaptive_risk_request(
            source,
            client_order_id=source.inputs.decision_id,
            entry_limit_price=10.0,
        )
    assert exc.value.reason == "builder_trusted_capture_attestation_unavailable"


def test_alpaca_builder_binds_private_detector_attestation_and_logs_provenance() -> None:
    fixture, source, opportunity = _trusted_alpaca_first_dip_source(
        decision_id="veee-private-active-capture"
    )
    built = build_adaptive_risk_request(
        source,
        client_order_id=source.inputs.decision_id,
        entry_limit_price=10.0,
        opportunity_key=opportunity,
        active_capture_attestation=fixture.detector_proof,
    )

    assert built.request.inputs == source.inputs
    assert built.request.setup_family == "first_dip_reclaim"
    assert built.trusted_capture_attestation_sha256 == (
        fixture.detector_proof.attestation_sha256
    )
    assert built.audit_payload()["trusted_capture_attestation_sha256"] == (
        fixture.detector_proof.attestation_sha256
    )

    with pytest.raises(AdaptiveRiskBuilderError) as alternate_cid:
        build_adaptive_risk_request(
            source,
            client_order_id="different-order-for-same-capture-proof",
            entry_limit_price=10.0,
            opportunity_key=opportunity,
            active_capture_attestation=fixture.detector_proof,
        )
    assert alternate_cid.value.detail == "client_order_id_decision_id"

    other_fixture, _other_source, _other_opportunity = (
        _trusted_alpaca_first_dip_source(
            decision_id="veee-private-active-capture-other"
        )
    )
    with pytest.raises(AdaptiveRiskBuilderError) as mismatch:
        build_adaptive_risk_request(
            source,
            client_order_id=source.inputs.decision_id,
            entry_limit_price=10.0,
            opportunity_key=opportunity,
            active_capture_attestation=other_fixture.detector_proof,
        )
    assert mismatch.value.reason == (
        "builder_trusted_capture_attestation_mismatch"
    )

    with pytest.raises(AdaptiveRiskBuilderError) as final_lineage:
        build_adaptive_risk_request(
            source,
            client_order_id=source.inputs.decision_id,
            entry_limit_price=10.0,
            opportunity_key=opportunity,
            active_capture_attestation=fixture.final_proof,
        )
    assert final_lineage.value.detail == "final_lineage_cannot_build_request"

    stale_source = replace(
        source,
        inputs=replace(
            source.inputs,
            as_of=fixture.detector_proof.expires_at + timedelta(microseconds=1),
        ),
    )
    with pytest.raises(AdaptiveRiskBuilderError) as stale:
        build_adaptive_risk_request(
            stale_source,
            client_order_id=source.inputs.decision_id,
            entry_limit_price=10.0,
            opportunity_key=opportunity,
            active_capture_attestation=fixture.detector_proof,
        )
    assert stale.value.detail == "capture_attestation_expired_or_from_future"


def test_db_paper_final_observation_binds_exact_clocks_gate_and_opportunity() -> None:
    source, boundary = _executable_final_source_and_boundary()
    observation = DbPaperFinalAdmissionObservation.create(source, **boundary)

    assert observation.source_sha256 == source.source_sha256
    assert observation.decision_at == source.inputs.as_of
    assert observation.opportunity_key == {
        "account_scope": source.account_scope,
        "symbol": source.inputs.symbol,
        "trading_date": source.inputs.as_of.astimezone(
            ZoneInfo("America/New_York")
        ).date().isoformat(),
        "setup_family": source.setup_family,
    }
    assert observation.first_dip_final_admission_envelope_sha256 is None
    assert len(observation.content_sha256) == 64


def test_db_paper_final_observation_rejects_future_or_missing_capture_fact() -> None:
    source, boundary = _final_source_and_boundary()
    future = source.inputs.as_of + timedelta(milliseconds=1)
    evidence = dict(source.inputs.evidence)
    prior = evidence["paper_entry_gate"]
    evidence["paper_entry_gate"] = replace(
        prior,
        observed_at=future,
        available_at=future,
    )
    future_source = replace(source, inputs=replace(source.inputs, evidence=evidence))
    with pytest.raises(AdaptiveRiskBuilderError) as exc:
        DbPaperFinalAdmissionObservation.create(future_source, **boundary)
    assert exc.value.reason == "db_paper_final_evidence_from_future"

    evidence = dict(source.inputs.evidence)
    evidence.pop("paper_entry_gate")
    missing_source = replace(source, inputs=replace(source.inputs, evidence=evidence))
    with pytest.raises(AdaptiveRiskBuilderError) as exc:
        DbPaperFinalAdmissionObservation.create(missing_source, **boundary)
    assert exc.value.reason == "db_paper_final_evidence_missing"


def test_db_paper_forged_first_dip_boolean_cannot_replace_typed_receipt() -> None:
    source, boundary = _final_source_and_boundary(
        setup_family="first_dip_reclaim"
    )
    with pytest.raises(AdaptiveRiskBuilderError) as exc:
        DbPaperFinalAdmissionObservation.create(source, **boundary)
    assert exc.value.reason == "db_paper_final_entry_gate_veto"
    assert exc.value.detail == "first_dip_final_admission_active_context_missing"


def test_db_paper_receipt_binds_source_request_packet_reservation_and_generation() -> None:
    source, boundary = _executable_final_source_and_boundary()
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
        "pending_buying_power_impact_usd": 0.0,
    }
    ledger_payload = {
        "schema_version": RESERVATION_LEDGER_GENERATION,
        "account_scope": source.account_scope,
        "aggregates": aggregates,
        "active_reservations": [],
        "pending_settlements": [],
        "paper_position_bindings": [],
    }
    locked = LockedAdaptiveRiskAdmissionSnapshot.create(
        account_scope=source.account_scope,
        symbol=source.inputs.symbol,
        correlation_cluster=source.correlation_cluster,
        account_snapshot_sha256=source.account_snapshot.snapshot_sha256,
        transaction_id="1",
        backend_pid=1,
        lock_receipt_id="00000000-0000-0000-0000-000000000001",
        observed_at=source.inputs.as_of,
        aggregates=aggregates,
        ledger_payload=ledger_payload,
        policy_buying_power_capacity_usd=source.inputs.buying_power_usd,
    )
    eligibility = db_paper_eligibility_evidence_payload(
        symbol=source.inputs.symbol,
        viability_id=boundary["viability_id"],
        variant_id=boundary["variant_id"],
        viability_score=boundary["viability_score"],
        paper_eligible=boundary["paper_eligible"],
        observed_at=boundary["eligibility_observed_at"],
        available_at=boundary["eligibility_available_at"],
        row_updated_at=boundary["eligibility_row_updated_at"],
        execution_readiness=boundary["execution_readiness"],
        source=source.inputs.evidence["paper_eligibility"].source,
        provider_generation=source.inputs.evidence[
            "paper_eligibility"
        ].provider_generation,
    )
    terms = db_paper_execution_terms_payload(
        effective_config_sha256=source.inputs.effective_config_sha256,
        stop_atr_mult=1.0,
        target_atr_mult=2.0,
        vol_floor_mult=0.5,
        reward_risk=2.0,
        entry_slippage_bps=source.inputs.entry_slippage_bps,
        exit_slippage_bps=source.inputs.exit_slippage_bps,
        fee_to_target_ratio=0.08,
    )
    material = DbPaperFinalAdmissionMaterial.create(
        source,
        quote_source=boundary["quote_source"],
        gate_allowed=True,
        gate_reason=boundary["gate_reason"],
        gate_debug=boundary["gate_debug"],
        opportunity_key=boundary["opportunity_key"],
        eligibility=eligibility,
        execution_terms=terms,
    )
    evidence = dict(source.inputs.evidence)
    evidence["reservation_ledger"] = RiskInputEvidence(
        source="postgresql:adaptive_risk_reservations",
        observed_at=locked.observed_at,
        available_at=locked.observed_at,
        content_sha256=locked.ledger_sha256,
        provider_generation=RESERVATION_LEDGER_GENERATION,
    )
    source = replace(
        source,
        inputs=replace(
            source.inputs,
            as_of=locked.observed_at,
            evidence=evidence,
        ),
    )
    bundle = DbPaperFinalAdmissionBundle.create(
        material,
        source,
        locked_risk_snapshot=locked,
    )
    observation = DbPaperFinalAdmissionObservation.create(source, **boundary)
    executable_entry_price = source.inputs.ask * (
        1.0 + source.inputs.entry_slippage_bps / 10_000.0
    )
    built = build_adaptive_risk_request(
        source,
        client_order_id="db-paper-71-final-boundary",
        entry_limit_price=executable_entry_price,
        opportunity_key=bundle.opportunity_key,
    )
    reservation_id = "00000000-0000-0000-0000-000000000071"
    executable = DbPaperExecutableAdmission.create(
        bundle,
        observation,
        built.request,
        built.resolution,
        reservation_id=reservation_id,
        structural_risk_usd=built.resolution.planned_structural_risk_usd,
        gross_notional_usd=built.resolution.planned_notional_usd,
        buying_power_impact_usd=(
            built.resolution.planned_buying_power_impact_usd
        ),
        entry_price=executable_entry_price,
        reference_price=(source.inputs.bid + source.inputs.ask) / 2.0,
        stop_price=source.inputs.structural_stop,
        target_price=11.0,
        fees_usd=1.25,
        effective_atr=0.05,
    )
    receipt = DbPaperAdmissionReceipt.create(
        source,
        observation,
        built.request,
        executable,
        decision_packet_sha256=built.resolution.decision_packet_sha256,
        reservation_id=reservation_id,
        connection_generation="db-paper-session:71",
    )
    loaded = load_db_paper_admission_receipt(receipt.to_payload())
    assert loaded == receipt
    assert loaded.reservation_id == reservation_id
    assert loaded.client_order_id == built.request.client_order_id
    assert loaded.opportunity_sha256 == observation.opportunity_sha256

    tampered = receipt.to_payload()
    tampered["generation"] += 1
    with pytest.raises(AdaptiveRiskBuilderError) as exc:
        load_db_paper_admission_receipt(tampered)
    assert exc.value.reason == "db_paper_admission_receipt_hash_mismatch"
