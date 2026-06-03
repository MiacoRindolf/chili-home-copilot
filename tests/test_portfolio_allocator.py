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
    confidence_input = proposal.allocation_decision_json["score_inputs"][
        "confidence_input"
    ]
    assert confidence_input["source_surface"] == "portfolio_allocator.proposal_confidence"
    assert confidence_input["raw_value"] == 4.0
    assert confidence_input["accepted_scale"] == "decile_1_10"
    assert confidence_input["normalized_probability"] == 0.4
    assert confidence_input["parser_outcome"] == "accepted"


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


def test_allocator_session_notional_uses_option_contract_multiplier():
    session = SimpleNamespace(
        execution_family="robinhood_options",
        venue="robinhood",
        risk_snapshot_json={
            "momentum_live_execution": {
                "position": {"quantity": 2.0, "entry_price": 1.25},
            },
        },
    )

    assert allocator_mod._session_position_notional_usd(session) == 250.0


def test_allocator_session_option_notional_keeps_direct_usd_value():
    session = SimpleNamespace(
        execution_family="robinhood_options",
        venue="robinhood",
        risk_snapshot_json={
            "momentum_live_execution": {
                "position": {
                    "quantity": 2.0,
                    "entry_price": 1.25,
                    "notional_usd": 260.0,
                },
            },
        },
    )

    assert allocator_mod._session_position_notional_usd(session) == 260.0


def test_allocator_session_notional_uses_snapshot_option_multiplier():
    session = SimpleNamespace(
        execution_family="robinhood_equity",
        venue="robinhood",
        risk_snapshot_json={
            "momentum_live_execution": {
                "asset_class": "options",
                "position": {
                    "quantity": 3.0,
                    "avg_fill_price": 0.75,
                    "contract_multiplier": 100.0,
                },
            },
        },
    )

    assert allocator_mod._session_position_notional_usd(session) == 225.0


def test_allocator_safe_float_rejects_bool_and_nonfinite_values():
    assert allocator_mod._safe_float(True, 7.0) == 7.0
    assert allocator_mod._safe_float("NaN", 7.0) == 7.0
    assert allocator_mod._safe_float("1e9999", 7.0) == 7.0


def test_allocator_normalizes_confidence_scales_without_fake_certainty():
    assert allocator_mod._normalize_confidence(True) == 0.0
    assert allocator_mod._normalize_confidence(-0.1) == 0.0
    assert allocator_mod._normalize_confidence(0.72) == 0.72
    assert allocator_mod._normalize_confidence(4.0) == 0.4
    assert allocator_mod._normalize_confidence(75.0) == 0.75
    assert allocator_mod._normalize_confidence(95.0) == 0.95
    assert allocator_mod._normalize_confidence(101.0) == 0.0


def test_allocator_confidence_evidence_records_scale_contracts():
    score, evidence = allocator_mod._normalize_confidence_evidence(
        4.0,
        source_surface="unit.allocator",
    )
    assert score == 0.4
    assert evidence == {
        "source_surface": "unit.allocator",
        "parser": "portfolio_allocator._normalize_confidence",
        "raw_value": 4.0,
        "accepted_scale": "decile_1_10",
        "normalized_probability": 0.4,
        "parser_outcome": "accepted",
        "rejection_reason": None,
    }

    score, evidence = allocator_mod._normalize_confidence_evidence(75.0)
    assert score == 0.75
    assert evidence["accepted_scale"] == "percent_0_100"
    assert evidence["normalized_probability"] == 0.75


def test_allocator_confidence_evidence_rejects_fake_certainty():
    score, evidence = allocator_mod._normalize_confidence_evidence(True)
    assert score == 0.0
    assert evidence["parser_outcome"] == "rejected"
    assert evidence["rejection_reason"] == "boolean_confidence"

    score, evidence = allocator_mod._normalize_confidence_evidence(101.0)
    assert score == 0.0
    assert evidence["parser_outcome"] == "rejected"
    assert evidence["rejection_reason"] == "above_percent_ceiling"


def test_allocator_win_rate_score_preserves_zero_oos_evidence():
    pattern = SimpleNamespace(oos_win_rate=0.0, win_rate=0.9)

    assert allocator_mod._pattern_win_rate_score(pattern) == 0.0


def test_pattern_allocator_research_quality_respects_zero_oos_win_rate(monkeypatch):
    monkeypatch.setattr(
        allocator_mod,
        "get_all_broker_statuses",
        lambda: {"robinhood": {"connected": True}, "coinbase": {"connected": True}},
    )
    pattern = SimpleNamespace(
        name="zero oos allocator pattern",
        scope_tickers="AAPL",
        timeframe="1d",
        asset_class="stock",
        hypothesis_family="breakout",
        confidence=1.0,
        oos_win_rate=0.0,
        win_rate=1.0,
        oos_validation_json={},
    )

    state = allocator_mod.build_pattern_allocation_state(
        _FakeAllocatorDb(),
        pattern,
        user_id=1,
        context="unit_test",
    )

    assert state["score_inputs"]["research_quality"] == 0.55
    assert pattern.oos_validation_json["allocation_state"]["score_inputs"][
        "research_quality"
    ] == 0.55


def test_proposal_allocator_records_confidence_evidence_without_db_fixture():
    proposal = SimpleNamespace(
        scan_pattern_id=None,
        confidence=75.0,
        risk_reward_ratio=2.0,
        entry_price=1.25,
        quantity=2.0,
        ticker="SPY",
        timeframe="swing",
        allocation_decision_json=None,
    )

    decision = build_proposal_allocation_decision(
        _FakeAllocatorDb(),
        proposal,
        user_id=1,
    )

    confidence_input = decision["score_inputs"]["confidence_input"]
    assert confidence_input["source_surface"] == "portfolio_allocator.proposal_confidence"
    assert confidence_input["raw_value"] == 75.0
    assert confidence_input["accepted_scale"] == "percent_0_100"
    assert confidence_input["normalized_probability"] == 0.75
    assert confidence_input["parser_outcome"] == "accepted"
    assert proposal.allocation_decision_json["score_inputs"]["confidence_input"] == confidence_input


def test_allocator_session_notional_ignores_false_option_path_marker():
    session = SimpleNamespace(
        execution_family="robinhood_equity",
        venue="robinhood",
        risk_snapshot_json={
            "momentum_live_execution": {
                "options_path": "false",
                "position": {"quantity": 2.0, "entry_price": 1.25},
            },
        },
    )

    assert allocator_mod._session_position_notional_usd(session) == 2.5


def test_allocator_option_bucket_helpers_use_underlying_family():
    family = allocator_mod._candidate_asset_family("PHHOPT_NEW", "options")

    assert family == "equity"
    assert (
        allocator_mod._correlation_bucket("PHHOPT_NEW", asset_class="options")
        == "equity:P"
    )
    assert (
        allocator_mod._correlation_bucket("PHHOPT_NEW", asset_class=family)
        == "equity:P"
    )


class _FakeAllocatorQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self._rows)

    def count(self):
        return len(self._rows)


class _FakeAllocatorDb:
    def __init__(self, *, trades=None, sessions=None, variants=None):
        self._trades = list(trades or [])
        self._sessions = list(sessions or [])
        self._variants = list(variants or [])

    def query(self, model):
        name = getattr(model, "__name__", "")
        if name == "Trade":
            return _FakeAllocatorQuery(self._trades)
        if name == "TradingAutomationSession":
            return _FakeAllocatorQuery(self._sessions)
        if name == "MomentumStrategyVariant":
            return _FakeAllocatorQuery(self._variants)
        return _FakeAllocatorQuery([])


def test_allocator_option_candidate_uses_underlying_correlation_bucket(monkeypatch):
    db = _FakeAllocatorDb(
        trades=[
            SimpleNamespace(
                id=1,
                user_id=1,
                scan_pattern_id=None,
                ticker="PHHOPT_INC",
                direction="long",
                entry_price=100.0,
                quantity=1.0,
                status="open",
                broker_source="robinhood",
            )
        ]
    )

    monkeypatch.setattr(
        "app.services.trading.portfolio_allocator.settings.brain_max_correlated_positions",
        1,
    )
    monkeypatch.setattr(
        "app.services.trading.portfolio_allocator.settings.brain_max_open_per_sector",
        0,
    )
    decision = evaluate_allocation_candidate(
        db,
        user_id=1,
        symbol="PHHOPT_NEW",
        timeframe="swing",
        asset_class="options",
        hypothesis_family="option_breakout",
        research_quality=0.75,
        live_drift_contract={"composite_tier": "healthy"},
        execution_contract={"robustness_tier": "healthy"},
        context="option_entry",
    )

    assert decision["allowed_if_enforced"] is False
    assert decision["blocked_reason"] == "correlation_bucket_cap"
    assert decision["asset_class"] == "equity"
    assert decision["correlation_bucket"] == "equity:P"
    assert "same_correlation_bucket" in decision["conflict_buckets"]
