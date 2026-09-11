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
    assert 0 < i_mb < i_bb < i_lv
    # the BOS (close-below-structure) site was retired 2026-09-10 [57]: nothing to order
    assert src.find('"live_bos_exit"') < 0
    # the max-loss circuit (a USD risk cap, not an opinion) precedes it; the burst-window
    # exit is a WALL-CLOCK decision and since 2026-09-08 ("ANG TICK ANG UNA, HINDI ANG
    # ORASAN") it sits AFTER the tick exit in the elif chain -- the tape speaks first.
    i_mlc = src.find('"reason": "max_loss_circuit"')
    i_bw = src.find('"live_burst_window_exit"')
    assert 0 < i_mlc < i_mb and 0 < i_mb < i_bw


# ── FRAME RECENCY (2026-09-06 review, confirmed major) ────────────────────────
# The decision is 100% frame-shaped and the caller discards the newest row as
# "forming"; on a lagging tape that row is a COMPLETE bar from an older bucket,
# so without a bound the exit market-sells the whole position on stale structure.

def test_the_frame_recency_bound_exists_and_fails_closed():
    src = inspect.getsource(lr._failed_pop_break_fires)
    i = src.find("_build_micro_bar_df")
    j = src.find("bar_closes_opens=_co")
    assert 0 < i < j
    block = src[i:j]
    assert "_frame_last_bar_age_seconds(_df, _utcnow_aware())" in block
    assert '"chili_momentum_failed_pop_break_max_frame_age_s", 20.0' in block
    # fail-closed: unreadable age or an age past the bound must NOT fire
    assert "if _fpb_age is None or not math.isfinite(_fpb_age) or _fpb_age > _fpb_max_age:" in block
    assert '"reason": "micro_frame_stale"' in block
    assert "return False" in block[block.find("micro_frame_stale"):]
    # and the age is on every receipt, so a fire can be triaged after the fact
    whole = inspect.getsource(lr._failed_pop_break_fires)
    assert 'dbg["frame_last_bar_age_s"]' in whole


def test_the_bound_is_two_micro_buckets_by_default():
    from app.config import Settings
    assert Settings.model_fields["chili_momentum_failed_pop_break_max_frame_age_s"].default == 20.0


def test_the_frame_age_helper_reads_a_lagging_frame_as_stale():
    from datetime import datetime, timedelta, timezone
    import pandas as pd
    now = datetime(2026, 8, 14, 13, 46, 30, tzinfo=timezone.utc)
    fresh = pd.DataFrame({"Close": [1.0, 1.1]}, index=pd.DatetimeIndex(
        [now - timedelta(seconds=20), now - timedelta(seconds=10)]))
    stale = pd.DataFrame({"Close": [1.0, 1.1]}, index=pd.DatetimeIndex(
        [now - timedelta(seconds=45), now - timedelta(seconds=35)]))
    assert lr._frame_last_bar_age_seconds(fresh, now) <= 20.0
    assert lr._frame_last_bar_age_seconds(stale, now) > 20.0
    assert lr._frame_last_bar_age_seconds(pd.DataFrame(), now) is None
