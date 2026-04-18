"""Global daily-loss halt (P0.2) — governance kill switch integration.

The existing per-path caps (autotrader $150, momentum $250) only see their
own surface. A mixed drawdown where autotrader loses $120 and momentum
loses $200 (total -$320) was previously invisible to either tripwire.

These tests verify the new governance helpers:
* ``global_realized_pnl_today_et`` aggregates BOTH ``Trade`` (all versions)
  and ``MomentumAutomationOutcome`` rows for today's ET session.
* ``check_daily_loss_breach`` fires the kill switch when the more
  conservative of (usd, pct_of_equity) caps is breached.
* Cross-version summation catches a mixed-path drawdown the v1-filtered
  helper would miss.
* Kill switch is idempotent (no double activation).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models.trading import MomentumAutomationOutcome, Trade
from app.services.trading import governance


def _today_et_utc_noon() -> datetime:
    """A naive UTC datetime that lands inside today's ET session."""
    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et).replace(hour=12, minute=0, second=0, microsecond=0)
    return now_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _reset_governance_state():
    """Each test starts with a clean kill switch."""
    governance.deactivate_kill_switch()
    yield
    governance.deactivate_kill_switch()


def _add_closed_trade(db, *, user_id: int | None, pnl: float, version: str | None = "v1") -> None:
    t = Trade(
        user_id=user_id,
        ticker="AAPL",
        direction="long",
        entry_price=100.0,
        exit_price=100.0 + pnl,  # placeholder; pnl column is what the helper reads
        quantity=1.0,
        entry_date=_today_et_utc_noon() - timedelta(hours=2),
        exit_date=_today_et_utc_noon(),
        status="closed",
        pnl=pnl,
        auto_trader_version=version,
    )
    db.add(t)
    db.commit()


def _add_momentum_outcome(db, *, user_id: int | None, pnl: float) -> None:
    from app.models.trading import MomentumStrategyVariant, TradingAutomationSession

    # Minimal supporting rows (FKs: variant, session).
    variant = MomentumStrategyVariant(
        family="test_family",
        variant_key=f"v_{abs(pnl):.0f}",
        label="test variant",
        params_json={},
    )
    db.add(variant)
    db.flush()
    sess = TradingAutomationSession(
        user_id=user_id,
        symbol="BTC-USD",
        mode="paper",
        variant_id=variant.id,
        state="finished",
    )
    db.add(sess)
    db.flush()
    row = MomentumAutomationOutcome(
        session_id=sess.id,
        user_id=user_id,
        variant_id=variant.id,
        symbol="BTC-USD",
        mode="paper",
        execution_family="coinbase_spot",
        terminal_state="finished",
        terminal_at=_today_et_utc_noon(),
        outcome_class="loss",
        realized_pnl_usd=pnl,
    )
    db.add(row)
    db.commit()


def test_global_pnl_helper_aggregates_trades_and_momentum(db):
    _add_closed_trade(db, user_id=None, pnl=-100.0, version="v1")
    _add_closed_trade(db, user_id=None, pnl=-50.0, version=None)   # non-v1
    _add_momentum_outcome(db, user_id=None, pnl=-75.0)

    result = governance.global_realized_pnl_today_et(db, user_id=None)
    assert result["autotrader_usd"] == pytest.approx(-150.0)
    assert result["momentum_usd"] == pytest.approx(-75.0)
    assert result["total_usd"] == pytest.approx(-225.0)


def test_cross_version_summation_catches_mixed_drawdown(db, monkeypatch):
    """The v1-only helper would see only -$100; the global helper sees all three paths."""
    # Split the drawdown so neither path-local cap is tripped:
    _add_closed_trade(db, user_id=None, pnl=-100.0, version="v1")   # under $150 autotrader cap
    _add_momentum_outcome(db, user_id=None, pnl=-200.0)             # under $250 momentum cap
    # Global total = -$300

    monkeypatch.setattr(
        governance.settings,
        "chili_global_max_daily_loss_usd",
        250.0,
        raising=False,
    )
    monkeypatch.setattr(
        governance.settings,
        "chili_global_max_daily_loss_pct_of_equity",
        0.0,
        raising=False,
    )

    res = governance.check_daily_loss_breach(db, user_id=None)
    assert res["breached"] is True
    assert res["source"] == "usd"
    assert res["limit_usd"] == pytest.approx(250.0)
    assert res["realized_usd"] == pytest.approx(-300.0)
    assert governance.is_kill_switch_active() is True
    assert "global_daily_loss_breach_usd" in governance.get_kill_switch_status()["reason"]


def test_usd_cap_breach_activates_kill_switch(db, monkeypatch):
    monkeypatch.setattr(
        governance.settings, "chili_global_max_daily_loss_usd", 300.0, raising=False
    )
    monkeypatch.setattr(
        governance.settings, "chili_global_max_daily_loss_pct_of_equity", 0.0, raising=False
    )
    _add_closed_trade(db, user_id=None, pnl=-301.0, version="v1")

    res = governance.check_daily_loss_breach(db, user_id=None)

    assert res["breached"] is True
    assert res["source"] == "usd"
    assert governance.is_kill_switch_active() is True


def test_pct_of_equity_wins_when_more_conservative(db, monkeypatch):
    # USD cap is $300; pct cap is 2% of $4000 equity = $80. $80 is stricter.
    monkeypatch.setattr(
        governance.settings, "chili_global_max_daily_loss_usd", 300.0, raising=False
    )
    monkeypatch.setattr(
        governance.settings, "chili_global_max_daily_loss_pct_of_equity", 0.02, raising=False
    )
    _add_closed_trade(db, user_id=None, pnl=-100.0, version="v1")

    res = governance.check_daily_loss_breach(db, user_id=None, equity_usd=4000.0)

    assert res["breached"] is True
    assert res["source"] == "pct_equity"
    assert res["limit_usd"] == pytest.approx(80.0)
    assert governance.is_kill_switch_active() is True


def test_within_caps_does_not_trip(db, monkeypatch):
    monkeypatch.setattr(
        governance.settings, "chili_global_max_daily_loss_usd", 300.0, raising=False
    )
    monkeypatch.setattr(
        governance.settings, "chili_global_max_daily_loss_pct_of_equity", 0.02, raising=False
    )
    _add_closed_trade(db, user_id=None, pnl=-50.0, version="v1")

    res = governance.check_daily_loss_breach(db, user_id=None, equity_usd=10_000.0)

    assert res["breached"] is False
    assert governance.is_kill_switch_active() is False


def test_activate_false_does_not_trip_kill_switch(db, monkeypatch):
    """Pre-entry evaluations must be able to check WITHOUT mutating state."""
    monkeypatch.setattr(
        governance.settings, "chili_global_max_daily_loss_usd", 100.0, raising=False
    )
    monkeypatch.setattr(
        governance.settings, "chili_global_max_daily_loss_pct_of_equity", 0.0, raising=False
    )
    _add_closed_trade(db, user_id=None, pnl=-500.0, version="v1")

    res = governance.check_daily_loss_breach(db, user_id=None, activate=False)

    assert res["breached"] is True
    assert governance.is_kill_switch_active() is False


def test_no_limits_configured_returns_not_breached(db, monkeypatch):
    monkeypatch.setattr(
        governance.settings, "chili_global_max_daily_loss_usd", 0.0, raising=False
    )
    monkeypatch.setattr(
        governance.settings, "chili_global_max_daily_loss_pct_of_equity", 0.0, raising=False
    )
    _add_closed_trade(db, user_id=None, pnl=-1_000.0, version="v1")

    res = governance.check_daily_loss_breach(db, user_id=None)

    assert res["breached"] is False
    assert res["source"] == "none"
    assert governance.is_kill_switch_active() is False


def test_kill_switch_not_re_activated_when_already_active(db, monkeypatch):
    """Idempotency: a second breach check should not re-fire activation."""
    monkeypatch.setattr(
        governance.settings, "chili_global_max_daily_loss_usd", 100.0, raising=False
    )
    monkeypatch.setattr(
        governance.settings, "chili_global_max_daily_loss_pct_of_equity", 0.0, raising=False
    )
    _add_closed_trade(db, user_id=None, pnl=-200.0, version="v1")

    res1 = governance.check_daily_loss_breach(db, user_id=None)
    assert res1["breached"] is True
    reason1 = governance.get_kill_switch_status()["reason"]

    # Second call should see breach still but not clobber the reason,
    # because `activate_kill_switch` is guarded by `is_kill_switch_active`.
    res2 = governance.check_daily_loss_breach(db, user_id=None)
    assert res2["breached"] is True
    assert governance.is_kill_switch_active() is True
    assert governance.get_kill_switch_status()["reason"] == reason1
