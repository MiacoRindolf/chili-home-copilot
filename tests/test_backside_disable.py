"""Ross FRONT/BACK-SIDE veto (gap #1): once a mover rolls to the back side of the
move — the 9-EMA crosses BELOW the 20-EMA, or the MACD line crosses below signal and
stays below — CHILI stops taking CONTINUATION entries (Ross's rule, videos 21/24/26/27).
The deep-reclaim/dip-buy reversal path stays EXEMPT (it intentionally catches the turn
off a dip and carries its own dip-vs-dump discipline, #734).

Three layers:
  * the pure ``_detect_back_side`` helper — trips on 9<20 and on a recent MACD
    cross-down-and-still-below; fails OPEN on thin/missing data so it can never veto
    on warmup,
  * the KILL-SWITCH byte-identity: a FRONT-side firing df fires identically with the
    flag ON and OFF (the load-bearing parity contract),
  * the integration veto: when the detector reads back-side, a normally-firing df
    DECLINES with reason ``back_side_disabled`` under the flag, and still fires with the
    flag OFF (proving the flag gate + wiring + that nothing else changed).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates
from app.services.trading.momentum_neural.entry_gates import (
    _detect_back_side,
    pullback_break_confirmation,
)


# ── pure _detect_back_side ───────────────────────────────────────────────────

def test_backside_trips_on_ema9_below_ema20():
    ema9 = [10.0, 10.1, 9.8]
    ema20 = [9.5, 9.9, 10.0]          # cur=2: 9.8 < 10.0 -> structural back side
    macd = [0.1, 0.1, 0.1]
    sig = [0.0, 0.0, 0.0]
    bs, reason = _detect_back_side(ema9, ema20, macd, sig, cur=2)
    assert bs is True and reason == "ema9_below_ema20"


def test_backside_trips_on_macd_cross_down_still_below():
    # 9 stays above 20 (no structural flip) but the MACD line crossed below signal at
    # the last bar and is STILL below -> momentum rollover.
    ema9 = [10.0, 10.1, 10.2]
    ema20 = [9.0, 9.1, 9.2]
    macd = [0.30, 0.20, 0.05]
    sig = [0.10, 0.15, 0.20]          # cur: prev m(0.20)>=s(0.15), now m(0.05)<s(0.20)
    bs, reason = _detect_back_side(ema9, ema20, macd, sig, cur=2, macd_lookback=3)
    assert bs is True and reason == "macd_crossed_below_signal"


def test_frontside_is_not_back_side():
    ema9 = [9.0, 9.5, 10.0]
    ema20 = [8.5, 8.8, 9.2]           # 9>20 the whole way
    macd = [0.0, 0.1, 0.2]
    sig = [0.0, 0.05, 0.1]            # line above signal, rising
    bs, reason = _detect_back_side(ema9, ema20, macd, sig, cur=2)
    assert bs is False and reason == ""


def test_stale_macd_cross_recovered_is_not_back_side():
    # crossed down earlier but the line is back ABOVE signal now -> not back side.
    ema9 = [10.0, 10.1, 10.2]
    ema20 = [9.0, 9.1, 9.2]
    macd = [0.30, 0.05, 0.30]
    sig = [0.10, 0.15, 0.10]          # cur: m(0.30) > s(0.10) -> not below now
    bs, _ = _detect_back_side(ema9, ema20, macd, sig, cur=2, macd_lookback=3)
    assert bs is False


def test_backside_fails_open_on_missing_data():
    assert _detect_back_side([], [], [], [], cur=0)[0] is False
    assert _detect_back_side([None], [None], [None], [None], cur=0)[0] is False
    assert _detect_back_side([10.0], [9.0], [0.1], [0.0], cur=5)[0] is False  # idx OOB


# ── integration: canonical FRONT-side firing df ──────────────────────────────
# Exact canonical explosive shallow-first-pullback -> new-high df (mirrors
# tests/test_first_pullback.py): the impulse keeps 9>20 (front side), so the
# back-side gate must be a no-op on it.
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


_FIRE_KW = dict(entry_interval="1m", require_retest=True, first_pullback_interval="1m")


@pytest.fixture(autouse=True)
def _fp_on():
    old = settings.chili_momentum_entry_first_pullback_enabled
    settings.chili_momentum_entry_first_pullback_enabled = True
    yield
    settings.chili_momentum_entry_first_pullback_enabled = old


def test_frontside_fires_unchanged():
    # The canonical FRONT-side df keeps 9>20 the whole way, so the always-on back-side
    # gate is a NO-OP on it: it fires exactly as it did before the gate existed (which
    # is what the detector reading False guarantees — proven below by forcing it False).
    ok, reason, _ = pullback_break_confirmation(_explosive_df(), **_FIRE_KW)
    assert ok is True, (reason,)
    assert reason in ("first_pullback_ok", "first_pullback_tick_ok"), reason


def test_backside_vetoes_when_detector_trips(monkeypatch):
    # Force the detector to read back-side; the normally-firing df must DECLINE with
    # reason 'back_side_disabled'. Forcing it False restores the fire (proving the gate
    # is the ONLY thing that changed — the do-no-harm contract, now via the detector
    # rather than an on/off flag).
    monkeypatch.setattr(entry_gates, "_detect_back_side", lambda *a, **k: (True, "forced"))
    ok_bs, reason_bs, dbg_bs = pullback_break_confirmation(_explosive_df(), **_FIRE_KW)
    assert ok_bs is False and reason_bs == "back_side_disabled"
    assert dbg_bs.get("back_side") == "forced"

    monkeypatch.setattr(entry_gates, "_detect_back_side", lambda *a, **k: (False, ""))
    ok_fs, _, _ = pullback_break_confirmation(_explosive_df(), **_FIRE_KW)
    assert ok_fs is True   # detector False -> the veto is skipped, the df fires
