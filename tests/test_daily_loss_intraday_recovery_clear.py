"""Intraday-recovery self-heal for the daily-loss kill switch (2026-06-16).

Incident: a transient 09:10 ET −$300 blip tripped the global daily-loss kill switch;
realized PnL recovered to +$265 within the hour, but the switch stayed FROZEN all day
(the only auto-clear was the ET-day-roll) → CHILI was locked out of a profitable day and
missed every mover. Fix: `_auto_clear_recovered_daily_breach` clears the breach once
realized recovers to above -(cap * fraction), with a hysteresis band so it can't flap at
the threshold, throttled because `is_kill_switch_active()` is on the hot order path.
"""

import datetime
import time

import pytest

from app.services.trading import governance as G


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # No DB writes on (de)activate; no DB-poll refresh racing the test.
    monkeypatch.setattr(G, "_persist_kill_switch_state", lambda *a, **k: True)
    monkeypatch.setattr(G, "_refresh_kill_switch_from_db_if_due", lambda *a, **k: None)
    with G._kill_switch_lock:
        G._kill_switch = False
        G._kill_switch_reason = None
        G._kill_switch_set_at = None
        G._daily_breach_recovery_last_check_monotonic = 0.0
    yield
    with G._kill_switch_lock:
        G._kill_switch = False
        G._kill_switch_reason = None
        G._kill_switch_set_at = None


def _cfg(monkeypatch, frac=0.5, interval=0.0):
    monkeypatch.setattr(G.settings, "chili_daily_loss_recovery_clear_fraction", frac, raising=False)
    monkeypatch.setattr(G.settings, "chili_daily_loss_recovery_check_interval_s", interval, raising=False)


def _arm(reason="global_daily_loss_breach_usd_$300"):
    with G._kill_switch_lock:
        G._kill_switch = True
        G._kill_switch_reason = reason
        G._kill_switch_set_at = datetime.datetime.utcnow()
        G._daily_breach_recovery_last_check_monotonic = 0.0


def _stub_breach(monkeypatch, realized, limit=300.0):
    monkeypatch.setattr(
        G, "check_daily_loss_breach",
        lambda db, **k: {"realized_usd": realized, "limit_usd": limit, "breached": realized <= -limit},
    )


def test_recovery_to_profit_auto_clears(monkeypatch):
    # The exact incident: tripped at -$300, recovered to +$265 → must self-clear.
    _cfg(monkeypatch); _arm(); _stub_breach(monkeypatch, realized=+265.0, limit=300.0)
    G._auto_clear_recovered_daily_breach(db=object())
    assert G._kill_switch is False


def test_still_in_loss_stays_frozen(monkeypatch):
    _cfg(monkeypatch); _arm(); _stub_breach(monkeypatch, realized=-280.0, limit=300.0)
    G._auto_clear_recovered_daily_breach(db=object())
    assert G._kill_switch is True


def test_hysteresis_band_stays_frozen(monkeypatch):
    # Recovered above -cap (-300) but NOT above -cap*frac (-150): no clear (anti-flap).
    _cfg(monkeypatch); _arm(); _stub_breach(monkeypatch, realized=-200.0, limit=300.0)
    G._auto_clear_recovered_daily_breach(db=object())
    assert G._kill_switch is True


def test_recovery_just_past_hysteresis_clears(monkeypatch):
    _cfg(monkeypatch); _arm(); _stub_breach(monkeypatch, realized=-149.0, limit=300.0)
    G._auto_clear_recovered_daily_breach(db=object())
    assert G._kill_switch is False  # -149 >= -150 → cleared


def test_manual_reason_untouched(monkeypatch):
    _cfg(monkeypatch); _arm(reason="manual_operator_halt"); _stub_breach(monkeypatch, +500.0)
    G._auto_clear_recovered_daily_breach(db=object())
    assert G._kill_switch is True  # non-daily reasons require operator action


def test_backstop_reason_untouched(monkeypatch):
    _cfg(monkeypatch); _arm(reason="global_daily_loss_breach_backstop_$600"); _stub_breach(monkeypatch, +500.0)
    G._auto_clear_recovered_daily_breach(db=object())
    assert G._kill_switch is True  # per-broker aggregate failsafe has its own clear path


def test_fraction_zero_disables_feature(monkeypatch):
    _cfg(monkeypatch, frac=0.0); _arm(); _stub_breach(monkeypatch, +500.0)
    G._auto_clear_recovered_daily_breach(db=object())
    assert G._kill_switch is True  # disabled → date-roll / manual only


def test_throttle_skips_within_interval(monkeypatch):
    _cfg(monkeypatch, interval=9999.0)
    calls = {"n": 0}

    def _cnt(db, **k):
        calls["n"] += 1
        return {"realized_usd": +265.0, "limit_usd": 300.0, "breached": False}

    monkeypatch.setattr(G, "check_daily_loss_breach", _cnt)
    _arm()
    with G._kill_switch_lock:
        G._daily_breach_recovery_last_check_monotonic = time.monotonic()  # just checked
    G._auto_clear_recovered_daily_breach(db=object())
    assert calls["n"] == 0          # throttled → no PnL query
    assert G._kill_switch is True   # therefore not cleared this pass


def test_is_kill_switch_active_fires_recovery_clear(monkeypatch):
    # Integration: the hot-path entry point clears a recovered breach.
    _cfg(monkeypatch)
    monkeypatch.setattr(G, "clear_stale_broker_daily_loss_blocks", lambda: None)
    _arm(); _stub_breach(monkeypatch, realized=+265.0, limit=300.0)
    assert G.is_kill_switch_active() is False
