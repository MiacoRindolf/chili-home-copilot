"""Gate #7: the Alpaca entry seam reads the broker market clock; the mock answers from the
sim instant, truthfully.

MEASURED 2026-09-04 (SDOT 2026-06-26 canon run, six gates cleared): 26 attempts reached
``live_entry_final_bbo`` (execution_bbo_ok) and 19 were deferred with
``alpaca_broker_clock_unavailable`` -- ``_strict_alpaca_clock_truth`` requires
``adapter.get_market_clock_snapshot``. These tests drive the REAL clock-truth check against
the mock under ``replay_clock``. DB-free.
"""
from __future__ import annotations

from datetime import datetime

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.replay_mock_broker import MockBrokerAdapter


def _mock_at(t):
    m = MockBrokerAdapter(resting_limit_fills=True, volume_cap_enabled=True, freshness_mode="wall")
    m.set_clock(t)
    return m


def test_premarket_instant_is_not_open_and_points_at_todays_open_and_close():
    t = datetime(2026, 6, 26, 13, 18, 12)  # Fri 09:18:12 ET
    snap = _mock_at(t).get_market_clock_snapshot()
    assert snap["ok"] is True and snap["paper"] is True
    assert snap["is_open"] is False
    assert snap["timestamp"] == "2026-06-26T13:18:12+00:00"
    assert snap["next_open"] == "2026-06-26T13:30:00+00:00"
    assert snap["next_close"] == "2026-06-26T20:00:00+00:00"


def test_rth_instant_is_open():
    t = datetime(2026, 6, 26, 15, 0, 0)  # Fri 11:00 ET
    snap = _mock_at(t).get_market_clock_snapshot()
    assert snap["is_open"] is True
    assert snap["next_open"] == "2026-06-29T13:30:00+00:00"   # Monday
    assert snap["next_close"] == "2026-06-26T20:00:00+00:00"


def test_friday_evening_points_at_monday():
    t = datetime(2026, 6, 26, 21, 0, 0)  # Fri 17:00 ET
    snap = _mock_at(t).get_market_clock_snapshot()
    assert snap["is_open"] is False
    assert snap["next_open"] == "2026-06-29T13:30:00+00:00"
    assert snap["next_close"] == "2026-06-29T20:00:00+00:00"


def test_the_runner_clock_truth_accepts_the_mock_at_the_sim_instant():
    t = datetime(2026, 6, 26, 13, 18, 12)
    m = _mock_at(t)
    with lr.replay_clock(t):
        clock, evidence = lr._strict_alpaca_clock_truth(m)
    assert clock is not None, evidence
    assert evidence["broker_clock_ok"] is True
    assert evidence["broker_clock_age_seconds"] == 0.0
    assert evidence["seconds_to_close"] == 6 * 3600 + 41 * 60 + 48   # 13:18:12Z -> 20:00:00Z
    assert clock["is_open"] is False


def test_the_runner_clock_truth_refuses_a_wall_clock_mismatch():
    """Outside replay_clock the runner's now is the wall clock, 70 days after the sim
    instant: the same strict check must refuse (stale broker clock), as it would in
    production -- the mock never makes a stale clock look fresh."""
    t = datetime(2026, 6, 26, 13, 18, 12)
    clock, evidence = lr._strict_alpaca_clock_truth(_mock_at(t))
    assert clock is None
    assert evidence["reason"] == "alpaca_broker_close_clock_unreadable"
