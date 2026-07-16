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
    with gov._alpaca_day_change_lock:
        gov._alpaca_day_change_cache.update(ts=0.0, realized=None, meta={})
    yield
    gov.deactivate_kill_switch()
    with gov._per_broker_lock:
        gov._per_broker_daily_loss.clear()
    with gov._alpaca_day_change_lock:
        gov._alpaca_day_change_cache.update(ts=0.0, realized=None, meta={})


@pytest.fixture
def fake_equity(monkeypatch):
    """Force deterministic per-broker account equity: RH $13,424, CB $1,994."""
    def _eq(
        execution_family=None,
        *,
        apply_margin_multiple=True,
        prefer_equity=False,
        prefer_cash_value=False,
    ):
        from app.services.trading.execution_family_registry import (
            EXECUTION_FAMILY_ROBINHOOD_SPOT,
            normalize_execution_family,
        )

        ef = normalize_execution_family(execution_family)
        return 13424.0 if ef == EXECUTION_FAMILY_ROBINHOOD_SPOT else 1994.0

    monkeypatch.setattr(rp, "_account_equity_usd", _eq)
    # Pin the daily-loss pct the breach amounts below are calibrated to (per-broker caps =
    # pct x BP -> RH ~$201, CB ~$30). The GLOBAL default moved 1.5% -> 5% (#788), which
    # tripled the live caps; pin it here so these breach-LOGIC tests stay deterministic and
    # decoupled from the default. (Production correctly uses 5%.)
    from app.services.trading import governance as _gov
    monkeypatch.setattr(_gov.settings, "chili_global_max_daily_loss_pct_of_equity", 0.015, raising=False)
    monkeypatch.setattr(
        _gov,
        "_alpaca_account_daily_change_usd",
        lambda: (0.0, {"data_source": "alpaca_account_equity_delta"}),
    )
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


# ── I1 / account-equity basis ─────────────────────────────────────────
def test_cap_uses_account_equity_per_broker(fake_equity):
    rh_cap, rh_src = gov.per_broker_daily_loss_cap_usd("robinhood_spot")
    cb_cap, cb_src = gov.per_broker_daily_loss_cap_usd("coinbase_spot")
    # pct default 0.015 (settings) * total account equity — never margin buying power.
    assert rh_cap == pytest.approx(0.015 * 13424.0, rel=1e-6)  # ~$201
    assert cb_cap == pytest.approx(0.015 * 1994.0, rel=1e-6)   # ~$30
    assert rh_cap > cb_cap  # the whole point: RH budget >> CB budget
    assert rh_src == "pct_cash_value" and cb_src == "pct_cash_value"


def test_cap_fail_closed_when_equity_unknown(monkeypatch):
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: None)
    # usd cap off (default 0) + no equity -> fail-CLOSED floor, never uncapped.
    cap, src = gov.per_broker_daily_loss_cap_usd("robinhood_spot")
    assert cap > 0
    assert src == "usd_failsafe"


def test_alpaca_families_are_preserved_as_paper_brokers():
    assert gov._normalize_real_family("alpaca_spot") == "alpaca_spot"
    assert gov._normalize_real_family("alpaca_short") == "alpaca_short"
    assert set(gov.PAPER_DAILY_LOSS_FAMILIES) == {"alpaca_spot", "alpaca_short"}
    assert set(gov.BROKER_DAILY_LOSS_FAMILIES) == (
        set(gov.REAL_DAILY_LOSS_FAMILIES) | set(gov.PAPER_DAILY_LOSS_FAMILIES)
    )
    assert not (set(gov.REAL_DAILY_LOSS_FAMILIES) & set(gov.PAPER_DAILY_LOSS_FAMILIES))


def test_alpaca_daily_change_comes_from_equity_minus_last_equity(monkeypatch):
    from app.services.trading.venue import alpaca_spot

    class _Adapter:
        def get_account_snapshot(self):
            return {"ok": True, "equity": 71_876.85, "last_equity": 73_588.07}

    monkeypatch.setattr(alpaca_spot, "AlpacaSpotAdapter", _Adapter)
    realized, info = gov._alpaca_account_daily_change_usd()

    assert realized == pytest.approx(-1_711.22)
    assert info["data_source"] == "alpaca_account_equity_delta"
    assert info["equity"] == pytest.approx(71_876.85)
    assert info["last_equity"] == pytest.approx(73_588.07)


def test_live_posture_never_reuses_cached_paper_day_change(monkeypatch):
    monkeypatch.setattr(gov.settings, "chili_alpaca_paper", False, raising=False)
    with gov._alpaca_day_change_lock:
        gov._alpaca_day_change_cache.update(
            ts=10**12,
            realized=123.45,
            meta={"data_source": "cached_paper_value"},
        )

    realized, info = gov._alpaca_account_daily_change_usd()

    assert realized is None
    assert info["error"] == "alpaca_live_posture_quarantined"


def test_alpaca_literal_admission_force_refresh_bypasses_healthy_cache(
    monkeypatch,
):
    from app.services.trading.venue import alpaca_spot

    snapshots = iter(
        [
            {"ok": True, "equity": 100_000.0, "last_equity": 100_000.0},
            {"ok": True, "equity": 99_740.0, "last_equity": 100_000.0},
        ]
    )
    calls = {"count": 0}

    class _Adapter:
        def get_account_snapshot(self):
            calls["count"] += 1
            return next(snapshots)

    monkeypatch.setattr(alpaca_spot, "AlpacaSpotAdapter", _Adapter)
    monkeypatch.setattr(
        gov,
        "_per_broker_daily_loss_cap_detail",
        lambda _fam: (250.0, "alpaca_momentum_fixed_usd_clamp", {}),
    )

    cached_healthy, _ = gov._alpaca_account_daily_change_usd()
    reused_healthy, _ = gov._alpaca_account_daily_change_usd()
    blocked, info = gov.broker_daily_loss_breached(
        None,
        "alpaca_spot",
        force_refresh=True,
    )

    assert cached_healthy == pytest.approx(0.0)
    assert reused_healthy == pytest.approx(0.0)
    assert calls["count"] == 2
    assert blocked is True
    assert info["realized"] == pytest.approx(-260.0)
    assert info["broker_snapshot_cache_bypassed"] is True
    assert gov.is_broker_daily_loss_blocked("alpaca_spot") is True


def test_alpaca_literal_force_refresh_failure_never_falls_back_to_healthy_cache(
    monkeypatch,
):
    from app.services.trading.venue import alpaca_spot

    snapshots = iter(
        [
            {"ok": True, "equity": 100_000.0, "last_equity": 100_000.0},
            {"ok": False, "error": "offline"},
        ]
    )

    class _Adapter:
        def get_account_snapshot(self):
            return next(snapshots)

    monkeypatch.setattr(alpaca_spot, "AlpacaSpotAdapter", _Adapter)
    monkeypatch.setattr(
        gov,
        "_per_broker_daily_loss_cap_detail",
        lambda _fam: (250.0, "alpaca_momentum_fixed_usd_clamp", {}),
    )

    healthy, _ = gov._alpaca_account_daily_change_usd()
    blocked, info = gov.broker_daily_loss_breached(
        None,
        "alpaca_spot",
        force_refresh=True,
    )

    assert healthy == pytest.approx(0.0)
    assert blocked is True
    assert info["transient"] is True
    assert info["error"] == "offline"
    assert info["broker_snapshot_cache_bypassed"] is True
    assert gov.is_broker_daily_loss_blocked("alpaca_spot") is False


def test_alpaca_snapshot_unavailable_is_transient_fail_closed(monkeypatch):
    monkeypatch.setattr(gov.settings, "chili_per_broker_daily_loss_enabled", True)
    monkeypatch.setattr(
        gov,
        "_per_broker_daily_loss_cap_detail",
        lambda _fam: (300.0, "usd", {"selected_cap_usd": 300.0}),
    )
    monkeypatch.setattr(
        gov,
        "_alpaca_account_daily_change_usd",
        lambda: (None, {"data_source": "alpaca_account_equity_delta", "error": "offline"}),
    )

    blocked, info = gov.broker_daily_loss_breached(object(), "alpaca_spot")

    assert blocked is True
    assert info["transient"] is True
    assert info["reason"] == "alpaca_account_daily_change_unavailable"
    assert gov.is_broker_daily_loss_blocked("alpaca_spot") is False


def test_alpaca_observed_cap_breach_becomes_sticky(monkeypatch):
    monkeypatch.setattr(gov.settings, "chili_per_broker_daily_loss_enabled", True)
    monkeypatch.setattr(
        gov,
        "_per_broker_daily_loss_cap_detail",
        lambda _fam: (300.0, "usd", {"selected_cap_usd": 300.0}),
    )
    observations = iter(
        [
            (-301.0, {"data_source": "alpaca_account_equity_delta"}),
            (+100.0, {"data_source": "alpaca_account_equity_delta"}),
        ]
    )
    monkeypatch.setattr(gov, "_alpaca_account_daily_change_usd", lambda: next(observations))

    breached, first = gov.broker_daily_loss_breached(object(), "alpaca_spot")
    recovered, second = gov.broker_daily_loss_breached(object(), "alpaca_spot")

    assert breached is True
    assert first["realized"] == pytest.approx(-301.0)
    assert recovered is True
    assert second["sticky"] is True
    assert second["realized"] == pytest.approx(-301.0)
    assert second["cap_detail"]["selected_cap_usd"] == pytest.approx(300.0)


def test_alpaca_broker_truth_stays_mandatory_when_generic_flag_is_off(monkeypatch):
    monkeypatch.setattr(gov.settings, "chili_per_broker_daily_loss_enabled", False)
    monkeypatch.setattr(
        gov,
        "_per_broker_daily_loss_cap_detail",
        lambda _fam: (250.0, "alpaca_momentum_fixed_usd_clamp", {}),
    )
    monkeypatch.setattr(
        gov,
        "_alpaca_account_daily_change_usd",
        lambda: (-300.0, {"data_source": "alpaca_account_equity_delta"}),
    )

    blocked, info = gov.broker_daily_loss_breached(object(), "alpaca_spot")

    assert blocked is True
    assert info["realized"] == pytest.approx(-300.0)
    assert info.get("disabled") is not True
    assert gov.is_broker_daily_loss_blocked("alpaca_spot") is True
    # The same rollout flag still disables real-family broker-local accounting.
    real_blocked, real_info = gov.broker_daily_loss_breached(
        object(), "coinbase_spot"
    )
    assert real_blocked is False
    assert real_info["disabled"] is True


def test_alpaca_read_only_peek_uses_broker_truth_when_generic_flag_is_off(monkeypatch):
    monkeypatch.setattr(gov.settings, "chili_per_broker_daily_loss_enabled", False)
    monkeypatch.setattr(
        gov,
        "_per_broker_daily_loss_cap_detail",
        lambda _fam: (250.0, "alpaca_momentum_fixed_usd_clamp", {}),
    )
    monkeypatch.setattr(
        gov,
        "_alpaca_account_daily_change_usd",
        lambda: (None, {"data_source": "alpaca_account_equity_delta", "error": "offline"}),
    )

    blocked, info = gov._peek_broker_breach(object(), "alpaca_spot")

    assert blocked is True
    assert info["transient"] is True
    assert info["reason"] == "alpaca_account_daily_change_unavailable"


def test_alpaca_large_account_uses_adaptive_equity_fraction(monkeypatch):
    monkeypatch.setattr(gov.settings, "chili_per_broker_daily_loss_enabled", True)
    monkeypatch.setattr(gov.settings, "chili_global_max_daily_loss_usd", 0.0)
    monkeypatch.setattr(gov.settings, "chili_global_max_daily_loss_pct_of_equity", 0.05)
    monkeypatch.setattr(
        gov.settings,
        "chili_momentum_risk_daily_loss_fraction_of_equity",
        0.05,
        raising=False,
    )
    monkeypatch.setattr(gov.settings, "chili_momentum_risk_max_daily_loss_usd", 250.0)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: 71_876.85)
    monkeypatch.setattr(
        gov,
        "_alpaca_account_daily_change_usd",
        lambda: (-469.0, {"data_source": "alpaca_account_equity_delta"}),
    )

    blocked, info = gov.broker_daily_loss_breached(object(), "alpaca_spot")

    assert blocked is False
    assert info["realized"] == pytest.approx(-469.0)
    assert info["cap"] == pytest.approx(3_593.8425)
    assert info["source"] == "pct_cash_value"
    assert info["cap_detail"]["broker_equity_cap_usd"] == pytest.approx(3_593.8425)
    assert "momentum_fixed_cap_usd" not in info["cap_detail"]
    assert gov.is_broker_daily_loss_blocked("alpaca_spot") is False


@pytest.mark.parametrize("configured", [0.0, float("nan"), 1_000.0])
def test_alpaca_fixed_daily_loss_setting_does_not_override_equity_fraction(
    monkeypatch, configured
):
    monkeypatch.setattr(gov.settings, "chili_global_max_daily_loss_usd", 0.0)
    monkeypatch.setattr(gov.settings, "chili_global_max_daily_loss_pct_of_equity", 0.05)
    monkeypatch.setattr(
        gov.settings,
        "chili_momentum_risk_daily_loss_fraction_of_equity",
        0.05,
        raising=False,
    )
    monkeypatch.setattr(
        gov.settings,
        "chili_momentum_risk_max_daily_loss_usd",
        configured,
    )
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: 100_000.0)

    cap, source = gov.per_broker_daily_loss_cap_usd("alpaca_spot")

    assert cap == pytest.approx(5_000.0)
    assert source == "pct_cash_value"


def test_alpaca_fixed_daily_loss_setting_cannot_lower_adaptive_budget(monkeypatch):
    monkeypatch.setattr(gov.settings, "chili_global_max_daily_loss_usd", 0.0)
    monkeypatch.setattr(gov.settings, "chili_global_max_daily_loss_pct_of_equity", 0.05)
    monkeypatch.setattr(
        gov.settings,
        "chili_momentum_risk_daily_loss_fraction_of_equity",
        0.05,
        raising=False,
    )
    monkeypatch.setattr(gov.settings, "chili_momentum_risk_max_daily_loss_usd", 125.0)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: 100_000.0)

    cap, source = gov.per_broker_daily_loss_cap_usd("alpaca_spot")

    assert cap == pytest.approx(5_000.0)
    assert source == "pct_cash_value"


def test_positive_alpaca_day_change_stays_open(monkeypatch):
    monkeypatch.setattr(gov.settings, "chili_per_broker_daily_loss_enabled", True)
    monkeypatch.setattr(
        gov,
        "_per_broker_daily_loss_cap_detail",
        lambda _fam: (250.0, "alpaca_momentum_fixed_usd_clamp", {}),
    )
    monkeypatch.setattr(
        gov,
        "_alpaca_account_daily_change_usd",
        lambda: (+100.0, {"data_source": "alpaca_account_equity_delta"}),
    )

    blocked, info = gov.broker_daily_loss_breached(object(), "alpaca_spot")

    assert blocked is False
    assert info["realized"] == pytest.approx(100.0)
    assert info["transient"] is False
    assert gov.is_broker_daily_loss_blocked("alpaca_spot") is False


def test_paper_brokers_checked_but_excluded_from_real_global_backstop(monkeypatch):
    monkeypatch.setattr(gov.settings, "chili_per_broker_aggregate_backstop_mult", 1.0)
    monkeypatch.setattr(gov.settings, "chili_kill_switch_db_poll_enabled", False)
    calls: list[str] = []

    def _gate(_db, family, *, user_id=None):
        calls.append(family)
        is_paper = family in gov.PAPER_DAILY_LOSS_FAMILIES
        return is_paper, {
            "family": family,
            "realized": -10_000.0 if is_paper else -10.0,
            "cap": 1.0 if is_paper else 100.0,
            "source": "test",
            "sticky": False,
        }

    monkeypatch.setattr(gov, "broker_daily_loss_breached", _gate)
    result = gov.check_per_broker_daily_loss(object(), activate=True)

    assert tuple(calls) == gov.BROKER_DAILY_LOSS_FAMILIES
    assert set(result["by_broker"]) == set(gov.BROKER_DAILY_LOSS_FAMILIES)
    assert result["aggregate_realized"] == pytest.approx(-30.0)
    assert result["aggregate_cap"] == pytest.approx(300.0)
    assert gov.is_kill_switch_active() is False


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
    _momentum(db, user_id=uid, pnl=-300.0, execution_family="alpaca_spot")
    _momentum(db, user_id=uid, pnl=-200.0, execution_family="alpaca_short")
    db.flush()

    by_broker = gov.realized_pnl_today_by_broker(db, uid)
    assert by_broker["robinhood_spot"] == pytest.approx(-48.0 - 7.0 - 10.0)  # -65
    assert by_broker["coinbase_spot"] == pytest.approx(-30.0 - 6.0)          # -36
    assert by_broker["alpaca_spot"] == pytest.approx(-300.0)
    assert by_broker["alpaca_short"] == pytest.approx(-200.0)
    # Paper rows are diagnostic only; they remain excluded from REAL global PnL.
    global_pnl = gov.global_realized_pnl_today_et(db, uid)
    assert global_pnl["momentum_usd"] == pytest.approx(-10.0 - 6.0)


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
    # RH -190 (< ~$201, not individually breached) + CB -90 (> ~$30, breached);
    # aggregate -280 exceeds all three real-family caps (~$261, including the
    # unused agentic rail), so the catastrophic backstop trips globally.
    _trade(db, user_id=uid, pnl=-190.0, broker_source="robinhood")
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


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_real_broker_nonfinite_ledger_is_transient_fail_closed_without_sticky(
    monkeypatch: pytest.MonkeyPatch,
    bad: float,
) -> None:
    monkeypatch.setattr(gov.settings, "chili_per_broker_daily_loss_enabled", True)
    monkeypatch.setattr(
        gov,
        "_per_broker_daily_loss_cap_detail",
        lambda _fam: (100.0, "test_cap", {"selected_cap_usd": 100.0}),
    )
    monkeypatch.setattr(
        gov,
        "realized_pnl_today_by_broker",
        lambda *_a, **_k: {"robinhood_spot": bad},
    )

    blocked, info = gov.broker_daily_loss_breached(
        object(),
        "robinhood_spot",
    )

    assert blocked is True
    assert info["transient"] is True
    assert info["sticky"] is False
    assert info["reason"] == "broker_daily_loss_ledger_nonfinite"
    assert gov.is_broker_daily_loss_blocked("robinhood_spot") is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_alpaca_nonfinite_broker_delta_is_transient_fail_closed_without_sticky(
    monkeypatch: pytest.MonkeyPatch,
    bad: float,
) -> None:
    monkeypatch.setattr(
        gov,
        "_per_broker_daily_loss_cap_detail",
        lambda _fam: (250.0, "test_cap", {"selected_cap_usd": 250.0}),
    )
    monkeypatch.setattr(
        gov,
        "_alpaca_account_daily_change_usd",
        lambda *args, **kwargs: (
            bad,
            {"data_source": "alpaca_account_equity_delta"},
        ),
    )

    blocked, info = gov.broker_daily_loss_breached(object(), "alpaca_spot")

    assert blocked is True
    assert info["transient"] is True
    assert info["sticky"] is False
    assert info["reason"] == "alpaca_account_daily_change_nonfinite"
    assert gov.is_broker_daily_loss_blocked("alpaca_spot") is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_broker_nonfinite_cap_is_transient_fail_closed_without_sticky(
    monkeypatch: pytest.MonkeyPatch,
    bad: float,
) -> None:
    monkeypatch.setattr(gov.settings, "chili_per_broker_daily_loss_enabled", True)
    monkeypatch.setattr(
        gov,
        "_per_broker_daily_loss_cap_detail",
        lambda _fam: (bad, "corrupt_cap", {}),
    )

    blocked, info = gov.broker_daily_loss_breached(
        object(),
        "robinhood_spot",
    )

    assert blocked is True
    assert info["transient"] is True
    assert info["sticky"] is False
    assert info["reason"] == "broker_daily_loss_cap_invalid"
    assert gov.is_broker_daily_loss_blocked("robinhood_spot") is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_global_nonfinite_ledger_blocks_shadow_check_without_mutating_switch(
    monkeypatch: pytest.MonkeyPatch,
    bad: float,
) -> None:
    monkeypatch.setattr(gov.settings, "chili_global_max_daily_loss_usd", 100.0)
    monkeypatch.setattr(gov.settings, "chili_global_max_daily_loss_pct_of_equity", 0.0)
    monkeypatch.setattr(
        gov,
        "global_realized_pnl_today_et",
        lambda *_a, **_k: {
            "total_usd": bad,
            "autotrader_usd": bad,
            "momentum_usd": 0.0,
        },
    )

    result = gov.check_daily_loss_breach(object(), activate=False)

    assert result["breached"] is True
    assert result["transient"] is True
    assert result["reason"] == "global_daily_loss_breach_invalid_ledger_nonfinite"
    assert gov.is_kill_switch_active() is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_global_nonfinite_cap_blocks_shadow_check_without_mutating_switch(
    monkeypatch: pytest.MonkeyPatch,
    bad: float,
) -> None:
    monkeypatch.setattr(gov.settings, "chili_global_max_daily_loss_usd", bad)
    monkeypatch.setattr(gov.settings, "chili_global_max_daily_loss_pct_of_equity", 0.0)
    monkeypatch.setattr(
        gov,
        "global_realized_pnl_today_et",
        lambda *_a, **_k: {
            "total_usd": 0.0,
            "autotrader_usd": 0.0,
            "momentum_usd": 0.0,
        },
    )

    result = gov.check_daily_loss_breach(object(), activate=False)

    assert result["breached"] is True
    assert result["transient"] is True
    assert result["reason"] == "global_daily_loss_breach_invalid_cap_nonfinite"
    assert gov.is_kill_switch_active() is False


def test_global_nonfinite_live_check_activates_fail_closed_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gov.settings, "chili_global_max_daily_loss_usd", 100.0)
    monkeypatch.setattr(gov.settings, "chili_global_max_daily_loss_pct_of_equity", 0.0)
    monkeypatch.setattr(
        gov,
        "global_realized_pnl_today_et",
        lambda *_a, **_k: {
            "total_usd": float("nan"),
            "autotrader_usd": float("nan"),
            "momentum_usd": 0.0,
        },
    )

    result = gov.check_daily_loss_breach(object(), activate=True)

    assert result["breached"] is True
    assert gov.is_kill_switch_active() is True
    assert gov.get_kill_switch_status()["reason"] == result["reason"]
    assert gov._kill_switch_halts_exits() is False


def test_global_disabled_caps_remain_disabled_with_unreadable_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gov.settings, "chili_global_max_daily_loss_usd", 0.0)
    monkeypatch.setattr(gov.settings, "chili_global_max_daily_loss_pct_of_equity", 0.0)
    monkeypatch.setattr(
        gov,
        "global_realized_pnl_today_et",
        lambda *_a, **_k: {
            "total_usd": float("nan"),
            "autotrader_usd": float("nan"),
            "momentum_usd": 0.0,
        },
    )

    result = gov.check_daily_loss_breach(object(), activate=True)

    assert result["breached"] is False
    assert result["source"] == "none"
    assert gov.is_kill_switch_active() is False


def test_real_broker_disabled_rollout_remains_noop_on_unreadable_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gov.settings, "chili_per_broker_daily_loss_enabled", False)
    monkeypatch.setattr(
        gov,
        "realized_pnl_today_by_broker",
        lambda *_a, **_k: {"robinhood_spot": float("nan")},
    )

    blocked, info = gov.broker_daily_loss_breached(
        object(),
        "robinhood_spot",
    )

    assert blocked is False
    assert info["disabled"] is True
    assert gov.is_broker_daily_loss_blocked("robinhood_spot") is False
