"""FIRST-DIP-OF-DAY certificate tests (the 2026-07-13 PLSM lesson).

A synthetic PLSM-shaped 1m session: flat base -> violent ignition leg -> deep first
pullback (below the falling 9-EMA, into/under VWAP) -> bottoming-tail flush bar ->
green curl whose live price fully reclaims the flush bar's high. The classic
front-side test (rising 9-EMA / rising VWAP) fails on that shape by construction;
the first-dip certificate must (a) accept it exactly once per day, (b) refuse a
NON-vertical day, (c) refuse a broken day-leg (retrace > 0.618), and (d) go dark
with the flag off.
"""

from types import SimpleNamespace

import numpy as np
import pandas as pd

from app.services.trading.momentum_neural import entry_gates as eg


def _mk_df(rows: list[tuple[float, float, float, float, float]], start: str = "2026-07-13 12:00:00") -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=len(rows), freq="1min")
    o, h, l, c, v = zip(*rows)
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v}, index=idx)


def _plsm_shape(leg_hi: float = 12.8) -> pd.DataFrame:
    """Base ~5.5 -> ignition to ``leg_hi`` -> deep pullback -> bottoming-tail flush at
    ~7.7 -> green curl. Volumes keep VWAP anchored near the ignition zone (above the
    flush low) so the support-touch guard sees a real undercut."""
    rows = [
        # ---- flat base (warms the EMA9) ----
        (5.50, 5.60, 5.45, 5.55, 20_000),
        (5.55, 5.65, 5.50, 5.60, 22_000),
        (5.60, 5.70, 5.55, 5.65, 21_000),
        (5.65, 5.75, 5.60, 5.70, 23_000),
        (5.70, 5.80, 5.65, 5.75, 22_000),
        (5.75, 5.85, 5.70, 5.80, 24_000),
        # ---- ignition leg (huge volume anchors VWAP high) ----
        (5.80, 8.90, 5.80, 8.80, 900_000),
        (8.85, leg_hi, 8.70, leg_hi - 0.4, 1_400_000),
        # ---- extended pullback so the 9-EMA has clearly TURNED DOWN by the flush ----
        (leg_hi - 0.5, leg_hi - 0.3, 10.0, 10.1, 500_000),
        (10.1, 10.3, 9.4, 9.6, 400_000),
        (9.6, 9.7, 9.0, 9.1, 350_000),
        (9.1, 9.2, 8.7, 8.8, 330_000),
        (8.8, 8.9, 8.5, 8.6, 320_000),
        (8.6, 8.7, 8.4, 8.5, 310_000),
        (8.5, 8.6, 8.3, 8.45, 300_000),
        # ---- the FLUSH bar: bottoming tail into/below support ----
        (8.45, 8.50, 7.70, 8.35, 600_000),
        # ---- the CURL bar (green, holds the dip low) ----
        (8.35, 8.55, 8.20, 8.50, 300_000),
    ]
    return _mk_df(rows)


_ON = SimpleNamespace(
    chili_momentum_flush_dip_buy_enabled=True,
    chili_momentum_first_dip_reclaim_enabled=True,
    chili_momentum_dip_buy_rth_only_enabled=False,   # synthetic clock; RTH gate off
    chili_momentum_reclaim_max_hours_after_open=24.0,  # keep the window open for the test
)
_OFF = SimpleNamespace(
    chili_momentum_flush_dip_buy_enabled=True,
    chili_momentum_first_dip_reclaim_enabled=False,
    chili_momentum_dip_buy_rth_only_enabled=False,
    chili_momentum_reclaim_max_hours_after_open=24.0,
)


def _call(df, settings, live_price, state=None):
    orig = eg.settings
    eg.settings = settings  # the module reads its own global settings symbol
    try:
        return eg.flush_dip_buy_confirmation(
            df, entry_interval="1m", live_price=live_price, symbol="PLSM",
            now=None, first_dip_state=state,
        )
    finally:
        eg.settings = orig


def test_first_dip_certificate_fires_on_plsm_shape():
    df = _plsm_shape()
    # live price fully reclaims the flush bar's high (8.65)
    ok, reason, dbg = _call(df, _ON, live_price=8.70)
    assert ok is True, (reason, dbg)
    assert reason == "flush_dip_buy"
    assert dbg.get("front_side_via") == "first_dip_day_leg"
    assert dbg.get("pullback_low") == 7.70  # structural stop = the dip low


def test_bounce_proof_requires_full_flush_high_reclaim():
    df = _plsm_shape()
    # price inside the flush bar's range — NOT above its high (8.50): no proof yet
    ok, reason, dbg = _call(df, _ON, live_price=8.45)
    assert ok is False
    assert reason == "flush_dip_not_reclaimed"


def test_once_per_day_marker_blocks_the_second_dip():
    df = _plsm_shape()
    state = {"first_dip_used_date": "2026-07-13"}  # already used today
    ok, reason, dbg = _call(df, _ON, live_price=8.70, state=state)
    assert ok is False
    assert reason == "flush_dip_not_front_side"


def test_non_vertical_day_never_certifies():
    # A gentle day: leg 5.5 -> 6.2 (~13%) is far below 3×ATR% for a volatile name;
    # atr_pct inside comes from the df via compute_all_from_df — shrink the leg so
    # the vertical bar fails.
    rows = [(5.5 + i * 0.05, 5.6 + i * 0.05, 5.45 + i * 0.05, 5.55 + i * 0.05, 20_000) for i in range(8)]
    rows += [
        (5.95, 6.20, 5.90, 6.10, 60_000),
        (6.10, 6.15, 5.60, 5.70, 50_000),   # pullback below EMA9
        (5.70, 5.75, 5.40, 5.65, 80_000),   # flush bar w/ tail
        (5.65, 5.80, 5.60, 5.78, 40_000),   # curl
    ]
    df = _mk_df(rows)
    ok, reason, dbg = _call(df, _ON, live_price=5.85)
    assert ok is False
    assert reason in ("flush_dip_not_front_side", "flush_dip_too_shallow", "flush_dip_no_support_touch")


def test_broken_leg_beyond_618_refused():
    df = _plsm_shape()
    # break the leg on a CLOSE basis (the intactness yardstick): the flush bar settles
    # far below the 0.618 line, not just wicks below it
    _flush = len(df) - 2
    df.iloc[_flush, df.columns.get_loc("Low")] = 5.90
    df.iloc[_flush, df.columns.get_loc("Close")] = 6.30
    df.iloc[_flush, df.columns.get_loc("Open")] = 8.45
    ok, reason, dbg = _call(df, _ON, live_price=8.70)
    assert ok is False
    assert reason in ("flush_dip_not_front_side", "flush_dip_undercut", "flush_dip_weak_curl")


def test_flag_off_is_dark():
    df = _plsm_shape()
    ok, reason, dbg = _call(df, _OFF, live_price=8.70)
    assert ok is False
    assert reason == "flush_dip_not_front_side"
