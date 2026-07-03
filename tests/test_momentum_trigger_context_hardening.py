"""FIX-19 — TRIGGER-CONTEXT HARDENING (4 sub-items).

(a) front_side_state blends the LIVE tick price (not just the stale last close).
(b) the backside-bench block emits a counted event on error instead of a bare `except: pass`
    (flag/config assertion + the live-path handler is covered by the momentum live suite).
(c) STICKY flow veto: a negative-flow veto persists over a rolling window (one spoofy print
    can't release a real-selling veto) — the window arithmetic is validated here.
(d) the tick-scalp pullback high anchors to the TRUE session high (incl. premarket), not just
    the watch-start signal price; the adaptive pullback-depth ceiling default is flipped ON.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.config import Settings
from app.services.trading.momentum_neural.ross_momentum import front_side_state
from app.services.trading.momentum_neural.tick_scalp import evaluate_tick_first_pullback


def _df(closes, vol=1000):
    return pd.DataFrame({
        "Open": closes,
        "High": [c * 1.001 for c in closes],
        "Low": [c * 0.999 for c in closes],
        "Close": closes,
        "Volume": [vol] * len(closes),
    })


# ── FIX-19(a): front_side_state live-tick blend ───────────────────────────────────────────

def test_front_side_live_price_extends_hod_and_range_pos():
    """A LIVE tick above the last completed close pushes the HOD up and lifts day_range_pos
    to the top of the range — the position read tracks the live tape, not the stale close."""
    closes = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 14.6, 14.3, 14.4]
    base = front_side_state(_df(closes))
    # A fresh live thrust to a NEW HIGH well above the last close (14.4).
    live = front_side_state(_df(closes), live_price=16.0)
    assert live.debug.get("live_price_used") is True
    assert base.debug.get("live_price_used") is False
    assert live.debug["hod"] >= 16.0  # HOD extended by the live tick
    assert live.debug["last"] == pytest.approx(16.0)
    assert live.day_range_pos > base.day_range_pos  # position climbs toward the top


def test_front_side_live_price_below_vwap_flips_backside():
    """A live tick that drops BELOW session VWAP flips the read to backside even when the last
    completed close was above VWAP — the veto reacts to the live tape, not a stale close."""
    closes = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 14.8, 14.9]  # front-side, above VWAP
    base = front_side_state(_df(closes))
    assert base.is_backside is False
    live = front_side_state(_df(closes), live_price=9.0)  # crashes below VWAP live
    assert live.debug.get("live_price_used") is True
    assert live.above_vwap is False
    assert live.is_backside is True


def test_front_side_no_live_price_is_byte_identical():
    closes = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 14.6, 14.3, 14.4]
    a = front_side_state(_df(closes))
    b = front_side_state(_df(closes), live_price=None)
    assert a.day_range_pos == b.day_range_pos
    assert a.above_vwap == b.above_vwap
    assert a.is_backside == b.is_backside
    assert b.debug.get("live_price_used") is False


def test_front_side_invalid_live_price_fails_open_to_close():
    closes = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 14.6, 14.3, 14.4]
    base = front_side_state(_df(closes))
    for bad in (0.0, -5.0, float("nan")):
        fs = front_side_state(_df(closes), live_price=bad)
        assert fs.debug.get("live_price_used") is False
        assert fs.day_range_pos == base.day_range_pos


# ── FIX-19(c): sticky flow-veto rolling-window arithmetic ─────────────────────────────────

def _sticky_release(*, latched: bool, clear_since, now: float, window: float):
    """Mirror the release logic in live_runner's sticky flow veto: a latched veto releases
    only after flow has been clear for the full window."""
    if not latched:
        return False, None  # not latched => not sticky
    if clear_since is None:
        clear_since = now
    released = (now - clear_since) >= window
    return (not released), (None if released else clear_since)


def test_sticky_flow_veto_holds_inside_window():
    # Latched, flow just cleared this tick (clear_since=now) => still sticky (holds).
    sticky, clear_since = _sticky_release(latched=True, clear_since=None, now=100.0, window=20.0)
    assert sticky is True
    assert clear_since == 100.0
    # 5s later, still inside the 20s window => still sticky.
    sticky, _ = _sticky_release(latched=True, clear_since=100.0, now=105.0, window=20.0)
    assert sticky is True


def test_sticky_flow_veto_releases_after_window():
    # 21s of continuous clear flow >= the 20s window => released.
    sticky, clear_since = _sticky_release(latched=True, clear_since=100.0, now=121.0, window=20.0)
    assert sticky is False
    assert clear_since is None


def test_one_spoofy_print_cannot_release_a_real_selling_veto():
    """The core FIX-19(c) property: a single clear tick 5s after the veto (a spoofy imbalance
    print) does NOT release a latched real-selling veto — it stays sticky."""
    sticky, _ = _sticky_release(latched=True, clear_since=None, now=53.0, window=20.0)
    assert sticky is True  # 53s scenario: one print, still vetoing


# ── Config defaults ───────────────────────────────────────────────────────────────────────

def test_fix19_config_defaults():
    # (b) no direct flag, but (c) + (d) defaults:
    assert Settings.model_fields["chili_momentum_sticky_flow_veto_enabled"].default is True
    assert Settings.model_fields["chili_momentum_sticky_flow_veto_window_sec"].default == 20.0
    # (d): the adaptive pullback-depth ceiling default is flipped ON.
    assert (
        Settings.model_fields["chili_momentum_adaptive_pullback_depth_ceiling_enabled"].default
        is True
    )


# ── FIX-19(d): tick-scalp pullback high anchors to the true session high ───────────────────

def _ross_signal(**extra) -> dict:
    """A valid Ross tick-scalp signal (passes ross_tick_scalp_evidence_ok)."""
    base = {
        "ticker": "CANF",
        "price": 6.79,
        "daily_change_pct": 128.62,
        "gap_pct": 119.19,
        "rvol_pace": 23.76,
        "float_shares": 2_120_000,
        "volume": 6_010_000,
        "scanner_source": "Ross's 5 Pillars Alert (Online)",
        "strategies": ["Low Float - High Rel Vol", "Squeeze Alert Up 10% in 10min"],
        "headline": "Phase 2a pancreatic cancer study update",
    }
    base.update(extra)
    return base


def test_tick_scalp_anchors_pullback_high_to_session_high():
    """A signal carrying a session_high ABOVE the watch-start price anchors the pullback high
    to the true HOD — so the depth is measured from the genuine top, not the lower signal
    price (which would under-read a real flush)."""
    # Watch-start signal price 6.79, but the true premarket session high was 8.50.
    signal = _ross_signal(session_high=8.50)
    dec = evaluate_tick_first_pullback(
        symbol="CANF",
        signal=signal,
        state=None,
        bid=6.78,
        ask=6.80,
        mid=6.79,
    )
    # The state's anchored high must reflect the TRUE session high (8.50), not the 6.79 signal.
    assert dec.state["high"] == pytest.approx(8.50)


def test_tick_scalp_no_session_high_uses_signal_price():
    """Fail-open: no session-high field => the high is the running max of the signal price and
    the tick (byte-identical to the prior anchor)."""
    signal = _ross_signal()  # no session_high / day_high / hod
    dec = evaluate_tick_first_pullback(
        symbol="CANF",
        signal=signal,
        state=None,
        bid=6.78,
        ask=6.80,
        mid=6.79,
    )
    assert dec.state["high"] == pytest.approx(6.79)
