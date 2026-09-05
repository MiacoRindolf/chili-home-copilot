"""Bench debug gains HOD-age / frame-freshness telemetry (2026-09-02).

The sticky backside bench already reads the session frame every score-ok tick
and reports ``cur_hod``; nothing reported how OLD that HOD was or how STALE the
frame was. The spent-leg seed needs both. This pins the new debug fields and
that the ``(benched, reason, anchor)`` decision is byte-identical with or
without ``now_utc``.

Runnable: pytest tests/test_entry_gate_evidence_0902_bench_hod_age_debug.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.config import settings
from app.services.trading.momentum_neural.entry_gates import (
    evaluate_sticky_backside_bench,
    session_frame_hod_debug,
)

UTC = timezone.utc


def _frame(day="2026-09-02", start="11:00", n=11, hod_at=2, hod=4.93, last_close=4.34,
           freq="1min", tz="UTC"):
    idx = pd.date_range(f"{day} {start}", periods=n, freq=freq, tz=tz)
    closes = np.linspace(4.60, last_close, n)
    highs = closes * 1.001
    highs[hod_at] = hod
    return pd.DataFrame({
        "Open": closes, "High": highs, "Low": closes * 0.999,
        "Volume": np.full(n, 5000.0), "Close": closes,
    }, index=idx)


def test_canf_shaped_frame_reports_hod_age_and_depth():
    df = _frame()  # 07:00-07:10 ET, HOD 4.93 at 07:02, last close 4.34
    last_end = df.index[-1] + pd.Timedelta(minutes=1)
    _b, _r, _a, dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=None, live_price=4.34, now_utc=last_end.to_pydatetime(),
    )
    assert 7.0 <= dbg["hod_age_min"] <= 9.0
    assert dbg["dd_from_hod_pct"] == pytest.approx(11.97, abs=0.05)
    assert dbg["hod_bar_date_et"] == "2026-09-02"
    assert dbg["frame_age_s"] == pytest.approx(0.0)
    assert dbg["interval_s"] == 60.0
    assert dbg["frame_hod"] == pytest.approx(4.93)
    assert dbg["hod_bar_ts"] == "2026-09-02T11:02:00+00:00"
    assert dbg["hod_bar_end_ts"] == "2026-09-02T11:03:00+00:00"
    assert dbg["frame_last_bar_end_ts"] == "2026-09-02T11:11:00+00:00"


def test_stale_frame_age_is_measured_against_the_tick_clock():
    df = _frame()
    later = (df.index[-1] + pd.Timedelta(minutes=12)).to_pydatetime()
    dbg = session_frame_hod_debug(df, now_utc=later)
    assert dbg["frame_age_s"] == pytest.approx(660.0)
    assert dbg["hod_age_min"] == pytest.approx(19.0)


def test_live_tick_above_frame_hod_is_zero_depth():
    df = _frame()
    _b, _r, _a, dbg = evaluate_sticky_backside_bench(df, benched_at_hod=None, live_price=4.95)
    assert dbg["cur_hod"] == pytest.approx(4.95)
    assert dbg["dd_from_hod_pct"] == pytest.approx(0.0)
    assert dbg["frame_hod"] == pytest.approx(4.93)  # the frame's own top is still reported


def test_yesterday_only_frame_is_dated_yesterday():
    df = _frame(day="2026-09-01")
    now = datetime(2026, 9, 2, 8, 5, tzinfo=UTC)  # 04:05 ET next day
    dbg = session_frame_hod_debug(df, now_utc=now)
    assert dbg["hod_bar_date_et"] == "2026-09-01" != "2026-09-02"
    assert dbg["hod_age_min"] > 60.0


def test_naive_now_is_read_as_utc_and_none_is_deterministic():
    df = _frame()
    naive = (df.index[-1] + pd.Timedelta(minutes=1)).to_pydatetime().replace(tzinfo=None)
    a = session_frame_hod_debug(df, now_utc=naive)
    b = session_frame_hod_debug(df, now_utc=None)
    c = session_frame_hod_debug(df, now_utc=None)
    assert a["hod_age_min"] == b["hod_age_min"] == c["hod_age_min"] == pytest.approx(8.0)
    assert b == c


def test_naive_index_is_read_as_utc():
    df = _frame(tz=None)
    dbg = session_frame_hod_debug(df, now_utc=datetime(2026, 9, 2, 11, 11, tzinfo=UTC))
    assert dbg["hod_age_min"] == pytest.approx(8.0)
    assert dbg["hod_bar_date_et"] == "2026-09-02"


def test_five_minute_frame_infers_interval_and_measures_against_bar_end():
    df = _frame(start="11:00", n=3, hod_at=0, freq="5min")  # bars 11:00, 11:05, 11:10
    now = datetime(2026, 9, 2, 11, 10, tzinfo=UTC)
    dbg = session_frame_hod_debug(df, now_utc=now)
    assert dbg["interval_s"] == 300.0
    assert dbg["hod_bar_end_ts"] == "2026-09-02T11:05:00+00:00"
    assert dbg["hod_age_min"] == pytest.approx(5.0)


def test_degenerate_frames_yield_none_filled_debug_not_exceptions():
    empty = pd.DataFrame({"Open": [], "High": [], "Low": [], "Volume": [], "Close": []})
    dbg = session_frame_hod_debug(empty, now_utc=datetime.now(UTC))
    assert dbg["hod_age_min"] is None and dbg["frame_hod"] is None
    df = _frame()
    df["High"] = np.nan
    dbg = session_frame_hod_debug(df, now_utc=datetime.now(UTC))
    assert dbg["frame_hod"] is None
    assert session_frame_hod_debug(None)["hod_age_min"] is None
    assert session_frame_hod_debug(object())["hod_age_min"] is None


# ── byte-identical decision on the min-fade fixtures ─────────────────────────

def _idx(n):
    return pd.date_range("2026-08-28 13:30", periods=n, freq="1min", tz="UTC")


def _ohlc(closes):
    closes = np.asarray(closes, dtype=float)
    opens = np.empty(len(closes))
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    return pd.DataFrame({
        "Open": opens,
        "High": np.maximum(opens, closes) * 1.001,
        "Low": np.minimum(opens, closes) * 0.999,
        "Volume": np.full(len(closes), 1000.0),
        "Close": closes,
    }, index=_idx(len(closes)))


def _swvl_shallow_rollover():
    return np.concatenate([np.linspace(2.70, 3.04, 15), np.linspace(3.03, 3.00, 10)])


def _deep_fade():
    return np.concatenate([np.linspace(2.00, 3.11, 10), np.linspace(3.05, 1.95, 20)])


@pytest.mark.parametrize("closes,anchor,px", [
    (_swvl_shallow_rollover(), None, 3.00),
    (_deep_fade(), None, 1.95),
    (_deep_fade(), 3.50, 1.95),
    (_deep_fade(), 3.00, 3.20),
])
def test_decision_byte_identical_with_and_without_now_utc(monkeypatch, closes, anchor, px):
    monkeypatch.setattr(settings, "chili_momentum_backside_bench_min_fade_pct", 5.0, raising=False)
    df = _ohlc(closes)
    base = evaluate_sticky_backside_bench(df, benched_at_hod=anchor, live_price=px)
    now = (df.index[-1] + pd.Timedelta(minutes=1)).to_pydatetime()
    with_now = evaluate_sticky_backside_bench(df, benched_at_hod=anchor, live_price=px, now_utc=now)
    stale = evaluate_sticky_backside_bench(
        df, benched_at_hod=anchor, live_price=px, now_utc=now + timedelta(minutes=30),
    )
    assert base[:3] == with_now[:3] == stale[:3]
    for dbg in (base[3], with_now[3], stale[3]):
        assert "hod_age_min" in dbg and "frame_age_s" in dbg and "dd_from_hod_pct" in dbg
    assert stale[3]["frame_age_s"] == pytest.approx(1800.0)
