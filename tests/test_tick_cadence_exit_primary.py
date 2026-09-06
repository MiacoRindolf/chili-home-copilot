"""The tick-cadence momentum-break exit (#1261) is the PRIMARY exit (2026-09-06).

Operator doctrine, repeated for months: hold while the 10-s candles stay green, exit on the
first red bar that breaks the prior bar's low. The exit census over the 155-receipt gate-15
baseline found it had ended ZERO legs — it shipped dark (default False, absent from the lane
.env) and gated to STATE_LIVE_ENTERED, which the early trail arm leaves seconds after the fill,
while the opinion bailouts (breakout-failed fast bail, lost-VWAP flatten, BOS) ended 116 RH
winner legs for -$3,185 (100 inside 60 s) and 73 Alpaca winner legs for -$1,442.

Runnable: pytest tests/test_tick_cadence_exit_primary.py -v  (DB-free)
"""
from __future__ import annotations

import inspect

from app.config import Settings
from app.services.trading.momentum_neural import live_runner as lr


def test_the_tick_cadence_exit_is_on_by_default():
    assert Settings.model_fields["chili_momentum_failed_pop_break_exit_enabled"].default is True


def test_it_is_evaluated_in_entered_and_trailing_and_its_fallback_is_on():
    src = inspect.getsource(lr.tick_live_session)
    i = src.find('"chili_momentum_failed_pop_break_exit_enabled"')
    assert i > 0
    block = src[i - 800:i + 300]
    assert "st in (STATE_LIVE_ENTERED, STATE_LIVE_TRAILING)" in block
    assert '"chili_momentum_failed_pop_break_exit_enabled", True' in block
    assert "_failed_pop_break_fires(db, sess, le, bid=bid, avg=avg)" in block


def test_it_runs_before_the_opinion_bailouts_in_the_held_tick():
    src = inspect.getsource(lr.tick_live_session)
    i_mb = src.find('"live_momentum_break_exit"')
    i_bb = src.find("breakout_failed_to_hold(")
    i_lv = src.find('"live_lost_vwap_flatten"')
    i_bos = src.find('"live_bos_exit"')
    assert 0 < i_mb < i_bb < i_lv and i_mb < i_bos
    # the safety exits (max-loss circuit, burst window) may precede it
    i_mlc = src.find('"reason": "max_loss_circuit"')
    i_bw = src.find('"live_burst_window_exit"')
    assert 0 < i_mlc < i_mb and 0 < i_bw < i_mb
