"""Ross-style profit-giveback session halt for the momentum LIVE lane.

Ross's rule (warriortrading.com/7-day-trading-rules, confirmed 2026-06-07): once he
gives back 50% of his PEAK accumulated daily profit he STOPS trading for the day.
This is the UPSIDE mirror of the equity-relative daily-loss cap. The giveback FRACTION
is the single documented knob (chili_momentum_profit_giveback_fraction, default 0.5);
the activation threshold is equity-relative (reuses the daily-loss-cap magnitude — no
second fixed-$ number). [[feedback_adaptive_no_magic]] [[project_momentum_lane]]
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import automation_query as aq
from app.services.trading.momentum_neural import risk_evaluator as re_mod
from app.services.trading.momentum_neural import risk_policy as rp_mod
from app.services.trading.momentum_neural.persistence import ensure_momentum_strategy_variants
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY


# ── Pure high-water-mark arithmetic (no I/O) ──────────────────────────────────


def test_running_peak_empty_is_flat() -> None:
    assert re_mod._running_peak_and_total([]) == (0.0, 0.0)


def test_running_peak_tracks_high_water_mark() -> None:
    # Cumulative: 60, 110, 80, 100 -> peak is the running max (110), not max single pnl
    # (60) and not the final total (100).
    peak, total = re_mod._running_peak_and_total([60.0, 50.0, -30.0, 20.0])
    assert peak == pytest.approx(110.0)
    assert total == pytest.approx(100.0)


def test_running_peak_floored_at_zero_when_never_green() -> None:
    # A day that only ever lost has no PEAK PROFIT to give back -> floored to 0.0.
    peak, total = re_mod._running_peak_and_total([-50.0, 30.0])
    assert peak == 0.0
    assert total == pytest.approx(-20.0)


def test_running_peak_ignores_none_pnl() -> None:
    peak, total = re_mod._running_peak_and_total([100.0, None, -30.0])
    assert peak == pytest.approx(100.0)
    assert total == pytest.approx(70.0)


# ── Halt decision (equity-relative activation + giveback fraction) ────────────


def _patch_decision(monkeypatch, *, peak, current, activation=110.0, fraction=0.5):
    """Drive evaluate_profit_giveback_halt with synthetic peak/current/activation."""
    monkeypatch.setattr(
        re_mod,
        "_daily_realized_pnl_peak_and_current",
        lambda db, uid, **kwargs: (peak, current),
    )
    monkeypatch.setattr(re_mod, "equity_relative_daily_loss_cap", lambda *a, **k: activation)
    monkeypatch.setattr(settings, "chili_momentum_profit_giveback_fraction", fraction, raising=False)


def test_halts_after_50pct_giveback(monkeypatch) -> None:
    # Peaked $200, gave back to $90 (>=50% giveback; floor = 200*0.5 = 100) -> HALT.
    _patch_decision(monkeypatch, peak=200.0, current=90.0, activation=110.0, fraction=0.5)
    out = re_mod.evaluate_profit_giveback_halt(object(), user_id=1)
    assert out["halted"] is True
    assert out["peak_pnl_usd"] == 200.0
    assert out["daily_pnl_usd"] == 90.0
    assert out["giveback_floor_usd"] == 100.0


def test_within_giveback_band_does_not_halt(monkeypatch) -> None:
    # Peaked $200, only down to $150 (25% giveback < 50%; floor = 100) -> NO halt.
    _patch_decision(monkeypatch, peak=200.0, current=150.0, activation=110.0, fraction=0.5)
    out = re_mod.evaluate_profit_giveback_halt(object(), user_id=1)
    assert out["halted"] is False
    assert out["armed"] is True  # peak >= activation, the rule IS armed, just within band


def test_below_activation_threshold_does_not_halt(monkeypatch) -> None:
    # Peaked only $80 (< $110 activation) then gave back to $20 (75% giveback). The
    # activation gate must prevent halting on trivial profit swings.
    _patch_decision(monkeypatch, peak=80.0, current=20.0, activation=110.0, fraction=0.5)
    out = re_mod.evaluate_profit_giveback_halt(object(), user_id=1)
    assert out["armed"] is False
    assert out["halted"] is False


def test_new_day_resets_no_halt(monkeypatch) -> None:
    # A fresh UTC day has no terminated outcomes yet -> peak/current both 0 -> no halt
    # (the rule resets with the daily window, same as the daily-loss cap).
    _patch_decision(monkeypatch, peak=0.0, current=0.0, activation=110.0, fraction=0.5)
    out = re_mod.evaluate_profit_giveback_halt(object(), user_id=1)
    assert out["halted"] is False
    assert out["peak_pnl_usd"] == 0.0


def test_fraction_zero_disables_rule(monkeypatch) -> None:
    # Operator disable: fraction 0 -> never halts, however far the giveback.
    _patch_decision(monkeypatch, peak=500.0, current=10.0, activation=110.0, fraction=0.0)
    out = re_mod.evaluate_profit_giveback_halt(object(), user_id=1)
    assert out["armed"] is False
    assert out["halted"] is False


def test_activation_falls_back_to_fixed_when_no_equity(monkeypatch) -> None:
    # Real equity-relative path: no equity -> activation falls back to the fixed
    # daily-loss cap ($250). Peak $200 < $250 -> not armed (no second magic number).
    monkeypatch.setattr(rp_mod, "_account_equity_usd", lambda *a, **k: None)
    monkeypatch.setattr(settings, "chili_momentum_risk_max_daily_loss_usd", 250.0, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_profit_giveback_fraction", 0.5, raising=False)
    monkeypatch.setattr(
        re_mod,
        "_daily_realized_pnl_peak_and_current",
        lambda db, uid, **kwargs: (200.0, 80.0),
    )
    out = re_mod.evaluate_profit_giveback_halt(object(), user_id=1)
    assert out["activation_threshold_usd"] == 250.0
    assert out["armed"] is False
    assert out["halted"] is False


# ── Real-DB peak computation + daily-window reset ─────────────────────────────


def _mk_outcome(db: Session, u: User, v: MomentumStrategyVariant, *, pnl: float, terminal_at: datetime, symbol: str):
    s = TradingAutomationSession(
        user_id=u.id,
        mode="live",
        symbol=symbol,
        variant_id=v.id,
        state="live_finished",
        risk_snapshot_json={RISK_SNAPSHOT_KEY: {"allowed": True}},
        ended_at=terminal_at,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    db.add(
        MomentumAutomationOutcome(
            session_id=s.id,
            user_id=u.id,
            variant_id=v.id,
            symbol=symbol,
            mode="live",
            execution_family="coinbase_spot",
            terminal_state=s.state,
            terminal_at=terminal_at,
            outcome_class="small_win" if pnl >= 0 else "small_loss",
            realized_pnl_usd=pnl,
            return_bps=pnl * 10.0,
            regime_snapshot_json={},
            entry_regime_snapshot_json={},
            exit_regime_snapshot_json={},
            readiness_snapshot_json={},
            admission_snapshot_json={},
            governance_context_json={},
            evidence_weight=1.0,
            contributes_to_evolution=True,
        )
    )
    db.commit()


def test_peak_and_current_from_db_is_et_day_and_prefix_bounded(db: Session) -> None:
    from sqlalchemy import inspect as sa_inspect

    if "momentum_automation_outcomes" not in set(sa_inspect(db.bind).get_table_names()):
        pytest.skip("momentum_automation_outcomes table not present")

    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").first()
    assert v is not None
    u = User(name="GivebackPeak")
    db.add(u)
    db.commit()
    db.refresh(u)

    frontier = datetime(2026, 7, 14, 16, 0, tzinfo=timezone.utc)
    day_start, _day_end = rp_mod._et_day_bounds_utc(as_of_utc=frontier)
    # Prior ET day must not leak into this one.
    _mk_outcome(db, u, v, pnl=500.0, terminal_at=day_start - timedelta(hours=1), symbol="PRIOR-USD")
    # In event order: cumulative 60, 110, 80, 100, 105 -> peak 110, current 105.
    _mk_outcome(db, u, v, pnl=60.0, terminal_at=day_start + timedelta(hours=1), symbol="A-USD")
    _mk_outcome(db, u, v, pnl=50.0, terminal_at=day_start + timedelta(hours=2), symbol="B-USD")
    _mk_outcome(db, u, v, pnl=-30.0, terminal_at=day_start + timedelta(hours=3), symbol="C-USD")
    _mk_outcome(db, u, v, pnl=20.0, terminal_at=day_start + timedelta(hours=4), symbol="D-USD")
    # Exact-frontier outcome is visible; a later same-day outcome is not.
    _mk_outcome(db, u, v, pnl=5.0, terminal_at=frontier.replace(tzinfo=None), symbol="EDGE-USD")
    future = frontier.replace(tzinfo=None) + timedelta(minutes=1)
    _mk_outcome(db, u, v, pnl=-1000.0, terminal_at=future, symbol="FUTURE-USD")

    peak, current = re_mod._daily_realized_pnl_peak_and_current(
        db, int(u.id), as_of_utc=frontier
    )
    assert peak == pytest.approx(110.0)
    assert current == pytest.approx(105.0)
    # And _daily_realized_pnl (used by the daily-loss cap) agrees on the current total.
    assert re_mod._daily_realized_pnl(
        db, int(u.id), as_of_utc=frontier
    ) == pytest.approx(105.0)

    # Advancing the frontier past the future close makes the same row visible.
    after = future.replace(tzinfo=timezone.utc) + timedelta(seconds=1)
    assert re_mod._daily_realized_pnl(
        db, int(u.id), as_of_utc=after
    ) == pytest.approx(-895.0)


# ── Monitor lane_status surfacing (halt_reason) ───────────────────────────────


def test_lane_status_surfaces_profit_giveback(monkeypatch) -> None:
    # Not below the daily-loss cap, but a green day gave back >=50% -> the Monitor card
    # must surface halt_reason='profit_giveback' (amber "locked in green" banner).
    monkeypatch.setattr(re_mod, "_daily_realized_pnl", lambda db, uid, **kwargs: 90.0)
    monkeypatch.setattr(rp_mod, "equity_relative_daily_loss_cap", lambda *a, **k: 110.0)
    monkeypatch.setattr(
        re_mod, "evaluate_profit_giveback_halt",
        lambda db, **k: {
            "halted": True, "armed": True, "peak_pnl_usd": 200.0, "daily_pnl_usd": 90.0,
            "activation_threshold_usd": 110.0, "giveback_fraction": 0.5, "giveback_floor_usd": 100.0,
        },
    )
    st = aq._compute_lane_status(object(), user_id=1)
    assert st["halted"] is True
    assert st["halt_reason"] == "profit_giveback"
    assert st["peak_pnl_usd"] == 200.0
    assert st["giveback_fraction"] == 0.5


def test_lane_status_daily_loss_takes_precedence(monkeypatch) -> None:
    # When today's realized PnL is below the daily-loss cap, that reason wins and the
    # giveback branch is not even evaluated.
    monkeypatch.setattr(re_mod, "_daily_realized_pnl", lambda db, uid, **kwargs: -150.0)
    monkeypatch.setattr(rp_mod, "equity_relative_daily_loss_cap", lambda *a, **k: 110.0)

    def _must_not_run(*a, **k):
        raise AssertionError("giveback must not be evaluated when daily-loss cap halts")

    monkeypatch.setattr(re_mod, "evaluate_profit_giveback_halt", _must_not_run)
    st = aq._compute_lane_status(object(), user_id=1)
    assert st["halted"] is True
    assert st["halt_reason"] == "daily_loss_cap"
