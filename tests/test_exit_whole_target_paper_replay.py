"""Actual simulated target routing; no broker, provider or historical replay I/O.

Paper cases execute tick_paper_session and persist its real fill/event rows.
Replay cases execute the production nested manage_open and close_trade code
objects with deterministic closure inputs, avoiding run_replay's historical DB
and provider loaders. These are target-shape tests, not full G/capture parity.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import FunctionType, CodeType, SimpleNamespace

import pytest

from app.config import settings
from app.models.core import User
from app.models.trading import (
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationEvent,
    TradingAutomationSession,
    TradingAutomationSimulatedFill,
)
from app.services.trading.momentum_neural import paper_runner as paper
from app.services.trading.momentum_neural import replay_v2 as replay
from app.services.trading.momentum_neural.paper_fsm import (
    STATE_ENTERED, STATE_EXITED, STATE_SCALING_OUT, STATE_TRAILING,
)
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Whole-target regression attempted network I/O")

    import requests
    import httpx
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    try:
        from curl_cffi.requests import Session
        monkeypatch.setattr(Session, "request", forbidden)
    except ImportError:
        pass


@pytest.mark.parametrize(
    "symbol,family,anchor,qty,original,partial,whole",
    [
        ("WTGT", "coinbase_spot", "valid", 20, 20, False, True),
        ("WTGT", "alpaca_spot", "valid", 20, 20, False, True),
        ("WTGT", "coinbase_spot", "valid", 10, 20, True, True),
        ("SIM-USD", "coinbase_spot", "valid", 20, 20, False, False),
        ("WTGT", "coinbase_spot", None, 20, 20, False, False),
        ("WTGT", "coinbase_spot", "malformed", 20, 20, False, False),
    ],
)
def test_actual_paper_target_quantity_and_persisted_receipt(
    db, monkeypatch, symbol, family, anchor, qty, original, partial, whole,
):
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    monkeypatch.setattr(paper, "runner_boundary_risk_ok", lambda *_: (True, {}))
    from app.services.trading import market_data
    monkeypatch.setattr(market_data, "fetch_ohlcv_df", lambda *_, **__: None)
    now = datetime(2026, 9, 10, 14, 0)
    monkeypatch.setattr(paper, "_utcnow", lambda: now)
    # Seed a persisted held context directly. Scoring a new equity candidate
    # would perform unrelated external fundamentals reads before the target.
    user = User(name="Whole target paper test")
    variant = MomentumStrategyVariant(
        family="impulse_breakout", variant_key="whole-target-fixture",
        params_json={}, label="Whole target fixture", execution_family=family,
    )
    db.add_all([user, variant])
    db.flush()
    vid = int(variant.id)
    via = MomentumSymbolViability(
        symbol=symbol, variant_id=vid, freshness_ts=now, viability_score=0.99,
        paper_eligible=True, live_eligible=False, scope="symbol",
        regime_snapshot_json={}, explain_json={}, evidence_window_json={},
        execution_readiness_json={
            "spread_bps": 0.0, "slippage_estimate_bps": 0.0, "fee_to_target_ratio": 0.0,
        },
    )
    db.add(via)
    position = {
        "side": "long", "entry_price": 10.0, "quantity": qty,
        "original_quantity": original, "stop_price": 9.5, "target_price": 10.35,
        "high_water_mark": 10.0, "fees_est_usd": 2.0, "partial_taken": partial,
    }
    if anchor:
        position["opened_at_utc"] = (
            (now - timedelta(seconds=10)).isoformat() if anchor == "valid" else anchor
        )
    sess = TradingAutomationSession(
        user_id=int(user.id), symbol=symbol, variant_id=vid,
        mode="paper", execution_family=family, venue="coinbase",
        state=STATE_TRAILING if partial else STATE_ENTERED,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {},
            paper.KEY_PAPER_EXEC: {"position": position, "realized_pnl_usd": 0.0},
        },
    )
    db.add(sess)
    db.flush()
    # An actual canonical simulated entry precedes this held target test.
    paper._record_sim_fill(
        db, sess, action="enter_long", fill_type="entry", price=10.0,
        quantity=original if partial else qty,
        position_state_before="flat", position_state_after="long",
        reason="test_recorded_entry", strict=True,
    )
    if partial:
        paper._record_sim_fill(
            db, sess, action="exit_long", fill_type="exit", price=10.0,
            quantity=original - qty, position_state_before="long",
            position_state_after="long", reason="legacy_partial",
            marker_json={"entry": 10.0}, pnl_usd=0.0, strict=True,
        )
    quote = lambda _: {"bid": 10.35, "ask": 10.35, "mid": 10.35, "source": "fixture"}
    first = paper.tick_paper_session(db, int(sess.id), quote_fn=quote)
    assert first.get("ok"), first
    assert sess.state == STATE_SCALING_OUT
    out = paper.tick_paper_session(db, int(sess.id), quote_fn=quote)
    assert out.get("ok"), out
    db.flush()
    fills = db.query(TradingAutomationSimulatedFill).filter_by(
        session_id=sess.id, fill_type="exit",
    ).order_by(TradingAutomationSimulatedFill.id).all()
    assert len(fills) == (2 if partial else 1)
    fill = fills[-1]
    pe = sess.risk_snapshot_json[paper.KEY_PAPER_EXEC]
    assert fill.price == pytest.approx(10.35)
    assert fill.marker_json["first_target_leaves_runner"] is (not whole)
    assert fill.marker_json["exit_shape_basis"] == (
        "whole_position_policy" if whole else "legacy_scale_policy"
    )
    if whole:
        assert fill.quantity == qty
        assert fill.reason == "target"
        assert fill.position_state_after == "flat"
        assert sess.state == STATE_EXITED
        assert pe["position"] is None
        assert fill.pnl_usd == pytest.approx(0.35 * qty - 2.0)
        assert not db.query(TradingAutomationEvent).filter_by(
            session_id=sess.id, event_type="paper_scaled_out_to_runner",
        ).count()
    else:
        assert 0 < fill.quantity < qty
        assert fill.reason == "scale_out_target"
        assert fill.position_state_after == "long"
        assert sess.state == STATE_TRAILING
        assert pe["position"]["quantity"] == pytest.approx(qty - fill.quantity)
        assert pe["position"]["partial_taken"] is True
    # The terminal/runner state and receipt survive the owning transaction.
    db.commit()
    db.refresh(sess)
    assert sess.state == (STATE_EXITED if whole else STATE_TRAILING)


def _closure_cell(value):
    return (lambda: value).__closure__[0]


def _nested_replay_function(name, scope):
    """Use the original compiled body, with only deterministic outer inputs."""
    code = next(
        value for value in replay.run_replay.__code__.co_consts
        if isinstance(value, CodeType) and value.co_name == name
    )
    return FunctionType(
        code, replay.__dict__, name,
        closure=tuple(_closure_cell(scope[key]) for key in code.co_freevars),
    )


@pytest.mark.parametrize(
    "symbol,family,policy,whole,already_scaled",
    [
        ("WTGT", "coinbase_spot", True, True, False),
        ("WTGT", "alpaca_spot", True, True, False),
        ("WTGT", "coinbase_spot", True, True, True),
        ("WTGT", "coinbase_spot", False, False, False),
        ("WTGT", "coinbase_spot", False, False, True),
        ("SIM-USD", "coinbase_spot", False, False, False),
        ("SIM-USD", "coinbase_spot", False, False, True),
        # Even a stale inherited equity marker cannot opt a crypto leg in.
        ("SIM-USD", "coinbase_spot", True, False, False),
    ],
)
def test_actual_replay_management_target_sells_whole_or_named_fallback(
    monkeypatch, symbol, family, policy, whole, already_scaled,
):
    monkeypatch.setattr(replay, "REPLAY_ENGINE_ON", False)
    monkeypatch.setattr(replay, "TRAIL_ACTIVATE_BPS", 10000)
    monkeypatch.setattr(replay, "cushion_adaptive_trail_stop", lambda **kw: kw["breakeven_floor"])
    monkeypatch.setattr(settings, "chili_momentum_exit_adaptive_equity_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_exit_ladder_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_exit_ask_pressure_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_exit_ofi_lock_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False, raising=False)
    now = datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc)
    p = {
        "entry": 10.0, "qty": 10.0 if already_scaled else 20.0,
        "qty0": 20.0, "stop": 9.5, "stop0": 9.5,
        "target": 10.35, "hwm": 10.0, "scaled": already_scaled, "scale_usd": 0.0,
        "trail_armed": False, "atrp": 0.02, "meta": {"sym": symbol},
        "execution_family": family, "whole_position_exit": policy,
    }
    scope = {
        "open_pos": {symbol: p}, "trades": [],
        "state": {"cum": 0.0, "peak": 0.0, "halted": None},
        "use_recorded": False, "loss_strikes": {}, "loss_cooldown_until": {},
        "engine_open_risk_usd": {"v": 0.0}, "daily_loss_cap_usd": 1000.0,
        "LOSS_COOLDOWN_MIN": 1,
        "now": now, "tape": SimpleNamespace(
            in_halt=lambda *_: False, at=lambda *_: (10.35, 10.35),
        ), "bars": lambda _: None,
    }
    scope["close_trade"] = _nested_replay_function("close_trade", scope)
    manage_open = _nested_replay_function("manage_open", scope)
    manage_open(now, None)
    if already_scaled and not whole:
        # Legacy runners do not take a second target merely because they are
        # still above it; their existing protection/trail remains responsible.
        assert symbol in scope["open_pos"]
        assert p["qty"] == 10.0
        assert p["scaled"] is True
        assert "first_target_leaves_runner" not in p["meta"]
        assert scope["trades"] == []
        assert scope["state"]["cum"] == 0.0
        return
    assert p["meta"]["first_target_leaves_runner"] is (not whole)
    if whole:
        assert symbol not in scope["open_pos"]
        assert p["scaled"] is already_scaled
        assert p["scale_usd"] == 0
        assert len(scope["trades"]) == 1
        assert scope["trades"][0]["why"] == "target"
        assert scope["state"]["cum"] == pytest.approx(3.5 if already_scaled else 7.0)
        assert scope["trades"][0]["exit_shape_basis"] == "whole_position_policy"
    else:
        assert symbol in scope["open_pos"]
        assert p["scaled"]
        assert 0 < p["qty"] < 20
        assert p["stop"] >= 10.0
        assert scope["trades"] == []
        assert p["meta"]["exit_shape_basis"] == "legacy_scale_policy"
