"""WAVE-4 ITEM-5 — JEM sticky-bench VWAP-reclaim UN-BENCH.

FIX D (test_backside_vwap_reclaim_fix_d) only declines to LATCH a fresh below_vwap bench.
Once a name is ALREADY benched (any latch reason), the ONLY un-bench was a genuine NEW HIGH
above the benched-at HOD. JEM (2026-07-02) benched, then at 12:50 reclaimed VWAP from below
(8.97 -> 9.06) into the 9.0 -> 9.7 leg — never a new HOD, so it stayed permanently benched.

This adds a SECOND un-bench for a benched name: a genuine fresh CROSS-from-below of session
VWAP (prior completed close < VWAP*(1-buffer) AND current px >= VWAP, rising). It is a CROSS
(a state change), NOT a level test — a level test would un-bench into the 13:24 hover-then-
dump; the cross preserves that veto while catching the 12:50 reclaim.

Tests:
  * the JEM 12:50 CROSS profile -> un-benched (unbenched_vwap_reclaim);
  * the 13:10 LEVEL-ONLY profile (price already above VWAP, no fresh cross) -> STAYS benched;
  * the flag OFF -> byte-identical (a benched name un-benches only on a new high).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.config import settings
from app.services.trading.momentum_neural.entry_gates import evaluate_sticky_backside_bench
from app.services.trading.momentum_neural.ross_momentum import front_side_state


def _session_index(date: str, n: int, freq: str = "1min", start: str = "13:30") -> pd.DatetimeIndex:
    return pd.date_range(f"{date} {start}", periods=n, freq=freq, tz="UTC")


def _ohlc_from_closes(closes, vols, idx) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    vols = np.asarray(vols, dtype=float)
    n = len(closes)
    opens = np.empty(n)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    highs = np.maximum(opens, closes) * 1.001
    lows = np.minimum(opens, closes) * 0.999
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols}, index=idx
    )


def _jem_faded_below_vwap_day():
    """JEM shape: a morning run pulls VWAP up, then a fade UNDER VWAP (the benched state).
    The LAST completed bar closes BELOW the cumulative VWAP (so a live tick above VWAP is a
    fresh cross-from-below). Cumulative VWAP settles ~ mid-8s; the last close ~ 8.97."""
    up = np.linspace(8.0, 9.8, 12)              # morning run: VWAP climbs toward ~9
    dip = np.linspace(9.8, 8.97, 10)            # fade UNDER VWAP; last completed close 8.97
    closes = np.concatenate([up, dip])
    vols = np.full(len(closes), 1000.0)
    return closes, vols


def _df(day):
    closes, vols = day
    return _ohlc_from_closes(closes, vols, _session_index("2026-07-02", len(closes)))


# --------------------------------------------------------------------------- #
# (a) the JEM 12:50 CROSS profile -> UN-BENCHED                                #
# --------------------------------------------------------------------------- #
def test_jem_vwap_reclaim_cross_unbenches(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_backside_bench_reclaim_unbench_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_entry_vwap_hold_buffer", 0.0, raising=False)
    df = _df(_jem_faded_below_vwap_day())
    fs = front_side_state(df)
    vwap = float(fs.session_vwap)
    prior_close = float(df["Close"].iloc[-1])
    assert prior_close < vwap, "the last completed close must be BELOW VWAP (the benched fade)"
    # ALREADY benched at a HOD ABOVE the session HOD -> no new high; the live tick reclaims
    # VWAP from below (9.06 > vwap while the prior close 8.97 < vwap) -> the CROSS un-benches.
    cur_hod = float(df["High"].astype(float).max())
    benched, reason, hod_out, dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=cur_hod + 5.0, live_price=vwap * 1.01,
    )
    assert benched is False, f"a fresh VWAP-reclaim cross must un-bench, got {reason}"
    assert reason == "unbenched_vwap_reclaim"
    assert hod_out is None            # the marker is dropped
    assert "unbenched_vwap_reclaim" in dbg


# --------------------------------------------------------------------------- #
# (b) the 13:10 LEVEL-ONLY profile (no fresh cross) -> STAYS benched           #
# --------------------------------------------------------------------------- #
def test_level_only_no_fresh_cross_stays_benched(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_backside_bench_reclaim_unbench_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_entry_vwap_hold_buffer", 0.0, raising=False)
    df = _df(_jem_faded_below_vwap_day())
    fs = front_side_state(df)
    vwap = float(fs.session_vwap)
    # A live tick ABOVE VWAP but the PRIOR completed close is ALSO above VWAP (no cross-from-
    # below): the name is merely hovering near/above VWAP (the 13:24 hover-then-dump). To
    # simulate a prior-close-above-VWAP frame, append a completed bar that closed above VWAP.
    closes = list(df["Close"].values) + [vwap * 1.02]   # last completed bar ABOVE VWAP
    vols = list(df["Volume"].values) + [1000.0]
    df2 = _ohlc_from_closes(closes, vols, _session_index("2026-07-02", len(closes)))
    fs2 = front_side_state(df2)
    vwap2 = float(fs2.session_vwap)
    prior_close2 = float(df2["Close"].iloc[-1])
    assert prior_close2 >= vwap2 * (1.0 - 0.0), "the prior close is at/above VWAP (no fresh cross)"
    cur_hod = float(df2["High"].astype(float).max())
    benched, reason, hod_out, dbg = evaluate_sticky_backside_bench(
        df2, benched_at_hod=cur_hod + 5.0, live_price=vwap2 * 1.03,
    )
    assert benched is True, f"a level-test-only (no cross) must STAY benched, got {reason}"
    assert reason == "benched_backside_sticky"
    assert "unbenched_vwap_reclaim" not in dbg


# --------------------------------------------------------------------------- #
# (c) flag OFF -> byte-identical (un-bench only on a new high)                 #
# --------------------------------------------------------------------------- #
def test_flag_off_byte_identical(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_backside_bench_reclaim_unbench_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_entry_vwap_hold_buffer", 0.0, raising=False)
    df = _df(_jem_faded_below_vwap_day())
    fs = front_side_state(df)
    vwap = float(fs.session_vwap)
    cur_hod = float(df["High"].astype(float).max())
    # The SAME cross that un-benched with the flag ON must NOT un-bench with it OFF.
    benched, reason, hod_out, dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=cur_hod + 5.0, live_price=vwap * 1.01,
    )
    assert benched is True, "flag OFF: a benched name un-benches only on a new high"
    assert reason == "benched_backside_sticky"
    assert "unbenched_vwap_reclaim" not in dbg
    # a genuine NEW HIGH still clears it (the mandatory un-bench is unaffected by the flag).
    benched2, reason2, _, _ = evaluate_sticky_backside_bench(
        df, benched_at_hod=cur_hod - 1.0, live_price=cur_hod + 1.0,
    )
    assert benched2 is False
    assert reason2 == "unbenched_fresh_hod"


def test_historical_wick_above_anchor_does_not_clear_failed_backside(monkeypatch):
    """A prior wick above the anchor is not a current phase transition."""
    monkeypatch.setattr(
        settings,
        "chili_momentum_backside_bench_reclaim_unbench_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "chili_momentum_entry_vwap_hold_buffer",
        0.0,
        raising=False,
    )
    df = _df(_jem_faded_below_vwap_day())
    historical_hod = float(df["High"].astype(float).max())
    anchor = historical_hod - 0.01
    current = float(df["Close"].iloc[-1])
    assert historical_hod > anchor
    assert current < anchor

    benched, reason, hod_out, dbg = evaluate_sticky_backside_bench(
        df,
        benched_at_hod=anchor,
        live_price=current,
    )

    assert benched is True
    assert reason == "benched_backside_sticky"
    assert hod_out == anchor
    assert dbg.get("current_px") == current
    assert "unbenched_new_high" not in dbg


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
