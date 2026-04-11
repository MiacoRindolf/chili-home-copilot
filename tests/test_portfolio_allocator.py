from __future__ import annotations

from types import SimpleNamespace

from app.models import StrategyProposal, Trade, User
from app.services.trading.portfolio_allocator import (
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
