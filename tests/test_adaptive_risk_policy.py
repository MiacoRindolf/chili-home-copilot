from __future__ import annotations

import ast
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import uuid

import pytest

from app.services.trading.momentum_neural.adaptive_risk_policy import (
    AdaptiveRiskInputs,
    AdaptiveRiskPolicy,
    AdaptiveRiskContractError,
    RiskInputEvidence,
    load_and_verify_adaptive_risk_decision_packet,
    resolve_adaptive_risk,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 14, 13, 5, tzinfo=UTC)


def _policy() -> AdaptiveRiskPolicy:
    return AdaptiveRiskPolicy(
        policy_version="adaptive-risk-test-v1",
        policy_source="recorded_fixture",
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
        reservation_data_max_age_seconds=0.25,
        context_data_max_age_seconds=60.0,
    )


def _evidence(*, available_at: datetime = NOW - timedelta(milliseconds=20)):
    return RiskInputEvidence(
        source="recorded_fixture",
        observed_at=available_at - timedelta(milliseconds=2),
        available_at=available_at,
        content_sha256="a" * 64,
        provider_generation="fixture-generation-7",
    )


def _inputs(*, surface: str = "replay", **overrides) -> AdaptiveRiskInputs:
    evidence = {
        name: _evidence()
        for name in (
            "account",
            "daily_pnl",
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
    values = {
        "decision_id": "veee-entry-fixture",
        "replay_or_paper_run_id": str(uuid.UUID(int=17)),
        "generation": 7,
        "execution_surface": surface,
        "execution_family": "alpaca_spot",
        "venue": "alpaca",
        "broker_environment": "paper",
        "symbol": "VEEE",
        "side": "long",
        "as_of": NOW,
        "account_identity_sha256": "b" * 64,
        "code_build_sha256": "c" * 64,
        "effective_config_sha256": "d" * 64,
        "feature_flags_sha256": "e" * 64,
        "capture_prefix_root_sha256": "f" * 64,
        "equity_usd": 100_000.0,
        "buying_power_usd": 400_000.0,
        "broker_day_change_usd": 0.0,
        "local_realized_pnl_usd": 0.0,
        "open_structural_risk_usd": 0.0,
        "pending_reserved_risk_usd": 0.0,
        "existing_same_symbol_structural_risk_usd": 0.0,
        "pending_same_symbol_structural_risk_usd": 0.0,
        "current_cluster_structural_risk_usd": 0.0,
        "pending_correlation_cluster_risk_usd": 0.0,
        "portfolio_gross_notional_usd": 0.0,
        "pending_portfolio_gross_notional_usd": 0.0,
        "policy_buying_power_capacity_usd": 400_000.0,
        "open_buying_power_impact_usd": 0.0,
        "pending_buying_power_impact_usd": 0.0,
        "candidate_buying_power_impact_per_share_usd": 10.0,
        "bid": 9.99,
        "ask": 10.00,
        "structural_stop": 9.50,
        "entry_slippage_bps": 10.0,
        "exit_slippage_bps": 20.0,
        "fees_per_share_usd": 0.005,
        "setup_quality": 0.80,
        "realized_volatility_fraction": 0.05,
        "average_daily_volume_shares": 5_000_000.0,
        "recent_volume_shares": 500_000.0,
        "executable_depth_shares": 100_000.0,
        "correlation_cluster_id": "equity:v",
        "evidence": evidence,
    }
    values.update(overrides)
    if "policy_buying_power_capacity_usd" not in overrides:
        values["policy_buying_power_capacity_usd"] = values["buying_power_usd"]
    return AdaptiveRiskInputs(**values)


def test_replay_and_alpaca_paper_have_identical_economic_resolution() -> None:
    replay_inputs = _inputs(surface="replay")
    paper_inputs = replace(
        replay_inputs,
        execution_surface="alpaca_paper",
        decision_id="veee-paper-fixture",
        replay_or_paper_run_id=str(uuid.UUID(int=18)),
    )
    replay = resolve_adaptive_risk(_policy(), replay_inputs)
    paper = resolve_adaptive_risk(_policy(), paper_inputs)

    assert replay.valid and paper.valid
    assert replay.economic_input_sha256 == paper.economic_input_sha256
    assert replay.economic_resolution_sha256 == paper.economic_resolution_sha256
    assert replay.quantity_shares == paper.quantity_shares
    assert replay.planned_structural_risk_usd == paper.planned_structural_risk_usd
    assert replay.decision_packet_sha256 != paper.decision_packet_sha256


def test_buying_power_never_increases_r_but_can_reduce_notional() -> None:
    policy = _policy()
    baseline = resolve_adaptive_risk(policy, _inputs(buying_power_usd=400_000))
    more_bp = resolve_adaptive_risk(policy, _inputs(buying_power_usd=800_000))
    less_bp = resolve_adaptive_risk(policy, _inputs(buying_power_usd=10_000))

    assert baseline.valid and more_bp.valid and less_bp.valid
    assert baseline.base_r_usd == more_bp.base_r_usd == less_bp.base_r_usd
    assert baseline.candidate_risk_budget_usd == more_bp.candidate_risk_budget_usd
    assert more_bp.quantity_shares == baseline.quantity_shares
    assert less_bp.quantity_shares < baseline.quantity_shares
    assert "quantity:buying_power_policy" in less_bp.binding_constraints


@pytest.mark.parametrize(
    ("impact_per_share", "expected_quantity"),
    ((5.0, 1_000), (10.0, 500), (15.0, 333)),
)
def test_candidate_buying_power_impact_has_its_own_share_cap(
    impact_per_share: float,
    expected_quantity: int,
) -> None:
    resolved = resolve_adaptive_risk(
        _policy(),
        _inputs(
            buying_power_usd=10_000,
            candidate_buying_power_impact_per_share_usd=impact_per_share,
        ),
    )

    assert resolved.valid
    assert resolved.quantity_shares == expected_quantity
    assert "quantity:buying_power_policy" in resolved.binding_constraints
    assert resolved.planned_buying_power_impact_usd <= 5_000


def test_zero_reservations_preserve_prior_budget_headroom() -> None:
    resolved = resolve_adaptive_risk(_policy(), _inputs())

    assert resolved.valid
    assert resolved.risk_budget_caps_usd[
        "symbol_remaining_after_existing_and_pending"
    ] == 3_000
    assert resolved.risk_budget_caps_usd["correlation_cluster_remaining"] == 4_000
    assert resolved.buying_power_caps_usd[
        "buying_power_policy_remaining_after_reservations"
    ] == 200_000
    assert resolved.buying_power_caps_usd["broker_available_buying_power"] == 400_000
    assert resolved.notional_caps_usd[
        "portfolio_gross_remaining_after_pending"
    ] == 200_000


@pytest.mark.parametrize("invalid_fraction", (0.0, 1.01))
def test_buying_power_fraction_must_be_a_true_positive_fraction(
    invalid_fraction: float,
) -> None:
    with pytest.raises(AdaptiveRiskContractError, match=r"must be in \(0, 1\]"):
        replace(
            _policy(),
            max_buying_power_fraction_for_notional=invalid_fraction,
        )


def test_daily_budget_reserves_open_pending_drawdown_and_candidate_risk() -> None:
    inputs = _inputs(
        broker_day_change_usd=-1_000,
        local_realized_pnl_usd=-900,
        open_structural_risk_usd=1_200,
        pending_reserved_risk_usd=800,
        portfolio_gross_notional_usd=10_000,
        pending_portfolio_gross_notional_usd=5_000,
        buying_power_usd=390_000,
        policy_buying_power_capacity_usd=400_000,
        open_buying_power_impact_usd=10_000,
        pending_buying_power_impact_usd=5_000,
    )
    resolved = resolve_adaptive_risk(_policy(), inputs)
    expected_before_candidate = 10_000 - 1_000 - 1_200 - 800 - 100

    assert resolved.valid
    assert (
        resolved.risk_budget_caps_usd[
            "daily_remaining_after_open_pending_and_reserve"
        ]
        == expected_before_candidate
    )
    assert resolved.remaining_daily_risk_after_candidate_usd == (
        expected_before_candidate - resolved.planned_structural_risk_usd
    )


def test_concurrency_emerges_from_aggregate_risk_not_one_symbol_cap() -> None:
    first = resolve_adaptive_risk(_policy(), _inputs())
    assert first.valid
    second = resolve_adaptive_risk(
        _policy(),
        _inputs(
            symbol="NXTC",
            open_structural_risk_usd=first.planned_structural_risk_usd,
            current_cluster_structural_risk_usd=first.planned_structural_risk_usd,
            portfolio_gross_notional_usd=first.planned_notional_usd,
            buying_power_usd=400_000 - first.planned_buying_power_impact_usd,
            policy_buying_power_capacity_usd=400_000,
            open_buying_power_impact_usd=first.planned_buying_power_impact_usd,
        ),
    )

    assert second.valid
    assert second.quantity_shares > 0
    assert second.remaining_portfolio_risk_after_candidate_usd >= 0
    assert second.remaining_cluster_risk_after_candidate_usd >= 0


def test_same_symbol_add_subtracts_existing_and_pending_structural_risk() -> None:
    resolved = resolve_adaptive_risk(
        _policy(),
        _inputs(
            open_structural_risk_usd=1_250,
            pending_reserved_risk_usd=1_250,
            existing_same_symbol_structural_risk_usd=1_250,
            pending_same_symbol_structural_risk_usd=1_250,
            current_cluster_structural_risk_usd=1_250,
            pending_correlation_cluster_risk_usd=1_250,
            portfolio_gross_notional_usd=10_000,
            pending_portfolio_gross_notional_usd=5_000,
            buying_power_usd=390_000,
            policy_buying_power_capacity_usd=400_000,
            open_buying_power_impact_usd=10_000,
            pending_buying_power_impact_usd=5_000,
        ),
    )

    assert resolved.valid
    assert (
        resolved.risk_budget_caps_usd[
            "symbol_remaining_after_existing_and_pending"
        ]
        == 500
    )
    assert resolved.candidate_risk_budget_usd == 500
    assert (
        "risk_budget:symbol_remaining_after_existing_and_pending"
        in resolved.binding_constraints
    )


def test_pending_correlation_cluster_risk_cannot_reuse_cluster_headroom() -> None:
    resolved = resolve_adaptive_risk(
        _policy(),
        _inputs(
            pending_reserved_risk_usd=2_900,
            open_structural_risk_usd=1_000,
            current_cluster_structural_risk_usd=1_000,
            pending_correlation_cluster_risk_usd=2_900,
            portfolio_gross_notional_usd=10_000,
            pending_portfolio_gross_notional_usd=5_000,
            buying_power_usd=390_000,
            policy_buying_power_capacity_usd=400_000,
            open_buying_power_impact_usd=10_000,
            pending_buying_power_impact_usd=5_000,
        ),
    )

    assert resolved.valid
    assert resolved.risk_budget_caps_usd["correlation_cluster_remaining"] == 100
    assert resolved.candidate_risk_budget_usd == 100
    assert (
        "risk_budget:correlation_cluster_remaining"
        in resolved.binding_constraints
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    (
        (
            {"existing_same_symbol_structural_risk_usd": 1},
            "same_symbol_existing_risk_exceeds_open_risk",
        ),
        (
            {"current_cluster_structural_risk_usd": 1},
            "cluster_existing_risk_exceeds_open_risk",
        ),
        (
            {
                "open_structural_risk_usd": 1,
                "existing_same_symbol_structural_risk_usd": 1,
            },
            "same_symbol_existing_risk_exceeds_cluster_risk",
        ),
        (
            {"pending_same_symbol_structural_risk_usd": 1},
            "same_symbol_pending_risk_exceeds_pending_risk",
        ),
        (
            {"pending_correlation_cluster_risk_usd": 1},
            "cluster_pending_risk_exceeds_pending_risk",
        ),
        (
            {
                "pending_reserved_risk_usd": 1,
                "pending_same_symbol_structural_risk_usd": 1,
            },
            "same_symbol_pending_risk_exceeds_cluster_pending_risk",
        ),
        (
            {"portfolio_gross_notional_usd": 1},
            "open_buying_power_impact_missing",
        ),
        (
            {"pending_portfolio_gross_notional_usd": 1},
            "pending_buying_power_impact_missing",
        ),
        (
            {"policy_buying_power_capacity_usd": 399_999},
            "policy_buying_power_capacity_below_available",
        ),
        (
            {"policy_buying_power_capacity_usd": 400_001},
            "policy_buying_power_capacity_exceeds_reconstructable",
        ),
    ),
)
def test_specific_risk_reservations_cannot_exceed_aggregate_claims(
    overrides: dict[str, float],
    reason: str,
) -> None:
    resolved = resolve_adaptive_risk(_policy(), _inputs(**overrides))

    assert not resolved.valid
    assert resolved.quantity_shares == 0
    assert reason in resolved.rejection_reasons


@pytest.mark.parametrize(
    ("overrides", "reason"),
    (
        (
            {"open_structural_risk_usd": 1},
            "open_reservation_dimensions_incomplete",
        ),
        (
            {"portfolio_gross_notional_usd": 1},
            "open_reservation_dimensions_incomplete",
        ),
        (
            {"open_buying_power_impact_usd": 1},
            "open_reservation_dimensions_incomplete",
        ),
        (
            {"pending_reserved_risk_usd": 1},
            "pending_reservation_dimensions_incomplete",
        ),
        (
            {"pending_portfolio_gross_notional_usd": 1},
            "pending_reservation_dimensions_incomplete",
        ),
        (
            {"pending_buying_power_impact_usd": 1},
            "pending_reservation_dimensions_incomplete",
        ),
    ),
)
def test_reservation_phases_require_risk_gross_and_buying_power_dimensions(
    overrides: dict[str, float],
    reason: str,
) -> None:
    resolved = resolve_adaptive_risk(_policy(), _inputs(**overrides))

    assert not resolved.valid
    assert resolved.quantity_shares == 0
    assert reason in resolved.rejection_reasons


def test_pending_gross_and_total_buying_power_reservations_bind_independently() -> None:
    gross_bound = resolve_adaptive_risk(
        replace(
            _policy(),
            max_buying_power_fraction_for_notional=1.0,
            max_portfolio_gross_fraction_of_equity=0.30,
        ),
        _inputs(
            pending_reserved_risk_usd=100,
            pending_portfolio_gross_notional_usd=29_000,
            pending_buying_power_impact_usd=29_000,
        ),
    )
    buying_power_bound = resolve_adaptive_risk(
        replace(_policy(), max_portfolio_gross_fraction_of_equity=4.0),
        _inputs(
            pending_reserved_risk_usd=100,
            pending_portfolio_gross_notional_usd=199_000,
            pending_buying_power_impact_usd=199_000,
        ),
    )

    assert gross_bound.valid and buying_power_bound.valid
    assert gross_bound.notional_caps_usd[
        "portfolio_gross_remaining_after_pending"
    ] == 1_000
    assert gross_bound.buying_power_caps_usd[
        "buying_power_policy_remaining_after_reservations"
    ] == 371_000
    assert gross_bound.planned_notional_usd <= 1_000
    assert buying_power_bound.buying_power_caps_usd[
        "buying_power_policy_remaining_after_reservations"
    ] == 1_000
    assert buying_power_bound.notional_caps_usd[
        "portfolio_gross_remaining_after_pending"
    ] == 201_000
    assert buying_power_bound.planned_buying_power_impact_usd <= 1_000
    assert "quantity:notional" in gross_bound.binding_constraints
    assert "quantity:buying_power_policy" in buying_power_bound.binding_constraints


def test_sequential_candidates_consume_first_decisions_pending_reservations() -> None:
    policy = replace(
        _policy(),
        cluster_risk_fraction_of_equity=0.02,
        max_buying_power_fraction_for_notional=0.30,
        max_portfolio_gross_fraction_of_equity=0.30,
    )
    first = resolve_adaptive_risk(policy, _inputs(buying_power_usd=100_000))
    assert first.valid

    second = resolve_adaptive_risk(
        policy,
        _inputs(
            symbol="NXTC",
            buying_power_usd=100_000,
            pending_reserved_risk_usd=first.planned_structural_risk_usd,
            pending_correlation_cluster_risk_usd=(
                first.planned_structural_risk_usd
            ),
            pending_portfolio_gross_notional_usd=first.planned_notional_usd,
            pending_buying_power_impact_usd=(
                first.planned_buying_power_impact_usd
            ),
        ),
    )

    assert second.valid
    assert second.risk_budget_caps_usd["correlation_cluster_remaining"] == (
        2_000 - first.planned_structural_risk_usd
    )
    assert second.notional_caps_usd[
        "portfolio_gross_remaining_after_pending"
    ] == (30_000 - first.planned_notional_usd)
    assert second.buying_power_caps_usd[
        "buying_power_policy_remaining_after_reservations"
    ] == (30_000 - first.planned_buying_power_impact_usd)
    assert (
        first.planned_structural_risk_usd
        + second.planned_structural_risk_usd
        <= 2_000 + 1e-8
    )
    assert first.planned_notional_usd + second.planned_notional_usd <= 30_000 + 1e-8


def test_buying_power_headroom_is_invariant_to_reflection_and_fill_timing() -> None:
    policy = replace(_policy(), max_buying_power_fraction_for_notional=0.30)
    first = resolve_adaptive_risk(policy, _inputs(buying_power_usd=100_000))
    assert first.valid
    reservations = {
        "symbol": "NXTC",
        "pending_reserved_risk_usd": first.planned_structural_risk_usd,
        "pending_correlation_cluster_risk_usd": first.planned_structural_risk_usd,
        "pending_portfolio_gross_notional_usd": first.planned_notional_usd,
        "policy_buying_power_capacity_usd": 100_000,
        "pending_buying_power_impact_usd": first.planned_buying_power_impact_usd,
    }
    before_reflection = resolve_adaptive_risk(
        policy,
        _inputs(buying_power_usd=100_000, **reservations),
    )
    after_reflection = resolve_adaptive_risk(
        policy,
        _inputs(
            buying_power_usd=100_000 - first.planned_buying_power_impact_usd,
            **reservations,
        ),
    )
    after_fill = resolve_adaptive_risk(
        policy,
        _inputs(
            symbol="NXTC",
            buying_power_usd=100_000 - first.planned_buying_power_impact_usd,
            policy_buying_power_capacity_usd=100_000,
            open_structural_risk_usd=first.planned_structural_risk_usd,
            current_cluster_structural_risk_usd=(
                first.planned_structural_risk_usd
            ),
            portfolio_gross_notional_usd=first.planned_notional_usd,
            open_buying_power_impact_usd=first.planned_buying_power_impact_usd,
        ),
    )

    assert before_reflection.valid and after_reflection.valid and after_fill.valid
    cap = "buying_power_policy_remaining_after_reservations"
    assert before_reflection.buying_power_caps_usd[cap] == (
        30_000 - first.planned_buying_power_impact_usd
    )
    assert after_reflection.buying_power_caps_usd[cap] == (
        30_000 - first.planned_buying_power_impact_usd
    )
    assert after_fill.buying_power_caps_usd[cap] == (
        30_000 - first.planned_buying_power_impact_usd
    )
    assert (
        before_reflection.quantity_shares
        == after_reflection.quantity_shares
        == after_fill.quantity_shares
    )
    assert (
        before_reflection.planned_structural_risk_usd
        == after_reflection.planned_structural_risk_usd
        == after_fill.planned_structural_risk_usd
    )
    assert (
        before_reflection.planned_notional_usd
        == after_reflection.planned_notional_usd
        == after_fill.planned_notional_usd
    )


def test_spread_slippage_and_volatility_cost_reduce_executable_quantity() -> None:
    clean = resolve_adaptive_risk(
        _policy(),
        _inputs(
            entry_slippage_bps=0,
            exit_slippage_bps=0,
            realized_volatility_fraction=0.03,
        ),
    )
    costly = resolve_adaptive_risk(
        _policy(),
        _inputs(
            bid=9.80,
            ask=10.00,
            entry_slippage_bps=80,
            exit_slippage_bps=120,
            realized_volatility_fraction=0.10,
        ),
    )

    assert clean.valid and costly.valid
    assert costly.risk_per_share_usd > clean.risk_per_share_usd
    assert costly.volatility_multiplier < clean.volatility_multiplier
    assert costly.quantity_shares < clean.quantity_shares


def test_wider_spread_alone_increases_reserved_risk_per_share() -> None:
    tight = resolve_adaptive_risk(_policy(), _inputs(bid=9.99, ask=10.00))
    wide = resolve_adaptive_risk(_policy(), _inputs(bid=9.70, ask=10.00))

    assert tight.valid and wide.valid
    assert wide.risk_per_share_usd > tight.risk_per_share_usd
    assert wide.quantity_shares < tight.quantity_shares


def test_liquidity_and_correlation_each_bind_and_are_logged() -> None:
    liquidity = resolve_adaptive_risk(
        _policy(),
        _inputs(executable_depth_shares=100),
    )
    correlation = resolve_adaptive_risk(
        _policy(),
        _inputs(
            open_structural_risk_usd=3_900,
            current_cluster_structural_risk_usd=3_900,
            portfolio_gross_notional_usd=10_000,
            buying_power_usd=390_000,
            policy_buying_power_capacity_usd=400_000,
            open_buying_power_impact_usd=10_000,
        ),
    )

    assert liquidity.valid
    assert liquidity.quantity_shares == 50
    assert "quantity:executable_depth_participation" in liquidity.binding_constraints
    assert correlation.valid
    assert correlation.candidate_risk_budget_usd == 100
    assert "risk_budget:correlation_cluster_remaining" in correlation.binding_constraints


def test_missing_or_stale_required_input_fails_closed_at_zero_quantity() -> None:
    missing = _inputs(evidence={})
    missing_result = resolve_adaptive_risk(_policy(), missing)
    stale_evidence = dict(_inputs().evidence)
    stale_evidence["bbo"] = _evidence(available_at=NOW - timedelta(seconds=3))
    stale = resolve_adaptive_risk(_policy(), _inputs(evidence=stale_evidence))

    assert not missing_result.valid
    assert missing_result.quantity_shares == 0
    assert "evidence_missing:bbo" in missing_result.rejection_reasons
    assert not stale.valid
    assert stale.quantity_shares == 0
    assert "evidence_stale:bbo" in stale.rejection_reasons


def test_recent_receive_cannot_make_an_old_provider_event_fresh() -> None:
    evidence = dict(_inputs().evidence)
    evidence["bbo"] = replace(
        _evidence(available_at=NOW - timedelta(milliseconds=10)),
        observed_at=NOW - timedelta(seconds=5),
    )

    resolved = resolve_adaptive_risk(_policy(), _inputs(evidence=evidence))

    assert not resolved.valid
    assert resolved.quantity_shares == 0
    assert "evidence_stale:bbo" in resolved.rejection_reasons
    assert "evidence_observed_clock_stale:bbo" in resolved.rejection_reasons
    assert "evidence_availability_clock_stale:bbo" not in resolved.rejection_reasons


@pytest.mark.parametrize(
    "evidence_name",
    ("reservation_ledger", "portfolio_heat", "candidate_buying_power_estimate"),
)
def test_reservation_evidence_uses_tight_freshness_budget(
    evidence_name: str,
) -> None:
    evidence = dict(_inputs().evidence)
    evidence[evidence_name] = _evidence(
        available_at=NOW - timedelta(milliseconds=300)
    )
    resolved = resolve_adaptive_risk(_policy(), _inputs(evidence=evidence))

    assert not resolved.valid
    assert resolved.quantity_shares == 0
    assert f"evidence_stale:{evidence_name}" in resolved.rejection_reasons


def test_decision_packet_logs_all_inputs_policy_caps_and_provenance() -> None:
    resolved = resolve_adaptive_risk(_policy(), _inputs())
    packet = resolved.to_decision_packet()

    assert resolved.valid
    assert len(packet["decision_packet_sha256"]) == 64
    assert len(packet["economic_resolution_sha256"]) == 64
    assert packet["input_snapshot"]["evidence"]["account"]["source"] == "recorded_fixture"
    assert packet["input_snapshot"]["as_of"] == "2026-07-14T13:05:00Z"
    assert packet["input_snapshot"]["evidence"]["account"][
        "available_at"
    ].endswith("Z")
    assert packet["policy_snapshot"]["policy_version"] == "adaptive-risk-test-v1"
    assert set(packet["risk_budget_caps_usd"]) >= {
        "quality_and_volatility_adjusted_r",
        "symbol_remaining_after_existing_and_pending",
        "daily_remaining_after_open_pending_and_reserve",
        "portfolio_remaining_after_open_and_pending",
        "correlation_cluster_remaining",
    }
    assert set(packet["buying_power_caps_usd"]) == {
        "broker_available_buying_power",
        "buying_power_policy_remaining_after_reservations",
    }
    assert packet["planned_buying_power_impact_usd"] == (
        packet["quantity_shares"]
        * packet["input_snapshot"]["candidate_buying_power_impact_per_share_usd"]
    )


def test_reservations_are_json_safe_hashed_provenanced_and_strictly_loaded() -> None:
    inputs = _inputs(
        open_structural_risk_usd=10,
        pending_reserved_risk_usd=20,
        existing_same_symbol_structural_risk_usd=10,
        pending_same_symbol_structural_risk_usd=20,
        current_cluster_structural_risk_usd=10,
        pending_correlation_cluster_risk_usd=20,
        portfolio_gross_notional_usd=100,
        pending_portfolio_gross_notional_usd=300,
        buying_power_usd=399_900,
        policy_buying_power_capacity_usd=400_000,
        open_buying_power_impact_usd=100,
        pending_buying_power_impact_usd=300,
    )
    resolved = resolve_adaptive_risk(_policy(), inputs)
    packet = resolved.to_decision_packet()

    assert resolved.valid
    json.dumps(packet, allow_nan=False)
    assert packet["schema_version"] == "chili.adaptive-risk-decision.v2"
    assert packet["input_snapshot"][
        "existing_same_symbol_structural_risk_usd"
    ] == 10
    assert packet["input_snapshot"][
        "pending_same_symbol_structural_risk_usd"
    ] == 20
    assert packet["input_snapshot"][
        "pending_correlation_cluster_risk_usd"
    ] == 20
    assert packet["input_snapshot"][
        "pending_portfolio_gross_notional_usd"
    ] == 300
    assert packet["input_snapshot"][
        "policy_buying_power_capacity_usd"
    ] == 400_000
    assert packet["input_snapshot"][
        "pending_buying_power_impact_usd"
    ] == 300
    assert packet["input_snapshot"]["evidence"]["portfolio_heat"][
        "source"
    ] == "recorded_fixture"
    assert packet["input_snapshot"]["evidence"]["correlation"][
        "provider_generation"
    ] == "fixture-generation-7"
    assert resolved.input_sha256 != resolve_adaptive_risk(
        _policy(), _inputs()
    ).input_sha256
    assert (
        load_and_verify_adaptive_risk_decision_packet(packet).to_decision_packet()
        == packet
    )

    tampered = copy.deepcopy(packet)
    tampered["input_snapshot"]["pending_portfolio_gross_notional_usd"] += 1
    with pytest.raises(AdaptiveRiskContractError, match="canonical recomputation"):
        load_and_verify_adaptive_risk_decision_packet(tampered)

    missing = copy.deepcopy(packet)
    missing["input_snapshot"].pop("pending_correlation_cluster_risk_usd")
    with pytest.raises(AdaptiveRiskContractError, match="input snapshot is invalid"):
        load_and_verify_adaptive_risk_decision_packet(missing)


@pytest.mark.parametrize(
    "field",
    (
        "existing_same_symbol_structural_risk_usd",
        "pending_same_symbol_structural_risk_usd",
        "pending_correlation_cluster_risk_usd",
        "pending_portfolio_gross_notional_usd",
        "policy_buying_power_capacity_usd",
        "open_buying_power_impact_usd",
        "pending_buying_power_impact_usd",
    ),
)
@pytest.mark.parametrize("invalid", (-1.0, float("nan"), float("inf")))
def test_reservation_inputs_require_finite_nonnegative_values(
    field: str,
    invalid: float,
) -> None:
    with pytest.raises(AdaptiveRiskContractError, match="finite and non-negative"):
        _inputs(**{field: invalid})


@pytest.mark.parametrize("invalid", (-1.0, 0.0, float("nan"), float("inf")))
def test_candidate_buying_power_impact_requires_finite_positive_value(
    invalid: float,
) -> None:
    with pytest.raises(AdaptiveRiskContractError, match="finite and positive"):
        _inputs(candidate_buying_power_impact_per_share_usd=invalid)


def test_strict_packet_loader_recomputes_every_field_and_hash() -> None:
    resolved = resolve_adaptive_risk(_policy(), _inputs())
    loaded = load_and_verify_adaptive_risk_decision_packet(
        resolved.to_decision_packet()
    )

    assert loaded.to_decision_packet() == resolved.to_decision_packet()


def test_strict_packet_loader_rejects_derived_or_input_tampering() -> None:
    packet = resolve_adaptive_risk(_policy(), _inputs()).to_decision_packet()
    derived_tamper = copy.deepcopy(packet)
    derived_tamper["quantity_shares"] += 1
    input_tamper = copy.deepcopy(packet)
    input_tamper["input_snapshot"]["buying_power_usd"] *= 2

    with pytest.raises(
        AdaptiveRiskContractError, match="canonical recomputation"
    ):
        load_and_verify_adaptive_risk_decision_packet(derived_tamper)
    with pytest.raises(
        AdaptiveRiskContractError, match="canonical recomputation"
    ):
        load_and_verify_adaptive_risk_decision_packet(input_tamper)


def test_grid_never_exceeds_any_resolved_risk_or_notional_budget() -> None:
    for equity in (40_000.0, 100_000.0, 250_000.0):
        for quality in (0.2, 0.7, 1.0):
            for volatility in (0.03, 0.08, 0.16):
                resolved = resolve_adaptive_risk(
                    _policy(),
                    _inputs(
                        equity_usd=equity,
                        buying_power_usd=equity * 4,
                        setup_quality=quality,
                        realized_volatility_fraction=volatility,
                    ),
                )
                assert resolved.valid
                assert resolved.planned_structural_risk_usd <= min(
                    resolved.risk_budget_caps_usd.values()
                ) + 1e-8
                assert resolved.planned_notional_usd <= min(
                    resolved.notional_caps_usd.values()
                ) + 1e-8
                assert resolved.planned_buying_power_impact_usd <= min(
                    resolved.buying_power_caps_usd.values()
                ) + 1e-8


def test_equity_scaling_is_monotonic_without_dollar_ceiling() -> None:
    resolved = [
        resolve_adaptive_risk(
            _policy(),
            _inputs(equity_usd=equity, buying_power_usd=equity * 4),
        )
        for equity in (40_000.0, 100_000.0, 250_000.0)
    ]
    assert all(row.valid for row in resolved)
    assert [row.base_r_usd for row in resolved] == [400.0, 1_000.0, 2_500.0]
    assert [row.quantity_shares for row in resolved] == sorted(
        row.quantity_shares for row in resolved
    )
    assert resolved[-1].planned_structural_risk_usd > resolved[0].planned_structural_risk_usd


def test_adaptive_resolver_contains_no_activation_only_dollar_literals() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "trading"
        / "momentum_neural"
        / "adaptive_risk_policy.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    numeric_literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    }
    assert 50 not in numeric_literals
    assert 250 not in numeric_literals
