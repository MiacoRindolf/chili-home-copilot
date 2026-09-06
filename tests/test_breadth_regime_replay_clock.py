"""HARNESS GATE 15 (2026-09-06): the breadth regime must be read at the DECISION instant.

MEASURED: the Ross Parity Bench replayed DFNS 2026-07-29 four times on identical logic and got
three different day P&Ls (-75.72, -71.47, -64.21). Fills were identical up to #37 and then
differed in QUANTITY only (12.043 vs 10.236 shares at the same instant and price, ratio 0.85).
With the 28 sizing factors carried in the receipt, the moving one was `wildcard_bgrade` =
0.85 = `_PRE_HOLIDAY_SIZE_TILT`: `compute_breadth_regime(db)` had defaulted `now` to the WALL
clock, and every run that started after 00:00Z on Sunday 2026-09-06 (the day before Labor Day)
sized a July replay as a pre-holiday session. Live is unaffected (wall == decision instant);
the replay was not.

Fix: the two FSM-path callers pass the replay-aware clock (`live_runner._utcnow_aware()`,
`risk_policy._risk_now_naive()`). DB-free.

Runnable: pytest tests/test_breadth_regime_replay_clock.py -v
"""
from __future__ import annotations

import inspect
from datetime import date

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import risk_policy as rp
from app.services.trading.momentum_neural.market_calendar import is_pre_holiday


def test_the_measured_calendar_flip():
    assert is_pre_holiday(date(2026, 9, 6)) is True      # the wall date of the runs that sized x0.85
    assert is_pre_holiday(date(2026, 9, 5)) is False     # the wall date of the runs that sized x1.0
    assert is_pre_holiday(date(2026, 7, 29)) is False    # the replayed session's own date


def test_live_runner_reads_the_regime_at_its_replay_aware_clock():
    src = inspect.getsource(lr.tick_live_session)
    i = src.find("_wc_reg = compute_breadth_regime(")
    assert i > 0
    assert "compute_breadth_regime(db, now=_utcnow_aware())" in src[i:i + 120]
    assert "compute_breadth_regime(db)\n" not in src


def test_risk_policy_reads_the_regime_at_its_replay_aware_clock():
    src = inspect.getsource(rp._wildcard_dominant_symbol)
    assert "compute_breadth_regime(db, now=_risk_now_naive())" in src
