"""Per-BROKER daily-loss caps (operator 2026-06-15: "dapat ang kill switch is by broker").

Covers the safety-invariant matrix from the design+red-team:
  I1 each broker hard-capped off ITS OWN real equity (no None->Coinbase basis bug)
  I2 the global kill switch / aggregate backstop still halts ALL brokers
  I3 EXITS are never blocked by a daily-loss breach
  I5 a Coinbase-sized breach must NOT freeze Robinhood (the literal incident)
  + PnL split-by-broker bucketing, fail-closed, ET-roll auto-clear, reversibility.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models.trading import (
    MomentumAutomationOutcome,
    Trade,
    TradingAutomationSession,
)
from app.services.trading import governance as gov
from app.services.trading.momentum_neural import risk_policy as rp


@pytest.fixture(autouse=True)
def _reset_governance():
    """Each test starts from a clean global + per-broker state."""
    gov.deactivate_kill_switch()
    with gov._per_broker_lock:
        gov._per_broker_daily_loss.clear()
    yield
    gov.deactivate_kill_switch()
    with gov._per_broker_lock:
        gov._per_broker_daily_loss.clear()


@pytest.fixture
def fake_equity(monkeypatch):
    """Force per-broker REAL equity: RH $12,585, CB $2,404 (the operator's truth)."""
    def _eq(execution_family=None, *, prefer_real_equity=False):
        from app.services.trading.execution_family_registry import (
            EXECUTION_FAMILY_ROBINHOOD_SPOT,
            normalize_execution_family,
        )

        ef = normalize_execution_family(execution_family)
        return 12585.0 if ef == EXECUTION_FAMILY_ROBINHOOD_SPOT else 2404.0

    monkeypatch.setattr(rp, "_account_equity_usd", _eq)
    return _eq


def _trade(db, *, user_id, pnl, broker_source, qty=10):
    """A closed Trade today with a deterministic realized PnL."""
    entry = 1000.0  # high enough that a loss never drives exit_price <= 0 (model validates)
    exit_ = entry + (pnl / qty)  # long: (exit-entry)*qty == pnl
    t = Trade(
        user_id=user_id,
        ticker="TST",
        direction="long",
        entry_price=entry,
        exit_price=exit_,
        quantity=qty,
        entry_date=datetime.utcnow() - timedelta(hours=1),
        exit_date=datetime.utcnow(),
        status="closed",
        pnl=pnl,
        broker_source=broker_source,
    )
    db.add(t)
    db.flush()
    return t


_variant_seq = 0


def _momentum(db, *, user_id, pnl, execution_family, symbol="TST"):
    """A momentum session + terminal outcome today with realized PnL (FK: variant)."""
    from app.models.trading import MomentumStrategyVariant

    global _variant_seq
    _variant_seq += 1
    variant = MomentumStrategyVariant(
        family="test_family",
        variant_key=f"pb_{_variant_seq}",
        label="test variant",
        params_json={},
    )
    db.add(variant)
    db.flush()
    sess = TradingAutomationSession(
        user_id=user_id,
        venue="test",
        execution_family=execution_family,
        mode="live",
        symbol=symbol,
        variant_id=variant.id,
        state="live_exited",
    )
    db.add(sess)
    db.flush()
    out = MomentumAutomationOutcome(
        session_id=sess.id,
        user_id=user_id,
        variant_id=variant.id,
        symbol=symbol,
        mode="live",
        execution_family=execution_family,
        terminal_state="exited",
        terminal_at=datetime.utcnow(),
        outcome_class="loss" if pnl < 0 else "win",
        realized_pnl_usd=pnl,
    )
    db.add(out)
    db.flush()
    return out


# ── I1 / equity basis ─────────────────────────────────────────────────
def test_cap_uses_real_equity_per_broker(fake_equity):
    rh_cap, rh_src = gov.per_broker_daily_loss_cap_usd("robinhood_spot")
    cb_cap, cb_src = gov.per_broker_daily_loss_cap_usd("coinbase_spot")
    # pct default 0.015 (settings) * real equity — NOT buying-power*margin.
    assert rh_cap == pytest.approx(0.015 * 12585.0, rel=1e-6)
    assert cb_cap == pytest.approx(0.015 * 2404.0, rel=1e-6)
    assert rh_cap > cb_cap  # the whole point: RH budget >> CB budget
    assert "pct" in rh_src and "pct" in cb_src


def test_cap_fail_closed_when_equity_unknown(monkeypatch):
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: None)
    # usd cap off (default 0) + no equity -> fail-CLOSED floor, never uncapped.
    cap, src = gov.per_broker_daily_loss_cap_usd("robinhood_spot")
    assert cap > 0
    assert src == "usd_failsafe"


# ── PnL split by broker ───────────────────────────────────────────────
def test_realized_pnl_split_by_broker(db, fake_equity):
    uid = None
    _trade(db, user_id=uid, pnl=-48.0, broker_source="robinhood")
    _trade(db, user_id=uid, pnl=-30.0, broker_source="coinbase")
    _trade(db, user_id=uid, pnl=-5.0, broker_source="manual")            # excluded (default)
    _trade(db, user_id=uid, pnl=-99.0, broker_source="reconcile_import")  # always excluded
    _trade(db, user_id=uid, pnl=-7.0, broker_source=None)                # NULL -> robinhood
    _momentum(db, user_id=uid, pnl=-10.0, execution_family="robinhood_spot")
    _momentum(db, user_id=uid, pnl=-6.0, execution_family="coinbase_spot")
    _momentum(db, user_id=uid, pnl=-300.0, execution_family="alpaca_spot")  # paper, excluded
    db.flush()

    by_broker = gov.realized_pnl_today_by_broker(db, uid)
    assert by_broker["robinhood_spot"] == pytest.approx(-48.0 - 7.0 - 10.0)  # -65
    assert by_broker["coinbase_spot"] == pytest.approx(-30.0 - 6.0)          # -36
    # manual / reconcile_import / alpaca never counted


# ── I5: the incident — no trip ────────────────────────────────────────
def test_incident_no_trip(db, fake_equity):
    uid = None
    _trade(db, user_id=uid, pnl=-48.0, broker_source="robinhood")  # RH -48 vs ~$189
    _momentum(db, user_id=uid, pnl=-16.0, execution_family="coinbase_spot")  # CB -16 vs ~$36
    db.flush()
    res = gov.check_per_broker_daily_loss(db, user_id=uid)
    assert res["by_broker"]["robinhood_spot"]["breached"] is False
    assert res["by_broker"]["coinbase_spot"]["breached"] is False
    assert gov.is_kill_switch_active() is False  # global flag stays clean


# ── I5: Coinbase breach must NOT freeze Robinhood ─────────────────────
def test_coinbase_breach_does_not_block_robinhood(db, fake_equity):
    uid = None
    _momentum(db, user_id=uid, pnl=-90.0, execution_family="coinbase_spot")  # CB -90 > $36 cap
    db.flush()
    cb_blocked, _ = gov.broker_daily_loss_breached(db, "coinbase_spot", user_id=uid)
    rh_blocked, _ = gov.broker_daily_loss_breached(db, "robinhood_spot", user_id=uid)
    assert cb_blocked is True
    assert rh_blocked is False
    assert gov.is_broker_daily_loss_blocked("coinbase_spot") is True
    assert gov.is_broker_daily_loss_blocked("robinhood_spot") is False
    assert gov.is_kill_switch_active() is False  # NEVER touches the global flag


def test_robinhood_breach_isolated_and_sticky(db, fake_equity):
    uid = None
    _trade(db, user_id=uid, pnl=-250.0, broker_source="robinhood")  # RH -250 > ~$189
    db.flush()
    rh_blocked, info = gov.broker_daily_loss_breached(db, "robinhood_spot", user_id=uid)
    assert rh_blocked is True
    assert gov.is_broker_daily_loss_blocked("coinbase_spot") is False
    assert gov.is_kill_switch_active() is False
    # sticky: a later winning exit does NOT re-open the budget this day
    _trade(db, user_id=uid, pnl=400.0, broker_source="robinhood")
    db.flush()
    still_blocked, info2 = gov.broker_daily_loss_breached(db, "robinhood_spot", user_id=uid)
    assert still_blocked is True
    assert info2.get("sticky") is True


# ── I2: aggregate backstop trips the TRUE global kill switch ───────────
def test_aggregate_backstop_trips_global(db, fake_equity):
    uid = None
    # RH -150 (< ~$189, not individually breached) + CB -90 (> $36, breached);
    # aggregate -240 > sum-of-caps (~$225) -> backstop trips the global switch.
    _trade(db, user_id=uid, pnl=-150.0, broker_source="robinhood")
    _momentum(db, user_id=uid, pnl=-90.0, execution_family="coinbase_spot")
    db.flush()
    gov.check_per_broker_daily_loss(db, user_id=uid)
    assert gov.is_kill_switch_active() is True
    assert "backstop" in (gov._kill_switch_reason or "")


# ── I3: exits never blocked by a daily-loss breach ────────────────────
def test_exits_never_blocked_on_daily_loss():
    gov.activate_kill_switch("global_daily_loss_breach_backstop_$225")
    assert gov.is_kill_switch_active() is True
    assert gov._kill_switch_halts_exits() is False  # daily-loss never halts exits
    gov.activate_kill_switch("manual_api")
    assert gov._kill_switch_halts_exits() is True   # manual DOES halt exits


# ── new-entry gate semantics + reversibility ──────────────────────────
def test_kill_switch_halts_new_entries_semantics(monkeypatch):
    # per-broker ON: a legacy single-global daily-loss breach does NOT halt new
    # entries globally (handled per-broker); the backstop + manual DO.
    monkeypatch.setattr(gov.settings, "chili_per_broker_daily_loss_enabled", True)
    gov.activate_kill_switch("global_daily_loss_breach_pct_real_equity_$60")
    assert gov.kill_switch_halts_new_entries() is False
    gov.activate_kill_switch("global_daily_loss_breach_backstop_$225")
    assert gov.kill_switch_halts_new_entries() is True
    gov.activate_kill_switch("manual_api")
    assert gov.kill_switch_halts_new_entries() is True
    # reversible: per-broker OFF -> any active kill switch halts (legacy behavior)
    monkeypatch.setattr(gov.settings, "chili_per_broker_daily_loss_enabled", False)
    gov.activate_kill_switch("global_daily_loss_breach_pct_real_equity_$60")
    assert gov.kill_switch_halts_new_entries() is True


# ── ET-roll auto-clear ────────────────────────────────────────────────
def test_et_roll_clears_stale_block():
    from datetime import date

    with gov._per_broker_lock:
        gov._per_broker_daily_loss["coinbase_spot"] = {
            "reason": "x",
            "et_date": date.today() - timedelta(days=1),
            "realized": -90.0,
            "limit": 36.0,
            "set_at": datetime.utcnow(),
        }
    gov.clear_stale_broker_daily_loss_blocks()
    assert gov.is_broker_daily_loss_blocked("coinbase_spot") is False
