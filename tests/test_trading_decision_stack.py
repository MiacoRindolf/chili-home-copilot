from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.config import Settings, settings
from app.models import (
    AlertHistory,
    MomentumStrategyVariant,
    MomentumSymbolViability,
    StrategyProposal,
    TradingAutomationEvent,
    TradingAutomationSession,
    TradingAutomationSimulatedFill,
    TradingDecisionCandidate,
    TradingDecisionPacket,
    TradingDeploymentState,
    Trade,
    User,
)
from app.services.trading.capacity_governor import evaluate_capacity
from app.services.trading.decision_ledger import (
    attach_shadow_signal_packets,
    mark_linked_trade_packets_executed,
    mark_linked_trade_packets_terminal,
    run_momentum_entry_decision,
    seal_decision_packet_snapshot,
    verify_decision_packet_snapshot,
)
from app.services.trading.deployment_ladder_service import (
    _promotion_readiness,
    evaluate_de_escalation,
    get_or_create_deployment_state,
    record_trade_outcome_metrics,
    sync_initial_stage_from_viability,
)
from app.services.trading.momentum_neural import paper_runner
from app.services.trading.momentum_neural.live_fsm import STATE_LIVE_PENDING_ENTRY
from app.services.trading.momentum_neural.live_runner import tick_live_session
from app.services.trading.momentum_neural.outcome_extract import extract_momentum_session_outcome, outcome_row_from_extracted
from app.services.trading.momentum_neural.paper_runner import tick_paper_session
from app.services.trading.portfolio_allocator import allocate_momentum_session_entry
from app.services.trading.venue.protocol import FreshnessMeta, NormalizedProduct, NormalizedTicker


@pytest.fixture
def momentum_user_and_session(db):
    user = User(name=f"Decision Stack User {uuid.uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    var = MomentumStrategyVariant(
        family="momentum_scalp",
        variant_key=f"ds_paper_{uuid.uuid4().hex[:12]}",
        label="test_var",
        params_json={"entry_viability_min": 0.5, "entry_revalidate_floor": 0.3},
    )
    db.add(var)
    db.flush()
    sess = TradingAutomationSession(
        user_id=user.id,
        venue="coinbase",
        execution_family="coinbase_spot",
        mode="paper",
        symbol="BTC-USD",
        variant_id=int(var.id),
        state="pending_entry",
        risk_snapshot_json={"momentum_risk": {"admitted": True}, "confidence": 0.7, "viability_score": 0.8},
    )
    db.add(sess)
    db.flush()
    via = MomentumSymbolViability(
        symbol="BTC-USD",
        variant_id=int(var.id),
        viability_score=0.85,
        paper_eligible=True,
        live_eligible=False,
        execution_readiness_json={"spread_bps": 8.0, "slippage_estimate_bps": 6.0, "fee_to_target_ratio": 0.08},
        regime_snapshot_json={"atr_pct": 2.0},
        evidence_window_json={"volume_usd_24h": 50_000_000},
        explain_json={},
    )
    db.add(via)
    db.commit()
    return user, sess, via, var


@pytest.fixture
def momentum_user_and_live_session(db):
    user = User(name=f"Decision Stack Live User {uuid.uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    var = MomentumStrategyVariant(
        family="momentum_scalp",
        variant_key=f"ds_live_{uuid.uuid4().hex[:12]}",
        label="test_var_live",
        params_json={"entry_viability_min": 0.5, "entry_revalidate_floor": 0.3},
    )
    db.add(var)
    db.flush()
    sess = TradingAutomationSession(
        user_id=user.id,
        venue="coinbase",
        execution_family="coinbase_spot",
        mode="live",
        symbol="BTC-USD",
        variant_id=int(var.id),
        state=STATE_LIVE_PENDING_ENTRY,
        risk_snapshot_json={
            "momentum_risk": {"admitted": True},
            "momentum_live_execution": {},
            "confidence": 0.7,
            "viability_score": 0.8,
        },
    )
    db.add(sess)
    db.flush()
    via = MomentumSymbolViability(
        symbol="BTC-USD",
        variant_id=int(var.id),
        viability_score=0.85,
        paper_eligible=True,
        live_eligible=True,
        execution_readiness_json={"spread_bps": 8.0, "slippage_estimate_bps": 6.0, "fee_to_target_ratio": 0.08},
        regime_snapshot_json={"atr_pct": 2.0},
        evidence_window_json={"volume_usd_24h": 50_000_000},
        explain_json={},
    )
    db.add(via)
    db.commit()
    return user, sess, via, var


def test_allocate_momentum_positive_net_proceeds(db, momentum_user_and_session, monkeypatch):
    monkeypatch.setattr(settings, "brain_enforce_net_expectancy_paper", False)
    monkeypatch.setattr(settings, "brain_expectancy_allocator_shadow_mode", True)
    monkeypatch.setattr(settings, "brain_capacity_hard_block_paper", False)
    monkeypatch.setattr(settings, "brain_paper_deployment_enforcement", False)
    user, sess, via, var = momentum_user_and_session
    out = allocate_momentum_session_entry(
        db,
        session=sess,
        viability=via,
        variant=var,
        user_id=user.id,
        max_notional_policy=250.0,
        quote_mid=100.0,
        spread_bps=8.0,
        execution_mode="paper",
        regime_snapshot=via.regime_snapshot_json,
        deployment_stage="paper",
    )
    assert out["proceed"] is True
    assert out["recommended_notional"] > 0


def test_allocate_momentum_abstains_negative_expectancy_when_enforced(db, momentum_user_and_session, monkeypatch):
    monkeypatch.setattr(settings, "brain_enforce_net_expectancy_paper", True)
    monkeypatch.setattr(settings, "brain_expectancy_allocator_shadow_mode", False)
    monkeypatch.setattr(settings, "brain_minimum_net_expectancy_to_trade", 0.99)
    monkeypatch.setattr(settings, "brain_capacity_hard_block_paper", False)
    monkeypatch.setattr(settings, "brain_paper_deployment_enforcement", False)
    user, sess, via, var = momentum_user_and_session
    via.viability_score = 0.01
    db.commit()
    out = allocate_momentum_session_entry(
        db,
        session=sess,
        viability=via,
        variant=var,
        user_id=user.id,
        max_notional_policy=250.0,
        quote_mid=100.0,
        spread_bps=8.0,
        execution_mode="paper",
        regime_snapshot=via.regime_snapshot_json,
        deployment_stage="paper",
    )
    assert out["proceed"] is False
    assert out["abstain_reason_code"] == "negative_net_expectancy"


def test_authoritative_net_edge_can_drive_abstain_without_live_defaults(db, momentum_user_and_session, monkeypatch):
    monkeypatch.setattr(settings, "brain_enforce_net_expectancy_paper", False)
    monkeypatch.setattr(settings, "brain_expectancy_allocator_shadow_mode", True)
    monkeypatch.setattr(settings, "brain_capacity_hard_block_paper", False)
    monkeypatch.setattr(settings, "brain_paper_deployment_enforcement", False)

    from app.services.trading import net_edge_ranker

    monkeypatch.setattr(net_edge_ranker, "mode_is_active", lambda: True)
    monkeypatch.setattr(net_edge_ranker, "mode_is_authoritative", lambda: True)
    monkeypatch.setattr(
        net_edge_ranker,
        "score",
        lambda *_a, **_k: SimpleNamespace(expected_net_pnl=-0.01, decision_id="ne_authority_1"),
    )

    user, sess, via, var = momentum_user_and_session
    out = allocate_momentum_session_entry(
        db,
        session=sess,
        viability=via,
        variant=var,
        user_id=user.id,
        max_notional_policy=250.0,
        quote_mid=100.0,
        spread_bps=8.0,
        execution_mode="paper",
        regime_snapshot=via.regime_snapshot_json,
        deployment_stage="paper",
    )
    assert out["proceed"] is False
    assert out["abstain_reason_code"] == "negative_net_expectancy"
    assert out["net_edge_authoritative"] is True
    assert out["expected_edge_net"] == -0.01


def test_capacity_governor_blocks_when_enforced(db, monkeypatch):
    monkeypatch.setattr(settings, "brain_enable_capacity_governor", True)
    monkeypatch.setattr(settings, "brain_capacity_hard_block_paper", True)
    monkeypatch.setattr(settings, "chili_momentum_risk_max_spread_bps_paper", 5.0)
    out = evaluate_capacity(
        db,
        user_id=1,
        symbol="BTC-USD",
        spread_bps=50.0,
        estimated_slippage_bps=10.0,
        intended_notional_usd=100.0,
        execution_mode="paper",
        adv_usd_proxy=1_000_000.0,
        min_volume_usd_proxy=None,
    )
    assert out["capacity_hard_signals"] is True
    assert out["capacity_blocked"] is True


def test_attach_shadow_signal_packets_reuses_recent_board_packet(db, momentum_user_and_session, monkeypatch):
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", True)
    monkeypatch.setattr(settings, "brain_opportunity_board_decision_packets_enabled", True)
    user, _sess, _via, _var = momentum_user_and_session
    candidate = {
        "ticker": "BTC-USD",
        "tier": "A",
        "sources": ["pattern_imminent"],
        "source_strength": "strong",
        "entry": 100.0,
        "stop": 96.0,
        "target": 110.0,
        "core_edge_score": 0.7,
        "net_edge_estimate": {"expected_net_edge": 0.012},
        "execution_risk": {"score": 0.2, "expected_slippage_bps": 6.0, "expected_fill_probability": 0.9},
        "liquidity_quality": {"score": 0.8},
        "data_quality_gate": {
            "status": "block",
            "capital_lane_eligible": False,
            "learning_lane_enabled": True,
            "hard_block_reason_code": "board_data_stale",
        },
        "capital_lane": {
            "board_data_quality_passed": False,
            "requires_runner_decision_packet": True,
            "approved_for_direct_execution": False,
            "hard_block_reason_code": "board_data_stale",
        },
    }

    first = attach_shadow_signal_packets(
        db,
        user_id=user.id,
        candidates=[candidate],
        source_surface="opportunity_board",
        generated_at=datetime.utcnow(),
        data_as_of="2026-01-01T12:00:00Z",
        ttl_seconds=180,
    )
    assert first == {"created": 1, "reused": 0}
    first_packet_id = candidate["decision_packet_id"]
    pkt = db.get(TradingDecisionPacket, first_packet_id)
    snap = pkt.allocator_input_json["decision_snapshot"]
    candidate_set = pkt.allocator_input_json["candidate_set"]
    assert candidate_set["candidate_count"] == 1
    assert len(candidate_set["fingerprint_sha256"]) == 64
    assert candidate_set["rows"][0]["reject_reason_code"] == "data_quality_blocked"
    assert snap["snapshot_id"].startswith("tdp_")
    assert len(snap["fingerprint_sha256"]) == 64
    assert pkt.research_vs_live_context_json["decision_snapshot"]["snapshot_id"] == snap["snapshot_id"]
    assert candidate["decision_snapshot_id"] == snap["snapshot_id"]
    assert pkt.abstain_reason_code == "data_quality_learning_observation"
    assert pkt.portfolio_context_json["capital_approval"] == "blocked_data_quality"
    assert pkt.allocator_output_json["data_quality_gate"]["hard_block_reason_code"] == "board_data_stale"

    candidate2 = dict(candidate)
    candidate2.pop("decision_packet_id", None)
    second = attach_shadow_signal_packets(
        db,
        user_id=user.id,
        candidates=[candidate2],
        source_surface="opportunity_board",
        generated_at=datetime.utcnow(),
        data_as_of="2026-01-01T12:00:00Z",
        ttl_seconds=180,
    )
    assert second == {"created": 0, "reused": 1}
    assert candidate2["decision_packet_id"] == first_packet_id
    assert candidate2["decision_snapshot_id"] == snap["snapshot_id"]


def test_approved_strategy_proposal_records_linked_decision_packet(db, monkeypatch):
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", True)
    monkeypatch.setattr(settings, "brain_decision_packet_required_for_proposals", True)
    monkeypatch.setattr(settings, "brain_allocator_live_hard_block_enabled", False)
    monkeypatch.setattr("app.services.broker_manager.is_any_connected", lambda: False)
    monkeypatch.setattr("app.services.broker_manager.get_best_broker_for", lambda _ticker: "manual")

    import app.services.trading.alerts as alerts_mod

    monkeypatch.setattr(alerts_mod, "dispatch_alert", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(alerts_mod, "_get_buying_power", lambda: 10_000.0)

    user = User(name=f"Proposal Packet User {uuid.uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    proposal = StrategyProposal(
        user_id=user.id,
        ticker="AAPL",
        direction="long",
        status="pending",
        entry_price=100.0,
        stop_loss=96.0,
        take_profit=110.0,
        quantity=2.0,
        projected_profit_pct=10.0,
        projected_loss_pct=4.0,
        risk_reward_ratio=2.5,
        confidence=0.72,
        timeframe="swing",
        thesis="Packetized proposal execution",
        signals_json='[{"signal": "unit"}]',
    )
    db.add(proposal)
    db.commit()

    out = alerts_mod.approve_proposal(db, int(proposal.id), user.id, broker="manual")

    assert out["ok"] is True
    assert out["execution"]["status"] == "recorded"
    db.refresh(proposal)
    pkt_id = int((proposal.allocation_decision_json or {})["decision_packet_id"])
    assert out["execution"]["decision_packet_id"] == pkt_id
    pkt = db.get(TradingDecisionPacket, pkt_id)
    assert pkt is not None
    assert pkt.source_surface == "strategy_proposal"
    assert pkt.decision_type == "trade"
    assert pkt.chosen_ticker == "AAPL"
    assert pkt.linked_trade_id == int(proposal.trade_id)
    assert pkt.outcome_status == "executed"
    assert pkt.size_shares_or_qty == 2.0
    assert verify_decision_packet_snapshot(pkt)["ok"] is True


def test_failed_strategy_proposal_order_marks_decision_packet_terminal(db, monkeypatch):
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", True)
    monkeypatch.setattr(settings, "brain_decision_packet_required_for_proposals", True)
    monkeypatch.setattr(settings, "brain_allocator_live_hard_block_enabled", False)
    monkeypatch.setattr("app.services.broker_manager.is_any_connected", lambda: True)
    monkeypatch.setattr("app.services.broker_manager.get_best_broker_for", lambda _ticker: "robinhood")
    monkeypatch.setattr(
        "app.services.broker_manager.place_buy_order",
        lambda **_kwargs: {
            "ok": False,
            "broker": "robinhood",
            "order_id": "rejected-order-1",
            "error": "unit rejection",
        },
    )

    import app.services.trading.alerts as alerts_mod

    monkeypatch.setattr(alerts_mod, "dispatch_alert", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(alerts_mod, "_get_buying_power", lambda: 10_000.0)

    user = User(name=f"Proposal Fail Packet User {uuid.uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    proposal = StrategyProposal(
        user_id=user.id,
        ticker="AAPL",
        direction="long",
        status="pending",
        entry_price=100.0,
        stop_loss=96.0,
        take_profit=110.0,
        quantity=1.0,
        projected_profit_pct=10.0,
        projected_loss_pct=4.0,
        risk_reward_ratio=2.5,
        confidence=0.72,
        timeframe="swing",
        thesis="Packetized failed proposal execution",
        signals_json='[{"signal": "unit"}]',
    )
    db.add(proposal)
    db.commit()

    out = alerts_mod.approve_proposal(db, int(proposal.id), user.id, broker="robinhood")

    assert out["ok"] is True
    assert out["execution"]["status"] == "failed"
    pkt_id = int(out["execution"]["decision_packet_id"])
    pkt = db.get(TradingDecisionPacket, pkt_id)
    assert pkt is not None
    assert pkt.outcome_status == "failed"
    assert pkt.abstain_reason_code is None
    terminal_events = pkt.research_vs_live_context_json["terminal_events"]
    assert terminal_events[0]["reason_code"] == "broker_order_failed"
    assert terminal_events[0]["reason_text"] == "unit rejection"
    assert terminal_events[0]["order_id"] == "rejected-order-1"


def test_rejected_strategy_proposal_records_terminal_no_trade_packet(db, monkeypatch):
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", True)

    import app.services.trading.alerts as alerts_mod

    user = User(name=f"Proposal Reject Packet User {uuid.uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    proposal = StrategyProposal(
        user_id=user.id,
        ticker="AAPL",
        direction="long",
        status="pending",
        entry_price=100.0,
        stop_loss=96.0,
        take_profit=110.0,
        quantity=1.0,
        projected_profit_pct=10.0,
        projected_loss_pct=4.0,
        risk_reward_ratio=2.5,
        confidence=0.72,
        timeframe="swing",
        thesis="Packetized rejected proposal",
        signals_json='[{"signal": "unit"}]',
    )
    db.add(proposal)
    db.commit()

    out = alerts_mod.reject_proposal(db, int(proposal.id), user.id)

    assert out["ok"] is True
    assert out["decision_packet_id"] == out["proposal"]["decision_packet_id"]
    pkt = db.get(TradingDecisionPacket, int(out["decision_packet_id"]))
    assert pkt is not None
    assert pkt.decision_type == "abstain"
    assert pkt.deployment_stage == "proposal_rejected"
    assert pkt.outcome_status == "rejected"
    assert pkt.selected_candidate_rank is None
    assert pkt.abstain_reason_code == "operator_rejected_proposal"
    terminal_events = pkt.research_vs_live_context_json["terminal_events"]
    assert terminal_events[0]["surface"] == "proposal_reject"
    assert verify_decision_packet_snapshot(pkt)["ok"] is True


def test_recheck_price_drift_expiry_records_terminal_packet(db, monkeypatch):
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", True)
    monkeypatch.setattr("app.services.trading.market_data.fetch_quote", lambda _ticker: {"price": 150.0})

    import app.services.trading.alerts as alerts_mod

    user = User(name=f"Proposal Expire Packet User {uuid.uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    proposal = StrategyProposal(
        user_id=user.id,
        ticker="AAPL",
        direction="long",
        status="pending",
        entry_price=100.0,
        stop_loss=96.0,
        take_profit=110.0,
        quantity=1.0,
        projected_profit_pct=10.0,
        projected_loss_pct=4.0,
        risk_reward_ratio=2.5,
        confidence=0.72,
        timeframe="swing",
        thesis="Packetized expired proposal",
        signals_json='[{"signal": "unit"}]',
    )
    db.add(proposal)
    db.commit()

    out = alerts_mod.recheck_proposal(db, int(proposal.id), user.id, drift_expire_pct=30.0)

    assert out["ok"] is True
    assert out["expired"] is True
    assert out["decision_packet_id"] == out["proposal"]["decision_packet_id"]
    pkt = db.get(TradingDecisionPacket, int(out["decision_packet_id"]))
    assert pkt is not None
    assert pkt.decision_type == "abstain"
    assert pkt.deployment_stage == "proposal_expired"
    assert pkt.outcome_status == "expired"
    assert pkt.abstain_reason_code == "proposal_price_drift"
    terminal_events = pkt.research_vs_live_context_json["terminal_events"]
    assert terminal_events[0]["surface"] == "proposal_recheck"
    assert terminal_events[0]["drift_pct"] == 50.0


def test_dispatch_alert_records_shadow_decision_packet(db, monkeypatch):
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", True)
    monkeypatch.setattr(settings, "brain_alert_decision_packets_enabled", True)
    monkeypatch.setattr("app.services.sms_service.is_configured", lambda: False)

    import app.services.trading.alerts as alerts_mod

    monkeypatch.setattr(alerts_mod, "broadcast_alert_sync", lambda *_args, **_kwargs: None, raising=False)

    user = User(name=f"Alert Packet User {uuid.uuid4().hex[:8]}")
    db.add(user)
    db.commit()

    sent = alerts_mod.dispatch_alert(
        db,
        user_id=user.id,
        alert_type=alerts_mod.BREAKOUT_TRIGGERED,
        ticker="AAPL",
        message="Unit breakout alert",
        price=101.25,
        scan_pattern_id=None,
        confidence=0.61,
        skip_throttle=True,
        content_signature="unit-alert-digest",
    )

    assert sent is False
    row = (
        db.query(AlertHistory)
        .filter(AlertHistory.user_id == user.id, AlertHistory.alert_type == alerts_mod.BREAKOUT_TRIGGERED)
        .one()
    )
    assert row.content_signature == "unit-alert-digest"
    assert row.decision_packet_id is not None
    pkt = db.get(TradingDecisionPacket, int(row.decision_packet_id))
    assert pkt is not None
    assert pkt.source_surface == "alert_breakout_triggered"
    assert pkt.decision_type == "manual_signal"
    assert pkt.execution_mode == "shadow"
    assert pkt.outcome_status == "observed"
    assert pkt.shadow_advisory_only is True
    assert pkt.allocator_output_json["alert_context"]["alert_type"] == alerts_mod.BREAKOUT_TRIGGERED
    assert verify_decision_packet_snapshot(pkt)["ok"] is True


def test_dispatch_alert_can_link_existing_decision_packet(db, monkeypatch):
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", True)
    monkeypatch.setattr(settings, "brain_alert_decision_packets_enabled", True)
    monkeypatch.setattr("app.services.sms_service.is_configured", lambda: False)

    import app.services.trading.alerts as alerts_mod

    user = User(name=f"Alert Existing Packet User {uuid.uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    pkt = TradingDecisionPacket(
        user_id=user.id,
        chosen_ticker="AAPL",
        decision_type="trade",
        execution_mode="live",
        deployment_stage="proposal_approved",
        source_surface="strategy_proposal",
        outcome_status="pending",
        shadow_advisory_only=False,
    )
    db.add(pkt)
    db.flush()
    seal_decision_packet_snapshot(pkt)
    db.commit()

    alerts_mod.dispatch_alert(
        db,
        user_id=user.id,
        alert_type=alerts_mod.POSITION_OPENED,
        ticker="AAPL",
        message="Unit order placed",
        skip_throttle=True,
        decision_packet_id=int(pkt.id),
    )

    row = (
        db.query(AlertHistory)
        .filter(AlertHistory.user_id == user.id, AlertHistory.alert_type == alerts_mod.POSITION_OPENED)
        .one()
    )
    assert row.decision_packet_id == int(pkt.id)
    assert (
        db.query(TradingDecisionPacket)
        .filter(TradingDecisionPacket.source_surface == "alert_position_opened")
        .count()
        == 0
    )


def test_linked_trade_packet_marked_executed_on_broker_fill(db):
    user = User(name=f"Broker Fill Packet User {uuid.uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    proposal = StrategyProposal(
        user_id=user.id,
        ticker="AAPL",
        direction="long",
        status="executed",
        entry_price=100.0,
        stop_loss=96.0,
        take_profit=110.0,
        quantity=1.0,
        signals_json='[{"signal": "unit"}]',
    )
    db.add(proposal)
    db.flush()
    trade = Trade(
        user_id=user.id,
        ticker="AAPL",
        direction="long",
        entry_price=100.0,
        quantity=1.0,
        status="working",
        broker_source="robinhood",
        broker_order_id="unit-order-1",
        strategy_proposal_id=proposal.id,
    )
    db.add(trade)
    db.flush()
    packet = TradingDecisionPacket(
        user_id=user.id,
        chosen_ticker="AAPL",
        decision_type="trade",
        execution_mode="live",
        deployment_stage="proposal_approved",
        source_surface="strategy_proposal",
        linked_trade_id=trade.id,
        outcome_status="pending",
        shadow_advisory_only=False,
        research_vs_live_context_json={"execution_intents": [{"source": "unit"}]},
    )
    db.add(packet)
    db.commit()

    count = mark_linked_trade_packets_executed(
        db,
        trade_id=int(trade.id),
        source="unit_broker_sync",
        broker_order_id="unit-order-1",
    )

    assert count == 1
    db.refresh(packet)
    assert packet.outcome_status == "executed"
    confirmations = packet.research_vs_live_context_json["execution_fill_confirmations"]
    assert confirmations[0]["source"] == "unit_broker_sync"
    assert confirmations[0]["trade_id"] == int(trade.id)
    assert confirmations[0]["broker_order_id"] == "unit-order-1"
    assert (
        mark_linked_trade_packets_executed(
            db,
            trade_id=int(trade.id),
            source="unit_broker_sync",
            broker_order_id="unit-order-1",
        )
        == 0
    )


def test_linked_trade_packet_marked_terminal_on_broker_rejection(db):
    user = User(name=f"Broker Reject Packet User {uuid.uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    proposal = StrategyProposal(
        user_id=user.id,
        ticker="AAPL",
        direction="long",
        status="working",
        entry_price=100.0,
        stop_loss=96.0,
        take_profit=110.0,
        quantity=1.0,
        signals_json='[{"signal": "unit"}]',
    )
    db.add(proposal)
    db.flush()
    trade = Trade(
        user_id=user.id,
        ticker="AAPL",
        direction="long",
        entry_price=100.0,
        quantity=1.0,
        status="working",
        broker_source="coinbase",
        broker_order_id="unit-order-rejected",
        strategy_proposal_id=proposal.id,
    )
    db.add(trade)
    db.flush()
    packet = TradingDecisionPacket(
        user_id=user.id,
        chosen_ticker="AAPL",
        decision_type="trade",
        execution_mode="live",
        deployment_stage="proposal_approved",
        source_surface="strategy_proposal",
        linked_trade_id=trade.id,
        outcome_status="pending",
        shadow_advisory_only=False,
    )
    db.add(packet)
    db.commit()

    count = mark_linked_trade_packets_terminal(
        db,
        trade_id=int(trade.id),
        outcome_status="rejected",
        source="unit_broker_sync",
        reason_code="unit_broker_rejected",
        reason_text="unit terminal rejection",
        broker_order_id="unit-order-rejected",
    )

    assert count == 1
    db.refresh(packet)
    assert packet.outcome_status == "rejected"
    assert packet.abstain_reason_code is None
    terminal_events = packet.research_vs_live_context_json["terminal_events"]
    assert terminal_events[0]["source"] == "unit_broker_sync"
    assert terminal_events[0]["reason_code"] == "unit_broker_rejected"
    assert terminal_events[0]["reason_text"] == "unit terminal rejection"
    assert terminal_events[0]["trade_id"] == int(trade.id)
    assert terminal_events[0]["broker_order_id"] == "unit-order-rejected"
    assert (
        mark_linked_trade_packets_terminal(
            db,
            trade_id=int(trade.id),
            outcome_status="rejected",
            source="unit_broker_sync",
            reason_code="unit_broker_rejected",
            reason_text="unit terminal rejection",
            broker_order_id="unit-order-rejected",
        )
        == 0
    )


def test_deployment_deescalates_on_metrics(db, momentum_user_and_session):
    user, sess, via, var = momentum_user_and_session
    st = get_or_create_deployment_state(
        db, scope_type="automation_session", scope_key=f"session:{sess.id}", user_id=user.id
    )
    st.current_stage = "scaled"
    st.rolling_slippage_bps = 99.0
    db.commit()
    evaluate_de_escalation(db, st)
    db.refresh(st)
    assert st.current_stage == "limited"
    assert st.last_reason_code == "auto_deescalate"


def test_run_momentum_entry_decision_persists_packet(db, momentum_user_and_session, monkeypatch):
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", True)
    monkeypatch.setattr(settings, "brain_enforce_net_expectancy_paper", False)
    monkeypatch.setattr(settings, "brain_expectancy_allocator_shadow_mode", True)
    monkeypatch.setattr(settings, "brain_capacity_hard_block_paper", False)
    monkeypatch.setattr(settings, "brain_paper_deployment_enforcement", False)
    user, sess, via, var = momentum_user_and_session
    dec = run_momentum_entry_decision(
        db,
        session=sess,
        viability=via,
        variant=var,
        user_id=user.id,
        max_notional_policy=250.0,
        quote_mid=100.0,
        spread_bps=8.0,
        execution_mode="paper",
        regime_snapshot=via.regime_snapshot_json,
    )
    assert dec["packet_id"] is not None
    pkt = db.query(TradingDecisionPacket).filter(TradingDecisionPacket.id == int(dec["packet_id"])).one()
    assert pkt.automation_session_id == int(sess.id)
    assert pkt.user_id == user.id
    snap = pkt.allocator_input_json["decision_snapshot"]
    candidate_set = pkt.allocator_input_json["candidate_set"]
    assert candidate_set["candidate_count"] >= 1
    assert len(candidate_set["fingerprint_sha256"]) == 64
    assert candidate_set["rows"][0]["ticker"] == "BTC-USD"
    assert snap["snapshot_id"].startswith("tdp_")
    assert len(snap["fingerprint_sha256"]) == 64
    assert pkt.research_vs_live_context_json["decision_snapshot"]["fingerprint_sha256"] == snap["fingerprint_sha256"]
    cands = db.query(TradingDecisionCandidate).filter(TradingDecisionCandidate.decision_packet_id == pkt.id).all()
    assert len(cands) >= 1


def test_sync_promotes_after_paper_trades(db, momentum_user_and_session, monkeypatch):
    monkeypatch.setattr(settings, "brain_enable_deployment_ladder", True)
    monkeypatch.setattr(settings, "brain_deployment_promote_min_paper_trades", 1)
    user, sess, via, var = momentum_user_and_session
    st = get_or_create_deployment_state(
        db, scope_type="automation_session", scope_key=f"session:{sess.id}", user_id=user.id
    )
    st.current_stage = "paper"
    st.paper_trade_count = 5
    db.commit()
    sync_initial_stage_from_viability(
        db,
        session_id=int(sess.id),
        variant_id=int(var.id),
        user_id=user.id,
        paper_eligible=True,
        live_eligible=True,
        mode="live",
    )
    row = (
        db.query(TradingDeploymentState)
        .filter_by(scope_type="automation_session", scope_key=f"session:{sess.id}")
        .one()
    )
    db.refresh(row)
    assert row.current_stage == "limited"


def test_deployment_promotion_readiness_blocks_bad_known_metrics():
    st = TradingDeploymentState(
        scope_type="automation_session",
        scope_key="session:unit",
        current_stage="paper",
        paper_trade_count=5,
        rolling_expectancy_net=-1.0,
        rolling_drawdown_pct=0.0,
        rolling_slippage_bps=1.0,
        rolling_missed_fill_rate=0.0,
    )
    readiness = _promotion_readiness(st)
    assert readiness["ok"] is False
    assert readiness["reasons"] == ["negative_expectancy"]


def test_live_enforcement_flags_default_off():
    assert Settings.model_fields["brain_enforce_net_expectancy_live"].default is False
    assert Settings.model_fields["brain_capacity_hard_block_live"].default is False
    assert Settings.model_fields["brain_live_deployment_enforcement"].default is False


def test_decision_packet_snapshot_seal_is_stable_without_db():
    pkt = TradingDecisionPacket(
        id=42,
        created_at=datetime(2026, 1, 1, 12, 0, 0),
        user_id=1,
        automation_session_id=2,
        scan_pattern_id=3,
        chosen_ticker="BTC-USD",
        decision_type="trade",
        execution_mode="paper",
        deployment_stage="paper",
        regime_snapshot_json={"atr_pct": 2.0},
        allocator_input_json={"policy_notional_cap": 250.0},
        allocator_output_json={"expected": "truth"},
        portfolio_context_json={"capital_approval": "runner_packet"},
        expected_edge_gross=0.2,
        expected_edge_net=0.1,
        candidate_count=1,
        capacity_blocked=False,
        capacity_reason_json={},
        source_surface="autopilot",
        outcome_status="pending",
        shadow_advisory_only=False,
    )

    first = seal_decision_packet_snapshot(pkt, as_of_utc="2026-01-01T12:00:00Z")
    second = seal_decision_packet_snapshot(pkt, as_of_utc="2026-01-01T12:00:00Z")
    assert first["snapshot_id"].startswith("tdp_")
    assert first["fingerprint_sha256"] == second["fingerprint_sha256"]
    assert pkt.allocator_input_json["decision_snapshot"]["snapshot_id"] == first["snapshot_id"]
    assert pkt.research_vs_live_context_json["decision_snapshot"]["snapshot_id"] == first["snapshot_id"]
    assert verify_decision_packet_snapshot(pkt)["ok"] is True
    pkt.expected_edge_net = 0.2
    check = verify_decision_packet_snapshot(pkt)
    assert check["ok"] is False
    assert check["reason"] == "fingerprint_mismatch"


def test_outcome_summary_carries_entry_decision_packet_id():
    row = outcome_row_from_extracted(
        {
            "session_id": 1,
            "user_id": 2,
            "variant_id": 3,
            "variant_family": "momentum_scalp",
            "variant_key": "v1",
            "symbol": "BTC-USD",
            "mode": "paper",
            "execution_family": "coinbase_spot",
            "terminal_state": "finished",
            "terminal_at_utc": "2026-01-01T12:00:00",
            "outcome_class": "small_win",
            "realized_pnl_usd": 1.25,
            "return_bps": 12.0,
            "hold_seconds": 60,
            "exit_reason": "target",
            "entry_occurred": True,
            "entry_decision_packet_id": 123,
            "partial_exit_occurred": False,
            "regime_snapshot_json": {},
            "entry_regime_snapshot_json": {},
            "exit_regime_snapshot_json": {},
            "readiness_snapshot_json": {},
            "admission_snapshot_json": {},
            "governance_context_json": {},
        }
    )
    assert row.extracted_summary_json["entry_decision_packet_id"] == 123
    assert row.contributes_to_evolution is True
    assert row.extracted_summary_json["evolution_credit"]["contributes_to_evolution"] is True


def test_outcome_without_entry_packet_keeps_audit_but_no_evolution_credit():
    row = outcome_row_from_extracted(
        {
            "session_id": 2,
            "user_id": 2,
            "variant_id": 3,
            "variant_family": "momentum_scalp",
            "variant_key": "v1",
            "symbol": "BTC-USD",
            "mode": "paper",
            "execution_family": "coinbase_spot",
            "terminal_state": "finished",
            "terminal_at_utc": "2026-01-01T12:00:00",
            "outcome_class": "small_win",
            "realized_pnl_usd": 1.25,
            "return_bps": 12.0,
            "hold_seconds": 60,
            "exit_reason": "target",
            "entry_occurred": True,
            "entry_decision_packet_id": None,
            "partial_exit_occurred": False,
            "regime_snapshot_json": {},
            "entry_regime_snapshot_json": {},
            "exit_regime_snapshot_json": {},
            "readiness_snapshot_json": {},
            "admission_snapshot_json": {},
            "governance_context_json": {},
        }
    )
    assert row.contributes_to_evolution is False
    assert "missing_entry_decision_packet" in row.extracted_summary_json["evolution_credit"]["reason_codes"]


def test_governance_outcome_keeps_audit_but_no_evolution_credit():
    row = outcome_row_from_extracted(
        {
            "session_id": 3,
            "user_id": 2,
            "variant_id": 3,
            "variant_family": "momentum_scalp",
            "variant_key": "v1",
            "symbol": "BTC-USD",
            "mode": "live",
            "execution_family": "coinbase_spot",
            "terminal_state": "live_finished",
            "terminal_at_utc": "2026-01-01T12:00:00",
            "outcome_class": "governance_exit",
            "realized_pnl_usd": 1.25,
            "return_bps": 12.0,
            "hold_seconds": 60,
            "exit_reason": "kill_switch_flatten",
            "entry_occurred": True,
            "entry_decision_packet_id": 123,
            "partial_exit_occurred": False,
            "regime_snapshot_json": {},
            "entry_regime_snapshot_json": {},
            "exit_regime_snapshot_json": {},
            "readiness_snapshot_json": {},
            "admission_snapshot_json": {},
            "governance_context_json": {"kill_switch_exit": True},
        }
    )
    credit = row.extracted_summary_json["evolution_credit"]
    assert row.contributes_to_evolution is False
    assert "non_strategy_outcome_governance_exit" in credit["reason_codes"]
    assert credit["entry_decision_packet_id"] == 123
    from app.services.trading.momentum_neural.feedback_query import _outcome_brief

    brief = _outcome_brief(row)
    assert brief["contributes_to_evolution"] is False
    assert "non_strategy_outcome_governance_exit" in brief["evolution_credit_reason_codes"]


def test_synthetic_paper_quote_keeps_audit_but_no_evolution_credit():
    row = outcome_row_from_extracted(
        {
            "session_id": 4,
            "user_id": 2,
            "variant_id": 3,
            "variant_family": "momentum_scalp",
            "variant_key": "v1",
            "symbol": "BTC-USD",
            "mode": "paper",
            "execution_family": "coinbase_spot",
            "terminal_state": "finished",
            "terminal_at_utc": "2026-01-01T12:00:00",
            "outcome_class": "small_win",
            "realized_pnl_usd": 1.25,
            "return_bps": 12.0,
            "hold_seconds": 60,
            "exit_reason": "target",
            "entry_occurred": True,
            "entry_decision_packet_id": 123,
            "quote_source_at_entry": "synthetic_spread",
            "partial_exit_occurred": False,
            "regime_snapshot_json": {},
            "entry_regime_snapshot_json": {},
            "exit_regime_snapshot_json": {},
            "readiness_snapshot_json": {},
            "admission_snapshot_json": {},
            "governance_context_json": {},
        }
    )
    credit = row.extracted_summary_json["evolution_credit"]
    assert row.contributes_to_evolution is False
    assert "paper_synthetic_quote_source" in credit["reason_codes"]
    assert row.extracted_summary_json["quote_source_at_entry"] == "synthetic_spread"


def test_live_partial_exit_filled_event_sets_partial_outcome_flag(db, momentum_user_and_live_session):
    user, sess, _via, _var = momentum_user_and_live_session
    sess.state = "live_finished"
    sess.risk_snapshot_json = {
        "momentum_live_execution": {
            "realized_pnl_usd": 0.25,
            "last_exit_notional_basis_usd": 25.0,
            "last_exit_reason": "target",
            "entry_decision_packet_id": 123,
        }
    }
    ev = TradingAutomationEvent(
        session_id=int(sess.id),
        event_type="live_partial_exit_filled",
        payload_json={"reason": "target", "qty": 0.1},
    )
    db.add(ev)
    db.commit()

    out = extract_momentum_session_outcome(db, sess)

    assert out["entry_occurred"] is True
    assert out["partial_exit_occurred"] is True
    assert out["return_bps"] == pytest.approx(100.0)
    assert out["notional_basis_usd"] == pytest.approx(25.0)


def test_paper_closed_position_basis_feeds_return_bps(db, momentum_user_and_session):
    _user, sess, _via, _var = momentum_user_and_session
    sess.state = "finished"
    sess.risk_snapshot_json = {
        "momentum_paper_execution": {
            "realized_pnl_usd": 1.5,
            "last_exit_notional_basis_usd": 100.0,
            "last_exit_reason": "target",
            "last_entry_decision_packet_id": 321,
        }
    }
    ev = TradingAutomationEvent(
        session_id=int(sess.id),
        event_type="paper_entry_filled",
        payload_json={"entry_price": 100.0},
    )
    db.add(ev)
    db.commit()

    out = extract_momentum_session_outcome(db, sess)

    assert out["entry_occurred"] is True
    assert out["return_bps"] == pytest.approx(150.0)
    assert out["notional_basis_usd"] == pytest.approx(100.0)


def test_existing_feedback_reingest_recomputes_credit_without_packet(monkeypatch):
    import app.services.trading.momentum_neural.feedback_emit as feedback_emit

    monkeypatch.setattr(feedback_emit, "economic_ledger_active", lambda: False)
    row = SimpleNamespace(
        session_id=33,
        mode="paper",
        outcome_class="small_win",
        return_bps=12.0,
        realized_pnl_usd=1.25,
        contributes_to_evolution=True,
        extracted_summary_json={
            "entry_occurred": True,
            "entry_decision_packet_id": None,
            "evolution_credit": {"contributes_to_evolution": True},
        },
    )

    credit = feedback_emit._recompute_existing_row_credit(SimpleNamespace(), row)

    assert credit["contributes_to_evolution"] is False
    assert row.contributes_to_evolution is False
    assert "missing_entry_decision_packet" in row.extracted_summary_json["evolution_credit"]["reason_codes"]


def test_economic_ledger_active_missing_parity_blocks_evolution_credit(monkeypatch):
    import app.services.trading.momentum_neural.feedback_emit as feedback_emit

    monkeypatch.setattr(settings, "brain_economic_ledger_require_parity_for_evolution", True)
    monkeypatch.setattr(feedback_emit, "economic_ledger_active", lambda: True)

    class _Query:
        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def first(self):
            return None

    class _Db:
        def query(self, *_args, **_kwargs):
            return _Query()

    credit = feedback_emit._apply_economic_ledger_credit_gate(
        _Db(),
        session_id=44,
        credit={
            "contributes_to_evolution": True,
            "reason_codes": [],
            "entry_decision_packet_id": 123,
            "outcome_class": "small_win",
        },
    )

    assert credit["contributes_to_evolution"] is False
    assert credit["economic_ledger_verification"] == {"ok": None, "reason": "parity_missing"}
    assert "economic_ledger_parity_missing" in credit["reason_codes"]


def test_economic_ledger_matching_parity_keeps_evolution_credit(monkeypatch):
    import app.services.trading.momentum_neural.feedback_emit as feedback_emit

    monkeypatch.setattr(settings, "brain_economic_ledger_require_parity_for_evolution", True)
    monkeypatch.setattr(feedback_emit, "economic_ledger_active", lambda: True)
    parity = SimpleNamespace(
        id=9,
        agree_bool=True,
        legacy_pnl=1.25,
        ledger_pnl=1.25,
        delta_abs=0.0,
        tolerance_usd=0.01,
        mode="shadow",
    )

    class _Query:
        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def first(self):
            return parity

    class _Db:
        def query(self, *_args, **_kwargs):
            return _Query()

    credit = feedback_emit._apply_economic_ledger_credit_gate(
        _Db(),
        session_id=45,
        credit={
            "contributes_to_evolution": True,
            "reason_codes": [],
            "entry_decision_packet_id": 123,
            "outcome_class": "small_win",
        },
    )

    assert credit["contributes_to_evolution"] is True
    assert credit["economic_ledger_verification"]["ok"] is True
    assert credit["reason_codes"] == []


def test_paper_tick_entry_requires_decision_packet_before_simulated_fill(db, momentum_user_and_session, monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", True)
    monkeypatch.setattr(settings, "brain_enforce_net_expectancy_paper", False)
    monkeypatch.setattr(settings, "brain_expectancy_allocator_shadow_mode", True)
    monkeypatch.setattr(settings, "brain_capacity_hard_block_paper", False)
    monkeypatch.setattr(settings, "brain_paper_deployment_enforcement", False)
    monkeypatch.setattr(paper_runner, "runner_boundary_risk_ok", lambda _db, _sess: (True, {}))

    orig_fill = paper_runner._record_sim_fill

    def _wrap_record(db_sess, sess, *, action, fill_type, **kwargs):
        if fill_type == "entry":
            n = (
                db_sess.query(TradingDecisionPacket)
                .filter(TradingDecisionPacket.automation_session_id == int(sess.id))
                .count()
            )
            assert n >= 1
        return orig_fill(db_sess, sess, action=action, fill_type=fill_type, **kwargs)

    monkeypatch.setattr(paper_runner, "_record_sim_fill", _wrap_record)

    user, sess, via, var = momentum_user_and_session
    quote = {"mid": 100.0, "bid": 99.5, "ask": 100.5}
    out = tick_paper_session(db, int(sess.id), quote_fn=lambda _s: quote)
    assert out.get("ok") is True
    row = db.query(TradingAutomationSimulatedFill).filter_by(session_id=int(sess.id), fill_type="entry").one()
    assert row.decision_packet_id is not None
    pkt = db.query(TradingDecisionPacket).filter(TradingDecisionPacket.id == int(row.decision_packet_id)).one()
    assert pkt.linked_trade_id is None


def test_paper_tick_packet_required_even_when_ledger_disabled(db, momentum_user_and_session, monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", False)
    monkeypatch.setattr(settings, "brain_decision_packet_required_for_runners", True)
    monkeypatch.setattr(paper_runner, "runner_boundary_risk_ok", lambda _db, _sess: (True, {}))

    _user, sess, _via, _var = momentum_user_and_session
    quote = {"mid": 100.0, "bid": 99.5, "ask": 100.5}
    out = tick_paper_session(db, int(sess.id), quote_fn=lambda _s: quote)

    assert out == {"ok": False, "error": "decision_packet_missing"}
    db.refresh(sess)
    assert sess.state == "error"
    fills = db.query(TradingAutomationSimulatedFill).filter_by(session_id=int(sess.id), fill_type="entry").all()
    assert fills == []
    ev = (
        db.query(TradingAutomationEvent)
        .filter(
            TradingAutomationEvent.session_id == int(sess.id),
            TradingAutomationEvent.event_type == "paper_error",
        )
        .order_by(TradingAutomationEvent.id.desc())
        .first()
    )
    assert ev is not None
    assert ev.payload_json["reason"] == "decision_packet_required_missing"


def test_paper_tick_abstain_persists_packet_without_entry_fill(db, momentum_user_and_session, monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", True)
    monkeypatch.setattr(settings, "brain_enforce_net_expectancy_paper", True)
    monkeypatch.setattr(settings, "brain_expectancy_allocator_shadow_mode", False)
    monkeypatch.setattr(settings, "brain_minimum_net_expectancy_to_trade", 0.99)
    monkeypatch.setattr(settings, "brain_capacity_hard_block_paper", False)
    monkeypatch.setattr(settings, "brain_paper_deployment_enforcement", False)
    monkeypatch.setattr(paper_runner, "runner_boundary_risk_ok", lambda _db, _sess: (True, {}))

    user, sess, via, var = momentum_user_and_session
    via.viability_score = 0.85
    db.commit()
    quote = {"mid": 100.0, "bid": 99.5, "ask": 100.5}
    out = tick_paper_session(db, int(sess.id), quote_fn=lambda _s: quote)
    assert out.get("abstained") is True
    abstain = (
        db.query(TradingDecisionPacket)
        .filter(
            TradingDecisionPacket.automation_session_id == int(sess.id),
            TradingDecisionPacket.decision_type == "abstain",
        )
        .order_by(TradingDecisionPacket.id.desc())
        .first()
    )
    assert abstain is not None
    assert abstain.linked_trade_id is None
    fills = db.query(TradingAutomationSimulatedFill).filter_by(session_id=int(sess.id), fill_type="entry").all()
    assert fills == []


def test_live_tick_runs_entry_decision_before_place_market_order(db, momentum_user_and_live_session, monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", True)
    monkeypatch.setattr(settings, "brain_enforce_net_expectancy_live", False)
    monkeypatch.setattr(settings, "brain_expectancy_allocator_shadow_mode", True)
    monkeypatch.setattr(settings, "brain_capacity_hard_block_live", False)
    monkeypatch.setattr(settings, "brain_live_deployment_enforcement", False)

    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    monkeypatch.setattr(live_runner_mod, "runner_boundary_risk_ok", lambda _db, _sess: (True, {}))
    monkeypatch.setattr(live_runner_mod, "is_kill_switch_active", lambda: False)

    decision_calls: list[int] = []

    def _wrap_decision(*args, **kwargs):
        decision_calls.append(1)
        return run_momentum_entry_decision(*args, **kwargs)

    monkeypatch.setattr(live_runner_mod, "run_momentum_entry_decision", _wrap_decision)

    fresh = FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc))

    class _StubAdapter:
        def is_enabled(self) -> bool:
            return True

        def get_best_bid_ask(self, product_id: str):
            t = NormalizedTicker(
                product_id=product_id,
                bid=99.95,
                ask=100.05,
                mid=100.0,
                freshness=fresh,
            )
            return t, fresh

        def get_product(self, product_id: str):
            p = NormalizedProduct(
                product_id=product_id,
                base_currency="BTC",
                quote_currency="USD",
                status="online",
                trading_disabled=False,
                cancel_only=False,
                limit_only=False,
                post_only=False,
                auction_mode=False,
                base_min_size=0.00001,
                base_increment=0.00001,
            )
            return p, fresh

        def place_market_order(self, **kwargs):
            assert kwargs.get("side") == "buy"
            assert len(decision_calls) >= 1
            return {"ok": True, "order_id": "stub_oid", "client_order_id": kwargs.get("client_order_id")}

        def get_order(self, order_id: str):
            return None, fresh

        def cancel_order(self, order_id: str):
            return {"ok": True}

    _user, sess, _via, _var = momentum_user_and_live_session

    def _factory():
        return _StubAdapter()

    out = tick_live_session(db, int(sess.id), adapter_factory=_factory)
    assert out.get("ok") is True
    assert len(decision_calls) == 1


def test_live_tick_packet_required_even_when_ledger_disabled(db, momentum_user_and_live_session, monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", False)
    monkeypatch.setattr(settings, "brain_decision_packet_required_for_runners", True)

    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    monkeypatch.setattr(live_runner_mod, "runner_boundary_risk_ok", lambda _db, _sess: (True, {}))
    monkeypatch.setattr(live_runner_mod, "is_kill_switch_active", lambda: False)

    fresh = FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc))
    placed: list[dict] = []

    class _StubAdapter:
        def is_enabled(self) -> bool:
            return True

        def get_best_bid_ask(self, product_id: str):
            t = NormalizedTicker(
                product_id=product_id,
                bid=99.95,
                ask=100.05,
                mid=100.0,
                freshness=fresh,
            )
            return t, fresh

        def get_product(self, product_id: str):
            p = NormalizedProduct(
                product_id=product_id,
                base_currency="BTC",
                quote_currency="USD",
                status="online",
                trading_disabled=False,
                cancel_only=False,
                limit_only=False,
                post_only=False,
                auction_mode=False,
                base_min_size=0.00001,
                base_increment=0.00001,
            )
            return p, fresh

        def place_market_order(self, **kwargs):
            placed.append(kwargs)
            return {"ok": True, "order_id": "stub_oid", "client_order_id": kwargs.get("client_order_id")}

        def get_order(self, order_id: str):
            return None, fresh

        def cancel_order(self, order_id: str):
            return {"ok": True}

    _user, sess, _via, _var = momentum_user_and_live_session
    out = tick_live_session(db, int(sess.id), adapter_factory=lambda: _StubAdapter())

    assert out == {"ok": False, "error": "decision_packet_missing"}
    db.refresh(sess)
    assert sess.state == "live_error"
    assert placed == []


def test_live_tick_caps_order_size_before_adapter(db, momentum_user_and_live_session, monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", True)
    monkeypatch.setattr(settings, "brain_enforce_net_expectancy_live", False)
    monkeypatch.setattr(settings, "brain_expectancy_allocator_shadow_mode", True)
    monkeypatch.setattr(settings, "brain_capacity_hard_block_live", False)
    monkeypatch.setattr(settings, "brain_live_deployment_enforcement", False)

    import app.services.trading.momentum_neural.live_runner as live_runner_mod

    monkeypatch.setattr(live_runner_mod, "runner_boundary_risk_ok", lambda _db, _sess: (True, {}))
    monkeypatch.setattr(live_runner_mod, "is_kill_switch_active", lambda: False)

    fresh = FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc))
    placed: dict[str, float] = {}

    class _StubAdapter:
        def is_enabled(self) -> bool:
            return True

        def get_best_bid_ask(self, product_id: str):
            t = NormalizedTicker(
                product_id=product_id,
                bid=99.95,
                ask=100.05,
                mid=100.0,
                freshness=fresh,
            )
            return t, fresh

        def get_product(self, product_id: str):
            p = NormalizedProduct(
                product_id=product_id,
                base_currency="BTC",
                quote_currency="USD",
                status="online",
                trading_disabled=False,
                cancel_only=False,
                limit_only=False,
                post_only=False,
                auction_mode=False,
                base_min_size=0.00001,
                base_increment=0.00001,
            )
            return p, fresh

        def place_market_order(self, **kwargs):
            placed["base_size"] = float(kwargs["base_size"])
            return {"ok": True, "order_id": "stub_oid", "client_order_id": kwargs.get("client_order_id")}

        def get_order(self, order_id: str):
            return None, fresh

        def cancel_order(self, order_id: str):
            return {"ok": True}

    _user, sess, _via, _var = momentum_user_and_live_session
    snap = dict(sess.risk_snapshot_json or {})
    snap["momentum_policy_caps"] = {"max_notional_per_trade_usd": 10.0}
    sess.risk_snapshot_json = snap
    db.commit()

    out = tick_live_session(db, int(sess.id), adapter_factory=lambda: _StubAdapter())
    assert out.get("ok") is True
    assert placed["base_size"] * 100.05 * 1.0025 <= 10.0


def test_rolling_session_drawdown_feed_deescalates(db, momentum_user_and_session, monkeypatch):
    monkeypatch.setattr(settings, "brain_enable_deployment_ladder", True)
    monkeypatch.setattr(settings, "brain_deployment_degrade_drawdown_pct", 8.0)
    user, sess, via, var = momentum_user_and_session
    st = get_or_create_deployment_state(
        db, scope_type="automation_session", scope_key=f"session:{sess.id}", user_id=user.id
    )
    st.current_stage = "scaled"
    vst = get_or_create_deployment_state(
        db, scope_type="strategy_variant", scope_key=f"variant:{var.id}", user_id=user.id
    )
    vst.current_stage = "scaled"
    db.commit()
    record_trade_outcome_metrics(
        db,
        session_id=int(sess.id),
        variant_id=int(var.id),
        user_id=user.id,
        mode="paper",
        realized_pnl_usd=10.0,
        slippage_bps=1.0,
        missed_fill=False,
        partial_fill=False,
        cumulative_session_pnl_usd=100.0,
    )
    record_trade_outcome_metrics(
        db,
        session_id=int(sess.id),
        variant_id=int(var.id),
        user_id=user.id,
        mode="paper",
        realized_pnl_usd=-5.0,
        slippage_bps=1.0,
        missed_fill=False,
        partial_fill=False,
        cumulative_session_pnl_usd=82.0,
    )
    db.refresh(st)
    assert float(st.rolling_drawdown_pct or 0) >= 8.0
    assert st.current_stage == "limited"
    assert st.last_reason_code == "auto_deescalate"
    db.refresh(vst)
    assert vst.paper_trade_count == 2
    assert vst.current_stage == "limited"
    assert vst.last_reason_code == "auto_deescalate"
    assert (vst.stage_metrics_json or {}).get("last_session_scope_key") == f"session:{sess.id}"


def test_paper_variant_observations_do_not_restrict_learning_lane(db, momentum_user_and_session, monkeypatch):
    monkeypatch.setattr(settings, "brain_enable_deployment_ladder", True)
    monkeypatch.setattr(settings, "brain_deployment_degrade_drawdown_pct", 1.0)
    user, sess, via, var = momentum_user_and_session
    record_trade_outcome_metrics(
        db,
        session_id=int(sess.id),
        variant_id=int(var.id),
        user_id=user.id,
        mode="paper",
        realized_pnl_usd=-50.0,
        slippage_bps=1.0,
        missed_fill=False,
        partial_fill=False,
        cumulative_session_pnl_usd=-50.0,
    )

    vst = (
        db.query(TradingDeploymentState)
        .filter(
            TradingDeploymentState.scope_type == "strategy_variant",
            TradingDeploymentState.scope_key == f"variant:{var.id}",
        )
        .one()
    )
    assert vst.paper_trade_count == 1
    assert vst.current_stage == "paper"
    assert vst.rolling_expectancy_net == -50.0
