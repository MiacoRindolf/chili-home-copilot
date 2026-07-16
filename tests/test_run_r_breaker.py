"""MACRO run-R breaker (L2.1, project_profitability_levers) — a SOFT, regime-RELATIVE
entry-bar raise when the lane's recent realized-R turns negative AND below its OWN
baseline (a no-follow-through regime). Entry-side only; freeze-safe (releases on
recovery). Run this file ALONE vs chili_test (truncating fixtures collide cross-module —
reference_pytest_db_isolation)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import risk_policy as rp

FAM = "robinhood_agentic_mcp"
_seq = 0


def _closed(db, *, realized_pnl, max_loss_cap, terminal_at, family=FAM, mode="live", symbol="TST", outcome_class=None):
    """A closed LIVE momentum session + terminal outcome carrying a frozen max-loss cap
    (the realized-R denominator). realized_R = realized_pnl / max_loss_cap. outcome_class
    defaults to win/loss (both REAL entered); pass cancelled_pre_entry etc. to model a
    never-entered row that must be PRUNED from the realized-R window."""
    global _seq
    _seq += 1
    variant = MomentumStrategyVariant(family="test_family", variant_key=f"rr_{_seq}", label="t", params_json={})
    db.add(variant)
    db.flush()
    sess = TradingAutomationSession(
        user_id=None, venue="test", execution_family=family, mode=mode, symbol=symbol,
        variant_id=variant.id, state="live_exited",
        risk_snapshot_json={"momentum_policy_caps": {"max_loss_per_trade_usd": float(max_loss_cap)}},
    )
    db.add(sess)
    db.flush()
    oc = outcome_class if outcome_class is not None else ("win" if realized_pnl >= 0 else "loss")
    out = MomentumAutomationOutcome(
        session_id=sess.id, user_id=None, variant_id=variant.id, symbol=symbol, mode=mode,
        execution_family=family, terminal_state="exited", terminal_at=terminal_at,
        outcome_class=oc, realized_pnl_usd=float(realized_pnl),
    )
    db.add(out)
    db.flush()
    return out


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setattr(rp.settings, "chili_momentum_run_r_breaker_enabled", True, raising=False)
    monkeypatch.setattr(rp.settings, "chili_momentum_run_r_breaker_viability_bump", 0.05, raising=False)
    monkeypatch.setattr(rp.settings, "chili_momentum_run_r_breaker_lookback", 40, raising=False)
    monkeypatch.setattr(rp.settings, "chili_momentum_run_r_breaker_short_window", 10, raising=False)
    monkeypatch.setattr(rp.settings, "chili_momentum_run_r_breaker_min_history", 8, raising=False)


def _base():
    return datetime(2026, 6, 22, 12, 0, 0)


def test_realized_r_computation(db):
    _closed(db, realized_pnl=30.0, max_loss_cap=20.0, terminal_at=_base())                          # +1.5R
    _closed(db, realized_pnl=-20.0, max_loss_cap=20.0, terminal_at=_base() + timedelta(minutes=1))  # -1.0R
    rr = rp._recent_realized_r(db, execution_family=FAM, lookback=40)
    assert rr == pytest.approx([-1.0, 1.5])  # most-recent-first


def test_replay_frontier_excludes_future_outcomes_from_run_r_and_streak(db):
    frontier = _base() + timedelta(minutes=10)
    for i in range(5):
        _closed(
            db,
            realized_pnl=30.0,
            max_loss_cap=20.0,
            terminal_at=_base() + timedelta(minutes=i),
            outcome_class="win",
        )
    for i in range(3):
        _closed(
            db,
            realized_pnl=-200.0,
            max_loss_cap=20.0,
            terminal_at=frontier + timedelta(minutes=i + 1),
            outcome_class="loss",
        )

    with rp.replay_risk_clock(frontier):
        rr = rp._recent_realized_r(db, execution_family=FAM, lookback=40)
        streak, meta = rp.streak_risk_multiplier(db, execution_family=FAM)

    assert rr == pytest.approx([1.5] * 5)
    assert streak == pytest.approx(1.5)
    assert meta["n"] == 5


def test_thin_history_fails_open(db):
    for i in range(5):  # < min_history (8)
        _closed(db, realized_pnl=-20.0, max_loss_cap=20.0, terminal_at=_base() + timedelta(minutes=i))
    bump, meta = rp.run_r_viability_bump(db, FAM)
    assert bump == 0.0
    assert meta["reason"] == "thin_history"


def test_triggers_when_recent_losing_and_below_baseline(db):
    # 8 older winners (+1.5R), then 8 recent losers (-1.0R). short(10) = 8 losers + 2 winners
    # => short_mean = (8*-1 + 2*1.5)/10 = -0.5 < 0 AND < long_mean (0.25) => triggered.
    t = _base()
    for i in range(8):
        _closed(db, realized_pnl=30.0, max_loss_cap=20.0, terminal_at=t + timedelta(minutes=i))
    for i in range(8):
        _closed(db, realized_pnl=-20.0, max_loss_cap=20.0, terminal_at=t + timedelta(minutes=100 + i))
    bump, meta = rp.run_r_viability_bump(db, FAM)
    assert bump == pytest.approx(0.05)
    assert meta["triggered"] is True
    assert meta["short_mean_r"] < meta["long_mean_r"]
    assert meta["short_mean_r"] < 0


def test_no_trigger_when_recent_healthy(db):
    t = _base()
    for i in range(16):  # all winners
        _closed(db, realized_pnl=30.0, max_loss_cap=20.0, terminal_at=t + timedelta(minutes=i))
    bump, meta = rp.run_r_viability_bump(db, FAM)
    assert bump == 0.0
    assert meta["triggered"] is False


def test_recovery_releases_the_bump(db):
    # 8 older losers, then 10 recent winners — the SHORT window is healthy now => released.
    # Freeze-safe: a bad stretch does not permanently de-arm once performance recovers.
    t = _base()
    for i in range(8):
        _closed(db, realized_pnl=-20.0, max_loss_cap=20.0, terminal_at=t + timedelta(minutes=i))
    for i in range(10):
        _closed(db, realized_pnl=30.0, max_loss_cap=20.0, terminal_at=t + timedelta(minutes=100 + i))
    bump, meta = rp.run_r_viability_bump(db, FAM)
    assert bump == 0.0
    assert meta["triggered"] is False


def test_excludes_cancelled_pre_entry(db):
    # 30 never-entered cancelled_pre_entry rows (0.0R, oldest) + 6 older real winners (+1.5R)
    # + 10 recent real losers (-1.0R). The cancels must be PRUNED — else (given the lane's
    # heavy cancel:fill churn) they would dominate the window and dilute both means toward 0,
    # neutering the breaker. This is the defect the adversarial review (wf wkocus1kc) caught;
    # is_real_entry_outcome prunes them so the metric measures ENTERED-trade follow-through.
    t = _base()
    for i in range(30):
        _closed(db, realized_pnl=0.0, max_loss_cap=20.0,
                terminal_at=t + timedelta(minutes=i), outcome_class="cancelled_pre_entry")
    for i in range(6):
        _closed(db, realized_pnl=30.0, max_loss_cap=20.0,
                terminal_at=t + timedelta(minutes=40 + i), outcome_class="win")
    for i in range(10):
        _closed(db, realized_pnl=-20.0, max_loss_cap=20.0,
                terminal_at=t + timedelta(minutes=200 + i), outcome_class="loss")
    rr = rp._recent_realized_r(db, execution_family=FAM, lookback=40)
    assert 0.0 not in rr                       # zero cancel-noise leaked in
    assert len(rr) == 16                        # ONLY the 16 real entered trades
    assert rr[:10] == pytest.approx([-1.0] * 10)  # recent real losers first
    # un-diluted, the recent real losing stretch correctly triggers the breaker
    bump, meta = rp.run_r_viability_bump(db, FAM)
    assert bump == pytest.approx(0.05)
    assert meta["triggered"] is True


def test_off_is_byte_identical(db, monkeypatch):
    monkeypatch.setattr(rp.settings, "chili_momentum_run_r_breaker_enabled", False, raising=False)
    t = _base()
    for i in range(16):
        _closed(db, realized_pnl=-20.0, max_loss_cap=20.0, terminal_at=t + timedelta(minutes=i))
    bump, meta = rp.run_r_viability_bump(db, FAM)
    assert bump == 0.0
    assert meta["reason"] == "disabled"
