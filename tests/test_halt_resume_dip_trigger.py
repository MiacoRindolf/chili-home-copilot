"""halt_resume_dip_trigger — Ross's post-halt dip buy (pure function, synthetic 1m bars).

The pattern: halt resumes -> pop to a reference high -> a REAL dip (>= noise floor,
<= deep cap, both ATR%-scaled) -> a conviction reclaim bar that holds the dip low.
Fires with debug pullback_low/pullback_high under the SAME keys as the pullback
trigger so sizing/stops/bailouts reuse the existing machinery.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.services.trading.momentum_neural.entry_gates import halt_resume_dip_trigger

RESUME = pd.Timestamp("2026-06-10 15:13:00", tz="UTC")
NOW = pd.Timestamp("2026-06-10 15:18:00", tz="UTC")


def _frame(rows, start="2026-06-10 14:50:00", freq="1min"):
    """rows = [(o, h, l, c, v), ...]; index is tz-aware UTC 1m bars."""
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame(
        [{"Open": o, "High": h, "Low": low, "Close": c, "Volume": v} for o, h, low, c, v in rows],
        index=idx,
    )


def _base_rows(n=23):
    """Quiet pre-halt drift 1.50->1.56 ending at 15:12 (last bar BEFORE the resume)."""
    rows = []
    px = 1.50
    for _ in range(n):
        rows.append((px, px + 0.01, px - 0.01, px + 0.005, 60_000))
        px += 0.003
    return rows


def test_fires_on_pop_dip_reclaim():
    rows = _base_rows()
    # post-resume (15:13..): pop to 1.78, dip to 1.64, stabilize, reclaim
    rows += [
        (1.60, 1.78, 1.58, 1.74, 900_000),   # 15:13 resume pop -> ref high 1.78
        (1.73, 1.74, 1.64, 1.66, 500_000),   # 15:14 the dip (1.78 -> 1.64 = ~7.9%)
        (1.66, 1.70, 1.65, 1.69, 450_000),   # 15:15 stabilizing
        (1.69, 1.75, 1.68, 1.74, 700_000),   # 15:16 RECLAIM: closes above prior high, holds dip low
    ]
    df = _frame(rows)
    ok, reason, dbg = halt_resume_dip_trigger(df, entry_interval="1m", halt_resumed_at_utc=RESUME, now=NOW)
    assert ok is True and reason == "halt_resume_dip_ok"
    assert dbg["pullback_high"] == pytest.approx(1.78)
    assert dbg["pullback_low"] == pytest.approx(1.64)


def test_no_dip_yet_still_pumping():
    rows = _base_rows()
    rows += [
        (1.60, 1.70, 1.58, 1.69, 900_000),
        (1.69, 1.78, 1.68, 1.77, 800_000),
        (1.77, 1.85, 1.76, 1.84, 700_000),   # ref high is the LAST bar -> no dip yet
    ]
    ok, reason, _ = halt_resume_dip_trigger(_frame(rows), entry_interval="1m", halt_resumed_at_utc=RESUME, now=NOW)
    assert ok is False and reason == "resume_dip_forming"


def test_collapse_too_deep_is_rejected():
    rows = _base_rows()
    rows += [
        (1.60, 1.80, 1.58, 1.76, 900_000),   # pop to 1.80
        (1.74, 1.75, 1.20, 1.25, 800_000),   # collapse -33% — not a dip, a breakdown
        (1.25, 1.30, 1.24, 1.29, 400_000),
        (1.29, 1.36, 1.28, 1.35, 500_000),   # "reclaim" off the collapse
    ]
    ok, reason, _ = halt_resume_dip_trigger(_frame(rows), entry_interval="1m", halt_resumed_at_utc=RESUME, now=NOW)
    assert ok is False and reason == "resume_dip_too_deep"


def test_weak_reclaim_candle_rejected():
    rows = _base_rows()
    rows += [
        (1.60, 1.78, 1.58, 1.74, 900_000),
        (1.73, 1.74, 1.64, 1.66, 500_000),
        (1.66, 1.70, 1.65, 1.69, 450_000),
        # topping-tail "reclaim": closes above prior high (1.70) but in the lower
        # part of its range with a dominant upper wick — no conviction
        (1.69, 1.75, 1.68, 1.705, 700_000),
    ]
    ok, reason, _ = halt_resume_dip_trigger(_frame(rows), entry_interval="1m", halt_resumed_at_utc=RESUME, now=NOW)
    assert ok is False and reason == "resume_dip_weak_candle"


def test_new_low_on_entry_bar_rejected():
    rows = _base_rows()
    rows += [
        (1.60, 1.78, 1.58, 1.74, 900_000),
        (1.73, 1.74, 1.64, 1.66, 500_000),
        (1.66, 1.70, 1.65, 1.69, 450_000),
        (1.69, 1.75, 1.62, 1.74, 700_000),   # undercuts the 1.64 dip low -> not stabilized
    ]
    ok, reason, _ = halt_resume_dip_trigger(_frame(rows), entry_interval="1m", halt_resumed_at_utc=RESUME, now=NOW)
    assert ok is False and reason == "resume_dip_no_reclaim"


def test_outside_window_rejected():
    rows = _base_rows()
    rows += [
        (1.60, 1.78, 1.58, 1.74, 900_000),
        (1.73, 1.74, 1.64, 1.66, 500_000),
        (1.66, 1.70, 1.65, 1.69, 450_000),
        (1.69, 1.75, 1.68, 1.74, 700_000),
    ]
    late = RESUME + pd.Timedelta(minutes=30)  # > 600s window
    ok, reason, _ = halt_resume_dip_trigger(_frame(rows), entry_interval="1m", halt_resumed_at_utc=RESUME, now=late)
    assert ok is False and reason == "resume_dip_window_passed"


def test_insufficient_post_resume_bars():
    rows = _base_rows()
    rows += [(1.60, 1.78, 1.58, 1.74, 900_000)]  # only 1 post-resume bar
    ok, reason, _ = halt_resume_dip_trigger(
        _frame(rows), entry_interval="1m", halt_resumed_at_utc=RESUME,
        now=RESUME + pd.Timedelta(minutes=2))
    assert ok is False and reason == "resume_dip_forming"


def test_bad_resume_timestamp_failsafe():
    ok, reason, _ = halt_resume_dip_trigger(
        _frame(_base_rows()), entry_interval="1m", halt_resumed_at_utc="garbage", now=NOW)
    assert ok is False and reason == "resume_dip_bad_resume_ts"


def test_naive_index_handled():
    rows = _base_rows()
    rows += [
        (1.60, 1.78, 1.58, 1.74, 900_000),
        (1.73, 1.74, 1.64, 1.66, 500_000),
        (1.66, 1.70, 1.65, 1.69, 450_000),
        (1.69, 1.75, 1.68, 1.74, 700_000),
    ]
    df = _frame(rows)
    df.index = df.index.tz_localize(None)  # naive UTC index, as some fetchers return
    ok, reason, _ = halt_resume_dip_trigger(df, entry_interval="1m", halt_resumed_at_utc=RESUME, now=NOW)
    assert ok is True and reason == "halt_resume_dip_ok"
