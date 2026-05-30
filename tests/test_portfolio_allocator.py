from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.config import Settings
from app.models import MomentumStrategyVariant, StrategyProposal, Trade, TradingAutomationSession, User
from app.models.trading import ScanPattern
from app.services.trading import portfolio_allocator as allocator_mod
from app.services.trading.portfolio_allocator import (
    allocation_block_reason,
    build_proposal_allocation_decision,
    evaluate_allocation_candidate,
)


def test_proposal_allocator_blocks_same_ticker_conflict(db):
    user = User(name="Allocator User")
    db.add(user)
    db.flush()
    db.add(
        Trade(
            user_id=user.id,
            ticker="AAPL",
            direction="long",
            entry_price=100.0,
            quantity=5.0,
            status="open",
            broker_source="robinhood",
        )
    )
    proposal = StrategyProposal(
        user_id=user.id,
        ticker="AAPL",
        direction="long",
        entry_price=101.0,
        stop_loss=97.0,
        take_profit=107.0,
        quantity=2.0,
        risk_reward_ratio=1.2,
        confidence=4.0,
        timeframe="swing",
        thesis="Duplicate same-symbol attempt",
    )
    db.add(proposal)
    db.commit()

    decision = build_proposal_allocation_decision(db, proposal, user_id=user.id)
    assert decision["allowed_if_enforced"] is False
    assert decision["blocked_reason"] == "same_ticker_conflict"
    assert proposal.allocation_decision_json["blocked_reason"] == "same_ticker_conflict"


def test_allocator_uses_sector_cap(db, monkeypatch):
    user = User(name="Sector Cap User")
    db.add(user)
    db.flush()
    db.add(
        Trade(
            user_id=user.id,
            ticker="AAPL",
            direction="long",
            entry_price=100.0,
            quantity=5.0,
            status="open",
            broker_source="robinhood",
        )
    )
    db.commit()

    monkeypatch.setattr("app.services.trading.portfolio_allocator.settings.brain_max_open_per_sector", 1)
    decision = evaluate_allocation_candidate(
        db,
        user_id=user.id,
        symbol="MSFT",
        timeframe="swing",
        asset_class=None,
        hypothesis_family="trend",
        research_quality=0.75,
        live_drift_contract={"composite_tier": "healthy"},
        execution_contract={"robustness_tier": "healthy"},
        context="proposal_approval",
    )
    assert decision["allowed_if_enforced"] is False
    assert decision["blocked_reason"] == "sector_cap"


def test_allocator_defaults_keep_live_hard_blocks_shadowed():
    assert Settings.model_fields["brain_allocator_shadow_mode"].default is True
    assert Settings.model_fields["brain_allocator_live_hard_block_enabled"].default is False
    assert (
        Settings.model_fields["chili_pilot_promoted_allow_bootstrap_recert_live"].default
        is False
    )


def test_pattern_capital_gate_blocks_pilot_recert_debt_even_if_legacy_flag_enabled(
    db,
    monkeypatch,
):
    pattern = ScanPattern(
        name="pilot recert stays observation only",
        rules_json={},
        active=True,
        lifecycle_stage="pilot_promoted",
        promotion_status="pilot_collecting_ev",
        recert_required=True,
        recert_reason="missing_oos_recert,missing_quality_composite_score,thin_realized_ev",
    )
    db.add(pattern)
    db.commit()

    monkeypatch.setattr(
        allocator_mod.settings,
        "chili_autotrader_block_live_on_recert_required",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        allocator_mod.settings,
        "chili_pilot_promoted_allow_bootstrap_recert_live",
        True,
        raising=False,
    )

    decision = allocator_mod._pattern_capital_gate(
        db,
        scan_pattern_id=int(pattern.id),
        execution_mode="live",
    )

    assert decision["status"] == "block"
    assert decision["hard_block_reason"] == "pattern_recert_required"


def test_allocator_block_reason_requires_authoritative_flag(monkeypatch):
    decision = {"allowed_if_enforced": False, "blocked_reason": "same_ticker_conflict"}
    monkeypatch.setattr("app.services.trading.portfolio_allocator.settings.brain_allocator_live_hard_block_enabled", False)
    assert allocation_block_reason(decision) is None
    monkeypatch.setattr("app.services.trading.portfolio_allocator.settings.brain_allocator_live_hard_block_enabled", True)
    assert allocation_block_reason(decision) == "same_ticker_conflict"


def test_allocator_family_cap_only_matches_same_family(db, monkeypatch):
    user = User(name=f"Allocator Family User {uuid.uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    incumbent = MomentumStrategyVariant(
        family="mean_reversion",
        variant_key=f"alloc_inc_{uuid.uuid4().hex[:10]}",
        label="incumbent",
        params_json={},
    )
    db.add(incumbent)
    db.flush()
    db.add(
        TradingAutomationSession(
            user_id=user.id,
            venue="coinbase",
            execution_family="coinbase_spot",
            mode="live",
            symbol="ETH-USD",
            variant_id=int(incumbent.id),
            state="live_entered",
            risk_snapshot_json={"momentum_live_execution": {"position": {"notional_usd": 25.0}}},
        )
    )
    db.commit()

    monkeypatch.setattr(
        "app.services.trading.portfolio_allocator.settings.brain_allocator_max_same_family_live_sessions",
        1,
    )
    different = evaluate_allocation_candidate(
        db,
        user_id=user.id,
        symbol="BTC-USD",
        timeframe="scalp",
        asset_class="crypto",
        hypothesis_family="momentum_scalp",
        research_quality=0.75,
        live_drift_contract={"composite_tier": "healthy"},
        execution_contract={"robustness_tier": "healthy"},
        context="momentum_entry",
        execution_mode="live",
        intended_notional_usd=10.0,
    )
    assert different["allowed_if_enforced"] is True
    assert "same_hypothesis_family" not in different["conflict_buckets"]

    same = evaluate_allocation_candidate(
        db,
        user_id=user.id,
        symbol="BTC-USD",
        timeframe="scalp",
        asset_class="crypto",
        hypothesis_family="mean_reversion",
        research_quality=0.75,
        live_drift_contract={"composite_tier": "healthy"},
        execution_contract={"robustness_tier": "healthy"},
        context="momentum_entry",
        execution_mode="live",
        intended_notional_usd=10.0,
    )
    assert same["allowed_if_enforced"] is False
    assert same["blocked_reason"] == "strategy_family_live_cap"
    assert same["portfolio_exposure"]["same_hypothesis_family_live_sessions"] == 1


def test_allocator_live_notional_cap_uses_projected_exposure(db, monkeypatch):
    user = User(name=f"Allocator Notional User {uuid.uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    variant = MomentumStrategyVariant(
        family="momentum_scalp",
        variant_key=f"alloc_notional_{uuid.uuid4().hex[:10]}",
        label="notional incumbent",
        params_json={},
    )
    db.add(variant)
    db.flush()
    db.add(
        TradingAutomationSession(
            user_id=user.id,
            venue="coinbase",
            execution_family="coinbase_spot",
            mode="live",
            symbol="ETH-USD",
            variant_id=int(variant.id),
            state="live_entered",
            risk_snapshot_json={
                "momentum_live_execution": {"position": {"quantity": 1.0, "entry_price": 90.0}}
            },
        )
    )
    db.commit()

    monkeypatch.setattr(
        "app.services.trading.portfolio_allocator.settings.brain_allocator_max_live_notional_usd",
        100.0,
    )
    decision = evaluate_allocation_candidate(
        db,
        user_id=user.id,
        symbol="BTC-USD",
        timeframe="scalp",
        asset_class="crypto",
        hypothesis_family="breakout",
        research_quality=0.75,
        live_drift_contract={"composite_tier": "healthy"},
        execution_contract={"robustness_tier": "healthy"},
        context="momentum_entry",
        execution_mode="live",
        intended_notional_usd=20.0,
    )
    assert decision["allowed_if_enforced"] is False
    assert decision["blocked_reason"] == "portfolio_live_notional_cap"
    assert decision["portfolio_exposure"]["projected_live_notional_usd"] == 110.0


def test_allocator_open_trade_notional_uses_option_contract_multiplier():
    from app.services.trading.portfolio_allocator import _trade_notional_usd

    notional = _trade_notional_usd(
        SimpleNamespace(
            ticker="SPY",
            entry_price=1.25,
            quantity=2.0,
            asset_kind="option",
            indicator_snapshot={"option_meta": {"strike": 729.0}},
        )
    )

    assert notional == 250.0
