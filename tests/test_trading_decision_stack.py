from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.config import Settings, settings
from app.models import (
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationSession,
    TradingAutomationSimulatedFill,
    TradingDecisionCandidate,
    TradingDecisionPacket,
    TradingDeploymentState,
    User,
)
from app.services.trading.capacity_governor import evaluate_capacity
from app.services.trading.decision_ledger import run_momentum_entry_decision
from app.services.trading.deployment_ladder_service import (
    evaluate_de_escalation,
    get_or_create_deployment_state,
    record_trade_outcome_metrics,
    sync_initial_stage_from_viability,
)
from app.services.trading.momentum_neural import paper_runner
from app.services.trading.momentum_neural.live_fsm import STATE_LIVE_PENDING_ENTRY
from app.services.trading.momentum_neural.live_runner import tick_live_session
from app.services.trading.momentum_neural.paper_runner import tick_paper_session
from app.services.trading.portfolio_allocator import allocate_momentum_session_entry
from app.services.trading.venue.protocol import FreshnessMeta, NormalizedProduct, NormalizedTicker


@pytest.fixture
def momentum_user_and_session(db):
    user = User(name="Decision Stack User")
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
    user = User(name="Decision Stack Live User")
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


def test_live_enforcement_flags_default_off():
    assert Settings.model_fields["brain_enforce_net_expectancy_live"].default is False
    assert Settings.model_fields["brain_capacity_hard_block_live"].default is False
    assert Settings.model_fields["brain_live_deployment_enforcement"].default is False


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
    via.viability_score = 0.01
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
                bid=99.5,
                ask=100.5,
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


def test_rolling_session_drawdown_feed_deescalates(db, momentum_user_and_session, monkeypatch):
    monkeypatch.setattr(settings, "brain_enable_deployment_ladder", True)
    monkeypatch.setattr(settings, "brain_deployment_degrade_drawdown_pct", 8.0)
    user, sess, via, var = momentum_user_and_session
    st = get_or_create_deployment_state(
        db, scope_type="automation_session", scope_key=f"session:{sess.id}", user_id=user.id
    )
    st.current_stage = "scaled"
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
