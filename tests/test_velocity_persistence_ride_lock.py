"""LEVER 2B — VELOCITY/PERSISTENCE RIDE-LOCK (CORE).

Pure-function tests for the denoised flow level/slope math, the RIDE/LOCK/HARD
regime decision, and the load-bearing INVARIANT-A proof (ratchet-only: the
RIDE-LOCK NEVER loosens / nulls the structural, breakeven, or live stop — RIDE in
particular only ever declines to tighten further, it can never lower a stop).
"""

from __future__ import annotations

import math

import pytest

from app.services.trading.momentum_neural.paper_execution import (
    ewma_series,
    ofi_level_and_slope,
    velocity_persistence_ride_lock,
)
from app.services.trading.momentum_neural.pipeline import (
    _event_grid_aggressor_flow,
    _live_flow_slope,
)


# --------------------------------------------------------------------------- #
# ewma_series — the denoising step
# --------------------------------------------------------------------------- #
def test_ewma_none_when_empty() -> None:
    assert ewma_series([], half_life=4.0) is None
    assert ewma_series([float("nan")], half_life=4.0) is None


def test_ewma_first_value_is_seed() -> None:
    out = ewma_series([0.5, 0.5, 0.5], half_life=4.0)
    assert out is not None
    assert out[0] == 0.5
    # constant input -> EWMA stays at the constant
    assert all(abs(v - 0.5) < 1e-12 for v in out)


def test_ewma_lags_a_step_change() -> None:
    # step from 0 -> 1: the EWMA rises monotonically toward 1 but lags (denoising).
    out = ewma_series([0.0, 0.0, 1.0, 1.0, 1.0], half_life=2.0)
    assert out is not None
    assert out[1] == 0.0
    assert 0.0 < out[2] < 1.0
    assert out[2] < out[3] < out[4] < 1.0  # monotone rise toward the new level


def test_ewma_rejects_bad_half_life() -> None:
    assert ewma_series([0.1, 0.2], half_life=0.0) is None
    assert ewma_series([0.1, 0.2], half_life=-1.0) is None


# --------------------------------------------------------------------------- #
# ofi_level_and_slope — denoised level + 1st-derivative slope
# --------------------------------------------------------------------------- #
def test_slope_none_when_one_bucket() -> None:
    level, slope = ofi_level_and_slope([0.5], half_life=4.0)
    assert level == pytest.approx(0.5)
    assert slope is None


def test_slope_positive_when_flow_building() -> None:
    # rising OFI level series -> EWMA rising -> last slope > 0 (RIDE signal).
    level, slope = ofi_level_and_slope([0.1, 0.2, 0.3, 0.5, 0.7], half_life=3.0)
    assert level is not None and slope is not None
    assert slope > 0.0


def test_slope_negative_on_rollover() -> None:
    # flow peaks then turns down -> last EWMA slope < 0 (LOCK signal).
    level, slope = ofi_level_and_slope([0.6, 0.7, 0.8, 0.4, 0.0, -0.3], half_life=2.0)
    assert level is not None and slope is not None
    assert slope < 0.0


def test_slope_none_when_no_buckets() -> None:
    level, slope = ofi_level_and_slope([], half_life=4.0)
    assert level is None and slope is None


# --------------------------------------------------------------------------- #
# velocity_persistence_ride_lock — regime decision
# --------------------------------------------------------------------------- #
_COMMON = dict(
    high_water_mark=10.0,
    entry_price=8.0,
    breakeven_floor=8.0,
    side_long=True,
)


def test_ride_holds_wide_and_does_not_tighten() -> None:
    # flow positive + building (slope>=0) + pace persists -> RIDE: band stays = base,
    # candidate stop = HWM*(1-base) = 10*(1-0.05)=9.5; current stop already 9.5 -> no
    # tighten (the runner is left to extend).
    out = velocity_persistence_ride_lock(
        bid=9.96, base_trail_dist_pct=0.05,
        ofi_level=0.5, ofi_slope=0.02,
        tick_rate_per_s=8.0, entry_tick_rate_per_s=10.0, persist_frac=0.6,
        current_stop=9.5, **_COMMON,
    )
    assert out["regime"] == "ride"
    assert out["ride"] is True
    assert out["band_pct"] == pytest.approx(0.05)
    assert out["fired"] is False  # held wide, did not move the stop


def test_lock_collapses_band_near_high_on_rollover() -> None:
    # flow rolls over (slope<0) while NEAR the high (small giveback) -> LOCK: band
    # collapses to ~half the base, the stop RATCHETS UP toward the high.
    out = velocity_persistence_ride_lock(
        bid=9.97, base_trail_dist_pct=0.05,
        ofi_level=0.2, ofi_slope=-0.03,
        tick_rate_per_s=8.0, entry_tick_rate_per_s=10.0, persist_frac=0.6,
        current_stop=9.5, **_COMMON,
    )
    assert out["regime"] == "lock"
    assert out["band_pct"] < 0.05
    assert out["new_stop_floor"] > 9.5  # tightened toward the climax
    assert out["fired"] is True


def test_hard_exit_tighter_than_lock_with_sellers_through() -> None:
    # strong-negative flow + last print at/below the mid (sellers lifting through) ->
    # HARD: an even tighter band than LOCK.
    hard = velocity_persistence_ride_lock(
        bid=9.97, base_trail_dist_pct=0.05,
        ofi_level=-0.6, ofi_slope=-0.05,
        tick_rate_per_s=8.0, entry_tick_rate_per_s=10.0, persist_frac=0.6,
        current_stop=9.5, micro_price_ref=9.98, last_trade_px=9.97,
        ofi_threshold=0.25, **_COMMON,
    )
    assert hard["regime"] == "hard"
    assert hard["new_stop_floor"] > 9.5
    # HARD band is half the LOCK band -> a strictly higher (tighter) stop than LOCK.
    lock = velocity_persistence_ride_lock(
        bid=9.97, base_trail_dist_pct=0.05,
        ofi_level=0.2, ofi_slope=-0.03,
        tick_rate_per_s=8.0, entry_tick_rate_per_s=10.0, persist_frac=0.6,
        current_stop=9.5, **_COMMON,
    )
    assert hard["new_stop_floor"] >= lock["new_stop_floor"]


def test_hard_degrades_to_lock_without_sellers_through() -> None:
    # strong-negative flow but the last print is ABOVE the mid (no sellers-through) ->
    # NOT hard; the rollover still triggers LOCK.
    out = velocity_persistence_ride_lock(
        bid=9.97, base_trail_dist_pct=0.05,
        ofi_level=-0.6, ofi_slope=-0.05,
        tick_rate_per_s=8.0, entry_tick_rate_per_s=10.0, persist_frac=0.6,
        current_stop=9.5, micro_price_ref=9.98, last_trade_px=9.99,
        ofi_threshold=0.25, **_COMMON,
    )
    assert out["regime"] == "lock"


def test_pace_fade_exits_ride() -> None:
    # flow still positive but the pace has FADED below persist_frac of entry pace ->
    # NOT ride (persistence broken). With no rollover + not near-high enough it falls to
    # neutral (defer to the 2A band); the stop is NOT loosened.
    out = velocity_persistence_ride_lock(
        bid=9.96, base_trail_dist_pct=0.05,
        ofi_level=0.5, ofi_slope=0.01,
        tick_rate_per_s=2.0, entry_tick_rate_per_s=10.0, persist_frac=0.6,  # 2 < 6
        current_stop=9.5, **_COMMON,
    )
    assert out["regime"] == "neutral"
    assert out["ride"] is False
    assert out["new_stop_floor"] >= 9.5


def test_neutral_when_flow_missing_is_byte_identical_to_base() -> None:
    # missing slope -> NEUTRAL, candidate stop = the 2A-width stop, no behavior change.
    out = velocity_persistence_ride_lock(
        bid=9.96, base_trail_dist_pct=0.05,
        ofi_level=None, ofi_slope=None,
        tick_rate_per_s=8.0, entry_tick_rate_per_s=10.0, persist_frac=0.6,
        current_stop=9.0, **_COMMON,
    )
    assert out["regime"] == "neutral"
    # candidate = max(cs=9.0, be=8.0, 10*(1-0.05)=9.5) = 9.5
    assert out["new_stop_floor"] == pytest.approx(9.5)


# --------------------------------------------------------------------------- #
# INVARIANT-A — ratchet-only. THE load-bearing safety property for 2B.
# A RIDE regime (wide band) must NEVER lower the live stop; LOCK/HARD only RAISE.
# --------------------------------------------------------------------------- #
def test_invariant_a_ride_never_loosens_a_high_existing_stop() -> None:
    # current stop ABOVE the RIDE candidate (the band is wide). RIDE must NOT loosen it.
    out = velocity_persistence_ride_lock(
        bid=9.96, base_trail_dist_pct=0.10,  # candidate = 10*(1-0.10)=9.0
        ofi_level=0.5, ofi_slope=0.05,
        tick_rate_per_s=9.0, entry_tick_rate_per_s=10.0, persist_frac=0.6,
        current_stop=9.7, **_COMMON,  # already tighter than the 9.0 candidate
    )
    assert out["new_stop_floor"] == pytest.approx(9.7)  # preserved, never lowered to 9.0
    assert out["fired"] is False


def test_invariant_a_never_below_breakeven() -> None:
    out = velocity_persistence_ride_lock(
        high_water_mark=10.0, entry_price=8.0, side_long=True,
        bid=9.96, base_trail_dist_pct=0.10,  # candidate = 9.0
        ofi_level=0.5, ofi_slope=0.05,
        tick_rate_per_s=9.0, entry_tick_rate_per_s=10.0, persist_frac=0.6,
        breakeven_floor=9.3, current_stop=9.1,
    )
    assert out["new_stop_floor"] == pytest.approx(9.3)  # breakeven floor governs


def test_invariant_a_property_over_grid() -> None:
    # PROOF: across regimes and inputs the returned stop is ALWAYS >= max(current, be).
    for lvl in (-0.8, -0.3, 0.0, 0.3, 0.8):
        for slope in (-0.1, -0.01, 0.0, 0.01, 0.1):
            for cs in (0.0, 5.0, 9.0, 9.99):
                for be in (0.0, 8.0, 9.5):
                    out = velocity_persistence_ride_lock(
                        high_water_mark=10.0, entry_price=8.0, side_long=True,
                        bid=9.9, base_trail_dist_pct=0.05,
                        ofi_level=lvl, ofi_slope=slope,
                        tick_rate_per_s=8.0, entry_tick_rate_per_s=10.0, persist_frac=0.6,
                        breakeven_floor=be, current_stop=cs,
                        micro_price_ref=9.95, last_trade_px=9.9,
                    )
                    nf = out["new_stop_floor"]
                    assert nf >= cs - 1e-9, (lvl, slope, cs, be, nf)
                    assert nf >= be - 1e-9, (lvl, slope, cs, be, nf)


def test_invariant_a_bad_inputs_return_current_stop() -> None:
    out = velocity_persistence_ride_lock(
        high_water_mark=float("nan"), entry_price=8.0, side_long=True,
        bid=9.9, base_trail_dist_pct=0.05,
        ofi_level=0.5, ofi_slope=-0.05,
        tick_rate_per_s=8.0, entry_tick_rate_per_s=10.0, persist_frac=0.6,
        breakeven_floor=9.0, current_stop=9.5,
    )
    assert out["new_stop_floor"] == 9.5  # never nulls the live stop


def test_short_side_noop() -> None:
    out = velocity_persistence_ride_lock(
        high_water_mark=10.0, entry_price=12.0, side_long=False,
        bid=10.1, base_trail_dist_pct=0.05,
        ofi_level=0.5, ofi_slope=-0.05,
        tick_rate_per_s=8.0, entry_tick_rate_per_s=10.0, persist_frac=0.6,
        breakeven_floor=12.0, current_stop=11.0,
    )
    assert out["regime"] == "neutral"
    assert out["new_stop_floor"] == 11.0


# --------------------------------------------------------------------------- #
# _event_grid_aggressor_flow — Lee-Ready bucketed OFI level series + tick_rate
# --------------------------------------------------------------------------- #
class _TS:
    def __init__(self, s: float) -> None:
        self._s = s

    def timestamp(self) -> float:
        return self._s


def _rows(specs):
    """specs: [(epoch, price, size, bid, ask)] -> (price, size, bid, ask, observed_at)."""
    return [(px, sz, bid, ask, _TS(ep)) for ep, px, sz, bid, ask in specs]


def test_aggressor_flow_buy_dominant_positive_level() -> None:
    # all prints at/above the ask -> Lee-Ready BUY -> OFI level near +1.
    rows = _rows([(float(i), 10.02, 100.0, 10.00, 10.02) for i in range(8)])
    levels, tick_rate, dbg = _event_grid_aggressor_flow(rows, grid_secs=2.0)
    assert dbg["n_ticks"] == 8
    assert dbg["n_grid"] >= 2
    assert all(lv > 0.9 for lv in levels)  # buy-dominated buckets
    assert tick_rate == pytest.approx(8 / 7.0, rel=1e-6)


def test_aggressor_flow_sell_dominant_negative_level() -> None:
    # all prints at/below the bid -> Lee-Ready SELL -> OFI level near -1.
    rows = _rows([(float(i), 10.00, 100.0, 10.00, 10.02) for i in range(8)])
    levels, _tr, dbg = _event_grid_aggressor_flow(rows, grid_secs=2.0)
    assert all(lv < -0.9 for lv in levels)


def test_aggressor_flow_thin_tape_empty() -> None:
    levels, tick_rate, dbg = _event_grid_aggressor_flow(
        _rows([(0.0, 10.0, 100.0, 9.99, 10.01)]), grid_secs=2.0
    )
    assert levels == []
    assert dbg["n_grid"] == 0


def test_aggressor_flow_rollover_series_feeds_negative_slope() -> None:
    # buy-dominated early buckets then sell-dominated late buckets -> the level series
    # rolls over -> ofi_level_and_slope returns a negative slope (the LOCK trigger).
    specs = []
    for i in range(6):  # buy phase 0-5s
        specs.append((float(i), 10.02, 100.0, 10.00, 10.02))
    for i in range(6, 12):  # sell phase 6-11s
        specs.append((float(i), 10.00, 100.0, 10.00, 10.02))
    levels, _tr, _dbg = _event_grid_aggressor_flow(_rows(specs), grid_secs=2.0)
    assert len(levels) >= 4
    _lvl, slope = ofi_level_and_slope(levels, half_life=2.0)
    assert slope is not None and slope < 0.0


# --------------------------------------------------------------------------- #
# _live_flow_slope — EXPLICIT latest-tick-age (stale-tape) gate. A tape that went
# silent but still has >= 2 grid buckets inside the trailing window must FAIL CLOSED
# (return None) so GAP-B / decisive_flow_cut never fire on a frozen tape.
# --------------------------------------------------------------------------- #
from datetime import datetime, timedelta  # noqa: E402


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_a, **_k):
        return _FakeResult(self._rows)


def _dt_rows(as_of, age_offsets, *, span=8.0):
    """(price, size, bid, ask, observed_at) buy-dominated rows; newest = as_of - age."""
    rows = []
    # spread a couple of buy-dominated buckets, newest tick at as_of - age_offsets[-1]
    n = len(age_offsets)
    for i, age in enumerate(age_offsets):
        observed_at = as_of - timedelta(seconds=age)
        rows.append((10.02, 100.0, 10.00, 10.02, observed_at))
    # SQL returns ASC (oldest first); ensure ordering by observed_at
    rows.sort(key=lambda r: r[4])
    return rows


def test_live_flow_slope_fresh_tape_returns_read() -> None:
    as_of = datetime(2026, 6, 29, 14, 30, 0)
    # two buckets within a 15s window, newest print only ~0.5s old (< grid_secs=2.0) -> FRESH
    rows = _dt_rows(as_of, [10.0, 8.0, 6.0, 4.0, 2.0, 0.5])
    out = _live_flow_slope("ABCD", db=_FakeDB(rows), as_of=as_of, grid_secs=2.0)
    assert out is not None
    assert out["ofi_level"] is not None
    assert out["n_grid"] >= 2


def test_live_flow_slope_stale_tape_fails_closed() -> None:
    as_of = datetime(2026, 6, 29, 14, 30, 0)
    # same shape, but the NEWEST print is 6s old (> grid_secs=2.0) though still inside the
    # 15s rolling window -> the tape is effectively FROZEN -> must return None (fail-closed).
    rows = _dt_rows(as_of, [13.0, 12.0, 11.0, 9.0, 7.0, 6.0])
    out = _live_flow_slope("ABCD", db=_FakeDB(rows), as_of=as_of, grid_secs=2.0)
    assert out is None
