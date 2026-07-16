"""G4 P2 (review m1) — day-wide green-banked basis for the escalation reset.

The green-banked reset must key on the SYMBOL's today-ET NET realized PnL across all
sessions (the ``_count_symbol_episodes_today`` precedent), not one session's local
ledger. ``symbol_day_banked_pnl_other_sessions`` supplies the other-terminal-sessions
half; the live_runner caller adds the current session's cumulative on top."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural.risk_policy import (
    replay_risk_clock,
    symbol_day_banked_pnl_other_sessions,
)


def _seed(db, *, symbol: str, pnl: float | None, mode: str = "live",
          execution_family: str = "robinhood_spot", outcome_class: str = "small_win",
          terminal_at: datetime | None = None) -> int:
    """One terminal session + its outcome row; returns the session id."""
    variant = MomentumStrategyVariant(
        family="g4_test",
        variant_key=f"g4m1_{uuid.uuid4().hex[:12]}",
        label="g4 m1 test variant",
        params_json={},
    )
    db.add(variant)
    db.flush()
    now = terminal_at or datetime.utcnow()
    sess = TradingAutomationSession(
        mode=mode, symbol=symbol, variant_id=variant.id, state="finished", ended_at=now,
    )
    db.add(sess)
    db.flush()
    db.add(MomentumAutomationOutcome(
        session_id=sess.id, variant_id=variant.id, symbol=symbol, mode=mode,
        execution_family=execution_family, terminal_state="finished", terminal_at=now,
        outcome_class=outcome_class, realized_pnl_usd=pnl,
        regime_snapshot_json={}, entry_regime_snapshot_json={},
        exit_regime_snapshot_json={}, readiness_snapshot_json={},
        admission_snapshot_json={}, governance_context_json={},
        extracted_summary_json={}, evidence_weight=1.0, contributes_to_evolution=False,
    ))
    db.flush()
    return int(sess.id)


def test_sums_today_live_outcomes_across_sessions(db) -> None:
    _seed(db, symbol="G4M1", pnl=100.0)
    _seed(db, symbol="G4M1", pnl=-40.0)
    total = symbol_day_banked_pnl_other_sessions(db, symbol="G4M1")
    assert total == pytest.approx(60.0)


def test_excludes_the_current_session(db) -> None:
    keep = _seed(db, symbol="G4M2", pnl=50.0)
    drop = _seed(db, symbol="G4M2", pnl=999.0)
    total = symbol_day_banked_pnl_other_sessions(db, symbol="G4M2", exclude_session_id=drop)
    assert total == pytest.approx(50.0)
    assert keep != drop


def test_excludes_never_entered_outcome_classes(db) -> None:
    _seed(db, symbol="G4M3", pnl=25.0)
    _seed(db, symbol="G4M3", pnl=500.0, outcome_class="risk_block")  # never entered
    total = symbol_day_banked_pnl_other_sessions(db, symbol="G4M3")
    assert total == pytest.approx(25.0)


def test_excludes_paper_mode_and_other_symbols(db) -> None:
    _seed(db, symbol="G4M4", pnl=10.0)
    _seed(db, symbol="G4M4", pnl=77.0, mode="paper")
    _seed(db, symbol="OTHER", pnl=88.0)
    total = symbol_day_banked_pnl_other_sessions(db, symbol="G4M4")
    assert total == pytest.approx(10.0)


def test_execution_family_filter(db) -> None:
    _seed(db, symbol="G4M5", pnl=10.0, execution_family="robinhood_spot")
    _seed(db, symbol="G4M5", pnl=66.0, execution_family="coinbase_spot")
    total = symbol_day_banked_pnl_other_sessions(
        db, symbol="G4M5", execution_family="robinhood_spot",
    )
    assert total == pytest.approx(10.0)
    # no family filter: both count
    total_all = symbol_day_banked_pnl_other_sessions(db, symbol="G4M5")
    assert total_all == pytest.approx(76.0)


def test_prior_days_do_not_count(db) -> None:
    _seed(db, symbol="G4M6", pnl=10.0)
    _seed(db, symbol="G4M6", pnl=400.0, terminal_at=datetime.utcnow() - timedelta(days=3))
    total = symbol_day_banked_pnl_other_sessions(db, symbol="G4M6")
    assert total == pytest.approx(10.0)


def test_replay_frontier_excludes_future_same_day_sessions(db) -> None:
    et = ZoneInfo("America/New_York")
    utc = ZoneInfo("UTC")
    frontier = (
        datetime.now(et)
        .replace(hour=12, minute=0, second=0, microsecond=0)
        .astimezone(utc)
        .replace(tzinfo=None)
    )
    _seed(db, symbol="G4FUT", pnl=10.0, terminal_at=frontier - timedelta(minutes=1))
    _seed(db, symbol="G4FUT", pnl=999.0, terminal_at=frontier + timedelta(minutes=1))

    with replay_risk_clock(frontier):
        total = symbol_day_banked_pnl_other_sessions(db, symbol="G4FUT")

    assert total == pytest.approx(10.0)


def test_no_rows_is_zero_not_none(db) -> None:
    total = symbol_day_banked_pnl_other_sessions(db, symbol="G4NONE")
    assert total == pytest.approx(0.0)


def test_missing_inputs_return_none() -> None:
    assert symbol_day_banked_pnl_other_sessions(None, symbol="G4X") is None
    # blank symbol: unusable basis -> None (caller falls back to session-local)


def test_blank_symbol_returns_none(db) -> None:
    assert symbol_day_banked_pnl_other_sessions(db, symbol="") is None
