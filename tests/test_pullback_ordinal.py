"""Ross gap #7: pullback-ordinal de-rate (videos 04/15/17/24/26). The 1st/2nd pullback is
an A-setup; by the 3rd you are greedy and it usually fails. A 3rd+-pullback break is
treated as a WEAKER prior — it must clear the SAME raised volume floor the runaway /
deep-reclaim paths do, so the weak ones filter out and a genuinely strong 3rd still fires.
No-op (byte-identical) for the 1st/2nd pullback.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.config import settings
from app.services.trading.momentum_neural import entry_gates
from app.services.trading.momentum_neural.entry_gates import (
    _LATE_PULLBACK_ORDINAL,
    pullback_break_confirmation,
    pullback_ordinal_recent,
)


# ── pure pullback_ordinal_recent ─────────────────────────────────────────────

def test_no_dip_is_first_pullback():
    assert pullback_ordinal_recent([10.0] * 10, [9.0] * 10, cur=9, ema_wick=0.005) == 1


def test_two_distinct_dips():
    ema9 = [9.0] * 10
    low = [10, 10, 8.0, 10, 10, 10, 8.0, 10, 10, 10]   # two separated dip events
    assert pullback_ordinal_recent(low, ema9, cur=9, ema_wick=0.005) == 2


def test_three_dips_is_late():
    ema9 = [9.0] * 12
    low = [10, 8.0, 10, 8.0, 10, 8.0, 10, 10, 10, 10, 10, 10]   # three dip events
    o = pullback_ordinal_recent(low, ema9, cur=11, ema_wick=0.005)
    assert o == 3 and o >= _LATE_PULLBACK_ORDINAL


def test_consecutive_below_counts_as_one_event():
    ema9 = [9.0] * 8
    low = [10, 8.0, 8.0, 8.0, 10, 10, 10, 10]   # one multi-bar dip = ONE pullback
    assert pullback_ordinal_recent(low, ema9, cur=7, ema_wick=0.005) == 1


def test_bounded_lookback_forgets_old_dips():
    ema9 = [9.0] * 30
    low = [8.0 if i in (0, 2, 4) else 10.0 for i in range(30)]   # 3 dips but all OLD
    assert pullback_ordinal_recent(low, ema9, cur=29, ema_wick=0.005, lookback=10) == 1


def test_fail_open_to_one():
    assert pullback_ordinal_recent([], [], cur=0, ema_wick=0.005) == 1
    assert pullback_ordinal_recent([None] * 5, [None] * 5, cur=4, ema_wick=0.005) == 1


# ── integration: weaker-prior raised volume floor on a 3rd+ pullback ──────────
def _explosive_df(n: int = 30) -> pd.DataFrame:
    base = np.linspace(10.0, 12.10, n - 4)
    highs = list(base + 0.10)
    lows = list(base - 0.10)
    closes = list(base + 0.05)
    opens = list(base - 0.05)
    for h, lo, c in ((12.20, 11.95, 12.05), (12.10, 11.90, 12.00), (12.05, 11.92, 12.02)):
        highs.append(h)
        lows.append(lo)
        closes.append(c)
        opens.append(c - 0.03)
    highs.append(12.40)
    lows.append(12.00)
    closes.append(12.30)
    opens.append(12.05)
    vol = list(np.linspace(200_000, 600_000, n - 4)) + [300_000, 280_000, 320_000, 900_000]
    return pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vol})


_KW = dict(entry_interval="1m", require_retest=True, first_pullback_interval="1m")


def test_marginal_third_pullback_is_de_rated(monkeypatch):
    # the explosive df's breakout volume (vol_ratio ~1.9) clears the BASE 1.5x floor but
    # NOT the raised runaway-level 2.0x floor: a 1st pullback fires, a 3rd+ is de-rated to
    # a decline (the weaker-prior treatment), proving the gap-#7 throttle bites.
    old = settings.chili_momentum_entry_first_pullback_enabled
    settings.chili_momentum_entry_first_pullback_enabled = True
    try:
        monkeypatch.setattr(entry_gates, "pullback_ordinal_recent", lambda *a, **k: 1)
        ok1, r1, _ = pullback_break_confirmation(_explosive_df(), **_KW)
        monkeypatch.setattr(entry_gates, "pullback_ordinal_recent", lambda *a, **k: 3)
        ok3, r3, d3 = pullback_break_confirmation(_explosive_df(), **_KW)
    finally:
        settings.chili_momentum_entry_first_pullback_enabled = old
    assert ok1 is True, r1                                # 1st pullback fires (base floor)
    assert ok3 is False and r3 == "break_low_volume"      # 3rd+ de-rated (raised floor)
    assert d3.get("pullback_ordinal") == 3                # wiring: late-pullback engaged


def test_first_pullback_is_byte_identical(monkeypatch):
    old = settings.chili_momentum_entry_first_pullback_enabled
    settings.chili_momentum_entry_first_pullback_enabled = True
    monkeypatch.setattr(entry_gates, "pullback_ordinal_recent", lambda *a, **k: 1)
    try:
        ok, _reason, dbg = pullback_break_confirmation(_explosive_df(), **_KW)
    finally:
        settings.chili_momentum_entry_first_pullback_enabled = old
    assert ok is True
    assert "pullback_ordinal" not in dbg   # no marker on a 1st/2nd pullback
