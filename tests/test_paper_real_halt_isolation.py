"""Paper-account outcomes must never halt real-capital entry rails."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import models
from app.models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import risk_evaluator, risk_policy


_AS_OF = datetime(2026, 7, 14, 16, 0, tzinfo=timezone.utc)


def _outcome(
    db,
    *,
    user_id: int,
    variant_id: int,
    family: str,
    symbol: str,
    pnl: float,
    terminal_at: datetime,
    mode: str = "live",
    broker_reconciled_at: datetime | None = None,
) -> None:
    session = TradingAutomationSession(
        user_id=user_id,
        venue="alpaca" if family in {"alpaca_spot", "alpaca_short"} else "robinhood",
        execution_family=family,
        mode=mode,
        symbol=symbol,
        variant_id=variant_id,
        state="live_finished",
        ended_at=terminal_at,
        risk_snapshot_json={},
    )
    db.add(session)
    db.flush()
    db.add(
        MomentumAutomationOutcome(
            session_id=session.id,
            user_id=user_id,
            variant_id=variant_id,
            symbol=symbol,
            mode=mode,
            execution_family=family,
            terminal_state="live_finished",
            terminal_at=terminal_at,
            outcome_class="small_win" if pnl >= 0 else "small_loss",
            realized_pnl_usd=pnl,
            return_bps=pnl,
            regime_snapshot_json={},
            entry_regime_snapshot_json={},
            exit_regime_snapshot_json={},
            readiness_snapshot_json={},
            admission_snapshot_json={},
            governance_context_json={},
            evidence_weight=1.0,
            contributes_to_evolution=True,
            broker_recon_status="reconciled",
            broker_realized_pnl_usd=pnl,
            broker_return_bps=pnl,
            broker_win=pnl > 0,
            broker_reconciled_at=broker_reconciled_at or terminal_at,
            broker_recon_detail_json={"source": "isolation_test"},
        )
    )
    db.flush()


def test_alpaca_paper_giveback_halts_paper_but_not_real_rails(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = models.User(name="paper-real-giveback-isolation")
    variant = MomentumStrategyVariant(
        family="paper-real-giveback-isolation",
        variant_key="paper_real_giveback_isolation_v1",
        label="paper-real-giveback-isolation",
        params_json={},
    )
    db.add_all([user, variant])
    db.flush()
    start, _end = risk_policy._et_day_bounds_utc(as_of_utc=_AS_OF)

    # The real rail is only +$10 and has no giveback.  Alpaca paper separately
    # peaks at +$200 and gives back to +$40, which should halt paper only.
    _outcome(
        db,
        user_id=user.id,
        variant_id=variant.id,
        family="robinhood_spot",
        symbol="REAL",
        pnl=10.0,
        terminal_at=start + timedelta(hours=1),
    )
    _outcome(
        db,
        user_id=user.id,
        variant_id=variant.id,
        family="alpaca_spot",
        symbol="PAPR",
        pnl=200.0,
        terminal_at=start + timedelta(hours=2),
    )
    _outcome(
        db,
        user_id=user.id,
        variant_id=variant.id,
        family="alpaca_short",
        symbol="PAPS",
        pnl=-160.0,
        terminal_at=start + timedelta(hours=3),
    )
    db.commit()

    monkeypatch.setattr(
        risk_evaluator,
        "equity_relative_daily_loss_cap",
        lambda *_args, **_kwargs: 100.0,
    )
    monkeypatch.setattr(
        risk_evaluator.settings,
        "chili_momentum_profit_giveback_fraction",
        0.5,
        raising=False,
    )

    real = risk_evaluator.evaluate_profit_giveback_halt(
        db,
        user_id=user.id,
        execution_family="robinhood_spot",
        as_of_utc=_AS_OF,
    )
    paper = risk_evaluator.evaluate_profit_giveback_halt(
        db,
        user_id=user.id,
        execution_family="alpaca_spot",
        as_of_utc=_AS_OF,
    )

    assert real["halted"] is False
    assert real["peak_pnl_usd"] == pytest.approx(10.0)
    assert real["daily_pnl_usd"] == pytest.approx(10.0)
    assert paper["halted"] is True
    assert paper["peak_pnl_usd"] == pytest.approx(200.0)
    assert paper["daily_pnl_usd"] == pytest.approx(40.0)


def test_real_broker_families_and_non_live_rows_are_isolated(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = models.User(name="real-family-isolation")
    variant = MomentumStrategyVariant(
        family="real-family-isolation",
        variant_key="real_family_isolation_v1",
        label="real-family-isolation",
        params_json={},
    )
    db.add_all([user, variant])
    db.flush()
    start, _end = risk_policy._et_day_bounds_utc(as_of_utc=_AS_OF)
    _outcome(
        db,
        user_id=user.id,
        variant_id=variant.id,
        family="robinhood_spot",
        symbol="RH",
        pnl=10.0,
        terminal_at=start + timedelta(hours=1),
    )
    _outcome(
        db,
        user_id=user.id,
        variant_id=variant.id,
        family="coinbase_spot",
        symbol="BTC-USD",
        pnl=200.0,
        terminal_at=start + timedelta(hours=2),
    )
    _outcome(
        db,
        user_id=user.id,
        variant_id=variant.id,
        family="coinbase_spot",
        symbol="ETH-USD",
        pnl=-160.0,
        terminal_at=start + timedelta(hours=3),
    )
    _outcome(
        db,
        user_id=user.id,
        variant_id=variant.id,
        family="robinhood_spot",
        symbol="PAPER_NOISE",
        pnl=-999.0,
        terminal_at=start + timedelta(hours=4),
        mode="paper",
    )
    db.commit()
    monkeypatch.setattr(
        risk_evaluator, "equity_relative_daily_loss_cap", lambda *_a, **_k: 100.0
    )
    monkeypatch.setattr(
        risk_evaluator.settings,
        "chili_momentum_profit_giveback_fraction",
        0.5,
        raising=False,
    )

    robinhood = risk_evaluator.evaluate_profit_giveback_halt(
        db,
        user_id=user.id,
        execution_family="robinhood_spot",
        as_of_utc=_AS_OF,
    )
    coinbase = risk_evaluator.evaluate_profit_giveback_halt(
        db,
        user_id=user.id,
        execution_family="coinbase_spot",
        as_of_utc=_AS_OF,
    )

    assert robinhood["halted"] is False
    assert robinhood["daily_pnl_usd"] == pytest.approx(10.0)
    assert coinbase["halted"] is True
    assert coinbase["peak_pnl_usd"] == pytest.approx(200.0)
    assert coinbase["daily_pnl_usd"] == pytest.approx(40.0)


def test_future_broker_reconciliation_is_not_visible_at_historical_frontier(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = models.User(name="broker-label-frontier")
    variant = MomentumStrategyVariant(
        family="broker-label-frontier",
        variant_key="broker_label_frontier_v1",
        label="broker-label-frontier",
        params_json={},
    )
    db.add_all([user, variant])
    db.flush()
    start, _end = risk_policy._et_day_bounds_utc(as_of_utc=_AS_OF)
    reconciliation_at = _AS_OF + timedelta(hours=1)
    _outcome(
        db,
        user_id=user.id,
        variant_id=variant.id,
        family="robinhood_spot",
        symbol="LATE_LABEL",
        pnl=100.0,
        terminal_at=start + timedelta(hours=1),
        broker_reconciled_at=reconciliation_at.replace(tzinfo=None),
    )
    db.commit()
    monkeypatch.setattr(
        risk_evaluator.settings,
        "chili_momentum_broker_truth_label_enabled",
        True,
        raising=False,
    )

    before = risk_evaluator._daily_realized_pnl(
        db,
        user.id,
        execution_family="robinhood_spot",
        as_of_utc=_AS_OF,
    )
    after = risk_evaluator._daily_realized_pnl(
        db,
        user.id,
        execution_family="robinhood_spot",
        as_of_utc=reconciliation_at + timedelta(seconds=1),
    )

    assert before == pytest.approx(0.0)
    assert after == pytest.approx(100.0)
