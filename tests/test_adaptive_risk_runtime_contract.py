from __future__ import annotations

import copy

import pytest

from app.services.trading.momentum_neural.adaptive_risk_policy import (
    AdaptiveRiskContractError,
    RiskInputEvidence,
    RISK_PACKET_SCHEMA_VERSION,
    resolve_adaptive_risk,
)
from app.services.trading.momentum_neural.adaptive_risk_runtime_contract import (
    ADAPTIVE_RISK_INPUT_CONTRACT_SHA256,
    ADAPTIVE_RISK_RESOLVER_ID,
    REQUIRED_ATOMIC_RESERVATION_DIMENSIONS,
    AdaptiveRiskRuntimeBinding,
    AdaptiveRiskLedgerSnapshot,
    assess_adaptive_risk_runtime_readiness,
    build_adaptive_risk_reservation_claim,
    load_and_verify_adaptive_risk_reservation_claim,
    require_adaptive_risk_runtime_ready,
    verify_adaptive_risk_claim_against_atomic_ledger,
)
from tests.test_adaptive_risk_policy import _inputs, _policy


def _binding(surface: str, **overrides) -> AdaptiveRiskRuntimeBinding:
    values = {
        "surface": surface,
        "resolver_id": ADAPTIVE_RISK_RESOLVER_ID,
        "packet_schema_version": RISK_PACKET_SCHEMA_VERSION,
        "input_contract_sha256": ADAPTIVE_RISK_INPUT_CONTRACT_SHA256,
        "policy_sha256": _policy().policy_sha256,
        "code_build_sha256": "a" * 64,
        "strict_packet_recomputed_at_last_risk_boundary": True,
        "decision_packet_persisted_content_addressed": True,
        "reservation_same_transaction_as_admission": True,
        "atomic_reservation_dimensions": REQUIRED_ATOMIC_RESERVATION_DIMENSIONS,
        "account_identity_bound": True,
        "order_idempotency_and_ownership_bound": True,
        "reconciliation_bound": True,
        "stale_data_fail_closed": True,
        "kill_switch_bound": True,
        "config_and_evidence_provenance_logged": True,
    }
    values.update(overrides)
    return AdaptiveRiskRuntimeBinding(**values)


def _all_bindings() -> list[AdaptiveRiskRuntimeBinding]:
    return [
        _binding(surface)
        for surface in ("replay_v3", "db_paper", "alpaca_paper", "live")
    ]


def test_missing_runtime_integrations_fail_closed() -> None:
    readiness = assess_adaptive_risk_runtime_readiness([])

    assert readiness.ready is False
    assert set(readiness.surface_reasons) == {
        "replay_v3",
        "db_paper",
        "alpaca_paper",
        "live",
    }
    assert all(reasons == ("binding_missing",) for reasons in readiness.surface_reasons.values())
    with pytest.raises(AdaptiveRiskContractError, match="runtime parity is not ready"):
        require_adaptive_risk_runtime_ready([])


def test_all_surfaces_must_share_resolver_schema_inputs_and_policy() -> None:
    bindings = _all_bindings()
    readiness = require_adaptive_risk_runtime_ready(bindings)

    assert readiness.ready is True
    assert readiness.common_policy_sha256 == _policy().policy_sha256
    assert len(readiness.binding_manifest_sha256) == 64

    bindings[-1] = _binding("live", policy_sha256="b" * 64)
    mismatch = assess_adaptive_risk_runtime_readiness(bindings)
    assert mismatch.ready is False
    assert "policy_hash_differs_across_surfaces" in mismatch.reasons


def test_atomic_three_dimension_reservation_and_no_magic_activation_caps_are_required() -> None:
    bindings = _all_bindings()
    bindings[2] = _binding(
        "alpaca_paper",
        atomic_reservation_dimensions=frozenset({"structural_risk_usd"}),
        activation_only_dollar_caps=("legacy_per_trade_usd",),
        fixed_symbol_concurrency_cap=1,
    )

    readiness = assess_adaptive_risk_runtime_readiness(bindings)

    reasons = readiness.surface_reasons["alpaca_paper"]
    assert "atomic_reservation_dimension_missing:gross_notional_usd" in reasons
    assert "atomic_reservation_dimension_missing:buying_power_impact_usd" in reasons
    assert "activation_only_dollar_cap_present" in reasons
    assert "fixed_symbol_concurrency_cap_present" in reasons


def test_reservation_claim_is_exact_content_addressed_packet_projection() -> None:
    packet = resolve_adaptive_risk(
        _policy(),
        _inputs(execution_surface="alpaca_paper"),
    ).to_decision_packet()
    claim = build_adaptive_risk_reservation_claim(packet, claim_id="cid-123")
    payload = claim.to_payload()

    loaded = load_and_verify_adaptive_risk_reservation_claim(packet, payload)
    assert loaded == claim
    assert payload["structural_risk_usd"] == packet["planned_structural_risk_usd"]
    assert payload["gross_notional_usd"] == packet["planned_notional_usd"]
    assert payload["buying_power_impact_usd"] == packet["planned_buying_power_impact_usd"]
    assert payload["account_identity_sha256"] == packet["input_snapshot"]["account_identity_sha256"]

    tampered = copy.deepcopy(payload)
    tampered["gross_notional_usd"] += 1
    with pytest.raises(AdaptiveRiskContractError, match="hash mismatch"):
        load_and_verify_adaptive_risk_reservation_claim(packet, tampered)

    rehashed_tamper = copy.deepcopy(tampered)
    rehashed_tamper.pop("claim_sha256")
    from app.services.trading.momentum_neural.adaptive_risk_runtime_contract import _sha256_json

    rehashed_tamper["claim_sha256"] = _sha256_json(rehashed_tamper)
    with pytest.raises(AdaptiveRiskContractError, match="canonical recomputation"):
        load_and_verify_adaptive_risk_reservation_claim(packet, rehashed_tamper)


def test_db_paper_is_an_explicit_resolver_surface_with_economic_parity() -> None:
    replay = resolve_adaptive_risk(_policy(), _inputs(execution_surface="replay"))
    db_paper = resolve_adaptive_risk(_policy(), _inputs(execution_surface="db_paper"))

    assert replay.valid and db_paper.valid
    assert replay.economic_resolution_sha256 == db_paper.economic_resolution_sha256


def test_reservation_claim_must_match_atomic_three_dimension_ledger() -> None:
    ledger = AdaptiveRiskLedgerSnapshot.from_dimensions(
        open_structural_risk_usd=0.0,
        pending_reserved_risk_usd=0.0,
        existing_same_symbol_structural_risk_usd=0.0,
        pending_same_symbol_structural_risk_usd=0.0,
        current_cluster_structural_risk_usd=0.0,
        pending_correlation_cluster_risk_usd=0.0,
        portfolio_gross_notional_usd=0.0,
        pending_portfolio_gross_notional_usd=0.0,
        open_buying_power_impact_usd=0.0,
        pending_buying_power_impact_usd=0.0,
    )
    inputs = _inputs(surface="alpaca_paper")
    evidence = dict(inputs.evidence)
    prior = evidence["reservation_ledger"]
    evidence["reservation_ledger"] = RiskInputEvidence(
        source=prior.source,
        observed_at=prior.observed_at,
        available_at=prior.available_at,
        content_sha256=ledger.content_sha256,
        provider_generation=prior.provider_generation,
    )
    from dataclasses import replace

    packet = resolve_adaptive_risk(
        _policy(),
        replace(inputs, evidence=evidence),
    ).to_decision_packet()
    claim = build_adaptive_risk_reservation_claim(packet, claim_id="cid-ledger")

    verified = verify_adaptive_risk_claim_against_atomic_ledger(
        packet,
        claim.to_payload(),
        ledger,
    )
    assert verified.correlation_cluster_id == "equity:v"

    changed = AdaptiveRiskLedgerSnapshot.from_dimensions(
        open_structural_risk_usd=1.0,
        pending_reserved_risk_usd=0.0,
        existing_same_symbol_structural_risk_usd=0.0,
        pending_same_symbol_structural_risk_usd=0.0,
        current_cluster_structural_risk_usd=0.0,
        pending_correlation_cluster_risk_usd=0.0,
        portfolio_gross_notional_usd=10.0,
        pending_portfolio_gross_notional_usd=0.0,
        open_buying_power_impact_usd=10.0,
        pending_buying_power_impact_usd=0.0,
    )
    with pytest.raises(AdaptiveRiskContractError, match="atomic ledger mismatch"):
        verify_adaptive_risk_claim_against_atomic_ledger(
            packet,
            claim.to_payload(),
            changed,
        )
