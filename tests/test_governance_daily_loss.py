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
from types import SimpleNamespace
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
        exit_price=100.0,  # placeholder; pnl column is what the helper reads
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


class _FakeQuery:
    def __init__(self, *, rows=None, scalar_value=0.0):
        self._rows = list(rows or [])
        self._scalar_value = scalar_value

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self._rows)

    def scalar(self):
        return self._scalar_value


class _FakeGovernanceDb:
    def __init__(self, trades, *, momentum_total=0.0):
        self.trades = list(trades)
        self.momentum_total = momentum_total

    def query(self, model):
        if model is Trade:
            return _FakeQuery(rows=self.trades)
        return _FakeQuery(scalar_value=self.momentum_total)


def test_global_pnl_helper_includes_live_partial_option_leg_without_db():
    trade = SimpleNamespace(
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
        entry_price=1.25,
        quantity=1.0,
        pnl=-10.0,
        direction="long",
        partial_taken=True,
        partial_taken_qty=1.0,
        partial_taken_price=1.45,
    )

    result = governance.global_realized_pnl_today_et(
        _FakeGovernanceDb([trade], momentum_total=-2.0),
        user_id=None,
    )

    assert result["autotrader_usd"] == pytest.approx(10.0)
    assert result["momentum_usd"] == pytest.approx(-2.0)
    assert result["total_usd"] == pytest.approx(8.0)


def test_paper_profit_helpers_use_partial_aware_option_outcome():
    paper_trade = SimpleNamespace(
        signal_json={"asset_type": "options"},
        entry_price=1.25,
        quantity=1.0,
        pnl=-10.0,
        pnl_pct=-8.0,
        direction="long",
        partial_taken=True,
        partial_taken_qty=1.0,
        partial_taken_price=1.45,
    )

    assert governance._paper_directional_win(paper_trade) is True
    assert governance._paper_realized_pnl_with_raw_fallback(paper_trade) == pytest.approx(10.0)


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


def test_daily_loss_basis_resolves_off_agentic_account(db, monkeypatch):
    """BASIS FIX (2026-06-22): with no explicit equity, check_daily_loss_breach must size
    the cap off the AGENTIC account (apply_margin_multiple=False = unlevered BP), NOT the
    legacy None->Coinbase default — else the $13.7k lane re-freezes at the spurious $55 cap.
    Regression guard: the prior stubs swallowed all args, so a revert would pass silently."""
    from app.services.trading.momentum_neural import risk_policy as rp
    from app.services.trading.execution_family_registry import EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP

    calls: dict = {}

    def _rec(execution_family=None, *, apply_margin_multiple=True):
        calls["execution_family"] = execution_family
        calls["apply_margin_multiple"] = apply_margin_multiple
        return 10_000.0

    monkeypatch.setattr(rp, "_account_equity_usd", _rec, raising=False)
    monkeypatch.setattr(governance.settings, "chili_global_max_daily_loss_usd", 0.0, raising=False)
    monkeypatch.setattr(governance.settings, "chili_global_max_daily_loss_pct_of_equity", 0.05, raising=False)
    _add_closed_trade(db, user_id=None, pnl=-30.0, version="v1")  # within the $500 cap

    res = governance.check_daily_loss_breach(db, user_id=None)  # equity_usd unset -> auto-resolve

    assert calls.get("execution_family") == EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP  # right account
    assert calls.get("apply_margin_multiple") is False                              # unlevered BP
    assert res["source"] == "pct_equity"
    assert res["limit_usd"] == pytest.approx(500.0)   # 0.05 * 10_000 (NOT ~$55 off the wrong basis)
    assert res["breached"] is False                   # -$30 within $500


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


def test_transient_db_fail_closed_clears_after_successful_empty_poll(monkeypatch):
    monkeypatch.setattr(governance.settings, "chili_kill_switch_db_poll_enabled", True, raising=False)
    monkeypatch.setattr(governance.settings, "chili_kill_switch_db_poll_interval_s", 9999.0, raising=False)
    monkeypatch.setattr(governance.settings, "chili_kill_switch_db_fail_closed", True, raising=False)

    state = {"mode": "down"}

    def _fetch():
        if state["mode"] == "down":
            raise RuntimeError("postgres starting up")
        return None

    monkeypatch.setattr(governance, "_fetch_latest_kill_switch_state_from_db", _fetch)
    governance._refresh_kill_switch_from_db_if_due(force=True)

    status = governance.get_kill_switch_status()
    assert status["active"] is True
    assert status["reason"] == "kill_switch_db_read_failed:RuntimeError"
    assert status["transient_db_fail_closed"] is True

    state["mode"] = "up"
    governance._refresh_kill_switch_from_db_if_due(force=True)

    status = governance.get_kill_switch_status()
    assert status["active"] is False
    assert status["reason"] is None
    assert status["db_error"] is None
    assert status["transient_db_fail_closed"] is False


def test_transient_db_fail_closed_preserves_manual_halt_reason(monkeypatch):
    monkeypatch.setattr(governance.settings, "chili_kill_switch_db_poll_enabled", True, raising=False)
    monkeypatch.setattr(governance.settings, "chili_kill_switch_db_poll_interval_s", 9999.0, raising=False)
    monkeypatch.setattr(governance.settings, "chili_kill_switch_db_fail_closed", True, raising=False)
    monkeypatch.setattr(
        governance,
        "_fetch_latest_kill_switch_state_from_db",
        lambda: (_ for _ in ()).throw(RuntimeError("postgres starting up")),
    )

    governance.activate_kill_switch("manual_halt")
    governance._refresh_kill_switch_from_db_if_due(force=True)

    status = governance.get_kill_switch_status()
    assert status["active"] is True
    assert status["reason"] == "manual_halt"
    assert status["db_error"] is not None
    assert status["transient_db_fail_closed"] is False


def test_kill_switch_session_refresh_uses_provided_db_session(monkeypatch):
    monkeypatch.setattr(governance.settings, "chili_kill_switch_db_poll_enabled", True, raising=False)
    monkeypatch.setattr(governance.settings, "chili_kill_switch_db_poll_interval_s", 0.0, raising=False)

    expected_db = object()
    seen: list[object] = []

    def _fetch(sess):
        seen.append(sess)
        return (True, "session_scoped_halt", datetime.now(timezone.utc))

    monkeypatch.setattr(governance, "_fetch_latest_kill_switch_state", _fetch)
    governance._apply_kill_switch_state(active=False, reason=None, set_at=None)

    assert governance.is_kill_switch_active_for_session(expected_db) is True

    monkeypatch.setattr(governance.settings, "chili_kill_switch_db_poll_interval_s", 9999.0, raising=False)
    status = governance.get_kill_switch_status()
    assert seen == [expected_db]
    assert status["active"] is True
    assert status["reason"] == "session_scoped_halt"
